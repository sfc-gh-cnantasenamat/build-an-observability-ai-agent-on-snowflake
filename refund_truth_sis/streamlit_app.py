"""Refund agent lab: pick an agent on the usual signals, then check business truth.

Three LangGraph agents (Cortex models via LangChain) answer the same synthetic refund
requests. TruLens and Langfuse observe the same runs. Snowflake's RUN_EVIDENCE view is the
business oracle. Every order, payment and refund is simulated.
"""

import importlib
import os
from uuid import uuid4

import pandas as pd
import streamlit as st

import refund_agent.agents
import refund_agent.config
import refund_agent.events
import refund_agent.lab
import refund_agent.observers

# The hosted container keeps imported modules cached across redeploys and only reruns
# this script. Reload any shared module whose file changed, dependencies first.
for _module in (refund_agent.config, refund_agent.events, refund_agent.agents,
                refund_agent.lab, refund_agent.observers):
    _mtime = os.path.getmtime(_module.__file__)
    if getattr(_module, "_LOADED_MTIME", _mtime) != _mtime:
        importlib.reload(_module)
    _module._LOADED_MTIME = _mtime

from refund_agent.agents import AGENTS, build_agent
from refund_agent.lab import AGENT_NAMES, MAX_ORDERS, SCENARIOS, SqlSink, simulate
from refund_agent.observers import (JUDGE_MODEL, PASS_THRESHOLD, TRULENS_METRICS,
                                    LangfuseObserver, TruLensObserver)

AGENT_LABELS = {"lean": "Lean", "baseline": "Baseline", "fixed": "Fixed"}

COHORTS_SQL = """
SELECT r.COHORT_ID, MAX(e.EVENT_AT) AS LAST_EVENT
FROM REFUND_AGENT_LAB.DEMO.RUNS r
JOIN REFUND_AGENT_LAB.DEMO.TRACE_EVENTS e ON e.RUN_ID = r.RUN_ID
WHERE STARTSWITH(r.COHORT_ID, 'APP_') AND r.AGENT_POLICY_ID IS NOT NULL
GROUP BY r.COHORT_ID ORDER BY LAST_EVENT DESC LIMIT 20
"""
RUNS_SQL = """
SELECT * FROM REFUND_AGENT_LAB.DEMO.RUN_EVIDENCE WHERE COHORT_ID = ? ORDER BY TRIAL, VARIANT
"""
ORDERS_SQL = """
SELECT o.ORDER_ID, o.AGE_DAYS, o.AMOUNT_CENTS, p.PAYMENT_STATUS, p.PROCESSOR_STATUS
FROM REFUND_AGENT_LAB.DEMO.ORDERS o JOIN REFUND_AGENT_LAB.DEMO.PAYMENTS p USING (ORDER_ID)
WHERE STARTSWITH(o.ORDER_ID, ?) ORDER BY o.ORDER_ID
"""
TRULENS_SQL = """
SELECT RUN_ID, METRIC, SCORE FROM REFUND_AGENT_LAB.DEMO.TRULENS_SCORES
WHERE STARTSWITH(TRULENS_RUN, ?)
"""
LANGFUSE_SQL = """
SELECT s.RUN_ID, s.METRIC, s.SCORE, s.TRACE_ID FROM REFUND_AGENT_LAB.DEMO.LANGFUSE_SCORES s
JOIN REFUND_AGENT_LAB.DEMO.RUNS r ON r.RUN_ID = s.RUN_ID WHERE r.COHORT_ID = ?
"""
AGREEMENT_SQL = """
SELECT * FROM REFUND_AGENT_LAB.DEMO.JUDGE_AGREEMENT WHERE COHORT_ID = ? ORDER BY METRIC, TOOL
"""
LLM_SQL = """
SELECT c.RUN_ID, c.NODE, c.MODEL, c.INPUT_TOKENS, c.OUTPUT_TOKENS, ROUND(c.LATENCY_MS) AS LATENCY_MS
FROM REFUND_AGENT_LAB.DEMO.LLM_CALLS c JOIN REFUND_AGENT_LAB.DEMO.RUNS r ON r.RUN_ID = c.RUN_ID
WHERE r.COHORT_ID = ? ORDER BY c.CALLED_AT
"""

st.set_page_config(page_title="Refund truth lab", page_icon=":material/fact_check:",
                   layout="wide")
st.session_state.setdefault("sim_error", None)
st.session_state.setdefault("sim_notes", [])
st.session_state.setdefault("revealed", False)

try:
    connection = st.connection(os.getenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME") or "snowflake",
                               type="snowflake")
except Exception as error:
    st.error(f"Could not connect to Snowflake: {error}")
    st.stop()

# Hosted in Snowflake, the app always runs in its own account: point the write guard
# there. The Langfuse ingress URL comes from the langfuse_host secret (app_access.sql).
_identity = connection.query("SELECT CURRENT_ORGANIZATION_NAME() AS O, CURRENT_ACCOUNT() AS A",
                             ttl=0).iloc[0]
refund_agent.config.ACCOUNT = f"{_identity['O']}-{_identity['A']}"
try:
    LANGFUSE_HOST = "https://" + st.secrets["langfuse_host"].removeprefix("https://").rstrip("/")
except Exception:
    LANGFUSE_HOST = refund_agent.config.LANGFUSE_HOST


def frame(sql, *params):
    result = connection.query(sql, params=list(params) or None, ttl=0)
    result.columns = [str(column).lower() for column in result.columns]
    return result


IWH = "REFUND_LAB_IWH"


def leaderboard_frames(cohort):
    """Read the leaderboard inputs on the interactive warehouse when it's already running.

    Each resume of an interactive warehouse starts a new one-hour minimum charge, so the app
    never resumes it: run the notebook or scripts/run_simulation.py to start it. Writes and
    the TruLens read (AI Observability event table) stay on the standard warehouse.
    """
    try:
        cursor = connection.raw_connection.cursor()
        cursor.execute(f"SHOW WAREHOUSES LIKE '{IWH}'")
        columns = [d[0].lower() for d in cursor.description]
        state = dict(zip(columns, cursor.fetchone() or ())).get("state")
    except Exception:
        state = None
    if state != "STARTED":
        return frame(RUNS_SQL, cohort), frame(LANGFUSE_SQL, cohort), "standard"
    cursor.execute("SELECT CURRENT_WAREHOUSE()")
    home = cursor.fetchone()[0]
    try:
        cursor.execute(f"USE WAREHOUSE {IWH}")
        return frame(RUNS_SQL, cohort), frame(LANGFUSE_SQL, cohort), "interactive"
    finally:
        cursor.execute(f"USE WAREHOUSE {home}")


@st.cache_resource
def agent_graphs():
    from langchain_snowflake import ChatSnowflake
    session = connection.session()
    # Each model call is also a TruLens cost span; it's a plain call outside a TruLens run.
    chat = TruLensObserver.chat_factory(
        lambda m: ChatSnowflake(model=m, session=session, temperature=0))
    return {a: build_agent(a, chat)
            for a in AGENT_NAMES}


@st.cache_resource
def trulens_observer():
    return TruLensObserver(connection.session())


def langfuse_observer():
    """Langfuse needs its API keys (Streamlit secrets) and a Snowflake ingress token. Its
    own evaluators score each run inside Langfuse (native judges)."""
    token = connection.raw_connection._rest._token_request("ISSUE")["data"]["sessionToken"]
    return LangfuseObserver(LANGFUSE_HOST, st.secrets["langfuse_public_key"],
                            st.secrets["langfuse_secret_key"], token)


def order_label(order_id):
    suffix = str(order_id).rsplit("_", 1)[-1]
    return f"O{int(suffix[1:])}" if suffix[1:].isdigit() else suffix


# --- Sidebar ------------------------------------------------------------------------
with st.sidebar:
    st.header("Simulation")
    use_scenarios = st.toggle(
        "Fixed scenario set", value=True, key="use_scenarios",
        help=f"{len(SCENARIOS)} orders and {len(SCENARIOS) + 1} requests that trigger each "
             "seeded flaw a known number of times. Turn off for random orders.")
    order_count = st.number_input("Orders", min_value=2, max_value=MAX_ORDERS, value=20,
                                  step=1, key="order_count", disabled=use_scenarios)
    limit = int(order_count) - 1
    if st.session_state.get("request_count", 0) > limit:
        st.session_state.request_count = limit
    request_count = st.number_input(
        "Refund requests", min_value=1, max_value=limit, value=min(15, limit), step=1,
        key="request_count", help="Fewer customers ask for refunds than place orders.",
        disabled=use_scenarios)
    agents = st.multiselect("Agents", list(AGENT_NAMES), default=list(AGENT_NAMES),
                            format_func=AGENT_LABELS.get, key="agents")
    observers_on = st.multiselect("Observers", ["TruLens", "Langfuse"],
                                  default=["TruLens", "Langfuse"], key="observers")
    run_clicked = st.button("Run simulation", type="primary", icon=":material/play_arrow:",
                            key="run_simulation", width="stretch", disabled=not agents)

if run_clicked:
    cohort = f"APP_{uuid4().hex[:16].upper()}"
    notes, observers = [], []
    if "TruLens" in observers_on:
        try:
            observers.append(trulens_observer())
        except Exception as error:
            notes.append(f"TruLens is off for this run: {error}")
    langfuse = None
    if "Langfuse" in observers_on:
        try:
            langfuse = langfuse_observer()
            observers.append(langfuse)
        except Exception as error:
            notes.append(f"Langfuse is off for this run (the notebook can still send traces): "
                         f"{type(error).__name__}: {error}")
    bar = st.progress(0.0, text="Generating orders...")
    total = len(agents) * (len(SCENARIOS) + 1 if use_scenarios else int(request_count))
    done = {"n": 0}

    def progress(agent, trial, count):
        done["n"] += 1
        bar.progress(done["n"] / total,
                     text=f"{AGENT_LABELS[agent]} agent: request {trial} of {count}")

    try:
        runs = simulate(connection.raw_connection, cohort, int(order_count),
                        int(request_count), agent_graphs(), SqlSink(connection.raw_connection),
                        agents=agents, observers=observers, progress=progress,
                        scenarios=use_scenarios)
        if langfuse is not None:
            bar.progress(1.0, text="Waiting for Langfuse's judges, then exporting to Snowflake...")
            langfuse.flush()
            result = langfuse.export(connection.raw_connection, runs, score_wait_s=180)
            if result["missing"]:
                notes.append(f"Langfuse: {result['missing']} traces had not arrived yet.")
            if result["scores"] < 2 * len(runs):
                notes.append(f"Langfuse: its judges had scored {result['scores'] // 2} of "
                             f"{len(runs)} runs when the export ran.")
        if any(o.name == "trulens" for o in observers):
            observer = trulens_observer()
            observer.flush()
            notes.append("TruLens judge scores are computed in Snowflake after ingestion; "
                         "use Score with TruLens in a minute or two.")
        st.session_state.update(review_cohort=cohort, sim_error=None, sim_notes=notes,
                                revealed=False)
        bar.empty()
        st.rerun()
    except Exception as error:
        bar.empty()
        st.session_state.update(sim_error=str(error), sim_notes=notes)

try:
    cohorts = frame(COHORTS_SQL).cohort_id.tolist()
except Exception as error:
    st.error(f"Could not list simulations: {error}")
    st.stop()

with st.sidebar:
    st.divider()
    if not cohorts:
        st.info("Run a simulation to begin.")
    elif st.session_state.get("review_cohort") not in cohorts:
        st.session_state.review_cohort = cohorts[0]
    cohort = st.selectbox("Simulation to review", cohorts, key="review_cohort") if cohorts else None
    st.caption("Every refund is simulated. No real money moves.")

st.title("Did the refund actually happen?")
st.caption("Three LangGraph agents on Snowflake Cortex | TruLens and Langfuse observe the "
           "same runs | Snowflake holds business truth")
if st.session_state.sim_error:
    st.error(f"The simulation did not complete: {st.session_state.sim_error}")
for note in st.session_state.sim_notes:
    st.info(note)
if not cohort:
    st.stop()

try:
    runs, langfuse, served_by = leaderboard_frames(cohort)
    trulens = frame(TRULENS_SQL, f"{cohort}_")
except Exception as error:
    st.error(f"Could not load the evidence: {error}")
    st.stop()
if runs.empty:
    st.warning("This simulation has no runs.")
    st.stop()

scores = runs[["run_id", "variant"]].copy()
for source, table in (("trulens", trulens), ("langfuse", langfuse)):
    if not table.empty:
        wide = table.pivot_table(index="run_id", columns="metric", values="score").add_prefix(
            f"{source}_")
        scores = scores.merge(wide, left_on="run_id", right_index=True, how="left")
board_runs = runs.merge(scores.drop(columns="variant"), on="run_id", how="left")

# --- 1. Pick a winner --------------------------------------------------------------
st.header("1. Pick a winner")
st.caption("These are the signals a team usually has: cost, speed, the agent's own report "
           "and two LLM judges. Pick the agent you'd ship before revealing business truth.")
st.caption(f"Leaderboard served by the interactive warehouse {IWH}." if served_by == "interactive"
           else f"Leaderboard served by the standard warehouse ({IWH} is suspended; the "
                "notebook and scripts/run_simulation.py start it).")


def leaderboard(df):
    rows = []
    for agent, group in df.groupby("variant"):
        row = {"Agent": AGENT_LABELS.get(agent, agent),
               "Model": AGENTS[agent]["model"] if agent in AGENTS else "",
               "Microcredits / request": round(group.model_cost_credits.mean() * 1e6),
               "p50 latency (s)": round(group.agent_latency_ms.median() / 1000, 1),
               "Self-reported success %": round(100 * group.claim.isin(
                   ["refunded", "already_refunded"]).mean())}
        for col, label in (("trulens_answer_relevance", "TruLens relevance"),
                           ("trulens_groundedness", "TruLens groundedness"),
                           ("langfuse_answer_relevance", "Langfuse relevance"),
                           ("langfuse_groundedness", "Langfuse groundedness")):
            row[label] = round(group[col].mean(), 2) if col in group and group[col].notna().any() else None
        row.update({"Business-correct %": round(100 * group.business_correct.mean()),
                    "False successes": int(group.false_success.sum()),
                    "Customers misinformed": int(group.customer_misinformed.sum()),
                    "Money at risk ($)": group.money_at_risk_cents.sum() / 100,
                    "Root causes": ", ".join(sorted(group.root_cause.dropna().unique()))})
        rows.append(row)
    return pd.DataFrame(rows).sort_values("Microcredits / request")


board = leaderboard(board_runs)
truth_cols = ["Business-correct %", "False successes", "Customers misinformed",
              "Money at risk ($)", "Root causes"]
pending = [label for label in ("TruLens relevance", "Langfuse relevance")
           if board[label].isna().all()]
with st.container(horizontal=True):
    pick = st.segmented_control("Your pick", list(board.Agent), key=f"pick_{cohort}")
    if st.button("Reveal business truth", icon=":material/visibility:", disabled=not pick,
                 key="reveal"):
        st.session_state.revealed = True
    if "TruLens relevance" in pending and st.button("Score with TruLens",
                                                    icon=":material/grading:", key="score_tl"):
        try:
            observer = trulens_observer()
            for agent in runs.variant.unique():
                observer.app_for(agent, agent_graphs()[agent])
            statuses = observer.compute(cohort, list(runs.variant.unique()),
                                        conn=connection.raw_connection)
            st.toast(f"TruLens: {statuses}")
        except Exception as error:
            st.error(f"TruLens scoring failed: {error}")
shown = board if st.session_state.revealed else board.drop(columns=truth_cols)
st.dataframe(shown, hide_index=True, key=f"board_{cohort}",
             column_config={"Money at risk ($)": st.column_config.NumberColumn(format="$%.2f")})
if pending:
    st.caption("Scores pending: " + ", ".join(pending) +
               ". Judge scores arrive after the run; reload in a minute.")
if st.session_state.revealed and pick:
    best = board.sort_values(["Business-correct %", "Money at risk ($)"],
                             ascending=[False, True]).Agent.iloc[0]
    cheapest = board.Agent.iloc[0]
    if pick == best:
        st.success(f"You picked {pick}, the most business-correct agent.")
    else:
        st.error(f"You picked {pick}. The most business-correct agent is {best}. "
                 f"The cheapest was {cheapest}.")
    agreement = frame(AGREEMENT_SQL, cohort)
    if not agreement.empty:
        st.subheader("How well each judge agrees with business truth")
        st.caption(
            f"Judge model for both tools: {JUDGE_MODEL}. Business truth is BUSINESS_CORRECT "
            "from RUN_EVIDENCE, copied into each tool as business_correct. Same statistics "
            "as Langfuse Score Analytics: correlation and error use the raw score against "
            "truth as 0/1; agreement, Cohen's kappa and F1 count a score of at least "
            f"{PASS_THRESHOLD} as a pass. Kappa near 0 means no better than chance.")
        stats = agreement.assign(Judge=agreement.tool + " " + agreement.metric.str.replace("_", " "))
        st.dataframe(
            stats[["Judge", "n", "pearson", "spearman", "mae", "rmse", "agreement", "kappa", "f1"]]
            .rename(columns={"n": "Runs", "pearson": "Pearson", "spearman": "Spearman",
                             "mae": "MAE", "rmse": "RMSE", "agreement": "Agreement",
                             "kappa": "Kappa", "f1": "F1"}),
            hide_index=True, key=f"agreement_{cohort}")
        cols = st.columns(len(stats))
        for col, row in zip(cols, stats.itertuples()):
            with col:
                st.caption(row.Judge)
                st.dataframe(pd.DataFrame(
                    {"Correct": [row.pass_correct, row.fail_correct],
                     "Wrong": [row.pass_wrong, row.fail_wrong]},
                    index=["Judge: pass", "Judge: fail"]), key=f"matrix_{cohort}_{row.Index}")

# --- 2. Refund requests -----------------------------------------------------------
st.header("2. Refund requests")


def verdict(row):
    if row.business_correct:
        return "Correct"
    return {"stale_data": "Wrong: stale policy", "tool": "Wrong: tool",
            "prompt": "Wrong: false reply", "model_error": "Wrong: model"}.get(
        row.root_cause, "Wrong")


requests = []
for trial, group in runs.groupby("trial"):
    first = group.iloc[0]
    row = {"Request": int(trial), "Order": order_label(first.order_id),
           "Asked ($)": first.amount_cents / 100,
           "Order total ($)": (first.order_total_cents or first.amount_cents) / 100,
           "Age (days)": first.age_days, "Payment": first.payment_status,
           "Processor": first.processor_status}
    for _, run in group.iterrows():
        row[AGENT_LABELS.get(run.variant, run.variant)] = verdict(run)
    requests.append(row)
requests = pd.DataFrame(requests)
agent_cols = [AGENT_LABELS[a] for a in AGENT_NAMES if AGENT_LABELS[a] in requests]
wrong_only = st.toggle("Wrong only", key="wrong_only")
view = requests[requests[agent_cols].ne("Correct").any(axis=1)] if wrong_only else requests
selection = st.dataframe(view, hide_index=True, key=f"requests_{cohort}_{wrong_only}",
                         on_select="rerun", selection_mode="single-row",
                         column_config={"Asked ($)": st.column_config.NumberColumn(format="$%.2f"),
                                        "Order total ($)": st.column_config.NumberColumn(format="$%.2f")})

# --- 3. Investigate ---------------------------------------------------------------
st.header("3. Investigate")
rows = list(getattr(getattr(selection, "selection", None), "rows", []) or [])
if view.empty:
    st.info("No requests match the filter.")
    st.stop()
chosen = view.iloc[rows[0]] if rows and rows[0] < len(view) else view.iloc[0]
trial = int(chosen["Request"])
st.subheader(f"Request {trial} on {chosen['Order']}")
st.caption(f"Asked for ${chosen['Asked ($)']:.2f} of a ${chosen['Order total ($)']:.2f} order, "
           f"{chosen['Age (days)']} days old, payment {chosen['Payment']}, processor "
           f"{chosen['Processor']}. Live policy: "
           f"{runs.policy_id.iloc[0]} ({int(runs.max_age_days.iloc[0])} days).")
try:
    llm = frame(LLM_SQL, cohort)
except Exception:
    llm = pd.DataFrame()
request_runs = board_runs[board_runs.trial == trial]
for column, (_, run) in zip(st.columns(len(request_runs)), request_runs.iterrows()):
    with column:
        st.markdown(f"**{AGENT_LABELS.get(run.variant, run.variant)}** · "
                    f"`{AGENTS.get(run.variant, {}).get('model', '')}`")
        st.markdown(f"Policy used **{run.agent_policy_id}** · decided "
                    f"**{'refund' if run.attempted else 'no refund'}** · tool "
                    f"**{run.tool_status}** · moved **${run.ledger_effect_cents / 100:.2f}**")
        st.markdown(f"> {run.reply}")
        if run.business_correct:
            st.success("Matches business truth.")
        else:
            st.error(f"{verdict(run)}. Expected ${run.expected_effect_cents / 100:.2f} to move; "
                     f"${run.money_at_risk_cents / 100:.2f} at risk"
                     + ("; the customer was misinformed." if run.customer_misinformed else "."))
        judges = {k: run.get(k) for k in ("trulens_answer_relevance", "trulens_groundedness",
                                          "langfuse_answer_relevance", "langfuse_groundedness")}
        st.caption(" · ".join(f"{k.replace('_', ' ')}: "
                              f"{'pending' if pd.isna(v) else f'{v:.2f}'}"
                              for k, v in judges.items()))
        trace = langfuse[langfuse.run_id == run.run_id]
        if not trace.empty:
            st.link_button("Langfuse trace", f"{LANGFUSE_HOST}/project/refund-agent-lab/traces/"
                           f"{trace.trace_id.iloc[0]}", icon=":material/open_in_new:")
        calls = llm[llm.run_id == run.run_id].drop(columns="run_id") if not llm.empty else llm
        if not calls.empty:
            st.dataframe(calls, hide_index=True, key=f"calls_{run.run_id}")

st.caption("Judge scores grade the reply's wording; only Snowflake's RUN_EVIDENCE checks the "
           "money against policy, payment, processor and ledger. Cost is estimated from "
           "Cortex token counts at published credit rates.")
