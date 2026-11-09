"""Accuracy and summary maths for the bench (numpy + the standard library).

    normalize(text)          language-aware cleanup before scoring (case, punctuation incl.
                             Armenian and CJK/Arabic marks, quotes, dashes; numbers kept)
    units(text)              scoring units: words, but each CJK / kana / Thai character is its
                             own unit, so mixed lines like "demoのリンク" score fairly
    wer / cer / chrf         single pair -> float (wer_counts / cer_counts give the parts)
    score_transcript(...)    one long transcript against a conversation's timeline turns:
                             whole-text WER/CER, bag-of-units F1 (order free), per turn (by word
                             time when the engine gave timings, else by the alignment), per
                             language, mixed-language lines vs single-language lines, echo words
    summary(values)          n, mean, p50, p90, p95, min, max

WER here is computed over units, so for Japanese / Chinese / Thai it equals a character
error rate; cer() always works on characters with spaces removed. primary_rate() picks CER
for ja/zh/th and WER for the rest.
"""
import functools
import math
import re
import unicodedata
from collections import Counter

import numpy as np

NO_SPACE_LANGS = {"ja", "zh", "yue", "th", "lo", "km", "my", "bo"}

_TAG = re.compile(r"\[[^\[\]\n]{1,40}\]")                 # v3 audio tags ([laughs]), STT events ([music])
# 1,000 / 1 000 / 1'000 / ١٬٠٠٠ -> 1000 (comma, no-break / narrow / thin space, apostrophe, Arabic separator)
_DIGIT_GROUP = re.compile("(?<=\\d)[,\u00a0\u202f\u2009'\u066c](?=\\d{3}(?!\\d))")
_DECIMAL = re.compile("(?<=\\d)[.,\u066b](?=\\d)")        # 3.5 / 3,5 / ٣٫٥ -> 3.5
_DOT = "․"                                           # placeholder that survives punctuation removal
_APOSTROPHES = set("'’‘ʼ՚`´′ʹ")
_ALWAYS_DROP = {"՛", "՜", "՞", "՟"}    # Armenian emphasis / exclamation / question /
                                                           # abbreviation marks sit inside words
_ARABIC_MARKS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ARABIC_FORMS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا",
                               "ٱ": "ا", "ى": "ي"})

# Addresses read out ("r dot moss at northpeak dot io", "billing собака nordsoft точка dev") and written
# ("r.moss@northpeak.io") score the same: a written "@" and a dot inside a lower-case address become
# "at" / "dot", and so do the spoken "@" / "." of the corpus languages inside an address (see _addresses).
_ADDRESS_DOT = re.compile(r"(?<=[^\W_])\.(?=[a-z][a-z0-9])")      # "r.moss", "gmail.com"; not "a.m.", "3.5"
_WRITTEN_AT = ""                                            # a written "@" until _addresses
_AT_WORDS = frozenset({"собака", "собачка", "arobase", "arroba", "艾特", "アット", "շնիկ", "ძაღლუკა"})
_DOT_WORDS = frozenset({"точка", "point", "punto", "点", "ドット", "կետ", "წერტილი"})


# -- normalisation --------------------------------------------------------------------------

_SCRIPT_RANGES = (
    (0x0041, 0x02AF, "Latn"), (0x1E00, 0x1EFF, "Latn"), (0x0370, 0x03FF, "Grek"), (0x1F00, 0x1FFF, "Grek"),
    (0x0400, 0x052F, "Cyrl"), (0x1C80, 0x1C8F, "Cyrl"), (0x2DE0, 0x2DFF, "Cyrl"), (0xA640, 0xA69F, "Cyrl"),
    (0x0530, 0x058F, "Armn"), (0xFB13, 0xFB17, "Armn"), (0x0590, 0x05FF, "Hebr"), (0xFB1D, 0xFB4F, "Hebr"),
    (0x0600, 0x06FF, "Arab"), (0x0750, 0x077F, "Arab"), (0x08A0, 0x08FF, "Arab"), (0xFB50, 0xFDFF, "Arab"),
    (0xFE70, 0xFEFF, "Arab"), (0x0900, 0x097F, "Deva"), (0x10A0, 0x10FF, "Geor"), (0x1C90, 0x1CBF, "Geor"),
    (0x2D00, 0x2D2F, "Geor"), (0x1100, 0x11FF, "Hang"), (0x3130, 0x318F, "Hang"), (0xAC00, 0xD7AF, "Hang"))


@functools.lru_cache(maxsize=4096)
def _script(ch):
    """A coarse script for a letter ("digit" for 0-9, "other" for unlisted scripts); None for
    anything else (combining marks, the decimal point), which stays with the character before."""
    if "0" <= ch <= "9":
        return "digit"
    if not ch.isalpha():
        return None
    o = ord(ch)
    for a, b, name in _SCRIPT_RANGES:
        if a <= o <= b:
            return name
    return "other"


def _split_scripts(t):
    """A space where a word changes script or goes from letters to digits ("buildը" ->
    "build ը", "3pm" -> "3 pm"): mixed-language words then count the same however an engine
    glues or hyphenates them."""
    out = []
    prev = None
    for ch in t:
        if ch == " ":
            prev = None
        else:
            s = _script(ch)
            if s is not None:
                if prev is not None and s != prev:
                    out.append(" ")
                prev = s
        out.append(ch)
    return "".join(out)


def _latin_word(tok):
    """A word of Latin letters and / or digits (a decimal point allowed)."""
    seen = [s for s in map(_script, tok) if s is not None]
    return bool(seen) and all(s in ("Latn", "digit") for s in seen)


def _addresses(tokens):
    """An address in words reads like a written one: a written "@" is "at"; and in a stretch of
    Latin-letter / digit words that holds an "@" (written, or spoken: собака, arobase, arroba, 艾特
    ...), the spoken "@" / "." between two such words become "at" / "dot" (точка, point, punto,
    点 ...). English "at" / "dot" are already those words. Elsewhere ("точка зрения", "三点", "the
    point is") the words stay."""
    out = [("at" if w == _WRITTEN_AT else w) for w in tokens]
    n = len(out)
    written = {k for k, w in enumerate(tokens) if w == _WRITTEN_AT}

    def between(k):
        return 0 < k < n - 1 and _latin_word(out[k - 1]) and _latin_word(out[k + 1])
    k = 0
    while k < n:
        j = k
        while j < n and (_latin_word(out[j]) or out[j] in _AT_WORDS or out[j] in _DOT_WORDS):
            j += 1
        if j == k:
            k += 1
            continue
        if any(i in written or (out[i] in _AT_WORDS and between(i)) for i in range(k, j)):
            for i in range(k, j):
                if (out[i] in _AT_WORDS or out[i] in _DOT_WORDS) and between(i):
                    out[i] = "at" if out[i] in _AT_WORDS else "dot"
        k = j
    return out


def _join_spelled(tokens):
    """Three or more single Latin letters in a row are a word spelled out: "M-O-S-S" = "M O S S" = "MOSS"."""
    out, run = [], []
    for w in list(tokens) + [None]:
        if w is not None and len(w) == 1 and "a" <= w <= "z":
            run.append(w)
            continue
        out += ["".join(run)] if len(run) >= 3 else run
        run = []
        if w is not None:
            out.append(w)
    return out


def normalize(text, lang=None):
    """Lowercased (cased scripts), NFKC, without punctuation / symbols, single spaces.

    Quotes and dashes become spaces; apostrophes and the Armenian marks written inside
    words (՞ ՛ ՜) are dropped without splitting the word; Arabic vowel marks and tatweel are
    dropped and alef forms unified; digits stay, in ASCII (٣ -> 3), 1,000 -> 1000 and
    3,5 -> 3.5; a word is split where its script changes ("Gusanի" = "Gusan-ի" = "gusan ի");
    bracketed tags ([laughs], [music]) go. Addresses read the same written or read out
    ("r.moss@northpeak.io" = "r dot moss at northpeak dot io" = "... собака ... точка ..."), and
    a word spelled letter by letter is one word ("M-O-S-S" = "MOSS"). ``lang`` is accepted for
    symmetry; the rules are safe for every script, so mixed-language text is handled in one pass."""
    t = unicodedata.normalize("NFKC", text or "")
    t = _TAG.sub(" ", t)
    t = _ADDRESS_DOT.sub(" dot ", t)                   # before casefold: an address is written in lower case
    t = t.replace("@", f" {_WRITTEN_AT} ")
    t = t.casefold()
    t = _ARABIC_MARKS.sub("", t).translate(_ARABIC_FORMS)
    t = _DIGIT_GROUP.sub("", t)
    t = _DECIMAL.sub(_DOT, t)
    out = []
    n = len(t)
    for i, ch in enumerate(t):
        if ch == _DOT:
            out.append(".")
            continue
        if ch in _ALWAYS_DROP:
            continue
        if ch in _APOSTROPHES:
            inside = 0 < i < n - 1 and t[i - 1].isalnum() and t[i + 1].isalnum()
            out.append("" if inside else " ")
            continue
        cat = unicodedata.category(ch)
        if cat == "Cf":                   # zero-width joiners, soft hyphen: not a word break
            continue
        if cat == "Nd":
            out.append(str(unicodedata.decimal(ch, 0)))      # Arabic-Indic, Devanagari, ... digits -> 0-9
        elif cat[0] in "PSZ" or cat == "Cc":
            out.append(" ")
        else:
            out.append(ch)
    return " ".join(_join_spelled(_addresses(_split_scripts("".join(out)).split())))


def _char_unit(ch):
    """Characters scored one by one (no spaces between words in these scripts)."""
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0x3400 <= o <= 0x4DBF
            or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FA1F
            or o in (0x3005, 0x3006) or 0x0E00 <= o <= 0x0EFF or 0x1000 <= o <= 0x109F
            or 0x1780 <= o <= 0x17FF or 0x0F00 <= o <= 0x0FFF)


def units(norm_text):
    """Scoring units of normalised text: whitespace words, with every CJK / kana / Thai-like
    character as its own unit (combining marks stay with their base character)."""
    out = []
    for tok in (norm_text or "").split():
        word = ""
        for ch in tok:
            if unicodedata.category(ch)[0] == "M":
                if word:
                    word += ch
                elif out:
                    out[-1] += ch
                else:
                    word = ch
            elif _char_unit(ch):
                if word:
                    out.append(word)
                    word = ""
                out.append(ch)
            else:
                word += ch
        if word:
            out.append(word)
    return out


def chars(norm_text):
    """Characters for CER: the normalised text without any whitespace."""
    return [c for c in (norm_text or "") if not c.isspace()]


def text_units(text, lang=None):
    return units(normalize(text, lang))


def spoken_text(text):
    """What a voice actually says for a corpus line (v3 audio tags removed)."""
    return " ".join(_TAG.sub(" ", text or "").split())


# -- edit distance --------------------------------------------------------------------------

_MAX_CELLS = 40_000_000


def _encode(a, b):
    vocab = {}
    ai = np.fromiter((vocab.setdefault(x, len(vocab)) for x in a), dtype=np.int64, count=len(a))
    bi = np.fromiter((vocab.setdefault(x, len(vocab)) for x in b), dtype=np.int64, count=len(b))
    return ai, bi


def _rows(ai, bi):
    """Yield the Levenshtein DP rows (row i = distances of a[:i] to every prefix of b).
    Insertions along a row are a running minimum: cur[j] = j + min_{k<=j}(cand[k] - k)."""
    m = len(bi)
    idx = np.arange(m + 1, dtype=np.int64)
    prev = idx.copy()
    yield prev
    for i in range(1, len(ai) + 1):
        cand = np.empty(m + 1, dtype=np.int64)
        cand[0] = i
        if m:
            cand[1:] = np.minimum(prev[1:] + 1, prev[:-1] + (bi != ai[i - 1]))
        prev = np.minimum.accumulate(cand - idx) + idx
        yield prev


def edit_distance(a, b):
    """Levenshtein distance between two sequences (no alignment). Bit-parallel (Myers /
    Hyyrö) with Python's big integers as the bit vectors: one pass over the longer sequence."""
    if len(a) > len(b):
        a, b = b, a
    m = len(a)
    if m == 0:
        return len(b)
    peq = {}
    for i, x in enumerate(a):
        peq[x] = peq.get(x, 0) | (1 << i)
    mask = (1 << m) - 1
    high = 1 << (m - 1)
    pv, mv, score = mask, 0, m
    for y in b:
        eq = peq.get(y, 0)
        xv = eq | mv
        xh = ((((eq & pv) + pv) & mask) ^ pv) | eq
        ph = (mv | ~(xh | pv)) & mask
        mh = pv & xh
        if ph & high:
            score += 1
        elif mh & high:
            score -= 1
        ph = ((ph << 1) | 1) & mask
        mh = (mh << 1) & mask
        pv = (mh | ~(xv | ph)) & mask
        mv = ph & xv
    return score


def align(ref, hyp):
    """Edit operations turning ref into hyp. -> (counts, ops) where counts has hits / sub / del /
    ins and ops is [(op, ref_index, hyp_index)] with op in "=", "S", "D", "I" (None for the
    missing side). Very long pairs skip the alignment (ops None, counts estimated)."""
    n, m = len(ref), len(hyp)
    if (n + 1) * (m + 1) > _MAX_CELLS:
        d = edit_distance(ref, hyp)
        ins = max(0, m - n)
        dele = max(0, n - m)
        sub = max(0, d - ins - dele)
        return {"hits": max(0, n - sub - dele), "sub": sub, "del": dele, "ins": ins}, None
    ai, bi = _encode(ref, hyp)
    D = np.vstack(list(_rows(ai, bi))) if n else np.arange(m + 1, dtype=np.int64)[None, :]
    i, j, ops = n, m, []
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            c = 0 if ai[i - 1] == bi[j - 1] else 1
            if D[i, j] == D[i - 1, j - 1] + c:
                ops.append(("=" if c == 0 else "S", i - 1, j - 1))
                i, j = i - 1, j - 1
                continue
        if i > 0 and D[i, j] == D[i - 1, j] + 1:
            ops.append(("D", i - 1, None))
            i -= 1
            continue
        ops.append(("I", None, j - 1))
        j -= 1
    ops.reverse()
    cnt = Counter(op for op, _, _ in ops)
    return {"hits": cnt["="], "sub": cnt["S"], "del": cnt["D"], "ins": cnt["I"]}, ops


def _rate(errors, n, hyp_n):
    if n:
        return errors / n
    return 0.0 if not hyp_n else 1.0


def wer_counts(ref, hyp, lang=None):
    """WER parts over units: {"wer", "errors", "sub", "del", "ins", "hits", "ref", "hyp"}."""
    r, h = text_units(ref, lang), text_units(hyp, lang)
    c, _ = align(r, h) if len(r) * len(h) <= 4_000_000 else (None, None)
    if c is None:
        d = edit_distance(r, h)
        return {"wer": _rate(d, len(r), len(h)), "errors": d, "ref": len(r), "hyp": len(h)}
    err = c["sub"] + c["del"] + c["ins"]
    return dict(c, wer=_rate(err, len(r), len(h)), errors=err, ref=len(r), hyp=len(h))


def wer(ref, hyp, lang=None):
    """Word error rate over units (CJK / kana / Thai characters count one each)."""
    return wer_counts(ref, hyp, lang)["wer"]


def cer_counts(ref, hyp, lang=None):
    r, h = chars(normalize(ref, lang)), chars(normalize(hyp, lang))
    d = edit_distance(r, h)
    return {"cer": _rate(d, len(r), len(h)), "errors": d, "ref": len(r), "hyp": len(h)}


def cer(ref, hyp, lang=None):
    """Character error rate on normalised text, spaces ignored."""
    return cer_counts(ref, hyp, lang)["cer"]


def primary_rate(lang, wer_value, cer_value):
    """CER for languages written without spaces, WER for the rest."""
    return cer_value if (lang or "").split("-")[0] in NO_SPACE_LANGS else wer_value


# -- chrF / chrF++ --------------------------------------------------------------------------

def _chrf_words(text):
    """sacrebleu's chrF++ word split: punctuation at a word's start or end becomes its own token."""
    out = []
    for w in (text or "").split():
        if len(w) == 1:
            out.append(w)
        elif unicodedata.category(w[-1])[0] == "P":
            out += [w[:-1], w[-1]]
        elif unicodedata.category(w[0])[0] == "P":
            out += [w[0], w[1:]]
        else:
            out.append(w)
    return out


def _ngrams(seq, n):
    return Counter(tuple(seq[i:i + n]) for i in range(len(seq) - n + 1))


def chrf_stats(ref, hyp, char_order=6, word_order=2):
    """[(n_hyp, n_ref, n_match)] per order: characters 1..char_order (whitespace removed),
    then words 1..word_order."""
    stats = []
    rc, hc = list("".join((ref or "").split())), list("".join((hyp or "").split()))
    for n in range(1, char_order + 1):
        r, h = _ngrams(rc, n), _ngrams(hc, n)
        stats.append((sum(h.values()), sum(r.values()), sum((h & r).values())))
    rw, hw = _chrf_words(ref), _chrf_words(hyp)
    for n in range(1, word_order + 1):
        r, h = _ngrams(rw, n), _ngrams(hw, n)
        stats.append((sum(h.values()), sum(r.values()), sum((h & r).values())))
    return stats


def chrf_from_stats(stats, beta=2.0):
    """sacrebleu's default chrF: precision and recall averaged over the orders that exist on
    both sides, then F-beta, x100."""
    prec = rec = 0.0
    eff = 0
    for n_hyp, n_ref, n_match in stats:
        if n_hyp > 0 and n_ref > 0:
            prec += n_match / n_hyp
            rec += n_match / n_ref
            eff += 1
    if not eff:
        return 0.0
    prec /= eff
    rec /= eff
    if prec + rec == 0:
        return 0.0
    f2 = beta * beta
    return 100.0 * (1 + f2) * prec * rec / (f2 * prec + rec)


def _word_order(lang, word_order):
    if word_order is not None:
        return word_order
    return 0 if (lang or "").split("-")[0] in NO_SPACE_LANGS else 2


def chrf(ref, hyp, lang=None, char_order=6, word_order=None, beta=2.0):
    """chrF++ (character 6-grams + word 2-grams, beta 2), 0..100, case-sensitive like
    sacrebleu. For ja / zh / th the word n-grams are left out (plain chrF): those texts have
    no spaces to split words on."""
    if not (ref or "").strip() and not (hyp or "").strip():
        return 100.0
    return chrf_from_stats(chrf_stats(ref, hyp, char_order, _word_order(lang, word_order)), beta)


def corpus_chrf(pairs, lang=None, char_order=6, word_order=None, beta=2.0):
    """chrF over many (ref, hyp) pairs: statistics summed first (sacrebleu's corpus score)."""
    total = None
    for ref, hyp in pairs:
        s = chrf_stats(ref, hyp, char_order, _word_order(lang, word_order))
        total = s if total is None else [tuple(a + b for a, b in zip(x, y)) for x, y in zip(total, s)]
    return chrf_from_stats(total, beta) if total else None


# -- summaries ------------------------------------------------------------------------------

def summary(values, digits=4):
    """{"n", "mean", "p50", "p90", "p95", "min", "max"} of the numbers given (None / NaN skipped)."""
    xs = []
    for v in (() if values is None else values):        # lists, tuples, numpy arrays, generators
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isnan(f):
            xs.append(f)
    if not xs:
        return {"n": 0, "mean": None, "p50": None, "p90": None, "p95": None, "min": None, "max": None}
    a = np.asarray(xs, dtype=np.float64)

    def r(x):
        return round(float(x), digits)
    return {"n": len(xs), "mean": r(a.mean()), "p50": r(np.percentile(a, 50)), "p90": r(np.percentile(a, 90)),
            "p95": r(np.percentile(a, 95)), "min": r(a.min()), "max": r(a.max())}


stats = summarize = summary


def bag_scores(ref_units, hyp_units):
    """Order-free match of two unit lists: precision, recall, F1 of the multiset overlap."""
    r, h = Counter(ref_units), Counter(hyp_units)
    hit = sum((r & h).values())
    p = hit / sum(h.values()) if h else (1.0 if not r else 0.0)
    rec = hit / sum(r.values()) if r else (1.0 if not h else 0.0)
    f = 2 * p * rec / (p + rec) if p + rec else 0.0
    return {"precision": round(p, 4), "recall": round(rec, 4), "f1": round(f, 4), "hits": hit}


# -- a transcript against the timeline ------------------------------------------------------

def _turn_langs(t):
    langs = t.get("langs") or [t.get("lang")]
    return [x for x in langs if x] or [t.get("lang") or "?"]


def _has_timings(words):
    """True when most words carry start/end times (in seconds) that actually move."""
    if not words or not all(isinstance(w, dict) for w in words):
        return False
    spoken = [w for w in words if str(w.get("text") or "").strip()]
    timed = [w for w in spoken if w.get("start") is not None and w.get("end") is not None]
    if not spoken or len(timed) < 0.8 * len(spoken):
        return False
    try:
        starts = [float(w["start"]) for w in timed]
        ends = [float(w["end"]) for w in timed]
    except (TypeError, ValueError):
        return False
    if any(math.isnan(x) for x in starts + ends):
        return False
    return len(timed) < 4 or max(starts) - min(starts) > 0.5


def _assign_by_time(spans, words, tol, other_spans=()):
    """spans: [(start, end, unit bag)]. Each word goes to the turn it overlaps (within tol);
    when several turns overlap it (cross-talk), the one whose text has the word wins, then
    the larger overlap. A word none of its overlapping turns says, but an overlapping
    other_spans turn does (the call leaking into the mic), stays unassigned so it counts as
    echo. -> ({turn index: [units]}, [unassigned (time, units)])"""
    got = {k: [] for k in range(len(spans))}
    left = []
    timed = []
    for w in words:
        u = text_units(str(w.get("text") or ""))
        if not u or w.get("start") is None:
            continue
        ws = float(w["start"])
        we = max(ws, float(w.get("end") if w.get("end") is not None else ws))
        timed.append((ws, we, u))
    timed.sort(key=lambda x: x[0])
    for ws, we, u in timed:
        best, best_key = None, None
        for k, (s, e, bag) in enumerate(spans):
            ov = min(we, e + tol) - max(ws, s - tol)
            if ov < 0:
                continue
            inbag = sum(1 for x in u if x in bag) / len(u)
            key = (inbag, ov)
            if best_key is None or key > best_key:
                best, best_key = k, key
        if best is not None and best_key[0] == 0 and any(
                min(we, e + tol) - max(ws, s - tol) >= 0 and any(x in bag for x in u) for s, e, bag in other_spans):
            best = None                       # someone else's words heard in this track: echo
        if best is None:
            left.append((ws, u))
        else:
            got[best].extend(u)
    return got, left


def _aggregate(rows, lang=None):
    ref_u = sum(r["ref_units"] for r in rows)
    err = sum(r["errors"] for r in rows)
    ref_c = sum(r["ref_chars"] for r in rows)
    cerr = sum(r["char_errors"] for r in rows)
    w = err / ref_u if ref_u else None
    c = cerr / ref_c if ref_c else None
    return {"n_turns": len(rows), "ref_units": ref_u, "errors": err, "wer": _r(w), "ref_chars": ref_c,
            "char_errors": cerr, "cer": _r(c), "primary": _r(primary_rate(lang, w, c)) if lang else _r(w),
            "dropped_turns": sum(1 for r in rows if r.get("dropped"))}


def _r(x, d=4):
    return None if x is None else round(float(x), d)


def score_transcript(turns, hyp_text, hyp_words=None, *, other_turns=(), time_tol=0.3):
    """Score one engine transcript of a whole track against the timeline turns it should hold.

    turns        timeline turns expected in this audio ({"i", "text", "lang", "langs", "start",
                 "end", "speaker"}); other_turns are turns NOT expected in it (e.g. the call's
                 turns for the mic track): words of theirs that show up are counted as echo.
    hyp_text     the engine's text; hyp_words its words with start/end (optional).

    -> {"overall": {wer, cer, sub, del, ins, ref_units, hyp_units, ref_chars, char_errors,
        bag: {precision, recall, f1}, echo_units, echo_rate, dropped_turns},
        "method": "timing" | "alignment", "per_turn": [...], "by_lang": {lang: {...}},
        "mixed": {...}, "single": {...}}"""
    tl = sorted([t for t in turns if (t.get("text") or "").strip()], key=lambda t: float(t.get("start") or 0))
    ref_norm = [normalize(spoken_text(t["text"]), t.get("lang")) for t in tl]
    ref_units, owner = [], []
    for k, nt in enumerate(ref_norm):
        u = units(nt)
        ref_units += u
        owner += [k] * len(u)
    hyp_norm = normalize(hyp_text or "")
    hyp_units = units(hyp_norm)

    counts, ops = align(ref_units, hyp_units)
    errors = counts["sub"] + counts["del"] + counts["ins"]
    ref_chars = chars(" ".join(ref_norm))
    char_err = edit_distance(ref_chars, chars(hyp_norm))
    bag = bag_scores(ref_units, hyp_units)

    # which hyp units belong to which turn
    method = "timing" if _has_timings(hyp_words) else "alignment"
    per_hyp = {k: [] for k in range(len(tl))}
    unassigned = []
    others = [t for t in other_turns if (t.get("text") or "").strip()]
    other_spans = [(float(t.get("start") or 0), float(t.get("end") or 0), set(text_units(spoken_text(t["text"]))))
                   for t in others]
    if method == "timing":
        spans = [(float(t.get("start") or 0), float(t.get("end") or 0), set(units(n))) for t, n in zip(tl, ref_norm)]
        per_hyp, unassigned = _assign_by_time(spans, hyp_words, time_tol, other_spans)
    elif ops is not None:
        last = 0
        for op, ri, hi in ops:
            if ri is not None:
                last = owner[ri]
            if hi is not None:
                if op == "I" and ri is None and not owner:
                    unassigned.append((0.0, [hyp_units[hi]]))
                    continue
                per_hyp[last if op == "I" else owner[ri]].append(hyp_units[hi])
        # insertions before the first ref unit were given to turn 0 via last = 0
    else:                                         # too long to align: spread by length
        for k in range(len(tl)):
            per_hyp[k] = []
        method = "none"

    per_turn = []
    for k, t in enumerate(tl):
        ru = units(ref_norm[k])
        hu = per_hyp.get(k, [])
        d = edit_distance(ru, hu)
        rc = chars(ref_norm[k])
        dc = edit_distance(rc, chars(" ".join(hu)))
        rec = bag_scores(ru, hu)["recall"]
        langs = _turn_langs(t)
        per_turn.append({
            "i": t.get("i", k), "speaker": t.get("speaker"), "lang": t.get("lang") or langs[0], "langs": langs,
            "mixed": len(set(langs)) > 1, "start": t.get("start"), "end": t.get("end"),
            "ref_units": len(ru), "errors": d, "wer": _r(_rate(d, len(ru), len(hu))),
            "ref_chars": len(rc), "char_errors": dc, "cer": _r(_rate(dc, len(rc), len(hu))),
            "recall": rec, "dropped": len(ru) >= 2 and rec < 0.2, "hyp": " ".join(hu),
        })

    # echo: words of turns that should not be in this audio
    echo = 0
    if others:
        expected_bag = set(ref_units)
        if method == "timing":
            for ws, u in unassigned:
                for s, e, bag_o in other_spans:
                    if s - time_tol <= ws <= e + time_tol:
                        echo += sum(1 for x in u if x in bag_o)
                        break
        elif ops is not None:
            other_bag = set().union(*[b for _, _, b in other_spans]) if other_spans else set()
            echo = sum(1 for op, _, hi in ops if op == "I" and hyp_units[hi] in other_bag
                       and hyp_units[hi] not in expected_bag)

    by_lang = {}
    for row in per_turn:
        by_lang.setdefault(row["lang"], []).append(row)
    overall = {
        "wer": _r(_rate(errors, len(ref_units), len(hyp_units))), "errors": errors,
        "sub": counts["sub"], "del": counts["del"], "ins": counts["ins"], "hits": counts["hits"],
        "ref_units": len(ref_units), "hyp_units": len(hyp_units),
        "cer": _r(_rate(char_err, len(ref_chars), len(hyp_units))), "char_errors": char_err,
        "ref_chars": len(ref_chars), "bag": bag, "echo_units": echo,
        "echo_rate": _r(echo / len(ref_units)) if ref_units else None,
        "dropped_turns": sum(1 for r in per_turn if r["dropped"]),
        "unassigned_units": sum(len(u) for _, u in unassigned),
    }
    # turn by turn: errors inside each turn + words that belong to no turn. Order between turns does
    # not count, so cross-talk an engine writes in another order is not punished twice.
    overall["turn_errors"] = sum(r["errors"] for r in per_turn) + overall["unassigned_units"]
    overall["wer_turns"] = _r(_rate(overall["turn_errors"], len(ref_units), len(hyp_units)))
    mixed_rows = [r for r in per_turn if r["mixed"]]
    single_rows = [r for r in per_turn if not r["mixed"]]
    return {
        "overall": overall, "method": method, "per_turn": per_turn,
        "by_lang": {lang: _aggregate(rows, lang) for lang, rows in sorted(by_lang.items())},
        "mixed": _aggregate(mixed_rows) if mixed_rows else None,
        "single": _aggregate(single_rows) if single_rows else None,
    }


score_timeline = score_transcript


def aggregate_turns(rows, lang=None):
    """Sum per-turn rows (from score_transcript) into one {wer, cer, primary, ...}."""
    return _aggregate(list(rows), lang)
