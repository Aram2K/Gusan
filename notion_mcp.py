"""Notion through its hosted MCP server: one "Connect Notion" button, no sharing,
no secret, no page picking. Qt-free; stdlib + requests; every call is blocking, so
run it on a worker thread.

Why this exists
---------------
Notion's hosted MCP server (https://mcp.notion.com/mcp) is an OAuth 2.0 server with
dynamic client registration (RFC 7591), PKCE and public clients. The user presses one
button, Notion's own consent page opens in the browser (workspace picker, "Continue"),
and the app gets a token that acts AS THE USER: no page has to be shared with an
integration, and "draft" pages can be created in the workspace without a parent. The
token works ONLY against the MCP endpoint, never against api.notion.com.

What is in here (top to bottom)
-------------------------------
* transport     :func:`_request` is the ONE place that touches the network (``requests``);
                tests swap it. Replies are :class:`Reply` objects (status, headers, body or
                a lazy event stream). No redirect is followed, and an answer larger than
                ``MAX_BODY`` is dropped.
* discovery     :func:`discover` reads the two ``.well-known`` documents and falls back to
                the known endpoints. Every address a document names must be https on
                notion.com (see :func:`_safe_url`), or it is ignored and the known one is used.
* registration  :func:`register_client` / :func:`client_id_for`. ONE client id per
                install, kept in the config key ``notion_mcp_client_id`` and reused for
                ever (a second registration would orphan earlier grants). A client secret,
                if a server ever hands one out, goes to the keyring
                (``storage.NOTION_MCP_SECRET``); Notion hands out none. The client is
                registered with TWO return addresses (our site's page, then the loopback
                one); ``notion_mcp_redirects`` remembers which set the saved client knows
                (:data:`REDIRECTS_VERSION`), so an install whose client knows only the
                loopback address registers a new one at the next Connect that wants the site.
* connect       :func:`connect` PKCE + a one-shot listener on 127.0.0.1 (the registered
                port when it is free, else a random one: RFC 8252 lets the port vary),
                state check, 180 s timeout, cancel Event; then the code is exchanged, the
                private "Gusan meetings" page is made, and a ready-to-save result comes back.
                Before the browser opens, the authorize address is asked once
                (:func:`authorize_refused`): a client Notion has forgotten is registered again
                instead of the user waiting out the timeout on an error page.
                WHERE NOTION SENDS THE BROWSER BACK: Notion's consent page shows the return
                address, and a bare ``http://127.0.0.1:53682/...`` looks unprofessional, so
                when our site's page (:data:`RETURN_URL`) is up - :func:`return_page_ready`
                asks it once, 4 s, and wants a 200 with ``<meta name="gusan-return"
                content="1">`` in the body - Notion returns there. That page forwards the
                query, unchanged, to the listener (the port rides in ``state``, which is
                ``"<port>.<random>"`` then). The site down, slow, or answering anything else
                (a 404, a catch-all 200): the loopback return, exactly as before. The
                ``redirect_uri`` of the token call is always the one the authorize step used.
                THE BROWSER TAB: the listener answers the browser only when the connect is
                done ("Connected." / "That didn't work."), at most notion_connect.HOLD_SECONDS
                later (then the usual "Almost done").
* tokens        :func:`renew` / :class:`Credentials`. Access tokens last about 8 hours and
                are renewed when under 5 minutes are left (or after a 401). The refresh
                token ROTATES on every refresh: refreshes are serialised by ``_LOCK``, the
                new pair is handed to ``persist`` BEFORE anything else is sent, and an old
                token a second caller still holds is never replayed (it would revoke the
                whole grant). ``invalid_grant`` is final: ``notion_reconnect`` is set
                through ``persist`` and nothing is retried. Credentials read before the
                connection was forgotten (:func:`generation`) are never refreshed and never
                flag a reconnect: a send that was running at Disconnect ends quietly.
* client        :class:`McpClient` speaks MCP over Streamable HTTP: initialize,
                notifications/initialized, tools/call; JSON or SSE replies, Mcp-Session-Id
                echoed, one retry after a 401 (with a refresh) and one after a short 429.
                Failures are the app's own :class:`notion_export.NotionError` family, with
                the same ``code`` values (auth | no_access | rate | offline | full | other);
                "full" = the workspace has used up its free blocks (a free workspace with
                more than one member has a total limit): no page can be made until the user
                frees space, upgrades or picks another workspace.
* sending       :func:`ensure_hub` (the private "Gusan meetings" page, made ONCE and again
                only when Notion says it is gone), :func:`send_meeting_mcp` (one page per
                meeting under it) and the Markdown the pages are written in.

Nothing here reads or writes the config: callers pass what they have (``cfg``,
:class:`Credentials`) and a ``persist(dict)`` callback that is called ON THE WORKER
THREAD with config keys to save (main: ``self._run_on_gui(self.set_settings, d)``).
The keys this module asks to be saved: ``notion_api_key``, ``notion_refresh_token``,
``notion_token_expires``, ``notion_reconnect``, ``notion_mcp_client_id``,
``notion_mcp_redirects``, ``notion_hub_page``, ``notion_parent_page``, ``notion_parent_title``.

Privacy of the logs: a token, a code, a verifier, a client secret and the return address's
query never reach a message or the log; only fixed words and status numbers do - with ONE
exception, on purpose: the start of a tool's error message is logged (:func:`_snippet`) so a
failure can be understood from the log. It is one line of at most 240 characters, with every
address cut before its query, every long id-like run, ``name=value`` secret and the tokens
this client holds masked. It is never shown to the user: the sentences are the app's own.
"""
import base64
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import urllib.parse

import requests

import notion_connect
import notion_export
import storage
from app_info import APP_NAME, APP_VERSION
from notion_connect import ConnectCancelled, ConnectError
from notion_export import NotionAuthError, NotionError, NotionNotFoundError

logger = logging.getLogger("transcribe.notion")

# ── constants ────────────────────────────────────────────────────────────────
MCP_BASE = "https://mcp.notion.com"
MCP_PATH = "/mcp"
CLIENT_NAME = APP_NAME
CLIENT_URI = "https://gusan.aibuben.xyz"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
# The door to a Client ID Metadata Document (Notion advertises it, nobody needs to register):
# once a small JSON file (client_id = its own https address, client_name, redirect_uris
# ["http://127.0.0.1/notion/callback"], token_endpoint_auth_method "none") is hosted at an https
# address, put that address here and every install uses it as its client id with no
# registration at all (its redirect_uris must then list RETURN_URL as well as the loopback
# one). Empty = register once per install (what works today).
CLIENT_ID_URL = ""
CALLBACK_PATH = "/notion/callback"
CALLBACK_PORT = 53682               # the port the client is registered with; used when free
REGISTERED_REDIRECT = f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}"
# Where Notion's consent page sends the browser back when our site can take it: a page that hands the
# answer to the listener on this computer (see the module docstring). Same path as the loopback one.
RETURN_URL = "https://gusan.aibuben.xyz" + CALLBACK_PATH
RETURN_TIMEOUT = 4                  # seconds the site gets to answer the one question asked before a connect
RETURN_PEEK = 65536                 # bytes of its answer that are looked at
# The mark our own page carries (a catch-all 200 or somebody else's page does not): either attribute order,
# quotes optional (a HTML minifier drops them: the page would then never count, and nobody would see why).
_RETURN_MARKER = re.compile(
    r"""<meta\s+name\s*=\s*["']?gusan-return["']?\s+content\s*=\s*["']?1["']?(?:\s[^>]*)?/?>|"""
    r"""<meta\s+content\s*=\s*["']?1["']?\s+name\s*=\s*["']?gusan-return["']?(?:\s[^>]*)?/?>""", re.I)
# Which set of return addresses the saved client was registered with ("notion_mcp_redirects"):
# "" / missing = the loopback one only (2.0.0 and 2.0.1 before the site page), "2" = RETURN_URL and
# the loopback one.
REDIRECTS_VERSION = "2"
PROTOCOL_VERSION = "2025-06-18"     # the MCP revision we speak (the server may answer with its own)
LOGIN_TIMEOUT = notion_connect.LOGIN_TIMEOUT
HTTP_TIMEOUT = 15                   # discovery, registration, token calls
TOOL_TIMEOUT = (10, 90)             # (connect, read) for MCP requests: a page with a long transcript takes a while
REFRESH_EARLY = 300                 # renew an access token with fewer seconds than this left
RETRY_AFTER_MAX = notion_export.RETRY_AFTER_MAX
DISCOVERY_TTL = 3600
MAX_TOKEN_LEN = 4096
MAX_BODY = 8 * 1024 * 1024          # the most one answer (a body, or one event stream) may hold
STREAM_SECONDS = 300                # the most an event stream may keep us waiting for its answer
HUB_TITLE = notion_connect.HUB_TITLE
HUB_INTRO = notion_connect.HUB_INTRO
HUB_ICON = "📓"
MEETING_ICON = "🎙️"
DEFAULT_TITLE = "Meeting notes"
MAX_TITLE_LEN = 200
CHUNK_CHARS = 12000                 # Markdown per call (a page is made with the first chunk, the rest is appended)
MIN_CHUNK_CHARS = 1500              # a "too big" answer halves the chunk, down to this
MAX_SHRINKS = 4
MAX_LINE_CHARS = 1800               # a longer paragraph is split at a space (Notion text items hold 2000)
ASYNC_POLLS = 30
ASYNC_WAIT = 1.0

CLIENT_SECRET_NAME = storage.NOTION_MCP_SECRET

_SAFE_ERROR = re.compile(r"[a-z_]{1,40}")
_CLIENT_ID = re.compile(r"[\x21-\x7e]{1,256}")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _now():
    return time.time()


def _sleep(seconds):
    time.sleep(seconds)


# ── transport: the one place that touches the network ───────────────────────
class NetworkError(OSError):
    """The server could not be reached (offline, DNS, TLS, timeout, a dropped stream)."""


class Reply:
    """What :func:`_request` returns: ``status``, lower-case ``headers``, ``body``
    (bytes) - or, for a ``text/event-stream`` answer, a lazy line iterator
    (:meth:`iter_lines`) that is read only as far as needed."""

    def __init__(self, status, headers=None, body=b"", lines=None, close=None):
        self.status = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.body = body or b""
        self._lines = lines
        self._close = close

    def header(self, name, default=""):
        return self.headers.get(name.lower(), default)

    @property
    def is_stream(self):
        return self._lines is not None

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def iter_lines(self):
        if self._lines is None:
            yield from self.body.decode("utf-8", "replace").splitlines()
            return
        try:
            for raw in self._lines:
                yield raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except NetworkError:
            raise
        except Exception as e:                      # a connection dropped mid-stream
            raise NetworkError("stream") from e

    def close(self):
        if self._close is not None:
            try:
                self._close()
            except Exception:
                pass


def _request(method, url, *, headers=None, json_body=None, form=None, timeout=HTTP_TIMEOUT):
    """One HTTP call -> :class:`Reply`; :class:`NetworkError` when nothing answers.
    Never follows a redirect (a token must not travel to wherever a 302 points)."""
    kw = {"headers": dict(headers or {}), "timeout": timeout, "allow_redirects": False, "stream": True}
    if json_body is not None:
        kw["json"] = json_body
    if form is not None:
        kw["data"] = form
    try:
        resp = requests.request(method, url, **kw)
        hdrs = dict(resp.headers)
        ctype = str(resp.headers.get("Content-Type") or "").lower()
        if ctype.startswith("text/event-stream") and 200 <= resp.status_code < 300:
            # chunk_size=1: hand every line over as soon as it arrives, so a server that
            # keeps the stream open after its answer cannot make us wait for more.
            return Reply(resp.status_code, hdrs, lines=resp.iter_lines(chunk_size=1), close=resp.close)
        try:
            # Read in pieces and stop at MAX_BODY: whatever a server (or a box in between) sends
            # back, it can never fill this computer's memory.
            parts, size = [], 0
            for piece in resp.iter_content(chunk_size=65536):
                size += len(piece)
                if size > MAX_BODY:
                    logger.info("Notion %s: the answer was too large, dropped", method.lower())
                    raise NetworkError("too large")
                parts.append(piece)
            body = b"".join(parts)
        finally:
            resp.close()
    except requests.RequestException as e:
        logger.info("Notion %s: couldn't connect (%s)", method.lower(), type(e).__name__)
        raise NetworkError("offline") from e
    return Reply(resp.status_code, hdrs, body)


def _safe_code(data):
    """The OAuth ``error`` word of a JSON answer when it is a plain snake_case word
    (safe to log), else ""."""
    err = data.get("error") if isinstance(data, dict) else ""
    return err if isinstance(err, str) and _SAFE_ERROR.fullmatch(err) else ""


def _save(persist, updates):
    """Hand config keys to the caller's ``persist`` (it must never break the flow)."""
    if persist is None:
        return
    try:
        persist(dict(updates))
    except Exception:
        logger.warning("Saving the Notion settings failed", exc_info=True)


def _is_notion_host(host):
    return host == "notion.com" or host.endswith(".notion.com")


def _safe_url(url, base=None):
    """The url when it may be followed, "" otherwise. ``base`` is the server discovery
    started from (default MCP_BASE). A code, a verifier and the refresh token travel to
    whatever address a discovery document names, so the address must be https, on the
    standard port, with no user name, and on the same host as ``base`` or - when ``base``
    is a notion.com host - on another notion.com host. (http on a loopback host is allowed
    only when ``base`` itself is on loopback: that is how the tests run a server on this
    computer; it never applies to Notion.)"""
    if not isinstance(url, str) or not url or len(url) > 2048 or re.search(r"\s", url):
        return ""
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port
        base_host = (urllib.parse.urlsplit(base or MCP_BASE).hostname or "").lower()
    except ValueError:
        return ""
    if p.username or p.password or p.fragment or not host or not base_host:
        return ""
    if p.scheme == "http":
        return url if host in _LOOPBACK_HOSTS and base_host in _LOOPBACK_HOSTS else ""
    if p.scheme == "https" and port in (None, 443) \
            and (host == base_host or (_is_notion_host(host) and _is_notion_host(base_host))):
        return url
    return ""


# ── discovery ────────────────────────────────────────────────────────────────
class Endpoints:
    """Where the authorization server and the MCP endpoint are."""
    __slots__ = ("base", "issuer", "authorize", "token", "register", "revoke", "mcp")

    def __init__(self, base, issuer=None, mcp=None):
        base = base.rstrip("/")
        self.base = base
        self.issuer = (issuer or base).rstrip("/")
        self.authorize = self.issuer + "/authorize"
        self.token = self.issuer + "/token"
        self.register = self.issuer + "/register"
        self.revoke = self.token
        self.mcp = mcp or base + MCP_PATH


_DISCOVERED = {}                    # base -> (expires_at, Endpoints)
_DISCOVERY_LOCK = threading.Lock()


def _get_json(url):
    try:
        r = _request("GET", url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    except NetworkError as e:
        raise ConnectError("offline") from e
    data = r.json() if r.status == 200 else None
    return data if isinstance(data, dict) else None


def _discover(base):
    ep = Endpoints(base)
    doc = (_get_json(base + "/.well-known/oauth-protected-resource" + MCP_PATH)
           or _get_json(base + "/.well-known/oauth-protected-resource") or {})
    resource = _safe_url(doc.get("resource"), base)
    if resource and urllib.parse.urlsplit(resource).path.rstrip("/") == MCP_PATH \
            and urllib.parse.urlsplit(resource).netloc == urllib.parse.urlsplit(base).netloc:
        ep.mcp = resource.rstrip("/")                   # the access token only ever goes to the host we started from
    servers = doc.get("authorization_servers")
    issuer = _safe_url(servers[0], base) if isinstance(servers, list) and servers else ""
    issuer = (issuer or base).rstrip("/")
    meta = _get_json(issuer + "/.well-known/oauth-authorization-server") or {}
    if meta and str(meta.get("issuer") or "").rstrip("/") != issuer:
        logger.info("Notion discovery: the issuer does not match, using the known endpoints")
        meta = {}
    methods = meta.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        logger.warning("Notion discovery: the server does not offer PKCE S256")
        raise ConnectError("server")
    ep = Endpoints(base, issuer=issuer, mcp=ep.mcp)
    for slot, key in (("authorize", "authorization_endpoint"), ("token", "token_endpoint"),
                      ("register", "registration_endpoint"), ("revoke", "revocation_endpoint")):
        url = _safe_url(meta.get(key), base)
        if url:
            setattr(ep, slot, url)
        elif slot == "revoke":
            ep.revoke = ep.token
    return ep


def discover(base=None, force=False):
    """The :class:`Endpoints` of the Notion MCP server (cached for an hour). Raises
    ConnectError "offline" when nothing answers; a server that answers oddly falls back
    to the known paths, because they have not moved."""
    base = (base or MCP_BASE).rstrip("/")
    with _DISCOVERY_LOCK:
        hit = _DISCOVERED.get(base)
        if hit and not force and hit[0] > _now():
            return hit[1]
    ep = _discover(base)
    with _DISCOVERY_LOCK:
        _DISCOVERED[base] = (_now() + DISCOVERY_TTL, ep)
    return ep


def forget_discovery():
    with _DISCOVERY_LOCK:
        _DISCOVERED.clear()


# ── registration: one client id per install ─────────────────────────────────
_REG_LOCK = threading.Lock()
_CLIENT_MEMO = {}                   # issuer -> client id registered in this run (before the config has it)


def _valid_client_id(value):
    return isinstance(value, str) and bool(_CLIENT_ID.fullmatch(value))


def redirect_uris():
    """The return addresses a new client is registered with: our site's page first, then the
    loopback one (the port is only a placeholder: Notion lets it vary at authorize time)."""
    return [RETURN_URL, REGISTERED_REDIRECT]


def return_page_ready(url=None, timeout=None):
    """Is our site's return page up? Asked once per connect, with no redirect followed: True
    only for a 200 whose body carries our own mark (:data:`_RETURN_MARKER`). A site that is down,
    slow (``timeout`` seconds in all, however the answer trickles in), moved (3xx), missing
    (404) or a catch-all that answers 200 with its own page is "not ready", and the connect
    uses the loopback return as it always has. Never raises."""
    url = url or RETURN_URL
    limit = RETURN_TIMEOUT if timeout is None else timeout
    box = []

    def ask():
        try:
            box.append(_request("GET", url, headers={"Accept": "text/html", "User-Agent": USER_AGENT},
                                timeout=limit))
        except Exception:
            box.append(None)

    worker = threading.Thread(target=ask, name="gusan-notion-return", daemon=True)
    worker.start()
    worker.join(limit)
    reply = box[0] if box else None
    if reply is None:
        logger.info("Notion connect: our return page did not answer, using the loopback return")
        return False
    try:
        if reply.status != 200:
            logger.info("Notion connect: our return page answered %s, using the loopback return", reply.status)
            return False
        text = reply.body[:RETURN_PEEK].decode("utf-8", "replace")
    finally:
        reply.close()
    if not _RETURN_MARKER.search(text):
        logger.info("Notion connect: that page is not ours, using the loopback return")
        return False
    return True


def register_client(ep, redirect_uri=None):
    """Register Gusan as a public client. Returns {"client_id", "client_secret" ("" when none)}.
    ``redirect_uri`` is one address (or a list); the default is :func:`redirect_uris`: our
    site's page AND the loopback one, so that either can be used at authorize time.
    Raises ConnectError (offline | server | other)."""
    if redirect_uri is None:
        uris = redirect_uris()
    else:
        uris = [redirect_uri] if isinstance(redirect_uri, str) else list(redirect_uri)
    body = {"client_name": CLIENT_NAME, "client_uri": CLIENT_URI, "redirect_uris": uris,
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none"}
    try:
        r = _request("POST", ep.register, headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                     json_body=body, timeout=HTTP_TIMEOUT)
    except NetworkError as e:
        raise ConnectError("offline") from e
    data = r.json()
    if r.status not in (200, 201) or not isinstance(data, dict):
        logger.info("Notion registration: answered %s %s", r.status, _safe_code(data))
        raise ConnectError("server" if r.status == 429 or r.status >= 500 else "other")
    client_id = data.get("client_id")
    if not _valid_client_id(client_id):
        logger.info("Notion registration: no usable client id in the answer")
        raise ConnectError("server")
    secret = data.get("client_secret")
    return {"client_id": client_id, "client_secret": secret if isinstance(secret, str) else ""}


def client_secret():
    """The client secret kept in the keyring ("" for a public client, which is what
    Notion registers)."""
    try:
        return (storage.read_secret(CLIENT_SECRET_NAME) or "").strip()
    except Exception:
        return ""


def client_for(cfg, ep, persist=None, site=False):
    """(client id, the redirect set it knows) for this install, registering when there is
    none - or, with ``site=True`` (the connect is about to use RETURN_URL), when the saved one
    was registered with the loopback address only (``notion_mcp_redirects`` is not
    :data:`REDIRECTS_VERSION`): Notion cannot add an address to a client afterwards. The new id
    and its version are saved right away through ``persist`` so a connection that fails later
    does not register a second time. A saved client is otherwise reused for ever, and a
    connection that is already live keeps refreshing under the id it has: only Connect gets here."""
    if CLIENT_ID_URL.startswith("https://") and _valid_client_id(CLIENT_ID_URL):
        return CLIENT_ID_URL, REDIRECTS_VERSION         # a hosted client document: nothing to register
    cfg = cfg or {}
    saved = (cfg.get("notion_mcp_client_id") or "").strip()
    known = str(cfg.get("notion_mcp_redirects") or "").strip()
    if _valid_client_id(saved) and (known == REDIRECTS_VERSION or not site):
        return saved, known
    with _REG_LOCK:
        memo = _CLIENT_MEMO.get(ep.issuer)
        if memo:
            return memo, REDIRECTS_VERSION
        reg = register_client(ep)
        _CLIENT_MEMO[ep.issuer] = reg["client_id"]
        if reg["client_secret"]:
            try:
                storage.write_secret(CLIENT_SECRET_NAME, reg["client_secret"])
            except Exception:
                logger.warning("Keeping the Notion client secret failed", exc_info=True)
        _save(persist, {"notion_mcp_client_id": reg["client_id"], "notion_mcp_redirects": REDIRECTS_VERSION})
        return reg["client_id"], REDIRECTS_VERSION


def client_id_for(cfg, ep, persist=None, site=False):
    """The id of :func:`client_for` (see there)."""
    return client_for(cfg, ep, persist, site)[0]


def forget_client(persist=None):
    """Notion no longer knows our client id: drop it so the next connect registers again."""
    with _REG_LOCK:
        _CLIENT_MEMO.clear()
    try:
        storage.write_secret(CLIENT_SECRET_NAME, "")
    except Exception:
        pass
    _save(persist, {"notion_mcp_client_id": "", "notion_mcp_redirects": ""})


# ── PKCE and the authorize address ──────────────────────────────────────────
def make_pkce():
    """(verifier, challenge): a 86-character url-safe verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(64)
    return verifier, challenge_for(verifier)


def challenge_for(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(ep, client_id, redirect_uri, state, challenge):
    """Notion's consent page for this client (workspace picker and "Continue")."""
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "prompt": "consent"})
    return ep.authorize + ("&" if "?" in ep.authorize else "?") + query


# ── the token endpoint ──────────────────────────────────────────────────────
def _form_headers():
    return {"Accept": "application/json", "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded"}


def _token_post(ep, fields, persist=None):
    """POST a form to the token endpoint; the parsed answer, or ConnectError
    (expired | offline | server | other). "expired" = the grant or the client is gone
    for good (the caller sets ``notion_reconnect``)."""
    fields = dict(fields)
    secret = client_secret()
    if secret:
        fields["client_secret"] = secret
    try:
        r = _request("POST", ep.token, headers=_form_headers(), form=fields, timeout=HTTP_TIMEOUT)
    except NetworkError as e:
        raise ConnectError("offline") from e
    data = r.json()
    if r.status == 200 and isinstance(data, dict):
        return data
    err = _safe_code(data)
    logger.info("Notion token endpoint: answered %s %s", r.status, err)
    if err == "invalid_grant":
        raise ConnectError("expired")
    if err in ("invalid_client", "unauthorized_client"):
        forget_client(persist)                  # our registration is gone: register again on the next connect
        raise ConnectError("expired")
    if r.status == 429 or r.status >= 500 or err == "temporarily_unavailable":
        raise ConnectError("server")
    raise ConnectError("other")


def _text(value, limit=200):
    return value[:limit] if isinstance(value, str) else ""


def _parse_tokens(data, old_refresh=""):
    """The token dict of a token answer (keys: access_token, refresh_token, expires_at
    (epoch seconds, 0 = unknown), workspace_id, workspace_name, bot_id). The identity
    fields come only with the first answer; they are "" on a refresh."""
    access = data.get("access_token")
    if not isinstance(access, str) or not access.strip() or len(access) > MAX_TOKEN_LEN \
            or not access.isascii() or re.search(r"\s", access):
        logger.info("Notion token endpoint: an answer without a usable access token")
        raise ConnectError("server")
    refresh = data.get("refresh_token")
    refresh = refresh.strip() if isinstance(refresh, str) and refresh.strip() and len(refresh) <= MAX_TOKEN_LEN \
        and refresh.isascii() and not re.search(r"\s", refresh) else (old_refresh or "")
    try:
        ttl = float(data.get("expires_in"))
    except (TypeError, ValueError):
        ttl = 0.0
    return {"access_token": access.strip(), "refresh_token": refresh,
            "expires_at": _now() + ttl if ttl > 0 else 0.0,
            "workspace_id": _text(data.get("workspace_id")),
            "workspace_name": notion_connect.clean_name(data.get("workspace_name")),
            "bot_id": _text(data.get("bot_id"))}


def exchange_code(ep, client_id, code, redirect_uri, verifier, persist=None):
    """Swap the authorization code for tokens (:func:`_parse_tokens`' dict)."""
    code = (code or "").strip()
    if not code:
        raise ConnectError("other")
    data = _token_post(ep, {"grant_type": "authorization_code", "code": code, "client_id": client_id,
                            "redirect_uri": redirect_uri, "code_verifier": verifier}, persist)
    return _parse_tokens(data)


# ── tokens that rotate ──────────────────────────────────────────────────────
_LOCK = threading.RLock()           # one refresh at a time, process-wide (held across the network call)
_STATE = threading.RLock()          # guards the four names below; held for an instant, NEVER across the network,
#                                     so the GUI thread (a disconnect) never waits behind a refresh
_LATEST = {}                        # the newest {"access_token", "refresh_token", "expires_at"} this run got
_SUPERSEDED = []                    # refresh tokens already swapped for a newer one
_SUPERSEDED_KEEP = 16
_GEN = [0]                          # +1 whenever the chain is forgotten or replaced (see renew)


def latest_tokens():
    """The newest token pair of this run ({} before any), newer than the saved config
    until the caller's save has run."""
    with _STATE:
        return dict(_LATEST)


def forget_tokens():
    """A new connection or a disconnect starts a new chain (and forgets the hub memo). Never
    waits for a refresh that is in flight: that refresh sees the chain changed and keeps
    nothing."""
    with _STATE:
        _LATEST.clear()
        del _SUPERSEDED[:]
        _HUBS.clear()
        _GEN[0] += 1


def generation():
    """Which connection this run is on: it changes whenever the connection is forgotten
    (Disconnect, another account, a grant given back) or replaced (a new Connect)."""
    with _STATE:
        return _GEN[0]


def _adopt_chain(tokens):
    """A fresh connection: its pair is the newest, with no history. Returns its generation."""
    with _STATE:
        del _SUPERSEDED[:]
        _LATEST.clear()
        _LATEST.update(access_token=tokens["access_token"], refresh_token=tokens["refresh_token"],
                       expires_at=tokens["expires_at"])
        _GEN[0] += 1
        return _GEN[0]


class Credentials:
    """The pair a connection holds, plus its client id: ``access``, ``refresh``,
    ``expires_at`` (epoch seconds, 0 = unknown), ``client_id``. Updated in place when a
    refresh swaps it, so whoever holds the object always sends the current token.
    ``gen`` is the :func:`generation` the connection was read in (None = not tied to one): a
    holder whose connection was forgotten since is never refreshed and never saves anything."""

    def __init__(self, access="", refresh="", expires_at=0.0, client_id=""):
        self.access = (access or "").strip()
        self.refresh = (refresh or "").strip()
        self.expires_at = float(expires_at or 0)
        self.client_id = (client_id or "").strip()
        self.gen = None

    @classmethod
    def from_cfg(cls, cfg):
        cfg = cfg or {}
        try:
            expires = float(cfg.get("notion_token_expires") or 0)
        except (TypeError, ValueError):
            expires = 0.0
        creds = cls(cfg.get("notion_api_key"), cfg.get("notion_refresh_token"), expires,
                    cfg.get("notion_mcp_client_id"))
        creds.adopt_newer()
        creds.gen = generation()
        return creds

    def current(self):
        """False once the connection these credentials belong to was forgotten or replaced."""
        return self.gen is None or self.gen == generation()

    def expiring(self):
        """True when the access token has less than REFRESH_EARLY seconds left."""
        return bool(self.expires_at) and self.expires_at - _now() < REFRESH_EARLY

    def expired(self):
        return bool(self.expires_at) and self.expires_at <= _now()

    def adopt_newer(self):
        """Take this run's newest pair when ours is an older link of the same chain."""
        with _STATE:
            if _LATEST and (self.refresh in _SUPERSEDED or (self.refresh and self.refresh == _LATEST["refresh_token"])):
                self.access = _LATEST["access_token"]
                self.refresh = _LATEST["refresh_token"]
                self.expires_at = float(_LATEST.get("expires_at") or 0)

    def updates(self):
        """The config keys that hold this pair."""
        return {"notion_api_key": self.access, "notion_refresh_token": self.refresh,
                "notion_token_expires": self.expires_at}


def renew(creds, persist=None, ep=None):
    """Make ``creds`` fresh: take a newer pair this run already has, or swap the refresh
    token at Notion. The new pair is handed to ``persist`` (with ``notion_reconnect``
    False) before this returns - and so before the next request. Serialised by ``_LOCK``.
    Raises ConnectError: "expired" = the connection is gone for good (``notion_reconnect``
    is set through ``persist``); "offline" / "server" = try again later, nothing is lost.

    If the connection is forgotten or replaced (a disconnect, a new Connect) while the
    request is in flight, the pair that comes back is the caller's to use but is neither kept
    nor handed to ``persist``: it must not write a disconnected grant back into the settings."""
    stale = creds.access
    with _LOCK:
        with _STATE:
            gen = _GEN[0]
            gone = creds.gen is not None and creds.gen != gen
            if not gone:
                creds.adopt_newer()
        if gone:
            # Forgotten since these credentials were read (a send that was running when the user
            # disconnected): nothing to renew, nothing to flag, nothing to save.
            raise ConnectError("expired")
        if creds.access != stale and not creds.expiring():
            return creds                        # another caller refreshed a moment ago
        old = creds.refresh
        if not old or not _valid_client_id(creds.client_id):
            _save(persist, {"notion_reconnect": True})
            raise ConnectError("expired")
        fields = {"grant_type": "refresh_token", "refresh_token": old, "client_id": creds.client_id}
        try:
            tokens = _parse_tokens(_token_post(ep or discover(), fields, persist), old_refresh=old)
        except ConnectError as e:
            if e.code == "expired" and _GEN[0] == gen:
                _save(persist, {"notion_reconnect": True})
            raise
        creds.access, creds.refresh, creds.expires_at = (
            tokens["access_token"], tokens["refresh_token"], tokens["expires_at"])
        with _STATE:
            if _GEN[0] != gen:
                logger.info("Notion: the connection changed while its token was renewed; the new pair is not kept")
                return creds
            if creds.refresh != old:
                _SUPERSEDED.append(old)
                del _SUPERSEDED[:-_SUPERSEDED_KEEP]
            _LATEST.clear()
            _LATEST.update(access_token=creds.access, refresh_token=creds.refresh, expires_at=creds.expires_at)
        _save(persist, dict(creds.updates(), notion_reconnect=False))
        return creds


def revoke(access="", refresh="", client_id=""):
    """Give the app's access back at Notion (best effort, blocking, never raises). True
    when Notion was told (or there was nothing to tell), False when it could not be:
    the user can then remove Gusan in Notion's settings. The in-memory pair is forgotten
    either way."""
    token = (refresh or access or "").strip()
    forget_tokens()
    if not token:
        return True
    try:
        fields = {"token": token, "token_type_hint": "refresh_token" if refresh else "access_token"}
        if _valid_client_id(client_id):
            fields["client_id"] = client_id
        r = _request("POST", discover().revoke, headers=_form_headers(), form=fields, timeout=HTTP_TIMEOUT)
        ok = 200 <= r.status < 300
        if not ok:
            logger.info("Notion revoke: answered %s %s", r.status, _safe_code(r.json()))
        return ok
    except (ConnectError, NetworkError):
        return False
    except Exception:
        logger.debug("Notion revoke failed", exc_info=True)
        return False


# ── typed errors, the same sentences and codes as the REST path ─────────────
class McpError(NotionError):
    """A NotionError that also says why (``reason``: "gone" | "forbidden" | "validation" |
    "" ) so the sender can decide what to do without reading text."""
    reason = ""

    def __init__(self, message="", *, reason="", **kw):
        super().__init__(message, **kw)
        if reason:
            self.reason = reason


class McpNotFound(McpError, NotionNotFoundError):
    """The page (or its parent) is gone or cannot be reached."""


GONE_TEXT = f"{APP_NAME} can't reach that Notion page anymore."
FORBIDDEN_TEXT = f"Notion won't let {APP_NAME} add pages there."
RATE_TEXT = "Notion rate limit hit - wait a moment and try again."
TOO_BIG_TEXT = "Notion couldn't take that much at once."
OTHER_TEXT = "Notion couldn't take that."


def _http_error(status, retry_after=None):
    """The NotionError for a failed HTTP answer of the MCP endpoint."""
    if status == 401:
        return NotionAuthError(notion_export.AUTH_AGAIN_TEXT)
    if status == 403:
        return McpError(FORBIDDEN_TEXT, status=403, code="no_access", reason="forbidden")
    if status in (429, 529):
        return McpError(RATE_TEXT, status=status, code="rate", retry_after=retry_after)
    if status >= 500:
        return McpError(f"Notion had a problem (error {status}) - try again in a minute.", status=status)
    return McpError(f"Notion couldn't take that (error {status}).", status=status)


def _retry_seconds(reply):
    try:
        return max(0.0, float(reply.header("retry-after")))
    except (TypeError, ValueError):
        return None


_CLASSES = (
    ("rate", re.compile(r"rate_limited|rate limit|too many requests|service_overload", re.I)),
    ("auth", re.compile(r"unauthorized|invalid_token|token (?:is )?(?:expired|invalid|revoked)|not authenticated", re.I)),
    # "Your workspace has used all of its free blocks. Upgrade your plan to create more content. [Learn more]..."
    # (a free workspace with more than one member has a total limit on blocks; Notion answers every create with it)
    ("full", re.compile(r"free blocks?|blocks? limit|limit of blocks|out of blocks|upgrade (?:your )?plan|"
                        r"workspace is full", re.I)),
    ("gone", re.compile(r"object_not_found|not found|could not find|couldn'?t find|does not exist|doesn'?t exist|"
                        r"archived|in_trash|in trash|trashed|deleted", re.I)),
    ("forbidden", re.compile(r"restricted_resource|forbidden|permission|access denied|not authorized|"
                             r"don'?t have access|do not have access|no access", re.I)),
    ("validation", re.compile(r"validation_error|too large|too long|too big|exceeds|payload|invalid markdown|"
                              r"could not parse|failed to parse", re.I)),
)


def classify_text(text):
    """"rate" | "auth" | "full" | "gone" | "forbidden" | "validation" | "other": what an
    error message from a tool says, judged by fixed words only."""
    text = text if isinstance(text, str) else ""
    for kind, pattern in _CLASSES:
        if pattern.search(text):
            return kind
    return "other"


# An address is cut at its first "?" or "#", and everything after it up to the next space goes: a query can
# hold a quote, a bar or a bracket, and whatever follows one must not be left in the log. (The closing
# bracket of a Markdown link goes with it: this line is for a person reading a log, not for rendering.)
_URL_QUERY = re.compile(r"((?:https?|wss?)://[^\s?#]*)[?#]\S*", re.I)
_NAMED_SECRET = re.compile(
    r"(\bbearer\s+|\b(?:access_?token|refresh_?token|id_?token|token|code_verifier|client_secret|secret|"
    r"password|api_?key|authorization)[\"']?\s*[=:]\s*[\"']?(?:(?:bearer|basic)\s+)?)[^\s\"',;)\]}>]+", re.I)
_LONG_RUN = re.compile(r"[A-Za-z0-9_\-]{24,}")
# A JWT-shaped thing. It may only START at the beginning of a run: without the look-behind every
# position inside one long run is a new start, and a few tens of thousands of characters take
# minutes (quadratic) - on the error path of a connect or a send.
_DOTTED_RUN = re.compile(r"(?<![A-Za-z0-9_\-])(?:[A-Za-z0-9_\-]{8,}\.){2,}[A-Za-z0-9_\-]{8,}")
_HIDE_MIN = 4
_SNIPPET_RAW = 20000                # how much of a message is looked at at all (a server can send megabytes)
_SNIPPET_SCAN = 4096                # ...and how much of that is masked word by word


def _snippet(text, limit=240, hide=()):
    """The start of an error message for the log, safe to keep: one line; every address cut
    before its query or fragment (that is where codes and tokens ride); the strings in
    ``hide`` (the tokens this client holds - a server could echo one) and anything shaped
    like ``token=...`` / ``Bearer ...`` masked; long id-like runs masked. Masking comes
    BEFORE the cut to ``limit``, so a long secret is never left half-visible at the end.
    Only the start of a long message is looked at, in whole words (a cut never leaves half
    of a secret), so a huge answer cannot keep the worker busy."""
    raw = text if isinstance(text, str) else ""
    for secret in sorted((h for h in hide if isinstance(h, str) and len(h.strip()) >= _HIDE_MIN),
                         key=len, reverse=True):
        raw = raw.replace(secret.strip(), "...")        # on the whole text: a plain replace is fast at any size
    capped = len(raw) > _SNIPPET_RAW
    one = re.sub(r"\s+", " ", raw[:_SNIPPET_RAW]).strip()
    scan = max(_SNIPPET_SCAN, limit * 4)
    if capped or len(one) > scan:
        one = one[:scan].rsplit(" ", 1)[0]              # whole words only: the last one may be cut in two
    one = _URL_QUERY.sub(r"\1", one)
    one = _NAMED_SECRET.sub(r"\1...", one)
    one = _DOTTED_RUN.sub("...", one)
    one = _LONG_RUN.sub("...", one)
    return one[:limit]


def _tool_error(text):
    """The NotionError for a tool (or JSON-RPC) error message. The message itself is
    only read, never repeated: it can hold anything."""
    kind = classify_text(text)
    if kind == "rate":
        m = re.search(r"retry[_ -]?after[^0-9]{0,20}(\d+(?:\.\d+)?)", text or "", re.I)
        return McpError(RATE_TEXT, status=429, code="rate", retry_after=float(m.group(1)) if m else None)
    if kind == "auth":
        return NotionAuthError(notion_export.AUTH_AGAIN_TEXT)
    if kind == "full":
        return McpError(notion_export.FULL_TEXT, code="full", reason="full")
    if kind == "gone":
        return McpNotFound(GONE_TEXT, reason="gone")
    if kind == "forbidden":
        return McpNotFound(FORBIDDEN_TEXT, code="no_access", reason="forbidden")
    if kind == "validation":
        return McpError(TOO_BIG_TEXT, reason="validation")
    return McpError(OTHER_TEXT)


def _as_notion_error(exc):
    """A ConnectError from the token side, as the NotionError the sender speaks."""
    if exc.code == "expired":
        return NotionAuthError(notion_export.AUTH_AGAIN_TEXT)
    if exc.code == "offline":
        return NotionError(notion_export.OFFLINE_TEXT, code="offline")
    if exc.code == "server":
        return McpError("Notion had a problem - try again in a minute.")
    return McpError(OTHER_TEXT)


# ── the MCP client ───────────────────────────────────────────────────────────
class ToolResult:
    """What a tool answered: ``text`` (its text content joined), ``data`` (the parsed JSON
    object - from structuredContent, else from the text - or None) and ``raw``."""

    def __init__(self, text="", data=None, raw=None):
        self.text = text
        self.data = data
        self.raw = raw if isinstance(raw, dict) else {}


def _parse_json_text(text):
    text = (text or "").strip()
    if text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except ValueError:
            return None
    return None


class McpClient:
    """MCP over Streamable HTTP against the Notion endpoint, for one worker's use.

    ``creds`` is a :class:`Credentials` (swapped in place by a refresh); ``persist`` gets
    the config keys of a refresh before the next request; ``ep`` the :class:`Endpoints`
    (discovered on first use). :meth:`call_tool` initialises the session once, then calls
    the tool. A 401 is answered with ONE refresh and one retry; a 429/529 with a short
    Retry-After with one retry; everything else is a :class:`NotionError`."""

    def __init__(self, creds, persist=None, ep=None):
        self.creds = creds
        self._persist = persist
        self._ep = ep
        self._session = ""
        self._protocol = PROTOCOL_VERSION
        self._ready = False
        self._id = 0

    def endpoints(self):
        if self._ep is None:
            try:
                self._ep = discover()
            except ConnectError as e:
                raise _as_notion_error(e) from e
        return self._ep

    # tokens
    def _refresh(self, force=False):
        """Renew the token; a refresh that fails for a temporary reason while the old
        token still has time left is not an error."""
        try:
            renew(self.creds, self._persist, self.endpoints())
        except ConnectError as e:
            if not force and e.code in ("offline", "server") and not self.creds.expired():
                logger.info("Notion: renewing the token failed (%s), using the current one", e.code)
                return
            raise _as_notion_error(e) from e

    # one HTTP round trip
    def _headers(self):
        h = {"Authorization": "Bearer " + self.creds.access, "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream", "User-Agent": USER_AGENT}
        if self._ready or self._session:
            h["MCP-Protocol-Version"] = self._protocol
        if self._session:
            h["Mcp-Session-Id"] = self._session
        return h

    def _post(self, payload):
        refreshed = waited = False
        url = self.endpoints().mcp
        while True:
            if self.creds.expiring():
                self._refresh()
            try:
                reply = _request("POST", url, headers=self._headers(), json_body=payload, timeout=TOOL_TIMEOUT)
            except NetworkError as e:
                raise NotionError(notion_export.OFFLINE_TEXT, code="offline") from e
            if reply.status == 401:
                reply.close()
                if refreshed:
                    raise NotionAuthError(notion_export.AUTH_AGAIN_TEXT)
                refreshed = True
                self._refresh(force=True)
                continue
            if reply.status in (429, 529):
                wait = _retry_seconds(reply)
                reply.close()
                if not waited and wait is not None and wait <= RETRY_AFTER_MAX:
                    waited = True
                    _sleep(wait)
                    continue
                raise _http_error(reply.status, wait)
            if not 200 <= reply.status < 300:
                reply.close()
                logger.info("Notion MCP: answered %s", reply.status)
                raise _http_error(reply.status)
            sid = reply.header("mcp-session-id")
            if sid and re.fullmatch(r"[\x21-\x7e]{1,200}", sid):
                self._session = sid
            return reply

    def _rpc(self, method, params=None):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        reply = self._post(payload)
        try:
            msg = self._message(reply, self._id)
        finally:
            reply.close()
        if msg.get("error") is not None:
            err = msg["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            if isinstance(err, dict) and err.get("code") in (-32700, -32600, -32601):
                logger.info("Notion MCP: %s was refused as a bad request", method)
                raise McpError(OTHER_TEXT)              # the protocol's own "no such method": not a missing page
            logger.info("Notion MCP: %s answered an error (%s): %s", method, classify_text(text),
                        _snippet(text, hide=(self.creds.access, self.creds.refresh)))
            raise _tool_error(text)
        return msg.get("result")

    def _notify(self, method):
        reply = self._post({"jsonrpc": "2.0", "method": method})
        reply.close()

    @staticmethod
    def _message(reply, rid):
        """The JSON-RPC answer with id ``rid`` out of a JSON body or an event stream."""
        def pick(obj):
            items = obj if isinstance(obj, list) else [obj]
            for item in items:
                if isinstance(item, dict) and item.get("id") == rid and ("result" in item or "error" in item):
                    return item
            return None

        ctype = reply.header("content-type").lower()
        if reply.is_stream or ctype.startswith("text/event-stream"):
            data, size = [], 0
            deadline = time.monotonic() + STREAM_SECONDS
            try:
                for line in _events(reply):
                    if time.monotonic() > deadline or size > MAX_BODY:
                        # an event that never ends, or a stream that never answers: stop here
                        logger.info("Notion MCP: the event stream went on too long or too large")
                        raise McpError(OTHER_TEXT)
                    if line is None:                        # a blank line ends one event
                        found = pick(_load(data))
                        data, size = [], 0
                        if found is not None:
                            return found
                    else:
                        size += len(line)
                        data.append(line)
                found = pick(_load(data))                   # a stream that ended without a blank line
                if found is not None:
                    return found
            except NetworkError as e:
                raise NotionError(notion_export.OFFLINE_TEXT, code="offline") from e
        else:
            found = pick(reply.json())
            if found is not None:
                return found
        logger.info("Notion MCP: no answer with the expected id")
        raise McpError(OTHER_TEXT)

    # the session
    def _ensure_ready(self):
        if self._ready:
            return
        result = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": {"name": CLIENT_NAME, "version": APP_VERSION}})
        if isinstance(result, dict) and isinstance(result.get("protocolVersion"), str) \
                and re.fullmatch(r"[0-9A-Za-z.-]{1,32}", result["protocolVersion"]):
            self._protocol = result["protocolVersion"]
        self._ready = True
        try:
            self._notify("notifications/initialized")
        except McpError as e:
            if e.code != "other":                       # auth, rate and a missing page still end the call
                raise
            logger.info("Notion MCP: the initialized notification was refused (%s), going on", e.status)

    def call_tool(self, name, arguments):
        """Call one Notion tool; a :class:`ToolResult`. Raises NotionError."""
        self._ensure_ready()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise McpError(OTHER_TEXT)
        parts = [c.get("text") for c in (result.get("content") or [])
                 if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)]
        text = "\n".join(parts)
        if result.get("isError"):
            logger.info("Notion MCP: tool %s answered an error (%s): %s", name, classify_text(text),
                        _snippet(text, hide=(self.creds.access, self.creds.refresh)))
            raise _tool_error(text)
        structured = result.get("structuredContent")
        data = structured if isinstance(structured, (dict, list)) else _parse_json_text(text)
        return ToolResult(text, data, result)


def _load(data_lines):
    """The JSON of one event's ``data:`` lines (None when there is none or it is not JSON)."""
    if not data_lines:
        return None
    try:
        return json.loads("\n".join(data_lines))
    except ValueError:
        return None


def _events(reply):
    """Server-sent event lines of ``reply``: each ``data:`` payload line, and None at the
    end of every event."""
    for line in reply.iter_lines():
        if line == "":
            yield None
        elif line.startswith("data:"):
            yield line[5:].lstrip(" ") if line[5:6] == " " else line[5:]
    yield None


# ── Notion-flavoured Markdown (the "enhanced Markdown" the page tools read) ──
# Documented syntax only (developers.notion.com/guides/data-apis/enhanced-markdown):
# # / ## / ### / #### headings, - bullets, 1. numbers, - [ ] / - [x] to-dos, > quotes,
# ``` fences, --- dividers, tab-indented children, **bold** *italic* ~~strike~~ `code`
# [text](url), and a backslash before  \ * ~ ` $ [ ] < > { } | ^  in ordinary text.
_ESCAPE = re.compile(r"([\\*~`$\[\]<>{}|^])")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LINK = re.compile(r"\[([^\]\n]{1,300})\]\((https?://[^\s()<>]{1,1000})\)")
_BLOCKISH = re.compile(r"^(#{1,6}(\s|$)|[-+]\s|\d{1,9}[.)](\s|$)|[-_=]{3,}\s*$|:{3})")
ZERO_WIDTH = "\u200b"


def escape_text(text):
    """Plain text made safe to place in Markdown (nothing in it can turn into formatting)."""
    return _ESCAPE.sub(r"\\\1", _CTRL.sub("", text if isinstance(text, str) else ""))


def _guard(line):
    """A paragraph that would read as a heading, list or divider gets an invisible
    character in front (the escape list has no way to say "a plain minus")."""
    return ZERO_WIDTH + line if _BLOCKISH.match(line) else line


def inline_markdown(text, _depth=0):
    """The notes' inline formatting (**bold**, *italic*, _italic_, ~~strike~~, `code`,
    [text](https link)) in Notion's syntax; every other character is escaped."""
    text = _CTRL.sub("", text if isinstance(text, str) else "")
    out, plain, i, n = [], [], 0, len(text)
    dead = set()            # markers with no closer anywhere after them: later ones have none either (no re-scan)

    def flush():
        if plain:
            out.append(escape_text("".join(plain)))
            del plain[:]

    while i < n:
        c = text[i]
        if c == "`":
            j = text.find("`", i + 1)
            if j > i + 1:
                flush()
                out.append(text[i:j + 1])
                i = j + 1
                continue
        elif c == "*" and text.startswith("**", i) and _depth < 3 and "**" not in dead:
            j = text.find("**", i + 2)
            if j > i + 2 and not text[i + 2].isspace() and not text[j - 1].isspace():
                flush()
                out.append("**" + inline_markdown(text[i + 2:j], _depth + 1) + "**")
                i = j + 2
                continue
            if j < 0:
                dead.add("**")
        elif c == "~" and text.startswith("~~", i) and _depth < 3 and "~~" not in dead:
            j = text.find("~~", i + 2)
            if j > i + 2 and not text[i + 2].isspace() and not text[j - 1].isspace():
                flush()
                out.append("~~" + inline_markdown(text[i + 2:j], _depth + 1) + "~~")
                i = j + 2
                continue
            if j < 0:
                dead.add("~~")
        elif c == "*" and i + 1 < n and text[i + 1] not in "* \t" and _depth < 3 and "*" not in dead:
            j = i + 1
            while True:
                j = text.find("*", j)
                if j < 0 or (not text[j - 1].isspace() and text[j - 1] != "*" and text[j + 1:j + 2] != "*"):
                    break
                j += 1
            if j > i + 1:
                flush()
                out.append("*" + inline_markdown(text[i + 1:j], _depth + 1) + "*")
                i = j + 1
                continue
            dead.add("*")
        elif c == "_" and (i == 0 or not text[i - 1].isalnum()) and i + 1 < n \
                and text[i + 1] not in "_ \t" and _depth < 3 and "_" not in dead:
            j = i + 1
            while True:
                j = text.find("_", j)
                if j < 0 or (not text[j - 1].isspace() and (j + 1 == n or not text[j + 1].isalnum())):
                    break
                j += 1
            if j > i + 1:
                flush()
                out.append("*" + inline_markdown(text[i + 1:j], _depth + 1) + "*")
                i = j + 1
                continue
            dead.add("_")
        elif c == "[":
            m = _LINK.match(text, i)
            if m:
                flush()
                out.append("[" + inline_markdown(m.group(1), _depth + 1) + "](" + m.group(2) + ")")
                i = m.end()
                continue
        plain.append(c)
        i += 1
    flush()
    return "".join(out)


def _split_long(text, limit=MAX_LINE_CHARS):
    """A very long line as several, cut at spaces."""
    text = text.strip()
    parts = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    parts.append(text)
    return [p for p in parts if p]


_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")      # no lazy group: a long run of spaces must not backtrack
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_TODO = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBER = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def markdown_units(md):
    """The notes' Markdown as Notion-flavoured Markdown blocks: a list of (kind, text)
    where kind is "h" | "p" | "li" | "quote" | "hr" | "code". A top-level list item and
    its nested items are one unit (so a split between calls never separates them)."""
    units, lines, i = [], (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"), 0
    stack = []                                   # indents of the open list levels
    while i < len(lines):
        raw = lines[i]
        i += 1
        raw = raw.expandtabs(4)
        stripped = raw.strip()
        if not stripped:
            continue
        fence = _FENCE.match(raw)
        if fence:
            body = []
            while i < len(lines) and not lines[i].strip().startswith(fence.group(1)[:3]):
                body.append(_CTRL.sub("", lines[i]))
                i += 1
            i += 1                               # the closing fence (or the end)
            units.append(("code", "```%s\n%s\n```" % (fence.group(2), "\n".join(body))))
            stack = []
            continue
        m = _HEADING.match(raw)
        if m:
            title = m.group(2).rstrip()
            bare = title.rstrip("#")
            if bare and bare != title and bare[-1].isspace():
                title = bare.rstrip()                    # "## Title ##": the closing hashes are not part of it
            units.append(("h", "#" * min(len(m.group(1)), 4) + " " + (inline_markdown(title) or " ")))
            stack = []
            continue
        if _RULE.match(raw):
            units.append(("hr", "---"))
            stack = []
            continue
        item = None
        m = _TODO.match(raw)
        if m:
            item = (len(m.group(1)), "- [%s] " % ("x" if m.group(2) in "xX" else " "), m.group(3))
        else:
            m = _BULLET.match(raw)
            if m:
                item = (len(m.group(1)), "- ", m.group(2))
            else:
                m = _NUMBER.match(raw)
                if m:
                    item = (len(m.group(1)), m.group(2) + ". ", m.group(3))
        if item:
            indent, marker, body = item
            while stack and indent < stack[-1]:
                stack.pop()
            if not stack or indent > stack[-1]:
                if stack and units and units[-1][0] == "li":
                    stack.append(indent)
                else:
                    stack = [indent]             # first item, or a "child" with no parent: a top-level item
            depth = len(stack) - 1
            line = "\t" * depth + marker + inline_markdown(body.strip())
            if depth and units and units[-1][0] == "li":
                units[-1] = ("li", units[-1][1] + "\n" + line)
            else:
                units.append(("li", line))
            continue
        stack = []
        m = _QUOTE.match(raw)
        if m:
            units.append(("quote", "> " + (inline_markdown(m.group(1)) or " ")))
            continue
        if _TABLE_ROW.match(raw):
            # A table is shown as plain lines (cells joined by " · "): its grid is not worth a risk
            # of Notion refusing the whole page; nothing is dropped.
            if _TABLE_RULE.match(raw):
                continue
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", stripped.strip("|"))]
            line = " · ".join(c for c in cells if c)
            if line:
                units.append(("p", _guard(inline_markdown(line))))
            continue
        for part in _split_long(stripped):
            units.append(("p", _guard(inline_markdown(part))))
    return units


def transcript_units(transcript):
    """"## Transcript" and one paragraph per spoken line, exactly as written (no
    formatting is read out of what people said)."""
    if not (transcript or "").strip():
        return []
    units = [("h", "## Transcript")]
    for line in transcript.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        for part in _split_long(line):
            units.append(("p", _guard(escape_text(part))))
    return units


def page_units(notes_md, transcript="", meta_line=""):
    """Everything on a meeting page, in order: the grey line (when · duration · people),
    the notes, the transcript."""
    units = []
    if (meta_line or "").strip():
        units.append(("p", _guard(escape_text(" ".join(meta_line.split()))) + ' {color="gray"}'))
    units.extend(markdown_units(notes_md))
    units.extend(transcript_units(transcript))
    return units


def render(units):
    """Blocks joined for a call: list items touch each other, everything else is
    separated by a blank line."""
    out, prev = [], None
    for kind, text in units:
        if out:
            out.append("\n" if (kind == "li" and prev == "li") else "\n\n")
        out.append(text)
        prev = kind
    return "".join(out)


def chunk_units(units, limit=CHUNK_CHARS):
    """The units in groups of at most ``limit`` characters (a unit is never cut; a lone
    unit longer than the limit is a group of its own)."""
    chunks, current, size = [], [], 0
    for unit in units:
        cost = len(unit[1]) + 2
        if current and size + cost > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(unit)
        size += cost
    if current:
        chunks.append(current)
    return chunks


def clean_title(title):
    """One line, no control characters, at most MAX_TITLE_LEN characters."""
    one = " ".join(_CTRL.sub(" ", title if isinstance(title, str) else "").split())
    return one[:MAX_TITLE_LEN].rstrip() or DEFAULT_TITLE


# ── reading what a tool answered ─────────────────────────────────────────────
_NOTION_URL = re.compile(r"https://(?:[A-Za-z0-9-]+\.)*notion\.(?:so|site)/[^\s\"'<>)\]\\,]+")


def safe_page_url(url):
    """The url when it is an https address on notion.so / notion.site, else ""."""
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > 2048 or re.search(r"\s", url):
        return ""
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower()
    except ValueError:
        return ""
    if p.username or p.password:
        return ""
    if host in ("notion.so", "notion.site") or host.endswith((".notion.so", ".notion.site")):
        return url
    return ""


def _dicts(obj, _depth=0):
    if _depth > 8:
        return
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _dicts(value, _depth + 1)
    elif isinstance(obj, list):
        for value in obj[:50]:
            yield from _dicts(value, _depth + 1)


def find_page(result):
    """{"id" (dashed), "url"} of the page a create/update tool answered, from its data or,
    failing that, from the first Notion address in its text. Empty strings when neither
    is there. Only https notion.so / notion.site addresses are returned."""
    page_id, url = "", ""
    for d in _dicts(getattr(result, "data", None)):
        u = safe_page_url(d.get("url"))
        i = notion_export.extract_page_id(d.get("id")) if isinstance(d.get("id"), str) else None
        if u or i:
            url, page_id = u, i or notion_export.extract_page_id(u) or ""
            break
    if not (url or page_id):
        m = _NOTION_URL.search(getattr(result, "text", "") or "")
        if m:
            url = safe_page_url(m.group(0))
            page_id = notion_export.extract_page_id(url) or ""
    if page_id and not url:
        url = notion_export.page_url(page_id)
    return {"id": page_id or "", "url": url or ""}


def _settle(client, result):
    """A tool that answered "queued" (allow_async is False, so this is rare): wait for
    the task, return the result that has the page."""
    data = result.data if isinstance(result.data, dict) else {}
    task = data.get("async_task") if isinstance(data.get("async_task"), dict) else None
    task_id = task.get("task_id") if task else None
    if not task_id:
        return result
    for _ in range(ASYNC_POLLS):
        _sleep(ASYNC_WAIT)
        polled = client.call_tool("notion-get-async-task", {"task_id": task_id})
        pdata = polled.data if isinstance(polled.data, dict) else {}
        status = str(pdata.get("status") or (pdata.get("async_task") or {}).get("status") or "").lower()
        if status in ("succeeded", "completed", "complete", "done"):
            return polled if find_page(polled)["id"] else result
        if status in ("failed", "error", "canceled", "cancelled"):
            raise McpError(OTHER_TEXT)
    raise McpError(OTHER_TEXT)


# ── the "Gusan meetings" page, and one page per meeting ─────────────────────
_HUB_LOCK = threading.RLock()       # one hub is made at a time (held across the network call)
_HUBS = {}                          # the id of a page that is gone ("" = there was none) -> the page made instead
#                                     (guarded by _STATE, like the token chain)
_HUBS_KEEP = 8


def _create_args(title, content, icon, parent_id):
    page = {"properties": {"title": title}, "content": content}
    if icon:
        page["icon"] = icon
    args = {"pages": [page], "allow_async": False}
    if parent_id:
        args["parent"] = {"type": "page_id", "page_id": parent_id}
    else:
        args["creation_mode"] = "draft"        # a private page at the top of the workspace: no parent to share
    return args


def _create_page(client, parent_id, title, icon, content):
    """Make one page (the tool "notion-create-pages"); {"id", "url"}."""
    result = client.call_tool("notion-create-pages", _create_args(title, content, icon, parent_id))
    page = find_page(_settle(client, result) if isinstance(result.data, dict) and "async_task" in result.data
                     else result)
    if not page["id"]:
        logger.info("Notion MCP: the create-pages answer had no page id")
        raise McpError("Notion didn't confirm the page.")
    return page


def _append(client, page_id, units):
    """Add blocks to the end of a page (the tool "notion-update-page"). A "too big"
    answer splits the group in two."""
    try:
        client.call_tool("notion-update-page", {
            "page_id": page_id, "command": "insert_content", "content": render(units),
            "position": {"type": "end"}, "allow_async": False})
    except McpError as e:
        if e.reason == "validation" and len(units) > 1:
            half = len(units) // 2
            _append(client, page_id, units[:half])
            _append(client, page_id, units[half:])
            return
        raise


_TRASHED = re.compile(r"(?:in_trash|archived|trashed)[\"']?\s*[:=]\s*[\"']?true|"
                      r"\b(?:in the trash|moved to (?:the )?trash|is in trash)\b", re.I)
_TRASH_FIELDS = ("in_trash", "archived", "is_archived", "trashed")
_CONTENT_START = re.compile(r"<content\b", re.I)


def _trashed(result):
    """Does a "notion-fetch" answer say the page ITSELF is in the trash? A yes/no field in
    the answer's data decides when there is one. Else only the page's head is read (its
    tag and properties) - never its <content>: that lists the pages under it by title, and
    a meeting called "Sorting what is in the trash" must not make the hub look trashed."""
    data = result.data if isinstance(result.data, dict) else {}
    flags = [data[k] for k in _TRASH_FIELDS if isinstance(data.get(k), bool)]
    if flags:
        return any(flags)
    text = result.text or ""
    body = _CONTENT_START.search(text)
    return bool(_TRASHED.search(text[:body.start()] if body else text))


def hub_alive(client, page_id):
    """Is the page still there (and not in the trash)? True / False, or None when that
    could not be found out. One call (the tool "notion-fetch"). Only Notion saying so
    makes it False - the page is not found (or not ours to reach), or the answer shows
    it in the trash; any other failure (a 5xx, an error we cannot read, token refused,
    offline, busy) is None: a passing outage must never make a second "Gusan meetings"
    page that every later meeting then goes to."""
    try:
        result = client.call_tool("notion-fetch", {"id": page_id})
    except McpNotFound:
        return False
    except NotionError:
        return None
    return not _trashed(result)


def ensure_hub(client, saved="", persist=None, gone=False):
    """The private "Gusan meetings" page the notes land under: {"id", "title", "url",
    "created"}. ``saved`` is the id the config has: a good one is returned as it is, no
    call (the page is made ONCE). With none - or with ``gone=True``, when Notion said that
    page no longer exists - a new one is made with creation_mode "draft" (private, at the
    top of the workspace, nothing to share first) and ``persist`` gets
    notion_hub_page / notion_parent_page / notion_parent_title. Two sends that both find
    the page gone make one new page, not two."""
    sid = notion_export.extract_page_id(saved) or ""
    with _HUB_LOCK:
        if sid and not gone:
            return {"id": sid, "title": HUB_TITLE, "url": notion_export.page_url(sid), "created": False}
        with _STATE:
            memo = _HUBS.get(sid)
        if memo and memo["id"] != sid:
            return dict(memo, created=False)
        page = _create_page(client, "", HUB_TITLE, HUB_ICON, escape_text(HUB_INTRO))
        hub = {"id": page["id"], "title": HUB_TITLE, "url": page["url"] or notion_export.page_url(page["id"])}
        with _STATE:
            _HUBS[sid] = hub
            while len(_HUBS) > _HUBS_KEEP:
                _HUBS.pop(next(iter(_HUBS)))
        creds = getattr(client, "creds", None)
        if creds is None or creds.current():        # not into the settings of a connection that was just given up
            _save(persist, {"notion_hub_page": hub["id"], "notion_parent_page": hub["id"],
                            "notion_parent_title": HUB_TITLE})
    return dict(hub, created=True)


def _publish(client, parent_id, title, icon, units):
    """Make a page with the first group of blocks and append the rest. A first call
    Notion calls too big creates nothing, so it is tried again with smaller groups; a
    failure after the page exists carries its link (``.url``)."""
    size, shrinks = CHUNK_CHARS, 0
    while True:
        chunks = chunk_units(units, size)
        try:
            page = _create_page(client, parent_id, title, icon, render(chunks[0]))
            break
        except McpError as e:
            if e.reason != "validation" or shrinks >= MAX_SHRINKS or size <= MIN_CHUNK_CHARS:
                raise
            size, shrinks = max(MIN_CHUNK_CHARS, size // 2), shrinks + 1
    for chunk in chunks[1:]:
        try:
            _append(client, page["id"], chunk)
        except NotionError as e:
            # A plain NotionError on purpose (not NotionAuthError): the page exists, so a caller
            # that refreshes the token and sends again must not make a second page.
            raise NotionError("The page was created but part of it didn't upload.", status=e.status,
                              code=e.code, retry_after=e.retry_after, url=page["url"]) from e
    return page


def send_meeting_mcp(creds, title, notes_md, transcript="", meta_line="", *,
                     parent="", hub="", persist=None, client=None):
    """Put one meeting in Notion: a page named ``title`` under the destination, with the
    grey ``meta_line``, the notes and (when given) the transcript. Returns {"url",
    "page_id", "hub_id"} (``hub_id`` "" when the notes went to a page the user picked).

    The destination is ``parent`` (the config's notion_parent_page), else ``hub`` (the
    page Gusan made); with neither, the hub is made now. When Notion says the hub is gone
    it is made again ONCE and the send is retried; a page the user picked is never
    swapped. ``persist`` receives refreshed tokens and a new hub. Raises NotionError
    (code auth | no_access | rate | offline | other; ``.url`` set when the page exists and
    only its tail failed)."""
    units = page_units(notes_md, transcript, meta_line)
    if not units:
        raise NotionError("Nothing to send yet - generate the notes first.")
    client = client or McpClient(creds, persist)
    title = clean_title(title)
    hub_id = notion_export.extract_page_id(hub) or ""
    dest = notion_export.extract_page_id(parent) or hub_id
    if not dest:
        hub_id = dest = ensure_hub(client, "", persist)["id"]
    recreated = False
    while True:
        try:
            page = _publish(client, dest, title, MEETING_ICON, units)
            break
        except McpNotFound as e:
            if recreated or e.reason != "gone" or not hub_id or dest != hub_id:
                raise                                   # a page the user picked: say so, never swap it
            recreated = True
            hub_id = dest = ensure_hub(client, dest, persist, gone=True)["id"]
        except McpError as e:
            # An answer that does not say "gone" in words we know: ask Notion whether the hub is
            # still there before giving up, so a trashed hub cannot make every send fail. Only a
            # clear "not there" makes a new one; a check that fails too (an outage) reports the error.
            if recreated or e.reason or e.code != "other" or not hub_id or dest != hub_id \
                    or hub_alive(client, dest) is not False:
                raise
            recreated = True
            hub_id = dest = ensure_hub(client, dest, persist, gone=True)["id"]
    return {"url": page["url"], "page_id": page["id"], "hub_id": hub_id if dest == hub_id else ""}


def workspace_name(client):
    """The workspace's name from the tool "notion-get-self" ("" when it cannot be read)."""
    try:
        data = client.call_tool("notion-get-self", {}).data
    except NotionError:
        return ""
    for d in _dicts(data):
        ws = d.get("workspace")
        if isinstance(ws, dict) and isinstance(ws.get("name"), str):
            return notion_connect.clean_name(ws["name"])
    return ""


# ── connecting ───────────────────────────────────────────────────────────────
def _cancelled(cancel):
    return cancel is not None and cancel.is_set()


def _bind_listener(preferred_port, state=""):
    """The one-shot listener on 127.0.0.1 (never on another address): the registered port when
    free, else any. ``state`` is what its callback must carry; with none yet (the state of the
    site's return carries the port, which is known only now) set ``server.expected_state``
    before the browser opens - until then every request is refused. The listener keeps a
    browser request open until :func:`_finish_tab` (see notion_connect._Callback)."""
    ports = (preferred_port, 0) if preferred_port else (0,)
    server = None
    for port in ports:
        try:
            server = notion_connect._LoopbackServer(("127.0.0.1", port), notion_connect._Callback)
            break
        except OSError as e:
            logger.info("Notion connect: port %s unavailable (%s)", port or "any", type(e).__name__)
    if server is None:
        raise ConnectError("port_busy")
    server.timeout = notion_connect.POLL_SECONDS
    server.result = None
    server.done = threading.Event()
    server.lock = threading.Lock()
    server.expected_state = state
    server.callback_path = CALLBACK_PATH
    server.hold_tab = True                  # the tab hears how it went (see _finish_tab)
    server.finished = threading.Event()
    server.tab_kind = ""
    return server


def _finish_tab(server, kind):
    """The connect is over: let the browser request the listener kept open answer. ``kind`` is
    notion_connect's landing "connected" or "failed" (a cancel, a refusal, a bug: "failed")."""
    server.tab_kind = kind
    server.finished.set()


def return_for(port, site):
    """(redirect_uri, state) for a listener on ``port``. With ``site`` the browser comes back to
    :data:`RETURN_URL`, which cannot know our port: it rides in the state as ``"<port>.<random>"``
    (the site page checks that shape, and forwards to that port on 127.0.0.1 only). Without it
    the browser comes back to the listener itself and the state is just random. The random part is
    always 32 url-safe characters (192 bits). The site's page only forwards a port of 1024-65535
    (4 or 5 digits), so for any other the loopback return is used."""
    token = secrets.token_urlsafe(24)
    if site and 1024 <= port <= 65535:
        return RETURN_URL, "%d.%s" % (port, token)
    return "http://127.0.0.1:%d%s" % (port, CALLBACK_PATH), token


def authorize_refused(url):
    """True when Notion's authorize address itself answers 400 or 401 (asked here, without a
    browser and without following anything): it does not know this client id - the
    registration is gone, which Notion may do to a client nobody used for a long time - or
    does not take this redirect. A browser would show that as an error page, never as an
    answer to the listener, and the connect would wait out its whole timeout, every time.
    Anything else (the redirect into Notion's consent page, a challenge, a busy server, no
    internet) is "not refused": the browser step goes on and finds out for itself."""
    try:
        r = _request("GET", url, headers={"Accept": "text/html,application/json", "User-Agent": USER_AGENT})
    except NetworkError:
        return False
    r.close()
    return r.status in (400, 401)


def connection_settings(res):
    """The config keys of a finished connection ``res`` (what :func:`connect` returns),
    for ONE set_settings call. The client id (and the return addresses it knows) is already
    saved; it is repeated here so a save that failed earlier is made good."""
    dest = res["dest"]
    out = {
        "notion_api_key": res["token"], "notion_refresh_token": res.get("refresh") or "",
        "notion_auth_mode": "mcp", "notion_workspace_name": res.get("workspace") or "",
        "notion_parent_page": dest["id"], "notion_parent_title": dest.get("title") or HUB_TITLE,
        "notion_hub_page": dest["id"], "notion_reconnect": False,
        "notion_mcp_client_id": res.get("client_id") or "",
        "notion_token_expires": res.get("expires_at") or 0,
    }
    if res.get("redirects"):
        out["notion_mcp_redirects"] = res["redirects"]
    return out


def _reusable_hub(cfg, workspace, client):
    """Connecting again keeps the "Gusan meetings" page the notes were going to - but only
    for the same workspace (the consent page lets the user pick another one) and not when
    Notion says it is gone or in the trash (a trashed page would make every send fail). A
    page that could not be checked just now is kept: a hiccup must not make a second one,
    and a send that finds it gone makes it again (send_meeting_mcp)."""
    saved = notion_export.extract_page_id(cfg.get("notion_hub_page"))
    old = (cfg.get("notion_workspace_name") or "").strip()
    if not (saved and old and old == workspace):
        return ""
    return saved if hub_alive(client, saved) is not False else ""


def connect(cfg=None, open_browser=None, timeout=LOGIN_TIMEOUT, cancel=None, on_url=None, on_code=None,
            persist=None, preferred_port=CALLBACK_PORT):
    """Connect Notion in the browser; blocking, run it on a worker.

    Opens Notion's consent page, waits for the answer on a one-shot listener at
    127.0.0.1, swaps the code for tokens and makes (or keeps) the private "Gusan
    meetings" page. Returns::

        {"mode": "mcp", "token", "refresh", "workspace", "expires_at", "client_id",
         "redirects", "dest": {"id", "title", "kind": "hub", "url"}, "pages": None,
         "settings": {...}}          # connection_settings(res): save it in ONE call

    The browser comes back to our site's page (:data:`RETURN_URL`) when that page is up
    (:func:`return_page_ready`), else straight to the listener; see the module docstring.
    The browser tab stays open until this returns or raises, and then says how it went.

    ``on_url(url)`` is called once the listener is up, before the browser is asked to open
    (keep it for "Open Notion again"); ``on_code()`` when the answer arrived and the
    "Setting things up" part starts; ``persist(dict)`` gets the client id (and the return
    addresses it knows) the moment it is registered. Raises ConnectError (code: timeout |
    denied | offline | port_busy | expired | server | browser | full | other) or
    ConnectCancelled once ``cancel`` (a threading.Event) is set; a grant that was made is
    given back first. "full": connected, but the workspace has no room for the first page."""
    cfg = cfg or {}
    if open_browser is None:
        import webbrowser
        open_browser = webbrowser.open
    if _cancelled(cancel):
        raise ConnectCancelled()
    ep = discover()
    site = return_page_ready()                      # one question, 4 s at most: is our return page up?
    if _cancelled(cancel):
        raise ConnectCancelled()
    client_id, redirects = client_for(cfg, ep, persist, site)
    if _cancelled(cancel):
        raise ConnectCancelled()
    verifier, challenge = make_pkce()
    server = _bind_listener(preferred_port)
    try:
        return _connect_on(server, cfg, ep, client_id, redirects, site, verifier, challenge, open_browser,
                           timeout, cancel, on_url, on_code, persist)
    except BaseException:
        _finish_tab(server, "failed")               # a browser tab that was kept waiting hears it did not work
        raise


def _connect_on(server, cfg, ep, client_id, redirects, site, verifier, challenge, open_browser, timeout,
                cancel, on_url, on_code, persist):
    """The part of :func:`connect` that has a listener: ask, wait, swap the code, make the
    first page. The browser tab is ended with "connected" when it returns."""
    try:
        port = server.server_address[1]
        redirect, state = return_for(port, site)
        site = redirect == RETURN_URL
        server.expected_state = state
        url = authorize_url(ep, client_id, redirect, state, challenge)
        if authorize_refused(url):
            # The saved client id (or redirect) is not accepted any more: register once more and
            # ask again (grants made under the old id are dead with it, and this connect replaces
            # them). Once only: if the new one is refused too, the browser is tried anyway, so a
            # server that answers a program differently from a browser can not lock anyone out.
            logger.info("Notion connect: the saved client was refused, registering again")
            forget_client(persist)
            client_id, redirects = client_for({}, ep, persist, site)
            url = authorize_url(ep, client_id, redirect, state, challenge)
            refused = authorize_refused(url)
            if refused and site:
                # Notion will not take our site's address: the loopback one (the client, registered
                # just now, knows it too) - the way connecting always worked.
                logger.info("Notion connect: the site's return address was refused, using the loopback one")
                site = False
                redirect, state = return_for(port, False)
                server.expected_state = state
                url = authorize_url(ep, client_id, redirect, state, challenge)
                refused = authorize_refused(url)
            if refused:
                logger.info("Notion connect: the new client was refused too, going on to the browser")
        if _cancelled(cancel):
            raise ConnectCancelled()
        notion_connect._notify(on_url, url)
        try:
            opened = open_browser(url)
        except Exception:
            opened = False
        if opened is False:                         # webbrowser.open: no browser could start
            raise ConnectError("browser")
        deadline = time.monotonic() + timeout
        while not server.done.is_set() and time.monotonic() < deadline and not _cancelled(cancel):
            server.handle_request()
            server.done.wait(0.05)
    finally:
        server.server_close()
    if _cancelled(cancel):
        raise ConnectCancelled()
    res = server.result
    if res is None:
        raise ConnectError("timeout")
    if res.get("error"):
        if res["error"] in ("invalid_client", "unauthorized_client"):
            forget_client(persist)
            raise ConnectError("expired")
        raise ConnectError("denied" if res["error"] == "access_denied" else "other")
    notion_connect._notify(on_code)
    tokens = exchange_code(ep, client_id, res["code"], redirect, verifier, persist)
    creds = Credentials(tokens["access_token"], tokens["refresh_token"], tokens["expires_at"], client_id)

    def give_back():
        revoke(creds.access, creds.refresh, client_id)

    if _cancelled(cancel):
        give_back()
        raise ConnectCancelled()
    creds.gen = _adopt_chain(tokens)
    forget_hubs()
    try:
        client = McpClient(creds, persist, ep)
        name = tokens["workspace_name"] or workspace_name(client)
        hub = ensure_hub(client, _reusable_hub(cfg, name, client))
    except NotionError as e:                        # connected, but no way to write: not a connection
        logger.info("Notion connect: the first page could not be made (%s)", e.code)
        give_back()
        # "full": the workspace has no room for a page (the grant is given back all the same: a connection
        # that can not write is not one, and the user may well pick another workspace next time)
        raise ConnectError({"offline": "offline", "full": "full"}.get(e.code, "other")) from e
    except Exception:                               # a bug: the grant must not outlive the failed connect
        give_back()
        raise
    if _cancelled(cancel):
        give_back()
        raise ConnectCancelled()
    out = {"mode": "mcp", "token": creds.access, "refresh": creds.refresh, "workspace": name,
           "expires_at": creds.expires_at, "client_id": client_id, "redirects": redirects,
           "dest": {"id": hub["id"], "title": HUB_TITLE, "kind": "hub", "url": hub["url"]}, "pages": None}
    out["settings"] = connection_settings(out)
    _finish_tab(server, "connected")
    return out


def forget_hubs():
    with _STATE:
        _HUBS.clear()
