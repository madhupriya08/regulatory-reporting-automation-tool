"""Run the SQLite reconciliation queries and export one result CSV each.

This is the batch step a scheduler would call: build the database, run the
tie-outs, drop the results somewhere a controller can pick them up. The
dashboard reads live from the database rather than from these files, so the
CSVs exist for the humans and the downstream tooling, not for the app.

--------------------------------------------------------------------------
WHY THE SQL FILES ARE DISCOVERED WITH glob('*.sql') AND NOT rglob
--------------------------------------------------------------------------
sql/snowflake/ contains a Snowflake-DIALECT rewrite of query 2 - a real FULL
OUTER JOIN, which SQLite cannot parse. If this runner recursed into
subdirectories it would pick that file up, hand it to SQLite, and die with a
syntax error that points at the SQL rather than at the discovery logic.

Two independent guards prevent that, because a single one is easy to defeat
by accident later:
  1. glob('*.sql') is non-recursive, so sql/snowflake/ is never visited.
  2. _sqlite_query_files() re-checks every discovered path and refuses
     anything living under the Snowflake directory.
The second guard is what survives someone "helpfully" switching the glob to
rglob six months from now.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import OUTPUT_DIR, SNOWFLAKE_SQL_DIR, SQL_DIR, SQLITE_DB_PATH  # noqa: E402
from src.audit_log import record_run  # noqa: E402


def _sqlite_query_files() -> list[Path]:
    """Return the SQLite-dialect query files, in run order.

    Guard 1 is the non-recursive glob. Guard 2 is the explicit exclusion
    below, which holds even if the glob is later widened.
    """
    files = sorted(SQL_DIR.glob("*.sql"))

    snowflake_dir = SNOWFLAKE_SQL_DIR.resolve()
    kept: list[Path] = []
    for path in files:
        if snowflake_dir in path.resolve().parents:
            # Snowflake-only dialect. Handing this to SQLite would fail on
            # FULL OUTER JOIN and the error message would blame the SQL.
            continue
        kept.append(path)

    if not kept:
        raise FileNotFoundError(f"No SQLite query files found in {SQL_DIR}")
    return kept


def run_all(db_path: Path = SQLITE_DB_PATH, output_dir: Path = OUTPUT_DIR
            ) -> dict[str, pd.DataFrame]:
    """Execute every SQLite query and write output/<query stem>.csv."""
    if not db_path.exists():
        raise FileNotFoundError(
            f"{db_path} not found. Run `python src/build_database.py` first."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, pd.DataFrame] = {}

    with sqlite3.connect(db_path) as conn:
        for sql_path in _sqlite_query_files():
            frame = pd.read_sql_query(sql_path.read_text(), conn)
            frame.to_csv(output_dir / f"{sql_path.stem}.csv", index=False)
            results[sql_path.stem] = frame

        # What this run concluded, recorded against the code version and the
        # input hashes that produced it. A break count on its own is a claim;
        # a break count tied to a commit and a set of SHA-256 digests is
        # something an examiner can re-derive.
        record_run(
            conn,
            stage="run_queries",
            backend="sqlite",
            detail={
                "queries_run": sorted(results),
                "breaks_by_query": {
                    name: int((frame["status"] == "BREAK").sum())
                    for name, frame in results.items()
                    if "status" in frame.columns
                },
            },
        )

    return results


def main() -> None:
    results = run_all()

    print(f"Backend: SQLite ({SQLITE_DB_PATH})")
    print()
    for name, frame in results.items():
        # Not every query has a status column - the trend query is already
        # aggregated - so the break count is reported only where it means
        # something rather than faked with a zero.
        if "status" in frame.columns:
            breaks = int((frame["status"] == "BREAK").sum())
            detail = f"{len(frame):>3} rows, {breaks:>2} BREAK"
        else:
            detail = f"{len(frame):>3} rows"
        print(f"  {name:<34} {detail}   -> output/{name}.csv")

    total_breaks = sum(
        int((f["status"] == "BREAK").sum())
        for f in results.values()
        if "status" in f.columns
    )
    print()
    print(f"Total breaks flagged across both tie-outs: {total_breaks}")


if __name__ == "__main__":
    main()
