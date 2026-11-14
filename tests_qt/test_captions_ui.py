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
    for size in (100, 160):
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
    for pct in (80, 100, 130, 160):
        ov.set_size(pct)
        sizes.append(ov.font_px(1080, 1920))
    assert sizes == sorted(sizes) and len(set(sizes)) == len(sizes)     # each step bigger
    ov.set_size(100)
    assert ov.font_px(1080, 1920) == 30                                  # 100 % is the size it always was
    ov.set_size(180)
    narrow = ov.font_px(1080, 700)                                       # a narrow window: it shrinks to fit
    from PySide6.QtGui import QFontMetricsF
    from ui.captions_overlay import BOX_SAMPLE, WIDTH_SHARE, caption_font
    width = QFontMetricsF(caption_font("clean", narrow)).horizontalAdvance(BOX_SAMPLE[:captions.MAX_CHARS])
    assert width <= 700 * WIDTH_SHARE
    narrow_sizes = []
    for pct in range(captions.SIZE_MIN, captions.SIZE_MAX + 1, captions.SIZE_STEP):   # cut down to fit: never
        ov.set_size(pct)                                                               # smaller than the step below
        narrow_sizes.append(ov.font_px(1080, 500))
    assert narrow_sizes == sorted(narrow_sizes)
    ov.set_size("nonsense")
    assert ov._size == 100
    ov.set_size("xl")                                                    # (what 2.1.7 saved)
    assert ov._size == 160


def test_a_size_change_in_the_middle_of_a_meeting_applies_at_once(qapp, glass_calls):
    from types import SimpleNamespace
    from PySide6.QtCore import QRect
    ov = _overlay()
    # an ordinary 1080p screen (a CI runner's own is so small that every size is cut down to fit its width)
    screen = SimpleNamespace(geometry=lambda: QRect(0, 0, 1920, 1080), availableGeometry=lambda: QRect(0, 0, 1920, 1040))
    ov._screen = lambda: screen
    ov.start()
    before = (ov._px, ov.height())
    ov.set_size(150)
    assert ov._px > before[0] and ov.height() > before[1]                # bigger text, a taller window
    ov.stop()


def test_words_show_at_once_and_while_a_line_is_read_the_window_looks_often(qapp, glass_calls):
    """Founder 2026-11-14: never late on purpose - a piece shows whole at once; words wait only for a line to be
    read, and then the window looks often so they show the moment they may."""
    from ui.captions_overlay import CaptionsOverlay, PACE_POLL_MS, POLL_MS
    clock = Clock()
    feed = captions.CaptionFeed(lambda: {"captions_who": "both"}, clock=clock, start_thread=False)
    ov = CaptionsOverlay(feed, clock=clock)
    ov.start()
    feed.final("one two three four five six", "Them", paced=True)
    ov.pull()
    assert [ws for _i, ws in feed.view()["them"]] == [["one", "two", "three", "four", "five", "six"]]
    assert ov._poll.interval() == POLL_MS                                  # nothing waits: the slow look
    n = (captions.MAX_CHARS + 1) // 5
    for name in ("aaaa", "bbbb", "cccc"):                                  # the screen full of fresh lines ...
        feed.final(" ".join([name] * n), "Them", paced=True)
    ov.pull()
    feed.final(" ".join(["dddd"] * n), "Them", paced=True)                 # ... one more must wait for "aaaa"
    ov.pull()
    assert feed.pacing() and ov._poll.isActive() and ov._poll.interval() == PACE_POLL_MS
    for _ in range(80):
        clock.t += PACE_POLL_MS / 1000.0
        ov.pull()
    assert not feed.pacing() and ov._poll.interval() == POLL_MS
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
    assert [ws for _i, ws in ctl.feed.view()["them"]] == [["Hello", "there"]]   # as they are said
    assert not ctl.feed.pacing()
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
    app.cfg["captions_size"] = 130
    sized = []
    monkeypatch.setattr(ctl.overlay, "set_size", lambda value: sized.append(value))
    ctl.on_setting_changed("captions_size")
    assert sized == [130]


def test_free_never_gets_captions_and_translations_use_the_sign_in(qapp, monkeypatch):
    ctl, session, app, started = _controller(monkeypatch, tier="free")
    session.state = "recording"
    ctl.sync()
    assert not ctl.running
    ctl2, s2, app2, _ = _controller(monkeypatch, captions_language="en")
    import action_api
    asked = []
    limits = []
    monkeypatch.setattr(action_api, "interpret_phrase",
                        lambda text, target, token, source, context, timeout=None:
                        (asked.append((text, target, token, source)), limits.append(timeout)) and "ok")
    assert ctl2._translate("Բարեւ", "hy", "en", []) == "ok"
    assert asked == [("Բարեւ", "en", "tok", "hy")]
    # one that hangs holds up the translations behind it (they show in the order said): a short limit, then as spoken
    assert limits[0] is not None and max(limits[0]) <= 6
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
    # a card of their own under the meeting settings (founder 2026-11-14: "a section a bit as itself ... caption
    # language, caption source, and the show / hidden while screen share"), each row named for what it sets
    card = rail.captions
    assert card is not rail and card.title_label.text() == "Live captions" and card.isVisibleTo(page.list_view)
    assert card.width() == rail.width()

    def top(w):
        from PySide6.QtCore import QPoint
        return w.mapTo(page.list_view, QPoint(0, 0)).y()
    assert top(card) >= top(rail) + rail.height()                        # under it, never over it
    assert rail.cap_row.isVisibleTo(card) and rail.cap_row.pro_tag is not None
    assert rail.cap_row.label.text() == "Show captions"
    assert not rail.cap_lang_row.isVisibleTo(card)
    assert rail.cap_row.property("last") is True                         # (off: the only row, no line under it)
    rail.cap_toggle.click()
    assert ("set_setting", "captions_on", True) in app.calls
    assert rail.cap_lang_row.isVisibleTo(card) and rail.cap_who_row.isVisibleTo(card)
    assert rail.cap_share_row.isVisibleTo(card)
    assert [r.label.text() for r in card.rows()] == ["Show captions", "Caption language", "Caption source", "Size",
                                                     "Style", "Show while screen sharing"]
    assert (rail.cap_lang.accessibleName(), rail.cap_who.accessibleName()) == ("Caption language", "Caption source")
    assert rail.cap_share_row.property("last") is True and rail.cap_row.property("last") is False
    assert all(row.parent() is not rail._body for row in card.rows())     # none of them is left on the meeting card
    assert rail.cap_lang.itemText(0) == "As spoken"
    rail.cap_lang.setCurrentIndex(rail.cap_lang.findData("en"))
    rail.cap_lang.activated.emit(rail.cap_lang.currentIndex())
    assert ("set_setting", "captions_language", "en") in app.calls
    rail.cap_who.setCurrentIndex(rail.cap_who.findData("them"))
    rail.cap_who.activated.emit(rail.cap_who.currentIndex())
    assert ("set_setting", "captions_who", "them") in app.calls
    rail.cap_share.click()
    assert ("set_setting", "captions_in_share", True) in app.calls
    # the look, small (founder 2026-11-14: the tiles and the stacked slider took "too much space"): ONE Style line -
    # the font a dropdown, the colours four dots - and the size's slider beside its name
    assert rail.cap_style_row.isVisibleTo(card)
    assert not hasattr(rail, "cap_font_row") and not hasattr(rail, "cap_colors_row")
    assert [rail.cap_font.itemData(i) for i in range(rail.cap_font.count())] == [k for k, _n in captions.FONTS]
    assert [d.key for d in rail.cap_colors.dots] == [k for k, _n, _a, _b in captions.COLORS]
    assert rail.cap_font.value() == "clean" and rail.cap_colors.value() == "yellow"
    rail.cap_font.setCurrentIndex(1)
    rail.cap_font.activated.emit(1)
    assert ("set_setting", "captions_font", "bold") in app.calls and rail.cap_font.value() == "bold"
    rail.cap_colors.dots[2].click()
    assert ("set_setting", "captions_colors", "green") in app.calls and rail.cap_colors.value() == "green"
    assert rail.cap_font.accessibleName() == "Caption font"
    assert all(d.accessibleName() and "the call, then you" in d.toolTip() for d in rail.cap_colors.dots)
    rail.cap_font.set_value("nonsense")                                  # an unknown saved look: the first one
    rail.cap_colors.set_value("nonsense")
    assert rail.cap_font.value() == "clean" and rail.cap_colors.value() == "yellow"
    rail.cap_colors.grab()
    # each of the two lines is one row high (a name and its control side by side), and fits the card
    assert rail.cap_style_row.height() <= 64 and rail.cap_size_row.height() <= 64
    assert rail.cap_style_row.sizeHint().width() <= rail.width() and rail.cap_size_row.sizeHint().width() <= rail.width()
    assert rail.cap_size_row.hint_label.isHidden()
    # the slider's handle has its whole height (it was clipped: "broken and cut a bit")
    from ui import captions_style
    assert rail.cap_size.slider.height() >= captions_style.HANDLE + 4
    # the size: a slider - small A, big A, the percent (founder 2026-11-14: "maybe a slider, must be more clear")
    from PySide6.QtTest import QTest
    assert rail.cap_size_row.isVisibleTo(card)
    size = rail.cap_size
    assert size.value() == 100 and size.pct.text() == "100%" and size.slider.accessibleName() == "Caption size"
    assert size.small.text() == size.big.text() == "A" and size.big.font().pointSizeF() > size.small.font().pointSizeF()
    size.slider.setValue(14)                                             # dragged to 140 %
    assert size.pct.text() == "140%"
    assert not any(c[:2] == ("set_setting", "captions_size") for c in app.calls)   # not saved while it moves ...
    size.set_value(100)                    # the page refreshes meanwhile (review 2026-11-14: it undid the drag)
    assert size.value() == 140 and size.pct.text() == "140%"
    QTest.qWait(size._timer.interval() + 80)
    assert ("set_setting", "captions_size", 140) in app.calls           # ... once it rests
    size.slider.setSliderDown(True)                                      # held in the hand: the same
    size.set_value(100)
    assert size.value() == 140
    size.slider.setSliderDown(False)
    size.set_value(120)                                                  # at rest it follows the saved size
    assert size.value() == 120 and size.pct.text() == "120%"
    size.set_value(140)
    assert not hasattr(size, "moving")                                   # (a signal nobody listened to)
    size.grab()

    assert rail.cap_who_row.sizeHint().width() <= card.width()            # "Caption source" and its list on one line


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
    # bigger in the middle of the meeting (founder 2026-11-13), with a slider (2026-11-14)
    from PySide6.QtTest import QTest
    assert not live.cap_size.isHidden() and live.cap_size.value() == 100
    live.cap_size.slider.setValue(13)
    QTest.qWait(live.cap_size._timer.interval() + 80)
    assert ("set_setting", "captions_size", 130) in app.calls
    assert live.cap_size.sizeHint().width() <= 240                       # fits the live card's buttons
    assert live.cap_size.pct.isHidden()                                  # (compact: no percent there)
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
