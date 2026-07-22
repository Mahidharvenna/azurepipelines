# Monthly Guidewire Login Report

A scheduled Azure DevOps pipeline that emails a monthly Excel report of
Guidewire "User Login" counts per environment, sourced from Loki.

Designed to mirror a Grafana "user logins" panel — same Loki label selectors,
same `|= "User Login"` line match — so the report reconciles with the dashboard.

## Files

| File | Purpose |
|---|---|
| `gw-monthly-login-report.yaml` | The scheduled pipeline (cron: 1st of month, 06:00 UTC) |
| `monthly_report.ps1` | **Used by the pipeline.** Queries Loki, builds the xlsx, emails it |
| `monthly_report.py` | Equivalent in Python, if you would rather use that |

### Why PowerShell

The pipeline runs the **PowerShell** version because it needs nothing installed
on the agent: no Python runtime, no `pip` packages, no PowerShell Gallery access
and no Excel. The `.xlsx` is written directly as OOXML (a zip of XML parts) via
`System.IO.Compression`, which ships with .NET.

That matters on a locked-down build agent. `UsePythonVersion@0` in particular is
a trap on self-hosted agents -- it only searches the agent tool cache
(`_work/_tool`), not a normal machine install, so it fails with *"did not match
any version in Agent.ToolsDirectory"* even when Python is present.

The Python version is kept for sites that already have Python plus `requests`
and `openpyxl`. To use it, swap the pipeline's script step back.

## One-time setup

### 1. Create the Variable Group

Pipelines → Library → **+ Variable group** → name it **`gw-reports-secrets`**:

| Variable | Example | Secret? |
|---|---|---|
| `LOKI_URL` | `https://your-loki-host.example.com:3100` | no |
| `LOKI_VERIFY_TLS` | `true` (set `false` if agent rejects the cert) | no |
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

`monthly_report.py` maps products to Loki labels — adjust `PRODUCT_META` to your
own scheme and confirm the `job` names via the Grafana Label browser:

| Product | `job` | filename frag |
|---|---|---|
| pc | `pclogs` | `pc` |
| bc | `bclogs` | `bc` |
| cc | `cclogs` | `cc` |
| cm | `cmlogs` | `cm` |
