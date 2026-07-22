# Monthly Guidewire Login Report

A scheduled Azure DevOps pipeline that emails a monthly Excel report of
Guidewire "User Login" counts per environment, sourced from Loki.

Designed to mirror a Grafana "user logins" panel — same Loki label selectors,
same `|= "User Login"` line match — so the report reconciles with the dashboard.

## Files

| File | Purpose |
|---|---|
| `gw-monthly-login-report.yaml` | The scheduled pipeline (cron: 1st of month, 06:00 UTC) |
| `monthly_report.py` | Queries Loki, builds the xlsx, emails it |

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

## Notes / gotchas

- **Counts login *events*, not unique users.** A user logging in 5× counts as 5.
  For unique users you'd extract the username from the log line via `| regexp`.
- **Agent network**: the build agent must reach both Loki and the SMTP relay.
- **Python on the agent**: Python 3 must be installed and on `PATH`, with
  `requests` and `openpyxl` installable via pip.

  `UsePythonVersion@0` is deliberately **not** used: it searches only the agent's
  tool cache (`_work/_tool`), not a normal machine install, so on a self-hosted
  agent it fails with *"did not match any version in Agent.ToolsDirectory"* even
  when Python is present. The pipeline resolves the interpreter itself, trying
  `python`, `python3`, then `py`.

  If it reports none found: install Python 3 on the agent, tick **Add python.exe
  to PATH**, then **restart the agent service** so it picks up the new PATH.
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
