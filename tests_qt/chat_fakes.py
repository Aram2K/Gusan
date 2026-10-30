"""Fakes shared by the "Ask about this meeting" tests (not a test file): a stand-in MeetingSession with the signals
the Meetings page listens to, and a meeting folder in a temp library."""
import json

from PySide6.QtCore import QObject, Signal


class _Sig(QObject):
    state_changed = Signal(str)
    chunk_text = Signal(str)
    live_line = Signal(str, str)
    recap = Signal(str)
    status = Signal(str)
    levels = Signal(float, float)
    finished = Signal(str, str, str)
    error = Signal(str, str)
    speakers_changed = Signal(str)
    transcript_saved = Signal(str)


class FakeSession:
    """Just enough of MeetingSession for the page: a phase, the folder being written, transcript_final."""
    _Sig = _Sig

    def __init__(self, phase="idle"):
        self.sig = self._Sig()
        for name in ("state_changed", "chunk_text", "live_line", "recap", "status", "levels", "finished", "error",
                     "speakers_changed", "transcript_saved"):
            setattr(self, name, getattr(self.sig, name))
        self.phase = phase
        self.title = "Live session 15:34"
        self.attendees = ""
        self.my_notes = ""
        self.transcript = ""
        self.last_error = ""
        self._live_summary_text = ""
        self.meeting_dir = None
        self.busy_label = "Writing notes\u2026"
        self.identifying = False
        self.final = True

    @property
    def is_busy(self):
        return self.phase in ("recording", "paused", "processing")

    def active_dir(self):
        return self.meeting_dir if self.is_busy else None

    def transcript_final(self, folder):
        return self.final

    def speakers_state(self, folder=None):
        return ""

    def elapsed_sec(self, now=None):
        return 75

    def live_lines(self):
        return []

    def snapshot(self):
        return {"phase": self.phase, "language": "fr", "audio_mode": "smart_meeting"}


TRANSCRIPT = "You: Hello, thank you for joining.\nThem: Of course. We cut the nightly run from six hours to ninety minutes."
NOTES = "## Summary\nA hiring call.\n\n## Key decisions\n- A second interview.\n"


def make_meeting(root, name="20261030_153400", title="Live session 15:34", transcript=TRANSCRIPT, notes=NOTES, chat=None,
                 **meta):
    d = root / name
    d.mkdir(exist_ok=True)
    started = "%s-%s-%s %s:%s:00" % (name[:4], name[4:6], name[6:8], name[9:11], name[11:13])
    data = {"title": title, "attendees": "Aram", "duration_sec": 3300, "started": started, "language": "fr",
            "notes_engine_label": "Gusan Cloud \u00b7 Mistral Small", "seen": True, "source": "live_assist"}
    data.update(meta)
    (d / "meta.json").write_text(json.dumps(data), encoding="utf-8")
    if transcript:
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")
    if notes:
        (d / "notes.md").write_text(notes, encoding="utf-8")
    if chat is not None:
        (d / "chat.json").write_text(json.dumps({"version": 1, "messages": chat}), encoding="utf-8")
    return d


