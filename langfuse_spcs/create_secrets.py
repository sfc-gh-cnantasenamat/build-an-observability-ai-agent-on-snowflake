"""Generate Langfuse credentials and store them as Snowflake secrets.

Values are never printed. A copy of the login and API keys is written to
~/.langfuse_spcs.env (mode 600) so the SDK and UI can be used locally.
Re-running keeps existing values (IF NOT EXISTS), so the stored file stays valid.
"""

import os
import secrets
import stat
from pathlib import Path

import snowflake.connector
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refund_agent import config

SCHEMA = "REFUND_AGENT_LAB.LANGFUSE"
ENV_FILE = Path.home() / ".langfuse_spcs.env"


def generate():
    pg = secrets.token_urlsafe(24)
    return {
        "LF_POSTGRES_PASSWORD": pg,
        "LF_DATABASE_URL": f"postgresql://postgres:{pg}@localhost:5432/postgres",
        "LF_CLICKHOUSE_PASSWORD": secrets.token_urlsafe(24),
        "LF_REDIS_AUTH": secrets.token_urlsafe(24),
        "LF_MINIO_PASSWORD": secrets.token_urlsafe(24),
        "LF_SALT": secrets.token_urlsafe(32),
        "LF_ENCRYPTION_KEY": secrets.token_hex(32),
        "LF_NEXTAUTH_SECRET": secrets.token_urlsafe(32),
        "LF_INIT_PUBLIC_KEY": f"pk-lf-{secrets.token_hex(16)}",
        "LF_INIT_SECRET_KEY": f"sk-lf-{secrets.token_hex(16)}",
        "LF_INIT_USER_PASSWORD": secrets.token_urlsafe(18),
    }


def main():
    if ENV_FILE.exists():
        raise SystemExit(f"{ENV_FILE} already exists; refusing to overwrite credentials")
    values = generate()
    con = snowflake.connector.connect(connection_name=config.CONNECTION)
    try:
        cur = con.cursor()
        cur.execute("SELECT CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT()")
        if tuple(cur.fetchone()) != config.expected_identity():
            raise SystemExit("Wrong account")
        for name, value in values.items():
            # Generated values are URL-safe base64 or hex, so they contain no quotes.
            assert "'" not in value and "\\" not in value
            cur.execute(
                f"CREATE SECRET IF NOT EXISTS {SCHEMA}.{name} "
                f"TYPE = GENERIC_STRING SECRET_STRING = '{value}'"
            )
            cur.execute(f"GRANT READ ON SECRET {SCHEMA}.{name} TO ROLE LANGFUSE_ROLE")
    finally:
        con.close()
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    os.chmod(ENV_FILE, stat.S_IRUSR | stat.S_IWUSR)
    print(f"Created {len(values)} secrets; local copy at {ENV_FILE} (mode 600)")


if __name__ == "__main__":
    main()
