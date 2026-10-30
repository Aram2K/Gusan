"""Stop on the Live Assistant opens the meeting (Part A of "Ask about this meeting"), offscreen.

Three pieces, each against its own fakes: the Live card's Stop (ui/live_assist.py: when it asks the app to open the
meeting, and when it does not), the controller's policy (main.AppController.open_meeting_after_live_stop: the window
opens at once - or, where it could expose the notes and the chat in a screen share, hidden from share before it is
shown, or not at all with a tray notice instead) and the Meetings page (open with writing=True lands on the notes
view of a meeting whose notes are still being written, which fills in by itself)."""
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from tests_qt.chat_fakes import FakeSession, make_meeting
from tests_qt.conftest import make_stub_app


def _pump(cond=None, timeout=3.0):
    end = time.monotonic() + timeout
    turns = 0
    while time.monotonic() < end:
        QApplication.processEvents()
        turns += 1
        if cond is None and turns >= 8:
            return True
        if cond is not None and cond():
            return True
        time.sleep(0.005)
    return cond() if cond is not None else True


# ── the Live card's Stop ──────────────────────────────────────────────────────────────────────────────────────────

class _CardApp(QObject):
    sig_setting_changed = Signal(str)

    def __init__(self, cfg=None):
        super().__init__()
        self.cfg = dict(cfg or {})
        self.opened = []
        self.tracked = []

    def schedule_config_save(self):
        pass

    def save_config(self):
        pass

    def track(self, event, props=None):
        self.tracked.append(event)

    def _pro_upsell(self, feature=None):
        pass

    def open_meeting_after_live_stop(self, folder):
        self.opened.append(folder)
        return True


@pytest.fixture
def card(qapp):
    from ui import glass, live_assist
    made = []
    with mock.patch.object(glass, "capture_exclusion_supported", return_value=True), \
            mock.patch.object(glass, "is_remote_session", return_value=False), \
            mock.patch.object(glass, "exclude_from_capture", return_value=True), \
            mock.patch.object(glass, "is_excluded_from_capture", return_value=True), \
            mock.patch.object(glass, "macos_26_or_later", return_value=False):
        def build(app=None):
            app = app or _CardApp()
            c = live_assist.LiveAssistOverlay(main_app=app)
            made.append(c)
            return c, app
        yield build
        for c in made:
            c.hide()
            c.deleteLater()
        QApplication.processEvents()


class _Session:
    """What the card reads and calls: state, source, meeting_dir, elapsed_sec(), stop()."""

    def __init__(self, source="live_assist", elapsed=75, stop_result=True, folder="C:/lib/20261030_153400"):
        self.state = "recording"
        self.source = source
        self.meeting_dir = folder
        self._elapsed = elapsed
        self.stop_result = stop_result
        self.stops = 0

    def elapsed_sec(self, now=None):
        return self._elapsed

    def stop(self, *a, **k):
        self.stops += 1
        return self.stop_result


def _press_stop(c, app, session, live_text="Some words were said."):
    app.meeting_session = session
    c._live_text = live_text
    c._stop_listening()
    _pump()


def test_stop_on_a_live_session_that_recorded_asks_the_app_to_open_the_meeting_once(card):
    c, app = card()
    s = _Session()
    _press_stop(c, app, s)
    assert s.stops == 1
    assert app.opened == ["C:/lib/20261030_153400"]
    assert c.lbl_status.text() == "Stopped - your notes will be in Meetings."


def test_the_jump_waits_for_the_card_to_repaint(card):
    c, app = card()
    app.meeting_session = _Session()
    c._live_text = "words"
    c._stop_listening()
    assert app.opened == []                      # not inside the Stop itself: a turn of the loop later
    _pump()
    assert len(app.opened) == 1


@pytest.mark.parametrize("source", ["page", "calendar", "classic", ""])
def test_a_session_started_from_meetings_or_the_calendar_keeps_its_own_way(card, source):
    c, app = card()
    s = _Session(source=source)
    _press_stop(c, app, s)
    assert s.stops == 1 and app.opened == []


def test_a_stop_that_did_not_stop_opens_nothing(card):
    c, app = card()
    _press_stop(c, app, _Session(stop_result=False))
    assert app.opened == []


def test_nothing_recorded_opens_nothing_but_five_seconds_or_some_words_count(card):
    c, app = card()
    _press_stop(c, app, _Session(elapsed=3), live_text="   ")
    assert app.opened == []
    _press_stop(c, app, _Session(elapsed=5), live_text="")
    assert len(app.opened) == 1
    c2, app2 = card()
    _press_stop(c2, app2, _Session(elapsed=1), live_text="Hello")
    assert len(app2.opened) == 1


def test_a_session_with_no_folder_opens_nothing(card):
    c, app = card()
    _press_stop(c, app, _Session(folder=None))
    assert app.opened == []


def test_an_app_without_the_opener_just_stops(card):
    app = SimpleNamespace(cfg={}, schedule_config_save=lambda: None, save_config=lambda: None, track=lambda *a, **k: None)
    from ui import live_assist
    c = live_assist.LiveAssistOverlay(main_app=app)
    try:
        s = _Session()
        app.meeting_session = s
        c._live_text = "words"
        c._stop_listening()
        _pump()
        assert s.stops == 1
        assert c.lbl_status.text() == "Stopped - your notes will be in Meetings."
    finally:
        c.hide()
        c.deleteLater()


def test_a_session_that_cannot_tell_its_facts_still_stops(card):
    class Odd(_Session):
        @property
        def meeting_dir(self):
            raise RuntimeError("no folder yet")

        @meeting_dir.setter
        def meeting_dir(self, value):
            pass
    c, app = card()
    s = Odd()
    _press_stop(c, app, s)
    assert s.stops == 1 and app.opened == []


def test_an_opener_that_raises_is_not_the_end_of_the_card(card):
    c, app = card()

    def boom(folder):
        raise RuntimeError("window failed")
    app.open_meeting_after_live_stop = boom
    _press_stop(c, app, _Session())
    assert c.lbl_status.text() == "Stopped - your notes will be in Meetings."


def test_a_second_press_does_nothing_more(card):
    c, app = card()
    s = _Session()
    _press_stop(c, app, s)
    s.state = "processing"
    c._stop_listening()
    _pump()
    assert s.stops == 1 and len(app.opened) == 1


# ── the controller's policy ──────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def main_mod(qapp):
    import main
    return main


class _Window:
    def __init__(self, log, hide_ok=True):
        self.log, self.hide_ok = log, hide_ok

    def hide_from_share(self, on=True):
        self.log.append(("hide_from_share", on))
        return self.hide_ok if on else False

    def hide(self):
        self.log.append(("hide",))


def _controller(log, private=True, quitting=False, hide_ok=True, shown=True):
    win = _Window(log, hide_ok)

    def show_main(page=None, **opts):
        log.append(("show_main", page, opts))
        return win if shown else None

    def ensure():
        log.append(("ensure",))
        return win
    return SimpleNamespace(
        cfg={"live_assist_private": private}, _quitting=quitting, show_main=show_main, _ensure_main_window=ensure,
        show_tray_hint=lambda title, text, on_click=None: log.append(("hint", title, text, on_click))), win


@pytest.fixture
def glass_state():
    """glass.* as set by the test: supported, remote, exclusion confirmed after the window is shown."""
    from ui import glass
    state = SimpleNamespace(supported=True, remote=False, excluded=True)
    with mock.patch.object(glass, "capture_exclusion_supported", lambda: state.supported), \
            mock.patch.object(glass, "is_remote_session", lambda: state.remote), \
            mock.patch.object(glass, "is_excluded_from_capture", lambda w: state.excluded):
        yield state


FOLDER = "C:/lib/20261030_153400"


def test_the_window_opens_on_the_meeting_when_the_card_is_not_private(main_mod, glass_state):
    log = []
    ctl, win = _controller(log, private=False)
    assert main_mod.AppController.open_meeting_after_live_stop(ctl, FOLDER) is True
    assert log == [("show_main", "meetings", {"meeting": FOLDER, "writing": True})]


def test_a_private_card_gets_a_window_that_is_hidden_from_share_before_it_is_shown(main_mod, glass_state):
    log = []
    ctl, win = _controller(log)
    assert main_mod.AppController.open_meeting_after_live_stop(ctl, FOLDER) is True
    assert log == [("ensure",), ("hide_from_share", True),
                   ("show_main", "meetings", {"meeting": FOLDER, "writing": True})]      # hidden FIRST, then shown
    assert not any(e[0] == "hint" for e in log)


def test_when_the_os_refuses_to_hide_the_window_nothing_opens_and_a_notice_says_why_it_waits(main_mod, glass_state):
    log = []
    ctl, win = _controller(log, hide_ok=False)
    assert main_mod.AppController.open_meeting_after_live_stop(ctl, FOLDER) is False
    assert not any(e[0] == "show_main" for e in log)
    hint = [e for e in log if e[0] == "hint"][0]
    assert hint[1].endswith("is writing your notes") and hint[2] == "Click to open the meeting."
    for word in ("limit", "armenia"):
        assert word not in (hint[1] + hint[2]).lower()
    hint[3]()                                          # the user's own click opens it
    assert log[-1] == ("show_main", "meetings", {"meeting": FOLDER, "writing": True})


def test_a_window_the_os_stopped_hiding_after_it_was_shown_goes_away_again(main_mod, glass_state):
    glass_state.excluded = False                       # the OS says no once the window is up
    log = []
    ctl, win = _controller(log)
    assert main_mod.AppController.open_meeting_after_live_stop(ctl, FOLDER) is False
    names = [e[0] for e in log]
    assert names[:3] == ["ensure", "hide_from_share", "show_main"]
    assert ("hide",) in log and ("hide_from_share", False) in log
    assert names.index("hide") > names.index("show_main")
    assert names[-1] == "hint"


@pytest.mark.parametrize("supported,remote", [(False, False), (True, True), (False, True)])
def test_where_hiding_cannot_be_promised_only_a_notice_comes(main_mod, glass_state, supported, remote):
    glass_state.supported, glass_state.remote = supported, remote
    log = []
    ctl, win = _controller(log)
    assert main_mod.AppController.open_meeting_after_live_stop(ctl, FOLDER) is False
    assert [e[0] for e in log] == ["hint"]             # the window was not even built
    log[0][3]()
    assert log[-1][0] == "show_main"


def test_a_window_that_could_not_be_built_falls_back_to_the_notice(main_mod, glass_state):
    log = []
    ctl, win = _controller(log, shown=False)
    assert main_mod.AppController.open_meeting_after_live_stop(ctl, FOLDER) is False
    assert [e[0] for e in log][-1] == "hint"
    assert ("hide_from_share", False) in log


def test_an_error_while_hiding_is_a_notice_not_an_open_window(main_mod, glass_state):
    log = []
    ctl, win = _controller(log)
    ctl._ensure_main_window = mock.Mock(side_effect=RuntimeError("no window"))
    assert main_mod.AppController.open_meeting_after_live_stop(ctl, FOLDER) is False
    assert [e[0] for e in log] == ["hint"]


def test_while_the_app_quits_nothing_opens(main_mod, glass_state):
    log = []
    ctl, win = _controller(log, quitting=True)
    assert main_mod.AppController.open_meeting_after_live_stop(ctl, FOLDER) is False
    assert log == []
    ctl, win = _controller(log, private=False, quitting=True)
    assert main_mod.AppController.open_meeting_after_live_stop(ctl, FOLDER) is False
    assert log == []


def test_no_folder_no_window(main_mod, glass_state):
    log = []
    ctl, win = _controller(log, private=False)
    for folder in ("", None):
        assert main_mod.AppController.open_meeting_after_live_stop(ctl, folder) is False
    assert log == []


def test_the_default_is_private(main_mod, glass_state):
    log = []
    ctl, win = _controller(log)
    ctl.cfg = {}                                       # a config that never mentions it
    main_mod.AppController.open_meeting_after_live_stop(ctl, FOLDER)
    assert log[1] == ("hide_from_share", True)


def test_the_real_notice_for_a_finished_meeting_is_not_sent_while_meetings_is_on_screen(main_mod):
    # (existing behaviour the jump relies on: the page is on screen, so no "Notes ready" hint)
    stub = SimpleNamespace(
        notify_history_changed=lambda: None, _maybe_auto_send_notion=lambda *a: None, _pending_notes_queue=None,
        _quitting=False, meeting_session=SimpleNamespace(last_run_speakers=False, _meeting_title="Live session"),
        cfg={"notify_when_ready": True},
        main_win=SimpleNamespace(isVisible=lambda: True, isMinimized=lambda: False, current_route=lambda: "meetings"),
        show_tray_hint=mock.Mock(), show_main=mock.Mock())
    stub._page_on_screen = lambda route: main_mod.AppController._page_on_screen(stub, route)
    main_mod.AppController._on_meeting_finished(stub, FOLDER, "## Notes", "")
    stub.show_tray_hint.assert_not_called()
    stub.main_win.current_route = lambda: "home"                          # somewhere else: the usual notice
    main_mod.AppController._on_meeting_finished(stub, FOLDER, "## Notes", "")
    stub.show_tray_hint.assert_called_once()


# ── the Meetings page ────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _inline(monkeypatch):
    from ui.pages import meeting_views
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)
    import ui.winfit
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)


@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


@pytest.fixture
def window(qapp, monkeypatch, meetings_root):
    from ui import shell as shell_mod
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    made = []

    def build(phase="processing", folder=None, final=False):
        app = make_stub_app("pro")
        session = FakeSession(phase)
        session.final = final
        session.meeting_dir = folder
        app.meeting_session = session
        win = shell_mod.MainWindow(app)
        made.append(win)
        win.resize(1280, 820)
        win.show()
        _pump()
        return win, session

    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def _labels(widget):
    from PySide6.QtWidgets import QLabel
    return [lb.text() for lb in widget.findChildren(QLabel) if lb.isVisibleTo(widget)]


def test_open_with_writing_lands_on_the_notes_view_of_a_meeting_being_written(window, meetings_root):
    f = make_meeting(meetings_root, notes="")
    win, session = window("processing", f)
    page = win.open("meetings", meeting=str(f), writing=True)
    _pump()
    assert page.current_view() == "notes"
    nv = page.notes_view
    assert nv.tabs.index() == 0 and nv.stack.currentIndex() == 0          # the Notes tab
    assert nv.folder == str(f)
    assert nv.title_label.text() == "Live session 15:34"
    assert any("Writing notes" in t for t in _labels(nv))               # the page's own banner, until the notes come
    assert nv.chat.isVisibleTo(nv)


def test_without_writing_a_meeting_being_written_still_opens_on_the_live_view(window, meetings_root):
    f = make_meeting(meetings_root, notes="")
    win, session = window("processing", f)
    page = win.open("meetings", meeting=str(f))
    _pump()
    assert page.current_view() == "live"
    page.open_notes(str(f))                                              # a list row does the same
    assert page.current_view() == "live"


def test_a_recording_in_progress_is_never_taken_for_a_meeting_to_read(window, meetings_root):
    f = make_meeting(meetings_root, notes="")
    win, session = window("recording", f)
    page = win.open("meetings", meeting=str(f), writing=True)
    _pump()
    assert page.current_view() == "live"


def test_a_meeting_that_is_not_being_written_opens_as_ever(window, meetings_root):
    f = make_meeting(meetings_root)
    win, session = window("idle", None)
    page = win.open("meetings", meeting=str(f), writing=True)
    _pump()
    assert page.current_view() == "notes" and page.notes_view.folder == str(f)


def test_the_notes_arriving_do_not_move_somebody_reading_the_transcript(window, meetings_root):
    f = make_meeting(meetings_root, notes="")
    win, session = window("processing", f)
    page = win.open("meetings", meeting=str(f), writing=True)
    _pump()
    nv = page.notes_view
    nv.stack_index(1)                                                    # the reader opened the Transcript
    (f / "notes.md").write_text("## Summary\nA hiring call.\n", encoding="utf-8")
    session.phase = "done"
    session.finished.emit(str(f), "## Summary\nA hiring call.\n", "")
    _pump()
    assert nv.stack.currentIndex() == 1 and nv.tabs.index() == 1          # still there
    assert "A hiring call." in nv.data["notes"]
    assert page._writing_folder == ""


def test_a_meeting_opened_the_ordinary_way_still_starts_on_the_notes_after_a_rewrite(window, meetings_root):
    f = make_meeting(meetings_root)
    win, session = window("idle", None)
    page = win.open("meetings", meeting=str(f))
    _pump()
    nv = page.notes_view
    nv.stack_index(1)
    session.finished.emit(str(f), "## Summary\nNew.\n", "")
    _pump()
    assert nv.stack.currentIndex() == 0                                    # (unchanged: a plain reload)


def test_the_saved_transcript_makes_the_open_meeting_read_again_and_nothing_else(window, meetings_root):
    f = make_meeting(meetings_root, notes="")
    g = make_meeting(meetings_root, "20261029_100000", "Other")
    win, session = window("processing", f)
    page = win.open("meetings", meeting=str(f), writing=True)
    _pump()
    nv = page.notes_view
    nv.stack_index(1)
    loads = []
    real = nv.reload
    nv.reload = lambda keep_view=False: loads.append(keep_view) or real(keep_view=keep_view)
    session.transcript_saved.emit(str(g))                                  # another meeting
    session.transcript_saved.emit("")
    assert loads == []
    session.transcript_saved.emit(str(f))
    _pump()
    assert loads == [True]                                                 # read again, the reader keeps the place
    assert nv.stack.currentIndex() == 1
    page.show_list()
    session.transcript_saved.emit(str(f))                                  # the list is showing: nothing to do
    assert loads == [True]


def test_opening_another_meeting_forgets_the_one_that_was_being_written(window, meetings_root):
    f = make_meeting(meetings_root, notes="")
    g = make_meeting(meetings_root, "20261029_100000", "Other")
    win, session = window("processing", f)
    page = win.open("meetings", meeting=str(f), writing=True)
    _pump()
    assert page._writing_folder == str(f)
    page.open_notes(str(g))
    _pump()
    assert page._writing_folder == ""
    assert page.notes_view.folder == str(g)
