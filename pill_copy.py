"""The dictation pill's words (ui/overlay.py): what it says while your words
are transcribed, while a Smart Action writes, and once it is done.

Qt-free and instant - a few regexes and dict lookups - because nothing here
may slow a dictation or a Smart Action down. Nothing here raises either: a
guess that can't be made is simply left out ("Thinking", no detail).

* intent(text) -> (kind, detail): what a Smart Action is about to write, from
  the transcript. kind is one of KINDS or None; detail is the language for a
  translation ("Spanish"), else "".
* busy(stage, kind, detail, ctx) -> (title, sub) for stage "transcribing"
  (dictation and Smart) or "writing" (Smart, after the transcription).
* done(kind, pasted) -> the "done" line.
* speech_info(backend, model) / writer_info(runs_on dict) / task(text, app):
  the context main.py hands the pill (the speech model, the writer, the kind).
* warm(): imports engine_choice on a worker thread once (the overlay calls it
  when a take starts), so the stop never pays for that import (~30 ms).
  Until it has landed the names come from here instead.
"""

import re
import sys
import threading

# What a Smart Action is writing. None = no clear ask (the writer tidies up).
EMAIL, LIST, TRANSLATE, SUMMARY, POLISH, NOTES, REPLY, MESSAGE = (
    "email", "list", "translate", "summary", "polish", "notes", "reply", "message")
KINDS = (EMAIL, LIST, TRANSLATE, SUMMARY, POLISH, NOTES, REPLY, MESSAGE)

TRANSCRIBING = "transcribing"
WRITING = "writing"

# ── timing (s) ──
SLOW_S = 4.0                 # busy this long -> "Almost there" / "Still writing"
LONG_S = 12.0                # transcribing on this computer this long -> "Long take"
LOCAL_WRITER_SLOW_S = 10.0   # a local writer this long -> "This computer is working on it"
LONG_AUDIO_S = 45.0          # a take this long says how much audio there is
SHORT_AUDIO_S = 20.0         # under this a slow transcription isn't a "long take"

ON_THIS_COMPUTER = "on this computer"
GUSAN_CLOUD = "Gusan Cloud"

# smart_prompt.detect_action_mode's modes -> what the pill calls them.
_MODE_KIND = {
    "write_email": EMAIL, "make_todo_list": LIST, "translate": TRANSLATE,
    "summarize": SUMMARY, "rewrite": POLISH, "meeting_notes": NOTES,
}
_REPLY_RE = re.compile(r"\breply\b", re.I)
_EMAIL_RE = re.compile(r"\be-?mail\b", re.I)
# smart_prompt's "email" rule also fires on "message", "draft" and "compose".
# The built-in rules then write an email; an AI writer writes what was asked,
# so for it the pill only says "email" when an email was asked for.
_EMAIL_WORDS = ((REPLY, re.compile(r"\b(?:reply|respond(?:ing)?\s+to|write\s+back)\b", re.I)),
                (EMAIL, _EMAIL_RE),
                (MESSAGE, re.compile(r"\bmessage\b", re.I)))
# Asks an AI writer understands that smart_prompt's rules don't name.
_AI_RULES = (
    (REPLY, re.compile(r"\brespond(?:ing)?\s+to\b|\bwrite\s+back\b", re.I)),
    (LIST, re.compile(r"\bbullet(?:ed)?[- ]?(?:points?|list)\b|\b(?:into|as) a list\b|\blist of\b", re.I)),
    (POLISH, re.compile(r"\bproofread\b|\bfix (?:my|the) (?:grammar|spelling|typos?)\b|"
                        r"\bmore (?:professional|formal|polite|friendly)\b", re.I)),
    (TRANSLATE, re.compile(r"\bhow (?:do|would|can) (?:you|i) say\b", re.I)),
)
_SAY_RE = _AI_RULES[-1][1]
_HEAD, _TAIL = 600, 300          # chars of a long take where its ask is looked for
_TRANSLATE_WORD_RE = re.compile(r"\b(?:translate|translation)\b", re.I)

# Language codes -> English names: actions.TRANSLATE_TARGETS' languages (the
# tests keep the two in step) plus common codes someone may say ("to ja").
_CODE_NAMES = {
    "en": "English", "hy": "Armenian", "ru": "Russian", "fr": "French",
    "de": "German", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
    "ar": "Arabic", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
    "nl": "Dutch", "pl": "Polish", "uk": "Ukrainian", "tr": "Turkish",
    "el": "Greek", "sv": "Swedish", "hi": "Hindi", "he": "Hebrew",
    "fa": "Persian",
}
# Named anywhere after "to" / "into" / "in": "can you translate this into Japanese".
_LANGUAGES = (
    "English", "Spanish", "French", "German", "Italian", "Portuguese", "Russian",
    "Armenian", "Arabic", "Chinese", "Mandarin", "Cantonese", "Japanese", "Korean",
    "Hindi", "Turkish", "Dutch", "Polish", "Ukrainian", "Greek", "Swedish",
    "Norwegian", "Danish", "Finnish", "Czech", "Hungarian", "Romanian", "Bulgarian",
    "Hebrew", "Persian", "Farsi", "Urdu", "Bengali", "Vietnamese", "Thai",
    "Indonesian", "Malay", "Filipino", "Tagalog", "Swahili", "Georgian", "Serbian",
    "Croatian", "Slovak", "Catalan",
)
_LANG_RE = re.compile(r"\b(?:to|into|in)\s+(" + "|".join(_LANGUAGES) + r")\b", re.I)
# "rewrite this in Spanish" / "write it again in French": a translation.
_REWRITE_IN_RE = re.compile(r"\b(?:rewrite|write (?:it|this|that))\b(?:\s+[\w']+){0,2}?\s+(?:in|into)\s+("
                            + "|".join(_LANGUAGES) + r")\b", re.I)


# ── what is being written ────────────────────────────────────────────────────
def _earliest(rules, text):
    """(name, position) of the rule that matches first in ``text``."""
    best, best_at = None, None
    for name, pattern in rules:
        m = pattern.search(text)
        if m and (best_at is None or m.start() < best_at):
            best, best_at = name, m.start()
    return best, best_at


def _ai_intent(smart_prompt, text):
    """What an AI writer is asked for: the ask said FIRST wins ("summarize
    this email" is a summary, "remind me to email John" a list) - closer to
    what a writer model does than detect_action_mode's fixed rule order.
    smart_prompt's rules plus a few asks they don't name (_AI_RULES)."""
    rules = getattr(smart_prompt, "_DETECT_RULES", None)
    if not rules:
        return _MODE_KIND.get(smart_prompt.detect_action_mode(text))
    mode, at = _earliest(rules, text)
    kind = _MODE_KIND.get(mode)
    extra, extra_at = _earliest(_AI_RULES, text)
    if extra and (at is None or extra_at < at):
        kind = extra
    if kind == EMAIL:
        # "Write a message" isn't an email, "draft a post" isn't either.
        kind = _earliest(_EMAIL_WORDS, text)[0]
    return kind


def _rules_intent(smart_prompt, text):
    """The built-in rules (no AI model) route by detect_action_mode's order
    and write an email for every "email" match - except a reply is named."""
    kind = _MODE_KIND.get(smart_prompt.detect_action_mode(text))
    if kind == EMAIL:
        r, e = _REPLY_RE.search(text), _EMAIL_RE.search(text)
        if r and (e is None or r.start() < e.start()):
            kind = REPLY
    return kind


def language_name(code):
    """'es' -> 'Spanish'; '' for a code it doesn't know."""
    return _CODE_NAMES.get(str(code or "").strip().lower(), "")


def _target_language(smart_prompt, text):
    try:
        name = language_name(smart_prompt.parse_translate_target(text))
    except Exception:
        name = ""
    if name:
        return name
    m = _LANG_RE.search(text)
    return m.group(1).title() if m else ""


def intent(text, rules=False):
    """(kind, detail) a Smart Action is about to write, from its transcript.

    ``rules``: the writer is the built-in rules (no AI model), which route by
    smart_prompt.detect_action_mode's rule order - then the pill follows that
    order. Never slow (regexes over at most ~1,100 chars: a long take's start
    and end), never raises: (None, "") if unsure."""
    if not isinstance(text, str) or not text.strip():
        return None, ""
    if len(text) > _HEAD + _TAIL + 200:
        # A long take: the ask is said at its start or its end. Looking only
        # there keeps this ~1 ms however long the take (the whole of a
        # 25-minute take is ~20 ms of regexes), and the order still holds.
        text = text[:_HEAD] + " … " + text[-_TAIL:]
    try:
        import smart_prompt
        if rules:
            kind = _rules_intent(smart_prompt, text)
        else:
            kind = _ai_intent(smart_prompt, text)
            if kind in (POLISH, None) and _REWRITE_IN_RE.search(text):
                kind = TRANSLATE          # "rewrite this in Spanish" is a translation
        detail = _target_language(smart_prompt, text) if kind == TRANSLATE else ""
        if kind == TRANSLATE and not detail and _SAY_RE.search(text) \
                and not _TRANSLATE_WORD_RE.search(text):
            kind = None                   # "how do you say ..." with no language: a question
        return kind, detail
    except Exception:
        return None, ""


# ── while it works ───────────────────────────────────────────────────────────
_WRITING_TITLE = {
    EMAIL: "Writing your email", LIST: "Making your list", TRANSLATE: "Translating",
    SUMMARY: "Summarizing", POLISH: "Polishing your words", NOTES: "Writing notes",
    REPLY: "Writing a reply", MESSAGE: "Writing your message", None: "Thinking",
}
_STILL = {
    EMAIL: "Still writing", LIST: "Still writing", NOTES: "Still writing",
    REPLY: "Still writing", MESSAGE: "Still writing", TRANSLATE: "Still translating",
    SUMMARY: "Still summarizing", POLISH: "Still polishing", None: "Still thinking",
}


def _num(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _flag(ctx, *keys):
    """The first of ``keys`` that is set (True/False), else None."""
    for key in keys:
        value = ctx.get(key)
        if value is not None:
            return bool(value)
    return None


def audio_length(seconds):
    """'2 min of audio' for a long take; '' under LONG_AUDIO_S."""
    s = _num(seconds)
    if s < LONG_AUDIO_S:
        return ""
    return f"{max(1, int(s / 60.0 + 0.5))} min of audio"


def _transcribing(ctx, elapsed):
    seconds = ctx.get("seconds")
    long_audio = audio_length(seconds)
    local = _flag(ctx, "speech_local", "local")
    if elapsed >= LONG_S:
        if local is not False:
            short = seconds is not None and _num(seconds) < SHORT_AUDIO_S
            if not short:
                return "Long take", "Still transcribing"
            return "Transcribing", "This computer is working on it"
        return "Transcribing", "Still working"
    if long_audio:
        return "Transcribing", long_audio
    if elapsed >= SLOW_S:
        return "Transcribing", "Almost there"
    sub = str(ctx.get("language") or "").strip() or str(ctx.get("speech") or "").strip()
    return "Transcribing", sub


def writer_line(ctx):
    """'Gusan Cloud', 'Qwen 1.5B · on this computer', 'Ministral 14B', or ''."""
    writer = str(ctx.get("writer") or "").strip()
    local = _flag(ctx, "writer_local", "local")
    if local is None:
        local = ON_THIS_COMPUTER in str(ctx.get("where") or "").lower()
    if local and writer:
        return f"{writer} · {ON_THIS_COMPUTER}"
    return writer


ON_SELECTION = "On your selection"
SEARCHING_WEB = "Searching the web"
RESEARCHING_WEB = "Researching the web"          # a deep search ("do a deep search on...")


def web_sources_line(n):
    """The done line's tail when a Smart Action used the web: "1 web source" / "3 web sources"."""
    return "1 web source" if n == 1 else "%d web sources" % n


def _writing(kind, detail, ctx, elapsed):
    kind = kind if kind in KINDS else None
    title = _WRITING_TITLE[kind]
    if kind == TRANSLATE and str(detail or "").strip():
        title = f"Translating to {str(detail).strip()}"
    local = _flag(ctx, "writer_local", "local")
    if elapsed >= LOCAL_WRITER_SLOW_S and local:
        return title, "This computer is working on it"
    if elapsed >= SLOW_S:
        return title, _STILL[kind]
    line = writer_line(ctx)
    if ctx.get("web"):                                # Gusan Cloud looks the facts up first (Linkup)
        what = RESEARCHING_WEB if ctx.get("web") == "deep" else SEARCHING_WEB
        line = what + (" \u00b7 " + line if line else "")
    elif ctx.get("on_selection"):                     # the take works on the text selected on screen: say so
        line = ON_SELECTION + (" \u00b7 " + line if line else "")
    return title, line


# ── the steps of a Smart Action, in order (founder, 2026-11-11) ──────────────
# "If we're doing something, first transcribe, then search the web, then summarize, then make a bullet point format -
# the icons in that order, and show them all until it has done them." The pill shows a row of the take's steps -
# transcribe, the web (when it searches), then every kind of writing asked for in a logical order (condense first,
# then shape, then language, then the message it goes in) - the one in progress moving, the done ones dim, the next
# ones faint. The server answers all at once (it searches first, ~2 s, then writes), so the pill moves on by time:
# the web for WEB_STEP_S, then each writing step for KIND_STEP_S, the last one until the answer comes.
STEP_TRANSCRIBE, STEP_WEB, STEP_THINK = "transcribe", "web", "think"
WEB_STEP_S = 2.0
KIND_STEP_S = 1.6
MAX_KINDS = 4
_KIND_ORDER = (SUMMARY, NOTES, LIST, POLISH, TRANSLATE, REPLY, EMAIL, MESSAGE)
_KIND_RES = (
    (SUMMARY, re.compile(r"\bsumm?ar(?:y|ies|i[sz]e|i[sz]ing)\b|\brecap\b|\btl;?dr\b|\bkey (?:points|takeaways)\b|"
                         r"\bgist\b|\bcondense\b|\bshorten\b", re.I)),
    (NOTES, re.compile(r"\b(?:meeting )?notes\b|\bmeeting minutes\b", re.I)),   # (not "in ten minutes")
    (LIST, re.compile(r"\bbullet(?:ed)?(?:[- ]?points?| list)?\b|\b(?:to-?do|check) ?list\b|\blist\b|\bnumbered\b",
                      re.I)),
    (POLISH, _AI_RULES[2][1]),
    (TRANSLATE, re.compile(r"\btranslat(?:e|ion|ing)\b", re.I)),
    (REPLY, _EMAIL_WORDS[0][1]),
    (EMAIL, _EMAIL_RE),
    (MESSAGE, _EMAIL_WORDS[2][1]),
)


def kinds(text, main=None):
    """Every kind of writing a Smart Action's request asks for, in the order the work is done (_KIND_ORDER), at most
    MAX_KINDS; ``main`` (intent()'s kind) is always one of them. Never raises."""
    found = {main} if main in KINDS else set()
    try:
        t = str(text or "")
        if len(t) > _HEAD + _TAIL + 200:
            t = t[:_HEAD] + " … " + t[-_TAIL:]
        for kind, rx in _KIND_RES:
            if rx.search(t):
                found.add(kind)
        if TRANSLATE not in found and _REWRITE_IN_RE.search(t):
            found.add(TRANSLATE)
    except Exception:
        pass
    if REPLY in found:
        found.discard(EMAIL)                         # a reply is the email
    if EMAIL in found:
        found.discard(MESSAGE)
    return [k for k in _KIND_ORDER if k in found][:MAX_KINDS]


def steps(kind_list, web=False):
    """The take's steps in order: transcribe, the web (when it searches), then its kinds of writing (or "think")."""
    out = [STEP_TRANSCRIBE]
    if web:
        out.append(STEP_WEB)
    out.extend([k for k in (kind_list or ()) if k in KINDS] or [STEP_THINK])
    return out


def current_step(step_list, elapsed, writing=None, waiting=False):
    """(index, seconds in that step) of the step in progress, ``elapsed`` seconds after the Smart Action began (the
    transcription is done by then). ``writing``: seconds since the answer started coming (a streamed answer says
    when), None when not known; ``waiting``: the answer will say - the web step lasts until it does, never a timer."""
    if len(step_list) < 2:
        return 0, _num(elapsed)
    t = max(0.0, _num(elapsed))
    i = 1
    if step_list[1] == STEP_WEB:
        if writing is not None:
            if len(step_list) == 2:
                return 1, t
            t = max(0.0, _num(writing))
        elif waiting or t < WEB_STEP_S or len(step_list) == 2:
            return 1, t
        else:
            t -= WEB_STEP_S
        i = 2
    k = min(len(step_list) - 1 - i, int(t // KIND_STEP_S))
    return i + k, t - k * KIND_STEP_S


def step_line(step_list, index, detail="", ctx=None):
    """(title, sub) for the step in progress."""
    ctx = ctx if isinstance(ctx, dict) else {}
    key = step_list[index] if 0 <= index < len(step_list) else STEP_THINK
    if key == STEP_WEB:
        title = RESEARCHING_WEB if ctx.get("web") == "deep" else SEARCHING_WEB
        line = writer_line(ctx)
        if ctx.get("on_selection"):
            line = ON_SELECTION + (" \u00b7 " + line if line else "")
        return title, line
    kind = key if key in KINDS else None
    last = index >= len(step_list) - 1
    # the slow-down lines ("Still writing") only on the last step, which lasts until the answer comes
    elapsed = _num(ctx.get("elapsed")) if last else 0.0
    return _writing(kind, detail if kind == TRANSLATE else "", dict(ctx, web=False), elapsed)


def busy(stage, kind=None, detail="", ctx=None):
    """(title, sub) while the pill works.

    stage "transcribing" (dictation and Smart) or "writing" (a Smart Action,
    after its transcription). ``ctx`` may carry: smart (bool), elapsed
    (seconds in this stage), seconds (of audio), language (its name), speech
    ("Whisper Small" / "Gusan Cloud"), speech_local (bool), writer
    ("Gusan Cloud" / "Qwen 1.5B"), writer_local (bool), where, local."""
    ctx = ctx if isinstance(ctx, dict) else {}
    elapsed = _num(ctx.get("elapsed"))
    if stage == WRITING:
        return _writing(kind, detail, ctx, elapsed)
    return _transcribing(ctx, elapsed)


# ── done ─────────────────────────────────────────────────────────────────────
_PASTED = {
    EMAIL: "Email pasted", LIST: "List pasted", TRANSLATE: "Translated · pasted",
    SUMMARY: "Summary pasted", POLISH: "Polished · pasted", NOTES: "Notes pasted",
    REPLY: "Reply pasted", MESSAGE: "Message pasted",
}
_COPIED = {
    EMAIL: "Email copied", LIST: "List copied", TRANSLATE: "Translation copied",
    SUMMARY: "Summary copied", POLISH: "Polished text copied", NOTES: "Notes copied",
    REPLY: "Reply copied", MESSAGE: "Message copied",
}


def paste_keys(mac=None):
    """The paste shortcut: Cmd+V on a Mac, Ctrl+V elsewhere."""
    if mac is None:
        mac = sys.platform == "darwin"
    return "Cmd+V" if mac else "Ctrl+V"


def done(kind, pasted, smart=None, mac=None):
    """The "done" line. ``kind``: what a Smart Action wrote (None for a plain
    dictation, or a Smart Action with no clear ask - pass smart=True then).
    Not pasted (nothing took the paste): it's on the clipboard."""
    kind = kind if kind in KINDS else None
    if smart is None:
        smart = kind is not None
    if pasted:
        if kind:
            return _PASTED[kind]
        return "Done · pasted" if smart else "Pasted"
    return f"{_COPIED.get(kind, 'Copied')} · press {paste_keys(mac)}"


# ── context from main.py ─────────────────────────────────────────────────────
_warming = False


def _loaded(name):
    """``name``'s module if it is fully imported already, else None - so a
    take never waits for an import (engine_choice + hardware: ~30 ms)."""
    mod = sys.modules.get(name)
    if mod is None or getattr(getattr(mod, "__spec__", None), "_initializing", False):
        return None
    return mod


def warm():
    """Import engine_choice on a worker thread, once (a take has just
    started: it lands long before the stop). Never raises, never waits."""
    global _warming
    if _warming or _loaded("engine_choice") is not None:
        return
    _warming = True

    def _import():
        try:
            import engine_choice  # noqa: F401
        except Exception:
            pass
    try:
        threading.Thread(target=_import, name="pill-warm", daemon=True).start()
    except Exception:
        _warming = False


def _whisper_name(model_id):
    """'small' -> 'Whisper Small', 'large-v3-turbo' -> 'Whisper Large v3
    Turbo': hardware.MODEL_INFO's names, while engine_choice isn't loaded."""
    if str(model_id or "").strip() == "hy-fastconformer":        # hardware.HY_MODEL_ID: the Armenian model
        return "NVIDIA FastConformer Armenian"
    words = [w if re.fullmatch(r"v\d+", w) else w.capitalize()
             for w in str(model_id or "base").strip().split("-") if w]
    return " ".join(["Whisper"] + words)


def speech_info(backend, model=None, cfg=None):
    """What transcribes this take, for the pill: {"speech": "Whisper Small" |
    "Gusan Cloud" | "Gemini 2.5 Flash" | "Voxtral Mini", "speech_local":
    bool}. ``backend`` and ``model`` are the ones main._stop_impl records for
    the take; ``cfg`` names an own-key cloud model. Never imports."""
    backend = str(backend or "local").strip().lower()
    cfg = cfg if isinstance(cfg, dict) else {}
    if backend == "managed":
        return {"speech": GUSAN_CLOUD, "speech_local": False}
    try:
        ec = _loaded("engine_choice")
        if backend == "google":
            mid = str(cfg.get("google_stt_model") or "").strip() or "gemini-2.5-flash"
            name = ec.model_display_name(mid) if ec else "Gemini"
            return {"speech": name, "speech_local": False}
        if backend == "mistral":
            name = ec.model_display_name(ec.normalize_mistral_model(cfg.get("mistral_stt_model"))) \
                if ec else "Voxtral Mini"
            return {"speech": name, "speech_local": False}
        name = ec.speech_model_name(model or "base") if ec else _whisper_name(model)
        return {"speech": name, "speech_local": True}
    except Exception:
        return {"speech_local": backend not in ("google", "mistral")}


def writer_info(runs):
    """An engine_choice.runs_on(app, "smart_actions") dict -> the pill's
    writer context: {"writer": "Gusan Cloud" | "Qwen 1.5B" | "Ministral 14B"
    | "", "writer_local": bool}; {} when it isn't known."""
    if not isinstance(runs, dict):
        return {}
    kind = runs.get("kind")
    if kind == "cloud":
        return {"writer": GUSAN_CLOUD, "writer_local": False}
    if kind == "local":
        return {"writer": str(runs.get("model_name") or ""), "writer_local": True}
    if kind == "own_key":
        return {"writer": str(runs.get("model_name") or runs.get("plain") or ""), "writer_local": False}
    if kind == "basic":
        return {"writer": "", "writer_local": True}       # built-in rules: instant
    return {}


def task(text, app):
    """(kind, detail, info) for a Smart Action about to write ``text``:
    overlay.set_task's arguments. Cheap (dict lookups, regexes, one file
    stat for a local writer), never imports (no writer named if
    engine_choice isn't loaded yet - see warm()) and never raises."""
    runs = {}
    try:
        ec = _loaded("engine_choice")
        runs = ec.runs_on(app, "smart_actions") if ec else {}
    except Exception:
        runs = {}
    runs = runs if isinstance(runs, dict) else {}
    kind, detail = intent(text, rules=runs.get("kind") == "basic")
    info = writer_info(runs)
    info = dict(info) if isinstance(info, dict) else {}
    # The pill's row of steps: every kind asked for - the built-in rules write one thing, so only theirs.
    info["kinds"] = ([kind] if kind else []) if runs.get("kind") == "basic" else kinds(text, kind)
    return kind, detail, info
