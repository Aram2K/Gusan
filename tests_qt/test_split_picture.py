"""The "You see / They see" picture of the Gusan 2.0 Live Assistant hero
(ui/pages/live_assistant.py, class ScreensPicture): ONE monitor split by a
draggable line that also slides by itself.

Nothing here depends on the wall clock. The state machine is driven directly
(_timer_fired, _glide, the animation's setCurrentTime), the motion tests give
every timer an absurdly long interval so nothing fires behind their back, and
the "may it play" inputs (application state, Windows animation effects, the
visible region) are monkeypatched. Mouse events are built by hand and sent
straight to the widget.
"""
import math
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QImage, QKeyEvent, QMouseEvent, QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import live_context
from ui.pages import live_assistant as la

PIC = la.ScreensPicture

# ── statuses (live_context.hide_status, as the page builds them) ──
_STATUS_ARGS = {
    "on": (True, dict(supported=True, remote=False, mac=False)),
    "partial": (True, dict(supported=False, remote=False, mac=True, macos26=False)),
    "off": (False, dict(supported=True, remote=False, mac=False)),
    "failed": (True, dict(supported=True, remote=False, mac=False, excluded=False)),
    "unavailable": (True, dict(supported=True, remote=True, mac=False)),
}
KEYS = list(_STATUS_ARGS)
VISIBLE = ("off", "failed", "unavailable")                    # the card shows to the call at full strength
SHOWS_CARD = ("partial", "off", "failed", "unavailable")      # the call side shows the card (faded or not)
# The line moves, glides, drags, focuses and takes keys in EVERY status: nothing is parked.


def status(key):
    wanted, kw = _STATUS_ARGS[key]
    st = live_context.hide_status(wanted, **kw)
    assert st.key == key
    return st


# ── fixtures ──
@pytest.fixture(autouse=True)
def calm(qapp, monkeypatch):
    """No motion by itself, the app is active, animations are on."""
    monkeypatch.setattr(PIC, "AUTOPLAY", False)
    monkeypatch.setattr(la, "_animations_on", lambda: True)
    monkeypatch.setattr(la, "QGuiApplication",
                        SimpleNamespace(applicationState=lambda: Qt.ApplicationActive))


@pytest.fixture
def make(qapp):
    made = []

    def _make(key="on", width=462, owner="Aram", show=True):
        pic = PIC()
        pic.set_owner(owner)
        pic.set_status(status(key))
        pic.resize(width, pic.heightForWidth(width))
        made.append(pic)
        if show:
            pic.show()
            QApplication.processEvents()
        return pic

    yield _make
    for pic in made:
        pic.hide()
        pic.deleteLater()
    QApplication.processEvents()


@pytest.fixture
def motion(monkeypatch):
    """AUTOPLAY on, every timer so long that it can only fire when the test says."""
    monkeypatch.setattr(PIC, "AUTOPLAY", True)
    monkeypatch.setattr(PIC, "FIRST_HOLD_MS", 3_000_001)
    monkeypatch.setattr(PIC, "HOLD_MS", 3_000_002)
    monkeypatch.setattr(PIC, "RESUME_MS", 3_000_003)
    monkeypatch.setattr(PIC, "RETRY_MS", 3_000_004)
    monkeypatch.setattr(PIC, "GLIDE_MS", 66_000)
    return PIC


# ── helpers ──
def split_x(pic, f):
    """Widget x of the line at share ``f`` of the glass."""
    s = pic._scale()
    return (la.BEZEL + f * la.INNER_W * la.K) * s


def mid_y(pic):
    return (la.LABEL_H + la.LABEL_GAP + la.BEZEL + la.INNER_H * la.K / 2.0) * pic._scale()


def mouse(pic, kind, x, y, button=Qt.LeftButton, buttons=None):
    if buttons is None:
        buttons = button if kind != QEvent.MouseMove else Qt.NoButton
        if kind == QEvent.MouseButtonRelease:
            buttons = Qt.NoButton
    ev = QMouseEvent(kind, QPointF(x, y), pic.mapToGlobal(QPointF(x, y)), button, buttons, Qt.NoModifier)
    QApplication.sendEvent(pic, ev)
    return ev


def press(pic, f, button=Qt.LeftButton):
    return mouse(pic, QEvent.MouseButtonPress, split_x(pic, f), mid_y(pic), button)


def move(pic, f, dragging=False, y=None):
    return mouse(pic, QEvent.MouseMove, split_x(pic, f), mid_y(pic) if y is None else y,
                 Qt.NoButton, Qt.LeftButton if dragging else Qt.NoButton)


def release(pic, f):
    return mouse(pic, QEvent.MouseButtonRelease, split_x(pic, f), mid_y(pic))


def leave(pic):
    QApplication.sendEvent(pic, QEvent(QEvent.Leave))


def key(pic, k):
    ev = QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier)
    QApplication.sendEvent(pic, ev)
    return ev


def image(pic):
    return pic.grab().toImage().convertToFormat(QImage.Format_RGB32)


def at(img, pic, x, y):
    """Colour at design coordinates (x, y) of the picture."""
    r = img.width() / float(pic.width())
    return img.pixelColor(int(x * pic._scale() * r), int(y * pic._scale() * r))


def slide_rect(x0, y0, x1, y1):
    """A rectangle given in slide units -> design units."""
    left, top = la.BEZEL, la.LABEL_H + la.LABEL_GAP + la.BEZEL
    return (left + x0 * la.K, top + y0 * la.K, left + x1 * la.K, top + y1 * la.K)


# the inside of the card, below its head (title + chip), clear of the line and the handle at either rest
CARD_BODY = slide_rect(150, 82, 255, 150)


def diff(a, b, pic, rect, step=2):
    """Mean absolute RGB difference of two grabs of ``pic`` inside ``rect`` (design units)."""
    x0, y0, x1, y1 = rect
    total = n = 0
    x = x0
    while x < x1:
        y = y0
        while y < y1:
            ca, cb = at(a, pic, x, y), at(b, pic, x, y)
            total += abs(ca.red() - cb.red()) + abs(ca.green() - cb.green()) + abs(ca.blue() - cb.blue())
            n += 1
            y += step
        x += step
    return total / (3.0 * max(1, n))


def grab_at(pic, f):
    pic.set_split(f)
    return image(pic)


# the glass in design units
def glass():
    return (la.BEZEL, la.LABEL_H + la.LABEL_GAP + la.BEZEL,
            la.BEZEL + la.INNER_W * la.K, la.LABEL_H + la.LABEL_GAP + la.BEZEL + la.INNER_H * la.K)


def glass_mid_y():
    _l, top, _r, bottom = glass()
    return (top + bottom) / 2.0


def handle_centre_x(f):
    """Where the handle should be for a line at share ``f``: the line's x, but kept 1.1 * HANDLE_R
    inside the glass at either end."""
    left, _t, right, _b = glass()
    r = la.HANDLE_R * 1.1
    return min(max(left + f * (right - left), left + r), right - r)


def design_x_to_widget(pic, x):
    return x * pic._scale()


def press_at(pic, x, y, button=Qt.LeftButton):
    """A press at design coordinates (x, y)."""
    s = pic._scale()
    return mouse(pic, QEvent.MouseButtonPress, x * s, y * s, button)


def drag_to(pic, x, y=None):
    s = pic._scale()
    return mouse(pic, QEvent.MouseMove, x * s, (glass_mid_y() if y is None else y) * s,
                 Qt.NoButton, Qt.LeftButton)


def release_at(pic, x, y=None):
    s = pic._scale()
    return mouse(pic, QEvent.MouseButtonRelease, x * s, (glass_mid_y() if y is None else y) * s)


def ring(cx, cy, r, n=12):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


class _Spy:
    """Every string QPainter.drawText was given, in order (installed in place of la.QPainter)."""
    texts = []


@pytest.fixture
def drawn_text(monkeypatch):
    from PySide6.QtGui import QPainter

    class Recording(QPainter):
        def drawText(self, *a):
            for v in a:
                if isinstance(v, str):
                    _Spy.texts.append(v)
            return super().drawText(*a)

    _Spy.texts = []
    monkeypatch.setattr(la, "QPainter", Recording)
    return _Spy


def side_diff(img, pic, side, x0, x1):
    """Mean RGB difference between the grab and one cached side ("you" / "they") over design x in
    [x0, x1) of the glass, away from the rounded corners - the pixels of a line parked at an end."""
    left, top, _r, _b = glass()
    pm = pic._cache[side].toImage().convertToFormat(QImage.Format_RGB32)
    ratio_pm = pm.width() / (la.INNER_W * la.K)
    ratio_img = img.width() / float(pic.width()) * pic._scale()
    total = n = 0
    x = x0
    while x < x1:
        y = 14.0
        while y < la.INNER_H * la.K - 14:
            a = img.pixelColor(int((left + x) * ratio_img), int((top + y) * ratio_img))
            b = pm.pixelColor(int(x * ratio_pm), int(y * ratio_pm))
            total += abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())
            n += 1
            y += 3
        x += 3
    return total / (3.0 * max(1, n))


# ═════════════════════════ geometry ═════════════════════════
def test_design_constants():
    assert (la.DESIGN_W, la.DESIGN_H) == (462, 353)
    assert la.DESIGN_W == la.SCREEN_W == int(la.INNER_W * la.K) + 2 * la.BEZEL
    # the line runs the FULL width of the glass: the stops are the ends
    assert (la.SPLIT_MIN, la.SPLIT_MAX) == (0.0, 1.0)
    assert la.STOP_THEY == la.SPLIT_MIN and la.STOP_YOU == la.SPLIT_MAX
    assert (la.STOP_THEY, la.STOP_YOU) == (0.0, 1.0)
    assert la.SPLIT_MIN < la.REST_STATIC < la.SPLIT_MAX           # animations off: both sides show
    assert (PIC.GLIDE_MS, PIC.HOLD_MS, PIC.FIRST_HOLD_MS, PIC.RESUME_MS, PIC.RETRY_MS) == (1900, 2200, 1400, 6000, 1500)
    assert la.STATUS_WORDS == {"on": "Hidden", "partial": "May be hidden"} and la.STATUS_WORD_DEFAULT == "Visible"


def test_size_hint_and_height_for_width(make):
    pic = make()
    assert pic.sizeHint().toTuple() == (la.DESIGN_W, la.DESIGN_H)
    assert pic.hasHeightForWidth()
    assert pic.sizePolicy().hasHeightForWidth()
    assert pic.heightForWidth(462) == 353
    assert pic.heightForWidth(1000) == 353                    # never scales up
    assert pic.heightForWidth(231) == math.ceil(353 / 2.0)    # 0.5 is exactly on a step
    assert pic.heightForWidth(0) == pic.heightForWidth(1) > 0  # a zero width never divides by zero
    heights = [pic.heightForWidth(w) for w in range(100, 470, 10)]
    assert heights == sorted(heights)                         # monotonic
    # the scale snaps DOWN to 1/64, so a drag of the window edge rebuilds the
    # cached images ~25 times and the picture never overflows its column
    s = math.floor(300 / 462.0 * la.SCALE_STEPS) / la.SCALE_STEPS
    assert pic.heightForWidth(300) == math.ceil(353 * s)
    assert 353 * 300 / 462.0 - 353 * s < 353 / float(la.SCALE_STEPS) + 1e-9


def test_minimum_size_hint(make):
    pic = make()
    m = pic.minimumSizeHint()
    assert m.width() == 260 and m.height() == pic.heightForWidth(260)
    assert m.width() < pic.sizeHint().width() and m.height() < pic.sizeHint().height()


def test_scale_follows_width(make):
    pic = make(width=231)
    assert pic._scale() == pytest.approx(0.5)
    pic.resize(900, pic.heightForWidth(900))
    assert pic._scale() == 1.0


# ═════════════════════════ split ═════════════════════════
@pytest.mark.parametrize("k", KEYS)
def test_starts_at_you_stop(make, k):
    assert make(k).split() == la.STOP_YOU == 1.0


@pytest.mark.parametrize("k", KEYS)
def test_set_split_clamps(make, k):
    pic = make(k)
    pic.set_split(-3)
    assert pic.split() == la.SPLIT_MIN
    pic.set_split(7)
    assert pic.split() == la.SPLIT_MAX
    pic.set_split(0.5)
    assert pic.split() == 0.5
    pic.set_split("0.25")                                      # anything float() takes
    assert pic.split() == 0.25
    pic.set_split(la.SPLIT_MIN)
    assert pic.split() == la.SPLIT_MIN == 0.0
    pic.set_split(la.SPLIT_MAX)
    assert pic.split() == la.SPLIT_MAX == 1.0


@pytest.mark.parametrize("k", KEYS)
def test_set_split_is_never_ignored(make, k):
    """Every status takes a new split (the old picture parked the line in off / failed / unavailable)."""
    pic = make(k)
    for f in (0.2, 0.9, 0.0, 1.0, 0.5):
        pic.set_split(f)
        assert pic.split() == f


@pytest.mark.parametrize("k", KEYS)
def test_set_split_updates_only_on_change(make, monkeypatch, k):
    pic = make(k)
    pic.set_split(0.4)                                         # off the end, so 0.5 below is a change
    calls = []
    monkeypatch.setattr(pic, "update", lambda *a: calls.append(a))
    pic.set_split(0.5)
    assert len(calls) == 1
    pic.set_split(0.5)
    pic.set_split(0.5 + 1e-9)                                  # below the 1e-6 threshold
    assert len(calls) == 1
    pic.set_split(0.6)
    assert len(calls) == 2


# ═════════════════════════ status, focus, text ═════════════════════════
@pytest.mark.parametrize("k", KEYS)
def test_status_flags(make, k):
    pic = make(k)
    assert pic.moves() is True                                  # every status: the line moves
    assert pic.they_see_card() is (k != "on")
    assert pic.focusPolicy() == Qt.StrongFocus                  # ... takes keyboard focus ...
    assert pic.split() == la.STOP_YOU                           # ... and waits at the end, not parked mid-glass
    assert pic.hasMouseTracking()


@pytest.mark.parametrize("k", KEYS)
def test_accessible_texts(make, k):
    pic = make(k)
    assert pic.accessibleName() == "You see and They see"
    desc = pic.accessibleDescription()
    if k == "on":
        assert "no card" in desc and "with the card on it" not in desc
    else:
        assert "with the card on it" in desc and "no card" not in desc
    assert desc.endswith("Drag the line, or use the arrow keys, to compare the two.")   # always ends with the hint
    assert "Live Assistant card is on it" in desc               # the "you" side always has it


def test_owner_banner_and_initial(make):
    pic = make(owner="Aram")
    assert pic.owner_banner() == "Aram’s screen"
    pic.set_owner("  maria ")
    assert pic.owner_banner() == "maria’s screen" and pic._initial == "M"
    pic.set_owner("")
    assert pic.owner_banner() == "Shared screen" and pic._initial == "Y"
    pic.set_owner(None)
    assert pic.owner_banner() == "Shared screen"


def test_status_without_a_key_counts_as_hidden(make):
    pic = make()
    pic.set_status(SimpleNamespace(text="Hidden"))
    assert pic.moves() and not pic.they_see_card()
    image(pic)                                                   # paints without icon / tone attributes


# ═════════════════════════ the loop ═════════════════════════
@pytest.mark.parametrize("k", KEYS)
def test_loop_starts_when_shown(make, motion, k):
    pic = make(k)
    assert pic.split() == la.STOP_YOU
    assert pic._timer.isActive() and pic._timer.interval() == PIC.FIRST_HOLD_MS
    assert pic._anim.state() != pic._anim.State.Running
    assert pic.playing()


@pytest.mark.parametrize("k", KEYS)
def test_first_glide_goes_to_they_stop_then_back(make, motion, k):
    """In EVERY status the first slide goes the whole way, 1.0 -> 0.0, then back 0.0 -> 1.0."""
    pic = make(k)
    pic._timer_fired()
    anim = pic._anim
    assert anim.state() == anim.State.Running
    assert anim.startValue() == pytest.approx(1.0) and anim.endValue() == pytest.approx(0.0)
    assert anim.startValue() == pytest.approx(la.STOP_YOU) and anim.endValue() == pytest.approx(la.STOP_THEY)
    assert anim.duration() == pytest.approx(PIC.GLIDE_MS, abs=2)       # full way = GLIDE_MS
    assert anim.easingCurve().type() == anim.easingCurve().Type.InOutCubic
    # half way: the line is in between, nothing else moved
    anim.setCurrentTime(anim.duration() // 2)
    assert la.STOP_THEY < pic.split() < la.STOP_YOU
    assert pic.split() == pytest.approx((la.STOP_YOU + la.STOP_THEY) / 2, abs=0.01)   # symmetric ease
    # arrival: it rests, then the hold timer is armed
    anim.setCurrentTime(anim.duration())
    assert anim.state() != anim.State.Running
    assert pic.split() == pytest.approx(la.STOP_THEY)
    assert pic._timer.isActive() and pic._timer.interval() == PIC.HOLD_MS
    # the next slide goes back
    pic._timer_fired()
    assert anim.startValue() == pytest.approx(la.STOP_THEY) and anim.endValue() == pytest.approx(la.STOP_YOU)
    anim.setCurrentTime(anim.duration())
    assert pic.split() == pytest.approx(la.STOP_YOU)
    assert pic._timer.interval() == PIC.HOLD_MS
    # and the loop goes on
    pic._timer_fired()
    assert anim.endValue() == pytest.approx(la.STOP_THEY)


@pytest.mark.parametrize("k", KEYS)
@pytest.mark.parametrize("start, target", [
    (1.0, 0.0), (0.95, 0.0), (0.80, 0.0), (0.70, 0.0), (0.51, 0.0),     # nearer the right end: go to the left end
    (0.50, 1.0),                                                         # the middle: ties go to the "You see" end
    (0.49, 1.0), (0.40, 1.0), (0.30, 1.0), (0.10, 1.0), (0.0, 1.0),     # nearer the left end: go to the right end
])
def test_far_stop_from_any_split(make, motion, k, start, target):
    pic = make(k)
    pic.set_split(start)
    pic._timer_fired()
    assert pic._anim.endValue() == pytest.approx(target)
    assert pic._anim.startValue() == pytest.approx(pic.split())


@pytest.mark.parametrize("k", KEYS)
@pytest.mark.parametrize("start", [1.0, 0.80, 0.50, 0.20, 0.0])
def test_glide_duration_scales_with_distance_and_has_a_floor(make, motion, k, start):
    pic = make(k)
    pic.set_split(start)
    pic._timer_fired()
    way = abs(pic._anim.endValue() - start)
    assert 0.5 <= way <= 1.0                                    # the far end is always at least half the glass away
    expected = max(500, PIC.GLIDE_MS * way)                     # the stops are 1.0 apart: GLIDE_MS is the whole way
    assert pic._anim.duration() == pytest.approx(expected, abs=2)
    assert pic._anim.duration() >= 500


def test_the_whole_way_takes_glide_ms(make, monkeypatch):
    """With the real GLIDE_MS (1900): end to end is exactly that, half way is half, a short hop hits the floor."""
    monkeypatch.setattr(PIC, "AUTOPLAY", True)
    for name in ("FIRST_HOLD_MS", "HOLD_MS", "RESUME_MS", "RETRY_MS"):
        monkeypatch.setattr(PIC, name, 3_000_000)
    assert PIC.GLIDE_MS == 1900
    pic = make("off")
    pic.set_split(1.0)
    pic._glide(0.0)
    assert pic._anim.duration() == 1900
    pic.set_split(0.0)
    pic._glide(1.0)
    assert pic._anim.duration() == 1900
    pic.set_split(0.5)
    pic._glide(1.0)
    assert pic._anim.duration() == 950
    pic.set_split(0.9)
    pic._glide(1.0)                                              # 0.1 of the way = 190 ms: under the 500 ms floor
    assert pic._anim.duration() == 500
    pic.set_split(1.0 - 500 / 1900.0 - 0.01)
    pic._glide(1.0)                                              # just over the floor
    assert 500 < pic._anim.duration() < 520


@pytest.mark.parametrize("k", KEYS)
def test_short_way_is_500ms_floor(make, motion, k):
    pic = make(k)
    pic.set_split(la.STOP_YOU - 0.02)                          # 0.02 from the near stop (GLIDE_MS is 66000 here)
    pic._glide(la.STOP_YOU)                                    # 66000 * 0.02 = 1320 -> no floor
    assert pic._anim.duration() == pytest.approx(1320, abs=2)
    pic.set_split(la.STOP_YOU - 0.001)
    pic._glide(la.STOP_YOU)                                    # 66 -> the floor
    assert pic._anim.duration() == 500
    pic.set_split(la.STOP_YOU)
    pic._glide(la.STOP_YOU)                                    # no way at all
    assert pic._anim.duration() == 500


@pytest.mark.parametrize("k", KEYS)
def test_the_sweep_covers_the_whole_glass_and_keeps_going(make, motion, k):
    """1.0 -> 0.0 -> 1.0 -> 0.0 ...: the line really visits every part of the glass, every status,
    and arrives exactly on the ends."""
    pic = make(k)
    seen = []
    for expect_end in (0.0, 1.0, 0.0, 1.0):
        pic._timer_fired()
        anim = pic._anim
        assert anim.endValue() == pytest.approx(expect_end)
        way = []
        for i in range(0, 11):
            anim.setCurrentTime(anim.duration() * i // 10)
            way.append(pic.split())
        seen.append(way)
        assert pic.split() == pytest.approx(expect_end)
        assert anim.state() != anim.State.Running
        assert pic._timer.isActive() and pic._timer.interval() == PIC.HOLD_MS
        # monotonic toward the far end, never leaving [0, 1]
        ordered = way if expect_end == 1.0 else way[::-1]
        assert ordered == sorted(ordered)
        assert all(0.0 <= v <= 1.0 for v in way)
    assert min(min(w) for w in seen) == pytest.approx(0.0) and max(max(w) for w in seen) == pytest.approx(1.0)


def test_glide_restart_stops_previous_one(make, motion):
    pic = make("on")
    pic._glide(la.STOP_THEY)
    pic._anim.setCurrentTime(1000)
    mid = pic.split()
    pic._glide(la.STOP_YOU)
    assert pic._anim.startValue() == pytest.approx(mid)
    assert pic._anim.endValue() == pytest.approx(la.STOP_YOU)
    assert pic._anim.state() == pic._anim.State.Running


@pytest.mark.parametrize("k", ["on", "partial", "unavailable"])
@pytest.mark.parametrize("why", ["autoplay", "animations", "inactive", "hovered", "dragging", "region", "hidden"])
def test_nothing_plays_when_it_may_not(make, motion, monkeypatch, why, k):
    pic = make(k)
    assert pic._can_play()
    if why == "autoplay":
        monkeypatch.setattr(PIC, "AUTOPLAY", False)
    elif why == "animations":
        monkeypatch.setattr(la, "_animations_on", lambda: False)
    elif why == "inactive":
        monkeypatch.setattr(la, "QGuiApplication",
                            SimpleNamespace(applicationState=lambda: Qt.ApplicationInactive))
    elif why == "hovered":
        pic._hover = True
    elif why == "dragging":
        pic._dragging = True
    elif why == "region":
        monkeypatch.setattr(pic, "visibleRegion", lambda: QRegion())
    elif why == "hidden":
        pic.hide()
    assert not pic._can_play()
    before = pic.split()
    pic._timer_fired()
    assert pic._anim.state() != pic._anim.State.Running
    assert pic.split() == before
    if why in ("autoplay", "hidden"):
        return
    # the slide is due but can't play: try again later, don't give up
    assert pic._timer.isActive() and pic._timer.interval() == PIC.RETRY_MS


@pytest.mark.parametrize("k", KEYS)
def test_every_status_plays_by_itself(make, motion, k):
    """The formerly still statuses (off / failed / unavailable) slide like the hidden ones: the founder
    wants the line to move in the visible case too."""
    pic = make(k)
    assert pic._can_play() and pic.playing()
    assert pic._timer.isActive() and pic._timer.interval() == PIC.FIRST_HOLD_MS
    pic._timer_fired()
    assert pic._anim.state() == pic._anim.State.Running
    assert (pic._anim.startValue(), pic._anim.endValue()) == pytest.approx((1.0, 0.0))


@pytest.mark.parametrize("k", KEYS)
def test_autoplay_off_starts_no_timer(make, k):
    pic = make(k)                                                # the calm fixture has AUTOPLAY False
    assert not pic._timer.isActive() and not pic.playing()
    assert pic.split() == la.STOP_YOU


@pytest.mark.parametrize("k", KEYS)
def test_retry_then_play_when_allowed_again(make, motion, monkeypatch, k):
    pic = make(k)
    monkeypatch.setattr(la, "_animations_on", lambda: False)
    pic._timer_fired()
    assert pic._timer.interval() == PIC.RETRY_MS
    monkeypatch.setattr(la, "_animations_on", lambda: True)
    pic._timer_fired()
    assert pic._anim.state() == pic._anim.State.Running


@pytest.mark.parametrize("k", KEYS)
def test_hide_stops_everything_and_rests_the_line(make, motion, k):
    pic = make(k)
    pic._timer_fired()
    pic._anim.setCurrentTime(pic._anim.duration() // 2)
    assert pic.split() < la.STOP_YOU
    pic.hide()
    assert pic._anim.state() != pic._anim.State.Running
    assert not pic._timer.isActive() and not pic.playing()
    assert pic.split() == la.STOP_YOU
    assert not pic._hover and not pic._dragging


@pytest.mark.parametrize("k", KEYS)
def test_hide_while_dragging_clears_the_drag(make, motion, k):
    pic = make(k)
    press(pic, 0.5)
    assert pic._dragging
    pic.hide()
    assert not pic._dragging and pic.split() == la.STOP_YOU


@pytest.mark.parametrize("k", KEYS)
def test_show_again_restarts_from_the_card(make, motion, k):
    pic = make(k)
    pic.set_split(0.4)
    pic.hide()
    pic.show()
    QApplication.processEvents()
    assert pic.split() == la.STOP_YOU
    assert pic._timer.isActive() and pic._timer.interval() == PIC.FIRST_HOLD_MS


_PAIRS = [(a, b) for a in KEYS for b in KEYS if a != b]


@pytest.mark.parametrize("a, b", _PAIRS)
def test_status_change_restarts_the_loop_and_it_keeps_moving(make, motion, a, b):
    """A real status change (any -> any) starts the sweep again from the card; nothing is parked or
    unfocusable on either side of the change."""
    pic = make(a)
    pic._timer_fired()
    pic._anim.setCurrentTime(pic._anim.duration() // 3)
    assert pic.split() < la.STOP_YOU
    pic.set_status(status(b))
    assert pic._anim.state() != pic._anim.State.Running
    assert pic.moves() and pic.focusPolicy() == Qt.StrongFocus
    assert pic.split() == la.STOP_YOU
    assert pic._timer.isActive() and pic._timer.interval() == PIC.FIRST_HOLD_MS and pic.playing()
    pic._timer_fired()
    assert pic._anim.state() == pic._anim.State.Running
    assert pic._anim.endValue() == pytest.approx(la.STOP_THEY)


@pytest.mark.parametrize("a, b", [("off", "on"), ("unavailable", "partial"), ("on", "failed")])
def test_status_change_while_hidden_starts_nothing(make, motion, a, b):
    pic = make(a, show=False)
    assert pic.split() == la.STOP_YOU                            # not visible: waiting at the card, never mid-glass
    pic.set_status(status(b))
    assert pic.split() == la.STOP_YOU
    assert not pic.playing()
    pic.show()
    QApplication.processEvents()
    assert pic.playing() and pic._timer.interval() == PIC.FIRST_HOLD_MS


@pytest.mark.parametrize("k", KEYS)
def test_glide_done_arms_hold_only_when_it_still_plays(make, motion, monkeypatch, k):
    pic = make(k)
    pic._timer.stop()
    pic._glide_done()
    assert pic._timer.interval() == PIC.HOLD_MS and pic._timer.isActive()      # in every status, off / failed too
    pic._timer.stop()
    monkeypatch.setattr(PIC, "AUTOPLAY", False)
    pic._glide_done()
    assert not pic._timer.isActive()


# ═════════════════════════ a hand takes over ═════════════════════════
@pytest.mark.parametrize("k", KEYS)
def test_press_jumps_the_line_and_starts_a_drag(make, motion, k):
    pic = make(k)
    pic._timer_fired()
    ev = press(pic, 0.50)
    assert ev.isAccepted()
    assert pic._dragging
    assert pic.split() == pytest.approx(0.50, abs=0.005)
    assert pic._anim.state() != pic._anim.State.Running
    assert not pic._timer.isActive()                             # nothing resumes while the hand is down


@pytest.mark.parametrize("k", KEYS)
def test_press_works_on_the_whole_glass_not_just_the_handle(make, k):
    pic = make(k)
    for f in (0.1, 0.33, 0.77):
        pic._dragging = False
        press(pic, f)
        assert pic.split() == pytest.approx(f, abs=0.005)


@pytest.mark.parametrize("k", KEYS)
def test_drag_follows_and_clamps(make, k):
    pic = make(k)
    press(pic, 0.5)
    move(pic, 0.7, dragging=True)
    assert pic.split() == pytest.approx(0.7, abs=0.005)
    mouse(pic, QEvent.MouseMove, -500, mid_y(pic), Qt.NoButton, Qt.LeftButton)
    assert pic.split() == la.SPLIT_MIN
    mouse(pic, QEvent.MouseMove, 5000, 4000, Qt.NoButton, Qt.LeftButton)       # far outside, even below
    assert pic.split() == la.SPLIT_MAX
    assert pic._dragging


@pytest.mark.parametrize("k", KEYS)
def test_press_outside_the_glass_does_nothing(make, k):
    pic = make(k)
    before = pic.split()
    mouse(pic, QEvent.MouseButtonPress, split_x(pic, 0.5), 3)                  # the label row
    assert not pic._dragging and pic.split() == before
    mouse(pic, QEvent.MouseButtonPress, split_x(pic, 0.5), pic.height() - 4)   # under the stand
    assert not pic._dragging and pic.split() == before


@pytest.mark.parametrize("k", KEYS)
def test_right_button_does_not_drag(make, k):
    pic = make(k)
    before = pic.split()
    press(pic, 0.5, button=Qt.RightButton)
    assert not pic._dragging and pic.split() == before


@pytest.mark.parametrize("k", VISIBLE)
def test_visible_statuses_take_the_mouse_like_hidden(make, k):
    """off / failed / unavailable used to ignore the mouse (a still, parked line); now a click takes the
    line, the pointer shows the resize cursor and holds the glide."""
    pic = make(k)
    move(pic, 0.7)
    assert pic._hover and pic.cursor().shape() == Qt.SizeHorCursor
    pic._hover = False
    pic.unsetCursor()
    press(pic, 0.8)
    assert pic._dragging and pic.split() == pytest.approx(0.8, abs=0.005)
    move(pic, 0.3, dragging=True)
    assert pic.split() == pytest.approx(0.3, abs=0.005)
    release(pic, 0.3)
    assert not pic._dragging and pic.split() == pytest.approx(0.3, abs=0.005)


@pytest.mark.parametrize("k", KEYS)
def test_press_drag_release_across_the_whole_width(make, motion, k):
    """Take the line anywhere, drag it the full width and back, let go: it follows the pointer the whole
    way, the ends are exactly 0.0 and 1.0, and the sweep resumes only RESUME_MS after the release."""
    pic = make(k)
    press(pic, 0.25)                                             # off the handle (it waits at 1.0): the line jumps
    assert pic._dragging and pic.split() == pytest.approx(0.25, abs=0.005)
    assert not pic._timer.isActive() and pic._anim.state() != pic._anim.State.Running
    for f in (0.35, 0.5, 0.65, 0.8, 0.95):
        move(pic, f, dragging=True)
        assert pic.split() == pytest.approx(f, abs=0.005), f
    mouse(pic, QEvent.MouseMove, split_x(pic, 1.0) + 400, mid_y(pic), Qt.NoButton, Qt.LeftButton)
    assert pic.split() == 1.0                                    # off the right end: exactly the end
    for f in (0.85, 0.6, 0.4, 0.2, 0.05):
        move(pic, f, dragging=True)
        assert pic.split() == pytest.approx(f, abs=0.005), f
    mouse(pic, QEvent.MouseMove, -400, mid_y(pic), Qt.NoButton, Qt.LeftButton)
    assert pic.split() == 0.0                                    # off the left end: exactly the end
    assert pic._dragging
    release(pic, 0.0)
    assert not pic._dragging and pic.split() == 0.0              # stays where the hand left it
    assert pic._timer.isActive() and pic._timer.interval() == PIC.RESUME_MS
    assert pic._hover                                            # let go over the glass: the pointer is still on it ...
    pic._timer_fired()
    assert pic._anim.state() != pic._anim.State.Running and pic._timer.interval() == PIC.RETRY_MS
    leave(pic)                                                   # ... so it waits until the pointer is gone
    pic._timer_fired()
    assert pic._anim.state() == pic._anim.State.Running
    assert pic._anim.startValue() == pytest.approx(0.0) and pic._anim.endValue() == pytest.approx(1.0)


@pytest.mark.parametrize("k", KEYS)
def test_release_ends_the_drag_and_resumes_later(make, motion, k):
    pic = make(k)
    press(pic, 0.5)
    ev = release(pic, 0.5)
    assert ev.isAccepted()
    assert not pic._dragging
    assert pic.split() == pytest.approx(0.5, abs=0.005)           # it stays where the hand left it
    assert pic._timer.isActive() and pic._timer.interval() == PIC.RESUME_MS
    # when the resume timer fires with the pointer still over the glass, it waits
    pic._hover = True
    pic._timer_fired()
    assert pic._anim.state() != pic._anim.State.Running and pic._timer.interval() == PIC.RETRY_MS
    # ... and plays once the pointer is gone
    pic._hover = False
    pic._timer_fired()
    assert pic._anim.state() == pic._anim.State.Running
    assert pic._anim.startValue() == pytest.approx(0.5, abs=0.005)
    assert pic._anim.endValue() == pytest.approx(la.STOP_YOU)       # 0.5 is nearer the card's rest


def test_release_without_drag_is_not_ours(make, motion):
    pic = make("on")
    pic._timer.stop()
    release(pic, 0.5)
    assert not pic._timer.isActive()


@pytest.mark.parametrize("k", KEYS)
def test_hover_stops_and_leave_resumes(make, motion, k):
    pic = make(k)
    assert pic._timer.interval() == PIC.FIRST_HOLD_MS
    move(pic, 0.6)
    assert pic._hover
    assert pic.cursor().shape() == Qt.SizeHorCursor
    pic._timer_fired()                                            # a due slide waits for the pointer
    assert pic._anim.state() != pic._anim.State.Running
    leave(pic)
    assert not pic._hover
    assert pic.cursor().shape() == Qt.ArrowCursor
    assert pic._timer.isActive() and pic._timer.interval() == PIC.RESUME_MS


@pytest.mark.parametrize("k", KEYS)
def test_hovering_holds_a_running_glide_where_it_is(make, motion, k):
    """A glide under way stops the moment the pointer comes onto the glass: the
    line must not slide away under a reaching hand."""
    pic = make(k)
    pic._timer_fired()
    pic._anim.setCurrentTime(600)
    held = pic.split()
    move(pic, 0.5)
    assert pic._hover and pic._anim.state() != pic._anim.State.Running
    assert not pic._timer.isActive()
    assert pic.split() == pytest.approx(held)
    pic._timer_fired()                                            # (the retry): the pointer is still there
    assert pic._anim.state() != pic._anim.State.Running


def test_pointer_moving_off_the_glass_resumes(make, motion):
    pic = make("on")
    move(pic, 0.5)
    assert pic._hover
    mouse(pic, QEvent.MouseMove, split_x(pic, 0.5), 2)               # up in the label row
    assert not pic._hover
    assert pic.cursor().shape() == Qt.ArrowCursor
    assert pic._timer.isActive() and pic._timer.interval() == PIC.RESUME_MS


def test_mouse_positions_are_converted_with_the_scale(make):
    pic = make("on", width=231)
    assert pic._scale() == pytest.approx(0.5)
    press(pic, 0.25)
    assert pic.split() == pytest.approx(0.25, abs=0.01)
    move(pic, 0.8, dragging=True)
    assert pic.split() == pytest.approx(0.8, abs=0.01)


def test_press_during_a_glide_is_not_overwritten_by_the_animation(make, motion):
    pic = make("on")
    pic._timer_fired()
    pic._anim.setCurrentTime(pic._anim.duration() // 4)
    press(pic, 0.5)
    held = pic.split()
    assert held == pytest.approx(0.5, abs=0.005)
    # the glide is over: no tick can reach the line any more, and no "arrived" hold timer is armed
    QApplication.processEvents()
    assert pic.split() == held
    assert pic._anim.state() != pic._anim.State.Running
    assert not pic._timer.isActive()


def test_press_during_a_real_glide_is_not_overwritten(make, monkeypatch):
    """The same with the real clock (a few events only; the bound is generous)."""
    monkeypatch.setattr(PIC, "AUTOPLAY", True)
    monkeypatch.setattr(PIC, "FIRST_HOLD_MS", 3_000_001)
    monkeypatch.setattr(PIC, "GLIDE_MS", 30_000)
    pic = make("on")
    pic._timer_fired()
    QTest.qWait(40)
    press(pic, 0.5)
    held = pic.split()
    QTest.qWait(120)
    assert pic.split() == held


def test_drag_during_a_glide_then_release_does_not_snap_back(make, motion):
    pic = make("on")
    pic._timer_fired()
    pic._anim.setCurrentTime(1000)
    press(pic, 0.45)
    release(pic, 0.45)
    QApplication.processEvents()
    assert pic.split() == pytest.approx(0.45, abs=0.005)
    assert pic._anim.state() != pic._anim.State.Running
    assert pic._timer.interval() == PIC.RESUME_MS


@pytest.mark.parametrize("fraction", [0.31, 0.77])
def test_auto_glide_resumes_from_where_the_hand_left_the_line(make, motion, fraction):
    pic = make("on")
    pic._timer_fired()
    pic._anim.setCurrentTime(int(pic._anim.duration() * fraction))
    key(pic, Qt.Key_Left)                                          # the hand takes over part-way
    pic.set_split(0.65)                                            # ... and leaves the line here
    pic._timer_fired()                                             # RESUME_MS later
    assert pic._anim.startValue() == pytest.approx(0.65)
    assert pic.split() == pytest.approx(0.65)                      # no jump at the start of the slide


# ═════════════════════════ keys ═════════════════════════
@pytest.mark.parametrize("sk", ["on", "off"])                       # (Home / End below run in every status; the key code never looks at the status)
@pytest.mark.parametrize("k, start, expected", [
    (Qt.Key_Left, 0.5, 0.5 - la.STEP), (Qt.Key_Down, 0.5, 0.5 - la.STEP),
    (Qt.Key_Right, 0.5, 0.5 + la.STEP), (Qt.Key_Up, 0.5, 0.5 + la.STEP),
    (Qt.Key_PageDown, 0.6, 0.6 - la.BIG_STEP), (Qt.Key_PageUp, 0.5, 0.5 + la.BIG_STEP),
    (Qt.Key_Home, 0.5, 0.0), (Qt.Key_End, 0.5, 1.0),
    (Qt.Key_Home, 1.0, 0.0), (Qt.Key_End, 0.0, 1.0), (Qt.Key_Home, 0.0, 0.0), (Qt.Key_End, 1.0, 1.0),
    (Qt.Key_Left, 0.0, 0.0), (Qt.Key_Right, 1.0, 1.0),
    (Qt.Key_Left, 0.03, 0.0), (Qt.Key_Right, 0.97, 1.0),         # the last step reaches the very end
    (Qt.Key_PageUp, 0.9, 1.0), (Qt.Key_PageDown, 0.1, 0.0),
])
def test_keys_move_the_line(make, motion, sk, k, start, expected):
    pic = make(sk)
    pic.set_split(start)
    ev = key(pic, k)
    assert ev.isAccepted()
    assert pic.split() == pytest.approx(expected)
    assert pic._anim.state() != pic._anim.State.Running
    assert pic._timer.isActive() and pic._timer.interval() == PIC.RESUME_MS


@pytest.mark.parametrize("k", KEYS)
def test_key_during_a_glide_stops_it(make, motion, k):
    pic = make(k)
    pic._timer_fired()
    pic._anim.setCurrentTime(pic._anim.duration() // 2)
    mid = pic.split()
    key(pic, Qt.Key_Right)
    assert pic._anim.state() != pic._anim.State.Running
    assert pic.split() == pytest.approx(min(la.SPLIT_MAX, mid + la.STEP))     # from where the line WAS, not from the glide's end


@pytest.mark.parametrize("k", VISIBLE)
@pytest.mark.parametrize("code", [Qt.Key_Left, Qt.Key_Right, Qt.Key_Home, Qt.Key_End, Qt.Key_PageUp, Qt.Key_Up])
def test_visible_statuses_take_the_keys(make, k, code):
    """The keys used to be ignored when the card was visible; now they move the line like in "on"."""
    pic = make(k)
    pic.set_split(0.5)
    ev = key(pic, code)
    assert ev.isAccepted()
    assert pic.split() != 0.5


@pytest.mark.parametrize("k", KEYS)
def test_other_keys_are_not_swallowed(make, k):
    pic = make(k)
    for code in (Qt.Key_A, Qt.Key_Tab, Qt.Key_Escape, Qt.Key_Space, Qt.Key_Return):
        assert not key(pic, code).isAccepted()
    assert pic.split() == la.STOP_YOU


@pytest.mark.parametrize("k", KEYS)
def test_focus_ring_only_for_keyboard_focus(make, k):
    pic = make(k)
    assert pic.focusPolicy() == Qt.StrongFocus
    for reason in (Qt.MouseFocusReason, Qt.OtherFocusReason, Qt.PopupFocusReason):
        QApplication.sendEvent(pic, QFocusEvent(QEvent.FocusIn, reason))
        assert not pic._focus_ring, reason
    for reason in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason):
        QApplication.sendEvent(pic, QFocusEvent(QEvent.FocusIn, reason))
        assert pic._focus_ring, reason
        QApplication.sendEvent(pic, QFocusEvent(QEvent.FocusOut, Qt.TabFocusReason))
        assert not pic._focus_ring
    # and through Qt's own focus handling (an offscreen window may not be activated: then only the events above apply)
    pic.setFocus(Qt.MouseFocusReason)
    QApplication.processEvents()
    assert not pic._focus_ring
    pic.clearFocus()
    pic.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    if pic.hasFocus():
        assert pic._focus_ring
        pic.clearFocus()
        QApplication.processEvents()
        assert not pic._focus_ring


@pytest.mark.parametrize("k", KEYS)
def test_tab_focus_ring_is_painted_on_the_handle(make, k):
    """After Tab the handle wears a blue ring, in every status (the visible ones used to have no focus at all)."""
    pic = make(k)
    pic.set_split(0.5)
    plain = image(pic)
    QApplication.sendEvent(pic, QFocusEvent(QEvent.FocusIn, Qt.TabFocusReason))
    ringed = image(pic)
    cx, cy = handle_centre_x(0.5), glass_mid_y()
    r = la.HANDLE_R + 3.5
    blue = [at(ringed, pic, x, y) for x, y in ring(cx, cy, r)]
    before = [at(plain, pic, x, y) for x, y in ring(cx, cy, r)]
    assert sum(1 for c in blue if c.blue() > c.red() + 25) >= 9          # the blue ring ...
    assert sum(1 for c in before if c.blue() > c.red() + 25) <= 3        # ... was not there before
    QApplication.sendEvent(pic, QFocusEvent(QEvent.FocusOut, Qt.TabFocusReason))
    assert image(pic) == plain


# ═════════════════════════ rendering ═════════════════════════
@pytest.mark.parametrize("k", KEYS)
@pytest.mark.parametrize("width", [462, 380, 300, 260, 700])
@pytest.mark.parametrize("f", [la.SPLIT_MIN, 0.3, 0.5, la.SPLIT_MAX])
def test_grab_everywhere_without_errors(make, k, width, f):
    pic = make(k, width=width)
    pic.set_split(f)
    img = image(pic)
    assert img.width() >= pic.width() and img.height() >= pic.height()
    # something was drawn: not a blank widget
    colours = {img.pixel(x, y) for x in range(0, img.width(), 23) for y in range(0, img.height(), 19)}
    assert len(colours) > 5


def test_grab_with_a_long_owner_name_and_no_owner(make):
    pic = make("on", owner="Bartholomew-Alexandrovich")
    image(pic)
    pic.set_owner("")
    image(pic)


def test_card_goes_when_the_line_goes_left(make):
    """The whole point: at the card's rest the card shows, at the other rest it is gone on their side."""
    pic = make("on")
    you = grab_at(pic, la.STOP_YOU)
    they = grab_at(pic, la.STOP_THEY)
    assert diff(you, they, pic, CARD_BODY) > 8
    # the stops are the ends of the glass: at 1.0 the WHOLE screen is "You see", at 0.0 the WHOLE screen is
    # "They see" (checked against the cached sides, clear of the handle and of the rounded corners)
    assert side_diff(you, pic, "you", 12, 390) < 1.0
    assert side_diff(they, pic, "they", 60, 438) < 1.0
    assert side_diff(you, pic, "they", 12, 390) > 3               # ... and not the other side
    assert side_diff(they, pic, "you", 60, 438) > 3
    # in between both show
    mid = grab_at(pic, 0.5)
    assert side_diff(mid, pic, "you", 12, 190) < 1.0
    assert side_diff(mid, pic, "they", 260, 438) < 1.0


def test_call_side_card_absent_when_hidden_present_otherwise(make):
    shots = {}
    for k in KEYS:
        pic = make(k)
        shots[k] = (grab_at(pic, la.SPLIT_MAX), grab_at(pic, la.SPLIT_MIN), pic)    # all "you" / all "they"
    # the picture of the card on THEIR side, as the same region of an "off" render
    absent = shots["on"][1]
    present = shots["off"][0]
    full = diff(absent, present, shots["on"][2], CARD_BODY)
    assert full > 8
    # "partial": the card is on their side at about 45%
    partial = diff(shots["partial"][1], absent, shots["on"][2], CARD_BODY)
    assert 0.25 * full < partial < 0.90 * full      # between: faded, not gone and not full (layers blend one by one)
    # off / failed / unavailable: the same card as on the left, to the last pixel, and the same on both
    # ends of the sweep (only the banners change as the line goes by)
    for k in VISIBLE:
        assert diff(shots[k][0], present, shots[k][2], CARD_BODY) < 1.0, k
        assert diff(shots[k][1], shots[k][0], shots[k][2], CARD_BODY) < 1.0, k
    # and the "you" side is the same card whatever the status
    assert diff(shots["on"][0], present, shots["on"][2], CARD_BODY) < 1.5   # (the macOS chip is wider: its card is too)


@pytest.mark.parametrize("k", VISIBLE)
def test_in_the_visible_statuses_the_sweep_only_changes_the_banners(make, k):
    """off / failed / unavailable: the card is full on both sides, so from one end of the glass to the
    other only "Sharing" / "<owner>'s screen" change."""
    pic = make(k, owner="Aram")
    you, they = grab_at(pic, la.STOP_YOU), grab_at(pic, la.STOP_THEY)
    top_left = slide_rect(0, 4, 140, 17)
    top_right = slide_rect(160, 4, 300, 17)
    assert diff(you, they, pic, CARD_BODY) < 1.0                           # the card: the same
    assert diff(you, they, pic, slide_rect(150, 20, 300, 160), step=3) < 6  # (the head differs only by the line / handle)
    assert diff(you, they, pic, top_left) > 3 and diff(you, they, pic, top_right) > 3    # the banners: swapped
    # the card is where it is on the call side at partial strength only in "partial"
    assert pic.they_see_card() and pic._they_opacity() == 1.0


@pytest.mark.parametrize("k", KEYS)
def test_both_sides_show_the_same_card_unless_hidden(make, k):
    """Below the banners the two sides of the glass are the same picture when the card shows to the call
    at full strength; when hidden, or faded (macOS), they differ."""
    pic = make(k)
    image(pic)
    you, they = pic._cache["you"].toImage(), pic._cache["they"].toImage()
    assert you.size() == they.size()
    top = int(30 * la.K * you.height() / (la.INNER_H * la.K))        # below the banners (slide y 30)
    low_you, low_they = you.copy(0, top, you.width(), you.height() - top), they.copy(0, top, they.width(), they.height() - top)
    assert (low_you == low_they) is (k in ("off", "failed", "unavailable"))
    assert you != they                                               # the banners always differ


@pytest.mark.parametrize("k", KEYS)
@pytest.mark.parametrize("f", [0.2, 0.5, 0.8])
def test_every_status_draws_the_line_and_the_handle(make, k, f):
    """There is no parked / faint seam any more: the solid line and the round handle are drawn in every
    status, wherever the line is."""
    pic = make(k)
    img = grab_at(pic, f)
    left, top, right, bottom = glass()
    x = left + f * (right - left)
    assert at(img, pic, x, bottom - 20).lightness() < 120                 # the ink line, low on the glass (clear of the banners) ...
    assert at(img, pic, x, (top + bottom) / 2.0 + 40).lightness() < 120   # ... and just under the disc
    assert at(img, pic, x - 4, bottom - 20).lightness() > 120             # (and it is thin: nothing dark beside it)
    assert at(img, pic, x + 4, bottom - 20).lightness() > 120
    cx, cy = handle_centre_x(f), glass_mid_y()
    for px, py in ring(cx, cy, 14):
        assert at(img, pic, px, py).lightness() > 235, (px, py)          # the white disc ...
    assert at(img, pic, cx + 9.5, cy).lightness() < 140                   # ... with its dark "›" tip
    assert at(img, pic, cx - 9.5, cy).lightness() < 140                   # ... and "‹" tip


def test_left_of_the_line_is_you_right_is_they(make):
    pic = make("on")
    ref_you = grab_at(pic, la.SPLIT_MAX)
    ref_they = grab_at(pic, la.SPLIT_MIN)
    mid = grab_at(pic, 0.5)
    left = slide_rect(5, 60, 100, 160)                                 # left of the line (slide x 150), clear of the banner
    right = slide_rect(200, 60, 290, 160)                              # right of it, where the card is on "you"
    assert diff(mid, ref_you, pic, left) < 1.0
    assert diff(mid, ref_they, pic, right) < 1.0
    assert diff(mid, ref_you, pic, right) > 8                          # not the 'you' side there
    assert diff(mid, ref_they, pic, left) >= 0                         # (the slide is the same on both sides)


def test_banners_differ_by_side(make):
    pic = make("on", owner="Aram")
    mid = grab_at(pic, 0.5)
    ref_you = grab_at(pic, la.SPLIT_MAX)
    ref_they = grab_at(pic, la.SPLIT_MIN)
    top_left = slide_rect(0, 4, 140, 17)                               # "Sharing" pill on the left, in "you"
    top_right = slide_rect(160, 4, 300, 17)                            # owner pill on the right, in "they"
    assert diff(mid, ref_you, pic, top_left) < 1.0
    assert diff(mid, ref_they, pic, top_right) < 1.0
    assert diff(ref_you, ref_they, pic, top_left) > 3                  # the two sides have different banners there
    assert diff(ref_you, ref_they, pic, top_right) > 3


@pytest.mark.parametrize("k", KEYS)
def test_handle_is_painted_where_the_line_is(make, k):
    pic = make(k)
    img = grab_at(pic, 0.5)
    assert img != grab_at(pic, 0.7)                                    # moving the line moves what is drawn
    x = la.BEZEL + 0.5 * la.INNER_W * la.K
    y = la.LABEL_H + la.LABEL_GAP + la.BEZEL + la.INNER_H * la.K / 2.0
    c = at(img, pic, x, y - 40)                                       # on the line, well above the disc (r = 17): ink-dark
    assert c.lightness() < 120


@pytest.mark.parametrize("k", KEYS)
def test_hot_handle_is_bigger(make, k):
    pic = make(k)
    pic.set_split(0.5)
    calm_img = image(pic)
    pic._hover = True
    hot_img = image(pic)
    assert calm_img != hot_img


# ═════════════════════════ the cache ═════════════════════════
def _ids(pic):
    """The cached pixmaps themselves (kept alive, so identity can't be fooled by a reused id)."""
    return {k: v for k, v in pic._cache.items() if not isinstance(v, (int, float))}


def same(a, b, *names):
    names = names or tuple(a)
    return all(a[k] is b[k] for k in names)


def test_cache_is_built_once_and_reused(make):
    pic = make("on")
    image(pic)
    assert {"body", "you", "they", "you_label", "they_label", "handle", "handle_hot"} <= set(pic._cache)
    first = _ids(pic)
    key0 = pic._cache_key
    for f in (0.2, 0.5, 0.9):
        pic.set_split(f)
        image(pic)
    pic._hover = True
    image(pic)
    pic._hover = False
    pic._focus_ring = True
    image(pic)
    assert same(_ids(pic), first) and pic._cache_key == key0


def test_cache_rebuilt_when_the_status_changes(make):
    pic = make("on")
    image(pic)
    first = _ids(pic)
    pic.set_status(status("partial"))
    assert pic._cache == {}                                            # dropped at once
    image(pic)
    second = _ids(pic)
    assert second.keys() == first.keys()
    assert not any(second[k] is first[k] for k in ("you", "they"))


def test_cache_key_follows_the_status_word_not_the_live_context_text(make, monkeypatch):
    """The card's chip says STATUS_WORDS[key] (the page's one word): the cache is keyed on the key and that
    word, and a different live_context text alone changes nothing."""
    pic = make("on")
    image(pic)
    first = _ids(pic)
    pic._status = status("on")._replace(text="Hidden!!")               # only the live text differs
    image(pic)
    assert same(_ids(pic), first)
    pic._status = status("failed")                                     # another key: re-keyed
    image(pic)
    second = _ids(pic)
    assert second["you"] is not first["you"]
    monkeypatch.setattr(la, "STATUS_WORDS", {"on": "Hidden", "partial": "May be hidden", "failed": "Seen"})
    image(pic)                                                         # the same key, another WORD: re-keyed
    assert _ids(pic)["you"] is not second["you"]


@pytest.mark.parametrize("field, value", [("text", "Something else"), ("tone", "amber"), ("icon", "eye_off")])
def test_set_status_with_a_changed_field_is_a_real_change(make, field, value):
    pic = make("off")
    image(pic)
    first = _ids(pic)
    pic.set_status(status("off")._replace(**{field: value}))
    assert pic._cache == {}
    image(pic)
    assert _ids(pic)["you"] is not first["you"]


def test_cache_rebuilt_when_the_owner_changes(make):
    pic = make("on", owner="Aram")
    image(pic)
    first = _ids(pic)
    pic.set_owner("Zoe")
    image(pic)
    second = _ids(pic)
    assert second["they"] is not first["they"] and second["you_label"] is not first["you_label"]
    pic.set_owner("Zoe")                                              # the same name again: harmless
    image(pic)


def test_cache_rebuilt_when_the_scale_changes_and_not_above_design_width(make):
    pic = make("on", width=462)
    image(pic)
    first = _ids(pic)
    pic.resize(900, pic.heightForWidth(900))                          # still scale 1.0
    image(pic)
    assert same(_ids(pic), first)
    pic.resize(300, pic.heightForWidth(300))
    image(pic)
    smaller = _ids(pic)
    assert smaller["you"] is not first["you"]
    p = pic._cache["you"]
    assert p.width() == pytest.approx(round(la.INNER_W * la.K * pic._scale() * pic.devicePixelRatioF()), abs=1)
    pic.resize(301, pic.heightForWidth(301))                           # the same 1/64 step: reused
    image(pic)
    assert same(_ids(pic), smaller)
    pic.resize(340, pic.heightForWidth(340))                           # another step: rebuilt
    image(pic)
    assert _ids(pic)["you"] is not smaller["you"]


def test_cache_pixmaps_are_in_device_pixels(make):
    pic = make("on", width=462)
    image(pic)
    dpr = pic.devicePixelRatioF()
    you = pic._cache["you"]
    assert you.width() == round(la.INNER_W * la.K * dpr)
    assert you.height() == round(la.INNER_H * la.K * dpr)
    assert you.devicePixelRatio() == pytest.approx(dpr * pic._scale())


def test_a_paint_costs_no_new_pixmaps(make, monkeypatch):
    pic = make("on")
    image(pic)
    built = []
    for name in ("_render_body", "_render_scene", "_render_label", "_render_handle"):
        real = getattr(pic, name)
        monkeypatch.setattr(pic, name, lambda *a, _r=real, _n=name, **k: built.append(_n) or _r(*a, **k))
    for f in (0.1, 0.4, 0.9):
        pic.set_split(f)
        image(pic)
    assert built == []


# ═════════════════════════ in the page ═════════════════════════
def test_picture_inside_a_layout_scales_with_its_column(make):
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    host = QWidget()
    lay = QVBoxLayout(host)
    pic = PIC()
    lay.addWidget(pic)
    lay.addStretch(1)
    host.resize(300, 600)
    host.show()
    QApplication.processEvents()
    assert pic.width() <= 300
    assert pic.height() == pic.heightForWidth(pic.width())
    host.resize(700, 600)
    QApplication.processEvents()
    assert pic.height() == pic.heightForWidth(pic.width()) <= la.DESIGN_H
    host.hide()
    host.deleteLater()


# ═════════════════════ the review's fixes (workflow wf_41efb64d-327) ═════════════════════
def test_the_same_status_again_changes_nothing(make, motion):
    """The page re-applies the status on every refresh (Open, the card's signal,
    a sidebar click): a line mid-glide or under a hand must stay where it is."""
    pic = make("on")
    pic._timer_fired()
    pic._anim.setCurrentTime(800)
    mid = pic.split()
    cache = dict(pic._cache)
    pic.set_status(status("on"))
    assert pic.split() == pytest.approx(mid)
    assert pic._anim.state() == pic._anim.State.Running
    assert pic._cache == cache or all(pic._cache[k] is cache[k] for k in cache)
    press(pic, 0.5)                                                  # a hand has the line ...
    pic.set_status(status("on"))
    assert pic._dragging and pic.split() == pytest.approx(0.5, abs=0.01)   # ... and keeps it
    pic.set_status(status("off"))                                    # even a REAL change leaves a held line alone ...
    assert pic._dragging and pic.split() == pytest.approx(0.5, abs=0.01)
    release(pic, 0.5)
    leave(pic)                                                       # ... until the hand is gone: then the next one restarts
    pic.set_status(status("on"))
    assert pic.moves() and pic.split() == la.STOP_YOU
    assert pic._timer.isActive() and pic._timer.interval() == PIC.FIRST_HOLD_MS


@pytest.mark.parametrize("k", KEYS)
def test_the_same_status_again_changes_nothing_in_every_status(make, motion, k):
    pic = make(k)
    pic._timer_fired()
    pic._anim.setCurrentTime(900)
    mid = pic.split()
    image(pic)
    cache = dict(pic._cache)
    for _ in range(3):
        pic.set_status(status(k))
    assert pic.split() == pytest.approx(mid) and pic._anim.state() == pic._anim.State.Running
    assert all(pic._cache[n] is cache[n] for n in cache)


@pytest.mark.parametrize("a, b", [("on", "off"), ("off", "on"), ("unavailable", "partial")])
def test_set_split_after_a_status_change_is_honoured_and_the_restart_wins_over_a_stale_split(make, a, b):
    pic = make(a)
    pic.set_status(status(b))
    pic.set_split(0.9)
    assert pic.split() == 0.9                                         # never ignored
    pic.set_status(status(a))
    assert pic.split() == la.STOP_YOU                                 # the change restarts from the card, not a stale 0.9


@pytest.mark.parametrize("k", KEYS)
def test_hiding_while_hovered_gives_the_cursor_back(make, k):
    pic = make(k)
    move(pic, 0.5)
    assert pic.cursor().shape() == Qt.SizeHorCursor
    pic.hide()
    pic.show()
    QApplication.processEvents()
    assert pic.cursor().shape() == Qt.ArrowCursor and not pic._hover


@pytest.mark.parametrize("k", KEYS)
def test_letting_go_away_from_the_glass_clears_hover_and_cursor(make, motion, k):
    pic = make(k)
    move(pic, 0.5)                                                   # the pointer arrives on the glass ...
    press(pic, 0.5)                                                  # ... and takes hold
    assert pic.cursor().shape() == Qt.SizeHorCursor
    mouse(pic, QEvent.MouseButtonRelease, split_x(pic, 0.5), 2)      # released up in the label row
    assert not pic._hover and not pic._dragging
    assert pic.cursor().shape() == Qt.ArrowCursor
    assert pic._timer.isActive() and pic._timer.interval() == PIC.RESUME_MS
    press(pic, 0.5)
    release(pic, 0.5)                                                # released on the glass: still hovering
    assert pic._hover and pic.cursor().shape() == Qt.SizeHorCursor


@pytest.mark.parametrize("k", KEYS)
def test_taking_hold_of_the_handle_does_not_make_the_line_jump(make, k):
    """Grab the round handle off-centre and the line stays where it was; press
    elsewhere on the glass and it jumps to the pointer."""
    pic = make(k, width=462)
    pic.set_split(0.5)
    off = 12.0                                                       # design units to the left of the line
    mouse(pic, QEvent.MouseButtonPress, split_x(pic, 0.5) - off * pic._scale(), mid_y(pic))
    assert pic._dragging and pic.split() == pytest.approx(0.5, abs=1e-3)
    mouse(pic, QEvent.MouseMove, split_x(pic, 0.7) - off * pic._scale(), mid_y(pic), Qt.NoButton, Qt.LeftButton)
    assert pic.split() == pytest.approx(0.7, abs=1e-3)               # followed with the same offset
    mouse(pic, QEvent.MouseButtonRelease, split_x(pic, 0.7), mid_y(pic))
    pic.set_split(0.5)
    mouse(pic, QEvent.MouseButtonPress, split_x(pic, 0.9), mid_y(pic) - 60)   # off the handle: a jump
    assert pic.split() == pytest.approx(0.9, abs=0.01)


def test_the_owner_banner_never_outgrows_its_half_of_the_glass(make):
    long_name = "firstname.lastname.department.sales.emea.northern.region1"
    pic = make("on", owner=long_name)
    pic.set_split(la.SPLIT_MIN)                                      # nearly all of the call's side shows
    img = image(pic)
    banner_row = (la.INNER_H * 0.0 + 15)                             # slide units: the banners' middle
    left_half = slide_rect(60, banner_row, 140, banner_row + 1)
    x0, y0, x1, y1 = left_half
    # No dark banner pixels in the left 2/5 of the glass: the name is elided, not run off the edge.
    for x in range(int(x0), int(x1), 6):
        c = at(img, pic, x, y0)
        assert c.red() > 200 and c.green() > 200, (x, c.name())
    assert pic.owner_banner() == long_name + "’s screen"             # the description keeps the whole name


@pytest.mark.parametrize("k", KEYS)
def test_with_animations_off_the_line_rests_where_both_sides_show(make, monkeypatch, k):
    monkeypatch.setattr(la, "_animations_on", lambda: False)
    pic = make(k)
    assert pic.split() == la.REST_STATIC == 0.62
    assert not pic._can_play()
    assert la.STOP_THEY < la.REST_STATIC < la.STOP_YOU
    pic.hide()
    pic.show()
    QApplication.processEvents()
    assert pic.split() == la.REST_STATIC
    monkeypatch.setattr(la, "_animations_on", lambda: True)
    pic.hide()
    pic.show()
    QApplication.processEvents()
    assert pic.split() == la.STOP_YOU


def test_partial_says_it_may_show(make):
    pic = make("partial")
    desc = pic.accessibleDescription()
    assert "with the card on it" in desc and "may see" in desc and "partly hides" in desc
    for k in VISIBLE:
        d = make(k).accessibleDescription()
        assert "may see" not in d and "partly hides" not in d and "with the card on it" in d
    assert "no card" in make("on").accessibleDescription()


def test_the_painter_scale_snaps_down_to_a_step(make):
    pic = make("on", width=462)
    seen = set()
    for w in range(300, 463):
        pic.resize(w, pic.heightForWidth(w))
        s = pic._scale()
        assert s <= w / 462.0 + 1e-9 and (w / 462.0 - s) < 1 / float(la.SCALE_STEPS) + 1e-9
        seen.add(round(s, 6))
    assert len(seen) <= la.SCALE_STEPS * 0.4 + 2                     # ~25 rebuilds across the whole drag


def test_the_page_never_says_hidden_when_it_could_not_find_out(qapp, monkeypatch):
    from tests_qt.conftest import make_stub_app
    monkeypatch.setattr(la, "load_sessions", lambda: [])
    monkeypatch.setattr(live_context, "hide_status",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    app = make_stub_app("pro")
    app.cfg.update({"live_assist_private": True, "live_assist_language": "", "live_assist_output_language": "",
                    "live_assist_theme": "light", "live_assist_opacity": 0.96, "live_assist_context": ""})
    app.live_assist = None
    page = la.create(app)
    try:
        # one word, never "Hidden"; the explanation is the name / tooltip only
        assert page.status_chip.text() == la.STATUS_WORD_DEFAULT == "Visible"
        assert page.status_chip.accessibleName() == "Can't confirm it's hidden"
        assert page.picture.they_see_card() and page.picture.moves()
        assert page.picture._status.key == "unavailable"
        assert page.picture.focusPolicy() == Qt.StrongFocus
    finally:
        page.deleteLater()
        QApplication.processEvents()


def test_the_page_says_visible_when_it_could_not_find_out_and_hiding_was_off(qapp, monkeypatch):
    from tests_qt.conftest import make_stub_app
    monkeypatch.setattr(la, "load_sessions", lambda: [])
    monkeypatch.setattr(live_context, "hide_status",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    app = make_stub_app("pro")
    app.cfg.update({"live_assist_private": False, "live_assist_language": "", "live_assist_output_language": "",
                    "live_assist_theme": "light", "live_assist_opacity": 0.96, "live_assist_context": ""})
    app.live_assist = None
    page = la.create(app)
    try:
        assert page.status_chip.text() == "Visible"
        assert page.status_chip.accessibleName() == "Visible on screen share"
        assert page.picture._status.key == "off" and page.picture.moves()
    finally:
        page.deleteLater()
        QApplication.processEvents()


# ═════════════════════════ the full-width sweep: the line at the ends, the handle clamped ═════════════════════════
def test_handle_x_is_the_line_in_the_middle_and_clamped_near_the_ends(make):
    pic = make("on")
    inner = pic._inner_rect()
    r = la.HANDLE_R * 1.1
    for f in (0.1, 0.25, 0.5, 0.75, 0.9):
        assert pic._handle_x(inner, f) == pytest.approx(inner.left() + f * inner.width())
    assert pic._handle_x(inner, 0.0) == pytest.approx(inner.left() + r)
    assert pic._handle_x(inner, 1.0) == pytest.approx(inner.right() - r)
    margin = r / inner.width()                                     # ~0.0416 of the glass
    for f in (0.0, 0.01, margin * 0.99):
        assert pic._handle_x(inner, f) == pytest.approx(inner.left() + r)
    for f in (1.0, 0.99, 1.0 - margin * 0.99):
        assert pic._handle_x(inner, f) == pytest.approx(inner.right() - r)
    for f in (margin * 1.01, 1.0 - margin * 1.01):                 # just past the clamp: the line itself again
        assert pic._handle_x(inner, f) == pytest.approx(inner.left() + f * inner.width())
    xs = [pic._handle_x(inner, i / 200.0) for i in range(201)]
    assert xs == sorted(xs)                                        # never goes backwards
    assert all(inner.left() + r - 1e-9 <= x <= inner.right() - r + 1e-9 for x in xs)
    # the whole disc (and its hot size) is inside the glass at the ends
    assert xs[0] - la.HANDLE_R * 1.1 >= inner.left() - 1e-9 and xs[-1] + la.HANDLE_R * 1.1 <= inner.right() + 1e-9


@pytest.mark.parametrize("k, width", [(k, 462) for k in KEYS] + [("on", 300), ("unavailable", 300)])
@pytest.mark.parametrize("hot", [False, True])
@pytest.mark.parametrize("f", [0.0, 1.0])
def test_at_either_end_the_handle_rests_inside_the_glass(make, k, width, f, hot):
    """At 0.0 and 1.0 the round handle (calm or hot) lies fully inside the glass: its rim is white
    pixels, and the bezel beside it is the bezel (the disc does not spill over it)."""
    pic = make(k, width=width)
    pic._hover = hot
    img = grab_at(pic, f)
    left, top, right, bottom = glass()
    cy = glass_mid_y()
    cx = handle_centre_x(f)
    assert cx == pytest.approx(left + la.HANDLE_R * 1.1 if f == 0.0 else right - la.HANDLE_R * 1.1)
    rim = la.HANDLE_R * (1.1 if hot else 1.0) - 3.0                 # inside the disc's 1px outline, whatever the scale
    for px, py in ring(cx, cy, rim, n=16):
        assert left <= px <= right
        assert at(img, pic, px, py).lightness() > 235, (px, py)
    edge_x = left - 2.0 if f == 0.0 else right + 2.0               # in the bezel, level with the handle
    for dy in (0, -10, 10):
        assert at(img, pic, edge_x, cy + dy).lightness() < 90, dy


@pytest.mark.parametrize("f", [0.0, 1.0])
@pytest.mark.parametrize("k", KEYS)
def test_at_either_end_the_line_is_the_edge_of_the_glass(make, k, f):
    """The line is clipped to the glass: a dark column right at the screen's edge, no white halo
    smeared over the bezel."""
    pic = make(k, width=462)
    img = grab_at(pic, f)
    left, top, right, bottom = glass()
    line_x = left + 0.5 if f == 0.0 else right - 0.5
    bezel_x = left - 1.5 if f == 0.0 else right + 1.5
    for y in (top + 20, bottom - 20):
        assert at(img, pic, line_x, y).lightness() < 120, y
        assert at(img, pic, bezel_x, y).lightness() < 90, y
    # the other end of the glass has no line
    assert at(img, pic, (right - 0.5) if f == 0.0 else (left + 0.5), top + 20).lightness() > 120


@pytest.mark.parametrize("f", [0.0, 1.0])
@pytest.mark.parametrize("k", KEYS)
def test_pressing_the_clamped_handle_at_an_end_does_not_move_the_handle(make, k, f):
    """The handle rests against the glass's edge while the line is the edge itself. Taking hold of the
    handle keeps it under the pointer (the line moves under it), wherever on the disc you press."""
    pic = make(k, width=462)
    inner = pic._inner_rect()
    cx, cy = handle_centre_x(f), glass_mid_y()
    for dx, dy in ((0, 0), (-9, 0), (9, 0), (0, -10), (0, 10), (-8, 8)):
        pic._dragging = False
        pic.set_split(f)
        press_at(pic, cx + dx, cy + dy)
        assert pic._dragging, (dx, dy)
        assert pic._handle_x(inner, pic.split()) == pytest.approx(cx, abs=0.01), (dx, dy)
        assert pic.split() == pytest.approx((cx - inner.left()) / inner.width(), abs=1e-6)
        release_at(pic, cx + dx, cy + dy)


@pytest.mark.parametrize("f, step", [(1.0, -60.0), (0.0, 60.0)])
@pytest.mark.parametrize("k", KEYS)
def test_dragging_the_clamped_handle_follows_without_a_jump(make, k, f, step):
    pic = make(k, width=462)
    inner = pic._inner_rect()
    cx, cy = handle_centre_x(f), glass_mid_y()
    off = 7.0                                                      # grabbed 7 units off its centre
    pic.set_split(f)
    press_at(pic, cx + off, cy)
    assert pic._handle_x(inner, pic.split()) == pytest.approx(cx, abs=0.01)
    for i in range(1, 9):                                          # six steps of 60 units, then on
        drag_to(pic, cx + off + step * i)
        want = cx + step * i
        expected = min(max(want, inner.left()), inner.right())
        assert pic.split() == pytest.approx((expected - inner.left()) / inner.width(), abs=1e-6), i
        if inner.left() + 20 < want < inner.right() - 20:          # in the middle the handle is under the pointer
            assert pic._handle_x(inner, pic.split()) == pytest.approx(want, abs=0.01), i
    # back to where it began: the handle rests at the same spot again, no jump
    drag_to(pic, cx + off)
    assert pic._handle_x(inner, pic.split()) == pytest.approx(cx, abs=0.01)
    release_at(pic, cx + off)
    assert not pic._dragging


@pytest.mark.parametrize("f", [0.0, 1.0])
def test_pressing_the_line_itself_at_an_end_leaves_it_there(make, f):
    pic = make("on", width=462)
    left, top, right, bottom = glass()
    x = left if f == 0.0 else right
    pic.set_split(f)
    press_at(pic, x + (1.0 if f == 0.0 else -1.0), top + 20)       # on the line, high above the disc
    assert pic._dragging and pic.split() == f
    drag_to(pic, left + 225, top + 20)
    assert pic.split() == pytest.approx(0.5, abs=0.003)


@pytest.mark.parametrize("k", KEYS)
def test_pressing_the_glass_away_from_the_handle_jumps_the_line_even_from_an_end(make, k):
    pic = make(k, width=462)
    pic.set_split(1.0)
    left, top, right, bottom = glass()
    press_at(pic, left + 100, glass_mid_y())
    assert pic.split() == pytest.approx(100 / 450.0, abs=0.003)
    release_at(pic, left + 100)
    pic.set_split(0.0)
    press_at(pic, right - 100, glass_mid_y())
    assert pic.split() == pytest.approx(350 / 450.0, abs=0.003)


@pytest.mark.parametrize("k", KEYS)
def test_home_and_end_reach_exactly_the_ends_in_every_status(make, motion, k):
    pic = make(k)
    pic.set_split(0.37)
    key(pic, Qt.Key_Home)
    assert pic.split() == 0.0
    image(pic)                                                      # (paints at the end)
    key(pic, Qt.Key_End)
    assert pic.split() == 1.0
    image(pic)
    key(pic, Qt.Key_Left)                                           # one step in from the end ...
    assert pic.split() == pytest.approx(1.0 - la.STEP)
    for _ in range(40):
        key(pic, Qt.Key_Left)                                       # ... and all the way: the clamp lands on 0.0
    assert pic.split() == 0.0
    for _ in range(40):
        key(pic, Qt.Key_Right)
    assert pic.split() == 1.0


# ---- what the mini Live Assistant card inside the picture says
@pytest.mark.parametrize("k", KEYS)
def test_the_mini_card_chip_says_the_one_word(make, drawn_text, k):
    """The chip on the card drawn inside the picture shows the page's one word (STATUS_WORDS), never the
    live_context text ("Not available" ...): Remote Desktop says "Visible"."""
    st = status(k)
    pic = make(k)
    image(pic)
    word = la.STATUS_WORDS.get(k, la.STATUS_WORD_DEFAULT)
    texts = drawn_text.texts
    assert texts.count(word) == (1 if k == "on" else 2), (k, texts)   # "you" side always, "they" side when the card shows there
    if st.text != word:
        assert st.text not in texts, (k, st.text)
    for other in set(la.STATUS_WORDS.values()) | {la.STATUS_WORD_DEFAULT}:
        if other != word:
            assert other not in texts, (k, other)
    assert texts.count("Live Assistant") == (1 if k == "on" else 2)


def test_the_chip_words_are_the_page_words():
    assert {k: la.STATUS_WORDS.get(k, la.STATUS_WORD_DEFAULT) for k in KEYS} == {
        "on": "Hidden", "partial": "May be hidden", "off": "Visible", "failed": "Visible", "unavailable": "Visible"}


@pytest.mark.parametrize("k", KEYS)
def test_the_banners_are_swapped_between_the_sides_in_every_status(make, drawn_text, k):
    pic = make(k, owner="Aram")
    image(pic)
    texts = drawn_text.texts
    assert texts.count("Sharing") == 1 and texts.count("Aram’s screen") == 1
    assert texts.index("Sharing") < texts.index("Aram’s screen")     # the "you" side is painted before the "they" side
    assert "You see" in texts and "They see" in texts



# ═════════════════ the second review's fixes (workflow wf_c6e782e4-0cb) ═════════════════
@pytest.mark.parametrize("k", KEYS)
def test_a_real_status_change_under_the_pointer_does_not_teleport_the_line(make, motion, k):
    pic = make("on")
    pic.set_split(0.3)
    move(pic, 0.3)
    assert pic._hover
    pic.set_status(status("off" if k == "on" else k))
    assert pic.split() == pytest.approx(0.3)                          # the hand is on it: it stays
    leave(pic)
    pic.set_status(status("on" if k != "on" else "partial"))
    assert pic.split() == la.STOP_YOU                                 # alone again: the loop starts over


def test_stepping_with_the_keys_lands_exactly_on_the_ends(make):
    pic = make("on")
    for _ in range(5):
        key(pic, Qt.Key_PageDown)
    assert pic.split() == la.SPLIT_MIN                                # not 5.5e-17
    for _ in range(25):
        key(pic, Qt.Key_Right)
    assert pic.split() == la.SPLIT_MAX                                # not 0.9999999999999999
    pic.set_split(1e-9)
    assert pic.split() == la.SPLIT_MIN


@pytest.mark.parametrize("f", [la.SPLIT_MIN, la.SPLIT_MAX])
def test_the_line_at_an_end_leaves_the_rounded_corners_clean(make, f):
    """The line's halo is clipped to the glass's ROUNDED shape: the corners at
    the line's end stay bezel-dark (a square clip left a pale notch)."""
    pic = make("on")
    img = grab_at(pic, f)
    x_edge = la.BEZEL if f == la.SPLIT_MIN else la.BEZEL + la.INNER_W * la.K - 1
    y_top = la.LABEL_H + la.LABEL_GAP + la.BEZEL
    c = at(img, pic, x_edge + (1 if f == la.SPLIT_MIN else -1), y_top + 0.5)
    assert c.red() < 90 and c.green() < 90, c.name()                  # the bezel's ink, not a pale speck


def test_pointing_beside_the_monitor_is_not_hovering_it(make, motion):
    pic = make("on", width=700)
    inner = pic._inner_rect()
    s = pic._scale()
    beside = (inner.right() + la.BEZEL + 8) * s                       # empty widget to the right of the monitor
    mouse(pic, QEvent.MouseMove, beside, mid_y(pic), Qt.NoButton, Qt.NoButton)
    assert not pic._hover and pic.cursor().shape() == Qt.ArrowCursor
    on_bezel = (inner.right() + la.BEZEL - 2) * s
    mouse(pic, QEvent.MouseMove, on_bezel, mid_y(pic), Qt.NoButton, Qt.NoButton)
    assert pic._hover                                                 # the monitor itself, bezel included
    mouse(pic, QEvent.MouseButtonPress, beside, mid_y(pic))
    assert not pic._dragging


def test_the_status_that_could_not_be_checked_still_explains_itself(qapp):
    for wanted in (True, False):
        st = la._unverified_status(wanted)
        assert st.tip and "share" in st.tip
        assert st.key == ("unavailable" if wanted else "off")
