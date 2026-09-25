# Deployment runbook — login collector

From an empty database to a Grafana dashboard with seeded history and a daily
pipeline keeping it current.

Everything is additive — rollback is at the bottom and takes two minutes.

---

## Phase 0 — Blocking checks

### 0.1 Database engine

Everything here is T-SQL and the dashboard uses Grafana's MSSQL datasource.

```bash
sqlcmd -S <server> -d <database> -Q "SELECT @@VERSION"
```

Anything other than Microsoft SQL Server → stop; the schema and the ten panel
queries need a dialect pass first.

### 0.2 Can the collector account create tables?

```bash
sqlcmd -S <server> -d <database> -Q "SELECT HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE TABLE') AS can_create"
```

`1` → proceed. `0` → a DBA runs `schema.sql` once; afterwards the account only
needs what `grants.sql` gives it.

### 0.3 Is the database backed up?

**Confirm this before seeding.** Once a day ages out of Loki's ~30-day window
these tables are the only copy and nothing can regenerate them. A machine
rebuild, a refresh-from-prod, or a snapshot restore silently destroys the whole
history.

If it isn't backed up, either pick a database that is, or add a periodic export
of `gw_login_daily` and `gw_login_user_daily` published as a build artifact.

### 0.4 Agent reachability

```powershell
Test-NetConnection <loki-host> -Port 3100
```

```powershell
Test-NetConnection <sql-host> -Port 1433
```

### 0.5 pyodbc and the ODBC driver

```powershell
Get-OdbcDriver -Name "*SQL Server*" | Select-Object Name
```

```powershell
python -m pip download pyodbc -d $env:TEMP\pyodbc-probe
```

The second confirms the agent can actually reach PyPI. If it can't, vendor a
wheel into the repo and install from disk — the pipeline's install step is one
line to change.

---

## Phase 1 — Database objects

```bash
sqlcmd -S <server> -d <database> -i logins/sql/schema.sql
```

Edit the two login names at the top of `grants.sql` first — the collector
account, and a **new read-only** login for Grafana:

```bash
sqlcmd -S <server> -d <database> -i logins/sql/grants.sql
```

**Check** — expect seven objects:

```bash
sqlcmd -S <server> -d <database> -Q "SELECT name, type_desc FROM sys.objects WHERE name LIKE 'gw_login%' ORDER BY name"
```

---

## Phase 2 — Variable groups

Both pipelines read two groups. Neither needs creating from scratch.

**`gw-logins-db`** — the database credential. Point it at your existing DB group
by editing the `- group:` line in both YAMLs; it must supply `DBINSTANCE`,
`DBNAME`, `DBUSER` and `DBPASS` (secret).

**`gw-reports-secrets`** — the monthly report's group, **shared on purpose**.
Every value the collector must agree with the report on already lives there:

| Already in the group | Why it must be shared |
|---|---|
| `LOKI_URL`, `LOKI_PROJECT`, `LOKI_VERIFY_TLS`, `BYPASS_PROXY` | same Loki, same selector |
| `ENVS`, `ENVS_EXCLUDE`, `PRODUCTS`, `PRODUCT_JOBS`, `PRODUCT_FRAGS` | same streams |
| `LOGIN_USER_REGEX` | same usernames |
| `REPORT_TIMEZONE`, `REPORT_TZ_LABEL` | same day boundaries |

A copy would work on day one and drift afterwards: tune the regex in one group
and the dashboard silently stops reconciling with the spreadsheet. One group
makes that impossible.

> **`LOKI_PROJECT` especially.** If it is missing the code falls back to the
> placeholder `myproject`, Loki matches nothing, and every count is a
> successful-looking **0**. If the report works, the group already has it.

**Nothing new has to be added.** Every collector-only setting has a working
default, and an undefined `$(NAME)` is treated as unset. Add one to the group
only to override it:

| Variable | Default | Override when |
|---|---|---|
| `DB_TRUST_SERVER_CERT` | `false` | **Likely needed.** Driver 18 encrypts by default; an internal SQL cert the agent doesn't trust fails with an SSL error. `check` shows it. |
| `DB_ODBC_DRIVER` | `ODBC Driver 18 for SQL Server` | The agent only has Driver 17. `check` lists what is installed. |
| `DB_ENCRYPT` | `true` | Rarely. Prefer `DB_TRUST_SERVER_CERT`. |
| `DB_TRUSTED_CONNECTION` | `false` | The agent's service account has DB rights (Windows auth). |
| `DB_SCHEMA` | `dbo` | Tables live in another schema. |
| `LOOKBACK_DAYS` | `7` | Longer self-healing window. |
| `LOKI_RETENTION_DAYS` | `30` | Your Loki keeps more or less. |
| `LOKI_LOG_LIMIT` | `5000` | Rarely; paging makes it a performance knob, not a correctness one. |
| `STORE_USERNAMES` | `true` | You decide not to keep user identifiers. |

The group also holds the report's SMTP secrets. Those are never mapped into
either job's `env:` block, and ADO does not expose an unmapped secret to a
script, so the collector never sees them.

**Grant access.** Under **Library → group → Pipeline permissions**, authorize
both `GW-Login-Setup` and `GW-Login-Collector` on **both** groups. A missing
authorization fails the run at queue time, before any step runs.

---

## Phase 3 — Register the pipeline

**Pipelines → New → Existing YAML → `/logins/gw-login-collector.yaml`** → name it
`GW-Login-Collector` → **Save**, don't run.

Then **Edit → ⋯ → Triggers → Scheduled** and add a daily run (e.g. 03:00). The
YAML deliberately has no `schedules:` block — defining one makes ADO ignore UI
triggers.

---

## Phase 4 — The test ladder

Four runs, each proving one new thing. A failure at step 3 is only easy to
diagnose if 1 and 2 passed.

### Test 1 — Loki reachable, labels and regex correct *(writes nothing)*

**Run → tick `Dry run` → Run.**

- Non-zero logins **and** non-zero distinct users → selector and regex both good.
- Logins but zero distinct users → `LOGIN_USER_REGEX` doesn't match. The log
  prints sample unmatched lines; fix it before storing anything, or you'll
  backfill 30 days of `(unparsed)`.
- All zeros → wrong `ENVS` casing, `LOKI_PROJECT`, or `job` label.

### Test 2 — Database write path *(one day)*

Dry run never opens a database connection, so this is the first real test of
credentials, ODBC and the upsert.

**Run → `Backfill start` = yesterday, `Backfill end` = yesterday, `Dry run` off.**

```bash
sqlcmd -S <server> -d <database> -Q "SELECT * FROM dbo.gw_login_daily"
```

Then **re-run the identical job**. Row counts must not grow — that proves the
upsert is idempotent, which is what makes the daily lookback safe.

### Test 3 — Seed history *(time-critical)*

Loki holds ~30 days right now; whatever you don't capture is gone.

**Run → `Backfill start` = 30 days ago → Run.**

Expect this to take noticeably longer than the report: it pulls raw log lines
rather than counts, because that's what usernames require.

### Test 4 — Verify

```bash
sqlcmd -S <server> -d <database> -i logins/sql/verify.sql
```

| Section | Expected |
|---|---|
| 1. Coverage | ~30 days per env, plausible totals |
| 2. Freshness | `days_behind` = 1 |
| 3. Gaps | **empty** |
| 4. Per-user reconciliation | **empty** — user rows sum to daily totals |
| 5. Unparsed | small or empty; large means tune the regex |
| 6. Runs | `status = ok`, `query_errors = 0` |

---

## Phase 5 — Grafana

1. **Connections → Data sources → Add → Microsoft SQL Server**, using the
   **read-only** login — never the collector's.
2. **Dashboards → New → Import** → `grafana/gw-logins-dashboard.json` → pick
   that datasource.

Import *after* phase 4: the `Environment` and `Product` dropdowns are populated
from the tables, and against empty tables they render as `IN ()`, which SQL
Server rejects.

**Check** at *Last 90 days*: both dropdowns populated, daily series drawn,
*Collector lag* green at **1**, *Most active users* populated, *Collector runs*
green.

### Reconcile against the report

Run `monthly_report.py` for a month that's fully inside the collected range and
compare totals. They should match closely — both count events from the same
`|= "User Login"` match, both bucket days in `REPORT_TIMEZONE`, and the report
already labels each `count_over_time` sample with `timestamp - 1d`.

Remaining differences are worth chasing, not shrugging at: the usual causes are
a `REPORT_TIMEZONE` mismatch between the two variable groups, or the report
running with `INCLUDE_USER_DETAIL=false` (which counts via a different query).

---

## Phase 6 — Staleness alert *(do not skip)*

This design fails silently. A dead collector doesn't break the dashboard; it
just stops growing, and past ~30 days the gap is permanent.

**Grafana → Alerting → New alert rule:**

- **Query A** (MSSQL, *Table* format):
  ```sql
  SELECT ISNULL(MAX(days_behind), 9999) AS days_behind FROM dbo.gw_login_freshness;
  ```
- **Condition**: `WHEN Last() OF A IS ABOVE 2`
- **Evaluate** every `1h` for `2h`, routed somewhere a human reads.

`1` is steady state. `2` is one missed run. `3+` means act today.

---

## Phase 7 — Next morning

```bash
sqlcmd -S <server> -d <database> -i logins/sql/verify.sql
```

`days_behind` = 1, a new `ok` run, gaps and reconciliation still empty. That's
the deployment confirmed end to end.

---

## Rollback

Nothing above modifies an existing object.

1. Disable `GW-Login-Collector`.
2. Delete the Grafana dashboard and alert rule.
3. Only if abandoning the approach — **this destroys collected history
   permanently**:

```bash
sqlcmd -S <server> -d <database> -Q "DROP VIEW IF EXISTS dbo.gw_login_monthly; DROP VIEW IF EXISTS dbo.gw_login_monthly_users; DROP VIEW IF EXISTS dbo.gw_login_freshness; DROP TABLE IF EXISTS dbo.gw_login_user_daily; DROP TABLE IF EXISTS dbo.gw_login_daily; DROP TABLE IF EXISTS dbo.gw_login_collector_run;"
```

The monthly report is untouched throughout — it reads Loki directly and has no
dependency on any of this.
