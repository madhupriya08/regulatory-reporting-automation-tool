"""Generate the three synthetic source systems plus the break answer key.

The three CSVs model the systems a bank ties out before an FR Y-14Q / CCAR
submission:

  general_ledger.csv     the accounting system of record (the certified number)
  sub_ledger.csv         loan-level detail that SHOULD foot to the GL
  regulatory_extract.csv what actually lands in the reg-reporting tool and
                         goes to the regulator

and answer_key.csv records every break we deliberately planted, so the test
suite can prove the SQL finds *the right* breaks rather than merely running
without an exception.

--------------------------------------------------------------------------
THE ROUNDING-NOISE TRAP (the single most important thing in this file)
--------------------------------------------------------------------------
The obvious way to build the sub-ledger is: pick an average loan size, draw
N independent random loan amounts around it, and let the sum be whatever it
is. That is wrong here, and it is wrong in a way that quietly destroys the
test suite.

If loan amounts are drawn independently, the sub-ledger total lands *near*
the GL balance but not on it. The residual is random, and on a combo with
few loans or a small balance the residual can drift past the 0.5%
materiality threshold on its own. You then get "breaks" on accounts you
never planted a break on, the answer key no longer describes the data, and
the failure looks like a bug in the SQL when it is really a bug in the data.

The fix used below: draw the loan amounts as *weights*, then allocate the GL
balance across those weights in integer cents using the largest-remainder
method. The allocation sums to the GL balance EXACTLY - not to within a
penny, exactly - so a clean account has a variance of 0.00 and 0.0000%, with
no floating-point residue at all. All arithmetic that has to be exact is done
in integer cents and only converted to dollars on write.

Real variance is then introduced *deliberately*, and only by the late_feed
and duplicate_load scenarios, which is precisely what those failure modes
mean in production.
"""

from __future__ import annotations

import sys
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    ANSWER_KEY_CSV,
    DATA_DIR,
    GENERAL_LEDGER_CSV,
    PRODUCTS,
    QUARTERS,
    RANDOM_SEED,
    REGULATORY_EXTRACT_CSV,
    SUB_LEDGER_CSV,
)

# --------------------------------------------------------------------------
# Portfolio shape
# --------------------------------------------------------------------------
# Entity scale factors: the retail bank carries the biggest book, the small
# business bank the smallest. Keeps relative magnitudes plausible.
ENTITY_SCALE = {
    "US Retail Bank": 1.00,
    "US Wealth Mgmt": 0.42,
    "US Card Services": 0.65,
    "US Small Business Bank": 0.38,
    "US Private Bank": 0.55,
}

# (average loan size in dollars, min loans, max loans) per product.
# Loan counts are deliberately scaled down so the repo stays portable - a real
# FR Y-14Q sub-ledger is millions of rows, not thousands. The reconciliation
# logic is identical either way; only the row count changes.
PRODUCT_PROFILE = {
    "Mortgage":            (420_000, 90, 150),
    "Auto Loan":           ( 31_000, 80, 140),
    "Credit Card":         (  6_500, 100, 150),
    "Personal Loan":       ( 18_000, 70, 130),
    "HELOC":               ( 85_000, 60, 110),
    "Small Business Loan": (145_000, 50, 100),
}

# Modest book growth between the two reporting quarters.
QUARTER_GROWTH = {"2025Q1": 1.000, "2025Q2": 1.037}

# FR Y-14Q-flavoured schedule codes, so the extract looks like a reg feed.
PRODUCT_SCHEDULE = {
    "Mortgage":            "H.1",
    "Auto Loan":           "H.2",
    "Credit Card":         "D.1",
    "Personal Loan":       "H.2",
    "HELOC":               "H.1",
    "Small Business Loan": "A.7",
}

QUARTER_END = {"2025Q1": "2025-03-31", "2025Q2": "2025-06-30"}
# The regulatory extract is pulled a few days after quarter end - which is
# exactly the window in which a late GL adjustment can slip past it.
EXTRACT_RUN_DATE = {"2025Q1": "2025-04-08", "2025Q2": "2025-07-09"}

# --------------------------------------------------------------------------
# The planted breaks
# --------------------------------------------------------------------------
# Each entry is a scenario applied to one entity/product/quarter combo. The
# combos are chosen by hand rather than at random so that no combo carries two
# breaks at once (which would make "what should the SQL have found?" ambiguous)
# and so the mapping-error pairs sit in entity/quarters that contain no other
# extract-side break (which would let the offset-detection logic pair the wrong
# two rows).
#
# Detected by query 1 (GL vs sub-ledger): the sub-ledger genuinely disagrees
# with the GL.
LATE_FEED_BREAKS = [
    # (entity, product, quarter, approximate shortfall as a fraction of GL)
    ("US Retail Bank",         "Mortgage",            "2025Q1", 0.024),
    ("US Card Services",       "Credit Card",         "2025Q2", 0.031),
    ("US Small Business Bank", "Small Business Loan", "2025Q1", 0.018),
    ("US Wealth Mgmt",         "HELOC",               "2025Q2", 0.042),
]

DUPLICATE_LOAD_BREAKS = [
    # (entity, product, quarter, approximate overstatement as a fraction of GL)
    ("US Private Bank",  "Personal Loan", "2025Q1", 0.029),
    ("US Retail Bank",   "Auto Loan",     "2025Q2", 0.016),
    ("US Card Services", "Personal Loan", "2025Q2", 0.037),
]

# Detected by query 2 (GL vs regulatory extract): the sub-ledger foots to the
# GL perfectly, but the number that reached the regulator does not.
TIMING_DIFFERENCE_BREAKS = [
    # (entity, product, quarter, size of the late GL adjustment as a fraction)
    ("US Wealth Mgmt",         "Mortgage",  "2025Q1", 0.022),
    ("US Small Business Bank", "Auto Loan", "2025Q2", 0.014),
    ("US Private Bank",        "Mortgage",  "2025Q2", 0.033),
]

# A mapping error moves balance between two products inside the same entity
# and quarter, so it always shows up as a PAIR of variances that net to zero:
# the source product understated by exactly what the target product is
# overstated by. That signature is what lets query 2 tell a mapping error apart
# from a timing difference without being told.
MAPPING_ERROR_BREAKS = [
    # (entity, quarter, source_product, target_product, fraction of source GL)
    ("US Retail Bank",   "2025Q1", "HELOC",     "Personal Loan",       0.026),
    ("US Card Services", "2025Q2", "Auto Loan", "Small Business Loan", 0.039),
]


def allocate_cents(total_cents: int, weights: np.ndarray) -> np.ndarray:
    """Split ``total_cents`` across ``weights`` so the parts sum EXACTLY.

    Largest-remainder apportionment: floor every share, then hand the leftover
    cents one at a time to the shares with the largest discarded fraction.
    Because everything is an integer, ``result.sum() == total_cents`` is an
    identity, not an approximation - which is the whole point (see the module
    docstring on the rounding-noise trap).
    """
    w = np.asarray(weights, dtype=float)
    w = w / w.sum()

    exact = w * total_cents
    floors = np.floor(exact).astype(np.int64)

    leftover = int(total_cents - floors.sum())
    if leftover > 0:
        order = np.argsort(-(exact - floors))
        floors[order[:leftover]] += 1

    assert floors.sum() == total_cents, "apportionment must be exact"
    assert (floors > 0).all(), "every loan must carry a positive balance"
    return floors


def build_general_ledger(rng: np.random.Generator) -> pd.DataFrame:
    """One certified GL balance per entity/product/quarter (5 x 6 x 2 = 60)."""
    rows = []
    for entity in ENTITY_SCALE:
        for product in PRODUCTS:
            avg_loan, lo, hi = PRODUCT_PROFILE[product]
            # Loan count is a property of the book, so it is drawn once per
            # entity/product and carried across both quarters.
            n_loans = int(rng.integers(lo, hi + 1))
            jitter = float(rng.uniform(0.90, 1.10))

            for quarter in QUARTERS:
                balance = (
                    n_loans
                    * avg_loan
                    * ENTITY_SCALE[entity]
                    * QUARTER_GROWTH[quarter]
                    * jitter
                )
                # Whole dollars: a GL balance with fractional cents would be
                # its own small source of noise.
                gl_cents = int(round(balance)) * 100
                rows.append(
                    {
                        "entity": entity,
                        "product": product,
                        "quarter": quarter,
                        # zlib.crc32, not hash(). Python salts hash() for str
                        # per process unless PYTHONHASHSEED is pinned, so
                        # hash() here made gl_account change on every run - the
                        # CSVs would differ between two runs of a generator
                        # whose whole contract is reproducibility, and the
                        # committed answer key would stop describing the
                        # committed data. crc32 is stable across processes,
                        # platforms and Python versions.
                        "gl_account": f"GL-{PRODUCT_SCHEDULE[product].replace('.', '')}-"
                                      f"{zlib.crc32(f'{entity}|{product}'.encode()) % 9000 + 1000}",
                        "as_of_date": QUARTER_END[quarter],
                        "_gl_cents": gl_cents,
                        "_n_loans": n_loans,
                    }
                )
    return pd.DataFrame(rows)


def build_sub_ledger(gl: pd.DataFrame, rng: np.random.Generator):
    """Loan-level detail, plus the answer-key rows for sub-ledger breaks.

    Clean combos are apportioned exactly onto the GL balance. Only the
    late_feed and duplicate_load scenarios carry real variance, and the
    variance recorded in the answer key is the variance that actually
    materialised - not the nominal target - because in production you do not
    get to choose how much of a batch failed to land.
    """
    late_feed = {(e, p, q): f for e, p, q, f in LATE_FEED_BREAKS}
    duplicate = {(e, p, q): f for e, p, q, f in DUPLICATE_LOAD_BREAKS}

    loan_rows: list[dict] = []
    answer_rows: list[dict] = []
    break_seq = 0

    for _, acct in gl.iterrows():
        entity, product, quarter = acct["entity"], acct["product"], acct["quarter"]
        key = (entity, product, quarter)
        gl_cents = int(acct["_gl_cents"])
        n_loans = int(acct["_n_loans"])

        # Lognormal weights give a realistic right-skewed loan-size mix: a
        # long tail of large loans, most of the book in smaller ones.
        weights = rng.lognormal(mean=0.0, sigma=0.55, size=n_loans)
        amounts = allocate_cents(gl_cents, weights)          # sums to GL exactly

        prefix = "".join(w[0] for w in entity.split())
        loans = [
            {
                "loan_id": f"{prefix}-{product[:3].upper()}-{quarter}-{i:04d}",
                "entity": entity,
                "product": product,
                "quarter": quarter,
                "_cents": int(cents),
                # Loans arrive in nightly batches; the batch is the unit that
                # goes missing or gets replayed.
                "load_batch_id": f"B{quarter}-{(i // 25) + 1:02d}",
            }
            for i, cents in enumerate(amounts)
        ]

        if key in late_feed:
            # LATE FEED: a tail batch of loans did not land before the
            # reporting cutoff, so the sub-ledger comes in UNDER the GL.
            target = int(round(gl_cents * late_feed[key]))
            dropped, dropped_cents = [], 0
            for loan in reversed(loans):
                if dropped_cents >= target:
                    break
                dropped.append(loan["loan_id"])
                dropped_cents += loan["_cents"]
            loans = [ln for ln in loans if ln["loan_id"] not in set(dropped)]

            break_seq += 1
            answer_rows.append(
                {
                    "break_id": f"BRK-{break_seq:03d}",
                    "entity": entity, "product": product, "quarter": quarter,
                    "break_type": "late_feed",
                    "detected_by": "gl_vs_subledger",
                    "expected_direction": "under",
                    "expected_root_cause": "LATE_FEED",
                    "planted_variance": round(-dropped_cents / 100.0, 2),
                    "planted_variance_pct": round(-100.0 * dropped_cents / gl_cents, 4),
                    "notes": f"{len(dropped)} loans missed the load cutoff and "
                             f"never reached the sub-ledger",
                }
            )

        elif key in duplicate:
            # DUPLICATE LOAD: a batch was replayed, so the same loan_ids appear
            # twice and the sub-ledger comes in OVER the GL.
            target = int(round(gl_cents * duplicate[key]))
            replay, replay_cents = [], 0
            for loan in loans:
                if replay_cents >= target:
                    break
                replay.append(loan)
                replay_cents += loan["_cents"]

            loans = loans + [
                {**loan, "load_batch_id": loan["load_batch_id"] + "-REPLAY"}
                for loan in replay
            ]

            break_seq += 1
            answer_rows.append(
                {
                    "break_id": f"BRK-{break_seq:03d}",
                    "entity": entity, "product": product, "quarter": quarter,
                    "break_type": "duplicate_load",
                    "detected_by": "gl_vs_subledger",
                    "expected_direction": "over",
                    "expected_root_cause": "DUPLICATE_LOAD",
                    "planted_variance": round(replay_cents / 100.0, 2),
                    "planted_variance_pct": round(100.0 * replay_cents / gl_cents, 4),
                    "notes": f"batch replayed: {len(replay)} loans double-counted",
                }
            )

        loan_rows.extend(loans)

    sub = pd.DataFrame(loan_rows)
    sub["loan_amount"] = (sub.pop("_cents") / 100.0).round(2)
    sub = sub[["loan_id", "entity", "product", "quarter", "loan_amount", "load_batch_id"]]
    return sub, answer_rows


def build_regulatory_extract(gl: pd.DataFrame, rng: np.random.Generator, break_seq: int):
    """What the reg-reporting tool actually filed, plus its answer-key rows.

    For every clean combo the extract equals the GL to the cent. The sub-ledger
    breaks are intentionally NOT reflected here: the extract is generated from
    the certified GL balances, so a late feed or a duplicate load in the
    sub-ledger leaves the filed number untouched. That separation is what keeps
    the two reconciliations independent - query 1 owns the sub-ledger failures,
    query 2 owns the extract failures, and neither double-reports the other's.
    """
    gl_lookup = {
        (r["entity"], r["product"], r["quarter"]): int(r["_gl_cents"])
        for _, r in gl.iterrows()
    }
    delta: dict[tuple, int] = {}
    answer_rows: list[dict] = []

    # TIMING DIFFERENCE: a late adjustment posted to the GL after the extract
    # had already been pulled. The GL is right, the filed number is stale, and
    # the extract therefore reads UNDER the GL. No offsetting entry exists.
    for entity, product, quarter, frac in TIMING_DIFFERENCE_BREAKS:
        key = (entity, product, quarter)
        adj = int(round(gl_lookup[key] * frac))
        delta[key] = delta.get(key, 0) - adj

        break_seq += 1
        answer_rows.append(
            {
                "break_id": f"BRK-{break_seq:03d}",
                "entity": entity, "product": product, "quarter": quarter,
                "break_type": "timing_difference",
                "detected_by": "gl_vs_regulatory_extract",
                "expected_direction": "under",
                "expected_root_cause": "TIMING_DIFFERENCE",
                "planted_variance": round(-adj / 100.0, 2),
                "planted_variance_pct": round(-100.0 * adj / gl_lookup[key], 4),
                "notes": f"GL adjustment posted after the {EXTRACT_RUN_DATE[quarter]} "
                         f"extract run; no offsetting entry elsewhere",
            }
        )

    # MAPPING ERROR: loans tagged to the wrong product code in the extract.
    # Balance leaves the source product and lands on the target product inside
    # the same entity and quarter, so the two variances net to exactly zero.
    for entity, quarter, source, target, frac in MAPPING_ERROR_BREAKS:
        src_key = (entity, source, quarter)
        tgt_key = (entity, target, quarter)
        moved = int(round(gl_lookup[src_key] * frac))

        delta[src_key] = delta.get(src_key, 0) - moved
        delta[tgt_key] = delta.get(tgt_key, 0) + moved

        break_seq += 1
        pair_id = f"BRK-{break_seq:03d}"
        answer_rows.append(
            {
                "break_id": pair_id,
                "entity": entity, "product": source, "quarter": quarter,
                "break_type": "mapping_error",
                "detected_by": "gl_vs_regulatory_extract",
                "expected_direction": "under",
                "expected_root_cause": "MAPPING_ERROR",
                "planted_variance": round(-moved / 100.0, 2),
                "planted_variance_pct": round(-100.0 * moved / gl_lookup[src_key], 4),
                "notes": f"loans mis-tagged out of {source} into {target} "
                         f"(source side of the pair)",
            }
        )
        answer_rows.append(
            {
                "break_id": pair_id,
                "entity": entity, "product": target, "quarter": quarter,
                "break_type": "mapping_error",
                "detected_by": "gl_vs_regulatory_extract",
                "expected_direction": "over",
                "expected_root_cause": "MAPPING_ERROR",
                "planted_variance": round(moved / 100.0, 2),
                "planted_variance_pct": round(100.0 * moved / gl_lookup[tgt_key], 4),
                "notes": f"loans mis-tagged into {target} out of {source} "
                         f"(target side of the pair)",
            }
        )

    rows = []
    for _, acct in gl.iterrows():
        key = (acct["entity"], acct["product"], acct["quarter"])
        cents = int(acct["_gl_cents"]) + delta.get(key, 0)
        rows.append(
            {
                "entity": acct["entity"],
                "product": acct["product"],
                "quarter": acct["quarter"],
                "extract_balance": round(cents / 100.0, 2),
                "schedule_code": PRODUCT_SCHEDULE[acct["product"]],
                "extract_run_date": EXTRACT_RUN_DATE[acct["quarter"]],
            }
        )
    return pd.DataFrame(rows), answer_rows, break_seq


def main() -> None:
    rng = np.random.default_rng(RANDOM_SEED)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    gl = build_general_ledger(rng)
    sub, sub_answers = build_sub_ledger(gl, rng)
    extract, ext_answers, _ = build_regulatory_extract(gl, rng, len(sub_answers))

    answer_key = pd.DataFrame(sub_answers + ext_answers)[
        [
            "break_id", "entity", "product", "quarter", "break_type",
            "detected_by", "expected_direction", "expected_root_cause",
            "planted_variance", "planted_variance_pct", "notes",
        ]
    ].sort_values(["quarter", "entity", "product"]).reset_index(drop=True)

    gl_out = gl.copy()
    gl_out["gl_balance"] = (gl_out.pop("_gl_cents") / 100.0).round(2)
    gl_out = gl_out[["entity", "product", "quarter", "gl_balance", "gl_account", "as_of_date"]]

    gl_out.to_csv(GENERAL_LEDGER_CSV, index=False)
    sub.to_csv(SUB_LEDGER_CSV, index=False)
    extract.to_csv(REGULATORY_EXTRACT_CSV, index=False)
    answer_key.to_csv(ANSWER_KEY_CSV, index=False)

    # ---- Self-check: prove the generator did what it claims -------------
    # If a clean combo has drifted off the GL by even a cent, the answer key
    # is a lie and every downstream test is measuring the wrong thing. Fail
    # loudly here rather than debugging it later as a "SQL bug".
    footed = sub.groupby(["entity", "product", "quarter"])["loan_amount"].sum().round(2)
    gl_map = gl_out.set_index(["entity", "product", "quarter"])["gl_balance"]
    planted_sub = {
        (r.entity, r.product, r.quarter)
        for r in answer_key.itertuples()
        if r.detected_by == "gl_vs_subledger"
    }
    for key, gl_balance in gl_map.items():
        variance = round(float(footed.get(key, 0.0)) - float(gl_balance), 2)
        if key in planted_sub:
            assert variance != 0.0, f"planted sub-ledger break {key} has zero variance"
        else:
            assert variance == 0.0, (
                f"clean combo {key} drifted by {variance} - the exact-apportionment "
                f"guarantee is broken"
            )

    print(f"general_ledger.csv      {len(gl_out):>6,} rows")
    print(f"sub_ledger.csv          {len(sub):>6,} rows")
    print(f"regulatory_extract.csv  {len(extract):>6,} rows")
    print(f"answer_key.csv          {len(answer_key):>6,} rows")
    print()
    print(answer_key["break_type"].value_counts().to_string())
    print()
    print("Clean combos foot to the GL exactly (0.00 variance): verified")


if __name__ == "__main__":
    main()
