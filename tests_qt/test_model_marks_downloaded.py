"""A local model already on this computer shows a check where a missing one shows its download icon, so every
row's chips (LOCAL, PRO...) line up (the founder, 2026-11-21)."""
from PySide6.QtCore import Qt


def _box(qapp):
    from PySide6.QtWidgets import QWidget
    from ui.components.model_combo import PlainNameCombo, combo
    win = QWidget()
    box = combo(PlainNameCombo(win), "Transcribes with", width=220)
    box.enable_row_marks()
    box.add_choice("Whisper Tiny", "tiny", plain="Lightest")
    box.add_choice("Whisper Base", "base", plain="Fast")
    box._test_host = win                     # (kept alive with the box)
    return box


def test_a_downloaded_model_gets_the_check_mark_and_says_so(qapp):
    from ui.components.model_combo import MARK_DOWNLOAD, MARK_DOWNLOADED, MARK_ROLE
    box = _box(qapp)
    box.set_choice_mark(0, MARK_DOWNLOAD, note="75 MB")
    box.set_choice_mark(1, MARK_DOWNLOADED)
    assert box.itemData(1, MARK_ROLE) == MARK_DOWNLOADED
    assert box.itemData(1, Qt.ToolTipRole) == "On this computer"
    assert "on this computer" in str(box.itemData(1, Qt.AccessibleTextRole))
    assert "Not downloaded yet (75 MB)" == box.itemData(0, Qt.ToolTipRole)


def test_the_check_takes_exactly_the_download_icons_place(qapp):
    from ui.components import model_combo as mc
    box = _box(qapp)
    delegate = mc._RowDelegate(box)
    down = delegate.mark_pixmap(mc.MARK_DOWNLOAD)
    done = delegate.mark_pixmap(mc.MARK_DOWNLOADED)
    assert down is not None and done is not None
    assert delegate.mark_width(mc.MARK_DOWNLOAD) == delegate.mark_width(mc.MARK_DOWNLOADED)
    assert mc.MARK_DOWNLOAD in mc._ICON_MARKS and mc.MARK_DOWNLOADED in mc._ICON_MARKS
