"""Write langfuse.yaml: a flat SPCS spec (no YAML anchors) for self-hosted Langfuse v4.

One service, six containers sharing localhost, mirroring langfuse/docker-compose.yml.
Differences from docker-compose, forced by SPCS:
  - all hosts are localhost; MinIO moves to 9002/9003 because ClickHouse owns 9000
  - PGDATA is a subdirectory because a fresh block volume contains lost+found
  - MinIO uses container-local storage: its image runs as non-root and can't own a
    root-owned block volume. Event/media blobs are lost on restart; traces live in
    ClickHouse and Postgres on block volumes.
  - the optional AI gateway is omitted
The spec is a template: {{ nextauth_url }} is the public ingress URL.
JSON is valid YAML, so the output is written as indented JSON.
"""

import json
import os
from pathlib import Path

IMG = "/refund_agent_lab/langfuse/images"
SECRET = "REFUND_AGENT_LAB.LANGFUSE.{}"
# The Langfuse UI admin login, created on first start.
ADMIN_EMAIL = os.environ.get("REFUND_LAB_ADMIN_EMAIL") or exit("Set REFUND_LAB_ADMIN_EMAIL")


def secret(name, env):
    return {"snowflakeSecret": {"objectName": SECRET.format(name)},
            "secretKeyRef": "secret_string", "envVarName": env}


def resources(cpu_req, mem_req, cpu_lim, mem_lim):
    return {"requests": {"cpu": cpu_req, "memory": mem_req},
            "limits": {"cpu": cpu_lim, "memory": mem_lim}}


LANGFUSE_ENV = {
    "NEXTAUTH_URL": "{{ nextauth_url }}",
    "TELEMETRY_ENABLED": "false",
    "CLICKHOUSE_MIGRATION_URL": "clickhouse://localhost:9000",
    "CLICKHOUSE_URL": "http://localhost:8123",
    "CLICKHOUSE_USER": "clickhouse",
    "CLICKHOUSE_CLUSTER_ENABLED": "false",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
}
for kind in ("EVENT", "MEDIA"):
    LANGFUSE_ENV.update({
        f"LANGFUSE_S3_{kind}_UPLOAD_BUCKET": "langfuse",
        f"LANGFUSE_S3_{kind}_UPLOAD_REGION": "auto",
        f"LANGFUSE_S3_{kind}_UPLOAD_ACCESS_KEY_ID": "minio",
        f"LANGFUSE_S3_{kind}_UPLOAD_ENDPOINT": "http://localhost:9002",
        f"LANGFUSE_S3_{kind}_UPLOAD_FORCE_PATH_STYLE": "true",
        f"LANGFUSE_S3_{kind}_UPLOAD_PREFIX": f"{kind.lower()}s/" if kind == "EVENT" else "media/",
    })

LANGFUSE_SECRETS = [
    secret("LF_DATABASE_URL", "DATABASE_URL"),
    secret("LF_SALT", "SALT"),
    secret("LF_ENCRYPTION_KEY", "ENCRYPTION_KEY"),
    secret("LF_CLICKHOUSE_PASSWORD", "CLICKHOUSE_PASSWORD"),
    secret("LF_REDIS_AUTH", "REDIS_AUTH"),
    secret("LF_MINIO_PASSWORD", "LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY"),
    secret("LF_MINIO_PASSWORD", "LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY"),
]

spec = {"spec": {
    "containers": [
        {"name": "postgres", "image": f"{IMG}/postgres:17",
         "env": {"POSTGRES_USER": "postgres", "POSTGRES_DB": "postgres",
                 "PGDATA": "/var/lib/postgresql/data/pgdata", "TZ": "UTC", "PGTZ": "UTC"},
         "secrets": [secret("LF_POSTGRES_PASSWORD", "POSTGRES_PASSWORD")],
         "volumeMounts": [{"name": "pgdata", "mountPath": "/var/lib/postgresql/data"}],
         "resources": resources("500m", "1Gi", "1000m", "2Gi")},
        {"name": "clickhouse", "image": f"{IMG}/clickhouse:26.3.62.3",
         "env": {"CLICKHOUSE_DB": "default", "CLICKHOUSE_USER": "clickhouse"},
         "secrets": [secret("LF_CLICKHOUSE_PASSWORD", "CLICKHOUSE_PASSWORD")],
         "volumeMounts": [{"name": "chdata", "mountPath": "/var/lib/clickhouse"},
                          {"name": "chlogs", "mountPath": "/var/log/clickhouse-server"}],
         "resources": resources("1500m", "6Gi", "2500m", "10Gi")},
        {"name": "redis", "image": f"{IMG}/redis:7",
         "command": ["sh", "-c",
                     'exec redis-server --requirepass "$REDIS_AUTH" --maxmemory-policy noeviction'],
         "secrets": [secret("LF_REDIS_AUTH", "REDIS_AUTH")],
         "resources": resources("250m", "512Mi", "500m", "1Gi")},
        {"name": "minio", "image": f"{IMG}/minio:latest",
         "command": ["sh", "-c",
                     'mkdir -p /data/langfuse && exec minio server --address ":9002" '
                     '--console-address ":9003" /data'],
         "env": {"MINIO_ROOT_USER": "minio"},
         "secrets": [secret("LF_MINIO_PASSWORD", "MINIO_ROOT_PASSWORD")],
         "volumeMounts": [{"name": "miniodata", "mountPath": "/data"}],
         "resources": resources("250m", "512Mi", "500m", "1Gi")},
        {"name": "langfuse-worker", "image": f"{IMG}/langfuse-worker:4",
         "env": dict(LANGFUSE_ENV),
         "secrets": list(LANGFUSE_SECRETS),
         "resources": resources("750m", "2Gi", "1500m", "4Gi")},
        {"name": "langfuse-web", "image": f"{IMG}/langfuse-web:4",
         "env": {**LANGFUSE_ENV, "HOSTNAME": "0.0.0.0", "PORT": "3000",
                 "LANGFUSE_INIT_ORG_ID": "refund-lab",
                 "LANGFUSE_INIT_ORG_NAME": "Refund Agent Lab",
                 "LANGFUSE_INIT_PROJECT_ID": "refund-agent-lab",
                 "LANGFUSE_INIT_PROJECT_NAME": "Refund Agent Lab",
                 "LANGFUSE_INIT_USER_EMAIL": ADMIN_EMAIL,
                 "LANGFUSE_INIT_USER_NAME": "Refund Lab Admin"},
         "secrets": LANGFUSE_SECRETS + [
             secret("LF_NEXTAUTH_SECRET", "NEXTAUTH_SECRET"),
             secret("LF_INIT_PUBLIC_KEY", "LANGFUSE_INIT_PROJECT_PUBLIC_KEY"),
             secret("LF_INIT_SECRET_KEY", "LANGFUSE_INIT_PROJECT_SECRET_KEY"),
             secret("LF_INIT_USER_PASSWORD", "LANGFUSE_INIT_USER_PASSWORD"),
         ],
         "readinessProbe": {"port": 3000, "path": "/api/public/health"},
         "resources": resources("750m", "2Gi", "1500m", "4Gi")},
    ],
    "endpoints": [{"name": "langfuse", "port": 3000, "public": True}],
    "volumes": [
        {"name": "pgdata", "source": "block", "size": "10Gi"},
        {"name": "chdata", "source": "block", "size": "20Gi"},
        {"name": "chlogs", "source": "local"},
        {"name": "miniodata", "source": "local"},
    ],
    "logExporters": {"eventTableConfig": {"logLevel": "INFO"}},
    # network.ingress.* metrics drive the idle auto-suspend check (MANAGE_LANGFUSE 'idle_check').
    "platformMonitor": {"metricConfig": {"groups": ["network"]}},
}}

def add_reset_user_container(email):
    """One-off: delete the init user so langfuse-web recreates it from the secrets on restart."""
    assert "'" not in email
    sql = f"DELETE FROM users WHERE email = '{email}';"
    spec["spec"]["containers"].append({
        "name": "reset-user", "image": f"{IMG}/postgres:17",
        "command": ["sh", "-c",
                    "until pg_isready -h localhost -U postgres; do sleep 2; done; "
                    f"psql -h localhost -U postgres -d postgres -c \"{sql}\"; sleep infinity"],
        "secrets": [secret("LF_POSTGRES_PASSWORD", "PGPASSWORD")],
        "resources": resources("100m", "128Mi", "250m", "256Mi")})


if __name__ == "__main__":
    import sys
    if "--reset-user" in sys.argv:
        add_reset_user_container(sys.argv[sys.argv.index("--reset-user") + 1])
    out = Path(__file__).with_name("langfuse.yaml")
    # USING passes the URL as a JSON string, so the template itself is unquoted.
    text = json.dumps(spec, indent=2).replace('"{{ nextauth_url }}"', "{{ nextauth_url }}")
    out.write_text(text + "\n")
    print(f"wrote {out}")
