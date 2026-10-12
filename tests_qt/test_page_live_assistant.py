"""Gusan 2.0 Live Assistant page (ui/pages/live_assistant.py): the truthful
one-word status chip beside the title (Hidden / May be hidden / Visible - the
explanation is only its tooltip), the hero (Open + shortcut, then the You see /
They see picture), Open (Pro window for Free, the card for Pro), the rail's
instant settings, the card sync, About my calls and Past sessions."""
import datetime
import json
import time

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QLabel, QPlainTextEdit,
                               QSlider, QWidget)

from tests_qt.conftest import make_stub_app

LIVE_CFG = {"live_assist_private": True, "live_assist_auto_answer": True,
            "live_assist_screen_auto": True, "live_assist_language": "",
            "live_assist_output_language": "", "live_assist_theme": "light",
            "live_assist_opacity": 0.96, "live_assist_context": ""}


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    return cond()


@pytest.fixture
def la(qapp, monkeypatch):
    """The page module with a deterministic hidden status (Windows 10 2004+,
    local session) and no disk scan for Past sessions."""
    import live_context
    from ui.pages import live_assistant
    real = live_context.hide_status
    monkeypatch.setattr(live_context, "hide_status",
                        lambda wanted, **kw: real(wanted, **dict(dict(supported=True, remote=False, mac=False), **kw)))
    monkeypatch.setattr(live_assistant, "load_sessions", lambda: [])
    return live_assistant


@pytest.fixture
def build(la):
    """build(tier, writes=True, **cfg) -> (page, app). With writes, set_setting
    writes cfg and tells the page (as AppController + the shell do)."""
    made = []

    def _build(tier="free", writes=True, refuse=(), **cfg):
        app = make_stub_app(tier)
        app.cfg.update(LIVE_CFG)
        app.cfg.update(cfg)
        app.live_assist = None
        holder = {}

        def set_setting(key, value):
            app.calls.append(("set_setting", key, value))
            if key in refuse:
                return False
            if writes:
                app.cfg[key] = value
                page = holder.get("page")
                if page is not None:
                    page.on_setting_changed(key)
            return True
        app.set_setting = set_setting
        app.show_main = lambda *a, **k: app.calls.append(("show_main",) + a + tuple(sorted(k.items())))
        host = QWidget()
        from ui import theme
        theme.apply(host)
        host.resize(1048, 1000)
        page = la.create(app)
        page.setParent(host)
        page.resize(1048, 1000)
        holder["page"] = page
        host.show()
        page.on_open()
        QApplication.processEvents()
        made.append(host)
        return page, app

    yield _build
    for h in made:
        h.hide()
        h.deleteLater()
    QApplication.processEvents()


def _calls(app, name):
    return [c for c in app.calls if c[0] == name]


# ── building ──
@pytest.mark.parametrize("tier", ["free", "pro", "guest"])
def test_the_page_opens_in_the_shell_without_an_error_card(qapp, la, tier):
    from ui import shell as shell_mod
    app = make_stub_app(tier)                   # the plain conftest stub: no live_assist keys at all
    win = shell_mod.MainWindow(app)
    try:
        win.show()
        page = win.open("live_assistant")
        QApplication.processEvents()
        assert isinstance(page, la.LiveAssistantPage)
        assert not isinstance(page, shell_mod.ErrorPage)
        assert page.header.title_label.text() == "Live Assistant"
        assert page.header.sub_label.text() == "Answers questions while you talk."
        assert page.status_chip.text() == "Hidden"
        assert page.status_chip.accessibleName() == "Hidden on screen share"
        assert page.fair_use.text() == ("For your own calls — not for exams or interviews "
                                        "where help isn’t allowed.")
    finally:
        win.hide()
        win.deleteLater()
        QApplication.processEvents()


def test_a_bare_controller_does_not_break_it(qapp, la):
    """Before Wave 4 the services may be missing: the page still builds and
    every action degrades quietly."""
    from types import SimpleNamespace

    def boom():
        raise RuntimeError("no auth yet")
    app = SimpleNamespace(cfg={}, is_pro=boom)
    page = la.create(app)
    try:
        page.on_open()
        page.open_button.click()                # no _pro_upsell: nothing happens, no crash
        page.hide_toggle.click()                # no set_setting: written into cfg
        assert app.cfg["live_assist_private"] is False
        page.local_change.click()
        page.see_all.click()
        page.refresh_account()
        page.on_setting_changed("privacy_mode")
    finally:
        page.deleteLater()


def test_free_and_pro_views(build):
    free, _ = build("free")
    assert free.open_pro_tag.text() == "PRO"
    assert free.local_option.radio.isChecked() and not free.cloud_option.radio.isChecked()
    assert free.cloud_option.pro_tag is not None and free.local_option.pro_tag is None
    assert free.cloud_option.model_label.text() == "DeepSeek V4.1 Flash"
    pro, _ = build("pro")
    assert pro.cloud_option.radio.isChecked() and not pro.local_option.radio.isChecked()
    assert pro.cloud_option.radio.accessibleName() == "Gusan Cloud · DeepSeek V4.1 Flash"
    assert pro.local_option.model_label.text().startswith("Qwen 1.5B")
    assert pro.open_pro_tag.text() == "PRO"     # calm Pro: the tag stays, nothing else


def test_every_control_has_a_name(build):
    page, _ = build("pro")
    page.more_button.click()                    # the More options rows too
    checked = 0
    for w in page.findChildren(QWidget):
        if isinstance(w, (QAbstractButton, QComboBox, QSlider, QPlainTextEdit)):
            name = w.accessibleName() or (w.text() if isinstance(w, QAbstractButton) else "")
            assert name.strip(), "%s has no accessible name" % type(w).__name__
            checked += 1
            if w.isVisibleTo(page):
                tab = int(Qt.TabFocus.value)
                assert int(w.focusPolicy().value) & tab, "%s %r is not keyboard reachable" % (type(w).__name__, name)
    assert checked >= 15
    assert page.picture.accessibleName() and "no card" in page.picture.accessibleDescription()


# ── hero and the status chip ──
def _force_status(monkeypatch, **defaults):
    """Make live_context.hide_status answer for a given computer (the page asks
    with just `wanted`); explicit kwargs at the call site still win."""
    import live_context
    real = live_context.hide_status
    monkeypatch.setattr(live_context, "hide_status",
                        lambda wanted, **kw: real(wanted, **dict(defaults, **kw)))


def _card_status(key, tone, icon, headline, tip="Because."):
    from live_context import HideStatus
    return HideStatus(key, headline, tone, icon, headline, tip)


def test_hero_says_what_is_true(build):
    page, app = build("free")
    assert page.status_chip.text() == "Hidden"
    assert page.status_chip.accessibleName() == "Hidden on screen share"
    assert page.status_chip.property("tone") == "ok"
    assert not page.picture.they_see_card()
    page.hide_toggle.click()                    # Hide from screen share: off
    assert ("set_setting", "live_assist_private", False) in app.calls
    assert page.status_chip.text() == "Visible"
    assert page.status_chip.accessibleName() == "Visible on screen share"
    assert page.status_chip.property("tone") == "red"
    assert page.status_chip.accessibleDescription() == "Shows on a shared screen and in recordings."
    assert page.picture.they_see_card()
    assert "with the card on it" in page.picture.accessibleDescription()
    page.hide_toggle.click()                    # and back
    assert page.status_chip.text() == "Hidden" and page.status_chip.property("tone") == "ok"


def test_the_words_are_hidden_may_be_hidden_and_visible(la):
    assert la.STATUS_WORDS == {"on": "Hidden", "partial": "May be hidden"}
    assert la.STATUS_WORD_DEFAULT == "Visible"
    for key in ("off", "failed", "unavailable"):
        assert la.STATUS_WORDS.get(key, la.STATUS_WORD_DEFAULT) == "Visible"


def test_the_removed_headline_pieces_are_gone(build, la):
    page, _ = build("pro")
    for name in ("headline", "mark", "status_tip"):
        assert not hasattr(page, name), name
    for name in ("StatusMark", "short_tip", "SHORT_TIPS", "SHORT_TIP_MAX"):
        assert not hasattr(la, name), name


def test_the_chip_sits_beside_the_title(build):
    page, _ = build("pro")
    chip, title = page.status_chip, page.header.title_label
    assert page.header.title_trail is chip
    assert page.header.isAncestorOf(chip)
    assert chip.isVisibleTo(page)
    t = title.mapTo(page, QPoint(0, 0))
    c = chip.mapTo(page, QPoint(0, 0))
    assert c.x() >= t.x() + title.width()                       # to the right of the title...
    assert abs((c.y() + chip.height() / 2) - (t.y() + title.height() / 2)) <= 6   # ...on its line
    assert c.y() + chip.height() <= page.header.height()        # inside the header, not the body


def test_the_hero_opens_with_the_actions_then_the_picture(build):
    page, _ = build("pro")
    hero = page.actions.parentWidget()
    assert hero is page.picture.parentWidget()
    assert hero.accessibleName() == "Live Assistant"
    lay = hero.layout()
    assert lay.indexOf(page.actions) == 0 and lay.indexOf(page.picture) > lay.indexOf(page.actions)
    a = page.actions.mapTo(page, QPoint(0, 0))
    p = page.picture.mapTo(page, QPoint(0, 0))
    assert a.y() + page.actions.height() <= p.y()               # on screen: the actions sit above the picture
    assert p.y() - (a.y() + page.actions.height()) >= 16        # with the 22 px gap (minus keycap shadow room)
    assert page.open_button.isVisibleTo(page) and page.hero_keys.isVisibleTo(page)
    # Nothing else rides above them in the hero: no sentence, no mark.
    texts = [w.text() for w in hero.findChildren(QLabel) if w.text() and w.isVisibleTo(page)]
    assert set(texts) <= {"or", "+", "Ctrl", "Alt", "A", "PRO"}, texts


def test_the_chip_changes_tone_with_the_status(build):
    page, app = build("pro")
    chip = page.status_chip
    seen = [(chip.text(), chip.property("tone"))]
    card = FakeCard(visible=True, state=_card_status("failed", "red", "eye", "Visible on screen share"))
    app.live_assist = card
    page.on_open()
    seen.append((chip.text(), chip.property("tone")))
    card.state = _card_status("partial", "amber", "eye_off", "May be hidden on screen share")
    card.hide_status_changed.emit("partial")
    seen.append((chip.text(), chip.property("tone")))
    card.state = _card_status("on", "ok", "incognito", "Hidden on screen share")
    card.hide_status_changed.emit("on")
    seen.append((chip.text(), chip.property("tone")))
    assert seen == [("Hidden", "ok"), ("Visible", "red"), ("May be hidden", "amber"), ("Hidden", "ok")]


STATES = [   # id, hide_status defaults, wanted, word, tone, name
    ("hidden", dict(), True, "Hidden", "ok", "Hidden on screen share"),
    ("mac-best-effort", dict(supported=False, mac=True, macos26=False), True,
     "May be hidden", "amber", "May be hidden on screen share"),
    ("switched-off", dict(), False, "Visible", "red", "Visible on screen share"),
    ("windows-refused", dict(excluded=False), True, "Visible", "red", "Visible on screen share"),
    ("remote-desktop", dict(remote=True), True, "Visible", "amber", "Can't hide in Remote Desktop"),
    ("not-supported", dict(supported=False), True, "Visible", "amber", "Can't hide on this computer"),
]


@pytest.mark.parametrize("defaults, wanted, word, tone, name", [x[1:] for x in STATES], ids=[x[0] for x in STATES])
def test_every_state_is_one_word_with_the_name_and_tip_on_the_chip(build, monkeypatch, defaults, wanted, word,
                                                                      tone, name):
    _force_status(monkeypatch, **defaults)
    page, _ = build("pro", live_assist_private=wanted)
    chip = page.status_chip
    assert chip.text() == word
    assert chip.text() in ("Hidden", "May be hidden", "Visible")
    assert chip.property("tone") == tone
    assert chip.accessibleName() == name == page._status.headline
    tip = page._status.tip.strip()
    assert tip and chip.accessibleDescription() == tip
    assert tip[:25] in chip.toolTip()               # the whole explanation lives in the tooltip
    assert chip.toolTip().startswith("<p")          # rich text, so it wraps
    assert page.picture.they_see_card() == (word != "Hidden")
    # One word on the page: no QLabel carries a status sentence, and the
    # word itself is the chip's alone.
    for lb in page.findChildren(QLabel):
        t = lb.text()
        if not t or chip.isAncestorOf(lb):
            continue
        assert t not in ("Hidden", "May be hidden", "Visible"), t
        low = t.lower()
        for bad in ("on screen share", "assume it shows", "windows refused", "remote desktop", "macos",
                    "recordings", "can't hide", "may be hidden"):
            assert bad not in low, (bad, t)
        assert tip[:25] not in t


def test_mac_best_effort_is_never_called_hidden(build, monkeypatch):
    _force_status(monkeypatch, supported=False, remote=False, mac=True, macos26=False)
    page, _ = build("pro")
    chip = page.status_chip
    assert chip.text() == "May be hidden" and chip.text() != "Hidden"
    assert chip.accessibleName() == "May be hidden on screen share"
    # No sentence on the page: the long macOS tip is the tooltip / description.
    assert "QuickTime" in chip.accessibleDescription()
    assert "QuickTime" in chip.toolTip()
    assert page.picture.they_see_card()
    assert "faded" in page.picture.accessibleDescription()
    assert "QuickTime" not in " ".join(lb.text() for lb in page.findChildren(QLabel))


def test_when_the_status_check_fails_it_never_says_hidden(build, monkeypatch):
    import live_context

    def boom(wanted, **kw):
        raise RuntimeError("glass is gone")
    monkeypatch.setattr(live_context, "hide_status", boom)
    page, _ = build("pro")                                  # wanted: hide
    assert page._status.key == "unavailable"
    assert page.status_chip.text() == "Visible" and page.status_chip.text() != "Hidden"
    assert page.status_chip.accessibleName() == "Can't confirm it's hidden"
    assert page.status_chip.property("tone") == "amber"
    assert page.picture.they_see_card()
    page.hide_toggle.click()                                # user turns hiding off
    assert page._status.key == "off"
    assert page.status_chip.text() == "Visible"
    assert page.status_chip.accessibleName() == "Visible on screen share"
    assert page.status_chip.property("tone") == "red"


def test_the_unverified_status_is_never_hidden(la):
    wanted, not_wanted = la._unverified_status(True), la._unverified_status(False)
    assert (wanted.key, wanted.headline) == ("unavailable", "Can't confirm it's hidden")
    assert (not_wanted.key, not_wanted.headline) == ("off", "Visible on screen share")
    for st in (wanted, not_wanted):
        assert la.STATUS_WORDS.get(st.key, la.STATUS_WORD_DEFAULT) == "Visible"
        assert st.tone in ("amber", "red") and st.tone != "ok"


def test_the_page_says_screen_share_only_in_the_rail(build):
    """The explanation is the chip's tooltip; the only visible words about
    'screen share' are the rail's switch label."""
    for tier in ("free", "pro"):
        page, _ = build(tier)
        page.more_button.click()
        found = []
        for w in page.findChildren(QWidget):
            texts = []
            if isinstance(w, QLabel):
                texts.append(w.text())
            elif isinstance(w, QAbstractButton):
                texts.append(w.text())
            elif isinstance(w, QPlainTextEdit):
                texts.append(w.placeholderText())
            for t in texts:
                if "screen share" in t.lower():
                    found.append(t)
        assert set(found) <= {"Hide from screen share"}, found
        assert found, "the rail's switch label is missing"
        assert "screen share" not in page.fair_use.text().lower()


def test_hero_keycaps_follow_the_live_assist_hotkey(build):
    page, app = build("pro")
    assert page.hero_keys.keys == ["Ctrl", "Alt", "A"]
    assert page.hero_keys.accessibleName() == "Ctrl + Alt + A"
    app.cfg["live_assist_hotkey"] = "alt+shift+l"
    page.on_setting_changed("live_assist_hotkey")
    assert page.hero_keys.keys == ["Alt", "Shift", "L"]
    assert page.shortcut.keys.accessibleName() == "Alt + Shift + L"
    app.cfg["live_assist_hotkey"] = ""
    page.on_setting_changed("live_assist_hotkey")
    assert not page.hero_keys.isVisibleTo(page) and not page.or_label.isVisibleTo(page)
    assert page.shortcut.none_label.isVisibleTo(page)


def test_open_is_the_pro_window_for_free(build):
    page, app = build("free")
    page.open_button.click()
    assert ("_pro_upsell", "Live Assistance") in app.calls
    assert not _calls(app, "toggle_live_assist")


def test_open_shows_the_card_for_pro(build):
    page, app = build("pro")
    page.open_button.click()
    assert ("toggle_live_assist",) in app.calls
    assert not _calls(app, "_pro_upsell")
    # Wave 4's show-only path wins when the controller has it.
    app.show_live_assist = lambda: app.calls.append(("show_live_assist",))
    page.open_button.click()
    assert ("show_live_assist",) in app.calls


def test_open_never_hides_an_existing_card(build, qapp):
    page, app = build("pro")
    card = FakeCard()
    app.live_assist = card
    page.open_button.click()
    assert card.opened == 1 and not _calls(app, "toggle_live_assist")


def test_narrow_column_wraps_the_keycaps(build):
    page, _ = build("pro")
    assert page.actions.one_line()
    page.resize(700, 1000)                  # the 960 px window: a ~300 px column
    QApplication.processEvents()
    assert not page.actions.one_line()
    assert page.picture.height() < page.picture.sizeHint().height()     # the picture scales down with it
    page.resize(1048, 1000)
    QApplication.processEvents()
    assert page.actions.one_line()


# ── the rail ──
def test_switches_apply_at_once(build):
    page, app = build("pro")
    page.auto_toggle.click()
    page.screen_toggle.click()
    assert ("set_setting", "live_assist_auto_answer", False) in app.calls
    assert ("set_setting", "live_assist_screen_auto", False) in app.calls
    page.look.buttons()[1].click()
    assert ("set_setting", "live_assist_theme", "dark") in app.calls


def test_a_refused_change_shows_the_old_value(build):
    page, app = build("pro", refuse=("live_assist_auto_answer", "live_assist_private"))
    page.auto_toggle.click()
    assert page.auto_toggle.isChecked()
    page.hide_toggle.click()
    assert page.hide_toggle.isChecked()
    assert page.status_chip.text() == "Hidden"
    assert page.status_chip.accessibleName() == "Hidden on screen share"


def test_languages(build):
    page, app = build("pro")
    assert page.call_lang.currentText() == "Same as Dictation"
    assert page.answer_lang.currentText() == "Same as the call"
    names = [page.call_lang.itemText(i) for i in range(page.call_lang.count())]
    assert names[:4] == ["Same as Dictation", "Detect automatically", "Mixed languages", "English"]
    assert "Armenian" in names                      # in the list, not first (no Armenian focus)
    i = page.call_lang.findData("fr")
    page.call_lang.setCurrentIndex(i)
    page.call_lang.activated.emit(i)
    assert ("set_setting", "live_assist_language", "fr") in app.calls
    j = page.answer_lang.findData("de")
    page.answer_lang.setCurrentIndex(j)
    page.answer_lang.activated.emit(j)
    assert ("set_setting", "live_assist_output_language", "de") in app.calls
    # A saved code the list doesn't have still shows (by its name).
    app.cfg["live_assist_output_language"] = "it"
    page.on_setting_changed("live_assist_output_language")
    assert page.answer_lang.currentText() == "Italian"
    # Today's saved "en" shows English, not "Same as the call".
    app.cfg["live_assist_language"] = "en"
    page.on_setting_changed("live_assist_language")
    assert page.call_lang.currentText() == "English"


def test_shortcut_change_captures_keys(build):
    page, app = build("pro")
    app.suspend_hotkeys = lambda on: app.calls.append(("suspend_hotkeys", on))
    cap = page.shortcut.capture                     # the shared HotkeyCaptureButton
    btn = page.shortcut.change
    assert btn is cap.button
    btn.click()
    assert cap.is_capturing() and page.shortcut.prompt.isVisibleTo(page)
    assert ("suspend_hotkeys", True) in app.calls   # the global hotkeys pause meanwhile
    QTest.keyClick(btn, Qt.Key_K)                   # a lone letter would fire while typing
    assert page.shortcut.error().startswith("Add Ctrl, Alt, Shift")
    assert cap.is_capturing()
    QTest.keyClick(btn, Qt.Key_K, Qt.ControlModifier | Qt.AltModifier)
    assert ("set_setting", "live_assist_hotkey", "ctrl+alt+k") in app.calls
    assert not cap.is_capturing() and ("suspend_hotkeys", False) in app.calls
    assert page.hero_keys.keys == ["Ctrl", "Alt", "K"]
    assert page.shortcut.keys.accessibleName() == "Ctrl + Alt + K"
    assert page.shortcut.error() == ""
    btn.click()
    QTest.keyClick(btn, Qt.Key_Q)
    assert page.shortcut.error()
    QTest.keyClick(btn, Qt.Key_Escape)
    assert not cap.is_capturing() and page.hero_keys.keys == ["Ctrl", "Alt", "K"]
    assert page.shortcut.error() == ""              # cancelled: the refusal goes with it


def test_a_refused_shortcut_says_so(build):
    page, app = build("pro", refuse=("live_assist_hotkey",), hotkey="alt+r")
    page.shortcut.change.click()
    QTest.keyClick(page.shortcut.change, Qt.Key_R, Qt.AltModifier)   # the Dictation shortcut
    assert ("set_setting", "live_assist_hotkey", "alt+r") in app.calls
    assert page.shortcut.error() == "Dictation uses Alt + R."
    assert page.hero_keys.keys == ["Ctrl", "Alt", "A"]
    assert page.shortcut.keys.accessibleName() == "Ctrl + Alt + A"    # back to what is set
    page.shortcut.change.click()
    assert page.shortcut.error() == ""                                # a new try clears it
    QTest.keyClick(page.shortcut.change, Qt.Key_J, Qt.ControlModifier | Qt.ShiftModifier)
    assert page.shortcut.error() == "Couldn't use Ctrl + Shift + J."


def test_answers_written_by_free(build, monkeypatch):
    import local_llm
    monkeypatch.setattr(local_llm, "model_downloaded", lambda *a, **k: False)
    page, app = build("free")
    assert page.local_option.model_label.text() == "Qwen 1.5B · not downloaded"
    monkeypatch.setattr(local_llm, "model_downloaded", lambda *a, **k: True)
    app.cfg["action_model"] = "qwen_tiny"
    page.on_setting_changed("action_model")
    assert page.local_option.model_label.text() == "Qwen 1.5B"
    page.cloud_option.radio.click()                 # Gusan Cloud is Pro
    assert ("_pro_upsell", "Live Assistance") in app.calls
    assert page.local_option.radio.isChecked()
    assert not _calls(app, "set_setting")
    page.local_change.click()
    assert ("show_main", "settings", ("section", "speech")) in app.calls


def test_answers_written_by_pro(build, monkeypatch):
    import local_llm
    monkeypatch.setattr(local_llm, "model_downloaded", lambda *a, **k: True)
    page, app = build("pro")
    assert page.cloud_option.radio.isChecked()
    page.local_option.radio.click()
    assert ("set_setting", "live_assist_writer", "local") in app.calls
    # Wave 4: live_engine honours live_assist_writer - a Pro user's
    # "This computer" sticks (it used to bounce back to Gusan Cloud with a hint).
    assert page.local_option.radio.isChecked()
    assert not page.writer_hint.isVisibleTo(page)
    page.cloud_option.radio.click()
    assert page.cloud_option.radio.isChecked()
    assert not page.writer_hint.isVisibleTo(page)


def test_pro_back_on_gusan_cloud_is_saved(build):
    page, app = build("pro")
    page.local_option.radio.click()
    assert app.cfg["live_assist_writer"] == "local"
    page.cloud_option.radio.click()                 # back on Gusan Cloud, saved
    assert ("set_setting", "live_assist_writer", "cloud") in app.calls
    assert app.cfg["live_assist_writer"] == "cloud"


def test_the_writer_hint_never_goes_stale(build, monkeypatch):
    import local_llm
    monkeypatch.setattr(local_llm, "model_downloaded", lambda *a, **k: True)
    page, app = build("pro", privacy_mode=True)
    page.cloud_option.radio.click()
    assert page.writer_hint.text() == "Privacy mode keeps answers on this computer."
    app.cfg["privacy_mode"] = False                 # Settings › Privacy: now it IS Gusan Cloud
    page.on_setting_changed("privacy_mode")
    assert page.cloud_option.radio.isChecked()
    assert not page.writer_hint.isVisibleTo(page)


def test_privacy_mode_keeps_pro_answers_local(build, monkeypatch):
    import local_llm
    monkeypatch.setattr(local_llm, "model_downloaded", lambda *a, **k: True)
    page, app = build("pro", privacy_mode=True)
    assert page.local_option.radio.isChecked()
    page.cloud_option.radio.click()
    assert page.writer_hint.text() == "Privacy mode keeps answers on this computer."
    assert page.local_option.radio.isChecked()
    assert not _calls(app, "set_setting")


def test_more_options(build):
    page, app = build("pro")
    assert not page.opacity_row.isVisibleTo(page)
    page.more_button.click()
    assert page.opacity_row.isVisibleTo(page) and page.reset_row.isVisibleTo(page)
    assert page.more_button.accessibleDescription() == "Expanded"
    page.reset_button.click()
    assert ("set_setting", "live_assist_pos", None) in app.calls
    page.opacity_slider.setValue(20)
    assert _wait(lambda: ("set_setting", "live_assist_opacity", 0.8) in app.calls)
    page.own_key_button.click()
    assert ("show_main", "settings", ("section", "speech")) in app.calls
    page.more_button.click()
    assert not page.opacity_row.isVisibleTo(page)


# ── the card ──
class FakeCard(QObject):
    """The bits of ui/live_assist.LiveAssistOverlay the page uses."""
    settings_changed = Signal(str, object)
    hide_status_changed = Signal(str)

    def __init__(self, visible=False, state=None):
        super().__init__()
        self.visible = visible
        self.state = state
        self.opened = 0

    def isVisible(self):
        return self.visible

    def hide_state(self):
        return self.state

    def open_card(self):
        self.opened += 1


def test_the_page_follows_the_card(build):
    page, app = build("pro")
    card = FakeCard()
    app.live_assist = card
    page.on_open()
    # The card announces before it writes cfg: the page uses the value it got.
    card.settings_changed.emit("live_assist_theme", "dark")
    assert page.look.index() == 1
    card.settings_changed.emit("live_assist_auto_answer", False)
    assert not page.auto_toggle.isChecked()
    card.settings_changed.emit("live_assist_screen_auto", False)
    assert not page.screen_toggle.isChecked()
    card.settings_changed.emit("live_assist_opacity", 0.7)
    assert page.opacity_slider.value() == 30
    card.settings_changed.emit("live_assist_context", "Board prep with Anna.")
    assert page.context_edit.toPlainText() == "Board prep with Anna."
    card.settings_changed.emit("live_assist_private", False)
    assert not page.hide_toggle.isChecked()
    assert page.status_chip.text() == "Visible"
    assert page.status_chip.accessibleName() == "Visible on screen share"
    assert not _calls(app, "set_setting")       # following never writes back


def test_an_open_card_tells_the_truth(build):
    from live_context import HideStatus
    page, app = build("pro")
    refused = HideStatus("failed", "Visible", "red", "eye", "Visible on screen share",
                         "Windows refused to hide it - assume it shows in a share.")
    card = FakeCard(visible=True, state=refused)
    app.live_assist = card
    page.on_open()
    assert page.status_chip.text() == "Visible"
    assert page.status_chip.accessibleName() == "Visible on screen share"
    assert page.status_chip.accessibleDescription() == "Windows refused to hide it - assume it shows in a share."
    assert page.status_chip.property("tone") == "red"
    card.state = None                           # card.hide_state() not known yet: what opening does
    card.hide_status_changed.emit("on")
    assert page.status_chip.text() == "Hidden"
    assert page.status_chip.accessibleName() == "Hidden on screen share"


def test_a_new_card_replaces_the_old_one(build):
    page, app = build("pro")
    old, new = FakeCard(), FakeCard()
    app.live_assist = old
    page.on_open()
    app.live_assist = new
    page.on_open()
    old.settings_changed.emit("live_assist_theme", "dark")
    assert page.look.index() == 0
    new.settings_changed.emit("live_assist_theme", "dark")
    assert page.look.index() == 1


# ── About my calls ──
def test_about_my_calls_saves_by_itself(build):
    page, app = build("pro")
    assert not page.saved_label.isVisibleTo(page)       # nothing written yet
    page.context_edit.setPlainText("I'm Aram. Calls are about the launch.")
    assert not page.saved_label.isVisibleTo(page)       # typing...
    assert _wait(lambda: ("set_setting", "live_assist_context",
                          "I'm Aram. Calls are about the launch.") in app.calls)
    assert page.saved_label.isVisibleTo(page) and page.saved_label.text() == "Saved"


def test_about_my_calls_grows_with_the_text(build):
    page, app = build("pro")
    ed = page.context_edit
    assert ed.height() == 66
    ed.setPlainText("I'm Aram from Aibuben. " * 12)          # several lines at this width
    assert _wait(lambda: ed.height() > 66)
    assert ed.height() <= 66 + 3 * ed.fontMetrics().lineSpacing()      # five lines at most
    ed.setPlainText("Short.")
    assert _wait(lambda: ed.height() == 66)


def test_about_my_calls_is_clipped(build):
    import live_context
    page, app = build("pro")
    page.context_edit.setPlainText("x" * (live_context.CONTEXT_CHARS + 50))
    page._save_context()
    saved = [c for c in app.calls if c[:2] == ("set_setting", "live_assist_context")][-1][2]
    assert len(saved) <= live_context.CONTEXT_CHARS + 1


# ── Past sessions ──
SESSIONS = [
    {"dir": "d1", "started": "", "duration_sec": 38 * 60, "text": "“Aram, when can we launch?”", "answers": 6},
    {"dir": "d2", "started": "", "duration_sec": 52 * 60, "text": "“Can we move the demo?”", "answers": 1},
    {"dir": "d3", "started": "", "duration_sec": 0, "text": "Live session", "answers": None},
    {"dir": "d4", "started": "", "duration_sec": 90 * 60, "text": "Weekly sync", "answers": 0},
    {"dir": "d5", "started": "", "duration_sec": 60, "text": "Old one", "answers": 2},
]


def test_past_sessions_list(build):
    page, app = build("pro")
    now = datetime.datetime.now()
    rows = [dict(s, started=(now - datetime.timedelta(days=i)).strftime("%Y-%m-%d 14:18:00"))
            for i, s in enumerate(SESSIONS)]
    page.set_sessions(rows)
    assert len(page.session_rows) == 4                  # the newest four; See all has the rest
    assert page.see_all.isVisibleTo(page) and not page.empty_row.isVisibleTo(page)
    first = page.session_rows[0]
    assert first.day_label.text() == "Today" and first.time_label.text() == "14:18"
    assert first.text_label.full_text() == "“Aram, when can we launch?”"
    assert first.meta_label.text() == "38 min · 6 answers"
    assert page.session_rows[1].day_label.text() == "Yesterday"
    assert page.session_rows[1].meta_label.text() == "52 min · 1 answer"
    assert page.session_rows[2].meta_label.text() == ""
    assert page.session_rows[3].meta_label.text() == "1 h 30 min · 0 answers"
    first.open_button.click()
    assert ("show_main", "meetings", ("meeting", "d1")) in app.calls
    page.see_all.click()
    assert ("show_main", "meetings") in app.calls


def test_a_narrow_column_keeps_the_question_readable(build):
    page, _ = build("pro")
    assert _wait(lambda: page._sessions is not None)    # the first (empty) load has landed
    page.set_sessions([dict(SESSIONS[0], started="2026-10-07 14:18:00")])
    row = page.session_rows[0]
    QApplication.processEvents()
    assert row.meta_label.isVisibleTo(page)
    page.resize(700, 1000)                              # the 960 px window
    QApplication.processEvents()
    assert not row.meta_label.isVisibleTo(page)         # length + answers step aside...
    assert "38 min · 6 answers" in row.open_button.accessibleName()   # ...but are still read out
    assert row.text_label.width() > 100
    page.resize(1048, 1000)
    QApplication.processEvents()
    assert row.meta_label.isVisibleTo(page)


def test_session_rows_follow_see_all_in_the_tab_order(build):
    page, _ = build("pro")
    assert _wait(lambda: page._sessions is not None)
    page.set_sessions([dict(s, started="2026-10-07 14:18:00") for s in SESSIONS[:2]])
    assert page.see_all.nextInFocusChain() is page.session_rows[0].open_button
    assert page.session_rows[0].open_button.nextInFocusChain() is page.session_rows[1].open_button


def test_no_sessions_yet(build):
    page, _ = build("free")
    page.set_sessions([])
    assert page.empty_row.isVisibleTo(page) and page.empty_label.text() == "No sessions yet."
    assert not page.see_all.isVisibleTo(page) and page.session_rows == []


def test_sessions_load_on_a_worker(build, la, monkeypatch):
    import threading
    gui = threading.get_ident()
    seen = {}

    def slow_load():
        seen["thread"] = threading.get_ident()
        return SESSIONS[:2]
    monkeypatch.setattr(la, "load_sessions", slow_load)
    page, _ = build("pro")
    assert _wait(lambda: len(page.session_rows) == 2)
    assert seen["thread"] != gui
    # A failing scan is just "No sessions yet."
    monkeypatch.setattr(la, "load_sessions", lambda: (_ for _ in ()).throw(OSError("disk")))
    page.reload_sessions()
    assert _wait(lambda: page.empty_row.isVisibleTo(page))


def test_load_sessions_reads_the_library(qapp):
    import meeting_store
    from ui.pages import live_assistant as la         # the real load_sessions (not the fixture's)
    root = meeting_store.meetings_dir()
    root.mkdir(parents=True, exist_ok=True)
    made = []

    def meeting(name, title, source="", log=None, notes=None, duration=0):
        d = root / name
        d.mkdir()
        made.append(d)
        (d / "transcript.txt").write_text("hello", encoding="utf-8")
        meeting_store.update_meta(d, title=title, duration_sec=duration, **({"source": source} if source else {}))
        if log is not None:
            (d / "live_answers.jsonl").write_text("".join(json.dumps(x) + "\n" for x in log), encoding="utf-8")
        if notes is not None:
            (d / "notes.md").write_text(notes, encoding="utf-8")
        return d
    try:
        meeting("20261007_141800", "Live session 14:18", "live_assist", duration=2280,
                log=[{"time": "14:20", "question": "Aram, when can we launch?"}, {"time": "14:30", "question": ""}])
        meeting("20261006_110000", "Live session 11:00", "live_assist", notes="# Summary\n- Demo moves to Thursday\n")
        meeting("20261005_090000", "Customer call", "live_assist")
        meeting("20261004_090000", "Q4 roadmap", "page")              # a normal meeting: not listed
        meeting("20250101_101500", "Live session 10:15")              # a 1.9 live session (no source)
        mine = {str(d) for d in made}
        out = [s for s in la.load_sessions() if s["dir"] in mine]   # other tests share the library
        assert [s["text"] for s in out] == ["“Aram, when can we launch?”", "Demo moves to Thursday",
                                            "Customer call", "Live session"]
        assert out[0]["answers"] == 2 and out[0]["duration_sec"] == 2280
        assert out[1]["answers"] is None
        assert out[0]["started"].startswith("2026-10-07 14:18")
    finally:
        import shutil
        for d in made:
            shutil.rmtree(d, ignore_errors=True)


def test_dates_and_lengths(la):
    now = datetime.datetime(2026, 10, 7, 18, 0)        # a Wednesday
    assert la.day_and_time("2026-10-07 14:18:00", now) == ("Today", "14:18")
    assert la.day_and_time("2026-10-06 11:00:00", now) == ("Yesterday", "11:00")
    assert la.day_and_time("2026-10-02 16:15:00", now) == ("Fri", "16:15")
    assert la.day_and_time("2026-09-29 10:00:00", now) == ("29 Sep", "10:00")
    assert la.day_and_time("2025-12-31 10:00:00", now) == ("31 Dec 2025", "10:00")
    assert la.day_and_time("garbage", now) == ("garbage", "")
    assert la.length_text(0) == "" and la.length_text(20) == "1 min"
    assert la.length_text(38 * 60) == "38 min" and la.length_text(3600) == "1 h"
    assert la.length_text(3900) == "1 h 5 min"
