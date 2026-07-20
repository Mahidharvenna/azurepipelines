# Config-Driven Azure DevOps Pipelines for Guidewire

Unified **build + deploy** for multi-product Guidewire InsuranceSuite
(PolicyCenter, BillingCenter, ClaimCenter, ContactManager), replacing a fleet of
classic Build pipelines and Release pipelines with **three wrapper pipelines**
(Dev / QA / UAT) driven by a single config file.

**The release branch is never typed at queue time.** It lives in
`config/branches.json`, keyed by environment. Change it once, every pipeline
picks it up.

---

## Layout

```
config/
  branches.json            # env -> branch + products  (source of truth)
pipelines/
  release-dev.yaml         # wrapper: DEV  (+ schedule)
  release-qa.yaml          # wrapper: QA   (+ schedule)
  release-uat.yaml         # wrapper: UAT  (+ schedule)
templates/
  read-config.yml          # resolves branch + product set at run time
  tier-orchestrator.yml    # shared body: resources, variables, stages
  gw-build.yml             # per-product build (gwb.bat clean/webResources/warTomcatDBCP)
  gw-deploy.yml            # per-product deploy (15-task SSH sequence)
reports/                   # unrelated: monthly login report -> Excel -> email
```

## How a run works

```
        queue OR schedule
                |
                v
  Stage 1  ReadConfig
           reads config/branches.json for this env label
           emits branch_<comp> and run_<comp> per centre
           fails fast on a missing env or an empty product set
                |
                v
  Stage 2  Build            (4 jobs; each runs only if run_<comp> = true)
           checkout -> switch to configured branch -> gwb.bat -> publish <comp>-drop
                |
                v
  Stage 3  Deploy           (4 deployment jobs, bound to the ADO Environment)
           download artifact -> verify WAR -> 15-task SSH sequence
```

## The config file

All centres normally share one branch per release, so each env records **one
branch** plus the centres in that release:

```json
"DEV2": {
  "branch":   "maintenance/rel-2026.06",
  "products": ["pc", "bc", "cc"]
}
```

- **`branch`** applies to every listed centre.
- **`products`** — a centre not listed never builds or deploys for that env.
- The **env label** is `<TIER><INSTANCE>` uppercased: `DEV1`, `QA2`, `UAT1`.

### Optional per-env keys

| Key | Effect |
|---|---|
| `overrides` | `{"bc": "feature/x"}` — one centre on a different branch |
| `genDataDictionary` | `true` — run `gwb.bat genDataDictionary` for this env (adds several minutes per centre) |
| `schedule.enabled` | `false` — a scheduled run stands down; **manual runs still work** |
| `schedule.cron` / `.note` | documentation only — see below |

## Manual vs scheduled runs

Each wrapper exposes `useConfigProducts`:

| | Setting | Result |
|---|---|---|
| Scheduled run | `true` (default) | Deploys whatever the config lists for that env |
| Manual, whole env | leave ticked | Same — pick the instance and go |
| Manual, one centre | untick | Only the ticked checkboxes deploy (branch still from config) |

## Scheduling — and one hard limit

| Piece | Lives in | Why |
|---|---|---|
| **When it fires** (`cron`) | the wrapper YAML | `schedules:` is parsed at **compile time** and cannot read a repo file |
| **Whether it runs** (`enabled`) | `config/branches.json` | evaluated at **run time** |

So pausing a nightly deploy is a one-line config edit — no pipeline change, no
UI change, and the pause is visible in git history. **Changing the time is a
YAML edit.** The `cron` value in config is a human-readable record that must
mirror the wrapper; changing it alone does nothing.

A scheduled run uses **parameter defaults**, which is why `useConfigProducts`
defaults to `true` and `envInstance` has a default. One wrapper can therefore
auto-schedule only its default instance — to schedule a second one, copy the
wrapper and change the default `envInstance` and cron.

---

## Setup

### 1. Replace the placeholders

| Placeholder | Replace with |
|---|---|
| `MyOrg` (build-number prefix, REST project name in `gw-deploy.yml`) | your project name |
| `PC` / `BC` / `CC` / `CM` in `tier-orchestrator.yml` | your repo names |
| `https://your-ado-host.example.com/your-collection` | your ADO Server URL |
| `C:\path\to\gw-core` | on-agent path to your Guidewire core install |
| `'deployment-secrets'` | your variable group name |
| `@example.com` / `@internal.example.com` | your prod / non-prod mail domains |
| `dbo.ReleaseNotesLog` / `dbo.CurrentBuild` / `dbo.ReleaseNotes` | your schema |
| `catalinaBase*` values | your Tomcat paths — **verify, see below** |
| `config/branches.json` | your real envs and branches |

### 2. Verify the Tomcat paths — do not assume

Tomcat installs are commonly **not** uniform across centres:

| Centre | Typical `CATALINA_BASE` |
|---|---|
| PC | `/opt/tomcat/apache-tomcat` |
| BC | `/opt/tomcat/apache-tomcat` |
| CC | `/opt/tomcat-cc/apache-tomcat` |
| CM | `/opt/tomcat-cm/apache-tomcat` |

Confirm each from a classic release log — the *"Copy Latest war file to Server"*
step prints the exact target it copied to:

```
Copying file ...\drop\dist\wars\TomcatDbcp\cc.war
to /opt/tomcat-cc/apache-tomcat/webapps/cc.war on remote machine.
```

A wrong value does not fail fast — the deploy shuts down, unpacks and restarts
against whatever path you gave it.

### 3. ADO prerequisites

- **Environments** — one per `<tier><instance>`, lowercase: `dev1`, `qa7`, `uat1`
- **SSH service connections** — named `<TIER><INSTANCE>-<COMP>`: `DEV1-PC`, `UAT1-CC`
- **Variable group** — `DBINSTANCE`, `DBNAME`, `DBUSER`, `DBPASS`, plus
  `pc_userpass`, `bc_userpass`, `cc_userpass`, `ab_userpass` (secrets). Grant the
  pipelines access under **Pipeline permissions**.

### 4. Register the pipelines

For each wrapper: New pipeline → Existing YAML file → pick the file → `master`.

---

## First run: prove one environment before widening

Do **not** start with all four centres and a schedule. The recommended order:

1. Config lists **one env, one centre** (`"DEV1": {"branch": "...", "products": ["pc"]}`)
   with `schedule.enabled: false`
2. Run `release-dev` manually, instance `1`, `useConfigProducts` ticked
3. Authorise each resource when prompted (first run only)
4. **Read the ReadConfig log before anything builds** — it prints exactly what
   it resolved:
   ```
   Env            : DEV1
   Default branch : maintenance/rel-2026.06
   In release     : pc
   Product source : config file
   PC  : maintenance/rel-2026.06 -> run=True
   BC  : not in this release -- skipping
   ```
5. Check **"Show staged artifact layout"** in the build — confirm the WAR is at
   `dist/wars/TomcatDbcp/<comp>.war` and not nested deeper
6. Confirm the app actually starts — a green pipeline is not proof
7. Only then add centres, then envs, then turn schedules on

### Why step 5 matters

`CopyFiles@2` preserves paths relative to its `SourceFolder`. A multi-repo
checkout can nest sources one level deeper, which shifts the WAR's path *inside
the artifact*. The deploy globs for it rather than assuming a fixed path, and
`failOnEmptySource: true` makes a mismatch fail loudly — because the failure
mode otherwise is nasty: the copy silently does nothing, the following steps
still delete the webapp and unzip a WAR that was never delivered, and you find
out at startup as a `NoClassDefFoundError` deep in OSGi.

---

## Scope: lower environments only

These pipelines target **Dev / QA / UAT**, where each environment has one host
per centre and one SSH connection named `<ENV>-<COMP>`.

They are **not** suitable for clustered environments as-is. Production estates
typically run several nodes per centre (`PROD_CC_IT1`, `IT2`, `IT3`, …), needing
a deploy template that fans out over a node list plus decisions about rolling
restarts and draining. Check whether any *lower* environment is also clustered —
if an env has connections like `UAT1-PCi1`, `UAT1-PCe1` alongside `UAT1-PC`,
deploying to only the plain one leaves the other nodes on the old WAR.

## ContactManager

- `COMP` is passed as `ab` (not `cm`) to match legacy task-group case statements
- Artifact stays `cm-drop`, built from the CM repo
- Often omitted from `products` — CM deploys far less often than the rest

## Platform notes

Tested against **Azure DevOps Server 2022 RTW**, which drives several choices:

- `ref:` is not supported on `checkout:` → the branch is applied by a runtime
  `git fetch` + `git checkout`, which is *why* config-driven branches work
- template expressions are not allowed in `resources.repositories[].ref`
- `PublishPipelineArtifact@1` is unavailable → `PublishBuildArtifacts@1`
- `DownloadBuildArtifacts@1` is unavailable → `@0`

## License

MIT — see [LICENSE](LICENSE).
