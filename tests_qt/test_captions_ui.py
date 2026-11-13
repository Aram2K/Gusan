"""Live captions on screen (founder 2026-11-11): the caption window (ui/captions_overlay.py), the controller that
brings it up with a meeting (ui/captions_controller.py), and the switches - the Meetings card's Live captions rows and
the live card's Captions switch."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

import captions
from tests_qt.conftest import make_stub_app
from tests_qt.test_page_meetings import _open, meetings_root, shell  # noqa: F401  (the Meetings page fixtures)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def glass_calls(monkeypatch):
    from ui import glass
    calls = []
    monkeypatch.setattr(glass, "exclude_from_capture", lambda w, enabled=True: calls.append(("exclude", enabled)))
    monkeypatch.setattr(glass, "set_click_through", lambda w, enabled=True: calls.append(("click_through", enabled)))
    monkeypatch.setattr(glass, "set_no_activate", lambda w, enabled=True: calls.append(("no_activate", enabled)))
    monkeypatch.setattr(glass, "animations_enabled", lambda: True)
    monkeypatch.setattr(glass, "foreground_monitor_name", lambda: "")
    return calls


def _overlay(clock=None):
    from ui.captions_overlay import CaptionsOverlay
    ov = CaptionsOverlay(None, clock=clock or Clock())
    ov._animate = True
    return ov


# ── the window ──
def test_it_never_takes_a_click_or_the_focus_and_hides_from_screen_sharing(qapp, glass_calls):
    ov = _overlay()
    flags = ov.windowFlags()
    for f in (Qt.Tool, Qt.FramelessWindowHint, Qt.WindowStaysOnTopHint, Qt.WindowTransparentForInput,
              Qt.WindowDoesNotAcceptFocus):
        assert flags & f
    assert ov.testAttribute(Qt.WA_TranslucentBackground) and ov.testAttribute(Qt.WA_ShowWithoutActivating)
    ov.start()
    assert ("click_through", True) in glass_calls and ("no_activate", True) in glass_calls
    assert glass_calls[-1] == ("exclude", True)                         # hidden from sharing by default
    ov.set_in_share(True)
    assert glass_calls[-1] == ("exclude", False)
    screen = QApplication.primaryScreen().availableGeometry()
    g = ov.geometry()
    assert abs(g.center().x() - screen.center().x()) <= 1                # the middle
    assert g.bottom() < screen.bottom() and g.top() > screen.center().y()  # near the bottom
    ov.stop()
    assert not ov.isVisible()


def test_words_that_only_gain_punctuation_keep_their_fade_new_ones_come_one_after_another(qapp, glass_calls):
    clock = Clock()
    ov = _overlay(clock)
    ov.set_view({"them": [(1, ["so", "today"])], "me": []})
    from ui import captions_overlay as co
    first = [w.born for w in ov._sides["them"].lines[0].words]
    assert first[1] - first[0] == pytest.approx(co.STAGGER_S)            # staggered, not all at once
    clock.t += 1.0
    ov.set_view({"them": [(1, ["So,", "today", "we", "look"])], "me": []})
    words = ov._sides["them"].lines[0].words
    assert [w.text for w in words] == ["So,", "today", "we", "look"]
    assert [w.born for w in words[:2]] == first                           # kept: no flicker
    assert words[2].born == pytest.approx(clock.t) and words[3].born == pytest.approx(clock.t + co.STAGGER_S)
    many = ["w%d" % i for i in range(30)]
    ov.set_view({"them": [(1, many)], "me": []})
    assert max(w.born for w in ov._sides["them"].lines[0].words) <= clock.t + co.MAX_LAG_S + 1e-9   # never far behind
    clock.t += 1.0
    ov.set_view({"them": [(1, ["So,", "today", "we", "looked"])], "me": []})
    assert ov._sides["them"].lines[0].words[3].born == pytest.approx(clock.t)  # a revised word: at once


def test_a_line_rolling_out_fades_and_the_sides_never_overlap(qapp, glass_calls):
    clock = Clock()
    ov = _overlay(clock)
    ov._set_font(30)
    ov.resize(1000, 400)
    ov.set_view({"them": [(1, ["one"]), (2, ["two"])], "me": [(1, ["mine"])]})
    for _ in range(200):
        clock.t += 0.016
        ov._tick()
    them, me = ov._sides["them"], ov._sides["me"]
    assert me.height == pytest.approx(ov._line_h, abs=0.5)
    assert them.height == pytest.approx(2 * ov._line_h, abs=0.5)
    ov.set_view({"them": [(2, ["two"]), (3, ["three"])], "me": [(1, ["mine"])]})
    leaving = [ln for ln in them.lines if ln.leaving_at is not None]
    assert [ln.id for ln in leaving] == [1]
    for _ in range(100):
        clock.t += 0.016
        ov._tick()
    assert [ln.id for ln in them.lines] == [2, 3]                        # gone once faded
    assert not ov._frame.isActive()                                      # settled: no timer running
    ov.grab()                                                            # draws without an error


def test_two_lines_are_never_on_top_of_each_other(qapp, glass_calls):
    """Founder 2026-11-13: "sometimes the sentences are coming one on top of each other" - a new line's first word
    showed in the place the line before was still gliding out of."""
    from ui import captions_overlay as co
    clock = Clock()
    ov = _overlay(clock)
    for size in ("m", "xl"):
        ov.set_size(size)
        ov._set_font(ov.font_px(1080, 1920))
        ov.resize(1600, 500)
        ov.set_view({"them": [], "me": []})
        for _ in range(100):
            clock.t += 0.016
            ov._tick()
        first = "Oui, parce que tu configures ton ChatGPT".split()
        ov.set_view({"them": [(10, first)], "me": []})
        for _ in range(100):
            clock.t += 0.016
            ov._tick()
        worst = 0.0
        for step in range(60):
            if step in (0, 5, 10):
                ov.set_view({"them": [(10, first), (11, "Sois quelqu'un qui".split()[:1 + step // 5])], "me": []})
            clock.t += 0.016
            ov._tick()
            old, new = [ln for ln in ov._sides["them"].lines if ln.leaving_at is None]
            if max(ov._word_alpha(w, clock.t) for w in new.words) > 0.02:     # the new line shows ...
                worst = max(worst, ov._line_h - (old.y - new.y))             # ... only once the old one made room
        assert worst <= co.CLEAR_PX + 0.5, (size, worst)
        assert ov._line_h >= ov._px * 1.4                                      # room for the outlines
    ov.set_size("m")


def test_a_line_is_written_from_the_left_and_its_words_never_move(qapp, glass_calls):
    """Founder 2026-11-12: every word added pushed the words before it to the left."""
    clock = Clock()
    ov = _overlay(clock)
    ov.resize(1400, 300)
    ov.set_view({"them": [(1, ["Hello"])], "me": []})
    ov._tick()
    x0 = ov._sides["them"].lines[0].x
    ov.set_view({"them": [(1, ["Hello", "everyone", "here"])], "me": []})
    for _ in range(30):
        clock.t += 0.016
        ov._tick()
    assert ov._sides["them"].lines[0].x == x0 == ov._box_left()
    assert 0 < x0 < 1400 / 2                                               # a box centred on the screen


def test_the_look_follows_the_card(qapp, glass_calls):
    from ui.captions_overlay import caption_font, color_pair
    ov = _overlay()
    ov.set_style("bold", "blue")
    assert ov._colors == {"them": (255, 255, 255), "me": (140, 205, 255)}
    assert ov._font.weight() >= 800
    ov.set_style("nonsense", "nonsense")                                   # unknown: the defaults
    assert ov._font_key == "clean" and ov._colors["me"] == (255, 226, 132)
    assert color_pair("gold")[0] == (255, 214, 96)
    assert caption_font("classic", 20).pixelSize() == 20


def test_the_size_picked_makes_the_text_bigger_but_never_wider_than_the_screen(qapp, glass_calls):
    ov = _overlay()
    sizes = []
    for key, _name, _scale in captions.SIZES:
        ov.set_size(key)
        sizes.append(ov.font_px(1080, 1920))
    assert sizes == sorted(sizes) and len(set(sizes)) == len(sizes)     # Small < Medium < Large < Extra large
    ov.set_size("m")
    assert ov.font_px(1080, 1920) == 30                                  # Medium is the size it always was
    ov.set_size("xl")
    narrow = ov.font_px(1080, 700)                                       # a narrow window: it shrinks to fit
    from PySide6.QtGui import QFontMetricsF
    from ui.captions_overlay import BOX_SAMPLE, WIDTH_SHARE, caption_font
    width = QFontMetricsF(caption_font("clean", narrow)).horizontalAdvance(BOX_SAMPLE[:captions.MAX_CHARS])
    assert width <= 700 * WIDTH_SHARE
    narrow_sizes = []
    for key, _name, _scale in captions.SIZES:                            # cut down to fit: never smaller than
        ov.set_size(key)                                                 # the size below it
        narrow_sizes.append(ov.font_px(1080, 500))
    assert narrow_sizes == sorted(narrow_sizes)
    ov.set_size("nonsense")
    assert ov._size == "m"


def test_a_size_change_in_the_middle_of_a_meeting_applies_at_once(qapp, glass_calls):
    from types import SimpleNamespace
    from PySide6.QtCore import QRect
    ov = _overlay()
    # an ordinary 1080p screen (a CI runner's own is so small that every size is cut down to fit its width)
    screen = SimpleNamespace(geometry=lambda: QRect(0, 0, 1920, 1080), availableGeometry=lambda: QRect(0, 0, 1920, 1040))
    ov._screen = lambda: screen
    ov.start()
    before = (ov._px, ov.height())
    ov.set_size("xl")
    assert ov._px > before[0] and ov.height() > before[1]                # bigger text, a taller window
    ov.stop()


def test_while_words_wait_the_window_looks_often_so_each_comes_on_time(qapp, glass_calls):
    from ui.captions_overlay import CaptionsOverlay, PACE_POLL_MS, POLL_MS
    clock = Clock()
    feed = captions.CaptionFeed(lambda: {"captions_who": "both"}, clock=clock, start_thread=False)
    ov = CaptionsOverlay(feed, clock=clock)
    ov.start()
    feed.final("one two three four five six", "Them", paced=True)
    ov.pull()
    assert ov._poll.isActive() and ov._poll.interval() == PACE_POLL_MS
    for _ in range(100):                                                  # 5 s of the window looking
        clock.t += PACE_POLL_MS / 1000.0
        ov.pull()
    assert not feed.pacing() and ov._poll.interval() == POLL_MS           # all written: the slow look again
    ov.stop()


def test_without_animations_words_simply_appear(qapp, glass_calls):
    clock = Clock()
    ov = _overlay(clock)
    ov._animate = False
    ov.set_view({"them": [(1, ["hi"])], "me": []})
    assert ov._word_alpha(ov._sides["them"].lines[0].words[0], clock()) == 1.0


# ── the controller ──
class FakeSession(QObject):
    state_changed = Signal(str)
    live_line = Signal(str, str)
    live_partial = Signal(str, str)

    caption_piece = Signal(str, str, str)
    caption_line = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        self.state = "idle"
        self._session_language = "hy"
        self.realtime = True
        self.live_showing = True

    def captions_realtime(self):
        return self.realtime

    def captions_from_pieces(self):
        return not self.realtime or not self.live_showing


def _controller(monkeypatch, tier="pro", **cfg):
    from ui import captions_controller as cc
    app = make_stub_app(tier)
    app.cfg.update(dict({"captions_on": True, "captions_language": "", "captions_who": "both",
                         "captions_in_share": False}, **cfg))
    app.recorder = SimpleNamespace(get_auth_token=lambda: "tok")
    started = []
    import ui.captions_overlay as co
    monkeypatch.setattr(co.CaptionsOverlay, "start", lambda self: started.append(self))
    monkeypatch.setattr(co.CaptionsOverlay, "stop", lambda self: started.remove(self) if self in started else None)
    ctl = cc.CaptionsController(app)
    session = FakeSession()
    ctl.attach(session)
    return ctl, session, app, started


def test_it_comes_with_a_recording_and_goes_with_it(qapp, monkeypatch):
    ctl, session, app, started = _controller(monkeypatch)
    assert not ctl.running
    session.state = "recording"
    session.state_changed.emit("recording")
    assert ctl.running and len(started) == 1
    session.live_partial.emit("Hello um there", "Them")
    assert [ws for _i, ws in ctl.feed.view()["them"]] == [["Hello"]]       # the first word at once ...
    assert ctl.feed.lanes["them"].live == ["Hello", "there"] and ctl.feed.pacing()   # ... the next at reading pace
    session.caption_line.emit("Hello there.", "You", "en")
    assert ctl.feed.view()["me"]
    session.state = "processing"
    session.state_changed.emit("processing")
    assert not ctl.running and started == []


def test_on_best_accuracy_the_captions_take_the_accurate_pieces_not_the_live_stream(qapp, monkeypatch):
    ctl, session, app, started = _controller(monkeypatch)
    session.state = "recording"
    ctl.sync()
    session.realtime = False                                  # Best accuracy / a model on this computer
    session.live_partial.emit("live guess", "Them")
    session.caption_line.emit("Live guess.", "Them", "hy")
    assert ctl.feed.view()["them"] == []
    session.caption_piece.emit("Accurate words.", "Them", "hy")
    assert ctl.feed.lanes["them"].pending or ctl.feed.view()["them"]   # paced in
    session.realtime = True                                   # Fast: the live stream, the pieces are not shown
    before = list(ctl.feed.lanes["me"].lines)
    session.caption_piece.emit("A piece.", "You", "en")
    assert ctl.feed.lanes["me"].lines == before and not ctl.feed.lanes["me"].pending
    session.live_showing = False                              # ... until the live stream gives up
    session.caption_piece.emit("A piece.", "You", "en")
    assert ctl.feed.lanes["me"].pending or ctl.feed.lanes["me"].lines != before


def test_the_switches_apply_in_the_middle_of_a_meeting(qapp, monkeypatch, glass_calls):
    ctl, session, app, started = _controller(monkeypatch)
    session.state = "recording"
    ctl.sync()
    app.cfg["captions_on"] = False
    ctl.on_setting_changed("captions_on")
    assert not ctl.running
    app.cfg["captions_on"] = True
    ctl.on_setting_changed("captions_on")
    assert ctl.running
    app.cfg["captions_in_share"] = True
    shared = []
    monkeypatch.setattr(ctl.overlay, "set_in_share", lambda on: shared.append(on))
    ctl.on_setting_changed("captions_in_share")
    assert shared == [True]
    app.cfg["captions_size"] = "l"
    sized = []
    monkeypatch.setattr(ctl.overlay, "set_size", lambda key: sized.append(key))
    ctl.on_setting_changed("captions_size")
    assert sized == ["l"]


def test_free_never_gets_captions_and_translations_use_the_sign_in(qapp, monkeypatch):
    ctl, session, app, started = _controller(monkeypatch, tier="free")
    session.state = "recording"
    ctl.sync()
    assert not ctl.running
    ctl2, s2, app2, _ = _controller(monkeypatch, captions_language="en")
    import action_api
    asked = []
    monkeypatch.setattr(action_api, "interpret_phrase",
                        lambda text, target, token, source, context: asked.append((text, target, token, source)) or "ok")
    assert ctl2._translate("Բարեւ", "hy", "en", []) == "ok"
    assert asked == [("Բարեւ", "en", "tok", "hy")]
    assert ctl2._source_language() == "hy"


# ── the switches ──
def _live_cfg(app):
    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        app.cfg[key] = value
        return True
    app.set_setting = set_setting


def test_the_meetings_card_has_live_captions_with_its_rows_only_when_on(shell, meetings_root):
    win, app = shell("pro")
    _live_cfg(app)
    app.cfg.update(captions_on=False, captions_language="", captions_who="both", captions_in_share=False)
    page = _open(win)
    rail = page.list_view.rail
    assert rail.cap_row.isVisibleTo(rail) and rail.cap_row.pro_tag is not None
    assert not rail.cap_lang_row.isVisibleTo(rail)
    rail.cap_toggle.click()
    assert ("set_setting", "captions_on", True) in app.calls
    assert rail.cap_lang_row.isVisibleTo(rail) and rail.cap_who_row.isVisibleTo(rail)
    assert rail.cap_share_row.isVisibleTo(rail)
    assert rail.cap_lang.itemText(0) == "As spoken"
    rail.cap_lang.setCurrentIndex(rail.cap_lang.findData("en"))
    rail.cap_lang.activated.emit(rail.cap_lang.currentIndex())
    assert ("set_setting", "captions_language", "en") in app.calls
    rail.cap_who.setCurrentIndex(rail.cap_who.findData("them"))
    rail.cap_who.activated.emit(rail.cap_who.currentIndex())
    assert ("set_setting", "captions_who", "them") in app.calls
    rail.cap_share.click()
    assert ("set_setting", "captions_in_share", True) in app.calls
    # the look: tiles showing each font and colour pair
    assert rail.cap_font_row.isVisibleTo(rail) and rail.cap_colors_row.isVisibleTo(rail)
    assert [t.key for t in rail.cap_font.tiles] == [k for k, _n in captions.FONTS]
    assert rail.cap_font.value() == "clean" and rail.cap_colors.value() == "yellow"
    rail.cap_font.tiles[1].click()
    assert ("set_setting", "captions_font", "bold") in app.calls and rail.cap_font.value() == "bold"
    rail.cap_colors.tiles[2].click()
    assert ("set_setting", "captions_colors", "green") in app.calls
    assert all(t.accessibleName() for t in rail.cap_font.tiles + rail.cap_colors.tiles)
    rail.cap_font.grab()
    rail.cap_colors.grab()
    # the size: an "Aa" of each size, Medium picked
    assert rail.cap_size_row.isVisibleTo(rail)
    assert [t.key for t in rail.cap_size.tiles] == ["s", "m", "l", "xl"] and rail.cap_size.value() == "m"
    assert [t.accessibleName() for t in rail.cap_size.tiles] == ["Small", "Medium", "Large", "Extra large"]
    rail.cap_size.tiles[3].click()
    assert ("set_setting", "captions_size", "xl") in app.calls
    rail.cap_size.grab()

    def y(w):
        from PySide6.QtCore import QPoint
        return w.mapTo(rail, QPoint(0, 0)).y()
    assert y(rail.speech.combo) < y(rail.cap_toggle) < y(rail.opt_cloud)  # after Transcribes with, before the notes


def test_free_people_get_the_pro_window(shell, meetings_root):
    win, app = shell("free")
    page = _open(win)
    rail = page.list_view.rail
    rail.cap_toggle.click()
    assert ("_pro_upsell", "Live captions") in app.calls
    assert not rail.cap_toggle.isChecked()
    assert not any(c[:2] == ("set_setting", "captions_on") for c in app.calls)


def test_the_live_card_switch(shell, meetings_root):
    win, app = shell("pro")
    _live_cfg(app)
    app.cfg.update(captions_on=False, captions_who="both")
    page = _open(win)
    live = page.live_view
    assert live.cap_switch.accessibleName() == "Live captions"
    live.cap_switch.click()
    assert ("set_setting", "captions_on", True) in app.calls
    assert live.cap_switch.isChecked() and not live.cap_who.isHidden()
    live.cap_who.setCurrentIndex(live.cap_who.findData("me"))
    live.cap_who.activated.emit(live.cap_who.currentIndex())
    assert ("set_setting", "captions_who", "me") in app.calls
    # bigger in the middle of the meeting (founder 2026-11-13)
    assert not live.cap_size.isHidden() and live.cap_size.value() == "m"
    live.cap_size.tiles[2].click()
    assert ("set_setting", "captions_size", "l") in app.calls
    assert live.cap_size.sizeHint().width() <= 240                       # fits the live card's buttons
    live.cap_switch.click()
    assert ("set_setting", "captions_on", False) in app.calls
    assert live.cap_who.isHidden() and live.cap_size.isHidden()


def test_a_new_live_line_never_flashes_a_window_of_its_own(shell, meetings_root):
    """Founder 2026-11-12: a small window flashed open and shut at every new line - the "YOU" label was shown before
    it had a parent."""
    from PySide6.QtCore import QEvent, QObject

    class Spy(QObject):
        def __init__(self):
            super().__init__()
            self.windows = []

        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Show and getattr(obj, "isWindow", None) and obj.isWindow():
                self.windows.append(type(obj).__name__)
            return False

    win, app = shell("pro")
    page = _open(win)
    pane = page.live_view.transcript
    pane.set_speaker_labels(True)
    spy = Spy()
    QApplication.instance().installEventFilter(spy)
    try:
        for i in range(3):
            pane.add_line("Line %d" % i, "You" if i % 2 else "Them")
            QApplication.processEvents()
    finally:
        QApplication.instance().removeEventFilter(spy)
    assert spy.windows == []
    assert [t.text() for _r, _w, t, _who in pane._lines][-3:] == ["Line 0", "Line 1", "Line 2"]
    assert all(r.parentWidget() is not None for r, _w, _t, _who in pane._lines)
