"""Gusan tray menu (2.0): the right-click menu by the clock.

Mockup: v2/canvas/project/Tray.dc.html (source v2/pages/floating.py tray()).

    tray = build_tray_menu(app, QMenu())       # -> TrayMenu (keeps the actions)
    app.tray_icon.setContextMenu(tray.menu)
    app.tray_icon.setToolTip(tray.tooltip())
    tray.refresh()                              # after sign-in / plan changes

Rows, top to bottom ("Next:" only while a Google Calendar is connected and a
meeting is coming up; the account rows depend on the plan):

    Gusan  v2.0.0                     (disabled)
    [bell] Next: Weekly product sync    14:30
    ──
    Open Gusan                        (bold, the default action)
    ──
    Dictate                             Alt+R
    Record a meeting…  PRO
    Live Assistant  PRO            Ctrl+Alt+A
    Convert audio to text…
    Recent                                  ›   (Home's newest 4 + "Show all in Gusan")
    ──
    ✓ Privacy mode                    (checkable)
    ──
    aram@aibuben.xyz  ·  Free         (disabled; Free and Pro)  |  Guest  (guests: no name, no email)
    Upgrade to Pro…                   (purple, Free only)  |  Sign in / Sign up  (blue, guests)
    ──
    Settings…
    Quit Gusan

A QMenu can't hold widgets that look native, so the menu keeps plain QActions
(keyboard, screen readers and the default action all stay Qt's) and a small
QProxyStyle (MenuLook) paints the items: the PRO pill after the words, the
bell and check glyphs (ui.components.icons), the shortcut column, the grey
"Next:" prefix and the purple Upgrade row, in the light or dark system menu
colours of the mockup. The menu's panel and frame stay the platform style's.
On macOS the tray menu is a native NSMenu that ignores Qt styles, so there the
rows are plain text and the PRO items read "… (Pro)". That "(Pro)" stays in
the action text in the painted look too - screen readers read the QAction
text, not the pill - and MenuLook paints the pill in its place.

Integration notes for Wave 4
----------------------------
Status: DONE - AppController._build_tray_menu builds it once and refreshes it
in place (sig_setting_changed connected, attach_calendar from _setup_calendar);
toggle_dictation exists. The Live Assistant row opens the card for every plan
(its Start asks for Pro), so it never calls _pro_upsell.

* Replace AppController._build_tray_menu (main.py) with::

      from ui.tray_menu import build_tray_menu
      if getattr(self, "tray_menu", None) is None:
          self.tray_menu = build_tray_menu(self, QMenu())
          self.tray_icon.setContextMenu(self.tray_menu.menu)
      else:
          self.tray_menu.refresh()
      self.tray_icon.setToolTip(self.tray_menu.tooltip())

  and connect ``sig_setting_changed -> tray_menu.on_setting_changed`` and,
  once app.calendar exists, ``tray_menu.attach_calendar(self.calendar)`` (or
  ``calendar.changed -> tray_menu.refresh_next``). The menu also re-reads
  everything cheap (plan, hotkeys, privacy, the calendar's cached next
  meeting) in aboutToShow, so a missed signal only delays nothing.
* App methods the rows call, looked up by name (a missing one is logged, never raised):
    Next: <meeting>         show_main("meetings")
    Open Gusan              show_main()
    Dictate                 toggle_dictation() when it exists, else _on_hotkey()
                            (please add a public ``toggle_dictation``)
    Record a meeting…       Pro: show_main("meetings")   Free: _pro_upsell("Meeting recording")
    Live Assistant          toggle_live_assist() for every plan (the card's Start asks for Pro)
    Convert audio to text…  show_main("audio_to_text")
    Recent › <item>         show_main(route, **opts) - Home's row targets
                            (dictation entry=, meetings meeting=, audio_to_text transcript=, smart_actions)
    Recent › Show all       show_main("home")
    Privacy mode            set_setting("privacy_mode", on) (fallback set_privacy_mode(on));
                            then show_tray_hint(title, text) - settings_apply calls
                            set_privacy_mode with notify=False, so the tray says it
    Upgrade to Pro…         _pro_upsell()
    Sign in / Sign up       show_auth_gate()   (guests; a super admin previewing Guest too)
    Settings…               show_main("settings")
    Quit Gusan              quit_app()
* Reads: app.cfg ("hotkey", "live_assist_hotkey", "privacy_mode"),
  app._registered_assist_hotkey / app._assist_listener (the Live Assistant
  hint shows only a hotkey that really registered), app.is_pro(),
  app.current_tier(), app.auth (is_authenticated, user_email),
  app.calendar (available, is_connected, next_meeting()).
* The old menu's "Manage subscription…", "Sign out" and "History Log" rows
  are gone in the mockup (Account & plan and Home have them).
* Recent loads history + meetings + transcripts on a worker (Home's
  ui.pages.home.load_sources / recent_items) when the menu opens; the submenu
  shows the last result at once. Nothing touches the disk on the GUI thread.
"""

import logging
import math
import sys
import time
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QRectF, QSize, Qt
from PySide6.QtGui import QAction, QColor, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QApplication, QMenu, QProxyStyle, QStyle, QStyleFactory, QStyleOptionMenuItem

import account_actions
from app_info import APP_NAME, APP_VERSION
from ui import theme
from ui.components import icons

logger = logging.getLogger("transcribe")

IS_MAC = sys.platform == "darwin"

# Sizes from the mockup (floating.py: M_W, SUB_W, H_HEAD, H_ITEM, H_INFO, H_SEP, M_PAD).
MENU_W, SUB_W = 284, 300
H_HEAD, H_ITEM, H_INFO, H_SEP = 30, 32, 28, 9
M_PAD, M_BORDER = 4, 1
MAX_W = 420                     # long emails / titles are cut with "…" beyond this
TEXT_X = 36                     # 8 padding + 18 lead slot + 10 gap
RECENT_TEXT_X = 35              # 10 padding + 15 icon + 10 gap
RIGHT_PAD = 12
PILL_H, PILL_PAD = 18, 8
RECENT_LIMIT = 4
RECENT_STALE_S = 20
# Kept in the PRO rows' action text, so a screen reader (which reads the
# QAction text) says "Pro"; the painted look shows the PRO pill instead.
PRO_SUFFIX = "  (Pro)"
SEP = "  ·  "

# The system menu colours the mockup draws (floating.py MENU), light and dark.
COLORS = {
    "light": {"text": "#1a1a1a", "sub": "#5c5c5c", "dis": "#6b6b6b", "sep": "rgba(0,0,0,0.08)",
              "hover": "rgba(0,0,0,0.05)", "upgrade": "pro-tx", "signin": "blue-tx"},
    "dark": {"text": "#ffffff", "sub": "#c2c2c2", "dis": "#9d9d9d", "sep": "rgba(255,255,255,0.09)",
             "hover": "rgba(255,255,255,0.08)", "upgrade": "#d8b4fe", "signin": "#93c5fd"},
}

FEATURE_MEETING = "Meeting recording"
RECENT_ICONS = {"dictation": "mic", "smart": "wand", "audio": "wave", "meeting": "people"}

# Action properties the style reads.
P_KIND = "gusanKind"            # header | next | item | info | upgrade | signin | recent | empty
P_HINT = "gusanHint"            # right-hand text (shortcut, time)
P_PRO = "gusanPro"              # PRO pill after the words
P_BOLD = "gusanBold"
P_ICON = "gusanIcon"            # lead glyph (icons.IC name)
P_PREFIX = "gusanPrefix"        # "Next:" drawn in the muted colour


# ── helpers ──────────────────────────────────────────────────────────────────
def _call(app, method, *args, **kwargs):
    fn = getattr(app, method, None) if app is not None else None
    if not callable(fn):
        logger.debug("main_app has no %s()", method)
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("%s() failed", method, exc_info=True)
        return None


def _esc(text):
    """Literal '&' in a QAction text (Qt reads '&x' as a mnemonic)."""
    return str(text or "").replace("&", "&&")


def _plain(text):
    return str(text or "").replace("&&", "\x00").replace("&", "").replace("\x00", "&")


def _painted(text, pro):
    """The words a PRO row paints: its text without the spoken " (Pro)" (the pill says it)."""
    return text[: -len(PRO_SUFFIX)] if pro and text.endswith(PRO_SUFFIX) else text


def hotkey_hint(hk):
    """'alt+r' -> 'Alt+R' (menus are compact: no spaces around +)."""
    hk = str(hk or "").strip()
    if not hk:
        return ""
    try:
        import hotkeys
        text = hotkeys.display(hk)
    except Exception:
        text = hk
    return text.replace(" + ", "+")


def one_line(text, limit=80):
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def recent_meta(when, now):
    """11:42 (today) · Yesterday · Tue (this week) · 29 Sep (older)."""
    if when is None:
        return ""
    d = (now.date() - when.date()).days
    if d <= 0:
        return when.strftime("%H:%M")
    if d == 1:
        return "Yesterday"
    if d < 7:
        return when.strftime("%a")
    return "%d %s" % (when.day, when.strftime("%b"))


def _qc(value):
    return theme.qcolor(value)


def _is_dark(palette):
    try:
        return palette.color(QPalette.Window).lightness() < 128
    except Exception:
        return False


def _fonts():
    f = _FONTS
    if not f:
        f.update(item=theme.font(14), bold=theme.font(14, 700), small=theme.font(13),
                 hint=theme.font(12.5), recent=theme.font(13), meta=theme.font(12),
                 pill=theme.font(10, 700))
    return f


_FONTS = {}


def _adv(font, text):
    return QFontMetricsF(font).horizontalAdvance(text)


def pro_pill_width():
    return math.ceil(_adv(_fonts()["pill"], "PRO")) + 2 * PILL_PAD


def paint_pro_pill(p, x, cy):
    """The PRO pill (ui.components.ProTag's look) at x, centred on cy. Returns its width."""
    f = _fonts()["pill"]
    w = pro_pill_width()
    rect = QRectF(x, cy - PILL_H / 2.0, w, PILL_H)
    grad = QLinearGradient(rect.topLeft(), rect.topRight())
    grad.setColorAt(0.0, _qc("rgba(168,85,247,0.92)"))
    grad.setColorAt(1.0, _qc("rgba(126,34,206,0.92)"))
    p.setPen(QPen(_qc("rgba(255,255,255,0.59)"), 1))
    p.setBrush(grad)
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), PILL_H / 2.0, PILL_H / 2.0)
    p.setPen(QColor("#ffffff"))
    p.setFont(f)
    p.drawText(rect, Qt.AlignCenter, "PRO")
    return w


def _prop(action, name, default=None):
    if action is None:
        return default
    v = action.property(name)
    return default if v is None else v


def _action_at(menu, rect):
    for a in menu.actions():
        if a.isVisible() and menu.actionGeometry(a) == rect:
            return a
    return None


def _action_for_text(menu, text):
    for a in menu.actions():
        if not a.isSeparator() and a.text() == text:
            return a
    return None


# ── the look ─────────────────────────────────────────────────────────────────
class MenuLook(QProxyStyle):
    """Paints the items of menus marked ``gusanMenu`` (see the module docstring).
    Everything else - panel, frame, other widgets - is the base style's."""

    def __init__(self, min_width=MENU_W):
        base = None
        try:
            cur = QApplication.style()
            name = cur.name() if cur is not None else ""
            base = QStyleFactory.create(name) if name else None
        except Exception:
            base = None
        if base is not None:
            super().__init__(base)
        else:
            super().__init__()
        self.min_item_w = min_width - 2 * (M_PAD + M_BORDER)

    @staticmethod
    def _ours(widget):
        return isinstance(widget, QMenu) and bool(widget.property("gusanMenu"))

    # metrics: a 1 px frame and 4 px padding, as the mockup
    def pixelMetric(self, metric, option=None, widget=None):
        if self._ours(widget):
            if metric == QStyle.PM_MenuPanelWidth:
                return M_BORDER
            if metric in (QStyle.PM_MenuHMargin, QStyle.PM_MenuVMargin):
                return M_PAD
        return super().pixelMetric(metric, option, widget)

    def sizeFromContents(self, ct, opt, size, widget=None):
        if ct == QStyle.CT_MenuItem and self._ours(widget):
            try:
                return self._item_size(opt, widget)
            except Exception:
                logger.debug("menu item size failed", exc_info=True)
        return super().sizeFromContents(ct, opt, size, widget)

    def drawControl(self, element, opt, p, widget=None):
        if element == QStyle.CE_MenuItem and self._ours(widget):
            try:
                p.save()
                self._paint_item(opt, p, widget)
                p.restore()
                return
            except Exception:
                p.restore()
                logger.debug("menu item paint failed", exc_info=True)
        super().drawControl(element, opt, p, widget)

    # sizes
    def _item_size(self, opt, menu):
        min_w = max(self.min_item_w, int(menu.property("gusanMinItemW") or 0))
        if opt.menuItemType == QStyleOptionMenuItem.Separator:
            return QSize(min_w, H_SEP)
        a = _action_for_text(menu, opt.text)
        kind = _prop(a, P_KIND, "item")
        f = _fonts()
        text = _plain(opt.text)
        if kind == "header":
            return QSize(max(min_w, min(MAX_W, math.ceil(TEXT_X + _adv(f["small"], text) + RIGHT_PAD))), H_HEAD)
        if kind in ("info", "empty"):
            return QSize(max(min_w, min(MAX_W, math.ceil(TEXT_X + _adv(f["small"], text) + RIGHT_PAD))), H_INFO)
        if kind == "recent":
            return QSize(min_w, H_ITEM)
        font = f["bold"] if _prop(a, P_BOLD, False) else f["item"]
        pro = bool(_prop(a, P_PRO, False))
        w = TEXT_X + _adv(font, _painted(text, pro)) + RIGHT_PAD
        if pro:
            w += 10 + pro_pill_width()
        hint = _prop(a, P_HINT, "")
        if hint:
            w += 20 + _adv(f["hint"], hint)
        if opt.menuItemType == QStyleOptionMenuItem.SubMenu:
            w += 24
        return QSize(max(min_w, min(MAX_W, math.ceil(w))), H_ITEM)

    # painting
    def _paint_item(self, opt, p, menu):
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(opt.rect)
        c = COLORS["dark" if _is_dark(opt.palette) else "light"]
        if opt.menuItemType == QStyleOptionMenuItem.Separator:
            p.fillRect(QRectF(r.left(), r.top() + 4, r.width(), 1), _qc(c["sep"]))
            return
        a = _action_at(menu, opt.rect)
        kind = _prop(a, P_KIND, "item")
        f = _fonts()
        enabled = bool(opt.state & QStyle.State_Enabled)
        selected = enabled and bool(opt.state & QStyle.State_Selected)
        if selected:
            path = QPainterPath()
            path.addRoundedRect(r, 4, 4)
            p.fillPath(path, _qc(c["hover"]))
        text = _plain(opt.text)
        right = r.right() - RIGHT_PAD

        if kind in ("header", "info", "empty"):
            p.setFont(f["small"])
            p.setPen(_qc(c["dis"]))
            box = QRectF(r.left() + TEXT_X, r.top(), right - r.left() - TEXT_X, r.height())
            p.drawText(box, Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetricsF(f["small"]).elidedText(text, Qt.ElideRight, box.width()))
            return

        if kind == "recent":
            name = _prop(a, P_ICON, "") or "file"
            icon = icons.pixmap(name if name in icons.IC else "file", 15, _qc(c["sub"]))
            p.drawPixmap(int(r.left() + 10), int(r.center().y() - 7.5), icon)
            meta = _prop(a, P_HINT, "")
            mw = 0.0
            if meta:
                p.setFont(f["meta"])
                p.setPen(_qc(c["sub"]))
                mw = _adv(f["meta"], meta)
                p.drawText(QRectF(right - mw, r.top(), mw + 1, r.height()), Qt.AlignVCenter | Qt.AlignRight, meta)
            box = QRectF(r.left() + RECENT_TEXT_X, r.top(), right - (mw + 12 if meta else 0) - r.left() - RECENT_TEXT_X,
                         r.height())
            p.setFont(f["recent"])
            p.setPen(_qc(c["text"]))
            p.drawText(box, Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetricsF(f["recent"]).elidedText(text, Qt.ElideRight, box.width()))
            return

        # lead glyph: the bell on "Next:", the check on a checked row, or a named icon
        cy = r.center().y()
        lead = None
        if opt.checkType != QStyleOptionMenuItem.NotCheckable and opt.checked:
            lead = icons.pixmap("check", 15, _qc(c["text"] if enabled else c["dis"]), sw=2)
        elif _prop(a, P_ICON, ""):
            name = _prop(a, P_ICON, "")
            lead = icons.pixmap(name if name in icons.IC else "info", 15, _qc(c["sub"]))
        if lead is not None:
            p.drawPixmap(int(r.left() + 8 + 1.5), int(cy - 7.5), lead)

        # right side: the shortcut / time, or the submenu chevron
        hint = _prop(a, P_HINT, "")
        if opt.menuItemType == QStyleOptionMenuItem.SubMenu:
            chev = icons.pixmap("chev_r", 14, _qc(c["sub"]), sw=2)
            p.drawPixmap(int(right - 14), int(cy - 7), chev)
            right -= 14 + 10
        elif hint:
            p.setFont(f["hint"])
            p.setPen(_qc(c["sub"] if enabled else c["dis"]))
            hw = _adv(f["hint"], hint)
            p.drawText(QRectF(right - hw, r.top(), hw + 1, r.height()), Qt.AlignVCenter | Qt.AlignRight, hint)
            right -= hw + 20

        bold = bool(_prop(a, P_BOLD, False)) or opt.menuItemType == QStyleOptionMenuItem.DefaultItem
        font = f["bold"] if bold else f["item"]
        if not enabled:
            colour = c["dis"]
        elif kind == "upgrade":
            colour = c["upgrade"]
        elif kind == "signin":
            colour = c["signin"]
        else:
            colour = c["text"]
        pro = bool(_prop(a, P_PRO, False))
        text = _painted(text, pro)
        room = right - r.left() - TEXT_X - ((10 + pro_pill_width()) if pro else 0)
        x = r.left() + TEXT_X
        fm = QFontMetricsF(font)
        p.setFont(font)
        prefix = _prop(a, P_PREFIX, "")
        if prefix and text.startswith(prefix):
            pw = fm.horizontalAdvance(prefix + " ")
            p.setPen(_qc(c["sub"]))
            p.drawText(QRectF(x, r.top(), pw + 1, r.height()), Qt.AlignVCenter | Qt.AlignLeft, prefix)
            x += pw
            room -= pw
            text = text[len(prefix):].lstrip()
        shown = fm.elidedText(text, Qt.ElideRight, max(10.0, room))
        p.setPen(_qc(colour))
        p.drawText(QRectF(x, r.top(), max(10.0, room) + 1, r.height()), Qt.AlignVCenter | Qt.AlignLeft, shown)
        if pro:
            paint_pro_pill(p, x + fm.horizontalAdvance(shown) + 10, cy)


# ── the menu ─────────────────────────────────────────────────────────────────
class TrayMenu(QObject):
    """The tray menu's actions and their refresh helpers (see the module docstring)."""

    def __init__(self, app, menu=None, custom=None):
        menu = menu if menu is not None else QMenu()
        super().__init__(menu)                 # lives (and dies) with the menu
        self.menu = menu
        self.app = app
        self.custom = (not IS_MAC) if custom is None else bool(custom)
        self.actions = {}
        self._next = None
        self._calendar = None              # the calendar attach_calendar connected
        self._recent_items = None          # None until the first load finished
        self._recent_at = 0.0
        self._recent_loading = False
        self._recent_gen = 0
        self.menu.clear()
        self.menu.setTitle(APP_NAME)
        self.menu.setAccessibleName(APP_NAME)
        self.recent = QMenu("Recent", self.menu)
        self.recent.setAccessibleName("Recent")
        if self.custom:
            self._style_menu(self.menu, MENU_W)
            self._style_menu(self.recent, SUB_W)
        self._build()
        self.menu.aboutToShow.connect(self._on_about_to_show)
        self.recent.aboutToShow.connect(self._on_recent_show)
        self.refresh()
        self._fill_recent()

    def _style_menu(self, menu, width):
        menu.setProperty("gusanMenu", True)
        menu.setProperty("gusanMinItemW", width - 2 * (M_PAD + M_BORDER))
        look = MenuLook(width)
        look.setParent(menu)
        menu.setStyle(look)
        menu.setFont(theme.font(14))
        self._follow_os(menu)

    @staticmethod
    def _follow_os(menu):
        """This menu is the system's, not Gusan's: it keeps the OS's light or dark colours (the app's
        own palette is light always - ui/theme.py), so it gets its own palette from the OS scheme."""
        try:
            menu.setPalette(theme.menu_palette())
        except Exception:
            logger.debug("tray menu palette failed", exc_info=True)

    # ── building ──
    def _add(self, key, text, slot=None, kind="item", menu=None, **props):
        menu = menu if menu is not None else self.menu
        a = QAction(_esc(text), menu)
        a.setProperty(P_KIND, kind)
        for name, value in props.items():
            a.setProperty(name, value)
        if slot is not None:
            a.triggered.connect(slot)
        menu.addAction(a)
        if key:
            self.actions[key] = a
        return a

    def _sep(self, key):
        a = self.menu.addSeparator()
        self.actions[key] = a
        return a

    def _build(self):
        head = self._add("header", "%s  v%s" % (APP_NAME, APP_VERSION), kind="header")
        head.setEnabled(False)
        self._add("next", "Next:", self.open_next, kind="next", **{P_ICON: "bell", P_PREFIX: "Next:"})
        self._sep("sep_next")
        op = self._add("open", "Open %s" % APP_NAME, self.open_app, **{P_BOLD: True})
        self.menu.setDefaultAction(op)
        self._sep("sep_open")
        self._add("dictate", "Dictate", self.dictate)
        self._add("meeting", "Record a meeting…", self.record_meeting, **{P_PRO: True})
        self._add("assist", "Live Assistant", self.live_assistant, **{P_PRO: True})
        self._add("convert", "Convert audio to text…", self.convert_audio)
        ra = self.menu.addMenu(self.recent)
        ra.setProperty(P_KIND, "item")
        self.actions["recent"] = ra
        self._sep("sep_tools")
        pv = self._add("privacy", "Privacy mode", None)
        pv.setCheckable(True)
        pv.triggered.connect(self.set_privacy)
        self._sep("sep_privacy")
        acct = self._add("account", "", kind="info")
        acct.setEnabled(False)
        self._add("upgrade", account_actions.UPGRADE + "…", self.upgrade, kind="upgrade", **{P_BOLD: True})
        self._add("signin", account_actions.SIGN_IN_UP, self.sign_in, kind="signin", **{P_BOLD: True})
        self._sep("sep_account")
        self._add("settings", "Settings…", self.open_settings)
        self._add("quit", "Quit %s" % APP_NAME, self.quit_app)
        for key in ("meeting", "assist"):
            a = self.actions[key]
            a.setToolTip("Pro feature")
            a.setStatusTip("Pro feature")
            a.setWhatsThis("Pro feature")
            # Both looks: the text is what screen readers say (the painted
            # look draws the pill in place of these words; macOS shows them).
            a.setText(_esc(_plain(a.text()) + PRO_SUFFIX))

    # ── state ──
    def _pro(self):
        try:
            return bool(self.app.is_pro())
        except Exception:
            return False

    def _cfg(self):
        cfg = getattr(self.app, "cfg", None)
        return cfg if isinstance(cfg, dict) else {}

    def account(self):
        """(tier, email): tier 'pro' | 'free' | 'guest' - the same rule as the
        sidebar's (account_actions.app_tier). A guest has no email: a super
        admin previewing Guest is signed in underneath, and the menu must not
        show "<email> · Free" or "Upgrade to Pro…" for them."""
        tier = account_actions.app_tier(self.app)
        if tier == "guest":
            return tier, ""
        auth = getattr(self.app, "auth", None)
        return tier, str(getattr(auth, "user_email", None) or "").strip()

    def tooltip(self):
        tier, email = self.account()
        tip = APP_NAME + (" - Pro" if tier == "pro" else "")
        if tier != "guest" and email:
            tip += "\n" + email
        return tip

    def _set_hint(self, key, hint):
        a = self.actions[key]
        if self.custom:
            a.setProperty(P_HINT, hint or "")
        else:
            base = _plain(a.text()).split("\t")[0]
            a.setText(_esc(base + ("\t" + hint if hint else "")))
        self.menu.update()

    def visible_texts(self, menu=None):
        """[(key or None, text)] of the rows a user would see (tests, previews).
        In the painted look a PRO row reads as painted (the pill, not "(Pro)");
        ``spoken_text`` gives what a screen reader says."""
        menu = menu or self.menu
        keys = {id(a): k for k, a in self.actions.items()}
        out = []
        for a in menu.actions():
            if a.isVisible() and not a.isSeparator():
                text = _plain(a.text())
                if self.custom:
                    text = _painted(text, bool(_prop(a, P_PRO, False)))
                out.append((keys.get(id(a)), text))
        return out

    def spoken_text(self, key):
        """A row's accessible name: its action text (what screen readers read)."""
        return _plain(self.actions[key].text())

    # ── refresh helpers (the controller calls these) ──
    def refresh(self):
        """Everything cheap: plan and account rows, hotkeys, privacy, Next."""
        self.refresh_account()
        self.refresh_hotkeys()
        self.refresh_privacy()
        self.refresh_next()

    def refresh_account(self):
        tier, email = self.account()
        acct = self.actions["account"]
        if tier == "guest":
            acct.setText(_esc(account_actions.GUEST_BADGE))          # just "Guest": no name, no email, no plan
        else:
            acct.setText(_esc("%s%s%s" % (email or "Signed in", SEP, "Pro" if tier == "pro" else "Free")))
        acct.setVisible(True)
        self.actions["upgrade"].setVisible(tier == "free")
        self.actions["signin"].setVisible(tier == "guest")

    def refresh_hotkeys(self):
        cfg = self._cfg()
        self._set_hint("dictate", hotkey_hint(cfg.get("hotkey", "alt+r")))
        if hasattr(self.app, "_registered_assist_hotkey"):
            hk = getattr(self.app, "_registered_assist_hotkey", None) or ""
            if not hk and getattr(self.app, "_assist_listener", None) is not None:
                hk = cfg.get("live_assist_hotkey", "")       # macOS / Linux register through pynput
        else:
            hk = cfg.get("live_assist_hotkey", "")
        self._set_hint("assist", hotkey_hint(hk))

    def refresh_privacy(self):
        a = self.actions["privacy"]
        a.setChecked(bool(self._cfg().get("privacy_mode", False)))

    def refresh_next(self, *_args):
        """The calendar's running meeting ("Now: ...") or next one (cached, no network) - or no row."""
        m, running = None, False
        cal = getattr(self.app, "calendar", None)
        if cal is not None:
            try:
                if getattr(cal, "available", True) and getattr(cal, "is_connected", True):
                    now_or_next = getattr(cal, "now_or_next", None)
                    if callable(now_or_next):
                        kind, m = now_or_next()
                        running = kind == "now"
                    else:                                  # a service from before "Now"
                        m = cal.next_meeting()
            except Exception:
                logger.debug("calendar now_or_next failed", exc_info=True)
                m, running = None, False
        self.set_next_meeting(m, running=running)

    def set_next_meeting(self, meeting, running=False):
        a, sep = self.actions["next"], self.actions["sep_next"]
        if not isinstance(meeting, dict) or not meeting.get("start"):
            a.setVisible(False)
            sep.setVisible(False)
            self._next = None
            return
        self._next = meeting
        title = str(meeting.get("title") or "").strip() or "Meeting"
        word = "Now:" if running else "Next:"
        a.setText(_esc(word + " " + title))
        a.setProperty(P_PREFIX, word)                      # drawn in the muted colour
        self._set_hint("next", "" if running else self._next_time(meeting))
        a.setVisible(True)
        sep.setVisible(True)

    @staticmethod
    def _next_time(meeting):
        try:
            import calendar_sync
            start = meeting["start"]
            now = datetime.now(timezone.utc) if start.tzinfo is not None else datetime.now()
            parts = calendar_sync.when_parts(meeting, now)
            return (parts["day"] + " " if parts.get("day") else "") + parts["time"]
        except Exception:
            try:
                return meeting["start"].astimezone().strftime("%H:%M")
            except Exception:
                return ""

    def attach_calendar(self, calendar):
        """Follow app.calendar (calendar_service.CalendarService): changed -> Next row.
        Safe to call again (each calendar is connected once)."""
        if calendar is not None and calendar is not self._calendar:
            try:
                calendar.changed.connect(self.refresh_next)
                calendar.disconnected.connect(self.refresh_next)
                self._calendar = calendar
            except Exception:
                logger.debug("tray: calendar signals unavailable", exc_info=True)
        self.refresh_next()

    def on_setting_changed(self, key):
        if key in ("hotkey", "live_assist_hotkey"):
            self.refresh_hotkeys()
        elif key == "privacy_mode":
            self.refresh_privacy()
        elif str(key).startswith("calendar_"):
            self.refresh_next()

    # ── Recent ──
    def refresh_recent(self, sync=False):
        """Reload Home's Recent rows on a worker (sync=True: right here)."""
        if self._recent_loading and not sync:
            return
        try:
            from ui.pages import home
        except Exception:
            logger.debug("tray: Home's Recent isn't available", exc_info=True)
            self._recent_items = []
            self._fill_recent()
            return
        self._recent_gen += 1
        gen = self._recent_gen
        self._recent_loading = True

        def load():
            return home.recent_items(*home.load_sources())[:RECENT_LIMIT]

        def done(items, g=gen):
            if g != self._recent_gen:
                return
            self._recent_loading = False
            self._recent_items = list(items or [])
            self._recent_at = time.monotonic()
            try:
                self._fill_recent()
            except RuntimeError:
                pass                                    # the menu was deleted meanwhile

        def failed(exc, g=gen):
            if g == self._recent_gen:
                self._recent_loading = False
            logger.warning("tray: Recent failed to load: %s", exc)

        try:
            home.run_later(load, done, error=failed, sync=sync)
        except Exception:
            self._recent_loading = False
            logger.warning("tray: Recent load didn't start", exc_info=True)

    def recent_items(self):
        return list(self._recent_items or [])

    def _fill_recent(self):
        m = self.recent
        m.clear()
        now = datetime.now()
        items = self._recent_items
        if items is not None and not items:
            e = QAction("Nothing yet", m)
            e.setProperty(P_KIND, "empty")
            e.setEnabled(False)
            m.addAction(e)
        for it in items or []:
            a = QAction(_esc(one_line(it.get("main"))), m)
            a.setProperty(P_KIND, "recent")
            a.setProperty(P_ICON, RECENT_ICONS.get(it.get("kind"), "file"))
            a.setProperty(P_HINT, recent_meta(it.get("when"), now))
            target = it.get("open")
            a.triggered.connect(lambda _=False, t=target: self.open_recent(t))
            m.addAction(a)
        if items:
            m.addSeparator()
        show_all = QAction("Show all in %s" % APP_NAME, m)
        show_all.setProperty(P_KIND, "item")
        show_all.triggered.connect(self.show_all)
        m.addAction(show_all)
        self.actions["show_all"] = show_all

    def _on_about_to_show(self):
        if self.custom:
            self._follow_os(self.menu)                  # the OS may have switched light / dark since
            self._follow_os(self.recent)
        self.refresh()
        if self._recent_items is None or time.monotonic() - self._recent_at > RECENT_STALE_S:
            self.refresh_recent()

    def _on_recent_show(self):
        if self._recent_items is None and not self._recent_loading:
            self.refresh_recent()

    # ── what the rows do ──
    def open_app(self):
        _call(self.app, "show_main")

    def open_next(self):
        _call(self.app, "show_main", "meetings")

    def dictate(self):
        if callable(getattr(self.app, "toggle_dictation", None)):
            _call(self.app, "toggle_dictation")
        else:
            _call(self.app, "_on_hotkey")

    def record_meeting(self):
        if self._pro():
            # The Meetings page's start card (it switches to the live view by
            # itself while a meeting records) - not the classic window.
            _call(self.app, "show_main", "meetings")
        else:
            _call(self.app, "_pro_upsell", FEATURE_MEETING)

    def live_assistant(self):
        # The card opens for everyone (Free and guests too); it asks for Pro
        # when Start is pressed, not here.
        _call(self.app, "toggle_live_assist")

    def convert_audio(self):
        _call(self.app, "show_main", "audio_to_text")

    def open_recent(self, target):
        if isinstance(target, (tuple, list)) and target:
            route = target[0]
            opts = dict(target[1]) if len(target) > 1 and isinstance(target[1], dict) else {}
            _call(self.app, "show_main", route, **opts)
        else:
            _call(self.app, "show_main", "home")

    def show_all(self):
        _call(self.app, "show_main", "home")

    def set_privacy(self, on):
        on = bool(on)
        before = bool(self._cfg().get("privacy_mode", False))
        if callable(getattr(self.app, "set_setting", None)):
            ok = _call(self.app, "set_setting", "privacy_mode", on)
        else:
            ok = _call(self.app, "set_privacy_mode", on) is not False
        self.refresh_privacy()
        now = bool(self._cfg().get("privacy_mode", False))
        if ok and now != before:
            if now:
                _call(self.app, "show_tray_hint", "Privacy mode on", "Everything stays on this computer.")
            else:
                _call(self.app, "show_tray_hint", "Privacy mode off", "Cloud features are back on.")

    def upgrade(self):
        _call(self.app, "_pro_upsell")

    def sign_in(self):
        _call(self.app, "show_auth_gate")

    def open_settings(self):
        _call(self.app, "show_main", "settings")

    def quit_app(self):
        _call(self.app, "quit_app")


def build_tray_menu(app, menu=None, custom=None):
    """Fill ``menu`` (a QMenu; created when None) with Gusan's tray rows and
    return the TrayMenu that keeps them current. ``custom`` forces the painted
    look on (True) or plain text (False); default: painted except on macOS."""
    return TrayMenu(app, menu, custom=custom)


__all__ = ["TrayMenu", "MenuLook", "build_tray_menu", "hotkey_hint", "recent_meta", "paint_pro_pill"]
