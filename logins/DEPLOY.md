# Deployment runbook — login collector

From an empty database to a Grafana dashboard with seeded history and a daily
pipeline keeping it current.

Both pipelines run in the **`DevopsAutomation`** agent pool, on **Linux agents
only** (`demands: Agent.OS -equals Linux`).
Everything is additive — rollback is at the bottom and takes two minutes.

The order matters: the setup pipeline needs the variable groups, and the
collector needs the tables.

The folder can live anywhere in your repo — `logins/` at the root, or deeper
such as `Grafana/logins/`. Each pipeline's first step finds it and checks every
file it needs is committed.

| Phase | What | Who |
|---|---|---|
| 0 | Agent prerequisites | an admin with root, once |
| 1 | Variable groups | you, in TFS |
| 2 | Database setup — `GW-Login-Setup` | you, in TFS |
| 3 | Register and schedule `GW-Login-Collector` | you, in TFS |
| 4 | Test ladder — four runs | you |
| 5 | Grafana datasource and dashboard | you |
| 6 | Staleness alert | you |
| 7 | Next-morning check | you |

---

## Phase 0 — The Linux agent *(usually nothing to do)*

### 0.1 What the agent needs

| Needs | Why | Without it |
|---|---|---|
| Python **3.9+** with its `venv` module | runs the scripts; job-local installs (system pip is blocked by PEP 668 on newer Debian/Ubuntu and too old on RHEL 8) | `Prepare Python` fails and names the fix |
| Python **3.12 or 3.9** — or else a route to a package index | `pymssql` is installed from wheels committed in `tools/wheels/` (Linux x86_64, Python 3.12 and 3.9), no network needed; any other Python version falls back to PyPI or a mirror | `Prepare Python` warns; `check` fails. See 0.3 |
| Network to Loki and SQL Server | the job itself | `check` names which one |

**No SQL Server driver is needed.** `pymssql`'s Linux wheel carries its own
SQL Server client — FreeTDS compiled in, plus OpenSSL and Kerberos — the same
way .NET's `System.Data.SqlClient` does for PowerShell tasks. `pip install` is
the whole install; nothing needs root.

Run `GW-Login-Setup` with `action=check` (phase 2) to confirm: it reports
everything missing in one run.

### 0.2 Only if Python 3.9+ is missing *(needs root)*

```bash
# RHEL / Rocky / Alma 8 or 9 -- RHEL 8's default python3 is 3.6, too old
sudo dnf install -y python3.11
# Ubuntu / Debian
sudo apt-get update && sudo apt-get install -y python3 python3-venv
```

**Ubuntu 20.04:** its `python3` is 3.8. Install `python3.9 python3.9-venv`
instead; `Prepare Python` picks the newest version on `PATH`. Debian 10 and
older have no 3.9 package and are past end of life.

### 0.3 Python other than 3.12 or 3.9, and no route to PyPI?

`pymssql` is installed from the wheels in `tools/wheels/`, so a Python 3.12 or
3.9 agent needs no network at all. For another version, either add its wheel
there (the folder's README says how), or give the job an index:

- add **`PIP_INDEX_URL`** (an internal PyPI mirror — Nexus, Artifactory, an Azure
  Artifacts feed) to `gw-reports-secrets`. If the URL carries a token, make it a
  **secret** variable: both YAMLs map it into *Prepare Python* explicitly, which
  is the only way ADO passes a secret to a script. pip masks the password in its
  own output.
- or add **`HTTPS_PROXY`** the same way. Loki calls are unaffected — they bypass
  the proxy explicitly when `BYPASS_PROXY=true`.

### 0.4 Loki on an internal CA?

Python on **Windows** trusts the Windows certificate store; on **Linux** it
trusts only the OpenSSL bundle. If Loki's certificate comes from an internal CA,
either add the CA to the agent's OS store:

```bash
# RHEL family
sudo cp corp-root.pem /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust
# Ubuntu / Debian -- file must be PEM and end in .crt
sudo cp corp-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates
```

or set `LOKI_CA_BUNDLE=/path/to/ca.pem`, or — as your group does today —
`LOKI_VERIFY_TLS=false`.

SQL Server's certificate is a different matter: `pymssql` encrypts the
connection (`DB_ENCRYPT=true`) but does not verify the server's certificate, so
an internal CA there needs nothing.

### 0.5 Python outside `/usr/bin`? Refresh the agent's PATH

The commands above put Python in `/usr/bin`, which the agent already searches —
nothing more to do.

If Python went somewhere else (`/usr/local/bin` from a source build, `/opt/...`),
know that the agent service does **not** use your shell's `PATH`. It uses a
snapshot in the `.path` file in the agent directory, written by `./env.sh` when
the agent was configured, and a restart alone re-reads the old snapshot. As the
agent's user, in the agent directory:

```bash
export PATH=/path/to/python/bin:$PATH   # skip if that user's login shell already has it
./env.sh                                # re-snapshots PATH into .path
cat .path                               # confirm the directory is listed
sudo ./svc.sh stop && sudo ./svc.sh start
```

`Prepare Python` prints the `PATH` it actually sees, so you can check.

### 0.6 More than one Linux agent in `DevopsAutomation`?

The demand only guarantees *a* Linux agent. Each one that can take the job
needs 0.1; to pin both pipelines to one, add a second demand in each YAML:

```yaml
  demands:
  - Agent.OS -equals Linux
  - Agent.Name -equals <agent-name>
```

### 0.7 Is the database backed up?

**Confirm before seeding.** Once a day ages out of Loki's ~30-day window these
tables are the only copy and nothing can regenerate them. A machine rebuild, a
refresh-from-prod, or a snapshot restore silently destroys the whole history.

If it isn't backed up, pick a database that is, or add a periodic export of
`gw_login_daily` and `gw_login_user_daily` published as a build artifact.

---

## Phase 1 — Variable groups

Both pipelines read two groups. Neither needs creating from scratch.

**`Dashboard DB`** — the existing database credential group, used as-is so the
password lives in one place. It supplies `DBINSTANCE`, `DBNAME`, `DBUSER` and
`DBPASS` (secret). `DBINSTANCE` takes SQL Server's own
syntax — `host`, `host,port`, or `host\instance` — never `host:port`.

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
default, and an undefined `$(NAME)` is treated as unset. Add one only to
override it:

| Variable | Default | Override when |
|---|---|---|
| `DB_ENCRYPT` | `true` | The server can't negotiate TLS. `check` detects it and says so. `false` matches a default SqlClient connection, which is unencrypted. The certificate is never verified either way. |
| `LOKI_CA_BUNDLE` | — | Loki uses an internal CA and you can't add it to the OS store. Path to a `.pem` on the agent. |
| `PIP_INDEX_URL` / `HTTPS_PROXY` | — | Only for a Python other than 3.12 / 3.9 on an agent without PyPI (0.3). |
| `DB_TRUSTED_CONNECTION` | `false` | Leave it. On Linux it means Kerberos and needs a ticket for the agent account. |
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

## Phase 2 — Database setup, from TFS

**Pipelines → New → Existing YAML → `<folder>/gw-login-setup.yaml`** (e.g.
`/Grafana/logins/gw-login-setup.yaml`) → **Save**, then **⋯ → Rename** it to
`GW-Login-Setup` — otherwise ADO names it after the repo, e.g. `<repo> (1)`.

Run it three times, changing only the `action`. The two login boxes show
`none`, which means *not set* — leave them unless a step below says otherwise.

### `check` *(writes nothing)*

Reports **every** problem it finds in one run, then exits non-zero if anything
would stop the collector:

- the agent's OS, Python, and the `pymssql` it installed
- TCP to SQL Server and to Loki; Loki's labels and TLS trust
- that SQL Server answers its handshake — `pymssql` would otherwise hang
  forever on a firewall that accepts connections and then goes silent
- SQL Server is Microsoft SQL Server, 2016 SP1 or later
- the account can create tables and views in `dbo`, create users, and grant on `dbo`
- once the tables exist: that `DBUSER` can read and write them

If the connection fails with encryption on, `check` retries once unencrypted —
for the diagnosis only — so credentials, version and rights are still checked,
and it tells you whether `DB_ENCRYPT=false` would actually fix it. A rejected
login is reported as that, with no pointless retry.

Read the **Done** section at the bottom — it lists everything to fix, labelled
`[db]` or `[collector]`. Fix, re-run, repeat until it says *No problems found*.

### `schema`

Creates three tables and three views, then confirms all six exist by name.
Idempotent — safe to re-run.

The pipeline's account creates them, so it needs the rights to. If `check`
warned *This account lacks …, which 'schema' needs*, it printed the exact
`GRANT` lines. A DBA runs them in the database once — for an account
`svc_user` lacking all three:

```sql
GRANT CREATE TABLE TO [svc_user];
GRANT CREATE VIEW TO [svc_user];
GRANT ALTER ON SCHEMA::dbo TO [svc_user];
```

All three are needed: creating anything in `dbo` takes `ALTER` on the schema
as well as `CREATE TABLE` / `CREATE VIEW`.

Creating a table in `dbo` doesn't make the account its owner, so `schema` then
checks it can read and write the three tables. If it can't — it can if it's in
`db_datareader` + `db_datawriter` — `schema` prints those `GRANT` lines too, on
just the three tables:

```sql
GRANT SELECT, INSERT, UPDATE ON dbo.gw_login_daily TO [svc_user];
GRANT SELECT, INSERT, UPDATE ON dbo.gw_login_collector_run TO [svc_user];
GRANT SELECT, INSERT, UPDATE, DELETE ON dbo.gw_login_user_daily TO [svc_user];
```

The collector itself only needs this second set; the first is for `schema`.
Once `schema` has succeeded and the second set is in place, a DBA can revoke
the first — `schema` prints the exact `REVOKE` lines for rights granted
directly. `ALTER` on `dbo` reaches every object in `dbo`, so a shared account
shouldn't keep it. Re-grant only when `schema.sql` changes.

```sql
REVOKE CREATE TABLE FROM [svc_user];
REVOKE CREATE VIEW FROM [svc_user];
REVOKE ALTER ON SCHEMA::dbo FROM [svc_user];
```

After that, `check` shows the three views as not visible to the account —
expected: the collector doesn't use them, and Grafana reads them with its own
login.

### `grants` — set `grafanaLogin`

Grants a **read-only** login to Grafana. Create that SQL login first (DBA); the
pipeline adds the database user and permissions but can't create a server login.

Skip this if `check` said the account can't create database users — the
usual case, and the right one for a shared account. A DBA then runs section 2
of `grants.sql` (the Grafana part) with the Grafana login filled in.

Leave `collectorLogin` at `none`. It defaults to `DBUSER`, which is also the account
running the pipeline, so `grants.sql` skips it — SQL Server refuses a grant to
yourself. Instead, the pipeline **checks** that `DBUSER` can already read and
write the three tables, and stops with the exact missing rights if not. (Creating
a table in `dbo` doesn't make you its owner, so `db_ddladmin` alone isn't enough.)

> Prefer sqlcmd? `sqlcmd -S <server> -d <database> -i logins/sql/schema.sql`,
> then edit the two `:setvar` lines at the top of `grants.sql` and run it the
> same way. `schema.sql` sets `QUOTED_IDENTIFIER ON` itself — sqlcmd defaults it
> off, which would otherwise make the computed-column indexes fail.

---

## Phase 3 — Register and schedule the collector

**Pipelines → New → Existing YAML → `<folder>/gw-login-collector.yaml`** →
**Save**, don't run → **⋯ → Rename** to `GW-Login-Collector`.

Then **Edit → ⋯ → Triggers → Scheduled**:

1. Under **Scheduled**, click **+ Add**; set **03:00** and tick **all seven days**.
2. **Untick *Only schedule builds if the source or pipeline has changed.***

That second box matters more than anything else on this page. Left ticked, the
collector runs once and every later scheduled run is skipped — its source never
changes day to day — with no error shown. Gaps pass Loki's retention and become
permanent before anyone notices.

The YAML deliberately has no `schedules:` block, so the UI is the single source
of truth. When both exist, ADO runs **only** the UI schedules and ignores the
YAML ones — so a `schedules:` block added later "as a backup" would never run.

---

## Phase 4 — The test ladder

Four runs, each proving one new thing. A failure at step 3 is only easy to
diagnose if 1 and 2 passed.

### Test 1 — Loki, labels and regex *(writes nothing)*

**Run → tick `Dry run` → Run.**

- Non-zero logins **and** non-zero distinct users → selector and regex both good.
- Logins but zero distinct users → `LOGIN_USER_REGEX` doesn't match. The log
  prints sample unmatched lines; fix it before storing anything, or you'll
  backfill 30 days of `(unparsed)`.
- All zeros → wrong `ENVS` casing, `LOKI_PROJECT`, or `job` label.

The header also prints `Day boundaries : … (<rule>)`. It must match what the
report uses — see *Reconcile* in phase 5.

### Test 2 — Database write path *(one day)*

Dry run never opens a database connection, so this is the first real test of
credentials, `pymssql` and the upsert.

**Run → `Backfill start` = yesterday, `Backfill end` = yesterday, `Dry run` off.**

Then **re-run the identical job**. Row counts must not grow — that proves the
upsert is idempotent, which is what makes the daily lookback safe.

### Test 3 — Seed history *(time-critical)*

Loki holds ~30 days right now; whatever you don't capture is gone.

**Run → `Backfill start` = 30 days ago, `Backfill end` left at `none` → Run.**

Expect this to take noticeably longer than the report: it pulls raw log lines
rather than counts, because that's what usernames require.

### Test 4 — Verify

**`GW-Login-Setup` → `action` = `verify`.**

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
   **read-only** login — never the collector's. Grafana's own host must reach
   SQL Server on 1433; that's a different machine from the agent, so phase 2's
   `check` doesn't cover it.
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

Remaining differences are worth chasing, not shrugging at. The usual causes:

- **The two jobs resolved `REPORT_TIMEZONE` differently.** The built-in Eastern
  names (`Eastern Standard Time`, `America/New_York`, `ET`, `EST`, `EDT`,
  `Eastern`) behave identically everywhere. Any other name goes through Python's
  `zoneinfo`, which on a **Windows** agent without the `tzdata` package falls back
  to UTC. If the report runs on Windows, use one of the built-in names.
- The report running with `INCLUDE_USER_DETAIL=false`, which counts via a
  different query.

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

**`GW-Login-Setup` → `action` = `verify`.**

`days_behind` = 1, a new `ok` run from the schedule, gaps and reconciliation
still empty. That's the deployment confirmed end to end.

Then check again the morning after. One scheduled run proves the schedule
exists; two prove the *only if source changed* box is unticked.

---

## Troubleshooting

| You see | Cause | Fix |
|---|---|---|
| `logins/tools/prepare_python.sh: No such file or directory` | an older YAML that assumed `logins/` at the repo root | pull the current YAMLs — they find the folder themselves |
| Run dialog: a text box marked *Required*, Run greyed out, *unavailable while parameters are invalid* | an older YAML whose optional text parameters default to `''` — this ADO Server treats that as required | pull the current YAMLs — they default to `none` |
| `No tools/prepare_python.sh anywhere in this repo` | the folder was copied in but new files never `git add`-ed | `git add <folder>`, commit, push |
| `Missing from the checked-out repo: …` | same, for the files named | same |
| `Found N copies of tools/prepare_python.sh` | the folder exists twice | delete the stale copy, or set a pipeline variable `LOGINS_DIR` to the one to use |
| `Unable to locate executable file: 'pwsh'` | a PowerShell step ran on a Linux agent | the `logins/` YAMLs are bash-only now — pull them. The monthly report's YAML still has a PowerShell step and fails the same way on a Linux agent |
| `No agent found in pool DevopsAutomation which satisfies the specified demands` | no online Linux agent in that pool | bring one online, or check the agent's `Agent.OS` capability |
| `No Python >= 3.9 found` | Python missing or too old, or outside the agent's `.path` | 0.2, then 0.5 |
| `None of the Python interpreters above could create a virtualenv` | Debian/Ubuntu without `python3-venv` | 0.2 |
| `Could not install pymssql==…: no wheel in … fits Python …` | the agent's Python isn't 3.12 or 3.9, and no index is reachable | 0.3 |
| `pymssql is not installed` | the install step failed — see its warnings | 0.3 |
| `accepted the connection but never answered SQL Server's handshake` | a firewall or proxy swallows the traffic, or it isn't SQL Server | check the path to `DBINSTANCE`; `pymssql` would have hung here |
| `Confirmed: it connects unencrypted` | the server can't negotiate TLS | `DB_ENCRYPT=false` in `gw-reports-secrets`, or enable TLS on the server |
| `nothing is listening there` / `Connection refused` | wrong host or port in `DBINSTANCE` | `host` or `host,port` — never `host:port` |
| `This account lacks …, which 'schema' needs` | `DBUSER` has no DDL rights in `dbo` | a DBA runs the `GRANT` lines printed under it (phase 2, `schema`) |
| `Cannot open database` | `DBNAME` wrong, or the login has no user in it | fix `DBNAME`, or a DBA maps the login |
| `The collector runs as '…', which lacks: INSERT on …` | `DBUSER` created the tables but can't write them | a DBA runs the `GRANT` lines printed under it (phase 2, `schema`) |
| `Loki's certificate is not trusted on this agent` | Loki uses an internal CA | 0.4, or `LOKI_CA_BUNDLE` |
| `Login failed for user` | wrong `DBUSER` / `DBPASS` | fix the DB variable group |
| `older than 2016 SP1` | SQL Server too old for `CREATE OR ALTER` | use a newer instance |
| Collector ran once, then never again | *Only schedule builds if the source … changed* is ticked | phase 3, step 2 |
| Every count is 0 | `LOKI_PROJECT` / `ENVS` / `job` wrong | phase 4, test 1 |

---

## Rollback

Nothing above modifies an existing object; the only change outside the new
objects is the rights granted to `DBUSER` in phase 2.

1. Disable `GW-Login-Collector`.
2. Delete the Grafana dashboard and alert rule.
3. Only if abandoning the approach — **this destroys collected history
   permanently**:

```bash
sqlcmd -S <server> -d <database> -Q "DROP VIEW IF EXISTS dbo.gw_login_monthly; DROP VIEW IF EXISTS dbo.gw_login_monthly_users; DROP VIEW IF EXISTS dbo.gw_login_freshness; DROP TABLE IF EXISTS dbo.gw_login_user_daily; DROP TABLE IF EXISTS dbo.gw_login_daily; DROP TABLE IF EXISTS dbo.gw_login_collector_run;"
```

4. If `DBUSER` still holds the `CREATE TABLE` / `CREATE VIEW` / `ALTER ON
   SCHEMA::dbo` rights from phase 2, revoke them (the `REVOKE` lines there).
   Its rights on the three tables go with the tables.

The monthly report is untouched throughout — it reads Loki directly and has no
dependency on any of this.
