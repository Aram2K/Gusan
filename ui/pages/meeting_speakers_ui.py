"""Identify speakers on the Meetings page (the saved meeting's notes view).

Three small pieces, kept out of meeting_views.py so that file stays as it was:

  * the "Identify speakers" entry in the ⋯ menu (install),
  * ONE calm line at the top of the Transcript tab when the meeting has a
    recording but no speaker labels, with the same action as a link (after_render),
  * the banner while it runs, with Cancel (busy_banner).

The work itself is MeetingSession.identify_speakers (off the GUI thread, with
progress in ``status`` and a cancel); the page reloads the meeting when the
session reports ``finished``.

A fourth piece belongs to the names that come after a meeting's notes: the notes
are written at once ("You" / "Them") and the session then tells the people on the
call apart in the background (MeetingSession.speakers_state). While that waits or
works, the meeting's notes view carries one calm banner with Cancel
(working_banner) and the suggestion line stays away; when it has ended the page
reloads the meeting (MeetingsPage._on_speakers_changed). A meeting whose "Them" was
never told apart - the pass was cancelled, interrupted too often, or failed - keeps
the suggestion line, so one click still does it (call_untold).
"""
import html as html_mod
import logging
import re

from PySide6.QtGui import QAction

import meeting_speakers
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


def wants_hint(data):
    """Show the one-line suggestion in the Transcript tab: a recording that is
    long enough, a transcript without speaker labels (or none at all), or one where
    the call is still just "Them"."""
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
        return view._banner(getattr(s, "busy_label", "Identifying speakers…"),
                            "It reads the recording again to tell who spoke. This takes a few "
                            "minutes for a long meeting - you can leave this page.",
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


def cancel_followup(view):
    """Cancel on the working banner: the meeting stays "You" and "Them"."""
    s = view.page.session()
    try:
        if s is not None and s.cancel_speaker_followup(view.folder):
            view.flash("Stopped. Speaker names can be added from ⋯ › Identify speakers.", "ok", ms=6000)
    except Exception:
        logger.debug("cancel_speaker_followup failed", exc_info=True)


def working_banner(view):
    """The banner of a meeting whose speaker names are being added in the
    background, after its notes (None when nothing is). The notes are already
    there; it says so, and Cancel leaves the meeting as it is."""
    state = followup_state(view)
    if state == "running":
        return view._banner("Identifying speakers…",
                            "The notes are ready. Gusan is telling the people on the call apart, so "
                            "“Them” becomes Speaker 1, 2 and so on. This takes a few minutes for a long "
                            "meeting - you can leave this page.",
                            "blue", "Cancel", lambda: cancel_followup(view))
    if state == "waiting":
        return view._banner("Speaker names are next",
                            "The notes are ready. Gusan adds who said what when you're not recording "
                            "or dictating.",
                            "blue", "Cancel", lambda: cancel_followup(view))
    return None


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
