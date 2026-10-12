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

* main.py can later take MISTRAL_STT_DEFAULT / normalize_mistral_model from
  here (same values; test_engine_choice checks they match).
"""

import actions
import action_api
import hardware
import local_llm

# ── names ────────────────────────────────────────────────────────────────────
GUSAN_CLOUD = "Gusan Cloud"
THIS_COMPUTER = "This computer"
OWN_KEY = "Your own key"
WHERE_LOCAL = "on this computer"
WHERE_CLOUD = "in Gusan Cloud"
WHERE_OWN_KEY = "with your own key"

# "smart_actions" is the Smart Actions WRITER; "smart_actions_speech" is what
# transcribes a Smart Action (its own pick, or the same as Dictation).
FEATURES = ("dictation", "smart_actions", "meeting_notes", "live_assistant", "audio_to_text",
            "smart_actions_speech")
_FEATURE_ALIASES = {
    "speech": "dictation", "meetings": "dictation", "meeting_speech": "dictation",
    "smart": "smart_actions", "notes": "meeting_notes", "live_assist": "live_assistant",
    "files": "audio_to_text", "file": "audio_to_text", "smart_speech": "smart_actions_speech",
}

# Smart Actions' own speech model (cfg key): "" = Same as Dictation,
# "cloud" = Gusan Cloud (Pro, never in Privacy mode), or a hardware.MODEL_INFO
# id (Whisper on this computer).
SMART_SPEECH_KEY = "smart_actions_speech_model"
SMART_SPEECH_SAME = ""
SMART_SPEECH_CLOUD = "cloud"
SMART_SPEECH_SAME_LABEL = "Same as Dictation"

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

# Gusan Cloud speech (transcribe-proxy): managed_provider -> the model it runs.
CLOUD_SPEECH_MODELS = {"gemini": hardware.CLOUD_SPEECH_MODEL, "mistral": "Voxtral Mini"}
CLOUD_WRITER_MODEL = hardware.CLOUD_WRITER_MODEL      # meeting notes on Gusan Cloud
CLOUD_LIVE_MODEL = hardware.CLOUD_LIVE_MODEL          # Live Assistant answers on Gusan Cloud
CLOUD_SMART_MODEL = hardware.CLOUD_SMART_MODEL        # Smart Actions on Gusan Cloud

# Mistral's /audio/transcriptions only takes Voxtral Mini (same as main.py).
MISTRAL_STT_DEFAULT = "voxtral-mini-latest"
MISTRAL_STT_MODELS = {"voxtral-mini-latest", "voxtral-mini-2602", "voxtral-mini-2507"}
GOOGLE_STT_DEFAULT = "gemini-2.5-flash"
# The model ids transcribe-proxy runs for each managed_provider (what a Gusan
# Cloud file job stores as its model; names in MODEL_NAMES).
CLOUD_SPEECH_IDS = {"gemini": "gemini-2.5-flash", "mistral": "voxtral-mini-latest"}


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
}


def model_display_name(model_id):
    """'gemini-2.5-flash' -> 'Gemini 2.5 Flash'; an unknown (custom) id as typed."""
    model_id = str(model_id or "").strip()
    return MODEL_NAMES.get(model_id, model_id)


def speech_plain(model_id):
    """'small' -> 'Balanced' (the id itself for an unknown model)."""
    return SPEECH_MODELS.get(model_id, (model_id,))[0]


def speech_model_name(model_id):
    """'small' -> 'Whisper Small'."""
    info = SPEECH_MODELS.get(model_id)
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
    return {"feature": feature, "kind": kind, "choice": choice, "plain": plain,
            "model_name": model_name, "where": where, "label": label, "model_id": model_id,
            "size": size, "pro": pro, "ready": ready, "problem": problem}


def _local_speech(feature, model_id):
    model_id = model_id or "base"
    plain, name, size, _mem = SPEECH_MODELS.get(model_id, (model_id, speech_model_name(model_id), "", ""))
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
                           f"{GUSAN_CLOUD} · {name}", provider, pro=True)
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
    if value in hardware.MODEL_INFO:
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


def smart_speech_choices(cfg=None):
    """[(value, plain, line, pro)] for the "Transcribes with" picker:
    ("", "Same as Dictation", "Same as Dictation", False), every Whisper model
    ("small", "Balanced", "Balanced · Whisper Small", False), then
    ("cloud", "Gusan Cloud", "Gusan Cloud · Gemini 2.5 Flash", True)."""
    out = [(SMART_SPEECH_SAME, SMART_SPEECH_SAME_LABEL, SMART_SPEECH_SAME_LABEL, False)]
    for mid, info in hardware.MODEL_INFO.items():
        out.append((mid, info.plain, info.label(), False))
    provider = str((cfg or {}).get("managed_provider") or "gemini").lower()
    cloud_model = CLOUD_SPEECH_MODELS.get(provider, CLOUD_SPEECH_MODELS["gemini"])
    out.append((SMART_SPEECH_CLOUD, GUSAN_CLOUD, f"{GUSAN_CLOUD} · {cloud_model}", True))
    return out


def set_smart_speech(app_or_cfg, value, *, pro=None, downloaded=None):
    """The config change for Smart Actions' "Transcribes with". Same return
    shape as set_speech_source: problem "pro" (cloud, not Pro; upsell
    UPSELL_SPEECH), "privacy_mode" (cloud in Privacy mode), "needs_download"
    (a Whisper model not on disk, when ``downloaded`` is given) or "unknown"."""
    cfg, pro, _signed_in = _context(app_or_cfg, pro)
    value = str(value or "").strip()
    if value == SMART_SPEECH_CLOUD:
        if cfg.get("privacy_mode"):
            return _outcome(problem="privacy_mode")
        if not pro:
            return _outcome(problem="pro", upsell=UPSELL_SPEECH)
    elif value and value not in hardware.MODEL_INFO:
        return _outcome(problem="unknown")
    elif value and downloaded is not None and not downloaded(value):
        return _outcome(problem="needs_download")
    return _outcome({SMART_SPEECH_KEY: value})


# ── Audio to text's model ────────────────────────────────────────────────────
def _managed_provider(cfg):
    """Gusan Cloud's speech provider: "gemini" (the default) or "mistral"."""
    provider = str((cfg or {}).get("managed_provider") or "gemini").strip().lower()
    return provider if provider in CLOUD_SPEECH_MODELS else "gemini"


def _has_key(cfg, field):
    return bool(str((cfg or {}).get(field) or "").strip())


def file_model_id(cfg, value=None):
    """The real model id an Audio to text value runs (what a finished job
    stores as its model): "cloud" -> the Gusan Cloud provider's
    ("gemini-2.5-flash" / "voxtral-mini-latest"), "own:google" ->
    google_stt_model (default gemini-2.5-flash), "own:mistral" ->
    mistral_stt_model (healed like main), a Whisper id -> itself, anything
    else (Automatic) -> "". ``value`` None reads cfg "file_model". Pro,
    Privacy mode and keys are file_session's business."""
    cfg = cfg or {}
    value = str((cfg.get(FILE_KEY) if value is None else value) or "").strip()
    if value == FILE_CLOUD:
        return CLOUD_SPEECH_IDS[_managed_provider(cfg)]
    if value == FILE_OWN_GOOGLE:
        return str(cfg.get("google_stt_model") or "").strip() or GOOGLE_STT_DEFAULT
    if value == FILE_OWN_MISTRAL:
        return normalize_mistral_model(str(cfg.get("mistral_stt_model") or "").strip())
    return value if value in hardware.MODEL_INFO else ""


def file_session(cfg, pro):
    """(backend, model) an Audio to text job runs with (cfg "file_model"):
    ("managed", provider) for Gusan Cloud (provider "gemini" | "mistral";
    needs Pro and no Privacy mode), ("google" | "mistral", model id) for your
    own key (no Privacy mode, the key saved), ("local", Whisper id), or
    ("local", None) = Automatic. A cloud pick that can't run now is
    Automatic too (the same rule as smart_speech_session)."""
    cfg = cfg or {}
    value = str(cfg.get(FILE_KEY) or "").strip()
    private = bool(cfg.get("privacy_mode"))
    if value == FILE_CLOUD:
        if pro and not private:
            return ("managed", _managed_provider(cfg))
        return ("local", None)
    if value in _FILE_OWN:
        backend, field = _FILE_OWN[value]
        if not private and _has_key(cfg, field):
            return (backend, file_model_id(cfg, value))
        return ("local", None)
    if value in hardware.MODEL_INFO:
        return ("local", value)
    return ("local", None)


def _file_cloud_engine(cfg, value, pro):
    """speech_engine's dict for a cloud Audio to text value, with model_id the
    real model id. The caller checks Pro, Privacy mode and the key."""
    backend = "managed" if value == FILE_CLOUD else _FILE_OWN[value][0]
    mid = file_model_id(cfg, value)
    over = {"backend": backend, "privacy_mode": False}
    if backend == "google":
        over["google_stt_model"] = mid
    elif backend == "mistral":
        over["mistral_stt_model"] = mid
    res = speech_engine(dict(cfg, **over), pro, "audio_to_text")
    res["model_id"] = mid
    return res


def file_choices(cfg=None):
    """[(value, plain, line, pro)] for Audio to text's Accuracy picker:
    ("auto", "Automatic", "Automatic", False), every Whisper model
    ("small", "Balanced", "Balanced · Whisper Small", False),
    ("cloud", "Gusan Cloud", "Gusan Cloud · Gemini 2.5 Flash", True), then
    each own key once it is saved: ("own:google", "Google Gemini",
    "Google Gemini · Gemini 2.5 Flash · your own key", False) and
    ("own:mistral", "Mistral", "Mistral · Voxtral Mini · your own key",
    False). Privacy mode keeps them listed (the page greys them out)."""
    cfg = cfg or {}
    out = [(FILE_AUTO, FILE_AUTO_LABEL, FILE_AUTO_LABEL, False)]
    for mid, info in hardware.MODEL_INFO.items():
        out.append((mid, info.plain, info.label(), False))
    cloud_model = CLOUD_SPEECH_MODELS[_managed_provider(cfg)]
    out.append((FILE_CLOUD, GUSAN_CLOUD, f"{GUSAN_CLOUD} · {cloud_model}", True))
    for value, (_backend, field) in _FILE_OWN.items():
        if _has_key(cfg, field):
            res = _file_cloud_engine(cfg, value, False)
            out.append((value, res["plain"], res["label"], False))
    return out


def set_file_model(app_or_cfg, value, *, pro=None):
    """The config change for Audio to text's Accuracy picker. Same return
    shape as set_speech_source: problem "privacy_mode" (a cloud value in
    Privacy mode), "pro" (Gusan Cloud, not Pro; upsell UPSELL_SPEECH),
    "needs_key" (that own key isn't saved) or "unknown"."""
    cfg, pro, _signed_in = _context(app_or_cfg, pro)
    value = str(value or "").strip()
    if value in FILE_CLOUD_VALUES:
        if cfg.get("privacy_mode"):
            return _outcome(problem="privacy_mode")
        if value == FILE_CLOUD and not pro:
            return _outcome(problem="pro", upsell=UPSELL_SPEECH)
        if value in _FILE_OWN and not _has_key(cfg, _FILE_OWN[value][1]):
            return _outcome(problem="needs_key")
    elif value != FILE_AUTO and value not in hardware.MODEL_INFO:
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


def file_engine(cfg, file_model=None, pro=False):
    """What converts an Audio to text file (``file_model`` None reads cfg
    "file_model"). Gusan Cloud and own-key picks run as file_session says:
    then model_id is the real model id the job stores and model_name its
    name ("Gemini 2.5 Flash"); a pick that can't run now is Automatic. A
    Whisper id runs that model; Automatic reads "Auto · on this computer"
    (the job picks the model, file_jobs.pick_auto_model)."""
    cfg = cfg or {}
    value = str((cfg.get(FILE_KEY) if file_model is None else file_model) or "").strip()
    backend, model = file_session(dict(cfg, **{FILE_KEY: value}), pro)
    if backend != "local":
        return _file_cloud_engine(cfg, value, pro)
    if model:
        return _local_speech("audio_to_text", model)
    return _result("audio_to_text", "local", THIS_COMPUTER, "Auto", "", WHERE_LOCAL,
                   f"Auto · {WHERE_LOCAL}", FILE_AUTO)


def runs_on(app_or_cfg, feature, *, pro=None, signed_in=None, action_model=None,
            downloaded=None, file_model=None):
    """The "Runs on …" line for ``feature`` (see the module notes)."""
    feature = _FEATURE_ALIASES.get(feature, feature)
    if feature not in FEATURES:
        raise ValueError(f"unknown feature: {feature!r}")
    cfg, pro, signed_in = _context(app_or_cfg, pro, signed_in)
    if feature == "dictation":
        return speech_engine(cfg, pro)
    if feature == "smart_actions_speech":
        return smart_speech_engine(cfg, pro, downloaded)
    if feature == "audio_to_text":
        return file_engine(cfg, file_model, pro)
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
        if mid not in SPEECH_MODELS:
            return _outcome(problem="unknown")
        if downloaded is not None and not downloaded(mid):
            return _outcome(problem="needs_download")
        return _outcome({"backend": "local", "whisper_model": mid})

    if source not in ("cloud", "own_key"):
        return _outcome(problem="unknown")
    if privacy and not turn_off_privacy:
        return _outcome(problem="privacy_mode")

    if source == "cloud":
        if not pro:
            return _outcome(problem="pro", upsell=UPSELL_SPEECH)
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
