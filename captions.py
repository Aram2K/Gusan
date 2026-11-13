"""Live captions (founder, 2026-11-11): the words of a meeting at the bottom middle of the screen as they are said -
as spoken or translated, without filler words, without a background, each side of the call in its own colour.

What he asked for, and the decisions:
  * captions follow Transcribes with ("if I have chosen best accuracy I don't care much about the speed; if I choose
    Fast I want the real time"): on Fast they take the meeting's live stream (Gladia, or OpenAI's live model for
    Armenian) word by word as it is being said; on Best accuracy, a model on this computer or your own key they take
    that engine's finished pieces - later, the accurate words. A piece (up to ~25 s of talk) is let out at reading
    pace (PACE_WPS words a second, faster when behind: everything shown within CATCH_UP_S), so two lines never skip
    words.
  * translated or not (cfg captions_language: "" = as spoken): a translation needs the whole sentence, so translated
    captions come a sentence at a time, about a second after it ends (the server's "interpret" mode, the Interpreter's).
  * "both with different colours and without overlapping": two sides that never mix - "them" (the call; everyone in a
    meeting with one source) and "me" (your microphone: the live stream's "You"), each in its own lines, and a switch
    for whose words (cfg captions_who: "both" | "them" | "me") that also works in the middle of a meeting.
  * filler words removed (text_cleanup's list, Dictation's "Remove filler words") from captions as spoken.
  * best practice (BBC / DCMP / university caption guides): at most two lines a side, about 32-42 characters a line,
    roll-up as new words come (live captions), a minimum time on screen, then they fade.
  * readable (founder 2026-11-13: "people cannot manage to read it ... the first sentence of every phrase is coming
    like a burst; keep a bit longer; a bigger size, also in the middle of a meeting"): EVERY word - the live stream's,
    a finished piece's, a translation's - comes out at reading pace (READ_WPS, a little faster only to catch up, never
    more than MAX_WPS), one after another in the order said; the start of a phrase never lands all at once. A new
    sentence starts on a new line (when the line before is half full), the captions stay HOLD_S after their last word,
    and the size is a setting (SIZES, cfg captions_size) the live card can change too. Then: "keep 3 sentences on the
    screen and a bit more space, to fill a bigger part of the screen" - three lines a side, about MAX_CHARS 60 a line
    (wider than the guides' 32-42, his call).

Qt-free: the overlay (ui/captions_overlay.py) draws; this keeps the words. A "line" is (id, [words]): the id stays the
same while the line is on screen, so the overlay can fade in only the new words and slide lines up smoothly.
"""
import collections
import logging
import re
import threading
import time

logger = logging.getLogger("transcribe")

# Settings (main.DEFAULT) - Meetings card › Live captions, and the live card's Captions switch.
CFG_ON = "captions_on"
CFG_LANGUAGE = "captions_language"        # "" = as spoken, else a language code to translate into
CFG_WHO = "captions_who"                  # whose words: "both" | "them" | "me"
CFG_IN_SHARE = "captions_in_share"        # False = hidden from screen sharing and recordings (like the Live Assistant)
CFG_FONT = "captions_font"                # FONTS
CFG_COLORS = "captions_colors"            # COLORS
CFG_SIZE = "captions_size"                # SIZES
WHO_BOTH, WHO_THEM, WHO_ME = "both", "them", "me"
WHO = (WHO_BOTH, WHO_THEM, WHO_ME)
SIDES = ("them", "me")
WHO_NAMES = ((WHO_BOTH, "Everyone"), (WHO_THEM, "Only the call"), (WHO_ME, "Only me"))
# The look (founder 2026-11-12: "choose the colour and the font ... not a lot of combinations, with the visual
# representation"): a few fonts and a few colour pairs (the call, you), each a tile showing it.
FONTS = (("clean", "Clean"), ("bold", "Bold"), ("classic", "Classic"), ("narrow", "Narrow"))
# family names tried in order (the first one on this computer), weight (CSS-like)
FONT_FAMILIES = {
    "clean": ((), 600),                                            # the app's own UI font
    "bold": (("Arial Black", "Segoe UI Black", "Helvetica Neue"), 900),
    "classic": (("Georgia", "Times New Roman", "Times"), 700),
    "narrow": (("Bahnschrift", "Arial Narrow", "Avenir Next Condensed", "Helvetica Neue"), 600),
}
COLORS = (("yellow", "White and yellow", (255, 255, 255), (255, 226, 132)),
          ("blue", "White and blue", (255, 255, 255), (140, 205, 255)),
          ("green", "White and green", (255, 255, 255), (150, 236, 172)),
          ("gold", "Yellow and white", (255, 214, 96), (255, 255, 255)))
# The size: the screen-based size times this (founder 2026-11-13: "the possibility to have it in a bigger size").
SIZES = (("s", "Small", 0.8), ("m", "Medium", 1.0), ("l", "Large", 1.3), ("xl", "Extra large", 1.65))
DEFAULT_SIZE = "m"


def size_scale(key):
    """The font scale of a SIZES key (Medium for an unknown one)."""
    for k, _name, scale in SIZES:
        if k == key:
            return scale
    return 1.0


# Captions in: as spoken, or translated into one of these (the server's interpret mode names them all).
LANGUAGES = (("", "As spoken"), ("en", "English"), ("hy", "Armenian"), ("fr", "French"), ("de", "German"),
             ("es", "Spanish"), ("it", "Italian"), ("pt", "Portuguese"), ("ru", "Russian"), ("uk", "Ukrainian"),
             ("ar", "Arabic"), ("tr", "Turkish"), ("nl", "Dutch"), ("pl", "Polish"), ("zh", "Chinese"),
             ("ja", "Japanese"), ("ko", "Korean"), ("hi", "Hindi"), ("fa", "Persian"))

MAX_CHARS = 60          # a line, about (the guides say 32-42; founder 2026-11-13: fill more of the screen)
MAX_LINES = 3           # a side (the guides: two; founder 2026-11-13: "keep 3 sentences on the screen")
HOLD_S = 7.0            # a side's captions stay this long after its last word, then fade (was 4.5: too short)
KEEP_LINES = MAX_LINES + 2
TRANSLATE_CONTEXT = 3   # earlier sentences of the same side, sent as context only
TRANSLATE_BACKLOG = 3   # a translation queue longer than this drops its oldest sentences (captions keep up with the call)
READ_WPS = 3.0          # words a second every caption is written at (180 a minute: the guides' comfortable reading)
MAX_WPS = 4.5           # ... a little faster only to catch up, never faster than this
CATCH_UP_S = 8.0        # ... the pace rises so what waits would be out in about this long (up to MAX_WPS)
PIECE_CATCH_UP_S = 20.0  # ... a finished piece (Best accuracy: ~25 s of talk at once) has until the next one comes
STALL_S = 0.35          # after a busy moment the words due come one after another again, not in a heap
NEW_LINE_AT = MAX_CHARS // 2   # a new sentence starts on a new line when the line before is at least this full
GAP_MARK = "[...]"
_SENTENCE_END = re.compile(r"[.!?\u2026\u3002\uff01\uff1f\u0589\u055e]['\")\]\u00bb\u201d]*$")


def side_of(who):
    """The side of a live line's speaker label: your microphone ("You") is "me"; the call, "Them", "Speaker 2" and an
    unknown speaker are "them"."""
    return "me" if str(who or "").strip().lower() == "you" else "them"


def shows(who_setting, side):
    """Does the whose-words switch show this side?"""
    who = who_setting if who_setting in WHO else WHO_BOTH
    return who == WHO_BOTH or who == side


_SPACES = re.compile(r"\s+")


def clean(text, fillers=True):
    """The words of a caption: no gap mark, no filler words (when asked), single spaces. Never raises."""
    text = str(text or "").replace(GAP_MARK, " ")
    if fillers:
        try:
            import text_cleanup
            text = text_cleanup._remove_fillers(text)
        except Exception:
            logger.debug("filler removal failed", exc_info=True)
    text = _SPACES.sub(" ", text).strip()
    text = re.sub(r"\s+([,.!?;:…])", r"\1", text)
    text = re.sub(r",+(?=[,.!?;:…])", "", text)          # "it, you know." -> "it."
    return text.lstrip(",;: ").strip()


def words_of(text, fillers=True):
    return clean(text, fillers).split()


def ends_sentence(word):
    """Does this word end a sentence ("now.", "you?", "Armenian full stop ։")?"""
    return bool(_SENTENCE_END.search(str(word or "")))


def place(lines, words, next_id, max_chars=MAX_CHARS):
    """``words`` added after ``lines`` ([(id, [words])]) the way a caption fills: on the last line while it fits, else
    on a new line (a word longer than a line gets one of its own). A new sentence starts on a new line when the line
    before is at least half full (easier to read; a short "Yes." keeps the next words beside it). Returns (lines,
    next_id) - new lists, the given ones are not changed."""
    out = [(i, list(ws)) for i, ws in lines]
    for w in words:
        if out:
            last = out[-1][1]
            if not last:
                last.append(w)
                continue
            text = " ".join(last)
            new_sentence = ends_sentence(last[-1]) and len(text) >= NEW_LINE_AT
            if not new_sentence and len(text) + 1 + len(w) <= max_chars:
                last.append(w)
                continue
        out.append((next_id, [w]))
        next_id += 1
    return out, next_id


class Lane:
    """One side's captions: the lines of its finished sentences, and the words of the sentence being said.

    Every word is written at reading pace (pace(): READ_WPS a second, up to MAX_WPS when more waits), in the order
    said: first the finished words waiting (``pending``), then the sentence being said (``live``, of which ``shown``
    are on screen). A word that arrives when nothing waits comes out at once - the first of a phrase - and the next
    ones follow one by one, never all together."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.lines = []          # [(id, [words])] - words on screen (finished sentences)
        self.live = []           # the sentence being said (live stream partial)
        self.shown = 0           # ... how many of its words are on screen
        self.pending = []        # finished words waiting to be written (a finished piece, a translation, a sentence's end)
        self.next_at = None      # when the next word may come out
        self.catch_up = CATCH_UP_S
        self.next_id = 1
        self.at = None           # when its words last changed

    def clear(self):
        self.lines, self.live, self.pending, self.at, self.next_at, self.shown = [], [], [], None, None, 0

    def backlog(self):
        """Words said but not on screen yet."""
        return len(self.pending) + max(0, len(self.live) - self.shown)

    @staticmethod
    def rate(backlog, catch_up=CATCH_UP_S):
        """Words a second: reading pace, faster only when more waits (so it would be out in ``catch_up`` seconds)."""
        return min(MAX_WPS, max(READ_WPS, backlog / catch_up))

    def _arrived(self, waited):
        """New words: when nothing was waiting, the first comes out at once (never sooner than the pace after the
        word before it)."""
        now = self._clock()
        if not waited:
            self.next_at = now if self.next_at is None else max(self.next_at, now)
        self.at = now

    def queue(self, words, catch_up=CATCH_UP_S):
        """Finished words to write at reading pace (a finished piece: ``catch_up`` PIECE_CATCH_UP_S; a translated
        sentence)."""
        words = list(words)
        if not words:
            return
        waited = self.backlog() > 0
        self._fresh()
        self.pending.extend(words)
        self.catch_up = catch_up
        self._arrived(waited)

    def drop_live(self):
        self.live, self.shown = [], 0

    def pace(self):
        """Write the words that are due, one after another. True when any were."""
        backlog = self.backlog()
        if not backlog:
            return False
        now = self._clock()
        if self.next_at is None:
            self.next_at = now
        self.next_at = max(self.next_at, now - STALL_S)
        written = 0
        while backlog and self.next_at <= now:
            if self.pending:
                word = self.pending.pop(0)
                self.lines, self.next_id = place(self.lines, [word], self.next_id)
                self.lines = self.lines[-KEEP_LINES:]
            else:
                self.shown += 1
            self.next_at += 1.0 / self.rate(backlog, self.catch_up)
            backlog -= 1
            written += 1
        if written:
            self.at = now
        return written > 0

    def _fresh(self):
        """After its captions faded, a side starts on a new line."""
        if self.at is not None and self._clock() - self.at > HOLD_S and not self.backlog():
            self.lines = []
            self.drop_live()

    def partial(self, words):
        """The sentence being said, so far (a revised word keeps its place; new words follow at pace)."""
        waited = self.backlog() > 0
        self._fresh()
        self.live = list(words)
        self.shown = min(self.shown, len(self.live))
        self.catch_up = CATCH_UP_S
        self._arrived(waited)

    def final(self, words):
        """The live stream finished the sentence being said: the words of it already on screen stay, the rest follow
        at pace."""
        waited = self.backlog() > 0
        self._fresh()
        words = list(words)
        keep = words[:self.shown] if not self.pending else []
        if keep:
            self.lines, self.next_id = place(self.lines, keep, self.next_id)
            self.lines = self.lines[-KEEP_LINES:]
        self.drop_live()
        self.pending.extend(words[len(keep):])
        self.catch_up = CATCH_UP_S
        self._arrived(waited)

    def view(self):
        """The lines on screen now (at most MAX_LINES), [] once the side has been quiet for HOLD_S."""
        self.pace()
        if self.at is None or (self._clock() - self.at > HOLD_S and not self.backlog()):
            return []
        lines, _ = place(self.lines, self.live[:self.shown], self.next_id)
        return [(i, ws) for i, ws in lines if ws][-MAX_LINES:]


class CaptionFeed:
    """The meeting's words in, the captions out (view()). ``settings()`` is the app's cfg (read each time, so a switch
    in the middle of a meeting applies at once). ``translate(text, source_lang, target_lang, context)`` returns the
    translation (it may raise: the sentence is then shown as spoken). ``on_change()`` is called when the view may have
    changed - from the translation thread too, so the overlay marshals it to its own thread."""

    def __init__(self, settings, translate=None, on_change=None, clock=time.monotonic, start_thread=True,
                 spoken_language=None):
        self._settings = settings
        self._translate = translate
        self._spoken = spoken_language        # () -> the meeting's set language ("" = detected)
        self._heard = {s: "" for s in SIDES}   # the language each side was last heard in (the live stream says)
        self.on_change = on_change
        self._clock = clock
        self.lanes = {s: Lane(clock) for s in SIDES}
        self._said = {s: collections.deque(maxlen=TRANSLATE_CONTEXT) for s in SIDES}
        self._lock = threading.Lock()
        self._queue = collections.deque()
        self._wake = threading.Event()
        self._closed = False
        self._thread = None
        if start_thread and translate is not None:
            self._thread = threading.Thread(target=self._worker, daemon=True, name="gusan-captions-translate")
            self._thread.start()

    # ── settings ──
    def _cfg(self):
        try:
            cfg = self._settings() or {}
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def target(self):
        """The language to translate into, "" = as spoken. Never in Privacy mode: a translation sends the words to
        Gusan Cloud."""
        cfg = self._cfg()
        if cfg.get("privacy_mode"):
            return ""
        return str(cfg.get(CFG_LANGUAGE) or "").strip()

    def who(self):
        who = self._cfg().get(CFG_WHO)
        return who if who in WHO else WHO_BOTH

    @staticmethod
    def _base(code):
        return str(code or "").split("-")[0].strip().lower()

    def _as_spoken(self, side, lang=""):
        """No translation needed: no caption language, or it is the language this side speaks - the sentence's own,
        else the one the side was last heard in, else the meeting's set language. Then captions are as spoken: the
        words being said show at once, word by word (founder 2026-11-12: French captions of French speech were slow)."""
        target = self._base(self.target())
        if not target or self._translate is None:
            return True
        heard = self._base(lang) or self._heard.get(side) or ""
        if not heard and callable(self._spoken):
            try:
                heard = self._base(self._spoken())
            except Exception:
                heard = ""
        return heard == target

    def _changed(self):
        cb = self.on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                logger.debug("captions on_change failed", exc_info=True)

    # ── words in ──
    def partial(self, text, who=""):
        """The sentence being said, so far (real time). Not shown while translating into another language: a
        translation needs the whole sentence."""
        if self._closed:
            return
        side = side_of(who)
        if not self._as_spoken(side):
            return
        with self._lock:
            self._drop_other_live(side)
            self.lanes[side].partial(words_of(text))
        self._changed()

    def _drop_other_live(self, side):
        """The sentence being said is on ONE side: the side of a partial comes from which source is loud, the side of
        the finished sentence from the speaker labels (a second voice at your microphone is "Speaker 1", the call's
        side) - its words left on the other side would show the sentence twice."""
        for other, lane in self.lanes.items():
            if other != side and lane.live:
                lane.drop_live()

    def final(self, text, who="", language="", paced=False):
        """A finished sentence: the live stream's (it replaces the words being said: those on screen stay, the rest
        follow), or a finished piece (``paced``: written after what waits). Both at reading pace."""
        if self._closed:
            return
        side = side_of(who)
        lang = self._base(language)
        if lang:
            self._heard[side] = lang
        target = self.target()
        if self._as_spoken(side, lang):
            with self._lock:
                if not paced:
                    self._drop_other_live(side)
                if paced:
                    self.lanes[side].queue(words_of(text), PIECE_CATCH_UP_S)
                else:
                    self.lanes[side].final(words_of(text))
            self._changed()
            return
        spoken = clean(text)
        if not spoken:
            return
        with self._lock:
            context = list(self._said[side])
            self._said[side].append(spoken)
            self._queue.append((side, spoken, lang or "auto", target, context))
            while len(self._queue) > TRANSLATE_BACKLOG:
                self._queue.popleft()                       # behind the call: the newest sentences matter
        self._wake.set()

    # ── translations ──
    def _worker(self):
        while not self._closed:
            self._wake.wait(0.5)
            self._wake.clear()
            while not self._closed:
                with self._lock:
                    if not self._queue:
                        break
                    side, spoken, lang, target, context = self._queue.popleft()
                self._translate_one(side, spoken, lang, target, context)

    def _translate_one(self, side, spoken, lang, target, context):
        text = ""
        try:
            text = str(self._translate(spoken, lang, target, context) or "").strip()
        except Exception as e:
            logger.info("Caption translation failed (%s): shown as spoken", getattr(e, "code", "") or type(e).__name__)
        if self._closed:
            return
        if target != self.target():
            return                                           # the language was changed meanwhile
        with self._lock:
            self.lanes[side].queue(words_of(text or spoken, fillers=False))
        self._changed()

    def drain(self):
        """Tests: translate what is queued, on this thread."""
        while True:
            with self._lock:
                if not self._queue:
                    return
                item = self._queue.popleft()
            self._translate_one(*item)

    # ── captions out ──
    def view(self):
        """{"them": [(id, [words])], "me": [...]} - only the sides the switch shows."""
        who = self.who()
        with self._lock:
            return {s: (self.lanes[s].view() if shows(who, s) else []) for s in SIDES}

    def busy(self):
        """Is anything on screen, waiting to come out or being translated? (The overlay looks again only then.)"""
        with self._lock:
            return bool(self._queue) or any(lane.view() or lane.backlog() for lane in self.lanes.values())

    def pacing(self):
        """Are words waiting to be written? (The overlay then looks often, so each comes out on time.)"""
        with self._lock:
            return any(lane.backlog() for lane in self.lanes.values())

    def clear(self):
        with self._lock:
            for lane in self.lanes.values():
                lane.clear()
            self._queue.clear()
        self._changed()

    def close(self):
        self._closed = True
        self._wake.set()


__all__ = ["CFG_COLORS", "CFG_FONT", "CFG_IN_SHARE", "CFG_LANGUAGE", "CFG_ON", "CFG_SIZE", "CFG_WHO", "COLORS",
           "CaptionFeed", "DEFAULT_SIZE", "FONTS", "FONT_FAMILIES", "HOLD_S", "LANGUAGES", "Lane",
           "MAX_CHARS", "SIZES", "WHO_NAMES", "ends_sentence", "size_scale",
           "MAX_LINES", "SIDES", "WHO", "WHO_BOTH", "WHO_ME", "WHO_THEM", "clean", "place", "shows", "side_of",
           "words_of"]
