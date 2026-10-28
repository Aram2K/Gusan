"""The Armenian speech model in every picker that lists models on this computer (Gusan 2.0.1).

* hidden until it is available (speech_hy.listed): no row, no card, nothing until then; the manifest
  lookup (app.downloads.refresh_catalog) runs when a page that would list it opens, and
  ``catalog_changed`` makes the row appear on its own;
* the row: "NVIDIA FastConformer Armenian", LOCAL, "Armenian only · about 105 MB · free, works
  offline", under "On this computer" next to the Whisper models, with the download icon;
* a pick of it that is not on this computer asks first (Download / Not now), then downloads with a
  progress line and a Cancel that stops it and puts the automatic choice back; Privacy mode does not
  stop a download (it sends nothing of the person's), only the cloud rows ask about it;
* Settings > Speech & AI: one wide card (Download / progress + Cancel / Use / In use + Remove / Try again);
* the Armenian hint (the same words as the Live Assistant's) on Dictation and Audio to text, and the
  order for an Armenian file under Automatic.

Set GUSAN_SHOTS_DIR to also write renders (picker_hy_*.png; QT_SCALE_FACTOR=1.5 adds _150).
"""
import os

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

import hardware
import speech_hy
from tests_qt.test_live_speech_row import build as live_build, la  # noqa: F401  (fixtures)
from tests_qt.test_model_picker import _click_row
from tests_qt.test_page_audio_to_text import build as audio_build, store  # noqa: F401  (fixtures)
from tests_qt.test_page_home_dictation import dict_page, made, pin_clock  # noqa: F401  (fixtures)
from tests_qt.test_page_settings_account import _settings, _spin, env  # noqa: F401
from tests_qt.test_smart_actions_v2 import asked, live_app, page_for, tries  # noqa: F401

HY = hardware.HY_MODEL_ID
SHOTS = os.environ.get("GUSAN_SHOTS_DIR", "")
NOTE = "Armenian only · about 105 MB · free, works offline"
NOTE_READY = "Armenian only · free, works offline"


def _shot(widget, name):
    if not SHOTS:
        return
    os.makedirs(SHOTS, exist_ok=True)
    scale = os.environ.get("QT_SCALE_FACTOR", "1")
    suffix = "" if scale in ("1", "1.0") else "_%d" % round(float(scale) * 100)
    widget.grab().save(os.path.join(SHOTS, "picker_hy_%s%s.png" % (name, suffix)))


class HyDownloads(QObject):
    """app.downloads as the pages use it, with the Armenian model's cancel and catalog."""
    progress = Signal(str, int, object, object)
    finished = Signal(str, str, str)
    removed = Signal(str, bool, str)
    state_changed = Signal(str, str)
    scanned = Signal()
    hardware_ready = Signal(object)
    catalog_changed = Signal()

    def __init__(self, ready=("base",), hy="missing"):
        super().__init__()
        self.states = {m: "ready" for m in ready}
        if hy != "missing":
            self.states[HY] = hy
        self.started, self.cancelled, self.pct, self.errors, self.refreshed = [], [], {}, {}, 0
        self.hardware = None
        self.recommendation = None

    def is_scanned(self):
        return True

    def state(self, name):
        return self.states.get(name, "missing")

    def progress_of(self, name):
        return (self.pct[name], 1, 2) if name in self.pct else None

    def error(self, name):
        return self.errors.get(name, "")

    def is_removing(self, name):
        return False

    def start(self, name):
        self.started.append(name)
        self.states[name] = "downloading"
        self.pct[name] = 42
        self.state_changed.emit(name, "downloading")
        return True

    def cancel(self, name):
        if self.states.get(name) != "downloading":
            return False
        self.cancelled.append(name)
        self.states[name] = "missing"
        self.pct.pop(name, None)
        self.state_changed.emit(name, "missing")
        self.finished.emit(name, "missing", "")
        return True

    def finish(self, name, state="ready", msg=""):
        self.states[name] = state
        self.pct.pop(name, None)
        if msg:
            self.errors[name] = msg
        self.state_changed.emit(name, state)
        self.finished.emit(name, state, msg)

    def remove(self, name):
        self.states[name] = "missing"
        self.state_changed.emit(name, "missing")
        return True

    def refresh_catalog(self, force=False):
        self.refreshed += 1

    def ready(self, kind=None):
        ids = (hardware.SPEECH_MODEL_IDS if kind == "whisper" else hardware.LOCAL_AI_MODEL_IDS if kind == "llm"
               else (HY,) if kind == "speech_hy" else hardware.SPEECH_MODEL_IDS + hardware.LOCAL_AI_MODEL_IDS + (HY,))
        return [i for i in ids if self.states.get(i) == "ready"]

    def probe_hardware(self, cfg=None, force=False):
        return None


@pytest.fixture
def hy_listed(monkeypatch):
    """The Armenian model as these tests need it: speech_hy.listed / ready follow ``state``."""
    state = {"listed": True, "ready": False}
    monkeypatch.setattr(speech_hy, "listed", lambda *a, **k: state["listed"])
    monkeypatch.setattr(speech_hy, "ready", lambda *a, **k: state["ready"])
    monkeypatch.setattr(speech_hy, "runtime_ok", lambda: True)
    return state


def _rows(box):
    return {str(box.itemData(i)): box.itemText(i) for i in box.row_indexes()}


# ═════════════════════════════ hidden until it is available ═════════════════════════════
def test_no_row_no_card_nothing_until_it_is_available(live_build, page_for, audio_build, env, monkeypatch):
    monkeypatch.setattr(speech_hy, "listed", lambda *a, **k: False)
    monkeypatch.setattr(speech_hy, "ready", lambda *a, **k: False)
    page, _app = live_build("pro")
    assert HY not in _rows(page.speech.combo)
    sa = page_for(live_app("pro"))
    assert sa.speech_combo.findData(HY) < 0
    att, _a = audio_build("pro")
    assert att.model_combo.findData(HY) < 0
    win, _app = env.build("pro")
    settings = _settings(win, section="speech")
    assert settings.hy_card.isHidden()


def test_the_row_comes_with_the_listing_and_goes_with_it(live_build, hy_listed):
    page, app = live_build("pro")
    app.downloads = HyDownloads()
    hy_listed["listed"] = False
    page.speech.refresh()
    assert HY not in _rows(page.speech.combo)
    hy_listed["listed"] = True                                  # the manifest named the model
    app.downloads.catalog_changed.emit()                        # (ModelDownloads emits it on the GUI thread)
    QApplication.processEvents()
    assert HY in _rows(page.speech.combo)
    hy_listed["listed"] = False
    app.downloads.catalog_changed.emit()
    assert HY not in _rows(page.speech.combo)


def test_a_page_that_lists_models_asks_for_the_catalog_when_it_opens(live_build, page_for, audio_build, env,
                                                                    hy_listed):
    page, app = live_build("pro")
    app.downloads = HyDownloads()
    page.speech.hide()
    page.speech.show()
    assert app.downloads.refreshed >= 1                         # a worker call: once a day at most, never in Privacy mode
    sa_app = live_app("pro")
    sa_app.downloads = HyDownloads()
    sa = page_for(sa_app)
    sa.on_open()
    assert sa_app.downloads.refreshed >= 1
    att, att_app = audio_build("pro")
    att_app.downloads = HyDownloads()
    att.on_open()
    assert att_app.downloads.refreshed >= 1
    win, app2 = env.build("pro")
    app2.downloads = HyDownloads(ready=("small",))
    _settings(win, section="speech")
    assert app2.downloads.refreshed >= 1


def test_nothing_touches_the_network_by_itself(monkeypatch):
    # the page helper only ever asks app.downloads for a worker call; with no downloads it does nothing
    from types import SimpleNamespace
    from ui import hy_model
    hy_model.refresh_catalog(SimpleNamespace())                  # no downloads service: no error, nothing to do
    hy_model.refresh_catalog(SimpleNamespace(downloads=SimpleNamespace(state=lambda n: "ready")))


# ═════════════════════════════ the row ═════════════════════════════
def test_the_row_in_every_picker(live_build, page_for, audio_build, hy_listed):
    page, app = live_build("pro")
    app.downloads = HyDownloads()
    page.speech.refresh()
    box = page.speech.combo
    rows = _rows(box)
    assert rows[HY] == "NVIDIA FastConformer Armenian · Local"
    i = box.findData(HY)
    assert box.row_tags(i) == ("local",) and box.row_note(i) == NOTE
    assert box.choice_mark(i) == "download"                                    # not on this computer: the icon
    values = list(rows)
    assert values.index("large-v3") < values.index(HY) < values.index("cloud")  # with the models on this computer
    assert box.headings()[0] == "On this computer"
    sa_app = live_app("pro")
    sa_app.downloads = HyDownloads()
    sa = page_for(sa_app)
    j = sa.speech_combo.findData(HY)
    assert j >= 0 and sa.speech_combo.row_note(j) == NOTE and sa.speech_combo.choice_mark(j) == "download"
    att, att_app = audio_build("pro")
    att_app.downloads = HyDownloads()
    att.on_open()
    assert att.model_combo.findData(HY) >= 0
    assert att.model_combo.row_note(att.model_combo.findData(HY)) == NOTE


def test_once_it_is_here_the_row_loses_the_size_and_the_icon(live_build, hy_listed):
    hy_listed["ready"] = True
    page, app = live_build("pro")
    app.downloads = HyDownloads(hy="ready")
    page.speech.refresh()
    box = page.speech.combo
    i = box.findData(HY)
    assert box.row_note(i) == NOTE_READY and box.choice_mark(i) == ""


def test_the_row_says_it_is_armenian_only_in_its_tooltip_and_credits_nvidia(live_build, hy_listed):
    page, app = live_build("pro")
    app.downloads = HyDownloads()
    page.speech.refresh()
    box = page.speech.combo
    from PySide6.QtCore import Qt
    tip = box.itemData(box.findData(HY), Qt.ToolTipRole)
    assert "NVIDIA" in str(tip) and "CC BY 4.0" in str(tip) and "Runs on this computer" in str(tip)
    assert "NVIDIA" in box.itemText(box.findData(HY))


# ═════════════════════════════ the Live Assistant ═════════════════════════════
def test_live_a_pick_asks_then_downloads_and_the_line_shows_the_progress(live_build, asked, hy_listed):
    page, app = live_build("pro")
    app.downloads = HyDownloads()
    page.speech.refresh()
    _click_row(page.speech.combo, HY)
    assert asked.calls == [("Download the Armenian model?",
                            "NVIDIA FastConformer Armenian · 105 MB. Free, works offline. "
                            "The Live Assistant will use it for Armenian.", "Download", "Not now")]
    assert app.downloads.started == [HY]
    assert app.cfg["live_assist_speech_model"] == HY
    assert page.speech.line.text() == "NVIDIA FastConformer Armenian · downloading 42%"
    assert not page.speech.cancel_button.isHidden()


def test_live_not_now_changes_nothing(live_build, asked, hy_listed):
    asked.answer = False
    page, app = live_build("pro")
    app.downloads = HyDownloads()
    page.speech.refresh()
    _click_row(page.speech.combo, HY)
    assert app.downloads.started == [] and app.cfg["live_assist_speech_model"] == ""
    assert page.speech.combo.currentData() == ""


def test_live_cancel_stops_the_download_and_puts_automatic_back(live_build, asked, hy_listed):
    page, app = live_build("pro")
    app.downloads = HyDownloads()
    page.speech.refresh()
    _click_row(page.speech.combo, HY)
    assert not page.speech.cancel_button.isHidden()
    page.speech.cancel_button.click()
    assert app.downloads.cancelled == [HY]
    assert app.cfg["live_assist_speech_model"] == ""                          # (or the next session would start it again)
    assert page.speech.combo.currentData() == ""
    assert page.speech.row.hint_label.text() == "The download was stopped. Automatic is back."
    assert page.speech.cancel_button.isHidden()


def test_live_when_it_is_here_the_line_says_it_is_armenian_only(live_build, hy_listed):
    hy_listed["ready"] = True
    page, app = live_build("pro", live_assist_speech_model=HY, live_assist_language="en")
    app.downloads = HyDownloads(hy="ready")
    page.speech.refresh()
    assert page.speech.combo.currentData() == HY
    assert page.speech.line.text() == "NVIDIA FastConformer Armenian · Local · Armenian only"
    assert page.speech.note.isHidden() or "isn't reliable" not in page.speech.note.text()


def test_live_a_failed_download_says_so_in_plain_words(live_build, asked, hy_listed):
    page, app = live_build("pro")
    app.downloads = HyDownloads()
    page.speech.refresh()
    _click_row(page.speech.combo, HY)
    app.downloads.finish(HY, "failed", "Not enough free disk space for this model.")
    QApplication.processEvents()
    assert page.speech.row.hint_label.text() == "Not enough free disk space for this model."


def test_live_the_hint_names_the_armenian_model_when_it_can_be_had(live_build, hy_listed):
    page, app = live_build("free", live_assist_language="hy", backend="local", whisper_model="base")
    app.downloads = HyDownloads()
    page.speech.refresh()
    assert page.speech.note.text() == ("Whisper on this computer isn't reliable for Armenian. The Armenian model "
                                       "(free, works offline) or Gusan Cloud (Pro) does much better.")
    hy_listed["listed"] = False
    page.speech.refresh()
    assert page.speech.note.text() == ("Whisper on this computer isn't reliable for Armenian. "
                                       "Gusan Cloud (Pro) or your own Google key does much better.")


def test_live_with_the_armenian_model_picked_for_a_call_in_another_language_says_so(live_build, hy_listed):
    hy_listed["ready"] = True
    page, app = live_build("free", live_assist_language="de", backend="local", whisper_model="base",
                           live_assist_speech_model=HY)
    app.downloads = HyDownloads(hy="ready")
    page.speech.refresh()
    assert page.speech.note.text() == "This model reads Armenian only. German is read by Whisper instead."
    app.cfg["live_assist_language"] = "hy"
    page.speech.refresh()
    assert page.speech.note.isHidden()                                        # a call in Armenian: it reads it


def test_live_free_armenian_runs_the_model_by_itself_once_it_is_here(live_build, hy_listed):
    hy_listed["ready"] = True
    page, app = live_build("free", live_assist_language="hy", backend="local", whisper_model="base")
    app.downloads = HyDownloads(hy="ready")
    page.speech.refresh()
    assert page.speech.combo.currentData() == ""                              # Automatic picks it
    assert page.speech.line.text() == "NVIDIA FastConformer Armenian · Local · for Armenian"
    assert page.speech.note.isHidden()                                        # the model that reads it: nothing to warn about


def test_live_privacy_mode_keeps_the_model_and_still_offers_the_download(live_build, asked, hy_listed):
    page, app = live_build("pro", live_assist_language="hy", privacy_mode=True)
    app.downloads = HyDownloads()
    page.speech.refresh()
    assert page.speech.note.text().startswith("Privacy mode keeps transcription on this computer")
    assert "Armenian model (free, works offline)" in page.speech.note.text()
    _click_row(page.speech.combo, HY)                                         # no Privacy question: it sends nothing
    assert [c[0] for c in asked.calls] == ["Download the Armenian model?"]
    assert app.downloads.started == [HY] and app.cfg["privacy_mode"] is True


# ═════════════════════════════ Smart Actions ═════════════════════════════
def test_smart_a_pick_asks_then_downloads_and_cancel_puts_the_same_as_dictation_back(page_for, asked, hy_listed):
    app = live_app("pro")
    app.downloads = HyDownloads()
    page = page_for(app)
    combo = page.speech_combo
    _click_row(combo, HY)
    assert asked.calls == [("Download the Armenian model?",
                            "NVIDIA FastConformer Armenian · 105 MB. Free, works offline. "
                            "Smart Actions will use it for Armenian.", "Download", "Not now")]
    assert app.downloads.started == [HY]
    assert app.cfg["smart_actions_speech_model"] == HY
    assert page.speech_line.text() == "NVIDIA FastConformer Armenian · downloading 42%"
    assert not page.speech_cancel.isHidden()
    page.speech_cancel.click()
    assert app.downloads.cancelled == [HY]
    assert app.cfg["smart_actions_speech_model"] == ""
    assert page.speech_cancel.isHidden()
    assert page.speech_row.hint_label.text() == "The download was stopped. Same as Dictation is back."


def test_smart_when_it_is_here_it_is_picked_at_once_and_says_armenian_only(page_for, asked, hy_listed):
    hy_listed["ready"] = True
    app = live_app("pro")
    app.downloads = HyDownloads(hy="ready")
    page = page_for(app)
    _click_row(page.speech_combo, HY)
    assert not asked.calls and app.cfg["smart_actions_speech_model"] == HY
    assert page.speech_line.text() == "NVIDIA FastConformer Armenian · Local · Armenian only"


# ═════════════════════════════ Audio to text ═════════════════════════════
def _wire(page, app):
    """The stub's set_setting only records: make it write cfg and tell the page, as the controller does."""
    record = app.set_setting

    def set_setting(key, value):
        record(key, value)
        app.cfg[key] = value
        page.on_setting_changed(key)
        return True
    app.set_setting = set_setting


def test_audio_a_pick_asks_then_starts_the_download_and_not_now_changes_nothing(audio_build, asked, hy_listed):
    page, app = audio_build("pro")
    app.downloads = HyDownloads()
    _wire(page, app)
    page.on_open()
    asked.answer = False
    page.model_combo.setCurrentIndex(page.model_combo.findData(HY))
    assert app.downloads.started == [] and app.cfg.get("file_model", "auto") in ("auto", None)
    assert page.model_combo.currentData() == "auto"
    asked.answer = True
    page.model_combo.setCurrentIndex(page.model_combo.findData(HY))
    assert asked.calls[-1] == ("Download the Armenian model?",
                               "NVIDIA FastConformer Armenian · 105 MB. Free, works offline. "
                               "Audio to text will use it for Armenian.", "Download", "Not now")
    assert app.downloads.started == [HY]
    assert app.cfg["file_model"] == HY
    assert page.model_line.text() == "NVIDIA FastConformer Armenian · downloading 42%"
    assert not page.model_cancel.isHidden()
    page.model_cancel.click()
    assert app.downloads.cancelled == [HY] and app.cfg["file_model"] == "auto"
    assert page.model_cancel.isHidden()


def test_audio_the_model_line_says_armenian_only_once_it_is_picked(audio_build, hy_listed):
    hy_listed["ready"] = True
    page, app = audio_build("pro", file_model=HY)
    app.downloads = HyDownloads(hy="ready")
    page.on_open()
    assert page.model_combo.currentData() == HY
    assert page.model_line.text() == "NVIDIA FastConformer Armenian · Local · Armenian only"


def test_audio_an_armenian_file_under_automatic_goes_to_gusan_cloud_for_pro(audio_build, hy_listed):
    page, app = audio_build("pro", file_language="hy", privacy_mode=False)
    app.downloads = HyDownloads()
    page.on_open()
    assert page.model_combo.currentData() == "auto"
    assert page.model_line.text() == "Best accuracy · Cloud · for Armenian"
    assert page.rail_foot.text() == "Sent to Gusan Cloud to transcribe."
    assert page.model_row.hint_label.isHidden()                                # nothing to warn about
    # ... and the Automatic row says so too: CLOUD and PRO, never LOCAL for audio that leaves this computer
    from ui.components import model_combo as mc
    box = page.model_combo
    i = box.findData("auto")
    assert box.itemData(i, mc.TAGS_ROLE) == ("cloud", "pro")
    assert box.itemData(i, mc.TITLE_ROLE) == "Automatic · Best accuracy" and box.itemData(i, mc.NOTE_ROLE) == "for Armenian"
    app.cfg["file_language"] = "fr"                                           # another language: Whisper, so LOCAL again
    page.on_setting_changed("file_language")
    i = box.findData("auto")
    assert box.itemData(i, mc.TAGS_ROLE) == ("local",) and box.itemData(i, mc.NOTE_ROLE) == "Picked for this computer"
    app.cfg["file_language"] = "hy"
    page.on_setting_changed("file_language")
    assert box.itemData(box.findData("auto"), mc.TAGS_ROLE) == ("cloud", "pro")


def test_audio_without_the_cloud_the_armenian_model_reads_it_when_it_is_here(audio_build, hy_listed):
    hy_listed["ready"] = True
    page, app = audio_build("free", file_language="hy")
    app.downloads = HyDownloads(hy="ready")
    page.on_open()
    assert page.model_line.text() == "NVIDIA FastConformer Armenian · Local"
    assert page.model_row.hint_label.isHidden()
    assert page.rail_foot.text() == "On this computer. Nothing is uploaded."


def test_audio_else_whisper_with_the_hint_that_offers_the_model(audio_build, hy_listed):
    page, app = audio_build("free", file_language="hy")
    app.downloads = HyDownloads()
    page.on_open()
    assert not page.model_row.hint_label.isHidden()
    assert page.model_row.hint_label.text() == ("Whisper on this computer isn't reliable for Armenian. The Armenian "
                                                "model (free, works offline) or Gusan Cloud (Pro) does much better.")
    page2, app2 = audio_build("pro", file_language="hy", privacy_mode=True)
    app2.downloads = HyDownloads()
    page2.on_open()
    # Privacy mode: the same Armenian words, in their Privacy form (why the cloud is off, and the offer)
    assert page2.model_row.hint_label.text() == ("Privacy mode keeps transcription on this computer, where Whisper "
                                                 "isn't reliable for Armenian. The Armenian model (free, works "
                                                 "offline) does much better.")
    page3, app3 = audio_build("pro", file_language="fr", privacy_mode=True)
    app3.downloads = HyDownloads()
    page3.on_open()
    assert page3.model_row.hint_label.text() == "Privacy mode keeps it on this computer."    # another language: as ever


def test_audio_another_language_has_no_hint(audio_build, hy_listed):
    page, app = audio_build("free", file_language="fr")
    app.downloads = HyDownloads()
    page.on_open()
    assert page.model_row.hint_label.isHidden()


def test_audio_with_the_armenian_model_picked_says_what_reads_another_language(audio_build, hy_listed):
    # The model is picked but the file is in French (or Automatic): Whisper reads it - the page says so.
    hy_listed["ready"] = True
    page, app = audio_build("free", file_model=HY, file_language="fr")
    app.downloads = HyDownloads(hy="ready")
    page.on_open()
    assert not page.model_row.hint_label.isHidden()
    assert page.model_row.hint_label.text() == "This model reads Armenian only. French is read by Whisper instead."
    page2, app2 = audio_build("free", file_model=HY, file_language="hy")
    app2.downloads = HyDownloads(hy="ready")
    page2.on_open()
    assert page2.model_row.hint_label.isHidden()                          # it reads this file


# ═════════════════════════════ Dictation ═════════════════════════════
def test_dictation_says_the_same_armenian_words(qapp, hy_listed):
    import engine_choice
    cfg = {"language": "hy", "backend": "local", "whisper_model": "base"}
    free = engine_choice.dictation_note(cfg, False)
    assert free == ("Whisper on this computer isn't reliable for Armenian. The Armenian model "
                    "(free, works offline) or Gusan Cloud (Pro) does much better.")
    pro = engine_choice.dictation_note(cfg, True, True)
    assert pro == "Whisper on this computer isn't reliable for Armenian. The Armenian model or Gusan Cloud does much better."
    assert engine_choice.dictation_note(dict(cfg, language="fr"), False) == ""
    assert engine_choice.dictation_note(dict(cfg, backend="managed"), True, True) == ""     # the cloud reads it
    assert engine_choice.dictation_note(dict(cfg, whisper_model=HY), False) == ""            # the model itself
    hy_listed["ready"] = True
    assert engine_choice.dictation_note(cfg, False) == ("Whisper on this computer isn't reliable for Armenian. "
                                                        "Pick the Armenian model - it's already here.")


def test_the_dictation_page_shows_the_hint_under_its_runs_on_line(dict_page, hy_listed):
    page, app, _ = dict_page("free", language="hy", backend="local", whisper_model="small")
    assert not page.runs_hint.isHidden()
    assert page.runs_hint.text() == ("Whisper on this computer isn't reliable for Armenian. The Armenian model "
                                     "(free, works offline) or Gusan Cloud (Pro) does much better.")
    app.cfg["language"] = "fr"
    page.on_setting_changed("language")
    assert page.runs_hint.isHidden()                                     # another language: nothing to say
    app.cfg.update(language="hy", whisper_model=HY)
    page.on_setting_changed("whisper_model")
    assert page.runs_hint.isHidden()                                     # the Armenian model reads it
    app.cfg["language"] = "auto"
    page.on_setting_changed("language")
    assert page.runs_hint.text() == "This model reads Armenian only. With Automatic, Whisper reads your speech instead."
    app.cfg["language"] = "fr"
    page.on_setting_changed("language")
    assert page.runs_hint.text() == "This model reads Armenian only. French is read by Whisper instead."


# ═════════════════════════════ Settings > Speech & AI ═════════════════════════════
def _card_texts(card):
    return card.title.text(), card.sub.text(), sorted(card.buttons)


def test_settings_the_card_follows_the_download(env, hy_listed):
    win, app = env.build("pro")
    app.downloads = HyDownloads(ready=("small",))
    page = _settings(win, section="speech")
    card = page.hy_card
    assert not card.isHidden()
    assert _card_texts(card) == ("NVIDIA FastConformer Armenian", NOTE, ["download"])
    card.buttons["download"].click()
    assert app.downloads.started == [HY]
    _spin()
    assert _card_texts(card)[2] == ["cancel"] and card.sub.text() == "Downloading · 42%"
    card.buttons["cancel"].click()
    assert app.downloads.cancelled == [HY]
    _spin()
    assert _card_texts(card) == ("NVIDIA FastConformer Armenian", NOTE, ["download"])
    card.buttons["download"].click()
    hy_listed["ready"] = True
    app.downloads.finish(HY, "ready")                                          # done: it is picked like a Whisper card
    _spin(10)
    assert app.cfg["whisper_model"] == HY
    assert card.sub.text() == NOTE_READY and sorted(card.buttons) == ["remove"]
    assert card.findChildren(type(card.action_lay.itemAt(0).widget())) is not None


def test_settings_a_failed_download_says_why_and_offers_another_try(env, hy_listed):
    win, app = env.build("pro")
    app.downloads = HyDownloads(ready=("small",), hy="failed")
    app.downloads.errors[HY] = "Not enough free disk space for this model."
    page = _settings(win, section="speech")
    assert page.hy_card.sub.text() == "Not enough free disk space for this model."
    assert list(page.hy_card.buttons) == ["download"] and page.hy_card.buttons["download"].text() == "Try again"


def test_settings_use_remove_and_the_cleanup_of_the_picks(env, hy_listed):
    hy_listed["ready"] = True
    win, app = env.build("pro", live_assist_speech_model=HY, file_model=HY, smart_actions_speech_model=HY)
    app.downloads = HyDownloads(ready=("small",), hy="ready")
    page = _settings(win, section="speech")
    card = page.hy_card
    assert sorted(card.buttons) == ["remove", "use"]
    card.buttons["use"].click()
    assert app.cfg["whisper_model"] == HY
    page._on_removed(HY, True, "")                                              # it was removed: nothing keeps asking for it
    assert app.cfg["live_assist_speech_model"] == "" and app.cfg["file_model"] == "auto"
    assert app.cfg["smart_actions_speech_model"] == ""


# ═════════════════════════════ renders ═════════════════════════════
def _open_list(box, name):
    box.showPopup()
    QApplication.processEvents()
    _shot(box.view().window(), name)
    box.hidePopup()
    QApplication.processEvents()


def test_render_every_picker_with_the_armenian_row(live_build, page_for, audio_build, env, hy_listed):
    from PySide6.QtCore import QPoint, QRect
    from ui import theme

    def rail(page, box, name):
        assert (box.findData(HY) >= 0) == hy_listed["listed"], name         # the row is there only when listed
        theme.apply(page)
        QApplication.processEvents()
        tl = box.mapTo(page, QPoint(0, 0))
        area = QRect(max(0, tl.x() - 30), max(0, tl.y() - 70), box.width() + 60, box.height() + 250)
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            scale = os.environ.get("QT_SCALE_FACTOR", "1")
            suffix = "" if scale in ("1", "1.0") else "_%d" % round(float(scale) * 100)
            page.grab(area).save(os.path.join(SHOTS, "picker_hy_%s_closed%s.png" % (name, suffix)))
        _open_list(box, name + "_open")

    for state in ("hidden", "missing", "ready"):
        hy_listed["listed"] = state != "hidden"                       # hidden = no row at all, in every picker
        hy_listed["ready"] = state == "ready"
        page, app = live_build("pro", live_assist_language="hy")
        app.downloads = HyDownloads(hy="ready" if state == "ready" else "missing")
        page.speech.refresh()
        rail(page, page.speech.combo, "live_" + state)
        sa_app = live_app("pro")
        sa_app.downloads = HyDownloads(hy="ready" if state == "ready" else "missing")
        sa = page_for(sa_app)
        rail(sa, sa.speech_combo, "smart_" + state)
        att, att_app = audio_build("pro", file_language="hy")
        att_app.downloads = HyDownloads(hy="ready" if state == "ready" else "missing")
        att.on_open()
        rail(att, att.model_combo, "audio_" + state)
    # Audio to text in Privacy mode with an Armenian file: the hint says why the cloud is off and offers the model
    hy_listed["listed"], hy_listed["ready"] = True, False
    att, att_app = audio_build("pro", file_language="hy", privacy_mode=True)
    att_app.downloads = HyDownloads()
    att.on_open()
    rail(att, att.model_combo, "audio_privacy")
    # the Armenian model picked and downloading (the line + Cancel)
    hy_listed["ready"] = False
    page, app = live_build("pro", live_assist_language="hy", live_assist_speech_model=HY)
    app.downloads = HyDownloads(hy="downloading")
    app.downloads.pct[HY] = 42
    page.speech.refresh()
    rail(page, page.speech.combo, "live_downloading")
    hy_listed["listed"] = True
    win, sapp = env.build("pro")
    win.resize(1280, 900)
    sapp.downloads = HyDownloads(ready=("small",))
    win.show()
    page = _settings(win, section="speech")
    _shot(page.hy_card.window(), "settings_card_missing")
    sapp.downloads.start(HY)
    sapp.downloads.pct[HY] = 42
    _spin(10)
    _shot(page.hy_card.window(), "settings_card_downloading")
    hy_listed["ready"] = True
    sapp.downloads.finish(HY, "ready")
    _spin(10)
    _shot(page.hy_card.window(), "settings_card_ready")
    hy_listed["listed"] = False                                              # hidden: the page has no card at all
    win2, sapp2 = env.build("pro")
    win2.resize(1280, 900)
    sapp2.downloads = HyDownloads(ready=("small",))
    win2.show()
    page2 = _settings(win2, section="speech")
    assert page2.hy_card.isHidden()
    _shot(page2.hy_card.window(), "settings_card_hidden")
