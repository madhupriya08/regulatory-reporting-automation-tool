# Regulatory Reporting Automation Tool

An automated reconciliation tool modelled on the pre-submission tie-out a bank
runs before filing **CCAR / FR Y-14Q** data to its regulator.

It generates three synthetic source systems with known defects planted in
them, reconciles those systems in SQL, labels the likely root cause of every
break, and grades its own answers against the planted key. It runs against
local SQLite for development and against a live Snowflake warehouse for the
real thing, selected by a single environment variable.

```
14 breaks planted  ->  14 breaks detected  ->  0 false positives
46 never-touched accounts reconcile at exactly 0.00 / 0.0000%
```

---

## Why this exists

Before a bank files regulatory data it has to prove three numbers agree:

1. what the **general ledger** says the balance is (the certified number),
2. what the **sub-ledger** of individual loans adds up to (the detail),
3. what the **regulatory extract** actually filed (the number the regulator sees).

When they disagree, somebody has to find out why before the filing deadline.
That work is the tie-out, and this tool automates the finding-out part: not
just *which* accounts broke, but *which failure mode* broke them, which is what
determines who fixes it and how.

---

## Architecture

```
  src/generate_data.py
        │  writes 4 CSVs
        ▼
  data/general_ledger.csv        60 rows    5 entities x 6 products x 2 quarters
  data/sub_ledger.csv         6,109 rows    loan-level detail
  data/regulatory_extract.csv    60 rows    what was filed
  data/answer_key.csv            14 rows    the planted breaks  ← the grading key
        │
        ├──────────────────────────────┬───────────────────────────────┐
        ▼                              ▼                               │
  src/build_database.py         src/build_snowflake.py                 │
   SQLite, + 3 indexes           Snowflake, no indexes                 │
        │                              │                               │
        ▼                              ▼                               │
  regulatory_reporting.db      3 Snowflake tables                      │
        │                              │                               │
        ├──────────────┬───────────────┤                               │
        ▼              ▼               ▼                               │
  run_queries.py   dashboard.py   run_queries_snowflake.py             │
        │           (either backend)   │                               │
        ▼                              ▼                               │
       output/01..03.csv  ── identical filenames and columns ──────────┘
                                       │
                                       ▼
                                   tests/  ── grades results against answer_key.csv
```

**The three reconciliations**

| # | Query | Catches | How the root cause is derived |
|---|-------|---------|-------------------------------|
| 1 | `sql/01_gl_vs_subledger.sql` | `late_feed`, `duplicate_load` | Variance **direction**. Under the GL means detail is missing; over means detail is doubled. |
| 2 | `sql/02_gl_vs_regulatory_extract.sql` | `timing_difference`, `mapping_error` | Whether an **equal and opposite** variance exists elsewhere in the same entity and quarter. |
| 3 | `sql/03_quarterly_break_trend.sql` | — | Aggregates both of the above per quarter. |

**The four failure modes**

| Failure mode | What happened | Signature |
|---|---|---|
| **Late feed** | Loans missed the load cutoff | Sub-ledger **under** the GL |
| **Duplicate load** | A batch was replayed | Sub-ledger **over** the GL, with duplicated `loan_id`s |
| **Timing difference** | A GL adjustment posted after the extract ran | Extract **under** the GL, one-sided |
| **Mapping error** | Loans tagged to the wrong product code | Extract variance arrives as a **pair that nets to zero** |

The extract is generated from certified GL balances, so a sub-ledger break does
not disturb the filed number. That keeps the two reconciliations independent:
query 1 owns sub-ledger failures, query 2 owns extract failures, and neither
double-reports the other's.

---

## Quick start

```bash
pip install -r requirements.txt

python src/generate_data.py      # write the 4 CSVs
python src/build_database.py     # load them into SQLite
python src/run_queries.py        # export output/01..03.csv
python -m pytest tests/ -q       # grade the SQL against the answer key
streamlit run src/dashboard.py   # break report
```

---

## Why 0.5% materiality

A variance is escalated only when it exceeds **0.5% of the GL balance**.

The threshold is a judgement call and both extremes are worse:

- **Zero tolerance** flags every rounding artifact. On a real sub-ledger of
  millions of loans, a report with hundreds of one-cent "breaks" buries the
  handful that matter, and people stop reading it. A reconciliation report
  nobody reads has negative value — it creates the appearance of control
  without the substance.
- **1%** is loose enough to hide a genuine mis-load. 1% of a $38M mortgage book
  is $380,000 sliding through unflagged.

0.5% is the conventional working tolerance for a schedule-level tie-out of this
kind. It is deliberately *not* a claim about what a regulator requires — it is
an operational trigger for human review, sized so the queue stays workable.

The interesting part is what the threshold is actually doing here. Because of
the apportionment fix below, clean accounts reconcile at **exactly 0.0000%**,
not "somewhere under 0.5%". So the threshold is not absorbing noise — there is
no noise. It is doing real diagnostic work, and the smallest planted break
(1.20%) sits well clear of it, so the test suite grades detection logic rather
than threshold luck.

The threshold is currently hardcoded in three `.sql` files and in
`config.MATERIALITY_THRESHOLD_PCT`. A test asserts they agree. See the scope
note — this is the first thing a production version would fix.

---

## The clean-scenario rescaling fix

**This is the single most important implementation detail in the project**, and
it is the kind of bug that fails silently and gets misdiagnosed.

### The obvious approach, and why it is wrong

The natural way to build loan-level detail is to pick an average loan size,
draw N independent random amounts around it, and let the sub-ledger total be
whatever it comes to:

```python
amounts = rng.normal(avg_loan_size, spread, size=n_loans)   # WRONG
```

The total lands *near* the GL balance but never *on* it. The residual is
random, and its size relative to the balance depends on the loan count and the
balance. On a combo with few loans or a small balance, that residual can drift
past 0.5% **on its own**.

What that produces:

- phantom breaks on accounts nothing was ever planted on,
- an answer key that no longer describes the data beside it,
- a test failure that looks like a bug in the SQL when the bug is in the data,
- and worst, a suite that passes on one random seed and fails on the next.

That last property is what makes this expensive. A test that fails
*intermittently* costs far more to diagnose than one that fails every time.

### The fix

Draw the loan amounts as **weights**, then apportion the GL balance across
those weights in **integer cents** using largest-remainder allocation:

```python
def allocate_cents(total_cents: int, weights: np.ndarray) -> np.ndarray:
    w = weights / weights.sum()
    exact  = w * total_cents
    floors = np.floor(exact).astype(np.int64)      # always under-allocates
    leftover = int(total_cents - floors.sum())     # 0 <= leftover < len(w)
    order = np.argsort(-(exact - floors))          # largest discarded fraction first
    floors[order[:leftover]] += 1
    assert floors.sum() == total_cents             # an identity, not a hope
    return floors
```

`sum(parts) == total` is now an **integer identity**, not a floating-point
approximation. Every quantity that has to be exact is carried as integer cents
and converted to dollars only on write.

Result: a clean account reconciles at exactly `0.00` and `0.0000%`.

Real variance is then introduced *deliberately*, and only where a failure mode
calls for it — `late_feed` drops a tail batch of loans, `duplicate_load`
replays one. Those are the only two scenarios with any variance from the GL at
all, which is precisely what those words mean on a reporting floor.

### How it is defended

Three layers, because a fix nobody notices reverting is a fix with a shelf life:

1. `generate_data.py` asserts it before writing. A clean combo that has drifted
   by one cent aborts the run rather than shipping a corrupted answer key.
2. `test_clean_accounts_reconcile_to_exactly_zero` asserts `== 0.0`, not
   `< 0.5`. Asserting the weaker property would let noise creep back in
   until it happened to cross the threshold on some future seed.
3. A separate test regenerates the data in a subprocess under a different
   `PYTHONHASHSEED` and compares SHA-256 digests, so the committed answer key
   is provably a valid key for the committed CSVs.

That third test earned its place: it caught a real bug. `gl_account` was built
from Python's `hash()`, which CPython salts per process, so that column changed
on every run while the fixed numpy seed made everything else look stable. Fixed
with `zlib.crc32`, which is stable across processes, platforms and versions.

---

## SQLite vs Snowflake

Both backends load the same CSVs into the same three tables and export the same
three result CSVs with the same filenames and columns. Anything consuming those
files cannot tell which engine produced them — the backend is an infrastructure
choice, not a change to the deliverable.

### Switching

```bash
# SQLite (default — no credentials needed)
python src/build_database.py
python src/run_queries.py
streamlit run src/dashboard.py

# Snowflake
export SNOWFLAKE_ACCOUNT=...      SNOWFLAKE_USER=...
export SNOWFLAKE_PASSWORD=...     SNOWFLAKE_WAREHOUSE=...
export SNOWFLAKE_DATABASE=...     SNOWFLAKE_SCHEMA=...
export SNOWFLAKE_ROLE=...         # optional

python src/build_snowflake.py
python src/run_queries_snowflake.py
USE_SNOWFLAKE=true streamlit run src/dashboard.py
```

`USE_SNOWFLAKE` is the only switch. The dashboard contains no branch on the
backend — every query goes through `src/backends.py`, which resolves the right
SQL file and normalises the result.

Anything other than `1/true/yes/y/on` selects SQLite. The default is
deliberately the local backend: a mistyped variable should never silently point
a run at a live warehouse.

**Credentials are read from environment variables and nowhere else.** No config
file, no keyring, no prompt, no defaults. If any are missing you get a message
naming exactly which ones, with the `export` lines to fix them — not a
connector stack trace. Nothing ever logs or echoes a credential value.

### What is actually different

#### 1. Index strategy

| | SQLite | Snowflake |
|---|---|---|
| Approach | 3 composite B-tree indexes on `(entity, product, quarter)` | none |

`build_database.py` creates indexes because every query groups or joins on that
composite and SQLite would otherwise rescan the whole sub-ledger once per
grouping.

`build_snowflake.py` has **no `CREATE INDEX` step at all**, and that is correct
rather than an oversight. Snowflake stores tables as immutable
**micro-partitions** and records per-column min/max metadata for each one. A
query filtering or grouping on those columns prunes whole micro-partitions
using that metadata — nothing to declare, nothing to maintain, no B-tree to
keep in sync on write.

Snowflake does offer **clustering keys**, which control the order data is
written in so pruning gets sharper. They are deliberately not used: they are
for multi-terabyte tables whose natural clustering has degraded, they incur
continuous background reclustering cost, and on a 6,000-row sub-ledger they
would cost money for no measurable benefit. Adding one would be cargo-culting
the index habit into an engine that does not want it.

#### 2. The FULL OUTER JOIN simplification

Reconciliation 2 must see **both** one-sided failures — a GL balance that never
got filed, and a filed balance with no GL support. A `LEFT JOIN` sees only the
first; an `INNER JOIN` sees neither.

The shared SQLite file simulates a full outer join:

```sql
SELECT ... FROM gl LEFT JOIN rx  ON <3 predicates>
UNION
SELECT ... FROM rx LEFT JOIN gl  ON <the same 3 predicates, written again>
WHERE gl.entity IS NULL          -- anti-join: contribute only extract-only rows
```

A 26-line CTE that scans each side twice and states the join key twice.
Snowflake states it once:

```sql
FROM gl FULL OUTER JOIN rx ON <3 predicates>
```

The whole file is 22 SQL lines shorter (100 vs 122), but the line count is not
the real gain — the join key is stated **once**, so it cannot drift out of step
between two branches. That is the actual maintenance hazard in the simulated
form.

The rewrite is not free. On an unmatched row, *every* column from the absent
side is NULL — **join keys included**. So `gl.entity` is NULL exactly when the
account is extract-only, and reading it directly would silently drop those rows.
`COALESCE(gl.entity, rx.entity)` is not defensive noise; it is the correct way
to read a key out of a full outer join, and forgetting it turns the one join
that was supposed to catch both one-sided failures into one that catches
neither.

> **An honest caveat.** SQLite gained `FULL OUTER JOIN` in **3.39** (2022), so
> the "SQLite can't do this" framing is only true of older builds. It is still
> the right choice for the shared file: Python's bundled `sqlite3` version
> varies by platform and distribution, and the simulated form runs everywhere.
>
> That version support turns out to be useful for a different reason. Because
> the local build here is 3.45.1, the *Snowflake-dialect* file can be executed
> against SQLite and compared row for row against the simulated one. They
> produce identical result sets — same 60 rows, same 7 breaks, same root causes
> and offsetting products — so the equivalence is **verified, not assumed**.
> That test proves the rewrite's logic; it does not prove Snowflake-specific
> behaviour like `NUMBER` arithmetic or identifier casing, which needs the live
> tests.

#### 3. Column casing

Snowflake folds unquoted identifiers to **UPPERCASE** and returns result columns
that way. SQLite preserves the case the query used.

```
SQLite     ->  entity, product, gl_balance
Snowflake  ->  ENTITY, PRODUCT, GL_BALANCE
```

Every consumer indexes columns by name, so left alone `frame["entity"]` works on
one backend and raises `KeyError` on the other. The tempting fix — scattering
`.get("entity") or .get("ENTITY")` through the dashboard — spreads a backend
detail into UI code that has no business knowing about it, and misses a spot.

`backends.normalize_columns()` lowercases every result frame at the boundary
instead. One function, one place, and no code downstream needs to know which
engine ran.

#### 4. Numeric type

| | SQLite | Snowflake |
|---|---|---|
| Balances | `REAL` (binary float — the best SQLite offers) | `NUMBER(18, 2)` (exact fixed-point decimal) |

On a reconciliation whose premise is that clean accounts tie out to the cent,
exact decimal arithmetic is the right type. This is one of the few places the
Snowflake backend is not merely different but better.

#### 5. Keeping the dialects apart

`run_queries.py` must never hand Snowflake-dialect SQL to SQLite. Two
independent guards:

1. `glob('*.sql')` is non-recursive, so `sql/snowflake/` is never visited.
2. `_sqlite_query_files()` re-checks every discovered path and drops anything
   under that directory — the guard that survives someone widening the glob to
   `rglob` later for a perfectly good reason.

Query selection is convention-based: `sql/snowflake/<stem>_snowflake.sql` wins
when it exists, otherwise the shared `sql/<stem>.sql`. Queries 1 and 3 are
portable ANSI and resolve to the **same file** on both engines, so both backends
provably apply the same materiality rule. Two copies of a materiality rule is
two materiality rules, eventually.

---

## Tests

```bash
python -m pytest tests/ -q          # 56 passed, 5 skipped without Snowflake
```

The distinction the suite exists to enforce: **"the query ran" is not the same
claim as "the query is correct."** A tie-out that flags 40 accounts finds all 14
real breaks too, and is useless. One that flags 7 looks disciplined and may have
found the wrong 7. So assertions compare against the answer key by **identity** —
the same accounts, no more and no fewer — reporting misses and false positives
separately, because those mean opposite things to whoever has to fix them.

| Area | What is asserted |
|---|---|
| Load integrity | Row counts, monetary checksums, and the exact `loan_id` **multiset** — multiset, not distinct set, because duplicated ids are a planted break, so this proves the duplicates present are the planted ones and not extras the loader introduced |
| Exact detection | Query 1 finds precisely the 7 planted sub-ledger breaks; query 2 precisely the 7 planted extract breaks |
| Clean accounts | Exactly `0.00` / `0.0000%` — not merely under threshold |
| Root causes | Labels match the planted failure mode **and** agree with the sign of their own variance; `DUPLICATE_LOAD` is cross-checked against independently computed duplicate-id counts |
| Mapping pairs | Each names its partner, the pairing is symmetric, and the pair nets to zero |
| Full outer join | Rewriting `sql/02`'s `UNION` to `UNION ALL` must leave the row count at 60 |
| Trend agreement | The governance view and the operational view cannot drift apart |
| Reproducibility | Regenerating in a subprocess under a different `PYTHONHASHSEED` yields byte-identical CSVs |
| Snowflake (offline) | Credential handling, backend selection, file resolution, casing, dialect equivalence |
| Snowflake (live) | Real row counts, lowercase columns, and the same 14 breaks on the warehouse |

The suite was itself checked by **mutation testing**: flipping the
`LATE_FEED`/`DUPLICATE_LOAD` labels fails 2 tests, perturbing one clean loan
amount fails the exact-zero test, and deleting the anti-join predicate fails the
disjointness test.

That last one is worth recording, because the first version of the test did
*not* catch it. Deleting the anti-join changed nothing — `UNION`'s set semantics
collapse the re-emitted rows anyway — which meant a comment in `sql/02` claiming
that predicate kept the branches disjoint was **wrong**. The comment now says
what is actually true, and the test derives its SQL from the real file rather
than restating it inline, so it tracks `sql/02` instead of a copy that can drift.

The live Snowflake tests **skip** rather than fail without credentials. A
developer without warehouse access should still get a green suite for the path
they can run; a red suite everyone learns to ignore protects nothing.

---

## Scope: what a production version would still need

This is a portfolio-scale build. What it would need before running a real
filing:

**Configurable thresholds.** 0.5% is hardcoded in three `.sql` files and
`config.py`, with a test asserting they agree. That test is a smell, not a
solution. Real reporting applies different tolerances by schedule, product and
materiality tier — a card portfolio and a mortgage book do not deserve the same
percentage. The fix is to promote the shared variance logic to a view or a dbt
model with the threshold as a parameter, joined from a reference table, which
also removes the current duplication of that logic across query 3.

**Audit trail.** Right now a run overwrites its outputs and leaves no history.
A regulated process needs the opposite: every run immutably recorded with its
inputs, code version and results; every break assigned an owner, an explanation
and a clearing date; every threshold change attributed to a person and a
justification. When an examiner asks why a $1.1M break was cleared in Q1, "the
CSV was regenerated" is not an answer.

**Orchestration.** The four scripts are run by hand in order. Production needs
a scheduler (Airflow, Dagster) with real dependency management, retries,
data-freshness checks that refuse to reconcile a stale feed, alerting when
break counts breach tolerance, and a hard gate that blocks submission while
material breaks are open.

**Access control.** There is none. Real deployment needs SSO, Snowflake roles
scoped so analysts read reconciliation views without touching source tables,
row-level security by legal entity, secrets in a managed vault rather than
shell environment variables, and segregation of duties — the person who clears
a break must not be the person who planted the adjustment.

**Also missing:** data-quality checks upstream of reconciliation (nulls,
duplicates, referential integrity, negative balances); break-aging and
recurrence analysis, since the same account breaking four quarters running is a
different problem from a one-off; automated evidence packs for audit;
reconciliation of the extract against the *filed* return rather than against
the GL alone; and multi-currency, which the entire dollar-denominated model here
quietly assumes away.

---

## Repository layout

```
config.py                       shared constants: universe, threshold, paths, seed
requirements.txt

data/                           generated CSVs + the answer key (committed)
output/                         query results (gitignored — regenerated)

sql/
  01_gl_vs_subledger.sql        portable: runs on both engines
  02_gl_vs_regulatory_extract.sql   SQLite dialect (simulated FULL OUTER JOIN)
  03_quarterly_break_trend.sql  portable: runs on both engines
  snowflake/
    02_gl_vs_regulatory_extract_snowflake.sql   native FULL OUTER JOIN

src/
  generate_data.py              3 source systems + answer key
  build_database.py             SQLite loader (with indexes)
  build_snowflake.py            Snowflake loader (no indexes, NUMBER(18,2))
  snowflake_conn.py             env-var credentials, clear errors, no echoing
  backends.py                   backend selection, SQL resolution, casing
  run_queries.py                SQLite runner
  run_queries_snowflake.py      Snowflake runner
  dashboard.py                  Streamlit break report (either backend)

tests/
  conftest.py                   fixtures: fresh DB built from committed CSVs
  test_data_integrity.py        load boundary, answer-key validity, reproducibility
  test_reconciliation.py        grades the SQL against the answer key
  test_snowflake_backend.py     Snowflake backend (live tests skip without creds)
```

All data is synthetic. The entities are fictional and no real institution's
figures appear anywhere in this repository.
