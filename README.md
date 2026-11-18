# Build an Observability AI Agent on Snowflake

Companion code for the Snowflake developer guide [Build an Observability AI Agent on Snowflake](https://github.com/sfc-gh-cnantasenamat/sfguide-build-an-observability-ai-agent-on-snowflake).

Three LangGraph refund agents answer the same synthetic refund requests. Each looks good on at least one usual signal: cost, its own reported success, or an LLM judge. Only Snowflake, which joins each run to orders, policies, payments and the ledger, shows which agent actually did the right thing with the money. All data is synthetic; nothing touches a real payment system.

**Lean and Baseline are broken on purpose.** Each has a planted flaw in code (a stale cached policy, a reply template that always reports success, a buggy refund tool) standing in for a mistake a real team could ship. Fixed is the control. The lab tests whether each signal can spot these flaws; it isn't a comparison of the Claude models.

| Agent | Model (Cortex) | Policy | Reply | Refund tool | Planted flaw |
| --- | --- | --- | --- | --- | --- |
| Lean | `claude-haiku-5-5` | cached 30-day snapshot | the model's reply | v1 | stale data, tool |
| Baseline | `claude-sonnet-5-5` | live | reply template v1 | v1 | reply, tool |
| Fixed | `claude-sonnet-5-5` | live | reply template v2 | v2 | none |

The refund window shrank from 30 to 14 days (policy `P_2026` to `P_2026_14D`). Tool v1 refunds the whole order on a partial request and ignores unsettled payments; tool v2 doesn't. Reply template v1 tells the customer the refund is complete whenever the agent decided to refund, whatever the tool returned; v2 sends the model's own reply.

## Quick start

Follow the guide for the full setup, including Langfuse on Snowpark Container Services. In short:

```bash
cp refund_lab.env.example .refund_lab.env   # fill in your connection, account, warehouse, Langfuse host and admin email
source .refund_lab.env
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-observer.txt -r requirements-streaming.txt
.venv/bin/python -m unittest discover -s tests -t .      # no Snowflake calls
.venv/bin/jupyter lab notebooks/refund_agent_lab.ipynb   # the notebook (start it after sourcing .refund_lab.env)
.venv/bin/python scripts/run_simulation.py --scenarios    # live run: fixed scenario set, both observers
.venv/bin/python scripts/run_simulation.py --eval         # optional: 300-run judge-alignment eval set
```

The live run is the whole workflow in one command:
1. It writes runs, trace events and model calls with **Snowpipe Streaming**, as the key-pair service user `REFUND_LAB_STREAMER`. Create it first with `sql/streaming_setup.sql`; the guide shows how to generate its key at `~/.snowflake/keys/refund_lab_streamer.p8`. Add `--sql` to use ordinary inserts instead.
2. TruLens and Langfuse trace and judge the same runs, and the Langfuse export copies its scores into Snowflake.
3. It runs the leaderboard (`sql/leaderboard.sql`) on the **interactive warehouse** `REFUND_LAB_IWH`, reports which warehouse served it, and suspends the warehouse again.

All three write to and read from **standard Snowflake tables**; nothing is copied into a separate store.

`.refund_lab.env` is gitignored. The code checks that it's connected to `REFUND_LAB_ACCOUNT` before writing anything. SQL scripts that need account values take `snow sql -D` variables, listed in each file's header.

## How it fits together

- **Agents** (`src/refund_agent/agents.py`): `fetch_facts → decide → refund_tool → respond`. The models are called through `langchain-snowflake`'s `ChatSnowflake`. Each call's tokens, latency and estimated AI credits are recorded.
- **Data and simulation** (`src/refund_agent/lab.py`): random orders (about 80% are 30 days old or less, about 10% have pending payments, about 8% have a declining processor, about 20% of requests are partial); `SCENARIOS`, a designed set of 10 requests that triggers every flaw; or `eval_set`, 100 requests per agent across the same scenario kinds, for measuring judge alignment.
- **Sinks** (`lab.py`): `StreamingSink` writes with Snowpipe Streaming (high-performance architecture, Python SDK) as the key-pair service user `REFUND_LAB_STREAMER`; `RestStreamingSink` uses the Snowpipe Streaming REST API with a container's own session token (the app); `SqlSink` uses ordinary inserts.
- **Business truth** (`sql/business_truth.sql`): the `RUN_EVIDENCE` view computes `BUSINESS_CORRECT`, `FALSE_SUCCESS`, `EXPECTED_EFFECT_CENTS`, `MONEY_AT_RISK_CENTS`, `CUSTOMER_MISINFORMED` and `ROOT_CAUSE` (`stale_data`, `tool`, `reply` or `model_error`).
- **Observers** (`src/refund_agent/observers.py`):
  - **TruLens** writes to Snowflake AI Observability, which computes `answer_relevance` and `groundedness` server-side. Scores are read through the `TRULENS_SCORES` view.
  - **Langfuse** is self-hosted on SPCS (see `langfuse_spcs/README.md`) and receives the same runs through LangChain's `CallbackHandler`. Its native LLM-as-a-judge evaluators score `answer_relevance` and `groundedness` on a `judge_input` step that carries the same facts TruLens sees. `LangfuseObserver.export` copies traces, Langfuse's cost per call and scores into `LANGFUSE_TRACES` and `LANGFUSE_SCORES`.
  - **Judges and cost:** both tools judge with `openai-gpt-5.4-mini` on Cortex, a different model family from the Claude agents. Langfuse reaches Cortex through its OpenAI-compatible REST API with a scoped short-lived token (`langfuse_spcs/judge_access.sql`, `scripts/rotate_judge_token.sh`, `scripts/langfuse_native_evals.py`). The token lasts 7 days; rerun `bash scripts/rotate_judge_token.sh` with Langfuse resumed to replace it in Snowflake and in Langfuse's Cortex connection. Langfuse computes cost from Cortex credit rates registered by `scripts/langfuse_models.py`, which take precedence over Langfuse's built-in USD prices for the same model names; TruLens records a cost span for each model call.
- **Leaderboard** (`sql/leaderboard.sql`): runs on the interactive warehouse `REFUND_LAB_IWH` straight against the standard tables (no copies), with `REFUND_LAB_WAREHOUSE` as the fallback for statements over 5 seconds. The TruLens scores are joined separately on the standard warehouse, because they come from the AI Observability event table.
- **Judge agreement** (`sql/judge_agreement.sql`): the `JUDGE_AGREEMENT` view treats each judge as a classifier: a score of 0.5 or more predicts "business-correct", and `BUSINESS_CORRECT` is the label. It reports the confusion counts, precision (of the runs a judge passed, how many were correct), recall, F1, agreement, Cohen's kappa and the false-pass rate, with 95% intervals, plus the score correlations Langfuse Score Analytics shows. `business_correct` is also copied into both tools as a reference score, so you can compare it there too (the TruLens metric needs `trulens-feedback` and pandas below 3).
- **App** (`refund_truth_sis/`): Streamlit in Snowflake on container runtime, with three sections: Pick a winner (business truth hidden until revealed), Refund requests and Investigate. It streams with `RestStreamingSink` as its owner role (a container can't sign in with an outside key pair). A sidebar switch resumes `REFUND_LAB_IWH` to serve the leaderboard, with a Suspend button next to the caption that says which warehouse served it.
- **Notebook** (`notebooks/refund_agent_lab.ipynb`): the same steps as the guide, plus a TruLens vs Langfuse comparison and cleanup.

## Example results

Your numbers will differ slightly from run to run, because the models aren't deterministic.

### Scenario set (9 orders, 10 requests, 30 runs)

The scenarios cover eligible full refunds, an eligible partial refund, an order exactly at the 14-day window, two orders inside only the old 30-day window, an order outside both windows, a pending payment, a card the processor declines, and a repeat request for an already refunded order.

| Agent | Business-correct | Microcredits / request | What went wrong |
| --- | --- | --- | --- |
| Fixed | 10 of 10 | 1,503 | |
| Baseline | 8 of 10 | 1,781 | said "refunded" on the declined card (reply template v1); refunded the whole order on the partial request (tool v1) |
| Lean | 7 of 10 | 98 | refunded both old-window orders (stale policy); refunded the whole order on the partial request (tool v1) |

Thirty runs are enough to see each flaw, but too few to rank judges: Langfuse groundedness had a kappa of 0.29 with a 95% interval from -0.35 to 0.94.

### Eval set (88 orders, 100 requests per agent, 300 runs)

`run_simulation.py --eval` sends the same scenario kinds, varied within each kind, to every agent. Business truth: Fixed 100 of 100 correct, Baseline 76 (12 false replies on declined cards, 12 whole-order refunds), Lean 70 (18 stale-policy refunds, 12 whole-order refunds). Each judge is scored as a classifier against business truth. The question that matters is how many of the 54 wrong runs each judge caught:

| Judge | Wrong runs caught | Wrong runs passed | Kappa |
| --- | --- | --- | --- |
| Langfuse groundedness | 14 of 54 (26%) | 40 | 0.36 [0.17, 0.54] |
| TruLens groundedness | 14 of 54 (26%) | 40 | 0.26 [0.08, 0.44] |
| TruLens answer relevance | 15 of 54 (28%) | 39 | 0.20 [0.02, 0.37] |
| Langfuse answer relevance | 1 of 54 (2%) | 53 | -0.04 [-0.26, 0.19] |

Precision flatters every judge here: 246 of 300 runs are correct, so a judge that passes everything scores 0.82. All four land between 0.82 and 0.86.

| Judge | Precision | Recall | F1 |
| --- | --- | --- | --- |
| Langfuse groundedness | 0.86 [0.82, 0.90] | 1.00 | 0.92 |
| TruLens groundedness | 0.85 [0.81, 0.89] | 0.95 | 0.90 |
| TruLens answer relevance | 0.85 [0.80, 0.89] | 0.90 | 0.87 |
| Langfuse answer relevance | 0.82 [0.77, 0.86] | 0.95 | 0.88 |

| Judge | False replies passed | Stale refunds passed | Whole-order refunds passed |
| --- | --- | --- | --- |
| TruLens groundedness | 0 of 12 | 18 of 18 | 22 of 24 |
| Langfuse groundedness | 0 of 12 | 18 of 18 | 22 of 24 |

#### Larger judges

The eval set was rerun with larger judges, set with `REFUND_LAB_JUDGE_MODEL` (default `openai-gpt-5.4-mini`). Each rerun had the same 246 correct and 54 wrong runs:

| Groundedness judge | Tool | Wrong runs caught | Stale-policy refunds passed | Kappa |
| --- | --- | --- | --- | --- |
| `openai-gpt-5.4-mini` | TruLens | 14 of 54 | 18 of 18 | 0.26 [0.08, 0.44] |
| `openai-gpt-5.4` | TruLens | 16 of 54 | 18 of 18 | 0.35 [0.17, 0.52] |
| `openai-gpt-6-astra` | TruLens | 16 of 54 | 18 of 18 | 0.32 [0.14, 0.50] |
| `openai-gpt-5.4-mini` | Langfuse | 14 of 54 | 18 of 18 | 0.36 [0.17, 0.54] |
| `openai-gpt-5.4` | Langfuse | 15 of 54 | 18 of 18 | 0.38 [0.20, 0.56] |

The intervals overlap, and every judge passes all 18 stale-policy refunds. The mini judge performs about as well at a fraction of the cost per call. Langfuse can't use `openai-gpt-6-astra`: Cortex rejects tool calls for it on `/v1/chat/completions`, which Langfuse's evaluators need. To compare a judge in Langfuse, switch it with `REFUND_LAB_JUDGE_MODEL` set, rerun `langfuse_llm_connection.py` and `langfuse_native_evals.py` (and `langfuse_models.py` for its price), then rerun both scripts without it to switch back.

### What each signal picks

On the eval set, every signal except business truth ranks a broken agent above Fixed:

- **Cost:** Lean, at about a fifteenth of the others' cost per request.
- **Self-reported success:** Lean (61%) and Baseline (62%) above Fixed (50%), which is the only agent that's always right.
- **Latency:** barely separates the agents; Lean's smaller model was slower per call here (about 1.8 to 2.1 s against 1.5 s).
- **Groundedness:** catches every reply that contradicts the facts, such as Baseline's false "refunded". It passes nearly every action that was wrong but described accurately: a refund under an out-of-date policy, or the wrong amount refunded. About 1 in 7 runs it passes is wrong, and most of those are Lean's, so Lean, the cheapest agent, also gets past the judges.
- **Answer relevance:** close to chance in both tools.
- **Business truth:** only it picks Fixed and shows the money at risk.

The 10-request scenario set is for seeing each flaw, not for comparing rates: one request moves a rate by 10 points.

Cost is estimated from token counts at the published Cortex rates (Snowflake Service Consumption Table, AI_COMPLETE rows).

## Running costs

- Langfuse runs on a compute pool. Resume it before a live run with `CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('resume','http://localhost:3000')`; an idle task suspends it after 30 minutes without traffic.
- The interactive warehouse can't auto-suspend in under 24 hours, and each resume starts a new one-hour minimum charge. `scripts/run_simulation.py` and the notebook suspend it when they finish. The app resumes it only when you turn on its interactive warehouse switch; use the app's Suspend button, or `ALTER WAREHOUSE REFUND_LAB_IWH SUSPEND`.
- The agents' Cortex calls are small: about 0.03 credits for the 30-run scenario set and 0.33 credits for the 300-run eval set in the verification runs, before judge calls.
- The guide's Clean Up section drops everything the lab creates.

## Deploying the app

```bash
bash scripts/sync_app_package.sh
snow streamlit deploy refund_truth_sis --connection "$REFUND_LAB_CONNECTION" \
  --project "$PWD/refund_truth_sis" --replace --prune --env warehouse="$REFUND_LAB_WAREHOUSE"
```

Run `sql/app_access.sql` first; it creates the Langfuse egress and secrets the app needs. Never grant the lab's objects to `PUBLIC`.

## License

Apache 2.0. See [LICENSE](LICENSE).
