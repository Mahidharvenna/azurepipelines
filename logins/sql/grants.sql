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
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '$(CollectorLogin)')
    CREATE USER [$(CollectorLogin)] FOR LOGIN [$(CollectorLogin)];
GO

GRANT SELECT, INSERT, UPDATE ON dbo.gw_login_daily         TO [$(CollectorLogin)];
GRANT SELECT, INSERT, UPDATE ON dbo.gw_login_collector_run TO [$(CollectorLogin)];
-- DELETE only on the per-user table: re-collecting a day must clear users who
-- no longer appear, otherwise a corrected day keeps phantom rows.
GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.gw_login_user_daily TO [$(CollectorLogin)];
GO

-- --- 2. Grafana: read only ---------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '$(GrafanaLogin)')
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
