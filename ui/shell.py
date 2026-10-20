"""Gusan main window: sidebar + one page at a time.

    win = MainWindow(main_app)          # cheap: no page is built yet
    win.open("settings", section="speech")
    win.current_route()                 # "settings"

Pages are built the first time they open, from a factory given to
register_page(), else the module ui/pages/<route>.py (its create(main_app)).
A page that fails to build (or has no module) shows an error card with Try
again and Copy details; the rest of the window keeps working. See
ui/pages/__init__.py for the optional page hooks (on_open, on_setting_changed,
refresh_account).

Closing the window only hides it: the tray app keeps running.
"""

import logging
import traceback

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QHBoxLayout, QMainWindow, QScrollArea, QSizePolicy, QStackedWidget,
                               QVBoxLayout, QWidget)

from app_info import APP_NAME
from ui import theme
from ui.components import Button, Card, IconTile, PageHeader, Sidebar, label

logger = logging.getLogger("transcribe")

ROUTES = ("home", "dictation", "smart_actions", "meetings", "live_assistant", "audio_to_text",
          "settings", "account")

# Pages that moved: the old route opens its new place. The Dictionary is a
# section of Settings (ui/pages/settings.py), not a page of its own.
ROUTE_ALIASES = {"dictionary": ("settings", {"section": "dictionary"})}

TITLES = {
    "home": "Welcome",
    "dictation": "Dictation",
    "smart_actions": "Smart Actions",
    "meetings": "Meetings",
    "live_assistant": "Live Assistant",
    "audio_to_text": "Convert audio to text",
    "settings": "Settings",
    "account": "Account & plan",
}

DEFAULT_SIZE = (1280, 820)
MIN_SIZE = (960, 600)

_REGISTRY = {}


def register_page(route, factory):
    """Use ``factory(main_app) -> QWidget`` for ``route`` (overrides ui/pages/<route>.py)."""
    if route not in ROUTES:
        raise ValueError("unknown route %r" % route)
    _REGISTRY[route] = factory


def unregister_page(route):
    _REGISTRY.pop(route, None)


def _import_page_module(route):
    """ui/pages/<route>.py, or None when it doesn't exist. Plain import
    statements (not importlib strings) so PyInstaller bundles every page."""
    try:
        if route == "home":
            import ui.pages.home as mod
        elif route == "dictation":
            import ui.pages.dictation as mod
        elif route == "smart_actions":
            import ui.pages.smart_actions as mod
        elif route == "meetings":
            import ui.pages.meetings as mod
        elif route == "live_assistant":
            import ui.pages.live_assistant as mod
        elif route == "audio_to_text":
            import ui.pages.audio_to_text as mod
        elif route == "settings":
            import ui.pages.settings as mod
        elif route == "account":
            import ui.pages.account as mod
        else:
            return None
    except ModuleNotFoundError as e:
        if e.name in ("ui.pages." + route, "ui.pages"):
            return None                     # no such page module: error card
        raise                               # the page imports something missing: error card
    return mod


class PageMissing(RuntimeError):
    """``route`` has no ui/pages/<route>.py with a create(main_app)."""


def page_factory(route):
    """The factory that builds ``route``: registered, else the page module's
    create(). A route without one gets a factory that raises PageMissing, so
    the window shows its error card for it."""
    if route in _REGISTRY:
        return _REGISTRY[route]
    mod = _import_page_module(route)
    create = getattr(mod, "create", None) if mod is not None else None
    if callable(create):
        return create

    def missing(main_app, _r=route):
        raise PageMissing("no page module with create() for %r" % _r)
    return missing


def account_state(main_app):
    """(tier, first name) for the sidebar: tier is 'pro' | 'free' | 'guest'."""
    auth = getattr(main_app, "auth", None)
    signed_in = bool(getattr(auth, "is_authenticated", False))
    tier = None
    ct = getattr(main_app, "current_tier", None)
    if callable(ct):
        try:
            tier = ct()
        except Exception:
            tier = None
    try:
        pro = bool(main_app.is_pro())
    except Exception:
        pro = False
    if pro:
        tier = "pro"
    elif tier not in ("pro", "free", "guest"):
        tier = "free" if signed_in else "guest"
    name = (getattr(auth, "user_name", None) or "").strip()
    if not name:
        email = (getattr(auth, "user_email", None) or "").strip()
        name = email.split("@")[0] if email else ""
    name = name.split()[0] if name else ""
    if not name:
        name = "Guest" if tier == "guest" else "Account"
    return tier, name


def _call(main_app, method, *args, **kwargs):
    fn = getattr(main_app, method, None)
    if not callable(fn):
        logger.debug("main_app has no %s()", method)
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("%s() failed", method, exc_info=True)
        return None


class ErrorPage(QWidget):
    """'This page couldn't open.' with Try again and Copy details."""

    def __init__(self, route, details, on_retry, parent=None):
        super().__init__(parent)
        self.route = route
        self.details = details
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(PageHeader(TITLES.get(route, route), parent=self))
        body = QVBoxLayout()
        body.setContentsMargins(32, 0, 32, 28)
        card = Card(self)
        row = QHBoxLayout()
        row.setSpacing(14)
        row.addWidget(IconTile("alert", color="red-tx", bg="red-sub", parent=card), 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(label("This page couldn't open.", "h3", card))
        col.addWidget(label("Try again. If it keeps happening, send us the details.", "muted", card, wrap=True))
        row.addLayout(col, 1)
        self.retry_button = Button("Try again", "primary", "s", parent=card)
        self.retry_button.clicked.connect(lambda _=False: on_retry())
        self.copy_button = Button("Copy details", "secondary", "s", icon="copy", parent=card)
        self.copy_button.clicked.connect(self._copy)
        row.addWidget(self.copy_button, 0, Qt.AlignVCenter)
        row.addWidget(self.retry_button, 0, Qt.AlignVCenter)
        card.add(row)
        body.addWidget(card)
        body.addStretch(1)
        lay.addLayout(body, 1)

    def _copy(self):
        try:
            QGuiApplication.clipboard().setText(self.details)
            self.copy_button.setText("Copied")
        except Exception:
            logger.debug("copy failed", exc_info=True)


class MainWindow(QMainWindow):
    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self._pages = {}            # route -> page widget
        self._holders = {}          # route -> the widget in the stack (scroll area or page)
        self._route = None
        self.setWindowTitle(APP_NAME)
        theme.apply(self)
        self.setMinimumSize(*MIN_SIZE)
        self.resize(*DEFAULT_SIZE)

        root = QWidget(self)
        root.setProperty("gusan", "root")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        self.sidebar = Sidebar(root)
        self.sidebar.navigate.connect(self.open)
        self.sidebar.upgrade.connect(self._on_upgrade)
        self.sidebar.account.connect(lambda: self.open("account"))
        h.addWidget(self.sidebar)
        self.stack = QStackedWidget(root)
        self.stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.refresh_account()

    # ── routing ──
    def current_route(self):
        return self._route

    def page(self, route):
        """The built page widget for ``route`` (None until it has opened once)."""
        return self._pages.get(route)

    def pages(self):
        return dict(self._pages)

    def open(self, route, **opts):
        """Show ``route`` (building it the first time) and return its page."""
        alias = ROUTE_ALIASES.get(route)
        if alias is not None:
            route, extra = alias
            opts = dict(extra, **opts)
        if route not in ROUTES:
            logger.warning("Unknown page %r - opening home", route)
            route = "home"
        if route not in self._pages:
            self._build(route)
        page = self._pages[route]
        self.stack.setCurrentWidget(self._holders[route])
        changed = route != self._route
        self._route = route
        self.sidebar.set_active(route)
        hook = getattr(page, "on_open", None)
        if callable(hook):
            try:
                hook(**opts)
            except Exception:
                logger.warning("%s.on_open failed", route, exc_info=True)
        if changed:
            _call(self.app, "track", "settings_tab_opened", {"tab": route})
        return page

    def reload(self, route):
        """Throw away ``route``'s page and build it again (the error card's Try again)."""
        holder = self._holders.pop(route, None)
        self._pages.pop(route, None)
        if holder is not None:
            self.stack.removeWidget(holder)
            holder.deleteLater()
        if self._route == route:
            self._route = None
            return self.open(route)
        return None

    def _build(self, route):
        try:
            page = page_factory(route)(self.app)
            if not isinstance(page, QWidget):
                raise TypeError("page factory for %r returned %r, not a QWidget" % (route, page))
        except Exception:
            details = "Gusan page: %s\n\n%s" % (route, traceback.format_exc())
            logger.error("Page %s failed to build:\n%s", route, details)
            page = ErrorPage(route, details, lambda r=route: self.reload(r))
        if page.property("gusan") is None:
            page.setProperty("gusan", "page")
        if getattr(page, "wants_scroll", True):
            holder = QScrollArea()
            holder.setProperty("gusan", "scroll")
            holder.setWidgetResizable(True)
            holder.setFrameShape(QScrollArea.NoFrame)
            holder.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            holder.viewport().setAutoFillBackground(False)
            holder.setWidget(page)
        else:
            holder = page
        self.stack.addWidget(holder)
        self._pages[route] = page
        self._holders[route] = holder

    # ── account / settings fan-out ──
    def refresh_account(self):
        """Plan badge + name in the sidebar, then every built page's refresh_account()."""
        try:
            tier, name = account_state(self.app)
            self.sidebar.set_account(tier, name)
        except Exception:
            logger.warning("Sidebar account refresh failed", exc_info=True)
        for route, page in list(self._pages.items()):
            hook = getattr(page, "refresh_account", None)
            if callable(hook):
                try:
                    hook()
                except Exception:
                    logger.warning("%s.refresh_account failed", route, exc_info=True)

    def on_setting_changed(self, key):
        """AppController.sig_setting_changed lands here; every built page hears it."""
        for route, page in list(self._pages.items()):
            hook = getattr(page, "on_setting_changed", None)
            if callable(hook):
                try:
                    hook(key)
                except Exception:
                    logger.warning("%s.on_setting_changed(%s) failed", route, key, exc_info=True)

    def _on_upgrade(self):
        _call(self.app, "_pro_upsell")

    # ── window ──
    def showEvent(self, event):
        super().showEvent(event)
        try:
            from ui.winfit import settle_on_screen
            settle_on_screen(self)
        except Exception:
            logger.debug("fit on screen failed", exc_info=True)

    def closeEvent(self, event):
        # The app lives in the tray: closing the window only hides it - but
        # not while the app quits (AppController.quit_app). Qt then closes
        # every window and stops at the first that refuses: a refusal here
        # left another open window (the Live Assistant card) up, and Qt
        # cancelled the quit.
        if getattr(self.app, "_quitting", False) is True:
            event.accept()
            return
        event.ignore()
        self.hide()
