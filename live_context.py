"""Live Assistant logic with no Qt in it: what the model is sent, when the
screen goes along, when a piece of the conversation is a question the
companion should answer on its own, and whether the card is hidden on screen
share (hide_status: one truthful answer for the card's chip, the Live
Assistant page and Settings › Privacy). Kept out of ui/live_assist.py so it is
testable without a display (and without PySide6)."""
import inspect
import re
import sys
from collections import namedtuple

import invite_context

TAIL_CHARS = 2600               # ~3-4 minutes of speech fed to the model
HISTORY_TURNS = 2                # earlier answers kept for follow-up questions
# The session context the user writes: room for a brief and a job or product
# description, and still well inside a local model's 8k-token window next to
# the transcript tail.
CONTEXT_CHARS = 5000
# The documents the user added (live_docs.py): the best-matching passages for the
# question, as one block in the model's input. The whole block - header, labels and
# text - is never longer than this. A local model has a small (~8k-token) window
# shared with the transcript tail and the session context; the cloud routes have room.
LOCAL_DOC_CHARS = 3000
CLOUD_DOC_CHARS = 12000

SOLVE_SCREEN = ("Solve the problem or answer the question shown on my screen. "
                "Give the solution directly.")

_LANG_NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish",
               "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
               "hy": "Armenian", "tr": "Turkish", "zh": "Chinese", "ja": "Japanese",
               "ar": "Arabic"}


# What the user writes as "Context" is a STANDING INSTRUCTION: the first block of every
# prompt, on every route (Gusan Cloud, Gemini, the Claude / Mistral routes, a local model),
# worded the same everywhere. The system prompt (action_api.LIVE_ASSIST_SYSTEM) and the local
# one (local_llm) explain it. It is the user's own text, so it is trusted for instructions;
# the conversation, documents and invite are data (their own delimiters stay as they are).
STANDING_HEADER = ("Standing instructions from the user - follow them in every answer; they "
                   "override your defaults on language, format and length:")


def standing_block(text):
    """The user's context as the first block of a prompt ("" when there is none)."""
    ctx = clip_context(text)
    return STANDING_HEADER + "\n" + ctx if ctx else ""


_FORGED_HEADER = re.compile(r"standing\s+instructions\s+from\s+(?:the\s+)?user", re.I)


# The labels of the prompt's own sections: speech that says one of them ("User's question: ...")
# must not read as the start of that section.
_FORGED_LABEL = re.compile(
    r"(user['’]s\s+question|conversation\s*\(latest\s+part\)|latest\s+piece\s+of\s+the\s+conversation"
    r"|earlier\s+in\s+the\s+conversation|reference\s+material\s+the\s+user\s+added)\s*:", re.I)


def defuse(text):
    """Untrusted talk (what other people said on the call) that cannot pass for the user's
    own heading: a speaker, or a pasted line, saying "Standing instructions from the user ..."
    becomes "standing instructions from the speaker", and one saying "User's question: ..." or
    another section's label loses the colon that would make it one. Everything else is left as
    it is."""
    return _FORGED_LABEL.sub(lambda m: m.group(1) + " -", defuse_header(text))


def defuse_header(text):
    """Only the heading: text someone else wrote (a meeting title, an invite's agenda) that says
    "Standing instructions from the user ..." is a quote of it, not the heading. (Its rows are
    bullets and framed as data already - see invite_context - so its other words stay.)"""
    return _FORGED_HEADER.sub("standing instructions from the speaker", text or "")


def respond_line(output_lang, standing=False):
    """The "(Respond in ...)" line for the "Answer in" setting ("" for the call's own
    language). With standing instructions in the prompt it gives way to them: "translate
    everything to French" must still win over an "Answer in English" set long ago."""
    if not output_lang or output_lang == "auto":
        return ""
    name = _LANG_NAMES.get(output_lang, output_lang)
    if standing:
        return f"(Respond in {name}, unless the standing instructions above say otherwise.)"
    return f"(Respond in {name}.)"


def clip_context(text, limit=CONTEXT_CHARS):
    """The session context as the model gets it: trimmed, at most ``limit``
    characters. (Whatever the user wrote there is a standing instruction: see
    STANDING_HEADER and live_rules.)"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def rolling_context(live_text, question="", title="", attendees="",
                    tail_chars=TAIL_CHARS, screen=False, output_lang="en", history=(),
                    session_context="", documents=None, doc_chars=None, pictures=(), invite=""):
    """The text handed to the model: recent conversation + optional question,
    plus the last answers of this session (``history``: (asked, answer) pairs)
    so a follow-up like "and the second part?" has something to refer to.
    ``session_context`` is what the user wrote about the call - it holds for
    every answer of the session. Cuts at a sentence boundary when it can so
    the model doesn't start mid-word.

    ``documents``: the ACTIVE documents the user added (live_docs.DocText, from
    DocStore.active_docs()). The passages that best match the question and the
    latest talk go in one delimited "reference material" block - data, never
    instructions - after the session context and before the conversation, never
    longer than ``doc_chars`` (LOCAL_DOC_CHARS for a local model, CLOUD_DOC_CHARS
    otherwise; default cloud). ``pictures``: the names of picture documents attached
    to this answer, named in that block. Without either, nothing changes.

    ``invite``: the agenda of the calendar invite this session started from (what
    the user can see and edit on the card's invite chip). It is written by whoever
    made the invite, so it goes in its own section, plain and capped, framed as
    data (invite_context.invite_block) - after the meeting line, before the
    documents. Empty adds nothing; it is never merged into ``session_context``
    (the user's own "About my calls" text)."""
    tail = defuse((live_text or "").strip())
    if len(tail) > tail_chars:
        tail = tail[-tail_chars:]
        cut = re.search(r"[.!?]\s+", tail)
        if cut and cut.end() < len(tail) // 2:
            tail = tail[cut.end():]
    parts = []
    standing = standing_block(session_context)
    if standing:
        # First, and the same on every call of the session: the rest is read
        # in its light, and the endpoint can cache it along with the brief. It is
        # the user's standing instruction - what to do with what is said, how to
        # answer - not just a note (see STANDING_HEADER).
        parts.append(standing)
    # The meeting line (title, at most MAX_PEOPLE names; a placeholder title like
    # "Live session 14:30" says nothing and is left out), then the calendar
    # invite's agenda as its own section - framed as data, never instructions.
    meeting = defuse_header(invite_context.live_lines(title, attendees))
    if meeting:
        parts.append(meeting)
    block = defuse_header(invite_context.invite_block(invite))
    if block:
        parts.append(block)
    if documents or pictures:
        import live_docs
        block = live_docs.documents_context(
            documents, question, tail[-live_docs.QUERY_CONVERSATION_CHARS:],
            CLOUD_DOC_CHARS if doc_chars is None else doc_chars, pictures)
        if block:
            parts.append(block)              # (its lines are quoted "> ..." inside its own delimiters)
    earlier =[(q, a) for q, a in list(history)[-HISTORY_TURNS:] if (a or "").strip()]
    if earlier:
        lines = ["Your earlier answers in this conversation (for follow-ups only):"]
        for q, a in earlier:
            a = " ".join(a.split())
            lines.append(f"- {('Q: ' + q.strip() + ' A: ') if (q or '').strip() else ''}"
                         f"{a[:280]}{'…' if len(a) > 280 else ''}")
        parts.append("\n".join(lines))
    parts.append("Conversation (latest part):\n" + (tail or "(nothing transcribed yet)"))
    if screen == "snip":
        # A part of the screen the user picked (or an image they pasted) for
        # this question: here the image IS the subject.
        parts.append("(A screenshot of the user's screen is attached. It is the part "
                     "of the screen the user picked for this question - use it as the "
                     "main context.)")
    elif screen:
        # Context, not the task: with Screen on a screenshot rides along with
        # every answer, relevant or not (the system prompt says the same).
        parts.append("(A screenshot of the user's screen is attached. Use it as "
                     "context; solve what it shows only when that is what is being "
                     "asked or nothing was asked.)")
    line = respond_line(output_lang, bool(standing))
    if line:
        parts.append(line)
    if (question or "").strip():
        parts.append("User's question: " + question.strip())
    return "\n\n".join(parts)


def should_attach_screen(screen_on, force=False):
    """Screen context is on or off - no keyword guessing. Guessing from the
    words ("slide", "error"...) missed most real questions about the screen
    ("how do I solve this?"), so with Screen on every answer sees the screen.
    ``force`` is an explicit request (the Solve screen button)."""
    return bool(screen_on or force)


# ── the calendar invite: starting a session with it ──
def takes_kwarg(fn, name):
    """True when ``fn(name=...)`` is accepted: a parameter of that name, or **kwargs."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def start_session(session, agenda="", **kw):
    """``session.start(**kw)`` with the invite's agenda (what the user sees on the
    start card or the Live card). It goes in as ``agenda=`` when start() takes it;
    otherwise (a recorder built before agendas) it is set with ``set_agenda`` after a
    good start. Returns start()'s own result: (ok, code, message)."""
    agenda = invite_context.clip_agenda(agenda)
    inline = bool(agenda) and takes_kwarg(session.start, "agenda")
    if inline:
        kw["agenda"] = agenda
    result = session.start(**kw)
    ok = bool(result[0]) if isinstance(result, (tuple, list)) and result else bool(result)
    setter = getattr(session, "set_agenda", None)
    if agenda and ok and not inline and callable(setter):
        try:
            setter(agenda)
        except Exception:
            pass                                 # the card still has it; the notes lose only the header
    return result


def others_of(attendees, me=""):
    """The people on a call without you: ``attendees`` (a list or the comma string a
    meeting keeps) minus ``me`` (your full name or first name). Best effort: when
    nobody matches, everybody stays."""
    names = invite_context.cap_names(attendees)
    full = " ".join(str(me or "").split()).lower()
    if not full:
        return names
    first = full.split()[0]
    return [n for n in names if n.lower() not in (full, first)]


# ── the reply path ──
# The card renders an answer as Markdown in a QTextEdit, and Qt's text widgets load
# an image named in it from the local disk: a file:// path, or a \\host\share path
# on Windows, which is a network connection (the PC's sign-in can leak to that
# host). Text the model read - a document, a web page on screen - could make it
# write one. An answer here never needs an image, so every form is replaced by its
# alt text BEFORE anything is rendered (the card calls this in render_markdown).
_MD_IMAGE = re.compile(r"!\[([^\]\n]{0,200})\](?:\([^)\n]{0,2000}\)|\[[^\]\n]{0,200}\])")
# (the blanks around the "/" are bounded: "<" followed by thousands of spaces - a reply can
# have them - made "<\s*/?\s*" backtrack quadratically and froze the card for minutes)
_HTML_MEDIA = re.compile(
    r"<\s{0,8}/?\s{0,8}(?:img|image|picture|source|video|audio|iframe|embed|object|link|svg|"
    r"script|style)\b[^>\n]{0,2000}>?", re.I)


def safe_answer_markdown(text):
    """``text`` (an answer, as Markdown) with every image taken out: ![alt](url) and
    ![alt][ref] become their alt text, raw <img>/<svg>/<iframe>... tags vanish, and
    any other "![" (a shortcut reference, a long or multi-line alt) gets a space, so
    it can never start an image (a backslash would not do: "\\\\![" is an image
    again). Plain text, links and code stay as they were."""
    text = _MD_IMAGE.sub(lambda m: m.group(1).strip(), text or "")
    text = _HTML_MEDIA.sub("", text)
    return text.replace("![", "! [")


# Speech recognition without a "?": a request is one however it ends ("Walk me
# through it."); a wh-/yes-no opener only when the sentence has no full stop -
# "What I would do first is add a cache." is a statement (and often the user
# reading the answer aloud, which must not trigger a new one).
_REQUEST_OPENER = re.compile(
    r"^(can you|could you|would you|will you|tell me|explain|describe|"
    r"walk me through|give me|show me)\b", re.I)
_QUESTION_OPENER = re.compile(
    r"^(what|how|why|when|where|who|whom|whose|which|do you|did you|have you|"
    r"are you|is it|is there|are there|should (i|we))\b", re.I)


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?։՞])\s+", text or "") if s.strip()]


def looks_like_question(piece):
    """True when freshly transcribed speech asks something - the moment the
    companion answers without being asked. Whisper punctuates questions in
    most languages (Armenian marks them with '՞'); without punctuation, an
    opener like "what" or "tell me" counts."""
    p = (piece or "").strip()
    if not p:
        return False
    if any(mark in p for mark in ("?", "՞", "？", "¿")):
        return True
    for s in _sentences(p):
        if _REQUEST_OPENER.match(s):
            return True
        if _QUESTION_OPENER.match(s) and not s.endswith((".", "!", "։", "…")):
            return True
    return False


def last_question(text):
    """The last question-like sentence of ``text`` ("" if none)."""
    for s in reversed(_sentences(text)):
        if looks_like_question(s):
            return s[:200]
    return ""


# ── Hidden on screen share ──
# The card is kept out of screen capture (ui/glass.py), so it never shows on a
# shared screen or in a recording while the user still sees it. The chip, the
# Live Assistant page and Settings › Privacy all say what the OS really does,
# from this one place: "Hidden" only where hiding is promised (Windows 10 2004+
# in a local session) and, once the card is open, confirmed by its window.
# macOS is best effort ("May be hidden") and never "invisible".

HIDE_KEYS = ("on", "off", "failed", "partial", "unavailable")

# The chip's words. Short: the card header is tight (at most 24 characters).
HIDE_TEXT = {"on": "Hidden", "off": "Visible", "failed": "Visible",
             "partial": "May be hidden", "unavailable": "Not available"}
# Tones of ui/components Chip and icon names shared by ui/icons (the card)
# and ui/components/icons (pages): green incognito, red open eye, amber.
HIDE_TONE = {"on": "ok", "off": "red", "failed": "red", "partial": "amber",
             "unavailable": "amber"}
HIDE_ICON = {"on": "incognito", "off": "eye", "failed": "eye", "partial": "eye_off",
             "unavailable": "eye"}

HideStatus = namedtuple("HideStatus", "key text tone icon headline tip")
HideStatus.__doc__ = """Whether the Live Assistant card is hidden on screen share.

key       on | off | failed | partial | unavailable (see private_state)
text      the chip's word: Hidden / Visible / May be hidden / Not available
tone      ui/components Chip tone: ok | red | amber
icon      incognito | eye | eye_off
headline  a page line: "Hidden on screen share", "Visible on screen share"...
tip       one or two plain sentences on what it means (no "click" wording)"""


def private_state(wanted, supported, remote, excluded, partial=False):
    """(key, chip text) - always TRUTHFUL about what the OS actually did. The
    chip is the whole privacy promise; it must never say hidden when the
    window is in fact capturable (macOS, old Windows, RDP, API refusal).

    ``partial``: macOS, where the window's sharingType is set to none and
    reads back so - some capture paths honour it, some don't (QuickTime,
    Zoom's default capture mode), and Apple promises nothing. Never "Hidden"."""
    if not wanted:
        key = "off"
    elif remote:
        key = "unavailable"
    elif not supported:
        key = "partial" if partial else "unavailable"
    elif excluded:
        key = "on"
    else:
        key = "failed"
    return key, HIDE_TEXT[key]


def mac_partial_tip(macos26=False):
    """What "May be hidden" means on macOS: the sharingType flag (what
    Electron's setContentProtection sets) is honoured by some capture paths,
    not all - see glass.capture_exclusion_supported. Never claims more."""
    caveat = "Not yet verified on macOS 26." if macos26 else "Apple doesn't guarantee it."
    return ("Uses macOS's private-window setting. Screenshots skip it, and so do some "
            "screen shares (reportedly Zoom's “…with window filtering” capture "
            "modes), but QuickTime and ⌘⇧5 recordings show it, and so can other "
            "apps and settings. " + caveat + " Test from a second device before relying on "
            "it, or share a single window.")


def _glass():
    """ui.glass (plain ctypes, no Qt), or None where it can't load."""
    try:
        from ui import glass
        return glass
    except Exception:
        return None


def _hide_tip(key, mac, remote, supported, macos26):
    if key == "on":
        return ("Hidden from screen shares, recordings and screenshots on this computer "
                "(Zoom, Teams, Meet and the rest). Not from a phone camera.")
    if key == "off":
        return "Shows on a shared screen and in recordings."
    if key == "failed":
        return ("macOS" if mac else "Windows") + (" refused to hide it - assume it shows "
                                                   "in a share.")
    if key == "partial":
        return mac_partial_tip(macos26)
    if remote:
        return ("Remote Desktop can't hide it: the remote view is itself a screen capture, "
                "so hiding it would hide it from you too. Assume it shows in a share.")
    if mac:
        return ("macOS didn't take the private-window setting, so assume it shows in "
                "screen shares and recordings. Share a single window - your browser or "
                "the document - to keep it out.")
    if sys.platform == "win32" and not supported:
        return ("Hiding from screen shares needs Windows 10 (version 2004) or later. "
                "Assume it shows in a share.")
    return "Hiding from screen shares isn't available here. Assume it shows in a share."


def _hide_headline(key, remote):
    if key == "on":
        return "Hidden on screen share"
    if key == "partial":
        return "May be hidden on screen share"
    if key == "unavailable":
        return "Can't hide in Remote Desktop" if remote else "Can't hide on this computer"
    return "Visible on screen share"


def hide_status(wanted, excluded=None, partial=None, supported=None, remote=None,
                mac=None, macos26=None):
    """Is the Live Assistant card hidden on screen share? -> HideStatus.

    ``wanted``: the user's choice (cfg "live_assist_private"; Settings ›
    Privacy can ask the same for "overlay_private").
    ``excluded``: what the OPEN card's window reported (True/False). None when
    no card window exists yet: then this says what opening it will do -
    Windows 10 2004+ in a local session hides it (the open card re-checks and
    says "Visible" if Windows refuses), macOS sets its best-effort flag.
    ``partial``: macOS with the flag set on the open window (None = derive).
    ``supported`` / ``remote`` / ``mac`` / ``macos26`` default to this
    computer (glass.capture_exclusion_supported, glass.is_remote_session,
    sys.platform, glass.macos_26_or_later); tests pass them."""
    glass = None
    if supported is None or remote is None:
        glass = _glass()
    if supported is None:
        supported = bool(glass and glass.capture_exclusion_supported())
    if remote is None:
        remote = bool(glass and glass.is_remote_session())
    if mac is None:
        mac = sys.platform == "darwin"
    if excluded is None:
        excluded = bool(supported)
        if partial is None:
            partial = bool(mac and not supported)
    elif partial is None:
        partial = bool(mac and not supported and excluded)
    key, text = private_state(bool(wanted), bool(supported), bool(remote), bool(excluded),
                              partial=bool(partial))
    if key == "partial" and macos26 is None:
        glass = glass or _glass()
        macos26 = bool(glass and glass.macos_26_or_later())
    return HideStatus(key, text, HIDE_TONE[key], HIDE_ICON[key],
                      _hide_headline(key, remote),
                      _hide_tip(key, mac, remote, supported, bool(macos26)))
