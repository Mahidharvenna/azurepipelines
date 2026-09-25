#!/usr/bin/env bash
# Prepare Python for the login pipelines on a Linux agent.
#
#   1. print what the agent has -- whoever installs prerequisites needs this
#   2. find every python >= 3.9 on PATH
#   3. build a job-local venv with the first one that can: avoids PEP 668
#      ('externally-managed-environment' on Debian 12+/Ubuntu 23.04+) and old
#      system pip that can't install pyodbc's wheels (RHEL 8's pip 9)
#   4. make pyodbc importable in it
#   5. hand the venv's interpreter to later steps as $(PYTHON_EXE)
#
# Fails only when no Python can build a venv. pyodbc problems are warnings, so
# the next step still runs and reports everything in one go -- setup.py 'check'
# diagnoses a missing ODBC stack precisely.
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
if command -v odbcinst >/dev/null 2>&1; then
  drivers="$(odbcinst -q -d 2>/dev/null | tr -d '[]' | paste -sd ',' -)"
  echo "ODBC drivers   : ${drivers:-none registered}"
else
  echo "ODBC drivers   : odbcinst not found -- unixODBC is not installed"
fi
LDCONFIG="$(command -v ldconfig 2>/dev/null || echo /sbin/ldconfig)"
if [ -x "$LDCONFIG" ]; then
  # Captured first: 'ldconfig -p | grep -q' under pipefail reports a false
  # MISSING, because grep exits early and ldconfig dies of SIGPIPE.
  libs="$("$LDCONFIG" -p 2>/dev/null)"
  case "$libs" in
    *libodbc.so.2*) echo "libodbc.so.2   : present" ;;
    *)              echo "libodbc.so.2   : MISSING" ;;
  esac
fi
[ -n "${PIP_INDEX_URL:-}" ] && echo "pip index      : PIP_INDEX_URL is set"

# ---- 1. interpreters >= 3.9 -------------------------------------------------
# Order: any that already imports pyodbc first -- an admin-installed OS package
# (RHEL 9's python3-pyodbc) exists only for that distro's own python3 -- then
# newest first.
WITH_PYODBC=""
OTHERS=""
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
  if "$p" -c 'import pyodbc' >/dev/null 2>&1; then
    echo "python         : $c -> $p ($v) -- already has pyodbc"
    WITH_PYODBC="$WITH_PYODBC $p"
  else
    echo "python         : $c -> $p ($v)"
    OTHERS="$OTHERS $p"
  fi
done
CANDIDATES="$WITH_PYODBC $OTHERS"
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
# --system-site-packages: an admin-installed OS package (python3-pyodbc) is
# picked up as-is, so agents with no route to PyPI still work. Trying each in
# turn means a newer python3.X without its venv package can't block a working
# python3.
VPY=""
for PY in $CANDIDATES; do
  rm -rf "$VENV"
  if "$PY" -m venv --system-site-packages "$VENV" >"$WORK/gwpy-venv.log" 2>&1; then
    VPY="$VENV/bin/python"
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

# ---- 3. pyodbc -------------------------------------------------------------
import_pyodbc() {
  "$VPY" -c 'import pyodbc; print("pyodbc %s  (%s)" % (pyodbc.version, pyodbc.__file__))' 2>"$WORK/gwpy-import.err"
}
pyodbc_present() {
  "$VPY" -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("pyodbc") else 1)' 2>/dev/null
}
explain_import() {
  err="$(tail -n 1 "$WORK/gwpy-import.err" 2>/dev/null)"
  case "$err" in
    *libodbc*) warn "pyodbc is installed but cannot load unixODBC ($err). An admin must install Microsoft's ODBC Driver 18 (msodbcsql18), which pulls in unixODBC -- see DEPLOY.md, phase 0." ;;
    *)         warn "pyodbc is installed but failed to import: ${err:-unknown error}" ;;
  esac
}
PIP_OPTS="--disable-pip-version-check --retries 2 --timeout 30"

if import_pyodbc; then
  :
elif pyodbc_present; then
  explain_import
else
  echo "Installing pyodbc into $VENV ..."
  # A current pip first: an old one can't read pyodbc's manylinux wheels and
  # tries to compile it, which needs gcc and unixODBC headers.
  "$VPY" -m pip install $PIP_OPTS --upgrade pip >"$WORK/gwpy-pip.log" 2>&1 || true
  if "$VPY" -m pip install $PIP_OPTS --only-binary=:all: pyodbc >>"$WORK/gwpy-pip.log" 2>&1; then
    import_pyodbc || explain_import
  else
    tail -n 15 "$WORK/gwpy-pip.log"
    case "$FAMILY:$OS_VER" in
      debian:*) os_pkg=" or have an admin install the OS package: apt-get install python3-pyodbc" ;;
      rhel:9*)  os_pkg=" or have an admin install the OS package: dnf install python3-pyodbc" ;;
      *)        os_pkg="" ;;
    esac
    warn "pip could not install pyodbc (above). No route to PyPI? Set PIP_INDEX_URL (an internal PyPI mirror; may be a secret) or HTTPS_PROXY in the variable group$os_pkg."
  fi
fi

echo "##vso[task.setvariable variable=PYTHON_EXE]$VPY"
echo "PYTHON_EXE     : $VPY"
if [ "$ISSUES" = 1 ]; then
  echo "##vso[task.complete result=SucceededWithIssues;]Python is ready but pyodbc is not usable yet -- see the warnings."
fi
exit 0
