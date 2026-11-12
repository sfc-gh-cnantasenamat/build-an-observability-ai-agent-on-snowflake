"""Point every test at a fictitious account so no test depends on the developer's shell."""
import os

os.environ.update({
    "REFUND_LAB_CONNECTION": "test",
    "REFUND_LAB_ACCOUNT": "MYORG-MY_ACCOUNT",
    "REFUND_LAB_WAREHOUSE": "TEST_WH",
    "REFUND_LAB_LANGFUSE_HOST": "https://abc-myorg-my-account.snowflakecomputing.app",
})
