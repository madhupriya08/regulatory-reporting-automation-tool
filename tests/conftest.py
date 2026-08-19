"""Shared fixtures for the reconciliation test suite.

Every test runs against a database built fresh into a pytest temp directory
from the committed CSVs. It deliberately does NOT reuse the developer's
regulatory_reporting.db in the repo root: that file is gitignored, may be
stale, half-built or hand-edited, and a test suite that silently grades a
different database than the one in version control is worse than no suite.
Building it here makes the loader itself part of what is under test.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from config import ANSWER_KEY_CSV, SQL_DIR, TABLE_SOURCES  # noqa: E402
from src.build_database import build  # noqa: E402


@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> Path:
    """A SQLite database built from the committed CSVs, once per session."""
    path = tmp_path_factory.mktemp("recon") / "test_regulatory_reporting.db"
    build(path)
    return path


@pytest.fixture(scope="session")
def conn(db_path: Path):
    connection = sqlite3.connect(db_path)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def answer_key() -> pd.DataFrame:
    """The planted breaks. This is the grading key, not an output."""
    return pd.read_csv(ANSWER_KEY_CSV)


@pytest.fixture(scope="session")
def source_frames() -> dict[str, pd.DataFrame]:
    return {name: pd.read_csv(path) for name, path in TABLE_SOURCES.items()}


def _run(conn, filename: str) -> pd.DataFrame:
    return pd.read_sql_query((SQL_DIR / filename).read_text(), conn)


@pytest.fixture(scope="session")
def subledger_recon(conn) -> pd.DataFrame:
    return _run(conn, "01_gl_vs_subledger.sql")


@pytest.fixture(scope="session")
def extract_recon(conn) -> pd.DataFrame:
    return _run(conn, "02_gl_vs_regulatory_extract.sql")


@pytest.fixture(scope="session")
def trend_recon(conn) -> pd.DataFrame:
    return _run(conn, "03_quarterly_break_trend.sql")


def account_keys(frame: pd.DataFrame) -> set[tuple[str, str, str]]:
    """Reduce a result set to its entity/product/quarter identity set."""
    return set(
        frame[["entity", "product", "quarter"]]
        .itertuples(index=False, name=None)
    )
