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

import logging
import os
import re
import sys

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QWidget

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
QToolButton[gusan="playBtn"] {{ background: {t['blue']}; border: 1px solid {t['blue-h']}; border-radius: 20px; }}
QToolButton[gusan="playBtn"]:hover {{ background: {t['blue-h']}; }}
QToolButton[gusan="playBtn"]:pressed, QToolButton[gusan="playBtn"][loading="true"] {{ background: #1e40af; }}
QToolButton[gusan="playBtn"][focusVisible="true"] {{ border: 2px solid #93c5fd; }}

/* ── pills, chips, keycaps ── */
QLabel[gusan="pill"] {{ border-radius: 9px; padding: 0px 10px; }}
QLabel[gusan="pill"][gsize="xs"] {{ padding: 0px 6px; }}
QLabel[gusan="pill"][gsize="s"] {{ padding: 0px 8px; }}
QLabel[gusan="pill"][tone="pro"] {{ background: {pro_pill}; color: #ffffff; border: 1px solid {c('rgba(255,255,255,0.59)')}; }}
QLabel[gusan="pill"][tone="free"] {{ background: {c('rgba(34,197,94,0.18)')}; color: #15803d; border: 1px solid {c('rgba(34,197,94,0.47)')}; }}
QLabel[gusan="pill"][tone="guest"] {{ background: {c('rgba(148,163,184,0.18)')}; color: #475569; border: 1px solid {c('rgba(148,163,184,0.35)')}; }}

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
