#!/usr/bin/env python3
"""
One-shot bootstrap for the login-history database, run from the pipeline.

Does the setup that would otherwise need sqlcmd on someone's laptop:
pre-flight checks, schema, grants, and verification.

Because it uses pyodbc rather than sqlcmd, it has to do three things sqlcmd
does client-side and the server knows nothing about:
  * split each file on GO      -- a batch separator, not T-SQL
  * expand :setvar / $(TOKEN)  -- a sqlcmd variable construct
  * surface PRINT as headings  -- pyodbc does not expose PRINT output, so the
                                  PRINT lines are lifted out and used to label
                                  the result set that follows

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
import ssl
import sys
import json
import socket
import argparse
import urllib.parse
import urllib.request

try:
    import pyodbc
except ImportError:
    raise SystemExit(
        "pyodbc is not installed. The pipeline installs it; if you are running\n"
        "this by hand:  python -m pip install pyodbc\n"
        "It also needs Microsoft's ODBC Driver for SQL Server on the machine."
    )


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def env(name, default=""):
    v = os.environ.get(name, "")
    # A variable not defined in the group arrives as the literal "$(NAME)".
    if v.strip() == "" or (v.strip().startswith("$(") and v.strip().endswith(")")):
        return default
    return v


def env_bool(name, default=False):
    v = env(name).strip().lower()
    return default if v == "" else v in ("true", "1", "yes", "y")


SOFT = []


def section(text):
    print("\n" + "=" * 72)
    print("  " + text)
    print("=" * 72, flush=True)


def ok(text):    print("  [ OK ] " + text, flush=True)
def info(text):  print("         " + text, flush=True)


def soft(text):
    SOFT.append(text)
    print("##vso[task.logissue type=warning]" + text, flush=True)


DB_SERVER = env("DB_SERVER")
DB_NAME = env("DB_NAME")
DB_USER = env("DB_USER")
DB_PASS = env("DB_PASS")
DB_DRIVER = env("DB_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
DB_TRUSTED = env_bool("DB_TRUSTED_CONNECTION", False)
DB_ENCRYPT = env_bool("DB_ENCRYPT", True)
DB_TRUST_CERT = env_bool("DB_TRUST_SERVER_CERT", False)

LOKI_URL = env("LOKI_URL").rstrip("/")
LOKI_VERIFY = env_bool("LOKI_VERIFY_TLS", True)
BYPASS_PROXY = env_bool("BYPASS_PROXY", True)
LOKI_PROJECT = env("LOKI_PROJECT", "myproject")


def connection_string():
    parts = ["DRIVER={%s}" % DB_DRIVER, "SERVER=%s" % DB_SERVER, "DATABASE=%s" % DB_NAME]
    if DB_TRUSTED:
        parts.append("Trusted_Connection=yes")
    else:
        if not DB_USER:
            raise SystemExit("Set DB_USER/DB_PASS, or DB_TRUSTED_CONNECTION=true.")
        parts += ["UID=%s" % DB_USER, "PWD=%s" % DB_PASS]
    parts.append("Encrypt=yes" if DB_ENCRYPT else "Encrypt=no")
    if DB_TRUST_CERT:
        parts.append("TrustServerCertificate=yes")
    parts.append("APP=GW-Login-Setup")
    return ";".join(parts) + ";"


def connect():
    cn = pyodbc.connect(connection_string(), timeout=30)
    cn.autocommit = True          # DDL and GRANT; no transaction to manage
    return cn


def scalar(query):
    cn = connect()
    try:
        cur = cn.cursor()
        cur.execute(query)
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cn.close()


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


def preflight():
    section("Pre-flight")

    # --- 1. TCP reachability -------------------------------------------------
    sql_host, sql_port = DB_SERVER, 1433
    if "," in DB_SERVER:
        sql_host, _, p = DB_SERVER.partition(",")
        sql_port = int(p)
    elif ":" in DB_SERVER and DB_SERVER.count(":") == 1:
        sql_host, _, p = DB_SERVER.partition(":")
        sql_port = int(p)

    if tcp_ok(sql_host, sql_port):
        ok("SQL Server reachable: %s:%d" % (sql_host, sql_port))
    else:
        raise SystemExit("Cannot reach %s:%d from this agent. Wrong host, a firewall, "
                         "or the collector needs a different agent pool." % (sql_host, sql_port))

    if LOKI_URL:
        u = urllib.parse.urlparse(LOKI_URL)
        lport = u.port or (443 if u.scheme == "https" else 80)
        if tcp_ok(u.hostname, lport):
            ok("Loki reachable: %s:%d" % (u.hostname, lport))
        else:
            soft("Cannot reach Loki at %s:%d. The collector will fail even though "
                 "the database is fine." % (u.hostname, lport))
    else:
        soft("LOKI_URL is not set -- skipping the Loki probe.")

    # --- 2. Loki actually answers -------------------------------------------
    if LOKI_URL:
        try:
            handlers = []
            if BYPASS_PROXY:
                handlers.append(urllib.request.ProxyHandler({}))
            if not LOKI_VERIFY:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                handlers.append(urllib.request.HTTPSHandler(context=ctx))
                info("LOKI_VERIFY_TLS is false -- certificate validation disabled.")
            opener = urllib.request.build_opener(*handlers)
            with opener.open(LOKI_URL + "/loki/api/v1/labels", timeout=30) as r:
                labels = json.loads(r.read().decode("utf-8")).get("data") or []
            ok("Loki answered. %d label(s): %s" % (len(labels), ", ".join(labels[:8])))
            for needed in ("env", "project", "job"):
                if needed not in labels:
                    soft("Loki has no '%s' label -- the collector's selector will match "
                         "nothing." % needed)
        except Exception as ex:
            soft("Loki HTTP probe failed: %s. Check TLS trust or BYPASS_PROXY." % ex)

    # --- 3. Database engine and rights ---------------------------------------
    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    if drivers:
        ok("ODBC drivers: %s" % "; ".join(drivers))
        if DB_DRIVER not in drivers:
            soft("DB_ODBC_DRIVER is '%s' but that is not installed. Set it to one of "
                 "the above." % DB_DRIVER)
    else:
        raise SystemExit("No SQL Server ODBC driver installed. Install Microsoft's "
                         "ODBC Driver 17 or 18 on this agent.")

    version = str(scalar("SELECT @@VERSION") or "")
    first = version.split("\n")[0].strip()
    ok("Connected. %s" % first)
    if "Microsoft SQL Server" not in version:
        raise SystemExit("This is not Microsoft SQL Server. The schema and the ten "
                         "dashboard panels are T-SQL and need a dialect pass first.")

    who = scalar("SELECT CONCAT(SUSER_NAME(), ' / ', USER_NAME(), ' @ ', DB_NAME())")
    info("identity: %s" % who)

    if scalar("SELECT HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE TABLE')") == 1:
        ok("account can CREATE TABLE in [%s]" % DB_NAME)
    else:
        soft("This account cannot CREATE TABLE in [%s]. A DBA must run schema.sql "
             "once; afterwards it only needs the DML in grants.sql." % DB_NAME)

    n = scalar("SELECT COUNT(*) FROM sys.objects WHERE name LIKE 'gw_login%'")
    info("existing gw_login* objects: %s  (7 once schema.sql has run)" % n)

    # --- 4. Collector prerequisites -----------------------------------------
    ok("Python: %s" % sys.version.split()[0])
    if sys.version_info < (3, 7):
        soft("Python %s is older than 3.7, which the collector needs." % sys.version.split()[0])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
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
        raise SystemExit("DB_SERVER and DB_NAME must be set. Is the variable group "
                         "linked to this pipeline?")

    collector = args.collector_login or DB_USER

    print("Action           : %s" % args.action)
    print("Database         : %s / %s" % (DB_SERVER, DB_NAME))
    print("Auth             : %s" % ("Windows integrated" if DB_TRUSTED
                                     else "SQL login '%s'" % DB_USER))
    print("Encrypt          : %s  (TrustServerCertificate=%s)" % (DB_ENCRYPT, DB_TRUST_CERT))

    if args.action in ("check", "all"):
        preflight()

    if args.action in ("schema", "all"):
        section("Apply schema")
        run_sql_file(os.path.join(args.sql_root, "schema.sql"))
        n = scalar("SELECT COUNT(*) FROM sys.objects WHERE name LIKE 'gw_login%'")
        if n < 7:
            raise SystemExit("Expected 7 gw_login* objects after schema.sql, found %s." % n)
        ok("%s objects present" % n)

    if args.action in ("grants", "all"):
        section("Apply grants")
        if not args.grafana_login:
            raise SystemExit("grants needs --grafana-login. Create a read-only SQL login "
                             "for Grafana first; it must never be the collector's account.")
        if args.grafana_login.lower() == (collector or "").lower():
            raise SystemExit("Grafana and collector logins are the same account. Grafana "
                             "must be read-only: panels run ad-hoc SQL that any dashboard "
                             "editor can change, and these tables are the only copy of the "
                             "history.")
        run_sql_file(os.path.join(args.sql_root, "grants.sql"),
                     overrides={"CollectorLogin": collector,
                                "GrafanaLogin": args.grafana_login})

    if args.action in ("verify", "all"):
        section("Verify")
        run_sql_file(os.path.join(args.sql_root, "verify.sql"), show_results=True)
        print("")
        info("Sections 3 (gaps) and 4 (per-user reconciliation) must be empty.")
        info("Before the first backfill, every section being empty is expected.")

    section("Done")
    if SOFT:
        print("Completed with %d warning(s):" % len(SOFT))
        for w in SOFT:
            print("  - %s" % w)
    else:
        print("No warnings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
