# Monthly Guidewire Login Report

A scheduled Azure DevOps pipeline that emails a monthly Excel report of
Guidewire "User Login" counts per environment, sourced from Loki.

Designed to mirror a Grafana "user logins" panel — same Loki label selectors,
same `|= "User Login"` line match — so the report reconciles with the dashboard.

## Files

| File | Purpose |
|---|---|
| `gw-monthly-login-report.yaml` | The scheduled pipeline (cron: 1st of month, 06:00 UTC) |
| `monthly_report.ps1` | Queries Loki, builds the xlsx, emails it |

### Why PowerShell

The pipeline runs the **PowerShell** version because it needs nothing installed
on the agent: no Python runtime, no `pip` packages, no PowerShell Gallery access
and no Excel. The `.xlsx` is written directly as OOXML (a zip of XML parts) via
`System.IO.Compression`, which ships with .NET.

That matters on a locked-down build agent. `UsePythonVersion@0` in particular is
a trap on self-hosted agents -- it only searches the agent tool cache
(`_work/_tool`), not a normal machine install, so it fails with *"did not match
any version in Agent.ToolsDirectory"* even when Python is present.

There is deliberately only one implementation, so there is never a question of
which script the pipeline actually runs.

## One-time setup

### 1. Create the Variable Group

Pipelines → Library → **+ Variable group** → name it **`gw-reports-secrets`**:

| Variable | Example | Secret? |
|---|---|---|
| `LOKI_URL` | `https://your-loki-host.example.com:3100` | no |
| `LOKI_VERIFY_TLS` | `true` (set `false` if agent rejects the cert) | no |
| `BYPASS_PROXY` | *(optional, default `true`)* skip the system proxy — see below | no |
| `LOKI_PROJECT` | `myproject` (your Loki `project` label value) | no |
| `SMTP_HOST` | `smtp.example.com` | no |
| `SMTP_PORT` | `25` | no |
| `SMTP_TLS` | `false` | no |
| `SMTP_USER` | (blank if relay needs no auth) | no |
| `SMTP_PASS` | (blank, or set) | **yes** |
| `FROM_ADDR` | `gw-reports@example.com` | no |
| `TO_ADDRS` | `you@example.com,team@example.com` (comma-sep) | no |
| `ENVS` | `DEV1,QA1,UAT1,PROD1` (match your Loki `env` label casing) | no |
| `PRODUCTS` | `pc` (or `pc,bc,cc,cm`) | no |
| `REPORT_TITLE` | *(optional)* e.g. `MyOrg Guidewire Login Report` — heads the email, subject and workbook | no |
| `REPORT_FILE_PREFIX` | *(optional, default `gw-logins`)* attachment filename prefix | no |
| `LOKI_MAX_QUERY_DAYS` | *(optional, default `7`)* — see below | no |
| `INCLUDE_USER_DETAIL` | *(optional, default `true`)* — per-user sheets | no |
| `LOGIN_USER_REGEX` | *(optional)* — must capture a named group `user`; see below | no |
| `REPORT_TIMEZONE` | *(optional)* e.g. `Eastern Standard Time`; blank = UTC | no |
| `LOKI_LOG_LIMIT` | *(optional, default `5000`)* Loki's per-query entry cap | no |
| `MAX_DETAIL_ROWS` | *(optional, default `50000`)* | no |

Toggle **Allow access to all pipelines** (or grant to this pipeline only).

### 2. Register the pipeline

Pipelines → **New pipeline** → your repo → **Existing Azure Pipelines YAML file**
→ `/reports/gw-monthly-login-report.yaml` → branch `master` → Save (don't run yet).

### 3. Validate

Click **Run pipeline** once manually. The script computes "last full calendar
month", so a run today produces last month's report. Check:

- The run's **monthly-report** artifact contains `gw-logins-YYYY-MM.xlsx`
- The email arrives at `TO_ADDRS`

Use the **Test month override** parameter (`YYYY-MM`) to regenerate any past
month still within Loki retention.

## How it queries Loki

For each environment + product it runs (PC example):

```logql
sum(count_over_time(
  {project="myproject", job="pclogs", env="DEV1", filename=~".*pc.log"}
  |= `User Login` [1d]
))
```

over the report window, stepped daily. Daily values populate the **Daily** sheet;
their sum is the **Summary** total.

## Scheduling

There is deliberately **no `schedules:` block in the YAML** -- defining one makes
ADO ignore any UI scheduled trigger for that pipeline. Scheduling is managed in
the UI instead:

**Pipeline → Edit → ⋯ → Triggers → Scheduled → +Add**

Set it to run on the last day of the month at whatever time suits. Changing the
cadence is then a UI change, not a commit.

A scheduled run needs no parameters: a blank month means **the current month**,
so a run on 31 July reports July.

### Which month gets reported

| `Month to report` | Result |
|---|---|
| blank *(scheduled runs, and normally manual ones)* | the **current** month |
| `2026-05` | exactly that month -- back-fill or re-issue |

The window always ends at the first of the *next* month, so a mid-month run
reports the month so far rather than failing. Useful for a spot check, but a run
before month-end is by definition a partial figure.

## Branding

`REPORT_TITLE` sets the name shown to recipients — it heads the email body, the
subject line and the workbook's Summary sheet:

```
REPORT_TITLE       = MyOrg Guidewire Login Report
REPORT_FILE_PREFIX = myorg-gw-logins
```

giving the subject *"MyOrg Guidewire Login Report - July 2026"* and an attachment
named `myorg-gw-logins-2026-07.xlsx`. Both default to a generic name, so nothing
site-specific is baked into the script.

## Multiple environments

`ENVS` takes a comma-separated list and produces **one** email covering all of
them:

```
ENVS = DEV1,QA7,UAT1
```

### `ENVS = ALL`

Set it to the literal `ALL` and the run discovers every environment that has logs
in the period, from Loki's label-values API, sorted naturally (`QA2` before
`QA10`, not after):

```
Discovered envs  : 14 found, 12 after exclusions
                   DEV1, DEV2, DEV5, QA1, QA2, QA7, QA8, QA12, UAT, UAT1, UAT2, UAT3
```

Two things to know:

- **`ALL` means all, including production.** Use `ENVS_EXCLUDE=PROD1,PROD2` to
  drop the ones you do not want.
- **Environments with no logins are omitted** when discovering, since a
  discovered-but-idle environment is noise. The run says how many were dropped.
  When you list environments *explicitly*, zero rows are kept — asking for an
  environment makes a zero meaningful.

`ALL` also means the report picks up a new environment on its own, which is
either convenient or surprising depending on your view. An explicit list is more
predictable; `ALL` is less maintenance.

The email leads with a table of logins and distinct users per environment and
centre, plus the busiest users across the whole report. Every environment also
appears in all four workbook sheets.

**Distinct users are counted with a set, not summed.** Someone who logs into both
DEV1 and QA7 counts once in the total, so the TOTAL row is normally *lower* than
the sum of the column above it. The email says so, since it otherwise looks like
an arithmetic error.

## Usernames and timestamps

With `INCLUDE_USER_DETAIL` on (the default) the workbook gains two sheets:

| Sheet | Contents |
|---|---|
| **Users** | User, Environment, Centre, Logins, First Login, Last Login — busiest first |
| **Detail** | One row per login: Timestamp, Environment, Centre, User |

This needs the raw log lines, not just counts, so it runs a second Loki query per
env/centre alongside the aggregate one. The totals still come from the aggregate
query, so they stay correct even if the line fetch is capped.

### Setting `LOGIN_USER_REGEX`

The username has to be pulled out of the log line, and that format is
site-specific. The default handles `User Login: jdoe`, `User Login jdoe` and
`User Login=jdoe`:

```
(?i)User\s+Login\s*[:=\-]?\s*(?<user>[A-Za-z0-9._\\@-]+)
```

It **must** contain a named group `user`. If a line does not match, the run warns
and prints the first few unmatched lines:

```
DEV1/pc : 412 line(s) did not match LOGIN_USER_REGEX -- those users are blank.
  Sample lines that did not match (use these to set LOGIN_USER_REGEX):
    2026-07-15 09:23:41,123 INFO  Server.Security  jdoe successful User Login from 10.1.2.3
```

The run also breaks each sample into numbered fields, because many Guidewire
logs are columnar and the username is simply the *n*th column rather than
something that follows a keyword:

```
    node1  jbankay  3de-f511  2026-07-01 00:14:00,552  https-jsse-...  INFO  ...
      fields: [1] node1   [2] jbankay   [3] 3de-f511   [4] 2026-07-01   [5] 00:14:00,552
    If the username is field N, set:  LOGIN_USER_REGEX = ^(\S+\s+){N-1}(?<user>\S+)
```

For a username in the second column that is:

```
LOGIN_USER_REGEX = ^\S+\s+(?<user>\S+)
```

Unmatched events are still counted, under the user `(unparsed)`, so nothing is
silently dropped — a large `(unparsed)` figure means the regex is wrong, not
that the data is missing.

### Timestamps

UTC by default. Set `REPORT_TIMEZONE` to a Windows time-zone id
(`Eastern Standard Time`, `GMT Standard Time`, …) to convert; the sheets say
which zone they are in. An unknown id warns and falls back to UTC.

### Entry cap

Loki limits entries per query (`LOKI_LOG_LIMIT`, default 5000). Chunking usually
keeps each window under it, but a busy environment can still hit it — the run
warns and tells you to lower `LOKI_MAX_QUERY_DAYS`. **Aggregate totals are
unaffected**; only the per-user detail would be short.

## Proxies and agent variation

Loki is an internal host, so `BYPASS_PROXY` defaults to **true** and the script
clears the system proxy before calling it. Without that, an agent configured to
use the corporate proxy gets a refusal page rather than Loki:

```
ERROR: The requested URL could not be retrieved
Access Denied. Access control configuration prevents your request ...
Generated ... by proxy-host (squid/4.15)
```

The script recognises that response and says so, rather than surfacing the raw
proxy page.

**Agents are not interchangeable here.** Whether a proxy is configured, whether
the internal CA is trusted, and whether the SMTP relay allowlists the host all
vary by agent — the same pipeline can pass on one and fail on another. If runs
are intermittent, check which agent each landed on before suspecting the script,
and consider pinning the pipeline to one known-good agent with a demand.

Set `BYPASS_PROXY=false` only if your Loki genuinely sits behind a proxy.

## Loki query length limit

Loki caps how long a single `query_range` may span (`max_query_length`, commonly
30 or 31 days). A calendar month can exceed it:

```
the query time range exceeds the limit (query length: 744h0m0s, limit: 30d1h)
```

The script therefore fetches the month in chunks of `LOKI_MAX_QUERY_DAYS` days
(default **7**) and stitches the daily figures together. Results are keyed by
date, so overlapping chunk boundaries cannot double count. Lower the value if
your Loki is stricter; raising it gains nothing.

### Day alignment

Loki returns a sample at each step whose value covers the **preceding** range —
the sample stamped `02 Jul 00:00` with `[1d]` counts **01 Jul**. The script
queries from `start + 1d` and labels each sample `timestamp - 1d`. Without that,
every figure would sit a day early and the day before the reporting period would
be pulled in.

Worth knowing if you ever reconcile against Grafana: the dashboard uses
`$__auto` and Grafana handles this alignment for you.

## Notes / gotchas

- **Counts login *events*, not unique users.** A user logging in 5× counts as 5.
  For unique users you'd extract the username from the log line via `| regexp`.
- **Agent network**: the build agent must reach both Loki and the SMTP relay.
- **Nothing to install on the agent.** The PowerShell version needs only
  Windows PowerShell 5.1, which is present by default.
- **Loki retention**: querying "last month" on the 1st needs ≥ ~32 days retention.
  Bump `retention_period` to `1080h` (45d) for safety.
- **TLS**: if Loki is internal HTTPS and the agent doesn't trust the cert, set
  `LOKI_VERIFY_TLS=false`, or point `LOKI_CA_BUNDLE` at your internal CA `.pem`.

## Label mapping

`monthly_report.ps1` maps products to Loki labels — adjust `$ProductMeta` to your
own scheme and confirm the `job` names via the Grafana Label browser:

| Product | `job` | filename frag |
|---|---|---|
| pc | `pclogs` | `pc` |
| bc | `bclogs` | `bc` |
| cc | `cclogs` | `cc` |
| cm | `cmlogs` | `cm` |
