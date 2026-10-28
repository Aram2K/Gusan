"""The in-app meeting player (ui/components/audio_player.py) and its place on
the notes view (ui/pages/meeting_views.py), offscreen.

Every test but one plays through a stand-in for QMediaPlayer: nothing is ever
heard. The last test loads a tiny WAV into a real, muted QMediaPlayer without
calling play() (skipped when QtMultimedia can't start here)."""
import os
import time

import numpy as np
import pytest
from PySide6.QtCore import QObject, QPoint, Qt, QUrl, Signal
from PySide6.QtTest import QTest
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


def _local(path):
    """A path as the player's QUrl gives it back (forward slashes on Windows)."""
    return QUrl.fromLocalFile(str(path)).toLocalFile()


def _wav(path, seconds, level=0.3):
    import audio_export
    t = np.arange(int(audio_export.RATE * seconds)) / float(audio_export.RATE)
    audio_export.write_wav(path, (level * np.sin(2 * np.pi * 330.0 * t)).astype(np.float32))
    return str(path)


class FakePlayer(QObject):
    """QMediaPlayer's surface, in memory. Statuses and states are plain strings
    (the player reads enum names). ``bad`` paths fail to load."""

    mediaStatusChanged = Signal(object)
    playbackStateChanged = Signal(object)
    errorOccurred = Signal(object, str)

    def __init__(self, bad=()):
        super().__init__()
        self.bad = {os.path.normcase(str(b)) for b in bad}
        self.calls = []
        self._source = QUrl()
        self._pos = 0
        self.state = "StoppedState"
        self.status = "NoMedia"
        self.async_load = False         # LoadedMedia only on finish_load()
        self.ffmpeg_like = False        # a new source after EndOfMedia first says "LoadedMedia" (the old one)

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            self.playbackStateChanged.emit(state)

    def _set_status(self, status):
        self.status = status
        self.mediaStatusChanged.emit(status)

    def source(self):
        return self._source

    def path(self):
        return self._source.toLocalFile() if not self._source.isEmpty() else ""

    def setSource(self, url):
        self.calls.append(("setSource", url.toLocalFile() if not url.isEmpty() else ""))
        if self.ffmpeg_like and self.status == "EndOfMedia":
            self._set_status("LoadedMedia")
        self._source = QUrl(url)
        self._set_state("StoppedState")
        self._pos = 0
        if url.isEmpty():
            self._set_status("NoMedia")
            return
        self._set_status("LoadingMedia")
        if os.path.normcase(url.toLocalFile()) in self.bad:
            self.errorOccurred.emit("ResourceError", "can't open")
            self._set_status("InvalidMedia")
            return
        if not self.async_load:
            self._set_status("LoadedMedia")

    def finish_load(self):
        self._set_status("LoadedMedia")

    def play(self):
        self.calls.append(("play",))
        if self.status in ("LoadedMedia", "BufferedMedia"):
            self._set_state("PlayingState")

    def pause(self):
        self.calls.append(("pause",))
        if self.state == "PlayingState":
            self._set_state("PausedState")

    def stop(self):
        self.calls.append(("stop",))
        self._pos = 0
        self._set_state("StoppedState")

    def setPosition(self, ms):
        self.calls.append(("setPosition", int(ms)))
        self._pos = int(ms)

    def position(self):
        return self._pos

    # test controls
    def advance(self, ms):
        self._pos += int(ms)

    def end(self):
        self._set_state("StoppedState")
        self._set_status("EndOfMedia")


@pytest.fixture
def fake(monkeypatch):
    """Every AudioPlayer made in the test gets one FakePlayer (``fake.made``)."""
    from ui.components import audio_player as ap
    monkeypatch.setattr(ap, "SYNC_LOAD", True)
    holder = {"bad": (), "made": [], "raise": False}

    def make(self):
        if holder["raise"]:
            raise ImportError("No module named PySide6.QtMultimedia")
        p = FakePlayer(holder["bad"])
        holder["made"].append(p)
        return p
    monkeypatch.setattr(ap.AudioPlayer, "_make_player", make)
    return holder


@pytest.fixture
def parts(tmp_path):
    return [_wav(tmp_path / "audio_part1.wav", 2.0), _wav(tmp_path / "audio_part2.wav", 3.0)]


def _player(parts=None, hint=0):
    from ui.components import AudioPlayer
    p = AudioPlayer()
    if parts is not None:
        p.set_parts(parts, hint)
    return p


# ── the component ────────────────────────────────────────────────────────────
def test_time_labels_format(qapp):
    from ui.components.audio_player import fmt_time
    assert fmt_time(0) == "0:00"
    assert fmt_time(389736) == "6:29"
    assert fmt_time(62_000) == "1:02"
    assert fmt_time(3_723_000) == "1:02:03"
    assert fmt_time(None) == "0:00" and fmt_time(-5) == "0:00"


def test_parts_lengths_waveform_and_the_same_list_is_a_no_op(qapp, fake, parts, tmp_path):
    p = _player(parts, hint=2520)                   # meta says 42 min; the WAVs say 5 s
    assert p.durations_ms() == [2000, 3000] and p.duration_ms == 5000
    assert p.total_label.text() == "0:05" and p.time_label.text() == "0:00"
    pk = p.wave.peaks()
    assert len(pk) == p.wave.bar_count() == 34 and max(pk) == 1.0
    p.seek(1500)
    assert p.position_ms == 1500 and p.time_label.text() == "0:01"
    p.set_parts(list(parts), 9999)                  # a reload of the same meeting
    assert p.position_ms == 1500
    other = [_wav(tmp_path / "other.wav", 1.0)]
    p.set_parts(other)
    assert p.position_ms == 0 and p.duration_ms == 1000 and p.total_label.text() == "0:01"
    p.set_parts([])
    assert p.duration_ms == 0 and p.parts() == []


def test_lengths_are_read_off_the_gui_thread(qapp, monkeypatch, parts):
    from ui.components import audio_player as ap
    monkeypatch.setattr(ap, "SYNC_LOAD", False)
    import audio_export
    real = audio_export.part_durations_ms
    seen = []

    def slow(paths):
        import threading
        seen.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.05)
        return real(paths)
    monkeypatch.setattr(audio_export, "part_durations_ms", slow)
    p = _player(parts, hint=389)
    assert p.total_label.text() == "6:29"           # the hint, until the WAVs are read
    assert _wait_for(lambda: p.duration_ms == 5000)
    assert seen == [False] and p.total_label.text() == "0:05"
    # A late result for parts that changed meanwhile is dropped.
    p.set_parts(parts[:1])
    p._on_loaded(p._gen - 1, ([9000, 9000], []))
    assert _wait_for(lambda: p.duration_ms == 2000)


def test_play_pressed_while_the_parts_are_read_starts_once_read(qapp, fake, parts, monkeypatch):
    from ui.components import audio_player as ap
    monkeypatch.setattr(ap, "SYNC_LOAD", False)
    p = _player(parts)
    p.play()                                        # the lengths aren't known yet
    assert p.is_playing and p.play_btn.property("loading") is True
    assert _wait_for(lambda: fake["made"] and fake["made"][0].state == "PlayingState")
    assert p.duration_ms == 5000 and p.play_btn.property("loading") is False


def test_play_pause_icon_names_and_the_first_play_looks_pressed(qapp, fake, parts, monkeypatch):
    from ui.components import audio_player as ap
    p = _player(parts)
    looks = []
    make = ap.AudioPlayer._make_player

    def watched(self):
        looks.append((self.play_btn.property("loading"), self.play_btn.accessibleName()))
        return make(self)
    monkeypatch.setattr(ap.AudioPlayer, "_make_player", watched)
    assert p.play_btn.accessibleName() == p.play_btn.toolTip() == "Play recording"
    assert p.play_btn.property("gusan") == "playBtn"
    p.play_btn.click()
    assert looks == [(True, "Pause recording")]     # pressed at once, before the backend starts
    player = fake["made"][0]
    assert player.path() == _local(parts[0])
    assert player.state == "PlayingState" and p.is_playing
    assert p.play_btn.accessibleName() == p.play_btn.toolTip() == "Pause recording"
    assert p.play_btn.property("loading") is False
    player.advance(1200)
    p._tick()
    assert p.position_ms == 1200 and p.time_label.text() == "0:01"
    p.play_btn.click()
    assert player.state == "PausedState" and not p.is_playing
    assert p.play_btn.accessibleName() == "Play recording"
    p.play_btn.click()                              # resumes on the same part, no reload
    assert player.state == "PlayingState"
    assert [c for c in player.calls if c[0] == "setSource"] == [("setSource", _local(parts[0]))]
    assert len(fake["made"]) == 1 and looks == [(True, "Pause recording")]


def test_click_drag_and_keys_seek_on_the_waveform(qapp, fake, parts):
    p = _player(parts)
    w = p.wave
    assert w.accessibleName() == "Recording position"
    assert w.accessibleDescription() == "0:00 of 0:05"
    QTest.mouseClick(w, Qt.LeftButton, Qt.NoModifier, QPoint(w.PAD + w.bars_width() // 2, 10))
    assert abs(p.position_ms - 2500) <= 40
    assert w.accessibleDescription() == "0:02 of 0:05"
    assert w.played_bars() == 17
    QTest.mousePress(w, Qt.LeftButton, Qt.NoModifier, QPoint(w.PAD, 10))
    QTest.mouseMove(w, QPoint(w.PAD + w.bars_width(), 10))
    QTest.mouseRelease(w, Qt.LeftButton, Qt.NoModifier, QPoint(w.PAD + w.bars_width(), 10))
    assert p.position_ms == 5000                    # dragged to the end
    w.setFocus()
    QTest.keyClick(w, Qt.Key_Home)
    assert p.position_ms == 0 and w.played_bars() == 0
    QTest.keyClick(w, Qt.Key_Right)
    assert p.position_ms == 5000                    # +5 s, capped at the end
    QTest.keyClick(w, Qt.Key_Left)
    assert p.position_ms == 0
    QTest.keyClick(w, Qt.Key_End)
    assert p.position_ms == 5000 and w.played_bars() == 34
    QTest.keyClick(w, Qt.Key_Home)
    QTest.keyClick(w, Qt.Key_Space)
    assert p.is_playing and fake["made"][0].state == "PlayingState"
    QTest.keyClick(w, Qt.Key_Return)
    assert not p.is_playing


def test_the_played_part_is_painted_blue(qapp, fake, parts):
    from ui import theme
    p = _player(parts)
    w = p.wave
    w.resize(w.size())
    p.seek(2500)
    img = w.grab().toImage()
    mid = w.PAD + w.HEIGHT // 2
    first = img.pixelColor(w.PAD + 1, mid)
    last = img.pixelColor(w.PAD + (w.bar_count() - 1) * w.PITCH + 1, mid)
    blue, grey = theme.qcolor("blue-l"), theme.qcolor("line2")
    assert (first.red(), first.green(), first.blue()) == (blue.red(), blue.green(), blue.blue())
    assert (last.red(), last.green(), last.blue()) == (grey.red(), grey.green(), grey.blue())
    w.setProperty("focusVisible", True)              # the keyboard focus ring
    ring = w.grab().toImage().pixelColor(1, w.height() // 2)
    assert ring.alpha() > 0 and ring.blue() > ring.red()


def test_two_parts_play_as_one_recording(qapp, fake, parts):
    p = _player(parts)
    p.play()
    player = fake["made"][0]
    one, two = (_local(x) for x in parts)
    assert player.path() == one
    p.seek(3000)                                    # 1 s into the second part
    assert player.path() == two and player.position() == 1000 and player.state == "PlayingState"
    player.advance(500)
    assert p.position_ms == 3500
    p.seek(1000)                                    # back across the boundary
    assert player.path() == one and player.position() == 1000
    player.end()                                    # part 1 ends: part 2 follows, still playing
    QApplication.processEvents()
    assert player.path() == two and player.position() == 0 and player.state == "PlayingState"
    assert p.position_ms == 2000 and p.play_btn.accessibleName() == "Pause recording"
    player.end()                                    # the end: back to 0:00, the file let go
    QApplication.processEvents()
    assert not p.is_playing and p.position_ms == 0 and player.source().isEmpty()
    assert p.play_btn.accessibleName() == "Play recording" and p.time_label.text() == "0:00"


def test_the_next_part_waits_for_its_own_load(qapp, fake, parts):
    # FFmpeg: a new source after EndOfMedia first reports the OLD part as
    # loaded, then loads the new one in the background.
    p = _player(parts)
    p.play()
    player = fake["made"][0]
    player.ffmpeg_like = player.async_load = True
    player.end()
    QApplication.processEvents()
    assert player.path() == _local(parts[1]) and player.state != "PlayingState"
    assert p.is_playing and p.play_btn.accessibleName() == "Pause recording"
    player.finish_load()
    assert player.state == "PlayingState" and p.position_ms == 2000
    # Seeking while a part loads lands once it has loaded.
    p.seek(500)
    assert player.path() == _local(parts[0]) and player.state != "PlayingState"
    p.seek(800)
    player.finish_load()
    assert player.position() == 800 and player.state == "PlayingState" and p.position_ms == 800


def test_stop_release_lets_go_of_the_file_and_keeps_the_place(qapp, fake, parts):
    p = _player(parts)
    p.play()
    player = fake["made"][0]
    player.advance(1500)
    p.stop(release=True)
    assert player.source().isEmpty() and not p.is_playing and p.position_ms == 1500
    p.play()                                        # loads again where it was
    assert player.path() == _local(parts[0]) and player.position() == 1500
    p.pause()
    assert not player.source().isEmpty()            # a pause keeps the file
    p.stop(release=False)
    assert not player.source().isEmpty()


def test_play_after_the_end_starts_over(qapp, fake, parts):
    p = _player(parts[:1])
    p.play()
    player = fake["made"][0]
    p.pause()
    QTest.keyClick(p.wave, Qt.Key_End)
    assert p.position_ms == 2000 and player.position() == 2000
    p.play()
    assert player.position() == 0 and player.state == "PlayingState" and p.position_ms == 0


def test_bad_parts_are_skipped_and_failed_only_when_nothing_plays(qapp, fake, parts, tmp_path):
    fake["bad"] = [_local(parts[0])]
    p = _player(parts)
    failed = []
    p.failed.connect(failed.append)
    p.play()
    assert _wait_for(lambda: fake["made"][0].path() == _local(parts[1]))
    assert fake["made"][0].state == "PlayingState" and failed == [] and p.is_playing
    assert p.position_ms == 2000
    # Every part broken: one message, the button back to Play.
    fake["bad"] = [_local(x) for x in parts]
    q = _player(parts)
    q.failed.connect(failed.append)
    q.play()
    assert _wait_for(lambda: failed)
    QApplication.processEvents()
    assert failed == ["Couldn't play the recording."]
    assert not q.is_playing and q.play_btn.accessibleName() == "Play recording"
    assert fake["made"][1].source().isEmpty()
    # Empty parts never reach the player at all.
    empty = _wav(tmp_path / "empty.wav", 0)
    r = _player([empty])
    r.failed.connect(failed.append)
    r.play()
    assert failed[-1] == "Couldn't play the recording." and len(failed) == 2


def test_a_part_read_while_it_was_written_is_read_again(qapp, fake, parts):
    # The view opened while "Saving the recording…": part 2 was half written.
    full = open(parts[1], "rb").read()
    with open(parts[1], "wb") as f:
        f.write(full[: 44 + 16000 * 2 // 2])        # 0.5 s of its 3 s
    p = _player(parts)
    assert p.duration_ms == 2500
    p.play()
    player = fake["made"][0]
    player.advance(600)
    with open(parts[1], "wb") as f:
        f.write(full)                               # the save finished
    st = os.stat(parts[1])
    os.utime(parts[1], ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    p.set_parts(list(parts))                        # the reload after the save
    assert p.duration_ms == 5000 and p.total_label.text() == "0:05"
    assert p.is_playing and player.state == "PlayingState" and p.position_ms == 600
    assert [c for c in player.calls if c[0] == "setSource"] == [("setSource", _local(parts[0]))]


def test_paused_at_a_parts_end_goes_on_from_the_next_part(qapp, fake, parts):
    p = _player(parts)
    p.play()
    player = fake["made"][0]
    player.advance(2000)
    p.pause()                                       # paused as part 1 ends
    player.end()
    QApplication.processEvents()
    assert not p.is_playing and p.position_ms == 2000   # not thrown to the very end (5 s)
    p.play()
    assert player.path() == _local(parts[1]) and player.position() == 0
    assert player.state == "PlayingState" and p.position_ms == 2000


def test_a_broken_last_part_ends_like_the_end(qapp, fake, parts):
    fake["bad"] = [_local(parts[1])]
    p = _player(parts)
    failed = []
    p.failed.connect(failed.append)
    p.play()
    player = fake["made"][0]
    player.end()                                    # part 1 played; part 2 can't load
    assert _wait_for(lambda: not p.is_playing)
    QApplication.processEvents()
    assert failed == [] and p.position_ms == 0 and player.source().isEmpty()
    assert p.play_btn.accessibleName() == "Play recording"
    p.seek(3000)                                    # inside the broken part
    p.play()                                        # starts again from the good part
    assert failed == [] and player.path() == _local(parts[0])
    assert player.state == "PlayingState" and p.position_ms == 0


def test_play_again_retries_parts_that_failed(qapp, fake, parts):
    fake["bad"] = [_local(x) for x in parts]
    p = _player(parts)
    failed = []
    p.failed.connect(failed.append)
    p.play()
    assert _wait_for(lambda: failed)
    player = fake["made"][0]
    player.bad = set()                              # e.g. the file was only locked for a moment
    p.play()
    assert player.path() == _local(parts[0]) and player.state == "PlayingState"
    assert failed == ["Couldn't play the recording."]
    # A part that failed is tried again after the view lets go (leave and come back).
    fake["bad"] = [_local(parts[1])]
    q = _player(parts)
    q.play()
    second = fake["made"][1]
    second.end()
    assert _wait_for(lambda: not q.is_playing)
    second.bad = set()
    q.stop(release=True)
    q.seek(2500)
    q.play()
    assert second.path() == _local(parts[1]) and second.state == "PlayingState"


def test_quit_stops_the_sound_and_lets_go_of_the_file(qapp, fake, parts):
    p = _player(parts)
    p.play()
    player = fake["made"][0]
    player.advance(800)
    p._on_quit()                                    # QCoreApplication.aboutToQuit
    assert not p.is_playing and player.source().isEmpty() and p.position_ms == 800


def test_no_multimedia_says_so(qapp, fake, parts):
    fake["raise"] = True
    p = _player(parts)
    failed = []
    p.failed.connect(failed.append)
    p.play_btn.click()
    assert failed == ["Couldn't play the recording."]
    assert not p.is_playing and p.play_btn.property("loading") is False


# ── on the notes view ────────────────────────────────────────────────────────
@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


@pytest.fixture
def shell(qapp, monkeypatch):
    from ui import shell as shell_mod
    from ui.pages import meeting_views
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    made = []

    def build(tier="pro", **attrs):
        app = make_stub_app(tier)
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


def _with_audio(root, name="20261001_100000", seconds=(2.0,)):
    from tests_qt.test_page_meetings import _meeting
    folder = _meeting(root, name, "Q4 roadmap")       # meta duration_sec 2520 (42 min)
    for i, s in enumerate(seconds, 1):
        _wav(folder / ("audio_part%d.wav" % i), s)
    return folder


def _notes(win, folder):
    page = win.open("meetings", folder=str(folder))
    QApplication.processEvents()
    assert page.current_view() == "notes"
    return page, page.notes_view


def test_the_card_plays_inside_the_app(shell, meetings_root, fake, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url) or True))
    folder = _with_audio(meetings_root, seconds=(2.0, 1.0))
    win, _app = shell("pro")
    page, nv = _notes(win, folder)
    assert nv.audio_card.isVisibleTo(page)
    assert nv.audio_len.text() == "0:03"            # the WAVs' length, not the meta's 42 min
    assert nv.audio_start.text() == "0:00"
    assert nv.play_btn is nv.audio_player.play_btn
    nv.play_btn.click()
    player = fake["made"][0]
    assert player.state == "PlayingState" and nv.play_btn.accessibleName() == "Pause recording"
    assert opened == []                             # never the system player
    player.advance(700)
    nv.reload()                                     # e.g. back from the sidebar: keeps playing
    assert nv.audio_player.is_playing and nv.audio_player.position_ms == 700
    page.on_open()
    assert nv.audio_player.is_playing and len(fake["made"]) == 1
    nv.play()
    assert not nv.audio_player.is_playing


def test_a_meeting_without_audio_hides_the_card(shell, meetings_root, fake):
    from tests_qt.test_page_meetings import _meeting
    folder = _meeting(meetings_root, "20261001_090000", "No audio")
    win, _app = shell("pro")
    page, nv = _notes(win, folder)
    assert not nv.audio_card.isVisibleTo(page)
    assert nv.audio_player.parts() == []


def test_another_meeting_lets_go_of_the_first(shell, meetings_root, fake):
    first = _with_audio(meetings_root, "20261001_100000")
    second = _with_audio(meetings_root, "20261002_100000", seconds=(1.0,))
    win, _app = shell("pro")
    page, nv = _notes(win, first)
    nv.play_btn.click()
    player = fake["made"][0]
    page.open_notes(str(second))
    QApplication.processEvents()
    assert player.source().isEmpty() and not nv.audio_player.is_playing
    assert nv.audio_len.text() == "0:01" and nv.audio_player.position_ms == 0


def test_delete_releases_the_file_first(shell, meetings_root, fake, monkeypatch):
    import meeting_trash
    folder = _with_audio(meetings_root)
    win, _app = shell("pro")
    page, nv = _notes(win, folder)
    nv.play_btn.click()
    player = fake["made"][0]
    real = meeting_trash.move_to_trash
    at_delete = []

    def move(f):
        at_delete.append(player.source().isEmpty())
        return real(f)
    monkeypatch.setattr(meeting_trash, "move_to_trash", move)
    nv.apply_delete()
    assert at_delete == [True]                                            # the player had let go of the file
    assert not folder.exists() and page.current_view() == "list"


def test_leaving_the_page_stops_and_releases(shell, meetings_root, fake):
    folder = _with_audio(meetings_root)
    win, _app = shell("pro")
    win.show()
    page, nv = _notes(win, folder)
    QApplication.processEvents()
    nv.play_btn.click()
    player = fake["made"][0]
    assert player.state == "PlayingState"
    page.show_list()
    QApplication.processEvents()
    assert not nv.audio_player.is_playing and player.source().isEmpty()


def test_a_recording_pauses_the_playback(shell, meetings_root, fake):
    from tests_qt.test_page_meetings import FakeSession
    folder = _with_audio(meetings_root)
    session = FakeSession()
    win, _app = shell("pro", meeting_session=session)
    page, nv = _notes(win, folder)
    nv.play_btn.click()
    assert nv.audio_player.is_playing
    session._set("recording")                       # from the tray, the calendar, the Live Assistant...
    assert not nv.audio_player.is_playing
    assert fake["made"][0].state == "PausedState"
    # Continue recording pauses it before the recorder starts.
    session.phase = "idle"
    page, nv = _notes(win, folder)
    nv.play_btn.click()
    assert nv.audio_player.is_playing
    order = []
    real = session.continue_meeting
    session.continue_meeting = lambda f, **kw: order.append(nv.audio_player.is_playing) or real(f, **kw)
    nv.continue_btn.click()
    assert order == [False]


def test_a_failure_flashes_on_the_notes_view(shell, meetings_root, fake):
    folder = _with_audio(meetings_root)
    fake["raise"] = True
    win, _app = shell("pro")
    page, nv = _notes(win, folder)
    nv.play_btn.click()
    assert nv.flash_label.text() == "Couldn't play the recording."
    assert nv.flash_label.property("mtone") == "red"


def test_every_player_control_has_a_name(qapp, fake, parts):
    from PySide6.QtWidgets import QAbstractButton
    p = _player(parts)
    for b in p.findChildren(QAbstractButton):
        assert (b.accessibleName() or "").strip()
    assert p.wave.accessibleName() == "Recording position"


# ── one real QMediaPlayer (muted, never played) ──────────────────────────────
def test_a_real_player_loads_a_wav_and_lets_go_of_it(qapp, tmp_path):
    try:
        import PySide6.QtMultimedia  # noqa: F401
    except Exception:
        pytest.skip("QtMultimedia isn't available here")
    from ui.components import AudioPlayer
    from ui.components import audio_player as ap
    path = _wav(tmp_path / "audio_part1.wav", 0.5)
    old = ap.SYNC_LOAD
    ap.SYNC_LOAD = True
    try:
        p = AudioPlayer()
        p.set_parts([path])
        try:
            player = p._ensure_player()
        except Exception:
            player = None
        if player is None:
            pytest.skip("QtMultimedia can't start here")
        p._output.setMuted(True)
        p._output.setVolume(0.0)
        p._load(0, 0)                               # load only: play() is never called
        if not _wait_for(lambda: p._media_ready, 5.0):
            pytest.skip("no media backend loaded the WAV here")
        assert abs(int(player.duration()) - 500) <= 30
        assert not p.is_playing and player.playbackState().name != "PlayingState"
        p.stop(release=True)
        assert player.source().isEmpty()
        QApplication.processEvents()
        assert _wait_for(lambda: _removable(path), 3.0)  # Windows lets go of the file
    finally:
        ap.SYNC_LOAD = old


def _removable(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False
