"""Telling the people on a call apart by one voice print per stretch of speech.

The speaker pass after a meeting's notes (meeting_speakers.name_the_call) has to turn
"Them" into "Speaker 1, 2 ..." from the call's own sound. The first engine for that
slid a 10 second window along the whole sound, one window per second, and embedded each
window again: about 1500 windows for a 55 minute call, 47 minutes on a busy computer,
nothing to show for the first five of them. The words are already known: segments.json
keeps where every piece of the call starts and ends. So this engine takes one voice
print (the speaker model's embedding) per stretch of speech instead - 299 pieces for the
same call, about two minutes - and groups the prints.

This module holds only the decisions, in plain Python (numpy is used for the
similarities when it is really there, never required), so all of them are tested on every
CI run; the model itself (diarization.embed_spans, in a child process) only hands the
vectors over.

  * ``units_for(segments)``: the stretches to embed. The call's own segments (never the
    user's), in time order; neighbours within JOIN_GAP_SEC are joined while the stretch is
    under MIN_UNIT_SEC (a voice print of a second or two is noisy); one over MAX_UNIT_SEC
    is cut into equal pieces; a lone stretch under TINY_SEC is left out (its words are
    given to the nearest voice afterwards, speaker_labels.attribute); at most MAX_UNITS
    are kept, spread over the whole call.
  * ``cluster(vectors, durations)``: average-linkage clustering on cosine similarity
    (``average_linkage``, the nearest-neighbour-chain algorithm: O(n^2)), then a cluster
    of fewer than MIN_CLUSTER_UNITS pieces or under MIN_CLUSTER_SEC joins the most similar
    real one (a cough or a half-word is not a person) - when there is no real one the call
    is one voice - and at most MAX_SPEAKERS voices are kept.
  * ``turns_for(units, ids, offset)``: the turns speaker_labels.attribute works from.

THRESHOLD is where two groups of voice prints stop being one voice. 0.35 comes from the
one real call it was measured on: a single known person (the user's own microphone, 444
pieces) stays ONE group up to 0.40 and splits from 0.45; two known different people never
mix between 0.30 and 0.45; the call's result is the same from 0.30 to 0.45. One call is
thin evidence, so the value lives here, in one place.

GUSAN_SPEAKER_ENGINE=windows brings the old sliding-window diarizer back (for comparing
the two in the field). Qt-free.
"""
import math
import os

THRESHOLD = 0.35            # average cosine similarity under which two groups are different voices
LOWEST_THRESHOLD = 0.25     # the cap on voices lowers the threshold in steps of 0.05 down to this
MIN_UNIT_SEC = 4.0          # a voice print of less than this is noisy: join neighbours up to it
MAX_UNIT_SEC = 8.0          # a longer stretch is cut into equal pieces
JOIN_GAP_SEC = 0.8          # neighbours this close are one stretch
TINY_SEC = 1.5              # a lone stretch shorter than this is not embedded
MIN_CLUSTER_UNITS = 2       # a voice needs at least this many pieces ...
MIN_CLUSTER_SEC = 10.0      # ... and this many seconds, else it is folded into the nearest voice
MAX_SPEAKERS = 8
MAX_UNITS = 1500            # most voice prints for one recording part (a 2 hour call is about 1400)

ENV_ENGINE = "GUSAN_SPEAKER_ENGINE"
ENGINE_VOICES = "voices"
ENGINE_WINDOWS = "windows"


def engine(environ=None):
    """"voices" (one voice print per stretch, the default) or "windows" (the old sliding-window
    diarizer, GUSAN_SPEAKER_ENGINE=windows)."""
    environ = os.environ if environ is None else environ
    value = str(environ.get(ENV_ENGINE, "") or "").strip().lower()
    return ENGINE_WINDOWS if value == ENGINE_WINDOWS else ENGINE_VOICES


# ── the stretches to embed ───────────────────────────────────────────────────

def _span_of(seg):
    """(start, end) of a segment in seconds, or None when it has no usable length."""
    try:
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
    except (TypeError, ValueError, AttributeError):
        return None
    if not (math.isfinite(start) and math.isfinite(end)):
        return None
    start = max(0.0, start)
    if end <= start:
        return None
    return start, end


def units_for(segments, max_units=MAX_UNITS):
    """The stretches of the call to embed, [(start_s, end_s)] in time order (see the module
    notes for the rules). ``segments``: dicts with start / end and, for the user's own, a
    "source" of "you" - those are never used."""
    spans = []
    for seg in segments or []:
        try:
            if seg.get("source") == "you":
                continue
        except AttributeError:
            continue
        span = _span_of(seg)
        if span is not None:
            spans.append(span)
    spans.sort()
    joined = []
    for start, end in spans:
        if joined and (start - joined[-1][1]) <= JOIN_GAP_SEC and (joined[-1][1] - joined[-1][0]) < MIN_UNIT_SEC:
            joined[-1][1] = max(joined[-1][1], end)
        else:
            joined.append([start, end])
    units = []
    for start, end in joined:
        length = end - start
        if length < TINY_SEC:
            continue
        pieces = max(1, int(math.ceil(length / MAX_UNIT_SEC - 1e-9)))
        step = length / pieces
        for k in range(pieces):
            units.append((start + k * step, end if k == pieces - 1 else start + (k + 1) * step))
    max_units = max(1, int(max_units or MAX_UNITS))
    if len(units) > max_units:
        every = len(units) / float(max_units)
        units = [units[int(i * every)] for i in range(max_units)]
    return units


# ── similarity ───────────────────────────────────────────────────────────────

def _numpy():
    """The real numpy, or None (it is not there, or a test has stood a stub in for it)."""
    try:
        import numpy as np
        probe = np.asarray([[3.0, 4.0]], dtype=np.float64)
        if float(np.linalg.norm(probe)) == 5.0 and isinstance(probe.tolist(), list):
            return np
    except Exception:
        pass
    return None


def _cosine_pure(vectors):
    unit = []
    for v in vectors:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        unit.append([x / norm for x in v])
    n = len(unit)
    sim = [[1.0] * n for _ in range(n)]
    for i in range(n):
        vi = unit[i]
        row = sim[i]
        for j in range(i + 1, n):
            row[j] = sim[j][i] = sum(map(float.__mul__, vi, unit[j]))
    return sim


def cosine_matrix(vectors):
    """The cosine similarity of every pair of ``vectors`` as a list of lists (the diagonal is
    1.0). numpy does the work when it is really there; any trouble with it falls back to plain
    Python (same numbers)."""
    vectors = [[float(x) for x in v] for v in vectors]
    n = len(vectors)
    if n == 0:
        return []
    np = _numpy()
    if np is not None:
        try:
            m = np.asarray(vectors, dtype=np.float64)
            norms = np.linalg.norm(m, axis=1, keepdims=True)
            norms[norms == 0.0] = 1.0
            m = m / norms
            sim = (m @ m.T).tolist()
            if len(sim) == n and all(len(r) == n for r in sim):
                for i in range(n):
                    sim[i][i] = 1.0
                return sim
        except Exception:
            pass
    return _cosine_pure(vectors)


def _renumber(labels):
    """Labels as 0, 1, 2 ... in order of first appearance."""
    seen, out = {}, []
    for label in labels:
        out.append(seen.setdefault(label, len(seen)))
    return out


def average_linkage(sim, threshold):
    """Group items whose AVERAGE pairwise similarity stays at or above ``threshold`` (UPGMA on
    a similarity matrix). Returns a cluster id per item, 0, 1, 2 ... in order of first appearance.

    The nearest-neighbour chain: follow "most similar" links until two items point at each
    other and merge them - O(n^2) instead of the O(n^3) of always searching the best pair.
    Average linkage is reducible (a merge never makes a pair more similar than the pair that
    was merged), so every merge at or above the threshold is the same whatever the order and
    they are applied afterwards with a union-find (tests: the same partitions as the brute
    force search)."""
    n = len(sim)
    if n == 0:
        return []
    sim = [list(row) for row in sim]
    size = [1] * n
    active = set(range(n))
    merges = []
    chain = []
    while len(active) > 1:
        if not chain:
            chain.append(min(active))
        while True:
            a = chain[-1]
            prev = chain[-2] if len(chain) > 1 else -1
            best, best_s = -1, float("-inf")
            row = sim[a]
            for b in active:
                if b == a:
                    continue
                s = row[b]
                if s > best_s or (s == best_s and b == prev):
                    best, best_s = b, s
            if best == prev:
                break
            chain.append(best)
        b = chain.pop()
        a = chain.pop()
        merges.append((a, b, best_s))
        na, nb = size[a], size[b]
        ra, rb = sim[a], sim[b]
        for c in active:
            if c != a and c != b:
                v = (na * ra[c] + nb * rb[c]) / (na + nb)
                ra[c] = v
                sim[c][a] = v
        size[a] = na + nb
        active.discard(b)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b, s in merges:
        if s >= threshold:
            parent[find(b)] = find(a)
    return _renumber([find(i) for i in range(n)])


# ── folding small groups in ──────────────────────────────────────────────────

def _groups(labels):
    groups = {}
    for i, label in enumerate(labels):
        groups.setdefault(label, []).append(i)
    return groups


def _mean_similarity(sim, members, others):
    total = 0.0
    for i in members:
        row = sim[i]
        for j in others:
            total += row[j]
    return total / float(len(members) * len(others))


def _absorb_small(labels, sim, seconds):
    """A group of fewer than MIN_CLUSTER_UNITS pieces or under MIN_CLUSTER_SEC joins the most
    similar real group; with no real group everyone is one voice."""
    groups = _groups(labels)
    big = [g for g, members in groups.items()
           if len(members) >= MIN_CLUSTER_UNITS and sum(seconds[i] for i in members) >= MIN_CLUSTER_SEC]
    if not big:
        return [0] * len(labels)
    if len(big) == len(groups):
        return list(labels)
    out = list(labels)
    for g, members in groups.items():
        if g in big:
            continue
        target = max(big, key=lambda b: _mean_similarity(sim, members, groups[b]))
        for i in members:
            out[i] = target
    return _renumber(out)


def _merge_smallest(labels, sim, seconds, keep):
    """Fold the smallest group (by seconds) into the group most like it until ``keep`` are left."""
    labels = list(labels)
    while True:
        groups = _groups(labels)
        if len(groups) <= keep:
            return _renumber(labels)
        smallest = min(groups, key=lambda g: (sum(seconds[i] for i in groups[g]), -g))
        rest = [g for g in groups if g != smallest]
        target = max(rest, key=lambda g: _mean_similarity(sim, groups[smallest], groups[g]))
        for i in groups[smallest]:
            labels[i] = target


def cluster(vectors, durations, threshold=THRESHOLD, max_speakers=MAX_SPEAKERS):
    """A voice id per unit: ``vectors[i]`` is the voice print of unit i (None = it has none: the
    unit was silent or past the end of the sound), ``durations[i]`` its length in seconds. The
    result is parallel to ``vectors`` - 0, 1, 2 ... in order of first appearance, None where there
    was no voice print. See the module notes for the rules."""
    n = len(vectors)
    out = [None] * n
    keep = [i for i in range(n) if vectors[i] is not None and len(vectors[i])]
    if not keep:
        return out
    secs = []
    for i in keep:
        try:
            secs.append(max(0.0, float(durations[i])))
        except (TypeError, ValueError, IndexError):
            secs.append(0.0)
    sim = cosine_matrix([vectors[i] for i in keep])
    labels = _absorb_small(average_linkage(sim, threshold), sim, secs)
    cap = max(1, int(max_speakers))
    thr = float(threshold)
    while len(set(labels)) > cap and thr - 0.05 >= LOWEST_THRESHOLD - 1e-9:
        thr -= 0.05
        labels = _absorb_small(average_linkage(sim, thr), sim, secs)
    if len(set(labels)) > cap:
        labels = _merge_smallest(labels, sim, secs, cap)
    for i, label in zip(keep, labels):
        out[i] = label
    return out


def turns_for(units, ids, offset=0.0):
    """[(start, end, voice id)] for speaker_labels.attribute: every unit that has a voice,
    moved ``offset`` seconds along the meeting's timeline (a later recording part)."""
    turns = []
    for (start, end), voice in zip(units, ids):
        if voice is None:
            continue
        turns.append((float(start) + offset, float(end) + offset, int(voice)))
    return turns


def shares(units, ids):
    """Each voice's share of the speech, biggest first: [0.95, 0.05] (numbers for the log)."""
    total, per = 0.0, {}
    for (start, end), voice in zip(units, ids):
        if voice is None:
            continue
        length = max(0.0, end - start)
        per[voice] = per.get(voice, 0.0) + length
        total += length
    if total <= 0.0:
        return []
    return sorted((round(v / total, 2) for v in per.values()), reverse=True)
