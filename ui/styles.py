"""The 1.x stylesheet, still worn by the two small windows built on it: the Pro window (ui/pro_dialog.py)
and the macOS permissions guide (main.py). Its colours are ui/theme.py tokens (``@token``), so it follows
the light / dark look like everything else: ``stylesheet()`` is the sheet in the active look, and a window
that wears it sets it with ``theme.sheet(window, styles.stylesheet)`` (kept current after a switch).

Only the rules those two windows use are kept (the 1.x cards, tabs, lists, badges and drop zone went with
the 1.x windows). ``STYLESHEET`` (the sheet in the active look) is kept for old callers."""

TEMPLATE = """
/* Global Styles */
QWidget {
    background-color: @bg;
    color: @text;
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, "Outfit", "Inter", Roboto, sans-serif;
    font-size: 13px;
}

/* Main Window and Dialogs - soft gradient backdrop gives the glass panels depth */
QMainWindow, QDialog {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 @window-a, stop:0.5 @window-b, stop:1 @window-c);
}

/* Labels */
QLabel {
    background: transparent;
}
QLabel:disabled {
    color: @faint;
}

/* Buttons */
QPushButton {
    background-color: @surface;
    color: @text2;
    border: 1px solid @line2;
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: @bg;
    border-color: @blue-l;
    color: @text;
}
QPushButton:pressed {
    background-color: @bg2;
}
QPushButton:disabled {
    background-color: @bg2;
    color: @faint;
    border-color: @line;
}

/* Primary Accent Buttons */
QPushButton#primaryButton {
    background-color: @cta;
    color: @on-accent;
    border: 1px solid @cta-line;
}
QPushButton#primaryButton:hover {
    background-color: @cta-h;
    border-color: @cta-hl;
}
QPushButton#primaryButton:pressed {
    background-color: @cta-p;
}
QPushButton#primaryButton:disabled {
    background-color: @cta-off;
    color: @cta-off-tx;
    border-color: transparent;
}

/* Input Fields */
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: @input;
    color: @ink;
    border: 1px solid @line2;
    border-radius: 8px;
    padding: 8px 12px;
    selection-background-color: @blue;
    selection-color: @on-accent;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid @blue-l;
    background-color: @input;
}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {
    background-color: @bg2;
    color: @faint;
    border-color: @line;
}

/* Combobox (Dropdown) - Fully Custom Styled to Fix Windows Glitches */
QComboBox {
    background-color: @input;
    border: 1px solid @line2;
    border-radius: 8px;
    padding: 8px 36px 8px 12px;
    min-width: 120px;
    color: @text;
}
QComboBox:hover {
    border-color: @blue-l;
}
QComboBox:focus {
    border-color: @blue-l;
}
QComboBox:disabled {
    background-color: @bg2;
    color: @faint;
    border-color: @line;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 30px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    image: url("assets/chevron.svg");
    width: 12px;
    height: 8px;
    margin-right: 12px;
}
QComboBox::down-arrow:on {
    /* keep the same chevron while the popup is open */
    image: url("assets/chevron.svg");
}
QComboBox {
    combobox-popup: 0;          /* the list drops down under the box, never over it */
}
/* The list window IS the card (ui/popup_corners.py): white, one thin border; on Windows 11 the system rounds its
   corners and draws the border. The list inside has no border or rounding of its own - a rounded card inside the
   square window showed square corners and a square shadow around it. */
QComboBoxPrivateContainer {
    background: @popup;
    border: 1px solid @popup-line;
}
QComboBox QAbstractItemView {
    background-color: @popup;
    border: none;
    border-radius: 0px;
    selection-background-color: @sel-sub;
    selection-color: @blue-tx;
    outline: 0;
    padding: 4px;
    color: @text;
}
QComboBox QAbstractItemView::item {
    min-height: 26px;           /* 34 px with the padding: the same rows as the model pickers */
    padding: 4px 10px;
    border-radius: 6px;
}
QComboBox QAbstractItemView::item:selected {
    background-color: @sel-sub;
    color: @blue-tx;
}
QComboBox QAbstractItemView::item:hover {
    background-color: @bg2;
    color: @ink;
}

/* Scroll areas - visible scrollbars for long settings panels */
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical {
    background: @bg2;
    width: 10px;
    margin: 2px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: @faint;
    min-height: 28px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background: @muted;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

/* Scrollbars */
QScrollBar:vertical {
    background-color: @bg;
    width: 8px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background-color: @line2;
    min-height: 20px;
    border-radius: 4px;
}
QScrollBar::handle:vertical:hover {
    background-color: @blue-l;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar:horizontal {
    background-color: @bg;
    height: 8px;
    margin: 0px;
}
QScrollBar::handle:horizontal {
    background-color: @line2;
    min-width: 20px;
    border-radius: 4px;
}
QScrollBar::handle:horizontal:hover {
    background-color: @blue-l;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}

/* Checkboxes */
QCheckBox {
    spacing: 8px;
    color: @text2;
    font-weight: 500;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1.5px solid @line2;
    border-radius: 6px;
    background-color: @input;
}
QCheckBox::indicator:hover {
    border-color: @blue-l;
    background-color: @bg;
}
QCheckBox::indicator:checked {
    border-color: @cta;
    background-color: @cta;
    image: url("assets/check.svg");
}
QCheckBox::indicator:checked:hover {
    border-color: @cta-h;
    background-color: @cta-h;
}
QCheckBox::indicator:disabled {
    background-color: @bg2;
    border-color: @line;
}
"""


def stylesheet():
    """The 1.x sheet in the active look, its pictures' paths resolved (any working folder)."""
    from app_info import resource_path
    from ui import theme
    check = resource_path("assets", "check.svg").replace("\\", "/")
    chevron = resource_path("assets", "chevron_dark.svg" if theme.is_dark() else "chevron.svg").replace("\\", "/")
    return (theme.fill(TEMPLATE)
            .replace('url("assets/check.svg")', 'url("%s")' % check)
            .replace('url("assets/chevron.svg")', 'url("%s")' % chevron))


def __getattr__(name):
    if name == "STYLESHEET":
        return stylesheet()
    raise AttributeError(name)
