"""Live captions (founder, 2026-11-11): the words of a meeting at the bottom middle of the screen as they are said -
as spoken or translated, without filler words, without a background, each side of the call in its own colour.

What he asked for, and the decisions:
  * captions follow Transcribes with ("if I have chosen best accuracy I don't care much about the speed; if I choose
    Fast I want the real time"): on Fast they take the meeting's live stream (Gladia, or OpenAI's live model for
    Armenian) word by word as it is being said; on Best accuracy, a model on this computer or your own key they take
    that engine's finished pieces - later, the accurate words.
  * translated or not (cfg captions_language: "" = as spoken), in PHRASES (founder 2026-11-14: "it's removing
    everything and writing everything back ... very slow ... I want it tack, tack, tack"): whole translated sentences
    swapped two lines at a time and waited for the speaker to finish; now, as the live words settle, every
    PHRASE_WORDS settled words (or at a comma, from PHRASE_MIN_WORDS) go to the server's "interpret" mode (the
    Interpreter's) with the phrases before as context, and the sentence's end goes when it is finished - a few
    translated words at a time, close behind the voice. Up to TRANSLATE_WORKERS translations are on their way at
    once (one at a time could not keep up with a phrase every two seconds) and show in the order said.
  * "both with different colours and without overlapping": two sides that never mix - "them" (the call; everyone in a
    meeting with one source) and "me" (your microphone: the live stream's "You"), each in its own lines, and a switch
    for whose words (cfg captions_who: "both" | "them" | "me") that also works in the middle of a meeting.
  * filler words removed (text_cleanup's list, Dictation's "Remove filler words") from captions as spoken.
  * best practice (BBC / DCMP / university caption guides): at most two lines a side, about 32-42 characters a line,
    roll-up as new words come (live captions), a minimum time on screen, then they fade.
  * readable (founder 2026-11-13: "people cannot manage to read it ... keep a bit longer; a bigger size, also in the
    middle of a meeting"; "keep 3 sentences on the screen and a bit more space"): three lines a side, about MAX_CHARS
    60 a line (wider than the guides' 32-42, his call), one flow that rolls up a line at a time, every line stays
    LINE_TTL_S after it last changed and then leaves for good, and the size is a setting (cfg captions_size: a percent
    of the screen-based size, SIZE_MIN-SIZE_MAX; founder 2026-11-14: "maybe a slider, must be more clear") the live
    card can change too.
  * never late on purpose (founder 2026-11-14: "I hope it's not purposefully delaying it ... it should really be
    catching up with the words ... if you have the full sentence, just show the full sentence, but keep it"): a word
    is on screen the moment it is known - the words being said as they come, a finished sentence, piece or
    translation whole, at once. Only one thing holds words back: showing them would push a line off the screen before
    it could be read (READ_WPS, at least MIN_LINE_S since it last changed) - and never for more than MAX_WAIT_S.

Qt-free: the overlay (ui/captions_overlay.py) draws; this keeps the words. A "line" is (id, [words]): the id stays the
same while the line is on screen, so the overlay can fade in only the new words and slide lines up smoothly.
"""
import collections
import difflib
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
CFG_SIZE = "captions_size"                # percent (SIZE_MIN..SIZE_MAX); 2.1.7 saved a SIZES key
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
# A slider (founder 2026-11-14), in steps of SIZE_STEP percent of the screen-based size; 100 = the size it always was.
SIZE_MIN, SIZE_MAX, SIZE_STEP = 80, 180, 10
DEFAULT_SIZE = 100
# 2.1.7 saved one of four keys: still read.
SIZES = (("s", "Small", 0.8), ("m", "Medium", 1.0), ("l", "Large", 1.3), ("xl", "Extra large", 1.65))


def size_percent(value):
    """The size as a percent (SIZE_MIN..SIZE_MAX, in SIZE_STEP steps): a number, or a 2.1.7 key; 100 when unknown."""
    if isinstance(value, bool) or value is None:
        return DEFAULT_SIZE
    try:
        p = float(value)
    except (TypeError, ValueError):
        p = next((scale * 100 for k, _n, scale in SIZES if k == value), DEFAULT_SIZE)
    if p != p:                                                       # NaN
        return DEFAULT_SIZE
    return int(min(SIZE_MAX, max(SIZE_MIN, round(p / SIZE_STEP) * SIZE_STEP)))


def size_scale(value):
    """The font scale of a saved size (percent or 2.1.7 key)."""
    return size_percent(value) / 100.0


# Captions in: as spoken, or translated into one of these (the server's interpret mode names them all).
LANGUAGES = (("", "As spoken"), ("en", "English"), ("hy", "Armenian"), ("fr", "French"), ("de", "German"),
             ("es", "Spanish"), ("it", "Italian"), ("pt", "Portuguese"), ("ru", "Russian"), ("uk", "Ukrainian"),
             ("ar", "Arabic"), ("tr", "Turkish"), ("nl", "Dutch"), ("pl", "Polish"), ("zh", "Chinese"),
             ("ja", "Japanese"), ("ko", "Korean"), ("hi", "Hindi"), ("fa", "Persian"))

MAX_CHARS = 60          # a line, about (the guides say 32-42; founder 2026-11-13: fill more of the screen)
MAX_LINES = 3           # a side (the guides: two; founder 2026-11-13: "keep 3 sentences on the screen")
LINE_TTL_S = 10.0       # a line leaves the screen this long after it last changed, newer lines or not (founder
                        # 2026-11-14: "time sensitive ... the limit should be 10 seconds for each sentence")
HOLD_S = LINE_TTL_S     # ... so a side's captions are gone this long after its last word
TRANSLATE_CONTEXT = 4   # earlier phrases of the same side, sent as context only
TRANSLATE_WORKERS = 3   # translations on their way at once (one takes about 2 s, a phrase comes every 2 s: one at a
                        # time fell behind the voice); they show in the order said
TRANSLATE_BACKLOG = 4   # phrases waiting for a free worker beyond this (the server is slow or down): the oldest are
                        # dropped - captions keep up with the call
SENT_SLACK = 3          # the finished sentence may be this many words longer where its phrases ended (sent_upto)
PHRASE_WORDS = 6        # settled words that go to translation without waiting for the sentence's end ...
PHRASE_MIN_WORDS = 3    # ... or fewer, from this many, when they end at a comma (a clause)
READ_WPS = 4.0          # a line may roll off the screen only once it could be read at this pace (240 a minute) ...
MIN_LINE_S = 1.5        # ... and never sooner than this after it last changed
MAX_WAIT_S = 3.0        # ... and a word never waits longer than this for it: the captions keep up with the call
TYPE_AT_ONCE = 8        # finished words up to this many show at once (a phrase); more type in ...
TYPE_WPS = 15.0         # ... this fast, so a block of lines is never swapped at once (Best accuracy's 10-second
                        # pieces: 30 words in 2 s) - far faster than anyone reads, it only keeps the roll-up
GAP_MARK = "[...]"
_SENTENCE_END = re.compile(r"[.!?\u2026\u3002\uff01\uff1f\u0589\u055e]['\")\]\u00bb\u201d]*$")
_CLAUSE_END = re.compile(r"[,;:\u060c\u3001\uff0c\u055d]['\")\]\u00bb\u201d]*$")
_WORDLIKE = re.compile(r"[^\w]+", re.UNICODE)


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


def ends_clause(word):
    """Does this word end a clause or a sentence ("well,", "now.")? A translation phrase may stop there."""
    return ends_sentence(word) or bool(_CLAUSE_END.search(str(word or "")))


def _same_word(a, b):
    return _WORDLIKE.sub("", str(a)).lower() == _WORDLIKE.sub("", str(b)).lower()


def place(lines, words, next_id, max_chars=MAX_CHARS):
    """``words`` added after ``lines`` ([(id, [words])]) the way roll-up captions fill: on the last line while it
    fits, else on a new line (a word longer than a line gets one of its own) - one flow, so lines roll up one at a time
    (a new line for every sentence swapped blocks of lines at once). Returns (lines, next_id) - new lists, the given
    ones are not changed."""
    out = [(i, list(ws)) for i, ws in lines]
    for w in words:
        if out:
            last = out[-1][1]
            if not last:
                last.append(w)
                continue
            if len(" ".join(last)) + 1 + len(w) <= max_chars:
                last.append(w)
                continue
        out.append((next_id, [w]))
        next_id += 1
    return out, next_id


class Lane:
    """One side's captions: the lines on screen, the sentence being said, and finished words not shown yet.

    A word is on screen the moment it is known (see the module notes: never late on purpose). Words wait only when
    showing them would push a line off the screen before it could be read - at most MAX_WAIT_S. A line that left the
    screen (rolled above it, or its LINE_TTL_S passed) never comes back."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.lines = []          # [(id, [words])] - finished words on screen
        self.live = []           # the sentence being said (live stream partial)
        self.live_shown = 0      # ... how many of its words are on screen (all, unless a line is still being read)
        self.live_at = None      # ... when it last changed
        self.pending = []        # finished words not on screen yet (a block typing in, or a line being read)
        self.waiting_since = None
        self.type_at = None      # a block typing in: when its next word comes
        self.next_id = 1
        self.at = None           # when its words last changed
        self._shown = []         # [(id, [words])] on screen at the last look
        self._changed = {}       # line id -> when its words last changed on screen
        self._stale = None       # a sentence nobody finished that left the screen (the same words do not bring it back)

    def clear(self):
        self.lines, self.live, self.pending, self.at, self.waiting_since, self.type_at = [], [], [], None, None, None
        self.live_shown, self.live_at, self._shown, self._changed, self._stale = 0, None, [], {}, None

    def backlog(self):
        """Words said but not on screen yet."""
        return len(self.pending) + max(0, len(self.live) - self.live_shown)

    @staticmethod
    def read_s(words):
        """How long a line must have been on screen, unchanged, before it may roll off."""
        return max(MIN_LINE_S, len(words) / READ_WPS)

    def _fits(self, after, now):
        """Could the screen become ``after`` without a line leaving it unread? (Always, once words waited
        MAX_WAIT_S: the captions keep up with the call.)"""
        if self.waiting_since is not None and now - self.waiting_since >= MAX_WAIT_S:
            return True
        stay = {i for i, ws in [(i, ws) for i, ws in after if ws][-MAX_LINES:]}
        for lid, ws in self._shown:
            if lid not in stay:
                changed = self._changed.get(lid)
                if changed is not None and now - changed < self.read_s(ws):
                    return False
        return True

    def _put(self, after, next_id, now):
        self.lines, self.next_id, self.at = after[-MAX_LINES:], next_id, now

    def _release(self, now):
        """Put the waiting finished words on screen - a phrase all at once when it fits, a longer block typed in at
        TYPE_WPS, never pushing off a line still being read."""
        if not self.pending:
            self.type_at = None
            if self.live_shown >= len(self.live):
                self.waiting_since = None                       # (words being said that wait keep counting: view)
            return
        if self.type_at is None and len(self.pending) <= TYPE_AT_ONCE:
            after, nid = place(self.lines, self.pending, self.next_id)
            if self._fits(after, now):
                self._put(after, nid, now)
                self.pending, self.waiting_since = [], None
                return
        if self.type_at is None and len(self.pending) > TYPE_AT_ONCE:
            self.type_at = now                                   # a block: type it in
        while self.pending and (self.type_at is None or self.type_at <= now):
            after, nid = place(self.lines, self.pending[:1], self.next_id)
            if not self._fits(after, now):
                break
            self._put(after, nid, now)
            self.pending.pop(0)
            if self.type_at is not None:
                self.type_at = max(self.type_at, now - 0.25) + 1.0 / TYPE_WPS
        if not self.pending:
            self.waiting_since = self.type_at = None
        elif self.waiting_since is None and (self.type_at is None or self.type_at <= now):
            self.waiting_since = now                            # (typing in is not waiting)

    def queue(self, words, *_old):
        """Finished words (a finished piece, a translated phrase): on screen at once, whole - after anything still
        waiting."""
        words = list(words)
        if not words:
            return
        self._fresh()
        self.pending.extend(words)
        self.at = self._clock()

    def drop_live(self):
        self.live, self.live_shown, self.live_at = [], 0, None

    def pace(self):
        """Show what may be shown now. True when anything new came on screen."""
        before = self.backlog()
        self._release(self._clock())
        return self.backlog() < before

    def _fresh(self):
        """After its captions faded, a side starts on a new line."""
        if self.at is not None and self._clock() - self.at > HOLD_S and not self.pending:
            self.lines, self._shown, self._changed = [], [], {}
            self.drop_live()

    def partial(self, words):
        """The sentence being said, so far: on screen as it comes (a revised word changes in its place)."""
        words = list(words)
        if words and words == self._stale:
            return                                    # the sentence that already left the screen, said again as it was
        self._stale = None
        self._fresh()
        now = self._clock()
        if words != self.live:
            self.live_at = now
        self.live = words
        self.live_shown = min(self.live_shown, len(self.live))
        self.at = now

    def final(self, words):
        """The live stream finished the sentence being said: the finished words take its place, at once. Its words
        already on screen stay there (the finished ones in their place); only words that would push a line off
        before it could be read wait - behind them, not instead of them."""
        words = list(words)
        self._stale = None
        self._fresh()
        shown = self.live_shown
        self.drop_live()
        if self.pending or not words:                 # (behind a block still typing in: after it)
            self.queue(words)
            return
        now = self._clock()
        n, least = len(words), min(shown, len(words))
        while n > least:
            after, nid = place(self.lines, words[:n], self.next_id)
            if self._fits(after, now):
                break
            n -= 1
        if n:
            after, nid = place(self.lines, words[:n], self.next_id)
            self._put(after, nid, now)
        self.pending = words[n:]
        self.at = now
        if not self.pending:
            self.waiting_since = None
        elif self.waiting_since is None:
            self.waiting_since = now

    def _expire(self, now):
        """A line unchanged for LINE_TTL_S leaves the screen, even while newer lines stay - with every line above
        it (never a hole, never an older line back). The sentence being said stays while it changes; one nobody
        finished (the live stream stopped in the middle of it) leaves the same way."""
        if self.live and self.live_at is not None and now - self.live_at > LINE_TTL_S:
            self._stale = list(self.live)
            self.drop_live()
        last = -1
        for n, (lid, _ws) in enumerate(self.lines):
            changed = self._changed.get(lid)
            if changed is not None and now - changed > LINE_TTL_S:
                last = n
        if last >= 0:
            self.lines = self.lines[last + 1:]

    def view(self):
        """The lines on screen now (at most MAX_LINES): [] once every line had its LINE_TTL_S."""
        now = self._clock()
        self._expire(now)
        self._release(now)
        k = 0 if self.pending else len(self.live)
        while k > 0:
            after, _ = place(self.lines, self.live[:k], self.next_id)
            if self._fits(after, now):
                break
            k -= 1
        if not self.pending:
            if k == len(self.live):
                self.waiting_since = None
            elif self.waiting_since is None:
                self.waiting_since = now              # the words being said wait for a line to be read too
        self.live_shown = k
        lines, _ = place(self.lines, self.live[:k], self.next_id)
        visible = [(i, ws) for i, ws in lines if ws][-MAX_LINES:]
        if visible:                                   # a line that rolled above the screen is gone for good
            top = visible[0][0]
            self.lines = [(i, ws) for i, ws in self.lines if i >= top]
        before = dict(self._shown)
        for lid, ws in visible:
            if before.get(lid) != ws:
                self._changed[lid] = now
                self.at = now
        on_screen = {i for i, _ in visible}
        self._changed = {i: t for i, t in self._changed.items() if i in on_screen}
        self._shown = visible
        return visible


def _new_phrase():
    return {"sent": [], "prev": [], "side": None}


def sent_upto(sent, words):
    """How many of a finished sentence's ``words`` the phrases already sent (``sent``: live words) cover. The
    finished sentence is not the live words plus a tail - the engine revises it (a number as digits, a word joined
    or dropped) - so the place is found by the words, not by counting. In doubt a word is said twice, never lost."""
    if not sent:
        return 0
    a = [_WORDLIKE.sub("", str(w)).lower() for w in sent]
    b = [_WORDLIKE.sub("", str(w)).lower() for w in words[:len(sent) + SENT_SLACK]]
    blocks = [m for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if m.size]
    if sum(m.size for m in blocks) * 2 < len(a):
        return min(len(sent), len(words))             # hardly the same words: by the count
    return blocks[-1].b + blocks[-1].size


class CaptionFeed:
    """The meeting's words in, the captions out (view()). ``settings()`` is the app's cfg (read each time, so a switch
    in the middle of a meeting applies at once). ``translate(text, source_lang, target_lang, context)`` returns the
    translation (it may raise: the sentence is then shown as spoken). ``on_change()`` is called when the view may have
    changed - from the translation threads too, so the overlay marshals it to its own thread."""

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
        self._phrase = _new_phrase()          # the sentence being said: its words already sent to translation
        self._lock = threading.Lock()
        self._more = threading.Condition(self._lock)
        self._queue = collections.deque()     # (number, side, spoken, lang, target, context) waiting for a worker
        self._seq = 0                         # the number of the next phrase sent ...
        self._out = 0                         # ... and of the next one to show: translations show in the order said
        self._done = {}                       # number -> (side, words) | None, translated but not its turn yet
        self._closed = False
        self._threads = []
        if start_thread and translate is not None:
            for n in range(TRANSLATE_WORKERS):
                t = threading.Thread(target=self._worker, daemon=True, name="gusan-captions-translate-%d" % (n + 1))
                t.start()
                self._threads.append(t)
        self._thread = self._threads[0] if self._threads else None

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
        """The sentence being said, so far (real time): shown as it comes - or, translating into another language,
        sent to translation a phrase at a time as its words settle (a sentence whose first phrase went that way
        stays that way, on that side, to its end)."""
        if self._closed:
            return
        side = side_of(who)
        if self._phrase["sent"] or not self._as_spoken(side):
            self._phrase_partial(side, text)
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
        """A finished sentence: the live stream's (it takes the place of the words being said), or a finished piece
        (``paced``: after anything still waiting). Shown at once, whole."""
        if self._closed:
            return
        side = side_of(who)
        lang = self._base(language)
        if lang:
            self._heard[side] = lang
        target = self.target()
        words = words_of(text)
        if not paced:
            phrase, self._phrase = self._phrase, _new_phrase()
            if phrase["sent"]:
                # Phrases of this sentence are on screen or on their way, translated, on the phrase's side: its end
                # follows them the same way, on the same side - also when the speaker labels put the finished
                # sentence on the other side, or it turns out to be in the caption language (shown as spoken it
                # would be there twice).
                rest = " ".join(words[sent_upto(phrase["sent"], words):])
                if rest:
                    self._send(phrase["side"] or side, rest, lang or "auto", target)
                return
        if self._as_spoken(side, lang):
            with self._lock:
                if paced:
                    self.lanes[side].queue(words)
                else:
                    self._drop_other_live(side)
                    self.lanes[side].final(words)
            self._changed()
            return
        if words:                                            # translated, whole
            self._send(side, " ".join(words), lang or "auto", target)

    def _phrase_partial(self, side, text):
        """The live words of a sentence being translated: once PHRASE_WORDS of them are settled (the same in two
        updates in a row) - or PHRASE_MIN_WORDS ending at a comma - they go to translation as a phrase."""
        words = words_of(text)
        phrase = self._phrase
        prev, sent = phrase["prev"], len(phrase["sent"])
        phrase["prev"] = words
        settled = 0
        while settled < min(len(prev), len(words)) and _same_word(prev[settled], words[settled]):
            settled += 1
        unsent = words[sent:settled]
        if len(unsent) >= PHRASE_WORDS or (len(unsent) >= PHRASE_MIN_WORDS and ends_clause(unsent[-1])):
            phrase["side"] = phrase["side"] or side          # (which source is loud may change in mid-sentence)
            phrase["sent"] = words[:settled]
            self._send(phrase["side"], " ".join(unsent), self._heard.get(side) or "auto", self.target())

    def _send(self, side, spoken, lang, target):
        """One phrase or sentence to translate, with the side's phrases before it as context."""
        spoken = clean(spoken, fillers=False)
        if not spoken or not target:
            return
        with self._lock:
            context = list(self._said[side])
            self._said[side].append(spoken)
            self._queue.append((self._seq, side, spoken, lang or "auto", target, context))
            self._seq += 1
            late = False
            while len(self._queue) > TRANSLATE_BACKLOG:      # far behind the call (the server is slow or down): the
                self._done[self._queue.popleft()[0]] = None  # newest phrases matter
                late = True
            if late:
                self._show_done()
            self._more.notify()

    # ── translations ──
    def _worker(self):
        while True:
            with self._lock:
                while not self._queue and not self._closed:
                    self._more.wait(0.5)
                if self._closed:
                    return
                item = self._queue.popleft()
            self._translate_one(*item)

    def _translate_one(self, seq, side, spoken, lang, target, context):
        text = ""
        try:
            text = str(self._translate(spoken, lang, target, context) or "").strip()
        except Exception as e:
            logger.info("Caption translation failed (%s): shown as spoken", getattr(e, "code", "") or type(e).__name__)
        if self._closed:
            return
        # (a language changed meanwhile: nothing to show)
        words = words_of(text or spoken, fillers=False) if target == self.target() else []
        with self._lock:
            if seq < self._out:
                return                                       # cleared meanwhile
            self._done[seq] = (side, words) if words else None
            shown = self._show_done()
        if shown:
            self._changed()

    def _show_done(self):
        """Translations in the order their words were said (several are on their way at once; one that came back
        early waits for the ones before it). Under the lock. Returns how many were put on screen."""
        n = 0
        while self._out in self._done:
            item = self._done.pop(self._out)
            self._out += 1
            if item:
                self.lanes[item[0]].queue(item[1])
                n += 1
        return n

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
            return self._out < self._seq or any(lane.view() or lane.backlog() for lane in self.lanes.values())

    def pacing(self):
        """Are words waiting for a line to be read? (The overlay then looks often, so they show the moment they may.)"""
        with self._lock:
            return any(lane.backlog() for lane in self.lanes.values())

    def clear(self):
        """Nothing on screen, nothing on its way (the caption language changed): translations still out are dropped
        when they come back, and the sentence being said starts over."""
        with self._lock:
            for lane in self.lanes.values():
                lane.clear()
            self._queue.clear()
            self._done.clear()
            self._out = self._seq
            self._phrase = _new_phrase()
            for said in self._said.values():
                said.clear()
        self._changed()

    def close(self):
        self._closed = True
        with self._lock:
            self._more.notify_all()


__all__ = ["CFG_COLORS", "CFG_FONT", "CFG_IN_SHARE", "CFG_LANGUAGE", "CFG_ON", "CFG_SIZE", "CFG_WHO", "COLORS",
           "CaptionFeed", "DEFAULT_SIZE", "FONTS", "FONT_FAMILIES", "HOLD_S", "LANGUAGES", "LINE_TTL_S", "Lane",
           "MAX_CHARS", "MAX_LINES", "SIDES", "SIZES", "SIZE_MAX", "SIZE_MIN", "SIZE_STEP", "TRANSLATE_WORKERS",
           "WHO", "WHO_BOTH", "WHO_ME", "WHO_NAMES", "WHO_THEM", "clean", "ends_clause", "ends_sentence", "place",
           "sent_upto", "shows", "side_of", "size_percent", "size_scale", "words_of"]
