"""
Helpers shared by collect_logins.py and tools/setup.py.

Kept in one place so the two scripts cannot disagree about how they read
configuration, connect to SQL Server, or trust Loki. Standard library only --
pymssql is passed in by the caller, never imported here.
"""

import os
import re
import ssl
import sys
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
