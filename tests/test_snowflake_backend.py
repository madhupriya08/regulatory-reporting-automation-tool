"""Tests for the Snowflake backend.

Split into two halves on purpose.

The first half needs no Snowflake account at all: credential handling, backend
selection, SQL-file resolution, column-casing normalisation, and a semantic
equivalence check between the simulated and native FULL OUTER JOIN. These run
in CI and on any laptop, and they cover the parts most likely to break.

The second half needs a live warehouse and skips cleanly without one. Skipping
is the right behaviour rather than failing: a developer without Snowflake
access should still get a green suite for the SQLite path they can actually
run, and a red suite that everyone learns to ignore protects nothing.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import SNOWFLAKE_SQL_DIR, SQL_DIR  # noqa: E402
from src import backends  # noqa: E402
from src.snowflake_conn import (  # noqa: E402
    OPTIONAL_ENV_VARS,
    REQUIRED_ENV_VARS,
    SnowflakeConfigError,
    check_env,
    describe_config,
    missing_env_vars,
)

SNOWFLAKE_CONFIGURED = not missing_env_vars()
requires_snowflake = pytest.mark.skipif(
    not SNOWFLAKE_CONFIGURED,
    reason=(
        "Snowflake credentials not set: "
        + ", ".join(missing_env_vars())
        + ". These tests need a live warehouse."
    ),
)

# FULL OUTER JOIN landed in SQLite 3.39 (2022). Where the local build has it,
# the Snowflake-dialect query can be executed locally as a semantic check.
SQLITE_HAS_FULL_OUTER = sqlite3.sqlite_version_info >= (3, 39, 0)


# ===========================================================================
# Credential handling - no Snowflake account required
# ===========================================================================
def test_missing_env_vars_names_every_missing_variable(monkeypatch):
    """The error names ALL missing variables, not just the first one.

    Reporting them one at a time turns setup into six failed runs. The whole
    reason this helper exists instead of letting the connector raise is to give
    the operator one complete list they can act on in a single pass.
    """
    for name in REQUIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Clear the key-pair variable too: leaving it set would switch the auth
    # mode, and this test would then be asserting about a different required
    # set than the one it names.
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY_FILE", raising=False)

    assert missing_env_vars() == list(REQUIRED_ENV_VARS)

    with pytest.raises(SnowflakeConfigError) as exc:
        check_env()

    message = str(exc.value)
    for name in REQUIRED_ENV_VARS:
        assert name in message, f"{name} missing from the error message"
    assert "export" in message, "the message should show how to fix it"


def test_blank_env_var_counts_as_missing(monkeypatch):
    """`export SNOWFLAKE_PASSWORD=` is a typo, not an empty password.

    Treating blank as set produces an authentication failure that sends the
    operator hunting through Snowflake's access logs instead of at their own
    shell, which is a long way to travel for a missing character.
    """
    for name in REQUIRED_ENV_VARS:
        monkeypatch.setenv(name, "placeholder")
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "   ")

    assert missing_env_vars() == ["SNOWFLAKE_WAREHOUSE"]


def test_config_summary_never_echoes_the_password(monkeypatch):
    """describe_config prints at the top of every Snowflake run.

    It has to show enough for the operator to confirm which account and
    database a regulatory tie-out is about to write to, without ever putting
    the secret itself on a terminal, in a log, or in a screenshot.
    """
    for name in REQUIRED_ENV_VARS:
        monkeypatch.setenv(name, f"value-for-{name}")
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "hunter2-do-not-print")

    summary = describe_config()

    assert "hunter2-do-not-print" not in summary
    assert "set" in summary
    # The non-secret settings ARE shown - that is the point of the summary.
    assert "value-for-SNOWFLAKE_ACCOUNT" in summary
    assert "value-for-SNOWFLAKE_DATABASE" in summary


def test_optional_role_is_not_required(monkeypatch):
    for name in REQUIRED_ENV_VARS:
        monkeypatch.setenv(name, "x")
    for name in OPTIONAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SNOWFLAKE_PRIVATE_KEY_FILE", raising=False)

    assert missing_env_vars() == []
    check_env()  # must not raise


# ===========================================================================
# Backend selection - no Snowflake account required
# ===========================================================================
@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "on"])
def test_use_snowflake_accepts_the_usual_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv("USE_SNOWFLAKE", value)
    assert backends.use_snowflake() is True
    assert backends.backend_name() == "Snowflake"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", " "])
def test_anything_else_selects_sqlite(monkeypatch, value):
    """Unrecognised values fall back to SQLite, never to Snowflake.

    The default has to be the local backend. A mistyped variable that silently
    pointed a run at a live warehouse would be a costly kind of typo, and
    failing safe means failing local.
    """
    monkeypatch.setenv("USE_SNOWFLAKE", value)
    assert backends.use_snowflake() is False
    assert backends.backend_name() == "SQLite"


def test_unset_use_snowflake_selects_sqlite(monkeypatch):
    monkeypatch.delenv("USE_SNOWFLAKE", raising=False)
    assert backends.use_snowflake() is False


# ===========================================================================
# SQL file resolution - no Snowflake account required
# ===========================================================================
def sql_body(path: Path) -> str:
    """The executable SQL with comment lines removed.

    Needed because both files DISCUSS FULL OUTER JOIN in their header comments -
    the SQLite one explains at length why it cannot use one. Searching the raw
    text for the phrase therefore matches the prose in both files and proves
    nothing. Only the statements themselves count.
    """
    return "\n".join(
        line for line in path.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def test_snowflake_run_prefers_the_native_rewrite():
    path = backends.resolve_query_path("02_gl_vs_regulatory_extract", snowflake=True)
    assert path.parent == SNOWFLAKE_SQL_DIR
    assert "FULL OUTER JOIN" in sql_body(path)


def test_sqlite_run_never_picks_up_the_native_rewrite():
    """The SQLite path must get SQL that older SQLite builds can actually parse.

    FULL OUTER JOIN only became available in SQLite 3.39, and Python's bundled
    sqlite3 version varies by platform and distribution, so the shared file has
    to stay on the simulated form to remain portable.
    """
    path = backends.resolve_query_path("02_gl_vs_regulatory_extract", snowflake=False)
    assert path.parent == SQL_DIR
    assert "FULL OUTER JOIN" not in sql_body(path)
    assert "UNION" in sql_body(path)


@pytest.mark.parametrize("stem", ["01_gl_vs_subledger", "03_quarterly_break_trend"])
def test_portable_queries_are_one_file_shared_by_both_backends(stem):
    """Queries 1 and 3 must resolve to the SAME file on both engines.

    They are portable ANSI, so forking them would buy nothing and cost the
    guarantee that both backends apply identical break logic. Two copies of a
    materiality rule is two materiality rules, eventually.
    """
    assert (
        backends.resolve_query_path(stem, snowflake=True)
        == backends.resolve_query_path(stem, snowflake=False)
        == SQL_DIR / f"{stem}.sql"
    )


# ===========================================================================
# Column casing - no Snowflake account required
# ===========================================================================
def test_normalize_columns_lowercases_snowflake_style_labels():
    """The single most likely runtime failure when switching backends.

    Snowflake returns ENTITY where SQLite returns entity, so every
    frame["entity"] in the dashboard would raise KeyError on one engine. This
    is where that gets reconciled, once, for both.
    """
    snowflake_style = pd.DataFrame(
        {"ENTITY": ["US Retail Bank"], "GL_BALANCE": [1.0], "STATUS": ["BREAK"]}
    )
    normalised = backends.normalize_columns(snowflake_style)

    assert list(normalised.columns) == ["entity", "gl_balance", "status"]
    assert normalised["entity"].iloc[0] == "US Retail Bank"


def test_normalize_columns_leaves_sqlite_style_labels_alone():
    """Idempotent, so it is safe on every result from either backend."""
    sqlite_style = pd.DataFrame({"entity": ["x"], "gl_balance": [1.0]})
    assert list(backends.normalize_columns(sqlite_style).columns) == [
        "entity", "gl_balance",
    ]


# ===========================================================================
# The native rewrite is semantically identical - no Snowflake account required
# ===========================================================================
@pytest.mark.skipif(
    not SQLITE_HAS_FULL_OUTER,
    reason=f"SQLite {sqlite3.sqlite_version} predates FULL OUTER JOIN (3.39)",
)
def test_native_full_outer_join_matches_the_simulated_version(conn):
    """The two dialects of query 2 must produce identical results.

    Worth being precise about what this does and does not prove. SQLite gained
    FULL OUTER JOIN in 3.39, so where the local build is new enough the
    Snowflake-dialect file can be parsed and executed locally. That makes this a
    real check on the LOGIC of the rewrite - the COALESCE on the join keys, the
    presence flags, the offset search - against the simulated version on the
    same data, and it would catch the classic mistake of reading gl.entity
    straight out of a full outer join and silently dropping every extract-only
    row.

    What it does not prove is Snowflake-specific behaviour: NUMBER(18,2)
    arithmetic, micro-partition pruning, or identifier casing. Those need the
    live tests below. Two backends agreeing on a laptop is not the same as a
    warehouse agreeing, and this test should not be read as if it were.
    """
    simulated = pd.read_sql_query(
        (SQL_DIR / "02_gl_vs_regulatory_extract.sql").read_text(), conn
    )
    native = pd.read_sql_query(
        (SNOWFLAKE_SQL_DIR / "02_gl_vs_regulatory_extract_snowflake.sql").read_text(),
        conn,
    )

    assert list(simulated.columns) == list(native.columns), (
        "the two dialects do not share an output contract"
    )

    key = ["entity", "product", "quarter"]
    pd.testing.assert_frame_equal(
        simulated.sort_values(key).reset_index(drop=True),
        native.sort_values(key).reset_index(drop=True),
    )


# ===========================================================================
# Live Snowflake - skipped without credentials
# ===========================================================================
@requires_snowflake
def test_live_connection_reaches_the_configured_account():
    from src.snowflake_conn import connection

    with connection() as conn:
        cursor = conn.cursor()
        try:
            row = cursor.execute(
                "SELECT CURRENT_ACCOUNT(), CURRENT_DATABASE(), CURRENT_SCHEMA(), "
                "CURRENT_WAREHOUSE()"
            ).fetchone()
        finally:
            cursor.close()

    assert all(value is not None for value in row), (
        f"connected but the session is not fully resolved: {row}. "
        "Check SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA and SNOWFLAKE_WAREHOUSE."
    )
    assert row[1].upper() == os.environ["SNOWFLAKE_DATABASE"].strip().upper()


@requires_snowflake
def test_live_tables_hold_the_same_row_counts_as_the_csvs(source_frames):
    """Real rows came back, and exactly as many as the CSVs held.

    This is the assertion that separates "the Snowflake code compiles" from
    "the Snowflake pipeline works". A backend that connects, runs, and returns
    an empty result set would pass every other test in this file.
    """
    for table, frame in source_frames.items():
        result = backends.run_snowflake(f"SELECT COUNT(*) AS n FROM {table}")
        assert int(result["n"].iloc[0]) == len(frame), (
            f"{table}: Snowflake has {int(result['n'].iloc[0])} rows, "
            f"CSV has {len(frame)}"
        )


@requires_snowflake
def test_live_results_come_back_with_lowercase_columns():
    """Proves the casing fix against the real thing.

    Snowflake genuinely returns uppercase labels; a mock cannot demonstrate
    that the normalisation is wired into the live path. Asserting lowercase
    here is what makes the dashboard's backend-agnostic column access safe.
    """
    frame = backends.run_snowflake(
        "SELECT entity, product, quarter, gl_balance FROM general_ledger LIMIT 5"
    )
    assert list(frame.columns) == ["entity", "product", "quarter", "gl_balance"]
    assert len(frame) == 5


@requires_snowflake
def test_live_reconciliations_find_exactly_the_planted_breaks(answer_key):
    """The end-to-end claim: the same 14 breaks, on the live warehouse.

    Same grading standard as the SQLite suite - identity against the answer
    key, not counts. If the two backends ever disagree about which accounts
    broke, one of them is wrong about a regulatory number, and that has to be a
    test failure rather than something noticed later in a dashboard.
    """
    subledger = backends.run_snowflake(
        backends.resolve_query_path("01_gl_vs_subledger", snowflake=True).read_text()
    )
    extract = backends.run_snowflake(
        backends.resolve_query_path(
            "02_gl_vs_regulatory_extract", snowflake=True
        ).read_text()
    )

    def keys(frame):
        breaks = frame[frame["status"] == "BREAK"]
        return set(
            breaks[["entity", "product", "quarter"]].itertuples(index=False, name=None)
        )

    def expected(detected_by):
        rows = answer_key[answer_key["detected_by"] == detected_by]
        return set(
            rows[["entity", "product", "quarter"]].itertuples(index=False, name=None)
        )

    assert keys(subledger) == expected("gl_vs_subledger")
    assert keys(extract) == expected("gl_vs_regulatory_extract")
    assert len(keys(subledger)) + len(keys(extract)) == 14


@requires_snowflake
def test_live_clean_accounts_reconcile_to_exactly_zero(answer_key):
    """NUMBER(18,2) should make this even more solidly true than on SQLite.

    Snowflake stores balances as exact fixed-point decimals rather than binary
    floats, so a clean account that ties out to the cent has no representation
    error to hide behind at all.
    """
    subledger = backends.run_snowflake(
        backends.resolve_query_path("01_gl_vs_subledger", snowflake=True).read_text()
    )
    planted = set(
        answer_key[["entity", "product", "quarter"]].itertuples(index=False, name=None)
    )
    clean = subledger[
        ~subledger[["entity", "product", "quarter"]].apply(tuple, axis=1).isin(planted)
    ]

    assert len(clean) == 46
    assert (clean["variance"].astype(float) == 0.0).all()


# ===========================================================================
# CLI behaviour on a misconfigured environment - no Snowflake account required
# ===========================================================================
@pytest.mark.parametrize(
    "script", ["src/build_snowflake.py", "src/run_queries_snowflake.py"]
)
def test_cli_reports_missing_vars_without_a_traceback(script, monkeypatch):
    """A missing credential must print the list, not 12 frames of internals.

    This is an operator problem with a known remedy, not a defect in the code,
    and a stack trace above the one line that matters actively buries the
    answer. The first version of these scripts DID emit a traceback - the
    helpful message was there, with a wall of contextlib and connector frames
    printed after it.

    Asserts three things: the exit status is a clean failure, every missing
    variable is named, and the word Traceback appears nowhere. The last one is
    the assertion that would have caught the original bug.
    """
    import subprocess

    env = {
        key: value for key, value in os.environ.items()
        if key not in REQUIRED_ENV_VARS
    }
    env["PATH"] = os.environ.get("PATH", "")

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / script)],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
    )
    combined = result.stdout + result.stderr

    assert result.returncode == 1, "a config error should exit 1, not 0 or a crash code"
    assert "Traceback" not in combined, (
        f"{script} printed a stack trace for a configuration problem:\n{combined}"
    )
    for name in REQUIRED_ENV_VARS:
        assert name in combined, f"{script} did not name {name}"
    assert "export" in combined, "the message should show how to fix it"


@pytest.mark.parametrize(
    "script", ["src/build_snowflake.py", "src/run_queries_snowflake.py"]
)
def test_cli_never_prints_a_credential_value(script, monkeypatch):
    """Even a fully-configured run must not echo the password.

    The credentials here are deliberately bogus, so the run fails at connection
    time rather than reaching Snowflake. That is the point: the failure path is
    exactly where a careless implementation would dump the connection kwargs -
    password included - into a log or a terminal someone screenshots.
    """
    import subprocess

    env = dict(os.environ)
    env.update({name: f"bogus-{name.lower()}" for name in REQUIRED_ENV_VARS})
    env["SNOWFLAKE_PASSWORD"] = "correct-horse-battery-staple"

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / script)],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    combined = result.stdout + result.stderr

    assert "correct-horse-battery-staple" not in combined, (
        f"{script} leaked the password into its output"
    )
