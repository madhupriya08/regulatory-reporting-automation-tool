"""Print the SQLite schema: DDL, indexes, row counts and a sample of each table.

A convenience for anyone opening the project for the first time, and the thing
the IDE "Show schema" run configurations call. It exists because the useful
answer to "what does this database look like?" is a page of output, not a
sqlite3 prompt someone has to know six dot-commands to drive.

Read-only: it opens the database in read-only mode so running it can never
alter the thing it is describing.
"""

from __future__ import annotations

import sqlite3
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SQLITE_DB_PATH, TABLE_SOURCES  # noqa: E402


def main() -> int:
    if not SQLITE_DB_PATH.exists():
        print(
            f"{SQLITE_DB_PATH} not found.\n"
            "Run `python src/generate_data.py` then `python src/build_database.py` first.",
            file=sys.stderr,
        )
        return 1

    # Read-only URI: a tool whose job is to describe the database should be
    # incapable of modifying it, even by accident.
    conn = sqlite3.connect(f"file:{SQLITE_DB_PATH}?mode=ro", uri=True)

    print("=" * 78)
    print(f"SCHEMA  {SQLITE_DB_PATH.name}")
    print("=" * 78)

    for name, obj_type, sql in conn.execute(
        "SELECT name, type, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL ORDER BY type DESC, name"
    ):
        print(f"\n-- {obj_type.upper()}: {name}")
        print(textwrap.dedent(sql).strip())

    print("\n" + "=" * 78)
    print("ROW COUNTS AND SAMPLE ROWS")
    print("=" * 78)

    for table in TABLE_SOURCES:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"\n{table}  ({count:,} rows)")

        cursor = conn.execute(f"SELECT * FROM {table} LIMIT 3")
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()

        # Width from the widest actual value, not a guess. loan_id values are
        # longer than the header, so a fixed width silently ragged the columns.
        widths = [
            max(len(col), *(len(str(row[i])) for row in rows)) if rows else len(col)
            for i, col in enumerate(columns)
        ]

        print("  " + "  ".join(col.ljust(w) for col, w in zip(columns, widths)))
        for row in rows:
            print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths)))

    conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Piping into `head` closes the pipe early. That is the caller being
        # done reading, not an error, and a tool that dumps a traceback for it
        # looks broken the first time anyone tries `show_schema.py | head`.
        sys.stderr.close()
        sys.exit(0)
