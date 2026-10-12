"""Write transcripts as real Word documents (.docx), stdlib only.

A .docx is an OPC zip of XML parts; the minimal package Word accepts is
[Content_Types].xml + _rels/.rels + word/document.xml (+ docProps/core.xml for
author metadata). Building it with zipfile keeps python-docx (and its lxml
binary dependency) out of the PyInstaller build for what is one export format.

All text is XML-escaped; formatting is direct (w:rPr) so no styles part is
needed. Also hosts the pure segment->paragraph grouping used by the
file-transcription tab, so it is unit-testable without Qt or models.

Paragraphs for build_docx are dicts: {"start", "speaker", "text"} (a
transcript, as group_segments makes them) plus these optional keys (Gusan
2.0, meeting notes in Word):
    "style"  "body" (default) | "heading" (bold 14 pt) | "subheading" (bold) |
             "bullet" (•) | "num" ("n": 1, 2 …) | "task" (☐, or ☑ with
             "done": True) | "quote" (italic, indented) | "code" (monospace) |
             "marker" (italic, muted: "— Resumed at 11:20 —")
    "md"     True: **bold** / *italic* in "text" become real bold / italic runs
    "label"  a bold name before the text ("Speaker 1", "Anna"), instead of
             the "speaker" index
    "after"  muted text after it ("(David · by Wed)" on a task)
notes_paragraphs(md) turns meeting notes Markdown into such paragraphs.
"""
import datetime
import html as _html
import re
import zipfile
from xml.sax.saxutils import escape

import rich_text

# Direct-formatting constants (half-points for sizes, hex for colors).
_TITLE_SIZE = 40      # 20pt
_HEAD_SIZE = 28       # 14pt
_META_SIZE = 18       # 9pt
_BODY_SIZE = 22       # 11pt
_CODE_SIZE = 20       # 10pt
_META_COLOR = "64748B"
_TS_COLOR = "94A3B8"
_SPEAKER_COLOR = "334155"
_CODE_FONT = "Consolas"
_LIST_INDENT = 400    # twips: the text of a list item / task
_LIST_HANG = 280      # the glyph hangs this far to the left of it

# XML 1.0 forbids C0 controls (except tab/LF/CR), lone surrogates and
# U+FFFE/U+FFFF - EVEN escaped. Whisper's byte-level decoder can emit such
# characters on noisy audio, and one of them makes Word refuse the whole
# document ("unreadable content") after a potentially hours-long job. Strip
# them from every string that enters an XML part.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def _xml_text(text):
    return escape(_XML_ILLEGAL.sub("", text or ""))


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '</Types>'
)

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    '</Relationships>'
)


def format_timestamp(seconds):
    """Seconds -> "MM:SS" under an hour, else "H:MM:SS"."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def group_segments(segments, max_gap=2.5, max_chars=550):
    """Merge timestamped transcript segments into readable paragraphs.

    A new paragraph starts on a speaker change, a silence gap of ``max_gap``
    seconds, or when the current one grows past ``max_chars`` - a wall-of-text
    docx is exactly what this feature must not produce.

    ``segments``: dicts with "start", "end", "text", optional "speaker".
    Returns dicts: {"start", "speaker" (int|None), "text"}.
    """
    paragraphs = []
    cur = None
    for seg in segments or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        spk = seg.get("speaker")
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        if (cur is None
                or spk != cur["speaker"]
                or start - cur["_last_end"] > max_gap
                or len(cur["text"]) + len(text) > max_chars):
            cur = {"start": start, "speaker": spk, "text": text, "_last_end": end}
            paragraphs.append(cur)
        else:
            cur["text"] += " " + text
            cur["_last_end"] = end
    for p in paragraphs:
        p.pop("_last_end", None)
    return paragraphs


def _run(text, *, bold=False, color=None, size=_BODY_SIZE, italic=False, font=None):
    props = []
    if font:
        props.append(f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:cs="{font}"/>')
    props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    # A line break inside the text is a real Word line break (<w:br/>).
    pieces = (text or "").split("\n")
    body = '<w:r><w:br/></w:r>'.join(
        f'<w:r><w:rPr>{"".join(props)}</w:rPr><w:t xml:space="preserve">{_xml_text(p)}</w:t></w:r>'
        for p in pieces)
    return body


_TAB = "<w:r><w:tab/></w:r>"


def _para(runs, *, space_after=160, space_before=0, indent=None, hanging=0, keep_next=False):
    props = [f'<w:spacing w:before="{space_before}" w:after="{space_after}"/>'
             if space_before else f'<w:spacing w:after="{space_after}"/>']
    if keep_next:
        props.insert(0, "<w:keepNext/>")
    if indent:
        props.append(f'<w:ind w:left="{indent}" w:hanging="{hanging}"/>' if hanging
                     else f'<w:ind w:left="{indent}"/>')
    return f'<w:p><w:pPr>{"".join(props)}</w:pPr>' + "".join(runs) + "</w:p>"


_INLINE_TAG_RE = re.compile(r"(</?b>|</?i>|</?code>)")


def inline_runs(text):
    """'**Fix** the *drift*' -> [("Fix", True, False), (" the ", False, False),
    ("drift", False, True)]: rich_text's inline Markdown as (text, bold, italic)."""
    out = []
    bold = italic = False
    for piece in _INLINE_TAG_RE.split(rich_text.inline_html(text or "")):
        if piece in ("<b>", "</b>"):
            bold = piece == "<b>"
        elif piece in ("<i>", "</i>"):
            italic = piece == "<i>"
        elif piece in ("<code>", "</code>"):
            continue
        elif piece:
            out.append((_html.unescape(piece), bold, italic))
    return out


def _text_runs(p, *, bold=False, italic=False, color=None, size=_BODY_SIZE, font=None):
    """The paragraph's text as runs (**bold** / *italic* when p["md"])."""
    text = p.get("text", "")
    if not p.get("md"):
        return [_run(text, bold=bold, italic=italic, color=color, size=size, font=font)]
    return [_run(t, bold=bold or b, italic=italic or i, color=color, size=size, font=font)
            for t, b, i in inline_runs(text)]


def _styled_para(p, style):
    """One notes paragraph (see the module notes) as Word XML."""
    after = p.get("after") or ""
    tail = [_run(" " + after, color=_META_COLOR)] if after else []
    if style == "heading":
        return _para(_text_runs(p, bold=True, size=_HEAD_SIZE), space_before=240, space_after=100, keep_next=True)
    if style == "subheading":
        return _para(_text_runs(p, bold=True), space_before=160, space_after=60, keep_next=True)
    if style in ("bullet", "num", "task"):
        if style == "bullet":
            glyph, color = rich_text.BULLET, None
        elif style == "num":
            glyph, color = "%s." % (p.get("n") or 1), None
        else:
            done = bool(p.get("done"))
            glyph, color = (rich_text.TASK_DONE if done else rich_text.TASK_OPEN), (_META_COLOR if done else None)
        runs = [_run(glyph, color=color), _TAB] + _text_runs(p, color=color) + tail
        return _para(runs, space_after=80, indent=_LIST_INDENT, hanging=_LIST_HANG)
    if style == "quote":
        return _para(_text_runs(p, italic=True, color=_META_COLOR) + tail, indent=_LIST_INDENT)
    if style == "code":
        return _para([_run(p.get("text", ""), size=_CODE_SIZE, font=_CODE_FONT)])
    if style == "marker":
        return _para(_text_runs(p, italic=True, color=_META_COLOR))
    return None


def speaker_name(spk, names=None):
    if names and spk in names:
        return names[spk]
    if spk is None:
        return ""
    return f"Speaker {int(spk) + 1}"


def notes_paragraphs(md):
    """Meeting notes Markdown -> styled build_docx paragraphs: ## headings bold
    14 pt, **Label:** lines bold, bullets / numbers / tasks indented with
    • / 1. / ☐ ☑ (owner and due muted after the task), **bold** runs."""
    out = []
    for b in rich_text.parse_blocks(md):
        kind = b[0]
        if kind == "head":
            out.append({"style": "heading" if b[1] < rich_text.BOLD_HEAD_LEVEL else "subheading",
                        "text": b[2], "md": True})
        elif kind == "para":
            out.append({"style": "body", "text": b[1], "md": True})
        elif kind == "bullet":
            out.append({"style": "bullet", "text": b[1], "md": True})
        elif kind == "num":
            out.append({"style": "num", "n": b[1], "text": b[2], "md": True})
        elif kind == "task":
            task, owner, due = rich_text.parse_task(b[2])
            extra = " · ".join(x for x in (owner, due) if x)
            out.append({"style": "task", "done": bool(b[1]), "text": task, "md": True,
                        "after": "(%s)" % extra if extra else ""})
        elif kind == "quote":
            out.append({"style": "quote", "text": b[1], "md": True})
        elif kind == "code":
            out.append({"style": "code", "text": b[2]})
    return out


def build_docx(title, paragraphs, meta_line="", speaker_names=None,
               include_timestamps=True):
    """The complete .docx as bytes.

    ``paragraphs``: output of :func:`group_segments`, or styled paragraphs
    (see the module notes, e.g. :func:`notes_paragraphs`).
    """
    body = [_para([_run(title or "Transcript", bold=True, size=_TITLE_SIZE)],
                  space_after=80)]
    if meta_line:
        body.append(_para([_run(meta_line, color=_META_COLOR, size=_META_SIZE)],
                          space_after=280))
    for p in paragraphs or []:
        style = p.get("style") or "body"
        styled = _styled_para(p, style) if style != "body" else None
        if styled is not None:
            body.append(styled)
            continue
        runs = []
        if include_timestamps:
            runs.append(_run(f"[{format_timestamp(p.get('start', 0))}]  ",
                             color=_TS_COLOR, size=_META_SIZE))
        label = p.get("label") or speaker_name(p.get("speaker"), speaker_names)
        if label:
            runs.append(_run(f"{label}:  ", bold=True, color=_SPEAKER_COLOR))
        runs += _text_runs(p)
        if p.get("after"):
            runs.append(_run(" " + p["after"], color=_META_COLOR))
        body.append(_para(runs))
    if len(body) == (2 if meta_line else 1):
        body.append(_para([_run("(No speech was detected in this file.)",
                                color=_META_COLOR)]))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>' + "".join(body) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1200" w:right="1200" w:bottom="1200" w:left="1200"/>'
        '</w:sectPr></w:body></w:document>'
    )

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties'
        ' xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:dcterms="http://purl.org/dc/terms/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{_xml_text(title or "Transcript")}</dc:title>'
        '<dc:creator>Gusan</dc:creator>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        '</cp:coreProperties>'
    )

    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", document)
        z.writestr("docProps/core.xml", core)
    return buf.getvalue()


def save_docx(path, title, paragraphs, **kwargs):
    data = build_docx(title, paragraphs, **kwargs)
    with open(path, "wb") as f:
        f.write(data)
    return path


def safe_filename(name, fallback="transcript"):
    """A filesystem-safe stem for the suggested save name."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name or "").strip().strip(".")
    return stem or fallback
