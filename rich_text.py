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
                          sections=True (the notes view): a topic heading under a
                          section ("### Topic" below "## Discussion") is
                          ("subhead", text), and a section with nothing under it
                          (or only "None.") is left out.
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
    answer_text(md, streaming=False)  a model's answer ready for qt_html: links, images and tables as plain
                          words, break points in long strings; streaming_markdown(md) hides a half-written
                          ** or ` while an answer is still arriving (or was cut short), close_markers(md)
                          a ** with no pair in a finished one.
    transcript_paragraphs(text, max_chars=550)  a meeting transcript as
                          readable paragraphs (sentence packing).
    parse_task / toggle_task_line / notes_plain  the meeting notes helpers.
    split_task(text)   '(Owner: X, Due: Y)' in every shape a model writes it ->
                          (task, owner, due); a generic side ("Them", "Speaker 2"),
                          an empty or placeholder value is no owner.
    clean_notes(md, me="")  the notes with every action item in one clean shape
                          and no empty section, no wrapper (a code fence, a
                          "Here are the notes:") and no table: what notes.md,
                          Notion and the Markdown export get.
    heading_roles(blocks)   which headings are sections and which are topics.
    inline_runs(text)  '**Fix** the *drift*' -> [(text, bold, italic)] for Word.
"""

import html as _html
import re

import notes_prompt

# ── block patterns (the meeting notes view's, unchanged) ─────────────────────
_TASK_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*\[( |x|X)\]\s*(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")
_HEAD_RE = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD_HEAD_RE = re.compile(r"^\s*\*\*([^*]{2,60}?):?\*\*:?\s*$")
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


_INLINE_TAG_RE = re.compile(r"(</?b>|</?i>|</?code>)")


def inline_runs(text):
    """'**Fix** the *drift*' -> [("Fix", True, False), (" the ", False, False), ("drift", False, True)]: the inline
    Markdown as (text, bold, italic) runs (the Word export's)."""
    out = []
    bold = italic = False
    for piece in _INLINE_TAG_RE.split(inline_html(text or "")):
        if piece in ("<b>", "</b>"):
            bold = piece == "<b>"
        elif piece in ("<i>", "</i>"):
            italic = piece == "<i>"
        elif piece in ("<code>", "</code>"):
            continue
        elif piece:
            out.append((_html.unescape(piece), bold, italic))
    return out


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


def parse_notes(md, sections=False):
    """The meeting notes view's blocks (meeting_views.parse_notes, same shape):
    ("head", text) / ("para", text) / ("bullet", text) /
    ("task", (done, text, line_index)) / ("quote", text).
    ``sections=True`` (what the notes view shows): a topic heading under a section
    is ("subhead", text) instead (see heading_roles) and a section with nothing
    under it - or only a "None." line - is left out. line_index is still the raw
    line of ``md``, so a ticked box finds its line."""
    blocks = [b for b, _gap in _scan(md, " ")]
    roles = {}
    if sections:
        blocks = _without_empty_sections(blocks)
        roles = heading_roles(blocks)
    out = []
    for i, block in enumerate(blocks):
        kind = block[0]
        if kind == "head":
            out.append(("subhead" if roles.get(i) == "sub" else "head", block[2]))
        elif kind == "num":
            out.append(("bullet", block[2]))
        elif kind == "task":
            out.append(("task", (block[1], block[2], block[3])))
        elif kind == "code":
            out.append(("para", block[2]))
        else:
            out.append((kind, block[1]))
    return out


# ── sections, topics and empty sections ──────────────────────────────────────
def heading_roles(blocks):
    """For parse_blocks() output: {index: "section" | "sub"} for each head. A heading is a sub-heading (a topic) when it
    is level 3 or deeper and the notes also have a level 1-2 heading (the section titles): "### Topic" under
    "## Discussion". Notes that only have deep headings keep them all as sections."""
    levels = [b[1] for b in blocks if b[0] == "head"]
    has_sections = any(lv <= 2 for lv in levels)
    return {i: ("sub" if has_sections and b[1] >= 3 else "section")
            for i, b in enumerate(blocks) if b[0] == "head"}


# A section whose only line says there is nothing ("None.", "_None._", "No open questions.", "No specific decisions were
# made.", "Aucune décision n'a été prise.", a lone dash) is as empty as no line at all: a short line that starts like that
# and names what is missing. The Summary is never dropped (a one-line Summary may well start with "No decision ...").
_NONE_START = re.compile(
    r"^(?:no|none|nothing|not applicable|n/?a|aucun(?:e)?|rien|pas d[e']|keine?|kein(?:e)?|"
    r"there (?:were|was|are|is) (?:no|none|nothing))\b", re.I)
_NONE_TOPIC = re.compile(
    r"decision|action|question|item|point|issue|identif|mention|raised|specific|explicit|applicable|report|unresolved|open|"
    r"agreed|assigned|discuss|d[ée]cision|t[âa]che|signaler|mentionn|soulev|[ée]voqu|relev|entscheid|frage|aufgabe", re.I)


# The few sentences that are "nothing here" at any level, a topic's too ("### Pricing" with only "No action items." under it
# is not a topic that was discussed).
_NONE_LINE = re.compile(
    r"^(?:none|nothing|n/?a|aucune?|rien|no (?:key )?decisions?(?: (?:were )?(?:made|taken|reached))?|"
    r"no action items?(?: (?:were )?(?:identified|found|mentioned|assigned))?|"
    r"no open questions?(?: (?:were )?(?:raised|identified|left))?)\.?$", re.I)


def _is_none_text(text, topic=False):
    """True for a line that only says there is nothing under its heading. ``topic``: the heading is a topic (level 3 or
    deeper): only the few plain sentences count, so a real line that starts with "No" ("No action was agreed on pricing.")
    is never taken for an empty one."""
    s = (text or "").strip().strip("*_ ").strip()
    if not s.strip("-–—•*_ ."):
        return True                                     # nothing but a dash
    s = s.lstrip("-–—•* ").strip().strip("*_ ").strip()
    if _NONE_LINE.match(s):
        return True
    words = s.split()
    if topic or len(words) > 10 or not _NONE_START.match(s):
        return False
    return len(words) == 1 or bool(_NONE_TOPIC.search(s))


def _is_summary_title(title):
    return re.sub(r"[*_:#\s]", "", title or "").lower() in ("summary", "résumé", "resume")


def _empty_sections(items):
    """``items``: [(level, text)] in order - level is the heading's level (text = its title), 0 for a body line (text = what
    it says; a task or a code line counts as real content). The indexes to drop: a heading with nothing under it (the next heading
    is of the same or a higher rank, or the notes end) or with only a "None." line, and that line. A title above its
    sections or a section made of topics has a deeper heading next, so it stays - until its topics are gone too."""
    dropped = set()
    while True:
        live = [i for i in range(len(items)) if i not in dropped]
        found = set()
        for pos, i in enumerate(live):
            level = items[i][0]
            if not level:
                continue
            j = pos + 1
            while j < len(live) and not items[live[j]][0]:
                j += 1
            body = live[pos + 1:j]
            nxt = items[live[j]][0] if j < len(live) else 0
            if not body and (j >= len(live) or nxt <= level):
                found.add(i)
            elif (len(body) == 1 and _is_none_text(items[body[0]][1], topic=level >= 3)
                  and not _is_summary_title(items[i][1])):
                found.update((i, body[0]))
        if not found:
            return dropped
        dropped |= found


def _block_text(b):
    kind = b[0]
    if kind in ("para", "bullet", "quote"):
        return strip_inline(b[1])
    if kind == "num":
        return strip_inline(b[2])
    return "x"                                      # a task or a code block is real content


def _without_empty_sections(blocks):
    items = [(b[1], b[2]) if b[0] == "head" else (0, _block_text(b)) for b in blocks]
    drop = _empty_sections(items)
    return [b for i, b in enumerate(blocks) if i not in drop] if drop else blocks


# ── who an action item belongs to ────────────────────────────────────────────
# A model writes "(Owner: David, Due: Wed)" - and also "(Owner: , Due: )", "Owner: Aram Due: Friday", "(Due: Fri, Owner:
# Aram)", "Responsable : Aram", "(Owner: Them)". One tolerant reader, shared by the notes view, Copy, Word, Notion and
# clean_notes, so every surface agrees.
# Words that mean "no value" in an Owner / Due slot.
EMPTY_VALUES = {
    "", "-", "–", "—", "?", "none", "n/a", "na", "tbd", "tbc", "unassigned", "unknown", "unspecified",
    "not specified", "not assigned", "not mentioned", "not given", "nobody", "no one", "no owner", "name", "date",
    "blank", "owner", "due", "aucun", "inconnu", "non spécifié", "non précisé", "à définir", "n/d", "tba",
    "to be determined", "to be confirmed", "to be assigned", "to be decided", "not applicable", "not defined", "not yet",
    "à déterminer", "à confirmer", "à assigner", "non applicable", "non défini", "n.a.", "n.a"}
# What a transcript calls people it cannot name, the roles a model gives them ("the interviewer", "Recruiter", "the
# company") and the ways it paraphrases "the group": a side, a role or a group is not a person, so it is never an owner.
# ("You" is not here: it is the person who recorded - see _YOU_WORDS and clean_notes.)
_ROLE = (r"interviewer|interviewee|recruiter|candidate|applicant|host|moderator|presenter|speaker|manager|company|client|"
         r"customer|vendor|supplier|employer|organi[sz]ation|business|firm|panel|committee|board|recorder|"
         r"recruteu(?:r|se)|candidat(?:e)?|interlocuteu(?:r|se)|interlocutrice|intervieweu(?:r|se)|entreprise|"
         r"soci[ée]t[ée]|cliente?|fournisseur|h[ôo]te")
_GENERIC_OWNER = re.compile(
    r"^(?:them|they|speaker(?:\s*(?:\d+|\?|[a-z]))?|spk\s*[\d?]+|participant\s*\d*|caller|unknown\s+(?:speaker|person|participant)|"
    r"unidentified\s+(?:speaker|person|participant)|(?:the\s+)?other\s+(?:side|party|person|speaker)s?|"
    r"(?:the\s+)?other\s+participants?|(?:the\s+)?others|someone|somebody|everyone|everybody|all|both|we|us|team|"
    r"the\s+team|the\s+group|(?:the\s+|le\s+|la\s+|les\s+|l')?(?:%s)s?|eux|elles|les\s+autres|"
    r"l'autre\s+partie|tout\s+le\s+monde|nous|l'?[ée]quipe)$" % _ROLE, re.I)
# The recorder, in the words a model uses for "you" (the notes view and clean_notes turn it into their name).
_YOU_WORDS = frozenset(("you", "vous", "toi", "moi", "tu"))
OWNER_LABELS = ("owner", "assignee", "assigned to", "responsible", "responsable", "propriétaire", "verantwortlich")
DUE_LABELS = ("due", "due date", "deadline", "échéance", "date limite", "fällig")
_ALL_LABELS = "|".join(re.escape(x) for x in sorted(OWNER_LABELS + DUE_LABELS, key=len, reverse=True))
_LABEL_RE = re.compile(r"(?<![\w])(%s)\s*:\s*" % _ALL_LABELS, re.I)
# one "(...)", with one level of parentheses inside ("Owner: Aram (host), Due: Fri")
_PAREN_RE = re.compile(r"\(((?:[^()]|\([^()]*\))*)\)")
# the same, with square brackets too: "[Owner: Claire, Due: Friday]"
_GROUP_RE = re.compile(r"[(\[]((?:[^()\[\]]|\([^()]*\)|\[[^\[\]]*\])*)[)\]]")
_OUTER_INNER = re.compile(r"^(.*?)\s*\((.*)\)$")
_TAIL_RE = re.compile(r"(?:\s+[-–—|]\s+|\.\s+|\s+)((?:%s)\s*:.*)$" % _ALL_LABELS, re.I)
_SEP = re.compile(r"\s*(?:/|&|\+|;|,|\band\b|\bet\b|\bund\b)\s*", re.I)
_MAX_META = 400                     # a longer "(...)" is prose, not an owner and a date
_TAIL_WINDOW = 240                  # where a trailing "Owner: .., Due: .." may start (and keeps the search linear)


def _plain(value):
    """An Owner / Due value without markdown marks, wrapping brackets or quotes and a closing full stop."""
    v = (value or "")[:_MAX_META].replace("**", "").replace("`", "").strip("*_ \t")
    v = v.rstrip(".").strip()
    for a, b in (("[", "]"), ("<", ">"), ('"', '"'), ("'", "'"), ("(", ")")):
        if len(v) >= 2 and v[0] == a and v[-1] == b and a not in v[1:-1]:
            v = v[1:-1].strip()
    return v


def is_empty_value(value):
    """True for '', '-', 'none', 'TBD', '[blank]', '<name>' ... (nothing was said)."""
    return _plain(value).lower() in EMPTY_VALUES


def owner_is_generic(name):
    """True for no name at all and for a side or a group rather than a person: Them, Speaker 2, the other side,
    everyone, the interviewer, 'Them (the company)' ..."""
    return not owner_names(name)


def _split_owner(value):
    """An Owner value cut into its people at ',', '/', '&', '+', ';', 'and', 'et', 'und' - not inside a parenthesis, and not
    in 'N/A'."""
    held = []

    def hold(m):
        held.append(m.group(0))
        return "\x00%d\x00" % (len(held) - 1)

    masked = re.sub(r"\b([nN])/([aA])\b", "\\1\x01\\2", _PAREN_RE.sub(hold, value))
    return [re.sub("\x00(\\d+)\x00", lambda m: held[int(m.group(1))], p).replace("\x01", "/")
            for p in _SEP.split(masked)]


def _people(part):
    """The people one piece of an Owner value stands for: 'Claire' -> ['Claire'], 'Them (Claire)' -> ['Claire'],
    'Aram (You)' -> ['Aram'], 'Speaker 1' / 'the interviewer' / 'N/A (to be assigned)' -> [], 'Vous' -> ['You']."""
    p = _plain(part)
    if not p or p.lower() in EMPTY_VALUES:
        return []
    m = _OUTER_INNER.match(p)
    if m and m.group(1).strip():
        outer, inner = m.group(1).strip(), m.group(2).strip()
        side, within = _people(outer), owner_names(inner)
        if not side:                                    # "Them (Claire)": the label tells nothing, the name does
            return within
        if side == ["You"]:                             # "You (Aram)"
            return within or side
        return side if within in ([], ["You"]) else [p]  # "Aram (You)" / "Claire (the host)": the name; "Marc (Acme)" as written
    if p.lower() in _YOU_WORDS:
        return ["You"]
    return [] if _GENERIC_OWNER.match(p) else [p]


def owner_names(value):
    """The people an Owner value names, in order: 'Them / Speaker 2' -> [], 'Marc and Nadia' -> ['Marc', 'Nadia'],
    'Them (Claire)' -> ['Claire']. The recorder ('You', 'Vous') stays 'You'."""
    if is_empty_value(value):
        return []
    out = []
    for part in _split_owner(_plain(value)):
        for name in _people(part):
            if name not in out:
                out.append(name)
    return out


def clean_owner(value):
    """An Owner value as one clean string ('' when it names nobody): 'Marc and Nadia' -> 'Marc, Nadia'."""
    return ", ".join(owner_names(value))


def _fields(meta):
    """'Aram Due: Friday, March 3' -> ({'owner': 'Aram', 'due': 'Friday, March 3'}, text before the first label).
    Label-aware: a value keeps its commas, and the labels may come in either order, with or without a comma."""
    marks = list(_LABEL_RE.finditer(meta))
    out = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(meta)
        kind = "owner" if m.group(1).lower() in OWNER_LABELS else "due"
        out.setdefault(kind, meta[m.end():end].strip().strip(" ,;|-–—\t"))
    return out, (meta[:marks[0].start()] if marks else meta)


_BOLD_LABEL = re.compile(r"\*\*\s*(%s)\s*:\s*\*\*|\*\*\s*(%s)\s*\*\*\s*:" % (_ALL_LABELS, _ALL_LABELS), re.I)


def _split(text):
    """(task as written, owner, due, found): ``found`` says an Owner / Due part was there to take off the end."""
    original = (text or "").strip()
    t = _BOLD_LABEL.sub(lambda m: (m.group(1) or m.group(2)) + ":", original)
    owner = due = ""
    found = False
    for m in reversed(list(_GROUP_RE.finditer(t))):         # every "(...)" or "[...]" that carries a label, the last first
        inner = m.group(1)
        if ":" not in inner or len(inner) > _MAX_META:
            continue
        fields, rest = _fields(inner)
        if fields and not rest.strip(" ,;"):
            owner, due, found = owner or fields.get("owner", ""), due or fields.get("due", ""), True
            t = (t[:m.start()].rstrip() + " " + t[m.end():].lstrip()).strip()
    if not found:
        m = _TAIL_RE.search(t, max(0, len(t) - _TAIL_WINDOW))
        if m and len(m.group(1)) <= _MAX_META:
            fields, rest = _fields(m.group(1))
            plain_space = not re.search(r"[-–—|.]", t[m.start():m.start(1)])
            # a plain-space tail ("Pay the invoice due: next month") needs BOTH labels: prose can say "due:" or "owner:"
            if fields and not rest.strip(" ,;") and (not plain_space or len(fields) == 2):
                owner, due, found = fields.get("owner", ""), fields.get("due", ""), True
                t = t[:m.start()].strip()
    if not found:
        t = original                                        # nothing was taken off: the text as it was written
    return t.rstrip(" -–—,;:").strip(), clean_owner(owner), "" if is_empty_value(due) else _plain(due), found


def split_task(text):
    """'`Fix X` (Owner: David, Due: Wed)' -> ('Fix X', 'David', 'Wed'). Tolerant (see above); an empty, placeholder or
    generic owner comes back as '', an empty due as ''. The due is as written: no 'by ' (see parse_task)."""
    task, owner, due, _found = _split(text)
    return task.replace("`", "").strip(), owner, due


# "by" goes in front of a due only when it starts like an English date ("Wed", "15 November", "March 3", "next week", "Q4",
# "this Friday"): a due that has its own word ("within a week", "on Friday", "today", "this afternoon", "ASAP") or is in another
# language ("vendredi", "15 novembre") reads as written.
_DAY = r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b"
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
          r"nov(?:ember)?|dec(?:ember)?)\b")
_BY_DUE = re.compile(
    r"^(?:%s|%s|(?:tomorrow|next|the|end|mid|middle|early|late|q[1-4])\b|this\s+%s|"
    r"\d+(?:st|nd|rd|th)?(?:\s+of)?\s+%s|\d+(?:st|nd|rd|th)\b|\d{1,2}\s*[ap]m\b|\d{1,4}[/.-]\d{1,2}(?:[/.-]\d{1,4})?$)"
    % (_DAY, _MONTH, _DAY, _MONTH), re.I)


def parse_task(text):
    """'`Fix X` (Owner: David, Due: Wed)' -> (task, owner, due): split_task, with 'by ' put in front of a due that starts like
    an English date ('by Wed', 'by 15 November', 'by next week'); not before 'within a week', 'on Friday', 'ASAP' or a due in
    another language ('vendredi')."""
    task, owner, due = split_task(text)
    if due and _BY_DUE.match(due):
        due = "by " + due
    return task, owner, due


def render_task_meta(owner, due):
    """' (Owner: X, Due: Y)' with the empty parts left out ('' when both are)."""
    parts = []
    if owner:
        parts.append("Owner: %s" % owner)
    if due:
        parts.append("Due: %s" % due)
    return " (%s)" % ", ".join(parts) if parts else ""


_TASK_LINE = re.compile(r"^(\s*(?:[-*\u2022]|\d+[.)])\s*\[(?: |x|X)\]\s*)(.*)$")


def _line_text(line):
    """What a notes line says, for deciding whether a section is empty."""
    s = line.strip()
    if _TASK_RE.match(s):
        return "x"
    m = _BULLET_RE.match(s)
    return strip_inline(m.group(1) if m else s)


_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line):
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") else s
    return [c.replace("**", "").strip() for c in s.split("|")]


def _table_lines(header, rows, section):
    """A table a model wrote anyway, as the list it should have been: under Action items one '- [ ] task (Owner: ..,
    Due: ..)' a row (the columns are found by their titles), elsewhere '- first cell (Title: value; Title: value)'."""
    low = [h.lower() for h in header]

    def col(pattern):
        return next((i for i, h in enumerate(low) if re.search(pattern, h)), None)

    task_i = col(r"task|action|item|todo|to-do|tâche")
    owner_i = col(r"owner|assignee|responsible|who|responsable|propriétaire")
    due_i = col(r"due|deadline|when|date|échéance|\bby\b")
    out = []
    for cells in rows:
        def cell(i):
            return cells[i].strip() if i is not None and i < len(cells) else ""
        if not any(cells):
            continue
        if "action" in section and task_i is not None and cell(task_i):
            out.append("- [ ] " + cell(task_i) + render_task_meta(cell(owner_i), cell(due_i)))
            continue
        extra = "; ".join("%s: %s" % (header[i], v) for i, v in enumerate(cells[1:], 1)
                          if v and i < len(header) and header[i])
        extra = extra or "; ".join(v for v in cells[1:] if v)
        first = cells[0] or next((v for v in cells if v), "")
        out.append("- " + first + (" (%s)" % extra if extra and cells[0] else ""))
    return out


def _without_tables(lines):
    """``lines`` (split on "\n", a "\r" may end each) with every markdown table made a list (see _table_lines); the same
    list when there is none."""
    out, i, n = [], 0, len(lines)
    section, in_fence, changed = "", None, False
    while i < n:
        raw = lines[i]
        line = raw[:-1] if raw.endswith("\r") else raw
        if in_fence is not None:
            if re.match(r"^\s*%s{%d,}\s*$" % (re.escape(in_fence[0]), len(in_fence)), line):
                in_fence = None
        elif _FENCE_RE.match(line):
            in_fence = _FENCE_RE.match(line).group(1)
        else:
            h = _HEAD_RE.match(line)
            if h:
                section = h.group(2).strip().lower()
            elif _TABLE_ROW.match(line) and i + 1 < n and _TABLE_SEP.match(lines[i + 1].rstrip("\r")) \
                    and "|" in lines[i + 1]:
                j = i + 2
                rows = []
                while j < n and _TABLE_ROW.match(lines[j].rstrip("\r")):
                    rows.append(_cells(lines[j]))
                    j += 1
                cr = "\r" if raw.endswith("\r") else ""
                out.extend(x + cr for x in _table_lines(_cells(line), rows, section))
                i, changed = j, True
                continue
        out.append(raw)
        i += 1
    return out if changed else lines


def clean_notes(md, me=""):
    """The notes in the shape every surface can show as it is: each action item is
    '- [ ] task (Owner: Name, Due: date)' with the empty and the generic parts left out ('Owner: , Due: ' and 'Owner:
    Them' never survive; 'Owner: You' becomes ``me``, the person's first name, when it is known), a section with
    nothing under it or only 'None.' is gone, and what a model wraps around its answer is taken off (a code fence around
    all of it, 'Here are the notes:', 'Let me know ...', a table made a list). Everything else is untouched (a ticked box
    stays ticked, the line endings stay), and cleaning clean notes changes nothing. The line numbers of the lines that stay
    move up when a section goes, so this is for text that is saved or sent (notes.md, the history entry, Notion, the
    Markdown export) - the notes view shows the raw notes through parse_notes(sections=True)."""
    base = md or ""
    src = notes_prompt.tidy_answer(base)
    parts = src.split("\n")
    lines = _without_tables(parts)
    changed = src is not base or lines is not parts
    in_fence = None
    items = []                                      # [(level, text)] of the lines outside code, for the empty sections
    keep = []
    for raw in lines:
        cr = raw.endswith("\r")
        line = raw[:-1] if cr else raw
        if in_fence is not None:
            if re.match(r"^\s*%s{%d,}\s*$" % (re.escape(in_fence[0]), len(in_fence)), line):
                in_fence = None
            items.append((0, "x"))
            keep.append(raw)
            continue
        f = _FENCE_RE.match(line)
        if f:
            in_fence = f.group(1)
            items.append((0, "x"))
            keep.append(raw)
            continue
        m = _TASK_LINE.match(line)
        if m:
            task, owner, due, found = _split(m.group(2))
            if found or owner or due:
                names = [(me if me and x.lower() == "you" else x) for x in owner.split(", ") if x]
                owner = ", ".join(names)
                meta = render_task_meta(owner, due)
                new = (m.group(1) + (task + meta if task else meta.lstrip())) if (task or owner or due) else line
                if new != line:
                    changed = True
                line = new
            items.append((0, "x"))
            keep.append(line + ("\r" if cr else ""))
            continue
        h = _HEAD_RE.match(line) if line.strip() else None
        items.append((len(h.group(1)), h.group(2)) if h else (0, _line_text(line) if line.strip() else ""))
        keep.append(raw)
    # blank lines are not body: only text under a heading counts
    live = [i for i, (lv, t) in enumerate(items) if lv or t]
    drop = _empty_sections([items[i] for i in live])
    if drop:
        gone = {live[i] for i in drop}
        out, blank = [], True
        for i, raw in enumerate(keep):
            if i in gone:
                continue
            if not raw.strip():
                if blank:
                    continue
                blank = True
            else:
                blank = False
            out.append(raw)
        while out and not out[-1].strip():
            out.pop()
        tail = "\n" if src.endswith("\n") else ""
        return "\n".join(out) + (tail if out else "")
    return "\n".join(keep) if changed else base


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


def _topic_ids(blocks):
    """ids of the blocks that are topic headings (see heading_roles)."""
    roles = heading_roles(blocks)
    return {id(b) for i, b in enumerate(blocks) if roles.get(i) == "sub"}


def to_html(md):
    """A minimal HTML fragment for the clipboard (Word, Outlook, Gmail, Docs,
    Notion, Slack, Teams): real headings, lists and bold, no styling. A topic
    under a section ("### Topic" below "## Discussion") is an <h4>."""
    parts = []
    blocks = parse_blocks(md)
    topics = _topic_ids(blocks)
    for kind, items in _groups(blocks):
        b = items[0]
        if kind == "head":
            text = inline_html(b[2], code=True)
            if b[1] >= BOLD_HEAD_LEVEL:
                parts.append("<p><b>%s</b></p>" % text)
            else:
                tag = "h4" if id(b) in topics else "h3"
                parts.append("<%s>%s</%s>" % (tag, text, tag))
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
    colours; done tasks and quotes use "muted"; "item_gap" ("3px") a little
    air above each list item (a model's answer reads better with it)."""
    c = dict(colors or {})

    def style(*pairs):
        css = "".join("%s:%s;" % (k, v) for k, v in pairs if v)
        return ' style="%s"' % css if css else ""

    text_c, muted_c, head_c = c.get("text"), c.get("muted"), c.get("head") or c.get("text")
    p_style = style(("margin-top", "0px"), ("margin-bottom", "8px"), ("color", text_c))
    parts = []
    blocks = parse_blocks(md)
    topics = _topic_ids(blocks)
    for kind, items in _groups(blocks):
        b = items[0]
        if kind == "head":
            text = inline_html(b[2], code=True)
            if b[1] < BOLD_HEAD_LEVEL and id(b) in topics:
                parts.append("<p%s><b>%s</b></p>" % (style(("margin-top", "6px"), ("margin-bottom", "4px"),
                                                           ("color", head_c)), text))
            elif b[1] < BOLD_HEAD_LEVEL:
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
            li_tag = "<li%s>" % style(("margin-top", c.get("item_gap")))
            li = "".join("%s%s</li>" % (li_tag, inline_html(x[1] if kind == "bullet" else x[2], code=True))
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


# ── a model's answer, shown in the app ───────────────────────────────────────
def streaming_markdown(md):
    """An answer that is still streaming, safe to render: an open ``` fence
    is closed, and a half-written ** or ` is left out until its pair arrives,
    so Markdown markers never flash as raw characters."""
    s = md or ""
    if s.count("```") % 2:
        return s + "\n```"
    for mark in ("**", "`"):
        if s.count(mark) % 2:
            i = s.rfind(mark)
            s = s[:i] + s[i + len(mark):]
    return s


def close_markers(md):
    """A finished answer, safe to render: an open ``` fence is closed and a ** with no pair is dropped, so a
    marker never shows as raw characters (an answer cut short can end inside one). A lone ` stays - in a finished
    answer it may be meant (streaming_markdown drops it too, for an answer still arriving)."""
    s = md or ""
    if s.count("```") % 2:
        return s + "\n```"
    if s.count("**") % 2:
        i = s.rfind("**")
        s = s[:i] + s[i + 2:]
    return s


_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\([^)\n]*\)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(\s*<?([^)\s>]*)>?[^)\n]*\)")
_REF_LINK_RE = re.compile(r"\[([^\]\n]+)\]\[[^\]\n]*\]")
_REF_DEF_RE = re.compile(r"(?m)^[ \t]*\[[^\]\n]+\]:[ \t]*\S+.*$\n?")
_TABLE_SEP_RE = re.compile(r"^:?-{2,}:?$")
_BREAK_RE = re.compile(r"(\S{19}[^\s*`])(?=[^\s*`])")
_ZWSP = "\u200b"                     # a place where a line may end, drawn as nothing (ui.components.mini_prompt)


def _plain_table_rows(md):
    """A Markdown table's rows as plain lines ("a · b"): a label cannot draw a table, and its pipes would show
    as symbols. The separator row ("---|---") is dropped."""
    out = []
    for line in (md or "").split("\n"):
        s = line.strip()
        if s.startswith("|") and s.count("|") >= 2:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(_TABLE_SEP_RE.match(c) for c in cells if c) and any(cells):
                continue
            out.append(" · ".join(c for c in cells if c))
        elif s and "|" in s and all(_TABLE_SEP_RE.match(c.strip()) for c in s.split("|") if c.strip()) \
                and s.count("-") >= 3:
            continue                                           # a separator row without outer pipes
        else:
            out.append(line)
    return "\n".join(out)


def without_breaks(text):
    """``text`` with the invisible break points answer_text puts in long strings taken out again: what a reader
    copies from an answer must paste as the address or the name it was."""
    return str(text or "").replace(_ZWSP, "")


def answer_text(md, streaming=False):
    """A model's answer made ready for qt_html: everything the label cannot show properly becomes plain words.
    Images give their description, links their text (never clickable: ``[text](url)`` -> "text", "text (url)"
    when they differ), table rows become lines, a run of 20+ characters without a space gets break points (a
    pasted address cannot widen the card). ``streaming``: a half-written ** or ` is left out
    (streaming_markdown) - for an answer still arriving and one that was stopped or cut short; any other answer
    only loses a ** with no pair and gets an open ``` closed (close_markers), so no marker ever shows as raw
    characters. Qt-free; everything stays escaped later (inline_html), so nothing the model writes is ever read
    as HTML."""
    s = str(md or "").replace("\r\n", "\n").replace("\r", "\n")
    s = _plain_table_rows(s)
    s = _IMAGE_RE.sub(lambda m: m.group(1).strip(), s)

    def link(m):
        text, url = m.group(1).strip(), m.group(2).strip()
        return text if not url or url == text or url.rstrip("/") == text.rstrip("/") else "%s (%s)" % (text, url)
    s = _LINK_RE.sub(link, s)
    s = _REF_LINK_RE.sub(lambda m: m.group(1), s)
    s = _REF_DEF_RE.sub("", s)
    s = streaming_markdown(s) if streaming else close_markers(s)
    return _BREAK_RE.sub("\\1" + _ZWSP, s)


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
