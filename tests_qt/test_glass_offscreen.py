"""ui.glass off the Cocoa platform: no NSWindow to message (it crashed the offscreen tests on a Mac)."""
from unittest import mock

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from ui import glass


def test_offscreen_has_no_ns_window_and_never_messages_one(qapp):
    assert QGuiApplication.platformName() == "offscreen"
    w = QWidget()
    w.show()
    try:
        with mock.patch.object(glass, "_objc_runtime", side_effect=AssertionError("messaged")):
            assert glass._ns_window(w) is None
            if glass.IS_MAC:
                assert glass.exclude_from_capture(w) is False
    finally:
        w.close()


@pytest.mark.skipif(not glass.IS_MAC, reason="NSWindow is macOS only")
def test_cocoa_still_asks_the_ns_view_for_its_window(qapp):
    w = QWidget()
    rt = {"sel": lambda name: name, "id": mock.Mock(return_value=1234)}
    with mock.patch.object(glass, "_cocoa", return_value=True), \
            mock.patch.object(glass, "_objc_runtime", return_value=rt), \
            mock.patch.object(w, "winId", return_value=99):
        assert glass._ns_window(w) == 1234
    rt["id"].assert_called_once_with(99, b"window")
