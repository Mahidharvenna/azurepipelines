"""
Helpers shared by collect_logins.py and tools/setup.py.

Kept in one place so the two scripts cannot disagree about how they read
configuration, pick an ODBC driver, build a connection string, or trust Loki.
Standard library only -- pyodbc is passed in by the caller, never imported here.
"""

import os
import re
import ssl
import sys


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
# pyodbc / ODBC driver
# --------------------------------------------------------------------------
def explain_import_error(ex):
    """Turn a pyodbc ImportError into something actionable.

    On Linux the pyodbc wheel imports fine only if unixODBC's libodbc.so.2 is
    installed. When it isn't, the ImportError names that library -- and
    reporting it as "pyodbc is not installed" sends people to pip, which is
    the wrong fix.
    """
    msg = str(ex)
    if "libodbc" in msg:
        return ("pyodbc is installed but cannot load the unixODBC library (%s). An admin "
                "must install Microsoft's ODBC Driver 18 for SQL Server (msodbcsql18) on "
                "this agent; it pulls in unixODBC. See DEPLOY.md, phase 0." % msg)
    if "No module named" in msg:
        return ("pyodbc is not installed in %s. The 'Prepare Python' step installs it -- "
                "check that step's warnings (usually no route to PyPI)." % sys.executable)
    return "pyodbc failed to import: %s" % msg


DRIVER_RE = re.compile(r"^ODBC Driver (\d+) for SQL Server$")


def choose_driver(installed, requested=""):
    """Return (driver_name, problem). Exactly one of them is None.

    With no DB_ODBC_DRIVER set, pick the newest Microsoft 'ODBC Driver NN for
    SQL Server'. The Windows inbox driver literally named 'SQL Server' is never
    picked: it predates TLS 1.2 support and the connection options used here.
    """
    installed = list(installed or [])
    if requested:
        if requested in installed:
            return requested, None
        return None, ("DB_ODBC_DRIVER is '%s' but this agent has: %s. Unset DB_ODBC_DRIVER "
                      "to pick the newest automatically, or install that driver."
                      % (requested, ", ".join(installed) or "no ODBC drivers"))
    modern = sorted((int(m.group(1)), d) for d in installed for m in [DRIVER_RE.match(d)] if m)
    if not modern:
        return None, ("No Microsoft 'ODBC Driver NN for SQL Server' is installed on this agent "
                      "(found: %s). An admin must install msodbcsql18 -- see DEPLOY.md, "
                      "'Agent prerequisites'." % (", ".join(installed) or "none"))
    return modern[-1][1], None


def odbc_quote(value):
    """Brace-quote an ODBC connection-string value.

    Unquoted, a password containing ';' is cut short and the rest is parsed as
    a new keyword (login fails), and one starting with '{' breaks parsing.
    Inside braces the only escape needed is '}' -> '}}'.
    """
    return "{" + str(value).replace("}", "}}") + "}"


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


def explain_connect_error(ex, server, database):
    text = str(ex)
    low = text.lower()
    hints = []
    if ("certificate" in low or "ssl provider" in low or "ssl routines" in low
            or "tls" in low or "target principal name" in low):
        hints.append("TLS: this agent does not trust the SQL Server certificate. Quick fix: "
                     "DB_TRUST_SERVER_CERT=true in the variable group. Proper fix: install the "
                     "issuing CA in the agent's OS trust store (DEPLOY.md, phase 0).")
    if "login failed" in low or "18456" in text:
        hints.append("Credentials: SQL Server rejected the login. Check DBUSER/DBPASS in the DB "
                     "variable group.")
    if "can't open lib" in low:
        hints.append("Driver: the ODBC driver library could not be loaded. Reinstall msodbcsql18.")
    if "kerberos" in low or "gss" in low or "sspi" in low:
        hints.append("Integrated auth on Linux means Kerberos, which needs a ticket for the agent "
                     "account. Use SQL auth: DB_TRUSTED_CONNECTION=false.")
    if not hints and ("timeout" in low or "tcp provider" in low or "network" in low):
        hints.append("Network: the agent could not reach SQL Server in time.")
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
    OpenSSL bundle on Linux -- so an internal CA that 'just worked' for the
    Windows-hosted report fails here. LOKI_CA_BUNDLE is ADDED to the default
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
