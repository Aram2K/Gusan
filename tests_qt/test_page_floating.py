"""Gusan 2.0 floating views: the meeting reminder toast (ui/floating/meeting_reminder.py)
and the tray menu (ui/tray_menu.py) - built with a stub app, Free / Pro / guest, every
row and button named, and each tap calling the right app method or set_setting."""
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QMenu

from tests_qt.conftest import make_stub_app

NOW = datetime(2026, 10, 7, 12, 29, 10, tzinfo=timezone.utc)


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    return cond()


def meeting(start_in_s=50, **kw):
    start = NOW + timedelta(seconds=start_in_s)
    m = {"id": "evt-1", "title": "Weekly product sync", "start": start, "end": start + timedelta(minutes=30),
         "call_type": "Google Meet", "link": "https://meet.google.com/abc-defg-hij", "people": "Anna, David +2",
         "attendees": ["Aram", "Anna", "David", "Mariam", "Lilit"], "others": ["Anna", "David", "Mariam", "Lilit"]}
    m.update(kw)
    return m


def full_app(tier="free"):
    app = make_stub_app(tier)
    app.cfg.update({"overlay_private": True})

    def recorder(method):
        return lambda *a, **k: app.calls.append((method,) + a + tuple(sorted(k.items())))

    for name in ("show_main", "quit_app", "_on_hotkey", "show_tray_hint", "show_auth_gate"):
        setattr(app, name, recorder(name))

    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        app.cfg[key] = value
        return True
    app.set_setting = set_setting
    return app


# ── the reminder ─────────────────────────────────────────────────────────────
@pytest.fixture
def mr(qapp):
    from ui.floating import meeting_reminder
    return meeting_reminder


@pytest.fixture
def toast(mr):
    made = []

    def build(tier="free", m=None, clock=None, show=True):
        app = full_app(tier)
        t = mr.MeetingReminder(app, clock=clock or (lambda: NOW))
        made.append(t)
        if show:
            assert t.show_meeting(m or meeting()) is True
            QApplication.processEvents()
        return t, app
    yield build
    for t in made:
        t.hide()
        t.deleteLater()


def _buttons(t):
    return [b for b in t.findChildren(QAbstractButton) if not b.isHidden()]


@pytest.mark.parametrize("tier", ["free", "pro"])
def test_reminder_builds_with_the_mockup_copy(toast, mr, tier):
    t, _app = toast(tier)
    assert t.isVisible() and t.is_showing()
    assert t.title_label.full_text() == "Weekly product sync"
    assert t.when_text() == "Starts in 1 min"
    assert not t.started()
    assert t.meta_label.full_text() == "Google Meet  ·  Anna, David +2"
    assert t.call_mark.isVisibleTo(t)
    assert t.record_button.text() == "Record"
    assert t.assist_button.text() == "Record + Live Assistant"
    assert t.dismiss_button.text() == "Not this one"
    assert t.join_button.text() == "Join call" and t.join_button.isVisibleTo(t)
    # Calm Pro: PRO on Record only for Free users, always on Record + Live Assistant.
    assert t.record_button.has_pro_tag() is (tier == "free")
    assert t.assist_button.has_pro_tag() is True
    # Every control is named and reachable with Tab.
    assert t.accessibleName() == "Meeting reminder"
    for b in _buttons(t):
        assert b.accessibleName().strip(), b
        assert b.focusPolicy() & Qt.TabFocus
    assert t.width() == mr.TOAST_W + 2 * mr.SH_SIDE
    assert not t.grab().isNull()


def test_reminder_size_matches_the_mockup(toast, mr):
    t, _ = toast("free")
    plate = t.plate_rect()
    assert round(plate.width()) == mr.TOAST_W - 1
    assert 218 <= plate.height() <= 230        # the board's toast is 224 px tall


def test_after_the_start_the_line_turns_amber(toast):
    t, _ = toast("free", meeting(-125))
    assert t.when_text() == "Started 2 min ago · Record now?"
    assert t.started()
    from ui import theme
    assert theme.qss_color("amber-tx") in t.when_label.styleSheet()


def test_free_record_opens_the_pro_window(toast):
    t, app = toast("free")
    got, during = [], []

    def upsell(feature=None):
        app.calls.append(("_pro_upsell", feature))
        during.append((t.isVisible(), t.is_showing()))     # hidden, but still "the one toast"
    app._pro_upsell = upsell
    t.record.connect(lambda m, a: got.append((m["id"], a)))
    t.record_button.click()
    assert ("_pro_upsell", "Meeting recording") in app.calls
    assert during == [(False, True)]
    assert got == []
    assert t.isVisible()                       # still due: back after the Pro window
    t.assist_button.click()
    assert ("_pro_upsell", "Live Assistance") in app.calls
    assert got == []


def test_free_user_who_upgrades_in_the_pro_window_records(toast):
    t, app = toast("free")
    app._pro_upsell = lambda feature=None: app.set_tier("pro")
    got, closed = [], []
    t.record.connect(lambda m, a: got.append((m["id"], a)))
    t.closed.connect(lambda m, why: closed.append(why))
    t.record_button.click()
    assert got == [("evt-1", False)]
    assert closed == ["record"] and not t.isVisible()


@pytest.mark.parametrize("with_assist", [False, True])
def test_pro_record_emits_and_hides(toast, with_assist):
    t, app = toast("pro")
    got, closed = [], []
    t.record.connect(lambda m, a: got.append((m["title"], a)))
    t.closed.connect(lambda m, why: closed.append(why))
    (t.assist_button if with_assist else t.record_button).click()
    assert got == [("Weekly product sync", with_assist)]
    assert closed == ["record"]
    assert not t.isVisible()
    assert not any(c[0] == "_pro_upsell" for c in app.calls)


def test_not_this_one_and_escape_dismiss(toast):
    t, _ = toast("pro")
    got = []
    t.dismissed.connect(lambda m: got.append(m["id"]))
    t.dismiss_button.click()
    assert got == ["evt-1"] and not t.isVisible()
    t.show_meeting(meeting())
    QTest.keyClick(t, Qt.Key_Escape)
    assert got == ["evt-1", "evt-1"] and not t.isVisible()


def test_join_opens_only_https_links(toast, mr, monkeypatch):
    opened = []
    monkeypatch.setattr(mr.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString())))
    t, _ = toast("free")
    joined = []
    t.join.connect(lambda m: joined.append(m["id"]))
    t.join_button.click()
    assert opened == ["https://meet.google.com/abc-defg-hij"]
    assert joined == ["evt-1"]
    assert t.isVisible()                       # still there: Record after joining
    assert t.join_button.toolTip() == "meet.google.com"
    t.show_meeting(meeting(link="http://meet.google.com/abc"))
    assert not t.join_button.isVisibleTo(t)
    t._on_join()
    assert opened == ["https://meet.google.com/abc-defg-hij"]
    t.show_meeting(meeting(link="", call_type=""))
    assert not t.join_button.isVisibleTo(t)
    assert not t.call_mark.isVisibleTo(t)
    assert t.meta_label.full_text() == "Anna, David +2"


def test_closes_itself_when_should_prompt_turns_false(toast, mr):
    clock = {"now": NOW}
    t, _ = toast("pro", clock=lambda: clock["now"])
    closed = []
    t.closed.connect(lambda m, why: closed.append(why))
    assert t._timer.isActive() and t._timer.interval() == mr.REFRESH_MS
    clock["now"] = NOW + timedelta(seconds=50 + 150)      # 2.5 min after the start
    t.refresh()
    assert t.isVisible() and t.when_text() == "Started 2 min ago · Record now?"
    clock["now"] = NOW + timedelta(seconds=50 + 301)      # past the 5 min grace
    t.refresh()
    assert not t.isVisible() and closed == ["expired"]
    assert not t._timer.isActive()


def test_a_meeting_no_longer_due_is_not_shown(toast):
    t, _ = toast("pro", show=False)
    closed = []
    t.closed.connect(lambda m, why: closed.append(why))
    assert t.show_meeting(meeting(-20 * 60)) is False
    assert not t.isVisible() and closed == ["expired"]
    assert t.show_meeting(meeting(10 * 60)) is False      # too early (> 1 min before)


def test_sits_bottom_right_inside_the_available_area(toast, mr):
    t, _ = toast("pro")
    scr = t.screen() or QApplication.primaryScreen()
    a = scr.availableGeometry()
    g = t.frameGeometry()
    assert a.contains(g.topLeft()) and a.contains(g.bottomRight())
    if not mr.glass.IS_MAC:
        assert g.right() == a.right() and g.bottom() == a.bottom()


def test_plan_change_moves_the_pro_tag(toast):
    t, app = toast("free")
    assert t.record_button.has_pro_tag()
    app.set_tier("pro")
    t.refresh_account()
    assert not t.record_button.has_pro_tag()
    assert t.assist_button.has_pro_tag()


def test_meta_line_variants(mr):
    assert mr.meta_line({"call_type": "other", "people": "Anna"}) == "Video call  ·  Anna"
    assert mr.meta_line({"call_type": "Zoom", "people": ""}) == "Zoom"
    assert mr.meta_line({"call_type": "", "others": ["Anna Petrosyan", "David Lee", "Mariam", "Lilit"]}) == \
        "Anna, David +2"
    assert mr.join_link({"link": "https://zoom.us/j/1"}) == "https://zoom.us/j/1"
    assert mr.join_link({"link": "zoom.us/j/1"}) == ""
    assert mr.when_line(meeting(50), NOW) == ("Starts in 1 min", False)
    assert mr.when_line(meeting(-30), NOW) == ("Starting now · Record now?", True)


def test_long_titles_are_cut_with_the_full_text_kept(toast):
    title = "Quarterly planning with the whole product and design team and friends"
    t, _ = toast("pro", meeting(title=title))
    assert t.title_label.full_text() == title
    assert t.title_label.text().endswith("…")
    assert t.title_label.toolTip() == title


def test_privacy_hook_and_hide_are_safe(toast):
    t, _ = toast("pro")
    t.on_setting_changed("overlay_private")
    t.on_setting_changed("something_else")
    closed = []
    t.closed.connect(lambda m, why: closed.append(why))
    t.hide_reminder()
    assert closed == ["hidden"] and not t.isVisible()
    from ui.floating.meeting_reminder import is_showing
    assert is_showing(t) is False and is_showing(None) is False


def test_backdrop_resample_runs_off_the_gui_thread(toast, mr, monkeypatch):
    import threading
    t, _ = toast("pro")
    seen = []

    def grab(x, y, w, h):
        seen.append((threading.current_thread() is threading.main_thread(), (x, y, w, h)))
        return b"\xc8" * (w * h * 4)                          # a light grey desktop
    monkeypatch.setattr(mr.glass, "window_rect", lambda hwnd: (100, 50, 100 + 2 * t.width(), 50 + 2 * t.height()))
    monkeypatch.setattr(mr.glass, "grab_screen", grab)
    t._hwnd, t._excluded, t._bd = 4242, True, None
    t._resample()
    assert _wait(lambda: t._bd is not None)
    assert seen == [(False, (100, 50, 2 * t.width(), 2 * t.height()))]
    assert (t._bd.width(), t._bd.height()) == (t.width(), t.height())   # physical grab -> toast size
    assert not t._sampling
    # a black grab (hardware video) leaves the plain plate
    monkeypatch.setattr(mr.glass, "grab_screen", lambda x, y, w, h: b"\x00" * (w * h * 4))
    t._resample()
    assert _wait(lambda: t._bd is None and not t._sampling)
    # a grab that lands after the toast closed is dropped
    monkeypatch.setattr(mr.glass, "grab_screen", grab)
    t._resample()
    t.hide_reminder()
    QTest.qWait(200)
    assert t._bd is None
    # not excluded from capture (or not native): no re-sampling at all
    t.show_meeting(meeting())
    t._excluded = False
    t._sample_timer.start()
    t._resample()
    assert not t._sample_timer.isActive() and not t._sampling


def test_take_focus_gives_record_a_visible_focus_ring(toast):
    from PySide6.QtWidgets import QLineEdit
    t, _ = toast("free")
    other = QLineEdit()                          # the app the user is typing in
    other.show()
    other.activateWindow()
    assert _wait(lambda: other.isActiveWindow() and not t.isActiveWindow())
    t.take_focus()
    assert _wait(lambda: t.record_button.hasFocus())
    other.deleteLater()
    QApplication.processEvents()
    # Keyboard focus: the ring shows (the activation's own focus-in would hide it).
    assert t.record_button.property("focusVisible") is True
    QTest.keyClick(QApplication.focusWidget(), Qt.Key_Tab)
    assert t.assist_button.hasFocus() and t.assist_button.property("focusVisible") is True
    assert t.record_button.property("focusVisible") is False
    got = []
    t.dismissed.connect(lambda m: got.append(m["id"]))
    QTest.keyClick(QApplication.focusWidget(), Qt.Key_Escape)
    assert got == ["evt-1"] and not t.isVisible()
    t.take_focus()                               # hidden: nothing happens
    assert not t._kbd_focus


def test_showing_again_while_up_keeps_the_backdrop(toast, mr):
    from PySide6.QtGui import QImage
    t, _ = toast("pro")
    bd = QImage(8, 8, QImage.Format_RGB32)
    t._bd = bd
    # Up already: a fresh grab would have the toast itself in it, so the sample stays.
    assert t.show_meeting(meeting(title="Design review")) is True
    assert t._bd is bd and t.title_label.full_text() == "Design review"
    t.dismiss()
    assert t.show_meeting(meeting()) is True     # shown from hidden: the old sample goes
    assert t._bd is None


def test_secondary_and_ghost_buttons_keep_their_hover(toast):
    t, _ = toast("pro")
    # Their own rules beat the inherited :hover, so the toast restates it.
    assert ":hover" in t.assist_button.styleSheet() and ":pressed" in t.assist_button.styleSheet()
    assert ":hover" in t.dismiss_button.styleSheet()


def test_privacy_switch_turned_on_starts_the_resample(toast, monkeypatch):
    t, _ = toast("pro")
    monkeypatch.setattr(t, "_apply_private", lambda: setattr(t, "_excluded", True))
    assert not t._sample_timer.isActive()
    t.on_setting_changed("overlay_private")
    assert t._sample_timer.isActive()
    t.dismiss()
    assert not t._sample_timer.isActive()


def test_without_an_app_record_is_not_gated(mr):
    t = mr.MeetingReminder(None, clock=lambda: NOW)
    got = []
    t.record.connect(lambda m, a: got.append(a))
    t.show_meeting(meeting())
    t.record_button.click()
    assert got == [False]
    t.deleteLater()


# ── the tray menu ────────────────────────────────────────────────────────────
@pytest.fixture
def tm(qapp):
    from ui import tray_menu
    return tray_menu


class FakeCalendar(QObject):
    changed = Signal(object)
    disconnected = Signal(str)
    available = True
    is_connected = True

    def __init__(self, m=None):
        super().__init__()
        self.m = m

    def next_meeting(self):
        return self.m


@pytest.fixture
def tray(tm, monkeypatch):
    from ui.pages import home
    monkeypatch.setattr(home, "load_sources", lambda: ([], [], []))
    made = []

    def build(tier="free", custom=True, calendar=None, **cfg):
        app = full_app(tier)
        app.cfg.update(cfg)
        app.calendar = calendar
        tr = tm.build_tray_menu(app, QMenu(), custom=custom)
        made.append(tr)
        return tr, app
    yield build
    for tr in made:
        tr.menu.hide()
        tr.menu.deleteLater()


def _keys(tr, menu=None):
    return [k for k, _t in tr.visible_texts(menu)]


def test_tray_rows_free(tray):
    from app_info import APP_VERSION
    tr, _ = tray("free")
    assert tr.visible_texts() == [
        ("header", "Gusan  v" + APP_VERSION), ("open", "Open Gusan"), ("dictate", "Dictate"),
        ("meeting", "Record a meeting…"), ("assist", "Live Assistant"), ("convert", "Convert audio to text…"),
        ("recent", "Recent"), ("privacy", "Privacy mode"),
        ("account", "aram@aibuben.xyz  ·  Free"), ("upgrade", "Upgrade to Pro…"),
        ("settings", "Settings…"), ("quit", "Quit Gusan")]
    assert tr.menu.defaultAction() is tr.actions["open"]
    assert not tr.actions["header"].isEnabled() and not tr.actions["account"].isEnabled()


def test_tray_rows_pro_and_guest(tray):
    tr, _ = tray("pro")
    keys = _keys(tr)
    assert "upgrade" not in keys and "signin" not in keys
    assert dict(tr.visible_texts())["account"].endswith("Pro")
    tr, _ = tray("guest")
    keys = _keys(tr)
    assert "account" not in keys and "upgrade" not in keys
    assert dict(tr.visible_texts())["signin"] == "Sign in…"


def test_tray_pro_tags_and_hints(tray, tm):
    tr, _ = tray("free")
    a = tr.actions
    assert a["meeting"].property(tm.P_PRO) and a["assist"].property(tm.P_PRO)
    assert not a["dictate"].property(tm.P_PRO)
    assert a["dictate"].property(tm.P_HINT) == "Alt+R"
    assert a["assist"].property(tm.P_HINT) == "Ctrl+Alt+A"
    assert a["meeting"].toolTip() == "Pro feature"
    assert a["upgrade"].property(tm.P_KIND) == "upgrade"
    # every visible row has words (the accessible name of a menu item)
    for _k, text in tr.visible_texts():
        assert text.strip()


def test_tray_free_actions_call_the_app(tray):
    tr, app = tray("free")
    a = tr.actions
    for key in ("open", "dictate", "meeting", "assist", "convert", "upgrade", "settings", "quit"):
        a[key].trigger()
    # Live Assistant opens the card for Free too (its Start asks for Pro); Record a meeting is Pro.
    assert app.calls == [("show_main",), ("_on_hotkey",), ("_pro_upsell", "Meeting recording"),
                         ("toggle_live_assist",), ("show_main", "audio_to_text"),
                         ("_pro_upsell", None), ("show_main", "settings"), ("quit_app",)]


def test_tray_live_assistant_opens_the_card_for_guests_too(tray):
    tr, app = tray("guest")
    tr.actions["assist"].trigger()
    assert app.calls == [("toggle_live_assist",)]               # never the Pro window just to open it


def test_tray_pro_actions_call_the_app(tray):
    tr, app = tray("pro")
    app.toggle_dictation = lambda: app.calls.append(("toggle_dictation",))
    for key in ("dictate", "meeting", "assist"):
        tr.actions[key].trigger()
    # Record a meeting… opens the Meetings page (not the classic window).
    assert app.calls == [("toggle_dictation",), ("show_main", "meetings"), ("toggle_live_assist",)]


@pytest.mark.parametrize("custom", [True, False])
def test_tray_pro_rows_say_pro_to_screen_readers(tray, tm, custom):
    tr, _ = tray("free", custom=custom)
    # A screen reader reads the QAction text: the PRO pill is painted, so the words stay in the text.
    assert tr.spoken_text("meeting") == "Record a meeting…  (Pro)"
    assert tr.spoken_text("assist").startswith("Live Assistant  (Pro)")
    assert tr.spoken_text("dictate").split("\t")[0] == "Dictate"
    seen = dict(tr.visible_texts())
    if custom:                                   # painted: the pill, not the words
        assert seen["meeting"] == "Record a meeting…" and seen["assist"] == "Live Assistant"
    else:                                        # macOS: the words are the tag
        assert seen["meeting"] == "Record a meeting…  (Pro)"


def test_tray_attach_calendar_connects_once(tray):
    calls = []

    class CountingCalendar(FakeCalendar):
        def next_meeting(self):
            calls.append(1)
            return None
    cal = CountingCalendar()
    tr, _ = tray("free", calendar=cal)
    tr.attach_calendar(cal)
    tr.attach_calendar(cal)                      # Wave 4 may call it on every rebuild
    calls.clear()
    cal.changed.emit([])
    assert calls == [1]
    tr.attach_calendar(None)                     # no calendar: only a refresh, no error


def test_tray_guest_signs_in(tray):
    tr, app = tray("guest")
    tr.actions["signin"].trigger()
    assert app.calls == [("show_auth_gate",)]


def test_tray_privacy_goes_through_set_setting(tray):
    tr, app = tray("free")
    pv = tr.actions["privacy"]
    assert pv.isCheckable() and not pv.isChecked()
    pv.trigger()
    assert ("set_setting", "privacy_mode", True) in app.calls
    assert app.cfg["privacy_mode"] is True and pv.isChecked()
    assert ("show_tray_hint", "Privacy mode on", "Everything stays on this computer.") in app.calls
    # refused: the check goes back to what the config says, no hint
    app.calls.clear()
    app.set_setting = lambda k, v: app.calls.append(("set_setting", k, v)) or False
    pv.trigger()
    assert ("set_setting", "privacy_mode", False) in app.calls
    assert pv.isChecked() is True
    assert not any(c[0] == "show_tray_hint" for c in app.calls)


def test_tray_next_row_follows_the_calendar(tray, tm):
    nxt = {"id": "n", "title": "Weekly product sync", "start": NOW + timedelta(minutes=10),
           "end": NOW + timedelta(minutes=40)}
    cal = FakeCalendar(nxt)
    tr, app = tray("free", calendar=cal)
    texts = dict(tr.visible_texts())
    assert texts["next"] == "Next: Weekly product sync"
    hint = tr.actions["next"].property(tm.P_HINT)
    assert hint.endswith(nxt["start"].astimezone().strftime("%H:%M"))
    assert tr.actions["sep_next"].isVisible()
    tr.actions["next"].trigger()
    assert app.calls[-1] == ("show_main", "meetings")
    # the calendar says "nothing next": the row and its separator go
    tr.attach_calendar(cal)
    cal.m = None
    cal.changed.emit([])
    assert "next" not in _keys(tr) and not tr.actions["sep_next"].isVisible()
    cal.m = nxt
    tr.on_setting_changed("calendar_only_with_link")
    assert "next" in _keys(tr)
    cal.is_connected = False
    tr.refresh_next()
    assert "next" not in _keys(tr)


def test_tray_recent_fills_from_home(tray, tm, monkeypatch):
    from ui.pages import home
    now = datetime.now()
    items = [
        dict(kind="dictation", when=now, main="Hi Anna, following up on the launch", open=("dictation", {"entry": "x"})),
        dict(kind="smart", when=now - timedelta(minutes=40), main="Hi David & the team", open=("smart_actions", {})),
        dict(kind="audio", when=now - timedelta(days=1), main="lecture_week3.mp4",
             open=("audio_to_text", {"transcript": "t1"})),
        dict(kind="meeting", when=now - timedelta(days=3), main="Q4 roadmap", open=("meetings", {"meeting": "d"})),
        dict(kind="meeting", when=now - timedelta(days=9), main="Older", open=("meetings", {})),
    ]
    monkeypatch.setattr(home, "recent_items", lambda *a: list(items))
    tr, app = tray("pro")
    tr.refresh_recent(sync=True)
    rows = tr.visible_texts(tr.recent)
    assert [t for _k, t in rows] == ["Hi Anna, following up on the launch", "Hi David & the team",
                                      "lecture_week3.mp4", "Q4 roadmap", "Show all in Gusan"]
    acts = [a for a in tr.recent.actions() if not a.isSeparator()]
    assert acts[0].property(tm.P_ICON) == "mic" and acts[1].property(tm.P_ICON) == "wand"
    assert acts[2].property(tm.P_ICON) == "wave" and acts[3].property(tm.P_ICON) == "people"
    assert acts[2].property(tm.P_HINT) == "Yesterday"
    acts[2].trigger()
    assert app.calls[-1] == ("show_main", "audio_to_text", ("transcript", "t1"))
    tr.actions["show_all"].trigger()
    assert app.calls[-1] == ("show_main", "home")
    # empty stores
    monkeypatch.setattr(home, "recent_items", lambda *a: [])
    tr.refresh_recent(sync=True)
    assert [t for _k, t in tr.visible_texts(tr.recent)] == ["Nothing yet", "Show all in Gusan"]


def test_tray_recent_loads_off_the_gui_thread(tray, monkeypatch):
    import threading
    from ui.pages import home
    seen = {}

    def load():
        seen["thread"] = threading.current_thread() is threading.main_thread()
        return ([], [], [])
    monkeypatch.setattr(home, "load_sources", load)
    monkeypatch.setattr(home, "recent_items",
                        lambda *a: [dict(kind="dictation", when=datetime.now(), main="Hi", open=("dictation", {}))])
    tr, _ = tray("free")
    tr.menu.aboutToShow.emit()                 # the menu opening starts the load
    assert _wait(lambda: tr.recent_items())
    assert seen["thread"] is False
    assert [t for _k, t in tr.visible_texts(tr.recent)] == ["Hi", "Show all in Gusan"]


def test_tray_refreshes_when_it_opens(tray):
    tr, app = tray("free")
    app.set_tier("pro")
    app.cfg["hotkey"] = "ctrl+shift+space"
    tr.menu.aboutToShow.emit()
    assert "upgrade" not in _keys(tr)
    assert tr.actions["dictate"].property("gusanHint") == "Ctrl+Shift+Space"
    assert tr.tooltip() == "Gusan - Pro\naram@aibuben.xyz"


def test_tray_hotkey_hints_follow_settings(tray):
    tr, app = tray("free")
    app.cfg["live_assist_hotkey"] = "alt+e"
    tr.on_setting_changed("live_assist_hotkey")
    assert tr.actions["assist"].property("gusanHint") == "Alt+E"
    app._registered_assist_hotkey = None         # the real controller: it didn't register
    tr.refresh_hotkeys()
    assert tr.actions["assist"].property("gusanHint") == ""
    app._registered_assist_hotkey = "ctrl+alt+a"
    tr.refresh_hotkeys()
    assert tr.actions["assist"].property("gusanHint") == "Ctrl+Alt+A"
    app.cfg["privacy_mode"] = True
    tr.on_setting_changed("privacy_mode")
    assert tr.actions["privacy"].isChecked()


def test_tray_paints_the_mockup_rows(tray, tm, caplog):
    caplog.set_level(logging.DEBUG, logger="transcribe")
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)      # the row reads the real clock
    cal = FakeCalendar({"id": "n", "title": "Weekly product sync", "start": soon,
                        "end": soon + timedelta(minutes=30)})
    tr, _ = tray("free", calendar=cal)
    tr.menu.popup(QPoint(300, 200))
    QApplication.processEvents()
    tr.menu.setActiveAction(tr.actions["meeting"])
    QApplication.processEvents()
    img = tr.menu.grab()
    assert not img.isNull()
    if " " in tr.actions["next"].property(tm.P_HINT):              # "Thu 00:05" just before midnight
        assert tm.MENU_W <= tr.menu.width() <= tm.MAX_W
    else:
        assert tr.menu.width() == tm.MENU_W
    g = tr.menu.actionGeometry
    assert g(tr.actions["header"]).height() == tm.H_HEAD
    assert g(tr.actions["open"]).height() == tm.H_ITEM
    assert g(tr.actions["account"]).height() == tm.H_INFO
    assert g(tr.actions["sep_open"]).height() == tm.H_SEP
    tr.menu.hide()
    bad = [r.getMessage() for r in caplog.records if "menu item" in r.getMessage()]
    assert bad == []


def test_tray_plain_rows_for_native_menus(tray):
    tr, _ = tray("free", custom=False)
    texts = dict(tr.visible_texts())
    assert texts["meeting"] == "Record a meeting…  (Pro)"
    assert texts["assist"] == "Live Assistant  (Pro)\tCtrl+Alt+A"
    assert texts["dictate"] == "Dictate\tAlt+R"
    assert tr.menu.style().inherits("QProxyStyle") is False


def test_tray_tooltip(tray):
    assert tray("free")[0].tooltip() == "Gusan\naram@aibuben.xyz"
    assert tray("guest")[0].tooltip() == "Gusan"


def test_tray_escapes_ampersands(tray, monkeypatch):
    from ui.pages import home
    monkeypatch.setattr(home, "recent_items",
                        lambda *a: [dict(kind="smart", when=datetime.now(), main="Q&A notes", open=("smart_actions", {}))])
    tr, _ = tray("free")
    tr.refresh_recent(sync=True)
    a = [x for x in tr.recent.actions() if not x.isSeparator()][0]
    assert a.text() == "Q&&A notes"
    assert tr.visible_texts(tr.recent)[0][1] == "Q&A notes"
