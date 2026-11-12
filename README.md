# Build an Observability AI Agent on Snowflake

Companion code for the Snowflake developer guide [Build an Observability AI Agent on Snowflake](https://github.com/sfc-gh-cnantasenamat/sfguide-build-an-observability-ai-agent-on-snowflake).

Three LangGraph refund agents answer the same synthetic refund requests. Each looks good on at least one usual signal: cost, latency, its own reported success, or an LLM judge. Only Snowflake, which joins each run to orders, policies, payments and the ledger, shows which agent actually did the right thing with the money. All data is synthetic; nothing touches a real payment system.

**Lean and Baseline are broken on purpose.** Each has a planted flaw (a stale cached policy, a prompt that always reassures, a buggy refund tool) standing in for a mistake a real team could ship. Fixed is the control. The lab tests whether each signal can spot these flaws; it isn't a comparison of the Mistral models.

| Agent | Model (Cortex) | Policy | Reply prompt | Refund tool | Planted flaw |
| --- | --- | --- | --- | --- | --- |
| Lean | `ministral-3-8b` | cached 30-day snapshot | short | v1 | stale data, tool |
| Baseline | `mistral-large3` | live | "always tell them the refund is complete" | v1 | prompt, tool |
| Fixed | `mistral-large3` | live | "report exactly what the tool returned" | v2 | none |

The refund window shrank from 30 to 14 days (policy `P_2026` to `P_2026_14D`). Tool v1 refunds the whole order on a partial request and ignores unsettled payments; tool v2 doesn't.

## Quick start

Follow the guide for the full setup, including Langfuse on Snowpark Container Services. In short:

```bash
cp refund_lab.env.example .refund_lab.env   # fill in your connection, account, warehouse, Langfuse host and admin email
source .refund_lab.env
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-observer.txt -r requirements-streaming.txt
.venv/bin/python -m unittest discover -s tests -t .      # no Snowflake calls
.venv/bin/python scripts/run_simulation.py --scenarios    # live run: fixed scenario set, both observers
```

`.refund_lab.env` is gitignored. The code checks that it's connected to `REFUND_LAB_ACCOUNT` before writing anything. SQL scripts that need account values take `snow sql -D` variables, listed in each file's header.

## How it fits together

- **Agents** (`src/refund_agent/agents.py`): `fetch_facts → decide → refund_tool → respond`. The models are called through `langchain-snowflake`'s `ChatSnowflake`. Each call's tokens, latency and estimated AI credits are recorded.
- **Data and simulation** (`src/refund_agent/lab.py`): either random orders (about 80% are 30 days old or less, about 10% have pending payments, about 8% have a declining processor, about 20% of requests are partial) or `SCENARIOS`, a designed set that triggers every flaw.
- **Sinks** (`lab.py`): `StreamingSink` writes with Snowpipe Streaming (high-performance architecture) as the key-pair service user `REFUND_LAB_STREAMER`; `SqlSink` uses ordinary inserts.
- **Business truth** (`sql/business_truth.sql`): the `RUN_EVIDENCE` view computes `BUSINESS_CORRECT`, `FALSE_SUCCESS`, `EXPECTED_EFFECT_CENTS`, `MONEY_AT_RISK_CENTS`, `CUSTOMER_MISINFORMED` and `ROOT_CAUSE` (`stale_data`, `tool`, `prompt` or `model_error`).
- **Observers** (`src/refund_agent/observers.py`):
  - **TruLens** writes to Snowflake AI Observability, which computes `answer_relevance` and `groundedness` server-side. Scores are read through the `TRULENS_SCORES` view.
  - **Langfuse** is self-hosted on SPCS (see `langfuse_spcs/README.md`) and receives the same runs through LangChain's `CallbackHandler`. Its native LLM-as-a-judge evaluators score `answer_relevance` and `groundedness` on a `judge_input` step that carries the same facts TruLens sees. `LangfuseObserver.export` copies traces, Langfuse's cost per call and scores into `LANGFUSE_TRACES` and `LANGFUSE_SCORES`.
  - **Judges and cost:** both tools judge with `openai-gpt-5-mini` on Cortex. Langfuse reaches Cortex through its OpenAI-compatible REST API with a scoped short-lived token (`langfuse_spcs/judge_access.sql`, `scripts/langfuse_judge_setup.py`, `scripts/langfuse_llm_connection.py`, `scripts/langfuse_native_evals.py`). Langfuse computes cost from Cortex prices registered by `scripts/langfuse_models.py`; TruLens records a cost span for each model call.
- **Leaderboard** (`sql/leaderboard.sql`): runs on the interactive warehouse `REFUND_LAB_IWH`, falling back to `REFUND_LAB_WAREHOUSE`.
- **Judge agreement** (`sql/judge_agreement.sql`): the `JUDGE_AGREEMENT` view measures how well each judge agrees with `BUSINESS_CORRECT`, using the same statistics as Langfuse Score Analytics: Pearson and Spearman correlation, MAE and RMSE on the raw score, and, counting a score of 0.5 or more as a pass, a confusion matrix, agreement, Cohen's kappa and F1. `business_correct` is also copied into both tools as a reference score, so you can compare it there too (the TruLens metric needs `trulens-feedback` and pandas below 3).
- **App** (`refund_truth_sis/`): Streamlit in Snowflake on container runtime, with three sections: Pick a winner (business truth hidden until revealed), Refund requests and Investigate.
- **Notebook** (`notebooks/refund_agent_lab_v2.ipynb`): the same steps as the guide, plus a TruLens vs Langfuse comparison and cleanup.

## Example results

Your numbers will differ slightly from run to run, because the models aren't deterministic.

### Scenario set (9 orders, 10 requests, 30 runs)

The scenarios cover eligible full refunds, an eligible partial refund, an order exactly at the 14-day window, two orders inside only the old 30-day window, an order outside both windows, a pending payment, a card the processor declines, and a repeat request for an already refunded order.

| Agent | Business-correct | What went wrong |
| --- | --- | --- |
| Fixed | 10 of 10 | |
| Lean | 7 of 10 | refunded both old-window orders (stale policy); refunded the whole order on the partial request (tool v1) and told the customer "already refunded" |
| Baseline | 4 of 10 | said "refunded" on 5 declines (prompt); refunded the whole order on the partial request (tool v1) |

| Judge | Kappa | False replies passed | Stale refunds passed | Whole-order refunds passed |
| --- | --- | --- | --- | --- |
| TruLens groundedness | 0.49 | 0 of 5 | 2 of 2 | 2 of 2 |
| Langfuse groundedness | 0.74 | 0 of 5 | 2 of 2 | 1 of 2 |
| TruLens answer relevance | 0.37 | | | |
| Langfuse answer relevance | 0.00 | | | |

Baseline's prompt flaw depends on the model's reply, so it doesn't fire on every request.

### Random orders (20 orders, 15 requests, 45 runs)

Business truth: Fixed 15 of 15 correct, Lean 8 of 15 (7 stale-policy refunds), Baseline 1 of 15 (14 false replies). Random orders can leave flaws untested: here the partial-refund tool flaw never occurred, which is why the scenario set exists.

| Judge | Pearson | Kappa | F1 | Wrong runs passed |
| --- | --- | --- | --- | --- |
| TruLens groundedness | 0.70 | 0.60 | 0.82 | 6 of 21 |
| Langfuse groundedness | 0.82 | 0.64 | 0.85 | 7 of 21 |
| TruLens answer relevance | 0.20 | 0.12 | 0.68 | 15 of 21 |
| Langfuse answer relevance | 0.07 | 0.05 | 0.71 | 20 of 21 |

### What each signal picks

- **Cost and latency:** Lean, the cheapest and fastest.
- **Self-reported success:** Baseline, which always says the refund worked.
- **Groundedness:** catches replies that contradict the facts, such as Baseline's false "refunded". It misses actions that were wrong but described accurately: a refund under an out-of-date policy, or the wrong amount refunded.
- **Answer relevance:** close to chance in both tools.
- **Business truth:** only it picks Fixed and shows the money at risk.

Cost is estimated from token counts at the published Cortex rates.

## Running costs

- Langfuse runs on a compute pool. Resume it before a live run with `CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('resume','http://localhost:3000')`; an idle task suspends it after 30 minutes without traffic.
- The interactive warehouse can't auto-suspend in under 24 hours, so suspend it after each session with `ALTER WAREHOUSE REFUND_LAB_IWH SUSPEND`.
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
