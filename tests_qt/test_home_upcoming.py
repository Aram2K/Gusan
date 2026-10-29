"""Home's "Coming up" (ui/pages/home_upcoming.py): the next meetings from Google Calendar, right above Recent.

Built offscreen on a real HomePage with a fake calendar service (every signal, the optional parts the
older fakes lack, and a log of what was asked of it), the clock pinned. One test uses the real
CalendarService (with its fake API) inside a real window next to the Meetings page: the founder's ask
is that Home shows the SAME meetings, so that is tested, not assumed.

What is covered: every state (not configured / unavailable, invitation, connecting, errors, list, empty,
checking, paused or retrying), the rows (labels, people, call badge, Join, Record, See all, Connect and
its disclosure), live updates (changed / connected / disconnected / connecting / status / error, the
page opening, a setting, the page shown again, the minute tick), keyboard (Tab order, Enter and Space,
names), text from outside, long titles and a narrow window, 100 and 150 percent, and the weight it adds
to Home (no polling, no network, no Meetings page code).
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices, QFontMetricsF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QWidget

from tests_qt.conftest import make_stub_app
from tests_qt.test_calendar_ui import FakeSession, meetings_root, shell  # noqa: F401 (fixtures + a stand-in session)

AGENDA = "Q3 numbers\nHiring plan\nLaunch checklist"
BODY = ("Gusan reads your calendar to show your next meetings and offer to record them. "
        "It can only read: it never changes your calendar.")
DETAIL = [
    "•  Reads: title, time, guests, agenda and call link of each meeting.",
    "•  Uses them to: show Coming up, remind you to record, and name your meeting.",
    "•  Stays on this computer. Only if you use Live Assistant or AI notes, that meeting's "
    "title, guest names and agenda go to the AI.",
    "•  Disconnect any time in Meeting settings."]
LINK = ("Privacy details: gusan.aibuben.xyz/privacy", "https://gusan.aibuben.xyz/privacy")
INVITE_SUB = "Connect Google Calendar to get a reminder to record."
PAUSED = "Paused in Privacy mode."
RETRY = "Couldn't read Google Calendar. Trying again."
HOSTILE = "<img src=p.png> Review"
HOSTILE_NAME = "J. <img src=p.png>"


# ── a calendar service stand-in ──────────────────────────────────────────────
class FakeCalendar(QObject):
    """Every signal of calendar_service.CalendarService and the parts Home reads; ``calls`` logs
    what was asked of it (Home never asks for a refresh)."""
    changed = Signal(object)
    connected = Signal(str)
    disconnected = Signal(str)
    error = Signal(str)
    remind = Signal(object)
    connecting = Signal(bool)
    status = Signal(str)

    def __init__(self, rows=(), connected=True, status_text="", has_read=True):
        super().__init__()
        self.available = True
        self.is_connected = connected
        self.is_connecting = False
        self.has_read = has_read
        self.email = "aram@aibuben.xyz" if connected else ""
        self.rows = list(rows)
        self._status = status_text
        self.privacy = False
        self.calls = []

    def meetings(self):
        return [dict(m) if isinstance(m, dict) else m for m in self.rows] if self.is_connected else []

    def current_meeting(self, only_meetings=False):
        return None

    def now_or_next(self):
        return "", None

    def next_meeting(self):
        return None

    def status_text(self):
        return self._status

    def refresh(self, force=False):
        self.calls.append(("refresh",))
        return False

    def set_muted(self, event_id, muted):
        self.calls.append(("set_muted", event_id, muted))
        return True

    def connect_calendar(self):
        self.calls.append(("connect",))
        if self.privacy:
            self.error.emit("Turn off Privacy Mode to use Google Calendar.")
            return False
        self.is_connecting = True
        self.connecting.emit(True)
        return True

    def cancel_connect(self):
        self.calls.append(("cancel",))
        self.is_connecting = False
        self.connecting.emit(False)

    def sign_in_done(self, email="aram@aibuben.xyz"):
        """The browser sign-in finished well: connecting(False), then connected(email)."""
        self.is_connecting = False
        self.is_connected = True
        self.connecting.emit(False)
        self.connected.emit(email)

    def disconnect_calendar(self, reason="user"):
        self.is_connected = False
        self.changed.emit([])
        self.disconnected.emit(reason)


def meeting(i, title, start, mins=30, call="Google Meet", others=("Anna", "David"), link="", agenda="",
            muted=False, private=False):
    names = list(others)
    people = ", ".join(names[:2]) + (" +%d" % (len(names) - 2) if len(names) > 2 else "")
    return {"id": "e%d" % i, "title": title, "start": start, "end": start + timedelta(minutes=mins),
            "call_type": call, "people": people, "others": names, "attendees": ["Aram"] + names,
            "link": link, "agenda": agenda, "muted": muted, "private": private, "has_others": bool(names)}


def local(days=0, h=10, m=0):
    """h:m local time, ``days`` from today (aware, in UTC)."""
    base = datetime.now().astimezone().replace(hour=h, minute=m, second=0, microsecond=0)
    return (base + timedelta(days=days)).astimezone(timezone.utc)


def five():
    """The list the tests share: 10:00 is "now"; the first is in 25 minutes."""
    return [meeting(1, "Weekly product sync", local(0, 10, 25), link="https://meet.google.com/abc-defg-hij"),
            meeting(2, "Customer call - Nova", local(0, 14, 0), call="Zoom", others=("Sona", "Hasmik", "Lilit"),
                    link="https://zoom.us/j/123456", agenda=AGENDA),
            meeting(3, "Design review", local(1, 10, 0), call="Teams", others=("Mariam",),
                    link="https://teams.microsoft.com/l/meetup-join/x"),
            meeting(4, "1:1 with Anna", local(1, 14, 30), call="", others=("Anna",)),
            meeting(5, "Board prep", local(3, 9, 0), others=("Anna", "David", "Sona", "Lilit"))]


def _tip_text(widget):
    import html
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", widget.toolTip()))


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _inline_bg(monkeypatch):
    from ui.pages import meeting_views
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)


@pytest.fixture
def now(monkeypatch):
    """The section's clock: 10:00 local today (so "Today" and "Tomorrow" are fixed). Move ``.now``."""
    from ui.pages import home_upcoming
    clock = SimpleNamespace(now=local(0, 10, 0))
    monkeypatch.setattr(home_upcoming, "_utcnow", lambda: clock.now)
    return clock


@pytest.fixture
def made(qapp):
    widgets = []
    yield widgets
    for w in widgets:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


@pytest.fixture
def build(qapp, made, now):
    """build(calendar, tier="pro", width=1100, **cfg) -> (page, app): a shown HomePage with the stub
    controller; ``app.calls`` logs show_main(route, **opts)."""
    from ui.pages import home

    def make(cal="none", tier="pro", width=1100, height=1000, show=True, **cfg):
        app = make_stub_app(tier)
        app.auth.user_id = "u-test"
        app.cfg.update({"whisper_model": "small", "save_history": True, "language": "auto"})
        app.cfg.update(cfg)
        app.show_main = lambda route=None, **opts: app.calls.append(("show_main", route, opts))
        if cal != "none" and cal is not None:
            app.calendar = cal
        page = home.create(app)
        page.sync_io = True
        page.resize(width, height)
        made.append(page)
        if show:
            page.show()
        for _ in range(3):
            QApplication.processEvents()
        page.refresh(sync=True)
        return page, app
    return make


def settle(page):
    for _ in range(3):
        QApplication.processEvents()
    page.layout().activate()


def recent_y(page):
    settle(page)
    return page.recent_title.mapTo(page, QPoint(0, 0)).y()


def _patch_ask(monkeypatch, answer):
    from ui.components import mini_prompt
    asked = []

    def ask(parent, title, body, yes, no, icon="info", private=False, *, detail=None, link=None):
        asked.append((title, body, yes, no, icon, detail, link))
        return answer
    monkeypatch.setattr(mini_prompt, "ask", ask)
    return asked


def _opened_urls(monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()) or True))
    return opened


def _section_of(page):
    sec = page.upcoming
    assert sec is not None
    return sec


def lines(sec):
    """The words the list card says besides its rows (a hidden label is one that was just replaced)."""
    QApplication.processEvents()
    return [lb.text() for lb in sec.box.findChildren(QLabel)
            if lb.text() and lb.parentWidget() is sec.box and not lb.isHidden()]


# ═══════════════════ not configured: nothing at all ═══════════════════
def test_no_calendar_service_means_no_section(build):
    page, app = build(None)
    assert not hasattr(app, "calendar")                  # main.py builds app.calendar only with a Google client
    assert page.upcoming is None
    assert not [w for w in page.findChildren(QWidget) if type(w).__name__ == "UpcomingSection"]
    assert page.recent_title.isVisible()
    # a stray later open changes nothing either
    page.on_open()
    page.on_setting_changed("privacy_mode")
    page.on_setting_changed("calendar_only_with_link")
    assert page.upcoming is None


def test_a_service_that_is_not_available_shows_nothing_and_moves_nothing(build, now):
    baseline, _ = build(None)
    y0 = recent_y(baseline)
    cal = FakeCalendar(five())
    cal.available = False
    page, _app = build(cal)
    sec = _section_of(page)
    assert sec.state() == "hidden" and not sec.isVisibleTo(page)
    assert recent_y(page) == y0                         # a hidden section adds no space (not even its gap)
    # Switched on (a Google client appears): the section is there and Recent moves down.
    cal.available = True
    cal.changed.emit(cal.meetings())
    assert sec.state() == "list" and sec.isVisibleTo(page)
    assert recent_y(page) > y0


def test_a_service_made_after_the_page_is_picked_up_when_home_opens(build, now):
    page, app = build(None)
    assert page.upcoming is None
    app.calendar = FakeCalendar(five())
    page.on_open()
    assert page.upcoming is not None and page.upcoming.state() == "list"
    page.on_open()                                        # opening again builds no second one
    assert len([w for w in page.findChildren(QWidget) if type(w).__name__ == "UpcomingSection"]) == 1


# ═══════════════════ where it sits ═══════════════════
def test_the_section_sits_directly_above_recent(build, now):
    page, _app = build(FakeCalendar(five()))
    sec = _section_of(page)
    settle(page)
    body = page._body
    assert body.indexOf(sec) == 2                         # hero, tiles, Coming up, Recent
    assert body.itemAt(3).layout() is not None and body.itemAt(3).layout().indexOf(page.recent_card) >= 0
    tiles_bottom = max(t.mapTo(page, QPoint(0, 0)).y() + t.height() for t in page.tiles)
    sec_top = sec.mapTo(page, QPoint(0, 0)).y()
    sec_bottom = sec_top + sec.height()
    assert tiles_bottom < sec_top <= tiles_bottom + 40            # the page's 32 px gap
    assert sec_bottom < page.recent_title.mapTo(page, QPoint(0, 0)).y()
    assert recent_y(page) - sec_bottom <= 40                      # ... and 32 px to Recent's title
    # The same width and left edge as Recent's card: one column.
    assert sec.mapTo(page, QPoint(0, 0)).x() == page.recent_card.mapTo(page, QPoint(0, 0)).x()
    assert sec.width() == page.recent_card.width()
    assert sec.card.width() == page.recent_card.width()


def test_how_far_each_state_pushes_recent_down(build, now):
    baseline, _ = build(None)
    y0 = recent_y(baseline)
    for rows, connected, has_read, expect in (
            (five(), True, True, (258, 270)),                 # three rows: header + card + the 32 px gap
            ([], True, True, (119, 131)),                     # one calm line
            ([], False, True, (98, 110))):                    # the invitation card
        page, _app = build(FakeCalendar(rows, connected=connected, has_read=has_read))
        push = recent_y(page) - y0
        assert expect[0] <= push <= expect[1], (len(rows), connected, push)


def test_the_card_has_the_look_of_recents_card(build, now):
    page, _app = build(FakeCalendar(five()))
    sec = _section_of(page)
    assert sec.card.property("gusan") == page.recent_card.property("gusan") == "card"
    assert sec.title_label.font().family() == page.recent_title.font().family()
    assert abs(sec.title_label.font().pointSizeF() - page.recent_title.font().pointSizeF()) < 0.01
    assert sec.title_label.property("textRole") == "h2" == page.recent_title.property("textRole")
    assert sec.title_label.text() == "Coming up"
    assert [r.height() for r in sec.rows] == [58, 59, 59]        # the first has no line above it
    assert sec.card.layout().contentsMargins().top() == 6


# ═══════════════════ the invitation card ═══════════════════
def test_configured_but_not_connected_shows_one_invitation(build):
    cal = FakeCalendar(connected=False)
    page, _app = build(cal)
    sec = _section_of(page)
    assert sec.state() == "invite"
    assert sec.invite.isVisibleTo(page) and not sec.card.isVisibleTo(page) and not sec.head.isVisibleTo(page)
    assert sec.invite_title.text() == "See your meetings here"
    assert sec.invite_sub.text() == INVITE_SUB
    assert sec.connect_button.isVisibleTo(page) and sec.connect_button.text() == "Connect"
    assert not sec.cancel_button.isVisibleTo(page) and not sec.see_all.isVisibleTo(page)
    assert sec.connect_button.accessibleName() == "Connect Google Calendar"
    assert sec.invite.accessibleName() == "Connect Google Calendar"
    assert sec.rows == []
    assert 60 <= sec.invite.height() <= 90                        # one compact card


def test_connect_asks_the_disclosure_first_and_not_now_does_nothing(build, monkeypatch):
    asked = _patch_ask(monkeypatch, False)
    cal = FakeCalendar(connected=False)
    page, _app = build(cal)
    sec = _section_of(page)
    sec.connect_button.click()
    assert cal.calls == []                                        # "Not now": the browser never opens
    assert asked == [("Connect Google Calendar", BODY, "Connect", "Not now", "calendar", DETAIL, LINK)]
    assert sec.state() == "invite"
    asked_yes = _patch_ask(monkeypatch, True)
    sec.connect_button.click()
    assert cal.calls == [("connect",)] and len(asked_yes) == 1
    assert sec.state() == "connecting"
    # Already waiting for the browser: no second question, no second sign-in.
    sec.connect_button.setVisible(True)
    sec.connect_button.click()
    assert len(asked_yes) == 1 and cal.calls == [("connect",)]


def test_connecting_shows_waiting_and_cancel(build):
    cal = FakeCalendar(connected=False)
    page, _app = build(cal)
    sec = _section_of(page)
    cal.connect_calendar()                                        # connecting(True)
    assert sec.state() == "connecting"
    assert sec.invite_sub.text() == "Waiting for Google… Finish in your browser."
    assert sec.cancel_button.isVisibleTo(page) and not sec.connect_button.isVisibleTo(page)
    assert sec.cancel_button.text() == "Cancel"
    assert sec.cancel_button.accessibleName() == "Cancel connecting Google Calendar"
    sec.cancel_button.click()
    assert ("cancel",) in cal.calls
    assert sec.state() == "invite" and sec.invite_sub.text() == INVITE_SUB
    assert sec.connect_button.isVisibleTo(page) and not sec.cancel_button.isVisibleTo(page)


def test_a_page_opened_while_the_browser_waits_says_so(build):
    cal = FakeCalendar(connected=False)
    cal.is_connecting = True
    page, _app = build(cal)
    assert _section_of(page).state() == "connecting"


def test_privacy_mode_skips_the_disclosure_and_the_service_says_so(build, monkeypatch):
    from ui import theme
    asked = _patch_ask(monkeypatch, True)
    cal = FakeCalendar(connected=False)
    cal.privacy = True
    page, app = build(cal, privacy_mode=True)
    sec = _section_of(page)
    sec.connect_button.click()
    assert asked == [] and cal.calls == [("connect",)]            # nothing to ask: the service answers
    assert sec.state() == "invite"
    assert sec.invite_sub.text() == "Turn off Privacy Mode to use Google Calendar."
    assert theme.qss_color("red-tx") in sec.invite_sub.styleSheet()


def test_a_failed_connect_stays_on_the_card_until_the_next_try(build, now):
    from ui import theme
    cal = FakeCalendar(connected=False)
    page, _app = build(cal)
    sec = _section_of(page)
    cal.connecting.emit(True)
    cal.connecting.emit(False)
    cal.error.emit("Couldn't connect Google Calendar - try again.")
    assert sec.invite_sub.text() == "Couldn't connect Google Calendar - try again."
    assert theme.qss_color("red-tx") in sec.invite_sub.styleSheet()
    sec.sync()                                                    # a re-sync doesn't wipe it
    page.on_open()
    assert sec.invite_sub.text() == "Couldn't connect Google Calendar - try again."
    cal.is_connecting = True
    cal.connecting.emit(True)                                     # the next try
    assert sec.invite_sub.text().startswith("Waiting for Google")
    assert theme.qss_color("muted") in sec.invite_sub.styleSheet()
    cal.is_connecting = False
    cal.connecting.emit(False)
    assert sec.invite_sub.text() == INVITE_SUB


def test_a_connect_that_works_leaves_the_card_for_the_list(build, now):
    cal = FakeCalendar([], connected=False, has_read=False)
    page, _app = build(cal)
    sec = _section_of(page)
    cal.error.emit("Couldn't connect Google Calendar - try again.")
    cal.rows = five()
    cal.has_read = True
    cal.sign_in_done()
    assert sec.state() == "list" and not sec.invite.isVisibleTo(page)
    assert [r.facts["id"] for r in sec.rows] == ["e1", "e2", "e3"]
    cal.disconnect_calendar("user")                               # the old error does not come back
    assert sec.state() == "invite" and sec.invite_sub.text() == INVITE_SUB


def test_an_expired_grant_says_so_until_the_next_connect(build, now):
    from ui import theme
    cal = FakeCalendar(five())
    page, _app = build(cal)
    sec = _section_of(page)
    assert sec.state() == "list"
    cal.disconnect_calendar("expired")
    assert sec.state() == "invite"
    assert sec.invite_sub.text() == "Calendar access expired. Connect again."
    assert theme.qss_color("red-tx") not in sec.invite_sub.styleSheet()          # a note, not an error
    sec.sync()
    assert sec.invite_sub.text() == "Calendar access expired. Connect again."
    cal.connect_calendar()
    assert sec.invite_sub.text().startswith("Waiting for Google")
    cal.cancel_connect()
    assert sec.invite_sub.text() == INVITE_SUB
    # A user's own disconnect leaves no expiry line.
    cal.is_connected = True
    cal.connected.emit("aram@aibuben.xyz")
    cal.disconnect_calendar("expired")
    cal.is_connected = True
    cal.connected.emit("aram@aibuben.xyz")
    cal.disconnect_calendar("user")
    assert sec.invite_sub.text() == INVITE_SUB


def test_a_read_error_while_connected_is_not_the_cards_business(build, now):
    cal = FakeCalendar(five())
    page, _app = build(cal)
    sec = _section_of(page)
    cal.error.emit("Couldn't reach Google Calendar - check your internet connection.")
    assert sec.state() == "list"
    cal.disconnect_calendar("user")                                # ... and it is not on the card later
    assert sec.invite_sub.text() == INVITE_SUB


def test_the_error_line_from_the_service_is_plain_text(build, picture):
    cal = FakeCalendar(connected=False)
    page, _app = build(cal)
    sec = _section_of(page)
    cal.error.emit("<img src=p.png> nope")
    assert sec.invite_sub.textFormat() == Qt.PlainText and sec.invite_sub.text() == "<img src=p.png> nope"
    assert QLabel.sizeHint(sec.invite_sub).height() < 100


# ═══════════════════ the list ═══════════════════
def test_connected_with_meetings_shows_the_first_three_soonest_first(build, now):
    cal = FakeCalendar(five())
    page, _app = build(cal)
    sec = _section_of(page)
    assert sec.state() == "list"
    assert [r.facts["id"] for r in sec.rows] == ["e1", "e2", "e3"]
    assert [r.title.text() for r in sec.rows] == ["Weekly product sync", "Customer call - Nova", "Design review"]
    assert sec.head.isVisibleTo(page) and sec.card.isVisibleTo(page) and not sec.invite.isVisibleTo(page)
    assert sec.see_all.isVisibleTo(page) and sec.see_all.text() == "See all"
    assert sec.see_all.accessibleName() == "See all meetings"
    assert sec.accessibleName() == "Coming up" and sec.card.accessibleName() == "Coming up"
    assert "No meetings coming up." not in lines(sec)


def test_the_limit_is_one_constant(build, now, monkeypatch):
    from ui.pages import home_upcoming
    assert home_upcoming.UPCOMING_LIMIT == 3
    monkeypatch.setattr(home_upcoming, "UPCOMING_LIMIT", 4)
    page, _app = build(FakeCalendar(five()))
    assert [r.facts["id"] for r in _section_of(page).rows] == ["e1", "e2", "e3", "e4"]


def test_it_shows_the_service_list_as_it_is_not_its_own_filter(build, now):
    """calendar.meetings() is what Meetings reads: the service applies "Only meetings with other
    people" and drops ended ones; a muted meeting is still in it. Home takes it as it comes."""
    rows = five()
    rows[0]["muted"] = True
    solo = meeting(9, "Lunch (alone)", local(0, 10, 40), call="Google Meet", others=())
    cal = FakeCalendar([rows[0], solo, rows[1]])
    page, _app = build(cal)
    ids = [r.facts["id"] for r in _section_of(page).rows]
    assert ids == ["e1", "e9", "e2"]                              # the muted one and the solo one the service sent


def test_a_meeting_that_ended_is_dropped(build, now):
    ended = meeting(9, "Already over", local(0, 9, 0), mins=30)               # 09:00-09:30, it is 10:00
    running = meeting(8, "Still on", local(0, 9, 45), mins=30)                # until 10:15
    cal = FakeCalendar([ended, running] + five())
    page, _app = build(cal)
    sec = _section_of(page)
    assert [r.facts["id"] for r in sec.rows] == ["e8", "e1", "e2"]
    now.now = local(0, 10, 16)                                                # 10:16: "Still on" is over now
    sec.sync()
    assert [r.facts["id"] for r in sec.rows] == ["e1", "e2", "e3"]


def test_a_meeting_that_cannot_be_read_is_skipped(build, now):
    naive = meeting(7, "Naive", datetime(2026, 1, 1, 10, 0))
    naive["end"] = datetime(2026, 1, 1, 11, 0)                                # no zone: can't compare with now
    no_start = meeting(6, "No start", local(0, 11, 0))
    del no_start["start"]
    cal = FakeCalendar([naive, "junk", None, no_start] + five())
    page, _app = build(cal)
    assert [r.facts["id"] for r in _section_of(page).rows] == ["e1", "e2", "e3"]


def test_a_service_that_raises_is_an_empty_list(build, now):
    cal = FakeCalendar(five())

    def boom():
        raise RuntimeError("no")
    cal.meetings = boom
    page, _app = build(cal)
    assert _section_of(page).state() == "empty"


def test_a_failure_hides_the_section_and_logs_once(build, now, monkeypatch, caplog):
    from ui.pages import home_upcoming
    page, _app = build(FakeCalendar(five()))
    sec = _section_of(page)

    def boom(*a, **k):
        raise RuntimeError("boom")
    with monkeypatch.context() as mp, caplog.at_level(logging.WARNING, logger="transcribe"):
        mp.setattr(home_upcoming.UpcomingSection, "_read", staticmethod(boom))
        sec.sync()
        sec.sync()
        sec.sync()
    assert sec.state() == "hidden" and not sec.isVisibleTo(page)               # never an error card
    assert len([r for r in caplog.records if "Coming up failed" in r.getMessage()]) == 1
    sec.sync()                                                                 # it works again once the fault is gone
    assert sec.state() == "list"


# ═══════════════════ one row ═══════════════════
def test_a_row_says_when_who_and_how(build, now):
    page, _app = build(FakeCalendar(five()))
    r1, r2, r3 = _section_of(page).rows
    assert (r1.time_label.text(), r1.when_label.text()) == ("10:25", "in 25 min")
    assert (r2.time_label.text(), r2.when_label.text()) == ("14:00", "Today")
    assert (r3.time_label.text(), r3.when_label.text()) == ("10:00", "Tomorrow")
    assert r1.title.text() == "Weekly product sync"
    assert r1.meta.text() == "Google Meet · Anna, David"
    assert r2.meta.text() == "Zoom · Sona, Hasmik +1"
    assert r3.meta.text() == "Teams · Mariam"
    for r in (r1, r2, r3):
        assert r.call_badge is not None and r.call_badge.width() == 18


def test_a_later_day_says_its_weekday(build, now):
    page, _app = build(FakeCalendar(five()[4:]))
    row = _section_of(page).rows[0]
    assert row.when_label.text() == (datetime.now().astimezone() + timedelta(days=3)).strftime("%A")


def test_now_and_the_next_one_are_blue_the_rest_quiet(build, now):
    from ui import theme
    blue, muted = theme.qss_color("blue-tx"), theme.qss_color("muted")
    running = meeting(1, "Running", local(0, 9, 50), mins=40)
    soon = meeting(2, "Soon", local(0, 10, 20))
    later = meeting(3, "Later", local(0, 15, 0))
    page, _app = build(FakeCalendar([running, soon, later]))
    r1, r2, r3 = _section_of(page).rows
    assert r1.when_label.text() == "Now" and blue in r1.when_label.styleSheet()
    assert r2.when_label.text() == "in 20 min" and muted in r2.when_label.styleSheet()   # only the first one is "next"
    assert r3.when_label.text() == "Today" and muted in r3.when_label.styleSheet()
    # The first row within the hour is blue as well; a first row that is not near is quiet.
    page2, _ = build(FakeCalendar([soon, later]))
    a, b = _section_of(page2).rows
    assert blue in a.when_label.styleSheet() and muted in b.when_label.styleSheet()
    page3, _ = build(FakeCalendar([later]))
    assert muted in _section_of(page3).rows[0].when_label.styleSheet()


def test_the_call_badge_only_with_a_call_and_no_empty_meta_line(build, now):
    solo = meeting(1, "Quiet block", local(0, 11, 0), call="", others=())
    anna = meeting(2, "1:1 with Anna", local(0, 12, 0), call="", others=("Anna",))
    video = meeting(3, "Call", local(0, 13, 0), call="other", others=())
    page, _app = build(FakeCalendar([solo, anna, video]))
    r_solo, r_anna, r_video = _section_of(page).rows
    assert r_solo.meta is None and r_solo.call_badge is None                    # nothing to say under the title
    assert r_anna.call_badge is None and r_anna.meta.text() == "Anna"
    assert r_video.call_badge is not None and r_video.meta.text() == "Video call"
    assert r_solo.height() == 58 and r_anna.height() == 59


def test_join_only_for_an_https_link_and_it_opens_it(build, now, monkeypatch):
    from ui.pages import home_upcoming
    monkeypatch.setattr(home_upcoming, "UPCOMING_LIMIT", 5)
    opened = _opened_urls(monkeypatch)
    links = ["https://meet.google.com/abc-defg-hij", "http://zoom.us/j/1", "", "javascript:alert(1)",
             "HTTPS://zoom.us/j/9"]
    rows = [meeting(i + 1, t, local(0, 11 + i), link=lk) for i, (t, lk) in enumerate(zip("ABCDE", links))]
    page, app = build(FakeCalendar(rows))
    sec = _section_of(page)
    assert [r.join_button is not None for r in sec.rows] == [True, False, False, False, True]
    assert _tip_text(sec.rows[0].join_button) == "meet.google.com"
    assert sec.rows[0].join_button.accessibleName() == "Join A"
    sec.rows[0].join_button.click()
    assert opened == ["https://meet.google.com/abc-defg-hij"]
    sec.rows[4].join_button.click()
    assert opened[-1].lower() == "https://zoom.us/j/9"
    assert [c for c in app.calls if c[0] == "show_main"] == []                  # Join is not Record: Home stays


def test_record_opens_meetings_with_the_card_filled_in_and_does_not_record(build, now):
    page, app = build(FakeCalendar(five()))
    sec = _section_of(page)
    sec.rows[1].record_button.click()
    assert app.calls[-1] == ("show_main", "meetings", {
        "title": "Customer call - Nova", "attendees": ["Sona", "Hasmik", "Lilit"],
        "calendar_event_id": "e2", "agenda": AGENDA})
    assert "record" not in app.calls[-1][2]                                     # opening Meetings is all it does
    assert not [c for c in app.calls if c[0] in ("_pro_upsell", "set_setting")]


def test_record_sends_the_same_fill_as_a_meetings_row(build, now):
    from invite_context import card_prefill
    rows = five()
    page, app = build(FakeCalendar(rows))
    sec = _section_of(page)
    for row, m in zip(sec.rows, rows):
        row.record_button.click()
        assert app.calls[-1] == ("show_main", "meetings", card_prefill(m))


def test_record_on_a_private_event_sends_no_agenda_and_no_title_says_meeting(build, now):
    private = meeting(1, "Private one", local(0, 11, 0), agenda="secret", private=True)
    untitled = meeting(2, "", local(0, 12, 0), agenda="x")
    page, app = build(FakeCalendar([private, untitled]))
    sec = _section_of(page)
    assert sec.rows[1].title.text() == "Untitled meeting"
    sec.rows[0].record_button.click()
    assert app.calls[-1][2]["agenda"] == "" and app.calls[-1][2]["title"] == "Private one"
    sec.rows[1].record_button.click()
    assert app.calls[-1][2]["title"] == "Meeting" and app.calls[-1][2]["calendar_event_id"] == "e2"


def test_record_sends_no_email_and_no_link(build, now):
    m = meeting(1, "Sync", local(0, 11, 0), others=("Anna <anna@example.com>", "dave@example.com"),
                link="https://meet.google.com/abc")
    page, app = build(FakeCalendar([m]))
    _section_of(page).rows[0].record_button.click()
    opts = app.calls[-1][2]
    assert "@" not in repr(opts) and "meet.google.com" not in repr(opts)
    assert opts["attendees"] == ["Anna", "Dave"]


def test_record_uses_the_newest_copy_of_the_meeting(build, now):
    rows = five()
    cal = FakeCalendar(rows)
    page, app = build(cal)
    sec = _section_of(page)
    row = sec.rows[0]
    cal.rows[0]["agenda"] = "Changed upstream"                                  # nothing the row shows changed
    cal.changed.emit(cal.meetings())
    assert sec.rows[0] is row                                                    # no rebuild ...
    row.record_button.click()
    assert app.calls[-1][2]["agenda"] == "Changed upstream"                      # ... and Record has the newest


def test_see_all_opens_meetings(build, now):
    page, app = build(FakeCalendar(five()))
    _section_of(page).see_all.click()
    assert app.calls[-1] == ("show_main", "meetings", {})


def test_people_tooltip_has_everyone_when_the_label_says_plus_n(build, now):
    page, _app = build(FakeCalendar(five()))
    r1, r2, _r3 = _section_of(page).rows
    assert _tip_text(r2) == "Sona, Hasmik, Lilit"
    assert "Sona, Hasmik +1" in r2.accessibleName()
    assert r1.toolTip() == ""                                                    # two names: the line has them both


def test_the_record_button_says_where_it_goes(build, now):
    page, _app = build(FakeCalendar(five()))
    row = _section_of(page).rows[0]
    assert row.record_button.text() == "Record" and row.record_button.toolTip() == "Opens Meetings, ready to record"
    assert row.join_button.text() == "Join"


# ═══════════════════ empty, checking, status ═══════════════════
def test_connected_with_nothing_ahead_says_so_calmly(build, now):
    page, _app = build(FakeCalendar([]))
    sec = _section_of(page)
    assert sec.state() == "empty"
    assert sec.head.isVisibleTo(page) and not sec.see_all.isVisibleTo(page)
    said = lines(sec)
    assert said == ["No meetings coming up."]
    assert sec.rows == []


def test_nothing_read_yet_never_says_no_meetings(build, now):
    cal = FakeCalendar([], has_read=False)
    page, _app = build(cal)
    sec = _section_of(page)
    assert sec.state() == "checking"
    assert lines(sec) == ["Checking your calendar…"]
    # Privacy mode at launch: the service's why, not a guess.
    cal._status = PAUSED
    cal.status.emit(PAUSED)
    assert lines(sec) == [PAUSED]
    # The first read landed, nothing on.
    cal._status = ""
    cal.has_read = True
    cal.changed.emit([])
    assert sec.state() == "empty"
    assert lines(sec) == ["No meetings coming up."]


def test_a_service_that_cannot_read_says_it_is_retrying(build, now):
    cal = FakeCalendar([], status_text=RETRY, has_read=False)
    page, _app = build(cal)
    sec = _section_of(page)
    assert lines(sec) == [RETRY]
    # Once read, with nothing ahead: both lines.
    cal.has_read = True
    cal.changed.emit([])
    assert lines(sec) == ["No meetings coming up.", RETRY]


def test_the_status_line_sits_under_the_rows_and_goes_with_the_next_good_read(build, now):
    cal = FakeCalendar(five())
    page, _app = build(cal)
    sec = _section_of(page)
    cal._status = PAUSED
    cal.status.emit(PAUSED)
    assert lines(sec) == [PAUSED]
    assert len(sec.rows) == 3                                                    # the cached list stays
    cal._status = RETRY
    cal.status.emit(RETRY)
    assert lines(sec) == [RETRY]
    cal._status = ""
    cal.status.emit("")
    assert lines(sec) == []
    assert len(sec.rows) == 3 and sec.state() == "list"


def test_the_status_line_is_below_the_last_row(build, now):
    cal = FakeCalendar(five(), status_text=PAUSED)
    page, _app = build(cal)
    sec = _section_of(page)
    settle(page)
    line = next(lb for lb in sec.box.findChildren(QLabel) if lb.text() == PAUSED and not lb.isHidden())
    assert line.mapTo(page, QPoint(0, 0)).y() > sec.rows[-1].mapTo(page, QPoint(0, 0)).y()
    assert sec.state() == "list"


def test_an_older_service_without_the_optional_parts_is_an_empty_list(build, now):
    class Old(QObject):
        changed = Signal(list)

        def __init__(self):
            super().__init__()
            self.available = True
            self.is_connected = True

        def next_meeting(self):
            return None
    page, _app = build(Old())
    sec = _section_of(page)
    assert sec.state() == "empty"
    assert lines(sec) == ["No meetings coming up."]
    page.on_open()                                                              # and never raises


# ═══════════════════ it follows the service ═══════════════════
def test_changed_connected_and_disconnected_move_the_section(build, now):
    cal = FakeCalendar([], connected=False)
    page, _app = build(cal)
    sec = _section_of(page)
    assert sec.state() == "invite"
    cal.rows = five()[:2]
    cal.sign_in_done()                                                          # connecting(False), connected(email)
    assert sec.state() == "list" and [r.facts["id"] for r in sec.rows] == ["e1", "e2"]
    cal.rows = five()                                                           # a new meeting: changed(list)
    cal.changed.emit(cal.meetings())
    assert [r.facts["id"] for r in sec.rows] == ["e1", "e2", "e3"]
    cal.rows = []
    cal.changed.emit([])
    assert sec.state() == "empty"
    cal.disconnect_calendar("user")                                             # changed([]) then disconnected
    assert sec.state() == "invite" and not sec.card.isVisibleTo(page)


def test_a_meeting_that_arrives_after_connect_replaces_checking(build, now):
    cal = FakeCalendar([], connected=False)
    page, _app = build(cal)
    sec = _section_of(page)
    cal.has_read = False
    cal.sign_in_done()
    assert sec.state() == "checking"                                            # connected, nothing read yet
    cal.rows = five()
    cal.has_read = True
    cal.changed.emit(cal.meetings())
    assert sec.state() == "list"


def test_opening_home_reads_the_service_again(build, now):
    cal = FakeCalendar(five()[:1])
    page, _app = build(cal)
    sec = _section_of(page)
    cal.rows = five()                                                           # changed while Home was away: no signal
    page.on_open()
    assert len(sec.rows) == 3


def test_a_setting_that_changes_the_list_or_the_line_is_heard(build, now):
    cal = FakeCalendar(five()[:1])
    page, _app = build(cal)
    sec = _section_of(page)
    cal._status = PAUSED                                                        # Privacy mode just went on
    page.on_setting_changed("privacy_mode")
    assert PAUSED in lines(sec)
    cal._status = ""
    page.on_setting_changed("privacy_mode")
    assert PAUSED not in lines(sec)
    cal.rows = five()                                                           # "Only meetings with other people" flipped
    page.on_setting_changed("calendar_only_with_link")
    assert len(sec.rows) == 3
    cal.rows = five()[:2]
    page.on_setting_changed("calendar_ask_to_record")
    assert len(sec.rows) == 2
    cal.rows = five()
    page.on_setting_changed("hotkey")                                           # not its business
    assert len(sec.rows) == 2


def test_showing_home_again_reads_the_list_again(build, now):
    cal = FakeCalendar(five())
    page, _app = build(cal)
    sec = _section_of(page)
    page.hide()
    cal.rows = five()[:1]                                                       # the tray kept the window hidden meanwhile
    cal._status = PAUSED
    page.show()
    QApplication.processEvents()
    assert len(sec.rows) == 1
    assert PAUSED in lines(sec)


def test_a_minute_tick_keeps_the_rows_and_refreshes_the_words(build, now):
    cal = FakeCalendar(five())
    page, _app = build(cal)
    sec = _section_of(page)
    rows = list(sec.rows)
    assert rows[0].when_label.text() == "in 25 min"
    now.now = local(0, 10, 1)
    page._on_minute()
    assert sec.rows == rows and all(a is b for a, b in zip(sec.rows, rows))      # the same widgets
    assert rows[0].when_label.text() == "in 24 min"
    assert rows[0].accessibleName().startswith("Weekly product sync, 10:25, in 24 min")
    now.now = local(0, 10, 25)
    page._on_minute()
    assert rows[0].when_label.text() == "Now"
    assert all(a is b for a, b in zip(sec.rows, rows))
    # Time moves on past the first meeting's end: it drops, the rest stays.
    now.now = local(0, 10, 56)
    page._on_minute()
    assert [r.facts["id"] for r in sec.rows] == ["e2", "e3", "e4"]


def test_a_minute_tick_is_the_pages_own_timer_while_it_is_shown(build, now):
    page, _app = build(FakeCalendar(five()))
    assert page._minute.isActive() and page._minute.interval() == 60_000
    page.hide()
    assert not page._minute.isActive()
    page.show()
    assert page._minute.isActive()


def test_the_chip_follows_the_same_tick(build, now):
    """The "Next:" chip used to own the 60 s timer (it ran only while the chip showed); both use it now."""
    cal = FakeCalendar(five())
    nxt = {"v": ("next", five()[0])}
    cal.now_or_next = lambda: nxt["v"]
    page, _app = build(cal)
    assert not page.next_chip.isHidden()
    nxt["v"] = ("", None)
    page._on_minute()
    assert page.next_chip.isHidden()


def test_only_the_bell_changed_keeps_the_rows_and_their_focus(build, now):
    cal = FakeCalendar(five())
    page, _app = build(cal)
    sec = _section_of(page)
    rows = list(sec.rows)
    sec.rows[1].record_button.setFocus(Qt.TabFocusReason)
    cal.rows[1]["muted"] = True                                                 # a bell on the Meetings page
    cal.changed.emit(cal.meetings())
    assert all(a is b for a, b in zip(sec.rows, rows))
    assert page.focusWidget() is sec.rows[1].record_button
    assert [r.facts["id"] for r in sec.rows] == ["e1", "e2", "e3"]               # muted meetings stay on Home


def test_a_real_change_rebuilds_but_the_focused_button_keeps_focus(build, now):
    cal = FakeCalendar(five())
    page, _app = build(cal)
    sec = _section_of(page)
    old = sec.rows[0]
    old.record_button.setFocus(Qt.TabFocusReason)
    assert page.focusWidget() is old.record_button
    cal.rows = [meeting(9, "New first", local(0, 10, 10))] + five()              # a meeting added before it
    cal.changed.emit(cal.meetings())
    assert [r.facts["id"] for r in sec.rows] == ["e9", "e1", "e2"]
    assert sec.rows[1] is not old
    assert page.focusWidget() is sec.rows[1].record_button                       # still on "Weekly product sync"
    sec.rows[1].join_button.setFocus(Qt.TabFocusReason)
    cal.rows = five()
    cal.changed.emit(cal.meetings())
    assert page.focusWidget() is sec.rows[0].join_button
    sec.see_all.setFocus(Qt.TabFocusReason)
    cal.rows = five()[:2]
    cal.changed.emit(cal.meetings())
    assert page.focusWidget() is sec.see_all
    # The meeting that had focus is gone: focus stays in the section.
    sec.rows[1].record_button.setFocus(Qt.TabFocusReason)
    cal.rows = five()[:1]
    cal.changed.emit(cal.meetings())
    assert page.focusWidget() is sec.see_all


def test_focus_follows_connect_to_cancel(build, now):
    cal = FakeCalendar(connected=False)
    page, _app = build(cal)
    sec = _section_of(page)
    sec.connect_button.setFocus(Qt.TabFocusReason)
    cal.connect_calendar()                                                      # the card swaps Connect for Cancel
    assert sec.state() == "connecting" and page.focusWidget() is sec.cancel_button


def test_home_never_asks_the_service_to_refresh_and_has_no_timer_of_its_own(build, now):
    cal = FakeCalendar(five())
    page, app = build(cal)
    sec = _section_of(page)
    assert sec.findChildren(QTimer) == []
    page.on_open()
    page._on_minute()
    page.on_setting_changed("calendar_only_with_link")
    cal.changed.emit(cal.meetings())
    cal.status.emit(PAUSED)
    sec.see_all.click()
    sec.rows[0].record_button.click()
    page.hide()
    page.show()
    assert ("refresh",) not in cal.calls                                         # no network from Home, ever


def test_service_signals_reach_the_section_once_and_go_with_the_page(qapp, now):
    from PySide6.QtCore import SIGNAL, QEvent
    from ui.pages import home
    cal = FakeCalendar(five())
    app = make_stub_app("pro")
    app.cfg.update({"save_history": True})
    app.show_main = lambda *a, **k: None
    app.calendar = cal
    for _ in range(2):
        page = home.create(app)
        page.sync_io = True
        page.on_open()
        page.on_open()
        assert cal.receivers(SIGNAL("changed(PyObject)")) == 2                  # the page's chip + the section
        for sig in ("connected(QString)", "disconnected(QString)", "connecting(bool)", "status(QString)",
                    "error(QString)"):
            assert cal.receivers(SIGNAL(sig)) == 1, sig                          # the section, once however often it opens
        page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        for sig in ("changed(PyObject)", "connected(QString)", "disconnected(QString)", "connecting(bool)",
                    "status(QString)", "error(QString)"):
            assert cal.receivers(SIGNAL(sig)) == 0, sig
    cal.changed.emit(cal.meetings())                                            # nobody left to call: no error
    cal.status.emit("x")
    cal.disconnect_calendar("user")


# ═══════════════════ text from outside ═══════════════════
@pytest.fixture
def picture(tmp_path, monkeypatch):
    """p.png (150 x 150, red) beside the working directory, where Qt looks for a relative src."""
    from PySide6.QtGui import QColor, QImage
    img = QImage(150, 150, QImage.Format_RGB32)
    img.fill(QColor("red"))
    assert img.save(str(tmp_path / "p.png"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_hostile_title_and_names_are_text_everywhere_in_the_row(build, now, picture):
    import html
    rows = [meeting(1, HOSTILE, local(0, 10, 20), others=(HOSTILE_NAME, "<b>x</b>", "Anna", "David"),
                    link="https://<b>x</b>.example/j/1"),
            meeting(2, "<b>Bold</b> & <i>it</i>", local(0, 12, 0), others=("<script>x</script>",))]
    page, _app = build(FakeCalendar(rows))
    sec = _section_of(page)
    for row in sec.rows:
        assert row.height() in (58, 59)                                         # no picture made it taller
        for lb in row.findChildren(QLabel):
            if lb.text():
                assert lb.textFormat() == Qt.PlainText
                assert QLabel.sizeHint(lb).height() < 100
    r1 = sec.rows[0]
    assert r1.title.text() == HOSTILE and "<img" in r1.meta.text()               # painted as they are (Runs)
    tip = r1.toolTip()                                                           # everyone, as escaped text
    assert tip.startswith("<p") and html.escape(HOSTILE_NAME, quote=False) in tip
    assert "<img" not in tip and "<b>" not in tip
    assert r1.accessibleName().startswith(HOSTILE)                               # a screen reader gets the words
    if r1.join_button is not None:
        jt = r1.join_button.toolTip()
        assert "<b>" not in jt and (not jt or jt.startswith("<p"))
    assert sec.rows[1].title.text() == "<b>Bold</b> & <i>it</i>"


# ═══════════════════ size ═══════════════════
def test_a_long_title_elides_and_the_buttons_keep_their_room(build, now):
    long_title = "Quarterly planning with the whole leadership team and a very long title " * 2
    assert len(long_title) > 110
    rows = [meeting(1, long_title.strip(), local(0, 10, 20), link="https://meet.google.com/abc",
                    others=("Anna", "David", "Sona", "Lilit", "Mariam")), five()[1]]
    page, _app = build(FakeCalendar(rows), width=718)                             # the 960 px window's page
    sec = _section_of(page)
    settle(page)
    row = sec.rows[0]
    assert row.title.text() == long_title.strip()                                 # the text is whole
    assert row.title.accessibleName() == long_title.strip()
    from ui import theme
    fm = QFontMetricsF(theme.font(14, 600))
    assert row.title.width() < fm.horizontalAdvance(long_title.strip())           # it is drawn cut with …
    assert row.height() == 58
    assert row.record_button.geometry().right() <= row.width()
    assert row.join_button.geometry().right() < row.record_button.geometry().left()
    for b in (row.join_button, row.record_button):
        assert b.width() >= b.sizeHint().width()                                  # never shrunk
    assert row.title.geometry().right() < row.join_button.geometry().left()
    assert row.title.width() > 250                                                # most of the line is the title
    assert row.title.toolTip()                                                    # the whole title, on hover


def test_narrow_and_wide_pages_follow_the_column(build, now):
    for width in (718, 900, 1100, 1400):
        page, _app = build(FakeCalendar(five()), width=width)
        sec = _section_of(page)
        settle(page)
        assert sec.width() == page.width() - 64                                   # the body's 32 px margins
        assert sec.card.width() == sec.width()
        for row in sec.rows:
            assert row.width() <= sec.card.width()
            assert row.record_button.geometry().right() <= row.width()
            assert row.height() in (58, 59)
        assert sec.mapTo(page, QPoint(0, 0)).x() == 32


def test_the_invitation_card_fits_a_narrow_page(build, now):
    cal = FakeCalendar(connected=False)
    cal.is_connecting = True
    page, _app = build(cal, width=718)
    sec = _section_of(page)
    settle(page)
    assert sec.invite.width() == sec.width() == 654
    assert sec.cancel_button.geometry().right() <= sec.invite.width()
    assert sec.invite_sub.sizeHint().height() < 80


# ═══════════════════ the keyboard and screen readers ═══════════════════
def _walk_tab_order(page, start):
    """Every widget Tab reaches (one lap of the focus chain from ``start``), as they come."""
    QApplication.processEvents()
    out = []
    w = start
    for _ in range(600):
        if w.focusPolicy() & Qt.TabFocus and w.isVisibleTo(page) and w.isEnabled():
            out.append(w)
        w = w.nextInFocusChain()
        if w is start:
            break
    return out


@pytest.fixture
def one_dictation():
    """One entry in Recent (the throwaway data folder's history), taken away again after."""
    import history
    history.save_all([{"text": "Hi Anna", "language": "en", "backend": "local",
                       "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}])
    yield
    history.save_all([])


def test_tab_goes_tiles_then_coming_up_then_search_then_recent(build, now, one_dictation):
    page, _app = build(FakeCalendar(five()))
    page.refresh(sync=True)
    QApplication.processEvents()
    sec = _section_of(page)
    order = _walk_tab_order(page, page.tiles[0])
    expected = list(page.tiles) + [sec.see_all]
    for row in sec.rows:
        expected += [row.join_button, row.record_button]
    expected += [page.search]
    assert order[:len(expected)] == expected
    recent_buttons = [b for b in page.recent_card.findChildren(QAbstractButton)
                      if b.focusPolicy() & Qt.TabFocus and b.isVisibleTo(page)]
    assert recent_buttons and set(order[len(expected):]) >= set(recent_buttons)


def test_tab_order_after_a_rebuild_and_for_the_invitation(build, now):
    cal = FakeCalendar(five())
    page, _app = build(cal)
    sec = _section_of(page)
    cal.rows = five()[2:5]
    cal.changed.emit(cal.meetings())                                              # new rows are made: new widgets
    order = _walk_tab_order(page, page.tiles[0])
    expected = list(page.tiles) + [sec.see_all] + [b for r in sec.rows for b in (r.join_button, r.record_button)
                                                   if b is not None] + [page.search]
    assert order[:len(expected)] == expected
    cal.disconnect_calendar("user")
    order = _walk_tab_order(page, page.tiles[0])
    assert order[:len(page.tiles) + 2] == list(page.tiles) + [sec.connect_button, page.search]


def test_every_button_is_a_tab_stop_with_a_name(build, now):
    page, _app = build(FakeCalendar(five()))
    sec = _section_of(page)
    named = {"See all meetings": sec.see_all}
    for row in sec.rows:
        title = row.facts["title"][:40]
        named["Record " + title] = row.record_button
        if row.join_button is not None:
            named["Join " + title] = row.join_button
    assert len(named) == 1 + 3 + 3
    for name, b in named.items():
        assert b.accessibleName() == name, name
        assert b.focusPolicy() == Qt.TabFocus
        assert b.autoDefault()
    r1 = sec.rows[0]
    assert r1.accessibleName() == "Weekly product sync, 10:25, in 25 min, with Anna, David"
    assert sec.rows[1].accessibleName() == "Customer call - Nova, 14:00, Today, with Sona, Hasmik +1"
    assert [w for w in (r1,) if w.focusPolicy() != Qt.NoFocus] == []               # rows themselves are not stops


@pytest.mark.parametrize("key", [Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter])
def test_space_and_enter_press_every_button(build, now, monkeypatch, key):
    opened = _opened_urls(monkeypatch)
    asked = _patch_ask(monkeypatch, True)
    page, app = build(FakeCalendar(five()))
    sec = _section_of(page)
    QTest.keyClick(sec.see_all, key)
    assert app.calls[-1] == ("show_main", "meetings", {})
    QTest.keyClick(sec.rows[0].record_button, key)
    assert app.calls[-1][2]["calendar_event_id"] == "e1"
    QTest.keyClick(sec.rows[0].join_button, key)
    assert opened == ["https://meet.google.com/abc-defg-hij"]
    cal = FakeCalendar(connected=False)
    page2, _ = build(cal)
    QTest.keyClick(_section_of(page2).connect_button, key)
    assert cal.calls == [("connect",)] and len(asked) == 1


def test_names_on_the_invitation_card(build, now):
    page, _app = build(FakeCalendar(connected=False))
    sec = _section_of(page)
    assert sec.connect_button.focusPolicy() == Qt.TabFocus and sec.connect_button.autoDefault()
    assert sec.invite.accessibleName() == "Connect Google Calendar"


# ═══════════════════ the weight it adds ═══════════════════
def test_home_does_not_load_the_meetings_page_code():
    """Importing meeting_views costs more than a second on a slow machine; Home's section needs only the glyph kit."""
    code = ("import sys; import ui.pages.home_upcoming, ui.calendar_door, ui.components.glyphs; "
            "bad = [m for m in ('ui.pages.meeting_views', 'ui.pages.meetings', 'ui.pages.meeting_delete', "
            "'ui.notion_sheet') if m in sys.modules]; print('BAD', bad)")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180,
                         env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert res.returncode == 0, res.stderr[-800:]
    assert "BAD []" in res.stdout, res.stdout


def test_the_meetings_names_it_moved_are_still_there():
    from ui import calendar_door
    from ui.components import glyphs
    from ui.pages import meeting_views, meetings
    for name in ("CONNECT_TITLE", "CONNECT_BODY", "CONNECT_DETAIL", "CONNECT_LINK", "EXPIRED_NOTE"):
        assert getattr(meetings, name) == getattr(calendar_door, name)
    for name in ("_dpr", "_CACHE", "_GLYPHS", "glyph", "icon_label", "dot_pixmap", "dot_label", "google_pixmap",
                 "google_label", "GoogleTile", "CALL_TONES", "call_name", "call_mark"):
        assert getattr(meeting_views, name) is getattr(glyphs, name), name
    assert meeting_views.call_name("other") == "Video call" and meeting_views.call_name("Zoom") == "Zoom"
    assert meeting_views.call_name("") == ""


# ═══════════════════ 100 and 150 percent ═══════════════════
PROBE = r"""
import json, os, sys
ROOT = __ROOT__
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from datetime import datetime, timedelta, timezone
from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtWidgets import QApplication
from tests_qt.conftest import make_stub_app
app_q = QApplication.instance() or QApplication([])
from ui import theme
theme.install_light_scheme(app_q)
theme.load_fonts()
from ui.pages import home

class Cal(QObject):
    changed = Signal(object); connected = Signal(str); disconnected = Signal(str); error = Signal(str)
    connecting = Signal(bool); status = Signal(str)
    def __init__(self, rows, connected=True):
        super().__init__()
        self.available = True; self.is_connected = connected; self.is_connecting = False; self.has_read = True
        self.rows = rows
    def meetings(self): return [dict(m) for m in self.rows]
    def status_text(self): return ""

def at(days, h, m=0):
    base = datetime.now().astimezone().replace(hour=h, minute=m, second=0, microsecond=0)
    return (base + timedelta(days=days)).astimezone(timezone.utc)

def mt(i, title, start, link="", call="Google Meet"):
    return {"id": "e%d" % i, "title": title, "start": start, "end": start + timedelta(minutes=30), "call_type": call,
            "people": "Anna, David +2", "others": ["Anna", "David", "Sona", "Lilit"], "link": link, "agenda": "",
            "muted": False, "private": False, "has_others": True}

out = {"dpr": app_q.primaryScreen().devicePixelRatio()}
soon = datetime.now(timezone.utc) + timedelta(minutes=25)
for name, cal, width in (("list", Cal([mt(1, "Weekly product sync", soon, "https://meet.google.com/abc"),
                                       mt(2, "A very long title " * 12, at(1, 10, 0), "https://zoom.us/j/1", "Zoom"),
                                       mt(3, "Retro", at(2, 10, 0), "", "")]), 718),
                         ("invite", Cal([], connected=False), 718), ("empty", Cal([]), 1100)):
    app = make_stub_app("pro")
    app.cfg.update({"save_history": True})
    app.show_main = lambda *a, **k: None
    app.calendar = cal
    page = home.create(app)
    page.sync_io = True
    page.resize(width, 1000)
    page.show()
    for _ in range(4):
        QApplication.processEvents()
    page.layout().activate()
    sec = page.upcoming
    info = {"state": sec.state(), "w": sec.width(), "h": sec.height(), "dpr_widget": page.devicePixelRatioF(),
            "recent_y": page.recent_title.mapTo(page, QPoint(0, 0)).y(),
            "rows": [[r.height(), r.width(), r.record_button.geometry().right(), r.record_button.width(),
                      r.title.width(), r.time_label.width(), r.when_label.text(), r.call_badge.width() if r.call_badge else 0,
                      (r.join_button.geometry().right() if r.join_button else -1)] for r in sec.rows],
            "invite_h": sec.invite.height() if sec.invite.isVisible() else 0}
    img = page.grab().toImage()
    info["img"] = [img.width(), img.height(), img.devicePixelRatio()]
    out[name] = info
print("PROBE" + json.dumps(out))
"""


def _run_probe(scale):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory(prefix="gusan-scale-") as data:
        env = dict(os.environ, QT_SCALE_FACTOR=str(scale), PYTHONDONTWRITEBYTECODE="1", QT_QPA_PLATFORM="offscreen",
                   TRANSCRIBE_APP_DATA_DIR=data, TRANSCRIBE_DISABLE_KEYRING="1", TRANSCRIBE_SKIP_MIGRATION="1")
        res = subprocess.run([sys.executable, "-c", PROBE.replace("__ROOT__", repr(root))], capture_output=True,
                             text=True, timeout=300, env=env, cwd=root)
    assert res.returncode == 0, res.stderr[-1500:]
    line = next(ln for ln in res.stdout.splitlines() if ln.startswith("PROBE"))
    return json.loads(line[5:])


def test_the_layout_is_the_same_at_100_and_150_percent():
    """Everything is in logical pixels: a screen at 150 % shows the same layout in the same numbers (and
    its picture is 1.5x as many device pixels) - nothing is clipped, cut or moved by the scale."""
    one = _run_probe(1)
    one_half = _run_probe(1.5)
    assert one["dpr"] == 1.0 and one_half["dpr"] == 1.5
    for name in ("list", "invite", "empty"):
        a, b = one[name], one_half[name]
        assert b["dpr_widget"] == 1.5 and a["dpr_widget"] == 1.0
        for key in ("state", "w", "h", "invite_h"):
            assert a[key] == b[key], (name, key, a[key], b[key])
        assert len(a["rows"]) == len(b["rows"])
        for ra, rb in zip(a["rows"], b["rows"]):
            assert ra[0:2] == rb[0:2]                                             # height, width
            assert ra[2] <= ra[1] and rb[2] <= rb[1]                              # Record inside its row
            assert ra[8] < ra[2] - ra[3] + 1 or ra[8] == -1                       # Join left of Record
            assert rb[5] == ra[5] == 68                                           # the time column
            assert rb[7] == ra[7]                                                 # the call badge (0 or 18)
            assert abs(rb[4] - ra[4]) <= 3                                        # the title's room: the same, give or take a font step
            assert abs(rb[3] - ra[3]) <= 3                                        # the buttons keep their width
        assert abs(a["recent_y"] - b["recent_y"]) <= 4
        assert b["img"][0] == round(a["img"][0] * 1.5) and b["img"][2] == 1.5
    assert [r[0] for r in one["list"]["rows"]] == [58, 59, 59]
    assert one["list"]["rows"][1][4] > 250                                        # the long title still gets most of the line


# ═══════════════════ the same meetings as the Meetings page ═══════════════════
@pytest.fixture
def real_service(qapp, tmp_path, now, monkeypatch):
    """The real CalendarService (its fake API, a fake clock, work done at once) and the clock every page reads."""
    import calendar_service
    from tests_qt.test_calendar_service import Clock, FakeApi, T, run_now
    from tests_qt.test_calendar_service import meeting as svc_meeting
    from ui.pages import home_upcoming
    from ui.pages import meetings as page_mod
    clock = Clock(T - timedelta(minutes=2))
    monkeypatch.setattr(home_upcoming, "_utcnow", clock)
    monkeypatch.setattr(page_mod, "_utcnow", clock)
    made = []

    def build(meetings, cfg=None):
        api = FakeApi(meetings)
        svc = calendar_service.CalendarService(cfg if cfg is not None else {}, api=api, clock=clock,
                                               state_path=tmp_path / "calendar_state.json", run_async=run_now)
        made.append(svc)
        return svc, api, clock
    yield SimpleNamespace(build=build, meeting=svc_meeting, T=T)
    for svc in made:
        svc.stop()
        svc.deleteLater()
    QApplication.processEvents()


def test_home_shows_the_meetings_the_meetings_page_shows(shell, real_service, monkeypatch):
    m = real_service.meeting
    solo = m("b", 90, others=())                                                # nobody else: not "a meeting"
    muted = m("c", 120)
    rows = [m("a", 30), solo, muted, m("d", 200), m("e", 300), m("f", 400)]
    svc, _api, _clock = real_service.build(rows, {})
    refreshes = []
    real_refresh = svc.refresh
    monkeypatch.setattr(svc, "refresh", lambda force=False: refreshes.append(force) or real_refresh(force))
    win, app = shell("pro", calendar=svc, meeting_session=FakeSession())
    svc.core.cfg = cfg = app.cfg                                                # one settings dict for the service and the pages
    svc.start()
    refreshes.clear()
    home_page = win.open("home")
    QApplication.processEvents()
    assert refreshes == []                                                       # opening Home reads no network
    sec = home_page.upcoming
    assert sec is not None and sec.state() == "list"
    meetings_page = win.open("meetings")
    QApplication.processEvents()
    assert refreshes                                                             # the Meetings page does (rate limited)
    home_ids = [r.facts["id"] for r in sec.rows]
    service_ids = [x["id"] for x in svc.meetings()]
    page_ids = [r.meeting["id"] for r in meetings_page.list_view.coming_up.rows]
    assert service_ids == ["a", "c", "d", "e", "f"]                              # the filter: "b" has nobody else on it
    assert home_ids == service_ids[:3] == page_ids[:3] == ["a", "c", "d"]
    # "Only meetings with other people" off: both lists follow, with nothing done on Home.
    cfg["calendar_only_with_link"] = False
    svc.on_setting_changed("calendar_only_with_link")
    win.on_setting_changed("calendar_only_with_link")
    assert [r.facts["id"] for r in sec.rows] == ["a", "b", "c"]
    assert [r.meeting["id"] for r in meetings_page.list_view.coming_up.rows][:3] == ["a", "b", "c"]
    # A muted meeting (its bell) stays on both lists: a mute only turns off the reminder.
    svc.set_muted("a", True)
    assert [r.facts["id"] for r in sec.rows] == ["a", "b", "c"]
    assert [r.meeting["id"] for r in meetings_page.list_view.coming_up.rows][:3] == ["a", "b", "c"]
    assert [x["muted"] for x in svc.meetings()][:3] == [True, False, False]
    assert sec.rows[0].meeting["muted"] is True
    # Time passes: the first meeting ends, and both drop it (the service tells them within 20 s).
    _clock.now = real_service.T + timedelta(minutes=95)
    svc.check_reminders()
    assert [r.facts["id"] for r in sec.rows] == ["b", "c", "d"]
    assert [r.meeting["id"] for r in meetings_page.list_view.coming_up.rows][:3] == ["b", "c", "d"]


def test_record_on_home_opens_meetings_with_the_start_card_filled_and_nothing_started(shell, real_service, monkeypatch):
    from ui.components import mini_prompt
    m = real_service.meeting
    svc, _api, _clock = real_service.build([m("a", 30, title="Design review", others=("Sona", "Hasmik"))])
    session = FakeSession()
    win, app = shell("pro", calendar=svc, meeting_session=session)
    svc.start()
    home_page = win.open("home")
    QApplication.processEvents()
    asked = []
    monkeypatch.setattr(mini_prompt, "ask", lambda *a, **k: asked.append(a) or True)
    row = home_page.upcoming.rows[0]
    row.record_button.click()
    QApplication.processEvents()
    assert win.current_route() == "meetings"
    card = win.page("meetings").list_view.start_card
    assert card.title_edit.text() == "Design review"
    assert card.calendar_event_id == "a" and card.people.names() == ["Sona", "Hasmik"]
    assert session.calls == [] and session.phase == "idle"                       # the Record button there does that
    assert [c for c in app.calls if c[0] in ("note_calendar_start", "_pro_upsell")] == []
    assert asked == []


def test_connect_on_home_asks_what_the_meetings_page_asks(shell, real_service, monkeypatch):
    from ui.components import mini_prompt
    asked = []
    monkeypatch.setattr(mini_prompt, "ask", lambda *a, **k: asked.append((a[1:], dict(k))) or False)
    svc, api, _clock = real_service.build([], {})
    api._connected = False
    svc.core.refresh_connected()
    win, _app = shell("pro", calendar=svc)
    home_page = win.open("home")
    QApplication.processEvents()
    assert home_page.upcoming.state() == "invite"
    home_page.upcoming.connect_button.click()
    meetings_page = win.open("meetings")
    QApplication.processEvents()
    meetings_page.list_view.connect_card.connect_btn.click()
    assert len(asked) == 2 and asked[0] == asked[1]                              # the same words, word for word
    assert asked[0][0][1] == BODY and asked[0][1]["detail"] == DETAIL and asked[0][1]["link"] == LINK


def test_the_meetings_page_scrolls_to_the_start_card_when_opened_with_a_meeting(shell, real_service):
    m = real_service.meeting
    svc, _api, _clock = real_service.build([m("a", 30)])
    win, _app = shell("pro", calendar=svc)
    svc.start()
    win.resize(1100, 320)
    win.show()
    page = win.open("meetings")
    QApplication.processEvents()
    bar = page._holders["list"].verticalScrollBar()
    assert bar.maximum() > 0                                                      # the page is taller than the window
    bar.setValue(bar.maximum())
    win.open("meetings", title="Standup", attendees=["Anna"], calendar_event_id="a", agenda="")
    QApplication.processEvents()
    assert bar.value() == 0 and page.list_view.start_card.title_edit.text() == "Standup"


# ═══════════════════ the shared door and the words ═══════════════════
def test_the_door_picks_the_line_of_a_connect_card():
    from ui import calendar_door as door
    assert door.line_for(False, "", "red") == (door.INVITE_SUB, "muted")
    assert door.line_for(True, "", "red") == ("Waiting for Google… Finish in your browser.", "muted")
    assert door.line_for(False, "Oops", "red") == ("Oops", "red")
    assert door.line_for(False, door.EXPIRED_NOTE, "muted") == ("Calendar access expired. Connect again.", "muted")
    assert door.line_for(True, "Oops", "red") == ("Oops", "red")                  # an error shows even while waiting
    assert door.line_for(False, "", "red", invite="Mine") == ("Mine", "muted")


def test_the_door_asks_nothing_when_there_is_nothing_to_connect(qapp, monkeypatch):
    from ui import calendar_door as door
    asked = _patch_ask(monkeypatch, True)
    assert door.connect_calendar(None, None) is False
    unavailable = FakeCalendar(connected=False)
    unavailable.available = False
    assert door.connect_calendar(None, unavailable) is False
    waiting = FakeCalendar(connected=False)
    waiting.is_connecting = True
    assert door.connect_calendar(None, waiting) is False
    assert asked == [] and unavailable.calls == [] and waiting.calls == []
    cal = FakeCalendar(connected=False)
    assert door.connect_calendar(None, cal) is True and len(asked) == 1 and cal.calls == [("connect",)]
    boom = FakeCalendar(connected=False)
    boom.connect_calendar = lambda: (_ for _ in ()).throw(RuntimeError("no"))
    assert door.connect_calendar(None, boom, privacy_mode=True) is False          # a failing service is False, not a crash
    door.cancel_connect(None)                                                      # nothing to cancel: nothing happens
    door.cancel_connect(cal)
    assert ("cancel",) in cal.calls


def test_the_door_opens_only_an_https_call(qapp, monkeypatch):
    from ui import calendar_door as door
    opened = _opened_urls(monkeypatch)
    assert door.open_join("https://meet.google.com/abc") is True
    for bad in ("http://zoom.us/j/1", "javascript:alert(1)", "file:///C:/x", "", None, "https://x.test/a b"):
        assert door.open_join(bad) is False, bad
    assert opened == ["https://meet.google.com/abc"]


def test_the_words_are_short_plain_and_never_about_limits(build, now, monkeypatch):
    """The founder's rules for copy: calm, plain English; no 'limit', no 'Armenia' anywhere a person reads."""
    seen = set()

    def collect(page):
        sec = _section_of(page)
        QApplication.processEvents()
        for w in sec.findChildren(QWidget) + [sec]:
            if w.isHidden():
                continue
            texts = [w.accessibleName(), w.toolTip()]
            if callable(getattr(w, "text", None)):
                texts.append(w.text())
            seen.update(x for x in texts if isinstance(x, str) and x)

    cal = FakeCalendar(five(), status_text=PAUSED)
    page, _app = build(cal)
    collect(page)
    cal.status.emit("")
    cal.rows = []
    cal.changed.emit([])
    collect(page)
    cal.has_read = False
    cal.changed.emit([])
    collect(page)
    cal.disconnect_calendar("expired")
    collect(page)
    cal.connect_calendar()
    collect(page)
    cal.error.emit("Couldn't connect Google Calendar - try again.")
    cal.cancel_connect()
    collect(page)
    assert len(seen) > 20
    for text in seen:
        low = text.lower()
        assert "limit" not in low and "armenia" not in low and "transcribe" not in low, text
        assert "{" not in text and "%s" not in text, text
