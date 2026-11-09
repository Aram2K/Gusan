"""Dropdown lists open without a jolt (ui/popup_corners.py). The founder (2026-11-09): the list was "vibrating at the
beginning and getting out of the scope while opening" - Windows' roll-down animation, and the list's border dropped
in the middle of opening (its size changed while it was being shown)."""

from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox

from ui import popup_corners


def test_lists_open_in_place_without_the_roll_down_animation(qapp):
    popup_corners.install(qapp)
    assert not QApplication.isEffectEnabled(Qt.UI_AnimateCombo)


def test_the_border_is_settled_before_the_list_first_shows(qapp):
    popup_corners.install(qapp)
    with mock.patch.object(popup_corners, "windows_rounds_corners", return_value=True), \
            mock.patch.object(popup_corners, "round_window", return_value=True):
        box = QComboBox()
        box.addItems(["Fast", "Best accuracy"])
        container = box.view().parentWidget()
        assert container.metaObject().className() == popup_corners.CONTAINER
        container.ensurePolished()
        assert "border: none" in container.styleSheet()          # before any Show
        sheet = container.styleSheet()
        box.showPopup()
        QApplication.processEvents()
        assert container.styleSheet() == sheet                   # nothing changes while it opens
        box.hidePopup()
        box.deleteLater()


def test_without_rounded_corners_the_list_keeps_its_own_border(qapp):
    popup_corners.install(qapp)
    with mock.patch.object(popup_corners, "windows_rounds_corners", return_value=False):
        box = QComboBox()
        box.addItems(["a", "b"])
        container = box.view().parentWidget()
        container.ensurePolished()
        assert "border: none" not in container.styleSheet()
        box.deleteLater()
