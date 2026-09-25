-- =============================================================================
-- Least-privilege grants for the login history tables.
--
-- TWO accounts, deliberately:
--   1. the pipeline's account  -- read + write
--   2. Grafana's account       -- read ONLY
--
-- Grafana must never write. Panels run ad-hoc SQL that anyone with dashboard
-- edit rights can change, and these tables are the only copy of the history --
-- Loki cannot rebuild them after ~30 days.
--
-- gw_login_user_daily holds usernames. Restrict the Grafana login to people
-- who are allowed to see user identifiers, or drop that GRANT and delete the
-- 'Most active users' panel.
--
-- Edit the two names below, then:
--   sqlcmd -S <server> -d <database> -i grants.sql
-- =============================================================================

:setvar CollectorLogin "your_collector_login"
:setvar GrafanaLogin   "your_grafana_readonly_login"
GO

-- --- 1. Collector: read + write ---------------------------------------------
-- Skipped when the collector IS the account running this script -- the usual
-- case, since that account just created the tables. SQL Server refuses a GRANT
-- to yourself, and if the account owns the database its user is 'dbo', so
-- CREATE USER would fail too. It already has every right below.
IF SUSER_NAME() = N'$(CollectorLogin)'
    PRINT 'Collector login $(CollectorLogin) is running this script and already has these rights -- skipped.';
ELSE IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'$(CollectorLogin)')
    CREATE USER [$(CollectorLogin)] FOR LOGIN [$(CollectorLogin)];
GO

IF SUSER_NAME() <> N'$(CollectorLogin)'
BEGIN
    GRANT SELECT, INSERT, UPDATE ON dbo.gw_login_daily         TO [$(CollectorLogin)];
    GRANT SELECT, INSERT, UPDATE ON dbo.gw_login_collector_run TO [$(CollectorLogin)];
    -- DELETE only on the per-user table: re-collecting a day must clear users
    -- who no longer appear, otherwise a corrected day keeps phantom rows.
    GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.gw_login_user_daily TO [$(CollectorLogin)];
END
GO

-- --- 2. Grafana: read only ---------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'$(GrafanaLogin)')
    CREATE USER [$(GrafanaLogin)] FOR LOGIN [$(GrafanaLogin)];
GO

GRANT SELECT ON dbo.gw_login_daily          TO [$(GrafanaLogin)];
GRANT SELECT ON dbo.gw_login_user_daily     TO [$(GrafanaLogin)];
GRANT SELECT ON dbo.gw_login_collector_run  TO [$(GrafanaLogin)];
GRANT SELECT ON dbo.gw_login_monthly        TO [$(GrafanaLogin)];
GRANT SELECT ON dbo.gw_login_monthly_users  TO [$(GrafanaLogin)];
GRANT SELECT ON dbo.gw_login_freshness      TO [$(GrafanaLogin)];

DENY INSERT, UPDATE, DELETE ON dbo.gw_login_daily         TO [$(GrafanaLogin)];
DENY INSERT, UPDATE, DELETE ON dbo.gw_login_user_daily    TO [$(GrafanaLogin)];
DENY INSERT, UPDATE, DELETE ON dbo.gw_login_collector_run TO [$(GrafanaLogin)];
GO

PRINT 'Grants applied. Collector = $(CollectorLogin) (rw), Grafana = $(GrafanaLogin) (ro).';
GO
