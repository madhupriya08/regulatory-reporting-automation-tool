"""Append-only audit trail for reconciliation runs.

A regulatory reconciliation is not finished when it produces a number. It is
finished when someone can answer, months later and under challenge:

    Which code produced this figure? Which input files did it read? Were those
    files the ones we think they were? What threshold was applied? When did it
    run, and against which backend?

Without that, a break report is an assertion. With it, the report is evidence.
This module is what turns one into the other.

--------------------------------------------------------------------------
APPEND-ONLY, AND WHY THAT IS THE WHOLE POINT
--------------------------------------------------------------------------
Nothing here ever issues UPDATE or DELETE. Every run appends rows and touches
nothing that came before. An audit trail that can be edited is not an audit
trail - it is a log with extra steps, because the first thing anyone would
want to change is the record of the run that went wrong.

That property is enforced by a test that greps the codebase for UPDATE or
DELETE against these tables, rather than left as an intention in a docstring.

build_database.py drops and recreates the three source tables on every run.
It must never drop these two. They are created with CREATE TABLE IF NOT
EXISTS precisely so history survives a rebuild.

--------------------------------------------------------------------------
WHY FILE HASHES AND NOT JUST FILE NAMES
--------------------------------------------------------------------------
"We read general_ledger.csv" is worth very little - the file could have been
regenerated, edited, or swapped since. A SHA-256 pins the exact bytes. Two
runs recording the same digest provably read identical input; two runs
recording different digests provably did not, however identical the filenames
look. That is the difference between a lineage claim and a lineage proof.

--------------------------------------------------------------------------
HONEST LIMITATION
--------------------------------------------------------------------------
The trail lives in the same SQLite file as the data it describes, so deleting
that file deletes the history with it. That is acceptable for a local
development tool and is NOT acceptable in production, where the audit store
must be a separate, access-controlled, retention-managed system that the
pipeline can append to but not administer. Called out in the README scope note
rather than papered over.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    ANSWER_KEY_CSV,
    MATERIALITY_THRESHOLD_PCT,
    PROJECT_ROOT,
    TABLE_SOURCES,
)

# The answer key is not loaded into any table, but it IS an input to the
# process in the sense that matters here: the tests grade against it. Hashing
# it alongside the source CSVs means a run can prove which key it was judged by.
AUDITED_INPUTS = {**TABLE_SOURCES, "answer_key": ANSWER_KEY_CSV}

AUDIT_SCHEMAS = (
    """
    CREATE TABLE IF NOT EXISTS audit_run_log (
        run_id                    TEXT NOT NULL,
        run_timestamp_utc         TEXT NOT NULL,
        stage                     TEXT NOT NULL,
        backend                   TEXT NOT NULL,
        code_version              TEXT NOT NULL,
        materiality_threshold_pct REAL NOT NULL,
        python_version            TEXT NOT NULL,
        detail_json               TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_input_file (
        run_id      TEXT    NOT NULL,
        file_name   TEXT    NOT NULL,
        sha256      TEXT    NOT NULL,
        byte_size   INTEGER NOT NULL,
        row_count   INTEGER NOT NULL
    )
    """,
)

AUDIT_TABLES = ("audit_run_log", "audit_input_file")


def code_version() -> str:
    """The exact commit that produced a run, or an honest marker if unknown.

    A run recorded against "the code" is not reproducible; a run recorded
    against a commit SHA is. The -dirty suffix matters as much as the SHA: it
    says the working tree had uncommitted changes, so the commit alone does not
    fully describe what executed. Silently reporting the clean SHA in that case
    would be the audit trail telling a small lie.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        if sha.returncode != 0:
            return "unknown (not a git checkout)"

        version = sha.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            version += "-dirty"
        return version
    except (OSError, subprocess.SubprocessError):
        return "unknown (git unavailable)"


def file_fingerprint(path: Path) -> dict:
    """SHA-256, byte size and data-row count for one input file."""
    data = path.read_bytes()
    text = data.decode("utf-8")
    # Line count minus the header. Trailing newline would otherwise add a
    # phantom row and make the audited count disagree with the loaded count.
    rows = max(len(text.splitlines()) - 1, 0)
    return {
        "file_name": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
        "row_count": rows,
    }


def ensure_tables(conn: sqlite3.Connection) -> None:
    for statement in AUDIT_SCHEMAS:
        conn.execute(statement)


def record_run(
    conn: sqlite3.Connection,
    stage: str,
    backend: str = "sqlite",
    detail: dict | None = None,
) -> str:
    """Append one immutable run record plus its input fingerprints.

    Returns the run_id so a caller can print it - the identifier a person would
    quote when asking "where did this number come from?".
    """
    ensure_tables(conn)

    run_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn.execute(
        "INSERT INTO audit_run_log (run_id, run_timestamp_utc, stage, backend, "
        "code_version, materiality_threshold_pct, python_version, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            timestamp,
            stage,
            backend,
            code_version(),
            MATERIALITY_THRESHOLD_PCT,
            platform.python_version(),
            json.dumps(detail or {}, sort_keys=True),
        ),
    )

    for path in AUDITED_INPUTS.values():
        if not path.exists():
            continue
        fingerprint = file_fingerprint(path)
        conn.execute(
            "INSERT INTO audit_input_file (run_id, file_name, sha256, byte_size, "
            "row_count) VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                fingerprint["file_name"],
                fingerprint["sha256"],
                fingerprint["byte_size"],
                fingerprint["row_count"],
            ),
        )

    conn.commit()
    return run_id


def run_history(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Most recent runs, newest first. Read-only."""
    ensure_tables(conn)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM audit_run_log ORDER BY run_timestamp_utc DESC, rowid DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()
