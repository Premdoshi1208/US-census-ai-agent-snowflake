import os
from typing import Any, Dict, List

from dotenv import load_dotenv
import snowflake.connector
from snowflake.connector import DictCursor

load_dotenv()


DEFAULT_DATABASE = "US_OPEN_CENSUS_DATA__NEIGHBORHOOD_INSIGHTS__FREE_DATASET"
DEFAULT_SCHEMA = "PUBLIC"
DEFAULT_WAREHOUSE = "COMPUTE_WH"
DEFAULT_ROLE = "ACCOUNTADMIN"


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return value.strip()


def get_snowflake_connection():
    return snowflake.connector.connect(
        account=_required_env("SNOWFLAKE_ACCOUNT"),
        user=_required_env("SNOWFLAKE_USER"),
        password=_required_env("SNOWFLAKE_PASSWORD"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", DEFAULT_WAREHOUSE),
        role=os.getenv("SNOWFLAKE_ROLE", DEFAULT_ROLE),
    )


def _apply_session_context(cursor) -> None:
    database = os.getenv("SNOWFLAKE_DATABASE", DEFAULT_DATABASE)
    schema = os.getenv("SNOWFLAKE_SCHEMA", DEFAULT_SCHEMA)
    warehouse = os.getenv("SNOWFLAKE_WAREHOUSE", DEFAULT_WAREHOUSE)

    cursor.execute(f'USE WAREHOUSE "{warehouse}"')
    cursor.execute(f'USE DATABASE "{database}"')
    cursor.execute(f'USE SCHEMA "{schema}"')


def run_query(sql: str) -> List[Dict[str, Any]]:
    conn = None
    cur = None
    try:
        conn = get_snowflake_connection()
        cur = conn.cursor(DictCursor)
        _apply_session_context(cur)
        cur.execute(sql)
        rows = cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()