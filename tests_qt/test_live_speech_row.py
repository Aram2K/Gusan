"""Gusan 2.0.1: what transcribes a Live Assistant call, as a person sees and sets it.

* the page's "Transcribes with" row (ui/pages/live_speech.py): REAL model names with a LOCAL /
  CLOUD / PRO chip under headings (On this computer / Gusan Cloud / Your own key): Automatic,
  models on this computer (download marks; the Armenian model once it is available), Fast and Best
  accuracy on Gusan Cloud (its server chooses the model), your own Google key with its models; Pro first, the Privacy-mode question, the download
  question; instant apply; the plain Armenian hint and the calm "couldn't be transcribed just
  now" line; accessible names and the keyboard path; Free / guest / Pro;
* the card (ui/live_assist.py): one line saying who transcribes (what RAN) and the same hint.
"""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from tests_qt.conftest import make_stub_app
from tests_qt.test_model_picker import FakeDownloads, _click_row

LIVE_CFG = {"live_assist_private": True, "live_assist_auto_answer": True, "live_assist_screen_auto": True,
            "live_assist_language": "", "live_assist_output_language": "", "live_assist_theme": "light",
            "live_assist_opacity": 0.96, "live_assist_context": "", "live_assist_speech_model": ""}
HY = {"live_assist_language": "hy", "backend": "local", "whisper_model": "base"}
KEY = {"google_api_key": "AIza-test"}


@pytest.fixture
def la(qapp, monkeypatch):
    import live_context
    from ui.pages import live_assistant
    real = live_context.hide_status
    monkeypatch.setattr(live_context, "hide_status",
                        lambda wanted, **kw: real(wanted, **dict(dict(supported=True, remote=False, mac=False), **kw)))
    monkeypatch.setattr(live_assistant, "load_sessions", lambda: [])
    return live_assistant


@pytest.fixture
def asked(monkeypatch):
    """Every question a mini popup would ask; answer with .answer."""
    from ui.components import mini_prompt
    rec = SimpleNamespace(calls=[], answer=True)

    def fake_ask(parent, title, body, yes, no, icon="info", private=False):
        rec.calls.append((title, body, yes, no))
        return rec.answer
    monkeypatch.setattr(mini_prompt, "ask", fake_ask)
    return rec


@pytest.fixture
def build(la):
    made = []

    def _build(tier="pro", **cfg):
        app = make_stub_app(tier)
        app.cfg.update(LIVE_CFG)
        app.cfg.update(cfg)
        app.live_assist = None
        holder = {}

        def set_setting(key, value):
            app.calls.append(("set_setting", key, value))
            app.cfg[key] = value
            page = holder.get("page")
            if page is not None:
                page.on_setting_changed(key)
            return True
        app.set_setting = set_setting
        app.set_settings = lambda updates: {k: set_setting(k, v) for k, v in updates.items()}
        app.show_main = lambda *a, **k: app.calls.append(("show_main",) + a + tuple(sorted(k.items())))
        host = QWidget()
        from ui import theme
        theme.apply(host)
        host.resize(1048, 1100)
        page = la.create(app)
        page.setParent(host)
        page.resize(1048, 1100)
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


def _settings(app, key=None):
    return [c for c in app.calls if c[0] == "set_setting" and (key is None or c[1] == key)]


def _rows(box):
    """value -> the row's one-sentence text (the headings are not rows)."""
    return {str(box.itemData(i)): box.itemText(i) for i in box.row_indexes()}


# ── the words and keys the page copies from engine_choice ────────────────────────
def test_the_page_and_the_card_use_engine_choices_keys_and_watch_everything_it_reads(qapp):
    import engine_choice as ec
    from ui import live_assist
    from ui.pages import live_speech
    assert live_speech.SPEECH_KEY == ec.LIVE_SPEECH_KEY
    assert live_speech.UPSELL == ec.UPSELL_SPEECH
    assert live_speech.CLOUD_VALUES == (ec.LIVE_SPEECH_CLOUD, ec.LIVE_SPEECH_BEST)
    assert live_speech.OWN_PREFIX == ec.LIVE_SPEECH_OWN_GOOGLE
    # every setting the choice, its hint and the engines under Automatic read
    reads = {"live_assist_speech_model", "live_assist_language", "language", "mix_languages", "privacy_mode",
             "backend", "whisper_model", "managed_provider", "google_api_key", "google_stt_model",
             "mistral_api_key", "mistral_stt_model"}
    assert reads <= live_speech.WATCHED
    assert reads <= live_assist.HEAR_KEYS


# ── what the row lists ───────────────────────────────────────────────────────────
def test_the_row_sits_next_to_the_writer_choice_and_lists_everything(build):
    page, _app = build("pro", **KEY)
    sp = page.speech
    assert sp.row.label.text() == "Transcribes with"
    order = page.rail.rows()
    assert order.index(page.speech_row) + 1 == order.index(page.writer_row)        # right before "Answers written by"
    rows = _rows(sp.combo)
    values = list(rows)
    assert values[0] == "" and rows[""] == "Automatic · Whisper Base · Local"      # names what it would pick now
    import hardware
    assert values[1:1 + len(hardware.MODEL_INFO)] == list(hardware.MODEL_INFO)
    # REAL model names and where they run - Gusan Cloud is a heading, never the name of a model
    assert rows["small"] == "Whisper Small · Local"
    assert rows["cloud"] == "Fast · Cloud · Pro"                    # Gusan Cloud's two PROFILES: it chooses the model
    assert rows["cloud:best"] == "Best accuracy · Cloud · Pro"
    assert [v for v in values if v.startswith("own:")] == [
        "own:google:gemini-2.5-flash", "own:google:gemini-2.5-pro", "own:google:gemini-3-flash-preview"]
    assert rows["own:google:gemini-3-flash-preview"] == "Gemini 3 Flash (preview) · your Google key · Cloud"
    assert not any("Gusan Cloud" in text.split(" · ")[0] for text in rows.values())
    assert sp.combo.headings() == ["On this computer", "Gusan Cloud", "Your own key"]


def test_headings_are_not_rows_and_a_row_has_its_chips_and_its_second_line(build):
    page, _app = build("pro", live_assist_language="hy", **KEY)
    box = page.speech.combo
    for i in range(box.count()):
        if box.is_heading(i):
            assert not box.choice_enabled(i)                       # it cannot be picked or reached
            assert box.itemData(i) is None
    assert [box.row_tags(box.findData(v)) for v in ("", "small", "cloud", "cloud:best",
                                                    "own:google:gemini-2.5-pro")] == [
        ("cloud", "pro"), ("local",), ("cloud", "pro"), ("cloud", "pro"), ("cloud",)]
    assert box.row_note(box.findData("small")) == "Balanced"
    assert box.row_note(box.findData("cloud:best")) == "Most accurate · Gusan chooses the model"
    assert box.row_note(box.findData("cloud")) == "Quick answers · Gusan chooses the model"
    assert box.row_note(box.findData("")) == "for Armenian"       # Automatic says what it would pick, and why
    assert box.itemText(box.findData("")) == "Automatic · Fast · Cloud · Pro"


def test_the_own_key_rows_come_with_a_saved_key(build):
    page, app = build("pro")
    assert not [v for v in _rows(page.speech.combo) if v.startswith("own:")]
    app.cfg["google_api_key"] = "AIza-test"
    page.on_setting_changed("google_api_key")
    assert len([v for v in _rows(page.speech.combo) if v.startswith("own:")]) == 3


@pytest.mark.parametrize("tier", ["free", "guest", "pro"])
def test_free_guest_and_pro_all_see_the_row_and_its_chips(build, tier):
    page, _app = build(tier)
    box = page.speech.combo
    for value in ("cloud", "cloud:best"):
        assert box.row_tags(box.findData(value)) == ("cloud", "pro")          # the chips are about the row, not the plan
        assert box.choice_mark(box.findData(value)) == ""                       # (a mark is a download icon only)
    assert box.row_tags(box.findData("small")) == ("local",)
    assert box.currentData() == "" and box.plain_text() == "Automatic"
    assert page.speech.row.isVisibleTo(page)


def test_automatic_follows_the_plan_and_the_language(build):
    page, app = build("free", live_assist_language="hy")
    box = page.speech.combo
    assert box.row_tags(box.findData("")) == ("local",)                         # Free: Whisper on this computer
    assert box.itemText(0) == "Automatic · Whisper Base · Local"
    app.set_tier("pro")
    page.refresh_account()
    assert box.row_tags(box.findData("")) == ("cloud", "pro")                   # Pro, Armenian: Gusan Cloud
    assert box.itemText(0) == "Automatic · Fast · Cloud · Pro"
    assert box.closed_text() == "Automatic · Fast · Cloud · Pro"
    app.cfg["live_assist_language"] = "fr"
    page.on_setting_changed("live_assist_language")
    assert box.itemText(0) == "Automatic · Whisper Base · Local"


def test_a_model_not_on_this_computer_wears_the_download_icon(build):
    app_dl = FakeDownloads(ready=("base",))
    page, app = build("pro")
    app.downloads = app_dl
    page.speech.refresh()
    box = page.speech.combo
    assert box.choice_mark(box.findData("base")) == ""
    assert box.choice_mark(box.findData("large-v3")) == "download"
    assert box.choice_mark(box.findData("cloud")) == ""


# ── what it says is running ─────────────────────────────────────────────────────
def test_the_line_says_what_really_transcribes(build):
    page, app = build("pro", backend="local", whisper_model="base", live_assist_language="en")
    sp = page.speech
    assert sp.line.text() == "Whisper Base · Local · same as Dictation"
    app.cfg["live_assist_speech_model"] = "small"
    page.on_setting_changed("live_assist_speech_model")
    assert sp.line.text() == "Whisper Small · Local" and sp.combo.currentData() == "small"
    app.cfg["live_assist_speech_model"] = "cloud:best"
    page.on_setting_changed("live_assist_speech_model")
    assert sp.line.text() == "Best accuracy · Cloud" and sp.combo.plain_text() == "Best accuracy"
    assert sp.combo.closed_text() == "Best accuracy · Cloud · Pro"                 # the closed box says its tags
    assert sp.combo.current_tags() == ("cloud", "pro")
    assert sp.combo.accessibleDescription() == "Best accuracy · Cloud"


def test_a_cloud_pick_that_cannot_run_reads_automatic(build):
    page, app = build("free", live_assist_speech_model="cloud:best", live_assist_language="en", **KEY)
    assert page.speech.combo.currentData() == ""                 # no Pro: Automatic really runs
    assert page.speech.line.text() == "Whisper Base · Local · same as Dictation"
    page, app = build("pro", live_assist_speech_model="cloud", privacy_mode=True, live_assist_language="en")
    assert page.speech.combo.currentData() == ""                 # Privacy mode paused it


def test_the_line_names_the_models_the_server_answered_with(build):
    import engine_choice
    saved = dict(engine_choice._CLOUD_SEEN)
    try:
        engine_choice._CLOUD_SEEN.clear()
        page, app = build("pro", live_assist_speech_model="cloud:best", live_assist_language="en")
        assert page.speech.line.text() == "Best accuracy · Cloud"                  # nothing has answered yet
        assert not page.speech.note.isVisibleTo(page)
        engine_choice.note_cloud_model("best", "gpt-transcribe")                  # the server chose GPT Transcribe
        page.speech.refresh()
        assert page.speech.line.text() == "GPT Transcribe · Cloud"                # the model that ran, not the profile
        assert not page.speech.note.isVisibleTo(page)                             # (no "wasn't available": it chose)
        assert page.speech.combo.plain_text() == "Best accuracy"                  # the picker still names the profile
    finally:
        engine_choice._CLOUD_SEEN.clear()
        engine_choice._CLOUD_SEEN.update(saved)


# ── Armenian ─────────────────────────────────────────────────────────────────────
def test_free_armenian_says_plainly_that_local_whisper_is_not_reliable(build):
    page, _app = build("free", **HY)
    sp = page.speech
    assert sp.note.isVisibleTo(page)
    assert sp.note.text() == ("Whisper on this computer isn't reliable for Armenian. "
                              "Gusan Cloud (Pro) or your own Google key does much better.")
    assert sp.line.text() == "Whisper Base · Local · same as Dictation"
    assert "isn't reliable for Armenian" in sp.combo.accessibleDescription()   # said to a screen reader too


def test_pro_armenian_runs_on_gusan_cloud_by_itself_with_nothing_to_warn_about(build):
    page, _app = build("pro", **HY)
    assert page.speech.combo.currentData() == "" and page.speech.combo.plain_text() == "Automatic"
    assert page.speech.line.text() == "Fast · Cloud · for Armenian"
    assert not page.speech.note.isVisibleTo(page)


def test_privacy_mode_keeps_it_local_and_says_why(build):
    page, _app = build("pro", privacy_mode=True, **HY)
    assert page.speech.line.text() == "Whisper Base · Local · same as Dictation"
    assert page.speech.note.text().startswith("Privacy mode keeps transcription on this computer")
    assert "Armenian" in page.speech.note.text()


def test_the_hint_follows_the_call_language_and_goes_when_it_does_not_apply(build):
    page, app = build("free", live_assist_language="en", backend="local")
    assert not page.speech.note.isVisibleTo(page)
    page._apply_combo("live_assist_language", _combo_with(page, "hy"))          # the Call language row picks Armenian
    assert page.speech.note.isVisibleTo(page)
    page._apply_combo("live_assist_language", _combo_with(page, "fr"))
    assert not page.speech.note.isVisibleTo(page)


def _combo_with(page, value):
    page.call_lang.setCurrentIndex(page.call_lang.findData(value))
    return page.call_lang


def test_the_dictation_language_counts_when_call_language_is_the_same_as_dictation(build):
    page, app = build("free", live_assist_language="", language="hy", backend="local")
    assert page.speech.note.isVisibleTo(page)
    app.cfg["language"] = "en"
    page.on_setting_changed("language")
    assert not page.speech.note.isVisibleTo(page)


# ── a pick ───────────────────────────────────────────────────────────────────────
def test_a_whisper_model_on_disk_applies_at_once(build, asked):
    page, app = build("pro")
    app.downloads = FakeDownloads(ready=("base", "small"))
    page.speech.refresh()
    _click_row(page.speech.combo, "small")
    assert _settings(app, "live_assist_speech_model") == [("set_setting", "live_assist_speech_model", "small")]
    assert page.speech.combo.currentData() == "small" and not asked.calls
    _click_row(page.speech.combo, "")
    assert app.cfg["live_assist_speech_model"] == ""


def test_a_model_not_on_disk_asks_then_downloads_and_picks_it(build, asked):
    page, app = build("pro")
    app.downloads = FakeDownloads(ready=("base",))
    page.speech.refresh()
    _click_row(page.speech.combo, "large-v3-turbo")
    assert asked.calls == [("Download Most accurate · Whisper Large v3 Turbo?",
                            "1.6 GB - The Live Assistant will use it once it is ready.", "Download", "Not now")]
    assert app.downloads.started == ["large-v3-turbo"]
    assert app.cfg["live_assist_speech_model"] == "large-v3-turbo"
    assert page.speech.line.text().startswith("Whisper Large v3 Turbo · downloading")


def test_a_pick_while_a_live_session_runs_applies_from_the_next_one(build, asked):
    from ui.pages import live_speech
    page, app = build("pro")
    app.downloads = FakeDownloads(ready=("base", "small"))
    page.speech.refresh()
    _click_row(page.speech.combo, "small")                         # nothing is recording: nothing to add
    assert not page.speech.row.hint_label.isVisibleTo(page)
    app.meeting_session = SimpleNamespace(source="live_assist", state="recording")
    _click_row(page.speech.combo, "")
    assert app.cfg["live_assist_speech_model"] == ""                # it still applies at once (to the setting)
    assert page.speech.row.hint_label.isVisibleTo(page)
    assert page.speech.row.hint_label.text() == live_speech.APPLIES_NEXT
    assert "keeps what it started with" in live_speech.APPLIES_NEXT
    _click_row(page.speech.combo, "small")                          # the next pick starts the note afresh
    assert page.speech.row.hint_label.text() == live_speech.APPLIES_NEXT
    app.meeting_session.state = "done"                              # the session is over: the note goes
    page.speech.refresh()
    assert not page.speech.row.hint_label.isVisibleTo(page)


def test_a_meetings_session_or_a_finished_one_does_not_make_the_note(build, asked):
    page, app = build("pro")
    app.downloads = FakeDownloads(ready=("base", "small"))
    page.speech.refresh()
    for session in (SimpleNamespace(source="page", state="recording"),
                    SimpleNamespace(source="live_assist", state="done")):
        app.meeting_session = session
        _click_row(page.speech.combo, "small")
        assert not page.speech.row.hint_label.isVisibleTo(page)
        _click_row(page.speech.combo, "")


def test_not_now_changes_nothing(build, asked):
    asked.answer = False
    page, app = build("pro")
    app.downloads = FakeDownloads(ready=("base",))
    page.speech.refresh()
    _click_row(page.speech.combo, "medium")
    assert app.downloads.started == [] and not _settings(app)
    assert page.speech.combo.currentData() == ""


@pytest.mark.parametrize("value", ["cloud", "cloud:best"])
def test_gusan_cloud_for_free_opens_the_pro_window_first(build, asked, value):
    page, app = build("free", privacy_mode=True)                   # Privacy mode on: still the Pro window, no question
    _click_row(page.speech.combo, value)
    assert [c for c in app.calls if c[0] == "_pro_upsell"] == [("_pro_upsell", "Fast cloud transcription")]
    assert not asked.calls and not _settings(app)
    assert page.speech.combo.currentData() == ""
    assert page.speech.combo.choice_enabled(page.speech.combo.findData(value))   # never greyed out


def test_best_accuracy_for_pro_applies_at_once(build, asked):
    page, app = build("pro")
    _click_row(page.speech.combo, "cloud:best")
    assert _settings(app, "live_assist_speech_model") == [("set_setting", "live_assist_speech_model", "cloud:best")]
    assert not asked.calls
    assert page.speech.combo.plain_text() == "Best accuracy"
    assert page.speech.line.text() == "Best accuracy · Cloud"
    _click_row(page.speech.combo, "cloud")
    assert app.cfg["live_assist_speech_model"] == "cloud"


def test_privacy_mode_asks_before_a_cloud_pick_and_keeping_it_on_changes_nothing(build, asked):
    page, app = build("pro", privacy_mode=True)
    asked.answer = False
    _click_row(page.speech.combo, "cloud:best")
    assert asked.calls == [("Privacy mode is on",
                            "Gusan Cloud sends your voice to our servers. Turn off Privacy mode to use it?",
                            "Turn off Privacy mode", "Keep it on")]
    assert not _settings(app) and app.cfg["privacy_mode"] is True
    assert page.speech.combo.currentData() == ""
    asked.answer = True
    _click_row(page.speech.combo, "cloud:best")
    assert _settings(app) == [("set_setting", "privacy_mode", False),
                              ("set_setting", "live_assist_speech_model", "cloud:best")]
    assert page.speech.combo.currentData() == "cloud:best"


def test_your_own_key_in_privacy_mode_asks_in_words_that_name_google(build, asked):
    page, app = build("free", privacy_mode=True, **KEY)
    asked.answer = False
    _click_row(page.speech.combo, "own:google:gemini-2.5-pro")
    assert asked.calls == [("Privacy mode is on",
                            "Your Google key sends your voice to Google. Turn off Privacy mode to use it?",
                            "Turn off Privacy mode", "Keep it on")]
    assert not _settings(app)
    asked.answer = True
    _click_row(page.speech.combo, "own:google:gemini-2.5-pro")
    assert app.cfg["live_assist_speech_model"] == "own:google:gemini-2.5-pro" and app.cfg["privacy_mode"] is False
    assert page.speech.combo.plain_text() == "Gemini 2.5 Pro"
    assert page.speech.combo.closed_text() == "Gemini 2.5 Pro · your Google key · Cloud"
    assert page.speech.line.text() == "Gemini 2.5 Pro · your Google key · Cloud"


def test_your_own_key_needs_no_plan(build, asked):
    page, app = build("free", **KEY)
    _click_row(page.speech.combo, "own:google:gemini-3-flash-preview")
    assert app.cfg["live_assist_speech_model"] == "own:google:gemini-3-flash-preview"
    assert not [c for c in app.calls if c[0] == "_pro_upsell"] and not asked.calls


def test_a_removed_key_under_a_pick_reads_automatic_and_the_pick_is_kept(build):
    page, app = build("free", live_assist_speech_model="own:google:gemini-2.5-pro", **KEY)
    assert page.speech.combo.currentData() == "own:google:gemini-2.5-pro"
    app.cfg["google_api_key"] = ""
    page.on_setting_changed("google_api_key")
    assert page.speech.combo.currentData() == ""
    assert app.cfg["live_assist_speech_model"] == "own:google:gemini-2.5-pro"


# ── accessible and keyboard ──────────────────────────────────────────────────────
def test_the_row_has_names_and_a_keyboard_path(build):
    page, _app = build("pro", **KEY)
    sp = page.speech
    assert sp.combo.accessibleName() == "Transcribes with"
    assert sp.accessibleName() == "Transcribes with"
    assert sp.combo.choice_mark(sp.combo.findData("cloud")) == ""
    free, _a = build("free")
    box = free.speech.combo
    spoken = box.itemData(box.findData("cloud:best"), Qt.AccessibleTextRole)
    assert spoken == "Best accuracy · Cloud · Pro. Most accurate · Gusan chooses the model"   # its tags and its second line are spoken
    assert int(box.focusPolicy().value) & int(Qt.TabFocus.value)                 # Tab reaches it
    box.setFocus()
    QTest.keyClick(box, Qt.Key_Down)                                             # opens the list, picks nothing
    QApplication.processEvents()
    assert box.view().isVisible()
    box.hidePopup()


def test_keyboard_only_pick_goes_through_the_same_questions(build, asked):
    page, app = build("free")
    box = page.speech.combo
    box.showPopup()
    QApplication.processEvents()
    view = box.view()
    view.setCurrentIndex(box.model().index(box.findData("cloud"), 0))
    QTest.keyClick(view, Qt.Key_Return)
    QApplication.processEvents()
    assert [c for c in app.calls if c[0] == "_pro_upsell"] == [("_pro_upsell", "Fast cloud transcription")]
    assert box.currentData() == "" and not _settings(app)


def test_a_setting_that_cannot_be_written_puts_the_real_choice_back(build):
    page, app = build("pro")
    app.set_settings = lambda updates: {k: False for k in updates}               # refused by the controller
    _click_row(page.speech.combo, "cloud:best")
    assert page.speech.combo.currentData() == ""


def test_needing_a_key_opens_speech_and_ai_settings(build):
    page, app = build("pro")
    page.speech._apply("own:google:gemini-2.5-pro")
    assert ("show_main", "settings", ("section", "speech")) in app.calls


def test_a_bare_controller_does_not_break_the_row(la):
    app = SimpleNamespace(cfg={"live_assist_language": "hy"}, is_pro=lambda: 1 / 0)
    page = la.create(app)
    try:
        page.on_open()
        page.refresh_account()
        page.on_setting_changed("privacy_mode")
        assert page.speech.combo.count() >= 9
        assert page.speech.note.text()                                           # Free: the Armenian hint
    finally:
        page.deleteLater()


# ── the card ─────────────────────────────────────────────────────────────────────
class _CardApp(QObject):
    sig_setting_changed = Signal(str)
    sig_auth_changed = Signal()

    def __init__(self, cfg=None, pro=False, report=None, source="live_assist"):
        super().__init__()
        self.cfg = dict(cfg or {})
        self.pro = pro
        self.report = report
        self.auth = SimpleNamespace(is_authenticated=pro)
        self.meeting_session = SimpleNamespace(source=source, state="recording")
        self.recorder = SimpleNamespace(speech_report=lambda: self.report)
        self.tracked = []

    def is_pro(self):
        return self.pro

    def schedule_config_save(self):
        pass

    def save_config(self):
        pass

    def track(self, event, props=None):
        self.tracked.append(event)

    def _pro_upsell(self, feature=None):
        pass


@pytest.fixture
def card(qapp):
    from unittest import mock
    from ui import glass, live_assist
    made = []
    patches = [mock.patch.object(glass, "capture_exclusion_supported", return_value=True),
               mock.patch.object(glass, "is_remote_session", return_value=False),
               mock.patch.object(glass, "exclude_from_capture", return_value=True),
               mock.patch.object(glass, "is_excluded_from_capture", return_value=True),
               mock.patch.object(glass, "macos_26_or_later", return_value=False)]
    for p in patches:
        p.start()

    def build(**kw):
        app = _CardApp(**kw)
        c = live_assist.LiveAssistOverlay(main_app=app)
        made.append(c)
        return c, app
    yield build
    for c in made:
        c.hide()
        c.deleteLater()
    for p in patches:
        p.stop()
    QApplication.processEvents()


def _report(**kw):
    base = {"backend": "managed", "whisper_model": "base", "language": "hy", "cloud_model": "best",
            "ran_model": "", "cloud_pieces": 0, "local_pieces": 0}
    base.update(kw)
    return base


def test_the_card_names_who_transcribes_once_a_session_runs(card):
    c, app = card(cfg=HY, pro=True, report=_report())
    assert not c.lbl_hear.isVisibleTo(c)                          # nothing to say before Start (Pro, Armenian: cloud)
    c.set_meeting_active(True, "Call")
    assert c.lbl_hear.isVisibleTo(c)
    assert c.lbl_hear.text() == "Transcribing with Gusan Cloud · Best accuracy"       # asked, nothing answered yet
    assert c.lbl_hear.accessibleName() == "Speech engine"
    c.set_meeting_active(False)
    assert not c.lbl_hear.isVisibleTo(c)


def test_the_card_shows_what_ran_not_what_was_asked(card):
    c, app = card(cfg=HY, pro=True, report=_report())
    c.set_meeting_active(True, "Call")
    app.report = _report(ran_model="scribe_v2", cloud_pieces=3)              # the server chose ElevenLabs Scribe v2
    c._on_tick()
    assert c.lbl_hear.text() == "Transcribing with ElevenLabs Scribe v2 · Cloud"      # what RAN; no "not available"
    app.report = _report(ran_model="scribe_v2", cloud_pieces=3, local_pieces=2)
    c._on_tick()
    assert "went through this computer" in c.lbl_hear.text()
    app.report = _report(ran_model="gpt-transcribe", ran_models=["scribe_v2", "gpt-transcribe"], cloud_pieces=4)
    c._on_tick()                                                              # a piece fell to the next engine: both named
    assert c.lbl_hear.text() == "Transcribing with ElevenLabs Scribe v2 and GPT Transcribe · Cloud"


def test_the_card_says_a_model_on_this_computer_and_the_armenian_hint(card):
    c, _app = card(cfg=HY, pro=False, report=_report(backend="local", cloud_model=None, whisper_model="small"))
    assert c.lbl_hear.isVisibleTo(c)                              # idle: the hint for what Start would use
    assert c.lbl_hear.text() == ("Whisper on this computer isn't reliable for Armenian. "
                                 "Gusan Cloud (Pro) or your own Google key does much better.")
    c.set_meeting_active(True, "Call")
    assert c.lbl_hear.text().split("\n")[0] == "Transcribing with Whisper Small · Local"
    assert "isn't reliable for Armenian" in c.lbl_hear.text().split("\n")[1]


def test_armenian_pieces_that_went_through_this_computer_are_called_unreliable(card):
    # Gusan Cloud wasn't available for some pieces (offline, a cap): this computer's Whisper read
    # them, and for Armenian that is a text to check - said in the one calm line under the engine.
    c, app = card(cfg=HY, pro=True,
                  report=_report(cloud_model="", ran_model="gpt-transcribe", cloud_pieces=3, local_pieces=2))
    c.set_meeting_active(True, "Call")
    lines = c.lbl_hear.text().split("\n")
    assert lines[0] == ("Transcribing with GPT Transcribe · Cloud. "
                        "Some of it went through this computer because the cloud wasn't available.")
    assert lines[1] == ("Whisper on this computer isn't reliable for Armenian, so those parts "
                        "may have mistakes.")
    app.report = _report(cloud_model="", ran_model="gpt-transcribe", cloud_pieces=5, local_pieces=0)
    c._on_tick()
    assert "\n" not in c.lbl_hear.text()                           # all of it from the cloud: nothing to add
    app.cfg["live_assist_language"] = "en"                         # another language: Whisper is fine
    app.report = _report(language="en", cloud_model="", ran_model="gpt-transcribe", cloud_pieces=3,
                         local_pieces=2)
    c.on_setting_changed("live_assist_language")
    assert "\n" not in c.lbl_hear.text() and "went through" in c.lbl_hear.text()


def test_a_meetings_session_shows_its_engine_but_no_language_hint(card):
    c, _app = card(cfg=HY, pro=False, source="page",
                   report=_report(backend="local", cloud_model=None, whisper_model="base"))
    c.set_meeting_active(True, "Weekly")
    assert c.lbl_hear.text() == "Transcribing with Whisper Base · Local"


def test_the_card_follows_the_settings(card):
    c, app = card(cfg=dict(HY, live_assist_language="en"), pro=False)
    assert not c.lbl_hear.isVisibleTo(c)
    app.cfg["live_assist_language"] = "hy"
    c.on_setting_changed("live_assist_language")
    assert c.lbl_hear.isVisibleTo(c) and "Armenian" in c.lbl_hear.text()
    app.cfg["privacy_mode"] = True
    c.on_setting_changed("privacy_mode")
    assert c.lbl_hear.text().startswith("Privacy mode keeps transcription on this computer")
    app.cfg["live_assist_language"] = "fr"
    c.on_setting_changed("live_assist_language")
    assert not c.lbl_hear.isVisibleTo(c)


def test_the_card_survives_a_recorder_without_a_report(card):
    c, app = card(cfg=HY, pro=True)
    app.recorder = SimpleNamespace()
    c.set_meeting_active(True, "Call")
    c._on_tick()                                                  # nothing to read: a plain local line, no crash
    assert c.lbl_hear.text().startswith("Transcribing with Whisper")
    app.recorder = None
    c._on_tick()


# ── Armenian that the cloud could not read just now ──────────────────────────────────
GAP_NOTE = "Armenian couldn't be transcribed just now - retrying. Local Whisper isn't reliable for it."


def _gap_report(**kw):
    return _report(cloud_model="", ran_model="gpt-transcribe", cloud_pieces=3, gap_open=True,
                   gap_reason="server", gap_language="Armenian", gap_pieces=1, **kw)


def test_the_card_says_calmly_that_armenian_could_not_be_read_and_it_goes_by_itself(card):
    c, app = card(cfg=HY, pro=True, report=_report(cloud_model="", ran_model="gpt-transcribe", cloud_pieces=3))
    c.set_meeting_active(True, "Call")
    assert c.lbl_hear.text() == "Transcribing with GPT Transcribe · Cloud"
    app.report = _gap_report()
    c._on_tick()
    lines = c.lbl_hear.text().split("\n")
    assert lines == ["Transcribing with GPT Transcribe · Cloud", GAP_NOTE]      # what ran, then the one calm line
    assert "Voxtral" not in c.lbl_hear.text() and "Gusan Cloud ·" not in c.lbl_hear.text()
    app.report = _report(cloud_model="", ran_model="gpt-transcribe", cloud_pieces=4, gap_open=False,
                         gap_language="Armenian", gap_pieces=1)                    # the cloud answers again
    c._on_tick()
    assert c.lbl_hear.text() == "Transcribing with GPT Transcribe · Cloud"


def test_a_gap_in_the_live_transcript_is_shown_once_and_never_heard_as_speech(card):
    import meeting_session
    from ui import live_assist
    assert live_assist.GAP_MARK == meeting_session.GAP_MARK == "[...]"          # the same mark on both sides
    c, app = card(cfg=HY, pro=True, report=_report())
    c.set_meeting_active(True, "Call")
    c.feed_transcript("Բարև ձեզ", answer=False)
    c.feed_transcript("[...]")
    c.feed_transcript("[...]")                                   # a run of them is one mark
    assert c._live_text == "Բարև ձեզ [...]"
    assert c.txt_summary.toPlainText().endswith("[...]")
    c.feed_transcript("Շնորհակալություն")
    assert c._live_text == "Բարև ձեզ [...] Շնորհակալություն"
    assert c._text_since_suggest == len("Բարև ձեզ") + len("Շնորհակալություն")        # the mark added no "speech"


def test_the_pages_row_names_what_ran_and_the_same_calm_line_while_a_session_is_recorded(build):
    page, app = build("pro", **HY)
    sp = page.speech
    assert sp.line.text() == "Fast · Cloud · for Armenian"                        # before: what Start would use
    app.meeting_session = SimpleNamespace(source="live_assist", state="recording")
    report = _gap_report()
    app.recorder = SimpleNamespace(speech_report=lambda: report)
    sp.refresh()
    assert sp.line.text() == "Transcribing with GPT Transcribe · Cloud"
    assert sp.note.isVisibleTo(page) and sp.note.text() == GAP_NOTE
    assert GAP_NOTE in sp.combo.accessibleDescription()
    report = _report(cloud_model="", ran_model="gpt-transcribe", cloud_pieces=4, gap_open=False)
    sp.refresh()
    assert not sp.note.isVisibleTo(page)                                          # the cloud answered: it goes
    report = _report(cloud_model="", ran_model="voxtral-mini-latest", cloud_pieces=4)
    sp.refresh()
    assert sp.line.text() == "Transcribing with Voxtral Mini · Cloud"             # it names what RAN, never what was asked
    assert "available" not in sp.line.text()                                       # (the server chooses: no downgrade)
    app.meeting_session = SimpleNamespace(source="live_assist", state="done")
    sp.refresh()
    assert sp.line.text() == "Fast · Cloud · for Armenian"                        # the session is over: the plan again


def test_the_row_looks_again_by_itself_only_while_a_session_runs(build):
    page, app = build("pro", **HY)
    sp = page.speech
    seen = []
    real = sp.refresh
    sp.refresh = lambda: (seen.append(1), real())[1]
    sp._tick()
    assert seen == []                                            # nothing is recorded: nothing to look at
    app.meeting_session = SimpleNamespace(source="live_assist", state="recording")
    app.recorder = SimpleNamespace(speech_report=lambda: _report())
    sp._tick()
    assert seen == [1]
    app.meeting_session = SimpleNamespace(source="live_assist", state="done")
    sp._tick()                                                   # it ended: one more look puts the plan back
    sp._tick()
    assert seen == [1, 1]
