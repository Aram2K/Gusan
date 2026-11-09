"""The voices the person can hear the others in (Hear direction).

Default is "Match the speaker": a woman's voice for a woman, a man's voice for a man, decided ONCE
per call from the first clear stretch of their voice (pitch.py), then kept. The person picks which woman's and which man's voice,
or one fixed voice for everyone. These are ElevenLabs premade voices (in every account, they
speak all v4 Turbo languages). Others' voices are never cloned.

Each preset has a short bundled sample (assets/voices/<id>.wav) for the preview button, so a
preview costs nothing and works offline.
"""
from app_info import resource_path

MATCH = "match"          # interpreter_voice_mode: match the speaker
FIXED = "fixed"          # one voice for everyone

# (voice id, name, gender, style shown under the name)
PRESETS = (
    ("EXAVITQu4vr4xnSDxMaL", "Sarah", "female", "calm"),
    ("Xb7hH8MSUJpSbSDYk0k2", "Alice", "female", "clear, British"),
    ("cgSgspJ2msm6clMCkdW9", "Jessica", "female", "warm"),
    ("pFZP5JQG7iQjIQuC4Bku", "Lily", "female", "soft, British"),
    ("cjVigY5qzO86Huf0OWal", "Eric", "male", "smooth"),
    ("JBFqnCBsd6RMkjVDRZzb", "George", "male", "warm, British"),
    ("nPczCjzI2devNBz1zQrb", "Brian", "male", "deep"),
    ("iP95p4xoKVk53GoZ742B", "Chris", "male", "casual"),
    ("SAz9YHcvj6GT2YYXdXww", "River", "neutral", "neutral"),
)
DEFAULT_FEMALE = "EXAVITQu4vr4xnSDxMaL"      # Sarah
DEFAULT_MALE = "cjVigY5qzO86Huf0OWal"        # Eric
DEFAULT_FIXED = "SAz9YHcvj6GT2YYXdXww"       # River
SAMPLE_TEXT = "Hi! This is how I'll sound when I translate for you."

_BY_ID = {p[0]: p for p in PRESETS}


def preset(voice_id):
    return _BY_ID.get(voice_id)


def label(voice_id):
    """"Sarah · calm" (the picker's row text)."""
    p = _BY_ID.get(voice_id)
    return f"{p[1]} · {p[3]}" if p else "Voice"


def of_gender(gender):
    return [p for p in PRESETS if p[2] == gender]


def sample_path(voice_id):
    return resource_path("assets", "voices", f"{voice_id}.wav")


class VoiceChoice:
    """What the person picked. ``for_gender(g)`` -> the voice id to speak a phrase in."""

    def __init__(self, mode=MATCH, female=DEFAULT_FEMALE, male=DEFAULT_MALE, fixed=DEFAULT_FIXED):
        self.mode = mode if mode in (MATCH, FIXED) else MATCH
        self.female = female if female in _BY_ID else DEFAULT_FEMALE
        self.male = male if male in _BY_ID else DEFAULT_MALE
        self.fixed = fixed if fixed in _BY_ID else DEFAULT_FIXED

    @classmethod
    def from_config(cls, cfg):
        return cls(cfg.get("interpreter_voice_mode") or MATCH, cfg.get("interpreter_voice_female") or DEFAULT_FEMALE,
                   cfg.get("interpreter_voice_male") or DEFAULT_MALE, cfg.get("interpreter_voice_fixed") or DEFAULT_FIXED)

    def all_ids(self):
        """Every voice this choice can use (registered on the voice socket up front)."""
        return [self.fixed] if self.mode == FIXED else [self.female, self.male]

    def for_gender(self, gender):
        if self.mode == FIXED:
            return self.fixed
        return self.male if gender == "male" else self.female if gender == "female" else self.female
