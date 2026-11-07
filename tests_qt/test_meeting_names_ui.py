"""The Meetings list shows meetings by name, in sections by day: a real title as it is, a placeholder as who it was
with ("Meeting with Anna and David" - never "Meeting · Tue 14:30" with the time twice, never a sentence from the
notes), and the notes' first line on the date line."""
import json
from datetime import datetime, timedelta

import pytest

from tests_qt.test_page_meetings import _meeting, _open, _wait_for, meetings_root, shell  # noqa: F401  (fixtures)
from tests_qt.test_page_meetings import _inline_bg  # noqa: F401

NOTES_PLAN = "## Summary\nThe team planned the launch of the new website and agreed who writes the announcement.\n"
NOTES_HIRE = "## Summary\nInterview with a backend candidate. Strong on databases.\n"


def _name(dt):
    return dt.strftime("%Y%m%d_%H%M%S")


def _headers(ml):
    """The headings in the list as it is now (read from its layout: a widget of an earlier render may still be
    waiting to be deleted)."""
    from ui.pages.meetings import SectionHeader
    items = (ml._rows.itemAt(i).widget() for i in range(ml._rows.count()))
    return [w.label.text() for w in items if isinstance(w, SectionHeader)]


@pytest.fixture
def library(meetings_root):
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    folders = {
        "today": _meeting(meetings_root, _name(now - timedelta(hours=1)), "Meeting · Tue 11:00", notes=NOTES_PLAN),
        "today2": _meeting(meetings_root, _name(now - timedelta(hours=3)), "Weekly sync", notes=NOTES_HIRE),
        "yesterday": _meeting(meetings_root, _name(now - timedelta(days=1)), "Untitled Meeting", notes=None),
        "old": _meeting(meetings_root, _name(now - timedelta(days=75)), "Board prep", notes=NOTES_HIRE),
    }
    return folders


def test_the_list_has_a_header_per_stretch_of_time(shell, library):
    win, _app = shell("pro")
    page = _open(win)
    ml = page.list_view.meetings
    assert _wait_for(lambda: len(ml.rows) == 4)
    heads = _headers(ml)
    assert heads[0] == "Today" and heads[1] == "Yesterday"
    assert len(heads) == 3 and heads[2] not in ("Today", "Yesterday")          # one more for the old meeting


def test_a_placeholder_is_shown_as_who_it_was_with_never_the_time_twice_nor_a_sentence(shell, library):
    win, _app = shell("pro")
    page = _open(win)
    ml = page.list_view.meetings
    assert _wait_for(lambda: len(ml.rows) == 4)
    first = ml.rows[0]
    assert first.title_label.full_text() == "Meeting with Anna and David"
    assert "Meeting ·" not in first.title_label.full_text()
    assert first.when_label.full_text() == "11:00 · The team planned the launch of the new website and agreed who writes the announcement."
    # no notes either: who it was with, the date line says when
    assert ml.rows[2].title_label.full_text() == "Meeting with Anna and David"
    assert ml.rows[2].when_label.full_text() == "12:00"


def test_a_named_meeting_keeps_its_name_and_gets_the_notes_first_line_under_it(shell, library):
    win, _app = shell("pro")
    page = _open(win)
    ml = page.list_view.meetings
    assert _wait_for(lambda: len(ml.rows) == 4)
    sync = ml.rows[1]
    assert sync.title_label.full_text() == "Weekly sync"
    assert sync.when_label.full_text().endswith("Interview with a backend candidate. Strong on databases.")
    assert sync.when_label.full_text().startswith("09:00 · ")


def test_a_row_still_says_the_whole_date_to_a_screen_reader(shell, library):
    win, _app = shell("pro")
    page = _open(win)
    ml = page.list_view.meetings
    assert _wait_for(lambda: len(ml.rows) == 4)
    assert ml.rows[1].accessibleName().startswith("Weekly sync, Today · 09:00, ")


def test_a_search_shows_one_list_with_no_headers_and_full_dates(shell, library):
    win, _app = shell("pro")
    page = _open(win)
    ml = page.list_view.meetings
    assert _wait_for(lambda: len(ml.rows) == 4)
    ml.search.setText("backend")
    ml._run_search()
    assert _wait_for(lambda: len(ml.rows) == 2)
    assert _headers(ml) == []
    assert ml.rows[0].when_label.full_text().startswith("Today · ")
    ml.search.setText("")
    ml._run_search()
    assert _wait_for(lambda: len(_headers(ml)) == 3)


def test_the_notes_page_shows_the_same_name_as_the_list(shell, library):
    win, _app = shell("pro")
    page = _open(win)
    ml = page.list_view.meetings
    assert _wait_for(lambda: len(ml.rows) == 4)
    ml.rows[0].click()
    assert page.notes_view.title_label.text() == "Meeting with Anna and David"


def test_a_name_the_ai_gave_shows_in_the_list_as_a_real_title(shell, meetings_root):
    _meeting(meetings_root, _name(datetime.now() - timedelta(hours=1)), "Website launch plan", notes=NOTES_PLAN,
             title_source="auto")
    win, _app = shell("pro")
    page = _open(win)
    ml = page.list_view.meetings
    assert _wait_for(lambda: len(ml.rows) == 1)
    assert ml.rows[0].title_label.full_text() == "Website launch plan"
    assert ml.rows[0].when_label.full_text().endswith("The team planned the launch of the new website and agreed "
                                                      "who writes the announcement.")


def test_past_sessions_show_a_real_title_before_the_first_question(meetings_root):
    from ui.pages import live_assistant as la
    named = _meeting(meetings_root, "20261007_141800", "Pricing call with Acme", notes=NOTES_PLAN, source="live_assist",
                     title_source="auto")
    (named / "live_answers.jsonl").write_text(json.dumps({"time": "14:20", "question": "Can we launch?"}) + "\n",
                                              encoding="utf-8")
    asked = _meeting(meetings_root, "20261006_110000", "Live session 11:00", notes=None, source="live_assist")
    (asked / "live_answers.jsonl").write_text(json.dumps({"time": "11:02", "question": "Who owns it?"}) + "\n",
                                              encoding="utf-8")
    plain = _meeting(meetings_root, "20261005_090000", "Meeting · Mon 09:00", notes=NOTES_HIRE, source="live_assist")
    out = {s["dir"]: s["text"] for s in la.load_sessions()}
    assert out[str(named)] == "Pricing call with Acme"
    assert out[str(asked)] == "“Who owns it?”"
    assert out[str(plain)] == "Meeting with Anna and David"
