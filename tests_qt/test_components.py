"""Gusan 2.0 building blocks: theme, icons and components (offscreen)."""
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontInfo
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QWidget


def test_tokens_and_colours(qapp):
    from ui import theme
    assert theme.TOKENS["blue"] == "#2563eb"
    assert theme.TOKENS["pro-l"] == "#a855f7"
    assert theme.qss_color("blue-sub") == "rgba(59, 130, 246, 20)"
    assert theme.qss_color("#ffffff") == "#ffffff"
    c = theme.qcolor("glass-line")
    assert (c.red(), c.green(), c.blue(), c.alpha()) == (203, 213, 225, 140)


def test_stylesheet_and_apply(qapp):
    from ui import theme
    qss = theme.stylesheet()
    for needle in ("#eaf0f8", "#f4f7fb", "#e7eef7", 'gusan="card"', "border-radius: 16px",
                   'kind="primary"', 'kind="pro"', 'kind="danger"', 'textRole="title"',
                   'gusan="sidebar"', "QScrollBar"):
        assert needle in qss, needle
    w = QWidget()
    theme.apply(w)
    assert w.styleSheet() == qss
    assert w.property("gusan") == "window"


def test_fonts(qapp):
    from ui import theme
    picks = theme.load_fonts()
    assert picks["display"] and picks["body"]
    if sys.platform == "win32":
        assert picks["body"] == "Segoe UI"
        assert picks["display"] in ("Outfit", "Segoe UI Variable", "Segoe UI")
    f = theme.font(13.5, 600)
    assert abs(QFontInfo(f).pixelSize() - 13.5) <= 1
    t = theme.font(28, 700, display=True)
    assert QFontInfo(t).pixelSize() == 28


def test_every_icon_renders(qapp):
    from ui.components import icons
    for name in icons.names():
        pm = icons.pixmap(name, 18, "#1d4ed8")
        assert not pm.isNull(), name
        img = pm.toImage()
        assert any(img.pixelColor(x, y).alpha() > 0
                   for x in range(img.width()) for y in range(img.height())), name
    for must in ("incognito", "wand", "wave", "headset", "home", "mic", "people", "book", "gear"):
        assert must in icons.IC
    assert icons.pixmap("mic", 18, "blue") is icons.pixmap("mic", 18, "blue")    # cached
    assert not icons.logo_pixmap(30).isNull()
    assert not icons.icon("gear", 19, "text2", active_color="blue-tx").isNull()
    with pytest.raises(KeyError):
        icons.pixmap("nope")


def test_pills(qapp):
    from ui.components import FreeBadge, GuestBadge, ProTag, TierBadge
    assert ProTag().text() == "PRO" and ProTag().property("tone") == "pro"
    assert FreeBadge(text="4/5 free").text() == "4/5 FREE"
    assert GuestBadge().property("tone") == "guest"
    assert isinstance(TierBadge("pro"), ProTag)
    assert isinstance(TierBadge("free"), FreeBadge)
    assert isinstance(TierBadge("guest"), GuestBadge)
    assert ProTag("xs").property("gsize") == "xs"


def test_toggle_is_a_keyboard_switch(qapp):
    from ui.components import Toggle
    with pytest.raises(ValueError):
        Toggle("")
    t = Toggle("Remove filler words", checked=False)
    assert t.accessibleName() == "Remove filler words"
    assert t.isCheckable() and not t.isChecked()
    assert (t.width(), t.height()) == (44, 26)
    seen = []
    t.toggled.connect(seen.append)
    QTest.keyClick(t, Qt.Key_Space)
    assert t.isChecked()
    QTest.keyClick(t, Qt.Key_Return)
    assert not t.isChecked()
    assert seen == [True, False]
    t.grab()                                    # paints without errors


def test_icon_button_needs_a_name(qapp):
    from ui.components import IconButton
    with pytest.raises(ValueError):
        IconButton("copy", "")
    b = IconButton("copy", "Copy")
    assert b.accessibleName() == "Copy" and b.toolTip() == "Copy"


def test_buttons(qapp):
    from ui.components import Button, ProTag
    for kind, h in (("primary", 36), ("secondary", 36), ("ghost", 36), ("pro", 36), ("danger", 36)):
        b = Button("Go", kind)
        assert b.height() == h and b.property("kind") == kind
    assert Button("Big", "primary", "l").height() == 44
    with pytest.raises(ValueError):
        Button("x", "neon")
    rec = Button("Record", "pro", trail=ProTag("xs"))
    assert rec.sizeHint().width() > Button("Record", "pro").sizeHint().width()


def test_keycaps(qapp):
    from ui.components import Keycaps
    from ui.components.controls import keycap_labels
    if sys.platform != "darwin":
        assert keycap_labels("alt+r") == ["Alt", "R"]
        assert keycap_labels("ctrl+alt+a") == ["Ctrl", "Alt", "A"]
    k = Keycaps.from_hotkey("alt+r", big=True)
    assert k.accessibleName() in ("Alt + R", "⌥ + R")
    k.set_hotkey("ctrl+shift+d")
    QApplication.processEvents()
    assert "D" in k.accessibleName()


def test_segmented(qapp):
    from ui.components import Segmented
    s = Segmented(["Notes", "Transcript"], 0, "View")
    got = []
    s.changed.connect(got.append)
    s.buttons()[1].click()
    assert s.index() == 1 and got == [1]
    assert s.accessibleName() == "View"


def test_rail_and_rows(qapp):
    from ui.components import SettingRow, SettingsRail, Toggle
    rail = SettingsRail("Dictation settings")
    a = rail.add_row(SettingRow("Shortcut", Toggle("Shortcut")))
    b = rail.add_row(SettingRow("Speech model", QComboBox(), model="Balanced · Whisper Small", pro=True))
    assert rail.width() == 316
    assert a.property("last") is False and b.property("last") is True
    assert b.pro_tag is not None and b.model_label.text() == "Balanced · Whisper Small"
    assert b.control.accessibleName() == "Speech model"     # unnamed control takes the label
    c = SettingRow("Language", QComboBox(), stacked=True, hint="Detect automatically")
    assert c.hint_label.text() == "Detect automatically"


def test_page_header(qapp):
    from ui.components import Button, PageHeader
    h = PageHeader("Meetings", "Record a call. Get the notes.", actions=[Button("New", "primary")])
    assert h.title_label.text() == "Meetings"
    assert h.title_label.property("textRole") == "title"
    assert h.sub_label.text() == "Record a call. Get the notes."
    h.set_sub(None)
    assert not h.sub_label.isVisibleTo(h)
    assert QFontInfo(h.title_label.font()).pixelSize() == 28


def test_cards_and_labels(qapp):
    from ui.components import Card, Chip, Divider, PlanCard, IconTile, WhiteCard, label
    c = Card()
    c.add(label("Hello", "h2"))
    c.add(Divider())
    assert c.property("gusan") == "card" and c.graphicsEffect() is not None
    assert Card(shadow=False).graphicsEffect() is None
    assert WhiteCard().property("gusan") == "whiteCard"
    assert PlanCard().property("gusan") == "tierCard"
    assert IconTile("mic").width() == 40
    ch = Chip("Hidden", "ok", "incognito")
    assert ch.text() == "Hidden" and ch.accessibleName() == "Hidden" and ch.height() == 24
    ey = label("Gusan 2.0", "eyebrow")
    assert ey.text() == "GUSAN 2.0"


def test_glass_orb(qapp):
    """Audio to text's liquid-glass circles: an emoji in a glossy orb, drawn
    once and cached, a little brighter while files are dragged over."""
    from ui.components.glass_orb import BOTTOM, SIDE, TOP, GlassOrb
    orb = GlassOrb("\U0001F3A7", 54, diameter=104, name="Audio")
    assert orb.text() == "\U0001F3A7" and orb.accessibleName() == "Audio"
    assert (orb.width(), orb.height()) == (104 + 2 * SIDE, 104 + TOP + BOTTOM)
    img = orb.grab().toImage()
    mid = img.pixelColor(img.width() // 2, TOP + 6)                 # the gloss at the top: near white
    assert mid.alpha() > 200 and mid.red() > 225
    edge = img.pixelColor(img.width() // 2, TOP + 102)              # the glass bends to blue at the bottom
    assert edge.blue() > edge.red()
    assert img.pixelColor(1, 1).alpha() == 0                         # outside the orb: see-through
    assert len(orb._cache) == 1
    orb.set_active(True)
    lit = orb.grab().toImage()
    assert lit.pixelColor(img.width() // 2, TOP + 102) != edge       # the drag glow changes the rim
