"""Spoken languages for transcription: what a prompt-driven cloud model is told
about the language(s) (one language, or several mixed inside a sentence), and
the helpers the local Whisper path uses for mixed speech.

"Mixed languages" is the language setting "multi" plus cfg["mix_languages"],
the languages the user switches between - e.g. English and Spanish in one
sentence. Every word should come out in the language it was spoken in, in that
language's own script: never translated, never transliterated. With fewer than
two ticked, any language may be detected (how "multi" always worked).
"""
import re

# The languages the app offers, in picker order.
NAMES = {"en": "English", "ar": "Arabic", "hy": "Armenian", "fr": "French",
         "de": "German", "ru": "Russian", "es": "Spanish"}

# Non-Latin scripts, named for the prompt; the rest are written in Latin letters.
_SCRIPT = {"hy": "the Armenian alphabet", "ru": "Cyrillic", "ar": "Arabic script",
           "uk": "Cyrillic", "bg": "Cyrillic", "el": "the Greek alphabet", "he": "Hebrew script",
           "fa": "Arabic script", "ur": "Arabic script", "hi": "Devanagari",
           "ja": "Japanese script (kanji and kana)", "ko": "Hangul", "zh": "Chinese characters",
           "th": "Thai script", "ka": "the Georgian alphabet", "bn": "Bengali script",
           "ta": "Tamil script", "te": "Telugu script"}

# One short, natural line per language, in its own script. A mixed primer as
# Whisper's prompt nudges it to keep each language in its script instead of
# transliterating everything into the language it detected.
_PRIMER = {
    "hy": "Բարև ձեզ, այսօր կխոսենք մեր նախագծի մասին։",
    "en": "Okay, let's get started.",
    "ru": "Хорошо, давайте начнём.",
    "fr": "D'accord, on commence.",
    "de": "Okay, fangen wir an.",
    "es": "Vale, empecemos.",
    "ar": "حسناً، لنبدأ.",
}


# Languages local Whisper is known to be poor at (independent ArmBench-ASR, Aug 2026:
# Whisper large-v3 scores a 65% word error rate on Armenian, the best cloud models 14-18%),
# with the name people read. The Live Assistant's Automatic speech model moves a call in one
# of them to Gusan Cloud when that is allowed, and otherwise says so (engine_choice.
# live_speech_*). Add a code here to cover another language: nothing else changes.
LOCAL_WHISPER_POOR = {"hy": "Armenian"}


def local_whisper_poor(language, cfg=None):
    """The name ("Armenian") of the language when local Whisper is known to be poor at
    it, else "". ``language``: a code, "multi" (poor when a language the user mixes is -
    cfg "mix_languages") or anything else ("auto", "") for "not known"."""
    language = normalize(language) or str(language or "").strip().lower()
    if language == "multi":
        for code in mix_languages(cfg):
            if code in LOCAL_WHISPER_POOR:
                return LOCAL_WHISPER_POOR[code]
        return ""
    return LOCAL_WHISPER_POOR.get(language, "")


def saved_mix(cfg):
    """The languages ticked in Settings, in picker order (any number)."""
    raw = (cfg or {}).get("mix_languages")
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    return [code for code in NAMES if code in raw]


def mix_languages(cfg):
    """The languages the user mixes - only when at least two are ticked; []
    means no restriction (any language may be detected)."""
    picked = saved_mix(cfg)
    return picked if len(picked) >= 2 else []


def _join(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _scripts_note(codes):
    """"(Armenian in the Armenian alphabet, English in Latin letters)"."""
    bits = []
    latin = [NAMES[c] for c in codes if c not in _SCRIPT]
    for c in codes:
        if c in _SCRIPT:
            bits.append(f"{NAMES[c]} in {_SCRIPT[c]}")
    if latin:
        bits.append(f"{_join(latin)} in Latin letters")
    return "(" + ", ".join(bits) + ")" if bits else ""


def cloud_hint(lang_setting, mix=None):
    """The language instruction appended to a cloud transcription prompt."""
    lang_setting = normalize(lang_setting, default=lang_setting)
    if lang_setting in WHISPER_NAMES:
        nm = WHISPER_NAMES[lang_setting]
        script = _SCRIPT.get(lang_setting)
        how = (f" in {script}, never transliterated into Latin letters" if script else "")
        return (f" The speaker speaks mainly {nm}. Write {nm}{how}. If they use words or "
                f"phrases from another language, keep those exactly as spoken, in that "
                f"language's own script - don't translate them.")
    if lang_setting == "multi":
        codes = [c for c in (mix or []) if c in NAMES]
        if len(codes) < 2:
            return (" The speaker may switch languages, even in the middle of a sentence. "
                    "Write every word in the language it was spoken in, in that language's "
                    "own script. Never translate, and never turn the whole text into one "
                    "language.")
        return (f" The speaker switches between {_join(NAMES[c] for c in codes)}, often in "
                f"the middle of a sentence. Write every word in the language it was spoken "
                f"in, in that language's own script {_scripts_note(codes)}. Never translate, "
                f"and never turn the whole text into one language.")
    return (" If the speaker switches languages, keep every word in the language it was "
            "spoken in, in its own script - don't translate.")


def whisper_primer(mix):
    return " ".join(_PRIMER[c] for c in mix if c in _PRIMER)


def dominant_among(all_probs, mix, fallback):
    """The most likely spoken language among the ones the user mixes, from
    Whisper's (language, probability) list. Armenian in particular is easily
    heard as another language when every language may win."""
    best, best_p = None, -1.0
    for lang, prob in (all_probs or []):
        if lang in mix and prob > best_p:
            best, best_p = lang, prob
    return best or fallback


def _norm(text):
    return re.sub(r"[\W_]+", " ", (text or "").lower()).strip()


def is_primer_echo(text, mix, quiet=False):
    """True when a decode is only the primer read back - Whisper sometimes
    writes its prompt into near-silent audio. The whole primer, or two of its
    lines, is an echo; a single line only on near-silent audio (``quiet``) -
    someone may really say "Okay, let's get started"."""
    t = _norm(text)
    if not t:
        return False
    lines = [_norm(_PRIMER[c]) for c in mix if c in _PRIMER]
    whole = _norm(whisper_primer(mix))
    if t == whole:
        return True
    found = [line for line in lines if line and line in t]
    rest = t
    for line in found:
        rest = rest.replace(line, " ")
    only_primer = not rest.strip()
    return only_primer and (len(found) >= 2 or (quiet and len(found) == 1))


# ── every language the engines know: ONE table ───────────────────────────────
# Whisper's languages, code -> name. The same codes are what faster-whisper accepts (anything
# else raises), what the cloud prompts name, and the pool the other engine families are
# checked against.
WHISPER_NAMES = {
    "en": "English", "zh": "Chinese", "de": "German", "es": "Spanish", "ru": "Russian",
    "ko": "Korean", "fr": "French", "ja": "Japanese", "pt": "Portuguese", "tr": "Turkish",
    "pl": "Polish", "ca": "Catalan", "nl": "Dutch", "ar": "Arabic", "sv": "Swedish",
    "it": "Italian", "id": "Indonesian", "hi": "Hindi", "fi": "Finnish", "vi": "Vietnamese",
    "he": "Hebrew", "uk": "Ukrainian", "el": "Greek", "ms": "Malay", "cs": "Czech",
    "ro": "Romanian", "da": "Danish", "hu": "Hungarian", "ta": "Tamil", "no": "Norwegian",
    "th": "Thai", "ur": "Urdu", "hr": "Croatian", "bg": "Bulgarian", "lt": "Lithuanian",
    "la": "Latin", "mi": "Maori", "ml": "Malayalam", "cy": "Welsh", "sk": "Slovak",
    "te": "Telugu", "fa": "Persian", "lv": "Latvian", "bn": "Bengali", "sr": "Serbian",
    "az": "Azerbaijani", "sl": "Slovenian", "kn": "Kannada", "et": "Estonian",
    "mk": "Macedonian", "br": "Breton", "eu": "Basque", "is": "Icelandic", "hy": "Armenian",
    "ne": "Nepali", "mn": "Mongolian", "bs": "Bosnian", "kk": "Kazakh", "sq": "Albanian",
    "sw": "Swahili", "gl": "Galician", "mr": "Marathi", "pa": "Punjabi", "si": "Sinhala",
    "km": "Khmer", "sn": "Shona", "yo": "Yoruba", "so": "Somali", "af": "Afrikaans",
    "oc": "Occitan", "ka": "Georgian", "be": "Belarusian", "tg": "Tajik", "sd": "Sindhi",
    "gu": "Gujarati", "am": "Amharic", "yi": "Yiddish", "lo": "Lao", "uz": "Uzbek",
    "fo": "Faroese", "ht": "Haitian Creole", "ps": "Pashto", "tk": "Turkmen",
    "nn": "Nynorsk", "mt": "Maltese", "sa": "Sanskrit", "lb": "Luxembourgish",
    "my": "Myanmar", "bo": "Tibetan", "tl": "Tagalog", "mg": "Malagasy", "as": "Assamese",
    "tt": "Tatar", "haw": "Hawaiian", "ln": "Lingala", "ha": "Hausa", "ba": "Bashkir",
    "jw": "Javanese", "su": "Sundanese", "yue": "Cantonese",
}

# Other spellings of a code that people, a system locale or a three-letter ISO code write.
_ALIASES = {
    "hye": "hy", "arm": "hy", "eng": "en", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de",
    "rus": "ru", "spa": "es", "ara": "ar", "ukr": "uk", "ell": "el", "gre": "el", "heb": "he",
    "fas": "fa", "per": "fa", "hin": "hi", "tha": "th", "jpn": "ja", "kor": "ko", "zho": "zh",
    "chi": "zh", "cmn": "zh", "kat": "ka", "geo": "ka", "bul": "bg", "ita": "it", "por": "pt",
    "nld": "nl", "dut": "nl", "pol": "pl", "tur": "tr", "ces": "cs", "cze": "cs", "ron": "ro",
    "rum": "ro", "swe": "sv", "urd": "ur", "ben": "bn", "tam": "ta", "tel": "te", "vie": "vi",
    "ind": "id", "srp": "sr", "hrv": "hr", "nor": "no", "dan": "da", "fin": "fi", "hun": "hu",
    "nb": "no", "iw": "he", "in": "id", "ji": "yi", "jv": "jw", "fil": "tl", "kaz": "kk",
}
_NAME_TO_CODE = {name.lower(): code for code, name in WHISPER_NAMES.items()}
_AUTO_WORDS = {"auto", "detect", "auto-detect", "autodetect", "detect automatically", "automatic"}
_MULTI_WORDS = {"multi", "mixed", "mixed languages", "multilingual"}


def normalize(value, default=""):
    """A language setting as one canonical code: a Whisper code ("hy"), "auto" or "multi";
    ``default`` for an empty or unknown value. Case, spaces, regions and three-letter codes
    are all the same language: "HY", " hy-AM ", "hye", "Armenian", "en_US", "zh-Hant" ->
    "hy", "hy", "hy", "hy", "en", "zh"."""
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return default
    if text in _AUTO_WORDS:
        return "auto"
    if text in _MULTI_WORDS:
        return "multi"
    if text in _NAME_TO_CODE:
        return _NAME_TO_CODE[text]
    base = re.split(r"[-_.@\s]", text, maxsplit=1)[0]
    base = _ALIASES.get(base, base)
    return base if base in WHISPER_NAMES else default


def language_name(value):
    """"hy" -> "Armenian" (any language the engines know); an unknown value as written."""
    code = normalize(value)
    if code in WHISPER_NAMES:
        return WHISPER_NAMES[code]
    return str(value or "").strip()


def whisper_language(value):
    """The ``language=`` for faster-whisper: a code it knows, or None (detect). "auto",
    "multi" and anything unknown are None - a code Whisper does not know would raise."""
    code = normalize(value)
    return code if code in WHISPER_NAMES else None


# Which engine family can transcribe which language (data). Whisper: all of its languages (but
# see LOCAL_WHISPER_POOR: some are barely usable). Gemini (Gusan Cloud and your own Google key):
# the same broad list, Armenian included. Voxtral (Mistral): only MISTRAL_LANGS - for any other
# language it answers with confident text in another script, so it is never asked.
FAMILY_WHISPER, FAMILY_GEMINI, FAMILY_VOXTRAL = "whisper", "gemini", "voxtral"
#
# MISTRAL_LANGS is the ONE cloud-routing table for Voxtral, and it is the SAME ten languages as
# transcribe-proxy's MISTRAL_LANGS (supabase/functions/transcribe-proxy/index.ts; tests compare the two):
# the app never claims Voxtral covers a language the server would not send it.
MISTRAL_LANGS = frozenset({"en", "es", "fr", "de", "it", "nl", "pt", "hi", "ar", "ru"})
# The other cloud engines Gusan Cloud runs: OpenAI's GPT transcribe models and ElevenLabs Scribe take any
# language of the table below (they detect it, Armenian included); they are never Whisper.
FAMILY_GPT, FAMILY_SCRIBE = "gpt", "scribe"
LANGUAGE_SUPPORT = {
    FAMILY_WHISPER: frozenset(WHISPER_NAMES),
    FAMILY_GEMINI: frozenset(WHISPER_NAMES),
    FAMILY_GPT: frozenset(WHISPER_NAMES),
    FAMILY_SCRIBE: frozenset(WHISPER_NAMES),
    FAMILY_VOXTRAL: MISTRAL_LANGS,
}
FAMILY_NAMES = {FAMILY_WHISPER: "Whisper", FAMILY_GEMINI: "Gemini", FAMILY_VOXTRAL: "Voxtral",
                FAMILY_GPT: "GPT", FAMILY_SCRIBE: "Scribe"}
# The cloud families that cover a language and are known to be good at it: an answer from one of them in
# another script is code-switching (the speaker used other words), not a model that does not know the
# language. Voxtral is not here (it does not cover Armenian, its words in another script are an error).
CAPABLE_FAMILIES = frozenset({FAMILY_GEMINI, FAMILY_GPT, FAMILY_SCRIBE})


def family_of_model(model_id):
    """The engine family of a model id or name: "gemini-2.5-pro" / "Gemini 2.5 Flash" ->
    "gemini", "voxtral-mini-latest" / "Voxtral Mini" -> "voxtral", "gpt-transcribe" /
    "GPT-4o transcribe" -> "gpt", "scribe_v2" / "ElevenLabs Scribe v2" -> "scribe", a Whisper id
    ("small", "large-v3-turbo") or "Whisper Small" / "whisper-1" -> "whisper", anything else ""
    (not known). A gpt-* model is never Whisper: it is a different engine that covers Armenian."""
    text = str(model_id or "").strip().lower()
    if text.startswith("gemini"):
        return FAMILY_GEMINI
    if text.startswith(("voxtral", "mistral")):
        return FAMILY_VOXTRAL
    if text.startswith("gpt"):
        return FAMILY_GPT
    if text.startswith(("scribe", "elevenlabs")):
        return FAMILY_SCRIBE
    if text.startswith("whisper") or text in ("tiny", "base", "small", "medium", "large",
                                              "large-v2", "large-v3", "large-v3-turbo"):
        return FAMILY_WHISPER
    return ""


def is_capable(model_id):
    """Does this cloud model cover the languages the app offers and is it good at them (Gemini, GPT
    transcribe, Scribe)? "" / an unknown model / Voxtral / Whisper: False."""
    return family_of_model(model_id) in CAPABLE_FAMILIES


def supports(family, language):
    """Can the engine family transcribe ONE language (a code, any spelling)? A family that is
    not known, or a value that is not a language ("auto"), is True: nothing to refuse."""
    code = normalize(language)
    pool = LANGUAGE_SUPPORT.get(family)
    if pool is None or code in ("", "auto", "multi"):
        return True
    return code in pool


def engine_covers(family, language, mix=None):
    """May this family be asked to transcribe the call's language setting? "auto" - yes
    (nothing is refused); "multi" - only when every language in the mix is covered (no
    list: any language may come, nothing is refused); a language - when it is supported."""
    code = normalize(language)
    if code in ("", "auto"):
        return True
    if code == "multi":
        picked = [normalize(c) for c in (mix or []) if normalize(c)]
        return all(supports(family, c) for c in picked)
    return supports(family, code)


def local_quality(language, cfg=None):
    """How local Whisper does in ``language``: "poor" (LOCAL_WHISPER_POOR), "good" (any other
    language it knows, and "auto"/"multi" where nothing is known to be poor) or "none" (a
    language it does not know)."""
    code = normalize(language)
    if local_whisper_poor(code, cfg):
        return "poor"
    if code in ("auto", "multi") or code in WHISPER_NAMES:
        return "good"
    if not str(language if language is not None else "").strip():
        return "good"                       # nothing was asked: nothing is refused
    return "none"


# ── is a cloud answer in the language that was asked for? ────────────────────
# Unicode blocks of the scripts that tell a language apart. A language with its own script
# can be checked from the letters alone; Latin-script languages cannot (and are never checked).
_RANGES = {
    "armenian": ((0x0530, 0x058F), (0xFB13, 0xFB17)),
    "cyrillic": ((0x0400, 0x052F), (0x1C80, 0x1C8F), (0x2DE0, 0x2DFF), (0xA640, 0xA69F)),
    "greek": ((0x0370, 0x03FF), (0x1F00, 0x1FFF)),
    "hebrew": ((0x0590, 0x05FF), (0xFB1D, 0xFB4F)),
    "arabic": ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF),
               (0xFE70, 0xFEFF)),
    "devanagari": ((0x0900, 0x097F),),
    "bengali": ((0x0980, 0x09FF),),
    "gurmukhi": ((0x0A00, 0x0A7F),),
    "gujarati": ((0x0A80, 0x0AFF),),
    "tamil": ((0x0B80, 0x0BFF),),
    "telugu": ((0x0C00, 0x0C7F),),
    "kannada": ((0x0C80, 0x0CFF),),
    "malayalam": ((0x0D00, 0x0D7F),),
    "sinhala": ((0x0D80, 0x0DFF),),
    "thai": ((0x0E00, 0x0E7F),),
    "lao": ((0x0E80, 0x0EFF),),
    "tibetan": ((0x0F00, 0x0FFF),),
    "myanmar": ((0x1000, 0x109F),),
    "georgian": ((0x10A0, 0x10FF), (0x1C90, 0x1CBF), (0x2D00, 0x2D2F)),
    "ethiopic": ((0x1200, 0x139F),),
    "khmer": ((0x1780, 0x17FF),),
    "hangul": ((0xAC00, 0xD7AF), (0x1100, 0x11FF), (0x3130, 0x318F)),
    "kana": ((0x3040, 0x30FF), (0x31F0, 0x31FF)),
    "han": ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF)),
}
_SCRIPT_OF_LANGUAGE = {
    "hy": ("armenian",), "ru": ("cyrillic",), "uk": ("cyrillic",), "bg": ("cyrillic",),
    "be": ("cyrillic",), "mk": ("cyrillic",), "kk": ("cyrillic",), "mn": ("cyrillic",),
    "tg": ("cyrillic",), "tt": ("cyrillic",), "ba": ("cyrillic",),
    "el": ("greek",), "he": ("hebrew",), "yi": ("hebrew",), "ar": ("arabic",),
    "fa": ("arabic",), "ur": ("arabic",), "ps": ("arabic",), "hi": ("devanagari",),
    "mr": ("devanagari",), "ne": ("devanagari",), "sa": ("devanagari",), "bn": ("bengali",),
    "as": ("bengali",), "pa": ("gurmukhi",), "gu": ("gujarati",), "ta": ("tamil",),
    "te": ("telugu",), "kn": ("kannada",), "ml": ("malayalam",), "si": ("sinhala",),
    "th": ("thai",), "lo": ("lao",), "bo": ("tibetan",), "my": ("myanmar",),
    "ka": ("georgian",), "am": ("ethiopic",), "km": ("khmer",), "ko": ("hangul",),
    "ja": ("kana", "han"), "zh": ("han",), "yue": ("han",),
}
SCRIPT_MIN_LETTERS = 8       # shorter pieces ("Okay", "Yes") are never judged
SCRIPT_MIN_SHARE = 0.2       # at least this share of the letters in the language's own script


def _in_script(ch, scripts):
    o = ord(ch)
    for name in scripts:
        for lo, hi in _RANGES.get(name, ()):
            if lo <= o <= hi:
                return True
    return False


def has_distinct_script(language):
    """Is the language written in a script of its own, so a wrong answer shows (Armenian,
    Russian, Greek, Hebrew, Arabic, Hindi, Thai, Japanese, Korean, Chinese, Georgian ...)?"""
    return normalize(language) in _SCRIPT_OF_LANGUAGE


def script_matches(text, language):
    """Is ``text`` plausibly in ``language``, judged from its letters only? False when the
    language has a script of its own and (nearly) none of the letters are in it - Armenian
    asked for and Cyrillic / Latin words came back. True otherwise, always: for "auto",
    "multi", Latin-script languages, an unknown language and a piece too short to judge. A few
    foreign words inside the language's own text are fine (they are kept as spoken)."""
    scripts = _SCRIPT_OF_LANGUAGE.get(normalize(language))
    if not scripts:
        return True
    letters = [ch for ch in str(text or "") if ch.isalpha()]
    if len(letters) < SCRIPT_MIN_LETTERS:
        return True
    own = sum(1 for ch in letters if _in_script(ch, scripts))
    return own / float(len(letters)) >= SCRIPT_MIN_SHARE


def _letter_script(ch):
    """The script of one letter: "latin", a name of _RANGES, or "other"."""
    if ord(ch) < 0x250:
        return "latin"
    for name in _RANGES:
        if _in_script(ch, (name,)):
            return name
    return "other"


def mix_script_matches(text, mix):
    """Is ``text`` plausibly in one of the languages a "multi" call mixes, judged from its
    letters only? The script most of the letters are written in must be the script of one of the
    languages in ``mix`` (Latin for the ones without a script of their own). A call mixing
    Armenian and English gets Latin or Armenian letters; an answer that is mostly Cyrillic (a
    model that does not cover Armenian) is not. True for no mix, an unknown language in it, and a
    piece too short to judge."""
    codes = [c for c in (normalize(x) for x in (mix or [])) if c and c not in ("auto", "multi")]
    if not codes:
        return True
    allowed = set()
    for code in codes:
        allowed.update(_SCRIPT_OF_LANGUAGE.get(code, ("latin",)))
    letters = [ch for ch in str(text or "") if ch.isalpha()]
    if len(letters) < SCRIPT_MIN_LETTERS:
        return True
    counts = {}
    for ch in letters:
        key = _letter_script(ch)
        counts[key] = counts.get(key, 0) + 1
    return max(counts, key=counts.get) in allowed


def script_name(text):
    """The dominant script of ``text`` for a log line ("armenian" | "cyrillic" | "latin" | ... |
    "" when it has no letters). Never the words themselves."""
    counts = {}
    for ch in str(text or ""):
        if not ch.isalpha():
            continue
        found = "latin" if ord(ch) < 0x250 else ""
        if not found:
            for name in _RANGES:
                if _in_script(ch, (name,)):
                    found = name
                    break
        key = found or "other"
        counts[key] = counts.get(key, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def answer_problem(text, language, model_id="", mix=None):
    """Why a cloud answer cannot be trusted for the language that was asked for, or "":
    "engine" (the model that answered does not cover the language - Voxtral for Armenian: it
    writes confident text in another script), "script" (the letters are not in the language's
    own script), "" (nothing wrong, or nothing to judge: "auto", "multi", Latin languages,
    an empty or very short piece). Pure; the words are never logged."""
    code = normalize(language)
    if code in ("", "auto"):
        return ""
    family = family_of_model(model_id)
    if family and not engine_covers(family, code, mix):
        return "engine"
    if code == "multi":
        if not mix_script_matches(text, mix):
            return "script"
    elif not script_matches(text, code):
        return "script"
    return ""
