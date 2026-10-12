"""Gusan 2.0 Live Assistant card (ui/live_assist.py): the green Hidden chip with
the incognito mark, theme-aware colours, the Gusan / Live Assistant words, and
the public setters + signals that keep the Live Assistant page in sync."""
import sys
from types import SimpleNamespace
from unittest import mock

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QLineEdit, QTextEdit, QWidget


class _App(QObject):
    """The controller bits the card uses: cfg, the debounced save, the
    settings signal (main.AppController.sig_setting_changed) and telemetry."""
    sig_setting_changed = Signal(str)

    def __init__(self, cfg=None):
        super().__init__()
        self.cfg = dict(cfg or {})
        self.saves = 0
        self.full_saves = 0
        self.tracked = []
        self.announced = []
        self.upsells = []
        self.sig_setting_changed.connect(self.announced.append)

    def schedule_config_save(self):
        self.saves += 1

    def save_config(self):
        self.full_saves += 1

    def track(self, event, props=None):
        self.tracked.append(event)

    def _pro_upsell(self, feature=None):
        self.upsells.append(feature)


@pytest.fixture
def glass_ok():
    """This computer can hide windows (Windows 10 2004+, local session)."""
    from ui import glass
    with mock.patch.object(glass, "capture_exclusion_supported", return_value=True), \
            mock.patch.object(glass, "is_remote_session", return_value=False), \
            mock.patch.object(glass, "exclude_from_capture", return_value=True), \
            mock.patch.object(glass, "is_excluded_from_capture", return_value=True), \
            mock.patch.object(glass, "macos_26_or_later", return_value=False):
        yield glass


@pytest.fixture
def card(qapp, glass_ok):
    from ui import live_assist
    made = []

    def build(**cfg):
        app = _App(cfg)
        c = live_assist.LiveAssistOverlay(main_app=app)
        made.append(c)
        return c, app

    yield build
    for c in made:
        c.hide()
        c.deleteLater()
    QApplication.processEvents()


def _recorder(signal):
    got = []
    signal.connect(lambda *a: got.append(a if len(a) > 1 else a[0]))
    return got


# ── icons ──
def test_incognito_icon_is_drawn_and_cached_per_colour(qapp):
    from ui.icons import eye_icon, incognito_icon, incognito_pixmap
    green = incognito_icon(16, "#15803d")
    assert not green.isNull()
    assert incognito_icon(16, QColor("#15803d")) is green          # same colour: cached
    assert incognito_icon(16, "#4ade80") is not green              # dark-glass green: its own
    img = incognito_pixmap(48, "#15803d").toImage()
    inked = [img.pixelColor(x, y) for x in range(img.width()) for y in range(img.height())
             if img.pixelColor(x, y).alpha() > 200]
    assert inked and all(abs(c.green() - 0x80) < 8 and c.red() < 40 for c in inked)
    # The eye no longer shares one cached picture between colours.
    assert eye_icon(False, 16, "#15803d") is not eye_icon(False, 16, "#b45309")
    assert eye_icon(True, 16, "#b91c1c") is eye_icon(True, 16, QColor("#b91c1c"))


# ── words ──
def test_the_card_says_gusan_and_live_assistant(card):
    c, _ = card()
    assert c.windowTitle() == "Live Assistant"
    assert c.lbl_title.text() == "Live Assistant"
    assert c.txt_summary.placeholderText() == "Press Start. What's said shows up here."
    assert c.txt_suggestion.placeholderText().startswith("Answers appear here")
    seen = []
    for w in [c] + c.findChildren(QWidget):
        seen += [w.toolTip(), w.windowTitle(), w.accessibleName()]
        if isinstance(w, (QAbstractButton, QLabel, QLineEdit)):
            seen.append(w.text())
        if isinstance(w, (QLineEdit, QTextEdit)):
            seen.append(w.placeholderText())
    text = " ".join(s for s in seen if s)
    for old in ("Live Assistance", "Transcribe", "Record Meeting", "Private"):
        assert old not in text, old


def test_start_carries_its_pro_tag(card):
    from PySide6.QtCore import Qt
    c, _ = card()
    assert c.btn_start.text() == "Start"
    assert not c.btn_start.icon().isNull()
    assert c.btn_start.layoutDirection() == Qt.RightToLeft     # the tag after the word


def test_start_for_a_free_user_opens_the_pro_window(card):
    c, app = card()
    started = []
    app.meeting_session = SimpleNamespace(state="idle", start=lambda **kw: started.append(kw))
    app.is_pro = lambda: False
    c._start_listening()
    assert app.upsells == ["Live Assistant"]
    assert started == []


def test_start_and_stop_drive_the_meeting_session(card):
    # Gusan 2.0: the card records through app.meeting_session (source
    # "live_assist"), in the call language, and stops it the same way.
    c, app = card()
    calls = []

    class Session:
        state = "idle"
        meeting_dir = None

        def start(self, **kw):
            calls.append(("start", kw))
            self.state = "recording"
            return True, "", ""

        def stop(self, *a, **k):
            calls.append(("stop",))
            self.state = "processing"
            return True
    app.meeting_session = Session()
    app.is_pro = lambda: True
    app.cfg.update(meeting_consent_ack=True, live_assist_language="de")
    app.recorder = SimpleNamespace(fast_live_chunks=False)
    with mock.patch("threading.Thread"):
        c._start_listening()
    kind, kw = calls[0]
    assert kind == "start" and kw["source"] == "live_assist" and kw["language"] == "de"
    assert kw["audio_mode"] == "smart_meeting" and app.recorder.fast_live_chunks is True
    c._stop_listening()
    assert calls[-1] == ("stop",)


def test_a_refused_start_says_why(card):
    c, app = card()
    app.meeting_session = SimpleNamespace(
        state="idle", start=lambda **kw: (False, "file_job", "A file is being converted right now.\nMore."))
    app.is_pro = lambda: True
    app.cfg.update(meeting_consent_ack=True)
    app.recorder = SimpleNamespace(fast_live_chunks=False)
    with mock.patch("threading.Thread"):
        c._start_listening()
    assert c.lbl_status.text() == "A file is being converted right now."
    assert app.recorder.fast_live_chunks is False


# ── the Hidden chip ──
ON = dict(remote=False, supported=True, excluded=True)
STATES = [
    # (name, private, refresh kwargs, platform, text, tone, click hint)
    ("on", True, ON, "win32", "Hidden", "ok", "Click to show it"),
    ("off", False, dict(remote=False, supported=True, excluded=False), "win32", "Visible", "red",
     "Click to hide it"),
    ("failed", True, dict(remote=False, supported=True, excluded=False), "win32", "Visible", "red",
     None),
    ("unavailable", True, dict(remote=True, supported=True, excluded=False), "win32",
     "Not available", "amber", None),
    ("partial", True, dict(remote=False, supported=False, excluded=True), "darwin",
     "May be hidden", "amber", "Click to make it visible"),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("name,private,kw,platform,text,tone,click", STATES,
                         ids=[s[0] for s in STATES])
def test_chip_state(card, theme, name, private, kw, platform, text, tone, click):
    from ui.live_assist import chip_colors
    c, _ = card(live_assist_theme=theme)
    c._private = private
    with mock.patch.object(sys, "platform", platform):
        c._refresh_private_chip(**kw)
    st = c.hide_state()
    assert (st.key, st.tone) == (name, tone)
    assert c.btn_private.text() == text
    assert c.btn_private.objectName() == "laPrivate"           # styling id kept
    assert not c.btn_private.icon().isNull()
    assert c.btn_private.accessibleName() == st.headline
    _bg, fg, _line = chip_colors(tone, theme)
    assert f"color: {fg};" in c.btn_private.styleSheet()
    tip = c.btn_private.toolTip()
    assert (click in tip) if click else ("Click" not in tip)
    for never in ("invisible", "undetect", "always hidden"):
        assert never not in tip.lower()


def test_hidden_is_green_on_both_glasses(qapp):
    from ui.live_assist import chip_colors
    assert chip_colors("ok", "light")[1] == "#15803d"
    assert chip_colors("ok", "dark")[1] == "#4ade80"


def test_chip_and_timer_follow_the_theme(card):
    c, _ = card()
    c._private = True
    c._refresh_private_chip(**ON)
    assert "color: #15803d;" in c.btn_private.styleSheet()
    assert "color: #15803d;" in c.styleSheet()                 # the live timer
    c.set_theme("dark")
    assert "color: #4ade80;" in c.btn_private.styleSheet()
    assert "color: #4ade80;" in c.styleSheet()
    assert "#15803d" not in c.styleSheet()


def test_hide_status_changed_fires_on_a_new_state_only(card):
    c, _ = card()
    got = _recorder(c.hide_status_changed)
    c._private = True
    c._refresh_private_chip(**ON)
    c._refresh_private_chip(**ON)
    c._private = False
    c._refresh_private_chip(remote=False, supported=True, excluded=False)
    assert got == ["off"]                     # built as "on" (this computer can hide it)


def test_a_hidden_card_predicts_what_showing_it_does(card):
    c, _ = card(live_assist_private=False)
    assert not c.isVisible()
    assert c.hide_state().key == "off"
    c.set_private(True)
    assert c.hide_state().key == "on"                         # Windows 10 2004+, local
    assert c.btn_private.text() == "Hidden"


def test_the_shared_state_matches_the_page_helper(card):
    import live_context
    c, app = card()
    assert c.hide_state() == live_context.hide_status(app.cfg.get("live_assist_private", True),
                                                      mac=sys.platform == "darwin")


# ── setters and sync ──
def test_setters_apply_save_and_announce(card):
    c, app = card()
    changed = _recorder(c.settings_changed)
    c.set_theme("dark")
    assert (c._theme_name, app.cfg["live_assist_theme"]) == ("dark", "dark")
    c.set_theme("dark")                                        # nothing new: nothing happens
    c.set_auto_answer(False)
    assert not c.btn_auto.isChecked() and app.cfg["live_assist_auto_answer"] is False
    c.set_screen_auto(False)
    assert not c.btn_screen.isChecked() and app.cfg["live_assist_screen_auto"] is False
    c.set_context("  Board prep with Anna.  ")
    assert app.cfg["live_assist_context"] == "Board prep with Anna."
    assert "Board prep" in c.ctx_chip.text.text()
    c.set_opacity(0.8)
    assert abs(c.windowOpacity() - 0.8) < 0.01 and app.cfg["live_assist_opacity"] == 0.8
    c.move(5, 5)
    c.reset_position()
    assert app.cfg["live_assist_pos"] is None and c.pos().x() != 5
    keys = ["live_assist_theme", "live_assist_auto_answer", "live_assist_screen_auto",
            "live_assist_context", "live_assist_opacity", "live_assist_pos"]
    assert [k for k, _v in changed] == keys
    assert app.announced == keys
    assert app.saves == len(keys) and app.full_saves == 0      # debounced saves only


def test_the_same_opacity_is_not_a_change(card):
    # Qt keeps window opacity in 1/255 steps (0.96 reads back as 0.957): the
    # card compares with what it set, so a page re-sending it changes nothing.
    c, app = card(live_assist_opacity=0.96)
    changed = _recorder(c.settings_changed)
    c.set_opacity(0.96)
    app.sig_setting_changed.emit("live_assist_opacity")
    assert changed == [] and app.saves == 0
    c.set_opacity(0.7)
    c.set_opacity(0.7)
    assert changed == [("live_assist_opacity", 0.7)] and app.saves == 1
    c.set_opacity(0.2)                                         # clamped, and saved so
    assert app.cfg["live_assist_opacity"] == 0.55 and changed[-1] == ("live_assist_opacity", 0.55)


def test_card_buttons_write_through_the_same_path(card):
    c, app = card(live_assist_private=True)
    changed = _recorder(c.settings_changed)
    c.btn_private.click()
    assert c._private is False and app.cfg["live_assist_private"] is False
    c.btn_auto.click()
    c.btn_theme.click()
    assert [k for k, _v in changed] == ["live_assist_private", "live_assist_auto_answer",
                                        "live_assist_theme"]
    assert app.announced == ["live_assist_private", "live_assist_auto_answer",
                             "live_assist_theme"]


def test_the_card_follows_cfg_without_writing_back(card):
    c, app = card()
    changed = _recorder(c.settings_changed)
    for key, value in (("live_assist_theme", "dark"), ("live_assist_auto_answer", False),
                       ("live_assist_screen_auto", False), ("live_assist_context", "Sales call"),
                       ("live_assist_opacity", 0.9), ("live_assist_private", False),
                       ("hotkey", "alt+q")):
        app.cfg[key] = value
        app.sig_setting_changed.emit(key)                     # what set_setting does
    assert c._theme_name == "dark" and not c._auto and not c.btn_screen.isChecked()
    assert c._context == "Sales call" and abs(c.windowOpacity() - 0.9) < 0.01
    assert c._private is False and c.hide_state().key == "off"
    assert [k for k, _v in changed] == ["live_assist_theme", "live_assist_auto_answer",
                                        "live_assist_screen_auto", "live_assist_context",
                                        "live_assist_opacity", "live_assist_private"]
    assert app.saves == 0 and app.full_saves == 0              # nothing written back


def test_reset_position_from_the_page(card):
    c, app = card(live_assist_pos=[5, 5])
    c.move(5, 5)
    changed = _recorder(c.settings_changed)
    app.cfg["live_assist_pos"] = None
    app.sig_setting_changed.emit("live_assist_pos")
    assert c.pos().x() != 5 and changed == [("live_assist_pos", None)]
    assert app.saves == 0
    c.move(5, 5)
    changed.clear()
    app.announced.clear()
    app.cfg["live_assist_pos"] = [5, 5]
    c.reset_position()                                         # the card's own call: once
    assert changed == [("live_assist_pos", None)] and app.announced == ["live_assist_pos"]


def test_a_drag_saves_the_spot_through_the_same_path(card):
    c, app = card()
    changed = _recorder(c.settings_changed)
    c.move(40, 50)
    c._drag_ended()
    pos = [c.x(), c.y()]
    assert app.cfg["live_assist_pos"] == pos
    assert changed == [("live_assist_pos", pos)] and app.announced == ["live_assist_pos"]
    assert app.saves == 1 and app.full_saves == 0              # debounced
    c._drag_ended()                                            # not moved: nothing new
    assert len(changed) == 1 and app.saves == 1


def test_settings_apply_reaches_the_card_once(card):
    import settings_apply
    c, app = card(live_assist_private=True)
    app.live_assist = c
    changed = _recorder(c.settings_changed)
    res = settings_apply.apply(app, {"live_assist_private": False})
    assert res == {"live_assist_private": True}
    assert c._private is False and c.btn_private.text() == "Visible"
    assert app.saves == 1                                      # settings_apply's own save
    assert app.announced == ["live_assist_private"]
    assert changed == [("live_assist_private", False)]


def test_a_card_without_a_controller_still_saves(qapp, glass_ok):
    from ui import live_assist
    app = SimpleNamespace(cfg={}, save_config=mock.MagicMock(), track=mock.MagicMock())
    c = live_assist.LiveAssistOverlay(main_app=app)
    try:
        c.set_theme("dark")
        assert app.cfg["live_assist_theme"] == "dark"
        app.save_config.assert_called_once()
    finally:
        c.deleteLater()


# ── open, footer ──
def test_open_card_only_shows(card):
    c, app = card()
    c.open_card()
    c.open_card()
    assert c.isVisible()
    assert app.tracked == ["live_prompter_opened"]             # telemetry name kept
    c.toggle()
    assert not c.isVisible()


def test_this_computer_without_a_local_model_says_what_to_download(card):
    # "Answers written by: This computer" and no local model: the card names
    # the download, not the sign-in / API key advice (which wouldn't help).
    import actions
    for cfg, want in (({"live_assist_writer": "local"}, actions.LOCAL_ANSWERS_MESSAGE),
                      ({"privacy_mode": True}, actions.LOCAL_ANSWERS_MESSAGE),
                      ({"live_assist_writer": ""}, actions.NO_ENGINE_MESSAGE)):
        c, _ = card(**cfg)
        c._resolve_engine = lambda cfg=cfg: (None, dict(cfg))
        c._gen = 3
        c._suggest_worker("Conversation (latest part):\nHow much is it?", None, 3)
        QApplication.processEvents()
        shown = c.txt_suggestion.toPlainText()
        assert want in shown, shown
    assert "Download Qwen 1.5B in Settings › Speech & AI to answer on this computer" in actions.LOCAL_ANSWERS_MESSAGE


def test_footer_names_who_answered(card):
    import actions
    c, _ = card()
    c._suggest_started = 100.0
    c._first_token_at = 100.8
    c._last_attached_screen = False
    c._answer_engine = (7, actions.API_MANAGED_ID)
    text = c._footer_text(7, 2.0)
    assert text.startswith("Answered ")
    assert " · Gusan Cloud · first words 0.8s · AI can be wrong - check facts" in text
    assert "Gusan Cloud" not in c._footer_text(8, 2.0)          # another answer's engine
    c._answer_engine = (7, actions.API_CEREBRAS_ID)
    assert "your Cerebras key" in c._footer_text(7, 2.0)
    local = next(k for k, v in actions.ACTION_MODELS.items() if v.get("kind") == "local_llm")
    c._answer_engine = (7, local)
    assert actions.ACTION_MODELS[local]["label"] + " on this computer" in c._footer_text(7, 2.0)
    c._first_token_at = 0.0
    assert "done 2.0s" in c._footer_text(7, 2.0)


# ── a streamed answer (D9) ──
def _wait(cond, timeout=2.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_a_streamed_answer_is_shown_rendered_never_as_raw_markdown(card):
    import time
    c, _ = card()
    c._gen, c._suggesting, c._suggest_started = 5, True, time.time()
    c._on_partial("**Say** this:\n\n- the price is fixed\n- delivery in May", 5)
    shown = c.txt_suggestion.toPlainText()
    assert "Say this:" in shown and "**" not in shown and "- the price" not in shown
    # Within 120 ms of that: drawn a moment later (throttled), never dropped.
    c._on_partial("**Say** this:\n\n- the price is fixed\n- delivery in May\n- a **disc", 5)
    assert "disc" not in c.txt_suggestion.toPlainText()
    assert _wait(lambda: "a disc" in c.txt_suggestion.toPlainText())
    assert "**" not in c.txt_suggestion.toPlainText()          # the half-written ** isn't shown
    sb = c.txt_suggestion.verticalScrollBar()
    assert sb.value() == sb.maximum()                          # still follows the end
    # An older question's tokens never land; the final answer replaces the stream.
    c._on_partial("old answer", 4)
    c._on_suggestion("Final **answer**.", "", 5)
    c._on_partial("late token", 5)
    QApplication.processEvents()
    assert c.txt_suggestion.toPlainText().strip() == "Final answer."


def test_streaming_markdown_hides_half_written_markers():
    from ui.live_assist import streaming_markdown
    assert streaming_markdown("Say **this") == "Say this"
    assert streaming_markdown("Say **this** now") == "Say **this** now"
    assert streaming_markdown("Run `pip") == "Run pip"
    assert streaming_markdown("```python\nprint(1)") == "```python\nprint(1)\n```"
    assert streaming_markdown("") == ""
