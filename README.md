# Guidewire admin data load

An Azure DevOps pipeline that loads Guidewire administrative data (users,
groups, roles, activity patterns, ...), one XML file from the repo per run,
into an environment's Guidewire application. It uses the server's
ImportToolsAPI web service, the API behind `import_tools`. Guidewire does not
support business data through this API, and an import overwrites existing
records with the same public ID without asking.

| Tool | What it does |
|---|---|
| [`admin-data-load/`](admin-data-load/) | Pick an admin-data XML file in the repo, an environment (e.g. DEV_1) and a product (pc, bc, cc, cm). The pipeline loads that file through ImportToolsAPI, with everything about the call read from the server's WSDL. `check` and `validate` change nothing (`check` logs in read-only when credentials are set); `import` sends the file. A production environment needs the env name typed again to confirm. |

Start with [`admin-data-load/README.md`](admin-data-load/README.md): setup,
variables, what Guidewire does with the file, safety, protecting production
and troubleshooting.

This branch is independent of the others in the repo. It shares no files,
templates or variables with them. Its pipeline is registered from its own YAML
file and reads its own variable group, `gw-admin-data`.

## License

MIT — see [LICENSE](LICENSE).
