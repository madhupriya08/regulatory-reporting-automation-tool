"""Tests for the append-only audit trail.

The trail's whole value is a claim about what CANNOT happen to it: nothing
rewrites history. A docstring saying so is worth nothing under challenge, so
the properties are asserted here - including one test that reads the source
code itself, because "no UPDATE statement exists" is a fact about the codebase
rather than about any single run.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import ANSWER_KEY_CSV, MATERIALITY_THRESHOLD_PCT, TABLE_SOURCES  # noqa: E402
from src.audit_log import (  # noqa: E402
    AUDIT_TABLES,
    AUDITED_INPUTS,
    code_version,
    file_fingerprint,
    record_run,
    run_history,
)


def test_a_run_is_recorded_with_everything_needed_to_reproduce_it(conn):
    """The row must answer: which code, which threshold, when, on what."""
    run_id = record_run(conn, stage="unit_test", backend="sqlite", detail={"k": 1})

    row = conn.execute(
        "SELECT * FROM audit_run_log WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row is not None

    columns = [d[0] for d in conn.execute(
        "SELECT * FROM audit_run_log LIMIT 0").description]
    record = dict(zip(columns, row))

    assert record["stage"] == "unit_test"
    assert record["backend"] == "sqlite"
    assert record["materiality_threshold_pct"] == MATERIALITY_THRESHOLD_PCT
    assert record["code_version"]
    assert record["run_timestamp_utc"].endswith("+00:00"), (
        "timestamps must be unambiguous UTC - a local timestamp in an audit "
        "trail is not evidence of when anything happened"
    )
    assert '"k": 1' in record["detail_json"]


def test_every_input_file_is_fingerprinted(conn):
    """Names prove nothing; digests prove the exact bytes that were read."""
    run_id = record_run(conn, stage="unit_test")

    recorded = dict(conn.execute(
        "SELECT file_name, sha256 FROM audit_input_file WHERE run_id = ?", (run_id,)
    ).fetchall())

    expected_files = {p.name for p in AUDITED_INPUTS.values()}
    assert set(recorded) == expected_files

    # The answer key is audited alongside the three source tables: it is what
    # the results are graded against, so a run should be able to prove which
    # version of the key judged it.
    assert ANSWER_KEY_CSV.name in recorded
    for path in TABLE_SOURCES.values():
        assert path.name in recorded

    for path in AUDITED_INPUTS.values():
        assert recorded[path.name] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_fingerprint_row_count_excludes_the_header(tmp_path):
    """An off-by-one here would make audited counts disagree with loaded counts,
    and the disagreement would look like a data-loss bug in the loader."""
    csv = tmp_path / "sample.csv"
    csv.write_text("a,b\n1,2\n3,4\n")          # header + 2 data rows
    assert file_fingerprint(csv)["row_count"] == 2

    csv.write_text("a,b\n1,2\n3,4")            # no trailing newline
    assert file_fingerprint(csv)["row_count"] == 2


def test_recording_a_run_never_touches_earlier_runs(conn):
    """Append-only, demonstrated rather than asserted in prose."""
    first = record_run(conn, stage="first", detail={"n": 1})
    before = conn.execute(
        "SELECT * FROM audit_run_log WHERE run_id = ?", (first,)
    ).fetchone()
    count_before = conn.execute("SELECT COUNT(*) FROM audit_run_log").fetchone()[0]

    record_run(conn, stage="second", detail={"n": 2})

    after = conn.execute(
        "SELECT * FROM audit_run_log WHERE run_id = ?", (first,)
    ).fetchone()
    count_after = conn.execute("SELECT COUNT(*) FROM audit_run_log").fetchone()[0]

    assert after == before, "an earlier run record was modified"
    assert count_after == count_before + 1, "the new run did not simply append"


def test_no_code_anywhere_updates_or_deletes_the_audit_tables():
    """The append-only guarantee, enforced against the source, not a single run.

    A test that only exercises the current code path would pass the day someone
    adds a cleanup routine elsewhere. This reads every Python and SQL file in
    the project and fails if any statement targets an audit table with UPDATE,
    DELETE, DROP or TRUNCATE. It is a blunt instrument - a string search - but
    the property it defends is exactly the kind that erodes quietly.
    """
    offenders: list[str] = []
    pattern = re.compile(
        r"\b(UPDATE|DELETE\s+FROM|DROP\s+TABLE|TRUNCATE(?:\s+TABLE)?)\s+"
        r"(?:IF\s+EXISTS\s+)?[\"'`]?(" + "|".join(AUDIT_TABLES) + r")\b",
        re.IGNORECASE,
    )

    searched = 0
    for path in list(PROJECT_ROOT.glob("src/*.py")) + list(PROJECT_ROOT.glob("sql/**/*.sql")):
        searched += 1
        for match in pattern.finditer(path.read_text()):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {match.group(0)!r}")

    assert searched > 0, "the file sweep found nothing to search"
    assert not offenders, (
        "the audit trail is supposed to be append-only, but these statements "
        "modify or drop it:\n  " + "\n  ".join(offenders)
    )


def test_rebuilding_the_database_preserves_audit_history(tmp_path):
    """build_database drops the three source tables. It must not drop these.

    This is the property that makes the trail a history rather than a snapshot:
    a rebuild is itself an auditable event, and a trail wiped by every rebuild
    would record only the most recent run - precisely the run least in need of
    independent evidence.
    """
    from src.build_database import build

    db = tmp_path / "audit_history.db"

    build(db)
    with sqlite3.connect(db) as conn:
        after_first = conn.execute("SELECT COUNT(*) FROM audit_run_log").fetchone()[0]
    assert after_first == 1

    build(db)
    build(db)
    with sqlite3.connect(db) as conn:
        after_third = conn.execute("SELECT COUNT(*) FROM audit_run_log").fetchone()[0]
        source_rows = conn.execute("SELECT COUNT(*) FROM general_ledger").fetchone()[0]

    assert after_third == 3, (
        f"expected 3 accumulated run records, found {after_third} - a rebuild "
        f"is destroying audit history"
    )
    # Meanwhile the source tables were genuinely rebuilt, not appended to.
    assert source_rows == 60


def test_code_version_flags_an_uncommitted_working_tree():
    """A clean SHA recorded against a dirty tree is a small lie in an audit
    trail, which is the one place small lies matter most. The -dirty suffix
    says the commit alone does not fully describe what ran."""
    version = code_version()
    assert version, "code_version must never return empty"

    import subprocess
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    if dirty.returncode == 0 and dirty.stdout.strip():
        assert version.endswith("-dirty"), (
            f"working tree has uncommitted changes but code_version() returned "
            f"{version!r} without the -dirty marker"
        )


def test_history_is_returned_newest_first(conn):
    record_run(conn, stage="older")
    record_run(conn, stage="newer")

    history = run_history(conn, limit=2)
    assert len(history) == 2
    assert history[0]["stage"] == "newer"


def test_run_history_does_not_mutate_anything(conn):
    record_run(conn, stage="readonly_probe")
    before = conn.execute("SELECT COUNT(*) FROM audit_run_log").fetchone()[0]
    run_history(conn)
    after = conn.execute("SELECT COUNT(*) FROM audit_run_log").fetchone()[0]
    assert before == after
