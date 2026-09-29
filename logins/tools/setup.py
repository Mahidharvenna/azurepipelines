#!/usr/bin/env python3
"""
One-shot bootstrap for the login-history database, run from the pipeline.

Does the setup that would otherwise need sqlcmd on someone's laptop:
pre-flight checks, schema, grants, and verification.

'check' reports EVERY problem it can find in one run rather than stopping at
the first -- each miss would otherwise cost a full pipeline round-trip. It
exits non-zero if anything would stop the collector, even when the database
side is fine.

Because it uses pymssql rather than sqlcmd, it does three things sqlcmd does
client-side and the server knows nothing about:
  * split each file on GO      -- a batch separator, not T-SQL
  * expand :setvar / $(TOKEN)  -- a sqlcmd variable construct
  * surface PRINT as headings  -- the client does not expose PRINT output

Configuration comes from environment variables set by the pipeline, so no
secret is ever passed as an argument (arguments are echoed in the build log).

Actions
-------
  check   pre-flight only. Touches nothing. The default, deliberately.
  schema  apply sql/schema.sql (idempotent). Tables that predate the project
          column get it, their rows tagged with LOKI_PROJECT.
  grants  apply sql/grants.sql  -- needs --grafana-login
  verify  run sql/verify.sql and print every result set
  all     check, schema, grants, verify -- in that order
"""

import os
import re
import sys
import json
import time
import socket
import platform
import argparse
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gwcommon import (env, env_bool, harden_stdio, explain_import_error, parse_server,
                      explain_connect_error, loki_ssl_context, is_cert_error, LOKI_CERT_HINT,
                      one_line, connect_sql, apply_session_options, tds_probe,
                      is_login_failure, sql_error_text, param)

harden_stdio()

MIN_PY = (3, 9)
# 13.0.4001 = SQL Server 2016 SP1, the first build with CREATE OR ALTER.
MIN_SQL = (13, 0, 4001)
TABLES = ["gw_login_daily", "gw_login_user_daily", "gw_login_collector_run"]
VIEWS = ["gw_login_monthly", "gw_login_monthly_users", "gw_login_freshness"]
EXPECTED_OBJECTS = sorted(TABLES + VIEWS)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
DB_SERVER = env("DB_SERVER")
DB_NAME = env("DB_NAME")
DB_USER = env("DB_USER")
DB_PASS = env("DB_PASS")
DB_TRUSTED = env_bool("DB_TRUSTED_CONNECTION", False)
DB_ENCRYPT = env_bool("DB_ENCRYPT", True)

LOKI_URL = env("LOKI_URL").rstrip("/")
LOKI_VERIFY = env_bool("LOKI_VERIFY_TLS", True)
LOKI_CA_BUNDLE = env("LOKI_CA_BUNDLE").strip()
BYPASS_PROXY = env_bool("BYPASS_PROXY", True)

# The Loki `project` label values the collector covers, read as it reads them:
# unset means LOKI_PROJECT alone, the single project there was before.
LOKI_PROJECT = env("LOKI_PROJECT").strip()
LOKI_PROJECTS = ([p.strip() for p in env("LOKI_PROJECTS").split(",") if p.strip()]
                 or ([LOKI_PROJECT] if LOKI_PROJECT else []))
# The collector's rule for a project value: it goes inside a LogQL selector's
# double quotes and into a VARCHAR(64) column.
PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def existing_project():
    """The project that rows stored before the project column belong to:
    LOKI_PROJECT, the only project the collector read until then -- whatever
    order LOKI_PROJECTS lists projects in. The first LOKI_PROJECTS entry only
    when LOKI_PROJECT is unset. '' when neither is set."""
    return LOKI_PROJECT or (LOKI_PROJECTS[0] if LOKI_PROJECTS else "")


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------
SOFT = []        # worth knowing, doesn't stop anything
BLOCKERS = []    # (kind, text); kind 'db' stops schema/grants, 'collector' only fails the run


def section(text):
    print("\n" + "=" * 72)
    print("  " + text)
    print("=" * 72, flush=True)


def ok(text):    print("  [ OK ] " + text, flush=True)
def info(text):  print("         " + text, flush=True)


def soft(text):
    SOFT.append(text)
    print("##vso[task.logissue type=warning]" + one_line(text), flush=True)


def blocker(kind, text):
    BLOCKERS.append((kind, text))
    print("##vso[task.logissue type=error]" + one_line(text), flush=True)


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------
_sql = None


def get_sql():
    """Import pymssql lazily, so 'check' can still report everything else if
    the install step failed."""
    global _sql
    if _sql is None:
        try:
            import pymssql
        except ImportError as ex:
            raise SystemExit(explain_import_error(ex))
        _sql = pymssql
    return _sql


def connect():
    p = get_sql()
    if not DB_TRUSTED and not DB_USER:
        raise SystemExit("Set DB_USER/DB_PASS, or DB_TRUSTED_CONNECTION=true.")
    try:
        cn = connect_sql(p, DB_SERVER, DB_NAME, DB_USER, DB_PASS, DB_TRUSTED, DB_ENCRYPT,
                         timeout=600, appname="GW-Login-Setup", autocommit=True)
    except p.Error as ex:
        raise SystemExit(explain_connect_error(ex, DB_SERVER, DB_NAME))
    apply_session_options(cn)
    return cn


def query(sql):
    cn = connect()
    try:
        cur = cn.cursor()
        try:
            cur.execute(sql)          # no params: pymssql leaves '%' in the SQL alone
        except get_sql().Error as ex:
            raise SystemExit("Query failed: %s\n  query: %s"
                             % (sql_error_text(ex), " ".join(sql.split())[:160]))
        return cur.fetchall() if cur.description else []
    finally:
        cn.close()


def scalar(sql):
    rows = query(sql)
    return rows[0][0] if rows else None


# What the collector does to each table; the collector connects as DBUSER.
COLLECTOR_DML = [
    ("gw_login_daily", ("SELECT", "INSERT", "UPDATE")),
    ("gw_login_collector_run", ("SELECT", "INSERT", "UPDATE")),
    ("gw_login_user_daily", ("SELECT", "INSERT", "UPDATE", "DELETE")),
]


def missing_dml():
    """Rights the RUNNING login lacks on the collector's tables. Creating a table
    in dbo does not make you its owner -- the schema owner owns it -- so a
    db_ddladmin account can build the tables and still be unable to write them."""
    checks = " UNION ALL ".join(
        "SELECT '%s on %s' AS need, HAS_PERMS_BY_NAME('dbo.%s','OBJECT','%s') AS has"
        % (perm, table, table, perm)
        for table, perms in COLLECTOR_DML for perm in perms)
    return [need for need, has in query(checks) if has != 1]


def tables_missing_project(present):
    """Existing tables without the project column -- created before it, so
    schema.sql has to upgrade them and the collector refuses to write them."""
    tables = [t for t in TABLES if t in present]
    if not tables:
        return []
    checks = " UNION ALL ".join(
        "SELECT '%s' AS name, COL_LENGTH('dbo.%s','project') AS len" % (t, t) for t in tables)
    lacking = set(name for name, length in query(checks) if length is None)
    return [t for t in tables if t in lacking]


def schema_needs(present):
    """(right, HAS_PERMS_BY_NAME test) pairs that schema.sql needs, given the
    objects that already exist. Creating anything in dbo takes ALTER on the
    schema as well as CREATE TABLE / CREATE VIEW, and so does adding the
    project column: ALTER on dbo covers the ALTER TABLEs, the rebuilt keys and
    indexes, and the views. Once everything is in place a re-run creates
    nothing -- its CREATE TABLEs and indexes are guarded -- and needs only
    ALTER on each view, for CREATE OR ALTER VIEW."""
    new_tables = any(t not in present for t in TABLES)
    # With every table in place, views it can't see are almost always views it
    # can no longer see -- its ALTER on dbo was revoked after 'schema' -- and
    # ALTER on dbo alone brings them back. Asking for CREATE VIEW too would have
    # a DBA grant a right that isn't needed. (If they truly are missing,
    # schema.sql's CREATE OR ALTER VIEW says so plainly.)
    views_hidden = any(v not in present for v in VIEWS)
    new_views = views_hidden and new_tables
    upgrade = bool(tables_missing_project(present))
    needs = []
    if new_tables:
        needs.append(("CREATE TABLE", "HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE TABLE')"))
    if new_views:
        needs.append(("CREATE VIEW", "HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE VIEW')"))
    if new_tables or views_hidden or upgrade:
        needs.append(("ALTER ON SCHEMA::dbo", "HAS_PERMS_BY_NAME('dbo','SCHEMA','ALTER')"))
    else:
        needs += [("ALTER ON dbo.%s" % v, "HAS_PERMS_BY_NAME('dbo.%s','OBJECT','ALTER')" % v)
                  for v in VIEWS]
    return needs


def missing_ddl(present):
    """schema.sql rights the RUNNING login lacks -- one by one, so a partial
    grant says what is still missing."""
    needs = schema_needs(present)
    checks = " UNION ALL ".join("SELECT '%s' AS need, %s AS has" % n for n in needs)
    lacking = set(need for need, has in query(checks) if has != 1)
    return [need for need, _ in needs if need in lacking]


def ddl_message(lacking):
    return ("This account lacks %s, which 'schema' needs. A DBA runs this in %s, then run "
            "'schema' (or the DBA runs schema.sql itself):\n%s"
            % (", ".join(lacking), DB_NAME, grant_lines(lacking)))


NO_PROJECT_COLUMN = "The login tables have no project column yet"


def upgrade_message(missing):
    """What to do about tables that predate the project column."""
    project = existing_project()
    if not project:
        return ("%s (%s), and the collector refuses to run against them. Set LOKI_PROJECT to "
                "the project their rows came from, then run 'schema' to add the column -- it "
                "tags those rows with it." % (NO_PROJECT_COLUMN, ", ".join(missing)))
    rows = ""
    if "gw_login_daily" in missing:
        try:
            rows = " (%s in gw_login_daily)" % scalar(
                "SELECT COUNT_BIG(*) FROM dbo.gw_login_daily")
        except SystemExit:
            pass        # no SELECT on it: the count is only a courtesy
    return ("%s (%s), and the collector refuses to run against them. Run 'schema' to add the "
            "project column; the existing rows%s are tagged '%s' -- LOKI_PROJECT, else the first "
            "LOKI_PROJECTS entry." % (NO_PROJECT_COLUMN, ", ".join(missing), rows, project))


def explicit_ddl_grants():
    """REVOKEs for the schema-only rights granted to this user directly. The
    collector needs none of them, and ALTER on dbo reaches every object in it."""
    rows = query("SELECT permission_name, class FROM sys.database_permissions "
                 "WHERE grantee_principal_id = USER_ID() AND state IN ('G', 'W') "
                 "AND ((class = 0 AND permission_name IN ('CREATE TABLE', 'CREATE VIEW')) "
                 "OR (class = 3 AND major_id = SCHEMA_ID('dbo') AND permission_name = 'ALTER'))")
    user = str(scalar("SELECT USER_NAME()")).replace("]", "]]")
    return ["    REVOKE %s%s FROM [%s];" % (perm, " ON SCHEMA::dbo" if cls == 3 else "", user)
            for perm, cls in sorted(rows, key=lambda r: (r[1], r[0]))]


def dml_rights(lacking):
    """missing_dml() output as grantable rights, one per table:
    ['SELECT on t', 'INSERT on t'] -> ['SELECT, INSERT ON dbo.t']."""
    by_table = {}
    for need in lacking:
        perm, _, table = need.partition(" on ")
        by_table.setdefault(table, []).append(perm)
    return ["%s ON dbo.%s" % (", ".join(p), t) for t, p in by_table.items()]


def grant_lines(rights):
    """The GRANTs that give the running login's database user `rights`, ready
    for a DBA to paste into SSMS."""
    user = str(scalar("SELECT USER_NAME()")).replace("]", "]]")
    return "\n".join("    GRANT %s TO [%s];" % (r, user) for r in rights)


def is_running_login(name):
    """Compare with SQL Server's own rules (collation), exactly as grants.sql's
    IF SUSER_NAME() guard will -- a Python .lower() could disagree with a
    case-sensitive server and send a GRANT to yourself."""
    return scalar("SELECT CASE WHEN SUSER_NAME() = N'%s' THEN 1 ELSE 0 END"
                  % (name or "").replace("'", "''")) == 1


def existing_objects():
    names = ", ".join("'%s'" % n for n in EXPECTED_OBJECTS)
    return sorted(r[0] for r in query(
        "SELECT name FROM sys.objects WHERE schema_id = SCHEMA_ID('dbo') AND name IN (%s)" % names))


# ---------------------------------------------------------------------------
# sqlcmd constructs the server does not understand
# ---------------------------------------------------------------------------
SETVAR_RE = re.compile(r'^\s*:setvar\s+(\w+)\s+"?([^"]*)"?\s*$')
GO_RE = re.compile(r'(?im)^[ \t]*GO[ \t]*$')
PRINT_RE = re.compile(r"^\s*PRINT\s+'(.*?)'\s*;?\s*$", re.IGNORECASE)


def expand_setvar(text, overrides=None, placeholder_ok=()):
    """Resolve :setvar declarations and $(TOKEN) references, then drop the
    :setvar lines. Overrides (from the command line) win over the file.
    placeholder_ok names variables the SQL checks itself, only where it needs
    them -- schema.sql's ExistingProject matters only to an upgrade."""
    variables = {}
    for line in text.split("\n"):
        m = SETVAR_RE.match(line)
        if m:
            variables[m.group(1)] = m.group(2)
    for k, v in (overrides or {}).items():
        if v:
            variables[k] = v
    for k, v in variables.items():
        if v.startswith("your_") and k not in placeholder_ok:
            raise SystemExit("%s is still the placeholder '%s' -- pass the real value." % (k, v))
    kept = [l for l in text.split("\n") if not re.match(r"^\s*:setvar\s", l)]
    out = "\n".join(kept)
    for k, v in variables.items():
        out = out.replace("$(" + k + ")", v)
        info(":setvar %s = %s" % (k, v))
    leftover = re.findall(r"\$\(\w+\)", out)
    if leftover:
        raise SystemExit("Unresolved sqlcmd token(s) %s -- pass them on the command line."
                         % sorted(set(leftover)))
    return out


def split_batches(text):
    return [b for b in GO_RE.split(text) if b.strip()]


def split_labelled(batch):
    """Split one batch into (label, sql) chunks on PRINT lines.

    The client gives no access to PRINT output, so the PRINT statements -- which is
    where verify.sql keeps its section headings -- would otherwise vanish.
    """
    chunks, label, buf = [], None, []
    for line in batch.split("\n"):
        m = PRINT_RE.match(line)
        if m:
            if "".join(buf).strip():
                chunks.append((label, "\n".join(buf)))
            label, buf = m.group(1), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        chunks.append((label, "\n".join(buf)))
    return chunks


def print_table(cursor):
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    if not rows:
        print("         (no rows)", flush=True)
        return 0
    widths = [len(c) for c in cols]
    text_rows = []
    for r in rows:
        cells = ["" if v is None else str(v) for v in r]
        text_rows.append(cells)
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))
    widths = [min(w, 48) for w in widths]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    print("         " + fmt.format(*[c[:48] for c in cols]), flush=True)
    print("         " + "  ".join("-" * w for w in widths), flush=True)
    for cells in text_rows:
        print("         " + fmt.format(*[c[:48] for c in cells]), flush=True)
    return len(rows)


def run_sql_file(path, overrides=None, show_results=False, placeholder_ok=()):
    if not os.path.isfile(path):
        raise SystemExit("SQL file not found: %s" % path)
    info("file: %s" % path)
    text = expand_setvar(open(path, encoding="utf-8-sig").read(), overrides, placeholder_ok)
    batches = split_batches(text)
    info("%d batch(es)" % len(batches))

    cn = connect()
    total_rows = 0
    try:
        cur = cn.cursor()
        for n, batch in enumerate(batches, 1):
            units = split_labelled(batch) if show_results else [(None, batch)]
            for label, sql in units:
                if not sql.strip():
                    continue
                try:
                    cur.execute(sql)
                except Exception as ex:
                    preview = " / ".join(
                        l.strip() for l in sql.strip().split("\n")[:3] if l.strip())
                    raise SystemExit("Batch %d of %s failed: %s\n  near: %s"
                                     % (n, path, ex, preview))
                if label:
                    print("\n  -- %s" % label, flush=True)
                if show_results:
                    while True:
                        if cur.description:
                            total_rows += print_table(cur)
                        if not cur.nextset():
                            break
    finally:
        cn.close()
    ok("applied %s" % path)
    return total_rows


# ---------------------------------------------------------------------------
# pre-flight
# ---------------------------------------------------------------------------
def tcp_ok(host, port, timeout=6):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def resolves(host):
    try:
        socket.getaddrinfo(host, None)
        return True
    except Exception:
        return False


def version_tuple(text):
    return tuple(int(x) for x in re.findall(r"\d+", str(text))[:3])


def check_loki_projects(opener):
    """LOKI_PROJECTS against the project values Loki actually has -- a typo
    there leaves that project with no data, or with zeros, and nothing else
    says why. Over the last 30 days, as the collector's env discovery asks:
    Loki's default is 6 hours, and a quiet project would look missing."""
    end = int(time.time()) * 10 ** 9
    qs = urllib.parse.urlencode({"start": end - 30 * 86400 * 10 ** 9, "end": end})
    try:
        with opener.open(LOKI_URL + "/loki/api/v1/label/project/values?" + qs,
                         timeout=30) as r:
            known = json.loads(r.read().decode("utf-8")).get("data") or []
    except Exception as ex:
        soft("Could not list Loki's project values (%s) -- LOKI_PROJECTS not checked." % ex)
        return
    ok("Loki projects: %s" % (", ".join(known) or "(none)"))
    for p in LOKI_PROJECTS:
        if PROJECT_RE.match(p) and p not in known:
            blocker("collector", "LOKI_PROJECTS names '%s', but Loki has no such project. "
                                 "Loki has: %s" % (p, ", ".join(known) or "(none)"))


def preflight():
    section("Pre-flight")

    # --- 1. this agent -------------------------------------------------------
    info("agent: %s" % platform.platform())
    if sys.version_info >= MIN_PY:
        ok("Python %s (%s)" % (platform.python_version(), sys.executable))
    else:
        blocker("db", "Python %s is older than %d.%d." % ((platform.python_version(),) + MIN_PY))
    if DB_TRUSTED and os.name != "nt":
        soft("DB_TRUSTED_CONNECTION=true on a non-Windows agent means Kerberos: the agent "
             "account needs a ticket. SQL auth (the default) needs none.")

    # --- 2. network ------------------------------------------------------------
    try:
        sql_host, sql_port, instance = parse_server(DB_SERVER)
    except SystemExit as ex:
        blocker("db", str(ex))
        sql_host = None
    if sql_host is None:
        pass
    elif sql_port is None:
        if resolves(sql_host):
            ok("SQL Server host resolves: %s (named instance '%s' -- port comes from SQL "
               "Browser on UDP 1434, so no TCP probe)" % (sql_host, instance))
        else:
            blocker("db", "Cannot resolve SQL Server host '%s' from this agent." % sql_host)
    elif tcp_ok(sql_host, sql_port):
        ok("SQL Server reachable: %s:%d" % (sql_host, sql_port))
    else:
        blocker("db", "Cannot reach %s:%d from this agent -- wrong host, a firewall, or this "
                      "agent sits on a network without a route to SQL Server."
                % (sql_host, sql_port))

    # Loki problems don't stop the database setup, but the collector can't run
    # without Loki, so they fail the run rather than hiding in a yellow warning.
    if LOKI_PROJECTS:
        info("collector covers: %s%s" % (", ".join(LOKI_PROJECTS),
             "" if env("LOKI_PROJECTS").replace(",", "").strip()
             else "  (LOKI_PROJECTS unset -- LOKI_PROJECT alone)"))
    else:
        blocker("collector", "Set LOKI_PROJECTS (or LOKI_PROJECT) in gw-reports-secrets: the "
                             "comma list of Loki project label values the collector covers.")
    for p in LOKI_PROJECTS:
        if not PROJECT_RE.match(p):
            blocker("collector", "LOKI_PROJECTS entry '%s' is not a valid Loki project label "
                                 "value: use 1-64 of A-Z a-z 0-9 . _ -" % p)
    if not LOKI_URL:
        blocker("collector", "LOKI_URL is not set -- the collector cannot run.")
    else:
        u = urllib.parse.urlparse(LOKI_URL)
        lport = u.port or (443 if u.scheme == "https" else 80)
        # Through a proxy (BYPASS_PROXY=false) a direct TCP connect proves nothing
        # either way; the HTTP probe below uses the same route as the collector.
        reachable = True
        if BYPASS_PROXY:
            reachable = tcp_ok(u.hostname, lport)
            if reachable:
                ok("Loki reachable: %s:%d" % (u.hostname, lport))
            else:
                blocker("collector", "Cannot reach Loki at %s:%d from this agent."
                        % (u.hostname, lport))
        else:
            info("BYPASS_PROXY=false -- Loki is probed over HTTP through the proxy only.")
        if reachable:
            try:
                if not LOKI_VERIFY:
                    info("LOKI_VERIFY_TLS is false -- certificate validation disabled.")
                handlers = [urllib.request.HTTPSHandler(
                    context=loki_ssl_context(LOKI_VERIFY, LOKI_CA_BUNDLE, soft))]
                if BYPASS_PROXY:
                    handlers.append(urllib.request.ProxyHandler({}))
                opener = urllib.request.build_opener(*handlers)
                with opener.open(LOKI_URL + "/loki/api/v1/labels", timeout=30) as r:
                    labels = json.loads(r.read().decode("utf-8")).get("data") or []
                ok("Loki answered. %d label(s): %s" % (len(labels), ", ".join(labels[:8])))
                for needed in ("env", "project", "job"):
                    if needed not in labels:
                        blocker("collector", "Loki has no '%s' label -- the collector's selector "
                                             "will match nothing." % needed)
                if "project" in labels:
                    check_loki_projects(opener)
            except Exception as ex:
                if is_cert_error(ex):
                    blocker("collector", LOKI_CERT_HINT + " (%s)" % ex)
                else:
                    blocker("collector", "Loki HTTP probe failed: %s. Check BYPASS_PROXY." % ex)

    # --- 3. SQL Server client and handshake ----------------------------------
    try:
        p = get_sql()
        ok("pymssql %s -- carries its own SQL Server client, no ODBC driver needed"
           % getattr(p, "__version__", "?"))
    except SystemExit as ex:
        blocker("db", str(ex))
        return
    if sql_host and sql_port:
        # pymssql hangs indefinitely if a server accepts the connection and then
        # says nothing, so find that out here, with a timeout, first.
        answer = tds_probe(sql_host, sql_port)
        if answer == "silent":
            blocker("db", "%s:%d accepted the connection but never answered SQL Server's "
                          "handshake -- a firewall or proxy is swallowing the traffic, or it "
                          "isn't SQL Server. Connecting would hang, so stopping here."
                    % (sql_host, sql_port))
            return
        if answer == "answered":
            ok("SQL Server answered the handshake")

    # --- 4. database ---------------------------------------------------------
    global DB_ENCRYPT
    configured_encrypt = DB_ENCRYPT
    try:
        _database_checks()
    except SystemExit as ex:
        blocker("db", str(ex))
    finally:
        DB_ENCRYPT = configured_encrypt


def _database_checks():
    global DB_ENCRYPT
    try:
        version = str(scalar("SELECT @@VERSION") or "")
    except SystemExit as ex:
        first = str(ex)
        low = first.lower()
        # Retrying can only tell us something if encryption could be the cause.
        if (not DB_ENCRYPT or is_login_failure(first) or "4060" in first
                or "cannot open database" in low or "connection refused" in low):
            raise
        info("Could not connect with encryption on -- retrying once unencrypted "
             "(diagnosis only, this run only).")
        DB_ENCRYPT = False
        try:
            version = str(scalar("SELECT @@VERSION") or "")
        except SystemExit:
            raise SystemExit(first + "\n  -> Fails unencrypted too, so encryption is not the "
                             "cause.")
        blocker("db", first + "\n  -> Confirmed: it connects unencrypted -- as a default "
                      "SqlClient connection does -- but not encrypted. Set DB_ENCRYPT=false in "
                      "the variable group (the checks below ran that way), or enable TLS on "
                      "the server.")

    ok("Connected. %s" % version.split("\n")[0].strip())
    if "Microsoft SQL Server" not in version:
        blocker("db", "This is not Microsoft SQL Server. The schema and the ten dashboard "
                      "panels are T-SQL and need a dialect pass first.")
        return

    edition = scalar("SELECT CAST(SERVERPROPERTY('EngineEdition') AS INT)")
    product = scalar("SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(32))")
    if edition in (5, 8):
        ok("Azure SQL (engine edition %s) -- supports everything schema.sql uses" % edition)
    elif version_tuple(product) >= MIN_SQL:
        ok("SQL Server build %s (>= 2016 SP1)" % product)
    else:
        blocker("db", "SQL Server build %s is older than 2016 SP1 (13.0.4001), which "
                      "schema.sql needs for CREATE OR ALTER." % product)

    # '+' rather than CONCAT, which SQL Server 2008 lacks -- this line must not be
    # the thing that crashes after the version check has already said why.
    who = scalar("SELECT SUSER_NAME() + ' / ' + USER_NAME() + ' @ ' + DB_NAME()")
    info("identity: %s" % who)

    present = existing_objects()
    tables = [t for t in TABLES if t in present]
    views = [v for v in VIEWS if v in present]
    info("gw_login objects: %d of %d tables, %d of %d views%s" % (
        len(tables), len(TABLES), len(views), len(VIEWS),
        "" if present else "  (normal before 'schema' has run)"))

    missing_project = tables_missing_project(present)
    if missing_project:
        blocker("collector", upgrade_message(missing_project))
    elif tables:
        ok("tables have the project column")

    lacking_ddl = missing_ddl(present)
    if len(tables) < len(TABLES):
        # 'schema' is still to run, so a missing right is in the way.
        if lacking_ddl:
            soft(ddl_message(lacking_ddl))
        else:
            ok("can create the tables and views in dbo (needed by 'schema')")
    else:
        if len(views) < len(VIEWS):
            info("Only %d of %d views are visible to this account: either 'schema' stopped "
                 "partway (re-run it), or its CREATE/ALTER rights were revoked after 'schema', "
                 "which hides the views from it. The collector doesn't use them; Grafana reads "
                 "them with its own login." % (len(views), len(VIEWS)))
        if lacking_ddl and missing_project:
            # The upgrade can't wait for the next schema.sql change.
            soft(ddl_message(lacking_ddl))
        elif lacking_ddl:
            info("Re-running 'schema' -- only needed when schema.sql changes -- would need "
                 "%s:\n%s" % (", ".join(lacking_ddl), grant_lines(lacking_ddl)))
        else:
            ok("can re-run 'schema'%s" % (" -- which adds the project column"
                                          if missing_project else ""))

    # What grants.sql needs: create users, and grant on dbo objects. Only for
    # Grafana's read-only login -- the collector never needs it.
    if scalar("SELECT CASE WHEN HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','ALTER ANY USER') = 1 "
              "AND (HAS_PERMS_BY_NAME('dbo','SCHEMA','CONTROL') = 1 "
              "OR IS_ROLEMEMBER('db_securityadmin') = 1) THEN 1 ELSE 0 END") == 1:
        ok("can create database users and grant on dbo (needed by 'grants')")
    else:
        info("'grants' sets up Grafana's read-only login, which takes creating a database "
             "user -- a right this account doesn't have, and doesn't need. Skip 'grants'; a "
             "DBA runs section 2 of grants.sql when Grafana is connected.")
    if all(t in present for t, _ in COLLECTOR_DML):
        lacking = missing_dml()
        if lacking:
            blocker("collector", "The collector runs as '%s', which lacks: %s. A DBA must grant "
                                 "these (or run grants.sql with this login as CollectorLogin):\n%s"
                    % (DB_USER or who, ", ".join(lacking), grant_lines(dml_rights(lacking))))
        else:
            ok("the collector's login can read and write all three tables")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def finish():
    section("Done")
    if SOFT:
        print("%d warning(s):" % len(SOFT))
        for w in SOFT:
            print("  - %s" % w)
    if BLOCKERS:
        print("%d problem(s) that must be fixed:" % len(BLOCKERS))
        for kind, text in BLOCKERS:
            print("  - [%s] %s" % (kind, text))
        return 1
    print("No problems found.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--action", default="check",
                    choices=["check", "schema", "grants", "verify", "all"])
    ap.add_argument("--grafana-login", default="")
    ap.add_argument("--collector-login", default="")
    # Default: the sql/ folder next to this script's tools/ folder, wherever the
    # logins folder sits in the repo.
    ap.add_argument("--sql-root", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir, "sql"))
    args = ap.parse_args()
    # Pipeline parameters: 'none' (their default) means blank.
    args.grafana_login = param(args.grafana_login)
    args.collector_login = param(args.collector_login)

    if not DB_SERVER or not DB_NAME:
        raise SystemExit("DB_SERVER and DB_NAME must be set. Is the DB variable group "
                         "linked to this pipeline?")
    # Before touching anything, so 'all' can't stop halfway for want of it.
    if args.action in ("grants", "all") and not args.grafana_login:
        raise SystemExit("'%s' needs the Grafana read-only SQL login, and grafanaLogin is not set. "
                         "Create that login first; it must never be the collector's account."
                         % args.action)

    print("Action           : %s" % args.action)
    print("Database         : %s / %s" % (DB_SERVER, DB_NAME))
    print("Auth             : %s" % ("integrated" if DB_TRUSTED else "SQL login '%s'" % DB_USER))
    print("Encrypt          : %s%s" % (DB_ENCRYPT, "  (server certificate not verified)"
                                         if DB_ENCRYPT else ""))

    if args.action in ("check", "all"):
        preflight()
        if args.action == "all" and any(kind == "db" for kind, _ in BLOCKERS):
            print("\nDatabase problems above -- not attempting schema, grants or verify.")
            return finish()

    if args.action in ("schema", "all"):
        section("Apply schema")
        present = existing_objects()
        upgrade = tables_missing_project(present)
        project = existing_project()
        # Pasted into the SQL text, so never a value the collector would refuse.
        valid = bool(PROJECT_RE.match(project))
        if upgrade:
            if not valid:
                raise SystemExit(
                    "The login tables have no project column yet (%s). Adding it tags their rows "
                    "with the project they came from: LOKI_PROJECT, else the first LOKI_PROJECTS "
                    "entry. %s Set it in gw-reports-secrets, then re-run 'schema'."
                    % (", ".join(upgrade), "Neither is set." if not project else
                       "'%s' is not a valid project value (1-64 of A-Z a-z 0-9 . _ -)." % project))
            info("adding the project column to %s -- existing rows are tagged '%s'"
                 % (", ".join(upgrade), project))
        lacking_ddl = missing_ddl(present)
        if lacking_ddl:
            raise SystemExit(ddl_message(lacking_ddl))
        # schema.sql refuses an upgrade itself while ExistingProject is still its
        # placeholder, so the placeholder may stand when there is none to do.
        run_sql_file(os.path.join(args.sql_root, "schema.sql"),
                     overrides={"ExistingProject": project if valid else ""},
                     placeholder_ok=("ExistingProject",))
        present = existing_objects()
        still = tables_missing_project(present)
        if still:
            raise SystemExit("schema.sql ran but %s still lack(s) the project column."
                             % ", ".join(still))
        if upgrade:
            ok("project column added to %s" % ", ".join(upgrade))
            # 'all' ran check first; its blocker about this is now out of date.
            BLOCKERS[:] = [b for b in BLOCKERS if not b[1].startswith(NO_PROJECT_COLUMN)]
        missing = [n for n in EXPECTED_OBJECTS if n not in present]
        if missing:
            raise SystemExit("schema.sql ran but these objects are missing: %s" % ", ".join(missing))
        ok("all %d objects present: %s" % (len(present), ", ".join(present)))
        lacking = missing_dml()
        if lacking:
            soft("Tables created, but this login lacks %s -- creating a table in dbo does not "
                 "make you its owner. The collector needs them; a DBA runs this in %s:\n%s"
                 % (", ".join(lacking), DB_NAME, grant_lines(dml_rights(lacking))))
        revokes = explicit_ddl_grants()
        if revokes:
            info("Done with the CREATE/ALTER rights: the collector doesn't need them, and ALTER "
                 "on dbo reaches every object in it. After the DML rights above are in place, "
                 "a DBA can revoke them (re-grant only when schema.sql changes):\n%s"
                 % "\n".join(revokes))

    if args.action in ("grants", "all"):
        section("Apply grants")
        running_as = scalar("SELECT SUSER_NAME()")
        collector = args.collector_login or DB_USER or running_as
        if args.grafana_login.lower() == (collector or "").lower():
            raise SystemExit("Grafana and collector logins are the same account. Grafana "
                             "must be read-only: panels run ad-hoc SQL that any dashboard "
                             "editor can change, and these tables are the only copy of the "
                             "history.")
        self_is_collector = is_running_login(collector)
        if self_is_collector:
            info("collector login '%s' is the account running this, so grants.sql skips its "
                 "grants -- SQL Server refuses a grant to yourself. Checking it already has "
                 "them instead." % collector)
        run_sql_file(os.path.join(args.sql_root, "grants.sql"),
                     overrides={"CollectorLogin": collector,
                                "GrafanaLogin": args.grafana_login})
        if self_is_collector:
            lacking = missing_dml()
            if lacking:
                raise SystemExit(
                    "'%s' runs this pipeline and the collector, but lacks: %s. It cannot grant "
                    "these to itself -- a DBA runs this in %s:\n%s"
                    % (collector, ", ".join(lacking), DB_NAME, grant_lines(dml_rights(lacking))))
            ok("collector login '%s' already has every right it needs" % collector)

    if args.action in ("verify", "all"):
        section("Verify")
        # verify.sql reads the project column; say what to do rather than
        # surfacing "Invalid column name 'project'".
        missing_project = tables_missing_project(existing_objects())
        if missing_project:
            raise SystemExit(upgrade_message(missing_project))
        run_sql_file(os.path.join(args.sql_root, "verify.sql"), show_results=True)
        print("")
        info("Sections 3 (gaps) and 4 (per-user reconciliation) must be empty.")
        info("Before the first backfill, every section being empty is expected.")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
