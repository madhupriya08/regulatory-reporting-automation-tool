"""Load the three source CSVs into Snowflake.

The Snowflake counterpart of src/build_database.py. Same three tables, same
column names, loaded from the same CSVs - so the reconciliation SQL is
comparing like with like and a result difference between the two backends
means a real difference, not a data-loading difference.

--------------------------------------------------------------------------
WHY THERE IS NO CREATE INDEX STEP HERE
--------------------------------------------------------------------------
The SQLite builder creates three composite indexes on (entity, product,
quarter) because SQLite would otherwise rescan the whole sub-ledger once per
grouping. Snowflake has no equivalent object, and the omission is correct
rather than an oversight.

Snowflake stores tables as immutable micro-partitions and records the min/max
of every column in each one. A query filtering or grouping on entity, product
and quarter prunes whole micro-partitions using that metadata, with nothing to
declare and nothing to maintain. There is no B-tree to keep in sync on write,
which is why the pattern that speeds up SQLite has no analogue here.

Snowflake does offer clustering keys, which control the ORDER data is written
in so pruning gets sharper. They are not used here and should not be: they are
for tables in the multi-terabyte range where natural clustering has degraded,
they incur continuous background reclustering cost, and on a 6,000-row
sub-ledger they would cost money to no measurable benefit. Reaching for one
here would be cargo-culting the index habit into an engine that does not want
it.

--------------------------------------------------------------------------
WHY NUMBER(18, 2) AND NOT FLOAT
--------------------------------------------------------------------------
The SQLite schema uses REAL because SQLite's type system offers nothing
better. Snowflake has true fixed-point numerics, so balances are stored as
NUMBER(18, 2) - exact decimal arithmetic on money, no binary floating-point
representation error to reason about at all. On a reconciliation whose entire
premise is that clean accounts tie out to the cent, that is the right type,
and it is one of the few places where the Snowflake backend is not merely
different but better.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import TABLE_SOURCES  # noqa: E402
from src.snowflake_conn import (  # noqa: E402
    check_env,
    cli_main,
    connection,
    describe_config,
)

SCHEMAS: dict[str, str] = {
    "general_ledger": """
        CREATE OR REPLACE TABLE general_ledger (
            entity      VARCHAR(64)   NOT NULL,
            product     VARCHAR(64)   NOT NULL,
            quarter     VARCHAR(8)    NOT NULL,
            gl_balance  NUMBER(18, 2) NOT NULL,
            gl_account  VARCHAR(32)   NOT NULL,
            as_of_date  DATE          NOT NULL
        )
    """,
    "sub_ledger": """
        CREATE OR REPLACE TABLE sub_ledger (
            loan_id       VARCHAR(64)   NOT NULL,
            entity        VARCHAR(64)   NOT NULL,
            product       VARCHAR(64)   NOT NULL,
            quarter       VARCHAR(8)    NOT NULL,
            loan_amount   NUMBER(18, 2) NOT NULL,
            load_batch_id VARCHAR(32)   NOT NULL
        )
    """,
    # As in SQLite: no unique constraint on loan_id. A replayed batch produces
    # the same id twice and that duplication IS the break being reconciled for.
    "regulatory_extract": """
        CREATE OR REPLACE TABLE regulatory_extract (
            entity           VARCHAR(64)   NOT NULL,
            product          VARCHAR(64)   NOT NULL,
            quarter          VARCHAR(8)    NOT NULL,
            extract_balance  NUMBER(18, 2) NOT NULL,
            schedule_code    VARCHAR(16)   NOT NULL,
            extract_run_date DATE          NOT NULL
        )
    """,
}

# NOTE: no INDEXES list. See the module docstring - this is deliberate.


def _load_frame(conn, table: str, frame: pd.DataFrame) -> None:
    """Insert a DataFrame into an existing Snowflake table.

    write_pandas stages the frame as Parquet and COPYs it in, which is the
    right shape for a bulk load and stays right as the sub-ledger grows. The
    executemany fallback exists because write_pandas depends on optional
    pyarrow extras that are not guaranteed to be present, and a missing extra
    should degrade the load, not fail the run.

    Column names are uppercased first because the tables were created with
    unquoted identifiers, which Snowflake folded to uppercase in the catalog.
    Passing quote_identifiers=False alongside keeps write_pandas from quoting
    them into a case-sensitive mismatch that would fail with a confusing
    "invalid identifier" error.
    """
    upper = frame.copy()
    upper.columns = [col.upper() for col in upper.columns]

    try:
        from snowflake.connector.pandas_tools import write_pandas

        success, _chunks, rows, _output = write_pandas(
            conn, upper, table.upper(), quote_identifiers=False
        )
        if not success:
            raise RuntimeError(f"write_pandas reported failure loading {table}")
        return
    except ImportError:
        pass

    placeholders = ", ".join(["%s"] * len(upper.columns))
    columns = ", ".join(upper.columns)
    cursor = conn.cursor()
    try:
        cursor.executemany(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            upper.astype(object).where(pd.notna(upper), None).values.tolist(),
        )
    finally:
        cursor.close()


def build() -> dict[str, int]:
    """Rebuild the Snowflake tables from the CSVs. Returns rows loaded."""
    missing = [str(p) for p in TABLE_SOURCES.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Source CSVs not found: " + ", ".join(missing)
            + "\nRun `python src/generate_data.py` first."
        )

    loaded: dict[str, int] = {}

    with connection() as conn:
        for table, csv_path in TABLE_SOURCES.items():
            frame = pd.read_csv(csv_path)

            cursor = conn.cursor()
            try:
                # CREATE OR REPLACE rather than DROP + CREATE: it is atomic, so
                # a failure partway through leaves the previous table intact
                # instead of leaving the schema with a table missing entirely.
                cursor.execute(SCHEMAS[table])
            finally:
                cursor.close()

            _load_frame(conn, table, frame)

            # Same load-boundary reconciliation as the SQLite builder. A tool
            # that silently drops or doubles rows on the way in reports its own
            # plumbing defects as the bank's, and nothing downstream can tell
            # the difference.
            cursor = conn.cursor()
            try:
                in_db = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                cursor.close()

            if in_db != len(frame):
                raise RuntimeError(
                    f"Row count mismatch loading {table}: "
                    f"CSV had {len(frame):,} rows, Snowflake has {in_db:,}."
                )
            loaded[table] = in_db

    return loaded


def main() -> None:
    # Validate before printing anything: a run that announces "Building
    # Snowflake tables with: NOT SET, NOT SET, NOT SET" and only then reports
    # the problem has wasted the operator's first read of the output.
    check_env()

    print("Building Snowflake tables with:")
    print(describe_config())
    print()

    loaded = build()
    for table, count in loaded.items():
        print(f"  {table:<22} {count:>7,} rows")
    print("Row counts reconcile against the source CSVs exactly.")
    print("No indexes created - Snowflake prunes via micro-partition metadata.")


if __name__ == "__main__":
    sys.exit(cli_main(main))
