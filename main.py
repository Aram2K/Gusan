# Transcribe App - PySide6 Entry Point and Logic Controller

import os
# numpy's bundled OpenBLAS reserves ~32 MB per thread the moment numpy is
# imported (24 threads = ~750 MB of commit, almost all untouched); the app's
# heavy maths runs in CTranslate2 / llama.cpp, not numpy. Before any import
# of numpy, or it has no effect.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")
import gc
import sys
if __name__ == "__main__":
    # The app runs this file as __main__. Services do `import main` /
    # `from main import ...` (file jobs, model checks): they
    # must get THIS module - a second copy would load its own config.
    sys.modules.setdefault("main", sys.modules["__main__"])
import threading
import time
import json
import math
import wave
import io
import struct
import webbrowser
import socket
import queue
import logging
import hashlib
import datetime
import shutil
import base64
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pyperclip
import ctypes
import psutil
from PIL import Image, ImageDraw

from PySide6.QtCore import Qt, QTimer, QObject, Signal, QEvent
from PySide6.QtGui import QIcon, QPixmap, QImage, QAction
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QMessageBox

import storage
import history as hist
import actions
import action_api
import local_llm
import telemetry
import auth
import entitlements
import pill_copy
import text_cleanup
import vocabulary
import hotkeys
import speech_langs
import autostart
import gpu_accel

# ── Name and version ──────────────────────────────────────────────────────────
# Defined in app_info.py (no Qt) so pages and services can read them without
# importing this module. main.APP_VERSION / main.resource_path stay available
# (and patchable) for existing callers and tests.
from app_info import APP_NAME, APP_VERSION, resource_path
import settings_apply
import updater
# Gusan 2.0 services' config defaults (both modules are Qt-free at import).
from file_jobs import FILE_DEFAULTS
from calendar_service import DEFAULTS as CALENDAR_DEFAULTS

# ── Managed cloud transcription (Pro moat) ────────────────────────────────────
MANAGED_PROXY_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/transcribe-proxy"
FEEDBACK_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/submit-feedback"
DELETE_ACCOUNT_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/delete-account"
BILLING_PORTAL_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/billing-portal"
SUPPORT_EMAIL = "aibuben.xyz@gmail.com"

# ── Monetization links (Stripe) ───────────────────────────────────────────────
PRO_MONTHLY_URL = "https://buy.stripe.com/3cI5kC30N1oeari7rh0Ba00"
PRO_ANNUAL_URL  = "https://buy.stripe.com/fZuaEW0SF4Aq1UMh1R0Ba01"
# "Manage subscription" opens the Stripe Customer Portal already signed in via
# the billing-portal function. This is Stripe's no-code login link (Settings →
# Billing → Customer portal → Login link) - the fallback for an account the
# function can't match to a Stripe customer. Blank while that link is off.
STRIPE_PORTAL_URL = ""
# The Pro window's feature names (AppController._pro_upsell). paywall_viewed's
# "feature" keeps the 1.9 value, so dashboards stay continuous; the window
# shows today's name.
PRO_FEATURE_EVENT = {"Live Assistant": "Live Assistance"}
PRO_FEATURE_SHOWN = {"Live Assistance": "Live Assistant"}
PROJECT_GITHUB_URL = "https://github.com/Aram2K/transcribe-app"
RELEASES_URL = "https://github.com/Aram2K/transcribe-app/releases/latest"
RELEASES_API = "https://api.github.com/repos/Aram2K/transcribe-app/releases/latest"
RELEASES_MANIFEST_URL = "https://github.com/Aram2K/transcribe-app/releases/latest/download/update-manifest.json"
AIBUBEN_URL = "https://aibuben.xyz"

SINGLE_INSTANCE_PORT = 47823   # localhost-only IPC for "open on second launch"

# ── Config ────────────────────────────────────────────────────────────────────
LOG_PATH = str(storage.path_for("transcribe.log"))
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("transcribe")

CONFIG_PATH = str(storage.path_for("config.json"))
LEGACY_CONFIG_PATH = "config.json"
ANALYTICS_CONSENT_PATH = storage.path_for("analytics_consent.accepted")
ANALYTICS_DECLINED_PATH = storage.path_for("analytics_consent.declined")

# The Smart Actions shortcut a new (or schema-2) config gets (D2).
SMART_ACTIONS_HOTKEY_DEFAULT = "alt+s"

DEFAULT = {
    "hotkey":        "alt+r",
    "whisper_model": "base",
    # Free the speech / local AI model after this many idle minutes (0 = keep
    # it loaded). Whisper reloads in ~4-9 s on CPU, so it waits longer; the
    # reload starts when a recording does, overlapping the speech.
    "whisper_idle_unload_min": 10,
    "llm_idle_unload_min": 5,
    "language":      "auto",
    # With language "multi" (Mixed languages): the languages the user switches
    # between, even mid-sentence (speech_langs.py). Fewer than two ticked =
    # any language, as "multi" always worked.
    "mix_languages": [],
    "sample_rate":   16000,
    "chunk_size":    1024,
    "accent_color":  "#3b82f6",
    "backend":       "local",
    "google_api_key": "",
    "mistral_api_key": "",
    "initial_prompt": "",
    # 1.9.x's dictation mode switch. Kept (a 1.9.x copy may read this file)
    # but read by nothing in 2.0: Alt+R always pastes the exact words and a
    # Smart Action runs from its own shortcut (smart_actions_hotkey).
    "output_action": "transcribe_only",
    "action_model": "rule_based",
    "action_api_provider": "openai_compatible",
    "action_api_key": "",
    "action_api_base_url": "",
    "action_api_model": "",
    "translate_target": "en",
    "input_device_index": None,
    "meeting_audio_mode": "smart_meeting",
    "silence_trigger_sec": 0.8,
    "min_speech_sec": 1.5,
    "max_speech_sec": 25.0,
    "privacy_mode": False,
    "save_history": True,
    "analytics_enabled": True,
    "analytics_consent_applied": False,
    "analytics_endpoint": "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/transcribe-analytics",
    "onboarding_done": False,
    "account_gate_seen": False,
    "admin_tier_override": "auto",  # super-admin only: auto|guest|free|pro
    "dismissed_update_version": "",
    "pending_update_version": "",
    "pending_update_body": "",
    "previous_version": "",
    "tray_hint_shown": False,
    "meeting_consent_ack": False,  # one-time recording-consent notice
    "macos_perms_guide_shown": False,  # one-time macOS permission walkthrough
    # Output-quality pass (text_cleanup.py). Runs on every dictation - the only
    # safety net between a Whisper hallucination and the user's cursor.
    "cleanup_enabled": True,
    "cleanup_strip_hallucinations": True,
    "cleanup_strip_artifacts": True,
    "cleanup_remove_fillers": True,    # ON: um / uh / you know are dropped (a saved False stays False)
    "cleanup_replacements": [],        # [{"from": "pyside 6", "to": "PySide6"}]
    "cleanup_custom_hallucinations": [],
    # Custom vocabulary (vocabulary.py). "initial_prompt" above is kept as a
    # derived mirror of this list so the local decode path needs no changes.
    "vocabulary": [],
    "vocabulary_share_with_cloud": True,
    # Put the user's clipboard back after we paste.
    "restore_clipboard": True,
    "clipboard_restore_delay_ms": 400,
    # Notion export (meeting notes -> Notion). notion_api_key is the access token
    # from "Connect Notion" or a pasted own-integration secret; with
    # notion_refresh_token (OAuth only) it lives in the OS keyring
    # (_NOTION_SECRETS: storage.NOTION_TOKEN_SECRET / NOTION_REFRESH_SECRET),
    # like the API keys; config.json keeps "" once the keyring has them.
    # notion_auth_mode: "" / "mcp" / "oauth" / "own" ("mcp" = the Connect button,
    # Notion's hosted server; a token with no mode is a setup from before OAuth,
    # read as "own"). notion_parent_page is the page the notes land
    # under (a pasted link still works); notion_hub_page is the "Gusan meetings"
    # page Gusan made, and the destination is "the hub" while the two match.
    # notion_reconnect: Notion refused the token for good, so Settings asks to
    # connect again. The workspace and page names are for display only.
    # notion_mcp_client_id: the id Gusan registered with Notion's hosted server,
    # once per install (public, kept across disconnects); notion_mcp_redirects:
    # which set of return addresses that client was registered with ("" = the
    # loopback one only, "2" = our site's page and the loopback one: a client
    # that knows only the loopback address is registered again at the next
    # Connect that wants to return through the site); notion_token_expires:
    # when the "mcp" access token ends (epoch seconds, 0 = unknown).
    "notion_api_key": "",
    "notion_refresh_token": "",
    "notion_auth_mode": "",
    "notion_workspace_name": "",
    "notion_parent_page": "",
    "notion_parent_title": "",
    "notion_hub_page": "",
    "notion_mcp_client_id": "",
    "notion_mcp_redirects": "",
    "notion_token_expires": 0,
    "notion_reconnect": False,
    # "Send every meeting's notes to Notion automatically": off until the user
    # turns it on (Meeting settings, or the connected Notion sheet).
    "notion_auto_send": False,
    # The Gusan account (auth.user_id) a Notion connection was made under; ""
    # = made as a guest, adopted by the first account that signs in. Another
    # account signing in forgets the connection (_reconcile_notion_owner).
    "notion_owner": "",
    # Live Assistance overlay (ui/live_assist.py): private copilot during calls.
    "live_assist_hotkey": "ctrl+alt+a",
    "live_assist_private": True,     # excluded from screen capture
    "live_assist_opacity": 0.96,
    "live_assist_theme": "light",
    # Answer automatically when someone asks a question. A new key on purpose:
    # 1.9.0 stored "live_assist_auto": false (its periodic refresh, off by
    # default) in every config.json, which would keep this off after updating.
    "live_assist_auto_answer": True,
    "live_assist_pos": None,
    # Transcription language of a Live Assistant session ("" = the dictation
    # language) and of its answers ("" = the call's language). 1.9.x saved
    # "en" in every config, and those keep English.
    "live_assist_language": "",
    "live_assist_output_language": "",
    # Who writes the answers: "" automatic, "cloud" (Gusan Cloud), "local".
    "live_assist_writer": "",
    "live_assist_screen_auto": True,      # attach a screenshot when the talk refers to the screen
    # What the user wrote about the call (the overlay's Context line): every
    # answer is shaped by it until they change or clear it.
    "live_assist_context": "",
    # Dictation HUD glass (ui/overlay.py): "auto" adapts to what is behind it
    # (light over bright content, dark over dark), or pin "dark" / "light".
    "overlay_theme": "auto",
    # Keep the HUD out of screen shares and recordings. Its live glass needs
    # this (it samples what is behind it); off, it paints a smoked plate.
    "overlay_private": True,
    "overlay_pos": None,                  # where the user dragged the HUD
    # ── Gusan 2.0 ──
    # Audio to text rail (file_jobs.FILE_DEFAULTS): file_model, file_language,
    # file_speakers, file_timestamps, file_save_word, notify_when_ready.
    **FILE_DEFAULTS,
    # Google Calendar reminders (calendar_service): calendar_ask_to_record,
    # calendar_only_with_link.
    **CALENDAR_DEFAULTS,
    "meeting_language": "",               # "" = the dictation language
    "meeting_notes_model": "",            # "" = action_model (meeting_session picks a real engine)
    "meeting_consent_reminder": True,     # the consent line on the Meetings start card
    # The Smart Actions shortcut: one take runs as a Smart Action (Option+S
    # on a Mac). "" = not set (Alt+S was another shortcut's at migration).
    "smart_actions_hotkey": SMART_ACTIONS_HOTKEY_DEFAULT,
    # What transcribes a Smart Action: "" = same as Dictation | "cloud" =
    # Gusan Cloud (Pro, never in Privacy mode) | a Whisper id (hardware.MODEL_INFO).
    "smart_actions_speech_model": "",
    "app_theme": "system",               # Settings > General > App look (row hidden in 2.0: light only)
    "install_updates_on_quit": True,      # download a found update, run it at Quit
    # One quiet line on Home for 1.x upgraders ("Transcribe is now Gusan…"):
    # set by _migrate_config, cleared once Home has shown it (or it was closed).
    "rename_note_pending": False,
    # Bumped when the config shape changes in a way that needs migration
    # (_migrate_config). 2 = Gusan 2.0; 3 = the Smart Actions shortcut (Alt+S).
    "config_schema_version": 3,
}

CONFIG_SCHEMA_VERSION = DEFAULT["config_schema_version"]


def _migrate_config(loaded, on_disk):
    """Bring an older config up to CONFIG_SCHEMA_VERSION, in place. Only adds
    or fills keys - never drops one (a 1.9.x copy may read this file again).
    ``on_disk`` is what the file held (the DEFAULT itself when there is none)."""
    try:
        version = int(on_disk.get("config_schema_version", 1) or 1)
    except (TypeError, ValueError):
        version = 1
    if version >= CONFIG_SCHEMA_VERSION:
        return
    if version < 2 and on_disk:
        # A 1.x install's settings (a real file - a fresh install reads the
        # DEFAULT, already schema 2): Home says once that Transcribe is now
        # Gusan and everything was kept. Never a modal.
        loaded["rename_note_pending"] = True
    if version < 2:
        # 1.9 file jobs used the dictation language: keep it for Audio to text
        # (a user who dictates in Russian keeps getting Russian files).
        if "file_language" not in on_disk:
            lang = on_disk.get("language") or loaded.get("language") or "auto"
            loaded["file_language"] = lang if isinstance(lang, str) and lang else "auto"
    if version < 3:
        # Smart Actions run from their own shortcut now (Alt+R always pastes
        # the exact words). A 1.9.x file has no key and an early 2.0 one saved
        # "" (= none): both get Alt+S - unless Alt+S is already the dictation
        # or Live Assistant key, then it stays "" (the page shows "Set
        # shortcut"). A shortcut the user picked is kept.
        if not settings_apply.norm_hotkey(on_disk.get("smart_actions_hotkey", "")):
            taken = {settings_apply.norm_hotkey(loaded.get("hotkey")),
                     settings_apply.norm_hotkey(loaded.get("live_assist_hotkey"))}
            loaded["smart_actions_hotkey"] = ("" if SMART_ACTIONS_HOTKEY_DEFAULT in taken
                                              else SMART_ACTIONS_HOTKEY_DEFAULT)
    loaded["config_schema_version"] = CONFIG_SCHEMA_VERSION

storage.migrate_legacy_file(LEGACY_CONFIG_PATH, CONFIG_PATH)

# Mistral's /audio/transcriptions endpoint only accepts the Voxtral Mini
# Transcribe model. "voxtral-small/large-latest" are NOT valid there (Small is a
# chat model; there is no Large), which is why selecting them returned
# "Invalid model". We normalize any stale/invalid choice to the working alias.
# Defined above load_config() because load_config() calls it at import time.
MISTRAL_STT_DEFAULT = "voxtral-mini-latest"
MISTRAL_STT_MODELS = {"voxtral-mini-latest", "voxtral-mini-2602", "voxtral-mini-2507"}


def normalize_mistral_model(model):
    return model if model in MISTRAL_STT_MODELS else MISTRAL_STT_DEFAULT


# BYO key fields that must never sit in plaintext config.json (they live in the
# OS keyring; config keeps "" once migrated).
_SECRET_CFG_KEYS = ("google_api_key", "action_api_key", "mistral_api_key")

# The Notion tokens (the access token or a pasted secret, and the OAuth refresh
# token) live in the keyring too. A table of their own, NOT part of
# _SECRET_CFG_KEYS: that tuple also drives the per-account stash, and a Notion
# connection belongs to the computer (see _reconcile_notion_owner).
_NOTION_SECRETS = {"notion_api_key": storage.NOTION_TOKEN_SECRET,
                   "notion_refresh_token": storage.NOTION_REFRESH_SECRET}

# What the OS keyring is known to hold ({secret name: value}), from this run's
# reads and successful writes. save_config runs after every instant-apply burst
# (and on macOS each keyring write is a `security` subprocess), so it writes a
# secret only when its value changed. Tied to the writer it was learned with:
# another writer (a test's stand-in, a keyring swap) starts from nothing.
_KEYRING_KNOWN = {}
_KEYRING_KNOWN_FOR = [None]


def _keyring_known():
    if _KEYRING_KNOWN_FOR[0] is not storage.write_secret:
        _KEYRING_KNOWN.clear()
        _KEYRING_KNOWN_FOR[0] = storage.write_secret
    return _KEYRING_KNOWN


def _note_keyring_value(name, value):
    """A secret read from (or written to) the keyring this run. Only a value
    actually found counts: "" can also mean the keyring couldn't be read."""
    if value:
        _keyring_known()[name] = value


def _store_secret(name, value):
    """Put one secret in the OS keyring unless it already holds that value.
    True = the keyring holds ``value`` (config.json may then keep "")."""
    known = _keyring_known()
    if name in known and known[name] == value:
        return True
    ok = storage.write_secret(name, value)
    if ok:
        known[name] = value
    else:
        known.pop(name, None)
    return ok


def _strip_user_secret_keys(store):
    """Split cfg['user_secrets'] into (sanitized_store_for_disk, keys_blob).
    The blob ({owner: {key_field: value}}) goes to the OS keyring; the on-disk
    store keeps everything else (engine choices) with the key fields blanked."""
    if not isinstance(store, dict):
        return store, {}
    sanitized, blob = {}, {}
    for owner, snap in store.items():
        if not isinstance(snap, dict):
            sanitized[owner] = snap
            continue
        clean = dict(snap)
        keys = {}
        for k in _SECRET_CFG_KEYS:
            v = clean.get(k)
            if isinstance(v, str) and v.strip():
                keys[k] = v
            clean[k] = ""
        sanitized[owner] = clean
        if keys:
            blob[owner] = keys
    return sanitized, blob


def _load_notion_secrets(loaded):
    """Fill the Notion tokens of ``loaded`` from the OS keyring; a plaintext one
    in config.json (a 1.9 setup) moves into it. Returns ({cfg key: ""} for each
    token the keyring now holds, moved): the blanks every config.json write of
    this load must carry, and whether a plaintext token was moved. A token the
    keyring would not take stays as it is (the API keys' fallback)."""
    held, moved = {}, False
    for key, name in _NOTION_SECRETS.items():
        value = (loaded.get(key) or "").strip()
        if value:
            if _store_secret(name, value):
                loaded[key] = value
                held[key] = ""
                moved = True
        else:
            loaded[key] = storage.read_secret(name)
            _note_keyring_value(name, loaded[key])
            if loaded[key]:
                held[key] = ""
    return held, moved


def load_config():
    data = storage.read_json(CONFIG_PATH, DEFAULT)
    if not isinstance(data, dict):
        data = {}
    loaded = {**DEFAULT, **data}
    _migrate_config(loaded, data)
    # Keep initial_prompt in step with the structured vocabulary list, so a
    # hand-edited or older config self-heals on load.
    vocabulary.sync_config(loaded)

    # First: every config.json write below carries the Notion blanks, or the
    # migration of another key would put a plaintext token back on disk.
    notion_blank, notion_moved = _load_notion_secrets(loaded)
    if notion_moved:
        try:
            # Twice: the second write also replaces config.json.bak, a copy of
            # the file as it was, which would still hold the plaintext token.
            for _ in range(2):
                storage.atomic_write_json(CONFIG_PATH, {**loaded, **notion_blank})
        except OSError as e:
            logger.warning("Could not sanitize the Notion token in config: %s", e)

    key = (loaded.get("google_api_key") or "").strip()
    if key:
        if _store_secret(storage.GOOGLE_API_KEY_SECRET, key):
            loaded["google_api_key"] = key
            disk = {**loaded, "google_api_key": "", **notion_blank}
            try:
                storage.atomic_write_json(CONFIG_PATH, disk)
            except OSError as e:
                logger.warning("Could not sanitize API key in config: %s", e)
    else:
        loaded["google_api_key"] = storage.read_secret(storage.GOOGLE_API_KEY_SECRET)
        _note_keyring_value(storage.GOOGLE_API_KEY_SECRET, loaded["google_api_key"])

    if loaded.get("privacy_mode"):
        loaded["backend"] = "local"
        # Privacy Mode only blocks the cloud; local history stays on the device
        # under the user's own "Save local transcription history" setting.
        if actions.ACTION_MODELS.get(actions.normalize_action_model(loaded.get("action_model")), {}).get("kind") == "cloud":
            loaded["action_model"] = actions.RULE_BASED_ID
    loaded["action_model"] = actions.normalize_action_model(loaded.get("action_model"))
    # Heal any stale/invalid Mistral STT choice (e.g. the removed voxtral-small).
    if loaded.get("mistral_stt_model"):
        loaded["mistral_stt_model"] = normalize_mistral_model(loaded["mistral_stt_model"])

    # output_action is 1.9.x's (kept as it is): dictation never reads it.
    # A Smart Action runs only from its own shortcut (smart_actions_hotkey).

    action_key = (loaded.get("action_api_key") or "").strip()
    if action_key:
        if _store_secret(storage.ACTION_API_KEY_SECRET, action_key):
            loaded["action_api_key"] = action_key
            disk = {**loaded, "google_api_key": "", "action_api_key": "", **notion_blank}
            try:
                storage.atomic_write_json(CONFIG_PATH, disk)
            except OSError as e:
                logger.warning("Could not sanitize action API key in config: %s", e)
    else:
        loaded["action_api_key"] = storage.read_secret(storage.ACTION_API_KEY_SECRET)
        _note_keyring_value(storage.ACTION_API_KEY_SECRET, loaded["action_api_key"])

    mistral_key = (loaded.get("mistral_api_key") or "").strip()
    if mistral_key:
        # Migrate a plaintext Mistral key from config.json into the OS keyring.
        if _store_secret(storage.MISTRAL_API_KEY_SECRET, mistral_key):
            loaded["mistral_api_key"] = mistral_key
            disk = {**loaded, "google_api_key": "", "action_api_key": "", "mistral_api_key": "", **notion_blank}
            try:
                storage.atomic_write_json(CONFIG_PATH, disk)
            except OSError as e:
                logger.warning("Could not sanitize Mistral API key in config: %s", e)
    else:
        loaded["mistral_api_key"] = storage.read_secret(storage.MISTRAL_API_KEY_SECRET)
        _note_keyring_value(storage.MISTRAL_API_KEY_SECRET, loaded["mistral_api_key"])

    # Per-user stashed keys (account switching) live in the keyring as one JSON
    # blob; merge them back over the sanitized on-disk snapshots.
    try:
        blob_raw = storage.read_secret(storage.USER_SECRETS_SECRET)
        _note_keyring_value(storage.USER_SECRETS_SECRET, blob_raw)
        if blob_raw:
            blob = json.loads(blob_raw)
            store = loaded.get("user_secrets")
            if isinstance(blob, dict) and isinstance(store, dict):
                for owner, keys in blob.items():
                    snap = store.get(owner)
                    if isinstance(snap, dict) and isinstance(keys, dict):
                        for k, v in keys.items():
                            if k in _SECRET_CFG_KEYS and v and not snap.get(k):
                                snap[k] = v
    except Exception:
        logger.debug("Could not merge user secrets from keyring", exc_info=True)

    if ANALYTICS_CONSENT_PATH.exists() and not loaded.get("analytics_consent_applied"):
        if ANALYTICS_DECLINED_PATH.exists():
            loaded["analytics_enabled"] = False
        loaded["analytics_consent_applied"] = True
        disk = {**loaded, "google_api_key": "", "action_api_key": "", **notion_blank}
        try:
            storage.atomic_write_json(CONFIG_PATH, disk)
        except OSError as e:
            logger.warning("Could not record installer analytics consent: %s", e)

    return loaded

def save_config(c):
    disk = {**c}
    if disk.get("privacy_mode"):
        disk["backend"] = "local"
        # Local history is independent of Privacy Mode (it never leaves the device).
        if actions.ACTION_MODELS.get(actions.normalize_action_model(disk.get("action_model")), {}).get("kind") == "cloud":
            disk["action_model"] = actions.RULE_BASED_ID
    # Each secret is written only when it changed (_store_secret).
    key = (disk.get("google_api_key") or "").strip()
    if _store_secret(storage.GOOGLE_API_KEY_SECRET, key):
        disk["google_api_key"] = ""
    action_key = (disk.get("action_api_key") or "").strip()
    if _store_secret(storage.ACTION_API_KEY_SECRET, action_key):
        disk["action_api_key"] = ""
    mistral_key = (disk.get("mistral_api_key") or "").strip()
    if _store_secret(storage.MISTRAL_API_KEY_SECRET, mistral_key):
        disk["mistral_api_key"] = ""
    for k, name in _NOTION_SECRETS.items():
        if _store_secret(name, (disk.get(k) or "").strip()):
            disk[k] = ""
    # Per-user stashed keys (account switching) go to the keyring too - the
    # on-disk store keeps only the non-secret engine config per user.
    sanitized_store, keys_blob = _strip_user_secret_keys(disk.get("user_secrets"))
    try:
        if _store_secret(storage.USER_SECRETS_SECRET, json.dumps(keys_blob, sort_keys=True)):
            disk["user_secrets"] = sanitized_store
    except Exception:
        logger.debug("Could not stash user secrets in keyring", exc_info=True)
    storage.atomic_write_json(CONFIG_PATH, disk)

cfg = load_config()

# resource_path comes from app_info (imported at the top).

# ── Single-instance IPC ──────────────────────────────────────────────────────
def acquire_single_instance_lock():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(5)
        return sock
    except OSError:
        sock.close()
        return None

IPC_ACTIONS = ("show_settings", "show_onboarding", "show_meeting", "live_prompter")


def launch_action(argv):
    """What a second launch asks the running instance to do: the action named
    on the command line, nothing for the quiet Windows-startup launch (the app
    is already running), otherwise "show_settings" - which opens the app (the
    name is kept for 1.9.x compatibility)."""
    for arg in argv[1:]:
        if arg in IPC_ACTIONS:
            return arg
    if "--background" in argv:
        return None
    return "show_settings"


def signal_running_instance(action="show_settings"):
    try:
        with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=1) as s:
            s.settimeout(1)
            s.sendall(action.encode("utf-8"))
            return s.recv(32) == b"transcribe-ok"
    except OSError:
        return False

def dispatch_ipc_action(controller, action):
    """What the running app does with a second launch's action (GUI thread):
    "show_settings" (the default, and the installer's --show-settings) opens
    the app, "show_onboarding" the welcome wizard (first run only - else the
    app), "show_meeting" the Meetings page, "live_prompter" toggles the Live
    Assistant card. Anything else
    opens the app - or, while the first-run wizard is on screen, brings the
    wizard forward (never the app behind it). The names never change: a 1.9.x
    copy may be the one asking."""
    if action == "live_prompter":
        controller.toggle_live_assist()
    elif action == "show_meeting":
        controller.show_meeting()
    elif action == "show_onboarding":
        controller.show_onboarding()
    else:
        front = getattr(controller, "front_first_run_wizard", None)
        if callable(front) and front() is True:
            return
        controller.show_main()


def start_ipc_server(server_sock, on_action):
    def _accept():
        while True:
            try:
                conn, _ = server_sock.accept()
                data = conn.recv(64).decode("utf-8", errors="ignore").strip()
                if data in IPC_ACTIONS:
                    conn.sendall(b"transcribe-ok")
                conn.close()
                if data:
                    on_action(data)
            except Exception:
                break
    threading.Thread(target=_accept, daemon=True).start()

# ── System Info ───────────────────────────────────────────────────────────────
RAM_GB = psutil.virtual_memory().total / (1024 ** 3)

# speed_rank is a relative compute cost (1 = fastest). The UI turns it into a
# plain-language estimate adjusted for the user's hardware (GPU vs CPU), instead
# of a fixed "~Ns" that is meaningless across machines. min_ram is a realistic,
# monotonic minimum-recommended system RAM for the int8 runtime.
MODELS = {
    "tiny":           {"min_ram": 2,  "speed_rank": 1, "quality": "Good",        "size": "75 MB"},
    "base":           {"min_ram": 2,  "speed_rank": 2, "quality": "Better",      "size": "140 MB"},
    "small":          {"min_ram": 4,  "speed_rank": 3, "quality": "Great",       "size": "460 MB"},
    "medium":         {"min_ram": 6,  "speed_rank": 5, "quality": "Excellent",   "size": "1.4 GB"},
    "large-v3-turbo": {"min_ram": 6,  "speed_rank": 4, "quality": "Best (fast)", "size": "1.6 GB"},
    "large-v3":       {"min_ram": 8,  "speed_rank": 6, "quality": "Best",        "size": "3 GB"},
}

LANG_NAMES = {
    "auto":  "Auto-detect",
    "multi": "Mixed languages",
    "en":    "English",
    "ar":    "Arabic",
    "hy":    "Armenian",
    "fr":    "French",
    "de":    "German",
    "ru":    "Russian",
    "es":    "Spanish",
}

# Languages Mistral Voxtral transcription accepts (ISO-639-1). Anything else -
# including our "auto"/"multi" pseudo-codes - is omitted so we never send an
# unsupported code, which Voxtral rejects with a 400 (this was the cause of cloud
# transcription failing even when the API key tested fine).
MISTRAL_LANGS = {"en", "es", "fr", "de", "it", "nl", "pt", "hi", "ar", "ru",
                 "uk", "pl", "tr", "ja", "ko", "zh", "ro", "cs", "el"}


def cloud_error_message(provider, status, body_text):
    """Turn a cloud provider's HTTP error into one clear, user-facing sentence."""
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


def billing_portal_session(token, post=None):
    """(url, problem) for the signed-in user's Stripe Customer Portal, from the
    billing-portal function. ``problem`` is "" on success, else "signed_out",
    "no_customer" (no Stripe customer for this account) or "unavailable"."""
    if not token:
        return None, "signed_out"
    if post is None:
        import requests
        post = requests.post
    try:
        resp = post(BILLING_PORTAL_URL, json={},
                    headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except Exception as e:
        logger.warning("Billing portal unreachable: %s", e)
        return None, "unavailable"
    if resp.status_code == 200:
        try:
            url = (resp.json() or {}).get("url") or ""
        except ValueError:
            url = ""
        # Only ever open Stripe's own portal from this response.
        if url.startswith("https://billing.stripe.com/"):
            return url, ""
        return None, "unavailable"
    if resp.status_code == 401:
        return None, "signed_out"
    if resp.status_code == 404:
        return None, "no_customer"
    logger.warning("Billing portal HTTP %s: %s", resp.status_code, (resp.text or "")[:200])
    return None, "unavailable"


def model_ok(name):
    return RAM_GB >= MODELS[name]["min_ram"]

def _looks_like_whisper_cache_error(err):
    low = str(err).lower()
    return (
        "model.bin" in low
        or ("unable to open file" in low and "model" in low)
        or ("no such file" in low and "model" in low)
    )

def _friendly_transcription_error(err):
    if _looks_like_whisper_cache_error(err):
        return (
            "Local Whisper model files are incomplete. Open Settings › Speech & AI "
            "and redownload the selected Whisper model."
        )
    return str(err)

def _materialize_hf_snapshot_links(model_dir):
    """Replace HF snapshot symlinks with real files when possible.

    Some Windows installs leave a snapshot that resolves through symlinks which
    CTranslate2 then fails to open as model.bin. Copying the blob into the
    snapshot makes the local model usable without deleting the whole cache.
    """
    try:
        model_dir = Path(model_dir)
        if not model_dir.exists():
            return False
        changed = False
        for item in model_dir.iterdir():
            if not item.is_symlink():
                continue
            try:
                raw_target = os.readlink(item)
                target = Path(raw_target)
                if not target.is_absolute():
                    target = (item.parent / target).resolve()
                if not target.exists() or not target.is_file():
                    continue
                tmp = item.with_name(f".{item.name}.materializing")
                shutil.copy2(target, tmp)
                item.unlink()
                os.replace(tmp, item)
                changed = True
            except Exception as e:
                logger.debug("Could not materialize HF cache link %s: %s", item, e)
        return changed
    except Exception as e:
        logger.debug("HF cache materialization failed for %s: %s", model_dir, e)
        return False

def _repair_whisper_model_cache(name):
    try:
        from faster_whisper.utils import download_model
        path = Path(download_model(name, local_files_only=False))
        _materialize_hf_snapshot_links(path)
        model_bin = path / "model.bin"
        return model_bin.exists() and model_bin.stat().st_size > 1024
    except Exception as e:
        logger.warning("Could not repair Whisper model cache for %s: %s", name, e)
        return False

def _missing_from_cache(e):
    """The offline load failed only because the model isn't downloaded yet
    (huggingface_hub's LocalEntryNotFoundError) - not a GPU problem."""
    return (type(e).__name__ == "LocalEntryNotFoundError"
            or "local_files_only" in str(e))


# The Hugging Face repos faster-whisper downloads the catalog models from
# (faster_whisper.utils._MODELS). Kept here so checking what's on disk never
# imports faster-whisper and Hugging Face's downloader: the Settings scan does
# it for every model while the app starts, and that cost ~2.5 s.
_WHISPER_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3": "Systran/faster-whisper-large-v3",
}


def _cached_model_bin(repo_id):
    """Whether the Hugging Face cache holds a complete model.bin for repo_id -
    a plain disk check. None when the cache location can't be read."""
    try:
        from huggingface_hub import constants
        root = constants.HF_HUB_CACHE
    except Exception:
        return None
    snaps = os.path.join(root, "models--" + repo_id.replace("/", "--"), "snapshots")
    try:
        names = os.listdir(snaps)
    except OSError:
        return False
    for snap in names:
        try:
            if os.path.getsize(os.path.join(snaps, snap, "model.bin")) > 1024:
                return True
        except OSError:
            continue
    return False


def model_downloaded(name):
    repo = _WHISPER_REPOS.get(name)
    if repo:
        on_disk = _cached_model_bin(repo)
        if on_disk is not None:
            return on_disk
    try:
        from faster_whisper.utils import download_model
        path = Path(download_model(name, local_files_only=True))
        model_bin = path / "model.bin"
        return model_bin.exists() and model_bin.stat().st_size > 1024
    except Exception:
        return False

def download_whisper_model(name, on_progress=None):
    if on_progress is None:
        from faster_whisper.utils import download_model
        return download_model(name)

    import huggingface_hub
    from tqdm.auto import tqdm
    import faster_whisper.utils as fw_utils

    model_map = getattr(fw_utils, "_MODELS", {})
    repo_id = model_map.get(name, name)
    if "/" not in repo_id and name not in model_map:
        from faster_whisper.utils import download_model
        return download_model(name)

    allow_patterns = [
        "config.json",
        "preprocessor_config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.*",
    ]

    progress_lock = threading.Lock()
    bars = {}
    totals = {"done": 0, "expected": 0, "last_emit": 0.0}

    def _emit(force=False):
        now = time.time()
        if not force and now - totals["last_emit"] < 0.2:
            return
        totals["last_emit"] = now
        with progress_lock:
            active_done = sum(max(0, v.get("n") or 0) for v in bars.values())
            active_expected = sum(max(0, v.get("total") or 0) for v in bars.values())
            done = totals["done"] + active_done
            expected = totals["expected"] + active_expected
        percent = None
        if expected > 0:
            percent = max(0, min(99, int(done * 100 / expected)))
        try:
            on_progress(percent, done, expected)
        except Exception:
            pass

    class ProgressTqdm(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs["file"] = io.StringIO()
            kwargs["disable"] = False
            super().__init__(*args, **kwargs)
            with progress_lock:
                bars[id(self)] = {"n": self.n or 0, "total": self.total or 0}
            _emit(force=True)

        def update(self, n=1):
            result = super().update(n)
            with progress_lock:
                bars[id(self)] = {"n": self.n or 0, "total": self.total or 0}
            _emit()
            return result

        def close(self):
            with progress_lock:
                item = bars.pop(id(self), None)
                if item:
                    done = max(item.get("n") or 0, item.get("total") or 0)
                    totals["done"] += done
                    totals["expected"] += max(item.get("total") or done, done)
            try:
                super().close()
            finally:
                _emit(force=True)

    on_progress(0, 0, 0)
    kwargs = {
        "local_files_only": False,
        "allow_patterns": allow_patterns,
        "tqdm_class": ProgressTqdm,
        "max_workers": 8,
    }
    try:
        path = huggingface_hub.snapshot_download(repo_id, **kwargs)
    except TypeError as e:
        if "max_workers" not in str(e):
            raise
        kwargs.pop("max_workers", None)
        path = huggingface_hub.snapshot_download(repo_id, **kwargs)
    on_progress(100, totals["expected"], totals["expected"])
    return path

def whisper_model_cache_dir(name):
    try:
        from pathlib import Path
        from faster_whisper.utils import download_model
        path = Path(download_model(name, local_files_only=True)).resolve()
        for candidate in (path, *path.parents):
            if candidate.name.startswith("models--"):
                return candidate
        return path
    except Exception:
        return None

def remove_whisper_model(name):
    import os, stat, shutil
    cache_dir = whisper_model_cache_dir(name)
    if not cache_dir or not cache_dir.exists():
        return False

    def _onerror(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            raise

    shutil.rmtree(cache_dir, onerror=_onerror)
    return True

# ── Audio Recorder ────────────────────────────────────────────────────────────
SILENCE_TRIGGER_SEC = 0.8
MIN_SPEECH_SEC      = 1.5
MAX_SPEECH_SEC      = 25

# Dynamically import PyAudio or PyAudioWPatch
try:
    import pyaudiowpatch as pyaudio
    HAS_LOOPBACK = True
except ImportError:
    import pyaudio
    HAS_LOOPBACK = False

class _GpuDecodeError(Exception):
    """A decode failed in the GPU stack (transcribe_segments retries on CPU)."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _model_device(model):
    """"cuda" / "cpu" for a faster-whisper model (its CTranslate2 model holds
    the device), or None when unknown."""
    for obj in (model, getattr(model, "model", None)):
        dev = getattr(obj, "device", None)
        if isinstance(dev, str):
            return dev
    return None


def cfg_float(name, default, minimum=None, maximum=None):
    try:
        value = float(cfg.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value

class AudioRecorder:
    def __init__(self):
        self.recording        = False
        self.stop_requested   = False
        self._record_thread   = None
        self.audio            = pyaudio.PyAudio()
        self._model           = None
        self._model_name      = None
        self._model_lock      = threading.Lock()
        # Transcriptions holding the model right now (use_model) and when it
        # was last used: the idle sweep (maybe_unload_idle) frees it only at 0
        # users after whisper_idle_unload_min.
        self._model_users     = 0
        self._last_model_use  = 0.0
        # Guards only the two above - never held during a load, so releasing a
        # lease never waits behind another model's 4-9 s build.
        self._users_lock      = threading.Lock()
        # Serializes actual transcribe() inference. A single WhisperModel is NOT
        # safe for concurrent calls; the chunked pipeline runs several chunk
        # threads at once, which hangs/crashes on CUDA (CPU only tolerated it by
        # luck). One global inference lock fixes the GPU hang.
        self._infer_lock      = threading.Lock()
        
        self.on_levels        = lambda lvls: None
        self.on_finalising    = lambda done, total: None
        self.on_chunk_complete = lambda idx, text, lang: None
        self.on_lang_detected = lambda code, name: None
        self.on_partial       = lambda text: None
        # Returns the signed-in user's Supabase access token (set by AppController);
        # used by the managed cloud backend to authenticate to the proxy.
        self.get_auth_token   = None
        
        self._chunk_frames    = []
        self._chunk_results   = {}
        self.partial_text     = ""   # what transcribed when a pass failed
        self._chunk_idx       = 0
        self._chunk_lock      = threading.Lock()
        self._samples_in_chunk= 0
        self._session_lang    = None
        self._chunk_threads   = []
        self._chunk_errors    = []
        self._chunk_silence_before = {}
        self._record_error    = ""
        self._abort           = False   # cooperative cancel: transcribe() bails fast when set (Esc)
        self._cloud_capped    = False   # set when managed cloud hit its cap and we fell back to local
        # Rolling peak amplitude for adaptive bar normalization. Decays slowly
        # so bars stay calibrated to recent mic activity (handles mics with
        # very different gain levels - laptop mic vs. headset vs. far-field).
        self._level_peak      = 0.05
        # Per-session language override (the meeting window's language
        # selector). None = use the global cfg["language"] default.
        self.language_override = None
        # Per-take speech engine (a Smart Action's own pick, start_recording
        # speech=): backend "local"/"managed" and a Whisper id. None = the
        # global cfg["backend"] / cfg["whisper_model"]. Every start resets them.
        self.backend_override = None
        self.model_override = None
        self._capture_mode = None     # smart_meeting/system_only/default_mic; None = dictation
        self.fast_live_chunks = False  # Live Assistance session: short pieces (see _record_loop)
        self._loopback_peak = 0.0     # loudest loopback sample this session (0 = no system audio)
        # Meeting Pause (set_paused): the record thread keeps reading - and so
        # draining - the streams, but keeps nothing while this is set.
        self.paused           = False
        # Meetings only: loudness of the mic and of the call (system sound)
        # BEFORE they are mixed, as on_source_levels(mic_rms, call_rms) from
        # the record thread. None = nobody listens (dictation).
        self.on_source_levels = None
        # Mic + system sound meetings: (mic energy, call energy) of each chunk,
        # by chunk index - a hint at who spoke (chunk_source_energy).
        self._chunk_sources   = {}
        # model_fetcher(name): called on a worker before a Whisper model is
        # built, so a missing model comes through the app's one download
        # manager (progress, no duplicate download) - AppController sets it.
        # None: the build downloads it itself (local_files_only=False).
        self.model_fetcher    = None
        self._fetching_model  = False
        # The language the last transcribe_segments() decode detected (file
        # jobs show it for Detect automatically / Mixed languages).
        self._last_segments_language = None

    def set_paused(self, paused):
        """Pause or resume a meeting recording. Only a flag: the record thread
        keeps reading the streams (they are never stopped or closed from
        another thread) and keeps nothing while paused - no speech detection,
        no chunk, no full-audio frame. The speech pending at Pause is cut as one
        chunk right away. One recording stays ONE segment, so the notes, the
        speaker pass and the 2-hour cap work as without a pause."""
        self.paused = bool(paused)

    def chunk_source_energy(self, idx):
        """(mic energy, call energy) of chunk ``idx`` in a mic + system sound
        meeting, or None (other modes, or not cut yet)."""
        with self._chunk_lock:
            return self._chunk_sources.get(idx)

    @staticmethod
    def _samples_rms(samples):
        if samples is None or len(samples) == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(samples))))

    @staticmethod
    def _whisper_device():
        """Prefer a CUDA GPU when one is usable (much faster); else CPU int8.
        int8_float16 keeps GPU VRAM low so it fits on small cards too."""
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                # The packaged Windows app has no cuBLAS until the optional
                # GPU download (gpu_accel): without it a CUDA model builds, then
                # fails at its first computation - seconds and video memory
                # wasted on every start. Same once the GPU was proven unusable.
                if sys.platform == "win32" and not gpu_accel.libs_dir():
                    return ("cpu", "int8")
                if cfg.get(gpu_accel.CFG_FAILED):
                    return ("cpu", "int8")
                return ("cuda", "int8_float16")
        except Exception:
            pass
        return ("cpu", "int8")

    @staticmethod
    def _add_cuda_dll_dirs():
        """Make CUDA libs (cuBLAS/cuDNN) from the `nvidia-*-cu12` pip packages
        discoverable on Windows, so GPU works after a simple
        `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` - no system CUDA needed.
        No-op if the packages aren't installed."""
        if sys.platform != "win32":
            return
        # The optional GPU download (or a CUDA Toolkit already on the PC).
        try:
            gpu_accel.register_dll_dirs()
        except Exception:
            pass
        try:
            import importlib.util
            for pkg in ("nvidia.cublas", "nvidia.cudnn"):
                spec = importlib.util.find_spec(pkg)
                locs = getattr(spec, "submodule_search_locations", None) if spec else None
                if not locs:
                    continue
                binp = os.path.join(list(locs)[0], "bin")
                if not os.path.isdir(binp):
                    continue
                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(binp)
                    except Exception:
                        pass
                # ctranslate2 loads cuBLAS/cuDNN lazily via the standard search
                # order, so the bin dir must also be on PATH (add_dll_directory
                # alone isn't honored for its delayed loads).
                if binp not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = binp + os.pathsep + os.environ.get("PATH", "")
        except Exception:
            pass

    def load_model(self, name=None):
        name = name or cfg["whisper_model"]
        with self._model_lock:
            self._load_locked(name)

    def _users_guard(self):
        lock = getattr(self, "_users_lock", None)
        if lock is None:                     # a test double built without __init__
            lock = self._users_lock = threading.Lock()
        return lock

    def _load_locked(self, name):
        """Load ``name`` unless it's the resident model. The caller holds
        _model_lock (load_model, use_model)."""
        # About to be used: the idle sweep must not free it right now.
        self._last_model_use = time.monotonic()
        if self._model is None or getattr(self, "_model_name", None) != name:
            # A switch (e.g. the file tab's model): let the old model go
            # BEFORE building the new one, so both are never resident at
            # once (~1 GB each). A transcription still running on it keeps
            # its own reference and finishes normally.
            self._model = None
            self._model_name = None
            # Seen by transcribe(): a model is being BUILT (not just looked
            # up), so local chunks are about to wait on it.
            self._building_model = True
            try:
                fetch = getattr(self, "model_fetcher", None)
                if fetch is not None:
                    # Not on disk yet: wait for the shared download (it raises
                    # model_manager.DownloadError with a plain sentence).
                    self._fetching_model = True
                    try:
                        fetch(name)
                    finally:
                        self._fetching_model = False
                self._build_model_locked(name)
            finally:
                self._building_model = False

    def _oom_models(self):
        """Models the GPU ran out of memory with this session (they run on
        the processor; smaller ones still use the GPU)."""
        models = getattr(self, "_cuda_oom_models", None)
        if models is None:
            models = self._cuda_oom_models = set()
        return models

    def _gpu_failed_mid_use(self, failed, name, err):
        """A model on the GPU failed while transcribing: out of memory = this
        model goes to the processor; anything else = the GPU is off for this
        run (and the app remembers it). The failed model is dropped so the
        retry builds a CPU one."""
        logger.warning("GPU transcription failed (%s); retrying on CPU.", err)
        if gpu_accel.is_oom(err):
            self._oom_models().add(name)
        else:
            self._cuda_usable = False
            self._report_cuda_failure(err)
        with self._model_lock:
            if self._model is failed:
                self._model = None
                self._model_name = None

    def _report_cuda_failure(self, message):
        """Tell the app the GPU failed (on_cuda_failed, set by AppController),
        so a real failure is remembered - not just for this run."""
        cb = getattr(self, "on_cuda_failed", None)
        if cb:
            try:
                cb(message)
            except Exception:
                pass

    def _build_model_locked(self, name):
        from faster_whisper import WhisperModel
        dev, ct = self._whisper_device()
        # Try GPU first (if available), then CPU; and the offline cache
        # first, then online - revalidating with Hugging Face costs a
        # round trip before every load and can stall on a flaky network. A GPU load can fail when the
        # CUDA libraries are missing or VRAM is short - we fall back to CPU
        # rather than erroring, so dictation always works.
        # Use all physical CPU cores (ctranslate2 otherwise caps at a low
        # default), so transcription runs as fast as the machine allows.
        try:
            import psutil
            cpu_threads = psutil.cpu_count(logical=False) or 0
        except Exception:
            cpu_threads = os.cpu_count() or 0
        # Once we learn the CUDA libs are missing, don't keep retrying GPU
        # on every model switch - go straight to CPU.
        if getattr(self, "_cuda_usable", None) is False or name in AudioRecorder._oom_models(self):
            dev = "cpu"                   # GPU unusable, or THIS model doesn't fit on it
        if dev == "cuda":
            self._add_cuda_dll_dirs()
        attempts = []
        if dev == "cuda":
            attempts += [("cuda", ct, True), ("cuda", ct, False)]
        attempts += [("cpu", "int8", True), ("cpu", "int8", False)]
        last_err = None
        repaired_cache = False
        for d, c, local_only in attempts:
            # GPU already proven unusable earlier in THIS load (its warm-up
            # raised) - skip the remaining CUDA attempt. Building a second
            # CUDA model would skip the warm-up below and hand back a broken
            # GPU model that hangs on real inference - the "stuck on
            # Finalising" bug on machines where cuBLAS/cuDNN are missing.
            if d == "cuda" and (getattr(self, "_cuda_usable", None) is False
                                or name in AudioRecorder._oom_models(self)):
                continue
            try:
                m = WhisperModel(
                    name, device=d, compute_type=c, local_files_only=local_only,
                    cpu_threads=cpu_threads, num_workers=1)
                if d == "cuda":
                    # A CUDA model constructs even when the CUDA runtime libs
                    # (cuBLAS/cuDNN, e.g. cublas64_12.dll) are missing - it
                    # only fails when it actually computes. Force a tiny
                    # warm-up (unless the GPU is already proven good) so we
                    # catch that here and fall back to CPU instead of handing
                    # back a model that hangs on a real dictation.
                    if getattr(self, "_cuda_usable", None) is not True:
                        seg, _ = m.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)
                        list(seg)
                    self._cuda_usable = True
                self._model = m
                self._model_name = name
                self._last_model_use = time.monotonic()
                # A failed CUDA attempt's exception references this
                # frame (and the broken CUDA model): a cycle that kept
                # both models alive after an unload until a gc pass.
                last_err = None
                logger.info("Whisper on %s (%s, cpu_threads=%s)", d, c, cpu_threads)
                return
            except Exception as e:
                last_err = e
                # Only the GPU stack's own errors say anything about the GPU:
                # a damaged cache or being offline must not switch it off.
                if d == "cuda" and gpu_accel.is_cuda_error(e):
                    self._cuda_error = str(e)[:300]
                    if gpu_accel.is_oom(e):
                        AudioRecorder._oom_models(self).add(name)      # smaller models still fit
                    else:
                        self._cuda_usable = False  # remember: skip GPU next time
                        self._report_cuda_failure(self._cuda_error)
                logger.warning("Whisper load failed on %s/%s (offline=%s): %s",
                               d, c, local_only, e)
                if not repaired_cache and _looks_like_whisper_cache_error(e):
                    if _repair_whisper_model_cache(name):
                        repaired_cache = True
                        logger.info("Repaired Whisper cache for %s; retrying load", name)
                        continue
        if last_err:
            try:
                raise last_err
            finally:
                last_err = None        # no frame <-> traceback cycle

    @contextmanager
    def use_model(self, name=None):
        """The Whisper model for one transcription: loaded if needed, and
        counted as in use until the block ends, so the idle sweep never frees
        it mid-call. Load and hand-out happen in ONE _model_lock hold, so the
        caller always gets exactly the model it asked for - never None, never
        a model another thread switched to in between."""
        name = name or cfg["whisper_model"]
        with self._model_lock:
            self._load_locked(name)
            model = self._model
            if model is None:
                raise RuntimeError("The speech model could not be loaded.")
            with self._users_guard():
                self._model_users = getattr(self, "_model_users", 0) + 1
                self._last_model_use = time.monotonic()
        try:
            yield model
        finally:
            with self._users_guard():
                self._model_users = max(0, getattr(self, "_model_users", 1) - 1)
                self._last_model_use = time.monotonic()

    def maybe_unload_idle(self, idle_sec, now=None):
        """Free the Whisper model after ``idle_sec`` without use (the idle
        sweep, on a worker thread). Never waits: a load holds the lock for
        seconds, and then this tick simply skips. Never frees a model that is
        in use (use_model), during a recording, or while chunk threads run.
        Dropping the reference frees CTranslate2's memory at once (measured
        ~0.8 GB for large-v3-turbo on CPU); a reload takes ~4-9 s."""
        if not idle_sec or idle_sec <= 0:
            return False
        if not self._model_lock.acquire(blocking=False):
            return False
        try:
            # Holding _model_lock: no new lease can start (use_model takes it);
            # a lease ending meanwhile only lowers the count.
            with self._users_guard():
                users = getattr(self, "_model_users", 0)
            if self._model is None or users > 0 or getattr(self, "recording", False):
                return False
            if any(t.is_alive() for t in list(getattr(self, "_chunk_threads", []) or [])):
                return False
            t = time.monotonic() if now is None else now
            if t - getattr(self, "_last_model_use", 0.0) < idle_sec:
                return False
            name, self._model, self._model_name = self._model_name, None, None
        finally:
            self._model_lock.release()
        gc.collect()
        logger.info("Freed the idle speech model (%s)", name)
        return True

    def unload_model(self, name=None):
        with self._model_lock:
            if name is None or self._model_name == name:
                self._model = None
                self._model_name = None

    def _find_default_loopback_device(self):
        """
        Dynamically scans host APIs and devices to find the WASAPI loopback device
        associated with the active default system playback/output device.
        If no exact match is found, falls back to the first WASAPI loopback or any loopback device.
        """
        # PyAudioWPatch ships a native resolver that pairs the default output
        # device with its loopback twin - always prefer it. The name matching
        # below is only a fallback, and names can collide: a virtual device
        # like 'Speakers (Steam Streaming Microphone)' also starts with
        # 'Speakers', so a loose match can capture a silent virtual endpoint
        # instead of the real speakers.
        try:
            info = self.audio.get_default_wasapi_loopback()
            if info is not None and info.get("index") is not None:
                logger.info("Native default WASAPI loopback: '%s' (index %s)",
                            info.get("name", "?"), info.get("index"))
                return int(info["index"])
        except Exception as e:
            logger.debug("get_default_wasapi_loopback unavailable: %s", e)

        out_name = ""
        try:
            default_out = self.audio.get_default_output_device_info()
            out_name = default_out.get("name", "")
            if isinstance(out_name, bytes):
                out_name = out_name.decode("utf-8", errors="ignore")
            logger.info("Active system default output device: %s", out_name)
        except Exception as e:
            logger.warning("Could not get default output device info: %s", e)

        # Extract core name to match loopbacks, e.g. "Speakers" or "Headphones"
        core_name = ""
        if out_name:
            core_name = out_name.split("(")[0].strip() if "(" in out_name else out_name.strip()
            if len(core_name) <= 3 and "(" in out_name:
                parts = out_name.split("(")
                if len(parts) > 1:
                    core_name = parts[1].split(")")[0].strip()

        num_devices = self.audio.get_device_count()
        wasapi_loopbacks = []
        any_loopbacks = []
        
        for i in range(num_devices):
            try:
                dev = self.audio.get_device_info_by_index(i)
                if dev.get('maxInputChannels', 0) <= 0:
                    continue
                name = dev.get('name', '')
                if isinstance(name, bytes):
                    name = name.decode('utf-8', errors='ignore')
                
                host_idx = dev.get('hostApi', 0)
                host_api_name = self.audio.get_host_api_info_by_index(host_idx).get('name', '')
                
                is_wasapi = "wasapi" in host_api_name.lower()
                is_loop = "loopback" in name.lower() or dev.get('isLoopback', False) or dev.get('is_loopback', False)
                is_stereo_mix = "stereo mix" in name.lower() or "wave out" in name.lower() or "monitor" in name.lower()
                
                if is_wasapi and (is_loop or is_stereo_mix):
                    wasapi_loopbacks.append((i, name))
                elif is_loop or is_stereo_mix:
                    any_loopbacks.append((i, name))
            except Exception as e:
                logger.debug("Error scanning audio device %d: %s", i, e)
                
        if wasapi_loopbacks:
            # The loopback twin carries the FULL output device name
            # ("Speakers (Realtek(R) Audio) [Loopback]") - match on that first.
            # The loose core-name match alone picked the wrong device when
            # several share a prefix (e.g. Steam's virtual speakers).
            if out_name:
                for idx, name in wasapi_loopbacks:
                    if out_name.lower() in name.lower():
                        logger.info("Matched WASAPI loopback device: '%s' (index %d) for output '%s'", name, idx, out_name)
                        return idx
            if core_name:
                for idx, name in wasapi_loopbacks:
                    if core_name.lower() in name.lower():
                        logger.info("Loosely matched WASAPI loopback device: '%s' (index %d) for output '%s'", name, idx, out_name)
                        return idx
            logger.info("Using first available WASAPI loopback device: '%s' (index %d)", wasapi_loopbacks[0][1], wasapi_loopbacks[0][0])
            return wasapi_loopbacks[0][0]

        if any_loopbacks:
            if out_name:
                for idx, name in any_loopbacks:
                    if out_name.lower() in name.lower():
                        logger.info("Matched fallback loopback device: '%s' (index %d)", name, idx)
                        return idx
            if core_name:
                for idx, name in any_loopbacks:
                    if core_name.lower() in name.lower():
                        logger.info("Loosely matched fallback loopback device: '%s' (index %d)", name, idx)
                        return idx
            logger.info("Using first available loopback device: '%s' (index %d)", any_loopbacks[0][1], any_loopbacks[0][0])
            return any_loopbacks[0][0]
            
        logger.warning("No loopback audio devices found on the system.")
        return None

    def _loopback_to_mono16k(self, raw):
        """Convert raw float32 loopback bytes (device rate/channels) to the
        16 kHz mono float32 stream the rest of the pipeline expects."""
        arr = np.frombuffer(raw, dtype=np.float32)
        # Stereo to mono. Truncate to a whole number of frames first so a
        # short/misaligned read can't crash reshape.
        if self._loopback_channels > 1 and len(arr) > 0:
            usable = len(arr) - (len(arr) % self._loopback_channels)
            arr = (arr[:usable].reshape(-1, self._loopback_channels).mean(axis=1)
                   if usable > 0 else arr[:0])
        if self._loopback_rate != cfg["sample_rate"] and len(arr) > 0:
            duration = len(arr) / self._loopback_rate
            target = int(duration * cfg["sample_rate"])
            if target > 0:
                indices = np.linspace(0, len(arr) - 1, target)
                arr = np.interp(indices, np.arange(len(arr)), arr).astype(np.float32)
            else:
                arr = arr[:0]
        return arr.astype(np.float32, copy=False)

    def _read_available_loopback(self, stream):
        """Non-blockingly read whatever the loopback stream has buffered, as
        16 kHz mono. Returns an empty array when nothing is playing - WASAPI
        loopback devices deliver NO frames while the computer is silent, so a
        blocking read could stall forever (and closing the stream under that
        blocked read crashed the whole app natively)."""
        try:
            avail = stream.get_read_available()
            if avail <= 0:
                return np.zeros(0, dtype=np.float32)
            raw = stream.read(avail, exception_on_overflow=False)
            arr = self._loopback_to_mono16k(raw)
            if len(arr) > 0:
                # Remember the loudest system-audio sample so the meeting UI
                # can tell "nothing was playing on the captured device" apart
                # from a real transcription failure.
                self._loopback_peak = max(self._loopback_peak, float(np.abs(arr).max()))
            return arr
        except Exception as e:
            logger.debug("loopback read failed: %s", e)
            return np.zeros(0, dtype=np.float32)

    @staticmethod
    def _read_input_draining(stream, sr):
        """Read at least one chunk from an input stream, draining any backlog
        in the same read. While Whisper saturates the CPU cores transcribing a
        chunk, this loop stalls for hundreds of ms; with fixed-size reads the
        backlog overflowed PortAudio's buffer and the overflowed audio was
        silently dropped (words went missing mid-dictation/meeting). Reading
        the whole backlog at once keeps up instead. Single reads are capped at
        ~2s; anything beyond that drains on the next tick."""
        avail = 0
        try:
            avail = int(stream.get_read_available())
        except Exception:
            pass
        read_n = max(cfg["chunk_size"], min(avail, sr * 2))
        return stream.read(read_n, exception_on_overflow=False)

    def start_recording(self, capture_mode=None, language=None, speech=None):
        """``speech``: (backend, model) THIS take transcribes with - a Smart
        Action's own pick (engine_choice.smart_speech_session); None = the
        global cfg backend / whisper_model."""
        try:
            self._start_recording_impl(capture_mode, language, speech)
        except BaseException:
            # A device that wouldn't open is not a recording - left True, the
            # idle sweep would never free the speech model again.
            self.recording = False
            raise

    def _start_recording_impl(self, capture_mode=None, language=None, speech=None):
        self.recording = True
        self._last_model_use = time.monotonic()
        # Per-session language: the meeting window passes its selector value;
        # plain dictation passes nothing, which resets to the global default.
        self.language_override = language or None
        # Per-take speech engine, the same way: set before any piece is cut
        # (pieces transcribe while recording) and reset by every other start
        # (Alt+R, a meeting, the Live Assistant).
        backend, model = (tuple(speech) + (None, None))[:2] if speech else (None, None)
        self.backend_override = backend or None
        self.model_override = model or None
        self._capture_mode = capture_mode
        self._loopback_peak = 0.0
        self.paused = False
        with self._chunk_lock:
            self._chunk_frames     = []
            self._chunk_results    = {}
            self.partial_text      = ""
            self._chunk_idx        = 0
            self._samples_in_chunk = 0
            self._chunk_errors     = []
            self._chunk_silence_before = {}
            self._chunk_sources    = {}
        self._session_lang  = None
        self._chunk_threads = []
        self._record_error  = ""
        self._abort         = False
        self._tail_speech_sec = None
        self._cloud_capped  = False
        self._level_peak    = 0.05
        # Full wall-clock recording retained for post-meeting speaker
        # diarization + a clean speaker-attributed re-transcription. Capped so a
        # marathon meeting can't grow memory without bound (~0.23 GB/hour).
        self._full_audio_frames = []
        self._full_audio_len = 0
        self._full_audio_truncated = False
        self._full_audio_max = cfg["sample_rate"] * 60 * 120  # ~2 hours

        open_args = {
            "format": pyaudio.paFloat32,
            "channels": 1,
            "rate": cfg["sample_rate"],
            "input": True,
            "frames_per_buffer": cfg["chunk_size"],
        }
        # Resolve what to capture. `capture_mode` is passed explicitly by the
        # caller: the meetings window passes "smart_meeting" (system + mic),
        # "system_only" (system sound, no mic) or "default_mic"; plain hotkey
        # dictation passes nothing and just uses the configured mic. This keeps
        # loopback/system-audio capture scoped to meetings - it can never leak
        # into ordinary dictation via a shared config key.
        self.mic_stream = None
        is_loopback = False
        self._loopback_rate = cfg["sample_rate"]
        self._loopback_channels = 1

        actual_device_index = None

        if capture_mode in ("smart_meeting", "system_only"):
            discovered = self._find_default_loopback_device()
            if discovered is not None:
                actual_device_index = discovered
                is_loopback = True
                try:
                    dev_info = self.audio.get_device_info_by_index(actual_device_index)
                    self._loopback_rate = int(dev_info.get("defaultSampleRate", 48000))
                    self._loopback_channels = int(dev_info.get("maxInputChannels", 2))
                except Exception as e:
                    logger.warning("Could not query loopback device rate/channels: %s", e)
                    self._loopback_rate = 48000
                    self._loopback_channels = 2
            else:
                logger.warning("System-audio capture selected but no loopback device discovered. Recording default microphone only.")
        elif capture_mode in ("default_mic", "default"):
            actual_device_index = None
        else:
            # Dictation / legacy: input_device_index may name a specific input
            # device. Meeting-mode sentinels left over in this key are treated as
            # the default microphone so a stale config never makes plain
            # dictation start capturing system audio.
            device_index = cfg.get("input_device_index")
            if device_index not in (None, "", "default", "default_mic", "smart_meeting", "system_only"):
                try:
                    actual_device_index = int(device_index)
                except (TypeError, ValueError):
                    actual_device_index = None

        # Open primary stream
        if actual_device_index is not None:
            open_args["input_device_index"] = actual_device_index
            
        if is_loopback:
            open_args["rate"] = self._loopback_rate
            open_args["channels"] = self._loopback_channels
            open_args["frames_per_buffer"] = int(cfg["chunk_size"] * (self._loopback_rate / cfg["sample_rate"]))
            
        self.stream = self.audio.open(**open_args)

        # If primary stream is a loopback, open a secondary stream for the default
        # microphone - but only in smart_meeting mode. "system_only" deliberately
        # records the computer's sound with NO mic.
        if is_loopback and capture_mode == "smart_meeting":
            try:
                mic_args = {
                    "format": pyaudio.paFloat32,
                    "channels": 1,
                    "rate": cfg["sample_rate"],
                    "input": True,
                    "frames_per_buffer": cfg["chunk_size"],
                }
                self.mic_stream = self.audio.open(**mic_args)
                logger.info("Successfully opened secondary default mic stream to mix with loopback!")
            except Exception as me:
                logger.warning("Could not open secondary mic stream: %s", me)
                
        self.stop_requested = False
        # The recording thread OWNS the streams: it is the only reader and the
        # only closer. Stream handles are passed as locals so a later
        # start_recording can never make an old thread close the new streams.
        self._record_thread = threading.Thread(
            target=self._record, args=(is_loopback, self.stream, self.mic_stream),
            daemon=True)
        self._record_thread.start()

    def _record(self, is_loopback, stream, mic_stream):
        try:
            self._record_loop(is_loopback, stream, mic_stream)
        finally:
            self._drain_and_close_streams(is_loopback, stream, mic_stream)

    def _drain_and_close_streams(self, is_loopback, stream, mic_stream):
        """Pull any tail audio still in PortAudio's buffers, then close the
        streams. Runs on the recording thread, which owns the streams -
        closing them from another thread while a read is in flight is a
        native crash (access violation inside PortAudio), which is exactly
        what happened when a meeting was stopped while the loopback read sat
        blocked on a silent device."""
        # Stopped while paused: the tail is audio from after Pause - drain it,
        # never keep it.
        keep = not getattr(self, "paused", False)
        try:
            avail = stream.get_read_available()
            if avail > 0:
                extra = stream.read(avail, exception_on_overflow=False)
                if extra and keep:
                    if is_loopback:
                        extra = self._loopback_to_mono16k(extra).tobytes()
                    if extra:
                        with self._chunk_lock:
                            self._chunk_frames.append(extra)
        except Exception as e:
            logger.debug("Failed to drain primary stream: %s", e)
        if mic_stream is not None:
            try:
                avail = mic_stream.get_read_available()
                if avail > 0:
                    extra_mic = mic_stream.read(avail, exception_on_overflow=False)
                    if extra_mic and keep:
                        with self._chunk_lock:
                            self._chunk_frames.append(extra_mic)
            except Exception as e:
                logger.debug("Failed to drain secondary mic stream: %s", e)
        for s in (stream, mic_stream):
            if s is None:
                continue
            try:
                s.stop_stream()
                s.close()
            except Exception:
                pass

    def _record_loop(self, is_loopback, stream, mic_stream):
        sr          = cfg["sample_rate"]
        frame_dur   = cfg["chunk_size"] / sr
        silence_trigger_sec = cfg_float("silence_trigger_sec", SILENCE_TRIGGER_SEC, 0.2, 5.0)
        min_speech_sec      = cfg_float("min_speech_sec", MIN_SPEECH_SEC, 0.2, 10.0)
        max_speech_sec      = cfg_float("max_speech_sec", MAX_SPEECH_SEC, 2.0, 120.0)

        vad_buf      = []
        speech_sec   = 0.0
        silence_sec  = 0.0
        noise_hist   = []
        read_errors   = 0

        post_roll_chunks = 0
        # ~512ms post-roll after stop to catch final spoken syllables in flight.
        # 256ms still clipped word endings when the hotkey was pressed while the
        # last word was being finished; the extra quarter second costs little.
        max_post_roll = 8

        # Pending loopback samples (16 kHz mono) waiting to be mixed with mic
        # frames, and how long the loopback has delivered nothing (silence).
        loop_fifo = np.zeros(0, dtype=np.float32)
        loop_idle_sec = 0.0

        # Meetings: continuous program audio (videos, music beds, long
        # talkers) may never show a VAD pause, so the silence-gap chunker
        # would sit on the whole recording until Stop and the live transcript
        # stayed empty. On a meeting capture, force a chunk on a fixed
        # cadence - gated on energy so quiet stretches don't send
        # hallucination-prone silence to Whisper.
        meeting_cadence = self._capture_mode is not None
        MEETING_CHUNK_SEC = 10.0
        if self.fast_live_chunks and meeting_cadence:   # meetings only, never dictation
            # Live Assistance: answers can only be as live as the transcript, so
            # cut short pieces - at a brief pause or every 5 s. The meeting's
            # final transcript is re-done over the full recording afterwards,
            # so the shorter context costs the notes nothing.
            MEETING_CHUNK_SEC = 5.0
            silence_trigger_sec = min(silence_trigger_sec, 0.6)
            min_speech_sec = min(min_speech_sec, 1.0)
        buffered_sec = 0.0
        chunk_peak = 0.0
        # Mic + system sound meetings: each source's energy since the last
        # chunk (chunk_source_energy). Pause: whether the last read was paused.
        two_sources = is_loopback and mic_stream is not None
        src_mic_e = 0.0
        src_call_e = 0.0
        was_paused = False
        # System sound only: a silent loopback delivers no frames at all, so
        # the meters get one zero reading when it goes quiet (not a frozen bar).
        call_quiet = False

        def cut_chunk(frames, silence_before, sources):
            # The audio since the last chunk becomes chunk N, transcribed on
            # its own thread.
            with self._chunk_lock:
                chunk_audio = np.frombuffer(
                    b"".join(frames), dtype=np.float32).copy()
                idx = self._chunk_idx
                self._chunk_idx   += 1
                self._chunk_frames = []
                self._chunk_silence_before[idx] = float(silence_before)
                chunk_sources = getattr(self, "_chunk_sources", None)
                if sources is not None and chunk_sources is not None:
                    chunk_sources[idx] = sources
            t = threading.Thread(target=self._transcribe_chunk,
                                 args=(chunk_audio, idx), daemon=True)
            self._chunk_threads.append(t)
            t.start()

        while self.recording or post_roll_chunks > 0:
            if self.stop_requested and post_roll_chunks == 0:
                if (self._abort or silence_sec >= 0.3
                        or getattr(self, "paused", False)
                        or (not meeting_cadence and speech_sec == 0.0
                            and self._chunk_idx > 0)):
                    # No syllable in flight - Esc (nothing gets transcribed),
                    # or the speaker had already stopped talking (a pause, or
                    # nothing since the last chunk) - so no post-roll: the
                    # result comes half a second sooner. Before any speech
                    # was ever detected the post-roll stays, in case the
                    # detector hasn't calibrated to this voice yet (meetings
                    # cut chunks on a timer, so a chunk proves nothing there).
                    # A paused meeting never gets one: that audio is after
                    # Pause.
                    self.recording = False
                    break
                post_roll_chunks = max_post_roll

            if post_roll_chunks > 0:
                post_roll_chunks -= 1
                if post_roll_chunks == 0:
                    self.recording = False

            try:
                mic_part = call_part = None
                if is_loopback and mic_stream is not None:
                    # smart_meeting: the MIC is the loop's clock (it always
                    # delivers frames); the loopback is drained opportunistically
                    # and mixed in. The old code blocked on the loopback read
                    # first - WASAPI loopbacks deliver nothing while the
                    # computer is silent, so the whole loop stalled (not even
                    # the mic was transcribed) and stopping then crashed the
                    # app by closing the stream under the blocked read.
                    mic_data = self._read_input_draining(mic_stream, sr)
                    arr_mic = np.frombuffer(mic_data, dtype=np.float32)
                    loop_chunk = self._read_available_loopback(stream)
                    mic_part, call_part = arr_mic, loop_chunk
                    if len(loop_chunk) > 0:
                        loop_fifo = np.concatenate([loop_fifo, loop_chunk])
                        # Clock-drift safety valve only - generous, because
                        # dropping from this queue loses system audio.
                        if len(loop_fifo) > sr * 10:
                            loop_fifo = loop_fifo[-sr * 10:]
                    n = min(len(arr_mic), len(loop_fifo))
                    if n > 0:
                        mixed = arr_mic.copy()
                        mixed[:n] = np.clip((arr_mic[:n] + loop_fifo[:n]) * 0.75, -1.0, 1.0)
                        loop_fifo = loop_fifo[n:]
                        data = mixed.tobytes()
                    else:
                        data = mic_data
                elif is_loopback:
                    # system_only: poll instead of blocking - a read blocked on
                    # a silent loopback device cannot be interrupted or closed
                    # safely. While silent, synthesize the occasional silence
                    # frame so the VAD still flushes the last spoken chunk to
                    # the live transcript.
                    loop_chunk = self._read_available_loopback(stream)
                    if len(loop_chunk) == 0:
                        time.sleep(0.05)
                        loop_idle_sec += 0.05
                        if loop_idle_sec >= silence_trigger_sec and speech_sec >= min_speech_sec:
                            loop_idle_sec = 0.0
                            data = np.zeros(cfg["chunk_size"], dtype=np.float32).tobytes()
                        else:
                            if not call_quiet:
                                call_quiet = True
                                on_src = getattr(self, "on_source_levels", None)
                                if (on_src is not None and meeting_cadence
                                        and not getattr(self, "paused", False)):
                                    try:
                                        on_src(0.0, 0.0)
                                    except Exception:
                                        pass
                            continue
                    else:
                        call_quiet = False
                        loop_idle_sec = 0.0
                        data = loop_chunk.tobytes()
                else:
                    data = self._read_input_draining(stream, sr)

                read_errors = 0

                if getattr(self, "paused", False):
                    # Paused meeting: the streams were read above (so they
                    # never overflow, and are never touched from another
                    # thread), but nothing is kept. On the first paused read
                    # the speech said before Pause becomes a chunk, so it
                    # reaches the live transcript now.
                    if not was_paused:
                        was_paused = True
                        if vad_buf and (speech_sec > 0.0 or chunk_peak >= 0.01):
                            cut_chunk(vad_buf, silence_sec,
                                      (src_mic_e, src_call_e) if two_sources else None)
                            vad_buf      = []
                            speech_sec   = 0.0
                            silence_sec  = 0.0
                            buffered_sec = 0.0
                            chunk_peak   = 0.0
                            src_mic_e = src_call_e = 0.0
                    # System sound queued for mixing is from the pause too.
                    loop_fifo = loop_fifo[:0]
                    loop_idle_sec = 0.0
                    continue
                was_paused = False

                arr = np.frombuffer(data, dtype=np.float32)
                this_dur = (len(arr) / sr) if len(arr) > 0 else frame_dur
                buffered_sec += this_dur
                if len(arr) > 0:
                    chunk_peak = max(chunk_peak, float(np.abs(arr).max()))
                rms = float(np.sqrt(np.mean(arr ** 2))) if len(arr) > 0 else 0.0

                if meeting_cadence:
                    # Meetings: how loud the mic and the call are, BEFORE the
                    # mix (the "You" / "The call" meters).
                    if two_sources:
                        m_part, c_part = mic_part, call_part
                    elif is_loopback:
                        m_part, c_part = None, arr          # system sound only
                    else:
                        m_part, c_part = arr, None          # microphone only
                    mic_rms = AudioRecorder._samples_rms(m_part)
                    call_rms = AudioRecorder._samples_rms(c_part)
                    if two_sources:
                        src_mic_e += mic_rms * mic_rms * len(m_part)
                        src_call_e += call_rms * call_rms * (len(c_part) if c_part is not None else 0)
                    on_src = getattr(self, "on_source_levels", None)
                    if on_src is not None:
                        try:
                            on_src(mic_rms, call_rms)
                        except Exception:
                            pass

                noise_hist.append(rms)
                if len(noise_hist) > 200:
                    noise_hist.pop(0)
                n10        = sorted(noise_hist)[max(0, len(noise_hist) // 10)]
                threshold  = max(n10 * 4.0, 0.006)
                is_speech  = rms > threshold

                if self.on_levels:
                    # Auto-normalize: track rolling peak amplitude so bars
                    # adapt to whatever mic gain the user has. Loud speech
                    # fills the bars, quiet speech still produces visible
                    # motion. Peak decays ~halves every 5s of silence so
                    # bars recalibrate if mic gain changes.
                    peak_now = float(np.abs(arr).max()) if len(arr) > 0 else 0.0
                    if peak_now > self._level_peak:
                        self._level_peak = peak_now
                    else:
                        self._level_peak = max(0.01, self._level_peak * 0.995)
                    # Use 0.7 * peak as the "full bar" reference - leaves
                    # headroom so transient loud sounds visibly spike past
                    # normal speech levels.
                    scale = max(0.01, self._level_peak * 0.7)
                    n, sz  = 20, max(len(arr) // 20, 1)
                    levels = [min(float(np.abs(arr[i*sz:(i+1)*sz]).mean()) / scale, 1.0)
                              for i in range(n)]
                    self.on_levels(levels)

                vad_buf.append(data)
                with self._chunk_lock:
                    self._chunk_frames.append(data)
                    # Retain the full wall-clock track (post-meeting diarization)
                    # until the memory cap, then stop growing it.
                    if not self._full_audio_truncated:
                        self._full_audio_frames.append(data)
                        self._full_audio_len += len(arr)
                        if self._full_audio_len > self._full_audio_max:
                            self._full_audio_truncated = True

                if is_speech:
                    speech_sec  += this_dur
                    silence_sec  = 0.0
                elif speech_sec > 0:
                    silence_sec += this_dur

                total_sec    = speech_sec + silence_sec
                force_chunk = (meeting_cadence
                               and buffered_sec >= MEETING_CHUNK_SEC
                               and chunk_peak >= 0.01)
                should_chunk = (
                    (silence_sec >= silence_trigger_sec and speech_sec >= min_speech_sec)
                    or total_sec >= max_speech_sec
                    or force_chunk
                )

                # During background post-roll, accumulate everything into remaining frames
                # rather than starting new parallel chunk transcription threads.
                if post_roll_chunks > 0:
                    should_chunk = False

                if should_chunk and (speech_sec >= min_speech_sec or force_chunk):
                    cut_chunk(vad_buf, silence_sec,
                              (src_mic_e, src_call_e) if two_sources else None)
                    vad_buf      = []
                    speech_sec   = 0.0
                    silence_sec  = 0.0
                    buffered_sec = 0.0
                    chunk_peak   = 0.0
                    src_mic_e = src_call_e = 0.0

            except Exception as e:
                read_errors += 1
                if read_errors >= 5:
                    self._record_error = f"!audio:{e}"
                    self.recording = False
                    break
                time.sleep(0.05)
        # Speech in the tail (the audio since the last chunk): transcribe()
        # skips a tail without any.
        self._tail_speech_sec = speech_sec

    def _transcribe_chunk(self, audio, idx):
        try:
            backend = AudioRecorder._backend(self)
            if backend == "managed":
                text, lang = self._run_managed(audio)
            elif backend == "google" and cfg["google_api_key"]:
                text, lang = self._run_google(audio)
            elif backend == "mistral" and cfg.get("mistral_api_key"):
                text, lang = self._run_mistral(audio)
            else:
                text, lang = self._run_local(audio)

            if lang and lang.startswith("!"):
                with self._chunk_lock:
                    self._chunk_errors.append(lang)
                return

            with self._chunk_lock:
                self._chunk_results[idx] = text
                partial = " ".join(
                    self._chunk_results[i]
                    for i in range(self._chunk_idx)
                    if i in self._chunk_results and self._chunk_results[i]
                )
            if self.on_partial and partial:
                self.on_partial(partial)
            if lang and not lang.startswith("!") and self.on_lang_detected:
                self.on_lang_detected(lang, LANG_NAMES.get(lang, lang.upper()))
            try:
                if text and self.on_chunk_complete:
                    self.on_chunk_complete(idx, text, lang or "")
            except Exception as e:
                logger.warning("on_chunk_complete failed: %s", e)
        except Exception as e:
            with self._chunk_lock:
                self._chunk_errors.append(f"!transcribe:{_friendly_transcription_error(e)}")

    def request_abort(self):
        """Cooperative cancel for an in-flight transcribe(): stop waiting on
        chunk threads and return immediately, so Esc frees the app fast instead
        of blocking on a slow/stuck local transcription."""
        self._abort = True
        self.stop_requested = True

    def stop_recording(self):
        self.stop_requested = True

        # The recording thread finishes its post-roll reads, drains the device
        # tail, and closes the streams itself (it owns them). NEVER close the
        # streams from this thread: a cross-thread close while a read is in
        # flight is a native PortAudio crash - the meeting-stop crash.
        t = getattr(self, "_record_thread", None)
        if t is not None:
            t.join(timeout=5.0)
            if t.is_alive():
                logger.warning("Recording thread still busy; it will close its streams itself.")

        self.recording = False
        self.mic_stream = None

    def transcribe(self):
        pending = list(self._chunk_threads)
        # A cold model load (e.g. after an idle unload) holds _model_lock and
        # the chunks queue behind it: start their time budget once it's done,
        # or a slow load would silently drop the first sentences.
        load_wait = time.time()
        if getattr(self, "_building_model", False) and AudioRecorder._runs_locally(self):
            # A model still downloading (model_fetcher) has no time limit
            # here: the pieces wait for it instead of being dropped.
            while (getattr(self, "_building_model", False)
                   and (time.time() - load_wait < 90 or getattr(self, "_fetching_model", False))
                   and not getattr(self, "_abort", False)):
                time.sleep(0.05)
        self._last_load_wait = time.time() - load_wait
        budget = max(30, min(180, 5 * len(pending)))
        deadline = time.time() + budget
        for t in pending:
            # Poll in short slices so Esc (request_abort) is honored within
            # ~0.15s instead of blocking up to the full budget on a slow chunk.
            while t.is_alive():
                if self._abort:
                    return "", "!aborted:Cancelled."
                if time.time() > deadline:
                    break
                t.join(timeout=0.15)
            try:
                with self._chunk_lock:
                    done = sum(1 for i in range(self._chunk_idx) if i in self._chunk_results)
                if self.on_finalising:
                    self.on_finalising(done, self._chunk_idx)
            except Exception:
                pass

        if self._abort:
            return "", "!aborted:Cancelled."
        if self._record_error:
            return "", self._record_error
        # A failed chunk still fails this pass (callers show the error), but
        # the tail is transcribed anyway and everything that DID transcribe is
        # kept in partial_text - a meeting can then keep its notes.
        self.partial_text = ""
        with self._chunk_lock:
            chunk_error = self._chunk_errors[0] if self._chunk_errors else ""

        remaining = np.frombuffer(b"".join(self._chunk_frames), dtype=np.float32).copy()
        last_text, detected = "", ""

        backend = AudioRecorder._backend(self)       # this take's (a Smart Action's own pick)
        is_cloud = (backend in ("managed", "google", "mistral"))
        min_samples = cfg["sample_rate"] // 4 if is_cloud else cfg["sample_rate"] // 2

        # Smart Audio Padding: Pad short final audio snippets with silence (zeros)
        # to satisfy the backend's minimum length requirement rather than discarding them.
        if 0 < len(remaining) < min_samples:
            padding_len = min_samples - len(remaining)
            remaining = np.concatenate([remaining, np.zeros(padding_len, dtype=np.float32)])

        # A tail at the noise floor would only make Whisper hallucinate (it
        # tends to echo the custom-vocabulary prompt on near-silence), so the
        # final blob must carry actual signal to be worth transcribing.
        # No speech since the last chunk (the speaker paused before stopping):
        # every word is transcribed already - decoding the silence would only
        # add a second and invite a made-up "Thank you."
        # Dictation only: meetings cut chunks on a timer, so there a chunk
        # doesn't prove the detector ever heard the (maybe quiet) speaker.
        silent_tail = (getattr(self, "_capture_mode", None) is None and self._chunk_idx > 0
                       and getattr(self, "_tail_speech_sec", None) == 0.0)
        if (not silent_tail and len(remaining) >= min_samples
                and float(np.abs(remaining).max()) >= 0.004):
            if backend == "managed":
                last_text, detected = self._run_managed(remaining)
            elif backend == "google" and cfg["google_api_key"]:
                last_text, detected = self._run_google(remaining)
            elif backend == "mistral" and cfg.get("mistral_api_key"):
                last_text, detected = self._run_mistral(remaining)
            else:
                last_text, detected = self._run_local(remaining)

        tail_error = ""
        if detected and (detected.startswith("!google:") or detected.startswith("!mistral:") or detected.startswith("!managed:")):
            tail_error, last_text, detected = detected, "", ""

        with self._chunk_lock:
            if last_text:
                self._chunk_results[self._chunk_idx] = last_text
            parts = [
                self._chunk_results[i]
                for i in range(self._chunk_idx + 1)
                if i in self._chunk_results and self._chunk_results[i]
            ]
            full_text = " ".join(parts)

        if chunk_error or tail_error:
            self.partial_text = full_text.strip()
            return "", chunk_error or tail_error

        if detected and self.on_lang_detected:
            self.on_lang_detected(detected, LANG_NAMES.get(detected, detected.upper()))
        return full_text.strip(), detected or "en"

    def get_full_audio(self):
        """The full retained wall-clock recording as 16 kHz mono float32 (empty
        array if nothing was retained). Used post-meeting for diarization + a
        clean speaker-attributed re-transcription."""
        with self._chunk_lock:
            frames = b"".join(self._full_audio_frames)
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(frames, dtype=np.float32).copy()

    def transcribe_segments(self, audio, language=None, model_name=None,
                            on_progress=None, should_cancel=None):
        """Transcribe a full audio array into timestamped segments using the
        LOCAL Whisper model: a list of {"start", "end", "text"} in seconds, for
        the post-meeting speaker-attributed transcript and the file-transcription
        tab. Returns [] on failure or very short audio, and None when
        ``should_cancel`` asked to stop mid-way. Holds the shared inference lock.

        ``on_progress(done_seconds, total_seconds)`` fires as segments stream
        out of the decoder - faster-whisper's segments are a lazy generator, so
        consumption IS progress. ``model_name`` overrides the configured model
        (the file tab lets the user pick per-job)."""
        try:
            audio = np.asarray(audio, dtype=np.float32)
        except Exception:
            return []
        sr = cfg["sample_rate"]
        if len(audio) < sr // 2:
            return []
        try:
            # In use for the whole decode (a file job can take an hour).
            for attempt in (1, 2):
                failed = None
                with self.use_model(model_name) as model:
                    t0 = time.monotonic()
                    try:
                        return self._segments_with(model, audio, sr, language,
                                                   on_progress, should_cancel,
                                                   gpu_errors=(attempt == 1))
                    except _GpuDecodeError as g:
                        failed, err = model, g.message
                    finally:
                        # Decode time only (no download/load): what the GPU would cut.
                        self._last_segments_seconds = time.monotonic() - t0
                # The GPU failed mid-file: the processor, once - never a silent
                # "no speech was detected".
                model = None
                self._gpu_failed_mid_use(failed, model_name or cfg["whisper_model"], err)
                del failed
            return []
        except Exception as e:
            logger.warning("transcribe_segments: model load failed: %s", e)
            return []

    def _segments_with(self, model, audio, sr, language, on_progress, should_cancel,
                       gpu_errors=False):
        # An explicit language (a file job always passes its own, "auto"
        # included) wins; only None / "" falls back to this session's.
        lang_setting = language or self._lang_setting()
        lang_arg = None if lang_setting in ("auto", "multi", "", None) else lang_setting
        # Mixed languages: faster-whisper re-detects the language per window
        # instead of locking onto the first one it heard.
        extra = {"multilingual": True} if lang_setting == "multi" else {}
        # No glossary prompt on long audio: with condition_on_previous_text off
        # it only ever reached the first 30 s window - where it could write the
        # terms into silence or unclear speech. Instead every segment gets its
        # near-miss spellings snapped to the terms, from start to finish.
        terms = vocabulary.load_terms(cfg)
        self._last_segments_language = None
        try:
            with self._infer_lock:
                kwargs = dict(
                    language=lang_arg, initial_prompt=None,
                    beam_size=4,
                    # Anti-hallucination (meeting re-transcription): VAD + restored
                    # confidence/no-speech thresholds + no carry-over context, so
                    # silent/music gaps aren't captioned with invented filler.
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                    condition_on_previous_text=False,
                    compression_ratio_threshold=2.4,
                    log_prob_threshold=-1.0,
                    no_speech_threshold=0.6,
                    repetition_penalty=1.3, no_repeat_ngram_size=5,
                    temperature=[0.0, 0.2, 0.4, 0.6, 0.8])
                try:
                    segs, _info = model.transcribe(audio, **kwargs, **extra)
                except TypeError:
                    if not extra:
                        raise
                    # An older faster-whisper without multilingual=.
                    segs, _info = model.transcribe(audio, **kwargs)
                detected = getattr(_info, "language", None)
                self._last_segments_language = detected if isinstance(detected, str) and detected else None
                total = len(audio) / float(sr)
                out = []
                for s in segs:
                    if should_cancel is not None and should_cancel():
                        return None
                    if s.text.strip():
                        out.append({"start": float(s.start), "end": float(s.end),
                                    "text": vocabulary.correct_spellings(s.text.strip(), terms)})
                    if on_progress is not None:
                        try:
                            on_progress(min(float(s.end), total), total)
                        except Exception:
                            pass
                return out
        except Exception as e:
            if gpu_errors and _model_device(model) == "cuda" and gpu_accel.is_cuda_error(e):
                raise _GpuDecodeError(str(e)) from None
            logger.warning("transcribe_segments failed: %s", e)
            return []

    def _runs_locally(self):
        """Will this dictation's pieces certainly go through local Whisper?
        The local backend, or a cloud backend without its key (it records with
        the local model). A cloud backend that fell back once may well work
        again - it doesn't wait for a local model it probably won't use."""
        b = AudioRecorder._backend(self)
        if b == "local":
            return True
        if b == "mistral" and not (cfg.get("mistral_api_key") or "").strip():
            return True
        return b == "google" and not (cfg.get("google_api_key") or "").strip()

    def _lang_setting(self):
        """The language for THIS recording session: an explicit per-session
        override (the meeting window's selector) wins over the global default."""
        return self.language_override or cfg.get("language", "auto")

    def _backend(self):
        """The speech backend of THIS take: a Smart Action's own pick
        (start_recording speech=) wins over the global cfg["backend"] -
        except a cloud pick once Privacy mode is on (turned on mid-take):
        the rest of the take stays on this computer."""
        override = getattr(self, "backend_override", None)
        if isinstance(override, str) and override and (
                override == "local" or not cfg.get("privacy_mode")):
            return override
        return cfg.get("backend") or "local"

    def _speech_model(self):
        """The local Whisper model of THIS take (a Smart Action's own pick, or
        the global cfg["whisper_model"])."""
        override = getattr(self, "model_override", None)
        if isinstance(override, str) and override:
            return override
        return cfg.get("whisper_model") or "base"

    def _run_local(self, audio):
        sr = cfg["sample_rate"]
        if len(audio) < sr // 2:
            return "", "en"
        out = self._run_local_once(audio)
        if AudioRecorder._backend(self) != "local":
            # A cloud backend ran locally (no key, cap, offline) - and it
            # worked: the next dictation loads the model early too.
            self._local_fallback_at = time.monotonic()
        return out

    def _fallback_to_local_or_error(self, audio, backend, reason):
        try:
            return self._run_local(audio)
        except Exception as e:
            friendly = _friendly_transcription_error(e)
            logger.warning(
                "%s cloud failed (%s), and local fallback also failed: %s",
                backend, reason, friendly,
            )
            return "", f"!{backend}:{reason} Local fallback is unavailable: {friendly}"

    def _run_local_once(self, audio):
        name = AudioRecorder._speech_model(self)
        with self.use_model(name) as model:
            try:
                return self._run_local_with(model, audio)
            except Exception as e:
                if _model_device(model) != "cuda" or not gpu_accel.is_cuda_error(e):
                    raise
                failed, err = model, str(e)   # the text only: no traceback cycle
        # The GPU failed mid-dictation (out of memory, driver hiccup): THIS
        # piece again on the processor. Decided by the model that failed, so
        # every chunk that hit the broken GPU model is retried, not just the
        # first one to notice.
        model = None
        self._gpu_failed_mid_use(failed, name, err)
        del failed                            # gone before the CPU model is built
        with self.use_model(name) as model:   # the same model, now on the processor
            return self._run_local_with(model, audio)

    def _run_local_with(self, model, audio):
        sr = cfg["sample_rate"]
        lang_setting = self._lang_setting()
        prompt = cfg.get("initial_prompt", "").strip() or None
        # Mixed languages: a short primer with a line in each mixed language,
        # in its own script, nudges Whisper to keep switching instead of
        # transliterating everything into the language it detected.
        mix = speech_langs.mix_languages(cfg) if lang_setting == "multi" else []
        base_prompt = speech_langs.whisper_primer(mix) or None
        if base_prompt:
            prompt = f"{base_prompt} {prompt}" if prompt else base_prompt

        # Serialize ALL inference on this model: faster-whisper/ctranslate2 is
        # not safe for concurrent transcribe() calls, and the chunked pipeline
        # fires several chunk threads at once. Without this, GPU/CUDA hangs (the
        # "works on the no-GPU laptop, stuck on the GPU PC" bug). The lock must
        # also span the generator consumption below - transcribe() is lazy and
        # does the real work while the segments are iterated.
        with self._infer_lock:
            if lang_setting not in ("auto", "multi"):
                lang_arg = lang_setting
            elif lang_setting == "auto" and self._session_lang is not None:
                lang_arg = self._session_lang
            else:
                sample = audio[:sr * 8]
                segs_detect, detect_info = model.transcribe(
                    sample, language=None, beam_size=1,
                    vad_filter=False, without_timestamps=True
                )
                list(segs_detect)
                lang_arg = detect_info.language
                if mix:
                    # Only the languages the user mixes can win - Armenian is
                    # otherwise easily heard as another language.
                    lang_arg = speech_langs.dominant_among(
                        getattr(detect_info, "all_language_probs", None), mix, lang_arg)
                if lang_setting == "auto":
                    self._session_lang = lang_arg

            # Anti-hallucination. Whisper invents filler ("Thank you.", "I'll
            # see you next time.") and loops it on silence/non-speech; the old
            # config disabled its safeguards. condition_on_previous_text=False
            # breaks the repetition loop, and the restored confidence + no-speech
            # thresholds make it drop silence instead of captioning it. VAD is
            # enabled for meetings (long silent stretches) but left OFF for
            # push-to-talk dictation, where the user is deliberately speaking and
            # VAD could clip a short utterance.
            is_meeting = getattr(self, "_capture_mode", None) is not None

            def _decode(initial_prompt):
                segs, _info = model.transcribe(
                    audio,
                    language=lang_arg,
                    initial_prompt=initial_prompt,
                    beam_size=4,
                    vad_filter=is_meeting,
                    vad_parameters=dict(min_silence_duration_ms=500),
                    condition_on_previous_text=False,
                    compression_ratio_threshold=2.4,
                    log_prob_threshold=-1.0,
                    no_speech_threshold=0.6,
                    repetition_penalty=1.3,
                    no_repeat_ngram_size=5,
                    temperature=[0.0, 0.2, 0.4, 0.6, 0.8],
                )
                return " ".join(s.text for s in segs)

            text = _decode(prompt)
            # Vocabulary evidence check: on short or unclear audio the glossary
            # prompt makes Whisper WRITE the terms in. When a term shows up,
            # decode again without the glossary (the mixed-language primer
            # stays); if that shows no trace of the term, the prompt invented
            # it - use the honest decode (with near-miss spellings snapped to
            # the terms). Costs a second pass only for pieces with a term.
            terms = vocabulary.load_terms(cfg)
            if prompt and prompt != base_prompt and terms and vocabulary.evidence_trigger(text, terms):
                plain = _decode(base_prompt)
                invented = vocabulary.unsupported_terms(text, plain, terms)
                if invented:
                    logger.info("[vocab] glossary insertion dropped (%d term(s))", len(invented))
                    text = vocabulary.correct_spellings(plain, terms)
            if mix:
                try:
                    quiet = float(np.abs(np.asarray(audio)).max()) < 0.02
                except Exception:
                    quiet = False
                if speech_langs.is_primer_echo(text, mix, quiet=quiet):
                    text = ""                 # only the primer, read back into silence
        return text, lang_arg

    def _run_google(self, audio):
        # Uses the Google AI Studio (Gemini) API, which accepts a plain API key.
        # (Google Cloud Speech-to-Text rejects API keys and needs OAuth/service
        # accounts, so the key people copy from AI Studio could never work there.)
        try:
            import requests
            key = (cfg.get("google_api_key") or "").strip()
            if not key:
                return "", "!google:Add your Google AI Studio (Gemini) API key in Settings."

            wav_bytes = self._float_to_wav(audio)  # full WAV; Gemini reads the header
            b64_data = base64.b64encode(wav_bytes).decode("utf-8")

            lang_setting = self._lang_setting()
            # Native script for the spoken language(s) - and words from another
            # language kept as spoken, not translated: an Armenian sentence
            # with English words in it stays exactly that. With Mixed
            # languages it's told which ones it will hear, switching anywhere.
            lang_hint = speech_langs.cloud_hint(lang_setting, speech_langs.mix_languages(cfg))

            # The user's custom vocabulary used to be dropped on every cloud
            # backend - configure it, upgrade to cloud, and it silently stopped
            # working. Gemini is prompt-driven, so the terms go straight in.
            vocab_hint = vocabulary.cloud_transcription_hint(cfg)

            model_name = cfg.get("google_stt_model", "gemini-2.5-flash")
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model_name}:generateContent?key={key}")
            payload = {
                "contents": [{
                    "parts": [
                        {"text": ("Transcribe this audio verbatim. Output only the exact "
                                  "spoken words with correct punctuation and capitalization, "
                                  "and nothing else." + lang_hint + vocab_hint)},
                        {"inline_data": {"mime_type": "audio/wav", "data": b64_data}},
                    ]
                }],
                "generationConfig": {"temperature": 0},
            }
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code != 200:
                reason = cloud_error_message("Google", resp.status_code, resp.text)
                return self._fallback_to_local_or_error(audio, "google", reason)

            data = resp.json()
            cands = data.get("candidates", [])
            if not cands:
                return "", "en"
            parts = cands[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            return text, (lang_setting if lang_setting != "auto" else "en")
        except Exception as e:
            return self._fallback_to_local_or_error(
                audio, "google", f"Google request failed: {e}")

    def _run_managed(self, audio):
        """Pro managed cloud transcription. Sends audio + the user's access token
        to our server proxy, which holds the real STT key. The key never touches
        the client, so this can't be used without a valid Pro/trial account."""
        try:
            import requests
            token = None
            try:
                if callable(self.get_auth_token):
                    token = self.get_auth_token()
            except Exception:
                token = None
            if not token:
                # Offline (the access token can't be refreshed) or signed out:
                # the local model keeps the dictation instead of losing it.
                return self._fallback_to_local_or_error(
                    audio, "managed", "Pro cloud unavailable - sign in or check your connection.")

            wav_bytes = self._float_to_wav(audio)  # full WAV; the proxy sends it to Gemini
            b64_data = base64.b64encode(wav_bytes).decode("utf-8")

            # Pro users pick which managed provider to use (server keys): Gemini
            # (default) or Mistral. No BYO key is involved here.
            provider = cfg.get("managed_provider", "gemini")
            # Custom vocabulary for the proxy to fold into its prompt. Older
            # proxy deployments ignore unknown fields, so this is a no-op until
            # the server side ships - never an error.
            lang_setting = self._lang_setting()
            body = {
                "audio": b64_data,
                "sample_rate": cfg["sample_rate"],
                "language": lang_setting,
                "provider": provider,
                # The languages to expect: the mix for Mixed languages, else the
                # one set (empty for auto). Its presence also tells the proxy to
                # keep foreign words as spoken; older proxies ignore it.
                "languages": (speech_langs.mix_languages(cfg) if lang_setting == "multi"
                              else [lang_setting] if lang_setting in speech_langs.NAMES
                              else []),
            }
            vocab_terms = vocabulary.cloud_terms(cfg)
            if vocab_terms:
                body["vocabulary"] = vocab_terms
            resp = requests.post(
                MANAGED_PROXY_URL,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code == 429:
                # The cloud declined (its quiet safety cap) - transparently fall
                # back to the local model so the user keeps working. Flag it so
                # the UI can say so once.
                self._cloud_capped = True
                return self._fallback_to_local_or_error(
                    audio, "managed", "Cloud transcription wasn't available.")
            if resp.status_code == 403:
                # Not entitled to managed cloud - fall back to local.
                return self._fallback_to_local_or_error(
                    audio, "managed", "Managed cloud access was denied.")
            if resp.status_code == 401:
                return "", "!managed:Session expired - sign in again."
            if resp.status_code != 200:
                # Any cloud error: don't fail the dictation, use local instead.
                reason = cloud_error_message("Managed cloud", resp.status_code, resp.text)
                return self._fallback_to_local_or_error(audio, "managed", reason)
            data = resp.json()
            return data.get("text", ""), data.get("lang", "en")
        except Exception as e:
            # Network/other failure - fall back to local rather than erroring.
            return self._fallback_to_local_or_error(
                audio, "managed", f"Managed cloud request failed: {e}")

    def _run_mistral(self, audio):
        try:
            import requests
            import io
            
            api_key = cfg.get("mistral_api_key", "")
            if not api_key:
                return "", "!mistral:Add your Mistral API key in Settings."

            model = normalize_mistral_model(cfg.get("mistral_stt_model", MISTRAL_STT_DEFAULT))
            lang_setting = self._lang_setting()

            wav_bytes = self._float_to_wav(audio)
            wav_io = io.BytesIO(wav_bytes)

            url = "https://api.mistral.ai/v1/audio/transcriptions"
            headers = {"Authorization": f"Bearer {api_key}"}
            files = {"file": ("audio.wav", wav_io, "audio/wav")}
            data = {"model": model}
            # Only send a language Voxtral actually supports; "auto"/"multi" and
            # unsupported codes (e.g. Armenian) are omitted so it auto-detects
            # instead of 400-ing.
            if lang_setting in MISTRAL_LANGS:
                data["language"] = lang_setting

            resp = requests.post(url, headers=headers, files=files, data=data, timeout=20)
            if resp.status_code != 200:
                reason = cloud_error_message("Mistral", resp.status_code, resp.text)
                return self._fallback_to_local_or_error(audio, "mistral", reason)

            resp_data = resp.json()
            text = resp_data.get("text", "").strip()
            detected_lang = resp_data.get("language", lang_setting if lang_setting in MISTRAL_LANGS else "en")

            return text, detected_lang
        except Exception as e:
            return self._fallback_to_local_or_error(
                audio, "mistral", f"Mistral request failed: {e}")

    def _float_to_wav(self, audio_float):
        int_data = (audio_float * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(cfg["sample_rate"])
            wf.writeframes(int_data.tobytes())
        return buf.getvalue()

    def shutdown(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass
        if hasattr(self, "mic_stream") and self.mic_stream is not None:
            try:
                self.mic_stream.stop_stream()
                self.mic_stream.close()
            except Exception:
                pass
        self.audio.terminate()

# ── Dynamic Pillow Tray Icon Generator ───────────────────────────────────────
def make_icon(color="#3b82f6"):
    # Create the transparent base image
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    
    # Create a temporary image for the Armenian flag background
    bg = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    
    # Armenian flag colors
    red = (217, 30, 24, 255)     # #d91e18
    blue = (0, 51, 160, 255)     # #0033a0
    orange = (242, 168, 21, 255) # #f2a815
    
    # Draw three horizontal stripes
    bg_draw.rectangle([0, 0, 64, 22], fill=red)
    bg_draw.rectangle([0, 22, 64, 42], fill=blue)
    bg_draw.rectangle([0, 42, 64, 64], fill=orange)
    
    # Create a circular mask
    mask = Image.new("L", (64, 64), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse([2, 2, 62, 62], fill=255)
    
    # Composite the flag background onto the main image using the circular mask
    img.paste(bg, (0, 0), mask)
    
    # Draw the white microphone outline/elements on top
    d = ImageDraw.Draw(img)
    d.rectangle([24, 12, 40, 40], fill="white")
    d.ellipse([18, 32, 46, 52],   fill="white")
    d.rectangle([30, 50, 34, 60], fill="white")
    d.rectangle([22, 58, 42, 62], fill="white")
    return img

def make_qicon(color="#3b82f6"):
    img = make_icon(color)
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.size[0], img.size[1], QImage.Format_RGBA8888)
    return QIcon(QPixmap.fromImage(qimg))


class GusanApplication(QApplication):
    """The app's QApplication. Every Quit Qt itself starts - macOS Cmd+Q, the
    app menu's or the Dock's Quit, a log-out, and qApp.quit() - arrives here
    as QEvent.Quit first; ``quit_filter`` (AppController._on_quit_event) says
    whether Qt may quit now (False swallows it - quit_app runs instead).
    Overriding event() costs only the app object's own events, unlike an
    application-wide event filter that would see every event of every widget."""

    def __init__(self, argv):
        super().__init__(argv)
        self.quit_filter = None

    def event(self, e):
        try:
            is_quit = e.type() == QEvent.Quit
        except Exception:
            is_quit = False
        if is_quit:
            allow = True
            flt = getattr(self, "quit_filter", None)
            if flt is not None:
                try:
                    allow = bool(flt())
                except Exception:
                    logger.warning("Quit handler failed - quitting anyway", exc_info=True)
            if not allow:
                e.ignore()
                return True
        return QApplication.event(self, e)


# Meeting notes a Quit left for later are written this long after startup
# (the speech model preload and the update check go first).
PENDING_NOTES_DELAY_MS = 20_000
PENDING_NOTES_RETRY_MS = 60_000
# A meeting started right after launch may run for hours: keep waiting for it
# (one check a minute) rather than leave the notes for the launch after.
PENDING_NOTES_MAX_TRIES = 240


# ── Main PySide6 Application Controller ───────────────────────────────────────
class AppController(QObject):
    # Cross-thread Signals - emitted from background hotkey-listener threads
    # (keyboard lib / pynput) and delivered on the Qt main thread via
    # QueuedConnection. Avoids QTimer.singleShot from non-Qt threads, which
    # silently no-ops because those threads have no Qt event loop.
    sig_hotkey = Signal()
    sig_assist_hotkey = Signal()
    sig_smart_hotkey = Signal()         # the Smart Actions shortcut (one dictation as a Smart Action)
    sig_enter = Signal()
    sig_escape = Signal()
    sig_update_available = Signal(str)  # tag of newer version
    sig_auth_changed = Signal()         # auth/entitlement state changed (from worker threads)
    sig_ipc_action = Signal(str)        # a second launch handed us an action (socket thread)
    sig_quit = Signal()                 # quit from a worker thread (after launching the installer)
    sig_gpu_progress = Signal(int, float, float)  # GPU download: percent, done bytes, total bytes
    sig_gpu_done = Signal(str, str)     # GPU download: state ("ok"/"gpu_failed"/"failed"/"cancelled"), message
    sig_gpu_wait = Signal(str, str, float)  # a local transcription made the user wait: context, model, seconds
    sig_gpu_runtime_failed = Signal(str)  # the GPU failed while transcribing (worker thread)
    sig_setting_changed = Signal(str)   # set_setting changed this config key (GUI thread)
    # A dictation, meeting or transcript was saved, or history was cleared
    # (GUI thread) - Home, Dictation, Smart Actions refresh their lists on it.
    sig_history_changed = Signal()
    # Dictation started (True) / stopped recording (False) - GUI thread.
    sig_recording_changed = Signal(bool)
    _sig_update_checked = Signal(str, str)  # the startup update check (worker) -> GUI thread

    # How long a stopped dictation waits for its speech model's download.
    SPEECH_MODEL_WAIT_S = 30 * 60

    # ── dictation state ──
    @property
    def is_rec(self):
        """True while a dictation records. Setting it announces the change
        (sig_recording_changed, on the GUI thread) - any thread may set it."""
        return getattr(self, "_is_rec", False)

    @is_rec.setter
    def is_rec(self, value):
        value = bool(value)
        self._is_rec = value
        if value != getattr(self, "_rec_announced", False):
            self._run_on_gui(self._announce_recording)

    def _announce_recording(self):
        # GUI thread: announce the state as it is NOW, so a worker's change
        # delivered late can never leave listeners with a stale value.
        now = getattr(self, "_is_rec", False)
        if now != getattr(self, "_rec_announced", False):
            self._rec_announced = now
            self.sig_recording_changed.emit(now)

    def _run_on_gui(self, fn, *args):
        """fn(*args) on the GUI thread: now when already there, else queued
        (through the dictation bar's call_soon, which every worker uses)."""
        if threading.current_thread() is threading.main_thread():
            fn(*args)
            return
        ov = getattr(self, "overlay", None)
        if ov is not None:
            ov.call_soon(fn, *args)

    def notify_history_changed(self):
        """Any thread: something was saved to (or cleared from) history -
        sig_history_changed on the GUI thread."""
        self._run_on_gui(self.sig_history_changed.emit)

    def __init__(self, qapp):
        super().__init__()
        self.qapp = qapp
        self.cfg = cfg

        # output_action is 1.9.x's dictation mode (nothing in 2.0 reads it:
        # Alt+R always pastes the exact words, Smart Actions have their own
        # shortcut). Still reset at launch so a 1.9.x copy reading this file
        # starts in plain dictation too.
        try:
            if actions.normalize_action_mode(self.cfg.get("output_action")) != actions.ACTION_TRANSCRIBE_ONLY:
                self.cfg["output_action"] = actions.ACTION_TRANSCRIBE_ONLY
                save_config(self.cfg)  # module-level: UI/tray don't exist yet
        except Exception:
            logger.debug("could not reset output_action", exc_info=True)

        # Every meeting records Both sides of the call unless its start card
        # picks another for that one meeting (never saved in 2.0). A 1.x pick
        # saved here goes back to the default; meeting_session heals it on a
        # system that can't capture the call's sound.
        try:
            if self.cfg.get("meeting_audio_mode") != DEFAULT["meeting_audio_mode"]:
                self.cfg["meeting_audio_mode"] = DEFAULT["meeting_audio_mode"]
                save_config(self.cfg)
        except Exception:
            logger.debug("could not reset meeting_audio_mode", exc_info=True)

        # Load stylesheet. Resolve the checkbox tick asset to an absolute path
        # so it renders regardless of the process working directory (packaged
        # builds and autostart can launch from an arbitrary CWD).
        from ui.styles import STYLESHEET
        _check_svg = resource_path("assets", "check.svg").replace("\\", "/")
        _chevron_svg = resource_path("assets", "chevron.svg").replace("\\", "/")
        self.style_content = (
            STYLESHEET
            .replace('url("assets/check.svg")', f'url("{_check_svg}")')
            .replace('url("assets/chevron.svg")', f'url("{_chevron_svg}")')
        )

        # Setup pure business Audio Recorder
        self.recorder = AudioRecorder()
        self.recorder.on_levels = self._on_levels
        self.recorder.on_lang_detected = self._on_lang
        self.recorder.on_partial = self._on_partial
        from pynput.keyboard import Controller
        self.kbd = Controller()
        self.is_rec = False
        self._mouse_listener = None
        self._kbd_listener = None
        self._registered_kbd_hotkey = None
        self._registered_assist_hotkey = None
        self._assist_listener = None
        # The Smart Actions shortcut (smart_actions_hotkey): its hook, and the
        # action the take it started runs (_start / _stop_impl).
        self._registered_smart_hotkey = None
        self._smart_listener = None
        self._smart_next = False
        self._oneshot_action = None
        # A Smart take transcribed with its own speech model: load Dictation's
        # again once it is done (_restore_dictation_speech).
        self._smart_speech_restore = False
        self._quitting = False
        self.live_assist = None          # created on first use
        self.main_win = None             # the Gusan window (ui/shell.py), built on first open
        self._transient_kbd_handles = []

        # Auth + Pro entitlement (Supabase). All network calls run on worker
        # threads; state changes are marshaled to the GUI thread via the
        # sig_auth_changed signal so tray/UI updates stay thread-safe.
        self.auth = auth.AuthManager(on_state_changed=self.sig_auth_changed.emit)
        # Let the recorder fetch a fresh access token for managed cloud transcription.
        self.recorder.get_auth_token = lambda: self.auth.get_access_token()
        # Every analytics event carries the account tier (never the account).
        telemetry.set_context_provider(self._analytics_context)
        self._tracked_settings = self._settings_snapshot()

        # ── Gusan 2.0 services (GUI thread, before any window or page) ──
        self._file_job_running = False
        self._model_wait = None              # {"name", "pct"} while a dictation waits on its model
        self._update_notice_shown = None     # the release a notice was shown for this run
        self._update_ready = None            # (tag, installer path) for "Install updates when I quit"
        self._update_fetching = None
        self._update_installing = False
        self._hotkeys_suspended = False
        # One download manager for speech and local AI models: Settings,
        # onboarding, Audio to text and dictation all share its downloads.
        from model_manager import ModelDownloads
        self.downloads = ModelDownloads(self)
        self.recorder.model_fetcher = self._fetch_speech_model
        # The meeting recorder: the Meetings page, Live Assistant and the
        # calendar reminder all drive this one session.
        from meeting_session import MeetingSession
        self.meeting_session = MeetingSession(self)
        self.meeting_session.finished.connect(self._on_meeting_finished)
        # Audio to text's one job queue; it owns _file_job_running. A missing
        # model comes through self.downloads (one download, shared progress).
        import file_jobs
        self.file_jobs = file_jobs.FileJobController(self, downloader=self.downloads.fetch)
        self.file_jobs.finished.connect(self._on_file_job_finished)
        # The mic check of Settings and onboarding (opens nothing until start()).
        try:
            from mic_monitor import MicMonitor
            self.mic_monitor = MicMonitor(pa=self.recorder.audio, parent=self)
        except Exception:
            logger.warning("Microphone check unavailable", exc_info=True)
            self.mic_monitor = None
        self.calendar = None                 # calendar_service.CalendarService when configured
        self._reminder = None                # the calendar reminder toast, once shown

        # The dictation bar is the only window built at startup: it is the
        # HUD and the workers' call_soon trampoline to the GUI thread. The
        # Gusan window (show_main) and every page are built on first open.
        from ui.overlay import Overlay
        self.overlay = Overlay(main_app=self)
        # The rich clipboard's GUI-thread bridge: a Smart Action's paste puts
        # HTML + nice plain text on the clipboard from the paste worker.
        try:
            from ui.components import clipboard as rich_clipboard
            rich_clipboard.install()
        except Exception:
            logger.warning("Rich clipboard unavailable", exc_info=True)
        # The 5-step welcome (ui/onboarding_v2), built only when a first run
        # opens it (show_onboarding) - an onboarded user never pays for it.
        self.onboarding_win = None

        # Wire Signals to main-thread handlers. QueuedConnection guarantees the
        # slot runs on this QObject's owning thread (the Qt main thread),
        # regardless of which thread emits the signal.
        self.sig_hotkey.connect(self._on_hotkey, Qt.QueuedConnection)
        self.sig_assist_hotkey.connect(self.toggle_live_assist, Qt.QueuedConnection)
        self._setup_assist_hotkey(self.cfg.get("live_assist_hotkey", ""))
        self.sig_smart_hotkey.connect(self._on_smart_hotkey, Qt.QueuedConnection)
        self._setup_smart_hotkey(self.cfg.get("smart_actions_hotkey", ""))
        self.sig_enter.connect(self._on_enter, Qt.QueuedConnection)
        self.sig_escape.connect(self._on_escape, Qt.QueuedConnection)
        self.sig_update_available.connect(self._prompt_update, Qt.QueuedConnection)
        self._sig_update_checked.connect(self._on_update_checked, Qt.QueuedConnection)
        self.sig_setting_changed.connect(self._on_own_setting_changed)
        self.sig_auth_changed.connect(self._on_auth_changed, Qt.QueuedConnection)
        # A QTimer started on a plain thread never fires: the updater hops here
        # - through quit_app, so services shut down like any other Quit.
        self.sig_quit.connect(self.quit_app, Qt.QueuedConnection)

        # Register the configured hotkey (keyboard combo or mouse button).
        self._setup_hotkey(self.cfg["hotkey"])
        # Load the speech model in the background now, so the first Alt+R
        # doesn't wait 1-2 s for it. (The idle sweep frees it again if it
        # isn't used for whisper_idle_unload_min.)
        QTimer.singleShot(2500, self._preload_speech_model)
        # Optional NVIDIA GPU acceleration (gpu_accel): the download runs on a
        # worker; its progress/result and the "you just waited" hints arrive here.
        self._gpu_thread = None
        self._gpu_phase = None                         # "download" / "verify" while working
        self._gpu_cancel = False
        self._gpu_removing = False                     # Settings' Remove is running
        self._tray_click = None
        self.sig_gpu_done.connect(self._on_gpu_done, Qt.QueuedConnection)
        self.sig_gpu_wait.connect(self._on_gpu_wait, Qt.QueuedConnection)
        self.sig_gpu_runtime_failed.connect(self._on_gpu_runtime_failed, Qt.QueuedConnection)
        self.recorder.on_cuda_failed = self.sig_gpu_runtime_failed.emit
        # Every minute: free a speech / local AI model that sits unused.
        self._idle_timer = QTimer(self)
        self._idle_timer.timeout.connect(self._idle_sweep)
        self._idle_timer.start(60_000)
        # Instant-apply settings (set_setting): a burst of changes is written
        # to disk once, 250 ms after the last one.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(250)
        self._save_timer.timeout.connect(self.flush_config_save)
        # Any way out saves a pending change first, and a meeting being
        # recorded (tray Quit already did both; this covers the rest, e.g. a
        # Windows log-off or shutdown, where Qt emits aboutToQuit and the
        # process ends right after).
        self.qapp.aboutToQuit.connect(self._on_about_to_quit)
        # Windows log-off / shutdown asks first (WM_QUERYENDSESSION): no
        # question can be shown then - save the meeting and settings quietly.
        try:
            self.qapp.commitDataRequest.connect(self._on_session_ending)
        except Exception:
            logger.debug("session-end signal unavailable", exc_info=True)
        # macOS Cmd+Q, the app menu's / Dock's Quit (and any qApp.quit()) go
        # through quit_app: it asks first and services shut down, instead of
        # Qt just hiding the windows (GusanApplication.event).
        if hasattr(self.qapp, "quit_filter"):
            self.qapp.quit_filter = self._on_quit_event
        # Meeting notes a Quit left for later: written once startup work is done.
        self._pending_notes_queue = []
        QTimer.singleShot(PENDING_NOTES_DELAY_MS, self._write_pending_notes)

        # Setup System Tray
        self._setup_tray()

        # Google Calendar (only in a build with a Google OAuth client).
        self._setup_calendar()
        # Which models are on disk - off the GUI thread, after startup work.
        QTimer.singleShot(1500, self.downloads.scan)

        # Restore any saved login + Pro entitlement in the background.
        threading.Thread(target=self._restore_session, daemon=True).start()

        # Check updates in background
        threading.Thread(target=self._background_check_updates, daemon=True).start()

        # The welcome wizard on the first launch only. Existing users (every
        # 1.9 upgrader) never see it, and get no automatic account gate either:
        # Sign in is one click away (sidebar, tray, Account & plan).
        if not self.cfg.get("onboarding_done", False):
            QTimer.singleShot(500, self.show_onboarding)

        # macOS needs explicit permission grants (mic / Accessibility / Input
        # Monitoring) or dictation looks silently broken. One-time guided setup.
        if sys.platform == "darwin" and not self.cfg.get("macos_perms_guide_shown", False):
            QTimer.singleShot(1200, self.show_macos_permissions_guide)

        # If a previous session already detected an update, say so at startup
        # - one quiet notice, not waiting for the network check (which still
        # runs and clears the flag if there is no update). Never for a release
        # the user said "Not now" to (updater.should_prompt).
        cached_pending = self.cfg.get("pending_update_version", "")
        if cached_pending and not self._is_newer(cached_pending):
            # Declined the popup, then installed that version another way.
            self.cfg["pending_update_version"] = cached_pending = ""
            self.save_config()
        if (cached_pending and self.cfg.get("onboarding_done", False)
                and updater.should_prompt(self.cfg, cached_pending, APP_VERSION)):
            QTimer.singleShot(1500, lambda: self.sig_update_available.emit(cached_pending))

    def _setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.update_tray_icon()
        self._build_tray_menu()
        self.tray_icon.show()

        # A click on the tray icon opens the app (_on_tray_activated).
        self.tray_icon.activated.connect(self._on_tray_activated)
        # A click on a notification runs the action it was shown with, if any.
        self.tray_icon.messageClicked.connect(self._on_tray_message_clicked)

    def _build_tray_menu(self):
        """The tray menu (ui/tray_menu.py): built once, then refreshed in place
        (plan, account rows, hotkeys, privacy, the calendar's Next row) - on
        auth and privacy changes, and by the menu itself when it opens."""
        tm = getattr(self, "tray_menu", None)
        try:
            if tm is None:
                from ui.tray_menu import build_tray_menu
                tm = build_tray_menu(self, QMenu())
                self.tray_menu = tm
                self.tray_icon.setContextMenu(tm.menu)
                self.sig_setting_changed.connect(tm.on_setting_changed)
                if getattr(self, "calendar", None) is not None:
                    tm.attach_calendar(self.calendar)
            else:
                tm.refresh()
            self.tray_icon.setToolTip(tm.tooltip())
        except Exception:
            logger.warning("Tray menu failed - using a plain one", exc_info=True)
            if getattr(self, "tray_menu", None) is None:
                self._plain_tray_menu()

    def _plain_tray_menu(self):
        """Open and Quit only: never leave the tray icon without a way out."""
        menu = QMenu()
        action_open = QAction(f"Open {APP_NAME}", menu)
        action_open.triggered.connect(lambda: self.show_main())
        menu.addAction(action_open)
        menu.setDefaultAction(action_open)
        action_quit = QAction(f"Quit {APP_NAME}", menu)
        action_quit.triggered.connect(self.quit_app)
        menu.addAction(action_quit)
        self._tray_menu = menu  # keep a reference so Qt doesn't GC it
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.setToolTip(APP_NAME)

    # ── Auth / Pro helpers ────────────────────────────────────────────────────
    def is_pro(self):
        # has_pro_access: a real Pro/trial entitlement is never downgraded by the
        # admin force-tier preview, so a genuine Pro user is never blocked/upsold.
        try:
            return entitlements.has_pro_access(self.auth, self.cfg)
        except Exception:
            return bool(getattr(self, "auth", None) and self.auth.is_pro)

    def current_tier(self):
        try:
            return entitlements.tier(self.auth, self.cfg)
        except Exception:
            return entitlements.TIER_GUEST

    # ── Usage analytics ──────────────────────────────────────────────────────
    # Settings whose changes are reported. Their values are engines, models,
    # languages and switches - never keys, paths or text. The Smart Actions
    # shortcut is reported as set or not (True/False), never its keys.
    _TRACKED_SETTINGS = (
        "backend", "whisper_model", "action_model", "language",
        "translate_target", "meeting_audio_mode", "save_history",
        "cleanup_enabled", "restore_clipboard",
        "smart_actions_hotkey", "smart_actions_speech_model",
        "notion_auto_send",
    )
    _TRACKED_AS_SET = ("smart_actions_hotkey",)

    def track(self, event, props=None):
        """Record an anonymous usage event (a no-op when analytics is off)."""
        telemetry.track(event, props, self.cfg, APP_VERSION)

    def _analytics_context(self):
        # The REAL server entitlement, not the admin force-tier preview, and
        # "admin" for the founder so their own usage can be filtered out.
        a = getattr(self, "auth", None)
        if a is None:
            return {}
        if getattr(a, "is_admin", False):
            tier = "admin"
        elif getattr(a, "is_pro", False):
            tier = "pro"
        elif getattr(a, "is_authenticated", False):
            # Signed in, but the server hasn't said Pro or not yet (the first
            # second after launch): don't guess "free".
            tier = "free" if getattr(a, "entitlement_known", True) else "unknown"
        else:
            tier = "guest"
        return {"tier": tier}

    def _restore_session(self):
        """Restore the saved login, then report the start - afterwards, so the
        event carries the user's real tier rather than 'guest'."""
        try:
            self.auth.load_session()
        except Exception:
            logger.debug("session restore failed", exc_info=True)
        self.track("app_started", {
            "privacy_mode": self.cfg.get("privacy_mode", False),
            "backend": self.cfg.get("backend", "local"),
            "smart_actions_hotkey": bool(self.cfg.get("smart_actions_hotkey")),
            "smart_actions_speech_model": self.cfg.get("smart_actions_speech_model") or "",
        })

    def _settings_snapshot(self):
        return {k: (bool(self.cfg.get(k)) if k in self._TRACKED_AS_SET else self.cfg.get(k))
                for k in self._TRACKED_SETTINGS}

    def _track_setting_changes(self):
        prev = getattr(self, "_tracked_settings", None)
        now = self._settings_snapshot()
        self._tracked_settings = now
        if prev is None:
            return
        changed = [k for k in self._TRACKED_SETTINGS if now[k] != prev.get(k)]
        if not changed:
            return
        if "backend" in changed:
            self.track("backend_selected", {"backend": now["backend"]})
        props = {"changed": ",".join(changed)}
        props.update({k: now[k] for k in changed})
        self.track("settings_saved", props)

    def _user_secret_id(self):
        return entitlements.user_secret_id(getattr(self, "auth", None))

    def _reconcile_user_secrets(self):
        """Keep BYO API keys + cloud-engine config scoped per user. Returns True if
        the active keys were swapped for a different user (so callers refresh UI)."""
        a = getattr(self, "auth", None)
        if a is not None and a.is_authenticated and not getattr(a, "user_id", None):
            # Signed in but offline at launch: the account isn't known yet, and
            # swapping to the guest bucket would hide this user's keys and Pro
            # engine for the whole session. Wait for the session to load.
            return False
        current = self._user_secret_id()
        if self.cfg.get("secrets_owner") == current:
            return False  # same user (or same guest session) - nothing to do
        switched = entitlements.reconcile_user_secrets(self.cfg, current)
        self.save_config()
        return switched

    def _reconcile_notion_owner(self):
        """A Notion connection belongs to the Gusan account that made it (the
        calendar's rule): when another account signs in on this computer, the
        connection is forgotten here - its meetings never land in someone
        else's workspace, and the new account never sees that workspace's
        pages. One made as a guest (notion_owner "") is adopted by the account
        that signs in. Signed out, offline at launch (no user_id yet) and not
        connected: nothing happens."""
        owner = str(getattr(getattr(self, "auth", None), "user_id", None) or "")
        if not owner or not self.cfg.get("notion_api_key"):
            return
        stamp = str(self.cfg.get("notion_owner") or "")
        if stamp == owner:
            return
        if not stamp:
            self.cfg["notion_owner"] = owner
            self.save_config()
            return
        import notion_connect
        import notion_export
        mode = notion_export.connection_mode(self.cfg)
        newest = notion_connect.latest_tokens()
        token = (newest.get("access_token") or self.cfg.get("notion_api_key") or "").strip()
        refresh = (newest.get("refresh_token") or self.cfg.get("notion_refresh_token") or "").strip()
        client_id = (self.cfg.get("notion_mcp_client_id") or "").strip()
        self.cfg["notion_owner"] = ""
        self.set_settings(dict(notion_export.BLANK_SETTINGS))      # also drops the token from the keyring
        notion_connect.forget_tokens()
        if mode == "oauth" and token:
            # Give the other account's grant back at Notion (best effort, a worker: it goes online).
            threading.Thread(target=notion_connect.revoke, args=(token,), daemon=True,
                             name="gusan-notion-revoke").start()
        elif mode == "mcp" and token:
            threading.Thread(target=notion_connect.revoke_mcp, args=(token, refresh, client_id), daemon=True,
                             name="gusan-notion-revoke").start()

    def set_admin_tier_override(self, value):
        """Super-admin only: force a tier locally (auto|guest|free|pro)."""
        self.cfg["admin_tier_override"] = value
        self.save_config()
        self.sig_auth_changed.emit()

    def _track_pro_activation(self, now_pro, known):
        """Fire pro_activated once, when an account last seen as free is now Pro.

        The last real answer is kept per account in config, so the upgrade
        counts whether it shows up mid-session (the checkout watcher) or on the
        next launch. An account never seen before is recorded silently -
        otherwise every existing Pro user would "activate" on every launch."""
        uid = getattr(self.auth, "user_id", None)
        if not (known and uid):
            return
        seen = self.cfg.get("last_known_pro")
        if not isinstance(seen, dict):
            seen = {}
        prev = seen.get(uid)
        if now_pro and prev is False:
            self.track("pro_activated", {"plan": getattr(self.auth, "plan", "") or ""})
        if prev is not now_pro:
            seen[uid] = now_pro
            self.cfg["last_known_pro"] = seen
            self.save_config()

    def watch_for_upgrade(self, minutes=10, every=30):
        """After the Stripe checkout opens, re-check the entitlement for a while
        so a purchase unlocks Pro (and is counted) without restarting the app."""
        if getattr(self, "_upgrade_watch", None) is not None:
            return
        stop = threading.Event()
        self._upgrade_watch = stop

        def _loop():
            try:
                deadline = time.monotonic() + minutes * 60
                while time.monotonic() < deadline and not stop.wait(every):
                    if not self.auth.is_authenticated:
                        return
                    self.auth.refresh_entitlement()
                    if getattr(self.auth, "is_pro", False):
                        return
            except Exception:
                logger.debug("upgrade watch failed", exc_info=True)
            finally:
                self._upgrade_watch = None

        threading.Thread(target=_loop, name="upgrade-watch", daemon=True).start()

    def _on_auth_changed(self):
        # Runs on the GUI thread (QueuedConnection). Refresh tray + any open UI.
        # First: swap in/out the per-user API keys if the signed-in user changed,
        # so we never expose the previous account's keys on a shared computer.
        try:
            self._reconcile_user_secrets()
        except Exception:
            logger.debug("user secret reconcile failed", exc_info=True)
        # Track the moment a user becomes Pro (trial or paid) for the funnel.
        # Use the REAL server entitlement here (not is_pro(), which honors the
        # admin force-tier preview) so toggling the preview never fires the
        # activation funnel or wipes the managed-engine choice during a preview.
        now_pro = bool(getattr(self.auth, "is_pro", False))
        known = bool(getattr(self.auth, "entitlement_known", False))
        self._track_pro_activation(now_pro, known)
        # If Pro lapsed while a keyless managed engine/backend was selected, fall
        # back to local so the UI and behavior stay consistent (managed = Pro).
        # Only on a real server answer: offline or while Supabase is slow the
        # entitlement is merely unknown, and wiping the choice then lost a Pro
        # user's engine for good (the managed path already falls back per
        # request when the server says no).
        if not now_pro and known:
            _changed = False
            if self.cfg.get("backend") == "managed":
                self.cfg["backend"] = "local"
                _changed = True
            if self.cfg.get("action_model") == actions.API_MANAGED_ID:
                self.cfg["action_model"] = actions.RULE_BASED_ID
                _changed = True
            if self.cfg.get("meeting_notes_model") == actions.API_MANAGED_ID:
                # Meetings pick their own engine again (a downloaded local one).
                self.cfg["meeting_notes_model"] = ""
                _changed = True
            if self.cfg.get("smart_actions_speech_model") == "cloud":
                # Smart Actions transcribe like Dictation again.
                self.cfg["smart_actions_speech_model"] = ""
                _changed = True
            if _changed:
                # An automatic fallback, not a user choice: keep it out of the
                # backend_selected / settings_saved analytics.
                self._tracked_settings = self._settings_snapshot()
                self.save_config()
        # Remember the signed-in email so the sign-in form can prefill it next time
        # (convenience only - the real session is restored from the encrypted
        # refresh token in the OS keyring, never from this).
        try:
            em = (getattr(self.auth, "user_email", None) or "").strip()
            if em and self.cfg.get("last_signin_email") != em:
                self.cfg["last_signin_email"] = em
                known = list(self.cfg.get("known_emails") or [])
                if em not in known:
                    known.insert(0, em)
                self.cfg["known_emails"] = known[:5]
                self.save_config()
        except Exception:
            logger.debug("could not remember email", exc_info=True)
        # A Google Calendar connected by another Gusan account is disconnected;
        # one connected as a guest is adopted by the account that signs in.
        cal = getattr(self, "calendar", None)
        if cal is not None:
            try:
                cal.set_owner(getattr(self.auth, "user_id", None))
            except Exception:
                logger.debug("calendar owner check failed", exc_info=True)
        # Same rule for Notion: another account's connection is forgotten here.
        try:
            self._reconcile_notion_owner()
        except Exception:
            logger.debug("Notion owner check failed", exc_info=True)
        try:
            self._build_tray_menu()
        except Exception:
            logger.debug("tray rebuild failed", exc_info=True)
        # The Gusan window: sidebar plan badge + every built page (Settings
        # re-reads the API keys here, so an account switch shows the new
        # account's keys).
        mw = getattr(self, "main_win", None)
        if mw is not None:
            try:
                mw.refresh_account()
            except Exception:
                logger.debug("main window account refresh failed", exc_info=True)

    def start_google_login(self):
        def _run():
            ok = False
            try:
                ok = self.auth.sign_in_with_google()
            except Exception:
                logger.debug("google login failed", exc_info=True)
            if ok:
                self.track("login_succeeded", {"method": "google"})
            else:
                self.track("login_failed", {
                    "method": "google",
                    "reason": getattr(self.auth, "last_error", "") or "unknown",
                })
        threading.Thread(target=_run, daemon=True).start()
        self.show_tray_hint("Sign in", "Opening your browser to sign in with Google…")

    def sign_out(self):
        self.track("signed_out")
        threading.Thread(target=self.auth.sign_out, daemon=True).start()

    def open_billing(self):
        """Settings → Account → Manage subscription (and the tray): the Stripe
        Customer Portal, already signed in - cancel (a trial is then never
        charged), change the card, download invoices."""
        if not self.auth.is_authenticated:
            self.show_auth_gate()
            return
        if getattr(self, "_billing_busy", False):
            return                              # a click is already on its way
        self._billing_busy = True
        threading.Thread(target=self._open_billing_worker, daemon=True).start()

    def _open_billing_worker(self):
        try:
            url, problem = billing_portal_session(self.auth.get_access_token())
            if not url and problem != "signed_out" and STRIPE_PORTAL_URL:
                url = STRIPE_PORTAL_URL         # Stripe's email-code login instead
            if url:
                webbrowser.open(url)
            else:
                self.overlay.call_soon(self._billing_problem, problem)
        finally:
            self._billing_busy = False

    def _billing_problem(self, problem):
        if problem == "signed_out":
            text = "Your session expired. Sign in again, then open Manage subscription."
        elif problem == "no_customer":
            who = self.auth.user_email or "this account"
            text = (f"There's no Stripe subscription linked to {who}.\n\n"
                    "If you subscribed with a different email, sign in with that one - "
                    f"or write to {SUPPORT_EMAIL} and we'll sort it out.")
        else:
            text = ("Couldn't open your subscription page right now. Check your connection "
                    f"and try again, or write to {SUPPORT_EMAIL}.")
        QMessageBox.information(self._dialog_parent(), "Manage subscription", text)

    def set_privacy_mode(self, on, notify=True):
        """Apply Privacy Mode globally and immediately. Privacy forces everything
        local: no cloud/managed backend, no history. Called from the tray and from
        the in-app footer checkbox so the two stay in lockstep."""
        on = bool(on)
        if on == bool(self.cfg.get("privacy_mode", False)):
            return  # no change
        self.cfg["privacy_mode"] = on
        if on:
            self.cfg["backend"] = "local"   # privacy = on-device only
        self.save_config()
        self.track("privacy_mode_enabled" if on else "privacy_mode_disabled")
        if notify:
            self.show_tray_hint(
                "Privacy Mode ON" if on else "Privacy Mode OFF",
                "Everything stays on your device - cloud features are disabled."
                if on else "Cloud features are available again.",
            )
        # Refresh any open Settings window + rebuild the tray checkmark.
        self.sig_auth_changed.emit()
        try:
            self._build_tray_menu()
        except Exception:
            pass

    def toggle_privacy_mode(self):
        """Flip Privacy Mode from the tray - through set_setting, so the pages
        and the calendar hear it (sig_setting_changed) like any other change."""
        on = not self.cfg.get("privacy_mode", False)
        if self.set_setting("privacy_mode", on):
            self.show_tray_hint(
                "Privacy Mode ON" if on else "Privacy Mode OFF",
                "Everything stays on your device - cloud features are disabled."
                if on else "Cloud features are available again.")

    def _dialog_parent(self):
        """The Gusan window while it is on screen (a dialog then opens over it,
        never behind it), else None."""
        mw = getattr(self, "main_win", None)
        try:
            if mw is not None and mw.isVisible() and not mw.isMinimized():
                return mw
        except RuntimeError:
            pass
        return None

    def _pro_upsell(self, feature=None):
        """The Pro window. ``feature`` names what was tapped ("Meeting
        recording", "Live Assistance", "Smart Actions", "Fast cloud
        transcription"); paywall_viewed keeps the 1.9 names (PRO_FEATURE_EVENT),
        the window shows today's (PRO_FEATURE_SHOWN)."""
        tracked = PRO_FEATURE_EVENT.get(feature, feature)
        shown = PRO_FEATURE_SHOWN.get(feature, feature)
        try:
            telemetry.track("paywall_viewed", {"feature": tracked or ""}, self.cfg, APP_VERSION)
        except Exception:
            pass
        parent = self._dialog_parent()
        try:
            from ui.pro_dialog import ProDialog
            dlg = ProDialog(main_app=self, feature=shown, parent=parent)
            dlg.exec()
            return
        except Exception as e:
            logger.warning("Pro dialog failed, using fallback: %s", e)

        # Fallback: simple prompt if the rich dialog can't be shown.
        box = QMessageBox(parent)
        box.setWindowTitle(f"{APP_NAME} Pro")
        feat = f"{shown} is a Pro feature.\n\n" if shown else ""
        box.setText(feat + "Upgrade to unlock Meetings, Smart Actions, and fast cloud transcription.")
        if not self.auth.is_authenticated:
            signin_btn = box.addButton("Create account", QMessageBox.AcceptRole)
        else:
            signin_btn = None
        monthly_btn = box.addButton("€7.99/mo", QMessageBox.ActionRole)
        annual_btn = box.addButton("€59/yr", QMessageBox.ActionRole)
        box.addButton("Maybe later", QMessageBox.RejectRole)
        if hasattr(self, "style_content"):
            box.setStyleSheet(self.style_content)
        box.exec()
        clicked = box.clickedButton()
        if signin_btn is not None and clicked == signin_btn:
            self.show_auth_gate()
        elif clicked == monthly_btn:
            webbrowser.open(self._checkout_url(PRO_MONTHLY_URL))
        elif clicked == annual_btn:
            webbrowser.open(self._checkout_url(PRO_ANNUAL_URL))

    def _checkout_url(self, base):
        """Append the signed-in user's id (+ email) to a Stripe payment link so
        the webhook can link the resulting subscription to the right account."""
        try:
            uid = getattr(self.auth, "user_id", None)
            if not uid:
                return base
            from urllib.parse import urlencode
            params = {"client_reference_id": uid}
            email = getattr(self.auth, "user_email", None)
            if email:
                params["prefilled_email"] = email
            sep = "&" if "?" in base else "?"
            return f"{base}{sep}{urlencode(params)}"
        except Exception:
            return base

    def _on_tray_activated(self, reason):
        # Any direct click on the tray icon opens the app - single, double,
        # or middle click. (Right-click shows the menu, whose first item is
        # also "Open Gusan".)
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick,
                      QSystemTrayIcon.MiddleClick):
            # First run with the welcome wizard on screen: bring IT forward,
            # never the app over it (closing it with X lets the app open).
            if self.front_first_run_wizard():
                return
            self.show_main()
            # A calendar reminder on screen never takes focus by itself: the
            # tray icon is the keyboard's way in (Win+B, Enter), so it gets
            # the focus after the window opens (Esc = Not this one).
            toast = getattr(self, "_reminder", None)
            try:
                if toast is not None and toast.isVisible():
                    QTimer.singleShot(0, toast.take_focus)
            except RuntimeError:
                pass

    def update_tray_icon(self):
        color = self.cfg.get("accent_color", "#3b82f6")
        self.tray_icon.setIcon(make_qicon(color))

    def show_tray_hint(self, title, message, on_click=None):
        # The click action belongs to THIS notification; a later plain one clears it.
        self._tray_click = on_click
        self._last_hint_at = time.monotonic()
        self.tray_icon.showMessage(title, message, QSystemTrayIcon.Information,
                                   8000 if on_click else 4000)

    def _on_tray_message_clicked(self):
        action, self._tray_click = getattr(self, "_tray_click", None), None
        if action:
            try:
                action()
            except Exception:
                logger.debug("Notification action failed", exc_info=True)

    # ── GPU acceleration (optional NVIDIA download - see gpu_accel.py) ──────
    def gpu_download_running(self):
        t = getattr(self, "_gpu_thread", None)
        return t is not None and t.is_alive()

    def start_gpu_download(self, source="settings"):
        """Download NVIDIA's cuBLAS and switch local Whisper to the GPU. GUI
        thread. Returns False if a download is already running."""
        if self.gpu_download_running():
            return False
        self._gpu_cancel = False
        self._gpu_phase = "download"
        self.cfg[gpu_accel.CFG_INTRO] = True
        self.cfg.pop(gpu_accel.CFG_FAILED, None)       # a fresh try
        self.save_config()
        self.track("gpu_download_started", {"source": source})

        def _run():
            try:
                gpu_accel.download(
                    on_progress=lambda p, d, t: self.sig_gpu_progress.emit(int(p), float(d), float(t)),
                    should_cancel=lambda: self._gpu_cancel)
            except Exception as e:
                cancelled = self._gpu_cancel
                if not isinstance(e, gpu_accel.GpuAccelError):
                    logger.warning("GPU download failed: %s", e, exc_info=True)
                self.sig_gpu_done.emit("cancelled" if cancelled else "failed", str(e))
                return
            self._gpu_phase = "verify"
            self.sig_gpu_progress.emit(-1, 0.0, 0.0)  # "checking your graphics card"
            state, msg = self._verify_gpu()
            self.sig_gpu_done.emit(state, msg)
        self._gpu_thread = threading.Thread(target=_run, daemon=True)
        self._gpu_thread.start()
        return True

    def retry_gpu(self):
        """"Try again" after the GPU couldn't run (e.g. the driver was updated
        since): no new download, just another attempt on the graphics card."""
        if self.gpu_download_running():
            return False
        if not self._can_verify_gpu():
            # Nothing to test it with right now: keep the recorded failure.
            self.show_tray_hint("GPU acceleration",
                                "Choose a downloaded local Whisper model in Settings › "
                                "Speech & AI first - the GPU is tested with it.")
            return False
        previous = self.cfg.pop(gpu_accel.CFG_FAILED, None)   # let it try the GPU
        self._gpu_phase = "verify"

        def _run():
            self.sig_gpu_progress.emit(-1, 0.0, 0.0)
            state, msg = self._verify_gpu()
            if state == "pending" and previous:
                state, msg = "gpu_failed", previous
            self.sig_gpu_done.emit(state, msg)
        self._gpu_thread = threading.Thread(target=_run, daemon=True)
        self._gpu_thread.start()
        return True

    def gpu_phase(self):
        return self._gpu_phase if self.gpu_download_running() else None

    def cancel_gpu_download(self):
        self._gpu_cancel = True

    def gpu_removable(self):
        """Gusan's own GPU download is on disk (not a CUDA Toolkit of this PC)
        and not already going away - Settings shows Remove then."""
        try:
            return (not getattr(self, "_gpu_removing", False) and not gpu_accel.removal_pending()
                    and gpu_accel.own_download_present())
        except Exception:
            return False

    def remove_gpu_download(self):
        """Settings' Remove: delete the downloaded GPU libraries, so local speech
        goes back to the processor (1.x Settings did this). GUI thread; the work
        runs on a worker, then the hardware is probed again so the card shows
        Download. False when there's nothing of ours to remove or a download
        is running."""
        if self.gpu_download_running() or not self.gpu_removable():
            return False
        self._gpu_removing = True
        self.cfg.pop(gpu_accel.CFG_FAILED, None)
        # Removing it is a "no" for now: no GPU reminder for the next few days.
        self.cfg[gpu_accel.CFG_INTRO] = True
        self.cfg[gpu_accel.CFG_LAST] = time.time()
        self.save_config()
        self.track("gpu_removed")
        rec = getattr(self, "recorder", None)

        def _run():
            now = False
            try:
                # Off the GUI thread: unloading waits for a model load in progress.
                if rec is not None:
                    rec._cuda_usable = None      # re-decided at the next load
                    rec.unload_model()
                now = gpu_accel.remove()
            except Exception:
                logger.warning("Removing the GPU libraries failed", exc_info=True)
            self._run_on_gui(self._on_gpu_removed, bool(now))
        threading.Thread(target=_run, name="gpu-remove", daemon=True).start()
        return True

    def _on_gpu_removed(self, removed_now):
        self._gpu_removing = False
        if not removed_now:
            # Windows keeps a loaded library locked: it goes at the next start.
            self.show_tray_hint("GPU acceleration removed",
                                f"The files are deleted the next time {APP_NAME} starts.")
        dl = getattr(self, "downloads", None)
        if dl is not None:
            try:
                dl.probe_hardware(force=True)
            except Exception:
                logger.debug("hardware re-probe failed", exc_info=True)

    def _verify_gpu(self):
        """Worker thread, right after the download: load the speech model on
        the GPU now, so the user learns at once whether it runs (an old driver
        or too little video memory fails here - and the CPU keeps working)."""
        try:
            gpu_accel.register_dll_dirs()
        except Exception:
            pass
        rec = self.recorder
        rec._cuda_usable = None                       # let it try the GPU again
        rec._cuda_error = ""
        name = self.cfg.get("whisper_model") or "base"
        if not self._can_verify_gpu():
            return "pending", ""                      # tried at the next local transcription
        if getattr(self, "_file_job_running", False) is True:
            # A file job holds the speech model: reloading it now would stall
            # (or evict) that job. Tried at the next local transcription.
            return "pending", ""
        try:
            # A transcription still running keeps its own (CPU) model; the
            # next one gets the GPU model loaded here.
            rec.unload_model()
            rec.load_model(name)
        except Exception as e:
            if gpu_accel.is_cuda_error(e):
                return "gpu_failed", _friendly_transcription_error(e)
            # The model didn't load at all (a damaged cache...): the GPU is
            # untested, not failed - it's tried at the next transcription.
            logger.warning("GPU check couldn't load the speech model: %s", e)
            return "pending", ""
        if getattr(rec, "_cuda_usable", None) is True:
            return "ok", ""
        err = getattr(rec, "_cuda_error", "") or "the graphics card couldn't run the speech model"
        if gpu_accel.is_oom(err) or name in AudioRecorder._oom_models(rec):
            return "gpu_oom", name
        return "gpu_failed", err

    def _can_verify_gpu(self):
        # File jobs run locally whatever the dictation backend, so the GPU is
        # worth testing whenever the local model is on disk.
        return model_downloaded(self.cfg.get("whisper_model") or "base")

    def _on_gpu_runtime_failed(self, message):
        """The GPU failed during a real transcription (worker -> here): remember
        it, so later runs stay on the CPU and Settings says why. Out of memory
        is about that model's size, not the GPU - not remembered."""
        if gpu_accel.is_oom(message) or not gpu_accel.libs_dir():
            return
        if self.cfg.get(gpu_accel.CFG_FAILED) != message[:300]:
            self.cfg[gpu_accel.CFG_FAILED] = (message or "unknown error")[:300]
            self.save_config()

    def _on_gpu_done(self, state, message):
        self._gpu_phase = None
        self.track("gpu_download_result", {"state": state})
        if state == "ok":
            self.cfg.pop(gpu_accel.CFG_FAILED, None)
            self.show_tray_hint("GPU acceleration is on",
                                "Local transcription now runs on your NVIDIA graphics card.")
        elif state == "pending":
            self.show_tray_hint("GPU acceleration is installed",
                                "It's used from your next transcription with a local "
                                "Whisper model.")
        elif state == "gpu_oom":
            self.show_tray_hint(
                "Model too big for your graphics card",
                f"Your NVIDIA card ran out of memory with the {message} model, so it runs "
                "on the processor. A smaller model (e.g. Small) runs on the GPU.",
                on_click=self.show_gpu_settings)
        elif state == "gpu_failed":
            self.cfg[gpu_accel.CFG_FAILED] = (message or "unknown error")[:300]
            self.show_tray_hint(
                "GPU acceleration couldn't start",
                "Your graphics card couldn't run it, so transcription stays on the "
                "processor. Updating your NVIDIA driver usually fixes this - then "
                "Settings › Speech & AI › GPU acceleration › Try again.",
                on_click=self.show_gpu_settings)
        elif state == "failed":
            self.show_tray_hint("GPU acceleration download failed",
                                message or "Please try again.", on_click=self.show_gpu_settings)
        self.save_config()
        # The GPU state changed: "Best for your computer" and the GPU card
        # follow the fresh hardware snapshot.
        dl = getattr(self, "downloads", None)
        if dl is not None and state not in ("cancelled",):
            try:
                dl.probe_hardware(force=True)
            except Exception:
                logger.debug("hardware re-probe failed", exc_info=True)

    def show_gpu_settings(self):
        """Settings › Speech & AI, on the GPU acceleration card (the GPU
        notifications' click)."""
        return self.show_main("settings", section="speech", focus="gpu")

    def _note_local_wait(self, context, seconds, model=None, backend=None):
        """Any thread: the user just waited ``seconds`` on a local transcription
        - maybe the moment to suggest GPU acceleration (decided on the GUI
        thread, rarely; see gpu_accel.should_offer_now). File jobs always run
        locally, whatever the dictation backend. ``backend``: the take's own
        (a Smart Action may transcribe differently from Dictation)."""
        try:
            if context != "file" and (backend or self.cfg.get("backend") or "local") != "local":
                return
            self.sig_gpu_wait.emit(context, str(model or self.cfg.get("whisper_model") or ""),
                                   float(seconds))
        except Exception:
            pass

    def _on_gpu_wait(self, context, model, seconds):
        on_gpu = getattr(getattr(self, "recorder", None), "_cuda_usable", None) is True
        if not gpu_accel.should_offer_now(self.cfg, model, seconds, on_gpu=on_gpu,
                                          local_job=(context == "file")):
            return
        # A few seconds later: the same dictation's own notices (Smart Action
        # results, cloud limits) would otherwise replace it at once.
        QTimer.singleShot(6000, lambda: self._show_gpu_offer(context, model, seconds))

    def _show_gpu_offer(self, context, model, seconds):
        on_gpu = getattr(getattr(self, "recorder", None), "_cuda_usable", None) is True
        if not gpu_accel.should_offer_now(self.cfg, model, seconds, on_gpu=on_gpu,
                                          local_job=(context == "file")):
            return
        if time.monotonic() - getattr(self, "_last_hint_at", -1e9) < 4:
            return                              # another notice is up: not now, not counted
        gpu_accel.note_offered(self.cfg)
        self.save_config()
        self.track("gpu_offer_shown", {"context": context, "model": model})
        self.show_tray_hint(
            "Speed up transcription with your NVIDIA GPU",
            f"That took {seconds:.0f} s on the processor. GPU acceleration gives "
            f"{gpu_accel.speedup_text()} - click to set it up (one-time "
            f"{gpu_accel.DOWNLOAD_MB} MB download from NVIDIA).",
            on_click=self.show_gpu_settings)

    # ── Updates (updater.py: the same GitHub release, tag and installer name
    #    every 1.9.x install updates through) ──
    def _background_check_updates(self):
        """Worker: the one automatic update check of a launch. The answer is
        handled on the GUI thread (_on_update_checked) - pending_update_version
        is the controller's key, written there."""
        try:
            status, tag = updater.check_latest(self.cfg)
        except Exception as e:
            logger.debug("Background update check failed: %s", e)
            status, tag = updater.ERROR, ""
        if status == updater.LATEST and sys.platform == "win32":
            updater.prune_cache()          # up to date: no installer kept in %TEMP%
        self.track("update_check_result", updater.check_props(status, manual=False))
        self._sig_update_checked.emit(status, tag)

    def _on_update_checked(self, status, tag):
        """GUI thread: the startup check's answer."""
        self.note_update_check(status, tag, manual=False)

    def note_update_check(self, status, tag, manual=True):
        """An update check answered (the startup one, or Settings' Check now -
        the page tracks its own). Stores pending_update_version (pages may not
        write it), fetches the installer ahead for "Install updates when I
        quit", and - for the automatic check only - asks for the quiet notice."""
        pending = updater.pending_after_check(self.cfg, status, tag)
        if pending is not None:
            self.cfg["pending_update_version"] = pending
            self.schedule_config_save()
            self.sig_setting_changed.emit("pending_update_version")
        if status in (updater.AVAILABLE, updater.DISMISSED) and tag:
            self._prepare_update_on_quit(tag)
        if not manual and status == updater.AVAILABLE and updater.should_prompt(self.cfg, tag, APP_VERSION):
            self.sig_update_available.emit(tag)

    def _prompt_update(self, tag):
        """GUI thread (sig_update_available): ONE quiet notice per run for a
        newer release - a tray notification whose click installs it; Settings ›
        Updates keeps "Install update" for later. Never a modal, never while
        recording, never for a release the user said "Not now" to
        (dismissed_update_version, updater.should_prompt)."""
        if not tag:
            return
        if not self._is_newer(tag):
            if self.cfg.get("pending_update_version") == tag:
                # Installed that version another way: nothing pending any more.
                self.cfg["pending_update_version"] = ""
                self.save_config()
            return
        if (self.is_rec or AppController._is_meeting_busy(self)
                or not updater.should_prompt(self.cfg, tag, APP_VERSION)):
            return                              # a meeting: shown after a later dictation
        if getattr(self, "_update_notice_shown", None) == tag:
            return                              # once a run, not after every dictation
        self._update_notice_shown = tag
        self.show_tray_hint(
            "Update available",
            f"{APP_NAME} {tag} is ready. Click to install it now, or later in Settings.",
            on_click=lambda t=tag: self.install_update(t, manual=False))

    def _nudge_update_after_dictation(self):
        """Any thread, after a dictation: the quiet update notice, if one is
        due and wasn't shown this run - a moment after the "Pasted" pill, on
        the GUI thread (the dictation worker never waits for it)."""
        pending = self.cfg.get("pending_update_version", "")
        if (not pending or getattr(self, "_update_notice_shown", None) == pending
                or not updater.should_prompt(self.cfg, pending, APP_VERSION)):
            return
        self._run_on_gui(QTimer.singleShot, 2500, lambda t=pending: self._prompt_update(t))

    @staticmethod
    def _is_newer(tag):
        """True when release ``tag`` is newer than the running version."""
        return _parse_version(tag) > _parse_version(APP_VERSION)

    def install_update(self, tag, manual=True, on_done=None):
        """Download, check (sha256 when published) and run the installer for
        ``tag`` on a worker (updater.install), then quit so it can replace the
        app. On macOS (no installer to run) the release page opens instead.
        ``on_done(ok, info)`` runs on the GUI thread when it did NOT install
        (info: a short message) so a page can say so; without it a tray
        notification does. False when an install is already running, or when
        it can't install now (a recording or a file job: installing quits the
        app) - on_done / the notification then says why."""
        if getattr(self, "_update_installing", False):
            return False
        blocked = AppController._update_blocker(self)
        if blocked:
            if callable(on_done):
                try:
                    on_done(False, blocked)
                except Exception:
                    logger.debug("update result callback failed", exc_info=True)
            else:
                self.show_tray_hint("Update not installed", blocked)
            return False
        self._update_installing = True
        if not manual:
            self.track("update_install_started", {"manual": False, "to": tag})
        ready = getattr(self, "_update_ready", None)

        def _run():
            try:
                if ready and ready[0] == tag and os.path.isfile(ready[1]):
                    ok, info = updater.launch(ready[1])      # fetched ahead already
                else:
                    ok, info = updater.install(tag)
            except Exception as e:
                logger.warning("Update install failed: %s", e)
                ok, info = False, "Couldn't install the update."
            self._run_on_gui(self._on_update_installed, tag, ok, info, manual, on_done)
        threading.Thread(target=_run, daemon=True).start()
        return True

    def _update_blocker(self):
        """Why an update can't install right now ("" = it can): installing
        quits the app, which would end a recording or a file being converted."""
        if getattr(self, "is_rec", False) is True or AppController._is_meeting_busy(self):
            return "Finish the recording first, then install the update."
        if getattr(self, "_file_job_running", False) is True:
            return "Wait for the file to finish converting, then install the update."
        return ""

    def _on_update_installed(self, tag, ok, info, manual, on_done):
        self._update_installing = False
        self.track("update_install_result", {"manual": bool(manual), "ok": bool(ok)})
        if ok:
            # The installer is running: quit so it can replace the files.
            self._update_ready = None
            self.cfg["pending_update_version"] = ""
            self.flush_config_save()
            QTimer.singleShot(1500, self.quit_app)
            return
        if info == updater.UNSUPPORTED:
            webbrowser.open(updater.release_page(tag))
            info = ""
        if callable(on_done):
            try:
                on_done(False, info)
            except Exception:
                logger.debug("update result callback failed", exc_info=True)
        elif info:
            self.show_tray_hint("Update not installed", info)

    def _prepare_update_on_quit(self, tag):
        """"Install updates when I quit" (Windows): fetch and check the
        installer now, on a worker, so quitting only has to start it. It is
        kept per release in %TEMP%\\gusan-update\\<tag>\\ and reused by the
        next runs once its sha256 checks out (updater.cached_download) - not
        ~100 MB downloaded again every launch."""
        if sys.platform != "win32" or not self.cfg.get("install_updates_on_quit", True):
            return
        if not updater.should_prompt(self.cfg, tag, APP_VERSION):
            return              # not newer, or a version the user skipped
        ready = getattr(self, "_update_ready", None)
        if (ready and ready[0] == tag) or getattr(self, "_update_fetching", None) == tag:
            return
        self._update_fetching = tag

        def _run():
            try:
                # Its own folder: an "Install now" download never writes the same file.
                ok, info = updater.cached_download(tag)
            except Exception as e:
                ok, info = False, str(e)
            self._run_on_gui(self._on_update_fetched, tag, ok, info)
        threading.Thread(target=_run, daemon=True).start()

    def _on_update_fetched(self, tag, ok, info):
        self._update_fetching = None
        if ok:
            self._update_ready = (tag, info)
        else:
            logger.info("Update %s not fetched ahead: %s", tag, info)

    def _on_own_setting_changed(self, key):
        """sig_setting_changed for the controller's own services."""
        if key == "install_updates_on_quit" and self.cfg.get(key, True):
            pending = self.cfg.get("pending_update_version") or ""
            if pending:
                self._prepare_update_on_quit(pending)

    def _run_update_on_quit(self):
        """quit_app: start the installer fetched ahead, when the setting is on
        and it is still newer than this version."""
        ready, self._update_ready = getattr(self, "_update_ready", None), None
        if not ready or not self.cfg.get("install_updates_on_quit", True):
            return
        tag, path = ready
        # Still newer, not skipped meanwhile, and still on disk.
        if not (updater.should_prompt(self.cfg, tag, APP_VERSION) and os.path.isfile(path)):
            return
        self.track("update_install_started", {"manual": False, "to": tag})
        ok, _info = updater.launch(path)
        self.track("update_install_result", {"manual": False, "ok": bool(ok)})
        if ok:
            self.cfg["pending_update_version"] = ""
            self.schedule_config_save()           # quit_app flushes it right after

    # ── Modular Window Surface Triggers (Main thread-safe wrappers) ──
    @staticmethod
    def _bring_to_front(w):
        """Reliably surface a window. A minimized window ignores plain show() on
        Windows - which made tray clicks look like they did nothing - and a
        window behind others needs the raise+activate pair."""
        if w.isMinimized():
            w.showNormal()
        w.show()
        w.raise_()
        w.activateWindow()

    def _ensure_main_window(self):
        """The Gusan window (ui/shell.py), built the first time it opens -
        never at startup before the tray, so launch stays fast."""
        if self.main_win is None:
            from ui.shell import MainWindow
            win = MainWindow(main_app=self)
            self.sig_setting_changed.connect(win.on_setting_changed)
            self.main_win = win
        return self.main_win

    def show_main(self, page=None, **opts):
        """Open the app: the Gusan window on ``page`` (a ui.shell route such as
        "home" or "settings"; None keeps the page it was on, home the first
        time). ``opts`` go to the page (e.g. section="speech")."""
        try:
            win = self._ensure_main_window()
            win.open(page or win.current_route() or "home", **opts)
        except Exception:
            # The window itself couldn't be built (a page that fails shows its
            # own error card instead). Say so rather than doing nothing.
            logger.error("The Gusan window failed to open", exc_info=True)
            try:
                self.show_tray_hint(f"{APP_NAME} couldn't open its window",
                                    "Try again from the tray. If it keeps happening, restart the app.")
            except Exception:
                logger.debug("tray hint failed", exc_info=True)
            return None
        self._bring_to_front(win)
        self.track("settings_opened")
        return win

    # The 1.x entry points keep their names (IPC, notifications, old callers)
    # and open their Gusan pages.
    def show_settings(self, section=None, **opts):
        """Settings (``section``: general | microphone | speech | privacy |
        updates; ``focus`` e.g. "gpu")."""
        if section:
            opts["section"] = section
        return self.show_main("settings", **opts)

    def show_file_transcribe(self, paths=None):
        """Audio to text (the tray, drops); ``paths`` are queued at once."""
        if paths:
            return self.show_main("audio_to_text", paths=paths)
        return self.show_main("audio_to_text")

    def show_history(self):
        """Home: its Recent list is the history."""
        win = self.show_main("home")
        self.track("history_opened")
        return win

    def show_meeting(self):
        """Meetings, for every plan: the Record button is the Pro gate (and
        the live view opens by itself while a meeting records)."""
        return self.show_main("meetings")

    def show_onboarding(self):
        """The 5-step welcome (ui/onboarding_v2) - first run only. An onboarded
        user (every 1.9 upgrader) gets the app instead and never sees it.
        Finishing opens Home; closing it with X writes nothing, so it shows
        again at the next launch."""
        if self.cfg.get("onboarding_done", False):
            return self.show_main()
        wiz = getattr(self, "onboarding_win", None)
        try:
            if wiz is not None:
                wiz.isVisible()                  # still alive?
        except RuntimeError:
            wiz = None
        if wiz is None:
            # The model step's "Your computer" chips and "Best for your
            # computer" come from the hardware probe (1-2 s on a worker).
            dl = getattr(self, "downloads", None)
            if dl is not None:
                try:
                    dl.probe_hardware()
                except Exception:
                    logger.debug("hardware probe failed to start", exc_info=True)
            from ui.onboarding_v2 import OnboardingWizard
            wiz = OnboardingWizard(self)
            # Land in the app, not silently in the tray: first-run users
            # otherwise think it closed.
            wiz.accepted.connect(lambda: self.show_main("home"))
            self.onboarding_win = wiz
        self._bring_to_front(wiz)
        return wiz

    def front_first_run_wizard(self):
        """The first-run wizard is on screen: bring it forward and say True
        (a tray click or a second launch then never opens the app behind it).
        False when there is none - closed with X, or onboarding is done."""
        wiz = getattr(self, "onboarding_win", None)
        try:
            if (wiz is not None and wiz.isVisible()
                    and not self.cfg.get("onboarding_done", False)):
                self._bring_to_front(wiz)
                return True
        except RuntimeError:
            pass                                  # the wizard was deleted
        return False

    # ── the one-time "Transcribe is now Gusan" line on Home ──
    def rename_note_due(self):
        """Home shows the upgrade line (settings from 1.x, not shown yet)."""
        return bool(self.cfg.get("rename_note_pending", False))

    def dismiss_rename_note(self):
        """Home showed it (or it was closed): never again."""
        if self.cfg.get("rename_note_pending"):
            self.cfg["rename_note_pending"] = False
            self.schedule_config_save()

    def show_macos_permissions_guide(self):
        """macOS only: a one-time walkthrough of the three permissions the app
        needs (Microphone for dictation, Accessibility for auto-paste, Input
        Monitoring for the global hotkey), with buttons that deep-link straight
        into the right System Settings pane. Without these grants the app looks
        silently broken on a Mac."""
        if sys.platform != "darwin":
            return
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                       QLabel, QPushButton)
        import subprocess

        dlg = QDialog()
        dlg.setWindowTitle("Set up macOS permissions")
        if hasattr(self, "style_content"):
            dlg.setStyleSheet(self.style_content)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(10)
        title = QLabel("Three quick permissions", dlg)
        title.setStyleSheet("font-size: 17px; font-weight: 700; color: #0f172a;")
        lay.addWidget(title)
        sub = QLabel(
            "macOS requires your explicit approval for each of these. "
            "Grant them once and dictation just works.", dlg)
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #475569;")
        lay.addWidget(sub)

        perms = (
            ("Microphone", "Hear your voice for dictation.",
             "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"),
            ("Accessibility", "Paste the finished text into the app you're using.",
             "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"),
            ("Input Monitoring", "Detect the global dictation hotkey.",
             "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"),
        )
        for name, why, url in perms:
            row = QHBoxLayout()
            row.setSpacing(10)
            txt = QLabel(f"<b>{name}</b><br><span style='color:#64748b'>{why}</span>", dlg)
            txt.setWordWrap(True)
            row.addWidget(txt, 1)
            btn = QPushButton("Open Settings", dlg)
            btn.setObjectName("secondaryButton")
            btn.clicked.connect(lambda _=False, u=url: subprocess.Popen(["open", u]))
            row.addWidget(btn, 0)
            lay.addLayout(row)

        done = QPushButton("Done - I've granted them", dlg)
        done.setObjectName("primaryButton")
        done.setMinimumHeight(38)
        done.clicked.connect(dlg.accept)
        lay.addWidget(done)

        dlg.exec()
        self.cfg["macos_perms_guide_shown"] = True
        self.save_config()

    def show_auth_gate(self):
        """Sign in / create an account (ui/auth_sheet.py): one sheet, brought
        forward on repeated clicks. Used by the tray, the pages and the Pro
        window. Already signed in: Account & plan instead."""
        if self.auth.is_authenticated:
            return self.show_main("account")
        return self._show_auth_sheet("signin")

    def show_account_gate(self):
        """The sign-up sheet once, for a user who has never seen it. Nothing
        opens it on its own any more: new users meet sign-up in the welcome
        wizard, and existing users are never interrupted at launch."""
        if self.cfg.get("account_gate_seen", False):
            return None
        if self.auth.is_authenticated:
            self.set_setting("account_gate_seen", True)
            return None
        sheet = self._show_auth_sheet("signup")
        if sheet is not None and not getattr(sheet, "_gusan_gate", False):
            sheet._gusan_gate = True
            sheet.finished.connect(lambda _result: self.set_setting("account_gate_seen", True))
        return sheet

    def _show_auth_sheet(self, mode):
        try:
            from ui.auth_sheet import show_auth_sheet
            sheet = show_auth_sheet(self, mode, parent=self._dialog_parent())
        except Exception:
            logger.warning("The sign-in sheet failed to open", exc_info=True)
            self.show_main("account")
            return None
        if not getattr(sheet, "_gusan_wired", False):
            sheet._gusan_wired = True
            # Signed in: the app, on the page it was on (Home the first time).
            sheet.signed_in.connect(lambda: self.show_main())
        return sheet

    # ── Instant-apply settings (Gusan 2.0 pages) ──
    def set_setting(self, key, value):
        """Change one setting now: written in place, its side effect run
        (hotkeys, privacy, start at login, model preload, dictation bar), saved
        250 ms later, then sig_setting_changed(key). False when refused or when
        its side effect failed. See settings_apply.py. GUI thread only (the
        save timer and the hotkey hooks live there): a worker hands its change
        over with a queued signal."""
        return bool(self.set_settings({key: value}).get(key, False))

    def set_settings(self, updates):
        """Several settings at once (one save, each side effect once).
        Returns {key: ok}."""
        if updates.get("notion_api_key") and updates.get("notion_auth_mode"):
            # A fresh Notion connection: stamp whose it is now (see
            # _reconcile_notion_owner), not at the next sign-in, or it would
            # look like nobody's when its account signs out.
            self.cfg["notion_owner"] = str(getattr(getattr(self, "auth", None), "user_id", None) or "")
        return settings_apply.apply(self, updates, autostart_mod=autostart)

    def schedule_config_save(self):
        """Save config.json ~250 ms after the last change of a burst."""
        t = getattr(self, "_save_timer", None)
        if t is None:
            self.flush_config_save()
        else:
            t.start()

    def _flush_pending_save(self):
        """On quit: write a debounced save that hasn't happened yet."""
        t = getattr(self, "_save_timer", None)
        try:
            if t is not None and t.isActive():
                self.flush_config_save()
        except Exception:
            logger.debug("final settings save failed", exc_info=True)

    def flush_config_save(self):
        """Write the config now (the debounced save; also on quit). Lighter than
        save_config(): no tray icon redraw, no stylesheet re-apply."""
        t = getattr(self, "_save_timer", None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
        try:
            save_config(self.cfg)
            self._track_setting_changes()
        except Exception:
            logger.warning("Saving settings failed", exc_info=True)

    def save_config(self):
        save_config(self.cfg)
        self._track_setting_changes()
        self.update_tray_icon()

    # ── Hotkeys ──────────────────────────────────────────────────────────────
    # The dictation hotkey can be either a keyboard combo ("alt+r") or a mouse
    # button ("mouse:middle"). We use the `keyboard` library on Windows for
    # keyboard combos because it uses a proven low-level hook (the same one
    # that worked in earlier Tkinter releases). On macOS/Linux we fall back to
    # pynput's GlobalHotKeys. Mouse always goes through pynput.
    #
    # All listeners emit `sig_hotkey` rather than calling handlers directly,
    # so the work is marshalled onto the Qt main thread safely.
    def _setup_hotkey(self, hotkey, remove_old=None):
        # Tear down any previous registration before installing the new one.
        self._unregister_kbd_hotkey()
        self._unregister_mouse_listener()

        if hotkeys.is_bare_typing_key(hotkey):
            # A lone typing key ("space") would start dictation on every
            # keystroke - older capture screens could save one. Back to the
            # default, and say so once the tray is up.
            bad, hotkey = hotkey, DEFAULT["hotkey"]
            logger.warning("Hotkey %r is a bare typing key - using %r", bad, hotkey)
            self.cfg["hotkey"] = hotkey
            self.save_config()
            QTimer.singleShot(3000, lambda: self.show_tray_hint(
                "Hotkey reset",
                f"{hotkeys.display(bad)} would trigger while you type, so dictation is back "
                f"on {hotkeys.display(hotkey)}. Pick another on the Dictation page.",
                on_click=lambda: self.show_main("dictation", focus="hotkey")))

        try:
            if hotkey.startswith("mouse:"):
                from pynput import mouse as pynput_mouse
                btn_map = {
                    "middle": pynput_mouse.Button.middle,
                    "left":   pynput_mouse.Button.left,
                    "right":  pynput_mouse.Button.right,
                    "x1":     pynput_mouse.Button.x1,
                    "x2":     pynput_mouse.Button.x2,
                }
                target = btn_map.get(hotkey.split(":")[1], pynput_mouse.Button.middle)

                def _on_click(x, y, btn, pressed):
                    if pressed and btn == target:
                        self.sig_hotkey.emit()

                listener = pynput_mouse.Listener(on_click=_on_click)
                listener.daemon = True
                listener.start()
                self._mouse_listener = listener
                logger.info("Registered mouse hotkey: %s", hotkey)
            elif sys.platform == "win32":
                import keyboard as kbd_lib
                # `keyboard` accepts the same "alt+r" / "ctrl+shift+space"
                # syntax we already produce in settings.py.
                kbd_lib.add_hotkey(
                    hotkey,
                    lambda: self.sig_hotkey.emit(),
                    suppress=False,
                    trigger_on_release=False,
                )
                self._registered_kbd_hotkey = hotkey
                logger.info("Registered keyboard hotkey: %s", hotkey)
            else:
                # macOS / Linux: pynput GlobalHotKeys (handles modifier tracking
                # internally - more robust than a custom Listener).
                from pynput import keyboard as pynput_keyboard
                hotkeys.prepare_pynput_listeners()
                listener = pynput_keyboard.GlobalHotKeys({
                    self._to_pynput_hotkey(hotkey): lambda: self.sig_hotkey.emit(),
                })
                listener.daemon = True
                listener.start()
                self._kbd_listener = listener
                logger.info("Registered keyboard hotkey (pynput): %s", hotkey)
        except Exception as e:
            logger.warning("Could not register hotkey %s: %s", hotkey, e)
            return False
        return True

    def _setup_assist_hotkey(self, hotkey):
        """Second global hotkey: toggles the Live Assistance overlay. Kept apart
        from the dictation hotkey's bookkeeping so re-registering one never
        tears down the other."""
        if self._registered_assist_hotkey is not None:
            try:
                import keyboard as kbd_lib
                kbd_lib.remove_hotkey(self._registered_assist_hotkey)
            except Exception:
                pass
            self._registered_assist_hotkey = None
        if self._assist_listener is not None:
            try:
                self._assist_listener.stop()
            except Exception:
                pass
            self._assist_listener = None
        # Normalise "Alt + E" -> "alt+e" (what the keyboard library expects).
        hotkey = "+".join(p.strip().lower() for p in (hotkey or "").split("+") if p.strip())
        dictation = "+".join(p.strip().lower()
                             for p in (self.cfg.get("hotkey") or "").split("+") if p.strip())
        if not hotkey or hotkey == dictation or hotkeys.is_bare_typing_key(hotkey):
            return False
        try:
            if sys.platform == "win32":
                import keyboard as kbd_lib
                kbd_lib.add_hotkey(hotkey, lambda: self.sig_assist_hotkey.emit(),
                                   suppress=False, trigger_on_release=False)
                self._registered_assist_hotkey = hotkey
            else:
                from pynput import keyboard as pynput_keyboard
                hotkeys.prepare_pynput_listeners()
                listener = pynput_keyboard.GlobalHotKeys({
                    self._to_pynput_hotkey(hotkey): lambda: self.sig_assist_hotkey.emit(),
                })
                listener.daemon = True
                listener.start()
                self._assist_listener = listener
            logger.info("Registered Live Assistance hotkey: %s", hotkey)
            return True
        except Exception as e:
            logger.warning("Could not register Live Assistance hotkey %s: %s", hotkey, e)
            return False

    def _release_smart_hotkey(self):
        hk = getattr(self, "_registered_smart_hotkey", None)
        if hk is not None:
            try:
                import keyboard as kbd_lib
                kbd_lib.remove_hotkey(hk)
            except Exception:
                pass
            self._registered_smart_hotkey = None
        listener = getattr(self, "_smart_listener", None)
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
            self._smart_listener = None

    def _setup_smart_hotkey(self, hotkey):
        """Third global hotkey, the Smart Actions shortcut (default Alt+S,
        Option+S on a Mac): ONE take runs as a Smart Action (Alt+R always
        pastes the exact words). Keyboard only, never on the dictation's or
        the Live Assistant's keys; kept apart from both, like the Live
        Assistant's. True when it registered."""
        self._release_smart_hotkey()
        hotkey = settings_apply.norm_hotkey(hotkey)
        taken = {settings_apply.norm_hotkey(self.cfg.get("hotkey")),
                 settings_apply.norm_hotkey(self.cfg.get("live_assist_hotkey"))}
        if (not hotkey or hotkey in taken or hotkey.startswith("mouse:")
                or hotkeys.is_bare_typing_key(hotkey)):
            return False
        try:
            if sys.platform == "win32":
                import keyboard as kbd_lib
                kbd_lib.add_hotkey(hotkey, lambda: self.sig_smart_hotkey.emit(),
                                   suppress=False, trigger_on_release=False)
                self._registered_smart_hotkey = hotkey
            else:
                from pynput import keyboard as pynput_keyboard
                hotkeys.prepare_pynput_listeners()
                listener = pynput_keyboard.GlobalHotKeys({
                    self._to_pynput_hotkey(hotkey): lambda: self.sig_smart_hotkey.emit(),
                })
                listener.daemon = True
                listener.start()
                self._smart_listener = listener
            logger.info("Registered Smart Actions hotkey: %s", hotkey)
            return True
        except Exception as e:
            logger.warning("Could not register Smart Actions hotkey %s: %s", hotkey, e)
            return False

    def _on_smart_hotkey(self):
        """GUI thread: the Smart Actions shortcut. Starts ONE take that runs
        as a Smart Action - Pro always; Free while tries are left (the try is
        counted when it finishes, _stop_impl); Free with none left gets the
        Pro window; a guest the sign-up sheet. Pressed while recording it
        just stops the take, which keeps the kind it started with (so does
        Alt+R). While a meeting records, only the quiet "paused" hint
        (_on_hotkey) - never the Pro window or sign-up over a meeting."""
        was_rec = self.is_rec
        if not was_rec:
            if getattr(self, "_hotkeys_suspended", False) is True:
                return              # a shortcut is being captured (a late hook event)
            if AppController._meeting_holds_recorder(self):
                pass                # no take can start: _on_hotkey says why
            elif not self.is_pro():
                auth_now, cfg_now = getattr(self, "auth", None), getattr(self, "cfg", None)
                if entitlements.tier(auth_now, cfg_now) == entitlements.TIER_GUEST:
                    self.show_auth_gate()       # signing up gives the free tries
                    return
                if not entitlements.can_use_smart_action(auth_now, cfg_now):
                    self._pro_upsell("Smart Actions")
                    return
            self._smart_next = True
        try:
            self._on_hotkey()
        finally:
            self._smart_next = False

    def _ensure_live_assist(self):
        """The Live Assistant card (ui/live_assist.py), built on first use and
        fed from the meeting session's live pipeline."""
        if self.live_assist is None:
            from ui.live_assist import LiveAssistOverlay
            card = LiveAssistOverlay(main_app=self)
            s = getattr(self, "meeting_session", None)
            if s is not None:
                # Emitted from worker threads; the card's slots run on the GUI thread.
                s.sig_chunk.connect(card.feed_transcript)
                s.sig_summary.connect(card.set_summary)
                if getattr(s, "state", None) == getattr(s, "STATE_RECORDING", "recording"):
                    # Opened mid-meeting: catch up (no answers for old questions).
                    card.set_meeting_active(True, getattr(s, "_meeting_title", ""),
                                            getattr(s, "_meeting_attendees", ""))
                    card.feed_transcript(getattr(s, "_live_text", ""), answer=False)
                    card.set_summary(getattr(s, "_live_summary_text", ""))
            self.live_assist = card
        return self.live_assist

    def toggle_live_assist(self):
        """Show/hide the private Live Assistant card (its hotkey, the tray)."""
        try:
            self._ensure_live_assist().toggle()
        except Exception as e:
            logger.warning("Live Assistant failed: %s", e, exc_info=True)
            self.show_tray_hint("Live Assistant", f"Couldn't open the Live Assistant: {e}")

    def show_live_assist(self):
        """Show the Live Assistant card - never hide it (the Live Assistant
        page's Open, the Meetings live view, a calendar reminder)."""
        try:
            self._ensure_live_assist().open_card()
        except Exception as e:
            logger.warning("Live Assistant failed: %s", e, exc_info=True)
            self.show_tray_hint("Live Assistant", f"Couldn't open the Live Assistant: {e}")

    def suspend_hotkeys(self, on):
        """Pause the global shortcuts while a page captures a new one (so the
        current Alt+R doesn't start a dictation mid-capture) and put them back
        after. Nothing in the config changes. GUI thread."""
        on = bool(on)
        if on == (getattr(self, "_hotkeys_suspended", False) is True):
            return True
        self._hotkeys_suspended = on
        if on:
            self._unregister_kbd_hotkey()
            self._unregister_mouse_listener()
            self._setup_assist_hotkey("")          # releases it, registers nothing
            self._release_smart_hotkey()
        else:
            self.apply_tray_bindings()
        return True

    def toggle_dictation(self):
        """Start or finish a dictation, as the shortcut does (the tray's Dictate)."""
        self._on_hotkey()

    def _unregister_kbd_hotkey(self):
        if self._registered_kbd_hotkey is not None:
            try:
                import keyboard as kbd_lib
                kbd_lib.remove_hotkey(self._registered_kbd_hotkey)
            except Exception:
                pass
            self._registered_kbd_hotkey = None
        if self._kbd_listener is not None:
            try:
                self._kbd_listener.stop()
            except Exception:
                pass
            self._kbd_listener = None

    def _unregister_mouse_listener(self):
        if self._mouse_listener is not None:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
            self._mouse_listener = None

    @staticmethod
    def _to_pynput_hotkey(hotkey):
        # "alt+shift+r" -> "<alt>+<shift>+r" for pynput.GlobalHotKeys
        return hotkeys.to_pynput(hotkey)

    def apply_tray_bindings(self):
        self._setup_hotkey(self.cfg["hotkey"])
        # Re-evaluate the overlay hotkey too: if the dictation hotkey was just
        # changed to the same combo, this drops the overlay binding instead
        # of letting both fire on one keypress.
        self._setup_assist_hotkey(self.cfg.get("live_assist_hotkey", ""))
        self._setup_smart_hotkey(self.cfg.get("smart_actions_hotkey", ""))

    # ── Event Callbacks ──
    def _on_levels(self, levels):
        self.overlay.call_soon(self.overlay.update_levels, levels)

    def _on_lang(self, code, name):
        self.overlay.call_soon(self.overlay.set_lang, name)

    def _on_partial(self, text):
        self.overlay.call_soon(self.overlay.set_partial, text)

    def _on_hotkey(self):
        logger.info("[main] _on_hotkey fired (is_rec=%s)", self.is_rec)
        if getattr(self, "_hotkeys_suspended", False) is True and not self.is_rec:
            return              # a shortcut is being captured (a late hook event)
        # A previous dictation is still transcribing/processing - ignore the
        # hotkey so we never re-enter the shared recorder mid-flight (crash).
        if getattr(self, "_busy", False):
            # If Esc already cancelled the transcribe/process phase, the worker
            # may still be unwinding. Stay quiet so the user doesn't see a stale
            # "previous dictation" warning for something they just cancelled.
            if getattr(self, "_cancel_processing", False):
                return
            if not self.is_rec:
                self.show_tray_hint(
                    "One moment",
                    "Finishing your previous dictation - try again in a second.",
                )
            return
        if not self.is_rec:
            # Dictation and the meeting recorder share one AudioRecorder + audio
            # device. Don't let the hotkey start a dictation on top of an active
            # meeting - it would clobber the meeting's audio stream. Tell the
            # user instead. (Notes written from a saved transcript don't use
            # the recorder: dictation works meanwhile.)
            if AppController._meeting_holds_recorder(self):
                if getattr(self, "_smart_next", False) is True:
                    # The Smart Actions shortcut: name it, not Alt+R.
                    body = ("Smart Actions are paused while a meeting is recording. "
                            "Stop the meeting to use them again.")
                else:
                    body = (f"{hotkeys.display(self.cfg.get('hotkey', 'alt+r'))} dictation is paused while a "
                            "meeting is recording so the two don't mix. Stop the meeting to dictate again.")
                self.show_tray_hint("Meeting Recording Active", body)
                return
            self._start()
        else:
            threading.Thread(target=self._stop, daemon=True).start()

    def _is_meeting_busy(self):
        """True while a meeting (or a Live Assistant session) records - paused
        included - or writes its notes, whoever started it: the meeting
        session then owns the shared AudioRecorder."""
        s = getattr(self, "meeting_session", None)
        return bool(s is not None and getattr(s, "is_busy", False))

    def _meeting_holds_recorder(self):
        """The meeting session owns the shared recorder right now: recording
        (paused included) or transcribing what it recorded. Writing notes from
        a saved transcript (Retry, Rewrite, notes a Quit left) doesn't."""
        s = getattr(self, "meeting_session", None)
        if s is None:
            return False
        holds = getattr(s, "holds_recorder", None)
        if holds is None:
            holds = getattr(s, "is_busy", False)
        return bool(holds)

    def force_stop_dictation(self):
        """Cancel an in-progress Alt-R dictation (discarding it). Called when a
        meeting starts so the two don't share/clobber the audio device. Returns
        True if a dictation was actually running."""
        if not self.is_rec:
            return False
        try:
            self.recorder.stop_recording()
        except Exception as e:
            logger.warning("force_stop_dictation: stop_recording failed: %s", e)
        self.is_rec = False
        self._unregister_transient_keys()
        try:
            self.overlay.call_soon(self.overlay.hide_overlay)
        except Exception:
            pass
        AppController._restore_dictation_speech(self)
        return True

    def _on_enter(self):
        logger.info("[main] _on_enter fired (is_rec=%s)", self.is_rec)
        if self.is_rec and not getattr(self, "_busy", False):
            threading.Thread(target=self._stop, daemon=True).start()

    def _on_escape(self):
        logger.info("[main] _on_escape fired (is_rec=%s, busy=%s)", self.is_rec, getattr(self, "_busy", False))
        # One Esc handler for both phases: stop everything and free the app for
        # the next recording immediately - whether we're still recording or
        # already transcribing/processing.
        threading.Thread(target=self._abort_everything, daemon=True).start()

    def _abort_everything(self):
        """Esc = full stop. Invalidate any in-flight transcription job (so its
        result is discarded and can't paste), tell the recorder to bail, stop
        recording, and clear the busy/recording state right away so the very
        next hotkey starts a fresh recording with no 'still finishing' wait."""
        # The pill goes first - Esc must feel instant even while the recorder
        # winds down below.
        self.overlay.call_soon(self.overlay.hide_overlay)
        # Bumping the job sequence orphans whatever _stop_impl is doing: its
        # result is dropped and it won't re-clear _busy from under a new job.
        self._job_seq = getattr(self, "_job_seq", 0) + 1
        self._cancel_processing = True
        self.is_rec = False
        try:
            self.recorder.request_abort()   # make transcribe() return fast
        except Exception:
            pass
        try:
            self.recorder.stop_recording()  # safe/idempotent if already stopped
        except Exception:
            pass
        self._busy = False                  # immediately ready for the next take
        self._unregister_transient_keys()
        # A Smart take on its own speech model: Dictation's model comes back.
        AppController._restore_dictation_speech(self)

    def _restore_dictation_speech(self):
        """After a Smart Action that transcribed with its own speech model
        (another Whisper model, or Gusan Cloud): load Dictation's model again
        in the background, once, so the next Alt+R is not a cold load. The
        one model slot holds one Whisper model at a time."""
        if getattr(self, "_smart_speech_restore", False) is not True:
            return
        self._smart_speech_restore = False
        try:
            self._preload_speech_model()
        except Exception:
            logger.debug("Dictation's speech model did not reload", exc_info=True)

    def _idle_sweep(self):
        """Free models nobody is using: Whisper after whisper_idle_unload_min,
        a local AI model after llm_idle_unload_min (0 = keep). Skipped while a
        dictation, meeting or Live Assistance session runs; the work itself
        runs on a worker thread, so the UI never waits on a model lock. Each
        model also guards itself - nothing in use is ever freed."""
        if AppController._is_meeting_busy(self):
            return              # a pause keeps the session busy: never swept mid-meeting
        if getattr(self, "is_rec", False) or getattr(self, "_busy", False) \
                or getattr(self, "_file_job_running", False):
            return
        whisper_s = cfg_float("whisper_idle_unload_min", 10, minimum=0) * 60
        llm_s = cfg_float("llm_idle_unload_min", 5, minimum=0) * 60

        def _sweep():
            try:
                if llm_s > 0:
                    local_llm.unload_idle(llm_s)
                if whisper_s > 0:
                    self.recorder.maybe_unload_idle(whisper_s)
            except Exception:
                logger.debug("Idle model sweep failed", exc_info=True)
        threading.Thread(target=_sweep, daemon=True).start()

    def release_models_after_session(self):
        """A meeting / Live Assistance session's notes are done: free the local
        AI model now (it held ~2 GB+ until the next sweep otherwise) - unless
        a Smart Action is recording or being written right now (its writer
        stays). The next Smart take pre-warms it again (_start); it reloads in
        ~1-2 s."""
        if cfg_float("llm_idle_unload_min", 5, minimum=0) <= 0:
            return
        keep = None
        if (getattr(self, "_oneshot_action", None) == actions.ACTION_SMART_AUTO
                and (getattr(self, "is_rec", False) is True or getattr(self, "_busy", False) is True)):
            keep = actions.normalize_action_model(self.cfg.get("action_model"))

        def _free():
            try:
                local_llm.unload_idle(0, keep=keep)
            except Exception:
                logger.debug("Freeing the session's AI model failed", exc_info=True)
        threading.Thread(target=_free, daemon=True).start()

    def prewarm_speech_model(self):
        """A recording just started (meeting / Live Assistance): load the local
        Whisper model now, so the load overlaps the speech instead of holding
        up the first piece. No-op for cloud backends or a model not on disk."""
        self._preload_speech_model()

    def _preload_speech_model(self):
        """Background-load the local Whisper model when dictation uses it
        (never a download: only a model already on disk)."""
        cfg_now = self._effective_cfg()
        if cfg_now.get("backend", "local") != "local":
            return
        name = cfg_now.get("whisper_model") or "base"

        def _load():
            try:
                if model_downloaded(name):
                    self.recorder.load_model(name)
            except Exception as e:
                logger.debug("Speech model preload failed: %s", e)
        threading.Thread(target=_load, daemon=True).start()

    # ── a speech model that isn't on disk yet (model_manager) ──
    def _fetch_speech_model(self, name):
        """AudioRecorder.model_fetcher - worker threads, right before a Whisper
        model is built. A model that isn't on disk comes through
        self.downloads: ONE download with progress (the Speech & AI card,
        Home and onboarding see it, and a running one is joined) instead of
        the build fetching it silently. A stopped dictation shows "Getting
        ready" meanwhile. Raises model_manager.DownloadError (a plain sentence)."""
        dl = getattr(self, "downloads", None)
        if dl is None or threading.current_thread() is threading.main_thread():
            return                      # never block the GUI: the build decides
        try:
            if dl.kind(name) != "whisper" or model_downloaded(name):
                return                  # a custom model, or on disk (a stat): the usual case
        except Exception:
            return
        import model_manager
        failed = getattr(self, "_model_fetch_failed", None)
        if failed and failed[0] == name and time.monotonic() - failed[2] < 30:
            # Every piece of one dictation queues behind the model: one failed
            # download answers them all instead of restarting it per piece.
            raise model_manager.DownloadError(failed[1])
        self._model_wait = {"name": name, "pct": None}
        self._run_on_gui(self._show_model_wait)
        try:
            dl.fetch(name, on_progress=self._on_model_wait_progress,
                     timeout=self.SPEECH_MODEL_WAIT_S)
            self._model_fetch_failed = None
        except model_manager.DownloadError as e:
            self._model_fetch_failed = (name, str(e), time.monotonic())
            raise
        finally:
            self._model_wait = None
            self._run_on_gui(self._end_model_wait)

    def _on_model_wait_progress(self, pct, done=0, total=0):
        # Worker (the manager throttles it to ~10 a second).
        wait = getattr(self, "_model_wait", None)
        if wait is not None:
            wait["pct"] = pct
            self._run_on_gui(self._show_model_wait)

    def _show_model_wait(self):
        """GUI thread: the pill says "Getting ready · 42%" while a stopped
        dictation waits for its speech model (while recording, the recording
        pill stays - the words are kept and wait for the model)."""
        wait = getattr(self, "_model_wait", None)
        ov = getattr(self, "overlay", None)
        if wait is None or ov is None or getattr(self, "_busy", False) is not True:
            return
        from ui.overlay import GETTING_READY, TRANSCRIBING
        if getattr(ov, "overlay_state", None) in (TRANSCRIBING, GETTING_READY):
            ov.set_model_progress(wait.get("pct"))
            if ov.overlay_state != GETTING_READY:
                ov.set_state(GETTING_READY)

    def _end_model_wait(self):
        ov = getattr(self, "overlay", None)
        if ov is None:
            return
        from ui.overlay import GETTING_READY, TRANSCRIBING
        if getattr(ov, "overlay_state", None) == GETTING_READY:
            ov.set_state(TRANSCRIBING)

    def _cloud_preflight_warn(self):
        """Immediate, friendly warning if a cloud backend is selected but clearly
        misconfigured (no key). Recording still proceeds and transcription falls
        back to the local model, so a dictation is never lost. Returns (title,
        body) or None."""
        b = self.cfg.get("backend", "local")
        if b == "mistral" and not (self.cfg.get("mistral_api_key") or "").strip():
            return ("Mistral key missing", "Add it in Settings - recording with the local model for now.")
        if b == "google" and not (self.cfg.get("google_api_key") or "").strip():
            return ("Google key missing", "Add your AI Studio key in Settings - recording with the local model for now.")
        return None

    def _start(self):
        if self.is_rec:
            return
        # A file transcription owns the shared Whisper model right now -
        # starting a dictation would just hang on the inference lock until the
        # whole file is done (potentially an hour of "Finalising…").
        if getattr(self, "_file_job_running", False):
            from file_jobs import BUSY_FOR_DICTATION, BUSY_TITLE
            self.show_tray_hint(BUSY_TITLE, BUSY_FOR_DICTATION)
            return
        # Started by the Smart Actions shortcut: THIS take runs as a Smart
        # Action (every other start clears it, so an Esc-cancelled one never
        # carries over). Alt+R always pastes the exact words.
        smart = getattr(self, "_smart_next", False) is True
        # A Smart Action may transcribe with its own speech model (the Smart
        # Actions page's "Transcribes with"), fixed for the whole take.
        speech = AppController._smart_speech_for_take(self) if smart else (None, None)
        warn = None if speech[0] else self._cloud_preflight_warn()
        if warn:
            try:
                self.show_tray_hint(*warn)
            except Exception:
                pass
        try:
            self.is_rec = True
            self._oneshot_action = actions.ACTION_SMART_AUTO if smart else None
            dictation_model = self.cfg.get("whisper_model") or "base"
            # Dictation's model is loaded again after a take that used
            # another one (or the cloud): _restore_dictation_speech.
            self._smart_speech_restore = bool(
                speech[0] == "managed" or (speech[0] == "local" and speech[1] != dictation_model))
            self.overlay.set_partial("")
            from ui.overlay import RECORDING
            self.overlay.show_overlay(RECORDING, smart=smart)
            if speech[0]:
                self.recorder.start_recording(speech=speech)
            else:
                self.recorder.start_recording()
            self._register_transient_keys()

            # Load the speech model while the user speaks - also when a cloud
            # backend will run on it (no key, or it fell back within the hour).
            if speech[0] == "local":
                # The Smart Action's own Whisper model (a model that isn't on
                # disk starts, or joins, its download now - model_fetcher).
                threading.Thread(
                    target=self._load_for_dictation, args=(speech[1],), daemon=True,
                ).start()
            else:
                fell_back = time.monotonic() - getattr(self.recorder, "_local_fallback_at", -1e9) < 3600
                cloud_take = speech[0] == "managed"
                local_soon = (fell_back or (not cloud_take and self._cloud_preflight_warn())) \
                    and model_downloaded(dictation_model)
                if (not cloud_take and self.cfg["backend"] == "local") or local_soon:
                    # A model that isn't on disk starts (or joins) its download now
                    # (model_fetcher), while the user speaks.
                    threading.Thread(
                        target=self._load_for_dictation,
                        daemon=True,
                    ).start()

            # A Smart Action with a local LLM: pre-warm the writer into RAM
            # while the user speaks, so after the stop it is just inference
            # time (no cold-load wait).
            if smart:
                action_model = actions.normalize_action_model(self.cfg.get("action_model"))
                if action_model in local_llm.MODEL_CATALOG and local_llm.model_downloaded(action_model):
                    def _prewarm():
                        try:
                            local_llm._load_model(action_model)
                        except Exception as exc:
                            logger.debug("Action LLM pre-warm failed: %s", exc)
                    threading.Thread(target=_prewarm, daemon=True).start()
        except Exception as e:
            self.is_rec = False
            self._unregister_transient_keys()
            self.overlay.show_error(f"Microphone error: {e}")

    def _load_for_dictation(self, name=None):
        """Worker: load the speech model while the user speaks (``name``: a
        Smart Action's own Whisper model; None = Dictation's). A failure is
        only logged here - the take's own pieces report it."""
        try:
            self.recorder.load_model(name)
        except Exception as e:
            logger.info("Speech model not ready for this dictation: %s", e)

    def _smart_speech_for_take(self):
        """(backend, model) a Smart Action transcribes with - cfg
        smart_actions_speech_model through engine_choice.smart_speech_session
        ("cloud" only for Pro and never in Privacy mode) - or (None, None):
        like Dictation."""
        if not str(self.cfg.get("smart_actions_speech_model") or "").strip():
            return (None, None)
        try:
            import engine_choice
            backend, model = engine_choice.smart_speech_session(self.cfg, self.is_pro())
        except Exception:
            logger.debug("Smart Actions speech model ignored", exc_info=True)
            return (None, None)
        return (backend, model) if backend else (None, None)

    def _cancel(self):
        self.recorder.stop_recording()
        self.is_rec = False
        self._unregister_transient_keys()
        self.overlay.call_soon(self.overlay.hide_overlay)

    def _register_transient_keys(self):
        # Enter (stop) and Esc (cancel) are only meaningful while recording.
        # Use a pynput Listener - we confirmed in the logs that pynput reliably
        # receives plain Enter/Esc events on Windows, while the `keyboard`
        # library's on_press_key callback wasn't firing for those keys when
        # another hotkey was already registered with add_hotkey.
        self._unregister_transient_keys()
        try:
            from pynput import keyboard as pynput_keyboard
            hotkeys.prepare_pynput_listeners()

            def _on_press(key):
                try:
                    if key == pynput_keyboard.Key.enter:
                        logger.info("[hook] Enter pressed, is_rec=%s", self.is_rec)
                        if self.is_rec:
                            self.sig_enter.emit()
                    elif key == pynput_keyboard.Key.esc:
                        logger.info("[hook] Esc pressed, is_rec=%s", self.is_rec)
                        # Esc cancels while recording AND while transcribing/
                        # processing, so a long transcription can be aborted.
                        if self.is_rec or getattr(self, "_busy", False):
                            self.sig_escape.emit()
                except Exception:
                    pass

            listener = pynput_keyboard.Listener(on_press=_on_press)
            listener.daemon = True
            listener.start()
            self._transient_kbd_handles.append(listener)
            logger.info("Registered transient Enter/Esc listener")
        except Exception as e:
            logger.warning("Could not register enter/esc listener: %s", e)

    def _unregister_transient_keys(self):
        for h in self._transient_kbd_handles:
            try:
                h.stop()
            except Exception:
                pass
        self._transient_kbd_handles = []

    def _effective_cfg(self):
        """The config governing THIS dictation.

        Today simply the global config. It exists as a single interception point
        so a future per-app profile system can override settings in one place
        rather than having to find every self.cfg read in the dictation path.
        """
        return self.cfg

    def _postprocess_transcript(self, text):
        """Output-quality pass (see text_cleanup). Extracted so it can be tested
        without standing up the recorder, overlay and telemetry."""
        if not text:
            return ""
        cfg = self._effective_cfg()
        try:
            opts = text_cleanup.options_from_config(cfg)
            if not cfg.get("cleanup_enabled", True):
                # Master switch off: nothing is second-guessed about what the
                # recognizer heard. The user's own replacement dictionary still
                # applies - the UI calls it "always replace these words", and a
                # deliberate correction isn't a cleanup heuristic.
                opts = text_cleanup.CleanupOptions(
                    strip_hallucinations=False, strip_artifacts=False,
                    collapse_repeats=False, remove_fillers=False,
                    replacements=opts.replacements)
            cleaned, report = text_cleanup.clean_with_report(text, options=opts)
            if any(report.values()):
                # Counts only - never transcript text.
                logger.debug("text cleanup: %s", report)
            return cleaned
        except Exception:
            # A regex bug must never cost the user a dictation.
            logger.debug("text cleanup failed; using raw transcript", exc_info=True)
            return text

    @staticmethod
    def should_restore_clipboard(pasted, restore_enabled, prev_was_text, still_ours):
        """Whether to put the user's previous clipboard back after a paste.

        Pure so the rules are testable:
        - only when the feature is on,
        - only if we actually pasted (otherwise the text is intentionally left
          on the clipboard for the user to paste by hand),
        - only if the previous content was text (never clear an image/file
          clipboard to an empty string - worse than leaving our text),
        - only if nothing else has changed the clipboard since.
        """
        return bool(restore_enabled and pasted and prev_was_text and still_ours)

    @staticmethod
    def _wait_keys_released(max_s=0.35):
        """Paste once the keys that stopped the dictation are up - a held Alt
        or Ctrl would turn Ctrl+V into another shortcut. By the time the text
        is ready they almost always are, so this returns at once (it used to
        always sleep 350 ms)."""
        if sys.platform != "win32":
            time.sleep(max_s)
            return
        try:
            state = ctypes.windll.user32.GetAsyncKeyState
            keys = (0x10, 0x11, 0x12, 0x5B, 0x5C, 0x0D, 0x1B)  # Shift Ctrl Alt Win Win Enter Esc
            deadline = time.monotonic() + max_s
            while any(state(k) & 0x8000 for k in keys) and time.monotonic() < deadline:
                time.sleep(0.015)
        except Exception:
            time.sleep(max_s)

    @staticmethod
    def _rich_copy(text):
        """A Smart Action's result on the clipboard: Markdown with markup goes
        as HTML (Word, Outlook, Gmail, Docs, Notion, Slack, Teams get real
        lists, bold and headings) AND plain text with nice characters
        (rich_text.to_plain: "• point", "☐ task", no ** # markers). Returns
        the plain text actually put there, or None (no markup: copy as is)."""
        import rich_text
        if not rich_text.has_markup(text):
            return None
        plain, html = rich_text.to_plain(text), rich_text.to_html(text)
        ok = False
        try:
            from ui.components import clipboard as rich_clipboard
            # From this worker it hops to the GUI thread (Qt writes the
            # clipboard there) and waits a moment.
            ok = rich_clipboard.set_rich_blocking(plain, html)
        except Exception:
            logger.warning("Rich paste unavailable; pasting plain text", exc_info=True)
        if not ok:
            pyperclip.copy(plain)
        return plain

    def _paste_via_clipboard(self, text, restore=True, rich=False):
        """Copy `text`, send the paste chord, then restore the prior clipboard.

        ``rich``: a Smart Action's Markdown, pasted as HTML + nice plain text
        (_rich_copy). Plain dictation (exact words) is never changed.

        Returns True if the paste chord was sent. The restore is deliberately
        delayed: the target app reads the clipboard asynchronously after Ctrl+V,
        so restoring immediately would race and paste the OLD content.
        """
        cfg = self._effective_cfg()
        prev, prev_was_text = None, False
        if restore and cfg.get("restore_clipboard", True):
            try:
                prev = pyperclip.paste()
                prev_was_text = isinstance(prev, str) and prev != ""
            except Exception:
                prev, prev_was_text = None, False

        # What is on the clipboard now: the restore below puts the user's
        # back only while it is still ours.
        put = None
        if rich:
            try:
                put = AppController._rich_copy(text)
            except Exception:
                logger.warning("Rich paste failed; pasting the text as is", exc_info=True)
                put = None
        if put is None:
            put = text
            pyperclip.copy(text)

        pasted = False
        try:
            from pynput.keyboard import Key
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            self.kbd.press(mod); self.kbd.press("v")
            self.kbd.release("v"); self.kbd.release(mod)
            pasted = True
        except Exception as e:
            logger.warning("[Paste] %s", e)

        if self.should_restore_clipboard(
                pasted, cfg.get("restore_clipboard", True), prev_was_text, True):
            def _restore():
                try:
                    delay = int(cfg.get("clipboard_restore_delay_ms", 400))
                except (TypeError, ValueError):
                    delay = 400
                time.sleep(max(0, min(delay, 5000)) / 1000.0)
                try:
                    # Someone else may have taken the clipboard meanwhile.
                    # (Qt writes Windows text with \r\n line ends.)
                    now = pyperclip.paste()
                    if isinstance(now, str) and now.replace("\r\n", "\n") == put.replace("\r\n", "\n"):
                        pyperclip.copy(prev)
                except Exception:
                    pass
            threading.Thread(target=_restore, daemon=True).start()
        return pasted

    def _resolve_action_engine(self, action_model=None):
        """Resolve (model, config) for a Smart Action / meeting-notes run,
        applying Pro routing: a signed-in Pro user runs through the managed cloud
        (our key, no BYO key needed) unless they deliberately picked a local LLM
        or a cloud engine with their OWN key. Shared by dictation and meetings so
        both honor a Pro subscription identically (meetings used to skip this,
        which is why Pro users were wrongly asked for an action API key)."""
        action_model = actions.normalize_action_model(
            action_model if action_model is not None
            else self.cfg.get("action_model", actions.RULE_BASED_ID))
        action_config = self.cfg
        if self.is_pro():
            token = None
            try:
                token = self.auth.get_access_token()
            except Exception:
                token = None
            if token:
                action_config = {**self.cfg, "_managed_token": token}
                m_kind = actions.ACTION_MODELS.get(action_model, {}).get("kind")
                has_own_cloud_key = bool(
                    ((self.cfg.get("action_api_key") or "") or (self.cfg.get("google_api_key") or "")).strip())
                if m_kind in ("rules", "managed") or (m_kind == "cloud" and not has_own_cloud_key):
                    action_model = actions.API_MANAGED_ID
        return action_model, action_config

    def _stop(self):
        # _busy spans the whole transcribe→action→paste pipeline so a second
        # hotkey press can't re-enter and clobber the shared recorder mid-flight
        # (a real crash source). Each run gets a job id; Esc bumps it to orphan
        # this run, and only the still-current job is allowed to clear _busy.
        self._job_seq = getattr(self, "_job_seq", 0) + 1
        job = self._job_seq
        self._busy = True
        self._cancel_processing = False
        try:
            self._stop_impl(job)
        finally:
            if job == self._job_seq:  # not superseded by an Esc/new recording
                self._busy = False
                self._unregister_transient_keys()
                # A Smart take on its own speech model: Dictation's model
                # comes back now that the words are in.
                AppController._restore_dictation_speech(self)

    def _stop_impl(self, job):
        # This take's own action (the Smart Actions shortcut), read now: a
        # later start resets it. Alt+R always pastes the exact words.
        oneshot = getattr(self, "_oneshot_action", None)
        if oneshot != actions.ACTION_SMART_AUTO:
            oneshot = None
        # What THIS take transcribes with (a Smart Action may use its own;
        # a cloud pick stays on this computer once Privacy mode is on, as
        # AudioRecorder._backend decides).
        rec_backend = getattr(self.recorder, "backend_override", None)
        if not (isinstance(rec_backend, str) and rec_backend) or (
                rec_backend != "local" and self.cfg.get("privacy_mode")):
            rec_backend = None
        used_backend = rec_backend or (self.cfg.get("backend") or "local")
        rec_model = getattr(self.recorder, "model_override", None)
        used_model = (rec_model if isinstance(rec_model, str) and rec_model
                      else self.cfg.get("whisper_model"))
        self.recorder.stop_recording()
        # Recording is finished now - flip is_rec immediately so Esc during the
        # transcription below is treated as "cancel processing", and a stale
        # is_rec can't make the next hotkey think we're still recording.
        self.is_rec = False
        # NOTE: the Esc/Enter listener is intentionally left running here so Esc
        # can abort the transcription below; it's torn down in _stop's finally.
        from ui.overlay import TRANSCRIBING
        # The pill names what transcribes this take ("Whisper Small").
        self.overlay.call_soon(self.overlay.set_speech,
                               pill_copy.speech_info(used_backend, used_model, self.cfg))
        self.overlay.call_soon(self.overlay.set_state, TRANSCRIBING)
        # Still downloading the speech model: "Getting ready" instead.
        self.overlay.call_soon(self._show_model_wait)

        TRANSCRIBE_TIMEOUT_SEC = 120
        _result = {}
        def _do_transcribe():
            try:
                _result["text"], _result["lang"] = self.recorder.transcribe()
            except Exception as e:
                _result["exc"] = e

        t = threading.Thread(target=_do_transcribe, daemon=True)
        t.start()
        # Poll instead of a blocking join so Esc can cancel mid-transcription.
        waited = 0.0
        while t.is_alive() and waited < TRANSCRIBE_TIMEOUT_SEC:
            if job != self._job_seq:
                # Esc/new recording superseded this run - drop it silently
                # (_abort_everything already reset state + hid the overlay).
                logger.info("[main] transcription job %s cancelled", job)
                return
            t.join(timeout=0.1)
            # Waiting for the speech model's download doesn't count: the
            # words wait for it ("Getting ready"), they aren't dropped.
            if not getattr(self.recorder, "_fetching_model", False):
                waited += 0.1
        if (not t.is_alive() and "exc" not in _result
                and not str(_result.get("lang") or "").startswith("!")):
            # Minus a cold model load: the GPU wouldn't save that part.
            self._note_local_wait(
                "dictation", max(0.0, waited - getattr(self.recorder, "_last_load_wait", 0.0)),
                model=used_model, backend=used_backend)

        # If the managed cloud turned this one down, we transparently used the
        # local model - tell the user once so the switch isn't a mystery. Only
        # when the computer really produced the text: a failed fallback shows
        # its own error.
        capped = getattr(self.recorder, "_cloud_capped", False)
        self.recorder._cloud_capped = False
        if (capped and not t.is_alive() and "exc" not in _result
                and (_result.get("text") or "").strip()
                and not str(_result.get("lang") or "").startswith("!")):
            self.overlay.call_soon(
                self.show_tray_hint,
                "Transcribed on this computer",
                "Cloud transcription wasn't available, so your words were transcribed "
                "on this computer.",
            )

        if t.is_alive():
            telemetry.track(
                "action_failed",
                {"action": "transcribe", "reason": f"timeout_{TRANSCRIBE_TIMEOUT_SEC}s"},
                self.cfg, APP_VERSION,
            )
            self.overlay.call_soon(
                self.overlay.show_error,
                f"Transcription timed out after {TRANSCRIBE_TIMEOUT_SEC}s - try a shorter clip or a smaller model.",
            )
            return
            
        if "exc" in _result:
            self.overlay.call_soon(
                self.overlay.show_error,
                _friendly_transcription_error(_result["exc"])[:120],
            )
            return

        # Esc pressed while the transcription was finishing - discard it.
        if job != self._job_seq:
            return

        raw_text = _result.get("text", "")
        lang = _result.get("lang", "")

        # Output-quality pass. Deliberately here and nowhere else: this is the
        # dictation-only path (meetings call the recorder directly), so cleanup
        # can never perturb the meeting/diarization pipeline.
        text = self._postprocess_transcript(raw_text)

        if raw_text and not text:
            # Cleanup consumed the whole transcript, i.e. it was pure
            # hallucination/noise. Say so rather than vanishing silently.
            self.overlay.call_soon(
                self.overlay.show_error, "Only background noise was detected.")
            return

        if not text:
            if lang and lang.startswith("!"):
                err_msg = lang[1:]
                if ":" in err_msg:
                    err_msg = err_msg.split(":", 1)[1]
                self.overlay.call_soon(self.overlay.show_error, err_msg)
            else:
                self.overlay.call_soon(self.overlay.hide_overlay)
            return

        # Only a take started by the Smart Actions shortcut runs as one: Alt+R
        # always pastes the exact words (output_action is never read).
        action_mode = oneshot or actions.ACTION_TRANSCRIBE_ONLY
        output_text = text

        # Smart Actions: Pro = unlimited; everyone else gets 5 free tries. The
        # shortcut already checked at the press (_on_smart_hotkey); this is
        # the backstop for tries that ran out meanwhile - then the exact words
        # are pasted. A try is counted quietly (the counter is on the Smart
        # Actions page).
        if action_mode != actions.ACTION_TRANSCRIBE_ONLY and not self.is_pro():
            if entitlements.can_use_smart_action(self.auth, self.cfg):
                entitlements.add_smart_action_use(self.auth)
                # Pages redraw the tries counter (Smart Actions, Account & plan).
                self.sig_auth_changed.emit()
            else:
                action_mode = actions.ACTION_TRANSCRIBE_ONLY
                if entitlements.tier(self.auth, self.cfg) == entitlements.TIER_GUEST:
                    self.overlay.call_soon(
                        self.show_tray_hint,
                        "Sign up free for 5 Smart Actions",
                        "Pasted your exact words.",
                    )
                else:
                    self.overlay.call_soon(
                        self.show_tray_hint,
                        "Free Smart Actions used up",
                        "Pasted your exact words. Pro has unlimited Smart Actions.",
                    )

        pill_kind = None
        if action_mode != actions.ACTION_TRANSCRIBE_ONLY:
            # The pill says what is being written while the smart action runs
            # ("Writing your email · Gusan Cloud"): a guess from the words
            # (regexes), never a wait.
            pill_kind, pill_detail, pill_info = pill_copy.task(text, self)
            self.overlay.call_soon(self.overlay.set_task, pill_kind, pill_detail, pill_info)
            from ui.overlay import PROCESSING
            self.overlay.call_soon(self.overlay.set_state, PROCESSING)
            # Pick the engine + inject the auth token for Pro managed actions.
            action_model, action_config = self._resolve_action_engine()
            try:
                output_text = actions.process(
                    text,
                    action_mode,
                    source_lang=lang,
                    target_lang=self.cfg.get("translate_target", "en"),
                    model=action_model,
                    config=action_config,
                )
                telemetry.track(
                    "action_completed",
                    {
                        "action": action_mode,
                        "model": action_model,
                        "language": lang,
                        "output_length_bucket": _bucket_count(len(output_text)),
                    },
                    self.cfg,
                    APP_VERSION,
                )
            except actions.ActionError as e:
                telemetry.track(
                    "action_failed",
                    {"action": action_mode, "reason": str(e)[:60]},
                    self.cfg,
                    APP_VERSION,
                )
                self.overlay.call_soon(self.overlay.show_error, str(e))
                return

        # Esc during the Smart Action / "Thinking…" phase: don't paste or save.
        if job != self._job_seq:
            return

        if self.cfg.get("save_history", True) and not self.cfg.get("privacy_mode", False):
            # A Smart Action keeps what was said and which action ran, so Home
            # and Smart Actions can show "said -> pasted".
            # The raw Markdown is kept (pages render it); the backend is the
            # one that really transcribed this take.
            extra = ({"action": action_mode, "said": text}
                     if action_mode != actions.ACTION_TRANSCRIBE_ONLY else {})
            hist.save_entry(output_text, lang, used_backend, **extra)
            self.notify_history_changed()

        telemetry.track(
            "transcription_completed",
            {
                "backend": used_backend,
                "language": lang,
                "action": action_mode,
                "input_length_bucket": _bucket_count(len(text)),
                "output_length_bucket": _bucket_count(len(output_text)),
            },
            self.cfg,
            APP_VERSION,
        )

        self._wait_keys_released(0.35)
        if action_mode != actions.ACTION_TRANSCRIBE_ONLY:
            # A Smart Action's Markdown: real lists / bold / headings where
            # the app takes HTML, nice plain text everywhere else.
            pasted = self._paste_via_clipboard(output_text, rich=True)
            self.overlay.call_soon(self.overlay.show_done, pasted, kind=pill_kind)  # "Email pasted"
        else:
            pasted = self._paste_via_clipboard(output_text)   # the exact words, untouched
            self.overlay.call_soon(self.overlay.show_done, pasted)

        # A pending update: one quiet notice a run (not after every dictation),
        # shown after the "done" pill - the next dictation never waits for it.
        self._nudge_update_after_dictation()

    def paste_text(self, text, rich=False):
        # Triggers cursor paste for clicked history logs. ``rich``: a Smart
        # Action's Markdown (pasted as HTML + nice plain text).
        if rich:
            return self._paste_via_clipboard(text, rich=True)
        return self._paste_via_clipboard(text)

    def _quit_question(self):
        """What quitting now would end, as one plain sentence ("" = nothing):
        a meeting or Live Assistant session recording or writing its notes, or
        a file being converted."""
        s = getattr(self, "meeting_session", None)
        if s is not None and getattr(s, "is_busy", False):
            if getattr(s, "state", None) == "recording":
                return ("A meeting is still recording. If you quit, it stops and is saved. "
                        f"Its notes are written the next time you open {APP_NAME}.")
            if getattr(s, "_rewrite_only", False) is True and (getattr(s, "notes", "") or "").strip():
                # A rewrite of notes it has: save_for_quit leaves those as they were.
                return ("Your meeting notes are being rewritten. If you quit, the earlier "
                        "notes are kept.")
            return ("Your meeting notes are still being written. If you quit, they're "
                    f"written the next time you open {APP_NAME}.")
        if getattr(self, "_file_job_running", False) is True:
            return "A file is still being converted to text. If you quit, it stops."
        return ""

    def _confirm_quit(self):
        """Ask before quitting ends a recording, its notes or a file job.
        True = go ahead."""
        text = AppController._quit_question(self)
        if not text:
            return True
        box = QMessageBox(self._dialog_parent())
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(f"Quit {APP_NAME}?")
        box.setText(text)
        quit_btn = box.addButton("Quit", QMessageBox.DestructiveRole)
        stay_btn = box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(stay_btn)
        box.setEscapeButton(stay_btn)
        box.exec()
        return box.clickedButton() is quit_btn

    def _stop_mic_monitor_for_quit(self):
        mm = getattr(self, "mic_monitor", None)
        if mm is None:
            return
        stop_now = getattr(mm, "stop_now", None)
        if callable(stop_now):
            stop_now()
        else:
            mm.stop()

    def _stop_recording_for_quit(self):
        """A recording still running at Quit (or a log-off). A meeting (or
        Live Assistant session) is saved the way Stop saves it first - My
        notes, the recording, the transcript so far - and marked so its notes
        are written at the next launch (MeetingSession.save_for_quit); its
        notes being written get the same mark. A dictation is dropped. Either
        way the record thread is stopped so it closes its OWN streams:
        recorder.shutdown() closing them from this thread mid-read is a native
        PortAudio crash. A second call finds nothing left to do."""
        if getattr(self, "_recorder_down", False):
            return                          # quit_app already ended PortAudio
        s = getattr(self, "meeting_session", None)
        if s is not None and getattr(s, "is_busy", False):
            save = getattr(s, "save_for_quit", None)
            if callable(save):
                try:
                    save()
                except Exception:
                    logger.warning("Saving the meeting at quit failed", exc_info=True)
        rec = getattr(self, "recorder", None)
        if rec is None or not getattr(rec, "recording", False):
            return
        try:
            rec.request_abort()             # no post-roll: the thread ends at its next read
        except Exception:
            pass
        try:
            rec.stop_recording()            # joins the record thread (at most 5 s)
        except Exception:
            logger.debug("stopping the recording at quit failed", exc_info=True)

    def quit_app(self):
        """Tray Quit (and the end of an update install, Cmd+Q on a Mac, the
        updater's sig_quit). Asks first when it would end a meeting, its notes
        or a file job (False = the user kept the app running). A meeting is
        saved before anything else shuts down, and its notes are written at
        the next launch."""
        if getattr(self, "_quitting", False):
            return True
        if getattr(self, "_quit_asking", False):
            return False                    # the question is already on screen
        self._quit_asking = True
        try:
            go = self._confirm_quit()
        finally:
            self._quit_asking = False
        if not go:
            return False
        self._quitting = True
        # Services first: a queued file job is cancelled (its worker stops at
        # the next check), the calendar's timers and a waiting sign-in end,
        # the mic check closes - on this thread, before PortAudio goes below.
        for stop in (lambda: self.file_jobs.shutdown(),
                     lambda: self.calendar is not None and self.calendar.stop(),
                     self._stop_mic_monitor_for_quit):
            try:
                stop()
            except Exception:
                logger.debug("service shutdown failed", exc_info=True)
        # A recording still running: the meeting is saved and the record
        # thread stops on its own - before an installer starts, and before
        # PortAudio goes.
        self._stop_recording_for_quit()
        # "Install updates when I quit": start the installer fetched ahead.
        try:
            self._run_update_on_quit()
        except Exception:
            logger.warning("Update on quit failed", exc_info=True)
        # A setting changed in the last 250 ms may still be waiting to be saved.
        self._flush_pending_save()
        # Graceful shutdown of system hooks
        self._unregister_transient_keys()
        self._unregister_kbd_hotkey()
        self._unregister_mouse_listener()
        self._release_smart_hotkey()
        self._recorder_down = True
        self.recorder.shutdown()
        self.qapp.quit()
        return True

    def _on_quit_event(self):
        """GusanApplication: Qt is about to quit (QEvent.Quit). True lets it.
        Our own quit (quit_app's qapp.quit) and a log-off already in progress
        pass; anything else - macOS Cmd+Q, the app menu's or the Dock's Quit -
        goes through quit_app on the next loop pass (it may ask first)."""
        if getattr(self, "_quitting", False) or getattr(self, "_session_ending", False):
            return True
        QTimer.singleShot(0, self.quit_app)
        return False

    def _on_session_ending(self, manager=None):
        """commitDataRequest: Windows is logging off or shutting down (the
        process ends soon after). No question may be asked: a meeting being
        recorded is saved (notes at the next launch), and settings too."""
        self._session_ending = True
        logger.info("Session ending: saving work")
        try:
            self._stop_recording_for_quit()
        except Exception:
            logger.warning("Saving work at log-off failed", exc_info=True)
        self._flush_pending_save()

    def _on_about_to_quit(self):
        """aboutToQuit - after quit_app, or on its own at a Windows log-off /
        shutdown: a meeting still recording is saved (once), then a pending
        settings save is written."""
        if not getattr(self, "_quitting", False):
            try:
                self._stop_recording_for_quit()
            except Exception:
                logger.warning("Saving work at exit failed", exc_info=True)
        self._flush_pending_save()

    # ── services' results (GUI thread) ──
    def _page_on_screen(self, route):
        """The Gusan window is on screen, showing ``route``."""
        mw = getattr(self, "main_win", None)
        try:
            return bool(mw is not None and mw.isVisible() and not mw.isMinimized()
                        and mw.current_route() == route)
        except Exception:
            return False

    def _on_meeting_finished(self, folder, notes, error):
        """GUI thread: the session saved the meeting (and, with notes, a
        history entry) - lists refresh, and "Notify me when notes or
        transcripts are ready" says so unless Meetings is on screen (notes of
        a Live Assistant session or a calendar recording land while you are
        elsewhere). Notes that failed get one quiet notice too ("Notes
        couldn't be written · Open"), whatever that switch says - the
        recording and transcript are kept and the notes view offers to try
        again. Then the next meeting whose notes a Quit left for later. First
        of all, with "Send every meeting's notes to Notion automatically" on,
        the notes go to Notion (_maybe_auto_send_notion)."""
        self.notify_history_changed()
        AppController._maybe_auto_send_notion(self, folder, notes, error)
        if getattr(self, "_pending_notes_queue", None) and not getattr(self, "_quitting", False):
            QTimer.singleShot(2000, self._next_pending_notes)
        if getattr(self, "_quitting", False):
            return
        failed = bool(error) or not (notes or "").strip()
        if failed and not folder:
            return
        if not failed and not self.cfg.get("notify_when_ready", True):
            return
        if AppController._page_on_screen(self, "meetings"):
            return
        s = getattr(self, "meeting_session", None)
        title = str(getattr(s, "_meeting_title", "") or "").strip() or "Your meeting"
        open_it = ((lambda f=folder: self.show_main("meetings", meeting=f)) if folder
                   else (lambda: self.show_main("meetings")))
        if failed:
            self.show_tray_hint("Notes couldn't be written", f"{title} · Open", on_click=open_it)
            return
        self.show_tray_hint("Notes ready", title, on_click=open_it)

    def _maybe_auto_send_notion(self, folder, notes, error):
        """GUI thread, first thing when a meeting finished: with "Send every
        meeting's notes to Notion automatically" on (notion_auto_send, off
        until the user turns it on) and Notion connected, a recorded meeting's
        notes go to Notion - the notes only, never the transcript, and only its
        FIRST notes: a rewrite, a Live Assistant session and a meeting already
        in Notion are left alone (the worker checks the last two against the
        saved meeting). The send runs on a worker thread; its answer comes to
        _notion_auto_done. Never raises: Notion can't cost a meeting its notes."""
        try:
            if not (getattr(self, "cfg", None) or {}).get("notion_auto_send"):
                return
            if (getattr(self, "_quitting", False) or error or not folder
                    or not (notes or "").strip()):
                return
            s = getattr(self, "meeting_session", None)
            if getattr(s, "last_notes_new", False) is not True:
                return                          # a rewrite of notes it had
            import notion_export
            if not notion_export.is_connected(self.cfg):
                return
            if self.cfg.get("notion_reconnect"):
                return                          # Notion refused the token for good: the failure that
                #                                 set the flag had its hint, Settings says "Reconnect"
            title = str(getattr(s, "_meeting_title", "") or "").strip() or "Your meeting"
            threading.Thread(target=self._auto_send_notion,
                             args=(folder, title, dict(self.cfg)),
                             daemon=True, name="gusan-notion-auto").start()
        except Exception:
            logger.warning("Sending the notes to Notion automatically failed", exc_info=True)

    def _auto_send_notion(self, folder, title, cfg):
        """Worker thread: the automatic Notion send (_maybe_auto_send_notion);
        ``cfg`` is the copy taken when the meeting finished. Privacy mode sends
        nothing and asks nothing: meta says why, and the Meetings settings row
        reads "Paused while Privacy mode is on"."""
        res = {"ok": False, "url": "", "code": "other", "sentence": ""}
        try:
            import meeting_store
            import notion_sync
            meta = meeting_store.load_meta(folder)
            if meeting_store.is_live_session(meta) or meta.get("notion_url"):
                return                          # never auto-sent / already in Notion
            if cfg.get("privacy_mode"):
                meeting_store.update_meta(folder, notion_error="privacy")
                res["code"] = "privacy"
            else:
                res = notion_sync.send_meeting(
                    folder, cfg, auto=True,
                    persist=lambda d: self._run_on_gui(self.set_settings, d))
        except Exception:
            logger.warning("Sending the notes to Notion automatically failed", exc_info=True)
        self._run_on_gui(self._notion_auto_done, folder, title, res)

    def _notion_auto_done(self, folder, title, res):
        """GUI thread: the automatic Notion send ended. A failure always gets
        one calm tray hint ("Couldn't send to Notion · Open"), whatever "Notify
        me when ready" says; a success is quiet while Meetings is on screen and
        otherwise says "Sent to Notion" only with that switch on. A privacy
        skip, a meeting already in Notion and a meeting another send already
        has in hand, say nothing."""
        try:
            import notion_export
            ok = bool(res.get("ok"))
            code = str(res.get("code") or "")
            if code == "busy" or (code == "sent" and not ok):
                return
            mode = notion_export.connection_mode(self.cfg) or "own"
            props = {"target": "notion", "ok": ok, "auto": True, "mode": mode}
            if not ok:
                props["reason"] = code if code in (
                    "privacy", "auth", "no_access", "rate", "offline", "full") else "other"
            self.track("meeting_exported", props)
            self.notify_history_changed()            # the meeting's meta changed
            if getattr(self, "_quitting", False):
                return
            if not ok and code == "auth" and mode in notion_export.GRANT_MODES \
                    and not self.cfg.get("notion_reconnect"):
                self.set_setting("notion_reconnect", True)     # the rail says "Reconnect"
            if not ok and code == "privacy":
                return
            if ok and (not self.cfg.get("notify_when_ready", True)
                       or AppController._page_on_screen(self, "meetings")):
                return
            open_it = lambda f=folder: self.show_main("meetings", meeting=f)
            if ok:
                self.show_tray_hint("Sent to Notion", title, on_click=open_it)
            else:
                # Say what to do when that is known; else which meeting it was.
                why = {"auth": ("Reconnect it in Meeting settings" if mode in notion_export.GRANT_MODES
                                else "Notion rejected the secret"),
                       "offline": "Check your internet",
                       "full": notion_export.FULL_HINT,
                       "no_access": ("Reconnect it in Meeting settings" if mode == "mcp"
                                     else "Pick another page in Meeting settings")}.get(code)
                self.show_tray_hint("Couldn't send to Notion", f"{why or title} · Open", on_click=open_it)
        except Exception:
            logger.warning("Showing the Notion send result failed", exc_info=True)

    def _write_pending_notes(self, tries=0):
        """Startup (+20 s): meetings whose notes a Quit or a log-off left for
        later (meta notes_pending) get them now - one at a time, only while
        nothing records or converts (else it tries again a minute later). A
        dictation may run meanwhile: writing notes from a saved transcript
        doesn't use the recorder (MeetingSession.holds_recorder)."""
        if getattr(self, "_quitting", False):
            return
        s = getattr(self, "meeting_session", None)
        if s is None or not callable(getattr(s, "write_pending_notes", None)):
            return
        if (self.is_rec or getattr(self, "_busy", False) is True
                or getattr(self, "_file_job_running", False) is True or s.is_busy):
            if tries < PENDING_NOTES_MAX_TRIES:
                QTimer.singleShot(PENDING_NOTES_RETRY_MS,
                                  lambda t=tries + 1: self._write_pending_notes(t))
            return

        def _scan():
            try:
                import meeting_session
                folders = meeting_session.pending_notes_folders()
            except Exception:
                logger.debug("Looking for unwritten meeting notes failed", exc_info=True)
                folders = []
            if folders:
                self._run_on_gui(self._start_pending_notes, folders)
        threading.Thread(target=_scan, daemon=True, name="gusan-pending-notes").start()

    def _start_pending_notes(self, folders):
        self._pending_notes_queue = list(folders)
        self._next_pending_notes()

    def _next_pending_notes(self):
        """The next queued meeting's notes (GUI thread). Waits while the
        session is busy (a meeting started meanwhile: after it)."""
        if getattr(self, "_quitting", False):
            return
        s = getattr(self, "meeting_session", None)
        queue_ = getattr(self, "_pending_notes_queue", None) or []
        if s is None or not queue_:
            return
        if s.is_busy:
            return                       # _on_meeting_finished calls again
        while queue_:
            folder = queue_.pop(0)
            try:
                if s.write_pending_notes(folder):
                    logger.info("Writing the notes a quit left for %s", folder)
                    return
            except Exception:
                logger.warning("Writing pending meeting notes failed", exc_info=True)

    def _on_file_job_finished(self, item):
        """A file became a transcript: lists refresh, and "Notify me when notes
        or transcripts are ready" says so unless Audio to text is on screen."""
        self.notify_history_changed()
        if not self.cfg.get("notify_when_ready", True):
            return
        if AppController._page_on_screen(self, "audio_to_text"):
            return
        tid = item.get("transcript_id")
        self.show_tray_hint(
            "Transcript ready", item.get("name") or "Your file is ready.",
            on_click=(lambda: self.show_main("audio_to_text", transcript_id=tid)) if tid
            else (lambda: self.show_main("audio_to_text")))

    # ── Google Calendar (calendar_service) ──
    def _setup_calendar(self):
        """The calendar service - only in a build with a Google OAuth client
        (calendar_sync.configured; none today, so every calendar part of the
        pages stays hidden). Cheap: no network until start()."""
        try:
            import calendar_sync
            if not calendar_sync.configured(self.cfg):
                return
            from calendar_service import CalendarService
            # Privacy Mode is "everything stays on this device": no calendar
            # reads then (the cached list and its reminders still work; it
            # refetches when Privacy Mode goes off). Drop network_ok to allow them.
            cal = CalendarService(self.cfg, is_busy=self._calendar_busy, parent=self,
                                  network_ok=lambda: not self.cfg.get("privacy_mode", False))
        except Exception:
            logger.warning("Google Calendar unavailable", exc_info=True)
            return
        cal.remind.connect(self._on_calendar_remind)
        cal.disconnected.connect(self._on_calendar_disconnected)
        self.sig_setting_changed.connect(cal.on_setting_changed)
        self.calendar = cal
        try:
            cal.set_owner(getattr(self.auth, "user_id", None))
        except Exception:
            logger.debug("calendar owner check failed", exc_info=True)
        tm = getattr(self, "tray_menu", None)        # the tray's "Next:" row
        if tm is not None:
            try:
                tm.attach_calendar(cal)
            except Exception:
                logger.debug("tray calendar row unavailable", exc_info=True)
        QTimer.singleShot(3000, cal.start)          # the first fetch after startup work

    def _calendar_busy(self):
        """No "record this meeting?" now: a dictation, a meeting or Live
        Assistant session, a file job - or a reminder already showing (one at
        a time; the next due one is asked when it closes)."""
        if self.is_rec or getattr(self, "_busy", False) is True \
                or getattr(self, "_file_job_running", False) is True:
            return True
        if AppController._is_meeting_busy(self):
            return True
        toast = getattr(self, "_reminder", None)
        try:
            return bool(toast is not None and toast.is_showing())
        except RuntimeError:
            return False

    def _on_calendar_remind(self, meeting):
        """calendar_service.remind: a meeting is about to start (already marked
        asked). The "record this meeting?" toast by the tray
        (ui/floating/meeting_reminder.py) - or, if it can't show, a tray
        notification that opens Meetings, so a reminder is never lost."""
        try:
            toast = self._ensure_reminder()
            anchor = None
            try:
                anchor = self.tray_icon.geometry()
            except Exception:
                pass
            toast.show_meeting(meeting, anchor=anchor)   # False: no longer due
            return
        except Exception:
            logger.warning("The meeting reminder failed to show", exc_info=True)
        title = (meeting or {}).get("title") or "Your meeting"
        self.show_tray_hint("Record this meeting?", f"{title} is starting.",
                            on_click=lambda: self.show_main("meetings"))

    def _ensure_reminder(self):
        """The one reminder toast, built the first time a meeting is due."""
        toast = getattr(self, "_reminder", None)
        if toast is not None:
            return toast
        from ui.floating.meeting_reminder import MeetingReminder
        toast = MeetingReminder(main_app=self)
        # Record fires for Pro users only (the toast opens the Pro window for
        # Free users itself); the controller gates again anyway.
        toast.record.connect(lambda m, assist: self.record_from_calendar(m, with_assist=assist))
        toast.dismissed.connect(self._on_reminder_dismissed)
        # "Join call": the toast opens the https link itself and stays up.
        self.sig_auth_changed.connect(toast.refresh_account)
        self.sig_setting_changed.connect(toast.on_setting_changed)
        s = getattr(self, "meeting_session", None)
        if s is not None:
            # A recording started elsewhere (the page, Live Assistant) closes it.
            s.state_changed.connect(self._on_session_state_for_reminder)
        self._reminder = toast
        return toast

    def _on_session_state_for_reminder(self, state):
        toast = getattr(self, "_reminder", None)
        if toast is not None and state in ("recording", "paused"):
            try:
                toast.hide_reminder()
            except RuntimeError:
                pass

    def _on_reminder_dismissed(self, meeting):
        """"Not this one": that meeting is never asked about again (the service
        already marked it when it asked; this keeps it so)."""
        cal = getattr(self, "calendar", None)
        mid = (meeting or {}).get("id")
        if cal is not None and mid:
            try:
                cal.mark_prompted(mid)
            except Exception:
                logger.debug("calendar mark_prompted failed", exc_info=True)

    def record_from_calendar(self, meeting, with_assist=False):
        """Record a Google Calendar meeting (the reminder's Record / Record +
        Live Assistant): Pro gate, the one-time consent notice, then the
        meeting session starts with the meeting's title, people and event id
        and the Meetings page shows it live. ``with_assist`` also opens the
        Live Assistant card. Returns True when the recording started."""
        m = dict(meeting or {})
        toast = getattr(self, "_reminder", None)
        if toast is not None:
            try:
                toast.hide_reminder()
            except RuntimeError:
                pass
        if not self.is_pro():
            self._pro_upsell("Meeting recording")
            if not self.is_pro():
                return False
        title = str(m.get("title") or "").strip()

        def names(value):
            if isinstance(value, str):
                return [p.strip() for p in value.split(",") if p.strip()]
            return [str(p).strip() for p in (value or []) if str(p).strip()]
        # The session's attendees include you (the notes say who is who); the
        # start card lists the others, as the Meetings page does.
        people = names(m.get("attendees")) or names(m.get("others"))
        others = names(m.get("others")) if "others" in m else people
        event_id = str(m.get("id") or "")
        session = getattr(self, "meeting_session", None)
        if session is None:
            self.show_main("meetings", title=title, attendees=others, calendar_event_id=event_id)
            return False
        if session.is_busy:
            self.show_main("meetings")              # one at a time: the live view
            return False
        rec = getattr(self, "recorder", None)
        try:
            from ui.recording_prompts import ensure_recording_consent
            if not ensure_recording_consent(self._dialog_parent(), self):
                return False
            if with_assist:
                # The card exists before the start, so the session tells it the
                # meeting began; short speech pieces keep its answers live
                # (the notes are written from the full recording afterwards).
                self._ensure_live_assist()
                if rec is not None:
                    rec.fast_live_chunks = True
            ok, _code, msg = session.start(title=title, attendees=", ".join(people),
                                           source="calendar", calendar_event_id=event_id or None)
        except Exception as e:
            logger.warning("Recording the calendar meeting failed", exc_info=True)
            ok, msg = False, str(e)
        if not ok and with_assist and rec is not None:
            rec.fast_live_chunks = False
        if not ok:
            # The start card, filled in: Record there says what is wrong.
            self.show_main("meetings", title=title, attendees=others, calendar_event_id=event_id)
            self.show_tray_hint("Couldn't start the recording", msg or "Try again from the Meetings page.")
            return False
        self.show_main("meetings", view="live")
        if with_assist:
            self.show_live_assist()
        return True

    def _on_calendar_disconnected(self, reason):
        if reason == "expired":
            self.show_tray_hint("Google Calendar disconnected",
                                "Connect it again on the Meetings page.",
                                on_click=lambda: self.show_main("meetings"))

# ── Helpers ───────────────────────────────────────────────────────────────────
def _parse_version(v):
    try:
        parts = [int(x) for x in v.lstrip("v").split(".")[:3]]
        return tuple((parts + [0, 0, 0])[:3])
    except Exception:
        return (0, 0, 0)

def _format_bytes(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024

def _trusted_update_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc.lower() == "github.com"
    except Exception:
        return False

def _sha256_from_checksum_text(text):
    for token in (text or "").replace("\r", " ").replace("\n", " ").split():
        token = token.strip()
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token):
            return token.lower()
    return ""

def _asset_url(data, wanted_name):
    wanted = wanted_name.lower()
    for asset in data.get("assets", []):
        if asset.get("name", "").lower() == wanted:
            return asset.get("browser_download_url")
    return None

def _update_info_from_manifest(manifest, ignore_dismissed=False):
    tag = manifest.get("tag") or f"v{manifest.get('version', '')}"
    version = manifest.get("version") or tag
    if not tag or _parse_version(version) <= _parse_version(APP_VERSION):
        return None
    if not ignore_dismissed and cfg.get("dismissed_update_version") == tag:
        return None

    windows = manifest.get("windows", {})
    installer = windows.get("installer", {})
    return {
        "tag": tag,
        "installer_url": installer.get("url"),
        "checksum_url": installer.get("sha256_url"),
        "checksum": installer.get("sha256", ""),
        "body": manifest.get("notes", "") or manifest.get("changelog", ""),
        "notes_url": manifest.get("notes_url", RELEASES_URL),
        "source": "manifest",
    }

def _update_info_from_release(data, ignore_dismissed=False):
    latest = data.get("tag_name", "")
    if _parse_version(latest) <= _parse_version(APP_VERSION):
        return None
    if not ignore_dismissed and cfg.get("dismissed_update_version") == latest:
        return None

    installer_url = None
    installer_name = ""
    preferred = _asset_url(data, "TranscribeApp-Windows-Setup.exe")
    if preferred:
        installer_url = preferred
        installer_name = "transcribeapp-windows-setup.exe"
    else:
        for asset in data.get("assets", []):
            name = asset.get("name", "").lower()
            if "setup" in name and name.endswith(".exe"):
                installer_url = asset.get("browser_download_url")
                installer_name = name
                break

    checksum_url = None
    if installer_name:
        checksum_url = (
            _asset_url(data, f"{installer_name}.sha256")
            or _asset_url(data, f"{installer_name}.sha256sum")
        )
    return {
        "tag": latest,
        "installer_url": installer_url,
        "checksum_url": checksum_url,
        "checksum": "",
        "body": data.get("body", "") or f"Release notes: {data.get('html_url', RELEASES_URL)}",
        "notes_url": data.get("html_url", RELEASES_URL),
        "source": "release",
    }

def _bucket_count(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "0"
    for limit in (25, 100, 250, 500, 1000, 2500):
        if value <= limit:
            return f"<= {limit}"
    return "> 2500"


# ── Main Entrypoint ───────────────────────────────────────────────────────────
def main():
    # 1. IPC Single-Instance check
    lock_sock = acquire_single_instance_lock()
    if not lock_sock:
        # Another instance is running, wake it and exit
        action = launch_action(sys.argv)
        if action:
            signal_running_instance(action)
        sys.exit(0)

    # A GPU-acceleration removal scheduled last run (its DLLs were loaded):
    # finish it before anything looks for - or loads - the libraries.
    try:
        gpu_accel.finish_pending_remove()
    except Exception:
        pass

    # 2. Standard Qt Setup (GusanApplication: a Quit from the platform -
    # Cmd+Q on a Mac - goes through AppController.quit_app)
    qapp = GusanApplication(sys.argv)
    # The product name people see. (No QSettings / QStandardPaths use it, so
    # the data folder stays "Transcribe" - see storage.py.)
    qapp.setApplicationName(APP_NAME)
    qapp.setQuitOnLastWindowClosed(False) # Tray-resident background app constraint!
    # Gusan 2.0 fonts: bundled assets/fonts if present, else the system faces.
    try:
        from ui.theme import load_fonts
        load_fonts()
    except Exception as e:
        logging.warning("Could not load fonts: %s", e)

    # Scrolling the page must never silently rewrite a dropdown/spin-box the
    # cursor happens to pass over (Qt's default wheel behaviour).
    try:
        from ui.wheelguard import install as install_wheel_guard
        install_wheel_guard(qapp)
    except Exception as e:
        logging.warning("Could not install wheel guard: %s", e)

    # Set premium global application window icon (circular Armenian Flag + Microphone)
    try:
        qapp.setWindowIcon(make_qicon())
    except Exception as e:
        logging.error("Failed to set global window icon: %s", e)

    # Create app logic controller
    controller = AppController(qapp)

    # Start-at-login: apply the setting to the OS (first run: default it on),
    # repairing a missing or stale entry. Packaged builds only.
    try:
        if autostart.reconcile(controller.cfg):
            controller.save_config()
    except Exception as e:
        logging.warning("Could not apply the start-at-login setting: %s", e)

    # Attach lock socket IPC listener
    # A second launch (or `python main.py <action>`) hands its action to the
    # running instance - see dispatch_ipc_action.
    def _ipc_dispatch(action):
        dispatch_ipc_action(controller, action)
    # The listener runs on a plain thread with no Qt event loop, so a QTimer
    # started there never fired and a second launch did nothing: hop to the GUI
    # thread through a queued signal instead.
    controller.sig_ipc_action.connect(_ipc_dispatch, Qt.QueuedConnection)
    start_ipc_server(lock_sock, controller.sig_ipc_action.emit)

    # Open the app on every launch. The only quiet launch is the Windows-startup
    # shortcut (--background), and the first run ever belongs to the onboarding
    # wizard (which opens the app itself when it finishes). The window is built
    # here, after the tray, not inside the controller.
    if "--background" not in sys.argv and controller.cfg.get("onboarding_done", False):
        QTimer.singleShot(200, lambda: controller.show_main())

    sys.exit(qapp.exec())

if __name__ == "__main__":
    main()
