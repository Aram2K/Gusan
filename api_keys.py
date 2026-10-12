"""Your own API keys (Gusan 2.0 · Settings › Speech & AI › My API keys).

Integration notes for Wave 4
----------------------------
* Save / remove a key:  api_keys.save_key(app, "mistral" | "google" |
  "writer" (or the cfg key), value)  /  api_keys.clear_key(app, field).
  It stamps cfg["secrets_owner"] with app._user_secret_id() (the keys belong
  to the signed-in account; settings_apply refuses that key from pages, so
  this module writes it in place), then hands the key to app.set_settings,
  which saves ~250 ms later through main.save_config - that moves the key
  into the OS keyring. Removing the key that speech runs on also moves speech
  back to this computer (dictation already falls back at runtime), and Audio
  to text's model back to Automatic when it used that key.
  Returns True when saved.
* Show a key:  key_view(app.cfg, field) -> {"has_key", "ending", "text"}
  ("Key ending 4f2" / "No key yet"). Never show the whole key.
* Test a key (network, so on a worker):  test_mistral(key, model),
  test_gemini(key) (key in the x-goog-api-key header, never in the URL),
  test_writer_key(provider, key, base, model) or test_key(field, key, cfg).
  Each returns (ok, message) with a short, plain message ("Works", "That key
  isn't valid. …").
* The AI writer's key row: the key lives in
  engine_choice.WRITER_PROVIDERS[provider]["key_field"] - Gemini shares the
  Google speech key ("google"), Mistral may fall back to the Voxtral key;
  the others use "writer" (action_api_key).
* Threads:  run_then(fn, *args, then=callback) runs fn on a daemon thread and
  calls then(result) back on the GUI thread (Qt, imported lazily; one relay
  QObject for the whole app), e.g.
      api_keys.run_then(api_keys.test_gemini, key, then=self._on_tested)
  A page that may close before the answer arrives should check it still
  exists in the callback. run_in_background(fn, *args, on_done=cb) is the
  Qt-free version (cb runs on the worker thread). account_actions,
  history_actions and updater use these.
* Account switch: after main._reconcile_user_secrets swaps keys, pages showing
  keys must re-read key_view: _on_auth_changed (main.py) calls
  main_win.refresh_account(), and the Settings page re-reads its key rows there.
* main.cloud_error_message can become `from api_keys import cloud_error_message`
  (same wording; test_api_keys checks they match).
"""

import io
import logging
import math
import struct
import threading
import wave

logger = logging.getLogger("transcribe")

KEY_FIELDS = ("mistral_api_key", "google_api_key", "action_api_key")
_FIELD_ALIASES = {
    "mistral": "mistral_api_key", "google": "google_api_key", "gemini": "google_api_key",
    "writer": "action_api_key", "action": "action_api_key",
}
KEY_NAMES = {"mistral_api_key": "Mistral", "google_api_key": "Google Gemini",
             "action_api_key": "AI writer"}

MISTRAL_STT_URL = "https://api.mistral.ai/v1/audio/transcriptions"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

OK_TEXT = "Works"
EMPTY_TEXT = "Paste a key first."


def key_field(field):
    """'mistral' / 'google' / 'writer' or a cfg key -> the cfg key."""
    f = str(field or "").strip()
    f = _FIELD_ALIASES.get(f.lower(), f)
    if f not in KEY_FIELDS:
        raise ValueError(f"not an API key field: {field!r}")
    return f


# ── saving ───────────────────────────────────────────────────────────────────
def _stamp_owner(app):
    """Tag the keys as this account's (ui/settings.py _mark_secrets_owner).
    Signed in but the account not known yet (offline launch): leave it - the
    guest bucket would be the wrong owner. Returns True when it changed."""
    auth = getattr(app, "auth", None)
    if auth is not None and getattr(auth, "is_authenticated", False) and not getattr(auth, "user_id", None):
        return False
    fn = getattr(app, "_user_secret_id", None)
    if not callable(fn):
        return False
    try:
        owner = fn()
    except Exception:
        return False
    if not owner or app.cfg.get("secrets_owner") == owner:
        return False
    app.cfg["secrets_owner"] = owner
    return True


def _speech_fallback(cfg, cfg_key):
    """Removing the key speech runs on: back to this computer (Dictation's
    backend, and Audio to text's model back to Automatic when it used it)."""
    from engine_choice import FILE_AUTO, FILE_KEY, FILE_OWN_GOOGLE, FILE_OWN_MISTRAL
    backend = cfg.get("backend")
    file_model = str(cfg.get(FILE_KEY) or "").strip()
    out = {}
    if (cfg_key == "mistral_api_key" and backend == "mistral") or \
            (cfg_key == "google_api_key" and backend == "google"):
        out["backend"] = "local"
    if (cfg_key == "mistral_api_key" and file_model == FILE_OWN_MISTRAL) or \
            (cfg_key == "google_api_key" and file_model == FILE_OWN_GOOGLE):
        out[FILE_KEY] = FILE_AUTO
    return out


def save_key(app, field, value):
    """Save one API key now (see the module notes). True when saved."""
    cfg_key = key_field(field)
    value = str(value or "").strip()
    owner_changed = _stamp_owner(app)
    changes = {cfg_key: value}
    if not value:
        changes.update(_speech_fallback(app.cfg, cfg_key))
    set_settings = getattr(app, "set_settings", None)
    if callable(set_settings):
        results = set_settings(changes) or {}
        ok = bool(results.get(cfg_key, False))
        if owner_changed and app.cfg.get(cfg_key) == value:
            # The key itself didn't change, so nothing was scheduled: the owner still must be saved.
            save = getattr(app, "schedule_config_save", None)
            if callable(save):
                save()
        return ok
    app.cfg.update(changes)          # an older controller without set_settings
    app.save_config()
    return True


def clear_key(app, field):
    return save_key(app, field, "")


def key_view(cfg, field):
    """What the key row shows: {"field", "name", "has_key", "ending", "text"}."""
    cfg_key = key_field(field)
    key = str((cfg or {}).get(cfg_key) or "").strip()
    ending = key[-3:] if len(key) >= 8 else ""
    if not key:
        text = "No key yet"
    elif ending:
        text = f"Key ending {ending}"
    else:
        text = "Key saved"
    return {"field": cfg_key, "name": KEY_NAMES[cfg_key], "has_key": bool(key),
            "ending": ending, "text": text}


# ── testing ──────────────────────────────────────────────────────────────────
def cloud_error_message(provider, status, body_text):
    """A cloud provider's HTTP error as one clear sentence (same wording as
    main.cloud_error_message)."""
    import json
    msg = ""
    try:
        j = json.loads(body_text)
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict):
                msg = err.get("message", "") or ""
            elif isinstance(err, str):
                msg = err
            if not msg:
                msg = j.get("message") or j.get("detail") or ""
            if isinstance(msg, (dict, list)):
                msg = str(msg)
    except Exception:
        msg = ""
    msg = (msg or (body_text or "")).strip()
    low = msg.lower()
    if (
        status in (401, 403)
        or "unauthorized" in low
        or "invalid api key" in low
        or "api key not valid" in low
        or ("api key" in low and "not valid" in low)
        or ("api key" in low and "invalid" in low)
    ):
        return f"{provider} API key is invalid or unauthorized. Check it in Settings."
    if status == 402 or "insufficient" in low or "out of credit" in low or "no credit" in low or "billing" in low:
        return f"Your {provider} account is out of credits. Add billing/credits to keep using it."
    if status == 429 or "rate limit" in low or "quota" in low or "resource_exhausted" in low or "exhausted" in low:
        return f"{provider} rate limit or quota reached. Wait a moment and try again."
    if status == 400 and "language" in low:
        return f"{provider} does not support the selected language. Pick another or use Auto-detect."
    if status == 400:
        return f"{provider} rejected the request: {msg[:90]}" if msg else f"{provider}: bad request."
    return (f"{provider} error (HTTP {status}). {msg[:90]}").strip()


def _requests():
    import requests
    return requests


def _no_connection(name, err):
    logger.info("%s key test: no connection (%s)", name, str(err)[:120])
    return f"Couldn't reach {name}. Check your connection."


def _test_tone_wav(seconds=1.0, rate=16000):
    """About a second of a quiet 220 Hz tone as WAV bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        n = int(rate * seconds)
        wf.writeframes(b"".join(struct.pack("<h", int(800 * math.sin(2 * math.pi * 220 * i / rate)))
                                for i in range(n)))
    return buf.getvalue()


def test_mistral(key, model=None, post=None, timeout=20):
    """Transcribe a second of tone with Voxtral: checks the key AND model
    access AND credits (a plain key check passes when transcription would
    later fail). Blocking - call on a worker."""
    key = str(key or "").strip()
    if not key:
        return False, EMPTY_TEXT
    from engine_choice import normalize_mistral_model
    post = post or _requests().post
    try:
        resp = post(MISTRAL_STT_URL, headers={"Authorization": f"Bearer {key}"},
                    files={"file": ("test.wav", io.BytesIO(_test_tone_wav()), "audio/wav")},
                    data={"model": normalize_mistral_model(model)}, timeout=timeout)
    except Exception as e:
        return False, _no_connection("Mistral", e)
    if resp.status_code == 200:
        return True, OK_TEXT
    return False, cloud_error_message("Mistral", resp.status_code, getattr(resp, "text", "") or "")


def test_gemini(key, get=None, timeout=10):
    """List the Gemini models with the key in the x-goog-api-key header (the
    old test put it in the URL, where it can reach logs and proxies)."""
    key = str(key or "").strip()
    if not key:
        return False, EMPTY_TEXT
    get = get or _requests().get
    try:
        resp = get(GEMINI_MODELS_URL, headers={"x-goog-api-key": key}, timeout=timeout)
    except Exception as e:
        return False, _no_connection("Google", e)
    if resp.status_code == 200:
        return True, OK_TEXT
    return False, cloud_error_message("Google", resp.status_code, getattr(resp, "text", "") or "")


def test_writer_key(provider, key, base=None, model=None, post=None, get=None, timeout=20):
    """Ping an AI-writer provider with its own key (ui/settings.py
    _validate_action_key). provider: an actions.API_* id or "mistral",
    "gemini", "openai", "anthropic", "cerebras"."""
    import actions
    from engine_choice import WRITER_PROVIDERS, _norm_provider
    key = str(key or "").strip()
    if not key:
        return False, EMPTY_TEXT
    pid = _norm_provider(provider) or actions.API_OPENAI_ID
    info = WRITER_PROVIDERS[pid]
    name = info["name"]
    # Only OpenAI-compatible / Cerebras take the user's URL (as set_writer
    # stores it); a stale URL from another provider must not decide the test.
    b = str(base or "").strip().rstrip("/") if info["base_url"] == "editable" else ""
    b = b or info["default_url"]
    m = str(model or "").strip() or info["default"]
    if post is None or get is None:
        req = _requests()
        post = post or req.post
        get = get or req.get
    try:
        if pid == actions.API_GEMINI_ID:
            resp = post(f"{b}/models/{m}:generateContent",
                        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                        json={"contents": [{"parts": [{"text": "ping"}]}],
                              "generationConfig": {"maxOutputTokens": 1}}, timeout=timeout)
        elif pid == actions.API_ANTHROPIC_ID:
            resp = post(f"{b}/messages",
                        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                 "Content-Type": "application/json"},
                        json={"model": m, "max_tokens": 1,
                              "messages": [{"role": "user", "content": "ping"}]}, timeout=timeout)
        else:   # Mistral, Cerebras, OpenAI-compatible: list the models
            resp = get(f"{b}/models", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except Exception as e:
        return False, _no_connection(name, e)
    if 200 <= resp.status_code < 300:
        return True, OK_TEXT
    if resp.status_code in (401, 403):
        return False, f"{name} says this key isn't valid."
    return False, cloud_error_message(name, resp.status_code, getattr(resp, "text", "") or "")


def test_key(field, key, cfg=None):
    """Test the key for a key row: Mistral (with the saved Voxtral model),
    Google Gemini, or the AI writer's provider/base/model from cfg."""
    import actions
    cfg = cfg or {}
    cfg_key = key_field(field)
    if cfg_key == "mistral_api_key":
        return test_mistral(key, cfg.get("mistral_stt_model"))
    if cfg_key == "google_api_key":
        return test_gemini(key)
    engine = actions.normalize_action_model(cfg.get("action_model"))
    if actions.ACTION_MODELS.get(engine, {}).get("kind") != "cloud":
        engine = cfg.get("action_api_provider") or actions.API_OPENAI_ID
    return test_writer_key(engine, key, cfg.get("action_api_base_url"), cfg.get("action_api_model"))


# ── threads ──────────────────────────────────────────────────────────────────
def run_in_background(fn, *args, on_done=None, on_error=None, name="gusan-worker", **kwargs):
    """Run fn(*args, **kwargs) on a daemon thread. on_done(result) /
    on_error(exception) run ON THAT THREAD (no Qt). Returns the thread."""
    def _run():
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            logger.warning("Background task %s failed", getattr(fn, "__name__", fn), exc_info=True)
            if on_error is not None:
                try:
                    on_error(e)
                except Exception:
                    logger.debug("on_error failed", exc_info=True)
            return
        if on_done is not None:
            try:
                on_done(result)
            except Exception:
                logger.debug("on_done failed", exc_info=True)
    t = threading.Thread(target=_run, name=name, daemon=True)
    t.start()
    return t


_RELAY = None
_RELAY_LOCK = threading.Lock()


def _relay():
    """The one relay QObject, living on the GUI (application) thread. A worker
    emits call(callback, value); Qt queues it to the GUI thread, where the
    callback runs. One long-lived object: nothing is created per call, so no
    QObject is ever deleted inside its own slot or from a worker thread."""
    global _RELAY
    with _RELAY_LOCK:
        if _RELAY is None:
            from PySide6.QtCore import QCoreApplication, QObject, Signal

            class _Relay(QObject):
                call = Signal(object, object)

                def __init__(self):
                    super().__init__()
                    self.call.connect(self._run)

                def _run(self, callback, value):
                    try:
                        callback(value)
                    except Exception:
                        logger.warning("run_then callback %s failed",
                                       getattr(callback, "__name__", callback), exc_info=True)

            relay = _Relay()
            qapp = QCoreApplication.instance()
            if qapp is not None and relay.thread() is not qapp.thread():
                relay.moveToThread(qapp.thread())      # first use came from a worker
            _RELAY = relay
    return _RELAY


def run_then(fn, *args, then=None, error=None, name="gusan-worker", **kwargs):
    """Qt: run fn(*args, **kwargs) on a worker thread, then call then(result)
    (or error(exception)) on the GUI thread. Needs the QApplication to exist."""
    relay = _relay()
    on_done = (lambda result: relay.call.emit(then, result)) if then is not None else None
    on_error = (lambda exc: relay.call.emit(error, exc)) if error is not None else None
    return run_in_background(fn, *args, on_done=on_done, on_error=on_error, name=name, **kwargs)
