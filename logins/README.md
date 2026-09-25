# Login history — Loki → SQL Server → Grafana

Daily job that copies login counts (and the usernames behind them) out of Loki
into SQL Server, plus a Grafana dashboard that reads SQL Server instead of Loki.

Companion to [`../reports/`](../reports/), not a replacement: the report emails a
monthly spreadsheet, this gives a dashboard with unbounded history. Both share
the same Loki selector, the same `LOGIN_USER_REGEX` and the same
`REPORT_TIMEZONE`, so their numbers reconcile.

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
| `collect_logins.py` | Queries Loki, upserts daily counts and per-user rows. |
| `gw-login-collector.yaml` | The daily pipeline. Schedule it **daily** in the UI. |
| `gw-login-setup.yaml` | **Temporary** bootstrap pipeline — runs the setup from TFS. |
| `tools/setup.ps1` | What that pipeline runs: pre-flight, schema, grants, verify. |
| `sql/schema.sql` | Tables and views. Idempotent. |
| `sql/grants.sql` | Least-privilege grants (collector r/w, Grafana read-only). |
| `sql/verify.sql` | Coverage, freshness, gaps, per-user reconciliation. |
| `grafana/gw-logins-dashboard.json` | Importable dashboard, 10 panels. |

## Tables

`gw_login_daily` — one row per `(day, env, product)` with `logins` and
`distinct_users`. The grain the dashboard reads.

`gw_login_user_daily` — one row per `(day, env, product, username)`.

That second table exists because **daily distinct-user counts cannot be summed**
into a monthly or quarterly figure — anyone active on several days would be
counted more than once. Storing the usernames is the only way to answer "how
many unique people used UAT1 last quarter". It also means the table holds user
identifiers: see the note in `grants.sql`.

Lines that `LOGIN_USER_REGEX` cannot parse are stored under `(unparsed)` so the
per-user rows still sum to the event count. `verify.sql` section 5 lists them —
if the number is large, tune the regex.

`gw_login_collector_run` — one row per run, for the freshness panel and alert.

## Configuration

Every variable the report understands works here too. Set them in the same two
places (see `DEPLOY.md`); the ones unique to the collector are:

| Variable | Default | Purpose |
|---|---|---|
| `LOOKBACK_DAYS` | `7` | Complete days re-collected each run. |
| `LOKI_RETENTION_DAYS` | `30` | Days older than this are skipped, not queried. |
| `STORE_USERNAMES` | `true` | Write `gw_login_user_daily`. |
| `ALLOW_ZERO_OVERWRITE` | `false` | Let a 0 overwrite a stored non-zero count. |
| `BACKFILL_START` / `_END` | — | Load a specific range instead of the lookback. |
| `DRY_RUN` | `false` | Query Loki, write nothing. |
| `DB_*` | — | Connection settings; see `DEPLOY.md`. |

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

`monthly_report.py` warns when a chunk hits Loki's entry cap and moves on —
reasonable, because it runs again next month. The collector pages instead: it
resumes from the last entry until the day is exhausted. A silently short day
here would be wrong permanently.

## Setting it up without local tooling

`gw-login-setup.yaml` does the database setup from the pipeline, so nobody needs
`sqlcmd` or a SQL client on their machine. It talks to SQL Server through
`System.Data.SqlClient`, which is built into Windows PowerShell — the agent
needs no database tooling at all for this part.

It takes an `action`:

| Action | Does | Writes? |
|---|---|---|
| `check` | Pre-flight: TCP to Loki and SQL, Loki labels, `@@VERSION`, `CREATE TABLE` rights, Python, ODBC, pip | no |
| `schema` | Applies `sql/schema.sql`, then asserts 7 objects exist | yes |
| `grants` | Applies `sql/grants.sql` — requires `grafanaLogin` | yes |
| `verify` | Runs `sql/verify.sql`, printing every result set | no |
| `all` | The four above, in order | yes |

`check` is the default so an accidental run changes nothing.

Because it bypasses `sqlcmd`, the script has to do two things `sqlcmd` does for
free: split each file on `GO` (a client-side batch separator the server rejects)
and expand `:setvar` / `$(TOKEN)` (also client-side). That's why `grants.sql`
keeps working unchanged whether you run it through this pipeline or `sqlcmd`.

Delete this pipeline once the dashboard is live. The daily collector is the
thing that stays.

## Agent prerequisites

- **Python 3** — located by the same PowerShell probe the report uses, because
  `UsePythonVersion@0` only searches the tool cache and fails on self-hosted
  agents.
- **pyodbc** plus Microsoft's ODBC Driver 17/18. This is the one pip dependency;
  SQL Server has no standard-library driver. If the agent has no route to PyPI,
  vendor a wheel and install from disk.
- **Network** — the agent must reach Loki (`:3100`) and SQL Server (`:1433`).
  `BYPASS_PROXY=true` (the default) skips the system proxy for Loki, matching
  the report.
