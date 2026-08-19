-- ===========================================================================
-- RECONCILIATION 2 OF 3 : GENERAL LEDGER  vs  REGULATORY EXTRACT
-- Engine: SNOWFLAKE  (native rewrite of sql/02_gl_vs_regulatory_extract.sql)
-- ===========================================================================
-- Same question, same output contract, same 0.5% materiality threshold. The
-- only difference is the join, and the difference is worth the extra file.
--
-- ---------------------------------------------------------------------------
-- WHAT THIS FILE DELETES
-- ---------------------------------------------------------------------------
-- The SQLite version cannot say FULL OUTER JOIN, so it builds one out of two
-- LEFT JOINs stitched together with UNION and an anti-join predicate:
--
--     SELECT ... FROM gl LEFT JOIN rx ON <3 predicates>
--     UNION
--     SELECT ... FROM rx LEFT JOIN gl ON <3 predicates>  -- the same 3, again
--     WHERE gl.entity IS NULL                            -- load-bearing
--
-- That is a 26-line CTE: two scans of each side, the three-part join key
-- written out twice, and one WHERE clause that silently doubles every matched
-- account if anyone deletes it. Snowflake expresses the identical semantics in
-- a single join:
--
--     FROM gl FULL OUTER JOIN rx ON <3 predicates>
--
-- The join key is stated once, so it cannot drift out of step between the two
-- branches - which is the actual maintenance hazard in the simulated version,
-- not the line count. (For the record the whole file is 22 SQL lines shorter,
-- 100 against 122, but the deleted lines are the ones that could go wrong.)
--
-- ---------------------------------------------------------------------------
-- WHAT A FULL OUTER JOIN COSTS YOU IN EXCHANGE
-- ---------------------------------------------------------------------------
-- On an unmatched row, EVERY column from the absent side is NULL - including
-- the join keys. So gl.entity is NULL exactly when the account is extract-only,
-- and referring to gl.entity below would silently drop those rows from the
-- result. COALESCE(gl.entity, rx.entity) is not defensive noise here; it is the
-- correct way to read a key out of a full outer join, and forgetting it turns
-- the one join that was supposed to catch both one-sided failures back into
-- something that catches neither.
--
-- ---------------------------------------------------------------------------
-- COLUMN CASING
-- ---------------------------------------------------------------------------
-- Every identifier here is unquoted, so Snowflake resolves it case-insensitively
-- against the uppercase names in the catalog and returns UPPERCASE column
-- labels - where SQLite would return them lowercase. Quoting the aliases to
-- force lowercase would work but would make this file's aliases fragile in a
-- different way. It is handled once, centrally, in src/backends.py, which
-- lowercases every result frame so no consumer needs to know which engine ran.
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

variance_calc AS (
    SELECT
        -- COALESCE on the keys, not gl.entity: see the note above. On an
        -- extract-only row every gl.* column is NULL, join keys included.
        COALESCE(gl.entity,  rx.entity)  AS entity,
        COALESCE(gl.product, rx.product) AS product,
        COALESCE(gl.quarter, rx.quarter) AS quarter,
        -- Presence flags captured before the COALESCE below, because after it
        -- a missing side and a genuine zero balance are the same number.
        CASE WHEN gl.gl_balance      IS NULL THEN 1 ELSE 0 END AS missing_from_gl,
        CASE WHEN rx.extract_balance IS NULL THEN 1 ELSE 0 END AS missing_from_extract,
        COALESCE(gl.gl_balance, 0)      AS gl_balance,
        COALESCE(rx.extract_balance, 0) AS extract_balance,
        ROUND(COALESCE(rx.extract_balance, 0) - COALESCE(gl.gl_balance, 0), 2) AS variance
    FROM gl
    FULL OUTER JOIN rx
                 ON rx.entity  = gl.entity
                AND rx.product = gl.product
                AND rx.quarter = gl.quarter
),

flagged AS (
    SELECT
        entity,
        product,
        quarter,
        missing_from_gl,
        missing_from_extract,
        gl_balance,
        extract_balance,
        variance,
        ROUND(100.0 * variance / NULLIF(gl_balance, 0), 4) AS variance_pct,
        CASE
            WHEN missing_from_gl = 1 OR missing_from_extract = 1 THEN 'BREAK'
            WHEN gl_balance = 0 AND variance <> 0 THEN 'BREAK'
            WHEN ABS(100.0 * variance / NULLIF(gl_balance, 0)) > 0.5 THEN 'BREAK'
            ELSE 'PASS'
        END AS status
    FROM variance_calc
),

offset_search AS (
    -- Identical to the SQLite version: a mapping error re-tags balance onto
    -- another product inside the same entity and quarter, so it always shows up
    -- as a pair of variances that net to zero. A timing difference has no
    -- partner, because that balance never reached the filing at all.
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
        WHEN f.status = 'PASS'              THEN 'NONE'
        WHEN f.missing_from_extract = 1     THEN 'MISSING_FROM_EXTRACT'
        WHEN f.missing_from_gl = 1          THEN 'NOT_IN_GL'
        WHEN COALESCE(os.has_offset, 0) = 1 THEN 'MAPPING_ERROR'
        WHEN f.variance < 0                 THEN 'TIMING_DIFFERENCE'
        ELSE 'UNEXPLAINED_OVERSTATEMENT'
    END AS root_cause,
    os.offset_product AS offsetting_product,
    CASE
        WHEN f.status = 'PASS'              THEN 'No action'
        WHEN f.missing_from_extract = 1     THEN 'GL balance never reached the filing - re-run the extract'
        WHEN f.missing_from_gl = 1          THEN 'Filed balance has no GL support - investigate before submission'
        WHEN COALESCE(os.has_offset, 0) = 1 THEN 'Correct the product mapping and refile the affected schedules'
        WHEN f.variance < 0                 THEN 'Late GL adjustment - re-run the extract after the posting date'
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
