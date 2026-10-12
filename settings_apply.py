"""Instant-apply settings (Gusan 2.0): what happens when a page changes a setting.

Pages never save config themselves. They call AppController.set_setting(key,
value) / set_settings({...}), which hands the change to apply() here:

1. The value is written into ``app.cfg`` IN PLACE. Never a new dict: the
   AudioRecorder reads main.py's module-level cfg, the same object.
2. The key's side effect runs (the table below), at most once per call even
   when several related keys change together.
3. One debounced save is scheduled (app.schedule_config_save, ~250 ms).
4. app.sig_setting_changed.emit(key) for every key whose value changed, so
   every open page (and later the tray) can refresh.

Side effects
    hotkey               re-register dictation + Live Assistant hotkeys; a
                         dictation hotkey that can't register is rolled back;
                         refused when it equals the Live Assistant hotkey
    live_assist_hotkey   re-register it (rolled back when it can't register);
                         refused when it equals the dictation hotkey
    smart_actions_hotkey the Smart Actions shortcut (default Alt+S; one take
                         runs as a Smart Action - Alt+R always pastes the
                         exact words): re-register it (app._setup_smart_hotkey;
                         rolled back when it can't register); refused when it
                         equals the dictation or Live Assistant hotkey (and
                         those two are refused when they equal it)
    smart_actions_speech_model   what transcribes a Smart Action: "" (same as
                         Dictation), "cloud" (Gusan Cloud; refused in Privacy
                         mode - Pro is checked when a take starts) or a
                         hardware.MODEL_INFO Whisper id; anything else is
                         refused. Never a preload: the one model slot keeps
                         Dictation's model (a Smart take loads its own)
    file_model           Audio to text's model: "auto", a hardware.MODEL_INFO
                         Whisper id, or "cloud" (Gusan Cloud) / "own:google" /
                         "own:mistral" (your own key) - those three refused in
                         Privacy mode; Pro and the key are checked when a job
                         starts. Anything else is refused
    privacy_mode        app.set_privacy_mode (forces the local engine, saves,
                         refreshes tray and windows)
    launch_at_login      autostart.set_enabled; on failure the value becomes
                         what the OS really has (autostart.is_registered)
    backend, whisper_model   preload the local speech model
    overlay_theme, overlay_private   restyle the dictation bar if it is showing
    live_assist_private  apply to an open Live Assistant card
    everything else      just written and saved

No Qt here: ``app`` is anything with those attributes (tests pass a fake).
"""

import logging

import hotkeys

logger = logging.getLogger("transcribe")

HOTKEY = "hotkey"
ASSIST_HOTKEY = "live_assist_hotkey"
SMART_HOTKEY = "smart_actions_hotkey"   # ui/pages/smart_actions.py HOTKEY_KEY
SMART_SPEECH = "smart_actions_speech_model"   # engine_choice.SMART_SPEECH_KEY
SMART_SPEECH_CLOUD = "cloud"                   # engine_choice.SMART_SPEECH_CLOUD
FILE_MODEL = "file_model"                      # engine_choice.FILE_KEY
FILE_AUTO = "auto"                             # engine_choice.FILE_AUTO
FILE_CLOUD_VALUES = ("cloud", "own:google", "own:mistral")   # engine_choice.FILE_CLOUD_VALUES
PRIVACY = "privacy_mode"
LAUNCH_AT_LOGIN = "launch_at_login"     # autostart.CFG_KEY (never in DEFAULT)
PRELOAD_KEYS = ("backend", "whisper_model")
OVERLAY_KEYS = ("overlay_theme", "overlay_private")
LIVE_ASSIST_PRIVATE = "live_assist_private"

# Keys a page must never write: account state the controller owns.
PROTECTED_KEYS = frozenset({
    "secrets_owner", "user_secrets", "last_known_pro", "known_emails", "last_signin_email",
    "pending_update_version", "pending_update_body", "config_schema_version",
})


def norm_hotkey(hk):
    """'Alt + R' -> 'alt+r' (the form the hotkey libraries and config use)."""
    return "+".join(p.strip().lower() for p in str(hk or "").split("+") if p.strip())


def _smart_speech_ok(value, private):
    """A smart_actions_speech_model value a page may write: "" (same as
    Dictation), "cloud" outside Privacy mode, or a known Whisper model id."""
    if value == "":
        return True
    if value == SMART_SPEECH_CLOUD:
        return not private
    import hardware
    return value in hardware.MODEL_INFO


def _file_model_ok(value, private):
    """A file_model value a page may write: "auto", a known Whisper model id,
    or a cloud value outside Privacy mode (Pro and the key are checked when
    a job starts)."""
    if value == FILE_AUTO:
        return True
    if value in FILE_CLOUD_VALUES:
        return not private
    import hardware
    return value in hardware.MODEL_INFO


def _try(label, fn, *args, **kwargs):
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        logger.warning("Setting side effect failed: %s", label, exc_info=True)
        return False, None


def apply(app, updates, autostart_mod=None):
    """Apply ``updates`` ({key: value}) to ``app``. Returns {key: ok}.

    ok is False when the change was refused or its side effect failed (the
    page can then show its old value again); True otherwise, including when
    the value was already set."""
    cfg = app.cfg
    results = {}
    changed = []
    wrote = False
    want = {"hotkey": False, "assist": False, "smart": False, "preload": False, "overlay": False,
            "la_private": False}
    old_hotkey = cfg.get(HOTKEY)
    old_assist = cfg.get(ASSIST_HOTKEY, "")
    old_smart = cfg.get(SMART_HOTKEY, "")
    updates = dict(updates)
    smart_after = norm_hotkey(updates.get(SMART_HOTKEY, cfg.get(SMART_HOTKEY)))
    # Privacy first: it decides whether a cloud backend in the same call is allowed.
    items = sorted(updates.items(), key=lambda kv: kv[0] != PRIVACY)

    for key, value in items:
        if key in PROTECTED_KEYS:
            logger.warning("Refused a page write to %s", key)
            results[key] = False
            continue

        if key == PRIVACY:
            on = bool(value)
            if on == bool(cfg.get(PRIVACY, False)):
                results[key] = True
                continue
            backend_before = cfg.get("backend")
            ok, _ = _try("privacy_mode", app.set_privacy_mode, on, notify=False)
            results[key] = ok and bool(cfg.get(PRIVACY, False)) == on
            if results[key]:
                changed.append(PRIVACY)
                if cfg.get("backend") != backend_before:
                    changed.append("backend")
                    want["preload"] = True      # privacy moved dictation onto the local model
            continue

        if key == LAUNCH_AT_LOGIN:
            if autostart_mod is None:
                import autostart as autostart_mod
            on = bool(value)
            before = cfg.get(LAUNCH_AT_LOGIN)
            ok, done = _try("launch_at_login", autostart_mod.set_enabled, on)
            ok = bool(ok and done)
            if ok:
                keep = on
            else:
                # The setting records whether a login entry exists at all;
                # after a failure that's whatever the OS has, not the ask.
                _, keep = _try("autostart.is_registered", autostart_mod.is_registered)
                keep = bool(keep)
            cfg[LAUNCH_AT_LOGIN] = keep
            wrote = True
            results[key] = ok
            if keep != before:
                changed.append(LAUNCH_AT_LOGIN)
            continue

        if key == "backend" and cfg.get(PRIVACY) and value != "local":
            # Privacy mode means on-device only; turn it off first (privacy_mode
            # in the same call was applied above, so cfg already says).
            results[key] = False
            continue

        if key == ASSIST_HOTKEY:
            value = norm_hotkey(value)
            dictation = norm_hotkey(updates.get(HOTKEY, cfg.get(HOTKEY)))
            if value and (value == dictation or value == smart_after
                          or hotkeys.is_bare_typing_key(value)):
                results[key] = False            # would fire with dictation / Smart Actions / while typing
                continue

        if key == SMART_HOTKEY:
            value = norm_hotkey(value)
            taken = (norm_hotkey(updates.get(HOTKEY, cfg.get(HOTKEY))),
                     norm_hotkey(updates.get(ASSIST_HOTKEY, cfg.get(ASSIST_HOTKEY))))
            if value and (value in taken or value.startswith("mouse:")
                          or hotkeys.is_bare_typing_key(value)):
                results[key] = False            # another shortcut's keys / not a key combo
                continue

        if key == SMART_SPEECH:
            value = str(value or "").strip()
            if not _smart_speech_ok(value, bool(cfg.get(PRIVACY))):
                results[key] = False            # unknown, or the cloud in Privacy mode
                continue

        if key == FILE_MODEL:
            value = str(value or "").strip()
            if not _file_model_ok(value, bool(cfg.get(PRIVACY))):
                results[key] = False            # unknown, or a cloud model in Privacy mode
                continue

        if key == HOTKEY:
            value = norm_hotkey(value)
            if not value or hotkeys.is_bare_typing_key(value):
                results[key] = False            # a lone typing key would fire while you type
                continue
            assist = norm_hotkey(updates.get(ASSIST_HOTKEY, cfg.get(ASSIST_HOTKEY)))
            if (assist and value == assist) or (smart_after and value == smart_after):
                # Taking the Live Assistant's (or Smart Actions') key would
                # silently unbind it (their setup refuses a clash): change
                # that one first.
                results[key] = False
                continue

        if cfg.get(key) == value and key in cfg:
            results[key] = True
            continue
        cfg[key] = value
        wrote = True
        results[key] = True
        changed.append(key)
        if key == HOTKEY:
            want["hotkey"] = True
        elif key == ASSIST_HOTKEY:
            want["assist"] = True
        elif key == SMART_HOTKEY:
            want["smart"] = True
        if key in PRELOAD_KEYS:
            want["preload"] = True
        if key in OVERLAY_KEYS:
            want["overlay"] = True
        if key == LIVE_ASSIST_PRIVATE:
            want["la_private"] = True

    # ── side effects, once each ──
    def register_assist():
        ok, reg = _try("live_assist_hotkey", app._setup_assist_hotkey, cfg.get(ASSIST_HOTKEY, ""))
        return bool(ok and (reg or not cfg.get(ASSIST_HOTKEY)))

    if want["hotkey"]:
        if old_assist and norm_hotkey(cfg[HOTKEY]) == norm_hotkey(old_assist):
            # A swap (dictation takes the Live Assistant's old key): free that
            # key first - two registrations of one combo can't be told apart.
            _try("live_assist_hotkey release", app._setup_assist_hotkey, "")
        ok, registered = _try("hotkey", app._setup_hotkey, cfg[HOTKEY])
        if not (ok and registered is not False):
            # Couldn't register: keep the shortcut that worked.
            cfg[HOTKEY] = old_hotkey
            _try("hotkey rollback", app._setup_hotkey, old_hotkey)
            results[HOTKEY] = False
            if HOTKEY in changed:
                changed.remove(HOTKEY)
    if want["hotkey"] or want["assist"]:
        # The Live Assistant hotkey depends on the dictation one (never both on one key).
        assist = norm_hotkey(cfg.get(ASSIST_HOTKEY))
        clash = bool(assist) and assist == norm_hotkey(cfg.get(HOTKEY))   # after a dictation rollback
        assist_ok = False if (want["assist"] and clash) else register_assist()
        if want["assist"]:
            results[ASSIST_HOTKEY] = assist_ok
            if not assist_ok:
                # Couldn't register, or clashes: keep the Live Assistant shortcut that worked.
                cfg[ASSIST_HOTKEY] = old_assist
                register_assist()
                if ASSIST_HOTKEY in changed:
                    changed.remove(ASSIST_HOTKEY)
        _refresh_tray(app)
    smart_fn = getattr(app, "_setup_smart_hotkey", None)
    if callable(smart_fn) and (want["hotkey"] or want["assist"] or want["smart"]):
        # The Smart Actions shortcut never shares keys with the other two:
        # registered again after any of them changed.
        ok, reg = _try("smart_actions_hotkey", smart_fn, cfg.get(SMART_HOTKEY, ""))
        smart_ok = bool(ok and (reg or not cfg.get(SMART_HOTKEY)))
        if want["smart"]:
            results[SMART_HOTKEY] = smart_ok
            if not smart_ok:
                # Couldn't register: keep the Smart Actions shortcut that worked.
                cfg[SMART_HOTKEY] = old_smart
                _try("smart_actions_hotkey rollback", smart_fn, old_smart)
                if SMART_HOTKEY in changed:
                    changed.remove(SMART_HOTKEY)
    if want["preload"]:
        _try("preload", app._preload_speech_model)
    if want["overlay"]:
        fn = getattr(getattr(app, "overlay", None), "apply_prefs", None)
        if callable(fn):
            _try("overlay", fn)
    if want["la_private"]:
        la = getattr(app, "live_assist", None)
        fn = getattr(la, "set_private", None)
        if callable(fn):
            _try("live_assist_private", fn, bool(cfg.get(LIVE_ASSIST_PRIVATE)))

    if wrote:
        _try("schedule save", app.schedule_config_save)
    for key in dict.fromkeys(changed):          # each key once, in order
        _try("announce %s" % key, app.sig_setting_changed.emit, key)
    return results


def _refresh_tray(app):
    fn = getattr(app, "_build_tray_menu", None)
    if callable(fn) and getattr(app, "tray_icon", None) is not None:
        _try("tray", fn)
