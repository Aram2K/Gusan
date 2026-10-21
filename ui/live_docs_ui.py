"""The Qt side of the Live Assistant's documents, shared by the Live Assistant page
(ui/pages/live_documents.py) and the floating card (ui/live_assist.py).

The logic is Qt-free (live_docs.py, doc_reader.py); this module is only glue:

    bridge().changed            one signal: the list or a switch changed somewhere, so
                                the page's rows and the card's chip stay the same
    AddJob(paths)               reads files one after the other on ONE worker thread
                                (never the GUI thread), tells the GUI after each, and
                                can be cancelled; it never raises
    dropped_paths(mime)         the local files of a drag (a pick or a drop), plain paths
    ask_cloud(parent, app, ...) the one-time "use your documents with the cloud?" question
    set_choice(app, yes)        remember the answer (config ``live_docs_cloud_ok``)

Nothing here opens, runs or follows a file: a path is only handed to doc_reader, which
reads it once through a size-capped stream.
"""
import logging
import os
import threading

from PySide6.QtCore import QObject, Signal

import doc_reader
import live_docs

logger = logging.getLogger("transcribe")

MAX_BATCH = 100                 # files taken from one pick or one drop


class DocsBridge(QObject):
    """One per app: "the documents changed" (an add, a remove, a switch, the cloud
    answer). Emitted on the GUI thread."""
    changed = Signal()


_bridge = None


def bridge():
    """The app's DocsBridge (made on first use, on the GUI thread)."""
    global _bridge
    if _bridge is None:
        _bridge = DocsBridge()
    return _bridge


# ── what a drag carries ──
def dropped_paths(mime):
    """(paths, more): the local files a drag or a pick carries, at most MAX_BATCH, in
    order and without repeats. ``more`` is True when there were still more. A web link or
    any other kind of drag gives nothing: only files on a disk are ever read."""
    paths, seen, more = [], set(), False
    try:
        urls = list(mime.urls()) if mime is not None and mime.hasUrls() else []
    except Exception:
        return [], False
    for url in urls:
        try:
            if not url.isLocalFile() or url.host().lower() not in ("", "localhost"):
                # (a file:// link to another computer - \\server\share on Windows - is never
                # followed: reading it would open a network connection, and a web page can
                # hand a drag such a link. "Add documents" still takes a file from a share.)
                continue
            path = url.toLocalFile()
            if os.name == "nt" and path.replace("\\", "/").startswith("//"):
                continue                # the same thing spelled file:////server/share/x
        except Exception:
            continue
        if not path or path in seen:
            continue
        if len(paths) >= MAX_BATCH:
            more = True
            break
        seen.add(path)
        paths.append(path)
    return paths, more


def wants_files(mime):
    """True when a drag carries at least one local file."""
    return bool(dropped_paths(mime)[0])


# ── reading files off the GUI thread ──
class AddJob(QObject):
    """Reads ``paths`` one after the other on a worker thread and keeps what each holds
    (live_docs.DocStore.add_file: size-capped read, hard budgets, never raises).

    Signals (emitted from the worker, delivered on the GUI thread):
        started(name, index, total)   a file is about to be read (name is plain text)
        added(AddResult)              one file is done: ``ok`` and its DocInfo, or why not
        finished(results, cancelled)  all done (or stopped): the AddResults so far

    Keep a Python reference until ``finished`` has been handled."""
    started = Signal(str, int, int)
    added = Signal(object)
    finished = Signal(object, bool)

    def __init__(self, paths, store=None, parent=None):
        super().__init__(parent)
        self._paths = [os.fspath(p) for p in (paths or ())]
        self._store = store
        self._cancel = threading.Event()
        self._thread = None
        self._done = False

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="gusan-docs-add", daemon=True)
            self._thread.start()
        return self

    def cancel(self):
        """Stop before the next file, and ask the file being read to stop."""
        self._cancel.set()

    def is_cancelled(self):
        return self._cancel.is_set()

    def is_running(self):
        return self._thread is not None and not self._done

    def total(self):
        return len(self._paths)

    def _run(self):
        results = []
        try:
            store = self._store or live_docs.get_store()
            total = len(self._paths)
            for i, path in enumerate(self._paths, 1):
                if self._cancel.is_set():
                    break
                self.started.emit(_shown_name(path), i, total)
                try:
                    result = store.add_file(path, self._cancel.is_set)
                except Exception:
                    logger.debug("adding a document failed", exc_info=True)
                    result = live_docs.AddResult(False, None, _shown_name(path), "error",
                                                 doc_reader.MESSAGES["error"], "")
                results.append(result)
                self.added.emit(result)
        except Exception:               # (KeyboardInterrupt / SystemExit are not swallowed)
            logger.debug("document worker failed", exc_info=True)
        finally:                        # ...but the page is always told the batch is over
            self._done = True
            self.finished.emit(results, self._cancel.is_set())


class AddQueue(QObject):
    """A queue of files to read, run as one AddJob after another (a pick or a drop that
    arrives while one is being read waits its turn). Both the page and the card use it, each
    with its own queue.

    Signals, all on the GUI thread: started(name, index, total), added(AddResult) and
    batch_done(results, cancelled) - the AddJob's, one batch at a time."""
    started = Signal(str, int, int)
    added = Signal(object)
    batch_done = Signal(object, bool)

    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        self._store = store
        self._job = None
        self._pending = []

    def add(self, paths):
        """Queue these files (at most MAX_BATCH) and start reading. Returns how many."""
        paths = [os.fspath(p) for p in (paths or ()) if p][:MAX_BATCH]
        self._pending.extend(paths)
        self._start()
        return len(paths)

    def is_busy(self):
        return self._job is not None

    def is_cancelled(self):
        return self._job is not None and self._job.is_cancelled()

    def cancel(self):
        """Stop before the next file, ask the one being read to stop, drop the waiting ones."""
        self._pending.clear()
        if self._job is not None:
            self._job.cancel()

    def _start(self):
        if self._job is not None or not self._pending:
            return
        batch, self._pending = self._pending, []
        job = AddJob(batch, store=self._store)
        self._job = job
        job.started.connect(self.started)
        job.added.connect(self.added)
        job.finished.connect(self._on_finished)
        job.start()

    def _on_finished(self, results, cancelled):
        self._job = None
        self.batch_done.emit(results, cancelled)
        self._start()


def _shown_name(path):
    try:
        return doc_reader.safe_name(os.path.basename(os.fspath(path)))
    except Exception:
        return "Document"


# ── the cloud question ──
def set_choice(app, yes, kind=None):
    """Remember the person's answer (config ``live_docs_cloud_ok``) and which kind of
    cloud route a yes was given for (``live_docs_cloud_route``: "managed" = Gusan Cloud,
    "cloud" = their own key; the question names it): through the controller's set_setting
    (applies at once, saved a moment later, the page hears of it), else straight into cfg."""
    yes = bool(yes)
    route = kind if yes and kind in ("cloud", "managed") else ""
    ok = False
    cfg = getattr(app, "cfg", None)
    setter = getattr(app, "set_setting", None)
    if callable(setter):
        writes = [(live_docs.CLOUD_CHOICE_KEY, yes)]
        if not isinstance(cfg, dict) or (cfg.get(live_docs.CLOUD_ROUTE_KEY) or "") != route:
            writes.insert(0, (live_docs.CLOUD_ROUTE_KEY, route))      # (first: the page reads both)
        for key, value in writes:
            try:
                done = bool(setter(key, value))
                ok = done if key == live_docs.CLOUD_CHOICE_KEY else ok
            except Exception:
                logger.debug("set_setting(%s) failed", key, exc_info=True)
    if isinstance(cfg, dict) and (cfg.get(live_docs.CLOUD_CHOICE_KEY) is not yes
                                  or (cfg.get(live_docs.CLOUD_ROUTE_KEY) or "") != route):
        cfg[live_docs.CLOUD_ROUTE_KEY] = route           # the app's own write didn't take
        cfg[live_docs.CLOUD_CHOICE_KEY] = yes
        for name in ("schedule_config_save", "save_config"):
            save = getattr(app, name, None)
            if callable(save):
                try:
                    save()
                except Exception:
                    logger.debug("%s failed", name, exc_info=True)
                break
    bridge().changed.emit()
    return ok or (isinstance(cfg, dict) and cfg.get(live_docs.CLOUD_CHOICE_KEY) is yes)


def ask_cloud(parent, app, managed=True, private=False):
    """Ask once - in plain words - whether the few parts of the documents that match a
    question may go to the cloud AI that writes the answer. True = yes. The answer is
    remembered (set_choice); Esc is a no. ``private``: the question is shown from the
    floating card while it is hidden from screen shares, so it is hidden from them too."""
    from ui.components import mini_prompt
    title, body, yes, no = live_docs.cloud_prompt(managed)
    try:
        if any(d.picture and d.active for d in live_docs.get_store().list()):
            body += " A picture you added is sent whole, not in parts."   # (it can't be cut)
    except Exception:
        logger.debug("the documents question: pictures not counted", exc_info=True)
    kw = {"private": True} if private else {}
    try:
        answer = bool(mini_prompt.ask(parent, title, body, yes, no, icon="file", **kw))
    except Exception:
        logger.warning("the documents question failed", exc_info=True)
        return False                    # nothing is sent, and nothing is remembered
    set_choice(app, answer, "managed" if managed else "cloud")
    return answer
