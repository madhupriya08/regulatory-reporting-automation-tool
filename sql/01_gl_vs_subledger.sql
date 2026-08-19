-- ===========================================================================
-- RECONCILIATION 1 OF 3 : GENERAL LEDGER  vs  SUB-LEDGER
-- Engine: SQLite (local development)
-- ===========================================================================
-- Question answered: does the loan-level detail actually FOOT to the balance
-- the accounting system of record has certified?
--
-- This is the first tie-out a bank runs before an FR Y-14Q / CCAR submission,
-- because everything downstream inherits the answer. If the sub-ledger does
-- not agree with the GL there is no point asking whether the regulatory
-- extract agrees with anything.
--
-- MATERIALITY: a variance is escalated only when it exceeds 0.5% of the GL
-- balance. A zero-tolerance threshold turns every rounding artifact into a
-- break and buries the seven real ones in noise; 1% is loose enough to let a
-- genuine mis-load slide on a large portfolio. 0.5% is the conventional
-- working tolerance for a schedule-level tie-out. The number is repeated in
-- config.MATERIALITY_THRESHOLD_PCT and the test suite asserts the two agree.
--
-- ROOT CAUSE FROM VARIANCE DIRECTION: the sign of the variance is diagnostic
-- on its own, because the two ways a feed goes wrong push in opposite
-- directions.
--   sub-ledger UNDER the GL  ->  detail is missing  ->  LATE_FEED
--                                (loans that did not land before cutoff)
--   sub-ledger OVER the GL   ->  detail is doubled  ->  DUPLICATE_LOAD
--                                (a batch replayed into the warehouse)
-- The duplicate-loan-id count carried alongside is corroborating evidence,
-- not the test itself: a replayed batch re-inserts the same loan_ids, so a
-- positive variance sitting next to duplicated ids is a near-certain replay.
--
-- PORTABILITY NOTE: this file runs unchanged on both SQLite and Snowflake.
-- Two things make that true and are easy to undo by accident. The CTE is named
-- variance_calc rather than variance, because VARIANCE is a built-in aggregate
-- in Snowflake and a bare CTE of that name invites the parser to resolve the
-- wrong thing. And the outer SELECT lists its columns explicitly instead of
-- using alias.*, so a reader can see the output contract without executing it
-- and neither engine has to guess at expansion order.
-- ===========================================================================

WITH subledger_rollup AS (
    -- Roll the loan-level detail up to the GL's grain. This is the only place
    -- the grains differ, and getting it wrong is how tie-outs quietly lie.
    SELECT
        entity,
        product,
        quarter,
        ROUND(SUM(loan_amount), 2) AS sub_ledger_balance,
        COUNT(*)                   AS loan_count,
        COUNT(DISTINCT loan_id)    AS distinct_loan_count
    FROM sub_ledger
    GROUP BY entity, product, quarter
),

variance_calc AS (
    -- LEFT JOIN from the GL, not an inner join: a GL account whose entire
    -- sub-ledger feed failed to arrive has NO rollup row at all, and an inner
    -- join would drop it silently. That is the worst possible failure - a
    -- 100% missing feed reported as "nothing to see here".
    SELECT
        gl.entity,
        gl.product,
        gl.quarter,
        ROUND(gl.gl_balance, 2)                              AS gl_balance,
        ROUND(COALESCE(sl.sub_ledger_balance, 0), 2)         AS sub_ledger_balance,
        COALESCE(sl.loan_count, 0)                           AS loan_count,
        COALESCE(sl.loan_count, 0)
            - COALESCE(sl.distinct_loan_count, 0)            AS duplicate_loan_ids,
        ROUND(COALESCE(sl.sub_ledger_balance, 0) - gl.gl_balance, 2) AS variance
    FROM general_ledger gl
    LEFT JOIN subledger_rollup sl
           ON sl.entity  = gl.entity
          AND sl.product = gl.product
          AND sl.quarter = gl.quarter
),

flagged AS (
    SELECT
        entity,
        product,
        quarter,
        gl_balance,
        sub_ledger_balance,
        loan_count,
        duplicate_loan_ids,
        variance,
        ROUND(100.0 * variance / NULLIF(gl_balance, 0), 4) AS variance_pct,
        CASE
            -- A zero GL balance with a non-zero sub-ledger cannot be expressed
            -- as a percentage, so it is escalated on the absolute value alone
            -- rather than disappearing into a NULL comparison.
            WHEN gl_balance = 0 AND variance <> 0 THEN 'BREAK'
            WHEN ABS(100.0 * variance / NULLIF(gl_balance, 0)) > 0.5 THEN 'BREAK'
            ELSE 'PASS'
        END AS status
    FROM variance_calc
)

SELECT
    entity,
    product,
    quarter,
    gl_balance,
    sub_ledger_balance,
    loan_count,
    duplicate_loan_ids,
    variance,
    variance_pct,
    status,
    CASE
        WHEN status = 'PASS'  THEN 'NONE'
        WHEN loan_count = 0   THEN 'FEED_NOT_RECEIVED'
        WHEN variance < 0     THEN 'LATE_FEED'
        WHEN variance > 0     THEN 'DUPLICATE_LOAD'
        ELSE 'UNCLASSIFIED'
    END AS root_cause,
    CASE
        WHEN status = 'PASS'  THEN 'No action'
        WHEN loan_count = 0   THEN 'Escalate to source-system owner: no detail received at all'
        WHEN variance < 0     THEN 'Confirm load cutoff and re-run the missing batches'
        WHEN variance > 0     THEN 'Identify the replayed batch_id and reverse the duplicate load'
        ELSE 'Manual review'
    END AS suggested_action
FROM flagged
ORDER BY
    CASE WHEN status = 'BREAK' THEN 0 ELSE 1 END,
    ABS(COALESCE(variance_pct, 0)) DESC,
    quarter, entity, product;
