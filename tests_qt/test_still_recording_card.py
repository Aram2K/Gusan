"""The hourly "Still recording · 1 h" card (ui/floating/still_recording.py, Gusan 2.1.11): its words,
Stop and Keep recording, Esc, never taking focus by itself, its place above the calendar reminder,
and both looks drawn without a colour of its own."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from ui.floating import still_recording as sr


@pytest.fixture
def card(qapp):
    c = sr.StillRecordingCard(main_app=SimpleNamespace(cfg={"overlay_private": False}, _reminder=None))
    yield c
    c.hide()
    c.deleteLater()


def test_it_says_how_long_and_which_meeting(card):
    card.show_hours(1, "Weekly product sync")
    assert card.isVisible()
    assert card.title_text() == "Still recording · 1 h"
    assert card.meeting_text() == "Weekly product sync"
    assert card.testAttribute(Qt.WA_ShowWithoutActivating)      # never steals the typing
    card.show_hours(3, "")
    assert card.title_text() == "Still recording · 3 h"
    assert card.meeting_text() == "Your meeting"
    assert sr.heading(0) == "Still recording · 1 h"


def test_stop_and_keep_recording(card):
    got = []
    card.stop_clicked.connect(lambda: got.append("stop"))
    card.keep_clicked.connect(lambda: got.append("keep"))
    card.show_hours(1, "Call")
    card.keep_button.click()
    assert got == ["keep"] and not card.isVisible()
    card.show_hours(2, "Call")
    card.stop_button.click()
    assert got == ["keep", "stop"] and not card.isVisible()
    card.show_hours(3, "Call")
    card.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert got[-1] == "keep" and not card.isVisible()          # Esc = Keep recording, never Stop


def test_it_sits_above_the_calendar_reminder_when_that_one_is_up(card):
    card.show_hours(1, "Call")
    alone = card.target_pos()
    other = SimpleNamespace(isVisible=lambda: True, height=lambda: 200)
    card.app._reminder = other
    stacked = card.target_pos()
    assert stacked.x() == alone.x()
    assert abs(stacked.y() - alone.y()) == 200 - sr.mr.SH_TOP


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_both_looks_draw(card, scheme, monkeypatch):
    from ui import theme
    monkeypatch.setattr(theme, "is_dark", lambda: scheme == "dark")
    card.show_hours(1, "Call")
    img = card.grab().toImage()
    assert not img.isNull() and img.width() == card.width()
