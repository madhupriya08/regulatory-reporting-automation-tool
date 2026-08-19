"""Load the three source CSVs into a local SQLite database.

This is the local-development backend. The Snowflake equivalent lives in
src/build_snowflake.py and loads the SAME CSVs into the SAME three tables, so
the reconciliation SQL is comparing like with like no matter which engine is
running it.

The build is destructive and idempotent by design: every run drops and
recreates all three tables. A regulatory tie-out that appends to whatever was
already there would produce duplicate loans on the second run and report them
as a "duplicate load" break - the tool would manufacture the exact defect it
exists to detect.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SQLITE_DB_PATH, TABLE_SOURCES  # noqa: E402

# Columns are typed explicitly rather than letting pandas.to_sql infer them.
# Inference is per-run and depends on the data it happens to see; an explicit
# DDL means the schema is a reviewable artifact and cannot change under us
# because one quarter's CSV happened to contain only whole-dollar balances.
SCHEMAS: dict[str, str] = {
    "general_ledger": """
        CREATE TABLE general_ledger (
            entity      TEXT    NOT NULL,
            product     TEXT    NOT NULL,
            quarter     TEXT    NOT NULL,
            gl_balance  REAL    NOT NULL,
            gl_account  TEXT    NOT NULL,
            as_of_date  TEXT    NOT NULL
        )
    """,
    "sub_ledger": """
        CREATE TABLE sub_ledger (
            loan_id       TEXT NOT NULL,
            entity        TEXT NOT NULL,
            product       TEXT NOT NULL,
            quarter       TEXT NOT NULL,
            loan_amount   REAL NOT NULL,
            load_batch_id TEXT NOT NULL
        )
    """,
    # Deliberately NO primary key or unique constraint on loan_id. A replayed
    # batch legitimately produces the same loan_id twice, and that duplication
    # IS the defect we are reconciling for. A unique constraint here would
    # reject the broken data at load time and hide the break from the report.
    "regulatory_extract": """
        CREATE TABLE regulatory_extract (
            entity           TEXT NOT NULL,
            product          TEXT NOT NULL,
            quarter          TEXT NOT NULL,
            extract_balance  REAL NOT NULL,
            schedule_code    TEXT NOT NULL,
            extract_run_date TEXT NOT NULL
        )
    """,
}

# Every reconciliation query groups or joins on entity/product/quarter, so
# that composite is the only access path worth indexing. SQLite will otherwise
# scan the full sub-ledger once per grouping.
#
# This step has no Snowflake counterpart - see src/build_snowflake.py.
INDEXES = [
    "CREATE INDEX idx_gl_key      ON general_ledger      (entity, product, quarter)",
    "CREATE INDEX idx_sub_key     ON sub_ledger          (entity, product, quarter)",
    "CREATE INDEX idx_extract_key ON regulatory_extract  (entity, product, quarter)",
]


def build(db_path: Path = SQLITE_DB_PATH) -> dict[str, int]:
    """Rebuild the SQLite database from the CSVs. Returns rows loaded per table."""
    missing = [str(p) for p in TABLE_SOURCES.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Source CSVs not found: "
            + ", ".join(missing)
            + "\nRun `python src/generate_data.py` first."
        )

    loaded: dict[str, int] = {}

    with sqlite3.connect(db_path) as conn:
        for table, csv_path in TABLE_SOURCES.items():
            frame = pd.read_csv(csv_path)

            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(SCHEMAS[table])
            frame.to_sql(table, conn, if_exists="append", index=False)

            # Row-count reconciliation on the LOADER itself. A reconciliation
            # tool that silently drops or doubles rows on the way into the
            # database would report breaks that are artifacts of its own
            # plumbing - and it would be impossible to tell those apart from
            # the real thing. Verify at the boundary, immediately, and fail
            # loudly rather than producing a plausible-looking wrong number.
            in_db = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if in_db != len(frame):
                raise RuntimeError(
                    f"Row count mismatch loading {table}: "
                    f"CSV had {len(frame):,} rows, database has {in_db:,}."
                )
            loaded[table] = in_db

        for statement in INDEXES:
            conn.execute(statement)

        conn.commit()

    return loaded


def main() -> None:
    loaded = build()
    print(f"Built SQLite database: {SQLITE_DB_PATH}")
    for table, count in loaded.items():
        print(f"  {table:<22} {count:>7,} rows")
    print("Row counts reconcile against the source CSVs exactly.")


if __name__ == "__main__":
    main()
