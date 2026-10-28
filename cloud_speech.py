"""Audio to text on a cloud model: one request per piece of a file.

Gusan Cloud (transcribe-proxy, Pro) or the user's own Google Gemini / Mistral
key. Gusan Cloud has TWO profiles, asked for in the request's "model" field: "fast" (dictation, the
Live Assistant) and "best" (Audio to text); the server picks the engine that runs - its answer's
"model" / X-Gusan-Model header names what REALLY ran (cloud_speech.send_ex), and that name is what the
card, History and a finished file show. cloud_files cuts the file into pieces and calls send() for each one on
its worker threads; this module only builds, sends and reads one request. It
never falls back to this computer and send() never retries: a failure is a typed
CloudSpeechError and the caller decides (err.retryable, LOCAL_FALLBACK_KINDS;
a file's pieces back off in cloud_files, a live piece uses attempt_with_retries
below). An answer that is not in the language asked for - a model that does not
cover it (Voxtral for Armenian), or letters of another script (speech_langs.
answer_problem) - is a failed piece too, kind "off_language", never kept as text.

Qt-free, no numpy; requests and engine_choice are imported lazily so
file_jobs stays light.

* spec = spec_from_cfg(cfg, backend, model_id, language=..., token_fn=...)
  once at job start, with backend/model_id from engine_choice.file_session
  ("managed" + provider, "google" / "mistral" + model id). Pass the live cfg:
  send() re-reads it (Privacy mode, the key) before every piece.
  token_fn is the recorder's get_auth_token (Gusan Cloud only).
* text, language = send(spec, wav_bytes, post=session.post)
  ``language`` is the set language, Voxtral's detection, or None.
* spec.model_id is the id a finished job stores: for Gusan Cloud the profile asked for ("best")
  until a piece answers - cloud_files then reports the model that really ran ("gpt-transcribe",
  "scribe_v2", "voxtral-mini-latest") - or the user's own model ("gemini-2.5-flash"); spec.model_name
  is its name ("Best accuracy" / "Gemini 2.5 Flash"). spec.spellings is vocabulary.load_terms(cfg)
  for correct_spellings on this computer; it is never sent.
* err.message is one short sentence for the card. Provider text goes to the
  log only, with the key or token blanked out.

Same request words and language rules as dictation (main.AudioRecorder.
_run_google / _run_mistral / _run_managed), except: the Gemini key goes in
the x-goog-api-key header, never the URL, and Gemini 2.5 Flash runs with
thinking off.
"""

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import speech_langs
import vocabulary

logger = logging.getLogger("transcribe.cloud")

# Same as main.MANAGED_PROXY_URL (test_cloud_speech checks); main.py keeps its own copy.
# The languages Voxtral covers are speech_langs.MISTRAL_LANGS - ONE table for every engine path.
PROXY_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/transcribe-proxy"
MISTRAL_LANGS = speech_langs.MISTRAL_LANGS
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MISTRAL_URL = "https://api.mistral.ai/v1/audio/transcriptions"

# The words dictation sends Gemini (main.py _run_google); the language and
# vocabulary sentences are added after them.
PROMPT = ("Transcribe this audio verbatim. Output only the exact spoken words with "
          "correct punctuation and capitalization, and nothing else.")

SAMPLE_RATE = 16000                  # cloud_files sends 16 kHz mono WAV
GUSAN_TIMEOUT = (10, 90)             # (connect, read); under Supabase's 150 s idle limit
OWN_KEY_TIMEOUT = (10, 120)
RETRY_AFTER_MAX = 60                 # seconds; a longer Retry-After is capped

NAMES = {"gusan": "Gusan Cloud", "google": "Google Gemini", "mistral": "Mistral"}
_ENGINES = {"managed": "gusan", "gusan": "gusan", "cloud": "gusan",
            "google": "google", "gemini": "google", "own:google": "google",
            "mistral": "mistral", "own:mistral": "mistral"}
_KEY_FIELDS = {"google": "google_api_key", "mistral": "mistral_api_key"}

# ── errors ───────────────────────────────────────────────────────────────────
_REACH = "Couldn't reach {name}. Check your internet connection and try again."
_UNFINISHED = "{name} couldn't finish this file. Try again, or pick a model on this computer."
MESSAGES = {
    "timeout": _REACH,
    "network": _REACH,
    "no_token": _REACH,              # signed in, but the token couldn't be refreshed (offline)
    "server": _UNFINISHED,
    "bad_response": _UNFINISHED,
    "bad_request": _UNFINISHED,
    "cap": _UNFINISHED,
    "rate_limit": ("{name} isn't taking more requests from your key right now. "
                   "Try again later, or pick a model on this computer."),
    "signed_out": "Sign in to use Gusan Cloud, or pick a model on this computer.",
    "pro_required": "Gusan Cloud is part of Pro. Pick a model on this computer to convert this file.",
    "bad_key": "{name} didn't accept your key. Check it in Settings, or pick a model on this computer.",
    "no_credit": "Your {name} account is out of credit. Add credit, or pick a model on this computer.",
    "language": ("{name} doesn't support this language. Pick Gusan Cloud, your own Google key "
                 "or a model on this computer."),
    # Gusan Cloud's own answers (transcribe-proxy): no engine for the language, a piece too big.
    "no_engine": "{name} can't transcribe this language right now. Pick a model on this computer.",
    "too_large": "This piece of audio is too big for {name}. Pick a model on this computer.",
    "busy": "{name} is busy right now. Try again in a moment, or pick a model on this computer.",
    "off_language": ("{name} didn't answer in {language}. Try again, or pick a model on this "
                     "computer."),
    "privacy": "Privacy mode is on, so this file stopped. Pick a model on this computer to convert it.",
    "no_key": "Your {name} key was removed. Add it again in Settings, or pick a model on this computer.",
}
# Worth another try (cloud_files backs off): no answer, a server error, a
# busy own key, or an answer that wasn't JSON. Everything else stops the job.
RETRY_KINDS = frozenset({"timeout", "network", "server", "rate_limit", "bad_response", "busy",
                         "off_language"})
# Gusan Cloud can't be used at all. At the first piece the job may go on with
# a model on this computer instead (later in the file it fails).
LOCAL_FALLBACK_KINDS = frozenset({"cap", "pro_required", "no_token"})


def error_message(kind, name="Gusan Cloud", language=""):
    """The card sentence for an error kind ("Couldn't reach Mistral. …"). ``language``: the
    language's name, for the kinds that name it ("off_language")."""
    return MESSAGES.get(kind, _UNFINISHED).format(name=name, language=language or "that language")


class CloudSpeechError(Exception):
    """A piece couldn't be transcribed. ``kind``: a MESSAGES key; ``message``:
    the card sentence (never provider text, a URL or a key); ``retry_after``:
    seconds the provider asked to wait (capped at RETRY_AFTER_MAX), or None."""

    def __init__(self, kind, message, retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after

    @property
    def retryable(self):
        return self.kind in RETRY_KINDS

    def __repr__(self):
        return f"CloudSpeechError({self.kind!r}, {self.message!r}, retry_after={self.retry_after!r})"


# ── the job's settings ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class Spec:
    """What one file job sends, read once at job start (spec_from_cfg).
    Secrets and the vocabulary stay out of repr (the Spec travels with the
    job item and may reach a log)."""
    engine: str                      # "gusan" | "google" | "mistral"
    model_id: str = ""               # the real id: "gemini-2.5-flash", "voxtral-mini-latest", ...
    label: str = ""                  # "Gusan Cloud · Gemini 2.5 Flash"
    model_name: str = ""             # "Gemini 2.5 Flash"
    provider: str = "gemini"         # "gemini" | "mistral" (Gusan Cloud's managed_provider)
    language: str = "auto"           # "auto" | "multi" | a speech_langs code
    languages: tuple = ()            # the mix for "multi", else the set language, else ()
    vocab_hint: str = field(default="", repr=False)    # Gemini prompt sentence ("" when gated)
    terms: tuple = field(default=(), repr=False)       # sent to Gusan Cloud (() when gated)
    spellings: tuple = field(default=(), repr=False)   # correct_spellings on this computer only
    api_key: str = field(default="", repr=False)
    token_fn: Optional[Callable] = field(default=None, repr=False)
    cfg: Optional[dict] = field(default=None, repr=False, compare=False)   # the live cfg
    # Gusan Cloud only: the profile sent in the request's "model" field - "fast" or "best"
    # (engine_choice.CLOUD_FAST / CLOUD_BEST). The server picks the engine and says which one ran.
    # (An older proxy ignores the field and runs its default.)
    request_model: str = ""

    @property
    def kind(self):
        """"cloud" (Gusan Cloud) or "own_key"."""
        return "cloud" if self.engine == "gusan" else "own_key"

    @property
    def name(self):
        """Who the audio goes to: "Gusan Cloud" | "Google Gemini" | "Mistral"."""
        return NAMES.get(self.engine, self.engine)


def _managed_provider(cfg):
    provider = str((cfg or {}).get("managed_provider") or "gemini").strip().lower()
    return provider if provider in ("gemini", "mistral") else "gemini"


def spec_from_cfg(cfg, backend, model_id=None, *, language="auto", token_fn=None):
    """The Spec for a job. ``backend``: "managed" (Gusan Cloud; ``model_id`` is the profile
    "best" / "fast" as engine_choice.file_session gives it - Audio to text asks for "best" - or any
    older value: "gemini-2.5-pro" is the best profile, "gemini" / "mistral" / a Gemini id / None the
    default one, which for a FILE is "best"), "google" or "mistral" (``model_id`` the model; None
    reads google_stt_model / mistral_stt_model). ``language``: the job's language setting.
    ``token_fn``: returns the Gusan Cloud access token (or None)."""
    import engine_choice as ec
    live = cfg
    cfg = cfg or {}
    engine = _ENGINES.get(str(backend or "").strip().lower())
    if engine is None:
        raise ValueError(f"not a cloud speech backend: {backend!r}")
    # One spelling for every engine: "hye", "HY", "hy-AM" are Armenian; a language nothing
    # knows is detected ("auto") rather than sent as something the engine would refuse.
    language = speech_langs.normalize(language, default="auto")
    if language == "multi":
        languages = tuple(speech_langs.mix_languages(cfg))
    else:
        languages = (language,) if language in speech_langs.NAMES else ()
    mid = str(model_id or "").strip()
    api_key = ""
    request_model = ""
    if engine == "gusan":
        # A file asks Gusan Cloud for its BEST profile unless a value names the fast one; the server
        # chooses the engine (and says which one ran, per piece: cloud_files reports it).
        profile = ec.cloud_profile(mid, default=ec.CLOUD_BEST)
        provider = _managed_provider(cfg) if profile == ec.CLOUD_FAST else "gemini"
        request_model = profile
        mid = profile
        model_name = ec.CLOUD_PROFILE_NAMES[profile]
        label = f"{ec.GUSAN_CLOUD} · {model_name}"
    else:
        if engine == "google":
            provider = "gemini"
            mid = mid or str(cfg.get("google_stt_model") or "").strip() or ec.GOOGLE_STT_DEFAULT
        else:
            provider = "mistral"
            mid = ec.normalize_mistral_model(mid or str(cfg.get("mistral_stt_model") or "").strip())
        model_name = ec.model_display_name(mid)
        label = f"{NAMES[engine]} · {model_name} · your own key"     # as speech_engine words it
        api_key = str(cfg.get(_KEY_FIELDS[engine]) or "").strip()
    return Spec(engine=engine, model_id=mid, label=label, model_name=model_name,
                provider=provider, language=language, languages=languages,
                vocab_hint=vocabulary.cloud_transcription_hint(cfg),
                terms=tuple(vocabulary.cloud_terms(cfg)),
                spellings=tuple(vocabulary.load_terms(cfg)),
                api_key=api_key, token_fn=token_fn, cfg=live, request_model=request_model)


# ── one request ──────────────────────────────────────────────────────────────
def _thinking_off(model_id):
    """Gemini 2.5 Flash (and Flash-Lite) can turn thinking off; 2.5 Pro and
    the 3.x models can't take a zero budget."""
    return str(model_id or "").strip().lower().startswith("gemini-2.5-flash")


# Gemini 2.5 Pro cannot turn thinking off: its smallest valid budget is 128 tokens.
PRO_MIN_THINKING_BUDGET = 128


def thinking_config(model_id):
    """The generationConfig.thinkingConfig that keeps a LIVE transcription fast and
    valid for ``model_id`` (the Live Assistant; main.AudioRecorder._run_google and the
    transcribe-proxy source use the same numbers), or None to send none:
    2.5 Flash / Flash-Lite thinking off (budget 0); 2.5 Pro the smallest budget it
    accepts (PRO_MIN_THINKING_BUDGET - a zero would be refused); the 3.x models none (no
    budget is valid for every one of them, their own default applies). build_request
    (Audio to text) keeps its own rule, _thinking_off."""
    model = str(model_id or "").strip().lower()
    if model.startswith("gemini-2.5-flash"):
        return {"thinkingBudget": 0}
    if model.startswith("gemini-2.5-pro"):
        return {"thinkingBudget": PRO_MIN_THINKING_BUDGET}
    return None


def build_request(spec, wav, token=None):
    """The request for one WAV piece as {"url", ...requests kwargs}. Pure.
    ``token``: the Gusan Cloud access token, or for an own key the key to
    use (None = spec.api_key)."""
    if not isinstance(wav, bytes):
        wav = bytes(wav)
    if spec.engine == "gusan":
        body = {
            "audio": base64.b64encode(wav).decode("ascii"),
            "sample_rate": SAMPLE_RATE,
            "language": spec.language,
            "provider": spec.provider,
            "languages": list(spec.languages),
        }
        # Sent like dictation does; today's proxy ignores it, so spellings are
        # fixed on this computer afterwards (spec.spellings).
        if spec.terms:
            body["vocabulary"] = list(spec.terms)
        # The profile ("fast" / "best"): the proxy picks the engine; a name it doesn't know is the
        # fast profile, and an old proxy ignores the field. Never a Gemini id.
        if spec.request_model:
            body["model"] = spec.request_model
        return {"url": PROXY_URL, "json": body,
                "headers": {"Authorization": f"Bearer {token}"}, "timeout": GUSAN_TIMEOUT}
    key = spec.api_key if token is None else token
    if spec.engine == "google":
        hint = speech_langs.cloud_hint(spec.language, list(spec.languages))
        gen = {"temperature": 0}
        if _thinking_off(spec.model_id):
            gen["thinkingConfig"] = {"thinkingBudget": 0}
        payload = {
            "contents": [{
                "parts": [
                    {"text": PROMPT + hint + spec.vocab_hint},
                    {"inline_data": {"mime_type": "audio/wav",
                                     "data": base64.b64encode(wav).decode("ascii")}},
                ]
            }],
            "generationConfig": gen,
        }
        return {"url": GEMINI_URL.format(model=spec.model_id), "json": payload,
                "headers": {"x-goog-api-key": key}, "timeout": OWN_KEY_TIMEOUT}
    if spec.engine == "mistral":
        data = {"model": spec.model_id}
        # Only a language Voxtral takes; "auto"/"multi" and others (Armenian)
        # are left out so it detects instead of answering 400.
        if spec.language in MISTRAL_LANGS:
            data["language"] = spec.language
        return {"url": MISTRAL_URL, "headers": {"Authorization": f"Bearer {key}"},
                "files": {"file": ("audio.wav", wav, "audio/wav")}, "data": data,
                "timeout": OWN_KEY_TIMEOUT}
    raise ValueError(f"not a cloud speech engine: {spec.engine!r}")


def _set_language(spec):
    return spec.language if spec.language not in ("auto", "multi") else None


def parse_response(spec, data):
    """(text, language) from a 200 answer. ``language`` is the set language,
    Voxtral's detection, or None. No words is ("", ...), not an error: the
    caller counts it."""
    if not isinstance(data, dict):
        return "", _set_language(spec)
    if spec.engine == "google":
        cands = data.get("candidates") or []
        cand = cands[0] if cands and isinstance(cands[0], dict) else {}
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(str(p.get("text") or "") for p in parts
                       if isinstance(p, dict) and not p.get("thought")).strip()
        if not text:
            logger.info("Gemini gave no words (finish %s, block %s)", cand.get("finishReason"),
                        (data.get("promptFeedback") or {}).get("blockReason"))
        return text, _set_language(spec)
    text = str(data.get("text") or "").strip()
    if spec.engine == "mistral":
        detected = data.get("language")
        if isinstance(detected, str) and detected.strip():
            return text, detected.strip()
    # The proxy's "lang" only echoes the language sent ("en" for auto).
    return text, _set_language(spec)


_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def language_problem(spec):
    """"language" when the job's engine cannot be asked for the job's language, else "": your
    own Mistral key (Voxtral) and a language it does not cover - Armenian comes back as
    confident text in another script - or a Mixed languages set that includes one. Gusan Cloud
    decides on its own server (it keeps such a language off Voxtral); Gemini covers them all."""
    if spec.engine == "mistral" and not speech_langs.engine_covers(
            speech_langs.FAMILY_VOXTRAL, spec.language, spec.languages):
        return "language"
    return ""


def answered_model(engine, data, headers=None):
    """The model that really transcribed a piece, as the provider says it ("gemini-2.5-pro"),
    or "" when it doesn't say. ``engine``: "gusan" | "google" | "mistral" (Spec.engine). Gusan
    Cloud: the "model" field of transcribe-proxy's answer (or its X-Gusan-Model header) - an
    older deployment says nothing, and always ran Gemini 2.5 Flash (or Voxtral); Google: the
    answer's "modelVersion"; Mistral: its "model". The caller shows what ran, never what it
    asked for."""
    value = None
    if isinstance(data, dict):
        value = data.get("modelVersion" if engine == "google" else "model")
    if not value and engine == "gusan" and headers:
        try:
            value = headers.get("X-Gusan-Model") or headers.get("x-gusan-model")
        except Exception:
            value = None
    value = str(value or "").strip()
    return value if _MODEL_ID.match(value) else ""


def _retry_after(headers):
    """Retry-After in seconds (capped at RETRY_AFTER_MAX), or None."""
    if not headers:
        return None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        secs = float(str(raw).strip())
    except Exception:
        return None
    if secs != secs or secs < 0:                        # NaN or negative
        return None
    return min(secs, float(RETRY_AFTER_MAX))


def _is_timeout(exc):
    """requests' Timeout (ReadTimeout, ConnectTimeout), urllib3's TimeoutError
    family or the builtin one - by class name, so requests isn't imported."""
    if isinstance(exc, TimeoutError):
        return True
    return any(c.__name__ in ("Timeout", "TimeoutError") for c in type(exc).__mro__)


def _bad_key_text(low):
    return "api_key_invalid" in low or ("api key" in low and (
        "not valid" in low or "invalid" in low or "expired" in low))


def classify(spec, *, status=None, body="", headers=None, exc=None):
    """The CloudSpeechError for a failed request: an exception from post, or
    a non-2xx ``status`` with its ``body`` text and ``headers``. Pure."""
    return classify_failure(spec.engine, spec.name, status=status, body=body, headers=headers,
                            exc=exc)


def proxy_answer(body):
    """(error, detail) of transcribe-proxy's JSON error answer, lower-case: {"error": "stt_failed",
    "detail": "provider_unavailable"} -> ("stt_failed", "provider_unavailable"). ("", "") for anything
    that is not that (an HTML error page of a gateway, nothing). Never raises."""
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    try:
        data = json.loads(body) if isinstance(body, str) and body.lstrip().startswith("{") else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("error") or "").strip().lower(), str(data.get("detail") or "").strip().lower()


# What the proxy's fixed `detail` of a 502 stt_failed means for the caller (kind, a CloudSpeechError key):
#   timeout / network_error / provider_unavailable   an engine did not answer: worth another try
#   rate_limited                                      the engines are busy: wait a little, try again
#   provider_auth / provider_credits                  Gusan Cloud's own accounts can't be used right now
#                                                     (not the person's key): "cap" - paused, not retried
#                                                     at once; the next piece, or later, tries again
#   provider_rejected                                 the audio itself was refused: asking again won't help
#   "no engine for this language"                     no engine can take the language
_PROXY_DETAIL_KIND = {
    "timeout": "server", "network_error": "server", "provider_unavailable": "server",
    "rate_limited": "busy", "provider_auth": "cap", "provider_credits": "cap",
    "provider_rejected": "bad_request", "invalid_audio": "bad_request",
}


def _gusan_kind(status, low, body):
    """The error kind of a failed Gusan Cloud (transcribe-proxy) answer."""
    error, detail = proxy_answer(body)
    if status == 401:
        return "signed_out"               # fatal at once: no token refresh to try
    if status == 403:
        return "pro_required"
    if status == 413 or error == "audio_too_large":
        return "too_large"
    if status == 429:
        if error == "quota_exceeded" or "quota" in low:
            return "cap"                  # the proxy's own 429: it has paused Gusan Cloud
        return "busy"                     # any other 429 (a rate limit): wait, retry
    if error == "stt_not_configured":
        return "cap"                      # no engine has a key on the server: nothing to retry
    if detail in _PROXY_DETAIL_KIND:
        return _PROXY_DETAIL_KIND[detail]
    if "no engine" in detail or "no engine" in low:
        return "no_engine"
    if status >= 500:
        return "server"
    return "bad_request"                  # 400: not audio / bad request


def classify_failure(engine, name, *, status=None, body="", headers=None, exc=None):
    """classify() for an engine ("gusan" | "google" | "mistral") and its name ("Gusan Cloud"),
    without a Spec: main.AudioRecorder's live pieces classify their answers with it. Gusan Cloud's
    answers are read by their fixed codes (proxy_answer): a 502 stt_failed names why in ``detail``."""
    def err(kind, retry_after=None):
        return CloudSpeechError(kind, error_message(kind, name), retry_after)

    if exc is not None:
        return err("timeout" if _is_timeout(exc) else "network")
    if not status:
        return err("network")
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    low = str(body or "").lower()
    if status == 408:
        return err("timeout")
    if engine == "gusan":
        kind = _gusan_kind(status, low, body)
        return err(kind, _retry_after(headers) if kind in ("busy", "server") else None)
    if status >= 500:
        return err("server", _retry_after(headers))
    if status in (401, 403) or (status == 400 and _bad_key_text(low)):
        return err("bad_key")
    if status == 402:
        return err("no_credit")
    if status == 429:
        return err("rate_limit", _retry_after(headers))
    if status in (400, 422) and "language" in low:
        return err("language")
    return err("bad_request")


# ── sending ──────────────────────────────────────────────────────────────────
# Workers ask for the token one at a time, so they never all refresh it at once.
_TOKEN_LOCK = threading.Lock()


def _token(spec):
    with _TOKEN_LOCK:
        try:
            token = spec.token_fn() if callable(spec.token_fn) else None
        except Exception as e:
            logger.info("Gusan Cloud token unavailable (%s)", type(e).__name__)
            token = None
    if not token:
        raise CloudSpeechError("no_token", error_message("no_token", spec.name))
    return token


def _live_key(spec):
    """The own key to send with: re-read from the live cfg (removed = stop)."""
    key = spec.api_key
    if spec.cfg is not None:
        key = str(spec.cfg.get(_KEY_FIELDS[spec.engine]) or "").strip()
    if not key:
        raise CloudSpeechError("no_key", error_message("no_key", spec.name))
    return key


def _scrub(text, *secrets, limit=300):
    """Provider text for the log: the key / token blanked, kept short."""
    text = str(text or "")
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return text[:limit]


def send(spec, wav, *, post=None):
    """Transcribe one WAV piece: (text, language). Raises CloudSpeechError;
    never falls back. ``post``: requests.post or a per-thread
    requests.Session().post (default requests.post). Before sending it checks
    the live cfg: Privacy mode on, or the own key removed, stops the job."""
    text, language, _model = send_ex(spec, wav, post=post)
    return text, language


def send_ex(spec, wav, *, post=None):
    """send() plus the model that ran: (text, language, model) - model is
    answered_model() ("" when the provider doesn't say)."""
    if spec.cfg is not None and spec.cfg.get("privacy_mode"):
        raise CloudSpeechError("privacy", error_message("privacy", spec.name))
    if language_problem(spec):
        raise CloudSpeechError("language", error_message("language", spec.name))
    secret = _token(spec) if spec.engine == "gusan" else _live_key(spec)
    req = build_request(spec, wav, secret)
    if post is None:
        import requests
        post = requests.post
    url = req.pop("url")
    try:
        resp = post(url, **req)
    except Exception as e:
        error = classify(spec, exc=e)
        logger.warning("%s: request failed (%s): %s", spec.name, error.kind,
                       _scrub(f"{type(e).__name__}: {e}", secret))
        raise error from None
    status = getattr(resp, "status_code", 0) or 0
    if not 200 <= status < 300:
        body = getattr(resp, "text", "") or ""
        error = classify(spec, status=status, body=body, headers=getattr(resp, "headers", None))
        logger.warning("%s: HTTP %s (%s): %s", spec.name, status, error.kind, _scrub(body, secret))
        raise error
    try:
        data = resp.json()
    except Exception:
        logger.warning("%s: the answer wasn't JSON: %s", spec.name,
                       _scrub(getattr(resp, "text", ""), secret, limit=120))
        raise CloudSpeechError("bad_response", error_message("bad_response", spec.name)) from None
    text, language = parse_response(spec, data)
    ran = answered_model(spec.engine, data, getattr(resp, "headers", None))
    # An answer that is not in the language that was asked for (a model that doesn't cover it, or
    # letters of another script) is a failed piece - never text the job would keep as speech.
    why = speech_langs.answer_problem(text, spec.language, ran or spec.model_id, spec.languages)
    if why:
        logger.warning("%s: the answer wasn't in the requested language (%s, %s, %d letters)",
                       spec.name, why, speech_langs.script_name(text), len(text))
        err = CloudSpeechError("off_language", error_message(
            "off_language", spec.name, speech_langs.language_name(spec.language)))
        # What was refused and why, for a caller that may still keep it (accept_off_script): a
        # capable model that writes another script twice for the same piece heard other speech.
        err.why, err.model, err.answer = why, ran or spec.model_id, (text, language, ran)
        raise err
    return text, language, ran


# ── live pieces: try again, then a gap ───────────────────────────────────────
# A live piece in a language local Whisper is poor at (Armenian) is never replaced by Whisper's
# text when the cloud fails: it is tried again, and if it still fails it becomes a gap that is
# filled later (main.AudioRecorder). The pure parts live here, so they are tested without audio.
#
# A dead proxy must never hold the ONE transcription worker of a call for a minute: a live piece gets
# TWO tries at most (LIVE_RETRIES = 1 more after the first), each cut at LIVE_ATTEMPT_TIMEOUT seconds,
# and everything - the tries and the wait between them - fits LIVE_PIECE_BUDGET seconds. The next piece
# tries the cloud again by itself. (A piece is a few seconds of speech: the server answers in about a
# second; live_timeout() gives a longer piece - a dictation of minutes - the time it needs.)
LIVE_RETRIES = 1                 # tries after the first one (two in all)
LIVE_BACKOFF = (1.0, 2.5)        # seconds before retry 1 and retry 2
LIVE_WAIT_MAX = 8.0              # one wait never longer: a call moves on, the next piece tries again
LIVE_ATTEMPT_TIMEOUT = 12.0      # seconds one request of a live piece may take
LIVE_PIECE_BUDGET = 20.0         # all the trying and waiting one piece may take, the first try included
LIVE_CONNECT_TIMEOUT = 5.0       # seconds to connect (inside the attempt's own time)


def live_timeout(seconds):
    """The time ONE request of a live piece of ``seconds`` of audio may take: LIVE_ATTEMPT_TIMEOUT for
    a piece up to 30 s (what a call sends: pieces are a few seconds), a little more for a longer one
    (a dictation take), never over 40 s."""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 0.0
    return min(40.0, LIVE_ATTEMPT_TIMEOUT + max(0.0, seconds - 30.0) * 0.3)


def live_budget(timeout):
    """The budget of all the tries of one live piece whose attempts may take ``timeout`` seconds."""
    return max(LIVE_PIECE_BUDGET, float(timeout) + 8.0)


def _sleep(seconds):
    time.sleep(seconds)                 # (a seam: tests replace it, nothing really waits)


def accept_off_script(why, model, off_count):
    """Is an answer in another script to be KEPT instead of failing the piece? Only when a capable
    model (speech_langs.is_capable: GPT transcribe, ElevenLabs Scribe, Gemini - models that cover the
    language) wrote letters of another script, ``why`` == "script", for the SAME piece twice
    (``off_count`` >= 2: the first answer, then the retry): it heard other speech - English names, a
    Russian phrase in an Armenian call - and asking again gets the same words. That is the speaker's own
    mix of languages, not a gap. Voxtral ("engine": it does not cover the language) and an engine that
    did not say what it is keep the failed-piece rule."""
    return bool(why == "script" and off_count >= 2 and speech_langs.is_capable(model))


def retry_wait(err, attempt):
    """Seconds to wait before retry number ``attempt`` (1 or 2) after ``err``, or None for "do
    not retry": the error is not one a retry fixes (signed out, not Pro, paused), or the
    provider asked for a longer wait than a live piece can afford (the next piece tries again)."""
    if not getattr(err, "retryable", False):
        return None
    if getattr(err, "same_again", False) and int(attempt) > 1:
        return None          # a model that covers the language wrote another script: it heard other speech, asking again gets the same words
    base = LIVE_BACKOFF[min(max(int(attempt), 1), len(LIVE_BACKOFF)) - 1]
    asked = getattr(err, "retry_after", None)
    if asked is not None and asked > LIVE_WAIT_MAX:
        return None
    return max(base, float(asked or 0.0))


def attempt_with_retries(do_once, *, retries=LIVE_RETRIES, sleep=None, clock=None, stop=None,
                         budget=LIVE_PIECE_BUDGET, on_retry=None, timeout=None):
    """Run ``do_once()`` (one request: returns a value or raises CloudSpeechError) and try again
    after a retryable failure - up to ``retries`` times, waiting retry_wait() each time (the
    Retry-After the provider sent, when it asked for one). Returns (value, None) on success,
    else (None, the LAST CloudSpeechError). ``stop()`` True ends the trying at once (the take
    was cancelled); ``budget`` seconds is all the trying one piece may take, the first try
    included. ``timeout``: when given, each try is called ``do_once(timeout=seconds)`` with the
    time that try may take - ``timeout`` itself, or what is left of the budget, whichever is less
    (never under 2 s) - so the tries together never outlast the budget. ``sleep`` / ``clock``
    are for tests."""
    sleep = sleep or _sleep
    clock = clock or time.monotonic
    began = clock()
    attempt = 0
    while True:
        try:
            if timeout is None:
                return do_once(), None
            return do_once(timeout=min(float(timeout), max(2.0, budget - (clock() - began)))), None
        except CloudSpeechError as e:
            err = e
        attempt += 1
        wait = retry_wait(err, attempt) if attempt <= retries else None
        # (a try that needs at least 2 s must still fit in what is left after the wait)
        if wait is None or (stop is not None and stop()) or clock() - began + wait > budget - (
                2.0 if timeout is not None else 0.0):
            return None, err
        if on_retry is not None:
            try:
                on_retry(attempt, err, wait)
            except Exception:
                pass
        sleep(wait)
