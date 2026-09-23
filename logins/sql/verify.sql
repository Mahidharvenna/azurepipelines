-- =============================================================================
-- Post-deployment checks.
--   sqlcmd -S <server> -d <database> -i verify.sql
-- =============================================================================
SET NOCOUNT ON;

PRINT '--- 1. Coverage per environment -------------------------------------';
SELECT env, product,
       COUNT(*)    AS days_stored,
       MIN([day])  AS first_day,
       MAX([day])  AS last_day,
       SUM(logins) AS total_logins
FROM dbo.gw_login_daily
GROUP BY env, product
ORDER BY env, product;

PRINT '--- 2. Freshness (expect days_behind = 1 in steady state) ------------';
SELECT * FROM dbo.gw_login_freshness ORDER BY days_behind DESC, env;

PRINT '--- 3. Gaps: missing days inside the stored range --------------------';
-- Any row here is a day the collector never wrote. Still inside Loki retention?
-- Re-run a backfill covering it. Outside retention, it is gone for good.
WITH bounds AS (
    SELECT env, product, MIN([day]) AS lo, MAX([day]) AS hi
    FROM dbo.gw_login_daily GROUP BY env, product
),
cal AS (
    SELECT b.env, b.product, b.lo AS d, b.hi FROM bounds b
    UNION ALL
    SELECT c.env, c.product, DATEADD(DAY, 1, c.d), c.hi FROM cal c WHERE c.d < c.hi
)
SELECT c.env, c.product, c.d AS missing_day
FROM cal c
LEFT JOIN dbo.gw_login_daily g
       ON g.[day] = c.d AND g.env = c.env AND g.product = c.product
WHERE g.[day] IS NULL
ORDER BY c.env, c.product, c.d
OPTION (MAXRECURSION 32767);

PRINT '--- 4. Per-user rows must sum to the daily totals --------------------';
-- A mismatch means the per-user write failed partway, or a day was collected
-- before STORE_USERNAMES was switched on.
SELECT d.[day], d.env, d.product,
       d.logins                AS daily_logins,
       ISNULL(SUM(u.logins),0) AS user_row_logins,
       d.logins - ISNULL(SUM(u.logins),0) AS difference
FROM dbo.gw_login_daily d
LEFT JOIN dbo.gw_login_user_daily u
       ON u.[day] = d.[day] AND u.env = d.env AND u.product = d.product
GROUP BY d.[day], d.env, d.product, d.logins
HAVING d.logins <> ISNULL(SUM(u.logins), 0)
ORDER BY d.[day] DESC;

PRINT '--- 5. Unparsed usernames (tune LOGIN_USER_REGEX if large) -----------';
SELECT [day], env, product, logins AS unparsed_logins
FROM dbo.gw_login_user_daily
WHERE username = '(unparsed)'
ORDER BY [day] DESC;

PRINT '--- 6. Recent collector runs -----------------------------------------';
SELECT TOP 10 run_id, started_at, finished_at, status, days_from, days_to,
       rows_written, query_errors, build_id, detail
FROM dbo.gw_login_collector_run
ORDER BY started_at DESC;
