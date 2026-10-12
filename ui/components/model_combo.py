"""PlainNameCombo: a model picker for a settings rail.

Closed it shows the plain name ("Balanced"); its list shows the whole line
("Balanced · Whisper Small"). Used by Audio to text's "Accuracy" and Smart
Actions' "Transcribes with".

    box = combo(PlainNameCombo(parent), "Accuracy", width=150)
    box.add_choice("Balanced · Whisper Small", "small", plain="Balanced")
    box.set_choice_enabled(box.findData("cloud"), False)     # e.g. Privacy mode
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox, QStylePainter

from ui import theme

PLAIN_ROLE = Qt.UserRole + 1
# Where QStandardItem keeps an item's flags. Rows are greyed out through the
# model index, never model.item(): PySide keeps that QStandardItem wrapper
# alive inside the model, so once clear() frees the item (a refill) the stale
# wrapper is handed out for whatever Qt allocates at its address next - an
# event in the Live Assistant's app-wide filter, and the app crashes.
_FLAGS_ROLE = Qt.UserRole - 1
_PICKABLE = Qt.ItemIsEnabled | Qt.ItemIsSelectable


class PlainNameCombo(QComboBox):
    """Closed it shows the plain name ('Balanced'); the list shows 'Balanced · Whisper Small'."""

    def paintEvent(self, _e):
        p = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        plain = self.currentData(PLAIN_ROLE)
        if plain:
            opt.currentText = plain
        p.drawComplexControl(QStyle.CC_ComboBox, opt)
        p.drawControl(QStyle.CE_ComboBoxLabel, opt)

    def add_choice(self, text, data, plain=None):
        """One item: ``text`` in the list, ``plain`` (or ``text``) when closed."""
        self.addItem(text, data)
        i = self.count() - 1
        if plain:
            self.setItemData(i, plain, PLAIN_ROLE)
        return i

    def plain_text(self):
        """What the closed box shows."""
        return self.currentData(PLAIN_ROLE) or self.currentText()

    def set_choice_enabled(self, index, enabled):
        """Grey an item out in the list (it can't be picked)."""
        model = self.model()
        idx = model.index(index, self.modelColumn())
        if not idx.isValid():
            return
        flags = model.flags(idx)
        flags = (flags | _PICKABLE) if enabled else (flags & ~_PICKABLE)
        model.setData(idx, int(getattr(flags, "value", flags)), _FLAGS_ROLE)

    def choice_enabled(self, index):
        model = self.model()
        idx = model.index(index, self.modelColumn())
        return bool(model.flags(idx) & Qt.ItemIsEnabled) if idx.isValid() else True


def combo(box, accessible, width):
    """The rail's look for a combo: name, font, cursor and a fixed width."""
    box.setAccessibleName(accessible)
    box.setFont(theme.font(13.5))         # height: the stylesheet's (min-height 34)
    box.setCursor(Qt.PointingHandCursor)
    box.setFixedWidth(width)
    return box


__all__ = ["PLAIN_ROLE", "PlainNameCombo", "combo"]
