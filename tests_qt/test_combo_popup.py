"""Every combo box's list: under the box, one rounded card (no square frame around it).

Fusion draws a list OVER its box (the box is hidden behind it), inside a square frame, and the stylesheet's
rounded card sat inside that frame: two borders, square corners, a hard shadow, and on the Speech & AI page the
Model box covered by its own list. theme.py's stylesheet (combobox-popup: 0, no frame on the list's container) fixes it."""
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QComboBox, QVBoxLayout, QWidget


@pytest.fixture
def host(qapp):
    from ui import theme
    made = []

    def build(x=40, y=40, w=700, h=420):
        win = QWidget()
        theme.apply(win)
        win.setGeometry(x, y, w, h)
        QVBoxLayout(win)
        win.show()
        QApplication.processEvents()
        made.append(win)
        return win
    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def _open(box):
    box.showPopup()
    for _ in range(5):
        QApplication.processEvents()
    return box.view().window()


def _plain(win):
    box = QComboBox(win)
    box.addItems(["Detect automatically", "English", "French", "German"])
    win.layout().addWidget(box)
    QApplication.processEvents()
    return box


def test_a_plain_combo_list_drops_down_under_the_box_not_over_it(host):
    win = host()
    box = _plain(win)
    pop = _open(box)
    try:
        field_bottom = box.mapToGlobal(QPoint(0, box.height())).y()
        assert pop.geometry().top() >= field_bottom - 2, (pop.geometry(), field_bottom)
    finally:
        box.hidePopup()


def test_the_list_windows_own_flags_are_left_alone(host):
    """Changing a combo list's window flags (to make it see-through) makes the list stop selecting a row and has
    crashed the app: the rounded look comes from the stylesheet alone."""
    win = host()
    box = _plain(win)
    pop = _open(box)
    try:
        assert (pop.windowFlags() & Qt.WindowType_Mask) == Qt.Popup
        assert not pop.testAttribute(Qt.WA_TranslucentBackground)
    finally:
        box.hidePopup()


def test_the_model_picker_list_with_chips_also_drops_down_under_the_box(host):
    import engine_choice
    from ui.components.model_combo import PlainNameCombo
    win = host()
    box = PlainNameCombo(win)
    box.enable_row_marks()
    box.fill_rows(engine_choice.writer_rows({}, False, choice="cloud"))
    win.layout().addWidget(box)
    box.setCurrentIndex(0)
    QApplication.processEvents()
    pop = _open(box)
    try:
        field_bottom = box.mapToGlobal(QPoint(0, box.height())).y()
        assert pop.geometry().top() >= field_bottom - 2, (pop.geometry(), field_bottom)
    finally:
        box.hidePopup()


def test_both_style_sheets_ask_for_a_drop_down_and_no_frame_of_the_lists_own():
    from ui import styles, theme
    for sheet in (theme.stylesheet(), styles.STYLESHEET):
        assert "combobox-popup: 0" in sheet
        assert "QComboBoxPrivateContainer" in sheet
