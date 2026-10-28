"""Audio to text page (ui/pages/audio_to_text.py + transcript_view.py), offscreen.

Every view builds for Free / Pro / guests with a stub AppController (and without
any Wave 2 service), the controls have accessible names, and the key
interactions reach the right app methods: app.set_setting for the rail,
app.file_jobs for files, transcript_store for the list and the exports."""
import datetime
import os

import pytest
from PySide6.QtCore import QMimeData, QObject, QPoint, QPointF, Qt, QUrl, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLabel

from tests_qt.conftest import make_stub_app


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeFileJobs(QObject):
    queued = Signal(object)
    started = Signal(object)
    progress = Signal(int, str, str)
    finished = Signal(object)
    failed = Signal(object, str)
    cancelled = Signal(object)
    running_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.cur = None
        self.queue = []
        self.calls = []
        self.refuse = ""

    def enqueue(self, paths, opts=None):
        self.calls.append(("enqueue", list(paths), opts))
        if self.refuse:
            return [], self.refuse
        return [{"path": p} for p in paths], ""

    def cancel(self, item_id=None):
        self.calls.append(("cancel", item_id))
        return True

    def current(self):
        return self.cur

    def pending(self):
        return list(self.queue)

    def track_saved(self, fmt, **props):
        self.calls.append(("track_saved", fmt))


class FakeDownloads(QObject):
    state_changed = Signal(str, str)
    scanned = Signal()
    hardware_ready = Signal(object)

    def __init__(self, ready=("base", "small"), gpu_state="unsupported"):
        super().__init__()
        self._ready = list(ready)
        self.hardware = type("Snap", (), {"gpu_state": gpu_state, "platform": "win32"})()

    def ready(self, kind=None):
        return list(self._ready)


RUNNING = {"id": 7, "path": r"C:\rec\interview_mariam.m4a", "name": "interview_mariam.m4a",
           "kind": "voice", "state": "running", "pct": 42, "stage": "Transcribing",
           "eta": "Ready in about 6 min", "duration": 24 * 60 + 20, "model": "small",
           "model_name": "Whisper Small", "opts": {"model": "small", "language": None}}


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def store(qapp, monkeypatch):
    """An empty transcript library; disk work runs inline (deterministic)."""
    import transcript_store
    import ui.pages.transcript_view as tv
    monkeypatch.setattr(tv, "_spawn", lambda fn: fn())
    transcript_store.delete_all_transcripts()
    yield transcript_store
    transcript_store.delete_all_transcripts()


def _now():
    return datetime.datetime.now().astimezone()


def _save(store, name, duration, created=None, segments=None, **extra):
    segs = segments or [{"start": 0.0, "end": 2.0, "text": "Hello there.", "speaker": None}]
    res = dict({"path": os.path.join(r"C:\nowhere", name), "name": name, "duration": duration,
                "segments": segs, "speakers": True, "timestamps": True, "model": "small",
                "language": "en", "language_setting": "auto"}, **extra)
    return os.path.basename(str(store.save_transcript(res, created=created or _now())))


def _interview(store, **extra):
    segs = [{"start": 0.0, "end": 3.5, "text": "Thanks for coming in, Mariam.", "speaker": 0},
            {"start": 4.0, "end": 8.0, "text": "Easy, thanks. Traffic was kind today.", "speaker": 1},
            {"start": 9.0, "end": 13.0, "text": "Good. Tell me about the project.", "speaker": 0}]
    return _save(store, "interview_mariam.m4a", 24 * 60 + 20, segments=segs, **extra)


@pytest.fixture
def build(qapp):
    made = []

    def _build(tier="free", file_jobs=True, downloads=False, **cfg):
        from ui.pages import audio_to_text
        app = make_stub_app(tier)
        app.cfg.update(cfg)
        if file_jobs:
            app.file_jobs = FakeFileJobs()
        if downloads:
            app.downloads = downloads if isinstance(downloads, FakeDownloads) else FakeDownloads()
        page = audio_to_text.create(app)
        page.resize(1048, 820)
        page.show()
        QApplication.processEvents()
        made.append(page)
        return page, app

    yield _build
    for p in made:
        p.hide()
        p.deleteLater()
    QApplication.processEvents()


def _texts(widget):
    out = []
    for w in widget.findChildren(QLabel):
        if w.isVisibleTo(widget) and w.text():
            out.append(w.text())
    for b in widget.findChildren(QAbstractButton):
        if b.isVisibleTo(widget) and b.text():
            out.append(b.text())
    return out


# ── building ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["free", "pro", "guest"])
def test_opens_in_the_shell_for_every_plan(qapp, store, tier):
    from ui import shell
    from ui.pages.audio_to_text import AudioToTextPage
    _save(store, "board_call.mp3", 6000)
    app = make_stub_app(tier)                       # no file_jobs / downloads yet (before Wave 4)
    win = shell.MainWindow(app)
    win.show()
    try:
        page = win.open("audio_to_text")
        QApplication.processEvents()
        assert isinstance(page, AudioToTextPage), "the page built (no error card)"
        assert not isinstance(page, shell.ErrorPage)
        assert page.header.title_label.text() == "Convert audio to text"
        assert [r.title() for r in page.files.rows] == ["board_call.mp3"]
        page.add_files([r"C:\rec\a.m4a"])
        assert page.notice.message() == "Audio to text isn't ready yet. Try again in a moment."
        assert not [c for c in app.calls if c[0] == "_pro_upsell"]
    finally:
        win.hide()
        win.deleteLater()
        QApplication.processEvents()


def test_same_page_for_free_and_pro_with_no_limit_wording(build, store):
    _save(store, "board_call.mp3", 6000)
    free, free_app = build("free")
    pro, pro_app = build("pro")
    texts = _texts(free)
    assert sorted(texts) == sorted(_texts(pro))
    for t in texts:
        low = t.lower()
        assert not any(b in low for b in ("limit", "hour", "upgrade", "minutes left", "unlock")), t
        assert t != "PRO", "calm Pro: no Pro tag or banner, only the chip on the one row in the Accuracy list"
    assert "MP3, WAV, M4A, FLAC or video · any length" in texts
    assert "On this computer. Nothing is uploaded." in _texts(free)
    # The one Pro row is Gusan Cloud's best profile (its server chooses the model), wearing its CLOUD
    # and PRO chips (it says what a row IS, so it reads the same for Free and for Pro).
    for page in (free, pro):
        box = page.model_combo
        assert [box.itemText(i) for i in box.row_indexes() if "pro" in box.row_tags(i)] == [
            "Best accuracy · Cloud · Pro"]
        assert box.headings() == ["On this computer", "Gusan Cloud"]
    assert not [c for c in free_app.calls + pro_app.calls if c[0] == "_pro_upsell"], "nothing opens by itself"


def test_every_control_has_a_name(build, store):
    _interview(store)
    page, app = build("free", downloads=True)
    app.file_jobs.cur = dict(RUNNING)
    page._sync_job_card()
    page.open_transcript(page.files.rows[0].tid)
    for w in page.findChildren(QAbstractButton) + page.findChildren(QComboBox):
        assert (w.text() if isinstance(w, QAbstractButton) else "") or w.accessibleName(), w
    for row in page.files.rows:
        assert row.open_button.accessibleName() == "Open interview_mariam.m4a"
        assert row.more_button.accessibleName() == "More for interview_mariam.m4a"
        assert row.tile.accessibleName() == "Voice recording"
    assert page.model_combo.accessibleName() == "Accuracy"
    assert page.lang_combo.accessibleName() == "Language"
    assert page.speakers_toggle.accessibleName() == "Speaker labels"
    assert page.drop_zone.accessibleName() == "Drop audio or video here"
    assert page.transcript.back_link.accessibleName() == "Back to Audio to text"
    assert page.transcript.browser.accessibleName() == "Transcript"
    # keyboard reachable
    for w in (page.drop_zone.choose_button, page.transcript.copy_button, page.speakers_toggle,
              page.files.rows[0].open_button, page.files.rows[0].more_button):
        assert w.focusPolicy() & Qt.TabFocus


# ── the list ─────────────────────────────────────────────────────────────────
def test_your_files_newest_first_with_one_emoji_by_type(build, store):
    now = _now()
    _save(store, "podcast_ep12.mp3", 47 * 60, now - datetime.timedelta(days=1, hours=2))
    _save(store, "lecture_week3.mp4", 52 * 60, now - datetime.timedelta(days=1))
    _save(store, "board_call.mp3", 100 * 60, now - datetime.timedelta(minutes=5))
    page, _ = build()
    rows = page.files.rows
    assert [r.title() for r in rows] == ["board_call.mp3", "lecture_week3.mp4", "podcast_ep12.mp3"]
    assert [r.tile.accessibleName() for r in rows] == ["Call", "Video", "Voice recording"]
    assert [r.tile.text() for r in rows] == ["\U0001F4DE", "\U0001F3AC", "\U0001F3A4"]
    assert "1 h 40 min" in rows[0].meta.text() and "Today" in rows[0].meta.text()
    assert "Yesterday" in rows[1].meta.text() or "Today" in rows[1].meta.text()
    assert rows[-1].property("last") is True and rows[0].property("last") is False
    assert not page.files.empty.isVisibleTo(page)


def test_empty_list(build, store):
    page, _ = build()
    assert page.files.rows == []
    assert page.files.empty.isVisibleTo(page)
    assert page.files.empty.text() == "Files you convert show up here."


def test_row_menu_rename_show_in_folder_and_delete(build, store, tmp_path, monkeypatch):
    import ui.pages.audio_to_text as a2t
    word = tmp_path / "board transcript.docx"
    word.write_bytes(b"x")
    tid = _save(store, "board_call.mp3", 6000)
    store.update_meta(tid, word_path=str(word))
    keep = _save(store, "lecture_week3.mp4", 60)
    page, _ = build()
    row = page.files.row(tid)
    assert [a.text() for a in row.build_menu().actions() if a.text()] == \
        ["Rename", "Open Word file", "Show in folder", "Delete"]
    assert [a.text() for a in page.files.row(keep).build_menu().actions() if a.text()] == \
        ["Rename", "Show in folder", "Delete"]

    opened = []
    monkeypatch.setattr(a2t, "open_path", lambda p: opened.append(p) or True)
    row.show_in_folder()
    row.open_word()
    assert opened == [str(tmp_path), str(word)]

    # Rename inline: Enter saves through transcript_store, then the list refreshes.
    row.start_rename()
    assert row.is_renaming() and row.edit.isVisibleTo(row)
    row.edit.setText("  Board   call, March ")
    row.edit.editingFinished.emit()
    assert store.load_transcript(tid)["title"] == "Board call, March"
    assert page.files.row(tid).title() == "Board call, March"
    # Esc cancels.
    row = page.files.row(tid)
    row.start_rename()
    row.edit.setText("nope")
    row.cancel_rename()
    assert store.load_transcript(tid)["title"] == "Board call, March"

    # Delete asks inline, then removes only the transcript.
    row = page.files.row(tid)
    row.ask_delete()
    assert row.meta.text() == "Delete this transcript? The audio file stays."
    assert row.delete_button.isVisibleTo(row) and not row.open_button.isVisibleTo(row)
    row.keep_button.click()
    assert row.open_button.isVisibleTo(row) and store.load_transcript(tid) is not None
    row.ask_delete()
    row.delete_button.click()
    assert store.load_transcript(tid) is None
    assert word.exists()
    assert [r.tid for r in page.files.rows] == [keep]


def test_a_list_refresh_waits_for_a_rename(build, store):
    tid = _save(store, "board_call.mp3", 6000)
    page, _ = build()
    row = page.files.row(tid)
    row.start_rename()
    _save(store, "new.mp3", 60)
    page.refresh_list()
    assert page.files.rows[0] is row and row.is_renaming()
    row.cancel_rename()
    assert [r.title() for r in page.files.rows][0] == "new.mp3"


def test_show_more(build, store, monkeypatch):
    import ui.pages.audio_to_text as a2t
    monkeypatch.setattr(a2t, "LIST_STEP", 2)
    for i in range(5):
        _save(store, "f%d.mp3" % i, 60, _now() - datetime.timedelta(minutes=i))
    page, _ = build()
    page.files._limit = 2
    page.files.set_rows(page._summaries)
    assert len(page.files.rows) == 2 and page.files.more_button.isVisibleTo(page)
    page.files.more_button.click()
    assert len(page.files.rows) == 4
    page.files.more_button.click()
    assert len(page.files.rows) == 5 and not page.files.more_button.isVisibleTo(page)


# ── the rail ─────────────────────────────────────────────────────────────────
def test_rail_reads_cfg_and_writes_each_change_through_set_setting(build, store):
    page, app = build(file_model="small", file_language="fr", file_speakers=False,
                      file_timestamps=True, file_save_word=False)
    assert page.model_combo.currentData() == "small"
    assert page.model_combo.plain_text() == "Whisper Small"
    assert page.model_combo.currentText() == "Whisper Small · Local"
    assert page.model_combo.current_tags() == ("local",)
    assert page.model_line.text() == "Whisper Small · Local"
    assert page.lang_combo.currentText() == "French"
    assert not page.speakers_toggle.isChecked() and page.timestamps_toggle.isChecked()
    assert not page.word_toggle.isChecked()
    assert not [c for c in app.calls if c[0] == "set_setting"], "reading never writes"

    page.model_combo.setCurrentIndex(page.model_combo.findData("large-v3-turbo"))
    page.lang_combo.setCurrentIndex(page.lang_combo.findData("multi"))
    page.speakers_toggle.click()
    page.timestamps_toggle.click()
    page.word_toggle.click()
    sets = [c[1:] for c in app.calls if c[0] == "set_setting"]
    assert sets == [("file_model", "large-v3-turbo"), ("file_language", "multi"),
                    ("file_speakers", True), ("file_timestamps", False), ("file_save_word", True)]
    assert page.model_line.text() == "Whisper Large v3 Turbo · Local"

    # Somebody else changed it: the page follows.
    app.cfg["file_language"] = "de"
    page.on_setting_changed("file_language")
    assert page.lang_combo.currentText() == "German"


def test_language_list_and_model_list(build, store):
    page, _ = build()
    langs = [page.lang_combo.itemText(i) for i in range(page.lang_combo.count())]
    assert langs == ["Detect automatically", "Mixed languages", "English", "Arabic", "Armenian",
                     "French", "German", "Russian", "Spanish"]
    box = page.model_combo
    names = [box.itemData(i, Qt.UserRole + 1) for i in box.row_indexes()]
    assert names == ["Automatic", "Whisper Tiny", "Whisper Base", "Whisper Small", "Whisper Medium",
                     "Whisper Large v3 Turbo", "Whisper Large v3", "Best accuracy"]      # real names, no plain words
    assert box.itemText(box.findData("large-v3-turbo")) == "Whisper Large v3 Turbo · Local"
    assert box.itemText(box.findData("cloud")) == "Best accuracy · Cloud · Pro"
    assert box.row_note(box.findData("medium")) == "Accurate"
    assert box.row_note(box.findData("cloud")) == "Most accurate · Gusan chooses the model"
    assert box.headings() == ["On this computer", "Gusan Cloud"]
    box.showPopup()
    QApplication.processEvents()
    widest = max(box.itemDelegate().row_width(box.model().index(i, 0), box.font()) for i in range(box.count()))
    assert box.view().minimumWidth() >= widest, "the list fits its longest row (name, chips and padding)"
    box.hidePopup()


def test_a_refused_setting_snaps_back(build, store):
    page, app = build(file_timestamps=True)

    def refuse(key, value):
        app.calls.append(("set_setting", key, value))
        return False
    app.set_setting = refuse
    page.timestamps_toggle.click()
    assert ("set_setting", "file_timestamps", False) in app.calls
    assert page.timestamps_toggle.isChecked()


def test_automatic_names_the_model_it_would_use(build, store):
    page, app = build(file_model="auto")
    assert page.model_combo.currentData() == "auto"
    assert not page.model_line.isVisibleTo(page), "unknown without the download manager"
    page, app = build(file_model="auto", downloads=FakeDownloads(ready=["base", "small"]))
    assert page.model_line.text() == "Whisper Small · Local"
    app.downloads._ready = ["tiny"]
    app.downloads.state_changed.emit("small", "missing")
    assert page.model_line.text() == "Whisper Tiny · Local"
    page, app = build(file_model="auto",
                      downloads=FakeDownloads(ready=["small", "large-v3-turbo"], gpu_state="ready"))
    assert page.model_line.text() == "Whisper Large v3 Turbo · Local"


# ── cloud models in Accuracy ─────────────────────────────────────────────────
def _saving(app):
    """set_setting that also saves to cfg (as the real controller does)."""
    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        app.cfg[key] = value
        return True
    app.set_setting = set_setting


def _sets(app):
    return [c[1:] for c in app.calls if c[0] == "set_setting"]


def _item_texts(page):
    box = page.model_combo
    return {box.itemData(i): box.itemText(i) for i in box.row_indexes()}


def _foot(page):
    """(footer text, its icon's name) - the icon compared with the real pixmaps."""
    from ui.components import icons
    img = page.rail_foot_icon.pixmap().toImage()
    names = [n for n in ("shield", "cloud", "key") if icons.pixmap(n, 15, "muted").toImage() == img]
    return page.rail_foot.text(), (names[0] if len(names) == 1 else names)


def test_free_pick_of_gusan_cloud_opens_the_pro_window_and_saves_nothing(build, store):
    page, app = build("free", file_model="small")
    page.model_combo.setCurrentIndex(page.model_combo.findData("cloud"))
    assert app.calls.count(("_pro_upsell", "Fast cloud transcription")) == 1
    assert _sets(app) == [], "nothing saved"
    assert page.model_combo.currentData() == "small", "the combo snapped back"
    assert page.model_line.text() == "Whisper Small · Local"
    assert _foot(page) == ("On this computer. Nothing is uploaded.", "shield")


def test_pro_pick_of_gusan_cloud_is_saved_once_and_named(build, store):
    page, app = build("pro", file_model="small")
    assert _item_texts(page)["cloud"] == "Best accuracy · Cloud · Pro"
    page.model_combo.setCurrentIndex(page.model_combo.findData("cloud"))
    assert _sets(app) == [("file_model", "cloud")]
    assert not [c for c in app.calls if c[0] == "_pro_upsell"]
    assert page.model_combo.plain_text() == "Best accuracy", "the profile's name, never the service's"
    assert page.model_combo.closed_text() == "Best accuracy · Cloud · Pro"
    assert page.model_line.text() == "Best accuracy · Cloud", "what Gusan Cloud is asked for, never a Whisper name"
    assert _foot(page) == ("Sent to Gusan Cloud to transcribe.", "cloud")
    page.model_combo.setCurrentIndex(page.model_combo.findData("auto"))
    assert _sets(app)[-1] == ("file_model", "auto")
    assert _foot(page) == ("On this computer. Nothing is uploaded.", "shield")

    # Saved: a file asks for the best profile whatever the old provider setting says (the server chooses)
    page, app = build("pro", file_model="cloud", managed_provider="mistral")
    assert page.model_combo.currentData() == "cloud"
    assert _item_texts(page)["cloud"] == "Best accuracy · Cloud · Pro"
    assert page.model_line.text() == "Best accuracy · Cloud"
    app.cfg["managed_provider"] = "gemini"
    page.on_setting_changed("managed_provider")
    assert _item_texts(page)["cloud"] == "Best accuracy · Cloud · Pro"
    assert page.model_combo.currentData() == "cloud" and page.model_line.text() == "Best accuracy · Cloud"
    assert _sets(app) == [], "following a setting never writes"


def test_own_key_items_appear_once_the_key_is_saved(build, store):
    page, app = build("free", file_model="small")
    _saving(app)
    assert not {"own:google", "own:mistral"} & set(_item_texts(page))
    app.cfg["google_api_key"] = "test-google-key"
    page.on_setting_changed("google_api_key")
    items = _item_texts(page)
    assert items["own:google"] == "Gemini 2.5 Flash · your Google key · Cloud", "never marked PRO"
    assert page.model_combo.row_tags(page.model_combo.findData("own:google")) == ("cloud",)
    assert "own:mistral" not in items
    page.model_combo.setCurrentIndex(page.model_combo.findData("own:google"))
    assert _sets(app) == [("file_model", "own:google")]
    assert not [c for c in app.calls if c[0] == "_pro_upsell"]
    assert page.model_line.text() == "Gemini 2.5 Flash · your Google key · Cloud"
    assert _foot(page) == ("Sent to Google Gemini with your key.", "key")

    app.cfg["mistral_api_key"] = "test-mistral-key"
    page.on_setting_changed("mistral_api_key")
    assert _item_texts(page)["own:mistral"] == "Voxtral Mini · your Mistral key · Cloud"
    assert page.model_combo.currentData() == "own:google", "the pick stays through a refill"
    page.model_combo.setCurrentIndex(page.model_combo.findData("own:mistral"))
    assert _sets(app)[-1] == ("file_model", "own:mistral")
    assert page.model_line.text() == "Voxtral Mini · your Mistral key · Cloud"
    assert _foot(page) == ("Sent to Mistral with your key.", "key")

    # The key removed (api_keys also resets file_model): the item goes, Automatic shows.
    app.cfg["mistral_api_key"] = ""
    page.on_setting_changed("mistral_api_key")
    assert "own:mistral" not in _item_texts(page)
    assert page.model_combo.currentData() == "auto"
    assert _foot(page) == ("On this computer. Nothing is uploaded.", "shield")


def test_privacy_mode_greys_out_the_cloud_items_and_says_why(build, store):
    page, app = build("pro", file_model="cloud", privacy_mode=True, google_api_key="test-google-key")
    box = page.model_combo
    hint = page.model_row.hint_label
    assert box.currentData() == "auto", "a cloud pick runs on this computer in Privacy mode"
    for value in ("cloud", "own:google"):
        assert not box.choice_enabled(box.findData(value)), value
    assert box.choice_enabled(box.findData("small")) and box.choice_enabled(box.findData("auto"))
    assert hint.isVisibleTo(page) and hint.text() == "Privacy mode keeps it on this computer."
    assert _foot(page) == ("On this computer. Nothing is uploaded.", "shield")
    box.setCurrentIndex(box.findData("cloud"))                 # can't be clicked; even so
    assert box.currentData() == "auto" and _sets(app) == []
    assert not [c for c in app.calls if c[0] == "_pro_upsell"]

    app.cfg["privacy_mode"] = False
    page.on_setting_changed("privacy_mode")
    assert box.choice_enabled(box.findData("cloud")) and box.choice_enabled(box.findData("own:google"))
    assert not hint.isVisibleTo(page)
    assert box.currentData() == "cloud", "the saved pick runs again"
    assert _foot(page) == ("Sent to Gusan Cloud to transcribe.", "cloud")


def test_a_saved_cloud_pick_that_cannot_run_shows_automatic(build, store):
    page, app = build("free", file_model="cloud", downloads=FakeDownloads(ready=["base", "small"]))
    assert page.model_combo.currentData() == "auto"
    assert page.model_line.text() == "Whisper Small · Local"
    assert _foot(page) == ("On this computer. Nothing is uploaded.", "shield")
    page, app = build("pro", file_model="own:google")              # no key saved
    assert page.model_combo.currentData() == "auto"
    assert _sets(app) == [], "showing Automatic never writes"


def test_refresh_account_follows_the_plan(build, store):
    page, app = build("free", file_model="cloud")
    assert _item_texts(page)["cloud"] == "Best accuracy · Cloud · Pro" and page.model_combo.currentData() == "auto"
    app.set_tier("pro")
    page.refresh_account()
    assert _item_texts(page)["cloud"] == "Best accuracy · Cloud · Pro"      # the row is what it is, whoever looks
    assert page.model_combo.currentData() == "cloud"
    assert _foot(page) == ("Sent to Gusan Cloud to transcribe.", "cloud")
    app.set_tier("free")                                         # Pro ended
    page.refresh_account()
    assert _item_texts(page)["cloud"] == "Best accuracy · Cloud · Pro"
    assert page.model_combo.currentData() == "auto"
    assert _foot(page) == ("On this computer. Nothing is uploaded.", "shield")
    assert _sets(app) == [] and not [c for c in app.calls if c[0] == "_pro_upsell"]


# ── adding files ─────────────────────────────────────────────────────────────
def _mime(*paths):
    m = QMimeData()
    m.setUrls([QUrl.fromLocalFile(p) for p in paths])
    return m


def test_choose_a_file_and_drops_queue_audio_on_file_jobs(build, store):
    page, app = build()
    page.ask_open_paths = lambda: [r"C:\rec\a.m4a", r"C:\rec\b.mp4"]
    page.drop_zone.choose_button.click()
    assert app.file_jobs.calls[-1] == ("enqueue", [r"C:\rec\a.m4a", r"C:\rec\b.mp4"], None)

    # The events don't own their QMimeData: keep each one referenced.
    text_only = _mime(r"C:\x\notes.txt")
    enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, text_only, Qt.LeftButton, Qt.NoModifier)
    page.dragEnterEvent(enter)
    assert not enter.isAccepted() and not page.drop_zone.is_active()
    mixed = _mime(r"C:\x\call.mp3", r"C:\x\notes.txt")
    enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mixed, Qt.LeftButton, Qt.NoModifier)
    page.dragEnterEvent(enter)
    assert enter.isAccepted() and page.drop_zone.is_active()
    three = _mime(r"C:\x\call.mp3", r"C:\x\notes.txt", r"C:\x\clip.MOV")
    drop = QDropEvent(QPointF(10, 10), Qt.CopyAction, three, Qt.LeftButton, Qt.NoModifier)
    page.dropEvent(drop)
    assert not page.drop_zone.is_active()
    paths = [os.path.normpath(p) for p in app.file_jobs.calls[-1][1]]
    assert paths == [os.path.normpath(r"C:\x\call.mp3"), os.path.normpath(r"C:\x\clip.MOV")]

    drop = QDropEvent(QPointF(10, 10), Qt.CopyAction, text_only, Qt.LeftButton, Qt.NoModifier)
    page.dropEvent(drop)
    assert page.notice.message() == "Drop an audio or video file."
    assert len([c for c in app.file_jobs.calls if c[0] == "enqueue"]) == 2


def test_a_refusal_shows_inline(build, store):
    import file_jobs
    page, app = build()
    app.file_jobs.refuse = file_jobs.MSG_RECORDING
    page.add_files([r"C:\rec\a.m4a"])
    assert page.notice.message() == file_jobs.MSG_RECORDING and page.notice.tone == "info"
    page.notice.close_button.click()
    assert page.notice.message() == ""


def test_on_open_with_paths_or_a_transcript(build, store):
    tid = _interview(store)
    page, app = build()
    page.on_open(paths=r"C:\rec\a.m4a")
    assert app.file_jobs.calls[-1][1] == [r"C:\rec\a.m4a"]
    assert page.current_view() == "library"
    page.on_open(transcript_id=tid)
    assert page.current_view() == "transcript"
    assert page.transcript.title_label.text() == "interview_mariam.m4a"
    page.on_open()
    assert page.current_view() == "library"
    page.on_open(transcript_id="20000101_000000")             # gone
    assert page.current_view() == "library"
    assert page.notice.message() == "This transcript couldn't be opened."


# ── the running job ──────────────────────────────────────────────────────────
def test_running_job_card_follows_file_jobs_and_cancels(build, store):
    page, app = build()
    assert not page.job_card.isVisibleTo(page)
    fj = app.file_jobs
    fj.cur = dict(RUNNING)
    fj.started.emit(fj.cur)
    card = page.job_card
    assert card.isVisibleTo(page) and card.state == "running"
    assert card.meta_texts() == ["interview_mariam.m4a", "Ready in about 6 min", "Transcribing · 42%",
                                 "24 min", "Whisper Small · Local"]
    assert card.bar.value() == 42 and card.tile.accessibleName() == "Voice recording"
    fj.cur.update(pct=60)
    fj.queue = [{"id": 8}, {"id": 9}]
    fj.progress.emit(60, "Transcribing", "Ready in about 4 min")
    assert "Transcribing · 60%" in card.meta_texts() and "2 more waiting" in card.meta_texts()
    card.cancel_button.click()
    assert ("cancel", 7) in fj.calls
    fj.cur.update(stage="Stopping")
    fj.progress.emit(60, "Stopping", "")
    assert "Stopping" in card.meta_texts() and not card.cancel_button.isEnabled()
    item = fj.cur
    fj.cur, fj.queue = None, []
    fj.cancelled.emit(item)
    assert not card.isVisibleTo(page)


def test_the_model_is_named_once_while_it_downloads(build, store):
    page, app = build()
    fj = app.file_jobs
    fj.cur = dict(RUNNING, stage="Downloading Whisper Small", pct=12, eta="")
    fj.started.emit(fj.cur)
    texts = page.job_card.meta_texts()
    assert "Downloading Whisper Small · 12%" in texts
    assert "Whisper Small" not in texts, "not repeated after the stage"
    fj.cur.update(stage="Transcribing", pct=30)
    fj.progress.emit(30, "Transcribing", "")
    assert "Whisper Small · Local" in page.job_card.meta_texts()


def test_a_failure_stays_readable_when_the_next_file_starts(build, store):
    import file_jobs
    page, app = build()
    fj = app.file_jobs
    bad = dict(RUNNING, state="failed", name="board_call.mp3", kind="call")
    fj.failed.emit(bad, file_jobs.MSG_UNREADABLE)
    assert page.job_card.state == "failed"
    fj.cur = dict(RUNNING, id=8)
    fj.started.emit(fj.cur)
    assert page.job_card.state == "running"
    assert page.notice.message() == "board_call.mp3: " + file_jobs.MSG_UNREADABLE
    assert page.notice.tone == "error"


def test_row_meta_ends_in_an_ellipsis_when_narrow(build, store):
    _save(store, "board_call.mp3", 6000)
    page, _ = build()
    meta = page.files.rows[0].meta
    assert meta.text().startswith("1 h 40 min · Today ")
    assert meta.minimumSizeHint().width() < meta.sizeHint().width(), "it can shrink without pushing the rail"
    meta.resize(60, meta.height())
    meta.repaint()
    assert meta.toolTip() == meta.text(), "the whole line is in the tooltip when cut"


def test_back_from_a_transcript_refreshes_the_list(build, store, tmp_path):
    tid = _interview(store)
    page, app = build()
    assert [a.text() for a in page.files.row(tid).build_menu().actions() if a.text()] == \
        ["Rename", "Show in folder", "Delete"]
    page.open_transcript(tid)
    docx = tmp_path / "interview.docx"
    page.transcript.ask_save_path = lambda *a: str(docx)
    page.transcript.word_button.click()
    page.transcript.back_link.click()
    assert page.current_view() == "library"
    assert "Open Word file" in [a.text() for a in page.files.row(tid).build_menu().actions()]


def test_transcript_header_stacks_its_actions_when_narrow(build, store):
    tid = _interview(store)
    page, _ = build()
    page.open_transcript(tid)
    view = page.transcript
    assert not view.is_header_stacked(), "one line at the usual width"
    page.resize(728, 600)
    QApplication.processEvents()
    assert view.is_header_stacked()
    for b in (view.copy_button, view.export_button, view.word_button):
        assert b.width() >= b.sizeHint().width(), b.text()
    assert view.word_button.mapTo(view, view.word_button.rect().topRight()).x() <= view.width()
    page.resize(1048, 820)
    QApplication.processEvents()
    assert not view.is_header_stacked()


def test_finished_job_says_ready_and_opens_the_transcript(build, store):
    page, app = build()
    fj = app.file_jobs
    fj.cur = dict(RUNNING)
    fj.started.emit(fj.cur)
    tid = _interview(store)
    fj.cur = None
    fj.finished.emit(dict(RUNNING, state="done", pct=100, transcript_id=tid))
    card = page.job_card
    assert card.state == "done" and "Ready" in card.meta_texts()
    assert card.open_button.isVisibleTo(page)
    assert [r.tid for r in page.files.rows] == [tid], "the list refreshed"
    card.open_button.click()
    assert page.current_view() == "transcript"
    assert not card.isVisibleTo(page)
    view = page.transcript
    assert view.title_label.text() == "interview_mariam.m4a"
    texts = view.meta_texts()
    assert texts[:4] == ["24 min 20 s", "2 speakers", "English", "Whisper Small · Local"]
    assert texts[4].startswith("Today ")


def test_failed_job_try_again_and_dismiss(build, store):
    import file_jobs
    page, app = build()
    fj = app.file_jobs
    bad = dict(RUNNING, state="failed", name="board_call.mp3", kind="call", path=r"C:\rec\board_call.mp3")
    fj.failed.emit(bad, file_jobs.MSG_UNREADABLE)
    card = page.job_card
    assert card.state == "failed" and card.error.text() == file_jobs.MSG_UNREADABLE
    assert card.tile.accessibleName() == "Call"
    card.retry_button.click()
    assert fj.calls[-1] == ("enqueue", [r"C:\rec\board_call.mp3"], bad["opts"])
    fj.failed.emit(bad, "")
    assert card.error.text() == file_jobs.MSG_ERROR
    card.close_button.click()
    assert not card.isVisibleTo(page)


def test_with_the_real_file_job_controller(build, store, tmp_path):
    """The real FileJobController (fake runner): its signals drive the card and
    the finished transcript lands in the list, ready to open."""
    import time
    import file_jobs
    src = tmp_path / "interview_mariam.m4a"
    src.write_bytes(b"")
    page, app = build(file_jobs=False)
    app.recorder = object()
    app._file_job_running = False
    app.is_rec = app._busy = False

    def runner(path, opts, recorder, on_progress, should_cancel, **kw):
        kw["on_info"]({"duration": 1460.0, "model": "small"})
        on_progress(42, file_jobs.STAGE_TRANSCRIBING, 360.0)
        time.sleep(0.2)
        return {"path": path, "name": os.path.basename(path), "duration": 1460.0,
                "segments": [{"start": 0.0, "end": 2.0, "text": "Hello.", "speaker": 0}],
                "paragraphs": [{"start": 0.0, "speaker": 0, "text": "Hello."}],
                "timestamps": True, "speakers": True, "speaker_count": 1, "model": "small",
                "language": "en", "language_setting": "auto", "decode_seconds": 1.0}

    app.file_jobs = file_jobs.FileJobController(app, runner=runner)
    try:
        page.add_files([str(src)])
        assert page.job_card.state == "running"
        end = time.monotonic() + 10
        while page.job_card.state != "done" and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)
        assert page.job_card.state == "done"
        assert "Whisper Small · Local" in page.job_card.meta_texts()
        assert [r.title() for r in page.files.rows] == ["interview_mariam.m4a"]
        page.job_card.open_button.click()
        assert page.current_view() == "transcript"
        assert "Hello." in page.transcript.browser.toPlainText()
    finally:
        app.file_jobs.shutdown()
        app.file_jobs.deleteLater()


# ── one transcript open ──────────────────────────────────────────────────────
def test_transcript_turns_copy_export_and_save_word(build, store, tmp_path):
    tid = _interview(store)
    page, app = build()
    page.open_transcript(tid)
    view = page.transcript
    text = view.browser.toPlainText()
    assert "00:04\tSpeaker 2\tEasy, thanks. Traffic was kind today." in text
    assert "00:00\tSpeaker 1\tThanks for coming in, Mariam." in text

    view.copy_button.click()
    assert QGuiApplication.clipboard().text() == store.plain_text(store.load_transcript(tid))
    assert view.copy_button.text() == "Copied"
    assert ("track_saved", "clipboard") in app.file_jobs.calls

    asked = []
    out = tmp_path / "interview.txt"
    view.ask_save_path = lambda caption, suggested, filt: asked.append((caption, suggested, filt)) or str(out)
    assert [a.text() for a in view.export_menu.actions()] == ["Text (.txt)", "Markdown (.md)",
                                                               "Subtitles (.srt)"]
    view.export_actions["txt"].trigger()
    assert out.read_text(encoding="utf-8").startswith("interview_mariam\n")
    assert os.path.basename(asked[0][1]) == "interview_mariam transcript.txt"
    assert ("track_saved", "txt") in app.file_jobs.calls
    assert view.saved_chip.isVisibleTo(view) and not view.error_chip.isVisibleTo(view)

    srt = tmp_path / "interview.srt"
    view.ask_save_path = lambda *a: str(srt)
    view.export_actions["srt"].trigger()
    assert "00:00:04,000 --> 00:00:08,000" in srt.read_text(encoding="utf-8")

    docx = tmp_path / "interview.docx"
    view.ask_save_path = lambda *a: str(docx)
    view.word_button.click()
    assert docx.is_file() and store.load_transcript(tid)["word_path"] == str(docx)
    assert ("track_saved", "docx") in app.file_jobs.calls

    view.ask_save_path = lambda *a: str(tmp_path / "missing" / "dir" / "x.md")
    view.export_actions["md"].trigger()
    assert view.error_chip.isVisibleTo(view) and not view.saved_chip.isVisibleTo(view)

    view.ask_save_path = lambda *a: ""                      # cancelled dialog: nothing
    n = len(app.file_jobs.calls)
    view.export_actions["md"].trigger()
    assert len(app.file_jobs.calls) == n


def test_transcript_without_timestamps_or_speakers(build, store):
    tid = _save(store, "memo.m4a", 30, timestamps=False,
                segments=[{"start": 0.0, "end": 2.0, "text": "Buy milk.", "speaker": None}])
    page, _ = build()
    page.open_transcript(tid)
    assert page.transcript.browser.toPlainText() == "Buy milk."
    assert page.transcript.meta_texts()[:2] == ["30 s", "English"]


def test_long_transcript_is_laid_out_in_chunks(build, store):
    segs = [{"start": i * 10.0, "end": i * 10.0 + 8, "text": "Line %d." % i, "speaker": i % 2}
            for i in range(700)]
    tid = _save(store, "board_call.mp3", 7000, segments=segs)
    page, _ = build()
    page.open_transcript(tid)
    doc = page.transcript.browser.document()
    first = doc.blockCount()
    assert 0 < first < 700, "the first chunk shows at once"
    for _ in range(50):
        QApplication.processEvents()
        if doc.blockCount() >= 700:
            break
    assert doc.blockCount() == 700
    assert page.transcript.browser.toPlainText().endswith("Line 699.")


def test_back_link_and_escape_go_back(build, store):
    from PySide6.QtTest import QTest
    tid = _interview(store)
    page, _ = build()
    page.open_transcript(tid)
    page.transcript.back_link.click()
    assert page.current_view() == "library"
    page.open_transcript(tid)
    QTest.keyClick(page.transcript, Qt.Key_Escape)
    assert page.current_view() == "library"


# ── layout ───────────────────────────────────────────────────────────────────
def test_narrow_window_keeps_everything_reachable(qapp, store, monkeypatch):
    import ui.winfit
    from ui import shell
    # The offscreen screen is 800 px wide: don't let fit-to-screen shrink the window below its minimum.
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    _save(store, "board_call.mp3", 6000)
    app = make_stub_app("free")
    app.file_jobs = FakeFileJobs()
    app.file_jobs.cur = dict(RUNNING)
    win = shell.MainWindow(app)
    win.resize(*shell.MIN_SIZE)
    win.show()
    try:
        page = win.open("audio_to_text")
        QApplication.processEvents()
        content = page.library.widget()
        assert content.width() <= page.library.viewport().width(), "no sideways overflow"
        assert not page.drop_zone.art_visible(), "the circles step aside when narrow"
        assert page.rail.geometry().right() <= content.width()
        assert page.job_card.state == "running"
    finally:
        win.hide()
        win.deleteLater()
        QApplication.processEvents()


# ── words ────────────────────────────────────────────────────────────────────
def test_short_duration_and_when_text():
    from ui.pages.transcript_view import short_duration, when_text
    assert short_duration(6000) == "1 h 40 min"
    assert short_duration(3600) == "1 h"
    assert short_duration(52 * 60 + 10) == "52 min"
    assert short_duration(45) == "45 s"
    assert short_duration(None) == "" and short_duration(0) == ""
    now = datetime.datetime(2026, 10, 7, 15, 0)
    assert when_text(datetime.datetime(2026, 10, 7, 9, 12), now) == "Today 09:12"
    assert when_text(datetime.datetime(2026, 10, 6, 17, 20), now) == "Yesterday 17:20"
    assert when_text(datetime.datetime(2026, 9, 29, 8, 0), now) == "Tue 29 Sep"
    assert when_text(datetime.datetime(2025, 3, 2, 8, 0), now) == "2 Mar 2025"
    assert when_text("not a date", now) == "" and when_text("", now) == ""
    aware = _now().replace(hour=11, minute=20)
    assert when_text(aware.isoformat()) == "Today 11:20"
