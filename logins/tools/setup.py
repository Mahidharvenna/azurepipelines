#!/usr/bin/env python3
"""
One-shot bootstrap for the login-history database, run from the pipeline.

Does the setup that would otherwise need sqlcmd on someone's laptop:
pre-flight checks, schema, grants, and verification.

'check' reports EVERY problem it can find in one run rather than stopping at
the first -- each miss would otherwise cost a full pipeline round-trip. It
exits non-zero if anything would stop the collector, even when the database
side is fine.

Because it uses pyodbc rather than sqlcmd, it does three things sqlcmd does
client-side and the server knows nothing about:
  * split each file on GO      -- a batch separator, not T-SQL
  * expand :setvar / $(TOKEN)  -- a sqlcmd variable construct
  * surface PRINT as headings  -- pyodbc does not expose PRINT output

Configuration comes from environment variables set by the pipeline, so no
secret is ever passed as an argument (arguments are echoed in the build log).

Actions
-------
  check   pre-flight only. Touches nothing. The default, deliberately.
  schema  apply sql/schema.sql (idempotent)
  grants  apply sql/grants.sql  -- needs --grafana-login
  verify  run sql/verify.sql and print every result set
  all     check, schema, grants, verify -- in that order
"""

import os
import re
import sys
import json
import socket
import platform
import argparse
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gwcommon import (env, env_bool, harden_stdio, explain_import_error, choose_driver,
                      odbc_quote, parse_server, explain_connect_error, loki_ssl_context,
                      is_cert_error, LOKI_CERT_HINT, one_line)

harden_stdio()

MIN_PY = (3, 9)
# 13.0.4001 = SQL Server 2016 SP1, the first build with CREATE OR ALTER.
MIN_SQL = (13, 0, 4001)
EXPECTED_OBJECTS = sorted([
    "gw_login_daily", "gw_login_user_daily", "gw_login_collector_run",   # tables
    "gw_login_monthly", "gw_login_monthly_users", "gw_login_freshness",  # views
])


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
DB_SERVER = env("DB_SERVER")
DB_NAME = env("DB_NAME")
DB_USER = env("DB_USER")
DB_PASS = env("DB_PASS")
DB_DRIVER_REQUESTED = env("DB_ODBC_DRIVER")       # blank = newest installed
DB_TRUSTED = env_bool("DB_TRUSTED_CONNECTION", False)
DB_ENCRYPT = env_bool("DB_ENCRYPT", True)
DB_TRUST_CERT = env_bool("DB_TRUST_SERVER_CERT", False)

LOKI_URL = env("LOKI_URL").rstrip("/")
LOKI_VERIFY = env_bool("LOKI_VERIFY_TLS", True)
LOKI_CA_BUNDLE = env("LOKI_CA_BUNDLE").strip()
BYPASS_PROXY = env_bool("BYPASS_PROXY", True)


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
_pyodbc = None
_driver = None


def get_pyodbc():
    """Import pyodbc lazily, so 'check' can still report everything else when
    the ODBC stack is missing."""
    global _pyodbc
    if _pyodbc is None:
        try:
            import pyodbc
        except ImportError as ex:
            raise SystemExit(explain_import_error(ex))
        _pyodbc = pyodbc
    return _pyodbc


def get_driver():
    global _driver
    if _driver is None:
        driver, problem = choose_driver(get_pyodbc().drivers(), DB_DRIVER_REQUESTED)
        if problem:
            raise SystemExit(problem)
        _driver = driver
    return _driver


def connection_string():
    parts = ["DRIVER={%s}" % get_driver(), "SERVER=%s" % DB_SERVER, "DATABASE=%s" % DB_NAME]
    if DB_TRUSTED:
        parts.append("Trusted_Connection=yes")
    else:
        if not DB_USER:
            raise SystemExit("Set DB_USER/DB_PASS, or DB_TRUSTED_CONNECTION=true.")
        parts += ["UID=%s" % odbc_quote(DB_USER), "PWD=%s" % odbc_quote(DB_PASS)]
    parts.append("Encrypt=yes" if DB_ENCRYPT else "Encrypt=no")
    if DB_TRUST_CERT:
        parts.append("TrustServerCertificate=yes")
    parts.append("APP=GW-Login-Setup")
    return ";".join(parts) + ";"


def connect():
    p = get_pyodbc()
    try:
        cn = p.connect(connection_string(), timeout=30)
    except p.Error as ex:
        raise SystemExit(explain_connect_error(ex, DB_SERVER, DB_NAME))
    cn.autocommit = True          # DDL and GRANT; no transaction to manage
    return cn


def query(sql):
    cn = connect()
    try:
        cur = cn.cursor()
        try:
            cur.execute(sql)
        except get_pyodbc().Error as ex:
            raise SystemExit("Query failed: %s\n  query: %s" % (ex, " ".join(sql.split())[:160]))
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


def expand_setvar(text, overrides=None):
    """Resolve :setvar declarations and $(TOKEN) references, then drop the
    :setvar lines. Overrides (from the command line) win over the file."""
    variables = {}
    for line in text.split("\n"):
        m = SETVAR_RE.match(line)
        if m:
            variables[m.group(1)] = m.group(2)
    for k, v in (overrides or {}).items():
        if v:
            variables[k] = v
    for k, v in variables.items():
        if v.startswith("your_"):
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

    pyodbc gives no access to PRINT output, so the PRINT statements -- which is
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


def run_sql_file(path, overrides=None, show_results=False):
    if not os.path.isfile(path):
        raise SystemExit("SQL file not found: %s" % path)
    info("file: %s" % path)
    text = expand_setvar(open(path, encoding="utf-8-sig").read(), overrides)
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
            except Exception as ex:
                if is_cert_error(ex):
                    blocker("collector", LOKI_CERT_HINT + " (%s)" % ex)
                else:
                    blocker("collector", "Loki HTTP probe failed: %s. Check BYPASS_PROXY." % ex)

    # --- 3. ODBC stack -----------------------------------------------------------
    try:
        p = get_pyodbc()
        ok("pyodbc %s" % getattr(p, "version", "?"))
        installed = p.drivers()
        info("ODBC drivers on this agent: %s" % (", ".join(installed) or "none"))
        driver = get_driver()
        ok("using %s%s" % (driver, "" if DB_DRIVER_REQUESTED else " (newest installed)"))
    except SystemExit as ex:
        blocker("db", str(ex))
        return                          # nothing below can run without a driver

    # --- 4. database ---------------------------------------------------------
    global DB_TRUST_CERT
    configured_trust = DB_TRUST_CERT
    try:
        _database_checks()
    except SystemExit as ex:
        blocker("db", str(ex))
    finally:
        DB_TRUST_CERT = configured_trust


def _database_checks():
    global DB_TRUST_CERT
    try:
        version = str(scalar("SELECT @@VERSION") or "")
    except SystemExit as ex:
        first = str(ex)
        if DB_TRUST_CERT or "TLS:" not in first:
            raise
        # A certificate problem hides everything behind it -- credentials,
        # version, rights -- and would cost a second run to discover. Retry once
        # without validating the certificate, for this diagnosis only.
        info("TLS failed -- retrying once with TrustServerCertificate=yes (diagnosis only, "
             "this run only) to check everything behind it.")
        DB_TRUST_CERT = True
        try:
            version = str(scalar("SELECT @@VERSION") or "")
        except SystemExit as ex2:
            if "TLS:" in str(ex2):
                raise SystemExit(first + "\n  -> TrustServerCertificate=yes fails the same way, "
                                 "so DB_TRUST_SERVER_CERT will NOT fix this: it is a TLS protocol "
                                 "mismatch (e.g. a SQL Server without TLS 1.2), not certificate trust.")
            blocker("db", first)
            raise
        blocker("db", first + "\n  -> Confirmed: it connects with TrustServerCertificate=yes, and "
                      "the checks below ran that way. Set DB_TRUST_SERVER_CERT=true, or install "
                      "the issuing CA on the agent.")

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

    # What schema.sql actually needs: creating in dbo takes ALTER on the schema
    # as well as CREATE TABLE, and the views need CREATE VIEW.
    if scalar("SELECT CASE WHEN HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE TABLE') = 1 "
              "AND HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE VIEW') = 1 "
              "AND HAS_PERMS_BY_NAME('dbo','SCHEMA','ALTER') = 1 THEN 1 ELSE 0 END") == 1:
        ok("can create tables and views in dbo (needed by 'schema')")
    else:
        soft("This account cannot create tables and views in dbo (needs CREATE TABLE, "
             "CREATE VIEW and ALTER on SCHEMA::dbo). A DBA must run schema.sql once.")
    # What grants.sql needs: create users, and grant on dbo objects.
    if scalar("SELECT CASE WHEN HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','ALTER ANY USER') = 1 "
              "AND (HAS_PERMS_BY_NAME('dbo','SCHEMA','CONTROL') = 1 "
              "OR IS_ROLEMEMBER('db_securityadmin') = 1) THEN 1 ELSE 0 END") == 1:
        ok("can create database users and grant on dbo (needed by 'grants')")
    else:
        soft("This account cannot create database users and grant on dbo, so 'grants' will "
             "fail -- a DBA must run grants.sql.")

    present = existing_objects()
    info("gw_login objects present: %d of %d%s" % (
        len(present), len(EXPECTED_OBJECTS),
        "" if present else "  (normal before 'schema' has run)"))
    if all(t in present for t, _ in COLLECTOR_DML):
        lacking = missing_dml()
        if lacking:
            blocker("collector", "The collector runs as '%s', which lacks: %s. A DBA must grant "
                                 "these (or run grants.sql with this login as CollectorLogin)."
                    % (DB_USER or who, ", ".join(lacking)))
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
    ap.add_argument("--sql-root", default="logins/sql")
    args = ap.parse_args()

    if not DB_SERVER or not DB_NAME:
        raise SystemExit("DB_SERVER and DB_NAME must be set. Is the DB variable group "
                         "linked to this pipeline?")

    print("Action           : %s" % args.action)
    print("Database         : %s / %s" % (DB_SERVER, DB_NAME))
    print("Auth             : %s" % ("integrated" if DB_TRUSTED else "SQL login '%s'" % DB_USER))
    print("Encrypt          : %s  (TrustServerCertificate=%s)" % (DB_ENCRYPT, DB_TRUST_CERT))

    if args.action in ("check", "all"):
        preflight()
        if args.action == "all" and any(kind == "db" for kind, _ in BLOCKERS):
            print("\nDatabase problems above -- not attempting schema, grants or verify.")
            return finish()

    if args.action in ("schema", "all"):
        section("Apply schema")
        run_sql_file(os.path.join(args.sql_root, "schema.sql"))
        present = existing_objects()
        missing = [n for n in EXPECTED_OBJECTS if n not in present]
        if missing:
            raise SystemExit("schema.sql ran but these objects are missing: %s" % ", ".join(missing))
        ok("all %d objects present: %s" % (len(present), ", ".join(present)))
        lacking = missing_dml()
        if lacking:
            soft("Tables created, but this login lacks %s on them -- creating a table in dbo "
                 "does not make you its owner. 'grants' will stop on this; a DBA must grant them."
                 % ", ".join(lacking))

    if args.action in ("grants", "all"):
        section("Apply grants")
        running_as = scalar("SELECT SUSER_NAME()")
        collector = args.collector_login or DB_USER or running_as
        if not args.grafana_login:
            raise SystemExit("grants needs --grafana-login. Create a read-only SQL login "
                             "for Grafana first; it must never be the collector's account.")
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
                    "these to itself -- a DBA must, e.g. add it to db_datareader and "
                    "db_datawriter, or GRANT them on the three gw_login tables."
                    % (collector, ", ".join(lacking)))
            ok("collector login '%s' already has every right it needs" % collector)

    if args.action in ("verify", "all"):
        section("Verify")
        run_sql_file(os.path.join(args.sql_root, "verify.sql"), show_results=True)
        print("")
        info("Sections 3 (gaps) and 4 (per-user reconciliation) must be empty.")
        info("Before the first backfill, every section being empty is expected.")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
