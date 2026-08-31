"""Tests for the window-function analytics layer (sql/04_break_analytics.sql).

These grade a different property than the other suites. Queries 1 and 2 are
graded on WHICH accounts they flag. This query flags nothing new - it re-reads
the same 14 breaks and ranks them - so what has to be correct here is the
arithmetic of the windows and, just as importantly, its determinism.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import SQL_DIR  # noqa: E402

SQLITE_HAS_WINDOWS = sqlite3.sqlite_version_info >= (3, 25, 0)

pytestmark = pytest.mark.skipif(
    not SQLITE_HAS_WINDOWS,
    reason=f"SQLite {sqlite3.sqlite_version} predates window functions (3.25)",
)


@pytest.fixture(scope="module")
def analytics(conn) -> pd.DataFrame:
    return pd.read_sql_query((SQL_DIR / "04_break_analytics.sql").read_text(), conn)


def test_one_row_per_break(analytics, answer_key):
    """The analytics layer reports every break exactly once - it is a lens on
    the same 14, not a source of new findings."""
    assert len(analytics) == len(answer_key) == 14


def test_rank_one_is_the_largest_break_in_its_quarter(analytics):
    """Rank 1 is where a controller starts, so it had better be the top break."""
    for quarter, group in analytics.groupby("quarter"):
        top = group[group["materiality_rank"] == 1]
        assert not top.empty
        assert top["break_amount"].iloc[0] == group["break_amount"].max()


def test_equal_breaks_share_a_rank(analytics):
    """RANK, not ROW_NUMBER.

    The two sides of a mapping error are equal in size and are one problem.
    Inventing an order between them would tell the reader something untrue, so
    the query uses RANK and they share a position.
    """
    for (_, amount), group in analytics.groupby(["quarter", "break_amount"]):
        assert group["materiality_rank"].nunique() == 1, (
            f"breaks of equal size {amount} were given different ranks"
        )

    # And this dataset genuinely contains such a tie, so the assertion above is
    # actually exercised rather than vacuously true.
    assert analytics.duplicated(["quarter", "break_amount"]).any(), (
        "no tied break amounts in the data - the RANK behaviour is untested"
    )


def test_quarter_shares_sum_to_one_hundred(analytics):
    for quarter, group in analytics.groupby("quarter"):
        total = group["pct_of_quarter_exposure"].sum()
        assert total == pytest.approx(100.0, abs=0.05), (
            f"{quarter}: shares sum to {total}, not 100"
        )
        assert group["cumulative_pct_of_exposure"].max() == pytest.approx(100.0, abs=0.05)


def test_cumulative_share_is_monotonic(analytics):
    """A running total that ever decreases means the frame is mis-specified."""
    for quarter, group in analytics.groupby("quarter"):
        ordered = group.sort_values("cumulative_pct_of_exposure")
        assert ordered["cumulative_pct_of_exposure"].is_monotonic_increasing


def test_priority_one_really_does_cover_eighty_percent(analytics):
    """The label PRIORITY_1_TOP_80PCT has to be true.

    This is the test that caught the original off-by-one. Filtering on
    `cumulative <= 80` reads naturally and is wrong: it drops the very row that
    carries the total past 80%, so the bucket claiming to be the top 80%
    reliably added up to less. The fix subtracts each row's own share before
    comparing, which includes the crossing row.
    """
    for quarter, group in analytics.groupby("quarter"):
        priority_one = group[group["remediation_priority"] == "PRIORITY_1_TOP_80PCT"]
        covered = 100.0 * priority_one["break_amount"].sum() / group["break_amount"].sum()
        assert covered >= 80.0, (
            f"{quarter}: PRIORITY_1 covers only {covered:.2f}% - the label is a lie"
        )
        # And it must be the SMALLEST such set: dropping its last row must
        # fall below 80%, or the bucket is padded.
        trimmed = priority_one.nlargest(len(priority_one) - 1, "break_amount")
        under = 100.0 * trimmed["break_amount"].sum() / group["break_amount"].sum()
        assert under < 80.0, (
            f"{quarter}: PRIORITY_1 is larger than it needs to be"
        )


def test_recurring_break_is_identified(analytics):
    """The same account breaking twice must read RECURRING, not two NEWs.

    One missed cutoff is an incident; the same feed missing it two quarters
    running is a broken process, and they go to different owners. The data
    contains exactly one such account by design.
    """
    recurring = analytics[analytics["break_persistence"] == "RECURRING"]
    assert not recurring.empty, "no recurring break detected - the window is not working"

    accounts = set(recurring[["entity", "product"]].itertuples(index=False, name=None))
    assert accounts == {("US Wealth Mgmt", "HELOC")}
    assert len(recurring) == 2, "the recurring account should appear in both quarters"
    assert set(recurring["quarter"]) == {"2025Q1", "2025Q2"}


def test_trend_direction_is_correct(analytics):
    """The recurring break grew, so it must read WORSENING in the later quarter.

    A recurring break that is shrinking means remediation is working; one that
    is growing means it is not. Getting this backwards would tell a controller
    to stand down on the one break that is actually accelerating.
    """
    heloc = analytics[
        (analytics["entity"] == "US Wealth Mgmt") & (analytics["product"] == "HELOC")
    ].set_index("quarter")

    assert heloc.loc["2025Q1", "trend_vs_prior_quarter"] == "FIRST_OCCURRENCE"
    assert heloc.loc["2025Q2", "trend_vs_prior_quarter"] == "WORSENING"
    assert abs(heloc.loc["2025Q2", "variance"]) > abs(heloc.loc["2025Q1", "variance"])


def test_first_occurrences_have_no_prior_quarter(analytics):
    first = analytics[analytics["trend_vs_prior_quarter"] == "FIRST_OCCURRENCE"]
    assert first["prior_quarter_variance"].isna().all()

    later = analytics[analytics["trend_vs_prior_quarter"] != "FIRST_OCCURRENCE"]
    assert later["prior_quarter_variance"].notna().all()


def test_results_are_deterministic_across_runs(conn):
    """Ties must not make the output wobble.

    A mapping error produces two breaks of EXACTLY equal magnitude. Ordering
    the running-total frame by break_amount alone leaves the engine free to
    place tied rows in either order, so each would get a different cumulative
    share from run to run - and, worse, between SQLite and Snowflake. A
    regulatory figure that changes depending on which engine printed it is not
    a figure. The fix is explicit tie-breakers in the window's ORDER BY; this
    test is what stops them being removed as noise.
    """
    sql = (SQL_DIR / "04_break_analytics.sql").read_text()
    first = pd.read_sql_query(sql, conn)
    second = pd.read_sql_query(sql, conn)
    pd.testing.assert_frame_equal(first, second)

    # The tied rows must receive DIFFERENT cumulative shares (they are two
    # rows in a running total), and which row gets which must be stable.
    tied = first[first.duplicated("break_amount", keep=False)]
    assert not tied.empty
    assert tied["cumulative_pct_of_exposure"].nunique() > 1


def test_analytics_query_is_portable_to_snowflake():
    """No SQLite-only syntax, and the Snowflake reserved-word trap avoided."""
    body = "\n".join(
        line for line in (SQL_DIR / "04_break_analytics.sql").read_text().splitlines()
        if not line.lstrip().startswith("--")
    )
    assert "OVER" in body and "PARTITION BY" in body

    # VARIANCE is a built-in aggregate in Snowflake, so no CTE may be named
    # exactly that - it invites the parser to resolve the wrong thing. The
    # rule is about the bare name, not about any particular replacement:
    # queries 1 and 2 happen to use variance_calc, this one uses
    # subledger_variance and extract_variance. All are fine; `variance` is not.
    cte_names = re.findall(r"(?m)^\s*(\w+)\s+AS\s*\(", body)
    assert cte_names, "no CTEs found - the regex is not matching this file"
    assert "variance" not in [name.lower() for name in cte_names], (
        f"a CTE is named `variance`, which collides with Snowflake's built-in "
        f"aggregate. CTEs found: {cte_names}"
    )
