-- ===========================================================================
-- RECONCILIATION 2 OF 3 : GENERAL LEDGER  vs  REGULATORY EXTRACT
-- Engine: SQLite (local development)
-- A Snowflake-native rewrite of this same query lives in
-- sql/snowflake/02_gl_vs_regulatory_extract_snowflake.sql
-- ===========================================================================
-- Question answered: does the number that actually reached the regulator
-- match the number the GL certified?
--
-- Reconciliation 1 proves the detail foots to the GL. It says nothing about
-- what got FILED. Balance can still be lost or mis-tagged between the GL and
-- the reporting tool, and this is the tie-out that catches it.
--
-- ---------------------------------------------------------------------------
-- WHY THE JOIN LOOKS LIKE THIS: SQLITE HAS NO FULL OUTER JOIN
-- ---------------------------------------------------------------------------
-- This reconciliation must see BOTH one-sided failures:
--   * in the GL but absent from the extract  -> a balance never got filed
--   * in the extract but absent from the GL  -> a balance was filed out of
--                                               thin air
-- A LEFT JOIN sees only the first. An INNER JOIN sees neither, and an
-- unfiled balance is exactly the sort of break that must never be silently
-- dropped by the tool meant to find it.
--
-- SQLite (before 3.39) has no FULL OUTER JOIN, so it is simulated the
-- standard way: the LEFT JOIN in one direction, UNION'd with the LEFT JOIN in
-- the other direction restricted to rows that found no match. The second
-- branch contributes only the extract-only rows, so the union is the full
-- outer join. UNION rather than UNION ALL is belt-and-braces - the anti-join
-- predicate already guarantees the branches are disjoint.
--
-- This is the single largest readability cost of using SQLite here, and it is
-- the piece the Snowflake version deletes outright.
--
-- ---------------------------------------------------------------------------
-- TELLING A TIMING DIFFERENCE APART FROM A MAPPING ERROR
-- ---------------------------------------------------------------------------
-- Both failure modes show up as "the extract disagrees with the GL", so a
-- variance on its own does not name the cause. What separates them is whether
-- the money went anywhere:
--
--   TIMING DIFFERENCE - a GL adjustment posted after the extract had already
--     run. The balance is simply absent from the filing. The variance is
--     one-sided; nothing else in that entity and quarter moved.
--
--   MAPPING ERROR - loans were tagged to the wrong product code. The balance
--     did not vanish, it landed on a different product inside the SAME entity
--     and quarter. So the variance always arrives as a PAIR that nets to
--     zero: one product short by exactly what another is long.
--
-- The offset_search CTE looks for that partner row. Finding one means the
-- money is still in the entity and simply sitting under the wrong label -
-- a mapping error. Finding none means the money never made the filing at all
-- - a timing difference. The distinction matters operationally: a mapping
-- error is fixed by re-tagging and refiling, a timing difference by re-running
-- the extract after the adjustment posts.
-- ===========================================================================

WITH gl AS (
    SELECT entity, product, quarter,
           ROUND(SUM(gl_balance), 2) AS gl_balance
    FROM general_ledger
    GROUP BY entity, product, quarter
),

rx AS (
    SELECT entity, product, quarter,
           ROUND(SUM(extract_balance), 2) AS extract_balance
    FROM regulatory_extract
    GROUP BY entity, product, quarter
),

full_outer AS (
    -- Branch 1: every GL account, with its extract balance where one exists.
    SELECT
        gl.entity,
        gl.product,
        gl.quarter,
        gl.gl_balance,
        rx.extract_balance
    FROM gl
    LEFT JOIN rx
           ON rx.entity  = gl.entity
          AND rx.product = gl.product
          AND rx.quarter = gl.quarter

    UNION

    -- Branch 2: the extract rows that have NO GL account behind them. The
    -- IS NULL predicate is what makes this an anti-join, and it is what stops
    -- branch 2 from re-emitting the matched rows branch 1 already produced.
    SELECT
        rx.entity,
        rx.product,
        rx.quarter,
        gl.gl_balance,
        rx.extract_balance
    FROM rx
    LEFT JOIN gl
           ON gl.entity  = rx.entity
          AND gl.product = rx.product
          AND gl.quarter = rx.quarter
    WHERE gl.entity IS NULL
),

variance AS (
    SELECT
        entity,
        product,
        quarter,
        -- The presence flags are captured BEFORE the COALESCE, because after
        -- it a genuinely missing side and a real zero balance are the same
        -- number and the difference is unrecoverable.
        CASE WHEN gl_balance      IS NULL THEN 1 ELSE 0 END AS missing_from_gl,
        CASE WHEN extract_balance IS NULL THEN 1 ELSE 0 END AS missing_from_extract,
        COALESCE(gl_balance, 0)      AS gl_balance,
        COALESCE(extract_balance, 0) AS extract_balance,
        ROUND(COALESCE(extract_balance, 0) - COALESCE(gl_balance, 0), 2) AS variance
    FROM full_outer
),

flagged AS (
    SELECT
        variance.*,
        ROUND(100.0 * variance / NULLIF(gl_balance, 0), 4) AS variance_pct,
        CASE
            -- A one-sided row is a break regardless of magnitude: there is no
            -- materiality tolerance for a balance that was never filed.
            WHEN missing_from_gl = 1 OR missing_from_extract = 1 THEN 'BREAK'
            WHEN gl_balance = 0 AND variance <> 0 THEN 'BREAK'
            WHEN ABS(100.0 * variance / NULLIF(gl_balance, 0)) > 0.5 THEN 'BREAK'
            ELSE 'PASS'
        END AS status
    FROM variance
),

offset_search AS (
    -- For each break, hunt for a partner break in the same entity and quarter
    -- whose variance is equal and opposite. ABS(a + b) < 0.01 is the test for
    -- "these two net to zero at the cent" - a tolerance, not equality, because
    -- the two sides are independently rounded floats.
    SELECT
        f.entity,
        f.product,
        f.quarter,
        MAX(CASE WHEN o.product IS NOT NULL THEN 1 ELSE 0 END) AS has_offset,
        MAX(o.product)                                         AS offset_product
    FROM flagged f
    LEFT JOIN flagged o
           ON o.entity   = f.entity
          AND o.quarter  = f.quarter
          AND o.product <> f.product
          AND o.status   = 'BREAK'
          AND o.variance <> 0
          AND ABS(o.variance + f.variance) < 0.01
    WHERE f.status = 'BREAK'
    GROUP BY f.entity, f.product, f.quarter
)

SELECT
    f.entity,
    f.product,
    f.quarter,
    f.gl_balance,
    f.extract_balance,
    f.variance,
    f.variance_pct,
    f.status,
    CASE
        WHEN f.status = 'PASS'           THEN 'NONE'
        WHEN f.missing_from_extract = 1  THEN 'MISSING_FROM_EXTRACT'
        WHEN f.missing_from_gl = 1       THEN 'NOT_IN_GL'
        WHEN COALESCE(os.has_offset, 0) = 1 THEN 'MAPPING_ERROR'
        WHEN f.variance < 0              THEN 'TIMING_DIFFERENCE'
        ELSE 'UNEXPLAINED_OVERSTATEMENT'
    END AS root_cause,
    os.offset_product AS offsetting_product,
    CASE
        WHEN f.status = 'PASS'           THEN 'No action'
        WHEN f.missing_from_extract = 1  THEN 'GL balance never reached the filing - re-run the extract'
        WHEN f.missing_from_gl = 1       THEN 'Filed balance has no GL support - investigate before submission'
        WHEN COALESCE(os.has_offset, 0) = 1 THEN 'Correct the product mapping and refile the affected schedules'
        WHEN f.variance < 0              THEN 'Late GL adjustment - re-run the extract after the posting date'
        ELSE 'Manual review'
    END AS suggested_action
FROM flagged f
LEFT JOIN offset_search os
       ON os.entity  = f.entity
      AND os.product = f.product
      AND os.quarter = f.quarter
ORDER BY
    CASE WHEN f.status = 'BREAK' THEN 0 ELSE 1 END,
    ABS(COALESCE(f.variance_pct, 0)) DESC,
    f.quarter, f.entity, f.product;
