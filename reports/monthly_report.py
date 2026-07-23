#!/usr/bin/env python3
"""
Monthly Guidewire login report: Loki -> Excel -> email.

Standard library only -- no pip install. urllib for HTTP, zipfile for the xlsx,
smtplib for mail. All configuration comes from environment variables.
"""

import os
import re
import ssl
import json
import zipfile
import smtplib
import urllib.parse
import urllib.request
from io import BytesIO
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate


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


LOKI_URL = env("LOKI_URL", "https://your-loki-host.example.com:3100").rstrip("/")
LOKI_PROJECT = env("LOKI_PROJECT", "myproject")
CHUNK_DAYS = int(env("LOKI_MAX_QUERY_DAYS", "7"))
VERIFY_TLS = env_bool("LOKI_VERIFY_TLS", True)
BYPASS_PROXY = env_bool("BYPASS_PROXY", True)

SMTP_HOST = env("SMTP_HOST")
SMTP_PORT = int(env("SMTP_PORT", "25"))
SMTP_TLS = env_bool("SMTP_TLS", False)
SMTP_USER = env("SMTP_USER")
SMTP_PASS = env("SMTP_PASS")

FROM_ADDR = env("FROM_ADDR")
TO_ADDRS = split_list(env("TO_ADDRS"))
ENVS = split_list(env("ENVS", "DEV1"))
ENVS_EXCLUDE = [e.upper() for e in split_list(env("ENVS_EXCLUDE"))]
PRODUCTS = [p.lower() for p in split_list(env("PRODUCTS", "pc"))]
OUT_DIR = env("OUTPUT_DIR", ".")

REPORT_TITLE = env("REPORT_TITLE", "Guidewire Login Report")
FILE_PREFIX = env("REPORT_FILE_PREFIX", "gw-logins")

INCLUDE_USERS = env_bool("INCLUDE_USER_DETAIL", True)
USER_REGEX = env("LOGIN_USER_REGEX", r"(?i)User\s+Login\s*[:=\-]?\s*(?P<user>[A-Za-z0-9._\\@-]+)")
LOG_LIMIT = int(env("LOKI_LOG_LIMIT", "5000"))
MAX_DETAIL_ROWS = int(env("MAX_DETAIL_ROWS", "50000"))
LEAST_USED_COUNT = int(env("LEAST_USED_COUNT", "5"))

# LOGIN_USER_REGEX uses .NET-style (?<user>...); accept it by rewriting to Python's (?P<user>...).
USER_REGEX = USER_REGEX.replace("(?<user>", "(?P<user>")
try:
    USER_RE = re.compile(USER_REGEX)
except re.error as ex:
    raise SystemExit("LOGIN_USER_REGEX is not a valid regex: %s" % ex)

for key, val in (("SMTP_HOST", SMTP_HOST), ("FROM_ADDR", FROM_ADDR)):
    if not val:
        raise SystemExit("%s is not set. Add it to the variable group." % key)
if not TO_ADDRS:
    raise SystemExit("TO_ADDRS is not set. Add it to the variable group.")

# pclogs is confirmed; BC/CC/CM are guesses. The label check below prints what
# exists. Override from the variable group: PRODUCT_JOBS / PRODUCT_FRAGS as
# comma lists of comp=value, e.g. PRODUCT_JOBS=cm=ablogs  PRODUCT_FRAGS=cm=ab
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
# timezone (no external deps -- built-in US Eastern DST, or a fixed offset)
# --------------------------------------------------------------------------
REPORT_TZ = env("REPORT_TIMEZONE")
_EASTERN = {"eastern standard time", "america/new_york", "et", "est", "edt", "eastern"}


def _second_sunday(year, month):
    d = datetime(year, month, 1)
    first_sun = d + timedelta(days=(6 - d.weekday()) % 7)
    return first_sun + timedelta(days=7)


def _first_sunday(year, month):
    d = datetime(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7)


def utc_offset(naive_local):
    """Return the tz offset (as timedelta) in effect at a naive local datetime."""
    if not REPORT_TZ:
        return timedelta(0)
    if REPORT_TZ.strip().lower() in _EASTERN:
        # EDT (UTC-4) from 2nd Sun Mar 02:00 to 1st Sun Nov 02:00, else EST (UTC-5).
        y = naive_local.year
        start = _second_sunday(y, 3).replace(hour=2)
        end = _first_sunday(y, 11).replace(hour=2)
        return timedelta(hours=-4) if start <= naive_local < end else timedelta(hours=-5)
    # Unknown zone: try zoneinfo if present, else fall back to UTC.
    try:
        from zoneinfo import ZoneInfo
        aware = naive_local.replace(tzinfo=ZoneInfo(REPORT_TZ))
        return aware.utcoffset()
    except Exception:
        warn("Unknown REPORT_TIMEZONE '%s' -- times stay in UTC." % REPORT_TZ)
        return timedelta(0)


def to_utc(naive_local):
    return naive_local - utc_offset(naive_local)


def from_utc(utc_dt):
    # Two-pass: offset depends on local time, but a month-scale report is far
    # from a DST boundary so one correction is enough.
    approx = utc_dt + utc_offset(utc_dt)
    return utc_dt + utc_offset(approx)


if REPORT_TZ and REPORT_TZ.strip().lower() in _EASTERN:
    _default_label = REPORT_TZ
elif REPORT_TZ:
    _default_label = REPORT_TZ
else:
    _default_label = "UTC"
TZ_LABEL = env("REPORT_TZ_LABEL", _default_label)


# --------------------------------------------------------------------------
# reporting window (boundaries in the report timezone)
# --------------------------------------------------------------------------
TEST_MONTH = env("TEST_MONTH")
now_local = from_utc(datetime.now(timezone.utc).replace(tzinfo=None)) if REPORT_TZ else datetime.now(timezone.utc).replace(tzinfo=None)

if TEST_MONTH:
    month_start = datetime.strptime(TEST_MONTH + "-01", "%Y-%m-%d")
    print("Period source    : TEST_MONTH override")
else:
    month_start = datetime(now_local.year, now_local.month, 1)
    print("Period source    : current month")

if month_start.month == 12:
    month_end = datetime(month_start.year + 1, 1, 1)
else:
    month_end = datetime(month_start.year, month_start.month + 1, 1)

start_utc = to_utc(month_start)
end_utc = to_utc(month_end)
month_label = month_start.strftime("%B %Y")
gen_stamp = from_utc(datetime.now(timezone.utc).replace(tzinfo=None)).strftime("%Y-%m-%d %H:%M") + " " + TZ_LABEL

print("Reporting period : %s to %s  (%s, %s)" % (
    month_start.strftime("%Y-%m-%d"),
    (month_end - timedelta(days=1)).strftime("%Y-%m-%d"),
    month_label, TZ_LABEL))
print("Environments     : %s" % ", ".join(ENVS))
print("Centres          : %s" % ", ".join(PRODUCTS))
print("")


# --------------------------------------------------------------------------
# loki
# --------------------------------------------------------------------------
def _unix_ns(dt):
    return int((dt - datetime(1970, 1, 1)).total_seconds()) * 1_000_000_000


_ssl_ctx = None
if not VERIFY_TLS:
    print("LOKI_VERIFY_TLS is false -- certificate validation disabled for this run.")
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE

_handlers = []
if BYPASS_PROXY:
    _handlers.append(urllib.request.ProxyHandler({}))
    print("Proxy            : bypassed (BYPASS_PROXY=true)")
else:
    print("Proxy            : system default")
if _ssl_ctx is not None:
    _handlers.append(urllib.request.HTTPSHandler(context=_ssl_ctx))
_opener = urllib.request.build_opener(*_handlers)


def loki_get(path, params, timeout=120):
    url = LOKI_URL + path + "?" + urllib.parse.urlencode(params)
    with _opener.open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def selector(job, env_label, frag):
    return '{project="%s", job="%s", env="%s", filename=~".*%s.log"}' % (
        LOKI_PROJECT, job, env_label, frag)


# ---- label diagnostics: show what actually exists ----
def label_values(label):
    # Some Loki builds reject start/end here; fall back to a bare call.
    for params in ({"start": str(_unix_ns(start_utc)), "end": str(_unix_ns(end_utc))}, {}):
        try:
            r = loki_get("/loki/api/v1/label/%s/values" % label, params, timeout=60)
            return sorted(r.get("data") or [])
        except Exception:
            continue
    return []


def series_for(job):
    try:
        r = loki_get("/loki/api/v1/series", {
            "start": str(_unix_ns(start_utc)),
            "end": str(_unix_ns(start_utc + timedelta(days=min(CHUNK_DAYS, 1), hours=1))),
            "match[]": '{project="%s", job="%s"}' % (LOKI_PROJECT, job),
        }, timeout=60)
        return r.get("data") or []
    except Exception as ex:
        print("  (series probe failed for job='%s': %s)" % (job, ex))
        return []


_jobs = label_values("job")
if _jobs:
    print("All job labels in Loki: %s" % ", ".join(_jobs))
print("Loki label check (project='%s'):" % LOKI_PROJECT)
for p in PRODUCTS:
    meta = PRODUCT_META[p]
    series = series_for(meta["job"])
    if not series:
        warn("  %s: job='%s' returned NO series. That job label probably does not "
             "exist -- check Grafana -> Label browser -> job." % (p.upper(), meta["job"]))
        continue
    files = sorted({s.get("filename") for s in series if s.get("filename")})
    envs = sorted({s.get("env") for s in series if s.get("env")})
    print("  %s: job='%s' OK -- %d series, %d env(s). Filenames: %s" % (
        p.upper(), meta["job"], len(series), len(envs), ", ".join(files[:3])))
    if not any(f.endswith(meta["frag"] + ".log") for f in files):
        warn("    None of those filenames end in '%s.log' -- set PRODUCT_FRAGS for "
             "%s (e.g. if the file is <x>log.log the fragment is <x>log)." % (meta["frag"], p.upper()))
print("")


def fetch_daily(env_label, product):
    """Return {date: count} for one env+product, chunked and day-aligned."""
    meta = PRODUCT_META[product]
    query = "sum(count_over_time(%s |= `User Login` [1d]))" % selector(meta["job"], env_label, meta["frag"])
    daily = {}
    # Loki stamps each sample at the END of its [1d] window, so query from
    # start+1d and label each sample (timestamp - 1d).
    chunk_from = start_utc + timedelta(days=1)
    while chunk_from < end_utc:
        chunk_to = min(chunk_from + timedelta(days=CHUNK_DAYS), end_utc)
        try:
            r = loki_get("/loki/api/v1/query_range", {
                "query": query, "start": str(_unix_ns(chunk_from)),
                "end": str(_unix_ns(chunk_to)), "step": "1d",
            })
        except Exception as ex:
            raise SystemExit("Loki query failed for %s/%s (%s to %s): %s\nQuery: %s" % (
                env_label, product, chunk_from.date(), chunk_to.date(), ex, query))
        result = r["data"]["result"]
        if result:
            for ts, val in result[0]["values"]:
                day = (datetime.fromtimestamp(float(ts), timezone.utc).replace(tzinfo=None) - timedelta(days=1)).date()
                if start_utc.date() <= day < end_utc.date():
                    daily[day] = int(float(val))
        chunk_from = chunk_to
    return daily


def fetch_events(env_label, product):
    """Return list of (local_dt, user) login events for one env+product."""
    meta = PRODUCT_META[product]
    sel = "%s |= `User Login`" % selector(meta["job"], env_label, meta["frag"])
    events = []
    samples = []
    unparsed = 0
    hit_limit = False
    chunk_from = start_utc
    while chunk_from < end_utc:
        chunk_to = min(chunk_from + timedelta(days=CHUNK_DAYS), end_utc)
        try:
            r = loki_get("/loki/api/v1/query_range", {
                "query": sel, "start": str(_unix_ns(chunk_from)),
                "end": str(_unix_ns(chunk_to)), "limit": str(LOG_LIMIT), "direction": "forward",
            }, timeout=180)
        except Exception as ex:
            raise SystemExit("Loki log query failed for %s/%s: %s\nSelector: %s" % (
                env_label, product, ex, sel))
        in_chunk = 0
        for stream in r["data"]["result"]:
            for ts, line in stream["values"]:
                in_chunk += 1
                when_utc = datetime.fromtimestamp(float(ts) / 1e9, timezone.utc).replace(tzinfo=None)
                when = from_utc(when_utc) if REPORT_TZ else when_utc
                m = USER_RE.search(line)
                user = m.group("user") if (m and m.groupdict().get("user")) else ""
                if not user:
                    unparsed += 1
                    if len(samples) < 3:
                        samples.append(line)
                events.append((when, user))
        if in_chunk >= LOG_LIMIT:
            hit_limit = True
        chunk_from = chunk_to
    if hit_limit:
        warn("%s/%s hit Loki's %d-entry cap in a chunk -- user detail is incomplete. "
             "Lower LOKI_MAX_QUERY_DAYS." % (env_label, product, LOG_LIMIT))
    if unparsed:
        warn("%s/%s : %d line(s) did not match LOGIN_USER_REGEX -- those users are blank." % (
            env_label, product, unparsed))
        print("  Sample lines that did not match, with their whitespace-delimited fields:")
        for smp in samples:
            fields = smp.split()
            hint = "   ".join("[%d] %s" % (i + 1, f) for i, f in enumerate(fields[:5]))
            print("    " + smp)
            print("      fields: " + hint)
        print("  Set LOGIN_USER_REGEX, e.g. username is field 2 -> ^\\s*\\S+\\s+(?<user>\\S+)")
    return events


# --------------------------------------------------------------------------
# environment discovery (ENVS = ALL)
# --------------------------------------------------------------------------
def natural_key(name):
    prefix = re.sub(r"\d", "", name)
    digits = re.sub(r"\D", "", name)
    return (prefix, int(digits) if digits else 0)


discovered = False
if len(ENVS) == 1 and ENVS[0].upper() == "ALL":
    discovered = True
    job = PRODUCT_META[PRODUCTS[0]]["job"]
    found = set()
    cf = start_utc
    while cf < end_utc:
        ct = min(cf + timedelta(days=CHUNK_DAYS), end_utc)
        try:
            r = loki_get("/loki/api/v1/label/env/values",
                         {"start": str(_unix_ns(cf)), "end": str(_unix_ns(ct))}, timeout=60)
            for v in (r.get("data") or []):
                if v:
                    found.add(v)
        except Exception as ex:
            raise SystemExit("Could not discover environments from Loki: %s\n"
                             "Set ENVS to an explicit list instead of ALL." % ex)
        cf = ct
    if not found:
        raise SystemExit("ENVS=ALL found no 'env' label values for the window. Check LOKI_PROJECT / job.")
    ENVS = sorted((e for e in found if e.upper() not in ENVS_EXCLUDE), key=natural_key)
    print("Discovered envs  : %d found, %d after exclusions" % (len(found), len(ENVS)))
    print("                   %s" % ", ".join(ENVS))
    print("")


# --------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------
data = {}  # (env, product) -> {"daily": {...}, "total": int, "events": [...]}
for e in ENVS:
    for p in PRODUCTS:
        if INCLUDE_USERS:
            # Counts come from the SAME events as the user detail, so "logins"
            # and "distinct users" can never disagree (a separate count query
            # offsets its window and can differ by one at month boundaries).
            raw = fetch_events(e, p)
            events = [(w, u) for (w, u) in raw
                      if month_start.date() <= w.date() < month_end.date()]
            daily = {}
            for when, _u in events:
                daily[when.date()] = daily.get(when.date(), 0) + 1
            total = len(events)
        else:
            daily = fetch_daily(e, p)
            total = sum(daily.values())
            events = []
        data[(e, p)] = {"daily": daily, "total": total, "events": events}
        distinct = len({u for _, u in events if u})
        if INCLUDE_USERS:
            print("  %-8s %-4s %8d logins   %5d distinct users" % (e, p.upper(), total, distinct))
        else:
            print("  %-8s %-4s %8d logins" % (e, p.upper(), total))
print("")

if discovered:
    keep = [e for e in ENVS if sum(data[(e, p)]["total"] for p in PRODUCTS) > 0]
    dropped = len(ENVS) - len(keep)
    if dropped:
        print("Omitted %d discovered env(s) with no logins in this period." % dropped)
    if not keep:
        raise SystemExit("None of the discovered environments had logins in this period.")
    ENVS = keep
    print("")


# --------------------------------------------------------------------------
# xlsx (OOXML written by hand -- stdlib zipfile, no dependency)
# --------------------------------------------------------------------------
def col_name(idx):
    s = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def xml_escape(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


EPOCH = datetime(1899, 12, 30)
# style ids: 0 normal, 1 bold, 2 number, 3 header, 4 date, 5 date+time
STYLES = (
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF305496"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="6">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="22" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


def sheet_xml(rows, header_row=-1, date_style="4"):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
    for r, cells in enumerate(rows):
        out.append('<row r="%d">' % (r + 1))
        for c, v in enumerate(cells):
            if v is None:
                continue
            ref = col_name(c + 1) + str(r + 1)
            if isinstance(v, datetime):
                serial = (v - EPOCH).total_seconds() / 86400.0
                out.append('<c r="%s" s="%s"><v>%s</v></c>' % (ref, date_style, serial))
            elif isinstance(v, bool):
                out.append('<c r="%s" t="inlineStr" s="0"><is><t>%s</t></is></c>' % (ref, v))
            elif isinstance(v, (int, float)):
                out.append('<c r="%s" s="2"><v>%s</v></c>' % (ref, v))
            else:
                st = "3" if r == header_row else "0"
                out.append('<c r="%s" t="inlineStr" s="%s"><is><t xml:space="preserve">%s</t></is></c>'
                           % (ref, st, xml_escape(str(v))))
        out.append('</row>')
    out.append('</sheetData></worksheet>')
    return "".join(out)


def write_xlsx(path, sheets):
    """sheets: list of dicts {name, rows, header_row, date_style}."""
    n = len(sheets)
    overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1)
        for i in range(n))
    sheet_refs = "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (xml_escape(sheets[i]["name"]), i + 1, i + 1)
                         for i in range(n))
    rels = "".join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1)
                   for i in range(n))
    style_rid = n + 1
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + overrides + '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets>' + sheet_refs + '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + rels +
                   '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' % style_rid
                   + '</Relationships>')
        z.writestr("xl/styles.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + STYLES)
        for i, sh in enumerate(sheets):
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1),
                       sheet_xml(sh["rows"], sh.get("header_row", -1), sh.get("date_style", "4")))


# ---- build the sheets ----
summary = [[REPORT_TITLE],
           ["Period: %s to %s  (%s, times in %s)" % (
               month_start.strftime("%Y-%m-%d"), (month_end - timedelta(days=1)).strftime("%Y-%m-%d"),
               month_label, TZ_LABEL)],
           ["Source: Loki %s  |  Match: |= \"User Login\"  |  Generated: %s" % (LOKI_URL, gen_stamp)],
           [""], ["Environment", "Centre", "Total Logins"]]
summary_header = len(summary) - 1
grand = 0
for e in ENVS:
    for p in PRODUCTS:
        t = data[(e, p)]["total"]
        grand += t
        summary.append([e, PRODUCT_META[p]["label"], int(t)])
summary.append(["TOTAL", "", int(grand)])

combos = [(e, p) for e in ENVS for p in PRODUCTS]
all_dates = sorted({d for v in data.values() for d in v["daily"]})
daily_rows = [["Daily Login Counts"], [""],
              ["Date"] + [(e if len(PRODUCTS) == 1 else "%s/%s" % (e, p.upper())) for e, p in combos]]
daily_header = len(daily_rows) - 1
for d in all_dates:
    row = [datetime(d.year, d.month, d.day)]
    for e, p in combos:
        row.append(int(data[(e, p)]["daily"].get(d, 0)))
    daily_rows.append(row)

sheets = [
    {"name": "Summary", "rows": summary, "header_row": summary_header},
    {"name": "Daily", "rows": daily_rows, "header_row": daily_header},
]

if INCLUDE_USERS:
    users = [["Logins by User"], ["%s   |   timestamps in %s" % (month_label, TZ_LABEL)], [""],
             ["User", "Environment", "Centre", "Logins", "First Login", "Last Login"]]
    users_header = len(users) - 1
    for e in ENVS:
        for p in PRODUCTS:
            evts = data[(e, p)]["events"]
            by_user = {}
            for when, user in evts:
                if user:
                    by_user.setdefault(user, []).append(when)
            for user in sorted(by_user, key=lambda u: (-len(by_user[u]), u)):
                times = sorted(by_user[user])
                users.append([user, e, PRODUCT_META[p]["label"], len(times), times[0], times[-1]])
            blank = sum(1 for _, u in evts if not u)
            if blank:
                users.append(["(unparsed)", e, PRODUCT_META[p]["label"], blank, None, None])
    if len(users) == users_header + 1:
        users.append(["(no login events found)"])

    detail = [["Login Detail"], ["%s   |   timestamps in %s" % (month_label, TZ_LABEL)], [""],
              ["Timestamp", "Environment", "Centre", "User"]]
    detail_header = len(detail) - 1
    rows = []
    for e in ENVS:
        for p in PRODUCTS:
            for when, user in data[(e, p)]["events"]:
                rows.append((when, e, PRODUCT_META[p]["label"], user or "(unparsed)"))
    rows.sort(key=lambda x: x[0])
    if len(rows) > MAX_DETAIL_ROWS:
        warn("Login detail truncated to %d of %d rows (MAX_DETAIL_ROWS)." % (MAX_DETAIL_ROWS, len(rows)))
        rows = rows[:MAX_DETAIL_ROWS]
    for when, e, centre, user in rows:
        detail.append([when, e, centre, user])
    if not rows:
        detail.append(["(no login events found)"])
    print("Detail rows      : %d" % len(rows))

    sheets.append({"name": "Users", "rows": users, "header_row": users_header, "date_style": "5"})
    sheets.append({"name": "Detail", "rows": detail, "header_row": detail_header, "date_style": "5"})

os.makedirs(OUT_DIR, exist_ok=True)
file_name = "%s-%s.xlsx" % (FILE_PREFIX, month_start.strftime("%Y-%m"))
xlsx_path = os.path.join(OUT_DIR, file_name)
write_xlsx(xlsx_path, sheets)
print("Wrote %s (%d bytes)" % (xlsx_path, os.path.getsize(xlsx_path)))


# --------------------------------------------------------------------------
# email
# --------------------------------------------------------------------------
all_users = set()
env_totals = []
rows_html = ""
for i, e in enumerate(ENVS):
    env_logins = 0
    env_users = set()
    for p in PRODUCTS:
        d = data[(e, p)]
        env_logins += d["total"]
        us = {u for _, u in d["events"] if u}
        all_users |= us
        env_users |= us
        user_cell = ("{:,}".format(len(us))) if INCLUDE_USERS else "&mdash;"
        bg = " background:#f7f9fc;" if i % 2 else ""
        rows_html += ('<tr style="%s"><td style="padding:7px 12px;border-bottom:1px solid #e4e8ee;">%s</td>'
                      '<td style="padding:7px 12px;border-bottom:1px solid #e4e8ee;">%s</td>'
                      '<td style="padding:7px 12px;border-bottom:1px solid #e4e8ee;text-align:right;">%s</td>'
                      '<td style="padding:7px 12px;border-bottom:1px solid #e4e8ee;text-align:right;">%s</td></tr>'
                      % (bg, e, PRODUCT_META[p]["label"], "{:,}".format(d["total"]), user_cell))
    env_totals.append((e, env_logins, len(env_users)))

grand_users = ("{:,}".format(len(all_users))) if INCLUDE_USERS else "&mdash;"
total_row = ('<tr style="font-weight:bold;background:#eef2f8;">'
             '<td style="padding:8px 12px;border-top:2px solid #305496;" colspan="2">TOTAL</td>'
             '<td style="padding:8px 12px;border-top:2px solid #305496;text-align:right;">%s</td>'
             '<td style="padding:8px 12px;border-top:2px solid #305496;text-align:right;">%s</td></tr>'
             % ("{:,}".format(grand), grand_users))

least_html = ""
if LEAST_USED_COUNT > 0 and len(env_totals) > 1:
    least = sorted(env_totals, key=lambda x: (x[1], x[0]))[:LEAST_USED_COUNT]
    lis = ""
    for e, logins, users_ct in least:
        noun = "login" if logins == 1 else "logins"
        who = (", %s user%s" % ("{:,}".format(users_ct), "" if users_ct == 1 else "s")) if INCLUDE_USERS else ""
        lis += '<li style="margin:2px 0;"><b>%s</b> &mdash; %s %s%s</li>' % (e, "{:,}".format(logins), noun, who)
    least_html = ('<p style="margin:22px 0 6px;font-size:13px;color:#333;"><b>Least used environments</b></p>'
                  '<ol style="margin:0;padding-left:22px;font-size:13px;color:#333;">%s</ol>'
                  '<p style="margin:6px 0 0;font-size:11px;color:#888;">Quietest first, across all centres reported.</p>' % lis)

period_text = "%s &ndash; %s" % (month_start.strftime("%d %b %Y"), (month_end - timedelta(days=1)).strftime("%d %b %Y"))
sheet_note = "Summary, Daily, Users and Detail sheets" if INCLUDE_USERS else "Summary and Daily sheets"

html = """\
<html><body style="margin:0;padding:0;background:#ffffff;">
<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;max-width:720px;padding:4px 2px;">
  <h2 style="margin:0 0 2px;font-size:19px;color:#1f3864;">{title}</h2>
  <p style="margin:0 0 18px;font-size:13px;color:#666;">{month} &middot; {period} &middot; times in {tz}</p>
  <table cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;color:#222;min-width:460px;">
    <thead><tr style="background:#305496;color:#ffffff;text-align:left;">
      <th style="padding:8px 12px;font-weight:600;">Environment</th>
      <th style="padding:8px 12px;font-weight:600;">Centre</th>
      <th style="padding:8px 12px;font-weight:600;text-align:right;">Logins</th>
      <th style="padding:8px 12px;font-weight:600;text-align:right;">Distinct users</th>
    </tr></thead>
    <tbody>{rows}{total}</tbody>
  </table>
  <p style="margin:8px 0 0;font-size:11px;color:#888;">Distinct users are counted once across the whole report, so the total is not the sum of the column.</p>
  {least}
  <p style="margin:22px 0 4px;font-size:13px;color:#333;">Full detail is in the attached workbook ({sheets}).</p>
  <p style="margin:18px 0 0;padding-top:10px;border-top:1px solid #e4e8ee;font-size:11px;color:#999;">
    Source: Loki at {loki} &middot; match <code>|= "User Login"</code><br/>
    Generated {gen} &middot; automated, do not reply
  </p>
</div></body></html>
""".format(title=xml_escape(REPORT_TITLE), month=month_label, period=period_text, tz=TZ_LABEL,
           rows=rows_html, total=total_row, least=least_html, sheets=sheet_note,
           loki=LOKI_URL, gen=gen_stamp)

msg = EmailMessage()
msg["Subject"] = "%s - %s" % (REPORT_TITLE, month_label)
msg["From"] = FROM_ADDR
msg["To"] = ", ".join(TO_ADDRS)
msg["Date"] = formatdate(localtime=True)
msg.set_content("This report requires an HTML-capable mail client. See the attached Excel file.")
msg.add_alternative(html, subtype="html")
with open(xlsx_path, "rb") as f:
    msg.add_attachment(f.read(), maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=file_name)

try:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as s:
        if SMTP_TLS:
            s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print("Sent '%s' to %d recipient(s): %s" % (file_name, len(TO_ADDRS), ", ".join(TO_ADDRS)))
except Exception as ex:
    raise SystemExit("SMTP send failed via %s:%d -- %s\nIf 'relay denied', the agent's IP is "
                     "probably not allowlisted on the relay." % (SMTP_HOST, SMTP_PORT, ex))
