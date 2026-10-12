"""Google Calendar for meeting recording - read-only, and straight between
Google and this computer (never through our server).

* :func:`connect`     one-time sign-in in the browser (OAuth for installed
                      apps: PKCE + a loopback redirect on a random local port).
                      Only the refresh token is kept, in the OS keyring. Can be
                      cancelled with a ``threading.Event``.
* :func:`disconnect`  forget it here (:func:`forget_local`) and give the access
                      back at Google (:func:`revoke`).
* :func:`upcoming`    the next CALENDAR_WINDOW_H hours of meetings: title,
                      people, call link and call type.
* :func:`should_prompt` / :func:`reminder_due`  when to ask "record this
                      meeting?" (1 min before until 5 min after the start).
* :func:`visible_upcoming`, :func:`people_label`, :func:`when_parts`,
  :func:`reminder_text`  what the "Coming up" list and the reminder show.

Parsing and timing are pure functions, so they test without Google. Every
datetime here is timezone-aware: pass ``datetime.now(timezone.utc)`` as now.

The service around this (worker fetches, reminder timer, saved bell/prompted
state) is calendar_service.py.
"""
import base64
import hashlib
import html
import http.server
import json
import logging
import math
import re
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone

import requests

import storage
from app_info import APP_NAME

logger = logging.getLogger("transcribe.calendar")

# An OAuth client of type "Desktop app" (Google Cloud Console -> APIs & Services
# -> Credentials) in the project that has the Google Calendar API enabled. For
# installed apps Google does not treat this secret as confidential - it ships
# inside every copy of a desktop app. Config keys override both, for testing.
GOOGLE_CLIENT_ID = ""
GOOGLE_CLIENT_SECRET = ""
SCOPES = "openid email https://www.googleapis.com/auth/calendar.events.readonly"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
REFRESH_SECRET = "calendar_google_refresh_token"
LOGIN_TIMEOUT = 180
HTTP_TIMEOUT = 15
_SKIP_EVENT_TYPES = {"workingLocation", "outOfOffice", "focusTime"}
# Fixed wording (the details go to the log): a requests error's text holds
# object addresses, so it would differ on every try and read as a new error.
OFFLINE_TEXT = "Couldn't reach Google - check your internet connection."
CALENDAR_OFFLINE_TEXT = "Couldn't reach Google Calendar - check your internet connection."

# How far ahead "Coming up" reads (and how far back, so a meeting that started
# a few minutes ago is still there to record). 12 h means no next-morning rows
# in the afternoon; if this grows, upcoming() already follows nextPageToken.
CALENDAR_WINDOW_H = 12
PAST_MINUTES = 60
PAGE_SIZE = 50
MAX_PAGES = 4
# The reminder window: from a minute before the start until five minutes in.
REMIND_LEAD_S = 60
REMIND_GRACE_S = 300

CALL_TYPES = ("Google Meet", "Zoom", "Teams", "Webex")
CALL_OTHER = "other"

_OK_HTML = ("<!doctype html><html><head><meta charset='utf-8'><title>" + APP_NAME + "</title>"
            "<style>body{font-family:Segoe UI,system-ui,sans-serif;background:#0f0f12;"
            "color:#fff;display:flex;height:100vh;align-items:center;justify-content:center;"
            "margin:0}.c{color:#22c55e;font-size:42px;text-align:center}</style></head>"
            "<body><div><div class='c'>&#10003;</div><h2>Calendar connected to " + APP_NAME + "</h2>"
            "<p>You can close this tab.</p></div></body></html>")
_FAIL_HTML = ("<!doctype html><html><head><meta charset='utf-8'><title>" + APP_NAME + "</title></head>"
              "<body style='font-family:sans-serif'><h2>Calendar wasn't connected</h2>"
              "<p>Go back to " + APP_NAME + " and try again.</p></body></html>")


class CalendarError(RuntimeError):
    """A user-readable reason the calendar couldn't be reached."""


class CalendarAuthError(CalendarError):
    """Access is gone (revoked, expired): the user must connect again."""


class CalendarCancelled(CalendarError):
    """The user cancelled the browser sign-in (nothing to show)."""


def _client(cfg=None):
    cfg = cfg or {}
    return ((cfg.get("google_oauth_client_id") or GOOGLE_CLIENT_ID).strip(),
            (cfg.get("google_oauth_client_secret") or GOOGLE_CLIENT_SECRET).strip())


def configured(cfg=None):
    """True when this build has a Google OAuth client to connect with. The
    gate for every calendar UI: while it is False, nothing calendar shows."""
    return bool(_client(cfg)[0])


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
        self.server.result = {k: v[0] for k, v in query.items()}
        body = _OK_HTML if "code" in query else _FAIL_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

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


def connect(cfg=None, open_browser=webbrowser.open, timeout=LOGIN_TIMEOUT, cancel=None):
    """Sign in to Google Calendar in the browser; returns the account email.
    Blocking - run on a worker thread. Raises CalendarError, or
    CalendarCancelled once ``cancel`` (a threading.Event) is set: the local
    listener stops within about a second and nothing is kept."""
    client_id, client_secret = _client(cfg)
    if not client_id:
        raise CalendarError("Calendar connection isn't set up in this version yet.")
    if _cancelled(cancel):
        raise CalendarCancelled("Sign-in cancelled.")
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(24)
    try:
        server = _LoopbackServer(("127.0.0.1", 0), _Callback)
    except OSError:
        logger.warning("Calendar sign-in listener failed to start", exc_info=True)
        raise CalendarError("Couldn't start the sign-in on this computer - try again.")
    server.timeout = 1                        # handle_request() slice: cancel is seen within ~1 s
    server.result = None
    server.expected_state = state
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPES, "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "access_type": "offline", "prompt": "consent",
    })
    try:
        if open_browser(url) is False:          # webbrowser.open: no browser could start
            raise CalendarError("Couldn't open your web browser to sign in to Google.")
        deadline = time.time() + timeout
        while server.result is None and time.time() < deadline and not _cancelled(cancel):
            server.handle_request()
    finally:
        server.server_close()

    res = server.result or {}
    if _cancelled(cancel):
        raise CalendarCancelled("Sign-in cancelled.")
    if not res:
        raise CalendarError("Sign-in timed out - try again.")
    if res.get("state") != state:
        raise CalendarError("Sign-in failed a security check - try again.")
    if res.get("error"):
        raise CalendarError("Google sign-in was cancelled." if res["error"] == "access_denied"
                            else f"Google sign-in failed ({res['error']}).")
    data = {"code": res.get("code", ""), "client_id": client_id, "redirect_uri": redirect,
            "grant_type": "authorization_code", "code_verifier": verifier}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        logger.info("Calendar sign-in: token exchange failed", exc_info=True)
        raise CalendarError(OFFLINE_TEXT)
    if r.status_code != 200:
        raise CalendarError(f"Google refused the sign-in (HTTP {r.status_code}).")
    try:
        tok = r.json()
    except ValueError:
        tok = None
    if not isinstance(tok, dict):
        raise CalendarError("Google sent an answer that couldn't be read - try again.")
    if "calendar" not in (tok.get("scope") or ""):
        # Google shows each permission as its own checkbox.
        raise CalendarError("Calendar access wasn't granted - tick the calendar "
                            "permission on Google's screen.")
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
                            "no system keyring is available.")
    _cache_token(tok)
    return _email_from_id_token(tok.get("id_token"))


def forget_local():
    """Forget the calendar on this computer right away (keyring + memory).
    Returns the refresh token that was kept ("" if none), so the caller can
    :func:`revoke` it at Google off the GUI thread. No network."""
    refresh = ""
    try:
        refresh = storage.read_secret(REFRESH_SECRET) or ""
        storage.write_secret(REFRESH_SECRET, "")
    except Exception:
        pass
    with _token_lock:
        _token.update(value="", expires=0.0)
    return refresh


def revoke(refresh_token):
    """Give the app's access back at Google (best effort, blocking)."""
    if not refresh_token:
        return
    try:
        requests.post(REVOKE_URL, params={"token": refresh_token}, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        pass


def disconnect():
    """Forget the calendar here and revoke the app's access at Google."""
    revoke(forget_local())


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
        raise CalendarError("Calendar connection isn't set up in this version yet.")
    data = {"client_id": client_id, "refresh_token": refresh, "grant_type": "refresh_token"}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
    except requests.RequestException:
        logger.debug("Calendar token refresh failed", exc_info=True)
        raise CalendarError(OFFLINE_TEXT)
    if r.status_code in (400, 401):
        try:
            err = str((r.json() or {}).get("error") or "")
        except ValueError:
            err = ""
        if err == "invalid_grant":
            # Revoked, the password changed, or - while the Google app is
            # still in "Testing" - the 7-day token lifetime ran out.
            storage.write_secret(REFRESH_SECRET, "")
            raise CalendarAuthError("Calendar access expired - connect it again on the Meetings page.")
        # invalid_client / invalid_request: our side is misconfigured - the
        # user's grant is fine, so keep it.
        raise CalendarError(f"Google refused the calendar refresh ({err or 'HTTP ' + str(r.status_code)}).")
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


def _name_from_email(email):
    local = (email or "").split("@")[0]
    words = [w for w in re.split(r"[._\-+]+", local) if w and not w.isdigit()]
    return " ".join(w.capitalize() for w in words)


def attendee_names(attendees):
    """People on the invite (you included - notes attribute owners by name),
    display name or a readable email; meeting rooms are left out."""
    names = []
    for a in attendees or []:
        if not isinstance(a, dict) or a.get("resource"):
            continue
        name = (a.get("displayName") or "").strip() or _name_from_email(a.get("email"))
        if name and name not in names:
            names.append(name)
    return names


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


def parse_event(item):
    """A Google event as a meeting dict, or None when it isn't a meeting you'd
    record: all-day, cancelled, declined by you, focus time / out of office.

    Keys: id, title, start, end (aware datetimes), attendees (you included),
    others (without you), people ("Anna, David +2"), link (join URL or ""),
    call_type ("Google Meet" / "Zoom" / "Teams" / "Webex" / "other" / ""),
    recurring_id (the series id or ""), location."""
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
    return {
        "id": item.get("id") or f"{st.isoformat()}|{item.get('summary', '')}",
        "title": (item.get("summary") or "").strip() or "Untitled meeting",
        "start": st,
        "end": en,
        "attendees": attendee_names(attendees),
        "others": others,
        "people": people_label(others),
        "link": link or "",
        "call_type": call_type(link, conference),
        "recurring_id": str(item.get("recurringEventId") or ""),
        "location": location[:300],
    }


def _get_events(cfg, params):
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {_access_token(cfg)}"}
        try:
            r = requests.get(EVENTS_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException:
            logger.debug("Calendar events request failed", exc_info=True)
            raise CalendarError(CALENDAR_OFFLINE_TEXT)
        if r.status_code == 401 and attempt == 1:
            with _token_lock:                          # stale access token: refresh once
                _token.update(value="", expires=0.0)
            continue
        if r.status_code != 200:
            raise CalendarError(f"Google Calendar returned HTTP {r.status_code}.")
        try:
            data = r.json()
        except ValueError:
            raise CalendarError("Google Calendar sent an answer that couldn't be read.")
        return data if isinstance(data, dict) else {}
    return {}


def upcoming(cfg=None, hours=None, past_minutes=PAST_MINUTES, now=None):
    """Meetings from ``past_minutes`` ago to ``hours`` (default
    CALENDAR_WINDOW_H) ahead, soonest first. Blocking - run on a worker
    thread. Raises CalendarError, or CalendarAuthError when access is gone."""
    hours = CALENDAR_WINDOW_H if hours is None else hours
    now = now or datetime.now(timezone.utc)
    params = {"timeMin": _rfc3339(now - timedelta(minutes=past_minutes)),
              "timeMax": _rfc3339(now + timedelta(hours=hours)),
              "singleEvents": "true", "orderBy": "startTime", "maxResults": str(PAGE_SIZE)}
    items = []
    for _page in range(MAX_PAGES):
        data = _get_events(cfg, params)
        items.extend(data.get("items") or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
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


def reminder_due(meetings, now, muted=(), prompted=(), only_with_link=False,
                 lead_seconds=REMIND_LEAD_S, grace_seconds=REMIND_GRACE_S):
    """The meeting to ask "record this meeting?" about right now, or None.

    Skips muted ids (the row's bell is off), ids already asked about, and -
    with ``only_with_link`` - meetings without a call link. ``muted`` and
    ``prompted`` are any containers of event ids (sets, or dicts keyed by id).
    When several are due (overlapping meetings) the one whose start is
    closest to now wins; the next one is due on the next check. No network."""
    muted = set(muted or ())
    prompted = set(prompted or ())
    due = [m for m in meetings or ()
           if is_meeting(m) and m["id"] not in muted and m["id"] not in prompted
           and (m.get("link") or not only_with_link)
           and should_prompt(m, now, lead_seconds, grace_seconds)]
    if not due:
        return None
    return min(due, key=lambda m: (abs((m["start"] - now).total_seconds()), m["start"]))


def visible_upcoming(meetings, now, only_with_link=False):
    """The "Coming up" rows: meetings that haven't ended yet (upcoming() also
    returns the last hour), soonest first, each id once."""
    seen, out = set(), []
    for m in sorted((m for m in meetings or () if is_meeting(m)),
                    key=lambda m: (m["start"], m["end"], m.get("title") or "")):
        if m["end"] <= now or m["id"] in seen:
            continue
        if only_with_link and not m.get("link"):
            continue
        seen.add(m["id"])
        out.append(m)
    return out


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


def reminder_text(meeting, now):
    """The reminder's timing line: "Starts in 1 min", "Starting now" or
    "Started 2 min ago"."""
    secs = (meeting["start"] - now).total_seconds()
    if secs > 0:
        return f"Starts in {max(1, math.ceil(secs / 60))} min"
    ago = int(-secs // 60)
    return "Starting now" if ago < 1 else f"Started {ago} min ago"
