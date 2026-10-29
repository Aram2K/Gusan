"""The loading card's window (splash_window.py), offscreen: its look is the app's one
light look whatever the OS is set to, it sits where the app's window opens, at every
display scale, and the real card process starts, ends on the app's word and goes
with the app. Nothing is shown on a screen: QT_QPA_PLATFORM=offscreen (tests_qt/conftest.py).
"""
import io
import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
import time
from unittest import mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication

import splash_window as sw
import startup_splash as ss
from tests_qt.conftest import os_dark_palette, os_light_palette

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k != "QT_SCALE_FACTOR"}
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _lerp(a, b, f):
    return tuple(a[i] + (b[i] - a[i]) * f for i in range(len(a)))


# ── The look: fixed light tokens ─────────────────────────────────────────────
def test_the_colours_are_the_themes_tokens():
    from ui.theme import TOKENS, qcolor

    def rgb(name):
        c = qcolor(name)
        return (c.red(), c.green(), c.blue())
    assert sw.INK == rgb("ink")
    assert sw.MUTED == rgb("muted")
    assert sw.BLUE == rgb("blue")
    assert sw.BLUE_L == rgb("blue-l")
    assert sw.GLASS_TOP[:3] == rgb("glass-a")          # the plate is the glass tokens' colours, denser
    assert sw.GLASS_BOTTOM[:3] == rgb("glass-b")
    assert sw.GLASS_TOP[3] > qcolor("glass-a").alpha() and sw.GLASS_BOTTOM[3] > qcolor("glass-b").alpha()
    assert TOKENS["ink"] == "#0f172a"


def test_the_card_is_light_glass_with_the_tokens_colours(qapp):
    img = sw.render(sw.SplashArt(), 2.0)
    card_x, card_y, card_w, card_h = sw.CARD
    for x, y in ((80, 290), (280, 290), (80, 70), (280, 70)):      # inside the card, clear of the content
        got = img.pixelColor(x, y)
        f = (y - card_y) / card_h
        want = _lerp(sw.GLASS_TOP, sw.GLASS_BOTTOM, f)
        assert abs(got.red() - want[0]) <= 4 and abs(got.green() - want[1]) <= 4 and abs(got.blue() - want[2]) <= 4, (x, y, got.getRgb(), want)
        assert abs(got.alpha() - want[3]) <= 4, (x, y, got.getRgb(), want)
        assert min(got.red(), got.green(), got.blue()) >= 236          # light, never grey or dark


def test_a_dark_os_changes_nothing(qapp):
    qapp.setPalette(os_light_palette())
    light = sw.render(sw.SplashArt(), 2.0)
    qapp.setPalette(os_dark_palette())
    try:
        dark = sw.render(sw.SplashArt(), 2.0)
    finally:
        qapp.setPalette(os_light_palette())
    assert bytes(light.constBits()) == bytes(dark.constBits())


def test_it_starts_empty_and_ends_empty(qapp):
    art = sw.SplashArt()
    assert sw.render(art, 0.0).pixelColor(180, 180).alpha() == 0           # nothing before it appears
    assert sw.render(art, 2.0, 1.0).pixelColor(180, 180).alpha() == 0      # nothing after its way out
    assert sw.render(art, 2.0).pixelColor(180, 180).alpha() > 200


def test_it_moves_but_stays_inside_the_window(qapp):
    art = sw.SplashArt()
    a, b = sw.render(art, 2.0), sw.render(art, 2.9)
    assert bytes(a.constBits()) != bytes(b.constBits())                    # the rings and the bars move
    for t in (0.1, 0.5, 1.7, 3.1):
        img = sw.render(art, t)
        assert all(img.pixelColor(x, y).alpha() == 0 for x, y in ((2, 2), (357, 2), (2, 357), (357, 357)))


def test_with_animations_switched_off_the_card_stands_still(qapp):
    art = sw.SplashArt(motion=False)
    a, b, c = sw.render(art, 0.5), sw.render(art, 1.7), sw.render(art, 3.1)
    assert bytes(a.constBits()) == bytes(b.constBits()) == bytes(c.constBits())       # nothing moves
    assert a.pixelColor(180, 180).alpha() > 200                                      # and it is all there at once
    assert sw.render(art, 0.0).pixelColor(180, 180).alpha() == 0
    assert sw.render(art, 2.0, 1.0).pixelColor(180, 180).alpha() == 0
    moving = sw.SplashArt()
    assert bytes(sw.render(moving, 1.7).constBits()) != bytes(sw.render(moving, 3.1).constBits())


def test_the_still_card_is_final_from_the_end_of_its_fade_in(qapp):
    art = sw.SplashArt(motion=False)
    assert sw.SplashArt.STILL_IN_S == 0.18
    at, later = sw.render(art, sw.SplashArt.STILL_IN_S), sw.render(art, 3.1)
    assert bytes(at.constBits()) == bytes(later.constBits())             # nothing changes after it: no repaint is needed
    before = sw.render(art, sw.SplashArt.STILL_IN_S - 0.05)
    assert bytes(before.constBits()) != bytes(later.constBits())          # ...but the fade in is still going before it


def _delta(a, b):
    return sum(abs(x - y) for x, y in zip(bytes(a.constBits()), bytes(b.constBits())))


def test_a_ring_is_born_from_nothing_not_in_one_frame(qapp):
    # Each ring used to appear at its peak in a single frame (a tick every 0.8 s, 5x a normal frame).
    art = sw.SplashArt()
    dt = 1.0 / 60.0
    steady = max(_delta(sw.render(art, t), sw.render(art, t + dt)) for t in (1.7, 2.9, 3.3, 4.1))
    for k in range(1, 5):
        born = sw.SplashArt.FIRST_RING_S + k * sw.SplashArt.RING_EVERY_S
        step = _delta(sw.render(art, born - dt / 2), sw.render(art, born + dt / 2))
        assert step < 1.6 * steady, (k, step, steady)


def test_the_way_out_dissolves_it_never_ends_in_a_step(qapp):
    assert sw.smoothstep(0.0) == 0.0 and sw.smoothstep(1.0) == 1.0
    assert sw.smoothstep(0.5) == pytest.approx(0.5)
    # painted at the card's own frame rate, from out = 0 until faded_out(): the last visible frame is faint
    # and no step is large
    frames = [i * sw.FRAME_MS / 1000.0 / ss.FADE_S for i in range(int(ss.FADE_S * 1000 // sw.FRAME_MS) + 1)]
    frames = [o for o in frames if o < 1.0]
    opacity = [1.0 - sw.smoothstep(o) for o in frames]
    assert opacity[-1] < 0.12, opacity
    steps = [a - b for a, b in zip(opacity, opacity[1:] + [0.0])]
    assert max(steps) < 0.35, steps
    art = sw.SplashArt()
    last = sw.render(art, 2.0, frames[-1]).pixelColor(80, 290).alpha()
    assert 0 < last < 0.15 * sw.render(art, 2.0).pixelColor(80, 290).alpha()
    assert sw.render(art, 2.0, 1.0).pixelColor(80, 290).alpha() == 0
    assert not hasattr(sw, "ease_in_quad")


def test_the_name_is_drawn_once_before_the_clock_starts_so_the_intro_never_freezes(qapp):
    # The first text a process draws costs hundreds of ms (font database, engine, glyphs): that is paid in
    # prepare(), on a throw-away image, not at t = 0.30 s in the middle of the intro.
    art = sw.SplashArt()
    drawn = []
    real = art._draw_name
    art._draw_name = lambda p, top: (drawn.append(top), real(p, top))
    art.prepare(1.0)
    assert drawn == [0.0]
    sw.render(art, 0.5)
    assert len(drawn) == 2                                              # ...and every frame draws it as before
    drawn.clear()
    art.prepare(1.0)                                                    # a layer already made is not made twice
    assert drawn == []


def test_it_is_crisp_at_other_pixel_ratios(qapp):
    art = sw.SplashArt()
    for dpr in (1.25, 1.5, 2.0):
        img = sw.render(art, 2.0, 0.0, dpr)
        assert img.width() == int(-(-sw.WIN * dpr // 1)) and img.devicePixelRatio() == dpr
        assert img.pixelColor(int(180 * dpr), int(300 * dpr)).alpha() > 200


def test_there_are_no_sentences_on_it():
    src = open(os.path.join(ROOT, "splash_window.py"), encoding="utf-8").read()
    assert src.count("drawText") == 1 and "APP_NAME" in src             # the name only


# ── The window ────────────────────────────────────────────────────────────────
def test_the_window_is_in_nobodys_way(qapp):
    w = sw.SplashWindow()
    flags = w.flags()
    for flag in (Qt.FramelessWindowHint, Qt.WindowStaysOnTopHint, Qt.WindowDoesNotAcceptFocus,
                 Qt.WindowTransparentForInput):
        assert flags & flag, flag
    assert (flags & Qt.WindowType_Mask) == Qt.Tool                         # no taskbar button (as the pill)
    assert w.format().alphaBufferSize() == 8


def test_it_sits_on_the_centre_of_the_primary_screens_work_area(qapp):
    w = sw.SplashWindow()
    w.appear()
    qapp.processEvents()
    avail = QGuiApplication.primaryScreen().availableGeometry()
    assert abs(w.geometry().center().x() - avail.center().x()) <= 1
    assert abs(w.geometry().center().y() - avail.center().y()) <= 1
    assert (w.width(), w.height()) == (sw.WIN, sw.WIN)
    w.close()


def test_the_intro_starts_when_the_first_frame_is_really_painted(qapp):
    # show() takes tens of ms on Windows (the window and its layered surface): the clock restarts at the first
    # paint, so the first frame the user sees is the start of the intro, not 10-15 % into it.
    clock = [10.0]
    w = sw.SplashWindow(clock=lambda: clock[0])
    w.appear()
    assert w.t0 == 10.0                           # the stamp that says it appeared (the fallback)
    clock[0] = 10.12
    qapp.processEvents()                          # the first paint
    assert w.t0 == 10.12
    clock[0] = 10.5
    w.update()
    qapp.processEvents()
    assert w.t0 == 10.12                          # only the first
    w.close()


def test_the_window_paints_and_fades(qapp):
    clock = [10.0]
    w = sw.SplashWindow(clock=lambda: clock[0])
    w.appear()
    for t in (0.0, 0.1, 0.4, 1.0):
        clock[0] = 10.0 + t
        w.update()
        qapp.processEvents()
    assert not w.fading and not w.faded_out()
    w.begin_fade()
    assert w.fading and not w.faded_out()
    clock[0] += ss.FADE_S + 0.01
    assert w.faded_out()
    w.close()


# ── What the card does on each tick (make_step), with a fake clock ─────────────
class FakeApp:
    def __init__(self):
        self.quits = 0

    def quit(self):
        self.quits += 1


class FakeLife:
    def __init__(self):
        self.verdict = ss.RUN

    def poll(self):
        return self.verdict


class Ticks:
    """A SplashWindow on a fake clock, with its update() and step_down() counted."""

    def __init__(self, qapp, motion=True, show_at=None):
        self.qapp, self.now = qapp, [100.0]
        self.app, self.life = FakeApp(), FakeLife()
        self.win = sw.SplashWindow(sw.SplashArt(motion), clock=lambda: self.now[0])
        self.updates = self.step_downs = 0
        self.win.update = self._update
        self.win.step_down = self._step_down
        self.step = sw.make_step(self.app, self.win, self.life, show_at, motion)

    def _update(self):
        self.updates += 1

    def _step_down(self):
        self.step_downs += 1

    def tick(self, at):
        self.now[0] = 100.0 + at
        self.step()

    def close(self):
        self.win.close()


def test_a_window_that_is_up_before_the_card_is_old_enough_waits_then_fades(qapp):
    # The card's own start takes 1.4-2.4 s, so it can appear just before the app's window does: that would
    # be a 200 ms blink of a half-formed card. It is seen for a whole beat first.
    k = Ticks(qapp)
    try:
        k.tick(0.0)                                         # appears
        assert k.win.t0 is not None and not k.win.fading
        k.life.verdict = ss.FADE
        k.tick(0.05)
        assert not k.win.fading and k.app.quits == 0         # too young: it keeps painting
        k.tick(sw.MIN_SHOWN_S - 0.01)
        assert not k.win.fading and k.app.quits == 0
        assert k.updates >= 3                                # ...and keeps painting
        k.tick(sw.MIN_SHOWN_S + 0.001)
        assert k.win.fading and k.app.quits == 0             # the fade begins (the verdict is sticky)
        k.tick(sw.MIN_SHOWN_S + 0.001 + ss.FADE_S + 0.01)
        assert k.app.quits == 1
    finally:
        k.close()


def test_a_card_that_never_showed_is_not_shown_for_a_window_that_is_already_up(qapp):
    k = Ticks(qapp, show_at=time.time() + 3600)             # not yet time to appear
    try:
        k.tick(0.0)
        assert k.win.t0 is None and k.app.quits == 0
        k.life.verdict = ss.FADE
        k.tick(0.1)
        assert k.win.t0 is None and k.app.quits == 1         # no flash
    finally:
        k.close()


def test_an_app_that_is_gone_ends_even_a_young_card_at_once(qapp):
    k = Ticks(qapp)
    try:
        k.tick(0.0)
        k.life.verdict = ss.QUIT
        k.tick(0.05)
        assert k.app.quits == 1
    finally:
        k.close()


def test_the_card_leaves_the_always_on_top_band_once_after_a_while(qapp):
    # A start that fails shows the bootloader's own error box and the process stays alive: the card must not
    # cover it for the 60 s of the hard timeout.
    k = Ticks(qapp)
    try:
        k.tick(0.0)
        k.tick(sw.TOPMOST_S - 0.1)
        assert k.step_downs == 0
        k.tick(sw.TOPMOST_S + 0.001)
        assert k.step_downs == 1
        for at in (sw.TOPMOST_S + 1, 30.0, 59.0):
            k.tick(at)
        assert k.step_downs == 1
        assert k.app.quits == 0                              # the hard timeout (60 s) is not changed
        assert ss.HARD_TIMEOUT_S == 60.0 and sw.TOPMOST_S < ss.HARD_TIMEOUT_S
    finally:
        k.close()


def test_step_down_asks_windows_for_the_bottom_of_the_z_order_and_is_a_no_op_elsewhere(qapp):
    import ctypes
    w = sw.SplashWindow()
    with mock.patch.object(ctypes, "windll", create=True) as windll:
        with mock.patch.object(sw.sys, "platform", "linux"):
            w.step_down()
        windll.user32.SetWindowPos.assert_not_called()
        with mock.patch.object(sw.sys, "platform", "win32"):
            w.step_down()
        # HWND_BOTTOM (not HWND_NOTOPMOST, which would stay above a dialog), no size, no move, no activation
        windll.user32.SetWindowPos.assert_called_once_with(int(w.winId()), 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        windll.user32.SetWindowPos.side_effect = OSError("no")
        with mock.patch.object(sw.sys, "platform", "win32"):
            w.step_down()                                    # a failing call changes nothing
    w.step_down()                                            # the real thing, offscreen: no error


def test_a_card_that_stands_still_stops_repainting_and_resumes_for_its_way_out(qapp):
    k = Ticks(qapp, motion=False)
    try:
        for at in (0.0, 0.05, 0.10, 0.15):
            k.tick(at)
        before = k.updates
        k.tick(0.20)                                         # a paint at t >= 0.18 s: the still card is final
        assert k.updates == before + 1
        for at in (0.25, 0.5, 1.0, 5.0, 30.0):
            k.tick(at)
        assert k.updates == before + 1, "nothing changes, so nothing is repainted"
        k.life.verdict = ss.FADE
        k.tick(31.0)
        assert k.win.fading
        n = k.updates
        k.tick(31.0 + ss.FADE_S / 2)
        assert k.updates == n + 1                            # the way out is painted again
        k.tick(31.0 + ss.FADE_S + 0.01)
        assert k.app.quits == 1
    finally:
        k.close()


def test_a_card_that_moves_repaints_on_every_tick(qapp):
    k = Ticks(qapp, motion=True)
    try:
        for i, at in enumerate((0.0, 0.1, 0.2, 1.0, 5.0, 30.0), start=1):
            k.tick(at)
            assert k.updates == i
    finally:
        k.close()


def test_the_timer_runs_at_30_fps(qapp):
    assert sw.FRAME_MS == 33


SCALE_PROBE = textwrap.dedent('''
    import json, os, sys
    sys.path.insert(0, %(root)r)
    from PySide6.QtGui import QColor, QGuiApplication, QPalette
    app = QGuiApplication([])
    pal = QPalette()                                  # a dark Windows: set before the card is built
    for role, col in ((QPalette.Window, "#2b2b2b"), (QPalette.WindowText, "#ffffff"), (QPalette.Base, "#1f1f1f"),
                      (QPalette.Text, "#ffffff"), (QPalette.Button, "#333333"), (QPalette.ButtonText, "#ffffff")):
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    import splash_window as sw
    w = sw.SplashWindow()
    w.appear()
    app.processEvents()
    dpr = w.devicePixelRatio()
    avail = QGuiApplication.primaryScreen().availableGeometry()
    g = w.geometry()
    img = sw.render(sw.SplashArt(), 2.0, 0.0, dpr)
    c = img.pixelColor(int(80 * dpr), int(290 * dpr))
    print(json.dumps({"dpr": dpr, "avail": [avail.center().x(), avail.center().y()],
                      "win": [g.center().x(), g.center().y()], "size": [g.width(), g.height()],
                      "fits": avail.width() >= g.width() and avail.height() >= g.height(),
                      "px": [c.red(), c.green(), c.blue(), c.alpha()], "side": img.width()}))
''')


@pytest.mark.parametrize("scale", ["1", "1.25", "1.5"])
def test_at_every_display_scale_it_is_centred_and_light_whatever_the_os_palette(scale):
    out = subprocess.run([sys.executable, "-c", SCALE_PROBE % {"root": ROOT}], capture_output=True, text=True,
                         env=_env(QT_SCALE_FACTOR=scale), timeout=120)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["dpr"] == pytest.approx(float(scale))
    assert got["size"] == [sw.WIN, sw.WIN]                       # logical size: the same on every display
    assert got["side"] == int(-(-sw.WIN * float(scale) // 1))     # drawn in device pixels
    assert got["fits"]
    assert abs(got["win"][0] - got["avail"][0]) <= 1 and abs(got["win"][1] - got["avail"][1]) <= 1
    want = _lerp(sw.GLASS_TOP, sw.GLASS_BOTTOM, (290 - sw.CARD[1]) / sw.CARD[3])
    assert all(abs(got["px"][i] - want[i]) <= 4 for i in range(4)), (got["px"], want)


# ── The real card process (main.py --splash-worker) ───────────────────────────
# A cold machine needs many seconds to start a Qt process (and a loaded one even more), so no test
# here sleeps for a guess: the card says "shown" when its window is on screen and the bounds are
# measured from that moment. main.py's own --splash-worker dispatch is still what runs (through runpy).
CARD_DRIVER = textwrap.dedent('''
    import os, runpy, sys
    root, parent, started = sys.argv[1:4]
    sys.path.insert(0, root)
    import splash_window as sw
    _appear = sw.SplashWindow.appear
    def appear(self):
        _appear(self)
        print("shown", flush=True)
    sw.SplashWindow.appear = appear
    main = os.path.join(root, "main.py")
    sys.argv = [main, "--splash-worker", parent, started]
    runpy.run_path(main, run_name="__main__")
''')


def _lines(stream):
    """A process's output lines on a queue (None at the end of the stream), read on a daemon thread."""
    q = queue.Queue()

    def pump():
        try:
            for line in iter(stream.readline, ""):
                q.put(line.strip())
        except (OSError, ValueError):
            pass
        q.put(None)
    threading.Thread(target=pump, daemon=True).start()
    return q


def wait_shown(q, ceiling=60):
    """Return when the card has put its window on screen; fail if its output ends first."""
    end = time.time() + ceiling
    while True:
        try:
            item = q.get(timeout=max(0.1, end - time.time()))
        except queue.Empty:
            pytest.fail("the card never showed within %s s" % ceiling)
        if item == "shown":
            return
        assert item is not None, "the card ended before it showed"


def _card(parent_pid=None, started=None, shown=False):
    """The card process. With ``shown`` its stdout is a pipe and the queue of its lines is returned too."""
    started = time.time() if started is None else started
    if not shown:
        return subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "main.py"), ss.WORKER_FLAG, str(parent_pid or os.getpid()), repr(started)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_env(), cwd=ROOT)
    card = subprocess.Popen([sys.executable, "-c", CARD_DRIVER, ROOT, str(parent_pid or os.getpid()), repr(started)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_env(), cwd=ROOT)
    return card, _lines(io.TextIOWrapper(card.stdout, encoding="utf-8"))


def test_the_card_process_fades_out_and_ends_when_the_app_says_the_window_is_up():
    card, lines = _card(shown=True)
    try:
        wait_shown(lines)
        assert card.poll() is None
        t0 = time.time()
        card.stdin.write(ss.CLOSE)
        card.stdin.flush()
        card.stdin.close()
        assert card.wait(20) == 0
        assert time.time() - t0 < 6
    finally:
        card.kill()


def test_the_card_process_ends_when_the_app_closes_the_pipe():
    card, lines = _card(shown=True)
    try:
        wait_shown(lines)
        assert card.poll() is None
        card.stdin.close()
        assert card.wait(20) == 0
    finally:
        card.kill()


def test_a_card_told_the_window_is_up_before_it_ever_showed_never_shows():
    # The duplicate launch / the quick start: CLOSE arrives while the card is still starting. It only
    # has to end long before the 60 s hard timeout; it does not depend on the card having shown.
    card = _card()
    try:
        card.stdin.write(ss.CLOSE)
        card.stdin.flush()
        card.stdin.close()
        assert card.wait(60) == 0
    finally:
        card.kill()


APP = textwrap.dedent('''
    import os, subprocess, sys, time
    card = subprocess.Popen([sys.executable, "-c", %(driver)r, %(root)r, str(os.getpid()), repr(time.time())],
                            stdin=subprocess.PIPE, env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    print(card.pid, flush=True)
    time.sleep(120)
''')


@pytest.mark.skipif(sys.platform != "win32", reason="the card's process handle is Windows' own")
def test_the_card_process_goes_with_a_killed_app():
    # The card inherits the app's stdout: the app prints the card's pid, then the card prints "shown".
    app = subprocess.Popen([sys.executable, "-c", APP % {"driver": CARD_DRIVER, "root": ROOT}],
                           stdout=subprocess.PIPE, text=True, encoding="utf-8")
    lines = _lines(app.stdout)
    pid = None
    try:
        pid = int(lines.get(timeout=60))
        wait_shown(lines)
        assert ss.ParentWatch(pid).alive(), "the card should be running"
        t0 = time.time()
        app.kill()                                  # TerminateProcess: "End task" / a frozen window closed by Windows
        while ss.ParentWatch(pid).alive() and time.time() - t0 < 10:
            time.sleep(0.1)
        assert time.time() - t0 < 3.0, "the card stayed on screen after its app was killed"
    finally:
        app.kill()
        if pid is not None:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
