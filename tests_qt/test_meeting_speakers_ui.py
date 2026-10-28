"""Identify speakers on the Meetings page (ui/pages/meeting_speakers_ui.py),
offscreen: the ⋯ menu entry, the calm line at the top of the Transcript tab, the
banner with Cancel while it runs - and one real MeetingSession run to the
labelled transcript.

Also the names that come after a meeting's notes: the notes view carries one calm
banner (with Cancel) while the session adds them in the background, follows the
session's ``speakers_changed`` signal to the new transcript, and keeps the
suggestion line for a call that was never told apart."""
import threading

import pytest
from PySide6.QtCore import QUrl, Signal
from PySide6.QtWidgets import QFrame, QLabel

from tests_qt.test_page_meetings import (FakeSession, _inline_bg, _meeting, _open, _wait_for,  # noqa: F401
                                         meetings_root, shell)

LINK = "gusan:identify-speakers"


def _with_audio(folder, seconds=60, name="audio_part1.wav"):
    (folder / name).write_bytes(b"RIFF" + b"\0" * 400)
    return folder


class IdentifySession(FakeSession):
    """The session as the Meetings page sees it, with Identify speakers."""

    def __init__(self, result=(True, "", ""), **kw):
        super().__init__(**kw)
        self.result = result
        self.identifying = False
        self.busy_label = "Writing notes…"
        self.identify_calls = []
        self.cancels = 0

    def identify_speakers(self, folder):
        self.identify_calls.append(str(folder))
        if self.result[0]:
            self.meeting_dir = folder
            self.identifying = True
            self.busy_label = "Identifying speakers…"
            self._rewrite_only = True
            self._set("processing")
        return self.result

    def cancel_identify(self):
        self.cancels += 1
        return True


def _open_notes(shell, meetings_root, session=None, transcript="Hello team, we ship Friday.",
                audio=True, notes="## Summary\n- ok", **meta):
    folder = _meeting(meetings_root, "20261021_103134", "Live session 10:31", notes=notes,
                      transcript=transcript, **meta)
    if audio:
        _with_audio(folder)
    session = session or IdentifySession()
    win, app = shell("pro", meeting_session=session)
    page = _open(win)
    page.open_notes(str(folder))
    return page, session, folder


def _hint(page):
    return page.notes_view.transcript.toPlainText()


# ── the line in the Transcript tab ───────────────────────────────────────────
def test_a_recording_without_speaker_labels_gets_one_calm_line(shell, meetings_root):
    page, session, folder = _open_notes(shell, meetings_root)
    text = _hint(page)
    assert text.startswith("Speaker labels aren't in this transcript. Identify speakers")
    assert "Hello team, we ship Friday." in text
    assert LINK in page.notes_view.transcript.toHtml()
    assert text.count("Identify speakers") == 1                  # one line, not a banner


def test_a_transcript_that_has_labels_gets_no_line(shell, meetings_root):
    page, _s, _f = _open_notes(shell, meetings_root, transcript="You: Hi.\nSpeaker 1: Hello.")
    assert "Identify speakers" not in _hint(page)
    assert "Speaker 1" in _hint(page)


def test_a_meeting_with_only_its_recording_says_so(shell, meetings_root):
    page, _s, _f = _open_notes(shell, meetings_root, transcript="", notes="")
    assert _hint(page).startswith("Only the recording was saved. Read it and identify speakers")


def test_no_recording_means_no_line_and_no_menu_entry(shell, meetings_root):
    page, _s, _f = _open_notes(shell, meetings_root, audio=False)
    assert "Identify speakers" not in _hint(page)
    page.notes_view.more_menu.aboutToShow.emit()
    assert not page.notes_view.act_identify.isEnabled()


def test_a_cloud_transcript_without_labels_is_not_offered_a_re_read(shell, meetings_root):
    # A Live Assistant session whose words came from a cloud engine: Identify speakers would not
    # read the recording again (that would replace the words), so it is not suggested for labels.
    page, _s, _f = _open_notes(shell, meetings_root, transcribed_with="cloud")
    assert "Identify speakers" not in _hint(page)
    assert "Hello team, we ship Friday." in _hint(page)


def test_a_cloud_call_that_was_never_told_apart_still_gets_the_suggestion(shell, meetings_root):
    page, _s, _f = _open_notes(shell, meetings_root, transcript=YOU_THEM, transcribed_with="cloud")
    assert _hint(page).startswith("The people on the call aren't told apart yet. Identify speakers")


def test_a_three_second_recording_is_not_worth_a_line(shell, meetings_root):
    page, _s, _f = _open_notes(shell, meetings_root, duration_sec=3)
    assert "Identify speakers" not in _hint(page)


def test_the_link_starts_it_for_this_meeting(shell, meetings_root):
    page, session, folder = _open_notes(shell, meetings_root)
    nv = page.notes_view
    nv.transcript.anchorClicked.emit(QUrl(LINK))
    assert session.identify_calls == [str(folder)]
    # Any other link does nothing.
    nv.transcript.anchorClicked.emit(QUrl("https://example.com"))
    assert session.identify_calls == [str(folder)]


# ── the ⋯ menu ───────────────────────────────────────────────────────────────
def test_the_menu_entry_follows_the_recording_and_the_session(shell, meetings_root):
    page, session, folder = _open_notes(shell, meetings_root)
    nv = page.notes_view
    names = [a.text() for a in nv.more_menu.actions() if not a.isSeparator()]
    assert names.index("Identify speakers") == names.index("Rewrite notes") + 1
    nv.more_menu.aboutToShow.emit()
    assert nv.act_identify.isEnabled()
    session.phase = "recording"                                  # a meeting is being recorded
    nv.more_menu.aboutToShow.emit()
    assert not nv.act_identify.isEnabled()
    session.phase = "idle"
    nv.data["parts"] = []                                        # no recording to read
    nv.more_menu.aboutToShow.emit()
    assert not nv.act_identify.isEnabled()


def test_the_menu_entry_starts_it_and_the_banner_shows_it_with_cancel(shell, meetings_root):
    page, session, folder = _open_notes(shell, meetings_root)
    nv = page.notes_view
    nv.act_identify.trigger()
    assert session.identify_calls == [str(folder)]
    assert nv.banner.accessibleName() == "Identifying speakers…"
    assert nv.banner.button.text() == "Cancel"
    assert page.current_view() == "notes"
    assert not nv.act_rewrite.isEnabled()                        # nothing else while it works
    nv.banner.button.click()
    assert session.cancels == 1


def test_a_refusal_is_said_on_the_page(shell, meetings_root):
    session = IdentifySession(result=(False, "busy", "Finish your dictation first, then try again."))
    page, _s, folder = _open_notes(shell, meetings_root, session=session)
    page.notes_view.act_identify.trigger()
    nv = page.notes_view
    assert nv.flash_label.text() == "Finish your dictation first, then try again."
    assert page.current_view() == "notes"


def test_other_busy_work_keeps_its_own_banner(shell, meetings_root):
    page, session, folder = _open_notes(shell, meetings_root)
    session.meeting_dir = folder
    session.phase = "processing"
    session._rewrite_only = True
    page.notes_view.render()
    assert page.notes_view.banner.accessibleName() == "Writing notes…"
    assert not hasattr(page.notes_view.banner, "button")
    session.busy_label = "Rebuilding the transcript and notes…"
    page.notes_view.render()
    assert page.notes_view.banner.accessibleName() == "Rebuilding the transcript and notes…"


# ── a real session, start to labelled transcript ─────────────────────────────
def test_a_real_run_replaces_the_transcript_and_the_page_shows_it(shell, meetings_root, monkeypatch):
    import meeting_session
    import meeting_speakers
    monkeypatch.setattr(meeting_session.telemetry, "track", lambda *a, **k: None)

    class Rec:
        pass

    def fake_build(rec, parts, **kw):
        out = meeting_speakers.Outcome()
        out.texts = ["You: Hello team.\nSpeaker 1: We ship Friday."]
        out.mode = "sources"
        out.speakers = 1
        kw["status"]("Re-reading the recording… 50%")
        return out
    monkeypatch.setattr(meeting_session.meeting_speakers, "build_transcript", fake_build)
    folder = _meeting(meetings_root, "20261021_103134", "Live session 10:31",
                      transcript="Hello team. We ship Friday.")
    _with_audio(folder)
    win, app = shell("pro")
    app.recorder = Rec()
    app.live_assist = None
    app.save_config = lambda: None
    app.release_models_after_session = lambda: None
    app.cfg.update(meeting_consent_ack=True)
    page = _open(win)
    s = meeting_session.get_session(app)
    app.meeting_session = s
    page.session()                                               # binds the page to it
    page.open_notes(str(folder))
    nv = page.notes_view
    assert "Identify speakers" in _hint(page)
    status = []
    s.status.connect(status.append)
    nv.act_identify.trigger()
    assert s.identifying and nv.banner.accessibleName() == "Identifying speakers…"
    assert _wait_for(lambda: not s.is_busy, timeout=10)
    assert _wait_for(lambda: "Speaker 1" in _hint(page), timeout=10)      # the page reloaded the meeting
    assert "You" in _hint(page) and "We ship Friday." in _hint(page)
    assert "Identify speakers" not in _hint(page)                # labels are there now
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == \
        "You: Hello team.\nSpeaker 1: We ship Friday."
    assert not (folder / "transcript.before_speakers.txt").exists()
    assert "Re-reading the recording… 50%" in status
    assert page.current_view() == "notes"
    assert s.last_run_speakers is True
    s.deleteLater()


def test_a_real_run_that_cannot_label_says_why_above_the_notes(shell, meetings_root, monkeypatch):
    import meeting_session
    import meeting_speakers
    monkeypatch.setattr(meeting_session.telemetry, "track", lambda *a, **k: None)

    def fake_build(rec, parts, **kw):
        out = meeting_speakers.Outcome()
        out.reason = meeting_speakers.R_PRIVACY
        out.notice = meeting_speakers.notice_for(out.reason)
        return out
    monkeypatch.setattr(meeting_session.meeting_speakers, "build_transcript", fake_build)
    folder = _meeting(meetings_root, "20261021_103134", "Live session 10:31",
                      transcript="Hello team. We ship Friday.")
    _with_audio(folder)
    win, app = shell("pro")
    app.recorder = object()
    app.live_assist = None
    app.release_models_after_session = lambda: None
    page = _open(win)
    s = meeting_session.get_session(app)
    app.meeting_session = s
    page.session()
    page.open_notes(str(folder))
    errors = []
    s.error.connect(lambda code, msg: errors.append((code, msg)))
    page.notes_view.act_identify.trigger()
    assert _wait_for(lambda: not s.is_busy, timeout=10)
    assert _wait_for(lambda: bool(errors), timeout=10)
    assert errors[0][0] == "speakers" and "Privacy mode" in errors[0][1]
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == "Hello team. We ship Friday."
    assert _wait_for(lambda: any("Speaker labels weren't added" in n
                                 for n in (page.notes_view.data.get("meta") or {}).get("notices", [])),
                     timeout=10)
    s.deleteLater()


# ── the names that come after the notes ──────────────────────────────────────
YOU_THEM = "You: Hello team.\nThem: We ship Friday."
NAMED = "You: Hello team.\nSpeaker 1: We ship Friday."


class WorkingSession(IdentifySession):
    """The session with a background speaker pass the page can see and cancel."""
    speakers_changed = Signal(str)

    def __init__(self, state="", **kw):
        super().__init__(**kw)
        self.state_of = state
        self.cancelled = []

    def speakers_state(self, folder=None):
        return self.state_of

    def cancel_speaker_followup(self, folder=None):
        self.cancelled.append(str(folder))
        self.state_of = ""
        return True


def _banners(page):
    return [f for f in page.notes_view.doc.findChildren(QFrame)
            if f.property("gusan") == "banner" and not f.isHidden()]


def test_while_the_names_are_added_the_notes_show_one_calm_banner_with_cancel(shell, meetings_root):
    page, session, folder = _open_notes(shell, meetings_root, session=WorkingSession("running"),
                                        transcript=YOU_THEM)
    nv = page.notes_view
    banners = _banners(page)
    assert [b.accessibleName() for b in banners] == ["Identifying speakers…"]
    assert nv.banner.button.text() == "Cancel"
    assert "The notes are ready" in " ".join(l.text() for l in nv.banner.findChildren(QLabel))
    assert "Identify speakers" not in _hint(page)                # the banner says it; no second line
    assert "Hello team." in _hint(page)
    assert nv.act_rewrite.isEnabled()                             # the session is free: nothing is locked
    assert nv.act_identify.isEnabled()
    nv.banner.button.click()
    assert session.cancelled == [str(folder)]
    assert "Speaker names can be added from" in nv.flash_label.text()
    assert nv.flash_label.text().startswith("Stopped.")


def test_a_pass_that_waits_for_the_computer_says_so_and_can_be_cancelled(shell, meetings_root):
    page, session, folder = _open_notes(shell, meetings_root, session=WorkingSession("waiting"),
                                        transcript=YOU_THEM)
    nv = page.notes_view
    assert [b.accessibleName() for b in _banners(page)] == ["Speaker names are next"]
    assert nv.banner.button.text() == "Cancel"
    nv.banner.button.click()
    assert session.cancelled == [str(folder)]


def test_another_banner_of_the_meeting_comes_first(shell, meetings_root):
    # a meeting whose notes failed keeps its own banner
    page, session, folder = _open_notes(shell, meetings_root, session=WorkingSession("running"),
                                        transcript=YOU_THEM, notes="", notes_error="quota exceeded")
    assert [b.accessibleName() for b in _banners(page)] == ["Notes couldn't be written."]


def test_a_session_without_the_feature_shows_no_banner_of_this_kind(shell, meetings_root):
    page, _s, _f = _open_notes(shell, meetings_root, transcript=YOU_THEM)       # the plain fake session
    assert [b.accessibleName() for b in _banners(page)] == []


def test_a_call_that_was_never_told_apart_keeps_the_suggestion(shell, meetings_root):
    page, session, folder = _open_notes(shell, meetings_root, transcript=YOU_THEM)
    text = _hint(page)
    assert text.startswith("The people on the call aren't told apart yet. Identify speakers")
    assert LINK in page.notes_view.transcript.toHtml()
    assert "Them" in text and "Hello team." in text
    page.notes_view.transcript.anchorClicked.emit(QUrl(LINK))
    assert session.identify_calls == [str(folder)]


def test_the_suggestion_stays_away_once_a_pass_reached_its_end(shell, meetings_root):
    page, _s, _f = _open_notes(shell, meetings_root, transcript=YOU_THEM, speakers_at="2026-10-21 11:00:00")
    assert "Identify speakers" not in _hint(page)
    assert "Hello team." in _hint(page)


def test_the_suggestion_stays_away_when_a_notice_already_says_why(shell, meetings_root):
    brief = ("The other side spoke for only a short time, so it is shown as \"Them\". "
             "One other voice at most is likely.")
    page, _s, _f = _open_notes(shell, meetings_root, transcript=YOU_THEM, notices=[brief])
    assert "Identify speakers" not in _hint(page)


def test_the_suggestion_stays_away_when_the_call_has_its_speakers(shell, meetings_root):
    page, _s, _f = _open_notes(shell, meetings_root, transcript=NAMED)
    assert "Identify speakers" not in _hint(page)


def test_the_page_follows_the_pass_banner_first_then_the_new_transcript(shell, meetings_root):
    session = WorkingSession("")
    page, session, folder = _open_notes(shell, meetings_root, session=session, transcript=YOU_THEM)
    nv = page.notes_view
    assert _banners(page) == []
    session.state_of = "waiting"
    session.speakers_changed.emit(str(folder))                    # queued behind other work
    assert [b.accessibleName() for b in _banners(page)] == ["Speaker names are next"]
    session.state_of = "running"
    session.speakers_changed.emit(str(folder))
    assert [b.accessibleName() for b in _banners(page)] == ["Identifying speakers…"]
    assert "Them" in _hint(page)
    # the pass ends: the transcript on disk changed, the page reloads the meeting
    (folder / "transcript.txt").write_text(NAMED, encoding="utf-8")
    session.state_of = ""
    session.speakers_changed.emit(str(folder))
    assert "Speaker 1" in _hint(page) and "Them" not in _hint(page)
    assert _banners(page) == []
    assert "Identify speakers" not in _hint(page)
    assert nv.folder == str(folder)
    assert page.current_view() == "notes"


def test_a_change_for_another_meeting_or_another_view_leaves_the_page_alone(shell, meetings_root):
    session = WorkingSession("")
    page, session, folder = _open_notes(shell, meetings_root, session=session, transcript=YOU_THEM)
    (folder / "transcript.txt").write_text(NAMED, encoding="utf-8")
    session.speakers_changed.emit(str(meetings_root / "20250101_000000"))      # some other meeting
    assert "Them" in _hint(page)                                  # not reloaded
    session.speakers_changed.emit("")
    assert "Them" in _hint(page)
    page.show_list()
    session.speakers_changed.emit(str(folder))                    # the list is showing, not the notes
    assert page.current_view() == "list"
    assert "Them" in _hint(page)
    page.open_notes(str(folder))                                  # opened later: reads what is there
    assert "Speaker 1" in _hint(page)


def test_a_real_session_pass_in_the_background_to_the_new_transcript_and_a_cancel(shell, meetings_root, monkeypatch):
    """The real MeetingSession and its worker thread: the signal crosses to the window thread."""
    import meeting_session
    import meeting_speakers
    monkeypatch.setattr(meeting_session.telemetry, "track", lambda *a, **k: None)
    gate, started = threading.Event(), threading.Event()
    calls = []

    def fake_refine(folder, *, privacy=False, status=None, should_cancel=None, names=None, diarizer=None):
        calls.append(str(folder))
        started.set()
        while not gate.is_set():
            if should_cancel():
                return meeting_speakers.PassResult("cancelled", "cancelled")
            gate.wait(0.02)
        meeting_speakers.meeting_store.write_transcript(folder, NAMED)
        return meeting_speakers.PassResult("done", "", "", 1)
    monkeypatch.setattr(meeting_session.meeting_speakers, "refine_meeting", fake_refine)
    folder = _meeting(meetings_root, "20261021_103134", "Live session 10:31", transcript=YOU_THEM)
    _with_audio(folder)
    win, app = shell("pro")
    app.recorder = object()
    app.live_assist = None
    app.release_models_after_session = lambda: None
    page = _open(win)
    s = meeting_session.get_session(app)
    app.meeting_session = s
    page.session()                                                # binds the page to it
    page.open_notes(str(folder))
    nv = page.notes_view
    assert "The people on the call aren't told apart yet" in _hint(page)
    # the notes are out; the names are queued from the window thread, worked on by a worker
    assert s._followup.enqueue(str(folder))
    assert started.wait(10)
    assert _wait_for(lambda: s.speakers_state(str(folder)) == "running", timeout=10)
    assert _wait_for(lambda: [b.accessibleName() for b in _banners(page)] == ["Identifying speakers…"], timeout=10)
    assert not s.is_busy                                          # the session itself is free
    gate.set()
    assert _wait_for(lambda: "Speaker 1" in _hint(page), timeout=10)      # the page reloaded the meeting
    assert _wait_for(lambda: _banners(page) == [], timeout=10)
    assert "Identify speakers" not in _hint(page)
    assert s.speakers_state(str(folder)) == ""
    # a second one, cancelled from the banner
    (folder / "transcript.txt").write_text(YOU_THEM, encoding="utf-8")
    gate.clear()
    started.clear()
    page.open_notes(str(folder))
    assert s._followup.enqueue(str(folder))
    assert started.wait(10)
    assert _wait_for(lambda: bool(_banners(page)), timeout=10)
    nv.banner.button.click()
    assert _wait_for(lambda: s.speakers_state(str(folder)) == "", timeout=10)
    assert _wait_for(lambda: _banners(page) == [], timeout=10)
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == YOU_THEM      # nothing changed
    assert "aren't told apart yet" in _hint(page)                 # so the suggestion is back
    s.deleteLater()


# ── a second look: whoever reads the meeting is never moved ──────────────────
def _long_call(n=400):
    return "\n".join("Them: sentence number %d of the call goes on and on and on." % i for i in range(n))


def _named_call(n=400):
    return "\n".join("Speaker 1: sentence number %d of the call goes on and on and on." % i for i in range(n))


def _reader(shell, meetings_root, state="running"):
    """The notes of a meeting whose names are being added, the Transcript tab shown and scrolled."""
    from PySide6.QtWidgets import QApplication
    page, session, folder = _open_notes(shell, meetings_root, session=WorkingSession(state),
                                        transcript="You: Hello team.\n" + _long_call())
    win = page.window()
    win.resize(1100, 800)
    win.show()
    QApplication.processEvents()
    nv = page.notes_view
    nv.stack_index(1)                                             # reading the Transcript tab
    QApplication.processEvents()
    bar = nv.transcript.verticalScrollBar()
    assert bar.maximum() > 0, "the transcript must be long enough to scroll"
    bar.setValue(bar.maximum() // 2)
    return page, session, folder, nv, bar.value()


def test_the_reader_keeps_the_tab_and_the_place_when_the_names_arrive(shell, meetings_root):
    from PySide6.QtWidgets import QApplication
    page, session, folder, nv, place = _reader(shell, meetings_root)
    assert place > 0
    (folder / "transcript.txt").write_text("You: Hello team.\n" + _named_call(), encoding="utf-8")
    session.state_of = ""
    session.speakers_changed.emit(str(folder))                    # the pass ended: the page reads the meeting again
    QApplication.processEvents()
    assert "Speaker 1" in nv.transcript.toPlainText() and "Them" not in nv.transcript.toPlainText()
    assert nv.stack.currentIndex() == 1 and nv.tabs.index() == 1  # still on the Transcript tab...
    assert nv.transcript.verticalScrollBar().value() == place     # ...at the same place, not thrown to the top


def test_a_banner_that_comes_or_goes_does_not_move_the_reader(shell, meetings_root):
    from PySide6.QtWidgets import QApplication
    page, session, folder, nv, place = _reader(shell, meetings_root, state="waiting")
    for state in ("running", "waiting", "running"):
        session.state_of = state
        session.speakers_changed.emit(str(folder))
        QApplication.processEvents()
        assert nv.stack.currentIndex() == 1
        assert nv.transcript.verticalScrollBar().value() == place, state


def test_someone_typing_in_my_notes_is_not_thrown_out_when_the_names_arrive(shell, meetings_root):
    from PySide6.QtWidgets import QApplication
    page, session, folder, nv, _place = _reader(shell, meetings_root)
    nv.stack_index(2)                                             # My notes
    nv.my_notes.setFocus()
    nv.my_notes.setPlainText("Ask Anna about the date")           # typed a moment ago (its save is still waiting)
    (folder / "transcript.txt").write_text("You: Hello team.\n" + _named_call(), encoding="utf-8")
    session.state_of = ""
    session.speakers_changed.emit(str(folder))
    QApplication.processEvents()
    assert nv.stack.currentIndex() == 2 and nv.tabs.index() == 2
    assert nv.my_notes.toPlainText() == "Ask Anna about the date"
    assert (folder / "my_notes.md").read_text(encoding="utf-8") == "Ask Anna about the date"   # nothing lost


def test_opening_a_meeting_still_starts_on_the_notes_tab(shell, meetings_root):
    page, session, folder, nv, _place = _reader(shell, meetings_root)
    page.open_notes(str(folder))                                  # (not the pass's reload: a fresh open)
    assert nv.stack.currentIndex() == 0 and nv.tabs.index() == 0


class StoppableSession(WorkingSession):
    def __init__(self, state="", **kw):
        super().__init__(state, **kw)
        self.stopped = []

    def stop_speaker_pass(self, folder, timeout=8.0):
        import os
        self.stopped.append((str(folder), os.path.isdir(str(folder))))
        return True


def test_deleting_a_meeting_stops_its_pass_first(shell, meetings_root):
    page, session, folder = _open_notes(shell, meetings_root, session=StoppableSession("running"),
                                        transcript=YOU_THEM)
    page.notes_view.apply_delete()
    assert session.stopped == [(str(folder), True)]               # asked while the folder was still there
    assert not folder.exists()                                    # and then it went
