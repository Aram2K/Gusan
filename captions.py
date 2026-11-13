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
# Captions in: as spoken, or translated into one of these (the server's interpret mode names them all).
LANGUAGES = (("", "As spoken"), ("en", "English"), ("hy", "Armenian"), ("fr", "French"), ("de", "German"),
             ("es", "Spanish"), ("it", "Italian"), ("pt", "Portuguese"), ("ru", "Russian"), ("uk", "Ukrainian"),
             ("ar", "Arabic"), ("tr", "Turkish"), ("nl", "Dutch"), ("pl", "Polish"), ("zh", "Chinese"),
             ("ja", "Japanese"), ("ko", "Korean"), ("hi", "Hindi"), ("fa", "Persian"))

MAX_CHARS = 40          # a line, about (the guides: 32-42)
MAX_LINES = 2           # a side (the guides: two lines; roll-up for live captions)
HOLD_S = 4.5            # a side's captions stay this long after its last word, then fade
KEEP_LINES = MAX_LINES + 2
TRANSLATE_CONTEXT = 3   # earlier sentences of the same side, sent as context only
TRANSLATE_BACKLOG = 3   # a translation queue longer than this drops its oldest sentences (captions keep up with the call)
PACE_WPS = 3.5          # words a second let out of a finished piece (about 210 a minute)
TRANSLATED_WPS = 12.0   # a translated sentence comes out quickly (it already waited for its translation)
CATCH_UP_S = 6.0        # ... faster when more is waiting: never more than this behind
GAP_MARK = "[...]"


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


def place(lines, words, next_id, max_chars=MAX_CHARS):
    """``words`` added after ``lines`` ([(id, [words])]) the way a caption fills: on the last line while it fits, else
    on a new line (a word longer than a line gets one of its own). Returns (lines, next_id) - new lists, the given ones
    are not changed."""
    out = [(i, list(ws)) for i, ws in lines]
    for w in words:
        if out:
            last = out[-1][1]
            width = len(" ".join(last + [w]))
            if last and width <= max_chars:
                last.append(w)
                continue
            if not last:
                last.append(w)
                continue
        out.append((next_id, [w]))
        next_id += 1
    return out, next_id


class Lane:
    """One side's captions: the lines of its finished sentences, and the words of the sentence being said."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.lines = []          # [(id, [words])] - finished sentences
        self.live = []           # the sentence being said (live stream partial)
        self.pending = []        # finished words waiting to be let out at reading pace (queue)
        self.paced_at = None
        self.rate = PACE_WPS
        self.next_id = 1
        self.at = None           # when its words last changed

    def clear(self):
        self.lines, self.live, self.pending, self.at, self.paced_at = [], [], [], None, None

    def queue(self, words, wps=PACE_WPS):
        """Finished words to let out at ``wps`` (pace()): the first at once."""
        now = self._clock()
        if not self.pending:
            self.rate = wps                  # (words already waiting keep the pace they were queued at)
            self._fresh()
            self.paced_at = max(now, self.paced_at or now)
        self.pending.extend(words)
        self.at = now

    def pace(self):
        """Let out the words that are due (PACE_WPS a second, faster when more is waiting). True when any were."""
        if not self.pending:
            return False
        now = self._clock()
        if self.paced_at is None:
            self.paced_at = now
        if now < self.paced_at:
            return False
        rate = max(self.rate, len(self.pending) / CATCH_UP_S)
        due = min(len(self.pending), 1 + int((now - self.paced_at) * rate))
        out, self.pending = self.pending[:due], self.pending[due:]
        self.lines, self.next_id = place(self.lines, out, self.next_id)
        self.lines = self.lines[-KEEP_LINES:]
        self.paced_at += due / rate
        self.at = now
        return True

    def _fresh(self):
        """After its captions faded, a side starts on a new line."""
        if self.at is not None and self._clock() - self.at > HOLD_S:
            self.lines, self.live = [], []

    def partial(self, words):
        self._fresh()
        self.live = list(words)
        self.at = self._clock()

    def final(self, words):
        self._fresh()
        words = list(words)
        if words:
            self.lines, self.next_id = place(self.lines, words, self.next_id)
            self.lines = self.lines[-KEEP_LINES:]
        self.live = []
        self.at = self._clock()

    def view(self):
        """The lines on screen now (at most MAX_LINES), [] once the side has been quiet for HOLD_S."""
        self.pace()
        if self.at is None or (self._clock() - self.at > HOLD_S and not self.pending):
            return []
        lines, _ = place(self.lines, self.live, self.next_id)
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
                lane.live = []

    def final(self, text, who="", language="", paced=False):
        """A finished sentence: the live stream's (shown at once - it replaces the words being said), or a finished
        piece (``paced``: let out at reading pace)."""
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
                    self.lanes[side].queue(words_of(text))
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
            self.lanes[side].queue(words_of(text or spoken, fillers=False), TRANSLATED_WPS)
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
            return bool(self._queue) or any(lane.pending or lane.view() for lane in self.lanes.values())

    def clear(self):
        with self._lock:
            for lane in self.lanes.values():
                lane.clear()
            self._queue.clear()
        self._changed()

    def close(self):
        self._closed = True
        self._wake.set()


__all__ = ["CFG_COLORS", "CFG_FONT", "CFG_IN_SHARE", "CFG_LANGUAGE", "CFG_ON", "CFG_WHO", "COLORS", "CaptionFeed",
           "FONTS", "FONT_FAMILIES", "HOLD_S", "LANGUAGES", "Lane",
           "MAX_CHARS", "WHO_NAMES",
           "MAX_LINES", "SIDES", "WHO", "WHO_BOTH", "WHO_ME", "WHO_THEM", "clean", "place", "shows", "side_of",
           "words_of"]
