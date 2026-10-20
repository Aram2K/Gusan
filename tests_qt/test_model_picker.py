"""The model picker (Gusan 2.0.1 picker fixes): ui/components/model_combo.py's
opt-in row marks, the two mini popups (ui/components/mini_prompt.py,
ui/privacy_prompt.py) and the Smart Actions "Transcribes with" list as a
person uses it (a click on a row)."""
import gc
import time

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QWidget

from tests_qt.conftest import make_stub_app

LONG = "Most accurate · Whisper Large v3 Turbo"


@pytest.fixture
def host(qapp):
    """A themed window with room for a combo; deleted after the test."""
    from ui import theme
    made = []

    def build(x=40, y=40, w=700, h=420):
        win = QWidget()
        theme.apply(win)
        win.setGeometry(x, y, w, h)
        QVBoxLayout(win)
        win.show()
        QApplication.processEvents()
        made.append(win)
        return win
    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def _picker(win, width=200, marks=True):
    from ui.components.model_combo import PlainNameCombo, combo
    box = combo(PlainNameCombo(win), "Transcribes with", width=width)
    if marks:
        box.enable_row_marks()
    box.add_choice("Same as Dictation", "", plain="Same as Dictation")
    box.add_choice("Fast · Whisper Base", "base", plain="Fast")
    box.add_choice(LONG, "large-v3-turbo", plain="Most accurate")
    box.add_choice("Gusan Cloud · Gemini 2.5 Flash", "cloud", plain="Gusan Cloud")
    win.layout().addWidget(box, 0, Qt.AlignLeft)
    QApplication.processEvents()
    return box


def _screen_of(widget):
    return (QGuiApplication.screenAt(widget.mapToGlobal(QPoint(1, 1))) or QGuiApplication.primaryScreen()).availableGeometry()


# ── opt-in ───────────────────────────────────────────────────────────────────
def test_row_marks_are_opt_in(qapp, host):
    from ui.components import model_combo
    win = host()
    plain = _picker(win, marks=False)
    assert not plain._marks_on and not isinstance(plain.itemDelegate(), model_combo._RowDelegate)
    assert plain.view().minimumWidth() == 0
    plain.showPopup()                                   # the plain Qt list: nothing of ours runs
    QApplication.processEvents()
    assert plain.view().minimumWidth() == 0
    plain.hidePopup()
    marked = _picker(win)
    assert marked._marks_on and isinstance(marked.itemDelegate(), model_combo._RowDelegate)
    assert marked.view().textElideMode() == Qt.ElideNone
    assert marked.enable_row_marks() is marked          # a second call changes nothing


def _wheel(box, dy=-120):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QWheelEvent
    ev = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, dy), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    QApplication.sendEvent(box, ev)
    return ev


def test_a_marked_picker_is_only_changed_through_its_list(qapp, host):
    """The wheel scrolls the page (it never picks a model, which may ask a question) and
    Up / Down open the list instead of stepping to the next row."""
    win = host()
    box = _picker(win)
    seen = []
    box.currentIndexChanged.connect(seen.append)
    box.setFocus()
    ev = _wheel(box)
    assert not ev.isAccepted() and seen == [] and box.currentIndex() == 0      # the page gets it
    for key in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_End):
        QTest.keyClick(box, key)
        QApplication.processEvents()
        assert box.view().isVisible() and seen == [], key                    # the list opened, nothing picked
        box.hidePopup()
        QApplication.processEvents()
    assert box.currentIndex() == 0
    # Space / Enter / Alt+Down still open it too (the platform's way), and a pick from the open list works.
    box.showPopup()
    QApplication.processEvents()
    box.view().setCurrentIndex(box.model().index(1, 0))
    QTest.keyClick(box.view(), Qt.Key_Return)
    QApplication.processEvents()
    assert seen == [1] and box.currentIndex() == 1


def test_a_plain_picker_keeps_the_wheel_and_arrows(qapp, host):
    box = _picker(host(), marks=False)
    box.setFocus()
    _wheel(box)
    assert box.currentIndex() == 1                                           # Audio to text: as before
    QTest.keyClick(box, Qt.Key_Down)
    assert box.currentIndex() == 2 and not box.view().isVisible()


def test_audio_to_text_keeps_its_plain_list(qapp):
    from ui.components import model_combo
    from ui.pages import audio_to_text
    assert audio_to_text.PlainNameCombo is model_combo.PlainNameCombo
    source = open(audio_to_text.__file__, encoding="utf-8").read()
    assert "enable_row_marks" not in source


def test_marks_are_stored_and_spoken(qapp, host):
    from ui.components.model_combo import MARK_DOWNLOAD, MARK_PRO
    box = _picker(host())
    base, cloud = box.findData("base"), box.findData("cloud")
    box.set_choice_mark(base, MARK_DOWNLOAD, note="140 MB")
    box.set_choice_mark(cloud, MARK_PRO)
    assert box.choice_mark(base) == "download" and box.choice_mark(cloud) == "pro"
    assert box.choice_mark(0) == "" and box.choice_mark(99) == ""
    assert box.itemData(base, Qt.AccessibleTextRole) == "Fast · Whisper Base, not downloaded"
    assert box.itemData(cloud, Qt.AccessibleTextRole) == "Gusan Cloud · Gemini 2.5 Flash, Pro"
    assert box.itemData(base, Qt.ToolTipRole) == "Not downloaded yet (140 MB)"
    assert box.itemData(cloud, Qt.ToolTipRole) == "Pro feature"
    box.set_choice_mark(cloud, "")
    assert box.choice_mark(cloud) == "" and box.itemData(cloud, Qt.AccessibleTextRole) == box.itemText(cloud)
    box.set_choice_mark(cloud, "sparkles")                # not a mark: nothing
    assert box.choice_mark(cloud) == ""
    box.set_choice_mark(99, MARK_PRO)                     # out of range: nothing happens
    assert box.itemText(0) == "Same as Dictation"         # the text itself is never edited


def test_marks_leave_no_item_wrapper_behind(qapp, host):
    # as test_greying_a_choice_out_leaves_no_wrapper_behind: never model.item()
    from ui.components.model_combo import MARK_PRO

    def wrappers():
        gc.collect()
        return sum(1 for o in gc.get_objects() if type(o).__name__ == "QStandardItem")
    box = _picker(host())
    before = wrappers()
    box.set_choice_mark(3, MARK_PRO)
    box.clear()                                           # a refill (the plan changed)
    box.add_choice("Same as Dictation", "")
    assert wrappers() == before
    assert box.choice_mark(0) == ""                       # a refill starts clean


# ── the list: whole rows, never narrower than the widest row ─────────────────
def test_the_list_is_as_wide_as_its_widest_row(qapp, host):
    from ui.components.model_combo import MARK_PRO
    box = _picker(host(), width=150)
    box.set_choice_mark(box.findData("cloud"), MARK_PRO)
    fm = box.fontMetrics()
    need_text = max(fm.horizontalAdvance(box.itemText(i)) for i in range(box.count()))
    pill = box.itemDelegate().mark_width("pro")
    assert pill > 20                                      # the real pill, with its padding
    hint = box.popup_width_hint()
    assert hint > box.width()                             # wider than the closed box
    assert hint >= need_text + 28                         # the whole text + its padding
    cloud_row = fm.horizontalAdvance(box.itemText(box.findData("cloud"))) + 28 + 16 + pill
    assert hint >= cloud_row                              # a marked row: text + gap + pill + padding
    box.showPopup()
    QApplication.processEvents()
    pop = box.view().window()
    assert pop.width() >= hint
    assert box.view().minimumWidth() >= hint
    assert box.view().viewport().width() >= max(
        box.itemDelegate().sizeHint(_option_for(box), box.model().index(i, 0)).width() for i in range(box.count()))
    box.hidePopup()


def _option_for(box):
    from PySide6.QtWidgets import QStyleOptionViewItem
    opt = QStyleOptionViewItem()
    opt.widget = box.view()
    opt.font = box.font()
    return opt


@pytest.mark.parametrize("px", [11, 13.5, 18, 24])
def test_the_list_follows_the_boxs_font(qapp, host, px):
    """A bigger font (zoom, a different family): the list grows with it, rows stay whole."""
    from ui import theme
    from ui.components.model_combo import MARK_PRO
    box = _picker(host(w=1200), width=150)
    box.setFont(theme.font(px))
    box.set_choice_mark(box.findData("cloud"), MARK_PRO)
    box.showPopup()
    QApplication.processEvents()
    fm = box.fontMetrics()
    pop = box.view().window()
    cloud_need = fm.horizontalAdvance(box.itemText(box.findData("cloud"))) + 28 + 16 + box.itemDelegate().mark_width("pro")
    assert pop.width() >= cloud_need
    assert pop.width() >= fm.horizontalAdvance(LONG) + 28
    box.hidePopup()


def test_the_list_never_elides_a_row(qapp, host):
    """Paint the open list and look for the row's last letter: 'Turbo' must be whole."""
    box = _picker(host(), width=120)
    box.showPopup()
    QApplication.processEvents()
    view = box.view()
    row = view.visualRect(box.model().index(box.findData("large-v3-turbo"), 0))
    text_w = box.fontMetrics().horizontalAdvance(LONG)
    assert row.width() >= text_w + 20, "the row is wide enough for its whole line"
    img = box.view().window().grab().toImage()
    assert img.width() >= text_w + 28
    box.hidePopup()


def test_the_list_stays_on_screen(qapp, host):
    """A combo at the right edge of the screen: the wide list is pulled back inside."""
    scr = QGuiApplication.primaryScreen().availableGeometry()
    win = host(x=scr.right() - 340, y=80, w=340, h=360)
    box = _picker(win, width=150)
    box.move(win.width() - 150 - 12, 40)                  # its right edge near the window's right edge
    win.layout().removeWidget(box)
    QApplication.processEvents()
    box.showPopup()
    QApplication.processEvents()
    pop = box.view().window()
    geo = pop.frameGeometry()
    avail = _screen_of(box)
    assert geo.width() >= box.popup_width_hint()
    assert geo.left() >= avail.left() and geo.right() <= avail.right(), (geo, avail)
    assert geo.width() > box.width()                      # still wider than the closed box
    box.hidePopup()


def test_the_list_lines_up_with_the_box_when_it_fits(qapp, host):
    win = host(x=60, y=60, w=700, h=360)
    box = _picker(win, width=150)
    box.showPopup()
    QApplication.processEvents()
    geo = box.view().window().frameGeometry()
    assert geo.left() == box.mapToGlobal(QPoint(0, 0)).x() or abs(geo.left() - box.mapToGlobal(QPoint(0, 0)).x()) <= 2
    box.hidePopup()


# ── what is painted ──────────────────────────────────────────────────────────
def _row_pixels(box, img, value):
    """Pixel colours of the right-hand 56 px of a row of the open list."""
    view = box.view()
    r = view.visualRect(box.model().index(box.findData(value), 0))
    off = view.viewport().mapTo(view.window(), QPoint(0, 0))
    x0 = off.x() + r.right() - 56
    cols = []
    for y in range(off.y() + r.top() + 2, off.y() + r.bottom() - 1):
        for x in range(x0, off.x() + r.right() - 2):
            cols.append(QColor(img.pixel(x, y)))
    return cols


def _is_purple(c):
    return c.red() > 110 and c.blue() > 170 and c.green() < c.red() - 20 and c.blue() > c.green() + 80


def _is_ink(c):
    return (c.red() + c.green() + c.blue()) < 600           # not the white of an empty row


def test_marks_are_painted_at_the_right_edge(qapp, host):
    from ui.components.model_combo import MARK_DOWNLOAD, MARK_PRO
    box = _picker(host(), width=150)
    box.setCurrentIndex(box.findData("base"))             # the highlighted row is not the one measured
    box.set_choice_mark(box.findData("large-v3-turbo"), MARK_DOWNLOAD)
    box.set_choice_mark(box.findData("cloud"), MARK_PRO)
    box.showPopup()
    QApplication.processEvents()
    img = box.view().window().grab().toImage()
    pill = _row_pixels(box, img, "cloud")
    assert sum(1 for c in pill if _is_purple(c)) > 150, "a purple PRO pill"
    arrow = _row_pixels(box, img, "large-v3-turbo")
    assert 5 < sum(1 for c in arrow if _is_ink(c)) < 120 and not any(_is_purple(c) for c in arrow), "a small download icon"
    assert not any(_is_ink(c) for c in _row_pixels(box, img, ""))           # "Same as Dictation": nothing
    box.hidePopup()


def test_the_pro_pill_is_the_real_pro_tag(qapp, host):
    from ui.components.badges import ProTag
    box = _picker(host())
    pm = box.itemDelegate().mark_pixmap("pro")
    tag = ProTag("s", parent=box.view())
    tag.hide()
    tag.ensurePolished()
    tag.resize(tag.sizeHint())
    assert pm.toImage() == tag.grab().toImage()           # same pixels as the PRO tag everywhere else
    tag.deleteLater()


def test_the_download_icon_follows_the_row_state(qapp, host):
    box = _picker(host())
    rest = box.itemDelegate().mark_pixmap("download", hot=False).toImage()
    hot = box.itemDelegate().mark_pixmap("download", hot=True).toImage()
    assert rest != hot                                    # muted at rest, blue under the pointer / selected
    assert box.itemDelegate().mark_pixmap("") is None


# ── the Smart Actions list, as a person uses it ──────────────────────────────
class FakeDownloads(QObject):
    progress = Signal(str, int, object, object)
    finished = Signal(str, str, str)
    state_changed = Signal(str, str)
    scanned = Signal()

    def __init__(self, ready=("base",)):
        super().__init__()
        self.states = {m: "ready" for m in ready}
        self.started = []

    def is_scanned(self):
        return True

    def state(self, name):
        return self.states.get(name, "missing")

    def progress_of(self, name):
        return None

    def start(self, name):
        self.started.append(name)
        self.states[name] = "downloading"
        self.state_changed.emit(name, "downloading")
        return True


def _live_app(tier, **cfg):
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


@pytest.fixture
def sa_page(qapp, monkeypatch):
    import entitlements
    import ui.pages.smart_actions as sa
    monkeypatch.setattr(entitlements, "smart_actions_remaining", lambda auth=None, cfg=None: 4)
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


@pytest.fixture
def asked(monkeypatch):
    from types import SimpleNamespace
    from ui.components import mini_prompt
    rec = SimpleNamespace(calls=[], answer=True)

    def fake_ask(parent, title, body, yes, no, icon="info"):
        rec.calls.append((title, body, yes, no))
        return rec.answer
    monkeypatch.setattr(mini_prompt, "ask", fake_ask)
    return rec


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    return cond()


def _click_row(box, value):
    """What a person does: open the list, click a row (a real mouse press and release)."""
    box.showPopup()
    QApplication.processEvents()
    QTest.qWait(QApplication.doubleClickInterval() + 120)    # Qt ignores a release right after it opens
    view = box.view()
    rect = view.visualRect(box.model().index(box.findData(value), 0))
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    QApplication.processEvents()


def test_clicking_a_row_not_on_disk_asks_and_does_not_select(sa_page, asked):
    asked.answer = False
    app = _live_app("pro", smart_actions_speech_model="base")
    app.downloads = FakeDownloads(ready=("base",))
    box = sa_page(app).speech_combo
    _click_row(box, "large-v3-turbo")
    assert asked.calls == [("Download Most accurate · Whisper Large v3 Turbo?",
                            "1.6 GB - Smart Actions will use it once it is ready.", "Download", "Not now")]
    assert box.currentData() == "base" and app.cfg["smart_actions_speech_model"] == "base"
    assert app.downloads.started == []
    assert not box.view().isVisible()                      # the list closed


def test_clicking_download_starts_it_and_selects_it(sa_page, asked):
    app = _live_app("pro")
    app.downloads = FakeDownloads(ready=("base",))
    page = sa_page(app)
    _click_row(page.speech_combo, "large-v3-turbo")
    assert len(asked.calls) == 1
    assert app.downloads.started == ["large-v3-turbo"]
    assert app.cfg["smart_actions_speech_model"] == "large-v3-turbo"
    assert page.speech_combo.currentData() == "large-v3-turbo"
    assert page.speech_line.text().startswith("Whisper Large v3 Turbo · downloading")


def test_clicking_a_downloaded_row_selects_it_with_no_question(sa_page, asked):
    app = _live_app("pro")
    app.downloads = FakeDownloads(ready=("base", "small"))
    page = sa_page(app)
    _click_row(page.speech_combo, "small")
    assert not asked.calls and app.downloads.started == []
    assert page.speech_combo.currentData() == "small" and app.cfg["smart_actions_speech_model"] == "small"
    _click_row(page.speech_combo, "")                       # Same as Dictation
    assert not asked.calls and app.cfg["smart_actions_speech_model"] == ""


def test_clicking_gusan_cloud_as_free_opens_the_pro_window(sa_page, asked):
    app = _live_app("free")
    page = sa_page(app)
    _click_row(page.speech_combo, "cloud")
    assert [c for c in app.calls if c[0] == "_pro_upsell"] == [("_pro_upsell", "Fast cloud transcription")]
    assert not asked.calls and page.speech_combo.currentData() == ""
    assert page.speech_combo.choice_enabled(page.speech_combo.findData("cloud"))      # never greyed


def test_scrolling_over_the_picker_never_asks_or_picks(sa_page, asked):
    app = _live_app("pro", smart_actions_speech_model="base")
    app.downloads = FakeDownloads(ready=("base",))
    box = sa_page(app).speech_combo
    box.setFocus()
    for _ in range(4):
        _wheel(box)
    QTest.keyClick(box, Qt.Key_Down)                        # opens the list, picks nothing
    QApplication.processEvents()
    box.hidePopup()
    assert not asked.calls and app.downloads.started == []
    assert box.currentData() == "base" and app.cfg["smart_actions_speech_model"] == "base"


def test_keyboard_enter_on_a_row_not_on_disk_asks_too(sa_page, asked):
    asked.answer = False
    app = _live_app("pro", smart_actions_speech_model="base")
    app.downloads = FakeDownloads(ready=("base",))
    box = sa_page(app).speech_combo
    box.showPopup()
    QApplication.processEvents()
    view = box.view()
    view.setCurrentIndex(box.model().index(box.findData("medium"), 0))
    QTest.keyClick(view, Qt.Key_Return)
    QApplication.processEvents()
    assert len(asked.calls) == 1 and asked.calls[0][0] == "Download Accurate · Whisper Medium?"
    assert box.currentData() == "base"


# ── the mini popups ──────────────────────────────────────────────────────────
def test_download_popup_words(qapp):
    from ui.components import mini_prompt
    assert mini_prompt.download_title("Balanced", "Whisper Small") == "Download Balanced · Whisper Small?"
    assert mini_prompt.download_title("Balanced") == "Download Balanced?"
    assert mini_prompt.download_body("460 MB") == "460 MB - Smart Actions will use it once it is ready."
    assert mini_prompt.download_body("") == "Smart Actions will use it once it is ready."


def _card(qapp, **kw):
    from ui.components import mini_prompt
    host = QWidget()
    host.resize(900, 600)
    host.show()
    dlg = mini_prompt.MiniPrompt(host, kw.get("title", "Download Balanced · Whisper Small?"),
                                 kw.get("body", "460 MB - Smart Actions will use it once it is ready."),
                                 kw.get("yes", "Download"), kw.get("no", "Not now"), icon=kw.get("icon", "download"))
    dlg.show()
    QApplication.processEvents()
    return host, dlg


def test_the_popup_has_names_and_a_default_button(qapp):
    host, dlg = _card(qapp)
    try:
        assert dlg.isModal() and dlg.windowTitle() == "Download Balanced · Whisper Small?"
        assert dlg.accessibleName() == "Download Balanced · Whisper Small?"
        assert dlg.accessibleDescription().startswith("460 MB - ")
        assert dlg.yes_button.accessibleName() == "Download" and dlg.no_button.accessibleName() == "Not now"
        assert dlg.yes_button.isDefault() and not dlg.no_button.isDefault()
        assert dlg.yes_button.hasFocus() or dlg.focusWidget() is dlg.yes_button
        assert dlg.title_label.wordWrap() and dlg.body_label.wordWrap()
        # in the middle of its window, whole
        c = dlg.frameGeometry().center()
        hc = host.frameGeometry().center()
        assert abs(c.x() - hc.x()) <= 3 and abs(c.y() - hc.y()) <= 3
        assert dlg.card.width() == 392                      # a mini popup, not a dialog
    finally:
        dlg.close()
        host.close()


def test_enter_presses_the_first_button_and_escape_the_second(qapp):
    host, dlg = _card(qapp)
    QTest.keyClick(dlg, Qt.Key_Return)
    assert dlg.result() == QDialog.Accepted
    dlg.close()
    host.close()
    host, dlg = _card(qapp)
    QTest.keyClick(dlg, Qt.Key_Escape)
    assert dlg.result() == QDialog.Rejected
    dlg.close()
    host.close()
    host, dlg = _card(qapp)
    dlg.no_button.click()
    assert dlg.result() == QDialog.Rejected
    dlg.close()
    host.close()


def test_ask_returns_the_answer(qapp):
    from ui.components import mini_prompt

    def press(which):
        def go():
            dlg = QApplication.activeModalWidget()
            assert isinstance(dlg, mini_prompt.MiniPrompt)
            getattr(dlg, which).click()
        return go
    QTimer.singleShot(0, press("yes_button"))
    assert mini_prompt.ask(None, "Download?", "1 GB - body", "Download", "Not now") is True
    QTimer.singleShot(0, press("no_button"))
    assert mini_prompt.confirm_download(None, "Fast", "Whisper Base", "140 MB") is False


def test_privacy_prompt_words_and_answer(qapp):
    from ui import privacy_prompt
    from ui.components import mini_prompt
    assert privacy_prompt.TITLE == "Privacy mode is on"
    assert privacy_prompt.body_text("Gusan Cloud") == (
        "Gusan Cloud sends your voice to our servers. Turn off Privacy mode to use it?")
    seen = {}

    def go():
        dlg = QApplication.activeModalWidget()
        seen.update(title=dlg.title_label.text(), body=dlg.body_label.text(),
                    yes=dlg.yes_button.text(), no=dlg.no_button.text())
        dlg.yes_button.click()
    QTimer.singleShot(0, go)
    assert privacy_prompt.confirm_turn_off_privacy(None, "Gusan Cloud") is True
    assert seen == {"title": "Privacy mode is on",
                    "body": "Gusan Cloud sends your voice to our servers. Turn off Privacy mode to use it?",
                    "yes": "Turn off Privacy mode", "no": "Keep it on"}
    assert isinstance(mini_prompt.MiniPrompt, type)
