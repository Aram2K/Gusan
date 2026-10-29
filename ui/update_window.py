"""The "Updating Gusan" window: download progress with Cancel, then a clear "installing - Gusan opens
again by itself" step, or what went wrong with a way forward. It is shown for every in-app update,
from the first click to the restart, so an update never looks like nothing happened (it used to be a
silent ~100 MB download that ended with the app vanishing and an installer wizard).

The work itself is updater.py; main.AppController.install_update drives this window through its
stages and listens to its four signals:

    win = UpdateWindow("v2.0.2", main_app=controller)
    win.cancel_requested.connect(...)      # Cancel, Esc or X while downloading / waiting
    win.install_requested.connect(...)     # "Install now" (waiting for a typed key to be saved)
    win.retry_requested.connect(...)       # "Try again" (failed)
    win.website_requested.connect(...)     # "Open the download page" (failed)

    win.show_downloading()                 # busy bar, Cancel
    win.show_progress(done, total)         # "45 of 100 MB"; at 100 percent "Checking the download"
    win.show_waiting(text=None, can_install=False)     # downloaded; waits for a recording / a typed key
    win.show_installing(reopens=True, admin=False)     # no buttons; Gusan closes in a moment
    win.show_failed("Couldn't download the update.", detail="Details: C:\\...\\x.log")

``stage`` is "downloading", "waiting", "installing" or "failed".

How it behaves, and why:
* A QDialog subclass with its own look (ui/theme.py, one light look whatever the OS is set to; a bare
  QDialog is banned, tests/test_no_system_dialogs.py), non-modal and parentless: it works while the
  Gusan window is hidden in the tray, has its own taskbar button and the OS title bar.
* NOT always on top: the one question it can lead to (a typed API key that is not saved yet) is a
  mini_prompt card that must sit over it. The controller brings it to the front at every stage.
* Esc or the X while downloading or waiting is Cancel (never a hidden download that later closes the
  app out of nowhere); while installing it is ignored (Gusan closes in a moment anyway); after a
  failure it is a plain close. EXCEPT while the app quits (main_app._quitting) or Windows logs off
  (main_app._session_ending): then it always closes - Qt's quit and a log-off ask every window to
  close and one that refuses cancels them, so the app would stay open and Setup would have to
  force it shut.
* Keyboard: Enter presses the focused button (every button is an auto-default one, like the question
  cards); the keyboard starts on the default button, else the first. While a typed key waits to be
  saved nothing is the default - Enter must never be the way to lose it.
* The controller shows it WITHOUT taking the keyboard whenever nobody just asked (a failure, the
  hand-over, after a restart): a dictation or a sentence typed elsewhere never lands in it.
* Fixed content width, height follows the (wrapped) text of each stage; a long log path wraps
  (mini_prompt.breakable) instead of running out of the card; it fits a 1366x768 screen at 150 percent.
* Asked every 2 s, the same "waiting" text does nothing (no flicker).
"""

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QLayout, QMenu, QProgressBar,
                               QVBoxLayout)

from app_info import APP_NAME, APP_VERSION
from ui import theme, winfit
from ui.components import icons
from ui.components.buttons import Button
from ui.components.mini_prompt import breakable
from ui.components.surfaces import WhiteCard, label

logger = logging.getLogger("transcribe")

CONTENT_WIDTH = 440          # the card's width (logical pixels); the height follows the words

# Everything the window says, in one place.
TITLE = "Updating %s" % APP_NAME
DOWNLOADING = "Downloading the update"
CHECKING = "Checking the download"
WAIT_WORK = "Ready to install. It starts when your recording or file is done."
WAIT_KEY = "Ready to install. Save the key you typed first, or choose Install now."
INSTALLING = "Installing. %s closes now and opens again by itself when the installer is done." % APP_NAME
INSTALLING_NO_REOPEN = ("Installing. %s closes now. Open it from the Start menu when the installer is done."
                        % APP_NAME)
INSTALLER_WINDOW = "The installer shows its own small window while it works."
ASKS_PERMISSION = "Windows asks for permission first."
FAILED = "The update didn't install."
STARTING = "\u00a0"                    # the progress line is always there: the window never grows when it first speaks
BREAK = "\u200b"                       # mini_prompt.breakable's invisible place for a line to end

# stage -> (icon, icon colour, tile background)
_TILES = {"download": ("download", "blue-tx", "blue-sub"), "install": ("refresh", "blue-tx", "blue-sub"),
          "failed": ("alert", "red-tx", "red-sub")}


def _mb(n):
    return "%.0f" % (n / 2 ** 20)


class _PlainCopyLabel(QLabel):
    """A selectable label whose copy is exactly what the person reads: the invisible break characters that
    let a long path wrap (mini_prompt.breakable) are left out - pasted into Explorer they would make the
    path "not exist". Copy (Ctrl+C or the menu) takes the selection, or the whole line when nothing is
    selected."""

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


class UpdateWindow(QDialog):
    cancel_requested = Signal()
    retry_requested = Signal()
    install_requested = Signal()
    website_requested = Signal()

    def __init__(self, tag="", main_app=None, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.tag = ""
        self.stage = "downloading"
        self._checking = False
        self._waiting_key = None             # what show_waiting last showed: (text, can_install)
        theme.apply(self)
        self.setModal(False)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)     # a download can take minutes: it can be put away
        self.setWindowTitle(TITLE)
        self.setAccessibleName(TITLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(0)
        # The window is exactly as tall as the words of the stage need.
        outer.setSizeConstraint(QLayout.SetFixedSize)
        self.card = WhiteCard(self, pad=(22, 20), gap=14)
        self.card.setFixedWidth(CONTENT_WIDTH)
        outer.addWidget(self.card)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(14)
        self.tile = QLabel(self.card)
        self.tile.setFixedSize(40, 40)
        self.tile.setAlignment(Qt.AlignCenter)
        self.tile.setAccessibleName("")
        head.addWidget(self.tile, 0, Qt.AlignTop)
        names = QVBoxLayout()
        names.setContentsMargins(0, 0, 0, 0)
        names.setSpacing(2)
        self.lbl_title = label(TITLE, "h2", self.card, wrap=True)
        self.lbl_title.setTextFormat(Qt.PlainText)
        self.lbl_title.setAccessibleName(TITLE)
        names.addWidget(self.lbl_title)
        self.lbl_version = label("", "muted", self.card)
        self.lbl_version.setTextFormat(Qt.PlainText)
        names.addWidget(self.lbl_version)
        head.addLayout(names, 1)
        self.card.add(head)

        self.lbl_status = label("", "body", self.card, wrap=True, selectable=True)
        self.lbl_status.setTextFormat(Qt.PlainText)
        self.card.add(self.lbl_status)

        self.bar = QProgressBar(self.card)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setAccessibleName("Update progress")
        self.card.add(self.bar)

        self.lbl_detail = label("", "muted", self.card, wrap=True)
        self.lbl_detail.setTextFormat(Qt.PlainText)
        self.card.add(self.lbl_detail)
        # A failure names its log file: selectable, so it can be copied.
        self.lbl_note = _PlainCopyLabel(self.card)
        theme.set_role(self.lbl_note, "muted")
        self.lbl_note.setWordWrap(True)
        self.lbl_note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_note.setTextFormat(Qt.PlainText)
        self.card.add(self.lbl_note)

        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(8)
        row.addStretch(1)
        self.btn_close = Button("Close", "ghost", "m", parent=self.card)
        self.btn_close.clicked.connect(self.close)
        self.btn_website = Button("Open the download page", "secondary", "m", parent=self.card)
        self.btn_website.clicked.connect(self.website_requested)
        self.btn_cancel = Button("Cancel", "secondary", "m", parent=self.card)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_install = Button("Install now", "primary", "m", parent=self.card)
        self.btn_install.clicked.connect(self.install_requested)
        self.btn_retry = Button("Try again", "primary", "m", icon="refresh", parent=self.card)
        self.btn_retry.clicked.connect(self.retry_requested)
        self._buttons = {"close": self.btn_close, "website": self.btn_website, "cancel": self.btn_cancel,
                         "install": self.btn_install, "retry": self.btn_retry}
        for b in self._buttons.values():
            b.setAutoDefault(True)          # Enter presses the button that has the keyboard (like the question cards)
            b.setDefault(False)
            row.addWidget(b)
        self.card.add(row)

        self.set_tag(tag)
        self.show_downloading()

    # ── what is shown ──
    def set_tag(self, tag):
        """The release this window installs ("v2.0.2" -> "Version 2.0.2")."""
        self.tag = str(tag or "")
        version = self.tag[1:] if self.tag[:1] in ("v", "V") else self.tag
        shown = "Version %s" % version if version else ""
        self.lbl_version.setText(shown)
        self.lbl_version.setAccessibleName(shown)
        self.lbl_version.setVisible(bool(version))

    @staticmethod
    def _say(lbl, text):
        text = str(text or "")
        shown = breakable(text)
        if lbl.text() != shown:             # (progress asks up to 10 times a second: a screen reader hears changes only)
            lbl.setText(shown)
            lbl.setAccessibleName(text.strip())
        lbl.setVisible(bool(text))

    def _show(self, tile, status, detail="", note="", buttons=(), default=None):
        icon, colour, bg = _TILES[tile]
        self.tile.setPixmap(icons.pixmap(icon, 20, colour))
        self.tile.setStyleSheet("QLabel { background: %s; border: none; border-radius: 12px; }" % theme.qss_color(bg))
        self._say(self.lbl_status, status)
        self._say(self.lbl_detail, detail)
        self._say(self.lbl_note, note)
        wanted = set(buttons)
        for name, b in self._buttons.items():
            b.setVisible(name in wanted)
            b.setDefault(name == default)
        # The keyboard starts on the default button, else on the first one shown.
        first = default if default in wanted else next((n for n in self._buttons if n in wanted), None)
        if first is not None:
            self._buttons[first].setFocus()
        self.setAccessibleDescription(" ".join(x for x in (status, detail.strip(), note) if x))
        # The wrapped words of this stage decide the height NOW (not a frame later): no cut-off line.
        self.layout().invalidate()
        self.layout().activate()
        self.adjustSize()

    def _busy(self):
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)

    def _full(self):
        self.bar.setVisible(True)
        self.bar.setRange(0, 100)
        self.bar.setValue(100)

    def show_downloading(self):
        self.stage = "downloading"
        self._checking = False
        self._waiting_key = None
        self._busy()
        self._show("download", DOWNLOADING, detail=STARTING, buttons=("cancel",))

    def show_progress(self, done, total):
        """The download's progress. ``total`` 0 = the size is not known (a busy bar and the MB so far)."""
        if self.stage != "downloading" or self._checking:
            return
        done, total = max(0, float(done or 0)), max(0, float(total or 0))
        if total > 0 and done >= total:
            self._checking = True
            self._busy()
            self._show("download", CHECKING, detail=STARTING, buttons=("cancel",))
            return
        if total > 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(min(99, int(done * 100 / total)))
            self._say(self.lbl_detail, "%s of %s MB" % (_mb(done), _mb(total)))
        else:
            self._busy()
            self._say(self.lbl_detail, "%s MB" % _mb(done))

    def show_waiting(self, text=None, can_install=False):
        """Downloaded and checked, waiting: for a recording or a file to finish (the default words), or for
        a typed API key to be saved (``can_install``: Install now goes on anyway)."""
        key = (text or (WAIT_KEY if can_install else WAIT_WORK), bool(can_install))
        if self.stage == "waiting" and self._waiting_key == key:
            return                           # asked every 2 s: nothing changes, nothing flickers
        self.stage = "waiting"
        self._waiting_key = key
        self._full()
        # (nothing is the default while a key waits: Enter must not be a way to lose it)
        self._show("download", key[0], buttons=("cancel", "install") if can_install else ("cancel",))

    def show_installing(self, reopens=True, admin=False):
        """The installer is starting and Gusan closes in a moment: no buttons. ``reopens`` False: nothing
        will reopen it, so the Start menu is named. ``admin``: Windows' permission prompt comes first."""
        self.stage = "installing"
        self._waiting_key = None
        self._busy()
        detail = INSTALLER_WINDOW
        if admin:
            detail = "%s %s" % (ASKS_PERMISSION, detail)
        self._show("install", INSTALLING if reopens else INSTALLING_NO_REOPEN, detail=detail)

    def show_failed(self, message, detail=""):
        """What went wrong, and the way forward: Try again, the download page, or Close. ``detail``: one
        more line (a log file's path), selectable."""
        self.stage = "failed"
        self._waiting_key = None
        self.bar.setVisible(False)
        message = str(message or "Please try again.").strip()
        self._show("failed", "%s %s%s" % (FAILED, message[:1].upper(), message[1:]),
                   detail="%s %s keeps working." % (APP_NAME, APP_VERSION), note=detail,
                   buttons=("close", "website", "retry"), default="retry")

    # ── closing ──
    def reject(self):
        # Esc, the X and Cancel. Downloading or waiting: a cancel. Installing: Gusan closes by itself in a
        # moment, so the window stays. After a failure: just closes.
        if self.stage == "installing":
            return
        if self.stage in ("downloading", "waiting"):
            self.cancel_requested.emit()
        super().reject()

    def closeEvent(self, event):
        # The app quits through Qt, which asks every window to close and stops at the first that refuses:
        # this one never does then (Gusan is closing for the installer, and the quit must go through).
        if getattr(self.app, "_quitting", False) is True or getattr(self.app, "_session_ending", False) is True:
            event.accept()
            return
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        try:
            winfit.settle_on_screen(self)
        except Exception:
            logger.debug("fit on screen failed", exc_info=True)
