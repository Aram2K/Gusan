"""Updates (Gusan 2.0): one copy of the check and the install, shared by the
controller and Settings › Updates & about.

Kept exactly, because every 1.9.x install updates through them: RELEASES_API
(latest release), tags "vX.Y.Z", and the asset TranscribeApp-Windows-Setup.exe
(+ its .sha256, which build.yml publishes next to it). The GitHub repo was
renamed from Aram2K/transcribe-app to Aram2K/Gusan: older installs still ask the
old address, which GitHub redirects, so a repo named transcribe-app must never
be created again.

Integration notes for Wave 4
----------------------------
Status (Wave 4, services): DONE in main.py - _background_check_updates uses
check_latest; AppController.note_update_check(status, tag, manual=True)
stores pending_update_version (and emits sig_setting_changed for it);
_prompt_update is ONE quiet tray notice a run (click = install), gated by
should_prompt, never a modal; install_update(tag, manual, on_done) runs
install() and quits; "Install updates when I quit" downloads ahead and
launch()es from quit_app. Wave 4 review: install_update refuses (on_done /
a notification says why) while a dictation or meeting records or a file job
runs - installing quits the app; the notice waits while a meeting records;
a dismissed (skipped) tag is never fetched ahead nor launched at quit.
Still open: a "Not now" control that writes dismissed_update_version (no page
has one yet).

* Startup check (replaces main._background_check_updates, on its worker):
      status, tag = updater.check_latest(app.cfg)          # automatic: respects
                                                           # dismissed_update_version
      track "update_check_result" updater.check_props(status, manual=False)
      pending = updater.pending_after_check(app.cfg, status, tag)
      if pending is not None: cfg["pending_update_version"] = pending (GUI
      thread; it is a PROTECTED key, so the controller writes it, not
      set_settings) and save; if status == "available": sig_update_available.
* "Check now" (Settings): updater.check_latest(app.cfg, manual=True) on a
  worker (api_keys.run_then); show updater.status_line(status, tag)
  ("Gusan 2.0.0" / "Up to date" | "Update available · v2.0.1" | ...).
* Calm prompts: "Not now" must remember the answer, so it isn't asked again
  after every dictation: app.set_setting("dismissed_update_version", tag)
  (updater.dismiss_changes(tag)). should_prompt(cfg, tag) says whether the
  quiet banner/prompt may show - gate EVERY sig_update_available emit with it:
  the startup cached_pending emit (main.py AppController.__init__, "prompt at
  startup immediately") and the after-dictation nudge (main.py, "nudge the
  user about a pending update"). pending_update_version stays set for a
  dismissed tag, so Settings and the tray can still say "Update available".
* Install (replaces main._download_and_install_update and the Settings copy):
  on a worker, ok, info = updater.install(tag). ok: info is the installer
  path - it is running; then on the GUI thread clear pending_update_version,
  flush the save, track "update_install_result" {"manual", "ok": True} and
  quit (sig_quit). Not ok: info is a short message to show. On macOS (no
  installer to run) install() returns (False, updater.UNSUPPORTED): open
  updater.release_page(tag) instead.
  "Install updates when I quit": cached_download(tag) ahead of time (kept in
  %TEMP%\\gusan-update\\<tag>\\ and reused by later runs once its sha256
  checks out; other releases' copies are removed, all of them by
  prune_cache() once the app is up to date), then launch(path) from quit_app.
* main._parse_version / _sha256_from_checksum_text / _trusted_update_url /
  _update_info_from_release / _update_info_from_manifest have equals here
  (parse_version, sha256_from_checksum_text, trusted_update_url,
  update_info_from_release, update_info_from_manifest - the last two take
  current/dismissed instead of reading main's module cfg); test_updater
  checks they agree, so main can import them later.
"""

import hashlib
import logging
import os
import re
import sys
import tempfile
from urllib.parse import urlparse

from app_info import APP_NAME, APP_VERSION

logger = logging.getLogger("transcribe")

PROJECT_GITHUB_URL = "https://github.com/Aram2K/Gusan"
RELEASES_URL = "https://github.com/Aram2K/Gusan/releases/latest"
RELEASES_API = "https://api.github.com/repos/Aram2K/Gusan/releases/latest"
RELEASES_MANIFEST_URL = "https://github.com/Aram2K/Gusan/releases/latest/download/update-manifest.json"
INSTALLER_NAME = "TranscribeApp-Windows-Setup.exe"      # never rename: 1.9.x downloads this

AVAILABLE, LATEST, ERROR, DISMISSED = "available", "latest", "error", "dismissed"
UNSUPPORTED = "unsupported"

_TAG_RE = re.compile(r"^v?\d+(\.\d+){0,2}([.\-+][0-9A-Za-z.\-]+)?$")


# ── versions ─────────────────────────────────────────────────────────────────
def parse_version(v):
    """'v1.2.3' -> (1, 2, 3); anything unreadable -> (0, 0, 0)."""
    try:
        parts = [int(x) for x in str(v).lstrip("v").split(".")[:3]]
        return tuple((parts + [0, 0, 0])[:3])
    except Exception:
        return (0, 0, 0)


def is_newer(tag, current=APP_VERSION):
    return bool(tag) and parse_version(tag) > parse_version(current)


def valid_tag(tag):
    """A release tag safe to put in a download URL."""
    return bool(tag) and bool(_TAG_RE.match(str(tag)))


# ── checking ─────────────────────────────────────────────────────────────────
def _get(get):
    if get is not None:
        return get
    import requests
    return requests.get


def check_latest(cfg=None, *, manual=False, get=None, current=APP_VERSION, timeout=8):
    """Ask GitHub for the latest release. Returns (status, tag):
    ("available", tag) newer than this version; ("latest", tag) not newer;
    ("dismissed", tag) newer but the user said "Not now" to that tag (an
    automatic check only - "Check now" always says available);
    ("error", "") no answer. Blocking (worker)."""
    try:
        # As 1.9: no system proxy (a broken proxy setting blocked the check).
        resp = _get(get)(RELEASES_API, timeout=timeout, proxies={"http": None, "https": None})
    except Exception as e:
        logger.debug("Update check failed: %s", e)
        return ERROR, ""
    if getattr(resp, "status_code", 0) != 200:
        return ERROR, ""
    try:
        data = resp.json() or {}
    except Exception:
        return ERROR, ""
    tag = str(data.get("tag_name") or "") if isinstance(data, dict) else ""
    if not tag:
        return ERROR, ""
    if not is_newer(tag, current):
        return LATEST, tag
    if not manual and (cfg or {}).get("dismissed_update_version") == tag:
        return DISMISSED, tag
    return AVAILABLE, tag


def check_props(status, manual=False):
    """The "update_check_result" analytics props (same as 1.9)."""
    if status == ERROR:
        return {"manual": bool(manual), "failed": True}
    return {"manual": bool(manual), "available": status in (AVAILABLE, DISMISSED)}


def pending_after_check(cfg, status, tag):
    """The new pending_update_version after a check, or None (no change)."""
    current = (cfg or {}).get("pending_update_version", "")
    if status in (AVAILABLE, DISMISSED):
        return tag if current != tag else None
    if status == LATEST:
        return "" if current else None
    return None


def should_prompt(cfg, tag, current=APP_VERSION):
    """May the app bring up this update by itself? Newer and not dismissed."""
    return is_newer(tag, current) and (cfg or {}).get("dismissed_update_version") != tag


def dismiss_changes(tag):
    """The set_settings changes for "Not now"."""
    return {"dismissed_update_version": tag}


def status_line(status, tag="", current=APP_VERSION):
    """(title, status text) for the Updates & about row."""
    title = f"{APP_NAME} {current}"
    if status in (AVAILABLE, DISMISSED):
        return title, f"Update available · {tag}"
    if status == LATEST:
        return title, "Up to date"
    if status == "checking":
        return title, "Checking…"
    if status == ERROR:
        return title, "Couldn't check right now"
    return title, ""


def release_page(tag=None):
    if tag and valid_tag(tag):
        return f"{PROJECT_GITHUB_URL}/releases/tag/{tag}"
    return RELEASES_URL


# ── the sha256 / release helpers (main.py's, without its module cfg) ─────────
def trusted_update_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc.lower() == "github.com"
    except Exception:
        return False


def sha256_from_checksum_text(text):
    for token in (text or "").replace("\r", " ").replace("\n", " ").split():
        token = token.strip()
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token):
            return token.lower()
    return ""


def asset_url(data, wanted_name):
    wanted = wanted_name.lower()
    for asset in data.get("assets", []):
        if asset.get("name", "").lower() == wanted:
            return asset.get("browser_download_url")
    return None


def update_info_from_manifest(manifest, current=APP_VERSION, dismissed=""):
    tag = manifest.get("tag") or f"v{manifest.get('version', '')}"
    version = manifest.get("version") or tag
    if not tag or parse_version(version) <= parse_version(current):
        return None
    if dismissed and dismissed == tag:
        return None
    installer = manifest.get("windows", {}).get("installer", {})
    return {
        "tag": tag,
        "installer_url": installer.get("url"),
        "checksum_url": installer.get("sha256_url"),
        "checksum": installer.get("sha256", ""),
        "body": manifest.get("notes", "") or manifest.get("changelog", ""),
        "notes_url": manifest.get("notes_url", RELEASES_URL),
        "source": "manifest",
    }


def update_info_from_release(data, current=APP_VERSION, dismissed=""):
    latest = data.get("tag_name", "")
    if parse_version(latest) <= parse_version(current):
        return None
    if dismissed and dismissed == latest:
        return None
    installer_url = None
    installer_name = ""
    preferred = asset_url(data, INSTALLER_NAME)
    if preferred:
        installer_url = preferred
        installer_name = INSTALLER_NAME.lower()
    else:
        for asset in data.get("assets", []):
            name = asset.get("name", "").lower()
            if "setup" in name and name.endswith(".exe"):
                installer_url = asset.get("browser_download_url")
                installer_name = name
                break
    checksum_url = None
    if installer_name:
        checksum_url = (asset_url(data, f"{installer_name}.sha256")
                        or asset_url(data, f"{installer_name}.sha256sum"))
    return {
        "tag": latest,
        "installer_url": installer_url,
        "checksum_url": checksum_url,
        "checksum": "",
        "body": data.get("body", "") or f"Release notes: {data.get('html_url', RELEASES_URL)}",
        "notes_url": data.get("html_url", RELEASES_URL),
        "source": "release",
    }


# ── installing ───────────────────────────────────────────────────────────────
def installer_url(tag):
    return f"{PROJECT_GITHUB_URL}/releases/download/{tag}/{INSTALLER_NAME}"


def checksum_url(tag):
    return f"{installer_url(tag)}.sha256"


def _urlopen(urlopen):
    if urlopen is not None:
        return urlopen
    import urllib.request

    def _open(url, timeout=60):
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        return urllib.request.urlopen(req, timeout=timeout)
    return _open


def _expected_sha256(tag, opener):
    """The published checksum, or "" when there is none to check against."""
    try:
        with opener(checksum_url(tag), timeout=20) as resp:
            return sha256_from_checksum_text(resp.read(4096).decode("ascii", "ignore"))
    except Exception as e:
        logger.info("No checksum for %s (%s) - installing without one", tag, e)
        return ""


def download(tag, *, urlopen=None, dest_dir=None, verify=True, expected=None):
    """Download the Windows installer for ``tag`` and check its sha256 when
    the release has one (``expected``: that checksum when the caller already
    read it, "" for none; None reads it here). Blocking (worker). Returns
    (True, path) or (False, message)."""
    if not valid_tag(tag):
        return False, "That isn't a release we can install."
    url = installer_url(tag)
    if not trusted_update_url(url):
        return False, "That isn't a release we can install."
    opener = _urlopen(urlopen)
    dest = os.path.join(dest_dir or tempfile.gettempdir(), INSTALLER_NAME)
    digest = hashlib.sha256()
    try:
        with opener(url, timeout=60) as resp, open(dest, "wb") as out:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
    except Exception as e:
        logger.warning("Update download failed: %s", e)
        _remove(dest)
        return False, "Couldn't download the update. Check your connection and try again."
    if verify:
        if expected is None:
            expected = _expected_sha256(tag, opener)
        if expected and expected != digest.hexdigest():
            logger.warning("Update %s failed its checksum", tag)
            _remove(dest)
            return False, "The download was damaged. Try again."
    return True, dest


# ── "Install updates when I quit": one download per release, kept ────────────
CACHE_DIR_NAME = "gusan-update"         # %TEMP%\gusan-update\<tag>\TranscribeApp-Windows-Setup.exe
_RECORD_SUFFIX = ".sha256"              # the checksum noted when it was downloaded


def cache_root():
    return os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_record(path):
    try:
        with open(path, "r", encoding="ascii", errors="ignore") as f:
            return sha256_from_checksum_text(f.read(4096))
    except OSError:
        return ""


def prune_cache(keep=None, root=None):
    """Remove kept installers except release ``keep`` (None: all of them) -
    e.g. once that release is installed. Never raises."""
    import shutil
    root = root or cache_root()
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if keep and name == keep:
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            _remove(path)        # a 2.0 pre-release build kept it in the root


def cached_download(tag, *, urlopen=None, root=None, verify=True):
    """The installer for ``tag``, kept in <root>/<tag>/ (root: %TEMP%\\gusan-
    update) and reused across runs - not ~100 MB downloaded every launch. A
    kept file is reused only when its sha256 matches the release's published
    checksum (or, when that can't be read, the one noted at download);
    anything else is downloaded again. Other releases' files are removed.
    Blocking (worker). Returns (True, path) or (False, message)."""
    if not valid_tag(tag):
        return False, "That isn't a release we can install."
    root = root or cache_root()
    prune_cache(keep=tag, root=root)
    folder = os.path.join(root, tag)
    path = os.path.join(folder, INSTALLER_NAME)
    record = path + _RECORD_SUFFIX
    opener = _urlopen(urlopen)
    expected = _expected_sha256(tag, opener) if verify else ""
    if os.path.isfile(path):
        try:
            actual = file_sha256(path)
        except OSError:
            actual = ""
        known = expected or _read_record(record)
        if actual and known and actual == known:
            logger.info("Update %s: reusing the installer downloaded earlier", tag)
            return True, path
        logger.info("Update %s: the kept installer doesn't check out - downloading again", tag)
        _remove(path)
        _remove(record)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        logger.warning("Update folder unavailable: %s", e)
        return False, "Couldn't save the update."
    ok, info = download(tag, urlopen=urlopen, dest_dir=folder, verify=verify, expected=expected)
    if not ok:
        return ok, info
    try:
        with open(record, "w", encoding="ascii") as f:
            f.write(f"{file_sha256(info)}  {INSTALLER_NAME}\n")
    except OSError:
        logger.debug("Couldn't note the update's checksum", exc_info=True)
    return True, info


def launch(path, *, startfile=None):
    """Run a downloaded installer (Windows). Returns (ok, message)."""
    if startfile is None:
        if sys.platform != "win32":
            return False, UNSUPPORTED
        startfile = os.startfile
    try:
        startfile(path)
    except Exception as e:
        logger.warning("Could not start the installer: %s", e)
        return False, "Couldn't start the installer."
    return True, path


def install(tag, *, urlopen=None, startfile=None, dest_dir=None, verify=True):
    """Download, check and run the installer for ``tag``. Blocking (worker).
    Returns (True, installer path) - the caller then quits so the installer
    can replace the files - or (False, message)."""
    if startfile is None and sys.platform != "win32":
        return False, UNSUPPORTED
    ok, info = download(tag, urlopen=urlopen, dest_dir=dest_dir, verify=verify)
    if not ok:
        return False, info
    return launch(info, startfile=startfile)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
