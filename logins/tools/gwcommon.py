"""
Helpers shared by collect_logins.py and tools/setup.py.

Kept in one place so the two scripts cannot disagree about how they read
configuration, resolve a project's scope, connect to SQL Server, or trust
Loki. Standard library only -- pymssql is passed in by the caller, never
imported here.
"""

import os
import re
import ssl
import sys
import copy
import socket
import struct


# --------------------------------------------------------------------------
# configuration
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
    return v in ("true", "1", "yes", "y")


def split_list(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


# --------------------------------------------------------------------------
# per-project scope
# --------------------------------------------------------------------------
# Projects in one Loki don't all log alike, so each setting below can be set
# for one project: <NAME>_<KEY> replaces <NAME> for that project only. Nothing
# maps these in the YAML, and nothing needs to: ADO hands every non-secret
# variable -- variable-group ones included -- to scripts as an environment
# variable of the same name, upper-cased. So project names stay out of the repo.
SCOPED_SETTINGS = ("LOGIN_USER_REGEX", "PRODUCTS", "PRODUCT_JOBS", "PRODUCT_FRAGS",
                   "ENVS", "ENVS_EXCLUDE")
DEFAULT_ENVS = "DEV1"
DEFAULT_PRODUCTS = "pc"

# pclogs is the common case; BC/CC/CM are guesses. PRODUCT_JOBS / PRODUCT_FRAGS
# override them as comma lists of comp=value, e.g. PRODUCT_FRAGS=cm=ablog. A
# frag is matched as filename=~".*<frag>.log".
DEFAULT_PRODUCT_META = {
    "pc": {"job": "pclogs", "frag": "pc", "label": "PolicyCenter"},
    "bc": {"job": "bclogs", "frag": "bc", "label": "BillingCenter"},
    "cc": {"job": "cclogs", "frag": "cc", "label": "ClaimCenter"},
    "cm": {"job": "cmlogs", "frag": "cm", "label": "ContactManager"},
}

# Reads both 'User Login: jsmith' (also ' jsmith', '=jsmith', '- jsmith') and
# 'User Login {user="jsmith", userId=...}'.
DEFAULT_USER_REGEX = (r'(?i)User\s+Login\s*(?:\{\s*user\s*=\s*"?|[:=\-]?\s*)'
                      r'(?P<user>[A-Za-z0-9._\\@-]+)')

# Where a username should be, a Tomcat worker thread ('https-jsse-nio-8443-
# exec-9'), a timestamp or a bare number: the regex reads the wrong field of
# that project's lines.
THREAD_NAME_RE = re.compile(r"^https?-.*-exec|-exec-", re.IGNORECASE)
WRONG_FIELD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d+$")
# More than a name: a regex that reads past the username takes in the rest of
# the line -- session ids and csrf tokens among it. Such a value is never
# printed.
NOT_A_NAME_RE = re.compile(r'[\s"{}=,:]')
# A bare session id or csrf token: a long run of letters and digits with no
# separators (hex, base62), or a UUID. Thread names have dashes and words, and
# stay visible -- they are what tells an operator the regex is wrong.
OPAQUE_RE = re.compile(r"^(?:(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{20,}"
                       r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
# The value of a session/csrf/token key in the line it came from.
TOKEN_KEY = r'(?i)\b(?:j?session\w*|sess|sid|csrf\w*|\w*token|auth\w*)\s*[=:]\s*"?'
NOT_A_USERNAME = "(not a username)"


def not_a_name(name, line=None):
    """Whether an extracted value is more than a name, or a token: never
    printed. With its line, a value that line holds under a session, csrf or
    token key counts too, however short."""
    name = name or ""
    if len(name) > 64 or NOT_A_NAME_RE.search(name) or OPAQUE_RE.match(name):
        return True
    return bool(line and name and re.search(TOKEN_KEY + re.escape(name), line))


def looks_wrong_field(name, line=None):
    name = name or ""
    return bool(THREAD_NAME_RE.search(name) or WRONG_FIELD_RE.search(name)) \
        or not_a_name(name, line)


def shown_user(name, width=40, line=None):
    """What of an extracted 'username' may go into a build log: at most
    `width` characters, and nothing at all of a value shaped like more than a
    name, or like a token."""
    return NOT_A_USERNAME if not_a_name(name, line) else (name or "")[:width]


def field_example(name, width=40, line=None):
    """A wrong-field value as a warning may quote it: its first whitespace-free
    part, capped -- a thread name, never the line that follows it. '' when
    even that part holds more than a name."""
    parts = (name or "").split()
    first = parts[0] if parts else ""
    return "" if not_a_name(first, line) else first[:width]


def project_key(project):
    """The suffix of a project's own settings: upper-cased, every character
    outside A-Z 0-9 as '_' -- project_b -> PROJECT_B, a.b-c -> A_B_C."""
    return re.sub(r"[^A-Z0-9]", "_", (project or "").upper())


def key_clashes(projects):
    """[(KEY, [projects])] for projects that share a KEY: each would read the
    others' <NAME>_<KEY> settings as its own."""
    by_key = {}
    for p in projects:
        by_key.setdefault(project_key(p), []).append(p)
    return [(k, ps) for k, ps in by_key.items() if len(ps) > 1]


def scoped_source(name, project):
    """The variable that decides `name` for this project: <name>_<KEY> when
    that is set, else <name> itself, set or not. Messages name it, so they
    point at the one to change."""
    key = project_key(project)
    # ENVS_EXCLUDE and ENVS_EXCLUDE_<KEY> are a setting of their own, not the
    # ENVS of a project whose KEY is EXCLUDE or starts EXCLUDE_.
    if name == "ENVS" and (key == "EXCLUDE" or key.startswith("EXCLUDE_")):
        return name
    own = "%s_%s" % (name, key)
    return own if env(own) else name


def scoped(name, project, default=""):
    """<name>_<KEY> when set for this project -- it replaces the shared value
    outright, never merges with it -- else env(name, default)."""
    return env(scoped_source(name, project), default)


def product_meta(project):
    """DEFAULT_PRODUCT_META with this project's PRODUCT_JOBS / PRODUCT_FRAGS
    applied, on a copy. A project's own list replaces the shared list
    wholesale; whichever applies goes on top of the built-in defaults."""
    meta = copy.deepcopy(DEFAULT_PRODUCT_META)
    for name, field in (("PRODUCT_JOBS", "job"), ("PRODUCT_FRAGS", "frag")):
        for pair in split_list(scoped(name, project)):
            if "=" in pair:
                comp, value = pair.split("=", 1)
                comp = comp.strip().lower()
                if comp in meta:
                    meta[comp][field] = value.strip()
    return meta


# .NET's named group, (?<user>...), as the variable group has it -- not the
# lookbehinds (?<= and (?<!.
_NET_GROUP_RE = re.compile(r"\(\?<(?=[A-Za-z_])")


def user_regex(project):
    """(compiled, variable, pattern text) of the regex that reads usernames
    for this project. ValueError, naming the variable, if it can't be used."""
    source = scoped_source("LOGIN_USER_REGEX", project)
    text = env(source, DEFAULT_USER_REGEX)
    try:
        rx = re.compile(_NET_GROUP_RE.sub("(?P<", text))
    except re.error as ex:
        raise ValueError("%s is not a valid regex: %s" % (source, ex))
    if "user" not in rx.groupindex:
        raise ValueError("%s has no group named 'user' -- put (?<user>...) around the "
                         "username." % source)
    return rx, source, text


def project_scope(project):
    """Everything that decides what is read for one project, each setting
    resolved for it. ValueError, naming the project and every variable at
    fault, when the project can't be collected as configured -- found before
    anything runs rather than after the projects ahead of it were collected,
    and all at once rather than one per run."""
    src = dict((n, scoped_source(n, project)) for n in SCOPED_SETTINGS)
    errors = []
    envs = split_list(scoped("ENVS", project, DEFAULT_ENVS))
    if not envs:
        errors.append("%s is empty -- nothing to collect." % src["ENVS"])
    meta = product_meta(project)
    products = [p.lower() for p in split_list(scoped("PRODUCTS", project, DEFAULT_PRODUCTS))]
    if not products:
        errors.append("%s is empty -- nothing to collect." % src["PRODUCTS"])
    unknown = [p for p in products if p not in meta]
    if unknown:
        errors.append("unknown product %s in %s. Expected any of: %s."
                      % (", ".join("'%s'" % p for p in unknown), src["PRODUCTS"], ", ".join(meta)))
    rx = rx_source = None
    try:
        rx, rx_source, _text = user_regex(project)
    except ValueError as ex:
        errors.append(str(ex))
    if errors:
        raise ValueError("%s: %s." % (project, "; ".join(e.rstrip(".") for e in errors)))
    # ALL = ask Loki for this project's envs; an explicit list is used as-is,
    # so an exclude list means nothing beside it.
    discover = len(envs) == 1 and envs[0].upper() == "ALL"
    own_exclude = src["ENVS_EXCLUDE"] != "ENVS_EXCLUDE"
    return {
        "project": project,
        "key": project_key(project),
        "discover": discover,
        "envs": envs,
        "exclude": [e.upper() for e in split_list(scoped("ENVS_EXCLUDE", project))],
        "products": products,
        "meta": meta,
        "user_re": rx,
        "user_re_source": rx_source,
        "sources": src,
        "overrides": [v for n, v in src.items() if v != n and (n != "ENVS_EXCLUDE" or discover)],
        # Set but without effect: callers warn.
        "ignored": (["%s ignored: %s is an explicit list" % (src["ENVS_EXCLUDE"], src["ENVS"])]
                    if own_exclude and not discover else []),
    }


# What the pipelines' optional text parameters default to. The Run dialog of
# ADO Server 2022 marks a text parameter whose default is '' as Required and
# won't start until something is typed -- so they default to this instead.
PARAM_BLANK = "none"


def param(value):
    """A pipeline text parameter's value, with the 'none' placeholder (or '-',
    or only spaces) read as blank."""
    v = (value or "").strip()
    return "" if v.lower() in (PARAM_BLANK, "-") else v


def one_line(text):
    """ADO keeps only the first line of a ##vso[task.logissue] message, so a
    multi-line explanation would lose its hints in the run summary."""
    return " | ".join(l.strip() for l in str(text).splitlines() if l.strip())


def harden_stdio():
    """Never let an odd character in a log line crash the run (Windows cp1252
    consoles, or a Linux agent with a C locale)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


# --------------------------------------------------------------------------
# SQL Server client: pymssql
# --------------------------------------------------------------------------
# pymssql's Linux wheels carry their own SQL Server client -- FreeTDS compiled
# in, plus OpenSSL and Kerberos -- so 'pip install' is the whole install: no
# ODBC driver, no admin. The same idea as .NET's System.Data.SqlClient, which
# is why PowerShell tasks writing to this database never needed a driver.
def explain_import_error(ex):
    msg = str(ex)
    if "No module named" in msg:
        return ("pymssql is not installed in %s. The 'Prepare Python' step installs it -- "
                "check that step's warnings (usually no route to PyPI)." % sys.executable)
    return ("pymssql failed to import: %s. Its wheel bundles everything it needs, so this "
            "usually means a wheel built for another platform or Python." % msg)


def parse_server(value):
    """Mirror SQL Server's client syntax: [proto:]host[\\instance][,port].

    Returns (host, port, instance). port is None for a named instance with no
    explicit port -- the real port then comes from SQL Browser (UDP 1434), so a
    TCP probe of 1433 would blame the network for nothing.
    """
    s = (value or "").strip()
    for proto in ("tcp:", "np:", "lpc:", "admin:"):
        if s.lower().startswith(proto):
            s = s[len(proto):]
            break
    hostpart, _, port = s.partition(",")
    host, _, instance = hostpart.partition("\\")
    host = host.strip()
    instance = instance.strip()
    if host in (".", "(local)"):
        host = "localhost"
    if port.strip():
        try:
            return host, int(port.strip()), instance
        except ValueError:
            raise SystemExit("DB_SERVER '%s': port '%s' is not a number." % (value, port.strip()))
    if host.count(":") == 1:
        raise SystemExit("DB_SERVER '%s' uses host:port. SQL Server clients need host,port "
                         "(a comma) -- fix DBINSTANCE." % value)
    return host, (None if instance else 1433), instance


def tds_probe(host, port, timeout=15):
    """Send SQL Server's opening PRELOGIN packet (MS-TDS 2.2.6.5) and wait for
    any reply. Returns 'answered', 'closed', 'silent' or 'unreachable'.

    Exists because pymssql ignores login_timeout when a server accepts the TCP
    connection and then says nothing -- a firewall or proxy swallowing traffic
    -- and its connect() hangs indefinitely. Only 'silent' should be treated as
    fatal: a server that closes on us is left for pymssql to explain, so an
    imperfect probe can never block a working server.
    """
    tokens = bytes([0x00, 0x00, 11, 0x00, 6,       # VERSION    at offset 11, 6 bytes
                    0x01, 0x00, 17, 0x00, 1,       # ENCRYPTION at offset 17, 1 byte
                    0xFF])                         # terminator
    payload = tokens + bytes(6) + bytes([0x00])    # version 0.0.0.0; ENCRYPT_OFF
    packet = (bytes([0x12, 0x01]) + struct.pack(">H", 8 + len(payload))
              + bytes([0x00, 0x00, 0x01, 0x00]) + payload)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return "unreachable"
    try:
        sock.settimeout(timeout)
        sock.sendall(packet)
        try:
            data = sock.recv(8)
        except socket.timeout:
            return "silent"
        except OSError:
            return "closed"
        return "answered" if data else "closed"
    finally:
        sock.close()


def connect_sql(pymssql, server, database, user, password, trusted, encrypt,
                timeout=0, appname=None, autocommit=False):
    """Open a pymssql connection.

    encryption='require' encrypts the session but does NOT verify the server's
    certificate (FreeTDS only verifies when given a CA file). That is still a
    step up from an unencrypted SqlClient connection; DB_ENCRYPT=false turns it
    off for servers that can't negotiate TLS.
    """
    host, port, instance = parse_server(server)
    kw = dict(server=host + ("\\" + instance if instance else ""), database=database,
              login_timeout=30, timeout=timeout or 0, charset="UTF-8", appname=appname,
              tds_version="7.4", encryption="require" if encrypt else "off",
              autocommit=autocommit)
    if port:
        kw["port"] = str(port)
    if not trusted:
        kw.update(user=user, password=password)
    return pymssql.connect(**kw)


# Set on every connection. SQL Server refuses INSERT/UPDATE/MERGE on a table
# with an index on a computed column (day_ts) unless these are exactly so.
# pymssql's defaults happen to match; stating them keeps that from being luck.
SESSION_OPTIONS_SQL = ("SET QUOTED_IDENTIFIER ON; SET ANSI_NULLS ON; SET ANSI_PADDING ON; "
                       "SET ANSI_WARNINGS ON; SET ARITHABORT ON; SET CONCAT_NULL_YIELDS_NULL ON; "
                       "SET NUMERIC_ROUNDABORT OFF;")


def apply_session_options(cn):
    cur = cn.cursor()
    cur.execute(SESSION_OPTIONS_SQL)
    cur.close()


def sql_error_text(ex):
    """pymssql errors arrive as (code, b'DB-Lib error message ...') with every
    line repeated; turn that into readable, de-duplicated text."""
    parts = []

    def walk(x):
        if isinstance(x, (tuple, list)):
            for y in x:
                walk(y)
        elif isinstance(x, bytes):
            parts.append(x.decode("utf-8", "replace"))
        elif x is not None:
            parts.append(str(x))
    walk(getattr(ex, "args", ()) or (ex,))
    seen, lines = set(), []
    for chunk in parts:
        for line in chunk.replace("\\n", "\n").splitlines():
            line = line.strip()
            if line and line not in seen and not line.isdigit():
                seen.add(line)
                lines.append(line)
    return " / ".join(lines) or str(ex)


def is_login_failure(text):
    low = text.lower()
    return "login failed" in low or "18456" in text


def explain_connect_error(ex, server, database):
    text = sql_error_text(ex)
    low = text.lower()
    hints = []
    if is_login_failure(text):
        hints.append("Credentials: SQL Server rejected the login. Check DBUSER/DBPASS in the "
                     "DB variable group.")
    elif "4060" in text or "cannot open database" in low:
        hints.append("Database: the login works but cannot open '%s' -- wrong DBNAME, or the "
                     "login has no user in that database." % database)
    elif "connection refused" in low:
        hints.append("Network: nothing is listening there -- check DBINSTANCE (host, or "
                     "host,port) and that SQL Server accepts TCP connections.")
    elif ("read from the server failed" in low or "connection reset" in low
          or "unexpected eof" in low or "adaptive server connection failed" in low
          or "ssl" in low or "tls" in low):
        hints.append("Handshake: the server dropped the connection while it was being set up "
                     "-- most often an encryption (TLS) mismatch. With DB_ENCRYPT=false the "
                     "connection is unencrypted, like a default SqlClient connection.")
    elif "unable to connect" in low or "adaptive server is unavailable" in low:
        hints.append("Network: SQL Server could not be reached -- the host doesn't resolve, a "
                     "firewall is in the way, or DBINSTANCE is wrong.")
    if "kerberos" in low or "gss" in low:
        hints.append("Integrated auth on Linux means Kerberos, which needs a ticket for the "
                     "agent account. Use SQL auth: DB_TRUSTED_CONNECTION=false.")
    out = "Could not connect to %s / %s:\n  %s" % (server, database, text)
    if hints:
        out += "\n  -> " + "\n  -> ".join(hints)
    return out


# --------------------------------------------------------------------------
# Loki TLS
# --------------------------------------------------------------------------
def loki_ssl_context(verify, ca_bundle, warn):
    """Build the TLS context for Loki calls.

    Python trusts the Windows certificate store on Windows, but only the
    OpenSSL bundle on Linux -- so an internal CA that 'just worked' for a
    Windows-hosted job fails here. LOKI_CA_BUNDLE is ADDED to the default
    trust rather than replacing it, and a path that doesn't exist on this agent
    is ignored with a warning, so one shared variable group can't break an
    agent of the other OS. An unreadable or non-PEM file is also ignored with a
    warning rather than crashing the job before its first log line.
    """
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if ca_bundle:
        if not os.path.isfile(ca_bundle):
            warn("LOKI_CA_BUNDLE '%s' does not exist on this agent -- ignored." % ca_bundle)
        else:
            try:
                ctx.load_verify_locations(cafile=ca_bundle)
            except ssl.SSLError as ex:      # must precede OSError: SSLError subclasses it
                warn("LOKI_CA_BUNDLE '%s' holds no PEM certificate (%s) -- ignored. A DER .cer "
                     "converts with: openssl x509 -inform der -in ca.cer -out ca.pem"
                     % (ca_bundle, ex))
            except OSError as ex:
                warn("LOKI_CA_BUNDLE '%s' cannot be read by the agent account (%s) -- ignored. "
                     "Make it readable (chmod 644)." % (ca_bundle, ex))
    return ctx


def is_cert_error(ex):
    s = str(ex)
    return "CERTIFICATE_VERIFY_FAILED" in s or "certificate verify failed" in s


LOKI_CERT_HINT = ("Loki's certificate is not trusted on this agent. Linux Python uses the OpenSSL "
                  "bundle, not the Windows store. Install the issuing CA in the OS trust store, or "
                  "set LOKI_CA_BUNDLE to its .pem in the variable group.")
