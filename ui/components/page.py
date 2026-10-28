"""Page furniture: PageHeader, and the right-hand SettingsRail with SettingRows."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ui import theme
from ui.components import icons
from ui.components.badges import ProTag
from ui.components.surfaces import label


class PageHeader(QWidget):
    """Title row of every page (padding 28 32 20): optional eyebrow, the title
    (display face 28/34 bold) with an optional trailing widget, an optional
    one-line sub, and actions on the right, bottom-aligned."""

    def __init__(self, title, sub=None, actions=(), eyebrow=None, title_trail=None, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "pageHeader")
        root = QHBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 20)
        root.setSpacing(16)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        self.eyebrow_label = None
        if eyebrow:
            self.eyebrow_label = label(eyebrow.upper(), "eyebrow", self)
            col.addWidget(self.eyebrow_label)
        trow = QHBoxLayout()
        trow.setContentsMargins(0, 0, 0, 0)
        trow.setSpacing(10)
        self.title_label = label(title, "title", self)
        # Plain text: the live meeting's title and people come from a calendar
        # invite, and a QLabel would read markup in them (an <img> loads a file).
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setAccessibleName(title)
        # CSS line-height 34px: the face's own line (~37 px at 28 px) would
        # push the header, and every page under it, 3 px lower than the mockup.
        self.title_label.setFixedHeight(34)
        trow.addWidget(self.title_label, 0, Qt.AlignVCenter)
        self.title_trail = title_trail
        if title_trail is not None:
            title_trail.setParent(self)
            trow.addWidget(title_trail, 0, Qt.AlignVCenter)
        trow.addStretch(1)
        col.addLayout(trow)
        self.sub_label = label(sub or "", "subtitle", self, wrap=True)
        self.sub_label.setTextFormat(Qt.PlainText)
        self.sub_label.setMinimumHeight(20)                 # line-height 20px
        self.sub_label.setVisible(bool(sub))
        col.addWidget(self.sub_label)
        root.addLayout(col, 1)

        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(10)
        root.addLayout(self._actions, 0)
        root.setAlignment(self._actions, Qt.AlignBottom | Qt.AlignRight)
        for a in actions or ():
            self.add_action(a)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_title(self, text):
        self.title_label.setText(text)
        self.title_label.setAccessibleName(text)

    def set_sub(self, text):
        self.sub_label.setText(text or "")
        self.sub_label.setVisible(bool(text))

    def add_action(self, widget):
        widget.setParent(self)
        self._actions.addWidget(widget, 0, Qt.AlignBottom)
        return widget


class SettingRow(QFrame):
    """One setting: label (+ PRO tag), optional hint and real-model line, and
    its control on the right - or under the label with stacked=True.
    Rows draw a hairline under themselves except the last one in a rail."""

    def __init__(self, text, control=None, hint=None, pro=False, stacked=False, model=None,
                 parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "row")
        self.setProperty("last", False)
        self.control = control
        self._stacked = bool(stacked)

        head = QVBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(1)
        lrow = QHBoxLayout()
        lrow.setContentsMargins(0, 0, 0, 0)
        lrow.setSpacing(6)
        self.label = label(text, "label", self)
        lrow.addWidget(self.label, 0, Qt.AlignVCenter)
        self.pro_tag = None
        if pro:
            self.pro_tag = ProTag("s", parent=self)
            lrow.addWidget(self.pro_tag, 0, Qt.AlignVCenter)
        lrow.addStretch(1)
        head.addLayout(lrow)
        self.hint_label = label(hint or "", "hint", self, wrap=True)
        self.hint_label.setVisible(bool(hint))
        head.addWidget(self.hint_label)
        self.model_label = label(model or "", "model", self)
        self.model_label.setVisible(bool(model))
        head.addWidget(self.model_label)

        if stacked:
            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 12, 0, 12)
            lay.setSpacing(8)
            lay.addLayout(head)
            if control is not None:
                lay.addWidget(control)
        else:
            lay = QHBoxLayout(self)
            lay.setContentsMargins(0, 12, 0, 12)
            lay.setSpacing(12)
            lay.addLayout(head, 1)
            if control is not None:
                lay.addWidget(control, 0, Qt.AlignVCenter | Qt.AlignRight)
        if control is not None and not control.accessibleName():
            control.setAccessibleName(text)

    def set_hint(self, text):
        self.hint_label.setText(text or "")
        self.hint_label.setVisible(bool(text))

    def set_model(self, text):
        """The real model under a plain name, e.g. 'Whisper Small · 460 MB'."""
        self.model_label.setText(text or "")
        self.model_label.setVisible(bool(text))

    def set_last(self, last):
        theme.set_prop(self, "last", bool(last), children=False)


class SettingsRail(QFrame):
    """The right-hand 'settings for this page' panel (316 px glass card):
    a header (icon + title), rows, and an optional footer."""

    def __init__(self, title, icon="gear", width=316, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "rail")
        self.setFixedWidth(width)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)
        self.setAccessibleName(title)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QFrame(self)
        head.setProperty("gusan", "railHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(18, 14, 18, 14)
        hl.setSpacing(8)
        ic = QLabel(head)
        ic.setPixmap(icons.pixmap(icon, 16, "muted"))
        hl.addWidget(ic)
        self.title_label = QLabel(title, head)
        self.title_label.setFont(theme.font(13, 700))
        self.title_label.setProperty("textRole", "h3")
        hl.addWidget(self.title_label)
        hl.addStretch(1)
        root.addWidget(head)

        self._body = QWidget(self)
        self._rows_lay = QVBoxLayout(self._body)
        self._rows_lay.setContentsMargins(18, 4, 18, 8)
        self._rows_lay.setSpacing(0)
        root.addWidget(self._body)
        self._rows = []
        self._foot = None
        theme.elevate(self, "card")

    def add_row(self, row):
        """Append a SettingRow (or any widget). Returns it."""
        row.setParent(self._body)
        self._rows_lay.addWidget(row)
        if isinstance(row, SettingRow):
            for r in self._rows:
                r.set_last(False)
            self._rows.append(row)
            row.set_last(True)
        return row

    def rows(self):
        return list(self._rows)

    def set_foot(self, widget):
        """A footer under the rows (e.g. a 'More options' link)."""
        if self._foot is not None:
            self._foot.deleteLater()
        foot = QFrame(self)
        foot.setProperty("gusan", "railFoot")
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(18, 12, 18, 12)
        fl.addWidget(widget)
        self.layout().addWidget(foot)
        self._foot = foot
        return widget
