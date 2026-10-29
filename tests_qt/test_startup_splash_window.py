"""The loading card's window (splash_window.py), offscreen: its look is the app's one
light look whatever the OS is set to, it sits where the app's window opens, at every
display scale, and the real card process starts, ends on the app's word and goes
with the app. Nothing is shown on a screen: QT_QPA_PLATFORM=offscreen (tests_qt/conftest.py).
"""
import json
import os
import subprocess
import sys
import textwrap
import time

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
def _card(parent_pid=None, started=None):
    started = time.time() if started is None else started
    return subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "main.py"), ss.WORKER_FLAG, str(parent_pid or os.getpid()), repr(started)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=_env(), cwd=ROOT)


def test_the_card_process_fades_out_and_ends_when_the_app_says_the_window_is_up():
    card = _card()
    try:
        time.sleep(3.0)
        assert card.poll() is None, card.stderr.read()
        t0 = time.time()
        card.stdin.write(ss.CLOSE)
        card.stdin.flush()
        card.stdin.close()
        assert card.wait(20) == 0
        assert time.time() - t0 < 6
    finally:
        card.kill()


def test_the_card_process_ends_when_the_app_closes_the_pipe():
    card = _card()
    try:
        time.sleep(3.0)
        assert card.poll() is None
        card.stdin.close()
        assert card.wait(20) == 0
    finally:
        card.kill()


def test_a_card_told_the_window_is_up_before_it_ever_showed_never_shows():
    # The duplicate launch / the quick start: CLOSE arrives while the card is still starting.
    card = _card()
    try:
        card.stdin.write(ss.CLOSE)
        card.stdin.flush()
        card.stdin.close()
        assert card.wait(30) == 0
    finally:
        card.kill()


APP = textwrap.dedent('''
    import os, subprocess, sys, time
    card = subprocess.Popen([sys.executable, os.path.join(%(root)r, "main.py"), "--splash-worker",
                             str(os.getpid()), repr(time.time())], stdin=subprocess.PIPE,
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    print(card.pid, flush=True)
    time.sleep(120)
''')


@pytest.mark.skipif(sys.platform != "win32", reason="the card's process handle is Windows' own")
def test_the_card_process_goes_with_a_killed_app():
    app = subprocess.Popen([sys.executable, "-c", APP % {"root": ROOT}], stdout=subprocess.PIPE, text=True)
    pid = int(app.stdout.readline())
    try:
        time.sleep(3.0)
        assert ss.ParentWatch(pid).alive(), "the card should be running"
        t0 = time.time()
        app.kill()                                  # TerminateProcess: "End task" / a frozen window closed by Windows
        while ss.ParentWatch(pid).alive() and time.time() - t0 < 10:
            time.sleep(0.1)
        assert time.time() - t0 < 3.0, "the card stayed on screen after its app was killed"
    finally:
        app.kill()
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
