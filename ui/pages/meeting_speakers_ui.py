"""Identify speakers on the Meetings page (the saved meeting's notes view).

Three small pieces, kept out of meeting_views.py so that file stays as it was:

  * the "Identify speakers" entry in the ⋯ menu (install),
  * ONE calm line at the top of the Transcript tab when the meeting has a
    recording but no speaker labels, with the same action as a link (after_render)
    - not when the recording ends before the transcript does (recording_cut_short),
  * the banner while it runs, with Cancel (busy_banner).

The work itself is MeetingSession.identify_speakers (off the GUI thread, with
progress in ``status`` and a cancel); the page reloads the meeting when the
session reports ``finished``.

A fourth piece belongs to the names that come after a meeting's notes: the notes
are written at once ("You" / "Them") and the session then tells the people on the
call apart in the background (MeetingSession.speakers_state / speakers_progress). The
meeting's notes view carries one calm banner meanwhile (working_banner), and it is
honest about what is happening:

  * waiting: "Speaker names are next", with what it waits for (your dictation, your
    recording, a file conversion, the notes, another meeting) - never "Identifying";
  * working: "Identifying speakers", first "Getting ready", then the real percentage, a
    bar and the time left, with Cancel (the very first time, "Getting the speaker model" with the
    real percentage of the download - Cancel works there too);
  * stopping: "Stopping..." from Cancel until the worker has really stopped.

The banner looks at the pass again every second (tick): its words follow in place, and
a pass that is gone reloads the meeting by itself - a lost signal can not leave a banner
behind. When it has ended the page reloads the meeting and says in one line what came
of it (MeetingsPage._on_speakers_changed, say_result: "Found 2 speakers on the call.").

A meeting whose "Them" was never told apart - the pass was cancelled, gave up, failed,
or the app was closed during it (nothing starts again at launch) - gets "Speakers aren't
told apart yet" with an Identify speakers button on the Notes tab (untold_banner,
needs_names) and keeps the suggestion line on the Transcript tab (call_untold).
"""
import html as html_mod
import logging
import re

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction

import meeting_speakers
import meeting_store
import speaker_labels
from ui import theme
from ui.components import icons

logger = logging.getLogger("transcribe")

LINK = "gusan:identify-speakers"
MENU_TEXT = "Identify speakers"
MIN_SECONDS = 5            # a shorter recording has nobody to tell apart
_THEM_RE = re.compile(r"(?m)^\s*Them:\s")
_SPEAKER_RE = re.compile(r"(?m)^\s*Speaker\s+(?:\d+|\?):\s")


def has_recording(data):
    return bool((data or {}).get("parts"))


def call_untold(data):
    """The transcript tells "You" and "Them" but the people on the call were never
    told apart: "Them" lines, no "Speaker N" line, and no pass has reached its end
    (``speakers_at``) or said why not (a speaker notice)."""
    data = data or {}
    text = data.get("transcript") or ""
    if not _THEM_RE.search(text) or _SPEAKER_RE.search(text):
        return False
    meta = data.get("meta") or {}
    if meta.get("speakers_at"):
        return False
    notices = meta.get("notices") if isinstance(meta.get("notices"), list) else []
    return not any(meeting_speakers.is_speaker_notice(n) for n in notices)


def recording_cut_short(data):
    """The saved recording ends before the transcript does (a meeting past the
    ~2-hour cap): read_meeting works it out off the window's thread, meta alone
    says it otherwise (meeting_store.recording_cut_short)."""
    data = data or {}
    return bool(data.get("recording_cut_short")) or meeting_store.recording_cut_short(data.get("meta"))


def wants_hint(data):
    """Show the one-line suggestion in the Transcript tab: a recording that is
    long enough, a transcript without speaker labels (or none at all), or one where
    the call is still just "Them" - never when the recording ends before the
    transcript does (labels from it would cut the transcript; Identify speakers
    refuses it)."""
    data = data or {}
    if not has_recording(data):
        return False
    try:
        seconds = int(data.get("duration_sec") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if 0 < seconds < MIN_SECONDS:
        return False
    text = data.get("transcript") or ""
    if text.strip() and recording_cut_short(data):
        return False
    if (data.get("meta") or {}).get("transcribed_with") == "cloud":
        # Its words came from a cloud engine (a Live Assistant session): Identify speakers would
        # not read the recording again (that would replace them), so the one thing it can still
        # do is tell the people on the call apart - offered only while that is undone.
        return call_untold(data)
    return (not speaker_labels.has_labels(text)) or call_untold(data)


def hint_html(data):
    """The suggestion as a paragraph for the Transcript tab ("" when not wanted)."""
    if not wants_hint(data):
        return ""
    muted = theme.qss_color("muted")
    link = theme.qss_color("blue-tx")
    if not (data.get("transcript") or "").strip():
        text = "Only the recording was saved."
        action = "Read it and identify speakers"
    elif call_untold(data):
        text = "The people on the call aren't told apart yet."
        action = "Identify speakers"
    else:
        text = "Speaker labels aren't in this transcript."
        action = "Identify speakers"
    return ("<p style='color:%s; margin:0 0 14px 0'>%s <a href='%s' style='color:%s'>%s</a></p>"
            % (muted, html_mod.escape(text), LINK, link, html_mod.escape(action)))


def install(view):
    """Add "Identify speakers" to the ⋯ menu (before Rename) and let the
    Transcript tab's suggestion link work. Called once, from the toolbar build."""
    action = QAction(icons.icon("mic", 16, "muted"), MENU_TEXT, view.more_menu)
    view.more_menu.insertAction(view.act_rename, action)
    view.act_identify = action
    action.triggered.connect(lambda _checked=False: identify(view))
    view.more_menu.aboutToShow.connect(lambda: sync(view))
    view.transcript.setOpenLinks(False)
    view.transcript.anchorClicked.connect(lambda url: _clicked(view, url))
    sync(view)
    return action


def sync(view):
    """The menu entry is available for a meeting with a recording while the
    session is free (nothing is being recorded or written)."""
    action = getattr(view, "act_identify", None)
    if action is None:
        return
    s = view.page.session()
    busy = bool(s is not None and getattr(s, "is_busy", False))
    action.setEnabled(has_recording(getattr(view, "data", None)) and not busy)


def _clicked(view, url):
    try:
        text = url.toString()
    except Exception:
        text = str(url)
    if text == LINK:
        identify(view)


def identify(view):
    """Start Identify speakers for the shown meeting."""
    folder = view.folder
    if not folder:
        return
    s = view.page.session(create=True)
    if s is None or not callable(getattr(s, "identify_speakers", None)):
        view.flash("Speaker labels can't be added right now.", "amber")
        return
    try:
        ok, _code, message = s.identify_speakers(folder)
    except Exception:
        logger.warning("Identify speakers failed to start", exc_info=True)
        ok, message = False, "Speaker labels can't be added right now."
    if ok:
        view.render()                       # the banner says what is happening
    else:
        view.flash(message or "Speaker labels can't be added right now.", "amber", ms=6000)


def cancel(view):
    s = view.page.session()
    try:
        if s is not None and s.cancel_identify():
            view.flash("Stopping…", "ok")
    except Exception:
        logger.debug("cancel_identify failed", exc_info=True)


def busy_banner(view):
    """The banner of a meeting the session is working on: Identify speakers
    (with Cancel), finishing an interrupted meeting, or writing the notes."""
    s = view.page.session()
    if bool(getattr(s, "identifying", False)):
        cloud = ((getattr(view, "data", None) or {}).get("meta") or {}).get("transcribed_with") == "cloud"
        if cloud:
            # (its words came from a cloud engine: the recording is never read again for them)
            text = ("Gusan is telling the people on the call apart. This takes a few minutes for a long "
                    "meeting - you can leave this page.")
        else:
            text = ("It reads the recording again to tell who spoke. This takes a few minutes for a long "
                    "meeting - you can leave this page.")
        return view._banner(getattr(s, "busy_label", "Identifying speakers…"), text,
                            "blue", "Cancel", lambda: cancel(view))
    label = getattr(s, "busy_label", "") or "Writing notes…"
    return view._banner(label, "The notes show up here when they're ready.", "blue")


def followup_state(view):
    """"" / "waiting" / "running": the background speaker pass of the meeting shown
    (the session's speakers_state; nothing for a session that has none)."""
    try:
        s = view.page.session()
        fn = getattr(s, "speakers_state", None)
        return str(fn(view.folder) or "") if callable(fn) and view.folder else ""
    except Exception:
        logger.debug("speakers_state failed", exc_info=True)
        return ""


def snapshot(view):
    """The background pass of the meeting shown, as one dict (the session's speakers_progress:
    state, what it waits for, stage, progress, time left, stopping) - or, for a session that can
    only tell the state, the state alone."""
    try:
        s = view.page.session()
        fn = getattr(s, "speakers_progress", None)
        if callable(fn) and view.folder:
            snap = fn(view.folder)
            if isinstance(snap, dict) and snap:
                return snap
    except Exception:
        logger.debug("speakers_progress failed", exc_info=True)
    return {"state": followup_state(view)}


def cancel_followup(view):
    """Cancel on the working banner. A pass that is waiting is dropped at once; one that is working
    needs a moment to stop (the banner says "Stopping…" until it has); either way the page says
    "Stopped." when it is over and offers Identify speakers again (untold_banner)."""
    s = view.page.session()
    try:
        if s is not None and s.cancel_speaker_followup(view.folder):
            view.render_keeping_view()                      # "Stopping..." - or, when it is over already, the offer
            if not followup_state(view):
                say_result(view, view.folder, default=(meeting_speakers.LINE_STOPPED, "ok"))
    except Exception:
        logger.debug("cancel_speaker_followup failed", exc_info=True)


# ── the words of the working banner (pure: tested without a window) ──────────

TITLE_WAITING = "Speaker names are next"
TITLE_WORKING = "Identifying speakers…"
TITLE_PERCENT = "Identifying speakers · %d%%"
TITLE_STOPPING = "Stopping…"
TITLE_DOWNLOAD = "Getting the speaker model…"
TITLE_DOWNLOAD_PERCENT = "Getting the speaker model · %d%%"
TEXT_DOWNLOAD = "It is downloaded once."
TITLE_UNTOLD = "Speakers aren't told apart yet"
TEXT_UNTOLD = "The people on the call show as “Them”."
NOTES_READY = "The notes are ready."
THEM_BECOMES = "“Them” becomes Speaker 1, 2 and so on."
TICK_MS = 1000                  # how often a working banner looks at the pass again
RESULT_FLASH_MS = 6000          # how long the line about how it ended stays up
BAR_STEPS = 1000                # the progress bar's range (NotesView._banner uses the same)
_WAITING = {
    "recording": "Waiting for your recording to finish.",
    "dictation": "Waiting for your dictation to finish.",
    "file": "Waiting for the file conversion to finish.",
    "notes": "Waiting for the notes to finish.",
    "queue": "Waiting for another meeting to finish.",
    "busy": "Waiting for your other work to finish.",
}


def waiting_text(reason):
    """What a queued pass is waiting for, in words (a reason this file does not know is "just about to
    start" - nothing that comes from outside ever becomes copy)."""
    return _WAITING.get(str(reason or ""), "Starting in a moment.")


def eta_text(seconds):
    """The time left, in words: "About 2 min left." ("Less than a minute left." under 45 s); "" when
    there is no estimate (None, not a number, negative)."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    if s != s or s < 0.0:
        return ""
    if s < 45.0:
        return "Less than a minute left."
    return "About %d min left." % max(1, int(s / 60.0 + 0.5))


def working_lines(snap):
    """(title, line, progress) of a pass that is working: not started yet = "Getting ready." with an endless
    bar (progress -1); then the real percentage in the title, "Them" becomes ... and the time left, with
    the bar at ``progress`` (0..1)."""
    frac = snap.get("fraction")
    try:
        frac = None if frac is None else float(frac)
    except (TypeError, ValueError):
        frac = None
    if frac is not None:
        frac = None if frac != frac else max(0.0, min(1.0, frac))        # (not a number: not known)
    if snap.get("stage") == "download":
        # the speaker model comes down once, on the very first pass: say so, with the real percentage
        line = "%s %s" % (NOTES_READY, TEXT_DOWNLOAD)
        if frac is None:
            return TITLE_DOWNLOAD, line, -1
        return TITLE_DOWNLOAD_PERCENT % int(frac * 100), line, frac
    if frac is None:
        return TITLE_WORKING, "%s Getting ready." % NOTES_READY, -1
    line = "%s %s" % (NOTES_READY, THEM_BECOMES)
    eta = eta_text(snap.get("eta_s"))
    if eta:
        line += " " + eta
    return TITLE_PERCENT % int(frac * 100), line, frac


def _group(snap):
    """"" (no pass), "waiting", "working" (starting or running: the one banner with a bar, updated in
    place) or "stopping" (Cancel was pressed, the worker has not ended)."""
    if not str((snap or {}).get("state") or ""):
        return ""
    if snap.get("stopping"):
        return "stopping"
    return "waiting" if snap.get("state") == "waiting" else "working"


def needs_names(data):
    """The meeting still tells "You" and "Them" and nothing has told the people on the call apart: "Them"
    lines, no "Speaker N" line, no pass that reached its end (``speakers_at``) - and it can be done: the
    saved segments and the call's sound are there (``can_name_call``, worked out when the meeting is
    read), or it is a meeting whose recording can be read again. This is also what a restart leaves: the
    pass is never started again by itself at launch, so the page says so and offers it."""
    data = data or {}
    text = data.get("transcript") or ""
    if not _THEM_RE.search(text) or _SPEAKER_RE.search(text):
        return False
    meta = data.get("meta") or {}
    if meta.get("speakers_at"):
        return False
    if data.get("can_name_call", True):
        return True
    if meta.get("transcribed_with") == "cloud":
        return False                       # its words are kept; with nothing saved to work from there is nothing to do
    return has_recording(data) and not recording_cut_short(data)


def untold_banner(view):
    """"Speakers aren't told apart yet" - a calm banner with the one action, for a meeting whose pass was
    cancelled, gave up, failed or was never started again after a restart."""
    fr = view._banner(TITLE_UNTOLD, TEXT_UNTOLD, "neutral", "Identify speakers", lambda: identify(view))
    fr.group = "untold"
    return fr


def working_banner(view):
    """The banner of a meeting whose speaker names are being added in the background, after its notes
    (None when nothing is to be said). Waiting says what it waits for; working shows the real percentage,
    a bar and the time left, with Cancel; stopping says so. The notes are already there. Without a pass
    queued or running, a meeting that still tells "Them" gets the calm "not told apart yet" banner."""
    snap = snapshot(view)
    group = _group(snap)
    if not group:
        s = view.page.session()
        if callable(getattr(s, "identify_speakers", None)) and needs_names(getattr(view, "data", None)):
            return untold_banner(view)
        return None
    cancel_slot = lambda: cancel_followup(view)             # noqa: E731
    if group == "waiting":
        fr = view._banner(TITLE_WAITING, "%s %s" % (NOTES_READY, waiting_text(snap.get("waiting_for"))),
                          "blue", "Cancel", cancel_slot)
    elif group == "stopping":
        fr = view._banner(TITLE_STOPPING, "", "blue")
    else:
        title, line, progress = working_lines(snap)
        fr = view._banner(title, line, "blue", "Cancel", cancel_slot, progress=progress)
    fr.group = group
    timer = QTimer(fr)                                       # (a child of the banner: gone with it)
    timer.setInterval(TICK_MS)
    timer.timeout.connect(lambda: tick(view, fr))
    timer.start()
    fr.timer = timer
    return fr


def tick(view, frame):
    """A working banner looks at the pass again, once a second: the words and the bar follow it in
    place; a change of kind (waiting -> working, working -> stopping) draws the page once more; and a
    pass that is gone - whatever became of the signal that should have said so - reloads the meeting,
    so a banner can never stay behind."""
    try:
        if getattr(view, "banner", None) is not frame or frame.isHidden():
            frame.timer.stop()                              # a newer render replaced this banner (or took it off)
            return
        snap = snapshot(view)
        group = _group(snap)
        if not group:
            frame.timer.stop()
            folder = view.folder
            view.reload(keep_view=True)
            say_result(view, folder)
            return
        if group != getattr(frame, "group", ""):
            frame.timer.stop()
            view.render_keeping_view()
            return
        _update(frame, group, snap)
    except RuntimeError:
        pass                                                # the widget went away between two events
    except Exception:
        logger.debug("speaker banner tick failed", exc_info=True)


def _update(frame, group, snap):
    """Put the pass's words and progress into a banner that is already on the page."""
    if group == "waiting":
        title, line, progress = TITLE_WAITING, "%s %s" % (NOTES_READY, waiting_text(snap.get("waiting_for"))), None
    else:
        title, line, progress = working_lines(snap)
    if frame.title_label.text() != title:
        frame.title_label.setText(title)
        frame.setAccessibleName(title)
    if frame.text_label is not None and frame.text_label.text() != line:
        frame.text_label.setText(line)
    bar = getattr(frame, "bar", None)
    if bar is not None and progress is not None:
        if progress < 0:
            if bar.maximum() != 0:
                bar.setRange(0, 0)
        else:
            if bar.maximum() != BAR_STEPS:
                bar.setRange(0, BAR_STEPS)
            bar.setValue(int(progress * BAR_STEPS))


def say_line(view, line):
    """``line`` - (text, tone) from the session, or None - for a few seconds, at the top right of the page
    (the page's note: it has room there in a narrow window, where a line beside the toolbar's buttons would
    push the page wider than the window)."""
    if line:
        text, tone = line
        say = getattr(view, "note", None) or view.flash
        say(text, tone, ms=RESULT_FLASH_MS)


def say_result(view, folder, default=None):
    """The one line about how a pass ended - "Found 2 speakers on the call." - in the page's note. Given once
    by the session (it keeps nothing); ``default`` is said when it has none."""
    s = view.page.session()
    line = None
    fn = getattr(s, "speakers_result", None)
    if callable(fn):
        try:
            line = fn(folder)
        except Exception:
            logger.debug("speakers_result failed", exc_info=True)
    say_line(view, line if line is not None else default)


def after_render(view):
    """End of NotesView.render(): the menu entry follows the meeting shown, and
    the suggestion goes on top of the Transcript tab (not while the names are
    being added: the banner says so)."""
    try:
        sync(view)
        if followup_state(view):
            return
        hint = hint_html(view.data)
        if not hint:
            return
        from ui.pages import meeting_views
        view.transcript.setHtml(hint + meeting_views.transcript_html(view.data.get("transcript") or ""))
    except Exception:
        logger.debug("speaker suggestion failed", exc_info=True)
