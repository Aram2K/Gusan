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
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWidgets import QFrame, QLabel, QProgressBar, QPushButton

from tests_qt.test_page_meetings import (FakeSession, _inline_bg, _meeting, _open, _wait_for,  # noqa: F401
                                         meetings_root, shell)
from ui.pages import meeting_speakers_ui as msu

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


def _wav_of(path, seconds):
    """A WAV whose header says it is ``seconds`` long (one frame a second: tiny)."""
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(1)
        w.writeframes(b"\0\0" * int(seconds))


def test_a_recording_that_ends_before_the_transcript_gets_no_line(shell, meetings_root):
    # A meeting past the ~2-hour cap: labels from its recording would cut the transcript,
    # so the Transcript tab never invites it (the ⋯ entry stays and says why when used).
    import meeting_store
    page, session, folder = _open_notes(shell, meetings_root, duration_sec=3 * 3600,
                                        **{meeting_store.META_RECORDING_CUT_SHORT: True})
    assert "Identify speakers" not in _hint(page)
    assert "Hello team, we ship Friday." in _hint(page)
    page.notes_view.more_menu.aboutToShow.emit()
    assert page.notes_view.act_identify.isEnabled()


def test_a_2_0_meeting_past_the_cap_gets_no_line_either(shell, meetings_root):
    notice = ("The saved recording ends at about 2 h 00 min - the most one session keeps. "
              "The transcript and notes still cover the whole meeting (without speaker labels).")
    page, _s, _f = _open_notes(shell, meetings_root, notices=[notice])
    assert "Identify speakers" not in _hint(page)


def test_an_older_meeting_is_known_by_its_recordings_length(shell, meetings_root):
    import meeting_store
    page, _s, folder = _open_notes(shell, meetings_root, duration_sec=3 * 3600, audio=False)
    _wav_of(folder / "audio_part1.wav", 2 * 3600)                # the recorder's cap, a 3-hour meeting
    page.open_notes(str(folder))
    assert page.notes_view.data["recording_cut_short"] is True
    assert "Identify speakers" not in _hint(page)
    # a meeting the recording covers (stopped right at the cap) still gets the line
    meeting_store.update_meta(folder, duration_sec=2 * 3600 + 20)
    page.open_notes(str(folder))
    assert page.notes_view.data["recording_cut_short"] is False
    assert _hint(page).startswith("Speaker labels aren't in this transcript. Identify speakers")


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


def test_a_real_session_refuses_a_meeting_past_the_cap_and_changes_nothing(shell, meetings_root, monkeypatch):
    import meeting_session
    import meeting_store
    monkeypatch.setattr(meeting_session.telemetry, "track", lambda *a, **k: None)
    built = []
    monkeypatch.setattr(meeting_session.meeting_speakers, "build_transcript", lambda *a, **k: built.append(1))
    full = "Hour one, the plan. Hour two, the budget. Hour three, the decisions."
    folder = _meeting(meetings_root, "20261021_103134", "Long meeting", transcript=full,
                      duration_sec=3 * 3600, **{meeting_store.META_RECORDING_CUT_SHORT: True})
    _with_audio(folder)
    win, app = shell("pro")
    app.recorder = object()
    app.live_assist = None
    app.release_models_after_session = lambda: None
    page = _open(win)
    s = meeting_session.get_session(app)
    app.meeting_session = s
    page.session()                                               # binds the page to it
    page.open_notes(str(folder))
    nv = page.notes_view
    assert "Identify speakers" not in _hint(page)                # not invited...
    nv.act_identify.trigger()                                    # ...and from the ⋯ menu it says why
    assert "would cut the transcript short" in nv.flash_label.text()
    assert not s.is_busy and built == []
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == full
    assert not (folder / "transcript.before_speakers.txt").exists()
    assert page.current_view() == "notes"
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
    """The session with a background speaker pass the page can see and cancel. ``snap`` holds what the pass
    reports beyond its state (what it waits for, the stage, the progress ...); ``line`` the one line about how
    it ended, given once."""
    speakers_changed = Signal(str)

    def __init__(self, state="", **kw):
        super().__init__(**kw)
        self.state_of = state
        self.snap = {}
        self.line = None
        self.cancelled = []

    def speakers_state(self, folder=None):
        return self.state_of

    def speakers_progress(self, folder):
        data = {"state": self.state_of, "stopping": False, "waiting_for": "", "stage": "", "done": 0,
                "total": 0, "fraction": None, "eta_s": None, "tries": 0}
        data.update(self.snap)
        return data

    def speakers_result(self, folder):
        line, self.line = self.line, None
        return line

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
    assert nv.note_label.text() == "Stopped."
    assert [b.accessibleName() for b in _banners(page)] == ["Speakers aren't told apart yet"]   # and the offer is back


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
    page, _s, _f = _open_notes(shell, meetings_root, session=FakeSession(), transcript=YOU_THEM)   # no Identify speakers
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
    assert [b.accessibleName() for b in _banners(page)] == ["Speakers aren't told apart yet"]
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

    def fake_refine(folder, *, privacy=False, status=None, should_cancel=None, names=None, diarizer=None,
                    progress=None, resume=None):
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
    assert _wait_for(lambda: [b.accessibleName() for b in _banners(page)] == ["Speakers aren't told apart yet"],
                     timeout=10)                                   # Stopped: the offer is back
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


# ── the honest banner: waiting, starting, running, stopping, and how it ends ──────────────────────────
# (every one runs in light and in dark: the `scheme` fixture)

def _names_to_tell(folder, text=YOU_THEM):
    """What a meeting keeps so that the pass can tell the call's people apart later: its segments and the
    call's saved sound (meeting_speakers.refinable)."""
    import meeting_store
    (folder / "audio_part1_system.wav").write_bytes(b"RIFF" + b"\0" * 400)
    assert meeting_store.write_segments(folder, {"version": 1, "parts": [{
        "audio": "audio_part1.wav", "kind": "sources", "seconds": 40.0, "done": False, "text": text,
        "segments": [{"s": 2.0, "e": 6.0, "t": "Hello team.", "src": "you"},
                     {"s": 7.0, "e": 30.0, "t": "We ship Friday.", "src": "them"}]}]})


def _meeting_page(shell, meetings_root, state="", snap=None, transcript=YOU_THEM, names=True,
                  notes="## Summary\n- ok", **meta):
    """A meeting that still says You / Them, open in the notes view, with a session whose pass is in ``state``."""
    session = WorkingSession(state)
    session.snap = dict(snap or {})
    folder = _meeting(meetings_root, "20261021_103134", "Live session 10:31", notes=notes,
                      transcript=transcript, **meta)
    _with_audio(folder)
    if names:
        _names_to_tell(folder, transcript)
    win, app = shell("pro", meeting_session=session)
    page = _open(win)
    page.open_notes(str(folder))
    return page, session, folder


def _words(page):
    """Every label of the banners on the page, in order."""
    return [lb.text() for b in _banners(page) for lb in b.findChildren(QLabel) if lb.text()]


def _bar(page):
    bars = [bar for b in _banners(page) for bar in b.findChildren(QProgressBar)]
    return bars[0] if bars else None


def _buttons(page):
    return [bt.text() for b in _banners(page) for bt in b.findChildren(QPushButton)]


@pytest.mark.parametrize("reason,words", [
    ("recording", "Waiting for your recording to finish."),
    ("dictation", "Waiting for your dictation to finish."),
    ("file", "Waiting for the file conversion to finish."),
    ("notes", "Waiting for the notes to finish."),
    ("queue", "Waiting for another meeting to finish."),
    ("settling", "Starting in a moment."),
    ("", "Starting in a moment."),
    ("busy", "Waiting for your other work to finish."),
])
def test_a_pass_that_is_only_queued_says_what_it_waits_for_and_never_says_identifying(
        scheme, shell, meetings_root, reason, words):
    page, session, folder = _meeting_page(shell, meetings_root, "waiting", {"waiting_for": reason})
    nv = page.notes_view
    assert [b.accessibleName() for b in _banners(page)] == ["Speaker names are next"]
    assert _words(page) == ["Speaker names are next", "The notes are ready. " + words]
    assert "Identifying" not in " ".join(_words(page))
    assert _buttons(page) == ["Cancel"] and _bar(page) is None
    assert nv.banner.property("tone") == "blue"


def test_a_pass_that_has_not_reported_yet_is_getting_ready_with_a_bar_that_just_moves(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "running", {"stage": "reading"})
    assert _words(page) == ["Identifying speakers…", "The notes are ready. Getting ready."]
    bar = _bar(page)
    assert bar is not None and (bar.minimum(), bar.maximum()) == (0, 0)          # not known how long
    assert bar.maximumHeight() == 6                                              # the app's own thin bar
    assert _buttons(page) == ["Cancel"]


def test_a_running_pass_shows_the_real_percentage_a_bar_and_the_time_left(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(
        shell, meetings_root, "running", {"stage": "voices", "done": 102, "total": 299, "fraction": 102 / 299.0,
                                          "eta_s": 100.0})
    nv = page.notes_view
    assert [b.accessibleName() for b in _banners(page)] == ["Identifying speakers · 34%"]
    assert _words(page) == ["Identifying speakers · 34%",
                            "The notes are ready. “Them” becomes Speaker 1, 2 and so on. About 2 min left."]
    bar = _bar(page)
    assert (bar.minimum(), bar.maximum(), bar.value()) == (0, 1000, 341)
    assert _buttons(page) == ["Cancel"]


def test_a_try_that_goes_on_from_an_earlier_one_keeps_its_percentage_while_it_reads_the_sound(
        scheme, shell, meetings_root):
    # (the bar must not fall back to "getting ready" for a moment after every interruption)
    page, session, folder = _meeting_page(
        shell, meetings_root, "running", {"stage": "reading", "done": 130, "total": 256, "fraction": 130 / 256.0})
    assert _words(page) == ["Identifying speakers · 50%",
                            "The notes are ready. “Them” becomes Speaker 1, 2 and so on."]
    bar = _bar(page)
    assert (bar.minimum(), bar.maximum(), bar.value()) == (0, 1000, 507)
    assert _buttons(page) == ["Cancel"]


def test_the_first_time_the_speaker_model_is_downloaded_with_its_real_percentage_and_cancel_works(
        scheme, shell, meetings_root):
    page, session, folder = _meeting_page(
        shell, meetings_root, "running", {"stage": "download", "done": 16, "total": 40, "fraction": 0.4})
    nv = page.notes_view
    assert [b.accessibleName() for b in _banners(page)] == ["Getting the speaker model · 40%"]
    assert _words(page) == ["Getting the speaker model · 40%", "The notes are ready. It is downloaded once."]
    assert "Identifying" not in " ".join(_words(page))                  # nothing is being identified yet
    bar = _bar(page)
    assert (bar.minimum(), bar.maximum(), bar.value()) == (0, 1000, 400)
    assert _buttons(page) == ["Cancel"] and nv.banner.property("tone") == "blue"
    nv.banner.button.click()
    assert session.cancelled == [str(folder)]                           # the same Cancel: it ends the download too


def test_a_download_of_an_unknown_size_is_a_bar_that_just_moves(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "running", {"stage": "download"})
    assert _words(page) == ["Getting the speaker model…", "The notes are ready. It is downloaded once."]
    bar = _bar(page)
    assert (bar.minimum(), bar.maximum()) == (0, 0)


def test_the_banner_goes_from_the_download_to_the_voices_in_place(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(
        shell, meetings_root, "running", {"stage": "download", "fraction": 0.9})
    nv = page.notes_view
    frame = nv.banner
    session.snap.update(stage="voices", fraction=0.0, eta_s=None)
    msu.tick(nv, frame)
    assert nv.banner is frame                                           # the same banner, words changed in place
    assert _words(page) == ["Identifying speakers · 0%",
                            "The notes are ready. “Them” becomes Speaker 1, 2 and so on."]


def test_the_banner_follows_the_pass_in_place_every_second(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(
        shell, meetings_root, "running", {"stage": "voices", "fraction": 0.1, "eta_s": 300.0})
    nv = page.notes_view
    frame = nv.banner
    assert frame.timer.interval() == 1000 and frame.timer.isActive()
    assert "About 5 min left." in _words(page)[1]
    session.snap.update(fraction=0.5, eta_s=60.0)
    msu.tick(nv, frame)
    assert nv.banner is frame                                      # the same banner: nothing was drawn again
    assert _words(page) == ["Identifying speakers · 50%",
                            "The notes are ready. “Them” becomes Speaker 1, 2 and so on. About 1 min left."]
    assert _bar(page).value() == 500 and frame.accessibleName() == "Identifying speakers · 50%"
    session.snap.update(fraction=0.9, eta_s=20.0)
    msu.tick(nv, frame)
    assert "Less than a minute left." in _words(page)[1]
    session.snap.update(fraction=0.95, eta_s=None)                 # no estimate: no sentence about it
    msu.tick(nv, frame)
    assert _words(page)[1] == "The notes are ready. “Them” becomes Speaker 1, 2 and so on."
    # the pass reports nothing again (a new try): the bar goes back to "just moving"
    session.snap.update(fraction=None, stage="")
    msu.tick(nv, frame)
    assert _words(page) == ["Identifying speakers…", "The notes are ready. Getting ready."]
    assert _bar(page).maximum() == 0
    assert nv.banner is frame


def test_the_banner_draws_the_page_once_when_its_kind_changes(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "waiting", {"waiting_for": "dictation"})
    nv = page.notes_view
    waiting = nv.banner
    session.snap["waiting_for"] = "settling"
    msu.tick(nv, waiting)
    assert nv.banner is waiting and "Starting in a moment." in _words(page)[1]      # same kind: in place
    session.state_of = "running"                                   # waiting -> working, no signal came
    msu.tick(nv, waiting)
    assert nv.banner is not waiting
    assert [b.accessibleName() for b in _banners(page)] == ["Identifying speakers…"]
    assert not waiting.timer.isActive()                            # the old one stopped looking
    msu.tick(nv, waiting)                                          # (a late tick of the old banner does nothing)
    assert [b.accessibleName() for b in _banners(page)] == ["Identifying speakers…"]
    session.snap["stopping"] = True
    msu.tick(nv, nv.banner)
    assert [b.accessibleName() for b in _banners(page)] == ["Stopping…"]


def test_cancel_says_stopping_until_the_worker_has_really_stopped(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "running",
                                          {"stage": "voices", "fraction": 0.4, "eta_s": 90.0})
    nv = page.notes_view

    def slow_cancel(folder=None):
        session.cancelled.append(str(folder))
        session.snap["stopping"] = True                            # told, but the worker has not ended yet
        return True
    session.cancel_speaker_followup = slow_cancel
    nv.banner.button.click()
    assert session.cancelled == [str(folder)]
    assert [b.accessibleName() for b in _banners(page)] == ["Stopping…"]
    assert _words(page) == ["Stopping…"] and _buttons(page) == [] and _bar(page) is None
    assert nv.note_label.isHidden()                               # nothing is said until it has stopped
    # the worker ended: the session says so and has the line for it
    session.state_of, session.snap, session.line = "", {}, ("Stopped.", "ok")
    session.speakers_changed.emit(str(folder))
    assert nv.note_label.text() == "Stopped."
    assert [b.accessibleName() for b in _banners(page)] == ["Speakers aren't told apart yet"]   # and the offer is back


@pytest.mark.parametrize("line,text,tone", [
    (("Found 2 speakers on the call.", "ok"), "Found 2 speakers on the call.", "ok"),
    (("Only one person spoke on the call.", "ok"), "Only one person spoke on the call.", "ok"),
    (("The other side spoke only briefly, so it stays Them.", "ok"),
     "The other side spoke only briefly, so it stays Them.", "ok"),
    (("Nobody on the call spoke.", "ok"), "Nobody on the call spoke.", "ok"),
])
def test_when_the_pass_finishes_the_banner_goes_and_one_calm_line_says_what_changed(
        scheme, shell, meetings_root, line, text, tone):
    page, session, folder = _meeting_page(shell, meetings_root, "running",
                                          {"stage": "voices", "fraction": 0.99, "eta_s": 2.0})
    nv = page.notes_view
    assert _banners(page)
    (folder / "transcript.txt").write_text(NAMED, encoding="utf-8")
    import meeting_store
    meeting_store.update_meta(folder, speakers_at="2026-10-30 16:40:00")
    session.state_of, session.snap, session.line = "", {}, line
    session.speakers_changed.emit(str(folder))
    assert _banners(page) == []                                    # the banner is gone ...
    assert "Speaker 1" in _hint(page) and "Them" not in _hint(page)        # ... the page shows what the pass wrote ...
    assert nv.note_label.text() == text and not nv.note_label.isHidden()   # ... and says what came of it
    assert nv.note_label.property("mtone") == tone
    assert nv._note.interval() == 6000                            # for a few seconds: long enough to read
    assert session.line is None                                    # the line was taken: given once


def test_a_pass_that_could_not_tell_them_apart_says_so_in_amber_and_offers_the_retry(scheme, shell, meetings_root):
    notice = "People on the call couldn't be told apart, so they are shown as \"Them\". Try again from ⋯ › Identify speakers."
    page, session, folder = _meeting_page(shell, meetings_root, "running", {"stage": "voices", "fraction": 0.5})
    nv = page.notes_view
    import meeting_store
    meeting_store.update_meta(folder, notices=[notice])             # the pass left its notice above the notes
    session.state_of, session.snap, session.line = "", {}, ("Couldn't tell the people on the call apart.", "amber")
    session.speakers_changed.emit(str(folder))
    assert nv.note_label.text() == "Couldn't tell the people on the call apart."
    assert nv.note_label.property("mtone") == "amber"
    assert [b.accessibleName() for b in _banners(page)] == ["Speakers aren't told apart yet"]
    assert notice in [lb.text() for lb in nv.doc.findChildren(QLabel)]      # the notice stays where it was
    assert _buttons(page) == ["Identify speakers"]


def test_a_pass_that_other_work_kept_interrupting_says_so_and_can_be_tried_again(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "waiting", {"waiting_for": "dictation"})
    nv = page.notes_view
    session.state_of, session.snap = "", {}
    session.line = ("Other work kept interrupting it. You can try again.", "amber")
    session.speakers_changed.emit(str(folder))
    assert nv.note_label.text() == "Other work kept interrupting it. You can try again."
    assert nv.note_label.property("mtone") == "amber"
    assert [b.accessibleName() for b in _banners(page)] == ["Speakers aren't told apart yet"]


def test_the_line_is_taken_even_when_the_meeting_is_not_open(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "")
    page.show_list()
    session.line = ("Found 2 speakers on the call.", "ok")
    session.speakers_changed.emit(str(folder))
    assert session.line is None                                    # the session keeps nothing
    assert page.current_view() == "list"
    page.open_notes(str(folder))
    assert page.notes_view.note_label.isHidden()                  # and it was not left for a later look


def test_a_line_for_a_pass_that_is_still_going_is_not_taken_early(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "waiting", {"waiting_for": "dictation"})
    session.line = ("Stopped.", "ok")
    session.speakers_changed.emit(str(folder))                     # a signal while it still waits (an interruption)
    assert session.line == ("Stopped.", "ok")
    assert page.notes_view.note_label.isHidden()


# ── a banner that stays behind is the worst outcome ──────────────────────────────────────────────────
def test_a_lost_signal_cannot_leave_the_banner_behind(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "running", {"stage": "voices", "fraction": 0.7})
    nv = page.notes_view
    frame = nv.banner
    (folder / "transcript.txt").write_text(NAMED, encoding="utf-8")
    import meeting_store
    meeting_store.update_meta(folder, speakers_at="2026-10-30 16:40:00")
    session.state_of, session.snap = "", {}                        # the pass ended, and speakers_changed never came
    session.line = ("Found 2 speakers on the call.", "ok")
    msu.tick(nv, frame)                                            # the banner looks again by itself
    assert _banners(page) == []
    assert "Speaker 1" in _hint(page)                              # it read the meeting again
    assert nv.note_label.text() == "Found 2 speakers on the call."
    assert not frame.timer.isActive()


def test_the_banner_looks_again_on_its_own_timer(scheme, shell, meetings_root):
    from PySide6.QtWidgets import QApplication
    page, session, folder = _meeting_page(shell, meetings_root, "running", {"stage": "voices", "fraction": 0.2})
    nv = page.notes_view
    nv.banner.timer.setInterval(20)                                # (a second is too long to wait for in a test)
    session.snap.update(fraction=0.6)
    assert _wait_for(lambda: "60%" in nv.banner.accessibleName(), timeout=5)
    session.state_of, session.snap = "", {}
    assert _wait_for(lambda: _buttons(page) == ["Identify speakers"], timeout=5)
    QApplication.processEvents()


def test_a_banner_that_was_replaced_stops_looking(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "waiting", {"waiting_for": "dictation"})
    nv = page.notes_view
    first = nv.banner
    nv.render()
    assert nv.banner is not first
    msu.tick(nv, first)
    assert not first.timer.isActive()
    assert nv.banner.timer.isActive()

def test_a_banner_the_page_took_off_stops_looking_and_does_not_read_the_meeting_again(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "running", {"stage": "voices", "fraction": 0.5})
    nv = page.notes_view
    old = nv.banner
    (folder / "transcript.txt").write_text(NAMED, encoding="utf-8")
    import meeting_store
    meeting_store.update_meta(folder, speakers_at="2026-10-30 16:40:00")
    session.state_of, session.snap = "", {}
    session.speakers_changed.emit(str(folder))                     # the page reloaded: no banner any more
    assert _banners(page) == []
    session.line = ("Found 2 speakers on the call.", "ok")
    msu.tick(nv, old)                                              # a late tick of the banner that went away
    assert not old.timer.isActive()
    assert session.line == ("Found 2 speakers on the call.", "ok")        # it did not take (or say) anything

def test_a_session_that_raises_cannot_break_the_page(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "running", {"stage": "voices", "fraction": 0.3})
    nv = page.notes_view

    def boom(folder):
        raise RuntimeError("session gone")
    session.speakers_progress = boom
    msu.tick(nv, nv.banner)                                        # falls back to the state alone: still working
    assert [b.accessibleName() for b in _banners(page)] == ["Identifying speakers…"]
    session.speakers_state = boom
    msu.tick(nv, nv.banner)                                        # nothing can be read: the pass is taken as gone
    assert "Identifying" not in " ".join(_words(page))


# ── after a restart (or a cancel, a give-up, a failure): the offer ───────────────────────────────────
def test_after_a_restart_the_notes_offer_identify_speakers_with_a_clear_line(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "")           # nothing queued: the app was restarted
    nv = page.notes_view
    assert nv.stack.currentIndex() == 0                                          # the Notes tab
    assert [b.accessibleName() for b in _banners(page)] == ["Speakers aren't told apart yet"]
    assert _words(page) == ["Speakers aren't told apart yet", "The people on the call show as “Them”."]
    assert _buttons(page) == ["Identify speakers"] and _bar(page) is None
    assert nv.banner.property("tone") == "neutral"
    assert nv.banner.button.accessibleName() or nv.banner.button.text()
    nv.banner.button.click()
    assert session.identify_calls == [str(folder)]                               # once
    # the Transcript tab keeps its one-line suggestion
    assert _hint(page).startswith("The people on the call aren't told apart yet. Identify speakers")


def test_identify_speakers_on_the_banner_shows_the_waiting_banner_at_once(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "")
    nv = page.notes_view

    def queue(folder):
        session.identify_calls.append(str(folder))
        session.state_of, session.snap = "waiting", {"waiting_for": "dictation"}   # (what the real session does)
        return True, "", ""
    session.identify_speakers = queue
    nv.banner.button.click()
    assert [b.accessibleName() for b in _banners(page)] == ["Speaker names are next"]
    assert _words(page)[1] == "The notes are ready. Waiting for your dictation to finish."


def test_a_refusal_of_identify_speakers_is_said_and_the_offer_stays(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "")
    session.result = (False, "busy", "Finish your dictation first, then try again.")
    nv = page.notes_view
    nv.banner.button.click()
    assert nv.flash_label.text() == "Finish your dictation first, then try again."
    assert [b.accessibleName() for b in _banners(page)] == ["Speakers aren't told apart yet"]


@pytest.mark.parametrize("kind", ["speakers_at", "named", "cloud_nothing_saved", "no_them", "no_recording"])
def test_no_offer_when_there_is_nothing_to_offer(scheme, shell, meetings_root, kind):
    kw = {}
    transcript, names, audio = YOU_THEM, True, True
    if kind == "speakers_at":
        kw["speakers_at"] = "2026-10-30 16:40:00"                  # a pass reached its end
    elif kind == "named":
        transcript = NAMED
    elif kind == "cloud_nothing_saved":
        kw["transcribed_with"] = "cloud"
        names = False                                              # its words are kept and nothing is left to work from
    elif kind == "no_them":
        transcript = "You: Hello team.\nAnna: We ship Friday."
    elif kind == "no_recording":
        names = audio = False
    session = WorkingSession("")
    folder = _meeting(meetings_root, "20261021_103134", "Live session 10:31", notes="## Summary\n- ok",
                      transcript=transcript, **kw)
    if audio:
        _with_audio(folder)
    if names:
        _names_to_tell(folder, transcript)
    win, app = shell("pro", meeting_session=session)
    page = _open(win)
    page.open_notes(str(folder))
    assert _banners(page) == []


def test_a_cloud_meeting_that_can_still_be_named_gets_the_offer(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "", transcribed_with="cloud")
    assert page.notes_view.data["can_name_call"] is True
    assert [b.accessibleName() for b in _banners(page)] == ["Speakers aren't told apart yet"]


def test_another_banner_comes_before_the_offer(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "", notes="", notes_error="quota exceeded")
    assert [b.accessibleName() for b in _banners(page)] == ["Notes couldn't be written."]


def test_the_page_knows_whether_the_call_can_still_be_named(scheme, shell, meetings_root):
    import ui.pages.meeting_views as views
    page, session, folder = _meeting_page(shell, meetings_root, "")
    assert views.read_meeting(str(folder))["can_name_call"] is True
    (folder / "audio_part1_system.wav").unlink()                   # the call's sound is gone
    assert views.read_meeting(str(folder))["can_name_call"] is False
    (folder / "transcript.txt").write_text(NAMED, encoding="utf-8")
    assert views.read_meeting(str(folder))["can_name_call"] is False


# ── what is said, and what must never get in ─────────────────────────────────────────────────────────
def test_the_words_are_fixed_text_so_nothing_from_outside_can_become_markup(scheme, shell, meetings_root):
    hostile = "<b>dictation</b><img src=x onerror=1>"
    page, session, folder = _meeting_page(
        shell, meetings_root, "waiting", {"waiting_for": hostile},
        transcript="You: <b>Hello</b> team.\nThem: *bold* __x__ <script>1</script>")
    nv = page.notes_view
    assert _words(page) == ["Speaker names are next", "The notes are ready. Starting in a moment."]
    for lb in nv.banner.findChildren(QLabel):
        if lb.text():                                              # (the icon is a picture)
            assert lb.textFormat() == Qt.PlainText
    session.snap["waiting_for"] = "dictation"
    msu.tick(nv, nv.banner)
    assert _words(page)[1] == "The notes are ready. Waiting for your dictation to finish."


def test_a_hostile_meeting_title_and_notice_never_change_the_banners_words(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "", notices=["<b>x</b>"])
    nv = page.notes_view
    nv.data["title"] = "<i>Quarterly</i> *review*"
    nv.render()
    assert _words(page) == ["Speakers aren't told apart yet", "The people on the call show as “Them”."]
    assert "<" not in nv.banner.accessibleName()


@pytest.mark.parametrize("state,snap", [
    ("waiting", {"waiting_for": "recording"}),
    ("running", {}),
    ("running", {"stage": "voices", "fraction": 0.34, "eta_s": 100.0}),
    ("running", {"stage": "reading", "done": 5, "total": 10, "fraction": 0.5}),
    ("running", {"stage": "download", "fraction": 0.25}),
    ("running", {"stage": "download"}),
    ("running", {"stopping": True}),
    ("", {}),
])
def test_every_banner_has_a_name_and_no_markdown_or_markup_in_its_words(scheme, shell, meetings_root, state, snap):
    page, session, folder = _meeting_page(shell, meetings_root, state, snap)
    (banner,) = _banners(page)
    assert banner.accessibleName().strip()
    assert banner.accessibleName() == page.notes_view.banner.title_label.text()
    for text in _words(page):
        assert not any(ch in text for ch in "*_`#<>[]{}\\"), text
        assert "limit" not in text.lower() and "Armen" not in text
    for button in banner.findChildren(QPushButton):
        assert button.text().strip() and button.isEnabled()
    for bar in banner.findChildren(QProgressBar):
        assert bar.accessibleName() == "Progress" and not bar.isTextVisible()


def test_cancel_calls_the_session_once_and_only_once(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "waiting", {"waiting_for": "recording"})
    page.notes_view.banner.button.click()
    assert session.cancelled == [str(folder)]


def test_the_banner_of_a_meeting_does_not_move_the_notes_or_the_side_column(scheme, shell, meetings_root):
    # The right column (People and the player) and the notes keep their places: the banner only ever
    # changes the banner (the chat on the side must not be disturbed by a pass)
    from PySide6.QtWidgets import QApplication
    page, session, folder = _meeting_page(shell, meetings_root, "running", {"stage": "voices", "fraction": 0.1})
    win = page.window()
    win.resize(1100, 800)
    win.show()
    QApplication.processEvents()
    nv = page.notes_view
    before = (nv.people_card.geometry(), nv.audio_card.geometry())
    session.snap.update(fraction=0.6, eta_s=40.0)
    msu.tick(nv, nv.banner)
    QApplication.processEvents()
    assert (nv.people_card.geometry(), nv.audio_card.geometry()) == before


@pytest.mark.parametrize("width", [960, 1100, 1180, 1280])
def test_the_line_about_how_a_pass_ended_never_widens_the_page_or_moves_a_button(scheme, shell, meetings_root, width):
    # (beside the toolbar's buttons it needed 160 pixels more than a window of 1200 or less had: the page grew
    # past the window and the buttons, the People card and the player were cut off)
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication
    page, session, folder = _meeting_page(shell, meetings_root, "", {}, transcript=NAMED)
    win = page.window()
    win.resize(width, 800)
    win.show()
    QApplication.processEvents()
    nv = page.notes_view

    def where():
        QApplication.processEvents()
        return (nv.minimumSizeHint().width(),
                [w.mapTo(win, QPoint(0, 0)).x() for w in (nv.copy_btn, nv.export_btn, nv.more_btn)],
                nv.title_label.geometry(), nv.people_card.geometry(), nv.audio_card.geometry(),
                nv.tabs.geometry())
    before = where()
    assert nv.note_label.isHidden()
    nv.note("Found 2 speakers on the call.", "ok", ms=60000)
    assert not nv.note_label.isHidden()
    assert where() == before                                       # nothing moved, nothing grew
    assert nv.note_label.property("mtone") == "ok"
    nv.note("Couldn't tell the people on the call apart.", "amber", ms=60000)
    assert where() == before and nv.note_label.property("mtone") == "amber"


def test_the_note_is_plain_text_it_ends_by_itself_and_goes_when_another_meeting_opens(scheme, shell, meetings_root):
    page, session, folder = _meeting_page(shell, meetings_root, "", {}, transcript=NAMED)
    nv = page.notes_view
    nv.note("<b>Found</b> 2 speakers", "ok", ms=20)
    assert nv.note_label.textFormat() == Qt.PlainText and nv.note_label.text() == "<b>Found</b> 2 speakers"
    assert _wait_for(lambda: nv.note_label.isHidden() and nv.note_label.text() == "", timeout=3)
    assert nv._note.interval() == 20 or nv._note.interval() == 6000                  # (the next one is 6 s again)
    nv.note("Found 2 speakers on the call.")
    assert nv._note.isActive() and nv._note.interval() == 6000
    other = _meeting(meetings_root, "20261029_090000", "Another meeting")
    nv.load(str(other))                                            # another meeting
    assert nv.note_label.isHidden() and not nv._note.isActive()
    nv.note("Found 2 speakers on the call.")
    nv.reload(keep_view=True)                                      # the same meeting read again: the note stays
    assert not nv.note_label.isHidden()


# ── the words, without a window ──────────────────────────────────────────────────────────────────────
def test_eta_text():
    assert msu.eta_text(None) == "" and msu.eta_text("x") == "" and msu.eta_text(-5) == ""
    assert msu.eta_text(float("nan")) == ""
    assert msu.eta_text(0) == msu.eta_text(44.9) == "Less than a minute left."
    assert msu.eta_text(45) == "About 1 min left."
    assert msu.eta_text(89) == "About 1 min left."
    assert msu.eta_text(90) == msu.eta_text(100) == "About 2 min left."
    assert msu.eta_text(600) == "About 10 min left."


def test_waiting_text_knows_only_its_own_reasons():
    assert msu.waiting_text("recording") == "Waiting for your recording to finish."
    assert msu.waiting_text("anything else") == msu.waiting_text(None) == msu.waiting_text("") == "Starting in a moment."


def test_working_lines():
    assert msu.working_lines({}) == ("Identifying speakers…", "The notes are ready. Getting ready.", -1)
    assert msu.working_lines({"fraction": "x"})[2] == -1
    title, line, progress = msu.working_lines({"fraction": 0.5, "eta_s": 30})
    assert (title, progress) == ("Identifying speakers · 50%", 0.5)
    assert line.endswith("Less than a minute left.")
    assert msu.working_lines({"fraction": 7})[0] == "Identifying speakers · 100%"
    assert msu.working_lines({"fraction": -1})[0] == "Identifying speakers · 0%"


def test_the_download_lines():
    line = "The notes are ready. It is downloaded once."
    assert msu.working_lines({"stage": "download"}) == ("Getting the speaker model…", line, -1)
    assert msu.working_lines({"stage": "download", "fraction": 0.25}) == ("Getting the speaker model · 25%", line, 0.25)
    assert msu.working_lines({"stage": "download", "fraction": float("nan")})[2] == -1
    assert msu.working_lines({"stage": "download", "fraction": 3})[0] == "Getting the speaker model · 100%"
    # (only the stage "download" says so: every other stage keeps the words of the pass)
    assert msu.working_lines({"stage": "reading", "fraction": 0.5})[0] == "Identifying speakers · 50%"
    assert msu.working_lines({"stage": "reading"})[0] == "Identifying speakers…"


def test_needs_names():
    base = {"transcript": YOU_THEM, "meta": {}, "can_name_call": True}
    assert msu.needs_names(base) is True
    assert msu.needs_names(dict(base, transcript=NAMED)) is False
    assert msu.needs_names(dict(base, transcript="You: Hi.")) is False
    assert msu.needs_names(dict(base, meta={"speakers_at": "2026-10-30 16:40:00"})) is False
    assert msu.needs_names(dict(base, can_name_call=False, meta={"transcribed_with": "cloud"}, parts=["a.wav"])) is False
    assert msu.needs_names(dict(base, can_name_call=False, parts=["a.wav"])) is True       # a recording to read again
    assert msu.needs_names(dict(base, can_name_call=False)) is False                       # nothing to work from
    assert msu.needs_names(dict(base, can_name_call=False, parts=["a.wav"], recording_cut_short=True)) is False
    assert msu.needs_names(None) is False and msu.needs_names({}) is False
    assert msu.needs_names({"transcript": YOU_THEM}) is True                               # (an older page's data)
