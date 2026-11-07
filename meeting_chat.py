"""Ask about this meeting: the chat's brain. Qt-free and widget-free, so tests/ import it.

The card (ui/pages/meeting_chat_ui.py) owns the window; this module owns everything that can be decided
without one:

    estimate_tokens(text)       a conservative token count, by script (no tokenizer needed)
    time_marked(transcript, segments_doc)
                                the transcript with [mm:ss] marks, worked out from segments.json
    load_material(folder)       what a question may use (worker thread; every question reads the folder again,
                                so the newest transcript and notes are always the ones used)
    plan_context(material, question, history, budget_tokens, local=False)
                                the one prompt: everything when it fits, else the notes and the parts of the
                                transcript closest to the question - and how much was left out
    pick_engine(cfg, pro, has_token, ...)
                                which AI answers (the notes' engine) and, when none can, why - no network
    ask(folder, question, history, cfg=, pro=, resolve=, ...)
                                the whole question, blocking (worker thread); streams through ``on_text``;
                                a Stop is noticed before anything is read or sent, not only at the first word;
                                the Answer says whether the connection dropped (``dropped``) or the engine
                                reached the longest answer it may write (``cut``): action_api.stream_end()
    friendly_error(text, kind)  a calm sentence for whatever went wrong, never raw error text
    dump_chat(messages)         chat.json's text (meeting_store.load_chat reads it)

The engine is the notes' engine (meeting_session.notes_engine, then the Pro routing of
AppController._resolve_action_engine): Gusan Cloud for a signed-in Pro user, a cloud engine with the user's own
key, or a model on this computer - only a model on this computer in Privacy mode. The question goes through
actions.process_stream with the mode ``meeting_chat`` (action_api.MEETING_CHAT_SYSTEM carries the rules); the
managed route accepts any mode, so there is nothing to deploy. No text of a question or an answer is ever sent
anywhere else: the only analytics event is a count (``meeting_chat_asked``, the engine kind).
"""

import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field

import action_api
import actions
import invite_context
import meeting_session
import meeting_store
import rich_text
import speech_langs

logger = logging.getLogger("transcribe")

MAX_QUESTION_CHARS = 4000            # a longer question (a pasted document) is cut here
HISTORY_QUESTION_CHARS = 500         # an earlier question sent back to the model
HISTORY_ANSWER_CHARS = {"cloud": 1500, "local": 500}
HISTORY_TURNS = {"cloud": 6, "local": 2}
BUDGET_CLOUD = 100000                # estimated tokens of the whole prompt: Gusan Cloud
BUDGET_OWN_KEY = 48000               # ... a cloud engine with the user's own key
LOCAL_OUT_TOKENS = 600               # what a local answer may write (local_llm._MAX_TOKENS_BY_MODE)
LOCAL_MARGIN = 160                   # room kept free in the local window
WINDOW_TOKENS = 700                  # a piece of transcript is chosen or left out as a whole
RETRIES = 2                          # smaller prompts tried when an engine says the first was too big
RETRIES_OWN_KEY = 4                  # ... with the user's own key: nobody knows that provider's window
SHRINK = 0.6
SMALL_BUDGET = 8000                  # a prompt this small that is still too long: it is the engine, not the question
ALLOWANCE_KEYS = ("_live_session_id", "_note_ref")      # what meters a free cloud note or Live Assistant session
LEFT_OUT = "[… part of the meeting left out …]"
TIME_GAP = 45.0                      # a time mark at the start of a line when this many seconds passed
TIME_GAP_INSIDE = 60.0               # ... and inside a long line
RESYNC = 12                          # segments looked ahead when a line does not match the next one
DISTINCT_CHARS, DISTINCT_WORDS = 20, 4   # a line shorter than both ("Yes.", "Okay, thanks.") may be said twice

STARTERS = ("What were the key decisions?", "What did I agree to do?", "Write a follow-up email")

# The words the card shows (plain, calm, no jargon, never "limit").
NO_TRANSCRIPT = "There's no transcript for this meeting yet, so there's nothing to ask about."
FINISHING = "Finishing the transcript… You can ask in a moment."
INTRO = "Ask anything about what was said. Answers come from this meeting only."
NEEDS_PRO = ("Questions about a meeting need Gusan Pro, or your own key or an AI on this computer "
             "(Settings › Speech & AI).")
SIGNED_OUT = "Sign in to ask questions about your meetings."
PRIVACY_NO_MODEL = actions.LOCAL_ANSWERS_MESSAGE
TOO_LONG = ("This meeting is too long for this AI model to read in one go. Try a more specific question, "
            "or pick another engine in Settings › Speech & AI.")
TOO_LONG_SMALL = "This AI model can't read a meeting this long. Pick another engine in Settings › Speech & AI."
DROPPED = "The connection dropped, so this answer may be cut short."
CUT_SHORT = "This answer was cut short. Ask for the rest if you need it."
OFFLINE_CLOUD = "Couldn't reach Gusan Cloud. Check your internet connection."
OFFLINE_KEY = "Couldn't reach your AI service. Check your internet connection."
SERVER = "Gusan Cloud couldn't answer just now. Try again in a moment."
GENERIC = "Couldn't get an answer. Try again."
NO_ANSWER = "The AI didn't write an answer. Try again."


class Cancelled(BaseException):
    """Raised from a stream's callback to stop it (the Stop button, another meeting, the window going away). A
    BaseException so a provider's per-chunk ``except Exception`` cannot swallow it. ``text``: what had arrived."""

    def __init__(self, text=""):
        super().__init__("cancelled")
        self.text = text


class ChatError(Exception):
    """A question that could not be answered, in words for the user. ``code``: no_transcript, needs_pro,
    signed_out, privacy_no_model, needs_download, offline, allowance, server, too_long, thought_only,
    local_missing, key, privacy, empty or generic."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ── tokens ───────────────────────────────────────────────────────────────────
# Characters per token by script, deliberately LOW (so the estimate is high): measured on the Qwen2.5 vocabulary
# (the on-computer models), English 4.7, French 3.5, German 4.1, Spanish 3.9, Russian 2.8, Arabic 3.0, Chinese
# 1.7, Armenian 1.0. The cloud engines' tokenizers are kinder; an estimate that is too high only costs a little
# of the meeting (said to the user) - one that is too low would make the engine refuse the question.
_CPT = {"latin": 3.3, "cyrillic": 2.4, "arabic": 2.6, "cjk": 1.4, "other": 0.8}


def _script(ch):
    o = ord(ch)
    if o < 0x370 or 0x1E00 <= o < 0x1F00 or 0x2000 <= o < 0x2C00:
        return "latin"                  # Latin, digits, punctuation, symbols
    if o < 0x530 or 0x1F00 <= o < 0x2000:
        return "cyrillic"               # Greek and Cyrillic
    if 0x590 <= o < 0x780 or 0xFB1D <= o < 0xFE00 or 0xFE70 <= o < 0xFF00:
        return "arabic"                 # Hebrew, Arabic, Syriac
    if 0x2E80 <= o < 0xA000 or 0xAC00 <= o < 0xD7B0 or 0xF900 <= o < 0xFB00 or 0xFF00 <= o < 0xFFF0:
        return "cjk"
    return "other"                      # Armenian, Georgian, Indic, Thai, emoji ...


def estimate_tokens(text):
    """A conservative token count for ``text`` (see _CPT)."""
    if not text:
        return 0
    if text.isascii():
        return int(math.ceil(len(text) / _CPT["latin"])) + 4
    counts = {}
    for ch, n in Counter(text).items():
        key = _script(ch)
        counts[key] = counts.get(key, 0) + n
    return int(math.ceil(sum(n / _CPT[key] for key, n in counts.items()))) + 4


def _clip_tokens(text, cap):
    """``text`` cut (at a word boundary, ended with "…") to about ``cap`` tokens."""
    text = text or ""
    cap = max(0, int(cap))
    if estimate_tokens(text) <= cap:
        return text
    if cap <= 8:
        return ""
    per = max(0.3, len(text) / float(estimate_tokens(text)))
    chars = max(20, int(cap * per))
    out = invite_context.cut_text(text, chars)
    while len(out) > 20 and estimate_tokens(out) > cap:
        out = invite_context.cut_text(text, int(len(out) * 0.85))
    return out


def _clip_chars(text, limit):
    return invite_context.cut_text(str(text or "").strip(), limit)


# ── times ────────────────────────────────────────────────────────────────────
def clock(seconds):
    """83 -> "01:23", 3725 -> "1:02:05"."""
    sec = max(0, int(seconds))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


def _norm(text):
    return " ".join(str(text or "").split())


_AUDIO_RE = re.compile(r"^audio_part(\d+)\.wav$", re.I)


def _flatten(segments_doc):
    """[(start in seconds from the start of the recording, normalised text)] over every part of segments.json, in
    order (a part starts where the parts before it end), and the index where each part's segments start. A part
    that follows a recording part segments.json has nothing for (a meeting continued from before 2.0.1) has no
    known place on the clock, so it and the parts after it are left out: never a wrong time."""
    parts = (segments_doc or {}).get("parts") if isinstance(segments_doc, dict) else None
    segs, firsts, offset, expected, known = [], [], 0.0, 1, True
    for idx, part in enumerate(parts if isinstance(parts, list) else []):
        if not isinstance(part, dict):
            continue
        m = _AUDIO_RE.match(str(part.get("audio") or ""))
        number = int(m.group(1)) if m else idx + 1
        if number != expected:
            known = False
        if known:
            firsts.append(len(segs))
            for seg in part.get("segments") or []:
                if not isinstance(seg, dict):
                    continue
                text = _norm(seg.get("t"))
                try:
                    start = float(seg.get("s", 0.0))
                except (TypeError, ValueError):
                    continue
                if text:
                    segs.append((offset + start, text))
        try:
            offset += float(part.get("seconds") or 0.0)
        except (TypeError, ValueError):
            known = False
        expected = number + 1
    return segs, firsts


def _match(segs, k, target):
    """The index after the run of segments from ``k`` whose texts, joined by one space, are exactly ``target``
    (a transcript line); None when they are not."""
    acc, i, n = "", k, len(segs)
    while i < n and len(acc) < len(target):
        nxt = segs[i][1] if not acc else acc + " " + segs[i][1]
        if not target.startswith(nxt):
            return None
        acc, i = nxt, i + 1
    return i if i > k and acc == target else None


def _is_short(line):
    """A line that may well be said more than once in a meeting ("Yes.", "Okay, thanks.")."""
    return len(line) < DISTINCT_CHARS and len(line.split()) < DISTINCT_WORDS


def time_marked(transcript, segments_doc, line_gap=TIME_GAP, inner_gap=TIME_GAP_INSIDE):
    """(the transcript with "[mm:ss]" marks, ok). Each line of the saved transcript is matched, word for word, to
    the segments segments.json kept (consecutive segments of one speaker make one line; the names that come after
    the notes only changed the labels, never the words) and gets the clock time of its first segment at the start
    of the line - and again inside a long line - once ``line_gap`` / ``inner_gap`` seconds passed since the last
    mark. A line that does not match gets no mark and the marks stop until a "— Resumed —" line (never a wrong
    time); a line is looked for up to RESYNC segments ahead of where the last one ended (a segment the saved
    text dropped). A short line that fits two places in that look-ahead ("Yes.") gets no mark: it could be either.
    ``ok``: every line matched. Without segments the transcript comes back as it is, (…, False)."""
    segs, firsts = _flatten(segments_doc)
    if not segs:
        return transcript or "", False
    out, j, last_mark = [], 0, -1e9
    synced, after_marker, matched, failed = True, False, False, False
    for line in (transcript or "").split("\n"):
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if rich_text.is_marker(s):
            out.append(line)
            after_marker = True
            continue
        starts = []
        if synced:
            starts = range(j, min(len(segs), j + RESYNC + 1))
        elif after_marker:
            starts = [f for f in firsts if f >= j]
        after_marker = False
        label_body = rich_text.split_speaker(s)
        tries = [label_body] + ([("", s)] if label_body[0] else [])
        hit = None
        for k in starts:
            for label, body in tries:
                end = _match(segs, k, _norm(body))
                if end is not None:
                    hit = (label, k, end, _norm(body))
                    break
            if hit:
                break
        if hit is None:
            out.append(line)
            synced, failed = False, True
            continue
        label, k, end, target = hit
        # A short line ("Yes.") that fits two places in the look-ahead could be either: it keeps the first (the
        # transcript is usually whole) but is given no time - never a wrong one.
        unsure = _is_short(target) and any(_match(segs, o, target) is not None for o in starts if o > k)
        synced, matched, j = True, True, end
        pieces = []
        for n, (t0, text) in enumerate(segs[k:end]):
            if not unsure and t0 - last_mark >= (line_gap if n == 0 else inner_gap):
                pieces.append("[%s] %s" % (clock(t0), text))
                last_mark = t0
            else:
                pieces.append(text)
        out.append((label + ": " if label else "") + " ".join(pieces))
    if not matched:
        return transcript or "", False
    return "\n".join(out), not failed


# ── the meeting's material ───────────────────────────────────────────────────
_LABELLED_RE = re.compile(r"(?m)^\s*(?:You|Them|Speaker\s+(?:\d+|\?))\s*:")
_YOU_RE = re.compile(r"(?m)^\s*You\s*:")


@dataclass
class Material:
    """Everything a question may use, read from the meeting's folder."""
    folder: str = ""
    title: str = ""
    people: list = field(default_factory=list)
    agenda: str = ""
    language: str = ""
    started: str = ""
    duration_sec: int = 0
    notes: str = ""
    my_notes: str = ""
    transcript: str = ""
    segments: object = None
    transcript_file: bool = False       # transcript.txt is on disk (else only the live pieces are)
    labelled: bool = False              # the transcript says who spoke (You / Them / Speaker N)
    me: str = ""                        # the recorder's first name (the notes call "You" by it); "" when unknown
    _marked: object = None

    def marked(self):
        """(transcript with time marks, ok) - worked out once."""
        if self._marked is None:
            self._marked = time_marked(self.transcript, self.segments)
        return self._marked


def load_material(folder):
    """The meeting's title, people, agenda, notes, My notes, transcript and segments. Disk work: worker thread."""
    meta = meeting_store.load_meta(folder)
    people = meta.get("attendees_list")
    if not (isinstance(people, list) and all(isinstance(p, str) for p in people)):
        people = meeting_store.split_attendees(meta.get("attendees") or "")
    try:
        duration = max(0, int(meta.get("duration_sec") or 0))
    except (TypeError, ValueError):
        duration = 0
    transcript = meeting_store.load_transcript(folder)
    return Material(
        folder=str(folder), title=str(meta.get("title") or ""), people=list(people),
        agenda=str(meta.get("agenda") or ""), language=str(meta.get("language") or ""),
        started=str(meta.get("started") or ""), duration_sec=duration,
        notes=meeting_store.load_notes(folder), my_notes=meeting_store.load_my_notes(folder),
        transcript=transcript, segments=meeting_store.load_segments(folder),
        transcript_file=meeting_store.has_transcript_file(folder),
        labelled=bool(_LABELLED_RE.search(transcript)))


# ── the prompt ───────────────────────────────────────────────────────────────
_TAGS = ("meeting", "notes_written_by_gusan", "users_own_notes", "transcript", "conversation_so_far", "question")
_TAG_RE = re.compile(r"<[\s\u200b]*/?[\s\u200b]*(?:%s)\b" % "|".join(_TAGS), re.I)    # "</ transcript>" too
_ZWSP = "​"


def _defang(text):
    """Text from the meeting, the notes or a question cannot close one of the prompt's sections: the tag's
    angle bracket gets an invisible character after it."""
    return _TAG_RE.sub(lambda m: "<" + _ZWSP + m.group(0)[1:], str(text or ""))


def _section(tag, body):
    return "<%s>\n%s\n</%s>" % (tag, body, tag)


def _length(seconds):
    minutes = max(1, int(round(seconds / 60.0)))
    h, m = divmod(minutes, 60)
    return "%d h %02d min" % (h, m) if h else "%d min" % minutes


def _meeting_text(m):
    lines = []
    header = invite_context.notes_header(m.title, m.people, m.agenda)   # title, people, agenda: framed as data
    if header:
        lines.append(header)
    facts = []
    when = str(m.started or "")[:16]
    if when:
        facts.append("Date: " + when)
    if m.duration_sec:
        facts.append("Length: " + _length(m.duration_sec))
    code = speech_langs.normalize(m.language)
    if code and code not in ("auto", "multi"):
        facts.append("Language: " + speech_langs.language_name(code))
    elif code == "multi":
        facts.append("Language: mixed")
    if facts:
        lines.append("\n".join(facts))
    who = invite_context.recorder_line(m.me) if (m.me and _YOU_RE.search(m.transcript or "")) else ""
    if who:
        lines.append(who)                    # the notes name the recorder; the transcript says "You"
    return "\n".join(lines) or "(no details)"


def _history_text(history, local):
    """The last few finished answers with their questions, newest last. A stopped or failed answer never goes back
    to the model, and neither does a question that has none."""
    kind = "local" if local else "cloud"
    pairs, question = [], None
    for msg in history if isinstance(history, (list, tuple)) else []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            question = msg.get("text")
        elif msg.get("role") == "assistant" and msg.get("status") == "done" and question:
            pairs.append((question, msg.get("text") or ""))
            question = None
    rows = []
    for q, a in pairs[-HISTORY_TURNS[kind]:]:
        rows.append("User: " + _clip_chars(q, HISTORY_QUESTION_CHARS))
        rows.append("Assistant: " + _clip_chars(a, HISTORY_ANSWER_CHARS[kind]))
    return _defang("\n".join(rows))


@dataclass
class Coverage:
    complete: bool = True
    pct: int = 100
    covered_min: object = None
    total_min: object = None

    def as_dict(self):
        out = {"complete": self.complete, "pct": self.pct}
        if self.total_min:
            out["covered_min"], out["total_min"] = self.covered_min, self.total_min
        return out


@dataclass
class Plan:
    prompt: str
    coverage: Coverage
    tokens: int
    times: bool = False


def coverage_note(cov):
    """The line under an answer written from part of the meeting ("" when it saw all of it). ``cov``: a Coverage
    or its dict."""
    if isinstance(cov, Coverage):
        cov = cov.as_dict()
    if not isinstance(cov, dict) or cov.get("complete", True):
        return ""
    total, covered = cov.get("total_min"), cov.get("covered_min")
    if total and covered is not None:
        part = "about %d of %d min" % (covered, total)
    else:
        part = "about %d%% of it" % int(cov.get("pct") or 0)
    return "Long meeting: this answer used the parts closest to your question (%s)." % part


_WORD_RE = re.compile(r"\w{3,}", re.U)


def _terms(text):
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def _spread_order(n):
    """0..n-1 in the order that samples the range evenly: both ends, the middle, the quarters ..."""
    order, seen, spans = [], set(), [(0, n - 1)]
    while spans and len(order) < n:
        nxt = []
        for lo, hi in spans:
            if lo > hi:
                continue
            for i in (lo, hi) if lo != hi else (lo,):
                if i not in seen:
                    seen.add(i)
                    order.append(i)
            mid = (lo + hi) // 2
            if mid not in seen:
                seen.add(mid)
                order.append(mid)
            nxt += [(lo + 1, mid - 1), (mid + 1, hi - 1)]
        spans = nxt
    order += [i for i in range(n) if i not in seen]
    return order


def _pieces(transcript, win_tokens):
    """The transcript's lines, a line longer than a window cut at sentence ends (a mic-only meeting is often
    one long line)."""
    out = []
    for line in (transcript or "").split("\n"):
        if not line.strip():
            continue
        tokens = estimate_tokens(line)
        if tokens <= win_tokens:
            out.append((line, tokens))
            continue
        per = len(line) / float(tokens)
        for part in rich_text.transcript_paragraphs(line, max_chars=max(40, int(win_tokens * per * 0.9))):
            out.append((part, estimate_tokens(part)))
    return out


def select_windows(transcript, question_text, budget_tokens, win_tokens=WINDOW_TOKENS):
    """(text, covered fraction 0..1): the transcript when it fits ``budget_tokens``; else windows of about
    ``win_tokens`` chosen by how many of the question's words (rarer words count more) they hold, with the
    opening and the end favoured, put back in order with a LEFT_OUT line at each gap and at a cut end. A question
    that names nothing in the transcript ("summarise it") gets windows spread over the whole meeting."""
    total = estimate_tokens(transcript)
    if total <= budget_tokens:
        return transcript, 1.0
    if budget_tokens < 60:
        return LEFT_OUT, 0.0
    win_tokens = max(120, min(win_tokens, budget_tokens // 3))
    wins, cur, cur_t = [], [], 0
    for text, tokens in _pieces(transcript, win_tokens):
        tokens += 1
        if cur and cur_t + tokens > win_tokens:
            wins.append(("\n".join(cur), cur_t))
            cur, cur_t = [], 0
        cur.append(text)
        cur_t += tokens
    if cur:
        wins.append(("\n".join(cur), cur_t))
    n = len(wins)
    bags = [_terms(body) for body, _t in wins]
    df = Counter(w for bag in bags for w in bag)
    q = _terms(question_text)
    base = [sum(math.log(1.0 + n / df[w]) for w in q if w in bag) for bag in bags]
    score = []
    for i in range(n):
        s = base[i] + 0.35 * ((base[i - 1] if i else 0.0) + (base[i + 1] if i + 1 < n else 0.0))
        if i in (0, n - 1):
            s += 0.5                             # the opening and the end: who is there, what comes next
        score.append(s)
    rank = {i: r for r, i in enumerate(_spread_order(n))}
    chosen, used = set(), 0
    for i in sorted(range(n), key=lambda i: (-round(score[i], 6), rank[i])):
        if used + wins[i][1] <= budget_tokens:
            chosen.add(i)
            used += wins[i][1]
    parts, prev = [], -1
    for i in sorted(chosen):
        if i != prev + 1:
            parts.append(LEFT_OUT)
        parts.append(wins[i][0])
        prev = i
    if prev != n - 1:
        parts.append(LEFT_OUT)
    covered = sum(wins[i][1] for i in chosen) / float(sum(t for _b, t in wins))
    return "\n".join(parts), min(1.0, covered)


def plan_context(material, question, history, budget_tokens, local=False):
    """The prompt for one question: the meeting's details, the notes Gusan wrote, the user's own notes, the
    transcript (with time marks when segments.json allows), the conversation so far and - last, so the part
    before it stays the same from one question to the next - the question. Everything when it fits in
    ``budget_tokens`` (estimated); else the notes and My notes (cut when they are very long) and the parts of the
    transcript closest to the question and to the one before it, with the gaps marked, and ``coverage`` says how
    much of the meeting the model saw."""
    question = _clip_chars(question, MAX_QUESTION_CHARS)
    transcript, _all_matched = material.marked()
    marked = transcript != material.transcript           # some lines got a time
    transcript = transcript.strip()
    notes, mine = (material.notes or "").strip(), (material.my_notes or "").strip()
    meeting = _section("meeting", _defang(_meeting_text(material)))
    q_section = _section("question", _defang(question))
    hist = _history_text(history, local)

    def fixed_tokens(hist_text):
        h = _section("conversation_so_far", hist_text) if hist_text else ""
        return estimate_tokens("\n\n".join(p for p in (meeting, h, q_section) if p)) + 80

    avail = budget_tokens - fixed_tokens(hist)
    if avail < 400 and hist:
        hist = ""                                # a tiny window: no room for the earlier answers
        avail = budget_tokens - fixed_tokens(hist)
    avail = max(0, avail)

    note_line = ("Speech recognition: words may be misheard."
                 + (" Times like [12:40] are minutes:seconds from the start of the recording." if marked else ""))
    full = (estimate_tokens(notes) + estimate_tokens(mine) + estimate_tokens(transcript)
            + estimate_tokens(note_line) + 60)
    if full <= avail:
        body, frac, complete = transcript, 1.0, True
    else:
        complete = False
        notes = _clip_tokens(notes, avail * (0.25 if local else 0.10))
        mine = _clip_tokens(mine, avail * (0.15 if local else 0.10))
        rest = avail - estimate_tokens(notes) - estimate_tokens(mine) - estimate_tokens(note_line) - 60
        previous = [m.get("text") for m in (history or []) if isinstance(m, dict) and m.get("role") == "user"][-1:]
        body, frac = select_windows(transcript, question + " " + " ".join(previous), max(0, rest))
    sections = [meeting]
    if notes:
        sections.append(_section("notes_written_by_gusan", _defang(notes)))
    if mine:
        sections.append(_section("users_own_notes", _defang(mine)))
    sections.append(_section("transcript", note_line + "\n" + _defang(body)))
    if hist:
        sections.append(_section("conversation_so_far", hist))
    sections.append(q_section)
    prompt = "\n\n".join(sections)
    total_min = int(round(material.duration_sec / 60.0)) if material.duration_sec else None
    pct = 100 if complete else min(99, int(round(frac * 100)))
    covered_min = (min(total_min, int(round(total_min * frac))) if total_min and not complete else total_min)
    cov = Coverage(complete, pct, covered_min, total_min)
    return Plan(prompt, cov, estimate_tokens(prompt), times=bool(marked))


# ── which AI answers ─────────────────────────────────────────────────────────
@dataclass
class EngineChoice:
    engine_id: str = ""
    kind: str = "none"                  # cloud (Gusan Cloud) | own_key | local | none
    label: str = ""                     # "Gusan Cloud · Mistral Small", "This computer · Qwen 3B", ...
    problem: str = ""                   # "" | needs_pro | signed_out | privacy_no_model | needs_download


def _on_disk_fn(downloaded):
    if downloaded is not None:
        return downloaded

    def check(model_id):
        try:
            import local_llm
            return bool(local_llm.model_downloaded(model_id))
        except Exception:
            return False
    return check


def _first_on_disk(on_disk):
    import local_llm
    for model_id in local_llm.MODEL_CATALOG:
        if on_disk(model_id):
            return model_id
    return None


def route_engine(pref, cfg, has_token):
    """AppController._resolve_action_engine without the network: ``pref`` (None = the saved action_model), then
    a signed-in Pro user's route through Gusan Cloud unless they deliberately picked a local model or a cloud
    engine with their own key."""
    cfg = cfg or {}
    engine = actions.normalize_action_model(pref if pref is not None else cfg.get("action_model"))
    if has_token:
        kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
        has_own_key = bool(((cfg.get("action_api_key") or "") or (cfg.get("google_api_key") or "")).strip())
        if kind in ("rules", "managed") or (kind == "cloud" and not has_own_key):
            engine = actions.API_MANAGED_ID
    return engine


def _choice(engine, kind, problem=""):
    return EngineChoice(engine_id=engine or "", kind=kind, label=meeting_session.engine_label(engine) if engine else "",
                        problem=problem)


def choose(engine, cfg, has_token, downloaded=None, signed_in=None):
    """Which engine really answers, from the routed ``engine``: Privacy mode keeps everything on this computer
    (the chosen local model, else the first one downloaded); otherwise a cloud engine needs its own key, Gusan
    Cloud a token, and what cannot run falls to a downloaded local model. Nothing that can run: ``problem``
    (needs_pro, or signed_out for a guest; privacy_no_model; needs_download for a local model not on disk).
    The built-in formatter never answers a question."""
    cfg = cfg or {}
    on_disk = _on_disk_fn(downloaded)
    kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
    if cfg.get("privacy_mode"):
        local = engine if kind == "local_llm" and on_disk(engine) else _first_on_disk(on_disk)
        if local:
            return _choice(local, "local")
        if kind == "local_llm":
            return _choice(engine, "local", "needs_download")
        return _choice("", "none", "privacy_no_model")
    usable = actions.usable_engine(engine, dict(cfg, _managed_token="x" if has_token else ""), on_disk)
    if usable is None:
        return _choice("", "none", "signed_out" if signed_in is False else "needs_pro")
    ukind = actions.ACTION_MODELS.get(usable, {}).get("kind")
    if ukind == "local_llm":
        return _choice(usable, "local", "" if on_disk(usable) else "needs_download")
    return _choice(usable, "cloud" if ukind == "managed" else "own_key")


def pick_engine(cfg, pro, has_token, override=None, downloaded=None, signed_in=None):
    """The engine that would answer now, with no network: the notes' engine (this meeting's ``override``, else
    cfg["meeting_notes_model"], else the saved action_model - meeting_session.notes_engine), the Pro routing
    (``has_token``: a signed-in Pro user), then choose(). The card asks it to decide what to show; the worker
    runs the same rules with the token it really got."""
    cfg = cfg or {}
    if override is None:
        override = cfg.get("meeting_notes_model") or None
    pref = meeting_session.notes_engine(cfg, pro, override, downloaded)
    return choose(route_engine(pref, cfg, has_token), cfg, has_token, downloaded, signed_in)


def problem_text(problem, choice=None):
    """The card's words for a pick_engine problem."""
    if problem == "signed_out":
        return SIGNED_OUT
    if problem == "privacy_no_model":
        return PRIVACY_NO_MODEL
    if problem == "needs_download":
        info = actions.ACTION_MODELS.get(getattr(choice, "engine_id", "") or "", {})
        name = info.get("label") or "a model"
        return "Download %s in Settings › Speech & AI to ask on this computer." % name
    return NEEDS_PRO


def local_budget():
    """Estimated tokens the whole user message may take on this computer's model (its window is fixed)."""
    import local_llm
    system = estimate_tokens(local_llm._CHAT_SYSTEM) + 48 + 16
    return max(1200, local_llm._N_CTX - LOCAL_OUT_TOKENS - LOCAL_MARGIN - system)


# ── errors ───────────────────────────────────────────────────────────────────
_TOO_LONG_RE = re.compile(
    r"context[ _-]?(?:length|window)|maximum context|too many tokens|prompt is too long|input is too long|"
    r"exceeds? the (?:maximum|limit|model)|token limit|request too large|reduce the length|too long for|"
    r"requested tokens exceed|payload too large|\b413\b", re.I)
_OFFLINE_RE = re.compile(
    r"network error|interrupted|connection|timed? ?out|max retries|name resolution|getaddrinfo|"
    r"temporary failure|unreachable|ssl", re.I)
_ALLOWANCE_RE = re.compile(r"fair-use|daily|quota|rate.?limit|too many requests|\b429\b|allowance|"
                           r"limit reached|resets at midnight", re.I)
_SERVER_CODE_RE = re.compile(r"action_failed|quota_check_failed|entitlement_check_failed|aren't set up")
_SERVER_RE = re.compile(r"\b50[0-9]\b|bad gateway|service unavailable|server error|overloaded|internal error", re.I)
_PRO_RE = re.compile(r"pro is required|sign in with (?:pro|your gusan)|sign in with a|requires? pro|"
                     r"no ai engine is connected|free cloud meeting note|free live assistant session|"
                     r"upgrade to gusan pro", re.I)
_KEY_RE = re.compile(r"api key|unauthori[sz]ed|invalid.*key|\b40[13]\b|authentication|permission", re.I)
_AUTH_RE = re.compile(r"unauthori[sz]ed|\b401\b|authentication", re.I)
AUTH = "Gusan Cloud didn't accept your sign-in. Sign in again from Account & plan."
_LOCAL_RE = re.compile(r"isn't downloaded|is not downloaded|couldn't load|couldn.t load|llama|local model|"
                       r"download it in", re.I)


def is_too_long(text):
    """Does an engine's error text say the prompt was too big for it?"""
    return bool(_TOO_LONG_RE.search(str(text or "")))


def friendly_error(text, kind="cloud"):
    """(code, a calm sentence) for the text of whatever went wrong. Never the raw text, never a URL."""
    t = str(text or "")
    own = kind == "own_key"
    if actions.CLOUD_ALLOWANCE_USED in t or (_ALLOWANCE_RE.search(t) and "Daily fair-use" in t):
        return "allowance", actions.CLOUD_ALLOWANCE_USED
    if _SERVER_CODE_RE.search(t):
        return "server", SERVER if not own else "Your AI service couldn't answer just now. Try again in a moment."
    if "privacy mode" in t.lower():
        return "privacy", PRIVACY_NO_MODEL
    if kind == "local":
        if _LOCAL_RE.search(t):
            return "local_missing", "This computer's AI isn't ready. Check Settings › Speech & AI."
        if is_too_long(t):
            return "too_long", TOO_LONG
        if action_api.THOUGHT_ONLY in t:
            return "thought_only", NO_ANSWER
        return "generic", GENERIC
    if is_too_long(t):
        return "too_long", TOO_LONG
    if _PRO_RE.search(t):
        return "needs_pro", NEEDS_PRO
    if action_api.THOUGHT_ONLY in t:
        return "thought_only", NO_ANSWER
    if _OFFLINE_RE.search(t):
        return "offline", OFFLINE_KEY if own else OFFLINE_CLOUD
    if _ALLOWANCE_RE.search(t):
        if own:
            return "allowance", "Your AI service says it can't take more questions right now. Try again in a moment."
        return "allowance", actions.CLOUD_ALLOWANCE_USED
    if _KEY_RE.search(t):
        if own:
            return "key", "Your AI key didn't work. Check it in Settings › Speech & AI."
        if _AUTH_RE.search(t):
            return "auth", AUTH
    if _SERVER_RE.search(t):
        return "server", SERVER if not own else "Your AI service couldn't answer just now. Try again in a moment."
    return "generic", GENERIC


# ── asking ───────────────────────────────────────────────────────────────────
class TokenBuffer:
    """The callback of a stream: keeps the text so far (a ReplaceText - the model's reasoning ended - starts it
    over), the time of the first real word, tells ``on_text`` the whole text after each piece, and raises
    Cancelled the moment ``cancelled()`` says so."""

    def __init__(self, on_text=None, cancelled=None):
        self.text = ""
        self.first_at = None
        self._on_text = on_text
        self._cancelled = cancelled

    @property
    def started(self):
        return bool(self.text.strip())

    def feed(self, delta):
        if self._cancelled is not None and self._cancelled():
            raise Cancelled(self.text)
        if isinstance(delta, action_api.ReplaceText):
            self.text = str(delta)
        else:
            self.text += str(delta or "")
        if self.first_at is None and self.text.strip():
            self.first_at = time.monotonic()
        if self._on_text is not None:
            self._on_text(self.text)


@dataclass
class Answer:
    text: str
    coverage: Coverage
    engine_label: str = ""
    kind: str = ""
    seconds: float = 0.0
    first_word_seconds: object = None
    dropped: bool = False               # the connection ended before the answer did: it may be cut short
    cut: bool = False                   # the engine stopped at the longest answer it may write
    sources: list = field(default_factory=list)


def ask(folder, question, history, *, cfg, pro, resolve, on_text=None, cancelled=None, stream=None,
        signed_in=None, material=None, me=""):
    """One question about the meeting in ``folder``, answered from it. Blocking: call it on a worker thread.

    ``history``: the chat so far (meeting_store.load_chat's rows, without this question). ``cfg``: the app's
    settings, ``pro``: whether the user is on Pro, ``resolve``: AppController._resolve_action_engine (adds the
    token - this can touch the network). ``on_text(text)`` gets the whole answer so far after each piece;
    ``cancelled()`` True stops it - Cancelled is raised, carrying what had arrived. Raises ChatError with a
    calm sentence for everything else. When an engine says the prompt was too big (before it wrote a word) a
    smaller one is sent, up to RETRIES times (RETRIES_OWN_KEY with the user's own key: nobody knows that
    provider's window), and the answer says how much of the meeting it saw. ``cancelled()`` is also asked
    before the meeting is read, after the sign-in check and right before the request goes out.

    Plans (Gusan 2.1): Gusan Cloud answers questions only for Gusan Pro - a free or signed-out user is never routed
    to it (not even while a Live Assistant session holds the month's free cloud allowance) and a question never
    carries the allowance fields (``_live_session_id``, ``_note_ref``): the chat is not metered, it is Pro. An own key
    and a model on this computer work for everybody (a guest too). Whatever the server still says about the plan
    ("allowance_used", "Pro is required") is the same calm needs-Pro line. ``me``: the recorder's first name (the
    profile), so the model can tell that "You" in the transcript is whom the notes name."""
    started = time.monotonic()
    cfg = cfg or {}
    stream = stream or actions.process_stream

    def check():
        """Stop pressed before a word came: nothing more is fetched, nothing is sent."""
        if cancelled is not None and cancelled():
            raise Cancelled("")

    check()
    material = material or load_material(folder)
    if not (material.transcript or "").strip():
        raise ChatError("no_transcript", NO_TRANSCRIPT)
    material.me = material.me or str(me or "")
    pref = meeting_session.notes_engine(cfg, pro, cfg.get("meeting_notes_model") or None)
    engine, config = resolve(pref)
    check()                                                 # (resolve can wait for a sign-in token)
    config = dict(config or cfg)
    for key in ALLOWANCE_KEYS:
        config.pop(key, None)                               # this question is not a Live Assistant session or a note
    if not pro:
        config.pop("_managed_token", None)                  # Gusan Cloud is Pro: the engine is read again without it
        engine = route_engine(pref, config, False)
    has_token = bool(config.get("_managed_token"))
    routed_managed = actions.ACTION_MODELS.get(engine, {}).get("kind") == "managed"
    if pro and routed_managed and not has_token and not config.get("privacy_mode"):
        raise ChatError("offline", OFFLINE_CLOUD)           # a signed-in Pro user whose token could not be fetched
    choice = choose(engine, config, has_token, signed_in=signed_in)
    if choice.problem:
        raise ChatError(choice.problem, problem_text(choice.problem, choice))
    local = choice.kind == "local"
    answer_info = {}
    config["_answer_info"] = answer_info
    config["_web_search"] = choice.kind == "cloud" and cfg.get("live_assist_web_search", True) and not cfg.get("privacy_mode")
    budget = local_budget() if local else (BUDGET_OWN_KEY if choice.kind == "own_key" else BUDGET_CLOUD)
    retries = RETRIES_OWN_KEY if choice.kind == "own_key" else RETRIES      # (its window is anybody's guess)
    attempt = 0
    while True:
        plan = plan_context(material, question, history, budget, local=local)
        check()
        buf = TokenBuffer(on_text, cancelled)
        action_api.begin_stream()                 # (process_stream does it too: a stream that is not it)
        try:
            text = stream(plan.prompt, actions.ACTION_MEETING_CHAT, buf.feed, model=choice.engine_id, config=config)
        except Cancelled:
            raise
        except Exception as e:                    # noqa: BLE001 - said in words, below
            too_long = isinstance(e, actions.ContextTooLong) or is_too_long(e)
            if attempt < retries and not buf.started and too_long:
                attempt += 1
                budget = int(budget * SHRINK)
                continue
            if too_long:
                code = "too_long"
                message = TOO_LONG_SMALL if choice.kind == "own_key" and budget < SMALL_BUDGET else TOO_LONG
            else:
                code, message = friendly_error(str(e), choice.kind)
                if getattr(e, "code", "") == "allowance_used":
                    code, message = "needs_pro", NEEDS_PRO      # the server meters the free allowances: a question is Pro
                if isinstance(e, actions.ActionError):      # (its text can echo the request: only the kind is kept)
                    logger.warning("Ask about this meeting: the AI route failed (%s, %s)", type(e).__name__, code)
                else:                                       # a bug, not a refusal: the log must show it
                    logger.warning("Ask about this meeting failed", exc_info=True)
            raise ChatError(code, message) from e
        break
    text = (text or buf.text or "").strip()
    if not text:
        raise ChatError("empty", NO_ANSWER)
    end = action_api.stream_end()
    dropped = bool(end.get("dropped"))
    now = time.monotonic()
    first = round(buf.first_at - started, 2) if buf.first_at else None
    return Answer(text=text, coverage=plan.coverage, engine_label=choice.label, kind=choice.kind,
                  seconds=round(now - started, 2), first_word_seconds=first,
                  dropped=dropped, cut=bool(end.get("cut")) and not dropped,
                  sources=answer_info.get("sources", []))


# ── the file ─────────────────────────────────────────────────────────────────
def dump_chat(messages):
    """chat.json's text for ``messages`` (meeting_store.clean_chat keeps only what belongs in it)."""
    return json.dumps({"version": meeting_store.CHAT_VERSION, "messages": meeting_store.clean_chat(messages)},
                      ensure_ascii=False, separators=(",", ":"))
