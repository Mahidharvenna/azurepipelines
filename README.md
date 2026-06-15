# Azure DevOps Unified Build + Deploy Pipelines for Guidewire

A reusable Azure DevOps YAML pipeline bundle that consolidates **Build** and
**Release** for multi-product Guidewire InsuranceSuite deployments
(PolicyCenter, BillingCenter, ClaimCenter, ContactManager) into one place per
environment tier.

Replaces a fleet of classic Build pipelines (one per product) and classic
Release pipelines (one per env × product) with **four** tier-scoped YAML
pipelines (Dev / QA / UAT / Preprod) that share a common orchestrator and
templates.

## What it gives you

- **One pipeline per env tier**, multiple env instances per tier (`dev1`,
  `dev2`, `dev3`, ...) selectable at queue time
- **Per-product checkboxes** at queue time — release one product at a time, all
  four, or any combination
- **Branch dropdown** at queue time — pick the release branch to build/deploy
- **One shared deploy template** translated from the typical `GW_TEM_Deployment`
  task group: shutdown Guidewire → swap WAR → patch configs → restart →
  Dashboard-DB tracking
- **DRY template architecture** — change the deploy logic once and all four
  tier pipelines pick it up
- Native support for ContactManager's quirks (different Tomcat path, `COMP=ab`
  legacy mapping)

## Layout

```
.
├── dev-pipeline.yaml            # Entry: Dev tier (instances 1-N)
├── qa-pipeline.yaml             # Entry: QA tier
├── uat-pipeline.yaml            # Entry: UAT tier
├── preprod-pipeline.yaml        # Entry: Preprod tier
└── templates/
    ├── tier-orchestrator.yml    # Shared body: resources, stages, fan-out
    ├── gw-build.yml             # Per-product build (gwb.bat clean/webResources/warTomcatDBCP)
    └── gw-deploy.yml            # Per-product deploy (15-task SSH sequence)
```

Each entry yaml is ~45 lines — defines its tier's parameters and `extends:`
the orchestrator. Change a step in `tier-orchestrator.yml` and all four
pipelines update.

## Prerequisites

- Azure DevOps Server 2020+ (or Azure DevOps Services) — multi-stage YAML
  and Environments required
- Self-hosted build agent with:
  - Windows + Guidewire build tool (`gwb.bat`) installed at a known path
  - Network access to the SQL Server that hosts your deployment-tracking DB
  - Network access to the target Linux Tomcat servers via SSH
- One Git repo per Guidewire product (PC, BC, CC, CM) — branch names must match
  across all four for a coordinated release

## Setup

### 1. Adapt the placeholders

Search and replace these in your fork:

| Placeholder | Replace with |
|---|---|
| `MyOrg/PC`, `MyOrg/BC`, etc. | Your ADO project + repo names (e.g. `Acme/PC`) |
| `MyOrg-` (in build-number `name:`) | Your org prefix |
| `https://your-ado-host.example.com/your-collection` | Your ADO Server URL |
| `C:\path\to\gw-core` | On-agent path to your Guidewire core install |
| `'deployment-secrets'` (variable group name) | Your variable group name |
| `'release/v1.0'` (default release branch) | Your default branch |
| `@example.com`, `@internal.example.com` (mail domain swap) | Your prod/dev mail domains |
| `dbo.ReleaseNotesLog`, `dbo.CurrentBuild`, `dbo.ReleaseNotes` | Your SQL schema if not `dbo` |
| `/opt/tomcat/apache-tomcat`, `/opt/tomcat-cm/apache-tomcat` | Your Tomcat install paths |

### 2. Create one Azure DevOps Environment per (tier, instance)

In ADO → Pipelines → Environments → New environment:
- `dev1`, `dev2`, `dev3`, ...
- `qa1`, `qa2`, ...
- `uat1`, `uat2`, ...
- `preprod1`

Names are lowercase and must match the `envLabel` computed by the orchestrator
(`<tier><instance>`). Add approvers/exclusive-lock on the env per your policy.

### 3. Create one SSH service connection per (instance, product)

In ADO → Project Settings → Service connections → New (SSH):
- `DEV1-PC` → host of Dev1's PolicyCenter Tomcat
- `DEV1-BC` → host of Dev1's BillingCenter Tomcat
- `DEV1-CC` → host of Dev1's ClaimCenter Tomcat
- `DEV1-CM` → host of Dev1's ContactManager Tomcat
- ... and the same for QA / UAT / Preprod

The orchestrator computes the connection name as `<TIER><INSTANCE>-<COMP>`.

### 4. Create the variable group

In ADO → Pipelines → Library → New variable group named `deployment-secrets`
(or whatever you renamed it to). Add:

| Variable | Notes |
|---|---|
| `DBINSTANCE` | SQL Server host for the deployment-tracking DB |
| `DBNAME` | Database name |
| `DBUSER` | DB user |
| `DBPASS` | DB password (secret) |
| `pc_userpass` | GWR runtime user password for PolicyCenter (secret) |
| `bc_userpass` | ... BillingCenter (secret) |
| `cc_userpass` | ... ClaimCenter (secret) |
| `ab_userpass` | ... ContactManager (legacy `ab` mapping — secret) |

Set the group to **"Allow access to all pipelines"** (or grant per pipeline).

### 5. Create the four pipelines

For each of the four entry yamls, in ADO → Pipelines → New pipeline:
- Repo: this devops repo
- Existing YAML file: `/dev-pipeline.yaml` (or qa/uat/preprod)
- Branch: `master`
- Rename to e.g. `MyOrg-Dev`, `MyOrg-QA`, etc.

### 6. First run

Open the pipeline → **Run pipeline**:

```
Release branch:    release/v1.0
DEV instance:      1
PolicyCenter:      [✓]    <-- start small for the first run
BillingCenter:     [ ]
ClaimCenter:       [ ]
ContactManager:    [ ]
```

First run pauses to authorize each repo resource, SSH connection, variable
group, and the Environment. Permit each and the pipeline resumes.

## How the orchestrator works

```
                          ┌───────────────────────────┐
                          │  Pipeline parameters      │
   queue dialog ────────► │  (branch, instance,       │
                          │   PC/BC/CC/CM checkboxes) │
                          └────────────┬──────────────┘
                                       ▼
                        ┌────────── extends: ─────────┐
                        │  tier-orchestrator.yml      │
                        │  - resources (4 repos)      │
                        │  - variables                │
                        │  - stages                   │
                        └────────────┬────────────────┘
                                     ▼
        ┌─── Stage 0: Validate (only if nothing selected) ───┐
        │                                                    │
        ▼                                                    ▼
   ┌─── Stage 1: Build (parallel jobs for ticked products) ──┐
   │  Build_PC ──┐                                           │
   │  Build_BC ──┤  each: checkout repo, gwb.bat steps,      │
   │  Build_CC ──┤        publish <product>-drop artifact    │
   │  Build_CM ──┘                                           │
   └───────────────────────────┬──────────────────────────────┘
                               ▼
   ┌─── Stage 2: Deploy to <tier><instance> ─────────────────┐
   │  Deploy_PC ──┐                                          │
   │  Deploy_BC ──┤  each: deployment job bound to the env,  │
   │  Deploy_CC ──┤        runs the 15-task SSH sequence     │
   │  Deploy_AB ──┘  (CM uses comp=ab internally)            │
   └─────────────────────────────────────────────────────────┘
```

## The 15-task deploy sequence

Translated from a typical Guidewire deployment task group:

1. Pre-deploy Dashboard DB row (status: `Started`)
2. Shutdown Guidewire (`forceShutDown.sh`)
3. Trim catalina.out
4. Delete old log files (>60 days)
5. Delete old WARs (>5 days)
6. Backup existing WAR (timestamp suffix)
7. SCP the new WAR to `$CATALINA_BASE/webapps/`
8. Remove old extracted webapp dir
9. Unzip new WAR
10. Patch config files with the runtime DB password (`sed` on properties + XML)
11. Swap prod mail domain → env-specific mail domain
12. Update log directory in `log4j2.xml`
13. Start Guidewire (`validateStartUp.sh`)
14. (on failure only) Dump last 500 lines of `catalina.out`
15. Post-deploy Dashboard DB row update (status: `Completed` / `Downgrade` /
    `No Changes` / `SourceBranch Changed`), with release notes pulled from
    ADO's build-changes REST API

## ContactManager quirks (handled)

- `COMP` is passed as `ab` (not `cm`) to match legacy task-group case statements
- `CATALINA_BASE` is overridden to `/opt/tomcat-cm/apache-tomcat` (separate
  Tomcat instance)
- Artifact name stays `cm-drop` (built from CM repo source)
- Default checkbox state is `false` — CM tends to deploy less often than the
  others

## Scheduling

Pipeline-level `schedules:` blocks use parameter defaults — since defaults are
all `false`, scheduled triggers won't deploy anything. Use **UI scheduled
triggers** (Pipelines → ⋮ → Triggers → Scheduled) which let you override
parameters per schedule:

- "DEV1 PC nightly" → tick PC only, instance 1, weekday cron
- "DEV1 CM weekly" → tick CM only, instance 1, weekend cron

Schedules layered on the same pipeline run independently.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

Built from a real-world Azure DevOps Server 2022 migration. Generalized and
scrubbed for reuse.
