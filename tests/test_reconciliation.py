"""Grade the reconciliation SQL against the planted answer key.

The distinction this file exists to enforce: "the query ran" is not the same
claim as "the query is correct". A reconciliation that flags 40 accounts finds
all 14 real breaks too, and is useless. A reconciliation that flags 7 accounts
looks disciplined and may have found the wrong 7.

So every assertion here is about EXACT agreement with the answer key - the same
accounts, no more and no fewer - not about counts alone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MATERIALITY_THRESHOLD_PCT, SQL_DIR  # noqa: E402
from tests.conftest import account_keys  # noqa: E402


def planted(answer_key, detected_by: str):
    return account_keys(answer_key[answer_key["detected_by"] == detected_by])


def flagged(recon):
    return account_keys(recon[recon["status"] == "BREAK"])


# ===========================================================================
# 1. GL vs sub-ledger finds exactly the planted sub-ledger breaks
# ===========================================================================
def test_subledger_recon_finds_exactly_the_planted_breaks(subledger_recon, answer_key):
    expected = planted(answer_key, "gl_vs_subledger")
    actual = flagged(subledger_recon)

    missed = expected - actual        # false negatives: a real break went unreported
    spurious = actual - expected      # false positives: a clean account was flagged

    assert not missed, f"planted breaks the query failed to detect: {sorted(missed)}"
    assert not spurious, f"clean accounts the query wrongly flagged: {sorted(spurious)}"
    assert len(actual) == len(expected) == 7


def test_subledger_recon_reports_no_extra_break_rows(subledger_recon, answer_key):
    """Row-level count, not just set identity.

    Set equality would still pass if the query emitted the same account twice -
    a duplicated row inflates every downstream dollar total while the account
    set looks perfect.
    """
    assert (subledger_recon["status"] == "BREAK").sum() == len(
        answer_key[answer_key["detected_by"] == "gl_vs_subledger"]
    )


# ===========================================================================
# 2. GL vs regulatory extract finds exactly the planted extract breaks
# ===========================================================================
def test_extract_recon_finds_exactly_the_planted_breaks(extract_recon, answer_key):
    expected = planted(answer_key, "gl_vs_regulatory_extract")
    actual = flagged(extract_recon)

    missed = expected - actual
    spurious = actual - expected

    assert not missed, f"planted breaks the query failed to detect: {sorted(missed)}"
    assert not spurious, f"clean accounts the query wrongly flagged: {sorted(spurious)}"
    assert len(actual) == len(expected) == 7


def test_full_outer_join_simulation_returns_the_whole_population(extract_recon):
    """The simulated FULL OUTER JOIN loses no rows.

    The union of two LEFT JOINs is only equivalent to a FULL OUTER JOIN if the
    anti-join predicate on the second branch is right. Get it wrong one way and
    matched rows are emitted twice; wrong the other way and one-sided rows
    vanish - and a vanished row is a balance that was never filed, silently
    dropped by the tool meant to find it. Both failure modes change the row
    count, so the count is the assertion.
    """
    assert len(extract_recon) == 60
    assert not extract_recon.duplicated(["entity", "product", "quarter"]).any()


# ===========================================================================
# 3. Clean accounts stay under materiality
# ===========================================================================
@pytest.mark.parametrize("recon_name", ["subledger_recon", "extract_recon"])
def test_clean_accounts_stay_under_materiality(request, recon_name, answer_key):
    """No unplanted account exceeds the 0.5% threshold in either tie-out."""
    recon = request.getfixturevalue(recon_name)
    planted_accounts = account_keys(answer_key)

    clean = recon[
        ~recon[["entity", "product", "quarter"]]
        .apply(tuple, axis=1)
        .isin(planted_accounts)
    ]

    # 60 accounts, 14 planted anywhere. Accounts planted in the OTHER tie-out
    # are excluded too: they are clean here, but treating them as clean would
    # let a genuine cross-contamination bug - an extract break leaking into the
    # sub-ledger tie-out - pass unnoticed. Grading only the 46 never-touched
    # accounts keeps the two reconciliations provably independent.
    assert len(clean) == 46, "unexpected number of never-planted accounts"
    worst = clean["variance_pct"].abs().max()
    assert worst <= MATERIALITY_THRESHOLD_PCT, (
        f"a clean account drifted to {worst}%, above the "
        f"{MATERIALITY_THRESHOLD_PCT}% threshold"
    )
    assert (clean["status"] == "PASS").all()


@pytest.mark.parametrize("recon_name", ["subledger_recon", "extract_recon"])
def test_clean_accounts_reconcile_to_exactly_zero(request, recon_name, answer_key):
    """The strong form of the previous test, and the point of the whole
    exact-apportionment fix in the generator.

    Clean accounts do not merely sit *under* 0.5% - they sit at exactly
    0.00 dollars and 0.0000%. That is only true because the generator
    apportions the GL balance across loan weights in integer cents instead of
    drawing independent random amounts and hoping the sum lands close.

    This test is what would fail first if that fix were ever reverted, and it
    would fail with an obvious message rather than as an intermittent phantom
    break somewhere in the break count. Asserting the weaker "under 0.5%"
    property alone would let the noise creep back until it happened to cross
    the threshold on some future seed.
    """
    recon = request.getfixturevalue(recon_name)
    planted_accounts = account_keys(answer_key)

    clean = recon[
        ~recon[["entity", "product", "quarter"]]
        .apply(tuple, axis=1)
        .isin(planted_accounts)
    ]

    assert (clean["variance"] == 0.0).all(), (
        "clean accounts carry non-zero variance - the exact-apportionment "
        "guarantee in src/generate_data.py has been broken:\n"
        f"{clean[clean['variance'] != 0.0][['entity', 'product', 'quarter', 'variance']]}"
    )
    assert (clean["variance_pct"] == 0.0).all()


# ===========================================================================
# 4. Root-cause labels are directionally correct
# ===========================================================================
def test_subledger_root_causes_match_the_planted_failure_mode(subledger_recon, answer_key):
    """Under the GL must read LATE_FEED, over must read DUPLICATE_LOAD.

    Direction is the whole diagnostic, so a label that disagrees with the sign
    of its own variance is a contradiction the report must never print - it
    would send an analyst to chase a replayed batch that does not exist.
    """
    expected_cause = {
        (r.entity, r.product, r.quarter): r.expected_root_cause
        for r in answer_key.itertuples()
        if r.detected_by == "gl_vs_subledger"
    }

    breaks = subledger_recon[subledger_recon["status"] == "BREAK"]
    for row in breaks.itertuples():
        key = (row.entity, row.product, row.quarter)
        assert row.root_cause == expected_cause[key], (
            f"{key}: labelled {row.root_cause}, planted as {expected_cause[key]}"
        )
        if row.root_cause == "LATE_FEED":
            assert row.variance < 0, f"{key}: LATE_FEED with a positive variance"
        if row.root_cause == "DUPLICATE_LOAD":
            assert row.variance > 0, f"{key}: DUPLICATE_LOAD with a negative variance"


def test_duplicate_loads_come_with_duplicated_loan_ids(subledger_recon):
    """Corroboration, not restatement of the same signal.

    The root cause is derived from variance direction alone. duplicate_loan_ids
    is computed independently, straight from COUNT(*) minus COUNT(DISTINCT).
    A replayed batch re-inserts the same loan_ids, so the two signals agreeing
    is real evidence the direction heuristic named the right cause - and this
    test is what would catch a positive variance caused by something other
    than a replay being mislabelled as one.
    """
    dupes = subledger_recon[subledger_recon["root_cause"] == "DUPLICATE_LOAD"]
    assert not dupes.empty
    assert (dupes["duplicate_loan_ids"] > 0).all(), (
        "an account labelled DUPLICATE_LOAD contains no duplicated loan_ids"
    )

    others = subledger_recon[subledger_recon["root_cause"] != "DUPLICATE_LOAD"]
    assert (others["duplicate_loan_ids"] == 0).all(), (
        "duplicated loan_ids found on an account not labelled DUPLICATE_LOAD"
    )


def test_extract_root_causes_match_the_planted_failure_mode(extract_recon, answer_key):
    """Mapping errors and timing differences are told apart correctly.

    Both present as 'the extract disagrees with the GL'. The query separates
    them by whether an equal-and-opposite partner exists in the same entity and
    quarter. This asserts it reached the right conclusion on all seven, which
    is the single most likely place for the logic to be subtly wrong.
    """
    expected_cause = {
        (r.entity, r.product, r.quarter): r.expected_root_cause
        for r in answer_key.itertuples()
        if r.detected_by == "gl_vs_regulatory_extract"
    }

    breaks = extract_recon[extract_recon["status"] == "BREAK"]
    for row in breaks.itertuples():
        key = (row.entity, row.product, row.quarter)
        assert row.root_cause == expected_cause[key], (
            f"{key}: labelled {row.root_cause}, planted as {expected_cause[key]}"
        )


def test_mapping_errors_name_their_offsetting_product(extract_recon, answer_key):
    """Every MAPPING_ERROR points at its partner, and the partner points back.

    A mapping error is a claim that the balance moved somewhere specific. If the
    query can label the cause but not name the destination, it has pattern-
    matched rather than reasoned, and an analyst cannot act on the finding.
    """
    mapping = extract_recon[extract_recon["root_cause"] == "MAPPING_ERROR"]
    assert len(mapping) == 4, "expected two mapping-error pairs"
    assert mapping["offsetting_product"].notna().all()

    by_key = {(r.entity, r.product, r.quarter): r for r in mapping.itertuples()}
    for row in mapping.itertuples():
        partner_key = (row.entity, row.offsetting_product, row.quarter)
        assert partner_key in by_key, f"{row.product} names a partner that is not itself a break"
        partner = by_key[partner_key]
        assert partner.offsetting_product == row.product, "pairing is not symmetric"
        assert round(row.variance + partner.variance, 2) == 0.0, "pair does not net to zero"


def test_timing_differences_are_one_sided(extract_recon):
    """A timing difference means the money never made the filing.

    So it must have no offsetting partner - if it had one, the balance is still
    inside the entity and the correct label is MAPPING_ERROR. It must also read
    UNDER the GL, because the failure mode is an adjustment that posted after
    the extract ran.
    """
    timing = extract_recon[extract_recon["root_cause"] == "TIMING_DIFFERENCE"]
    assert len(timing) == 3
    assert timing["offsetting_product"].isna().all(), (
        "a TIMING_DIFFERENCE has an offsetting partner - it is a mapping error"
    )
    assert (timing["variance"] < 0).all(), (
        "a TIMING_DIFFERENCE shows the extract OVER the GL, which the failure "
        "mode cannot produce"
    )


# ===========================================================================
# 5. The trend query agrees with the detail queries
# ===========================================================================
def test_trend_matches_the_detail_queries(trend_recon, subledger_recon, extract_recon):
    """The governance view and the operational view cannot disagree.

    Query 3 restates the variance logic instead of reading queries 1 and 2, so
    nothing structural stops the three from drifting apart. This test is the
    thing that stops it: a CFO reading the trend and a controller reading the
    detail must be looking at the same breaks.
    """
    sub_breaks = subledger_recon[subledger_recon["status"] == "BREAK"]
    ext_breaks = extract_recon[extract_recon["status"] == "BREAK"]

    for row in trend_recon.itertuples():
        q = row.quarter
        assert row.subledger_breaks == (sub_breaks["quarter"] == q).sum()
        assert row.extract_breaks == (ext_breaks["quarter"] == q).sum()

        distinct = set(
            sub_breaks[sub_breaks["quarter"] == q][["entity", "product"]]
            .itertuples(index=False, name=None)
        ) | set(
            ext_breaks[ext_breaks["quarter"] == q][["entity", "product"]]
            .itertuples(index=False, name=None)
        )
        assert row.accounts_with_breaks == len(distinct)

        expected_amount = (
            sub_breaks[sub_breaks["quarter"] == q]["variance"].abs().sum()
            + ext_breaks[ext_breaks["quarter"] == q]["variance"].abs().sum()
        )
        assert row.total_break_amount == pytest.approx(expected_amount, abs=0.02)


def test_trend_accounts_for_every_planted_break(trend_recon, answer_key):
    """All 14 planted breaks are represented in the trend, none invented."""
    assert (
        trend_recon["subledger_breaks"].sum() + trend_recon["extract_breaks"].sum()
        == len(answer_key)
        == 14
    )
    assert (trend_recon["break_rate_pct"] <= 100).all()
    assert (trend_recon["total_accounts"] == 30).all()

def test_full_outer_join_branches_are_disjoint(conn):
    """The two UNION branches of sql/02 must not overlap.

    Written after a mutation test embarrassed the comment in that file:
    deleting the anti-join predicate changed nothing, because UNION's set
    semantics collapse the re-emitted matched rows anyway. Nothing in the suite
    was actually testing that predicate.

    It still matters. The moment anyone swaps UNION for the cheaper UNION ALL
    to skip the dedup sort, a missing anti-join silently doubles every matched
    account and every dollar total with it.

    So this reads the real query file, strips its comments, rewrites its one
    UNION to UNION ALL, and runs THAT. The rewrite is the point: with the
    anti-join in place the branches are disjoint and the count is unchanged at
    60; with it removed the matched rows are emitted twice and the count jumps
    to 120. Deriving the SQL from the file rather than restating it inline is
    what makes this test track sql/02 instead of a copy of it that can drift.
    """
    raw = (SQL_DIR / "02_gl_vs_regulatory_extract.sql").read_text()

    # Comments mention UNION in prose; strip them so only the keyword is left.
    body = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("--")
    )
    body, substitutions = re.subn(r"(?m)^(\s*)UNION\s*$", r"\1UNION ALL", body)
    assert substitutions == 1, (
        f"expected exactly one UNION keyword in sql/02, found {substitutions}"
    )

    rows = conn.execute(
        f"SELECT COUNT(*) FROM ({body.strip().rstrip(';')})"
    ).fetchone()[0]

    assert rows == 60, (
        f"the two UNION branches of sql/02 overlap: rewritten with UNION ALL it "
        f"yields {rows} rows instead of 60. The anti-join predicate "
        f"(WHERE gl.entity IS NULL) on the second branch is missing or wrong."
    )
