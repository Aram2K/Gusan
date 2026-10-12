"""Saved Audio to text transcripts and their exports (Gusan 2.0). No Qt.

Library layout (like meeting_store): %APPDATA%/Transcribe/transcripts/<YYYYMMDD_HHMMSS>/
  segments.json  raw segments [{"start", "end", "text", "speaker"}] (seconds, speaker int|None)
  meta.json      title, source_name, source_path, duration_sec, model, language,
                 language_setting, speaker_count, speakers, timestamps, created, status
                 (+ word_path once a Word copy was saved, speaker_names if ever set)
Both are written atomically (storage.atomic_write_json), segments first: a folder
without meta.json is an unfinished save and never listed. Paragraphs are not
stored - they are rebuilt on load with docx_export.group_segments, so the
Timestamps toggle and any later grouping change apply to old transcripts too.

Exports (pure functions over a "doc": a loaded transcript or a file_jobs result):
  plain_text (Copy text) · to_txt · to_markdown · to_srt (subtitles from the kept
  segment times) · save_word (docx_export, creator "Gusan") · export_to(doc, fmt, path).
Telemetry is the caller's job (FileJobController.track_saved).
"""
import ctypes
import datetime
import logging
import os
import re
import shutil
import sys
import textwrap
from pathlib import Path

import docx_export
import storage

logger = logging.getLogger("transcript_store")

TRANSCRIPTS_DIR_NAME = "transcripts"
META = "meta.json"
SEGMENTS = "segments.json"
APP_NAME = "Gusan"                        # user-visible; the data folder stays "Transcribe"

EXPORT_FORMATS = ("docx", "txt", "md", "srt")
EXPORT_LABELS = {"docx": "Word (.docx)", "txt": "Text (.txt)", "md": "Markdown (.md)",
                 "srt": "Subtitles (.srt)"}

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
_CALL_WORDS = {"call", "calls", "zoom", "teams", "meet", "meeting", "webex", "phone",
               "skype", "whatsapp", "facetime"}
_ZOOM_DEFAULT = re.compile(r"^gmt\d{8}")      # Zoom's own "GMT20240101-120000_Recording"
_EDITABLE = {"title", "timestamps", "speaker_names", "word_path"}

SRT_LINE_CHARS = 42                        # two lines of 42: the usual subtitle limit
SRT_MAX_LINES = 2


# ── small helpers ─────────────────────────────────────────────────────────────

def format_duration(sec):
    """3725 -> "1 h 02 min", 125 -> "2 min 05 s", 59 -> "59 s"."""
    m, s = divmod(int(sec or 0), 60)
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else (f"{m} min {s:02d} s" if m else f"{s} s")


def file_kind(name):
    """"video" | "call" | "voice" - the row emoji on Audio to text (🎬 / 📞 / 🎤).
    Calls are a name guess (board_call.mp3, Zoom's GMT2024... files)."""
    base = os.path.basename(str(name or ""))
    stem, ext = os.path.splitext(base.lower())
    if ext in VIDEO_EXTS:
        return "video"
    if _ZOOM_DEFAULT.match(stem) or _CALL_WORDS & set(re.split(r"[^a-z0-9]+", stem)):
        return "call"
    return "voice"


def _float(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default            # NaN -> default


def _speaker(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def clean_segments(segments):
    """Plain JSON-safe segments: {"start", "end", "text", "speaker"} with times
    rounded to ms and blank text dropped."""
    out = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, _float(seg.get("start")))
        end = max(start, _float(seg.get("end"), start))
        out.append({"start": round(start, 3), "end": round(end, 3), "text": text,
                    "speaker": _speaker(seg.get("speaker"))})
    return out


def _names(raw):
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            spk = _speaker(k)
            if spk is not None and isinstance(v, str) and v.strip():
                out[spk] = v.strip()
    return out


# ── the library ──────────────────────────────────────────────────────────────

def transcripts_dir():
    return Path(storage.path_for(TRANSCRIPTS_DIR_NAME))


def _now():
    return datetime.datetime.now().astimezone()


def _new_folder(created):
    root = transcripts_dir()
    root.mkdir(parents=True, exist_ok=True)
    stamp = created.strftime("%Y%m%d_%H%M%S")
    n = 1
    while True:
        folder = root / (stamp if n == 1 else f"{stamp}_{n}")
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            n += 1


def save_transcript(result, created=None):
    """Store a finished job (file_jobs.run_file_job's result). Returns the folder."""
    created = created or _now()
    path = str(result.get("path") or "")
    source_name = str(result.get("name") or os.path.basename(path) or "Transcript")
    segments = clean_segments(result.get("segments"))
    speakers = sorted({s["speaker"] for s in segments if s["speaker"] is not None})
    meta = {
        "version": 1,
        "title": source_name,
        "source_name": source_name,
        "source_path": path,
        "duration_sec": round(_float(result.get("duration")), 2),
        "model": result.get("model") or "",
        "language": result.get("language") or None,
        "language_setting": result.get("language_setting") or "auto",
        "speaker_count": int(result.get("speaker_count") or len(speakers)),
        "speakers": bool(result.get("speakers", bool(speakers))),
        "timestamps": bool(result.get("timestamps", True)),
        "created": created.isoformat(timespec="seconds"),
        "status": "done",
    }
    folder = _new_folder(created)
    storage.atomic_write_json(folder / SEGMENTS, segments, ensure_ascii=False)
    storage.atomic_write_json(folder / META, meta, ensure_ascii=False)
    return folder


def _resolve(id_or_dir):
    """The transcript folder for an id ("20261007_112000") or a folder path -
    only a direct child of the library, never the root or anything outside."""
    if not id_or_dir:
        return None
    try:
        root = transcripts_dir().resolve()
        raw = str(id_or_dir)
        if os.path.isabs(raw) or "/" in raw or "\\" in raw:
            target = Path(raw).resolve()
        else:
            target = (root / raw).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if target == root or target.parent != root:
        return None
    return target


def _read_meta(folder):
    data = storage.read_json(Path(folder) / META, None)
    return data if isinstance(data, dict) else None


def _summary(folder, meta):
    source_name = str(meta.get("source_name") or folder.name)
    return {
        "id": folder.name,
        "dir": str(folder),
        "title": str(meta.get("title") or source_name).strip() or source_name,
        "source_name": source_name,
        "source_path": str(meta.get("source_path") or ""),
        "duration_sec": _float(meta.get("duration_sec")),
        "created": str(meta.get("created") or ""),
        "model": str(meta.get("model") or ""),
        "language": meta.get("language") or None,
        "language_setting": meta.get("language_setting") or "auto",
        "speaker_count": int(_float(meta.get("speaker_count"))),
        "speakers": bool(meta.get("speakers", False)),
        "timestamps": bool(meta.get("timestamps", True)),
        "word_path": str(meta.get("word_path") or ""),
        "kind": file_kind(source_name),
    }


def list_transcripts(limit=None):
    """Newest first. Folders without meta.json (a save cut short) are skipped."""
    try:
        folders = [p for p in transcripts_dir().iterdir() if p.is_dir()]
    except OSError:
        return []
    rows = []
    for folder in folders:
        meta = _read_meta(folder)
        if meta is None:
            continue
        rows.append(_summary(folder, meta))
    rows.sort(key=lambda r: (r["created"], r["id"]), reverse=True)
    return rows[:limit] if limit else rows


def load_transcript(id_or_dir):
    """Everything the transcript view and the exports need, or None."""
    folder = _resolve(id_or_dir)
    if folder is None or not folder.is_dir():
        return None
    meta = _read_meta(folder)
    if meta is None:
        return None
    raw = storage.read_json(folder / SEGMENTS, [])
    doc = _summary(folder, meta)
    doc["segments"] = clean_segments(raw if isinstance(raw, list) else [])
    doc["paragraphs"] = docx_export.group_segments(doc["segments"])
    doc["speaker_names"] = _names(meta.get("speaker_names"))
    doc["duration"] = doc["duration_sec"]
    doc["path"] = doc["source_path"]
    return doc


def update_meta(id_or_dir, **fields):
    """Change title / timestamps / speaker_names / word_path. True when saved."""
    folder = _resolve(id_or_dir)
    if folder is None or not fields or set(fields) - _EDITABLE:
        return False
    meta = _read_meta(folder)
    if meta is None:
        return False
    if "title" in fields:
        title = " ".join(str(fields["title"] or "").split())[:200]
        if not title:
            return False
        fields["title"] = title
    if "timestamps" in fields:
        fields["timestamps"] = bool(fields["timestamps"])
    if "speaker_names" in fields:
        fields["speaker_names"] = {str(k): v for k, v in _names(fields["speaker_names"]).items()}
    if "word_path" in fields:
        wp = fields["word_path"]
        fields["word_path"] = os.fspath(wp) if isinstance(wp, (str, os.PathLike)) else ""
    meta.update(fields)
    try:
        storage.atomic_write_json(folder / META, meta, ensure_ascii=False)
    except (OSError, TypeError, ValueError):
        return False
    return True


def rename_transcript(id_or_dir, title):
    return update_meta(id_or_dir, title=title)


def delete_transcript(id_or_dir):
    """Permanently delete one transcript folder (not the source audio, not an
    exported file). Refuses anything that isn't a folder inside the library."""
    folder = _resolve(id_or_dir)
    if folder is None or not folder.is_dir():
        return False
    try:
        shutil.rmtree(folder)
    except OSError:
        return False
    return not folder.exists()


def delete_all_transcripts(keep=None):
    """Delete every transcript folder except ``keep``. Returns how many went."""
    try:
        folders = [p for p in transcripts_dir().iterdir() if p.is_dir()]
    except OSError:
        return 0
    keep_path = _resolve(keep) if keep else None
    removed = 0
    for folder in folders:
        if keep_path is not None and folder.resolve() == keep_path:
            continue
        if delete_transcript(folder):
            removed += 1
    return removed


# ── exports ──────────────────────────────────────────────────────────────────

def _doc(doc):
    """Normalise a loaded transcript or a run_file_job result."""
    path = str(doc.get("source_path") or doc.get("path") or "")
    source_name = str(doc.get("source_name") or doc.get("name") or os.path.basename(path) or "")
    segments = clean_segments(doc.get("segments"))
    paragraphs = doc.get("paragraphs")
    if paragraphs is None:
        paragraphs = docx_export.group_segments(segments)
    title = str(doc.get("title") or source_name or "Transcript").strip() or "Transcript"
    return {
        "title": title,
        "source_name": source_name,
        "source_path": path,
        "duration": _float(doc.get("duration_sec", doc.get("duration"))),
        "segments": segments,
        "paragraphs": paragraphs,
        "timestamps": bool(doc.get("timestamps", True)),
        "names": _names(doc.get("speaker_names")),
    }


def export_title(doc):
    """The document title: the user's name for it, or the source file's stem."""
    d = _doc(doc)
    if d["title"] == d["source_name"]:
        return os.path.splitext(d["source_name"])[0] or "Transcript"
    return d["title"]


def meta_line(doc):
    d = _doc(doc)
    bits = []
    if d["source_name"]:
        bits.append(f"Transcribed from {d['source_name']}")
    if d["duration"]:
        bits.append(format_duration(d["duration"]))
    bits.append(APP_NAME)
    return " · ".join(bits)


def paragraph_lines(doc, timestamps=None):
    """[(prefix, text)]: prefix like "[01:05] Speaker 2:" (may be "")."""
    d = _doc(doc)
    show_ts = d["timestamps"] if timestamps is None else bool(timestamps)
    out = []
    for p in d["paragraphs"]:
        bits = []
        if show_ts:
            bits.append(f"[{docx_export.format_timestamp(p.get('start', 0))}]")
        label = docx_export.speaker_name(p.get("speaker"), d["names"])
        if label:
            bits.append(f"{label}:")
        out.append((" ".join(bits), str(p.get("text") or "")))
    return out


def plain_text(doc, timestamps=None):
    """Copy text: the paragraphs only, one blank line apart."""
    return "\n\n".join(f"{prefix} {text}".strip()
                       for prefix, text in paragraph_lines(doc, timestamps))


def to_txt(doc):
    body = plain_text(doc)
    return f"{export_title(doc)}\n{meta_line(doc)}\n\n{body}\n"


_MD_INLINE = re.compile(r"([\\`*\[\]<])")
_MD_BLOCK_START = re.compile(r"^([#>+=|-])")
_MD_ORDERED = re.compile(r"^(\d+)([.)])")


def _md(text, line_start=False):
    """Escape what would turn speech into formatting (emphasis, links, HTML,
    and at a line start: headings, quotes, lists)."""
    text = _MD_INLINE.sub(r"\\\1", text)
    if line_start:
        text = _MD_BLOCK_START.sub(r"\\\1", text)
        text = _MD_ORDERED.sub(r"\1\\\2", text)
    return text


def to_markdown(doc):
    parts = [f"# {_md(export_title(doc))}", _md(meta_line(doc), line_start=True)]
    for prefix, text in paragraph_lines(doc):
        if prefix:
            parts.append(f"**{prefix}** {_md(text)}")
        else:
            parts.append(_md(text, line_start=True))
    return "\n\n".join(parts) + "\n"


def srt_time(seconds):
    ms = int(round(max(0.0, _float(seconds)) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_cues(doc):
    """[(start, end, [line, line])] - long segments split into two-line cues,
    their time shared out by length; the speaker named when it changes."""
    d = _doc(doc)
    cues = []
    prev = object()
    for seg in d["segments"]:
        text = " ".join(seg["text"].split())
        start, end = seg["start"], seg["end"]
        if end <= start:
            end = start + 2.0
        spk = seg["speaker"]
        label = docx_export.speaker_name(spk, d["names"]) if spk is not None else ""
        if label and spk != prev:
            text = f"{label}: {text}"
        prev = spk
        lines = textwrap.wrap(text, SRT_LINE_CHARS, break_long_words=False,
                              break_on_hyphens=False) or [text]
        groups = [lines[i:i + SRT_MAX_LINES] for i in range(0, len(lines), SRT_MAX_LINES)]
        total = sum(len(" ".join(g)) for g in groups) or 1
        t = start
        for i, g in enumerate(groups):
            g_end = end if i == len(groups) - 1 else t + (end - start) * len(" ".join(g)) / total
            cues.append((t, g_end, g))
            t = g_end
    return cues


def to_srt(doc):
    blocks = []
    for i, (start, end, lines) in enumerate(srt_cues(doc), 1):
        blocks.append(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n" + "\n".join(lines) + "\n")
    return "\n".join(blocks)


def save_word(doc, path):
    d = _doc(doc)
    return docx_export.save_docx(
        str(path), export_title(doc), d["paragraphs"], meta_line=meta_line(doc),
        include_timestamps=d["timestamps"], speaker_names=d["names"] or None)


_TEXT_EXPORTS = {"txt": to_txt, "md": to_markdown, "srt": to_srt}


def export_to(doc, fmt, path):
    """Write ``doc`` as fmt ("docx" | "txt" | "md" | "srt") to ``path``. Raises
    OSError when the file can't be written (shown inline by the page)."""
    fmt = str(fmt or "").lower().lstrip(".")
    if fmt == "docx":
        return save_word(doc, path)
    if fmt not in _TEXT_EXPORTS:
        raise ValueError(f"unknown export format: {fmt}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_TEXT_EXPORTS[fmt](doc))
    return str(path)


def suggested_name(doc, fmt):
    """"interview transcript.docx" - the stem of the title, file-system safe."""
    ext = str(fmt or "txt").lower().lstrip(".")
    return f"{docx_export.safe_filename(export_title(doc))} transcript.{ext}"


def unique_path(path):
    """``path``, or "name (2).ext", "name (3).ext"... when it already exists."""
    path = Path(path)
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def documents_dir():
    """The user's Documents folder (follows a OneDrive / moved Documents on Windows)."""
    if sys.platform == "win32":
        try:
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)                 # 5 = CSIDL_PERSONAL
        except Exception:
            pass
    return Path.home() / "Documents"


def fallback_dir():
    return documents_dir() / APP_NAME


def save_word_copy(doc):
    """"Save as Word": "<stem> transcript.docx" next to the source file, or in
    Documents/Gusan when that folder can't be written. " (2)" on a name clash.
    Returns the path written (raises OSError when neither place works)."""
    d = _doc(doc)
    name = suggested_name(doc, "docx")
    folders = []
    if d["source_path"]:
        folders.append(Path(d["source_path"]).parent)
    folders.append(fallback_dir())
    last = None
    for folder in folders:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            return save_word(doc, unique_path(folder / name))
        except OSError as e:
            last = e
            logger.info("Word copy not saved in %s: %s", folder, e)
    raise last or OSError("no folder to save the Word copy in")
