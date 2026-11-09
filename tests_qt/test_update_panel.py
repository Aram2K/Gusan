"""The in-app update, in place: Settings > Updates & about's version row (ui/update_panel.py). The founder
(2026-11-09): "I don't want the download loading to be a separate window popup but in the app, in the same
section where I have the versioning". The controller's side of it (what drives each stage, Cancel, Try again,
the tray notices) is tests/test_update_flow.py and tests_qt/test_wave4_controller.py."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel

from tests_qt.conftest import make_stub_app

MB = 2 ** 20


def _spin(n=5):
    for _ in range(n):
        QApplication.processEvents()


@pytest.fixture
def page(qapp, monkeypatch):
    import ui.winfit
    from ui.pages import settings as S
    from ui.shell import MainWindow
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    monkeypatch.setattr(S, "autostart_state", lambda: (True, True))
    app = make_stub_app("free")
    app.cfg["pending_update_version"] = "v9.9.9"
    win = MainWindow(app)
    win.resize(1280, 820)
    win.show()
    p = win.open("settings", section="updates")
    p.ensure_built()
    _spin()
    p.update_panel.set_tag("v9.9.9")
    yield p
    win.hide()
    win.deleteLater()
    _spin()


def _shown_text(widget):
    return [w.text() for w in widget.findChildren(QLabel) if w.isVisible() and w.text().strip()] + \
           [b.text() for b in widget.findChildren(QAbstractButton) if b.isVisible() and b.text().strip()]


def test_it_lives_in_the_version_row_never_a_window_of_its_own(page):
    panel = page.update_panel
    assert not panel.isWindow() and panel.window() is page.window()
    assert page.version_row.isAncestorOf(panel) and page.version_row.isAncestorOf(panel.actions)
    assert panel.stage == "idle" and not panel.isVisible() and page.update_button.isVisible()


def test_downloading_shows_the_bar_the_megabytes_and_cancel_in_the_row(page):
    panel = page.update_panel
    panel.show_downloading()
    _spin()
    assert page.update_status.text() == "Downloading 9.9.9"
    assert panel.isVisible() and panel.bar.isVisible() and panel.bar.maximum() == 0      # busy until it knows the size
    assert panel.btn_cancel.isVisible() and not page.update_button.isVisible()           # Cancel takes the button's place
    panel.show_progress(85 * MB, 110 * MB)
    assert panel.lbl_amount.text() == "85 / 110 MB" and panel.bar.value() == 77
    panel.show_progress(5 * MB, 0)                                                        # size unknown
    assert panel.lbl_amount.text() == "5 MB" and panel.bar.maximum() == 0


def test_at_100_percent_it_checks_and_progress_outside_a_download_is_ignored(page):
    panel = page.update_panel
    panel.show_downloading()
    panel.show_progress(100 * MB, 100 * MB)
    assert page.update_status.text() == "Checking the download"
    panel.show_progress(50 * MB, 100 * MB)                                                # a late report
    assert page.update_status.text() == "Checking the download"
    panel.show_installing()
    panel.show_progress(10 * MB, 100 * MB)
    assert panel.stage == "installing" and panel.lbl_amount.text() == ""


def test_waiting_says_why_and_a_typed_key_offers_install_now(page):
    panel = page.update_panel
    panel.show_waiting()
    _spin()
    assert page.update_status.text() == "Ready to install"
    assert panel.lbl_detail.text().replace("\u200b", "") == "It starts when your recording or file is done."
    assert panel.btn_cancel.isVisible() and not panel.btn_install.isVisible()
    panel.show_waiting(can_install=True)
    _spin()
    assert panel.btn_install.isVisible() and panel.btn_cancel.isVisible()
    shown = panel.lbl_detail.text()
    panel.show_waiting(can_install=True)                                                  # asked every 2 s
    assert panel.lbl_detail.text() is not None and panel.lbl_detail.text() == shown


def test_installing_has_no_buttons_and_says_gusan_reopens(page):
    panel = page.update_panel
    panel.show_installing()
    _spin()
    assert page.update_status.text() == "Installing 9.9.9"
    assert "opens again by itself" in panel.lbl_detail.accessibleName()
    assert not panel.actions.isVisible() and not page.update_button.isVisible()
    panel.show_installing(reopens=False, admin=True)
    assert "Windows asks for permission first" in panel.lbl_detail.accessibleName()
    assert "Start menu" in panel.lbl_detail.accessibleName()


def test_a_failure_says_why_with_try_again_and_the_download_page(page):
    panel = page.update_panel
    got = []
    panel.retry_requested.connect(lambda: got.append("retry"))
    panel.website_requested.connect(lambda: got.append("page"))
    panel.show_failed("couldn't download the update.", detail="Details: C:\\Temp\\gusan-update\\x.log")
    _spin()
    assert page.update_status.text() == "The update didn't install"
    assert panel.lbl_detail.accessibleName() == "Couldn't download the update."
    assert panel.lbl_note.isVisible() and panel.lbl_note.textInteractionFlags() & Qt.TextSelectableByMouse
    assert panel.lbl_note.copy_text() == "Details: C:\\Temp\\gusan-update\\x.log"           # no invisible characters
    assert not panel.bar.isVisible()
    panel.btn_retry.click()
    panel.btn_website.click()
    assert got == ["retry", "page"]


def test_cancel_asks_the_controller_and_hiding_gives_the_row_back(page):
    panel = page.update_panel
    got = []
    panel.cancel_requested.connect(lambda: got.append("cancel"))
    page._install_update = lambda tag: None
    page._show_update("installing", "v9.9.9")                     # the row's own words when its button was pressed
    panel.show_downloading()
    panel.btn_cancel.click()
    assert got == ["cancel"]
    panel.hide()                                                  # what the controller does on Cancel
    _spin()
    assert panel.stage == "idle" and not panel.isVisible() and not panel.actions.isVisible()
    assert page.update_button.isVisible() and page.update_button.text() == "Install update"
    assert page.update_status.text() == "Update available · v9.9.9"


def test_a_check_that_lands_meanwhile_does_not_cover_the_progress(page):
    import updater
    panel = page.update_panel
    panel.show_downloading()
    page._show_update(updater.LATEST, "")
    page._install_failed("v9.9.9", "The download was damaged. Try again.")
    assert page.update_status.text() == "Downloading 9.9.9"
    assert not page.update_button.isVisible()


def test_a_long_log_path_wraps_inside_the_row(page):
    panel = page.update_panel
    panel.show_failed("The installer stopped before it finished.",
                      "Details: C:/Users/Someone/AppData/Local/Temp/gusan-update/logs/gusan-update-0a1b2c3d4e5f6a7b8c9d.log")
    _spin()
    row = page.version_row
    note = panel.lbl_note
    right = note.mapTo(row, note.rect().topRight()).x()
    assert right <= row.width(), (right, row.width())


def test_every_stage_says_gusan_never_the_old_name_and_no_limit(page):
    panel = page.update_panel
    seen = []
    for go in (panel.show_downloading, lambda: panel.show_progress(4 * MB, 9 * MB), panel.show_waiting,
               lambda: panel.show_waiting(can_install=True), panel.show_installing,
               lambda: panel.show_installing(reopens=False, admin=True),
               lambda: panel.show_failed("Couldn't download the update.", "Details: C:\\x.log")):
        go()
        _spin()
        seen += _shown_text(page.version_row)
    text = " ".join(seen)
    assert "Transcribe" not in text and "limit" not in text.lower()
    for words in ("Downloading 9.9.9", "Ready to install", "Installing 9.9.9", "Try again", "Download page"):
        assert words in text, words
