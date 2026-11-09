"""Cut a stream of live transcripts into phrases worth translating.

The speech-to-text service already commits a transcript at every pause (its own voice activity
detection), and most phrases come from there. Three things it can't do, done here:

* A long run-on sentence with no pause would wait until the speaker breathes. When the live
  (partial) words have stayed the same for a moment and contain a clause end (, . ? ! and the
  CJK marks), the stable part goes out early. Without punctuation, a very long stable stretch is
  cut at a word boundary.
* A short pause is not long enough for a commit, and a real one is committed only ~0.4 s after
  the service's own wait. tick() looks again on time, with no new partial needed: live words that
  end in a comma and stood still for ``stable_s`` go out at once. A full stop there goes out once
  the next partial goes on after it (the service ends what it heard so far with one, even
  mid-sentence, and a partial comes only about once a second).
* The committed text can differ from the partial words that were already sent (the service
  revises the last few words). Only the part after what was sent goes out, aligned by units
  (words, and each Chinese or Japanese character on its own).

Pure logic, no threads, no I/O: the caller passes the clock time.
"""
import re

# A clause ends after one of these marks. A Western mark needs a space after it (so "3.5" does not
# cut); CJK marks end a clause by themselves.
_WESTERN_END = re.compile(r"[.!?;:,։՝؟،؛](?=\s|$)")
_CJK_END = re.compile(r"[。！？、，；：]")
# ...but a "." after a single letter ("U.S.", "e.g.", "a.m.") or a short title ("Dr.") ends none.
_ABBREV = re.compile(r"(?:^|[\s.])(?:[^\W\d_]|mrs?|ms|dr|st|vs|etc|jr|sr|prof)\.$", re.IGNORECASE)
# A mark at the very end of the live words counts once the newest partial goes on after it. While
# they only stand still, just a comma glued to its word does: Scribe ends what it heard so far with
# a full stop even mid-sentence, partials come ~1 s apart (so standing still is no pause), and a
# CJK mark is a unit of its own (one it takes back would shift the words sent).
_STILL_END = frozenset(",;՝،؛")

# One unit: a Chinese or Japanese character or mark (written without spaces; a full-width comma
# never glues to the word after it), or a run of anything else between spaces (a word). The same
# rule for every string, so mixed text ("今日の meeting は") and short heads ("감사합니다,") are counted
# and cut alike, whatever the mix.
_CJK = "\u3001-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef\U00020000-\U0002fa1f"
_UNIT = re.compile(f"[{_CJK}]|[^\\s{_CJK}]+")


def units(text):
    """Unit count of ``text`` (words, and each Chinese or Japanese character)."""
    return len(_UNIT.findall(text or ""))


def _cut_after_units(text, n):
    """Split ``text`` after its first ``n`` units -> (head, tail)."""
    if n <= 0:
        return "", text
    for i, m in enumerate(_UNIT.finditer(text), 1):
        if i == n:
            return text[:m.end()], text[m.end():]
    return text, ""


class PhraseCutter:
    """Feed it ``partial(text, now)`` and ``committed(text, now)``, and call ``tick(now)`` often
    (~every 50 ms); each returns a list of phrases (strings) that are ready for translation now,
    in order.

    min_units  - an early cut needs at least this many units before the clause end.
    max_units  - with no clause end, a stable stretch this long is cut anyway.
    stable_s   - how long the live words must stay unchanged before an early cut.
    """

    def __init__(self, min_units=4, max_units=24, stable_s=0.35):
        self.min_units = min_units
        self.max_units = max_units
        self.stable_s = stable_s
        self._sent_units = 0          # units of the current utterance already sent
        self._history = []            # (time, text) of recent partials in this utterance
        self._since = 0.0             # when the newest partial's exact words first showed
        self._settled = True          # nothing more can come of the partials so far (tick rests)
        self._wake = 0.0              # tick looks again no sooner (nothing can change before)

    def reset(self):
        self._sent_units = 0
        self._history = []
        self._settled = True

    # -- input -------------------------------------------------------------------------------
    def partial(self, text, now):
        text = (text or "").strip()
        if not text:
            return []
        if not self._history or self._history[-1][1] != text:
            self._since = now
        self._history.append((now, text))
        # keep just enough history to judge stability
        cutoff = now - max(2.0, self.stable_s * 4)
        while len(self._history) > 2 and self._history[0][0] < cutoff:
            self._history.pop(0)
        return self._cut(now)

    def tick(self, now):
        """No new partial: the live words may have stood still long enough by now (a short
        pause). No work until a partial leaves the stable_s window (only that changes the look),
        and none at all once the newest words were judged as stood still."""
        if self._settled or now < self._wake:
            return []
        return self._cut(now)

    def committed(self, text, now=None):
        text = (text or "").strip()
        sent = self._sent_units
        self.reset()
        if not text:
            return []
        _head, rest = _cut_after_units(text, sent)
        rest = rest.strip()
        return [rest] if rest and any(c.isalnum() for c in rest) else []

    def flush(self):
        """The connection to the service dropped before it committed the utterance: the live
        words it already showed, minus what was sent (it will never commit them now), and a fresh
        start for the next utterance."""
        last = self._history[-1][1] if self._history else ""
        return self.committed(last)

    # -- helpers -----------------------------------------------------------------------------
    def _cut(self, now):
        """The stable live words' early cut, if any."""
        last = self._history[-1][1]
        stable = self._stable_prefix(now)
        # the newest words stood still for stable_s: a comma at their very end counts, and nothing
        # new can come of them until the next partial (tick rests)
        still = stable == last and now - self._since >= self.stable_s
        self._settled = still
        # the look only changes when the oldest partial in the window leaves it
        self._wake = min((ts for ts, _t in self._history if ts >= now - self.stable_s),
                         default=now) + self.stable_s
        if not stable:
            return []
        _sent, rest = _cut_after_units(stable, self._sent_units)
        if units(rest) < self.min_units:
            return []
        cut = self._clause_cut(rest, last[len(stable):len(stable) + 1], still)
        if cut is None and units(rest) >= self.max_units:
            # no clause end in a long stable run: cut at a word boundary, keep 2 units back
            # (the service may still revise the last words)
            cut = _cut_after_units(rest, units(rest) - 2)[0]
        if not cut or units(cut) < self.min_units:
            return []
        self._sent_units += units(cut)
        self._wake = now                  # the rest may hold one more cut: look at the next tick
        return [cut.strip()]

    def _stable_prefix(self, now):
        """The longest text that every partial of the last ``stable_s`` seconds starts with,
        provided partials go back at least that far."""
        if not self._history or now - self._history[0][0] < self.stable_s:
            return ""
        window = [t for (ts, t) in self._history if ts >= now - self.stable_s]
        # include the last partial from before the window: it has been on screen since then
        older = [t for (ts, t) in self._history if ts < now - self.stable_s]
        if older:
            window.insert(0, older[-1])
        prefix = window[0]
        for t in window[1:]:
            i = 0
            limit = min(len(prefix), len(t))
            while i < limit and prefix[i] == t[i]:
                i += 1
            prefix = prefix[:i]
            if not prefix:
                return ""
        return prefix

    def _clause_cut(self, rest, nxt, still):
        """The longest head of ``rest`` that ends at a clause mark and has >= min_units units.
        A mark at the very end of ``rest`` counts when the newest partial goes on from it (``nxt``:
        the character after ``rest`` there; a space after a Western mark), or, for a comma (not
        "1," of "1,500"), when the words stood still (``still``)."""
        n = len(rest)
        comma = still and rest[-1:] in _STILL_END and not rest[-2:-1].isdigit()
        end = max((m.end() for m in _CJK_END.finditer(rest) if m.end() < n or nxt), default=0)
        for m in _WESTERN_END.finditer(rest):
            e = m.end()
            if e == n and not (comma or nxt.isspace()):
                continue                  # "3." of "3.5", or a full stop Scribe takes back
            if m.group() == "." and _ABBREV.search(rest, max(0, e - 7), e):
                continue                  # "U.S.", "e.g.", "Dr." (looks at the last 7 characters)
            end = max(end, e)
        head = rest[:end]
        return head if end and units(head) >= self.min_units else None
