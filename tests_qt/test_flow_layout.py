"""The Home hero's FlowLayout (ui/pages/home.py): it wraps its items when the row
is too narrow, and it keeps no QLayoutItem wrappers of its own - PySide
invalidates item wrappers while their C++ items are alive (it showed up as
"QWidgetItem already deleted" on a later layout pass, only some runs), so a
layout holding them in a list breaks the hero chips."""
import pytest
import shiboken6
from PySide6.QtWidgets import QApplication, QFrame, QVBoxLayout, QWidget

from ui.pages.home import FlowLayout

CHIP_W, CHIP_H, GAP = 100, 24, 8


def _spin(n=4):
    for _ in range(n):
        QApplication.processEvents()


@pytest.fixture
def hero(qapp):
    """Three fixed-size chips in a FlowLayout inside a vertical layout, like the hero column."""
    host = QWidget()
    col = QVBoxLayout(host)
    col.setContentsMargins(0, 0, 0, 0)
    flow = FlowLayout(spacing=GAP)
    chips = []
    for _ in range(3):
        c = QFrame(host)
        c.setFixedSize(CHIP_W, CHIP_H)
        flow.addWidget(c)
        chips.append(c)
    col.addLayout(flow)
    host.resize(400, 200)
    host.show()
    _spin()
    yield host, flow, chips
    host.hide()
    host.deleteLater()
    _spin()


def test_one_row_when_it_fits_and_a_new_line_when_it_does_not(hero):
    _host, flow, _chips = hero
    row = 3 * CHIP_W + 2 * GAP                                   # 316
    assert flow.hasHeightForWidth()
    assert flow.heightForWidth(row) == CHIP_H
    assert flow.heightForWidth(row - 1) == 2 * CHIP_H + GAP      # the third chip wraps
    assert flow.heightForWidth(2 * CHIP_W + GAP) == 2 * CHIP_H + GAP
    assert flow.heightForWidth(CHIP_W) == 3 * CHIP_H + 2 * GAP   # one chip per line
    assert flow.heightForWidth(10) == 3 * CHIP_H + 2 * GAP       # narrower than a chip: still one each


def test_sizes(hero):
    _host, flow, _chips = hero
    assert flow.sizeHint().toTuple() == (3 * CHIP_W + 2 * GAP, CHIP_H)
    assert flow.minimumSize().toTuple() == (CHIP_W, CHIP_H)      # it may shrink to one chip wide
    assert flow.maximumSize().width() >= 16777215 and flow.maximumSize().height() >= 16777215
    assert flow.minimumHeightForWidth(200) == flow.heightForWidth(200)


def test_items_are_placed_in_rows(hero):
    host, flow, chips = hero
    host.resize(2 * CHIP_W + GAP + 5, 200)                       # room for two
    _spin()
    a, b, c = (ch.geometry() for ch in chips)
    assert a.top() == b.top() < c.top()
    assert b.left() == a.left() + CHIP_W + GAP
    assert c.left() == a.left()
    host.resize(400, 200)
    _spin()
    assert len({ch.geometry().top() for ch in chips}) == 1       # back on one line


def test_hidden_items_take_no_room(hero):
    host, flow, chips = hero
    chips[1].hide()
    _spin()
    assert flow.heightForWidth(2 * CHIP_W + GAP) == CHIP_H       # two chips left: one row
    assert flow.sizeHint().width() == 2 * CHIP_W + GAP
    chips[1].show()
    _spin()
    assert flow.heightForWidth(2 * CHIP_W + GAP) == 2 * CHIP_H + GAP


def test_it_survives_pyside_invalidating_the_item_wrappers(hero):
    """What the flaky Home test hit: the wrappers of the items go invalid while the
    C++ items live on. The layout must lay out as before, not raise on the next pass."""
    host, flow, chips = hero
    before = [ch.geometry().getRect() for ch in chips]
    for i in range(flow.count()):
        it = flow.itemAt(i)
        assert shiboken6.isValid(it)
        shiboken6.invalidate(it)
        assert not shiboken6.isValid(it)
    del it
    assert flow.heightForWidth(2 * CHIP_W + GAP) == 2 * CHIP_H + GAP
    assert flow.sizeHint().height() == CHIP_H and flow.minimumSize().height() == CHIP_H
    host.resize(2 * CHIP_W + GAP + 5, 200)                       # a layout pass with the wrappers invalidated
    _spin()
    assert chips[2].geometry().top() > chips[0].geometry().top()
    host.resize(400, 200)
    _spin()
    assert [ch.geometry().getRect() for ch in chips] == before


def test_it_keeps_no_python_list_of_items(hero):
    _host, flow, _chips = hero
    assert not [v for v in vars(flow).values() if isinstance(v, (list, tuple, dict)) and v]
    assert flow.count() == 3


def test_the_hero_chips_use_it():
    from ui.pages import home
    assert issubclass(home.FlowLayout, FlowLayout) and home.FlowLayout is FlowLayout
