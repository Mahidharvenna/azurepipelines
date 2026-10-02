# Guidewire admin data load

Loads one admin-data XML file from this repo into one Guidewire environment.
It is for **administrative data only**: users, groups, roles, activity
patterns, authority limits, regions, security zones, holidays and the like.
Guidewire does not support loading claims, policies or other business data
this way, and it does not fully validate what it imports.

The pipeline calls the server's **ImportToolsAPI** web service, the SOAP API
that Guidewire's `import_tools` command uses. Nothing needs installing on the
servers, and no SSH connection is used. The agent needs HTTPS access to the
application and a Guidewire user with the `soapadmin` permission.

```
admin-data-load/
  gw-admin-data-load.yaml   the pipeline: locate tool, find Python, run one action
  admin_data_load.py        the tool (Python 3.9+, standard library only)
  .gitattributes            keeps both files LF for the Linux agent
```

The Guidewire version does not need to be known, and can differ between
environments. The tool reads the server's own ImportToolsAPI WSDL and takes
everything about the call from it: namespace, operation, element names,
SOAPAction and endpoint. `check` prints what it found.

**Read [What Guidewire does with the file](#what-guidewire-does-with-the-file)
before the first import.** An import overwrites existing records without
asking.

## Quick start

1. **Create the variable group, once.** Pipelines → Library → *+ Variable
   group* → name it **`gw-admin-data`**. Add one URL per environment, plus the
   credentials (see [Variables](#variables)):

   | Name | Value | Secret |
   |---|---|---|
   | `DEV_1` | `https://gw-dev1-pc.example.internal:9443/pc` | no |
   | `DEV_1_BC` | `https://gw-dev1-bc.example.internal:9443/bc` | no |
   | `username` | `svc_import` | no |
   | `password` | *(the password)* | **yes** |

   Then, under *Pipeline permissions*, allow the pipeline (or grant access when
   the first run asks for it). Before you add a production environment, set up
   [Protecting production](#protecting-production).
2. **Register the pipeline, once.** Pipelines → New pipeline → this repo →
   *Existing Azure Pipelines YAML file* → choose the **branch that holds this
   folder** → `admin-data-load/gw-admin-data-load.yaml` → Save. Rename it
   (⋯ → Rename/move), for example to **GW-AdminData-Load**.

   The file to load is read from the branch the run checks out. Set the
   pipeline's default branch to the branch that holds your admin-data files
   (Edit → ⋯ → Triggers → YAML → Get sources → *Default branch for manual and
   scheduled builds*), or pick that branch in the Run dialog every time.
3. **Commit the XML file** to that branch (new files need `git add`), for
   example `admin-data/dev_1/roles.xml`. Take it from *Administration →
   Utilities → Export Data* in the source environment rather than writing it
   by hand: the export has the right format for that release.
4. **Run `check`.** Choose *action* `check`, *env* `DEV_1`, *product* `pc`.
   Nothing is written. The run shows that the agent reaches the server, that
   the WSDL can be read, the exact endpoint the import will use, and which
   operation and parameters it will call. With credentials set, it also logs
   in through `SystemToolsAPI.getVersion` (read-only) and prints the server
   version. **The credentials are proven only when that login passes**; if
   `check` says *NOT verified*, a wrong password or a missing `soapadmin`
   permission shows up only on import.
5. **Run `validate`** with the same values, plus *file*
   `admin-data/dev_1/roles.xml`. Nothing is written. The run shows the file's
   root element and namespace, the root's `version` and `usePeriodicFlushes`
   attributes, element counts, the public-id prefixes it uses, size, SHA-256
   and the size of the SOAP request. Look at the counts and prefixes before
   loading: the prefixes show which environment the data was exported from.
6. **Run `import`** with the same values. The run repeats `check` and
   `validate`, sends the file, and prints the server's result: `Ok`, every
   `ErrorLog` entry, the per-entity `Summaries`, `ParseTime` and `WriteTime`,
   then the number of `Details` entries and the first 10.
7. **After the import**, run the **User Exception** and **Group Exception**
   batch processes (Server Tools → Batch Process Info), as Guidewire advises
   after an admin-data import, and spot-check the data in the application.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `action` | `check` | `check` and `validate` write nothing; `import` loads the file. |
| `env` | `none` | Environment name, e.g. `DEV_1`, `QA3`: 1–32 letters, digits or `_`. The pipeline upper-cases it, replaces `$` with `_` and drops spaces before using it. `none`, `USERNAME`, `PASSWORD`, names ending in `_USERNAME` or `_PASSWORD`, names starting `ADL_`, the setting names (`GW_TIMEOUT`, `PROD_ENV_PATTERN`, ...) and `PYTHON_EXE` are refused. |
| `product` | `pc` | `pc` PolicyCenter, `bc` BillingCenter, `cc` ClaimCenter, `cm` ContactManager (served at `/ab`). |
| `file` | `none` | The XML file's path from the repo root (validate and import). Must be a `.xml` file inside the repo, 200 MB or less. Backslashes are accepted. A path containing `$(` fails the run before anything else runs. |
| `confirm` | `none` | Production only: type the env name again, exactly as the log shows it (in capitals). |

Text parameters default to `none` rather than empty, because this server's Run
dialog marks an empty default as *Required*.

## Variables

All variables live in the group `gw-admin-data`. `<ENV>` and `<PRODUCT>` are
the Run-dialog values upper-cased, so env `dev_1` with product `bc` reads
`DEV_1_BC` first, then `DEV_1`.

| Variable | Required | Meaning |
|---|---|---|
| `<ENV>`, e.g. `DEV_1` | yes, or `<ENV>_<PRODUCT>` | The application's base URL, **including its path**: `https://gw-dev1-pc.example.internal:9443/pc`. Not the web-service URL. |
| `<ENV>_<PRODUCT>`, e.g. `DEV_1_BC` | — | This product's URL. Takes precedence over `<ENV>` when set. Needed for each product that `<ENV>` does not point at: a `/pc` URL used with product `bc` is refused, and the message names the variable to add. |
| `username` / `password` (secret) | for import | The Guidewire integration user, shared by all environments. |
| `<ENV>_USERNAME` / `<ENV>_PASSWORD` (secret) | — | This environment's own login. Takes precedence over `username`/`password`. Set both or neither: one without the other is refused, naming both. |
| `GW_VERIFY_TLS` | — | `false` switches certificate checking off. Default `true`. Use for testing only. |
| `GW_CA_BUNDLE` | — | Path **on the agent** to a PEM file holding the internal CA. It is added to the default trust. A missing file is ignored with a warning. |
| `BYPASS_PROXY` | — | Default `true`: connect directly, ignoring the agent's proxy settings. Set it to `false` to use the proxy. |
| `GW_TIMEOUT` | — | Seconds to wait for the server. Default `600`. A large import is answered only when it has finished. The step itself stops after 30 minutes. |
| `GW_AUTH` | `auto` | How to sign in: `auto` sends the Guidewire SOAP authentication header when the WSDL declares it (Guidewire's WS-I services do), else HTTP Basic; or force `header`, `basic`, or `both`. One method per request is the safe choice: Guidewire refuses a request carrying both with *Multiple authentication methods provided*. |
| `GW_IMPORT_OPERATION` | — | Use this WSDL operation instead of the first found of `importXmlData`, `importXml`, `importData`. |
| `PROD_ENV_PATTERN` | — | A regular expression (case-insensitive) for production env names. Default `^(PROD\|PRD\|PRODUCTION)`. Every run prints the pattern in force and whether the env matched it. |
| `GW_ALLOW_HTTP` | — | `true` allows an `http://` URL. That sends the password unencrypted. |
| `GW_ALLOW_CONTEXT_MISMATCH` | — | `true` allows a URL whose last path segment is not the product's context. |

Optional variables can be left out of the group entirely. ADO then passes the
literal text `$(NAME)`, and the tool treats that as unset. (Before it does,
the agent also looks for an environment variable of exactly that name in its
own service environment, so keep the group complete rather than relying on
the agent's environment.)

### How the values reach the tool

The step maps every value to an `ADL_*` environment variable, and the tool
reads nothing else:

| Step env key | Mapped from |
|---|---|
| `ADL_URL_PRODUCT` / `ADL_URL_ENV` | `$(<ENV>_<PRODUCT>)` / `$(<ENV>)` |
| `ADL_USER` / `ADL_PASSWORD` | `$(username)` / `$(password)` |
| `ADL_USER_ENV` / `ADL_PASSWORD_ENV` | `$(<ENV>_USERNAME)` / `$(<ENV>_PASSWORD)` |
| `ADL_<NAME>` for each optional setting | `$(<NAME>)`, e.g. `ADL_GW_TIMEOUT` from `$(GW_TIMEOUT)` |
| `ADL_ACTION`, `ADL_ENV`, `ADL_PRODUCT`, `ADL_FILE`, `ADL_CONFIRM` | the Run-dialog values |

The keys are `ADL_*` because the agent exports every plain (non-secret)
pipeline variable into the step's environment **after** applying `env:`, so a
plain variable with the same name as a key would silently replace the
mapping. **Never add a plain variable named like one of these `ADL_*` keys**
to the group, or at queue time.

A secret variable reaches the tool only because the YAML maps it explicitly.
Never put a password into a non-secret variable. To run the tool by hand, set
the same `ADL_*` variables in your shell.

## What Guidewire does with the file

From Guidewire's documentation (PolicyCenter 10.1.2 System Administration
Guide, and the ImportToolsAPI source); check them against your release.

- **Admin data only.** Guidewire supports this import for administrative
  tables such as users, groups and roles. It does not fully validate the
  data. `validate` warns when a top-level element is not a known admin entity
  type; it checks well-formedness only. To check the file against your
  release's schema, generate `pc_import.xsd` (or `bc_`, `cc_`, `ab_`) with
  `gwb genImportAdminDataXsd`.
- **Existing records are overwritten.** Records are matched by public ID. A
  record that matches is overwritten field by field with the file's values,
  and an empty element in the file sets that field to **null**. There is no
  prompt (unlike the *Import Data* screen) and no concurrent-change check. A
  record identical to the database is left alone.
- **Environments that exchange data need different `PublicIDPrefix`
  values** (`config.xml`). Otherwise a file exported from one environment can
  overwrite unrelated records that happen to share a public ID in another.
  `validate` prints the prefixes in the file, so you can see where it came
  from.
- **Deletion: an assumption, not a guarantee.** Top-level records left out of
  the file are not expected to be deleted. But owned arrays, such as a group's
  users or a role's privileges, may be **replaced** by the file's contents:
  Guidewire documents that for the *Import Data* screen, and the API's
  behaviour is not documented. Test the effect in a lower environment before
  relying on it in production.
- **Side effects.** Guidewire documents that this import does not fully
  validate what it loads and generates no events, so downstream integrations hear nothing about the
  change. The data takes effect immediately, with no restart.
- **After importing**, run the **User Exception** and **Group Exception**
  batch processes. The tool reminds you after a successful import.
- **Dependency order.** Records refer to each other by public ID. References
  within one file may point forward, so related records can stay in one
  file. Across files, load what is referred to first: for example roles,
  then users, then the groups that list them.
- **Size.** `MaximumFileUploadSize` in `config.xml` (default 20 MB) must
  exceed what you import. The SOAP request is larger than the file, because
  the XML is escaped. `validate` prints the request size, and `import` warns
  when the request is over 20 MB.

## Safety

- **Check first.** The default action is `check`. `check` and `validate` never
  send anything that could change data (`check`'s login is the read-only
  `getVersion`). `import` repeats both and stops at the first problem, before
  any data is sent.
- **Production needs `confirm`.** An env matching `PROD_ENV_PATTERN` is refused
  (exit 2, nothing sent) unless `confirm` repeats the env name exactly. Every
  run prints the pattern in force and whether the env matched, so a pattern
  that misses production is visible.
- **Wrong-application guard.** The last segment of the URL must be the
  product's context (`/pc`, `/bc`, `/cc`, `/ab` for cm), so PolicyCenter data
  is not sent to BillingCenter by mistake. `validate` also warns when the
  file's root namespace names another product (e.g. `.../pc/...` loaded with
  product `bc`).
- **Only files from the repo.** The path is resolved with symlinks followed.
  Anything outside the checked-out repo, `.git/`, non-`.xml` files, empty
  files and files over 200 MB are refused. A file with a DOCTYPE is refused,
  because a DTD can define entities that expand or read files when the XML is
  parsed.
- **UTF-8 files are sent unchanged.** The tool decodes the file, escapes it
  once (`&`, `<`, `>`, and CR, which would otherwise become LF) and sends it as
  the operation's string parameter. No CDATA wrapping, no reformatting. A
  byte-order mark is not sent. Guidewire turns that string into UTF-8 bytes
  and parses them with the file's own XML declaration, so `import` refuses
  (exit 2) a UTF-16 or UTF-32 file, and a file declaring another encoding
  that contains non-ASCII characters: re-save it as UTF-8.
- **Credentials go only to the configured server.** The endpoint is the
  WSDL's SOAP 1.1 address from `/ws/` onward, appended to the configured URL,
  so a WSDL that names an internal host or a path without a proxy's prefix
  cannot redirect the call. Imported schemas are fetched the same way.
  Redirects are never followed. A URL that contains credentials is refused.
- **Nothing secret in the log.** The password and the Authorization header are
  never printed, and are masked if a server echoes them. A URL value the tool
  refuses is never printed, only the name of the variable it came from: if a
  name collided with another variable, the value could be a secret. An
  accepted URL is printed as scheme, host, port and path only. Of the file,
  only element names, counts, the root's `version` and `usePeriodicFlushes`,
  public-id prefixes, size and hash are printed. Every line quoted from a
  server or a file is cleaned before printing: control characters are
  removed, long lines are cut, and `##vso[` / `##[` become `#_#vso[` /
  `#_#[`, so a response cannot issue pipeline commands.
- **Run-dialog values cannot pull in secrets.** Step env values are
  macro-expanded, secrets included, just before the step runs. So `env` is
  upper-cased with `$` replaced and spaces dropped when the run is queued,
  before it is used in any variable name; `confirm` has `$` replaced; and a
  `file` containing `$(` fails the run in a first step. Reserved env names
  are refused, because the URL would be read from another variable: a
  credential (`USERNAME`, `PASSWORD`, `*_USERNAME`, `*_PASSWORD`), a step key
  (`ADL_*`), a setting or `PYTHON_EXE`. None of the values is pasted into
  the script text, so a quote typed into *file* cannot run as a command.

## Protecting production

The `confirm` check stops a production load by accident, not on purpose. Set
up these before the group holds production values:

- **Limit queue-time variables.** Project settings (or Collection settings) →
  Pipelines → Settings → turn on *Limit variables that can be set at queue
  time*. Otherwise someone who can queue the pipeline can add variables at
  queue time, for example a URL for a new env name that points at their own
  host, which would then receive the shared credentials, or a
  `PROD_ENV_PATTERN` that misses production.
- **Approval and branch control.** Under *Approvals and checks* of the
  variable group, add an *Approval* and a *Branch control* check (allow only
  `refs/heads/<your release branch>`), so a production load waits for someone
  else and loads only reviewed files. Options:
  - On `gw-admin-data` itself, if every run should be approved.
  - On a second group, e.g. `gw-admin-data-prod`, that holds only the
    production URLs and logins, referenced only for production envs:
    ```yaml
    variables:
    - group: 'gw-admin-data'
    - ${{ if or(startsWith(upper(parameters.env), 'PROD'), startsWith(upper(parameters.env), 'PRD')) }}:
      - group: 'gw-admin-data-prod'
    ```
    Keep this condition in step with `PROD_ENV_PATTERN`.
  - On an ADO Environment: Pipelines → Environments → create e.g.
    `gw-admin-data-prod`, add the checks, and convert the job into a
    deployment job that targets it. A deployment job does not check out the
    repo by itself, so add `- checkout: self`.
- **Prove the production login with `check`.** `check` proves the
  credentials only when its `SystemToolsAPI.getVersion` login passes. If it
  says *NOT verified*, the first real use of the production credentials is
  the import.
- **Keep `ADL_*` names out of the group.** Never add a plain variable named
  like a step env key (see [How the values reach the tool](#how-the-values-reach-the-tool)).

The YAML ships as a plain job so it works without anyone creating
environments first.

## What the Guidewire user needs

- To be **active**, with the right password, and able to log in through the
  web-service API.
- A role with the **`soapadmin`** system permission (*SOAP administration*).
  Every ImportToolsAPI operation checks it and answers a
  `WsiAuthenticationException` fault without it. The base roles `superuser`
  and `user_admin` have it. (`viewadmin` is needed only for the
  *Import Data* screen, not for this API.) This is from the 10.x source;
  confirm it for your release.

Use a dedicated integration user (e.g. `svc_import`), not a person's account.
The import is recorded in Guidewire as made by that user.

## Exit codes

| Code | Meaning | Examples |
|---|---|---|
| 0 | OK | check passed; file valid; import finished with no reported errors |
| 1 | Failed | server unreachable, TLS not trusted, WSDL missing or unusable, login refused in check, malformed XML, SOAP fault, HTTP 401/403, `Ok=false` or `ErrorLog` entries in the result, reply unreadable, timeout |
| 2 | Refused, nothing sent | env `none`, invalid or reserved, URL missing or refused, half a per-env login, path outside the repo, not `.xml`, DOCTYPE, too large, file not UTF-8 with non-ASCII text, context mismatch, production without `confirm`, invalid setting values |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| *Refuse file parameter* fails | The `file` value contains `$(`. Give the file's path from the repo root. |
| *Locate tool* or *Find Python* fails | The folder is not committed on the branch the run used (new files need `git add`), or two copies exist. Or no `python3` ≥ 3.9 is on the agent's PATH; the error names the admin fix. |
| `No server URL: the variable group has neither DEV_1_PC nor DEV_1` | The group has no URL for this env, or the pipeline is not allowed to use the group. |
| `The URL in DEV_1 does not end in /bc ...` | `DEV_1` points at another product's app. Add `DEV_1_BC` with the BillingCenter URL; it takes precedence for product `bc`. Set `GW_ALLOW_CONTEXT_MISMATCH=true` only if the app really runs under another context. |
| `The URL in ... is not an https URL with a host` (value not shown) | The variable does not hold a URL, or the env name matches another variable. The value is not printed because it might be a secret. |
| `Environment 'X' is reserved` | The env name is a credential variable (`USERNAME`, `PASSWORD`, `*_USERNAME`, `*_PASSWORD`), a pipeline step key (`ADL_*`), a setting (`GW_TIMEOUT`, `PROD_ENV_PATTERN`, ...) or `PYTHON_EXE`. Use the environment's own name. |
| `DEV_1_USERNAME is set but DEV_1_PASSWORD is not` | Set both of the environment's own login variables, or remove the one you set to use the shared `username`/`password`. |
| `check` fails at **TCP**: no answer, or connection refused | The agent cannot reach the server's port. The set-up steps pass, because only `check` contacts the server. A firewall drops the traffic, the port is wrong, or the app server is down. Ask for the agent to be allowed to reach the host's HTTPS port. |
| `check` fails at **TLS**: *not trusted on this agent* | The server's certificate comes from an internal CA. On Linux, Python uses the OpenSSL bundle, not the Windows store. Set `GW_CA_BUNDLE` to the CA's `.pem` on the agent, or have the CA added to the agent's OS trust store. `GW_VERIFY_TLS=false` works, but only for testing. A DER `.cer` file converts with `openssl x509 -inform der -in ca.cer -out ca.pem`. |
| `No ImportToolsAPI WSDL at ...: ... HTTP 404` | The URL's context is wrong for this server, or the server does not publish ImportToolsAPI (web services not exposed, or blocked by a proxy). Open `<URL>/ws/gw/wsi/pl/ImportToolsAPI?wsdl` in a browser from a machine that can reach the server. |
| Warning: `The WSDL imports .../soapheaders.xsd, which could not be read` | A proxy or firewall blocks `.xsd` paths. The tool continues with Guidewire's default authentication header; it fails only if the request element itself is missing. |
| WSDL needs credentials | Some servers protect the WSDL. `check` then retries with the credentials and says so. If it still gets 401, the user or password is wrong. |
| `check`: login *NOT verified* | `SystemToolsAPI` could not be reached or planned (404, a fault that is not about login, ...). `check` still passes, but the credentials have not been tried. |
| `check`: `SystemToolsAPI.getVersion refused the login` | Wrong user name or password, inactive user, or missing permission. See [What the Guidewire user needs](#what-the-guidewire-user-needs). |
| HTTP 401 / 403, or a fault with `detail: WsiAuthenticationException` | Same as above: credentials or the `soapadmin` permission. Also try `GW_AUTH=basic` or `GW_AUTH=header` if the server accepts only one style. |
| `Bad username or password` (`WsiAuthenticationException`) | The sign-in method worked; the values didn't. Log in to the application's web page with the same `username` and password: if that fails, the password in `gw-admin-data` is wrong for this server. Re-type it rather than paste it (the tool warns about a leading or trailing space). If the web login works but this doesn't, the server checks web-service logins elsewhere (a custom authentication plugin) or the user may not call web services: ask the Guidewire admins which user to use, and give it `soapadmin`. |
| `Multiple authentication methods provided: [HTTP Basic Authentication, Guidewire SOAP Header Authentication]` | `GW_AUTH` is `both`: Guidewire accepts one method per request. Remove `GW_AUTH` (the default picks one) or set it to `header`. |
| SOAP Fault | The server rejected the call or the data. `faultstring` (up to 2,000 characters) and the names of the `detail` elements (e.g. `DataConversionException`) are in the log. Faults about unknown typecodes or missing references mean the file does not match this environment's configuration. |
| `Import reported: Ok = false` / `ErrorLog = ...` | The server answered, but reported errors. Every `ErrorLog` entry is printed above. Part of the file may have been applied, so check in Guidewire before re-running. |
| `... HTTP 200, so the import has probably been applied` | The reply could not be read to the end (too large, cut off or timed out). The server received the file and answered, so check the data in Guidewire before running again. |
| Warning: `over Guidewire's default MaximumFileUploadSize` | The request is over 20 MB. If the server rejects it, split the file, or ask the Guidewire admins for the configured `MaximumFileUploadSize`. |
| `Import will refuse this file ... Re-save it as UTF-8` | The file is UTF-16/32, or declares another encoding and contains non-ASCII characters. Re-save it as UTF-8 (and fix its XML declaration). |
| `No reply within GW_TIMEOUT` | The server may still be importing. Check its log before running again, or raise `GW_TIMEOUT` (the step stops at 30 minutes). |
| `Cannot tell which parameter ...` / `No import operation` | This server's ImportToolsAPI differs from what the tool expects. `check` lists the operations and parameters; set `GW_IMPORT_OPERATION` if one of them is the XML import. |

## Limits

- **One file per run.** Split a large set into several files and runs, in the
  order they depend on each other (see *Dependency order* above).
- **Large files.** The file is streamed, never held in memory whole, and the
  reply is parsed as it arrives, with `Details` entries counted rather than
  kept. The server still parses the file in one request and answers only when
  it has finished, so very large files are slow and can hit `GW_TIMEOUT`, the
  30-minute step limit, or the server's `MaximumFileUploadSize`. Prefer files
  of a few MB.
- **No rollback.** Re-importing the same file is normally harmless. Importing
  an older file reverts those records to the older values. Keep the files in
  source control, so the history shows what was loaded.
- **SOAP 1.1, document/literal only.** This is how Guidewire publishes its
  WS-I web services. An rpc-style or SOAP-1.2-only WSDL is reported by
  `check` and not used.
- **Not yet tested on a live Guidewire server.** The tool has been tested
  against a simulated ImportToolsAPI and SystemToolsAPI built from the real
  WSDLs only. Run `check`, then `validate`, then an `import` of a small file
  in a lower environment first.

## How the call is built

For maintainers. `check` prints each of these values.

1. GET `<URL>/ws/gw/wsi/pl/ImportToolsAPI?wsdl`. On a 404, try
   `<URL>/ws/gw/webservice/pl/ImportToolsAPI?wsdl`, where older releases
   publish it. `wsdl:import` and `xsd:import`/`include` documents are fetched
   relative to the document that names them, always from the configured
   server. One that cannot be read is a warning, not an error.
2. Take the first SOAP 1.1 port. Its binding's portType gives the operation
   list. Choose the import operation, either `GW_IMPORT_OPERATION` or the first
   of `importXmlData`, `importXml`, `importData`.
3. Get the request element from the operation's input message. In the schema,
   find its single string parameter (`xmlData`), with namespace qualification
   from `elementFormDefault` or `form`. If another required parameter exists,
   stop.
4. Get the SOAPAction from the binding operation (empty if none). Get the
   authentication header from the binding's `soap:header`, or use Guidewire's
   `{http://guidewire.com/ws/soapheaders}authentication` when none is
   declared or its schema could not be read.
5. The endpoint is the SOAP 1.1 port's address from `/ws/` onward, appended
   to the configured URL. With no usable address it is the service URL plus
   `/soap11`: Guidewire serves SOAP 1.2 at `.../ImportToolsAPI` and SOAP 1.1
   at `.../ImportToolsAPI/soap11`.
6. With credentials set, `check` plans `SystemToolsAPI.getVersion` from
   `<URL>/ws/gw/wsi/pl/SystemToolsAPI?wsdl` the same way and calls it. Only an
   authentication or permission refusal fails `check`.
7. `import` POSTs a SOAP 1.1 envelope to the endpoint and reads the
   `ImportResults` reply: `Ok=false` or any `ErrorLog` entry is a failure, and
   fields elsewhere named like *error* or *failure* that have content are
   reported too.
