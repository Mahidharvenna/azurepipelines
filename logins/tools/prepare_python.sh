#!/usr/bin/env bash
# Prepare Python for the login pipelines on a Linux agent.
#
#   1. print what the agent has -- whoever installs prerequisites needs this
#   2. find every python >= 3.9 on PATH
#   3. build a job-local venv with the first one that can: avoids PEP 668
#      ('externally-managed-environment' on Debian 12+/Ubuntu 23.04+) and old
#      system pip that can't install modern wheels (RHEL 8's pip 9)
#   4. install pymssql into it -- its wheel carries its own SQL Server client,
#      so no ODBC driver or admin install is needed
#   5. hand the venv's interpreter to later steps as $(PYTHON_EXE)
#
# Fails only when no Python can build a venv. An install problem is a warning,
# so the next step still runs and reports everything else in one go.
#
# Usage: prepare_python.sh <work-dir>     (the pipeline passes $(Agent.TempDirectory))

set -u -o pipefail

WORK="${1:-${AGENT_TEMPDIRECTORY:-/tmp}}"
VENV="$WORK/gwpy"
MIN_MINOR=9
ISSUES=0

warn() { echo "##vso[task.logissue type=warning]$*"; ISSUES=1; }
fail() { echo "##vso[task.logissue type=error]$*"; exit 1; }

# Every log below is written here; if it can't be created, say so rather than
# letting a failed redirect masquerade as a broken venv module.
mkdir -p "$WORK" 2>/dev/null && [ -w "$WORK" ] || fail "Work directory '$WORK' does not exist and cannot be created."

# Pipeline variables mapped explicitly but not defined arrive as the literal
# "$(NAME)". pip would try that as an index URL or proxy, so drop them.
for v in PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_TRUSTED_HOST HTTPS_PROXY HTTP_PROXY NO_PROXY \
         https_proxy http_proxy no_proxy; do
  eval "val=\${$v:-}"
  case "$val" in '$('*) unset "$v" ;; esac
done

OS_RELEASE="${OS_RELEASE_FILE:-/etc/os-release}"     # overridable for tests only
os_field() { ( [ -r "$OS_RELEASE" ] && . "$OS_RELEASE" && eval "echo \${$1:-}" ) 2>/dev/null; }
OS_ID="$(os_field ID)"
OS_VER="$(os_field VERSION_ID)"
case "$OS_ID $(os_field ID_LIKE)" in
  *rhel*|*fedora*|*centos*) FAMILY=rhel ;;
  *debian*|*ubuntu*)        FAMILY=debian ;;
  *)                        FAMILY=other ;;
esac

PATH_HINT="If Python is installed outside the directories on the agent's PATH, refresh the agent's PATH snapshot: in the agent directory, as the agent user, run ./env.sh, then sudo ./svc.sh stop && sudo ./svc.sh start. (A restart alone re-reads the old snapshot in .path.)"

echo "=================== agent ==================="
echo "OS             : $(os_field PRETTY_NAME || true) ($(uname -s) $(uname -m))"
echo "user           : $(id -un 2>/dev/null || echo '?')"
echo "PATH           : $PATH"
[ -n "${PIP_INDEX_URL:-}" ] && echo "pip index      : PIP_INDEX_URL is set"

# ---- 1. interpreters >= 3.9, newest first -----------------------------------
CANDIDATES=""
SEEN=""
for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
  p="$(command -v "$c" 2>/dev/null)" || continue
  real="$(readlink -f "$p" 2>/dev/null || echo "$p")"
  case " $SEEN " in *" $real "*) continue ;; esac
  SEEN="$SEEN $real"
  v="$("$p" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)" || continue
  if ! "$p" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MIN_MINOR) else 1)" 2>/dev/null; then
    echo "python         : $c -> $p ($v) -- too old"
    continue
  fi
  echo "python         : $c -> $p ($v)"
  CANDIDATES="$CANDIDATES $p"
done
echo "=============================================="

if [ -z "${CANDIDATES// /}" ]; then
  case "$FAMILY:$OS_ID:$OS_VER" in
    rhel:*)             hint="sudo dnf install -y python3.11" ;;
    debian:ubuntu:20.*) hint="sudo apt-get install -y python3.9 python3.9-venv" ;;
    debian:debian:9|debian:debian:10)
                        hint="this Debian release has no Python 3.$MIN_MINOR package and is past end of life -- upgrade the agent's OS" ;;
    debian:*)           hint="sudo apt-get install -y python3 python3-venv" ;;
    *)                  hint="install Python 3.$MIN_MINOR or newer" ;;
  esac
  fail "No Python >= 3.$MIN_MINOR found. Admin fix: $hint. $PATH_HINT"
fi

# ---- 2. job-local venv: first interpreter that can build one ---------------
# Trying each in turn means a newer python3.X without its venv package can't
# block a working python3.
VPY=""
for PY in $CANDIDATES; do
  rm -rf "$VENV"
  if "$PY" -m venv "$VENV" >"$WORK/gwpy-venv.log" 2>&1; then
    VPY="$VENV/bin/python"
    PYV="$("$VPY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    echo "Using $("$PY" --version 2>&1) ($PY)"
    break
  fi
  echo "venv failed with $PY -- trying the next interpreter:"
  tail -n 2 "$WORK/gwpy-venv.log" | sed 's/^/    /'
done
if [ -z "$VPY" ]; then
  case "$FAMILY" in
    debian) hint="sudo apt-get install -y python3-venv   (plus python3.X-venv for any other python3.X listed above)" ;;
    *)      hint="install the venv module for the interpreters listed above" ;;
  esac
  fail "None of the Python interpreters above could create a virtualenv. Admin fix: $hint"
fi

# ---- 3. pymssql ------------------------------------------------------------
# Pinned: the Linux wheel bundles FreeTDS, OpenSSL and Kerberos, so this one
# package is the entire SQL Server client.
#
# Installed from the wheels committed next to this script first -- the build
# agent has no route to PyPI, and a pip that can't connect spends minutes
# retrying before it gives up. Only if none of those wheels fits this Python
# does it try an index, and then only after a quick check that it's reachable.
PYMSSQL="pymssql==2.3.13"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WHEELS="${GW_WHEELS_DIR:-$HERE/wheels}"          # overridable for tests only
import_pymssql() {
  "$VPY" -c 'import pymssql; print("pymssql %s  (%s)" % (pymssql.__version__, pymssql.__file__))' 2>"$WORK/gwpy-import.err"
}
index_reachable() {
  # A proxy or a private index can't be probed reliably from here -- let pip try.
  if [ -n "${PIP_INDEX_URL:-}${HTTPS_PROXY:-}${https_proxy:-}" ]; then return 0; fi
  "$VPY" - <<'PY' 2>/dev/null
import sys, urllib.request
try:
    urllib.request.urlopen("https://pypi.org/simple/pymssql/", timeout=10).read(1)
except Exception as ex:
    print("    PyPI unreachable: %s" % ex)
    sys.exit(1)
PY
}

installed=""
if [ -d "$WHEELS" ] && ls "$WHEELS"/*.whl >/dev/null 2>&1; then
  echo "Installing $PYMSSQL from $WHEELS (no network) ..."
  if "$VPY" -m pip install --disable-pip-version-check --no-index --find-links "$WHEELS" \
       "$PYMSSQL" >"$WORK/gwpy-pip.log" 2>&1; then
    installed=local
  else
    echo "    no wheel there fits this Python ($PYV) -- trying a package index"
  fi
fi
if [ -z "$installed" ]; then
  if index_reachable; then
    echo "Installing $PYMSSQL from ${PIP_INDEX_URL:+your PIP_INDEX_URL}${PIP_INDEX_URL:-PyPI} ..."
    "$VPY" -m pip install --disable-pip-version-check --retries 1 --timeout 20 \
         --only-binary=:all: "$PYMSSQL" >>"$WORK/gwpy-pip.log" 2>&1 && installed=index
    [ -n "$installed" ] || tail -n 15 "$WORK/gwpy-pip.log"
  fi
fi
if [ -n "$installed" ]; then
  import_pymssql || warn "pymssql installed but failed to import: $(tail -n 1 "$WORK/gwpy-import.err")"
else
  warn "Could not install $PYMSSQL: no wheel in $WHEELS fits Python $PYV, and no package index is reachable. Add a matching wheel there (see its README), or set PIP_INDEX_URL (an internal mirror; may be a secret) or HTTPS_PROXY in the variable group."
fi

echo "##vso[task.setvariable variable=PYTHON_EXE]$VPY"
echo "PYTHON_EXE     : $VPY"
if [ "$ISSUES" = 1 ]; then
  echo "##vso[task.complete result=SucceededWithIssues;]Python is ready but pymssql is not usable yet -- see the warnings."
fi
exit 0
