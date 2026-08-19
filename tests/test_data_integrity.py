"""Tests for the loading boundary and for the answer key itself.

If either of these is wrong, every reconciliation test above is measuring the
wrong thing while still passing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    ENTITIES,
    MATERIALITY_THRESHOLD_PCT,
    PRODUCTS,
    QUARTERS,
    SNOWFLAKE_SQL_DIR,
    SQL_DIR,
)
from src.run_queries import _sqlite_query_files  # noqa: E402


# ---------------------------------------------------------------------------
# Nothing dropped or duplicated on the way into SQLite
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", ["general_ledger", "sub_ledger", "regulatory_extract"])
def test_row_counts_survive_the_load(conn, source_frames, table):
    """The database holds exactly as many rows as the CSV did.

    A loader that drops rows understates every balance and manufactures
    late-feed breaks; one that duplicates rows manufactures duplicate-load
    breaks. Either way the tool reports defects in its own plumbing as defects
    in the bank's data, and nothing downstream can tell the difference.
    """
    in_db = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert in_db == len(source_frames[table]), (
        f"{table}: CSV has {len(source_frames[table])} rows, database has {in_db}"
    )


@pytest.mark.parametrize(
    "table,column",
    [
        ("general_ledger", "gl_balance"),
        ("sub_ledger", "loan_amount"),
        ("regulatory_extract", "extract_balance"),
    ],
)
def test_balances_survive_the_load(conn, source_frames, table, column):
    """Row counts alone would not catch one row dropped and another doubled.

    Summing the monetary column is the cheap checksum that does: a compensating
    pair of errors that leaves the count intact almost never leaves the total
    intact too.
    """
    in_db = conn.execute(f"SELECT ROUND(SUM({column}), 2) FROM {table}").fetchone()[0]
    in_csv = round(float(source_frames[table][column].sum()), 2)
    assert in_db == pytest.approx(in_csv, abs=0.01), (
        f"{table}.{column}: CSV totals {in_csv:,.2f}, database totals {in_db:,.2f}"
    )


def test_sub_ledger_loan_multiset_survives_the_load(conn, source_frames):
    """The strongest form of the check: the exact bag of loan_ids matches.

    Duplicated loan_ids are legitimate here - a replayed batch is one of the
    planted breaks - so this compares MULTISETS. It therefore proves the
    duplicates that exist are the ones the generator planted and not extras the
    loader introduced, which a DISTINCT-based check could never distinguish.
    """
    in_db = pd.read_sql_query("SELECT loan_id FROM sub_ledger", conn)["loan_id"]
    assert (
        in_db.value_counts().sort_index().equals(
            source_frames["sub_ledger"]["loan_id"].value_counts().sort_index()
        )
    ), "loan_id multiset differs between the CSV and the database"


# ---------------------------------------------------------------------------
# The reporting universe is complete
# ---------------------------------------------------------------------------
def test_general_ledger_covers_every_account(conn):
    """5 entities x 6 products x 2 quarters, with no combo appearing twice.

    A missing GL row would silently shrink the population that queries 1 and 2
    reconcile, so the break RATE would improve simply because an account
    vanished - the most flattering possible way for the tool to be wrong.
    """
    rows = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT entity || product || quarter) FROM general_ledger"
    ).fetchone()
    expected = len(ENTITIES) * len(PRODUCTS) * len(QUARTERS)
    assert rows[0] == expected, f"expected {expected} GL rows, found {rows[0]}"
    assert rows[1] == expected, "general_ledger contains duplicate entity/product/quarter rows"


# ---------------------------------------------------------------------------
# The answer key is a usable grading key
# ---------------------------------------------------------------------------
def test_answer_key_has_one_row_per_affected_account(answer_key):
    """No account is planted twice.

    If a combo carried two breaks, 'what should the SQL have found here?' would
    have two answers and the exact-count assertions in the reconciliation tests
    would be meaningless.
    """
    dupes = answer_key.duplicated(["entity", "product", "quarter"])
    assert not dupes.any(), (
        "answer key plants two breaks on the same account:\n"
        f"{answer_key[dupes][['entity', 'product', 'quarter']]}"
    )


def test_answer_key_covers_all_four_failure_modes(answer_key):
    assert set(answer_key["break_type"]) == {
        "late_feed", "duplicate_load", "timing_difference", "mapping_error",
    }


def test_every_planted_break_clears_materiality(answer_key):
    """Every planted break is materially larger than the threshold.

    A break planted at 0.51% would pass or fail on floating-point luck, and the
    suite would then be grading arithmetic noise rather than detection logic.
    Requiring a clear margin keeps the tests measuring what they claim to.
    """
    smallest = answer_key["planted_variance_pct"].abs().min()
    assert smallest > MATERIALITY_THRESHOLD_PCT, (
        f"smallest planted break is {smallest}%, at or under the "
        f"{MATERIALITY_THRESHOLD_PCT}% threshold"
    )
    assert smallest > MATERIALITY_THRESHOLD_PCT * 2, (
        f"smallest planted break is {smallest}%, too close to the "
        f"{MATERIALITY_THRESHOLD_PCT}% threshold to grade detection reliably"
    )


def test_mapping_errors_are_planted_as_offsetting_pairs(answer_key):
    """Each mapping error nets to zero across its two sides.

    Query 2 identifies a mapping error by finding that equal-and-opposite
    partner. If the generator ever planted a one-sided mapping error the query
    would correctly label it a timing difference, and the resulting test
    failure would point at the SQL instead of at the data.
    """
    mapping = answer_key[answer_key["break_type"] == "mapping_error"]
    assert not mapping.empty
    for break_id, pair in mapping.groupby("break_id"):
        assert len(pair) == 2, f"{break_id} is not a pair"
        assert set(pair["expected_direction"]) == {"under", "over"}
        assert round(float(pair["planted_variance"].sum()), 2) == 0.0, (
            f"{break_id} does not net to zero"
        )


# ---------------------------------------------------------------------------
# The threshold has one definition
# ---------------------------------------------------------------------------
def test_sql_files_enforce_the_configured_threshold():
    """Every SQLite query compares against the same 0.5% as config.py.

    The threshold necessarily lives in both the .sql files and Python. This
    catches the day someone tunes one and forgets the other, which would make
    the dashboard's stated threshold a lie about what the SQL actually did.
    """
    threshold = str(MATERIALITY_THRESHOLD_PCT)
    for path in SQL_DIR.glob("*.sql"):
        assert f"> {threshold}" in path.read_text(), (
            f"{path.name} does not enforce the configured {threshold}% threshold"
        )


# ---------------------------------------------------------------------------
# The SQLite runner never touches Snowflake-dialect SQL
# ---------------------------------------------------------------------------
def test_sqlite_runner_excludes_snowflake_sql():
    """The Snowflake rewrite uses FULL OUTER JOIN, which SQLite cannot parse.

    If the runner ever picked it up the failure would be a SQL syntax error
    pointing at the query file, sending whoever debugs it to entirely the wrong
    place. This asserts the exclusion holds rather than trusting that the glob
    stays non-recursive.
    """
    discovered = _sqlite_query_files()
    assert discovered, "runner found no SQLite queries at all"

    snowflake_dir = SNOWFLAKE_SQL_DIR.resolve()
    for path in discovered:
        assert snowflake_dir not in path.resolve().parents, (
            f"SQLite runner picked up the Snowflake-dialect file {path}"
        )
    assert not any("snowflake" in p.name.lower() for p in discovered)


# ---------------------------------------------------------------------------
# The generator is reproducible
# ---------------------------------------------------------------------------
def test_generator_is_byte_for_byte_reproducible(tmp_path):
    """Two runs of the generator must produce identical CSVs.

    This test exists because it caught a real bug. gl_account was built from
    Python's hash() on a tuple of strings, and CPython salts str hashing per
    process unless PYTHONHASHSEED is pinned - so the column changed on every
    run while the fixed numpy seed made everything else look stable.

    That matters more than a cosmetic diff. The committed answer key is only a
    valid grading key for the exact CSVs committed beside it. A generator whose
    output drifts between runs means anyone who regenerates gets data the key no
    longer describes, and the failure surfaces as an inexplicable test failure
    somewhere else entirely. The fix was zlib.crc32, which is stable across
    processes, platforms and Python versions.
    """
    import hashlib
    import subprocess

    project_root = Path(__file__).resolve().parents[1]

    def digests() -> dict[str, str]:
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((project_root / "data").glob("*.csv"))
        }

    before = digests()

    # Fresh interpreter with a different hash seed: if anything still depends
    # on salted hashing, this is what surfaces it. Running in-process would
    # reuse this process's seed and prove nothing.
    result = subprocess.run(
        [sys.executable, str(project_root / "src" / "generate_data.py")],
        cwd=project_root,
        env={**os.environ, "PYTHONHASHSEED": "12345"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"generator failed:\n{result.stderr}"

    after = digests()
    differing = [name for name in before if before[name] != after.get(name)]
    assert not differing, (
        f"regenerating changed {differing} - the generator is not reproducible, "
        f"so the committed answer key may not describe the committed data"
    )
