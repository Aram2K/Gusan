"""Keep made-up text out of the voice (bench 2026-11-09, FINDINGS A1 and V5).

Live speech-to-text writes words nobody said, mostly into the pause before it commits: a run of
words twice, a phrase it already sent, a short word in Latin letters for a word of another script
("Love." for Armenian Լավ, "How there?" for Chinese 好的). Each phrase is checked here BEFORE it is
queued: later, waiting phrases are merged into one turn and a slip can't be told apart any more.

  * a run of words said twice back to back is said once ("no, no, no" -> "no"); in Armenian and
    Georgian such a repeat comes with invented text, so the whole sentence holding it goes (when
    it is the whole phrase, it is said once: a real "Ալո, ալո, ալո" is not lost);
  * a phrase that repeats a recent one, or the end of the one before it, is dropped;
  * a flip - a language with a script of its own, and the phrase is 2 words or fewer, all in Latin
    letters, not a Dictionary word, no @, domain or digit - is shown but not voiced.

Words are counted the bench's way: punctuation is ignored and an address or a domain is one word
(segmenter.units counts "there？" as two words, and most rules would miss).

VoiceMeter (V5) is the Hear side's ear for the other person: seconds of the call's cleaned sound
(Gusan's own voice taken out) above a small floor. One dot product per chunk, nothing else.

Pure logic, no threads: the caller passes the clock time.
"""
import collections
import re
import unicodedata

import numpy as np

import speech_langs
from interpreter.audio_io import RATE

WHOLE_SENTENCE = frozenset(("hy", "ka"))   # a repeat takes its whole sentence with it
MAX_WORDS = 2                 # a flip is this short
MAX_RUN = 12                  # words: the longest run looked for twice in a row
# A phrase that repeats the end of the longer one just before (2+ words, that one came at most END_S
# ago: the speech-to-text wrote its tail again), or a whole recent one (SAME_WORDS+ words, RECENT_S):
# dropped. A short reply said again ("Yes, sure." to two questions) is a real answer and stays.
END_S = 5.0
END_SLACK = 4                 # letters the STT may still add after the repeated end ("...անգամ ան")
SAME_WORDS = 4
RECENT_S = 30.0
KEEP = 4                      # phrases remembered
VOICE_FLOOR = 100.0           # rms: a chunk above it holds the other person's voice
# A Hear phrase with less of their voice than this, while Gusan's voice played, is not voiced. The
# bench's 0.3 s was measured on the call's own track; what the app hears of them over Gusan's voice
# (the call turned down to 0.25, the gate) is less: a real short reply ("Լավ", 0.48 s) keeps 0.20-0.30 s,
# Gusan alone 0.00 s (offline replay of the corpus through the canceller and the gate).
MIN_VOICE_S = 0.1
# ...counted from the last phrase's end, but at least this far back from its own end: its words can
# come before the last phrase's end (a short tail said right after an early cut, a slow commit).
VOICE_LOOKBACK_S = 1.0

# Inside a word these keep it whole: an address, a domain, "don't", "M-O-S-S", "10:30", and the
# Armenian marks written inside words (լա՞վ).
_INSIDE = frozenset("@.'’-_/:՚՛՜՞՟")
_SENTENCE_END = re.compile(r"[.!?։:;…]+(?=\s|$)")
_NOT_ALNUM = re.compile(r"[\W_]+")
_OK = frozenset(("ok", "okay"))   # said as a loanword in every language: no flip


def _is_word_char(ch):
    return ch.isalnum() or unicodedata.category(ch)[0] == "M"


def spans(text):
    """(start, end) of each word: letters, digits and their marks; one of _INSIDE between two
    of them keeps the word whole (a "," only between digits: "4,500"). Anything else ends it."""
    out, start, n = [], None, len(text)
    for i, ch in enumerate(text):
        if _is_word_char(ch):
            if start is None:
                start = i
        elif start is not None and i + 1 < n and _is_word_char(text[i + 1]) and (
                ch in _INSIDE or (ch == "," and text[i - 1].isdigit() and text[i + 1].isdigit())):
            continue
        elif start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, n))
    return out


def _key(word):
    return unicodedata.normalize("NFKC", word).casefold()


def words(text):
    """The words as people read them (case and width folded)."""
    return [_key(text[s:e]) for s, e in spans(text or "")]


def _letters(text):
    """Letters and digits only, folded: how a repeat is compared ("Northpeak.io." is the end of
    "r.moss@northpeak.io.")."""
    return _NOT_ALNUM.sub("", _key(text or ""))


def _plain(w):
    """A number or a single letter: "5 5 5" and "M O S S" are no repeats."""
    return w.isdigit() or len(w) == 1


def find_repeat(keys):
    """(i, k, copies): the first run of k words said again right after itself (k >= 2), or one
    word 3 times or more in a row ("no, no" is real speech); None when there is none."""
    n = len(keys)
    for i in range(n - 1):
        for k in range(1, min(MAX_RUN, (n - i) // 2) + 1):
            if keys[i] != keys[i + k]:               # (the cheap look first)
                continue
            run = keys[i:i + k]
            copies = 1
            while keys[i + copies * k:i + (copies + 1) * k] == run:
                copies += 1
            if copies >= (3 if k == 1 else 2) and not all(_plain(w) for w in run):
                return i, k, copies
    return None


def _sentences(text):
    out, start = [], 0
    for m in _SENTENCE_END.finditer(text):
        out.append((start, m.end()))
        start = m.end()
    if text[start:].strip():
        out.append((start, len(text)))
    return out


def drop_repeats(text, whole_sentence=False):
    """``text`` with each back-to-back repeat said once - or, ``whole_sentence``, without the
    sentences that hold one, unless nothing else is left: then it is said once too (a real "Ալո,
    ալո, ալո" or "Լսու՞մ եք, լսու՞մ եք" on a bad line)."""
    for _ in range(8):                      # each pass takes one repeat out
        sp = spans(text)
        hit = find_repeat([_key(text[s:e]) for s, e in sp])
        if hit is None:
            break
        i, k, copies = hit
        a, b = sp[i][0], sp[i + copies * k - 1][1]
        rest = ""
        if whole_sentence:
            rest = "".join(text[s:e] for s, e in _sentences(text) if e <= a or s >= b).strip()
        text = rest if spans(rest) else text[:sp[i + k - 1][1]] + text[b:]
        text = text.strip()
    return text


class Guard:
    """One direction's check. ``src``: the language spoken; ``glossary``: the Dictionary words
    (written in Latin letters on purpose, so never a flip)."""

    def __init__(self, src, glossary=()):
        self.src = speech_langs.normalize(src) if src else ""
        self.whole_sentence = self.src in WHOLE_SENTENCE
        self.own_script = speech_langs.has_distinct_script(self.src)
        self.dictionary = {w for term in glossary for w in words(str(term or ""))}
        self._recent = collections.deque(maxlen=KEEP)     # (time, letters) of the last phrases

    def reset(self):
        self._recent.clear()

    def check(self, text, now):
        """-> (text, say): the phrase to queue (maybe with a repeat taken out) and whether it may be
        voiced; None when it is dropped (nothing left, or it repeats a recent phrase)."""
        flat = heard = _letters(text)
        try:
            sp = spans(text)
            if find_repeat([_key(text[s:e]) for s, e in sp]) is not None:
                text = drop_repeats(text, self.whole_sentence)
                if not text:
                    return None
                sp, flat = spans(text), _letters(text)
            if self._repeats(len(sp), flat, now):
                return None
            return text, not (len(sp) <= MAX_WORDS and self.is_flip(text))
        finally:
            self._recent.append((now, heard))

    def _repeats(self, n, flat, now):
        """``n`` words, ``flat`` their letters: a repeat of the phrase just before's end, or of a
        whole recent phrase?"""
        if not self._recent:
            return False
        t, last = self._recent[-1]
        if n >= 2 and now - t <= END_S and flat != last and flat in last[-(len(flat) + END_SLACK):]:
            return True
        return n >= SAME_WORDS and any(flat == p for t, p in self._recent if now - t <= RECENT_S)

    def is_flip(self, text):
        """A short Latin-letter phrase where the language has its own script ("Love." for Լավ)."""
        if not self.own_script or "@" in text:
            return False
        ws = words(text)
        if not ws or len(ws) > MAX_WORDS:
            return False
        if any(not w.isalpha() for w in ws):           # digits, a domain ("aibuben.xyz")
            return False
        if any(w in self.dictionary for w in ws) or " ".join(ws) in _OK:
            return False
        letters = [ch for ch in text if ch.isalpha()]
        return all("LATIN" in unicodedata.name(ch, "") for ch in letters)


class VoiceMeter:
    """How much of the other person's voice the Hear side heard: the call's cleaned sound (Gusan's
    own voice taken out, muted while it plays unless they talk over it), chunk by chunk. feed() runs
    on the capture thread, since() where phrases are cut: a deque takes both without a lock."""

    def __init__(self, floor=VOICE_FLOOR, keep=3000):
        self._floor2 = floor * floor
        self._heard = collections.deque(maxlen=keep)   # (time, seconds) of chunks with their voice

    def feed(self, pcm, now):
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if a.size and float(np.dot(a, a)) >= self._floor2 * a.size:     # rms >= floor, no root
            self._heard.append((now, a.size / RATE))

    def since(self, t):
        """Seconds of their voice after clock time ``t`` (older chunks are forgotten)."""
        h = self._heard
        while h and h[0][0] <= t:
            h.popleft()
        return sum(s for _t, s in list(h))
