-- =============================================================================
-- Login history -- SQL Server schema.
--
-- Durable store for daily login counts scraped out of Loki. Loki retains ~30
-- days; these tables are the permanent record, so Grafana can show years.
--
-- Idempotent -- safe to re-run.
--   sqlcmd -S <server> -d <database> -i schema.sql
-- =============================================================================

SET NOCOUNT ON;
GO

-- -----------------------------------------------------------------------------
-- gw_login_daily -- one row per (day, env, product). The grain the dashboard
-- reads. [day] is a calendar day in REPORT_TIMEZONE, matching the emailed
-- report; if that variable is unset both use UTC.
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.gw_login_daily', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.gw_login_daily
    (
        [day]          DATE         NOT NULL,
        env            VARCHAR(32)  NOT NULL,   -- Loki `env` label, e.g. DEV1
        product        VARCHAR(8)   NOT NULL,   -- pc | bc | cc | cm
        logins         BIGINT       NOT NULL,   -- login events
        distinct_users INT          NOT NULL,   -- unique usernames that day
        collected_at   DATETIME2(0) NOT NULL
            CONSTRAINT DF_gw_login_daily_collected_at DEFAULT (SYSUTCDATETIME()),

        -- Companion timestamp for Grafana. Its $__timeFilter macro is a text
        -- substitution that stops at the first ')', so it cannot wrap an
        -- expression like CAST([day] AS DATETIME2) -- it needs a bare column.
        -- The macro also emits an RFC3339 literal ('2026-01-01T00:00:00Z'),
        -- which DATETIME2 accepts and DATE comparisons handle inconsistently.
        day_ts AS CAST([day] AS DATETIME2(0)) PERSISTED,

        CONSTRAINT PK_gw_login_daily PRIMARY KEY CLUSTERED ([day], env, product),
        CONSTRAINT CK_gw_login_daily_logins CHECK (logins >= 0),
        CONSTRAINT CK_gw_login_daily_users  CHECK (distinct_users >= 0)
    );

    CREATE NONCLUSTERED INDEX IX_gw_login_daily_env_product_day
        ON dbo.gw_login_daily (env, product, [day]) INCLUDE (logins, distinct_users);

    CREATE NONCLUSTERED INDEX IX_gw_login_daily_day_ts
        ON dbo.gw_login_daily (day_ts) INCLUDE (env, product, logins, distinct_users);
END
GO

-- -----------------------------------------------------------------------------
-- gw_login_user_daily -- one row per (day, env, product, username).
--
-- Exists because daily distinct counts CANNOT be summed into a monthly or
-- quarterly distinct count. Keeping the usernames is the only way to answer
-- "how many unique people used UAT1 last quarter" -- or "who".
--
-- Usernames LOGIN_USER_REGEX could not extract are stored as '(unparsed)' so
-- these rows still sum to gw_login_daily.logins. That sentinel is excluded
-- from distinct_users.
--
-- NOTE: this table holds usernames, so it falls under whatever data-retention
-- and access policy covers user identifiers. Grafana reads it through a
-- read-only login (see grants.sql).
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.gw_login_user_daily', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.gw_login_user_daily
    (
        [day]        DATE          NOT NULL,
        env          VARCHAR(32)   NOT NULL,
        product      VARCHAR(8)    NOT NULL,
        username     NVARCHAR(128) NOT NULL,
        logins       INT           NOT NULL,
        collected_at DATETIME2(0)  NOT NULL
            CONSTRAINT DF_gw_login_user_daily_collected_at DEFAULT (SYSUTCDATETIME()),

        day_ts AS CAST([day] AS DATETIME2(0)) PERSISTED,

        CONSTRAINT PK_gw_login_user_daily
            PRIMARY KEY CLUSTERED ([day], env, product, username),
        CONSTRAINT CK_gw_login_user_daily_logins CHECK (logins >= 0)
    );

    CREATE NONCLUSTERED INDEX IX_gw_login_user_daily_user
        ON dbo.gw_login_user_daily (username, [day]) INCLUDE (env, product, logins);

    CREATE NONCLUSTERED INDEX IX_gw_login_user_daily_day_ts
        ON dbo.gw_login_user_daily (day_ts) INCLUDE (env, product, username, logins);
END
GO

-- -----------------------------------------------------------------------------
-- gw_login_collector_run -- one row per collector execution.
--
-- The silent failure mode of this design is nasty: if the collector stops
-- running the dashboard just quietly stops growing, and once the gap falls
-- outside Loki retention the data is gone forever. The freshness panel and
-- the staleness alert read this.
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.gw_login_collector_run', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.gw_login_collector_run
    (
        run_id       BIGINT IDENTITY(1,1) NOT NULL,
        started_at   DATETIME2(0)   NOT NULL,
        finished_at  DATETIME2(0)   NULL,
        status       VARCHAR(16)    NOT NULL,   -- running | ok | partial | failed
        days_from    DATE           NULL,
        days_to      DATE           NULL,
        rows_written INT            NOT NULL CONSTRAINT DF_gw_run_rows DEFAULT (0),
        query_errors INT            NOT NULL CONSTRAINT DF_gw_run_errs DEFAULT (0),
        build_id     VARCHAR(64)    NULL,
        detail       NVARCHAR(2000) NULL,
        CONSTRAINT PK_gw_login_collector_run PRIMARY KEY CLUSTERED (run_id)
    );

    CREATE NONCLUSTERED INDEX IX_gw_login_collector_run_started
        ON dbo.gw_login_collector_run (started_at DESC);
END
GO

-- -----------------------------------------------------------------------------
-- Convenience views.
-- -----------------------------------------------------------------------------
CREATE OR ALTER VIEW dbo.gw_login_monthly
AS
SELECT
    DATEFROMPARTS(YEAR([day]), MONTH([day]), 1) AS [month],
    CAST(DATEFROMPARTS(YEAR([day]), MONTH([day]), 1) AS DATETIME2(0)) AS month_ts,
    env,
    product,
    SUM(logins) AS logins,
    COUNT(*)    AS days_with_data
FROM dbo.gw_login_daily
GROUP BY DATEFROMPARTS(YEAR([day]), MONTH([day]), 1),
         CAST(DATEFROMPARTS(YEAR([day]), MONTH([day]), 1) AS DATETIME2(0)),
         env, product;
GO

-- True distinct users per month -- only possible because usernames are stored.
-- Summing gw_login_daily.distinct_users would double-count anyone active on
-- more than one day.
CREATE OR ALTER VIEW dbo.gw_login_monthly_users
AS
SELECT
    DATEFROMPARTS(YEAR([day]), MONTH([day]), 1) AS [month],
    CAST(DATEFROMPARTS(YEAR([day]), MONTH([day]), 1) AS DATETIME2(0)) AS month_ts,
    env,
    product,
    COUNT(DISTINCT username) AS distinct_users
FROM dbo.gw_login_user_daily
WHERE username <> '(unparsed)'
GROUP BY DATEFROMPARTS(YEAR([day]), MONTH([day]), 1),
         CAST(DATEFROMPARTS(YEAR([day]), MONTH([day]), 1) AS DATETIME2(0)),
         env, product;
GO

CREATE OR ALTER VIEW dbo.gw_login_freshness
AS
SELECT
    env,
    product,
    MAX([day]) AS last_day,
    DATEDIFF(DAY, MAX([day]), CAST(SYSUTCDATETIME() AS DATE)) AS days_behind
FROM dbo.gw_login_daily
GROUP BY env, product;
GO
