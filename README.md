# Config-Driven Azure DevOps Pipelines for Guidewire

Unified **build + deploy** pipelines for multi-product Guidewire InsuranceSuite
(PolicyCenter, BillingCenter, ClaimCenter, ContactManager), replacing a fleet of
classic Build pipelines and Release pipelines with **three wrapper pipelines**
(Dev / QA / UAT) driven by a single config file.

**The release branch is never typed at queue time.** It lives in
`config/branches.json`, keyed by environment. Change the branch once, every
pipeline picks it up.

## Layout

```
.
├── config/
│   └── branches.json            # env -> product -> release branch  (source of truth)
├── pipelines/
│   ├── release-dev.yaml         # wrapper: DEV  (+ schedule)
│   ├── release-qa.yaml          # wrapper: QA   (+ schedule)
│   └── release-uat.yaml         # wrapper: UAT  (+ schedule)
├── templates/
│   ├── read-config.yml          # resolves branch + product set at run time
│   ├── tier-orchestrator.yml    # shared body: resources, stages, fan-out
│   ├── gw-build.yml             # per-product build (gwb.bat clean/webResources/warTomcatDBCP)
│   └── gw-deploy.yml            # per-product deploy (15-task SSH sequence)
└── reports/                     # (separate) monthly login report -> Excel -> email
```

## How it works

```
        queue OR schedule
                │
                ▼
┌─ Stage 1: ReadConfig ───────────────────────────────────┐
│  reads config/branches.json for this env label          │
│  emits  branch_pc / branch_bc / branch_cc / branch_cm   │
│  emits  run_pc    / run_bc    / run_cc    / run_cm      │
│  fails fast if the env is missing or nothing would run  │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌─ Stage 2: Build ────────────────────────────────────────┐
│  4 jobs defined; each runs only if run_<comp> = true    │
│  each checks out its own configured branch              │
│  publishes <comp>-drop artifact                         │
└──────────────────────────┬───────────────────────────────┘
                           ▼
┌─ Stage 3: Deploy to <TIER><INSTANCE> ───────────────────┐
│  4 deployment jobs bound to the ADO Environment         │
│  each runs only if run_<comp> = true                    │
│  15-task SSH sequence + Dashboard-DB bookkeeping        │
└──────────────────────────────────────────────────────────┘
```

## The config file

All four centres normally sit on the **same** branch for a given release, so
`config/branches.json` records **one branch per env** plus the list of products
in that release:

```json
{
  "QA7": {
    "branch":   "maintenance/rel-2026.08",
    "products": ["pc", "bc", "cc"]
  },
  "UAT1": {
    "branch":   "maintenance/rel-2026.06",
    "products": ["pc", "bc", "cc"]
  }
}
```

- **`branch`** — applies to every product listed. Change it in one place when
  the release rolls; all centres follow.
- **`products`** — which centres are in this release. A product not listed
  never builds or deploys for that env (the equivalent of `NA` in a release
  matrix). CM is usually omitted.
- The **env label** is `<TIER><INSTANCE>` uppercased — `DEV2`, `QA7`, `UAT1` —
  built from the pipeline's tier plus the instance chosen at queue time.

### Optional per-env flags

**`genDataDictionary`** — `gwb.bat genDataDictionary` is disabled in the classic
builds almost everywhere. It is an **environment-level** switch: turn it on for
an env and every centre in that release generates the data dictionary. Absent
means off.

```json
"UAT3": {
  "branch":   "releases/rel-2026.04",
  "products": ["pc", "bc", "cc"],
  "genDataDictionary": true
}
```

The step is always present in the build job but carries a runtime condition, so
it shows as **skipped** on every other env — visible in the log rather than
silently missing. It runs after `webResources` and before `warTomcatDBCP`,
matching the classic PC build.

Moving it to a different env is a config edit, not a pipeline change.

> Expect a materially longer build where this is on — dictionary generation runs
> for several minutes per centre.

### Exceptions

When one centre is off on its own branch, add an `overrides` block. Everything
else still follows `branch`:

```json
"DEV4": {
  "branch":    "feature/example-migration",
  "products":  ["pc", "bc"],
  "overrides": { "bc": "feature/example-org-merge" }
}
```

The **ReadConfig** log prints the resolved branch per product and marks any
override, so a run always shows exactly what it used.

### Keeping it in sync with a release matrix

If you track releases in a spreadsheet (release → branch per product → list of
target envs), note that this file is the **transpose**: it records the *current*
branch per env, not release history. One env appearing in several releases
collapses to a single entry here — whichever release it is on now.

## Manual vs scheduled runs

Each wrapper exposes `useConfigProducts`:

| | `useConfigProducts` | Result |
|---|---|---|
| **Scheduled run** | `true` (the default) | Deploys every product the config lists for that env. No human input needed. |
| **Manual, full env** | leave ticked | Same as above — pick the instance and go. |
| **Manual, one product** | untick it | Only the product checkboxes you tick are built/deployed (still using the config's branch). |

## Scheduling

Scheduling is split across two places, and the split is forced by ADO:

| Piece | Lives in | Why |
|---|---|---|
| **When it fires** (`cron`) | the wrapper YAML | `schedules:` is parsed at **compile time**, before any agent runs — it cannot read a repo file |
| **Whether it runs** (`enabled`) | `config/branches.json` | evaluated at **run time**, so it can come from the config |

### Turning a schedule on/off

Add a `schedule` block to any env:

```json
"QA12": {
  "branch":   "maintenance/rel-2026.06_hf",
  "products": ["pc"],
  "schedule": {
    "enabled": false,
    "cron":    "0 8 * * 2,4",
    "note":    "paused during hotfix testing"
  }
}
```

With `enabled: false`, a **scheduled** run starts, logs why it is standing down,
and exits without building or deploying anything:

```
Scheduled run for 'QA12' is DISABLED in config/branches.json.
Nothing will be built or deployed. Flip schedule.enabled to true to resume.
```

**Manual runs ignore the toggle** — you can always deploy on demand while the
schedule is paused. An env with no `schedule` block behaves as enabled.

So pausing a nightly deploy is a one-line config edit: no pipeline YAML change,
no ADO UI change, and the pause is visible in git history.

### The `cron` field is a record, not the trigger

The `cron` and `note` values are documentation — they let you see the intended
cadence next to the branch. **The wrapper's `schedules:` block is authoritative**
and must mirror them. Changing `cron` in the config alone changes nothing.

If that duplication becomes a problem, the fix is a small generator script that
rewrites each wrapper's `schedules:` block from the config and is run on commit.
Worth it above roughly 20–30 schedules; below that, mirroring two lines is
cheaper than owning a generator.

Also, a scheduled run uses **parameter defaults**. That is why
`useConfigProducts` defaults to `true` and `envInstance` has a default — without
them a scheduled run would deploy nothing.

**To schedule a second instance of the same tier on its own cadence**, copy the
wrapper and change the default `envInstance` and the cron:

```yaml
# pipelines/release-qa7-nightly.yaml
schedules:
- cron: '0 4 * * 1-5'
  displayName: 'QA7 nightly'
  branches: { include: [master] }
  always: false
parameters:
- name: envInstance
  type: string
  default: '7'          # <-- the only real change
# ... rest identical, extends the same orchestrator
```

`always: false` means the run is skipped when nothing has been committed to
`master` since the last run.

## Prerequisites

- Azure DevOps Server 2020+ (or Azure DevOps Services) — multi-stage YAML and
  Environments required. Tested against ADO Server 2022 RTW, which is why the
  branch is applied via `git checkout` rather than `ref:` on `checkout:`.
- Self-hosted Windows build agent with the Guidewire build tool (`gwb.bat`),
  network access to the deployment-tracking SQL Server, and SSH to the target
  Linux Tomcat hosts.
- One Git repo per product (PC, BC, CC, CM).

## Setup

### 1. Adapt the placeholders

| Placeholder | Replace with |
|---|---|
| `MyOrg` (build-number prefix, REST project name) | Your org / project name |
| `PC`, `BC`, `CC`, `CM` (repo names in `tier-orchestrator.yml`) | Your repo names |
| `https://your-ado-host.example.com/your-collection` | Your ADO Server URL |
| `C:\path\to\gw-core` | On-agent path to your Guidewire core install |
| `'deployment-secrets'` | Your variable group name |
| `@example.com` / `@internal.example.com` | Your prod / non-prod mail domains |
| `dbo.ReleaseNotesLog`, `dbo.CurrentBuild`, `dbo.ReleaseNotes` | Your SQL schema |
| `/opt/tomcat/apache-tomcat`, `/opt/tomcat-cm/apache-tomcat` | Your Tomcat paths |
| `config/branches.json` contents | Your real envs and branches |

### 2. Create one ADO Environment per (tier, instance)

Pipelines → Environments → New: `dev1`, `dev2`, `qa7`, `uat1`, ... (lowercase —
must match the `envLabel` the orchestrator computes). Add approvals per policy.

### 3. Create one SSH service connection per (instance, product)

Named `<TIER><INSTANCE>-<COMP>` — e.g. `DEV1-PC`, `QA7-BC`, `UAT1-CM`.

### 4. Create the variable group

Library → New variable group → `deployment-secrets`:

| Variable | Notes |
|---|---|
| `DBINSTANCE` / `DBNAME` / `DBUSER` / `DBPASS` | Deployment-tracking DB (DBPASS secret) |
| `pc_userpass` / `bc_userpass` / `cc_userpass` / `ab_userpass` | GWR runtime passwords (secret) |

### 5. Register the three pipelines

For each of `pipelines/release-dev.yaml`, `release-qa.yaml`, `release-uat.yaml`:
New pipeline → Existing YAML file → pick the file → branch `master` → Save.

### 6. First run

Run `release-dev.yaml`, instance `1`, leave `useConfigProducts` ticked. The first
run pauses to authorize each repo resource, SSH connection, variable group, and
the Environment — permit each and it resumes. Check the **ReadConfig** log first:
it prints exactly which branch and which products it resolved.

## ContactManager quirks (handled)

- `COMP` is passed as `ab` (not `cm`) to match legacy task-group case statements
- `CATALINA_BASE` overridden to `/opt/tomcat-cm/apache-tomcat`
- Artifact stays `cm-drop` (built from the CM repo)
- Usually `"cm": "NA"` in the config — CM deploys far less often than the rest

## License

MIT — see [LICENSE](LICENSE).
