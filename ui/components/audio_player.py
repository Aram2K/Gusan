"""AudioPlayer: a meeting's recording, played inside the app.

    player = AudioPlayer(parent)
    player.set_parts(["...\\audio_part1.wav", "...\\audio_part2.wav"], duration_hint_sec=389)
    player.failed.connect(lambda text: view.flash(text, "red"))

A round play/pause button, a waveform drawn from the recording (click or
drag it to seek; Left / Right 5 s, Home / End, Space / Enter play or pause)
and the time played / the total.

- Several parts (a meeting that was continued) play as one recording: each
  part keeps its offset, the player loads the next part when one ends, and
  nothing is exported to a temp file.
- The total is the parts' real length (from their WAV headers), not the
  meeting's meta duration (pauses and the 2 h audio cap make those differ).
- Lengths and the waveform are read off the GUI thread
  (audio_export.part_durations_ms / audio_export.peaks).
- QtMultimedia is imported on the first Play only (the first QAudioOutput
  costs ~0.5 s): the button turns to its pressed look at once. On Windows
  the Media Foundation backend is picked (QT_MEDIA_BACKEND=windows, set right
  before that first import): it starts faster than FFmpeg and plays WAV.
- A loaded part holds its file open (Windows then refuses to delete it), so
  ``stop(release=True)`` lets go of it: call it before a delete, when the
  view hides and when another meeting loads.
- Parts that are empty or fail to load are skipped; ``failed(text)`` fires
  only when nothing can be played.

Tests: set ``SYNC_LOAD = True`` to read the parts inline, and replace
``AudioPlayer._make_player`` with a stand-in (never real sound in a test).
"""

import logging
import os
import sys
import threading

from PySide6.QtCore import QCoreApplication, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from ui import theme
from ui.components import icons
from ui.components.surfaces import label

logger = logging.getLogger("transcribe")

SYNC_LOAD = False           # tests: read lengths and the waveform inline

BARS = 34                   # the waveform's bars (3 px on a 6 px pitch, like the mockup)
STEP_MS = 5000              # Left / Right
TICK_MS = 200               # the time label while playing

PLAY_TEXT = "Play recording"
PAUSE_TEXT = "Pause recording"
FAILED_TEXT = "Couldn't play the recording."


def fmt_time(ms):
    """389736 -> '6:29'; 3723000 -> '1:02:03'."""
    try:
        sec = max(0, int(ms or 0)) // 1000
    except (TypeError, ValueError):
        sec = 0
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def _name(value):
    """A QMediaPlayer enum (or a stand-in's plain string) -> its name."""
    return str(getattr(value, "name", value))


def _signature(paths):
    """[(path, size, mtime)] - a stat per part, cheap enough for the GUI thread."""
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((p, st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((p, -1, 0))
    return out


class WaveSeek(QWidget):
    """The recording's waveform, which is also the seek bar.

    ``seeked(fraction)`` on a click, a drag, Home (0) and End (1);
    ``stepped(ms)`` on Left / Right; ``toggled()`` on Space / Enter."""

    seeked = Signal(float)
    stepped = Signal(int)
    toggled = Signal()

    PAD = 4                 # room for the focus ring
    PITCH = 6
    BAR = 3
    HEIGHT = 28

    def __init__(self, bars=BARS, parent=None):
        super().__init__(parent)
        self._n = max(1, int(bars))
        self._peaks = []
        self._frac = 0.0
        self._drag = False
        self.setFixedSize(self.bars_width() + 2 * self.PAD, self.HEIGHT + 2 * self.PAD)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Recording position")
        self.setAccessibleDescription("0:00 of 0:00")
        theme.focus_ring(self)

    def bars_width(self):
        return self._n * self.PITCH - (self.PITCH - self.BAR)

    def bar_count(self):
        return self._n

    def peaks(self):
        return list(self._peaks)

    def fraction(self):
        return self._frac

    def played_bars(self):
        """How many bars show as played (blue)."""
        if self._frac <= 0:
            return 0
        return sum(1 for i in range(self._n) if (i + 0.5) / self._n <= self._frac)

    def set_peaks(self, peaks):
        self._peaks = [max(0.0, min(1.0, float(v))) for v in (peaks or [])][: self._n]
        self.update()

    def set_progress(self, fraction, current="0:00", total="0:00"):
        frac = max(0.0, min(1.0, float(fraction or 0.0)))
        desc = "%s of %s" % (current, total)
        if desc != self.accessibleDescription():
            self.setAccessibleDescription(desc)
        if frac != self._frac:
            before = self.played_bars()
            self._frac = frac
            if self.played_bars() != before:
                self.update()

    def fraction_at(self, x):
        return max(0.0, min(1.0, (float(x) - self.PAD) / float(self.bars_width())))

    # ── painting ──
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        played = self.played_bars()
        blue, grey = theme.qcolor("blue-l"), theme.qcolor("line2")
        for i in range(self._n):
            f = self._peaks[i] if i < len(self._peaks) else 0.0
            h = max(4, int(round(self.HEIGHT * f)))
            p.setBrush(blue if i < played else grey)
            p.drawRoundedRect(QRectF(self.PAD + i * self.PITCH, self.PAD + (self.HEIGHT - h) / 2.0,
                                     self.BAR, h), 1.5, 1.5)
        if self.property("focusVisible"):
            pen = QPen(theme.qcolor("blue-l"))
            pen.setWidthF(2.0)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 6, 6)
        p.end()

    # ── mouse and keys ──
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            super().mousePressEvent(e)
            return
        self._drag = True
        self.seeked.emit(self.fraction_at(e.position().x()))
        e.accept()

    def mouseMoveEvent(self, e):
        if self._drag and (e.buttons() & Qt.LeftButton):
            self.seeked.emit(self.fraction_at(e.position().x()))
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag = False
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Left:
            self.stepped.emit(-STEP_MS)
        elif k == Qt.Key_Right:
            self.stepped.emit(STEP_MS)
        elif k == Qt.Key_Home:
            self.seeked.emit(0.0)
        elif k == Qt.Key_End:
            self.seeked.emit(1.0)
        elif k in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.toggled.emit()
        else:
            super().keyPressEvent(e)
            return
        e.accept()

    def wheelEvent(self, e):
        e.ignore()                      # the page scrolls; the wheel never seeks


class AudioPlayer(QWidget):
    """Play / pause, the waveform seek bar and the time labels."""

    failed = Signal(str)
    playing_changed = Signal(bool)
    _loaded = Signal(int, object)       # from the reading thread

    def __init__(self, parent=None, bars=BARS):
        super().__init__(parent)
        self._paths = []
        self._sig = []                  # (path, size, mtime) when the parts were read
        self._durs = []                 # ms per part (0: skipped)
        self._offsets = []
        self._total = 0
        self._hint = 0                  # ms shown until the real lengths are read
        self._ready = True              # lengths known
        self._pos = 0                   # ms across all parts
        self._index = -1                # the part loaded into the player
        self._pending = None            # ms to seek to once that part has loaded
        self._media_ready = False
        self._loading_seen = False
        self._want = False              # the person asked to play
        self._bad = set()
        self._any_ok = False
        self._fail_queued = False
        self._gen = 0
        self._player = None
        self._output = None
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._loaded.connect(self._on_loaded)
        app = QCoreApplication.instance()
        if app is not None:
            # Quit (also a Windows log-off): the sound stops and the file is
            # let go before the windows and the media backend are torn down.
            app.aboutToQuit.connect(self._on_quit)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.play_btn = QToolButton(self)
        self.play_btn.setProperty("gusan", "playBtn")
        self.play_btn.setProperty("loading", False)
        self.play_btn.setFixedSize(40, 40)
        self.play_btn.setIconSize(QSize(18, 18))
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self.play_btn)
        self.play_btn.clicked.connect(self.toggle)
        row.addWidget(self.play_btn, 0, Qt.AlignVCenter)
        self.wave = WaveSeek(bars, self)
        self.wave.seeked.connect(lambda f: self.seek(int(round(f * self._shown_total()))))
        self.wave.stepped.connect(lambda d: self.seek(self.position_ms + d))
        self.wave.toggled.connect(self.toggle)
        row.addWidget(self.wave, 0, Qt.AlignVCenter)
        row.addStretch(1)
        col.addLayout(row)
        times = QWidget(self)
        times.setFixedWidth(self.wave.width() - 2 * WaveSeek.PAD)
        tl = QHBoxLayout(times)
        tl.setContentsMargins(0, 0, 0, 0)
        self.time_label = label("0:00", "muted", times)
        self.total_label = label("0:00", "muted", times)
        for lb in (self.time_label, self.total_label):
            lb.setFont(theme.font(12.5, tabular=True))
        tl.addWidget(self.time_label)
        tl.addStretch(1)
        tl.addWidget(self.total_label)
        trow = QHBoxLayout()
        trow.setContentsMargins(40 + 12 + WaveSeek.PAD, 0, 0, 0)
        trow.addWidget(times)
        trow.addStretch(1)
        col.addLayout(trow)
        self._set_button(False)
        self._refresh()

    # ── what the page reads ──
    @property
    def position_ms(self):
        self._sync_pos()
        return self._pos

    @property
    def duration_ms(self):
        return self._total

    @property
    def is_playing(self):
        return self._want

    def parts(self):
        return list(self._paths)

    def durations_ms(self):
        return list(self._durs)

    # ── the recording ──
    def set_parts(self, paths, duration_hint_sec=0):
        """Show these recording parts (in order). The same list again does
        nothing, so a reload of the same meeting keeps playing (a part that
        changed on disk is only read again); a new list stops, lets go of the
        old file and starts at 0:00."""
        paths = [str(p) for p in (paths or [])]
        sig = _signature(paths)
        if paths == self._paths:
            if sig != self._sig:
                # A part was still being written when it was read (opened
                # while "Saving the recording…"): read it again, keep playing.
                self._sig = sig
                self._gen += 1
                self._read(paths, self._gen)
            return
        self.stop(release=True)
        self._paths = paths
        self._sig = sig
        self._durs, self._offsets, self._total = [], [], 0
        self._pos = 0
        self._bad = set()
        self._any_ok = False
        self._gen += 1
        try:
            self._hint = max(0, int(float(duration_hint_sec or 0) * 1000))
        except (TypeError, ValueError):
            self._hint = 0
        self._ready = not paths
        self.wave.set_peaks([])
        self._refresh()
        if paths:
            self._read(paths, self._gen)

    def _read(self, paths, gen):
        bars = self.wave.bar_count()

        def work():
            import audio_export
            try:
                return audio_export.part_durations_ms(paths), audio_export.peaks(paths, bars)
            except Exception:
                logger.warning("Reading the recording failed", exc_info=True)
                return [0] * len(paths), []

        if SYNC_LOAD:
            self._on_loaded(gen, work())
            return

        def run():
            res = work()
            try:
                self._loaded.emit(gen, res)
            except RuntimeError:
                pass                    # the player is gone

        threading.Thread(target=run, daemon=True, name="gusan-audio").start()

    def _on_loaded(self, gen, res):
        if gen != self._gen:
            return                      # the parts changed meanwhile
        durs, pk = res if res else ([], [])
        durs = [max(0, int(d or 0)) for d in (list(durs) + [0] * len(self._paths))[: len(self._paths)]]
        self._durs = durs
        self._offsets, acc = [], 0
        for d in durs:
            self._offsets.append(acc)
            acc += d
        self._total = acc
        self._ready = True
        self.wave.set_peaks(pk)
        self._refresh()
        if self._want and self._index < 0:
            self._start()               # Play was pressed while the parts were read

    # ── controls ──
    def toggle(self):
        if self._want:
            self.pause()
        else:
            self.play()

    def play(self):
        if not self._paths or self._want:
            return
        self._want = True
        self._set_button(True, loading=True)
        self.playing_changed.emit(True)
        if not self._ready:
            return                      # starts when the parts are read
        if self._bad and not self._playable():
            self._bad = set()           # every part failed last time: try them again
        self._start()

    def pause(self):
        was = self._want
        self._want = False
        p = self._player
        if p is not None and self._index >= 0:
            self._sync_pos()
            try:
                p.pause()
            except Exception:
                logger.debug("pause failed", exc_info=True)
        self._timer.stop()
        self._set_button(False)
        self._refresh()
        if was:
            self.playing_changed.emit(False)

    def stop(self, release=True):
        """Pause; release=True also lets go of the file (Windows can then
        delete it). The position is kept."""
        self.pause()
        if release:
            self._unload()
            self._bad = set()           # a part that failed (e.g. briefly locked) is tried again next time

    def _on_quit(self):
        try:
            self.stop(release=True)
        except Exception:
            logger.debug("Stopping the recording at quit failed", exc_info=True)

    def seek(self, ms):
        """Go to ``ms`` across all parts (clamped)."""
        if not self._ready or self._total <= 0:
            return
        try:
            ms = max(0, min(self._total, int(ms)))
        except (TypeError, ValueError):
            return
        self._pos = ms
        p = self._player
        if p is not None and self._index >= 0:
            import audio_export
            idx, local = audio_export.locate(self._durs, ms)
            if idx == self._index:
                if self._media_ready and self._pending is None:
                    try:
                        p.setPosition(local)
                    except Exception:
                        logger.debug("setPosition failed", exc_info=True)
                else:
                    self._pending = local
            elif idx >= 0:
                self._load(idx, local)
        self._refresh()

    # ── the player ──
    def _start(self):
        if self._total <= 0 or not self._playable():
            self._fail()
            return
        if self._player is None:
            self.play_btn.repaint()      # the pressed look before the backend's slow first start
        p = self._ensure_player()
        if p is None:
            self._fail()
            return
        if self._pos >= self._total:
            self._pos = 0
        import audio_export
        idx, local = audio_export.locate(self._durs, self._pos)
        if idx in self._bad:
            # A broken part: the next good one, else from the first good one.
            nxt = self._next_good(idx)
            idx = nxt if nxt is not None else self._next_good(-1)
            if idx is None:
                self._fail()
                return
            local = 0
            self._pos = self._offsets[idx]
        if idx != self._index:
            self._load(idx, local)       # plays once loaded
        elif self._media_ready and self._pending is None:
            try:
                if abs(int(p.position()) - local) > 300:
                    p.setPosition(local)     # e.g. Play after End: from the start
                p.play()
            except Exception:
                logger.warning("Playing the recording failed", exc_info=True)
                self._fail()

    def _ensure_player(self):
        if self._player is not None:
            return self._player
        try:
            p = self._make_player()
        except Exception:
            logger.warning("The audio player couldn't start", exc_info=True)
            return None
        p.mediaStatusChanged.connect(self._on_status)
        p.playbackStateChanged.connect(self._on_state)
        p.errorOccurred.connect(self._on_error)
        self._player = p
        return p

    def _make_player(self):
        if sys.platform == "win32":
            # Media Foundation: plays WAV, starts faster than FFmpeg. Only
            # read before the first QtMultimedia use, so it is set right here.
            os.environ.setdefault("QT_MEDIA_BACKEND", "windows")
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        player = QMediaPlayer(self)
        self._output = QAudioOutput(player)
        player.setAudioOutput(self._output)
        return player

    def _load(self, idx, local):
        p = self._player
        self._index = idx
        self._pending = max(0, int(local or 0))
        self._media_ready = False
        # Only a LoadedMedia after this load's LoadingMedia counts: FFmpeg
        # reports the old part as loaded again when a new source replaces one
        # that had ended.
        self._loading_seen = False
        try:
            p.setSource(QUrl.fromLocalFile(self._paths[idx]))
        except Exception:
            logger.warning("Loading the recording failed", exc_info=True)
            self._queue_fail()

    def _unload(self):
        self._sync_pos()
        p = self._player
        had = self._index >= 0
        # Forget the part first: the signals the player sends while it stops
        # must not move the position.
        self._index = -1
        self._pending = None
        self._media_ready = False
        self._timer.stop()
        if p is not None and had:
            try:
                p.stop()
                p.setSource(QUrl())
            except Exception:
                logger.debug("Releasing the recording failed", exc_info=True)

    def _playable(self):
        return any(d > 0 and i not in self._bad for i, d in enumerate(self._durs))

    def _next_good(self, idx):
        for j in range(idx + 1, len(self._durs)):
            if self._durs[j] > 0 and j not in self._bad:
                return j
        return None

    def _sync_pos(self):
        p = self._player
        if p is None or self._index < 0 or not self._media_ready or self._pending is not None:
            return
        try:
            local = int(p.position())
        except Exception:
            return
        self._pos = max(0, min(self._total, self._offsets[self._index] + max(0, local)))

    def _tick(self):
        self._sync_pos()
        self._refresh()

    def _on_status(self, status):
        name = _name(status)
        if name == "LoadingMedia":
            self._loading_seen = True
        elif (name in ("LoadedMedia", "BufferedMedia") and not self._media_ready and self._index >= 0
              and self._loading_seen):
            self._media_ready = True
            self._any_ok = True
            p = self._player
            local, self._pending = self._pending, None
            try:
                if local:
                    p.setPosition(local)
                if self._want:
                    p.play()
            except Exception:
                logger.warning("Playing the recording failed", exc_info=True)
                self._queue_fail()
        elif name == "EndOfMedia":
            QTimer.singleShot(0, self._part_ended)
        elif name == "InvalidMedia":
            self._queue_fail()

    def _on_state(self, state):
        if _name(state) == "PlayingState":
            self._timer.start()
            self._set_button(True)
        else:
            self._timer.stop()
            self._sync_pos()
            if not self._want:
                self._set_button(False)
            self._refresh()

    def _on_error(self, error, text=""):
        if _name(error) in ("NoError", "0"):
            return
        logger.warning("Recording playback error: %s %s", _name(error), text)
        self._queue_fail()

    def _queue_fail(self):
        # errorOccurred and InvalidMedia come together: handle them once.
        if not self._fail_queued:
            self._fail_queued = True
            QTimer.singleShot(0, self._part_failed)

    def _part_failed(self):
        self._fail_queued = False
        if self._index < 0:
            return
        self._bad.add(self._index)
        nxt = self._next_good(self._index)
        if nxt is not None and self._want:
            self._pos = self._offsets[nxt]
            self._load(nxt, 0)
            return
        nothing = not self._any_ok or not self._playable()
        was = self._want
        self._want = False
        self._unload()
        if was:
            self._pos = 0                       # the rest is broken: like the end, back to the start
        self._set_button(False)
        self._refresh()
        if was:
            self.playing_changed.emit(False)
        if nothing:
            self.failed.emit(FAILED_TEXT)

    def _part_ended(self):
        if self._index < 0:
            return
        idx = self._index
        nxt = self._next_good(idx)
        if nxt is not None and self._want:
            self._pos = self._offsets[nxt]
            self._load(nxt, 0)
            return
        was = self._want
        self._want = False
        self._unload()
        if was:
            self._pos = 0                       # played to the end: back to the start
        else:
            # Paused right at a part's end: stay there (Play goes on from
            # the next part), not at the very end.
            self._pos = min(self._total, self._offsets[idx] + self._durs[idx])
        self._set_button(False)
        self._refresh()
        if was:
            self.playing_changed.emit(False)

    def _fail(self):
        was = self._want
        self._want = False
        self._unload()
        self._set_button(False)
        self._refresh()
        if was:
            self.playing_changed.emit(False)
        self.failed.emit(FAILED_TEXT)

    # ── look ──
    def _set_button(self, playing, loading=False):
        b = self.play_btn
        b.setIcon(icons.icon("pause" if playing else "play", 18, "#ffffff", sw=2.4))
        text = PAUSE_TEXT if playing else PLAY_TEXT
        if b.accessibleName() != text:
            b.setAccessibleName(text)
            b.setToolTip(text)
        theme.set_prop(b, "loading", bool(loading), children=False)

    def _shown_total(self):
        if self._ready and self._total > 0:
            return self._total
        return self._hint

    def _refresh(self):
        total = self._shown_total()
        pos = min(self._pos, total) if total else 0
        cur, tot = fmt_time(pos), fmt_time(total)
        if self.time_label.text() != cur:
            self.time_label.setText(cur)
        if self.total_label.text() != tot:
            self.total_label.setText(tot)
        self.wave.set_progress(pos / float(total) if total else 0.0, cur, tot)
