"""The window must never freeze at the end of (or after) a long session.

What froze it (found with a 45-minute Live Assistant meeting on the founder's
computer, Windows logged "Gusan stopped interacting with Windows"): the speaker
pass. sherpa-onnx's process() keeps Python's interpreter lock for its whole run
- minutes for a long meeting - so every thread, the GUI thread included, stood
still. It now runs in its own process (diarization_proc.py).

These tests drive the real UI offscreen with a GUI heartbeat (a 50 ms timer on
the GUI thread recording its longest gap) and fail when the window would have
frozen. Budgets are scaled by a calibration of this machine's speed, so a slow
CI runner does not make them flaky; a real freeze is seconds to minutes.
"""
import json
import os
import struct
import sys
import threading
import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from tests_qt.conftest import make_stub_app

BASE_BUDGET_S = 1.0          # the longest gap the heartbeat may show on a normal computer


def _real_numpy():
    try:
        import numpy as np
        return float(np.asarray([1.0, 2.0, 3.0]).mean()) == 2.0
    except Exception:
        return False


needs_numpy = pytest.mark.skipif(not _real_numpy(), reason="real numpy needed")


def _scale():
    """How much slower than the reference this computer is right now (>= 1, <= 6)."""
    t0 = time.perf_counter()
    total = 0
    for i in range(1_500_000):
        total += i * i
    took = time.perf_counter() - t0
    return max(1.0, min(6.0, took / 0.12))


class Heartbeat:
    """A 50 ms QTimer on the GUI thread; ``worst`` is the longest the thread was away."""

    def __init__(self):
        self.worst = 0.0
        self._last = time.perf_counter()
        self.timer = QTimer()
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)

    def _tick(self):
        now = time.perf_counter()
        self.worst = max(self.worst, now - self._last - 0.05)
        self._last = now

    def __enter__(self):
        self._last = time.perf_counter()
        self.timer.start()
        return self

    def __exit__(self, *a):
        self.timer.stop()

    def reset(self):
        self.worst = 0.0
        self._last = time.perf_counter()


def pump(seconds=0.0, until=None, limit=60.0):
    end = time.perf_counter() + (limit if until else seconds)
    while time.perf_counter() < end:
        QApplication.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.004)
    QApplication.processEvents()
    return until() if until else True


# ── the speaker pass: the culprit ────────────────────────────────────────────

@needs_numpy
def test_the_speaker_pass_does_not_freeze_the_window(qapp, tmp_path, monkeypatch):
    """A call that holds the interpreter lock for 2.5 s (as sherpa's process()
    does for the whole run) in the child process leaves the GUI heartbeat alone."""
    import numpy as np
    import diarization
    from tests.test_diarization_proc import FAKE_SHERPA

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "sherpa_onnx.py").write_text(FAKE_SHERPA, encoding="utf-8")
    models = tmp_path / "appdata" / "models" / "diarization"
    (models / "sherpa-onnx-pyannote-segmentation-3-0").mkdir(parents=True)
    (models / "embedding.onnx").write_bytes(b"x")
    (models / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx").write_bytes(b"x")
    monkeypatch.setenv("TRANSCRIBE_APP_DATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("PYTHONPATH", str(stubs) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setenv("FAKE_SHERPA_HOLD_S", "2.5")
    monkeypatch.delenv("GUSAN_DIARIZE_IN_PROCESS", raising=False)
    monkeypatch.setattr(diarization, "is_available", lambda: True)

    box = []
    worker = threading.Thread(target=lambda: box.append(diarization.diarize(np.zeros(16000 * 5, np.float32))),
                              daemon=True)
    with Heartbeat() as hb:
        t0 = time.perf_counter()
        worker.start()
        assert pump(until=lambda: bool(box), limit=90)
        took = time.perf_counter() - t0
    assert box[0] == [(0.0, 1.0, 0), (1.0, 2.0, 1)]
    assert took >= 2.4                                  # the child really spent the 2.5 s
    assert hb.worst < BASE_BUDGET_S * _scale(), "the window froze for %.2f s" % hb.worst


# ── a 45-minute meeting, opened in the Meetings page ─────────────────────────

def _fake_45_minute_wav(path, minutes=45):
    """A real WAV header for ``minutes`` of 16 kHz mono audio; the samples are
    never written (the file is extended: silence). The player only reads its
    header and a few short windows."""
    frames = int(minutes * 60 * 16000)
    data_bytes = frames * 2
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16))
        f.write(b"data" + struct.pack("<I", data_bytes))
        f.seek(44 + data_bytes - 1)
        f.write(b"\0")


def _words(n, speakers):
    import random
    rng = random.Random(1)
    vocab = ("the team we will ship next week after review of plan budget design launch customer "
             "feedback roadmap quarter owner decision risk follow up schedule update").split()
    lines, count, turn = [], 0, 0
    while count < n:
        k = rng.randint(30, 110)
        text = " ".join(rng.choice(vocab) for _ in range(k)) + "."
        count += k
        lines.append(("Speaker %d: %s" % (1 + turn % 3, text)) if speakers else text)
        turn += 1
    return lines


def _write_meeting(root, name, speakers=True, one_line=False, notes=True, wav=True, minutes=45, words=8000):
    d = root / name
    d.mkdir()
    started = "%s-%s-%s %s:%s:00" % (name[:4], name[4:6], name[6:8], name[9:11], name[11:13])
    (d / "meta.json").write_text(json.dumps({
        "title": "Live session 10:31", "attendees": "Anna, David", "attendees_list": ["Anna", "David"],
        "duration_sec": minutes * 60, "started": started, "source": "live_assist", "language": "en",
        "seen": False, "notes_engine_label": "Gusan Cloud · Mistral Small"}), encoding="utf-8")
    lines = _words(words, speakers)
    (d / "transcript.txt").write_text((" " if one_line else "\n").join(lines), encoding="utf-8")
    with open(d / "chunks.jsonl", "w", encoding="utf-8") as f:
        flat = " ".join(lines).split()
        for i in range(600):
            f.write(json.dumps({"index": i, "text": " ".join(flat[i * 13:(i + 1) * 13]) or "ok",
                                "language": "en", "timestamp": started}) + "\n")
    if notes:
        (d / "notes.md").write_text("## Summary\n- We ship Friday.\n\n## Action items\n- [ ] Do it (Owner: Aram)\n",
                                    encoding="utf-8")
    (d / "my_notes.md").write_text("\n".join("- a note about the plan %d" % i for i in range(60)), encoding="utf-8")
    if wav:
        _fake_45_minute_wav(str(d / "audio_part1.wav"), minutes)
    return d


@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


@pytest.fixture
def window(qapp, monkeypatch):
    from ui import shell as shell_mod
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    made = []

    def build():
        app = make_stub_app("pro")
        win = shell_mod.MainWindow(app)
        made.append(win)
        return win, app
    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def test_opening_a_45_minute_meeting_keeps_the_window_alive(qapp, meetings_root, window):
    """Notes, Transcript and My notes tabs, the People card and the audio player
    (45 min of recording) - read on workers, drawn in slices."""
    for i in range(30):                                  # a library the size of a real user's
        _write_meeting(meetings_root, "202610%02d_%02d0000" % (1 + i // 3, 8 + i % 3),
                       wav=False, minutes=10, words=1500)
    folder = _write_meeting(meetings_root, "20261021_103134")
    win, _app = window()
    win.show()
    page = win.open("meetings")
    pump(1.0)
    budget = BASE_BUDGET_S * _scale()
    with Heartbeat() as hb:
        page.open_notes(str(folder))
        assert pump(until=lambda: page.notes_view.audio_player.duration_ms > 0, limit=30)
        for tab in (1, 2, 0):                            # Transcript, My notes, Notes again
            page.notes_view.stack_index(tab)
            pump(0.6)
        pump(0.5)
    view = page.notes_view
    assert view.title_label.text() == "Live session 10:31"
    assert "Speaker 1" in view.transcript.toPlainText()
    assert len(view.my_notes.toPlainText()) > 500
    assert abs(view.audio_player.duration_ms - 45 * 60 * 1000) < 2000
    assert hb.worst < budget, "the window froze for %.2f s opening the meeting" % hb.worst


def test_a_recovered_one_line_transcript_without_notes_does_not_freeze_it(qapp, meetings_root, window):
    """What the founder's recovered meeting looked like: a plain 8000-word line
    with no speaker labels and no notes (the transcript is shown as paragraphs)."""
    folder = _write_meeting(meetings_root, "20261021_093000", speakers=False, one_line=True, notes=False, wav=False)
    win, _app = window()
    win.show()
    page = win.open("meetings")
    pump(1.0)
    with Heartbeat() as hb:
        page.open_notes(str(folder))
        assert pump(until=lambda: bool(page.notes_view.data.get("transcript")), limit=30)
        pump(1.0)
        page.notes_view.stack_index(1)
        pump(0.8)
    assert hb.worst < BASE_BUDGET_S * _scale(), "froze for %.2f s" % hb.worst
    assert len(page.notes_view.transcript.toPlainText().split()) > 7000


# ── Stop -> processing -> finished, 45 minutes of transcript, every page listening ──

class _Rec:
    """The AudioRecorder surface a meeting touches; hands back a long transcript."""

    def __init__(self, words, segments):
        self.words, self.segments = words, segments
        self._full_audio_truncated = False
        self._full_audio_max = 16000 * 60 * 120
        self._loopback_peak = 1.0
        self.on_chunk_complete = self.on_levels = self.on_partial = self.on_lang_detected = None
        self.on_source_levels = None
        self.fast_live_chunks = True
        self.partial_text = ""
        self.paused = False

    def start_recording(self, **kw):
        pass

    def stop_recording(self):
        pass

    def request_abort(self):
        pass

    def transcribe(self):
        return " ".join(self.words), "en"

    def get_full_audio(self):
        import numpy as np
        return np.zeros(16000 * 20, dtype=np.float32)

    def transcribe_segments(self, audio, language=None, **kw):
        return [dict(s) for s in self.segments]

    def set_paused(self, on):
        self.paused = bool(on)

    def chunk_source_energy(self, idx):
        return (1.0, 0.1) if idx % 3 else (0.1, 1.0)


@needs_numpy
def test_stop_to_finished_of_a_45_minute_live_session_keeps_the_window_alive(qapp, meetings_root, window, monkeypatch):
    """The real MeetingSession + the Meetings page + the Live Assistant card,
    the recorder handing back 8000 words in 1300 segments and 1400 speaker turns:
    Stop -> processing -> finished -> the notes opening. The history entry is
    written by the worker, before ``finished`` - never in the GUI-thread slot."""
    from unittest import mock
    import actions
    import diarization
    import meeting_session
    from ui import glass, live_assist

    words = " ".join(_words(8000, False)).split()
    segs = [{"start": k * 2.0, "end": k * 2.0 + 1.8, "text": " ".join(words[k * 6:(k + 1) * 6])}
            for k in range(len(words) // 6)]
    turns = [(k * 2700 / 1400.0, (k + 1) * 2700 / 1400.0, k % 3) for k in range(1400)]

    win, app = window()
    app.cfg.update(meeting_consent_ack=True, save_history=True, meeting_audio_mode="smart_meeting",
                   action_model=actions.API_MANAGED_ID)
    app.recorder = _Rec(words, segs)
    app.save_config = lambda: None
    app.release_models_after_session = lambda: None
    app.prewarm_speech_model = lambda: None
    app._resolve_action_engine = lambda action_model=None: (
        action_model or app.cfg["action_model"], {**app.cfg, "_managed_token": "tok"})

    saved = []

    def fake_save_entry(text, language, backend, audio_dir=None, **extra):
        saved.append((threading.current_thread() is threading.main_thread(), len(text), time.perf_counter()))

    with mock.patch.object(meeting_session.telemetry, "track", lambda *a, **k: None), \
            mock.patch.object(meeting_session.actions, "process",
                              lambda text, mode, **kw: "## Summary\n- We ship Friday.\n\n## Action items\n- [ ] Do it (Owner: Aram)"), \
            mock.patch.object(diarization, "sherpa_installed", lambda: True), \
            mock.patch.object(diarization, "models_downloaded", lambda: True), \
            mock.patch.object(diarization, "is_available", lambda: True), \
            mock.patch.object(diarization, "diarize", lambda audio, *a, **k: list(turns)), \
            mock.patch("history.save_entry", fake_save_entry), \
            mock.patch.object(glass, "capture_exclusion_supported", return_value=True), \
            mock.patch.object(glass, "is_remote_session", return_value=False), \
            mock.patch.object(glass, "exclude_from_capture", return_value=True), \
            mock.patch.object(glass, "is_excluded_from_capture", return_value=True), \
            mock.patch.object(glass, "macos_26_or_later", return_value=False):
        session = meeting_session.MeetingSession(app)
        app.meeting_session = session
        card = live_assist.LiveAssistOverlay(main_app=app)
        app.live_assist = card
        session.sig_chunk.connect(card.feed_transcript)
        win.show()
        page = win.open("meetings")
        pump(0.5)
        finished = []
        session.finished.connect(lambda f, n, e: finished.append((time.perf_counter(), f, n, e)))
        assert session.start(title="Live session 10:31", audio_mode="smart_meeting", source="live_assist")[0]
        budget = BASE_BUDGET_S * _scale()
        with Heartbeat() as hb:
            rec = app.recorder
            for i in range(150):                         # live pieces (the live view keeps its last 400)
                rec.on_chunk_complete(i, " ".join(words[i * 13:(i + 1) * 13]) or "ok", "en")
                if i % 10 == 0:
                    pump(0.01)
            pump(0.5)
            assert session.stop("typed during the meeting")
            assert pump(until=lambda: bool(finished), limit=120)
            pump(1.0)
        folder = finished[0][1]
        assert finished[0][3] == "" and "We ship Friday" in finished[0][2]
        assert open(os.path.join(folder, "transcript.txt"), encoding="utf-8").read().startswith("Speaker ")
        # the history entry: written once, on the worker, before `finished`
        assert len(saved) == 1
        assert saved[0][0] is False, "the history file was rewritten on the GUI thread"
        assert saved[0][2] <= finished[0][0]
        assert hb.worst < budget * 2, "the window froze for %.2f s from Start to the notes" % hb.worst
        session.deleteLater()
        card.hide()
        card.deleteLater()


# ── a long library is drawn in slices ────────────────────────────────────────

def test_a_long_library_is_drawn_in_slices_not_in_one_freeze(qapp, meetings_root, window):
    """300 meetings built all at once froze the window for seconds at every
    refresh (after each meeting, rename, delete). The first rows come at once,
    the rest in short slices - same rows, same order."""
    import meeting_store
    for i in range(80):
        _write_meeting(meetings_root, "20260%d%02d_%02d%02d00" % (1 + i // 28, 1 + i % 28, 8 + i % 10, i % 60),
                       wav=False, minutes=10, words=300)
    win, _app = window()
    win.show()
    page = win.open("meetings")
    pump(1.0)
    items = meeting_store.list_meetings()
    assert len(items) == 80
    ml = page.list_view.meetings
    ml._row_timer.setInterval(0)
    with Heartbeat() as hb:
        ml.set_items(items)
        sync_rows = len(ml.rows)
        assert ml.FIRST_ROWS <= sync_rows < 80, "the first rows come at once, not all 80 (got %d)" % sync_rows
        assert pump(until=lambda: len(ml.rows) == 80, limit=60)
        pump(0.3)
    assert [r.meeting["dir"] for r in ml.rows] == [m["dir"] for m in items]
    assert ml._row_state is None
    assert hb.worst < BASE_BUDGET_S * _scale(), "the window froze for %.2f s drawing the list" % hb.worst


def test_a_newer_list_replaces_one_still_being_drawn(qapp, meetings_root, window):
    import meeting_store
    for i in range(60):
        _write_meeting(meetings_root, "20260%d%02d_%02d%02d00" % (1 + i // 28, 1 + i % 28, 8 + i % 10, i % 60),
                       wav=False, minutes=10, words=300)
    win, _app = window()
    win.show()
    page = win.open("meetings")
    pump(1.0)
    items = meeting_store.list_meetings()
    ml = page.list_view.meetings
    ml.set_items(items)
    assert len(ml.rows) < 60
    ml.set_items(items[:5])                          # e.g. a search, before the first list finished
    pump(1.0)
    assert [r.meeting["dir"] for r in ml.rows] == [m["dir"] for m in items[:5]]
    ml.set_items(items)
    pump(until=lambda: len(ml.rows) == 60, limit=60)
    assert len(ml.rows) == 60 and len({r.meeting["dir"] for r in ml.rows}) == 60


# ── the field watchdog, on a real event loop ─────────────────────────────────

def test_the_watchdog_names_the_function_that_blocks_the_event_loop(qapp):
    """A slot that blocks the GUI thread: the log gets ONE entry saying where
    the GUI thread is stuck, then the length once it answers again."""
    import logging
    import stall_watchdog

    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    lg = logging.getLogger("wd.qt.integration")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    lg.handlers[:] = [H()]
    dog = stall_watchdog.StallWatchdog(lg, threshold_s=0.4, relog_s=5.0, poll_s=0.05,
                                       gui_thread_id=threading.get_ident(), fault_stream=None)
    beat = QTimer()
    beat.setInterval(50)
    beat.timeout.connect(dog.beat)

    def _a_slot_that_blocks_the_window():
        time.sleep(1.5)

    dog.start()
    beat.start()
    try:
        pump(0.3)
        QTimer.singleShot(0, _a_slot_that_blocks_the_window)
        assert pump(until=lambda: any("answering again" in m for m in records), limit=20)
    finally:
        beat.stop()
        dog.stop()
    stalls = [m for m in records if "not responding" in m]
    assert len(stalls) == 1, records
    assert "_a_slot_that_blocks_the_window" in stalls[0]
    again = [m for m in records if "answering again" in m]
    assert len(again) == 1
    import re
    assert re.search(r"after [12]\.\d s", again[0]), again[0]
