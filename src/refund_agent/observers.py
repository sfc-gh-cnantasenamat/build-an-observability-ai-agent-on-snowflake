"""Two observers on the same agent runs: TruLens (Snowflake AI Observability) and Langfuse.

TruLens: each agent is an app version of the External Agent REFUND_AGENT_LAB.DEMO.
REFUND_AGENT_LAB. A simulation is one live run per agent (`<cohort>_<agent>`); Snowflake
computes LLM-judge metrics server-side with a Cortex model.

Langfuse: the LangChain CallbackHandler sends each run to the self-hosted Langfuse on
SPCS, through a private OpenTelemetry provider so TruLens's global tracer is untouched.
Langfuse's own LLM-as-a-judge evaluators score a judge_input step on each trace, calling
Cortex through its OpenAI-compatible REST API (see scripts/langfuse_native_evals.py).
"""

from __future__ import annotations

import json

from trulens.otel.semconv.trace import SpanAttributes

from refund_agent import config

TRULENS_APP = "REFUND_AGENT_LAB"
TRULENS_METRICS = ("answer_relevance", "groundedness")
# One Cortex judge model for both observers, so their scores are comparable. It's from
# a different family than the agents (Mistral) so the judge isn't grading itself.
JUDGE_MODEL = "openai-gpt-5-mini"
# Langfuse's native evaluators reach Cortex through this LLM connection
# (scripts/langfuse_llm_connection.py) and score every observation with this name and
# metadata lab_step
# (scripts/langfuse_native_evals.py).
LANGFUSE_JUDGE_PROVIDER = "snowflake-cortex"
NATIVE_JUDGE_STEP = "judge_input"
# A judge score at or above this counts as "pass" when compared with business truth.
PASS_THRESHOLD = 0.5


def request_text(request: dict) -> str:
    """What the customer asked, as the judge sees it."""
    partial = request["amount_cents"] < request["order_total_cents"]
    return (f"Customer asks for a refund of {dollars(request['amount_cents'])} on order "
            f"{request['order_id']}" + (" (part of the order)." if partial else "."))


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


TOOL_OUTCOMES = {
    "posted": "The refund was posted.",
    "not_called": "No refund was attempted: the agent decided the request was not eligible.",
    "declined": "The refund was attempted but the card processor declined it; no money moved.",
    "pending": "The refund was refused because the payment has not settled; no money moved.",
    "duplicate": "The refund was refused as a duplicate: the order was already refunded.",
}


def context_text(run: dict, request: dict) -> list[str]:
    """Facts the reply should be grounded in, stated explicitly. The groundedness judge
    is literal: it won't infer "denied because outside the window" from an age and a
    window, so derived facts are spelled out. Amounts are in dollars (the judge doesn't
    equate "3750 cents" with "$37.50"). These are facts, not the oracle's verdict."""
    from refund_agent.agents import CACHED_POLICY

    live = request["live_policy"]
    applied = live if run["agent_policy_id"] == live["policy_id"] else CACHED_POLICY
    age = request["age_days"]

    def window(policy):
        side = "inside" if age <= policy["max_age_days"] else "outside"
        return f"{side} the {policy['max_age_days']}-day window of policy {policy['policy_id']}"

    prior = run.get("prior_refunded_cents", 0)
    facts = [
        f"Order {request['order_id']} is {age} days old: {window(live)} (the live policy)"
        + ("" if applied is live else f", and {window(applied)} (the policy the agent applied)")
        + ".",
        f"Payment status: {request['payment_status']}. Order total "
        f"{dollars(request['order_total_cents'])}.",
        f"The customer asked for {dollars(request['amount_cents'])}"
        + (f" ({'part' if request['amount_cents'] < request['order_total_cents'] else 'all'}"
           " of the order)."),
        (f"{dollars(prior)} had already been refunded for this order before this request."
         if prior else "Nothing had been refunded for this order before this request."),
        f"The agent decided the request was {'eligible' if run['eligible'] else 'not eligible'}.",
        TOOL_OUTCOMES.get(run["tool_status"], f"Refund tool result: {run['tool_status']}."),
        f"Money refunded by this request: {dollars(run['ledger_effect_cents'])}.",
    ]
    return facts


# --- TruLens -----------------------------------------------------------------------

class TruLensTracedChat:
    """Wraps a LangChain chat model so each call is a TruLens GENERATION span carrying the
    model, token counts and cost (AI credits). TruLens times the span itself. ChatSnowflake
    calls Cortex through SQL, which TruLens's own Cortex cost tracking doesn't see."""

    def __init__(self, llm, model: str):
        self.llm, self.model = llm, model

    def __getattr__(self, name):
        return getattr(self.llm, name)

    @staticmethod
    def _cost_attributes(ret, exception, self, *args, **kwargs):
        from refund_agent.agents import estimate_credits
        usage = (getattr(ret, "usage_metadata", None) or {}) if ret is not None else {}
        tokens_in, tokens_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        return {
            SpanAttributes.COST.MODEL: self.model,
            SpanAttributes.COST.NUM_PROMPT_TOKENS: tokens_in,
            SpanAttributes.COST.NUM_COMPLETION_TOKENS: tokens_out,
            SpanAttributes.COST.NUM_TOKENS: tokens_in + tokens_out,
            SpanAttributes.COST.COST: estimate_credits(self.model, tokens_in, tokens_out),
            SpanAttributes.COST.CURRENCY: "Snowflake AI credits",
        }

    from trulens.core.otel.instrument import instrument as _instrument

    @_instrument(span_type=SpanAttributes.SpanType.GENERATION, attributes=_cost_attributes)
    def invoke(self, messages, config=None, **kwargs):
        return self.llm.invoke(messages, config=config, **kwargs)


class TruLensRefundApp:
    """Thin instrumented wrapper so TruLens records input, context and reply per run."""

    def __init__(self, agent: str, graph):
        self.agent = agent
        self.graph = graph
        self.last_run = None

    from trulens.core.otel.instrument import instrument as _instrument

    @_instrument(span_type=SpanAttributes.SpanType.RETRIEVAL, attributes={
        SpanAttributes.RETRIEVAL.QUERY_TEXT: "query",
        SpanAttributes.RETRIEVAL.RETRIEVED_CONTEXTS: "return",
    })
    def record_context(self, query: str, contexts: list) -> list:
        return contexts

    @_instrument(span_type=SpanAttributes.SpanType.RECORD_ROOT, attributes={
        SpanAttributes.RECORD_ROOT.INPUT: "query",
        SpanAttributes.RECORD_ROOT.OUTPUT: "return",
        "refund_lab.run_id": "run_id",
    })
    def answer(self, query: str, run_id: str, call) -> str:
        """``call()`` runs the graph and returns the run record."""
        run, request = call()
        self.record_context(query, context_text(run, request))
        self.last_run = run
        return run["reply"]


class TruLensObserver:
    name = "trulens"

    def __init__(self, snowpark_session):
        from trulens.apps.app import TruApp
        from trulens.connectors.snowflake import SnowflakeConnector

        identity = snowpark_session.sql(
            "SELECT CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT()").collect()[0]
        if tuple(identity) != config.expected_identity():
            raise RuntimeError("Wrong account; refusing TruLens initialization")
        snowpark_session.use_database("REFUND_AGENT_LAB")
        snowpark_session.use_schema("DEMO")
        self.connector = SnowflakeConnector(snowpark_session=snowpark_session)
        self._TruApp = TruApp
        self.apps: dict[str, tuple] = {}

    def app_for(self, agent: str, graph):
        if agent not in self.apps:
            wrapper = TruLensRefundApp(agent, graph)
            tru_app = self._TruApp(wrapper, app_name=TRULENS_APP, app_version=agent,
                                   connector=self.connector, main_method=wrapper.answer,
                                   start_evaluator=False)
            self.apps[agent] = (wrapper, tru_app)
        return self.apps[agent]

    def run_name(self, cohort: str, agent: str) -> str:
        return f"{cohort}_{agent}"

    @staticmethod
    def chat_factory(factory):
        """Wrap ``factory(model)`` so agent model calls become TruLens cost spans."""
        return lambda model: TruLensTracedChat(factory(model), model)

    def batch(self, cohort: str, agent: str, graph, loop):
        """Run all of one agent's requests inside one TruLens live run.

        TruLens ingests a live run once, when the decorated function returns, so the
        whole batch must happen inside it.
        """
        from trulens.core.app import trace_with_run
        from trulens.core.run import RunConfig

        _, tru_app = self.app_for(agent, graph)
        # trace_with_run would create the run with the default judge; create it first
        # with JUDGE_MODEL (trace_with_run then reuses the existing run).
        try:
            tru_app.add_run(run_config=RunConfig(
                run_name=self.run_name(cohort, agent), dataset_name=f"live_{cohort}",
                source_type="DATAFRAME", dataset_spec={}, llm_judge_name=JUDGE_MODEL))
        except Exception:  # already exists
            pass
        trace_with_run(app=tru_app, run_name=self.run_name(cohort, agent))(loop)()

    def invoke(self, agent: str, request: dict, run_id: str, do_run):
        """Route one run through the instrumented root so TruLens records it."""
        wrapper, _ = self.apps[agent]
        wrapper.answer(request_text(request), run_id, lambda: (do_run(), request))
        return wrapper.last_run

    def flush(self):
        from trulens.core.session import TruSession
        TruSession().force_flush()

    @staticmethod
    def business_correct_metric(conn, cohort: str):
        """A client-side TruLens metric holding business truth: 1.0 when RUN_EVIDENCE
        marks the run correct, else 0.0. No LLM is involved; it copies the oracle's
        verdict into TruLens so it sits beside the judge scores in Snowsight."""
        from trulens.core import Metric, Selector

        cursor = conn.cursor()
        try:
            cursor.execute("SELECT RUN_ID, BUSINESS_CORRECT FROM REFUND_AGENT_LAB.DEMO.RUN_EVIDENCE "
                           "WHERE COHORT_ID = ?", (cohort,))
            truth = dict(cursor.fetchall())
        finally:
            cursor.close()

        def business_correct(run_id: str) -> float:
            return 1.0 if truth[run_id] else 0.0

        return Metric(name="business_correct", implementation=business_correct,
                      selectors={"run_id": Selector(
                          span_type=SpanAttributes.SpanType.RECORD_ROOT,
                          span_attribute="refund_lab.run_id", collect_list=False)})

    def compute(self, cohort: str, agents, conn=None):
        """Ask Snowflake to compute judge metrics for each agent's live run, plus the
        business_correct metric when a connection is given."""
        statuses = {}
        metrics = list(TRULENS_METRICS)
        if conn is not None:
            metrics.append(self.business_correct_metric(conn, cohort))
        for agent in agents:
            _, tru_app = self.apps[agent]
            run = tru_app.get_run(self.run_name(cohort, agent))
            statuses[agent] = run.compute_metrics(metrics)
        return statuses


# --- Langfuse ----------------------------------------------------------------------

JUDGE_PROMPT = (
    "You grade a customer-support reply about a refund. Score each criterion from 0 to 1.\n"
    "relevance: does the reply address the customer's request?\n"
    "helpfulness: is the reply clear, polite and useful to the customer?\n"
    'Respond with only JSON: {"relevance": <0-1>, "helpfulness": <0-1>, "reason": "<short>"}'
)


class LangfuseObserver:
    name = "langfuse"

    def __init__(self, host: str, public_key: str, secret_key: str, spcs_token: str,
                 judge_llm=None):
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler
        from opentelemetry.sdk.trace import TracerProvider

        self.client = Langfuse(
            public_key=public_key, secret_key=secret_key, base_url=host,
            additional_headers={"X-SF-SPCS-Authorization": f'Snowflake Token="{spcs_token}"'},
            tracer_provider=TracerProvider())  # private: don't disturb TruLens's tracer
        self.public_key = public_key
        self._secret_key = secret_key
        self._spcs_token = spcs_token
        self.host = host.rstrip("/")
        self._handler_cls = CallbackHandler
        self.judge_llm = judge_llm
        self.trace_ids: dict[str, str] = {}

    def trace_id(self, run_id: str) -> str:
        """Langfuse trace ID derived from the business run ID, so the two always join."""
        return self.client.create_trace_id(seed=run_id)

    def guard(self, fn):
        """Run ``fn`` in a copied context: the Langfuse callback attaches its own spans as
        the current OpenTelemetry context, which would otherwise leak into TruLens and
        strip its record ID from later spans."""
        import contextvars
        return contextvars.copy_context().run(fn)

    def callbacks(self, agent: str, request: dict, run_id: str) -> list:
        return [self._handler_cls(public_key=self.public_key,
                                  trace_context={"trace_id": self.trace_id(run_id)})]

    def judge(self, run: dict, request: dict) -> dict:
        """Client-side LLM judge (Cortex), stored in Langfuse as scores on the run's trace."""
        from langchain_core.messages import HumanMessage, SystemMessage
        import re

        out = self.judge_llm.invoke([
            SystemMessage(JUDGE_PROMPT),
            HumanMessage(f"Request: {request_text(request)}\nReply: {run['reply']}")])
        text = str(out.content)
        start = text.find("{")
        try:
            # raw_decode reads the first JSON object and ignores any text after it.
            parsed = json.JSONDecoder().raw_decode(text[start:])[0] if start >= 0 else {}
            scores = {k: float(parsed[k]) for k in ("relevance", "helpfulness") if k in parsed}
        except (ValueError, TypeError):
            return {}  # unparseable judge output: leave this run unscored
        if not isinstance(parsed, dict):
            return {}
        for name, value in scores.items():
            self.client.create_score(name=name, value=value, trace_id=self.trace_id(run["run_id"]),
                                     data_type="NUMERIC", comment=parsed.get("reason"),
                                     metadata={"judge": JUDGE_MODEL, "run_id": run["run_id"]})
        return scores

    def after_run(self, agent: str, run: dict, request: dict) -> None:
        if self.judge_llm is not None:
            run["langfuse_scores"] = self.judge(run, request)
        else:
            self.record_judge_input(run, request)

    def record_judge_input(self, run: dict, request: dict) -> None:
        """Add the step Langfuse's native evaluators score: the same request and facts
        TruLens sees, with the reply as output."""
        step = self.client.start_observation(
            trace_context={"trace_id": self.trace_id(run["run_id"])}, name=NATIVE_JUDGE_STEP,
            input={"request": request_text(request),
                   "facts": "\n".join(context_text(run, request))},
            output=run["reply"],
            metadata={"run_id": run["run_id"], "lab_step": NATIVE_JUDGE_STEP})
        step.end()
        run["judge_step_id"] = step.id

    def post_truth(self, conn, runs: list[dict]) -> int:
        """Copy business truth into Langfuse beside its judges' scores, on the same
        judge_input step so Score Analytics pairs them: business_correct (BOOLEAN, and
        as 0/1 NUMERIC) from RUN_EVIDENCE, and each judge score as pass/fail at
        PASS_THRESHOLD (the cutoff JUDGE_AGREEMENT uses). Returns scores posted."""
        steps = {run["run_id"]: run for run in runs if run.get("judge_step_id")}
        if not steps:
            return 0
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT RUN_ID, BUSINESS_CORRECT FROM REFUND_AGENT_LAB.DEMO.RUN_EVIDENCE "
                "WHERE RUN_ID IN (SELECT VALUE::VARCHAR FROM TABLE(FLATTEN(PARSE_JSON(?))))",
                (json.dumps(list(steps)),))
            truth = dict(cursor.fetchall())
        finally:
            cursor.close()
        posted = 0
        for run_id, correct in truth.items():
            run = steps[run_id]
            target = {"trace_id": self.trace_id(run_id), "observation_id": run["judge_step_id"]}
            scores = [("business_correct", 1 if correct else 0, "BOOLEAN"),
                      ("business_correct_value", 1.0 if correct else 0.0, "NUMERIC")]
            scores += [(f"{metric}_pass", 1 if value >= PASS_THRESHOLD else 0, "BOOLEAN")
                       for metric, value in (run.get("langfuse_scores") or {}).items()]
            for name, value, data_type in scores:
                self.client.create_score(name=name, value=value, data_type=data_type,
                                         comment="From Snowflake RUN_EVIDENCE"
                                         if name.startswith("business_correct") else
                                         f"Judge score >= {PASS_THRESHOLD}", **target)
                posted += 1
        self.client.flush()
        return posted

    def fetch_scores(self, run_id: str) -> list[dict]:
        import requests
        r = requests.get(f"{self.host}/api/public/v3/scores", headers=self._headers(),
                         params={"traceId": self.trace_id(run_id), "limit": 50}, timeout=30)
        r.raise_for_status()
        return r.json().get("data", [])

    def flush(self):
        self.client.flush()

    def _headers(self):
        import base64
        basic = base64.b64encode(f"{self.public_key}:{self._secret_key}".encode()).decode()
        return {"X-SF-SPCS-Authorization": f'Snowflake Token="{self._spcs_token}"',
                "Authorization": f"Basic {basic}"}

    def fetch_observations(self, run_id: str) -> list[dict]:
        import requests
        r = requests.get(f"{self.host}/api/public/v2/observations", headers=self._headers(),
                         params={"traceId": self.trace_id(run_id), "limit": 100,
                                 "fields": "core,basic,usage,model,metrics"}, timeout=30)
        r.raise_for_status()
        return r.json().get("data", [])

    def export(self, conn, runs: list[dict], wait_s: int = 60, score_wait_s: int = 300) -> dict:
        """Read each run's trace back from Langfuse and load traces and scores into
        Snowflake. Native judges score asynchronously, so their scores are polled for up
        to ``score_wait_s``. Returns counts of runs found and rows written."""
        import time

        pending = {run["run_id"] for run in runs}
        found: dict[str, list[dict]] = {}
        deadline = time.monotonic() + wait_s
        while pending and time.monotonic() < deadline:
            for run_id in list(pending):
                observations = self.fetch_observations(run_id)
                if observations:
                    found[run_id] = observations
                    pending.discard(run_id)
            if pending:
                time.sleep(5)
        if self.judge_llm is None:
            want = {run["run_id"] for run in runs}
            deadline = time.monotonic() + score_wait_s
            while want and time.monotonic() < deadline:
                for run in runs:
                    if run["run_id"] not in want:
                        continue
                    native = {s["name"]: s["value"] for s in self.fetch_scores(run["run_id"])
                              if s.get("source") == "EVAL" and s.get("dataType") == "NUMERIC"}
                    if len(native) >= 2:
                        run["langfuse_scores"] = native
                        want.discard(run["run_id"])
                if want:
                    time.sleep(10)
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN")
            for run_id, observations in found.items():
                for o in observations:
                    usage = o.get("usageDetails") or {}
                    cursor.execute(
                        "INSERT INTO REFUND_AGENT_LAB.DEMO.LANGFUSE_TRACES (RUN_ID, TRACE_ID, "
                        "OBSERVATION_ID, PARENT_OBSERVATION_ID, TYPE, NAME, MODEL, INPUT_TOKENS, "
                        "OUTPUT_TOKENS, START_TIME, END_TIME, COST_CREDITS, EXPORTED_AT) "
                        "SELECT ?,?,?,?,?,?,?,?,?,TO_TIMESTAMP_TZ(?),TO_TIMESTAMP_TZ(?),?,"
                        "CURRENT_TIMESTAMP() WHERE NOT EXISTS (SELECT 1 FROM "
                        "REFUND_AGENT_LAB.DEMO.LANGFUSE_TRACES WHERE OBSERVATION_ID = ?)",
                        (run_id, o["traceId"], o["id"], o.get("parentObservationId"),
                         o.get("type"), o.get("name"), o.get("providedModelName") or o.get("model"),
                         usage.get("input"), usage.get("output"), o.get("startTime"),
                         o.get("endTime"), o.get("totalCost"), o["id"]))
            scored = 0
            for run in runs:
                for metric, score in (run.get("langfuse_scores") or {}).items():
                    cursor.execute(
                        "INSERT INTO REFUND_AGENT_LAB.DEMO.LANGFUSE_SCORES (RUN_ID, TRACE_ID, "
                        "METRIC, JUDGE, SCORE, SCORED_AT) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP())",
                        (run["run_id"], self.trace_id(run["run_id"]), metric, JUDGE_MODEL, score))
                    scored += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
        truth_scores = self.post_truth(conn, runs) if self.judge_llm is None else 0
        return {"runs": len(runs), "traces_found": len(found), "missing": len(pending),
                "observations": sum(map(len, found.values())), "scores": scored,
                "truth_scores": truth_scores}
