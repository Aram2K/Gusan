"""The "Updating Gusan" window (ui/update_window.py), on real Qt, offscreen: every stage and its buttons,
the progress text, how Esc / the X behave (a Cancel while downloading or waiting, nothing while installing,
and always a clean close while Gusan quits - a refusing window would cancel the quit), the one light look,
that long paths wrap inside the card, that every stage fits a 1366x768 screen at 150 percent (910 x 512
logical pixels) and has names for screen readers. The colours are measured in test_dialog_visibility.py (a
light and a dark OS), the old product name in test_no_old_name.py; the controller that drives it is
tests/test_update_flow.py.

Set GUSAN_SHOTS_DIR to also write a render of every stage (update_<stage>.png; with QT_SCALE_FACTOR=1.5
the file names get _150, like tests_qt/test_hy_pickers.py): the way to look at it at 150 percent."""
import os
import re

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QDialog, QLabel, QProgressBar, QWidget

from app_info import APP_VERSION

MB = 2 ** 20
SCREEN = (1366 / 1.5, 768 / 1.5)          # a 1366x768 laptop at 150 percent Windows scaling, in logical pixels


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    import ui.winfit
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)      # the offscreen screen is 800x600


@pytest.fixture
def made(qapp):
    items = []
    yield items
    for w in items:
        try:
            w.hide()
            w.deleteLater()
        except RuntimeError:
            pass
    QApplication.processEvents()


@pytest.fixture
def build(qapp, made):
    """build(tag, app) -> (window, hits): hits records every signal the window emits."""
    def _build(tag="v2.0.2", app=None, show=True):
        from ui.update_window import UpdateWindow
        w = UpdateWindow(tag, main_app=app)
        hits = []
        for name in ("cancel_requested", "retry_requested", "install_requested", "website_requested"):
            getattr(w, name).connect(lambda n=name: hits.append(n))
        made.append(w)
        if show:
            w.show()
            QApplication.processEvents()
        return w, hits
    return _build


def shown(button):
    return not button.isHidden()


def buttons_of(w):
    return {"cancel": shown(w.btn_cancel), "install": shown(w.btn_install), "retry": shown(w.btn_retry),
            "website": shown(w.btn_website), "close": shown(w.btn_close)}


def only(*names):
    return {n: n in names for n in ("cancel", "install", "retry", "website", "close")}


def go_to(w, stage):
    """Put the window in a named stage the way the controller does."""
    from ui import update_window as uw
    if stage == "downloading":
        w.show_downloading()
    elif stage == "progress":
        w.show_downloading()
        w.show_progress(45 * MB, 100 * MB)
    elif stage == "unknown size":
        w.show_downloading()
        w.show_progress(12 * MB, 0)
    elif stage == "checking":
        w.show_downloading()
        w.show_progress(100 * MB, 100 * MB)
    elif stage == "waiting":
        w.show_waiting()
    elif stage == "waiting for a key":
        w.show_waiting(can_install=True)
    elif stage == "installing":
        w.show_installing()
    elif stage == "installing as administrator":
        w.show_installing(admin=True)
    elif stage == "installing, nothing reopens it":
        w.show_installing(reopens=False, admin=True)
    elif stage == "failed":
        w.show_failed("The update couldn't be downloaded. Check your connection and try again.")
    elif stage == "failed with a log":
        w.show_failed("The installer stopped before it finished.",
                      "Details: " + os.path.join("C:\\Users\\Someone With A Long Name\\AppData\\Local\\Temp",
                                                 "gusan-update", "logs", "gusan-update-0a1b2c3d.log"))
    elif stage == "failed with a path that has no spaces":
        w.show_failed("The installer stopped before it finished.", "Details: " + "C:\\" + "a" * 150 + ".log")
    else:
        raise AssertionError(stage)
    QApplication.processEvents()
    assert uw  # (the module under test)


STAGES = ["downloading", "progress", "unknown size", "checking", "waiting", "waiting for a key", "installing",
          "installing as administrator", "installing, nothing reopens it", "failed", "failed with a log",
          "failed with a path that has no spaces"]


# ═════════════════════════ the stages ═════════════════════════
def test_downloading(build):
    w, _ = build()
    assert w.stage == "downloading"
    assert w.windowTitle() == "Updating Gusan"
    assert w.lbl_title.text() == "Updating Gusan"
    assert w.lbl_version.text() == "Version 2.0.2"
    assert w.lbl_status.text() == "Downloading the update"
    assert w.bar.minimum() == w.bar.maximum() == 0               # busy until a size is known
    assert buttons_of(w) == only("cancel")


def test_progress_shows_megabytes_and_a_moving_bar(build):
    w, _ = build()
    w.show_progress(45 * MB, 100 * MB)
    assert (w.bar.minimum(), w.bar.maximum(), w.bar.value()) == (0, 100, 45)
    assert w.lbl_detail.text() == "45 of 100 MB"
    assert w.lbl_status.text() == "Downloading the update"
    w.show_progress(80 * MB, 100 * MB)
    assert w.bar.value() == 80 and w.lbl_detail.text() == "80 of 100 MB"
    assert buttons_of(w) == only("cancel")


def test_an_unknown_size_shows_the_megabytes_so_far(build):
    w, _ = build()
    w.show_progress(12 * MB, 0)
    assert w.lbl_detail.text() == "12 MB"
    assert w.bar.maximum() == 0                                   # a busy bar, never a fake percentage


def test_at_100_percent_it_says_it_is_checking_the_download(build):
    w, _ = build()
    w.show_progress(100 * MB, 100 * MB)
    assert w.stage == "downloading"                              # still Cancel-able
    assert w.lbl_status.text() == "Checking the download"
    assert w.lbl_detail.isHidden() and w.bar.maximum() == 0
    assert buttons_of(w) == only("cancel")
    w.show_progress(40 * MB, 100 * MB)                           # a late, older report changes nothing
    assert w.lbl_status.text() == "Checking the download"
    w.show_downloading()                                         # a new attempt starts from the top
    assert w.lbl_status.text() == "Downloading the update"
    w.show_progress(1 * MB, 100 * MB)
    assert w.lbl_detail.text() == "1 of 100 MB"


def test_progress_is_ignored_outside_a_download(build):
    w, _ = build()
    w.show_installing()
    text = w.lbl_status.text()
    w.show_progress(50 * MB, 100 * MB)
    assert w.lbl_status.text() == text and w.stage == "installing"


def test_waiting_for_a_recording_or_a_file(build):
    w, _ = build()
    w.show_waiting()
    assert w.stage == "waiting"
    assert w.lbl_status.text() == "Ready to install. It starts when your recording or file is done."
    assert buttons_of(w) == only("cancel")
    assert (w.bar.minimum(), w.bar.maximum(), w.bar.value()) == (0, 100, 100)       # downloaded: a full bar


def test_waiting_for_a_typed_key_offers_install_now(build):
    w, hits = build()
    w.show_waiting(can_install=True)
    assert w.lbl_status.text() == "Ready to install. Save the key you typed first, or choose Install now."
    assert buttons_of(w) == only("cancel", "install")
    assert w.btn_install.isDefault()
    QTest.mouseClick(w.btn_install, Qt.LeftButton)
    assert hits == ["install_requested"]


def test_the_same_waiting_text_every_two_seconds_does_not_flicker(build):
    w, _ = build()
    writes = []
    real = w.lbl_status.setText
    w.lbl_status.setText = lambda text: (writes.append(text), real(text))
    w.show_waiting()
    w.show_waiting()
    w.show_waiting()
    assert len(writes) == 1
    w.show_waiting(can_install=True)                              # the situation changed: new words
    assert len(writes) == 2 and "Save the key" in w.lbl_status.text()
    w.show_waiting(can_install=True)
    assert len(writes) == 2
    w.show_waiting("Something else")                              # and the words may be the caller's own
    assert w.lbl_status.text() == "Something else" and w.stage == "waiting"


def test_installing_has_no_buttons_and_says_gusan_reopens(build):
    w, _ = build()
    w.show_installing()
    assert w.stage == "installing"
    assert w.lbl_status.text() == "Installing. Gusan closes now and opens again by itself in about a minute."
    assert w.lbl_detail.text() == "The installer shows its own small window while it works."
    assert buttons_of(w) == only()
    assert w.bar.maximum() == 0


def test_an_all_users_install_says_windows_asks_first(build):
    w, _ = build()
    w.show_installing(admin=True)
    assert w.lbl_detail.text().startswith("Windows asks for permission first.")
    assert "own small window" in w.lbl_detail.text()


def test_when_nothing_reopens_gusan_the_start_menu_is_named(build):
    w, _ = build()
    w.show_installing(reopens=False)
    assert "Start menu" in w.lbl_status.text()
    assert "opens again by itself" not in w.lbl_status.text()


def test_failed_names_the_reason_and_the_way_forward(build):
    w, hits = build()
    w.show_failed("couldn't download the update. Check your connection and try again.")
    assert w.stage == "failed"
    assert w.lbl_status.text() == ("The update didn't install. Couldn't download the update. "
                                   "Check your connection and try again.")
    assert w.lbl_detail.text() == "Gusan %s keeps working." % APP_VERSION
    assert w.lbl_note.isHidden()
    assert buttons_of(w) == only("close", "website", "retry")
    assert w.bar.isHidden()
    assert w.btn_retry.isDefault() and w.btn_retry.text() == "Try again"
    assert w.btn_website.text() == "Open the download page"
    for button, signal in ((w.btn_retry, "retry_requested"), (w.btn_website, "website_requested")):
        QTest.mouseClick(button, Qt.LeftButton)
        assert hits[-1] == signal
    assert "cancel_requested" not in hits


def test_failed_with_a_log_shows_it_selectable(build):
    w, _ = build()
    w.show_failed("The installer stopped before it finished.", "Details: C:\\t\\x.log")
    assert w.lbl_note.text() == "Details: C:\\t\\x.log" and not w.lbl_note.isHidden()
    assert w.lbl_note.textInteractionFlags() & Qt.TextSelectableByMouse
    w.show_failed("")                                              # an empty message still says something
    assert w.lbl_status.text().endswith("Please try again.")
    assert w.lbl_note.isHidden()


def test_a_new_attempt_in_the_same_window_starts_clean(build):
    w, _ = build()
    w.show_failed("It broke.", "Details: x")
    w.show_downloading()
    assert w.stage == "downloading" and w.lbl_status.text() == "Downloading the update"
    assert w.lbl_note.isHidden() and w.lbl_detail.isHidden() and not w.bar.isHidden()
    assert buttons_of(w) == only("cancel")
    w.show_waiting()
    w.show_downloading()                                           # Cancel, then Install again: the same window
    assert w.stage == "downloading" and buttons_of(w) == only("cancel")


def test_the_version_line_follows_the_release(build):
    w, _ = build("v2.0.2")
    assert w.lbl_version.text() == "Version 2.0.2"
    w.set_tag("v2.1.0")
    assert (w.tag, w.lbl_version.text()) == ("v2.1.0", "Version 2.1.0")
    w.set_tag("")
    assert w.lbl_version.isHidden()


# ═════════════════════════ closing ═════════════════════════
@pytest.mark.parametrize("stage", ["downloading", "waiting"])
def test_esc_the_x_and_cancel_while_downloading_or_waiting_are_a_cancel(build, stage):
    for how in ("reject", "close", "escape", "button"):
        w, hits = build()
        w.show_downloading() if stage == "downloading" else w.show_waiting()
        if how == "reject":
            w.reject()
        elif how == "close":
            assert w.close()
        elif how == "escape":
            QTest.keyClick(w, Qt.Key_Escape)
        else:
            QTest.mouseClick(w.btn_cancel, Qt.LeftButton)
        assert hits == ["cancel_requested"], how
        assert not w.isVisible(), how


def test_esc_and_the_x_while_installing_do_nothing(build):
    w, hits = build()
    w.show_installing()
    QTest.keyClick(w, Qt.Key_Escape)
    w.reject()
    w.close()
    QApplication.processEvents()
    assert hits == []
    assert w.isVisible()                                          # Gusan closes by itself in a moment anyway


def test_after_a_failure_closing_is_just_closing(build):
    for how in ("escape", "x", "close button"):
        w, hits = build()
        w.show_failed("It broke.")
        if how == "escape":
            QTest.keyClick(w, Qt.Key_Escape)
        elif how == "x":
            w.close()
        else:
            QTest.mouseClick(w.btn_close, Qt.LeftButton)
        assert hits == [], how                                    # no cancel: there is nothing to cancel
        assert not w.isVisible(), how


def test_the_window_never_vetoes_the_apps_quit(build):
    """Qt asks every window to close when the app quits and stops at the first that refuses - the app would
    then never close and Setup would have to force it shut. While installing the window ignores Esc and the
    X, EXCEPT once the controller is quitting."""
    from types import SimpleNamespace
    app = SimpleNamespace(_quitting=False)
    w, hits = build(app=app)
    w.show_installing()
    ev = QCloseEvent()
    ev.ignore()
    w.closeEvent(ev)
    assert not ev.isAccepted()                                    # a person pressing the X: ignored
    assert w.isVisible()
    app._quitting = True                                          # quit_app has set it: Qt's quit now closes everything
    ev = QCloseEvent()
    ev.ignore()
    w.closeEvent(ev)
    assert ev.isAccepted()
    assert w.close()
    assert not w.isVisible() and hits == []                       # (and no Cancel is sent for it)
    for stage in ("downloading", "waiting", "failed"):            # also in every other stage
        w2, hits2 = build(app=SimpleNamespace(_quitting=True))
        {"downloading": w2.show_downloading, "waiting": w2.show_waiting,
         "failed": lambda: w2.show_failed("x")}[stage]()
        assert w2.close() and not w2.isVisible() and hits2 == []


def test_a_controller_without_the_flag_is_treated_as_not_quitting(build):
    w, _ = build(app=object())
    w.show_installing()
    assert w.close() is False or w.isVisible()


def test_cancel_button_hides_the_window_and_it_can_come_back(build):
    w, hits = build()
    QTest.mouseClick(w.btn_cancel, Qt.LeftButton)
    assert hits == ["cancel_requested"] and not w.isVisible()
    w.show_downloading()
    w.show()
    assert w.isVisible() and buttons_of(w) == only("cancel")


# ═════════════════════════ what kind of window ═════════════════════════
def test_a_themed_qdialog_subclass_that_is_not_modal_not_on_top_and_not_a_child(build):
    w, _ = build()
    assert isinstance(w, QDialog) and type(w) is not QDialog     # a bare QDialog is banned (test_no_system_dialogs)
    assert not w.isModal()
    assert w.parent() is None and w.isWindow()
    flags = w.windowFlags()
    # a question card (mini_prompt) must be able to sit over it
    assert not flags & Qt.WindowStaysOnTopHint
    assert flags & Qt.WindowTitleHint and flags & Qt.WindowCloseButtonHint
    assert not flags & Qt.WindowContextHelpButtonHint
    assert w.property("gusan") == "window"                        # theme.apply: the one light look
    assert w.styleSheet()


def test_it_is_placed_on_screen_when_shown(build, monkeypatch):
    import ui.winfit
    placed = []
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda win, *a, **k: placed.append(win))
    w, _ = build()
    assert placed == [w]


def test_a_question_card_sits_over_it(build):
    """The one question of an update (a typed key) is a mini_prompt card over this window."""
    from ui.components import mini_prompt
    w, _ = build()
    card = mini_prompt.MiniPrompt(w, "Install the update now?", "The key you typed isn't saved yet.", None, None,
                                  buttons=[("install", "Install now", "primary"), ("wait", "Not yet", "secondary")],
                                  default="wait")
    try:
        assert card.parentWidget() is w
        assert card.buttons["wait"].isDefault()
    finally:
        card.deleteLater()


# ═════════════════════════ size, wrapping, names ═════════════════════════
@pytest.mark.parametrize("stage", STAGES)
def test_every_stage_fits_a_1366_by_768_screen_at_150_percent(build, stage):
    w, _ = build()
    go_to(w, stage)
    size = w.sizeHint()
    assert size.width() <= SCREEN[0] and size.height() <= SCREEN[1], (stage, size)
    assert (w.width(), w.height()) == (size.width(), size.height())        # exactly as tall as the words need
    assert w.card.width() == 440                                           # the same width in every stage
    assert w.width() == 440 + 2 * 18


def test_the_height_follows_the_words_and_the_width_never_changes(build):
    w, _ = build()
    heights = {}
    for stage in STAGES:
        go_to(w, stage)
        assert w.width() == 476, stage
        heights[stage] = w.height()
        for lbl in (w.lbl_status, w.lbl_detail, w.lbl_note):               # one stage after another: never a stale, cut-off label
            assert lbl.isHidden() or lbl.height() >= lbl.heightForWidth(lbl.width()), (stage, lbl.text()[:30])
    assert heights["failed with a log"] > heights["failed"] > heights["downloading"]
    assert heights["waiting for a key"] >= heights["waiting"]
    assert max(heights.values()) <= SCREEN[1]


@pytest.mark.parametrize("stage", ["failed with a log", "failed with a path that has no spaces"])
def test_a_long_path_wraps_inside_the_card(build, stage):
    w, _ = build()
    go_to(w, stage)
    card = w.card.rect()
    for lbl in (w.lbl_status, w.lbl_detail, w.lbl_note, w.lbl_title):
        assert lbl.height() >= lbl.heightForWidth(lbl.width()), (stage, "text is cut off", lbl.text()[:30])
        assert lbl.parentWidget() is w.card                                 # (so its geometry is in the card's pixels)
        assert card.contains(lbl.geometry().adjusted(0, 0, -1, -1)), (stage, lbl.text()[:30], lbl.geometry(), card)
    assert w.lbl_note.heightForWidth(w.lbl_note.width()) > w.lbl_note.fontMetrics().height() * 1.5     # more than one line
    for b in w.card.findChildren(QAbstractButton):
        if shown(b):
            assert w.card.rect().contains(b.mapTo(w.card, b.rect().bottomRight())), b.text()


@pytest.mark.parametrize("stage", STAGES)
def test_every_piece_has_a_name_for_screen_readers(build, stage):
    w, _ = build()
    go_to(w, stage)
    assert w.accessibleName() == "Updating Gusan"
    assert w.accessibleDescription().startswith(w.lbl_status.text()[:20])
    for lbl in w.findChildren(QLabel):
        if lbl is w.tile or lbl.isHidden() or not lbl.text().strip():
            continue
        assert lbl.accessibleName().strip(), (stage, lbl.text())
    assert w.tile.accessibleName() == ""                                   # an icon: nothing to read
    for b in w.findChildren(QAbstractButton):
        assert b.accessibleName().strip() == b.text().strip(), b.text()
    assert w.findChildren(QProgressBar)[0].accessibleName() == "Update progress"
    assert w.btn_cancel.accessibleName() == "Cancel"


def test_it_says_gusan_never_the_old_name_and_no_limit(build):
    from tests_qt.test_no_old_name import old_names
    w, _ = build()
    for stage in STAGES:
        go_to(w, stage)
        strings = [x for x in ([w.windowTitle(), w.accessibleName(), w.accessibleDescription()]
                               + [c.text() for c in w.findChildren(QLabel)]
                               + [c.text() for c in w.findChildren(QAbstractButton)]) if x]
        for text in strings:
            assert old_names(text) is None, (stage, text)
            assert not re.search(r"\blimit|armeni", text, re.I), (stage, text)


def test_no_widget_has_a_fixed_pixel_height_except_buttons_and_the_bar(build):
    """So that it looks the same at 100 and 150 percent: only the buttons (36) and the 6 px bar are fixed."""
    w, _ = build()
    go_to(w, "failed with a log")
    for child in w.findChildren(QWidget):
        if child.minimumHeight() and child.minimumHeight() == child.maximumHeight():
            assert isinstance(child, (QAbstractButton, QProgressBar, QLabel)), child
            if isinstance(child, QLabel):
                assert child is w.tile                                      # the 40 px icon tile
            elif isinstance(child, QProgressBar):
                assert child.height() == 6
            else:
                assert child.height() == 36


def test_renders_of_every_stage(build):
    """With GUSAN_SHOTS_DIR set: one picture per stage (and no crash painting any of them)."""
    folder = os.environ.get("GUSAN_SHOTS_DIR", "")
    scale = os.environ.get("QT_SCALE_FACTOR", "1")
    suffix = "" if scale in ("1", "1.0") else "_%d" % round(float(scale) * 100)
    w, _ = build()
    for stage in STAGES:
        go_to(w, stage)
        image = w.grab()
        assert not image.isNull() and image.width() > 0
        if folder:
            os.makedirs(folder, exist_ok=True)
            image.save(os.path.join(folder, "update_%s%s.png" % (re.sub(r"\W+", "_", stage), suffix)))
