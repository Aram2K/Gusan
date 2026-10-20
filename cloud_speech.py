"""Audio to text on a cloud model: one request per piece of a file.

Gusan Cloud (transcribe-proxy, Pro) or the user's own Google Gemini / Mistral
key. cloud_files cuts the file into pieces and calls send() for each one on
its worker threads; this module only builds, sends and reads one request. It
never falls back to this computer and never retries: a failure is a typed
CloudSpeechError and the caller decides (err.retryable, LOCAL_FALLBACK_KINDS).

Qt-free, no numpy; requests and engine_choice are imported lazily so
file_jobs stays light.

* spec = spec_from_cfg(cfg, backend, model_id, language=..., token_fn=...)
  once at job start, with backend/model_id from engine_choice.file_session
  ("managed" + provider, "google" / "mistral" + model id). Pass the live cfg:
  send() re-reads it (Privacy mode, the key) before every piece.
  token_fn is the recorder's get_auth_token (Gusan Cloud only).
* text, language = send(spec, wav_bytes, post=session.post)
  ``language`` is the set language, Voxtral's detection, or None.
* spec.model_id is the real id a finished job stores ("gemini-2.5-flash",
  "voxtral-mini-latest", or the user's own model); spec.model_name is its
  name ("Gemini 2.5 Flash"). spec.spellings is vocabulary.load_terms(cfg)
  for correct_spellings on this computer; it is never sent.
* err.message is one short sentence for the card. Provider text goes to the
  log only, with the key or token blanked out.

Same request words and language rules as dictation (main.AudioRecorder.
_run_google / _run_mistral / _run_managed), except: the Gemini key goes in
the x-goog-api-key header, never the URL, and Gemini 2.5 Flash runs with
thinking off.
"""

import base64
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import speech_langs
import vocabulary

logger = logging.getLogger("transcribe.cloud")

# Same as main.MANAGED_PROXY_URL and main.MISTRAL_LANGS (test_cloud_speech
# checks); main.py keeps its own copies.
PROXY_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/transcribe-proxy"
MISTRAL_LANGS = {"en", "es", "fr", "de", "it", "nl", "pt", "hi", "ar", "ru",
                 "uk", "pl", "tr", "ja", "ko", "zh", "ro", "cs", "el"}
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
    "language": ("{name} doesn't support this language. Choose Detect automatically, "
                 "or pick a model on this computer."),
    "privacy": "Privacy mode is on, so this file stopped. Pick a model on this computer to convert it.",
    "no_key": "Your {name} key was removed. Add it again in Settings, or pick a model on this computer.",
}
# Worth another try (cloud_files backs off): no answer, a server error, a
# busy own key, or an answer that wasn't JSON. Everything else stops the job.
RETRY_KINDS = frozenset({"timeout", "network", "server", "rate_limit", "bad_response"})
# Gusan Cloud can't be used at all. At the first piece the job may go on with
# a model on this computer instead (later in the file it fails).
LOCAL_FALLBACK_KINDS = frozenset({"cap", "pro_required", "no_token"})


def error_message(kind, name="Gusan Cloud"):
    """The card sentence for an error kind ("Couldn't reach Mistral. …")."""
    return MESSAGES.get(kind, _UNFINISHED).format(name=name)


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
    """The Spec for a job. ``backend``: "managed" (Gusan Cloud; ``model_id``
    is the provider "gemini" / "mistral" as engine_choice.file_session gives
    it, or its real id; None reads cfg managed_provider), "google" or
    "mistral" (``model_id`` the model; None reads google_stt_model /
    mistral_stt_model). ``language``: the job's language setting.
    ``token_fn``: returns the Gusan Cloud access token (or None)."""
    import engine_choice as ec
    live = cfg
    cfg = cfg or {}
    engine = _ENGINES.get(str(backend or "").strip().lower())
    if engine is None:
        raise ValueError(f"not a cloud speech backend: {backend!r}")
    language = str(language or "auto").strip() or "auto"
    if language == "multi":
        languages = tuple(speech_langs.mix_languages(cfg))
    else:
        languages = (language,) if language in speech_langs.NAMES else ()
    mid = str(model_id or "").strip()
    api_key = ""
    if engine == "gusan":
        ids = {real: p for p, real in ec.CLOUD_SPEECH_IDS.items()}
        provider = (mid.lower() if mid.lower() in ec.CLOUD_SPEECH_MODELS
                    else ids.get(mid) or _managed_provider(cfg))
        mid = ec.CLOUD_SPEECH_IDS[provider]
        model_name = ec.CLOUD_SPEECH_MODELS[provider]
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
                api_key=api_key, token_fn=token_fn, cfg=live)


# ── one request ──────────────────────────────────────────────────────────────
def _thinking_off(model_id):
    """Gemini 2.5 Flash (and Flash-Lite) can turn thinking off; 2.5 Pro and
    the 3.x models can't take a zero budget."""
    return str(model_id or "").strip().lower().startswith("gemini-2.5-flash")


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
    def err(kind, retry_after=None):
        return CloudSpeechError(kind, error_message(kind, spec.name), retry_after)

    if exc is not None:
        return err("timeout" if _is_timeout(exc) else "network")
    if not status:
        return err("network")
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    low = str(body or "").lower()
    if status == 408:
        return err("timeout")
    if status >= 500:
        return err("server", _retry_after(headers))
    if spec.engine == "gusan":
        if status == 401:
            return err("signed_out")      # fatal at once: no token refresh to try
        if status == 403:
            return err("pro_required")
        if status == 429:
            return err("cap")             # the proxy's only 429 is its daily cap
        return err("bad_request")
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
    if spec.cfg is not None and spec.cfg.get("privacy_mode"):
        raise CloudSpeechError("privacy", error_message("privacy", spec.name))
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
    return parse_response(spec, data)
