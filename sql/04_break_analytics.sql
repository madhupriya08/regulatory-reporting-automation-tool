-- ===========================================================================
-- RECONCILIATION 4 OF 4 : BREAK ANALYTICS  (window functions)
-- Engine: SQLite 3.25+ and Snowflake - one file, both engines
-- ===========================================================================
-- Queries 1 and 2 answer "which accounts broke". Query 3 answers "how did the
-- quarter go". Neither answers the question a controller actually has to
-- settle on Monday morning:
--
--     There are 14 breaks and time to clear four of them before the deadline.
--     Which four?
--
-- Answering that needs each break ranked against its peers, not just reported
-- on its own - and comparison against other rows is exactly what window
-- functions do that GROUP BY cannot. A GROUP BY collapses rows; a window
-- keeps every break visible while giving it a view of the others.
--
-- ---------------------------------------------------------------------------
-- WHAT EACH WINDOW ANSWERS
-- ---------------------------------------------------------------------------
--   RANK() OVER (PARTITION BY quarter ORDER BY break_amount DESC)
--       Where does this break sit against every other break in its quarter?
--       Rank 1 is where the work starts.
--
--   SUM(...) OVER (PARTITION BY quarter)
--       What share of the quarter's total exposure is this one break? Turns
--       "$1.1m" into "62% of everything at risk this quarter", which is the
--       form a reader can act on without doing arithmetic.
--
--   SUM(...) OVER (PARTITION BY quarter ORDER BY ... ROWS UNBOUNDED PRECEDING)
--       A running total down the ranked list - a Pareto curve. It answers
--       "how far down do I have to work to cover 80% of the money?" Usually
--       not far, and knowing exactly how far is the scheduling decision.
--
--   COUNT(*) OVER (PARTITION BY entity, product, reconciliation)
--       How many quarters has this account broken in this tie-out? One is an
--       incident. Two in a row is a broken process. Those go to different
--       people, so the report must tell them apart rather than showing a
--       chronic failure as if it were new.
--
--       COUNT(*) is correct here rather than COUNT(DISTINCT quarter) because
--       the input has exactly one row per entity/product/quarter/tie-out, so
--       the row count IS the quarter count. It is also the portable choice:
--       COUNT(DISTINCT ...) as a window function is not supported in SQLite
--       and is restricted in Snowflake.
--
--   LAG(...) OVER (PARTITION BY entity, product, reconciliation ORDER BY quarter)
--       What was this account's variance last quarter? A recurring break that
--       is shrinking is a remediation working; one that is growing is a
--       remediation that is not. NULL means the account is new to the list.
--
-- ---------------------------------------------------------------------------
-- PORTABILITY
-- ---------------------------------------------------------------------------
-- Window functions arrived in SQLite 3.25 (2018) and Snowflake has always had
-- them, so this file runs unchanged on both.
--
-- One naming rule carries over from queries 1-3: no CTE may be called
-- `variance`, because VARIANCE is a built-in aggregate in Snowflake and a bare
-- CTE of that name invites the parser to resolve the wrong thing. The rule is
-- about that one name, not about any particular replacement - queries 1 and 2
-- use variance_calc, this file uses subledger_variance and extract_variance,
-- and a test checks the rule rather than the spelling.
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
    FROM general_ledger GROUP BY entity, product, quarter
),

rx_rollup AS (
    SELECT entity, product, quarter, ROUND(SUM(extract_balance), 2) AS extract_balance
    FROM regulatory_extract GROUP BY entity, product, quarter
),

-- FULL OUTER JOIN simulated with two LEFT JOINs, as in sql/02. See that file
-- for why the anti-join predicate is kept even though UNION would mask its
-- absence.
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
        entity, product, quarter,
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

all_breaks AS (
    SELECT quarter, entity, product,
           'gl_vs_subledger' AS reconciliation,
           root_cause,
           variance,
           ABS(variance) AS break_amount
    FROM subledger_variance
    WHERE status = 'BREAK'
    UNION ALL
    SELECT quarter, entity, product,
           'gl_vs_regulatory_extract' AS reconciliation,
           'EXTRACT_VARIANCE' AS root_cause,
           variance,
           ABS(variance) AS break_amount
    FROM extract_variance
    WHERE status = 'BREAK'
),

windowed AS (
    SELECT
        quarter,
        entity,
        product,
        reconciliation,
        root_cause,
        variance,
        break_amount,

        -- Where this break ranks against its peers in the same quarter.
        -- RANK, not ROW_NUMBER, and deliberately WITHOUT the tie-breakers used
        -- for the running total below: two breaks of equal size genuinely are
        -- equally urgent, and inventing an order between them would tell the
        -- reader something untrue. The two sides of a mapping error share a
        -- rank, which is exactly right - they are one problem.
        RANK() OVER (
            PARTITION BY quarter
            ORDER BY break_amount DESC
        ) AS materiality_rank,

        -- Share of the quarter's total exposure.
        ROUND(
            100.0 * break_amount
            / SUM(break_amount) OVER (PARTITION BY quarter), 2
        ) AS pct_of_quarter_exposure,

        -- Pareto: cumulative share working down the ranked list.
        --
        -- The tie-breakers on entity/product/reconciliation are load-bearing,
        -- not tidiness. A mapping error produces two breaks of EXACTLY equal
        -- magnitude, and with ORDER BY break_amount alone the engine may place
        -- tied rows in either order - so the running total assigned to each of
        -- them would differ between runs, and between SQLite and Snowflake.
        -- A regulatory figure that changes depending on which engine printed
        -- it is not a figure. Ordering the frame fully makes it deterministic.
        ROUND(
            100.0 * SUM(break_amount) OVER (
                PARTITION BY quarter
                ORDER BY break_amount DESC, entity, product, reconciliation
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ) / SUM(break_amount) OVER (PARTITION BY quarter), 2
        ) AS cumulative_pct_of_exposure,

        -- Quarters this account has broken in this tie-out. One row per
        -- entity/product/quarter/tie-out, so the row count is the quarter count.
        COUNT(*) OVER (
            PARTITION BY entity, product, reconciliation
        ) AS quarters_with_breaks,

        -- Last quarter's variance for this same account and tie-out.
        LAG(variance) OVER (
            PARTITION BY entity, product, reconciliation
            ORDER BY quarter
        ) AS prior_quarter_variance
    FROM all_breaks
)

SELECT
    quarter,
    entity,
    product,
    reconciliation,
    root_cause,
    variance,
    break_amount,
    materiality_rank,
    pct_of_quarter_exposure,
    cumulative_pct_of_exposure,
    quarters_with_breaks,
    prior_quarter_variance,

    -- An account broken in more than one quarter is a process failure, not an
    -- incident, and belongs with a different owner.
    CASE
        WHEN quarters_with_breaks > 1 THEN 'RECURRING'
        ELSE 'NEW'
    END AS break_persistence,

    -- Direction of travel for a recurring break. Comparing ABSOLUTE variance
    -- because a break that flips sign has not improved - it has become a
    -- different problem, and shrinking toward zero is the only thing that
    -- counts as remediation working.
    CASE
        WHEN prior_quarter_variance IS NULL THEN 'FIRST_OCCURRENCE'
        WHEN ABS(variance) > ABS(prior_quarter_variance) THEN 'WORSENING'
        WHEN ABS(variance) < ABS(prior_quarter_variance) THEN 'IMPROVING'
        ELSE 'UNCHANGED'
    END AS trend_vs_prior_quarter,

    -- Work queue: the Pareto cut-off - the SMALLEST set of breaks whose
    -- combined value reaches 80% of the quarter's exposure.
    --
    -- The subtraction matters. Testing `cumulative <= 80` looks equivalent and
    -- is wrong: it excludes the very row that carries the total past 80%, so
    -- the "top 80%" bucket reliably adds up to less than 80%. Subtracting this
    -- row's own share asks the right question - was the total still under 80%
    -- BEFORE this row? - which includes the crossing row and makes the label
    -- true to its name.
    CASE
        WHEN cumulative_pct_of_exposure - pct_of_quarter_exposure < 80.0
            THEN 'PRIORITY_1_TOP_80PCT'
        ELSE 'PRIORITY_2_TAIL'
    END AS remediation_priority
FROM windowed
ORDER BY quarter, materiality_rank, entity, product, reconciliation;
