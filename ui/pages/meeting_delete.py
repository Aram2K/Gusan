"""Deleting meetings from a list (Meetings, and the Live Assistant's past sessions): the words of the
question and the one controller both lists use.

    deleter = MeetingDeleter(app, host_widget, release=notes_view.release_folder, dock=page)
    # (dock: the "Deleted 3 meetings - Undo" toast floats over the bottom of ``page``, so it is in sight
    #  however far the list is scrolled; without dock the caller puts deleter.bar in a layout)
    deleter.removed.connect(row_gone)                    # a folder left the list (it is in the trash)
    deleter.restored.connect(reload_the_list)            # an Undo put meetings back
    deleter.ask(meetings, source="row")                  # themed confirm, then delete

What happens (see meeting_trash.py for the safety rules and Undo):
  * ``blocked(m)`` says why a meeting can't go ("Recording now" / "Finishing up"), "" when it can:
    the recording or its notes are being made, or the names pass (speaker labels) is queued or running.
  * ``ask`` confirms once for the whole batch - the name, how long, what is removed - with a red
    Delete and the safe Cancel as the default, then ``delete`` moves each folder to the trash on a
    worker thread, one by one: the row dims to "Deleting…" (``busy``), then leaves (``removed``).
    A folder that a program still has open (the player letting go of the recording, a virus scanner)
    is tried again twice, a quarter of a second apart; one that can't go (no permission, still open)
    stays, the rest go, and the bar says so in plain words.
  * ``is_gone(folder)``: a list read from the disk before a move and shown after it filters its rows
    through this, so a deleted meeting never comes back on screen until an Undo has brought it back.
  * the bar shows "Deleted 3 meetings - Undo" for 10 seconds (merging quick deletes); Undo moves the
    folders back and ``restored`` fires. When it closes - or the app quits, or the next start finds
    something older than a minute - the folders are deleted for good, on a worker thread.
  * telemetry: ``meeting_deleted`` with a count and where it came from, never a name.
"""

import logging
import os
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

import meeting_trash
from ui.components import mini_prompt
from ui.components.row_actions import UndoBar
from ui.pages import meeting_views as views

logger = logging.getLogger("transcribe")

UNDO_MS = meeting_trash.UNDO_SECONDS * 1000
ERROR_MS = 12000                  # an error line stays a bit longer than the undo line
RETRIES = 2                       # a folder a program still has open (the player that is letting go of the
RETRY_WAIT_S = 0.25               # recording, a virus scanner reading it) is tried again this often
GONE_S = 180                      # a list read that began before a delete may still hold the meeting this long
TITLE_MAX = 80                    # a name longer than this is cut (with …) in the question and the bar

REASON_RECORDING = "Recording now"
REASON_FINISHING = "Finishing up"
DETAIL_ONE = "Its notes, transcript, recording and speaker names are removed from this computer."
DETAIL_MANY = "Their notes, transcripts, recordings and speaker names are removed from this computer."


def meeting_title(m):
    return " ".join(str((m or {}).get("title") or "").split()) or "Untitled meeting"


def short_title(m, limit=TITLE_MAX):
    """The name as the question and the bar say it: a long one (a calendar invite's whole subject) is cut."""
    t = meeting_title(m)
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def folder_key(folder):
    try:
        return os.path.normcase(os.path.abspath(str(folder)))
    except Exception:
        return str(folder)


def _plural(n):
    return "%d meeting%s" % (n, "" if n == 1 else "s")


def words(items):
    """(title, text, detail) of the confirmation for these meeting dicts."""
    items = list(items or [])
    if len(items) == 1:
        m = items[0]
        started = m.get("started") or m.get("timestamp")
        bits = [views.fmt_when(started, with_time=False) if started else "",
                views.fmt_duration(m.get("duration_sec")) if m.get("duration_sec") else ""]
        return "Delete “%s”?" % short_title(m), " · ".join(b for b in bits if b), DETAIL_ONE
    shown = ["“%s”" % short_title(m, 40) for m in items[:3]]
    if len(items) > 3:
        listed = "%s and %d more" % (", ".join(shown), len(items) - 3)
    elif len(shown) > 1:
        listed = ", ".join(shown[:-1]) + " and " + shown[-1]
    else:
        listed = shown[0] if shown else ""
    total = sum(int(m.get("duration_sec") or 0) for m in items)
    text = DETAIL_MANY + (" Together %s." % views.fmt_duration(total) if total else "")
    return "Delete %s?" % _plural(len(items)), text, listed


def confirm_delete(parent, items):
    """The one confirmation (a themed card, never a system box). True = delete."""
    title, text, detail = words(items)
    return bool(mini_prompt.confirm(parent, title, text, "Delete", "Cancel", danger=True,
                                    detail=detail or None, icon="trash"))


def same_folder(a, b):
    return folder_key(a) == folder_key(b)


class MeetingDeleter(QObject):
    """See the module notes. ``host`` is the widget the confirmation opens over; ``release(folder)``
    (optional) lets go of a recording a player still has open before its folder moves."""

    removed = Signal(str)               # a folder left the list
    busy = Signal(str, bool)            # a folder is being deleted (True) / was left alone (False)
    restored = Signal(int)              # an Undo brought this many back
    _item = Signal(str, object, str)
    _all_done = Signal()

    def __init__(self, app, host, release=None, parent=None, dock=None):
        super().__init__(parent or host)
        self.app = app
        self.host = host
        self._release = release
        self._deleting = {}             # folder -> title, while it is on its way
        self._gone = {}                 # folder_key -> when it went to the trash (until an Undo brings it back)
        self._errors = []               # (title, code) of this round
        self._pending = []              # Trashed records inside the undo window
        self._moved = 0                 # moved in the round that is running
        self._jobs = 0
        self._source = "row"
        self.bar = UndoBar(host)
        if dock is not None:
            self.bar.dock(dock)                 # a toast at the bottom of the page, seen however far the list is scrolled
        self.bar.undo.connect(self.undo)
        self.bar.closed.connect(self.dismiss)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._item.connect(self._on_item)
        self._all_done.connect(self._on_done)
        try:
            qa = QApplication.instance()
            if qa is not None:
                qa.aboutToQuit.connect(self._on_quit)
        except Exception:
            logger.debug("no aboutToQuit", exc_info=True)

    # ── what can go ──
    def session(self):
        return getattr(self.app, "meeting_session", None)

    def blocked(self, m, names=True):
        """"" when this meeting can be deleted, else why not, in two words. ``names``: False when the
        caller has just stopped the names pass of this meeting (the open meeting's Delete does), so a
        pass that is still winding down is not a reason to refuse."""
        folder = (m or {}).get("dir")
        s = self.session()
        if s is None or not folder:
            return ""
        try:
            import meeting_store
            active = s.active_dir()
            status = meeting_store.meeting_status(m, active, getattr(s, "phase", "idle"))
        except Exception:
            status = ""
        if status == "recording":
            return REASON_RECORDING
        if status == "processing":
            return REASON_FINISHING
        fn = getattr(s, "speakers_state", None)
        if names and callable(fn):
            try:
                if fn(folder):
                    return REASON_FINISHING
            except Exception:
                logger.debug("speakers_state failed", exc_info=True)
        return ""

    def is_deleting(self, folder):
        return folder in self._deleting

    def is_gone(self, folder):
        """True for a folder this controller moved to the trash a moment ago (and that no Undo has
        brought back). A list that was read from the disk BEFORE the move, and shown AFTER it, must not
        put the meeting back on screen: it filters its rows through this."""
        t = self._gone.get(folder_key(folder)) if folder else None
        if t is None:
            return False
        if time.monotonic() - t > GONE_S:
            self._gone.pop(folder_key(folder), None)
            return False
        return True

    def deleting(self):
        return list(self._deleting)

    def pending(self):
        return list(self._pending)

    # ── delete ──
    def ask(self, meetings, source="row"):
        """Confirm once for ``meetings`` (dicts with dir / title / duration_sec / started), then delete
        them. False when it was cancelled or nothing could go."""
        items = [m for m in (meetings or []) if m and not self.is_deleting(m.get("dir"))]
        free = [m for m in items if not self.blocked(m)]
        if not free:
            if items:
                self._say_blocked(items)
            return False
        if not confirm_delete(self.host, free):
            return False
        return self.delete(free, source)

    def delete(self, meetings, source="row", names_stopped=False):
        """Move these meetings to the trash (no question: ``ask`` asks). True when work started.
        ``names_stopped``: the caller stopped the meetings' names passes first (the open meeting's
        Delete), so they do not block."""
        todo, skipped = [], []
        for m in meetings or []:
            folder = (m or {}).get("dir")
            if not folder or self.is_deleting(folder):
                continue
            (skipped if self.blocked(m, names=not names_stopped) else todo).append(m)
        if skipped and not todo:
            self._say_blocked(skipped)
            return False
        if not todo:
            return False
        self._source = source
        if self._jobs == 0:
            self._errors = []
            self._moved = 0
        for m in skipped:
            self._errors.append((short_title(m), "working"))
        for m in todo:
            folder = m["dir"]
            self._deleting[folder] = short_title(m)
            if callable(self._release):
                try:
                    self._release(folder)          # a loaded recording holds its file open
                except Exception:
                    logger.debug("could not release %s", folder, exc_info=True)
            self.busy.emit(folder, True)
        folders = [m["dir"] for m in todo]
        self._jobs += 1

        def work():
            patient = True
            for folder in folders:
                try:
                    self._item.emit(folder, self._move(folder, patient), "")
                except meeting_trash.TrashError as e:
                    if e.code in ("busy", "denied"):
                        patient = False                  # waited once: not again for every folder of a batch
                    self._item.emit(folder, None, e.code)
                except Exception:
                    logger.warning("deleting a meeting failed", exc_info=True)
                    self._item.emit(folder, None, "failed")
            self._all_done.emit()

        if views.SYNC_BG:
            work()
        else:
            threading.Thread(target=work, daemon=True, name="gusan-delete").start()
        return True

    @staticmethod
    def _move(folder, patient=True):
        """One folder into the trash (on the worker thread). A folder that a program still has open -
        the audio player that is just letting go of the recording, a virus scanner reading it - is
        tried again a moment later (twice); a real refusal ("denied", "outside"...) comes back at once."""
        tries = RETRIES + 1 if patient else 1
        for attempt in range(tries):
            try:
                return meeting_trash.move_to_trash(folder)
            except meeting_trash.TrashError as e:
                if e.code not in ("busy", "denied") or attempt == tries - 1:
                    raise
                time.sleep(RETRY_WAIT_S)

    def _on_item(self, folder, trashed, code):
        title = self._deleting.pop(folder, "") or "this meeting"
        if trashed is not None or code == "missing":
            self._gone[folder_key(folder)] = time.monotonic()
        if trashed is not None:
            self._pending.append(trashed)
            self._moved += 1
            self.removed.emit(folder)
        elif code == "missing":
            self.removed.emit(folder)              # already gone: the row just leaves
        else:
            self._errors.append((title, code))
            self.busy.emit(folder, False)

    def _on_done(self):
        self._jobs = max(0, self._jobs - 1)
        if self._jobs:
            return
        if self._moved:
            self._track(self._moved)
        self._show_bar()

    def _track(self, n):
        fn = getattr(self.app, "track", None)
        if callable(fn):
            try:
                fn("meeting_deleted", {"count": int(n), "from": self._source})
            except Exception:
                logger.debug("track failed", exc_info=True)

    # ── the bar ──
    def _error_text(self):
        if not self._errors:
            return ""
        title, code = self._errors[0]
        if code == "working":
            line = "“%s” is still being worked on. Try again in a moment." % title
        else:
            line = meeting_trash.plain(code, title)
        if len(self._errors) > 1:
            line += " %d more couldn't be deleted either." % (len(self._errors) - 1)
        return line

    def _show_bar(self):
        err = self._error_text()
        n = len(self._pending)
        if n:
            text = "Deleted %s." % _plural(n)
            if err:
                text += " " + err
            self.bar.undo_button.show()
            self.bar.set_message(text, "error" if err else "info")
            self._timer.start(UNDO_MS)
        elif err:
            self.bar.undo_button.hide()
            self.bar.set_message(err, "error")
            self._timer.start(ERROR_MS)
        else:
            self.bar.hide()
            return
        self.bar.show()

    def _say_blocked(self, items):
        self._errors = [(short_title(m), "working") for m in items]
        self._show_bar()

    def note_error(self, text):
        """A line in the bar's error look (the owner's own words)."""
        self.bar.undo_button.setVisible(bool(self._pending))
        self.bar.set_message(text, "error")
        self._timer.start(ERROR_MS)
        self.bar.show()

    # ── undo / purge ──
    def undo(self):
        """Bring every meeting of the undo window back."""
        items, self._pending = list(self._pending), []
        self._timer.stop()
        self.bar.hide()
        if not items:
            return
        failed = []

        def work():
            back = 0
            for t in items:
                try:
                    meeting_trash.restore(t)
                    self._gone.pop(folder_key(t.original), None)       # back on disk: lists may show it again
                    back += 1
                except meeting_trash.TrashError as e:
                    failed.append((t.name, e.code))
                except Exception:
                    logger.warning("undo failed", exc_info=True)
                    failed.append((t.name, "failed"))
            return back

        def done(back):
            self.restored.emit(int(back or 0))
            if failed:
                name, code = failed[0]
                self.note_error(meeting_trash.plain(code, name))

        self._run(work, done)

    def dismiss(self):
        """Close the bar now: what it could still bring back is deleted for good."""
        items, self._pending = list(self._pending), []
        self._timer.stop()
        self.bar.hide()
        if items:
            self._run(lambda: [meeting_trash.purge(t) for t in items], None)

    def _run(self, fn, done):
        runner = getattr(self, "_runner", None)
        if runner is None:
            runner = self._runner = views.BgRunner(self)
        runner.run(fn, (lambda res, err: done(res if err is None else 0)) if done else None)

    def _on_quit(self):
        """The app is closing: delete what is waiting, but never keep the quit waiting for it - the
        next start finishes (meeting_trash.purge_older_than)."""
        try:
            meeting_trash.purge_all(budget_s=2.0)
        except Exception:
            logger.debug("purge at quit failed", exc_info=True)
