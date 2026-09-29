#!/usr/bin/env python3
"""
Login history collector: Loki -> SQL Server.

Loki retains ~30 days. This runs daily, well inside that window, and copies
per-day login counts (and the usernames behind them) into SQL Server, which
keeps them forever. Grafana then reads SQL Server instead of Loki, so a
dashboard can show years of history.

Shares its conventions with reports/monthly_report.py -- same env() handling,
same selector, same timezone treatment, same LOGIN_USER_REGEX -- so the stored
numbers reconcile with the emailed report.

Standard library only, except pymssql for the database (its wheel carries its
own SQL Server client -- no ODBC driver needed).

Modes
-----
  normal    re-collect the last LOOKBACK_DAYS complete days. Re-collecting
            rather than only doing yesterday is deliberate: the upsert is
            idempotent, so a missed run or a late-arriving log self-heals on
            the next run with nobody intervening.

  backfill  set BACKFILL_START (and optionally BACKFILL_END) to load a range.
            Use once at go-live to seed whatever history Loki still holds.

Projects
--------
One Loki holds several projects (its `project` label), and environment names
repeat across them, so every stored row -- and every run row -- carries its
project.

  LOKI_PROJECTS  comma list of the Loki project label values a run covers,
                 e.g. project_a,project_b. Unset = LOKI_PROJECT alone, so a
                 single-project setup keeps working unchanged.
  PROJECT        the pipeline's 'project' parameter. all (the default, and
                 what scheduled runs get) = every LOKI_PROJECTS entry, in
                 order; otherwise one label value -- e.g. to backfill a new
                 project before LOKI_PROJECTS lists it.
"""

import os
import re
import sys
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
from gwcommon import (env, env_bool, harden_stdio, explain_import_error, parse_server,
                      explain_connect_error, loki_ssl_context, is_cert_error, LOKI_CERT_HINT,
                      connect_sql, apply_session_options, tds_probe, param)

harden_stdio()

# pymssql is imported only when the database is actually used, so a dry run
# needs nothing but the standard library.
_sql = None


def get_sql():
    global _sql
    if _sql is None:
        try:
            import pymssql
        except ImportError as ex:
            raise SystemExit(explain_import_error(ex))
        _sql = pymssql
    return _sql


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def split_list(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def warn(msg):
    print("##vso[task.logissue type=warning]" + msg)


def log(msg):
    print(msg, flush=True)


LOKI_URL = env("LOKI_URL", "https://your-loki-host.example.com:3100").rstrip("/")
LOKI_PROJECT = env("LOKI_PROJECT", "myproject").strip()
# Falls back to the single LOKI_PROJECT so a setup from before there were
# several projects keeps working.
LOKI_PROJECTS = split_list(env("LOKI_PROJECTS")) or [LOKI_PROJECT]
PROJECT = param(env("PROJECT"))
VERIFY_TLS = env_bool("LOKI_VERIFY_TLS", True)
LOKI_CA_BUNDLE = env("LOKI_CA_BUNDLE").strip()
BYPASS_PROXY = env_bool("BYPASS_PROXY", True)
LOG_LIMIT = int(env("LOKI_LOG_LIMIT", "5000"))
HTTP_TIMEOUT = int(env("LOKI_HTTP_TIMEOUT", "180"))

ENVS = split_list(env("ENVS", "DEV1"))
# ALL = ask Loki, per project. An explicit list applies to every project.
DISCOVER_ENVS = len(ENVS) == 1 and ENVS[0].upper() == "ALL"
ENVS_EXCLUDE = [e.upper() for e in split_list(env("ENVS_EXCLUDE"))]
PRODUCTS = [p.lower() for p in split_list(env("PRODUCTS", "pc"))]

LOOKBACK_DAYS = int(env("LOOKBACK_DAYS", "7"))
RETENTION_DAYS = int(env("LOKI_RETENTION_DAYS", "30"))
# Pipeline parameters: 'none' (their default) means blank.
BACKFILL_START = param(env("BACKFILL_START"))
BACKFILL_END = param(env("BACKFILL_END"))
DRY_RUN = env_bool("DRY_RUN", False)
ALLOW_ZERO_OVERWRITE = env_bool("ALLOW_ZERO_OVERWRITE", False)
STORE_USERNAMES = env_bool("STORE_USERNAMES", True)
BUILD_ID = env("BUILD_ID")[:64] or None

USER_REGEX = env("LOGIN_USER_REGEX",
                 r"(?i)User\s+Login\s*[:=\-]?\s*(?P<user>[A-Za-z0-9._\\@-]+)")
# LOGIN_USER_REGEX uses .NET-style (?<user>...); accept it by rewriting to Python's.
USER_REGEX = USER_REGEX.replace("(?<user>", "(?P<user>")
try:
    USER_RE = re.compile(USER_REGEX)
except re.error as ex:
    raise SystemExit("LOGIN_USER_REGEX is not a valid regex: %s" % ex)

# Usernames the regex could not extract are stored under this sentinel so the
# per-user rows still sum to the event count. Excluded from distinct_users.
UNKNOWN_USER = "(unparsed)"

# --- database ---
DB_SERVER = env("DB_SERVER")
DB_NAME = env("DB_NAME")
DB_USER = env("DB_USER")
DB_PASS = env("DB_PASS")
DB_SCHEMA = env("DB_SCHEMA", "dbo")
DB_TRUSTED = env_bool("DB_TRUSTED_CONNECTION", False)
DB_ENCRYPT = env_bool("DB_ENCRYPT", True)
DB_TIMEOUT = int(env("DB_TIMEOUT", "30"))

# Schema/table names cannot be bound as parameters, so they are interpolated --
# validate rather than trusting the variable group.
if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", DB_SCHEMA):
    raise SystemExit("DB_SCHEMA is not a valid SQL identifier: %r" % DB_SCHEMA)
if not DRY_RUN:
    for key, val in (("DB_SERVER", DB_SERVER), ("DB_NAME", DB_NAME)):
        if not val:
            raise SystemExit("%s is not set. Add it to the variable group." % key)

# Which projects this run collects. A value goes inside a LogQL selector's
# double quotes and into a VARCHAR(64) column, so only characters that need
# escaping in neither are allowed.
PROJECT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PROJECTS = LOKI_PROJECTS if PROJECT.lower() in ("", "all") else [PROJECT]
PROJECTS = list(dict.fromkeys(PROJECTS))        # a repeated entry would collect twice
if not PROJECTS:
    raise SystemExit("Set LOKI_PROJECTS (comma list of Loki project label values) or "
                     "LOKI_PROJECT in the variable group -- there is no project to collect.")
for _p in PROJECTS:
    if not PROJECT_RE.match(_p):
        raise SystemExit("Project %r is not a valid Loki project label value: use 1-64 of "
                         "A-Z a-z 0-9 . _ -  Check the pipeline's Project parameter and "
                         "LOKI_PROJECTS." % _p)

# pclogs is the common case; BC/CC/CM are guesses. Override from the variable
# group: PRODUCT_JOBS / PRODUCT_FRAGS as comma lists of comp=value,
# e.g. PRODUCT_JOBS=cm=ablogs  PRODUCT_FRAGS=cm=ab
PRODUCT_META = {
    "pc": {"job": "pclogs", "frag": "pc", "label": "PolicyCenter"},
    "bc": {"job": "bclogs", "frag": "bc", "label": "BillingCenter"},
    "cc": {"job": "cclogs", "frag": "cc", "label": "ClaimCenter"},
    "cm": {"job": "cmlogs", "frag": "cm", "label": "ContactManager"},
}


def apply_overrides(raw, key):
    for pair in split_list(raw):
        if "=" in pair:
            comp, value = pair.split("=", 1)
            comp = comp.strip().lower()
            if comp in PRODUCT_META:
                PRODUCT_META[comp][key] = value.strip()


apply_overrides(env("PRODUCT_JOBS"), "job")
apply_overrides(env("PRODUCT_FRAGS"), "frag")

for p in PRODUCTS:
    if p not in PRODUCT_META:
        raise SystemExit("Unknown product '%s' in PRODUCTS. Expected any of: pc, bc, cc, cm." % p)


# --------------------------------------------------------------------------
# timezone -- must match monthly_report.py, or the dashboard and the emailed
# report will bucket the same login into different days.
# --------------------------------------------------------------------------
REPORT_TZ = env("REPORT_TIMEZONE")
_EASTERN = {"eastern standard time", "america/new_york", "et", "est", "edt", "eastern"}


def _second_sunday(year, month):
    d = datetime(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7) + timedelta(days=7)


def _first_sunday(year, month):
    d = datetime(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7)


def utc_offset(naive_local):
    if not REPORT_TZ:
        return timedelta(0)
    if REPORT_TZ.strip().lower() in _EASTERN:
        # EDT (UTC-4) from 2nd Sun Mar 02:00 to 1st Sun Nov 02:00, else EST (UTC-5).
        y = naive_local.year
        start = _second_sunday(y, 3).replace(hour=2)
        end = _first_sunday(y, 11).replace(hour=2)
        return timedelta(hours=-4) if start <= naive_local < end else timedelta(hours=-5)
    try:
        from zoneinfo import ZoneInfo
        return naive_local.replace(tzinfo=ZoneInfo(REPORT_TZ)).utcoffset()
    except Exception:
        warn("Unknown REPORT_TIMEZONE '%s' -- days are bucketed in UTC." % REPORT_TZ)
        return timedelta(0)


def to_utc(naive_local):
    return naive_local - utc_offset(naive_local)


def from_utc(utc_naive):
    approx = utc_naive + utc_offset(utc_naive)
    return utc_naive + utc_offset(approx)


TZ_LABEL = env("REPORT_TZ_LABEL", REPORT_TZ or "UTC")


def tz_rule():
    """How REPORT_TIMEZONE is being resolved. The built-in Eastern rules behave
    identically on every OS; any other name goes through zoneinfo, which on a
    Windows agent without the 'tzdata' package falls back to UTC -- so the
    report and this collector could bucket days differently."""
    if not REPORT_TZ:
        return "UTC"
    if REPORT_TZ.strip().lower() in _EASTERN:
        return "built-in Eastern rules"
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(REPORT_TZ)
        return "zoneinfo"
    except Exception:
        return "UNRESOLVED -- falling back to UTC"


def today_local():
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return (from_utc(now_utc) if REPORT_TZ else now_utc).date()


# --------------------------------------------------------------------------
# loki
# --------------------------------------------------------------------------
def _unix_ns(dt):
    return int((dt - datetime(1970, 1, 1)).total_seconds()) * 1_000_000_000


if not VERIFY_TLS:
    log("LOKI_VERIFY_TLS is false -- certificate validation disabled for this run.")
_handlers = [urllib.request.HTTPSHandler(
    context=loki_ssl_context(VERIFY_TLS, LOKI_CA_BUNDLE, lambda m: warn(m)))]
if BYPASS_PROXY:
    _handlers.append(urllib.request.ProxyHandler({}))
_opener = urllib.request.build_opener(*_handlers)


def loki_get(path, params, timeout=None):
    url = LOKI_URL + path + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(1, 4):
        try:
            with _opener.open(url, timeout=timeout or HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as ex:
            last = ex
            if attempt < 3:
                time.sleep(2 ** attempt)
    if last is not None and is_cert_error(last):
        raise RuntimeError("%s: %s -- %s" % (path, last, LOKI_CERT_HINT))
    raise RuntimeError("%s: %s" % (path, last))


def selector(project, job, env_label, frag):
    return '{project="%s", job="%s", env="%s", filename=~".*%s.log"}' % (
        project, job, env_label, frag)


def fetch_day_events(project, day_local, env_label, product):
    """
    Every login event for one project / local calendar day / env / product,
    as a list of (local_datetime, username).

    Pages through Loki rather than warning about truncation. The report can
    afford to warn and move on -- it runs again next month. This table is the
    only permanent copy, so a silently short day would be wrong forever.
    """
    meta = PRODUCT_META[product]
    sel = "%s |= `User Login`" % selector(project, meta["job"], env_label, meta["frag"])

    start_utc = to_utc(datetime.combine(day_local, datetime.min.time()))
    end_utc = to_utc(datetime.combine(day_local + timedelta(days=1), datetime.min.time()))

    events = []
    unparsed = []
    cursor = _unix_ns(start_utc)
    end_ns = _unix_ns(end_utc)
    guard = 0

    while cursor < end_ns:
        guard += 1
        if guard > 1000:
            raise RuntimeError("Paging did not terminate for %s %s %s/%s" % (
                project, day_local, env_label, product))
        r = loki_get("/loki/api/v1/query_range", {
            "query": sel, "start": str(cursor), "end": str(end_ns),
            "limit": str(LOG_LIMIT), "direction": "forward",
        })
        rows = []
        for stream in r["data"]["result"]:
            for ts, line in stream["values"]:
                rows.append((int(ts), line))
        if not rows:
            break
        rows.sort(key=lambda x: x[0])
        for ts_ns, line in rows:
            when_utc = datetime.fromtimestamp(ts_ns / 1e9, timezone.utc).replace(tzinfo=None)
            when = from_utc(when_utc) if REPORT_TZ else when_utc
            m = USER_RE.search(line)
            user = m.group("user") if (m and m.groupdict().get("user")) else ""
            if not user:
                user = UNKNOWN_USER
                if len(unparsed) < 3:
                    unparsed.append(line)
            events.append((when, user))
        if len(rows) < LOG_LIMIT:
            break
        cursor = rows[-1][0] + 1          # resume just past the last entry

    if unparsed:
        warn("%s %s %s/%s : some lines did not match LOGIN_USER_REGEX -- stored as %s." % (
            project, day_local, env_label, product, UNKNOWN_USER))
        for smp in unparsed:
            log("    " + smp)

    return events


def loki_projects():
    """The project label values Loki has seen in the last 30 days, or None if
    it can't say. Same window as discover_envs(): Loki's default is 6 hours,
    which would miss a quiet project."""
    end_utc = to_utc(datetime.combine(today_local(), datetime.min.time()))
    start_utc = end_utc - timedelta(days=min(RETENTION_DAYS, 30))
    try:
        r = loki_get("/loki/api/v1/label/project/values",
                     {"start": str(_unix_ns(start_utc)), "end": str(_unix_ns(end_utc))},
                     timeout=60)
        return {v for v in (r.get("data") or []) if v}
    except Exception as ex:
        warn("Could not list Loki's projects (%s) -- the project names are not checked." % ex)
        return None


def check_projects(projects):
    """Stop on a project Loki doesn't have: every day of it would be stored as
    zero logins under a misspelt name, and the dashboard would list it. A
    difference only in case is corrected to Loki's spelling."""
    known = loki_projects()
    if not known:
        return projects
    checked = []
    for p in projects:
        if p in known:
            checked.append(p)
            continue
        same = [k for k in known if k.lower() == p.lower()]
        if len(same) == 1:
            log("NOTE: project '%s' -> '%s' (Loki's spelling)." % (p, same[0]))
            checked.append(same[0])
            continue
        raise SystemExit("Loki has no project '%s'. Loki has: %s. Check the pipeline's Project "
                         "value and LOKI_PROJECTS." % (p, ", ".join(sorted(known))))
    return list(dict.fromkeys(checked))


def discover_envs(project):
    """ENVS=ALL -- ask Loki which env labels exist in this project.

    The 'query' selector scopes the answer to the project. Without it Loki
    lists every project's envs, and each project stored a zero row per day for
    environments it doesn't have. A Loki too old to support the parameter
    ignores it, which degrades to that unscoped list rather than failing.

    Raises RuntimeError, so one project's failure doesn't stop the others."""
    found = set()
    end_utc = to_utc(datetime.combine(today_local(), datetime.min.time()))
    start_utc = end_utc - timedelta(days=min(RETENTION_DAYS, 30))
    try:
        r = loki_get("/loki/api/v1/label/env/values",
                     {"start": str(_unix_ns(start_utc)), "end": str(_unix_ns(end_utc)),
                      "query": '{project="%s"}' % project}, timeout=60)
        found = {v for v in (r.get("data") or []) if v}
    except Exception as ex:
        raise RuntimeError("Could not discover environments of project '%s' from Loki: %s -- "
                           "set ENVS to an explicit list instead of ALL." % (project, ex))
    if not found:
        raise RuntimeError("ENVS=ALL found no 'env' label values for project '%s'. Check "
                           "LOKI_PROJECTS / job." % project)

    def natural_key(name):
        prefix = re.sub(r"\d", "", name)
        digits = re.sub(r"\D", "", name)
        return (prefix, int(digits) if digits else 0)

    keep = sorted((e for e in found if e.upper() not in ENVS_EXCLUDE), key=natural_key)
    log("Discovered envs  : %d found in %s, %d after exclusions" % (len(found), project, len(keep)))
    if not keep:
        raise RuntimeError("Every env of project '%s' is in ENVS_EXCLUDE -- nothing to collect."
                           % project)
    return keep


# --------------------------------------------------------------------------
# which days
# --------------------------------------------------------------------------
def target_days():
    """Complete local days to collect, oldest first. Never includes today."""
    yesterday = today_local() - timedelta(days=1)

    if BACKFILL_START:
        start = datetime.strptime(BACKFILL_START, "%Y-%m-%d").date()
        end = datetime.strptime(BACKFILL_END, "%Y-%m-%d").date() if BACKFILL_END else yesterday
    else:
        end = yesterday
        start = end - timedelta(days=LOOKBACK_DAYS - 1)

    if start > end:
        raise SystemExit("Empty window: start %s is after end %s." % (start, end))
    if end > yesterday:
        log("NOTE: clamping end %s -> %s (today is incomplete)." % (end, yesterday))
        end = yesterday

    oldest = yesterday - timedelta(days=RETENTION_DAYS - 1)
    days, skipped = [], []
    d = start
    while d <= end:
        (skipped if d < oldest else days).append(d)
        d += timedelta(days=1)

    if skipped:
        warn("Skipping %d day(s) older than Loki retention (%s..%s). Those logs are "
             "gone; querying them would return 0 and overwrite real history."
             % (len(skipped), skipped[0], skipped[-1]))
    return days


# --------------------------------------------------------------------------
# sql server
# --------------------------------------------------------------------------
def connect():
    p = get_sql()
    if not DB_TRUSTED and not DB_USER:
        raise SystemExit("Set DB_USER/DB_PASS, or DB_TRUSTED_CONNECTION=true.")
    host, port, _instance = parse_server(DB_SERVER)
    # pymssql hangs indefinitely when a server accepts the connection and then
    # says nothing; find that out with a timeout instead of hanging the job.
    if port and tds_probe(host, port) == "silent":
        raise SystemExit("%s:%d accepted the connection but never answered SQL Server's "
                         "handshake -- a firewall or proxy is swallowing the traffic. Not "
                         "connecting: it would hang." % (host, port))
    try:
        cn = connect_sql(p, DB_SERVER, DB_NAME, DB_USER, DB_PASS, DB_TRUSTED, DB_ENCRYPT,
                         timeout=300, appname="GW-Login-Collector", autocommit=False)
    except p.Error as ex:
        raise SystemExit(explain_connect_error(ex, DB_SERVER, DB_NAME))
    apply_session_options(cn)
    log("SQL client       : pymssql %s, %s" % (
        getattr(p, "__version__", "?"),
        "encrypted (certificate not verified)" if DB_ENCRYPT else "unencrypted"))
    return cn


def require_project_column(cn):
    """Refuse to write to tables from before the project column. Only setup's
    'schema' action can upgrade them, because it has to tag the rows already
    stored with the project they came from. Runs before the first write, so a
    refused run leaves nothing behind -- not even a run row."""
    cur = cn.cursor()
    cur.execute("SELECT COL_LENGTH('{schema}.gw_login_daily', 'project'), "
                "COL_LENGTH('{schema}.gw_login_user_daily', 'project'), "
                "COL_LENGTH('{schema}.gw_login_collector_run', 'project')".format(schema=DB_SCHEMA))
    row = cur.fetchone()
    cur.close()
    if row is None or any(v is None for v in row):
        cn.close()
        raise SystemExit("The login tables have no project column yet. Run the setup pipeline's "
                         "'schema' action to upgrade them (existing rows are tagged with "
                         "LOKI_PROJECT), then re-run.")


# Every statement names its project: env names repeat across projects, so a
# key without it would overwrite -- or delete -- another project's rows.
#
# The `%s = 1 OR s.logins > 0` guard means a zero never overwrites an existing
# non-zero count. A zero almost always means something upstream broke -- Loki
# down, a renamed label, logs outside retention -- and unlike Loki this table
# has no other copy. The first write for a day still records a legitimate 0.
UPSERT_DAILY = """
MERGE {schema}.gw_login_daily WITH (HOLDLOCK) AS t
USING (SELECT CAST(%s AS VARCHAR(64)) AS project,
              CAST(%s AS DATE)        AS [day],
              CAST(%s AS VARCHAR(32)) AS env,
              CAST(%s AS VARCHAR(8))  AS product,
              CAST(%s AS BIGINT)      AS logins,
              CAST(%s AS INT)         AS distinct_users) AS s
    ON  t.project = s.project AND t.[day] = s.[day]
    AND t.env = s.env AND t.product = s.product
WHEN MATCHED AND (%s = 1 OR s.logins > 0) THEN
    UPDATE SET logins = s.logins, distinct_users = s.distinct_users,
               collected_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT (project, [day], env, product, logins, distinct_users)
    VALUES (s.project, s.[day], s.env, s.product, s.logins, s.distinct_users);
"""

UPSERT_USER = """
MERGE {schema}.gw_login_user_daily WITH (HOLDLOCK) AS t
USING (SELECT CAST(%s AS VARCHAR(64))  AS project,
              CAST(%s AS DATE)         AS [day],
              CAST(%s AS VARCHAR(32))  AS env,
              CAST(%s AS VARCHAR(8))   AS product,
              CAST(%s AS NVARCHAR(128)) AS username,
              CAST(%s AS INT)          AS logins) AS s
    ON  t.project = s.project AND t.[day] = s.[day] AND t.env = s.env
    AND t.product = s.product AND t.username = s.username
WHEN MATCHED THEN
    UPDATE SET logins = s.logins, collected_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT (project, [day], env, product, username, logins)
    VALUES (s.project, s.[day], s.env, s.product, s.username, s.logins);
"""

# A user who stops appearing on a re-collected day must not linger.
DELETE_STALE_USERS = """
DELETE FROM {schema}.gw_login_user_daily
WHERE project = CAST(%s AS VARCHAR(64)) AND [day] = %s
  AND env = CAST(%s AS VARCHAR(32)) AND product = CAST(%s AS VARCHAR(8))
"""

INSERT_RUN = """
INSERT INTO {schema}.gw_login_collector_run
    (project, started_at, status, days_from, days_to, build_id)
OUTPUT INSERTED.run_id
VALUES (%s, %s, 'running', %s, %s, %s)
"""

UPDATE_RUN = """
UPDATE {schema}.gw_login_collector_run
SET finished_at = %s, status = %s, rows_written = %s, query_errors = %s, detail = %s
WHERE run_id = %s
"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def collect_project(cn, project, days):
    """Every day x env x product of one project, under that project's own run
    row. Returns (rows_written, query_errors). Loki failures -- env discovery
    included -- are counted, not raised, so the next project still runs."""
    started = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    written = 0
    errors = 0
    failed = []
    totals = {}

    try:
        envs = discover_envs(project) if DISCOVER_ENVS else ENVS
    except RuntimeError as ex:
        envs = []
        errors += 1
        failed.append(str(ex))
        warn("  ERROR %s" % ex)
    log("--- project %s : %d env(s)" % (project, len(envs)))
    if envs:
        log("Environments     : %s" % ", ".join(envs))

    run_id = None
    if cn:
        cur = cn.cursor()
        cur.execute(INSERT_RUN.format(schema=DB_SCHEMA),
                    (project, started, days[0], days[-1], BUILD_ID))
        run_id = cur.fetchone()[0]
        cn.commit()
        log("run_id           : %s" % run_id)
    log("")

    for day in days:
        for env_label in envs:
            for product in PRODUCTS:
                try:
                    events = fetch_day_events(project, day, env_label, product)
                except Exception as ex:
                    errors += 1
                    failed.append("%s %s/%s" % (day, env_label, product))
                    warn("  ERROR %s %s %s/%s: %s" % (project, day, env_label, product, ex))
                    # Leave whatever is stored alone; the next run's lookback retries.
                    continue

                per_user = {}
                for _when, user in events:
                    per_user[user] = per_user.get(user, 0) + 1
                logins = len(events)
                distinct = len([u for u in per_user if u != UNKNOWN_USER])
                totals[env_label] = totals.get(env_label, 0) + logins

                if cn:
                    cur = cn.cursor()
                    cur.execute(UPSERT_DAILY.format(schema=DB_SCHEMA),
                                (project, day, env_label, product, logins, distinct,
                                 1 if ALLOW_ZERO_OVERWRITE else 0))
                    written += max(cur.rowcount, 0)    # -1 = 'unknown' on some drivers
                    if STORE_USERNAMES and (logins > 0 or ALLOW_ZERO_OVERWRITE):
                        cur.execute(DELETE_STALE_USERS.format(schema=DB_SCHEMA),
                                    (project, day, env_label, product))
                        for user, n in per_user.items():
                            cur.execute(UPSERT_USER.format(schema=DB_SCHEMA),
                                        (project, day, env_label, product, user[:128], n))

                log("  %s  %-8s %-3s %8d logins  %5d distinct" % (
                    day, env_label, product.upper(), logins, distinct))
        if cn:
            cn.commit()

    status = "failed" if errors and not written else ("partial" if errors else "ok")
    detail = ("Loki failures: " + "; ".join(failed[:20]))[:2000] if failed else None

    if cn:
        cur = cn.cursor()
        cur.execute(UPDATE_RUN.format(schema=DB_SCHEMA),
                    (datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None),
                     status, written, errors, detail, run_id))
        cn.commit()

    log("")
    for env_label in envs:
        log("  %-8s %10d logins across %d day(s)" % (
            env_label, totals.get(env_label, 0), len(days)))
    log("project=%s  status=%s  rows_written=%d  query_errors=%d" % (
        project, status, written, errors))
    log("")
    return written, errors


def main():
    global PROJECTS

    if not DISCOVER_ENVS and not ENVS:
        raise SystemExit("ENVS is empty -- nothing to collect.")

    days = target_days()
    if not days:
        log("Nothing to collect (every requested day is outside Loki retention).")
        return 0

    mode = "backfill" if BACKFILL_START else "daily"
    PROJECTS = check_projects(PROJECTS)
    log("Mode             : %s" % mode)
    log("Days             : %s to %s  (%d)" % (days[0], days[-1], len(days)))
    log("Projects         : %s" % ", ".join(PROJECTS))
    log("Centres          : %s" % ", ".join(PRODUCTS))
    log("Day boundaries   : %s (%s)" % (TZ_LABEL, tz_rule()))
    log("Loki             : %s  (proxy %s)" % (
        LOKI_URL, "bypassed" if BYPASS_PROXY else "system"))
    log("Database         : %s/%s.%s   dry_run=%s" % (DB_SERVER or "-", DB_NAME or "-", DB_SCHEMA, DRY_RUN))
    for project in PROJECTS:
        if project not in LOKI_PROJECTS:
            # Allowed, so a new project can be backfilled before the variable
            # group is updated -- but the scheduled run won't keep it current.
            log("NOTE: project '%s' is not in LOKI_PROJECTS (%s) -- collecting it anyway; "
                "add it there so the daily run covers it." % (project, ", ".join(LOKI_PROJECTS)))
    log("")

    cn = None if DRY_RUN else connect()
    if cn:
        require_project_column(cn)

    written = 0
    errors = 0
    for project in PROJECTS:
        w, e = collect_project(cn, project, days)
        written += w
        errors += e

    if cn:
        cn.close()

    log("overall: %d project(s), rows_written=%d, query_errors=%d" % (
        len(PROJECTS), written, errors))

    # Non-zero exit so ADO flags the run and somebody looks. A gap is only
    # recoverable while it is still inside Loki retention.
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
