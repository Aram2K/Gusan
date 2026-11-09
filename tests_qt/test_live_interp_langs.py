"""The searchable language picker (ui/components/lang_picker.py) and the live card's Interpreter row
(ui/live_interp.py DirectionRow, wired in ui/live_assist.py), offscreen and with fakes:

* the picker: filtering by name or code, Enter / Esc / a click, ``picked`` only for the person's
  pick, the current language ticked, its list kept out of screen shares where its window is;
* the row: "You [English ▾] ⇄ [Japanese ▾] Anna", a same-language pick swaps the two, the swap
  button, the cfg keys it writes (a running interpreter starts again once, the row stays on), the mic
  and headphones toggles (toggled, names, tooltips), nothing wider than the 480 px card.
"""
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from tests_qt.conftest import make_stub_app


def _spin(n=5):
    for _ in range(n):
        QApplication.processEvents()
        time.sleep(0.005)


def _close_popups():
    for _ in range(5):
        w = QApplication.activePopupWidget()
        if w is None:
            break
        w.close()
    QApplication.processEvents()


# ── fakes (as in tests_qt/test_interpreter_ui.py) ──
class FakeCtl:
    made = []

    def __init__(self, token_getter, on_event, settings):
        self.on_event = on_event
        self.settings = settings
        self.calls = []
        self.session = SimpleNamespace(info={"seconds_left": 15120, "allowance_seconds": 18000, "own_voice": True})
        self._dirs = {"hear": 1, "speak": 1}
        self.speakers_mode = False
        FakeCtl.made.append(self)

    def start(self):
        self.calls.append("start")
        self.on_event({"type": "session", "state": "running", "seconds_left": 15120,
                       "speakers_mode": False, "directions": ["hear", "speak"]})

    def stop(self, reason="user"):
        self.calls.append(("stop", reason))
        self.on_event({"type": "session", "state": "stopped", "reason": reason, "seconds_left": 15000})

    def set_direction(self, name, on):
        self.calls.append(("direction", name, on))
        self._dirs = {d: 1 for d in ("hear", "speak") if (d != name and d in self._dirs) or (d == name and on)}
        self.on_event({"type": "session", "state": "running", "directions": sorted(self._dirs)})

    def latency(self):
        return 1.8


class _Sig(QObject):
    changed = Signal(str)


def make_app(tier="pro", refuse=(), **cfg):
    from ui import interpreter_host as ih
    app = make_stub_app(tier)
    app.cfg.update({"language": "en", "interpreter_my_language": "", "interpreter_their_language": "ja",
                    "interpreter_hear": True, "interpreter_speak": True, "interpreter_hotkey": "ctrl+alt+i"})
    app.cfg.update(cfg)
    sig = _Sig()
    app._sig = sig
    app.sig_setting_changed = sig.changed

    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        if key in refuse:
            return False
        if app.cfg.get(key) != value or key not in app.cfg:
            app.cfg[key] = value
            sig.changed.emit(key)
        return True

    def set_settings(updates):
        app.calls.append(("set_settings", dict(updates)))
        return {k: set_setting(k, v) for k, v in updates.items()}
    app.set_setting = set_setting
    app.set_settings = set_settings
    app.show_main = lambda *a, **k: None
    app.show_tray_hint = lambda *a, **k: None
    app.show_live_assist = lambda: None
    host = ih.InterpreterHost(app, factory=lambda tg, oe, st: FakeCtl(tg, oe, st), voices=object())
    host.spawn = lambda fn, name="": fn()
    app.interpreter = host
    app.schedule_config_save = lambda: None
    app.save_config = lambda: None
    return app


@pytest.fixture
def card(qapp):
    from unittest import mock
    from ui import glass, live_assist
    made = []
    excl = mock.MagicMock(return_value=True)
    patches = [mock.patch.object(glass, "capture_exclusion_supported", return_value=True),
               mock.patch.object(glass, "is_remote_session", return_value=False),
               mock.patch.object(glass, "exclude_from_capture", excl),
               mock.patch.object(glass, "is_excluded_from_capture", return_value=True)]
    for pt in patches:
        pt.start()

    def build(tier="pro", **cfg):
        app = make_app(tier, **cfg)
        c = live_assist.LiveAssistOverlay(main_app=app)
        c.exclude_mock = excl
        made.append(c)
        return c, app

    yield build
    _close_popups()
    for c in made:
        c.hide()
        c.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()
    for pt in patches:
        pt.stop()


@pytest.fixture
def host_widget(qapp):
    made = []

    def build(style="glass", current="en", **attrs):
        from ui.components.lang_picker import LangPicker
        w = QWidget()
        for k, v in attrs.items():
            setattr(w, k, v)
        w.resize(320, 120)
        p = LangPicker(current, "They speak", style, w)
        p.move(10, 10)
        p.resize(p.sizeHint())
        w.show()
        _spin()
        made.append(w)
        return p
    yield build
    _close_popups()
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def _running(c, app):
    c.set_meeting_active(True, "Call with Anna", "Aram Adamyan, Anna Sato")
    c.btn_interp.click()
    assert app.interpreter.state == "running"
    return c.interp_row


# ── the picker ──
def test_filter_finds_by_name_or_code_best_first():
    from ui.components.lang_picker import filter_languages
    from ui import interpreter_host as ih
    items = ih.languages()
    assert [c for c, _ in filter_languages(items, "en")][0] == "en"             # the code first, then names
    assert "hy" in [c for c, _ in filter_languages(items, "en")]                # "Armenian" holds "en"
    assert [c for c, _ in filter_languages(items, "arm")] == ["hy"]
    assert [c for c, _ in filter_languages(items, "ja")][0] == "ja"
    assert [c for c, _ in filter_languages(items, "  JAPA ")] == ["ja"]
    assert filter_languages(items, "zzz") == []
    assert filter_languages(items, "") == items
    assert filter_languages([("es", "Español")], "espan") == [("es", "Español")]  # accents do not matter


def test_the_popup_searches_and_enter_picks_the_first_match(host_widget):
    from ui.components.lang_picker import CURRENT_ROLE, SEARCH_PLACEHOLDER
    p = host_widget("glass", "en")
    got = []
    p.picked.connect(got.append)
    p.open_popup()
    _spin()
    pop = p.popup
    assert pop.isVisible() and pop.isWindow() and pop.windowType() == Qt.Popup
    assert QApplication.focusWidget() is pop.search
    assert pop.search.placeholderText() == SEARCH_PLACEHOLDER and pop.search.accessibleName() == SEARCH_PLACEHOLDER
    assert pop.list.count() == 44 and pop.list.accessibleName() == "Languages"
    cur = pop.list.currentItem()
    assert cur.text() == "English" and cur.data(CURRENT_ROLE) is True               # the current one, ticked
    assert cur.data(Qt.AccessibleTextRole) == "English, selected"
    QTest.keyClicks(pop.search, "arm")
    assert pop.codes() == ["hy"]
    QTest.keyClick(pop.search, Qt.Key_Return)
    _spin()
    assert got == ["hy"] and not pop.isVisible()
    assert p.code() == "hy" and p.text() == "Armenian" and p.accessibleName() == "They speak: Armenian"


def test_esc_closes_and_nothing_is_picked(host_widget):
    p = host_widget("glass", "en")
    got = []
    p.picked.connect(got.append)
    p.open_popup()
    _spin()
    QTest.keyClicks(p.popup.search, "fr")
    QTest.keyClick(p.popup.search, Qt.Key_Escape)
    _spin()
    assert not p.popup.isVisible() and got == [] and p.code() == "en"
    p.open_popup()                                                    # opens again with every language
    _spin()
    assert p.popup.search.text() == "" and p.popup.list.count() == 44
    p.popup.hide()


def test_no_match_says_so_and_enter_does_nothing(host_widget):
    from ui.components.lang_picker import NO_MATCH
    p = host_widget("glass", "en")
    got = []
    p.picked.connect(got.append)
    p.open_popup()
    _spin()
    QTest.keyClicks(p.popup.search, "qqq")
    assert p.popup.empty.isVisibleTo(p.popup) and not p.popup.list.isVisibleTo(p.popup)
    assert p.popup.empty.text() == NO_MATCH
    QTest.keyClick(p.popup.search, Qt.Key_Return)
    assert got == [] and p.popup.isVisible()
    p.popup.hide()


def test_arrows_move_and_enter_picks_the_highlighted_row(host_widget):
    p = host_widget("glass", "en")
    got = []
    p.picked.connect(got.append)
    p.open_popup()
    _spin()
    QTest.keyClicks(p.popup.search, "an")                 # several matches
    first, second = p.popup.codes()[:2]
    QTest.keyClick(p.popup.search, Qt.Key_Down)
    QTest.keyClick(p.popup.search, Qt.Key_Return)
    _spin()
    assert got == [second] and second != first


def test_a_click_on_a_row_picks_it(host_widget):
    p = host_widget("glass", "en")
    got = []
    p.picked.connect(got.append)
    p.open_popup()
    _spin()
    lst = p.popup.list
    row = p.popup.codes().index("ja")
    item = lst.item(row)
    lst.scrollToItem(item)
    _spin()
    rect = lst.visualItemRect(item)
    QTest.mouseMove(lst.viewport(), rect.center())
    QTest.mouseClick(lst.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    _spin()
    assert got == ["ja"] and p.code() == "ja" and not p.popup.isVisible()


def test_a_click_on_the_picker_opens_it_and_keys_open_it_too(host_widget):
    p = host_widget("glass", "en")
    QTest.mouseClick(p, Qt.LeftButton, Qt.NoModifier, p.rect().center())
    _spin()
    assert p.popup.isVisible()
    p.popup.hide()
    _spin()
    p.setFocus(Qt.TabFocusReason)
    QTest.keyClick(p, Qt.Key_Space)
    _spin()
    assert p.popup.isVisible()
    p.popup.hide()


def test_set_code_is_quiet_and_set_codes_limits_the_list(host_widget):
    p = host_widget("app", "en")
    got = []
    p.picked.connect(got.append)
    p.set_code("hy")
    assert got == [] and p.code() == "hy" and p.text() == "Armenian"
    p.set_codes(["en", "ja", "fr"])
    p.open_popup()
    _spin()
    assert p.popup.codes() == ["hy", "en", "fr", "ja"]                # by name; the current one stays in it
    p.popup.hide()
    p.set_codes(None)
    assert len(p.items()) == 44


def test_picking_the_same_language_again_says_nothing(host_widget):
    p = host_widget("glass", "en")
    got = []
    p.picked.connect(got.append)
    p.open_popup()
    _spin()
    QTest.keyClick(p.popup.search, Qt.Key_Return)                    # the current row: English
    _spin()
    assert got == [] and not p.popup.isVisible()


def test_over_a_window_kept_out_of_shares_the_list_is_kept_out_too(host_widget):
    from unittest import mock
    from ui import glass
    with mock.patch.object(glass, "exclude_from_capture", return_value=True) as excl:
        p = host_widget("app", "en", hidden_from_share=True)
        p.open_popup()
        _spin()
        assert mock.call(p.popup, True) in excl.call_args_list
        p.popup.hide()
        excl.reset_mock()
        q = host_widget("app", "en")
        q.open_popup()
        _spin()
        assert excl.call_args_list == []
        q.popup.hide()


@pytest.mark.parametrize("dark", [False, True])
def test_the_glass_look_follows_the_card(host_widget, dark):
    from ui import live_assist
    p = host_widget("glass", "en")
    t = live_assist._THEMES["dark" if dark else "light"]
    p.set_theme(t, dark=dark)
    assert p._c["text"] == t["text"] and p._c["accent"] == t["accent"]
    assert t["text"].lower() in p.popup.styleSheet().lower()
    p.open_popup()
    _spin()
    img = p.popup.grab().toImage()
    assert not img.isNull()
    p.popup.hide()


# ── the row on the card ──
def test_the_row_reads_you_my_language_swap_their_language_and_their_name(card):
    c, app = card("pro")
    row = _running(c, app)
    assert row.isVisibleTo(c)
    assert row.me_label.text() == "You" and row.them_label.text() == "Anna"
    assert row.my_picker.code() == "en" and row.my_picker.text() == "English"
    assert row.their_picker.code() == "ja" and row.their_picker.text() == "Japanese"
    assert row.my_picker.accessibleName() == "I speak: English"
    assert row.their_picker.accessibleName() == "They speak: Japanese"
    assert row.swap_btn.accessibleName() == "Swap the languages"
    assert row.speak_switch.isChecked() and row.hear_switch.isChecked()
    assert row.speak_switch.accessibleName() == "They hear me in Japanese: on"
    assert row.hear_switch.accessibleName() == "I hear them in English: on"
    assert row.speak_switch.toolTip().startswith("They hear me in Japanese: on")
    assert "my own voice" in row.speak_switch.toolTip()                # the "my voice" note lives here now
    assert row.hear_switch.toolTip().startswith("I hear them in English: on")


def test_without_a_name_the_other_side_is_them(card):
    c, app = card("pro")
    app.interpreter.toggle(source="hotkey")
    assert c.interp_row.them_label.text() == "Them"


def test_the_icon_toggles_emit_and_write_the_directions(card):
    c, app = card("pro")
    row = _running(c, app)
    seen = []
    row.toggled.connect(lambda name, on: seen.append((name, on)))
    row.speak_switch.click()
    assert seen == [("speak", False)]
    assert app.cfg["interpreter_speak"] is False and not row.speak_switch.isChecked()
    assert row.speak_switch.accessibleName() == "They hear me in Japanese: off"
    assert row.speak_switch.toolTip().startswith("They hear me in Japanese: off")
    assert ("direction", "speak", False) in FakeCtl.made[-1].calls
    row.hear_switch.click()                                            # the last one off: it stops
    assert seen[-1] == ("hear", False) and app.interpreter.state == "idle"
    # ...and keeps its setting: the chip turns it back on the same way (the row is hidden while off)
    assert app.cfg["interpreter_hear"] is True and app.cfg["interpreter_speak"] is False
    c.btn_interp.click()
    assert app.interpreter.state == "running" and row.isVisibleTo(c)
    assert FakeCtl.made[-1].settings.hear and not FakeCtl.made[-1].settings.speak


def test_a_pick_writes_its_key_and_the_interpreter_starts_again_once(card):
    c, app = card("pro")
    row = _running(c, app)
    before = len(FakeCtl.made)
    shown = []
    app.interpreter.state_changed.connect(lambda s: shown.append((s, row.isVisibleTo(c), c.btn_interp.is_on())))
    row.my_picker.open_popup()
    _spin()
    QTest.keyClicks(row.my_picker.popup.search, "french")
    QTest.keyClick(row.my_picker.popup.search, Qt.Key_Return)
    _spin(10)
    assert app.cfg["interpreter_my_language"] == "fr" and app.cfg["interpreter_their_language"] == "ja"
    assert ("set_settings", {"interpreter_my_language": "fr"}) in app.calls
    assert app.interpreter.state == "running" and len(FakeCtl.made) == before + 1
    assert FakeCtl.made[-1].settings.my_lang == "fr" and FakeCtl.made[-1].settings.their_lang == "ja"
    assert [s for s, *_ in shown] == ["stopping", "idle", "starting", "running"]
    assert all(vis and on for _s, vis, on in shown), shown        # the row and the chip never blink off
    assert row.my_picker.code() == "fr" and "FR ⇄ JA" in c.btn_interp.accessibleName()
    assert row.hear_switch.accessibleName() == "I hear them in French: on"


def test_picking_the_other_sides_language_swaps_them(card):
    c, app = card("pro")
    row = _running(c, app)
    before = len(FakeCtl.made)
    got = []
    row.languages_picked.connect(lambda m, t: got.append((m, t)))
    row.their_picker.open_popup()
    _spin()
    QTest.keyClicks(row.their_picker.popup.search, "english")
    QTest.keyClick(row.their_picker.popup.search, Qt.Key_Return)
    _spin(10)
    assert got == [("ja", "en")]
    assert app.cfg["interpreter_my_language"] == "ja" and app.cfg["interpreter_their_language"] == "en"
    assert ("set_settings", {"interpreter_my_language": "ja", "interpreter_their_language": "en"}) in app.calls
    assert len(FakeCtl.made) == before + 1                              # two keys, one restart
    assert row.my_picker.code() == "ja" and row.their_picker.code() == "en"
    # and from the left: picking their language there swaps back
    row.my_picker.open_popup()
    _spin()
    QTest.keyClicks(row.my_picker.popup.search, "en")
    QTest.keyClick(row.my_picker.popup.search, Qt.Key_Return)
    _spin(10)
    assert (app.cfg["interpreter_my_language"], app.cfg["interpreter_their_language"]) == ("en", "ja")


def test_the_swap_button_swaps(card):
    c, app = card("pro")
    row = _running(c, app)
    row.swap_btn.click()
    _spin(10)
    assert (app.cfg["interpreter_my_language"], app.cfg["interpreter_their_language"]) == ("ja", "en")
    assert row.my_picker.text() == "Japanese" and row.their_picker.text() == "English"
    assert row.speak_switch.accessibleName() == "They hear me in English: on"
    assert app.interpreter.state == "running"


def test_a_refused_write_puts_the_row_back(card):
    c, app = card("pro")
    app.set_settings = lambda updates: {k: False for k in updates}
    row = _running(c, app)
    row._picked("theirs", "de")
    _spin()
    assert app.cfg["interpreter_their_language"] == "ja" and row.their_picker.code() == "ja"


def test_a_change_from_the_page_refreshes_the_row(card):
    c, app = card("pro")
    row = _running(c, app)
    app.set_setting("interpreter_their_language", "ko")
    _spin(10)
    assert row.their_picker.code() == "ko" and row.speak_switch.accessibleName() == "They hear me in Korean: on"


def test_the_list_is_kept_out_of_shares_while_the_card_is(card):
    from ui import live_assist
    c, app = card("pro")
    row = _running(c, app)
    for w in QApplication.topLevelWidgets():                  # one card, as in the app (other tests' cards
        if isinstance(w, live_assist.LiveAssistOverlay) and w is not c:     # may still be alive)
            QApplication.instance().removeEventFilter(w._popup_privacy)
    c._exclusion_ok = True                                    # the card is hidden from shares
    c.exclude_mock.reset_mock()
    row.their_picker.open_popup()
    _spin()
    pop = row.their_picker.popup
    assert pop.isVisible()
    assert c.exclude_mock.call_args_list[-1] == ((pop, True),) and pop.property("la_private") is True
    pop.hide()
    c._private = False                                        # Visible: shown like the card
    c.exclude_mock.reset_mock()
    row.their_picker.open_popup()
    _spin()
    assert c.exclude_mock.call_args_list[-1] == ((pop, False),) and pop.property("la_private") is False
    pop.hide()


def test_the_list_closes_when_the_interpreter_stops(card):
    c, app = card("pro")
    row = _running(c, app)
    c.show()
    _spin()
    row.my_picker.open_popup()
    _spin()
    assert row.my_picker.popup.isVisible()
    app.interpreter.stop("user")
    _spin()
    assert not row.isVisibleTo(c) and not row.my_picker.popup.isVisible()


@pytest.mark.parametrize("look", ["light", "dark"])
def test_nothing_in_the_row_is_cut_or_wider_than_the_card(card, look):
    from ui import live_assist
    c, app = card("pro", live_assist_theme=look, interpreter_my_language="id", interpreter_their_language="pt")
    c.set_meeting_active(True, "Call with Maximiliano", "Aram Adamyan, Maximiliano-Alessandro Fernandez")
    c.show()
    c.btn_interp.click()
    _spin(10)
    c.interp_row.set_latency(1.8)
    _spin()
    row = c.interp_row
    assert row.height() == 34
    assert row.width() <= live_assist.CARD_W
    right = row.mapTo(c, QPoint(row.width(), 0)).x()
    assert right <= live_assist.SHADOW + live_assist.CARD_W
    for w in (row.me_label, row.my_picker, row.swap_btn, row.their_picker, row.them_label, row.speak_switch,
              row.hear_switch, row.delay):
        g = w.geometry()
        assert g.left() >= 0 and g.right() < row.width() and g.top() >= 0 and g.bottom() < row.height(), w
    for p in (row.my_picker, row.their_picker):                       # "Indonesian", "Portuguese" whole
        assert p.width() >= p.sizeHint().width(), (p.text(), p.width(), p.sizeHint().width())
    assert row.delay.width() >= row.delay.sizeHint().width()
    assert row.them_label.text().endswith("…") and row.them_label.width() <= 64 + 2
    order = [row.me_label, row.my_picker, row.swap_btn, row.their_picker, row.them_label, row.speak_switch,
             row.hear_switch, row.delay]
    xs = [w.geometry().left() for w in order]
    assert xs == sorted(xs)
    for a, b in zip(order, order[1:]):                                # no overlaps
        assert a.geometry().right() < b.geometry().left(), (a, b)
    # the header still holds its title
    assert c.lbl_title.width() > 0
