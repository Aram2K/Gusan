"""Home (Gusan 2.0): Ready + the real hotkey, quick tiles, one Recent list.

Mockup: v2/canvas/project/Home.dc.html (source v2/pages/core.py home()). Calm
Pro: plain PRO tags on the Pro tiles, no upsell of its own; Free and Pro look
the same here (the sidebar shows the plan).

    page = create(main_app)        # what ui/shell.py calls
    page.refresh(sync=True)        # tests / previews: load Recent now

What it reads (all optional - a missing service shows a calm state, never an
error card):
- cfg: hotkey, smart_actions_hotkey / live_assist_hotkey (the Smart Actions
  and Live Assistant tiles' keycaps; "" = "Set shortcut"), language, backend /
  whisper_model / managed_provider / privacy_mode (through engine_choice.runs_on).
- Smart Action and meeting rows show their Markdown as plain text
  (rich_text.to_plain) and copy it as HTML + plain text (ui.components.clipboard).
- Recent: history.load() + meeting_store.list_meetings() +
  transcript_store.list_transcripts(), read on a worker (api_keys.run_then);
  history's meeting entries are dropped when their audio_dir is a listed
  meeting (the meeting row stands for both).
- app.meeting_session (active_dir() + phase) for a meeting being recorded;
  its state_changed re-reads Recent (Recording -> Writing notes… -> Notes ready).
- app.calendar (calendar_service.CalendarService): "Next:" chip from
  next_meeting() when available and connected, updated on changed(list).
- app.downloads (model_manager.ModelDownloads): "Getting ready · 42%" while
  the speech model downloads.
- app.rename_note_due() / dismiss_rename_note(): the one-time "Transcribe is
  now Gusan" line for 1.x upgraders under the header - remembered once it has
  been on screen (it stays up for that run until ✕), never a modal.

Integration notes for Wave 4
----------------------------
* Navigation: rows and tiles open pages through the MainWindow (``window().open``)
  or, outside it, ``app.show_main(route, **opts)``. Options this page sends
  (the ones those pages' on_open already reads):
    dictation      entry=<history timestamp "YYYY-MM-DD HH:MM:SS">  (highlights the row)
    meetings       folder=<meeting folder path> (opens its notes), or
                   view="live" for the meeting being recorded
    audio_to_text  transcript_id=<transcript id (folder name)>
    smart_actions  (none)
* Refresh: the page re-reads Recent on every on_open and, while visible, when
  history.json / the meetings folder / the transcripts folder change (an
  os.stat every 3 s, nothing else on the GUI thread). A controller signal
  ``sig_history_changed`` (no args) is connected when it exists - emit it after
  a dictation / meeting / transcript is saved or history is cleared and the
  poll becomes a backup only. Service signals are connected to bound methods
  (dropped with the page) and again on on_open for a service created later.
* Smart Action rows: history.save_entry() does not record that a Smart Action
  ran, so every history entry reads "Dictation · EN". When main.py saves a
  Smart Action result, store the mode in the entry (``entry["action"] =
  "write_email" | "make_todo_list" | "translate" | "summarize" | "smart_auto"``);
  this page then shows "Smart Action · Email" with the wand icon and opens
  Smart Actions.
* Calendar: connect is done here (changed -> chip); nothing else needed.
"""

import logging
import os
from datetime import datetime

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QFontMetricsF, QGuiApplication, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel,
                               QLayout, QLineEdit, QSizePolicy, QVBoxLayout, QWidget)

import rich_text
from ui import theme
from ui.components import Button, Card, Chip, IconButton, Keycaps, PageHeader, ProTag, clipboard, icons, label
from ui.components.controls import keycap_labels

logger = logging.getLogger("transcribe")

# The page's clock (previews and tests pin it).
_now = datetime.now

HOME_LIMIT = 6          # Recent rows without a search (the mockup's six)
SEARCH_LIMIT = 40       # with a search
POLL_MS = 3000

LANG_NAMES = {
    "auto": "Detect language", "multi": "Mixed languages", "en": "English", "ar": "Arabic",
    "hy": "Armenian", "fr": "French", "de": "German", "ru": "Russian", "es": "Spanish",
}

ACTION_LABELS = {
    "write_email": "Email", "make_todo_list": "To-do list", "translate": "Translate",
    "summarize": "Summary", "polish": "Polish", "smart_auto": "", "meeting_notes": "Notes",
}

MEETING_STATUS_TEXT = {
    "notes_ready": "Notes ready", "notes_failed": "Transcript ready", "audio_only": "Recording saved",
    "recording": "Recording", "processing": "Writing notes…",
}

# Shown once to 1.x upgraders (app.rename_note_due / dismiss_rename_note):
# a quiet line under the header, never a modal.
RENAME_NOTE = "Transcribe is now Gusan. Same app — your settings, history and sign-in are kept."


# ── small helpers shared with the Dictation page ─────────────────────────────
def call(app, method, *args, **kwargs):
    """app.method(*args) when it exists; None (logged) when it is missing or fails."""
    fn = getattr(app, method, None)
    if not callable(fn):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("%s() failed", method, exc_info=True)
        return None


def is_pro(app):
    try:
        return bool(app.is_pro())
    except Exception:
        return False


def cfg_of(app):
    cfg = getattr(app, "cfg", None)
    return cfg if isinstance(cfg, dict) else {}


SMART_HOTKEY_DEFAULT = "alt+s"          # main.DEFAULT["smart_actions_hotkey"] (D2)


def smart_hotkey(app):
    """The Smart Actions shortcut; "" when none is set (it clashed at the upgrade)."""
    return str(cfg_of(app).get("smart_actions_hotkey", SMART_HOTKEY_DEFAULT) or "").strip()


LIVE_HOTKEY_DEFAULT = "ctrl+alt+a"      # main.DEFAULT["live_assist_hotkey"]


def live_hotkey(app):
    """The Live Assistant shortcut; "" when none is set."""
    return str(cfg_of(app).get("live_assist_hotkey", LIVE_HOTKEY_DEFAULT) or "").strip()


def go(widget, app, route, **opts):
    """Open ``route`` in the main window (or through app.show_main)."""
    win = widget.window() if widget is not None else None
    opener = getattr(win, "open", None)
    if callable(opener) and hasattr(win, "current_route"):
        try:
            opener(route, **opts)
            return True
        except Exception:
            logger.warning("open(%s) failed", route, exc_info=True)
            return False
    fn = getattr(app, "show_main", None)
    if callable(fn):
        try:
            fn(route, **opts)
            return True
        except Exception:
            logger.warning("show_main(%s) failed", route, exc_info=True)
    return False


def copy_text(text):
    try:
        QGuiApplication.clipboard().setText(text or "")
        return True
    except Exception:
        logger.debug("copy failed", exc_info=True)
        return False


def copy_rich(text):
    """A Smart Action's or meeting's Markdown the way people paste it: HTML
    (real lists, bold, headings) + nice plain text (☐ / • , no ** or #).
    Text without markup is copied unchanged (rich_text / ui.components.clipboard)."""
    try:
        return bool(clipboard.copy_rich(text or ""))
    except Exception:
        logger.debug("rich copy failed", exc_info=True)
        return False


def flash_copied(btn, color="muted"):
    """The copy icon turns into a green check for a moment."""
    try:
        btn.set_icon("check", "ok-tx")
        btn.setToolTip("Copied")
        QTimer.singleShot(1300, lambda: _unflash(btn, color))
    except Exception:
        pass


def _unflash(btn, color):
    try:
        btn.set_icon("copy", color)
        btn.setToolTip(btn.accessibleName())
    except RuntimeError:
        pass                                    # the row was rebuilt meanwhile


def run_later(fn, then, error=None, sync=False):
    """fn() on a worker, then(result) on the GUI thread (api_keys.run_then);
    sync=True runs it right here (tests, previews)."""
    if sync:
        try:
            res = fn()
        except Exception as e:                  # noqa: BLE001 - shown as an empty list
            if error:
                error(e)
            return
        then(res)
        return
    try:
        from api_keys import run_then
        run_then(fn, then=then, error=error, name="gusan-page")
    except Exception:
        logger.warning("background load failed to start; loading here", exc_info=True)
        run_later(fn, then, error, sync=True)


def connect_signals(owner, specs):
    """Connect (source, signal name, bound slot) triples, each once per owner,
    skipping a missing source or signal (a service Wave 4 hasn't created yet)."""
    done = owner.__dict__.setdefault("_connected_signals", set())
    for src, name, slot in specs:
        sig = getattr(src, name, None) if src is not None else None
        if sig is None or not hasattr(sig, "connect"):
            continue
        key = (id(src), name, getattr(slot, "__name__", repr(slot)))
        if key in done:
            continue
        try:
            sig.connect(slot)
            done.add(key)
        except Exception:
            logger.debug("%s connect failed", name, exc_info=True)


def colored(lbl, token):
    """Text colour from a theme token (QSS on the label only)."""
    lbl.setStyleSheet("color: %s; background: transparent;" % theme.qss_color(token))
    return lbl


def text_label(text, px, weight=400, token="text", display=False, parent=None, tabular=False):
    lb = QLabel(text, parent)
    lb.setFont(theme.font(px, weight, display=display, tabular=tabular))
    colored(lb, token)
    return lb


def short_duration(sec):
    """52 min / 45 s / 1 h 40 min."""
    try:
        sec = int(float(sec or 0))
    except (TypeError, ValueError):
        sec = 0
    if sec < 60:
        return "%d s" % sec if sec else ""
    m = int(round(sec / 60.0))
    if m < 60:
        return "%d min" % m
    h, m = divmod(m, 60)
    return "%d h %d min" % (h, m) if m else "%d h" % h


def parse_when(value):
    """'2026-10-04 11:42:00' or an ISO time with a zone -> naive local datetime | None."""
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def group_of(when, now):
    if when is None:
        return "Earlier"
    d = (now.date() - when.date()).days
    if d <= 0:
        return "Today"
    if d == 1:
        return "Yesterday"
    if d < 7:
        return "Earlier this week"
    return "Earlier"


def time_text(when, now):
    """11:42 (today, yesterday) · Tue (this week) · 29 Sep (older)."""
    if when is None:
        return ""
    d = (now.date() - when.date()).days
    if d <= 1:
        return when.strftime("%H:%M")
    if d < 7:
        return when.strftime("%a")
    if when.year == now.year:
        return "%d %s" % (when.day, when.strftime("%b"))
    return "%d %s %d" % (when.day, when.strftime("%b"), when.year)


def lang_code(code):
    c = str(code or "").strip().lower()
    if not c or c in ("auto", "multi", "none"):
        return ""
    return c.split("-")[0].upper()[:3]


def _norm(path):
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except Exception:
        return str(path)


def one_line(text):
    return " ".join(str(text or "").split())


def plain_line(text):
    """One line of a Smart Action / notes result as people read it: no
    ** # - [ ] markers ("☐ Call David ☐ Book the room"). Prose is unchanged."""
    return one_line(rich_text.to_plain(str(text or "")))


def is_meeting_entry(e):
    return str(e.get("backend") or "") == "meeting" or bool(e.get("audio_dir"))


def entry_action(e):
    for k in ("action", "output_action", "mode"):
        v = str(e.get(k) or "").strip()
        if v and v != "transcribe_only":
            return v
    return ""


def meeting_title_from_entry(text):
    first = (str(text or "").strip().splitlines() or [""])[0]
    for suffix in (" - Meeting Notes", " – Meeting Notes"):
        if first.endswith(suffix):
            return first[: -len(suffix)].strip() or "Meeting"
    return first.strip() or "Meeting"


def recent_items(entries, meetings, transcripts, active_dir=None, active_state=None):
    """One newest-first list of Recent rows from the three stores.

    Each row: kind ("dictation" | "smart" | "meeting" | "audio"), when (naive
    local datetime | None), main (one line: text, or the title; Smart Action
    Markdown as plain text), status (a few words after a title), label
    ("Dictation · EN"), copy (the text, or ("meeting", dir) / ("transcript",
    id) to read on demand), rich (True: copy the text as HTML + plain text,
    a Smart Action's or meeting's Markdown), open ((route, opts) | None), key
    (stable id) and search (lower-case haystack)."""
    import meeting_store
    items = []
    meeting_dirs = {_norm(m.get("dir")) for m in meetings or [] if m.get("dir")}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        text = str(e.get("text") or "")
        if not text.strip():
            continue
        when = parse_when(e.get("timestamp"))
        if is_meeting_entry(e):
            if e.get("audio_dir") and _norm(e.get("audio_dir")) in meeting_dirs:
                continue                                    # the meeting row stands for it
            plain = rich_text.to_plain(text)
            title = one_line(meeting_title_from_entry(plain)) or "Meeting"
            items.append(dict(kind="meeting", when=when, main=title, status="Notes ready", label="Meeting",
                              copy=text, rich=True, open=("meetings", {}), key="h:" + str(e.get("timestamp")),
                              search=(title + " " + plain[:400]).lower()))
            continue
        action = entry_action(e)
        if action:
            word = ACTION_LABELS.get(action, "")
            lab = "Smart Action · " + word if word else "Smart Action"
            main = plain_line(text)
            items.append(dict(kind="smart", when=when, main=main, status="", label=lab, copy=text, rich=True,
                              open=("smart_actions", {}), key="h:" + str(e.get("timestamp")),
                              search=(main + " " + lab).lower()))
            continue
        code = lang_code(e.get("language"))
        lab = "Dictation · " + code if code else "Dictation"
        items.append(dict(kind="dictation", when=when, main=one_line(text), status="", label=lab, copy=text,
                          open=("dictation", {"entry": str(e.get("timestamp") or "")}),
                          key="h:" + str(e.get("timestamp")), search=(text + " " + lab).lower()))
    for m in meetings or []:
        try:
            status = meeting_store.meeting_status(m, active_dir, active_state)
        except Exception:
            status = "notes_ready" if m.get("has_notes") else "audio_only"
        dur = short_duration(m.get("duration_sec"))
        lab = "Meeting · " + dur if dur else "Meeting"
        title = str(m.get("title") or "Meeting")
        # The Meetings page's on_open: folder=<dir> opens its notes; the one
        # being recorded opens the live view instead.
        busy = status in ("recording", "processing")
        target = ("meetings", {"view": "live"}) if busy else ("meetings", {"folder": m.get("dir")})
        items.append(dict(kind="meeting", when=parse_when(m.get("started") or m.get("timestamp")), main=title,
                          status=MEETING_STATUS_TEXT.get(status, ""), label=lab,
                          copy=("meeting", m.get("dir")), open=target,
                          key="m:" + str(m.get("id") or m.get("dir")),
                          search=(title + " " + str(m.get("attendees") or "") + " meeting").lower()))
    for t in transcripts or []:
        dur = short_duration(t.get("duration_sec"))
        lab = "Audio to text · " + dur if dur else "Audio to text"
        title = str(t.get("title") or t.get("source_name") or "Recording")
        items.append(dict(kind="audio", when=parse_when(t.get("created")), main=title, status="Transcript ready",
                          label=lab, copy=("transcript", t.get("id")),
                          open=("audio_to_text", {"transcript_id": t.get("id")}), key="t:" + str(t.get("id")),
                          search=(title + " " + str(t.get("source_name") or "") + " audio").lower()))
    items.sort(key=lambda it: it["when"] or datetime.min, reverse=True)
    return items


def filter_items(items, query):
    words = [w for w in str(query or "").lower().split() if w]
    if not words:
        return list(items)
    return [it for it in items if all(w in it["search"] for w in words)]


def load_sources():
    """(history entries, meetings, transcripts) - disk reads, for a worker."""
    out = []
    try:
        import history
        out.append(history.load())
    except Exception:
        logger.warning("history.load failed", exc_info=True)
        out.append([])
    try:
        import meeting_store
        out.append(meeting_store.list_meetings())
    except Exception:
        logger.warning("list_meetings failed", exc_info=True)
        out.append([])
    try:
        import transcript_store
        out.append(transcript_store.list_transcripts(limit=60))
    except Exception:
        logger.warning("list_transcripts failed", exc_info=True)
        out.append([])
    return tuple(out)


def read_copy(spec):
    """(text, rich) a row's Copy puts on the clipboard: a meeting's notes
    (rich: Markdown rendered), else its transcript in readable paragraphs
    (plain); an Audio to text transcript (plain). Disk reads, for a worker."""
    if isinstance(spec, str):
        return spec, False
    kind, ref = spec
    if kind == "meeting":
        import meeting_store
        notes = meeting_store.load_notes(ref) or ""
        if notes.strip():
            return notes, True
        transcript = meeting_store.load_transcript(ref) or ""
        return "\n\n".join(rich_text.transcript_paragraphs(transcript)), False
    if kind == "transcript":
        import transcript_store
        doc = transcript_store.load_transcript(ref)
        return (transcript_store.plain_text(doc) if doc else ""), False
    return "", False


def read_copy_text(spec):
    """The text a row's Copy puts on the clipboard (meeting notes, a transcript)."""
    return read_copy(spec)[0]


def store_signature():
    """mtimes of history.json and the meetings / transcripts folders (cheap stats)."""
    paths = []
    try:
        import history
        paths.append(history.HISTORY_PATH)
    except Exception:
        pass
    try:
        import meeting_store
        paths.append(str(meeting_store.meetings_dir()))
    except Exception:
        pass
    try:
        import transcript_store
        paths.append(str(transcript_store.transcripts_dir()))
    except Exception:
        pass
    sig = []
    for p in paths:
        try:
            sig.append(os.stat(p).st_mtime_ns)
        except OSError:
            sig.append(0)
    return tuple(sig)


def speech_bits(app):
    """(plain, model name, where, kind) of what transcribes dictation now."""
    try:
        import engine_choice
        r = engine_choice.runs_on(app, "dictation")
        return r.get("plain", ""), r.get("model_name", ""), r.get("where", ""), r.get("kind", "local"), r
    except Exception:
        logger.debug("runs_on failed", exc_info=True)
        return "", "", "on this computer", "local", {}


def model_download_state(app, info):
    """(state, pct) of the local speech model in app.downloads, or ("", 0)."""
    dl = getattr(app, "downloads", None)
    if dl is None or not info or info.get("kind") != "local":
        return "", 0
    mid = info.get("model_id")
    try:
        state = dl.state(mid)
    except Exception:
        return "", 0
    pct = 0
    if state == "downloading":
        try:
            prog = dl.progress_of(mid)
            pct = int(prog[0]) if prog else 0
        except Exception:
            pct = 0
    return state, pct


# ── painted pieces ───────────────────────────────────────────────────────────
class FlowLayout(QHBoxLayout):
    """Left-to-right items that wrap to a new line when the row is too narrow
    (the hero chips at the window's minimum width).

    The items stay in Qt's own box layout, nothing here keeps a QLayoutItem: a
    layout that holds the items' Python wrappers in a list (the usual FlowLayout
    recipe) finds them "already deleted" on a later layout pass, because PySide
    invalidates item wrappers while their C++ items are alive (it happened when
    an unrelated label was deleted, so only sometimes). Wrappers are fetched
    fresh in each pass instead and dropped before the event loop runs."""

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do(QRect(0, 0, width, 0), True)

    def minimumHeightForWidth(self, width):
        return self.heightForWidth(width)

    def setGeometry(self, rect):
        QLayout.setGeometry(self, rect)             # not QBoxLayout's: the items are placed by _do
        self._do(rect, False)

    def _visible(self):
        """The items on screen now (fresh wrappers: use them within this call)."""
        items = (self.itemAt(i) for i in range(self.count()))
        return [it for it in items if it is not None and not it.isEmpty()]

    def sizeHint(self):
        items = self._visible()
        w = sum(it.sizeHint().width() for it in items) + self._sp * max(0, len(items) - 1)
        h = max([it.sizeHint().height() for it in items] or [0])
        return QSize(w, h)

    def minimumSize(self):
        s = QSize(0, 0)
        for it in self._visible():
            s = s.expandedTo(it.minimumSize())
        return s

    def maximumSize(self):
        return QSize(16777215, 16777215)

    def _do(self, rect, test):
        x, y, line = rect.x(), rect.y(), 0
        for it in self._visible():
            hint = it.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x = rect.x()
                y += line + self._sp
                line = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._sp
            line = max(line, hint.height())
        return y + line - rect.y()


class BigKeycap(QWidget):
    """One hero keycap (the mockup's bigcap: 92 / 76 px, display face, thick bottom)."""

    def __init__(self, text, h=92, fs=38, parent=None):
        super().__init__(parent)
        self._text = text
        self._h = h
        self._font = theme.font(fs, 700, display=True, letter_spacing=-0.01 * fs)
        self._pad = int(h * 0.25)
        self._radius = int(h * 0.19)
        self._bottom = max(4, h // 16)
        tw = QFontMetricsF(self._font).horizontalAdvance(text)
        self.setFixedSize(int(max(h, tw + 2 * self._pad + 2)), h)
        self.setAccessibleName(text)

    def text(self):
        return self._text

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0, 0, self.width(), self.height())
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("line2"))
        p.drawRoundedRect(r, self._radius, self._radius)
        inner = QRectF(1, 1, self.width() - 2, self.height() - 1 - self._bottom)
        g = QLinearGradient(inner.topLeft(), inner.bottomLeft())
        g.setColorAt(0, theme.qcolor("white"))
        g.setColorAt(1, theme.qcolor("bg"))
        p.setBrush(g)
        p.drawRoundedRect(inner, self._radius - 1, self._radius - 1)
        p.setFont(self._font)
        p.setPen(theme.qcolor("ink"))
        p.drawText(inner, Qt.AlignCenter, self._text)
        p.end()


class HeroKeys(QWidget):
    """The real hotkey as huge keycaps joined by '+' (Home 92 px, Dictation 76 px).

    ``max_width``: a long shortcut (Ctrl + Shift + Space) is drawn smaller so
    it fits. The soft shadow (CSS 0 14px 30px) is one effect on this widget,
    so the parent needs ~24 px of room under it (the hero layouts have it)."""

    MIN_H = 40

    def __init__(self, hotkey="", h=92, fs=38, parent=None, max_width=None):
        super().__init__(parent)
        self._h0, self._fs0 = h, fs
        self._max_width = max_width
        self._hotkey = None
        self._drawn = None                      # (keys, h, fs) on screen now
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        eff = QGraphicsDropShadowEffect(self)
        eff.setBlurRadius(30)
        eff.setOffset(0, 12)
        eff.setColor(theme.qcolor("rgba(15,23,42,0.12)"))
        self.setGraphicsEffect(eff)
        self.set_hotkey(hotkey)

    @staticmethod
    def natural_width(keys, h, fs):
        font = theme.font(fs, 700, display=True, letter_spacing=-0.01 * fs)
        fm = QFontMetricsF(font)
        pfm = QFontMetricsF(theme.font(int(fs * 0.75), 500, display=True))
        gap = int(h * 0.15)
        w = sum(max(h, fm.horizontalAdvance(k) + 2 * int(h * 0.25) + 2) for k in keys)
        w += (len(keys) - 1) * (2 * gap + pfm.horizontalAdvance("+"))
        return w

    def set_max_width(self, max_width):
        """Room for the keys (a narrow window draws a long shortcut smaller)."""
        if max_width != self._max_width:
            self._max_width = max_width
            self.set_hotkey(self._hotkey)

    def set_hotkey(self, hotkey):
        self._hotkey = hotkey
        keys = keycap_labels(hotkey) or ["Not set"]
        h, fs = self._h0, self._fs0
        if self._max_width:
            nat = self.natural_width(keys, h, fs)
            if nat > self._max_width:
                s = self._max_width / float(nat)
                h, fs = max(self.MIN_H, int(h * s)), max(15, int(fs * s))
        if self._drawn == (tuple(keys), h, fs):
            return                              # nothing changed: keep the keycaps (no flicker)
        self._drawn = (tuple(keys), h, fs)
        while self._lay.count():
            it = self._lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self._h, self._fs = h, fs
        self._lay.setSpacing(int(h * 0.15))
        for i, k in enumerate(keys):
            if i:
                plus = QLabel("+", self)
                theme.set_role(plus, "faint")
                plus.setFont(theme.font(int(fs * 0.75), 500, display=True))
                self._lay.addWidget(plus, 0, Qt.AlignVCenter)
            self._lay.addWidget(BigKeycap(k, h, fs, self), 0, Qt.AlignVCenter)
        self.setAccessibleName(" + ".join(keys))
        self.setToolTip(" + ".join(keys))
        self.updateGeometry()

    def key_height(self):
        return self._h

    def keys(self):
        return [w.text() for w in self.findChildren(BigKeycap) if w.isVisibleTo(self)]


class Dot(QWidget):
    def __init__(self, token="ok", size=9, parent=None):
        super().__init__(parent)
        self._token = token
        self.setFixedSize(size, size)

    def set_token(self, token):
        self._token = token
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor(self._token))
        p.drawEllipse(QRectF(0, 0, self.width(), self.height()))
        p.end()


class Runs(QWidget):
    """One line of text runs [(text, font, colour token)], elided with … at the end."""

    def __init__(self, runs=(), height=20, parent=None):
        super().__init__(parent)
        self._runs = []
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_runs(runs)

    def set_runs(self, runs):
        self._runs = [(str(t), f, c) for t, f, c in runs if t]
        full = "".join(t for t, _, _ in self._runs)
        self.setAccessibleName(full)
        self.setToolTip(full if len(full) > 60 else "")
        self.updateGeometry()
        self.update()

    def text(self):
        return "".join(t for t, _, _ in self._runs)

    def sizeHint(self):
        w = sum(QFontMetricsF(f).horizontalAdvance(t) for t, f, _ in self._runs)
        return QSize(int(w) + 2, self.height())

    def minimumSizeHint(self):
        return QSize(0, self.height())

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        x = 0.0
        avail = float(self.width())
        for i, (t, f, c) in enumerate(self._runs):
            fm = QFontMetricsF(f)
            w = fm.horizontalAdvance(t)
            p.setFont(f)
            p.setPen(theme.qcolor(c))
            rest = avail - x
            if w > rest:
                t = fm.elidedText(t, Qt.ElideRight, max(0.0, rest))
                w = fm.horizontalAdvance(t)
            base = (self.height() + fm.ascent() - fm.descent()) / 2.0
            p.drawText(QPointF(x, base), t)
            x += w
            if x >= avail:
                break
        p.end()


class KeysOrText(QWidget):
    """Small keycaps of a hotkey; plain text ("Ctrl + Shift + Space") when the
    keycaps don't fit (a narrow tile)."""

    def __init__(self, hotkey="", parent=None, empty_text=""):
        super().__init__(parent)
        self._hotkey = None
        self._empty_text = empty_text           # shown when no shortcut is set ("Set shortcut")
        self.keys = Keycaps(parent=self)
        self.text_line = Runs(height=24, parent=self)
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_hotkey(hotkey)

    def set_hotkey(self, hotkey):
        if hotkey == self._hotkey:
            return
        self._hotkey = hotkey
        self.keys.set_hotkey(hotkey)
        name = " + ".join(keycap_labels(hotkey))
        if name:
            self.text_line.set_runs([(name, theme.font(12.5, 600), "text2")])
        else:
            name = self._empty_text
            self.text_line.set_runs([(name, theme.font(12.5, 400), "muted")])
        self.setAccessibleName(name)
        self._fit()
        # New keycaps join their layout once the event loop shows them: fit again then.
        QTimer.singleShot(0, self._fit)

    def text(self):
        return self.accessibleName()

    def showing_keys(self):
        return self.keys.width() > 0 and self.text_line.isHidden()

    def sizeHint(self):
        return QSize(self.keys.sizeHint().width(), 24)

    def minimumSizeHint(self):
        return QSize(0, 24)

    def _fit(self):
        try:
            want = self.keys.sizeHint()
        except RuntimeError:
            return
        # The keycaps are never hidden (a hidden widget's new children don't
        # count in its size hint); when they don't fit they get no room.
        fits = want.width() > 0 and self.width() >= want.width()
        if fits:
            self.keys.setGeometry(0, (self.height() - want.height()) // 2, want.width(), want.height())
        else:
            self.keys.setGeometry(0, 0, 0, 0)
        self.text_line.setVisible(not fits)
        self.text_line.setGeometry(0, 0, self.width(), 24)

    def showEvent(self, e):
        super().showEvent(e)
        self._fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()


class _Ring(QWidget):
    """Keyboard focus ring drawn over a clickable card (radius 16)."""

    def __init__(self, owner, radius=16):
        super().__init__(owner)
        self._owner = owner
        self._radius = radius
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _e):
        if not (self._owner.hasFocus() and self._owner.property("focusVisible")):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(theme.qcolor("blue-l"), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), self._radius - 1, self._radius - 1)
        p.end()


class Tile(QAbstractButton):
    """A quick-start tile: glass card 124 px tall, icon (+ PRO) on top, name and
    one line at the bottom. A real button (Tab, Space / Enter, screen readers).
    Home shows no PRO tags (the founder, 2.0): a Free user who starts a Pro
    feature gets the Pro window there."""

    SUB_H = 24                     # KeysOrText's height: the tallest second line

    def __init__(self, icon_widget, title, sub, pro=False, parent=None, sub_token="muted", sub_weight=400):
        super().__init__(parent)
        self._sub_weight = sub_weight
        if isinstance(sub, str):
            sub_widget = Runs([(sub, theme.font(12.5, sub_weight), sub_token)], height=18)
        else:
            sub_widget = sub
        self.setFixedHeight(124)
        self.setMinimumWidth(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.card = Card(self, pad=(15, 16), gap=0, shadow=False)
        self.card.layout().setContentsMargins(15, 16, 15, 15)
        self.card.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(icon_widget, 0, Qt.AlignVCenter)
        top.addStretch(1)
        self.pro_tag = None
        if pro:
            self.pro_tag = ProTag("s", parent=self.card)
            top.addWidget(self.pro_tag, 0, Qt.AlignVCenter)
        wrap = QWidget(self.card)
        wrap.setFixedHeight(24)
        wrap.setLayout(top)
        icon_widget.setParent(wrap)
        self.card.add(wrap)
        self.card.layout().addStretch(1)
        self._title = title
        self.title_label = Runs([(title, theme.font(15, 700), "ink")], height=20, parent=self.card)
        self.card.add(self.title_label)
        self.card.layout().addSpacing(6)
        self.sub = sub_widget
        # The line under the name sits in a keycap-tall slot, so every tile's
        # name is on the same level - keys (Dictate, Smart Actions) or words.
        slot = QWidget(self.card)
        slot.setFixedHeight(self.SUB_H)
        sl = QVBoxLayout(slot)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addStretch(1)
        sub_widget.setParent(slot)
        sl.addWidget(sub_widget)
        sl.addStretch(1)
        self.sub_slot = slot
        self.card.add(slot)
        self._ring = _Ring(self)
        theme.elevate(self, "card")
        sub_text = sub_widget.accessibleName() or (sub_widget.text() if hasattr(sub_widget, "text") else "")
        self._name(sub_text)
        self.setToolTip(title)

    def title(self):
        return self._title

    def sub_text(self):
        return self.sub.text() if hasattr(self.sub, "text") else ""

    def _name(self, sub_text):
        name = self._title + (", " + sub_text if sub_text else "") + (", Pro" if self.pro_tag else "")
        self.setAccessibleName(name)

    def set_sub_text(self, text, token=None):
        if isinstance(self.sub, Runs):
            self.sub.set_runs([(text, theme.font(12.5, self._sub_weight), token or "muted")])
        self._name(text)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.card.setGeometry(self.rect())
        self._ring.setGeometry(self.rect())
        self._ring.raise_()

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self._ring.update()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self._ring.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(e)

    def sizeHint(self):
        return QSize(190, 124)

    def paintEvent(self, _e):
        pass                                    # the card child draws the tile


class NextChip(QAbstractButton):
    """"🔔 Next: Weekly product sync · 14:30" - one calm pill in the header (34 px)."""

    H = 34
    MAX_TITLE = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self._title = ""
        self._time = ""
        self._f_muted = theme.font(13.5, 400)
        self._f_title = theme.font(13.5, 600)
        self._f_time = theme.font(13.5, 400, tabular=True)
        self.setFixedHeight(self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.setToolTip("Gusan asks to record 1 min before")
        self.setAccessibleName("Next meeting")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_meeting(self, title, when):
        self._title = one_line(title) or "Meeting"
        self._time = when or ""
        self.setAccessibleName("Next meeting: %s%s" % (self._title, (" at " + self._time) if self._time else ""))
        self.setFixedWidth(self._width())
        self.update()

    def title(self):
        return self._title

    def time(self):
        return self._time

    def _parts(self):
        fm_m, fm_t, fm_n = (QFontMetricsF(f) for f in (self._f_muted, self._f_title, self._f_time))
        title = fm_t.elidedText(self._title, Qt.ElideRight, self.MAX_TITLE)
        tail = (" · " + self._time) if self._time else ""
        return (fm_m.horizontalAdvance("Next:"), title, fm_t.horizontalAdvance(title), tail,
                fm_n.horizontalAdvance(tail))

    def _width(self):
        w_next, _t, w_title, _tail, w_tail = self._parts()
        return int(12 + 16 + 8 + w_next + 8 + w_title + w_tail + 14 + 1)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        focus = self.hasFocus() and self.property("focusVisible")
        p.setPen(QPen(theme.qcolor("blue-l" if focus else "glass-line"), 2 if focus else 1))
        p.setBrush(theme.qcolor("rgba(255,255,255,0.6)" if not self.underMouse() else "rgba(255,255,255,0.9)"))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.drawPixmap(12, (self.height() - 16) // 2, icons.pixmap("bell", 16, "blue"))
        w_next, title, w_title, tail, w_tail = self._parts()
        x = 12 + 16 + 8
        for text, font, token, w in (("Next:", self._f_muted, "muted", w_next), (title, self._f_title, "ink", w_title),
                                     (tail, self._f_time, "text2", w_tail)):
            fm = QFontMetricsF(font)
            base = (self.height() + fm.ascent() - fm.descent()) / 2.0
            p.setFont(font)
            p.setPen(theme.qcolor(token))
            p.drawText(QPointF(x, base), text)
            x += w + (8 if text == "Next:" else 0)
        p.end()

    def enterEvent(self, e):
        super().enterEvent(e)
        self.update()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(e)


def search_field(placeholder, width, height, parent=None):
    """A search box with the search icon in front and a clear button."""
    le = QLineEdit(parent)
    le.setPlaceholderText(placeholder)
    le.setAccessibleName(placeholder)
    le.setFixedSize(width, height)
    le.setFont(theme.font(13.5))
    act = le.addAction(icons.icon("search", 16, "muted"), QLineEdit.LeadingPosition)
    act.setText("Search")
    le.setClearButtonEnabled(True)
    # The line edit's own icon buttons get names too (screen readers list them).
    for b in le.findChildren(QAbstractButton):
        if not (b.text() or "").strip() and not (b.accessibleName() or "").strip():
            mine = getattr(b, "defaultAction", lambda: None)() is act
            b.setAccessibleName("Search" if mine else "Clear search")
            b.setFocusPolicy(Qt.NoFocus)
    return le


class _Hairline(QWidget):
    """A row whose top edge is a 1 px line (unless it's the first of its group)."""

    def __init__(self, parent=None, first=False, token="line"):
        super().__init__(parent)
        self._first = first
        self._token = token
        self._flash = False

    def set_flash(self, on):
        self._flash = bool(on)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        if self._flash:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(Qt.NoPen)
            p.setBrush(theme.qcolor("blue-sub"))
            p.drawRoundedRect(QRectF(0, 1, self.width(), self.height() - 1), 8, 8)
        if not self._first:
            p.fillRect(0, 0, self.width(), 1, theme.qcolor(self._token))
        p.end()


KIND_ICON = {"dictation": ("mic", "blue"), "smart": ("wand", "pro"), "meeting": ("people", "meet"),
             "audio": ("wave", "blue")}


class RecentRow(_Hairline):
    """icon · time · one line · kind · Copy · Open (50 px)."""

    def __init__(self, item, now, page, first=False):
        super().__init__(page.list_box, first=first)
        self.item = item
        self.page = page
        self.setFixedHeight(50 if first else 51)        # 50 px + the 1 px line above (CSS border-top)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0 if first else 1, 8, 0)
        lay.setSpacing(14)
        name, tok = KIND_ICON.get(item["kind"], ("text", "blue"))
        ic = QLabel(self)
        ic.setPixmap(icons.pixmap(name, 18, tok))
        ic.setFixedWidth(18)
        lay.addWidget(ic, 0, Qt.AlignVCenter)
        self.time_label = text_label(time_text(item["when"], now), 12.5, 400, "muted", parent=self, tabular=True)
        self.time_label.setFixedWidth(40)
        lay.addWidget(self.time_label, 0, Qt.AlignVCenter)
        if item["kind"] in ("meeting", "audio"):
            runs = [(item["main"], theme.font(13.5, 600), "ink")]
            if item.get("status"):
                runs.append((" · " + item["status"], theme.font(13.5), "muted"))
        else:
            runs = [(item["main"], theme.font(13.5), "text")]
        self.main = Runs(runs, parent=self)
        lay.addWidget(self.main, 1, Qt.AlignVCenter)
        self.kind_label = text_label(item["label"], 12.5, 400, "muted", parent=self)
        self.kind_label.setFixedWidth(150)
        lay.addWidget(self.kind_label, 0, Qt.AlignVCenter)
        btns = QHBoxLayout()
        btns.setSpacing(2)
        short = item["main"][:40]
        self.copy_button = IconButton("copy", "Copy", size=32, icon_size=16, color="muted", parent=self)
        self.copy_button.setAccessibleName("Copy " + short)
        self.copy_button.clicked.connect(self._copy)
        btns.addWidget(self.copy_button)
        self.open_button = Button("Open", "ghost", "s", parent=self)
        self.open_button.setFont(theme.font(13, 600))
        self.open_button.setAccessibleName("Open " + short)
        self.open_button.clicked.connect(self._open)
        btns.addWidget(self.open_button)
        lay.addLayout(btns)

    def _copy(self):
        # Smart Actions and meeting notes: HTML + nice plain text (D9);
        # dictations and transcripts: the words as they are.
        spec = self.item["copy"]
        if isinstance(spec, str):
            if (copy_rich if self.item.get("rich") else copy_text)(spec):
                flash_copied(self.copy_button)
            return
        btn = self.copy_button

        def done(res):
            text, rich = res
            try:
                if text and (copy_rich if rich else copy_text)(text):
                    flash_copied(btn)
            except RuntimeError:
                pass
        run_later(lambda: read_copy(spec), done, sync=self.page.sync_io)

    def _open(self):
        route, opts = self.item["open"]
        self.page.app_call_open(route, **(opts or {}))


class HomePage(QWidget):
    """The Home page. See the module notes."""

    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.sync_io = False                    # tests / previews load on the GUI thread
        self._items = []
        self._loaded = False
        self._gen = 0
        self._sig = None
        self._query = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.next_chip = NextChip()
        self.next_chip.hide()
        self.next_chip.clicked.connect(lambda: self.app_call_open("meetings"))
        self.header = PageHeader(self._title(), actions=[self.next_chip], parent=self)
        root.addWidget(self.header)
        # The one-time "Transcribe is now Gusan" line (1.x upgraders): stays
        # up for this run once shown, gone for good after that or after ✕.
        self._rename_note_live = bool(call(self.app, "rename_note_due"))
        self._rename_note_marked = False
        self.rename_note = self._build_rename_note()
        self.rename_note.setVisible(self._rename_note_live)
        note_row = QHBoxLayout()
        note_row.setContentsMargins(32, 0, 32, 8)
        note_row.addWidget(self.rename_note)
        root.addLayout(note_row)

        body = QVBoxLayout()
        body.setContentsMargins(32, 4, 32, 32)
        body.setSpacing(32)
        root.addLayout(body)
        body.addLayout(self._build_hero())
        body.addLayout(self._build_tiles())
        body.addLayout(self._build_recent())
        root.addStretch(1)

        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_stores)
        self._minute = QTimer(self)
        self._minute.setInterval(60_000)
        self._minute.timeout.connect(self._update_next)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._render_list)

        self._connect_services()
        self._update_hero()
        self._update_live_tile()
        self._update_next()
        self._render_list()
        QTimer.singleShot(0, self._first_load)

    # ── building ──
    def _title(self):
        try:
            from ui.shell import account_state
            tier, name = account_state(self.app)
        except Exception:
            tier, name = "guest", ""
        return "Welcome, %s" % name if tier != "guest" and name else "Welcome"

    def _build_rename_note(self):
        note = QWidget(self)
        note.setObjectName("renameNote")
        note.setAttribute(Qt.WA_StyledBackground, True)
        note.setStyleSheet("#renameNote { background: %s; border: 1px solid %s; border-radius: 10px; }"
                           % (theme.qss_color("blue-sub"), theme.qss_color("blue-line")))
        note.setAccessibleName(RENAME_NOTE)
        h = QHBoxLayout(note)
        h.setContentsMargins(12, 6, 6, 6)
        h.setSpacing(10)
        ic = QLabel(note)
        ic.setPixmap(icons.icon("info", 16, "blue-tx").pixmap(16, 16))
        h.addWidget(ic, 0, Qt.AlignVCenter)
        self.rename_note_text = text_label(RENAME_NOTE, 13, 400, "text2", parent=note)
        self.rename_note_text.setWordWrap(True)
        h.addWidget(self.rename_note_text, 1, Qt.AlignVCenter)
        self.rename_note_close = IconButton("x", "Dismiss", 28, 14, "muted", parent=note)
        self.rename_note_close.clicked.connect(self.close_rename_note)
        h.addWidget(self.rename_note_close, 0, Qt.AlignVCenter)
        return note

    def close_rename_note(self):
        """✕: gone now and for good."""
        self._rename_note_live = False
        self.rename_note.hide()
        self._mark_rename_note()

    def _mark_rename_note(self):
        if not self._rename_note_marked:
            self._rename_note_marked = True
            call(self.app, "dismiss_rename_note")

    def _build_hero(self):
        # A layout, not a widget: the keycaps' shadow paints onto the page.
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 6, 0, 4)
        lay.setSpacing(36)
        self.hero_keys = HeroKeys(cfg_of(self.app).get("hotkey", "alt+r"), 92, 38, self, max_width=380)
        lay.addWidget(self.hero_keys, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(10)
        srow = QHBoxLayout()
        srow.setSpacing(8)
        self.status_dot = Dot("ok", 9, self)
        srow.addWidget(self.status_dot, 0, Qt.AlignVCenter)
        self.status_label = text_label("Ready", 16, 600, "ok-tx", display=True, parent=self)
        srow.addWidget(self.status_label, 0, Qt.AlignVCenter)
        srow.addStretch(1)
        col.addLayout(srow)
        self.caption = text_label("Press in any app and talk.", 24, 600, "ink", display=True, parent=self)
        self.caption.setWordWrap(True)          # one line normally; two in a narrow window
        col.addWidget(self.caption)
        self._chips = FlowLayout(spacing=8)
        self.lang_chip = Chip("Detect language", "neutral", "globe", self)
        self.model_chip = Chip("Balanced", "neutral", "wave", self)
        self.where_chip = Chip("On this computer", "neutral", "laptop", self)
        for c in (self.lang_chip, self.model_chip, self.where_chip):
            self._chips.addWidget(c)
        col.addLayout(self._chips)
        lay.addLayout(col, 1)
        return lay

    def _build_tiles(self):
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)

        def icon(name, token):
            lb = QLabel()
            lb.setPixmap(icons.pixmap(name, 22, token))
            return lb

        dict_keys = KeysOrText(cfg_of(self.app).get("hotkey", "alt+r"))
        self.tile_dictate = Tile(icon("mic", "blue"), "Dictate", dict_keys)
        # D7: the Smart Actions shortcut's keycaps, like the Dictate tile.
        smart_keys = KeysOrText(smart_hotkey(self.app), empty_text="Set shortcut")
        self.tile_smart = Tile(icon("wand", "pro"), "Smart Actions", smart_keys)
        # One colour per feature: Dictate blue, Smart Actions violet, meetings
        # orange, Live Assistant green.
        self.tile_meeting = Tile(icon("people", "meet"), "Record a meeting", "AI notetaker")
        live_keys = KeysOrText(live_hotkey(self.app), empty_text="Set shortcut")
        self.tile_live = Tile(icon("incognito", "live"), "Live Assistant", live_keys)
        headphones = QLabel("\U0001F3A7")
        headphones.setFont(theme.font(21))
        f = headphones.font()
        f.setFamilies(["Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", f.family()])
        headphones.setFont(f)
        headphones.setFixedSize(24, 24)
        headphones.setAlignment(Qt.AlignCenter)
        headphones.setAccessibleName("")
        self.tile_audio = Tile(headphones, "Audio to text", "Drop any recording")
        routes = ((self.tile_dictate, "dictation"), (self.tile_smart, "smart_actions"),
                  (self.tile_meeting, "meetings"), (self.tile_live, "live_assistant"),
                  (self.tile_audio, "audio_to_text"))
        grid.setVerticalSpacing(12)
        for t, route in routes:
            t.setParent(self)
            t.clicked.connect(lambda _=False, r=route: self.app_call_open(r))
        self.tiles = [t for t, _ in routes]
        self._tile_grid = grid
        self._tile_cols = 0
        self._layout_tiles(len(self.tiles))
        return grid

    def _layout_tiles(self, cols):
        """Five tiles in one row; three + two when the window is too narrow
        for "Record a meeting" to fit on a tile."""
        if cols == self._tile_cols:
            return
        self._tile_cols = cols
        grid = self._tile_grid
        for t in self.tiles:
            grid.removeWidget(t)
        for i, t in enumerate(self.tiles):
            grid.addWidget(t, i // cols, i % cols)
        for c in range(len(self.tiles)):
            grid.setColumnStretch(c, 1 if c < cols else 0)

    def tile_columns(self):
        return self._tile_cols

    TILE_MIN = 156                              # "Record a meeting" + the tile's padding

    def _reflow(self):
        body_w = self.width() - 64
        five = 5 * self.TILE_MIN + 4 * 12
        self._layout_tiles(5 if body_w >= five else 3)
        # The hero: the keys give way to "Press in any app and talk." on one line.
        need = QFontMetricsF(self.caption.font()).horizontalAdvance(self.caption.text()) + 8
        self.hero_keys.set_max_width(int(max(200, min(380, body_w - 36 - need))))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow()

    def _build_recent(self):
        col = QVBoxLayout()
        col.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(12)
        self.recent_title = label("Recent", "h2", self)
        head.addWidget(self.recent_title, 0, Qt.AlignVCenter)
        head.addStretch(1)
        self.search = search_field("Search everything", 280, 36, self)
        self.search.textChanged.connect(self._on_search)
        head.addWidget(self.search, 0, Qt.AlignVCenter)
        col.addLayout(head)
        self.recent_card = Card(self, pad=(0, 0), gap=0)
        self.recent_card.layout().setContentsMargins(0, 2, 0, 8)
        self.recent_card.setAccessibleName("Recent")
        self.list_box = QWidget(self.recent_card)
        self.list_lay = QVBoxLayout(self.list_box)
        self.list_lay.setContentsMargins(6, 0, 6, 0)
        self.list_lay.setSpacing(0)
        self.recent_card.add(self.list_box)
        col.addWidget(self.recent_card)
        return col

    # ── services ──
    def _connect_services(self):
        # Bound methods, not lambdas: Qt drops these connections when the page
        # is deleted (the error card's Try again rebuilds it), so a long-lived
        # service never calls into a dead page.
        connect_signals(self, (
            (self.app, "sig_history_changed", self._on_history_changed),
            # Recording -> Writing notes… -> Notes ready on a meeting row (the
            # folder poll can't see notes written inside a meeting's folder).
            (getattr(self.app, "meeting_session", None), "state_changed", self._on_history_changed),
            (getattr(self.app, "calendar", None), "changed", self._on_calendar_changed),
            (getattr(self.app, "downloads", None), "state_changed", self._on_download_event),
            (getattr(self.app, "downloads", None), "progress", self._on_download_event),
            (getattr(self.app, "downloads", None), "finished", self._on_download_event),
        ))

    def _on_history_changed(self, *_):
        self.refresh()

    def _on_calendar_changed(self, *_):
        self._update_next()

    def _on_download_event(self, *_):
        self._update_status()               # progress comes ~10 times a second: the status line only

    def app_call_open(self, route, **opts):
        go(self, self.app, route, **opts)

    # ── hero ──
    def _update_hero(self):
        cfg = cfg_of(self.app)
        hk = cfg.get("hotkey", "alt+r")
        self.hero_keys.set_hotkey(hk)
        try:
            self.tile_dictate.sub.set_hotkey(hk)
            self.tile_dictate.set_sub_text(" + ".join(keycap_labels(hk)))
        except Exception:
            logger.debug("Dictate tile update failed", exc_info=True)
        self._update_smart_tile()
        lang = str(cfg.get("language") or "auto")
        self.lang_chip.setText(LANG_NAMES.get(lang, lang.upper()))
        plain, model, where, kind, info = speech_bits(self.app)
        if model and model != plain:
            muted = theme.qss_color("muted")
            self.model_chip.setText('%s<span style="font-weight: 500; color: %s">&#160;· %s</span>'
                                    % (plain, muted, model))
            self.model_chip.setAccessibleName("%s · %s" % (plain, model))
        else:
            self.model_chip.setText(plain or model or "Speech model")
        self.model_chip.setToolTip("Speech model")
        icon_name = {"cloud": "cloud", "own_key": "key"}.get(kind, "laptop")
        self._set_chip_icon(self.where_chip, icon_name)
        self.where_chip.setText((where[:1].upper() + where[1:]) if where else "On this computer")
        self.model_chip.setVisible(bool(plain or model))
        # "Gusan Cloud · Gemini 2.5 Flash" already says where it runs.
        self.where_chip.setVisible(kind != "cloud")
        self._update_status(info)

    def _update_smart_tile(self):
        """The Smart Actions tile's keycaps follow smart_actions_hotkey."""
        try:
            self.tile_smart.sub.set_hotkey(smart_hotkey(self.app))
            self.tile_smart.set_sub_text(self.tile_smart.sub.text())
        except Exception:
            logger.debug("Smart Actions tile update failed", exc_info=True)

    def _update_status(self, info=None):
        """"Ready", or "Getting ready · 42%" while the speech model downloads."""
        if info is None:
            info = speech_bits(self.app)[4]
        state, pct = model_download_state(self.app, info)
        if state == "downloading":
            self.status_dot.set_token("amber")
            self.status_label.setText("Getting ready · %d%%" % pct if pct else "Getting ready")
            colored(self.status_label, "amber-tx")
        else:
            self.status_dot.set_token("ok")
            self.status_label.setText("Ready")
            colored(self.status_label, "ok-tx")
        self.status_label.setAccessibleName(self.status_label.text())

    @staticmethod
    def _set_chip_icon(chip, name):
        ic = getattr(chip, "_icon", None)
        if ic is not None:
            ic.setPixmap(icons.pixmap(name, 13, "text2", sw=2))

    def _update_live_tile(self):
        """The Live Assistant tile's keycaps follow live_assist_hotkey."""
        try:
            self.tile_live.sub.set_hotkey(live_hotkey(self.app))
            self.tile_live.set_sub_text(self.tile_live.sub.text())
        except Exception:
            logger.debug("Live Assistant tile update failed", exc_info=True)

    # ── calendar ──
    def _update_next(self):
        m = None
        cal = getattr(self.app, "calendar", None)
        try:
            if cal is not None and getattr(cal, "available", False) and getattr(cal, "is_connected", False):
                m = cal.next_meeting()
        except Exception:
            logger.debug("calendar.next_meeting failed", exc_info=True)
            m = None
        if not m:
            self.next_chip.hide()
            self._minute.stop()
            return
        when = ""
        try:
            import calendar_sync
            parts = calendar_sync.when_parts(m, datetime.now().astimezone())
            when = ("%s %s" % (parts["day"], parts["time"])).strip()
        except Exception:
            start = m.get("start")
            when = start.strftime("%H:%M") if hasattr(start, "strftime") else ""
        self.next_chip.set_meeting(m.get("title") or "Meeting", when)
        # The promise in the tooltip only when the reminder is on (Meetings' setting).
        asks = cfg_of(self.app).get("calendar_ask_to_record", True)
        self.next_chip.setToolTip("Gusan asks to record 1 min before" if asks else "Opens Meetings")
        self.next_chip.show()
        if not self._minute.isActive():
            self._minute.start()

    # ── Recent ──
    def _first_load(self):
        if self._gen == 0:                      # nothing asked for it yet (on_open usually has)
            self.refresh(sync=self.sync_io)

    def refresh(self, sync=None):
        """Re-read the three stores (on a worker unless sync) and redraw Recent."""
        sync = self.sync_io if sync is None else bool(sync)
        self._gen += 1
        gen = self._gen
        try:
            self._sig = store_signature()
        except Exception:
            self._sig = None

        # The meeting being recorded is read here (GUI thread); the merge runs on
        # the worker too (meeting_status resolves each folder path - disk work).
        active_dir, phase = self._active_meeting()

        def work():
            entries, meetings, transcripts = load_sources()
            return recent_items(entries, meetings, transcripts, active_dir, phase)

        def then(items, g=gen):
            if g != self._gen:
                return
            try:
                self._items = items
                self._loaded = True
                self._render_list()
            except RuntimeError:
                pass                            # the page was closed meanwhile

        run_later(work, then, error=lambda e: logger.warning("Recent failed to load: %s", e), sync=sync)

    def _active_meeting(self):
        s = getattr(self.app, "meeting_session", None)
        if s is None:
            return None, None
        try:
            d = s.active_dir() if callable(getattr(s, "active_dir", None)) else None
            ph = getattr(s, "phase", None)
            ph = ph() if callable(ph) else ph
            return d, ph
        except Exception:
            return None, None

    def _on_search(self, text):
        self._query = text
        self._search_timer.start()

    def items(self):
        return list(self._items)

    def shown_rows(self):
        return [w for w in self.list_box.findChildren(RecentRow) if not w.isHidden()]

    def _clear_list(self):
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()

    def _group_label(self, text):
        lb = text_label(text, 12, 700, "muted", parent=self.list_box)
        lb.setContentsMargins(8, 14, 8, 6)
        return lb

    def _empty(self, text):
        lb = text_label(text, 13.5, 400, "muted", parent=self.list_box)
        lb.setContentsMargins(8, 16, 8, 10)
        lb.setWordWrap(True)
        return lb

    def _render_list(self):
        self._clear_list()
        now = _now()
        if not self._loaded:
            self.list_lay.addWidget(self._empty(" "))
            return
        items = filter_items(self._items, self._query)
        limit = SEARCH_LIMIT if self._query.strip() else HOME_LIMIT
        items = items[:limit]
        if not items:
            cfg = cfg_of(self.app)
            if self._query.strip():
                text = "No matches."
            elif cfg.get("privacy_mode"):
                text = "Privacy mode is on: nothing new is saved."
            elif cfg.get("save_history", True) is False:
                text = "History is off."
            else:
                hk = " + ".join(keycap_labels(cfg_of(self.app).get("hotkey", "alt+r"))) or "your shortcut"
                text = "Nothing yet. Press %s in any app and talk." % hk
            self.list_lay.addWidget(self._empty(text))
            return
        group = None
        first = True
        for it in items:
            g = group_of(it["when"], now)
            if g != group:
                group = g
                first = True
                self.list_lay.addWidget(self._group_label(g))
            self.list_lay.addWidget(RecentRow(it, now, self, first=first))
            first = False

    def _poll_stores(self):
        if not self.isVisible():
            return
        try:
            sig = store_signature()
        except Exception:
            return
        if sig != self._sig:
            self.refresh()

    # ── shell hooks ──
    def on_open(self, **opts):
        self._connect_services()                # a service created after the page (once each)
        self._update_hero()
        self._update_live_tile()
        self._update_next()
        self.refresh()
        if "search" in opts:
            self.search.setText(str(opts.get("search") or ""))

    def on_setting_changed(self, key):
        if key in ("hotkey", "language", "backend", "whisper_model", "managed_provider", "privacy_mode",
                   "mistral_stt_model", "google_stt_model", "mistral_api_key", "google_api_key"):
            self._update_hero()
        if key == "smart_actions_hotkey":
            self._update_smart_tile()
        if key == "live_assist_hotkey":
            self._update_live_tile()
        if key in ("save_history", "privacy_mode"):
            self.refresh()
        if key.startswith("calendar"):
            self._update_next()

    def refresh_account(self):
        self.header.set_title(self._title())
        self._update_hero()

    def showEvent(self, e):
        super().showEvent(e)
        if not self._poll.isActive():
            self._poll.start()
        if self._rename_note_live:
            self._mark_rename_note()            # seen once: not shown at the next launch

    def hideEvent(self, e):
        super().hideEvent(e)
        self._poll.stop()


def create(main_app):
    return HomePage(main_app)


__all__ = ["create", "HomePage", "HeroKeys", "BigKeycap", "Tile", "NextChip", "Runs", "recent_items",
           "filter_items", "group_of", "time_text", "short_duration", "parse_when"]
