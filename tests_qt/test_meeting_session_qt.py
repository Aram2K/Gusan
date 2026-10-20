"""MeetingSession (meeting_session) on a real Qt event loop, and the Meetings
page following it (offscreen).

The engine's logic is covered Qt-free in tests/test_meeting_session.py and
tests/test_meeting_library.py; here: Qt signals cross from the recorder /
worker threads to the GUI thread, the timers (recap only while recording,
My notes debounce), and the Meetings page following a session started by
anyone."""
import json
import threading
import time

import pytest
from PySide6.QtCore import QObject, QThread
from PySide6.QtWidgets import QApplication

from tests_qt.conftest import make_stub_app


def _wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    QApplication.processEvents()
    return cond()


class _Rec:
    """The AudioRecorder surface a meeting touches."""

    def __init__(self, result=("Hello team. We ship Friday.", "en")):
        self.result = result
        self._full_audio_truncated = False
        self._full_audio_max = 16000 * 60 * 120
        self._loopback_peak = 1.0
        self.on_chunk_complete = self.on_levels = None
        self.on_partial = self.on_lang_detected = None
        self.on_source_levels = None
        self.fast_live_chunks = False
        self.partial_text = ""
        self.paused = False
        self.started = None
        self.stops = 0

    def start_recording(self, **kw):
        self.started = kw

    def stop_recording(self):
        self.stops += 1

    def transcribe(self):
        return self.result

    def get_full_audio(self):
        return None

    def set_paused(self, on):
        self.paused = bool(on)

    def chunk_source_energy(self, idx):
        return None


def _app():
    import actions
    app = make_stub_app("pro")
    app.cfg.update(meeting_consent_ack=True, save_history=False,
                   meeting_audio_mode="default_mic", action_model=actions.API_MANAGED_ID)
    app.recorder = _Rec()
    app.live_assist = None
    app.save_config = lambda: app.calls.append(("save_config",))
    app.release_models_after_session = lambda: app.calls.append(("release",))
    app._resolve_action_engine = lambda action_model=None: (
        action_model or app.cfg["action_model"], {**app.cfg, "_managed_token": "tok"})
    return app


@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import meeting_session
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for",
                        lambda name: root if name == "meetings" else real(name))
    monkeypatch.setattr(meeting_session.telemetry, "track", lambda *a, **k: None)
    monkeypatch.setattr(meeting_session.actions, "process",
                        lambda text, mode, **kw: "## Summary\n- We ship Friday.")
    return root


class _Sink(QObject):
    """A page-like receiver living on the GUI thread (a QObject slot, so a
    signal emitted on another thread is queued to this one)."""

    def __init__(self, name, log):
        super().__init__()
        self.name, self.log = name, log

    def take(self, *args):
        gui = QApplication.instance().thread()
        self.log.append((self.name, QThread.currentThread() is gui) + args)


def _log(session, names=("state_changed", "chunk_text", "live_line", "recap", "status",
                         "levels", "finished", "error")):
    log = []
    session._test_sinks = []
    for name in names:
        sink = _Sink(name, log)
        session._test_sinks.append(sink)
        getattr(session, name).connect(sink.take)
    return log


def test_a_meeting_from_start_to_notes_on_the_gui_thread(qapp, meetings_root):
    import meeting_session
    import meeting_store
    app = _app()
    s = meeting_session.MeetingSession(app)
    log = _log(s)
    ok, code, msg = s.start(title="Weekly sync", attendees="Anna, David", source="page")
    assert (ok, code, msg) == (True, "", "")
    assert s.state == s.STATE_RECORDING and s.phase == "recording"
    assert s._recap_timer.isActive()                     # recap only while recording
    folder = s.meeting_dir
    assert folder.parent == meetings_root
    assert meeting_store.load_meta(folder)["source"] == "page"

    # The recorder calls back from its own threads: everything reaches the
    # GUI thread as signals.
    rec = app.recorder
    workers = [threading.Thread(target=rec.on_chunk_complete, args=(0, "Hello team.", "en")),
               threading.Thread(target=rec.on_source_levels, args=(0.1, 0.0))]
    for t in workers:
        t.start()
        t.join(5)
    assert _wait_for(lambda: s.live_sentences() == ["Hello team."])
    assert _wait_for(lambda: any(e[0] == "levels" for e in log))
    assert ("chunk_text", True, "Hello team.") in log
    level = next(e for e in log if e[0] == "levels")
    assert level[1] is True and level[2] > 0.5 and level[3] == 0.0

    assert s.pause() and s.phase == "paused" and rec.paused is True
    assert s.state == s.STATE_RECORDING and s.is_busy          # dictation stays locked out
    assert s.resume() and rec.paused is False

    assert s.stop("Ask Anna about the budget")
    assert not s._recap_timer.isActive()
    assert _wait_for(lambda: any(e[0] == "finished" for e in log), timeout=10)
    assert all(e[1] for e in log), "every signal is delivered on the GUI thread"
    fin = next(e for e in log if e[0] == "finished")
    assert fin[2:] == (str(folder), "## Summary\n- We ship Friday.", "")
    phases = [e[2] for e in log if e[0] == "state_changed"]
    assert phases == ["recording", "paused", "recording", "processing", "done"]
    assert any(e[0] == "status" and e[2] == "Writing notes…" for e in log)
    assert meeting_store.load_my_notes(folder) == "Ask Anna about the budget"
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["title"] == "Weekly sync" and meta["attendees_list"] == ["Anna", "David"]
    assert meta["notes_engine_label"] == "Gusan Cloud · Mistral Small"
    assert meta["seen"] is False and meta["language"] == "en"
    assert ("release",) in app.calls
    # The recorder's dictation callbacks are back.
    assert rec.on_source_levels is None
    s.deleteLater()


def test_my_notes_are_saved_shortly_after_typing(qapp, meetings_root):
    import meeting_session
    import meeting_store
    s = meeting_session.MeetingSession(_app())
    s._notes_timer.setInterval(30)
    assert s.start(title="Notes")[0]
    s.update_my_notes("- first point")
    assert s._notes_timer.isActive()
    assert _wait_for(lambda: meeting_store.load_my_notes(s.meeting_dir) == "- first point")
    # Abort stops the timers and leaves a NEW meeting out of the list.
    assert s.abort() and s.phase == "idle"
    assert not s._recap_timer.isActive() and not s._notes_timer.isActive()
    assert meeting_store.list_meetings() == []
    s.deleteLater()


def test_refusals_are_returned_and_emitted(qapp, meetings_root):
    import meeting_session
    app = _app()
    app._file_job_running = True
    s = meeting_session.MeetingSession(app)
    errors = []
    s.error.connect(lambda code, msg: errors.append((code, msg)))
    ok, code, msg = s.start()
    assert (ok, code) == (False, meeting_session.ERR_FILE_JOB)
    assert "Audio to text" in msg and errors == [(code, msg)]
    assert s.phase == "idle" and not s._recap_timer.isActive()
    s.deleteLater()


def test_get_session_makes_one_per_app(qapp):
    import meeting_session
    app = _app()
    s = meeting_session.get_session(app)
    assert isinstance(s, meeting_session.MeetingSession)
    assert meeting_session.get_session(app) is s is app.meeting_session


# The 1.x Record Meeting window retired in Gusan 2.0. Its three tests below
# keep their names and now check the same behaviour where it lives: the
# Meetings page (ui/pages/meetings.py) following a session started anywhere,
# and the session's own empty-meeting / Retry handling.
@pytest.fixture
def meetings_window(qapp, monkeypatch):
    from ui import shell as shell_mod
    from ui.pages import meeting_views
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)
    made = []

    def build(app):
        win = shell_mod.MainWindow(app)
        made.append(win)
        return win
    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def test_classic_window_follows_the_session(qapp, meetings_root, meetings_window):
    import meeting_session
    import meeting_store
    app = _app()
    s = meeting_session.get_session(app)
    win = meetings_window(app)
    page = win.open("meetings")
    assert page.current_view() == "list" and s.phase == "idle"

    # Started elsewhere (Live Assistant, the calendar): the page shows the recording.
    assert s.start(title="From elsewhere", source="live_assist")[0]
    assert _wait_for(lambda: page.current_view() == "live")
    live = page.live_view
    assert live.header.title_label.text() == "From elsewhere"
    assert live.state_label.text() == "RECORDING"
    app.recorder.on_chunk_complete(0, "First point. Second", "en")
    app.recorder.on_chunk_complete(1, "point.", "en")
    # One line per transcribed piece on the page; the session joins sentences.
    assert _wait_for(lambda: [t for _w, t in live.transcript.lines()] == ["First point. Second", "point."])
    assert s.live_sentences() == ["First point.", "Second point."]
    s.pause()
    assert _wait_for(lambda: live.state_label.text() == "PAUSED")
    s.resume()
    # Typed notes reach the session as they are typed; Stop keeps them.
    live.my_notes.setPlainText("Typed on the page")
    assert s.my_notes == "Typed on the page"

    live.stop_btn.click()
    assert s.state == s.STATE_PROCESSING
    assert _wait_for(lambda: page.current_view() == "notes", timeout=10)
    nv = page.notes_view
    assert nv.data["title"] == "From elsewhere"
    assert "We ship Friday." in nv.data["notes"]
    assert "Hello team." in nv.data["transcript"]
    assert nv.data["my_notes"] == "Typed on the page"
    assert s._final_notes == "## Summary\n- We ship Friday."
    assert [m["title"] for m in meeting_store.list_meetings()] == ["From elsewhere"]


def test_an_empty_meeting_is_reported_by_whoever_started_it(qapp, meetings_root):
    import meeting_session
    app = _app()
    app.recorder.result = ("", "en")                 # nothing was said
    s = meeting_session.MeetingSession(app)
    errors = []
    s.error.connect(lambda code, msg: errors.append(code))
    # The session reports it (error "empty" - a toast on whatever page is
    # open, never a modal box) and stays "failed" until the next start.
    assert s.start(title="Silent", source="page")[0]
    s.stop("")
    assert _wait_for(lambda: errors == ["empty"], timeout=10)
    assert s.phase == "failed" and s.last_error and not s.is_busy
    assert s.start(title="Again", source="page")[0]   # the next start is a new meeting
    assert s.phase == "recording" and not s.last_error
    s.abort()
    s.deleteLater()


def test_classic_window_start_and_retry(qapp, meetings_root, monkeypatch):
    import meeting_session
    app = _app()
    s = meeting_session.MeetingSession(app)
    assert s.start(title="Board", attendees="CEO, CFO", language="en")[0]
    assert s.state == s.STATE_RECORDING and s.title == "Board"
    assert s._source == "page"
    assert app.recorder.started == {"capture_mode": "default_mic", "language": "en"}

    # The notes fail once: the transcript is kept and Retry rewrites them.
    calls = []

    def flaky(text, mode, **kw):
        calls.append(kw.get("model"))
        if len(calls) == 1:
            raise RuntimeError("quota exceeded")
        return "## Retried"

    monkeypatch.setattr(meeting_session.actions, "process", flaky)
    finished = []
    s.finished.connect(lambda folder, notes, err: finished.append((notes, err)))
    s.stop("")
    assert _wait_for(lambda: s.phase == "failed", timeout=10)
    assert "quota exceeded" in finished[-1][1] and "Hello team." in s.transcript
    assert s.retry_notes()
    assert _wait_for(lambda: s.phase == "done", timeout=10)
    assert finished[-1] == ("## Retried", "")
    assert s.notes == "## Retried"
    s.deleteLater()
