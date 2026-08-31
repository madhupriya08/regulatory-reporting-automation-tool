"""Backend selection and result normalisation.

One module owns the answer to two questions - which engine is live, and which
SQL file to hand it - so that no consumer has to. The dashboard, the runners
and the tests all go through here, which is why switching engines takes an
environment variable and no code change at all.

--------------------------------------------------------------------------
THE COLUMN-CASING GOTCHA
--------------------------------------------------------------------------
Snowflake folds unquoted identifiers to UPPERCASE and returns result columns
that way: ENTITY, PRODUCT, GL_BALANCE. SQLite preserves whatever case the
query used, so the same query returns entity, product, gl_balance.

Every consumer of these frames indexes columns by name. Left alone, that means
`frame["entity"]` works on SQLite and raises KeyError on Snowflake, and the
"fix" people reach for is a scattering of `.get("entity") or .get("ENTITY")`
through the dashboard - which spreads a backend detail into code that has no
business knowing about it, and misses one spot.

So it is normalised exactly once, here, at the boundary: every frame leaving
this module has lowercase column names regardless of engine. Nothing
downstream needs to know which backend ran.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SNOWFLAKE_SQL_DIR, SQL_DIR, SQLITE_DB_PATH  # noqa: E402

# Accepted truthy spellings for USE_SNOWFLAKE. Anything else - including unset,
# empty, "0" and "false" - selects SQLite. The default is deliberately the
# local backend: a mistyped variable should never silently point a run at a
# live warehouse.
_TRUTHY = {"1", "true", "yes", "y", "on"}


def use_snowflake() -> bool:
    return os.environ.get("USE_SNOWFLAKE", "").strip().lower() in _TRUTHY


def backend_name() -> str:
    return "Snowflake" if use_snowflake() else "SQLite"


def normalize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Lowercase every column label. The one place casing is reconciled."""
    frame.columns = [str(col).lower() for col in frame.columns]
    return frame


def resolve_query_path(stem: str, snowflake: bool | None = None) -> Path:
    """Return the SQL file for ``stem`` on the active backend.

    Convention over configuration: a Snowflake-specific rewrite lives at
    sql/snowflake/<stem>_snowflake.sql and is picked up automatically when it
    exists. Queries with no dialect-specific version - 1 and 3, which are
    portable ANSI - fall through to the shared file in sql/ and are therefore
    guaranteed to be the same text on both engines rather than two copies that
    can drift.

    Adding a Snowflake rewrite of another query later means dropping a file in
    that directory. No registry to update, and nothing to forget.
    """
    if snowflake is None:
        snowflake = use_snowflake()

    if snowflake:
        override = SNOWFLAKE_SQL_DIR / f"{stem}_snowflake.sql"
        if override.exists():
            return override
    return SQL_DIR / f"{stem}.sql"


# The reconciliations, in the order a close would run them.
QUERY_STEMS = (
    "01_gl_vs_subledger",
    "02_gl_vs_regulatory_extract",
    "03_quarterly_break_trend",
    "04_break_analytics",
)


def run_sqlite(sql: str) -> pd.DataFrame:
    if not SQLITE_DB_PATH.exists():
        raise FileNotFoundError(
            f"{SQLITE_DB_PATH} not found. Run `python src/build_database.py` first."
        )
    with sqlite3.connect(SQLITE_DB_PATH) as conn:
        return normalize_columns(pd.read_sql_query(sql, conn))


def run_snowflake(sql: str) -> pd.DataFrame:
    """Execute one query against Snowflake and return a lowercase-column frame.

    fetch_pandas_all() is used where available because it pulls the result set
    over Arrow rather than row by row, which matters once a sub-ledger is
    millions of rows rather than thousands. It only supports Arrow-eligible
    result sets, so a cursor-description fallback keeps the path working
    regardless.
    """
    from src.snowflake_conn import connection

    with connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            try:
                frame = cursor.fetch_pandas_all()
            except Exception:
                columns = [col[0] for col in cursor.description]
                frame = pd.DataFrame(cursor.fetchall(), columns=columns)
        finally:
            cursor.close()

    return normalize_columns(frame)


def run_query(sql: str) -> pd.DataFrame:
    """Execute SQL on whichever backend USE_SNOWFLAKE selects."""
    return run_snowflake(sql) if use_snowflake() else run_sqlite(sql)


def run_reconciliation(stem: str) -> pd.DataFrame:
    """Run one named reconciliation on the active backend."""
    return run_query(resolve_query_path(stem).read_text())


def load_all() -> dict[str, pd.DataFrame]:
    """Run all three reconciliations on the active backend."""
    return {stem: run_reconciliation(stem) for stem in QUERY_STEMS}
