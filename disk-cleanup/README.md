# Disk cleanup for Linux servers

Answers "why is `/opt` at 98%?" on the Guidewire app servers, and, when asked,
reclaims the part of the space that is safe to reclaim.

The pipeline runs [`disk_cleanup.sh`](disk_cleanup.sh) on each server you
list, through that server's existing **SSH service connection** (the same
connections the deploy pipelines use), as that connection's user. The default
is **report only**: it shows where the space went and what a cleanup *would*
remove. Nothing on the server changes until you run it again with
`mode: clean`.

```
disk-cleanup/
  disk-cleanup.yaml   the pipeline (one SSH@0 step per server)
  disk_cleanup.sh     runs on the server; copied over by SSH@0, removed afterwards
  .gitattributes      keeps the script LF so bash on the server can run it
```

## Quick start

1. **Register the pipeline, once.** Pipelines → New pipeline → this repo →
   *Existing Azure Pipelines YAML file* → choose the **branch that holds this
   folder** → `disk-cleanup/disk-cleanup.yaml` → Save. Then rename it
   (⋯ → Rename/move) to **Disk-Cleanup**.

   Manual runs use the pipeline's *default branch*, which starts as the
   repository's default branch, where this file may not exist. Set it to the
   branch that holds this folder: Edit → ⋯ → Triggers → YAML → Get sources →
   *Default branch for manual and scheduled builds*. Otherwise pick that branch
   in the Run dialog every time.
2. **Report on one server.** Run pipeline → under *Servers* replace `[]` with
   the service connection name:
   ```yaml
   - DEV2-PC
   ```
   Leave *Mode* on `report`. The first run against a connection stops before
   anything starts with *"This pipeline needs permission to access a
   resource"*: the whole run waits until someone chooses **Permit** for each
   new connection. That happens once per connection.
3. **Read the report**: the step *Disk report: DEV2-PC*. Check section 2 for
   the full filesystem (`!!`), sections 3–4 for where the space is, section 5
   for deleted files still held open on the roots' filesystems, and section 6
   for what each rule would remove (the 20 largest per rule, with a count of
   the rest) and what it would leave alone and why (the first 10, with a count
   of the rest).
4. **Clean.** Run again with the same servers and *Mode* `clean`. Section 6
   then lists every file removed, what was left alone and why, `df` afterwards
   and how much was freed. Several servers can go in one run, one line each:
   ```yaml
   - DEV2-PC
   - DEV4-CM
   ```

Always report before cleaning a server for the first time. The report shows
each rule's totals and largest files; clean mode then logs every file it
removes, so the step log is the record of what went.

## Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `servers` | `[]` | SSH service connection names, one per line as `- NAME`. Required. |
| `mode` | `report` | `report` changes nothing. `clean` removes what the rules match. |
| `roots` | `/opt /var/app/logs` | Absolute directories to scan, space- or comma-separated. Each is scanned on its own filesystem only. |
| `logDays` | `60` | Rotated logs and JVM crash logs older than this many days are removed. |
| `warDays` | `5` | WAR backups made more than this many days ago are removed, except the newest of each component. |
| `dumpDays` | `7` | Heap dumps and core files older than this are removed. |
| `exclude` | `*/ab/*` | Paths matching any of these globs are never cleaned. `none` for no exclusions. See below. |
| `trimCatalinaOutOverMB` | `0` (off) | Clean mode only: trim `catalina.out` files larger than this. See below. |
| `useSudo` | `false` | Run the file commands through `sudo -n`. Needs passwordless sudo. |

Ages are whole days: 1–3650 in clean mode, 0 allowed in report mode. "Older
than N days" is `find -mtime +N`, meaning last modified more than N whole days
ago. WAR backups are the exception: `-ctime +N`, explained under Rules.

`roots` may contain only letters, digits, `/ . _ - + @`, spaces and commas;
`exclude` only letters, digits, `/ . _ - * ? [ ]`, spaces and commas. Both
are handed to the server's shell, so anything else is refused before a server
is contacted.

## Rules

Each rule is one `find <root> -xdev -type f` per root, with the name patterns
and age below. Every match then passes a guard (next section) and the
`exclude` globs before it can be removed.

| Rule | Matches | Default age | Why it is safe to remove |
|---|---|---|---|
| `logs` | `*.log.*` (e.g. `x.log.1`, `x.log.2026-09-01`, `x.log.1.gz`), `*.log.gz`, `catalina.20??-??-??*.log`, `localhost.20??-??-??*.log`, `manager.20??-??-??*.log`, `host-manager.20??-??-??*.log`, `localhost_access_log*.20??-??-??*.txt`, `catalina.out.*`, and the JVM crash logs `hs_err_pid*.log`, `javacore*.txt` | 60 days since last modified | Rotated or dated logs are closed at rollover and never written again. Crash logs are written once, by a JVM that has since died; they are tiny, so they are kept as long as the logs, not the dumps. The default age and the `ab` exclusion are the same as the deploy pipeline's own log cleanup, which covers only `*.log.*` under `/var/app/logs/<env>/<comp>/gw`; this rule covers more names and every directory under the roots. `catalina.out.*` includes the copies this script's trim leaves behind. |
| `wars` | `*.war_*`, `*.war.[0-9]*`, only directly inside a directory named `webapps` | 5 days since the backup was made; the newest of each component is always kept | Backups the deploy makes (`<comp>.war_YYYYMMDD@HHMM`) before copying a new WAR. It makes them with `mv`, which keeps the modification time the WAR got when the previous deploy copied it to the server (possibly weeks earlier) but sets the inode change time (ctime) to the moment of the backup, so the age is taken from ctime. The newest backup (by ctime) of each component (the part of the name before `.war_` or `.war.<n>`) in each `webapps` directory is kept whatever its age, so a rollback copy always remains; once it is old enough to have matched, it is listed as *newest backup of `<comp>`, kept for rollback*. Tomcat runs the live `<comp>.war` and its extracted directory, and neither is ever matched. |
| `dumps` | `*.hprof`, `java_pid*.hprof`, `core`, `core.[0-9]*`, `heapdump*.phd` | 7 days since last modified | Written once, by a JVM that crashed or was asked for a dump. A heap dump can be tens of GB (the JVMs run with up to ~40 GB heaps). |

**Dumps and crash logs may be evidence.** A heap dump, core file or crash log
may be needed for an open incident or a vendor support case. Before a clean,
copy them off the server, or raise `dumpDays` (and `logDays` for the crash
logs).

Anything that changes a WAR backup's owner, mode or links (`chmod`, `chown`)
also resets its ctime. It then looks newer and is kept longer. Changing a
single older backup that way also makes it the one kept as the component's
newest, so the most recent backup can then be removed; changing all of them
(e.g. `chown -R`) only delays removal.

**`exclude`** (default `*/ab/*`): paths matching any of these shell globs are
never cleaned, by any rule or by the `catalina.out` trim. Each glob is matched
against the whole resolved path (as shown under *Roots* in the report, so
write globs for where a symlinked root really points) and `*` also matches
`/`, so `*/ab/*` means "any path
with a directory named `ab` in it". That keeps the ab component's logs
(`/var/app/logs/<env>/ab/gw/...`), which the deploy pipeline deliberately
never deletes, and also anything else under a directory named `ab`. Excluded
files are listed as left alone with the reason `excluded`. To change it, set
`exclude` in the Run dialog: `none` cleans ab like every other component, and
more globs, space- or comma-separated, keep more (e.g. `*/ab/* */bc/gw/*`).

`trimCatalinaOutOverMB` (clean mode, above 0): every regular file named
exactly `catalina.out` under the roots, larger than N MB and not excluded, has
its last 20 MB copied to `catalina.out.<YYYYmmddHHMMSS>` beside it, with the
original's mode (and its owner, with `useSudo`). The original is then emptied **in place** with
`truncate -s 0`: same file, same inode. Tomcat keeps it open and keeps
appending, so a `mv` or `rm` would leave Tomcat writing to a deleted file that
still fills the disk. **No copy, no trim:** if the filesystem lacks room for
the copy plus 32 MB of headroom (52 MB for a full 20 MB copy), or the copy
fails for any other reason, the file is left as it was and listed as a
failure (exit 1). The logs rule runs first, so a clean often makes the room;
otherwise free some space by hand and run again. Lines Tomcat writes in the
instant between the copy and the truncate are lost. Report mode lists the
files it would trim.

## Safety guarantees

These hold in both modes, with or without sudo:

- **Report mode changes nothing on the server.** The only writes are the
  script itself and the script's scratch lists in `/dev/shm`, or failing
  that `$TMPDIR`, `/tmp` or `/var/tmp`, which it removes on exit.
- **SSH@0 writes and then deletes `~/disk_cleanup.sh` in the SSH user's
  home.** Keep no file of that name there, and don't run two cleanups against
  the same server at once: one can delete the other's copy.
- **Regular files only.** Directories are never deleted (`rm -f`, never `-r`).
- **Symlinks under a root are never followed** (a root that is itself a
  symlink is resolved and judged by its target). `find` runs without
  `-L`/`-follow`. Before
  each delete the file is re-checked: it must still be a regular file, not a
  symlink, its directory must still resolve to the same place, and it must be
  unchanged since the scan (same modification time, and for WAR backups the
  same ctime). A file rotated into an old name in between is left alone as
  *changed since the scan*.
- **One filesystem per root** (`-xdev`). A filesystem mounted inside a root is
  not scanned as part of it; list its mount point as a root of its own, and it
  is scanned separately and shown in `df`.
- **Files named like active logs are refused, and every other match must also
  be older than its age.** The guard refuses `catalina.out`, anything ending
  `.current` (e.g. the JVM's `gc.log.0.current`), any `*.log` or `*.txt`
  without a date in its name (the crash logs `hs_err_pid*.log` and
  `javacore*.txt` excepted), and lock/state files (`*.lck`, `*.lock`, `*.pid`,
  `*.pos`, `*.swp`). A log that is being written to under a rotation-style
  name the guard does not know (e.g. log4j2 writing straight to a dated file)
  is protected by its age alone: it was modified recently.
- **`*.log.*` must really be a rotation.** What follows `.log.` must start
  with a digit or be `gz`/`bz2`/`xz`/`zip`/`Z`, so `x.log.properties` and
  `x.log.bak` are left alone.
- **WAR backups only directly inside `webapps/`**, never a file ending `.war`,
  and never the newest backup of a component.
- **`core` files must be ELF core dumps.** The header is read (ELF type
  `ET_CORE`), so a file that is merely named `core` survives.
- **OS directories are never roots.** `/` and anything that is, is inside, or
  contains `/bin /boot /dev /etc /lib /lib64 /proc /root /run /sbin /sys /usr
  /var/lib /var/spool` is refused, both as typed (`/opt/../etc` is `/etc`) and
  after resolving symlinks. `/var/log` is allowed. `/var` is refused because it
  contains `/var/lib`.
- **Overlapping roots are scanned once.** A root inside another root on the
  same filesystem is dropped, with a note. One inside another but on its own
  filesystem (a separate mount) is kept as a root of its own, also with a note.
- **Never prompts, never escalates on its own.** sudo is used only with
  `useSudo: true` and always as `sudo -n`. If sudo is not available, clean
  mode stops before touching anything (exit 2) and report mode carries on
  without it.
- **File names cannot steer the pipeline.** Everything printed that comes
  from the server (paths, reasons that name a file, mount points, error
  lines) has `##` and control characters defused, so a crafted file name
  cannot become an ADO logging command.

Every file the rules matched but left alone (guard, `exclude`, newest WAR
backup) is counted under *matched but left alone*, and the first 10 are
listed with the reason.

## What it does not do

- It never restarts, stops or kills a process.
- It never deletes a directory.
- It never removes a file named like an active log, and nothing younger than
  its rule's age. Old *rotated* logs go.
- It never crosses filesystems or follows symlinks.
- It never cleans anything outside the listed roots, or anything matching
  `exclude`. Package caches, `/tmp`, journals and home directories are not
  touched unless you list them, and even then only files matching the rules
  are removed.

## Deleted but still open

Deleting a file removes its name. The space comes back only when the last
process holding the file open closes it. A log deleted while Tomcat still
writes to it keeps growing, invisible to `du` and `ls` but counted by `df`.
Section 5 lists these (`lsof +L1`) on the filesystems that hold the roots,
matched by device number, with their size and the process holding them. That
figure is the one in the report-mode SUMMARY line; without `lsof` it says
*not measured*, and a capped `lsof` is marked *partial*. When other filesystems (e.g. `/tmp`,
`/dev/shm`) hold some too, a separate host-wide total follows; those files use
no space on the roots. The only fix is for the process to close the file, which
in practice means restarting the service in a maintenance window. This script
never does that. Without `useSudo`, only the SSH user's own processes are
visible, which normally includes Tomcat.

## Exit codes

| Code | Meaning | Step result |
|---|---|---|
| 0 | Ran. Report mode, clean mode with no failures, or none of the roots exist on the server (`SUMMARY <host>: no roots present`). | green |
| 1 | Clean mode could not remove or trim some files. Each is listed under its rule, with the reason, and counted at the end. | orange (warning), others still run |
| 2 | Bad argument; a forbidden root, or one that is not a directory or can't be resolved; no writable scratch directory; or `useSudo` requested but `sudo -n` unusable in clean mode. Nothing was scanned or changed; the last line is `SUMMARY <host>: not run: <reason>`. | orange (warning), others still run |

**A server's log is complete only if it ends with the `SUMMARY <host>:`
line.** A step without it was cut off (dropped connection, killed process,
timeout) whatever its colour: SSH@0 can show green for a run that never
finished. Rerun it. The SUMMARY line is also handy for comparing servers.

Every server step has `continueOnError: true`, so once the run has started,
one failing or unreachable server never stops the rest. The run then ends
*partially succeeded*. Each server step may run for 60 minutes and the whole
job for 370. The report sections are capped (per root: `du` 3 minutes, the
search for big files 5 minutes; `lsof` 2 minutes), so the rules get most of
each step's hour.

## Troubleshooting

**`Permission denied` in the failures, or "error line(s), usually directories
... cannot read".** The SSH user does not own those files or cannot enter
those directories, often because they were written by root or another service
account. If that account has passwordless sudo, rerun with `useSudo: true`.
Otherwise ask the server owner to clean them or fix the ownership. Without
sudo the report still covers everything the account can read.

**`sudo -n true failed` with `useSudo: true`.** The account has no
passwordless sudo, or sudoers has `Defaults requiretty` (the SSH task has no
tty). Clean mode refuses to continue (exit 2). Report mode continues without
sudo.

**The run waits with "needs permission to access a resource".** First use of
a service connection by this pipeline. The whole run waits, with no server
started, until someone chooses *Permit* for each new connection.

**The run fails at once: "could not be found", "does not exist or has not
been authorized".** A name under *Servers* does not match a service connection
exactly (names are per project), or you lack the User role on it. A mistyped
connection name fails the whole run at queue time, before any server is
touched. Fix the name and queue again.

**The step fails to connect or times out before any output.** The agent could
not reach the server on port 22, or the connection's host/credentials are
wrong. Run from the same agent pool the deploy pipelines use, and check the
service connection. If the SSH user's home filesystem is 100% full, SSH@0
cannot copy the script there. Free a little space by hand first.

**A step's log does not end with `SUMMARY`.** It was cut off. If it stopped at
60 minutes, the rules' searches of a very large tree took too long: list
narrower roots and run again.

**No server steps at all, or a compile error about `servers`.** *Servers* must
be a YAML list, one `- NAME` per line. A bare name without `- ` is not a list.

**A directory is missing from the report.** If it is a separate mount inside a
root, `-xdev` stops there: add its mount point to `roots`. It is then scanned
as a root of its own and shown in `df`.

**`/opt` is still full after clean.** Look at section 5. A large deleted file
held open by a running JVM keeps its space until the JVM restarts. Also look
at sections 3–4 for what is actually big, and at what section 6 left alone.
It may be something the rules deliberately never touch (an install, a data
directory, an active log, an excluded path, the newest WAR backups). Decide
on those by hand.

**A big `.hprof` was not removed.** A heap dump being written right now, or
written in the last `dumpDays` days, is too recent for the rule. Wait, or copy
it off and remove it by hand once it is no longer needed.

**`catalina.out` was not trimmed.** Trimming is off unless
`trimCatalinaOutOverMB` is above 0 and mode is `clean`, and only files larger
than that size and not matching `exclude` are trimmed. A trim also fails
(listed, exit 1, file left as it was) when the file is not writable by the
SSH user, when the filesystem has no room for the 20 MB copy plus 32 MB of
headroom, or when the dated copy could not be written.

**Line-ending or syntax errors in "Locate cleanup script".** The script was
committed with CRLF endings or has a bash syntax error. `.gitattributes` keeps
it LF. Re-commit it with LF.
