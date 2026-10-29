"""Google Calendar in the window (Gusan 2.0.x): the Meetings page's "Coming up" and start card,
the Connect disclosure and the rail, offscreen with a fake CalendarService (no network).

Spec: docs/redesign/google-calendar-spec.md (decisions 11, 14, 16, 17; gaps G9). The Home chip
is tested in test_page_home_dictation.py, the reminder wiring in test_wave4_controller.py,
the Live Assistant card in test_live_card.py."""
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication

from tests_qt.conftest import make_stub_app

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


@pytest.fixture(autouse=True)
def _inline_bg(monkeypatch):
    from ui.pages import meeting_views
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)


@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


@pytest.fixture
def shell(qapp, monkeypatch, meetings_root):
    from ui import shell as shell_mod
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    made = []

    def build(tier="pro", **attrs):
        app = make_stub_app(tier)
        app.note_calendar_start = lambda ctx, source, with_assist=None: app.calls.append(
            ("note_calendar_start", dict(ctx), source, with_assist))
        for k, v in attrs.items():
            setattr(app, k, v)
        win = shell_mod.MainWindow(app)
        made.append(win)
        return win, app

    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


class FakeSession(QObject):
    state_changed = Signal(str)
    chunk_text = Signal(str)
    live_line = Signal(str, str)
    recap = Signal(str)
    status = Signal(str)
    levels = Signal(float, float)
    finished = Signal(str, str, str)
    error = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.phase = "idle"
        self.title = ""
        self.attendees = ""
        self.my_notes = ""
        self.transcript = ""
        self.last_error = ""
        self._live_summary_text = ""
        self.meeting_dir = None
        self.calls = []

    @property
    def is_busy(self):
        return self.phase in ("recording", "paused", "processing")

    def active_dir(self):
        return self.meeting_dir if self.is_busy else None

    def elapsed_sec(self, now=None):
        return 0

    def live_lines(self):
        return []

    def snapshot(self):
        return {"phase": self.phase, "language": "en", "audio_mode": "smart_meeting"}

    def start(self, **kw):
        self.calls.append(kw)
        self.title = kw.get("title") or ""
        self.phase = "recording"
        self.state_changed.emit("recording")
        return True, "", ""


class FakeCalendar(QObject):
    changed = Signal(object)
    connected = Signal(str)
    disconnected = Signal(str)
    error = Signal(str)
    remind = Signal(object)
    connecting = Signal(bool)
    status = Signal(str)

    def __init__(self, rows=(), connected=True, status_text=""):
        super().__init__()
        self.available = True
        self.is_connected = connected
        self.is_connecting = False
        self.email = "aram@aibuben.xyz" if connected else ""
        self.rows = list(rows)
        self._status = status_text
        self.calls = []

    def meetings(self):
        return [dict(m) for m in self.rows] if self.is_connected else []

    def current_meeting(self):
        return dict(self.rows[0]) if (self.rows and self.is_connected) else None

    def status_text(self):
        return self._status

    def refresh(self, force=False):
        return False

    def set_muted(self, event_id, muted):
        self.calls.append(("set_muted", event_id, muted))
        return True

    def connect_calendar(self):
        self.calls.append(("connect",))
        self.is_connecting = True
        self.connecting.emit(True)
        return True

    def cancel_connect(self):
        self.is_connecting = False
        self.connecting.emit(False)

    def disconnect_calendar(self, reason="user"):
        self.is_connected = False
        self.changed.emit([])
        self.disconnected.emit(reason)


def meeting(i, title, start, mins=30, call="Google Meet", others=("Anna", "David"), link="",
            agenda="", muted=False, private=False):
    names = list(others)
    people = ", ".join(names[:2]) + (" +%d" % (len(names) - 2) if len(names) > 2 else "")
    return {"id": "e%d" % i, "title": title, "start": start, "end": start + timedelta(minutes=mins),
            "call_type": call, "people": people, "others": names, "attendees": ["Aram"] + names,
            "link": link, "agenda": agenda, "muted": muted, "private": private,
            "has_others": bool(names)}


def _open(win, **opts):
    page = win.open("meetings", **opts)
    QApplication.processEvents()
    return page


def _tip_text(widget):
    """A tooltip's words: the paragraph and the escapes taken off."""
    import html
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", widget.toolTip()))


def _local_noon(days=0):
    """Noon, local time, ``days`` from today (aware, in UTC)."""
    base = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    return (base + timedelta(days=days)).astimezone(timezone.utc)


@pytest.fixture
def at_noon(monkeypatch):
    """The page's clock stands at 10:00 local today, so "today" and "tomorrow" are fixed."""
    from ui.pages import meetings as page_mod
    fixed = _local_noon() - timedelta(hours=2)
    monkeypatch.setattr(page_mod, "_utcnow", lambda: fixed)
    return fixed


# ── the Connect disclosure (A17) ─────────────────────────────────────────────
def _patch_ask(monkeypatch, answer):
    from ui.components import mini_prompt
    asked = []

    def ask(parent, title, body, yes, no, icon="info", private=False, *, detail=None, link=None):
        asked.append((title, body, yes, no, icon, detail, link))
        return answer
    monkeypatch.setattr(mini_prompt, "ask", ask)
    return asked


def test_connect_asks_disclosure_first(shell, monkeypatch):
    asked = _patch_ask(monkeypatch, False)
    cal = FakeCalendar(connected=False)
    win, _app = shell("free", calendar=cal)
    page = _open(win)
    lv = page.list_view
    # "Not now": nothing happens, from the card and from the rail.
    lv.connect_card.connect_btn.click()
    lv.rail.cal_connect.click()
    assert cal.calls == [] and len(asked) == 2
    title, body, yes, no, icon, detail, link = asked[0]
    assert (title, yes, no, icon) == ("Connect Google Calendar", "Connect", "Not now", "calendar")
    assert body == BODY                                   # Google's prominent disclosure, word for word
    assert detail == DETAIL and link == LINK
    assert not lv.connect_card.cancel_btn.isVisibleTo(page)
    # "Connect": the browser sign-in starts - from either button.
    asked2 = _patch_ask(monkeypatch, True)
    lv.connect_card.connect_btn.click()
    assert cal.calls == [("connect",)] and len(asked2) == 1
    assert lv.connect_card.cancel_btn.isVisibleTo(page)
    # Already waiting for the browser: no second card, no second sign-in.
    lv.rail.cal_connect.setVisible(True)
    page.connect_calendar()
    assert len(asked2) == 1 and cal.calls == [("connect",)]


def test_the_rail_connect_goes_through_the_disclosure_too(shell, monkeypatch):
    asked = _patch_ask(monkeypatch, True)
    cal = FakeCalendar(connected=False)
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    page.list_view.rail.cal_connect.click()
    assert len(asked) == 1 and cal.calls == [("connect",)]


def test_privacy_mode_skips_the_disclosure_and_the_service_says_so(shell, monkeypatch):
    asked = _patch_ask(monkeypatch, True)
    cal = FakeCalendar(connected=False)
    win, app = shell("pro", calendar=cal)
    app.cfg["privacy_mode"] = True
    page = _open(win)
    page.connect_calendar()
    assert asked == [] and cal.calls == [("connect",)]    # the service answers "Turn off Privacy Mode…"


def test_no_calendar_no_connect(shell, monkeypatch):
    asked = _patch_ask(monkeypatch, True)
    win, _app = shell("pro")                               # dormant: no Google client in this build
    page = _open(win)
    assert page.connect_calendar() is False and asked == []


# ── the rail (A16) ───────────────────────────────────────────────────────────
def test_rail_labels(shell, at_noon):
    cal = FakeCalendar([meeting(1, "Sync", _local_noon())])
    win, app = shell("pro", calendar=cal)
    rail = _open(win).list_view.rail
    assert rail.cal_row.label.text() == "Google Calendar"
    assert rail.ask_row.label.text() == "Ask me to record"
    assert rail.ask_row.hint_label.text() == "1 min before"
    assert rail.link_row.label.text() == "Only meetings with other people"
    assert rail.link_toggle.accessibleName() == "Only meetings with other people"
    assert "call link" not in rail.link_row.label.text()
    rail.link_toggle.click()
    assert ("set_setting", "calendar_only_with_link", False) in app.calls     # the key keeps its old name
    assert rail.cal_row.hint_label.text() == "aram@aibuben.xyz"


def test_rail_says_paused_in_privacy_mode(shell, at_noon):
    cal = FakeCalendar([meeting(1, "Sync", _local_noon())], status_text="Paused in Privacy mode.")
    win, _app = shell("pro", calendar=cal)
    rail = _open(win).list_view.rail
    assert rail.cal_row.hint_label.text() == "aram@aibuben.xyz\nPaused in Privacy mode."
    cal._status = ""
    cal.status.emit("")
    assert rail.cal_row.hint_label.text() == "aram@aibuben.xyz"
    # A read error is the list's line only, not the rail's.
    cal._status = "Couldn't read Google Calendar. Trying again."
    cal.status.emit(cal._status)
    assert rail.cal_row.hint_label.text() == "aram@aibuben.xyz"


# ── Coming up (A14) ──────────────────────────────────────────────────────────
def test_coming_up_groups_today_tomorrow(shell, at_noon):
    rows = [meeting(1, "Product sync", _local_noon(), link="https://meet.google.com/a"),
            meeting(2, "Customer call", _local_noon() + timedelta(hours=2)),
            meeting(3, "Standup", _local_noon(1) - timedelta(hours=2.5)),
            meeting(4, "Board prep", _local_noon(3))]
    win, _app = shell("pro", calendar=FakeCalendar(rows))
    cu = _open(win).list_view.coming_up
    day3 = (datetime.now().astimezone() + timedelta(days=3)).strftime("%A")
    assert [h.text() for h in cu.headers] == ["Today", "Tomorrow", day3]
    assert [r.meeting["id"] for r in cu.rows] == ["e1", "e2", "e3", "e4"]
    # The time line runs inside a day: the first row of a day has no line above it.
    assert cu.rows[0].rel_label.text() == "" and cu.rows[0].time_label.text() == \
        _local_noon().astimezone().strftime("%H:%M")
    assert not cu.empty.isVisibleTo(cu)
    assert cu.rows[2].rel_label.text() == "" and cu.rows[3].rel_label.text() == ""   # the day is the header's


def test_a_meeting_that_started_yesterday_and_still_runs_is_today(shell, at_noon):
    started = _local_noon(-1) + timedelta(hours=11)         # 23:00 yesterday
    rows = [meeting(1, "Late call", started, mins=14 * 60)]    # runs into today
    win, _app = shell("pro", calendar=FakeCalendar(rows))
    cu = _open(win).list_view.coming_up
    assert [h.text() for h in cu.headers] == ["Today"]


def test_now_and_in_n_min(shell, monkeypatch):
    from ui.pages import meetings as page_mod
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(page_mod, "_utcnow", lambda: now)
    rows = [meeting(1, "Running", now - timedelta(minutes=10), mins=40),
            meeting(2, "Soon", now + timedelta(minutes=12))]
    win, _app = shell("pro", calendar=FakeCalendar(rows))
    cu = _open(win).list_view.coming_up
    assert cu.rows[0].rel_label.text() == "Now" and cu.rows[1].rel_label.text() == "in 12 min"


def test_show_more_after_five(shell, at_noon):
    rows = [meeting(i, "Meeting %d" % i, _local_noon() + timedelta(minutes=30 * i)) for i in range(8)]
    win, _app = shell("pro", calendar=FakeCalendar(rows))
    page = _open(win)
    cu = page.list_view.coming_up
    assert len(cu.rows) == 5 and cu.more.isVisibleTo(cu) and cu.more.text() == "Show more"
    cu.more.click()
    assert len(cu.rows) == 8 and cu.more.text() == "Show less"
    cu.more.click()
    assert len(cu.rows) == 5 and cu.more.text() == "Show more"
    # Five or fewer: no link.  More than twenty: twenty.
    page.list_view.set_upcoming(rows[:5])
    assert len(cu.rows) == 5 and not cu.more.isVisibleTo(cu)
    page.list_view.set_upcoming([meeting(i, "M%d" % i, _local_noon() + timedelta(minutes=i)) for i in range(30)])
    cu.more.click()
    assert len(cu.rows) == 20


def test_join_link_only_for_https(shell, at_noon, monkeypatch):
    opened = []
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString())))
    rows = [meeting(1, "A", _local_noon(), link="https://meet.google.com/abc-defg-hij"),
            meeting(2, "B", _local_noon() + timedelta(hours=1), link="http://zoom.us/j/1"),
            meeting(3, "C", _local_noon() + timedelta(hours=2), link=""),
            meeting(4, "D", _local_noon() + timedelta(hours=3), link="javascript:alert(1)"),
            meeting(5, "E", _local_noon() + timedelta(hours=4), link="HTTPS://zoom.us/j/9")]
    win, _app = shell("pro", calendar=FakeCalendar(rows))
    cu = _open(win).list_view.coming_up
    assert [r.join_button is not None for r in cu.rows] == [True, False, False, False, True]
    assert _tip_text(cu.rows[0].join_button) == "meet.google.com"
    assert cu.rows[0].join_button.accessibleName() == "Join A"
    cu.rows[0].join_button.click()
    assert opened == ["https://meet.google.com/abc-defg-hij"]
    # Pressing Join is not the row's own click: the start card is not filled with that meeting.
    card = win.page("meetings").list_view.start_card
    assert card.title_edit.text() == "A"                     # the meeting on now filled it, as before
    cu.rows[4].join_button.click()
    assert opened[-1].lower() == "https://zoom.us/j/9" and card.title_edit.text() == "A"


def test_names_tooltip_has_everyone(shell, at_noon):
    rows = [meeting(1, "A", _local_noon(), others=("Anna", "David", "Mariam", "Lilit"))]
    win, _app = shell("pro", calendar=FakeCalendar(rows))
    row = _open(win).list_view.coming_up.rows[0]
    assert _tip_text(row) == "Anna, David, Mariam, Lilit"
    assert "Anna, David +2" in row.accessibleName()


def test_row_click_prefills_start_card(shell, at_noon):
    rows = [meeting(1, "First", _local_noon()),
            meeting(2, "Design review", _local_noon() + timedelta(hours=3), others=("Sona", "Hasmik"),
                    agenda=AGENDA),
            meeting(3, "Private one", _local_noon() + timedelta(hours=4), agenda="secret", private=True)]
    win, _app = shell("pro", calendar=FakeCalendar(rows))
    page = _open(win)
    card = page.list_view.start_card
    cu = page.list_view.coming_up
    QTest.mouseClick(cu.rows[1], Qt.LeftButton)
    assert card.title_edit.text() == "Design review"
    assert card.calendar_event_id == "e2" and card.people.names() == ["Sona", "Hasmik"]
    assert card.agenda_box.isVisibleTo(page) and card.agenda_edit.toPlainText() == AGENDA
    assert card.cal_sub.isVisibleTo(page)
    # Another row: its own agenda, not the last one's; a private event has none.
    QTest.mouseClick(cu.rows[2], Qt.LeftButton)
    assert card.title_edit.text() == "Private one" and card.calendar_event_id == "e3"
    assert card.agenda() == "" and not card.agenda_box.isVisibleTo(page)
    # Keyboard: Enter on a focused row.
    cu.rows[1].setFocus()
    QTest.keyClick(cu.rows[1], Qt.Key_Return)
    assert card.title_edit.text() == "Design review" and card.agenda() == AGENDA
    assert page.current_view() == "list"


def test_status_line_paused_and_error(shell, at_noon):
    cal = FakeCalendar([meeting(1, "Sync", _local_noon())], status_text="Paused in Privacy mode.")
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    cu = page.list_view.coming_up
    assert cu.status.isVisibleTo(page) and cu.status.text() == "Paused in Privacy mode."
    cal._status = "Couldn't read Google Calendar. Trying again."
    cal.status.emit(cal._status)
    assert cu.status.text() == "Couldn't read Google Calendar. Trying again."
    cal._status = ""
    cal.status.emit("")
    assert not cu.status.isVisibleTo(page)
    assert len(cu.rows) == 1                                # the list itself stays through all of it


def test_empty_text(shell, at_noon):
    win, _app = shell("pro", calendar=FakeCalendar([]))
    page = _open(win)
    cu = page.list_view.coming_up
    assert cu.isVisibleTo(page) and cu.empty.isVisibleTo(page)
    assert cu.empty.text() == "No meetings coming up." and cu.rows == [] and cu.headers == []
    assert not cu.more.isVisibleTo(page)


def test_no_record_button_on_the_rows(shell, at_noon):
    win, _app = shell("pro", calendar=FakeCalendar([meeting(1, "Sync", _local_noon(), link="https://meet.google.com/a")]))
    row = _open(win).list_view.coming_up.rows[0]
    names = [(b.text() or b.accessibleName()) for b in row.findChildren(QAbstractButton)]
    assert names and not any("Record" in n for n in names)          # Join and the bell only


# ── the start card's agenda (A7, A13) ────────────────────────────────────────
def test_start_card_agenda_prefill_and_edit(shell, at_noon):
    rows = [meeting(1, "Weekly product sync", _local_noon() - timedelta(hours=2) + timedelta(seconds=30),
                    agenda=AGENDA)]
    cal = FakeCalendar(rows)
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    card = page.list_view.start_card
    # The meeting on now fills the card, agenda included (the user sees what is sent).
    assert card.calendar_event_id == "e1"
    assert card.agenda_box.isVisibleTo(page) and card.agenda_edit.toPlainText() == AGENDA
    assert card.agenda() == AGENDA
    # An edit sticks through a refresh of the list.
    card.agenda_edit.setPlainText("Budget only")
    cal.rows[0]["agenda"] = "Changed upstream"
    cal.changed.emit(cal.meetings())
    assert card.agenda() == "Budget only"
    # Clear: the box goes and nothing is sent.
    card.agenda_clear.click()
    assert card.agenda() == "" and not card.agenda_box.isVisibleTo(page)


def test_no_agenda_no_box(shell, at_noon):
    rows = [meeting(1, "Sync", _local_noon() - timedelta(hours=2) + timedelta(seconds=30), agenda="")]
    win, _app = shell("pro", calendar=FakeCalendar(rows))
    page = _open(win)
    card = page.list_view.start_card
    assert card.calendar_event_id == "e1" and not card.agenda_box.isVisibleTo(page) and card.agenda() == ""


def test_a_typed_meeting_has_no_agenda_even_if_the_box_has_text(shell, at_noon):
    win, _app = shell("pro", calendar=FakeCalendar([]))
    card = _open(win).list_view.start_card
    card.agenda_edit.setPlainText("left over")
    assert card.agenda() == ""                                # no calendar meeting on the card


def test_record_passes_agenda_and_marks_prompted(shell, at_noon):
    session = FakeSession()
    rows = [meeting(1, "Weekly product sync", _local_noon() - timedelta(hours=2) + timedelta(seconds=30),
                    agenda=AGENDA)]
    win, app = shell("pro", calendar=FakeCalendar(rows), meeting_session=session)
    page = _open(win)
    card = page.list_view.start_card
    card.agenda_edit.setPlainText("Budget\nHiring")
    assert page.record() is True
    kw = session.calls[0]
    assert kw["title"] == "Weekly product sync" and kw["source"] == "calendar"
    assert kw["calendar_event_id"] == "e1" and kw["agenda"] == "Budget\nHiring"
    noted = [c for c in app.calls if c[0] == "note_calendar_start"]
    assert len(noted) == 1 and noted[0][1]["event_id"] == "e1" and noted[0][2] == "start_card"
    assert noted[0][1]["agenda"] == "Budget\nHiring" and noted[0][3] is False
    # The card is fresh again: nothing of this meeting is left to be sent with the next one.
    assert card.agenda() == "" and not card.agenda_box.isVisibleTo(page) and card.calendar_event_id == ""


def test_a_plain_record_sends_no_agenda_and_marks_nothing(shell, at_noon):
    session = FakeSession()
    win, app = shell("pro", calendar=FakeCalendar([]), meeting_session=session)
    page = _open(win)
    page.list_view.start_card.title_edit.setText("Quick chat")
    assert page.record() is True
    kw = session.calls[0]
    assert kw["source"] == "page" and kw["calendar_event_id"] is None and "agenda" not in kw
    assert not [c for c in app.calls if c[0] == "note_calendar_start"]


def test_open_options_prefill_with_an_agenda(shell, at_noon):
    # The meetings the reminder or a row names are in the list (a picked one stays until it is over).
    rows = [meeting(9, "Standup", _local_noon() + timedelta(hours=3)),
            meeting(10, "Lunch", _local_noon() + timedelta(hours=4))]
    win, _app = shell("pro", calendar=FakeCalendar(rows), meeting_session=FakeSession())
    page = _open(win, title="Standup", attendees=["Anna"], calendar_event_id="e9", agenda=AGENDA)
    card = page.list_view.start_card
    assert card.title_edit.text() == "Standup" and card.calendar_event_id == "e9"
    assert card.agenda() == AGENDA and card.agenda_box.isVisibleTo(page)
    # Opened again for another meeting without an agenda: not the last one's.
    page = _open(win, title="Lunch", attendees=["Sam"], calendar_event_id="e10")
    assert card.agenda() == "" and not card.agenda_box.isVisibleTo(page)


def test_dormant_page_has_none_of_it(shell):
    win, _app = shell("pro")                                  # no calendar service
    page = _open(win)
    lv = page.list_view
    assert not lv.coming_up.isVisibleTo(page) and not lv.connect_card.isVisibleTo(page)
    assert not lv.rail.cal_row.isVisibleTo(page) and not lv.start_card.agenda_box.isVisibleTo(page)
    lv.set_calendar_status("Paused in Privacy mode.")         # a stray signal changes nothing visible
    assert not lv.coming_up.isVisibleTo(page)


def test_the_rows_have_names_for_screen_readers(shell, at_noon):
    rows = [meeting(1, "Sync", _local_noon(), link="https://meet.google.com/a")]
    win, _app = shell("pro", calendar=FakeCalendar(rows))
    row = _open(win).list_view.coming_up.rows[0]
    assert row.accessibleName().startswith("Sync, ") and "with Anna, David" in row.accessibleName()
    assert row.bell.accessibleName() == "Ask me to record: Sync"
    assert row.focusPolicy() == Qt.TabFocus


# ── a picked meeting stays on the start card until it is over (not the last row pressed forever) ──
def test_a_pressed_row_stays_until_its_meeting_is_over(shell, monkeypatch):
    from ui.pages import meetings as page_mod
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(page_mod, "_utcnow", lambda: now)
    design = meeting(2, "Design review", now + timedelta(hours=3), others=("Sona", "Hasmik"), agenda=AGENDA)
    cal = FakeCalendar([design])
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    lv, card = page.list_view, page.list_view.start_card
    QTest.mouseClick(lv.coming_up.rows[0], Qt.LeftButton)                  # 09:00: pressed, not recorded
    assert (card.title_edit.text(), card.calendar_event_id) == ("Design review", "e2")
    # 15:00: another meeting is on now. The list refreshes and the page ticks; the pick stays.
    customer = meeting(4, "Customer call", now - timedelta(minutes=5), mins=60, others=("Lilit",))
    cal.rows = [customer, design]
    cal.changed.emit(cal.meetings())
    lv.tick()
    assert (card.title_edit.text(), card.calendar_event_id) == ("Design review", "e2")
    assert card.agenda() == AGENDA and card.people.names() == ["Sona", "Hasmik"]
    # Its meeting is over (gone from the list): the card follows the calendar again.
    cal.rows = [customer]
    cal.changed.emit(cal.meetings())
    assert (card.title_edit.text(), card.calendar_event_id) == ("Customer call", "e4")
    assert card.agenda() == "" and card.people.names() == ["Lilit"]
    lv.tick()
    assert card.calendar_event_id == "e4"


def test_a_pick_is_released_when_nothing_is_on_and_the_card_goes_back_to_a_fresh_name(shell, monkeypatch):
    from ui.pages import meetings as page_mod
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(page_mod, "_utcnow", lambda: now)
    later = meeting(2, "Later", now + timedelta(hours=3), agenda=AGENDA)
    cal = FakeCalendar([later])
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    card = page.list_view.start_card
    QTest.mouseClick(page.list_view.coming_up.rows[0], Qt.LeftButton)
    assert card.calendar_event_id == "e2"
    cal.rows = []
    cal.changed.emit([])
    assert card.calendar_event_id == "" and card.agenda() == "" and card.people.names() == []
    assert card.title_edit.text().startswith("Meeting")               # the default name again


def test_a_title_the_user_typed_is_kept_even_after_the_pick_is_over(shell, monkeypatch):
    from ui.pages import meetings as page_mod
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(page_mod, "_utcnow", lambda: now)
    later = meeting(2, "Later", now + timedelta(hours=3))
    cal = FakeCalendar([later])
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    card = page.list_view.start_card
    QTest.mouseClick(page.list_view.coming_up.rows[0], Qt.LeftButton)
    card.title_edit.setFocus()
    QTest.keyClicks(card.title_edit, " v2")                           # real typing
    assert card.title_edit.text() == "Later v2"
    cal.rows = [meeting(4, "Customer call", now - timedelta(minutes=5), mins=60)]
    cal.changed.emit(cal.meetings())
    assert card.title_edit.text() == "Later v2"


def test_a_disconnect_releases_a_picked_meeting(shell, monkeypatch):
    from ui.pages import meetings as page_mod
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(page_mod, "_utcnow", lambda: now)
    cal = FakeCalendar([meeting(2, "Later", now + timedelta(hours=3), agenda=AGENDA)])
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    card = page.list_view.start_card
    QTest.mouseClick(page.list_view.coming_up.rows[0], Qt.LeftButton)
    assert card.calendar_event_id == "e2"
    cal.disconnect_calendar("user")
    assert card.calendar_event_id == "" and card.agenda() == ""


def test_a_recorded_pick_starts_fresh_next_time(shell, monkeypatch):
    from ui.pages import meetings as page_mod
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(page_mod, "_utcnow", lambda: now)
    session = FakeSession()
    cal = FakeCalendar([meeting(2, "Later", now + timedelta(hours=3))])
    win, _app = shell("pro", calendar=cal, meeting_session=session)
    page = _open(win)
    card = page.list_view.start_card
    QTest.mouseClick(page.list_view.coming_up.rows[0], Qt.LeftButton)
    assert page.record() is True
    assert card.calendar_event_id == "" and card._picked is False


# ── "No meetings coming up." only after a read ───────────────────────────────
def test_empty_text_waits_for_the_first_read(shell, at_noon):
    cal = FakeCalendar([], status_text="Paused in Privacy mode.")
    cal.has_read = False                                         # Privacy mode at launch: nothing read yet
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    cu = page.list_view.coming_up
    assert cu.isVisibleTo(page) and not cu.empty.isVisibleTo(page)
    assert cu.status.isVisibleTo(page) and cu.status.text() == "Paused in Privacy mode."     # the why
    cal.has_read = True                                          # the first read landed, still nothing on
    cal.changed.emit([])
    assert cu.empty.isVisibleTo(page) and cu.empty.text() == "No meetings coming up."


def test_a_service_without_has_read_shows_the_empty_text_as_before(shell, at_noon):
    win, _app = shell("pro", calendar=FakeCalendar([]))
    page = _open(win)
    assert page.list_view.coming_up.empty.isVisibleTo(page)


# ── access that ran out, and an old error that must not come back ────────────
def test_an_expired_grant_says_so_on_the_card_and_in_the_rail(shell, at_noon):
    cal = FakeCalendar([meeting(1, "Sync", _local_noon())])
    win, _app = shell("pro", calendar=cal)
    page = _open(win)
    lv = page.list_view
    cal.disconnect_calendar("expired")
    assert lv.connect_card.isVisibleTo(page)
    assert lv.connect_card.sub.text() == "Calendar access expired. Connect again."
    assert lv.rail.cal_row.hint_label.text() == "Calendar access expired. Connect again."
    # Still there after the page syncs again, until the next connect.
    lv.sync_calendar()
    assert lv.connect_card.sub.text() == "Calendar access expired. Connect again."
    cal.connect_calendar()                                       # connecting
    assert lv.connect_card.sub.text().startswith("Waiting for Google")
    cal.cancel_connect()
    assert lv.connect_card.sub.text() == "See your meetings and get a nudge to record."
    assert lv.rail.cal_row.hint_label.text() == ""


def test_a_users_own_disconnect_leaves_no_expiry_line(shell, at_noon):
    cal = FakeCalendar([meeting(1, "Sync", _local_noon())])
    win, _app = shell("pro", calendar=cal)
    lv = _open(win).list_view
    cal.disconnect_calendar("expired")
    cal.is_connected = True
    cal.connected.emit("aram@aibuben.xyz")
    cal.disconnect_calendar("user")
    assert lv.connect_card.sub.text() == "See your meetings and get a nudge to record."
    assert lv.rail.cal_row.hint_label.text() == ""


def test_an_old_error_does_not_come_back_under_google_calendar(shell, at_noon):
    cal = FakeCalendar([meeting(1, "Sync", _local_noon())])
    win, _app = shell("pro", calendar=cal)
    rail = _open(win).list_view.rail
    offline = "Couldn't reach Google Calendar - check your internet connection."
    cal.error.emit(offline)                                      # an offline streak while connected
    assert rail.cal_row.hint_label.text() == "aram@aibuben.xyz"  # hidden: the row shows the account
    cal.disconnect_calendar("user")
    assert rail.cal_row.hint_label.text() == ""                  # not the old offline text
    # A failed Connect shows its error; a later good one and a later disconnect leave none behind.
    cal.error.emit("Couldn't connect Google Calendar - try again.")
    assert rail.cal_row.hint_label.text() == "Couldn't connect Google Calendar - try again."
    cal.is_connected = True
    cal.connected.emit("aram@aibuben.xyz")
    cal.disconnect_calendar("user")
    assert rail.cal_row.hint_label.text() == ""


def test_a_failed_connect_error_stays_on_the_card_until_the_next_try(shell, at_noon):
    cal = FakeCalendar([], connected=False)
    win, _app = shell("pro", calendar=cal)
    lv = _open(win).list_view
    cal.connecting.emit(True)
    cal.connecting.emit(False)
    cal.error.emit("Sign-in timed out - try again.")
    assert lv.connect_card.sub.text() == "Sign-in timed out - try again."
    lv.sync_calendar()                                           # a re-sync doesn't wipe it
    assert lv.connect_card.sub.text() == "Sign-in timed out - try again."
    cal.connecting.emit(True)
    assert lv.connect_card.sub.text().startswith("Waiting for Google")
    cal.connecting.emit(False)
    assert lv.connect_card.sub.text() == "See your meetings and get a nudge to record."


# ── a calendar invite's title and names are text, never markup ───────────────
@pytest.fixture
def picture(tmp_path, monkeypatch):
    from PySide6.QtGui import QColor, QImage
    img = QImage(150, 150, QImage.Format_RGB32)
    img.fill(QColor("red"))
    assert img.save(str(tmp_path / "p.png"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


HOSTILE = "<img src=p.png> Review"
HOSTILE_NAME = "J. <img src=p.png>"


def _person_chips(field):
    return [b for b in field.findChildren(QAbstractButton) if b.property("gusan") == "personChip"]


def test_the_coming_up_rows_show_hostile_text_as_text(shell, at_noon, picture):
    import html
    from PySide6.QtWidgets import QLabel
    rows = [meeting(1, HOSTILE, _local_noon(), others=(HOSTILE_NAME, "<b>x</b>", "Anna", "David"))]
    win, _app = shell("pro", calendar=FakeCalendar(rows))
    row = _open(win).list_view.coming_up.rows[0]
    assert row.title_label.textFormat() == Qt.PlainText
    assert QLabel.sizeHint(row.title_label).height() < 100
    tip = row.toolTip()                                          # "everyone", as text
    assert tip.startswith("<p") and html.escape(HOSTILE_NAME, quote=False) in tip
    assert "<img" not in tip and "<b>" not in tip


def test_the_people_field_chips_and_overflow_are_text(shell, at_noon, picture):
    import html
    from PySide6.QtWidgets import QLabel
    win, _app = shell("pro", calendar=FakeCalendar([]))
    card = _open(win).list_view.start_card
    card.prefill(title="Sync", attendees=[HOSTILE_NAME, "Anna", "David", "Mariam"], calendar_event_id="e1")
    people = card.people
    assert people.more.text().startswith("+")                    # two shown, the rest behind "+2"
    assert people.more.toolTip().startswith("<p") and "<img" not in people.more.toolTip()
    card.people.set_names([HOSTILE_NAME], "Add people")
    chip = _person_chips(card.people)[-1]                        # the newest (the old ones await deletion)
    assert chip.name == HOSTILE_NAME and chip.toolTip().startswith("<p")
    assert html.escape(HOSTILE_NAME, quote=False) in chip.toolTip() and "<img" not in chip.toolTip()
    shown = [lb for lb in chip.findChildren(QLabel) if lb.text()]
    assert shown and all(lb.textFormat() == Qt.PlainText for lb in shown)
    assert all(QLabel.sizeHint(lb).height() < 100 for lb in shown)


def test_the_notes_view_and_the_delete_box_show_a_hostile_title_as_text(shell, picture, meetings_root,
                                                                       monkeypatch):
    import json
    from PySide6.QtWidgets import QLabel
    from ui.components import mini_prompt
    folder = meetings_root / "20261007_143000"
    folder.mkdir()
    (folder / "meta.json").write_text(json.dumps({
        "title": HOSTILE, "attendees": "Aram, %s, <b>x</b>" % HOSTILE_NAME,
        "attendees_list": ["Aram", HOSTILE_NAME, "<b>x</b>"], "started": "2026-10-07 14:30:00",
        "duration_sec": 600, "language": "en"}), encoding="utf-8")
    (folder / "notes.md").write_text("## Summary\nx", encoding="utf-8")
    (folder / "transcript.txt").write_text("Speaker 1: hi", encoding="utf-8")
    win, _app = shell("pro")
    page = _open(win)
    page.open_notes(str(folder))
    QApplication.processEvents()
    nv = page.notes_view
    assert nv.title_label.text() == HOSTILE and nv.title_label.textFormat() == Qt.PlainText
    assert QLabel.sizeHint(nv.title_label).height() < 100
    named = [lb for lb in nv.findChildren(QLabel) if "<img" in lb.text() or "<b>x</b>" in lb.text()]
    assert named                                                 # the people line and the people card
    assert all(lb.textFormat() == Qt.PlainText for lb in named)
    seen = []
    monkeypatch.setattr(mini_prompt.MiniPrompt, "exec", lambda self: seen.append(
        [(lb.textFormat(), lb.text()) for lb in self.findChildren(QLabel) if lb.text()]) or 0)
    nv.delete()
    assert seen and seen[0]
    assert all(fmt == Qt.PlainText for fmt, _t in seen[0])                  # every word on the card is text, never markup
    assert any(HOSTILE in t for _f, t in seen[0])


def test_the_live_view_header_shows_a_hostile_title_as_text(shell, picture, meetings_root):
    from PySide6.QtWidgets import QLabel
    session = FakeSession()
    session.phase = "recording"
    session.title = HOSTILE
    session.attendees = "Anna, %s" % HOSTILE_NAME
    win, _app = shell("pro", meeting_session=session)
    page = _open(win)
    page.show_live()
    h = page.live_view.header
    assert h.title_label.textFormat() == Qt.PlainText and h.title_label.text() == HOSTILE
    assert h.sub_label.textFormat() == Qt.PlainText and HOSTILE_NAME in h.sub_label.text()
    assert QLabel.sizeHint(h.title_label).height() < 100 and QLabel.sizeHint(h.sub_label).height() < 100


def test_the_connect_box_is_short_points_and_a_https_link():
    # Simple and clear: one short sentence, four one-line points (no wall of text), one link.
    assert "\n" not in BODY and len(BODY) < 160
    assert len(DETAIL) == 4 and all(len(x) < 140 and x.startswith("•  ") for x in DETAIL)
    text, url = LINK
    assert url == "https://gusan.aibuben.xyz/privacy" and "gusan.aibuben.xyz/privacy" in text
    # what Google must be told: what is read, what for, who else gets it, how to stop
    joined = " ".join(DETAIL).lower()
    for word in ("reads", "agenda", "call link", "remind you to record", "ai", "disconnect"):
        assert word in joined, word


def test_the_link_on_a_card_opens_the_browser_and_leaves_the_card(qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from ui.components import mini_prompt as mp
    opened = []
    monkeypatch.setattr(mp, "_open_link", lambda url: opened.append(url) or True)
    dlg = mp.MiniPrompt(None, "Connect Google Calendar", BODY, "Connect", "Not now", icon="calendar",
                        detail=DETAIL, link=LINK)
    dlg.show()
    QApplication.processEvents()
    assert dlg.link_button is not None and dlg.link_button.text() == LINK[0]
    assert dlg.link_button.accessibleName() == "Open " + LINK[0]
    dlg.link_button.click()
    assert opened == [LINK[1]]
    assert dlg.isVisible() and dlg.result() != QDialog.Accepted           # reading the page is not an answer
    dlg.close()


def test_only_a_plain_https_address_is_ever_linked_or_opened(qapp, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    from ui.components import mini_prompt as mp
    for bad in ("http://gusan.aibuben.xyz/privacy", "javascript:alert(1)", "file:///C:/Windows/notepad.exe",
                "https://x.test/a b", "https://x.test/\nnext", "", None):
        dlg = mp.MiniPrompt(None, "T", "B", "Yes", "No", link=("Privacy", bad))
        assert dlg.link_button is None, bad                              # no link is drawn for it
        dlg.deleteLater()
    urls = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: urls.append(url.toString()) or True))
    assert mp._open_link("http://x.test/") is False and mp._open_link("ftp://x.test/") is False and urls == []
    assert mp._open_link("https://gusan.aibuben.xyz/privacy") is True
    assert urls == ["https://gusan.aibuben.xyz/privacy"]
