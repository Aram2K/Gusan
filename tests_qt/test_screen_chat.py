"""The floating chat: capture boundaries, conversation, focus and settings."""
import threading
import time
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QObject, Signal, QCoreApplication, QEvent
from PySide6.QtGui import QImage, QColor, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

import foreground
import screen_chat
from ui import screen_chat as ui


def wait_until(predicate, timeout=10):
    end = time.monotonic() + timeout
    while not predicate() and time.monotonic() < end:
        QApplication.processEvents()
        QTest.qWait(10)
    assert predicate()


class App(QObject):
    sig_setting_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.cfg = {"privacy_mode": False, "screen_chat_hotkey": "ctrl+alt+g",
                    "hotkey": "alt+r", "live_assist_hotkey": "ctrl+alt+a", "smart_actions_hotkey": "alt+s"}
        self.paste_text = Mock()
        self._resolve_action_engine = Mock(return_value=("api_managed", dict(self.cfg)))


@pytest.fixture
def chat(qapp, monkeypatch):
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    image = QImage(160, 100, QImage.Format_RGB32)
    image.fill(QColor("#dbeafe"))
    capture = Mock(return_value=ui.live_assist.jpeg_b64(image))
    monkeypatch.setattr(ui.live_assist, "capture_screen_b64", capture)
    monkeypatch.setattr(ui.glass, "exclude_from_capture", Mock())
    monkeypatch.setattr(ui.glass, "round_corners", Mock())
    app = App()
    window = ui.ScreenChatWindow(app)
    window.target = foreground.Target("win32", 12345, "Mail", 123)
    yield window, capture, app
    window._generation += 1
    window.render_timer.stop()
    window.hide()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_open_captures_once_and_reopen_raises_without_capture(chat):
    w, capture, app = chat
    w.open_chat()
    assert w.isVisible() and w.thumbnail.isVisible()
    assert "Screenshot attached" in w.shot_label.text()
    assert w.windowFlags() & Qt.WindowStaysOnTopHint
    capture.assert_called_once()
    w.open_chat()
    capture.assert_called_once()
    QTest.keyClick(w.input, Qt.Key_Escape)
    assert not w.isVisible()
    w.open_chat()
    assert capture.call_count == 2


def test_privacy_never_calls_capture_or_worker_and_disables_input(chat, monkeypatch):
    w, capture, app = chat
    app.cfg["privacy_mode"] = True
    run = Mock()
    monkeypatch.setattr(screen_chat, "run_turn", run)
    w.open_chat()
    assert w.banner.text() == "Privacy Mode is on — Screen chat is off. Nothing was captured."
    assert not w.input.isEnabled() and not w.send_button.isEnabled()
    w.retake()
    w.new_chat()
    w.input.setPlainText("Reply")
    w.send()
    capture.assert_not_called()
    run.assert_not_called()


def test_privacy_changed_while_open_discards_image_without_new_capture(chat):
    w, capture, app = chat
    w.open_chat()
    app.cfg["privacy_mode"] = True
    app.sig_setting_changed.emit("privacy_mode")
    assert not w.image_b64 and not w.input.isEnabled()
    app.cfg["privacy_mode"] = False
    app.sig_setting_changed.emit("privacy_mode")
    assert w.input.isEnabled() and not w.image_b64
    capture.assert_called_once()


def test_mac_permission_denied_no_capture_settings_button(chat, monkeypatch):
    w, capture, app = chat
    monkeypatch.setattr(screen_chat.sys, "platform", "darwin")
    monkeypatch.setattr(ui.mac_permissions, "screen_recording_granted", lambda: False)
    request = Mock()
    monkeypatch.setattr(ui.mac_permissions, "open_settings", request)
    w.open_chat()
    capture.assert_not_called()
    assert w.permission_button.isVisible() and not w.input.isEnabled()
    w.permission_button.click()
    request.assert_called_once_with(ui.mac_permissions.SCREEN_RECORDING)


def test_unknown_mac_permission_still_attempts_capture(chat, monkeypatch):
    w, capture, app = chat
    monkeypatch.setattr(screen_chat.sys, "platform", "darwin")
    monkeypatch.setattr(ui.mac_permissions, "screen_recording_granted", lambda: None)
    w.open_chat()
    capture.assert_called_once()


def test_remove_retake_hides_until_capture_and_new_chat(chat):
    w, capture, app = chat
    w.open_chat()
    w.remove_button.click()
    assert not w.image_b64
    assert w.shot_label.text() == "No screenshot — the AI won't see your screen"
    assert w.retake_button.text() == "Add screenshot"
    w.retake_button.click()
    assert not w.isVisible() and capture.call_count == 1
    w.open_chat()
    assert not w.isVisible() and capture.call_count == 1
    wait_until(lambda: w.isVisible())
    assert capture.call_count == 2 and w.image_b64
    w._add_question("Old chat")
    w._add_answer()
    w.history.append({"role": "user", "text": "Old chat", "status": "done"})
    w.new_chat()
    wait_until(lambda: w.isVisible())
    assert not w.history and w.quick.isVisible() and w.turns.count() == 2
    assert capture.call_count == 3


def test_capture_failure_removable_to_continue_text_only(chat):
    w, capture, app = chat
    capture.return_value = ""
    w.open_chat()
    assert w.capture_failed and "Couldn't capture" in w.banner.text()
    assert w.remove_button.isVisible() and w.remove_button.isEnabled()
    w.remove_button.click()
    assert not w.capture_failed


def test_stream_runs_off_gui_history_and_text_only_followup(chat, monkeypatch):
    w, capture, app = chat
    calls = []
    release = threading.Event()

    def run(history, question, **kwargs):
        calls.append((history, question, kwargs, threading.current_thread()))
        kwargs["on_token"]("Thanks ")
        if len(calls) == 1:
            release.wait(3)
        kwargs["on_token"]("for the update.")
        return "Thanks for the update."

    monkeypatch.setattr(screen_chat, "run_turn", run)
    w.open_chat()
    w.input.setPlainText("Reply to this")
    QTest.keyClick(w.input, Qt.Key_Return)
    wait_until(lambda: w._pending == "Thanks ")
    assert w._busy and not w.send_button.isEnabled()
    w._render_pending()
    assert "Thanks" in w._answer.toPlainText()
    release.set()
    wait_until(lambda: not w._busy)
    assert len(w.history) == 2 and w.history[-1]["status"] == "done"
    assert calls[0][3] is not threading.main_thread()
    assert calls[0][2]["image_b64"]
    app._resolve_action_engine.assert_called_once_with(allow_live_session=False)
    assert w._answer.toPlainText() == "Thanks for the update."
    buttons = [b.text() for b in w._answer_box.findChildren(QPushButton)]
    assert "Copy" in buttons and "Paste into previous app" in buttons
    w.remove_image()
    w.input.setPlainText("Shorter please")
    w.send()
    wait_until(lambda: not w._busy)
    assert calls[1][0] == w.history[:2]
    assert not calls[1][2]["image_b64"] and not calls[1][2]["capture_failed"]
    capture.assert_called_once()


@pytest.mark.parametrize("error", ["Screen chat needs Gusan Cloud or your own API key.", "Sign in to Gusan.", "Managed request failed"])
def test_worker_error_inline_and_empty_turn_does_nothing(chat, monkeypatch, error):
    w, capture, app = chat
    def fail(*args, **kwargs):
        raise RuntimeError(error)
    run = Mock(side_effect=fail)
    monkeypatch.setattr(screen_chat, "run_turn", run)
    w.open_chat()
    w.send()
    run.assert_not_called()
    w.input.setPlainText("Hello")
    w.send()
    wait_until(lambda: not w._busy)
    assert w._answer.toPlainText() == error
    assert not w.history and w.input.isEnabled()


def test_copy_and_paste_restore_target_or_copy_fallback(chat, monkeypatch):
    w, capture, app = chat
    target = w.target
    w.open_chat(target)
    w.copy_answer("Draft reply")
    assert QApplication.clipboard().text() == "Draft reply"
    restore = Mock(return_value=True)
    monkeypatch.setattr(ui.foreground, "restore", restore)
    w.paste_answer("Ready to paste")
    assert not w.isVisible()
    restore.assert_called_once_with(target)
    wait_until(lambda: app.paste_text.called)
    app.paste_text.assert_called_once_with("Ready to paste")
    restore.return_value = False
    w.paste_answer("Fallback")
    assert w.isVisible() and "Copied — press Ctrl+V" == w.banner.text()
    assert QApplication.clipboard().text() == "Fallback"
    assert app.paste_text.call_count == 1
    capture.assert_called_once()


def test_shift_enter_newline_clipboard_image_replaces_without_capture(chat):
    w, capture, app = chat
    w.open_chat()
    w.input.setPlainText("Line one")
    w.input.moveCursor(QTextCursor.End)
    QTest.keyClick(w.input, Qt.Key_Return, Qt.ShiftModifier)
    assert w.input.toPlainText() == "Line one\n"
    image = QImage(60, 40, QImage.Format_RGB32)
    image.fill(QColor("#f59e0b"))
    QApplication.clipboard().setImage(image)
    previous = w.image_b64
    QTest.keyClick(w.input, Qt.Key_V, Qt.ControlModifier)
    assert w.image_b64 != previous and "Clipboard image" in w.shot_label.text()
    capture.assert_called_once()


def test_settings_row_instant_apply_and_conflict(qapp):
    import settings_apply
    from ui.pages.settings import ScreenChatShortcutRow
    app = App()
    app.save_config = Mock()
    app._setup_screen_chat_hotkey = Mock(return_value=True)
    app.set_setting = lambda key, value: settings_apply.apply(app, {key: value}).get(key, False)
    row = ScreenChatShortcutRow(app)
    row.show()
    from ui.components.controls import Keycap
    caps = row.capture.keys.findChildren(Keycap)
    assert caps and all(cap.height() == 32 and cap.objectName() == "midKey" for cap in caps)
    assert row.capture.button.parentWidget().width() == 150
    QTest.mouseClick(row.capture.keys, Qt.LeftButton)
    QTest.mouseClick(row.capture.button, Qt.LeftButton)
    QApplication.processEvents()
    assert not row.capture.is_capturing()
    row.capture._finish("ctrl+shift+g")
    assert app.cfg["screen_chat_hotkey"] == "ctrl+shift+g"
    app._setup_screen_chat_hotkey.assert_called_with("ctrl+shift+g")
    row.capture._finish("alt+r")
    assert "Dictation" in row.error_label.text()
    assert app.cfg["screen_chat_hotkey"] == "ctrl+shift+g"
    assert row.capture.hotkey() == "ctrl+shift+g"
    row.capture._finish("")
    assert app.cfg["screen_chat_hotkey"] == ""
    row.hide()
    row.deleteLater()


def test_tray_row_hotkey_and_external_target(qapp, monkeypatch):
    from ui.tray_menu import build_tray_menu
    from tests_qt.conftest import make_stub_app
    app = make_stub_app("pro")
    app.open_screen_chat = Mock()
    target = foreground.Target("win32", 12345, "Mail", 123)
    monkeypatch.setattr(foreground, "remember", lambda: target)
    tray = build_tray_menu(app)
    tray._on_about_to_show()
    action = tray.actions["screen_chat"]
    assert "Ask about my screen" in action.text()
    assert "G" in str(action.property("gusanHint"))
    action.trigger()
    app.open_screen_chat.assert_called_once_with(target=target, from_tray=True)
    tray.menu.deleteLater()
