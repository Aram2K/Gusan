"""The invited friend's side of Invite friends: "Got a friend's code?" on the Invite page and on Account & plan -
any time, once: an account can enter a friend's code whenever it likes until it has joined (the "me" answer's
can_join). Home has no invite card (the founder, 2026-11-20: "it's enough on the Invite friends page and Account").

CodeJoinCard, while the account can still join:

* found - the first time it is shown it asks the server once (InviteHub.ask_hint) whether this person opened a
  friend's link; when so: "<Name> invited you" (or "A friend invited you"), a primary "Join <Name>'s invite" and a
  small "Not me" link that switches to the field;
* code - otherwise (or after Not me): "Got a friend's code?" with the field (a code or a link, any case), Paste
  (reads the clipboard only when pressed) and Join;
* joined - after joining: "You joined <Name>'s invite. Pro is on for 2 weeks." (kept for the run: the plan change
  that follows redraws the page, and the card says it again).

Every join error is one plain sentence (invites.join_error). Calls go through ui/invite_hub.py (a worker); nothing
here touches the network.

    card = CodeJoinCard(hub, parent)       # the Invite page and Account & plan make one while can_join
    card.mode                              # "found" | "code" | "joined"
"""

import logging

from PySide6.QtCore import QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

import invites
from ui import theme
from ui.components import Button, Card, icons
from ui.components import celebrate
from ui.components.celebrate import motion
from ui.pages.invite import tinted

logger = logging.getLogger("transcribe")

PLACEHOLDER = "Code or link"                    # (no example: the founder, 2026-11-20)
CODE_TITLE = "Got a friend's code?"
CODE_SUB = "Enter it once, any time, and get 2 weeks of Pro."


def joined_text(name):
    """"You joined Aram's invite. Pro is on for 2 weeks." - or, when the server keeps the name, "the invite"."""
    if name:
        return "You joined %s's invite. Pro is on for 2 weeks." % name
    return "You joined the invite. Pro is on for 2 weeks."


def found_texts(name):
    """(title, line, button) for an invite the server found."""
    if name:
        return ("%s invited you" % name, "Join and get 2 weeks of Pro - %s gets closer to a free month." % name,
                "Join %s's invite" % name)
    return ("A friend invited you", "Join and get 2 weeks of Pro - your friend gets closer to a free month.",
            "Join the invite")


class _Badge(QWidget):
    """A round lead mark: the inviter's letter avatar, the gift tile, or the green check (it pops in)."""

    def __init__(self, kind="gift", size=36, name="", parent=None):
        super().__init__(parent)
        self.kind, self.name = kind, name
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._t = 1.0
        self.anim = QVariantAnimation(self)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setDuration(500)
        self.anim.valueChanged.connect(self._tick)

    def set_kind(self, kind, name="", pop=False):
        self.kind, self.name = kind, name
        if pop and motion():
            self._t = 0.0
            self.anim.start()
        self.update()

    def _tick(self, v):
        self._t = float(v)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = float(self.width())
        r = QRectF(0, 0, s, s)
        if self._t < 1.0:
            from ui.pages.invite import _pop
            k = _pop(self._t)
            p.translate(r.center())
            p.scale(k, k)
            p.translate(-r.center())
        p.setPen(Qt.NoPen)
        isz = int(s / 2)
        at = QPointF(r.center().x() - isz / 2.0, r.center().y() - isz / 2.0)
        if self.kind == "avatar":
            celebrate.paint_friend(p, r, (self.name or "")[:1], self.name, 4)     # no name: the neutral avatar
        elif self.kind == "check":
            p.setBrush(theme.qcolor("ok"))
            p.drawEllipse(r)
            p.drawPixmap(at, icons.pixmap("check", isz, "on-accent", sw=3))
        else:
            p.setBrush(theme.qcolor("pro-sub"))
            p.drawRoundedRect(r, 10, 10)
            p.drawPixmap(at, icons.pixmap("gift", isz, "pro-tx", sw=1.9))
        p.end()


class JoinBox(QWidget):
    """The field (a code or a link) with Paste and Join, and the error line under it."""

    def __init__(self, hub, parent=None, on_joined=None):
        super().__init__(parent)
        self.hub = hub
        self.on_joined = on_joined
        self._busy = False
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        self.field = QLineEdit(self)
        self.field.setPlaceholderText(PLACEHOLDER)
        self.field.setAccessibleName("Invite link or code")
        self.field.setFixedHeight(36)
        self.field.setFont(theme.font(13.5))
        self.field.addAction(icons.icon("link", 16, "muted", sw=1.8), QLineEdit.LeadingPosition)
        self.field.returnPressed.connect(self.join)
        self.field.textChanged.connect(lambda _t: self.set_error(""))
        col.addWidget(self.field)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.paste_button = Button("Paste", "secondary", "s", icon="clipboard", parent=self)
        self.paste_button.clicked.connect(self.paste)
        self.join_button = Button("Join", "primary", "s", parent=self)
        self.join_button.clicked.connect(self.join)
        row.addWidget(self.paste_button)
        row.addWidget(self.join_button)
        row.addStretch(1)
        col.addLayout(row)
        self.error = tinted("", 12.5, 400, "red-tx", parent=self, wrap=True)
        self.error.hide()
        col.addWidget(self.error)

    def paste(self):
        """The clipboard is read here only - when Paste is pressed."""
        try:
            text = QGuiApplication.clipboard().text() or ""
        except Exception:
            text = ""
        code = invites.parse_code(text)
        self.field.setText(text.strip() if not code else (invites.link_for(code) if "/" in text else code))
        self.field.setFocus(Qt.OtherFocusReason)
        if text.strip() and not code:
            self.set_error(invites.join_error("bad_code"))

    def set_error(self, text):
        self.error.setText(text or "")
        self.error.setVisible(bool(text))

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.join_button.setEnabled(not busy)
        self.join_button.setText("Joining…" if busy else "Join")
        self.field.setEnabled(not busy)

    def join(self, source="paste", code=None):
        if self._busy:
            return
        code = invites.parse_code(code if code is not None else self.field.text())
        if not code:
            self.set_error(invites.join_error("empty" if not self.field.text().strip() else "bad_code"))
            return
        self.set_busy(True)
        self.set_error("")
        self.hub.join(code, source if source == "hint" else "paste", then=self._done)

    def _done(self, res):
        try:
            self.set_busy(False)
            if res.get("ok"):
                if callable(self.on_joined):
                    self.on_joined(res)
                return
            self.set_error(invites.join_error(res.get("reason")))
        except RuntimeError:
            pass                                    # the card was closed meanwhile


class CodeJoinCard(Card):
    """"Got a friend's code?" - or, when the server found the friend's invite, "<Name> invited you" with a Join and
    a "Not me". See the module notes. ``on_joined(text)`` is called after a join."""

    def __init__(self, hub, parent=None, on_joined=None):
        super().__init__(parent, pad=(20, 18), gap=12)
        self.hub = hub
        self.on_joined = on_joined
        self.mode = ""
        self._not_me = False                    # "Not me": the field, whatever the hint says
        self._asked = False
        top = QHBoxLayout()
        top.setSpacing(12)
        self.badge = _Badge("gift", 36, "", self)
        top.addWidget(self.badge, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = tinted(CODE_TITLE, 14, 600, "ink", parent=self, wrap=True)
        col.addWidget(self.title)
        self.sub = tinted(CODE_SUB, 12.5, 400, "muted", parent=self, wrap=True)
        col.addWidget(self.sub)
        top.addLayout(col, 1)
        self.add(top)
        # found: one Join for the invite the server saw, and a small "Not me"
        self.found_row = QWidget(self)
        fr = QHBoxLayout(self.found_row)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.setSpacing(12)
        self.join_found = Button("Join the invite", "primary", "s", icon="gift", parent=self.found_row)
        self.join_found.clicked.connect(self.join_hint)
        fr.addWidget(self.join_found)
        self.not_me_button = Button("Not me", "link", "s", parent=self.found_row)
        self.not_me_button.setAccessibleName("Not me: enter a code instead")
        self.not_me_button.clicked.connect(self.not_me)
        fr.addWidget(self.not_me_button)
        fr.addStretch(1)
        self.add(self.found_row)
        self.found_error = tinted("", 12.5, 400, "red-tx", parent=self, wrap=True)
        self.found_error.hide()
        self.add(self.found_error)
        # code: the field
        self.box = JoinBox(hub, parent=self, on_joined=self._joined)
        self.add(self.box)
        if hub.joined_now() is not None:        # drawn again after a join (the plan changed): keep the answer
            self._show_joined(hub.joined_now(), pop=False)
        else:
            hint = hub.hint()
            self._show_found(hint) if (hint or {}).get("found") else self._show_code()

    # ── states ──
    def _show_code(self):
        self.mode = "code"
        self.badge.set_kind("gift")
        self.title.setText(CODE_TITLE)
        self.sub.setText(CODE_SUB)
        self.found_row.hide()
        self.found_error.hide()
        self.box.show()
        self.setAccessibleName(CODE_TITLE)

    def _show_found(self, hint):
        name = (hint or {}).get("name") or ""
        title, line, button = found_texts(name)
        self.mode = "found"
        self.badge.set_kind("avatar", name)
        self.title.setText(title)
        self.sub.setText(line)
        self.join_found.setText(button)
        self.join_found.setAccessibleName(button)
        self.join_found.setEnabled(True)
        self.found_row.show()
        self.box.hide()
        self.setAccessibleName(title)

    def _show_joined(self, name, pop=True):
        self.mode = "joined"
        self.badge.set_kind("check", pop=pop)
        self.title.setText(joined_text(name))
        self.sub.setText(("Thanks - %s gets closer to a free month." % name) if name
                         else "Thanks - your friend gets closer to a free month.")
        self.found_row.hide()
        self.found_error.hide()
        self.box.hide()
        self.setAccessibleName(self.title.text())

    # ── the hint: asked once, the first time the card is on screen ──
    def showEvent(self, e):
        super().showEvent(e)
        if not self._asked and self.mode == "code":
            self._asked = True
            # (the server is asked once a run per account; an answer that landed meanwhile comes back at once)
            self.hub.ask_hint(self._on_hint)

    def _on_hint(self, hint):
        try:
            if self.mode != "code" or self._not_me or self.box.field.text().strip():
                return                          # already typing, or said "Not me": leave the field be
            if (hint or {}).get("found"):
                self._show_found(hint)
        except RuntimeError:
            pass                                # the page went away meanwhile

    # ── actions ──
    def not_me(self):
        self._not_me = True
        self._show_code()
        self.box.field.setFocus(Qt.OtherFocusReason)

    def join_hint(self):
        hint = self.hub.hint() or {}
        self.join_found.setEnabled(False)
        self.join_found.setText("Joining…")
        self.found_error.hide()
        self.hub.join(hint.get("code") or "", "hint", then=self._hint_done)

    def _hint_done(self, res):
        try:
            if res.get("ok"):
                self._joined(res)
                return
            self._show_found(self.hub.hint())
            self.found_error.setText(invites.join_error(res.get("reason")))
            self.found_error.show()
        except RuntimeError:
            pass

    def _joined(self, res):
        name = res.get("name") or self.hub.joined_name() or ""
        self._show_joined(name)
        if callable(self.on_joined):
            self.on_joined(joined_text(name))


AccountJoinCard = CodeJoinCard                   # (its first name: Account & plan's box)

__all__ = ["CodeJoinCard", "AccountJoinCard", "JoinBox", "joined_text", "found_texts"]
