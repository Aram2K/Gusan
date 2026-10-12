# -*- coding: utf-8 -*-
"""Gusan 2.0 onboarding (ui/onboarding_v2.py), the sign-in sheet
(ui/auth_sheet.py) and the mic-check stream (mic_monitor.py).

Built offscreen with conftest's stub controller (guest / Free / Pro) plus fake
services: app.downloads (hardware probe, downloads), app.mic_monitor and an
auth whose network calls answer at once. Workers run synchronously (run=)."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (QAbstractButton, QApplication, QCheckBox, QComboBox, QDialog, QLineEdit,
                               QTextEdit, QWidget)

import hardware
from tests_qt.conftest import make_stub_app


# ── fakes ────────────────────────────────────────────────────────────────────
def sync_run(fn, *args, then=None, error=None, **kwargs):
    try:
        res = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        if error is not None:
            error(e)
        return None
    if then is not None:
        then(res)
    return None


class Deferred:
    """A runner that keeps the work: .finish(i, result) answers it later."""

    def __init__(self):
        self.jobs = []

    def __call__(self, fn, *args, then=None, error=None, **kwargs):
        self.jobs.append(SimpleNamespace(fn=fn, args=args, then=then, error=error))

    def answer(self, i, result):
        job = self.jobs[i]
        job.then(result)


def snapshot(nvidia=False, ram=15.7, cores=12):
    if nvidia:
        return hardware.HardwareSnapshot(
            cpu_name="AMD Ryzen 7 7840HS", cores=8, ram_gb=31.3, ram_display_gb=32,
            gpu_name="NVIDIA GeForce RTX 4060 Laptop GPU", gpu_discrete=True, gpu_state="available",
            platform="win32", recommended=hardware.recommend_model(31.3, 8, "available", "win32"))
    return hardware.HardwareSnapshot(
        cpu_name="12th Gen Intel Core i7-1260P", cores=cores, ram_gb=ram,
        ram_display_gb=hardware.display_ram_gb(ram), gpu_name="Intel Iris Xe Graphics", gpu_discrete=False,
        gpu_state="unsupported", platform="win32",
        recommended=hardware.recommend_model(ram, cores, "unsupported", "win32"))


class FakeDownloads(QObject):
    progress = Signal(str, int, object, object)
    finished = Signal(str, str, str)
    removed = Signal(str, bool, str)
    state_changed = Signal(str, str)
    scanned = Signal()
    hardware_ready = Signal(object)

    def __init__(self, ready=(), hw=True, nvidia=False):
        super().__init__()
        self.states = {m: "ready" for m in ready}
        self.prog, self.calls = {}, []
        self.hardware = snapshot(nvidia) if hw else None

    @property
    def recommendation(self):
        return self.hardware.recommended if self.hardware else None

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

    def progress_of(self, n):
        return self.prog.get(n)

    def probe_hardware(self, cfg=None, force=False):
        self.calls.append(("probe", force))
        return self.hardware


class FakeMic:
    """Settings' app.mic_monitor interface (a peak ``level``, no ``rms``)."""

    def __init__(self):
        self.level, self.error = 0.0, ""
        self.started, self.stopped = [], 0

    def devices(self):
        return [(1, "Microphone (Realtek Audio)"), (3, "Headset (Jabra)")]

    def default_index(self):
        return 1

    def start(self, idx=None):
        self.started.append(idx)

    def stop(self):
        self.stopped += 1


def app_for(tier="guest", hw=True, ready=(), nvidia=False, downloads=True, mic=True, **cfg):
    app = make_stub_app(tier)
    app.cfg.update({"analytics_enabled": True, "input_device_index": None, "whisper_model": "base",
                    "backend": "local"})
    app.cfg.update(cfg)
    app.is_rec = False
    app._busy = False
    app.auth.last_error = ""
    app.auth.calls = []

    def rec(name, result):
        def fn(*a):
            app.auth.calls.append((name,) + a)
            return result() if callable(result) else result
        return fn
    app.auth.sign_in_with_google = rec("google", False)
    app.auth.cancel_google_login = rec("cancel_google", True)
    app.auth.sign_up_email = rec("sign_up", lambda: ("verify", app.auth.calls[-1][1]))
    app.auth.sign_in_email = rec("sign_in", ("error", "Please verify your email first - check your inbox."))
    app.auth.resend_verification = rec("resend", True)
    app.auth.send_password_reset = rec("reset", True)

    def set_settings(updates):
        app.calls.append(("set_settings", dict(updates)))
        app.cfg.update(updates)
        return {k: True for k in updates}

    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        app.cfg[key] = value
        return True
    app.set_settings = set_settings
    app.set_setting = set_setting
    app.start_gpu_download = lambda source="settings": app.calls.append(("start_gpu_download", source)) or True
    if downloads:
        app.downloads = FakeDownloads(ready=ready, hw=hw, nvidia=nvidia)
    if mic:
        app.mic_monitor = FakeMic()
    return app


@pytest.fixture
def made(qapp, monkeypatch):
    import ui.winfit
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    widgets = []
    yield widgets
    for w in widgets:
        try:
            cap = getattr(getattr(w, "try_", None), "shortcut", None)
            if cap is not None and cap.is_capturing():
                cap.cancel_capture()
            w.hide()
            w.deleteLater()
        except RuntimeError:
            pass
    QApplication.processEvents()


@pytest.fixture
def wizard(made):
    from ui import onboarding_v2 as onb

    def build(tier="guest", run=sync_run, show=True, **kw):
        app = app_for(tier, **kw)
        w = onb.OnboardingWizard(app, run=run, animate=False)
        made.append(w)
        if show:
            w.show()
            QApplication.processEvents()
        return w, app
    return build


def _visible_controls(root):
    out = []
    for cls in (QAbstractButton, QLineEdit, QComboBox, QTextEdit):
        for wd in root.findChildren(cls):
            if wd.isVisibleTo(root):
                out.append(wd)
    return out


def _name_of(wd):
    if wd.accessibleName().strip():
        return wd.accessibleName()
    if isinstance(wd, QAbstractButton):
        return wd.text().strip()
    return ""


def _texts(root):
    from PySide6.QtWidgets import QLabel
    return [lb.text() for lb in root.findChildren(QLabel) if lb.isVisibleTo(root)]


# ── every step builds ────────────────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["guest", "free", "pro"])
def test_every_step_builds_without_error_cards(wizard, tier):
    from ui import onboarding_v2 as onb
    from ui.shell import ErrorPage
    w, _app = wizard(tier)
    assert w.size().width() >= 480
    assert w.stack.size().width() == onb.W and w.stack.size().height() == onb.H
    for name in onb.PAGES:
        w.go(name)
        QApplication.processEvents()
        page = w.page(name)
        assert w.current() == name and w.stack.currentWidget() is page
        assert not page.findChildren(ErrorPage)
        assert page.dots.accessibleName() == "Step %d of 5" % onb.STEP_OF[name]
        assert w.accessibleDescription() == "Step %d of 5" % onb.STEP_OF[name]
        assert not page.grab().isNull()
    w.go("mic")
    w.stop_mic()


@pytest.mark.parametrize("tier", ["guest", "pro"])
def test_every_control_has_an_accessible_name(wizard, tier):
    from ui import onboarding_v2 as onb
    w, _app = wizard(tier)
    w.dictation_pasted("Hello, this is my first dictation.")
    for name in onb.PAGES:
        w.go(name)
        QApplication.processEvents()
        page = w.page(name)
        missing = [type(wd).__name__ for wd in _visible_controls(page) if not _name_of(wd)]
        assert not missing, (name, missing)
        for wd in _visible_controls(page):
            group = wd.group() if isinstance(wd, QAbstractButton) else None
            if group is not None and group.exclusive():
                # A radio group: Tab reaches its checked option, the arrows the others.
                checked = group.checkedButton()
                assert checked is None or checked.focusPolicy() & Qt.TabFocus, (name, _name_of(checked))
                continue
            if isinstance(wd, (QAbstractButton, QLineEdit, QComboBox, QTextEdit)):
                assert wd.focusPolicy() != Qt.NoFocus or not wd.isEnabled(), (name, _name_of(wd))
    w.stop_mic()


def test_hello_shows_the_real_shortcut_and_sign_in_for_guests(wizard):
    w, app = wizard("guest", hotkey="ctrl+shift+space")
    assert w.current() == "hello"
    assert w.hello.keys.accessibleName() == "Ctrl + Shift + Space"
    assert w.hello.signin_row.isVisibleTo(w.hello)
    texts = _texts(w.hello)
    assert "Speak." in texts and "Gusan types." in texts and "in any app" in texts
    assert w.hello.screen.accessibleName() == "Gusan typing an email while you speak"
    w.hello.start_btn.click()
    assert w.current() == "model"
    w2, _ = wizard("free")
    assert not w2.hello.signin_row.isVisibleTo(w2.hello)


# ── step 2: the model ────────────────────────────────────────────────────────
def test_model_step_uses_the_hardware_probe(wizard):
    w, app = wizard()
    w.go("model")
    m = w.model
    chips = [c.text() for c in m.findChildren(QWidget) if hasattr(c, "text") and type(c).__name__ == "Chip"
             and c.isVisibleTo(m)]
    assert chips[:3] == ["i7-1260P · 12 cores", "16 GB memory", "Intel Iris Xe"]
    assert m.cards["small"].is_best() and not m.cards["base"].is_best()
    assert m.selected() == "small"
    assert m.cards["large-v3-turbo"].note_text() == "A little slower here"
    assert m.cards["base"].note_text() == ""
    assert "Whisper Small" in m.cards["small"].accessibleName()
    assert "Best for your computer" in m.cards["small"].accessibleName()
    assert m.cta.text() == "Download Balanced · 460 MB"
    labels = _texts(m)
    for txt in ("Pick your speech model", "Whisper Base · 140 MB", "Whisper Small · 460 MB",
                "Whisper Large v3 Turbo · 1.6 GB", "Gemini 2.5 Flash", "Whisper Tiny · 75 MB",
                "Whisper Medium · 1.4 GB", "Whisper Large v3 · 3 GB", "Fastest. Nothing to download.",
                "More sizes", "Works offline", "Change it later in Settings"):
        assert txt in labels, txt
    assert m.cloud.pro_tag.text() == "PRO"


def test_model_cta_follows_the_pick(wizard):
    w, app = wizard()
    w.go("model")
    m = w.model
    for mid, text in (("base", "Download Fast · 140 MB"), ("large-v3-turbo", "Download Most accurate · 1.6 GB"),
                      ("tiny", "Download Lightest · 75 MB"), ("medium", "Download Accurate · 1.4 GB"),
                      ("large-v3", "Download Maximum · 3 GB")):
        m.buttons()[mid].click()
        assert m.selected() == mid
        assert m.cta.text() == text
        assert m.cta.property("kind") == "primary"
    m.cloud.click()
    assert m.selected() == "cloud"
    assert m.cta.text() == "Try Pro free" and m.cta.property("kind") == "pro"


def test_model_next_sets_whisper_model_and_starts_the_download(wizard):
    w, app = wizard()
    w.go("model")
    w.model.cta.click()
    assert ("set_setting", "whisper_model", "small") in app.calls
    assert ("start", "small") in app.downloads.calls
    assert w.current() == "mic" and w.model_choice == "small"
    chip = w.mic.chip
    assert chip.isVisibleTo(w.mic) and chip.plain.text() == "Balanced" and chip.real.text() == "Whisper Small"
    app.downloads.progress.emit("small", 62, 285, 460)
    assert chip.status.text() == "62%"
    app.downloads.finish("small")
    assert chip.state() == "ready" and chip.status.text() == "ready"
    assert w.try_.chip.state() == "ready"
    w.stop_mic()


def test_model_already_on_disk_says_next_and_downloads_nothing(wizard):
    w, app = wizard(ready=("small",))
    w.go("model")
    assert w.model.cta.text() == "Next"
    w.model.cta.click()
    assert ("start", "small") not in app.downloads.calls
    assert w.current() == "mic" and w.mic.chip.state() == "ready"
    w.stop_mic()


def test_model_from_cloud_backend_switches_back_to_local(wizard):
    w, app = wizard("free", backend="managed")
    w.go("model")
    w.model.buttons()["base"].click()
    w.model.cta.click()
    assert ("set_settings", {"backend": "local", "whisper_model": "base"}) in app.calls
    w.stop_mic()


def test_gusan_cloud_free_opens_the_pro_window(wizard):
    w, app = wizard("free")
    w.go("model")
    w.model.cloud.click()
    w.model.cta.click()
    assert ("_pro_upsell", "Fast cloud transcription") in app.calls
    assert w.current() == "model"
    assert not [c for c in app.calls if c[0] in ("set_settings", "set_setting")]


def test_gusan_cloud_pro_is_used(wizard):
    w, app = wizard("pro")
    w.go("model")
    w.model.cloud.click()
    assert w.model.cta.text() == "Use Gusan Cloud" and w.model.cta.property("kind") == "primary"
    w.model.cta.click()
    sets = [c[1] for c in app.calls if c[0] == "set_settings"]
    assert sets and sets[-1].get("backend") == "managed"
    assert w.current() == "mic" and w.model_choice == "cloud"
    assert w.mic.chip.state() == "hidden"
    w.stop_mic()


def test_model_step_without_the_downloads_service(wizard):
    w, app = wizard(downloads=False)
    w.go("model")
    assert w.model.snapshot() is not None              # hardware.snapshot(probe_gpu=False) on the runner
    assert w.model.cta.text() == "Next"
    w.model.cta.click()
    assert ("set_setting", "whisper_model", w.model.snapshot().recommended.model) in app.calls
    assert w.current() == "mic" and w.mic.chip.state() == "hidden"
    w.stop_mic()


def test_hardware_arriving_late_preselects_unless_the_user_picked(wizard):
    w, app = wizard(hw=False)
    w.go("model")
    assert w.model.snapshot() is None
    assert "Checking…" in [c.text() for c in w.model.findChildren(QWidget) if type(c).__name__ == "Chip"
                                 and c.isVisibleTo(w.model)]
    app.downloads.hardware = snapshot()
    app.downloads.hardware_ready.emit(app.downloads.hardware)
    assert w.model.selected() == "small"
    w2, app2 = wizard(hw=False)
    w2.go("model")
    w2.model.buttons()["base"].click()
    app2.downloads.hardware_ready.emit(snapshot())
    assert w2.model.selected() == "base" and w2.model.cards["small"].is_best()


def test_weak_computer_suggests_lightest_and_cloud_first(wizard):
    w, app = wizard(hw=False)
    w.go("model")
    app.downloads.hardware_ready.emit(snapshot(ram=3.0, cores=2))
    assert w.model.selected() == "tiny"
    assert w.model.sizes["tiny"].best.isVisibleTo(w.model)
    first = w.model.stack_box.itemAt(0).widget()
    assert first is w.model.cloud


# ── step 3: the mic ──────────────────────────────────────────────────────────
def test_mic_step_lists_devices_shows_level_and_closes_on_leave(wizard):
    w, app = wizard()
    w.go("mic")
    mic = app.mic_monitor
    assert mic.started == [None]
    items = [w.mic.picker.itemText(i) for i in range(w.mic.picker.count())]
    assert items == ["Microphone (Realtek Audio)", "Headset (Jabra)"]
    assert w.mic.state() == "listening" and w.mic.status.text() == "Say hi"
    mic.level = 0.6                                    # a voice (the Settings-style peak level)
    for _ in range(4):
        w._mic_tick()
    assert w.mic.state() == "heard" and w.mic.status.text() == "We hear you"
    assert w.mic.visual.level() > 0.3
    mic.level = 0.0
    for _ in range(5):
        w._mic_tick()
    assert w.mic.state() == "heard"                    # stays: it heard you once
    w.mic.picker.setCurrentIndex(1)
    assert ("set_setting", "input_device_index", 3) in app.calls
    assert mic.started[-1] == 3
    w.mic.next_btn.click()
    assert w.current() == "try" and mic.stopped >= 1
    assert not w._mic_timer.isActive()


def test_mic_step_reads_mic_monitor_rms(wizard):
    import mic_monitor
    w, app = wizard()
    app.mic_monitor.rms = 0.1                          # -20 dBFS
    w.go("mic")
    for _ in range(4):
        w._mic_tick()
    assert w.mic.state() == "heard"
    assert mic_monitor.display_level(0.1) > 0.7
    w.stop_mic()


def test_mic_step_error(wizard):
    w, app = wizard()
    w.go("mic")
    app.mic_monitor.error = "Can't open this microphone."
    w._mic_started -= 1.0
    w._mic_tick()
    assert w.mic.state() == "error" and "open this mic" in w.mic.status.text()
    w.stop_mic()


def test_mic_default_device_from_config(wizard):
    w, app = wizard(input_device_index=3)
    w.go("mic")
    assert w.mic.picker.currentText() == "Headset (Jabra)"
    assert app.mic_monitor.started == [3]
    w.stop_mic()


def test_mic_button_and_status_follow_the_check(wizard):
    import mic_monitor
    w, app = wizard()
    w.go("mic")
    assert w.mic.status.text() == "Say hi" and w.mic.next_btn.text() == "Next"
    app.mic_monitor.error = mic_monitor.MSG_NO_MIC
    w._mic_started -= 1.0
    w._mic_tick()
    assert w.mic.state() == "error" and w.mic.status.text() == "No mic found"
    assert w.mic.next_btn.text() == "Next" and w.mic.next_btn.accessibleName() == "Next"
    app.mic_monitor.error = ""
    w.mic.picker.setCurrentIndex(1)                    # another mic: listening again
    assert w.mic.state() == "listening" and w.mic.status.text() == "Say hi"
    app.mic_monitor.level = 0.6
    for _ in range(4):
        w._mic_tick()
    assert w.mic.status.text() == "We hear you"
    assert w.mic.next_btn.text() == "Sounds good" and w.mic.next_btn.accessibleName() == "Sounds good"
    w.stop_mic()


@pytest.mark.parametrize("configured, opened_after_list", [(3, None), (9, [None])])
def test_mic_listens_to_the_configured_mic_before_the_list_arrives(wizard, monkeypatch, configured,
                                                                   opened_after_list):
    """A real MicMonitor answers on workers: the stream opens before the list
    is in. It opens the configured mic, and when the list doesn't have that
    mic (unplugged) the check moves to the one the picker shows."""
    import mic_monitor
    jobs = []
    monkeypatch.setattr(mic_monitor, "_thread", lambda fn, *a: jobs.append((fn, a)))
    pa = FakePA()
    w, app = wizard(mic=False, input_device_index=configured)
    app.recorder = SimpleNamespace(audio=pa)
    w.go("mic")
    names = [fn.__name__ for fn, _a in jobs]
    assert names == ["_run", "_open"]                  # list the mics, open the stream
    fn, a = jobs.pop(1)
    fn(*a)                                             # the stream opens first ...
    assert pa.opened[-1]["input_device_index"] == configured
    fn, a = jobs.pop(0)
    fn(*a)                                             # ... then the list comes in
    if opened_after_list is None:
        assert w.mic.picker.currentText() == "Headset (Jabra)" and not jobs
    else:
        assert w.mic.picker.currentIndex() == 0
        reopen = [job for job in jobs if job[0].__name__ == "_open"]
        assert reopen and reopen[-1][1][1] is None     # (gen, index): the default mic
    w.stop_mic()


def test_hardware_that_never_answers_drops_checking(wizard):
    w, app = wizard(hw=False)
    w.go("model")
    m = w.model

    def chip_texts():
        QApplication.processEvents()                   # chips added to a shown layout appear queued
        return [c.text() for c in m.findChildren(QWidget) if type(c).__name__ == "Chip" and c.isVisibleTo(m)]
    assert "Checking…" in chip_texts() and w._hw_timer.isActive()
    w._hw_timer.timeout.emit()                         # 15 s later, still nothing
    assert "Checking…" not in chip_texts() and not m.your_computer.isVisibleTo(m)
    assert m.selected() == "base" and m.cta.text() == "Download Fast · 140 MB"
    app.downloads.hardware_ready.emit(snapshot())      # a late answer still fills the chips
    assert chip_texts()[:3] == ["i7-1260P · 12 cores", "16 GB memory", "Intel Iris Xe"]
    assert m.your_computer.isVisibleTo(m) and m.selected() == "small"


def test_closed_before_the_probe_answered_gets_the_chips_when_shown_again(wizard):
    # Closed with X while the probe still ran: its answer came while the wizard
    # wasn't listening. Shown again, it asks again - the cached snapshot fills
    # the chips (it used to say "Checking…" for good, with no give-up timer).
    w, app = wizard(hw=False)
    m = w.model
    w.reject()
    assert not w.isVisible() and not w._hw_timer.isActive()
    app.downloads.hardware = snapshot()
    app.downloads.hardware_ready.emit(app.downloads.hardware)   # nobody listens now
    assert m.snapshot() is None
    w.show()
    QApplication.processEvents()
    assert m.snapshot() is app.downloads.hardware
    w.go("model")
    QApplication.processEvents()
    chips = [c.text() for c in m.findChildren(QWidget) if type(c).__name__ == "Chip" and c.isVisibleTo(m)]
    assert "Checking…" not in chips and "16 GB memory" in chips and m.selected() == "small"
    probes = [c for c in app.downloads.calls if c[0] == "probe"]
    w.reject()
    w.show()                                           # chips known: no new probe
    QApplication.processEvents()
    assert [c for c in app.downloads.calls if c[0] == "probe"] == probes


class FakeController(QObject):
    sig_setting_changed = Signal(str)
    sig_auth_changed = Signal()


def test_closing_with_x_stops_everything_and_lets_go(made):
    from ui import onboarding_v2 as onb
    from ui.auth_sheet import AuthSheet
    app = app_for()
    ctl = FakeController()
    app.sig_setting_changed, app.sig_auth_changed = ctl.sig_setting_changed, ctl.sig_auth_changed
    w = onb.OnboardingWizard(app, run=sync_run, animate=False)
    made.append(w)
    w.show()
    QApplication.processEvents()
    w.hello.signin_btn.click()
    assert isinstance(w.sheet, AuthSheet) and w.sheet.isVisible()
    sheet = w.sheet
    w.go("try")
    calls = []
    app.suspend_hotkeys = lambda on: calls.append(on)
    w.try_.shortcut.start_capture()
    w.reject()                                         # the window's X
    assert not w.isVisible() and not w.is_finished()
    assert calls == [True, False]                      # the hotkeys are back on
    assert w.sheet is None and not sheet.isVisible()
    assert w._links == [] and not w._hw_timer.isActive()
    assert not [c for c in app.calls if c[0] in ("set_settings", "set_setting")]
    app.cfg["hotkey"] = "ctrl+alt+r"
    ctl.sig_setting_changed.emit("hotkey")             # no longer listening
    assert w.try_.keys.keys() == ["Alt", "R"]
    w.show()                                           # shown again: listening again
    QApplication.processEvents()
    ctl.sig_setting_changed.emit("hotkey")
    assert w.try_.keys.keys() == ["Ctrl", "Alt", "R"]


def test_keyboard_focus_follows_the_steps(wizard):
    w, app = wizard()
    QApplication.processEvents()
    assert w.focusWidget() is w.hello.start_btn        # not Skip
    w.hello.start_btn.click()
    assert w.focusWidget() is w.model.cards["small"]   # the picked model
    assert not w.model.cards["small"]._kbd             # a click: no focus ring
    from PySide6.QtTest import QTest
    QTest.keyClick(w.model.cards["small"], Qt.Key_Right)       # a radio group: arrows pick
    assert w.model.selected() == "large-v3-turbo" and w.focusWidget() is w.model.cards["large-v3-turbo"]
    assert w.model.cta.text() == "Download Most accurate · 1.6 GB"
    QTest.keyClick(w.model.cards["large-v3-turbo"], Qt.Key_Left)
    assert w.model.selected() == "small"
    w.activateWindow()
    QApplication.processEvents()
    w.model.cta.setFocus(Qt.TabFocusReason)            # the user tabs to the button ...
    QApplication.processEvents()
    w.model.cta.click()                                # ... and presses Space
    assert w.focusWidget() is w.mic.next_btn
    if w.isActiveWindow():                             # focus events need an active window
        assert w.mic.next_btn.property("focusVisible") is True
    w.mic.next_btn.click()
    assert w.focusWidget() is w.try_.edit
    w.try_.next_btn.click()
    assert w.focusWidget() is w.signup.google
    w.stop_mic()


def test_without_app_mic_monitor_the_wizard_opens_its_own_short_stream(wizard, monkeypatch):
    """No shared app.mic_monitor: a MicMonitor on app.recorder.audio, a 16 kHz
    mono level stream only while step 3 shows, closed on leaving it."""
    import array

    import mic_monitor
    monkeypatch.setattr(mic_monitor, "_thread", lambda fn, *a: fn(*a))    # workers run inline
    pa = FakePA()
    w, app = wizard(mic=False)
    app.recorder = SimpleNamespace(audio=pa)
    assert not pa.opened                                   # nothing opens before step 3
    w.go("mic")
    mon = w.monitor()
    assert isinstance(mon, mic_monitor.MicMonitor) and mon.audio() is pa
    items = [w.mic.picker.itemText(i) for i in range(w.mic.picker.count())]
    assert items == ["Microphone (Realtek Audio)", "Headset (Jabra)"]
    kw = pa.opened[-1]
    assert kw["rate"] == mic_monitor.RATE and kw["channels"] == 1 and kw["input"] is True
    loud = array.array("h", [8000, -8000] * 266).tobytes()
    for _ in range(4):
        pa.streams[-1].cb(loud, 532, None, 0)
        w._mic_tick()
    assert w.mic.state() == "heard"
    w.mic.picker.setCurrentIndex(1)                        # another mic: the stream is replaced
    assert pa.opened[-1]["input_device_index"] == 3 and pa.streams[-2].closed
    w.mic.next_btn.click()
    assert w.current() == "try" and pa.streams[-1].closed and not mon.is_running()
    w.go("mic")                                            # back again: a fresh stream
    assert not pa.streams[-1].closed and mon.is_running()
    w.finish()
    assert pa.streams[-1].closed and not mon.is_running()


def test_small_screens_scroll_the_whole_step(wizard, monkeypatch):
    from PySide6.QtCore import QSize

    import ui.winfit
    from ui import onboarding_v2 as onb
    fitted = []
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda win, **k: fitted.append((win, k)))
    w, app = wizard(show=False)
    w.show()
    QApplication.processEvents()
    assert fitted and fitted[-1][0] is w and fitted[-1][1].get("recenter") is True
    w.resize(800, 520)                                     # 1280 x 720 at 150 %
    w.go("model")
    QApplication.processEvents()
    assert w.stack.size() == QSize(onb.W, onb.H)           # the board keeps its size ...
    vbar, hbar = w.scroll.verticalScrollBar(), w.scroll.horizontalScrollBar()
    assert vbar.maximum() > 0 and hbar.maximum() > 0       # ... and scrolls
    w.scroll.ensureWidgetVisible(w.model.cta)
    assert vbar.value() > 0 and hbar.value() > 0
    w.resize(onb.W + 40, onb.H + 40)
    QApplication.processEvents()
    assert vbar.maximum() == 0 and hbar.maximum() == 0


# ── step 4: try it ───────────────────────────────────────────────────────────
def test_try_step_follows_the_real_dictation(wizard):
    w, app = wizard()
    w.go("try")
    t = w.try_
    assert t._poll.isActive()
    assert t.keys.keys() == ["Alt", "R"]
    assert t.pill.state() == "idle" and not t.works.isVisibleTo(t)
    app.is_rec = True
    t.poll()
    assert t.pill.state() == "recording"
    app.is_rec, app._busy = False, True
    t.poll()
    assert t.pill.state() == "transcribing"
    app._busy = False
    t.edit.paste_text("Hello, this is my first dictation.")
    t.poll()
    assert t.pill.state() == "pasted" and t.works.isVisibleTo(t)
    assert t.edit.toPlainText() == "Hello, this is my first dictation."
    assert w.first_dictation() == "Hello, this is my first dictation."
    assert w.signup.first.isVisibleTo(w.signup)
    assert w.signup.first_text.text() == "“Hello, this is my first dictation.”"
    assert w.signup.first_meta.text().startswith("Your first dictation · Today ")
    t.next_btn.click()
    assert w.current() == "signup" and not t._poll.isActive()


def test_try_step_typing_is_not_a_dictation(wizard):
    from PySide6.QtTest import QTest
    w, app = wizard()
    w.go("try")
    QTest.keyClicks(w.try_.edit, "typed by hand")
    assert w.try_.edit.toPlainText() == "typed by hand"
    assert w.first_dictation() == "" and not w.try_.works.isVisibleTo(w.try_)
    w.try_.leave()


def test_change_shortcut(wizard):
    w, app = wizard()
    w.go("try")
    t = w.try_
    assert t.shortcut.button.text() == "Change shortcut"
    assert not t.shortcut.keys.isVisibleTo(t)
    calls = []
    app.suspend_hotkeys = lambda on: calls.append(on)
    t.shortcut.start_capture()
    assert t.shortcut.button.text() == "Cancel" and calls == [True]
    t.shortcut.cancel_capture()
    assert calls == [True, False]
    w.hotkey_captured("ctrl+shift+d")
    assert ("set_setting", "hotkey", "ctrl+shift+d") in app.calls
    assert t.keys.keys() == ["Ctrl", "Shift", "D"]
    assert w.hello.keys.accessibleName() == "Ctrl + Shift + D"
    app.set_setting = lambda k, v: False
    w.hotkey_captured("ctrl+q")
    assert t.shortcut.hotkey() == "ctrl+shift+d"
    assert t.shortcut_problem.isVisibleTo(t) and t.shortcut_problem.text()
    t.leave()


def test_change_shortcut_names_the_feature_that_has_the_keys(wizard):
    w, app = wizard()
    w.go("try")
    t = w.try_
    app.set_setting = lambda k, v: False                  # the controller refuses a clash
    w.hotkey_captured("alt+s")
    assert t.shortcut_problem.text() == "Alt + S runs Smart Actions. Pick another."
    w.hotkey_captured("ctrl+alt+a")
    assert t.shortcut_problem.text() == "Ctrl + Alt + A opens Live Assistant. Pick another."
    w.hotkey_captured("ctrl+q")
    assert t.shortcut_problem.text() == "That shortcut can’t be used. Try another."
    assert t.shortcut.hotkey() == "alt+r"
    t.leave()


def test_try_step_takes_a_rich_paste(wizard):
    """A paste with an HTML flavour (a Smart Action) keeps its list; the
    plain flavour is what counts as the first dictation."""
    w, app = wizard()
    w.go("try")
    t = w.try_
    t.edit.paste_text("• Call David\n• Book the room", html="<ul><li>Call David</li><li>Book the room</li></ul>")
    assert t.edit.toPlainText() == "Call David\nBook the room"
    assert t.edit.document().firstBlock().textList() is not None            # a real list
    assert w.first_dictation() == "• Call David\n• Book the room"
    t.leave()


def test_signed_in_user_finishes_from_try_it(wizard):
    w, app = wizard("free")
    w.go("try")
    assert w.try_.next_btn.text() == "Done"
    w.try_.next_btn.click()
    assert w.is_finished() and w.result() == QDialog.Accepted
    assert ("set_settings", {"onboarding_done": True, "account_gate_seen": True,
                             "analytics_enabled": True}) in app.calls


# ── skip / finish ────────────────────────────────────────────────────────────
def test_skip_goes_to_sign_up_and_skip_for_now_finishes(wizard):
    accepted = []
    w, app = wizard()
    w.accepted.connect(lambda: accepted.append(True))
    w.hello.skip_btn.click()
    assert w.current() == "signup"
    assert w.signup.skip_link.text() == "Skip for now"
    assert not w.signup.first.isVisibleTo(w.signup)    # no dictation yet: no quote
    w.signup.skip_link.click()
    assert accepted == [True] and w.is_finished()
    assert ("set_settings", {"onboarding_done": True, "account_gate_seen": True,
                             "analytics_enabled": True}) in app.calls
    done = [c for c in app.calls if c[0] == "track" and c[1] == "onboarding_completed"]
    assert done and done[0][2]["signed_in"] is False


def test_unticked_usage_stats_are_written(wizard):
    w, app = wizard(analytics_enabled=True)
    w.go("signup")
    assert w.signup.stats.isChecked() and w.signup.stats.text() == "Share anonymous usage stats"
    w.signup.stats.setChecked(False)
    w.signup.skip_link.click()
    assert app.cfg["onboarding_done"] is True and app.cfg["analytics_enabled"] is False


def test_skip_when_signed_in_finishes(wizard):
    w, app = wizard("free")
    w.go("model")
    w.model.skip_btn.click()
    assert w.is_finished()


def test_esc_does_not_close_the_wizard(wizard):
    w, app = wizard()
    ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    w.keyPressEvent(ev)
    QApplication.processEvents()
    assert w.isVisible() and not w.is_finished()


def test_back_walks_the_steps(wizard):
    w, app = wizard()
    w.go("inbox")
    for expected in ("email", "signup", "try", "mic", "model", "hello"):
        w.page(w.current()).back_btn.click()
        assert w.current() == expected
    w.stop_mic()


def test_gpu_download_when_the_recommendation_needs_it(wizard):
    w, app = wizard(nvidia=True)
    w.go("model")
    assert w.model.selected() == "large-v3-turbo"
    w.model.cta.click()
    w.finish()
    assert ("set_settings", {"gpu_offer_intro_done": True}) in app.calls
    assert ("start_gpu_download", "onboarding") in app.calls


def test_no_gpu_download_for_another_model(wizard):
    w, app = wizard(nvidia=True)
    w.go("model")
    w.model.buttons()["base"].click()
    w.model.cta.click()
    w.finish()
    assert ("start_gpu_download", "onboarding") not in app.calls


# ── step 5: sign up ──────────────────────────────────────────────────────────
def test_sign_up_step_copy(wizard):
    w, app = wizard()
    w.go("signup")
    s = w.signup
    texts = _texts(s)
    assert "Keep your words." in texts and "Already have an account?" in texts
    assert s.google.text() == "Continue with Google"
    assert s.email_btn.text() == "Continue with email"
    assert s.benefit.text() == "5 free Smart Actions"
    assert s.skip_btn is None                          # no Skip on the last step


def test_google_sign_in_success_finishes(wizard):
    w, app = wizard()
    app.auth.sign_in_with_google = lambda: True
    w.go("signup")
    w.signup.google.click()
    assert w.is_finished()
    assert ("track", "login_succeeded", {"method": "google"}) in app.calls


def test_google_waiting_cancel_and_late_answer(wizard):
    run = Deferred()
    w, app = wizard(run=run)
    w.go("signup")
    s = w.signup
    s.google.click()
    assert s.google.text() == "Finish in your browser…" and not s.google.isEnabled()
    assert s.skip_link.text() == "Cancel" and not s.email_btn.isEnabled()
    s.skip_link.click()
    assert ("cancel_google",) in app.auth.calls
    assert s.google.text() == "Continue with Google" and s.google.isEnabled()
    assert s.skip_link.text() == "Skip for now"
    app.auth.last_error = "cancelled"
    run.answer(len(run.jobs) - 1, False)               # the browser gave up after the Cancel
    assert not w.is_finished() and w.current() == "signup" and not s.error.isVisibleTo(s)
    assert not [c for c in app.calls if c[0] == "track" and c[1] == "login_failed"]


def test_google_failure_shows_why(wizard):
    from ui import auth_sheet
    w, app = wizard()
    app.auth.last_error = "timeout"
    w.go("signup")
    w.signup.google.click()
    assert not w.is_finished()
    assert w.signup.error.text() == auth_sheet.MSG_GOOGLE["timeout"]
    assert ("track", "login_failed", {"method": "google", "reason": "timeout"}) in app.calls


def test_email_sign_up_verify_inbox_resend_wrong_email(wizard):
    w, app = wizard()
    w.go("signup")
    w.signup.email_btn.click()
    assert w.current() == "email"
    st = w.email_step
    st.name_field.edit.setText("Aram")
    st.email_field.edit.setText("aram@aibuben.xyz")
    st.password_field.edit.setText("example123")
    assert st.hint.is_ok()
    st.create_btn.click()
    assert ("sign_up", "aram@aibuben.xyz", "example123", "Aram") in app.auth.calls
    assert ("track", "signup_verification_sent", {}) in app.calls
    assert w.current() == "inbox"
    inbox = w.inbox
    assert inbox.email() == "aram@aibuben.xyz"
    assert "aram@aibuben.xyz" in inbox.sent.text() and inbox.open_btn.text() == "Open Gmail"
    inbox.resend_btn.click()
    assert ("resend", "aram@aibuben.xyz") in app.auth.calls
    assert inbox.resend_btn.text() == "Sent" and not inbox.resend_btn.isEnabled()
    inbox.wrong_btn.click()
    assert w.current() == "email"
    assert st.email_field.edit.selectedText() == "aram@aibuben.xyz"


def test_inbox_signs_in_once_the_email_is_confirmed(wizard):
    w, app = wizard()
    w.go("email")
    st = w.email_step
    st.email_field.edit.setText("aram@outlook.com")
    st.password_field.edit.setText("example123")
    w.create_account()
    assert w.current() == "inbox" and w.inbox.open_btn.text() == "Open Outlook"
    assert w.retry_sign_in() is True                   # not confirmed yet: quiet
    assert ("sign_in", "aram@outlook.com", "example123") in app.auth.calls
    assert not w.is_finished() and not w.inbox.message.isVisibleTo(w.inbox)
    assert not [c for c in app.calls if c[0] == "track" and c[1] == "login_failed"]
    app.auth.sign_in_email = lambda e, p: ("ok", "")
    w._last_retry = 0
    w.retry_sign_in()
    assert w.is_finished()


def test_email_form_checks_before_sending(wizard):
    from ui import auth_sheet
    w, app = wizard()
    w.go("email")
    st = w.email_step
    st.email_field.edit.setText("not-an-email")
    st.password_field.edit.setText("example123")
    w.create_account()
    assert st.error.text() == auth_sheet.MSG_BAD_EMAIL and st.email_field.box.has_error()
    st.email_field.edit.setText("aram@aibuben.xyz")
    st.password_field.edit.setText("abc")
    assert not st.hint.is_ok()
    w.create_account()
    assert st.error.text() == auth_sheet.MSG_SHORT_PASSWORD
    assert not [c for c in app.auth.calls if c[0] == "sign_up"]


def test_email_sign_up_that_signs_in_finishes(wizard):
    w, app = wizard()
    app.auth.sign_up_email = lambda e, p, n=None: ("ok", "")
    w.go("email")
    st = w.email_step
    st.email_field.edit.setText("aram@aibuben.xyz")
    st.password_field.edit.setText("example123")
    st.create_btn.click()
    assert w.is_finished()


def test_email_sign_up_error_is_shown(wizard):
    w, app = wizard()
    app.auth.last_error = "user_already_exists"
    app.auth.sign_up_email = lambda e, p, n=None: ("error", "User already registered")
    w.go("email")
    st = w.email_step
    st.email_field.edit.setText("aram@aibuben.xyz")
    st.password_field.edit.setText("example123")
    st.create_btn.click()
    assert st.error.text() == "User already registered" and st.create_btn.isEnabled()
    assert ("track", "login_failed", {"method": "email", "mode": "signup",
                                      "reason": "user_already_exists"}) in app.calls


def test_password_eye(wizard):
    w, app = wizard()
    w.go("email")
    pf = w.email_step.password_field
    assert pf.edit.echoMode() == QLineEdit.Password and pf.eye.accessibleName() == "Show password"
    pf.eye.click()
    assert pf.edit.echoMode() == QLineEdit.Normal and pf.eye.accessibleName() == "Hide password"


def test_auth_changed_on_the_last_step_finishes(wizard):
    w, app = wizard()
    w.go("signup")
    app.set_tier("free")
    w.on_auth_changed()
    assert w.is_finished()


def test_hello_sign_in_opens_the_sheet_then_continues_setup(wizard):
    from ui.auth_sheet import AuthSheet
    w, app = wizard()
    w.hello.signin_btn.click()
    sheet = w.sheet
    assert isinstance(sheet, AuthSheet) and sheet.mode == "signin"
    app.auth.sign_in_email = lambda e, p: ("ok", "")
    sheet.email_field.edit.setText("aram@aibuben.xyz")
    sheet.password_field.edit.setText("example123")
    app.set_tier("free")
    sheet.submit.click()
    assert w.current() == "model" and not w.is_finished()


def test_setting_changed_refreshes_the_shortcut(wizard):
    w, app = wizard()
    app.cfg["hotkey"] = "ctrl+alt+r"
    w.on_setting_changed("hotkey")
    assert w.try_.keys.keys() == ["Ctrl", "Alt", "R"]
    assert w.hello.keys.accessibleName() == "Ctrl + Alt + R"


def test_create_returns_the_wizard(made):
    from ui import onboarding_v2 as onb
    app = app_for()
    w = onb.create(app)
    made.append(w)
    assert isinstance(w, onb.OnboardingWizard) and w.windowTitle() == "Welcome to Gusan"
    w.stop_mic()


# ── the sign-in sheet ────────────────────────────────────────────────────────
@pytest.fixture
def sheet(made):
    from ui.auth_sheet import AuthSheet

    def build(mode="signin", run=sync_run, tier="guest"):
        app = app_for(tier)
        s = AuthSheet(app, mode=mode, run=run)
        made.append(s)
        s.show()
        QApplication.processEvents()
        return s, app
    return build


def test_sheet_modes_and_names(sheet):
    s, app = sheet("signin")
    assert s.title.text() == "Sign in" and s.submit.text() == "Sign in"
    assert s.forgot.isVisibleTo(s) and not s.name_field.isVisibleTo(s)
    missing = [type(wd).__name__ for wd in _visible_controls(s) if not _name_of(wd)]
    assert not missing
    s.switch_btn.click()
    assert s.mode == "signup" and s.title.text() == "Create your account"
    assert s.submit.text() == "Create account" and s.name_field.isVisibleTo(s) and not s.forgot.isVisibleTo(s)
    assert s.switch_btn.text() == "Sign in"


def test_sheet_sign_in_ok(sheet):
    s, app = sheet("signin")
    got = []
    s.signed_in.connect(lambda: got.append(True))
    app.auth.sign_in_email = lambda e, p: ("ok", "")
    s.email_field.edit.setText("aram@aibuben.xyz")
    s.password_field.edit.setText("example123")
    s.submit.click()
    assert got == [True] and s.result() == QDialog.Accepted
    assert ("track", "login_succeeded", {"method": "email", "mode": "signin"}) in app.calls


def test_sheet_sign_in_error_stays(sheet):
    s, app = sheet("signin")
    app.auth.last_error = "invalid_credentials"
    app.auth.sign_in_email = lambda e, p: ("error", "Incorrect email or password.")
    s.email_field.edit.setText("aram@aibuben.xyz")
    s.password_field.edit.setText("wrongpass")
    s.submit.click()
    assert s.isVisible() and s.message.text() == "Incorrect email or password."
    assert s.submit.isEnabled() and s.submit.text() == "Sign in"


def test_sheet_sign_up_verify_then_sign_in(sheet):
    s, app = sheet("signup")
    s.email_field.edit.setText("aram@aibuben.xyz")
    s.password_field.edit.setText("example123")
    s.submit.click()
    assert s.page_name() == "inbox" and "aram@aibuben.xyz" in s.inbox_line.text()
    s.resend_btn.click()
    assert ("resend", "aram@aibuben.xyz") in app.auth.calls
    s.inbox_signin.click()
    assert s.inbox_message.text().startswith("Please verify")
    app.auth.sign_in_email = lambda e, p: ("ok", "")
    s.inbox_signin.click()
    assert s.result() == QDialog.Accepted
    s2, _ = sheet("signup")
    s2.email_field.edit.setText("aram@aibuben.xyz")
    s2.password_field.edit.setText("example123")
    s2.submit.click()
    s2.wrong_btn.click()
    assert s2.page_name() == "form" and s2.mode == "signup"


def test_sheet_google_wait_and_cancel(sheet):
    run = Deferred()
    s, app = sheet("signin", run=run)
    s.google_btn.click()
    assert s.page_name() == "google"
    s.google_cancel.click()
    assert s.page_name() == "form" and ("cancel_google",) in app.auth.calls


def test_sheet_forgot_password(sheet):
    s, app = sheet("signin")
    s.email_field.edit.setText("")
    s.forgot.click()
    assert s.message.text() == "Enter your email first."
    s.email_field.edit.setText("aram@aibuben.xyz")
    s.forgot.click()
    assert ("reset", "aram@aibuben.xyz") in app.auth.calls


def test_sheet_says_when_a_reset_or_resend_was_not_sent(sheet):
    from ui import auth_sheet
    s, app = sheet("signin")
    app.auth.send_password_reset = lambda e: False
    s.email_field.edit.setText("aram@aibuben.xyz")
    s.forgot.click()
    assert s.message.text() == auth_sheet.MSG_NOT_SENT
    s2, app2 = sheet("signup")
    app2.auth.resend_verification = lambda e: False
    s2.email_field.edit.setText("aram@aibuben.xyz")
    s2.password_field.edit.setText("example123")
    s2.submit.click()
    s2.resend_btn.click()
    assert s2.inbox_message.text() == auth_sheet.MSG_NOT_SENT and s2.inbox_message.isVisibleTo(s2)
    assert s2.resend_btn.isEnabled() and s2.resend_btn.text() == "Resend"


def test_wizard_resend_failure_can_be_retried(wizard):
    w, app = wizard()
    app.auth.resend_verification = lambda e: False
    w.go("email")
    st = w.email_step
    st.email_field.edit.setText("aram@aibuben.xyz")
    st.password_field.edit.setText("example123")
    w.create_account()
    w.inbox.resend_btn.click()
    assert w.inbox.message.isVisibleTo(w.inbox) and w.inbox.resend_btn.isEnabled()


def test_sheet_listens_to_sign_ins_only_while_open(made):
    from ui.auth_sheet import AuthSheet
    app = app_for()
    ctl = FakeController()
    app.sig_auth_changed = ctl.sig_auth_changed
    s = AuthSheet(app, mode="signin", run=sync_run)
    made.append(s)
    s.show()
    got = []
    s.signed_in.connect(lambda: got.append(True))
    s.reject()
    app.set_tier("free")
    ctl.sig_auth_changed.emit()                        # closed: nothing happens
    assert got == [] and s._auth_sig is None
    app.set_tier("guest")
    s.show()
    app.set_tier("free")
    ctl.sig_auth_changed.emit()                        # open again: signed in elsewhere -> done
    assert got == [True] and s.result() == QDialog.Accepted


def test_show_auth_sheet_reuses_one(made):
    from ui import auth_sheet
    app = app_for()
    a = auth_sheet.show_auth_sheet(app, "signin")
    made.append(a)
    b = auth_sheet.show_auth_sheet(app, "signup")
    assert a is b and a.mode == "signup"
    a.close()


def test_mailbox_and_email_helpers():
    from ui import auth_sheet
    from ui import onboarding_v2 as onb
    assert auth_sheet.mailbox_for("x@gmail.com")[0] == "Open Gmail"
    assert auth_sheet.mailbox_for("x@hotmail.com")[0] == "Open Outlook"
    assert auth_sheet.mailbox_for("x@aibuben.xyz") is None
    assert onb.mailbox("x@aibuben.xyz")[0] == "Open Gmail"
    assert auth_sheet.valid_email("aram@aibuben.xyz") and not auth_sheet.valid_email("aram@")


# ── mic_monitor ──────────────────────────────────────────────────────────────
class FakeStream:
    def __init__(self, cb):
        self.cb = cb
        self.closed = False
        self.active = True

    def is_active(self):
        return self.active

    def start_stream(self):
        self.active = True

    def stop_stream(self):
        self.active = False

    def close(self):
        self.closed = True


class FakePA:
    def __init__(self, fail_rates=(), fail_all=False, default=1):
        self.opened, self.streams = [], []
        self.fail_rates, self.fail_all, self.default = set(fail_rates), fail_all, default
        self.devs = [
            {"index": 0, "name": "Microsoft Sound Mapper - Input", "maxInputChannels": 2, "hostApi": 0},
            {"index": 1, "name": "Microphone (Realtek(R) Audio)", "maxInputChannels": 2, "hostApi": 0,
             "defaultSampleRate": 44100.0},
            {"index": 2, "name": "Speakers (Realtek(R) Audio)", "maxInputChannels": 0, "hostApi": 0},
            {"index": 3, "name": "Headset (Jabra)", "maxInputChannels": 1, "hostApi": 0},
            {"index": 4, "name": "Microphone (Realtek(R) Audio)", "maxInputChannels": 2, "hostApi": 0},
            {"index": 5, "name": "Stereo Mix (Realtek(R) Audio)", "maxInputChannels": 2, "hostApi": 0},
            {"index": 6, "name": "Microphone (Realtek HD)", "maxInputChannels": 2, "hostApi": 2},
            {"index": 7, "name": "Speakers [Loopback]", "maxInputChannels": 2, "hostApi": 0},
        ]

    def get_default_host_api_info(self):
        return {"index": 0}

    def get_device_count(self):
        return len(self.devs)

    def get_device_info_by_index(self, i):
        return self.devs[i]

    def get_default_input_device_info(self):
        if self.default is None:
            raise OSError("no default input")
        return {"index": self.default}

    def get_format_from_width(self, w):
        return 8

    def open(self, **kw):
        self.opened.append(kw)
        if self.fail_all or kw["rate"] in self.fail_rates:
            raise OSError("Invalid sample rate")
        s = FakeStream(kw["stream_callback"])
        self.streams.append(s)
        return s


def _monitor(pa):
    import mic_monitor
    return mic_monitor.MicMonitor(pa=pa, spawn=lambda fn, *a: fn(*a))


def test_mic_monitor_lists_microphones_like_settings(qapp):
    import mic_monitor
    m = _monitor(FakePA())
    assert m.devices() == [(1, "Microphone (Realtek Audio)"), (3, "Headset (Jabra)")]
    assert m.default_index() == 1
    got = []
    m.devices_ready.connect(lambda devs, default: got.append((devs, default)))
    m.refresh_devices()
    assert got == [([(1, "Microphone (Realtek Audio)"), (3, "Headset (Jabra)")], 1)]
    assert mic_monitor.clean_device_name("Microphone (Realtek High Defini") == "Microphone (Realtek High Defini…"


def test_mic_monitor_measures_and_closes(qapp):
    import mic_monitor
    pa = FakePA()
    m = _monitor(pa)
    levels, opened = [], []
    m.level_changed.connect(levels.append)
    m.opened.connect(opened.append)
    m.start(3)
    kw = pa.opened[-1]
    assert kw["rate"] == mic_monitor.RATE and kw["channels"] == 1 and kw["input"] is True
    assert kw["frames_per_buffer"] == mic_monitor.BLOCK and kw["input_device_index"] == 3
    assert opened == [3] and m.is_running()
    import array
    loud = array.array("h", [8000, -8000] * 266).tobytes()
    pa.streams[-1].cb(loud, 532, None, 0)
    assert levels and abs(levels[-1] - 8000 / 32768.0) < 1e-3
    assert abs(m.rms - 8000 / 32768.0) < 1e-3 and abs(m.level - 8000 / 32768.0) < 1e-3
    m.stop()
    assert pa.streams[-1].closed and not m.is_running() and m.level == 0.0
    pa.streams[-1].cb(loud, 532, None, 0)               # a late block after stop: no signal
    assert len(levels) == 1


def test_mic_monitor_restart_replaces_the_stream(qapp):
    pa = FakePA()
    m = _monitor(pa)
    m.start(None)
    first = pa.streams[-1]
    assert "input_device_index" not in pa.opened[-1]
    m.start(1)
    assert first.closed and not pa.streams[-1].closed
    m.close()
    assert pa.streams[-1].closed


def test_mic_monitor_drops_blocks_from_a_replaced_stream(qapp):
    import array
    pa = FakePA()
    m = _monitor(pa)
    levels = []
    m.level_changed.connect(levels.append)
    m.start(1)
    old = pa.streams[-1]
    m.start(3)
    loud = array.array("h", [8000, -8000] * 266).tobytes()
    old.cb(loud, 532, None, 0)                          # the old stream, still closing
    assert levels == [] and m.rms == 0.0
    pa.streams[-1].cb(loud, 532, None, 0)
    assert len(levels) == 1 and m.rms > 0.2


def test_mic_monitor_close_terminates_only_its_own_pyaudio(qapp):
    import mic_monitor
    shared = FakePA()
    shared.terminated = False
    shared.terminate = lambda: setattr(shared, "terminated", True)
    m = _monitor(shared)
    m.start(1)
    m.close()
    assert shared.streams[-1].closed and not shared.terminated     # the app's PyAudio stays
    made = []

    def factory():
        pa = FakePA()
        pa.terminated = False
        pa.terminate = lambda: setattr(pa, "terminated", True)
        made.append(pa)
        return pa
    m2 = mic_monitor.MicMonitor(pa_factory=factory, spawn=lambda fn, *a: fn(*a))
    m2.start(None)
    assert len(made) == 1 and made[0].streams
    m2.close()
    assert made[0].streams[-1].closed and made[0].terminated
    jobs = []
    m3 = mic_monitor.MicMonitor(pa_factory=factory, spawn=lambda fn, *a: jobs.append((fn, a)))
    m3.start(None)
    m3.close()                                          # closed before the open ran
    for fn, a in jobs:
        fn(*a)
    assert len(made) == 1                               # no PyAudio made for nothing


def test_mic_monitor_falls_back_and_fails_plainly(qapp):
    import mic_monitor
    pa = FakePA(fail_rates=(16000,))
    m = _monitor(pa)
    m.start(1)
    assert [k["rate"] for k in pa.opened] == [16000, 48000]
    m.stop()
    pa2 = FakePA(fail_all=True)
    m2 = _monitor(pa2)
    failed = []
    m2.failed.connect(failed.append)
    m2.start(1)
    assert failed == [mic_monitor.MSG_CANT_OPEN] and m2.error == mic_monitor.MSG_CANT_OPEN
    pa3 = FakePA(fail_all=True, default=None)
    m3 = _monitor(pa3)
    m3.start(None)
    assert m3.error == mic_monitor.MSG_NO_MIC


def test_mic_monitor_levels_math():
    import mic_monitor
    assert mic_monitor.rms_int16(b"") == (0.0, 0.0)
    assert mic_monitor.display_level(0.0) == 0.0
    assert mic_monitor.display_level(1.0) == 1.0
    assert 0.0 < mic_monitor.display_level(0.01) < mic_monitor.display_level(0.05) < 1.0
    assert mic_monitor.level_db(0.1) == pytest.approx(-20.0)
