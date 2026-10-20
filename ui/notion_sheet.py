# -*- coding: utf-8 -*-
"""Connect Notion (Gusan 2.0): the sheet behind Meeting settings > "Send notes to
Notion" and the "Send to Notion" item of a meeting's Export menu.

    show_notion_sheet(host, then="", reason="")   # modal; True = sent / connected
    disconnect_notion(host)                       # forget locally, revoke in the background

``host`` is the Meetings page (cfg(), set_settings(), set_setting(), track(),
call()). One sheet, one decision per screen, painted only with the app's tokens
(never the OS palette, so it reads the same on a dark Windows):

  intro      one button, "Connect Notion" (Notion opens in the browser, the user
             allows access, the app makes a private "Gusan meetings" page)
  waiting    "Finish in your browser" with Cancel; "Setting things up…" while the
             code is swapped for a token and the page is made
  connected  workspace + "Connected", the automatic-send switch, where notes go
  manual     "Use your own integration": the secret from notion.so/my-integrations
             (the only path while notion_connect.NOTION_CLIENT_ID is empty)
  share      "Share a page first": connected, but there is nowhere to put notes yet

The waiting work (notion_connect.connect, then the destination) runs on a worker
(api_keys.run_then, or the ``run=`` a test passes); a cancelled or closed sheet
ignores a late answer, and a grant that was made after the user gave up is revoked.

Persistence (cfg keys, see main.DEFAULT): a connection is written in ONE
set_settings call the moment it has a destination (notion_api_key and
notion_refresh_token go to the keyring through main's own handling), then
flush_config_save so the grant is stored now. Nothing is written for a token that
has nowhere to send notes ("share"): it is held in memory, and an OAuth grant is
revoked if the sheet closes without a page. The picker and the switch apply at
once. An own-integration secret is never sent to our server, not even to revoke.

Privacy mode keeps everything on this computer: Connect, a pasted secret and
Refresh ask once per sheet before going online, and opening Manage shows the
saved page and waits ("Paused while Privacy mode is on"). "Search pages" under
the picker (only when the list is full, the newest PAGE_LIMIT) searches Notion
after a pause in the typing. Connecting again after the connection expired keeps
the page notes were going to when Notion can still reach it.

Integration notes
-----------------
* ui/pages/meetings.py: MeetingsPage.connect_notion(then, reason) calls
  show_notion_sheet; the rail's Connect / Manage / Reconnect open it and its
  Disconnect calls disconnect_notion.
* ui/pages/meeting_views.py: Send to Notion calls connect_notion(then="Send this
  meeting"); the connected screen then ends in that button and the send goes on.
* Telemetry (no text, ids or names): notion_connect_result {mode, ok, reason} and
  notion_disconnected {mode, reason}.
"""

import logging
import math
import threading
import time
import webbrowser

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFontMetrics, QPainter, QPalette, QPen, QPixmap, QPolygonF, QTextDocumentFragment
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QStackedWidget, QStyle, QStyleOptionComboBox, QStylePainter,
                               QVBoxLayout, QWidget)

import notion_connect
import notion_export
from app_info import APP_NAME
from notion_connect import MESSAGES, ConnectCancelled, ConnectError
from notion_export import NotionAuthError, NotionError
from ui import privacy_prompt, theme
from ui.auth_sheet import FieldBox, PasswordField, link_button, text_label, tint
from ui.components import Button, Chip, Divider, IconButton, Toggle, WhiteCard, icons, mini_prompt
from ui.pages.meeting_views import ElidedLabel

logger = logging.getLogger("transcribe")

# ── words (the product's, in one place) ──────────────────────────────────────
INTRO_TITLE = "Send your meeting notes\nto Notion."
INTRO_NAME = "Send your meeting notes to Notion."
CONNECT = "Connect Notion"
TRY_AGAIN = "Try again"
CAPTION = "Opens Notion in your browser."
OWN_LINK = "Use a Notion secret instead"
WAIT_TITLE = "Finish in your browser"
WAIT_BODY = "Allow access in Notion, then come back here."
REOPEN = "Open Notion again"
FINISH_TITLE = "Setting things up…"
FINISH_BODY = "Just a moment."
AUTO_LABEL = "Send every meeting's notes automatically"
AUTO_NAME = "Send every meeting's notes to Notion automatically"
AUTO_HINT = "Right after each meeting."
DEST_LABEL = "Notes go to"
DEST_HINT = "To add pages, share them with %s in Notion." % APP_NAME
DEST_HINT_OWN = "To add pages, share them with your integration in Notion."
DEST_HINT_FAILED = "Couldn't load your pages. Try refresh."
DEST_HINT_PAUSED = "Paused while Privacy mode is on. Refresh to load your pages."
SEARCH_LABEL = "Search pages"
LEFT_BEHIND = ("Removed from %s. Also remove it in Notion under Settings > Connections." % APP_NAME)
HUB_LABEL = "%s (private page)" % notion_connect.HUB_TITLE
MANUAL_TITLE = "Use your own integration"
STEP_1 = "Create an integration in Notion."
STEP_2 = "Open the page for your notes, click ••• then Connections, and add your integration."
STEP_3 = "Paste the integration's secret here."
SECRET_HINT = "Starts with ntn_ or secret_"
SHARE_TITLE = "Share a page first"
SHARE_BODY = "In Notion, open the page for your notes, click ••• then Connections, and add %s. Then come back here."
SHARE_BODY_OWN = SHARE_BODY.replace("add %s", "add your integration")
SHARE_EMPTY = "Nothing shared yet. Add the page, then check again."
PRIVACY_TITLE = privacy_prompt.TITLE         # the same question, the same words, as the rest of the app
PRIVACY_BODY = "This goes online. Connect anyway?"
# Meeting settings (the rail row that opens this sheet)
RAIL_AUTO_HINT = "After every meeting"
RAIL_PAUSED_HINT = "Paused while Privacy mode is on"
RAIL_RECONNECT_HINT = "Reconnect Notion to keep sending."

# ── numbers ──────────────────────────────────────────────────────────────────
W = 440                         # the sheet's width
MAX_H = 640                     # taller than this and the sheet scrolls
SIDE, TOP, BOTTOM = 32, 28, 24  # body margins; the content is W - 2 * SIDE = 376 wide
RING = 5                        # a text link: its text starts this far in, so the focus ring has room
INSET = 6                       # each screen is this much wider than its content on both sides, so a
                                # link pulled left by RING (to line its text up) keeps its whole ring
PAGE_LIMIT = 50                 # pages listed in the picker
PICK_CHARS = 40                 # a page title longer than this ends in "…" in the picker
SEARCH_DELAY_MS = 300           # typing in "Search pages" waits this long for a pause

PAGES = ("intro", "waiting", "connected", "manual", "share")

# The tokens drawn as text straight on the window gradient (the tests check each
# is readable, 4.5:1, on all three gradient stops). "muted" is only for the white
# card: on the gradient it is below 4.5:1.
TEXT_ON_WINDOW = ("ink", "text2", "blue-tx", "red-tx")
WINDOW_STOPS = ("window-a", "window-b", "window-c")


def _open_url(url):
    """Open ``url`` in the browser (tests replace this)."""
    return webbrowser.open(url)


def _run_then(fn, *args, then=None, error=None, **kwargs):
    """fn on a worker, then(result) / error(exc) on the GUI thread (tests replace it)."""
    import api_keys
    return api_keys.run_then(fn, *args, then=then, error=error, name="gusan-notion", **kwargs)


# ── the mark ─────────────────────────────────────────────────────────────────
def notion_pixmap(size):
    """The Notion mark at ``size`` logical px: a white rounded tile with a plain
    "N" drawn in ink (not Notion's own logo file). 48 px is the sheet's tile, 16 px
    the Settings row's label icon."""
    dpr = icons._dpr()
    px = max(1, int(round(size * dpr)))
    pm = QPixmap(px, px)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(theme.qcolor("line2"), 1))
    p.setBrush(theme.qcolor("white"))
    p.drawRoundedRect(QRectF(0.5, 0.5, size - 1, size - 1), size * 0.25, size * 0.25)
    # The N: a polyline in a 26 x 26 box on the 48 px tile, a bit bigger on small tiles.
    k = size / 48.0
    box = 26 * k * (1.0 if size >= 32 else 1.35)
    ox = oy = (size - box) / 2.0
    f = box / 26.0
    n = QPolygonF([QPointF(ox + x * f, oy + y * f) for x, y in ((7, 20), (7, 6), (19, 20), (19, 6))])
    p.setPen(QPen(theme.qcolor("ink"), max(1.5, 2.6 * f), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    p.drawPolyline(n)
    p.end()
    return pm


def mark_label(size=16):
    """The mark as a QLabel (the Settings row's leading icon, the sheet's tile)."""
    lb = QLabel()
    lb.setPixmap(notion_pixmap(size))
    lb.setFixedSize(size, size)
    lb.setAccessibleName("Notion")
    lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return lb


# ── the connection written to / removed from the settings ────────────────────
# Everything disconnect blanks (the switch goes back to off: a new connection
# starts quiet, the user turns it on). Defined Qt-free, so main.py can use it too.
BLANK = dict(notion_export.BLANK_SETTINGS)


def connection_settings(res):
    """The cfg keys of a finished connection ``res`` ({"mode", "token", "refresh",
    "workspace", "dest": {"id", "title", "kind"}})."""
    dest = res["dest"]
    return {
        "notion_api_key": res["token"], "notion_refresh_token": res.get("refresh") or "",
        "notion_auth_mode": res["mode"], "notion_workspace_name": res.get("workspace") or "",
        "notion_parent_page": dest["id"], "notion_parent_title": dest.get("title") or "",
        "notion_hub_page": dest["id"] if dest.get("kind") == "hub" else "",
        "notion_reconnect": False,
    }


def _save(host, updates):
    """Apply settings at once (and store them now). False when one was refused."""
    fn = getattr(host, "set_settings", None)
    try:
        ok = bool(fn(dict(updates))) if callable(fn) else all(host.set_setting(k, v) for k, v in updates.items())
    except Exception:
        logger.warning("Saving the Notion settings failed", exc_info=True)
        return False
    _flush(host)
    return ok


def _flush(host):
    """Write the settings now, so a fresh grant is in the keyring and not on a timer."""
    fn = getattr(host, "call", None)
    if callable(fn):
        try:
            fn("flush_config_save")
        except Exception:
            logger.warning("flush_config_save() failed", exc_info=True)


def _track(host, event, props):
    fn = getattr(host, "track", None)
    if callable(fn):
        try:
            fn(event, props)
        except Exception:
            logger.debug("track failed", exc_info=True)


def disconnect_notion(host, run=None, reason="user"):
    """Forget Notion on this computer at once; give the app's access back at
    Notion in the background (best effort). Only a connection made with the
    button has a grant to give back: a pasted integration secret is the user's own
    and never leaves for our server."""
    cfg = host.cfg()
    mode = notion_export.connection_mode(cfg) or "own"
    token = (notion_connect.latest_tokens().get("access_token") or cfg.get("notion_api_key") or "").strip()
    _save(host, BLANK)
    notion_connect.forget_tokens()
    _track(host, "notion_disconnected", {"mode": mode, "reason": reason})
    if mode == "oauth" and token:
        (run or _run_then)(notion_connect.revoke, token, then=lambda done: _revoked(host, done))


def _revoked(host, done):
    """The grant could not be given back (offline, our server down) and the
    token is already forgotten here, so nothing will retry: the user is the only
    one who can remove the app in Notion. Say so (a tray notice; the sheet or the
    Settings row it came from may be gone). ``done`` is notion_connect.revoke's
    answer; only a plain False means it failed."""
    if done is not False:
        return
    fn = getattr(host, "call", None)
    if callable(fn):
        try:
            fn("show_tray_hint", "Notion", LEFT_BEHIND)
        except Exception:
            logger.debug("tray hint failed", exc_info=True)


# ── the workers ──────────────────────────────────────────────────────────────
class NotionFlow(QObject):
    """Connecting and listing pages on worker threads.

    waiting(bool)       the browser is open (True) / the wait ended (False)
    finishing()         the code arrived; the token is being swapped and the page made
    connected(res)      a destination exists: {"mode", "token", "refresh", "workspace",
                        "dest": {"id", "title", "kind"}, "pages": [...] or None}
    no_page(res)        connected but nothing is shared yet (same keys, no "dest")
    failed(code, text)  a ConnectError code and its plain sentence (MESSAGES)
    pages_loaded(list, code)   the picker's pages (code "" = fine, else why not)
    session_lost()      Notion refused the saved token for good: connect again"""

    waiting = Signal(bool)
    finishing = Signal()
    connected = Signal(dict)
    no_page = Signal(dict)
    failed = Signal(str, str)
    pages_loaded = Signal(list, str)
    session_lost = Signal()

    def __init__(self, host, parent=None, run=None, connect_fn=None):
        super().__init__(parent)
        self.host = host
        self._run = run
        self._connect_fn = connect_fn
        self._attempt = 0               # a cancelled or superseded attempt's late answer is ignored
        self._pages_gen = 0
        self._busy = False
        self._cancel = None
        self._url = ""

    def _go(self, fn, *args, then=None, error=None):
        return (self._run or _run_then)(fn, *args, then=then, error=error)

    def busy(self):
        return self._busy

    def url(self):
        return self._url

    def _set_url(self, url):
        self._url = url                 # from the worker: a plain assignment is enough

    def _track_result(self, mode, ok, reason=""):
        _track(self.host, "notion_connect_result", {"mode": mode, "ok": bool(ok), "reason": reason})

    # ── the button ──
    def start_oauth(self):
        """"Connect Notion": the browser, then the token, then where notes go."""
        if self._busy:
            return False
        self._attempt += 1
        attempt = self._attempt
        self._busy = True               # first: a stand-in runner answers before _go returns
        self._url = ""
        self._cancel = cancel = threading.Event()
        self.waiting.emit(True)
        self._go(self._oauth_work, cancel, dict(self.host.cfg()),
                 then=lambda res, a=attempt: self._oauth_done(a, res),
                 error=lambda exc, a=attempt: self._oauth_failed(a, exc))
        return True

    def _oauth_work(self, cancel, cfg):
        """The worker. A cancel, a timeout, a denial, no internet: the ordinary
        ways connecting ends are answered as {"error": ConnectError}, not raised -
        a raise would be logged as a failed background task, with a traceback, for
        something the user chose. A real fault (a bug) still raises."""
        try:
            return self._oauth_steps(cancel, cfg)
        except ConnectError as exc:                 # ConnectCancelled too
            return {"error": exc}

    def _oauth_steps(self, cancel, cfg):
        connect = self._connect_fn or notion_connect.connect
        tok = connect(cfg, open_browser=lambda url: _open_url(url), cancel=cancel,
                      on_url=self._set_url, on_code=self.finishing.emit)
        token = tok["access_token"]
        template = tok.get("duplicated_template_id") or ""
        try:
            dest = None if template else self._saved_destination(token, cfg)
            if dest is None:
                dest = notion_connect.choose_destination(token, template)
        except NotionError as e:
            notion_connect.revoke(token)            # connected, but nowhere to put notes and no way to ask
            raise ConnectError("offline" if e.code == "offline" else "other") from e
        if cancel.is_set():
            notion_connect.revoke(token)            # the user gave up while the page was being made
            raise ConnectCancelled()
        return {"mode": "oauth", "token": token, "refresh": tok.get("refresh_token") or "",
                "workspace": tok.get("workspace_name") or "", "dest": dest, "pages": None}

    @staticmethod
    def _saved_destination(token, cfg):
        """Connecting again after the connection expired: the page notes were going
        to is still the destination when Notion can still reach it - no second
        "Gusan meetings" page for the same notes. None = make or pick one."""
        saved = notion_export.extract_page_id(cfg.get("notion_parent_page"))
        if not saved:
            return None
        try:
            if not notion_connect.page_alive(token, saved):
                return None                         # trashed, or not shared with this grant
        except NotionError:
            return None                             # could not check: the normal way
        hub = notion_export.extract_page_id(cfg.get("notion_hub_page"))
        kind = "hub" if saved == hub else "page"
        title = (cfg.get("notion_parent_title") or "").strip() or (
            notion_connect.HUB_TITLE if kind == "hub" else "Selected page")
        return {"id": saved, "kind": kind, "title": title}

    def _oauth_done(self, attempt, res):
        if "error" in res:
            self._oauth_failed(attempt, res["error"])
            return
        if attempt != self._attempt:
            self._go(notion_connect.revoke, res["token"])   # cancelled or closed: nobody keeps this grant
            return
        self._busy = False
        self._track_result("oauth", True)
        self._emit_result(res)
        self.waiting.emit(False)

    def _oauth_failed(self, attempt, exc):
        if attempt != self._attempt:
            return
        self._busy = False
        code = exc.code if isinstance(exc, ConnectError) else "other"
        self._track_result("oauth", False, "" if code == "cancelled" else code)
        if code != "cancelled":
            self.failed.emit(code, MESSAGES.get(code, MESSAGES["other"]))
        self.waiting.emit(False)

    def cancel(self):
        """The "Cancel" next to "Finish in your browser"."""
        if not self._busy:
            return
        self._attempt += 1              # the worker's late answer is ignored
        self._busy = False
        ev, self._cancel = self._cancel, None
        if ev is not None:
            ev.set()
        self.waiting.emit(False)
        self._track_result("oauth", False, "cancelled")

    def drop_pages(self):
        """Forget a picker search still running (a late answer must not save anything)."""
        self._pages_gen += 1

    def reopen(self):
        """"Open Notion again": the same address, the same listener."""
        if self._url:
            _open_url(self._url)

    def shutdown(self):
        """The sheet closed: stop waiting and drop every late answer."""
        ev, self._cancel = self._cancel, None
        if ev is not None:
            ev.set()
            if self._busy:
                self._track_result("oauth", False, "cancelled")     # closed with the window's own X
        self._attempt += 1
        self._pages_gen += 1
        self._busy = False

    # ── the pasted secret, and "Check again" ──
    def check_secret(self, secret):
        """Own integration: is the secret good, and which pages did the user share?"""
        secret = (secret or "").strip()
        if not secret or self._busy:
            return False
        return self._search({"mode": "own", "token": secret, "refresh": "", "workspace": ""}, validate=True)

    def recheck(self, held):
        """"Check again" on "Share a page first": look for shared pages."""
        if self._busy:
            return False
        return self._search(dict(held), validate=False)

    def _search(self, base, validate):
        self._attempt += 1
        attempt = self._attempt
        self._busy = True
        self._go(self._search_work, base, validate,
                 then=lambda res, a=attempt: self._search_done(a, res, validate),
                 error=lambda exc, a=attempt: self._search_failed(a, exc, base["mode"], validate))
        return True

    @staticmethod
    def _search_work(base, validate):
        res = dict(base)
        if validate:
            res["workspace"] = notion_connect.validate_token(res["token"]).get("workspace_name") or ""
        pages = notion_connect.search_pages(res["token"], limit=PAGE_LIMIT)
        first = pages[0] if pages else None
        res["pages"] = pages
        res["dest"] = {"id": first["id"], "title": first["title"], "kind": "page"} if first else None
        return res

    def _search_done(self, attempt, res, validate):
        if attempt != self._attempt:
            return
        self._busy = False
        if validate:
            self._track_result(res["mode"], True)
        self._emit_result(res)

    def _search_failed(self, attempt, exc, mode, validate):
        if attempt != self._attempt:
            return
        self._busy = False
        if isinstance(exc, NotionAuthError):
            code = "rejected"
        elif isinstance(exc, NotionError) and exc.code == "offline":
            code = "offline"
        else:
            code = "other"
        if validate:
            self._track_result(mode, False, "other" if code == "rejected" else code)
        self.failed.emit(code, MESSAGES[code])

    def _emit_result(self, res):
        (self.connected if res.get("dest") else self.no_page).emit(res)

    # ── the picker's pages ──
    def load_pages(self, query=""):
        """Search the pages the saved token can see (the picker; ``query`` narrows
        them by title). A token Notion refuses is refreshed once (OAuth); a refresh
        it refuses ends the connection. The newest search wins."""
        cfg = self.host.cfg()
        self._pages_gen += 1
        gen = self._pages_gen
        mode = notion_export.connection_mode(cfg)
        self._go(self._pages_work, (cfg.get("notion_api_key") or "").strip(),
                 (cfg.get("notion_refresh_token") or "").strip(), mode, (query or "").strip(),
                 then=lambda res, g=gen: self._pages_done(g, res),
                 error=lambda exc, g=gen, m=mode: self._pages_failed(g, exc, m))

    @staticmethod
    def _pages_work(token, refresh_token, mode, query=""):
        try:
            return {"pages": notion_connect.search_pages(token, query=query, limit=PAGE_LIMIT),
                    "tokens": None, "from_refresh": ""}
        except NotionAuthError:
            if mode != "oauth" or not refresh_token:
                raise
        new = notion_connect.refresh(refresh_token)         # ConnectError "expired": connect again
        return {"pages": notion_connect.search_pages(new["access_token"], query=query, limit=PAGE_LIMIT),
                "from_refresh": refresh_token,              # the token this pair replaced
                "tokens": {"notion_api_key": new["access_token"],
                           "notion_refresh_token": new.get("refresh_token") or refresh_token,
                           "notion_reconnect": False}}

    def _pages_done(self, gen, res):
        tokens = res.get("tokens")
        # Notion already rotated the old refresh token: keep the new pair even when
        # this answer is late (the sheet closed, a newer search began) - or the saved
        # token would be a refused one after the next start. But never after a
        # disconnect or a reconnect: then the saved refresh token is not the one this
        # pair replaced.
        if tokens and (self.host.cfg().get("notion_refresh_token") or "").strip() == res.get("from_refresh"):
            _save(self.host, tokens)
        if gen != self._pages_gen:
            return
        self.pages_loaded.emit(list(res.get("pages") or []), "")

    def _pages_failed(self, gen, exc, mode):
        if gen != self._pages_gen:
            return
        if (isinstance(exc, ConnectError) and exc.code == "expired") or isinstance(exc, NotionAuthError):
            if mode == "oauth":
                _save(self.host, {"notion_reconnect": True})      # a pasted secret has no "connect again"
            self.session_lost.emit()
            return
        self.pages_loaded.emit([], exc.code if isinstance(exc, (ConnectError, NotionError)) else "other")


# ── widgets ──────────────────────────────────────────────────────────────────
class ProgressLine(QWidget):
    """A 4 px line with a blue 96 px stretch that slides along it and back every
    1.6 s. It only runs while it is on screen."""

    SEG, THICK, PERIOD = 96, 4, 1.6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.THICK)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAccessibleName("Waiting for Notion")
        self._t0 = time.monotonic()
        self._pin = None                # tests and screenshots pin the position (0..1)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)

    def pin(self, phase):
        self._pin = phase
        self.update()

    def position(self):
        """0..1, eased: left end -> right end -> left end."""
        if self._pin is not None:
            return self._pin
        phase = ((time.monotonic() - self._t0) % self.PERIOD) / self.PERIOD
        tri = 1.0 - abs(2.0 * phase - 1.0)
        return 0.5 - 0.5 * math.cos(math.pi * tri)

    def showEvent(self, e):
        super().showEvent(e)
        self._timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        r = self.THICK / 2.0
        p.setBrush(theme.qcolor("line2"))
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.THICK), r, r)
        seg = min(self.SEG, self.width())
        x = (self.width() - seg) * self.position()
        p.setBrush(theme.qcolor("blue"))
        p.drawRoundedRect(QRectF(x, 0, seg, self.THICK), r, r)
        p.end()


class _Underline(QObject):
    """Underline a link while the mouse is over it or the keyboard is on it."""

    def eventFilter(self, obj, ev):
        t = ev.type()
        on = None
        if t == QEvent.Enter or (t == QEvent.FocusIn and ev.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)):
            on = True
        elif t in (QEvent.Leave, QEvent.FocusOut):
            on = False
        if on is not None:
            f = obj.font()
            f.setUnderline(on)
            obj.setFont(f)
        return False


_UNDERLINE = _Underline()


def ring_room(button, color):
    """Colour a text link and leave room around its text for the keyboard ring: the
    text starts RING px in, with the 1 px border and 4 px padding, or the 2 px ring
    and 3 px padding (so it does not move when the ring appears). The ring then
    encloses the letters instead of touching them."""
    button.setStyleSheet('QPushButton { color: %s; padding: 0px %dpx; }'
                         'QPushButton[focusVisible="true"] { padding: 0px %dpx; }'
                         % (theme.qss_color(color), RING - 1, RING - 2))
    return button


def quiet_link(text, px=13, weight=500, parent=None):
    """A quiet text link (slate, underlined on hover)."""
    b = ring_room(link_button(text, px, weight, "text2", parent=parent), "text2")
    b.installEventFilter(_UNDERLINE)
    return b


class PulledRow(QHBoxLayout):
    """A row that reaches RING px to the left of the column it sits in, so a text
    link at its start has its text (not its invisible ring room) lined up with the
    text above and below it. (Qt reads a negative layout margin as "use the
    default", so the row moves its own rectangle.)"""

    def __init__(self):
        super().__init__()
        self.setContentsMargins(0, 0, 0, 0)

    def setGeometry(self, rect):                        # noqa: N802 (Qt name)
        super().setGeometry(rect.adjusted(-RING, 0, 0, 0))


def flush_left(link):
    """``link`` alone in a PulledRow."""
    row = PulledRow()
    row.addWidget(link, 0, Qt.AlignLeft)
    row.addStretch(1)
    return row


def set_text(button, text):
    button.setText(text)
    button.setAccessibleName(text)


def bold_names(text):
    """The two things to click in Notion, in bold ink, so the sentence can be scanned.
    The last two words stay together: a lone "here." never ends a paragraph."""
    strong = theme.qss_color("ink")
    for word in ("•••", "Connections"):
        text = text.replace(word, '<b style="color: %s;">%s</b>' % (strong, word))
    head, _, last = text.rpartition(" ")
    return "%s %s" % (head, last) if head else text


def _plain(html):
    """The sentence a screen reader reads: no markup, ordinary spaces."""
    return QTextDocumentFragment.fromHtml(html).toPlainText().replace(" ", " ")


def rich_label(text, px=14, weight=400, color="text2", parent=None):
    """A wrapped label of ``text`` with the clickable names in bold; its accessible
    name is the plain sentence (not the markup)."""
    html = bold_names(text)
    lb = text_label(html, px, weight, color, wrap=True, parent=parent, rich=True)
    lb.setAccessibleName(_plain(html))
    return lb


def title_label(text, parent=None):
    """The 26 px headline of every screen (\\n = a line break that balances it)."""
    lb = text_label(text, 26, 700, "ink", display=True, ls=-0.52, wrap=True, parent=parent)
    lb.setAccessibleName(text.replace("\n", " "))
    return lb


class _Badge(QLabel):
    """The 24 px numbered circle of a step."""

    def __init__(self, n, parent=None):
        super().__init__(str(n), parent)
        self.setFixedSize(24, 24)
        self.setAlignment(Qt.AlignCenter)
        self.setFont(theme.font(12.5, 700))
        self.setStyleSheet("QLabel { background: %s; color: %s; border: 1px solid %s; border-radius: 12px; }"
                           % (theme.qss_color("white"), theme.qss_color("ink"), theme.qss_color("line2")))
        self.setAccessibleName("Step %d" % n)


class _Step(QWidget):
    """A numbered step: the badge, the words, and what to do (a button, a field)."""

    def __init__(self, n, text, extra=None, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(_Badge(n, self), 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        self.text = rich_label(text, parent=self)
        self.text.setContentsMargins(0, 2, 0, 0)      # the first line sits on the badge's middle
        col.addWidget(self.text)
        if extra is not None:
            extra.setParent(self)
            if isinstance(extra, QPushButton):
                col.addWidget(extra, 0, Qt.AlignLeft)
            else:
                col.addWidget(extra)
        row.addLayout(col, 1)


class SecretField(PasswordField):
    """The integration secret: one 44 px field with a show / hide eye."""

    def __init__(self, parent=None):
        super().__init__("Integration secret", parent)
        self.label.hide()
        self.edit.setPlaceholderText(SECRET_HINT)
        pal = self.edit.palette()                       # not the OS's grey: it is unreadable on a dark Windows
        pal.setColor(QPalette.PlaceholderText, theme.qcolor("muted"))
        self.edit.setPalette(pal)

    def _show_state(self):
        super()._show_state()
        name = "Hide secret" if self.visible() else "Show secret"
        self.eye.setAccessibleName(name)
        self.eye.setToolTip(name)


class ElidedComboBox(QComboBox):
    """A combo box whose closed label ends in "…" when the chosen text is wider
    than its field: QComboBox cuts it mid-letter. Only the painting changes; the
    item text, currentText() and the popup list are the stock ones."""

    def paintEvent(self, _e):
        p = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        p.drawComplexControl(QStyle.CC_ComboBox, opt)
        field = self.style().subControlRect(QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, self)
        opt.currentText = QFontMetrics(self.font()).elidedText(opt.currentText, Qt.ElideRight,
                                                               max(0, field.width() - 4))
        p.drawControl(QStyle.CE_ComboBoxLabel, opt)


# ── the sheet ────────────────────────────────────────────────────────────────
class NotionSheet(QDialog):
    """Connect Notion. See the module docstring."""

    def __init__(self, host, parent=None, run=None, then="", reason="", connect_fn=None):
        super().__init__(parent)
        self.host = host
        self.then = then or ""
        self.result_sent = False        # True once the caller's ``then`` button was pressed
        self._held = None               # a token with nowhere to send notes yet (kept in memory only)
        self._origin = "intro"          # the screen a failure is shown on
        self._finishing = False
        self._online_ok = False         # Privacy mode: the user already said yes to going online here
        theme.apply(self)
        self.setWindowTitle("Notion")
        self.setMinimumWidth(360)

        self.flow = NotionFlow(host, self, run=run, connect_fn=connect_fn)
        self.flow.waiting.connect(self._on_waiting)
        self.flow.finishing.connect(self._on_finishing)
        self.flow.connected.connect(self._on_connected)
        self.flow.no_page.connect(self._on_no_page)
        self.flow.failed.connect(self._on_failed)
        self.flow.pages_loaded.connect(self._on_pages)
        self.flow.session_lost.connect(self._on_session_lost)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = scroll = QScrollArea(self)
        scroll.setProperty("gusan", "scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setFocusPolicy(Qt.NoFocus)           # not a Tab stop with no ring and no name
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(scroll)
        body = QWidget()
        body.setProperty("gusan", "scrollContent")
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(SIDE - INSET, TOP, SIDE - INSET, BOTTOM)
        root.setSpacing(0)
        self.stack = QStackedWidget(body)
        root.addWidget(self.stack)
        root.addStretch(1)
        for build in (self._build_intro, self._build_waiting, self._build_connected, self._build_manual,
                      self._build_share):
            self.stack.addWidget(build())
        self._primary = {"intro": self.connect_btn, "connected": self.done_btn, "manual": self.manual_btn,
                         "share": self.open_btn}
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(True)          # Enter presses the button the keyboard is on

        self._open_first_screen(reason)

    # ── small builders ──
    @staticmethod
    def _column(parent=None):
        page = QWidget(parent)
        v = QVBoxLayout(page)
        v.setContentsMargins(INSET, 0, INSET, 0)
        v.setSpacing(0)
        return page, v

    @staticmethod
    def _tile(parent=None):
        t = mark_label(48)
        t.setParent(parent)
        return t

    def _message(self, parent, tone="red-tx", bottom=14):
        lb = text_label("", 13, 400, tone, wrap=True, parent=parent)
        lb.setContentsMargins(0, 0, 0, bottom)
        lb.hide()
        return lb

    # ── the five screens ──
    def _build_intro(self):
        page, v = self._column()
        v.addWidget(self._tile(page), 0, Qt.AlignLeft)
        v.addSpacing(20)
        self.intro_title = title_label(INTRO_TITLE, page)
        v.addWidget(self.intro_title)
        v.addSpacing(26)
        self.intro_message = self._message(page)
        v.addWidget(self.intro_message)
        self.connect_btn = Button(CONNECT, "primary", "xl", parent=page, full=True)
        self.connect_btn.clicked.connect(self._connect_clicked)
        v.addWidget(self.connect_btn)
        v.addSpacing(12)
        v.addWidget(text_label(CAPTION, 12.5, 400, "text2", parent=page))
        v.addSpacing(28)
        self.own_link = quiet_link(OWN_LINK, parent=page)
        self.own_link.clicked.connect(lambda: self._show_manual(""))
        v.addLayout(flush_left(self.own_link))
        v.addStretch(1)
        return page

    def _build_waiting(self):
        page, v = self._column()
        v.addWidget(self._tile(page), 0, Qt.AlignLeft)
        v.addSpacing(20)
        self.wait_title = title_label(WAIT_TITLE, page)
        v.addWidget(self.wait_title)
        v.addSpacing(8)
        self.wait_body = text_label(WAIT_BODY, 14, 400, "text2", wrap=True, parent=page)
        v.addWidget(self.wait_body)
        v.addSpacing(22)
        self.progress = ProgressLine(page)
        v.addWidget(self.progress)
        v.addSpacing(22)
        row = PulledRow()                               # the link's text lines up with the text above
        self.reopen_link = ring_room(link_button(REOPEN, 13.5, 600, parent=page), "blue-tx")
        self.reopen_link.clicked.connect(self.flow.reopen)
        row.addWidget(self.reopen_link, 0, Qt.AlignVCenter)
        row.addStretch(1)
        self.cancel_btn = Button("Cancel", "secondary", "m", parent=page)
        self.cancel_btn.clicked.connect(self.flow.cancel)
        row.addWidget(self.cancel_btn)
        for w in (self.reopen_link, self.cancel_btn):
            sp = w.sizePolicy()
            sp.setRetainSizeWhenHidden(True)            # "Setting things up…" keeps the screen's height
            w.setSizePolicy(sp)
        v.addLayout(row)
        v.addStretch(1)
        return page

    def _build_connected(self):
        page, v = self._column()
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(14)
        head.addWidget(self._tile(page), 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        self.name_label = ElidedLabel("", page)
        self.name_label.setFont(theme.font(18, 600, display=True))
        tint(self.name_label, "ink")
        col.addWidget(self.name_label)
        col.addWidget(Chip("Connected", "ok", icon="check", parent=page), 0, Qt.AlignLeft)
        head.addLayout(col, 1)
        v.addLayout(head)
        v.addSpacing(22)

        card = WhiteCard(page, pad=(18, 16), gap=0)
        lay = card.layout()
        a = QHBoxLayout()
        a.setContentsMargins(0, 0, 0, 0)
        a.setSpacing(16)
        txt = QVBoxLayout()
        txt.setContentsMargins(0, 0, 0, 0)
        txt.setSpacing(3)
        txt.addWidget(text_label(AUTO_LABEL, 13.5, 600, "text", wrap=True, parent=card))
        txt.addWidget(text_label(AUTO_HINT, 12, 400, "muted", parent=card))
        a.addLayout(txt, 1)
        self.auto_toggle = Toggle(AUTO_NAME, False, card)
        self.auto_toggle.toggled.connect(self._auto_toggled)
        a.addWidget(self.auto_toggle, 0, Qt.AlignVCenter)
        lay.addLayout(a)
        lay.addSpacing(14)
        lay.addWidget(Divider(card))
        lay.addSpacing(14)
        lay.addWidget(text_label(DEST_LABEL, 13.5, 600, "text", parent=card))
        lay.addSpacing(8)
        pick = QHBoxLayout()
        pick.setContentsMargins(0, 0, 0, 0)
        pick.setSpacing(8)
        self.picker = ElidedComboBox(card)
        self.picker.setAccessibleName(DEST_LABEL)
        self.picker.setFont(theme.font(13.5))
        self.picker.setFixedHeight(36)
        self.picker.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.picker.setMinimumContentsLength(12)
        self.picker.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.picker.setMaxVisibleItems(8)
        self.picker.view().setTextElideMode(Qt.ElideRight)
        self.picker.activated.connect(self._picked)
        pick.addWidget(self.picker, 1)
        self.refresh_btn = IconButton("refresh", "Refresh pages", size=36, framed=True, parent=card)
        self.refresh_btn.clicked.connect(self._refresh_clicked)
        pick.addWidget(self.refresh_btn)
        lay.addLayout(pick)
        # "Search pages": only there when it is needed - a workspace with more pages than
        # the picker lists (the newest PAGE_LIMIT), or while a search is on.
        self.search_row = QWidget(card)
        srow = QVBoxLayout(self.search_row)
        srow.setContentsMargins(0, 8, 0, 0)
        srow.setSpacing(0)
        self.search = QLineEdit()
        self.search.setPlaceholderText(SEARCH_LABEL)
        self.search.setAccessibleName(SEARCH_LABEL)
        pal = self.search.palette()                     # not the OS's grey: unreadable on a dark Windows
        pal.setColor(QPalette.PlaceholderText, theme.qcolor("muted"))
        self.search.setPalette(pal)
        srow.addWidget(FieldBox(self.search, height=36, parent=self.search_row))
        self.search.setFont(theme.font(13.5))
        self.search.installEventFilter(self)            # Enter searches now; it must not press "Done"
        self.search.textChanged.connect(self._search_typed)
        self.search_row.hide()
        lay.addWidget(self.search_row)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._search_fire)
        lay.addSpacing(8)
        self.dest_hint = text_label(DEST_HINT, 12, 400, "muted", wrap=True, parent=card)
        lay.addWidget(self.dest_hint)
        v.addWidget(card)
        v.addSpacing(22)

        foot = PulledRow()                              # Disconnect's text lines up with the card above
        self.disconnect_link = quiet_link("Disconnect", parent=page)
        self.disconnect_link.clicked.connect(self._disconnect)
        foot.addWidget(self.disconnect_link, 0, Qt.AlignVCenter)
        foot.addStretch(1)
        self.done_btn = Button("Done", "primary", "m", parent=page)
        self.done_btn.setMinimumWidth(96)
        self.done_btn.clicked.connect(self._done_clicked)
        foot.addWidget(self.done_btn)
        v.addLayout(foot)
        v.addStretch(1)
        return page

    def _build_manual(self):
        page, v = self._column()
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self._tile(page), 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.back_btn = Button("Back", "ghost", "s", icon="chev_l", parent=page)
        self.back_btn.clicked.connect(self._back)
        top.addWidget(self.back_btn, 0, Qt.AlignVCenter)
        v.addLayout(top)
        v.addSpacing(20)
        v.addWidget(title_label(MANUAL_TITLE, page))
        v.addSpacing(22)
        integrations = Button("Open Notion integrations", "secondary", "s", parent=page)
        integrations.clicked.connect(lambda: _open_url(notion_connect.INTEGRATIONS_URL))
        v.addWidget(_Step(1, STEP_1, integrations, page))
        v.addSpacing(18)
        v.addWidget(_Step(2, STEP_2, None, page))
        v.addSpacing(18)
        self.secret = SecretField(page)
        self.secret.edit.textChanged.connect(self._secret_changed)
        v.addWidget(_Step(3, STEP_3, self.secret, page))
        v.addSpacing(20)
        self.manual_message = self._message(page, bottom=6)
        v.addWidget(self.manual_message)
        v.addSpacing(6)
        self.manual_btn = Button("Connect", "primary", "l", parent=page, full=True)
        self.manual_btn.setEnabled(False)
        self.manual_btn.clicked.connect(self._secret_clicked)
        v.addWidget(self.manual_btn)
        v.addStretch(1)
        return page

    def _build_share(self):
        page, v = self._column()
        v.addWidget(self._tile(page), 0, Qt.AlignLeft)
        v.addSpacing(20)
        v.addWidget(title_label(SHARE_TITLE, page))
        v.addSpacing(10)
        self.share_body = rich_label(SHARE_BODY % APP_NAME, parent=page)
        v.addWidget(self.share_body)
        v.addSpacing(10)
        self.share_message = self._message(page, tone="text2", bottom=0)
        v.addWidget(self.share_message)
        v.addSpacing(12)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.open_btn = Button("Open Notion", "primary", "m", parent=page)
        self.open_btn.clicked.connect(lambda: _open_url(notion_connect.NOTION_HOME_URL))
        row.addWidget(self.open_btn)
        self.check_btn = Button("Check again", "secondary", "m", parent=page)
        self.check_btn.clicked.connect(self._check_again)
        row.addWidget(self.check_btn)
        row.addStretch(1)
        self.share_cancel = quiet_link("Cancel", parent=page)
        self.share_cancel.clicked.connect(self.reject)
        row.addWidget(self.share_cancel, 0, Qt.AlignVCenter)
        v.addLayout(row)
        v.addStretch(1)
        return page

    # ── which screen ──
    def page_name(self):
        return PAGES[self.stack.currentIndex()]

    def _show(self, name):
        self.stack.setCurrentIndex(PAGES.index(name))
        for b in self._primary.values():
            b.setDefault(False)
        if name in self._primary:
            self._primary[name].setDefault(True)
        self._fit()
        if self.isVisible():
            self._focus_primary(name)

    def _focus_primary(self, name=None):
        """The keyboard starts on the screen's main control: the secret field on the
        manual screen, else the primary button (no ring: it is not a Tab)."""
        name = name or self.page_name()
        target = self.secret.edit if name == "manual" else self._primary.get(name)
        if target is not None:
            target.setFocus(Qt.OtherFocusReason)

    def _fit(self):
        """The sheet is as tall as the screen it shows (capped; then it scrolls)."""
        lay = self.stack.currentWidget().layout()
        lay.activate()
        h = (lay.totalHeightForWidth(W - 2 * (SIDE - INSET)) if lay.hasHeightForWidth()
             else lay.sizeHint().height())
        h = max(h, lay.minimumSize().height())
        self.stack.setFixedHeight(h)
        self.setFixedSize(W, min(h + TOP + BOTTOM, MAX_H))
        if self.isVisible():
            # A taller screen after it was shown (the secret screen, an error line) may
            # reach under the taskbar: pull the window back onto the desktop.
            try:
                from ui.winfit import fit_on_screen
                fit_on_screen(self)
            except Exception:
                logger.debug("fit on screen failed", exc_info=True)

    def _open_first_screen(self, reason):
        cfg = self.host.cfg()
        again = reason == "reconnect" or bool(cfg.get("notion_reconnect"))
        if notion_export.is_connected(cfg) and not again:
            self._enter_connected(None)
        elif notion_connect.configured():
            self._show_intro(MESSAGES["reconnect"] if again else "")
        else:
            self._show_manual(MESSAGES["reconnect"] if again else "")

    def _show_intro(self, message=""):
        self._origin = "intro"
        self._set_message(self.intro_message, message)
        set_text(self.connect_btn, TRY_AGAIN if message and message != MESSAGES["reconnect"] else CONNECT)
        self.own_link.setVisible(notion_connect.configured())
        self._show("intro")

    def _show_manual(self, message=""):
        self._origin = "manual"
        self.back_btn.setVisible(notion_connect.configured())
        self._set_message(self.manual_message, message)
        self.secret.set_error(False)
        self._busy_manual(False)
        self._show("manual")

    def _set_message(self, label, text):
        label.setText(text or "")
        label.setVisible(bool(text))

    # ── intro ──
    def _confirm_online(self):
        """Privacy mode keeps everything on this computer: ask before going online
        (once per sheet: a yes covers its later searches and refreshes)."""
        if self._online_ok or not self.host.cfg().get("privacy_mode"):
            return True
        self._online_ok = bool(mini_prompt.ask(self, PRIVACY_TITLE, PRIVACY_BODY, "Connect", "Not now",
                                               icon="incognito"))
        return self._online_ok

    def _connect_clicked(self):
        if not self._confirm_online():
            return
        self._set_message(self.intro_message, "")
        self._origin = "intro"
        self.flow.start_oauth()

    def _on_waiting(self, on):
        if on:
            self._finishing = False
            self._set_waiting_text(False)
            self._show("waiting")
        elif self.page_name() == "waiting":
            self._show_intro("")

    def _on_finishing(self):
        if not self.flow.busy():
            return                      # cancelled a moment ago: this late signal has nothing to show
        self._set_waiting_text(True)
        if self.page_name() != "waiting":
            self._show("waiting")

    def _set_waiting_text(self, finishing):
        self._finishing = finishing
        self.wait_title.setText(FINISH_TITLE if finishing else WAIT_TITLE)
        self.wait_title.setAccessibleName(self.wait_title.text())
        self.wait_body.setText(FINISH_BODY if finishing else WAIT_BODY)
        self.reopen_link.setVisible(not finishing)
        self.cancel_btn.setVisible(not finishing)
        if self.page_name() == "waiting":
            self._fit()

    def _on_failed(self, code, text):
        if self._origin == "manual":
            self._busy_manual(False)
            self.secret.set_error(code == "rejected")
            self._set_message(self.manual_message, text)
            self._show("manual")
        elif self._origin == "share":
            tint(self.share_message, "red-tx")
            self._set_message(self.share_message, text)
            set_text(self.check_btn, "Check again")
            self.check_btn.setEnabled(True)
            self._fit()
        else:
            self._show_intro(text)

    # ── manual ──
    def _secret_changed(self, text):
        self.manual_btn.setEnabled(bool(text.strip()) and not self.flow.busy())
        if self.manual_message.isVisible():
            self.secret.set_error(False)
            self._set_message(self.manual_message, "")
            self._fit()

    def _busy_manual(self, on):
        # Read-only and ignored, never "disabled": Qt greys a disabled icon (the
        # eye, the arrow) from the OS palette, and the sheet must look the same
        # on every Windows theme.
        self.secret.edit.setReadOnly(on)
        self.manual_btn.setEnabled(not on and bool(self.secret.edit.text().strip()))
        set_text(self.manual_btn, "Checking…" if on else "Connect")

    def _back(self):
        if not self.flow.busy():
            self._show("intro")

    def _secret_clicked(self):
        secret = self.secret.edit.text().strip()
        if not secret or not self._confirm_online():
            return
        self._origin = "manual"
        self._set_message(self.manual_message, "")
        self.secret.set_error(False)
        self._busy_manual(True)
        if not self.flow.check_secret(secret):
            self._busy_manual(False)

    # ── a connection with (or without) a destination ──
    def _on_connected(self, res):
        self._busy_manual(False)
        if not _save(self.host, connection_settings(res)):
            self._on_failed("other", MESSAGES["other"])
            return
        self._held = None
        self.secret.edit.clear()
        self._enter_connected(res.get("pages"))
        self.raise_()
        self.activateWindow()

    def _on_no_page(self, res):
        self._busy_manual(False)
        self._held = {k: res.get(k) for k in ("mode", "token", "refresh", "workspace")}
        own = res["mode"] == "own"
        plain = SHARE_BODY_OWN if own else SHARE_BODY % APP_NAME
        html = bold_names(plain)
        self.share_body.setText(html)
        self.share_body.setAccessibleName(_plain(html))
        # Coming back from "Check again" with still nothing: say so.
        tint(self.share_message, "text2")
        self._set_message(self.share_message, SHARE_EMPTY if self.page_name() == "share" else "")
        set_text(self.check_btn, "Check again")
        self.check_btn.setEnabled(True)
        self.secret.edit.clear()
        self._show("share")
        self.raise_()
        self.activateWindow()

    def _check_again(self):
        if self._held is None:
            return
        self.check_btn.setEnabled(False)
        set_text(self.check_btn, "Checking…")
        self._origin = "share"
        if not self.flow.recheck(self._held):
            self.check_btn.setEnabled(True)
            set_text(self.check_btn, "Check again")

    # ── connected ──
    def _enter_connected(self, pages):
        cfg = self.host.cfg()
        self.name_label.setText((cfg.get("notion_workspace_name") or "").strip() or "Notion")
        self.auto_toggle.blockSignals(True)
        self.auto_toggle.setChecked(bool(cfg.get("notion_auto_send")))
        self.auto_toggle.blockSignals(False)
        self.auto_toggle.update()
        self.dest_hint.setText(DEST_HINT_OWN if notion_export.connection_mode(cfg) == "own" else DEST_HINT)
        tint(self.dest_hint, "muted")
        set_text(self.done_btn, self.then or "Done")
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.search_row.setVisible(len(pages or []) >= PAGE_LIMIT)       # a full list (a pasted secret's pages)
        self._fill_picker(pages or [])
        self._show("connected")
        if pages is None:
            if cfg.get("privacy_mode") and not self._online_ok:
                # Opening Manage must not go online by itself: the picker shows what is
                # saved, and Refresh asks first.
                self.dest_hint.setText(DEST_HINT_PAUSED)
                tint(self.dest_hint, "muted")
            else:
                self._reload_pages()

    def _reload_pages(self):
        # No "disabled" look while it runs: Qt greys a disabled icon from the OS
        # palette, and the newest search wins anyway (see NotionFlow.load_pages).
        self.flow.load_pages(self.search.text())

    def _refresh_clicked(self):
        if self._confirm_online():
            self._reload_pages()

    def _search_typed(self, _text):
        self._search_timer.start()                      # a pause in the typing, then one search

    def _search_fire(self):
        if self.host.cfg().get("privacy_mode") and not self._online_ok:
            return                                      # never online behind the user's back
        self._reload_pages()

    def eventFilter(self, obj, ev):                     # noqa: N802 (Qt name)
        if obj is self.search and ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._search_timer.stop()
            self._search_fire()
            return True                                 # not "Done"
        return super().eventFilter(obj, ev)

    def _on_pages(self, pages, code):
        if code:
            self.dest_hint.setText(DEST_HINT_FAILED)
            tint(self.dest_hint, "red-tx")
            return
        self.dest_hint.setText(DEST_HINT_OWN if notion_export.connection_mode(self.host.cfg()) == "own" else DEST_HINT)
        tint(self.dest_hint, "muted")
        self._fill_picker(pages)
        want = bool(self.search.text().strip()) or len(pages) >= PAGE_LIMIT
        if want != self.search_row.isVisibleTo(self):
            self.search_row.setVisible(want)
            self._fit()

    def _on_session_lost(self):
        """Notion refused the saved connection for good: say so and offer to connect again."""
        self._held = None
        if notion_export.connection_mode(self.host.cfg()) == "own":
            self._show_manual(MESSAGES["rejected"])
        elif notion_connect.configured():
            self._show_intro(MESSAGES["reconnect"])
        else:
            self._show_manual(MESSAGES["reconnect"])

    @staticmethod
    def _short(title):
        title = " ".join((title or "").split()) or "Untitled"
        return title if len(title) <= PICK_CHARS else title[:PICK_CHARS - 1].rstrip() + "…"

    def _fill_picker(self, pages):
        """The picker: the Gusan page first (OAuth), the pages the user shared, newest
        edited first; the saved destination is always in it."""
        cfg = self.host.cfg()
        current = notion_export.extract_page_id(cfg.get("notion_parent_page")) or ""
        hub = notion_export.extract_page_id(cfg.get("notion_hub_page")) or ""
        rows = []
        query = self.search.text().strip().lower()
        if hub and (not query or query in notion_connect.HUB_TITLE.lower()):
            rows.append((hub, notion_connect.HUB_TITLE, HUB_LABEL))
        for p in pages:
            if p.get("id") and p["id"] != hub:
                text = ("%s " % p["emoji"] if p.get("emoji") else "") + self._short(p.get("title"))
                rows.append((p["id"], p.get("title") or "Untitled", text))
        if current and current not in [r[0] for r in rows]:
            saved = (cfg.get("notion_parent_title") or "").strip() or "Selected page"
            rows.insert(1 if rows and rows[0][0] == hub else 0, (current, saved, self._short(saved)))
        self.picker.blockSignals(True)
        self.picker.clear()
        for pid, title, text in rows:
            self.picker.addItem(text, (pid, title))
        for i in range(self.picker.count()):
            if self.picker.itemData(i)[0] == current:
                self.picker.setCurrentIndex(i)
        self.picker.blockSignals(False)

    def _picked(self, i):
        data = self.picker.itemData(i)
        if data:
            _save(self.host, {"notion_parent_page": data[0], "notion_parent_title": data[1]})

    def _auto_toggled(self, on):
        try:
            self.host.set_setting("notion_auto_send", bool(on))
        except Exception:
            logger.warning("Saving the Notion switch failed", exc_info=True)

    def _disconnect(self):
        self.flow.drop_pages()
        disconnect_notion(self.host, run=self.flow._run)
        self._show_intro("") if notion_connect.configured() else self._show_manual("")

    def _done_clicked(self):
        self.result_sent = bool(self.then)
        self.accept()

    # ── window ──
    def showEvent(self, ev):
        super().showEvent(ev)
        self._fit()
        try:
            from ui.winfit import settle_on_screen
            settle_on_screen(self)
        except Exception:
            logger.debug("fit on screen failed", exc_info=True)
        QTimer.singleShot(0, self._focus_shown)

    def _focus_shown(self):
        try:
            if self.isVisible():
                self._focus_primary()
        except RuntimeError:
            pass                                        # the sheet was closed before the tick

    def done(self, result):
        # Closing the sheet stops a wait still on, and gives back a grant that has
        # nowhere to put notes.
        self.flow.shutdown()
        held, self._held = self._held, None
        if held and held.get("mode") == "oauth" and held.get("token"):
            self.flow._go(notion_connect.revoke, held["token"])
        super().done(result)


_SHEET = {}


def show_notion_sheet(host, then="", reason=""):
    """Show the one Notion sheet (modal). Returns True when the caller's ``then``
    button was pressed, or - with no ``then`` - when Notion is connected as it closed."""
    cur = _SHEET.get("sheet")
    try:
        if cur is not None and cur.isVisible():
            cur.raise_()
            cur.activateWindow()
            return False
    except RuntimeError:
        pass
    parent = host.window() if callable(getattr(host, "window", None)) else None
    sheet = NotionSheet(host, parent=parent, then=then, reason=reason)
    _SHEET["sheet"] = sheet
    try:
        sheet.exec()
        sent = sheet.result_sent
    finally:
        _SHEET.pop("sheet", None)
        sheet.deleteLater()
    return bool(sent) if then else notion_export.is_connected(host.cfg())
