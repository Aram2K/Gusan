"""Settings (ui/pages/settings.py) and Account & plan (ui/pages/account.py):
every view builds for Free, Pro and Guest, controls are named, and the key
interactions reach the right controller methods / settings."""
import time

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLineEdit, QPlainTextEdit

import hardware
from tests_qt.conftest import make_stub_app


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    return cond()


def _spin(n=5):
    for _ in range(n):
        QApplication.processEvents()


def snapshot(nvidia=False):
    if nvidia:
        return hardware.HardwareSnapshot(
            cpu_name="AMD Ryzen 7 7840HS", cores=8, ram_gb=31.3, ram_display_gb=32,
            gpu_name="NVIDIA GeForce RTX 4060 Laptop GPU", gpu_discrete=True, gpu_state="available",
            platform="win32", recommended=hardware.recommend_model(31.3, 8, "available", "win32"))
    return hardware.HardwareSnapshot(
        cpu_name="12th Gen Intel Core i7-1260P", cores=12, ram_gb=15.7, ram_display_gb=16,
        gpu_name="Intel Iris Xe Graphics", gpu_discrete=False, gpu_state="unsupported", platform="win32",
        recommended=hardware.recommend_model(15.7, 12, "unsupported", "win32"))


class FakeDownloads(QObject):
    progress = Signal(str, int, object, object)
    finished = Signal(str, str, str)
    removed = Signal(str, bool, str)
    state_changed = Signal(str, str)
    scanned = Signal()
    hardware_ready = Signal(object)

    def __init__(self, ready=("small", "qwen_tiny"), nvidia=False):
        super().__init__()
        self.states = {m: "ready" for m in ready}
        self.prog, self.errors, self.calls = {}, {}, []
        self.hardware = snapshot(nvidia)

    @property
    def recommendation(self):
        return self.hardware.recommended

    def state(self, n):
        return self.states.get(n, "missing")

    def start(self, n):
        self.calls.append(("start", n))
        if self.states.get(n) == "ready":
            return False
        self.states[n] = "downloading"
        self.state_changed.emit(n, "downloading")
        return True

    def finish(self, n, state="ready"):
        self.states[n] = state
        self.state_changed.emit(n, state)
        self.finished.emit(n, state, "")

    def remove(self, n):
        self.calls.append(("remove", n))
        return True

    def ready(self, kind=None):
        ids = (hardware.SPEECH_MODEL_IDS if kind == "whisper" else hardware.LOCAL_AI_MODEL_IDS if kind == "llm"
               else hardware.SPEECH_MODEL_IDS + hardware.LOCAL_AI_MODEL_IDS)
        return [i for i in ids if self.states.get(i) == "ready"]

    def progress_of(self, n):
        return self.prog.get(n)

    def error(self, n):
        return self.errors.get(n, "")

    def is_removing(self, n):
        return False

    def probe_hardware(self, cfg=None, force=False):
        self.calls.append(("probe", force))
        return self.hardware


class FakeMic:
    def __init__(self):
        self.level, self.error, self.started = 0.0, "", []

    def devices(self):
        return [(1, "Microphone (Realtek Audio)"), (3, "Headset (Jabra)")]

    def start(self, idx=None):
        self.started.append(idx)

    def stop(self):
        pass


def rich_app(tier="free", nvidia=False, **cfg):
    """make_stub_app plus instant-apply settings, the shared services and the
    account entry points."""
    app = make_stub_app(tier, name="Aram")
    app.cfg.update({"whisper_model": "small", "backend": "managed" if tier == "pro" else "local",
                    "action_model": "rule_based" if tier == "pro" else "qwen_tiny",
                    "mistral_api_key": "", "google_api_key": "", "action_api_key": "",
                    "save_history": True, "analytics_enabled": True, "overlay_private": True,
                    "live_assist_private": True, "vocabulary_share_with_cloud": True})
    app.cfg.update(cfg)
    app.auth.is_admin = False
    app.auth.user_id = "u1" if tier != "guest" else None
    app.auth.plan = "annual" if tier == "pro" else None
    app.auth.period_end = "2027-10-04T09:00:00Z" if tier == "pro" else None
    app.auth.get_access_token = lambda: "token"
    app.downloads = FakeDownloads(nvidia=nvidia)
    app.mic_monitor = FakeMic()

    def set_settings(changes):
        app.calls.append(("set_settings", dict(changes)))
        app.cfg.update(changes)
        return {k: True for k in changes}
    app.set_settings = set_settings
    app.set_setting = lambda k, v: set_settings({k: v})[k]
    for m in ("open_billing", "sign_out", "start_gpu_download", "retry_gpu", "cancel_gpu_download",
              "set_admin_tier_override"):
        app.__dict__[m] = (lambda name: (lambda *a, **k: app.calls.append((name,) + a) or True))(m)
    app.gpu_phase = lambda: None
    return app


@pytest.fixture
def env(qapp, monkeypatch):
    from ui.pages import settings as S
    import ui.winfit
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    monkeypatch.setattr(S, "autostart_state", lambda: (True, True))
    monkeypatch.setattr(S, "open_url", lambda url: opened.append(url))
    opened = []
    asked = []
    answers = []

    def fake_ask(parent, title, text, buttons, typed=None):
        asked.append({"title": title, "text": text, "buttons": [b[0] for b in buttons], "typed": typed})
        return answers.pop(0) if answers else None
    monkeypatch.setattr(S, "ask", fake_ask)
    made = []

    def build(tier="free", rich=True, **kw):
        from ui.shell import MainWindow
        app = rich_app(tier, **kw) if rich else make_stub_app(tier)
        win = MainWindow(app)
        win.resize(1280, 820)
        made.append(win)
        return win, app

    yield type("Env", (), {"build": staticmethod(build), "asked": asked, "answers": answers,
                           "opened": opened, "S": S})
    for w in made:
        w.hide()
        w.deleteLater()
    _spin()


def _settings(win, **opts):
    page = win.open("settings", **opts)
    page.ensure_built()
    _spin()
    return page


def _unnamed(widget):
    missing = []
    for b in widget.findChildren(QAbstractButton):
        if not (b.text() or "").strip() and not (b.accessibleName() or "").strip():
            missing.append("%s(%s)" % (type(b).__name__, b.objectName()))
    for f in widget.findChildren(QComboBox) + widget.findChildren(QLineEdit) + widget.findChildren(QPlainTextEdit):
        if not (f.accessibleName() or "").strip():
            missing.append(type(f).__name__)
    return missing


# ── building ──
@pytest.mark.parametrize("tier", ["free", "pro", "guest"])
def test_every_view_builds_without_an_error_card(env, tier):
    from ui.shell import ErrorPage
    from ui.pages.account import AccountPage
    from ui.pages.settings import SettingsPage
    win, app = env.build(tier)
    win.show()
    for section in ("general", "microphone", "speech", "privacy", "updates"):
        page = win.open("settings", section=section)
        _spin()
        assert isinstance(page, SettingsPage), page
        assert page.current_section() == section
    acct = win.open("account")
    assert isinstance(acct, AccountPage) and not isinstance(acct, ErrorPage)
    assert acct.header.title_label.text() == "Account & plan"
    page = win.page("settings")
    assert page.header.title_label.text() == "Settings"
    assert page.header.sub_label.text() == "Changes save on their own."
    assert all(page.is_built(s) for s in ("general", "microphone", "speech", "privacy", "updates"))


def test_plain_stub_app_without_services_degrades(env):
    """The Wave 1 stub (no set_settings, no downloads, no recorder): no error card,
    the current model shows In use, Download is disabled, toggles still apply."""
    from ui.shell import ErrorPage
    win, app = env.build("free", rich=False)
    page = _settings(win, section="speech")
    assert not isinstance(page, ErrorPage)
    card = page.model_cards["base"]                    # the stub's whisper_model
    assert card.is_selected() and "remove" in card.buttons
    assert not page.model_cards["small"].buttons["download"].isEnabled()
    page.t_notify.click()
    assert ("set_setting", "notify_when_ready", False) in app.calls
    acct = win.open("account")
    assert not isinstance(acct, ErrorPage)


def test_lazy_sections_build_after_the_first_paint(env):
    win, _app = env.build("free")
    win.show()
    page = win.open("settings")
    assert page.is_built("general") and page.is_built("microphone")
    # One section per event-loop turn; generous for a busy CI machine.
    assert _wait(lambda: page.is_built("updates"), 30), sorted(page._built)


@pytest.mark.parametrize("tier", ["free", "pro", "guest"])
def test_every_control_has_a_name(env, tier):
    win, app = env.build(tier, nvidia=True)
    page = _settings(win)
    page._toggle_more_options(True)
    page.key_rows["google_api_key"].open_editor()
    assert not _unnamed(page), _unnamed(page)
    acct = win.open("account")
    assert not _unnamed(acct), _unnamed(acct)


def test_admin_preview_row(env):
    win, app = env.build("pro")
    app.auth.is_admin = True
    acct = win.open("account")
    acct.refresh_account()
    seg = acct.admin_segmented
    seg.buttons()[2].click()
    assert ("set_admin_tier_override", "free") in app.calls
    assert not _unnamed(acct)


# ── sections and scrolling ──
def test_section_option_scrolls_and_highlights(env):
    win, _app = env.build("free")
    win.show()
    page = win.open("settings", section="privacy")
    _spin(10)
    assert page.opts == {"section": "privacy"}
    assert page.current_section() == "privacy"
    bar = page.scroll.verticalScrollBar()
    assert _wait(lambda: bar.value() == min(page.section_widget("privacy").y(), bar.maximum()), 2)
    assert bar.value() == page.section_widget("privacy").y()       # the tail lets it reach the top
    page.nav.items["general"].click()
    _spin()
    assert bar.value() == 0 and page.current_section() == "general"
    win.open("settings", section="updates")
    _spin(10)
    assert page.current_section() == "updates"


def test_jump_near_the_end_lands_once_the_rest_is_built(env):
    """A tall window opened straight on Privacy: the page is too short to bring
    it to the top until Updates is built; then it settles at the top."""
    win, _app = env.build("free")
    win.resize(1280, 1300)
    win.show()
    page = win.open("settings", section="privacy")
    bar = page.scroll.verticalScrollBar()
    assert _wait(lambda: page.is_built("updates"), 30)
    assert _wait(lambda: bar.value() == page.section_widget("privacy").y(), 3), \
        (bar.value(), page.section_widget("privacy").y(), bar.maximum())
    assert page.current_section() == "privacy"


def test_the_smallest_window_clips_nothing(env):
    """The shell's 960 px minimum: the section list makes room, Speech runs on /
    AI writer stack, Account stacks its side cards - no content is wider than
    what the page can show (the scroll areas never scroll sideways)."""
    from PySide6.QtWidgets import QBoxLayout
    win, _app = env.build("pro", nvidia=True, google_api_key="AIza-test-key-0xyz")
    win.resize(960, 700)
    win.show()
    page = _settings(win)
    page._toggle_more_options(True)
    page.key_rows["mistral_api_key"].open_editor()
    _spin(10)
    assert page.nav.isHidden() and page.is_narrow()
    assert page.speech_two.direction() == QBoxLayout.TopToBottom
    assert page.content.minimumSizeHint().width() <= page.scroll.viewport().width(), \
        (page.content.minimumSizeHint().width(), page.scroll.viewport().width())
    win.resize(1280, 820)
    _spin(10)
    assert not page.nav.isHidden() and not page.is_narrow()
    assert page.speech_two.direction() == QBoxLayout.LeftToRight
    assert page.content.minimumSizeHint().width() <= page.scroll.viewport().width()
    acct = win.open("account")
    _spin(5)
    assert not acct.is_stacked()
    win.resize(960, 700)
    _spin(10)
    assert acct.is_stacked()
    assert acct.minimumSizeHint().width() <= acct.parentWidget().width()


def test_nav_account_link_opens_account(env):
    win, _app = env.build("free")
    page = _settings(win)
    page.nav.account_link.click()
    assert win.current_route() == "account"


# ── General ──
def test_general_controls_apply_at_once(env):
    win, app = env.build("free")
    page = _settings(win)
    page.t_notify.click()
    assert ("set_settings", {"notify_when_ready": False}) in app.calls
    # 2.0.0 is light only: no App look row (app_theme is kept in the config).
    assert page.row_look is None and page.seg_look.isHidden()
    from PySide6.QtWidgets import QLabel
    assert not [lb for lb in page.findChildren(QLabel) if lb.text() == "App look" and lb.isVisibleTo(page)]
    page.t_login.click()
    assert ("set_settings", {"launch_at_login": False}) in app.calls


def test_shortcut_rows_show_keys_and_open_their_pages(env):
    win, app = env.build("free")
    page = _settings(win)
    dictate, smart, live = page.shortcut_rows                   # Dictate, Smart Actions, Live Assistant
    assert [r.name for r in page.shortcut_rows] == ["Dictate", "Smart Actions", "Live Assistant"]
    assert "Alt + R" in dictate.keys.accessibleName()
    assert "Ctrl + Alt + A" in live.keys.accessibleName()
    assert smart.keys.accessibleName() == "Change the Smart Actions shortcut (Alt + S)"
    dictate.keys.click()
    assert win.current_route() == "dictation"
    win.open("settings")
    smart.link.click()
    assert win.current_route() == "smart_actions"
    app.cfg["smart_actions_hotkey"] = "ctrl+alt+s"              # the Smart Actions page's key
    page.on_setting_changed("smart_actions_hotkey")
    _spin()
    assert "Ctrl + Alt + S" in smart.keys.accessibleName()
    app.cfg["smart_actions_hotkey"] = ""                        # none set (it clashed at the upgrade)
    page.on_setting_changed("smart_actions_hotkey")
    _spin()
    assert smart.keys.accessibleName() == "Set a Smart Actions shortcut"


def test_smart_shortcut_row_defaults_to_alt_s(env):
    win, app = env.build("free")
    app.cfg.pop("smart_actions_hotkey", None)                   # a config from before the key
    page = _settings(win)
    assert page.shortcut_rows[1].keys.accessibleName() == "Change the Smart Actions shortcut (Alt + S)"


def test_shortcut_rows_ask_their_page_to_focus_the_shortcut(env):
    win, _app = env.build("free")
    page = _settings(win)
    seen = []
    real_open = win.open
    win.open = lambda route, **opts: seen.append((route, opts)) or real_open(route, **opts)
    page.shortcut_rows[0].keys.click()
    assert seen[-1] == ("dictation", {"focus": "hotkey"})        # Dictation and Smart Actions accept it


def test_feature_links(env):
    win, _app = env.build("free")
    page = _settings(win)
    page.feature_links["audio_to_text"].click()
    assert win.current_route() == "audio_to_text"


def test_microphone_picker_and_level(env):
    win, app = env.build("free")
    win.show()
    page = win.open("settings", section="microphone")
    assert _wait(lambda: page.mic_combo.count() == 3, 3)
    assert page.mic_combo.itemText(1) == "Microphone (Realtek Audio)"
    page.mic_combo.setCurrentIndex(2)
    page.mic_combo.activated.emit(2)
    assert app.cfg["input_device_index"] == 3
    app.mic_monitor.level = 0.3
    page._mic_on = True
    page._mic_tick()
    assert page.level.state() == "heard"
    assert page.level.status.text() == "We hear you"


# ── Speech & AI ──
def test_model_cards_reflect_the_shared_downloads(env):
    win, app = env.build("free")
    page = _settings(win)
    small, base, turbo = page.model_cards["small"], page.model_cards["base"], page.model_cards["large-v3-turbo"]
    assert small.is_selected() and "remove" in small.buttons
    assert page.cards_row.recommended() is small                   # Best for your computer
    assert page.cards_row.ribbon.isVisibleTo(page.cards_row)
    assert turbo.note.text() == "A little slower here"
    assert small.model_label.text().replace(" ", " ") == "Whisper Small · 460 MB"
    assert page.hw_trail.text() == "This computer: i7-1260P · 16 GB"
    base.buttons["download"].click()
    assert ("start", "base") in app.downloads.calls
    _spin()
    assert base.progress is not None                                 # downloading state
    app.downloads.finish("base")
    _spin()
    assert app.cfg["whisper_model"] == "base" and app.cfg["backend"] == "local"   # used once it's here
    assert page.model_cards["base"].is_selected()


def test_downloads_that_appear_later_are_heard_once(env):
    """Wave 4 may create app.downloads after the window: the next open connects
    it, and opening again doesn't connect it twice."""
    win, app = env.build("free")
    dl = app.downloads
    del app.downloads
    page = _settings(win, section="speech")
    assert not page.model_cards["base"].buttons["download"].isEnabled()
    app.downloads = dl
    win.open("settings")
    win.open("settings")
    _spin()
    base = page.model_cards["base"]
    assert base.buttons["download"].isEnabled()
    base.buttons["download"].click()
    _spin()
    assert base.progress is not None
    hits = []
    base._set_progress = hits.append
    dl.progress.emit("base", 40, 40, 100)
    assert hits == [40]
    dl.progress.emit("base", 0, 0, 0)                               # size not known yet
    assert hits == [40, None]                                       # indeterminate, not "0%"


def test_use_and_remove_a_model(env):
    win, app = env.build("free")
    app.downloads.states["base"] = "ready"
    page = _settings(win)
    page.model_cards["base"].buttons["use"].click()
    assert app.cfg["whisper_model"] == "base"
    env.answers.append("remove")
    page.model_cards["base"].buttons["remove"].click()
    assert env.asked[-1]["title"] == "Remove Fast?"
    assert ("remove", "base") in app.downloads.calls
    app.downloads.states["base"] = "missing"
    app.downloads.removed.emit("base", True, "")
    _spin()
    assert app.cfg["whisper_model"] == "small"                       # moved to a model on disk
    env.answers.append(None)                                         # Cancel
    page.model_cards["small"].buttons["remove"].click()
    assert ("remove", "small") not in app.downloads.calls


def test_removing_the_smart_actions_speech_model_resets_it(env):
    """Smart Actions transcribed with the removed model: back to Same as
    Dictation (not a fresh download at the next Smart Action)."""
    win, app = env.build("free", smart_actions_speech_model="base")
    app.downloads.states["base"] = "ready"
    _settings(win)
    app.downloads.states["base"] = "missing"
    app.downloads.removed.emit("base", True, "")
    _spin()
    assert app.cfg["smart_actions_speech_model"] == ""
    assert app.cfg["whisper_model"] == "small"                       # dictation's own pick untouched
    # Another model's removal, or a failed one, leaves the pick alone.
    app.cfg["smart_actions_speech_model"] = "medium"
    app.downloads.removed.emit("base", True, "")
    app.downloads.removed.emit("medium", False, "In use")
    _spin()
    assert app.cfg["smart_actions_speech_model"] == "medium"


def test_more_sizes_tiles(env):
    win, app = env.build("free")
    page = _settings(win)
    tiny = page.size_tiles["tiny"]
    assert tiny.accessibleName() == "Download Lightest (Whisper Tiny, 75 MB)"
    tiny.click()
    assert ("start", "tiny") in app.downloads.calls
    page.more_button.click()
    assert not page.tiles_row.isVisibleTo(page)


def test_pro_sees_offline_backup_and_cloud_selected(env):
    win, app = env.build("pro")
    page = _settings(win)
    assert page.speech_tiles["cloud"].isChecked()
    assert page.writer_tiles["cloud"].isChecked()
    assert page.writer_tiles["cloud"].hints() == ["DeepSeek V4.1 Flash", "Mistral Small for meeting notes"]
    from PySide6.QtWidgets import QLabel
    texts = [lb.text() for lb in page.model_cards["small"].action.findChildren(QLabel)]
    assert "Offline backup" in texts
    assert page.writer_sizes["qwen_tiny"].is_selected()


def test_speech_runs_on_free_user_gets_the_pro_window(env):
    win, app = env.build("free")
    page = _settings(win)
    assert page.speech_tiles["local"].isChecked()
    assert page.speech_tiles["local"].hints() == ["Whisper Small · Local · Balanced"]
    assert page.speech_tiles["cloud"].pro_tag is not None
    page.speech_tiles["cloud"].click()
    assert ("_pro_upsell", "Fast cloud transcription") in app.calls
    assert app.cfg["backend"] == "local"
    assert page.speech_tiles["local"].isChecked() and not page.speech_tiles["cloud"].isChecked()
    page.writer_tiles["cloud"].click()
    assert ("_pro_upsell", "Smart Actions") in app.calls


def test_pro_switches_speech_and_writer(env):
    win, app = env.build("pro", backend="local")
    page = _settings(win)
    page.speech_tiles["cloud"].click()
    assert app.cfg["backend"] == "managed"
    page.writer_tiles["local"].click()                               # Small is on disk
    assert app.cfg["action_model"] == "qwen_tiny"
    assert page.writer_tiles["local"].isChecked()


def test_writer_sizes_download_then_use(env):
    win, app = env.build("free")
    page = _settings(win)
    page.writer_sizes["qwen_3b"].click()
    assert ("start", "qwen_3b") in app.downloads.calls
    app.downloads.finish("qwen_3b")
    _spin()
    assert app.cfg["action_model"] == "qwen_3b"
    assert page.writer_sizes["qwen_3b"].is_selected()


def test_privacy_mode_asks_before_cloud(env):
    win, app = env.build("pro", backend="local", privacy_mode=True)
    page = _settings(win)
    assert page.speech_tiles["cloud"].hints() == ["Off while Privacy mode is on"]
    env.answers.append("off")
    page.speech_tiles["cloud"].click()
    assert env.asked[-1]["title"] == "Turn off Privacy mode?"
    assert app.cfg["privacy_mode"] is False and app.cfg["backend"] == "managed"


def test_own_key_without_a_key_opens_the_key_row(env):
    win, app = env.build("free")
    page = _settings(win)
    page.speech_tiles["own_key"].click()
    row = page.key_rows["mistral_api_key"]
    assert row.is_editing()
    assert app.cfg["backend"] == "local"
    row.input.setText("mk-test-key-0000-abc")
    row.save_button.click()                                          # saving the key finishes the pick
    _spin()
    assert app.cfg["mistral_api_key"] == "mk-test-key-0000-abc"
    assert app.cfg["backend"] == "mistral"
    assert page.speech_tiles["own_key"].isChecked()


def test_own_key_cancelled_then_saved_later_keeps_speech(env):
    win, app = env.build("free")
    page = _settings(win)
    page.speech_tiles["own_key"].click()
    row = page.key_rows["mistral_api_key"]
    row.cancel_button.click()
    row.add_button.click()
    row.input.setText("mk-test-key-0000-abc")
    row.save_button.click()
    _spin()
    assert app.cfg["mistral_api_key"] == "mk-test-key-0000-abc"
    assert app.cfg["backend"] == "local"                             # only saved, not switched


def test_api_key_tested_on_paste_masked_and_saved(env, monkeypatch):
    import api_keys
    seen = []

    def fake_test(field, key, cfg=None):
        seen.append((field, key))
        return (True, "Works")
    monkeypatch.setattr(api_keys, "test_key", fake_test)
    win, app = env.build("free")
    page = _settings(win)
    row = page.key_rows["google_api_key"]
    assert row.text.text() == "No key yet" and row.add_button.isVisibleTo(row)
    row.add_button.click()
    assert row.is_editing() and row.input.echoMode() == QLineEdit.Password
    row.input.setText("AIza-test-key-0xyz")
    assert _wait(lambda: row.status.text() == "✓ Works", 4)
    assert seen == [("google_api_key", "AIza-test-key-0xyz")]
    row.save_button.click()
    assert app.cfg["google_api_key"] == "AIza-test-key-0xyz"
    _spin()
    assert not row.is_editing()
    assert row.text.text() == "Key ending xyz"
    assert row.chip.isVisibleTo(row) and row.chip.text() == "Works"
    env.answers.append("remove")
    row.remove_button.click()
    assert app.cfg["google_api_key"] == ""


def test_more_options_writer_with_own_key(env):
    win, app = env.build("free", action_api_key="sk-openai-test-123")
    page = _settings(win)
    page.more_options.click()
    panel = page.writer_panel
    assert panel.isVisibleTo(page)
    i = panel.provider.findData("api_openai_compatible")
    assert i >= 0
    panel.provider.setCurrentIndex(i)
    assert panel.row_base.isVisibleTo(panel)
    panel.use_button.click()
    assert app.cfg["action_model"] == "api_openai_compatible"
    assert page.writer_tiles["own_key"].isChecked()


def test_gpu_card_only_for_nvidia(env):
    win, app = env.build("free")
    page = _settings(win)
    assert not page.gpu_card.isVisibleTo(page)
    win2, app2 = env.build("free", nvidia=True)
    page2 = _settings(win2)
    assert page2.gpu_card.isVisibleTo(page2)
    assert "NVIDIA GeForce RTX 4060" in page2.gpu_card.sub.text()
    page2.gpu_card.buttons["start"].click()
    assert ("start_gpu_download", "settings") in app2.calls
    assert page2.gpu_card.progress is not None
    page2._on_gpu_done("failed", "HTTPSConnectionPool(host=...) read timed out")   # the network dropped
    assert "start" in page2.gpu_card.buttons                         # Download again
    assert page2.gpu_card.sub.text() == "The download stopped. Try again."
    page2.gpu_card.buttons["start"].click()
    assert "about 20× faster" not in page2.gpu_card.sub.text() and page2.gpu_card.progress is not None


def test_gpu_card_remove_when_it_is_ours(env):
    # The 1.x Settings window's "Remove" (the GPU download): Wave 4 put it on
    # the card - only for Gusan's own download, after a question.
    import dataclasses
    win, app = env.build("free", nvidia=True)
    app.downloads.hardware = dataclasses.replace(app.downloads.hardware, gpu_state="ready")
    app.gpu_removable = lambda: False                     # e.g. a CUDA Toolkit's libraries
    app.remove_gpu_download = lambda: app.calls.append(("remove_gpu_download",)) or True
    page = _settings(win)
    assert page.gpu_card.title.text() == "Your graphics card is on"
    assert "remove" not in page.gpu_card.buttons
    app.gpu_removable = lambda: True
    page._refresh_gpu()
    remove = page.gpu_card.buttons["remove"]
    assert remove.accessibleName() == "Remove the graphics card speed-up"
    env.answers.append("cancel")
    remove.click()
    assert ("remove_gpu_download",) not in app.calls
    assert env.asked[-1]["buttons"] == ["cancel", "remove"]
    env.answers.append("remove")
    page.gpu_card.buttons["remove"].click()
    assert ("remove_gpu_download",) in app.calls


# ── Privacy ──
def test_privacy_toggles(env):
    win, app = env.build("free")
    page = _settings(win)
    for toggle, key in ((page.t_privacy, "privacy_mode"), (page.t_history, "save_history"),
                        (page.t_stats, "analytics_enabled"), (page.t_bar, "overlay_private"),
                        (page.t_card, "live_assist_private")):
        before = bool(app.cfg.get(key))
        toggle.click()
        assert app.cfg[key] is (not before), key
    page.row_stats.hint_label.linkActivated.emit("#collect")
    assert "Never your voice" in page.row_stats.hint_label.text()


def test_delete_all_history(env, monkeypatch):
    S = env.S
    monkeypatch.setattr(S, "_count_history", lambda keep: {"dictations": 3, "meetings": 1, "transcripts": 2})
    cleared = []

    def fake_clear(everything, keep, track_fn):
        cleared.append(everything)
        return {"dictations": 3, "meetings": 1 if everything else 0, "transcripts": 2 if everything else 0,
                "meetings_left": 0, "kept_active": False}
    monkeypatch.setattr(S, "_clear_history", fake_clear)
    win, app = env.build("free")
    page = _settings(win)
    env.answers.append("all")
    page.btn_delete.click()
    assert _wait(lambda: cleared == [True])
    q = env.asked[-1]
    assert q["text"].startswith("Permanently delete 3 dictations, 1 meeting and 2 transcripts?")
    assert q["buttons"] == ["cancel", "dictations", "all"]
    assert _wait(lambda: page.row_delete.hint_label.text() == "Deleted 3 dictations, 1 meeting and 2 transcripts.")


def test_delete_history_when_empty(env, monkeypatch):
    S = env.S
    monkeypatch.setattr(S, "_count_history", lambda keep: {"dictations": 0, "meetings": 0, "transcripts": 0})
    win, _app = env.build("free")
    page = _settings(win)
    page.btn_delete.click()
    assert _wait(lambda: page.row_delete.hint_label.text() == "History is already empty.")
    assert not env.asked


# ── Updates & about ──
def test_check_now_then_install(env, monkeypatch):
    import updater
    from app_info import APP_VERSION
    monkeypatch.setattr(updater, "check_latest", lambda cfg=None, manual=False, **k: ("available", "v9.9.9"))
    monkeypatch.setattr(updater, "install", lambda tag, **k: (False, "Couldn't download the update."))
    win, app = env.build("free")
    page = _settings(win)
    assert page.version_label.text() == "Gusan " + APP_VERSION
    assert page.update_status.text() == "Up to date"
    page.update_button.click()
    assert _wait(lambda: page.update_status.text() == "Update available · v9.9.9")
    assert ("track", "update_check_result", {"manual": True, "available": True}) in app.calls
    assert page.update_button.text() == "Install update"
    page.update_button.click()
    assert _wait(lambda: page.update_status.text() == "Couldn't download the update.")


def test_pending_update_from_the_startup_check(env):
    win, _app = env.build("free", pending_update_version="v9.9.9")
    page = _settings(win)
    assert page.update_status.text() == "Update available · v9.9.9"


def test_install_update_goes_to_the_controller_and_the_row_follows_it(env):
    """The controller (main.AppController.install_update) opens the update window; it calls back only when it
    did NOT install: a message goes on the row in red, a plain Cancel ("") leaves it as it was."""
    from ui.pages import settings as S
    win, app = env.build("free", pending_update_version="v9.9.9")
    asked = []
    app.install_update = lambda tag, manual=True, on_done=None: asked.append((tag, manual, on_done)) or True
    page = _settings(win)
    page.update_button.click()
    assert asked[0][:2] == ("v9.9.9", True)
    assert ("track", "update_install_started", {"manual": True, "to": "v9.9.9"}) in app.calls
    assert page.update_status.text() == "Updating v9.9.9…"
    assert not page.update_button.isEnabled()
    on_done = asked[0][2]
    on_done(False, "")                                            # Cancel: no red text, back to Install update
    assert page.update_status.text() == "Update available · v9.9.9"
    assert page.update_button.text() == "Install update" and page.update_button.isEnabled()
    page.update_button.click()
    asked[1][2](False, "The download was damaged. Try again.")    # a failure: the reason, on the row
    assert page.update_status.text() == "The download was damaged. Try again."
    assert page.update_button.text() == "Install update"
    page.update_button.click()
    asked[2][2](False, S.updater.UNSUPPORTED)                     # (the old wording of "the browser took over")
    assert page.update_status.text() == "Update available · v9.9.9"


def test_the_page_says_when_a_typed_key_is_not_saved(env):
    """AppController._update_unsaved_edits asks the pages: installing closes Gusan and a key typed into an open
    editor (and not saved) would be lost."""
    win, _app = env.build("free")
    page = _settings(win, section="speech")
    assert page.unsaved_edits() is False
    row = page.key_rows["google_api_key"]
    row.open_editor()
    assert page.unsaved_edits() is False                          # open, nothing typed
    row.input.setText("   ")
    assert page.unsaved_edits() is False
    row.input.setText("AIzaSyExampleKey")
    assert page.unsaved_edits() is True
    row.close_editor()                                            # Cancel: nothing to lose
    assert page.unsaved_edits() is False
    page._toggle_more_options(True)
    writer = page.writer_panel.key_row
    writer.open_editor()
    writer.input.setText("sk-example")
    assert page.unsaved_edits() is True                           # the AI writer's key too
    writer.close_editor()
    assert page.unsaved_edits() is False
    assert win.pages()["settings"] is page                        # where the controller finds it


def test_about_rows(env):
    from ui.components import Chip
    win, app = env.build("free")
    page = _settings(win)
    texts = [c.text() for c in page.row_computer.findChildren(Chip)]
    assert texts == ["i7-1260P · 12 cores", "16 GB memory", "Intel Iris Xe"]
    page.about_links["Source code"].click()
    assert env.opened[-1] == "https://github.com/Aram2K/Gusan"
    page.about_links["Website"].click()
    assert env.opened[-1] == "https://gusan.aibuben.xyz"                 # Gusan's own site
    page.about_links["AIBUBEN community"].click()
    assert env.opened[-1] == "https://aibuben.xyz"
    assert page.about_links["AIBUBEN community"].accessibleName() == "Open the AIBUBEN community website"
    page.t_install.click()
    assert app.cfg["install_updates_on_quit"] is False


def test_feedback_dialog(env, monkeypatch):
    import account_actions
    monkeypatch.setattr(account_actions, "send_feedback",
                        lambda auth, text, *a, **k: (True, "Thanks! We read every message."))
    win, app = env.build("free")
    page = _settings(win)
    dlg = env.S.FeedbackDialog(page, app)
    dlg.box.setPlainText("Please add Georgian.")
    dlg.send_button.click()
    assert _wait(lambda: dlg.result.text() == "Thanks! We read every message.")
    assert ("track", "feedback_sent", {"category": "feedback"}) in app.calls
    guest_win, guest = env.build("guest")
    gdlg = env.S.FeedbackDialog(_settings(guest_win), guest)
    assert gdlg.box is None
    gdlg.send_button.click()
    assert ("show_auth_gate",) in guest.calls


def test_ask_dialog_typed_confirmation(qapp):
    from ui.pages.settings import AskDialog
    dlg = AskDialog(None, "Delete your account?", "Gone for good.",
                    [("cancel", "Cancel", "ghost"), ("delete", "Delete account", "danger")], typed="DELETE")
    assert not dlg.buttons["delete"].isEnabled()
    dlg.input.setText("DELETE")
    assert dlg.buttons["delete"].isEnabled()
    dlg.buttons["delete"].click()
    assert dlg.choice == "delete"
    dlg.deleteLater()


def test_on_setting_changed_redraws(env):
    win, app = env.build("free")
    page = _settings(win)
    app.cfg["save_history"] = False
    app.cfg["whisper_model"] = "large-v3-turbo"
    app.downloads.states["large-v3-turbo"] = "ready"
    page.on_setting_changed("save_history")
    _spin()
    assert not page.t_history.isChecked()
    assert page.model_cards["large-v3-turbo"].is_selected()
    page.on_setting_changed("not_a_setting_we_show")             # ignored, no error


# ── Account & plan ──
def test_free_account(env, monkeypatch):
    import entitlements
    monkeypatch.setattr(entitlements, "smart_actions_used", lambda auth=None: 1)
    win, app = env.build("free")
    acct = win.open("account")
    assert acct.plan_name.text() == "Free"
    assert [r.accessibleName() for r in acct.feature_rows] == [
        "Meetings with AI notes", "Live Assistant", "Unlimited Smart Actions", "Gusan Cloud"]
    assert acct.usage_text.text() == "Unlimited"
    assert acct.tries_bar is None
    assert "manage" not in acct.buttons
    acct.buttons["upgrade"].click()
    assert ("_pro_upsell", None) in app.calls
    acct.buttons["sign_out"].click()
    assert ("sign_out",) in app.calls
    acct.buttons["Terms of Service"].click()
    assert env.opened[-1] == "https://aibuben.xyz/terms"


def test_pro_account(env):
    win, app = env.build("pro")
    acct = win.open("account")
    assert acct.plan_name.text() == "Pro"
    assert acct.plan_sub.text() == "Annual · renews 4 Oct 2027"
    assert "upgrade" not in acct.buttons                            # calm Pro: no Upgrade
    assert all(r.check is not None for r in acct.feature_rows)
    assert acct.usage_text.text() == "Unlimited"
    acct.buttons["manage"].click()
    assert ("open_billing",) in app.calls


def test_a_stale_trial_has_no_empty_footer(env):
    from ui.components import Divider
    win, app = env.build("pro")
    app.auth.plan = "trial"
    app.auth.subscription_status = "trialing"
    acct = win.open("account")
    assert acct.plan_sub.text() == "Pro"                            # trials are no longer shown
    assert "manage" not in acct.buttons and "upgrade" not in acct.buttons
    assert len(acct.plan_card.findChildren(Divider)) == 1           # only the one under the plan name


def test_guest_account(env):
    win, app = env.build("guest")
    acct = win.open("account")
    assert acct.plan_name.text() == "Guest"
    assert acct.email_label.text() == "Not signed in"
    assert acct.delete_card is None
    acct.buttons["sign_in"].click()
    assert ("show_auth_gate",) in app.calls


def test_account_follows_the_plan(env):
    win, app = env.build("free")
    acct = win.open("account")
    app.set_tier("pro")
    app.auth.plan = "monthly"
    win.refresh_account()
    assert acct.plan_name.text() == "Pro" and "manage" in acct.buttons


def test_delete_account(env, monkeypatch):
    import account_actions
    monkeypatch.setattr(account_actions, "delete_account", lambda auth, **k: (True, "Your account was deleted."))
    win, app = env.build("free")
    acct = win.open("account")
    env.answers.append(None)
    acct.buttons["delete"].click()
    assert env.asked[-1]["typed"] == "DELETE"
    assert ("sign_out",) not in app.calls
    env.answers.append("delete")
    acct.buttons["delete"].click()
    assert _wait(lambda: ("sign_out",) in app.calls)
    assert acct.delete_note.text() == "Your account was deleted."
    # sign_out() -> sig_auth_changed redraws the page as a guest: the answer stays.
    app.set_tier("guest")
    app.auth.is_authenticated = False
    win.refresh_account()
    assert acct.delete_card is None
    assert acct.notice_label is not None and acct.notice_label.text() == "Your account was deleted."
    win.open("account")                                              # a one-time answer
    assert acct.notice_label is None
