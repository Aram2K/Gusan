"""Gusan 2.0.0 Smart Actions page, D6 / D8 / D9: the "Transcribes with" rail
row (cfg smart_actions_speech_model), the rich Try it practice box, and the
clash messages on Live Assistant that name Smart Actions."""
import time

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

from tests_qt.conftest import make_stub_app


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    return cond()


def live_app(tier="free", **cfg):
    """make_stub_app whose set_setting really writes cfg (like the controller)."""
    app = make_stub_app(tier)
    app.cfg.update({"translate_target": "en", "action_model": "rule_based", "smart_actions_speech_model": ""})
    app.cfg.update(cfg)

    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        app.cfg[key] = value
        return True
    app.set_setting = set_setting
    app.set_settings = lambda updates: {k: set_setting(k, v) for k, v in updates.items()}
    return app


def settings(app, key):
    return [c for c in app.calls if c[0] == "set_setting" and c[1] == key]


def upsells(app):
    return [c for c in app.calls if c[0] == "_pro_upsell"]


class FakeDownloads(QObject):
    progress = Signal(str, int, int, int)
    finished = Signal(str, str, str)
    state_changed = Signal(str, str)

    def __init__(self, ready=("base",)):
        super().__init__()
        self.states = {m: "ready" for m in ready}
        self.started = []
        self.pct = {}

    def state(self, name):
        return self.states.get(name, "missing")

    def progress_of(self, name):
        return (self.pct[name], 1, 2) if name in self.pct else None

    def start(self, name):
        self.started.append(name)
        self.states[name] = "downloading"
        self.pct[name] = 42
        self.state_changed.emit(name, "downloading")
        return True

    def finish(self, name, state="ready", msg=""):
        self.states[name] = state
        self.pct.pop(name, None)
        self.state_changed.emit(name, state)
        self.finished.emit(name, state, msg)


@pytest.fixture
def tries(monkeypatch):
    import entitlements
    monkeypatch.setattr(entitlements, "smart_actions_remaining", lambda auth=None, cfg=None: 4)


@pytest.fixture
def page_for(qapp, tries, monkeypatch):
    import ui.pages.smart_actions as sa
    monkeypatch.setattr(sa, "load_recent", lambda limit=5: [])
    made = []

    def build(app):
        page = sa.create(app)
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


def _items(combo):
    return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]


# ── Transcribes with ────────────────────────────────────────────────────────
def test_transcribes_with_lists_same_whisper_and_cloud(page_for):
    import hardware
    page = page_for(live_app("free"))
    combo = page.speech_combo
    items = _items(combo)
    assert items[0] == ("", "Same as Dictation")
    assert [v for v, _t in items[1:-1]] == list(hardware.MODEL_INFO)
    assert ("small", "Balanced · Whisper Small") in items
    assert items[-1] == ("cloud", "Gusan Cloud · Gemini 2.5 Flash  PRO")     # a plain PRO for Free users
    assert combo.accessibleName() == "Transcribes with"
    assert combo.currentData() == "" and combo.plain_text() == "Same as Dictation"
    assert page.speech_line.text() == "Whisper Base"      # the stub dictates with base (no "Same as" twice)
    assert page.speech_row.hint_label.isHidden()


def test_pro_sees_no_pro_mark_and_picks_gusan_cloud(page_for):
    app = live_app("pro")
    page = page_for(app)
    combo = page.speech_combo
    assert _items(combo)[-1] == ("cloud", "Gusan Cloud · Gemini 2.5 Flash")
    combo.setCurrentIndex(combo.findData("cloud"))
    assert settings(app, "smart_actions_speech_model") == [("set_setting", "smart_actions_speech_model", "cloud")]
    assert combo.plain_text() == "Gusan Cloud"
    assert page.speech_line.text() == "Gusan Cloud · Gemini 2.5 Flash"
    assert not upsells(app)


def test_free_picking_gusan_cloud_opens_the_pro_window(page_for):
    app = live_app("free")
    page = page_for(app)
    combo = page.speech_combo
    combo.setCurrentIndex(combo.findData("cloud"))
    assert upsells(app) == [("_pro_upsell", "Fast cloud transcription")]
    assert not settings(app, "smart_actions_speech_model")
    assert combo.currentData() == ""                                         # back to what is set


def test_privacy_mode_greys_gusan_cloud_out(page_for):
    app = live_app("pro", privacy_mode=True, smart_actions_speech_model="cloud")
    page = page_for(app)
    combo = page.speech_combo
    cloud = combo.findData("cloud")
    assert not combo.choice_enabled(cloud)
    assert page.speech_row.hint_label.text() == "Privacy mode keeps it on this computer."
    # A "cloud" pick paused by Privacy mode runs like Dictation, and reads so.
    assert combo.currentData() == ""
    assert page.speech_line.text() == "Whisper Base"
    app.cfg["privacy_mode"] = False
    page.on_setting_changed("privacy_mode")
    assert combo.choice_enabled(cloud) and combo.currentData() == "cloud"
    assert page.speech_row.hint_label.isHidden()


def test_a_whisper_model_not_on_disk_downloads_and_is_saved(page_for):
    app = live_app("free")
    app.downloads = FakeDownloads(ready=("base",))
    page = page_for(app)
    combo = page.speech_combo
    combo.setCurrentIndex(combo.findData("small"))
    assert app.downloads.started == ["small"]
    assert settings(app, "smart_actions_speech_model") == [("set_setting", "smart_actions_speech_model", "small")]
    assert page.speech_line.text() == "Whisper Small · downloading 42%"
    app.downloads.pct["small"] = 80
    app.downloads.progress.emit("small", 80, 1, 2)
    assert page.speech_line.text() == "Whisper Small · downloading 80%"
    app.downloads.finish("small")
    assert page.speech_line.text() == "Whisper Small"
    assert combo.plain_text() == "Balanced"
    # On disk already: no download.
    combo.setCurrentIndex(combo.findData("base"))
    assert app.downloads.started == ["small"]
    assert page.speech_line.text() == "Whisper Base"
    combo.setCurrentIndex(0)
    assert app.cfg["smart_actions_speech_model"] == ""


def test_a_failed_download_says_so(page_for):
    app = live_app("pro")
    app.downloads = FakeDownloads(ready=())
    page = page_for(app)
    combo = page.speech_combo
    combo.setCurrentIndex(combo.findData("medium"))
    app.downloads.finish("medium", "failed", "No internet connection.")
    assert page.speech_row.hint_label.text() == "No internet connection."
    assert page.speech_line.text().startswith("Whisper Medium · ")
    assert "download" in page.speech_line.text()


def test_a_model_without_the_download_service(page_for):
    app = live_app("free", smart_actions_speech_model="small")
    page = page_for(app)
    assert page.speech_combo.currentData() == "small"
    assert page.speech_line.text() == "Whisper Small"                      # unknown on disk: no size
    app.cfg["smart_actions_speech_model"] = "tiny"                          # picked elsewhere
    page.on_setting_changed("smart_actions_speech_model")
    assert page.speech_combo.currentData() == "tiny"


def test_the_pro_mark_follows_the_plan(page_for):
    app = live_app("free")
    page = page_for(app)
    assert page.speech_combo.itemText(page.speech_combo.count() - 1).endswith("PRO")
    app.set_tier("pro")
    page.refresh_account()
    assert page.speech_combo.itemText(page.speech_combo.count() - 1) == "Gusan Cloud · Gemini 2.5 Flash"


def test_same_as_dictation_follows_the_dictation_model(page_for):
    app = live_app("pro", backend="managed", managed_provider="gemini")
    page = page_for(app)
    assert page.speech_line.text() == "Gemini 2.5 Flash"
    app.cfg.update(backend="local", whisper_model="small")
    page.on_setting_changed("backend")
    assert page.speech_line.text() == "Whisper Small"


def test_the_cloud_item_follows_the_gusan_cloud_provider(page_for):
    app = live_app("pro", managed_provider="gemini")
    page = page_for(app)
    combo = page.speech_combo
    assert combo.itemText(combo.findData("cloud")) == "Gusan Cloud · Gemini 2.5 Flash"
    app.cfg["managed_provider"] = "mistral"
    page.on_setting_changed("managed_provider")
    assert combo.itemText(combo.findData("cloud")) == "Gusan Cloud · Voxtral Mini"
    assert combo.currentData() == ""                                         # the pick is kept


# ── Try it: a rich paste ─────────────────────────────────────────────────────
def test_practice_box_takes_the_html_of_a_smart_action(page_for):
    page = page_for(live_app("free"))
    card = page.example("To-do list")
    card.try_button.click()
    box = card.practice
    assert box.accessibleName() == "To-do list practice box"
    assert box.placeholderText() == "Press Alt + S and say it."
    import rich_text
    md = "- [ ] Call David\n- [ ] Book the room"
    box.paste_rich(rich_text.to_plain(md), rich_text.to_html(md))
    assert box.toPlainText() == "☐ Call David\n☐ Book the room"
    box.clear()
    box.paste_rich("", "<ul><li>one</li><li>two</li></ul>")
    assert box.document().firstBlock().textList() is not None              # real bullets
    box.clear()
    box.paste_rich("just words")                                            # plain: as is
    assert box.toPlainText() == "just words"
    card.try_button.click()
    assert not card.practicing() and box.toPlainText() == ""


def test_practice_placeholder_without_a_shortcut(page_for):
    page = page_for(live_app("pro", smart_actions_hotkey=""))
    card = page.example("Email")
    card.try_button.click()
    assert card.practice.placeholderText() == "Set a shortcut, then say it."


# ── Live Assistant names Smart Actions on a clash ────────────────────────────
@pytest.fixture
def live_page(qapp, monkeypatch):
    import live_context
    from ui.pages import live_assistant
    real = live_context.hide_status
    monkeypatch.setattr(live_context, "hide_status",
                        lambda wanted, **kw: real(wanted, **dict(dict(supported=True, remote=False, mac=False), **kw)))
    monkeypatch.setattr(live_assistant, "load_sessions", lambda: [])
    made = []

    def build(app):
        host = QWidget()
        from ui import theme
        theme.apply(host)
        page = live_assistant.create(app)
        page.setParent(host)
        host.resize(1048, 1000)
        host.show()
        page.on_open()
        QApplication.processEvents()
        made.append(host)
        return page
    yield build
    for h in made:
        h.hide()
        h.deleteLater()
    QApplication.processEvents()


def test_live_assistant_clash_with_smart_actions(live_page):
    app = make_stub_app("pro")
    app.live_assist = None
    app.set_setting = lambda key, value: app.calls.append(("set_setting", key, value)) and False
    page = live_page(app)
    page._on_hotkey_captured("alt+s")
    assert page.shortcut.error() == "Alt + S runs Smart Actions. Pick another."
    page._on_hotkey_captured("alt+r")
    assert page.shortcut.error() == "Dictation uses Alt + R."
    page._on_hotkey_captured("ctrl+shift+j")
    assert page.shortcut.error() == "Couldn't use Ctrl + Shift + J."


# ── the shared picker ────────────────────────────────────────────────────────
def test_plain_name_combo_moved_to_components(qapp):
    from ui.components import model_combo
    from ui.pages import audio_to_text
    assert audio_to_text.PlainNameCombo is model_combo.PlainNameCombo
    assert audio_to_text.PLAIN_ROLE == model_combo.PLAIN_ROLE
    box = model_combo.combo(model_combo.PlainNameCombo(), "Accuracy", width=150)
    box.add_choice("Balanced · Whisper Small", "small", plain="Balanced")
    box.add_choice("Gusan Cloud · Gemini 2.5 Flash", "cloud", plain="Gusan Cloud")
    assert box.accessibleName() == "Accuracy" and box.width() == 150
    assert box.plain_text() == "Balanced" and box.currentText() == "Balanced · Whisper Small"
    box.set_choice_enabled(1, False)
    assert not box.choice_enabled(1) and box.choice_enabled(0)
    box.deleteLater()


def test_greying_a_choice_out_leaves_no_wrapper_behind(qapp):
    # PySide keeps a model.item() wrapper alive inside the model: after a
    # refill (clear()) it was stale, handed out for whatever Qt allocated at
    # that address next - an event in the Live Assistant's app-wide filter
    # crashed the app (seen in the E2E run after a plan change).
    import gc
    from PySide6.QtCore import Qt
    from ui.components import model_combo

    def wrappers():
        gc.collect()
        return sum(1 for o in gc.get_objects() if type(o).__name__ == "QStandardItem")
    before = wrappers()
    box = model_combo.PlainNameCombo()
    for value in ("", "small", "cloud"):
        box.add_choice(value or "Same as Dictation", value)
    box.set_choice_enabled(2, False)
    assert not box.choice_enabled(2) and box.choice_enabled(1)
    assert not (box.model().flags(box.model().index(2, 0)) & Qt.ItemIsSelectable)
    box.set_choice_enabled(2, True)
    assert box.choice_enabled(2)
    assert box.model().flags(box.model().index(2, 0)) & Qt.ItemIsSelectable
    box.set_choice_enabled(2, False)
    box.clear()                                   # a refill (the plan changed)
    box.add_choice("Same as Dictation", "")
    assert wrappers() == before
    assert box.choice_enabled(0) and box.choice_enabled(7)    # out of range: not greyed
    box.set_choice_enabled(7, False)                          # out of range: nothing happens
    box.deleteLater()
