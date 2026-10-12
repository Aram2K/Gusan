"""Gusan 2.0 building blocks (the mockups' gusan_ds library, as Qt widgets).

Import what you need:

    from ui.components import Card, PageHeader, Button, Toggle, SettingsRail, SettingRow
    from ui.components import icons

Every widget is styled by ui/theme.py's stylesheet through dynamic
properties, so it looks right inside any window that ran ``theme.apply()``
(the main window does). Fonts are set in code (see ui/theme.py).
"""

from ui.components import icons
from ui.components import clipboard
from ui.components.badges import Chip, FreeBadge, GuestBadge, ProTag, TierBadge
from ui.components.buttons import Button, IconButton
from ui.components.controls import Keycap, Keycaps, Segmented, Toggle
from ui.components.surfaces import Card, Divider, PlanCard, IconTile, WhiteCard, label
from ui.components.page import PageHeader, SettingRow, SettingsRail
from ui.components.sidebar import Avatar, NavItem, Sidebar
from ui.components.hotkey_capture import HotkeyCaptureButton
from ui.components.audio_player import AudioPlayer

__all__ = [
    "icons", "clipboard",
    "Chip", "FreeBadge", "GuestBadge", "ProTag", "TierBadge",
    "Button", "IconButton",
    "Keycap", "Keycaps", "Segmented", "Toggle",
    "Card", "Divider", "PlanCard", "IconTile", "WhiteCard", "label",
    "PageHeader", "SettingRow", "SettingsRail",
    "Avatar", "NavItem", "Sidebar",
    "HotkeyCaptureButton",
    "AudioPlayer",
]
