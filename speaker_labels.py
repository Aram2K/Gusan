"""Who spoke each line of a mic + system sound meeting, from WHERE the sound came from.

In a "mic + system sound" meeting Gusan keeps two tracks: the microphone (the
user, and the room) and the system sound (everyone on the call). After Stop the
transcript is labelled by SOURCE first:

  * a segment whose speech is on the microphone  -> "You"
  * a segment whose speech is on the system sound -> a person on the call; the
    system track alone is then diarized into "Speaker 1..N" (diarization.py),
    so the user is never an anonymous "Speaker N" again.

Echo-aware: without headphones the microphone also hears the call. The echo is
a quieter copy of the system sound, so the labeller learns how loud that copy
is (the coupling, k = mic level / system level while the call plays) and calls
a frame "you" only when the mic is clearly louder than that echo would be. A
hysteresis keeps a label from flickering when the two are close, and a short
smoothing window drops isolated blips.

The two tracks are not alike, and the labeller treats them that way:

  * the system track is DIGITAL: nothing in it but what was played, so "the call
    is making sound" is a fixed level, never a percentile of the track itself
    (a video or music that plays all the time has no quiet floor to learn);
  * the microphone has a room-noise floor, learned from the stretches where the
    call is silent (with the call playing all the time, any echo would be
    mistaken for the floor);
  * the echo of the call reaches the microphone LATE (the speakers, the room, a
    block of the recorder): a frame just after the call stopped is still its
    echo, not the user, so the call's level is the loudest of the last
    ECHO_LAG_FRAMES, not only the present frame.

Pure Python after the frame energies: every decision works on plain lists, so
the logic is testable without numpy (frame_rms is the only numpy part) and
never raises on odd input - a failing labeller just means "no source labels".
"""
import collections
import logging

logger = logging.getLogger("transcribe")

SAMPLE_RATE = 16000
HOP_SEC = 0.05              # one energy value per 50 ms

# Speaker values in labelled segments (diarization speaker ids are >= 0).
YOU = -1                    # the user (the microphone)
THEM = -2                   # the call, people not told apart

SRC_YOU = "you"
SRC_THEM = "them"

# A frame is "active" above max(NOISE_MULT x the track's own noise floor, ABS_MIN).
NOISE_MULT = 4.0
ABS_MIN = 0.004             # about -48 dBFS: below this is room hiss, not speech
SYS_ABS_MIN = 0.002         # the system track is digital: anything above ~-54 dBFS is played sound
QUIET_MIN_FRAMES = 40       # 2 s of call silence to learn the microphone's own floor from
ECHO_LAG_FRAMES = 6         # 0.3 s: the call reaches the microphone this much later than the track
NOISE_PERCENTILE = 0.10
# Echo coupling (mic level / system level while the call plays), clamped.
K_MIN = 0.03
K_MAX = 1.5
K_DEFAULT = 0.15
K_PERCENTILE = 0.25
K_MIN_FRAMES = 20           # frames of steady call sound needed to learn the coupling
# Both active: the mic must beat the learned echo by HI to become "you", the
# system by LO to go back to "them"; in between the previous frame's label stays.
MARGIN_HI = 2.5
MARGIN_LO = 1.5
SMOOTH_FRAMES = 5           # majority over this many active frames (0.25 s)
# A segment whose you/them split is this close to even keeps the previous label.
TIE_BAND = 0.1
# A segment with no active frame is read by level only when a source is at least
# this many times above its OWN noise floor; below that it is silence and keeps
# the previous label.
SIGNAL_RATIO = 2.0
FLOOR_MIN = 0.0005


# ── the numpy part: one RMS per hop ──────────────────────────────────────────

def frame_rms(track, sr=SAMPLE_RATE, hop_sec=HOP_SEC):
    """RMS (0..1) of each ``hop_sec`` frame of ``track`` (int16 PCM or float
    samples) as a list of floats. The last, shorter frame counts too. Works in
    blocks, so a two-hour track costs a few MB of temporary memory."""
    import numpy as np
    a = np.asarray(track)
    n = int(a.shape[0]) if a.ndim else 0
    hop = max(1, int(round(sr * hop_sec)))
    if n == 0:
        return []
    scale = 1.0 / 32767.0 if a.dtype.kind in "iu" else 1.0
    full = n // hop
    out = []
    block = 2048                                    # frames per block
    for f0 in range(0, full, block):
        f1 = min(full, f0 + block)
        x = a[f0 * hop:f1 * hop].astype(np.float32)
        if scale != 1.0:
            x *= np.float32(scale)
        out.extend(np.sqrt(np.mean(x.reshape(f1 - f0, hop) ** 2, axis=1)).tolist())
    tail = a[full * hop:]
    if len(tail):
        x = tail.astype(np.float32) * np.float32(scale)
        out.append(float(np.sqrt(np.mean(x ** 2))))
    return out


# ── activity, coupling, per-frame state ──────────────────────────────────────

def _percentile(sorted_values, p):
    if not sorted_values:
        return 0.0
    i = min(len(sorted_values) - 1, max(0, int(p * len(sorted_values))))
    return sorted_values[i]


def noise_floor(rms):
    """A track's own noise floor: its 10th-percentile frame level."""
    return _percentile(sorted(float(v) for v in rms), NOISE_PERCENTILE)


def activity_threshold(rms):
    """The level above which a frame of this track counts as sound: 4x its own
    noise floor, never below ABS_MIN."""
    return max(NOISE_MULT * noise_floor(rms), ABS_MIN)


def _finite(v):
    """A level as a plain non-negative float (NaN / negative -> 0, inf -> 1)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v < 0.0:
        return 0.0
    return 1.0 if v == float("inf") else v


def peak_before(values, lag=ECHO_LAG_FRAMES):
    """For every frame the largest of values[i - lag .. i] (a sliding maximum,
    one pass): how loud the call was in the last ``lag`` frames."""
    out = []
    window = collections.deque()
    for i, v in enumerate(values):
        while window and values[window[-1]] <= v:
            window.pop()
        window.append(i)
        if window[0] < i - lag:
            window.popleft()
        out.append(values[window[0]])
    return out


def system_threshold(system=None):
    """The level above which the call is making sound. A fixed level: the
    system track is digital silence when nothing plays, and a track that plays
    all the time (a video, music under the speech) must not read its own
    content as its "noise floor"."""
    return SYS_ABS_MIN


def mic_threshold(mic, system, thr_sys=None):
    """The level above which the microphone carries sound: 4x its room-noise
    floor, learned from the frames where the call is silent (so the call's echo
    never counts as room noise); with fewer than QUIET_MIN_FRAMES of those the
    floor cannot be told from the echo and only ABS_MIN applies - the echo test
    then keeps the call out of "you"."""
    thr_sys = system_threshold(system) if thr_sys is None else thr_sys
    quiet = [m for m, s in zip(mic, system) if s <= thr_sys]
    if len(quiet) < QUIET_MIN_FRAMES:
        return ABS_MIN
    return max(NOISE_MULT * noise_floor(quiet), ABS_MIN)


def echo_coupling(mic, system, thr_sys=None, ref=None):
    """k: how loud the microphone is next to the call while the call plays and
    the user is (mostly) quiet. A low percentile of mic / call-level over the
    frames of STEADY call sound (the call has been playing for more than
    ECHO_LAG_FRAMES frames: the start of a sound has not reached the
    microphone yet), whatever the microphone carries - so the user talking over
    the call does not inflate it, and with headphones (nothing but room noise
    on the microphone) it lands on K_MIN. K_DEFAULT when there are too few
    frames."""
    thr_sys = system_threshold(system) if thr_sys is None else thr_sys
    ref = peak_before(system) if ref is None else ref
    ratios = []
    run = 0
    for m, s, r in zip(mic, system, ref):
        run = run + 1 if s > thr_sys else 0
        if run > ECHO_LAG_FRAMES:
            ratios.append(m / r)
    if len(ratios) < K_MIN_FRAMES:
        return K_DEFAULT
    ratios.sort()
    return min(K_MAX, max(K_MIN, _percentile(ratios, K_PERCENTILE)))


def _pad(values, n):
    values = [_finite(v) for v in values]
    if len(values) < n:
        values.extend([0.0] * (n - len(values)))
    return values


NONE, YOU_F, THEM_F = 0, 1, 2


def classify_frames(mic, system, k=None, thr_mic=None, thr_sys=None):
    """A label per frame: 0 nothing, 1 you (the mic), 2 them (the call).
    Returns (labels, info) with the thresholds and coupling it used."""
    n = max(len(mic), len(system))
    mic, system = _pad(mic, n), _pad(system, n)
    thr_sys = system_threshold(system) if thr_sys is None else thr_sys
    thr_mic = mic_threshold(mic, system, thr_sys) if thr_mic is None else thr_mic
    ref = peak_before(system)              # the call's level over the last 0.3 s
    k = echo_coupling(mic, system, thr_sys, ref) if k is None else k
    labels = [NONE] * n
    state = NONE
    for i in range(n):
        m, s, r = mic[i], system[i], ref[i]
        m_act, s_act = m > thr_mic, s > thr_sys
        if m_act and not s_act:
            # The call just stopped: what the microphone still carries is its
            # echo unless it is clearly louder than that echo would be.
            if r > thr_sys and m < MARGIN_HI * k * r:
                cur = NONE
            else:
                cur = YOU_F
        elif s_act and not m_act:
            cur = THEM_F
        elif m_act and s_act:
            expected_echo = max(k * r, 1e-9)
            ratio = m / expected_echo
            if ratio >= MARGIN_HI:
                cur = YOU_F
            elif ratio <= MARGIN_LO:
                cur = THEM_F
            else:
                cur = state if state != NONE else (YOU_F if ratio >= (MARGIN_HI + MARGIN_LO) / 2
                                                   else THEM_F)
        else:
            cur = NONE
        labels[i] = cur
        if cur != NONE:
            state = cur
    return labels, {"k": k, "thr_mic": thr_mic, "thr_sys": thr_sys}


def smooth_labels(labels, width=SMOOTH_FRAMES):
    """Majority label over ``width`` neighbouring ACTIVE frames (silence is
    transparent): a 50 ms blip inside a long turn takes the turn's label, while
    a real change of speaker (a quarter second or more) survives."""
    active = [i for i, v in enumerate(labels) if v != NONE]
    if width <= 1 or len(active) < 3:
        return list(labels)
    half = width // 2
    out = list(labels)
    vals = [labels[i] for i in active]
    ones = [0]
    for v in vals:
        ones.append(ones[-1] + (1 if v == YOU_F else 0))
    last = len(vals)
    for j, i in enumerate(active):
        lo, hi = max(0, j - half), min(last, j + half + 1)
        you_n = ones[hi] - ones[lo]
        them_n = (hi - lo) - you_n
        if you_n > them_n:
            out[i] = YOU_F
        elif them_n > you_n:
            out[i] = THEM_F
    return out


# ── segments ────────────────────────────────────────────────────────────────

def _frames_of(seg, hop, n):
    try:
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
    except (TypeError, ValueError, AttributeError):
        return 0, 0
    if not (start == start and end == end):          # NaN
        return 0, 0
    a = max(0, int(start / hop))
    b = min(n, int(end / hop) + 1)
    return a, max(a, b)


def label_segments(segments, mic_rms, sys_rms, hop_sec=HOP_SEC):
    """The source of each transcript segment: a list parallel to ``segments`` of
    dicts {"source": "you" | "them", "you_share": 0..1, "basis": ...}.

    ``mic_rms`` / ``sys_rms``: frame_rms() of the two tracks. Never raises: a
    segment that cannot be placed gets "them" with basis "none"."""
    mic, system = list(mic_rms or []), list(sys_rms or [])
    n = max(len(mic), len(system))
    out = []
    if n == 0:
        return [{"source": SRC_THEM, "you_share": 0.0, "basis": "none"} for _ in segments]
    mic, system = _pad(mic, n), _pad(system, n)
    labels, info = classify_frames(mic, system)
    labels = smooth_labels(labels)
    thr_mic, thr_sys, k = info["thr_mic"], info["thr_sys"], info["k"]
    # (the call's track has no noise floor of its own: see system_threshold)
    quiet = [m for m, s in zip(mic, system) if s <= thr_sys]
    floor_mic = max(noise_floor(quiet if len(quiet) >= QUIET_MIN_FRAMES else mic), FLOOR_MIN)
    floor_sys = FLOOR_MIN
    prev = None
    for seg in segments:
        a, b = _frames_of(seg, hop_sec, n)
        you_n = sum(1 for v in labels[a:b] if v == YOU_F)
        them_n = sum(1 for v in labels[a:b] if v == THEM_F)
        basis = "frames"
        if you_n + them_n == 0:
            # Nothing above the activity thresholds (very quiet speech, or
            # timestamps off): compare how far each source rises over its own
            # floor, the mic discounted by the echo it would carry anyway.
            if b > a:
                mic_avg, sys_avg = sum(mic[a:b]) / (b - a), sum(system[a:b]) / (b - a)
                mic_lvl, sys_lvl = mic_avg / max(thr_mic, 1e-9), sys_avg / max(thr_sys, 1e-9)
                basis = "level"
                if mic_avg < SIGNAL_RATIO * floor_mic and sys_avg < SIGNAL_RATIO * floor_sys:
                    src = prev or SRC_THEM
                    basis = "inherit" if prev else "none"
                else:
                    src = SRC_YOU if mic_lvl > sys_lvl * max(k, 0.5) * MARGIN_HI else SRC_THEM
                share = 1.0 if src == SRC_YOU else 0.0
            else:
                src, share, basis = prev or SRC_THEM, 0.0, "none"
        else:
            share = you_n / float(you_n + them_n)
            if abs(share - 0.5) <= TIE_BAND and prev:
                src = prev
                basis = "hysteresis"
            else:
                src = SRC_YOU if share > 0.5 else SRC_THEM
        out.append({"source": src, "you_share": share, "basis": basis})
        prev = src
    return out


# ── putting it together ──────────────────────────────────────────────────────

def merge_minor_speakers(turns, min_sec=4.0, min_share=0.04, cap_sec=30.0):
    """Fold diarization over-splits back in: a "speaker" who talks for less than
    max(min_sec, min(min_share x all speech, cap_sec)) is taken to be another
    person's voice changing - or noise - and its turns go to the nearest turn of
    a speaker who talks enough. ``turns``: [(start, end, id)]. Returns a new,
    start-sorted list; unchanged when nobody is minor (or everybody is)."""
    turns = sorted((float(s), float(e), int(i)) for s, e, i in turns if e > s)
    if not turns:
        return []
    totals = {}
    for s, e, i in turns:
        totals[i] = totals.get(i, 0.0) + (e - s)
    speech = sum(totals.values())
    need = max(min_sec, min(min_share * speech, cap_sec))
    major = {i for i, t in totals.items() if t >= need}
    if not major or len(major) == len(totals):
        return turns
    out = []
    for idx, (s, e, i) in enumerate(turns):
        if i in major:
            out.append((s, e, i))
            continue
        best, gap = None, float("inf")
        for j in range(idx - 1, -1, -1):             # the previous major turn first
            ts, te, ti = turns[j]
            if ti in major:
                best, gap = ti, max(0.0, s - te)
                break
        for j in range(idx + 1, len(turns)):          # then the next one, if it is closer
            ts, te, ti = turns[j]
            if ti in major:
                g = max(0.0, ts - e)
                if g < gap:
                    best, gap = ti, g
                break
        out.append((s, e, best if best is not None else i))
    return out


# ── diarizing only the call's own sound ──────────────────────────────────────
# The system track is silent whenever nobody on the call talks, and the
# diarizer's cost grows with the audio it reads: so it is given only the stretches
# where the call made sound, joined with short gaps, and its turns are mapped back.

def active_spans(rms, hop_sec=HOP_SEC, thr=None, pad=0.4, bridge=1.0, min_len=0.3):
    """[(start_sec, end_sec)] where a track made sound: runs of frames above the
    activity threshold (shorter than ``min_len`` dropped), widened by ``pad`` on
    both sides, runs closer than ``bridge`` joined."""
    rms = list(rms)
    if not rms:
        return []
    thr = activity_threshold(rms) if thr is None else thr
    runs, start = [], None
    for i, v in enumerate(rms):
        if v > thr:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(rms)))
    total = len(rms) * hop_sec
    spans = []
    for a, b in runs:
        if (b - a) * hop_sec < min_len:
            continue
        lo, hi = max(0.0, a * hop_sec - pad), min(total, b * hop_sec + pad)
        if spans and lo - spans[-1][1] < bridge:
            spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
        else:
            spans.append((lo, hi))
    return spans


def compact_system(parts, sr=SAMPLE_RATE, gap_sec=0.3, hop_sec=HOP_SEC):
    """The stretches of the system track(s) where the call made sound, joined into
    ONE float32 stream for the diarizer. ``parts``: [(system samples - int16 or
    float, its frame_rms, its start in seconds on the meeting's joined
    timeline)]. Returns (audio or None, mapping) - mapping [(start, end, origin)]
    says that joined-stream seconds [start, end) are the meeting's seconds from
    ``origin`` on (see expand_turns)."""
    import numpy as np
    pieces, mapping, cursor = [], [], 0.0
    gap = np.zeros(int(gap_sec * sr), np.float32)
    for system, rms, offset in parts:
        if system is None:
            continue
        for a, b in active_spans(rms, hop_sec, thr=system_threshold()):
            i0, i1 = int(a * sr), int(b * sr)
            seg = np.asarray(system[i0:i1])
            if len(seg) < sr // 5:
                continue
            f = seg.astype(np.float32)
            if seg.dtype.kind in "iu":
                f *= np.float32(1.0 / 32767.0)
            if pieces:
                pieces.append(gap)
                cursor += gap_sec
            dur = len(f) / float(sr)
            pieces.append(f)
            mapping.append((cursor, cursor + dur, offset + i0 / float(sr)))
            cursor += dur
    if not pieces:
        return None, []
    return np.concatenate(pieces), mapping


def expand_turns(turns, mapping):
    """Speaker turns of the joined stream compact_system built, back on the
    meeting's own timeline: a turn that crosses a join is cut there, one that
    sits in a gap is dropped."""
    out = []
    for s, e, spk in turns:
        for c0, c1, origin in mapping:
            lo, hi = max(s, c0), min(e, c1)
            if hi > lo:
                out.append((origin + (lo - c0), origin + (hi - c0), spk))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def attribute(segments, mic_rms, sys_rms, turns, hop_sec=HOP_SEC, labels=None, offset=0.0):
    """Label every segment: {"speaker": YOU | a diarization id | THEM | None,
    "source": "you" | "them"} next to its start/end/text.

    ``turns`` come from diarizing the SYSTEM track only, so every id in them is
    somebody on the call. A "them" segment takes the speaker whose turn
    overlaps it most (the nearest turn when it sits in a gap); with no turns at
    all the call stays "Them". ``labels``: label_segments() already done;
    ``offset``: seconds to add to a segment's time before matching a turn (the
    segments of a later part of a meeting against the turns of all parts)."""
    import diarization
    if labels is None:
        labels = label_segments(segments, mic_rms, sys_rms, hop_sec)
    turns = merge_minor_speakers(turns) if turns else []
    them_idx = [i for i, lab in enumerate(labels) if lab["source"] == SRC_THEM]
    them_segments = [segments[i] for i in them_idx]
    attributed = diarization.attribute_segments(them_segments, turns, offset=offset) if turns else []
    speaker_of = {}
    for pos, i in enumerate(them_idx):
        spk = attributed[pos].get("speaker") if attributed else None
        speaker_of[i] = THEM if spk is None else spk
    out = []
    for i, seg in enumerate(segments):
        row = dict(seg)
        row["source"] = labels[i]["source"]
        row["speaker"] = YOU if labels[i]["source"] == SRC_YOU else speaker_of.get(i, THEM)
        out.append(row)
    return out


def speaker_names(extra=None):
    """The ``names`` for diarization.build_speaker_transcript: the user and the
    undivided call, plus any caller names ({diarization id: "Anna"})."""
    names = {YOU: "You", THEM: "Them"}
    names.update(extra or {})
    return names


def has_labels(text):
    """True when a transcript carries speaker labels ("Speaker 1:", "You:",
    "Them:" at the start of a line)."""
    import re
    return bool(re.search(r"(?m)^\s*(?:Speaker\s+(?:\d+|\?)|You|Them):\s", text or ""))
