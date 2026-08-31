# Governance: BCBS 239 Mapping

**BCBS 239** — *Principles for effective risk data aggregation and risk
reporting* (Basel Committee, January 2013) — sets out 14 principles across four
sections. It is the standard behind most of what a bank's regulatory reporting
controls are actually trying to satisfy, including the CCAR / FR Y-14Q
submissions this tool models.

This document maps the tool honestly against those principles: what it
demonstrates, what it partially demonstrates, and what it does not address at
all. The last column is the important one. A mapping that claimed full coverage
would be worthless — a single local tool cannot satisfy a firm-wide governance
standard, and saying otherwise is the kind of claim that collapses under one
follow-up question.

---

## Section I · Overarching governance and infrastructure

| # | Principle | Status | How |
|---|---|---|---|
| 1 | **Governance** | ✗ Not addressed | Requires board and senior-management oversight, ownership and accountability structures. Organisational, not technical — nothing in a repository can satisfy it. |
| 2 | **Data architecture and IT infrastructure** | ◐ Partial | Explicit typed DDL, documented source-to-target lineage (`docs/DATA_LINEAGE.md`), reproducible builds, and an audit trail tying every result to a code version and input digests. Missing: enterprise metadata management, access control, disaster recovery. |

## Section II · Risk data aggregation capabilities

| # | Principle | Status | How |
|---|---|---|---|
| 3 | **Accuracy and Integrity** | ✓ Demonstrated | The core of the tool. Automated GL-to-sub-ledger and GL-to-extract tie-outs at a 0.5% materiality threshold; clean accounts reconcile to **exactly** 0.00 via integer-cent apportionment; 80 automated tests grade results against a known answer key by identity, not by count. Load-boundary row-count and checksum reconciliation catches corruption in the tool's own plumbing. |
| 4 | **Completeness** | ✓ Demonstrated | All 60 in-scope accounts are reconciled every run. `sql/01` uses a LEFT JOIN from the GL so an account whose feed failed entirely still appears (`FEED_NOT_RECEIVED`) rather than vanishing. `sql/02` uses a full outer join so balances present on only one side — filed-but-unsupported, or supported-but-unfiled — are caught in both directions. |
| 5 | **Timeliness** | ◐ Partial | The timing-difference detection is exactly this principle: it catches a GL adjustment that posted after the extract ran, comparing `as_of_date` against `extract_run_date`. Missing: SLA monitoring, feed-freshness gates that refuse to reconcile stale data, and scheduled execution. |
| 6 | **Adaptability** | ◐ Partial | Two interchangeable backends behind one environment variable; new reconciliations are added by dropping a `.sql` file in `sql/`; dialect-specific rewrites resolve by convention. Missing: configurable thresholds — 0.5% is hardcoded in four `.sql` files plus `config.py`, which is a known weakness, not a design choice. |

## Section III · Risk reporting practices

| # | Principle | Status | How |
|---|---|---|---|
| 7 | **Accuracy** | ✓ Demonstrated | Reports are reconciled to source before publication, and the reconciliation itself is verified against planted breaks. The trend view and the detail views are asserted to agree, so the governance summary and the operational detail cannot drift apart. |
| 8 | **Comprehensiveness** | ◐ Partial | Covers all five entities, six products and both quarters, with four distinct failure modes root-caused. Missing: it reconciles balances only — no risk dimensions such as exposure at default, delinquency status or concentration. |
| 9 | **Clarity and usefulness** | ✓ Demonstrated | Every break carries a root cause and a suggested action, so the report says what to do rather than only what is wrong. `sql/04` ranks breaks by materiality with a Pareto cut-off, distinguishes recurring failures from one-off incidents, and flags whether a recurring break is improving or worsening. |
| 10 | **Frequency** | ◐ Partial | Designed around a quarterly reporting cycle and re-runnable on demand. Missing: scheduling and orchestration — every run is manual. |
| 11 | **Distribution** | ✗ Not addressed | No access control, no distribution lists, no confidentiality controls. The dashboard is unauthenticated. |

## Section IV · Supervisory review, tools and cooperation

| # | Principle | Status | How |
|---|---|---|---|
| 12 | **Review** | ◐ Partial | Results are independently verifiable: the audit trail pins each run to a code version and input SHA-256s, so a reviewer can re-derive any published figure. Missing: independent validation function, formal sign-off workflow. |
| 13 | **Remedial actions and supervisory measures** | ◐ Partial | Each break carries a remediation action and a priority; recurring breaks are separated from new ones because they belong to different owners. Missing: break assignment, ageing against SLA, clearance workflow and evidence capture. |
| 14 | **Home/host cooperation** | ✗ Not applicable | Concerns supervisory coordination across jurisdictions. |

---

## Summary

**4 demonstrated · 7 partial · 3 not addressed.**

The tool's genuine strength is Section II — the data-aggregation principles
(3, 4, 5) — plus reporting accuracy and clarity (7, 9). That is unsurprising:
those are the principles a reconciliation engine is *for*.

Its genuine gaps are governance (1), distribution (11), and the workflow half
of remediation (13). Those need an organisation, an access model and an
orchestration layer around the tool, not more code inside it.

Anyone reading this as a portfolio piece should take the ✗ rows as seriously as
the ✓ rows. A candidate who claims a personal project satisfies BCBS 239 has
misunderstood the standard; one who can say precisely which five principles it
touches, which three it cannot, and why, has understood it.

---

## What each principle would need next

| Gap | Concrete next step |
|---|---|
| Configurable thresholds (6) | Promote the shared variance logic to a view or dbt model with the threshold as a parameter, joined from a reference table by schedule and product tier. Removes the current duplication across four `.sql` files. |
| Durable audit store (2, 12) | Move `audit_run_log` out of the operational database into a separate append-only store with retention policy and its own access controls. |
| Orchestration and freshness (5, 10) | Airflow or Dagster with dependency management, retries, feed-freshness checks that refuse to reconcile stale inputs, and a submission gate that blocks while material breaks are open. |
| Access control (11) | SSO, Snowflake roles scoped so analysts read reconciliation views without touching source tables, row-level security by legal entity, secrets in a managed vault. |
| Break workflow (13) | Break assignment, ageing against SLA, clearance evidence, and segregation of duties — the person clearing a break must not be the person who posted the adjustment. |
