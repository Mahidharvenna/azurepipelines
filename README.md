# Misc pipelines

Small, self-contained Azure DevOps pipelines for day-to-day operations work,
one folder per tool. Each folder has its own README covering setup and use.

| Tool | What it does |
|---|---|
| [`disk-cleanup/`](disk-cleanup/) | Reports where the disk space went on Linux servers, over their existing SSH service connections, and on request removes only old rotated logs and JVM crash logs, WAR backups, heap dumps and core files. Report-only by default. |

This branch is independent of the others in the repo. It shares no files,
templates or variables with them, and each pipeline here is registered from
its own YAML file.

## License

MIT — see [LICENSE](LICENSE).
