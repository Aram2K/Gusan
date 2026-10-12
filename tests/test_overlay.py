"""Dictation HUD (ui/overlay.py): sizes, error copy, theme choice, level
gating, the never-steal-focus window contract, and an offscreen paint smoke
for every state. Qt-dependent parts are guarded like test_wheelguard."""
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


class _FixedMetrics:
    """7 px per character: the offscreen platform has no real fonts, so copy
    decisions are tested against deterministic widths."""
    def __init__(self, font):
        pass

    def horizontalAdvance(self, text):
        return 7.0 * len(text)

    def elidedText(self, text, mode, width):
        return text


def _ensure_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestErrorHint(unittest.TestCase):
    def test_contextual_hints(self):
        from ui.overlay import _error_hint
        self.assertIn("API key", _error_hint("401 Unauthorized"))
        self.assertIn("microphone", _error_hint("Microphone error: device busy"))
        self.assertIn("shorter clip", _error_hint("Request timed out"))
        self.assertEqual(_error_hint("something odd"), "Try again or restart Gusan")


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestOverlay(unittest.TestCase):
    def setUp(self):
        _ensure_app()
        import ui.overlay as ov
        self.ov = ov
        self.cfg = {"hotkey": "alt+r", "accent_color": "#3b82f6", "backend": "local"}
        self.app = SimpleNamespace(cfg=self.cfg, save_config=MagicMock())
        self.hud = ov.Overlay(main_app=self.app)

    def tearDown(self):
        self.hud.timer.stop()
        self.hud.hide()
        self.hud.deleteLater()

    # ── window contract ──
    def test_never_takes_focus_but_takes_the_mouse(self):
        # The text is pasted into whatever has focus: the HUD must never take
        # it - but it must get mouse input, so it can be dragged. (Outside
        # the capsule the window is made click-through at runtime.)
        from PySide6.QtCore import Qt
        flags = self.hud.windowFlags()
        self.assertTrue(flags & Qt.WindowDoesNotAcceptFocus)
        self.assertFalse(flags & Qt.WindowTransparentForInput)
        self.assertTrue(flags & Qt.WindowStaysOnTopHint)
        self.assertTrue(self.hud.testAttribute(Qt.WA_ShowWithoutActivating))
        # Offscreen test platform: no fake window id may reach a Win32 call.
        self.assertEqual(self.hud._hwnd, 0)

    # ── moving it ──
    def _mouse(self, kind, local, button=None):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        types = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove,
                 "release": QEvent.MouseButtonRelease, "double": QEvent.MouseButtonDblClick}
        local = QPointF(local)
        glob = QPointF(self.hud.mapToGlobal(local.toPoint()))
        btn = Qt.LeftButton if kind != "move" else Qt.NoButton
        held = Qt.LeftButton if kind in ("press", "move", "double") else Qt.NoButton
        ev = QMouseEvent(types[kind], local, glob, btn, held, Qt.NoModifier)
        getattr(self.hud, {"press": "mousePressEvent", "move": "mouseMoveEvent",
                           "release": "mouseReleaseEvent",
                           "double": "mouseDoubleClickEvent"}[kind])(ev)
        return ev

    def _capsule_centre(self):
        return self.hud._capsule_now().center()

    def test_dragging_moves_it_and_remembers_the_spot(self):
        from PySide6.QtCore import QPointF
        self.hud.show_overlay(self.ov.RECORDING)
        start = self.hud.pos()
        c = self._capsule_centre()
        self._mouse("press", c)
        self._mouse("move", c + QPointF(-60, -40))
        self._mouse("release", c + QPointF(-60, -40))
        moved = self.hud.pos()
        self.assertNotEqual(moved, start)
        self.assertEqual(self.cfg["overlay_pos"], [moved.x(), moved.y()])
        self.app.save_config.assert_called()
        self.assertIsNone(self.hud._press)

    def test_a_click_is_not_a_drag(self):
        from PySide6.QtCore import QPointF
        self.hud.show_overlay(self.ov.RECORDING)
        start = self.hud.pos()
        c = self._capsule_centre()
        self._mouse("press", c)
        self._mouse("move", c + QPointF(1, 1))           # under the drag slop
        self._mouse("release", c + QPointF(1, 1))
        self.assertEqual(self.hud.pos(), start)
        self.assertNotIn("overlay_pos", self.cfg)
        self.app.save_config.assert_not_called()

    def test_presses_outside_the_capsule_are_ignored(self):
        from PySide6.QtCore import QPointF
        self.hud.show_overlay(self.ov.RECORDING)
        ev = self._mouse("press", QPointF(3, 3))           # transparent corner
        self.assertFalse(ev.isAccepted())
        self.assertIsNone(self.hud._press)

    def test_double_click_sends_it_home(self):
        self.cfg["overlay_pos"] = [40, 40]
        self.hud.show_overlay(self.ov.RECORDING)
        self._mouse("double", self._capsule_centre())
        self.assertIsNone(self.cfg["overlay_pos"])
        self.assertIsNotNone(self.hud._glide)
        for _ in range(200):                               # let the glide finish
            self.hud._glide_step(0.016)
            if self.hud._glide is None:
                break
        self.assertIsNone(self.hud._glide)
        self.assertEqual(self.hud.pos(), self.hud._home(self.hud._screen))

    def test_saved_position_is_used_only_while_on_a_screen(self):
        from PySide6.QtWidgets import QApplication
        a = QApplication.primaryScreen().availableGeometry()
        self.cfg["overlay_pos"] = [a.x() + 10, a.y() + 10]
        self.hud.reposition()
        self.assertEqual((self.hud.x(), self.hud.y()), (a.x() + 10, a.y() + 10))
        self.cfg["overlay_pos"] = [a.x() + 100000, a.y() + 100000]   # monitor gone
        self.hud.reposition()
        self.assertEqual(self.hud.pos(), self.hud._home(self.hud._screen))
        self.cfg["overlay_pos"] = "garbage"
        self.hud.reposition()
        self.assertEqual(self.hud.pos(), self.hud._home(self.hud._screen))

    # ── live glass samples ──
    def _sample(self, luma, gen=None):
        from PySide6.QtGui import QImage
        img = QImage(8, 8, QImage.Format_RGB32)
        img.fill(0)
        return (self.hud._glass_gen if gen is None else gen, img, (0, 0), 1.0, luma)

    def test_first_sample_picks_the_theme_and_starts_the_fade_in(self):
        self.hud.show_overlay(self.ov.RECORDING)
        self.hud._live, self.hud._waiting = True, True
        self.hud._on_backdrop(self._sample(0.95))
        self.assertFalse(self.hud._waiting)
        self.assertEqual(self.hud._theme, "light")           # auto, bright backdrop
        self.assertIsNotNone(self.hud._bd)
        seq = self.hud._bd_seq
        self.hud._on_backdrop(self._sample(0.1))              # a later live sample
        self.assertEqual(self.hud._bd_seq, seq + 1)
        self.assertEqual(self.hud._theme, "light")            # theme holds per appearance

    def test_samples_from_an_earlier_appearance_are_dropped(self):
        self.hud.show_overlay(self.ov.RECORDING)
        self.hud._live, self.hud._waiting = True, True
        self.hud._on_backdrop(self._sample(0.95, gen=self.hud._glass_gen - 1))
        self.assertTrue(self.hud._waiting)
        self.assertIsNone(self.hud._bd)

    def test_without_live_glass_one_sample_only_sets_the_theme(self):
        self.hud.show_overlay(self.ov.RECORDING)
        self.hud._live, self.hud._waiting = False, True
        self.hud._sampler = MagicMock()
        self.hud._on_backdrop(self._sample(0.1))
        self.assertEqual(self.hud._theme, "dark")
        self.assertIsNone(self.hud._bd)                       # plate, not a stale picture
        self.assertFalse(self.hud._sampler.active)

    def test_the_fade_in_never_waits_forever_for_a_sample(self):
        self.hud.show_overlay(self.ov.RECORDING)
        self.hud._waiting = True
        self.hud._shown_at = time.monotonic() - self.ov.FIRST_SAMPLE_S - 0.01
        self.hud._loop()
        self.assertFalse(self.hud._waiting)
        self.assertGreater(self.hud._show, 0.0)

    def test_done_waits_while_the_cursor_is_on_it(self):
        self.hud.show_done(True)
        self.hud._hover = True
        self.hud._update_hover = lambda: None              # keep the hover
        self.hud._hide_at = time.monotonic() - 1           # already due
        self.hud._loop()
        self.assertTrue(self.hud._visible)
        self.assertGreater(self.hud._hide_at, time.monotonic())

    def test_frame_clock_runs_only_while_shown(self):
        self.assertFalse(self.hud.timer.isActive())
        self.hud.show_overlay(self.ov.RECORDING)
        self.assertTrue(self.hud.timer.isActive())
        self.hud.hide_overlay()
        self.hud._show = 0.0001
        self.hud._loop()
        self.assertFalse(self.hud.timer.isActive())
        self.assertFalse(self.hud.isVisible())

    # ── sizes ──
    def test_recording_capsule_settles_after_the_hint(self):
        ov = self.ov
        self.hud.overlay_state = ov.RECORDING
        self.hud._rec_started = time.monotonic()
        self.assertEqual(self.hud._target_size(), (float(ov.REC_W_HINT), float(ov.PILL_H)))
        self.hud._rec_started = time.monotonic() - ov.HINT_S - 0.1
        self.assertEqual(self.hud._target_size(), (float(ov.REC_W), float(ov.PILL_H)))
        self.assertLess(ov.REC_W, ov.REC_W_HINT)

    def test_busy_capsule_grows_with_live_words_up_to_a_cap(self):
        ov = self.ov
        self.hud.overlay_state = ov.TRANSCRIBING
        short_w, _ = self.hud._target_size()
        self.hud.set_partial("so the launch moves to the second week of October")
        mid_w, h = self.hud._target_size()
        self.hud.set_partial("word " * 400)
        long_w, _ = self.hud._target_size()
        self.assertGreater(mid_w, short_w)
        self.assertEqual(long_w, float(ov.PILL_MAX_W))
        self.assertEqual(h, float(ov.PILL_H))
        # Every state fits in the fixed window with room for the shadow.
        self.assertLessEqual(ov.PILL_MAX_W, ov.WIN_W - 2 * 16)

    def test_error_is_taller_and_fits(self):
        ov = self.ov
        self.hud.show_error("x" * 500)
        w, h = self.hud._target_size()
        self.assertEqual(h, float(ov.PILL_H_ERROR))
        self.assertLessEqual(w, float(ov.PILL_MAX_W))

    # ── error copy ──
    def _lines(self, msg):
        self.hud.show_error(msg)
        self.hud._err_split = None          # drop the split memoised with real metrics
        with mock.patch.object(self.ov, "QFontMetricsF", _FixedMetrics):
            return self.hud._error_lines()

    def test_short_error_gets_a_contextual_hint(self):
        title, detail = self._lines("Only background noise was detected.")
        self.assertEqual(title, "Only background noise was detected.")
        self.assertEqual(detail, "Try again or restart Gusan")

    def test_long_error_splits_at_its_own_dash_instead_of_repeating_a_hint(self):
        title, detail = self._lines("Transcription timed out after 120s - try a shorter clip "
                                    "or a smaller model.")
        self.assertEqual(title, "Transcription timed out after 120s")
        self.assertEqual(detail, "Try a shorter clip or a smaller model.")

    def test_long_error_without_separator_wraps(self):
        msg = ("The selected input device stopped delivering audio frames while the "
               "recording was running so nothing could be transcribed this time")
        title, detail = self._lines(msg)
        self.assertTrue(title and detail)
        self.assertEqual((title + " " + detail).split(), msg.split())

    # ── theme ──
    def test_theme_is_auto_by_default(self):
        # No key: light glass over bright content, dark over dark.
        self.hud._bd_luma = 0.95
        self.assertEqual(self.hud._pick_theme(), "light")
        self.hud._bd_luma = 0.1
        self.assertEqual(self.hud._pick_theme(), "dark")
        # No snapshot (macOS, transparency effects off): the system theme.
        self.hud._bd_luma = None
        with mock.patch.object(self.ov, "_system_prefers_light", return_value=True):
            self.assertEqual(self.hud._pick_theme(), "light")
        with mock.patch.object(self.ov, "_system_prefers_light", return_value=False):
            self.assertEqual(self.hud._pick_theme(), "dark")

    def test_pinned_theme_wins_over_the_backdrop(self):
        self.cfg["overlay_theme"] = "dark"
        self.hud._bd_luma = 0.95
        self.assertEqual(self.hud._pick_theme(), "dark")
        self.cfg["overlay_theme"] = "light"
        self.hud._bd_luma = 0.1
        self.assertEqual(self.hud._pick_theme(), "light")
        self.cfg["overlay_theme"] = "neon"                         # unknown -> auto
        self.assertEqual(self.hud._pick_theme(), "dark")

    def test_apply_prefs_repicks_the_theme_only_while_on_screen(self):
        # Gusan 2.0 settings apply at once (settings_apply -> overlay.apply_prefs).
        self.hud._theme = "light"
        self.cfg["overlay_theme"] = "dark"
        self.hud.apply_prefs()                                     # hidden: nothing to do
        self.assertEqual(self.hud._theme, "light")
        with mock.patch.object(self.hud, "isVisible", return_value=True):   # on screen
            self.hud.apply_prefs()
        self.assertEqual(self.hud._theme, "dark")

    # ── voice levels ──
    def test_levels_gate_the_noise_floor(self):
        self.hud.update_levels([0.02] * 20)
        self.assertLess(self.hud._level_target, 0.02)
        self.hud.update_levels([0.9] * 20)
        self.assertGreater(self.hud._level_target, 0.8)
        before = self.hud._level_target
        self.hud.update_levels([])                                 # ignored
        self.assertEqual(self.hud._level_target, before)

    # ── paint smoke ──
    def _render(self):
        from PySide6.QtGui import QImage, QColor
        from PySide6.QtCore import Qt
        hud = self.hud
        hud._show = 1.0
        hud._xfade = 1.0
        hud._from_vis = None
        hud._cur_vis = hud._visual()
        hud._pw, hud._ph = hud._target_size()
        img = QImage(hud.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        hud.render(img)
        ov = self.ov
        centre = img.pixelColor(ov.WIN_W // 2, int(ov.WIN_H - ov.BOTTOM_PAD - hud._ph / 2))
        corner = img.pixelColor(2, 2)
        return centre, corner

    # ── a Smart Action take (D4) ──
    def test_a_smart_take_is_marked_and_every_show_resets_it(self):
        ov = self.ov
        self.hud.show_overlay(ov.RECORDING, smart=True)
        self.assertTrue(self.hud._smart)
        self.hud._rec_started = time.monotonic() - ov.HINT_S - 0.1
        self.assertEqual(self.hud._target_size(), (float(ov.REC_W + ov.SMART_W), float(ov.PILL_H)))
        self.hud.set_state(ov.PROCESSING)                  # still the same take
        self.assertTrue(self.hud._smart)
        self.hud.show_overlay(ov.RECORDING)                # the next take: plain dictation
        self.assertFalse(self.hud._smart)
        self.hud._rec_started = time.monotonic() - ov.HINT_S - 0.1
        self.assertEqual(self.hud._target_size(), (float(ov.REC_W), float(ov.PILL_H)))

    def _purple_pixels(self):
        from PySide6.QtGui import QImage
        from PySide6.QtCore import Qt
        hud = self.hud
        hud._show, hud._xfade, hud._from_vis = 1.0, 1.0, None
        hud._cur_vis = hud._visual()
        hud._pw, hud._ph = hud._target_size()
        img = QImage(hud.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        hud.render(img)
        n = 0
        for y in range(0, img.height(), 2):
            for x in range(0, img.width(), 2):
                c = img.pixelColor(x, y)
                if c.alpha() > 200 and c.blue() - c.green() > 70 and c.red() - c.green() > 30:
                    n += 1
        return n

    def test_the_smart_mark_is_purple_and_only_on_a_smart_take(self):
        ov = self.ov
        for theme in ("dark", "light"):
            for smart in (True, False):
                self.hud.show_overlay(ov.RECORDING, smart=smart)
                self.hud._theme = theme
                self.hud._bars = [0.6] * ov.N_BARS         # some voice
                n = self._purple_pixels()
                if smart:
                    self.assertGreater(n, 10, theme)
                else:
                    self.assertEqual(n, 0, theme)

    # ── what it says (pill_copy) ──
    CLOUD = {"writer": "Gusan Cloud", "writer_local": False}

    def _smart_take(self, kind=None, detail="", info=None):
        ov = self.ov
        self.hud.show_overlay(ov.RECORDING, smart=True)
        self.hud.set_speech({"speech": "Whisper Small", "speech_local": True})
        self.hud.set_state(ov.TRANSCRIBING)
        self.hud.set_task(kind, detail, self.CLOUD if info is None else info)
        self.hud.set_state(ov.PROCESSING)

    def test_a_smart_action_says_what_it_writes(self):
        ov, hud = self.ov, self.hud
        hud.show_overlay(ov.RECORDING, smart=True)
        hud.set_speech({"speech": "Whisper Small", "speech_local": True})
        hud.set_state(ov.TRANSCRIBING)
        self.assertEqual(hud._busy_label("transcribing"), ("Transcribing", "Whisper Small"))
        hud.set_task("email", "", self.CLOUD)
        hud.set_state(ov.PROCESSING)
        self.assertEqual(hud._busy_label("processing"), ("Writing your email", "Gusan Cloud"))
        hud._state_at -= 4.5                                   # a slow writer
        self.assertEqual(hud._busy_label("processing"), ("Writing your email", "Still writing"))
        hud.show_done(True, kind="email")
        self.assertEqual(hud._done_msg, "Email pasted")
        self._smart_take("translate", "Spanish")
        self.assertEqual(hud._busy_label("processing"), ("Translating to Spanish", "Gusan Cloud"))
        self._smart_take(None, "Spanish", {"writer": "Qwen 1.5B", "writer_local": True})
        self.assertEqual(hud._busy_label("processing"), ("Thinking", "Qwen 1.5B · on this computer"))
        hud._state_at -= 10.5
        self.assertEqual(hud._busy_label("processing")[1], "This computer is working on it")
        self._smart_take("poem")                               # unknown: just thinking
        self.assertEqual(hud._task_kind, None)
        self.assertEqual(hud._busy_label("processing")[0], "Thinking")

    def test_done_lines(self):
        import pill_copy
        ov, hud = self.ov, self.hud
        hud.show_overlay(ov.RECORDING)
        hud.show_done(True)
        self.assertEqual(hud._done_msg, "Pasted")
        hud.show_overlay(ov.RECORDING)
        hud.show_done(False)
        self.assertEqual(hud._done_msg, pill_copy.done(None, False))
        self.assertIn("press", hud._done_msg)
        self._smart_take(None)                                 # a Smart Action, no clear ask
        hud.show_done(True)
        self.assertEqual(hud._done_msg, "Done · pasted")
        self._smart_take("translate", "Spanish")
        hud.show_done(False, kind="translate")
        self.assertEqual(hud._done_msg, pill_copy.done("translate", False))
        # Smart take whose free tries ran out: the exact words, plainly.
        hud.show_overlay(ov.RECORDING, smart=True)
        hud.set_state(ov.TRANSCRIBING)
        hud.show_done(True)
        self.assertEqual(hud._done_msg, "Pasted")

    def test_transcribing_names_the_model_language_and_length(self):
        ov, hud = self.ov, self.hud
        self.cfg["whisper_model"] = "small"
        hud.show_overlay(ov.RECORDING)
        hud.set_state(ov.TRANSCRIBING)                         # main didn't say: the configured one
        self.assertEqual(hud._busy_label("transcribing"), ("Transcribing", "Whisper Small"))
        hud.set_lang("English")
        self.assertEqual(hud._busy_label("transcribing"), ("Transcribing", "English"))
        # The take's length comes from the recording clock.
        hud.show_overlay(ov.RECORDING)
        hud._rec_started = time.monotonic() - 70.0
        hud.set_state(ov.TRANSCRIBING)
        self.assertAlmostEqual(hud._audio_s, 70.0, delta=1.0)
        self.assertEqual(hud._busy_label("transcribing"), ("Transcribing", "1 min of audio"))
        hud.set_audio_seconds(130)
        self.assertEqual(hud._busy_label("transcribing")[1], "2 min of audio")
        hud._state_at -= 13.0
        self.assertEqual(hud._busy_label("transcribing"), ("Long take", "Still transcribing"))

    def test_a_model_download_does_not_count_as_a_long_take(self):
        ov, hud = self.ov, self.hud
        hud.show_overlay(ov.RECORDING)
        hud.set_speech({"speech": "Whisper Small", "speech_local": True})
        hud.set_state(ov.TRANSCRIBING)
        hud.set_state(ov.GETTING_READY)
        hud.set_model_progress(42)
        hud._state_at -= 60.0                                  # a minute of download
        self.assertEqual(hud._visual(), "ready")               # its own visual: it crossfades
        self.assertEqual(hud._busy_label("ready"), ("Getting ready", "speech model · 42%"))
        self.assertEqual(hud._busy_label("transcribing"), ("Getting ready", "speech model · 42%"))
        hud.set_state(ov.TRANSCRIBING)
        self.assertEqual(hud._visual(), "transcribing")
        self.assertEqual(hud._busy_label("transcribing"), ("Transcribing", "Whisper Small"))

    def test_every_show_overlay_starts_clean(self):
        ov, hud = self.ov, self.hud
        self._smart_take("email")
        hud.set_audio_seconds(80)
        hud.show_done(True, kind="email")
        hud._target_size()
        hud.show_overlay(ov.RECORDING)
        self.assertEqual((hud._task_kind, hud._task_detail, hud._writing, hud._ctx, hud._audio_s,
                          hud._done_kind, hud._smart, hud._held_w, hud._busy_last),
                         (None, "", False, {}, None, None, False, None, {}))
        hud.set_state(ov.PROCESSING)
        self.assertEqual(hud._busy_label("processing")[0], "Thinking")

    def test_a_take_warms_the_names_off_the_gui_thread(self):
        # pill_copy.warm imports engine_choice on a worker when a take starts,
        # so neither the stop nor a frame ever waits for that import.
        ov = self.ov
        with mock.patch.object(ov.pill_copy, "warm") as warm:
            self.hud.show_overlay(ov.RECORDING)
        warm.assert_called_once_with()

    def test_within_a_stage_the_capsule_does_not_breathe_in(self):
        ov, hud = self.ov, self.hud
        with mock.patch.object(ov, "QFontMetricsF", _FixedMetrics):
            hud._widths.clear()
            hud.show_overlay(ov.RECORDING)
            hud.set_speech({"speech": "Whisper Small", "speech_local": True})
            hud.set_state(ov.TRANSCRIBING)
            hud.set_audio_seconds(30)
            first, _ = hud._target_size()                      # "Transcribing · Whisper Small"
            hud._state_at -= 4.5                               # "· Almost there": a little shorter
            self.assertEqual(hud._busy_label("transcribing")[1], "Almost there")
            self.assertLess(hud._measure(hud._f_small, "Almost there"),
                            hud._measure(hud._f_small, "Whisper Small"))
            self.assertEqual(hud._target_size()[0], first)
            hud._state_at -= 8.0                               # "Long take · Still transcribing"
            longer, _ = hud._target_size()
            self.assertGreater(longer, first)                  # it grows
            hud.show_done(True)                                # a new stage: its own width
            done_w, _ = hud._target_size()
            # A big change within a stage still moves (the download ring -> words).
            hud.show_overlay(ov.RECORDING)
            hud.set_state(ov.TRANSCRIBING)
            hud.set_partial("hello there, this is a much longer line of live words")
            wide, _ = hud._target_size()
            hud.set_partial("hi")
            narrow, _ = hud._target_size()
            hud._widths.clear()
        self.assertLess(done_w, longer)
        self.assertLess(narrow, wide)

    def test_a_stage_fading_out_keeps_its_words(self):
        ov, hud = self.ov, self.hud
        hud.show_overlay(ov.RECORDING, smart=True)
        hud.set_speech({"speech": "Whisper Small", "speech_local": True})
        hud.set_state(ov.TRANSCRIBING)
        hud.set_audio_seconds(70)
        hud._state_at -= 13.0
        self._frame(0.5)                                       # painted: "Long take · Still transcribing"
        hud.set_task("email", "", self.CLOUD)
        hud.set_state(ov.PROCESSING)
        hud._from_vis, hud._cur_vis, hud._xfade = "transcribing", "processing", 0.1
        hud._from_at = hud._vis_at = time.monotonic()
        seen = []
        real = hud._busy_label
        with mock.patch.object(hud, "_busy_label", side_effect=lambda vis: seen.append(vis) or real(vis)):
            from PySide6.QtGui import QImage
            img = QImage(hud.size(), QImage.Format_ARGB32_Premultiplied)
            hud._show = 1.0
            hud.render(img)
        self.assertNotIn("transcribing", seen)                 # not re-guessed while it fades
        self.assertEqual(hud._busy_last["transcribing"], ("Long take", "Still transcribing"))

    def test_the_capsule_grows_for_longer_words_up_to_the_cap(self):
        ov, hud = self.ov, self.hud
        with mock.patch.object(ov, "QFontMetricsF", _FixedMetrics):
            hud._widths.clear()
            self._smart_take(None)
            thinking, _ = hud._target_size()
            self._smart_take("translate", "Portuguese")
            portuguese, h = hud._target_size()
            self._smart_take("translate", "Portuguese", {"writer": "A very long writer name " * 6,
                                                         "writer_local": True})
            capped, _ = hud._target_size()
            hud.show_done(False, kind="polish")
            done_w, _ = hud._target_size()
            hud._widths.clear()
        self.assertGreater(portuguese, thinking)
        self.assertEqual(h, float(ov.PILL_H))
        self.assertEqual(capped, float(ov.PILL_MAX_W))
        self.assertLessEqual(done_w, float(ov.PILL_MAX_W))

    # ── the icons ──
    def _frame(self, t, motion=True, theme="dark"):
        from PySide6.QtGui import QImage
        from PySide6.QtCore import Qt
        hud = self.hud
        hud._theme, hud._motion = theme, motion
        hud._show, hud._xfade, hud._from_vis = 1.0, 1.0, None
        hud._cur_vis = hud._visual()
        hud._vis_at = time.monotonic() - t
        hud._pw, hud._ph = hud._target_size()
        img = QImage(hud.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        hud.render(img)
        return img

    def _icon_slot(self, img):
        from PySide6.QtCore import QRect
        ov, hud = self.ov, self.hud
        cap = hud._capsule(float(round(hud._pw)), float(round(hud._ph)))
        return img.copy(QRect(int(cap.left() + ov.ICON_X - 11), int(cap.center().y() - 11), 22, 22))

    def _cases(self):
        ov, hud = self.ov, self.hud

        def transcribing(smart):
            hud.show_overlay(ov.RECORDING, smart=smart)
            hud.set_state(ov.TRANSCRIBING)

        def ready(smart):
            transcribing(smart)
            hud.set_state(ov.GETTING_READY)
            hud.set_model_progress(None)

        def ready_pct(smart):
            ready(smart)
            hud.set_model_progress(42)

        def writing(kind, detail=""):
            def setup(smart):
                self._smart_take(kind, detail)
                hud._smart = smart
            return setup

        def done(kind):
            def setup(smart):
                writing(kind)(smart)
                hud.show_done(True, kind=kind)
            return setup

        cases = [("rec", lambda smart: hud.show_overlay(ov.RECORDING, smart=smart)),
                 ("transcribing", transcribing), ("ready", ready), ("ready 42%", ready_pct),
                 ("words", lambda smart: (transcribing(smart), hud.set_partial("hello there"))),
                 ("done", done(None)), ("done email", done("email")),
                 ("error", lambda smart: hud.show_error("Microphone error: device unavailable"))]
        cases += [("writing %s" % k, writing(k, "Spanish" if k == "translate" else ""))
                  for k in (None, "email", "list", "translate", "summary", "polish", "notes", "reply")]
        cases += [("message", writing("message"))]           # the reply's bubble: not in the icon check
        return cases

    def test_every_case_paints_in_both_themes_with_and_without_motion(self):
        ov = self.ov
        for name, setup in self._cases():
            for smart in (True, False):
                for motion in (True, False):
                    for theme in ("dark", "light"):
                        for t in (0.05, 0.7, 1.9, 4.6):
                            setup(smart)
                            img = self._frame(t, motion, theme)
                            ph = self.hud._ph
                            centre = img.pixelColor(ov.WIN_W // 2, int(ov.WIN_H - ov.BOTTOM_PAD - ph / 2))
                            where = (name, smart, motion, theme, t)
                            self.assertGreater(centre.alpha(), 200, where)
                            self.assertEqual(img.pixelColor(2, 2).alpha(), 0, where)

    def test_every_handoff_crossfades_cleanly(self):
        # Mid-crossfade both visuals paint, each on its own clock and words
        # (a paint exception would take the whole app down).
        from PySide6.QtGui import QImage
        from PySide6.QtCore import Qt
        ov, hud = self.ov, self.hud

        def ready():
            hud.show_overlay(ov.RECORDING)
            hud.set_state(ov.TRANSCRIBING)
            hud.set_state(ov.GETTING_READY)
            hud.set_model_progress(42)

        def frame(t):
            img = QImage(hud.size(), QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            hud._show = 1.0
            hud._vis_at = time.monotonic() - t
            hud._from_at = hud._vis_at - 1.0
            hud.render(img)
            return img

        handoffs = [("ready", ready, lambda: hud.set_state(ov.TRANSCRIBING))]
        for kind in (None,) + tuple(__import__("pill_copy").KINDS):
            handoffs.append(("transcribing", lambda k=kind: self._smart_take(k) or hud.set_state(ov.TRANSCRIBING),
                             lambda: hud.set_state(ov.PROCESSING)))
            handoffs.append(("processing", lambda k=kind: self._smart_take(k),
                             lambda k=kind: hud.show_done(False, kind=k)))
        handoffs.append(("processing", lambda: self._smart_take("email"),
                         lambda: hud.show_error("The writer timed out")))
        for theme in ("dark", "light"):
            for motion in (True, False):
                for old, before, after in handoffs:
                    before()
                    hud._theme, hud._motion = theme, motion
                    hud._cur_vis, hud._from_vis, hud._xfade = hud._visual(), None, 1.0
                    hud._pw, hud._ph = hud._target_size()
                    frame(0.6)                                  # the old stage, painted
                    self.assertEqual(hud._cur_vis, old)
                    after()
                    hud._from_vis, hud._cur_vis = hud._cur_vis, hud._visual()
                    self.assertNotEqual(hud._from_vis, hud._cur_vis)
                    for x in (0.1, 0.3, 0.6):
                        hud._xfade = x
                        img = frame(0.05)
                        centre = img.pixelColor(ov.WIN_W // 2, int(ov.WIN_H - ov.BOTTOM_PAD - hud._ph / 2))
                        self.assertGreater(centre.alpha(), 200, (old, hud._cur_vis, theme, motion, x))

    def test_each_case_has_its_own_icon(self):
        icons = {}
        for name, setup in self._cases():
            if name.startswith(("writing", "transcribing", "ready")):
                setup(True)
                icons[name] = self._icon_slot(self._frame(0.5, motion=False))
        names = sorted(icons)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                self.assertNotEqual(icons[a], icons[b], (a, b))

    def test_without_motion_the_icon_stands_still(self):
        for kind in (None, "email", "list", "translate", "summary", "polish", "notes", "reply"):
            self._smart_take(kind)
            a = self._frame(0.4, motion=False)
            b = self._frame(1.3, motion=False)
            self.assertEqual(self._icon_slot(a), self._icon_slot(b), kind)
            self.assertEqual(a, b, kind)                       # the words too (no shimmer)
            moving = [self._icon_slot(self._frame(t)) for t in (0.3, 0.9, 1.5)]
            self.assertTrue(moving[0] != moving[1] or moving[1] != moving[2], kind)

    def test_smart_work_is_purple_and_dictation_is_not(self):
        ov, hud = self.ov, self.hud
        for theme in ("dark", "light"):
            hud.show_overlay(ov.RECORDING)
            hud.set_state(ov.TRANSCRIBING)
            hud._theme = theme
            self.assertEqual(self._purple_pixels(), 0, theme)
            for kind in (None, "email", "list"):
                self._smart_take(kind)
                hud._theme = theme
                self.assertGreater(self._purple_pixels(), 10, (theme, kind))
            hud.show_overlay(ov.RECORDING, smart=True)          # a Smart take, transcribing
            hud.set_state(ov.TRANSCRIBING)
            hud._theme = theme
            self.assertGreater(self._purple_pixels(), 10, theme)

    def test_every_state_paints_a_capsule_on_a_clear_window(self):
        ov = self.ov
        states = [
            lambda: self.hud.show_overlay(ov.RECORDING),
            lambda: self.hud.set_state(ov.TRANSCRIBING),
            lambda: self.hud.set_partial("hello world, this is a test"),
            lambda: self.hud.set_state(ov.PROCESSING),
            lambda: self.hud.show_done(True),
            lambda: self.hud.show_done(False),
            lambda: self.hud.show_error("Microphone error: device unavailable"),
            lambda: self.hud.show_overlay(ov.RECORDING, smart=True),     # a Smart Action take
            lambda: self.hud.set_state(ov.PROCESSING),
        ]
        for theme in ("dark", "light"):
            self.hud._theme = theme
            for step in states:
                step()
                centre, corner = self._render()
                self.assertGreater(centre.alpha(), 200, (theme, self.hud._visual()))
                self.assertEqual(corner.alpha(), 0, (theme, self.hud._visual()))


class TestGlassHelpers(unittest.TestCase):
    def test_helpers_are_safe_everywhere(self):
        from ui import glass
        self.assertIsInstance(glass.animations_enabled(), bool)
        self.assertIsInstance(glass.foreground_monitor_name(), str)


if __name__ == "__main__":
    unittest.main()
