"""Google Calendar for meeting recording - read-only, and straight between
Google and this computer (never through our server).

* :func:`connect`     one-time sign-in in the browser (OAuth for installed
                      apps: PKCE + a loopback redirect on a random local port).
                      Only the refresh token is kept, in the OS keyring. Can be
                      cancelled with a ``threading.Event``.
* :func:`disconnect`  forget it here (:func:`forget_local`) and give the access
                      back at Google (:func:`revoke`). A give-back Google never
                      heard (offline, the app quit first) waits in the keyring
                      until :func:`retry_pending_revoke` finishes it.
* :func:`upcoming`    the meetings from an hour ago to the end of tomorrow
                      (:func:`window_hours`): title, people, agenda, call link
                      and call type.
* :func:`should_prompt` / :func:`reminder_due`  when to ask "record this
                      meeting?" (1 min before until 5 min after the start).
* :func:`worth_asking`, :func:`visible_upcoming`, :func:`people_label`,
  :func:`when_parts`, :func:`reminder_text`  what the "Coming up" list and the
  reminder show; :func:`next_fetch_delay` how often to read again.
* :func:`plain_agenda`, :func:`cap_names`, :func:`clean_person_name`  the invite
  text made short and safe (the AI-facing parts live in invite_context.py).

Parsing and timing are pure functions, so they test without Google. Every
datetime here is timezone-aware: pass ``datetime.now(timezone.utc)`` as now.

Run ``python calendar_sync.py`` from the project's venv (Gusan closed) to save
the Google client ID and secret in config.json without typing them in a chat.

The service around this (worker fetches, reminder timer, saved bell/prompted
state) is calendar_service.py.
"""
import base64
import getpass
import hashlib
import html
import http.server
import json
import logging
import math
import random
import re
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

import requests

import invite_context
import storage
from app_info import APP_NAME

logger = logging.getLogger("transcribe.calendar")

# An OAuth client of type "Desktop app" (Google Cloud Console -> APIs & Services
# -> Credentials) in the project that has the Google Calendar API enabled. For
# installed apps Google does not treat this secret as confidential - it ships
# inside every copy of a desktop app, but its OAuth policy says never to commit
# credentials to a public repo: the ID is a constant here, the secret stays ""
# in git and is put in at build time (a GitHub secret) or, for testing, in the
# local config.json through ``python calendar_sync.py``. Config keys override both.
GOOGLE_CLIENT_ID = ""
GOOGLE_CLIENT_SECRET = ""

# The narrowest scope that reads the primary calendar's events: "events on
# calendars you own". SCOPE_EVENTS (all your calendars) is the tested fallback,
# for the case that owned.readonly doesn't return people or the call link for an
# event you were invited to; cfg "google_oauth_scope" tries it without a code
# change (testing only). Installed apps have no incremental authorization, so
# this is decided before the first verification and never widened later.
SCOPE_OWNED = "https://www.googleapis.com/auth/calendar.events.owned.readonly"
SCOPE_EVENTS = "https://www.googleapis.com/auth/calendar.events.readonly"
SCOPES = "openid email " + SCOPE_OWNED
# Every scope events.list accepts: a grant must hold one of them, exactly.
EVENT_SCOPES = frozenset({
    SCOPE_OWNED, SCOPE_EVENTS,
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
})

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
REFRESH_SECRET = "calendar_google_refresh_token"
# A refresh token whose give-back to Google (revoke) hasn't been confirmed yet.
PENDING_REVOKE_SECRET = "calendar_google_pending_revoke"
LOGIN_TIMEOUT = 180
HTTP_TIMEOUT = 15
# Ask Google for plain meetings only, and only the fields read below (a smaller
# answer, and nothing else about your calendar is even transferred).
EVENT_TYPES = "default"
EVENT_FIELDS = (
    "nextPageToken,items(id,status,summary,description,location,start,end,eventType,"
    "visibility,recurringEventId,hangoutLink,"
    "conferenceData(conferenceSolution/name,entryPoints(entryPointType,uri)),"
    "organizer(self),attendees(email,displayName,self,resource,responseStatus),"
    "attendeesOmitted)")
_SKIP_EVENT_TYPES = {"workingLocation", "outOfOffice", "focusTime", "fromGmail", "birthday"}
# At most this many names (and this much agenda) of one invite is ever kept.
MAX_PEOPLE = invite_context.MAX_PEOPLE
AGENDA_CHARS = invite_context.AGENDA_CHARS
MAX_TITLE_CHARS = invite_context.MAX_TITLE_CHARS
# Fixed wording (the details go to the log): a requests error's text holds
# object addresses, so it would differ on every try and read as a new error.
OFFLINE_TEXT = "Couldn't reach Google - check your internet connection."
CALENDAR_OFFLINE_TEXT = "Couldn't reach Google Calendar - check your internet connection."

# How far ahead "Coming up" reads: to the end of tomorrow in local time (so the
# "Today" / "Tomorrow" headers mean something), but never less than WINDOW_MIN_H
# or more than WINDOW_MAX_H hours (window_hours). CALENDAR_WINDOW_H is that
# minimum, kept for the old name. PAST_MINUTES reaches back so a meeting that
# started a few minutes ago is still there to record; upcoming() follows
# nextPageToken.
WINDOW_MIN_H = 12
WINDOW_MAX_H = 48
CALENDAR_WINDOW_H = WINDOW_MIN_H
PAST_MINUTES = 60
PAGE_SIZE = 50
MAX_PAGES = 4
# The reminder window: from a minute before the start until five minutes in.
REMIND_LEAD_S = 60
REMIND_GRACE_S = 300
# How often to read the calendar again (next_fetch_delay): soon while a meeting
# is on or near, rarely when the day is quiet, doubled per failed read up to the
# cap, with +-25 % jitter, plus one read just before the next start.
FETCH_FAST_S = 300
FETCH_IDLE_S = 900
FETCH_PRE_MEETING_S = 150
FETCH_BACKOFF_CAP_S = 1800
FETCH_NEAR_S = 2 * 3600
FETCH_JITTER = 0.25

CALL_TYPES = ("Google Meet", "Zoom", "Teams", "Webex")
CALL_OTHER = "other"

# Where the browser lands after Google's sign-in: pages on the Gusan site (they
# carry nothing from the sign-in), so the address bar never shows 127.0.0.1.
CONNECTED_URL = "https://gusan.aibuben.xyz/calendar/connected"
NOT_CONNECTED_URL = "https://gusan.aibuben.xyz/calendar/not-connected"

_OK_HTML = ("<!doctype html><html><head><meta charset='utf-8'><title>" + APP_NAME + "</title>"
            "<style>body{font-family:Segoe UI,system-ui,sans-serif;background:#0f0f12;"
            "color:#fff;display:flex;height:100vh;align-items:center;justify-content:center;"
            "margin:0}.c{color:#22c55e;font-size:42px;text-align:center}</style></head>"
            "<body><div><div class='c'>&#10003;</div><h2>Calendar connected to " + APP_NAME + "</h2>"
            "<p>You can close this tab.</p></div></body></html>")
_FAIL_HTML = ("<!doctype html><html><head><meta charset='utf-8'><title>" + APP_NAME + "</title></head>"
              "<body style='font-family:sans-serif'><h2>Calendar wasn't connected</h2>"
              "<p>Go back to " + APP_NAME + " and try again.</p></body></html>")


# What the user reads (the details go to the log, never a token or a body).
EXPIRED_TEXT = "Calendar access expired - connect it again on the Meetings page."
SCOPE_TEXT = "Calendar permission is missing. Connect again and tick the calendar box."
API_OFF_TEXT = "Google Calendar isn't switched on for this app yet."
QUOTA_TEXT = "Google is busy. Trying again soon."
SERVER_TEXT = "Google Calendar isn't answering. Trying again soon."
NOT_GRANTED_TEXT = ("Calendar access wasn't granted - tick the calendar "
                    "permission on Google's screen.")


class CalendarError(RuntimeError):
    """A user-readable reason the calendar couldn't be reached. ``code`` says
    what kind, for telemetry and backoff (never any text): "other", "timeout",
    "denied", "offline", "port_busy", "scope", "admin", "client", "keyring",
    "api_off", "quota", "server", "auth", "cancelled"."""

    code = "other"
    status = 0           # the HTTP status of a Google answer, when there was one
    reason = ""          # Google's reason word for it (never a message or a token)

    def __init__(self, message="", code=None):
        super().__init__(message)
        if code:
            self.code = code


class CalendarAuthError(CalendarError):
    """Access is gone (revoked, expired): the user must connect again."""

    code = "auth"


class CalendarCancelled(CalendarError):
    """The user cancelled the browser sign-in (nothing to show)."""

    code = "cancelled"


class CalendarApiError(CalendarError):
    """Google Calendar answered, but not with events. ``code``: "api_off" (the
    API isn't switched on for this app), "quota" (rate limit), "server" (5xx) or
    "other"; ``status`` is the HTTP status and ``reason`` Google's reason word.
    Quota and server errors are temporary: the service backs off and keeps the
    list it has."""

    def __init__(self, message="", code="other", status=0, reason=""):
        super().__init__(message, code)
        self.status = status
        self.reason = reason


def _client(cfg=None):
    cfg = cfg or {}
    return ((cfg.get("google_oauth_client_id") or GOOGLE_CLIENT_ID).strip(),
            (cfg.get("google_oauth_client_secret") or GOOGLE_CLIENT_SECRET).strip())


def configured(cfg=None):
    """True when this build has a Google OAuth client to connect with. The
    gate for every calendar UI: while it is False, nothing calendar shows."""
    return bool(_client(cfg)[0])


def _scopes(cfg=None):
    """What the sign-in asks for. cfg "google_oauth_scope" wins (testing only:
    try ``SCOPE_EVENTS`` without a code change); "openid email" - non-sensitive,
    only for "Connected as <email>" - is added when the override leaves it out."""
    override = " ".join(str((cfg or {}).get("google_oauth_scope") or "").split())
    if not override:
        return SCOPES
    return override if "openid" in override.split() else "openid email " + override


def granted_ok(scope_text):
    """True when Google's answer grants at least one scope events.list accepts.
    Exact scope words: "calendar.calendarlist.readonly" alone doesn't count."""
    return any(word in EVENT_SCOPES for word in str(scope_text or "").split())


def is_connected():
    try:
        return bool(storage.read_secret(REFRESH_SECRET))
    except Exception:
        return False


# ── sign-in ──────────────────────────────────────────────────────────────────
def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class _Callback(http.server.BaseHTTPRequestHandler):
    """One-shot handler for Google's redirect back to 127.0.0.1."""

    # Per accepted connection: an idle socket (a browser preconnect, a port
    # scan) is dropped after this long instead of pinning the sign-in forever.
    timeout = 5

    def do_GET(self):  # noqa: N802 (http.server API)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in query and "error" not in query:
            self.send_response(404)             # favicon and friends
            self.end_headers()
            return
        # Only Google's redirect carries our state. Anything else - a local
        # process or a web page probing ports with ?error=access_denied - is
        # refused and the real redirect is still waited for.
        expected = getattr(self.server, "expected_state", "") or ""
        got = query.get("state", [""])[0]
        if not expected or not secrets.compare_digest(got.encode(), expected.encode()):
            self.send_response(400)
            self.end_headers()
            return
        res = {k: v[0] for k, v in query.items()}
        self.server.result = res
        finish = getattr(self.server, "finish", None)
        if finish is None:                      # no sign-in behind this listener: the plain page
            body = _OK_HTML if "code" in query else _FAIL_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return
        # The answer is only final once the code is exchanged and kept, so the
        # browser waits here (a few seconds) and is then sent to the page on the
        # Gusan site for what really happened - not left on this 127.0.0.1 address.
        ok = finish(res)
        self.send_response(302)
        self.send_header("Location", CONNECTED_URL if ok else NOT_CONNECTED_URL)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, *args):
        return


class _LoopbackServer(http.server.HTTPServer):
    """The redirect listener, bound exclusively on Windows (as auth.py's):
    stock HTTPServer sets SO_REUSEADDR, which there lets another socket bind
    the same port while it listens. POSIX keeps SO_REUSEADDR (it never shares
    a listening port there)."""

    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _email_from_id_token(id_token):
    """The account email from an OpenID id_token (display only - the token
    came straight from Google over TLS, so it isn't re-verified here)."""
    try:
        payload = (id_token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email") or ""
    except Exception:
        return ""


def _cancelled(cancel):
    return cancel is not None and cancel.is_set()


# Google's sign-in error words -> (what the user reads, the code). Anything else
# shows the word itself, so a rare error can be reported.
_SIGNIN_ERRORS = {
    "access_denied": ("Google sign-in was cancelled.", "denied"),
    "admin_policy_enforced": ("Your Google account's admin doesn't allow this app. "
                              "Try a personal account.", "admin"),
    "invalid_client": ("Google doesn't recognise this app's sign-in (invalid_client).", "client"),
    "redirect_uri_mismatch": ("Google refused this app's sign-in address "
                              "(redirect_uri_mismatch).", "client"),
    "invalid_scope": ("Google refused the calendar permission this app asks for "
                      "(invalid_scope).", "scope"),
}


def _signin_error(word):
    """A CalendarError for a Google sign-in error word (the redirect's ``error``
    or the token endpoint's). The word is shown only when it is a plain token."""
    word = str(word or "")
    if word in _SIGNIN_ERRORS:
        text, code = _SIGNIN_ERRORS[word]
        return CalendarError(text, code)
    safe = _safe_word(word)
    return CalendarError(f"Google sign-in failed ({safe})." if safe else "Google sign-in failed.")


def _safe_word(word):
    """A word from Google's error answer as it may be shown or logged: a plain
    token of letters, digits and _.-, else ""."""
    word = str(word or "")
    return word if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", word) else ""


def _json_dict(response):
    """A response's JSON object, or {} (a body that isn't JSON, or isn't an object)."""
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def connect(cfg=None, open_browser=webbrowser.open, timeout=LOGIN_TIMEOUT, cancel=None):
    """Sign in to Google Calendar in the browser; returns the account email.
    Blocking - run on a worker thread. Raises CalendarError, or
    CalendarCancelled once ``cancel`` (a threading.Event) is set: the local
    listener stops within about a second and nothing is kept."""
    client_id, client_secret = _client(cfg)
    if not client_id:
        raise CalendarError("Calendar connection isn't set up in this version yet.", "client")
    if _cancelled(cancel):
        raise CalendarCancelled("Sign-in cancelled.")
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(24)
    try:
        server = _LoopbackServer(("127.0.0.1", 0), _Callback)
    except OSError:
        logger.warning("Calendar sign-in listener failed to start", exc_info=True)
        raise CalendarError("Couldn't start the sign-in on this computer - try again.", "port_busy")
    server.timeout = 1                        # handle_request() slice: cancel is seen within ~1 s
    server.result = None
    server.outcome = None                     # ("ok", email) or ("err", CalendarError), set by finish()
    server.expected_state = state
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": _scopes(cfg), "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "access_type": "offline", "prompt": "consent",
    })

    def complete(res):
        """Everything after Google's redirect: the checks, the code exchange and
        keeping the token. Returns the account email; raises CalendarError."""
        if _cancelled(cancel):
            raise CalendarCancelled("Sign-in cancelled.")
        if res.get("state") != state:
            raise CalendarError("Sign-in failed a security check - try again.")
        if res.get("error"):
            raise _signin_error(res["error"])
        data = {"code": res.get("code", ""), "client_id": client_id, "redirect_uri": redirect,
                "grant_type": "authorization_code", "code_verifier": verifier}
        if client_secret:
            data["client_secret"] = client_secret
        try:
            r = requests.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
        except requests.RequestException:
            logger.info("Calendar sign-in: token exchange failed", exc_info=True)
            raise CalendarError(OFFLINE_TEXT, "offline")
        if r.status_code != 200:
            word = _safe_word(_json_dict(r).get("error"))
            logger.info("Calendar sign-in: token exchange answered HTTP %s (%s)", r.status_code, word)
            if word in _SIGNIN_ERRORS:
                raise _signin_error(word)
            raise CalendarError(f"Google refused the sign-in (HTTP {r.status_code}).")
        try:
            tok = r.json()
        except ValueError:
            tok = None
        if not isinstance(tok, dict):
            raise CalendarError("Google sent an answer that couldn't be read - try again.")
        if not granted_ok(tok.get("scope")):
            # Google shows each permission as its own checkbox. Nothing here can
            # use a grant without the calendar box: give it back at once.
            revoke(tok.get("refresh_token") or tok.get("access_token") or "")
            raise CalendarError(NOT_GRANTED_TEXT, "scope")
        if not tok.get("refresh_token"):
            raise CalendarError("Google didn't return lasting access - try again.")
        if _cancelled(cancel):
            # Cancelled while the code was being exchanged: don't keep it.
            revoke(tok["refresh_token"])
            raise CalendarCancelled("Sign-in cancelled.")
        if not storage.write_secret(REFRESH_SECRET, tok["refresh_token"]):
            # Nowhere to keep it: don't report "connected" and go quiet an hour
            # later - give the grant back and say why.
            revoke(tok["refresh_token"])
            raise CalendarError("Couldn't save calendar access on this computer - "
                                "no system keyring is available.", "keyring")
        _cache_token(tok)
        return _email_from_id_token(tok.get("id_token"))

    def finish(res):
        """Run complete() for the listener's handler; True when it worked."""
        try:
            server.outcome = ("ok", complete(res))
        except CalendarError as e:
            server.outcome = ("err", e)
        except Exception:
            logger.warning("Calendar sign-in failed", exc_info=True)
            server.outcome = ("err", CalendarError("Google sign-in failed - try again."))
        return server.outcome[0] == "ok"

    server.finish = finish
    try:
        if open_browser(url) is False:          # webbrowser.open: no browser could start
            raise CalendarError("Couldn't open your web browser to sign in to Google.")
        deadline = time.time() + timeout
        while server.result is None and time.time() < deadline and not _cancelled(cancel):
            server.handle_request()
    finally:
        server.server_close()

    outcome = server.outcome
    if outcome is None:                         # Google's redirect never arrived
        if _cancelled(cancel):
            raise CalendarCancelled("Sign-in cancelled.")
        raise CalendarError("Sign-in timed out - try again.", "timeout")
    kind, value = outcome
    if kind == "err":
        raise value
    return value


def forget_local():
    """Forget the calendar on this computer right away (keyring + memory).
    Returns the refresh token that was kept ("" if none), so the caller can
    :func:`revoke` it at Google off the GUI thread. No network.

    The token moves to the "pending revoke" slot first and leaves it only when
    Google has answered :func:`revoke`: a Disconnect made offline (or a quit
    right after it) is finished by :func:`retry_pending_revoke` on a later start."""
    refresh = ""
    try:
        refresh = storage.read_secret(REFRESH_SECRET) or ""
        if refresh:
            storage.write_secret(PENDING_REVOKE_SECRET, refresh)
        storage.write_secret(REFRESH_SECRET, "")
    except Exception:
        pass
    with _token_lock:
        _token.update(value="", expires=0.0)
    return refresh


def revoke(refresh_token):
    """Give the app's access back at Google (blocking). True when there is
    nothing left to give back: Google answered (it also answers 400 for a
    token that is already dead). False when it couldn't be reached or was busy
    (offline, 429, 5xx): the token then waits in the "pending revoke" slot for
    :func:`retry_pending_revoke`. The token goes in the request body, never the
    URL (proxies and debug logs record URLs)."""
    if not refresh_token:
        return True
    try:
        r = requests.post(REVOKE_URL, data={"token": refresh_token}, timeout=HTTP_TIMEOUT)
        status = r.status_code
        done = not (status == 429 or status >= 500)
    except requests.RequestException:
        logger.debug("Calendar revoke failed", exc_info=True)
        done = False
    _set_pending_revoke(refresh_token, keep=not done)
    return done


def _set_pending_revoke(token, keep):
    """Keep ``token`` in the pending slot (``keep``) or clear the slot when it
    holds that token. Never raises."""
    try:
        held = storage.read_secret(PENDING_REVOKE_SECRET) or ""
        if keep and held != token:
            storage.write_secret(PENDING_REVOKE_SECRET, token)
        elif not keep and held == token:
            storage.write_secret(PENDING_REVOKE_SECRET, "")
    except Exception:
        logger.debug("Couldn't update the pending calendar revoke", exc_info=True)


def has_pending_revoke():
    """True while a Disconnect's give-back to Google is still to be done."""
    try:
        return bool(storage.read_secret(PENDING_REVOKE_SECRET))
    except Exception:
        return False


def retry_pending_revoke():
    """Finish a give-back that a Disconnect couldn't (offline, Google busy, the
    app quit first). Blocking, one request: run it on a worker thread. True when
    nothing is left to do. A token that is the one now connected is only dropped
    from the slot, never revoked."""
    try:
        token = storage.read_secret(PENDING_REVOKE_SECRET) or ""
        if not token:
            return True
        if token == (storage.read_secret(REFRESH_SECRET) or ""):
            storage.write_secret(PENDING_REVOKE_SECRET, "")
            return True
    except Exception:
        return True
    return revoke(token)


def disconnect():
    """Forget the calendar here and revoke the app's access at Google."""
    return revoke(forget_local())


# ── access token (memory only) ───────────────────────────────────────────────
_token = {"value": "", "expires": 0.0}
_token_lock = threading.Lock()


def _cache_token(tok):
    with _token_lock:
        _token["value"] = tok.get("access_token") or ""
        _token["expires"] = time.time() + float(tok.get("expires_in") or 3600) - 60


def _access_token(cfg=None):
    with _token_lock:
        if _token["value"] and time.time() < _token["expires"]:
            return _token["value"]
    refresh = storage.read_secret(REFRESH_SECRET) or ""
    if not refresh:
        raise CalendarAuthError("Calendar isn't connected.")
    client_id, client_secret = _client(cfg)
    if not client_id:
        # Refreshing with an empty client id gets a 400 that is not a revoke.
        raise CalendarError("Calendar connection isn't set up in this version yet.", "client")
    data = {"client_id": client_id, "refresh_token": refresh, "grant_type": "refresh_token"}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        logger.debug("Calendar token refresh failed", exc_info=True)
        raise CalendarError(OFFLINE_TEXT, "offline")
    if r.status_code in (400, 401):
        err = _safe_word(_json_dict(r).get("error"))
        if err == "invalid_grant":
            # Revoked, the password changed, or - while the Google app is
            # still in "Testing" - the 7-day token lifetime ran out.
            storage.write_secret(REFRESH_SECRET, "")
            raise CalendarAuthError(EXPIRED_TEXT)
        # invalid_client / invalid_request: our side is misconfigured - the
        # user's grant is fine, so keep it.
        raise CalendarError(f"Google refused the calendar refresh ({err or 'HTTP ' + str(r.status_code)}).",
                            "client" if err == "invalid_client" else None)
    if r.status_code == 429:
        raise CalendarApiError(QUOTA_TEXT, "quota", 429)
    if r.status_code >= 500:
        raise CalendarApiError(SERVER_TEXT, "server", r.status_code)
    if r.status_code != 200:
        raise CalendarError(f"Google returned HTTP {r.status_code}.")
    try:
        tok = r.json()
    except ValueError:
        tok = None
    if not isinstance(tok, dict) or not tok.get("access_token"):
        raise CalendarError("Google sent an answer that couldn't be read.")
    _cache_token(tok)
    with _token_lock:
        return _token["value"]


# ── events ───────────────────────────────────────────────────────────────────
def _rfc3339(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_name_from_email = invite_context.name_from_email
# Shared calendars and rooms are on an invite as attendees too; they aren't people.
_GROUP_ADDRESS_RE = re.compile(r"@(?:group|resource)\.calendar\.google\.com\s*$", re.IGNORECASE)


def clean_person_name(name):
    """A person's name as the app keeps it: one line (no control characters), at
    most 80 characters, no email. "Smith, John" (one comma, no brackets) is
    "John Smith"; any other comma or semicolon is dropped, since the meeting's
    people field would read it as two people (meeting_store.split_attendees)."""
    text = invite_context.one_line(name)
    if (text.count(",") == 1 and "@" not in text
            and not re.search(r"[()\[\]<>{};]", text)):
        last, first = (part.strip() for part in text.split(","))
        if last and first and len(last.split()) <= 3 and len(first.split()) <= 3:
            text = first + " " + last
    return invite_context.clean_name(text)


def attendee_names(attendees):
    """People on the invite (you included - notes attribute owners by name),
    display name or a readable email; meeting rooms and shared calendars are
    left out. No email ever comes out of this: a name made of an address is cut
    to the part before the "@"."""
    names, seen = [], set()
    for a in attendees or []:
        if not isinstance(a, dict) or a.get("resource"):
            continue
        email = str(a.get("email") or "")
        if _GROUP_ADDRESS_RE.search(email):
            continue
        name = clean_person_name(a.get("displayName")) or clean_person_name(_name_from_email(email))
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def cap_names(names, limit=MAX_PEOPLE):
    """The first ``limit`` distinct clean names of a list (or a comma string)."""
    return invite_context.cap_names(names, limit)


def _short_name(name):
    """First name for the compact label; the full name when the first word is
    a title or an initial ("Dr. Smith", "J. Doe")."""
    words = name.split()
    first = words[0] if words else name
    return name if len(first) < 2 or first.endswith(".") else first


def people_label(attendees, max_names=2):
    """'Anna, David +2': the first ``max_names`` people by first name, then
    how many more. Takes names (meeting["others"]) or Google attendee dicts.
    Full names when two first names would read the same. "" for nobody."""
    names = []
    for a in attendees or []:
        if isinstance(a, dict):
            names.extend(n for n in attendee_names([a]) if n not in names)
        elif isinstance(a, str) and a.strip() and a.strip() not in names:
            names.append(a.strip())
    if not names:
        return ""
    shown = [_short_name(n) for n in names[:max_names]]
    if len(set(shown)) < len(shown):
        shown = names[:max_names]
    extra = len(names) - len(shown)
    return ", ".join(shown) + (f" +{extra}" if extra > 0 else "")


# Call links people paste into the location or the description, besides the
# ones Google attaches (hangoutLink / conferenceData). A link must start at a
# scheme or on its own (not inside another host or path), so
# "https://evil.example/meet.google.com/x" or "notzoom.us/j/1" never match.
_CALL_LINK_RE = re.compile(
    r"(?:\bhttps?://|(?<![\w@./:%-]))"
    r"(meet\.google\.com/[\w-]+"
    r"|(?:[a-z0-9-]+\.)*zoom\.us/(?:j|my|w)/[\w.-]+"
    r"|teams\.microsoft\.com/(?:l/meetup-join|meet)/"
    r"|teams\.live\.com/"
    r"|(?!(?:www|help)\.)(?:[a-z0-9-]+\.)+webex\.com/)"
    r"[^\s<>\"'()\[\]{}|\\^`]*",
    re.IGNORECASE)
_SCAN_LIMIT = 20000


def find_call_link(text):
    """The first Google Meet / Zoom / Teams / Webex join link in free text
    (HTML descriptions included), as an https URL; "" if there is none."""
    if not text or not isinstance(text, str):
        return ""
    m = _CALL_LINK_RE.search(html.unescape(text[:_SCAN_LIMIT]))
    if not m:
        return ""
    return "https://" + m.group(0)[m.start(1) - m.start(0):].rstrip(".,;:!?")


def _host(link):
    try:
        return (urllib.parse.urlsplit(link).hostname or "").lower()
    except ValueError:
        return ""


def call_type(link, conference=None):
    """The call's service for a join link: one of CALL_TYPES (from the
    link's host, else the name Google gives the conference) or "other";
    "" when the meeting has no link."""
    if not link:
        return ""
    host = _host(link)
    if host == "meet.google.com":
        return "Google Meet"
    if host == "zoom.us" or host.endswith(".zoom.us"):
        return "Zoom"
    if host in ("teams.microsoft.com", "teams.live.com"):
        return "Teams"
    if host == "webex.com" or host.endswith(".webex.com"):
        return "Webex"
    solution = conference.get("conferenceSolution") if isinstance(conference, dict) else None
    name = str(solution.get("name") or "").lower() if isinstance(solution, dict) else ""
    for key, label in (("meet", "Google Meet"), ("zoom", "Zoom"), ("teams", "Teams"), ("webex", "Webex")):
        if key in name:
            return label
    return CALL_OTHER


def _video_entry(conference):
    for e in (conference or {}).get("entryPoints") or []:
        if isinstance(e, dict) and e.get("entryPointType") == "video":
            uri = str(e.get("uri") or "").strip()
            if uri.lower().startswith(("https://", "http://")):
                return uri
    return ""


# ── the invite's description as a short, safe agenda ─────────────────────────
# The description is written by whoever made the invite, so it is untrusted text
# that can carry join links with passcodes, dial-in numbers and email boilerplate.
# What is kept is only the human part, plain and short (invite_context frames it
# as data wherever it reaches a model). Every pattern here is bounded, so a huge
# or hostile description can't stall the worker thread.
_AGENDA_SCAN = 20000
_LI_RE = re.compile(r"<li\b[^<>]{0,2000}>", re.IGNORECASE)
_BLOCK_RE = re.compile(r"</?(?:br|p|div|ul|ol|li|tr|table|blockquote|section|article|"
                       r"h[1-6])\b[^<>]{0,2000}>", re.IGNORECASE)
_TAG_RE = re.compile(r"</?[a-zA-Z][^<>]{0,2000}>|<!--.{0,2000}?-->", re.DOTALL)
_URL_RE = re.compile(
    r"(?:\b(?:https?|ftp)://|\bwww\.)[^\s<>\"']+"
    r"|\b(?:mailto|tel|sms):[^\s<>\"']+"
    r"|\b(?:[a-z0-9-]{1,63}\.){1,6}[a-z]{2,24}(?::\d{1,5})?/[^\s<>\"']*"
    r"|\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5}(?:/[^\s<>\"']*)?|/[^\s<>\"']*)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]{1,64}@(?:[\w-]{1,63}\.){1,6}[\w-]{2,24}")
_PHONE_RE = re.compile(
    r"(?<![\w.])(?:\+\d[\d\s().-]{6,25}\d"
    r"|\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}"
    r"|\d{3}[\s.-]\d{3,4}[\s.-]\d{3,4})(?!\w)")
_EMPTY_BRACKETS_RE = re.compile(r"[(\[{<]\s*[)\]}>]")
# Lines that are the conferencing service's or the calendar's own text.
_BOILERPLATE_RE = re.compile(
    r"^(?:dial\b|one[ -]tap\b|join\s+(?:with|by|zoom|microsoft|on\s+a\s+video)\b"
    r"|join\s+the\s+(?:zoom\s+|teams\s+|webex\s+|google\s+meet\s+)?(?:meeting|call)\b"
    r"|find\s+(?:your|a)\s+local\s+number|more\s+phone\s+numbers|meeting\s+link\b"
    r"|microsoft\s+teams\s+(?:meeting|need\s+help)|need\s+help\b|for\s+organi[sz]ers\b"
    r"|tenant\s+key\b|video\s+id\b|system\s+reference\b|meeting\s+options\b|more\s+info\s*$"
    r"|invitation\s+from\s+google\s+calendar|forwarding\s+this\s+invitation"
    r"|to\s+stop\s+receiving|reply\s+for\b|view\s+all\s+guest\s+info"
    r"|you\s+are\s+receiving\s+this|learn\s+more\s+about\s+(?:google\s+)?meet"
    r"|please\s+do\s+not\s+edit\s+this\s+section)", re.IGNORECASE)
# "Meeting ID: 812 3456 7890", "Passcode: 123456", "PIN: 886 236 346#" - anywhere.
# Without the colon only when a token with a digit follows ("Passcode 849302",
# "The password is hunter2"), so "Password rotation policy" stays.
_CREDENTIAL_RE = re.compile(
    r"\b(?:(?:meeting|webinar|conference|participant|access|host)\s*(?:id|code|number|key)"
    r"|pass\s?code|password|pin)\b(?:\s*[:#=]|(?:\s+is)?\s+\S*\d)", re.IGNORECASE)


def plain_agenda(description, limit=AGENDA_CHARS):
    """An invite's description as a short plain-text agenda: HTML turned into
    text (line breaks for br / p / li / div, list items as "- "), every link and
    email address removed, the conferencing and calendar boilerplate dropped
    (Meeting ID, passcode, dial-in lines, "Join with Google Meet", ...), phone
    numbers removed, and the rest cut at ``limit`` characters on a word boundary
    with "…". "" when nothing human is left. The text is untrusted: it only
    stays text here, and invite_context frames it as data for every model."""
    text = description if isinstance(description, str) else ""
    text = text[:_AGENDA_SCAN]
    if "<" in text:
        text = _LI_RE.sub("\n- ", text)
        text = _BLOCK_RE.sub("\n", text)
        text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    rows = []
    for raw in text.splitlines():
        row = invite_context.one_line(raw)
        if not row:
            continue
        linked = bool(_URL_RE.search(row) or _EMAIL_RE.search(row))
        row = _EMAIL_RE.sub(" ", _URL_RE.sub(" ", row))
        lead = re.sub(r"^[\W_]+", "", row)
        if _BOILERPLATE_RE.match(lead) or _CREDENTIAL_RE.search(row):
            continue
        phone = _PHONE_RE.search(row)
        if phone and len(row[:phone.start()].split()) <= 2:
            continue                                 # a dial-in listing: "(US) +1 346 ..."
        row = _PHONE_RE.sub(" ", row)
        row = " ".join(_EMPTY_BRACKETS_RE.sub(" ", row).split())
        if not re.search(r"[^\W_]", row):
            continue                                 # nothing but punctuation or rules
        if linked and row.endswith(":") and len(row.split()) <= 4:
            continue                                 # "Slides:" - what it pointed at is gone
        rows.append(row)
    return invite_context.cut_text("\n".join(rows), limit)


def parse_event(item):
    """A Google event as a meeting dict, or None when it isn't a meeting you'd
    record: all-day, cancelled, declined by you, focus time / out of office,
    birthdays, events from Gmail.

    Keys: id, title, start, end (aware datetimes), attendees (you included),
    others (without you), people ("Anna, David +2"), link (join URL or ""),
    call_type ("Google Meet" / "Zoom" / "Teams" / "Webex" / "other" / ""),
    recurring_id (the series id or ""), location, agenda (the description as
    short plain text, "" for a private or confidential event), has_others
    (someone besides you is on it, or may be: a hidden guest list, or an invite
    from someone else), private (visibility private / confidential).

    No email address, and nothing of the description besides ``agenda``, is
    kept in the result."""
    if not isinstance(item, dict) or item.get("status") == "cancelled":
        return None
    if item.get("eventType") in _SKIP_EVENT_TYPES:
        return None
    start, end = item.get("start") or {}, item.get("end") or {}
    if not start.get("dateTime") or not end.get("dateTime"):
        return None                                   # all-day
    attendees = item.get("attendees") or []
    me = next((a for a in attendees if isinstance(a, dict) and a.get("self")), None)
    if me and me.get("responseStatus") == "declined":
        return None
    try:
        st, en = _parse_dt(start["dateTime"]), _parse_dt(end["dateTime"])
    except (ValueError, TypeError, AttributeError):
        return None
    if st.tzinfo is None or en.tzinfo is None:
        return None                                   # Google always sends an offset
    location = str(item.get("location") or "").strip()
    conference = item.get("conferenceData") if isinstance(item.get("conferenceData"), dict) else {}
    link = (str(item.get("hangoutLink") or "").strip() or _video_entry(conference)
            or find_call_link(location) or find_call_link(item.get("description")))
    others = attendee_names([a for a in attendees if not (isinstance(a, dict) and a.get("self"))])
    private = str(item.get("visibility") or "") in ("private", "confidential")
    organizer = item.get("organizer")
    organizer_is_you = isinstance(organizer, dict) and bool(organizer.get("self"))
    return {
        "id": item.get("id") or f"{st.isoformat()}|{item.get('summary', '')}",
        "title": invite_context.one_line(item.get("summary"), MAX_TITLE_CHARS) or "Untitled meeting",
        "start": st,
        "end": en,
        "attendees": attendee_names(attendees),
        "others": others,
        "people": people_label(others),
        "link": link or "",
        "call_type": call_type(link, conference),
        "recurring_id": str(item.get("recurringEventId") or ""),
        "location": location[:300],
        "agenda": "" if private else plain_agenda(item.get("description")),
        # Someone besides you is on it - or may be: Google leaves the guest list
        # out of a hidden-list invite, so an invite from someone else counts.
        "has_others": bool(others) or bool(item.get("attendeesOmitted")) or not organizer_is_you,
        "private": private,
    }


# Google's error words (errors[0].reason, error.status, details[].reason).
_SCOPE_REASONS = {"insufficientpermissions", "access_token_scope_insufficient"}
_API_OFF_REASONS = {"accessnotconfigured", "service_disabled"}
_QUOTA_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded",
                  "dailylimitexceeded", "calendarusagelimitsexceeded",
                  "rate_limit_exceeded", "resource_exhausted"}


def _error_words(body):
    """Google's error words from an error answer (a JSON text or an already
    parsed object), plain tokens only, the first being the main reason."""
    if isinstance(body, (str, bytes)):
        try:
            body = json.loads(body)
        except ValueError:
            body = {}
    err = body.get("error") if isinstance(body, dict) else None
    raw = []
    if isinstance(err, str):
        raw.append(err)
    elif isinstance(err, dict):
        for e in list(err.get("errors") or [])[:10]:
            raw.append(e.get("reason") if isinstance(e, dict) else None)
        raw.append(err.get("status"))
        for d in list(err.get("details") or [])[:10]:
            raw.append(d.get("reason") if isinstance(d, dict) else None)
    return [w for w in (_safe_word(x) for x in raw) if w]


def _api_error(status, body_text=""):
    """The CalendarError for a Google Calendar answer that isn't events: a
    CalendarAuthError for a 401 (after the refresh) and for a missing calendar
    permission, else a CalendarApiError whose ``code`` is "api_off", "quota",
    "server" or "other". ``status`` and ``reason`` are set on it (for the log)."""
    words = _error_words(body_text)
    low = {w.lower() for w in words}
    if status == 401:
        err = CalendarAuthError(EXPIRED_TEXT)
    elif low & _SCOPE_REASONS:
        err = CalendarAuthError(SCOPE_TEXT, "scope")
    elif low & _API_OFF_REASONS:
        err = CalendarApiError(API_OFF_TEXT, "api_off")
    elif status == 429 or low & _QUOTA_REASONS:
        err = CalendarApiError(QUOTA_TEXT, "quota")
    elif status >= 500:
        err = CalendarApiError(SERVER_TEXT, "server")
    else:
        err = CalendarApiError(f"Google Calendar returned HTTP {status}.")
    err.status, err.reason = status, (words[0] if words else "")
    return err


def _error_body(response):
    text = getattr(response, "text", None)
    return text if isinstance(text, str) and text else _json_dict(response)


def _drop_grant():
    """Access is gone for good (no calendar permission, or Google keeps refusing
    a fresh token): forget it here - the service sees that the keyring is empty
    and disconnects ("expired") - and give it back at Google. Best effort."""
    try:
        revoke(forget_local())
    except Exception:
        logger.debug("Couldn't give the calendar grant back", exc_info=True)


def _get_events(cfg, params):
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {_access_token(cfg)}"}
        try:
            r = requests.get(EVENTS_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException:
            logger.debug("Calendar events request failed", exc_info=True)
            raise CalendarError(CALENDAR_OFFLINE_TEXT, "offline")
        if r.status_code == 401 and attempt == 1:
            with _token_lock:                          # stale access token: refresh once
                _token.update(value="", expires=0.0)
            continue
        if r.status_code != 200:
            err = _api_error(r.status_code, _error_body(r))
            # The status and Google's reason word only - never a token or a body.
            logger.warning("Calendar events request failed: HTTP %s, reason %s",
                           err.status, err.reason or "-")
            if isinstance(err, CalendarAuthError):
                _drop_grant()
            raise err
        try:
            data = r.json()
        except ValueError:
            raise CalendarError("Google Calendar sent an answer that couldn't be read.")
        return data if isinstance(data, dict) else {}
    return {}


def window_hours(now, tz=None, lo=WINDOW_MIN_H, hi=WINDOW_MAX_H):
    """How many hours ahead "Coming up" reads: from ``now`` to the end of
    tomorrow in local time (the start of the day after tomorrow), clamped to
    [``lo``, ``hi``] hours - so the "Today" / "Tomorrow" headers mean something,
    and a day with a clock change can't make the window odd. ``tz`` is a tzinfo
    (default: this computer's time zone)."""
    day = now.astimezone(tz).date() + timedelta(days=2)
    if tz is not None:
        end = datetime.combine(day, dt_time.min, tzinfo=tz)
    else:
        end = datetime.combine(day, dt_time.min).astimezone()     # system local time
    return max(lo, min(hi, (end - now).total_seconds() / 3600))


def upcoming(cfg=None, hours=None, past_minutes=PAST_MINUTES, now=None, tz=None):
    """Meetings from ``past_minutes`` ago to ``hours`` ahead (default:
    :func:`window_hours`, the end of tomorrow in ``tz`` / local time), soonest
    first. Blocking - run on a worker thread. Raises CalendarError (a
    CalendarApiError with ``code`` "quota" / "server" / "api_off" when Google
    answered badly), or CalendarAuthError when access is gone."""
    now = now or datetime.now(timezone.utc)
    hours = window_hours(now, tz) if hours is None else hours
    params = {"timeMin": _rfc3339(now - timedelta(minutes=past_minutes)),
              "timeMax": _rfc3339(now + timedelta(hours=hours)),
              "singleEvents": "true", "orderBy": "startTime", "maxResults": str(PAGE_SIZE),
              "eventTypes": EVENT_TYPES, "fields": EVENT_FIELDS}
    items, tokens = [], set()
    for _page in range(MAX_PAGES):
        data = _get_events(cfg, params)
        page = data.get("items")
        if isinstance(page, list):
            items.extend(page)
        page_token = data.get("nextPageToken")
        if not page_token or not isinstance(page_token, str) or page_token in tokens:
            break
        tokens.add(page_token)
        params = dict(params, pageToken=page_token)
    meetings = []
    for item in items:
        try:
            m = parse_event(item)
        except Exception:
            # One odd event must not blank the whole "Coming up" list.
            logger.debug("Skipped a calendar event that couldn't be read", exc_info=True)
            continue
        if m:
            meetings.append(m)
    return meetings


# ── timing (pure) ────────────────────────────────────────────────────────────
def should_prompt(meeting, now, lead_seconds=REMIND_LEAD_S, grace_seconds=REMIND_GRACE_S):
    """Ask from a minute before the start until five minutes in - not for a
    meeting that's well underway or over."""
    start, end = meeting["start"], meeting["end"]
    return (start - timedelta(seconds=lead_seconds) <= now
            <= min(end, start + timedelta(seconds=grace_seconds)))


def current_meeting(meetings, now, lead_seconds=300):
    """The meeting happening now (or starting within ``lead_seconds``), for
    filling in the Record Meeting form. None if there isn't one."""
    live = [m for m in meetings
            if m["start"] - timedelta(seconds=lead_seconds) <= now < m["end"]]
    return min(live, key=lambda m: abs((m["start"] - now).total_seconds())) if live else None


def is_meeting(m):
    return (isinstance(m, dict) and isinstance(m.get("start"), datetime)
            and isinstance(m.get("end"), datetime) and m.get("id"))


def worth_asking(m, only_meetings):
    """Does this meeting count for the list and the reminders? With
    ``only_meetings`` ("Only meetings with other people", on by default) it
    must have other people on it (``has_others``: see parse_event); without it
    every event parse_event kept counts - a call link says nothing, since
    Workspace puts a Meet link on nearly every solo event. A meeting dict
    without ``has_others`` falls back to whether it lists other people."""
    if not isinstance(m, dict):
        return False
    if not only_meetings:
        return True
    if "has_others" in m:
        return bool(m["has_others"])
    return bool(m.get("others"))


def reminder_due(meetings, now, muted=(), prompted=(), only_meetings=False,
                 lead_seconds=REMIND_LEAD_S, grace_seconds=REMIND_GRACE_S):
    """The meeting to ask "record this meeting?" about right now, or None.

    Skips muted ids (the row's bell is off), ids already asked about, and -
    with ``only_meetings`` - meetings with nobody else on them
    (:func:`worth_asking`). ``muted`` and ``prompted`` are any containers of
    event ids (sets, or dicts keyed by id). When several are due (overlapping
    meetings) the one whose start is closest to now wins; the next one is due
    on the next check. No network."""
    muted = set(muted or ())
    prompted = set(prompted or ())
    due = [m for m in meetings or ()
           if is_meeting(m) and m["id"] not in muted and m["id"] not in prompted
           and worth_asking(m, only_meetings)
           and should_prompt(m, now, lead_seconds, grace_seconds)]
    if not due:
        return None
    return min(due, key=lambda m: (abs((m["start"] - now).total_seconds()), m["start"]))


def visible_upcoming(meetings, now, only_meetings=False):
    """The "Coming up" rows: meetings that haven't ended yet (upcoming() also
    returns the last hour), soonest first, each id once; with ``only_meetings``
    only those with other people on them (:func:`worth_asking`)."""
    seen, out = set(), []
    for m in sorted((m for m in meetings or () if is_meeting(m)),
                    key=lambda m: (m["start"], m["end"], m.get("title") or "")):
        if m["end"] <= now or m["id"] in seen:
            continue
        if not worth_asking(m, only_meetings):
            continue
        seen.add(m["id"])
        out.append(m)
    return out


def next_fetch_delay(now, meetings, failures=0, rand=random.random):
    """Seconds until the calendar should be read again.

    FETCH_FAST_S (5 min) while a meeting is running or starts within 2 hours -
    and when there is none at all, since an empty calendar is where a meeting
    added at the last minute would otherwise be missed - else FETCH_IDLE_S
    (15 min). Each failed read in a row (``failures``) doubles it, up to
    FETCH_BACKOFF_CAP_S. +-25 % jitter (``rand`` gives 0..1) keeps many
    computers from reading together. One exception, no jitter: the read just
    before the next start (FETCH_PRE_MEETING_S before it), so a meeting
    cancelled or moved a few minutes ago is caught before its reminder."""
    live = [m for m in meetings or () if is_meeting(m) and m["end"] > now]
    busy = (not live or any(m["start"] <= now for m in live)
            or any((m["start"] - now).total_seconds() <= FETCH_NEAR_S for m in live))
    delay = FETCH_FAST_S if busy else FETCH_IDLE_S
    delay = min(delay * 2 ** min(max(0, int(failures or 0)), 10), FETCH_BACKOFF_CAP_S)
    try:
        luck = min(1.0, max(0.0, float(rand())))
    except (TypeError, ValueError):
        luck = 0.5
    delay *= 1 + FETCH_JITTER * (2 * luck - 1)
    ahead = [(m["start"] - now).total_seconds() for m in live if m["start"] > now]
    if ahead:
        pre = min(ahead) - FETCH_PRE_MEETING_S
        if 1 <= pre < delay:
            return float(pre)
    return float(delay)


def start_context(meeting, with_agenda=True):
    """What a recording or Live Assistant session starts with, from a meeting
    dict: {"title", "attendees" (people on the invite, you included, at most
    MAX_PEOPLE), "others", "agenda", "event_id"} - clean text, no emails, no
    links (:func:`invite_context.from_meeting`). ``with_agenda`` False sends
    the title and names only (a plain Record from the reminder toast)."""
    return invite_context.from_meeting(meeting, with_agenda)


def when_parts(meeting, now, tz=None):
    """What a "Coming up" row shows for its time, in local time (or ``tz``):
    {"time": "14:30", "rel": "in 1 min" | "Now" | "", "day": "" | "Tomorrow" | "Thu"}.
    "rel" is only set within the hour before the start, or while it runs."""
    start = meeting["start"].astimezone(tz)
    today = now.astimezone(tz).date()
    if start.date() == today:
        day = ""
    elif start.date() == today + timedelta(days=1):
        day = "Tomorrow"
    else:
        day = start.strftime("%a")
    secs = (meeting["start"] - now).total_seconds()
    if meeting["start"] <= now < meeting["end"]:
        rel = "Now"
    elif 0 < secs <= 3600:
        rel = f"in {max(1, math.ceil(secs / 60))} min"
    else:
        rel = ""
    return {"time": start.strftime("%H:%M"), "rel": rel, "day": day}


def day_word(meeting, now, tz=None):
    """"Today", "Tomorrow" or "Thursday": the day a meeting is on, in local time (or
    ``tz``). One that began yesterday and still runs counts as today. Anything that
    can't be read says "Today"."""
    try:
        start = meeting["start"].astimezone(tz)
        today = now.astimezone(tz).date()
    except Exception:
        return "Today"
    if start.date() <= today:
        return "Today"
    if start.date() == today + timedelta(days=1):
        return "Tomorrow"
    return start.strftime("%A")


def call_label(kind):
    """What the meta line calls the call service: "" without a link, "Video call"
    for a link of no known service (``CALL_OTHER``), else the service's own name."""
    if not kind:
        return ""
    return "Video call" if kind == CALL_OTHER else str(kind)


def join_url(meeting_or_link):
    """The link a Join button may open: an ``https://`` address (any case of the
    scheme, spaces around it dropped) with a host and no spaces inside; "" for
    anything else (http, javascript:, file:, empty, missing). Takes a meeting
    dict (its ``link``) or the link itself."""
    link = meeting_or_link.get("link") if isinstance(meeting_or_link, dict) else meeting_or_link
    link = str(link or "").strip()
    if not link.lower().startswith("https://"):
        return ""
    if any(ch.isspace() or ord(ch) < 32 for ch in link):
        return ""
    try:
        return link if urllib.parse.urlsplit(link).hostname else ""
    except ValueError:
        return ""


def upcoming_facts(meeting, now, tz=None):
    """Everything a one-line "Coming up" row says about a meeting, as plain strings
    (never markup): {"id", "title" ("Untitled meeting" when it has none), "time"
    "14:30", "rel" ("Now" | "in 25 min" | ""), "label" (rel, else the day word:
    Today / Tomorrow / Thursday), "hot" (it is on now), "people" ("Anna, David
    +2"), "names" (everyone else on it), "call_type", "call" ("Google Meet" /
    "Video call" / ""), "meta" ("Google Meet · Anna, David +2"), "join" (see
    :func:`join_url`), "name" (the row for a screen reader: "Sync, 14:30, in 25
    min, with Anna, David")}."""
    m = meeting if isinstance(meeting, dict) else {}
    parts = when_parts(m, now, tz)
    rel = parts["rel"]
    title = str(m.get("title") or "").strip() or "Untitled meeting"
    names = [str(n) for n in (m.get("others") or []) if n]
    people = str(m.get("people") or "") or people_label(m.get("others"))
    call_type = str(m.get("call_type") or "")
    call = call_label(call_type)
    label = rel or day_word(m, now, tz)
    return {
        "id": str(m.get("id") or ""), "title": title, "time": parts["time"], "rel": rel, "label": label,
        "hot": rel == "Now", "people": people, "names": names, "call_type": call_type, "call": call,
        "meta": " · ".join(x for x in (call, people) if x), "join": join_url(m),
        "name": "%s, %s, %s%s" % (title, parts["time"], label, (", with " + people) if people else ""),
    }


def reminder_text(meeting, now):
    """The reminder's timing line: "Starts in 1 min", "Starting now" or
    "Started 2 min ago"."""
    secs = (meeting["start"] - now).total_seconds()
    if secs > 0:
        return f"Starts in {max(1, math.ceil(secs / 60))} min"
    ago = int(-secs // 60)
    return "Starting now" if ago < 1 else f"Started {ago} min ago"


# ── the founder's local setup (the secret never goes through a chat) ─────────
def set_client_cli(input_fn=input, secret_fn=getpass.getpass, cfg_path=None):
    """Save the Google OAuth client ID and secret in this computer's config.json
    (``google_oauth_client_id`` / ``google_oauth_client_secret``, the keys that
    override the constants), asking for them in this terminal: the secret is
    typed hidden, and neither value is ever printed or logged. Other keys of
    the file are kept. Close Gusan first - it rewrites config.json while it
    runs. Returns True when saved. Run: ``python calendar_sync.py``."""
    print("Close Gusan first - it rewrites its settings while it runs.")
    client_id = str(input_fn("Google client ID: ") or "").strip()
    secret = str(secret_fn("Google client secret (typing is hidden): ") or "").strip()
    if not client_id:
        print("No client ID - nothing saved.")
        return False
    path = Path(cfg_path) if cfg_path else storage.path_for("config.json")
    data = storage.read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    data["google_oauth_client_id"] = client_id
    data["google_oauth_client_secret"] = secret
    try:
        storage.atomic_write_json(path, data)
    except OSError:
        print("Couldn't save the settings file.")
        return False
    print("Saved.")
    return True


if __name__ == "__main__":
    set_client_cli()
