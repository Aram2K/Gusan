"""Gusan 2.0 look: design tokens, fonts and the stylesheet for v2 windows.

The values are the approved mockups' (scratchpad v2/gusan_ds.py TOKENS), which
are the current app's own colours (ui/styles.py) with the fills that carry
white text darkened just enough to read (#3b82f6 -> #2563eb, #a855f7 ->
#9333ea). Only v2 windows (the main window, its pages, new floating windows)
use this; the few small 1.x dialogs still shown (the Pro window and its
fallback box, the macOS permissions guide) keep ui/styles.py and their look.

Split of work:
- QSS (``QSS`` / ``apply()``): colours, backgrounds, borders, radii, paddings.
  Components are matched by the dynamic property ``gusan`` (plus ``kind``,
  ``size``, ``tone``, ``active`` ...), text roles by ``textRole``, so a page
  author's objectName never changes how something looks.
- Code (``font()`` / ``set_role()``): every font. Qt stylesheets can't pick
  the display face's optical style, letter-spacing or fractional pixel
  sizes, so fonts are never set in QSS - a stylesheet font would also
  override the display face chosen in code.
"""

import html as _html
import logging
import os
import re
import sys

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QWidget

from app_info import resource_path

logger = logging.getLogger("transcribe")

IS_MAC = sys.platform == "darwin"

# ── Tokens (gusan_ds.TOKENS, value for value) ────────────────────────────────
TOKENS = {
    "ink": "#0f172a", "text": "#1e293b", "text2": "#334155", "muted": "#64748b", "faint": "#94a3b8",
    "line": "#e2e8f0", "line2": "#cbd5e1", "bg": "#f8fafc", "bg2": "#f1f5f9", "white": "#ffffff",
    "blue": "#2563eb", "blue-h": "#1d4ed8", "blue-l": "#3b82f6", "blue-tx": "#1d4ed8",
    "blue-sub": "rgba(59,130,246,0.08)", "blue-line": "rgba(59,130,246,0.30)",
    "pro": "#9333ea", "pro-l": "#a855f7", "pro-tx": "#7e22ce", "pro-sub": "#faf5ff",
    "pro-line": "rgba(168,85,247,0.35)",
    "plan-a": "rgba(239,246,255,0.94)", "plan-b": "rgba(219,234,254,0.72)",
    "plan-line": "rgba(59,130,246,0.28)",
    "ok": "#16a34a", "ok-tx": "#15803d", "ok-sub": "#dcfce7", "ok-line": "#86efac",
    # Feature colours on Home (Dictate is blue, Smart Actions the Pro violet).
    "meet": "#ea580c", "live": "#16a34a",
    "red": "#dc2626", "red-tx": "#b91c1c", "red-sub": "#fee2e2",
    "amber": "#d97706", "amber-tx": "#92400e", "amber-sub": "#fef3c7",
    "glass-a": "rgba(255,255,255,0.90)", "glass-b": "rgba(241,245,249,0.76)",
    "glass-line": "rgba(203,213,225,0.55)",
    "window-a": "#eaf0f8", "window-b": "#f4f7fb", "window-c": "#e7eef7",
    "flag-red": "#d90012", "flag-blue": "#0033a0", "flag-orange": "#f2a800",
}

# Shadows (CSS "0 8px 24px rgba(15,23,42,0.06)" and friends) as
# (blur, y offset, colour) for QGraphicsDropShadowEffect.
SHADOWS = {
    "card": (24, 8, "rgba(15,23,42,0.06)"),
    "lg": (48, 24, "rgba(15,23,42,0.12)"),
    "item": (6, 1, "rgba(15,23,42,0.10)"),      # the active sidebar item / selected segment
    "key": (18, 6, "rgba(15,23,42,0.10)"),      # big keycaps
}

_RGBA = re.compile(r"rgba\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)")


def _parse(value):
    """'#rrggbb' or 'rgba(r,g,b,a)' (a = 0..1) -> (r, g, b, alpha 0..255)."""
    value = TOKENS.get(value, value)
    m = _RGBA.fullmatch(value.strip())
    if m:
        r, g, b, a = m.groups()
        return int(float(r)), int(float(g)), int(float(b)), int(round(float(a) * 255))
    c = QColor(value)
    return c.red(), c.green(), c.blue(), c.alpha()


def qcolor(value):
    """QColor for a token name ('blue') or any CSS colour."""
    r, g, b, a = _parse(value)
    return QColor(r, g, b, a)


def qss_color(value):
    """A token / CSS colour written the way Qt stylesheets read it (alpha 0-255)."""
    r, g, b, a = _parse(value)
    if a >= 255:
        return "#%02x%02x%02x" % (r, g, b)
    return "rgba(%d, %d, %d, %d)" % (r, g, b, a)


# ── One light look, whatever the OS is set to ────────────────────────────────
# Gusan's look is light only. Qt, though, follows the OS colour scheme for everything a
# stylesheet does not paint: a window's background, a button's fill, a menu's panel, a
# placeholder, a disabled word all come from the OS palette, which is DARK on a Windows set
# to dark mode. The stylesheet (build_qss) sets a text colour on every widget under the main
# window - and a dialog, a menu or a message box opened from a page is a child of it, so it
# got that dark-blue text on the OS's dark-grey panel: "Delete meeting" with a navy question
# on charcoal (about 1:1), the same for a rename box, the Export menu on Dictation and every
# text field's right-click menu. Two halves of one rule, so nothing depends on the OS again:
#   1. install_light_scheme() - called once, right after the QApplication exists (main() and the
#      test session), before any window: Qt's "Fusion" style (paints only from the palette, so
#      what the tests render is what runs; Windows 11's and macOS's own styles paint unstyled
#      controls from OS tables that ignore the palette) and an explicit LIGHT palette from the
#      tokens, every role in every group. Nothing else changes with the OS: the window frames and
#      title bars stay the OS's (the colour scheme itself is never touched), and the system tray
#      menu - drawn by the OS look on purpose - asks os_is_dark() and sets its own palette.
#   2. build_qss() paints the pop-ups itself (menus, tooltips, message boxes) so a window that
#      has the stylesheet is complete: text AND background from the same tokens.
# The pop-ups Gusan asks questions with are ui/components/mini_prompt.py cards (confirm, notify,
# choose, ask_text), never QMessageBox / QInputDialog (tests_qt/test_dialog_visibility.py
# renders them all in both schemes and refuses the old classes).
_SCHEME = {"os_dark": None, "hooked": False}


def light_palette():
    """The one palette every Gusan widget gets from the application: the tokens, in the active,
    inactive and disabled groups and for every role, so no role is left to the OS."""
    c = qcolor
    roles = {
        QPalette.WindowText: c("text"), QPalette.Button: c("white"), QPalette.Light: c("white"),
        QPalette.Midlight: c("bg2"), QPalette.Dark: c("faint"), QPalette.Mid: c("line2"),
        QPalette.Text: c("text"), QPalette.BrightText: c("white"), QPalette.ButtonText: c("text2"),
        QPalette.Base: c("white"), QPalette.Window: c("bg"), QPalette.Shadow: c("ink"),
        QPalette.Highlight: c("blue"), QPalette.HighlightedText: c("white"),
        QPalette.Link: c("blue-tx"), QPalette.LinkVisited: c("pro-tx"), QPalette.AlternateBase: c("bg2"),
        QPalette.ToolTipBase: c("ink"), QPalette.ToolTipText: c("white"),
        QPalette.PlaceholderText: c("muted"),
    }
    accent = getattr(QPalette, "Accent", None)            # Qt 6.6+
    if accent is not None:
        roles[accent] = c("blue")
    pal = QPalette()
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        for role, colour in roles.items():
            pal.setColor(group, role, colour)
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText, QPalette.PlaceholderText):
        pal.setColor(QPalette.Disabled, role, c("faint"))
    return pal


def dark_menu_palette():
    """The palette of an OS menu on a dark Windows (the tray menu follows the system)."""
    pal = QPalette()
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        for role, colour in ((QPalette.Window, "#2b2b2b"), (QPalette.Base, "#2b2b2b"),
                             (QPalette.Button, "#2b2b2b"), (QPalette.AlternateBase, "#333333"),
                             (QPalette.WindowText, "#ffffff"), (QPalette.Text, "#ffffff"),
                             (QPalette.ButtonText, "#ffffff"), (QPalette.BrightText, "#ffffff"),
                             (QPalette.Highlight, "#3a3a3a"), (QPalette.HighlightedText, "#ffffff"),
                             (QPalette.ToolTipBase, "#2b2b2b"), (QPalette.ToolTipText, "#ffffff"),
                             (QPalette.PlaceholderText, "#c2c2c2"), (QPalette.Link, "#4cc2ff"),
                             (QPalette.Light, "#4a4a4a"), (QPalette.Midlight, "#3a3a3a"),
                             (QPalette.Mid, "#3a3a3a"), (QPalette.Dark, "#202020"), (QPalette.Shadow, "#000000")):
            pal.setColor(group, role, QColor(colour))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#9d9d9d"))
    return pal


def _is_light(pal):
    """True for the palette light_palette() builds (the app already carries it)."""
    try:
        return (pal.color(QPalette.Window) == qcolor("bg") and pal.color(QPalette.Base) == qcolor("white")
                and pal.color(QPalette.Text) == qcolor("text"))
    except Exception:
        return False


def os_is_dark():
    """True when the OS is set to dark: what Qt reports (Windows 10/11 and macOS; Qt 6.5+), else the
    palette the application had before install_light_scheme() replaced it. For the few surfaces
    that follow the system on purpose (the tray menu); the app itself is light either way."""
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except Exception:
        pass
    return bool(_SCHEME["os_dark"])


def menu_palette():
    """The palette for a menu that follows the OS (see os_is_dark)."""
    return dark_menu_palette() if os_is_dark() else light_palette()


def _reassert_light():
    app = QApplication.instance()
    if app is None:
        return
    try:
        pal = app.palette()
        if not _is_light(pal):                 # the OS pushed its palette (dark mode switched while running)
            _SCHEME["os_dark"] = pal.color(QPalette.Window).lightness() < 128
            app.setPalette(light_palette())
    except Exception:
        logger.debug("could not keep the light palette", exc_info=True)


def install_light_scheme(app=None):
    """Make the application itself light (see "One light look" above). Call it once, right
    after the QApplication exists and before any window; safe to call again. True when applied."""
    app = app or QApplication.instance()
    if app is None:
        return False
    cur = app.palette()
    if not _is_light(cur):
        _SCHEME["os_dark"] = cur.color(QPalette.Window).lightness() < 128   # what the OS handed over
    try:
        if isinstance(app, QApplication) and (app.style() is None or app.style().objectName().lower() != "fusion"):
            app.setStyle("Fusion")
    except Exception:
        logger.debug("could not set the Fusion style", exc_info=True)
    app.setPalette(light_palette())
    try:
        QApplication.setFont(font(13.5), "QMenu")      # a plain menu (Export, a field's right-click) reads in Gusan's face
    except Exception:
        logger.debug("could not set the menu font", exc_info=True)
    if not _SCHEME["hooked"]:
        try:
            QGuiApplication.styleHints().colorSchemeChanged.connect(lambda *_a: QTimer.singleShot(0, _reassert_light))
            _SCHEME["hooked"] = True
        except Exception:
            logger.debug("no colorSchemeChanged signal", exc_info=True)
    return True


# ── Fonts ────────────────────────────────────────────────────────────────────
_FONTS = {"loaded": False, "display": None, "body": None, "bundled": []}

_WEIGHT_WORDS = {300: "Light", 350: "Semilight", 400: "Regular", 500: "Semibold",
                 600: "Semibold", 700: "Bold", 800: "Bold", 900: "Bold"}


def load_fonts():
    """Register bundled fonts and pick the display and body faces. Call once,
    right after QApplication exists (main() does; tests and the renderer too).

    - Display (titles): Outfit when its TTFs are bundled under assets/fonts/,
      else Segoe UI Variable's Display style on Windows, the system face on Mac.
    - Body: Segoe UI on Windows, the system face on Mac.
    Nothing is ever downloaded. Safe to call again; returns the picks."""
    if _FONTS["loaded"]:
        return dict(_FONTS)
    families = set()
    folder = resource_path("assets", "fonts")
    try:
        names = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
    except OSError:
        names = []
    for name in names:
        if not name.lower().endswith((".ttf", ".otf")):
            continue
        fid = QFontDatabase.addApplicationFont(os.path.join(folder, name))
        if fid >= 0:
            fams = QFontDatabase.applicationFontFamilies(fid)
            families.update(fams)
            _FONTS["bundled"].extend(fams)
        else:
            logger.debug("Could not load font %s", name)
    available = set(QFontDatabase.families())
    system = QFontDatabase.systemFont(QFontDatabase.GeneralFont).family()
    if "Outfit" in families or "Outfit" in available:
        display = "Outfit"
    elif IS_MAC:
        display = system
    elif "Segoe UI Variable" in available:
        display = "Segoe UI Variable"
    elif "Segoe UI" in available:
        display = "Segoe UI"
    else:
        display = system
    if IS_MAC:
        body = system
    elif "Segoe UI" in available:
        body = "Segoe UI"
    else:
        body = system
    _FONTS.update(loaded=True, display=display, body=body)
    return dict(_FONTS)


def display_family():
    return _FONTS["display"] or load_fonts()["display"]


def body_family():
    return _FONTS["body"] or load_fonts()["body"]


def _dpi():
    try:
        scr = QGuiApplication.primaryScreen()
        if scr is not None:
            return float(scr.logicalDotsPerInchY()) or 96.0
    except Exception:
        pass
    return 72.0 if IS_MAC else 96.0


def _display_style(family, weight):
    """Segoe UI Variable's optical 'Display' style for a weight, e.g.
    'Display Bold' (DirectWrite) or 'Bold Display' (FreeType), or None."""
    word = _WEIGHT_WORDS.get(weight, "Regular")
    want = {"Display", word}
    styles = QFontDatabase.styles(family)
    for s in styles:
        if set(s.split()) == want:
            return s
    if word == "Regular":
        for s in styles:
            if s.strip() == "Display":
                return s
    return None


def _font_tag(name):
    """QFont.Tag for an OpenType tag such as "tnum", or None. PySide6 6.8
    can't build a Tag from a string (no str constructor, no fromString), so
    features are skipped there - Segoe UI's digits are tabular anyway."""
    if name in _TAGS:
        return _TAGS[name]
    found = None
    maker = getattr(QFont.Tag, "fromString", None)
    for build in ((lambda: maker(name)) if maker else None, lambda: QFont.Tag(name)):
        if build is None:
            continue
        try:
            tag = build()
        except Exception:
            continue
        if tag is not None and tag.isValid():
            found = tag
            break
    _TAGS[name] = found
    return found


_TAGS = {}


def font(px, weight=400, display=False, letter_spacing=None, tabular=False):
    """A QFont of exactly ``px`` logical pixels (fractions allowed: 13.5).

    display=True uses the headline face. letter_spacing is in pixels (CSS
    em values are converted by the caller: 11px * 0.14em = 1.54)."""
    fam = display_family() if display else body_family()
    f = QFont(fam)
    # Point size from pixels through the logical DPI keeps fractional sizes
    # (setPixelSize only takes ints) and is right on Windows (96) and Mac (72).
    f.setPointSizeF(max(1.0, px * 72.0 / _dpi()))
    style = _display_style(fam, weight) if display and fam == "Segoe UI Variable" else None
    if style:
        f.setStyleName(style)
    else:
        # QFont.Weight only has the hundreds (Thin=100 ... Black=900).
        w = min(900, max(100, int(round(float(weight) / 100.0)) * 100))
        f.setWeight(QFont.Weight(w))
    if letter_spacing:
        f.setLetterSpacing(QFont.AbsoluteSpacing, float(letter_spacing))
    if tabular:
        tag = _font_tag("tnum")
        if tag is not None:
            f.setFeature(tag, 1)
    f.setHintingPreference(QFont.PreferNoHinting if IS_MAC else QFont.PreferDefaultHinting)
    return f


# ── Text roles ───────────────────────────────────────────────────────────────
# role -> (px, weight, display, letter_spacing px, uppercase)
ROLES = {
    "title":    (28, 700, True, -0.28, False),     # h1 28/34, -0.01em
    "h2":       (18, 600, True, None, False),
    "h3":       (14, 600, False, None, False),
    "brand":    (20, 700, True, -0.2, False),      # "Gusan" next to the logo
    "subtitle": (14, 400, False, None, False),
    "body":     (14, 400, False, None, False),
    "label":    (13.5, 600, False, None, False),   # setting row label
    "hint":     (12, 400, False, None, False),
    "muted":    (12.5, 400, False, None, False),
    "faint":    (12, 400, False, None, False),
    "model":    (12, 400, False, None, False),     # real model name under a plain name
    "eyebrow":  (11, 800, False, 1.54, True),      # 0.14em, uppercase
    "link":     (13.5, 600, False, None, False),
    "error":    (13, 400, False, None, False),
}


def set_role(widget, role):
    """Give a label a text role: its font here, its colour from the QSS."""
    px, weight, display, ls, upper = ROLES[role]
    widget.setProperty("textRole", role)
    widget.setFont(font(px, weight, display=display, letter_spacing=ls, tabular=(role == "model")))
    if upper and hasattr(widget, "text") and hasattr(widget, "setText"):
        widget.setText(widget.text().upper())
    repolish(widget)
    return widget


def plain_tip(text):
    """A tooltip that wraps, holding ``text`` as plain text: a tooltip is rich text
    unless told otherwise, and rich text loads an image it names - from the disk, or
    from a network share (a Windows UNC path is a connection to that host). Use it for
    every tooltip that can hold text somebody else wrote (a calendar title, a guest's
    name). "" for no text, so a tooltip can be cleared with it."""
    if not text:
        return ""
    return "<p style='white-space:normal'>%s</p>" % _html.escape(str(text), quote=False)


def repolish(widget, children=True):
    """Re-apply the stylesheet after a dynamic property changed."""
    try:
        st = widget.style()
        st.unpolish(widget)
        st.polish(widget)
        if children:
            for ch in widget.findChildren(QWidget):
                ch.style().unpolish(ch)
                ch.style().polish(ch)
        widget.update()
    except Exception:
        logger.debug("repolish failed", exc_info=True)


def set_prop(widget, name, value, children=True):
    """Set a dynamic property and restyle (no-op when unchanged)."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    repolish(widget, children=children)


class _FocusWatcher(QObject):
    """Sets focusVisible=true only for keyboard focus (Tab / Shift+Tab), so the
    QSS focus ring is CSS's :focus-visible - never left behind by a click or
    by the window giving its first button focus when it opens."""

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.FocusIn:
            kbd = ev.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
            set_prop(obj, "focusVisible", kbd, children=False)
        elif t == QEvent.FocusOut:
            set_prop(obj, "focusVisible", False, children=False)
        return False


_WATCHER = None


def focus_ring(widget):
    """Show the QSS focus ring on ``widget`` only when reached by keyboard."""
    global _WATCHER
    if _WATCHER is None:
        _WATCHER = _FocusWatcher()
    widget.setProperty("focusVisible", False)
    widget.installEventFilter(_WATCHER)
    return widget


def elevate(widget, level="card"):
    """A soft drop shadow (CSS box-shadow). level: card | lg | item | key | None."""
    if not level:
        widget.setGraphicsEffect(None)
        return None
    blur, dy, colour = SHADOWS[level]
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(qcolor(colour))
    widget.setGraphicsEffect(eff)
    return eff


# ── Stylesheet ───────────────────────────────────────────────────────────────
def _asset_url(name):
    return resource_path("assets", name).replace("\\", "/")


def build_qss():
    t = {k: qss_color(k) for k in TOKENS}
    glass = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %s, stop:1 %s)"
             % (t["glass-a"], t["glass-b"]))
    window = ("qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 %s, stop:0.5 %s, stop:1 %s)"
              % (t["window-a"], t["window-b"], t["window-c"]))
    plan = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %s, stop:1 %s)"
            % (t["plan-a"], t["plan-b"]))
    pro_pill = ("qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 %s, stop:1 %s)"
                % (qss_color("rgba(168,85,247,0.92)"), qss_color("rgba(126,34,206,0.92)")))
    pro_btn = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #a855f7, stop:0.55 #9333ea, stop:1 #7e22ce)"
    pro_btn_h = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #9333ea, stop:0.55 #7e22ce, stop:1 #6b21a8)"
    chev = _asset_url("chevron.svg")
    check = _asset_url("check.svg")
    check_ink = _asset_url("check_ink.svg")
    c = qss_color
    return f"""
/* ── window ── */
QMainWindow[gusan="window"], QDialog[gusan="window"], QWidget[gusan="root"] {{
    background: {window};
}}
QWidget {{ color: {t['text']}; }}
QLabel {{ background: transparent; }}
QToolTip {{
    background: {t['ink']}; color: #ffffff; border: none; border-radius: 6px; padding: 4px 8px;
}}

/* ── pop-ups: a menu, a text field's right-click menu and a stray message box are children of
      the window that has this sheet, so they get a background as well as a text colour (the
      text colour alone is what once left navy words on the OS's dark panel; see theme.py) ── */
QMenu {{
    background: #ffffff; color: {t['text']}; border: 1px solid {t['line2']}; padding: 4px;
}}
QMenu::item {{
    background: transparent; color: {t['text']}; padding: 7px 26px 7px 28px; border-radius: 6px;
}}
QMenu::item:selected {{ background: {t['blue-sub']}; color: {t['blue-tx']}; }}
QMenu::item:disabled {{ background: transparent; color: {t['faint']}; }}
QMenu::separator {{ height: 1px; background: {t['line']}; margin: 4px 6px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 8px; }}
QMenu::indicator:checked {{ image: url("{check_ink}"); }}
QMessageBox, QInputDialog {{ background: {t['bg']}; }}
QMessageBox QLabel, QInputDialog QLabel {{ color: {t['text']}; }}
QMessageBox QPushButton, QInputDialog QPushButton, QDialogButtonBox QPushButton {{
    background: #ffffff; color: {t['text2']}; border: 1px solid {t['line2']}; border-radius: 8px;
    padding: 6px 16px; min-width: 72px; min-height: 22px;
}}
QMessageBox QPushButton:hover, QInputDialog QPushButton:hover, QDialogButtonBox QPushButton:hover {{
    background: {t['bg']}; border-color: {t['blue-l']}; color: {t['text']};
}}
QMessageBox QPushButton:default, QInputDialog QPushButton:default, QDialogButtonBox QPushButton:default {{
    background: {t['blue']}; color: #ffffff; border-color: {t['blue-h']};
}}
QMessageBox QPushButton:disabled, QInputDialog QPushButton:disabled, QDialogButtonBox QPushButton:disabled {{
    background: {t['bg2']}; color: {t['faint']}; border-color: {t['line']};
}}
QScrollArea[gusan="scroll"] {{ background: transparent; border: none; }}
QScrollArea[gusan="scroll"] > QWidget#qt_scrollarea_viewport {{ background: transparent; }}
QWidget[gusan="page"], QWidget[gusan="scrollContent"] {{ background: transparent; }}

/* ── text roles (fonts are set in code) ── */
QLabel[textRole="title"], QLabel[textRole="h2"], QLabel[textRole="h3"], QLabel[textRole="brand"] {{ color: {t['ink']}; }}
QLabel[textRole="subtitle"], QLabel[textRole="muted"], QLabel[textRole="hint"], QLabel[textRole="model"] {{ color: {t['muted']}; }}
QLabel[textRole="body"] {{ color: {t['text2']}; }}
QLabel[textRole="label"] {{ color: {t['text']}; }}
QLabel[textRole="faint"] {{ color: {t['faint']}; }}
QLabel[textRole="eyebrow"] {{ color: {t['blue-tx']}; }}
QLabel[textRole="link"] {{ color: {t['blue-tx']}; }}
QLabel[textRole="error"] {{ color: {t['red-tx']}; }}

/* ── surfaces ── */
QFrame[gusan="card"] {{
    background: {glass}; border: 1px solid {t['glass-line']}; border-radius: 16px;
}}
QFrame[gusan="whiteCard"] {{
    background: #ffffff; border: 1px solid {t['line']}; border-radius: 12px;
}}
QFrame[gusan="tierCard"] {{
    background: {plan}; border: 1px solid {t['plan-line']}; border-radius: 16px;
}}
QFrame[gusan="divider"] {{
    background: {t['line']}; border: none; min-height: 1px; max-height: 1px;
}}
QLabel[gusan="iconTile"] {{
    background: {t['blue-sub']}; border: none; border-radius: 12px;
}}

/* ── buttons ── */
QPushButton[gusan="btn"] {{
    border-radius: 8px; padding: 0px 16px; text-align: center;
}}
QPushButton[gusan="btn"][trail="true"] {{ text-align: left; }}
QPushButton[gusan="btn"][gsize="s"] {{ padding: 0px 12px; }}
QPushButton[gusan="btn"][gsize="l"] {{ padding: 0px 20px; border-radius: 10px; }}
QPushButton[gusan="btn"][gsize="xl"] {{ padding: 0px 24px; border-radius: 10px; }}
QPushButton[gusan="btn"][pill="true"][gsize="s"] {{ border-radius: 15px; }}
QPushButton[gusan="btn"][pill="true"][gsize="m"] {{ border-radius: 18px; }}
QPushButton[gusan="btn"][pill="true"][gsize="l"] {{ border-radius: 22px; }}
QPushButton[gusan="btn"][pill="true"][gsize="xl"] {{ border-radius: 26px; }}

QPushButton[gusan="btn"][kind="primary"] {{ background: {t['blue']}; color: #ffffff; border: 1px solid {t['blue-h']}; }}
QPushButton[gusan="btn"][kind="primary"]:hover {{ background: {t['blue-h']}; }}
QPushButton[gusan="btn"][kind="primary"]:pressed {{ background: #1e40af; }}
QPushButton[gusan="btn"][kind="primary"][focusVisible="true"] {{ border: 2px solid #93c5fd; }}

QPushButton[gusan="btn"][kind="secondary"] {{ background: #ffffff; color: {t['text2']}; border: 1px solid {t['line2']}; }}
QPushButton[gusan="btn"][kind="secondary"]:hover {{ background: {t['bg']}; border-color: {t['blue-l']}; color: {t['text']}; }}
QPushButton[gusan="btn"][kind="secondary"]:pressed {{ background: {t['bg2']}; }}

QPushButton[gusan="btn"][kind="ghost"] {{ background: transparent; color: {t['text2']}; border: 1px solid transparent; }}
QPushButton[gusan="btn"][kind="ghost"]:hover {{ background: {c('rgba(15,23,42,0.05)')}; color: {t['text']}; }}
QPushButton[gusan="btn"][kind="ghost"]:pressed {{ background: {c('rgba(15,23,42,0.08)')}; }}

QPushButton[gusan="btn"][kind="link"] {{ background: transparent; color: {t['blue-tx']}; border: 1px solid transparent; padding: 0px 2px; }}
QPushButton[gusan="btn"][kind="link"]:hover {{ color: {t['blue']}; }}
QPushButton[gusan="btn"][kind="link"][room="true"] {{ padding: 0px 6px; }}   /* the keyboard ring has air around the word */

QPushButton[gusan="btn"][kind="pro"] {{ background: {pro_btn}; color: #ffffff; border: 1px solid {t['pro']}; }}
QPushButton[gusan="btn"][kind="pro"]:hover {{ background: {pro_btn_h}; }}
QPushButton[gusan="btn"][kind="pro"][focusVisible="true"] {{ border: 2px solid #d8b4fe; }}

QPushButton[gusan="btn"][kind="danger"] {{ background: {t['red']}; color: #ffffff; border: 1px solid #b91c1c; }}
QPushButton[gusan="btn"][kind="danger"]:hover {{ background: #b91c1c; }}

QPushButton[gusan="btn"][kind="dark"] {{ background: {t['ink']}; color: #ffffff; border: 1px solid {t['ink']}; }}
QPushButton[gusan="btn"][kind="dark"]:hover {{ background: #1e293b; }}

QPushButton[gusan="btn"][focusVisible="true"] {{ border: 2px solid {t['blue-l']}; }}
QPushButton[gusan="btn"]:disabled {{ background: {t['bg2']}; color: {t['faint']}; border: 1px solid {t['line']}; }}

QPushButton[gusan="upgradeQuiet"] {{
    background: {t['pro-sub']}; border: 1px solid {t['pro-line']}; color: {t['pro-tx']};
    border-radius: 8px; padding: 0px 10px;
}}
QPushButton[gusan="upgradeQuiet"]:hover {{ background: #f3e8ff; }}
QPushButton[gusan="upgradeQuiet"][focusVisible="true"] {{ border: 2px solid {t['pro-l']}; }}

QToolButton[gusan="iconBtn"], QPushButton[gusan="iconBtn"] {{
    background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 0px;
}}
QToolButton[gusan="iconBtn"]:hover, QPushButton[gusan="iconBtn"]:hover {{ background: {c('rgba(15,23,42,0.06)')}; }}
QToolButton[gusan="iconBtn"]:pressed, QPushButton[gusan="iconBtn"]:pressed {{ background: {c('rgba(15,23,42,0.10)')}; }}
QToolButton[gusan="iconBtn"][focusVisible="true"], QPushButton[gusan="iconBtn"][focusVisible="true"] {{ border: 2px solid {t['blue-l']}; }}
QToolButton[gusan="iconBtn"][framed="true"], QPushButton[gusan="iconBtn"][framed="true"] {{
    background: #ffffff; border: 1px solid {t['line2']};
}}
QToolButton[gusan="iconBtn"][framed="true"][focusVisible="true"], QPushButton[gusan="iconBtn"][framed="true"][focusVisible="true"] {{
    border: 2px solid {t['blue-l']};
}}
QToolButton[gusan="playBtn"] {{ background: {t['blue']}; border: 1px solid {t['blue-h']}; border-radius: 20px; }}
QToolButton[gusan="playBtn"]:hover {{ background: {t['blue-h']}; }}
QToolButton[gusan="playBtn"]:pressed, QToolButton[gusan="playBtn"][loading="true"] {{ background: #1e40af; }}
QToolButton[gusan="playBtn"][focusVisible="true"] {{ border: 2px solid #93c5fd; }}

/* ── delete from a list (ui/components/row_actions.py) ── */
QToolButton[gusan="trashBtn"] {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 0px; }}
QToolButton[gusan="trashBtn"]:hover {{ background: {t['red-sub']}; border: 1px solid #fecaca; }}
QToolButton[gusan="trashBtn"]:pressed {{ background: #fecaca; }}
QToolButton[gusan="trashBtn"]:disabled {{ background: transparent; border: 1px solid transparent; }}
QFrame[gusan="actionBar"] {{ background: {t['blue-sub']}; border: 1px solid {t['blue-line']}; border-radius: 12px; }}
QFrame[gusan="actionBar"][tone="error"] {{ background: {t['red-sub']}; border: 1px solid #fecaca; }}
QFrame[gusan="actionBar"][docked="true"] {{ background: #ffffff; border: 1px solid {t['blue-line']}; }}
QFrame[gusan="actionBar"][docked="true"][tone="error"] {{ background: {t['red-sub']}; border: 1px solid #fecaca; }}

/* ── pills, chips, keycaps ── */
QLabel[gusan="pill"] {{ border-radius: 9px; padding: 0px 10px; }}
QLabel[gusan="pill"][gsize="xs"] {{ padding: 0px 6px; }}
QLabel[gusan="pill"][gsize="s"] {{ padding: 0px 8px; }}
QLabel[gusan="pill"][tone="pro"] {{ background: {pro_pill}; color: #ffffff; border: 1px solid {c('rgba(255,255,255,0.59)')}; }}
QLabel[gusan="pill"][tone="free"] {{ background: {c('rgba(34,197,94,0.18)')}; color: #15803d; border: 1px solid {c('rgba(34,197,94,0.47)')}; }}
QLabel[gusan="pill"][tone="guest"] {{ background: {c('rgba(148,163,184,0.18)')}; color: #475569; border: 1px solid {c('rgba(148,163,184,0.35)')}; }}
QLabel[gusan="pill"][tone="local"] {{ background: {t['ok-sub']}; color: {t['ok-tx']}; border: 1px solid {t['ok-line']}; }}
QLabel[gusan="pill"][tone="cloud"] {{ background: {t['blue-sub']}; color: {t['blue-tx']}; border: 1px solid {t['blue-line']}; }}

QFrame[gusan="chip"] {{ border-radius: 12px; }}
QFrame[gusan="chip"][tone="neutral"] {{ background: {t['bg2']}; border: 1px solid {t['line']}; }}
QFrame[gusan="chip"][tone="neutral"] QLabel {{ color: {t['text2']}; }}
QFrame[gusan="chip"][tone="blue"] {{ background: {t['blue-sub']}; border: 1px solid {t['blue-line']}; }}
QFrame[gusan="chip"][tone="blue"] QLabel {{ color: {t['blue-tx']}; }}
QFrame[gusan="chip"][tone="pro"] {{ background: {t['pro-sub']}; border: 1px solid {t['pro-line']}; }}
QFrame[gusan="chip"][tone="pro"] QLabel {{ color: {t['pro-tx']}; }}
QFrame[gusan="chip"][tone="ok"] {{ background: {t['ok-sub']}; border: 1px solid {t['ok-line']}; }}
QFrame[gusan="chip"][tone="ok"] QLabel {{ color: {t['ok-tx']}; }}
QFrame[gusan="chip"][tone="red"] {{ background: {t['red-sub']}; border: 1px solid #fecaca; }}
QFrame[gusan="chip"][tone="red"] QLabel {{ color: {t['red-tx']}; }}
QFrame[gusan="chip"][tone="amber"] {{ background: {t['amber-sub']}; border: 1px solid #fde68a; }}
QFrame[gusan="chip"][tone="amber"] QLabel {{ color: {t['amber-tx']}; }}

QLabel[gusan="kbd"] {{
    background: #ffffff; color: {t['ink']}; border: 1px solid {t['line2']}; border-bottom: 2px solid {t['line2']};
    border-radius: 6px; padding: 0px 7px;
}}
QLabel[gusan="kbd"][big="true"] {{ border-bottom: 4px solid {t['line2']}; border-radius: 12px; padding: 0px 16px; }}
QLabel[gusan="kbdPlus"] {{ color: {t['faint']}; }}

/* ── segmented ── */
QFrame[gusan="segmented"] {{ background: {c('rgba(226,232,240,0.7)')}; border: none; border-radius: 17px; }}
QFrame[gusan="segmented"][gsize="s"] {{ border-radius: 15px; }}
QPushButton[gusan="seg"] {{
    background: transparent; color: {t['muted']}; border: none; border-radius: 14px; padding: 0px 14px;
}}
QFrame[gusan="segmented"][gsize="s"] QPushButton[gusan="seg"] {{ border-radius: 12px; }}
QPushButton[gusan="seg"]:hover {{ color: {t['text2']}; }}
QPushButton[gusan="seg"]:checked {{ background: #ffffff; color: {t['blue-tx']}; }}
QPushButton[gusan="seg"][focusVisible="true"] {{ border: 2px solid {t['blue-l']}; }}

/* ── inputs ── */
QLineEdit, QPlainTextEdit, QTextEdit {{
    background: #ffffff; color: {t['text']}; border: 1px solid {t['line2']}; border-radius: 8px;
    padding: 6px 12px; selection-background-color: {t['blue']}; selection-color: #ffffff;
    placeholder-text-color: {t['muted']};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {t['blue-l']}; }}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
    background: {t['bg2']}; color: {t['faint']}; border-color: {t['line']};
}}
QComboBox {{
    background: #ffffff; color: {t['text']}; border: 1px solid {t['line2']}; border-radius: 8px;
    padding: 0px 30px 0px 12px; min-height: 34px;
}}
QComboBox:hover, QComboBox:focus {{ border-color: {t['blue-l']}; }}
QComboBox:disabled {{ background: {t['bg2']}; color: {t['faint']}; border-color: {t['line']}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right; width: 28px; border: none; background: transparent;
}}
QComboBox::down-arrow, QComboBox::down-arrow:on {{
    image: url("{chev}"); width: 12px; height: 8px; margin-right: 10px;
}}
QComboBox QAbstractItemView {{
    background: #ffffff; border: 1px solid {t['line2']}; border-radius: 8px; outline: 0px; padding: 4px;
    color: {t['text']}; selection-background-color: {t['blue-sub']}; selection-color: {t['blue-tx']};
}}
QComboBox QAbstractItemView::item {{ min-height: 30px; padding: 4px 10px; border-radius: 6px; }}
QComboBox QAbstractItemView::item:hover {{ background: {t['bg2']}; color: {t['ink']}; }}
QComboBox QAbstractItemView::item:selected {{ background: {t['blue-sub']}; color: {t['blue-tx']}; }}
QCheckBox, QRadioButton {{ spacing: 10px; color: {t['text']}; background: transparent; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border: 1.5px solid {t['line2']}; border-radius: 5px; background: #ffffff;
}}
QCheckBox::indicator:hover {{ border-color: {t['blue-l']}; }}
QCheckBox::indicator:checked {{ border-color: {t['blue']}; background: {t['blue']}; image: url("{check}"); }}
QCheckBox::indicator:disabled {{ background: {t['bg2']}; border-color: {t['line']}; }}
QProgressBar {{ background: {t['line']}; border: none; border-radius: 3px; max-height: 6px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 3px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #60a5fa); }}

/* ── scrollbars: thin, quiet ── */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px 2px 2px 0px; }}
QScrollBar::handle:vertical {{ background: {t['line2']}; min-height: 32px; border-radius: 4px; }}
QScrollBar::handle:vertical:hover {{ background: {t['faint']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0px 2px 2px 2px; }}
QScrollBar::handle:horizontal {{ background: {t['line2']}; min-width: 32px; border-radius: 4px; }}
QScrollBar::handle:horizontal:hover {{ background: {t['faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* ── settings rail + rows ── */
QFrame[gusan="rail"] {{ background: {glass}; border: 1px solid {t['glass-line']}; border-radius: 16px; }}
QFrame[gusan="railHead"] {{ background: transparent; border: none; border-bottom: 1px solid {t['line']}; border-radius: 0px; }}
QFrame[gusan="railFoot"] {{
    background: {c('rgba(248,250,252,0.6)')}; border: none; border-top: 1px solid {t['line']};
    border-radius: 0px; border-bottom-left-radius: 15px; border-bottom-right-radius: 15px;
}}
QFrame[gusan="row"] {{ background: transparent; border: none; border-bottom: 1px solid {t['line']}; border-radius: 0px; }}
QFrame[gusan="row"][last="true"] {{ border-bottom: none; }}

/* ── sidebar ── */
QFrame[gusan="sidebar"] {{
    background: {c('rgba(255,255,255,0.45)')}; border: none; border-right: 1px solid {t['glass-line']}; border-radius: 0px;
}}
QPushButton[gusan="nav"] {{ background: transparent; border: none; border-radius: 10px; text-align: left; padding: 0px; }}
QPushButton[gusan="nav"]:hover {{ background: {c('rgba(255,255,255,0.6)')}; }}
QPushButton[gusan="nav"][focusVisible="true"] {{ border: 2px solid {t['blue-l']}; }}
QPushButton[gusan="nav"][active="true"] {{ background: #ffffff; }}
QPushButton[gusan="nav"] QLabel {{ color: {t['text2']}; }}
QPushButton[gusan="nav"][active="true"] QLabel[navText="true"] {{ color: {t['blue-tx']}; }}
QFrame[gusan="planCard"] {{
    background: {c('rgba(255,255,255,0.7)')}; border: 1px solid {t['glass-line']}; border-radius: 12px;
}}
QPushButton[gusan="accountRow"] {{ background: transparent; border: none; border-radius: 10px; padding: 0px; text-align: left; }}
QPushButton[gusan="accountRow"]:hover {{ background: {c('rgba(255,255,255,0.6)')}; }}
QPushButton[gusan="accountRow"][focusVisible="true"] {{ border: 2px solid {t['blue-l']}; }}
QPushButton[gusan="accountRow"][active="true"] {{ background: #ffffff; }}
QPushButton[gusan="accountRow"] QLabel[navText="true"] {{ color: {t['ink']}; }}
"""


QSS = None


def stylesheet():
    """The v2 stylesheet (built once; needs a QGuiApplication for colours)."""
    global QSS
    if QSS is None:
        QSS = build_qss()
    return QSS


def apply(window):
    """Style a v2 top-level window: stylesheet, body font, window gradient."""
    load_fonts()
    window.setProperty("gusan", "window")
    window.setFont(font(14))
    window.setStyleSheet(stylesheet())
    return window
