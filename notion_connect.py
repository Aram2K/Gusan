"""Connect Gusan to Notion with one button, and list where notes can go.

The whole flow, in the order it happens (all blocking - run it on a worker):

* :func:`connect`     opens Notion's "Allow access" page in the browser and waits
                      for the answer on a one-shot listener at 127.0.0.1 (random
                      port). Notion redirects to ONE registered https address, our
                      Supabase function ``notion-oauth``, which answers with a bare
                      302 to that listener (the port rides inside ``state``). The
                      code is then swapped for tokens by the same function, because
                      only it holds Notion's client secret. Can be cancelled with a
                      ``threading.Event``; the listener stops within ~0.2 s.
* :func:`exchange` / :func:`refresh` / :func:`revoke`  the three calls to that
                      function. Notion rotates the refresh token, so :func:`refresh`
                      is serialised by ``_LOCK`` and remembers the newest pair for
                      the process (:func:`latest_tokens`): a second caller that
                      still holds the token that was just swapped gets the new pair
                      instead of a second POST that would be refused.
* :func:`validate_token`, :func:`search_pages`, :func:`create_hub`,
  :func:`choose_destination`, :func:`page_alive`   straight between this computer
                      and Notion (never through our server): check a pasted secret,
                      list the pages the user shared, make the private "Gusan
                      meetings" page and pick where notes go.
* :func:`landing_html` what the browser tab shows after Notion (light and dark).

"mcp" mode - what the Connect button uses - lives in notion_mcp.py (Notion's hosted
server: browser consent, no page to share, no secret, no page to pick). The calls the
sheet and main.py make are these thin ones, all blocking (run them on a worker):

* :func:`mcp_available` ``() -> True``   the one-button path exists in this build.
* :func:`connect_mcp` ``(cfg=None, open_browser=webbrowser.open, timeout=LOGIN_TIMEOUT,
  cancel=None, on_url=None, on_code=None, persist=None) -> dict``   the whole connect:
  consent in the browser, then the private "Gusan meetings" page. ``cfg`` is a snapshot of
  the settings (it supplies ``notion_mcp_client_id``, ``notion_hub_page`` and
  ``notion_workspace_name``); ``on_url(url)`` once the listener is up (keep it for "Open
  Notion again"); ``on_code()`` when Notion answered and "Setting things up" starts;
  ``persist(dict)`` is called on the worker with ``{"notion_mcp_client_id": id,
  "notion_mcp_redirects": "2"}`` the moment the client is registered. Returns
  ``{"mode": "mcp", "token", "refresh",
  "workspace", "expires_at", "client_id", "redirects", "dest": {"id", "title", "kind": "hub",
  "url"}, "pages": None, "settings": {...}}`` - ``settings`` holds every config key of the
  connection (notion_api_key, notion_refresh_token, notion_auth_mode "mcp",
  notion_workspace_name, notion_parent_page, notion_parent_title, notion_hub_page,
  notion_reconnect False, notion_mcp_client_id, notion_mcp_redirects, notion_token_expires): save it in ONE
  set_settings call, then flush_config_save. Raises :class:`ConnectError` (code: timeout |
  denied | offline | port_busy | expired | server | browser | full | other) or
  :class:`ConnectCancelled` once ``cancel`` (a threading.Event) is set; a grant that was
  made is given back first. Notion comes back to https://gusan.aibuben.xyz/notion/callback
  (a page on our site that hands the answer on to this computer) when that page is up, else
  straight to 127.0.0.1; the browser tab is kept open until the connect has finished and
  then says "Connected." or "That didn't work." (notion_mcp.py has the details).
* :func:`disconnect` ``(cfg) -> bool``   give a connection back at Notion, whatever its
  mode ("mcp" and "oauth" revoke, "own" has nothing to give back). Pass the settings as
  they were BEFORE they were blanked. True = done or nothing to do; False = Notion could
  not be told (the user can remove Gusan under Settings > Connections in Notion).
* :func:`revoke_mcp` ``(token, refresh_token="", client_id="") -> bool``   the same for
  an mcp connection when the caller holds the pieces (main's other-account sign-in).
* :func:`latest_tokens` / :func:`forget_tokens`   cover both token chains.

Sending is notion_sync.send_meeting (it routes "mcp" itself). A refreshed token pair is
handed to the ``persist`` callback of send_meeting BEFORE the next request, with
``notion_token_expires`` and ``notion_reconnect`` False; a connection Notion refuses for
good sets ``notion_reconnect`` True through ``persist``. The REST helpers further down
(validate_token, search_pages, create_hub, choose_destination, page_alive) belong to the
"own" and "oauth" modes: an "mcp" token does NOT work against api.notion.com.

Nothing here reads or writes the config or the keyring: the caller (the sheet or
main.py) keeps the tokens, so this module tests with fake HTTP only. Failures are
:class:`ConnectError` with a ``code`` and a plain sentence from :data:`MESSAGES`
(the details go to the log; a token, a code and what Notion or ``requests`` said
never appear in a message or the log). The Notion REST helpers raise
notion_export.NotionError and its two subclasses instead.

``NOTION_CLIENT_ID`` ships empty until the Notion connection "Gusan" exists
(docs/redesign/notion-setup-for-founder.md); :func:`configured` is the gate for
the one-button screen. The client id is public (it is in the address bar of the
consent page), the client secret never leaves the server.
"""
import http.server
import logging
import re
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser

import requests

import notion_export
from app_info import APP_NAME
from notion_export import NotionError

logger = logging.getLogger("transcribe.notion")

# The OAuth client id of the public Notion connection "Gusan" (Notion developer
# portal -> the connection -> Configuration -> OAuth client ID). Public.
NOTION_CLIENT_ID = ""
FUNCTION_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/notion-oauth"
REDIRECT_URI = FUNCTION_URL     # the one redirect URI registered in Notion
AUTH_URL = "https://api.notion.com/v1/oauth/authorize"
API_BASE = notion_export.API_BASE
LOGIN_TIMEOUT = 180
HTTP_TIMEOUT = 15
HOLD_SECONDS = 100              # the most a held browser request (see _Callback) waits for the app to finish
HUB_TITLE = "Gusan meetings"
HUB_INTRO = "Meeting notes sent from Gusan land here, one page per meeting."
INTEGRATIONS_URL = "https://www.notion.so/my-integrations"
NOTION_HOME_URL = "https://www.notion.so"

CALLBACK_PATH = "/notion"       # where the function's 302 lands on 127.0.0.1
POLL_SECONDS = 0.2              # handle_request() slice: a cancel is seen this fast
MAX_CODE_LEN = 2000             # the function caps codes and tokens the same way
SEARCH_MAX_REQUESTS = 10        # pages of search results followed, at most

# Fixed wording for every way connecting can end (the sheet shows one of these).
# "rejected" and "reconnect" are not ConnectError codes of connect() itself: they
# are the sentences for a pasted secret Notion refused and for a later 401.
MESSAGES = {
    "cancelled": "Cancelled.",
    "denied": "You cancelled in Notion. Nothing was connected.",
    "timeout": "That took too long. Nothing was connected.",
    "offline": "Can't reach Notion. Check your internet and try again.",
    "port_busy": f"Couldn't start the connection on this computer. Close other {APP_NAME} windows and try again.",
    "expired": "That Notion sign-in expired. Try again.",
    "server": "Notion couldn't finish connecting. Try again in a minute.",
    "browser": "Couldn't open your browser. Try again.",
    "full": notion_export.FULL_TEXT,
    "rejected": "Notion didn't accept that secret. Copy it again from your integration.",
    "reconnect": "Notion needs to be connected again.",
    "other": "Couldn't connect to Notion. Try again.",
}


class ConnectError(RuntimeError):
    """Connecting (or refreshing) failed. ``code`` is a key of MESSAGES and
    ``str(e)`` its sentence - never anything a server or library said.

    Codes: cancelled | denied | timeout | offline | port_busy | expired |
    server | browser | full | other. From :func:`refresh`, "expired" means Notion
    refused the refresh token for good: the user has to connect again. "full":
    connected, but the workspace has no room for even the first page."""

    def __init__(self, code="other"):
        self.code = code if code in MESSAGES else "other"
        super().__init__(MESSAGES[self.code])


class ConnectCancelled(ConnectError):
    """The user pressed Cancel (nothing to show)."""

    def __init__(self):
        super().__init__("cancelled")


def configured(cfg=None):
    """True when this build has a Notion client id to connect with. While it is
    False the sheet opens on the "own integration" path and says nothing about
    the missing button."""
    return bool(NOTION_CLIENT_ID.strip())


# ── authorize URL and state ──────────────────────────────────────────────────
# state = "<nonce>.<port>": the function validates exactly this shape before it
# redirects to 127.0.0.1:<port>. tests/test_notion_connect.py checks this regex,
# MAX_CODE_LEN, CALLBACK_PATH and the redirect against the function's index.ts,
# so the two sides cannot drift apart unnoticed.
_STATE_RE = re.compile(r"([A-Za-z0-9_-]{16,64})\.([0-9]{4,5})")


def build_state(nonce, port):
    return f"{nonce}.{port}"


def parse_state(state):
    """(nonce, port) from a state, or None when it is not the shape the
    function accepts (16-64 url-safe characters, a dot, a port 1024-65535)."""
    m = _STATE_RE.fullmatch(state) if isinstance(state, str) else None
    if not m:
        return None
    port = int(m.group(2))
    return (m.group(1), port) if 1024 <= port <= 65535 else None


def authorize_url(client_id, state):
    """Notion's "Allow access" page for this client, coming back to ``state``."""
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "owner": "user",
        "redirect_uri": REDIRECT_URI, "state": state,
    })


# ── the browser tab after Notion ─────────────────────────────────────────────
# One inline page, no external request, readable in light and dark. It is shown
# BEFORE the code is swapped, so it only promises what is true at that moment.
_LANDING = {
    "ok": ("check", "Almost done.",
           f"Go back to {APP_NAME} to finish. You can close this tab."),
    "error": ("cross", "Notion wasn't connected.",
              f"Go back to {APP_NAME} and try again."),
    "denied": ("cross", "Cancelled.", "Nothing was connected."),
    # The "mcp" connect keeps the tab waiting until the app is done (see _Callback), so it can say how it went.
    "connected": ("check", "Connected.", f"You can close this tab and go back to {APP_NAME}."),
    "failed": ("cross", "That didn't work.", f"Go back to {APP_NAME} and try again."),
}
_ICONS = {
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "cross": '<path d="M6 6l12 12M18 6L6 18"/>',
}
_LANDING_CSS = (
    ":root{color-scheme:light dark}"
    "*{box-sizing:border-box}"
    "body{margin:0;min-height:100vh;padding:40px 16px;color:#334155;"
    "background-color:#eaf0f8;background-image:linear-gradient(160deg,#eaf0f8,#f4f7fb);"
    'font-family:"Segoe UI Variable","Segoe UI",system-ui,sans-serif}'
    ".card{max-width:420px;margin:0 auto;padding:28px;border-radius:16px;"
    "background:#fff;border:1px solid #e2e8f0}"
    ".tile{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;"
    "justify-content:center}"
    ".check{color:#16a34a;background:#dcfce7}"
    ".cross{color:#b91c1c;background:#fee2e2}"
    "h1{margin:20px 0 6px;font-size:20px;line-height:1.3;font-weight:700;"
    "letter-spacing:-.01em;color:#0f172a}"
    "p{margin:0;font-size:15px;line-height:1.5;color:#334155}"
    "@media (prefers-color-scheme:dark){"
    "body{color:#cbd5e1;background-color:#0f172a;background-image:none}"
    ".card{background:#1e293b;border-color:#334155}"
    "h1{color:#fff}p{color:#cbd5e1}"
    ".check{color:#4ade80;background:rgba(74,222,128,.14)}"
    ".cross{color:#f87171;background:rgba(248,113,113,.14)}}"
)


def landing_html(kind):
    """The page the browser shows after Notion: kind "ok" (a code came back),
    "error" (Notion sent an error) or "denied" (the user said no); the "mcp"
    connect, which answers only when it is done, also uses "connected" and
    "failed". Anything else reads as "error"."""
    icon, title, line = _LANDING.get(kind) or _LANDING["error"]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{APP_NAME}</title><style>{_LANDING_CSS}</style></head>"
        f'<body><main class="card"><div class="tile {icon}">'
        '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        f"{_ICONS[icon]}</svg></div><h1>{title}</h1><p>{line}</p></main></body></html>")


# ── the one-shot listener ────────────────────────────────────────────────────
class _Callback(http.server.BaseHTTPRequestHandler):
    """One-shot handler for the function's redirect back to 127.0.0.1."""

    # Per accepted connection: an idle socket (a browser preconnect, a port
    # scan) is dropped after this long. Each connection has its own thread, so
    # it cannot hold up the real redirect either.
    timeout = 5

    def _reply(self, status, body=None):
        data = (body or "").encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        try:
            query = urllib.parse.parse_qs(parsed.query, max_num_fields=20)
        except ValueError:
            query = {}
        # The listener of the "mcp" mode answers on its own path (server.callback_path).
        if parsed.path != getattr(self.server, "callback_path", CALLBACK_PATH) \
                or ("code" not in query and "error" not in query):
            self._reply(404)                    # favicon and friends
            return
        # Only the function's redirect carries our state. Anything else - a
        # local process or a web page probing ports with ?error=access_denied -
        # is refused and the real redirect is still waited for.
        expected = getattr(self.server, "expected_state", "") or ""
        got = query.get("state", [""])[0]
        if not expected or not secrets.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
            self._reply(400)
            return
        error = query.get("error", [""])[0]
        code = query.get("code", [""])[0]
        if error:
            result = {"error": error[:60]}
            kind = "denied" if error == "access_denied" else "error"
        elif code and len(code) <= MAX_CODE_LEN:
            result = {"code": code}
            kind = "ok"
        else:
            self._reply(400)
            return
        if kind == "ok" and getattr(self.server, "hold_tab", False):
            # The "mcp" connect still has a code to swap and a first page to make. Take the answer
            # now, but keep this request open and tell the tab how it went once the app knows
            # (HOLD_SECONDS at most: after that it reads "Almost done", like every other flow).
            with self.server.lock:
                first = self.server.result is None
                if first:
                    self.server.result = result
            self.server.done.set()
            kind = "ok"
            if self.server.finished.wait(HOLD_SECONDS if first else 0):
                kind = getattr(self.server, "tab_kind", "") or "ok"
            try:
                self._reply(200, landing_html(kind))
            except OSError:
                pass                            # the tab went away; nothing else depends on it
            return
        try:
            self._reply(200, landing_html(kind))
        except OSError:
            pass                                # the tab went away; the answer still counts
        with self.server.lock:
            if self.server.result is None:      # the first valid answer wins
                self.server.result = result
        self.server.done.set()

    def log_message(self, *args):
        return                                  # the default line would print the code


class _LoopbackServer(http.server.ThreadingHTTPServer):
    """The redirect listener, bound exclusively on Windows (as auth.py's):
    stock HTTPServer sets SO_REUSEADDR, which there lets another socket bind
    the same port while it listens. POSIX keeps SO_REUSEADDR (it never shares
    a listening port there). Threaded, so an idle connection cannot hold up
    the redirect."""

    daemon_threads = True
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _cancelled(cancel):
    return cancel is not None and cancel.is_set()


def _notify(callback, *args):
    """Call an optional UI callback; one that raises must not break the flow."""
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        logger.debug("Notion connect callback failed", exc_info=True)


def connect(cfg=None, open_browser=webbrowser.open, timeout=LOGIN_TIMEOUT, cancel=None,
            on_url=None, on_code=None):
    """Connect Notion in the browser; returns the token dict (the keys of
    :func:`exchange`). Blocking - run on a worker thread.

    ``cfg`` is unused (the client id is the NOTION_CLIENT_ID constant); it is
    kept so every ``connect`` in the app has one shape. ``on_url(url)`` is
    called once the listener is up, before the browser is asked to open - keep
    the url to offer "Open Notion again". ``on_code()`` is called when the code
    arrived and the swap starts (the sheet's "Setting things up…" state).

    Raises ConnectError (code: timeout | denied | offline | port_busy |
    expired | server | browser | other) or ConnectCancelled once ``cancel`` (a
    threading.Event) is set: the listener stops within about a fraction of a
    second and nothing is kept (a grant that was already made is revoked).
    """
    client_id = NOTION_CLIENT_ID.strip()
    if not client_id:
        raise ConnectError("other")
    if _cancelled(cancel):
        raise ConnectCancelled()
    try:
        server = _LoopbackServer(("127.0.0.1", 0), _Callback)
    except OSError as e:
        logger.warning("Notion connect: listener failed to start (%s)", type(e).__name__)
        raise ConnectError("port_busy") from e
    server.timeout = POLL_SECONDS
    server.result = None
    server.done = threading.Event()
    server.lock = threading.Lock()
    server.expected_state = build_state(secrets.token_urlsafe(18), server.server_address[1])
    url = authorize_url(client_id, server.expected_state)
    try:
        _notify(on_url, url)
        try:
            opened = open_browser(url)
        except Exception:
            opened = False
        if opened is False:                     # webbrowser.open: no browser could start
            raise ConnectError("browser")
        deadline = time.monotonic() + timeout
        while not server.done.is_set() and time.monotonic() < deadline and not _cancelled(cancel):
            server.handle_request()
            server.done.wait(0.05)              # the handler thread answers in a few ms
    finally:
        server.server_close()

    if _cancelled(cancel):
        raise ConnectCancelled()
    res = server.result
    if res is None:
        raise ConnectError("timeout")
    if res.get("error"):
        raise ConnectError("denied" if res["error"] == "access_denied" else "other")
    _notify(on_code)
    tok = exchange(res["code"])
    if _cancelled(cancel):
        # Cancelled while the code was being swapped: don't keep the grant.
        revoke(tok["access_token"])
        raise ConnectCancelled()
    return tok


# ── the edge function: exchange, refresh, revoke ─────────────────────────────
_LOCK = threading.RLock()       # serialises refresh(); re-entrant so a caller may hold it around refresh + save
_LATEST = {}                    # the newest token pair this process got (see refresh)
_SUPERSEDED = []                # refresh tokens already swapped for a newer pair, oldest first
_SUPERSEDED_KEEP = 16

_TOKEN_KEYS = ("access_token", "refresh_token", "workspace_id", "workspace_name",
               "workspace_icon", "bot_id", "duplicated_template_id")


def _code_for(status, err):
    """The ConnectError code for a failed answer from the function."""
    if status == 400 and err == "invalid_grant":
        return "expired"
    if status in (404, 429) or status >= 500:
        return "server"                         # not deployed, busy, or its own Notion call failed
    return "other"


def _function(action, **fields):
    """POST one action to the edge function; the parsed JSON object, or a
    ConnectError. The body holds secrets, so neither it nor the answer is ever
    logged - only the action and the status."""
    try:
        r = requests.post(FUNCTION_URL, json={"action": action, **fields}, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        logger.info("notion-oauth %s: couldn't connect (%s)", action, type(e).__name__)
        raise ConnectError("offline") from e
    try:
        data = r.json()
    except Exception:
        data = None
    if r.status_code == 200:
        if isinstance(data, dict):
            return data
        logger.info("notion-oauth %s: unreadable answer", action)
        raise ConnectError("server")
    err = data.get("error") if isinstance(data, dict) else ""
    err = err if isinstance(err, str) and re.fullmatch(r"[a-z_]{1,40}", err) else ""
    logger.info("notion-oauth %s: answered %s %s", action, r.status_code, err)
    raise ConnectError(_code_for(r.status_code, err))


_NAME_JUNK = re.compile(r"[<>\x00-\x1f\x7f-\x9f]")


def clean_name(text):
    """A workspace name for display: one line, at most 200 characters, with no
    angle brackets or control characters. The name is chosen by whoever owns that
    Notion workspace - it is data to show, never markup (the labels that show it
    are plain text too)."""
    return " ".join(_NAME_JUNK.sub(" ", text).split())[:200] if isinstance(text, str) else ""


def _tokens(data):
    """The token dict of an answer: every key a string ("" when missing), and
    an access token that is really there."""
    out = {k: (data.get(k) if isinstance(data.get(k), str) else "") for k in _TOKEN_KEYS}
    out["workspace_name"] = clean_name(out["workspace_name"])
    if not out["access_token"]:
        logger.info("notion-oauth: answer without an access token")
        raise ConnectError("server")
    return out


def forget_tokens():
    """Drop the in-memory token pair and the refresh history (a new connection
    or a disconnect starts a new chain). Covers the "mcp" chain too."""
    global _LATEST
    with _LOCK:
        _LATEST = {}
        del _SUPERSEDED[:]
    import notion_mcp
    notion_mcp.forget_tokens()


def latest_tokens():
    """The newest pair :func:`refresh` (or the "mcp" mode's refresh) returned in
    this process ({"access_token", "refresh_token", ...}), or {}. It is newer than
    the saved config until the caller's save has run: prefer it when sending."""
    import notion_mcp
    return notion_mcp.latest_tokens() or dict(_LATEST)


def exchange(code, redirect_uri=REDIRECT_URI):
    """Swap the code Notion gave for tokens. Returns {"access_token",
    "refresh_token" ("" if Notion gave none), "workspace_id", "workspace_name",
    "workspace_icon", "bot_id", "duplicated_template_id"}, all strings."""
    code = (code or "").strip()
    if not code:
        raise ConnectError("other")
    tok = _tokens(_function("exchange", code=code, redirect_uri=redirect_uri))
    forget_tokens()
    return tok


def refresh(refresh_token):
    """A new token pair for a refresh token (same keys as :func:`exchange`).

    Notion rotates the refresh token, so two callers that both hold the old one
    must not both POST: the second would be refused and look like a lost
    connection. Serialised by ``_LOCK``; a token that was already swapped gets
    the newest pair back with no request. ConnectError code "expired" means
    Notion refused it for good (reconnect); "offline" / "server" are temporary
    and the saved connection is still fine.
    """
    old = (refresh_token or "").strip()
    if not old:
        raise ConnectError("expired")
    global _LATEST
    with _LOCK:
        if old in _SUPERSEDED and _LATEST:
            return dict(_LATEST)
        tok = _tokens(_function("refresh", refresh_token=old))
        if not tok["refresh_token"]:
            tok["refresh_token"] = old          # Notion didn't rotate it: keep the one we have
        elif tok["refresh_token"] != old:
            _SUPERSEDED.append(old)
            del _SUPERSEDED[:-_SUPERSEDED_KEEP]
        _LATEST = tok
        return dict(tok)


def revoke(token):
    """Give the app's access back at Notion (best effort, blocking, never
    raises): the token is revoked and with it the grant. True when Notion did
    it (or there was nothing to give back), False when it could not be done
    (offline, our function down): the grant is then still there, and only the
    user can remove it in Notion's settings."""
    token = (token or "").strip()
    if not token:
        return True
    done = True
    try:
        _function("revoke", token=token)
    except ConnectError:
        done = False                            # already logged
    except Exception:
        done = False
        logger.debug("Notion revoke failed", exc_info=True)
    forget_tokens()
    return done


# ── Notion itself: check a token, list and make pages ────────────────────────
_STOP_CODES = ("auth", "offline", "rate")   # a chain of fallbacks stops on these: the next step would fail the same way


def _api(method, path, token, **kw):
    """One Notion call (notion_export.call): a token that cannot be a Notion
    token is a NotionAuthError before anything is sent."""
    return notion_export.call(method, path, token, timeout=HTTP_TIMEOUT, **kw)


def _trashed(page):
    return bool(page.get("in_trash") or page.get("archived"))


def _page_title(page):
    """The page's title: the property of type "title" (named anything - a
    database row calls it "Name"), as one line; "Untitled" when empty."""
    props = page.get("properties")
    for prop in (props.values() if isinstance(props, dict) else ()):
        if not isinstance(prop, dict) or prop.get("type") != "title":
            continue
        parts = [p for p in (prop.get("title") or []) if isinstance(p, dict)]
        text = "".join(p.get("plain_text") or (p.get("text") or {}).get("content") or ""
                       for p in parts)
        text = " ".join(text.split())
        if text:
            return text[:200]
    return "Untitled"


def _page_item(page):
    """One search result as {"id", "title", "emoji", "url", "top"}, or None for
    anything that is not a live page. ``top``: a top-level page (its parent is
    the workspace, not another page or a database)."""
    if not isinstance(page, dict) or page.get("object") != "page" or _trashed(page):
        return None
    page_id = notion_export.extract_page_id(page.get("id"))
    if not page_id:
        return None
    icon = page.get("icon") if isinstance(page.get("icon"), dict) else {}
    parent = page.get("parent") if isinstance(page.get("parent"), dict) else {}
    emoji = icon.get("emoji") if icon.get("type") == "emoji" else ""
    url = page.get("url")
    return {"id": page_id, "title": _page_title(page),
            "emoji": emoji if isinstance(emoji, str) else "",
            "url": url if isinstance(url, str) else "",
            "top": parent.get("type") == "workspace"}


def validate_token(token):
    """Check a token with Notion (GET /users/me). Returns {"workspace_name":
    str ("" when Notion doesn't say)}. Raises notion_export.NotionAuthError
    when Notion refuses it, NotionError (code "offline" / "other") otherwise."""
    data = _api("get", "/users/me", token)
    bot = data.get("bot") if isinstance(data.get("bot"), dict) else {}
    return {"workspace_name": clean_name(bot.get("workspace_name"))}


def search_pages(token, query="", limit=50):
    """The pages this token can see, most recently edited first, up to
    ``limit`` (follows Notion's paging; trashed pages are dropped). Each item:
    {"id" (dashed), "title", "emoji" ("" if none), "url", "top"}. A page whose
    title is empty reads "Untitled". Raises NotionError."""
    limit = max(1, int(limit))
    query = (query or "").strip()
    items, seen, cursor = [], set(), None
    for _ in range(SEARCH_MAX_REQUESTS):
        body = {"filter": {"property": "object", "value": "page"},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
                "page_size": min(100, limit)}
        if query:
            body["query"] = query
        if cursor:
            body["start_cursor"] = cursor
        data = _api("post", "/search", token, json=body)
        for raw in data.get("results") or []:
            item = _page_item(raw)
            if item and item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        cursor = data.get("next_cursor")
        if len(items) >= limit or not data.get("has_more") or not cursor:
            break
    return items[:limit]


def create_hub(token, title=HUB_TITLE):
    """Make the private page notes land under, at the top of the user's
    workspace: {"id" (dashed), "title", "url"}. If Notion rejects the parent
    shape (400), it is tried once more in the short form. Raises NotionError."""
    body = {"parent": {"type": "workspace", "workspace": True},
            "properties": {"title": {"title": notion_export._rich(title)}},
            "children": [notion_export._block("paragraph", HUB_INTRO)]}
    try:
        data = _api("post", "/pages", token, json=body)
    except NotionError as e:
        if e.status != 400:
            raise
        data = _api("post", "/pages", token, json=dict(body, parent={"workspace": True}))
    page_id = notion_export.extract_page_id(data.get("id"))
    if not page_id:
        raise NotionError("Notion didn't create the page.")
    url = data.get("url")
    return {"id": page_id, "title": title, "url": url if isinstance(url, str) else ""}


def choose_destination(token, template_id=""):
    """Where notes go right after connecting. In order: the template page Notion
    duplicated for the user (``template_id``), a new private hub page, the most
    recently edited page the user shared, else None (nothing to send to yet:
    ask them to share a page). Returns {"id", "title", "kind": "template" |
    "hub" | "page"} or None. A step that fails for its own reason falls through
    to the next; auth, offline and rate errors raise, since the next step would
    fail the same way."""
    tid = notion_export.extract_page_id(template_id)
    if tid:
        try:
            page = _api("get", f"/pages/{tid}", token)
            if not _trashed(page):
                return {"id": tid, "title": _page_title(page), "kind": "template"}
        except NotionError as e:
            if e.code in _STOP_CODES:
                raise
    try:
        hub = create_hub(token)
        return {"id": hub["id"], "title": hub["title"], "kind": "hub"}
    except NotionError as e:
        if e.code in _STOP_CODES:
            raise
    pages = search_pages(token, limit=1)
    if pages:
        return {"id": pages[0]["id"], "title": pages[0]["title"], "kind": "page"}
    return None


def page_alive(token, page_id):
    """True when the page exists, is not in the trash and this token can reach
    it; False when it is gone or not shared. Any other failure (token refused,
    offline, busy) raises NotionError: it says nothing about the page."""
    pid = notion_export.extract_page_id(page_id)
    if not pid:
        return False
    try:
        return not _trashed(_api("get", f"/pages/{pid}", token))
    except NotionError as e:
        if e.code == "no_access":
            return False
        raise


# ── "mcp" mode: Notion's hosted server (the work is in notion_mcp.py) ────────
def mcp_available():
    """True when this build can connect Notion with one button through Notion's hosted
    server (nothing to configure: the client registers itself). The sheet's gate
    for the one-button screen."""
    return True


def connect_mcp(cfg=None, open_browser=webbrowser.open, timeout=LOGIN_TIMEOUT, cancel=None,
                on_url=None, on_code=None, persist=None):
    """Connect Notion in the browser and make the private "Gusan meetings" page; the
    result dict is described in the module docstring. Blocking - run on a worker.
    Raises ConnectError / ConnectCancelled."""
    import notion_mcp
    return notion_mcp.connect(cfg, open_browser=open_browser, timeout=timeout, cancel=cancel,
                              on_url=on_url, on_code=on_code, persist=persist)


def revoke_mcp(token="", refresh_token="", client_id=""):
    """Give an "mcp" connection back at Notion (best effort, never raises). True when
    Notion was told (or there was nothing to tell)."""
    import notion_mcp
    return notion_mcp.revoke(token, refresh_token, client_id)


def disconnect(cfg):
    """Give the connection described by ``cfg`` (the settings BEFORE they were blanked)
    back at Notion, whatever its mode. Best effort, blocking, never raises: True when
    Notion was told or there was nothing to tell (a pasted secret is the user's own),
    False when it could not be told. The in-memory tokens are forgotten either way."""
    cfg = cfg or {}
    try:
        mode = notion_export.connection_mode(cfg)
        newest = latest_tokens()
        token = (newest.get("access_token") or cfg.get("notion_api_key") or "").strip()
        if mode == "mcp":
            refresh_token = (newest.get("refresh_token") or cfg.get("notion_refresh_token") or "").strip()
            return revoke_mcp(token, refresh_token, (cfg.get("notion_mcp_client_id") or "").strip())
        if mode == "oauth":
            return revoke(token)
        forget_tokens()
        return True
    except Exception:
        logger.debug("Notion disconnect failed", exc_info=True)
        return False
