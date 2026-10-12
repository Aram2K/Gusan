"""Markdown for people: display, copy and paste (Gusan 2.0, D9/D10).

Markdown stays the machine format (prompts, history, notes.md). This one
module renders it when something is shown, copied or pasted. Qt-free and
stdlib only, so tests/ (PySide6 stubbed) can import it.

    parse_blocks(md)   -> [("head", level, text) | ("para", text) |
                           ("bullet", text) | ("num", n, text) |
                           ("task", done, text, line_index) | ("quote", text) |
                           ("code", lang, text)]
                          "para" keeps its line breaks ("\\n"). line_index is
                          the raw line in ``md.split("\\n")``, so
                          toggle_task_line(md, line_index, done) flips it.
                          "**Label:**" lines are headings of level 4.
    parse_notes(md)    -> the meeting notes view's 2-tuples, unchanged:
                          ("head", text) / ("para", text joined by " ") /
                          ("bullet", text) (numbered items too) /
                          ("task", (done, text, line_index)) / ("quote", text);
                          a code fence comes back as ("para", code).
    inline_html(text, code=False)  escape, **b**, *i* / _i_, `code` stripped
                          (code=True keeps it as <code>).
    strip_inline(text) no ** * _ ` markers.
    has_markup(md)     a line-start block marker, ** or `.
    to_plain(md)       nice plain text: headings on their own line (blank line
                          before), "• " bullets, "N. " numbers, "☐ "/"☑ " tasks.
                          ``md`` comes back unchanged when not has_markup(md).
    to_html(md)        a minimal clipboard fragment (<h3> <p> <ul> <ol> <b>
                          <i> <code> <pre> <blockquote>); no fonts, colours or
                          -qt- CSS.
    qt_html(md, colors=None)  the same structure for QLabel / QTextEdit.
    transcript_paragraphs(text, max_chars=550)  a meeting transcript as
                          readable paragraphs (sentence packing).
    parse_task / toggle_task_line / notes_plain  the meeting notes helpers.
"""

import html as _html
import re

# ── block patterns (the meeting notes view's, unchanged) ─────────────────────
_TASK_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*\[( |x|X)\]\s*(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_HEAD_RE = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD_HEAD_RE = re.compile(r"^\s*\*\*([^*]{2,60}?):?\*\*:?\s*$")
_OWNER_RE = re.compile(r"\(\s*Owner:\s*([^,)]*)(?:,\s*Due:\s*([^)]*))?\)", re.I)
_DUE_RE = re.compile(r"\(\s*Due:\s*([^)]*)\)", re.I)
_EMPTY = {"", "-", "—", "none", "n/a", "na", "tbd", "unassigned", "unknown", "unspecified", "not specified"}
# New: numbered items and code fences.
_NUM_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([^\s`]*)[^`]*$")
_RULE_RE = re.compile(r"[-*_]{3,}")

BOLD_HEAD_LEVEL = 4

# ── inline patterns ──────────────────────────────────────────────────────────
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w])_(?!\s)(.+?)(?<!\s)_(?![\w])")
_CODE_RE = re.compile(r"`([^`]+)`")

# A line that starts a Markdown block: # heading, - * • bullet, 1. / 1) item,
# - [ ] task, > quote, ``` / ~~~ fence.
_BLOCK_START_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]|[-*•][ \t]|[-*•][ \t]*\[[ xX]\]|\d{1,3}[.)][ \t]|>(?:[ \t]|$)|`{3}|~{3})",
    re.M)

TASK_OPEN = "☐"
TASK_DONE = "☑"
BULLET = "•"


def inline_html(text, code=False):
    """Escape, then **bold**, *italic* / _italic_; `code` as plain text
    (``code=True``: <code>code</code>). meeting_views.md_inline."""
    s = _html.escape(text or "", quote=False)
    s = _BOLD_RE.sub(r"<b>\1</b>", s)
    s = _ITALIC_STAR_RE.sub(r"<i>\1</i>", s)
    s = _ITALIC_UNDER_RE.sub(r"<i>\1</i>", s)
    s = _CODE_RE.sub(r"<code>\1</code>" if code else r"\1", s)
    return s


def strip_inline(text):
    """The text without ** * _ ` markers (and stripped)."""
    s = _BOLD_RE.sub(r"\1", text or "")
    s = _ITALIC_STAR_RE.sub(r"\1", s)
    s = _ITALIC_UNDER_RE.sub(r"\1", s)
    s = _CODE_RE.sub(r"\1", s)
    return s.strip()


def _strip_md(text):
    # meeting_views.strip_md (headings keep *italic* as typed).
    s = _BOLD_RE.sub(r"\1", text or "")
    s = _CODE_RE.sub(r"\1", s)
    return s.strip()


def has_markup(md):
    """True when ``md`` has a Markdown block marker at a line start, ** or `."""
    if not md:
        return False
    return "**" in md or "`" in md or bool(_BLOCK_START_RE.search(md))


# ── blocks ───────────────────────────────────────────────────────────────────
def _scan(md, join="\n"):
    """[(block, gap)] where gap = a blank line (or rule) came before it."""
    out = []
    para = []
    state = {"gap": False}

    def emit(block):
        out.append((block, state["gap"] and bool(out)))
        state["gap"] = False

    def flush():
        if para:
            emit(("para", join.join(para)))
            del para[:]

    # Split on "\n" only, exactly as toggle_task_line does: str.splitlines()
    # also breaks at \f, \x1c-\x1e, \x85,   and  , so one of those
    # in the notes would shift every later line_index and a ticked checkbox
    # would rewrite the wrong notes.md line.
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in (md or "").split("\n")]
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        s = raw.strip()
        if not s:
            flush()
            state["gap"] = True
            i += 1
            continue
        f = _FENCE_RE.match(raw)
        if f:
            flush()
            mark = f.group(1)
            closing = re.compile(r"^\s*%s{%d,}\s*$" % (re.escape(mark[0]), len(mark)))
            body = []
            i += 1
            while i < n and not closing.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1          # the closing fence (or the end)
            emit(("code", f.group(2), "\n".join(body)))
            continue
        i += 1
        m = _HEAD_RE.match(s)
        if m:
            flush()
            emit(("head", len(m.group(1)), _strip_md(m.group(2))))
            continue
        m = _BOLD_HEAD_RE.match(s)
        if m:
            flush()
            emit(("head", BOLD_HEAD_LEVEL, m.group(1).strip()))
            continue
        m = _TASK_RE.match(s)
        if m:
            flush()
            emit(("task", m.group(1).lower() == "x", m.group(2), i - 1))
            continue
        m = _NUM_RE.match(s)
        if m:
            flush()
            emit(("num", int(m.group(1)), m.group(2)))
            continue
        m = _BULLET_RE.match(s)
        if m:
            flush()
            emit(("bullet", m.group(1)))
            continue
        if s.startswith(">"):
            flush()
            emit(("quote", s.lstrip("> ").strip()))
            continue
        if _RULE_RE.fullmatch(s):
            flush()
            state["gap"] = True
            continue
        para.append(s)
    flush()
    return out


def parse_blocks(md):
    """Markdown -> typed blocks (see the module notes)."""
    return [block for block, _gap in _scan(md)]


def parse_notes(md):
    """The meeting notes view's blocks (meeting_views.parse_notes, same shape):
    ("head", text) / ("para", text) / ("bullet", text) /
    ("task", (done, text, line_index)) / ("quote", text)."""
    out = []
    for block in (b for b, _gap in _scan(md, " ")):
        kind = block[0]
        if kind == "head":
            out.append(("head", block[2]))
        elif kind == "num":
            out.append(("bullet", block[2]))
        elif kind == "task":
            out.append(("task", (block[1], block[2], block[3])))
        elif kind == "code":
            out.append(("para", block[2]))
        else:
            out.append((kind, block[1]))
    return out


def parse_task(text):
    """'`Fix X` (Owner: David, Due: Wed)' -> (task, owner, due)."""
    t = text or ""
    owner = due = ""
    m = _OWNER_RE.search(t)
    if m:
        owner = (m.group(1) or "").strip()
        due = (m.group(2) or "").strip()
        t = (t[:m.start()] + t[m.end():])
    else:
        d = _DUE_RE.search(t)
        if d:
            due = d.group(1).strip()
            t = t[:d.start()] + t[d.end():]
    t = t.replace("`", "").strip().rstrip(" -–—").strip()
    if owner.lower() in _EMPTY:
        owner = ""
    if due.lower() in _EMPTY:
        due = ""
    if due and not due.lower().startswith(("by ", "before ", "until ")):
        due = "by " + due
    return t, owner, due


def toggle_task_line(md, line_index, done):
    """The notes with one checkbox line flipped (other lines untouched)."""
    lines = (md or "").split("\n")
    if 0 <= line_index < len(lines):
        lines[line_index] = re.sub(r"\[( |x|X)\]", "[x]" if done else "[ ]", lines[line_index], count=1)
    return "\n".join(lines)


def notes_plain(md):
    """Notes as plain paragraphs (Word export): no #, - [ ] -> ☐ / ☑."""
    out = []
    for kind, data in parse_notes(md):
        if kind == "head":
            out.append(data)
        elif kind == "task":
            done, text, _i = data
            task, owner, due = parse_task(text)
            extra = " · ".join(x for x in (owner, due) if x)
            out.append(("☑ " if done else "☐ ") + task + (" (%s)" % extra if extra else ""))
        elif kind == "bullet":
            out.append("• " + _strip_md(data))
        else:
            out.append(_strip_md(data))
    return out


def _task_parts(text):
    """(task, ' (Owner · by Due)' or '') for a task line's text."""
    task, owner, due = parse_task(text)
    extra = " · ".join(x for x in (owner, due) if x)
    return task, (" (%s)" % extra if extra else "")


# ── plain text ───────────────────────────────────────────────────────────────
def to_plain(md):
    """Nice plain text for apps that only take text (see the module notes).
    Prose without markup comes back byte-identical."""
    if md is None:
        return ""
    if not has_markup(md):
        return md
    lines = []
    prev = None
    for block, gap in _scan(md):
        kind = block[0]
        if lines and (kind == "head" or (gap and prev != "head")):
            lines.append("")
        if kind == "head":
            lines.append(strip_inline(block[2]))
        elif kind == "para":
            lines.extend(strip_inline(line) for line in block[1].split("\n"))
        elif kind == "bullet":
            lines.append("%s %s" % (BULLET, strip_inline(block[1])))
        elif kind == "num":
            lines.append("%d. %s" % (block[1], strip_inline(block[2])))
        elif kind == "task":
            task, extra = _task_parts(block[2])
            lines.append("%s %s%s" % (TASK_DONE if block[1] else TASK_OPEN, strip_inline(task), extra))
        elif kind == "quote":
            lines.append(strip_inline(block[1]))
        elif kind == "code":
            lines.extend(block[2].split("\n"))
        prev = kind
    return "\n".join(lines)


# ── HTML ─────────────────────────────────────────────────────────────────────
def _groups(blocks):
    """Consecutive list items of one kind together: [(kind, [blocks])]."""
    groups = []
    for block in blocks:
        kind = block[0]
        if kind in ("bullet", "num", "task") and groups and groups[-1][0] == kind:
            groups[-1][1].append(block)
        else:
            groups.append((kind, [block]))
    return groups


def _task_html(block, code):
    task, extra = _task_parts(block[2])
    return "%s&nbsp;%s%s" % (TASK_DONE if block[1] else TASK_OPEN, inline_html(task, code),
                             _html.escape(extra, quote=False))


def to_html(md):
    """A minimal HTML fragment for the clipboard (Word, Outlook, Gmail, Docs,
    Notion, Slack, Teams): real headings, lists and bold, no styling."""
    parts = []
    for kind, items in _groups(parse_blocks(md)):
        b = items[0]
        if kind == "head":
            text = inline_html(b[2], code=True)
            parts.append("<h3>%s</h3>" % text if b[1] < BOLD_HEAD_LEVEL else "<p><b>%s</b></p>" % text)
        elif kind == "para":
            parts.append("<p>%s</p>" % "<br>".join(inline_html(line, code=True) for line in b[1].split("\n")))
        elif kind == "bullet":
            parts.append("<ul>%s</ul>" % "".join("<li>%s</li>" % inline_html(x[1], code=True) for x in items))
        elif kind == "num":
            start = "" if b[1] == 1 else ' start="%d"' % b[1]
            parts.append("<ol%s>%s</ol>" % (start, "".join("<li>%s</li>" % inline_html(x[2], code=True)
                                                          for x in items)))
        elif kind == "task":
            parts.append("<p>%s</p>" % "<br>".join(_task_html(x, True) for x in items))
        elif kind == "quote":
            parts.append("<blockquote>%s</blockquote>" % inline_html(b[1], code=True))
        elif kind == "code":
            parts.append("<pre>%s</pre>" % _html.escape(b[2], quote=False))
    return "\n".join(parts)


def qt_html(md, colors=None):
    """The same structure as to_html for in-app QLabel / QTextEdit display.
    ``colors`` (all optional): {"text", "muted", "head", "code_bg"} as CSS
    colours; done tasks and quotes use "muted"."""
    c = dict(colors or {})

    def style(*pairs):
        css = "".join("%s:%s;" % (k, v) for k, v in pairs if v)
        return ' style="%s"' % css if css else ""

    text_c, muted_c, head_c = c.get("text"), c.get("muted"), c.get("head") or c.get("text")
    p_style = style(("margin-top", "0px"), ("margin-bottom", "8px"), ("color", text_c))
    parts = []
    for kind, items in _groups(parse_blocks(md)):
        b = items[0]
        if kind == "head":
            text = inline_html(b[2], code=True)
            if b[1] < BOLD_HEAD_LEVEL:
                parts.append("<h3%s>%s</h3>" % (style(("margin-top", "4px"), ("margin-bottom", "6px"),
                                                      ("color", head_c)), text))
            else:
                parts.append("<p%s><b>%s</b></p>" % (style(("margin-top", "0px"), ("margin-bottom", "4px"),
                                                           ("color", head_c)), text))
        elif kind == "para":
            parts.append("<p%s>%s</p>" % (p_style, "<br>".join(inline_html(line, code=True)
                                                               for line in b[1].split("\n"))))
        elif kind in ("bullet", "num"):
            tag = "ul" if kind == "bullet" else "ol"
            start = ' start="%d"' % b[1] if kind == "num" and b[1] != 1 else ""
            li = "".join("<li>%s</li>" % inline_html(x[1] if kind == "bullet" else x[2], code=True)
                         for x in items)
            parts.append("<%s%s%s>%s</%s>" % (tag, start, style(("margin-top", "0px"), ("margin-bottom", "8px"),
                                                                ("color", text_c)), li, tag))
        elif kind == "task":
            rows = []
            for x in items:
                row = _task_html(x, True)
                if x[1] and muted_c:
                    row = '<span style="color:%s;">%s</span>' % (muted_c, row)
                rows.append(row)
            parts.append("<p%s>%s</p>" % (p_style, "<br>".join(rows)))
        elif kind == "quote":
            parts.append("<p%s>%s</p>" % (style(("margin-top", "0px"), ("margin-bottom", "8px"),
                                                 ("color", muted_c or text_c), ("font-style", "italic")),
                                           inline_html(b[1], code=True)))
        elif kind == "code":
            parts.append("<pre%s>%s</pre>" % (style(("margin-top", "0px"), ("margin-bottom", "8px"),
                                                    ("background-color", c.get("code_bg")),
                                                    ("color", text_c)),
                                              _html.escape(b[2], quote=False)))
    return "".join(parts)


# ── meeting transcripts (D10) ────────────────────────────────────────────────
# "Speaker 1: …", "You: …", "Anna: …", "Anna Lee: …" (meeting_views._SPK_RE).
# A name is 1-3 capitalised words: a transcript that starts "So here's the
# thing: …" is speech, not a label (its colon would otherwise be dropped).
SPEAKER_RE = re.compile(
    r"^((?:Speaker\s+(?:\d+|\?))|You|Them|[A-Z][\w.'-]{0,24}(?: [A-Z][\w.'-]{0,24}){0,2}):\s+(.*)$")
_CLOSERS = "\"'”’»)]"
# Where a sentence ends: . ! ? … ։ ՞ ؟ then closing quotes/brackets and a
# space; 。！？ need no space after them.
_SENTENCE_END_RE = re.compile(
    r"[.!?…։՞؟]+[%s]*\s+|[。！？]+[%s]*\s*" % (re.escape(_CLOSERS), re.escape(_CLOSERS)))
_WORD_RE = re.compile(r"\S+\s*")


def is_marker(line):
    """'— Resumed at 11:20 —' style lines (kept as they are)."""
    s = (line or "").strip()
    return len(s) > 1 and s.startswith("—") and s.endswith("—")


def split_speaker(line):
    """'Speaker 1: Hello' -> ('Speaker 1', 'Hello'); ('', line) without a label."""
    m = SPEAKER_RE.match((line or "").strip())
    return (m.group(1), m.group(2)) if m else ("", (line or "").strip())


def _sentences(text):
    """The text cut after each sentence end (pieces keep their spaces)."""
    pieces = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(text):
        pieces.append(text[start:m.end()])
        start = m.end()
    if start < len(text):
        pieces.append(text[start:])
    return pieces


def _words(piece, max_chars):
    """A piece longer than max_chars cut at word boundaries (hard cut for a
    single word longer than that)."""
    out = []
    cur = ""
    for w in _WORD_RE.findall(piece):
        while len(w) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            out.append(w[:max_chars])
            w = w[max_chars:]
        if cur and len(cur) + len(w.rstrip()) > max_chars:
            out.append(cur)
            cur = ""
        cur += w
    if cur:
        out.append(cur)
    return out


def _pack(text, max_chars):
    paras = []
    cur = ""
    for piece in _sentences(text):
        if len(piece.rstrip()) > max_chars:
            if cur:
                paras.append(cur)
            chunks = _words(piece, max_chars)
            paras.extend(chunks[:-1])
            cur = chunks[-1] if chunks else ""
            continue
        if cur and len(cur) + len(piece.rstrip()) > max_chars:
            paras.append(cur)
            cur = ""
        cur += piece
    if cur:
        paras.append(cur)
    return [p.strip() for p in paras if p.strip()]


def transcript_paragraphs(text, max_chars=550):
    """A transcript as readable paragraphs: each line is packed by sentences
    up to ``max_chars``; a "Speaker N:" / "Name:" label stays on the first
    paragraph of its turn; "— marker —" lines and short lines stay as they
    are (stripped). Blank lines are dropped."""
    max_chars = max(40, int(max_chars or 550))
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if len(s) <= max_chars or is_marker(s):
            out.append(s)
            continue
        m = SPEAKER_RE.match(s)
        if m:
            prefix, body = s[:m.start(2)], m.group(2)
        else:
            prefix, body = "", s
        paras = _pack(body, max_chars)
        if paras and prefix:
            paras[0] = prefix + paras[0]
        out.extend(paras)
    return out
