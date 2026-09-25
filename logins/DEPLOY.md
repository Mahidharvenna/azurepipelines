# Deployment runbook — login collector

From an empty database to a Grafana dashboard with seeded history and a daily
pipeline keeping it current.

Both pipelines run on **Linux agents only** (`demands: Agent.OS -equals Linux`).
Everything is additive — rollback is at the bottom and takes two minutes.

The order matters: the setup pipeline needs the variable groups, and the
collector needs the tables.

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

## Phase 0 — The Linux agent *(one-time, needs root)*

### 0.1 What the agent needs

| Needs | Why | Without it |
|---|---|---|
| Python **3.9+** | pyodbc wheels, `zoneinfo` | `Prepare Python` fails: *No Python >= 3.9 found* |
| Python's `venv` module | job-local installs; system pip is blocked by PEP 668 on newer Debian/Ubuntu and too old on RHEL 8 | `Prepare Python` fails: *Could not create a virtualenv* |
| **Microsoft ODBC Driver 18** (`msodbcsql18`) | the SQL Server driver; installing it pulls in unixODBC (`libodbc.so.2`) | `check` fails: *cannot load the unixODBC library* |
| Route to PyPI, **or** an internal mirror | installs pyodbc into the job venv | `Prepare Python` warns; `check` fails. See 0.4 |
| Internal CA in the OS trust store | only if Loki or SQL Server use an internal CA | `check` fails with a certificate error |

The ODBC driver cannot be installed by the pipeline: it needs root and accepting
Microsoft's EULA. That's the one piece of admin work this design can't avoid.

### 0.2 Install — RHEL / Rocky / Alma 8 or 9

```bash
sudo dnf install -y python3.11
curl -fsSL "https://packages.microsoft.com/config/rhel/$(rpm -E %rhel)/prod.repo" \
  | sudo tee /etc/yum.repos.d/mssql-release.repo
sudo ACCEPT_EULA=Y dnf install -y msodbcsql18
```

`venv` ships with Python on RHEL. RHEL 8's default `python3` is 3.6 — too old —
which is why this installs `python3.11` alongside it; `Prepare Python` picks the
newest version on `PATH`.

If `unixODBC-utf16` is installed it conflicts with `msodbcsql18`; remove it first.

### 0.3 Install — Ubuntu / Debian

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv curl
curl -fsSL -o /tmp/packages-microsoft-prod.deb \
  "https://packages.microsoft.com/config/$(. /etc/os-release && echo "$ID/$VERSION_ID")/packages-microsoft-prod.deb"
sudo dpkg -i /tmp/packages-microsoft-prod.deb && sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18
```

`ACCEPT_EULA` goes **after** `sudo` — `sudo` resets the environment, so
`ACCEPT_EULA=Y sudo …` silently doesn't pass it through.

**Ubuntu 20.04:** its `python3` is 3.8 — too old. Install
`python3.9 python3.9-venv` as well; `Prepare Python` picks it up. Debian 10 and
older have no 3.9 package and are past end of life; upgrade the agent's OS.

**Check**, on either distro — it should list `ODBC Driver 18 for SQL Server`:

```bash
odbcinst -q -d
```

### 0.4 No route to PyPI?

The job installs pyodbc into a fresh venv each run. If the agent can't reach
PyPI, pick one:

- add **`PIP_INDEX_URL`** (an internal PyPI mirror — Nexus, Artifactory, an Azure
  Artifacts feed) to `gw-reports-secrets`. If the URL carries a token, make it a
  **secret** variable: both YAMLs map it into the *Prepare Python* step
  explicitly, which is the only way ADO passes a secret to a script. pip masks
  the password in its own output.
- add **`HTTPS_PROXY`** the same way. Loki calls are unaffected — they bypass the
  proxy explicitly when `BYPASS_PROXY=true`.
- or have the admin install the OS package. The venv inherits system packages,
  and `Prepare Python` prefers an interpreter that already has pyodbc, so pip is
  never called:
  - Ubuntu / Debian: `apt-get install python3-pyodbc`
  - RHEL 9: `dnf install python3-pyodbc`
  - **RHEL 8: not an option** — its `python3-pyodbc` is built for the platform
    Python 3.6, which is too old. Use a mirror or a proxy.

### 0.5 Internal CA *(only if Loki or SQL Server use one)*

Python on **Windows** trusts the Windows certificate store; on **Linux** it trusts
only the OpenSSL bundle. An internal CA that "just works" for a Windows-hosted
job fails here.

```bash
# RHEL family
sudo cp corp-root.pem /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust
# Ubuntu / Debian -- file must be PEM and end in .crt
sudo cp corp-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates
```

That fixes both Loki and SQL Server. Narrower alternatives if you can't touch
the OS store: `LOKI_CA_BUNDLE=/path/to/ca.pem` for Loki, and
`DB_TRUST_SERVER_CERT=true` for SQL Server.

### 0.6 Python outside `/usr/bin`? Refresh the agent's PATH

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

### 0.7 More than one Linux agent in `Default`?

The demand only guarantees *a* Linux agent. If there are several, either give
every one of them the prerequisites, or pin both pipelines to the one that has
them by adding a second demand in each YAML:

```yaml
  demands:
  - Agent.OS -equals Linux
  - Agent.Name -equals <agent-name>
```

Otherwise `check` can pass on one agent and the daily collector land on another.

### 0.8 Is the database backed up?

**Confirm before seeding.** Once a day ages out of Loki's ~30-day window these
tables are the only copy and nothing can regenerate them. A machine rebuild, a
refresh-from-prod, or a snapshot restore silently destroys the whole history.

If it isn't backed up, pick a database that is, or add a periodic export of
`gw_login_daily` and `gw_login_user_daily` published as a build artifact.

---

## Phase 1 — Variable groups

Both pipelines read two groups. Neither needs creating from scratch.

**`gw-logins-db`** — the database credential. Point it at your existing DB group
by editing the `- group:` line in both YAMLs; it must supply `DBINSTANCE`,
`DBNAME`, `DBUSER` and `DBPASS` (secret). `DBINSTANCE` takes SQL Server's own
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
| `DB_TRUST_SERVER_CERT` | `false` | **Likely needed** if the SQL certificate comes from an internal CA you haven't added to the agent (0.5). Driver 18 encrypts by default. `check` says so. |
| `LOKI_CA_BUNDLE` | — | Loki uses an internal CA and you can't add it to the OS store. Path to a `.pem` on the agent. |
| `PIP_INDEX_URL` / `HTTPS_PROXY` | — | The agent can't reach PyPI (0.4). |
| `DB_ODBC_DRIVER` | newest installed | You need a specific driver. `check` lists what's there. |
| `DB_ENCRYPT` | `true` | Rarely. Prefer `DB_TRUST_SERVER_CERT`. |
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

**Pipelines → New → Existing YAML → `/logins/gw-login-setup.yaml`** → name it
`GW-Login-Setup` → **Save**.

Run it three times, changing only the `action`:

### `check` *(writes nothing)*

Reports **every** problem it finds in one run, then exits non-zero if anything
would stop the collector:

- the agent's OS, Python, and installed ODBC drivers
- TCP to SQL Server and to Loki; Loki's labels; TLS trust for both
- SQL Server is Microsoft SQL Server, 2016 SP1 or later
- the account can create tables and views in `dbo`, create users, and grant on `dbo`
- once the tables exist: that `DBUSER` can read and write them

A certificate problem doesn't hide the rest: `check` retries once with
`TrustServerCertificate=yes` — for the diagnosis only — so credentials, version
and rights are still checked, and it tells you whether `DB_TRUST_SERVER_CERT`
would actually fix it.

Read the **Done** section at the bottom — it lists everything to fix, labelled
`[db]` or `[collector]`. Fix, re-run, repeat until it says *No problems found*.

### `schema`

Creates three tables and three views, then confirms all six exist by name.
Idempotent — safe to re-run.

### `grants` — set `grafanaLogin`

Grants a **read-only** login to Grafana. Create that SQL login first (DBA); the
pipeline adds the database user and permissions but can't create a server login.

Leave `collectorLogin` blank. It defaults to `DBUSER`, which is also the account
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

**Pipelines → New → Existing YAML → `/logins/gw-login-collector.yaml`** → name it
`GW-Login-Collector` → **Save**, don't run.

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
credentials, ODBC and the upsert.

**Run → `Backfill start` = yesterday, `Backfill end` = yesterday, `Dry run` off.**

Then **re-run the identical job**. Row counts must not grow — that proves the
upsert is idempotent, which is what makes the daily lookback safe.

### Test 3 — Seed history *(time-critical)*

Loki holds ~30 days right now; whatever you don't capture is gone.

**Run → `Backfill start` = 30 days ago → Run.**

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
| `Unable to locate executable file: 'pwsh'` | a PowerShell step ran on a Linux agent | the `logins/` YAMLs are bash-only now — pull them. The monthly report's YAML still has a PowerShell step and fails the same way on a Linux agent |
| `No agent found in pool Default which satisfies the specified demands` | no online Linux agent | bring one online, or check the agent's `Agent.OS` capability |
| `No Python >= 3.9 found` | Python missing or too old, or outside the agent's `.path` | 0.2 / 0.3, then 0.6 |
| `None of the Python interpreters above could create a virtualenv` | Debian/Ubuntu without `python3-venv` | 0.3 |
| `pip could not install pyodbc` | no route to PyPI | 0.4 |
| `cannot load the unixODBC library` | `msodbcsql18` not installed | 0.2 / 0.3 |
| `No Microsoft 'ODBC Driver NN for SQL Server'` | same | same |
| `SSL Provider: [error:…:certificate verify failed…]` (Linux) or `certificate chain was issued by an authority that is not trusted` (Windows) | SQL Server uses an internal CA | 0.5, or `DB_TRUST_SERVER_CERT=true` — `check` confirms which works |
| `TrustServerCertificate=yes fails the same way` | TLS protocol mismatch, not trust — e.g. an old SQL Server without TLS 1.2 | patch SQL Server; the setting won't help |
| `The collector runs as '…', which lacks: INSERT on …` | `DBUSER` created the tables but can't write them | a DBA grants it (e.g. `db_datawriter`) |
| `Loki's certificate is not trusted on this agent` | Loki uses an internal CA | 0.5, or `LOKI_CA_BUNDLE` |
| `Login failed for user` | wrong `DBUSER` / `DBPASS` | fix the DB variable group |
| `older than 2016 SP1` | SQL Server too old for `CREATE OR ALTER` | use a newer instance |
| Collector ran once, then never again | *Only schedule builds if the source … changed* is ticked | phase 3, step 2 |
| Every count is 0 | `LOKI_PROJECT` / `ENVS` / `job` wrong | phase 4, test 1 |

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
