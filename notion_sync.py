"""Send a finished meeting to Notion: the one routine behind Export > Notion
(manual) and "Send every meeting's notes automatically" (auto).

    send_meeting(folder, cfg, auto=False, persist=None) -> {"ok", "url", "code", "sentence"}

Blocking: run it on a worker thread. It reads the meeting from its folder
(notes, meta, and the transcript unless ``auto``), publishes a page with
notion_export.publish_meeting, and writes the outcome back into the meeting's
meta.json (``notion_url``, ``notion_page_id``, ``notion_sent_at``,
``notion_auto``, ``notion_error``). What it does when Notion objects:

* a refused OAuth token (401): the token is refreshed ONCE under
  notion_connect._LOCK, the new pair is handed to ``persist`` BEFORE the retry
  (Notion rotates the refresh token: a pair that was lost would be a lost
  connection), then the send is retried once. A refresh Notion refuses for good
  sets ``notion_reconnect`` through ``persist`` and ends in code "auth".
* a missing page (404) when the destination is the "Gusan meetings" page Gusan
  made (``notion_hub_page``): the page is made again, ``persist`` gets it, and
  the send is retried once. A page the user picked is never swapped silently:
  code "no_access".
* a second send of the same meeting while one is running is "busy"; an
  automatic send of a meeting already in Notion is skipped with code "sent".
* mode "mcp" (the Connect button: Notion's hosted server, notion_mcp.py) sends
  through notion_mcp.send_meeting_mcp instead: the page lands under the private
  "Gusan meetings" page (made when it is missing or gone), the token is renewed
  inside the client - and every new pair, and a new hub, is handed to ``persist``
  before the next request. A connection Notion refuses for good ends in code
  "auth" with ``notion_reconnect`` set, same as "oauth" - unless the user
  disconnected (or connected again) while the send ran: then nothing is set and
  the result carries ``"stale": True`` (an "auth" result always has ``stale``).
  ``persist`` is the only way the flag is set: callers never set it themselves.

``persist(dict)`` is called on the worker thread with cfg keys to save; the
caller hops to the GUI thread (main: ``self._run_on_gui(self.set_settings, d)``).
Nothing here touches Qt, the config or the keyring, and nothing a server or
library said ever reaches a sentence or the log.

``code`` is "sent" when the meeting is in Notion (``ok`` True: it just went
there; ``ok`` False: it was already there and an automatic send skipped it),
"busy", "auth", "no_access", "rate", "offline", "full" or "other"; ``sentence`` is the
plain line for the user ("" when there is nothing to say). "full" (mode "mcp"): the Notion
workspace has used up its free blocks, so no page can be made until the user frees space,
upgrades or picks another workspace; the connection itself is fine and nothing is flagged.
"""
import logging
import os
import threading
from datetime import datetime, timezone

import meeting_store
import notion_connect
import notion_export
import notion_mcp
from app_info import APP_NAME
from notion_connect import ConnectError
from notion_export import NotionAuthError, NotionError, NotionNotFoundError

logger = logging.getLogger("transcribe.notion")

CODES = ("sent", "busy", "auth", "no_access", "rate", "offline", "full", "other")
DEFAULT_TITLE = "Meeting notes"

BUSY_TEXT = "Already sending this meeting."
NOTHING_TEXT = "Nothing to send yet."
OTHER_TEXT = "Couldn't send to Notion."
SENTENCES = {
    "no_access": OTHER_TEXT + " Pick another page in Meeting settings.",
    "rate": "Notion is busy. Try again in a minute.",
    "offline": notion_export.OFFLINE_TEXT,
    "full": notion_export.FULL_TEXT,
    "other": OTHER_TEXT,
}
AUTH_OAUTH_TEXT = OTHER_TEXT + " Reconnect it in Meeting settings."
AUTH_OWN_TEXT = OTHER_TEXT + " Notion rejected the secret."
# Mode "mcp", a page Notion won't let us use: there is no page to pick, and the token is fine, so
# Settings shows no "Reconnect" button - only Manage and Disconnect. Say the steps that are there.
# (Two lines in the notes toolbar at most, like the others: a longer one makes the toolbar taller.)
NO_ACCESS_MCP_TEXT = OTHER_TEXT + " Disconnect and connect it in Meeting settings."

_INFLIGHT = set()               # normalised folders being sent right now (auto, a click, a retry)
_GUARD = threading.Lock()


# ── words ────────────────────────────────────────────────────────────────────
def _when(started):
    """"2026-09-29 11:00:00" -> "Tue 29 Sep · 11:00" (the meeting page's own line)."""
    try:
        dt = datetime.strptime(str(started or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(started or "")
    return "%s %d %s · %s" % (dt.strftime("%a"), dt.day, dt.strftime("%b"), dt.strftime("%H:%M"))


def meta_line(meta, folder=None):
    """The grey line under the page title: when · duration · people · Gusan.
    ``folder`` only supplies the start time of a meeting whose meta has none
    (its name is the time it started)."""
    meta = meta if isinstance(meta, dict) else {}
    started = meta.get("started")
    if not started and folder:
        started = meeting_store.folder_timestamp(os.path.basename(str(folder).rstrip("\\/")))
    try:
        seconds = max(0, int(meta.get("duration_sec") or 0))
    except (TypeError, ValueError):
        seconds = 0
    people = meta.get("attendees_list")
    if not (isinstance(people, list) and all(isinstance(p, str) for p in people)):
        people = meeting_store.split_attendees(meta.get("attendees") or "")
    bits = [_when(started), meeting_store.format_duration(seconds) if seconds else "",
            ", ".join(people), APP_NAME]
    return " · ".join(b for b in bits if b)


def friendly(exc, mode):
    """(code, sentence) for a failed send. ``mode`` is "mcp", "oauth" or "own": a
    refused token reads differently when the user never saw a secret."""
    if isinstance(exc, ConnectError):            # a refresh that failed
        code = {"expired": "auth", "offline": "offline"}.get(exc.code, "other")
    elif isinstance(exc, NotionError):
        code = exc.code if exc.code in ("auth", "no_access", "rate", "offline", "full") else "other"
    else:
        code = "other"
    if code == "auth":
        return code, AUTH_OAUTH_TEXT if mode in notion_export.GRANT_MODES else AUTH_OWN_TEXT
    if code == "no_access" and mode == "mcp":
        return code, NO_ACCESS_MCP_TEXT             # there is no page to pick: connecting again is the way out
    return code, SENTENCES[code]


def _result(ok, url="", code="sent", sentence=""):
    return {"ok": bool(ok), "url": url or "", "code": code, "sentence": sentence}


# ── the send ─────────────────────────────────────────────────────────────────
def send_meeting(folder, cfg, *, auto=False, persist=None, publish=None, refresh=None,
                 create_hub=None, update_meta=None, mcp_send=None):
    """Send one meeting to Notion (see the module docstring). ``publish``,
    ``refresh``, ``create_hub``, ``update_meta`` and ``mcp_send`` default to
    notion_export.publish_meeting, notion_connect.refresh,
    notion_connect.create_hub, meeting_store.update_meta and
    notion_mcp.send_meeting_mcp (looked up on each call, so a test can swap them
    either way). ``publish``, ``refresh`` and ``create_hub`` serve the "own" and
    "oauth" modes, ``mcp_send`` the "mcp" mode."""
    key = os.path.normcase(os.path.abspath(str(folder)))
    with _GUARD:
        if key in _INFLIGHT:
            return _result(False, code="busy", sentence=BUSY_TEXT)
        _INFLIGHT.add(key)
    try:
        return _send(folder, cfg or {}, bool(auto), persist,
                     publish or notion_export.publish_meeting,
                     refresh or notion_connect.refresh,
                     create_hub or notion_connect.create_hub,
                     update_meta or meeting_store.update_meta,
                     mcp_send or notion_mcp.send_meeting_mcp)
    finally:
        with _GUARD:
            _INFLIGHT.discard(key)


def _save(persist, updates):
    """Hand settings to the caller's ``persist`` (never lets it break the send)."""
    if persist is None:
        return
    try:
        persist(dict(updates))
    except Exception:
        logger.warning("Saving the Notion settings failed", exc_info=True)


def _write(update_meta, folder, **fields):
    try:
        update_meta(folder, **fields)
    except Exception:
        logger.debug("Writing the Notion result into the meeting failed", exc_info=True)


def _send(folder, cfg, auto, persist, publish, refresh, create_hub, update_meta, mcp_send):
    meta = meeting_store.load_meta(folder)
    if auto and meta.get("notion_url"):
        return _result(False, str(meta["notion_url"]))                  # already there: never twice
    notes = meeting_store.load_notes(folder)
    transcript = "" if auto else meeting_store.load_transcript(folder)
    if not (notes.strip() or transcript.strip()):
        return _result(False, code="other", sentence=NOTHING_TEXT)

    mode = notion_export.connection_mode(cfg) or "own"
    oauth = mode == "oauth"
    title = str(meta.get("title") or "").strip() or DEFAULT_TITLE
    line = meta_line(meta, folder)
    token = (cfg.get("notion_api_key") or "").strip()
    refresh_token = (cfg.get("notion_refresh_token") or "").strip()
    parent = cfg.get("notion_parent_page") or ""
    hub = notion_export.extract_page_id(cfg.get("notion_hub_page")) or ""

    def attempt():
        return publish(token, parent, title, notes, transcript=transcript, meta_line=line, oauth=oauth)

    held = []                                           # the credentials of an "mcp" send (see the failure branch)

    def attempt_mcp():
        # Notion's hosted server: the client renews the token itself and hands every new pair
        # (and a new hub) to ``persist`` before the next request, so nothing is saved here.
        creds = notion_mcp.Credentials.from_cfg(cfg)
        held.append(creds)
        out = mcp_send(creds, title, notes, transcript, line,
                       parent=parent, hub=hub, persist=lambda updates: _save(persist, updates))
        return out["url"]

    fixed_token = fixed_hub = False
    try:
        if mode == "mcp":
            url = attempt_mcp()
        else:
            while True:
                try:
                    url = attempt()
                    break
                except NotionAuthError:
                    if not oauth or fixed_token:
                        raise
                    fixed_token = True
                    with notion_connect._LOCK:              # one refresh at a time; the pair is saved before the retry
                        new = refresh(refresh_token)
                        token = new["access_token"]
                        refresh_token = new.get("refresh_token") or refresh_token
                        _save(persist, {"notion_api_key": token, "notion_refresh_token": refresh_token})
                except NotionNotFoundError:
                    if fixed_hub or not hub or notion_export.extract_page_id(parent) != hub:
                        raise                               # a page the user picked: tell them, never swap it
                    fixed_hub = True
                    page = create_hub(token)
                    parent = hub = page["id"]
                    _save(persist, {"notion_hub_page": page["id"], "notion_parent_page": page["id"],
                                    "notion_parent_title": page.get("title") or notion_connect.HUB_TITLE})
    except Exception as e:                              # NotionError, ConnectError, or a bug: never raised to the caller
        code, sentence = friendly(e, mode)
        if not isinstance(e, (NotionError, ConnectError)):
            logger.warning("Sending a meeting to Notion failed (%s)", type(e).__name__, exc_info=True)
        # The Settings row then says "Reconnect" - unless the user disconnected (or connected again)
        # while this send was running: then a refused token is no news, and nothing is flagged.
        stale = not all(c.current() for c in held)
        if code == "auth" and mode in notion_export.GRANT_MODES and not stale:
            _save(persist, {"notion_reconnect": True})
        fields = {"notion_error": code}
        partial = str(getattr(e, "url", "") or "")
        if partial:
            fields["notion_url"] = partial              # the page exists, only its tail failed: keep the link
        _write(update_meta, folder, **fields)
        out = _result(False, partial, code, sentence)
        if code == "auth":
            out["stale"] = stale                        # so the caller doesn't ask to reconnect a connection that is gone
        return out

    _write(update_meta, folder, notion_url=url, notion_page_id=notion_export.extract_page_id(url) or "",
           notion_sent_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           notion_auto=auto, notion_error=None)
    return _result(True, url)
