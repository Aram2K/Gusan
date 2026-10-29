"""The one door to Google Calendar's Connect, and the words every calendar card shares.

Google asks that the app says what it reads and why BEFORE the browser sign-in opens, so every
Connect button - the Meetings page's card and rail, Home's "Coming up" card - goes through
:func:`connect_calendar`, which asks first (the box below) and starts the sign-in only on "Connect".
"Not now" does nothing. In Privacy mode there is nothing to ask: the service says it is paused.

Light on purpose (Qt, the question card, calendar_sync): Home imports it without the Meetings
page code. ui/pages/meetings.py re-exports the moved names, so its callers and tests see no change.

    calendar_door.connect_calendar(parent, cal, privacy_mode)   # True when the sign-in started
    calendar_door.cancel_connect(cal)                           # the card's Cancel
    calendar_door.open_join(link)                               # a Join button (https only)
    calendar_door.line_for(connecting, error, tone, invite)     # the card's one line of words
"""

import logging

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

import calendar_sync
from ui.components import mini_prompt

logger = logging.getLogger("transcribe")

# The disclosure Google asks for, shown before the browser opens (every Connect button).
CONNECT_TITLE = "Connect Google Calendar"
CONNECT_BODY = ("Gusan reads your calendar to show your next meetings and offer to record them. "
                "It can only read: it never changes your calendar.")
# One short point per line (what it reads, what for, where it goes, how to stop), then a link.
CONNECT_DETAIL = (
    "•  Reads: title, time, guests, agenda and call link of each meeting.",
    "•  Uses them to: show Coming up, remind you to record, and name your meeting.",
    "•  Stays on this computer. Only if you use Live Assistant or AI notes, that meeting's "
    "title, guest names and agenda go to the AI.",
    "•  Disconnect any time in Meeting settings.")
CONNECT_LINK = ("Privacy details: gusan.aibuben.xyz/privacy", "https://gusan.aibuben.xyz/privacy")
# When the grant ran out (Google's 7-day limit while the app is in Testing, or revoked in the
# Google account): shown on the Connect card and in the rail until the next connect.
EXPIRED_NOTE = "Calendar access expired. Connect again."
# The line on a Connect card: what Connect does (Meetings), and while the browser waits.
INVITE_SUB = "See your meetings and get a reminder to record."
WAITING_LINE = "Waiting for Google… Finish in your browser."


def service_status(cal):
    """The calendar service's muted line ("Paused in Privacy mode." ...), "" when it has none."""
    fn = getattr(cal, "status_text", None)
    try:
        return str(fn() or "") if callable(fn) else ""
    except Exception:
        return ""


def paused_text():
    """calendar_service.STATUS_PAUSED_TEXT ("Paused in Privacy mode.")."""
    try:
        from calendar_service import STATUS_PAUSED_TEXT
        return STATUS_PAUSED_TEXT
    except Exception:
        return "Paused in Privacy mode."


def line_for(connecting, error, tone="red", invite=INVITE_SUB):
    """(text, tone) of a Connect card's line: "Waiting for Google…" (muted) while the browser
    waits, else the last error or the expiry note (in ``tone``), else the invitation ``invite``."""
    if connecting and not error:
        return WAITING_LINE, "muted"
    if error:
        return str(error), tone
    return invite, "muted"


def connect_calendar(parent, cal, privacy_mode=False):
    """Every Connect button's one door: the disclosure first, then the browser sign-in
    (``cal.connect_calendar()``). True when the sign-in started. False for no calendar, one
    that is not available, a sign-in already waiting for the browser (no second card), and
    "Not now"."""
    if cal is None or not getattr(cal, "available", True) or getattr(cal, "is_connecting", False):
        return False
    if not privacy_mode and not mini_prompt.ask(
            parent, CONNECT_TITLE, CONNECT_BODY, "Connect", "Not now", icon="calendar",
            detail=list(CONNECT_DETAIL), link=CONNECT_LINK):
        return False
    try:
        return bool(cal.connect_calendar())
    except Exception:
        logger.warning("connect_calendar failed", exc_info=True)
        return False


def cancel_connect(cal):
    """The Connect card's Cancel while the browser waits."""
    if cal is None:
        return
    try:
        cal.cancel_connect()
    except Exception:
        logger.debug("cancel_connect failed", exc_info=True)


def open_join(link):
    """A Join button: open the call in the browser - an https address only. True when asked."""
    url = calendar_sync.join_url(link)
    if not url:
        return False
    try:
        QDesktopServices.openUrl(QUrl(url))
        return True
    except Exception:
        logger.warning("Couldn't open the call link", exc_info=True)
        return False
