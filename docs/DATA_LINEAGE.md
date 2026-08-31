# Data Lineage

Field-level lineage for every figure this tool publishes: where each number
originates, what happens to it, and which control proves it survived the trip.

This exists because a reconciliation result is only as defensible as its
provenance. "The tool says $1,138,184.59" invites the question "from what?",
and the answer has to be traceable to a source column and a transformation,
not to a screenshot.

---

## 1 · Source-to-target map

```
  general_ledger.csv ─────────┐
  sub_ledger.csv ─────────────┼──► SQLite / Snowflake ──► sql/01..04 ──► output/*.csv
  regulatory_extract.csv ─────┘         (3 tables)                          dashboard
                                              │
  answer_key.csv ─────────────────────────────┴──────────► tests/  (grading only,
                                                            never an input to a result)
```

`answer_key.csv` is deliberately **outside** the calculation path. It is read
only by the test suite. No query, loader or dashboard component reads it, so a
result can never be contaminated by the key it is graded against.

---

## 2 · Field-level lineage

### `general_ledger`

| Target column | Source | Transformation | Control |
|---|---|---|---|
| `entity` | `generate_data.ENTITY_SCALE` keys | verbatim | `test_general_ledger_covers_every_account` |
| `product` | `config.PRODUCTS` | verbatim | same |
| `quarter` | `config.QUARTERS` | verbatim | same |
| `gl_balance` | `n_loans × avg_loan × entity_scale × quarter_growth × jitter` | rounded to whole dollars, stored as integer cents until write | generator self-check asserts clean combos foot exactly |
| `gl_account` | `zlib.crc32(entity\|product)` | deterministic digest → 4-digit suffix | `test_generator_is_byte_for_byte_reproducible` |
| `as_of_date` | `QUARTER_END[quarter]` | verbatim | — |

### `sub_ledger`

| Target column | Source | Transformation | Control |
|---|---|---|---|
| `loan_id` | entity/product/quarter + sequence | formatted key | `test_sub_ledger_loan_multiset_survives_the_load` |
| `loan_amount` | lognormal weights | **largest-remainder apportionment of `gl_balance` in integer cents** | `test_clean_accounts_reconcile_to_exactly_zero` |
| `load_batch_id` | loan index ÷ 25 | batch grouping; `-REPLAY` suffix marks a duplicate load | `test_duplicate_loads_come_with_duplicated_loan_ids` |

The apportionment step is the single most important transformation in the
project. Loan amounts are drawn as *weights*, then the GL balance is divided
across them in whole cents, so a clean account's detail sums to its GL balance
**exactly** — 0.00, not 0.003. See the README for why the obvious alternative
silently corrupts the answer key.

### `regulatory_extract`

| Target column | Source | Transformation | Control |
|---|---|---|---|
| `extract_balance` | `general_ledger.gl_balance` | equals GL exactly, **except** where a timing or mapping break is planted | `test_extract_recon_finds_exactly_the_planted_breaks` |
| `schedule_code` | `PRODUCT_SCHEDULE[product]` | FR Y-14Q schedule mapping | — |
| `extract_run_date` | `EXTRACT_RUN_DATE[quarter]` | 8–9 days after quarter end | — |

That date gap is not decoration. It is the window in which a late GL
adjustment can post after the extract has already been pulled — the timing
difference failure mode, expressed in the schema rather than bolted on.

---

## 3 · Derived-result lineage

| Output column | Derived from | In |
|---|---|---|
| `sub_ledger_balance` | `SUM(sub_ledger.loan_amount)` grouped to GL grain | `sql/01` |
| `variance` | `sub_ledger_balance − gl_balance` | `sql/01` |
| `variance_pct` | `100 × variance ÷ gl_balance` | `sql/01`, `sql/02` |
| `status` | `ABS(variance_pct) > 0.5` | all queries |
| `root_cause` (tie-out 1) | sign of `variance` | `sql/01` |
| `duplicate_loan_ids` | `COUNT(*) − COUNT(DISTINCT loan_id)` | `sql/01` — computed **independently** of `root_cause`, so agreement between them is corroboration rather than restatement |
| `root_cause` (tie-out 2) | presence of an equal-and-opposite variance in the same entity/quarter | `sql/02` |
| `offsetting_product` | the partner product in that pair | `sql/02` |
| `break_rate_pct` | distinct broken accounts ÷ total accounts | `sql/03` |
| `materiality_rank` | `RANK() OVER (PARTITION BY quarter ORDER BY break_amount DESC)` | `sql/04` |
| `cumulative_pct_of_exposure` | running `SUM(...) OVER (... ROWS UNBOUNDED PRECEDING)` | `sql/04` |
| `break_persistence` | `COUNT(*) OVER (PARTITION BY entity, product, reconciliation)` | `sql/04` |
| `trend_vs_prior_quarter` | `LAG(variance) OVER (... ORDER BY quarter)` | `sql/04` |

---

## 4 · Run provenance

Every execution appends to `audit_run_log` and `audit_input_file`:

| Recorded | Why it is needed |
|---|---|
| `run_id`, `run_timestamp_utc` | identifies the run; UTC so "when" is unambiguous |
| `code_version` | git SHA, suffixed `-dirty` when the tree had uncommitted changes |
| `materiality_threshold_pct` | the threshold that was actually applied, not the one in today's config |
| `backend` | SQLite or Snowflake |
| `detail_json` | rows loaded, queries run, breaks found |
| `sha256` per input file | proves the exact bytes read — a filename proves nothing, since files get regenerated |

The tables are created `IF NOT EXISTS` and **never dropped**, so history
survives the rebuild that drops and recreates the three source tables. No code
path issues `UPDATE`, `DELETE`, `DROP` or `TRUNCATE` against them, and
`test_no_code_anywhere_updates_or_deletes_the_audit_tables` enforces that by
scanning the source rather than trusting the intention.

**Limitation, stated plainly:** the trail lives in the same SQLite file as the
data it describes, so deleting that file deletes the history. Acceptable for a
local development tool; not acceptable in production, where the audit store
must be separate, access-controlled and retention-managed — a system the
pipeline can append to but not administer.
