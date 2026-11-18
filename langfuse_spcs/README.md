# Self-hosting Langfuse on Snowpark Container Services

This guide sets up open-source Langfuse (v4) inside Snowflake on Snowpark Container Services (SPCS). Commands use the `REFUND_LAB_*` environment variables described in `src/refund_agent/config.py`.

Langfuse's documentation covers Docker Compose, Kubernetes and the major clouds. SPCS isn't covered, so the steps below adapt the official [`docker-compose.yml`](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml) and note each difference.

The finished setup has:
- one SPCS service with six containers that talk to each other over `localhost`: Langfuse web, Langfuse worker, Postgres, Redis, ClickHouse and MinIO;
- a public HTTPS endpoint, where you sign in to Snowflake and then to Langfuse;
- traces stored in ClickHouse and Postgres on persistent block volumes.

`pip install langfuse` installs only the client SDK. The server is the six containers above, and SDK v5 needs a v4 server.

## Prerequisites

- **Snowflake role:** ACCOUNTADMIN, or a role that can create roles, compute pools and grants.
- **Docker:** Docker Desktop. On Apple Silicon the images are pulled as `linux/amd64`, the architecture SPCS runs.
- **Snowflake CLI:** v3.x, with a connection named in `REFUND_LAB_CONNECTION`.
- **Python 3.11:** the lab's virtual environment, `.venv` (see the top-level README). It already has `snowflake-connector-python`, `langfuse` and `requests`, so every command below uses `.venv/bin/python`.

## Files

| File | What it does |
| --- | --- |
| `setup.sql` | Creates the role, schema, compute pool and grants |
| `mirror_images.sh` | Pulls the six images and pushes them to the Snowflake image repository |
| `create_secrets.py` | Generates credentials and stores them as Snowflake secrets |
| `build_spec.py` | Writes `langfuse.yaml`, the service specification template |
| `deploy.sql` | Creates the specification stage and the `MANAGE_LANGFUSE` deploy procedure |
| `auto_suspend.sql` | Creates the idle safeguard: a task that suspends Langfuse after 30 minutes without use |
| `verify.py` | Runs the end-to-end check: health, API keys, then sends a trace and reads it back |

## Step 1: Create the role, schema and compute pool

```bash
snow sql --connection "$REFUND_LAB_CONNECTION" -f langfuse_spcs/setup.sql \
  -D admin_user=<your user> -D warehouse="$REFUND_LAB_WAREHOUSE"
```

This script:
- creates `LANGFUSE_ROLE` and grants it to your user;
- creates the schema `REFUND_AGENT_LAB.LANGFUSE` and makes `LANGFUSE_ROLE` its owner;
- creates the compute pool `LANGFUSE_POOL`: `CPU_X64_M`, one node, auto-suspend after one hour;
- grants the pool, `BIND SERVICE ENDPOINT` (needed for a public endpoint) and warehouse usage to `LANGFUSE_ROLE`.

**ACCOUNTADMIN can't own a service**, so the service is owned by `LANGFUSE_ROLE`.

Grant the custom role to SYSADMIN as well. ACCOUNTADMIN then inherits it, which it needs to replace objects owned by `LANGFUSE_ROLE` and to reach the endpoint:

```sql
GRANT ROLE LANGFUSE_ROLE TO ROLE SYSADMIN;
```

If your connection can switch roles, run the last statement of `setup.sql` (`CREATE IMAGE REPOSITORY`) as `LANGFUSE_ROLE`. If its sessions are restricted to one role (`USE ROLE` fails), as with a role-restricted programmatic access token, create the repository as ACCOUNTADMIN and grant read access instead:

```sql
GRANT CREATE IMAGE REPOSITORY, CREATE SECRET, CREATE PROCEDURE, CREATE STAGE
  ON SCHEMA REFUND_AGENT_LAB.LANGFUSE TO ROLE ACCOUNTADMIN;
CREATE IMAGE REPOSITORY IF NOT EXISTS REFUND_AGENT_LAB.LANGFUSE.IMAGES;
GRANT READ ON IMAGE REPOSITORY REFUND_AGENT_LAB.LANGFUSE.IMAGES TO ROLE LANGFUSE_ROLE;
```

Check that the pool reaches `IDLE` or `ACTIVE`:

```sql
DESCRIBE COMPUTE POOL LANGFUSE_POOL;
```

## Step 2: Mirror the images into Snowflake

SPCS can only pull images from a Snowflake image repository. Get its URL:

```sql
SHOW IMAGE REPOSITORIES IN SCHEMA REFUND_AGENT_LAB.LANGFUSE;
```

The script derives the registry host from `REFUND_LAB_ACCOUNT` (lowercase, underscores as hyphens), so no edits are needed. Run it:

```bash
bash langfuse_spcs/mirror_images.sh
```

The script logs in with `snow spcs image-registry login`, then pulls, tags and pushes each image. The versions match Langfuse's `docker-compose.yml`:

| Source | Repository tag |
| --- | --- |
| `docker.langfuse.com/langfuse/langfuse:4` | `langfuse-web:4` |
| `docker.langfuse.com/langfuse/langfuse-worker:4` | `langfuse-worker:4` |
| `clickhouse/clickhouse-server:26.3.62.3` | `clickhouse:26.3.62.3` |
| `postgres:17` | `postgres:17` |
| `redis:7` | `redis:7` |
| `cgr.dev/chainguard/minio:latest` | `minio:latest` |

## Step 3: Create the secrets

```bash
.venv/bin/python langfuse_spcs/create_secrets.py
```

This generates 11 random values and stores each as a `GENERIC_STRING` secret named `REFUND_AGENT_LAB.LANGFUSE.LF_*`. It grants `READ` on them to `LANGFUSE_ROLE`.

The values are never printed. A copy goes to `~/.langfuse_spcs.env` (mode 600) and holds:
- `LF_INIT_PUBLIC_KEY` and `LF_INIT_SECRET_KEY`: the project's API keys for the SDK;
- `LF_INIT_USER_PASSWORD`: the Langfuse UI login.

The script refuses to overwrite an existing file.

**The UI password must contain letters, digits and a special character.** Langfuse rejects anything weaker. The generator in `create_secrets.py` only sometimes includes a symbol (`-` or `_`), so append one or set a stronger value before the first deploy:

```sql
ALTER SECRET REFUND_AGENT_LAB.LANGFUSE.LF_INIT_USER_PASSWORD SET SECRET_STRING = '<strong password>';
```

Then update `~/.langfuse_spcs.env` to match.

## Step 4: Build the service specification

```bash
.venv/bin/python langfuse_spcs/build_spec.py
```

This writes `langfuse.yaml`. It's JSON, which is valid YAML, so it avoids YAML anchors, which SPCS may not support. It differs from docker-compose in these ways:

| Change | Why |
| --- | --- |
| All hosts are `localhost` | Containers in one service share a network namespace |
| MinIO moves to ports 9002 and 9003 | ClickHouse already uses 9000 |
| Postgres `PGDATA` is `/var/lib/postgresql/data/pgdata` | A new block volume contains `lost+found`, which stops `initdb` from using the volume root |
| ClickHouse data (20Gi) and Postgres data (10Gi) use block volumes | These hold traces and accounts, so they must survive restarts |
| MinIO and ClickHouse logs use local volumes | The MinIO image runs as a non-root user and can't take ownership of a root-owned block volume. MinIO files are lost on restart; traces are not |
| The AI gateway is left out | Not needed for tracing |
| `NEXTAUTH_URL: {{ nextauth_url }}` is not quoted | It's a template variable, and `USING` passes the value as a JSON string |

Passwords and keys come from the Snowflake secrets through each container's `secrets:` entries; none are written into the spec. Langfuse's startup settings (`LANGFUSE_INIT_*`) create the organization `refund-lab`, the project `refund-agent-lab` and the admin user on first start.

## Step 5: Create the stage and deploy procedure

```bash
snow sql --connection "$REFUND_LAB_CONNECTION" -f langfuse_spcs/deploy.sql
snow sql --connection "$REFUND_LAB_CONNECTION" -q "PUT file://$PWD/langfuse_spcs/langfuse.yaml @REFUND_AGENT_LAB.LANGFUSE.SPECS AUTO_COMPRESS=FALSE OVERWRITE=TRUE"
```

`deploy.sql` creates the stage `SPECS` and the procedure `MANAGE_LANGFUSE(ACTION, NEXTAUTH_URL)`. The procedure uses owner's rights and is owned by `LANGFUSE_ROLE`, so its service changes run as that role even when called from ACCOUNTADMIN.

It accepts only these actions, and a URL that passes a strict `https://` pattern check:

| Action | What it does |
| --- | --- |
| `create` | Creates the service |
| `upgrade` | Applies the current spec from the stage. Block volumes are kept |
| `suspend` | Suspends the service |
| `resume` | Resumes the service |
| `grant_admin` | Lets ACCOUNTADMIN use the endpoint |
| `endpoint` | Returns the ingress URL |
| `idle_check` | Suspends the service if it has had no ingress for 30 minutes (see Step 9) |

## Step 6: Create the service and set its public URL

The URL is only known after the service exists, so create it with a placeholder first:

```sql
CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('create', 'http://localhost:3000');
```

Wait until every container is `READY`. The first start takes about two minutes while Langfuse runs its Postgres and ClickHouse migrations:

```sql
SHOW SERVICE CONTAINERS IN SERVICE REFUND_AGENT_LAB.LANGFUSE.LANGFUSE;
```

Get the URL, then apply it as `NEXTAUTH_URL`. Langfuse uses it for sign-in redirects:

```sql
CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('endpoint', 'http://localhost:3000');
-- returns e.g. abcd1234-myorg-myaccount.snowflakecomputing.app
CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('upgrade', 'https://<ingress_url>');
CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('grant_admin', 'http://localhost:3000');
```

To troubleshoot, read a container's log:

```sql
SELECT SYSTEM$GET_SERVICE_LOGS('REFUND_AGENT_LAB.LANGFUSE.LANGFUSE', 0, 'langfuse-web', 100);
```

## Step 7: Verify from a laptop

Set `REFUND_LAB_LANGFUSE_HOST` to `https://<ingress_url>`, then run:

```bash
.venv/bin/python langfuse_spcs/verify.py
```

Expected output:

```text
1 health: 200 {"status":"OK","version":"4.76.0"}
2 projects with Langfuse keys: 200 ['Refund Agent Lab']
3 sent trace: spcs-smoke-xxxxxxxx <trace id>
4 read back: ['spcs-smoke-xxxxxxxx'] after 5s
```

Each request carries two credentials:
- **Snowflake session token** in `X-SF-SPCS-Authorization: Snowflake Token="..."`. The ingress proxy needs it and strips it before forwarding. It must be a token issued for this purpose (from SPCS Tutorial 8):

  ```python
  token = conn._rest._token_request("ISSUE")["data"]["sessionToken"]
  ```

  The connector's own `conn.rest.token` gets a 500 FAULT from the proxy.
- **Langfuse API keys** as `Authorization: Basic <public_key:secret_key>`, which reaches Langfuse unchanged.

With the SDK:

```python
Langfuse(public_key=..., secret_key=..., base_url=HOST, additional_headers={"X-SF-SPCS-Authorization": ...})
```

Langfuse v4 runs in events-only mode. The old `/api/public/traces/{id}` and `/api/public/observations` endpoints return 404, so read data back through `/api/public/v2/observations?traceId=...`. SDK v5 sends traces through OpenTelemetry.

## Step 8: Sign in to the UI

1. Open `https://<ingress_url>` and sign in to Snowflake. The role you sign in with needs `ALL_ENDPOINTS_USAGE` on the service, or must inherit `LANGFUSE_ROLE`.
2. On the Langfuse page, choose **Sign in**, not Sign up. Use the `REFUND_LAB_ADMIN_EMAIL` address (set before running `build_spec.py`) and `LF_INIT_USER_PASSWORD`.
3. Open the **Refund Agent Lab** project. The verify traces appear under Home and Tracing.

## Step 9: Turn on the idle safeguard

A service with a public endpoint never suspends on its own. To avoid paying for an unused service, create a task that suspends it after 30 minutes without use:

```bash
snow sql --connection "$REFUND_LAB_CONNECTION" -f langfuse_spcs/auto_suspend.sql
```

**How it decides the service is idle:**
- The spec turns on the `network` platform metrics group (`platformMonitor`). SPCS then records ingress connections to the endpoint (`network.ingress.connections.active` and `network.ingress.cps`) in the account event table. These count both browser sessions and SDK or API calls.
- The serverless task `LANGFUSE_IDLE_SUSPEND` runs every 10 minutes and calls `MANAGE_LANGFUSE('idle_check', ...)`.
- That reads the metrics with `LANGFUSE!SPCS_GET_METRICS`, so it doesn't need access to the whole event table. It suspends the service only when all of these are true:
  1. the service is `RUNNING`;
  2. it was resumed or upgraded more than 30 minutes ago, so a fresh start isn't treated as idle;
  3. there were no ingress connections in the last 30 minutes.
- Because the task runs every 10 minutes, Langfuse suspends 30–40 minutes after its last request.

Check what the task did:

```sql
SELECT state, scheduled_time, error_message
FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.TASK_HISTORY(TASK_NAME => 'LANGFUSE_IDLE_SUSPEND', RESULT_LIMIT => 100))
WHERE database_name = 'REFUND_AGENT_LAB' ORDER BY scheduled_time DESC;

-- run the check by hand to see its decision
CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('idle_check', 'http://localhost:3000');
```

It never resumes Langfuse. When you come back, resume it and wait about 2 minutes for all containers to be `READY`:

```sql
CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('resume', 'http://localhost:3000');
```

Turn the safeguard off or back on with `ALTER TASK REFUND_AGENT_LAB.LANGFUSE.LANGFUSE_IDLE_SUSPEND SUSPEND` or `RESUME`.

## Resetting the UI password

Langfuse creates the startup user only if it doesn't exist, so changing the secret alone does nothing. SPCS has no shell into a running container, so a temporary helper container deletes the user:

```bash
.venv/bin/python langfuse_spcs/build_spec.py --reset-user <email>   # adds a one-off psql container
# PUT langfuse.yaml to the stage, then: CALL ...MANAGE_LANGFUSE('upgrade', 'https://<ingress_url>')
# wait for the reset-user log to show "DELETE 1":
#   SELECT SYSTEM$GET_SERVICE_LOGS('REFUND_AGENT_LAB.LANGFUSE.LANGFUSE', 0, 'reset-user', 20);
.venv/bin/python langfuse_spcs/build_spec.py                         # normal spec without the helper
# PUT again, then upgrade again
```

On that restart Langfuse recreates the user from the current secret, with access to the project.

## Cost and cleanup

- **Running cost:** the pool bills while the service runs. A service with a public endpoint can't auto-suspend, so the idle safeguard (Step 9) does it after 30 minutes without use. You can also suspend it by hand:

  ```sql
  CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('suspend', 'http://localhost:3000');
  CALL REFUND_AGENT_LAB.LANGFUSE.MANAGE_LANGFUSE('resume',  'http://localhost:3000');
  ```

- **While suspended:** block volume storage is still billed.
- **Full teardown:** suspend or drop the task `LANGFUSE_IDLE_SUSPEND`, then drop the service as `LANGFUSE_ROLE` (add a `drop` action to the procedure, because ACCOUNTADMIN doesn't own it). Then drop `LANGFUSE_POOL`, the image repository, the secrets and the schema.

## Not yet verified

- Sending traces from inside Snowflake (Streamlit or a notebook), which needs a network rule and external access integration or an internal service call.
- Langfuse's LLM-as-judge evaluators.
- Whether data survives a full suspend and resume.
