"""Armenian speech on this computer (Gusan 2.0.1): NVIDIA's FastConformer, run by sherpa-onnx.

Whisper on this computer is poor at Armenian (speech_langs.LOCAL_WHISPER_POOR). This module is a
second local speech engine for that one language: NVIDIA's "stt_hy_fastconformer_hybrid_large_pc"
(CC BY 4.0), converted to int8 for sherpa-onnx - 105 MB to download, 221 MB in memory, a 10 s piece
in about 0.9 s on a laptop CPU, 12.1 % word error rate on FLEURS Armenian (NVIDIA: 12.3 %; Whisper
large-v3: 65 %). No Qt here: Linux CI imports it, and sherpa_onnx is only imported when a model is
loaded (it is optional: without it the model is simply never listed).

PUBLIC API (everything below is thread-safe unless it says otherwise)

  Names and visibility
    MODEL_ID                    "hy-fastconformer": the config value (whisper_model,
                                smart_actions_speech_model, live_assist_speech_model, file_model)
    is_hy_model(value)          is this config value the Armenian model?
    ready()                     the model is on disk (a stat per file, no network, no import)
    has_source()                it has somewhere to be downloaded from (a URL in the code, or a
                                valid entry in the cached manifest)
    runtime_ok() / runtime_problem()   sherpa-onnx is installed and new enough (>= 1.13.5)
    listed()                    the ONE visibility rule: (ready or has_source) and runtime_ok.
                                While it is False the model is hidden everywhere: no row, no
                                card, no credit, and no code path touches the network.
    status() -> dict            {"state": "hidden" | "missing" | "ready", "listed", "ready",
                                "has_source", "runtime", "size_text", "name", "plain", "hint", "id"}
    covers(language, mix=None)  may the model transcribe this language setting? ONLY Armenian
                                ("hy"), or "multi" whose ticked languages are all Armenian.
                                "auto", every other language and any real mix: False -> Whisper.
    fallback_whisper(downloaded, device="cpu")   the Whisper id used for a language it doesn't cover
    credit() -> dict            NVIDIA's credit line and links (CC BY 4.0)
    ROW_HINT                    the one-line hint of the model's row
    SIZE_TEXT / REAL_NAME / PLAIN_NAME   "105 MB" / "NVIDIA FastConformer Armenian" / "Armenian"

  Catalog (the optional manifest) - blocking, WORKER THREADS ONLY
    refresh_catalog(force=False, privacy=False) -> bool   fetch https://gusan.aibuben.xyz/models/
                                manifest.json at most once a day (cached, even "nothing there");
                                True when the listing changed. Never at import or startup: call it
                                when a page that would list the model opens, or when the user asks
                                (model_manager.ModelDownloads.refresh_catalog does it on a worker).
                                Skipped in Privacy mode (MANIFEST_BLOCKED_IN_PRIVACY_MODE), offline
                                or on any error; never raises.

  Download / remove - blocking, WORKER THREADS ONLY (model_manager.ModelOps calls them)
    download(on_progress=None, should_cancel=None, privacy=False) -> model dir
                                on_progress(pct, done_bytes, total_bytes); resumable (Range);
                                the archive's SHA-256 is checked BEFORE it is unpacked, every
                                unpacked file against the hash pinned in this file, then one
                                atomic rename. A download is a network action like a Whisper
                                model's (not refused in Privacy mode: DOWNLOAD_BLOCKED_IN_PRIVACY_MODE).
                                Raises SpeechHyError (str(e) is one plain sentence; e.reason is a
                                short code): NotAvailable, NoRuntime, PrivacyBlocked, Cancelled,
                                DownloadFailed.
    cancel_download() / request_cancel()   stop a running download (it keeps its part file)
    remove() -> bool            unload and delete the model (raises InUse while it transcribes)

  Speech
    engine() -> Engine          the process-wide engine (lazy; nothing is loaded until used)
    transcribe(audio, language=None) -> str            16 kHz mono float32 -> text ("" = nothing)
    transcribe_segments(audio, on_progress=None, should_cancel=None) -> [{"start","end","text"}]
                                (seconds; None when cancelled) - segments come out like Whisper's:
                                a sentence or a few seconds each, cut at the model's own token times
    load() / unload() / loaded() / maybe_unload_idle(idle_sec)   the model's memory
                                (AudioRecorder.maybe_unload_idle sweeps it with the Whisper model)

THE RECIPE (tested on 100 FLEURS clips, see tests/test_speech_hy.py)
  * pieces of at most 25 s (30 s is the hard limit: single calls of 1-2 minutes lose 1.6-1.9
    points and need 1.25 GB), cut at pauses found by a voice detector, and when a stretch of
    speech is longer than that at its quietest point, the next piece starting 0.3 s earlier
    (the words heard twice are dropped when the texts are joined); a pause of a second always ends
    a piece (the model drops words after a long silence);
  * a piece whose loudest sample is below 0.2 is raised to 0.5 (the model collapses on quiet
    input: 44 % WER at a peak of 0.01) - never lowered; digital silence (peak < 1e-4) and
    anything the voice detector calls no speech is never decoded (the model makes words up from
    silence and noise); output without a single letter is dropped;
  * the model writes the word "and" as two letters; modern spelling is the ligature U+0587
    (sentence start: Եվ) - without that fix the same text scores 5 points worse;
  * greedy search, 2 threads (4 are no faster), one piece at a time.
"""
from __future__ import annotations

import gc
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field

import hardware
import storage

logger = logging.getLogger("transcribe")

# ── what the model is ───────────────────────────────────────────────────────────
MODEL_ID = hardware.HY_MODEL_ID                 # "hy-fastconformer"
KIND = "speech_hy"                              # model_manager's kind (telemetry "kind")
MANIFEST_KEY = "hy-fastconformer-int8"          # its key in the manifest's "models"
MODEL_DIR_NAME = "sherpa-onnx-nemo-hy-fastconformer-int8"
MIN_SHERPA = (1, 13, 5)                         # 1.12.26 - 1.13.4 return EMPTY text for ~13 % of clips

# WHERE IT IS HOSTED. The founder has not hosted the file yet: while this is empty (and no
# manifest entry exists) the model is hidden and nothing here touches the network. To switch it on
# WITHOUT an app update, put the archive somewhere on POLICY.file_hosts and publish
# https://gusan.aibuben.xyz/models/manifest.json:
#   {"models": {"hy-fastconformer-int8": {"url": "https://...", "sha256": "<ARMENIAN_MODEL_SHA256>",
#                                         "size": <ARMENIAN_MODEL_SIZE>, "version": "1"}}}
# (or set ARMENIAN_MODEL_URL in the next release). Either way only the exact archive built for
# Gusan can be installed: its SHA-256 and size are pinned below, and so is every file inside it.
ARMENIAN_MODEL_URL = ""
ARMENIAN_MODEL_SHA256 = "309509e3cac257c5c675bec5e8145d85ad25ca46c8e99d31f8f3fe7f9428ebf1"
ARMENIAN_MODEL_SIZE = 105_462_960               # bytes
ARMENIAN_MODEL_VERSION = "1"

# File name -> (size in bytes, SHA-256) of what the archive unpacks to (nemo_work/release/
# SHA256SUMS.txt). The archive holds one folder, MODEL_DIR_NAME, with exactly these five files.
MODEL_FILES = {
    "encoder.int8.onnx": (131_113_824, "38ca762e242295a2d57c0a0a55da628eeabb3f0b5d5309ac355aaa3385cec3c8"),
    "decoder.int8.onnx": (3_955_863, "00766e155e6236005dc16cebedac713b92dbe3d6db0b1094e2aa62bd5ef55fdb"),
    "joiner.int8.onnx": (1_408_182, "6973a71aae6e68b963eb181023e6dd4754fea4562b2fae3fa687fbb235e6ebd5"),
    "tokens.txt": (12_946, "221b1dd978a2d8f4fe8a8e185e400c09f98d71c887de9099cb2f1154d321416b"),
    "NOTICE.txt": (1_710, "46a5c5213e80b24552c39a1b6a6cfd48ddec164b1bbde1b7e648eb76bef56067"),
}
NEEDED_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt", "NOTICE.txt")
SIZE_TEXT = "105 MB"

# ── names and words ─────────────────────────────────────────────────────────────
PLAIN_NAME = hardware.HY_INFO.plain             # "Armenian"
REAL_NAME = hardware.HY_INFO.real               # "NVIDIA FastConformer Armenian"
CREDIT_URL = "https://huggingface.co/nvidia/stt_hy_fastconformer_hybrid_large_pc"
LICENSE_NAME = "CC BY 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
CREDIT_TEXT = ("Armenian speech model: NVIDIA FastConformer (CC BY 4.0), converted for Gusan. "
               "Not made or endorsed by NVIDIA.")
# The one line under the model's row. Functional, no more.
ROW_HINT = "Used when the language is Armenian. Model by NVIDIA (CC BY 4.0)."

MSG_NOT_AVAILABLE = "This model can't be downloaded right now. Try again later."
MSG_NO_RUNTIME = "This copy of Gusan can't run the Armenian model."
MSG_PRIVACY = "Privacy mode keeps Gusan offline. Turn it off to download this model."
MSG_NOT_READY = "The Armenian model isn't on this computer yet. Download it first."
MSG_UNSAFE = "The download didn't pass Gusan's safety check, so it wasn't installed. Try again later."
MSG_INCOMPLETE = "The download didn't finish. Try again."
MSG_DISK_FULL = "Not enough free disk space for this model."
MSG_NETWORK = "Couldn't reach the download server. Check your connection and try again."
MSG_SERVER = "The download server had a problem. Try again in a few minutes."
MSG_CANCELLED = "The download was stopped."
MSG_IN_USE = "The model is in use right now. Try again when it finishes."
MSG_OTHER = "The download stopped. Try again."

# Privacy mode ("everything stays on this computer"): the manifest is a lookup nobody asked for, so
# it is never fetched while Privacy mode is on. The model itself is downloaded exactly like a
# Whisper model is (main.download_whisper_model is not gated by Privacy mode: a download sends
# nothing of the user's, and the person pressed Download). Flip DOWNLOAD_BLOCKED_IN_PRIVACY_MODE to
# True to refuse the download too, as the speaker model does (meeting_speakers.R_PRIVACY).
MANIFEST_BLOCKED_IN_PRIVACY_MODE = True
DOWNLOAD_BLOCKED_IN_PRIVACY_MODE = False


class SpeechHyError(Exception):
    """A plain sentence in str(e); ``reason`` is a short code (safe for analytics)."""
    reason = "other"
    message = MSG_OTHER

    def __init__(self, message=None, reason=None):
        super().__init__(message or self.message)
        if reason:
            self.reason = reason

    @property
    def user_message(self):
        return str(self)


class NotAvailable(SpeechHyError):
    reason, message = "unavailable", MSG_NOT_AVAILABLE


class NoRuntime(NotAvailable):
    reason, message = "no_runtime", MSG_NO_RUNTIME


class NotReady(SpeechHyError):
    reason, message = "not_ready", MSG_NOT_READY


class PrivacyBlocked(SpeechHyError):
    reason, message = "privacy", MSG_PRIVACY


class Cancelled(SpeechHyError):
    reason, message = "cancelled", MSG_CANCELLED


class InUse(SpeechHyError):
    reason, message = "in_use", MSG_IN_USE


class DownloadFailed(SpeechHyError):
    reason, message = "incomplete", MSG_INCOMPLETE


# ── pins and policy (data: tests pass their own) ─────────────────────────────────
@dataclass(frozen=True)
class Pins:
    """What the model IS: its archive and every file in it. Nothing outside of this is installed."""
    dir_name: str = MODEL_DIR_NAME
    files: dict = field(default_factory=lambda: dict(MODEL_FILES))
    archive_sha256: str = ARMENIAN_MODEL_SHA256
    archive_size: int = ARMENIAN_MODEL_SIZE
    needed: tuple = NEEDED_FILES


@dataclass(frozen=True)
class Policy:
    """Where the manifest and the file may come from."""
    manifest_url: str = "https://gusan.aibuben.xyz/models/manifest.json"
    manifest_hosts: tuple = ("gusan.aibuben.xyz",)
    file_hosts: tuple = ("gusan.aibuben.xyz", "github.com", "objects.githubusercontent.com",
                         "release-assets.githubusercontent.com", "huggingface.co")
    # A download is allowed to be redirected to its host's own CDN (GitHub, Hugging Face): the
    # archive is hash-checked, so the CDN cannot change what is installed.
    redirect_suffixes: tuple = (".githubusercontent.com", ".huggingface.co", ".hf.co")
    require_https: bool = True
    manifest_max_bytes: int = 64 * 1024
    ttl_seconds: float = 24 * 3600
    timeout: float = 20.0
    max_redirects: int = 5
    proxies: bool = True


PINS = Pins()
POLICY = Policy()


def _pins(pins=None):
    return pins if pins is not None else PINS


def _policy(policy=None):
    return policy if policy is not None else POLICY


# ── where it lives ───────────────────────────────────────────────────────────────
def models_dir():
    return os.path.join(str(storage.app_data_dir()), "models")


def model_dir(pins=None):
    return os.path.join(models_dir(), _pins(pins).dir_name)


def _download_dir():
    return os.path.join(models_dir(), "hy-download")


def _manifest_cache_path():
    return os.path.join(models_dir(), "hy-manifest.json")


def is_hy_model(value):
    return str(value or "").strip() == MODEL_ID


def files_present(directory, pins=None):
    """Every needed file is there with exactly its pinned size (a stat each - cheap)."""
    pins = _pins(pins)
    for name in pins.needed:
        want = pins.files.get(name)
        try:
            size = os.path.getsize(os.path.join(directory, name))
        except OSError:
            return False
        if want is not None and size != want[0]:
            return False
    return True


def ready(pins=None):
    return files_present(model_dir(pins), pins)


# ── the runtime (sherpa-onnx, optional) ──────────────────────────────────────────
_runtime_lock = threading.Lock()
_runtime_cache = None


def _parse_version(text):
    nums = re.findall(r"\d+", str(text or ""))[:3]
    return tuple(int(n) for n in nums) if nums else None


def _detect_runtime():
    """("ok" | "missing" | "old", version text). Cheap: no import of sherpa_onnx itself."""
    import importlib.util
    try:
        spec = importlib.util.find_spec("sherpa_onnx")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return "missing", ""
    version = ""
    try:
        from importlib import metadata
        version = metadata.version("sherpa-onnx")
    except Exception:
        version = ""
    parsed = _parse_version(version)
    if parsed is not None and parsed < MIN_SHERPA:
        return "old", version
    return "ok", version


def runtime_state():
    """("ok" | "missing" | "old", version). Cached for the run."""
    global _runtime_cache
    with _runtime_lock:
        if _runtime_cache is None:
            try:
                _runtime_cache = _detect_runtime()
            except Exception:
                logger.debug("sherpa-onnx check failed", exc_info=True)
                _runtime_cache = ("missing", "")
        return _runtime_cache


def runtime_ok():
    return runtime_state()[0] == "ok"


def runtime_problem():
    state = runtime_state()[0]
    return "" if state == "ok" else state


# ── the catalog: a URL in the code, or the manifest ──────────────────────────────
@dataclass(frozen=True)
class Entry:
    url: str
    sha256: str
    size: int
    version: str = ""


class _Refused(Exception):
    """A URL the policy does not allow."""


def _host_ok(host, hosts, suffixes=()):
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    if host in hosts:
        return True
    return any(host.endswith(s) for s in suffixes)


def check_url(url, hosts, policy=None, redirect=False):
    """Raise _Refused unless ``url`` is an allowed address: HTTPS (unless the policy is relaxed
    for a test), a host on the allow-list, no user name or password, a standard or no port."""
    policy = _policy(policy)
    # (8 KB: a download is redirected to its CDN with a signed address - Hugging Face's carries a base64
    # policy and an RSA signature and runs to 1.5-2 KB, and nothing is gained by being stricter than that)
    if not isinstance(url, str) or not url or len(url) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise _Refused("not a usable address")
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme != "https" and (policy.require_https or scheme != "http"):
        raise _Refused("not https")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise _Refused("credentials in the address")
    try:
        port = parts.port
    except ValueError:
        raise _Refused("bad port") from None
    if policy.require_https and port not in (None, 443):
        raise _Refused("unusual port")
    suffixes = policy.redirect_suffixes if redirect else ()
    if not _host_ok(parts.hostname, tuple(h.lower() for h in hosts), suffixes):
        raise _Refused("host not allowed")
    return parts


def _entry_from(obj, pins, policy, source):
    """A manifest/cache entry -> Entry, or None when anything about it is off. The archive it
    points at must be EXACTLY the one pinned in this file (hash and size)."""
    if not isinstance(obj, dict):
        return None
    url, sha, size, version = obj.get("url"), obj.get("sha256"), obj.get("size"), obj.get("version", "")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        return None
    if isinstance(size, bool) or not isinstance(size, int):
        return None
    if sha.lower() != pins.archive_sha256.lower() or size != pins.archive_size:
        logger.info("Model catalog (%s) names another archive than the one Gusan was built for", source)
        return None
    try:
        check_url(url, policy.file_hosts, policy)
    except _Refused as e:
        logger.info("Model catalog (%s) address refused: %s", source, e)
        return None
    return Entry(url, sha.lower(), size, str(version)[:24] if isinstance(version, (str, int)) else "")


def parse_manifest(data, pins=None, policy=None):
    """The model's Entry from manifest bytes/text, or None (bad JSON, no entry, or an entry that
    is not the pinned archive on an allowed host)."""
    pins, policy = _pins(pins), _policy(policy)
    try:
        if isinstance(data, (bytes, bytearray)):
            if len(data) > policy.manifest_max_bytes:
                return None
            data = bytes(data).decode("utf-8")
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    models = obj.get("models") if isinstance(obj, dict) else None
    if not isinstance(models, dict):
        return None
    return _entry_from(models.get(MANIFEST_KEY), pins, policy, "manifest")


def _read_cache():
    """{"checked": epoch, "entry": dict|None} from disk, or {}."""
    try:
        with open(_manifest_cache_path(), "r", encoding="utf-8") as f:
            raw = f.read(64 * 1024)
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(entry, checked):
    obj = {"checked": float(checked),
           "entry": None if entry is None else {"url": entry.url, "sha256": entry.sha256,
                                                "size": entry.size, "version": entry.version}}
    path = _manifest_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except OSError:
        logger.debug("Could not save the model catalog", exc_info=True)


def catalog_entry(pins=None, policy=None):
    """Where the model can be downloaded from: the URL in the code, else the cached manifest's
    entry (re-checked against the policy every time), else None. Disk only - never the network."""
    pins, policy = _pins(pins), _policy(policy)
    if ARMENIAN_MODEL_URL:
        return _entry_from({"url": ARMENIAN_MODEL_URL, "sha256": pins.archive_sha256,
                            "size": pins.archive_size, "version": ARMENIAN_MODEL_VERSION},
                           pins, policy, "code")
    return _entry_from(_read_cache().get("entry"), pins, policy, "cache")


def has_source(pins=None, policy=None):
    return catalog_entry(pins, policy) is not None


def listed(pins=None, policy=None):
    """THE visibility rule: on disk or downloadable, and able to run. False = hidden everywhere."""
    if not runtime_ok():
        return False
    return ready(pins) or has_source(pins, policy)


def _opener(policy, check):
    class _Redirects(urllib.request.HTTPRedirectHandler):
        max_redirections = policy.max_redirects

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            check_url(newurl, policy.file_hosts if check == "file" else policy.manifest_hosts,
                      policy, redirect=(check == "file"))
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    handlers = [_Redirects()]
    if not policy.proxies:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _user_agent():
    try:
        from app_info import APP_VERSION, APP_NAME
        return "%s/%s" % (APP_NAME, APP_VERSION)
    except Exception:
        return "Gusan"


def fetch_manifest(policy=None, pins=None):
    """GET the manifest (HTTPS, allowed host, at most 64 KB, one redirect chain within the host
    allow-list) -> the model's Entry or None. Raises OSError on a network failure."""
    policy, pins = _policy(policy), _pins(pins)
    try:
        check_url(policy.manifest_url, policy.manifest_hosts, policy)
    except _Refused as e:
        logger.info("Model catalog address refused: %s", e)
        return None
    req = urllib.request.Request(policy.manifest_url, headers={
        "User-Agent": _user_agent(), "Accept": "application/json"})
    try:
        with _opener(policy, "manifest").open(req, timeout=policy.timeout) as resp:
            final = getattr(resp, "url", "") or policy.manifest_url
            check_url(final, policy.manifest_hosts, policy)
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > policy.manifest_max_bytes:
                logger.info("Model catalog too big (%s bytes): ignored", length)
                return None
            data = resp.read(policy.manifest_max_bytes + 1)
    except _Refused as e:
        logger.info("Model catalog redirected somewhere not allowed: %s", e)
        return None
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return None                       # nothing published: a clean "no"
        raise OSError("manifest HTTP %s" % e.code) from None
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from None
    except http.client.HTTPException as e:
        raise OSError("manifest: %s" % type(e).__name__) from None
    if len(data) > policy.manifest_max_bytes:
        logger.info("Model catalog too big: ignored")
        return None
    return parse_manifest(data, pins, policy)


_catalog_lock = threading.Lock()


def refresh_catalog(force=False, privacy=False, now=None, policy=None, pins=None):
    """Look for the model's entry in the manifest - at most once a day (the answer, even "nothing
    there", is cached), never while a URL in the code already says where the model is, never in
    Privacy mode, never at import or startup. True when ``listed()`` / ``has_source()`` changed.
    Blocking (a few seconds at most): worker threads only. Never raises."""
    policy, pins = _policy(policy), _pins(pins)
    try:
        if ARMENIAN_MODEL_URL:
            return False
        if privacy and MANIFEST_BLOCKED_IN_PRIVACY_MODE:
            return False
        if not runtime_ok():
            return False                      # nothing to offer on a copy that cannot run it
        if ready(pins):
            return False                      # it is already here (listed whatever the manifest says): no lookup
        now = time.time() if now is None else now
        with _catalog_lock:
            cache = _read_cache()
            checked = cache.get("checked")
            fresh = isinstance(checked, (int, float)) and 0 <= now - checked < policy.ttl_seconds
            if fresh and not force:
                return False
            before = has_source(pins, policy)
            try:
                entry = fetch_manifest(policy, pins)
            except OSError as e:
                logger.info("Model catalog not reachable: %s", e)
                # Offline: keep what we knew, but don't ask again for a few minutes.
                if not fresh:
                    old = _entry_from(cache.get("entry"), pins, policy, "cache")
                    _write_cache(old, now - policy.ttl_seconds + 600)
                return False
            _write_cache(entry, now)
            return before != (entry is not None)
    except Exception:
        logger.warning("Model catalog check failed", exc_info=True)
        return False


# ── status for the pages ─────────────────────────────────────────────────────────
def status(pins=None, policy=None):
    """What a card or a picker needs, without any network or heavy import."""
    is_ready = ready(pins)
    source = has_source(pins, policy)
    runtime = runtime_state()[0]
    visible = (is_ready or source) and runtime == "ok"
    state = "hidden" if not visible else ("ready" if is_ready else "missing")
    return {"state": state, "listed": visible, "ready": bool(is_ready and visible),
            "has_source": source, "runtime": runtime, "size_text": SIZE_TEXT,
            "name": REAL_NAME, "plain": PLAIN_NAME, "hint": ROW_HINT, "id": MODEL_ID}


def credit():
    """NVIDIA's credit for the Settings / About credits line."""
    return {"name": REAL_NAME, "text": CREDIT_TEXT, "url": CREDIT_URL,
            "license": LICENSE_NAME, "license_url": LICENSE_URL}


# ── which language goes to this engine ───────────────────────────────────────────
def covers(language, mix=None):
    """May the Armenian model transcribe this call's language setting? Only Armenian ("hy" in any
    spelling), or "multi" whose ticked languages are ALL Armenian. "auto" (the language isn't
    known), every other language and any real mix: no - those go to Whisper."""
    import speech_langs
    code = speech_langs.normalize(language)
    if code == "hy":
        return True
    if code == "multi":
        picked = [speech_langs.normalize(c) for c in (mix or []) if speech_langs.normalize(c)]
        return bool(picked) and all(c == "hy" for c in picked)
    return False


def fallback_whisper(downloaded=None, device="cpu"):
    """The Whisper model that reads a language the Armenian model doesn't cover when
    "Armenian" is what Dictation (or a Live Assistant / Audio to text pick) is set to: the best
    one already on this computer for its device, else "base" (the app's default).
    ``downloaded(id)`` -> bool (main.model_downloaded)."""
    order = (("large-v3-turbo", "large-v3", "medium", "small", "base", "tiny") if device == "cuda"
             else ("small", "base", "tiny", "medium", "large-v3-turbo", "large-v3"))
    if downloaded is not None:
        for model in order:
            try:
                if downloaded(model):
                    return model
            except Exception:
                continue
    return hardware.DEFAULT_SPEECH_MODEL


# ── unpacking the archive, safely ────────────────────────────────────────────────
def _sha256_of(path, should_cancel=None, on_bytes=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
            if should_cancel is not None and should_cancel():
                raise Cancelled()
    return h.hexdigest()


def _safe_member(name, pins):
    """The file name of an archive member, or raise ValueError. Exactly MODEL_DIR/<one of the
    known files> - nothing else: no sub folders, no '..', no absolute or drive paths."""
    if "\\" in name or name.startswith("/") or ":" in name or "\x00" in name:
        raise ValueError("unsafe path: %r" % name)
    parts = name.split("/")
    if len(parts) != 2 or parts[0] != pins.dir_name or parts[1] not in pins.files:
        raise ValueError("unexpected entry: %r" % name)
    return parts[1]


def extract_archive(zip_path, models_root, pins=None, should_cancel=None, on_bytes=None):
    """Unpack the model archive into ``models_root``/<dir name>, safely, and return that path.

    Only the pinned file names are accepted, each as a plain file directly inside the one top
    folder; declared sizes are capped before anything is written and a header that lies is caught
    while writing; every file goes to a temporary folder, is hashed and compared with the value
    pinned in this file; the folder appears by one rename or not at all. Raises ValueError on
    anything unexpected (a bad archive), Cancelled, OSError (disk)."""
    pins = _pins(pins)
    final = os.path.join(models_root, pins.dir_name)
    tmp = final + ".part"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    seen = set()
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = z.infolist()
            if len(infos) > 3 * len(pins.files) + 3:
                raise ValueError("too many entries")
            for zi in infos:
                n = zi.filename
                if zi.is_dir():
                    if n.rstrip("/") != pins.dir_name:
                        raise ValueError("unexpected folder: %r" % n)
                    continue
                if (zi.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("link in the archive: %r" % n)
                if zi.flag_bits & 0x1:
                    raise ValueError("encrypted entry: %r" % n)
                name = _safe_member(n, pins)
                if name in seen:
                    raise ValueError("duplicate entry: %r" % n)
                seen.add(name)
                size, want = pins.files[name]
                if zi.file_size > size:
                    raise ValueError("entry too large: %r" % n)
                written, h = 0, hashlib.sha256()
                with z.open(zi) as src, open(os.path.join(tmp, name), "wb") as dst:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > size:               # a header that lies cannot get past this
                            raise ValueError("entry grew past its size: %r" % n)
                        h.update(chunk)
                        dst.write(chunk)
                        if on_bytes is not None:
                            on_bytes(len(chunk))
                        if should_cancel is not None and should_cancel():
                            raise Cancelled()
                if written != size:
                    raise ValueError("wrong size: %r" % n)
                if h.hexdigest() != want:
                    raise ValueError("hash mismatch: %r" % n)
        missing = set(pins.files) - seen
        if missing:
            raise ValueError("missing files: %s" % sorted(missing))
        _swap_into_place(tmp, final)
        return final
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _replace(src, dst, tries=6):
    """os.replace, tried again for a moment: on Windows a virus scanner that is still reading the
    files just written makes the rename of their folder fail for a second or two."""
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.4)


def _swap_into_place(tmp, final):
    """Rename ``tmp`` to ``final`` (replacing a folder that is there): one rename for the model
    folder, so it exists complete or not at all."""
    if not os.path.isdir(final):
        _replace(tmp, final)
        return
    old = final + ".old"
    shutil.rmtree(old, ignore_errors=True)
    _replace(final, old)                         # raises when a file of it is open (Windows)
    try:
        _replace(tmp, final)
    except OSError:
        try:
            os.replace(old, final)
        except OSError:
            pass
        raise
    shutil.rmtree(old, ignore_errors=True)


def _cleanup_leftovers(pins):
    """Unfinished unpacking and replaced folders from an earlier run."""
    base = os.path.join(models_dir(), pins.dir_name)
    for suffix in (".part", ".old"):
        shutil.rmtree(base + suffix, ignore_errors=True)


# ── download ─────────────────────────────────────────────────────────────────────
_download_lock = threading.Lock()
_cancel = threading.Event()
_CHUNK = 256 * 1024


def request_cancel():
    """Ask a running download to stop (it keeps what it has: the next one resumes)."""
    _cancel.set()


cancel_download = request_cancel


def _http_failure(status):
    if status in (401, 403, 404, 410):
        return NotAvailable()
    if status == 429 or status >= 500:
        return DownloadFailed(MSG_SERVER, "server")
    return DownloadFailed(MSG_OTHER, "http_%d" % status)


def _fetch_archive(entry, dest, pins, policy, should_cancel, on_progress):
    """Download ``entry.url`` to ``dest`` (a .part file), resuming what is there. The size is
    pinned: anything else is refused. Returns when the file has exactly the pinned size."""
    total = pins.archive_size
    have = 0
    try:
        have = os.path.getsize(dest)
    except OSError:
        have = 0
    if have > total:
        os.remove(dest)
        have = 0

    def progress(done):
        if on_progress is not None:
            on_progress(min(90, int(done * 90 / total)), done, total)

    if have == total:
        progress(have)
        return
    headers = {"User-Agent": _user_agent(), "Accept": "application/octet-stream",
               "Accept-Encoding": "identity"}
    if have:
        headers["Range"] = "bytes=%d-" % have
    req = urllib.request.Request(entry.url, headers=headers)
    try:
        resp = _opener(policy, "file").open(req, timeout=policy.timeout)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:                # the server says the range is wrong: start over
            try:
                os.remove(dest)
            except OSError:
                pass
            return _fetch_archive(entry, dest, pins, policy, should_cancel, on_progress)
        raise _http_failure(e.code) from None
    except _Refused as e:
        logger.info("Model download redirected somewhere not allowed: %s", e)
        raise NotAvailable() from None
    except urllib.error.URLError:
        raise DownloadFailed(MSG_NETWORK, "network") from None
    except (OSError, ValueError, http.client.HTTPException):
        raise DownloadFailed(MSG_NETWORK, "network") from None
    with resp:
        try:
            check_url(getattr(resp, "url", "") or entry.url, policy.file_hosts, policy, redirect=True)
        except _Refused as e:
            logger.info("Model download ended on an address that is not allowed: %s", e)
            raise NotAvailable() from None
        status = getattr(resp, "status", None) or resp.getcode()
        length = resp.headers.get("Content-Length")
        length = int(length) if length and length.isdigit() else None
        if status == 206 and have:
            rng = resp.headers.get("Content-Range", "")
            m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)$", rng.strip())
            if not m or int(m.group(1)) != have or (m.group(3) != "*" and int(m.group(3)) != total):
                have = 0                          # not the part we asked for: start over
                raise _Restart()
            mode = "ab"
        elif status == 200:
            have, mode = 0, "wb"                  # (a server that ignores Range sends it all)
            if length is not None and length != total:
                raise NotAvailable()
        else:
            raise _http_failure(status)
        if status == 206 and length is not None and have + length != total:
            raise NotAvailable()
        done = have
        progress(done)
        with open(dest, mode) as out:
            while True:
                if should_cancel is not None and should_cancel():
                    raise Cancelled()
                try:
                    chunk = resp.read(_CHUNK)
                except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError):
                    raise DownloadFailed(MSG_INCOMPLETE, "incomplete") from None
                if not chunk:
                    break
                done += len(chunk)
                if done > total:
                    raise DownloadFailed(MSG_UNSAFE, "too_big")
                out.write(chunk)
                progress(done)
    if done != total:
        raise DownloadFailed(MSG_INCOMPLETE, "incomplete")       # a truncated download: kept to resume


class _Restart(Exception):
    pass


def _disk_free_ok(pins):
    try:
        os.makedirs(models_dir(), exist_ok=True)
        free = shutil.disk_usage(models_dir()).free
    except OSError:
        return True
    need = pins.archive_size + sum(size for size, _ in pins.files.values()) + 64 * 1024 * 1024
    return free >= need


def download(on_progress=None, should_cancel=None, privacy=False, *, policy=None, pins=None, entry=None):
    """Download, verify and install the model. Blocking: worker threads only. Returns the model
    folder. ``on_progress(pct, done, total)``: 0-90 while downloading, up to 99 while it is
    checked and unpacked (100 is for the caller once it is on disk). See the module notes."""
    pins, policy = _pins(pins), _policy(policy)
    with _download_lock:
        _cancel.clear()

        def cancelled():
            return _cancel.is_set() or bool(should_cancel and should_cancel())

        if ready(pins):
            if on_progress is not None:
                on_progress(100, pins.archive_size, pins.archive_size)
            return model_dir(pins)
        if privacy and DOWNLOAD_BLOCKED_IN_PRIVACY_MODE:
            raise PrivacyBlocked()
        if not runtime_ok():
            raise NoRuntime()
        entry = entry or catalog_entry(pins, policy)
        if entry is None:
            raise NotAvailable()
        try:
            check_url(entry.url, policy.file_hosts, policy)
        except _Refused:
            raise NotAvailable() from None
        if not _disk_free_ok(pins):
            raise DownloadFailed(MSG_DISK_FULL, "disk_full")
        _cleanup_leftovers(pins)
        dl_dir = _download_dir()
        os.makedirs(dl_dir, exist_ok=True)
        part = os.path.join(dl_dir, pins.dir_name + ".zip.part")
        try:
            for attempt in (1, 2):
                try:
                    _fetch_archive(entry, part, pins, policy, cancelled, on_progress)
                    break
                except _Restart:
                    if attempt == 2:
                        raise DownloadFailed(MSG_OTHER, "resume") from None
                    try:
                        os.remove(part)
                    except OSError:
                        pass
            if cancelled():
                raise Cancelled()
            # 1. the archive is exactly the one Gusan was built for - BEFORE anything is unpacked
            done_bytes = [0]

            def hashed(n):
                done_bytes[0] += n
                if on_progress is not None:
                    on_progress(90 + int(4 * done_bytes[0] / max(1, pins.archive_size)),
                                pins.archive_size, pins.archive_size)
            digest = _sha256_of(part, cancelled, hashed)
            if digest != pins.archive_sha256.lower() or digest != entry.sha256.lower():
                logger.warning("Armenian model download failed its check (SHA-256 mismatch); deleted")
                os.remove(part)                  # never resumed: it is not the file we want
                raise DownloadFailed(MSG_UNSAFE, "bad_hash")
            # 2. unpack by the hardened recipe
            unpacked = [0]
            expect = max(1, sum(size for size, _ in pins.files.values()))

            def extracted(n):
                unpacked[0] += n
                if on_progress is not None:
                    on_progress(94 + int(5 * unpacked[0] / expect), unpacked[0], expect)
            try:
                path = extract_archive(part, models_dir(), pins, cancelled, extracted)
            except ValueError as e:
                logger.warning("Armenian model archive refused: %s", e)
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise DownloadFailed(MSG_UNSAFE, "unsafe") from None
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
                # (a damaged or odd archive that still has the pinned hash cannot happen; an
                # unknown compression or a corrupt stream is the same "not installed" answer)
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise DownloadFailed(MSG_UNSAFE, "unsafe") from None
            except OSError as e:
                raise _disk_or_other(e) from None
            try:
                os.remove(part)
            except OSError:
                pass
            if on_progress is not None:
                on_progress(100, pins.archive_size, pins.archive_size)
            logger.info("Armenian model installed (%s)", entry.version or "?")
            return path
        except OSError as e:
            raise _disk_or_other(e) from None


def _disk_or_other(e):
    import errno
    if getattr(e, "errno", None) == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112):
        return DownloadFailed(MSG_DISK_FULL, "disk_full")
    if isinstance(e, PermissionError):
        return DownloadFailed("Gusan couldn't save the model on this computer.", "permission")
    return DownloadFailed(MSG_OTHER, "other")


def remove(pins=None):
    """Delete the model (and any download left over). False when there was nothing to delete.
    Raises InUse while a piece is being transcribed with it."""
    pins = _pins(pins)
    eng = _ENGINE
    if eng is not None and not eng.unload():
        raise InUse()
    directory = model_dir(pins)
    existed = os.path.isdir(directory)
    for path in (directory, directory + ".part", directory + ".old", _download_dir()):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            if path == directory:
                raise InUse(MSG_IN_USE) from e
    return existed


# ── the audio recipe (pure functions) ────────────────────────────────────────────
SR = 16000
MIN_SECONDS = 0.3            # < 0.3 s is never a word, and an empty input raises in sherpa-onnx
SILENCE_PEAK = 1e-4          # digital silence: the model would invent a word
QUIET_PEAK = 0.2             # below this a piece is raised ...
BOOST_PEAK = 0.5             # ... to this
MAX_WINDOW_S = 25.0          # a piece is at most this long (the hard limit is 30 s)
PAUSE_CLOSE_S = 0.5          # a pause this long ends a piece that is already ...
MIN_CLOSE_S = 6.0            # ... this long
HARD_PAUSE_S = 1.0           # a pause this long ends a piece whatever its length (the model drops words after a long silence)
OVERLAP_S = 0.3              # a piece started mid-speech starts this much earlier
MERGE_GAP_S = 0.35           # speech regions closer than this are one


def tidy(text):
    """The model spells "and" as two letters (its training text was NFKC-normalised); modern
    Armenian writes the single letter U+0587 (sentence start: Եվ). Output without a letter at all
    (only punctuation: what it makes up for noise) is dropped."""
    t = (text or "").strip()
    t = t.replace("եւ", "և").replace("Եւ", "Եվ").replace(
        "ԵՒ", "ԵՎ")
    return t if any(c.isalpha() for c in t) else ""


def prepare(x):
    """16 kHz mono float32 ready for the model, or None when there is nothing to decode (shorter
    than 0.3 s, or digital silence). A quiet piece is raised to a peak of 0.5, never lowered."""
    import numpy as np
    try:
        x = np.ascontiguousarray(x, dtype=np.float32).reshape(-1)
    except (TypeError, ValueError):
        return None
    if x.size < int(MIN_SECONDS * SR):
        return None
    if not np.isfinite(x).all():
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    peak = float(max(x.max(), -x.min()))              # (no temporary copy: a file can be hours long)
    if peak < SILENCE_PEAK:
        return None
    if peak < QUIET_PEAK:
        x = (x * (BOOST_PEAK / peak)).astype(np.float32)
    return x


def _silero_regions(x):
    """Speech regions [(start, end)] in samples from the voice detector faster-whisper ships
    (Silero, the one Whisper's own vad_filter uses), or None when it isn't there."""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception:
        return None
    try:
        opts = VadOptions(min_silence_duration_ms=300, speech_pad_ms=400, min_speech_duration_ms=120)
        return [(int(r["start"]), int(r["end"])) for r in get_speech_timestamps(x, opts, SR)]
    except Exception:
        logger.debug("The voice detector failed; using the level gate", exc_info=True)
        return None


def _warm_vad():
    """Import the voice detector and build its session now (about two seconds the first time), so
    the first piece of speech does not wait for it."""
    try:
        import numpy as np
        _silero_regions(np.zeros(SR, dtype=np.float32))
    except Exception:
        logger.debug("The voice detector could not be warmed up", exc_info=True)


def _energy_regions(x):
    """Speech regions from the loudness alone (the fallback when the voice detector is missing):
    20 ms frames louder than a share of the loud part of the piece."""
    import numpy as np
    frame = SR // 50
    n = x.size // frame
    if n < 3:
        return []
    rms = np.sqrt(np.mean(np.square(x[:n * frame].reshape(n, frame)), axis=1))
    thr = max(0.008, 0.2 * float(np.percentile(rms, 90)))
    loud = rms > thr
    regions, start = [], None
    for i, on in enumerate(loud):
        if on and start is None:
            start = i
        elif not on and start is not None:
            regions.append((start * frame, i * frame))
            start = None
    if start is not None:
        regions.append((start * frame, n * frame))
    pad = int(0.15 * SR)
    padded = [(max(0, s - pad), min(x.size, e + pad)) for s, e in regions if e - s >= int(0.12 * SR)]
    return padded


def speech_regions(x, detector=None):
    """[(start, end)] in samples where someone speaks, merged across gaps shorter than 0.35 s."""
    found = detector(x) if detector is not None else _silero_regions(x)
    if found is None:
        found = _energy_regions(x)
    merged = []
    for s, e in sorted((int(s), int(e)) for s, e in found if e > s):
        if merged and s - merged[-1][1] <= int(MERGE_GAP_S * SR):
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _quietest(x, lo, hi):
    """The sample in [lo, hi) where the audio is quietest over 0.1 s (a pause between words)."""
    import numpy as np
    win, hop = SR // 10, SR // 50
    if hi - lo <= win:
        return (lo + hi) // 2
    seg = np.square(x[lo:hi])
    n = (seg.size - win) // hop + 1
    if n <= 0:
        return (lo + hi) // 2
    csum = np.concatenate(([0.0], np.cumsum(seg, dtype=np.float64)))
    starts = np.arange(n) * hop
    energy = csum[starts + win] - csum[starts]
    best = int(np.argmin(energy))
    return lo + best * hop + win // 2


def plan_windows(regions, x=None, max_s=MAX_WINDOW_S):
    """[(start, end, nominal_start, overlapped)] in samples: the pieces the model reads. Regions are
    packed into pieces of at most ``max_s`` seconds, closing one at a pause of 0.5 s once it is 6 s
    long and at any pause of 1 s; speech that runs longer than ``max_s`` without a pause is cut at its quietest point and
    the next piece starts 0.3 s earlier (``overlapped``: its first words may repeat the last words of
    the one before - join_texts drops them). ``nominal_start`` is where the piece truly starts."""
    max_n = int(max_s * SR)
    pause_n, close_n, overlap_n = int(PAUSE_CLOSE_S * SR), int(MIN_CLOSE_S * SR), int(OVERLAP_S * SR)
    hard_n = int(HARD_PAUSE_S * SR)
    atoms = []                                    # (start, end, continued_from_the_atom_before)
    for s, e in regions:
        cur, cont = s, False
        while e - cur > max_n:
            lo, hi = cur + int(0.6 * max_n), cur + max_n
            cut = _quietest(x, lo, hi) if x is not None else (lo + hi) // 2
            atoms.append((cur, cut, cont))
            cur, cont = cut, True
        atoms.append((cur, e, cont))
    windows = []
    cur = None                                    # [start, end, nominal_start, overlapped]
    for s, e, cont in atoms:
        if cur is not None and not cont:
            gap, length = s - cur[1], cur[1] - cur[0]
            if e - cur[0] <= max_n and not (gap >= hard_n or (gap >= pause_n and length >= close_n)):
                cur[1] = e                        # the same piece goes on
                continue
        if cur is not None:
            windows.append(tuple(cur))
        if cont:
            cur = [max(0, s - overlap_n), e, s, True]
        else:
            cur = [s, e, s, False]
    if cur is not None:
        windows.append(tuple(cur))
    return windows


def _words(text):
    return [w for w in re.split(r"\s+", text.strip()) if w]


def _norm_word(word):
    folded = unicodedata.normalize("NFKC", word).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def drop_repeat(prev, text, max_words=4):
    """``text`` without the words (at most 4) it repeats from the end of ``prev``: a piece that
    started 0.3 s early hears the last word of the one before again."""
    before, mine = _words(prev), _words(text)
    for k in range(min(max_words, len(before), len(mine)), 0, -1):
        a = [_norm_word(w) for w in before[-k:]]
        b = [_norm_word(w) for w in mine[:k]]
        if a == b and all(a):
            return " ".join(mine[k:])
    return " ".join(mine)


def join_texts(parts):
    """Join the texts of consecutive pieces, ``parts`` = [(text, overlapped)]; a piece that started
    mid-speech loses the words it repeats from the one before."""
    out = []
    for text, overlapped in parts:
        text = (text or "").strip()
        if out and overlapped:
            text = drop_repeat(out[-1], text)
        if text:
            out.append(text)
    return " ".join(out)


# A piece the model read is cut into sentence-sized segments for the final re-read of a meeting or a
# file (the speaker labels are decided per segment, and a 25 s piece may hold two people): at the end
# of a sentence, at a pause, or at 12 s. The times come from the model's own token times.
SENTENCE_END = "\u0589.!?\u2026"     # the Armenian full stop, and the ones every language has
SEG_MAX_S = 12.0
SEG_PAUSE_S = 0.8
SEG_MIN_S = 1.0


def split_result(text, tokens, stamps, length_s):
    """[(start_s, end_s, text)], seconds from the start of the piece, for one decoded piece: split at
    the end of a sentence, at a pause of 0.8 s and at 12 s, with the model's token times. One entry
    for the whole piece when the times are not there (a recognizer without them) or do not belong to
    the text."""
    text = (text or "").strip()
    length_s = max(0.0, float(length_s))
    whole = [(0.0, length_s, text)] if text else []
    try:
        if not text or not tokens or len(tokens) != len(stamps) or "".join(tokens).strip() != text:
            return whole
        stamps = [float(t) for t in stamps]
    except (TypeError, ValueError):
        return whole
    groups = []                                   # [start, [token, ...]]
    for i, token in enumerate(tokens):
        word_start = i == 0 or str(token).startswith(" ")
        if groups and word_start:
            start, got = groups[-1]
            before = "".join(got).rstrip().rstrip("»”\"')")
            ends = bool(before) and before[-1] in SENTENCE_END
            paused = stamps[i] - stamps[i - 1] >= SEG_PAUSE_S and stamps[i - 1] - start >= SEG_MIN_S
            long = stamps[i] - start >= SEG_MAX_S
            if ends or paused or long:
                groups.append([stamps[i], []])
        if not groups:
            groups.append([stamps[i], []])
        groups[-1][1].append(str(token))
    out = []
    for k, (start, got) in enumerate(groups):
        end = groups[k + 1][0] if k + 1 < len(groups) else length_s
        out.append([max(0.0, min(start, length_s)), max(start, min(end, length_s)), "".join(got).strip()])
    merged = []
    for seg in out:                               # a stub of a second is no segment: it joins its neighbour
        if merged and (seg[1] - seg[0] < SEG_MIN_S or merged[-1][1] - merged[-1][0] < SEG_MIN_S):
            merged[-1][1] = seg[1]
            merged[-1][2] = (merged[-1][2] + " " + seg[2]).strip()
        else:
            merged.append(seg)
    return [(a, b, t) for a, b, t in merged if t]


# ── the engine ───────────────────────────────────────────────────────────────────
THREADS = 2                  # 2 threads; 4 are no faster (measured)
_PIECE_SECONDS_LOG = 1.0     # pieces shorter than this are not worth a log line


def _build_recognizer(directory, threads):
    import sherpa_onnx
    version = _parse_version(getattr(sherpa_onnx, "__version__", ""))
    if version is not None and version < MIN_SHERPA:
        raise NoRuntime()
    j = lambda name: os.path.join(directory, name)       # noqa: E731
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=j("encoder.int8.onnx"), decoder=j("decoder.int8.onnx"), joiner=j("joiner.int8.onnx"),
        tokens=j("tokens.txt"), num_threads=threads, sample_rate=SR, feature_dim=80,
        decoding_method="greedy_search", model_type="nemo_transducer")


class Engine:
    """The Armenian recognizer: loaded once on first use (3-8 s, 221 MB), one piece at a time,
    freed after it has been idle (maybe_unload_idle, with the other speech models).

    ``model_dir`` / ``pins``: where the files are (tests; the default is the app's folder);
    ``factory(directory, threads)``: builds the recognizer (tests pass a fake);
    ``detector(x)``: speech regions [(start, end)] (tests); ``warm_vad``: build the voice detector
    together with the recognizer (the process-wide engine does, so the first piece is not slowed)."""

    def __init__(self, model_dir=None, pins=None, factory=None, detector=None, clock=time.monotonic,
                 threads=THREADS, warm_vad=False):
        self._warm_vad = bool(warm_vad)
        self._dir = model_dir
        self._pins = pins
        self._factory = factory or _build_recognizer
        self._detector = detector
        self._clock = clock
        self._threads = threads
        self._rec = None
        self._load_lock = threading.Lock()          # a load, a lease start, an unload
        self._infer_lock = threading.Lock()         # one decode at a time
        self._waiting = 0                           # pieces waiting for it (a long read lets them go first)
        self._users_lock = threading.Lock()
        self._users = 0
        self._last_use = 0.0
        self.last_seconds = 0.0

    # -- state
    def _directory(self):
        return self._dir or model_dir(self._pins)

    def loaded(self):
        return self._rec is not None

    @property
    def users(self):
        with self._users_lock:
            return self._users

    @property
    def last_use(self):
        return self._last_use

    # -- load / unload
    def _build_locked(self):
        directory = self._directory()
        if not files_present(directory, self._pins):
            raise NotReady()
        t0 = time.monotonic()
        try:
            rec = self._factory(directory, self._threads)
        except SpeechHyError:
            raise
        except ImportError:
            raise NoRuntime() from None
        if self._warm_vad and self._detector is None:
            _warm_vad()
        logger.info("Armenian model loaded in %.1f s (%d threads)", time.monotonic() - t0, self._threads)
        return rec

    def load(self):
        """Load the recognizer now (idempotent). Raises NotReady / NoRuntime / the factory's error."""
        with self._load_lock:
            if self._rec is None:
                self._rec = self._build_locked()
            self._last_use = self._clock()

    def unload(self):
        """Free the recognizer. False (nothing freed) while a piece is being transcribed."""
        with self._load_lock:
            if self.users > 0:
                return False
            had = self._rec is not None
            self._rec = None
        if had:
            gc.collect()
        return True

    def maybe_unload_idle(self, idle_sec, now=None):
        """Free the recognizer after ``idle_sec`` without use. Never waits and never frees one in
        use (the idle sweep; model_manager's Whisper rule). True when it freed it."""
        if not idle_sec or idle_sec <= 0:
            return False
        if not self._load_lock.acquire(blocking=False):
            return False
        try:
            if self._rec is None or self.users > 0:
                return False
            t = self._clock() if now is None else now
            if t - self._last_use < idle_sec:
                return False
            self._rec = None
        finally:
            self._load_lock.release()
        gc.collect()
        logger.info("Freed the idle Armenian model")
        return True

    @contextmanager
    def _lease(self):
        """The recognizer for one transcription: loaded if needed, and counted as in use until the
        block ends - the idle sweep and unload() never take it away mid-piece."""
        with self._load_lock:
            if self._rec is None:
                self._rec = self._build_locked()
            rec = self._rec
            self._last_use = self._clock()
            with self._users_lock:
                self._users += 1
        try:
            yield rec
        finally:
            with self._users_lock:
                self._users = max(0, self._users - 1)
            self._last_use = self._clock()

    # -- decoding
    @contextmanager
    def _decode_slot(self):
        """One decode at a time, and a fair queue: a long read (a file of an hour is a few hundred
        pieces) hands the model over to a piece that is waiting - a dictation, a live call - instead of
        taking the lock again at once (a plain lock lets the thread that just let go win it back)."""
        with self._users_lock:
            self._waiting += 1
        self._infer_lock.acquire()
        with self._users_lock:
            self._waiting -= 1
        try:
            yield
        finally:
            self._infer_lock.release()
            if self._waiting > 0:
                time.sleep(0.003)

    def _decode(self, rec, x):
        """One piece through the model: (text as the model wrote it - not tidied, its tokens, their
        times in seconds). ('', [], []) for an empty input (sherpa-onnx raises RuntimeError there)."""
        try:
            stream = rec.create_stream()
            stream.accept_waveform(SR, x)
            rec.decode_stream(stream)
            result = stream.result
            text = str(getattr(result, "text", "") or "")
            try:
                tokens = [str(t) for t in (getattr(result, "tokens", None) or [])]
                stamps = [float(t) for t in (getattr(result, "timestamps", None) or [])]
            except (TypeError, ValueError):
                tokens, stamps = [], []
            return text, tokens, stamps
        except RuntimeError as e:
            logger.debug("Armenian model: empty piece (%s)", e)
            return "", [], []

    def _read(self, audio, should_cancel=None, on_progress=None):
        """[{"begin", "end", "nominal", "overlapped", "text", "parts"}] for the whole audio (seconds;
        ``parts`` = split_result's sentences), in order, or None when cancelled."""
        t0 = time.monotonic()
        x = prepare(audio)
        if x is None:
            return []
        total = x.size / float(SR)
        out, windows = [], []
        with self._lease() as rec:
            # The voice detector reads the whole audio outside the decode lock (it keeps no state
            # between calls, so concurrent reads are fine), and the lock is taken PER PIECE: a file of
            # an hour is a few hundred pieces, and a dictation or a live piece that arrives meanwhile
            # waits for one piece (about a second), not for the whole file.
            regions = speech_regions(x, self._detector)
            windows = plan_windows(regions, x)
            for s, e, nominal, overlapped in windows:
                if should_cancel is not None and should_cancel():
                    return None
                piece = prepare(x[s:e])
                if piece is not None:
                    with self._decode_slot():
                        raw, tokens, stamps = self._decode(rec, piece)
                else:
                    raw, tokens, stamps = "", [], []
                text = tidy(raw)
                if text:
                    seconds = (e - s) / float(SR)
                    parts = [(a, b, tidy(t)) for a, b, t in split_result(raw, tokens, stamps, seconds)]
                    parts = [(a, b, t) for a, b, t in parts if t]
                    out.append({"begin": s / float(SR), "end": e / float(SR),
                                "nominal": nominal / float(SR), "overlapped": overlapped,
                                "text": text, "parts": parts or [(0.0, seconds, text)]})
                if on_progress is not None:
                    try:
                        on_progress(min(e / float(SR), total), total)
                    except Exception:
                        pass
        took = time.monotonic() - t0
        self.last_seconds = took
        if total >= _PIECE_SECONDS_LOG:
            logger.info("Armenian model: %.1f s of audio read in %.2f s (%.2fx real time, %d piece(s), "
                        "%d with words)", total, took, took / total, len(windows), len(out))
        return out

    def transcribe(self, audio, language=None):
        """The text of ``audio`` (16 kHz mono float32); "" when there is nothing to read (silence,
        noise, under 0.3 s). Callers send Armenian here and nothing else (``covers``). Raises
        NotReady / NoRuntime when the model can't be loaded."""
        read = self._read(audio) or []
        return join_texts([(w["text"], w["overlapped"]) for w in read])

    def transcribe_segments(self, audio, on_progress=None, should_cancel=None):
        """[{"start", "end", "text"}] in seconds, in order, not overlapping (like Whisper's
        segments: a sentence or a stretch up to 12 s each, never a whole 25 s piece); [] for
        nothing; None when ``should_cancel()`` stopped it. ``on_progress(done_s, total_s)``. The
        words a piece started mid-speech repeats are dropped."""
        read = self._read(audio, should_cancel, on_progress)
        if read is None:
            return None
        segments, prev = [], ""
        for w in read:
            first = True
            for a, b, text in w["parts"]:
                if first and w["overlapped"] and prev:
                    text = drop_repeat(prev, text)
                start = w["begin"] + a
                if first and w["overlapped"]:
                    start = max(start, w["nominal"])
                first = False
                if not text:
                    continue
                end = w["begin"] + b
                if segments and start < segments[-1]["end"]:
                    start = segments[-1]["end"]
                segments.append({"start": round(start, 2), "end": round(max(end, start + 0.05), 2),
                                 "text": text})
                prev = text
        return segments


_ENGINE = None
_engine_lock = threading.Lock()


def engine():
    """The process-wide engine. Creating it loads nothing."""
    global _ENGINE
    with _engine_lock:
        if _ENGINE is None:
            _ENGINE = Engine(warm_vad=True)
        return _ENGINE


def transcribe(audio, language=None):
    return engine().transcribe(audio, language)


def transcribe_segments(audio, on_progress=None, should_cancel=None):
    return engine().transcribe_segments(audio, on_progress=on_progress, should_cancel=should_cancel)


def load():
    engine().load()


def unload():
    eng = _ENGINE
    return True if eng is None else eng.unload()


def loaded():
    eng = _ENGINE
    return bool(eng is not None and eng.loaded())


def maybe_unload_idle(idle_sec, now=None):
    eng = _ENGINE
    return False if eng is None else eng.maybe_unload_idle(idle_sec, now)
