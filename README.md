# Refund agent business-truth lab

Three LangGraph refund agents answer the same synthetic refund requests. Each looks good on at least one usual signal: cost, latency, its own reported success, or an LLM judge. Only Snowflake, which joins each run to orders, policies, payments and the ledger, shows which agent actually did the right thing with the money. Nothing touches a real payment system.

**Lean and Baseline are broken on purpose.** Each has a planted flaw (a stale cached policy, a prompt that always reassures, a buggy refund tool) standing in for a mistake a real team could ship. Fixed is the control. The lab tests whether each signal can spot these flaws; it isn't a comparison of the Mistral models. The step-by-step sfguide is `build-an-observability-ai-agent-on-snowflake`.

| Agent | Model (Cortex) | Policy | Reply prompt | Refund tool | Seeded root cause |
| --- | --- | --- | --- | --- | --- |
| Lean | `ministral-3-8b` | cached 30-day snapshot | short | v1 | stale data, tool |
| Baseline | `mistral-large3` | live | "always tell them the refund is complete" | v1 | prompt, tool |
| Fixed | `mistral-large3` | live | "report exactly what the tool returned" | v2 | none |

The refund window shrank from 30 to 14 days on 2026-11-07 (`P_2026` to `P_2026_14D`). Tool v1 refunds the whole order on a partial request and ignores unsettled payments; tool v2 doesn't.

## How it fits together

- **Agents** (`src/refund_agent/agents.py`): `fetch_facts → decide → refund_tool → respond`. The models are called through `langchain-snowflake`'s `ChatSnowflake`. Each call's tokens, latency and estimated AI credits are recorded.
- **Data and simulation** (`src/refund_agent/lab.py`): realistic orders. About 80% are 30 days old or less, about 10% have pending payments, about 8% have a declining processor, and about 20% of requests are partial. Requests are fewer than orders.
- **Sinks** (`lab.py`):
  - `StreamingSink` uses Snowpipe Streaming (high-performance architecture) with the key-pair service user `REFUND_LAB_STREAMER`.
  - `SqlSink` uses ordinary inserts.
- **Oracle** (`sql/business_truth.sql`): the `RUN_EVIDENCE` view computes:
  - `BUSINESS_CORRECT`, `FALSE_SUCCESS` and `EXPECTED_EFFECT_CENTS`;
  - `MONEY_AT_RISK_CENTS` and `CUSTOMER_MISINFORMED`;
  - `ROOT_CAUSE`: `stale_data`, `tool`, `prompt` or `model_error`.
- **Observers** (`src/refund_agent/observers.py`):
  - **TruLens** writes to Snowflake AI Observability (External Agent `REFUND_AGENT_LAB.DEMO.REFUND_AGENT_LAB`), and Snowflake computes `answer_relevance` and `groundedness` server-side. Scores are read through the `TRULENS_SCORES` view.
  - **Langfuse** is self-hosted on SPCS (see `langfuse_spcs/README.md`) and receives the same runs through LangChain's `CallbackHandler`. Its native LLM-as-a-judge evaluators score `answer_relevance` and `groundedness` inside Langfuse, on a `judge_input` step that carries the same facts TruLens sees. `LangfuseObserver.export` loads traces, Langfuse's own cost per call, and scores into `LANGFUSE_TRACES` and `LANGFUSE_SCORES`.
  - **Native judges and cost (2026-11-08):** both tools judge with `openai-gpt-5-mini`. Langfuse reaches Cortex through its OpenAI-compatible REST API, using a scoped 7-day token (`langfuse_spcs/judge_access.sql`, `scripts/langfuse_judge_setup.py`, `langfuse_llm_connection.py`, `langfuse_native_evals.py`). Langfuse computes cost from Cortex prices registered by `scripts/langfuse_models.py`; TruLens records cost spans for each model call. Cost and latency from Langfuse, TruLens and the lab agree.
- **Leaderboard** (`sql/leaderboard.sql`): runs on the interactive warehouse `REFUND_LAB_IWH` (XSMALL, falling back to `REFUND_LAB_WAREHOUSE`).
- **Judge agreement** (`sql/judge_agreement.sql`): the `JUDGE_AGREEMENT` view measures how well each tool's judge scores agree with `BUSINESS_CORRECT`. It uses the same statistics as Langfuse Score Analytics:
  - **Raw score against truth as 0/1:** Pearson and Spearman correlation, MAE and RMSE.
  - **Score at or above 0.5 counted as a pass:** a 2×2 confusion matrix, overall agreement, Cohen's kappa and F1.
  - **Business truth in each tool:** `business_correct` comes only from `RUN_EVIDENCE` and is copied into both tools as a reference label.
    - **Langfuse:** the export posts `business_correct` (yes/no, and as a 0/1 number) and `<metric>_pass` scores on each run's `judge_input` step, so Score Analytics can compare them with Langfuse's own judges.
    - **TruLens:** a client-side `business_correct` metric reads `RUN_EVIDENCE`. It needs `trulens-feedback` and pandas below 3.
- **App** (`refund_truth_sis/`): `REFUND_AGENT_LAB.DEMO.REFUND_TRUTH_LAB`, Streamlit in Snowflake on container runtime. It has three sections: Pick a winner (business truth hidden until revealed), Refund requests and Investigate.
- **Notebook** (`notebooks/refund_agent_lab_v2.ipynb`): the six steps, plus a TruLens vs Langfuse comparison and cleanup.

## Fixed scenario set (simulation `APP_40D8CEC790E84A78`: 9 orders, 10 requests, 30 runs)

Random orders can leave flaws untested: in the 45-run random simulation below, only 1 of 15 requests was refundable, and the partial-refund tool flaw never occurred. `lab.SCENARIOS` is a designed set that triggers every case, and it's now the app's default (`--scenarios` in `scripts/run_simulation.py`):
- eligible full refunds (5 and 9 days old);
- an eligible partial refund (40% asked);
- an order exactly at the 14-day window;
- two orders inside only the old 30-day window (20 and 27 days old);
- an order outside both windows (45 days old);
- a pending payment;
- a card the processor declines;
- a repeat request for an order already refunded.

| Agent | Business-correct | What went wrong |
| --- | --- | --- |
| Fixed | 10 of 10 | |
| Lean | 7 of 10 | refunded both old-window orders (stale policy); refunded the whole order on the partial request (tool v1) and told the customer "already refunded" |
| Baseline | 4 of 10 | said "refunded" on 5 declines (prompt); refunded the whole order on the partial request (tool v1) |

Baseline answered the 27-day request honestly this time, so its prompt flaw is model-dependent. Judge agreement (`JUDGE_AGREEMENT`):

| Judge | Kappa | False replies passed | Stale refunds passed | Whole-order refunds passed |
| --- | --- | --- | --- | --- |
| TruLens groundedness | 0.49 | 0 of 5 | 2 of 2 | 2 of 2 |
| Langfuse groundedness | 0.74 | 0 of 5 | 2 of 2 | 1 of 2 |
| TruLens answer relevance | 0.37 | | | |
| Langfuse answer relevance | 0.00 | | | |

Groundedness catches replies that contradict the facts. It doesn't catch actions that were wrong but described accurately: a refund under an out-of-date policy, or the wrong amount refunded. Only business truth catches those.

## Judge agreement with business truth (simulation `APP_CAC541CE9D894D2E`: Mistral agents, 20 orders, 15 requests, 45 runs)

Both tools judged natively with `openai-gpt-5-mini`. Business truth: Fixed 15 of 15 correct, Lean 8 of 15 (7 stale-policy refunds), Baseline 1 of 15 (14 false replies). From `JUDGE_AGREEMENT`:

| Judge | Pearson | Kappa | F1 | Wrong runs passed |
| --- | --- | --- | --- | --- |
| TruLens groundedness | 0.70 | 0.60 | 0.82 | 6 of 21 |
| Langfuse groundedness | 0.82 | 0.64 | 0.85 | 7 of 21 |
| TruLens answer relevance | 0.20 | 0.12 | 0.68 | 15 of 21 |
| Langfuse answer relevance | 0.07 | 0.05 | 0.71 | 20 of 21 |

- **Groundedness:** in both tools it catches all 14 of Baseline's false replies. It passes almost every stale-policy refund (TruLens 6 of 7, Langfuse 7 of 7), because the reply matches what the agent did.
- **Answer relevance:** in both tools it is close to chance (kappa about 0.1 or below).
- **Business truth:** only it flags Lean's refunds.

## Verified result (simulation `APP_2B2265DE2DCC46CC`: 20 orders, 15 requests, 45 runs)

This run used the earlier Llama models (`llama3.1-8b` for Lean, `llama3.3-70b` for Baseline and Fixed). The agents moved to Mistral on 2026-11-08: a six-case live check passed with the same seeded behaviour, at about 40 microcredits per request for Lean and about 170 for the other two. Its judges were `llama3.1-70b` (TruLens) and `llama3.3-70b` (a client-side Langfuse judge scoring relevance and helpfulness); both tools now use their native judges on `openai-gpt-5-mini`. In a 9-run check (`APP_7CAADF8C1CC449AF`), groundedness averaged 0.94 (TruLens) and 1.0 (Langfuse) on correct runs and 0.17 on wrong runs for both.

Writes went through Snowpipe Streaming. Langfuse had 45 of 45 traces, and TruLens scored 15 runs per agent. The leaderboard ran on `REFUND_LAB_IWH`.

| Agent | Microcredits / request | p50 latency | Self-reported success | TruLens relevance / groundedness | Langfuse relevance / helpfulness | Business-correct | Money at risk | Misinformed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Lean | 51 | 1.8 s | 60% | 0.67 / 0.51 | 1.00 / 0.69 | 67% | $327.50 | 0 |
| Baseline | 172 | 2.4 s | 100% | 0.67 / 0.17 | 1.00 / 0.96 | 27% | $0 | 11 |
| Fixed | 171 | 2.6 s | 27% | 0.78 / 0.76 | 1.00 / 0.63 | 100% | $0 | 0 |

**What each signal picks:**
- **Cost and latency:** Lean.
- **Self-reported success and Langfuse helpfulness:** Baseline.
- **TruLens groundedness:** catches Baseline's false replies, but not Lean's stale-policy refunds.
- **Business truth:** only it picks Fixed and shows the money at risk.

**Caveats:**
- The tool v1 partial-refund flaw didn't occur in this sample.
- Cost is estimated from token counts at the published Cortex rates (Service Consumption Table 6(c)).

## Run it

```bash
.venv/bin/python -m unittest discover -s tests -t .           # no Snowflake calls
.venv/bin/python scripts/run_simulation.py --scenarios         # live: fixed scenario set, both observers
```

Before a live run, resume Langfuse with `CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('resume','http://localhost:3000')`. An idle task suspends it after 30 minutes without traffic. The interactive warehouse can't auto-suspend in under 24 hours, so suspend it after each session with `ALTER WAREHOUSE REFUND_LAB_IWH SUSPEND`.

To deploy the app: run `scripts/sync_app_package.sh`, then `snow streamlit deploy refund_truth_sis --connection "$REFUND_LAB_CONNECTION" --project "$PWD/refund_truth_sis" --replace --prune --env warehouse="$REFUND_LAB_WAREHOUSE"`.

**Setup scripts in `sql/`:**
- **Data model:** `redesign_schema.sql` (policies, payment state, LLM_CALLS) and `business_truth.sql`.
- **Observers:** `observer_scores.sql`.
- **Infrastructure:** `streaming_setup.sql`, `interactive_warehouse.sql` and `app_access.sql` (Langfuse egress plus secrets for the app).

## Rules

- **Account:** copy `refund_lab.env.example` to `.refund_lab.env` (gitignored), fill in `REFUND_LAB_CONNECTION`, `REFUND_LAB_ACCOUNT` (ORG-ACCOUNT), `REFUND_LAB_WAREHOUSE`, `REFUND_LAB_LANGFUSE_HOST` and `REFUND_LAB_ADMIN_EMAIL`, and `source` it (see `src/refund_agent/config.py`). Code checks the account before writing. SQL scripts that need account values take `snow sql -D` variables, listed in each file's header.
- **Synthetic data only.** Never grant `PUBLIC`.
- **Claims:** only report streaming, scores or cost that a run actually verified.
