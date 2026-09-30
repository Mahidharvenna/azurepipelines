#!/bin/bash
# disk_cleanup.sh -- show where the disk space went on a Linux app server and,
# when asked, reclaim the part that is safe to reclaim.
#
# Runs ON the server: the Disk-Cleanup pipeline copies it over each SSH service
# connection (SSH@0, runOptions: script) and runs it as that connection's user.
# It can also be run by hand; --help lists the options.
#
#   --mode report  (default) df, biggest directories and files, deleted files
#                  still held open, and what each rule WOULD remove. Changes
#                  nothing.
#   --mode clean   the same, then removes the files each rule matched, listing
#                  every one, and trims catalina.out if --trim-catalina-mb is
#                  above 0.
#
# Rules -- regular files older than the rule's age, never directories:
#   logs   rotated/archived logs     *.log.<n|date>[.gz], dated Tomcat logs, catalina.out.*
#          and JVM crash logs        hs_err_pid*.log, javacore*.txt
#   wars   WAR backups in webapps/   *.war_*, *.war.<n>, aged from when the backup
#                                    was made; the newest of each component is kept
#   dumps  JVM heap dumps and cores  *.hprof, core[.<n>], heapdump*.phd
# Paths matching an --exclude glob (default */ab/*) are never cleaned.
#
# Never: follows a symlink under a root (a symlinked root is resolved and
# judged by its target), crosses into another filesystem, removes a file
# named like an active log, scans an OS directory, kills or restarts a
# process, prompts for a password.
#
# Exit: 0 ran (report, or clean with no failures)
#       1 clean mode could not remove or trim some files (listed in the output)
#       2 bad arguments, a forbidden, non-directory or unresolvable root, no
#         writable scratch directory, or --sudo true unusable in clean mode
# Every run that gets past --help ends with a "SUMMARY <host>:" line; a log
# without one was cut off.
#
# Needs bash 4.2+ (RHEL 7) and GNU findutils/coreutils on the server. Kept
# bash-3.2 compatible (no associative arrays, mapfile or ${v,,}) so the
# argument checks can be exercised on a workstation.

set -u            # no -e: one unreadable directory must not cut the report short
umask 077         # the only files written are catalina.out copies; start them private
export LC_ALL=C   # stable sort order and tool messages
# Non-login SSH sessions get a short PATH; lsof lives in /usr/sbin on RHEL 7.
PATH=$PATH:/usr/sbin:/sbin:/usr/bin:/bin

TAB=$'\t'
NL=$'\n'
MIB=1048576

# Defaults. The pipeline passes every option explicitly.
MODE=report
ROOTS_ARG='/opt /var/app/logs'
LOG_DAYS=60                     # the deploy's own log retention
WAR_DAYS=5
DUMP_DAYS=7
EXCLUDE_ARG='*/ab/*'            # the deploy never deletes the ab component's logs
TRIM_MB=0
USE_SUDO=false

MAX_DAYS=3650
MAX_TRIM_MB=1048576             # 1 TiB: anything larger is a typo
TRIM_KEEP=$((20 * MIB))         # tail of catalina.out kept in the dated copy
TRIM_HEADROOM=$((32 * MIB))     # never be the one who fills the filesystem
BIG_FILE=100M
TOP_DIRS=15 TOP_FILES=20 TOP_OPEN=10 TOP_CAND=20 TOP_SKIP=10 TOP_FAIL=20
# Caps on the report sections, so the rules still run inside the step's time
# limit: two roots take at most 2 x (180 + 300) + 120 s (lsof) = 18 minutes.
DU_SECS=180 BIG_SECS=300

# OS-owned trees: never a root, never inside a root, never contained by one.
FORBIDDEN='/bin /boot /dev /etc /lib /lib64 /proc /root /run /sbin /sys /usr /var/lib /var/spool'

# find -printf record: KiB allocated, bytes, mtime date, path. NUL-terminated so
# any file name survives. KiB allocated because that is what a delete frees; a
# sparse core file's apparent size can be many times larger.
FMT='%k\t%s\t%TY-%Tm-%Td\t%p\0'
# The rules' record adds mtime and ctime in seconds, so each file's age can be
# re-checked just before it is removed. WAR backups show their ctime date.
RFMT_M='%k\t%s\t%TY-%Tm-%Td\t%T@\t%C@\t%p\0'
RFMT_C='%k\t%s\t%CY-%Cm-%Cd\t%T@\t%C@\t%p\0'

SUDO=""                 # "sudo -n" under --sudo true; unquoted on purpose
HAVE_TIMEOUT=""
RAW_ROOTS=() CANONS=() CDEVS=() ROOTS=() ROOT_DEVS=() EXCLUDES=()
FS_MOUNTS=() FS_PATHS=() FS_BEFORE=()
WAR_KEYS=() WAR_MAX=()
SETUP_NOTES=""
WORK=""
STAMP=""
HOST=""
ME=""
# Totals for the closing summary.
CAND_N=0 CAND_KB=0 DONE_N=0 DONE_KB=0 FAIL_N=0 TRIM_N=0 TRIM_BYTES=0
FULL_N=0 OPEN_N=0 OPEN_BYTES=0 ALL_N=0 ALL_BYTES=0 FREED=0
OPEN_STATE=measured   # measured | partial (lsof capped) | none (no lsof)

usage() {
  cat <<'EOF'
Usage: disk_cleanup.sh [options]
  --mode report|clean      report (default) changes nothing
  --roots "DIR DIR"        absolute directories, space- or comma-separated
                           (default: /opt /var/app/logs)
  --log-days N             rotated logs and JVM crash logs older than N days
                                                                    (default 60)
  --war-days N             WAR backups made more than N days ago; the newest
                           of each component is always kept         (default 5)
  --dump-days N            heap dumps and core files older than N days (default 7)
  --exclude "GLOB GLOB"    never clean a path matching one of these shell
                           globs (whole path; space- or comma-separated;
                           letters, digits and / . _ - * ? [ ] only; 'none'
                           for no exclusions)                  (default: */ab/*)
  --trim-catalina-mb N     clean mode: trim catalina.out files over N MB in
                           place, keeping the last 20 MB in a dated copy; not
                           trimmed when that copy cannot be made
                           (default 0 = never)
  --sudo true|false        run file commands through 'sudo -n'      (default false)
Ages are 1-3650 days in clean mode (0 allowed in report mode).
Exit: 0 ok, 1 clean mode had failures, 2 not run (bad arguments, forbidden or
unusable root, no scratch directory, no sudo in clean mode).
Exclude globs match the resolved path shown under "Roots".
The last line is always "SUMMARY <host>: ..."; a log without it was cut off.
EOF
}

# --- output helpers ----------------------------------------------------------

# Everything printed from the server's filesystem goes through here: the agent
# obeys any "##vso[" it finds in the log, so a crafted file name could set
# pipeline variables. Break every "##" and every control character. Sets SHOWN.
show() {
  SHOWN=${1//[[:cntrl:]]/?}
  SHOWN=${SHOWN//\#\#/#_}
}

# The last line of every run that got past --help, whatever the outcome, so a
# log without it is known to have been cut off.
summary_line() {  # text
  local h
  show "${HOST:-unknown}"; h=$SHOWN
  show "$1"
  printf '\nSUMMARY %s: %s\n' "$h" "$SHOWN"
}

die() {
  local why
  show "$*"; why=$SHOWN
  printf 'ERROR: %s\n' "$why" >&2
  summary_line "not run: $why"
  exit 2
}

isnum() {
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
  return 0
}

# Bytes -> "1.5G" (IEC, one decimal). Sets HUMAN; no subshell per call.
human() {
  local n=$1 d=1 u=B t
  isnum "$n" || { HUMAN="?"; return; }
  for u in B K M G T P; do
    [ "$n" -lt $((d * 1024)) ] && break
    [ "$u" = P ] && break
    d=$((d * 1024))
  done
  if [ "$d" -eq 1 ]; then HUMAN="${n}B"; return; fi
  t=$(( (n * 10 + d / 2) / d ))
  HUMAN="$((t / 10)).$((t % 10))$u"
}

# One listed file or directory: size, optional prefix (e.g. a date), path.
item() {  # bytes path [prefix] [suffix]
  local suffix
  human "$1"
  show "${4:-}"; suffix=$SHOWN
  show "$2"
  printf '    %8s  %s%s%s\n' "$HUMAN" "${3:-}" "$SHOWN" "$suffix"
}

section() {
  printf '\n------------------------------------------------------------------------\n'
  printf ' %s\n' "$1"
  printf -- '------------------------------------------------------------------------\n'
}

# find/du complain once per unreadable directory; summarise instead of flooding.
read_errors() {  # stderr-file
  local n line
  n=$(grep -c . "$1" 2>/dev/null)
  isnum "$n" || n=0
  [ "$n" -gt 0 ] || return 0
  if [ -n "$SUDO" ]; then
    printf '    (%d error line(s), even with sudo; figures are a lower bound)\n' "$n"
  else
    printf '    (%d error line(s), usually directories %s cannot read; figures are a\n' "$n" "$ME"
    printf '     lower bound. useSudo=true reads everything if the account has passwordless sudo)\n'
  fi
  head -n 3 "$1" | while IFS= read -r line; do
    show "$line"; printf '      %s\n' "$SHOWN"
  done
}

# timeout(1) when present; a hung NFS mount must not hang the whole run.
tmo() {  # seconds command...
  local s=$1
  shift
  if [ -n "$HAVE_TIMEOUT" ]; then timeout "$s" "$@"; else "$@"; fi
}

# --- arguments -----------------------------------------------------------------

is_option() {
  case "$1" in
    --mode|--roots|--log-days|--war-days|--dump-days|--exclude|--trim-catalina-mb|--sudo) return 0 ;;
  esac
  return 1
}

parse_args() {
  local opt val
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help) usage; exit 0 ;;
      --*=*)     opt=${1%%=*}; val=${1#*=}; shift ;;
      --*)       opt=$1; shift
                 is_option "$opt" || die "unknown option '$opt' (see --help)"
                 [ $# -gt 0 ] || die "$opt needs a value (see --help)"
                 val=$1; shift ;;
      *)         die "unexpected argument '$1' (see --help)" ;;
    esac
    case "$opt" in
      --mode)             MODE=$val ;;
      --roots)            ROOTS_ARG=$val ;;
      --log-days)         LOG_DAYS=$val ;;
      --war-days)         WAR_DAYS=$val ;;
      --dump-days)        DUMP_DAYS=$val ;;
      --exclude)          EXCLUDE_ARG=$val ;;
      --trim-catalina-mb) TRIM_MB=$val ;;
      --sudo)             USE_SUDO=$val ;;
      *)                  die "unknown option '$opt' (see --help)" ;;
    esac
  done
}

# Whole numbers only: no sign, no decimals. 10# keeps "08" from reading as octal.
int_opt() {  # option value min max -> INT
  case "$2" in
    ''|*[!0-9]*) die "$1 must be a whole number from $3 to $4, got '$2'" ;;
  esac
  [ ${#2} -le 9 ] || die "$1 must be from $3 to $4, got '$2'"
  INT=$((10#$2))
  [ "$INT" -ge "$3" ] || die "$1 must be at least $3 in $MODE mode, got '$2'"
  [ "$INT" -le "$4" ] || die "$1 must be from $3 to $4, got '$2'"
}

validate_args() {
  local min=0 i
  case "$MODE" in
    report|clean) ;;
    *) die "--mode must be report or clean, got '$MODE'" ;;
  esac
  # Clean mode needs a real age: 0 would make every match eligible at once.
  [ "$MODE" = clean ] && min=1
  int_opt --log-days  "$LOG_DAYS"  "$min" "$MAX_DAYS"; LOG_DAYS=$INT
  int_opt --war-days  "$WAR_DAYS"  "$min" "$MAX_DAYS"; WAR_DAYS=$INT
  int_opt --dump-days "$DUMP_DAYS" "$min" "$MAX_DAYS"; DUMP_DAYS=$INT
  int_opt --trim-catalina-mb "$TRIM_MB" 0 "$MAX_TRIM_MB"; TRIM_MB=$INT
  # ADO renders a boolean parameter as True/False.
  case "$USE_SUDO" in
    true|True|TRUE)    USE_SUDO=true ;;
    false|False|FALSE) USE_SUDO=false ;;
    *) die "--sudo must be true or false, got '$USE_SUDO'" ;;
  esac
  # Globs are only ever matched with case, never expanded to file names. The
  # character set keeps them plain patterns; 'none' is there because a
  # pipeline parameter cannot be left empty.
  set -f
  # shellcheck disable=SC2206  # split on purpose; set -f stops globbing
  EXCLUDES=( ${EXCLUDE_ARG//,/ } )
  set +f
  if [ ${#EXCLUDES[@]} -eq 1 ] && [ "${EXCLUDES[0]}" = none ]; then
    EXCLUDES=()
  fi
  for ((i = 0; i < ${#EXCLUDES[@]}; i++)); do
    case "${EXCLUDES[i]}" in
      *[!]A-Za-z0-9/._*?[-]*)
        die "--exclude '${EXCLUDES[i]}' may contain only letters, digits and / . _ - * ? [ ]" ;;
    esac
  done
}

# --- roots -----------------------------------------------------------------------

under() {  # true when $1 is $2 or below it
  [ "$1" = "$2" ] && return 0
  case "$1" in "$2"/*) return 0 ;; esac
  return 1
}

# Collapse //, /./ and /../ without touching the filesystem. Sets NORM.
# Only called on paths already limited to [A-Za-z0-9._/+@-], so no globbing.
norm_path() {
  local part out="" IFS=/
  for part in $1; do
    case "$part" in
      ''|.) ;;
      ..)   out=${out%/*} ;;
      *)    out=$out/$part ;;
    esac
  done
  NORM=${out:-/}
}

check_forbidden() {  # path label
  local f
  [ "$1" = / ] && die "root '$2' is /: the whole machine is never scanned"
  for f in $FORBIDDEN; do
    under "$1" "$f" && die "root '$2' is $f or inside it; OS directories are never scanned"
    under "$f" "$1" && die "root '$2' contains $f, which is never scanned; list the directories you want instead (e.g. /var/log)"
  done
  return 0
}

# Syntax and the forbidden list, before anything touches the filesystem.
check_roots_syntax() {
  local raw
  # Split on spaces/commas without letting a stray * expand to file names.
  set -f
  # shellcheck disable=SC2206  # split on purpose; set -f stops globbing
  RAW_ROOTS=( ${ROOTS_ARG//,/ } )
  set +f
  [ ${#RAW_ROOTS[@]} -gt 0 ] || die "--roots is empty"
  for raw in "${RAW_ROOTS[@]}"; do
    case "$raw" in
      /*) ;;
      *)  die "root '$raw' is not an absolute path" ;;
    esac
    case "$raw" in
      *[!A-Za-z0-9._/+@-]*) die "root '$raw' may contain only letters, digits and / . _ - + @" ;;
    esac
    # As spelled first ("/opt/../etc" is /etc), so a forbidden directory is
    # refused even where symlinks would make it resolve somewhere else.
    norm_path "$raw"
    check_forbidden "$NORM" "$raw"
  done
}

setup_sudo() {
  local why
  [ "$USE_SUDO" = true ] || return 0
  # -n: fail instead of prompting; nobody can answer a prompt in a pipeline.
  if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    SUDO="sudo -n"
    return 0
  fi
  why="--sudo true, but 'sudo -n true' failed for $(id -un 2>/dev/null): sudo missing, no passwordless sudo, or sudoers requires a tty"
  [ "$MODE" = clean ] && die "$why. Refusing to clean without it."
  SETUP_NOTES+="$why; reporting without sudo, so some paths may be unreadable$NL"
}

# Existence, canonical form, overlap. Missing roots are a note, not an error.
resolve_roots() {
  local raw canon i j c o dup other
  for raw in "${RAW_ROOTS[@]}"; do
    if ! $SUDO test -e "$raw"; then
      SETUP_NOTES+="root $raw does not exist here; skipped$NL"
      continue
    fi
    $SUDO test -d "$raw" || die "root '$raw' exists but is not a directory"
    canon=$($SUDO readlink -f -- "$raw" 2>/dev/null) || canon=""
    [ -n "$canon" ] || canon=$($SUDO realpath -- "$raw" 2>/dev/null) || canon=""
    [ -n "$canon" ] || die "cannot resolve root '$raw' (readlink -f and realpath failed)"
    # A symlinked root is judged by where it really points.
    if [ "$canon" = "$raw" ]; then
      check_forbidden "$canon" "$raw"
    else
      check_forbidden "$canon" "$raw (really $canon)"
    fi
    CANONS+=("$canon")
    CDEVS+=("$($SUDO stat -c %d -- "$canon" 2>/dev/null)")
  done
  # A root inside another on the same filesystem would be scanned, and
  # cleaned, twice. One on another filesystem is a mount that -xdev stops at,
  # so it stays a root of its own.
  for ((i = 0; i < ${#CANONS[@]}; i++)); do
    c=${CANONS[i]}
    dup="" other=""
    for ((j = 0; j < ${#CANONS[@]}; j++)); do
      [ "$i" -eq "$j" ] && continue
      o=${CANONS[j]}
      if [ "$c" = "$o" ]; then
        [ "$j" -lt "$i" ] && dup=$o
      elif under "$c" "$o"; then
        # Unknown device numbers: assume one filesystem, as before.
        if [ -n "${CDEVS[i]}" ] && [ -n "${CDEVS[j]}" ] && [ "${CDEVS[i]}" != "${CDEVS[j]}" ]; then
          other=$o
        else
          dup=$o
        fi
      fi
    done
    if [ -z "$dup" ]; then
      ROOTS+=("$c")
      ROOT_DEVS+=("${CDEVS[i]}")
      [ -n "$other" ] && SETUP_NOTES+="root $c is inside $other but on another filesystem; scanned as a root of its own$NL"
    elif [ "$c" = "$dup" ]; then
      SETUP_NOTES+="root $c is listed twice; scanned once$NL"
    else
      SETUP_NOTES+="root $c is inside $dup; scanned as part of it$NL"
    fi
  done
}

# /dev/shm first: RAM-backed, so it still works when the full disk is the one
# holding /tmp. Removed on exit.
make_workdir() {
  local d
  for d in /dev/shm "${TMPDIR:-/tmp}" /tmp /var/tmp; do
    [ -d "$d" ] && [ -w "$d" ] || continue
    WORK=$(mktemp -d "$d/disk_cleanup.XXXXXX" 2>/dev/null) && break
    WORK=""
  done
  [ -n "$WORK" ] || die "cannot create a scratch directory in /dev/shm, /tmp or /var/tmp"
  trap cleanup_work EXIT
  # A dropped SSH session or a pipeline timeout still removes the scratch files.
  trap 'exit 129' HUP
  trap 'exit 141' PIPE
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

# Only our own flat scratch directory: files, then the empty directory.
cleanup_work() {
  [ -n "$WORK" ] && [ -d "$WORK" ] || return 0
  rm -f -- "$WORK"/* 2>/dev/null
  rmdir -- "$WORK" 2>/dev/null
}

# --- filesystems -------------------------------------------------------------------

# Free KiB on the filesystem holding $1. Sets AVAIL (0 when df fails).
avail_kb() {
  AVAIL=$(tmo 60 $SUDO df -P -k -- "$1" 2>/dev/null | awk 'NR == 2 { print $4 }')
  isnum "$AVAIL" || AVAIL=0
}

# One entry per filesystem, with one root on it to ask df about.
build_fs_list() {
  local root mnt i dup
  for root in "${ROOTS[@]}"; do
    mnt=$(tmo 60 $SUDO df -P -k -- "$root" 2>/dev/null |
          awk 'NR == 2 { m = $6; for (i = 7; i <= NF; i++) m = m " " $i; print m }')
    if [ -z "$mnt" ]; then
      SETUP_NOTES+="df could not read the filesystem holding $root$NL"
      continue
    fi
    dup=""
    for ((i = 0; i < ${#FS_MOUNTS[@]}; i++)); do
      [ "${FS_MOUNTS[i]}" = "$mnt" ] && dup=1
    done
    [ -n "$dup" ] && continue
    FS_MOUNTS+=("$mnt")
    FS_PATHS+=("$root")
  done
}

print_df() {
  local line
  if [ ${#FS_PATHS[@]} -eq 0 ]; then
    echo "  df could not read any root's filesystem"
    return 0
  fi
  tmo 60 $SUDO df -hP -- "${FS_PATHS[@]}" > "$WORK/df" 2>/dev/null
  # Mount points are paths too.
  awk 'NR == 1 { print "  " $0; next }
       { p = $5; sub(/%/, "", p); print "  " $0 ((p + 0 >= 90) ? "   !!" : "") }' "$WORK/df" |
    while IFS= read -r line; do
      show "$line"; printf '%s\n' "$SHOWN"
    done
  FULL_N=$(awk 'NR > 1 { p = $5; sub(/%/, "", p); if (p + 0 >= 90) n++ } END { print n + 0 }' "$WORK/df")
}

# --- report sections -------------------------------------------------------------

report_host() {
  local line
  section "1. Host summary"
  show "$HOST"; printf '  Host      : %s\n' "$SHOWN"
  printf '  User      : %s%s\n' "$ME" "${SUDO:+ (file commands through sudo -n)}"
  printf '  Date      : %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
  show "$(cat /etc/redhat-release 2>/dev/null || uname -sr)"; printf '  OS        : %s\n' "$SHOWN"
  if [ "$MODE" = clean ]; then
    printf '  Mode      : clean -- files matching the rules below are removed\n'
  else
    printf '  Mode      : report -- nothing is changed\n'
  fi
  if [ ${#ROOTS[@]} -gt 0 ]; then
    show "${ROOTS[*]}"; printf '  Roots     : %s  (one filesystem each, symlinks not followed)\n' "$SHOWN"
  else
    printf '  Roots     : none of the requested roots exist here\n'
  fi
  printf '  Rules     : logs and JVM crash logs > %d days; WAR backups made > %d days ago,\n' \
    "$LOG_DAYS" "$WAR_DAYS"
  printf '              the newest of each component kept; heap dumps and cores > %d days\n' \
    "$DUMP_DAYS"
  if [ ${#EXCLUDES[@]} -gt 0 ]; then
    show "${EXCLUDES[*]}"; printf '  Exclude   : %s  (never cleaned)\n' "$SHOWN"
  else
    printf '  Exclude   : none\n'
  fi
  if [ "$TRIM_MB" -gt 0 ]; then
    printf '  catalina  : trim catalina.out over %d MB, keeping the last 20 MB (no copy, no trim)\n' "$TRIM_MB"
  else
    printf '  catalina  : never trimmed\n'
  fi
  printf '%s' "$SETUP_NOTES" | while IFS= read -r line; do
    show "$line"; printf '  NOTE      : %s\n' "$SHOWN"
  done
}

report_dirs() {
  local root rc kb path
  section "3. Biggest directories (du -x, 3 levels deep, top $TOP_DIRS per root)"
  for root in "${ROOTS[@]}"; do
    show "$root"; printf '\n  %s\n' "$SHOWN"
    tmo "$DU_SECS" $SUDO du -x -k --max-depth=3 -0 -- "$root" > "$WORK/du" 2> "$WORK/du.err"
    rc=$?
    tr '\n\0' '?\n' < "$WORK/du" | sort -t "$TAB" -k1,1nr | head -n "$TOP_DIRS" |
      while IFS="$TAB" read -r kb path; do
        isnum "$kb" && item $((kb * 1024)) "$path"
      done
    [ "$rc" -eq 124 ] && echo "    (du stopped after ${DU_SECS}s: partial figures)"
    read_errors "$WORK/du.err"
  done
}

report_files() {
  local root rc kb bytes day path extra
  section "4. Biggest files over $BIG_FILE (top $TOP_FILES per root, same filesystem only)"
  for root in "${ROOTS[@]}"; do
    show "$root"; printf '\n  %s\n' "$SHOWN"
    tmo "$BIG_SECS" $SUDO find "$root" -xdev -type f -size +"$BIG_FILE" -printf "$FMT" \
      > "$WORK/big" 2> "$WORK/big.err"
    rc=$?
    [ -s "$WORK/big" ] || echo "    none"
    tr '\n\0' '?\n' < "$WORK/big" | sort -t "$TAB" -k1,1nr | head -n "$TOP_FILES" |
      while IFS="$TAB" read -r kb bytes day path; do
        isnum "$kb" && isnum "$bytes" || continue
        extra=""
        if [ "$bytes" -gt $((kb * 2048)) ]; then
          human "$bytes"; extra="  (sparse; apparent size $HUMAN)"
        fi
        item $((kb * 1024)) "$path" "$day  " "$extra"
      done
    [ "$rc" -eq 124 ] && echo "    (find stopped after ${BIG_SECS}s: partial list)"
    read_errors "$WORK/big.err"
  done
}

report_open() {
  local s pid cmd name on i devs=""
  section "5. Deleted files still held open on the roots' filesystems (lsof +L1)"
  if ! command -v lsof >/dev/null 2>&1; then
    echo "  lsof is not installed; skipped. By hand: ls -l /proc/*/fd 2>/dev/null | grep '(deleted)'"
    OPEN_STATE=none
    return 0
  fi
  # lsof reports a file's device (st_dev) in hex; stat gave the roots' in
  # decimal. Compared as bare lower-case hex.
  for ((i = 0; i < ${#ROOT_DEVS[@]}; i++)); do
    isnum "${ROOT_DEVS[i]}" && devs+=" $(printf '%x' "${ROOT_DEVS[i]}")"
  done
  # -F fields: p pid, c command, t type, D device, s size, i inode, n name.
  # One line per open file (device+inode), however many descriptors hold it,
  # first field 1 when it is on a root's filesystem. Host-wide, lsof also
  # sees /tmp, /dev/shm and memory-only files that no cleanup here can touch.
  tmo 120 $SUDO lsof -nP -w +L1 -F pctDsin 2>/dev/null | awk -v OFS='\t' -v devs="$devs" '
    BEGIN { n = split(devs, a, " "); for (k = 1; k <= n; k++) root[a[k]] = 1 }
    /^p/ { pid = substr($0, 2) }
    /^c/ { cmd = substr($0, 2) }
    /^f/ { t = ""; s = ""; d = ""; i = "" }
    /^t/ { t = substr($0, 2) }
    /^D/ { d = tolower(substr($0, 2)); sub(/^0x/, "", d); sub(/^0+/, "", d); if (d == "") d = "0" }
    /^s/ { s = substr($0, 2) }
    /^i/ { i = substr($0, 2) }
    /^n/ { if (t == "REG" && s != "" && !((d, i) in seen)) {
             seen[d, i] = 1; print ((d in root) ? 1 : 0), s, pid, cmd, substr($0, 2) } }
  ' > "$WORK/open"
  # A capped lsof lists only part of the files: say so rather than
  # present the sum as the whole.
  [ "${PIPESTATUS[0]}" = 124 ] && OPEN_STATE=partial
  while IFS="$TAB" read -r on s pid cmd name; do
    isnum "$s" || continue
    ALL_N=$((ALL_N + 1))
    ALL_BYTES=$((ALL_BYTES + s))
    [ "$on" = 1 ] || continue
    OPEN_N=$((OPEN_N + 1))
    OPEN_BYTES=$((OPEN_BYTES + s))
    printf '%s\t%s\t%s\t%s\n' "$s" "$pid" "$cmd" "$name" >&3
  done < "$WORK/open" 3> "$WORK/open.roots"
  human "$OPEN_BYTES"
  printf "  %d deleted file(s) still open on the roots' filesystems, holding %s\n" "$OPEN_N" "$HUMAN"
  [ "$OPEN_STATE" = partial ] && echo "  (lsof stopped after 120s: partial figures)"
  if [ "$ALL_N" -gt "$OPEN_N" ]; then
    human "$ALL_BYTES"
    printf '  (host-wide: %d file(s), %s; the others are on filesystems not scanned here,\n' "$ALL_N" "$HUMAN"
    printf '   e.g. /tmp or /dev/shm, or are memory-only)\n'
  fi
  sort -t "$TAB" -k1,1nr "$WORK/open.roots" | head -n "$TOP_OPEN" |
    while IFS="$TAB" read -r s pid cmd name; do
      isnum "$s" && item "$s" "$name" "" "  (pid $pid, $cmd)"
    done
  [ "$OPEN_N" -gt "$TOP_OPEN" ] && printf '    ... and %d more\n' $((OPEN_N - TOP_OPEN))
  cat <<'EOF'
  The space of a deleted file returns only when the process holding it closes
  it, usually at a restart. This script never kills or restarts anything:
  restart the owning service in a maintenance window if the space is needed.
EOF
  [ -n "$SUDO" ] || echo "  Without sudo, only this user's processes are visible."
}

# --- rules ----------------------------------------------------------------------

# The find half of each rule. Regular files only, one filesystem, and no -L /
# -follow anywhere: find does not follow symlinks unless told to. Each record
# starts with 1 when the file is old enough for the rule.
rule_find() {  # root rule age
  case "$2" in
    logs)
      # JVM crash logs are tiny evidence files: kept as long as the logs.
      $SUDO find "$1" -xdev -type f -mtime +"$3" \( \
          -name '*.log.*' -o -name '*.log.gz' \
          -o -name 'catalina.20??-??-??*.log' -o -name 'localhost.20??-??-??*.log' \
          -o -name 'manager.20??-??-??*.log' -o -name 'host-manager.20??-??-??*.log' \
          -o -name 'localhost_access_log*.20??-??-??*.txt' \
          -o -name 'catalina.out.*' \
          -o -name 'hs_err_pid*.log' -o -name 'javacore*.txt' \
        \) -printf "1\t$RFMT_M" ;;
    wars)
      # Every backup, old or not, since the newest of each component is kept
      # whatever its age. Aged by ctime: the deploy makes a backup with mv,
      # which keeps the mtime the WAR got when the previous deploy copied it
      # to the server, but sets ctime to now.
      $SUDO find "$1" -xdev -type f -path '*/webapps/*' \( \
          -name '*.war_*' -o -name '*.war.[0-9]*' \
        \) \( -ctime +"$3" -printf "1\t$RFMT_C" -o -printf "0\t$RFMT_C" \) ;;
    dumps)
      $SUDO find "$1" -xdev -type f -mtime +"$3" \( \
          -name '*.hprof' -o -name 'java_pid*.hprof' \
          -o -name core -o -name 'core.[0-9]*' -o -name 'heapdump*.phd' \
        \) -printf "1\t$RFMT_M" ;;
  esac
}

# One rule_find record -> R_OLD R_KB R_DAY R_MT R_CT R_PATH, times in whole
# seconds. Split from the left: only the path can hold a tab.
split_rec() {  # record
  local r=$1
  R_OLD=${r%%"$TAB"*}; r=${r#*"$TAB"}
  R_KB=${r%%"$TAB"*};  r=${r#*"$TAB"}
  r=${r#*"$TAB"}                                # apparent size: not needed here
  R_DAY=${r%%"$TAB"*}; r=${r#*"$TAB"}
  R_MT=${r%%"$TAB"*};  r=${r#*"$TAB"}
  R_CT=${r%%"$TAB"*};  R_PATH=${r#*"$TAB"}
  R_MT=${R_MT%%.*}
  R_CT=${R_CT%%.*}
}

has_date() {  # 2026-09-01 or 20260901 somewhere in the name
  case "$1" in
    *20[0-9][0-9]-[01][0-9]-[0-3][0-9]*|*20[0-9][0-9][01][0-9][0-3][0-9]*) return 0 ;;
  esac
  return 1
}

# "core" is too generic a name to trust: require an ELF header of type
# ET_CORE (4), in either byte order.
is_elf_core() {
  local hex
  hex=$($SUDO od -An -tx1 -N18 -- "$1" 2>/dev/null | tr -d ' \n')
  case "$hex" in
    7f454c46????????????????????????0400|7f454c46????????????????????????0004) return 0 ;;
  esac
  return 1
}

# Last line of defence, applied to every file the find patterns matched.
# Sets SKIP to why the file must be left alone; empty means it may go.
skip_reason() {  # rule path
  local name=${2##*/} dir=${2%/*} rest
  SKIP=""
  case "$name" in
    # Written to right now: catalina.out, and the live file of a scheme that
    # marks it, such as the JVM's own GC log rotation (gc.log.0.current).
    catalina.out|*.current)
      SKIP="active log"; return ;;
    # Locks and log-shipper offsets can sit old and untouched while in use.
    *.lck|*.lock|*.pid|*.pos|*.swp)
      SKIP="lock or state file"; return ;;
    # JVM crash output: written once, by a JVM that has since died.
    hs_err_pid*.log|javacore*.txt)
      ;;
    # A .log or .txt with no date in its name is the file being written to.
    *.log|*.txt)
      has_date "$name" || { SKIP="active log (no date in its name)"; return; } ;;
  esac
  case "$1" in
    logs)
      case "$name" in
        catalina.out.*) rest=${name#catalina.out.} ;;
        *.log.*)        rest=${name##*.log.} ;;
        *)              rest=0 ;;   # dated Tomcat names and crash logs, checked above
      esac
      # "*.log.*" also matches things like x.log.properties: what follows
      # ".log." must look like a rotation (a number or date) or a compressor.
      case "$rest" in
        [0-9]*|gz|bz2|xz|zip|Z) ;;
        *) SKIP="not a rotation suffix" ;;
      esac ;;
    wars)
      [ "${dir##*/}" = webapps ] || SKIP="not directly inside a webapps directory"
      case "$name" in *.war) SKIP="live WAR" ;; esac ;;
    dumps)
      case "$name" in
        core|core.[0-9]*) is_elf_core "$2" || SKIP="named core but not an ELF core dump" ;;
      esac ;;
  esac
}

# The component a WAR backup belongs to: pc.war_20260901@1200 and pc.war.1
# are both pc's. Sets COMP.
war_comp() {  # name
  case "$1" in
    *.war_*) COMP=${1%.war_*} ;;
    *)       COMP=${1%.war.[0-9]*} ;;
  esac
}

# Newest ctime per component per directory, over every backup found, old or
# not. Parallel arrays: bash 3.2 has no associative ones, and backups are few.
war_newest() {  # found-file
  local rec key i
  WAR_KEYS=() WAR_MAX=()
  while IFS= read -r -d '' rec; do
    split_rec "$rec"
    isnum "$R_CT" || continue
    war_comp "${R_PATH##*/}"
    key=${R_PATH%/*}/$COMP
    for ((i = 0; i < ${#WAR_KEYS[@]}; i++)); do
      [ "${WAR_KEYS[i]}" = "$key" ] && break
    done
    if [ "$i" -eq ${#WAR_KEYS[@]} ]; then
      WAR_KEYS[i]=$key; WAR_MAX[i]=$R_CT
    elif [ "$R_CT" -gt "${WAR_MAX[i]}" ]; then
      WAR_MAX[i]=$R_CT
    fi
  done < "$1"
}

# The newest backup is the rollback copy: kept however old. Ties all stay.
war_keep() {  # path ctime -> SKIP
  local i key
  war_comp "${1##*/}"
  key=${1%/*}/$COMP
  for ((i = 0; i < ${#WAR_KEYS[@]}; i++)); do
    [ "${WAR_KEYS[i]}" = "$key" ] || continue
    if [ "$2" = "${WAR_MAX[i]}" ]; then
      # The reason goes in a TAB-separated record: no tab or newline in it.
      show "$COMP"; SKIP="newest backup of $SHOWN, kept for rollback"
    fi
    return 0
  done
}

excluded() {  # path
  local i
  for ((i = 0; i < ${#EXCLUDES[@]}; i++)); do
    # shellcheck disable=SC2254  # unquoted on purpose: the glob is the point
    case "$1" in ${EXCLUDES[i]}) return 0 ;; esac
  done
  return 1
}

# Removes each listed file after re-checking it, and says what happened.
# In:  "kb<TAB>date<TAB>mtime<TAB>ctime|-<TAB>path" NUL records.
# Out: "STATUS<TAB>kb<TAB>message<TAB>path" NUL records; STATUS is OK (message
#      is the date), GONE, SKIP or FAIL. Self-contained: under --sudo it runs
#      as root via "sudo -n bash -c", one sudo call per rule instead of one per
#      file.
delete_files() {
  local t=$'\t' rec kb day m c p d real err now
  while IFS= read -r -d '' rec; do
    kb=${rec%%$t*};  rec=${rec#*$t}
    day=${rec%%$t*}; rec=${rec#*$t}
    m=${rec%%$t*};   rec=${rec#*$t}
    c=${rec%%$t*};   p=${rec#*$t}
    d=${p%/*}
    if [ ! -e "$p" ] && [ ! -L "$p" ]; then
      printf 'GONE\t%s\t-\t%s\0' "$kb" "$p"; continue
    fi
    # Nothing may have become a symlink or a directory since the scan.
    if [ -L "$p" ] || [ ! -f "$p" ]; then
      printf 'SKIP\t%s\tno longer a regular file\t%s\0' "$kb" "$p"; continue
    fi
    # The directory must still be where find saw it: no symlink swapped into
    # the path since the scan.
    if ! real=$(cd -P -- "$d" 2>/dev/null && pwd); then
      printf 'FAIL\t%s\tcannot enter its directory\t%s\0' "$kb" "$p"; continue
    fi
    if [ "$real" != "$d" ]; then
      printf 'SKIP\t%s\tits directory now resolves elsewhere\t%s\0' "$kb" "$p"; continue
    fi
    # Still the file the scan judged old: a rotation can put a new file under
    # an old name (x.log -> x.log.1) between the scan and here.
    now=$(stat -c '%Y %Z' -- "$p" 2>/dev/null)
    if [ "${now% *}" != "$m" ] || { [ "$c" != - ] && [ "${now#* }" != "$c" ]; }; then
      printf 'SKIP\t%s\tchanged since the scan\t%s\0' "$kb" "$p"; continue
    fi
    if err=$(rm -f -- "$p" 2>&1); then
      printf 'OK\t%s\t%s\t%s\0' "$kb" "$day" "$p"
    else
      err=${err##*: }
      printf 'FAIL\t%s\t%s\t%s\0' "$kb" "${err//[[:cntrl:]]/ }" "$p"
    fi
  done
}

# One "left alone"/"FAILED" list from a "reason<TAB>path" NUL file, capped.
list_reasons() {  # file label cap
  local why path
  tr '\n\0' '?\n' < "$1" | head -n "$3" |
    while IFS="$TAB" read -r why path; do
      show "$why"; why=$SHOWN
      show "$path"; printf '      %s%s  (%s)\n' "$2" "$SHOWN" "$why"
    done
}

delete_candidates() {  # rule candidate-file count
  local rec st kb msg path why lost
  local ok=0 okkb=0 gone=0 skip=0 fail=0 seen=0
  printf '    removing (every removed file is listed):\n'
  # Read as the deleter goes, so each removal is logged the moment it happens
  # and a run cut off halfway still shows what it removed.
  while IFS= read -r -d '' rec; do
    st=${rec%%"$TAB"*};  rec=${rec#*"$TAB"}
    kb=${rec%%"$TAB"*};  rec=${rec#*"$TAB"}
    msg=${rec%%"$TAB"*}; path=${rec#*"$TAB"}
    isnum "$kb" || kb=0
    seen=$((seen + 1))
    case "$st" in
      OK)   ok=$((ok + 1)); okkb=$((okkb + kb))
            human $((kb * 1024)); show "$msg  $path"
            printf '      removed %8s  %s\n' "$HUMAN" "$SHOWN" ;;
      GONE) gone=$((gone + 1)) ;;
      SKIP) skip=$((skip + 1)); printf '%s\t%s\0' "$msg" "$path" >&3 ;;
      *)    fail=$((fail + 1)); printf '%s\t%s\0' "$msg" "$path" >&4 ;;
    esac
  done 3> "$WORK/$1.dskip" 4> "$WORK/$1.fail" < <(
    if [ -n "$SUDO" ]; then
      $SUDO bash -c "$(declare -f delete_files); delete_files" < "$2" 2> "$WORK/$1.delerr"
    else
      delete_files < "$2" 2> "$WORK/$1.delerr"
    fi)
  # Fewer answers than files means the deleter itself died (e.g. sudo refused).
  lost=$(($3 - seen))
  [ "$lost" -gt 0 ] || lost=0
  fail=$((fail + lost))
  DONE_N=$((DONE_N + ok)); DONE_KB=$((DONE_KB + okkb)); FAIL_N=$((FAIL_N + fail))

  human $((okkb * 1024))
  printf '    removed: %d file(s), %s' "$ok" "$HUMAN"
  [ "$gone" -gt 0 ] && printf '; already gone: %d' "$gone"
  [ "$skip" -gt 0 ] && printf '; left alone on re-check: %d' "$skip"
  [ "$fail" -gt 0 ] && printf '; FAILED: %d' "$fail"
  printf '\n'
  list_reasons "$WORK/$1.dskip" "left alone: " "$TOP_SKIP"
  [ "$skip" -gt "$TOP_SKIP" ] && printf '      ... and %d more left alone\n' $((skip - TOP_SKIP))
  list_reasons "$WORK/$1.fail" "FAILED: " "$TOP_FAIL"
  # Only the failures listed one by one; the lost ones are explained below.
  [ $((fail - lost)) -gt "$TOP_FAIL" ] && printf '      ... %d failure(s) in all\n' "$fail"
  if [ "$lost" -gt 0 ]; then
    printf '      FAILED: %d file(s) not processed; the delete step stopped early:\n' "$lost"
    head -n 3 "$WORK/$1.delerr" | while IFS= read -r why; do
      show "$why"; printf '        %s\n' "$SHOWN"
    done
  fi
}

run_rule() {  # rule age label [note]
  local rule=$1 rec kb day ct path n=0 sum=0 nskip=0 root
  local found="$WORK/$1.found" cand="$WORK/$1.cand" skipped="$WORK/$1.skip" errs="$WORK/$1.err"
  printf '\n  %s: %s older than %d days%s\n' "$rule" "$3" "$2" "${4:-}"
  : > "$found"
  : > "$errs"
  for root in "${ROOTS[@]}"; do
    rule_find "$root" "$rule" "$2" >> "$found" 2>> "$errs"
  done
  [ "$rule" = wars ] && war_newest "$found"
  # Every match passes the guard before it can become a candidate. The list is
  # read on fd 5 so nothing run by the guard can swallow it.
  while IFS= read -r -d '' rec <&5; do
    split_rec "$rec"
    [ "$R_OLD" = 1 ] || continue                # a WAR backup too recent for the rule
    isnum "$R_KB" && isnum "$R_MT" && isnum "$R_CT" || continue
    path=$R_PATH
    skip_reason "$rule" "$path"
    [ -z "$SKIP" ] && [ "$rule" = wars ] && war_keep "$path" "$R_CT"
    [ -z "$SKIP" ] && excluded "$path" && SKIP="excluded"
    if [ -n "$SKIP" ]; then
      nskip=$((nskip + 1))
      printf '%s\t%s\0' "$SKIP" "$path" >&3
    else
      n=$((n + 1)); sum=$((sum + R_KB))
      # ctime is re-checked only where it is the age: the WAR backups.
      ct=-
      [ "$rule" = wars ] && ct=$R_CT
      printf '%s\t%s\t%s\t%s\t%s\0' "$R_KB" "$R_DAY" "$R_MT" "$ct" "$path"
    fi
  done 5< "$found" > "$cand" 3> "$skipped" < /dev/null
  CAND_N=$((CAND_N + n)); CAND_KB=$((CAND_KB + sum))

  human $((sum * 1024))
  printf '    candidates: %d file(s), %s\n' "$n" "$HUMAN"
  # Clean mode lists every file as it is removed instead.
  if [ "$MODE" = report ]; then
    tr '\n\0' '?\n' < "$cand" | sort -t "$TAB" -k1,1nr | head -n "$TOP_CAND" |
      while IFS="$TAB" read -r kb day _ _ path; do
        isnum "$kb" && item $((kb * 1024)) "$path" "$day  "
      done
    [ "$n" -gt "$TOP_CAND" ] && printf '    ... and %d more, all smaller\n' $((n - TOP_CAND))
  fi
  if [ "$nskip" -gt 0 ]; then
    printf '    matched but left alone: %d\n' "$nskip"
    list_reasons "$skipped" "" "$TOP_SKIP"
    [ "$nskip" -gt "$TOP_SKIP" ] && printf '      ... and %d more\n' $((nskip - TOP_SKIP))
  fi
  read_errors "$errs"

  [ "$MODE" = clean ] || return 0
  if [ "$n" -eq 0 ]; then
    echo "    nothing to remove"
    return 0
  fi
  delete_candidates "$rule" "$cand" "$n"
}

trim_fail() {  # path reason
  FAIL_N=$((FAIL_N + 1))
  show "$1"; printf '      FAILED: %s  (' "$SHOWN"
  show "$2"; printf '%s)\n' "$SHOWN"
}

trim_one() {  # bytes path
  local size=$1 p=$2 d=${2%/*} keep=$1 keep_h bak rc err after before_h
  [ "$keep" -gt "$TRIM_KEEP" ] && keep=$TRIM_KEEP
  human "$keep"; keep_h=$HUMAN
  item "$size" "$p"
  if $SUDO test -L "$p" || ! $SUDO test -f "$p"; then
    trim_fail "$p" "no longer a regular file"; return
  fi
  if ! $SUDO test -w "$p"; then
    trim_fail "$p" "not writable by ${SUDO:+root via }$ME"; return
  fi
  bak=$d/catalina.out.$STAMP
  # No copy, no trim: emptying it without one would lose the whole log.
  avail_kb "$d"
  if [ $((AVAIL * 1024)) -lt $((keep + TRIM_HEADROOM)) ]; then
    human $((AVAIL * 1024))
    trim_fail "$p" "only $HUMAN free, too little to keep a copy of its last $keep_h; not trimmed"; return
  fi
  if $SUDO test -e "$bak" || $SUDO test -L "$bak"; then
    trim_fail "$p" "$bak already exists; not trimmed"; return
  fi
  # set -C: never overwrite. Exit 90 = the copy could not even be created.
  $SUDO bash -c 'set -C; exec 3> "$3" || exit 90; tail -c "$1" -- "$2" >&3' \
    _ "$keep" "$p" "$bak" 2>/dev/null
  rc=$?
  if [ "$rc" -eq 90 ]; then
    trim_fail "$p" "cannot create $bak; not trimmed"; return
  elif [ "$rc" -ne 0 ]; then
    $SUDO rm -f -- "$bak"
    trim_fail "$p" "copying its last $keep_h failed (exit $rc); not trimmed"; return
  fi
  # Same mode as the log it came from (and owner, under sudo): same data,
  # same readers.
  $SUDO chmod --reference="$p" -- "$bak" 2>/dev/null
  [ -n "$SUDO" ] && $SUDO chown --reference="$p" -- "$bak" 2>/dev/null
  # In place, same inode: Tomcat holds catalina.out open (O_APPEND) and keeps
  # writing to it. mv or rm would leave it writing to a deleted file.
  if ! err=$($SUDO truncate -s 0 -- "$p" 2>&1); then
    # The copy alone only adds usage.
    $SUDO rm -f -- "$bak"
    trim_fail "$p" "truncate failed: ${err##*: }"; return
  fi
  after=$($SUDO stat -c %s -- "$p" 2>/dev/null)
  isnum "$after" || after=0
  TRIM_N=$((TRIM_N + 1))
  [ "$size" -gt "$after" ] && TRIM_BYTES=$((TRIM_BYTES + size - after))
  human "$size"; before_h=$HUMAN
  human "$after"
  show "$bak"
  printf '      trimmed: %s -> %s; last %s kept in %s\n' "$before_h" "$HUMAN" "$keep_h" "$SHOWN"
}

run_trim() {
  local root rec size path keep n=0
  printf '\n  catalina.out: '
  if [ "$TRIM_MB" -eq 0 ]; then
    echo "trimming off (--trim-catalina-mb 0)"
    return 0
  fi
  printf 'trim files over %d MB, keeping the last 20 MB in a dated copy\n' "$TRIM_MB"
  : > "$WORK/trim"
  : > "$WORK/trim.err"
  for root in "${ROOTS[@]}"; do
    $SUDO find "$root" -xdev -type f -name catalina.out -size +"$TRIM_MB"M \
      -printf '%s\t%p\0' >> "$WORK/trim" 2>> "$WORK/trim.err"
  done
  while IFS= read -r -d '' rec <&5; do
    size=${rec%%"$TAB"*}; path=${rec#*"$TAB"}
    isnum "$size" || continue
    n=$((n + 1))
    if excluded "$path"; then
      item "$size" "$path" "" "  (excluded; left alone)"
    elif [ "$MODE" = clean ]; then
      trim_one "$size" "$path"
    else
      # The same room check clean mode makes, so the report never promises
      # a trim that clean would refuse.
      keep=$size; [ "$keep" -gt "$TRIM_KEEP" ] && keep=$TRIM_KEEP
      avail_kb "${path%/*}"
      if [ $((AVAIL * 1024)) -lt $((keep + TRIM_HEADROOM)) ]; then
        human $((AVAIL * 1024))
        item "$size" "$path" "" "  (would NOT trim: only $HUMAN free for its copy)"
      else
        item "$size" "$path" "" "  (would trim)"
      fi
    fi
  done 5< "$WORK/trim" < /dev/null
  [ "$n" -gt 0 ] || printf '    none over %d MB\n' "$TRIM_MB"
  [ "$n" -gt 0 ] && [ "$MODE" = report ] && echo "    (report mode: nothing trimmed)"
  read_errors "$WORK/trim.err"
}

report_rules() {
  local i
  section "6. Cleanup rules ($MODE mode)"
  if [ "$MODE" = report ]; then
    echo "  Report mode: lists what clean mode would remove. Nothing is changed."
  else
    # Just before the first delete, so the difference is this run's work.
    for ((i = 0; i < ${#FS_PATHS[@]}; i++)); do
      avail_kb "${FS_PATHS[i]}"; FS_BEFORE[i]=$AVAIL
    done
  fi
  run_rule logs  "$LOG_DAYS"  "rotated/archived logs and JVM crash logs"
  run_rule wars  "$WAR_DAYS"  "WAR backups in webapps" \
    "$NL    (counted from when each backup was made; the newest of each component is always kept)"
  run_rule dumps "$DUMP_DAYS" "heap dumps and core files"
  run_trim
  [ "$MODE" = clean ] || return 0

  printf '\n  After cleanup (df):\n'
  print_df
  FREED=0
  for ((i = 0; i < ${#FS_PATHS[@]}; i++)); do
    avail_kb "${FS_PATHS[i]}"
    [ "$AVAIL" -gt "${FS_BEFORE[i]}" ] && FREED=$((FREED + (AVAIL - ${FS_BEFORE[i]}) * 1024))
  done
  human $((DONE_KB * 1024)); printf '\n  Removed %d file(s), %s.' "$DONE_N" "$HUMAN"
  if [ "$TRIM_N" -gt 0 ]; then
    human "$TRIM_BYTES"; printf ' Trimmed %d catalina.out by %s.' "$TRIM_N" "$HUMAN"
  fi
  human "$FREED"; printf ' Freed %s (df, before vs after).\n' "$HUMAN"
  if [ "$FAIL_N" -gt 0 ]; then
    printf '  %d FAILURE(S), listed above under each rule. Permission denied usually\n' "$FAIL_N"
    printf '  means the files belong to another account: see useSudo in the README.\n'
  fi
}

summary() {
  local cand open done_h text
  human $((CAND_KB * 1024)); cand=$HUMAN
  human "$OPEN_BYTES"; open=$HUMAN
  case "$OPEN_STATE" in
    none)    open="deleted-but-open files not measured (no lsof)" ;;
    partial) open="$open held by deleted-but-open files on the roots' filesystems (partial: lsof stopped after 120s)" ;;
    *)       open="$open held by deleted-but-open files on the roots' filesystems" ;;
  esac
  if [ "$MODE" = report ]; then
    printf -v text "report; %d file(s), %s would be removed; %d filesystem(s) at 90%%+; %s" \
      "$CAND_N" "$cand" "$FULL_N" "$open"
  else
    human $((DONE_KB * 1024)); done_h=$HUMAN
    human "$FREED"
    printf -v text 'clean; removed %d file(s), %s; trimmed %d catalina.out; %d failure(s); freed %s per df' \
      "$DONE_N" "$done_h" "$TRIM_N" "$FAIL_N" "$HUMAN"
  fi
  summary_line "$text"
}

main() {
  # First, so that even a bad argument ends with a SUMMARY line naming the host.
  HOST=$(hostname 2>/dev/null || uname -n)
  parse_args "$@"
  validate_args
  check_roots_syntax
  command -v timeout >/dev/null 2>&1 && HAVE_TIMEOUT=1
  setup_sudo
  resolve_roots
  make_workdir
  [ ${#ROOTS[@]} -gt 0 ] && build_fs_list

  STAMP=$(date +%Y%m%d%H%M%S)
  # Only ever printed, so made safe to print once here.
  show "$(id -un 2>/dev/null || id -u)"; ME=$SHOWN
  show "$HOST"
  printf 'disk_cleanup.sh on %s at %s, mode %s\n' "$SHOWN" "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$MODE"

  report_host
  if [ ${#ROOTS[@]} -eq 0 ]; then
    printf '\nNone of the requested roots exist on this host; nothing to scan.\n'
    summary_line "no roots present; nothing scanned"
    exit 0
  fi
  section "2. Filesystems holding the roots (df -hP; !! = 90% or more used)"
  print_df
  report_dirs
  report_files
  report_open
  report_rules
  summary

  [ "$MODE" = clean ] && [ "$FAIL_N" -gt 0 ] && exit 1
  exit 0
}

main "$@"
