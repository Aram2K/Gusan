"""People from the screen: the names of the people on a video call, read off the Live Assistant's own screenshot.

With Screen on, every Live Assistant answer carries a screenshot of the monitor the user works on. A call's tiles and
participant list are on that screen, so once in a while - and without any new question - Gusan asks the SAME engine
that answers (Gusan Cloud inside the session that is already open, or the user's own key) for the names it can read
there. They join the meeting's People, so the notes, the live answers, "Ask about this meeting" and the speaker pass
spell and use them. Qt-free and network-free at import (a leaf module, so the rules test without a display).

What is here:

* the reading itself - :func:`read_reply` (one screenshot -> the model's raw reply, through the same engine route as an
  answer: actions.process_stream, mode "live_people", sent to the server as "live_assist");
* the screen text is UNTRUSTED (anyone on the call chooses their display name): :func:`parse_reply` is defensive about
  what a model sends back, and :func:`clean_name` / :func:`clean_people` keep only what can be a person's name
  (letters, spaces and . ' - only; no digits, markup or brackets; at most MAX_NAME_CHARS and MAX_WORDS; not a button, a
  phone number, an email or something that reads like an order to the model). Names are never logged, only counts;
* :func:`merge` / :func:`lone_other` - how the names join the people a meeting already has;
* :class:`ScreenReader` - WHEN to look: ~30 s after the session starts listening, then every ~5 minutes but only when the
  screen changed, at most 12 looks, never in Privacy Mode, only with Screen on and the card on screen, only with an
  engine that reads images, a newer answer request always wins, a failure is silent;
* a small command line: ``python -m people_from_screen shot.png`` runs the sanitizer and the real configured engine on a
  picture, so a person can check what the reading makes of a real call.
"""
import base64
import json
import logging
import re
import threading
import time
import unicodedata

import invite_context

logger = logging.getLogger("transcribe")

MODE = "live_people"                  # actions.ACTION_LIVE_PEOPLE (kept literal: this module imports no engine code)

# ── what is allowed to be a name ─────────────────────────────────────────────
MAX_PEOPLE = 12                       # names read off the screen, per meeting and per look
MAX_NAME_CHARS = 40
MAX_WORDS = 5
MAX_WORD_CHARS = 24
_PUNCT_OK = frozenset(" .'’-‐‑")      # (a letter, a combining mark, a space and these: nothing else)
_JOINERS = frozenset("‌‍")  # Persian / Indic names need them; allowed between two letters only
_BRACKETS = frozenset("()[]{}<>（）［］｛｝【】《》〈〉«»")
_BRACKET_GROUP = re.compile(r"\s*[(\[{（［｛][^()\[\]{}（）［］｛｝]{0,24}[)\]}）］｝]")
_SEPARATOR = re.compile(r"\s+[-–—|•·/]\s+")

# What a screen shows around a name or instead of one. Whole-name matches (or every word of the name being one of these).
_UI_WORDS = frozenset("""
you me myself i my self your yours user you're
host co-host cohost co host guest guests participant participants attendee attendees presenter presenters organizer
organiser speaker speakers moderator owner admin administrator unknown unnamed anonymous caller phone dial dial-in
mute muted unmute unmuted share sharing shared stop start present presenting presentation screen window desktop
chat chats message messages meeting call calls conference room lobby waiting waiting-room admit remove kick leave end
join joined joining invite invited copy link pin pinned unpin spotlight spotlighted layout view views gallery grid
fullscreen full minimize maximize close open more less settings setting options option menu apps app whiteboard
captions caption subtitles subtitle transcript transcription live record recording recorded reactions reaction react
raise lower hand hands camera video videos audio microphone mic speaker-view sound volume network connection
connecting reconnecting
poor good details info information people everyone everybody all others other none nobody noone no one name your-name
zoom teams microsoft google meet webex skype slack huddle discord whatsapp facetime gusan assistant
yes no ok cancel done next back
я вы ты мы хост гость участник участники
ich du sie wir gast teilnehmer
moi vous toi nous invité participant participants
yo tú usted invitado
""".split())

# What a screen writes beside the user's OWN name ("Aram (You)", "Aram - Me"): that tile is not another person.
_SELF_WORDS = frozenset(("you", "me", "myself", "я", "вы", "moi", "ich", "yo"))

# Words that make a "name" read like an order to a model. A person is hardly ever called this, and a label that is is
# harmless to lose (the user can add the person on the meeting page, or the invite has them).
_INJECTION_WORDS = frozenset("""
ignore ignoring disregard disregarding forget override overrides instruction instructions prompt prompts system
assistant previous prior above jailbreak pretend respond reply output chatgpt openai anthropic llm
""".split())

# Words a name may begin with in lower case (van der Berg, de la Cruz, bin Salman, ter Stegen ...).
_PARTICLES = frozenset("""
van von der den de del della di da dos das du la le el al bin ibn ben bat abu af av ter ten zu y e i
""".split())

# Hard limits for a bad model reply: no more than this many items / characters are looked at.
_MAX_REPLY_CHARS = 20000
_MAX_REPLY_ITEMS = 60


def _fold(text):
    """``text`` without accents and case: "Ánna" and "anna" are the same person."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def first_key(name):
    """The key two spellings of one person share: the first word of the name, any case, without accents or a
    trailing dot ("Anna Petrosyan", "anna" and "Ánna K." are all "anna")."""
    words = _fold(name).split()
    return words[0].strip(".'’-‐‑") if words else ""


def _my_keys(me):
    full = " ".join(_fold(me).split())
    return full, (full.split()[0].strip(".'’-‐‑") if full else "")


def is_me(name, me):
    """Is ``name`` the user themselves? ``me``: their full name or their first name ("" = unknown: nobody is)."""
    full, first = _my_keys(me)
    if not full:
        return False
    return " ".join(_fold(name).split()) == full or first_key(name) == first


# ── one name ─────────────────────────────────────────────────────────────────
def clean_name(raw):
    """One person's name as read from a screen, or "" when it cannot be one. Strict on purpose: the text came off a
    screen, where anyone on the call chooses their own display name."""
    if not isinstance(raw, str) or not raw or len(raw) > 4 * MAX_NAME_CHARS:
        return ""
    for ch in raw:                                    # a line break or a control character is no name
        if unicodedata.category(ch) in ("Cc", "Cs", "Zl", "Zp"):
            return ""
    text = "".join(ch for ch in raw if ord(ch) not in invite_context._DROPPED)    # bidi marks, zero-width space, BOM
    text = unicodedata.normalize("NFC", text)
    text = " ".join(text.replace(" ", " ").split())
    aside = [m.group(0) for m in _BRACKET_GROUP.finditer(text)]
    text = _BRACKET_GROUP.sub("", text).strip()        # "Anna (Host)" -> "Anna"
    parts = _SEPARATOR.split(text, 1)
    text = parts[0].strip()                            # "Anna Petrosyan - Host" -> "Anna Petrosyan"
    aside += parts[1:]
    if any(w in _SELF_WORDS for piece in aside for w in re.findall(r"\w+", _fold(piece))):
        return ""                                      # "Aram (You)", "Aram - Me": the user's own tile
    text = text.strip(" ,;:\"“”‘").strip()
    if not text or len(text) > MAX_NAME_CHARS:
        return ""
    if any(ch in _BRACKETS for ch in text):
        return ""
    prev = ""
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in _JOINERS:
            if not prev.isalpha():
                return ""
        elif not (cat[0] in ("L", "M") or ch in _PUNCT_OK):
            return ""                                  # a digit, a symbol, "@", "#", "<", an emoji ...
        prev = ch
    if not text[0].isalpha() or text[-1] in "-‐‑'’":
        return ""
    if sum(1 for ch in text if ch.isalpha()) < 2:
        return ""
    words = text.split(" ")
    if len(words) > MAX_WORDS or any(len(w) > MAX_WORD_CHARS for w in words):
        return ""
    folded = [_fold(w).strip(".'’-‐‑") for w in words]
    if " ".join(folded) in _UI_WORDS or all(w in _UI_WORDS for w in folded):
        return ""
    if any(w in _INJECTION_WORDS for w in folded):
        return ""
    loose = sum(1 for w in words if w[0].islower() and _fold(w).strip(".") not in _PARTICLES)
    if len(words) >= 3 and loose >= 2:                 # a sentence, not a name
        return ""
    return text


def clean_people(raw_names, me="", limit=MAX_PEOPLE):
    """The clean names of ``raw_names`` (strings), in order: no repeat (any case, any accent, and one first name is one
    person), nobody who is the user (``me``), at most ``limit``."""
    out, seen = [], set()
    for raw in list(raw_names or [])[:_MAX_REPLY_ITEMS]:
        name = clean_name(raw)
        if not name:
            continue
        key = first_key(name)
        if not key or key in seen or is_me(name, me):
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= limit:
            break
    return out


# ── what a model sends back ──────────────────────────────────────────────────
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\n?(.*?)```", re.S)
_BULLET = re.compile(r"^\s*(?:[-*•–·]|\d{1,2}[.)])\s+(.+?)\s*$")
_LABEL = re.compile(r"^\s*(?:people|names|participants|attendees)\s*[:：]\s*(.*)$", re.I)


def _names_of(value):
    """The strings in the value of a "people" key: a list of names (or of {"name": ...}), or one comma string."""
    if isinstance(value, str):
        return [p for p in re.split(r"[,;\n]", value)]
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:_MAX_REPLY_ITEMS]:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for key in ("name", "full_name", "display_name"):
                if isinstance(item.get(key), str):
                    out.append(item[key])
                    break
    return out


def _from_object(obj):
    """The raw names of one decoded JSON value, or None when it does not look like the answer asked for."""
    if isinstance(obj, dict):
        for key in obj:
            if isinstance(key, str) and key.strip().lower() in ("people", "names", "participants"):
                return _names_of(obj[key])
        return None
    if isinstance(obj, list):
        return _names_of(obj)
    return None


def _json_names(text):
    text = text.strip()
    try:
        found = _from_object(json.loads(text))
        if found is not None:
            return found
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    tries = 0
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        tries += 1
        if tries > 40:
            break
        try:
            obj, _end = decoder.raw_decode(text, i)
        except ValueError:
            continue
        found = _from_object(obj)
        if found is not None:
            return found
    return None


def _list_names(text):
    """A model that ignored the JSON format may have written a bullet list, or a line "People: Anna, Bob". Nothing
    else counts: a sentence is never read for names (the app must not guess)."""
    out = []
    for line in text.splitlines()[:_MAX_REPLY_ITEMS]:
        label = _LABEL.match(line)
        if label:
            out.extend(p for p in re.split(r"[,;]", label.group(1)))
            continue
        bullet = _BULLET.match(line)
        if bullet:
            out.append(bullet.group(1))
    return out


def parse_reply(text):
    """The raw names in a model's reply, not yet checked (``clean_people`` does that): the JSON asked for, also inside
    a code fence or after some words; failing that a bullet list or a "People: ..." line; else []. Never raises."""
    try:
        text = str(text or "").strip()[:_MAX_REPLY_CHARS]
        if not text:
            return []
        for block in _FENCE.findall(text) + [text]:
            found = _json_names(block)
            if found is not None:
                return found
        return _list_names(text)
    except Exception:
        return []


# ── joining the people of a meeting ──────────────────────────────────────────
def merge(current, found, me="", removed=(), limit=MAX_PEOPLE):
    """``(people, added)``: the people a meeting already has (``current``: a list, or the comma string it keeps) with the
    clean names of ``found`` after them. The meeting's own names come first and keep their spelling; nobody is there
    twice (the same first name is the same person); never the user (``me``); never a name in ``removed`` (the
    user took it off); at most ``limit`` new ones. ``added`` is the names that were new."""
    people = invite_context.cap_names(current)
    keys = {first_key(n) for n in people}
    gone = {first_key(n) for n in (removed or ())}
    added = []
    for name in clean_people(found, me=me):
        key = first_key(name)
        if key in keys or key in gone:
            continue
        if len(added) >= max(0, int(limit)):
            break
        keys.add(key)
        added.append(name)
    return people + added, added


def lone_other(people, me=""):
    """The one other person on the call, when the meeting has exactly one name besides the user's own; else "". The
    speaker pass calls the one voice it finds on the call by it."""
    others = [n for n in invite_context.cap_names(people) if not is_me(n, me)]
    return others[0] if len(others) == 1 else ""


def my_name(app):
    """The signed-in user's name as the app knows it, "" for a guest: the profile name, else the part of the email before
    the "@" (what the People card shows as "You")."""
    auth = getattr(app, "auth", None) if app is not None else None
    if not getattr(auth, "is_authenticated", False):
        return ""
    name = str(getattr(auth, "user_name", "") or "").strip()
    if not name:
        email = str(getattr(auth, "user_email", "") or "").strip()
        name = email.split("@")[0] if email else ""
    return " ".join(name.split())[:80]


# ── asking the engine ────────────────────────────────────────────────────────
USER_PROMPT = "List the people on the video call shown in this screenshot. Reply with JSON only."


class NoVision(Exception):
    """The engine that answers cannot read an image (a model on this computer, a text-only model), or nothing may leave
    this computer (Privacy Mode). The reader then stops for the session, silently."""


class Cancelled(BaseException):
    """A newer answer request (or the end of the session) took over: the look is dropped. Not an Exception: it must pass
    through every ``except Exception`` between the stream and here."""


def can_read_images(engine, cfg, actions_mod=None):
    """Does the engine that answers read images? Only the cloud routes do - Gusan Cloud and the user's own key; a model
    on this computer never does, and Privacy Mode keeps everything here. Pure: no network."""
    if actions_mod is None:
        import actions as actions_mod
    cfg = cfg or {}
    if cfg.get("privacy_mode") or cfg.get("live_assist_writer") == "local" or not engine:
        return False
    return actions_mod.ACTION_MODELS.get(engine, {}).get("kind") in ("cloud", "managed")


def read_reply(image_b64, engine, cfg, cancelled=None, actions_mod=None):
    """One screenshot (base64 PNG or JPEG) -> the engine's raw reply. The same route an answer takes (``engine`` and
    ``cfg`` are what the Live Assistant resolved: Gusan Cloud inside the open session - it sends the session id, so the
    free allowance is not spent again - or the user's own key); the reply is JSON for ``parse_reply``.

    Blocking: a worker thread only. ``cancelled()`` is asked for every piece that arrives; when it says True the
    stream is closed and :class:`Cancelled` is raised. Raises :class:`NoVision` when the engine cannot read an image
    (also when it refused this one) and the engine's own ActionError for everything else."""
    if actions_mod is None:
        import actions as actions_mod
    if not can_read_images(engine, cfg, actions_mod):
        raise NoVision()
    config = dict(cfg or {})
    status = {}
    config["_image_png_b64"] = image_b64
    config["_image_status"] = status               # set when the model turned the picture down
    parts = []

    def on_token(delta):
        if cancelled is not None and cancelled():
            raise Cancelled()
        if isinstance(delta, str) and type(delta).__name__ == "ReplaceText":
            parts[:] = [str(delta)]
        else:
            parts.append(delta)

    text = actions_mod.process_stream(USER_PROMPT, getattr(actions_mod, "ACTION_LIVE_PEOPLE", MODE), on_token,
                                      model=engine, config=config)
    if status.get("dropped"):
        raise NoVision()
    return text or "".join(parts)


# ── has the screen changed? ──────────────────────────────────────────────────
HASH_SIDE = 16                        # 16 x 16 grey pixels: 256 bits
CHANGE_BITS = 16                      # this many bits differ (6 %): the screen is another one


def _gray_pixels(image_b64, side=HASH_SIDE):
    """``side`` x ``side`` grey values (0-255) of a base64 image, or None when it cannot be read. Pillow when there is
    one, else Qt's QImage (a plain image class: fine on a worker thread)."""
    try:
        data = base64.b64decode(image_b64, validate=False)
    except Exception:
        return None
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            small = im.convert("L").resize((side, side), Image.BOX)
            return list((getattr(small, "get_flattened_data", None) or small.getdata)())
    except ImportError:
        pass
    except Exception:
        return None
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        img = QImage.fromData(data)
        if img.isNull():
            return None
        small = img.scaled(side, side, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        small = small.convertToFormat(QImage.Format_Grayscale8)
        return [small.pixelColor(x, y).red() for y in range(side) for x in range(side)]
    except Exception:
        return None


def image_hash(image_b64):
    """An average hash of the picture - 1 bit for each of 256 grey pixels: brighter than the mean or not - as an int, or
    None when the picture cannot be read. Cheap, and steady against a face moving in a tile."""
    gray = _gray_pixels(image_b64)
    if not gray:
        return None
    mean = sum(gray) / float(len(gray))
    bits = 0
    for value in gray:
        bits = (bits << 1) | (1 if value >= mean else 0)
    return bits


def hash_distance(a, b):
    """How many of the bits differ."""
    return bin(int(a) ^ int(b)).count("1")


# ── when to look ─────────────────────────────────────────────────────────────
class ScreenReader:
    """Decides WHEN the Live Assistant's screen is read for names, and runs a look off the GUI thread. No Qt, no
    engine: everything it needs is handed in, so the schedule tests without either.

    ``ask(image_b64, cancelled)``: the engine's raw reply (a worker thread; raises NoVision, Cancelled, or anything
    else for a failure). ``probe()``: does the engine that answers read images? True / False, None when unknown
    (a worker thread, once per session). ``deliver(names, session)``: the clean names a look found (a worker thread;
    the card turns it into a signal for the GUI thread). ``spawn(fn)``: runs ``fn`` on another thread (tests run it at
    once). ``clock()``: seconds. ``hasher(image_b64)``: a picture's hash (None = cannot tell).

    The rules (see docs/redesign/people-from-screen-spec.md):
    * first look FIRST_LOOK_S after the session starts listening; then one every LOOK_EVERY_S, only when the screen
      changed since the last look (a screen that did not change is looked at again after RECHECK_S);
    * at most MAX_LOOKS looks; stop after MAX_EMPTY_IN_A_ROW empty results, MAX_FAILS_IN_A_ROW failures, an engine
      that cannot read images, or when the meeting has all MAX_PEOPLE names it takes;
    * a screenshot taken for an answer in the last FRESH_SHOT_S is used instead of taking another;
    * never while an answer is being written; a newer answer request cancels a look that is running and the next one
      waits AFTER_ANSWER_S;
    * nothing without Screen on and the card on screen, never in Privacy Mode; a failure is silent."""

    FIRST_LOOK_S = 30.0
    LOOK_EVERY_S = 300.0
    RECHECK_S = 90.0
    MAX_LOOKS = 12
    MAX_EMPTY_IN_A_ROW = 3
    MAX_FAILS_IN_A_ROW = 3
    FRESH_SHOT_S = 60.0
    AFTER_ANSWER_S = 20.0

    def __init__(self, ask=None, probe=None, deliver=None, spawn=None, clock=time.monotonic, hasher=image_hash):
        self._ask = ask
        self._probe = probe
        self._deliver = deliver
        self._spawn = spawn or self._thread
        self._clock = clock
        self._hasher = hasher
        self._lock = threading.RLock()
        self._session = 0                  # bumped by begin() and end(): a late look of an older one is dropped
        self._active = False
        self._me = ""
        self._reset()

    # state -------------------------------------------------------------
    def _reset(self):
        self._started_at = 0.0
        self._looks = 0                    # looks that went out (a cancelled one is given back)
        self._empty = 0
        self._fails = 0
        self._last_look_at = None
        self._last_hash = None
        self._next_check_at = None
        self._hold_until = 0.0
        self._shot = None                  # (image_b64, taken at) of the last answer's screenshot
        self._busy = False
        self._cancel = False
        self._vision = None
        self._probing = False
        self._stopped = ""
        self.last_looks_ms = []            # how long each look took (for the log)

    @staticmethod
    def _thread(fn):
        threading.Thread(target=fn, daemon=True, name="gusan-people-from-screen").start()

    @property
    def active(self):
        return self._active

    @property
    def looks(self):
        return self._looks

    @property
    def stopped(self):
        """Why it stopped looking for this session ("" = it has not): looks | empty | fails | vision | full."""
        return self._stopped

    @property
    def busy(self):
        return self._busy

    @property
    def session(self):
        return self._session

    # the session's life ------------------------------------------------
    def begin(self, me="", now=None):
        """A Live Assistant session started listening: the first look is FIRST_LOOK_S away."""
        with self._lock:
            self._session += 1
            self._reset()
            self._me = str(me or "")
            self._active = True
            self._started_at = self._clock() if now is None else now

    def end(self):
        """The session ended: a look still running is dropped when it comes back."""
        with self._lock:
            self._session += 1
            self._active = False
            self._cancel = True
            self._shot = None

    def set_me(self, me):
        with self._lock:
            self._me = str(me or "")

    def note_shot(self, image_b64, now=None):
        """The card took a screenshot of the screen for an answer: a look in the next FRESH_SHOT_S uses it."""
        if not image_b64:
            return
        with self._lock:
            self._shot = (image_b64, self._clock() if now is None else now)

    def answer_started(self, now=None):
        """A newer answer request: it wins. A look in flight is cancelled (and given back); the next one waits."""
        with self._lock:
            now = self._clock() if now is None else now
            if self._busy:
                self._cancel = True
                self._hold_until = max(self._hold_until, now + self.AFTER_ANSWER_S)

    def note_full(self):
        """The meeting has every name it takes: no more looks."""
        with self._lock:
            if self._active and not self._stopped:
                self._stopped = "full"

    # the schedule ------------------------------------------------------
    def tick(self, screen_on, privacy, visible, answering, capture, now=None):
        """About once a second, on the GUI thread. Starts a look when one is due and returns what it did (a word for the
        tests): idle | stopped | off | busy | wait | probing | no_shot | looking. ``capture()``: a screenshot as base64
        (taken here, on the GUI thread, only when no fresh one from an answer exists)."""
        shot = None
        with self._lock:
            now = self._clock() if now is None else now
            if not self._active:
                return "idle"
            if self._stopped:
                return "stopped"
            if privacy or not (screen_on and visible):
                return "off"
            if answering or self._busy:
                return "busy"
            if self._looks >= self.MAX_LOOKS:
                self._stopped = "looks"
                return "stopped"
            if now < self._hold_until:
                return "wait"
            due = (self._started_at + self.FIRST_LOOK_S if self._last_look_at is None
                   else self._last_look_at + self.LOOK_EVERY_S)
            if self._next_check_at is not None:
                due = max(due, self._next_check_at)
            if now < due:
                return "wait"
            if self._vision is None:
                if self._probe is not None and not self._probing:
                    self._probing = True
                    token = self._session
                    self._spawn(lambda: self._run_probe(token))
                return "probing"
            if self._vision is False:
                self._stopped = "vision"
                return "stopped"
            if self._shot is not None and now - self._shot[1] <= self.FRESH_SHOT_S:
                shot = self._shot[0]
            token = self._session
        if shot is None:
            try:
                shot = capture() if capture is not None else ""
            except Exception:
                shot = ""
            if not shot:
                with self._lock:
                    self._next_check_at = now + self.RECHECK_S
                return "no_shot"
        with self._lock:
            if token != self._session or not self._active:
                return "idle"
            self._busy = True
            self._cancel = False
            self._shot = None              # used: the next look takes a new one
        self._spawn(lambda: self._run_look(shot, token))
        return "looking"

    def _run_probe(self, token):
        verdict = None
        try:
            verdict = self._probe()
        except Exception:
            logger.debug("People from the screen: engine check failed", exc_info=True)
        with self._lock:
            if token != self._session:
                return
            self._probing = False
            if verdict is None:
                self._fails += 1
                if self._fails >= self.MAX_FAILS_IN_A_ROW:
                    self._stopped = "fails"
            else:
                self._vision = bool(verdict)

    def _cancelled(self, token):
        with self._lock:
            return self._cancel or token != self._session or not self._active

    def _run_look(self, shot, token):
        started = time.monotonic()
        names, outcome = [], "ok"
        prev_hash, prev_at = None, None
        counted = False
        try:
            digest = None
            try:
                digest = self._hasher(shot) if self._hasher is not None else None
            except Exception:
                digest = None
            with self._lock:
                if self._cancel or token != self._session or not self._active:
                    outcome = "cancelled"
                elif (self._last_look_at is not None and digest is not None and self._last_hash is not None
                      and hash_distance(digest, self._last_hash) < CHANGE_BITS):
                    outcome = "same"       # the screen is the one already read: look again a little later
                else:
                    prev_hash, prev_at = self._last_hash, self._last_look_at
                    self._looks += 1
                    counted = True
                    self._last_look_at = self._clock()
                    self._last_hash = digest
            if outcome == "ok":
                raw = self._ask(shot, lambda: self._cancelled(token))
                names = clean_people(parse_reply(raw), me=self._me)
        except NoVision:
            outcome = "no_vision"
        except Cancelled:
            outcome = "cancelled"
        except Exception as e:             # silent for the user; the log holds the kind of failure, never a name
            outcome = "failed"
            logger.debug("People from the screen: a look failed (%s %s)", type(e).__name__,
                         getattr(e, "code", "") or "")
        ms = int((time.monotonic() - started) * 1000)
        deliver = None
        with self._lock:
            stale = token != self._session or not self._active
            self._busy = False
            if outcome == "same":
                self._next_check_at = self._clock() + self.RECHECK_S
            elif outcome == "cancelled":
                if counted:                # given back, and the screen it would have read is still to be read
                    self._looks -= 1
                    self._last_hash, self._last_look_at = prev_hash, prev_at
                self._hold_until = max(self._hold_until, self._clock() + self.AFTER_ANSWER_S)
            elif outcome == "no_vision":
                if counted:
                    self._looks -= 1
                self._vision = False
                self._stopped = "vision"
            elif outcome == "failed":
                self._fails += 1
                if self._fails >= self.MAX_FAILS_IN_A_ROW:
                    self._stopped = "fails"
            elif outcome == "ok":
                self._fails = 0
                self._empty = 0 if names else self._empty + 1
                self._next_check_at = None
                self.last_looks_ms.append(ms)
                if self._empty >= self.MAX_EMPTY_IN_A_ROW:
                    self._stopped = self._stopped or "empty"
                if self._looks >= self.MAX_LOOKS:
                    self._stopped = self._stopped or "looks"
                if not stale and self._deliver is not None:
                    deliver = self._deliver
        if outcome in ("ok", "failed"):
            logger.info("People from the screen: look %d %s in %d ms, %d names", self._looks, outcome, ms, len(names))
        if deliver is not None:
            try:
                deliver(names, token)
            except Exception:
                logger.debug("People from the screen: the names were not taken", exc_info=True)


# ── the command line ─────────────────────────────────────────────────────────
def _cli_config(env=None):
    """The saved settings and keys of this computer's Gusan (read-only), plus a Gusan Cloud token from the environment
    variable GUSAN_ACCESS_TOKEN when there is one."""
    import os
    import storage
    env = os.environ if env is None else env
    cfg = storage.read_json(storage.path_for("config.json"), {})
    cfg = dict(cfg) if isinstance(cfg, dict) else {}
    for key, secret in (("google_api_key", storage.GOOGLE_API_KEY_SECRET),
                        ("action_api_key", storage.ACTION_API_KEY_SECRET),
                        ("mistral_api_key", storage.MISTRAL_API_KEY_SECRET)):
        if not str(cfg.get(key) or "").strip():
            try:
                cfg[key] = storage.read_secret(secret) or ""
            except Exception:
                cfg[key] = ""
    token = str(env.get("GUSAN_ACCESS_TOKEN") or "").strip()
    if token:
        cfg["_managed_token"] = token
    return cfg


def _cli_engine(cfg, forced=""):
    import actions
    chosen = actions.normalize_action_model(forced or cfg.get("action_model"))
    return actions.live_engine(chosen, cfg)


def main(argv=None, cfg=None, reader=None, out=None):
    """``python -m people_from_screen shot.png [--me NAME] [--engine ID] [--reply FILE] [--raw]``: what the reading makes
    of one screenshot. Runs the real engine this computer's Gusan would answer with (own key, or Gusan Cloud with
    GUSAN_ACCESS_TOKEN set), or - with ``--reply FILE`` - no engine at all: the file's text is taken as the model's
    reply, to try the sanitizer on its own. Prints the clean names, one per line; the exit status is 0 (names or none) or
    2 (nothing could be asked). ``cfg`` / ``reader`` / ``out``: for tests (the engine call, the printing)."""
    import argparse
    import sys
    out = out or sys.stdout
    ap = argparse.ArgumentParser(
        prog="python -m people_from_screen",
        description="Read the names of the people on a video call from a screenshot, the way Gusan does.")
    ap.add_argument("image", help="a PNG or JPEG screenshot of a call")
    ap.add_argument("--me", default="", help="your own name: it is left out of the list")
    ap.add_argument("--engine", default="", help="an engine id (api_gemini, api_mistral, ...) instead of the saved one")
    ap.add_argument("--reply", default="", help="a text file with a model's reply: skips the engine")
    ap.add_argument("--raw", action="store_true", help="also print what the model replied")
    args = ap.parse_args(argv)

    if args.reply:
        with open(args.reply, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    else:
        with open(args.image, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("ascii")
        try:
            config = cfg if cfg is not None else _cli_config()
            if reader is not None:
                raw = reader(image_b64, config)
            else:
                engine = _cli_engine(config, args.engine)
                if not can_read_images(engine, config):
                    out.write("This engine cannot read images: pick Gusan Cloud (set GUSAN_ACCESS_TOKEN) or a cloud "
                              "engine with your own key.\n")
                    return 2
                raw = read_reply(image_b64, engine, config)
        except NoVision:
            out.write("The engine turned the picture down: it cannot read images.\n")
            return 2
        except Exception as e:
            out.write("The engine could not be asked: %s\n" % str(e)[:200])
            return 2
    if args.raw:
        out.write("reply: %s\n" % raw)
    names = clean_people(parse_reply(raw), me=args.me)
    for name in names:
        out.write(name + "\n")
    if not names:
        out.write("(no people read)\n")
    return 0


if __name__ == "__main__":                # pragma: no cover
    import sys
    sys.exit(main())
