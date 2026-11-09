"""The in-app update, shown in place: Settings > Updates & about, in the version row - never a window of its
own (the founder, 2026-11-09: "not a separate window popup but in the app, in the same section where I have
the versioning"). It shows every stage from the first click to the restart, so an update never looks like
nothing happened:

    Gusan 2.1.1                                              [Cancel]
    ● Downloading 2.1.3
    [===============--------]  85 / 110 MB

The work itself is updater.py; main.AppController.install_update drives this panel through its stages (the
same calls the old "Updating Gusan" window had) and listens to its four signals:

    panel.cancel_requested      # Cancel while downloading / waiting
    panel.install_requested     # "Install now" (waiting for a typed key to be saved)
    panel.retry_requested       # "Try again" (failed)
    panel.website_requested     # "Download page" (failed)

    panel.show_downloading()                 # busy bar, Cancel
    panel.show_progress(done, total)         # "45 / 100 MB"; at 100 percent "Checking the download"
    panel.show_waiting(text=None, can_install=False)     # downloaded; waits for a recording / a typed key
    panel.show_installing(reopens=True, admin=False)     # no buttons; Gusan closes in a moment
    panel.show_failed("Couldn't download the update.", detail="Details: C:\\...\\x.log")
    panel.hide()                             # back to the row's own "Up to date" / "Install update"

``stage`` is "idle", "downloading", "waiting", "installing" or "failed"; ``active()`` is True outside idle.

How it behaves:
* It borrows the row's status line, its dot and its button (the page passes them): while it is active the
  line says the stage, the dot is blue (red after a failure) and the row's own button gives way to the
  panel's buttons. Back to idle it gives them back and calls ``on_idle`` (the page shows its own status again).
* Built without a page (the Settings page could not be built) it still works, unseen: the attempt runs and
  the controller's tray notices say how it ended.
* Progress is asked up to 10 times a second: a label changes only when its words do (no flicker, and a
  screen reader hears changes only). The same "waiting" words asked every 2 s do nothing.
"""

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMenu, QProgressBar, QVBoxLayout, QWidget

from app_info import APP_NAME
from ui import theme
from ui.components.buttons import Button
from ui.components.mini_prompt import breakable

logger = logging.getLogger("transcribe")

# Everything the panel says, in one place.
# The status line stays short (it sits next to the row's dot); what it means goes on the line under the bar.
DOWNLOADING = "Downloading %s"
DOWNLOADING_PLAIN = "Downloading the update"
CHECKING = "Checking the download"
READY = "Ready to install"
WAIT_WORK = "It starts when your recording or file is done."
WAIT_KEY = "Save the key you typed first, or choose Install now."
INSTALLING_LINE = "Installing %s"
INSTALLING_PLAIN = "Installing the update"
INSTALLING = "%s closes now and opens again by itself." % APP_NAME
INSTALLING_NO_REOPEN = "%s closes now. Open it from the Start menu when it's done." % APP_NAME
INSTALLER_WINDOW = "The installer shows its own small window while it works."
ASKS_PERMISSION = "Windows asks for permission first."
FAILED = "The update didn't install"
BREAK = "\u200b"                       # mini_prompt.breakable's invisible place for a line to end

# stage -> (dot colour, status colour)
_COLOURS = {"downloading": ("blue", "blue-tx"), "waiting": ("blue", "blue-tx"),
            "installing": ("blue", "blue-tx"), "failed": ("red", "red-tx")}


def _mb(n):
    return "%.0f" % (n / 2 ** 20)


def _version(tag):
    tag = str(tag or "")
    return tag[1:] if tag[:1] in ("v", "V") else tag


class _PlainCopyLabel(QLabel):
    """A selectable label whose copy is exactly what the person reads: the invisible break characters that
    let a long path wrap are left out (pasted into Explorer they would make the path "not exist")."""

    def copy_text(self):
        text = (self.selectedText() if self.hasSelectedText() else self.text()).replace(BREAK, "")
        QApplication.clipboard().setText(text)
        return text

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy):
            self.copy_text()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("Copy", self.copy_text)
        menu.exec(event.globalPos())


def _small_label(parent, role="muted", selectable=False):
    lb = _PlainCopyLabel(parent) if selectable else QLabel(parent)
    theme.set_role(lb, role)
    lb.setWordWrap(True)
    lb.setTextFormat(Qt.PlainText)
    if selectable:
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lb.setVisible(False)
    return lb


class UpdatePanel(QWidget):
    cancel_requested = Signal()
    retry_requested = Signal()
    install_requested = Signal()
    website_requested = Signal()

    def __init__(self, tag="", status_label=None, dot=None, row_button=None, on_idle=None, parent=None, extras=()):
        super().__init__(parent)
        self._extras = tuple(w for w in (extras or ()) if w is not None)    # hidden while an update runs ("What's new")
        self.tag = ""
        self.stage = "idle"
        self._checking = False
        self._waiting_key = None             # what show_waiting last showed: (text, can_install)
        self._status, self._dot, self._row_button, self._on_idle = status_label, dot, row_button, on_idle
        self.setAccessibleName("Update progress")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(4)
        if self._status is None:             # no row to borrow from: a status line of its own
            self._status = QLabel(self)
            theme.set_role(self._status, "body")
            self._status.setTextFormat(Qt.PlainText)
            lay.addWidget(self._status)
        self.lbl_status = self._status
        bar_row = QHBoxLayout()
        bar_row.setContentsMargins(0, 0, 0, 0)
        bar_row.setSpacing(10)
        self.bar = QProgressBar(self)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setAccessibleName("Update progress")
        bar_row.addWidget(self.bar, 1, Qt.AlignVCenter)
        self.lbl_amount = QLabel(self)       # "85 / 110 MB", right of the bar
        theme.set_role(self.lbl_amount, "muted")
        self.lbl_amount.setTextFormat(Qt.PlainText)
        self.lbl_amount.setVisible(False)
        bar_row.addWidget(self.lbl_amount, 0, Qt.AlignVCenter)
        lay.addLayout(bar_row)
        self.lbl_detail = _small_label(self)
        lay.addWidget(self.lbl_detail)
        self.lbl_note = _small_label(self, selectable=True)     # a failure's log file: can be copied
        lay.addWidget(self.lbl_note)

        # The panel's buttons take the row's button place (the page puts ``actions`` next to it).
        self.actions = QWidget(parent)
        al = QHBoxLayout(self.actions)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(8)
        self.btn_website = Button("Download page", "link", "s", parent=self.actions)
        self.btn_website.setAccessibleName("Open the download page")
        self.btn_website.clicked.connect(self.website_requested)
        self.btn_cancel = Button("Cancel", "secondary", "s", parent=self.actions)
        self.btn_cancel.setAccessibleName("Cancel the update")
        self.btn_cancel.clicked.connect(self._cancel_clicked)
        self.btn_install = Button("Install now", "primary", "s", parent=self.actions)
        self.btn_install.clicked.connect(self.install_requested)
        self.btn_retry = Button("Try again", "secondary", "s", icon="refresh", parent=self.actions)
        self.btn_retry.clicked.connect(self.retry_requested)
        self._buttons = {"website": self.btn_website, "cancel": self.btn_cancel, "install": self.btn_install,
                         "retry": self.btn_retry}
        for b in self._buttons.values():
            al.addWidget(b, 0, Qt.AlignVCenter)
        self.set_tag(tag)
        self._go_idle(tell=False)

    # ── state ──
    def active(self):
        return self.stage != "idle"

    def set_tag(self, tag):
        """The release this panel installs ("v2.1.3")."""
        self.tag = str(tag or "")

    # ── what is shown ──
    @staticmethod
    def _say(lbl, text):
        if lbl is None:
            return
        text = str(text or "")
        shown = breakable(text)
        if lbl.text() != shown:
            lbl.setText(shown)
            lbl.setAccessibleName(text.strip())
        lbl.setVisible(bool(text))

    def _show(self, stage, status, detail="", note="", buttons=()):
        self.stage = stage
        dot, colour = _COLOURS[stage]
        if self._dot is not None:
            self._dot.set_color(dot)
        st = self._status
        self._say(st, status)
        st.setStyleSheet("QLabel { color: %s; }" % theme.qss_color(colour))
        st.setAccessibleDescription(status)
        self._say(self.lbl_detail, detail)
        self._say(self.lbl_note, note)
        wanted = set(buttons)
        for name, b in self._buttons.items():
            b.setVisible(name in wanted)
        if self._row_button is not None:
            self._row_button.setVisible(False)
        for w in self._extras:
            w.setVisible(False)
        self.actions.setVisible(bool(wanted))
        self.setVisible(True)
        self.setAccessibleDescription(" ".join(x for x in (status, detail, note) if x))

    def _busy(self):
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self._say(self.lbl_amount, "")

    def _full(self):
        self.bar.setVisible(True)
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self._say(self.lbl_amount, "")

    def _downloading_words(self):
        version = _version(self.tag)
        return DOWNLOADING % version if version else DOWNLOADING_PLAIN

    def show_downloading(self):
        self._checking = False
        self._waiting_key = None
        self._busy()
        self._show("downloading", self._downloading_words(), buttons=("cancel",))

    def show_progress(self, done, total):
        """The download's progress. ``total`` 0 = the size is not known (a busy bar and the MB so far)."""
        if self.stage != "downloading" or self._checking:
            return
        done, total = max(0, float(done or 0)), max(0, float(total or 0))
        if total > 0 and done >= total:
            self._checking = True
            self._busy()
            self._show("downloading", CHECKING, buttons=("cancel",))
            return
        if total > 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(min(99, int(done * 100 / total)))
            self._say(self.lbl_amount, "%s / %s MB" % (_mb(done), _mb(total)))
        else:
            self.bar.setRange(0, 0)
            self._say(self.lbl_amount, "%s MB" % _mb(done))

    def show_waiting(self, text=None, can_install=False):
        """Downloaded and checked, waiting: for a recording or a file to finish (the default words), or for
        a typed API key to be saved (``can_install``: Install now goes on anyway)."""
        key = (text or (WAIT_KEY if can_install else WAIT_WORK), bool(can_install))
        if self.stage == "waiting" and self._waiting_key == key:
            return                           # asked every 2 s: nothing changes, nothing flickers
        self._waiting_key = key
        self._full()
        self._show("waiting", READY, detail=key[0], buttons=("cancel", "install") if can_install else ("cancel",))

    def show_installing(self, reopens=True, admin=False):
        """The installer is starting and Gusan closes in a moment: no buttons. ``reopens`` False: nothing
        will reopen it, so the Start menu is named. ``admin``: Windows' permission prompt comes first."""
        self._waiting_key = None
        self._busy()
        detail = "%s %s" % (INSTALLING if reopens else INSTALLING_NO_REOPEN, INSTALLER_WINDOW)
        if admin:
            detail = "%s %s" % (ASKS_PERMISSION, detail)
        version = _version(self.tag)
        self._show("installing", INSTALLING_LINE % version if version else INSTALLING_PLAIN, detail=detail)

    def show_failed(self, message, detail=""):
        """What went wrong, and the way forward: Try again or the download page. ``detail``: one more line
        (a log file's path), selectable."""
        self._waiting_key = None
        self.bar.setVisible(False)
        self._say(self.lbl_amount, "")
        message = str(message or "Please try again.").strip()
        self._show("failed", FAILED, detail="%s%s" % (message[:1].upper(), message[1:]), note=detail,
                   buttons=("website", "retry"))

    # ── back to the row's own status ──
    def _cancel_clicked(self):
        if self.stage in ("downloading", "waiting"):
            self.cancel_requested.emit()

    def _go_idle(self, tell=True):
        self.stage = "idle"
        self._checking = False
        self._waiting_key = None
        QWidget.setVisible(self, False)
        self.actions.setVisible(False)
        if self._row_button is not None:
            self._row_button.setVisible(True)
        for w in self._extras:
            w.setVisible(True)
        if tell and callable(self._on_idle):
            try:
                self._on_idle()
            except Exception:
                logger.debug("update panel: on_idle failed", exc_info=True)

    def hide(self):
        """The attempt is over without installing (a Cancel): the row shows its own status again."""
        self._go_idle()
