"""Reads a document the user added to the Live Assistant, safely.

A document is UNTRUSTED. This module turns one file into plain text (or one
small picture) and nothing else: no Qt, no network, nothing is executed or
opened by the OS, no macros, no links, no embedded files, no external entities.
The file is read once, through a size-capped stream, into memory; everything
after that works on bytes. See docs/redesign/live-documents-spec.md.

    result = doc_reader.read_document(path, cancel=None)   # never raises
    if result.ok: result.sections  # ((label, text), ...)  e.g. ("page 3", "...")
                  result.image     # JPEG bytes for a picture, else b""
    else:         result.reason, result.message   # a calm line for the person

Safety, in short: file size cap; a watchdog thread with a hard wall-clock budget
and cooperative cancel (a parser that hangs is abandoned); ZIP formats with
entry/size/ratio caps and bounded reads (nothing is extracted); XML through
expat with DOCTYPE and entity declarations refused and a depth cap; PDF through
pypdf (non-strict, page and character caps, its stream caps lowered, never a
password); pictures through Pillow limited to six formats with the pixel count
checked before decoding; HTML and RTF through small linear scanners; extracted
text stripped of control, bidi, zero-width and tag characters.

Imports of pypdf and PIL are plain statements inside the readers (PyInstaller
follows them) and lazy, so importing this module stays cheap and works where a
library is missing.
"""
import codecs
import contextlib
import dataclasses
import hashlib
import html
import io
import logging
import os
import posixpath
import re
import stat
import struct
import threading
import time
import unicodedata
import warnings
import zipfile
import zlib
from collections import namedtuple
from urllib.parse import unquote
from xml.parsers import expat

logger = logging.getLogger("transcribe")

# ── budgets (named so tests and the spec can point at them) ──
FILE_BYTES_MAX = 50 * 1024 * 1024        # one file, read through a capped stream
TIME_BUDGET_S = 40.0                     # wall clock per file, hard
_GRACE_S = 3.0                           # extra wait for a reader that ignores its budget
TEXT_CHARS_MAX = 1_500_000               # extracted characters kept per document
TEXT_BYTES_MAX = 16 * 1024 * 1024        # a text-like file (txt, csv, html, rtf): only this much is read into text
ZIP_ENTRIES_MAX = 5000
ZIP_DIRECTORY_MAX = 4 * 1024 * 1024      # the zip's list of parts, as its end record declares it
ZIP_PART_MAX = 40 * 1024 * 1024          # one zip part, declared size
ZIP_TOTAL_MAX = 100 * 1024 * 1024        # all parts read from one zip
ZIP_RATIO_MAX = 300                      # declared / compressed, for parts over 4 MB
XML_DEPTH_MAX = 200
PDF_PAGES_MAX = 500
PDF_PAGE_CHARS_MAX = 50_000
PDF_STREAM_MAX = 25_000_000              # pypdf's own decompression caps are lowered to this
SLIDES_MAX = 1000
SHEETS_MAX = 60
CHAPTERS_MAX = 600
RTF_DEPTH_MAX = 200
SHARED_STRINGS_CHARS_MAX = 8_000_000
IMAGE_PIXELS_MAX = 40_000_000            # decoded in full (png, gif, bmp, tiff, webp)
IMAGE_PIXELS_MAX_JPEG = 85_000_000       # JPEG is decoded at reduced size (Pillow warns at 89 M)
IMAGE_SIDE = 1280                        # stored picture: longest side
IMAGE_BYTES_MAX = 300 * 1024             # stored picture: size after JPEG
NAME_CHARS_MAX = 80

# What a person is told. Calm, plain, never "limit" or a technical word.
MESSAGES = {
    "unsupported": "Gusan can't read this kind of file yet.",
    "too_big": "This file is too big to read here.",
    "password": "This PDF is protected with a password, so Gusan can't read it.",
    "protected": "This file is protected, so Gusan can't read it.",
    "damaged": "Gusan can't read this file. It may be damaged.",
    "unsafe": "Gusan didn't open this file because it couldn't be read safely.",
    "empty": "There's nothing to read in this file.",
    "no_text": "There's no text in this file that Gusan can read.",
    "timeout": "This file took too long to read, so Gusan stopped.",
    "cancelled": "Stopped.",
    "busy": "Gusan is still stopping another file. Try this one again in a moment.",
    "folder": "That's a folder. Add the files inside it instead.",
    "link": "Gusan doesn't open shortcuts or links. Add the file itself.",
    "open": "Gusan couldn't open this file.",
    "error": "Gusan couldn't read this file.",
    "scanned": "Gusan can't read the words in this one on your computer.",
}
REASONS = tuple(MESSAGES)

# fmt -> (type class, what its sections are called)
FORMATS = {
    "pdf": ("pdf", "page"), "docx": ("office", ""), "pptx": ("office", "slide"),
    "xlsx": ("office", "sheet"), "odt": ("office", ""), "odp": ("office", "slide"),
    "ods": ("office", "sheet"), "rtf": ("office", ""), "epub": ("office", "chapter"),
    "html": ("text", ""), "csv": ("text", ""), "text": ("text", ""),
    "png": ("image", ""), "jpeg": ("image", ""), "gif": ("image", ""),
    "bmp": ("image", ""), "tiff": ("image", ""), "webp": ("image", ""),
}
_PIL_FORMATS = {"PNG": "png", "JPEG": "jpeg", "GIF": "gif", "BMP": "bmp", "WEBP": "webp",
                "TIFF": "tiff"}
# Extensions that should be one of the formats above: with another content the file is
# "damaged" rather than "a kind we can't read".
_KNOWN_EXT = {".pdf", ".docx", ".docm", ".pptx", ".pptm", ".xlsx", ".xlsm", ".odt", ".odp",
              ".ods", ".rtf", ".epub", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif",
              ".tiff", ".webp"}
_HTML_EXT = {".html", ".htm", ".xhtml"}
_CSV_EXT = {".csv", ".tsv"}
_SVG_START = re.compile(r"(<\?xml[^>]{0,200}\?>\s*)?(<!doctype svg[^>]{0,300}>\s*)?<svg\b")

ReadResult = namedtuple(
    "ReadResult",
    "ok kind fmt type_class sections pages unit truncated image width height reason "
    "message size sha")
ReadResult.__doc__ = """One file, read.

ok          True when there is text or a picture to keep
kind        "text" | "image" | "failed"
fmt         pdf docx pptx xlsx odt odp ods rtf epub html csv text png jpeg gif bmp tiff webp
type_class  pdf | office | text | image | other
sections    ((label, text), ...) - label like "page 3", "slide 2", "sheet Budget" or ""
pages       pages/slides/sheets/chapters in the file (0 when it has none)
unit        "page" | "slide" | "sheet" | "chapter" | ""
truncated   True when only the first part was read
image       JPEG bytes of a picture (downscaled), else b""
width/height  of that stored picture
reason      "" when ok, else one of REASONS
message     the plain line for the person
size        bytes of the original
sha         short fingerprint of the original (to refuse a duplicate)"""


def _result(**kw):
    base = dict(ok=False, kind="failed", fmt="", type_class="other", sections=(), pages=0,
                unit="", truncated=False, image=b"", width=0, height=0, reason="",
                message="", size=0, sha="")
    base.update(kw)
    return ReadResult(**base)


def failure(reason, message=None, **kw):
    """A ReadResult for a file that could not be read."""
    return _result(reason=reason, message=message or MESSAGES.get(reason, MESSAGES["error"]),
                   **kw)


class _Stop(Exception):
    """Internal: stop reading this file for ``reason`` (one of REASONS)."""

    def __init__(self, reason, message=None):
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.fmt = ""              # what the file looked like (for the failure's type)


class _Truncate(Exception):
    """Internal: enough text was read; keep what there is."""


# ── names and text ──
_NAME_DROP = re.compile(
    "[\x00-\x1f\x7f-\x9f\u00ad\u061c\u115f\u1160\u180e\u200b-\u200f\u2028-\u202e"
    "\u2060-\u206f\u3164\ufe00-\ufe0e\ufeff\uffa0\ufff9-\ufffc\U000e0000-\U000e007f"
    "\U000e0100-\U000e01ef\ud800-\udfff\ue000-\uf8ff]")
_TEXT_DROP = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u00ad\u061c\u180e\u200b-\u200f\u2028\u2029"
    "\u202a-\u202e\u2060-\u206f\ufe00-\ufe0e\ufeff\ufff9-\ufffc\U000e0000-\U000e007f"
    "\U000e0100-\U000e01ef\ud800-\udfff]")


def safe_line(text, limit=NAME_CHARS_MAX, keep_ext=False):
    """``text`` as ONE plain line that is safe to show or to hand a model: control,
    bidi, zero-width and tag characters gone, no line breaks, no angle brackets,
    blanks collapsed, at most ``limit`` characters (``keep_ext`` keeps a short file
    extension when cutting). "" when nothing is left."""
    try:
        text = str(text or "")
    except Exception:
        text = ""
    text = text.replace("\t", " ").replace("\n", " ").replace("\r", " ")
    text = _NAME_DROP.sub("", unicodedata.normalize("NFC", text))
    text = text.replace("<", "\u2039").replace(">", "\u203a")
    text = re.sub(r" {2,}", " ", text).strip()
    if len(text) > limit:
        stem, ext = os.path.splitext(text) if keep_ext else (text, "")
        if len(ext) > 12:
            stem, ext = text, ""
        text = stem[:max(limit - len(ext) - 1, 1)].rstrip() + "\u2026" + ext
    return text


def safe_name(name, limit=NAME_CHARS_MAX):
    """A file name that is safe to show as plain text: no folders, no control, bidi
    or invisible characters, no angle brackets, at most ``limit`` characters (the
    extension is kept). Never empty."""
    try:
        text = re.split(r"[\\/]", str(name or ""))[-1]
    except Exception:
        text = ""
    return safe_line(text, limit, keep_ext=True) or "Document"


def strip_invisible(text):
    """``text`` with the characters a person cannot see but a model reads gone (control,
    bidi, zero-width, "tag" characters, line and paragraph separators) and every line end
    a plain "\\n". Nothing else changes."""
    if not text:
        return ""
    return _TEXT_DROP.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def clean_text(text):
    """Extracted text made calm: newlines normalised, control / bidi / zero-width /
    tag characters dropped (they hide text from people but not from a model), runs
    of blanks and blank lines collapsed."""
    if not text:
        return ""
    text = strip_invisible(text.replace("\u00a0", " "))
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── the budget of one read ──
class _Budget:
    def __init__(self, seconds=TIME_BUDGET_S, cancel=None, max_chars=None):
        self.deadline = time.monotonic() + max(float(seconds), 0.0)
        self.cancel = cancel
        self.max_chars = TEXT_CHARS_MAX if max_chars is None else max_chars
        self.chars = 0
        self.truncated = False
        self.aborted = ""          # set by the watchdog when it gives up on this read

    def check(self):
        if self.aborted:
            raise _Stop(self.aborted)
        if self.cancel is not None:
            try:
                stop = bool(self.cancel())
            except Exception:
                stop = False
            if stop:
                raise _Stop("cancelled")
        if time.monotonic() >= self.deadline:
            raise _Stop("timeout")

    @property
    def full(self):
        return self.chars >= self.max_chars

    def take(self, text):
        """``text`` cut to what is still allowed; raises _Truncate when nothing is."""
        room = self.max_chars - self.chars
        if room <= 0:
            self.truncated = True
            raise _Truncate()
        if len(text) > room:
            text = text[:room]
            self.truncated = True
        self.chars += len(text)
        return text


# ── ZIP formats: read parts in memory, never extract ──
def _directory_size(data):
    """How many bytes the zip's own end record says its list of parts (the central
    directory) takes, read the way zipfile reads it (the LAST end record in the final
    64 KB, a zip64 record just before it when there is one); -1 when there is none.
    zipfile builds an object for EVERY part before any cap of ours could look: a 47 MB
    file of a million tiny parts costs about 14 seconds and 400 MB. So it is measured first."""
    start = max(0, len(data) - (1 << 16) - 22)
    tail = data[start:]
    pos = tail.rfind(b"PK\x05\x06")
    if pos < 0 or len(tail) - pos < 22:
        return -1
    size = struct.unpack_from("<I", tail, pos + 12)[0]
    end = start + pos
    if end >= 76 and data[end - 20:end - 16] == b"PK\x06\x07":
        record = data[end - 76:end - 20]
        if record[:4] == b"PK\x06\x06" and len(record) == 56:
            size = struct.unpack_from("<Q", record, 40)[0]
    return size


class _Zip:
    def __init__(self, data):
        if _directory_size(data) > ZIP_DIRECTORY_MAX:
            raise _Stop("unsafe")
        self.zf = zipfile.ZipFile(io.BytesIO(data))
        infos = self.zf.infolist()
        if len(infos) > ZIP_ENTRIES_MAX:
            raise _Stop("unsafe")
        self.names = {zi.filename: zi for zi in infos}
        self.total = 0

    def has(self, name):
        return name in self.names

    def read(self, name, limit=ZIP_PART_MAX):
        zi = self.names.get(name)
        if zi is None:
            raise _Stop("damaged")
        if zi.flag_bits & 0x1:
            raise _Stop("protected")
        if zi.file_size > limit:
            raise _Stop("too_big")
        if (zi.file_size > 4 * 1024 * 1024 and zi.compress_size
                and zi.file_size / zi.compress_size > ZIP_RATIO_MAX):
            raise _Stop("unsafe")
        with self.zf.open(zi) as fh:
            buf = fh.read(zi.file_size + 1)
        if len(buf) != zi.file_size:
            raise _Stop("damaged")
        self.total += len(buf)
        if self.total > ZIP_TOTAL_MAX:
            raise _Stop("too_big")
        return buf


def _local(name):
    return name.rpartition("|")[2]


def _attr(attrs, local):
    for key, value in attrs.items():
        if _local(key) == local:
            return value
    return None


def _xml_walk(data, b, start=None, end=None, text=None):
    """Stream-parse ``data`` with expat. A DOCTYPE or an entity declaration is
    refused (no entity expansion, no external DTD), depth is capped, the budget is
    checked every 64 KB. Handlers get local names. True when a handler stopped
    early with _Truncate."""
    parser = expat.ParserCreate(namespace_separator="|")
    parser.buffer_text = True
    depth = 0

    def refuse(*_args):
        raise _Stop("unsafe")

    def on_start(name, attrs):
        nonlocal depth
        depth += 1
        if depth > XML_DEPTH_MAX:
            raise _Stop("unsafe")
        if start is not None:
            start(_local(name), attrs)

    def on_end(name):
        nonlocal depth
        depth -= 1
        if end is not None:
            end(_local(name))

    parser.StartElementHandler = on_start
    parser.EndElementHandler = on_end
    if text is not None:
        parser.CharacterDataHandler = text
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    except Exception:
        pass
    try:
        for i in range(0, len(data), 65536):
            b.check()
            parser.Parse(data[i:i + 65536], False)
        parser.Parse(b"", True)
    except expat.ExpatError:
        raise _Stop("damaged")
    except _Truncate:
        return True
    return False


def _rels(z, rel_path, b):
    """[(id, type, target)] of an OPC/OPF-style .rels part, or []."""
    if not z.has(rel_path):
        return []
    out = []

    def start(name, attrs):
        if name == "Relationship":
            out.append((attrs.get("Id", ""), attrs.get("Type", ""), attrs.get("Target", "")))
    try:
        _xml_walk(z.read(rel_path, 4 * 1024 * 1024), b, start=start)
    except _Stop as s:
        if s.reason != "damaged":              # only a broken helper part is skipped
            raise
        return []
    return out


def _resolve(base_dir, target):
    """A relationship target as a zip name, or "" when it points outside."""
    if not target or "://" in target:
        return ""
    if target.startswith("/"):
        path = posixpath.normpath(target.lstrip("/"))
    else:
        path = posixpath.normpath(posixpath.join(base_dir, target))
    return "" if path.startswith("..") or path == "." else path


# ── readers: each returns (sections, pages, truncated) ──
def _read_docx(z, b):
    lines, cur = [], []
    state = {"cell": 0, "skip": 0, "in_t": False}

    def flush(newline=True):
        text = "".join(cur)
        cur.clear()
        text = text.strip()
        if text:
            lines.append(b.take(text))

    def start(name, attrs):
        if state["skip"]:
            if name in ("Fallback",):
                state["skip"] += 1
            return
        if name == "Fallback":
            state["skip"] = 1
        elif name == "t":
            state["in_t"] = True
        elif name == "tab":
            cur.append("\t")
        elif name in ("br", "cr"):
            cur.append("\n")
        elif name == "tc":
            state["cell"] += 1

    def end(name):
        if state["skip"]:
            if name == "Fallback":
                state["skip"] -= 1
            return
        if name == "t":
            state["in_t"] = False
        elif name == "p":
            if state["cell"]:
                cur.append(" ")
            else:
                flush()
        elif name == "tc":
            state["cell"] = max(state["cell"] - 1, 0)
            cur.append(" | ")
        elif name == "tr":
            text = re.sub(r" {2,}", " ", "".join(cur)).strip().strip("|").strip()
            cur.clear()
            if text:
                lines.append(b.take(text))

    def text(chars):
        if state["in_t"] and not state["skip"]:
            cur.append(chars)

    truncated = _xml_walk(z.read("word/document.xml"), b, start, end, text)
    try:
        flush()
    except _Truncate:
        truncated = True
    body = "\n".join(lines)
    return ([("", body)] if body.strip() else []), 0, truncated or b.truncated


def _a_text(data, b):
    """Paragraph text of a DrawingML part (slide, notes): a:t runs, a:p breaks."""
    lines, cur = [], []
    state = {"in_t": False}

    def start(name, attrs):
        if name == "t":
            state["in_t"] = True
        elif name == "br":
            cur.append("\n")

    def end(name):
        if name == "t":
            state["in_t"] = False
        elif name == "p":
            text = "".join(cur).strip()
            cur.clear()
            if text:
                lines.append(b.take(text))

    def text(chars):
        if state["in_t"]:
            cur.append(chars)

    truncated = _xml_walk(data, b, start, end, text)
    return "\n".join(lines), truncated


def _read_pptx(z, b):
    order = []
    rels = {rid: target for rid, _t, target in _rels(z, "ppt/_rels/presentation.xml.rels", b)}
    if z.has("ppt/presentation.xml"):
        # The slide list is "sldId r:id": the r:id attribute is namespaced (its local
        # name is "id" too), the plain numeric id has no namespace.
        ids_rel = []

        def start_rel(name, attrs):
            if name == "sldId":
                for key, value in attrs.items():
                    if "|" in key and _local(key) == "id":
                        ids_rel.append(value)
        try:
            _xml_walk(z.read("ppt/presentation.xml", 8 * 1024 * 1024), b, start=start_rel)
        except _Stop as s:
            if s.reason != "damaged":              # only a broken helper part is skipped
                raise
        for rid in ids_rel:
            part = _resolve("ppt", rels.get(rid, ""))
            if part and z.has(part):
                order.append(part)
    if not order:
        found = []
        for name in z.names:
            m = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name)
            if m:
                found.append((int(m.group(1)), name))
        order = [name for _n, name in sorted(found)]
    sections, truncated = [], False
    for n, part in enumerate(order[:SLIDES_MAX], 1):
        b.check()
        text, cut = _a_text(z.read(part), b)
        if text.strip():
            sections.append(("slide %d" % n, text))
        notes = ""
        slide_dir, slide_file = posixpath.split(part)
        for _rid, rtype, target in _rels(
                z, posixpath.join(slide_dir, "_rels", slide_file + ".rels"), b):
            if rtype.endswith("/notesSlide"):
                npart = _resolve(slide_dir, target)
                if npart and z.has(npart):
                    try:
                        notes, ncut = _a_text(z.read(npart), b)
                    except _Stop as s:
                        if s.reason != "damaged":              # only a broken helper part is skipped
                            raise
                        notes, ncut = "", False
                    cut = cut or ncut
        if notes.strip():
            sections.append(("slide %d notes" % n, notes))
        if cut:
            truncated = True
            break
    return sections, len(order), truncated or b.truncated or len(order) > SLIDES_MAX


def _shared_strings(z, b):
    out = []
    if not z.has("xl/sharedStrings.xml"):
        return out
    state = {"skip": 0, "in_t": False, "total": 0}
    cur = []

    def start(name, attrs):
        if name == "rPh":
            state["skip"] += 1
        elif name == "t" and not state["skip"]:
            state["in_t"] = True
        elif name == "si":
            cur.clear()

    def end(name):
        if name == "rPh":
            state["skip"] = max(state["skip"] - 1, 0)
        elif name == "t":
            state["in_t"] = False
        elif name == "si":
            s = "".join(cur)
            state["total"] += len(s)
            if state["total"] > SHARED_STRINGS_CHARS_MAX:
                raise _Stop("too_big")
            out.append(s)

    def text(chars):
        if state["in_t"]:
            cur.append(chars)

    _xml_walk(z.read("xl/sharedStrings.xml"), b, start, end, text)
    return out


def _sheet_rows(data, shared, b):
    rows, cells, cur = [], [], []
    state = {"t": "", "in_v": False, "in_is": False, "in_t": False}

    def start(name, attrs):
        if name == "c":
            state["t"] = attrs.get("t", "")
            cur.clear()
        elif name == "v":
            state["in_v"] = True
        elif name == "is":
            state["in_is"] = True
        elif name == "t" and state["in_is"]:
            state["in_t"] = True

    def end(name):
        if name == "v":
            state["in_v"] = False
        elif name == "is":
            state["in_is"] = False
        elif name == "t":
            state["in_t"] = False
        elif name == "c":
            value = "".join(cur).strip()
            kind = state["t"]
            if kind == "s":
                try:
                    value = shared[int(value)]
                except (ValueError, IndexError):
                    value = ""
            elif kind == "b":
                value = "TRUE" if value == "1" else "FALSE" if value == "0" else value
            elif kind == "e":
                value = ""
            value = value.strip()
            if value:
                cells.append(value)
            cur.clear()
        elif name == "row":
            if cells:
                rows.append(b.take(" | ".join(cells)))
            cells.clear()

    def text(chars):
        if state["in_v"] or state["in_t"]:
            cur.append(chars)

    truncated = _xml_walk(data, b, start, end, text)
    return rows, truncated


def _read_xlsx(z, b):
    rels = {rid: target for rid, _t, target in _rels(z, "xl/_rels/workbook.xml.rels", b)}
    sheets = []

    def start(name, attrs):
        if name == "sheet":
            rid = None
            for key, value in attrs.items():
                if "|" in key and _local(key) == "id":
                    rid = value
            sheets.append((attrs.get("name", ""), rid, attrs.get("state", "")))
    _xml_walk(z.read("xl/workbook.xml", 8 * 1024 * 1024), b, start=start)
    shared = _shared_strings(z, b)
    sections, truncated, count = [], False, 0
    for idx, (name, rid, state) in enumerate(sheets, 1):
        if idx > SHEETS_MAX:
            truncated = True
            break
        count += 1
        if state in ("hidden", "veryHidden"):
            continue                       # hidden sheets are not the person's to share
        b.check()
        part = _resolve("xl", rels.get(rid, "")) or "xl/worksheets/sheet%d.xml" % idx
        if not z.has(part):
            continue
        rows, cut = _sheet_rows(z.read(part), shared, b)
        if rows:
            sections.append(("sheet " + (safe_line(name, 40) or str(idx)), "\n".join(rows)))
        if cut:
            truncated = True
            break
    return sections, count, truncated or b.truncated


def _read_odf(z, b, fmt):
    state = {"skip": 0, "body": 0, "cell": 0, "page": 0}
    sections, lines = [], []
    label = [""]
    paras, cell_buf, row_cells = [], [], []     # paras: a stack (a text box can sit in a paragraph)

    def close_section():
        text = "\n".join(lines).strip()
        lines.clear()
        if text:
            sections.append((label[0], text))

    def add_line(text):
        text = text.strip()
        if text:
            lines.append(b.take(text))

    def start(name, attrs):
        if state["skip"]:
            state["skip"] += 1
            return
        if name == "body":
            state["body"] = 1
            return
        if not state["body"]:
            return
        if name in ("tracked-changes", "annotation", "forms", "scripts"):
            state["skip"] = 1
        elif name == "page" and fmt == "odp":
            close_section()
            state["page"] += 1
            label[0] = "slide %d" % state["page"]
        elif name == "table":
            if fmt == "ods":
                if _attr(attrs, "display") == "false":
                    state["skip"] = 1
                    return
                close_section()
                label[0] = "sheet " + (safe_line(_attr(attrs, "name") or "", 40) or
                                       str(len(sections) + 1))
        elif name in ("p", "h"):
            if len(paras) < 50:
                paras.append([])
        elif name == "s" and paras:
            try:
                paras[-1].append(" " * min(int(_attr(attrs, "c") or 1), 20))
            except ValueError:
                paras[-1].append(" ")
        elif name == "tab" and paras:
            paras[-1].append("\t")
        elif name == "line-break" and paras:
            paras[-1].append("\n")
        elif name == "table-cell" or name == "covered-table-cell":
            state["cell"] += 1

    def end(name):
        if state["skip"]:
            state["skip"] -= 1
            return
        if not state["body"]:
            return
        if name in ("p", "h") and paras:
            text = "".join(paras.pop()).strip()
            if state["cell"]:
                if text:
                    cell_buf.append(text)
            else:
                add_line(text)
        elif name in ("table-cell", "covered-table-cell"):
            state["cell"] = max(state["cell"] - 1, 0)
            text = " ".join(cell_buf).strip()
            cell_buf.clear()
            if text:
                row_cells.append(text)
        elif name == "table-row":
            if row_cells:
                add_line(" | ".join(row_cells))
            row_cells.clear()

    def text(chars):
        if paras and not state["skip"]:
            paras[-1].append(chars)

    truncated = _xml_walk(z.read("content.xml"), b, start, end, text)
    close_section()
    return sections, len(sections) if fmt in ("odp", "ods") else 0, truncated or b.truncated


def _read_epub(z, b):
    spine_parts = []
    try:
        container = z.read("META-INF/container.xml", 1024 * 1024)
        roots = []

        def start(name, attrs):
            if name == "rootfile" and attrs.get("full-path"):
                roots.append(attrs["full-path"])
        _xml_walk(container, b, start=start)
        opf_path = _resolve("", roots[0]) if roots else ""
        if opf_path and z.has(opf_path):
            manifest, spine = {}, []

            def start_opf(name, attrs):
                if name == "item":
                    manifest[attrs.get("id", "")] = attrs.get("href", "")
                elif name == "itemref":
                    spine.append(attrs.get("idref", ""))
            _xml_walk(z.read(opf_path, 8 * 1024 * 1024), b, start=start_opf)
            base = posixpath.dirname(opf_path)
            for idref in spine:
                part = _resolve(base, unquote(manifest.get(idref, "")).split("#")[0])
                if part and z.has(part):
                    spine_parts.append(part)
    except _Stop as s:
        if s.reason != "damaged":              # only a broken helper part is skipped
            raise
        spine_parts = []
    if not spine_parts:
        spine_parts = sorted(n for n in z.names if n.lower().endswith((".xhtml", ".html", ".htm")))
    sections, truncated = [], False
    for n, part in enumerate(spine_parts[:CHAPTERS_MAX], 1):
        b.check()
        raw = z.read(part, 12 * 1024 * 1024).decode("utf-8", "replace")
        text = clean_text(html_to_text(raw))
        if text:
            try:
                sections.append(("chapter %d" % n, b.take(text)))
            except _Truncate:
                truncated = True
                break
    return sections, len(spine_parts), truncated or b.truncated or len(spine_parts) > CHAPTERS_MAX


# ── HTML and RTF: small linear scanners ──
_BLOCK_TAGS = frozenset(
    "p div br li ul ol tr table section article header footer h1 h2 h3 h4 h5 h6 blockquote "
    "pre hr dt dd figcaption nav aside form".split())
_TAG_NAME = re.compile(r"(/?)([A-Za-z][A-Za-z0-9:-]*)")


def html_to_text(src):
    """Visible text of an HTML/XHTML/SVG string. Scripts, styles and comments are
    dropped, tags become line breaks or nothing. Linear in the input: every step
    moves forward and a tag with no ">" ends the scan."""
    out = []
    i, n = 0, len(src)
    low = src.lower()
    while i < n:
        j = src.find("<", i)
        if j < 0:
            out.append(src[i:])
            break
        out.append(src[i:j])
        if src.startswith("<!--", j):
            k = src.find("-->", j + 4)
            if k < 0:
                break
            i = k + 3
            continue
        m = _TAG_NAME.match(src, j + 1)
        if not m:
            out.append("<")
            i = j + 1
            continue
        k = src.find(">", m.end())
        if k < 0:
            break
        name = m.group(2).lower()
        if name in ("script", "style") and not m.group(1):
            close = low.find("</" + name, k)
            if close < 0:
                break
            k2 = src.find(">", close)
            i = n if k2 < 0 else k2 + 1
            continue
        if name in _BLOCK_TAGS:
            out.append("\n")
        i = k + 1
    return html.unescape("".join(out))


_RTF_TOKEN = re.compile(
    r"\\([a-zA-Z]{1,32})(-?\d{1,10})?[ ]?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z'])|([{}])|([^\\{}]+)")
_RTF_SKIP = frozenset(
    "fonttbl colortbl stylesheet info pict object objdata header footer headerl headerr "
    "headerf footerl footerr footerf themedata colorschememapping latentstyles datastore "
    "xmlnstbl listtable listoverridetable rsidtbl generator private1 bkmkstart bkmkend "
    "fldinst falt panose".split())


def rtf_to_text(raw, b):
    """Plain text of an RTF string (latin-1 decoded bytes). Destinations that hold
    fonts, styles, pictures or embedded objects are skipped, \\uN and \\'hh are
    decoded, nothing else is interpreted."""
    out = []
    stack = []                       # (skip, uc) per open group
    skip, uc, codepage = False, 1, "cp1252"
    pending_hex = bytearray()
    skip_chars = 0
    count = 0

    def flush_hex():
        if pending_hex:
            try:
                out.append(bytes(pending_hex).decode(codepage, "replace"))
            except LookupError:
                out.append(bytes(pending_hex).decode("cp1252", "replace"))
            pending_hex.clear()

    for m in _RTF_TOKEN.finditer(raw):
        count += 1
        if count % 20000 == 0:
            b.check()
        word, num, hexa, sym, brace, plain = m.groups()
        if hexa is not None:
            if skip_chars:
                skip_chars -= 1
            elif not skip:
                pending_hex.append(int(hexa, 16))
            continue
        flush_hex()
        if brace == "{":
            if len(stack) >= RTF_DEPTH_MAX:
                raise _Stop("unsafe")
            stack.append((skip, uc))
        elif brace == "}":
            if stack:
                skip, uc = stack.pop()
        elif plain is not None:
            if skip_chars:
                cut = min(skip_chars, len(plain))
                skip_chars -= cut
                plain = plain[cut:]
            if not skip:
                out.append(plain.replace("\r", "").replace("\n", ""))
        elif sym is not None:
            if sym == "*":
                skip = True
            elif not skip:
                if sym == "~":
                    out.append(" ")
                elif sym in "\\{}":
                    out.append(sym)
                elif sym == "\n" or sym == "\r":
                    out.append("\n")
        else:
            w = word.lower()
            if w in _RTF_SKIP:
                skip = True
            elif w == "ansicpg" and num:
                codepage = "cp" + num
            elif w == "uc" and num:
                uc = max(int(num), 0)
            elif w == "u" and num and not skip:
                code = int(num)
                if code < 0:
                    code += 65536
                out.append(chr(code) if 0 <= code <= 0x10FFFF else "")
                skip_chars = uc
            elif skip:
                continue
            elif w in ("par", "line", "row", "sect", "page"):
                out.append("\n")
            elif w in ("tab",):
                out.append("\t")
            elif w == "cell":
                out.append(" | ")
            elif w in ("emdash",):
                out.append("\u2014")
            elif w in ("endash",):
                out.append("\u2013")
            elif w in ("lquote", "rquote"):
                out.append("'")
            elif w in ("ldblquote", "rdblquote"):
                out.append('"')
            elif w == "bullet":
                out.append("\u2022")
    flush_hex()
    # A character outside the BMP comes as two surrogate halves: join them.
    return "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


# ── plain text ──
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"      # old .doc/.xls/.ppt, or an encrypted .docx
_BINARY_MAGIC = (
    b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe", b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c", b"\x1f\x8b\x08",
    b"\xfd7zXZ\x00", b"OggS\x00", b"fLaC", b"\x1aE\xdf\xa3", b"SQLite format 3\x00",
    _OLE_MAGIC, b"%!PS-Adobe", b"wOFF", b"wOF2", b"\x00asm",
)
# A password-protected modern Office file is an OLE container holding this stream.
_ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")


def _looks_binary(head):
    if not head:
        return False
    if b"\x00" in head:
        return True
    ctrl = sum(1 for c in head if c < 9 or 13 < c < 32 and c != 27)
    return ctrl / len(head) > 0.02


def _decode_text(data):
    """Text of a text-like file, or None for binary. UTF-8 first (strict), a BOM,
    UTF-16 without one, else charset_normalizer on a sample, else cp1252."""
    if not data:
        return ""
    for bom, enc in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF32_LE, "utf-32"),
                     (codecs.BOM_UTF32_BE, "utf-32"), (codecs.BOM_UTF16_LE, "utf-16"),
                     (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            try:
                return data.decode(enc)
            except UnicodeError:
                return data.decode(enc, "replace")
    head = data[:65536]
    if b"\x00" in head:
        even = head[0::2].count(0) / max(len(head[0::2]), 1)
        odd = head[1::2].count(0) / max(len(head[1::2]), 1)
        if odd > 0.3 > even or even > 0.3 > odd:
            enc = "utf-16-le" if odd > even else "utf-16-be"
            try:
                return data.decode(enc)
            except UnicodeError:
                return data.decode(enc, "replace")
        return None
    if _looks_binary(head):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    enc = None
    try:
        import charset_normalizer
        best = charset_normalizer.from_bytes(head).best()
        enc = best.encoding if best is not None else None
    except Exception:
        enc = None
    for candidate in (enc, "cp1252"):
        if not candidate:
            continue
        try:
            text = data.decode(candidate, "replace")
        except (LookupError, UnicodeError):
            continue
        if text.count("\ufffd") <= max(len(text) * 0.05, 3):
            return text
    return None


# ── pictures ──
def _read_image(data, b):
    from PIL import Image, ImageOps            # plain import: PyInstaller bundles Pillow
    # Pillow chatters about damaged files (truncated data, bad EXIF) through warnings:
    # a damaged picture is simply read as far as it goes. A "decompression bomb" warning
    # is promoted to an error (the pixel cap below is lower, so it never fires).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        return _decode_picture(Image, ImageOps, data, b)


def _decode_picture(Image, ImageOps, data, b):
    b.check()
    try:
        im = Image.open(io.BytesIO(data), formats=list(_PIL_FORMATS))
    except Image.DecompressionBombError:
        raise _Stop("too_big", "This picture is too big to read here.")
    with im:
        fmt = _PIL_FORMATS.get(im.format or "", "")
        if not fmt:
            raise _Stop("unsupported")
        w, h = im.size
        if w < 1 or h < 1:
            raise _Stop("damaged")
        cap = IMAGE_PIXELS_MAX_JPEG if fmt == "jpeg" else IMAGE_PIXELS_MAX
        if w * h > cap:
            raise _Stop("too_big", "This picture is too big to read here.")
        if fmt == "jpeg":
            try:
                im.draft("RGB", (IMAGE_SIDE * 2, IMAGE_SIDE * 2))
            except Exception:
                pass
        try:
            im.seek(0)
        except Exception:
            pass
        im.load()
        b.check()
        try:
            # (exif_transpose hands back a full-size COPY even when there is nothing to turn)
            if im.getexif().get(0x0112, 1) in (2, 3, 4, 5, 6, 7, 8):
                im = ImageOps.exif_transpose(im)
        except Exception:
            pass
        has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        try:
            # Make it small FIRST, flatten it after: a 40-megapixel picture would otherwise
            # need three or four full-size copies in memory at once (700 MB for one PNG).
            if im.mode in ("P", "PA", "1"):
                im = im.convert("RGBA" if has_alpha else "RGB")
            im.thumbnail((IMAGE_SIDE, IMAGE_SIDE), Image.Resampling.LANCZOS, reducing_gap=2.0)
        except Exception:
            pass
        try:
            if has_alpha:
                rgba = im.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                im = flat
            else:
                im = im.convert("RGB")
        except Exception:
            im = im.convert("L").convert("RGB")
        side = IMAGE_SIDE
        quality = 80
        while True:
            b.check()
            work = im.copy()
            work.thumbnail((side, side), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            work.save(buf, "JPEG", quality=quality, optimize=True)
            jpeg = buf.getvalue()
            if len(jpeg) <= IMAGE_BYTES_MAX or (quality <= 45 and side <= 400):
                break
            if quality > 45:
                quality -= 12
            else:
                side = int(side * 0.8)
        return fmt, jpeg, work.size[0], work.size[1]


# ── PDF ──
_PDF_CAPS = (
    "maximum_declared_stream_length", "array_based_stream_maximum_output_length",
    "jbig2_maximum_output_length", "lzw_maximum_output_length",
    "run_length_maximum_output_length", "zlib_maximum_output_length",
    "brotli_maximum_output_length", "image_maximum_buffer_size")


def _pdf_limits(pypdf):
    """A context manager that tightens pypdf's own safety limits for this read (they
    are per execution context, so only this reader thread is affected): streams may
    not inflate past PDF_STREAM_MAX, the page tree stays small, forms are not
    re-entered over and over, and the external jbig2dec program is never started."""
    # pypdf reports what it repaired, and some of its messages quote bytes or names out of
    # the document: none of that belongs in transcribe.log.
    pdf_log = logging.getLogger("pypdf")
    pdf_log.setLevel(logging.CRITICAL)
    pdf_log.propagate = False
    config_cls = getattr(pypdf, "Configuration", None)
    apply = getattr(pypdf, "apply_configuration", None)
    if config_cls is not None and apply is not None:
        have = {f.name for f in dataclasses.fields(config_cls)}
        wanted = {name: PDF_STREAM_MAX for name in _PDF_CAPS}
        wanted.update(stream_decoding_work_maximum_length=100_000_000,
                      page_tree_maximum_entries=20_000, page_tree_maximum_depth=40,
                      xform_maximum_invocations_per_extraction=500, jbig2dec_binary=None)
        return apply(**{k: v for k, v in wanted.items() if k in have})
    try:                                        # an older pypdf: the module constants
        from pypdf import filters
        for name in ("ZLIB_MAX_OUTPUT_LENGTH", "LZW_MAX_OUTPUT_LENGTH",
                     "RUN_LENGTH_MAX_OUTPUT_LENGTH", "JBIG2_MAX_OUTPUT_LENGTH",
                     "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH", "MAX_DECLARED_STREAM_LENGTH",
                     "FLATE_MAX_BUFFER_SIZE"):
            if hasattr(filters, name):
                setattr(filters, name, min(getattr(filters, name), PDF_STREAM_MAX))
        filters.JBIG2DEC_BINARY = None
    except Exception:
        logger.debug("pypdf caps not lowered", exc_info=True)
    return contextlib.nullcontext()


def _read_pdf(data, b):
    import pypdf                                # plain import: PyInstaller bundles pypdf
    with _pdf_limits(pypdf):
        return _read_pdf_pages(pypdf, data, b)


def _read_pdf_pages(pypdf, data, b):
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
    except RecursionError:
        raise _Stop("unsafe")
    except Exception:
        raise _Stop("damaged")
    b.check()
    if getattr(reader, "is_encrypted", False):
        # Only the open-without-a-password check every viewer makes. A password is
        # never guessed or asked for.
        try:
            opened = reader.decrypt("")
            if not opened:
                raise _Stop("password")
        except _Stop:
            raise
        except Exception as e:
            raise _Stop("protected" if type(e).__name__ == "DependencyError" else "password")
    try:
        total = len(reader.pages)
    except Exception:
        raise _Stop("damaged")
    if total <= 0:
        raise _Stop("damaged")
    sections, failures, visible = [], 0, 0
    truncated = total > PDF_PAGES_MAX
    for i in range(min(total, PDF_PAGES_MAX)):
        b.check()
        try:
            text = reader.pages[i].extract_text() or ""
        except _Stop:
            raise
        except Exception:
            failures += 1
            if failures > 25:
                break
            continue
        text = clean_text(text[:PDF_PAGE_CHARS_MAX])
        if not text:
            continue
        visible += len(re.sub(r"\s+", "", text))
        try:
            sections.append(("page %d" % (i + 1), b.take(text)))
        except _Truncate:
            truncated = True
            break
    if visible < 3:
        # No text layer: a scan. There is no OCR in the app and the pages are not
        # rendered, so say so, with the page count.
        raise _Stop("scanned", MESSAGES["scanned"][:-1] + " (it's a scan, %d page%s)."
                    % (total, "" if total == 1 else "s"))
    return sections, total, truncated or b.truncated


# ── the dispatcher ──
def _sniff(data, ext):
    """(fmt, how) for the content: how is "zip" for zip containers (fmt then comes
    from the parts), "image" is a fmt from magic, "" for a plain candidate."""
    head = data[:16]
    if data[:24].find(b"%PDF-") in range(0, 9):
        return "pdf"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"BM") and data[6:10] == b"\x00\x00\x00\x00":
        return "bmp"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[:8].lstrip().startswith(b"{\\rtf"):
        return "rtf"
    for magic in _BINARY_MAGIC:
        if data.startswith(magic):
            if magic == _OLE_MAGIC and data.find(_ENCRYPTED_PACKAGE) >= 0:
                return "protected"
            return "binary"
    if data[:2] == b"MZ" and b"PE\x00\x00" in data[:1024]:
        return "binary"
    if data[4:8] == b"ftyp" or (data[:4] == b"RIFF" and data[8:12] in (b"WAVE", b"AVI ")):
        return "binary"
    return ""


def _zip_format(z, b):
    names = z.names
    if "word/document.xml" in names:
        return "docx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "content.xml" in names and "mimetype" in names:
        try:
            mime = z.read("mimetype", 256).decode("ascii", "ignore").strip()
        except _Stop:
            mime = ""
        return {"application/vnd.oasis.opendocument.text": "odt",
                "application/vnd.oasis.opendocument.presentation": "odp",
                "application/vnd.oasis.opendocument.spreadsheet": "ods"}.get(mime, "")
    if "META-INF/container.xml" in names and "mimetype" in names:
        try:
            if z.read("mimetype", 256).decode("ascii", "ignore").strip() == "application/epub+zip":
                return "epub"
        except _Stop:
            pass
    return ""


def _parse(data, name, b):
    """The whole job for ``data`` (already in memory): a ReadResult, or a raised
    _Stop (which says what the file looked like, for the failure's type class).
    Runs inside the watchdog thread."""
    try:
        return _parse_content(data, name, b)
    except _Stop as stop:
        if not stop.fmt:
            kind = _sniff(data, "")
            stop.fmt = kind if kind in FORMATS else ""
        raise


def _parse_content(data, name, b):
    size = len(data)
    sha = hashlib.sha1(data, usedforsecurity=False).hexdigest()[:16]
    if size == 0:
        raise _Stop("empty")
    ext = os.path.splitext(name or "")[1].lower()
    kind = _sniff(data, ext)
    fmt, sections, pages, truncated = "", [], 0, False
    image, width, height = b"", 0, 0
    if kind == "zip":
        try:
            z = _Zip(data)
            fmt = _zip_format(z, b)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, OSError, EOFError,
                zlib.error, RuntimeError, ValueError):
            raise _Stop("damaged" if ext in _KNOWN_EXT else "unsupported")
        if not fmt:
            raise _Stop("unsupported")        # an archive: never opened
        reader = {"docx": _read_docx, "pptx": _read_pptx, "xlsx": _read_xlsx,
                  "epub": _read_epub}.get(fmt)
        try:
            if reader is not None:
                sections, pages, truncated = reader(z, b)
            else:
                sections, pages, truncated = _read_odf(z, b, fmt)
        except _Stop as stop:
            stop.fmt = fmt
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, EOFError, zlib.error,
                RuntimeError, ValueError, UnicodeError, OSError, NotImplementedError):
            raise _Stop("damaged")
    elif kind == "pdf":
        fmt = "pdf"
        sections, pages, truncated = _read_pdf(data, b)
    elif kind in ("png", "jpeg", "gif", "bmp", "webp", "tiff"):
        try:
            fmt, image, width, height = _read_image(data, b)
        except _Stop:
            raise
        except Exception:
            raise _Stop("damaged")
    elif kind == "protected":
        raise _Stop("protected")
    elif kind == "binary":
        raise _Stop("unsupported")
    elif kind == "rtf":
        fmt = "rtf"
        cut = len(data) > TEXT_BYTES_MAX
        text = clean_text(rtf_to_text(data[:TEXT_BYTES_MAX].decode("latin-1"), b))
        try:
            sections = [("", b.take(text))] if text else []
        except _Truncate:
            truncated = True
        truncated = truncated or b.truncated or cut
    else:
        # Only the first TEXT_BYTES_MAX are decoded: the character budget keeps far less, and
        # a 50 MB text file took 700-900 MB of memory (and up to 27 s) to turn into text.
        cut = len(data) > TEXT_BYTES_MAX
        text = _decode_text(data[:TEXT_BYTES_MAX] if cut else data)
        if text is None:
            raise _Stop("damaged" if ext in _KNOWN_EXT else "unsupported")
        head = text[:2048].lstrip().lower()
        if ext in _HTML_EXT or head.startswith(("<!doctype html", "<html")) or _SVG_START.match(head):
            fmt = "html"
            text = html_to_text(text)
        else:
            fmt = "csv" if ext in _CSV_EXT else "text"
        text = clean_text(text)
        try:
            sections = [("", b.take(text))] if text else []
        except _Truncate:
            truncated = True
        truncated = truncated or b.truncated or cut
    type_class, unit = FORMATS.get(fmt, ("other", ""))
    if image:
        return _result(ok=True, kind="image", fmt=fmt, type_class=type_class, image=image,
                       width=width, height=height, size=size, sha=sha)
    # Every format, once more: XML allows bidi overrides, zero-width and "tag" characters
    # (invisible to a person, readable to a model: a way to hide instructions in a .docx),
    # so the Office and OpenDocument readers - which end here without a clean_text of
    # their own - must not keep them.
    sections = tuple((l, clean_text(t)) for l, t in sections if t and t.strip())
    sections = tuple((l, t) for l, t in sections if t)
    if not sections:
        raise _Stop("no_text")
    return _result(ok=True, kind="text", fmt=fmt, type_class=type_class, sections=sections,
                   pages=pages, unit=unit, truncated=bool(truncated), size=size, sha=sha)


def _file_job(path, b):
    """The file stage, then the parse: open ONCE, read through a capped stream."""
    try:
        path = os.fspath(path)
        name = os.path.basename(path)
        info = os.lstat(path)
    except (OSError, TypeError, ValueError):
        raise _Stop("open")
    if stat.S_ISLNK(info.st_mode):
        raise _Stop("link")
    if stat.S_ISDIR(info.st_mode):
        raise _Stop("folder")
    if not stat.S_ISREG(info.st_mode):
        raise _Stop("unsupported")
    if info.st_size > FILE_BYTES_MAX:
        raise _Stop("too_big")
    try:
        with open(path, "rb") as fh:
            now = os.fstat(fh.fileno())
            if not stat.S_ISREG(now.st_mode) or (
                    info.st_ino and now.st_ino and info.st_ino != now.st_ino):
                raise _Stop("open")             # swapped between the check and the open
            data = fh.read(FILE_BYTES_MAX + 1)
    except OSError:
        raise _Stop("open")
    if len(data) > FILE_BYTES_MAX:
        raise _Stop("too_big")
    b.check()
    result = _parse(data, name, b)
    return result


class _Abandon(BaseException):
    """Raised INTO a reader thread that was given up on. A BaseException, so a parser's
    own ``except Exception`` can't swallow it and carry on."""


_ABANDONED = []                  # reader threads that were given up on and are being stopped
_ABANDONED_MAX = 4               # this many still running: no new file is started
_abandoned_lock = threading.Lock()


def _abandoned_running():
    with _abandoned_lock:
        _ABANDONED[:] = [t for t in _ABANDONED if t.is_alive()]
        return len(_ABANDONED)


def _reap(thread):
    """Stop a reader thread that was given up on. Python cannot kill a thread, so
    ``_Abandon`` is raised inside it (at its next bytecode) until it ends: without this a
    13 KB PDF whose one page takes an hour to extract would keep a CPU core, and a share
    of the GUI's interpreter lock, until the app was closed. Best effort: a thread inside
    one long C call ends when that call does. Never raises."""
    with _abandoned_lock:
        _ABANDONED.append(thread)

    def stop():
        try:
            import ctypes
            raise_in = ctypes.pythonapi.PyThreadState_SetAsyncExc
            for _ in range(80):
                if not thread.is_alive() or thread.ident is None:
                    return
                raise_in(ctypes.c_ulong(thread.ident), ctypes.py_object(_Abandon))
                thread.join(0.25)
        except Exception:
            logger.debug("a document reader could not be stopped", exc_info=True)

    try:
        threading.Thread(target=stop, name="gusan-doc-reaper", daemon=True).start()
    except Exception:
        pass


def _guarded(work, cancel, seconds):
    """Run ``work(budget)`` in a watchdog thread; ALWAYS returns a ReadResult within
    about ``seconds`` + a short grace, however the reader behaves. A reader that is given
    up on is stopped (see _reap); while several of those are still running no new file
    is started."""
    if _abandoned_running() >= _ABANDONED_MAX:
        return failure("busy")
    b = _Budget(seconds, cancel)
    box = {}
    done = threading.Event()

    def run():
        try:
            try:
                box["r"] = work(b)
            except _Stop as s:
                box["r"] = failure(s.reason, s.message, fmt=s.fmt,
                                   type_class=FORMATS.get(s.fmt, ("other", ""))[0])
            except MemoryError:
                box["r"] = failure("too_big")
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                if not b.aborted:                  # (an abandoned reader is stopped on purpose)
                    logger.debug("document read failed", exc_info=True)
                box["r"] = failure("error")
        except BaseException:                      # a stop that arrived late, or an interrupt
            pass
        finally:
            done.set()

    worker = threading.Thread(target=run, name="gusan-doc-reader", daemon=True)
    worker.start()
    while not done.wait(0.05):
        if cancel is not None:
            try:
                stop = bool(cancel())
            except Exception:
                stop = False
            if stop:
                b.aborted = "cancelled"
                _reap(worker)
                return failure("cancelled")
        if time.monotonic() > b.deadline + _GRACE_S:
            b.aborted = "timeout"
            _reap(worker)
            return failure("timeout")
    result = box.get("r")
    return result if result is not None else failure("error")


def read_document(path, cancel=None, time_budget=TIME_BUDGET_S):
    """Read the file at ``path``. Blocking: call it from a worker thread. Never
    raises (except KeyboardInterrupt/SystemExit). ``cancel`` is an optional
    callable returning True to stop (e.g. an Event's is_set)."""
    return _guarded(lambda b: _file_job(path, b), cancel, time_budget)


def read_bytes(data, name="", cancel=None, time_budget=TIME_BUDGET_S):
    """The same for content already in memory (tests, drag of raw data)."""
    data = bytes(data or b"")
    if len(data) > FILE_BYTES_MAX:
        return failure("too_big")
    return _guarded(lambda b: _parse(data, name, b), cancel, time_budget)


__all__ = [
    "FILE_BYTES_MAX", "FORMATS", "MESSAGES", "REASONS", "ReadResult", "TIME_BUDGET_S",
    "clean_text", "failure", "html_to_text", "read_bytes", "read_document", "rtf_to_text",
    "safe_line", "safe_name", "strip_invisible",
]
