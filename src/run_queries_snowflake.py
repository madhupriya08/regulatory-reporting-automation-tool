"""Run the reconciliation queries against Snowflake and export the same CSVs.

The Snowflake counterpart of src/run_queries.py. It writes to the same
output/*.csv paths with the same filenames and the same columns, so anything
consuming those files - a downstream report, a spreadsheet, a person - cannot
tell which engine produced them. That is the point: the backend is an
infrastructure choice, not a change to the deliverable.

WHICH SQL EACH QUERY GETS
Query selection goes through backends.resolve_query_path, which prefers
sql/snowflake/<stem>_snowflake.sql when one exists and falls back to the shared
sql/<stem>.sql when it does not. Today that means query 2 runs the native FULL
OUTER JOIN rewrite while queries 1 and 3 run the exact same text SQLite runs -
they are portable ANSI and there is nothing to gain from forking them. Keeping
them as one file rather than two copies is what stops the backends from
quietly diverging on the logic that actually decides what counts as a break.

The run prints which file each query resolved to, because "which SQL actually
ran" is the first question anyone asks when two backends disagree.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import OUTPUT_DIR  # noqa: E402
from src.backends import QUERY_STEMS, resolve_query_path, run_snowflake  # noqa: E402
from src.snowflake_conn import check_env, describe_config  # noqa: E402


def run_all(output_dir: Path = OUTPUT_DIR) -> dict:
    check_env()
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for stem in QUERY_STEMS:
        # snowflake=True explicitly rather than reading USE_SNOWFLAKE: this
        # script IS the Snowflake runner, so it must resolve Snowflake SQL even
        # if the operator forgot to export the variable. Inferring it from the
        # environment here would let the script silently run SQLite-dialect SQL
        # against Snowflake and call it a Snowflake run.
        path = resolve_query_path(stem, snowflake=True)
        frame = run_snowflake(path.read_text())
        frame.to_csv(output_dir / f"{stem}.csv", index=False)
        results[stem] = (path, frame)

    return results


def main() -> None:
    print("Running reconciliations against Snowflake:")
    print(describe_config())
    print()

    results = run_all()

    for stem, (path, frame) in results.items():
        if "status" in frame.columns:
            breaks = int((frame["status"] == "BREAK").sum())
            detail = f"{len(frame):>3} rows, {breaks:>2} BREAK"
        else:
            detail = f"{len(frame):>3} rows"
        source = path.relative_to(Path(__file__).resolve().parents[1])
        print(f"  {stem:<34} {detail}   [{source}]  -> output/{stem}.csv")

    total = sum(
        int((f["status"] == "BREAK").sum())
        for _, f in results.values()
        if "status" in f.columns
    )
    print()
    print(f"Total breaks flagged across both tie-outs: {total}")


if __name__ == "__main__":
    main()
