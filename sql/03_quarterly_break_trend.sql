-- ===========================================================================
-- RECONCILIATION 3 OF 3 : QUARTERLY BREAK TREND
-- Engine: SQLite (local development)
-- ===========================================================================
-- Question answered: is the reporting process getting better or worse?
--
-- Queries 1 and 2 answer "which accounts are broken right now". That is the
-- operational view - it tells a controller what to chase before Friday. This
-- query answers the governance question a CFO or an examiner asks instead:
-- what share of the reporting population broke this quarter, how much money
-- was in doubt, and is the trend moving the right way?
--
-- The two headline measures are deliberately BOTH reported, because either
-- one alone misleads:
--   * break_rate_pct  - how much of the population is affected. Catches a
--                       process degrading broadly across many small accounts.
--   * total_break_amount - how many dollars are in doubt. Catches a single
--                       break on the mortgage book that matters more than
--                       twenty on credit cards.
-- A quarter can improve on one and worsen on the other; showing only the
-- flattering one is how reporting metrics become theatre.
--
-- ---------------------------------------------------------------------------
-- WHY AN ACCOUNT IS COUNTED ONCE, NOT TWICE
-- ---------------------------------------------------------------------------
-- The same entity/product/quarter can break in both reconciliations at once.
-- accounts_with_breaks counts DISTINCT accounts across the union of the two,
-- so the break rate can never exceed 100% no matter how many ways one account
-- fails. The per-reconciliation counts are reported alongside it so the total
-- stays decomposable.
--
-- total_break_amount, by contrast, sums the absolute variance from BOTH
-- reconciliations. That is intentional: a sub-ledger break and an extract
-- break on the same account are two distinct dollar exposures that both need
-- clearing, and netting them would understate the work outstanding. Absolute
-- value, because a 1m understatement and a 1m overstatement are two problems,
-- not zero problems.
--
-- ---------------------------------------------------------------------------
-- WHY THE RECONCILIATION LOGIC IS REPEATED HERE
-- ---------------------------------------------------------------------------
-- The CTEs below restate the variance logic from queries 1 and 2 rather than
-- selecting from their output. Each .sql file is a standalone artifact that an
-- analyst can paste into a console and run without first materialising two
-- other result sets, which is how these queries actually get used during a
-- close. The cost is that a change to the materiality rule has to be made in
-- three files - a real maintenance tax, called out in the README's scope note,
-- and the first thing a production version would fix by promoting the shared
-- logic to a view or a dbt model.
-- ===========================================================================

WITH subledger_rollup AS (
    SELECT entity, product, quarter,
           ROUND(SUM(loan_amount), 2) AS sub_ledger_balance
    FROM sub_ledger
    GROUP BY entity, product, quarter
),

subledger_variance AS (
    SELECT
        gl.entity,
        gl.product,
        gl.quarter,
        ROUND(COALESCE(sl.sub_ledger_balance, 0) - gl.gl_balance, 2) AS variance,
        CASE
            WHEN gl.gl_balance = 0
                 AND COALESCE(sl.sub_ledger_balance, 0) <> 0 THEN 'BREAK'
            WHEN ABS(100.0 * (COALESCE(sl.sub_ledger_balance, 0) - gl.gl_balance)
                     / NULLIF(gl.gl_balance, 0)) > 0.5 THEN 'BREAK'
            ELSE 'PASS'
        END AS status,
        CASE
            WHEN COALESCE(sl.sub_ledger_balance, 0) - gl.gl_balance < 0
                THEN 'LATE_FEED'
            ELSE 'DUPLICATE_LOAD'
        END AS root_cause
    FROM general_ledger gl
    LEFT JOIN subledger_rollup sl
           ON sl.entity  = gl.entity
          AND sl.product = gl.product
          AND sl.quarter = gl.quarter
),

gl_rollup AS (
    SELECT entity, product, quarter, ROUND(SUM(gl_balance), 2) AS gl_balance
    FROM general_ledger
    GROUP BY entity, product, quarter
),

rx_rollup AS (
    SELECT entity, product, quarter, ROUND(SUM(extract_balance), 2) AS extract_balance
    FROM regulatory_extract
    GROUP BY entity, product, quarter
),

-- Same FULL OUTER JOIN simulation as query 2: LEFT JOIN one way, UNION'd with
-- the anti-joined LEFT JOIN the other way. See sql/02 for the full rationale.
extract_full_outer AS (
    SELECT gl.entity, gl.product, gl.quarter, gl.gl_balance, rx.extract_balance
    FROM gl_rollup gl
    LEFT JOIN rx_rollup rx
           ON rx.entity = gl.entity AND rx.product = gl.product AND rx.quarter = gl.quarter
    UNION
    SELECT rx.entity, rx.product, rx.quarter, gl.gl_balance, rx.extract_balance
    FROM rx_rollup rx
    LEFT JOIN gl_rollup gl
           ON gl.entity = rx.entity AND gl.product = rx.product AND gl.quarter = rx.quarter
    WHERE gl.entity IS NULL
),

extract_variance AS (
    SELECT
        entity,
        product,
        quarter,
        ROUND(COALESCE(extract_balance, 0) - COALESCE(gl_balance, 0), 2) AS variance,
        CASE
            WHEN gl_balance IS NULL OR extract_balance IS NULL THEN 'BREAK'
            WHEN gl_balance = 0 AND COALESCE(extract_balance, 0) <> 0 THEN 'BREAK'
            WHEN ABS(100.0 * (COALESCE(extract_balance, 0) - COALESCE(gl_balance, 0))
                     / NULLIF(gl_balance, 0)) > 0.5 THEN 'BREAK'
            ELSE 'PASS'
        END AS status
    FROM extract_full_outer
),

-- One row per break from either reconciliation, tagged with which tie-out
-- raised it. Stacking them here keeps the aggregation below trivial.
all_breaks AS (
    SELECT quarter, entity, product, 'gl_vs_subledger' AS reconciliation,
           root_cause, ABS(variance) AS break_amount
    FROM subledger_variance
    WHERE status = 'BREAK'
    UNION ALL
    SELECT quarter, entity, product, 'gl_vs_regulatory_extract' AS reconciliation,
           'EXTRACT_VARIANCE' AS root_cause, ABS(variance) AS break_amount
    FROM extract_variance
    WHERE status = 'BREAK'
),

population AS (
    SELECT quarter, COUNT(*) AS total_accounts
    FROM general_ledger
    GROUP BY quarter
)

SELECT
    p.quarter,
    p.total_accounts,
    -- DISTINCT: an account broken in both tie-outs is still one broken
    -- account, so the rate stays bounded by 100%.
    COUNT(DISTINCT b.entity || '|' || b.product)                       AS accounts_with_breaks,
    ROUND(100.0 * COUNT(DISTINCT b.entity || '|' || b.product)
          / p.total_accounts, 2)                                        AS break_rate_pct,
    -- Absolute variance summed across BOTH tie-outs: two exposures on one
    -- account are two pieces of work, and netting them would hide one.
    ROUND(COALESCE(SUM(b.break_amount), 0), 2)                          AS total_break_amount,
    SUM(CASE WHEN b.reconciliation = 'gl_vs_subledger' THEN 1 ELSE 0 END)
                                                                        AS subledger_breaks,
    SUM(CASE WHEN b.reconciliation = 'gl_vs_regulatory_extract' THEN 1 ELSE 0 END)
                                                                        AS extract_breaks,
    ROUND(COALESCE(SUM(CASE WHEN b.reconciliation = 'gl_vs_subledger'
                            THEN b.break_amount END), 0), 2)            AS subledger_break_amount,
    ROUND(COALESCE(SUM(CASE WHEN b.reconciliation = 'gl_vs_regulatory_extract'
                            THEN b.break_amount END), 0), 2)            AS extract_break_amount
FROM population p
LEFT JOIN all_breaks b
       ON b.quarter = p.quarter
GROUP BY p.quarter, p.total_accounts
ORDER BY p.quarter;
