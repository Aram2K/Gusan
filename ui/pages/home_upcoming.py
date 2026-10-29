"""Home's "Coming up": your next meetings from Google Calendar, right above Recent.

    section = UpcomingSection(home_page)        # what HomePage._ensure_upcoming builds
    section.sync()                              # the one place that reads the calendar and draws

Only built when the app has a calendar service (``app.calendar``: public builds have none until a
Google client is set up, so Home is exactly what it was). One section, one visible state at a time:

    hidden      no service, or a service that is not available
    invite      configured, not connected: "See your meetings here" + Connect (and an error / expiry line)
    connecting  the same card while the browser waits: "Waiting for Google..." + Cancel
    list        connected: up to UPCOMING_LIMIT meetings, soonest first, each with Join and Record
    empty       connected, read once, nothing ahead: "No meetings coming up."
    checking    connected, nothing read yet (Privacy mode or offline at launch): the status, else "Checking..."

What it shows is ``calendar.meetings()`` - the very call the Meetings page's "Coming up" reads - so the
"Only meetings with other people" rule, the dropped ended meetings, the order and the muted ones are the
same by construction; Home filters nothing of its own. Nothing here touches the network: it re-reads the
in-memory list when the service says something (changed / connected / disconnected / connecting / status /
error), when Home opens or shows, when a related setting changes, and on Home's own 60 s tick (so "in 25
min" stays true and an ended meeting drops). Home never asks the service to refresh: "See all" opens
Meetings, which does (once a minute at most).

The actions are the Meetings page's:

    Record  opens Meetings with the start card filled in (title, people, event id, agenda) and does NOT
            start recording: the Record button there does, with its Pro gate and consent notice
            (invite_context.card_prefill is the one definition of that fill).
    Join    opens the call link in the browser (https only; calendar_door.open_join).
    Connect ui/calendar_door.connect_calendar: the disclosure box first, "Not now" does nothing.

Titles and names come from the invite, so they are only ever painted as plain text (Runs) or put in a
tooltip through theme.plain_tip; never markup. Light on purpose: it imports the glyph kit
(ui/components/glyphs.py), not the Meetings page code. ui/pages/home.py imports this module lazily.
"""

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

import calendar_sync
import invite_context
from ui import calendar_door, theme
from ui.components import Button, Card, label
from ui.components.glyphs import GoogleTile, call_mark, google_label
from ui.pages.home import Runs, _Hairline, cfg_of, colored, connect_signals, go, text_label

logger = logging.getLogger("transcribe")

UPCOMING_LIMIT = 3          # meetings shown on Home (the Meetings page has the rest)

TITLE = "Coming up"
SEE_ALL = "See all"
JOIN = "Join"
RECORD = "Record"
EMPTY_TEXT = "No meetings coming up."                       # the Meetings page's own words
CHECKING_TEXT = "Checking your calendar…"
INVITE_TITLE = "See your meetings here"
INVITE_SUB = "Connect Google Calendar to get a reminder to record."
RECORD_TIP = "Opens Meetings, ready to record"

ROW_H = 58                  # a row's height (59 with the hairline above it)
TIME_W = 68                 # the time column


def _utcnow():
    """The section's clock (tests pin it, like meetings._utcnow)."""
    return datetime.now(timezone.utc)


def row_key(m):
    """What a row draws from a meeting (not the time-dependent words, not the bell)."""
    return (m.get("id"), str(m.get("start")), str(m.get("end")), m.get("title"), m.get("people"),
            m.get("call_type"), m.get("link"), len(m.get("others") or []))


def _button(text, kind, px, weight, parent):
    b = Button(text, kind, "s", parent=parent)
    b.setFont(theme.font(px, weight))
    b.setAutoDefault(True)              # Enter presses it as well as Space (a Button outside a dialog ignores Enter)
    return b


class UpcomingRow(_Hairline):
    """One meeting: time and "in 25 min" / "Today", the title, the call and who is on it, Join, Record (58 px)."""

    def __init__(self, section, m, facts, first, nxt, parent):
        super().__init__(parent, first=first)
        self.section = section
        self.meeting = m
        self.facts = facts
        self._hot = None
        self.setFixedHeight(ROW_H if first else ROW_H + 1)      # the 1 px line above counts
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0 if first else 1, 8, 0)
        lay.setSpacing(14)
        tcol = QWidget(self)
        tcol.setFixedWidth(TIME_W)
        tl = QVBoxLayout(tcol)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        tl.addStretch(1)
        self.time_label = text_label(facts["time"], 14, 700, "ink", parent=tcol, tabular=True)
        self.when_label = text_label(facts["label"], 11.5, 600, "muted", parent=tcol)
        for lb in (self.time_label, self.when_label):
            lb.setTextFormat(Qt.PlainText)
            tl.addWidget(lb)
        tl.addStretch(1)
        lay.addWidget(tcol)
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(2)
        body.addStretch(1)
        self.title = Runs([(facts["title"], theme.font(14, 600), "ink")], height=20, parent=self)
        body.addWidget(self.title)
        self.meta = None
        self.call_badge = None
        if facts["meta"]:
            mrow = QHBoxLayout()
            mrow.setContentsMargins(0, 0, 0, 0)
            mrow.setSpacing(6)
            if facts["call_type"]:
                self.call_badge = call_mark(facts["call_type"], 18)
                mrow.addWidget(self.call_badge, 0, Qt.AlignVCenter)
            self.meta = Runs([(facts["meta"], theme.font(12.5, 400), "muted")], height=18, parent=self)
            mrow.addWidget(self.meta, 1)
            body.addLayout(mrow)
        body.addStretch(1)
        lay.addLayout(body, 1)
        short = facts["title"][:40]
        self.join_button = None
        if facts["join"]:
            self.join_button = _button(JOIN, "link", 12.5, 600, self)
            self.join_button.setAccessibleName("%s %s" % (JOIN, short))
            try:
                self.join_button.setToolTip(theme.plain_tip(urlsplit(facts["join"]).hostname or ""))
            except ValueError:
                pass
            self.join_button.clicked.connect(self._join)
            lay.addWidget(self.join_button, 0, Qt.AlignVCenter)
        self.record_button = _button(RECORD, "ghost", 13, 600, self)
        self.record_button.setAccessibleName("%s %s" % (RECORD, short))
        self.record_button.setToolTip(RECORD_TIP)
        self.record_button.clicked.connect(self._record)
        lay.addWidget(self.record_button, 0, Qt.AlignVCenter)
        # Everyone on it, when the line says "+2" (as text: a guest's name can hold markup).
        self.setToolTip(theme.plain_tip(", ".join(facts["names"])) if len(facts["names"]) > 2 else "")
        self.set_when(facts, nxt)

    def set_when(self, facts, nxt):
        """The words that change as time passes (in place: the buttons keep their focus)."""
        self.facts = facts
        self.time_label.setText(facts["time"])
        self.when_label.setText(facts["label"])
        hot = bool(facts["rel"]) and (facts["hot"] or bool(nxt))        # blue: on now, or the next one within the hour
        if hot != self._hot:
            self._hot = hot
            self.when_label.setFont(theme.font(11.5, 700 if hot else 600))
            colored(self.when_label, "blue-tx" if hot else "muted")
        self.setAccessibleName(facts["name"])

    def _record(self):
        self.section.record(self.meeting)

    def _join(self):
        calendar_door.open_join(self.facts["join"])


class UpcomingSection(QWidget):
    """The "Coming up" block on Home. See the module notes."""

    def __init__(self, page, parent=None):
        super().__init__(parent or page)
        self.page = page
        self.app = page.app
        self.rows = []
        self._state = "hidden"
        self._key = None
        self._error = ""                        # the Connect card's line: the last Connect's error / an expiry
        self._tone = "red"
        self._line_shown = None                 # (text, tone) the invitation card says now
        self._failed = False
        self.setAccessibleName(TITLE)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        # ── the header: Coming up · G ········ See all
        self.head = QWidget(self)
        head = QHBoxLayout(self.head)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.title_label = label(TITLE, "h2", self.head)
        head.addWidget(self.title_label, 0, Qt.AlignVCenter)
        head.addWidget(google_label(16, self.head), 0, Qt.AlignVCenter)
        head.addStretch(1)
        self.see_all = _button(SEE_ALL, "link", 13, 600, self.head)
        self.see_all.setAccessibleName("See all meetings")
        self.see_all.clicked.connect(self._see_all)
        head.addWidget(self.see_all, 0, Qt.AlignVCenter)
        col.addWidget(self.head)
        # ── the list card (the look of Recent's)
        self.card = Card(self, pad=(0, 0), gap=0)
        self.card.layout().setContentsMargins(0, 6, 0, 6)
        self.card.setAccessibleName(TITLE)
        self.box = QWidget(self.card)
        self.lay = QVBoxLayout(self.box)
        self.lay.setContentsMargins(6, 0, 6, 0)
        self.lay.setSpacing(0)
        self.card.add(self.box)
        col.addWidget(self.card)
        # ── the invitation card (the Meetings page's Connect card, shorter)
        self.invite = Card(self, pad=(14, 14), gap=0)
        self.invite.layout().setContentsMargins(14, 14, 16, 14)
        self.invite.setAccessibleName(calendar_door.CONNECT_TITLE)
        irow = QHBoxLayout()
        irow.setSpacing(14)
        irow.addWidget(GoogleTile(40, 20, self.invite), 0, Qt.AlignVCenter)
        icol = QVBoxLayout()
        icol.setSpacing(1)
        self.invite_title = text_label(INVITE_TITLE, 14.5, 700, "ink", parent=self.invite)
        icol.addWidget(self.invite_title)
        self.invite_sub = text_label(INVITE_SUB, 13, 400, "muted", parent=self.invite)
        self.invite_sub.setWordWrap(True)
        self.invite_sub.setTextFormat(Qt.PlainText)         # an error line from the service is text, whatever it says
        icol.addWidget(self.invite_sub)
        irow.addLayout(icol, 1)
        self.connect_button = Button("Connect", "secondary", "s", parent=self.invite)
        self.connect_button.setAccessibleName(calendar_door.CONNECT_TITLE)
        self.connect_button.setAutoDefault(True)
        self.connect_button.clicked.connect(self._connect)
        irow.addWidget(self.connect_button, 0, Qt.AlignVCenter)
        self.cancel_button = Button("Cancel", "ghost", "s", parent=self.invite)
        self.cancel_button.setAccessibleName("Cancel connecting Google Calendar")
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.clicked.connect(self._cancel)
        irow.addWidget(self.cancel_button, 0, Qt.AlignVCenter)
        self.invite.add(irow)
        col.addWidget(self.invite)
        self._apply_state("hidden")

    # ── the service ──
    def calendar(self):
        """The app's calendar service when this build has one, else None."""
        return getattr(self.app, "calendar", None)

    def connect_service(self, cal):
        """Listen to ``cal`` (once per signal; the connections go with this widget)."""
        connect_signals(self, (
            (cal, "changed", self.on_changed),
            (cal, "connected", self.on_connected),
            (cal, "disconnected", self.on_disconnected),
            (cal, "connecting", self.on_connecting),
            (cal, "status", self.on_status),
            (cal, "error", self.on_error),
        ))

    def on_changed(self, *_):
        self.sync()

    def on_status(self, *_):
        self.sync()

    def on_connected(self, *_):
        """Connected: whatever error the last tries left is over."""
        self._error, self._tone = "", "red"
        self.sync()

    def on_disconnected(self, reason="", *_):
        """An old error goes away; an expiry says so on the card (a tray balloon alone is easy to miss)."""
        self._error, self._tone = (calendar_door.EXPIRED_NOTE, "muted") if reason == "expired" else ("", "red")
        self.sync()

    def on_connecting(self, on=False, *_):
        if on:
            self._error, self._tone = "", "red"         # a new try: the last one's error is over
        self.sync()

    def on_error(self, text="", *_):
        """The last Connect's error stays on the card until the next try (a read error while
        connected is the status line's business: the card is not on screen then)."""
        self._error, self._tone = str(text or ""), "red"
        self.sync()

    # ── what is on screen ──
    def state(self):
        """hidden | invite | connecting | list | empty | checking."""
        return self._state

    def sync(self):
        """Read the calendar service (memory only) and make the section say what it says."""
        try:
            self._sync()
        except Exception:
            if not self._failed:
                logger.warning("Home's Coming up failed; hiding it", exc_info=True)
            self._failed = True
            self._key = None
            self._apply_state("hidden")

    def _sync(self):
        cal = self.calendar()
        if cal is None or not bool(getattr(cal, "available", False)):
            self._key = None
            self._apply_state("hidden")
            return
        if not getattr(cal, "is_connected", False):
            self._key = None
            self._show_invite(bool(getattr(cal, "is_connecting", False)))
            return
        self._error, self._tone = "", "red"
        now = _utcnow()
        shown = self._read(cal, now)
        status = calendar_door.service_status(cal)
        if shown:
            state = "list"
        elif bool(getattr(cal, "has_read", True)):
            state = "empty"
        else:
            state = "checking"              # nothing read yet: "no meetings" would be a guess
        key = (state, status, tuple(row_key(m) for m, _f in shown))
        if key == self._key and state == self._state and len(self.rows) == len(shown):
            for i, (row, (m, facts)) in enumerate(zip(self.rows, shown)):
                row.meeting = m                 # the latest copy (its agenda) is what Record fills in
                row.set_when(facts, i == 0)
            return
        token = self._focus_token()
        self._key = key
        old = self._detach()
        self._build(state, shown, status)
        self._tab_order()
        self._restore_focus(token)
        self._discard(old)                      # the old rows go last: focus is already on its new place

    @staticmethod
    def _read(cal, now):
        """[(meeting, facts)]: the service's list (the Meetings page's), without any that ended, the first
        UPCOMING_LIMIT. A row that can't be read is skipped."""
        fn = getattr(cal, "meetings", None)
        try:
            raw = list(fn() or []) if callable(fn) else []
        except Exception:
            logger.debug("calendar.meetings failed", exc_info=True)
            raw = []
        out = []
        for m in raw:
            try:
                if not isinstance(m, dict) or m["end"] <= now:
                    continue
                out.append((m, calendar_sync.upcoming_facts(m, now)))
            except Exception:
                logger.debug("Skipped a meeting that couldn't be shown", exc_info=True)
                continue
            if len(out) >= UPCOMING_LIMIT:
                break
        return out

    def _apply_state(self, state):
        self._state = state
        listing = state in ("list", "empty", "checking")
        self.setVisible(state != "hidden")
        self.head.setVisible(listing)
        self.card.setVisible(listing)
        self.invite.setVisible(state in ("invite", "connecting"))
        self.see_all.setVisible(state == "list")
        self.connect_button.setVisible(state == "invite")
        self.cancel_button.setVisible(state == "connecting")

    def _show_invite(self, connecting):
        token = self._focus_token()
        text, tone = calendar_door.line_for(connecting, self._error, self._tone, invite=INVITE_SUB)
        if (text, tone) != self._line_shown:
            self._line_shown = (text, tone)
            self.invite_sub.setText(text)
            colored(self.invite_sub, "red-tx" if tone == "red" else "muted")
        old = self._detach()
        self._apply_state("connecting" if connecting else "invite")
        self._tab_order()
        self._restore_focus(token)
        self._discard(old)

    def _detach(self):
        """Take the rows and lines out of the card (they are discarded once the new ones are in)."""
        old = []
        while self.lay.count():
            w = self.lay.takeAt(0).widget()
            if w is not None:
                old.append(w)
        self.rows = []
        return old

    @staticmethod
    def _discard(widgets):
        for w in widgets:
            w.hide()
            w.deleteLater()

    def _line(self, text, px=13.5, top=14, bottom=10):
        lb = text_label(text, px, 400, "muted", parent=self.box)
        lb.setTextFormat(Qt.PlainText)
        lb.setContentsMargins(8, top, 8, bottom)
        lb.setWordWrap(True)
        self.lay.addWidget(lb)
        lb.show()                               # at once: a widget added to a shown card waits for the event loop otherwise
        return lb

    def _build(self, state, shown, status):
        self._apply_state(state)
        if state == "list":
            for i, (m, facts) in enumerate(shown):
                row = UpcomingRow(self, m, facts, i == 0, i == 0, self.box)
                self.lay.addWidget(row)
                row.show()                      # at once (see _line): Tab order and focus need it shown
                self.rows.append(row)
            if status:
                self._line(status, 12.5, top=10, bottom=6)
        elif state == "empty":
            self._line(EMPTY_TEXT)
            if status:
                self._line(status, 12.5, top=0, bottom=8)
        else:
            self._line(status or CHECKING_TEXT)

    # ── keyboard ──
    def focusables(self):
        """The buttons Tab reaches in this section now, top to bottom."""
        out = [self.see_all, self.connect_button, self.cancel_button]
        for row in self.rows:
            out += [row.join_button, row.record_button]
        return [w for w in out if w is not None and w.isVisibleTo(self)]

    def _tab_order(self):
        """Tab goes tiles -> See all -> Join / Record of each row -> Search -> Recent. Qt's own order is the
        order widgets were made in, and this section is made after the page (and again at each rebuild),
        so without this it comes last."""
        chain = self.focusables()
        if not chain:
            return
        before = (getattr(self.page, "tiles", None) or [None])[-1]
        after = getattr(self.page, "search", None)
        seq = [w for w in [before] + chain + [after] if w is not None]
        for a, b in zip(seq, seq[1:]):
            QWidget.setTabOrder(a, b)

    # ── keyboard focus survives a rebuild ──
    def _focus_token(self):
        if not self.isVisible():
            return None
        try:
            fw = self.window().focusWidget()
        except Exception:
            return None
        if fw is None or not self.isAncestorOf(fw):
            return None
        if fw is self.see_all:
            return ("", "see_all")
        if fw in (self.connect_button, self.cancel_button):
            return ("", "connect")
        for row in self.rows:
            if fw is row.join_button:
                return (row.facts["id"], "join")
            if fw is row.record_button:
                return (row.facts["id"], "record")
        return None

    def _restore_focus(self, token):
        if token is None or not self.isVisible():
            return
        who, what = token
        target = None
        if what == "see_all":
            target = self.see_all
        elif what == "connect":
            target = next((b for b in (self.cancel_button, self.connect_button) if b.isVisibleTo(self)), None)
        else:
            for row in self.rows:
                if row.facts["id"] == who:
                    target = row.join_button if what == "join" and row.join_button is not None else row.record_button
        if target is None and self.see_all.isVisibleTo(self):
            target = self.see_all                   # what had focus is gone: stay in the section
        if target is not None:
            target.setFocus(Qt.TabFocusReason)

    # ── the doors ──
    def record(self, m):
        """Record on a row: Meetings, with the start card filled in with this meeting (it does not start
        recording; the Record button there does, after the Pro gate and the consent notice)."""
        go(self, self.app, "meetings", **invite_context.card_prefill(m))

    def _see_all(self):
        self.page.app_call_open("meetings")

    def _connect(self):
        calendar_door.connect_calendar(self, self.calendar(), cfg_of(self.app).get("privacy_mode"))

    def _cancel(self):
        calendar_door.cancel_connect(self.calendar())


__all__ = ["UpcomingSection", "UpcomingRow", "UPCOMING_LIMIT", "row_key"]
