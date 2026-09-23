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

Standard library only, except pyodbc for the database.

Modes
-----
  normal    re-collect the last LOOKBACK_DAYS complete days. Re-collecting
            rather than only doing yesterday is deliberate: the upsert is
            idempotent, so a missed run or a late-arriving log self-heals on
            the next run with nobody intervening.

  backfill  set BACKFILL_START (and optionally BACKFILL_END) to load a range.
            Use once at go-live to seed whatever history Loki still holds.
"""

import os
import re
import ssl
import sys
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    import pyodbc
except ImportError:
    raise SystemExit(
        "pyodbc is not installed. On the agent:  python -m pip install pyodbc\n"
        "It also needs Microsoft's ODBC Driver for SQL Server on the machine.\n"
        "See DEPLOY.md -> 'Agent prerequisites'."
    )


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def env(name, default=""):
    v = os.environ.get(name, "")
    # A variable not defined in the group arrives as the literal "$(NAME)"
    # rather than empty; treat that as unset.
    if v.strip() == "" or (v.strip().startswith("$(") and v.strip().endswith(")")):
        return default
    return v


def env_bool(name, default=False):
    v = env(name).strip().lower()
    if v == "":
        return default
    return v in ("true", "1", "yes")


def split_list(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def warn(msg):
    print("##vso[task.logissue type=warning]" + msg)


def log(msg):
    print(msg, flush=True)


LOKI_URL = env("LOKI_URL", "https://your-loki-host.example.com:3100").rstrip("/")
LOKI_PROJECT = env("LOKI_PROJECT", "myproject")
VERIFY_TLS = env_bool("LOKI_VERIFY_TLS", True)
BYPASS_PROXY = env_bool("BYPASS_PROXY", True)
LOG_LIMIT = int(env("LOKI_LOG_LIMIT", "5000"))
HTTP_TIMEOUT = int(env("LOKI_HTTP_TIMEOUT", "180"))

ENVS = split_list(env("ENVS", "DEV1"))
ENVS_EXCLUDE = [e.upper() for e in split_list(env("ENVS_EXCLUDE"))]
PRODUCTS = [p.lower() for p in split_list(env("PRODUCTS", "pc"))]

LOOKBACK_DAYS = int(env("LOOKBACK_DAYS", "7"))
RETENTION_DAYS = int(env("LOKI_RETENTION_DAYS", "30"))
BACKFILL_START = env("BACKFILL_START").strip()
BACKFILL_END = env("BACKFILL_END").strip()
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
DB_DRIVER = env("DB_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
DB_SCHEMA = env("DB_SCHEMA", "dbo")
DB_TRUSTED = env_bool("DB_TRUSTED_CONNECTION", False)
DB_ENCRYPT = env_bool("DB_ENCRYPT", True)
DB_TRUST_CERT = env_bool("DB_TRUST_SERVER_CERT", False)
DB_TIMEOUT = int(env("DB_TIMEOUT", "30"))

# Schema/table names cannot be bound as parameters, so they are interpolated --
# validate rather than trusting the variable group.
if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", DB_SCHEMA):
    raise SystemExit("DB_SCHEMA is not a valid SQL identifier: %r" % DB_SCHEMA)
if not DRY_RUN:
    for key, val in (("DB_SERVER", DB_SERVER), ("DB_NAME", DB_NAME)):
        if not val:
            raise SystemExit("%s is not set. Add it to the variable group." % key)

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


def today_local():
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return (from_utc(now_utc) if REPORT_TZ else now_utc).date()


# --------------------------------------------------------------------------
# loki
# --------------------------------------------------------------------------
def _unix_ns(dt):
    return int((dt - datetime(1970, 1, 1)).total_seconds()) * 1_000_000_000


_ssl_ctx = None
if not VERIFY_TLS:
    log("LOKI_VERIFY_TLS is false -- certificate validation disabled for this run.")
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE

_handlers = []
if BYPASS_PROXY:
    _handlers.append(urllib.request.ProxyHandler({}))
if _ssl_ctx is not None:
    _handlers.append(urllib.request.HTTPSHandler(context=_ssl_ctx))
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
    raise RuntimeError("%s: %s" % (path, last))


def selector(job, env_label, frag):
    return '{project="%s", job="%s", env="%s", filename=~".*%s.log"}' % (
        LOKI_PROJECT, job, env_label, frag)


def fetch_day_events(day_local, env_label, product):
    """
    Every login event for one local calendar day / env / product,
    as a list of (local_datetime, username).

    Pages through Loki rather than warning about truncation. The report can
    afford to warn and move on -- it runs again next month. This table is the
    only permanent copy, so a silently short day would be wrong forever.
    """
    meta = PRODUCT_META[product]
    sel = "%s |= `User Login`" % selector(meta["job"], env_label, meta["frag"])

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
            raise RuntimeError("Paging did not terminate for %s %s/%s" % (day_local, env_label, product))
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
            when_utc = datetime.utcfromtimestamp(ts_ns / 1e9)
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
        warn("%s %s/%s : some lines did not match LOGIN_USER_REGEX -- stored as %s." % (
            day_local, env_label, product, UNKNOWN_USER))
        for smp in unparsed:
            log("    " + smp)

    return events


def discover_envs():
    """ENVS=ALL -- ask Loki which env labels exist."""
    found = set()
    end_utc = to_utc(datetime.combine(today_local(), datetime.min.time()))
    start_utc = end_utc - timedelta(days=min(RETENTION_DAYS, 30))
    try:
        r = loki_get("/loki/api/v1/label/env/values",
                     {"start": str(_unix_ns(start_utc)), "end": str(_unix_ns(end_utc))}, timeout=60)
        found = {v for v in (r.get("data") or []) if v}
    except Exception as ex:
        raise SystemExit("Could not discover environments from Loki: %s\n"
                         "Set ENVS to an explicit list instead of ALL." % ex)
    if not found:
        raise SystemExit("ENVS=ALL found no 'env' label values. Check LOKI_PROJECT / job.")

    def natural_key(name):
        prefix = re.sub(r"\d", "", name)
        digits = re.sub(r"\D", "", name)
        return (prefix, int(digits) if digits else 0)

    keep = sorted((e for e in found if e.upper() not in ENVS_EXCLUDE), key=natural_key)
    log("Discovered envs  : %d found, %d after exclusions" % (len(found), len(keep)))
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
    cn = pyodbc.connect(";".join(parts) + ";", timeout=DB_TIMEOUT)
    cn.autocommit = False
    return cn


# The `? = 1 OR s.logins > 0` guard means a zero never overwrites an existing
# non-zero count. A zero almost always means something upstream broke -- Loki
# down, a renamed label, logs outside retention -- and unlike Loki this table
# has no other copy. The first write for a day still records a legitimate 0.
UPSERT_DAILY = """
MERGE {schema}.gw_login_daily WITH (HOLDLOCK) AS t
USING (SELECT CAST(? AS DATE)        AS [day],
              CAST(? AS VARCHAR(32)) AS env,
              CAST(? AS VARCHAR(8))  AS product,
              CAST(? AS BIGINT)      AS logins,
              CAST(? AS INT)         AS distinct_users) AS s
    ON  t.[day] = s.[day] AND t.env = s.env AND t.product = s.product
WHEN MATCHED AND (? = 1 OR s.logins > 0) THEN
    UPDATE SET logins = s.logins, distinct_users = s.distinct_users,
               collected_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT ([day], env, product, logins, distinct_users)
    VALUES (s.[day], s.env, s.product, s.logins, s.distinct_users);
"""

UPSERT_USER = """
MERGE {schema}.gw_login_user_daily WITH (HOLDLOCK) AS t
USING (SELECT CAST(? AS DATE)         AS [day],
              CAST(? AS VARCHAR(32))  AS env,
              CAST(? AS VARCHAR(8))   AS product,
              CAST(? AS NVARCHAR(128)) AS username,
              CAST(? AS INT)          AS logins) AS s
    ON  t.[day] = s.[day] AND t.env = s.env
    AND t.product = s.product AND t.username = s.username
WHEN MATCHED THEN
    UPDATE SET logins = s.logins, collected_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT ([day], env, product, username, logins)
    VALUES (s.[day], s.env, s.product, s.username, s.logins);
"""

# A user who stops appearing on a re-collected day must not linger.
DELETE_STALE_USERS = """
DELETE FROM {schema}.gw_login_user_daily
WHERE [day] = ? AND env = ? AND product = ?
"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    global ENVS

    if len(ENVS) == 1 and ENVS[0].upper() == "ALL":
        ENVS = discover_envs()
    if not ENVS:
        raise SystemExit("ENVS is empty -- nothing to collect.")

    days = target_days()
    if not days:
        log("Nothing to collect (every requested day is outside Loki retention).")
        return 0

    mode = "backfill" if BACKFILL_START else "daily"
    log("Mode             : %s" % mode)
    log("Days             : %s to %s  (%d)" % (days[0], days[-1], len(days)))
    log("Environments     : %s" % ", ".join(ENVS))
    log("Centres          : %s" % ", ".join(PRODUCTS))
    log("Day boundaries   : %s" % TZ_LABEL)
    log("Loki             : %s  (project=%s, proxy %s)" % (
        LOKI_URL, LOKI_PROJECT, "bypassed" if BYPASS_PROXY else "system"))
    log("Database         : %s/%s.%s   dry_run=%s" % (DB_SERVER or "-", DB_NAME or "-", DB_SCHEMA, DRY_RUN))
    log("")

    started = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    cn = None if DRY_RUN else connect()
    run_id = None

    if cn:
        cur = cn.cursor()
        cur.execute(
            "INSERT INTO %s.gw_login_collector_run "
            "(started_at, status, days_from, days_to, build_id) "
            "OUTPUT INSERTED.run_id VALUES (?, 'running', ?, ?, ?)" % DB_SCHEMA,
            started, days[0], days[-1], BUILD_ID)
        run_id = cur.fetchone()[0]
        cn.commit()
        log("run_id           : %s" % run_id)
        log("")

    written = 0
    errors = 0
    failed = []
    totals = {}

    for day in days:
        for env_label in ENVS:
            for product in PRODUCTS:
                try:
                    events = fetch_day_events(day, env_label, product)
                except Exception as ex:
                    errors += 1
                    failed.append("%s %s/%s" % (day, env_label, product))
                    warn("  ERROR %s %s/%s: %s" % (day, env_label, product, ex))
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
                    cur.execute(UPSERT_DAILY.format(schema=DB_SCHEMA), day, env_label,
                                product, logins, distinct,
                                1 if ALLOW_ZERO_OVERWRITE else 0)
                    written += cur.rowcount or 0
                    if STORE_USERNAMES and (logins > 0 or ALLOW_ZERO_OVERWRITE):
                        cur.execute(DELETE_STALE_USERS.format(schema=DB_SCHEMA),
                                    day, env_label, product)
                        for user, n in per_user.items():
                            cur.execute(UPSERT_USER.format(schema=DB_SCHEMA),
                                        day, env_label, product, user[:128], n)

                log("  %s  %-8s %-3s %8d logins  %5d distinct" % (
                    day, env_label, product.upper(), logins, distinct))
        if cn:
            cn.commit()

    status = "failed" if errors and not written else ("partial" if errors else "ok")
    detail = ("Loki failures: " + "; ".join(failed[:20]))[:2000] if failed else None

    if cn:
        cur = cn.cursor()
        cur.execute(
            "UPDATE %s.gw_login_collector_run SET finished_at = ?, status = ?, "
            "rows_written = ?, query_errors = ?, detail = ? WHERE run_id = ?" % DB_SCHEMA,
            datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None),
            status, written, errors, detail, run_id)
        cn.commit()
        cn.close()

    log("")
    for env_label in ENVS:
        log("  %-8s %10d logins across %d day(s)" % (
            env_label, totals.get(env_label, 0), len(days)))
    log("status=%s  rows_written=%d  query_errors=%d" % (status, written, errors))

    # Non-zero exit so ADO flags the run and somebody looks. A gap is only
    # recoverable while it is still inside Loki retention.
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
