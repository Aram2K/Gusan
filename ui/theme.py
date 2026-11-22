"""Gusan look: design tokens (a light and a dark set), fonts and the stylesheet.

The light values are the approved 2.0 mockups' (scratchpad v2/gusan_ds.py TOKENS), the dark
ones the approved dark-mode mockups' (canvas_gen/common.py DARK). Every colour the app paints
comes from the ACTIVE set (``TOKENS``), so one switch changes them all:

- QSS (``stylesheet()`` / ``apply()``): colours, backgrounds, borders, radii, paddings.
  Components are matched by the dynamic property ``gusan`` (plus ``kind``,
  ``size``, ``tone``, ``active`` ...), text roles by ``textRole``, so a page
  author's objectName never changes how something looks.
- Code (``font()`` / ``set_role()``): every font. Qt stylesheets can't pick
  the display face's optical style, letter-spacing or fractional pixel
  sizes, so fonts are never set in QSS - a stylesheet font would also
  override the display face chosen in code.
- Paint code reads ``qcolor(token)`` when it paints (a switch repaints everything).
- A widget with a sheet of its own sets it through ``sheet(widget, builder)``; anything else
  that bakes a colour in (HTML, a cached picture) re-makes it in an ``on_change(owner, fn)``
  hook. Icons from ui/components/icons.py drawn in a token colour are re-tinted by themselves.

Light, dark or the computer's choice: the ``app_theme`` setting ("system" | "light" | "dark",
Settings > General > Appearance). See "Light and dark" below.
"""

import html as _html
import logging
import time
import os
import re
import sys
import weakref

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QWidget

from app_info import resource_path

logger = logging.getLogger("transcribe")

IS_MAC = sys.platform == "darwin"

# ── Tokens ───────────────────────────────────────────────────────────────────
# The light set is the 2.0 look, value for value, with one fix: muted. #64748b read 4.4:1 on the cards and
# #5f6f86 4.46:1 straight on the window's gradient and 4.27:1 as an unpicked option on a segmented control's
# grey track; #58677d reads 4.9:1 and 4.75:1 there. Keys added for dark mode carry the light look's own value,
# so the light picture is unchanged: surface (a card, a button, a menu), input (a field), popup (a menu or a
# list), on-accent (words on a blue, red or purple fill), hover / press overlays and the rest.
LIGHT = {
    "ink": "#0f172a", "text": "#1e293b", "text2": "#334155", "muted": "#58677d", "faint": "#94a3b8",
    "line": "#e2e8f0", "line2": "#cbd5e1", "bg": "#f8fafc", "bg2": "#f1f5f9",
    "surface": "#ffffff", "surface2": "#f8fafc", "input": "#ffffff", "popup": "#ffffff",
    "on-accent": "#ffffff",
    "blue": "#2563eb", "blue-h": "#1d4ed8", "blue-l": "#3b82f6", "blue-tx": "#1d4ed8", "blue-p": "#1e40af",
    "blue-sub": "rgba(59,130,246,0.08)", "blue-line": "rgba(59,130,246,0.30)", "focus": "#93c5fd",
    "pro": "#9333ea", "pro-l": "#a855f7", "pro-tx": "#7e22ce", "pro-sub": "#faf5ff", "pro-sub-h": "#f3e8ff",
    "pro-line": "rgba(168,85,247,0.35)", "pro-focus": "#d8b4fe",
    "plan-a": "rgba(239,246,255,0.94)", "plan-b": "rgba(219,234,254,0.72)",
    "plan-line": "rgba(59,130,246,0.28)",
    "ok": "#16a34a", "ok-tx": "#15803d", "ok-sub": "#dcfce7", "ok-line": "#86efac",
    "free-sub": "rgba(34,197,94,0.18)", "free-line": "rgba(34,197,94,0.47)",
    "guest-sub": "rgba(148,163,184,0.18)", "guest-tx": "#475569", "guest-line": "rgba(148,163,184,0.35)",
    # Feature colours on Home (Dictate is blue, Smart Actions the Pro violet).
    "meet": "#ea580c", "live": "#16a34a",
    "red": "#dc2626", "red-tx": "#b91c1c", "red-sub": "#fee2e2", "red-line": "#fecaca", "red-press": "#fecaca",
    # Fills that carry white words keep their strength in both looks (white on them passes 4.5:1).
    "danger": "#dc2626", "danger-h": "#b91c1c",
    "amber": "#d97706", "amber-tx": "#92400e", "amber-sub": "#fef3c7", "amber-line": "#fde68a",
    "glass-a": "rgba(255,255,255,0.90)", "glass-b": "rgba(241,245,249,0.76)",
    "glass-line": "rgba(203,213,225,0.55)",
    "window-a": "#eaf0f8", "window-b": "#f4f7fb", "window-c": "#e7eef7",
    "sidebar": "rgba(255,255,255,0.45)", "nav-hover": "rgba(255,255,255,0.6)", "nav-active": "#ffffff",
    "plan-card": "rgba(255,255,255,0.7)", "rail-foot": "rgba(248,250,252,0.6)",
    "hover": "rgba(15,23,42,0.05)", "press": "rgba(15,23,42,0.08)",
    "hover2": "rgba(15,23,42,0.06)", "press2": "rgba(15,23,42,0.10)",
    "seg-track": "rgba(226,232,240,0.7)", "seg-on": "#ffffff",
    "kbd-a": "#ffffff", "kbd-b": "#ffffff",
    "track": "#e2e8f0", "knob": "#ffffff",
    "tip-bg": "#0f172a", "tip-fg": "#ffffff",
    # A button in the ink colour (kind="dark"): it turns light in the dark look.
    "inv-bg": "#0f172a", "inv-fg": "#ffffff", "inv-h": "#1e293b",
    # Qt palette roles Fusion draws bevels with.
    "pal-light": "#ffffff", "pal-dark": "#94a3b8", "pal-shadow": "#0f172a",
    # A soft shade under a painted card or knob (the look's shadow colour at full strength).
    "shade": "#0f172a",
    # home / settings / components
    "teams-sub": "#ede9fe", "teams-tx": "#5b21b6",             # a Teams call's mark (glyphs.CALL_TONES)
    "guest-face-a": "#e8eef6", "guest-face-b": "#cbd5e1", "guest-face-icon": "#475569",   # a guest's avatar
    "cap-b": "#f8fafc", "cap-shadow": "rgba(15,23,42,0.12)",  # the big keycaps: face bottom, their shadow
    "frost-45": "rgba(255,255,255,0.45)", "frost-55": "rgba(255,255,255,0.55)",   # frosted panels over the glass
    "frost-60": "rgba(255,255,255,0.6)", "frost-65": "rgba(255,255,255,0.65)",
    "frost-70": "rgba(255,255,255,0.7)", "frost-72": "rgba(255,255,255,0.72)", "frost-90": "rgba(255,255,255,0.9)",
    "rec": "#e9152d",                                          # the recording dot (the dictation pill's red)
    "focus-glow": "rgba(59,130,246,0.12)",                     # the glow around a focused practice box
    "mark-sub": "rgba(59,130,246,0.14)",                       # a marked word in rich text (Dictionary's proof)
    "icon-blue": "#2563eb", "icon-pro": "#9333ea",            # an icon in the accent colours (lighter in the dark)
    # meetings / audio
    "pink-sub": "#fce7f3", "pink-tx": "#be185d",              # a face's initials (one of the six tones)
    "glass-solid": "#fcfdfe",                                 # a glass card's colour, as one solid (a fade into it)
    "row-hover": "rgba(15,23,42,0.039)", "row-press": "rgba(15,23,42,0.063)",   # a list row under the pointer
    "drop-fill": "#dbeafe",                                   # Audio to text's drop zone (alpha set in code)
    # the Record button: its well, its domed cap (raised / pressed) and the cap's rim light
    "rec-well-a": "rgba(203,213,225,0.588)", "rec-well-b": "rgba(226,232,240,0.431)",
    "rec-well-c": "rgba(255,255,255,0.784)",
    "rec-cap-a": "#ffffff", "rec-cap-b": "#e9edf2", "rec-cap-da": "#eef1f5", "rec-cap-db": "#fbfcfd",
    "rec-rim": "rgba(255,255,255,0.902)",
    # the blue-to-violet hero button (Open Live Assistant): strong fills under white words in both looks
    "hero-a": "#3b82f6", "hero-b": "#a855f7", "hero-ha": "#2563eb", "hero-hb": "#9333ea",
    # windows / dialogs (the welcome, the sign-in sheet, the Pro window, the meeting reminder, the Mac guide)
    "text3": "#475569",                                       # a quiet second line (slate 600)
    "warn-tx": "#b45309",                                     # "Almost done: restart" (the Mac guide)
    "glass-btn": "rgba(255,255,255,0.78)",                    # a button on a glass plate (the reminder)
    "chip-glass": "rgba(255,255,255,0.6)",                    # a small glass chip on the window (the welcome)
    "mic-ring": "rgba(255,255,255,0.702)",                    # the ring around the welcome's mic button
    "cta": "#3b82f6", "cta-h": "#2563eb", "cta-line": "#2563eb", "cta-hl": "#60a5fa", "cta-p": "#1d4ed8",
    "cta-off": "rgba(59,130,246,0.157)", "cta-off-tx": "rgba(255,255,255,0.706)",   # the 1.x blue button (ui/styles.py)
    "popup-line": "#d5dce6", "sel-sub": "#eef4ff",            # the 1.x list window's edge and picked row
    "pro-track": "#eef2f7", "pro-edge": "#e9d5ff",            # the Pro window's plan toggle track, a plan card's edge
    "pro-btn": "#a855f7",                                     # its checkout button (white words: the brand's 3:1)
    "save-on": "#166534", "save-on-tx": "#bbf7d0",            # "-38%" on the toggle's thumb
    # live / pill / tray: the Live Assistant page's document tiles (a Word file's pale blue, a picture's sky blue,
    # a problem's amber icon)
    "tile-doc": "#dbeafe", "tile-img": "#e0f2fe", "tile-img-tx": "#0369a1", "tile-alert-tx": "#b45309",
    # Invite friends: a deep violet fill under white words (the ready ticket, the READY pill, the PRO pill on a
    # claimed ticket) that is also the words on the white "Claim my month" button over it - the same in both looks;
    # a friend not there yet in the sidebar's three dots; the light edge a dark confetti piece gets on a dark window.
    "pro-deep": "#7e22ce", "dot-off": "#cbd5e1", "confetti-rim": "rgba(255,255,255,0)",
    # The Armenian flag in the logo: brand colours, the same in both looks.
    "flag-red": "#d90012", "flag-blue": "#0033a0", "flag-orange": "#f2a800",
}

# Dark: never pure black (a deep blue-grey window), off-white text, calmer lighter accents for
# words, the same strong fills under white words, depth from slightly lighter surfaces.
DARK = {
    "ink": "#e8edf4", "text": "#d5dbe4", "text2": "#b6bfcc", "muted": "#8d97a8", "faint": "#6b7587",
    "line": "#252c37", "line2": "#323b49", "bg": "#12161d", "bg2": "#181d25",
    "surface": "#1b212a", "surface2": "#161b23", "input": "#11161d", "popup": "#1f2630",
    "on-accent": "#ffffff",
    "blue": "#2563eb", "blue-h": "#3b74f0", "blue-l": "#60a5fa", "blue-tx": "#82adff", "blue-p": "#1d4ed8",
    "blue-sub": "rgba(96,165,250,0.12)", "blue-line": "rgba(96,165,250,0.32)", "focus": "#93c5fd",
    "pro": "#9333ea", "pro-l": "#c084fc", "pro-tx": "#c9a7ff", "pro-sub": "rgba(168,85,247,0.14)",
    "pro-sub-h": "rgba(168,85,247,0.22)",
    "pro-line": "rgba(192,132,252,0.36)", "pro-focus": "#d8b4fe",
    "plan-a": "rgba(96,165,250,0.13)", "plan-b": "rgba(96,165,250,0.07)",
    "plan-line": "rgba(96,165,250,0.30)",
    "ok": "#22c55e", "ok-tx": "#6fdc9a", "ok-sub": "rgba(34,197,94,0.14)", "ok-line": "rgba(74,222,128,0.35)",
    "free-sub": "rgba(34,197,94,0.14)", "free-line": "rgba(74,222,128,0.35)",
    "guest-sub": "rgba(148,163,184,0.14)", "guest-tx": "#b6bfcc", "guest-line": "rgba(148,163,184,0.30)",
    "meet": "#fb923c", "live": "#4ade80",
    "red": "#ef4444", "red-tx": "#fb8c8c", "red-sub": "rgba(239,68,68,0.15)", "red-line": "rgba(248,113,113,0.35)",
    "red-press": "rgba(239,68,68,0.25)",
    "danger": "#dc2626", "danger-h": "#b91c1c",
    "amber": "#f59e0b", "amber-tx": "#f6c35e", "amber-sub": "rgba(245,158,11,0.14)",
    "amber-line": "rgba(251,191,36,0.35)",
    "glass-a": "rgba(255,255,255,0.055)", "glass-b": "rgba(255,255,255,0.03)",
    "glass-line": "rgba(148,163,184,0.15)",
    "window-a": "#0e1218", "window-b": "#121721", "window-c": "#0f141b",
    "sidebar": "rgba(255,255,255,0.025)", "nav-hover": "rgba(255,255,255,0.05)",
    "nav-active": "rgba(255,255,255,0.075)",
    "plan-card": "rgba(255,255,255,0.04)", "rail-foot": "rgba(255,255,255,0.025)",
    "hover": "rgba(255,255,255,0.05)", "press": "rgba(255,255,255,0.08)",
    "hover2": "rgba(255,255,255,0.07)", "press2": "rgba(255,255,255,0.10)",
    "seg-track": "rgba(255,255,255,0.06)", "seg-on": "#2a323e",
    "kbd-a": "#232a35", "kbd-b": "#1b212a",
    "track": "#262e3a", "knob": "#e8edf4",
    "tip-bg": "#323a47", "tip-fg": "#e8edf4",
    "inv-bg": "#e8edf4", "inv-fg": "#0f172a", "inv-h": "#ffffff",
    "pal-light": "#323b49", "pal-dark": "#0b0e13", "pal-shadow": "#000000",
    "shade": "#000000",
    # home / settings / components
    "teams-sub": "rgba(139,92,246,0.18)", "teams-tx": "#c4b5fd",
    "guest-face-a": "#3a4352", "guest-face-b": "#2a313c", "guest-face-icon": "#c3cad5",
    "cap-b": "#1b212a", "cap-shadow": "rgba(0,0,0,0.45)",
    "frost-45": "rgba(255,255,255,0.025)", "frost-55": "rgba(255,255,255,0.03)",
    "frost-60": "rgba(255,255,255,0.04)", "frost-65": "rgba(255,255,255,0.035)",
    "frost-70": "rgba(255,255,255,0.04)", "frost-72": "rgba(255,255,255,0.04)", "frost-90": "rgba(255,255,255,0.08)",
    "rec": "#ff4245",
    "focus-glow": "rgba(96,165,250,0.18)",
    "mark-sub": "rgba(96,165,250,0.22)",
    "icon-blue": "#60a5fa", "icon-pro": "#c084fc",
    # meetings / audio
    "pink-sub": "rgba(236,72,153,0.16)", "pink-tx": "#f9a8d4",
    "glass-solid": "#1e232c",
    "row-hover": "rgba(255,255,255,0.04)", "row-press": "rgba(255,255,255,0.07)",
    "drop-fill": "#1e3a66",
    "rec-well-a": "rgba(0,0,0,0.35)", "rec-well-b": "rgba(0,0,0,0.18)",
    "rec-well-c": "rgba(255,255,255,0.06)",
    "rec-cap-a": "#2c333e", "rec-cap-b": "#1e242c", "rec-cap-da": "#20262e", "rec-cap-db": "#2a313b",
    "rec-rim": "rgba(255,255,255,0.10)",
    "hero-a": "#3b82f6", "hero-b": "#a855f7", "hero-ha": "#2563eb", "hero-hb": "#9333ea",
    # windows / dialogs
    "text3": "#a9b2bf",
    "warn-tx": "#f6c35e",
    "glass-btn": "rgba(255,255,255,0.06)",
    "chip-glass": "rgba(255,255,255,0.05)",
    "mic-ring": "rgba(255,255,255,0.08)",
    "cta": "#2563eb", "cta-h": "#3b74f0", "cta-line": "#3b74f0", "cta-hl": "#93c5fd", "cta-p": "#1d4ed8",
    "cta-off": "rgba(37,99,235,0.25)", "cta-off-tx": "rgba(255,255,255,0.706)",
    "popup-line": "#323b49", "sel-sub": "rgba(96,165,250,0.16)",
    "pro-track": "#1b212a", "pro-edge": "rgba(192,132,252,0.28)",
    "pro-btn": "#a855f7",
    "save-on": "#166534", "save-on-tx": "#bbf7d0",
    # live / pill / tray
    "tile-doc": "rgba(96,165,250,0.16)", "tile-img": "rgba(56,189,248,0.14)", "tile-img-tx": "#7dd3fc",
    "tile-alert-tx": "#f6c35e",
    "pro-deep": "#7e22ce", "dot-off": "#4b5567", "confetti-rim": "rgba(232,237,244,0.6)",
    "flag-red": "#d90012", "flag-blue": "#0033a0", "flag-orange": "#f2a800",
}

assert set(LIGHT) == set(DARK), set(LIGHT) ^ set(DARK)

# The ACTIVE set. It is the same dict object for the whole run (swapped in place), so a module
# that keeps ``theme.TOKENS`` sees the switch.
TOKENS = dict(LIGHT)

# Shadows (CSS "0 8px 24px rgba(15,23,42,0.06)" and friends) as
# (blur, y offset, colour) for QGraphicsDropShadowEffect. In the dark look they are black and deeper.
SHADOWS_LIGHT = {
    "card": (24, 8, "rgba(15,23,42,0.06)"),
    "lg": (48, 24, "rgba(15,23,42,0.12)"),
    "item": (6, 1, "rgba(15,23,42,0.10)"),      # the active sidebar item / selected segment
    "key": (18, 6, "rgba(15,23,42,0.10)"),      # big keycaps
}
SHADOWS_DARK = {
    "card": (24, 8, "rgba(0,0,0,0.28)"),
    "lg": (48, 24, "rgba(0,0,0,0.50)"),
    "item": (6, 1, "rgba(0,0,0,0.35)"),
    "key": (18, 6, "rgba(0,0,0,0.35)"),
}
SHADOWS = dict(SHADOWS_LIGHT)

_RGBA = re.compile(r"rgba\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)")


def _parse(value, tokens=None):
    """'#rrggbb' or 'rgba(r,g,b,a)' (a = 0..1) -> (r, g, b, alpha 0..255)."""
    value = (tokens or TOKENS).get(value, value)
    m = _RGBA.fullmatch(value.strip())
    if m:
        r, g, b, a = m.groups()
        return int(float(r)), int(float(g)), int(float(b)), int(round(float(a) * 255))
    c = QColor(value)
    return c.red(), c.green(), c.blue(), c.alpha()


def qcolor(value, scheme=None):
    """QColor for a token name ('blue') or any CSS colour - in the active look, or ``scheme``'s."""
    r, g, b, a = _parse(value, _set_of(scheme))
    return QColor(r, g, b, a)


def qss_color(value, scheme=None):
    """A token / CSS colour written the way Qt stylesheets read it (alpha 0-255)."""
    r, g, b, a = _parse(value, _set_of(scheme))
    if a >= 255:
        return "#%02x%02x%02x" % (r, g, b)
    return "rgba(%d, %d, %d, %d)" % (r, g, b, a)


def _set_of(scheme):
    if scheme is None:
        return None
    return DARK if scheme == "dark" else LIGHT


def is_token(value):
    return isinstance(value, str) and value in TOKENS


_TOKEN_REF = re.compile(r"@([a-z][a-z0-9]*(?:-[a-z0-9]+)*)")


def fill(template):
    """``template`` with every ``@token`` written as its colour in the active look
    ("QLabel { color: @muted; }" -> "QLabel { color: #8d97a8; }"). Unknown names stay as they are."""
    return _TOKEN_REF.sub(lambda m: qss_color(m.group(1)) if m.group(1) in TOKENS else m.group(0), template)


# ── Light and dark ───────────────────────────────────────────────────────────
# What follows the computer: Qt follows the OS colour scheme for everything a stylesheet does not
# paint (a window's background, a button's fill, a menu's panel, a placeholder, a disabled word come
# from the application palette, which Qt replaces with the OS's). So the app never leaves that to
# the OS: it uses Qt's "Fusion" style (paints only from the palette, so what the tests render is what
# runs) and sets an explicit palette from the ACTIVE token set, every role in every group, and
# build_qss() paints the pop-ups itself (menus, tooltips, message boxes) - text AND background from the
# same set. (A navy question on the OS's charcoal panel, about 1:1, was the 2.0 bug that started this.)
#
# Which set: ``resolve(app_theme, os_dark)`` - "light" / "dark" pin it, "system" follows the OS.
# A pin that differs from the OS also asks Qt for that scheme (QStyleHints.setColorScheme, Qt 6.8),
# which turns the window frames / title bars too; on Windows each themed window's title bar is also
# set directly (DWMWA_USE_IMMERSIVE_DARK_MODE) when it gets its native window. ``switch()`` re-paints
# everything already open: no restart.
#
# The pop-ups Gusan asks questions with are ui/components/mini_prompt.py cards (confirm, notify,
# choose, ask_text), never QMessageBox / QInputDialog (tests_qt/test_dialog_visibility.py
# renders them all in both looks and refuses the old classes).
PREFERENCES = ("system", "light", "dark")
_SCHEME = {"os_dark": None, "hooked": False, "scheme": "light", "pref": "system", "override": False}

# What a switch re-applies (weak: a closed window drops out by itself).
_WINDOWS = weakref.WeakSet()       # widgets carrying the whole stylesheet (apply / use_stylesheet)
_SHEETED = weakref.WeakSet()       # widgets with a sheet of their own (sheet()); the builder is on the widget
_HOOKED = weakref.WeakSet()        # owners of on_change hooks; the hooks are on the owner


def scheme():
    """The active look: "light" or "dark"."""
    return _SCHEME["scheme"]


def is_dark():
    return _SCHEME["scheme"] == "dark"


def preference():
    """The app_theme setting the look follows: "system" | "light" | "dark"."""
    return _SCHEME["pref"]


def normalize(pref):
    pref = str(pref or "system").strip().lower()
    return pref if pref in PREFERENCES else "system"


def resolve(pref, os_dark):
    """The look for an app_theme setting and the OS's scheme: "light" or "dark"."""
    pref = normalize(pref)
    if pref in ("light", "dark"):
        return pref
    return "dark" if os_dark else "light"


def palette(name=None):
    """The palette every Gusan widget gets from the application: a token set, in the active,
    inactive and disabled groups and for every role, so no role is left to the OS."""
    tok = DARK if (name or scheme()) == "dark" else LIGHT

    def c(key):
        return QColor(*_parse(key, tok))
    roles = {
        QPalette.WindowText: c("text"), QPalette.Button: c("surface"), QPalette.Light: c("pal-light"),
        QPalette.Midlight: c("bg2"), QPalette.Dark: c("pal-dark"), QPalette.Mid: c("line2"),
        QPalette.Text: c("text"), QPalette.BrightText: c("on-accent"), QPalette.ButtonText: c("text2"),
        QPalette.Base: c("input"), QPalette.Window: c("bg"), QPalette.Shadow: c("pal-shadow"),
        QPalette.Highlight: c("blue"), QPalette.HighlightedText: c("on-accent"),
        QPalette.Link: c("blue-tx"), QPalette.LinkVisited: c("pro-tx"), QPalette.AlternateBase: c("bg2"),
        QPalette.ToolTipBase: c("tip-bg"), QPalette.ToolTipText: c("tip-fg"),
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


def light_palette():
    """The light look's palette (whatever look is active)."""
    return palette("light")


def dark_palette():
    """The dark look's palette (whatever look is active)."""
    return palette("dark")


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


def _matches(pal, name):
    tok = DARK if name == "dark" else LIGHT
    try:
        return (pal.color(QPalette.Window) == QColor(*_parse("bg", tok))
                and pal.color(QPalette.Base) == QColor(*_parse("input", tok))
                and pal.color(QPalette.Text) == QColor(*_parse("text", tok)))
    except Exception:
        return False


def _is_light(pal):
    """True for the palette light_palette() builds."""
    return _matches(pal, "light")


def _is_ours(pal):
    """True for a palette of either look (one the app set, not one the OS handed over)."""
    return _matches(pal, "light") or _matches(pal, "dark")


def _registry_dark():
    """Windows' own answer (HKCU ...\\Themes\\Personalize AppsUseLightTheme = 0), or None."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return int(winreg.QueryValueEx(k, "AppsUseLightTheme")[0]) == 0
    except Exception:
        return None


def os_is_dark():
    """True when the OS is set to dark: what Qt reports (Windows 10/11 and macOS; Qt 6.5+) - while
    the app asks Qt for a scheme of its own, Windows' registry - else the palette the application had
    before the app replaced it (what the offscreen test platform has)."""
    if _SCHEME["override"] and QGuiApplication.platformName() == "windows":
        reg = _registry_dark()
        if reg is not None:
            return reg
    try:
        sch = QGuiApplication.styleHints().colorScheme()
        if sch == Qt.ColorScheme.Dark:
            return True
        if sch == Qt.ColorScheme.Light:
            return False
    except Exception:
        pass
    return bool(_SCHEME["os_dark"])


def menu_palette():
    """The palette for a menu that follows the OS (see os_is_dark)."""
    return dark_menu_palette() if os_is_dark() else light_palette()


def _request_os_scheme(pref):
    """Ask Qt for the pinned scheme (title bars, native dialogs), or hand it back to the OS."""
    try:
        hints = QGuiApplication.styleHints()
        if not hasattr(hints, "setColorScheme"):
            return
        if pref in ("light", "dark"):
            hints.setColorScheme(Qt.ColorScheme.Dark if pref == "dark" else Qt.ColorScheme.Light)
            _SCHEME["override"] = True
        elif _SCHEME["override"]:
            hints.setColorScheme(Qt.ColorScheme.Unknown)
            _SCHEME["override"] = False
    except Exception:
        logger.debug("could not ask Qt for a colour scheme", exc_info=True)


class ThemeManager(QObject):
    """The one object that says which look is active; ``changed(name)`` after every switch."""
    changed = Signal(str)

    def scheme(self):
        return scheme()

    def resolve(self, pref=None, os_dark=None):
        return resolve(preference() if pref is None else pref, os_is_dark() if os_dark is None else os_dark)

    def switch(self, name, force=False):
        return switch(name, force=force)

    def set_preference(self, pref):
        return set_preference(pref)


_MANAGER = None


def manager():
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ThemeManager()
    return _MANAGER


def install_scheme(app=None, pref=None):
    """Give the application its look (see "Light and dark" above): Fusion, the token set for the
    app_theme setting ``pref`` (None: the one already set) and the OS, its palette, and a hook that
    follows the OS when it changes. Call it once, right after the QApplication exists and before any
    window; safe to call again. True when applied. Costs nothing but choosing the set."""
    app = app or QApplication.instance()
    if app is None:
        return False
    if pref is not None:
        _SCHEME["pref"] = normalize(pref)
    cur = app.palette()
    if not _is_ours(cur):
        _SCHEME["os_dark"] = cur.color(QPalette.Window).lightness() < 128   # what the OS handed over
    try:
        if isinstance(app, QApplication) and (app.style() is None or app.style().objectName().lower() != "fusion"):
            app.setStyle("Fusion")
    except Exception:
        logger.debug("could not set the Fusion style", exc_info=True)
    _request_os_scheme(_SCHEME["pref"])
    switch(resolve(_SCHEME["pref"], os_is_dark()), force=True)
    try:
        QApplication.setFont(font(13.5), "QMenu")      # a plain menu (Export, a field's right-click) reads in Gusan's face
    except Exception:
        logger.debug("could not set the menu font", exc_info=True)
    if not _SCHEME["hooked"]:
        try:
            QGuiApplication.styleHints().colorSchemeChanged.connect(lambda *_a: QTimer.singleShot(0, follow_os))
            _SCHEME["hooked"] = True
        except Exception:
            logger.debug("no colorSchemeChanged signal", exc_info=True)
    return True


def follow_os():
    """The OS changed its scheme (or pushed its palette): follow it when app_theme is "system", and
    keep the app's own palette either way."""
    app = QApplication.instance()
    if app is None:
        return
    try:
        pal = app.palette()
        if not _is_ours(pal):
            _SCHEME["os_dark"] = pal.color(QPalette.Window).lightness() < 128
        target = resolve(_SCHEME["pref"], os_is_dark())
        if target != _SCHEME["scheme"]:
            switch(target)
        elif not _matches(app.palette(), target):
            app.setPalette(palette(target))
    except Exception:
        logger.debug("could not follow the OS scheme", exc_info=True)


def set_preference(pref):
    """Settings > General > Appearance changed (app_theme): apply it now. Returns the look."""
    pref = normalize(pref)
    _SCHEME["pref"] = pref
    _request_os_scheme(pref)
    switch(resolve(pref, os_is_dark()))
    return _SCHEME["scheme"]


def switch(name, force=False):
    """Make ``name`` ("light" / "dark") the active look and re-paint everything already open:
    the palette, every themed window's stylesheet, every widget's own sheet, icons drawn in a token
    colour, shadows, title bars and the on_change hooks; then ``manager().changed``. True when it
    switched."""
    global QSS
    name = "dark" if name == "dark" else "light"
    if name == _SCHEME["scheme"]:
        if force:                                  # the same look: only the palette may have been replaced
            app = QApplication.instance()
            if app is not None and not _matches(app.palette(), name):
                app.setPalette(palette(name))
        return False
    old_qss = QSS
    _SCHEME["scheme"] = name
    TOKENS.clear()
    TOKENS.update(DARK if name == "dark" else LIGHT)
    SHADOWS.clear()
    SHADOWS.update(SHADOWS_DARK if name == "dark" else SHADOWS_LIGHT)
    QSS = None
    app = QApplication.instance()
    if app is not None:
        try:
            app.setPalette(palette(name))
        except Exception:
            logger.debug("could not set the palette", exc_info=True)
    if isinstance(app, QApplication) and app.allWidgets():      # (at start-up nothing is open yet: no work)
        started = time.perf_counter()
        _repaint_all(app, old_qss)
        # One line per live switch, with its cost: a slow switch on a busy machine can be told apart
        # from any other pause of the window (the stall watchdog only sees the Qt event loop).
        logger.info("Look switched to %s: %d windows re-styled in %.2f s", name, len(_WINDOWS),
                    time.perf_counter() - started)
    try:
        manager().changed.emit(name)
    except Exception:
        logger.debug("theme changed signal failed", exc_info=True)
    return True


def _repaint_all(app, old_qss):
    new_qss = None
    # widgets with a sheet of their own first (each re-polishes its subtree), then the windows
    for w in list(_SHEETED):
        _set_own_sheet(w)
    for w in list(_WINDOWS):
        try:
            if new_qss is None:
                new_qss = stylesheet()
            w.setStyleSheet(new_qss)
        except RuntimeError:
            pass
    if old_qss:
        for w in app.topLevelWidgets():
            try:
                if w not in _WINDOWS and w.styleSheet() == old_qss:
                    w.setStyleSheet(new_qss or stylesheet())
            except RuntimeError:
                pass
    widgets = app.allWidgets()
    try:
        from ui.components import icons
        icons.retint(widgets)
    except Exception:
        logger.debug("re-tinting the icons failed", exc_info=True)
    for w in widgets:
        try:
            eff = w.graphicsEffect()
            if eff is not None and eff.property("gusanShadow"):
                _shade(eff, str(eff.property("gusanShadow")))
        except RuntimeError:
            pass
    for owner in list(_HOOKED):
        for fn in list(getattr(owner, "_gusan_theme_hooks", ()) or ()):
            try:
                fn()
            except RuntimeError:
                pass                                   # deleted underneath
            except Exception:
                logger.warning("a theme hook failed", exc_info=True)
    for w in app.topLevelWidgets():
        if w.isWindow():
            title_bar(w)
    for w in widgets:
        try:
            QWidget.update(w)                  # (a list view's own update() wants an index)
        except RuntimeError:
            pass


# ── Per-widget sheets and hooks ──────────────────────────────────────────────
def sheet(widget, builder):
    """Give ``widget`` a stylesheet of its own that follows the look: ``builder`` is a template with
    ``@token`` colours ("QLabel { color: @muted; }") or a function returning the sheet. Set now and
    again after every switch. ``builder`` None takes the widget's own sheet away (back to the window's).
    Returns the widget."""
    if builder is None:
        widget._gusan_sheet = None
        _SHEETED.discard(widget)
        widget.setStyleSheet("")
        return widget
    widget._gusan_sheet = builder
    _SHEETED.add(widget)
    _set_own_sheet(widget)
    return widget


def _set_own_sheet(widget):
    try:
        b = getattr(widget, "_gusan_sheet", None)
        if b is None:
            return
        widget.setStyleSheet(b() if callable(b) else fill(b))
    except RuntimeError:
        pass


def on_change(owner, fn):
    """Call ``fn()`` after every switch while ``owner`` (a QObject) lives - for a colour baked into
    HTML, a cached picture or a palette. ``fn`` may refer to ``owner``: it is kept on the owner."""
    hooks = getattr(owner, "_gusan_theme_hooks", None)
    if hooks is None:
        hooks = []
        owner._gusan_theme_hooks = hooks
    hooks.append(fn)
    _HOOKED.add(owner)
    return fn


def use_stylesheet(widget):
    """The whole stylesheet on ``widget`` (a dialog, a card, a frameless window), kept current."""
    widget.setStyleSheet(stylesheet())
    _WINDOWS.add(widget)
    _watch_window(widget)
    return widget


# ── Title bars (Windows: the frame's dark mode per window) ───────────────────
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20


def title_bar(widget):
    """Turn a window's title bar dark or light with the look (Windows 10 1809+; elsewhere Qt and
    the OS do it). Only for a window that already has its native window - nothing is created."""
    if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
        return False
    try:
        if not widget.isWindow() or widget.windowHandle() is None:
            return False
        if widget.windowFlags() & Qt.FramelessWindowHint:
            return False
        import ctypes
        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if is_dark() else 0)
        res = ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), _DWMWA_USE_IMMERSIVE_DARK_MODE,
                                                         ctypes.byref(value), ctypes.sizeof(value))
        return res == 0
    except Exception:
        logger.debug("could not set the title bar", exc_info=True)
        return False


class _WindowWatch(QObject):
    """On one themed window (never the application): its title bar follows the look once the native
    window exists (it does not when theme.apply runs, in the constructor)."""

    def eventFilter(self, obj, ev):
        if ev.type() in (QEvent.WinIdChange, QEvent.Show):
            title_bar(obj)
        return False


_WATCH = None


def _watch_window(widget):
    global _WATCH
    if sys.platform != "win32":
        return
    try:
        if not widget.isWindow() or widget.property("_gusanTitleWatch"):
            return
        if _WATCH is None:
            _WATCH = _WindowWatch()
        widget.setProperty("_gusanTitleWatch", True)
        widget.installEventFilter(_WATCH)
    except Exception:
        logger.debug("could not watch the window", exc_info=True)


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


def _shade(eff, level):
    blur, dy, colour = SHADOWS[level]
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(qcolor(colour))


def elevate(widget, level="card"):
    """A soft drop shadow (CSS box-shadow). level: card | lg | item | key | None.
    It follows the look (black and deeper in the dark one)."""
    if not level:
        widget.setGraphicsEffect(None)
        return None
    eff = QGraphicsDropShadowEffect(widget)
    eff.setProperty("gusanShadow", level)
    _shade(eff, level)
    widget.setGraphicsEffect(eff)
    return eff


# ── Stylesheet ───────────────────────────────────────────────────────────────
def _asset_url(name):
    return resource_path("assets", name).replace("\\", "/")


def build_qss():
    t = {k: qss_color(k) for k in TOKENS}
    dark = is_dark()
    glass = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %s, stop:1 %s)"
             % (t["glass-a"], t["glass-b"]))
    window = ("qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 %s, stop:0.5 %s, stop:1 %s)"
              % (t["window-a"], t["window-b"], t["window-c"]))
    plan = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %s, stop:1 %s)"
            % (t["plan-a"], t["plan-b"]))
    kbd = (t["kbd-a"] if t["kbd-a"] == t["kbd-b"] else
           "qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 %s, stop:1 %s)" % (t["kbd-a"], t["kbd-b"]))
    # The brand's purple: the same in both looks.
    pro_pill = ("qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 %s, stop:1 %s)"
                % (qss_color("rgba(168,85,247,0.92)"), qss_color("rgba(126,34,206,0.92)")))
    pro_btn = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #a855f7, stop:0.55 #9333ea, stop:1 #7e22ce)"
    pro_btn_h = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #9333ea, stop:0.55 #7e22ce, stop:1 #6b21a8)"
    chev = _asset_url("chevron_dark.svg" if dark else "chevron.svg")
    check = _asset_url("check.svg")
    check_ink = _asset_url("check_ink_dark.svg" if dark else "check_ink.svg")
    c = qss_color
    return f"""
/* ── window ── */
QMainWindow[gusan="window"], QDialog[gusan="window"], QWidget[gusan="root"] {{
    background: {window};
}}
QWidget {{ color: {t['text']}; }}
QLabel {{ background: transparent; }}
QToolTip {{
    background: {t['tip-bg']}; color: {t['tip-fg']}; border: none; border-radius: 6px; padding: 4px 8px;
}}

/* ── pop-ups: a menu, a text field's right-click menu and a stray message box are children of
      the window that has this sheet, so they get a background as well as a text colour (the
      text colour alone is what once left navy words on the OS's dark panel; see theme.py) ── */
QMenu {{
    background: {t['popup']}; color: {t['text']}; border: 1px solid {t['line2']}; padding: 4px;
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
    background: {t['surface']}; color: {t['text2']}; border: 1px solid {t['line2']}; border-radius: 8px;
    padding: 6px 16px; min-width: 72px; min-height: 22px;
}}
QMessageBox QPushButton:hover, QInputDialog QPushButton:hover, QDialogButtonBox QPushButton:hover {{
    background: {t['bg']}; border-color: {t['blue-l']}; color: {t['text']};
}}
QMessageBox QPushButton:default, QInputDialog QPushButton:default, QDialogButtonBox QPushButton:default {{
    background: {t['blue']}; color: {t['on-accent']}; border-color: {t['blue-h']};
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
    background: {t['surface']}; border: 1px solid {t['line']}; border-radius: 12px;
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

QPushButton[gusan="btn"][kind="primary"] {{ background: {t['blue']}; color: {t['on-accent']}; border: 1px solid {t['blue-h']}; }}
QPushButton[gusan="btn"][kind="primary"]:hover {{ background: {t['blue-h']}; }}
QPushButton[gusan="btn"][kind="primary"]:pressed {{ background: {t['blue-p']}; }}
QPushButton[gusan="btn"][kind="primary"][focusVisible="true"] {{ border: 2px solid {t['focus']}; }}

QPushButton[gusan="btn"][kind="secondary"] {{ background: {t['surface']}; color: {t['text2']}; border: 1px solid {t['line2']}; }}
QPushButton[gusan="btn"][kind="secondary"]:hover {{ background: {t['bg']}; border-color: {t['blue-l']}; color: {t['text']}; }}
QPushButton[gusan="btn"][kind="secondary"]:pressed {{ background: {t['bg2']}; }}

QPushButton[gusan="btn"][kind="ghost"] {{ background: transparent; color: {t['text2']}; border: 1px solid transparent; }}
QPushButton[gusan="btn"][kind="ghost"]:hover {{ background: {t['hover']}; color: {t['text']}; }}
QPushButton[gusan="btn"][kind="ghost"]:pressed {{ background: {t['press']}; }}

QPushButton[gusan="btn"][kind="link"] {{ background: transparent; color: {t['blue-tx']}; border: 1px solid transparent; padding: 0px 2px; }}
QPushButton[gusan="btn"][kind="link"]:hover {{ color: {t['blue']}; }}
QPushButton[gusan="btn"][kind="link"][room="true"] {{ padding: 0px 6px; }}   /* the keyboard ring has air around the word */

QPushButton[gusan="btn"][kind="pro"] {{ background: {pro_btn}; color: {t['on-accent']}; border: 1px solid {t['pro']}; }}
QPushButton[gusan="btn"][kind="pro"]:hover {{ background: {pro_btn_h}; }}
QPushButton[gusan="btn"][kind="pro"][focusVisible="true"] {{ border: 2px solid {t['pro-focus']}; }}

QPushButton[gusan="btn"][kind="danger"] {{ background: {t['danger']}; color: {t['on-accent']}; border: 1px solid {t['danger-h']}; }}
QPushButton[gusan="btn"][kind="danger"]:hover {{ background: {t['danger-h']}; }}

QPushButton[gusan="btn"][kind="dark"] {{ background: {t['inv-bg']}; color: {t['inv-fg']}; border: 1px solid {t['inv-bg']}; }}
QPushButton[gusan="btn"][kind="dark"]:hover {{ background: {t['inv-h']}; }}

QPushButton[gusan="btn"][focusVisible="true"] {{ border: 2px solid {t['blue-l']}; }}
QPushButton[gusan="btn"]:disabled {{ background: {t['bg2']}; color: {t['faint']}; border: 1px solid {t['line']}; }}

QPushButton[gusan="upgradeQuiet"] {{
    background: {t['pro-sub']}; border: 1px solid {t['pro-line']}; color: {t['pro-tx']};
    border-radius: 8px; padding: 0px 10px;
}}
QPushButton[gusan="upgradeQuiet"]:hover {{ background: {t['pro-sub-h']}; }}
QPushButton[gusan="upgradeQuiet"][focusVisible="true"] {{ border: 2px solid {t['pro-l']}; }}

QToolButton[gusan="iconBtn"], QPushButton[gusan="iconBtn"] {{
    background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 0px;
}}
QToolButton[gusan="iconBtn"]:hover, QPushButton[gusan="iconBtn"]:hover {{ background: {t['hover2']}; }}
QToolButton[gusan="iconBtn"]:pressed, QPushButton[gusan="iconBtn"]:pressed {{ background: {t['press2']}; }}
QToolButton[gusan="iconBtn"][focusVisible="true"], QPushButton[gusan="iconBtn"][focusVisible="true"] {{ border: 2px solid {t['blue-l']}; }}
QToolButton[gusan="iconBtn"][framed="true"], QPushButton[gusan="iconBtn"][framed="true"] {{
    background: {t['surface']}; border: 1px solid {t['line2']};
}}
QToolButton[gusan="iconBtn"][framed="true"][focusVisible="true"], QPushButton[gusan="iconBtn"][framed="true"][focusVisible="true"] {{
    border: 2px solid {t['blue-l']};
}}
QToolButton[gusan="playBtn"] {{ background: {t['blue']}; border: 1px solid {t['blue-h']}; border-radius: 20px; }}
QToolButton[gusan="playBtn"]:hover {{ background: {t['blue-h']}; }}
QToolButton[gusan="playBtn"]:pressed, QToolButton[gusan="playBtn"][loading="true"] {{ background: {t['blue-p']}; }}
QToolButton[gusan="playBtn"][focusVisible="true"] {{ border: 2px solid {t['focus']}; }}

/* ── delete from a list (ui/components/row_actions.py) ── */
QToolButton[gusan="trashBtn"] {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 0px; }}
QToolButton[gusan="trashBtn"]:hover {{ background: {t['red-sub']}; border: 1px solid {t['red-line']}; }}
QToolButton[gusan="trashBtn"]:pressed {{ background: {t['red-press']}; }}
QToolButton[gusan="trashBtn"]:disabled {{ background: transparent; border: 1px solid transparent; }}
QFrame[gusan="actionBar"] {{ background: {t['blue-sub']}; border: 1px solid {t['blue-line']}; border-radius: 12px; }}
QFrame[gusan="actionBar"][tone="error"] {{ background: {t['red-sub']}; border: 1px solid {t['red-line']}; }}
QFrame[gusan="actionBar"][docked="true"] {{ background: {t['surface']}; border: 1px solid {t['blue-line']}; }}
QFrame[gusan="actionBar"][docked="true"][tone="error"] {{ background: {t['red-sub']}; border: 1px solid {t['red-line']}; }}

/* ── pills, chips, keycaps ── */
QLabel[gusan="pill"] {{ border-radius: 9px; padding: 0px 10px; }}
QLabel[gusan="pill"][gsize="xs"] {{ padding: 0px 6px; }}
QLabel[gusan="pill"][gsize="s"] {{ padding: 0px 8px; }}
QLabel[gusan="pill"][tone="pro"] {{ background: {pro_pill}; color: {t['on-accent']}; border: 1px solid {c('rgba(255,255,255,0.59)')}; }}
QLabel[gusan="pill"][tone="free"] {{ background: {t['free-sub']}; color: {t['ok-tx']}; border: 1px solid {t['free-line']}; }}
QLabel[gusan="pill"][tone="guest"] {{ background: {t['guest-sub']}; color: {t['guest-tx']}; border: 1px solid {t['guest-line']}; }}
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
QFrame[gusan="chip"][tone="red"] {{ background: {t['red-sub']}; border: 1px solid {t['red-line']}; }}
QFrame[gusan="chip"][tone="red"] QLabel {{ color: {t['red-tx']}; }}
QFrame[gusan="chip"][tone="amber"] {{ background: {t['amber-sub']}; border: 1px solid {t['amber-line']}; }}
QFrame[gusan="chip"][tone="amber"] QLabel {{ color: {t['amber-tx']}; }}

QLabel[gusan="kbd"] {{
    background: {kbd}; color: {t['ink']}; border: 1px solid {t['line2']}; border-bottom: 2px solid {t['line2']};
    border-radius: 6px; padding: 0px 7px;
}}
QLabel[gusan="kbd"][big="true"] {{ border-bottom: 4px solid {t['line2']}; border-radius: 12px; padding: 0px 16px; }}
QLabel[gusan="kbdPlus"] {{ color: {t['faint']}; }}

/* ── segmented ── */
QFrame[gusan="segmented"] {{ background: {t['seg-track']}; border: none; border-radius: 17px; }}
QFrame[gusan="segmented"][gsize="s"] {{ border-radius: 15px; }}
QPushButton[gusan="seg"] {{
    background: transparent; color: {t['muted']}; border: none; border-radius: 14px; padding: 0px 14px;
}}
QFrame[gusan="segmented"][gsize="s"] QPushButton[gusan="seg"] {{ border-radius: 12px; }}
QPushButton[gusan="seg"]:hover {{ color: {t['text2']}; }}
QPushButton[gusan="seg"]:checked {{ background: {t['seg-on']}; color: {t['blue-tx']}; }}
QPushButton[gusan="seg"][focusVisible="true"] {{ border: 2px solid {t['blue-l']}; }}

/* ── inputs ── */
QLineEdit, QPlainTextEdit, QTextEdit {{
    background: {t['input']}; color: {t['text']}; border: 1px solid {t['line2']}; border-radius: 8px;
    padding: 6px 12px; selection-background-color: {t['blue']}; selection-color: {t['on-accent']};
    placeholder-text-color: {t['muted']};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{ border: 1px solid {t['blue-l']}; }}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
    background: {t['bg2']}; color: {t['faint']}; border-color: {t['line']};
}}
QComboBox {{
    background: {t['input']}; color: {t['text']}; border: 1px solid {t['line2']}; border-radius: 8px;
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
/* The list drops down UNDER the box (Fusion's default draws it over the box, hiding it). The list WINDOW is the
   card (ui/popup_corners.py): white with one thin border; on Windows 11 the system rounds its corners and draws
   the border. The list inside has no border or rounding of its own: a rounded card inside the square window
   showed square corners and a square shadow around it. (The window's flags are left alone: changing them makes
   the list stop selecting.) */
QComboBox {{ combobox-popup: 0; }}
QComboBoxPrivateContainer {{ background: {t['popup']}; border: 1px solid {t['line2']}; }}
QComboBox QAbstractItemView {{
    background: {t['popup']}; border: none; border-radius: 0px; outline: 0px; padding: 4px;
    color: {t['text']}; selection-background-color: {t['blue-sub']}; selection-color: {t['blue-tx']};
}}
QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 4px 10px; border-radius: 6px; }}   /* 34 px: the model pickers' rows */
QComboBox QAbstractItemView::item:hover {{ background: {t['bg2']}; color: {t['ink']}; }}
QComboBox QAbstractItemView::item:selected {{ background: {t['blue-sub']}; color: {t['blue-tx']}; }}
QCheckBox, QRadioButton {{ spacing: 10px; color: {t['text']}; background: transparent; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border: 1.5px solid {t['line2']}; border-radius: 5px; background: {t['input']};
}}
QCheckBox::indicator:hover {{ border-color: {t['blue-l']}; }}
QCheckBox::indicator:checked {{ border-color: {t['blue']}; background: {t['blue']}; image: url("{check}"); }}
QCheckBox::indicator:disabled {{ background: {t['bg2']}; border-color: {t['line']}; }}
QProgressBar {{ background: {t['track']}; border: none; border-radius: 3px; max-height: 6px; text-align: center; color: transparent; }}
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
    background: {t['rail-foot']}; border: none; border-top: 1px solid {t['line']};
    border-radius: 0px; border-bottom-left-radius: 15px; border-bottom-right-radius: 15px;
}}
QFrame[gusan="row"] {{ background: transparent; border: none; border-bottom: 1px solid {t['line']}; border-radius: 0px; }}
QFrame[gusan="row"][last="true"] {{ border-bottom: none; }}

/* ── sidebar ── */
QFrame[gusan="sidebar"] {{
    background: {t['sidebar']}; border: none; border-right: 1px solid {t['glass-line']}; border-radius: 0px;
}}
QPushButton[gusan="nav"] {{ background: transparent; border: none; border-radius: 10px; text-align: left; padding: 0px; }}
QPushButton[gusan="nav"]:hover {{ background: {t['nav-hover']}; }}
QPushButton[gusan="nav"][focusVisible="true"] {{ border: 2px solid {t['blue-l']}; }}
QPushButton[gusan="nav"][active="true"] {{ background: {t['nav-active']}; }}
QPushButton[gusan="nav"] QLabel {{ color: {t['text2']}; }}
QPushButton[gusan="nav"][active="true"] QLabel[navText="true"] {{ color: {t['blue-tx']}; }}
QFrame[gusan="planCard"] {{
    background: {t['plan-card']}; border: 1px solid {t['glass-line']}; border-radius: 12px;
}}
QPushButton[gusan="accountRow"] {{ background: transparent; border: none; border-radius: 10px; padding: 0px; text-align: left; }}
QPushButton[gusan="accountRow"]:hover {{ background: {t['nav-hover']}; }}
QPushButton[gusan="accountRow"][focusVisible="true"] {{ border: 2px solid {t['blue-l']}; }}
QPushButton[gusan="accountRow"][active="true"] {{ background: {t['nav-active']}; }}
QPushButton[gusan="accountRow"] QLabel[navText="true"] {{ color: {t['ink']}; }}
"""


QSS = None


def stylesheet():
    """The stylesheet of the active look (built once per look; needs a QGuiApplication for colours)."""
    global QSS
    if QSS is None:
        QSS = build_qss()
    return QSS


def apply(window):
    """Style a top-level window: stylesheet, body font, window gradient; it follows the look."""
    load_fonts()
    window.setProperty("gusan", "window")
    window.setFont(font(14))
    use_stylesheet(window)
    return window
