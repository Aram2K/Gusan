"""The Interpreter's UI (Gusan 2.2), offscreen and with fakes - no network, no sound devices:

* ui/pages/interpreter.py - the page for Pro / Free / guests, Start (the Pro window for Free), the
  tabs, the language swap, the switches and the rail (incl. "Voice for them"), My voice's three
  states with a fake VoiceStore and a fake meetings scan;
* ui/interpreter_host.py - start / stop / directions / the Meetings hook / telemetry / notices;
* ui/live_assist.py + ui/live_interp.py - the card's chip, the direction row and the captions;
* the Meetings start card's and the Live Assistant rail's Interpreter rows;
* settings_apply - the Interpreter shortcut never takes another shortcut's keys.
"""
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

from tests_qt.conftest import make_stub_app

VOICE = {"voice_id": "v1", "source": "meetings", "created_at": "2026-11-07T10:00:00Z"}


def _spin(n=5):
    for _ in range(n):
        QApplication.processEvents()
        time.sleep(0.005)


def inline_run(fn, then=None, error=None, name=""):
    try:
        result = fn()
    except Exception as e:                     # noqa: BLE001
        if error is not None:
            error(e)
        return
    if then is not None:
        then(result)


class Hold:
    """A run_bg whose jobs wait until run() (the worker has not answered yet)."""

    def __init__(self):
        self.jobs = []

    def __call__(self, fn, then=None, error=None, name=""):
        self.jobs.append((fn, then, error))

    def run(self, i=0):
        inline_run(*self.jobs.pop(i))


class FakeCtl:
    """interpreter.controller.Interpreter's surface, recording every call."""
    made = []

    def __init__(self, token_getter, on_event, settings, fail=None):
        self.on_event = on_event
        self.settings = settings
        self.calls = []
        self.session = SimpleNamespace(info={"seconds_left": 15120, "allowance_seconds": 18000,
                                             "own_voice": True})
        self._dirs = {"hear": 1, "speak": 1}
        self.speakers_mode = False
        self.fail = fail
        FakeCtl.made.append(self)

    def start(self):
        self.calls.append("start")
        if self.fail:
            from interpreter.session import InterpreterError
            raise InterpreterError(self.fail)
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

    def set_voice_choice(self, choice):
        self.calls.append(("voice", choice.mode, tuple(choice.all_ids())))


class FakeVoices:
    def __init__(self, voice=None):
        self.voice = voice
        self.calls = []

    def get(self):
        self.calls.append("get")
        return self.voice

    def create(self, clips, source, phrase):
        self.calls.append(("create", len(clips), source, phrase))
        if not (phrase or "").strip():                       # like the server: no sentence, no voice
            from interpreter.session import InterpreterError
            raise InterpreterError("consent_required", status=400)
        self.voice = {"voice_id": "v2", "source": source, "created_at": "2026-11-07T12:00:00Z"}
        return {"voice_id": "v2", "created_at": "2026-11-07T12:00:00Z"}

    def preview(self, text, language):
        self.calls.append(("preview", language))
        return b"\x00\x00" * 160, 16000

    def delete(self):
        self.calls.append("delete")
        self.voice = None
        return True


class _Sig(QObject):
    changed = Signal(str)


def make_app(tier="pro", fail=None, voice=None, **cfg):
    """make_stub_app + the interpreter's keys, a set_setting that writes and announces (as
    AppController does) and an InterpreterHost on fakes that runs its work inline."""
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
        if app.cfg.get(key) != value or key not in app.cfg:
            app.cfg[key] = value
            sig.changed.emit(key)
        return True

    def set_settings(updates):
        return {k: set_setting(k, v) for k, v in updates.items()}
    app.set_setting = set_setting
    app.set_settings = set_settings
    app.show_main = lambda *a, **k: app.calls.append(("show_main",) + a + tuple(sorted(k.items())))
    app.show_tray_hint = lambda *a, **k: app.calls.append(("tray_hint",) + a)
    app.show_live_assist = lambda: app.calls.append(("show_live_assist",))
    app.voices = FakeVoices(voice)
    host = ih.InterpreterHost(app, factory=lambda tg, oe, st: FakeCtl(tg, oe, st, fail=fail), voices=app.voices)
    host.spawn = lambda fn, name="": fn()
    app.interpreter = host
    return app


def _calls(app, name):
    return [c for c in app.calls if c[0] == name]


def _tracked(app, event):
    return [c[2] for c in app.calls if c[0] == "track" and c[1] == event]


@pytest.fixture
def ip(qapp, monkeypatch):
    from ui.pages import interpreter as ip_mod
    monkeypatch.setattr(ip_mod, "run_bg", inline_run)
    monkeypatch.setattr(ip_mod.InterpreterPage, "check_microphone", lambda self: self._show_mic(""))
    found = _found(372, 12)
    monkeypatch.setattr(ip_mod.voice_builder, "find_own_voice", lambda **k: found)
    return ip_mod


def _found(seconds, meetings):
    from interpreter import voice_builder
    f = voice_builder.Found()
    f.seconds = seconds
    f.meetings = {"m%d" % i for i in range(meetings)}
    f.clips = [(b"RIFF" + b"\x00" * 100, 10.0, "m0")] if seconds else []
    return f


@pytest.fixture
def page(ip):
    made = []

    def build(tier="pro", **kw):
        app = make_app(tier, **kw)
        host = QWidget()
        from ui import theme
        theme.apply(host)
        host.resize(1100, 1000)
        p = ip.create(app)
        p.setParent(host)
        p.resize(1100, 1000)
        app._sig.changed.connect(p.on_setting_changed)
        host.show()
        p.on_open()
        _spin()
        made.append(host)
        return p, app

    yield build
    for h in made:
        h.hide()
        h.deleteLater()
    QApplication.processEvents()


# ── the page in the shell ──
@pytest.mark.parametrize("tier", ["pro", "free", "guest"])
def test_the_page_opens_in_the_shell_for_every_plan(ip, tier):
    from ui import shell as shell_mod
    app = make_stub_app(tier)                    # the plain stub: no interpreter host, no interpreter keys
    win = shell_mod.MainWindow(app)
    try:
        win.show()
        p = win.open("interpreter")
        _spin()
        assert isinstance(p, ip.InterpreterPage)
        assert p.header.title_label.text() == "Interpreter"
        assert p.header.sub_label.text() == "AI multilingual voice clone. They hear you in their language, in your voice."
        assert p.pro_tag.text() == "PRO"                        # the PRO pill by the title, every plan
        assert win.sidebar.items["interpreter"].is_active()
        assert win.sidebar.items["interpreter"].pro_tag is None  # no PRO tags in the sidebar
        # one page: no tabs, no Start button (it starts from Meetings / the Live Assistant / the shortcut)
        assert not hasattr(p, "tabs") and not hasattr(p, "start_button")
        assert p.voice_tab.isVisibleTo(p) and p.picture.isVisibleTo(p)
        assert app.interpreter is p.host                         # a host made for a controller that has none
    finally:
        win.hide()
        win.deleteLater()
        QApplication.processEvents()


def _toggle(p):
    """What the live card's chip / the shortcut do (the page has no Start button)."""
    if p.host.active:
        p.host.stop("user")
    else:
        p.host.start(source="page", parent=p)


@pytest.mark.parametrize("tier", ["free", "guest"])
def test_start_for_free_or_a_guest_opens_the_pro_window(page, tier):
    p, app = page(tier)
    _toggle(p)
    assert _calls(app, "_pro_upsell") == [("_pro_upsell", "Interpreter")]
    assert p.host.state == "idle"


def test_start_and_stop_for_pro(page):
    FakeCtl.made.clear()
    p, app = page("pro")
    _toggle(p)
    assert p.host.state == "running"
    assert ("show_live_assist",) in app.calls                    # the captions live on the live card
    started = _tracked(app, "interpreter_started")
    assert started == [{"hear": True, "speak": True, "pair": "en-ja", "output": "headphones", "source": "page"}]
    ctl = FakeCtl.made[-1]
    assert (ctl.settings.my_lang, ctl.settings.their_lang) == ("en", "ja")      # "" = the Dictation language
    assert p.usage_box.isVisibleTo(p) and p.usage_label.text() == "48 min of 5 h"
    _toggle(p)
    assert p.host.state == "idle" and ("stop", "user") in ctl.calls
    stopped = _tracked(app, "interpreter_stopped")
    assert len(stopped) == 1 and stopped[0]["reason"] == "user" and set(stopped[0]) == {"reason", "minutes", "latency"}


def test_privacy_mode_asks_first(page, monkeypatch):
    from ui import privacy_prompt
    asked = []
    monkeypatch.setattr(privacy_prompt, "confirm_turn_off_privacy", lambda parent, f: asked.append(f) or False)
    p, app = page("pro", privacy_mode=True)
    _toggle(p)
    assert asked == ["The interpreter"] and p.host.state == "idle" and app.cfg["privacy_mode"] is True
    monkeypatch.setattr(privacy_prompt, "confirm_turn_off_privacy", lambda parent, f: True)
    _toggle(p)
    assert app.cfg["privacy_mode"] is False and p.host.state == "running"


def test_a_server_refusal_is_one_calm_sentence(page):
    p, app = page("pro", fail="allowance_used")
    _toggle(p)
    assert p.host.state == "idle"
    assert p.notice_label.text() == "You've used this month's interpreter time."
    assert p.notice_box.isVisibleTo(p) and not p.notice_link.isVisibleTo(p)
    assert _tracked(app, "interpreter_failed") == [{"code": "allowance_used"}]


def test_no_virtual_mic_says_so_with_a_set_up_link(page):
    p, app = page("pro")
    p.host.say("no_virtual_mic")
    from ui import interpreter_host as ih
    assert p.notice_label.text() == ih.NO_VIRTUAL_MIC
    assert p.notice_link.isVisibleTo(p) and p.notice_link.text() == "Set up"
    p.notice_link.click()
    assert p.mic_hint.isVisibleTo(p)


# ── one page: the set-up card, your voice under it ──
def test_one_page_the_card_then_your_voice(page):
    p, _app = page("pro")
    lay = p.picture.parentWidget().layout().itemAt(0).layout()
    order = [lay.itemAt(i).widget() for i in range(lay.count()) if lay.itemAt(i).widget() is not None]
    assert order.index(p.picture) < order.index(p.voice_tab)          # Clone your voice is below the card
    # the languages, the examples, the switches and Try it are in ONE card
    for w in (p.my_combo, p.their_combo, p.swap_button, p.speak_row, p.hear_row, p.picture.try_button,
              p.picture.mine[0], p.picture.theirs[1]):
        assert p.picture.isAncestorOf(w)
    assert p.picture.you_label.text() == "I speak" and p.picture.them_label.text() == "They speak"
    p.on_open(tab="voice")                                             # old "My voice" links still land
    assert p.voice_tab.isVisibleTo(p)


def test_the_languages_and_the_swap(page):
    p, app = page("pro")
    assert (p.my_combo.code(), p.their_combo.code()) == ("en", "ja")
    assert p.speak_row.title.text() == "They hear me in Japanese"
    assert p.hear_row.title.text() == "I hear them in English"
    p.swap_button.click()
    assert (app.cfg["interpreter_my_language"], app.cfg["interpreter_their_language"]) == ("ja", "en")
    assert (p.my_combo.code(), p.their_combo.code()) == ("ja", "en")
    assert p.speak_row.title.text() == "They hear me in English"
    p.their_combo.setCurrentIndex(p.their_combo.findData("fr"))
    p.their_combo.activated.emit(p.their_combo.currentIndex())
    assert app.cfg["interpreter_their_language"] == "fr"
    p.my_combo.setCurrentIndex(p.my_combo.findData("fr"))           # the same as theirs: they swap
    p.my_combo.activated.emit(p.my_combo.currentIndex())
    assert (app.cfg["interpreter_my_language"], app.cfg["interpreter_their_language"]) == ("fr", "ja")
    assert (p.my_combo.currentText(), p.their_combo.currentText()) == ("French", "Japanese")
    assert p.picture.mine[0].text() == "On peut commencer à trois heures ?"       # the examples follow


def test_the_picture_greys_out_a_direction_that_is_off(page):
    p, app = page("pro")
    pic = p.picture
    assert pic.speak_arrow.is_on() and pic.hear_arrow.is_on()
    assert all(b.is_on() for b in pic.mine + pic.theirs)
    p.speak_row.toggle.click()                                         # they hear me: off
    assert app.cfg["interpreter_speak"] is False
    assert not pic.speak_arrow.is_on() and pic.speak_arrow.label() == "off"
    assert not pic.mine[0].is_on() and not pic.theirs[0].is_on()
    assert pic.hear_arrow.is_on() and pic.mine[1].is_on() and pic.theirs[1].is_on()
    p.hear_row.toggle.click()                                          # I hear them: off too
    assert not pic.hear_arrow.is_on() and not pic.theirs[1].is_on()
    p.speak_row.toggle.click()
    p.hear_row.toggle.click()
    assert pic.speak_arrow.is_on() and pic.hear_arrow.label() == "in your ear"
    assert all(b.is_on() for b in pic.mine + pic.theirs)


def test_language_lists_are_as_wide_as_their_rows(page):
    p, _app = page("pro")
    p.my_combo.showPopup()
    try:
        fm = p.my_combo.fontMetrics()
        widest = max(fm.horizontalAdvance(p.my_combo.itemText(i)) for i in range(p.my_combo.count()))
        assert p.my_combo.view().minimumWidth() >= widest
    finally:
        p.my_combo.hidePopup()


def test_the_switches_write_config(page):
    p, app = page("pro")
    p.speak_row.toggle.click()
    assert app.cfg["interpreter_speak"] is False
    p.hear_row.toggle.click()
    assert app.cfg["interpreter_hear"] is False
    for toggle, key in ((p.duck_toggle, "interpreter_duck"), (p.under_toggle, "interpreter_keep_underneath"),
                        (p.captions_toggle, "interpreter_captions"), (p.notice_toggle, "interpreter_notice")):
        toggle.click()
        assert app.cfg[key] is False, key
    p.output_seg.buttons()[2].click()
    assert app.cfg["interpreter_output"] == "speakers"
    assert p.hear_row.sub.text() == "Voice only"                    # captions off


def test_both_directions_off_refuses_to_start(page):
    p, app = page("pro", interpreter_hear=False, interpreter_speak=False)
    _toggle(p)
    assert p.host.state == "idle" and p.notice_label.text() == "Turn on at least one direction."


def test_switching_a_direction_while_running_switches_it_live(page):
    FakeCtl.made.clear()
    p, app = page("pro")
    _toggle(p)
    ctl = FakeCtl.made[-1]
    p.speak_row.toggle.click()
    assert ("direction", "speak", False) in ctl.calls and p.host.directions == ("hear",)
    p.hear_row.toggle.click()                                       # the last one off: it stops
    assert p.host.state == "idle"


def test_a_language_change_while_running_starts_it_again(page):
    FakeCtl.made.clear()
    p, app = page("pro")
    _toggle(p)
    p.swap_button.click()
    _spin()
    assert p.host.state == "running" and len(FakeCtl.made) == 2
    assert (FakeCtl.made[-1].settings.my_lang, FakeCtl.made[-1].settings.their_lang) == ("ja", "en")
    assert ("stop", "restart") in FakeCtl.made[0].calls


def test_the_rail_rows(page, ip):
    p, app = page("pro")
    labels = [r.label.text() for r in p.rail.rows() if r.isVisibleTo(p.rail)]
    notice = ["Tell people an AI interpreter is on"] if ip.notice_spoken() else []    # once the engine says it
    assert labels == ["I hear it on", "Turn the call down while translating", "Keep their voice underneath",
                      "Captions", "Voice for them", "Microphone in the call"] + notice + ["Shortcut"]
    assert p.rail.title_label.text() == "Interpreter settings"
    assert [b.text() for b in p.output_seg.buttons()] == ["Automatic", "Headphones", "Speakers"]
    assert "Scribe v2 Realtime" in p.models_label.text() and "ElevenLabs v4 Turbo" in p.models_label.text()
    assert "DeepSeek V4.1 Flash" in p.models_label.text()
    assert p.shortcut.capture.keys.accessibleName() == "Ctrl + Alt + I"
    assert not p.usage_box.isVisibleTo(p)                           # hidden until the server says


def test_tell_people_shows_only_once_the_engine_says_it(page, monkeypatch):
    from interpreter import controller
    # an engine that says nothing: no switch that would let people believe the others were told
    monkeypatch.setattr(controller.Settings, "from_config", staticmethod(lambda cfg: SimpleNamespace()))
    p, _app = page("pro")
    assert not p.notice_toggle._row.isVisibleTo(p.rail)
    # an engine that reads it (Settings.notice): the switch is there
    monkeypatch.setattr(controller.Settings, "from_config",
                        staticmethod(lambda cfg: SimpleNamespace(notice=bool(cfg.get("interpreter_notice")))))
    p, _app = page("pro")
    assert p.notice_toggle._row.isVisibleTo(p.rail)


def test_the_microphone_row(page, monkeypatch):
    p, app = page("pro")
    assert p.mic_chip.text() == "Not set up" and p.mic_setup.isVisibleTo(p)
    opened = []
    from ui.pages import interpreter as ip_mod
    monkeypatch.setattr(ip_mod.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    p.mic_setup.click()
    assert opened == [ip_mod.CABLE_URL]
    assert p.mic_hint.isVisibleTo(p) and p.mic_hint.text() == ip_mod.CABLE_HINT
    p._show_mic("CABLE Output")
    assert "CABLE Output" in p.mic_chip.text() and not p.mic_setup.isVisibleTo(p)
    p._show_mic("Gusan Microphone")
    assert "Gusan Microphone" in p.mic_chip.text()


def test_the_shortcut_row_writes_the_setting(page):
    p, app = page("pro")
    p.shortcut.capture.captured.emit("ctrl+alt+j")
    assert app.cfg["interpreter_hotkey"] == "ctrl+alt+j"
    assert p.shortcut.capture.keys.accessibleName() == "Ctrl + Alt + J"


# ── Voice for them ──
def test_voice_for_them_match_and_one_voice(page):
    from interpreter import voices
    p, app = page("pro")
    row = p.voice_row
    assert row.mode_combo.currentData() == voices.MATCH
    assert row.female_line.isVisibleTo(p) and row.male_line.isVisibleTo(p) and row.hint.isVisibleTo(p)
    assert not row.fixed_line.isVisibleTo(p)
    assert row.hint.text() == "A woman's or a man's voice, picked once when they start talking."
    assert [row.female_combo.itemText(i) for i in range(row.female_combo.count())] == \
        [voices.label(v[0]) for v in voices.of_gender("female")]
    assert [row.male_combo.itemData(i) for i in range(row.male_combo.count())] == \
        [v[0] for v in voices.of_gender("male")]
    assert row.fixed_combo.count() == len(voices.PRESETS)
    row.mode_combo.setCurrentIndex(1)
    row.mode_combo.activated.emit(1)
    assert app.cfg["interpreter_voice_mode"] == voices.FIXED
    assert row.fixed_line.isVisibleTo(p) and not row.female_line.isVisibleTo(p) and not row.hint.isVisibleTo(p)
    i = row.fixed_combo.findData("JBFqnCBsd6RMkjVDRZzb")
    row.fixed_combo.setCurrentIndex(i)
    row.fixed_combo.activated.emit(i)
    assert app.cfg["interpreter_voice_fixed"] == "JBFqnCBsd6RMkjVDRZzb"
    row.mode_combo.setCurrentIndex(0)
    row.mode_combo.activated.emit(0)
    i = row.female_combo.findData("cgSgspJ2msm6clMCkdW9")
    row.female_combo.setCurrentIndex(i)
    row.female_combo.activated.emit(i)
    i = row.male_combo.findData("nPczCjzI2devNBz1zQrb")
    row.male_combo.setCurrentIndex(i)
    row.male_combo.activated.emit(i)
    assert (app.cfg["interpreter_voice_female"], app.cfg["interpreter_voice_male"]) == \
        ("cgSgspJ2msm6clMCkdW9", "nPczCjzI2devNBz1zQrb")


def test_voice_for_them_follows_config_and_a_running_interpreter_takes_it(page):
    FakeCtl.made.clear()
    p, app = page("pro")
    _toggle(p)
    app.set_setting("interpreter_voice_mode", "fixed")
    assert p.voice_row.fixed_line.isVisibleTo(p)
    assert ("voice", "fixed", ("SAz9YHcvj6GT2YYXdXww",)) in FakeCtl.made[-1].calls


def test_a_sample_plays_in_the_app_and_a_missing_file_turns_its_button_off(page, monkeypatch, tmp_path):
    from interpreter import voice_builder, voices
    from ui import interpreter_audio
    p, app = page("pro")
    row = p.voice_row
    wav = tmp_path / "s.wav"
    wav.write_bytes(voice_builder.to_wav_bytes([0] * 1600))
    monkeypatch.setattr(voices, "sample_path", lambda vid: str(wav) if vid == "EXAVITQu4vr4xnSDxMaL" else str(tmp_path / "x.wav"))
    played = []
    monkeypatch.setattr(interpreter_audio, "play_pcm", lambda pcm, rate, stop=None: played.append((len(pcm), rate)))
    row.refresh(app.cfg)
    assert row.female_play.isEnabled() and not row.male_play.isEnabled()
    row.female_play.click()
    assert played == [(3200, 16000)]


def ip_consent_text():
    from ui.pages import interpreter as page_mod
    return page_mod.CONSENT_TEXT


# ── My voice ──
def test_my_voice_from_my_meetings(page):
    p, app = page("pro")
    p.set_tab(1)
    vt = p.voice_tab
    assert vt.state() == "create" and app.voices.calls[:1] == ["get"]
    assert vt.opt_meetings.radio.isChecked()
    assert vt.opt_meetings.sub.text() == "6 min of you in 12 meetings"
    assert vt.opt_meetings.chip.text() == "Enough"
    assert not vt.create_button.isEnabled()                     # off until the box is ticked
    vt.create_button.click()
    assert not [c for c in app.voices.calls if c[0] == "create"] and vt.create_error.isHidden()
    assert "*" in vt.consent_text.text()                        # marked required
    vt.consent_text.clicked.emit()                              # the words tick it too
    assert vt.consent.isChecked() and vt.create_button.isEnabled()
    vt.consent.setChecked(False)
    assert not vt.create_button.isEnabled()
    vt.consent.setChecked(True)
    vt.create_button.click()
    assert ("create", 1, "meetings", ip_consent_text()) in app.voices.calls
    assert vt.state() == "ready"
    assert vt.made_label.text() == "Made 7 Nov from 12 meetings"
    assert _tracked(app, "voice_created") == [{"source": "meetings"}]
    assert p.speak_row.sub.text() == "In my voice"


def test_not_enough_in_the_meetings_picks_record_now(page, ip, monkeypatch):
    monkeypatch.setattr(ip.voice_builder, "find_own_voice", lambda **k: _found(20, 2))
    p, _app = page("pro")
    p.set_tab(1)
    vt = p.voice_tab
    assert vt.opt_record.radio.isChecked() and vt.opt_meetings.chip.text() == "Not enough yet"
    # only calls count (at a table everyone is on the mic): none yet says so
    monkeypatch.setattr(ip.voice_builder, "find_own_voice", lambda **k: _found(0, 0))
    p, _app = page("pro")
    assert p.voice_tab.opt_meetings.sub.text() == "No calls with your microphone yet."


def test_record_now_waits_for_record_and_next(page, ip, monkeypatch):
    from interpreter import voice_builder
    takes = []
    lengths = [3, 6, 7, 6, 6]                     # the first take is too short; the second is redone
    monkeypatch.setattr(ip.interpreter_audio, "record_mic",
                        lambda **k: takes.append(1) or voice_builder.to_wav_bytes([100] * (lengths.pop(0) * 16000)))
    played = []
    monkeypatch.setattr(ip.interpreter_audio, "play_pcm", lambda pcm, rate, stop=None: played.append((len(pcm), rate)))
    monkeypatch.setattr(voice_builder, "consent_phrase", lambda rng=None: "blue river seven")
    p, app = page("pro")
    vt = p.voice_tab
    vt.opt_record.radio.setChecked(True)
    vt.consent.setChecked(True)
    vt.create_button.click()
    # nothing records by itself: the text waits for the Record button
    assert vt.state() == "record" and vt.rec_mode() == "ready" and takes == []
    assert vt.rec_button.mode() == "record" and vt.rec_time.text() == "Press to record"
    assert vt.step_label.text() == "1 of 3" and vt.rec_foot.text() == "Read it at your normal pace."
    vt.rec_button.click()                                          # 3 s: too short, read it again
    assert vt.rec_mode() == "ready" and vt.rec_note.text() == "That was short. Read it once more."
    vt.rec_button.click()                                          # 6 s
    # it does not move on by itself: play it back, read it again, or Next
    assert vt.rec_mode() == "recorded" and vt.steps._step == 0 and vt.take_actions.isVisibleTo(vt)
    assert not vt.rec_button.isVisibleTo(vt) and vt.rec_time.text() == "0:06 recorded"
    vt.play_button.click()
    assert played == [(6 * 32000, 16000)] and vt.play_button.text() == "Play"
    vt.redo_button.click()                                         # 7 s replaces it
    assert vt.rec_mode() == "recorded" and vt.rec_time.text() == "0:07 recorded"
    vt.next_button.click()
    assert vt.steps._step == 1 and vt.rec_mode() == "ready" and vt.step_label.text() == "2 of 3"
    vt.rec_button.click()
    vt.next_button.click()
    assert vt.step_label.text() == "3 of 3" and vt.next_button.text() == "Clone my voice"
    assert "blue river seven" in vt.read_box.text() and vt.rec_foot.text().startswith("Last text")
    vt.rec_button.click()
    assert not [c for c in app.voices.calls if c[0] == "create"]   # still waiting for the press
    vt.next_button.click()
    assert len(takes) == 5
    assert ("create", 3, "recorded", "blue river seven") in app.voices.calls
    assert vt.state() == "ready" and vt.made_label.text().endswith("from a recording")


def test_record_now_shows_each_step(page, ip, monkeypatch):
    monkeypatch.setattr(ip, "run_bg", lambda fn, then=None, error=None, name="": None)    # the take never ends
    from interpreter import voice_builder
    monkeypatch.setattr(voice_builder, "consent_phrase", lambda rng=None: "green bridge four")
    p, _app = page("pro")
    vt = p.voice_tab
    vt.opt_record.radio.setChecked(True)
    vt.consent.setChecked(True)
    vt.create_button.click()
    assert vt.state() == "record" and vt.steps._step == 0
    assert "Thursday" in vt.read_box.text() and "green bridge four" not in vt.read_box.text()
    vt.rec_button.click()
    assert vt.rec_mode() == "recording" and vt.rec_button.mode() == "stop"
    assert vt.rec_button.accessibleName() == "Stop recording"
    vt._step = 2
    vt._show_take()
    assert "green bridge four" in vt.read_box.text() and "Then say" in vt.read_box.text()
    vt.rec_cancel.click()
    assert vt.state() == "create"


def test_making_the_voice_shows_a_bar_and_the_time_left(page, ip, monkeypatch):
    p, app = page("pro")
    vt = p.voice_tab
    got = {}

    def hold(fn, then=None, error=None, name=""):
        got["then"] = then                                        # the server has not answered yet

    monkeypatch.setattr(ip, "run_bg", hold)
    vt.consent.setChecked(True)
    vt.create_button.click()
    assert vt.create_button.text() == "Cloning your voice…" and vt.make_box.isVisibleTo(vt)
    assert vt.make_eta.text().startswith("About ") and vt.make_eta.text().endswith(" s left")
    assert vt.make_label.text().startswith("Sending ")
    vt._make_started -= 1000                                       # far past the estimate
    vt._tick_making()
    assert vt.make_eta.text() == "Almost done…" and 900 <= vt.make_bar.value() < 1000
    got["then"]({"voice_id": "v9", "created_at": "2026-11-08T12:00:00Z"})
    assert vt.state() == "ready" and not vt.make_box.isVisibleTo(vt)
    assert vt.create_button.text() == "Clone my voice"


def test_my_voice_ready_previews_and_delete(page, ip, monkeypatch):
    from ui.components import mini_prompt
    played = []
    monkeypatch.setattr(ip.interpreter_audio, "play_pcm", lambda pcm, rate, stop=None: played.append(rate))
    p, app = page("pro", voice=dict(VOICE))
    p.set_tab(1)
    vt = p.voice_tab
    assert vt.state() == "ready"
    # one dropdown with every language (what the others hear first) and Play
    assert vt.preview_combo.code() == "ja" and vt.preview_combo.count() >= 40
    assert vt.preview_combo.findData("en") >= 0 and vt.preview_combo.findData("hy") >= 0
    # the first language's sound was fetched as the card showed: Play starts at once
    assert app.voices.calls.count(("preview", "ja")) == 1
    said = []
    real_preview = app.voices.preview
    monkeypatch.setattr(app.voices, "preview", lambda text, lang: said.append((lang, text)) or real_preview(text, lang))
    vt.preview_play.click()
    assert said == [] and played == [16000]                              # no server call: it was ready
    assert vt.preview_play.text() == "Play" and vt.preview_state() == "idle"
    vt.preview_play.click()                                              # again: the kept sound
    assert said == [] and played == [16000, 16000]
    vt.preview_combo.setCurrentIndex(vt.preview_combo.findData("en"))
    vt.preview_combo.activated.emit(vt.preview_combo.currentIndex())     # picking one plays it
    assert said[-1] == ("en", "Hi, this is my voice in English.") and len(played) == 3
    vt.preview_combo.setCurrentIndex(vt.preview_combo.findData("ar"))
    vt.preview_combo.activated.emit(vt.preview_combo.currentIndex())     # its own sentence, never "3 o'clock"
    assert said[-1] == ("ar", "مرحبًا، هذا صوتي باللغة العربية.")
    # a language we have no sentence for (none today): "Hi, this is my voice in <it>." translated once
    from interpreter import translate
    asked = []
    monkeypatch.setattr(translate, "translate_phrase",
                        lambda text, src, dst, *a, **k: asked.append((text, src, dst)) or "مرحبا، هذا صوتي.")
    monkeypatch.setattr(ip, "_SAMPLE_CACHE", {})
    monkeypatch.delitem(ip.SAMPLES, "ar")
    vt._preview_audio.clear()
    vt.preview_play.click()
    assert asked == [("Hi, this is my voice in Arabic.", "en", "ar")]
    assert said[-1] == ("ar", "مرحبا، هذا صوتي.") and ip.LINES["ar"][0] not in [t for _l, t in said]
    vt._preview_audio.clear()
    vt.preview_play.click()
    assert len(asked) == 1 and said[-1] == ("ar", "مرحبا، هذا صوتي.")    # the same words every time
    # loading: a turning arc and "Loading…"; a press cancels it
    inline = ip.run_bg
    monkeypatch.setattr(ip, "run_bg", lambda fn, then=None, error=None, name="": None)
    vt._preview_audio.clear()
    vt.preview_play.click()
    assert vt.preview_state() == "loading" and vt.preview_play.text() == "Loading…"
    assert vt.preview_play.toolTip() == "Cancel"
    vt.preview_play.click()
    assert vt.preview_state() == "idle" and vt.preview_play.text() == "Play"
    monkeypatch.setattr(ip, "run_bg", inline)
    vt.use_toggle.click()
    assert app.cfg["interpreter_voice_on"] is False
    assert p.speak_row.sub.text() == "Standard voice"
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: False)
    vt.delete_button.click()
    assert "delete" not in app.voices.calls
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: True)
    vt.delete_button.click()
    assert "delete" in app.voices.calls and vt.state() == "create"
    assert _tracked(app, "voice_deleted") == [{}]


def test_every_language_has_its_own_sentence(ip):
    from ui import interpreter_host as ih
    assert [c for c in ih.LANG_CODES if not ip.SAMPLES.get(c)] == []
    assert len(set(ip.SAMPLES.values())) == len(ip.SAMPLES)                # no copy-paste slips


def test_every_language_has_its_own_example_lines(ip):
    from ui import interpreter_host as ih
    assert [c for c in ih.LANG_CODES if c not in ip.LINES] == []            # never English bubbles for it
    assert len(set(ip.LINES.values())) == len(ip.LINES)


def test_every_language_has_its_own_texts_to_read(ip):
    from interpreter import voice_builder as vb
    from ui import interpreter_host as ih
    assert [c for c in ih.LANG_CODES if c not in vb.READ_TEXTS] == []
    texts = [t for c in vb.READ_TEXTS for t in vb.READ_TEXTS[c]]
    assert len(set(texts)) == len(texts)                                    # no copy-paste slips
    for c in ih.LANG_CODES:
        # ~20 s each read aloud (about a minute with the consent words): several times the
        # "Hear yourself in" sentence in any script, never one short sentence
        assert min(len(t) for t in vb.read_texts(c)) >= 3.5 * len(ip.SAMPLES[c]), c


def test_a_take_can_run_to_a_minute(page, ip, monkeypatch):
    asked = []
    monkeypatch.setattr(ip.interpreter_audio, "record_mic", lambda **k: asked.append(k.get("max_seconds")) or b"")
    p, _app = page("pro")
    vt = p.voice_tab
    vt.opt_record.radio.setChecked(True)
    vt.consent.setChecked(True)
    vt.create_button.click()
    vt.rec_button.click()
    assert asked == [60.0]                              # a slow reader of a longer text is never cut off


def test_how_it_is_used_names_this_page(ip):
    assert ip.HOW_USED.endswith("Delete it any time on this page.") and "tab" not in ip.HOW_USED


class _FakeStream:
    def __init__(self, frames):
        self.frames = frames                       # float32 frames handed out per poll (0 = a silent mic)
        self.closed = False

    def get_read_available(self):
        return self.frames

    def read(self, n, exception_on_overflow=False):
        import numpy as np
        return (np.ones(n, np.float32) * 0.2).tobytes()

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class _FakePA:
    paFloat32 = 1

    def __init__(self, frames):
        self.stream = _FakeStream(frames)
        self.opened = []

    def PyAudio(self):
        return self

    def get_default_input_device_info(self):
        return {"index": 1}

    def get_device_info_by_index(self, i):
        return {"maxInputChannels": 2, "defaultSampleRate": 44100}

    def open(self, **kw):
        self.opened.append(kw)
        return self.stream

    def terminate(self):
        pass


def test_recording_never_hangs_and_stop_works_at_once(monkeypatch):
    import threading
    import time as _time
    from interpreter import audio_io
    from ui import interpreter_audio as ia
    fake = _FakePA(frames=320)
    monkeypatch.setattr(audio_io, "_pyaudio", lambda: fake)
    stop = threading.Event()
    levels = []
    threading.Timer(0.3, stop.set).start()
    t0 = _time.monotonic()
    wav = ia.record_mic(stop=stop, on_level=levels.append, cfg={"input_device_index": "smart_meeting"})
    assert _time.monotonic() - t0 < 1.5 and len(wav) > 44 and levels and fake.stream.closed
    # the bars show the voice: 0.2 of full scale is RMS 6553, a full bar (never wrapped int16 squares)
    assert all(lv == pytest.approx(1.0) for lv in levels)
    # opened like the dictation recorder: float32, mono, 16 kHz, the default mic ("smart_meeting" is no device)
    assert fake.opened[0]["channels"] == 1 and fake.opened[0]["rate"] == 16000 and fake.opened[0]["input_device_index"] == 1
    # a microphone that sends nothing: an error after NO_SOUND_S, never a hang
    silent = _FakePA(frames=0)
    monkeypatch.setattr(audio_io, "_pyaudio", lambda: silent)
    monkeypatch.setattr(ia, "NO_SOUND_S", 0.2)
    t0 = _time.monotonic()
    with pytest.raises(ia.MicSilent):
        ia.record_mic(max_seconds=30)
    assert _time.monotonic() - t0 < 1.5 and silent.stream.closed


class _ScriptStream:
    """Hands out the given float32 pieces, one per poll, then nothing."""

    def __init__(self, pieces):
        self.pieces = list(pieces)
        self.closed = False

    def get_read_available(self):
        return len(self.pieces[0]) if self.pieces else 0

    def read(self, n, exception_on_overflow=False):
        return self.pieces.pop(0).tobytes()

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


def _mic(monkeypatch, *parts):
    """The default microphone sends ``parts`` (float32 arrays at 16 kHz), in 50 ms pieces."""
    import numpy as np
    from interpreter import audio_io
    a = np.concatenate(parts).astype(np.float32)
    pa = _FakePA(frames=0)
    pa.stream = _ScriptStream([a[i:i + 800] for i in range(0, len(a), 800)])
    monkeypatch.setattr(audio_io, "_pyaudio", lambda: pa)
    return pa


def _room(seconds, seed=3):
    import numpy as np
    return (np.random.default_rng(seed).standard_normal(int(seconds * 16000)) * 0.002).astype(np.float32)


def _voice(seconds, amp=0.15):
    import numpy as np
    return (np.sin(np.arange(int(seconds * 16000)) * 2 * np.pi * 180 / 16000) * amp).astype(np.float32)


def test_a_muted_microphone_says_so(monkeypatch):
    import numpy as np
    from ui import interpreter_audio as ia
    # frames that are all zeros (muted, blocked by Windows, an idle virtual cable): said, never sent on
    _mic(monkeypatch, np.zeros(4 * 16000, np.float32))
    with pytest.raises(ia.MicSilent):
        ia.record_mic(seconds=0.3)
    # Record now (up to a minute): it says so while still recording, not after the whole take
    pa = _mic(monkeypatch, np.zeros(30 * 16000, np.float32))
    with pytest.raises(ia.MicSilent):
        ia.record_mic(max_seconds=60)
    assert len(pa.stream.pieces) > 400                       # stopped after ~6 s of zeros, not 30
    # a quiet but working microphone is never refused; a short take is the page's to judge
    _mic(monkeypatch, _room(4.0))
    assert len(ia.record_mic(seconds=0.3)) > 44
    _mic(monkeypatch, np.zeros(2 * 16000, np.float32))
    assert len(ia.record_mic(seconds=0.3)) > 44


def test_try_it_ends_soon_after_the_speech(monkeypatch):
    from ui import interpreter_audio as ia

    def take(*parts):
        _mic(monkeypatch, *parts)
        levels = []
        wav = ia.record_mic(seconds=ia.TRY_SECONDS, max_seconds=ia.TRY_MAX_S, until_quiet=ia.TRY_QUIET_S,
                            on_level=levels.append)
        return (len(wav) - 44) / 32000.0, levels

    # "Hello": 0.4 s of the room, 0.8 s of speech; it ends ~0.7 s after the speech, not at 4 s
    secs, levels = take(_room(0.4), _voice(0.8), _room(6.0))
    assert 1.8 <= secs <= 2.05
    assert max(levels) == pytest.approx(0.15 * 32767 / 2 ** 0.5 / 6000, abs=0.02)     # the bars show it
    # nothing said: it ends at TRY_SECONDS, as before
    secs, _ = take(_room(10.0))
    assert secs == pytest.approx(ia.TRY_SECONDS, abs=0.06)
    # a long sentence with short pauses runs on, up to TRY_MAX_S
    secs, _ = take(_room(0.4), *([_voice(0.9), _room(0.3)] * 10))
    assert secs == pytest.approx(ia.TRY_MAX_S, abs=0.06)


def test_try_it_sends_the_dictionary_only_when_that_is_allowed(monkeypatch):
    # F12: like a live run, Try it keeps the Dictionary words on the PC when "Use my words with
    # cloud speech" is off (or Privacy mode is on)
    from types import SimpleNamespace
    import cloud_speech
    from interpreter import translate
    from ui import interpreter_audio as ia
    sent = []
    monkeypatch.setattr(ia, "record_mic", lambda **k: b"RIFF" + bytes(40))
    monkeypatch.setattr(cloud_speech, "spec_from_cfg", lambda *a, **k: object())
    monkeypatch.setattr(cloud_speech, "send", lambda spec, wav: ("Hello Anna", "en"))
    monkeypatch.setattr(translate, "translate_phrase",
                        lambda *a, glossary=(), **k: sent.append(list(glossary)) or "Բարև Աննա")
    words = ["Anna Petrosyan"]
    for cfg, want in (({"vocabulary": words}, words),
                      ({"vocabulary": words, "vocabulary_share_with_cloud": False}, []),
                      ({"vocabulary": words, "privacy_mode": True}, [])):
        app = SimpleNamespace(cfg=cfg, auth=SimpleNamespace(get_access_token=lambda: "jwt"))
        assert ia.try_it(app, "en", "hy")["heard"] == "Բարև Աննա"
        assert sent[-1] == want


def test_record_now_with_a_silent_microphone_says_so(page, ip, monkeypatch):
    from ui import interpreter_audio as ia

    def silent(**k):
        raise ia.MicSilent("no sound")

    monkeypatch.setattr(ip.interpreter_audio, "record_mic", silent)
    p, app = page("pro")
    vt = p.voice_tab
    vt.opt_record.radio.setChecked(True)
    vt.consent.setChecked(True)
    vt.create_button.click()
    vt.rec_button.click()
    assert vt.state() == "record" and vt.rec_mode() == "ready"
    assert vt.rec_note.text() == "Your microphone isn't sending any sound. Check it in Settings › Microphone."


def test_create_my_voice_for_free_opens_the_pro_window(page):
    p, app = page("free")
    p.set_tab(1)
    vt = p.voice_tab
    # signed in: the server is asked (a voice can always be seen and deleted) - none here
    assert vt.state() == "create" and app.voices.calls == ["get"]
    vt.consent.setChecked(True)
    vt.create_button.click()
    assert _calls(app, "_pro_upsell") == [("_pro_upsell", "Interpreter")]
    assert not [c for c in app.voices.calls if c[0] == "create"]
    p, app = page("guest")
    assert p.voice_tab.state() == "create" and app.voices.calls == []       # no account: no server call


def test_a_voice_stays_seen_and_deletable_when_pro_ends(page, monkeypatch):
    from ui.components import mini_prompt
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: True)
    p, app = page("free", voice=dict(VOICE))            # cloned while Pro; the plan has ended since
    vt = p.voice_tab
    assert vt.state() == "ready" and vt.delete_button.isVisibleTo(vt)
    assert not [c for c in app.voices.calls if c[0] == "preview"]           # nothing paid is fetched
    vt.preview_play.click()
    vt.again_button.click()
    assert len(_calls(app, "_pro_upsell")) == 2 and vt.state() == "ready"  # Play / Make it again: Pro
    vt.delete_button.click()
    assert "delete" in app.voices.calls and vt.state() == "create"
    # Pro ending while the page is open keeps the card (and its Delete) too
    p, app = page("pro", voice=dict(VOICE))
    app.set_tier("free")
    p.refresh_account()
    assert p.voice_tab.state() == "ready" and app.voices.calls.count("get") == 2


def test_a_voice_read_from_before_sign_out_is_dropped(page, ip, monkeypatch):
    held = Hold()
    monkeypatch.setattr(ip, "run_bg", held)
    p, app = page("pro", voice=dict(VOICE))             # the voice read is on its way ...
    app.set_tier("guest")                               # ... and the person signs out meanwhile
    p.refresh_account()
    held.run()                                          # the old account's answer arrives late
    assert p.voice_tab.state() == "create" and p.voice_tab.voice is None
    assert p.speak_row.sub.text() == "Standard voice"


def test_the_meetings_are_looked_at_only_when_a_voice_can_be_made(page, ip, monkeypatch):
    scans = []
    monkeypatch.setattr(ip.voice_builder, "find_own_voice", lambda **k: scans.append(1) or _found(372, 12))
    held = Hold()
    monkeypatch.setattr(ip, "run_bg", held)
    p, _app = page("pro", voice=dict(VOICE))            # a voice already: nothing to look for
    while held.jobs:
        held.run()
    assert scans == [] and p.voice_tab.state() == "ready"
    monkeypatch.setattr(ip, "run_bg", inline_run)
    page("free")                                        # can't clone (the Pro window): no scan either
    page("guest")
    assert scans == []
    p, _app = page("pro")                               # no voice: looked through once
    assert scans == [1] and p.voice_tab.opt_meetings.chip.text() == "Enough"


def test_leaving_during_the_meetings_scan_looks_again(page, ip, monkeypatch):
    full = _found(372, 12)
    monkeypatch.setattr(ip.voice_builder, "find_own_voice",
                        lambda should_stop=lambda: False, **k: _found(30, 1) if should_stop() else full)
    held = Hold()
    monkeypatch.setattr(ip, "run_bg", held)
    p, _app = page("pro")
    vt = p.voice_tab
    held.run()                                          # no voice: the meetings scan starts
    assert vt._scanning and len(held.jobs) == 1
    p.parentWidget().hide()                             # another page, or minimized, mid-scan
    held.run()                                          # it stops with only what it read so far
    assert vt.found is None and not vt._scanning        # ... and that is not kept for the session
    p.parentWidget().show()                             # back: it looks again
    assert len(held.jobs) == 1
    held.run()
    assert vt.found is full and vt.opt_meetings.chip.text() == "Enough" and vt.opt_meetings.radio.isChecked()


def test_leaving_the_page_stops_record_now(page, ip, monkeypatch):
    from interpreter import voice_builder
    p, _app = page("pro")
    monkeypatch.setattr(ip, "run_bg", lambda fn, then=None, error=None, name="": None)    # the take never ends
    vt = p.voice_tab
    vt.opt_record.radio.setChecked(True)
    vt.consent.setChecked(True)
    vt.create_button.click()
    vt.rec_button.click()
    stop, gen = vt._rec_stop, vt._rec_gen
    assert vt.rec_mode() == "recording"
    p.parentWidget().hide()                             # the mic is never kept open off the page
    assert stop.is_set() and vt.rec_mode() == "ready" and not vt._rec_timer.isActive()
    vt._take_done(gen, voice_builder.to_wav_bytes([3000] * 16000 * 8))      # that take, late: dropped
    assert vt.rec_mode() == "ready" and vt._take is None


def test_make_it_again_can_go_back_to_the_voice(page, ip, monkeypatch):
    p, _app = page("pro", voice=dict(VOICE))
    vt = p.voice_tab
    vt.again_button.click()
    assert vt.state() == "create" and vt.keep_button.isVisibleTo(vt) and vt.keep_button.text() == "Keep my voice"
    vt.keep_button.click()
    assert vt.state() == "ready" and vt.voice["voice_id"] == "v1"
    # Make it again, Record now, Cancel: back to the voice (it still exists), not "Clone your voice"
    monkeypatch.setattr(ip, "run_bg", lambda fn, then=None, error=None, name="": None)
    vt.again_button.click()
    vt.opt_record.radio.setChecked(True)
    vt.consent.setChecked(True)
    vt.create_button.click()
    assert vt.state() == "record"
    vt.rec_cancel.click()
    assert vt.state() == "ready"
    monkeypatch.setattr(ip, "run_bg", inline_run)
    p, _app = page("pro")                               # no voice: nothing to go back to
    assert p.voice_tab.state() == "create" and not p.voice_tab.keep_button.isVisibleTo(p.voice_tab)


def test_a_failed_upload_keeps_the_three_takes(page, ip, monkeypatch):
    from interpreter import voice_builder
    from interpreter.session import InterpreterError
    monkeypatch.setattr(ip.interpreter_audio, "record_mic", lambda **k: voice_builder.to_wav_bytes([3000] * 16000 * 6))
    monkeypatch.setattr(voice_builder, "consent_phrase", lambda rng=None: "blue river seven")
    p, app = page("pro")                                # (the meetings have "Enough" too)
    vt = p.voice_tab
    real_create = app.voices.create
    fails = ["network"]

    def create(clips, source, phrase):
        if fails:
            raise InterpreterError(fails.pop(0))
        return real_create(clips, source, phrase)

    monkeypatch.setattr(app.voices, "create", create)

    def read_all():
        for _ in range(3):
            vt.rec_button.click()
            vt.next_button.click()

    vt.opt_record.radio.setChecked(True)
    vt.consent.setChecked(True)
    vt.create_button.click()
    read_all()
    assert vt.state() == "create" and vt.create_error.text() == "No connection to Gusan Cloud."
    assert vt.opt_record.radio.isChecked()              # the choice stays Record now
    vt.create_button.click()                            # again: the same three takes go, nothing is read again
    assert vt.state() == "ready" and ("create", 3, "recorded", "blue river seven") in app.voices.calls
    # the recordings themselves refused: those are read again
    fails.append("voice_samples_rejected")
    vt.again_button.click()
    vt.opt_record.radio.setChecked(True)
    vt.consent.setChecked(True)
    vt.create_button.click()
    read_all()
    assert vt.state() == "create" and "quiet room" in vt.create_error.text()
    vt.create_button.click()
    assert vt.state() == "record" and vt.step_label.text() == "1 of 3"


def test_ticking_the_box_again_during_the_upload_leaves_clone_on(page, ip, monkeypatch):
    from interpreter.session import InterpreterError
    p, _app = page("pro")
    vt = p.voice_tab
    got = {}
    monkeypatch.setattr(ip, "run_bg", lambda fn, then=None, error=None, name="": got.update(error=error))
    vt.consent.setChecked(True)
    vt.create_button.click()                            # being made ...
    vt.consent.setChecked(False)
    vt.consent.setChecked(True)                         # ... the box unticked and ticked again meanwhile
    got["error"](InterpreterError("network"))
    assert vt.consent.isChecked() and vt.create_button.isEnabled()


def test_a_voice_made_or_deleted_while_it_runs_takes_effect_at_once(page, monkeypatch):
    from ui.components import mini_prompt
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: True)
    FakeCtl.made.clear()
    p, _app = page("pro")
    _toggle(p)
    vt = p.voice_tab
    vt.consent.setChecked(True)
    vt.create_button.click()                            # made during the call: the engine starts again with it
    _spin()
    assert vt.state() == "ready" and p.host.state == "running" and len(FakeCtl.made) == 2
    assert ("stop", "restart") in FakeCtl.made[0].calls
    vt.delete_button.click()                            # deleted: it never speaks in a deleted voice
    _spin()
    assert vt.state() == "create" and p.host.state == "running" and len(FakeCtl.made) == 3


def test_a_failed_delete_does_not_leave_its_error(page, monkeypatch):
    from interpreter.session import InterpreterError
    from ui.components import mini_prompt
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: True)
    p, app = page("pro", voice=dict(VOICE))
    vt = p.voice_tab
    real = app.voices.delete

    def offline():
        raise InterpreterError("network")

    monkeypatch.setattr(app.voices, "delete", offline)
    vt.delete_button.click()
    assert vt.ready_error.text() == "No connection to Gusan Cloud." and vt.ready_error.isVisibleTo(vt)
    monkeypatch.setattr(app.voices, "delete", real)
    vt.delete_button.click()
    assert vt.state() == "create"
    vt.consent.setChecked(True)
    vt.create_button.click()                            # a new voice: its card starts clean
    assert vt.state() == "ready" and not vt.ready_error.isVisibleTo(vt)


def test_play_while_the_first_sound_is_fetched_waits_for_it(page, ip, monkeypatch):
    import threading
    played = []
    monkeypatch.setattr(ip.interpreter_audio, "play_pcm", lambda pcm, rate, stop=None: played.append(len(pcm)))
    p, app = page("pro", voice=dict(VOICE))
    vt = p.voice_tab
    vt._preview_audio.clear()
    app.voices.calls.clear()
    held = Hold()
    monkeypatch.setattr(ip, "run_bg", held)
    vt._prefetch_preview()                              # the card's first sound: on its way (slow the first time)
    vt.preview_play.click()                             # Play pressed meanwhile
    assert vt.preview_state() == "loading" and len(held.jobs) == 2
    fetch, play = held.jobs
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", play[0]()))
    t.start()
    t.join(0.3)
    assert t.is_alive()                                 # it waits for that sound, it doesn't ask again
    inline_run(*fetch)                                  # the first sound arrives
    t.join(2)
    play[1](out["r"])
    assert app.voices.calls == [("preview", "ja")] and played == [320] and vt.preview_state() == "idle"


def test_picking_a_language_while_one_plays_plays_that_one(page, ip, monkeypatch):
    p, _app = page("pro", voice=dict(VOICE))
    vt = p.voice_tab
    monkeypatch.setattr(ip, "run_bg", Hold())
    vt._preview_audio.clear()
    vt.preview_play.click()                             # Japanese: loading
    first = vt._play_stop
    vt.preview_combo.setCurrentIndex(vt.preview_combo.findData("fr"))
    vt.preview_combo.activated.emit(vt.preview_combo.currentIndex())
    assert first.is_set()                               # Japanese stops ...
    assert vt.preview_state() == "loading" and vt._play_stop not in (None, first)       # ... French starts


def test_try_it_for_free_opens_the_pro_window_and_for_pro_shows_the_result(page, ip, monkeypatch):
    p, app = page("free")
    p.picture.try_button.click()
    assert _calls(app, "_pro_upsell") == [("_pro_upsell", "Interpreter")]
    p, app = page("pro")
    monkeypatch.setattr(ip.interpreter_audio, "try_it",
                        lambda a, my, their, **k: {"said": "Hello there", "heard": "こんにちは", "voice": True})
    p.picture.try_button.click()
    assert p.picture.mine[0].text() == "Hello there" and p.picture.theirs[0].text() == "こんにちは"
    assert p.picture.try_button.text() == "Try it"


def test_try_it_says_how_it_went_on_this_page_only(page, ip, monkeypatch):
    from ui import interpreter_audio as ia
    p, app = page("pro")
    results = [{"said": "", "heard": "", "voice": False},
               {"said": "Hello there", "heard": "こんにちは", "voice": False}]
    monkeypatch.setattr(ip.interpreter_audio, "try_it", lambda *a, **k: results.pop(0))
    line = p.picture.hint.text()
    p.picture.try_button.click()                                    # nothing heard
    assert p.picture.hint.text() == "Didn't catch that. Try it again."
    p.picture.try_button.click()                                    # a good try: that line goes
    assert p.picture.hint.text() == line and p.picture.mine[0].text() == "Hello there"

    def muted(*a, **k):
        raise ia.MicSilent("zeros")

    monkeypatch.setattr(ip.interpreter_audio, "try_it", muted)
    p.picture.try_button.click()
    assert p.picture.hint.text() == "Your microphone isn't sending any sound. Check it in Settings › Microphone."
    # never the interpreter's own notice (the page's top line, the live card, the tray)
    assert app.interpreter.last_notice == ("", "") and not p.notice_box.isVisibleTo(p)
    assert not _calls(app, "tray_hint")


def test_try_it_pressed_again_lets_that_run_go(page, ip, monkeypatch):
    p, _app = page("pro")
    held = Hold()
    monkeypatch.setattr(ip, "run_bg", held)
    btn = p.picture.try_button
    btn.click()
    assert btn.text() == "Listening…"
    btn.click()                                         # pressed again: the button is free at once
    assert btn.text() == "Try it" and not p._try_busy
    btn.click()                                         # ... and a new try starts right away
    assert btn.text() == "Listening…" and len(held.jobs) == 2
    monkeypatch.setattr(ip.interpreter_audio, "try_it", lambda *a, **k: {"said": "Old", "heard": "Hin", "voice": True})
    held.run(0)                                         # the first one, late: nothing of it shows
    assert p.picture.mine[0].text() != "Old" and btn.text() == "Listening…"
    monkeypatch.setattr(ip.interpreter_audio, "try_it", lambda *a, **k: {"said": "New", "heard": "Nor", "voice": True})
    held.run(0)
    assert p.picture.mine[0].text() == "New" and btn.text() == "Try it"
    btn.click()                                         # leaving the page lets a try go too
    p.parentWidget().hide()
    assert btn.text() == "Try it" and not p._try_busy

    def offline(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(ip.interpreter_audio, "try_it", offline)
    held.run(0)                                         # it fails afterwards: nothing is said about it
    assert p.picture.hint.text() != ip.TRY_FAILED


def test_try_it_cancelled_pays_for_nothing_more(monkeypatch):
    import sys
    import threading
    from interpreter import translate
    from ui import interpreter_audio as ia
    stop = threading.Event()
    calls = []
    monkeypatch.setattr(ia, "record_mic", lambda **k: calls.append("record") or b"RIFF" + b"\0" * 40)

    def stt(spec, wav):
        calls.append("stt")
        stop.set()                                      # pressed again while it says "Translating…"
        return "Hello there", "en"

    monkeypatch.setitem(sys.modules, "cloud_speech", SimpleNamespace(spec_from_cfg=lambda *a, **k: None, send=stt))
    monkeypatch.setattr(translate, "translate_phrase", lambda *a, **k: calls.append("translate") or "Barev")
    played = []
    monkeypatch.setattr(ia, "play_pcm", lambda *a, **k: played.append(1))
    voices = FakeVoices(dict(VOICE))
    app = SimpleNamespace(cfg={}, auth=None)
    assert ia.try_it(app, "en", "hy", stop=stop, voices=voices) == {"said": "", "heard": "", "voice": False}
    assert calls == ["record", "stt"] and voices.calls == [] and played == []      # no translation, no voice
    stop.clear()
    calls.clear()
    assert ia.try_it(app, "en", "hy", stop=threading.Event(), voices=voices)["voice"] is True
    assert calls == ["record", "stt", "translate"] and voices.calls == [("preview", "hy")] and played == [1]


def test_try_it_plays_back_only_in_my_own_voice(page, ip, monkeypatch):
    got = []
    monkeypatch.setattr(ip.interpreter_audio, "try_it",
                        lambda a, my, their, **k: got.append(k.get("voices")) or {"said": "Hi", "heard": "Barev"})
    p, app = page("pro")                                # no voice yet: the words only, and how to hear them
    assert p.picture.hint.text() == "Say one sentence, see it in Japanese. Clone your voice to hear it."
    p.picture.try_button.click()
    assert got == [None]                                # (no server call that can only answer "no voice")
    p, app = page("pro", voice=dict(VOICE))
    assert p.picture.hint.text() == "Say one sentence, hear it back in Japanese."
    p.picture.try_button.click()
    assert got[-1] is app.voices
    p.voice_tab.use_toggle.click()                      # my voice off: calls use a standard voice
    assert p.picture.hint.text() == "Say one sentence, see it in Japanese."
    p.picture.try_button.click()
    assert got[-1] is None


# ── the host ──
def test_the_meetings_hook_starts_and_stops_with_the_recording(qapp):
    app = make_app("pro", interpreter_with_meetings=True)
    h = app.interpreter
    h._on_meeting_phase("recording")
    assert h.state == "running" and h._source == "meeting"
    h._on_meeting_phase("paused")
    assert h.state == "running"
    h._on_meeting_phase("processing")
    assert h.state == "idle"
    app.cfg["interpreter_with_meetings"] = False
    h._on_meeting_phase("recording")
    assert h.state == "idle"


def test_the_meetings_hook_is_quiet_for_free(qapp):
    app = make_app("free", interpreter_with_meetings=True)
    app.interpreter._on_meeting_phase("recording")
    assert app.interpreter.state == "idle" and not _calls(app, "_pro_upsell")


def test_events_reach_the_surfaces_on_the_gui_thread(qapp):
    import threading
    app = make_app("pro")
    h = app.interpreter
    h.toggle()
    got = []
    h.event.connect(lambda e: got.append((e.get("type"), threading.current_thread() is threading.main_thread())))
    ctl = h.controller
    t = threading.Thread(target=lambda: ctl.on_event({"type": "live", "direction": "hear", "text": "hi"}))
    t.start()
    t.join()
    end = time.monotonic() + 2
    while not got and time.monotonic() < end:
        QApplication.processEvents()
    assert got == [("live", True)]


def test_pair_helpers():
    from ui import interpreter_host as ih
    assert ih.my_language({"language": "auto"}) == "en"
    assert ih.my_language({"language": "hy"}) == "hy"
    assert ih.my_language({"language": "hy", "interpreter_my_language": "fr"}) == "fr"
    assert ih.pair_codes({"language": "en", "interpreter_their_language": "ja"}) == "EN ⇄ JA"
    assert ih.pair_names({"language": "en", "interpreter_their_language": "ja"}) == "English ⇄ Japanese"
    # nothing picked yet: English ⇄ Armenian (English ⇄ ... when I speak Armenian myself: Armenian ⇄ English)
    assert ih.pair_names({"language": "en"}) == "English ⇄ Armenian"
    assert ih.pair_codes({"language": "hy"}) == "HY ⇄ EN"
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert '"interpreter_their_language": "hy",' in src
    assert ih.minutes_text(2880) == "48 min" and ih.minutes_text(18000) == "5 h"
    from ui import interpreter_host as ih
    assert ih.message("no_virtual_mic") == ih.NO_VIRTUAL_MIC


# ── the live card ──
@pytest.fixture
def card(qapp):
    from unittest import mock
    from ui import glass, live_assist
    made = []
    patches = [mock.patch.object(glass, "capture_exclusion_supported", return_value=True),
               mock.patch.object(glass, "is_remote_session", return_value=False),
               mock.patch.object(glass, "exclude_from_capture", return_value=True),
               mock.patch.object(glass, "is_excluded_from_capture", return_value=True)]
    for pt in patches:
        pt.start()

    def build(tier="pro", **cfg):
        app = make_app(tier, **cfg)
        app.schedule_config_save = lambda: None
        app.save_config = lambda: None
        c = live_assist.LiveAssistOverlay(main_app=app)
        made.append(c)
        return c, app

    yield build
    for c in made:
        c.hide()
        c.deleteLater()
    QApplication.processEvents()
    for pt in patches:
        pt.stop()


EVENTS = [
    {"type": "phrase", "direction": "hear", "id": 101, "text": "明日の会議は3時からです。"},
    {"type": "translation", "direction": "hear", "id": 101, "text": "Tomorrow's meeting", "final": False},
    {"type": "translation", "direction": "hear", "id": 101, "text": "Tomorrow's meeting starts at three.",
     "final": True},
    {"type": "phrase", "direction": "speak", "id": 102, "text": "Can we keep it to 30 minutes?"},
    {"type": "translation", "direction": "speak", "id": 102, "text": "30分で終われますか？", "final": True},
    {"type": "live", "direction": "hear", "text": "はい、大丈夫"},
]


def test_the_card_chip_turns_it_on_and_shows_the_directions_and_captions(card):
    c, app = card("pro")
    c.set_meeting_active(True, "Call with Anna", "Aram Adamyan, Anna Sato")
    assert not c.btn_interp.is_on() and c.btn_interp.text() == ""
    assert c.btn_interp.accessibleName() == "Turn the interpreter on"
    assert not c.interp_row.isVisibleTo(c) and c.lbl_sum_head.text() == "LIVE TRANSCRIPT"
    c.btn_interp.click()
    assert app.interpreter.state == "running"
    assert c.btn_interp.is_on() and c.btn_interp.text() == "" and "EN ⇄ JA" in c.btn_interp.accessibleName()
    assert c.interp_row.isVisibleTo(c)
    assert c.interp_row.my_picker.code() == "en" and c.interp_row.my_picker.text() == "English"
    assert c.interp_row.their_picker.code() == "ja" and c.interp_row.their_picker.text() == "Japanese"
    assert c.interp_row.them_label.text() == "Anna"
    assert c.interp_row.speak_switch.accessibleName() == "They hear me in Japanese: on"
    assert c.interp_row.hear_switch.accessibleName() == "I hear them in English: on"
    assert c.interp_row.speak_switch.isChecked() and c.interp_row.hear_switch.isChecked()
    assert c.lbl_sum_head.text() == "CAPTIONS"
    for e in EVENTS:
        app.interpreter.event.emit(e)
    c._render_captions()
    text = c.txt_summary.toPlainText()
    assert "Tomorrow's meeting starts at three." in text and "明日の会議は3時からです。" in text
    assert "30分で終われますか？" in text and "YOU" in text and "ANNA" in text
    assert "はい、大丈夫" in text                                      # the live line
    c.feed_transcript("words of the Live Assistant session", answer=False)
    assert "Tomorrow's meeting" in c.txt_summary.toPlainText()         # captions keep the box
    c._on_tick()
    assert "1.8 s" in c.interp_row.delay.text()
    c.interp_row.speak_switch.click()
    assert app.cfg["interpreter_speak"] is False and not c.interp_row.speak_switch.isChecked()
    c.btn_interp.click()
    assert app.interpreter.state == "idle" and not c.btn_interp.is_on()
    assert not c.interp_row.isVisibleTo(c) and c.lbl_sum_head.text() == "LIVE TRANSCRIPT"
    assert "words of the Live Assistant session" in c.txt_summary.toPlainText()


def test_the_hidden_chip_is_icon_only_while_the_interpreter_runs(card):
    c, app = card("pro")
    c.set_meeting_active(True, "Call with Anna", "Aram Adamyan, Anna Sato")
    word = c.btn_private.text()
    assert word                                                       # "Hidden" / "Visible"
    c.btn_interp.click()
    assert c.btn_private.text() == "" and c.btn_private.accessibleName()  # icon only, still named
    c.btn_interp.click()
    assert c.btn_private.text() == word                               # the word is back


def test_the_card_chip_for_free_opens_the_pro_window(card):
    c, app = card("free")
    c.btn_interp.click()
    assert _calls(app, "_pro_upsell") == [("_pro_upsell", "Interpreter")]
    assert app.interpreter.state == "idle" and not c.btn_interp.is_on()


def test_the_card_works_without_a_live_assistant_session(card):
    c, app = card("pro")
    app.interpreter.toggle(source="hotkey")
    assert c.btn_interp.is_on() and c.interp_row.isVisibleTo(c) and c.lbl_sum_head.text() == "CAPTIONS"
    assert c.interp_row.them_label.text() == "Them"
    assert c.interp_row.hear_switch.accessibleName() == "I hear them in English: on"
    app.interpreter.event.emit(EVENTS[0])
    app.interpreter.event.emit(EVENTS[2])
    c._render_captions()
    assert "Tomorrow's meeting starts at three." in c.txt_summary.toPlainText()


def test_the_card_captions_can_be_off(card):
    c, app = card("pro", interpreter_captions=False)
    app.interpreter.toggle()
    assert c.interp_row.isVisibleTo(c) and c.lbl_sum_head.text() == "LIVE TRANSCRIPT"


def test_the_card_says_when_speak_needs_a_virtual_microphone(card):
    c, app = card("pro")
    app.interpreter.say("no_virtual_mic")
    from ui import interpreter_host as ih
    assert c.lbl_status.text() == ih.NO_VIRTUAL_MIC
    assert c.btn_interp_setup.isVisibleTo(c)
    c.btn_interp_setup.click()
    assert ("show_main", "interpreter", ("focus", "microphone")) in app.calls


def test_the_card_keeps_its_window_flags(card):
    from PySide6.QtCore import Qt
    c, app = card("pro")
    flags = c.windowFlags()
    app.interpreter.toggle()
    assert c.windowFlags() == flags
    assert flags & Qt.WindowStaysOnTopHint and flags & Qt.Tool and flags & Qt.FramelessWindowHint


def test_caption_log_cuts_the_live_line():
    from ui.live_interp import CaptionLog
    log = CaptionLog()
    log.event({"type": "live", "direction": "hear", "text": "Hello there, how are"})
    log.event({"type": "phrase", "direction": "hear", "id": 1, "text": "Hello there,"})
    assert log.live_text("hear") == "how are"
    log.event({"type": "live", "direction": "hear", "text": "Hello there, how are you"})
    assert log.live_text("hear") == "how are you"
    log.event({"type": "phrase", "direction": "hear", "id": 2, "text": "how are you doing?"})      # committed
    assert log.live_text("hear") == ""
    log.event({"type": "live", "direction": "hear", "text": "Next"})
    assert log.live_text("hear") == "Next"
    log.event({"type": "translation", "id": 2, "text": "Wie geht's?", "final": True})
    assert log.items[-1]["dst"] == "Wie geht's?" and log.items[-1]["final"]
    html_text = log.html({"text": "#000", "muted": "#555", "faint": "#999", "mine": "#43c", "accent": "#00f"},
                         them="Anna <b>")
    assert "ANNA &lt;B&gt;" in html_text                   # names and words are escaped, never markup


# ── Meetings and Live Assistant rows ──
def test_the_meetings_start_card_has_the_interpreter_row(qapp, monkeypatch):
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    app = make_app("pro")
    p = page_mod.create(app)
    try:
        app._sig.changed.connect(p.on_setting_changed)
        card = p.list_view.start_card
        langs = card.interp_langs
        assert langs.languages() == ("en", "ja")
        assert (langs.my_picker.text(), langs.their_picker.text()) == ("English", "Japanese")
        assert not card.interp_toggle.isChecked()
        card.interp_toggle.click()
        assert app.cfg["interpreter_with_meetings"] is True and card.interp_toggle.isChecked()
        # Their language, picked on the card itself: search, Enter.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        langs.their_picker.open_popup()
        QTest.keyClicks(langs.their_picker.popup.search, "french")
        QTest.keyClick(langs.their_picker.popup.search, Qt.Key_Return)
        assert not langs.their_picker.popup.isVisible()
        assert app.cfg["interpreter_their_language"] == "fr" and langs.their_picker.text() == "French"
        assert app.cfg["interpreter_my_language"] == ""              # only what changed is written
        assert not any(c[0] == "show_main" for c in app.calls)       # it never leaves the page
        # Picking theirs on my side swaps the two; the swap button swaps them back.
        langs.my_picker.popup.chosen.emit("fr")
        assert (app.cfg["interpreter_my_language"], app.cfg["interpreter_their_language"]) == ("fr", "en")
        assert langs.languages() == ("fr", "en")
        langs.swap_button.click()
        assert (app.cfg["interpreter_my_language"], app.cfg["interpreter_their_language"]) == ("en", "fr")
        # A change made elsewhere shows here; a refused write puts the pickers back.
        app.set_setting("interpreter_their_language", "de")
        assert langs.their_picker.text() == "German"
        app.set_settings = lambda updates: {k: False for k in updates}
        langs.their_picker.popup.chosen.emit("es")
        assert app.cfg["interpreter_their_language"] == "de" and langs.their_picker.code() == "de"
    finally:
        p.deleteLater()
    app = make_app("free")
    p = page_mod.create(app)
    try:
        card = p.list_view.start_card
        card.interp_toggle.click()
        assert _calls(app, "_pro_upsell") == [("_pro_upsell", "Interpreter")]
        assert not card.interp_toggle.isChecked() and "interpreter_with_meetings" not in app.cfg
    finally:
        p.deleteLater()


def test_the_language_pair_shows_short_codes_when_names_do_not_fit(qapp):
    """A narrow window: both pickers show "EN ⇄ HY" (never "Eng…" or one name and one code); wide
    enough again: both names. The accessible names keep the whole language."""
    from ui.pages.interpreter import LangPairRow
    host = QWidget()
    row = LangPairRow(host)
    host.show()                                                # a hidden widget's resize waits for show

    def size_to(width):
        row.resize(width, 32)
        for _ in range(5):                                     # the row's layout places the pickers
            QApplication.processEvents()

    try:
        row.set_languages("en", "hy")
        size_to(row._full_width() + 20)
        assert not row.my_picker.is_compact() and not row.their_picker.is_compact()
        assert (row.my_picker.shown_text(), row.their_picker.shown_text()) == ("English", "Armenian")
        size_to(row._full_width() - 30)
        assert row.my_picker.is_compact() and row.their_picker.is_compact()
        assert (row.my_picker.shown_text(), row.their_picker.shown_text()) == ("EN", "HY")
        assert row.my_picker.accessibleName() == "I speak: English"
        assert row.their_picker.accessibleName() == "They speak: Armenian"
        row.set_languages("en", "ja")
        size_to(row._full_width())                             # just wide enough: the names again
        assert (row.my_picker.shown_text(), row.their_picker.shown_text()) == ("English", "Japanese")
        # One picker on its own (the live card): the code when its name doesn't fit.
        p = row.their_picker
        p.set_compact(False)
        assert p.shown_text(room=10) == "JA" and p.shown_text(room=500) == "Japanese"
    finally:
        host.deleteLater()


def test_the_live_assistant_rail_has_the_interpreter_row(qapp, monkeypatch):
    import live_context
    from ui.pages import live_assistant
    monkeypatch.setattr(live_assistant, "load_sessions", lambda: [])
    real = live_context.hide_status
    monkeypatch.setattr(live_context, "hide_status",
                        lambda wanted, **kw: real(wanted, **dict(dict(supported=True, remote=False, mac=False), **kw)))
    app = make_app("pro")
    app.live_assist = None
    p = live_assistant.create(app)
    try:
        app._sig.changed.connect(p.on_setting_changed)
        assert p.interp_row.label.text() == "Interpreter"
        assert p.interp_row in p.rail.rows()
        langs = p.interp_langs
        assert langs.languages() == ("en", "ja")
        assert (langs.my_picker.text(), langs.their_picker.text()) == ("English", "Japanese")
        # no voice known yet (no run, no Interpreter page): never "in my voice" before one is
        assert p.interp_hint.text() == "They hear me · I hear them"
        assert not p.interp_toggle.isChecked()
        p.interp_toggle.click()
        assert app.interpreter.state == "running" and p.interp_toggle.isChecked()
        assert _tracked(app, "interpreter_started")[0]["source"] == "live_assistant"
        # Languages picked on the row while it runs: it starts again with them, still on.
        made = len(FakeCtl.made)
        langs.their_picker.popup.chosen.emit("fr")
        for _ in range(10):
            QApplication.processEvents()
        assert app.cfg["interpreter_their_language"] == "fr" and app.cfg["interpreter_my_language"] == ""
        assert app.interpreter.state == "running" and p.interp_toggle.isChecked()
        assert len(FakeCtl.made) == made + 1 and FakeCtl.made[-1].settings.their_lang == "fr"
        langs.swap_button.click()
        for _ in range(10):
            QApplication.processEvents()
        assert (app.cfg["interpreter_my_language"], app.cfg["interpreter_their_language"]) == ("fr", "en")
        assert len(FakeCtl.made) == made + 2                         # a swap: one restart for both keys
        assert FakeCtl.made[-1].settings.my_lang == "fr" and FakeCtl.made[-1].settings.their_lang == "en"
        assert not any(c[0] == "show_main" for c in app.calls)
        p.interp_toggle.click()
        assert app.interpreter.state == "idle" and not p.interp_toggle.isChecked()
        app.set_setting("interpreter_hear", False)
        assert p.interp_hint.text() == "They hear me in my voice"
        p.interp_more.click()
        assert ("show_main", "interpreter") in app.calls
    finally:
        p.deleteLater()
    app = make_app("free")
    app.live_assist = None
    p = live_assistant.create(app)
    try:
        p.interp_toggle.click()
        assert _calls(app, "_pro_upsell") == [("_pro_upsell", "Interpreter")]
        assert not p.interp_toggle.isChecked()
    finally:
        p.deleteLater()


# ── the tray and the shortcut ──
def test_the_tray_row_toggles_the_interpreter(qapp):
    from PySide6.QtWidgets import QMenu
    from ui import tray_menu as tm
    app = make_app("pro")
    app.toggle_interpreter = lambda source="tray": app.interpreter.toggle(source=source)
    tr = tm.build_tray_menu(app, QMenu(), custom=True)
    try:
        a = tr.actions["interpreter"]
        assert a.isCheckable() and a.property(tm.P_PRO) and a.property(tm.P_HINT) == "Ctrl+Alt+I"
        a.trigger()
        assert app.interpreter.state == "running" and a.isChecked()
        a.trigger()
        assert app.interpreter.state == "idle" and not a.isChecked()
    finally:
        tr.menu.deleteLater()


def test_the_interpreter_shortcut_never_takes_another_shortcut(qapp):
    import settings_apply
    registered = []
    cfg = {"hotkey": "alt+r", "live_assist_hotkey": "ctrl+alt+a", "smart_actions_hotkey": "alt+s",
           "interpreter_hotkey": "ctrl+alt+i"}
    app = SimpleNamespace(cfg=cfg, schedule_config_save=lambda: None,
                          sig_setting_changed=SimpleNamespace(emit=lambda k: None),
                          _setup_interpreter_hotkey=lambda hk: registered.append(hk) or True,
                          _setup_assist_hotkey=lambda hk: True, _setup_smart_hotkey=lambda hk: True,
                          _setup_hotkey=lambda hk: True)
    for taken in ("alt+r", "Ctrl + Alt + A", "alt+s", "i", "mouse:middle"):
        assert settings_apply.apply(app, {"interpreter_hotkey": taken}) == {"interpreter_hotkey": False}, taken
    assert cfg["interpreter_hotkey"] == "ctrl+alt+i" and registered == []
    assert settings_apply.apply(app, {"interpreter_hotkey": "Ctrl + Alt + J"}) == {"interpreter_hotkey": True}
    assert cfg["interpreter_hotkey"] == "ctrl+alt+j" and registered == ["ctrl+alt+j"]
    # ...and the others can't take the Interpreter's keys.
    for key in ("hotkey", "live_assist_hotkey", "smart_actions_hotkey"):
        assert settings_apply.apply(app, {key: "ctrl+alt+j"}) == {key: False}, key
