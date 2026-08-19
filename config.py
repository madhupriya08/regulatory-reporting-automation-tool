"""Shared configuration for the Regulatory Reporting Automation Tool.

Every module (generator, loaders, runners, dashboard, tests) imports its
constants from here so that there is exactly one definition of the reporting
universe and one definition of materiality. If the threshold lived in three
places it would drift, and a reconciliation tool whose threshold drifts is
worse than no tool at all.
"""

from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
SQL_DIR = PROJECT_ROOT / "sql"
SNOWFLAKE_SQL_DIR = SQL_DIR / "snowflake"
OUTPUT_DIR = PROJECT_ROOT / "output"
SQLITE_DB_PATH = PROJECT_ROOT / "regulatory_reporting.db"

GENERAL_LEDGER_CSV = DATA_DIR / "general_ledger.csv"
SUB_LEDGER_CSV = DATA_DIR / "sub_ledger.csv"
REGULATORY_EXTRACT_CSV = DATA_DIR / "regulatory_extract.csv"
ANSWER_KEY_CSV = DATA_DIR / "answer_key.csv"

# Table name -> source CSV. Used by both the SQLite and the Snowflake builder
# so the two backends can never diverge on what gets loaded.
TABLE_SOURCES = {
    "general_ledger": GENERAL_LEDGER_CSV,
    "sub_ledger": SUB_LEDGER_CSV,
    "regulatory_extract": REGULATORY_EXTRACT_CSV,
}

# --------------------------------------------------------------------------
# Reporting universe
# --------------------------------------------------------------------------
ENTITIES = [
    "US Retail Bank",
    "US Wealth Mgmt",
    "US Card Services",
    "US Small Business Bank",
    "US Private Bank",
]

PRODUCTS = [
    "Mortgage",
    "Auto Loan",
    "Credit Card",
    "Personal Loan",
    "HELOC",
    "Small Business Loan",
]

QUARTERS = ["2025Q1", "2025Q2"]

# --------------------------------------------------------------------------
# Materiality
# --------------------------------------------------------------------------
# A GL-to-sub-ledger variance is only escalated when it exceeds 0.5% of the
# GL balance. See the README for why 0.5% and not 0% or 1%: a hard zero
# threshold turns every rounding artifact into a "break" and buries the real
# ones, while 1% is loose enough to let a genuine mis-load through on a large
# portfolio. 0.5% is the conventional working tolerance for a schedule-level
# tie-out of this kind.
#
# NOTE: this value is also hardcoded in the .sql files. SQLite and Snowflake
# have no portable way to bind a parameter into a plain .sql file that both
# the runner and a human running the query by hand would honour, so the SQL
# is the source of truth for the engines and this constant is the source of
# truth for Python. The tests assert the two agree.
MATERIALITY_THRESHOLD_PCT = 0.5

# --------------------------------------------------------------------------
# Data generation
# --------------------------------------------------------------------------
# Fixed seed: the answer key must describe the exact CSVs in the repo, so
# regeneration has to be reproducible bit for bit.
RANDOM_SEED = 20250819
