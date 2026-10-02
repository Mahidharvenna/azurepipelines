#!/usr/bin/env python3
"""
Load one Guidewire admin-data XML file into one environment through the
server's ImportToolsAPI web service -- the SOAP API behind import_tools.

    --action check     reach the server, read its ImportToolsAPI WSDL and, when
                       credentials are set, log in with ImportToolsAPI's
                       xmlToCsv, which only converts text; writes nothing
    --action validate  parse the XML file and describe it; writes nothing
    --action import    check + validate, then send the file

Everything about the call -- namespace, operation, element names, SOAPAction,
endpoint -- is read from the server's own WSDL, because the Guidewire version
is not known and may differ between environments. Nothing here hard-codes
what the WSDL can say.

Standard library only (the agent has no route to PyPI); Python 3.9+.
Configuration and secrets come only from ADL_* environment variables, which
the pipeline maps from the variable group -- see README.md.
Exit codes: 0 ok, 1 failed, 2 refused (arguments, configuration, safety).
"""

import argparse
import base64
import codecs
import collections
import hashlib
import http.client
import os
import re
import socket
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

# The team says cm; the ContactManager web app's context is ab.
APP_CONTEXT = {"pc": "pc", "bc": "bc", "cc": "cc", "cm": "ab"}
PRODUCT_NAME = {"pc": "PolicyCenter", "bc": "BillingCenter", "cc": "ClaimCenter",
                "cm": "ContactManager"}
# WS-I services first; the second path is where older releases publish them.
WSDL_PATHS = ("/ws/gw/wsi/pl/ImportToolsAPI", "/ws/gw/webservice/pl/ImportToolsAPI")
# Preference order when GW_IMPORT_OPERATION is not set.
IMPORT_OPERATIONS = ("importXmlData", "importXml", "importData")
# check's login test: read-only, published next to ImportToolsAPI.
# check's login test: ImportToolsAPI.xmlToCsv -- the import's own service and
# permission, and it only converts text. SystemToolsAPI.getVersion only when
# the WSDL has no xmlToCsv: it can refuse a user that may import.
LOGIN_OPERATION = "xmlToCsv"
PROBE_SERVICE, PROBE_OPERATION = "SystemToolsAPI", "getVersion"
MAX_FILE_BYTES = 200 * 1024 * 1024
# Guidewire's default MaximumFileUploadSize (config.xml) for admin imports.
GW_UPLOAD_DEFAULT_BYTES = 20 * 1024 * 1024
MAX_WSDL_BYTES = 10 * 1024 * 1024
MAX_WSDL_DOCS = 25
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
# The import reply is parsed as it arrives and Details entries are counted,
# not kept (one per imported record), so it may be far larger than the rest.
MAX_IMPORT_REPLY_BYTES = 512 * 1024 * 1024
DETAILS_SHOWN = 10
MAX_RESULT_LINES = 200
MAX_ERRORLOG_LINES = 2000
DEFAULT_PROD_PATTERN = r"^(PROD|PRD|PRODUCTION)"
ENV_RE = re.compile(r"[A-Za-z0-9_]{1,32}\Z")
# Env names that are variable names in this scheme: env PASSWORD would make
# the pipeline read the "URL" from $(PASSWORD), the shared secret.
# Env names that would make the URL variable collide with a credential, a step
# key the pipeline maps (ADL_*), a setting, or a variable the pipeline sets.
RESERVED_ENV = re.compile(
    r"(NONE|USERNAME|PASSWORD|.*_USERNAME|.*_PASSWORD|ADL_.*|PYTHON_EXE|"
    r"GW_VERIFY_TLS|GW_CA_BUNDLE|BYPASS_PROXY|GW_TIMEOUT|GW_AUTH|GW_IMPORT_OPERATION|"
    r"PROD_ENV_PATTERN|GW_ALLOW_HTTP|GW_ALLOW_CONTEXT_MISMATCH)\Z", re.IGNORECASE)
# Top-level element names known as admin data. Anything else only warns:
# names vary by product and version.
ADMIN_ENTITIES = {"user", "usercontact", "credential", "userrole", "usersettings", "group",
                  "groupuser", "groupregion", "role", "roleprivilege", "activitypattern",
                  "region", "regionzone", "zone", "securityzone", "organization", "address",
                  "assignablequeue", "businessweek"}
ADMIN_ENTITY_PREFIXES = ("authoritylimit", "attribute", "holiday", "userattribute",
                         "producercode", "uwauthority", "uwissuetype")
CHUNK = 1024 * 1024

NS_WSDL = "http://schemas.xmlsoap.org/wsdl/"
NS_WSDL_SOAP11 = "http://schemas.xmlsoap.org/wsdl/soap/"
NS_WSDL_SOAP12 = "http://schemas.xmlsoap.org/wsdl/soap12/"
NS_XSD = "http://www.w3.org/2001/XMLSchema"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
NS_SOAP11_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
NS_SOAP12_ENV = "http://www.w3.org/2003/05/soap-envelope"
# Guidewire's SOAP authentication header. Used only when the WSDL does not
# declare an authentication header of its own.
NS_GWSOAP = "http://guidewire.com/ws/soapheaders"


class Refused(Exception):
    """Bad arguments or configuration, or a safety rule: exit 2, nothing sent."""


class Failed(Exception):
    """Connectivity, WSDL, SOAP fault, import errors, malformed XML: exit 1."""


class DoctypeFound(Exception):
    pass


def q(ns, local):
    return "{%s}%s" % (ns, local) if ns else local


def split_tag(tag):
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


def local_name(tag):
    return split_tag(tag)[1]


def qn_text(qname):
    ns, local = qname
    return "{%s}%s" % (ns, local) if ns else local


# --------------------------------------------------------------------------
# output: every line is sanitised -- server text and file names included
# --------------------------------------------------------------------------
_REDACT = []
_EOL = re.compile(r"[\t\r\n]")
# C0/C1 controls, line/paragraph separators, bidi overrides.
_CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
# '##vso[' and '##[' are agent commands wherever they appear in a line.
_ADO_CMD = re.compile(r"#(?=#(?:vso)?\[)", re.IGNORECASE)


def add_secret(value):
    if value and len(value) >= 3 and value not in _REDACT:
        _REDACT.append(value)
        _REDACT.sort(key=len, reverse=True)


def clean(value, limit=1000):
    s = "" if value is None else str(value)
    for secret in _REDACT:
        s = s.replace(secret, "***")
    s = _CTRL.sub("", _EOL.sub(" ", s))
    if len(s) > limit:
        s = s[:limit] + "...(%d more characters)" % (len(s) - limit)
    return _ADO_CMD.sub("#_", s)


def say(msg=""):
    print(clean(msg, 4000), flush=True)


def warn(msg):
    print("##vso[task.logissue type=warning]" + clean(msg, 4000), flush=True)


def error(msg):
    print("##vso[task.logissue type=error]" + clean(msg, 4000), flush=True)


def section(title):
    say("")
    say("== %s %s" % (title, "=" * max(3, 66 - len(title))))


def field(name, value):
    say("  %-17s: %s" % (name, value))


def human_bytes(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "bytes" else ("%.1f %s" % (n, unit))
        n /= 1024.0


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
# A variable the pipeline maps but the group does not define arrives as the
# literal text "$(NAME)".
_UNSET_MACRO = re.compile(r"^\$\([^()]*\)$")


def env(name, default="", strip=True):
    raw = os.environ.get(name)
    if raw is None:
        return default
    s = raw.strip()
    if not s or _UNSET_MACRO.match(s):
        return default
    return s if strip else raw


def setting(name, default=""):
    """Group variable NAME, which the pipeline hands over as ADL_NAME: a step
    env key no group variable has, so a plain variable of the same name cannot
    overwrite the mapping (the agent exports public variables after env:)."""
    return env("ADL_" + name, default)


def setting_bool(name, default):
    v = setting(name).lower()
    if not v:
        return default
    if v in ("true", "1", "yes", "y", "on"):
        return True
    if v in ("false", "0", "no", "n", "off"):
        return False
    # Strict: a typo in GW_VERIFY_TLS must not silently switch checking off.
    raise Refused("%s must be true or false, not '%s'." % (name, clean(v, 40)))


def none_to_blank(value):
    v = (value or "").strip()
    return "" if v.lower() == "none" else v


class Config(object):
    pass


def build_ssl_context(verify, ca_bundle):
    """Linux Python trusts only the OpenSSL bundle, not the Windows store, so an
    internal CA usually needs GW_CA_BUNDLE. It is ADDED to the default trust; a
    path missing on this agent is ignored with a warning."""
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if ca_bundle:
        if not os.path.isfile(ca_bundle):
            warn("GW_CA_BUNDLE '%s' does not exist on this agent -- ignored." % ca_bundle)
        else:
            try:
                ctx.load_verify_locations(cafile=ca_bundle)
            except ssl.SSLError as ex:      # before OSError: SSLError subclasses it
                warn("GW_CA_BUNDLE '%s' holds no PEM certificate (%s) -- ignored. A DER .cer "
                     "converts with: openssl x509 -inform der -in ca.cer -out ca.pem"
                     % (ca_bundle, ex))
            except OSError as ex:
                warn("GW_CA_BUNDLE '%s' cannot be read by the agent account (%s) -- ignored."
                     % (ca_bundle, ex))
    return ctx


def resolve_file(raw):
    """The file, as a real path inside the repo (= the current directory)."""
    raw = none_to_blank(raw)
    if not raw:
        raise Refused("No file chosen: set 'file' in the Run dialog to the XML file's path "
                      "from the repo root, e.g. admin-data/qa3/roles.xml.")
    raw = raw.replace("\\", "/")
    root = os.path.realpath(os.getcwd())
    path = raw if os.path.isabs(raw) else os.path.join(root, raw)
    real = os.path.realpath(path)
    if os.path.commonpath([root, real]) != root:
        raise Refused("'%s' resolves to a path outside the repository (symlinks included). "
                      "Only files in the checked-out repo can be loaded; give the path from "
                      "the repo root." % raw)
    rel = os.path.relpath(real, root)
    if rel == "." or rel.split(os.sep)[0] == ".git":
        raise Refused("'%s' is not a file in the repository." % raw)
    if not os.path.exists(real):
        raise Refused("'%s' does not exist in the checked-out repo. Is it committed (git add) "
                      "on the branch this run used?" % raw)
    if not os.path.isfile(real):
        raise Refused("'%s' is not a regular file." % raw)
    if not real.lower().endswith(".xml"):
        raise Refused("'%s' is not an .xml file. Admin data is loaded from XML only." % rel)
    size = os.path.getsize(real)
    if size == 0:
        raise Refused("'%s' is empty." % rel)
    if size > MAX_FILE_BYTES:
        raise Refused("'%s' is %s; the limit is %s. Split it into smaller files and load them "
                      "one per run." % (rel, human_bytes(size), human_bytes(MAX_FILE_BYTES)))
    return real, rel, size


def example_url(cfg):
    return "https://gw-%s-%s.example.internal:9443/%s" % (
        cfg.env.lower().replace("_", ""), cfg.product, cfg.context)


def parse_base_url(raw, cfg):
    """Never prints raw, not even in part, when refusing it: if env collided
    with another variable's name the value could be anything, a secret
    included. The message names only the variable it came from."""
    src = cfg.url_src

    def refuse(why, example=True):
        add_secret(raw)
        raise Refused("The URL in %s %s. (Its value is not shown.)%s"
                      % (src, why, " Expected the app's base URL, e.g. %s" % example_url(cfg)
                         if example else ""))
    try:
        u = urllib.parse.urlsplit(raw)
        port = u.port
    except ValueError:
        refuse("cannot be read as a URL, or has an invalid port")
    scheme = (u.scheme or "").lower()
    if scheme not in ("http", "https") or not u.hostname:
        refuse("is not an https URL with a host")
    if u.username or u.password:
        refuse("contains credentials (user@ or user:password@); keep those in username and "
               "password")
    if u.query or u.fragment:
        refuse("has a ?query or #fragment")
    if "/ws/" in u.path.rstrip("/") + "/":
        refuse("is a web-service URL; give the app's base URL, the tool finds ImportToolsAPI "
               "under it")
    if scheme == "http" and not cfg.allow_http:
        refuse("is http://, which would send the password in clear text. Use https, or set "
               "GW_ALLOW_HTTP=true for a server that has no TLS")
    app_path = u.path.rstrip("/")
    last = app_path.rsplit("/", 1)[-1] if app_path else ""
    mismatch = last.lower() != cfg.context
    if mismatch and not cfg.allow_context_mismatch:
        fix = ("%s is the environment's URL; add %s with the %s URL (e.g. %s), which takes "
               "precedence for product %s" % (src, cfg.url_var_product, PRODUCT_NAME[cfg.product],
                                              example_url(cfg), cfg.product)
               if src == cfg.url_var_env else "Correct %s" % src)
        refuse("does not end in /%s, where product %s (%s) is served -- this guard stops %s "
               "data going into another application. %s, or set GW_ALLOW_CONTEXT_MISMATCH=true "
               "if the app really is deployed under another context"
               % (cfg.context, cfg.product, PRODUCT_NAME[cfg.product], PRODUCT_NAME[cfg.product],
                  fix), example=False)
    cfg.scheme = scheme
    cfg.host = u.hostname
    cfg.port = port or (443 if scheme == "https" else 80)
    cfg.netloc = u.netloc.lower()
    cfg.app_path = app_path
    cfg.base_url = "%s://%s%s" % (scheme, cfg.netloc, cfg.app_path)
    # Accepted: print scheme://host:port and path, nothing else.
    host = "[%s]" % cfg.host if ":" in cfg.host else cfg.host
    cfg.shown_url = "%s://%s:%d%s" % (scheme, host, cfg.port, cfg.app_path)
    if mismatch:
        warn("The URL in %s ends in /%s, not /%s (product %s). Continuing: "
             "GW_ALLOW_CONTEXT_MISMATCH=true." % (src, last, cfg.context, cfg.product))


def load_config(args):
    cfg = Config()
    cfg.action = args.action
    # env first: it builds the variable names everything else is read from, so
    # a bad one stops the run before any of those values is looked at.
    cfg.env = (args.env or "").strip()
    if cfg.env.lower() in ("", "none"):
        raise Refused("No environment chosen: 'env' is 'none'. Type the environment name in "
                      "the Run dialog, e.g. DEV_1.")
    if not ENV_RE.match(cfg.env):
        raise Refused("Environment '%s' is not valid: use 1-32 letters, digits or _ (e.g. DEV_1)."
                      % clean(cfg.env, 60))
    if RESERVED_ENV.match(cfg.env):
        raise Refused("Environment '%s' is reserved: it is the name of a credential "
                      "(USERNAME, PASSWORD, *_USERNAME, *_PASSWORD), a pipeline step key "
                      "(ADL_*), a setting (GW_TIMEOUT, PROD_ENV_PATTERN, ...) or PYTHON_EXE, so "
                      "the pipeline would read the server URL from that variable. Type the "
                      "environment's name, e.g. DEV_1." % cfg.env)
    cfg.product = args.product
    cfg.context = APP_CONTEXT[cfg.product]
    cfg.confirm = none_to_blank(args.confirm)
    ENV, PROD = cfg.env.upper(), cfg.product.upper()
    cfg.url_var_env, cfg.url_var_product = ENV, "%s_%s" % (ENV, PROD)
    cfg.user_var_env, cfg.password_var_env = ENV + "_USERNAME", ENV + "_PASSWORD"

    pattern = setting("PROD_ENV_PATTERN")
    cfg.prod_pattern_src = "PROD_ENV_PATTERN" if pattern else "default"
    pattern = pattern or DEFAULT_PROD_PATTERN
    try:
        cfg.is_prod = bool(re.search(pattern, cfg.env, re.IGNORECASE))
    except re.error as ex:
        raise Refused("PROD_ENV_PATTERN '%s' is not a valid regular expression (%s), so it "
                      "cannot tell whether %s is production." % (pattern, ex, cfg.env))
    cfg.prod_pattern = pattern

    cfg.verify_tls = setting_bool("GW_VERIFY_TLS", True)
    cfg.ca_bundle = setting("GW_CA_BUNDLE")
    cfg.bypass_proxy = setting_bool("BYPASS_PROXY", True)
    cfg.allow_http = setting_bool("GW_ALLOW_HTTP", False)
    cfg.allow_context_mismatch = setting_bool("GW_ALLOW_CONTEXT_MISMATCH", False)
    timeout = setting("GW_TIMEOUT", "600")
    if not re.match(r"[0-9]{1,5}\Z", timeout) or not 1 <= int(timeout) <= 86400:
        raise Refused("GW_TIMEOUT must be a whole number of seconds from 1 to 86400, not '%s'."
                      % clean(timeout, 40))
    cfg.timeout = int(timeout)
    # One method per request by default: Guidewire refuses a request that
    # carries both ("Multiple authentication methods provided").
    cfg.auth = setting("GW_AUTH", "auto").lower()
    if cfg.auth not in ("auto", "basic", "header", "both"):
        raise Refused("GW_AUTH must be auto, basic, header or both, not '%s'."
                      % clean(cfg.auth, 40))
    cfg.op_override = setting("GW_IMPORT_OPERATION")

    # Credentials: <ENV>_USERNAME + <ENV>_PASSWORD together, else the shared
    # username + password. Half a pair is a mistake, not a fallback: the user
    # of one login with the password of another. Never printed.
    user_env, pw_env = env("ADL_USER_ENV"), env("ADL_PASSWORD_ENV", strip=False)
    add_secret(pw_env)
    if bool(user_env) != bool(pw_env):
        have, lack = ((cfg.user_var_env, cfg.password_var_env) if user_env
                      else (cfg.password_var_env, cfg.user_var_env))
        raise Refused("%s is set but %s is not. This environment's own login needs both (%s, "
                      "and %s as a secret); remove %s to use the shared username and password."
                      % (have, lack, cfg.user_var_env, cfg.password_var_env, have))
    if user_env:
        cfg.user, cfg.password = user_env, pw_env
        cfg.cred_src = "%s / %s (this environment's own)" % (cfg.user_var_env,
                                                              cfg.password_var_env)
    else:
        cfg.user, cfg.password = env("ADL_USER"), env("ADL_PASSWORD", strip=False)
        cfg.cred_src = "username / password (shared)"
    add_secret(cfg.password)
    if cfg.password and cfg.password != cfg.password.strip():
        # A pasted value often brings a trailing space; Guidewire then says
        # only "Bad username or password".
        warn("The password (%s) starts or ends with a space. If that isn't part of it, "
             "re-type it in gw-admin-data." % cfg.cred_src)
    # As sent in the SOAP header, so a server that echoes the request can't
    # show it either.
    if cfg.password:
        add_secret(xml_text(cfg.password))
        add_secret(xml_attr(cfg.password))
    cfg.basic_token = ""
    if cfg.user and cfg.password:
        cfg.basic_token = base64.b64encode(
            ("%s:%s" % (cfg.user, cfg.password)).encode("utf-8")).decode("ascii")
        add_secret(cfg.basic_token)
    cfg.has_credentials = bool(cfg.user and cfg.password)
    cfg.cred_missing = [n for n, v in (("username", cfg.user), ("password", cfg.password))
                        if not v]
    if cfg.action == "import" and not cfg.has_credentials:
        raise Refused("Import needs credentials, and no %s is set. Add the shared username and "
                      "password (secret) to the variable group, or %s and %s (secret) for %s "
                      "only." % (" or ".join(cfg.cred_missing), cfg.user_var_env,
                                 cfg.password_var_env, cfg.env))

    # The URL: <ENV>_<PRODUCT> when set, else <ENV>; each includes the app path.
    raw_url, cfg.url_src = env("ADL_URL_PRODUCT"), cfg.url_var_product
    if not raw_url:
        raw_url, cfg.url_src = env("ADL_URL_ENV"), cfg.url_var_env
    cfg.has_url = bool(raw_url)
    if raw_url:
        parse_base_url(raw_url, cfg)
    elif cfg.action != "validate":
        raise Refused("No server URL: the variable group has neither %s nor %s (or the pipeline "
                      "is not allowed to use the group). Add %s = the app's base URL, e.g. %s; "
                      "for one product only, %s takes precedence."
                      % (cfg.url_var_product, cfg.url_var_env, cfg.url_var_env, example_url(cfg),
                         cfg.url_var_product))

    cfg.file = cfg.file_rel = None
    cfg.file_size = 0
    if cfg.action in ("validate", "import"):
        cfg.file, cfg.file_rel, cfg.file_size = resolve_file(args.file)

    cfg.ssl_ctx = build_ssl_context(cfg.verify_tls, cfg.ca_bundle)
    return cfg


def print_config(cfg):
    section("Settings")
    field("action", cfg.action)
    field("environment", cfg.env)
    # Always shown, so a PROD_ENV_PATTERN that misses production is visible.
    field("production", "%s -- %s %s PROD_ENV_PATTERN %s (%s)%s"
          % ("YES" if cfg.is_prod else "no", cfg.env,
             "matches" if cfg.is_prod else "does not match", cfg.prod_pattern,
             cfg.prod_pattern_src, "; import needs confirm" if cfg.is_prod else ""))
    field("product", "%s (%s, app context /%s)"
          % (cfg.product, PRODUCT_NAME[cfg.product], cfg.context))
    if cfg.has_url:
        field("server", "%s  (from %s%s)"
              % (cfg.shown_url, cfg.url_src, "; %s not set" % cfg.url_var_product
                 if cfg.url_src == cfg.url_var_env else ""))
    else:
        field("server", "neither %s nor %s is set (validate does not need it)"
              % (cfg.url_var_product, cfg.url_var_env))
    if cfg.action == "check":
        field("file", "not used by check")
    else:
        field("file", cfg.file_rel)
    if cfg.has_credentials:
        field("credentials", cfg.cred_src)
    elif cfg.user or cfg.password:
        field("credentials", "incomplete: %s set, %s not -- check cannot log in"
              % ("username" if cfg.user else "password", " or ".join(cfg.cred_missing)))
    else:
        field("credentials", "not set (validate does not need them; check then does not log "
                             "in; import needs them)")
    if cfg.has_url:
        if cfg.scheme == "https":
            field("TLS verify", ("on" + (" + GW_CA_BUNDLE" if cfg.ca_bundle else ""))
                  if cfg.verify_tls else "OFF (GW_VERIFY_TLS=false)")
        field("proxy", "bypassed" if cfg.bypass_proxy else "system settings")
        field("timeout", "%d s" % cfg.timeout)
        field("auth", {"auto": "auto: the Guidewire SOAP header when the WSDL declares it, "
                               "else HTTP Basic",
                       "basic": "HTTP Basic", "header": "Guidewire SOAP header",
                       "both": "HTTP Basic + Guidewire SOAP header"}[cfg.auth])
    if cfg.has_url and not cfg.verify_tls and cfg.scheme == "https":
        warn("GW_VERIFY_TLS is false: the server's certificate is not checked in this run.")
    if cfg.has_url and cfg.scheme == "http":
        warn("The server URL is plain http: credentials cross the network unencrypted.")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would replay the Authorization header to
    wherever it points. A 3xx comes back as an HTTPError instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def describe_network_error(reason, where):
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
        detail = getattr(reason, "verify_message", "") or str(reason)
        return ("%s: the server's TLS certificate is not trusted on this agent (%s). Linux Python "
                "uses the OpenSSL bundle, not the Windows store: set GW_CA_BUNDLE to the issuing "
                "CA's .pem, or install the CA in the agent's OS trust store. GW_VERIFY_TLS=false "
                "switches checking off (testing only)." % (where, detail))
    if isinstance(reason, socket.timeout) or "timed out" in str(reason):
        return "%s: timed out." % where
    if isinstance(reason, ConnectionRefusedError):
        return ("%s: connection refused -- nothing is listening on that port, or a firewall "
                "rejects the agent." % where)
    if isinstance(reason, socket.gaierror):
        return "%s: the host name does not resolve on this agent (%s)." % (where, reason)
    return "%s: %s" % (where, reason)


class Http(object):
    def __init__(self, cfg):
        handlers = [urllib.request.HTTPSHandler(context=cfg.ssl_ctx), _NoRedirect()]
        if cfg.bypass_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        self.timeout = cfg.timeout

    @staticmethod
    def _read(resp, limit):
        data = resp.read(limit + 1)
        if len(data) > limit:
            raise Failed("The response is larger than %s; not reading it." % human_bytes(limit))
        return data

    def open(self, url, data=None, headers=None, timeout=None, what="request"):
        """(status, reason, headers, response) with the body unread. HTTP error
        statuses are returned, not raised; network failures raise Failed."""
        req = urllib.request.Request(url, data=data, headers=headers or {},
                                     method="POST" if data is not None else "GET")
        try:
            resp = self.opener.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as ex:
            return ex.code, ex.reason, ex.headers, ex
        except urllib.error.URLError as ex:
            raise Failed(describe_network_error(ex.reason, what))
        except (OSError, http.client.HTTPException) as ex:
            raise Failed(describe_network_error(ex, what))
        return resp.status, resp.reason, resp.headers, resp

    def request(self, url, data=None, headers=None, timeout=None, limit=MAX_RESPONSE_BYTES,
                what="request"):
        """(status, reason, headers, body), the body read whole."""
        status, reason, rh, resp = self.open(url, data, headers, timeout, what)
        try:
            with resp:
                return status, reason, rh, self._read(resp, limit)
        except (OSError, http.client.HTTPException, Failed) as ex:
            if status >= 400:
                return status, reason, rh, b""
            if isinstance(ex, Failed):
                raise
            raise Failed(describe_network_error(ex, what + " (reading the response)"))

    def soap(self, url, body, headers, timeout, limit, what):
        """POST a SOAP request; the reply is parsed as it arrives (Reply)."""
        status, reason, rh, resp = self.open(url, body, headers, timeout, what)
        r = Reply(status, reason)
        target = _ReplyTarget()
        parser = ET.XMLParser(target=target)
        try:
            with resp:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    if len(r.head) < 2000:
                        r.head += chunk[:2000 - len(r.head)]
                    r.size += len(chunk)
                    if r.size > limit:
                        r.problem = "the reply is larger than %s; stopped reading it" \
                                    % human_bytes(limit)
                        return r
                    parser.feed(chunk)
            # http.client returns a short body, not an error, when the
            # connection closes early.
            length = (rh.get("Content-Length") or "").strip() if rh else ""
            if length.isdigit() and int(length) > r.size:
                r.problem = "the reply was cut off after %d of %s bytes" % (r.size, length)
                return r
            r.root = parser.close()
        except DoctypeFound:
            r.problem = "the reply contains a DOCTYPE declaration; not parsed"
        except ET.ParseError as ex:
            r.problem = "the reply is not well-formed XML (%s)" % ex
        except (OSError, http.client.HTTPException) as ex:
            r.problem = describe_network_error(ex, "reading the reply")
        r.details_counts = target.details_counts
        r.skipped_problems = target.problems
        return r


class Reply(object):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason
        self.root, self.head, self.size, self.problem = None, b"", 0, ""
        self.details_counts, self.skipped_problems = {}, []

    def start(self):
        return clean(self.head[:500].decode("utf-8", "replace"), 500)


class _ReplyTarget(object):
    """Builds the reply's tree but keeps only the first DETAILS_SHOWN entries
    of a Details list, counting the rest: there is one per imported record,
    which would otherwise fill memory and the log. Skipped entries are still
    searched for error-like fields, so problem detection covers everything."""

    def __init__(self):
        self.tb = ET.TreeBuilder()
        self.path = []              # (element or None, local name) per open element
        self.skip = 0               # depth inside a skipped entry
        self.skip_stack = []        # [name, text parts] inside it
        self.details_counts = {}    # Details element -> number of entries
        self.problems = []

    def start(self, tag, attrs):
        name = local_name(tag)
        parent = self.path[-1][0] if self.path else None
        if self.skip:
            self.skip += 1
            self.skip_stack.append([name, []])
            return None
        if parent is not None and parent in self.details_counts:
            self.details_counts[parent] += 1
            if self.details_counts[parent] > DETAILS_SHOWN:
                self.skip = 1
                self.skip_stack = [[name, []]]
                self.path.append((None, name))
                return None
        el = self.tb.start(tag, attrs)
        if name.lower() == "details":
            self.details_counts[el] = 0
        self.path.append((el, name))
        return el

    def end(self, tag):
        if self.skip:
            name, parts = self.skip_stack.pop()
            text = " ".join(t.strip() for t in parts if t.strip())
            if self.skip_stack:
                self.skip_stack[-1][1].append(text)
            if len(self.problems) < 20 and (
                    (_ERRORISH.search(name) and text.lower() not in _EMPTYISH) or
                    (name.lower() in _OKISH and text.lower() == "false")):
                self.problems.append(("Details.%s" % name, text))
            self.skip -= 1
            if not self.skip:
                self.path.pop()
            return None
        self.path.pop()
        return self.tb.end(tag)

    def data(self, text):
        if self.skip:
            parts = self.skip_stack[-1][1]
            if sum(len(p) for p in parts) < 2000:
                parts.append(text)
        else:
            self.tb.data(text)

    def doctype(self, name, pubid, system):
        raise DoctypeFound()

    def close(self):
        return self.tb.close()


def origin_of(url):
    u = urllib.parse.urlsplit(url)
    scheme = (u.scheme or "").lower()
    try:
        port = u.port
    except ValueError:
        port = None
    return scheme, (u.hostname or "").lower(), port or {"https": 443, "http": 80}.get(scheme)


def same_server(cfg, url, what):
    """url, on the configured server. A path with '/ws/' keeps only that part,
    appended to the configured base URL: a WSDL generated behind a proxy names
    the app server's own host and path. Anything else on another host moves
    to the configured scheme, host and port. Credentials only ever go there.
    (url, changed)."""
    u = urllib.parse.urlsplit(url)
    if (u.scheme or "").lower() not in ("http", "https") or not u.hostname:
        raise Failed("%s '%s' is not an http(s) URL." % (what, url))
    if "/ws/" in u.path:
        moved = cfg.base_url + "/ws/" + u.path.split("/ws/", 1)[1] + \
            ("?" + u.query if u.query else "")
        return moved, moved != urllib.parse.urldefrag(url)[0]
    if origin_of(url) == (cfg.scheme, cfg.host.lower(), cfg.port):
        return url, False
    moved = urllib.parse.urlunsplit((cfg.scheme, cfg.netloc, u.path, u.query, ""))
    return moved, True


# --------------------------------------------------------------------------
# XML parsing (namespace-aware, no DOCTYPE)
# --------------------------------------------------------------------------
class _NsTreeTarget(object):
    """Builds an ElementTree and records the prefixes in scope at each element:
    WSDL and XSD attributes such as element="tns:x" need them, and
    ElementTree otherwise discards them."""

    def __init__(self, nsmaps):
        self.tb = ET.TreeBuilder()
        self.stack = [{"xml": "http://www.w3.org/XML/1998/namespace"}]
        self.pending = {}
        self.nsmaps = nsmaps

    def start_ns(self, prefix, uri):
        self.pending[prefix or ""] = uri

    def start(self, tag, attrs):
        el = self.tb.start(tag, attrs)
        scope = self.stack[-1]
        if self.pending:
            scope = dict(scope)
            scope.update(self.pending)
            self.pending = {}
        self.stack.append(scope)
        self.nsmaps[el] = scope
        return el

    def end(self, tag):
        self.stack.pop()
        return self.tb.end(tag)

    def data(self, text):
        self.tb.data(text)

    def doctype(self, name, pubid, system):
        # No WSDL, schema or SOAP response needs one; entity tricks do.
        raise DoctypeFound()

    def close(self):
        return self.tb.close()


def parse_xml(data, nsmaps, what):
    parser = ET.XMLParser(target=_NsTreeTarget(nsmaps))
    try:
        parser.feed(data)
        return parser.close()
    except DoctypeFound:
        raise Failed("%s contains a DOCTYPE declaration; refusing to parse it." % what)
    except ET.ParseError as ex:
        raise Failed("%s is not well-formed XML (%s). It starts: %s"
                     % (what, ex, clean(data[:200].decode("utf-8", "replace"), 200)))


def resolve_qname(nsmaps, el, value):
    value = (value or "").strip()
    prefix, local = value.split(":", 1) if ":" in value else ("", value)
    scope = nsmaps.get(el, {})
    if prefix and prefix not in scope:
        raise Failed("The WSDL uses an undeclared namespace prefix in '%s'." % value)
    return scope.get(prefix, ""), local


# --------------------------------------------------------------------------
# WSDL
# --------------------------------------------------------------------------
Param = collections.namedtuple("Param", "name ns type_text is_string min_occurs max_occurs "
                                        "qualified")
Port = collections.namedtuple("Port", "service name binding address soap")


def _occurs(el):
    raw = (el.get("minOccurs") or "1").strip()
    return (int(raw) if re.match(r"[0-9]{1,9}\Z", raw) else 1), el.get("maxOccurs", "1")


class Schema(object):
    def __init__(self, tns, qualified):
        self.tns = tns
        self.qualified = qualified


class Wsdl(object):
    """The parts of one or more WSDL 1.1 documents and their schemas that
    building a document/literal call needs."""

    def __init__(self):
        self.nsmaps = {}
        self.wsdl_docs = []        # (url, root)
        self.schemas = []          # (schema element, url, inherited tns)
        self.fetched = []          # urls of extra documents
        self.unfetched = []        # (url, why) of imports that could not be read
        self.target_ns = ""
        self.messages = {}
        self.port_types = {}
        self.bindings = {}
        self.ports = []
        self.elements = {}
        self.ctypes = {}
        self.stypes = {}

    def index(self):
        for _url, root in self.wsdl_docs:
            tns = root.get("targetNamespace", "")
            for msg in root.findall(q(NS_WSDL, "message")):
                parts = []
                for part in msg.findall(q(NS_WSDL, "part")):
                    parts.append({
                        "name": part.get("name", ""),
                        "element": resolve_qname(self.nsmaps, part, part.get("element"))
                        if part.get("element") else None,
                        "type": resolve_qname(self.nsmaps, part, part.get("type"))
                        if part.get("type") else None})
                self.messages.setdefault((tns, msg.get("name", "")), parts)
            for pt in root.findall(q(NS_WSDL, "portType")):
                ops = collections.OrderedDict()
                for op in pt.findall(q(NS_WSDL, "operation")):
                    inp = op.find(q(NS_WSDL, "input"))
                    ops[op.get("name", "")] = (resolve_qname(self.nsmaps, inp, inp.get("message"))
                                               if inp is not None and inp.get("message") else None)
                self.port_types.setdefault((tns, pt.get("name", "")), ops)
            for b in root.findall(q(NS_WSDL, "binding")):
                info = {"type": resolve_qname(self.nsmaps, b, b.get("type")), "soap": None,
                        "style": "document", "ops": {}}
                for ns, version in ((NS_WSDL_SOAP11, "1.1"), (NS_WSDL_SOAP12, "1.2")):
                    sb = b.find(q(ns, "binding"))
                    if sb is not None:
                        info["soap"], info["style"] = version, sb.get("style", "document")
                        info["soap_ns"] = ns
                for op in b.findall(q(NS_WSDL, "operation")):
                    o = {"action": None, "style": None, "use": "literal", "parts": None,
                         "headers": []}
                    sns = info.get("soap_ns")
                    if sns:
                        so = op.find(q(sns, "operation"))
                        if so is not None:
                            o["action"] = so.get("soapAction")
                            o["style"] = so.get("style")
                        inp = op.find(q(NS_WSDL, "input"))
                        if inp is not None:
                            body = inp.find(q(sns, "body"))
                            if body is not None:
                                o["use"] = body.get("use", "literal")
                                if body.get("parts") is not None:
                                    o["parts"] = body.get("parts").split()
                            for h in inp.findall(q(sns, "header")):
                                if h.get("message"):
                                    o["headers"].append(
                                        (resolve_qname(self.nsmaps, h, h.get("message")),
                                         h.get("part", "")))
                    info["ops"][op.get("name", "")] = o
                self.bindings.setdefault((tns, b.get("name", "")), info)
            for svc in root.findall(q(NS_WSDL, "service")):
                for port in svc.findall(q(NS_WSDL, "port")):
                    address, soap = "", None
                    for ns, version in ((NS_WSDL_SOAP11, "1.1"), (NS_WSDL_SOAP12, "1.2")):
                        a = port.find(q(ns, "address"))
                        if a is not None:
                            address, soap = a.get("location", ""), version
                    self.ports.append(Port(svc.get("name", ""), port.get("name", ""),
                                           resolve_qname(self.nsmaps, port, port.get("binding")),
                                           address, soap))
        for sch, _url, inherited in self.schemas:
            tns = sch.get("targetNamespace") or inherited or ""
            info = Schema(tns, sch.get("elementFormDefault", "unqualified") == "qualified")
            for child in sch:
                kind = {q(NS_XSD, "element"): self.elements,
                        q(NS_XSD, "complexType"): self.ctypes,
                        q(NS_XSD, "simpleType"): self.stypes}.get(child.tag)
                if kind is not None and child.get("name"):
                    kind.setdefault((tns, child.get("name")), (child, info))

    # ---- schema walking -----------------------------------------------------
    def _is_string_type(self, qname, depth=0):
        if qname == (NS_XSD, "string"):
            return True
        if depth < 8 and qname in self.stypes:
            node, _sch = self.stypes[qname]
            return self._simple_is_string(node, depth + 1)
        return False

    def _simple_is_string(self, node, depth):
        r = node.find(q(NS_XSD, "restriction"))
        if r is None:
            return False
        if r.get("base"):
            return self._is_string_type(resolve_qname(self.nsmaps, r, r.get("base")), depth)
        inner = r.find(q(NS_XSD, "simpleType"))
        return inner is not None and self._simple_is_string(inner, depth + 1)

    def _param(self, el, sch):
        min_occurs, max_occurs = _occurs(el)
        if el.get("ref"):
            ref = resolve_qname(self.nsmaps, el, el.get("ref"))
            target = self.elements.get(ref)
            if target is None:
                return Param(ref[1], ref[0], "unresolved ref %s" % qn_text(ref), False,
                             min_occurs, max_occurs, True)
            el, sch = target
            name, ns, qualified = ref[1], ref[0], True
        else:
            name = el.get("name", "")
            form = el.get("form")
            qualified = (form == "qualified") if form else sch.qualified
            ns = sch.tns if qualified else ""
        if el.get("type"):
            t = resolve_qname(self.nsmaps, el, el.get("type"))
            type_text = ("xs:" + t[1]) if t[0] == NS_XSD else qn_text(t)
            is_string = self._is_string_type(t)
        elif el.find(q(NS_XSD, "simpleType")) is not None:
            type_text = "anonymous simple type"
            is_string = self._simple_is_string(el.find(q(NS_XSD, "simpleType")), 1)
        elif el.find(q(NS_XSD, "complexType")) is not None:
            type_text, is_string = "anonymous complex type", False
        else:
            type_text, is_string = "xs:anyType (no type given)", False
        return Param(name, ns, type_text, is_string, min_occurs, max_occurs, qualified)

    def _group(self, group, sch, out, depth):
        for child in group:
            if child.tag == q(NS_XSD, "element"):
                out.append(self._param(child, sch))
            elif child.tag in (q(NS_XSD, "sequence"), q(NS_XSD, "choice"), q(NS_XSD, "all")):
                if depth < 10:
                    self._group(child, sch, out, depth + 1)
            elif child.tag in (q(NS_XSD, "any"), q(NS_XSD, "group")):
                min_occurs, max_occurs = _occurs(child)
                out.append(Param(local_name(child.tag), "", "xs:" + local_name(child.tag), False,
                                 min_occurs, max_occurs, False))

    def _complex(self, ct, sch, depth=0):
        out = []
        if depth > 10:
            return out
        content = ct.find(q(NS_XSD, "complexContent"))
        holder = ct
        if content is not None:
            ext = content.find(q(NS_XSD, "extension"))
            if ext is None:
                ext = content.find(q(NS_XSD, "restriction"))
            if ext is not None:
                if ext.tag == q(NS_XSD, "extension") and ext.get("base"):
                    base = self.ctypes.get(resolve_qname(self.nsmaps, ext, ext.get("base")))
                    if base is not None:
                        out.extend(self._complex(base[0], base[1], depth + 1))
                holder = ext
        for group in holder:
            if group.tag in (q(NS_XSD, "sequence"), q(NS_XSD, "choice"), q(NS_XSD, "all")):
                self._group(group, sch, out, 0)
        return out

    def element_children(self, qname):
        node, sch = self.elements[qname]
        if node.get("type"):
            t = resolve_qname(self.nsmaps, node, node.get("type"))
            if t in self.ctypes:
                ct, tsch = self.ctypes[t]
                return self._complex(ct, tsch)
            return None     # a simple-typed element: not a wrapper
        ct = node.find(q(NS_XSD, "complexType"))
        return self._complex(ct, sch) if ct is not None else []


def wsdl_get(http, cfg, url, use_auth):
    headers = {"Accept": "text/xml, application/xml, */*"}
    if use_auth and cfg.has_credentials:
        headers["Authorization"] = "Basic " + cfg.basic_token
    return http.request(url, headers=headers, timeout=min(cfg.timeout, 120),
                        limit=MAX_WSDL_BYTES, what="GET %s" % url)


def fetch_wsdl(http, cfg, paths=WSDL_PATHS, service="ImportToolsAPI", quiet=False):
    """(Wsdl, url, used_auth). Tries each path; 404 or 'not a WSDL' moves on
    to the next, anything else stops."""
    tried = []
    for path in paths:
        url = cfg.base_url + path + "?wsdl"
        status, reason, _h, body = wsdl_get(http, cfg, url, False)
        used_auth = False
        if status in (401, 403):
            if not cfg.has_credentials:
                raise Failed("%s returned HTTP %d: this server wants credentials for its WSDL, "
                             "and none are set. Add the shared username and password (or %s "
                             "and %s) to the variable group."
                             % (url, status, cfg.user_var_env, cfg.password_var_env))
            if not quiet:
                say("  WSDL needs authentication (HTTP %d) -- retrying with the credentials "
                    "from %s." % (status, cfg.cred_src))
            status, reason, _h, body = wsdl_get(http, cfg, url, True)
            used_auth = True
            if status in (401, 403):
                raise Failed("%s returned HTTP %d even with the credentials from %s: the user "
                             "name or password is wrong, or the user may not use web services."
                             % (url, status, cfg.cred_src))
        if status in (301, 302, 303, 307, 308):
            raise Failed("%s redirected (HTTP %d) -- not followed, so credentials cannot leak. "
                         "Usually the URL needs https or a different host/port." % (url, status))
        if status == 404:
            tried.append("%s -> HTTP 404" % path)
            continue
        if status != 200:
            raise Failed("%s returned HTTP %d %s: %s"
                         % (url, status, reason, clean(body[:300].decode("utf-8", "replace"), 300)))
        w = Wsdl()
        try:
            root = parse_xml(body, w.nsmaps, "The WSDL")
        except Failed as ex:
            tried.append("%s -> HTTP 200 but %s" % (path, ex))
            continue
        if root.tag != q(NS_WSDL, "definitions"):
            tried.append("%s -> HTTP 200 but the document is <%s>, not a WSDL 1.1 definitions"
                         % (path, local_name(root.tag)))
            continue
        if not quiet:
            for t in tried:
                say("  %s" % t)
            say("  WSDL found at %s%s" % (path, " (with credentials)" if used_auth else ""))
        load_wsdl_documents(http, cfg, w, url, root, used_auth)
        w.target_ns = root.get("targetNamespace", "")
        w.index()
        return w, url, used_auth
    raise Failed("No %s WSDL at %s: %s. Either the URL's context is wrong for this server, or "
                 "the server does not publish %s (web services not exposed, or a different path "
                 "in this Guidewire release)."
                 % (service, cfg.shown_url, "; ".join(tried), service))


def load_wsdl_documents(http, cfg, w, url, root, use_auth):
    """Pull in wsdl:import and xsd:import/include/redefine documents that carry
    a location, resolved against the importing document's URL. One that cannot
    be read is noted in w.unfetched, not fatal: Guidewire's WSDLs import
    soapheaders.xsd, which only names the auth header's fields (defaults
    exist), and a proxy may block .xsd paths. Planning fails later if
    something the call needs is still missing."""
    seen = {url}
    queue = [(url, root, None)]
    while queue:
        doc_url, doc, inherited = queue.pop(0)
        if doc.tag == q(NS_WSDL, "definitions"):
            w.wsdl_docs.append((doc_url, doc))
            refs = [(imp.get("location"), None) for imp in doc.findall(q(NS_WSDL, "import"))]
            schemas = []
            for types in doc.findall(q(NS_WSDL, "types")):
                schemas.extend(types.findall(q(NS_XSD, "schema")))
        elif doc.tag == q(NS_XSD, "schema"):
            refs, schemas = [], [doc]
        else:
            w.unfetched.append((doc_url, "it is <%s>, neither a WSDL nor an XML schema"
                                % local_name(doc.tag)))
            w.fetched = [f for f in w.fetched if f.split("  (", 1)[0] != doc_url]
            continue
        for sch in schemas:
            w.schemas.append((sch, doc_url, inherited if sch is doc else None))
            tns = sch.get("targetNamespace") or (inherited if sch is doc else None)
            for tag in ("import", "include", "redefine"):
                for imp in sch.findall(q(NS_XSD, tag)):
                    refs.append((imp.get("schemaLocation"), tns if tag != "import" else None))
        for loc, inh in refs:
            if not loc:
                continue
            target = urllib.parse.urljoin(doc_url, loc.strip())
            target, moved = same_server(cfg, target, "Imported document")
            target = urllib.parse.urldefrag(target)[0]
            if target in seen:
                continue
            seen.add(target)
            if len(seen) > MAX_WSDL_DOCS:
                raise Failed("The WSDL imports more than %d documents; stopping." % MAX_WSDL_DOCS)
            try:
                status, reason, _h, body = wsdl_get(http, cfg, target, use_auth)
                if status in (401, 403) and not use_auth and cfg.has_credentials:
                    status, reason, _h, body = wsdl_get(http, cfg, target, True)
                if status != 200:
                    raise Failed("HTTP %d %s" % (status, reason))
                doc = parse_xml(body, w.nsmaps, "it")
            except Failed as ex:
                w.unfetched.append((target, str(ex)))
                continue
            w.fetched.append(target + ("  (on the configured server)" if moved else ""))
            queue.append((target, doc, inh))


class Plan(object):
    """Everything needed to build and send one call."""


def _unread_hint(w):
    return (" The WSDL imports documents that could not be read, which may define it: %s."
            % "; ".join(u for u, _ in w.unfetched)) if w.unfetched else ""


def plan_port(w, cfg, wsdl_url):
    """The first SOAP 1.1 port, its operations, and the endpoint to POST to."""
    p = Plan()
    soap11 = [port for port in w.ports
              if port.soap == "1.1" and (w.bindings.get(port.binding) or {}).get("soap") == "1.1"]
    if not soap11:
        found = ", ".join("%s (SOAP %s)" % (pt.name, pt.soap or "?") for pt in w.ports) or "none"
        raise Failed("The WSDL has no SOAP 1.1 port; this tool sends SOAP 1.1. Ports: %s.%s"
                     % (found, _unread_hint(w)))
    port = soap11[0]
    binding = w.bindings[port.binding]
    ops = w.port_types.get(binding["type"])
    if ops is None:
        raise Failed("Binding %s refers to portType %s, which the WSDL does not define.%s"
                     % (qn_text(port.binding), qn_text(binding["type"]), _unread_hint(w)))
    p.port, p.binding, p.ops, p.operations = port, binding, ops, list(ops)

    # The endpoint: the SOAP 1.1 port's address from '/ws/' on, appended to
    # the configured base URL. The WSDL is generated from the URL the app
    # server saw, so behind a proxy its host and path prefix are not ours.
    p.advertised = port.address
    u = urllib.parse.urlsplit(p.advertised or "")
    if "/ws/" in u.path and (u.scheme or "http").lower() in ("http", "https"):
        p.endpoint = cfg.base_url + "/ws/" + u.path.split("/ws/", 1)[1]
        p.endpoint_note = ("as advertised" if p.endpoint == p.advertised else
                           "the advertised path from /ws/ on, under the configured URL")
    else:
        # Guidewire's convention: SOAP 1.2 at the service URL, SOAP 1.1 at
        # <service>/soap11. Posting SOAP 1.1 to the 1.2 URL fails every call.
        p.endpoint = wsdl_url.split("?", 1)[0] + "/soap11"
        p.endpoint_note = ("the WSDL gives no usable address; the service URL + /soap11, "
                           "Guidewire's SOAP 1.1 path")
    return p


def plan_operation(w, p, name):
    """Request element, parameters, SOAPAction and auth header of `name`."""
    bop = p.binding["ops"].get(name)
    if bop is None:
        raise Failed("Operation %s is in the portType but not in binding %s."
                     % (name, qn_text(p.port.binding)))
    style = bop["style"] or p.binding["style"] or "document"
    if style != "document":
        raise Failed("Operation %s uses %s style; this tool builds document/literal calls only."
                     % (name, style))
    if bop["use"] != "literal":
        raise Failed("Operation %s uses '%s' encoding; this tool builds document/literal "
                     "calls only." % (name, bop["use"]))
    p.soap_action = bop["action"] or ""

    msg_qn = p.ops[name]
    parts = w.messages.get(msg_qn) if msg_qn else None
    if parts is None:
        raise Failed("The input message of %s (%s) is not defined in the WSDL.%s"
                     % (name, qn_text(msg_qn) if msg_qn else "none", _unread_hint(w)))
    if bop["parts"] is not None:
        parts = [pt for pt in parts if pt["name"] in bop["parts"]]
    if len(parts) != 1 or not parts[0]["element"]:
        raise Failed("The input of %s is not a single document/literal element part (%d parts)."
                     % (name, len(parts)))
    p.request = parts[0]["element"]
    if p.request not in w.elements:
        raise Failed("The request element %s is not defined in the WSDL's schemas.%s"
                     % (qn_text(p.request), _unread_hint(w) or
                        " (An xsd:import without a schemaLocation?)"))
    params = w.element_children(p.request)
    if params is None:
        raise Failed("The request element %s is a simple type, not a wrapper element."
                     % qn_text(p.request))
    p.params = params

    # The authentication header: as the WSDL declares it, else Guidewire's.
    p.auth_header = (NS_GWSOAP, "authentication")
    p.auth_fields = ("username", "password")
    p.auth_header_source = "Guidewire default (not declared in the WSDL)"
    for msg, part_name in bop["headers"]:
        for part in w.messages.get(msg) or []:
            el = part.get("element")
            if part["name"] == part_name and el and el[1].lower() == "authentication":
                p.auth_header = el
                if el in w.elements:
                    p.auth_header_source = "declared in the WSDL"
                    kids = w.element_children(el) or []
                    names = {k.name.lower(): k.name for k in kids}
                    if "username" in names and "password" in names:
                        p.auth_fields = (names["username"], names["password"])
                else:
                    p.auth_header_source = ("declared in the WSDL; its schema was not read, so "
                                            "Guidewire's username/password fields are used")


def resolve_auth(cfg, p):
    """The one sign-in method a plan uses: Guidewire refuses a request that
    carries two. auto = the SOAP header when the WSDL declares it, else Basic."""
    p.auth_mode = cfg.auth
    p.auth_from = "GW_AUTH"
    if cfg.auth == "auto":
        p.auth_mode = "header" if p.auth_header_source.startswith("declared") else "basic"
        p.auth_from = "auto"


def plan_call(w, cfg, wsdl_url):
    """The import call."""
    p = plan_port(w, cfg, wsdl_url)
    ops = p.ops
    if cfg.op_override:
        exact = [n for n in ops if n == cfg.op_override]
        loose = [n for n in ops if n.lower() == cfg.op_override.lower()]
        chosen = exact or (loose if len(loose) == 1 else [])
        if not chosen:
            raise Failed("GW_IMPORT_OPERATION is '%s', which is not an operation of this "
                         "service. Operations: %s." % (cfg.op_override, ", ".join(ops)))
        p.op_source = "GW_IMPORT_OPERATION"
    else:
        chosen = []
        for want in IMPORT_OPERATIONS:
            chosen = [n for n in ops if n.lower() == want.lower()]
            if chosen:
                break
        if not chosen:
            raise Failed("No import operation in the WSDL: looked for %s. Operations: %s. If "
                         "one of these imports admin XML, set GW_IMPORT_OPERATION to its name."
                         % (", ".join(IMPORT_OPERATIONS), ", ".join(ops) or "none"))
        p.op_source = "first of %s" % ", ".join(IMPORT_OPERATIONS)
    p.op = chosen[0]
    plan_operation(w, p, p.op)
    resolve_auth(cfg, p)

    params = p.params
    strings = [x for x in params if x.is_string]
    if len(params) == 1 and strings:
        target = strings[0]
    else:
        named = [x for x in strings if x.name.lower() == "xmldata"]
        target = named[0] if named else (strings[0] if len(strings) == 1 else None)
    if target is None:
        raise Failed("Cannot tell which parameter of %s takes the XML: %s. Expected a single "
                     "string parameter (usually xmlData)."
                     % (p.op, ", ".join("%s (%s)" % (x.name, x.type_text) for x in params) or
                        "it has none"))
    others = [x for x in params if x is not target and x.min_occurs > 0]
    if others:
        raise Failed("Operation %s also requires %s, which this tool does not know how to "
                     "fill. Set GW_IMPORT_OPERATION to an operation that takes only the XML."
                     % (p.op, ", ".join("%s (%s)" % (x.name, x.type_text) for x in others)))
    p.param = target
    return p


def plan_login(w, cfg, wsdl_url):
    """ImportToolsAPI.xmlToCsv: one string in, a string out, nothing written.
    None when this WSDL has no usable one."""
    try:
        p = plan_port(w, cfg, wsdl_url)
        chosen = [n for n in p.ops if n.lower() == LOGIN_OPERATION.lower()]
        if not chosen:
            return None
        p.op = chosen[0]
        plan_operation(w, p, p.op)
    except Failed:
        return None
    resolve_auth(cfg, p)
    strings = [x for x in p.params if x.is_string]
    if len(strings) != 1 or any(x is not strings[0] and x.min_occurs > 0 for x in p.params):
        return None
    p.param = strings[0]
    return p


def plan_probe(w, cfg, wsdl_url):
    """SystemToolsAPI.getVersion: no parameters, nothing written."""
    p = plan_port(w, cfg, wsdl_url)
    chosen = [n for n in p.ops if n.lower() == PROBE_OPERATION.lower()]
    if not chosen:
        raise Failed("%s has no %s operation" % (PROBE_SERVICE, PROBE_OPERATION))
    p.op = chosen[0]
    plan_operation(w, p, p.op)
    resolve_auth(cfg, p)
    needed = [x.name for x in p.params if x.min_occurs > 0]
    if needed:
        raise Failed("%s.%s wants parameters (%s)" % (PROBE_SERVICE, p.op, ", ".join(needed)))
    p.param = None
    return p


def print_plan(w, p, wsdl_url):
    field("WSDL", wsdl_url)
    for extra in w.fetched:
        field("  imported", extra)
    for url, why in w.unfetched:
        warn("The WSDL imports %s, which could not be read (%s). Continuing without it."
             % (url, why))
    field("targetNamespace", w.target_ns or "(none)")
    field("service / port", "%s / %s (SOAP 1.1)" % (p.port.service, p.port.name))
    field("soap:address", p.advertised or "(none)")
    field("endpoint", p.endpoint)
    say("    -> import POSTs here: %s" % p.endpoint_note)
    field("operations", "%d: %s" % (len(p.operations), ", ".join(p.operations)))
    field("import operation", "%s  (%s)" % (p.op, p.op_source))
    field("SOAPAction", '"%s"' % p.soap_action)
    field("request element", qn_text(p.request))
    for x in p.params:
        say("    %-20s %s, %s, minOccurs %d%s"
            % (x.name, x.type_text, ("qualified {%s}" % x.ns) if x.qualified and x.ns
               else "unqualified", x.min_occurs,
               "   <- the file goes here" if x is p.param else
               ("   (optional, left out)" if x.min_occurs == 0 else "")))
    field("auth header", "%s  (%s)" % (qn_text(p.auth_header), p.auth_header_source))
    field("auth method", "%s%s" % ({"basic": "HTTP Basic", "header": "Guidewire SOAP header",
                                     "both": "HTTP Basic + Guidewire SOAP header"}[p.auth_mode],
                                    "  (%s)" % p.auth_from))


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------
def probe(cfg):
    proxies = urllib.request.getproxies()
    if not cfg.bypass_proxy and proxies.get(cfg.scheme) and not urllib.request.proxy_bypass(cfg.host):
        field("reachability", "not probed: BYPASS_PROXY=false and a proxy is configured")
        return
    where = "%s:%d" % (cfg.host, cfg.port)
    t0 = time.monotonic()
    try:
        sock = socket.create_connection((cfg.host, cfg.port), timeout=min(cfg.timeout, 20))
    except socket.gaierror as ex:
        raise Failed("Cannot resolve %s on this agent (%s). Check the URL's host name and the "
                     "agent's DNS." % (cfg.host, ex))
    except socket.timeout:
        raise Failed("No answer from %s within %d s: a firewall drops the traffic or there is "
                     "no route from this agent. Ask for the agent to be allowed to reach the "
                     "server on port %d." % (where, min(cfg.timeout, 20), cfg.port))
    except ConnectionRefusedError:
        raise Failed("%s refused the connection: nothing listens on port %d (app server down, "
                     "or the wrong port), or a firewall rejects the agent." % (where, cfg.port))
    except OSError as ex:
        raise Failed("Cannot connect to %s: %s" % (where, ex))
    field("TCP", "%s reachable (%d ms)" % (where, (time.monotonic() - t0) * 1000))
    if cfg.scheme != "https":
        sock.close()
        return
    try:
        sock.settimeout(min(cfg.timeout, 20))
        tls = cfg.ssl_ctx.wrap_socket(sock, server_hostname=cfg.host)
    except ssl.SSLCertVerificationError as ex:
        sock.close()
        raise Failed(describe_network_error(ex, "TLS handshake with %s" % where))
    except (ssl.SSLError, OSError) as ex:
        sock.close()
        raise Failed("TLS handshake with %s failed: %s. Is the port TLS (https)?" % (where, ex))
    with tls:
        cipher = tls.cipher()
        field("TLS", "%s, %s" % (tls.version(), cipher[0] if cipher else "?"))
        if cfg.verify_tls:
            cert = tls.getpeercert() or {}

            def cn(name):
                for rdn in cert.get(name, ()):
                    for k, v in rdn:
                        if k == "commonName":
                            return v
                return "?"
            field("certificate", "CN=%s, issued by CN=%s, valid until %s"
                  % (cn("subject"), cn("issuer"), cert.get("notAfter", "?")))
            if cert.get("notAfter"):
                days = (ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400
                if days < 30:
                    warn("The server's certificate expires in %d days." % days)
        else:
            field("certificate", "not checked (GW_VERIFY_TLS=false)")


def soap_headers(cfg, p, length):
    headers = {"Content-Type": "text/xml; charset=utf-8",
               "SOAPAction": '"%s"' % p.soap_action,
               "Accept": "text/xml, application/soap+xml, */*",
               "Content-Length": str(length),
               "User-Agent": "gw-admin-data-load"}
    if p.auth_mode in ("basic", "both"):
        headers["Authorization"] = "Basic " + cfg.basic_token
    return headers


def login_call(http, cfg, p, service, text=""):
    """POST one of check's login calls: (reply, SOAP Body, fault, problem).
    HTTP 401/403 fails check: the server refused the credentials outright."""
    head, tail = envelope_parts(cfg, p)
    data = head + xml_text(text).encode("utf-8") + tail
    say("  logging in       : %s.%s at %s" % (service, p.op, p.endpoint))
    r = http.soap(p.endpoint, data, soap_headers(cfg, p, len(data)), min(cfg.timeout, 120),
                  MAX_RESPONSE_BYTES, "POST %s" % p.endpoint)
    if r.status in (401, 403):
        raise Failed("%s.%s answered HTTP %d with the credentials from %s: Guidewire %s. %s"
                     % (service, p.op, r.status, cfg.cred_src,
                        "did not accept them" if r.status == 401 else "refused permission",
                        PERMISSION_HELP))
    try:
        body = soap_body(r)
    except Failed as ex:
        return r, None, None, str(ex).rstrip(".")
    fault = soap_fault(body)
    if fault:
        print_fault(fault)
    return r, body, fault, None


def check_login(http, cfg, w, wsdl_url):
    """Log in so a wrong password or a missing permission shows up in check,
    not first in an import: through ImportToolsAPI.xmlToCsv (same service and
    permission as the import, writes nothing), else SystemToolsAPI.getVersion.
    Only a refused login or permission fails check; anything else just says
    the login was not verified."""
    if not cfg.has_credentials:
        field("login", "not tested: no credentials. A wrong password or a missing soapadmin "
                       "permission would show up only on import.")
        return

    def not_verified(why):
        field("login", "NOT verified")
        warn("The credentials (%s) and the soapadmin permission were NOT verified: %s. Check "
             "continues; the import will be the first call that logs in." % (cfg.cred_src, why))

    p = plan_login(w, cfg, wsdl_url)
    if p is not None:
        # An empty admin-data document of this product: nothing to convert.
        sample = '<import xmlns="http://guidewire.com/%s/exim/import"/>' % cfg.context
        r, body, fault, problem = login_call(http, cfg, p, "ImportToolsAPI", sample)
        if problem:
            return not_verified(problem)
        if fault:
            why = login_fault_reason(fault)
            if why == "permission":
                raise Failed("ImportToolsAPI.%s refused %s: %s. %s"
                             % (p.op, cfg.cred_src, fault_summary(fault), auth_help(fault)))
            if why:
                raise Failed("ImportToolsAPI.%s refused the login of %s: %s. %s"
                             % (p.op, cfg.cred_src, fault_summary(fault), auth_help(fault)))
            if fault[3]:
                # A declared exception comes from the operation itself, which
                # runs only after the login and permission checks passed.
                field("login", "accepted -- credentials from %s; ImportToolsAPI allowed them (%s "
                               "answered %s about the empty sample)"
                      % (cfg.cred_src, p.op, clean(", ".join(fault[3]), 100)))
                return
            return not_verified("ImportToolsAPI.%s answered with a SOAP Fault: %s"
                                % (p.op, fault_summary(fault)))
        if r.status != 200:
            return not_verified("HTTP %d %s" % (r.status, r.reason))
        field("login", "accepted -- credentials from %s; ImportToolsAPI allowed them"
              % cfg.cred_src)
        return

    path = urllib.parse.urlsplit(wsdl_url).path[len(urllib.parse.urlsplit(cfg.base_url).path):]
    path = path.rsplit("/", 1)[0] + "/" + PROBE_SERVICE
    try:
        w2, url, _a = fetch_wsdl(http, cfg, (path,), PROBE_SERVICE, quiet=True)
        p = plan_probe(w2, cfg, url)
    except Failed as ex:
        return not_verified("ImportToolsAPI has no %s, and %s" % (LOGIN_OPERATION,
                                                                    str(ex).rstrip(".")))
    r, body, fault, problem = login_call(http, cfg, p, PROBE_SERVICE)
    if problem:
        return not_verified(problem)
    if fault:
        why = login_fault_reason(fault)
        if why == "permission":
            # The password worked; this service just isn't one the user may
            # call. ImportToolsAPI checks its own permission on import.
            return not_verified("%s.%s refused permission (%s), which says nothing about "
                                "ImportToolsAPI" % (PROBE_SERVICE, p.op, fault_summary(fault)))
        if why:
            raise Failed("%s.%s refused the login of %s: %s. %s"
                         % (PROBE_SERVICE, p.op, cfg.cred_src, fault_summary(fault),
                            auth_help(fault)))
        return not_verified("%s.%s answered with a SOAP Fault: %s"
                            % (PROBE_SERVICE, p.op, fault_summary(fault)))
    if r.status != 200:
        return not_verified("HTTP %d %s" % (r.status, r.reason))
    wrapper = list(body)[0] if len(body) else None
    result = list(wrapper)[0] if wrapper is not None and len(wrapper) == 1 else wrapper
    if result is None:
        return not_verified("%s.%s returned an empty SOAP Body" % (PROBE_SERVICE, p.op))
    lines = []
    flatten(result, "", lines)
    field("server version", ", ".join(("%s=%s" % (clean(n, 60), clean(v, 100))) if n
                                      else clean(v, 100) for n, v in lines[:8]) or "(empty)")
    field("login", "accepted -- credentials from %s" % cfg.cred_src)


def do_check(cfg):
    section("Check: %s" % cfg.shown_url)
    probe(cfg)
    http = Http(cfg)
    w, wsdl_url, _used_auth = fetch_wsdl(http, cfg)
    plan = plan_call(w, cfg, wsdl_url)
    print_plan(w, plan, wsdl_url)
    check_login(http, cfg, w, wsdl_url)
    say("  Check passed: the import operation and its request were identified.")
    return http, plan


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------
class _CountTarget(object):
    """Counts elements without building a tree, so memory stays flat."""

    def __init__(self):
        self.depth = 0
        self.total = 0
        self.root = None
        self.root_attrs = {}
        self.children = collections.Counter()
        self.prefixes = collections.Counter()

    def start(self, tag, attrs):
        self.total += 1
        if self.depth == 0:
            self.root = tag
            self.root_attrs = {local_name(k): v for k, v in attrs.items()}
        elif self.depth == 1:
            self.children[local_name(tag)] += 1
        # public-id on records, publicID on references to them. The prefix
        # shows which environment's PublicIDPrefix the data was exported from.
        for k, v in attrs.items():
            if local_name(k).lower() in ("public-id", "publicid"):
                prefix = v.split(":", 1)[0].strip() if ":" in v else "(no prefix)"
                if prefix in self.prefixes or len(self.prefixes) < 10000:
                    self.prefixes[prefix] += 1
        self.depth += 1

    def end(self, tag):
        self.depth -= 1

    def doctype(self, name, pubid, system):
        raise DoctypeFound()

    def close(self):
        return None


_DECL_ENCODING = re.compile(br'^<\?xml[^>]*?\sencoding\s*=\s*["\']([A-Za-z][A-Za-z0-9._-]*)["\']')


def detect_encoding(path):
    """(codec, description). The text sent is the file decoded with this."""
    with open(path, "rb") as f:
        head = f.read(1024)
    if head.startswith(codecs.BOM_UTF8):
        # Guidewire parses the text as UTF-8 bytes under the file's own
        # declaration: a BOM'd file that declares another encoding would be
        # read wrongly while the import reports success.
        m = _DECL_ENCODING.match(head[len(codecs.BOM_UTF8):])
        if m:
            name = m.group(1).decode("ascii")
            try:
                declared = codecs.lookup(name).name
            except LookupError:
                declared = name
            if declared != "utf-8":
                raise Refused("The file starts with a UTF-8 byte-order mark but its XML "
                              "declaration says encoding '%s'. Fix the declaration to UTF-8 "
                              "(or re-save the file as UTF-8 without the mark)." % name)
        return "utf-8-sig", "UTF-8 (byte-order mark, not sent)"
    if head.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32", "UTF-32 (byte-order mark)"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16", "UTF-16 (byte-order mark)"
    if head.startswith(b"<\x00?\x00"):
        return "utf-16-le", "UTF-16LE (no byte-order mark)"
    if head.startswith(b"\x00<\x00?"):
        return "utf-16-be", "UTF-16BE (no byte-order mark)"
    m = _DECL_ENCODING.match(head)
    if m:
        name = m.group(1).decode("ascii")
        try:
            codec = codecs.lookup(name).name
        except LookupError:
            raise Refused("The file declares encoding '%s', which Python does not know." % name)
        return codec, "%s (XML declaration)" % name
    return "utf-8", "UTF-8 (no declaration: the XML default)"


def ascii_compatible(codec):
    sample = '<?xml version="1.0"?><a b="c">x</a>'
    try:
        return sample.encode(codec) == sample.encode("ascii")
    except (UnicodeError, LookupError):
        return False


def is_admin_entity(name):
    n = name.lower()
    return n in ADMIN_ENTITIES or n.startswith(ADMIN_ENTITY_PREFIXES)


def do_validate(cfg):
    section("Validate: %s" % cfg.file_rel)
    encoding, enc_text = detect_encoding(cfg.file)
    if encoding.startswith(("utf-16", "utf-32")):
        # The XML parser here has no UTF-32, and Guidewire would read either as
        # UTF-8 bytes anyway: say so plainly instead of a parse error.
        raise Refused("The file is %s (%s). Re-save it as UTF-8. Nothing was sent."
                      % (enc_text.split(" ")[0], cfg.file_rel))
    decoder = codecs.getincrementaldecoder(encoding)("strict")
    target = _CountTarget()
    parser = ET.XMLParser(target=target)
    sha = hashlib.sha256()
    non_ascii = False
    escaped = 0
    try:
        with open(cfg.file, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if chunk:
                    sha.update(chunk)
                    parser.feed(chunk)
                text = decoder.decode(chunk, final=not chunk)
                if text:
                    escaped += len(xml_text(text).encode("utf-8"))
                    if not non_ascii and not text.isascii():
                        non_ascii = True
                if not chunk:
                    break
        parser.close()
    except DoctypeFound:
        raise Refused("%s has a DOCTYPE declaration. Admin data does not use one, and a DTD can "
                      "define entities that expand or read files when parsed -- remove it."
                      % cfg.file_rel)
    except ET.ParseError as ex:
        line, col = getattr(ex, "position", (0, 0))
        raise Failed("%s is not well-formed XML at line %d, column %d: %s"
                     % (cfg.file_rel, line, col + 1, str(ex).split(":")[0]))
    except UnicodeDecodeError as ex:
        raise Failed("%s cannot be decoded as %s (byte %d): %s"
                     % (cfg.file_rel, encoding, ex.start, ex.reason))
    root_ns, root_local = split_tag(target.root or "")
    field("size", human_bytes(cfg.file_size) + (" (%d bytes)" % cfg.file_size
                                                if cfg.file_size >= 1024 else ""))
    field("sha256", sha.hexdigest())
    field("encoding", enc_text)
    field("root element", root_local)
    field("root namespace", root_ns or "(none)")
    # Where the file came from: an export carries its release's version.
    field("root attributes", ", ".join("%s=%s" % (a, clean(target.root_attrs[a], 100))
                                       if a in target.root_attrs else "%s not set" % a
                                       for a in ("version", "usePeriodicFlushes")))
    field("elements", "{:,}".format(target.total))
    kinds = target.children.most_common()
    field("top level", "%d element(s) of %d kind(s) under the root"
          % (sum(target.children.values()), len(kinds)))
    width = max([len(k) for k, _ in kinds[:30]] + [8])
    for name, count in kinds[:30]:
        say("    %-*s %s" % (width, name, "{:,}".format(count)))
    if len(kinds) > 30:
        say("    and %d more kinds" % (len(kinds) - 30))
    prefixes = target.prefixes.most_common()
    field("public-id prefix", "%d distinct (public-id and publicID attributes)%s"
          % (len(prefixes), "" if prefixes else ": none found"))
    width = max([len(k) for k, _ in prefixes[:10]] + [8])
    for name, count in prefixes[:10]:
        say("    %-*s %s" % (width, clean(name, 60), "{:,}".format(count)))
    if len(prefixes) > 10:
        say("    and %d more prefixes" % (len(prefixes) - 10))
    request = escaped + 700         # + the envelope, roughly
    field("request size", "about %s (the file escaped into the SOAP request)"
          % human_bytes(request))

    tokens = set(re.split(r"[^a-z0-9]+", root_ns.lower()))
    contexts = set(APP_CONTEXT.values())
    if cfg.context not in tokens:
        others = sorted((contexts & tokens) - {cfg.context})
        if others:
            warn("The root namespace '%s' looks like /%s data, but product %s is /%s. Check "
                 "this file is meant for %s." % (root_ns, "/".join(others), cfg.product,
                                                 cfg.context, PRODUCT_NAME[cfg.product]))
        else:
            warn("The root namespace '%s' does not mention /%s; check this file is %s admin "
                 "data." % (root_ns or "(none)", cfg.context, PRODUCT_NAME[cfg.product]))
    unknown = [(k, c) for k, c in kinds if not is_admin_entity(k)]
    if unknown:
        warn("%d top-level element kind(s) are not in this tool's list of admin entity types: "
             "%s%s. Guidewire supports this import for administrative data only (users, "
             "groups, roles, activity patterns, authority limits, ...) and does not fully "
             "validate what it imports. If these are admin entities, ignore this warning."
             % (len(unknown), ", ".join("%s (%d)" % kc for kc in unknown[:10]),
                " and %d more" % (len(unknown) - 10) if len(unknown) > 10 else ""))

    # Guidewire turns the string it receives into UTF-8 bytes and parses those
    # with the file's own XML declaration, so only text that reads the same in
    # UTF-8 arrives intact. A wrong declaration can garble names with Ok=true.
    shown = enc_text.split(" ")[0]
    refuse = ""
    if encoding.startswith(("utf-16", "utf-32")):
        refuse = ("The file is %s; Guidewire would parse it as UTF-8 bytes under a %s "
                  "declaration. Re-save it as UTF-8." % (shown, shown))
    elif encoding not in ("utf-8", "utf-8-sig") and (non_ascii or not ascii_compatible(encoding)):
        refuse = ("The file is %s %s; Guidewire would parse it as UTF-8 bytes under a %s "
                  "declaration and could garble them while reporting success. Re-save it as "
                  "UTF-8." % (shown, "with non-ASCII characters" if non_ascii else
                              "(not ASCII-compatible)", shown))
    if refuse:
        warn("Import will refuse this file: " + refuse)
    if cfg.action == "validate" and request > GW_UPLOAD_DEFAULT_BYTES:
        warn("The request will be about %s, over Guidewire's default MaximumFileUploadSize "
             "(20 MB, config.xml). Ask the Guidewire admins for the configured value, or split "
             "the file." % human_bytes(request))
    say("  Validate passed: well-formed XML.")
    return {"encoding": encoding, "sha256": sha.hexdigest(), "refuse": refuse}


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------
def xml_text(s):
    # \r as a reference: a parser would otherwise turn the file's CRLF into LF.
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\r", "&#13;"))


def xml_attr(s):
    return xml_text(s).replace('"', "&quot;").replace("\n", "&#10;").replace("\t", "&#9;")


def escaped_chunks(path, encoding):
    """The file's text, decoded and XML-escaped, as UTF-8 chunks."""
    decoder = codecs.getincrementaldecoder(encoding)("strict")
    with open(path, "rb") as f:
        while True:
            raw = f.read(CHUNK)
            text = decoder.decode(raw, final=not raw)
            if text:
                yield xml_text(text).encode("utf-8")
            if not raw:
                return


class EnvelopeBody(object):
    """The request body, streamed: envelope head, the file escaped chunk by
    chunk, envelope tail. Never all in memory, never written to disk."""

    def __init__(self, head, path, encoding, tail):
        self.head, self.path, self.encoding, self.tail = head, path, encoding, tail
        self.length = len(head) + len(tail) + sum(len(c) for c in escaped_chunks(path, encoding))
        self._gen = None
        self._buf = b""
        self._pos = 0

    def _chunks(self):
        yield self.head
        for c in escaped_chunks(self.path, self.encoding):
            yield c
        yield self.tail

    def read(self, size=-1):
        """Up to `size` bytes from the current chunk (short reads are fine for
        http.client, which reads until b''); b'' at the end."""
        if self._gen is None:
            self._gen = self._chunks()
        if size is None or size < 0:
            rest = [self._buf[self._pos:]] + list(self._gen)
            self._buf, self._pos = b"", 0
            return b"".join(rest)
        while self._pos >= len(self._buf):
            try:
                self._buf, self._pos = next(self._gen), 0
            except StopIteration:
                return b""
        out = self._buf[self._pos:self._pos + size]
        self._pos += len(out)
        return out


def envelope_parts(cfg, p):
    """(head, tail) bytes of a SOAP 1.1 envelope for plan p: the file's
    escaped text goes between them, or nothing for a call without one."""
    header = ""
    if p.auth_mode in ("header", "both"):
        hns, hname = p.auth_header
        uf, pf = p.auth_fields
        header = ('<soapenv:Header><gwsoap:%s xmlns:gwsoap="%s"><gwsoap:%s>%s</gwsoap:%s>'
                  '<gwsoap:%s>%s</gwsoap:%s></gwsoap:%s></soapenv:Header>'
                  % (hname, xml_attr(hns), uf, xml_text(cfg.user), uf,
                     pf, xml_text(cfg.password), pf, hname))
    rns, rname = p.request
    if rns:
        open_w, close_w = '<op:%s xmlns:op="%s">' % (rname, xml_attr(rns)), "</op:%s>" % rname
    else:
        open_w, close_w = "<%s>" % rname, "</%s>" % rname
    x = p.param
    if x is None:
        open_p = close_p = ""
    elif x.ns and x.ns == rns:
        open_p, close_p = "<op:%s>" % x.name, "</op:%s>" % x.name
    elif x.ns:
        open_p, close_p = '<p:%s xmlns:p="%s">' % (x.name, xml_attr(x.ns)), "</p:%s>" % x.name
    else:
        # Unqualified: the wrapper uses a prefix, so no default namespace is in scope.
        open_p, close_p = "<%s>" % x.name, "</%s>" % x.name
    head = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<soapenv:Envelope xmlns:soapenv="%s">%s<soapenv:Body>%s%s'
            % (NS_SOAP11_ENV, header, open_w, open_p))
    tail = "%s%s</soapenv:Body></soapenv:Envelope>" % (close_p, close_w)
    return head.encode("utf-8"), tail.encode("utf-8")


def build_envelope(cfg, p, encoding):
    head, tail = envelope_parts(cfg, p)
    return EnvelopeBody(head, cfg.file, encoding, tail)


_ERRORISH = re.compile(r"error|fail", re.IGNORECASE)
_EMPTYISH = {"", "0", "0.0", "false", "none", "null", "nil", "n/a"}
_OKISH = {"ok", "success", "successful", "succeeded"}


def is_nil(el):
    return el.get(q(NS_XSI, "nil"), "").lower() in ("true", "1")


def text_of(el):
    return "" if is_nil(el) else " ".join(t.strip() for t in el.itertext() if t.strip())


def flatten(el, path, out):
    kids = list(el)
    if not kids:
        out.append((path, "" if is_nil(el) else (el.text or "").strip()))
        return
    counts = collections.Counter(local_name(k.tag) for k in kids)
    seen = collections.Counter()
    for k in kids:
        name = local_name(k.tag)
        seen[name] += 1
        label = "%s[%d]" % (name, seen[name]) if counts[name] > 1 else name
        flatten(k, "%s.%s" % (path, label) if path else label, out)


def import_problems(el, path="", skip=()):
    """Elements named like error/errors/failure that carry content, and
    ok/success flags that are false. A flagged element's content is not
    searched again; elements in skip are left out."""
    found = []
    for k in el:
        if k in skip:
            continue
        name = local_name(k.tag)
        here = "%s.%s" % (path, name) if path else name
        text = text_of(k)
        if _ERRORISH.search(name) and text.lower() not in _EMPTYISH:
            found.append((here, text))
            continue
        if name.lower() in _OKISH and text.lower() == "false":
            found.append((here, text))
            continue
        found.extend(import_problems(k, here))
    return found


def soap_body(r):
    """The SOAP Body of Reply r; Failed if it is not a SOAP reply."""
    if r.problem:
        raise Failed("HTTP %d %s, but %s. It starts: %s" % (r.status, r.reason, r.problem,
                                                           r.start()))
    root = r.root
    if root is None or local_name(root.tag) != "Envelope":
        raise Failed("HTTP %d %s, and the reply is not a SOAP envelope: %s"
                     % (r.status, r.reason, r.start()))
    body = root.find(q(split_tag(root.tag)[0], "Body"))
    if body is None:
        raise Failed("HTTP %d: the SOAP reply has no Body." % r.status)
    return body


def soap_fault(body_el):
    """(code, text, detail text, detail element names) or None. Guidewire
    names a declared exception only by the detail's (empty) child element,
    e.g. WsiAuthenticationException, and its faultstring may be empty."""
    for ns in (NS_SOAP11_ENV, NS_SOAP12_ENV):
        fault = body_el.find(q(ns, "Fault"))
        if fault is None:
            continue
        if ns == NS_SOAP11_ENV:
            code = fault.findtext("faultcode") or ""
            text = fault.findtext("faultstring") or ""
            detail = fault.find("detail")
        else:
            code = fault.findtext("{%s}Code/{%s}Value" % (ns, ns)) or ""
            text = fault.findtext("{%s}Reason/{%s}Text" % (ns, ns)) or ""
            detail = fault.find(q(ns, "Detail"))
        detail_text = text_of(detail) if detail is not None else ""
        names = [local_name(k.tag) for k in detail] if detail is not None else []
        return code.strip(), text.strip(), detail_text, names
    return None


_AUTH_HINT = re.compile(r"authenticat|password|credential|permission|not authori[sz]ed|"
                        r"unauthori[sz]ed|login|access denied|soapadmin", re.IGNORECASE)
PERMISSION_HELP = ("Guidewire refused the login or the permission: check that the user name and "
                   "password are right and the user is active, and that it has a role with the "
                   "soapadmin (SOAP administration) system permission, which every "
                   "ImportToolsAPI operation checks (the base roles superuser and user_admin "
                   "have it). Ask the Guidewire admins to check the user's roles.")


_AUTH_DETAIL = re.compile(r"authenticat|permission|authori[sz]", re.IGNORECASE)


_NOT_ALLOWED = re.compile(r"unauthori[sz]ed access|permission|not authori[sz]ed|access denied",
                          re.IGNORECASE)
NOT_ALLOWED_HELP = ("Guidewire answers 'Bad username or password' for a wrong password, so the "
                    "password was accepted; the user may not call this service. ImportToolsAPI "
                    "checks the soapadmin (SOAP administration) system permission: give the user a "
                    "role that has it (the base roles superuser and user_admin do). If its roles "
                    "already have it, ask the Guidewire admins what limits this user's web-service "
                    "calls (for example a custom authentication plugin), or which user to use.")


def auth_help(fault):
    """What to do about an auth fault: the method, the password, or the permission."""
    code, text, detail, names = fault
    said = text + " " + detail
    if re.search(r"multiple authentication methods", said, re.IGNORECASE):
        return ("This server accepts one sign-in method per request: set GW_AUTH to header "
                "(or basic) in gw-admin-data, or remove GW_AUTH=both.")
    if re.search(r"bad user ?name or password", said, re.IGNORECASE):
        return ("The user name or password is wrong for this server: check username and password "
                "(or <ENV>_USERNAME and <ENV>_PASSWORD) in gw-admin-data, for example by logging "
                "in to the application's web page with them.")
    if _NOT_ALLOWED.search(said):
        return NOT_ALLOWED_HELP
    return PERMISSION_HELP


def login_fault_reason(fault):
    """'permission' when the login worked but the call is not allowed,
    'credentials' for any other auth fault, None for a fault that isn't one."""
    if not is_auth_fault(fault):
        return None
    code, text, detail, names = fault
    if re.search(r"multiple authentication methods|bad user ?name or password",
                 text + " " + detail, re.IGNORECASE):
        return "credentials"
    return "permission" if _NOT_ALLOWED.search(text + " " + detail) else "credentials"


def is_auth_fault(fault):
    """Typed faults name their exception in the detail element: trust that
    (a DataConversionException about a Credential's Password field is a data
    error, not a login failure). Only an untyped fault is judged by its text."""
    code, text, detail, names = fault
    if names:
        return any(_AUTH_DETAIL.search(n) for n in names)
    return bool(_AUTH_HINT.search(" ".join([text, detail])))


def fault_summary(fault):
    code, text, detail, names = fault
    return clean(text, 300) or ", ".join(names) or clean(code, 100) or "(no text)"


def print_fault(fault):
    code, text, detail, names = fault
    say("  SOAP Fault")
    say("    faultcode   : %s" % clean(code, 200))
    say("    faultstring : %s" % (clean(text, 2000) or "(empty)"))
    if names:
        say("    detail      : %s" % clean(", ".join(names), 500))
    if detail:
        say("    detail text : %s" % clean(detail, 2000))


def entry_text(el):
    """A list entry as 'Name=value, ...', or its text."""
    kids = list(el)
    if not kids:
        return "" if is_nil(el) else (el.text or "").strip()
    return ", ".join("%s=%s" % (local_name(k.tag), text_of(k)) for k in kids)


def print_result(result, r):
    """ImportResults (Ok, ErrorLog, Summaries, ParseTime, WriteTime, Details):
    the first five in full, then Details as a count and its first entries --
    one per imported record, so they would bury the rest. Other fields, or a
    reply of another shape, are flattened (bounded). Returns the problems."""
    say("  Result (%s):" % local_name(result.tag))
    by = {}
    for k in result:
        by.setdefault(local_name(k.tag).lower(), k)
    known = ("ok", "errorlog", "summaries", "parsetime", "writetime", "details")
    problems = []
    if not any(n in by for n in known):
        lines = []
        flatten(result, "", lines)
        for name, value in lines[:MAX_RESULT_LINES]:
            say("    %s: %s" % (clean(name or local_name(result.tag), 200),
                                clean(value, 300) if value else "(empty)"))
        if len(lines) > MAX_RESULT_LINES:
            say("    ... and %d more lines" % (len(lines) - MAX_RESULT_LINES))
        problems = import_problems(result)
        if not problems and not list(result) and _ERRORISH.search(local_name(result.tag)) and \
                (result.text or "").strip().lower() not in _EMPTYISH:
            problems = [(local_name(result.tag), result.text.strip())]
        if not problems:
            # Success is Ok=true; a reply without it doesn't confirm anything.
            problems = [("Ok", "not in the reply -- the server's answer does not confirm the "
                               "import")]
        return problems + r.skipped_problems

    ok = by.get("ok")
    ok_text = text_of(ok).lower() if ok is not None else ""
    say("    Ok         : %s" % (clean(ok_text, 50) if ok is not None else "(not in the reply)"))
    if ok is None:
        problems.append(("Ok", "not in the reply -- the server's answer does not confirm the "
                               "import"))
    elif ok_text != "true":
        problems.append(("Ok", ok_text or "(empty)"))

    log = by.get("errorlog")
    entries = list(log) if log is not None else []
    if log is not None and not entries and text_of(log):
        entries = [log]
    say("    ErrorLog   : %s" % ("%d entr%s" % (len(entries), "y" if len(entries) == 1 else "ies")
                                 if entries else "none"))
    shown = 0
    for i, e in enumerate(entries, 1):
        for n, line in enumerate((text_of(e) or "(empty)").splitlines() or ["(empty)"]):
            if shown < MAX_ERRORLOG_LINES:
                say("      %s %s" % ("[%d]" % i if n == 0 else "   ", clean(line, 2000)))
            shown += 1
    if shown > MAX_ERRORLOG_LINES:
        say("      ... and %d more lines" % (shown - MAX_ERRORLOG_LINES))
    if entries:
        problems.append(("ErrorLog", "%d entr%s; the first: %s"
                         % (len(entries), "y" if len(entries) == 1 else "ies",
                            clean(text_of(entries[0]), 300) or "(empty)")))

    summaries = list(by["summaries"]) if "summaries" in by else []
    say("    Summaries  : %s" % ("%d" % len(summaries) if summaries else "none"))
    for e in summaries:
        f = {local_name(k.tag).lower(): text_of(k) for k in e}
        if "entityname" in f or "count" in f:
            say("      %-28s %8s%s" % (clean(f.get("entityname", "?"), 60),
                                       clean(f.get("count", "?"), 20),
                                       ("   (Type %s)" % clean(f["type"], 20)) if "type" in f
                                       else ""))
        else:
            say("      %s" % clean(entry_text(e), 500))
    for key, label in (("parsetime", "ParseTime"), ("writetime", "WriteTime")):
        if key in by:
            say("    %-10s : %s ms" % (label, clean(text_of(by[key]), 30)))

    details = by.get("details")
    if details is not None:
        total = r.details_counts.get(details, len(details))
        say("    Details    : %d entr%s%s" % (total, "y" if total == 1 else "ies",
                                             " (the first %d below)" % min(total, DETAILS_SHOWN)
                                             if total else ""))
        for i, e in enumerate(list(details)[:DETAILS_SHOWN], 1):
            say("      [%d] %s" % (i, clean(entry_text(e), 500)))

    others = [k for k in result if local_name(k.tag).lower() not in known]
    lines = []
    for k in others:
        flatten(k, local_name(k.tag), lines)
    for name, value in lines[:MAX_RESULT_LINES]:
        say("    %s: %s" % (clean(name, 200), clean(value, 300) if value else "(empty)"))
    if len(lines) > MAX_RESULT_LINES:
        say("    ... and %d more lines" % (len(lines) - MAX_RESULT_LINES))
    problems += import_problems(result, skip=[x for x in (ok, log) if x is not None])
    return problems + r.skipped_problems


def handle_response(r, p, cfg):
    # A 200 whose SOAP reply could not be read to the end (too large, cut
    # off, timed out) means the server got the file and answered.
    applied = (" The server answered HTTP 200, so the import has probably been applied, fully "
               "or in part: check the data in Guidewire before re-running."
               if r.status == 200 and r.problem and (not r.head or b"Envelope" in r.head)
               else "")
    if r.status in (401, 403):
        raise Failed("HTTP %d %s: Guidewire %s the import (credentials from %s). %s Also try "
                     "GW_AUTH=basic or GW_AUTH=header if the server accepts only one style."
                     % (r.status, r.reason, "did not accept the credentials for" if r.status == 401
                        else "refused permission for", cfg.cred_src, PERMISSION_HELP))
    if r.status in (301, 302, 303, 307, 308):
        raise Failed("HTTP %d: the endpoint redirected; not followed. Nothing was imported."
                     % r.status)
    try:
        body_el = soap_body(r)
    except Failed as ex:
        raise Failed(str(ex) + applied)
    fault = soap_fault(body_el)
    if fault:
        print_fault(fault)
        raise Failed("The server answered with a SOAP Fault (HTTP %d): %s.%s"
                     % (r.status, fault_summary(fault),
                        (" " + auth_help(fault)) if is_auth_fault(fault) else ""))
    if r.status != 200:
        raise Failed("HTTP %d %s with a SOAP reply that holds no Fault." % (r.status, r.reason))
    wrappers = list(body_el)
    if not wrappers:
        raise Failed("The server returned an empty SOAP Body: its answer does not confirm the "
                     "import. Check the data in Guidewire before re-running.")
    wrapper = wrappers[0]
    items = list(wrapper)
    result = items[0] if len(items) == 1 else wrapper
    problems = print_result(result, r)
    if problems:
        for name, text in problems[:20]:
            error("Import reported: %s = %s" % (clean(name, 200), clean(text, 500)))
        raise Failed("The import reported %d problem(s) (above). Part of the file may have been "
                     "applied: check the data in Guidewire before re-running." % len(problems))


def do_import(cfg):
    http, plan = do_check(cfg)
    info = do_validate(cfg)
    section("Import")
    if info["refuse"]:
        raise Refused(info["refuse"] + " Nothing was sent.")
    if cfg.is_prod:
        if cfg.confirm != cfg.env:
            raise Refused("%s is a production environment (it matches PROD_ENV_PATTERN %s). "
                          "Type %s in 'confirm' to load it. Nothing was sent."
                          % (cfg.env, cfg.prod_pattern, cfg.env))
        say("  Production load confirmed for %s." % cfg.env)
    body = build_envelope(cfg, plan, info["encoding"])
    field("sending", "%s -> %s %s" % (cfg.file_rel, plan.op, plan.endpoint))
    field("request size", "%s (sha256 of the file %s)" % (human_bytes(body.length),
                                                         info["sha256"][:16]))
    if body.length > GW_UPLOAD_DEFAULT_BYTES:
        warn("The request is %s, over Guidewire's default MaximumFileUploadSize (20 MB, "
             "config.xml). A server that keeps the default may reject it; if it does, split the "
             "file or ask the Guidewire admins to raise the limit." % human_bytes(body.length))
    t0 = time.monotonic()
    try:
        r = http.soap(plan.endpoint, body, soap_headers(cfg, plan, body.length), cfg.timeout,
                      MAX_IMPORT_REPLY_BYTES, "POST %s" % plan.endpoint)
    except Failed as ex:
        if "timed out" in str(ex):
            raise Failed("No reply within GW_TIMEOUT (%d s). The server may still be importing: "
                         "check its log before running again (re-importing the same file is "
                         "normally harmless, but wait for the first one to finish), or raise "
                         "GW_TIMEOUT." % cfg.timeout)
        raise
    field("server time", "%.1f s (HTTP %d, %s reply)" % (time.monotonic() - t0, r.status,
                                                         human_bytes(r.size)))
    handle_response(r, plan, cfg)
    say("  Import finished without reported errors.")
    say("  Next: run the User Exception and Group Exception batch processes (Server Tools -> "
        "Batch Process Info), as Guidewire advises after an admin-data import, and spot-check "
        "the data in the application.")


# --------------------------------------------------------------------------
def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Load a Guidewire admin-data XML file through ImportToolsAPI.")
    ap.add_argument("--action", required=True, choices=("check", "validate", "import"))
    ap.add_argument("--env", required=True, help="environment name, e.g. DEV_1")
    ap.add_argument("--product", required=True, type=str.lower, choices=sorted(APP_CONTEXT))
    ap.add_argument("--file", default="none",
                    help="XML file, relative to the repo root (the current directory)")
    ap.add_argument("--confirm", default="none",
                    help="production only: the environment name again")
    return ap.parse_args(argv)


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    args = parse_args(argv)
    t0 = time.monotonic()
    rc = EXIT_OK
    try:
        cfg = load_config(args)
        print_config(cfg)
        if cfg.action == "check":
            do_check(cfg)
        elif cfg.action == "validate":
            do_validate(cfg)
        else:
            do_import(cfg)
    except Refused as ex:
        error("Refused: %s" % ex)
        rc = EXIT_REFUSED
    except Failed as ex:
        error(str(ex))
        rc = EXIT_FAILED
    except KeyboardInterrupt:
        error("Interrupted.")
        rc = EXIT_FAILED
    except Exception as ex:     # report, sanitised, rather than a raw traceback
        error("Unexpected %s: %s" % (type(ex).__name__, ex))
        for line in traceback.format_exc().splitlines():
            say("  " + line)
        rc = EXIT_FAILED
    say("")
    say("Elapsed: %.1f s, exit %d (%s)" % (time.monotonic() - t0, rc,
                                          {0: "ok", 1: "failed", 2: "refused"}[rc]))
    return rc


if __name__ == "__main__":
    sys.exit(main())
