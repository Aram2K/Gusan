"""A screen-aware conversation, captured only on open or an explicit retake."""
import base64
import sys
import threading

from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtGui import QImage, QPixmap, QKeySequence, QTextDocument, QShortcut
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QPlainTextEdit, QScrollArea, QTextBrowser)

import foreground
import action_api
import mac_permissions
import screen_chat
from live_context import safe_answer_markdown
from ui import glass, theme, live_assist
from ui.components.surfaces import Card, label


class _Input(QPlainTextEdit):
    submitted = Signal()
    image_pasted = Signal(QImage)
    _IMAGE_FILES = live_assist._AskEdit._IMAGE_FILES
    _clipboard_image = live_assist._AskEdit._clipboard_image

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Paste):
            img = self._clipboard_image()
            if img is not None and not img.isNull():
                self.image_pasted.emit(img)
                return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class _Answer(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QTextBrowser.NoFrame)
        self.setStyleSheet("background: transparent; border: none; padding: 0px; color: #1e293b;")
        self.setFont(theme.font(14))
        self.document().setDocumentMargin(0)

    def loadResource(self, resource_type, url):
        return None

    def render(self, text):
        self.document().setMarkdown(safe_answer_markdown(text), QTextDocument.MarkdownDialectGitHub)
        self._fit()

    def _fit(self):
        self.document().setTextWidth(max(100, self.viewport().width()))
        self.setFixedHeight(max(26, int(self.document().size().height()) + 6))
        self.verticalScrollBar().setValue(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()


class ScreenChatWindow(QWidget):
    partial = Signal(int, str)
    finished = Signal(int, str, str)

    def __init__(self, app, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.app = app
        self.history = []
        self.image_b64 = ""
        self.target = None
        self.capture_failed = False
        self._monitor = None
        self._monitor_name = "Current monitor"
        self._generation = 0
        self._busy = False
        self._drag = None
        self._answer = None
        self._answers = []
        self._pending = ""
        self._retaking = False
        self.setWindowTitle("Screen chat")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(420, 560)
        self.setMinimumSize(380, 460)
        theme.apply(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        self.panel = Card(self, pad=(18, 16), gap=12)
        self.panel.setStyleSheet(
            'QFrame[gusan="card"] {background: qlineargradient(x1:0,y1:0,x2:1,y2:1,'
            'stop:0 rgba(255,255,255,248),stop:1 rgba(241,245,249,248));'
            'border:1px solid #cbd5e1; border-radius:18px;} QLabel {color:#475569;}')
        root.addWidget(self.panel)
        head = QHBoxLayout()
        self.title = label("Screen chat", "h3")
        head.addWidget(self.title)
        head.addStretch()
        self.new_button = self._button("New chat", self.new_chat)
        head.addWidget(self.new_button)
        self.close_button = self._button("×", self.close)
        self.close_button.setAccessibleName("Close Screen chat")
        self.close_button.setFixedWidth(30)
        head.addWidget(self.close_button)
        self.panel.add(head)

        self.banner = label("", "hint", wrap=True)
        self.banner.setTextFormat(Qt.PlainText)
        self.banner.setStyleSheet("color:#92400e; background:#fffbeb; border-radius:8px; padding:8px;")
        self.banner.hide()
        self.panel.add(self.banner)
        self.permission_button = self._button("Open Screen Recording settings", self._open_permission)
        self.permission_button.hide()
        self.panel.add(self.permission_button)

        shot = QHBoxLayout()
        self.thumbnail = QLabel()
        self.thumbnail.setFixedSize(52, 38)
        self.thumbnail.setStyleSheet("background:#e2e8f0; border-radius:6px;")
        shot.addWidget(self.thumbnail)
        self.shot_label = label("", "hint", wrap=True)
        self.shot_label.setTextFormat(Qt.PlainText)
        shot.addWidget(self.shot_label, 1)
        self.panel.add(shot)
        tools = QHBoxLayout()
        self.retake_button = self._button("Add screenshot", self.retake)
        self.remove_button = self._button("Remove", self.remove_image)
        tools.addWidget(self.retake_button)
        tools.addWidget(self.remove_button)
        tools.addStretch()
        self.panel.add(tools)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget {background:transparent;}")
        self.conversation = QWidget()
        self.turns = QVBoxLayout(self.conversation)
        self.turns.setContentsMargins(0, 0, 3, 0)
        self.turns.setSpacing(12)
        self.turns.addStretch()
        self.scroll.setWidget(self.conversation)
        self.panel.layout().addWidget(self.scroll, 1)
        self.quick = QWidget()
        quick_layout = QVBoxLayout(self.quick)
        quick_layout.setContentsMargins(0, 0, 0, 0)
        quick_layout.setSpacing(6)
        quick_layout.addWidget(label("What can I help with?", "label"))
        for text in ("Reply to this", "Summarize this page", "Explain this error", "What's in this image?"):
            quick_layout.addWidget(self._button(text, lambda _=False, q=text: self._quick_send(q)))
        self.turns.insertWidget(0, self.quick)

        self.input = _Input()
        self.input.setPlaceholderText("Ask about your screen…")
        self.input.setAccessibleName("Message to Screen chat")
        self.input.setFixedHeight(70)
        self.input.setStyleSheet("background:#ffffff; color:#1e293b; border:1px solid #94a3b8;"
                                 "border-radius:10px; padding:8px; selection-background-color:#dbeafe;")
        self.input.setFont(theme.font(14))
        self.input.submitted.connect(self.send)
        self.input.image_pasted.connect(self.attach_image)
        self.panel.add(self.input)
        footer = QHBoxLayout()
        footer.addWidget(label("Enter to send · Shift+Enter for a new line", "hint"), 1)
        self.send_button = self._button("Send", self.send)
        self.send_button.setStyleSheet("QPushButton {background:#2563eb; color:white; border:0; border-radius:8px; padding:8px 14px;}"
                                       "QPushButton:disabled {background:#e2e8f0; color:#64748b;}")
        footer.addWidget(self.send_button)
        self.panel.add(footer)
        self.partial.connect(self._on_partial)
        self.finished.connect(self._on_finished)
        self.render_timer = QTimer(self)
        self.render_timer.setInterval(100)
        self.render_timer.timeout.connect(self._render_pending)
        self.capture_timer = QTimer(self)
        self.capture_timer.setSingleShot(True)
        self.capture_timer.setInterval(250)
        self.capture_timer.timeout.connect(self._retake_capture)
        self.escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.escape.activated.connect(self.close)
        signal = getattr(app, "sig_setting_changed", None)
        if signal is not None:
            signal.connect(self._setting_changed)
        self._refresh_image()

    def _button(self, text, slot):
        button = QPushButton(text)
        button.setCursor(Qt.PointingHandCursor)
        button.setFont(theme.font(12))
        button.setStyleSheet("QPushButton {color:#334155; background:#f1f5f9; border:1px solid #cbd5e1;"
                             "border-radius:7px; padding:6px 9px;}"
                             "QPushButton:hover {background:#e2e8f0;}"
                             "QPushButton:focus {border:2px solid #2563eb;}"
                             "QPushButton:disabled {color:#64748b; background:#f1f5f9;}")
        button.clicked.connect(slot)
        return button

    def _policy(self):
        permission = mac_permissions.screen_recording_granted()
        return screen_chat.can_capture(self.app.cfg, permission)

    def _apply_policy(self):
        reason = self._policy()
        self.permission_button.setVisible(reason == "mac_permission")
        allowed = reason == "ok"
        self.input.setEnabled(allowed and not self._busy)
        self.send_button.setEnabled(allowed and not self._busy)
        self.retake_button.setEnabled(allowed and not self._busy)
        self.remove_button.setEnabled(allowed and not self._busy and bool(self.image_b64 or self.capture_failed))
        self.quick.setEnabled(allowed and not self._busy)
        if not allowed:
            self.remove_image()
            self._notice("Privacy Mode is on — Screen chat is off. Nothing was captured."
                         if reason == "privacy" else
                         "Screen Recording is off — allow access to attach your screen.")
        return allowed

    def _setting_changed(self, key):
        if key == "privacy_mode":
            if self.app.cfg.get("privacy_mode"):
                self._generation += 1
                self._busy = False
                self.render_timer.stop()
                if self._answer is not None:
                    self._answer.render("Stopped — Privacy Mode is on.")
            self._notice("")
            self._apply_policy()

    def _notice(self, text):
        self.banner.setText(text)
        self.banner.setVisible(bool(text))

    def _open_permission(self):
        mac_permissions.open_settings(mac_permissions.SCREEN_RECORDING)

    def changeEvent(self, event):
        if (event.type() == QEvent.ActivationChange and self.isActiveWindow()
                and self.permission_button.isVisible()):
            self._notice("")
            self._apply_policy()
        super().changeEvent(event)

    def _pick_monitor(self):
        rect = foreground.monitor_rect(self.target)
        if rect:
            x, y, w, h = rect
            for screen in QApplication.screens():
                geometry = screen.geometry()
                scale = screen.devicePixelRatio()
                if (abs(geometry.x() * scale - x) < 4 and abs(geometry.y() * scale - y) < 4
                        and abs(geometry.width() * scale - w) < 4):
                    return screen
        return live_assist.screen_to_capture(self.screen())

    def open_chat(self, target=None):
        if self._retaking:
            return
        if self.isVisible():
            self.raise_()
            self.activateWindow()
            return
        self.target = target
        self._monitor = self._pick_monitor()
        self._notice("")
        if self._apply_policy():
            self._capture()
        geometry = self._monitor.availableGeometry()
        self.resize(min(420, geometry.width() - 24), min(560, geometry.height() - 24))
        self.move(geometry.right() - self.width() - 16, geometry.top() + 16)
        self._show()

    def _show(self):
        glass.exclude_from_capture(self, True)
        self.show()
        glass.exclude_from_capture(self, True)
        glass.round_corners(self)
        self.raise_()
        self.activateWindow()
        if self.input.isEnabled():
            self.input.setFocus()

    def _capture(self):
        if not self._apply_policy():
            return
        self._monitor_name = self._monitor.name() or "Current monitor"
        self.image_b64 = live_assist.capture_screen_b64(self._monitor)
        self.capture_failed = not bool(self.image_b64)
        self._refresh_image()
        if self.capture_failed:
            self._notice("Couldn't capture the screen. Retake, check screen permissions, or Remove to chat without it.")

    def retake(self):
        if self._busy or not self._apply_policy():
            return
        self._retaking = True
        self.hide()
        self.capture_timer.start()

    def _retake_capture(self):
        self._retaking = False
        self._notice("")
        self._capture()
        self._show()

    def remove_image(self):
        self.image_b64 = ""
        self.capture_failed = False
        self._notice("")
        self._refresh_image()

    def attach_image(self, image):
        if self._busy or not self._apply_policy():
            return
        self.image_b64 = live_assist.jpeg_b64(image)
        self.capture_failed = False
        self._monitor_name = "Clipboard image"
        self._refresh_image()

    def _refresh_image(self):
        pixmap = QPixmap()
        if self.image_b64:
            pixmap.loadFromData(base64.b64decode(self.image_b64))
        self.thumbnail.setPixmap(pixmap.scaled(52, 38, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                                 if not pixmap.isNull() else pixmap)
        self.thumbnail.setVisible(bool(self.image_b64))
        self.shot_label.setText("Screenshot attached · " + self._monitor_name if self.image_b64 else
                                "No screenshot — the AI won't see your screen")
        self.retake_button.setText("Retake" if self.image_b64 else "Add screenshot")
        self.remove_button.setVisible(bool(self.image_b64) or self.capture_failed)
        self.remove_button.setEnabled(not self._busy and bool(self.image_b64 or self.capture_failed))

    def new_chat(self):
        self._generation += 1
        self._busy = False
        self.render_timer.stop()
        self.history.clear()
        self._answers.clear()
        self._answer = None
        while self.turns.count() > 2:
            item = self.turns.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        self.quick.show()
        self.input.clear()
        self.retake()

    def _quick_send(self, question):
        self.input.setPlainText(question)
        self.send()

    def _add_question(self, text):
        bubble = label(text, "body", wrap=True, selectable=True)
        bubble.setTextFormat(Qt.PlainText)
        bubble.setStyleSheet("color:#1e3a8a; background:#dbeafe; border-radius:10px; padding:10px;")
        self.turns.insertWidget(self.turns.count() - 1, bubble)

    def _add_answer(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(8)
        layout.addWidget(label("GUSAN", "eyebrow"))
        answer = _Answer()
        answer.render("Thinking…")
        layout.addWidget(answer)
        self.turns.insertWidget(self.turns.count() - 1, box)
        self._answer_box = box
        self._answer = answer
        self._answers.append(answer)

    def send(self):
        if self._busy or not self._apply_policy():
            return
        question = self.input.toPlainText().strip()
        if not question:
            return
        self._notice("")
        self.quick.hide()
        self._add_question(question)
        self._add_answer()
        self.input.clear()
        self._busy = True
        self._generation += 1
        generation = self._generation
        history = list(self.history)
        image, failed = self.image_b64, self.capture_failed
        self._pending = ""
        self._current_question = question
        self._apply_policy()
        self.render_timer.start()

        def worker():
            accumulated = []

            def token(delta):
                if generation != self._generation:
                    raise screen_chat.ScreenChatError("cancelled", "Stopped")
                if isinstance(delta, action_api.ReplaceText):
                    accumulated[:] = [str(delta)]
                else:
                    accumulated.append(delta)
                self.partial.emit(generation, "".join(accumulated))

            try:
                model, config = self.app._resolve_action_engine(allow_live_session=False)
                text = screen_chat.run_turn(history, question, cfg=config, model=model,
                                            image_b64=image, on_token=token, capture_failed=failed,
                                            mac_perm_status=mac_permissions.screen_recording_granted())
                self.finished.emit(generation, text, "")
            except Exception as exc:
                self.finished.emit(generation, "", str(exc)[:400])

        threading.Thread(target=worker, daemon=True).start()

    def _on_partial(self, generation, text):
        if generation == self._generation and self._busy:
            self._pending = text

    def _render_pending(self):
        if self._answer is not None and self._pending:
            self._answer.render(self._pending)
            self._scroll_bottom()

    def _on_finished(self, generation, text, error):
        if generation != self._generation:
            return
        self.render_timer.stop()
        self._busy = False
        self._answer.render(error or text)
        if not error:
            self.history.extend([{"role": "user", "text": self._current_question, "status": "done"},
                                 {"role": "assistant", "text": text, "status": "done"}])
            buttons = QHBoxLayout()
            buttons.addWidget(self._button("Copy", lambda: self.copy_answer(text)))
            name = self.target.name if self.target else "previous app"
            buttons.addWidget(self._button("Paste into " + name, lambda: self.paste_answer(text)))
            buttons.addStretch()
            self._answer_box.layout().addLayout(buttons)
        self._apply_policy()
        self._scroll_bottom()
        self.input.setFocus()

    def _scroll_bottom(self):
        QTimer.singleShot(0, self, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def copy_answer(self, text):
        QApplication.clipboard().setText(text)
        self._notice("Copied to clipboard")

    def paste_answer(self, text):
        self.hide()
        if not foreground.restore(self.target):
            QApplication.clipboard().setText(text)
            self._notice("Copied — press " + ("Cmd+V" if sys.platform == "darwin" else "Ctrl+V"))
            self._show()
            return
        QTimer.singleShot(150, lambda: self.app.paste_text(text))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        self.capture_timer.stop()
        self._retaking = False
        self.hide()
        event.ignore()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() < 65:
            self._drag = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event):
        self._drag = None
