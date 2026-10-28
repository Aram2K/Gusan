"""What runs each feature, and the engine choices (Gusan 2.0 · Settings › Speech & AI).

Integration notes for Wave 4
----------------------------
Pages show "Runs on … · Change" lines and the Speech & AI choices from here.
Nothing here imports Qt or main; it reads the config and the Pro state only.

* runs_on(app, feature) -> dict, for feature in "dictation" (speech; also
  meetings), "smart_actions" (the writer), "smart_actions_speech" (what
  transcribes a Smart Action, cfg "smart_actions_speech_model"; for it
  downloaded= is a speech-model check), "meeting_notes", "live_assistant",
  "audio_to_text". It mirrors what really runs, not the widget mapping:
  main.AudioRecorder._transcribe_chunk (backend + key), main.AppController.
  _resolve_action_engine (Pro routing), actions.process (privacy fallbacks) and
  actions.live_engine (Live Assistant). Keys of the dict:
      kind        "local" | "cloud" (Gusan Cloud) | "own_key" | "basic"
                  (no AI model: built-in rules) | "none" (Live Assistant has
                  nothing that can answer)
      choice      the option name in the rails: "This computer" |
                  "Gusan Cloud" | "Your own key"
      plain       "Balanced" / "Small" / "Gusan Cloud" / "Mistral" / "Basic"
      model_name  the real model: "Whisper Small", "Qwen 1.5B",
                  "Gemini 2.5 Flash", "Mistral Small", "DeepSeek V4.1 Flash"
      where       "on this computer" | "in Gusan Cloud" | "with your own key"
      label       the whole line, e.g. "Balanced · Whisper Small · on this
                  computer", "Gusan Cloud · Gemini 2.5 Flash",
                  "This computer · Qwen 1.5B"
      model_id, size, pro (a Pro option), ready, problem ("" |
      "needs_key" | "needs_download"), feature
  The Pro state comes from app.is_pro() (admin preview honoured) and
  app.auth.is_authenticated, so call it again on sig_auth_changed,
  sig_setting_changed and after a model download finishes. It touches the
  disk only to ask local_llm.model_downloaded (a stat) - fine on the GUI
  thread. Pass a plain cfg dict plus pro=/signed_in= in tests.

* Names and tags (Gusan 2.0.1): every speech picker lists REAL model names with a small chip for
  where each runs - LOCAL (this computer), CLOUD (Gusan Cloud or your own key), PRO where a Pro plan
  is needed - under headings "On this computer" / "Gusan Cloud" / "Your own key"; Gusan Cloud is a
  service, never a model's name. ONE place builds them: speech_row() and
      live_speech_rows(cfg, pro, signed_in, language)   Live Assistant "Transcribes with"
      smart_speech_rows(cfg, pro)                       Smart Actions "Transcribes with"
      file_rows(cfg, pro)                               Audio to text "Accuracy"
  (dicts: value, name, title, note, tags, group, pro, line; the old *_choices() are the same rows as
  (value, title, line, pro)). Every runs_on dict also has tags and ``tagged`` ("Gemini 2.5 Flash ·
  Cloud", "Whisper Small · Local", "Gemini 2.5 Pro · your Google key · Cloud"); model_tagged(id) is
  the same for a model id (History, a finished file). Config values are unchanged.

* The Live Assistant's own speech model (cfg "live_assist_speech_model", Gusan 2.0.1):
  "" Automatic, a Whisper id, "cloud" (Gusan Cloud, Gemini 2.5 Flash), "cloud:best" (Gusan
  Cloud, Gemini 2.5 Pro: the best accuracy, slower) or "own:google:<Gemini id>" (your own key).
      runs_on(app, "live_assistant_speech", language=None)   the line, + auto / reason /
                                language_name / note (the plain hint when local Whisper is poor
                                at the call's language: speech_langs.LOCAL_WHISPER_POOR)
      live_speech_session(cfg, pro, language)   the take's ``speech=`` for the recorder
      live_speech_choices(cfg) / set_live_speech(app, value, downloaded=, turn_off_privacy=)
                                the picker rows and the change (problem "pro" | "privacy_mode"
                                | "needs_key" | "needs_download" | "unknown")
      live_speech_status(engine, report)   the card's "Transcribing with Gemini 2.5 Flash · Cloud"
                                line, from what the recorder's speech_report() says RAN
      live_speech_note(..., report=)       the one calm line under it: the hint when local Whisper
                                is poor at the language, or - when the cloud could not read the
                                latest piece of such a language - gap_line() ("Armenian couldn't be
                                transcribed just now - retrying. Local Whisper isn't reliable for
                                it."), gone by itself when the cloud answers again
  Automatic is the same as Dictation, except for a call in a language local Whisper is poor at
  (Armenian): then Gusan Cloud (Flash) when signed-in Pro, no Privacy mode - else local, with
  the hint. Only sessions started from the Live Assistant use it (Meetings and dictation keep
  Dictation's model).

* Choices on Settings › Speech & AI (and onboarding's Gusan Cloud card):
      res = engine_choice.set_speech_source(app, "local" | "cloud" | "own_key",
                                            provider=None, model=None)
      res = engine_choice.set_writer(app, "cloud" | "local" | "own_key",
                                     model=None, provider=None, base_url=None)
  Each returns {"ok", "changes", "problem", "upsell"}. When ok, the page
  calls app.set_settings(res["changes"]) (never cfg.update). Otherwise:
      problem "pro"            -> app._pro_upsell(res["upsell"])
      problem "privacy_mode"   -> say Privacy mode is on (or pass
                                  turn_off_privacy=True after asking)
      problem "needs_key"      -> open the key row (api_keys.save_key first)
      problem "needs_download" -> start the download (model_manager), then
                                  call the same function again
  Pass downloaded=model_manager's "is it on disk" for speech models (this
  module can't import main.model_downloaded); local writers check
  local_llm.model_downloaded themselves.
  Selected option for the radio groups: speech_choice(cfg) / writer_choice(cfg),
  local_writer(cfg). Model lists: SPEECH_MODELS, MAIN_SPEECH, MORE_SIZES,
  LOCAL_WRITERS (all derived from hardware.py's MODEL_INFO / LOCAL_AI_INFO,
  the app's one name catalog), WRITER_PROVIDERS (+ writer_models /
  writer_model_for) for the bring-your-own-key writers.

* Audio to text's model (cfg "file_model", the FILE_* names): "auto", a
  hardware.MODEL_INFO Whisper id, "cloud" (Gusan Cloud, Pro) or
  "own:google" / "own:mistral" (your own key; never in Privacy mode).
      file_choices(cfg)              [(value, plain, line, pro)] for the picker
      set_file_model(app, value)     same return shape as set_speech_source
      file_session(cfg, pro)         (backend, model) the job runs with; a
                                     cloud pick that can't run now is
                                     ("local", None) = Automatic
      file_engine(cfg, file_model=None, pro=False)   the runs_on dict
      file_model_id(cfg, value=None) the real model id ("gemini-2.5-flash"),
                                     what a finished job stores as its model;
                                     its name is in MODEL_NAMES

* The Armenian speech model (Gusan 2.0.1, speech_hy.py): a local model whose config value is
  hardware.HY_MODEL_ID ("hy-fastconformer") wherever a Whisper id fits (whisper_model,
  smart_actions_speech_model, live_assist_speech_model, file_model). It is NOT in SPEECH_MODELS /
  hardware.MODEL_INFO (so no list that walks those shows it); hy_row() is its picker row, None while
  speech_hy.listed() is False. It reads Armenian only - the recorder sends any other language to a
  Whisper model (AudioRecorder.engine_model) - and its names come from hardware.HY_INFO
  (speech_model_name, speech_plain, model_tagged).

* main.py can later take MISTRAL_STT_DEFAULT / normalize_mistral_model from
  here (same values; test_engine_choice checks they match).
"""

import actions
import action_api
import hardware
import local_llm
import speech_langs

# ── names ────────────────────────────────────────────────────────────────────
GUSAN_CLOUD = "Gusan Cloud"
THIS_COMPUTER = "This computer"
OWN_KEY = "Your own key"
WHERE_LOCAL = "on this computer"
WHERE_CLOUD = "in Gusan Cloud"
WHERE_OWN_KEY = "with your own key"

# ── names and tags: the ONE place every speech picker, line and label reads ──────
# A row of a speech picker is a REAL model name plus a small tag: LOCAL (runs on this computer),
# CLOUD (runs in the cloud: Gusan Cloud or the user's own key) and, where a Pro plan is needed,
# PRO. Gusan Cloud is a SERVICE (a heading of the list), never the name of a model.
TAG_LOCAL, TAG_CLOUD, TAG_PRO = "local", "cloud", "pro"
TAG_WORD = {TAG_LOCAL: "Local", TAG_CLOUD: "Cloud", TAG_PRO: "Pro"}        # in a sentence
TAG_CHIP = {tag: word.upper() for tag, word in TAG_WORD.items()}            # on the chip
TAG_TIP = {TAG_LOCAL: "Runs on this computer", TAG_CLOUD: "Runs in the cloud",
           TAG_PRO: "Needs a Pro plan"}
# The headings of a picker's groups (the service, not a model).
GROUP_THIS_COMPUTER = "On this computer"
GROUP_GUSAN_CLOUD = GUSAN_CLOUD
GROUP_OWN_KEY = OWN_KEY
# The muted second line of a cloud model: what it is good for. A model not listed has none.
MODEL_NOTES = {
    "gemini-2.5-flash": "Fast and accurate",
    "gemini-2.5-pro": "Best accuracy · slower",
    "gemini-3-flash-preview": "Newest · preview",
    "voxtral-mini-latest": "Fast · 10 languages",
}

# ── Gusan Cloud speech: two PROFILES (transcribe-proxy) ───────────────────────────
# Gusan Cloud is a service that CHOOSES the engine: the request says "fast" or "best" (its "model"
# field) and the server tries its engines in order - ElevenLabs Scribe v2, Voxtral Mini (only for the
# languages it covers), GPT Transcribe, ... - and answers with the one that REALLY ran (the answer's
# "model" and X-Gusan-Model). So a picker names the PROFILE, never a model it cannot promise, and every
# line, card and History entry that says what transcribed names the model from that answer
# (MODEL_NAMES below). The old Gemini ids are not sent to Gusan Cloud any more; a saved "cloud:best" /
# "gemini-2.5-pro" is the best profile, "cloud" / "gemini-2.5-flash" / a provider the fast one.
CLOUD_FAST, CLOUD_BEST = "fast", "best"
CLOUD_PROFILES = (CLOUD_FAST, CLOUD_BEST)
CLOUD_PROFILE_NAMES = {CLOUD_FAST: "Fast", CLOUD_BEST: "Best accuracy"}
CLOUD_PROFILE_NOTES = {CLOUD_FAST: "Quick answers · Gusan chooses the model",
                       CLOUD_BEST: "Most accurate · Gusan chooses the model"}
_PROFILE_ALIASES = {"best": CLOUD_BEST, "gemini-2.5-pro": CLOUD_BEST, "fast": CLOUD_FAST}


def cloud_profile(value, default=CLOUD_FAST):
    """The Gusan Cloud profile a value means: "best" / "gemini-2.5-pro" -> "best", "fast" -> "fast",
    anything else ("", None, "gemini-2.5-flash", "gemini", "mistral", a model id) -> ``default``."""
    return _PROFILE_ALIASES.get(str(value or "").strip().lower(), default)
# Who an own-key row belongs to ("Gemini 2.5 Flash · your Google key").
OWN_KEY_OWNER = {"Google Gemini": "your Google key", "Mistral": "your Mistral key"}
SHOW_PRO_TO_PRO = True       # a Gusan Cloud row wears PRO for a Pro person too (it says why it is there)


def pro_chip(user_pro):
    """May a row that needs Pro wear its PRO chip for this person? Always for Free and guests
    (it says why a row opens the Pro window); for Pro people while SHOW_PRO_TO_PRO."""
    return (not user_pro) or SHOW_PRO_TO_PRO


def kind_tags(kind, pro=False):
    """The tags of a kind of engine: "local" -> (local,), "cloud" (Gusan Cloud) -> (cloud, pro),
    "own_key" -> (cloud,), anything else (no AI model) -> ()."""
    if kind == "local":
        return (TAG_LOCAL,)
    if kind == "cloud":
        return (TAG_CLOUD, TAG_PRO)
    if kind == "own_key":
        return (TAG_CLOUD,)
    return ()


def tag_words(tags):
    """("cloud", "pro") -> "Cloud · Pro"."""
    return " · ".join(TAG_WORD[t] for t in (tags or ()) if t in TAG_WORD)


def tag_tip(tags):
    """("cloud", "pro") -> "Runs in the cloud. Needs a Pro plan" (a row's tooltip)."""
    return ". ".join(TAG_TIP[t] for t in (tags or ()) if t in TAG_TIP)


def owner_phrase(plain):
    """"Google Gemini" -> "your Google key" (the owner of an own-key row)."""
    return OWN_KEY_OWNER.get(str(plain or ""), "your own key")


def tagged_name(name, kind, plain=""):
    """The name of what transcribes, with its tag: "Whisper Small · Local", "Gemini 2.5 Flash ·
    Cloud", "Gemini 2.5 Pro · your Google key · Cloud". Used by the card's line, the pickers'
    lines and a finished file's label. "" for no name."""
    name = str(name or "").strip()
    if not name:
        return ""
    if kind == "local":
        return f"{name} · {TAG_WORD[TAG_LOCAL]}"
    if kind == "own_key":
        return f"{name} · {owner_phrase(plain)} · {TAG_WORD[TAG_CLOUD]}"
    if kind == "cloud":
        return f"{name} · {TAG_WORD[TAG_CLOUD]}"
    return name


def model_tagged(model_id):
    """A model id as a finished job or a History line names it: "gemini-2.5-flash" ->
    "Gemini 2.5 Flash · Cloud", "small" -> "Whisper Small · Local"; "" for no model, and the
    plain name for one nothing is known about."""
    model_id = str(model_id or "").strip()
    if not model_id:
        return ""
    if hardware.is_speech_id(model_id):
        return tagged_name(speech_model_name(model_id), "local")
    family = speech_langs.family_of_model(model_id)
    name = model_display_name(model_id)
    if model_id in CLOUD_PROFILES:
        return tagged_name(name, "cloud")
    if (family in (speech_langs.FAMILY_GEMINI, speech_langs.FAMILY_VOXTRAL, speech_langs.FAMILY_GPT,
                   speech_langs.FAMILY_SCRIBE) or model_id == "whisper-1"):
        return tagged_name(name, "cloud")
    return name


def speech_row(value, name, kind, group="", *, detail="", note="", pro=False, model="",
               plain="", show_pro=True):
    """One row of a speech picker (a dict): ``value`` (the config value, unchanged), ``name`` (the
    real model name, or "Automatic" / "Same as Dictation"), ``detail`` (more after the name: the
    model Automatic picks, "your Google key"), ``title`` (name + detail: the first line),
    ``note`` (the muted second line), ``kind`` ("auto" | "local" | "cloud" | "own_key"),
    ``tags`` (what the chips say), ``pro`` (a Pro plan is needed), ``group`` (the heading it sits
    under, "" = none), ``line`` (the same as one sentence: tooltips, screen readers)."""
    tag_kind = {"auto": ""}.get(kind, kind)
    tags = list(kind_tags(tag_kind, pro))
    if TAG_PRO in tags and not (pro and show_pro):
        tags.remove(TAG_PRO)
    title = f"{name} · {detail}" if detail else name
    words = tag_words(tags)
    return {"value": value, "name": name, "detail": detail, "title": title, "closed": name,
            "note": note, "kind": kind, "tags": tuple(tags), "pro": bool(pro), "group": group,
            "model": model or name, "plain": plain,
            "line": f"{title} · {words}" if words else title, "tip": tag_tip(tags)}


def _row_for_engine(value, name, engine, *, group="", note="", detail="", show_pro=True):
    """The row of an Automatic / Same as Dictation choice: it wears the tags of the engine it
    would run (a runs_on dict) and names that engine after its own name."""
    kind = engine.get("kind") if engine.get("kind") in ("local", "cloud", "own_key") else "local"
    row = speech_row(value, name, "auto", group, detail=detail or engine.get("model_name") or "",
                     note=note, model=engine.get("model_name") or "")
    tags = [t for t in kind_tags(kind, True) if t != TAG_PRO or show_pro]
    row["tags"] = tuple(tags)
    row["runs"] = kind
    words = tag_words(tags)
    row["line"] = f"{row['title']} · {words}" if words else row["title"]
    row["tip"] = tag_tip(tags)
    return row


def _whisper_rows(group=GROUP_THIS_COMPUTER):
    """The Whisper models, as rows: the real name (Whisper Small), LOCAL, and the plain word
    (Balanced) under it."""
    return [speech_row(mid, info.real, "local", group, note=info.plain, model=info.real, plain=info.plain)
            for mid, info in hardware.MODEL_INFO.items()]


# The Armenian model's row (speech_hy.py), in every picker that lists models on this computer: its real
# name (NVIDIA is credited in it), LOCAL, and one muted line that says what it is good for. "Armenian
# only" is the whole explanation of why another language is read by Whisper instead: a model picked for a
# language it does not read is never silently ignored - the line says it, and the recorder keeps a Whisper
# model for every other language (AudioRecorder.engine_model).
HY_ROW_NOTE = "Armenian only · about {size} · free, works offline"
HY_ROW_NOTE_READY = "Armenian only · free, works offline"
HY_ROW_TIP = "Model by NVIDIA (CC BY 4.0)"


def hy_status():
    """The Armenian model's state for a picker: "hidden" (not on this computer, nowhere to download it
    from, or this copy of Gusan can't run it), "missing" (can be downloaded) or "ready"."""
    try:
        import speech_hy
        return speech_hy.status()["state"]
    except Exception:
        return "hidden"


def hy_row(group=GROUP_THIS_COMPUTER):
    """The Armenian speech model's row of a picker (the same dict as _whisper_rows': its real name,
    LOCAL, and the muted line "Armenian only · about 105 MB · free, works offline" under it), or None
    while the model is hidden (speech_hy.listed: not on this computer, nowhere to download it from,
    or no runtime). Pickers add it after the Whisper rows; the value saved is hardware.HY_MODEL_ID."""
    import speech_hy
    if not speech_hy.listed():
        return None
    info = hardware.HY_INFO
    note = HY_ROW_NOTE_READY if speech_hy.ready() else HY_ROW_NOTE.format(size=info.size)
    row = speech_row(info.id, info.real, "local", group, note=note, model=info.real, plain=info.plain)
    row["tip"] = (row["tip"] + ". " if row["tip"] else "") + HY_ROW_TIP
    return row


def _local_rows(group=GROUP_THIS_COMPUTER):
    """Every model that runs on this computer, as picker rows: the Whisper models, then the Armenian
    model while it is listed (hy_row)."""
    rows = _whisper_rows(group)
    extra = hy_row(group)
    if extra is not None:
        rows.append(extra)
    return rows


def _tuples(rows):
    """The old [(value, plain, line, pro)] shape of a picker's rows: ``plain`` is what the closed
    box says (the title), ``line`` the whole row as one sentence."""
    return [(r["value"], r["title"], r["line"], r["pro"]) for r in rows]

# "smart_actions" is the Smart Actions WRITER; "smart_actions_speech" is what
# transcribes a Smart Action (its own pick, or the same as Dictation).
# "live_assistant" is the Live Assistant's ANSWER writer; "live_assistant_speech" is what
# transcribes a Live Assistant session (its own pick, or Automatic).
FEATURES = ("dictation", "smart_actions", "meeting_notes", "live_assistant", "audio_to_text",
            "smart_actions_speech", "live_assistant_speech")
_FEATURE_ALIASES = {
    "speech": "dictation", "meetings": "dictation", "meeting_speech": "dictation",
    "smart": "smart_actions", "notes": "meeting_notes", "live_assist": "live_assistant",
    "files": "audio_to_text", "file": "audio_to_text", "smart_speech": "smart_actions_speech",
    "live_speech": "live_assistant_speech", "live_assist_speech": "live_assistant_speech",
}

# Smart Actions' own speech model (cfg key): "" = Same as Dictation,
# "cloud" = Gusan Cloud (Pro, never in Privacy mode), or a hardware.MODEL_INFO
# id (Whisper on this computer).
SMART_SPEECH_KEY = "smart_actions_speech_model"
SMART_SPEECH_SAME = ""
SMART_SPEECH_CLOUD = "cloud"
SMART_SPEECH_SAME_LABEL = "Same as Dictation"

# The Live Assistant's own speech model (cfg key live_assist_speech_model):
#   ""                  Automatic: the same as Dictation - except for a call in a language local
#                       Whisper is known to be poor at (Armenian, speech_langs.LOCAL_WHISPER_POOR)
#                       when Dictation would run local Whisper. THE ORDER FOR SUCH A LANGUAGE:
#                       1. Gusan Cloud, the fast profile, when it is allowed (signed-in Pro, no
#                          Privacy mode; a piece it can't answer - offline - is read by 2 if that is
#                          on this computer, else it is a gap, never Whisper's text),
#                       2. the model for that language on this computer (the Armenian model,
#                          speech_hy) when it is downloaded - also in Privacy mode,
#                       3. local Whisper, with a plain hint that offers 2 when it can be downloaded
#   a Whisper id        that model on this computer (hardware.MODEL_INFO)
#   "hy-fastconformer"  the Armenian model (hardware.HY_MODEL_ID): reads Armenian only, any other
#                       language of the call goes to a Whisper model
#   "cloud"             Gusan Cloud, the fast profile (Pro, never in Privacy mode)
#   "cloud:best"        Gusan Cloud, the best profile: "Best accuracy" (Pro, never in Privacy mode)
#   "own:google[:id]"   your own Google key, one of GEMINI_SPEECH_MODELS (never in Privacy mode)
LIVE_SPEECH_KEY = "live_assist_speech_model"
LIVE_SPEECH_AUTO = ""
LIVE_SPEECH_AUTO_LABEL = "Automatic"
LIVE_SPEECH_CLOUD = "cloud"
LIVE_SPEECH_BEST = "cloud:best"            # (the row says "Best accuracy" under the Gusan Cloud heading)
LIVE_SPEECH_OWN_GOOGLE = "own:google"
# The kinds parse_live_speech returns.
LIVE_AUTO, LIVE_LOCAL, LIVE_CLOUD, LIVE_BEST, LIVE_OWN, LIVE_UNKNOWN = (
    "auto", "local", "cloud", "cloud_best", "own_google", "unknown")

# The Gemini audio models, in ONE place - for YOUR OWN Google key only (the Live Assistant's short
# list; ids as in Google's generateContent URL, cloud_speech.GEMINI_URL). Gusan Cloud is asked for a
# PROFILE (CLOUD_FAST / CLOUD_BEST), never a Gemini id: its server decides which engine runs.
GEMINI_SPEECH_MODELS = ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash-preview")

# Audio to text's model (cfg key): "auto" (picked for this computer), a
# hardware.MODEL_INFO id (Whisper on this computer), "cloud" (Gusan Cloud,
# Pro) or "own:google" / "own:mistral" (your own key). The three cloud
# values are never used in Privacy mode.
FILE_KEY = "file_model"
FILE_AUTO = "auto"
FILE_AUTO_LABEL = "Automatic"
FILE_CLOUD = "cloud"
FILE_OWN_GOOGLE = "own:google"
FILE_OWN_MISTRAL = "own:mistral"
FILE_CLOUD_VALUES = (FILE_CLOUD, FILE_OWN_GOOGLE, FILE_OWN_MISTRAL)
# own-key value -> (backend, the cfg key it needs)
_FILE_OWN = {FILE_OWN_GOOGLE: ("google", "google_api_key"),
             FILE_OWN_MISTRAL: ("mistral", "mistral_api_key")}

# What the Pro window says each choice is (ProDialog feature names).
UPSELL_SPEECH = "Fast cloud transcription"
UPSELL_WRITER = "Smart Actions"

# ── speech models ────────────────────────────────────────────────────────────
# The names come from hardware.py (one catalog for the whole app): id ->
# (plain name, real model, download size, memory), e.g. "small" ->
# ("Balanced", "Whisper Small", "460 MB", "4 GB").
def _names(catalog):
    return {mid: (i.plain, i.real, i.size, f"{i.memory_gb} GB") for mid, i in catalog.items()}


SPEECH_MODELS = _names(hardware.MODEL_INFO)
MAIN_SPEECH = tuple(hardware.MAIN_SPEECH_MODELS)      # the three big cards
MORE_SIZES = tuple(hardware.MORE_SPEECH_MODELS)       # under "More sizes"

# Gusan Cloud speech (transcribe-proxy): managed_provider -> what it is called. The setting is a
# leftover of 1.9 (Gemini or Mistral on the server): the server now chooses by profile, so both read
# the same - the fast profile, which is what Dictation asks for (a "mistral" provider only moves
# Voxtral to the front of that profile for the languages it covers: cloud_speech / main send it).
CLOUD_SPEECH_MODELS = {"gemini": CLOUD_PROFILE_NAMES[CLOUD_FAST], "mistral": CLOUD_PROFILE_NAMES[CLOUD_FAST]}
CLOUD_WRITER_MODEL = hardware.CLOUD_WRITER_MODEL      # meeting notes on Gusan Cloud
CLOUD_LIVE_MODEL = hardware.CLOUD_LIVE_MODEL          # Live Assistant answers on Gusan Cloud
CLOUD_SMART_MODEL = hardware.CLOUD_SMART_MODEL        # Smart Actions on Gusan Cloud

# Mistral's /audio/transcriptions only takes Voxtral Mini (same as main.py).
MISTRAL_STT_DEFAULT = "voxtral-mini-latest"
MISTRAL_STT_MODELS = {"voxtral-mini-latest", "voxtral-mini-2602", "voxtral-mini-2507"}
GOOGLE_STT_DEFAULT = "gemini-2.5-flash"
# What a Gusan Cloud request asks for per managed_provider (the profile; names in MODEL_NAMES).
CLOUD_SPEECH_IDS = {"gemini": CLOUD_FAST, "mistral": CLOUD_FAST}


def normalize_mistral_model(model):
    return model if model in MISTRAL_STT_MODELS else MISTRAL_STT_DEFAULT


# ── AI writer models ─────────────────────────────────────────────────────────
# local_llm id -> (plain size name, real model, download, memory), from
# hardware.LOCAL_AI_INFO: "qwen_tiny" -> ("Small", "Qwen 1.5B", "1.12 GB", "8 GB").
LOCAL_WRITERS = _names(hardware.LOCAL_AI_INFO)
DEFAULT_LOCAL_WRITER = local_llm.QWEN_TINY_ID

# Bring-your-own-key writers (lifted from ui/settings.py _set_backend_layout /
# _save_action_configs). key_field: where the key lives (Gemini shares the
# speech key; Mistral falls back to the Voxtral key - actions.cloud_api_config).
# base_url: "fixed" (always this URL), "editable" (the user's, default shown),
# or None (never stored: a stale URL from another provider broke Gemini).
WRITER_PROVIDERS = {
    actions.API_MISTRAL_ID: {
        "name": "Mistral", "key_field": "action_api_key", "key_fallback": "mistral_api_key",
        "base_url": "fixed", "default_url": "https://api.mistral.ai/v1",
        "default": "ministral-14b-2512",
        "models": [("ministral-14b-2512", "fast, sees images"), ("ministral-8b-2512", "cheaper, sees images"),
                   ("mistral-small-latest", "stronger, sees images"), ("mistral-medium-latest", "best quality")],
    },
    actions.API_GEMINI_ID: {
        "name": "Google Gemini", "key_field": "google_api_key", "key_fallback": None,
        "base_url": None, "default_url": "https://generativelanguage.googleapis.com/v1beta",
        "default": "gemini-2.5-flash",
        "models": [("gemini-2.5-flash", ""), ("gemini-2.5-flash-lite", "cheaper"),
                   ("gemini-2.5-pro", "needs a paid plan"), ("gemini-3.5-flash", ""),
                   ("gemini-3.1-flash-lite", "cheaper")],
    },
    actions.API_CEREBRAS_ID: {
        "name": "Cerebras", "key_field": "action_api_key", "key_fallback": None,
        "base_url": "editable", "default_url": "https://api.cerebras.ai/v1",
        "default": "qwen-3.8-27b",
        "models": [("qwen-3.8-27b", "fastest, sees images"), ("gpt-oss-120b", "text only, cheaper")],
    },
    actions.API_OPENAI_ID: {
        "name": "OpenAI-compatible", "key_field": "action_api_key", "key_fallback": None,
        "base_url": "editable", "default_url": "https://api.openai.com/v1",
        "default": "gpt-5.4-mini",
        "models": [("gpt-5.5", ""), ("gpt-5.4", ""), ("gpt-5.4-mini", "cheaper"), ("gpt-4o", ""),
                   ("gpt-4o-mini", "cheaper"), ("gpt-3.5-turbo", "")],
    },
    actions.API_ANTHROPIC_ID: {
        "name": "Anthropic Claude", "key_field": "action_api_key", "key_fallback": None,
        "base_url": None, "default_url": "https://api.anthropic.com/v1",
        "default": "claude-sonnet-4-6",
        "models": [("claude-opus-4-8", ""), ("claude-sonnet-4-6", ""), ("claude-haiku-4-5-20251001", "cheaper"),
                   ("claude-3-5-sonnet-latest", ""), ("claude-3-5-haiku-latest", "cheaper"),
                   ("claude-3-5-opus-latest", ""), ("claude-3-haiku-20240307", "cheaper"),
                   ("claude-3-opus-20240229", "")],
    },
}
_PROVIDER_ALIASES = {
    "mistral": actions.API_MISTRAL_ID, "gemini": actions.API_GEMINI_ID, "google": actions.API_GEMINI_ID,
    "cerebras": actions.API_CEREBRAS_ID, "openai": actions.API_OPENAI_ID,
    "openai_compatible": actions.API_OPENAI_ID, "anthropic": actions.API_ANTHROPIC_ID,
    "claude": actions.API_ANTHROPIC_ID,
}
# Models Google removed (404): heal to the default.
_DEAD_MODELS = {"gemini-1.5-flash"}

# Real names for model ids people see in "Runs on" lines.
MODEL_NAMES = {
    "gemini-2.5-flash": "Gemini 2.5 Flash", "gemini-2.5-flash-lite": "Gemini 2.5 Flash-Lite",
    "gemini-2.5-pro": "Gemini 2.5 Pro", "gemini-3.5-flash": "Gemini 3.5 Flash",
    "gemini-3.1-flash-lite": "Gemini 3.1 Flash-Lite",
    "gemini-3-flash-preview": "Gemini 3 Flash (preview)",
    "claude-opus-4-8": "Claude Opus 4.8", "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5", "claude-3-5-sonnet-latest": "Claude 3.5 Sonnet",
    "claude-3-5-haiku-latest": "Claude 3.5 Haiku", "claude-3-5-opus-latest": "Claude 3.5 Opus",
    "claude-3-haiku-20240307": "Claude 3 Haiku", "claude-3-opus-20240229": "Claude 3 Opus",
    "gpt-5.5": "GPT-5.5", "gpt-5.4": "GPT-5.4", "gpt-5.4-mini": "GPT-5.4 mini", "gpt-4o": "GPT-4o",
    "gpt-4o-mini": "GPT-4o mini", "gpt-3.5-turbo": "GPT-3.5 Turbo",
    "ministral-14b-2512": "Ministral 14B", "ministral-8b-2512": "Ministral 8B",
    "mistral-small-latest": "Mistral Small", "mistral-medium-latest": "Mistral Medium",
    "qwen-3.8-27b": "Qwen 3.8 27B", "gpt-oss-120b": "gpt-oss 120B",
    "voxtral-mini-latest": "Voxtral Mini", "voxtral-mini-2602": "Voxtral Mini 2602",
    "voxtral-mini-2507": "Voxtral Mini 2507",
    # What Gusan Cloud's server can answer with (transcribe-proxy: the answer's "model" / X-Gusan-Model).
    "scribe_v2": "ElevenLabs Scribe v2", "gpt-transcribe": "GPT Transcribe",
    "gpt-4o-transcribe": "GPT-4o Transcribe", "gpt-4o-mini-transcribe": "GPT-4o mini Transcribe",
    "whisper-1": "Whisper (OpenAI)",
    # Gusan Cloud's two profiles (a job that asked for one and has no answer yet).
    CLOUD_FAST: CLOUD_PROFILE_NAMES[CLOUD_FAST], CLOUD_BEST: CLOUD_PROFILE_NAMES[CLOUD_BEST],
}


def model_display_name(model_id):
    """'gemini-2.5-flash' -> 'Gemini 2.5 Flash'; an unknown (custom) id as typed."""
    model_id = str(model_id or "").strip()
    return MODEL_NAMES.get(model_id, model_id)


def _speech_info(model_id):
    """(plain, real name, download size, memory) of a speech model on this computer: a Whisper id
    (SPEECH_MODELS) or the Armenian model (hardware.HY_INFO, which is not in SPEECH_MODELS: the
    pickers list it only while speech_hy.listed()), else None."""
    info = SPEECH_MODELS.get(model_id)
    if info is None and model_id == hardware.HY_MODEL_ID:
        i = hardware.HY_INFO
        info = (i.plain, i.real, i.size, f"{i.memory_gb} GB")
    return info


def speech_plain(model_id):
    """'small' -> 'Balanced' (the id itself for an unknown model)."""
    return (_speech_info(model_id) or (model_id,))[0]


def speech_model_name(model_id):
    """'small' -> 'Whisper Small'; 'hy-fastconformer' -> 'NVIDIA FastConformer Armenian'."""
    info = _speech_info(model_id)
    return info[1] if info else f"Whisper {str(model_id or '').title()}".strip()


def _norm_provider(provider):
    p = str(provider or "").strip()
    if p in WRITER_PROVIDERS:
        return p
    return _PROVIDER_ALIASES.get(p.lower(), "")


def _other_providers_url(pid, url):
    """True when ``url`` is another writer provider's standard URL."""
    u = str(url or "").strip().rstrip("/").lower()
    return bool(u) and any(u == info["default_url"].rstrip("/").lower()
                           for other, info in WRITER_PROVIDERS.items() if other != pid)


def writer_models(provider):
    """[(model id, real name, note)] for a bring-your-own-key writer."""
    info = WRITER_PROVIDERS.get(_norm_provider(provider))
    if not info:
        return []
    return [(m, model_display_name(m), note) for m, note in info["models"]]


def writer_model_for(provider, cfg):
    """The model a writer provider runs with: the saved action_api_model
    unless it belongs to another provider (or was removed), else the
    provider's default. A custom model id stays."""
    pid = _norm_provider(provider)
    info = WRITER_PROVIDERS.get(pid)
    if not info:
        return ""
    saved = str((cfg or {}).get("action_api_model") or "").strip()
    if not saved or saved in _DEAD_MODELS:
        return info["default"]
    for other, other_info in WRITER_PROVIDERS.items():
        if other != pid and any(saved == m for m, _ in other_info["models"]):
            return info["default"]
    return saved


# ── what runs ────────────────────────────────────────────────────────────────
def _context(app_or_cfg, pro=None, signed_in=None):
    """(cfg, pro, signed_in) from an AppController-like object or a cfg dict."""
    if app_or_cfg is None or isinstance(app_or_cfg, dict):
        cfg = app_or_cfg or {}
        pro = bool(pro)
        return cfg, pro, bool(pro if signed_in is None else signed_in)
    app = app_or_cfg
    cfg = getattr(app, "cfg", None) or {}
    if pro is None:
        try:
            pro = bool(app.is_pro())
        except Exception:
            pro = False
    if signed_in is None:
        signed_in = bool(getattr(getattr(app, "auth", None), "is_authenticated", False))
    return cfg, bool(pro), bool(signed_in)


def _result(feature, kind, choice, plain, model_name, where, label, model_id="", size="",
            pro=False, ready=True, problem=""):
    # tags / tagged: the same words every picker, the card's line and a finished file's label use
    # ("Gemini 2.5 Flash · Cloud") - see tagged_name. (An AI writer wears them too: the same words.)
    return {"feature": feature, "kind": kind, "choice": choice, "plain": plain,
            "model_name": model_name, "where": where, "label": label, "model_id": model_id,
            "size": size, "pro": pro, "ready": ready, "problem": problem,
            "tags": kind_tags(kind, pro), "tagged": tagged_name(model_name, kind, plain)}


def _local_speech(feature, model_id):
    model_id = model_id or "base"
    plain, name, size, _mem = _speech_info(model_id) or (model_id, speech_model_name(model_id), "", "")
    return _result(feature, "local", THIS_COMPUTER, plain, name, WHERE_LOCAL,
                   f"{plain} · {name} · {WHERE_LOCAL}", model_id, size)


def speech_engine(cfg, pro, feature="dictation"):
    """What transcribes dictation and meetings (AudioRecorder._transcribe_chunk):
    managed needs Pro (else it falls back to this computer), a BYO backend
    needs its key (else this computer), privacy mode is this computer."""
    cfg = cfg or {}
    backend = cfg.get("backend") or "local"
    if not cfg.get("privacy_mode"):
        if backend == "managed" and pro:
            provider = str(cfg.get("managed_provider") or "gemini").lower()
            provider = provider if provider in CLOUD_SPEECH_MODELS else "gemini"
            name = CLOUD_SPEECH_MODELS[provider]
            return _result(feature, "cloud", GUSAN_CLOUD, GUSAN_CLOUD, name, WHERE_CLOUD,
                           f"{GUSAN_CLOUD} · {name}", CLOUD_SPEECH_IDS[provider], pro=True)
        if backend == "google" and cfg.get("google_api_key"):
            mid = str(cfg.get("google_stt_model") or GOOGLE_STT_DEFAULT)
            name = model_display_name(mid)
            return _result(feature, "own_key", OWN_KEY, "Google Gemini", name, WHERE_OWN_KEY,
                           f"Google Gemini · {name} · your own key", mid)
        if backend == "mistral" and cfg.get("mistral_api_key"):
            mid = normalize_mistral_model(cfg.get("mistral_stt_model", MISTRAL_STT_DEFAULT))
            name = model_display_name(mid)
            return _result(feature, "own_key", OWN_KEY, "Mistral", name, WHERE_OWN_KEY,
                           f"Mistral · {name} · your own key", mid)
    return _local_speech(feature, cfg.get("whisper_model") or "base")


# ── Smart Actions' speech model (D8) ─────────────────────────────────────────
def smart_speech_session(cfg, pro):
    """(backend, model) for a Smart Action take (AudioRecorder.start_recording
    speech=), or (None, None) = transcribe like Dictation. "cloud" needs Pro
    and no Privacy mode; an unknown value is Same as Dictation."""
    cfg = cfg or {}
    value = str(cfg.get(SMART_SPEECH_KEY) or "").strip()
    if value == SMART_SPEECH_CLOUD:
        if pro and not cfg.get("privacy_mode"):
            return ("managed", None)
        return (None, None)
    if hardware.is_speech_id(value):
        return ("local", value)
    return (None, None)


def smart_speech_engine(cfg, pro, downloaded=None):
    """What transcribes a Smart Action: the runs_on dict (feature
    "smart_actions_speech"). ``downloaded`` (optional, a speech-model "is it
    on disk") marks a local pick not on disk as problem "needs_download"."""
    cfg = cfg or {}
    feature = "smart_actions_speech"
    backend, model = smart_speech_session(cfg, pro)
    if backend == "managed":
        return speech_engine(dict(cfg, backend="managed"), pro, feature)
    if backend == "local":
        res = _local_speech(feature, model)
        if downloaded is not None and not downloaded(model):
            res.update(ready=False, problem="needs_download")
        return res
    return speech_engine(cfg, pro, feature)


def smart_speech_rows(cfg=None, pro=None):
    """The rows of Smart Actions' "Transcribes with" picker (speech_row dicts): "Same as
    Dictation" (naming the model Dictation uses now, with its tag), then under "On this
    computer" every Whisper model, then under "Gusan Cloud" the model Gusan Cloud transcribes
    with (CLOUD, PRO). Values: "", a Whisper id, "cloud"."""
    cfg = cfg or {}
    _c, pro, _signed = _context(cfg, pro)
    dictation = speech_engine(cfg, pro)
    rows = [_row_for_engine(SMART_SPEECH_SAME, SMART_SPEECH_SAME_LABEL, dictation,
                            show_pro=pro_chip(pro))]
    rows += _local_rows()
    name = CLOUD_PROFILE_NAMES[CLOUD_FAST]
    rows.append(speech_row(SMART_SPEECH_CLOUD, name, "cloud", GROUP_GUSAN_CLOUD,
                           note=CLOUD_PROFILE_NOTES[CLOUD_FAST], pro=True, model=name,
                           show_pro=pro_chip(pro)))
    return rows


def smart_speech_choices(cfg=None, pro=None):
    """[(value, title, line, pro)] for the "Transcribes with" picker, the rows of
    smart_speech_rows in the old shape: ("", "Same as Dictation · Whisper Base", ..., False),
    ("small", "Whisper Small", "Whisper Small · Local", False), ("cloud", "Gemini 2.5 Flash",
    "Gemini 2.5 Flash · Cloud · Pro", True)."""
    return _tuples(smart_speech_rows(cfg, pro))


def set_smart_speech(app_or_cfg, value, *, pro=None, downloaded=None, turn_off_privacy=False):
    """The config change for Smart Actions' "Transcribes with". Same return
    shape as set_speech_source: problem "pro" (cloud, not Pro; upsell
    UPSELL_SPEECH), "privacy_mode" (cloud in Privacy mode; with
    ``turn_off_privacy`` the changes turn it off instead), "needs_download"
    (a Whisper model not on disk, when ``downloaded`` is given) or "unknown".
    Pro is checked before Privacy mode: a Free person gets the Pro window."""
    cfg, pro, _signed_in = _context(app_or_cfg, pro)
    value = str(value or "").strip()
    if value == SMART_SPEECH_CLOUD:
        if not pro:
            return _outcome(problem="pro", upsell=UPSELL_SPEECH)
        if cfg.get("privacy_mode"):
            if not turn_off_privacy:
                return _outcome(problem="privacy_mode")
            return _outcome({"privacy_mode": False, SMART_SPEECH_KEY: value})
    elif value and not hardware.is_speech_id(value):
        return _outcome(problem="unknown")
    elif value and downloaded is not None and not downloaded(value):
        return _outcome(problem="needs_download")
    return _outcome({SMART_SPEECH_KEY: value})


# ── Live Assistant's speech model ────────────────────────────────────────────
# What a live call's pieces (and, for a model on this computer, the re-read after Stop) are
# transcribed with. Qt-free; main.AudioRecorder.start_recording(speech=...) takes
# live_speech_session()'s answer, the Live Assistant page's "Transcribes with" row reads
# live_speech_choices / live_speech_engine and writes through set_live_speech.
POOR_LOCAL_NOTE = ("Whisper on this computer isn't reliable for {language}. "
                   "Gusan Cloud (Pro) or your own Google key does much better.")
POOR_LOCAL_NOTE_PRO = ("Whisper on this computer isn't reliable for {language}. "
                       "Automatic or Gusan Cloud does much better.")
POOR_LOCAL_PIECES_NOTE = ("Whisper on this computer isn't reliable for {language}, so those parts "
                          "may have mistakes.")
POOR_LOCAL_NOTE_PRIVATE = ("Privacy mode keeps transcription on this computer, where Whisper isn't "
                           "reliable for {language}. Turn it off to use Gusan Cloud or your own "
                           "Google key.")
# The same hints when the language has a model of its own on this computer (the Armenian model,
# speech_hy): "download" = it can be downloaded (the row is in the picker, with its download icon),
# "ready" = it is on this computer, so the hint says to pick it (or Automatic, which already would).
POOR_LOCAL_NOTE_HY = ("Whisper on this computer isn't reliable for {language}. The {language} model "
                      "(free, works offline) or Gusan Cloud (Pro) does much better.")
POOR_LOCAL_NOTE_PRO_HY = ("Whisper on this computer isn't reliable for {language}. Automatic, the "
                          "{language} model or Gusan Cloud does much better.")
POOR_LOCAL_NOTE_PRIVATE_HY = ("Privacy mode keeps transcription on this computer, where Whisper isn't "
                              "reliable for {language}. The {language} model (free, works offline) "
                              "does much better.")
POOR_LOCAL_NOTE_READY = ("Whisper on this computer isn't reliable for {language}. Pick the {language} "
                         "model - it's already here.")
# Where there is no Automatic to point at (Dictation, Audio to text with a Whisper pick).
POOR_LOCAL_NOTE_PRO_PLAIN = ("Whisper on this computer isn't reliable for {language}. "
                             "Gusan Cloud does much better.")
POOR_LOCAL_NOTE_PRO_HY_PLAIN = ("Whisper on this computer isn't reliable for {language}. The {language} "
                                "model or Gusan Cloud does much better.")


# A piece in such a language that the cloud could not read is never replaced by Whisper's text
# (main.AudioRecorder): it is tried again, and then a gap. These are the calm lines for the card
# and the Live Assistant page - one per kind of failure (cloud_speech error kinds), the rest of
# the sentence is always the same: Whisper on this computer isn't an option for the language.
GAP_RETRYING = "{language} couldn't be transcribed just now - retrying. Local Whisper isn't reliable for it."
GAP_FINAL = ("{language} couldn't be transcribed just now. Try again in a moment. "
             "Local Whisper isn't reliable for it.")      # dictation: it tried, nothing more will
GAP_OFFLINE = ("{language} couldn't be transcribed - there's no connection. It will keep trying. "
               "Local Whisper isn't reliable for it.")
GAP_SIGNED_OUT = ("{language} couldn't be transcribed - sign in again to use Gusan Cloud. "
                  "Local Whisper isn't reliable for it.")
GAP_NOT_PRO = ("{language} needs Gusan Cloud or your own Google key. "
               "Local Whisper isn't reliable for it.")
GAP_PAUSED = ("{language} couldn't be transcribed - Gusan Cloud isn't available right now. "
              "Local Whisper isn't reliable for it.")
GAP_NO_ENGINE = ("{language} couldn't be transcribed - Gusan Cloud has no model for it right now. "
                 "Local Whisper isn't reliable for it.")
GAP_ENGINE = "Your Mistral key can't transcribe {language}. Pick Gusan Cloud or your own Google key."
GAP_KEY = ("{language} couldn't be transcribed - check your key in Settings. "
           "Local Whisper isn't reliable for it.")
GAP_NOTHING = ("None of the call could be transcribed in {language} just now. "
               "The recording has everything.")
GAP_SUMMARY_ONE = ("One part of the call couldn't be transcribed in {language} and is left out. "
                   "The recording has everything.")
GAP_SUMMARY = ("{count} parts of the call couldn't be transcribed in {language} and are left out. "
               "The recording has everything.")
_GAP_BY_KIND = {
    "no_token": GAP_OFFLINE, "signed_out": GAP_SIGNED_OUT, "pro_required": GAP_NOT_PRO,
    "cap": GAP_PAUSED, "language": GAP_ENGINE, "bad_key": GAP_KEY, "no_credit": GAP_KEY,
    "no_key": GAP_KEY, "no_engine": GAP_NO_ENGINE, "too_large": GAP_PAUSED,
}


def gap_line(language, kind="", final=False):
    """The plain line for a piece that could not be transcribed in ``language`` ("Armenian"),
    ``kind``: the cloud_speech error kind ("server", "no_token", "signed_out", ...). Retryable
    kinds (and any unknown one) say it is being tried again - or, with ``final`` (a dictation:
    it already tried and will not again), to try again in a moment."""
    text = _GAP_BY_KIND.get(str(kind or "")) or (GAP_FINAL if final else GAP_RETRYING)
    return text.format(language=language or "This language")


def gap_nothing(language):
    """The note when every part of a call stayed unread (the cloud never answered)."""
    return GAP_NOTHING.format(language=language or "this language")


def gap_summary(language, count):
    """The note after Stop: ``count`` parts of the call that stayed unread."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return ""
    template = GAP_SUMMARY_ONE if count == 1 else GAP_SUMMARY
    return template.format(language=language or "this language", count=count)


def _has_google_key(cfg):
    return _has_key(cfg, "google_api_key")


def live_speech_language(cfg, language=None):
    """The language a Live Assistant call is transcribed in: ``language`` (a session's own),
    else Call language, else the dictation language, else "auto" - the card's _call_language."""
    cfg = cfg or {}
    return str(language or cfg.get("live_assist_language") or cfg.get("language")
               or "auto").strip() or "auto"


def parse_live_speech(value, cfg=None):
    """(kind, model) of a live_assist_speech_model value. kind: LIVE_AUTO ("" - model ""),
    LIVE_LOCAL (a Whisper id, or the Armenian model's), LIVE_CLOUD (Gusan Cloud, model "fast"),
    LIVE_BEST (Gusan Cloud, model "best"), LIVE_OWN (a Gemini id for your own key; a bare
    "own:google" takes google_stt_model when it is one of GEMINI_SPEECH_MODELS, else the first) or
    LIVE_UNKNOWN (model "")."""
    value = str(value or "").strip()
    if not value:
        return LIVE_AUTO, ""
    if value == LIVE_SPEECH_CLOUD:
        return LIVE_CLOUD, CLOUD_FAST
    if value == LIVE_SPEECH_BEST:
        return LIVE_BEST, CLOUD_BEST
    if value == LIVE_SPEECH_OWN_GOOGLE:
        saved = str((cfg or {}).get("google_stt_model") or "").strip()
        return LIVE_OWN, saved if saved in GEMINI_SPEECH_MODELS else GEMINI_SPEECH_MODELS[0]
    if value.startswith(LIVE_SPEECH_OWN_GOOGLE + ":"):
        model = value[len(LIVE_SPEECH_OWN_GOOGLE) + 1:]
        return (LIVE_OWN, model) if model in GEMINI_SPEECH_MODELS else (LIVE_UNKNOWN, "")
    if hardware.is_speech_id(value):
        return LIVE_LOCAL, value
    return LIVE_UNKNOWN, ""


def live_speech_value(cfg):
    """The saved choice as a picker value ("own:google" becomes "own:google:<model>"); an
    unknown one is Automatic ("")."""
    cfg = cfg or {}
    kind, model = parse_live_speech(cfg.get(LIVE_SPEECH_KEY), cfg)
    if kind == LIVE_OWN:
        return f"{LIVE_SPEECH_OWN_GOOGLE}:{model}"
    if kind in (LIVE_CLOUD, LIVE_BEST):
        return LIVE_SPEECH_CLOUD if kind == LIVE_CLOUD else LIVE_SPEECH_BEST
    return model if kind == LIVE_LOCAL else LIVE_SPEECH_AUTO


def live_speech_pick(cfg, pro, signed_in=None):
    """The picker value really in force: live_speech_value, or "" (Automatic) when the saved pick
    can't run now (Gusan Cloud without Pro or in Privacy mode, your own key removed or in Privacy
    mode) - the box then shows what runs, as Smart Actions' does."""
    cfg = cfg or {}
    _c, pro, signed_in = _context(cfg, pro, signed_in)
    value = live_speech_value(cfg)
    kind, _model = parse_live_speech(value, cfg)
    if kind in (LIVE_CLOUD, LIVE_BEST) and not _cloud_allowed(cfg, pro, signed_in):
        return LIVE_SPEECH_AUTO
    if kind == LIVE_OWN and (cfg.get("privacy_mode") or not _has_google_key(cfg)):
        return LIVE_SPEECH_AUTO
    return value


# What Gusan Cloud answered with this run, per PROFILE asked for ("best" -> the model that really ran):
# the server chooses the engine (transcribe-proxy) and names it in its answer, so the picker's line and
# the card say what RAN - never just the profile that was asked for.
_CLOUD_SEEN = {}


def note_cloud_model(asked, ran):
    """The recorder saw Gusan Cloud answer a request for ``asked`` (a profile) with the model ``ran``."""
    if asked and ran:
        _CLOUD_SEEN[str(asked)] = str(ran)


def cloud_model_seen(asked):
    """The model Gusan Cloud ran for a request for ``asked`` (this run), else ""."""
    return _CLOUD_SEEN.get(str(asked), "")


def live_speech_class(value, cfg=None):
    """The kind of a choice, for usage numbers: "auto" | "local" | "cloud" | "cloud_best" |
    "own_key" | "unknown" (never a model name)."""
    kind, _model = parse_live_speech(value, cfg)
    return "own_key" if kind == LIVE_OWN else kind


def _cloud_allowed(cfg, pro, signed_in):
    """May this take go to Gusan Cloud: signed-in Pro, and Privacy mode off."""
    return bool(pro and signed_in and not (cfg or {}).get("privacy_mode"))


# ── the Armenian model, from the routes' point of view ───────────────────────────
def hy_usable(cfg, language):
    """Is the Armenian model (speech_hy) already on this computer, able to run here, and does it read
    this language setting (only Armenian, or a mix of nothing else)? Disk only: a few stats."""
    try:
        import speech_hy
        return bool(speech_hy.ready() and speech_hy.runtime_ok()
                    and speech_hy.covers(language, speech_langs.mix_languages(cfg)))
    except Exception:
        return False


def hy_offer(cfg, language):
    """What this computer can do about Armenian for ``language``: "ready" (the model is on disk),
    "download" (it is listed and can be downloaded - speech_hy.listed) or "" (nothing: another language,
    hidden until it is available, or no runtime). Disk only."""
    try:
        import speech_hy
        if not speech_hy.listed() or not speech_hy.covers(language, speech_langs.mix_languages(cfg)):
            return ""
        return "ready" if speech_hy.ready() else "download"
    except Exception:
        return ""


def live_speech_session(cfg, pro, language=None, signed_in=None):
    """What a Live Assistant take transcribes with, as the ``speech=`` of main.AudioRecorder.
    start_recording: (backend, model, cloud) or (None, None, None) = like Dictation.
      ("local", "<Whisper id>", None)         a model on this computer (or the Armenian model's id)
      ("managed", None, "fast")               Gusan Cloud, the fast profile
      ("managed", None, "best")               Gusan Cloud, Best accuracy
      ("google", None, "<Gemini id>")         your own Google key
    A cloud pick that can't run now (no Pro, Privacy mode, the key removed) is Automatic, like
    smart_speech_session. Automatic is Dictation's - except for a call in a language local Whisper is
    poor at (speech_langs.LOCAL_WHISPER_POOR) when Dictation would run local Whisper: THE ORDER is
    Gusan Cloud (fast) when allowed, else the language's own model on this computer when it is
    downloaded (the Armenian model - also in Privacy mode), else Dictation's local Whisper with a hint
    (live_speech_note). Offline, a Gusan Cloud piece is read on this computer instead (the recorder's own
    fallback: the Armenian model when it is here, else a gap in such a language), and says so."""
    return _live_route(cfg, pro, language, signed_in)[0]


def _live_route(cfg, pro, language=None, signed_in=None):
    """(live_speech_session's tuple, automatic): ``automatic`` is True when Automatic's own
    rules decided the route (also for a pick that can't run now), False for a pick that runs
    as chosen."""
    cfg = cfg or {}
    _c, pro, signed_in = _context(cfg, pro, signed_in)
    kind, model = parse_live_speech(cfg.get(LIVE_SPEECH_KEY), cfg)
    private = bool(cfg.get("privacy_mode"))
    allowed = _cloud_allowed(cfg, pro, signed_in)
    if kind == LIVE_LOCAL:
        return ("local", model, None), False
    if kind in (LIVE_CLOUD, LIVE_BEST) and allowed:
        return ("managed", None, model), False
    if kind == LIVE_OWN and not private and _has_google_key(cfg):
        return ("google", None, model), False
    # Automatic (also a pick that can't run now)
    lang = live_speech_language(cfg, language)
    if (speech_langs.local_whisper_poor(lang, cfg)
            and (speech_engine(cfg, pro)["kind"] == "local" or _mistral_cannot(cfg, lang))):
        if allowed:
            return ("managed", None, CLOUD_FAST), True
        if hy_usable(cfg, lang):
            return ("local", hardware.HY_MODEL_ID, None), True
    return (None, None, None), True


def _mistral_cannot(cfg, language):
    """Dictation runs on your own Mistral key and Voxtral does not cover the call's language
    (Armenian): Automatic cannot stay on it - every piece would be refused - so it moves the call
    to Gusan Cloud when that is allowed, and says so when it is not (live_speech_note)."""
    cfg = cfg or {}
    return bool(str(cfg.get("backend") or "") == "mistral" and _has_key(cfg, "mistral_api_key")
                and not speech_langs.engine_covers(speech_langs.FAMILY_VOXTRAL, language,
                                                   speech_langs.mix_languages(cfg)))


def poor_language_note(cfg, pro, language, signed_in=None, local_engine=True, automatic=True):
    """The plain hint for a language local Whisper is poor at, when Whisper on this computer would
    transcribe it (Live Assistant, Dictation, Audio to text): "Whisper on this computer isn't reliable
    for Armenian. ..." naming what does better - the language's own model (free, works offline) when it
    is on this computer or can be downloaded, Gusan Cloud, your own Google key. "" for a language
    Whisper reads well, and when ``local_engine`` is False (the cloud or the Armenian model reads it).
    ``automatic``: there is an Automatic choice to point at (the Live Assistant's picker) - else the
    hint names Gusan Cloud and the language's model only."""
    cfg = cfg or {}
    _c, pro, signed_in = _context(cfg, pro, signed_in)
    lang = live_speech_language(cfg, language)
    name = speech_langs.local_whisper_poor(lang, cfg)
    if not name or not local_engine:
        return ""
    offer = hy_offer(cfg, lang)
    if offer == "ready":
        return POOR_LOCAL_NOTE_READY.format(language=name)
    if cfg.get("privacy_mode"):
        return (POOR_LOCAL_NOTE_PRIVATE_HY if offer else POOR_LOCAL_NOTE_PRIVATE).format(language=name)
    if _cloud_allowed(cfg, pro, signed_in):
        if automatic:
            return (POOR_LOCAL_NOTE_PRO_HY if offer else POOR_LOCAL_NOTE_PRO).format(language=name)
        return (POOR_LOCAL_NOTE_PRO_HY_PLAIN if offer else POOR_LOCAL_NOTE_PRO_PLAIN).format(language=name)
    return (POOR_LOCAL_NOTE_HY if offer else POOR_LOCAL_NOTE).format(language=name)


# The Armenian model is picked, but this language is not Armenian (or the model can't be used now): the
# recorder reads such speech with a Whisper model instead - never silently, the page says so.
HY_NOTE_AUTO = "This model reads Armenian only. With Automatic, Whisper reads your speech instead."
HY_NOTE_OTHER = "This model reads Armenian only. {language} is read by Whisper instead."
HY_NOTE_MIX = "This model reads Armenian only. Mixed languages are read by Whisper instead."
HY_NOTE_UNAVAILABLE = "The Armenian model isn't available right now, so Whisper reads your speech."


def hy_language_note(cfg, language):
    """The plain hint for a place that has the Armenian model picked (Dictation, Audio to text, the
    Live Assistant) when it will NOT be the one reading: the language is not Armenian (Automatic can't
    tell the model which language it hears; any other language, or a mix, goes to Whisper), or the
    model is not usable (hidden: no runtime / nowhere to get it). "" when it reads this language."""
    cfg = cfg or {}
    try:
        import speech_hy
        if not speech_hy.listed():
            return HY_NOTE_UNAVAILABLE
        if speech_hy.covers(language, speech_langs.mix_languages(cfg)):
            return ""
    except Exception:
        return ""
    lang = speech_langs.normalize(language, default="auto")
    if lang == "auto":
        return HY_NOTE_AUTO
    if lang == "multi":
        return HY_NOTE_MIX
    return HY_NOTE_OTHER.format(language=speech_langs.language_name(lang))


def live_speech_note(cfg, pro, language=None, signed_in=None, engine=None, report=None):
    """The one plain line when a call would be transcribed by local Whisper in a language it
    is poor at ("Whisper on this computer isn't reliable for Armenian. Gusan Cloud (Pro) or
    your own Google key does much better."; with the Armenian model available it names that too),
    else "". Data-driven: speech_langs.LOCAL_WHISPER_POOR. ``engine``: live_speech_engine's dict when
    the caller has it. ``report``: the recorder's speech_report() of a take that is running: a cloud
    take whose pieces had to go through this computer (the cloud wasn't available) says so for such a
    language too - those parts are Whisper's (not when the Armenian model read them)."""
    cfg = cfg or {}
    _c, pro, signed_in = _context(cfg, pro, signed_in)
    lang = live_speech_language(cfg, language)
    picked_kind, picked_model = parse_live_speech(cfg.get(LIVE_SPEECH_KEY), cfg)
    if picked_kind == LIVE_LOCAL and picked_model == hardware.HY_MODEL_ID:
        return hy_language_note(cfg, lang)           # the Armenian model is picked: Armenian only
    name = speech_langs.local_whisper_poor(lang, cfg)
    if not name:
        return ""
    if (report or {}).get("gap_open"):
        # The cloud could not read the latest piece and this computer must not (Whisper is poor at
        # the language): say so, calmly. It goes by itself when the cloud answers again.
        return gap_line(str(report.get("gap_language") or name), report.get("gap_reason"))
    engine = engine or live_speech_engine(cfg, pro, signed_in, language)
    if engine.get("kind") == "own_key" and engine.get("plain") == "Mistral" and _mistral_cannot(cfg, lang):
        return gap_line(name, "language")            # "Your Mistral key can't transcribe Armenian. Pick ..."
    if engine.get("kind") != "local":
        try:
            pieces = int((report or {}).get("local_pieces") or 0)
        except (TypeError, ValueError):
            pieces = 0
        return POOR_LOCAL_PIECES_NOTE.format(language=name) if pieces > 0 else ""
    # The Armenian model reads it: nothing to warn about.
    if str(engine.get("model_id") or "") == hardware.HY_MODEL_ID:
        return ""
    return poor_language_note(cfg, pro, language, signed_in)


def _cloud_profile_engine(feature, profile, seen=True):
    """The runs_on dict of Gusan Cloud asked for ``profile``: named after the model that answered the
    last time this run when it is known (``seen``: the server decides, so that is the truth), else
    after the profile ("Fast", "Best accuracy"); ``asked_id`` / ``asked_name`` / ``profile`` keep what was
    asked for."""
    asked = CLOUD_PROFILE_NAMES[profile]
    ran = cloud_model_seen(profile) if seen else ""
    name, mid = (model_display_name(ran), ran) if ran else (asked, profile)
    res = _result(feature, "cloud", GUSAN_CLOUD, GUSAN_CLOUD, name, WHERE_CLOUD,
                  f"{GUSAN_CLOUD} · {name}", mid, pro=True)
    res.update(asked_id=profile, asked_name=asked, profile=profile)
    return res


def live_speech_engine(cfg, pro, signed_in=None, language=None, downloaded=None):
    """What transcribes a Live Assistant call: the runs_on dict (feature "live_assistant_speech")
    plus  ``auto`` (the pick is Automatic), ``reason`` ("" | "language": Automatic moved the call off
    Dictation's local Whisper - to Gusan Cloud, or to the Armenian model on this computer - because
    Whisper is poor at the call's language), ``language_name`` (that language when local Whisper is
    poor at it, else "") and ``note`` (live_speech_note). ``downloaded`` (optional) marks a local pick
    not on disk as problem "needs_download"."""
    cfg = cfg or {}
    feature = "live_assistant_speech"
    _c, pro, signed_in = _context(cfg, pro, signed_in)
    (backend, model, cloud), automatic = _live_route(cfg, pro, language, signed_in)
    if backend == "managed":
        res = _cloud_profile_engine(feature, cloud_profile(cloud))
    elif backend == "google":
        name = model_display_name(cloud)
        res = _result(feature, "own_key", OWN_KEY, "Google Gemini", name, WHERE_OWN_KEY,
                      f"Google Gemini · {name} · your own key", cloud)
    elif backend == "local":
        res = _local_speech(feature, model)
        if downloaded is not None and not downloaded(model):
            res.update(ready=False, problem="needs_download")
    else:
        res = speech_engine(cfg, pro, feature)
    lang = live_speech_language(cfg, language)
    res["auto"] = automatic
    res["downgrade"] = ""             # (the server chooses by profile now: whatever ran is simply named)
    res["language_name"] = speech_langs.local_whisper_poor(lang, cfg)
    res["reason"] = "language" if (automatic and backend in ("managed", "local")) else ""
    res["note"] = live_speech_note(cfg, pro, language, signed_in, engine=res)
    return res


def _automatic_engine(cfg, pro, signed_in, language=None):
    """What Automatic would transcribe with right now (a live_speech_engine dict), whatever the
    saved pick is."""
    return live_speech_engine(dict(cfg or {}, **{LIVE_SPEECH_KEY: ""}), pro, signed_in, language)


def live_speech_rows(cfg=None, pro=None, signed_in=None, language=None):
    """The rows of the Live Assistant's "Transcribes with" picker (speech_row dicts), in order:
    Automatic - which names what it would pick right now ("Automatic · Fast", CLOUD, "for Armenian");
    under "On this computer" every Whisper model (LOCAL, "Whisper Small", "Balanced") and, while it is
    listed, the Armenian model (LOCAL, "Armenian only · about 105 MB · free, works offline"); under
    "Gusan Cloud" two profiles, Fast and Best accuracy (CLOUD, PRO: the server chooses the model);
    under "Your own key", once a Google key is saved, one row per GEMINI_SPEECH_MODELS ("Gemini 2.5
    Pro · your Google key", CLOUD). Config values are as ever: "", a Whisper id (or the Armenian
    model's), "cloud", "cloud:best", "own:google:<id>". Privacy mode keeps every row listed (the
    page asks before it turns it off)."""
    cfg = cfg or {}
    _c, pro, signed_in = _context(cfg, pro, signed_in)
    show_pro = pro_chip(pro)
    auto = _automatic_engine(cfg, pro, signed_in, language)
    note = ""
    if auto.get("reason") == "language" and auto.get("language_name"):
        note = f"for {auto['language_name']}"
    else:
        note = "same as Dictation"
    rows = [_row_for_engine(LIVE_SPEECH_AUTO, LIVE_SPEECH_AUTO_LABEL, auto, note=note, show_pro=show_pro)]
    rows += _local_rows()
    for value, profile in ((LIVE_SPEECH_CLOUD, CLOUD_FAST), (LIVE_SPEECH_BEST, CLOUD_BEST)):
        name = CLOUD_PROFILE_NAMES[profile]
        rows.append(speech_row(value, name, "cloud", GROUP_GUSAN_CLOUD, note=CLOUD_PROFILE_NOTES[profile],
                               pro=True, model=name, show_pro=show_pro))
    if _has_google_key(cfg):
        for mid in GEMINI_SPEECH_MODELS:
            name = model_display_name(mid)
            rows.append(speech_row(f"{LIVE_SPEECH_OWN_GOOGLE}:{mid}", name, "own_key", GROUP_OWN_KEY,
                                   detail=OWN_KEY_OWNER["Google Gemini"], note=MODEL_NOTES.get(mid, ""),
                                   model=name, plain="Google Gemini"))
    return rows


def live_speech_choices(cfg=None, pro=None, signed_in=None, language=None):
    """[(value, title, line, pro)] for the Live Assistant's "Transcribes with" picker - the
    same rows as live_speech_rows, in the old shape: ("", "Automatic · Whisper Base", "Automatic
    · Whisper Base · Local", False); ("small", "Whisper Small", "Whisper Small · Local", False);
    ("cloud", "Fast", "Fast · Cloud · Pro", True); ("cloud:best", "Best accuracy", ...); once a Google
    key is saved ("own:google:gemini-2.5-pro", "Gemini 2.5 Pro · your Google key", "Gemini 2.5 Pro ·
    your Google key · Cloud", False)."""
    return _tuples(live_speech_rows(cfg, pro, signed_in, language))


def set_live_speech(app_or_cfg, value, *, pro=None, downloaded=None, turn_off_privacy=False):
    """The config change for the Live Assistant's "Transcribes with". Same return shape as
    set_speech_source / set_smart_speech: problem "pro" (a Gusan Cloud row, not Pro; upsell
    UPSELL_SPEECH - Pro is checked BEFORE Privacy mode, a Free person gets the Pro window),
    "privacy_mode" (a cloud row in Privacy mode; with ``turn_off_privacy`` the changes turn it
    off instead), "needs_key" (your own Google key isn't saved), "needs_download" (a local
    model not on disk - a Whisper model or the Armenian one - when ``downloaded`` is given) or
    "unknown". A bare "own:google" is saved as "own:google:<model>"."""
    cfg, pro, _signed_in = _context(app_or_cfg, pro)
    value = str(value or "").strip()
    kind, model = parse_live_speech(value, cfg)
    if kind == LIVE_UNKNOWN:
        return _outcome(problem="unknown")
    if kind in (LIVE_CLOUD, LIVE_BEST):
        if not pro:
            return _outcome(problem="pro", upsell=UPSELL_SPEECH)
    if kind in (LIVE_CLOUD, LIVE_BEST, LIVE_OWN):
        if cfg.get("privacy_mode"):
            if not turn_off_privacy:
                return _outcome(problem="privacy_mode")
            if kind == LIVE_OWN and not _has_google_key(cfg):
                return _outcome(problem="needs_key")
            return _outcome({"privacy_mode": False, LIVE_SPEECH_KEY: _canonical_live(kind, model, value)})
        if kind == LIVE_OWN and not _has_google_key(cfg):
            return _outcome(problem="needs_key")
    elif kind == LIVE_LOCAL and downloaded is not None and not downloaded(model):
        return _outcome(problem="needs_download")
    return _outcome({LIVE_SPEECH_KEY: _canonical_live(kind, model, value)})


def _canonical_live(kind, model, value):
    return f"{LIVE_SPEECH_OWN_GOOGLE}:{model}" if kind == LIVE_OWN else value


def take_engine(cfg, report, feature="live_assistant_speech"):
    """The runs_on dict of what a take REALLY runs, from the recorder's speech_report() - the
    card's truth, whatever the settings say now (they may have changed since the take began).
    A Gusan Cloud take names the profile it asked for (``report["cloud_model"]``: "fast" / "best";
    None for Dictation's own take = fast); what ANSWERED is the report's ran_model
    (live_speech_status). A take on this computer names its model as the recorder reports it."""
    cfg = cfg or {}
    report = report or {}
    backend = report.get("backend") or "local"
    cloud = report.get("cloud_model")
    if backend == "managed":
        return _cloud_profile_engine(feature, cloud_profile(cloud), seen=False)
    over = {"backend": backend, "privacy_mode": False,
            "whisper_model": report.get("whisper_model") or cfg.get("whisper_model") or "base"}
    if backend == "google" and cloud:
        over["google_stt_model"] = cloud
    return speech_engine(dict(cfg, **over), True, feature)


def ran_names(report):
    """The names of the models that answered this take, from the recorder's speech_report() (the
    server says which one ran: its answer's model): "ElevenLabs Scribe v2", or "GPT Transcribe and
    Voxtral Mini" when two of them answered (the last two), "" when nothing has answered yet."""
    report = report or {}
    models = [str(m) for m in (report.get("ran_models") or []) if m]
    if not models and report.get("ran_model"):
        models = [str(report["ran_model"])]
    names = []
    for m in models:
        n = model_display_name(m)
        if n not in names:
            names.append(n)
    return " and ".join(names[-2:])


def live_speech_status(engine, report=None):
    """The card's line for a take, from its live_speech_engine / speech_engine dict and the
    recorder's speech_report(): "Transcribing with GPT Transcribe · Cloud". It says what RAN, not what
    was asked: Gusan Cloud answers with the model its server chose (named from the answer), and before
    the first answer the line names the profile ("Transcribing with Gusan Cloud · Fast"). Pieces that
    went through this computer because the cloud could not be reached are mentioned. The words after
    the name are the pickers' tag: "Local", "Cloud" (your own key: "your Google key · Cloud")."""
    engine = engine or {}
    report = report or {}
    name = engine.get("model_name") or ""
    kind = engine.get("kind")
    shown_kind = kind if kind in ("local", "cloud", "own_key") else "local"
    cloudish = kind in ("cloud", "own_key")
    notes = []
    line = ""
    if kind == "cloud":
        answered = ran_names(report)
        if answered:
            line = f"Transcribing with {tagged_name(answered, 'cloud')}"
        elif engine.get("profile"):
            line = f"Transcribing with {GUSAN_CLOUD} · {engine.get('asked_name') or name}"
    if cloudish and int(report.get("local_pieces") or 0) > 0:
        notes.append("Some of it went through this computer because the cloud wasn't available")
    if not line:
        if name:
            line = f"Transcribing with {tagged_name(name, shown_kind, engine.get('plain'))}"
        else:
            line = f"Transcribing · {TAG_WORD[TAG_LOCAL if shown_kind == 'local' else TAG_CLOUD]}"
    return line + "".join(". " + n[0].upper() + n[1:] for n in notes) + ("." if notes else "")


# ── Audio to text's model ────────────────────────────────────────────────────
def _managed_provider(cfg):
    """Gusan Cloud's speech provider: "gemini" (the default) or "mistral" (a 1.9 leftover)."""
    provider = str((cfg or {}).get("managed_provider") or "gemini").strip().lower()
    return provider if provider in CLOUD_SPEECH_MODELS else "gemini"


def _has_key(cfg, field):
    return bool(str((cfg or {}).get(field) or "").strip())


def file_model_id(cfg, value=None):
    """The model id an Audio to text value runs (what a job stores until Gusan Cloud says which
    model really ran): "cloud" -> the profile it asks for, "best" (a file asks for accuracy: the
    server picks the engine, cloud_files reports the one that ran), "own:google" -> google_stt_model
    (default gemini-2.5-flash), "own:mistral" -> mistral_stt_model (healed like main), a Whisper id
    (or the Armenian model's) -> itself, anything else (Automatic) -> "". ``value`` None reads cfg
    "file_model". Pro, Privacy mode and keys are file_session's business."""
    cfg = cfg or {}
    value = str((cfg.get(FILE_KEY) if value is None else value) or "").strip()
    if value == FILE_CLOUD:
        return CLOUD_BEST
    if value == FILE_OWN_GOOGLE:
        return str(cfg.get("google_stt_model") or "").strip() or GOOGLE_STT_DEFAULT
    if value == FILE_OWN_MISTRAL:
        return normalize_mistral_model(str(cfg.get("mistral_stt_model") or "").strip())
    return value if hardware.is_speech_id(value) else ""


def file_session(cfg, pro, language=None, signed_in=None):
    """(backend, model) an Audio to text job runs with (cfg "file_model"):
    ("managed", "best") for Gusan Cloud (the best profile; needs Pro and no Privacy mode),
    ("google" | "mistral", model id) for your own key (no Privacy mode, the key saved),
    ("local", model id) for a model on this computer (a Whisper id or the Armenian model's), or
    ("local", None) = Automatic. A cloud pick that can't run now is Automatic too (the same rule as
    smart_speech_session).

    ``language`` (the job's language setting; None = not asked): Automatic for a file in a language
    local Whisper is poor at (Armenian) follows THE ORDER - Gusan Cloud's best profile when it is
    allowed (signed-in Pro, no Privacy mode), else the language's own model when it is downloaded
    (the Armenian model), else Whisper on this computer ("local", None) with a hint (file_note)."""
    cfg = cfg or {}
    value = str(cfg.get(FILE_KEY) or "").strip()
    private = bool(cfg.get("privacy_mode"))
    if value == FILE_CLOUD:
        if pro and not private:
            return ("managed", CLOUD_BEST)
        value = FILE_AUTO
    elif value in _FILE_OWN:
        backend, field = _FILE_OWN[value]
        if not private and _has_key(cfg, field):
            return (backend, file_model_id(cfg, value))
        value = FILE_AUTO
    elif hardware.is_speech_id(value):
        return ("local", value)
    if language is not None:
        _c, pro, signed = _context(cfg, pro, signed_in)
        lang = speech_langs.normalize(language, default="auto")
        if speech_langs.local_whisper_poor(lang, cfg):
            if _cloud_allowed(cfg, pro, signed):
                return ("managed", CLOUD_BEST)
            if hy_usable(cfg, lang):
                return ("local", hardware.HY_MODEL_ID)
    return ("local", None)


def _file_cloud_engine(cfg, value, pro):
    """speech_engine's dict for a cloud Audio to text value, with model_id the model it runs (Gusan
    Cloud: the profile it asks for). The caller checks Pro, Privacy mode and the key."""
    backend = "managed" if value == FILE_CLOUD else _FILE_OWN[value][0]
    mid = file_model_id(cfg, value)
    if backend == "managed":
        return _cloud_profile_engine("audio_to_text", CLOUD_BEST, seen=False)
    over = {"backend": backend, "privacy_mode": False}
    if backend == "google":
        over["google_stt_model"] = mid
    elif backend == "mistral":
        over["mistral_stt_model"] = mid
    res = speech_engine(dict(cfg, **over), pro, "audio_to_text")
    res["model_id"] = mid
    return res


def file_rows(cfg=None, pro=None, language=None, signed_in=None):
    """The rows of Audio to text's Accuracy picker (speech_row dicts): Automatic (LOCAL: the job
    picks the Whisper model that suits this computer; for an Armenian file see file_session), under
    "On this computer" every Whisper model and, while it is listed, the Armenian model, under "Gusan
    Cloud" its best profile (Best accuracy: CLOUD, PRO - the server chooses the model), under "Your
    own key" - once that key is saved - Gemini ("Gemini 2.5 Flash · your Google key", CLOUD) and
    Voxtral ("Voxtral Mini · your Mistral key", CLOUD). Values: "auto", a Whisper id (or the Armenian
    model's), "cloud", "own:google", "own:mistral". Privacy mode keeps them listed (the page greys
    them out). ``language`` (the job's language setting; None = not asked): when Automatic would send
    a file in that language to Gusan Cloud (file_session's order for a language Whisper is poor at)
    its row wears CLOUD and PRO and names the profile ("Automatic · Best accuracy", "for Armenian") -
    never LOCAL for audio that leaves this computer."""
    cfg = cfg or {}
    _c, pro, signed_in = _context(cfg, pro, signed_in)
    rows = [speech_row(FILE_AUTO, FILE_AUTO_LABEL, "local", "", note="Picked for this computer",
                       model=FILE_AUTO_LABEL)]
    if language is not None:
        auto = file_engine(dict(cfg, **{FILE_KEY: FILE_AUTO}), FILE_AUTO, pro, language, signed_in)
        if auto.get("kind") == "cloud":
            name = speech_langs.local_whisper_poor(speech_langs.normalize(language, default="auto"), cfg)
            rows[0] = _row_for_engine(FILE_AUTO, FILE_AUTO_LABEL, auto, note=f"for {name}" if name else "",
                                      show_pro=pro_chip(pro))
    rows[0]["kind"] = "auto"
    rows += _local_rows()
    name = CLOUD_PROFILE_NAMES[CLOUD_BEST]
    rows.append(speech_row(FILE_CLOUD, name, "cloud", GROUP_GUSAN_CLOUD,
                           note=CLOUD_PROFILE_NOTES[CLOUD_BEST], pro=True, model=name,
                           show_pro=pro_chip(pro)))
    for value, (_backend, field) in _FILE_OWN.items():
        if _has_key(cfg, field):
            res = _file_cloud_engine(cfg, value, False)
            rows.append(speech_row(value, res["model_name"], "own_key", GROUP_OWN_KEY,
                                   detail=owner_phrase(res["plain"]),
                                   note=MODEL_NOTES.get(res["model_id"], ""), model=res["model_name"],
                                   plain=res["plain"]))
    return rows


def file_choices(cfg=None, pro=None):
    """[(value, title, line, pro)] for Audio to text's Accuracy picker (the rows of file_rows in
    the old shape): ("auto", "Automatic", "Automatic · Local", False), ("small", "Whisper Small",
    "Whisper Small · Local", False), ("cloud", "Best accuracy", "Best accuracy · Cloud · Pro", True),
    then each saved own key: ("own:google", "Gemini 2.5 Flash · your Google key", ..., False),
    ("own:mistral", "Voxtral Mini · your Mistral key", ..., False)."""
    return _tuples(file_rows(cfg, pro))


def set_file_model(app_or_cfg, value, *, pro=None):
    """The config change for Audio to text's Accuracy picker. Same return
    shape as set_speech_source: problem "privacy_mode" (a cloud value in
    Privacy mode), "pro" (Gusan Cloud, not Pro; upsell UPSELL_SPEECH),
    "needs_key" (that own key isn't saved) or "unknown". The Armenian model is a local pick like a
    Whisper one: it is downloaded when the job starts (or from its row's download)."""
    cfg, pro, _signed_in = _context(app_or_cfg, pro)
    value = str(value or "").strip()
    if value in FILE_CLOUD_VALUES:
        if cfg.get("privacy_mode"):
            return _outcome(problem="privacy_mode")
        if value == FILE_CLOUD and not pro:
            return _outcome(problem="pro", upsell=UPSELL_SPEECH)
        if value in _FILE_OWN and not _has_key(cfg, _FILE_OWN[value][1]):
            return _outcome(problem="needs_key")
    elif value != FILE_AUTO and not hardware.is_speech_id(value):
        return _outcome(problem="unknown")
    return _outcome({FILE_KEY: value})


def resolve_action_engine(cfg, pro, signed_in, action_model=None):
    """(engine id, has_token): main.AppController._resolve_action_engine
    without the network. A signed-in Pro user (who has an access token) runs
    on Gusan Cloud unless they picked a local model or a cloud engine with
    their OWN key."""
    cfg = cfg or {}
    engine = actions.normalize_action_model(
        action_model if action_model is not None else cfg.get("action_model", actions.RULE_BASED_ID))
    token = bool(pro and signed_in)
    if token:
        kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
        has_own_cloud_key = bool(((cfg.get("action_api_key") or "") or (cfg.get("google_api_key") or "")).strip())
        if kind in ("rules", "managed") or (kind == "cloud" and not has_own_cloud_key):
            engine = actions.API_MANAGED_ID
    return engine, token


def _downloaded_fn(downloaded):
    if downloaded is not None:
        return downloaded

    def _on_disk(model_id):
        try:
            return bool(local_llm.model_downloaded(model_id))
        except Exception:
            return False
    return _on_disk


def _local_writer(feature, model_id, on_disk):
    plain, name, size, _mem = LOCAL_WRITERS.get(model_id, (model_id, model_id, "", ""))
    ok = bool(on_disk(model_id))
    return _result(feature, "local", THIS_COMPUTER, plain, name, WHERE_LOCAL,
                   f"{THIS_COMPUTER} · {name}", model_id, size,
                   ready=ok, problem="" if ok else "needs_download")


def _basic(feature):
    return _result(feature, "basic", THIS_COMPUTER, "Basic", "No AI model", WHERE_LOCAL,
                   f"{THIS_COMPUTER} · no AI model yet", actions.RULE_BASED_ID)


def _own_key_writer(feature, engine, cfg):
    info = WRITER_PROVIDERS.get(engine, {"name": engine})
    mid = writer_model_for(engine, cfg) or str(cfg.get("action_api_model") or "")
    name = model_display_name(mid)
    has_key = actions.engine_has_key(engine, cfg)
    label = f"{info['name']} · {name} · your own key" if name else f"{info['name']} · your own key"
    return _result(feature, "own_key", OWN_KEY, info["name"], name, WHERE_OWN_KEY, label, engine,
                   ready=has_key, problem="" if has_key else "needs_key")


def writer_engine(cfg, pro, signed_in, feature="smart_actions", action_model=None, downloaded=None):
    """What writes Smart Actions / meeting notes: the Pro routing above, then
    actions.process's rules (privacy mode keeps cloud engines off; managed
    without a token degrades to the built-in rules)."""
    cfg = cfg or {}
    on_disk = _downloaded_fn(downloaded)
    engine, token = resolve_action_engine(cfg, pro, signed_in, action_model)
    kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
    if cfg.get("privacy_mode") and kind in ("cloud", "managed"):
        return _basic(feature)
    if kind == "managed":
        if token:
            model = CLOUD_SMART_MODEL if feature == "smart_actions" else CLOUD_WRITER_MODEL
            return _result(feature, "cloud", GUSAN_CLOUD, GUSAN_CLOUD, model, WHERE_CLOUD,
                           f"{GUSAN_CLOUD} · {model}", actions.API_MANAGED_ID, pro=True)
        return _basic(feature)
    if kind == "cloud":
        return _own_key_writer(feature, engine, cfg)
    if kind == "local_llm":
        return _local_writer(feature, engine, on_disk)
    return _basic(feature)


def _first_on_disk(on_disk):
    # Same order as actions._first_downloaded_local_model (MODEL_CATALOG order).
    for model_id in local_llm.MODEL_CATALOG:
        if on_disk(model_id):
            return model_id
    return None


def live_engine(cfg, pro, signed_in, downloaded=None):
    """What answers in the Live Assistant: actions.live_engine over the
    resolved engine. Pro always answers on Gusan Cloud (except in privacy
    mode, or with live_assist_writer "local" - "Answers written by: This
    computer"); otherwise an own-key cloud engine or a downloaded local model."""
    cfg = cfg or {}
    on_disk = _downloaded_fn(downloaded)
    feature = "live_assistant"
    engine, token = resolve_action_engine(cfg, pro, signed_in)
    kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
    if cfg.get("privacy_mode") or cfg.get("live_assist_writer") == "local":
        chosen = engine if kind == "local_llm" else _first_on_disk(on_disk)
    elif token:
        return _result(feature, "cloud", GUSAN_CLOUD, GUSAN_CLOUD, CLOUD_LIVE_MODEL, WHERE_CLOUD,
                       f"{GUSAN_CLOUD} · {CLOUD_LIVE_MODEL}", actions.API_MANAGED_ID, pro=True)
    elif (engine == actions.RULE_BASED_ID or kind is None
          or (kind == "cloud" and not actions.engine_has_key(engine, cfg))
          or kind == "managed"):
        chosen = _first_on_disk(on_disk)
    else:
        chosen = engine
    if chosen is None:
        return _result(feature, "none", THIS_COMPUTER, "No AI model", "", WHERE_LOCAL,
                       "No AI model yet", "", ready=False, problem="needs_download")
    if actions.ACTION_MODELS.get(chosen, {}).get("kind") == "cloud":
        return _own_key_writer(feature, chosen, cfg)
    return _local_writer(feature, chosen, on_disk)


def file_engine(cfg, file_model=None, pro=False, language=None, signed_in=None):
    """What converts an Audio to text file (``file_model`` None reads cfg
    "file_model"). Gusan Cloud and own-key picks run as file_session says:
    then model_id is the model it runs (Gusan Cloud: the profile "best" - the model that really ran
    is named once the job has an answer) and model_name its name ("Best accuracy", "Gemini 2.5
    Flash"); a pick that can't run now is Automatic. A Whisper id (or the Armenian model's) runs that
    model; Automatic reads "Auto · on this computer" (the job picks the model,
    file_jobs.pick_auto_model). ``language`` (the job's language setting) makes Automatic follow
    file_session's order for a language Whisper is poor at: the dict then carries auto=True and
    reason="language" and names Gusan Cloud or the Armenian model."""
    cfg = cfg or {}
    value = str((cfg.get(FILE_KEY) if file_model is None else file_model) or "").strip()
    backend, model = file_session(dict(cfg, **{FILE_KEY: value}), pro, language, signed_in)
    picked_cloud = ((value == FILE_CLOUD and backend == "managed")
                    or (value in _FILE_OWN and backend == _FILE_OWN[value][0]))
    if backend != "local":
        if picked_cloud:
            return _file_cloud_engine(cfg, value, pro)
        res = _cloud_profile_engine("audio_to_text", CLOUD_BEST, seen=False)       # Automatic, for the language
        res.update(auto=True, reason="language")
        return res
    if model:
        res = _local_speech("audio_to_text", model)
        if value in ("", FILE_AUTO):
            res.update(auto=True, reason="language")
        return res
    return _result("audio_to_text", "local", THIS_COMPUTER, "Auto", "", WHERE_LOCAL,
                   f"Auto · {WHERE_LOCAL}", FILE_AUTO)


def file_note(cfg, pro=False, language=None, signed_in=None, file_model=None):
    """The plain hint under Audio to text's Accuracy picker when a file in a language Whisper is poor at
    (Armenian) would be converted by Whisper on this computer - Automatic with no cloud and no Armenian
    model on disk, or a Whisper pick - the same words as the Live Assistant's (poor_language_note), naming
    the Armenian model when it can be had. "" for any other language or engine. ``language`` None reads
    cfg "file_language"."""
    cfg = cfg or {}
    lang = speech_langs.normalize(language if language is not None else cfg.get("file_language"),
                                  default="auto")
    eng = file_engine(cfg, file_model, pro, lang, signed_in)
    if eng.get("kind") == "local" and str(eng.get("model_id") or "") == hardware.HY_MODEL_ID:
        return hy_language_note(cfg, lang)           # picked (or Automatic's choice): Armenian only
    whisper = eng.get("kind") == "local"
    return poor_language_note(cfg, pro, lang, signed_in, local_engine=whisper, automatic=False)


def dictation_note(cfg, pro=False, signed_in=None):
    """The plain hint on the Dictation page when Dictation would be transcribed by Whisper on this
    computer in a language it is poor at (the same words as the Live Assistant's), else "". Only the
    language setting of Dictation counts (cfg "language", or the mix of "multi")."""
    cfg = cfg or {}
    lang = speech_langs.normalize(cfg.get("language"), default="auto")
    eng = speech_engine(cfg, pro)
    if eng.get("kind") == "local" and str(eng.get("model_id") or "") == hardware.HY_MODEL_ID:
        return hy_language_note(cfg, lang)           # picked: it reads Armenian only
    whisper = eng.get("kind") == "local"
    return poor_language_note(cfg, pro, lang, signed_in, local_engine=whisper, automatic=False)


def runs_on(app_or_cfg, feature, *, pro=None, signed_in=None, action_model=None,
            downloaded=None, file_model=None, language=None):
    """The "Runs on …" line for ``feature`` (see the module notes). ``language``: the call
    language of "live_assistant_speech" (None = Call language, else the dictation language)."""
    feature = _FEATURE_ALIASES.get(feature, feature)
    if feature not in FEATURES:
        raise ValueError(f"unknown feature: {feature!r}")
    cfg, pro, signed_in = _context(app_or_cfg, pro, signed_in)
    if feature == "dictation":
        return speech_engine(cfg, pro)
    if feature == "smart_actions_speech":
        return smart_speech_engine(cfg, pro, downloaded)
    if feature == "live_assistant_speech":
        return live_speech_engine(cfg, pro, signed_in, language, downloaded)
    if feature == "audio_to_text":
        return file_engine(cfg, file_model, pro, language, signed_in)
    if feature == "live_assistant":
        return live_engine(cfg, pro, signed_in, downloaded)
    return writer_engine(cfg, pro, signed_in, feature, action_model, downloaded)


# ── the configured choice (the selected radio), not what runs ────────────────
def speech_choice(cfg):
    """"local" | "cloud" | "own_key" as set on Speech & AI."""
    backend = (cfg or {}).get("backend") or "local"
    if (cfg or {}).get("privacy_mode"):
        return "local"
    if backend == "managed":
        return "cloud"
    if backend in ("google", "mistral"):
        return "own_key"
    return "local"


def writer_choice(cfg):
    """"cloud" | "local" | "own_key" as set on Speech & AI › AI writer."""
    engine = actions.normalize_action_model((cfg or {}).get("action_model"))
    kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
    if kind == "managed":
        return "cloud"
    if kind == "cloud":
        return "own_key"
    return "local"


def local_writer(cfg):
    """The local AI writer model that is (or would be) used: action_model when
    it is a local one, else the default Small (Qwen 1.5B)."""
    engine = actions.normalize_action_model((cfg or {}).get("action_model"))
    return engine if engine in LOCAL_WRITERS else DEFAULT_LOCAL_WRITER


# ── changing the choice ──────────────────────────────────────────────────────
def _outcome(changes=None, problem="", upsell=""):
    return {"ok": not problem, "changes": dict(changes or {}) if not problem else {},
            "problem": problem, "upsell": upsell}


def set_speech_source(app_or_cfg, source, provider=None, model=None, *, pro=None,
                      downloaded=None, turn_off_privacy=False):
    """The config changes for "Speech runs on": source "local" (model = a
    Whisper id, default the current one), "cloud" (Gusan Cloud, Pro; provider
    "gemini" | "mistral") or "own_key" (provider "mistral" | "google"; the key
    must be saved first). Returns {"ok", "changes", "problem", "upsell"}; the
    page applies changes with app.set_settings."""
    cfg, pro, _signed_in = _context(app_or_cfg, pro)
    privacy = bool(cfg.get("privacy_mode"))
    pre = {"privacy_mode": False} if (privacy and turn_off_privacy) else {}

    if source == "local":
        mid = model or cfg.get("whisper_model") or "base"
        if not hardware.is_speech_id(mid):
            return _outcome(problem="unknown")
        if downloaded is not None and not downloaded(mid):
            return _outcome(problem="needs_download")
        return _outcome({"backend": "local", "whisper_model": mid})

    if source not in ("cloud", "own_key"):
        return _outcome(problem="unknown")
    # Gusan Cloud: Pro is checked before Privacy mode (a Free person gets the
    # Pro window, never a question about a mode they could not use it in).
    if source == "cloud" and not pro:
        return _outcome(problem="pro", upsell=UPSELL_SPEECH)
    if privacy and not turn_off_privacy:
        return _outcome(problem="privacy_mode")

    if source == "cloud":
        prov = str(provider or cfg.get("managed_provider") or "gemini").lower()
        if prov not in CLOUD_SPEECH_MODELS:
            return _outcome(problem="unknown")
        changes = dict(pre, backend="managed", managed_provider=prov)
        if prov == "mistral" and model:
            changes["mistral_stt_model"] = normalize_mistral_model(model)
        return _outcome(changes)

    prov = str(provider or "").lower()
    if prov in ("google", "gemini"):
        if not (cfg.get("google_api_key") or "").strip():
            return _outcome(problem="needs_key")
        return _outcome(dict(pre, backend="google"))
    if prov == "mistral":
        if not (cfg.get("mistral_api_key") or "").strip():
            return _outcome(problem="needs_key")
        return _outcome(dict(pre, backend="mistral",
                             mistral_stt_model=normalize_mistral_model(model or cfg.get("mistral_stt_model"))))
    return _outcome(problem="unknown")


def set_writer(app_or_cfg, choice, model=None, provider=None, *, pro=None, base_url=None,
               downloaded=None, turn_off_privacy=False):
    """The config changes for "AI writer" (Smart Actions, meeting notes, Live
    Assistant): choice "cloud" (Gusan Cloud, Pro), "local" (model = a
    local_llm id, default Small; must be downloaded) or "own_key" (provider =
    an actions.API_* id or "mistral"/"gemini"/"openai"/"anthropic"/
    "cerebras"; model = its model id, default the saved/standard one; save
    the key with api_keys.save_key first). Same return shape as
    set_speech_source."""
    cfg, pro, _signed_in = _context(app_or_cfg, pro)
    privacy = bool(cfg.get("privacy_mode"))
    pre = {"privacy_mode": False} if (privacy and turn_off_privacy) else {}

    if choice == "local":
        mid = model or local_writer(cfg)
        mid = local_llm.normalize_model_id(mid)
        if mid not in LOCAL_WRITERS:
            return _outcome(problem="unknown")
        if not _downloaded_fn(downloaded)(mid):
            return _outcome(problem="needs_download")
        return _outcome({"action_model": mid})

    if choice not in ("cloud", "own_key"):
        return _outcome(problem="unknown")
    if privacy and not turn_off_privacy:
        return _outcome(problem="privacy_mode")

    if choice == "cloud":
        if not pro:
            return _outcome(problem="pro", upsell=UPSELL_WRITER)
        return _outcome(dict(pre, action_model=actions.API_MANAGED_ID))

    pid = _norm_provider(provider)
    info = WRITER_PROVIDERS.get(pid)
    if not info:
        return _outcome(problem="unknown")
    if not actions.engine_has_key(pid, cfg):
        return _outcome(problem="needs_key")
    mid = str(model or "").strip() or writer_model_for(pid, cfg)
    if info["base_url"] == "fixed":
        url = info["default_url"]
    elif info["base_url"] == "editable":
        url = str(base_url if base_url is not None else cfg.get("action_api_base_url") or "").strip()
        if base_url is None and _other_providers_url(pid, url):
            # The saved URL is another provider's (e.g. Mistral's, left by an
            # earlier choice): with URLs under "More options" nobody would see it.
            url = ""
        if pid == actions.API_CEREBRAS_ID and "cerebras" not in url:
            url = info["default_url"]
    else:
        url = ""
    return _outcome(dict(pre, action_model=pid, action_api_provider=action_api.normalize_provider(pid),
                         action_api_model=mid, action_api_base_url=url))
