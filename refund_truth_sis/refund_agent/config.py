"""Account-specific settings, read from environment variables.

  REFUND_LAB_CONNECTION     Snowflake CLI / connector connection name   (default: "default")
  REFUND_LAB_ACCOUNT        ORG-ACCOUNT identifier writes must target    (required)
  REFUND_LAB_WAREHOUSE      standard warehouse for queries and the app   (required)
  REFUND_LAB_LANGFUSE_HOST  Langfuse ingress URL on SPCS                 (required for Langfuse)
  REFUND_LAB_ADMIN_EMAIL    Langfuse admin login (langfuse_spcs/build_spec.py only)

Copy refund_lab.env.example to .refund_lab.env, fill it in, and `source .refund_lab.env`.
The account guard refuses writes anywhere else, so a misconfigured connection can't touch
the wrong account.
"""
from __future__ import annotations

import os

CONNECTION = os.getenv("REFUND_LAB_CONNECTION", "default")
ACCOUNT = os.getenv("REFUND_LAB_ACCOUNT", "").upper()
WAREHOUSE = os.getenv("REFUND_LAB_WAREHOUSE", "").upper()
LANGFUSE_HOST = os.getenv("REFUND_LAB_LANGFUSE_HOST", "").rstrip("/")


def expected_identity() -> tuple[str, str]:
    """(organization, account) as returned by CURRENT_ORGANIZATION_NAME(), CURRENT_ACCOUNT()."""
    org, _, account = ACCOUNT.partition("-")
    if not account:
        raise ValueError("Set REFUND_LAB_ACCOUNT to your ORG-ACCOUNT identifier")
    return org, account


def account_host() -> str:
    """https://org-account.snowflakecomputing.com (underscores become hyphens for TLS)."""
    expected_identity()
    return f"https://{ACCOUNT.lower().replace('_', '-')}.snowflakecomputing.com"


def streaming_account() -> str:
    """Account identifier in the form the Snowpipe Streaming SDK expects."""
    expected_identity()
    return ACCOUNT.lower()
