# Login history — Loki → SQL Server → Grafana

Daily job that copies login counts (and the usernames behind them) out of Loki
into SQL Server — one or several Loki projects per run — plus a Grafana
dashboard that reads SQL Server instead of Loki.

## Why

Loki retains ~30 days. Anything reading it directly inherits that ceiling —
"logins over the last year" is simply not answerable.

The fix is to decouple collection from presentation:

```
                  runs daily, well inside retention
                              │
   Loki  ───────────────────► collect_logins.py ──────► SQL Server
  (~30d rolling)                                     (permanent)
                                                            │
                                                            ▼
                                                        Grafana
                                                  (unbounded history)
```

Loki only has to remember the last few days. Everything older lives in tables
nobody purges.

## What's here

| File | Purpose |
|---|---|
| [`DEPLOY.md`](DEPLOY.md) | **Step-by-step deployment with the test ladder.** |
| `collect_logins.py` | Queries Loki, upserts daily counts and per-user rows, project by project. |
| `gw-login-collector.yaml` | The daily pipeline — every project, or one picked at run time. Schedule it **daily** in the UI. |
| `gw-login-setup.yaml` | **Temporary** bootstrap pipeline — runs the setup from TFS. |
| `tools/setup.py` | What that pipeline runs: pre-flight, schema, grants, verify. |
| `tools/prepare_python.sh` | Both pipelines' Python step on the Linux agent: picks Python, builds a job venv, installs `pymssql`. |
| `tools/gwcommon.py` | Config, SQL Server connection and TLS helpers shared by the collector and `setup.py`. |
| `tools/wheels/` | `pymssql` wheels for Linux Python 3.12 and 3.9, installed without network. |
| `sql/schema.sql` | Tables and views. Idempotent; adds the `project` column to tables made before it. |
| `sql/grants.sql` | Least-privilege grants (collector r/w, Grafana read-only). |
| `sql/verify.sql` | Per project: coverage, freshness, gaps, per-user reconciliation. |
| `grafana/gw-user-logins-dashboard.json` | Importable dashboard, 10 panels, filtered by project. |

The folder can sit anywhere in your repo (`logins/`, `Grafana/logins/`, ...); each
pipeline's first step locates it. Paths in these docs are relative to the folder.

## Tables

`gw_login_daily` — one row per `(project, day, env, product)` with `logins`
and `distinct_users`. The grain the dashboard reads.

`gw_login_user_daily` — one row per `(project, day, env, product, username)`.

That second table exists because **daily distinct-user counts cannot be summed**
into a monthly or quarterly figure — anyone active on several days would be
counted more than once. Storing the usernames is the only way to answer "how
many unique people used UAT1 last quarter". It also means the table holds user
identifiers: see the note in `grants.sql`.

Lines that `LOGIN_USER_REGEX` cannot parse are stored under `(unparsed)` so the
per-user rows still sum to the event count. `verify.sql` section 5 lists them —
if the number is large, tune the regex.

`gw_login_collector_run` — one row per project per run, for the freshness
panel and alert.

`project` is the Loki `project` label the row was collected under, stored
exactly as Loki spells it. Environment names repeat across projects — two can
each have a `DEV1` — so it leads every key; without it their rows would
overwrite each other.

## Configuration

The Loki, scope and timezone settings (`LOKI_URL`, `LOKI_PROJECT`, `ENVS`,
`PRODUCTS`, `LOGIN_USER_REGEX`, `REPORT_TIMEZONE`, ...) come from the
`gw-reports-secrets` group (see `DEPLOY.md`); these have working defaults and
need setting only to override:

| Variable | Default | Purpose |
|---|---|---|
| `LOKI_PROJECTS` | `LOKI_PROJECT` | Comma list of the Loki `project` label values a run covers. |
| `PROJECT` | `all` | The pipeline's *Project* parameter: one project instead of all of `LOKI_PROJECTS`. |
| `LOOKBACK_DAYS` | `7` | Complete days re-collected each run. |
| `LOKI_RETENTION_DAYS` | `30` | Days older than this are skipped, not queried. |
| `STORE_USERNAMES` | `true` | Write `gw_login_user_daily`. |
| `ALLOW_ZERO_OVERWRITE` | `false` | Let a 0 overwrite a stored non-zero count. |
| `BACKFILL_START` / `_END` | — | Load a specific range instead of the lookback. |
| `DRY_RUN` | `false` | Query Loki, write nothing. |
| `DB_*` | — | Connection settings; see `DEPLOY.md`. |

## Several projects, one pipeline

A run covers every project in `LOKI_PROJECTS`, one after another, each under
its own run row and with its own environments — `ENVS=ALL` asks Loki per
project. One project's Loki failure doesn't stop the next; the run still exits
non-zero so somebody looks. The *Project* box narrows a manual run to one
project, e.g. to backfill a newly added one: type its Loki `project` value. A
value Loki doesn't have stops the run and lists the ones it has. It's a text
box, not a list, so no project name is written into the YAML. Scheduled runs
can't pass parameters, so they always run `all`.

The dashboard's *Project* dropdown filters every panel.

Tables made before the `project` column are upgraded in place by setup's
`schema`, their rows tagged with `LOKI_PROJECT`. [`DEPLOY.md`](DEPLOY.md),
*Adding projects*, has the order to do it in.

## Two safeguards worth knowing about

Both exist because these tables are the *only* copy. Loki cannot rebuild them.

**Out-of-retention days are never queried.** A day whose logs have aged out
returns `0`, not an error. Writing that `0` would overwrite real history with a
lie, so the collector refuses to ask and says so in the log.

**A zero never overwrites a non-zero.** On upsert, an existing non-zero count
survives a new value of `0`. A zero nearly always means something upstream broke
— Loki down, a renamed `env` or `job` label — and only rarely means "genuinely
no logins". The first write for a day still records a legitimate `0`. Override
with `ALLOW_ZERO_OVERWRITE=true` when correcting bad history on purpose.

## Daily mode re-collects a window

Each run re-collects the last `LOOKBACK_DAYS` complete days, not just yesterday.
The upsert is idempotent, so this is free, and it means a missed run or a
late-arriving log self-heals without anyone intervening.

## Paging, not truncation

Loki caps how many entries one query returns. The collector doesn't warn and
move on when a day hits the cap: it pages, resuming from the last entry until
the day is exhausted. These tables are the only permanent copy, so a silently
short day would be wrong forever.

## Setting it up without local tooling

`gw-login-setup.yaml` does the database setup from the pipeline, so nobody needs
`sqlcmd` or a SQL client on their machine. It's Python + `pymssql`, same as the
collector — so a successful `check` also proves the collector can reach and
write to SQL Server from that agent.

It takes an `action`:

| Action | Does | Writes? |
|---|---|---|
| `check` | Pre-flight: OS, Python, `pymssql`, TCP to Loki and SQL, the SQL Server handshake, Loki labels and TLS, `LOKI_PROJECTS` against Loki's projects, SQL Server 2016 SP1+, rights, the `project` column. Reports **every** problem in one run. | no |
| `schema` | Applies `sql/schema.sql` — adding the `project` column to tables made before it — then confirms all six objects (3 tables, 3 views) exist by name | yes |
| `grants` | Applies `sql/grants.sql` — requires `grafanaLogin` | yes |
| `verify` | Runs `sql/verify.sql`, printing every result set | no |
| `all` | The four above, in order | yes |

`check` is the default so an accidental run changes nothing.

Because it bypasses `sqlcmd`, the script has to do three things `sqlcmd` does
client-side and the server knows nothing about: split each file on `GO`, expand
`:setvar` / `$(TOKEN)`, and recover `PRINT` output — the client doesn't expose it, so
the `PRINT` lines are lifted out and used to label the result set that follows.
That's why the `.sql` files work unchanged through either this pipeline or
`sqlcmd`.

Once the dashboard is live, disable this pipeline rather than deleting it:
schema upgrades — adding projects, for one — run through it. The daily
collector is the thing that runs every day.

## Agent prerequisites

Both pipelines run in the `DevopsAutomation` pool on **Linux agents** (`demands: Agent.OS -equals Linux`). The
agent needs the following — usually already there; details in
[`DEPLOY.md`](DEPLOY.md), phase 0:

- **Python 3.9+** with the `venv` module. `UsePythonVersion@0` only searches the
  agent's tool cache and fails on self-hosted agents, so `prepare_python.sh`
  tries each `python3.x` on `PATH`, newest first, until one can build a venv.
- **Nothing from PyPI** for Python 3.12 or 3.9: `pymssql` installs from the
  wheels in `tools/wheels/` into the job-local venv (which also sidesteps PEP
  668 on newer Debian/Ubuntu). Any other Python falls back to PyPI or a mirror
  (`PIP_INDEX_URL`).
- **No SQL Server driver.** `pymssql`'s Linux wheel carries its own SQL Server
  client (FreeTDS compiled in, plus OpenSSL and Kerberos), the way .NET's
  `System.Data.SqlClient` does for PowerShell tasks — nothing needs root.
- **Network** to Loki (`:3100`) and SQL Server (`:1433`). `BYPASS_PROXY=true`
  (the default) skips the system proxy for Loki.
- **Loki on an internal CA?** Linux Python trusts only the OpenSSL bundle, not
  a Windows store: add the CA to the OS store, or set `LOKI_CA_BUNDLE`. SQL
  Server's certificate is not verified, so it needs nothing.

Run `GW-Login-Setup` with `action=check` to see which of these are missing — it
reports all of them in one run.
