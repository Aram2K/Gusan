"""voice_builder: own-voice pieces are taken only from calls, where the person talks and the call
is quiet (an in-person meeting never counts: everyone in the room is on the mic)."""
import random
import io
import wave

import numpy as np
import pytest

from interpreter import voice_builder as vb


def _real_numpy():
    try:
        return float(np.asarray([1.0, 2.0, 3.0]).mean()) == 2.0
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(autouse=True)
def _needs_real_numpy():
    """Checked when each test RUNS: other test files swap numpy for a stub during the run."""
    import sys
    for mod in (sys.modules.get("numpy"), np, vb.np):
        try:
            ok = float(mod.asarray([1.0, 2.0, 3.0]).mean()) == 2.0
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            pytest.skip("real numpy needed (suite-wide stub in test_core)")

RATE = 16000


def speechlike(seconds, amp=6000, seed=1):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 3 * t) ** 2
    return (rng.standard_normal(len(t)) * 0.2 * amp + np.sin(2 * np.pi * 180 * t) * amp * env).astype(np.int16)


def silence(seconds):
    return np.zeros(int(seconds * RATE), np.int16)


def write(path, a):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(a.tobytes())


def call(turns, theirs=1.5, gap=0.5, seed=10):
    """A call on headphones: my turns on the mic track, the other side's between them on the call
    track. Returns (mic, system)."""
    mic, system = [], []
    for i, mine in enumerate(turns):
        mic += [speechlike(mine, seed=seed + i), silence(gap + theirs + gap)]
        system += [silence(mine + gap), speechlike(theirs, seed=500 + i), silence(gap)]
    return np.concatenate(mic), np.concatenate(system)


def in_person(seconds, seed=40):
    """Two people at one table, 3 s turns each: both on the mic (the colleague a bit quieter)."""
    turns, i = [], 0
    while sum(len(t) for t in turns) < seconds * RATE:
        turns += [speechlike(3.0, amp=6000 if i % 2 == 0 else 3000, seed=seed + i), silence(0.5)]
        i += 1
    return np.concatenate(turns)[: int(seconds * RATE)]


def meeting(root, name, mic, system):
    d = root / name
    d.mkdir()
    write(d / "audio_part1.wav", mic)
    write(d / "audio_part1_mic.wav", mic)
    write(d / "audio_part1_system.wav", system)
    return d


def test_regions_skip_overlap_with_the_other_side():
    # me: 0-6 s and 10-16 s; them: 9-12 s (overlaps my second turn)
    mic = np.concatenate([speechlike(6), silence(4), speechlike(6, seed=2), silence(2)])
    system = np.concatenate([silence(9), speechlike(3, seed=3), silence(6)])
    regions = vb.own_voice_regions(mic, system)
    secs = [((s / RATE), (e / RATE)) for s, e in regions]
    assert secs[0][0] < 0.2 and 5.5 < secs[0][1] <= 6.1          # the clean first turn
    # the second turn is kept only after the other voice (+ guard) ended at 12 s
    assert all(s >= 12.2 or e <= 9.0 for s, e in secs[1:])


def test_long_talk_is_split_evenly_and_tiny_bits_dropped():
    mic = np.concatenate([speechlike(40), silence(1), speechlike(1.5, seed=4), silence(1),
                          speechlike(0.5, seed=5), silence(1)])
    regions = vb.own_voice_regions(mic)
    lens = [(e - s) / RATE for s, e in regions]
    assert all(vb.MIN_PIECE_S <= x <= vb.MAX_CLIP_S + 0.05 for x in lens)
    assert min(lens[:3]) > 13                                     # 40 s = 3 x ~13.3 s, no tiny tail
    assert sum(lens) > 40                                         # the 1.5 s bit kept, the 0.5 s one not


def test_every_sample_is_long_enough_for_elevenlabs(tmp_path):
    # many short turns (2-4 s), as in a real call: ElevenLabs refuses samples under 4.6 s
    mic, system = call([2.0 + (i % 3) for i in range(30)])
    meeting(tmp_path, "20261108_100000", mic, system)
    found = vb.find_own_voice(vb.meeting_folders(tmp_path))
    assert found.clips and found.seconds > 60
    for wav_bytes, secs, _folder in found.clips:
        assert secs >= vb.MIN_SAMPLE_S >= 4.6
        with wave.open(io.BytesIO(wav_bytes)) as w:
            assert w.getnframes() / w.getframerate() >= 4.6
        assert secs <= vb.MAX_CLIP_S + vb.JOIN_PAUSE_S + 0.05


def test_clipped_audio_is_rejected():
    loud = np.full(RATE * 4, 32767, np.int16)
    assert not vb.clean_enough(loud)
    assert vb.clean_enough(speechlike(4))
    assert not vb.clean_enough(speechlike(4, amp=200))


def test_find_own_voice_walks_meetings_newest_first(tmp_path):
    for name, turns in (("20261101_100000", 8), ("20261105_100000", 12)):
        meeting(tmp_path, name, *call([4.0] * turns))
    found = vb.find_own_voice(vb.meeting_folders(tmp_path), target_s=60)
    assert found.enough and found.seconds >= 60
    assert len(found.meetings) == 2
    assert found.clips[0][2].endswith("20261105_100000")           # newest first
    wav_bytes, secs, _folder = found.clips[0]
    assert wav_bytes[:4] == b"RIFF" and secs <= vb.MAX_CLIP_S + 0.05
    assert found.summary()["meetings"] == 2


def test_meeting_without_tracks_gives_nothing(tmp_path):
    d = tmp_path / "20261101_100000"
    d.mkdir()
    write(d / "audio_part1.wav", speechlike(30))
    found = vb.find_own_voice(vb.meeting_folders(tmp_path))
    assert found.seconds == 0 and not found.enough


def test_an_in_person_meeting_is_never_used(tmp_path):
    # a colleague at the same table: the call track is silent, so both voices are on the mic
    mic = in_person(90)
    meeting(tmp_path, "20261106_100000", mic, silence(90))
    found = vb.find_own_voice(vb.meeting_folders(tmp_path))
    assert found.seconds == 0 and not found.clips


def test_an_in_person_meeting_with_a_short_video_is_never_used(tmp_path):
    # 4 minutes at a table; the laptop played a 15 s video once (6 % of the call track)
    system = np.concatenate([silence(60), speechlike(15, seed=77), silence(165)])
    meeting(tmp_path, "20261106_100000", in_person(240), system)
    found = vb.find_own_voice(vb.meeting_folders(tmp_path))
    assert found.seconds == 0 and not found.clips


def test_only_the_minutes_of_a_live_call_are_used(tmp_path):
    # a 3-minute call, then the call ends and two people in the room keep talking for a minute
    mic, system = call([4.0] * 27)
    only_call = vb.find_own_voice([meeting(tmp_path, "a_call", mic, system)])
    room = in_person(60, seed=90)
    both = vb.find_own_voice([meeting(tmp_path, "b_call_then_room", np.concatenate([mic, room]),
                                      np.concatenate([system, silence(60)]))])
    assert only_call.seconds > 100
    assert both.seconds == pytest.approx(only_call.seconds)          # nothing from the room minute
    sys_rms = vb.frame_rms(np.concatenate([system, silence(60)]))
    live = vb.live_call(sys_rms)
    per_s = RATE / vb.FRAME
    assert live[: int(170 * per_s)].all() and not live[int(180 * per_s):].any()
    assert vb.live_call(vb.frame_rms(silence(120))) is None


def test_tracks_are_read_in_blocks_like_a_whole_read(tmp_path, monkeypatch):
    monkeypatch.setattr(vb, "BLOCK_S", 1.0)
    path = tmp_path / "t.wav"
    write(path, np.concatenate([speechlike(4.3), silence(3.0)]))
    assert np.allclose(vb.track_rms(path), vb.frame_rms(vb.read_wav(path)))


def test_a_stop_is_seen_inside_a_long_track(tmp_path, monkeypatch):
    # leaving the page stops the scan within one block, not after the whole meeting is read
    monkeypatch.setattr(vb, "BLOCK_S", 2.0, raising=False)
    meeting(tmp_path, "20261108_100000", *call([4.0] * 12))
    asked = []

    def stop():
        asked.append(1)
        return len(asked) > 3              # the folder, the part, one block of the call track: then stop

    found = vb.find_own_voice(vb.meeting_folders(tmp_path), should_stop=stop)
    assert found.seconds == 0 and len(asked) <= 5


def test_read_texts_and_consent_phrase():
    assert len(vb.read_texts("hy")) == 3 and vb.read_texts("xx") == vb.READ_TEXTS["en"]
    # about 20 s each read aloud, so the three takes and the consent words come to about a minute
    assert all(len(t.split()) >= 40 for t in vb.READ_TEXTS["en"])
    assert "Thursday" in vb.READ_TEXTS["en"][0]
    for code, texts in vb.READ_TEXTS.items():
        assert len(texts) == 3 and len(set(texts)) == 3 and all(t.strip() for t in texts), code
    p = vb.consent_phrase(random.Random(1))
    assert len(p.split()) == 3 and len(set(p.split())) == 3
