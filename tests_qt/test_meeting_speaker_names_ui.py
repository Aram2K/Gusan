"""The People card's speakers on a saved meeting (Gusan 2.1.11, offscreen): who spoke and how much, the inline rename
(pencil -> field -> Enter; Esc cancels; empty = Speaker N again), the attendees offered as names, the Transcript tab
drawn again at once, "Update notes with these names", and both looks readable while a name is being typed.

A real main window on the real Meetings page (the chat tests' fixtures); meetings in a temp folder."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import meeting_store
import speaker_map
from tests_qt.chat_fakes import FakeSession, make_meeting
# the page fixtures of the chat tests: a real main window on the real Meetings page, meetings in a temp folder
from tests_qt.test_meeting_chat_ui import _inline, _pump, build, meetings_root  # noqa: F401

THREE = "You: Shall we ship on Friday?\nSpeaker 1: Yes, the sync engine is ready and tested.\nSpeaker 2: Fine."


def rows(nv):
    return list(getattr(nv, "speaker_rows", []) or [])


def row(nv, sid):
    return next(r for r in rows(nv) if r.sid == sid)


def attendee_names(nv):
    from ui.pages.meeting_views import PersonRow
    return [nv._people.itemAt(i).widget().name for i in range(nv._people.count())
            if isinstance(nv._people.itemAt(i).widget(), PersonRow)]


def transcript_text(nv):
    return nv.transcript.toPlainText()


class NotesSession(FakeSession):
    """A session that rewrites notes (MeetingSession.retry_notes) and renames speakers like the real one."""

    def __init__(self, phase="idle"):
        super().__init__(phase)
        self.rewrites = []

    def retry_notes(self, engine=None, folder=None, persist=False):
        self.rewrites.append(str(folder))
        return True

    def rename_speaker(self, folder, sid, name):
        return speaker_map.rename(folder, sid, name)


@pytest.fixture
def open_meeting(build, meetings_root, monkeypatch):
    def make(transcript=THREE, people=(), session=None, **meta):
        folder = make_meeting(meetings_root, transcript=transcript, attendees=", ".join(people),
                              attendees_list=list(people), **meta)
        win, page, card, fake, app = build("pro", folder)
        s = session or NotesSession()
        app.meeting_session = s
        page.session()                                   # (bound to the page like the real one)
        nv = page.notes_view
        nv.reload()
        _pump()
        return folder, page, nv, s
    return make


# ── who spoke ────────────────────────────────────────────────────────────────

def test_the_speakers_are_listed_with_how_much_each_spoke(open_meeting):
    folder, page, nv, s = open_meeting(people=["Anna Petrosyan"])
    assert [r.sid for r in rows(nv)] == ["you", "s1", "s2"]
    you, one, two = rows(nv)
    assert you.who.full_text() == "Aram" and you.you_tag is not None and you.you_tag.text() == "(You)"
    assert one.who.full_text() == "Speaker 1" and two.who.full_text() == "Speaker 2"
    assert [r.share.text() for r in rows(nv)] == ["36%", "57%", "7%"]          # 5, 8 and 1 of 14 words
    assert one.pencil.accessibleName() == "Rename Speaker 1"
    # the attendees stay, below - the user is a speaker already, not listed twice
    assert attendee_names(nv) == ["Anna Petrosyan"]


def test_the_transcript_writes_you_as_the_first_name(open_meeting):
    folder, page, nv, s = open_meeting()
    nv.stack_index(1)
    text = transcript_text(nv)
    assert "Aram (You)" in text and "Speaker 1" in text
    assert (folder / "transcript.txt").read_text(encoding="utf-8").startswith("You: ")      # (the file says "You")


def test_a_plain_transcript_has_no_speakers_and_the_card_is_as_before(open_meeting):
    folder, page, nv, s = open_meeting(transcript="We talked about the launch.", people=["Anna"])
    assert rows(nv) == []
    assert attendee_names(nv) == ["Anna", "Aram"]


def test_an_automatic_name_says_where_it_came_from(open_meeting):
    folder, page, nv, s = open_meeting(
        transcript="You: hi\nAnna Petrosyan: hello", speakers_at="2026-11-21 10:00:00",
        speaker_names={"s1": "Anna Petrosyan"}, speaker_name_by={"s1": "screen"})
    anna = row(nv, "s1")
    assert anna.who.full_text() == "Anna Petrosyan"
    assert anna.hint is not None and anna.hint.text() == "Named from the call's screen"
    assert row(nv, "you").hint is None


# ── renaming ─────────────────────────────────────────────────────────────────

def test_pencil_field_enter_renames_everywhere_at_once(open_meeting):
    folder, page, nv, s = open_meeting()
    one = row(nv, "s1")
    one.pencil.click()                                   # no attendees: the field at once
    assert one.editing and one.edit.isVisibleTo(nv) and not one.who.isVisibleTo(nv)
    assert one.edit.placeholderText() == "Speaker 1"
    one.edit.setText("Anna")
    QTest.keyClick(one.edit, Qt.Key_Return)
    _pump()
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == THREE.replace("Speaker 1:", "Anna:")
    assert row(nv, "s1").who.full_text() == "Anna"
    nv.stack_index(1)
    assert "Anna" in transcript_text(nv) and "Speaker 1" not in transcript_text(nv)
    meta = meeting_store.load_meta(folder)
    assert meta["speaker_names"] == {"s1": "Anna"} and meta["speaker_name_by"] == {"s1": "user"}
    # the notes can be written again with the names
    assert nv.speaker_offer is not None and nv.speaker_offer.isVisibleTo(nv)
    assert nv.speaker_offer.text() == "Update notes with these names"
    nv.speaker_offer.click()
    _pump()
    assert s.rewrites == [str(folder)]


def test_esc_cancels_and_changes_nothing(open_meeting):
    folder, page, nv, s = open_meeting()
    one = row(nv, "s1")
    one.start_edit()
    one.edit.setText("Bob")
    QTest.keyClick(one.edit, Qt.Key_Escape)
    _pump()
    assert not one.editing and one.who.full_text() == "Speaker 1"
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == THREE
    assert nv.speaker_offer is None


def test_an_empty_name_is_speaker_n_again(open_meeting):
    folder, page, nv, s = open_meeting()
    speaker_map.rename(folder, "s2", "Bob")
    nv.reload()
    _pump()
    two = row(nv, "s2")
    assert two.who.full_text() == "Bob"
    two.start_edit()
    assert two.edit.text() == "Bob"
    two.edit.clear()
    QTest.keyClick(two.edit, Qt.Key_Return)
    _pump()
    assert row(nv, "s2").who.full_text() == "Speaker 2"
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == THREE


def test_leaving_a_changed_field_keeps_the_name(open_meeting):
    folder, page, nv, s = open_meeting()
    one = row(nv, "s1")
    one.start_edit()
    one.edit.setText("Lee")
    one.eventFilter(one.edit, _focus_out())
    _pump()
    assert row(nv, "s1").who.full_text() == "Lee"


def _focus_out():
    from PySide6.QtGui import QFocusEvent
    from PySide6.QtCore import QEvent
    return QFocusEvent(QEvent.FocusOut, Qt.OtherFocusReason)


def test_the_attendees_are_offered_as_names(open_meeting):
    folder, page, nv, s = open_meeting(people=["Anna Petrosyan", "Bob Lee", "Aram Adamyan"])
    one = row(nv, "s1")
    assert one.picks == ["Anna Petrosyan", "Bob Lee"]                       # never the user
    one.pencil.click()
    _pump()
    texts = [a.text() for a in one.menu.actions() if not a.isSeparator()]
    assert texts == ["Anna Petrosyan", "Bob Lee", "Type a name"]
    one.menu.hide()
    one.pick("Anna Petrosyan")
    _pump()
    assert row(nv, "s1").who.full_text() == "Anna Petrosyan"
    assert "Anna Petrosyan: Yes" in (folder / "transcript.txt").read_text(encoding="utf-8")
    # Anna is a speaker now: not listed again below, and not offered for another voice
    assert attendee_names(nv) == ["Bob Lee"]
    assert row(nv, "s2").picks == ["Bob Lee"]
    # "Type a name" is the field
    two = row(nv, "s2")
    two.pick("")
    assert two.editing


def test_a_name_another_speaker_has_is_said_and_nothing_changes(open_meeting):
    folder, page, nv, s = open_meeting()
    speaker_map.rename(folder, "s1", "Anna")
    nv.reload()
    _pump()
    two = row(nv, "s2")
    two.start_edit()
    two.edit.setText("anna")
    QTest.keyClick(two.edit, Qt.Key_Return)
    _pump()
    assert "already called" in nv.flash_label.text()
    assert row(nv, "s2").who.full_text() == "Speaker 2"
    assert "Speaker 2: Fine." in (folder / "transcript.txt").read_text(encoding="utf-8")


def test_nothing_is_renamed_while_the_meeting_is_being_written(open_meeting, meetings_root):
    s = NotesSession("processing")
    folder, page, nv, _s = open_meeting(session=s)
    s.meeting_dir = folder
    nv.render()
    _pump()
    assert rows(nv) and all(not r.pencil.isEnabled() for r in rows(nv))


def test_a_transcript_whose_speakers_all_have_names_is_not_called_unlabelled(qapp):
    from ui.pages import meeting_speakers_ui as msu
    data = {"parts": ["audio_part1.wav"], "duration_sec": 600, "transcript": "anna: hi\nBob Lee: hello",
            "meta": {"speaker_names": {"s1": "anna", "s2": "Bob Lee"}, "speaker_name_by": {"s1": "user", "s2": "user"}}}
    assert not msu.wants_hint(data)                     # (no "Speaker labels aren't in this transcript.")
    data["meta"] = {}
    data["transcript"] = "We talked about the launch."
    assert msu.wants_hint(data)


# ── both looks ───────────────────────────────────────────────────────────────

def _people_pieces(nv):
    """The People card's words as the window shows them (the page scrolled so the card is in view)."""
    import tests_qt.test_dialog_visibility as vis
    from PySide6.QtWidgets import QScrollArea
    w = nv.parentWidget()
    while w is not None and not isinstance(w, QScrollArea):
        w = w.parentWidget()
    if w is not None:
        w.ensureWidgetVisible(nv.people_card, 0, 24)
    _pump()
    win = nv.window()
    pieces = [p for p in vis.widget_pieces(win) if nv.people_card.geometry().isValid()
              and _inside(nv, win, p[1])]
    return win, pieces


def _inside(nv, win, rect):
    from PySide6.QtCore import QPoint, QRect
    top_left = nv.people_card.mapTo(win, QPoint(0, 0))
    return QRect(top_left, nv.people_card.size()).contains(rect.center())


def test_the_speakers_and_the_field_are_readable_in_both_looks(scheme, open_meeting):
    import tests_qt.test_dialog_visibility as vis
    folder, page, nv, s = open_meeting(
        transcript="You: hi there\nAnna Petrosyan: hello\nSpeaker 2: ok", speakers_at="2026-11-21 10:00:00",
        people=["Anna Petrosyan", "Mariam Sargsyan"],
        speaker_names={"s1": "Anna Petrosyan"}, speaker_name_by={"s1": "screen"}, speaker_names_changed=True)
    _pump()
    assert nv.speaker_offer is not None
    assert attendee_names(nv) == ["Mariam Sargsyan"]
    win, pieces = _people_pieces(nv)
    names = " ".join(p[0] for p in pieces)
    for word in ("Speakers", "(You)", "Named from the call", "Speaker 2", "Update notes with these names"):
        assert word in names, word
    vis.readable(win, "the People card's speakers", pieces=pieces, minimum=8)
    two = row(nv, "s2")
    two.pencil_clicked()                                  # the names menu: Mariam, then "Type a name"
    _pump()
    assert two.menu is not None and two.menu.isVisible()
    vis.readable(two.menu, "the menu of names for a speaker", minimum=2)
    two.menu.hide()
    two.start_edit()
    two.edit.setText("Lee")
    win, pieces = _people_pieces(nv)
    assert any("Lee" in p[0] for p in pieces)
    vis.readable(win, "a speaker's name being typed", pieces=pieces, minimum=8)
