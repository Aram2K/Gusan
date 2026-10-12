"""On-disk meeting library: %APPDATA%/Transcribe/meetings/<YYYYMMDD_HHMMSS>/.

Each folder holds meta.json, transcript.txt, notes.md, my_notes.md (what the
user typed during the meeting), chunks.jsonl (live chunks) and
audio_partN.wav (one per recording segment - a resumed meeting adds a part).
This module is the single reader used by the History tab, the detail view,
Resume and the Gusan 2.0 Meetings page.

meta.json keys (all optional - older builds wrote only the first four):
  title, attendees (comma string, kept for older builds), duration_sec
  (recorded time, pauses excluded), timestamp (last processing time - drifts,
  display ``started`` instead), started ("YYYY-MM-DD HH:MM:SS", the real
  start), language, notes_engine, notes_engine_label, notices (list of
  caveats to show above the notes), notes_notice (the one of them about the
  notes engine), notes_error, seen (False until the user opened the finished
  notes), calendar_event_id, attendees_list, source ("page", "live_assist",
  "calendar", "classic").
Meta is only ever changed through ``update_meta`` (read, merge, atomic
write), so keys written by one step survive every other step.
"""
import json
import os
import re
import shutil
import tempfile
import threading
import time
import wave
from pathlib import Path

import storage

_PART_RE = re.compile(r"^audio_part(\d+)\.wav$", re.I)
# A session the user aborted & discarded: its live chunks, kept on disk under
# this name (as before) but out of the History list.
DISCARDED_CHUNKS = "chunks.discarded.jsonl"
META_FILE = "meta.json"
MY_NOTES_FILE = "my_notes.md"

# Row status for the Meetings list (see meeting_status).
STATUS_RECORDING = "recording"
STATUS_PROCESSING = "processing"
STATUS_NOTES_READY = "notes_ready"
STATUS_NOTES_FAILED = "notes_failed"      # transcript saved, no notes
STATUS_AUDIO_ONLY = "audio_only"          # recording or live chunks only

# Read-merge-write of meta.json happens on the processing worker and on the
# GUI thread (rename, seen): one lock keeps the merges from losing keys.
_META_LOCK = threading.Lock()


def meetings_dir():
    return Path(storage.path_for("meetings"))


def load_meta(folder):
    try:
        with open(Path(folder) / META_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_write_text(path, text):
    """Write ``text`` to ``path`` through a temp file in the same folder and
    one rename, so a crash or a reader never sees half a file. Retries the
    rename briefly: on Windows a reader (or a virus scanner) holding the old
    file open makes os.replace fail for a moment."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def update_meta(folder, **fields):
    """Merge ``fields`` into the folder's meta.json and write it atomically.
    Keys not named are kept (an older build's, or another step's). A value of
    None removes that key. Returns the merged dict, or None when the folder
    can't be written."""
    folder = Path(folder)
    with _META_LOCK:
        meta = load_meta(folder)
        for key, value in fields.items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        try:
            _atomic_write_text(folder / META_FILE, json.dumps(meta, ensure_ascii=False))
        except OSError:
            return None
    return meta


def split_attendees(text):
    """"Anna, David,  anna" -> ["Anna", "David"]: the people chips. Commas or
    semicolons separate names; blanks and repeats (any case) are dropped."""
    out, seen = [], set()
    for part in re.split(r"[,;\n]", str(text or "")):
        name = part.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def _read(folder, name):
    try:
        with open(Path(folder) / name, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def load_notes(folder):
    return _read(folder, "notes.md")


def load_my_notes(folder):
    """What the user typed in "My notes" during the meeting ("" if nothing)."""
    return _read(folder, MY_NOTES_FILE)


def save_my_notes(folder, text):
    """Save "My notes" (atomic). Empty text removes the file. True when saved."""
    folder = Path(folder)
    text = text or ""
    try:
        if not text.strip():
            path = folder / MY_NOTES_FILE
            if path.is_file():
                path.unlink()
            return True
        if not folder.is_dir():
            return False
        _atomic_write_text(folder / MY_NOTES_FILE, text)
        return True
    except OSError:
        return False


def load_transcript(folder):
    """transcript.txt - or, for a meeting that never got that far (the app
    closed mid-meeting), what its live chunks captured, so it still shows
    and resumes with what was said."""
    text = _read(folder, "transcript.txt")
    return text if text.strip() else chunks_text(folder)


def chunks_text(folder):
    """The text of chunks.jsonl in spoken order. Chunk indices restart with
    every recording segment, so they're only sorted when unique (one
    segment); otherwise the file order is kept."""
    rows = []
    try:
        with open(Path(folder) / "chunks.jsonl", "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue            # a line cut short by a crash
                if isinstance(row, dict) and isinstance(row.get("text"), str) \
                        and row["text"].strip():
                    rows.append(row)
    except Exception:
        return ""
    idx = [r.get("index") for r in rows]
    if all(isinstance(i, int) for i in idx) and len(set(idx)) == len(idx):
        rows.sort(key=lambda r: r["index"])
    return " ".join(r["text"].strip() for r in rows)


def mark_discarded(folder):
    """Hide an aborted session's live chunks from History (see list_meetings)
    without deleting them. A chunk thread finishing after the rename can
    recreate chunks.jsonl - the marker still hides it."""
    folder = Path(folder)
    try:
        src = folder / "chunks.jsonl"
        if src.is_file():
            src.replace(folder / DISCARDED_CHUNKS)
        elif folder.is_dir():
            (folder / DISCARDED_CHUNKS).touch()
    except OSError:
        pass


def audio_parts(folder):
    """Recording segments in order (audio_part1.wav, audio_part2.wav, ...)."""
    folder = Path(folder)
    parts = []
    try:
        for p in folder.iterdir():
            m = _PART_RE.match(p.name)
            if m and p.is_file():
                parts.append((int(m.group(1)), p))
    except Exception:
        return []
    return [p for _, p in sorted(parts)]


def next_audio_part_path(folder):
    parts = audio_parts(folder)
    n = 1
    if parts:
        n = int(_PART_RE.match(parts[-1].name).group(1)) + 1
    return Path(folder) / f"audio_part{n}.wav"


def summary_preview(notes, limit=140):
    """First real line of the notes for the list card: no headings, quotes,
    list or task markers, and no **bold** / `code` symbols."""
    import rich_text
    for block in rich_text.parse_blocks(notes or ""):
        kind = block[0]
        if kind in ("head", "quote", "code"):
            continue
        text = block[2] if kind in ("num", "task") else block[1]
        for line in str(text).splitlines():
            s = rich_text.strip_inline(line).strip()
            if s:
                return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"
    return ""


def list_meetings():
    """Newest first. Every folder that holds any of a meeting is listed -
    including one with only its recording or live chunks (transcription or
    notes failed, or the app closed mid-meeting), so a saved recording is
    always reachable. Empty and discarded folders are skipped."""
    root = meetings_dir()
    out = []
    try:
        folders = [p for p in root.iterdir() if p.is_dir()]
    except Exception:
        return out
    for folder in sorted(folders, key=lambda p: p.name, reverse=True):
        meta = load_meta(folder)
        has_transcript = (folder / "transcript.txt").is_file()
        has_notes = (folder / "notes.md").is_file()
        parts = audio_parts(folder)
        has_chunks = ((folder / "chunks.jsonl").is_file()
                      and not (folder / DISCARDED_CHUNKS).exists())
        # Live chunks alone don't make a meeting: that is the one being
        # recorded right now, or a session discarded before 1.9.1 (no marker
        # then) - both must stay out of History. With meta.json (written when
        # processing starts) they are a meeting whose transcription failed.
        # meta.json alone (nothing was captured) is no meeting either.
        if not (has_transcript or has_notes or parts or (meta and has_chunks)):
            continue
        attendees = str(meta.get("attendees") or "")
        people = meta.get("attendees_list")
        if not (isinstance(people, list) and all(isinstance(p, str) for p in people)):
            people = split_attendees(attendees)
        notices = meta.get("notices")
        out.append({
            "dir": str(folder),
            "id": folder.name,
            "title": str(meta.get("title") or "Untitled Meeting").strip(),
            "attendees": attendees,
            "timestamp": str(meta.get("timestamp") or _folder_timestamp(folder.name)),
            # No meta.json (it's written when processing starts): the
            # recording's own length beats a "0 s" card.
            "duration_sec": _int(meta.get("duration_sec")) or _wav_seconds(parts),
            "has_transcript": has_transcript,
            "has_notes": has_notes,
            "audio_parts": [str(p) for p in parts],
            # Gusan 2.0 fields. The folder name is the real start; "timestamp"
            # is rewritten on every processing run.
            "started": str(meta.get("started") or _folder_timestamp(folder.name)),
            "attendees_list": list(people),
            "language": str(meta.get("language") or ""),
            "notes_engine": str(meta.get("notes_engine") or ""),
            "notes_engine_label": str(meta.get("notes_engine_label") or ""),
            "notices": [str(n) for n in notices] if isinstance(notices, list) else [],
            "notes_error": str(meta.get("notes_error") or ""),
            # Meetings from before 2.0 were all seen in the old window.
            "seen": meta.get("seen") is not False,
            "source": str(meta.get("source") or ""),
            "calendar_event_id": str(meta.get("calendar_event_id") or ""),
        })
    return out


def meeting_status(m, active_dir=None, active_state=None):
    """One list row's status: "recording" / "processing" for the meeting the
    session is busy with (``active_dir`` + its state; "paused" counts as
    recording), else "notes_ready", "notes_failed" (transcript but no notes)
    or "audio_only" (only the recording or live chunks)."""
    if active_dir and active_state in (STATUS_RECORDING, "paused", STATUS_PROCESSING):
        try:
            same = Path(m["dir"]).resolve() == Path(active_dir).resolve()
        except Exception:
            same = str(m.get("dir")) == str(active_dir)
        if same:
            return STATUS_PROCESSING if active_state == STATUS_PROCESSING else STATUS_RECORDING
    if m.get("has_notes"):
        return STATUS_NOTES_READY
    if m.get("has_transcript"):
        return STATUS_NOTES_FAILED
    return STATUS_AUDIO_ONLY


def search(query, meetings=None, transcripts=False):
    """Meetings matching every word of ``query`` (any case) in the title,
    people, date, language or the first line of the notes - the old History
    filter, word by word. ``transcripts`` also looks through the notes, My
    notes and transcript (slower: reads each folder). An empty query returns
    them all. ``meetings`` defaults to list_meetings()."""
    items = list_meetings() if meetings is None else list(meetings)
    words = [w for w in str(query or "").lower().split() if w]
    if not words:
        return items
    out = []
    for m in items:
        notes = load_notes(m["dir"])
        hay = " ".join([
            str(m.get("title", "")), str(m.get("attendees", "")),
            str(m.get("timestamp", "")), str(m.get("started", "")),
            str(m.get("language", "")), summary_preview(notes),
        ]).lower()
        if transcripts and not all(w in hay for w in words):
            hay = " ".join([hay, notes, load_my_notes(m["dir"]),
                            load_transcript(m["dir"])]).lower()
        if all(w in hay for w in words):
            out.append(m)
    return out


def set_title(folder, title):
    """Rename a meeting (the title pen). Blank becomes "Untitled Meeting".
    True when saved."""
    title = " ".join(str(title or "").split()) or "Untitled Meeting"
    if not Path(folder).is_dir():
        return False
    return update_meta(folder, title=title) is not None


def mark_seen(folder, seen=True):
    """The user opened the finished notes (or, with seen=False, mark them new
    again). True when saved."""
    if not Path(folder).is_dir():
        return False
    return update_meta(folder, seen=bool(seen)) is not None


def is_live_session(m):
    """A meeting recorded from the Live Assistant: tagged ``source`` since
    2.0; older ones were named "Live session HH:MM" by the card."""
    source = m.get("source") or ""
    if source:
        return source == "live_assist"
    return bool(re.match(r"^Live session \d{1,2}:\d{2}$", str(m.get("title") or "")))


def list_live_sessions(meetings=None):
    """The Live Assistant page's "Past sessions": meetings it recorded, newest
    first."""
    items = list_meetings() if meetings is None else meetings
    return [m for m in items if is_live_session(m)]


def _int(value):
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _wav_seconds(parts):
    total = 0.0
    for p in parts:
        try:
            with wave.open(str(p), "rb") as w:
                total += w.getnframes() / float(w.getframerate() or 1)
        except Exception:
            continue                # unreadable/partial WAV: skip it
    return int(total)


def delete_meeting(folder):
    """Permanently delete one meeting folder - notes, transcript, recording.
    Refuses anything that isn't a folder inside the meetings library.
    Returns True when it's gone."""
    try:
        root = meetings_dir().resolve()
        target = Path(folder).resolve()
    except Exception:
        return False
    if target == root or root not in target.parents or not target.is_dir():
        return False
    try:
        shutil.rmtree(target)
    except OSError:
        return False
    return not target.exists()


def delete_all_meetings(keep=None):
    """Delete every meeting folder (listed or aborted leftovers) except
    ``keep`` - a meeting still being recorded or processed. Returns how many
    folders were removed."""
    try:
        folders = [p for p in meetings_dir().iterdir() if p.is_dir()]
        keep_path = Path(keep).resolve() if keep else None
    except Exception:
        return 0
    removed = 0
    for folder in folders:
        if keep_path is not None and folder.resolve() == keep_path:
            continue
        if delete_meeting(folder):
            removed += 1
    return removed


def _folder_timestamp(name):
    # "_2", "_3"...: a second meeting started within the same second.
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?:_\d+)?$", name)
    if not m:
        return name
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}:{s}"


def folder_timestamp(name):
    """"20260929_143000" (a meeting folder's name) -> "2026-09-29 14:30:00",
    the meeting's start. Any other name comes back unchanged."""
    return _folder_timestamp(str(name))


def format_duration(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"
