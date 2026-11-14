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
            import action_api
            m = action_api.claude_model(m)      # (a retired Claude 3.x answers 404: test its current model)
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


# ── Settings › My API keys: any number of named keys ────────────────────────
# An entry is {"id", "name", "provider" (an engine_choice.WRITER_PROVIDERS id), "model",
# "base_url"} in cfg["api_keys"]; its secret is cfg["api_key_secrets"][id] (main.save_config puts
# that object in the OS keyring). The cards copy the key they use into the slot the engines read
# (KEY_FIELDS), and remember which entry it was (speech_key_id / writer_key_id), so the engines
# never changed. Everything here goes through app.set_settings (one save, one redraw).
import uuid as _uuid

# Set once the keys saved the old way (the three slots) joined the list (slot_migration).
MIGRATED_KEY = "api_keys_migrated"

# The providers a key can be for, in the order the "Provider" list shows them.
ENTRY_PROVIDERS = ("api_mistral", "api_gemini", "api_openai_compatible", "api_anthropic", "api_cerebras")
ENTRY_PROVIDER_LABELS = {
    "api_mistral": "Mistral", "api_gemini": "Google Gemini",
    "api_openai_compatible": "OpenAI or any compatible service", "api_anthropic": "Anthropic Claude",
    "api_cerebras": "Cerebras",
}


def entries(cfg):
    from engine_choice import key_entries
    return key_entries(cfg)


def entry(cfg, key_id):
    for e in entries(cfg):
        if e["id"] == key_id:
            return e
    return None


def secret_of(cfg, key_id):
    from engine_choice import key_secret
    return key_secret(cfg, key_id)


def provider_has_address(provider):
    """True for a provider whose server address can be typed (OpenAI-compatible, Cerebras)."""
    from engine_choice import WRITER_PROVIDERS
    return (WRITER_PROVIDERS.get(provider) or {}).get("base_url") == "editable"


def _clean_entry(key_id, name, provider, model, base_url):
    from engine_choice import WRITER_PROVIDERS
    info = WRITER_PROVIDERS[provider]
    base = str(base_url or "").strip().rstrip("/") if provider_has_address(provider) else ""
    if base == info["default_url"]:
        base = ""
    return {"id": key_id, "name": str(name or "").strip() or info["name"], "provider": provider,
            "model": str(model or "").strip() or info["default"], "base_url": base}


def entry_view(cfg, e):
    """What a key row shows: {"title", "sub" ("Mistral · Ministral 14B"), "masked" ("Key ending
    4f2"), "speech" (it can transcribe), "in_use" ("Speech", "AI writer", both or "")}."""
    from engine_choice import SPEECH_KEY_PROVIDERS, model_display_name
    key = secret_of(cfg, e["id"])
    ending = key[-3:] if len(key) >= 8 else ""
    uses = []
    if (cfg or {}).get("speech_key_id") == e["id"] and (cfg or {}).get("backend") in ("mistral", "google"):
        uses.append("Speech")
    from engine_choice import writer_choice
    if (cfg or {}).get("writer_key_id") == e["id"] and writer_choice(cfg) == "own_key":
        uses.append("AI writer")
    provider = ENTRY_PROVIDER_LABELS.get(e["provider"], e["provider"])
    if e["provider"] == "api_openai_compatible":
        provider = "OpenAI-compatible"
    return {"title": e.get("name") or provider,
            "sub": "%s · %s" % (provider, model_display_name(e.get("model") or "")),
            "masked": ("Key ending %s" % ending) if ending else ("Key saved" if key else "No key"),
            "speech": e["provider"] in SPEECH_KEY_PROVIDERS, "in_use": " and ".join(uses)}


def _apply(app, changes):
    set_settings = getattr(app, "set_settings", None)
    if callable(set_settings):
        res = set_settings(changes) or {}
        return all(bool(res.get(k, False)) for k in changes)
    app.cfg.update(changes)
    save = getattr(app, "save_config", None)
    if callable(save):
        save()
    return True


def _active_changes(cfg, e, secret):
    """The slot copies for an entry the cards use now (its key or model changed)."""
    from engine_choice import SPEECH_KEY_PROVIDERS, WRITER_PROVIDERS
    out = {}
    backend = SPEECH_KEY_PROVIDERS.get(e["provider"])
    if cfg.get("speech_key_id") == e["id"] and backend and cfg.get("backend") == backend:
        out["google_api_key" if backend == "google" else "mistral_api_key"] = secret
    if cfg.get("writer_key_id") == e["id"]:
        out[WRITER_PROVIDERS[e["provider"]]["key_field"]] = secret
        out.update(action_model=e["provider"], action_api_model=e["model"], action_api_base_url=e["base_url"])
        try:
            import action_api
            out["action_api_provider"] = action_api.normalize_provider(e["provider"])
        except Exception:
            pass
    return out


def add_entry(app, name, provider, model, key, base_url=""):
    """Save a new key. Returns its id, or "" when it couldn't be saved."""
    from engine_choice import WRITER_PROVIDERS
    key = str(key or "").strip()
    if provider not in WRITER_PROVIDERS or not key:
        return ""
    key_id = _uuid.uuid4().hex[:12]
    e = _clean_entry(key_id, name, provider, model, base_url)
    owner_changed = _stamp_owner(app)
    lst = [dict(x) for x in entries(app.cfg)] + [e]
    secrets = dict(app.cfg.get("api_key_secrets") or {})
    secrets[key_id] = key
    changes = {"api_keys": lst, "api_key_secrets": secrets}
    changes.update(_fill_empty_slot(app.cfg, provider, key))
    ok = _apply(app, changes)
    if owner_changed:
        save = getattr(app, "schedule_config_save", None)
        if callable(save):
            save()
    return key_id if ok else ""


def _fill_empty_slot(cfg, provider, key):
    """A Mistral / Google key also fills its slot while that one is empty, as the old key rows did:
    the pages that only look at the slot (Live Assistant, Audio to text: "your own key") see it."""
    from engine_choice import SPEECH_KEY_PROVIDERS
    backend = SPEECH_KEY_PROVIDERS.get(provider)
    field = {"mistral": "mistral_api_key", "google": "google_api_key"}.get(backend)
    if field and key and not str(cfg.get(field) or "").strip():
        return {field: key}
    return {}


def update_entry(app, key_id, name, provider, model, key=None, base_url=""):
    """Change a saved key (``key`` None or "": keep the secret). A key in use is copied again."""
    from engine_choice import WRITER_PROVIDERS
    old = entry(app.cfg, key_id)
    if old is None or provider not in WRITER_PROVIDERS:
        return False
    e = _clean_entry(key_id, name, provider, model, base_url)
    lst = [dict(e) if x["id"] == key_id else dict(x) for x in entries(app.cfg)]
    secrets = dict(app.cfg.get("api_key_secrets") or {})
    old_secret = secret_of(app.cfg, key_id)
    if str(key or "").strip():
        secrets[key_id] = str(key).strip()
    changes = {"api_keys": lst, "api_key_secrets": secrets}
    new_secret = secrets.get(key_id, "")
    if old_secret and new_secret != old_secret:
        for field in KEY_FIELDS:          # a slot filled with this key (added, or used by a card)
            if str(app.cfg.get(field) or "").strip() == old_secret:
                changes[field] = new_secret
    changes.update(_active_changes(app.cfg, e, new_secret))
    return _apply(app, changes)


def remove_entry(app, key_id):
    """Forget a saved key. A card that used it stops: speech goes back to this computer, the AI
    writer's key slot is emptied (it then says it needs a key)."""
    from engine_choice import SPEECH_KEY_PROVIDERS, WRITER_PROVIDERS
    e = entry(app.cfg, key_id)
    if e is None:
        return False
    cfg = app.cfg
    lst = [dict(x) for x in entries(cfg) if x["id"] != key_id]
    secrets = {k: v for k, v in (cfg.get("api_key_secrets") or {}).items() if k != key_id}
    changes = {"api_keys": lst, "api_key_secrets": secrets}
    backend = SPEECH_KEY_PROVIDERS.get(e["provider"])
    if cfg.get("speech_key_id") == key_id:
        changes["speech_key_id"] = ""
        if backend and cfg.get("backend") == backend:
            field = "google_api_key" if backend == "google" else "mistral_api_key"
            changes[field] = ""
            changes.update(_speech_fallback(cfg, field))
    if cfg.get("writer_key_id") == key_id:
        changes["writer_key_id"] = ""
        changes[WRITER_PROVIDERS[e["provider"]]["key_field"]] = ""
    # Every slot still holding this key (filled when it was added, or by a card): emptied too,
    # so a removed key is never used again (and never moved back into the list).
    secret = secret_of(cfg, key_id)
    for field in KEY_FIELDS:
        if secret and str(cfg.get(field) or "").strip() == secret and field not in changes:
            changes[field] = ""
            changes.update(_speech_fallback(cfg, field))
    return _apply(app, changes)


def slot_migration(cfg):
    """The changes that turn keys saved the old way (one Mistral, one Google, one AI-writer slot)
    into entries of the list, once: each slot key no entry holds yet becomes one, and a card that
    runs on it remembers it. {} when there is nothing to move."""
    import actions
    from engine_choice import WRITER_PROVIDERS, _norm_provider, writer_choice
    cfg = cfg or {}
    if cfg.get(MIGRATED_KEY):
        return {}                         # once only: a key removed from the list never comes back
    lst = [dict(x) for x in entries(cfg)]
    secrets = dict(cfg.get("api_key_secrets") or {})
    held = {v for v in secrets.values() if v}
    changes = {}

    def add(provider, key, name, model="", base=""):
        key_id = _uuid.uuid4().hex[:12]
        lst.append(_clean_entry(key_id, name, provider, model, base))
        secrets[key_id] = key
        held.add(key)
        return key_id

    def find(key):
        for x in lst:
            if secrets.get(x["id"]) == key:
                return x["id"]
        return ""

    for field, provider, backend in (("mistral_api_key", actions.API_MISTRAL_ID, "mistral"),
                                     ("google_api_key", actions.API_GEMINI_ID, "google")):
        key = str(cfg.get(field) or "").strip()
        if not key:
            continue
        key_id = find(key) or add(provider, key, WRITER_PROVIDERS[provider]["name"])
        if cfg.get("backend") == backend and not cfg.get("speech_key_id"):
            changes["speech_key_id"] = key_id
    if writer_choice(cfg) == "own_key" and not cfg.get("writer_key_id"):
        pid = _norm_provider(cfg.get("action_model"))
        info = WRITER_PROVIDERS.get(pid)
        if info:
            key = str(cfg.get(info["key_field"]) or "").strip() or \
                  str(cfg.get(info.get("key_fallback") or "") or "").strip()
            if key:
                key_id = find(key) or add(pid, key, info["name"], cfg.get("action_api_model"),
                                          cfg.get("action_api_base_url"))
                changes["writer_key_id"] = key_id
    elif str(cfg.get("action_api_key") or "").strip() and not find(str(cfg["action_api_key"]).strip()):
        pid = _norm_provider(cfg.get("action_api_provider")) or actions.API_OPENAI_ID
        if pid in WRITER_PROVIDERS:
            add(pid, str(cfg["action_api_key"]).strip(), WRITER_PROVIDERS[pid]["name"],
                cfg.get("action_api_model"), cfg.get("action_api_base_url"))
    if len(lst) != len(entries(cfg)):
        changes.update(api_keys=lst, api_key_secrets=secrets)
    if changes:
        changes[MIGRATED_KEY] = True      # done: a key removed from the list later stays removed
    return changes


def test_entry(provider, key, model="", base_url=""):
    """Test a key for the list's editor (blocking - call on a worker). Mistral checks it can
    transcribe too (Voxtral) - the speech card may use it; the rest ping the provider's chat."""
    import actions
    if provider == actions.API_MISTRAL_ID:
        return test_mistral(key)
    return test_writer_key(provider, key, base_url or None, model or None)
