"""Smart Actions and Dictionary pages (ui/pages/smart_actions.py, ui/pages/dictionary.py):
they build for every plan, every control is named and reachable, and the key
interactions call the right app methods / settings."""
import time
from datetime import datetime, timedelta

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLineEdit, QPlainTextEdit, QTextEdit

from tests_qt.conftest import make_stub_app


# ── helpers ──────────────────────────────────────────────────────────────────
def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    return cond()


def live_app(tier="free", **cfg):
    """make_stub_app whose set_setting really writes cfg (like the controller)."""
    app = make_stub_app(tier)
    app.cfg.update({"translate_target": "en", "action_model": "rule_based"})
    app.cfg.update(cfg)

    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        app.cfg[key] = value
        return True
    app.set_setting = set_setting
    app.set_settings = lambda updates: {k: set_setting(k, v) for k, v in updates.items()}
    return app


def settings(app, key=None):
    out = [c for c in app.calls if c[0] == "set_setting"]
    return [c for c in out if c[1] == key] if key else out


def upsells(app):
    return [c for c in app.calls if c[0] == "_pro_upsell"]


class FakeDownloads(QObject):
    progress = Signal(str, int, int, int)
    finished = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        self.started = []

    def state(self, name):
        return "downloading" if name in self.started else "missing"

    def progress_of(self, name):
        return (42, 1, 2) if name in self.started else None

    def start(self, name):
        self.started.append(name)
        return True


@pytest.fixture
def tries(monkeypatch):
    """Free Smart Action tries left (entitlements reads usage.json otherwise)."""
    import entitlements
    left = {"n": 4}
    monkeypatch.setattr(entitlements, "smart_actions_remaining", lambda auth=None, cfg=None: left["n"])
    return left


@pytest.fixture
def no_history(monkeypatch):
    """Pages read history on a worker; most tests don't need it."""
    import ui.pages.dictionary as dct
    import ui.pages.smart_actions as sa
    monkeypatch.setattr(sa, "load_recent", lambda limit=5: [])
    monkeypatch.setattr(dct, "load_proof", lambda terms: "")


@pytest.fixture
def pages(qapp, no_history):
    made = []

    def build(module, app):
        mod = __import__("ui.pages." + module, fromlist=["create"])
        page = mod.create(app)
        page.resize(1048, 1100)
        page.show()
        QApplication.processEvents()
        made.append(page)
        return page

    yield build
    for p in made:
        p.hide()
        p.deleteLater()
    QApplication.processEvents()


@pytest.fixture
def shell(qapp, no_history):
    from ui import shell as shell_mod
    made = []

    def build(app):
        win = shell_mod.MainWindow(app)
        made.append(win)
        return win

    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def _interactive(page):
    return (page.findChildren(QAbstractButton) + page.findChildren(QLineEdit)
            + page.findChildren(QComboBox) + page.findChildren(QPlainTextEdit) + page.findChildren(QTextEdit))


def _assert_named_and_reachable(page):
    for w in _interactive(page):
        if isinstance(w, QLineEdit) and isinstance(w.parentWidget(), QComboBox):
            continue
        text = w.text() if isinstance(w, QAbstractButton) else ""
        assert (text or "").strip() or w.accessibleName().strip(), w
        fp = w.focusPolicy()
        assert int(getattr(fp, "value", fp)) & 1, (w, w.accessibleName())   # Qt.TabFocus


# ── both pages, every plan ───────────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["guest", "free", "pro"])
def test_pages_build_in_the_shell_for_every_plan(pages, shell, tries, tier):
    """Smart Actions builds in the shell (the Dictionary is no page of the shell
    any more: it is a Settings section, see test_settings_dictionary.py) and the
    standalone Dictionary page still builds for every plan."""
    from ui.pages.dictionary import DictionaryPage
    from ui.pages.smart_actions import SmartActionsPage
    from ui.shell import ErrorPage
    win = shell(make_stub_app(tier))           # the plain stub: no set_settings, no services
    win.show()
    sa = win.open("smart_actions")
    QApplication.processEvents()
    assert not isinstance(sa, ErrorPage) and isinstance(sa, SmartActionsPage)
    assert sa.header.title_label.text() == "Smart Actions"
    _assert_named_and_reachable(sa)
    dc = pages("dictionary", make_stub_app(tier))
    assert isinstance(dc, DictionaryPage) and dc.header.title_label.text() == "Dictionary"
    _assert_named_and_reachable(dc)
    # Hooks the shell calls never raise.
    win.refresh_account()
    win.on_setting_changed("smart_actions_hotkey")
    win.on_setting_changed("smart_actions_speech_model")
    win.on_setting_changed("vocabulary")
    win.open("smart_actions")
    dc.on_open()
    dc.on_setting_changed("vocabulary")


def test_rail_moves_under_the_page_in_a_narrow_window(shell, tries, monkeypatch):
    import ui.winfit
    # The offscreen screen is 800 px wide: keep the size the test asks for.
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    win = shell(live_app("free"))
    win.resize(1280, 820)
    win.show()
    sa = win.open("smart_actions")
    QApplication.processEvents()
    assert not sa.is_stacked()
    win.resize(960, 820)
    assert _wait(lambda: sa.is_stacked())


def test_standalone_dictionary_rail_follows_its_parent_width(qapp, no_history):
    """The standalone Dictionary page (not embedded) keeps its width rule: the
    rail sits beside the lists from NARROW_AT up and under them below it."""
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    from ui.pages.dictionary import NARROW_AT, DictionaryPage
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    page = DictionaryPage(live_app("free"), parent=host)
    lay.addWidget(page)
    host.resize(1048, 900)
    host.show()
    QApplication.processEvents()
    try:
        assert NARROW_AT == 940
        assert not page.is_stacked()
        host.resize(NARROW_AT - 1, 900)
        assert _wait(lambda: page.is_stacked())
        host.resize(NARROW_AT, 900)
        assert _wait(lambda: not page.is_stacked())
        host.resize(728, 900)
        assert _wait(lambda: page.is_stacked())
        host.resize(1280, 900)
        assert _wait(lambda: not page.is_stacked())
    finally:
        host.hide()
        host.deleteLater()
        QApplication.processEvents()


# ── Smart Actions: the hero ──────────────────────────────────────────────────
def test_free_with_tries_shows_the_shortcut_and_the_counter(pages, tries):
    app = live_app("free")
    page = pages("smart_actions", app)
    # No switch: the hero is the Smart Actions shortcut, drawn big.
    assert not hasattr(page, "switch")
    assert page.hero_keys.keys() == ["Alt", "S"] and not page.hero_keys.isHidden()
    assert page.hero_set_button.isHidden()
    assert page.hero_line.full_text() == "Press, say what you want, press again."
    assert page.hero_line.accessibleName() == "Press, say what you want, press again."
    # Too narrow for one line: it breaks after "want," (never "again." alone).
    assert page.hero_line.text() in ("Press, say what you want, press again.",
                                     "Press, say what you want,\npress again.")
    one = page.hero_line.fontMetrics().horizontalAdvance(page.hero_line.full_text())
    page.hero_line.resize(one - 20, page.hero_line.height())
    assert page.hero_line.text() == "Press, say what you want,\npress again."
    page.hero_line.resize(one + 4, page.hero_line.height())
    assert page.hero_line.text() == "Press, say what you want, press again."
    assert page.exact_keys.accessibleName() == "Alt + R"          # the real dictation keys
    assert page.exact_label.text() == "pastes your exact words."
    assert not page.state_pro.isHidden()
    assert page.state_tries.isHidden() and page.state_used.isHidden() and page.state_guest.isHidden()
    assert not settings(app) and not upsells(app)


def test_free_smart_actions_stay_unlimited(pages, tries):
    tries["n"] = 0
    app = live_app("free")
    page = pages("smart_actions", app)
    assert not page.state_pro.isHidden() and page.state_used.isHidden() and page.state_tries.isHidden()
    assert page.hero_keys.keys() == ["Alt", "S"]                  # the shortcut is still shown


def test_refresh_account_keeps_free_unlimited(pages, tries):
    app = live_app("free")
    page = pages("smart_actions", app)
    tries["n"] = 0
    page.refresh_account()
    assert not page.state_pro.isHidden() and page.state_used.isHidden()


def test_guest_signs_up_and_pro_is_unlimited(pages, tries):
    guest = live_app("guest")
    page = pages("smart_actions", guest)
    assert not page.state_guest.isHidden()
    page.signup_button.click()
    assert guest.calls.count(("show_auth_gate",)) == 1
    # An account is what brings the free tries: Try it asks to sign up, not for Pro.
    page.example("Email").try_button.click()
    assert guest.calls.count(("show_auth_gate",)) == 2
    assert not upsells(guest) and not settings(guest)
    assert not page.example("Email").practicing()

    pro = live_app("pro")
    page = pages("smart_actions", pro)
    assert not page.state_pro.isHidden()
    assert page.state_tries.isHidden() and page.state_used.isHidden()
    assert not upsells(pro) and not settings(pro)


def test_setting_changed_elsewhere_redraws_the_hero_keys(pages, tries):
    app = live_app("pro")
    page = pages("smart_actions", app)
    app.cfg["smart_actions_hotkey"] = "ctrl+shift+s"
    app.cfg["hotkey"] = "ctrl+shift+d"
    page.on_setting_changed("smart_actions_hotkey")
    assert page.hero_keys.accessibleName() == "Ctrl + Shift + S"
    assert page.shortcut.keys.accessibleName() == "Ctrl + Shift + S"
    page.on_setting_changed("hotkey")
    assert page.exact_keys.accessibleName() == "Ctrl + Shift + D"


def test_no_shortcut_set_shows_set_shortcut(pages, tries):
    """"" (it clashed with the dictation key at the upgrade): "Set shortcut"
    in the hero starts the rail's capture."""
    app = live_app("free", smart_actions_hotkey="")
    app.suspend_hotkeys = lambda on: app.calls.append(("suspend_hotkeys", on))
    page = pages("smart_actions", app)
    assert page.hero_keys.isHidden() and not page.hero_set_button.isHidden()
    assert page.hero_set_button.text() == "Set shortcut"
    assert page.shortcut.button.text() == "Set shortcut"
    page.hero_set_button.click()
    assert page.shortcut.is_capturing() and ("suspend_hotkeys", True) in app.calls
    QTest.keyClick(page.shortcut.button, Qt.Key_S, Qt.ControlModifier | Qt.ShiftModifier)
    assert settings(app, "smart_actions_hotkey") == [("set_setting", "smart_actions_hotkey", "ctrl+shift+s")]
    assert not page.hero_keys.isHidden() and page.hero_set_button.isHidden()
    assert page.hero_keys.accessibleName() == "Ctrl + Shift + S"
    assert page.shortcut.button.text() == "Change"


# ── Smart Actions: examples and Try it ───────────────────────────────────────
def test_four_examples_with_try_it(pages, tries):
    page = pages("smart_actions", live_app("free"))
    names = [c.name for c in page.examples]
    assert names == ["Email", "To-do list", "Translate", "Polish"]
    for card in page.examples:
        assert card.try_button.accessibleName() == "Try %s" % card.name
    assert page.example("Email").signoff_label.text() == "Best, Aram"
    # The line under the examples is gone (the hero says Alt+R pastes the exact words).
    from PySide6.QtWidgets import QLabel
    assert not [lb for lb in page.findChildren(QLabel) if "Say nothing special" in lb.text()]


def test_try_it_opens_a_practice_box(pages, tries):
    app = live_app("free")
    page = pages("smart_actions", app)
    card = page.example("Translate")
    card.try_button.click()
    assert not settings(app)                                      # nothing is switched on
    assert card.practicing() and card.paper.isHidden()
    assert card.practice.placeholderText() == "Press Alt + S and say it."
    assert card.try_button.text() == "Done"
    other = page.example("Email")
    other.try_button.click()                       # one practice box at a time
    assert other.practicing() and not card.practicing()
    other.try_button.click()
    assert not other.practicing() and not other.paper.isHidden()


def test_try_it_without_tries_still_opens_the_practice_box(pages, tries):
    """Free Smart Actions are not counted. Zero on the old counter does not open Pro."""
    tries["n"] = 0
    app = live_app("free")
    page = pages("smart_actions", app)
    page.example("Email").try_button.click()
    assert upsells(app) == []
    assert page.example("Email").practicing()
    assert not settings(app)


# ── Smart Actions: Recent ────────────────────────────────────────────────────
def test_recent_actions_keeps_smart_actions_only():
    from ui.pages.smart_actions import day_label, recent_actions
    now = datetime(2026, 10, 4, 12, 0)
    entries = [
        {"text": "Hi David, done.", "said": "Email David it's done", "action": "smart_auto",
         "timestamp": "2026-10-04 11:05:00"},
        {"text": "plain dictation", "timestamp": "2026-10-04 10:00:00"},
        {"text": "raw", "said": "raw", "action": "transcribe_only", "timestamp": "2026-10-04 09:00:00"},
        {"text": "Les fichiers.", "said": "Translate to French", "action": "smart_auto",
         "timestamp": "2026-10-03 17:30:00"},
        {"text": "x", "said": "y", "action": "smart_auto", "timestamp": "not a date"},
        "garbage",
    ]
    got = recent_actions(entries)
    assert [g["said"] for g in got] == ["Email David it's done", "Translate to French"]
    assert day_label(got[0]["when"], now) == "Today"
    assert day_label(got[1]["when"], now) == "Yesterday"
    assert day_label(datetime(2026, 9, 29, 9, 0), now) == "Tue 29 Sep"
    assert len(recent_actions(entries * 5, limit=3)) == 3


def test_recent_rows_copy_said_and_pasted(pages, tries):
    page = pages("smart_actions", live_app("free"))
    assert not page.recent_empty.isHidden()
    now = datetime.now()
    page.set_recent([
        {"when": now, "said": "Email David the invoice is attached", "pasted": "Hi David,\nthe invoice is attached."},
        {"when": now - timedelta(days=1), "said": "To-do: call Anna", "pasted": "- [ ] Call Anna\n- [ ] Book it"},
    ])
    rows = page.recent_widgets()
    assert len(rows) == 2 and page.recent_empty.isHidden()
    assert rows[0].pasted_label.full_text() == "Hi David, the invoice is attached."   # one line
    assert rows[1].pasted_label.full_text() == "☐ Call Anna ☐ Book it"             # no Markdown markers
    rows[0].exact_button.click()
    assert QGuiApplication.clipboard().text() == "Email David the invoice is attached"
    assert rows[0].exact_button.text() == "Copied"
    rows[0].copy_button.click()                                    # prose: unchanged, plain
    assert QGuiApplication.clipboard().text() == "Hi David,\nthe invoice is attached."
    rows[1].copy_button.click()                                    # HTML + nice plain text
    mime = QGuiApplication.clipboard().mimeData()
    assert mime.text() == "☐ Call Anna\n☐ Book it"
    assert mime.hasHtml() and "Call Anna" in mime.html()
    page.set_recent([])
    assert page.recent_widgets() == [] and not page.recent_empty.isHidden()


def _tab_sequence(start, wanted):
    """The order in which ``wanted`` widgets come in the Tab chain after ``start``."""
    out, w = [], start
    for _ in range(2000):
        w = w.nextInFocusChain()
        if w is start:
            break
        if w in wanted and w not in out:
            out.append(w)
    return out


def test_recent_rows_are_tabbed_where_they_are_on_screen(pages, tries):
    page = pages("smart_actions", live_app("free"))
    now = datetime.now()
    page.set_recent([{"when": now, "said": "a", "pasted": "A"}, {"when": now, "said": "b", "pasted": "B"}])
    rows = page.recent_widgets()
    want = [rows[0].exact_button, rows[0].copy_button, rows[1].exact_button, rows[1].copy_button,
            page.shortcut.button]                                  # the rail's first stop
    assert _tab_sequence(page.examples[-1].try_button, want) == want     # after the examples, before the rail
    assert rows[1].exact_button.accessibleDescription() == "Copies “b”"


def test_recent_ignores_a_stale_answer(pages, tries):
    page = pages("smart_actions", live_app("free"))
    old = page._recent_gen
    page.reload_recent()
    page._on_recent([{"when": datetime.now(), "said": "old", "pasted": "old"}], old)
    assert page.recent_widgets() == []


def test_recent_loads_from_history_on_a_worker(qapp, tries, monkeypatch):
    import history
    import ui.pages.smart_actions as sa
    saved = history.load()
    try:
        history.save_all([
            {"text": "Les fichiers sont dans le dossier.", "said": "Translate to French: the files",
             "action": "smart_auto", "language": "en", "backend": "local",
             "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            {"text": "just words", "language": "en", "backend": "local",
             "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        ])
        page = sa.create(live_app("free"))
        assert _wait(lambda: len(page.recent_widgets()) == 1)
        assert page.recent_widgets()[0].said_label.full_text() == "“Translate to French: the files”"
        page.deleteLater()
    finally:
        history.save_all(saved)


# ── Smart Actions: rail ──────────────────────────────────────────────────────
def test_rail_rows_in_order(pages, tries):
    page = pages("smart_actions", live_app("free"))
    # No "Translate into" row: people say the language ("to French").
    assert [r.label.text() for r in page.rail.rows()] == ["Shortcut", "Transcribes with", "Writes with"]
    assert not hasattr(page, "translate_combo")
    assert page.shortcut_row.pro_tag is None                       # changing the key is not Pro
    assert page.more_button.accessibleName() == "More options in Speech & AI"


@pytest.mark.parametrize("tier", ["guest", "free"])
def test_shortcut_is_not_pro(pages, tries, tier):
    """Anyone may change the key (only using it is gated)."""
    app = live_app(tier)
    app.suspend_hotkeys = lambda on: app.calls.append(("suspend_hotkeys", on))
    page = pages("smart_actions", app)
    ctl = page.shortcut
    assert ctl.keys.accessibleName() == "Alt + S" and ctl.button.text() == "Change"
    ctl.button.click()
    assert ctl.is_capturing() and not upsells(app)
    QTest.keyClick(ctl.button, Qt.Key_S, Qt.ControlModifier | Qt.ShiftModifier)
    assert settings(app, "smart_actions_hotkey") == [("set_setting", "smart_actions_hotkey", "ctrl+shift+s")]
    assert ctl.keys.accessibleName() == "Ctrl + Shift + S"
    assert page.hero_keys.accessibleName() == "Ctrl + Shift + S"
    assert [c for c in app.calls if c[0] == "suspend_hotkeys"] == [("suspend_hotkeys", True),
                                                                  ("suspend_hotkeys", False)]
    assert not upsells(app)


def test_shortcut_capture_refuses_clashes(pages, tries):
    app = live_app("pro")
    app.suspend_hotkeys = lambda on: app.calls.append(("suspend_hotkeys", on))
    page = pages("smart_actions", app)
    ctl = page.shortcut
    # No remove: D2 offers Change only.
    assert not hasattr(ctl, "remove_button")
    ctl.button.click()
    assert ctl.is_capturing()
    assert ("suspend_hotkeys", True) in app.calls            # Alt+R must not start a dictation
    QTest.keyClick(ctl.button, Qt.Key_R, Qt.AltModifier)     # the dictation key
    assert not settings(app, "smart_actions_hotkey")
    assert page.shortcut_problem() == "Alt + R starts dictation. Pick another."
    assert page.shortcut_row.hint_label.property("textRole") == "error"
    assert not ctl.is_capturing() and ctl.keys.accessibleName() == "Alt + S"   # back to what is set
    assert [c for c in app.calls if c[0] == "suspend_hotkeys"][-1] == ("suspend_hotkeys", False)
    ctl.button.click()
    assert page.shortcut_problem() == ""                     # a new try clears it
    QTest.keyClick(ctl.button, Qt.Key_A, Qt.ControlModifier | Qt.AltModifier)   # the Live Assistant's
    assert page.shortcut_problem() == "Ctrl + Alt + A opens Live Assistant. Pick another."
    assert not settings(app, "smart_actions_hotkey")
    ctl.button.click()
    QTest.keyClick(ctl.button, Qt.Key_Escape)                # Esc cancels
    assert not ctl.is_capturing() and not settings(app, "smart_actions_hotkey")
    ctl.button.click()
    QTest.keyClick(ctl.button, Qt.Key_Q)                     # a bare typing key: refused, keeps waiting
    assert ctl.is_capturing() and "Add Ctrl" in page.shortcut_problem()
    QTest.keyClick(ctl.button, Qt.Key_S, Qt.ControlModifier | Qt.ShiftModifier)
    assert settings(app, "smart_actions_hotkey") == [("set_setting", "smart_actions_hotkey", "ctrl+shift+s")]
    assert ctl.keys.accessibleName() == "Ctrl + Shift + S" and page.shortcut_problem() == ""


def test_shortcut_refused_by_the_controller(pages, tries):
    app = live_app("pro")
    app.set_setting = lambda key, value: app.calls.append(("set_setting", key, value)) or False
    page = pages("smart_actions", app)
    page.shortcut.captured.emit("ctrl+shift+j")
    assert page.shortcut_problem() == "Couldn't use Ctrl + Shift + J. Try another."
    assert page.shortcut.keys.accessibleName() == "Alt + S"


def test_open_with_focus_on_the_shortcut(shell, tries):
    win = shell(live_app("pro"))
    win.show()
    page = win.open("smart_actions", focus="shortcut")
    QApplication.processEvents()
    assert win.focusWidget() is page.shortcut.button


def test_writes_with_free_shows_this_computer_and_gates_cloud(pages, tries, monkeypatch):
    import local_llm
    monkeypatch.setattr(local_llm, "model_downloaded", lambda model_id="qwen_tiny": True)
    app = live_app("free", action_model="qwen_tiny")
    page = pages("smart_actions", app)
    assert page.checked_writer() == "local"
    assert page.opt_local.model_label.text() == "Qwen 1.5B · offline"
    assert page.opt_cloud.model_label.text() == "DeepSeek V4.1 Flash · fastest"
    assert page.opt_own.isHidden() and page.writer_row.hint_label.isHidden()
    page.opt_cloud.click()
    assert upsells(app) == [("_pro_upsell", "Smart Actions")]
    assert page.checked_writer() == "local"
    assert not settings(app, "action_model")


def test_writes_with_says_when_no_ai_model_writes(pages, tries, monkeypatch):
    """rule_based (the default, and a Free user's after Pro ends) runs the
    built-in rules: Qwen isn't drawn as the writer even when it's on disk."""
    import local_llm
    monkeypatch.setattr(local_llm, "model_downloaded", lambda model_id="qwen_tiny": True)
    app = live_app("free", action_model="rule_based")
    page = pages("smart_actions", app)
    assert page.checked_writer() is None
    assert page.writer_row.hint_label.text() == "Pick one to write with AI."
    page.opt_cloud.click()                                   # Free: the Pro window, still nothing chosen
    assert upsells(app) == [("_pro_upsell", "Smart Actions")] and page.checked_writer() is None
    page.opt_local.click()                                   # on disk: one click makes it the writer
    assert settings(app, "action_model") == [("set_setting", "action_model", "qwen_tiny")]
    assert page.checked_writer() == "local" and page.writer_row.hint_label.isHidden()


def test_writes_with_in_privacy_mode(pages, tries):
    # An own key is paused by Privacy mode, not missing: no "needs a key".
    app = live_app("pro", privacy_mode=True, action_model="api_mistral", action_api_key="k")
    page = pages("smart_actions", app)
    assert page.checked_writer() is None and page.opt_own.isHidden()
    assert not page.opt_cloud.isEnabled()
    assert page.writer_row.hint_label.text() == "Privacy mode keeps it on this computer."
    app.cfg["privacy_mode"] = False
    page.on_setting_changed("privacy_mode")
    assert page.checked_writer() == "own_key" and page.opt_cloud.isEnabled()
    assert page.opt_own.model_label.text() == "Mistral · Ministral 14B"


def test_writes_with_pro_switches_engines(pages, tries, monkeypatch):
    import local_llm
    monkeypatch.setattr(local_llm, "model_downloaded", lambda model_id="qwen_tiny": True)
    app = live_app("pro")
    page = pages("smart_actions", app)
    assert page.checked_writer() == "cloud"                 # Pro runs on Gusan Cloud by default
    page.opt_local.click()
    assert settings(app, "action_model") == [("set_setting", "action_model", "qwen_tiny")]
    assert page.checked_writer() == "local"
    page.opt_cloud.click()
    assert app.cfg["action_model"] == "managed" and page.checked_writer() == "cloud"


def test_writes_with_downloads_the_local_model_first(qapp, no_history, tries, monkeypatch):
    import local_llm
    import ui.pages.smart_actions as sa
    on_disk = {"yes": False}
    monkeypatch.setattr(local_llm, "model_downloaded", lambda model_id="qwen_tiny": on_disk["yes"])
    app = live_app("pro")
    page = sa.create(app)
    app.downloads = FakeDownloads()          # the service may appear after the page
    page.on_open()
    assert page.opt_local.model_label.text() == "Qwen 1.5B · 1.12 GB download"   # the size before the click
    page.opt_local.click()
    assert app.downloads.started == ["qwen_tiny"]
    assert page.opt_local.model_label.text() == "Qwen 1.5B · downloading 42%"
    assert not settings(app, "action_model")
    on_disk["yes"] = True
    app.downloads.started.clear()
    app.downloads.finished.emit("qwen_tiny", "ready", "")
    assert settings(app, "action_model") == [("set_setting", "action_model", "qwen_tiny")]
    assert page.checked_writer() == "local"
    page.deleteLater()


def test_writes_with_download_failure_and_change_of_mind(qapp, no_history, tries, monkeypatch):
    import local_llm
    import ui.pages.smart_actions as sa
    monkeypatch.setattr(local_llm, "model_downloaded", lambda model_id="qwen_tiny": False)
    app = live_app("pro")
    app.downloads = FakeDownloads()
    page = sa.create(app)
    page.opt_local.click()
    app.downloads.started.clear()
    app.downloads.finished.emit("qwen_tiny", "failed", "No internet connection.")
    assert page.writer_row.hint_label.text() == "No internet connection."
    assert not settings(app, "action_model")
    # Started again, then Gusan Cloud picked while it runs: the end of the
    # download doesn't switch back to this computer.
    page.opt_local.click()
    assert page.writer_row.hint_label.isHidden()
    page.opt_cloud.click()
    assert app.cfg["action_model"] == "managed"
    monkeypatch.setattr(local_llm, "model_downloaded", lambda model_id="qwen_tiny": True)
    app.downloads.finished.emit("qwen_tiny", "ready", "")
    assert app.cfg["action_model"] == "managed" and page.checked_writer() == "cloud"
    page.deleteLater()


def test_a_writer_picked_elsewhere_wins_over_a_running_download(qapp, no_history, tries, monkeypatch):
    import local_llm
    import ui.pages.smart_actions as sa
    on_disk = {"yes": False}
    monkeypatch.setattr(local_llm, "model_downloaded", lambda model_id="qwen_tiny": on_disk["yes"])
    app = live_app("pro", action_model="managed")
    app.downloads = FakeDownloads()
    page = sa.create(app)
    page.opt_local.click()                                   # starts the download
    assert app.downloads.started == ["qwen_tiny"]
    app.cfg["action_model"] = "api_mistral"                  # Settings › Speech & AI, meanwhile
    app.cfg["action_api_key"] = "k"
    page.on_setting_changed("action_model")
    on_disk["yes"] = True
    app.downloads.started.clear()
    app.downloads.finished.emit("qwen_tiny", "ready", "")
    assert not settings(app, "action_model") and app.cfg["action_model"] == "api_mistral"
    assert page.checked_writer() == "own_key"
    page.deleteLater()


def test_writes_with_shows_an_own_key(pages, tries):
    app = live_app("pro", action_model="api_mistral", action_api_key="k")
    page = pages("smart_actions", app)
    assert page.checked_writer() == "own_key" and not page.opt_own.isHidden()
    assert page.opt_own.model_label.text() == "Mistral · Ministral 14B"


def test_more_options_opens_speech_and_ai(shell, tries):
    app = live_app("free")
    win = shell(app)
    win.show()
    page = win.open("smart_actions")
    page.more_button.click()
    assert win.current_route() == "settings"


# ── Dictionary ───────────────────────────────────────────────────────────────
WORDS = ["Aram", "Aibuben", "PySide6"]


def test_my_words_chips_add_and_remove(pages):
    app = live_app("free", vocabulary=list(WORDS))
    page = pages("dictionary", app)
    assert [c.word for c in page.chips] == WORDS
    assert page.words_count.text() == "3 words"
    assert page.chips[0].remove_button.accessibleName() == "Remove Aram"
    page.word_input.setText("Kubernetes, Hakobyan")
    page.add_word_button.click()
    assert app.cfg["vocabulary"] == WORDS + ["Kubernetes", "Hakobyan"]
    assert app.cfg["initial_prompt"] == "Glossary: Aram, Aibuben, PySide6, Kubernetes, Hakobyan."
    assert page.word_input.text() == "" and page.word_error.isHidden()
    assert len(page.chips) == 5
    page.word_input.setText("pyside6")
    QTest.keyClick(page.word_input, Qt.Key_Return)
    assert not page.word_error.isHidden() and "already there" in page.word_error.text()
    assert app.cfg["vocabulary"] == WORDS + ["Kubernetes", "Hakobyan"]
    page.chips[0].remove_button.click()
    assert app.cfg["vocabulary"] == ["Aibuben", "PySide6", "Kubernetes", "Hakobyan"]


def test_removing_the_last_word_clears_the_prompt(pages):
    app = live_app("free", vocabulary=["Gusan"], initial_prompt="Glossary: Gusan.")
    page = pages("dictionary", app)
    page.chips[0].remove_button.click()
    assert app.cfg["vocabulary"] == [] and app.cfg["initial_prompt"] == ""
    assert not page.words_empty.isHidden() and page.chips_box.isHidden()
    assert page.words_count.text() == "0 words"


def test_check_new_words_problems():
    from ui.pages.dictionary import check_new_words
    assert check_new_words(["Aram"], "Anna; David\nAram") == (["Anna", "David"], "“Aram” is already there.")
    add, problem = check_new_words([], "x" * 60)
    assert add == [] and "under 50" in problem
    add, problem = check_new_words([], "one two three four five six seven")
    assert add == [] and "6 words" in problem
    add, problem = check_new_words(["w%d" % i for i in range(100)], "new")
    assert add == [] and "100" in problem


def test_always_replace_add_update_remove(pages):
    app = live_app("free", cleanup_replacements=[{"from": "goo san", "to": "Gusan"}])
    page = pages("dictionary", app)
    assert len(page.replace_rows) == 1 and page.replace_count.text() == "1"
    assert page.replace_rows[0].remove_button.accessibleName() == "Remove Gusan"
    page.when_input.setText("pie side six")
    page.write_input.setText("PySide6")
    QTest.keyClick(page.write_input, Qt.Key_Return)
    assert app.cfg["cleanup_replacements"] == [{"from": "goo san", "to": "Gusan"},
                                               {"from": "pie side six", "to": "PySide6"}]
    page.when_input.setText("Goo San")                       # same "When I hear": updated, not doubled
    page.write_input.setText("Gusan app")
    page.add_replace_button.click()
    assert app.cfg["cleanup_replacements"] == [{"from": "pie side six", "to": "PySide6"},
                                               {"from": "Goo San", "to": "Gusan app"}]
    page.when_input.setText("only this")
    page.add_replace_button.click()
    assert not page.replace_error.isHidden() and len(app.cfg["cleanup_replacements"]) == 2
    page.replace_rows[0].remove_button.click()
    assert app.cfg["cleanup_replacements"] == [{"from": "Goo San", "to": "Gusan app"}]


def test_more_folds_out_always_remove(pages):
    app = live_app("free", cleanup_custom_hallucinations=["thanks for watching"])
    page = pages("dictionary", app)
    assert page.remove_card.isHidden() and page.more_button.count_label.text() == "1 phrase"
    page.more_button.click()
    assert not page.remove_card.isHidden()
    assert page.more_button.accessibleDescription() == "Expanded"
    page.phrase_input.setText("subtitles by the community")
    page.add_phrase_button.click()
    assert app.cfg["cleanup_custom_hallucinations"] == ["thanks for watching", "subtitles by the community"]
    assert page.more_button.count_label.text() == "2 phrases"
    page.phrase_input.setText("Thanks for watching!")
    page.add_phrase_button.click()
    assert not page.remove_error.isHidden() and len(app.cfg["cleanup_custom_hallucinations"]) == 2
    page.remove_rows[0].remove_button.click()
    assert app.cfg["cleanup_custom_hallucinations"] == ["subtitles by the community"]
    page.more_button.click()
    assert page.remove_card.isHidden()


def test_cloud_words_toggle_and_privacy(pages):
    app = live_app("free")
    page = pages("dictionary", app)
    assert page.cloud_toggle.isChecked() and page.cloud_toggle.accessibleName() == "Use my words with cloud speech"
    page.cloud_toggle.click()
    assert settings(app, "vocabulary_share_with_cloud") == [("set_setting", "vocabulary_share_with_cloud", False)]
    private = live_app("free", privacy_mode=True)
    page = pages("dictionary", private)
    assert not page.cloud_toggle.isEnabled() and not page.cloud_toggle.isChecked()
    assert page.cloud_row.hint_label.text() == "Off in Privacy mode"


def test_dictionary_redraws_on_setting_changed(pages):
    app = live_app("free", vocabulary=["Aram"])
    page = pages("dictionary", app)
    app.cfg["vocabulary"] = ["Aram", "Anna"]
    page.on_setting_changed("vocabulary")
    assert [c.word for c in page.chips] == ["Aram", "Anna"]


def test_proof_sentence(pages):
    from ui.pages.dictionary import find_proof, proof_html
    entries = [{"text": "Nothing here."},
               {"text": "Hello. Mariam, the PySide6 build for Aibuben is ready. Thanks <3"}]
    s = find_proof(entries, ["Mariam", "PySide6", "Aibuben"])
    assert s == "Mariam, the PySide6 build for Aibuben is ready."
    assert find_proof(entries, []) == "" and find_proof(entries, ["Zed"]) == ""
    html = proof_html("A <b> & PySide6", ["PySide6"])
    assert "&lt;b&gt; &amp;" in html and html.count("<span") == 1
    app = live_app("free", vocabulary=["Mariam"])
    page = pages("dictionary", app)
    assert page.proof_row.isHidden()
    page.set_proof("Mariam is here.")
    assert not page.proof_row.isHidden() and "Mariam" in page.proof_label.text()


def test_proof_hides_when_its_word_goes(pages):
    app = live_app("free", vocabulary=["Mariam", "Anna"])
    page = pages("dictionary", app)
    page.set_proof("Mariam is here.")
    assert not page.proof_row.isHidden()
    page.chips[0].remove_button.click()                    # Mariam
    assert app.cfg["vocabulary"] == ["Anna"] and page.proof_row.isHidden()
    # Only the newest reload's answer is drawn.
    old = page._proof_gen
    page.reload_proof()
    page._on_proof("Anna said hi.", old)
    assert page.proof_row.isHidden()
    page._on_proof("Anna said hi.", page._proof_gen)
    assert not page.proof_row.isHidden()
    app.cfg["vocabulary"] = ["David"]                       # changed elsewhere (another window)
    page.on_setting_changed("vocabulary")
    assert page.proof_row.isHidden() and [c.word for c in page.chips] == ["David"]


def test_lists_rebuild_only_when_they_change(pages):
    app = live_app("free", vocabulary=["Aram", "Anna"], cleanup_replacements=[{"from": "goo san", "to": "Gusan"}],
                   cleanup_custom_hallucinations=["thanks for watching"])
    page = pages("dictionary", app)
    chips, rows, phrase_rows = list(page.chips), list(page.replace_rows), list(page.remove_rows)
    for key in ("initial_prompt", "vocabulary_share_with_cloud", "privacy_mode"):
        page.on_setting_changed(key)
    assert page.chips == chips and page.replace_rows == rows and page.remove_rows == phrase_rows
    assert page.more_button.accessibleName() == "More: Always remove, 1 phrase"


def test_zero_counts_are_not_shown(pages):
    page = pages("dictionary", live_app("free"))
    assert page.words_count.isHidden() and not page.words_empty.isHidden()
    assert page.replace_count.isHidden()


def test_keyboard_removal_keeps_the_place(pages):
    app = live_app("free", vocabulary=["Aram", "Anna", "David"])
    page = pages("dictionary", app)
    page.activateWindow()
    _wait(lambda: page.isActiveWindow(), 2.0)
    if not page.isActiveWindow():
        pytest.skip("the offscreen platform did not activate the window")
    page.chips[1].remove_button.setFocus(Qt.TabFocusReason)
    QTest.keyClick(page.chips[1].remove_button, Qt.Key_Space)   # removes Anna
    assert [c.word for c in page.chips] == ["Aram", "David"]
    assert page.chips[1].remove_button.hasFocus()             # the next ✕, not the window's start
    QTest.keyClick(page.chips[1].remove_button, Qt.Key_Space)   # removes David (the last one)
    assert page.chips[0].remove_button.hasFocus()


def test_dictionary_tab_order_follows_the_screen(pages):
    app = live_app("free", vocabulary=["Aram"], cleanup_replacements=[{"from": "goo san", "to": "Gusan"}])
    page = pages("dictionary", app)
    page.word_input.setText("Anna")
    page.add_word_button.click()                             # chips built later still tab in place
    want = [page.add_word_button] + [c.remove_button for c in page.chips] + [
        page.replace_rows[0].remove_button, page.when_input, page.more_button, page.cloud_toggle]
    assert _tab_sequence(page.word_input, want) == want


def test_proof_loads_from_history_on_a_worker(qapp):
    import history
    import ui.pages.dictionary as dct
    saved = history.load()
    try:
        history.save_all([{"text": "Ask Mariam about the Aibuben invoice.", "language": "en",
                           "backend": "local", "timestamp": "2026-10-04 10:00:00"}])
        page = dct.create(live_app("free", vocabulary=["Aibuben"]))
        assert _wait(lambda: not page.proof_row.isHidden())
        assert page.proof_label.accessibleName() == "Ask Mariam about the Aibuben invoice."
        page.deleteLater()
    finally:
        history.save_all(saved)
