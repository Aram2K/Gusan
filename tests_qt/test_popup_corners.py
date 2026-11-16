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
    seen = []
    with mock.patch.object(popup_corners, "windows_rounds_corners", return_value=True), \
            mock.patch.object(popup_corners, "round_window", return_value=True):
        box = QComboBox()
        box.addItems(["Fast", "Best accuracy"])
        container = box.view().parentWidget()
        assert container.metaObject().className() == popup_corners.CONTAINER
        from PySide6.QtCore import QEvent, QObject

        class Watch(QObject):
            def eventFilter(self, obj, ev):
                if ev.type() == QEvent.Show:
                    seen.append(obj.styleSheet())                # what the list window has as it first shows
                return False
        watch = Watch()
        container.installEventFilter(watch)
        box.showPopup()
        QApplication.processEvents()
        assert seen and "border: none" in seen[0]                # settled before it showed ...
        sheet = container.styleSheet()
        assert "border: none" in sheet and container.property("gusanCard") is True
        box.hidePopup()
        box.showPopup()                                          # ... and nothing changes when it opens again
        QApplication.processEvents()
        assert container.styleSheet() == sheet
        box.hidePopup()
        box.deleteLater()


def test_every_dropdown_without_an_application_wide_filter(qapp):
    """2026-11-16 speed audit: an application-wide event filter put every event of every widget through Python
    (pages built about twice as slowly). A subclass that opens its own list (ModelCombo) gets the same card."""
    popup_corners.install(qapp)
    with mock.patch.object(popup_corners, "windows_rounds_corners", return_value=True), \
            mock.patch.object(popup_corners, "round_window", return_value=True) as rounded:
        class Mine(QComboBox):
            def showPopup(self):
                super().showPopup()
        box = Mine()
        box.addItems(["a", "b"])
        box.showPopup()
        QApplication.processEvents()
        assert rounded.called and box.view().parentWidget().property("gusanCard") is True
        box.hidePopup()
        box.deleteLater()


def test_without_rounded_corners_the_list_keeps_its_own_border(qapp):
    popup_corners.install(qapp)
    with mock.patch.object(popup_corners, "windows_rounds_corners", return_value=False):
        box = QComboBox()
        box.addItems(["a", "b"])
        container = box.view().parentWidget()
        box.showPopup()
        QApplication.processEvents()
        assert "border: none" not in container.styleSheet()
        box.hidePopup()
        box.deleteLater()
