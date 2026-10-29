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
should_prompt, never a modal; install_update(tag, manual, on_done) opens the
update window (see the last section) and quits; "Install updates when I quit"
downloads ahead and runs the installer quietly from quit_app. Wave 4 review:
install_update refuses (on_done / a notification says why) while a dictation
or meeting records or a file job runs - installing quits the app; the notice
waits while a meeting records; a dismissed (skipped) tag is never fetched
ahead nor launched at quit.
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
* Install: main.AppController.install_update(tag, manual, on_done) - one entry for the Settings
  button, the tray notice and the update window's Try again. A worker fetches the installer with
  cached_download(tag, on_progress, should_cancel) (the same download the quiet fetch ahead for
  "Install updates when I quit" makes: one file, kept in %TEMP%\\gusan-update\\<tag>\\ and reused
  by later runs once its sha256 checks out; other releases' copies are removed, all of them by
  prune_cache() once the app is up to date); then launch_installer() hands over and the app
  quits through quit_app. pending_update_version is kept until the new version actually runs
  (cfg["update_attempt"] tells the next start whether it worked). A copy that can't update
  itself (a Mac: install() / launch_installer() return UNSUPPORTED; the portable zip, python
  main.py: can_self_install() is False) opens updater.release_page(tag) instead. install() and
  launch() remain the plain wizard path, used by Settings only when there is no controller.
  "Install updates when I quit": cached_download(tag) ahead of time, then launch_installer(path,
  all_users=False, relaunch=False) from quit_app - per-user installed copies only.
* main._parse_version / _sha256_from_checksum_text / _trusted_update_url /
  _update_info_from_release / _update_info_from_manifest have equals here
  (parse_version, sha256_from_checksum_text, trusted_update_url,
  update_info_from_release, update_info_from_manifest - the last two take
  current/dismissed instead of reading main's module cfg); test_updater
  checks they agree, so main can import them later.

The update window (main.AppController.install_update + ui/update_window.py)
---------------------------------------------------------------------------
Install update shows a window from start to finish instead of a silent ~100 MB download that
ends with the app vanishing and a wizard: progress with Cancel (download(on_progress,
should_cancel) -> CANCELLED), the checksum check, "Installing" with Gusan reopening by itself,
or what went wrong with Try again / the download page.
* can_self_install(): only an installed Windows copy updates itself; a source checkout and the
  portable zip are sent to the download page (release_page).
* The download is never installed unchecked: the release's published sha256 must be readable (else
  Try again), redirects go only to https GitHub hosts, a body without an announced size stops at
  MAX_INSTALLER_BYTES. Checked installers are noted in memory (note_verified), never in a file.
* launch_installer(): Inno Setup run quietly (INSTALL_ARGS) for exactly this copy - /CURRENTUSER,
  or /ALLUSERS with administrator rights asked up front (is_all_users_install; a "No" is
  (False, message) and Gusan keeps running). An all-users start first holds the file unchangeable
  and compares it with the checked sha256 (the folder is writable by the user, the start is not).
  A per-user Setup reopens Gusan itself (/relaunch=1, installer.iss RelaunchAfterUpdate).
* spawn_relauncher(): a small hidden watcher, started as the person, reopens Gusan once Setup
  has finished AND the Gusan that started it has ended - also after a failed install, which the
  reopened app then reports (install_finished(log) tells a finished install from one still running).
* One log per attempt in %TEMP%\\gusan-update\\logs, kept until an update has succeeded
  (prune_logs); every download has its own file when a kept one is locked.
"""

import errno
import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from urllib.parse import urlparse

from app_info import APP_NAME, APP_VERSION

logger = logging.getLogger("transcribe")

PROJECT_GITHUB_URL = "https://github.com/Aram2K/Gusan"
RELEASES_URL = "https://github.com/Aram2K/Gusan/releases/latest"
RELEASES_API = "https://api.github.com/repos/Aram2K/Gusan/releases/latest"
RELEASES_MANIFEST_URL = "https://github.com/Aram2K/Gusan/releases/latest/download/update-manifest.json"
INSTALLER_NAME = "TranscribeApp-Windows-Setup.exe"      # never rename: 1.9.x downloads this
APP_ID = "{8E3B7C8A-9D54-4F61-9F6C-2E8C7F0A1B23}"       # installer.iss AppId (never changes)

AVAILABLE, LATEST, ERROR, DISMISSED = "available", "latest", "error", "dismissed"
UNSUPPORTED = "unsupported"
CANCELLED = "cancelled"          # download/cached_download: the user pressed Cancel

MIN_INSTALLER_BYTES = 1024 * 1024      # anything smaller is an error page, not an installer
MAX_INSTALLER_BYTES = 1024 ** 3        # a body with no announced size stops here (the installer is ~100 MB)
CHUNK = 256 * 1024                     # Cancel is looked at once per chunk
READ_TIMEOUT = 30                      # seconds: a stalled line is noticed (and Cancel acted on)

# The words people read when an update can't be fetched (Settings and the update window show them).
MSG_NOT_A_RELEASE = "That isn't a release we can install."
MSG_NETWORK = "Couldn't download the update. Check your connection and try again."
MSG_NOT_PUBLISHED = "This update isn't ready to download yet. Try again in a few minutes."
MSG_DISK = "There isn't enough free disk space for the update."
MSG_DAMAGED = "The download was damaged. Try again."
MSG_UNCHECKED = "Couldn't check the download. Try again in a few minutes."
MSG_NO_PERMISSION = "Windows asked for permission, and it wasn't given."
MSG_BLOCKED = "Your antivirus blocked the installer."
MSG_NO_START = "Windows wouldn't start the installer."
MSG_FILE_MISSING = "The downloaded update couldn't be found any more. Try again."
MSG_SETUP_RUNNING = "Another installer is still running. Try again in a minute."
# What a dictation, a meeting or a file job is told once the installer is running.
UPDATING_TITLE = "%s is updating" % APP_NAME
UPDATING_TEXT = "It closes and opens again in a moment."

# Inno Setup, run quietly: its own small progress window, no wizard pages and no Cancel (a Cancel
# would leave Gusan closed and not updated). Never the fresh-install "start at login" task: an
# update keeps the person's setting (the script also skips it for an upgrade, this makes sure even
# when Setup misjudges one). /NORESTARTAPPLICATIONS: Setup's Restart Manager must not start Gusan
# again itself (it would be elevated after an all-users install) - the app comes back through
# /relaunch=1 or the watcher below, as the person.
INSTALL_ARGS = ("/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NOCANCEL", "/NORESTART",
                "/NORESTARTAPPLICATIONS", '/MERGETASKS="!startup"')
# How long the watcher waits for Setup before it reopens Gusan anyway - it never stays gone.
RELAUNCH_TIMEOUT_MIN = 20
SETUP_PROCESS_PATTERN = "TranscribeApp-Windows-Setup*"      # Setup (and its Inno copy) as Task Manager names them

# "v2.0.1", "2.0", "v2.0.0-beta.1": digits, then an optional suffix of words split by single dots or
# dashes (never ".." and never a line break: `$` alone lets "v2.0.1\n" through, so valid_tag uses fullmatch).
_TAG_RE = re.compile(r"v?\d+(\.\d+){0,2}([.\-+][0-9A-Za-z]+([.\-][0-9A-Za-z]+)*)?")


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
    return bool(tag) and bool(_TAG_RE.fullmatch(str(tag)))


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


# Where GitHub's release downloads may be redirected to (it serves the file from its own CDN).
_GITHUB_HOSTS = ("github.com", "githubusercontent.com")


def trusted_redirect(url):
    """May a download be sent on to ``url``? Only https, and only to GitHub's own hosts: a hostile or
    broken redirect (to plain http, or to some other site) must never decide which bytes get installed."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and any(host == h or host.endswith("." + h) for h in _GITHUB_HOSTS)
    except Exception:
        return False


class _GitHubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted_redirect(newurl):
            logger.warning("Update download: not following a redirect to %s", newurl)
            return None                       # urllib then raises the redirect as an HTTPError
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _urlopen(urlopen):
    if urlopen is not None:
        return urlopen
    opener = urllib.request.build_opener(_GitHubRedirects)

    def _open(url, timeout=60):
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        return opener.open(req, timeout=timeout)
    return _open


def _expected_sha256(tag, opener):
    """The published checksum of release ``tag``: (sha256, "") - or ("", the words to show) when it can't
    be read. Every release publishes one next to the installer, and an installer nobody could check is
    never run (it is started quietly, for an all-users copy with administrator rights): a missing or
    unreadable checksum is a failure with Try again, not a reason to install unchecked."""
    try:
        with opener(checksum_url(tag), timeout=20) as resp:
            found = sha256_from_checksum_text(resp.read(4096).decode("ascii", "ignore"))
    except Exception as e:
        logger.info("No checksum for %s: %s", tag, e)
        return "", (MSG_UNCHECKED if getattr(e, "code", None) == 404 else MSG_NETWORK)
    if not found:
        logger.info("The checksum file of %s holds no sha256", tag)
        return "", MSG_UNCHECKED
    return found, ""


def _announced_size(resp):
    """The size the server announced (Content-Length), 0 when it didn't (or the stand-in has no headers)."""
    try:
        headers = getattr(resp, "headers", None)
        value = headers.get("Content-Length") if headers is not None else None
        return max(0, int(value or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def _failure_message(error):
    """The one plain sentence for a download that broke."""
    if getattr(error, "code", None) == 404:          # urllib.error.HTTPError
        return MSG_NOT_PUBLISHED
    if getattr(error, "errno", None) == errno.ENOSPC or getattr(error, "winerror", None) in (39, 112):
        return MSG_DISK
    return MSG_NETWORK


def download(tag, *, urlopen=None, dest_dir=None, verify=True, expected=None,
             on_progress=None, should_cancel=None, min_bytes=None, name=None):
    """Download the Windows installer for ``tag`` and check it against the release's published sha256
    (``expected``: that checksum when the caller already read it, None reads it here). A release whose
    checksum can't be read is not installed: the answer is (False, message), before anything is
    downloaded. ``verify=False`` (tests, never the app) skips the check. Blocking (worker). Returns
    (True, path), (False, message) or (False, CANCELLED). A downloaded installer is noted as checked
    (note_verified), so launch_installer can tell it is still the same bytes.

    ``on_progress(done, total)`` after every chunk (total 0 = the server did not
    say; the last call is always (size, size)); ``should_cancel()`` is asked once
    per chunk - a cancelled download removes its partial file. A body shorter than
    announced, longer than MAX_INSTALLER_BYTES, or smaller than ``min_bytes`` (default
    MIN_INSTALLER_BYTES: an error page, not an installer), is refused and removed.
    ``name``: the file name (a unique one when a kept file is locked)."""
    if not valid_tag(tag):
        return False, MSG_NOT_A_RELEASE
    url = installer_url(tag)
    if not trusted_update_url(url):
        return False, MSG_NOT_A_RELEASE
    opener = _urlopen(urlopen)
    floor = MIN_INSTALLER_BYTES if min_bytes is None else int(min_bytes)
    cancelled = should_cancel or (lambda: False)
    if verify:
        problem = ""
        if expected is None:
            expected, problem = _expected_sha256(tag, opener)
        if not expected:
            return (False, CANCELLED) if cancelled() else (False, problem or MSG_UNCHECKED)
    dest = os.path.join(dest_dir or tempfile.gettempdir(), name or INSTALLER_NAME)
    digest = hashlib.sha256()
    done = total = 0
    stopped = False
    try:
        with opener(url, timeout=READ_TIMEOUT) as resp, open(dest, "wb") as out:
            total = _announced_size(resp)
            while True:
                if cancelled():
                    stopped = True
                    break
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
                done += len(chunk)
                _progress(on_progress, done, total)
                if done > MAX_INSTALLER_BYTES:
                    break                     # no announced size and no end: never fill the disk
    except Exception as e:
        _remove(dest)
        if cancelled():                       # the stall they cancelled, timing out
            return False, CANCELLED
        logger.warning("Update download failed: %s", e)
        return False, _failure_message(e)
    if stopped or cancelled():
        _remove(dest)
        return False, CANCELLED
    if (total and done != total) or done < floor or done > MAX_INSTALLER_BYTES:
        logger.warning("Update %s: %d bytes arrived, %d announced - not installing it", tag, done, total)
        _remove(dest)
        return False, MSG_NETWORK
    if not total:
        _progress(on_progress, done, done)
    if verify:
        if expected != digest.hexdigest():
            logger.warning("Update %s failed its checksum", tag)
            _remove(dest)
            return False, MSG_DAMAGED
        note_verified(dest, expected)
    return True, dest


def _progress(on_progress, done, total):
    if on_progress is None:
        return
    try:
        on_progress(done, total)
    except Exception:                         # a window that is gone must not end the download
        logger.debug("update progress callback failed", exc_info=True)


# ── "Install updates when I quit": one download per release, kept ────────────
CACHE_DIR_NAME = "gusan-update"         # %TEMP%\gusan-update\<tag>\TranscribeApp-Windows-Setup.exe
LOGS_DIR_NAME = "logs"                  # %TEMP%\gusan-update\logs\<attempt>.log (new_log_path)

# The installers THIS run has checked against their published sha256: normalised path -> sha256. Kept in
# memory on purpose - a note next to the file would sit in a folder any program of the same user can
# write, so it would "prove" nothing. launch_installer compares the file to it right before an
# administrator-rights start.
_verified = {}


def cache_root():
    return os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _path_key(path):
    return os.path.normcase(os.path.abspath(path))


def note_verified(path, digest):
    """This run checked the installer at ``path``: its bytes hash to ``digest``, the published sha256."""
    _verified[_path_key(path)] = str(digest or "").lower()


def verified_sha256(path):
    """The sha256 this run checked the installer at ``path`` against, "" when it never did."""
    return _verified.get(_path_key(path), "")


def prune_cache(keep=None, root=None):
    """Remove kept installers except release ``keep`` (None: all of them) -
    e.g. once that release is installed. The install logs (the ``logs`` folder:
    prune_logs) are not installers and stay. Never raises."""
    import shutil
    root = root or cache_root()
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if (keep and name == keep) or name == LOGS_DIR_NAME:
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            _remove(path)        # a 2.0 pre-release build kept it in the root


def cached_download(tag, *, urlopen=None, root=None, verify=True, on_progress=None, should_cancel=None):
    """The installer for ``tag``, kept in <root>/<tag>/ (root: %TEMP%\\gusan-
    update) and reused across runs - not ~100 MB downloaded every launch. A
    kept file is reused only when its sha256 matches the release's published
    checksum, read again now: with no checksum to compare to (offline, or none
    published) nothing is reused and nothing is installed. Anything that doesn't
    match is downloaded again (under a new name when the old file can't be
    removed - a running installer holds it). Other releases' files are removed.
    Blocking (worker). Returns (True, path), (False, message) or (False,
    CANCELLED); ``on_progress`` / ``should_cancel`` as for download()."""
    if not valid_tag(tag):
        return False, MSG_NOT_A_RELEASE
    root = root or cache_root()
    prune_cache(keep=tag, root=root)
    folder = os.path.join(root, tag)
    path = os.path.join(folder, INSTALLER_NAME)
    opener = _urlopen(urlopen)
    expected, problem = _expected_sha256(tag, opener) if verify else ("", "")
    if should_cancel is not None and should_cancel():
        return False, CANCELLED
    if verify and not expected:
        return False, problem or MSG_UNCHECKED
    name = INSTALLER_NAME
    if os.path.isfile(path):
        try:
            actual = file_sha256(path)
        except OSError:
            actual = ""
        if actual and expected and actual == expected:
            logger.info("Update %s: reusing the installer downloaded earlier", tag)
            note_verified(path, actual)
            _progress(on_progress, 1, 1)
            return True, path
        logger.info("Update %s: the kept installer doesn't check out - downloading again", tag)
        _remove(path)
        if os.path.isfile(path):
            # Still there: a running installer holds it. Each download may have its own file.
            name = "%s-%s.exe" % (INSTALLER_NAME[:-len(".exe")], uuid.uuid4().hex[:8])
            path = os.path.join(folder, name)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        logger.warning("Update folder unavailable: %s", e)
        return False, "Couldn't save the update."
    ok, info = download(tag, urlopen=urlopen, dest_dir=folder, verify=verify, expected=expected,
                        on_progress=on_progress, should_cancel=should_cancel, name=name)
    if not ok:
        return ok, info
    _remove_leftovers(folder, keep=(info,))
    return True, info


def _remove_leftovers(folder, keep=()):
    """Earlier downloads of this release in its folder (a locked one is simply left)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    keep = {os.path.normcase(os.path.abspath(p)) for p in keep}
    for name in names:
        path = os.path.join(folder, name)
        if os.path.normcase(os.path.abspath(path)) not in keep and os.path.isfile(path):
            _remove(path)


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
    """Download, check and run the installer for ``tag`` (the plain wizard, no window of ours). Blocking
    (worker). Returns (True, installer path) - the caller then quits so the installer can replace the
    files - or (False, message). The app itself never uses this (install_update does, with its window
    and a quiet Setup); it stays for a page without a controller. The download goes to the release's
    own folder under %TEMP%\\gusan-update, never to a fixed name in %TEMP% itself."""
    if startfile is None and sys.platform != "win32":
        return False, UNSUPPORTED
    if dest_dir is None and valid_tag(tag):
        dest_dir = os.path.join(cache_root(), tag)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError:
            dest_dir = None
    ok, info = download(tag, urlopen=urlopen, dest_dir=dest_dir, verify=verify)
    if not ok:
        return False, info
    return launch(info, startfile=startfile)


# ── In-app update (the update window): which copy, a quiet install, the watcher ──
def can_self_install(exe=None):
    """Does THIS copy update itself in place? Only an installed Windows copy
    does: the frozen program with its uninstaller (unins*.exe) next to it. The
    portable zip and a source checkout would get a second, separate installed
    copy instead - they are sent to the download page. ``exe``: a program path
    to judge (tests); None is this program."""
    if sys.platform != "win32":
        return False
    if exe is None and not getattr(sys, "frozen", False):
        return False
    folder = os.path.dirname(exe or sys.executable)
    try:
        return any(n.lower().startswith("unins") and n.lower().endswith(".exe") for n in os.listdir(folder))
    except OSError:
        return False


def _registry_install_folders():
    """Where the per-user (HKCU) and all-users (HKLM) installs of Gusan are, from
    Setup's uninstall entries: {"HKCU": [folders], "HKLM": [folders]}."""
    if sys.platform != "win32":
        return {}
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\%s_is1" % APP_ID
    found = {"HKCU": [], "HKLM": []}
    for name, hive, views in (("HKCU", winreg.HKEY_CURRENT_USER, (0,)),
                              ("HKLM", winreg.HKEY_LOCAL_MACHINE,
                               (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY))):
        for view in views:
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as k:
                    for value in ("Inno Setup: App Path", "InstallLocation"):
                        try:
                            found[name].append(str(winreg.QueryValueEx(k, value)[0]))
                        except OSError:
                            pass
            except OSError:
                continue
    return found


def is_all_users_install(exe=None, registry=None):
    """Is THIS copy installed for all users (Program Files, or the all-users
    uninstall entry points at its folder)? Then Setup needs administrator
    rights, asked for up front so a "No" leaves Gusan running instead of closed.
    Another account's all-users copy elsewhere on the PC doesn't count (updating
    that one would leave this copy old for ever). ``registry()`` ->
    {"HKCU": [folders], "HKLM": [folders]} (tests)."""
    exe = os.path.normcase(os.path.abspath(exe or sys.executable))
    folder = os.path.dirname(exe)
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        root = os.environ.get(var)
        if root and exe.startswith(os.path.normcase(os.path.abspath(root)) + os.sep):
            return True
    try:
        found = (registry or _registry_install_folders)()
    except Exception:
        return False

    def here(paths):
        return any(os.path.normcase(os.path.abspath(p.rstrip("\\/"))) == folder for p in paths if p)
    if here(found.get("HKCU", ())):
        return False                          # the per-user entry wins
    return here(found.get("HKLM", ()))


def reset_child_env(environ):
    """Anything this app starts that later starts a NEW Gusan (Setup, its
    reopening, the watcher) must not hand it this bundle's private PyInstaller
    variables - PyInstaller's documented way to start an independent instance.
    Changes and returns ``environ``."""
    for key in [k for k in environ if k.startswith("_PYI_") or k == "_MEIPASS2"]:
        environ.pop(key, None)
    environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environ


def install_command(path, *, all_users=False, log_path=None, relaunch=False):
    """(file, verb, arguments) of the quiet install - what launch_installer hands to the shell.
    A per-user install is always asked for explicitly (/CURRENTUSER): Setup must never pick another
    account's all-users copy instead. ``relaunch``: Setup reopens Gusan itself when it succeeds (a
    per-user in-app update only: an elevated Setup cannot start anything as the person)."""
    args = list(INSTALL_ARGS)
    args.append('/LOG="%s"' % (log_path or new_log_path()))
    args.append("/ALLUSERS" if all_users else "/CURRENTUSER")
    if relaunch and not all_users:
        args.append("/relaunch=1")
    return path, ("runas" if all_users else "open"), " ".join(args)


def _open_unchangeable(path):
    """``path`` opened for reading so that nothing - no program of the same user either - can write to
    it, replace it or delete it until the file is closed (Windows: share mode FILE_SHARE_READ only;
    where the system has no such thing, a plain read)."""
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 0x80000000, 0x1, 3, 0x80
    handle = kernel32.CreateFileW(os.path.abspath(path), GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING,
                                  FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb")


def _hold_checked_installer(path):
    """(open file, "") when the installer at ``path`` is exactly what this run downloaded and checked, and
    nobody can change it while the file is held; else (None, the words to show). It is held across the
    start: an all-users start waits for a Windows permission answer that can take a while, and the
    folder %TEMP%\\gusan-update belongs to the user - anything running as the same user could swap the
    file in the meantime, and it would then run with administrator rights."""
    expected = verified_sha256(path)
    if not expected:
        logger.warning("The installer was not checked by this run - not starting it with administrator rights")
        return None, MSG_DAMAGED
    try:
        held = _open_unchangeable(path)
    except FileNotFoundError:
        return None, MSG_FILE_MISSING
    except OSError as e:
        logger.warning("Could not hold the installer: %s", e)
        return None, MSG_NO_START
    try:
        digest = hashlib.sha256()
        for chunk in iter(lambda: held.read(1024 * 1024), b""):
            digest.update(chunk)
    except OSError as e:
        held.close()
        logger.warning("Could not read the installer: %s", e)
        return None, MSG_NO_START
    if digest.hexdigest() != expected:
        held.close()
        logger.warning("The installer changed after it was checked - not starting it")
        return None, MSG_DAMAGED
    return held, ""


def launch_installer(path, *, all_users=False, log_path=None, relaunch=False, start=None, environ=None,
                     verify=True):
    """Start the quiet install through the shell. All users: as administrator
    ("runas"), so Windows' permission prompt comes now - a "No" comes back here
    as (False, message) and Gusan stays open. Blocking until the prompt is
    answered (worker). Returns (True, path), or (False, message) - on a
    system with no installer to run (False, UNSUPPORTED).

    All users, ``verify`` (the default): the file must be the one this run downloaded and checked
    (note_verified) and is held unchangeable until Windows has started it (_hold_checked_installer),
    or nothing starts: (False, MSG_DAMAGED). A per-user start gains nothing from that - the same user
    could run anything anyway - and only costs the time to read ~100 MB again."""
    if start is None:
        if sys.platform != "win32":
            return False, UNSUPPORTED
        start = os.startfile
    file, verb, args = install_command(path, all_users=all_users, log_path=log_path, relaunch=relaunch)
    held = None
    if all_users and verify:
        held, problem = _hold_checked_installer(path)
        if held is None:
            return False, problem
    try:
        # The shell hands this process's environment on to Setup and to what it starts.
        reset_child_env(os.environ if environ is None else environ)
        try:
            start(file, verb, args, cwd=os.path.dirname(os.path.abspath(file)))
        except Exception as e:
            logger.warning("Could not start the installer: %s", e)
            winerror = getattr(e, "winerror", None)
            if winerror == 1223:
                return False, MSG_NO_PERMISSION
            if winerror == 225:
                return False, MSG_BLOCKED
            return False, MSG_NO_START
    finally:
        if held is not None:
            held.close()
    return True, path


# One log per attempt, in %TEMP%\gusan-update\logs: the watcher waits for THIS one to close, and
# the next start reads it to tell a finished install from a stopped one.
def logs_dir(root=None):
    return os.path.join(root or cache_root(), LOGS_DIR_NAME)


def new_log_path(root=None):
    folder = logs_dir(root)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        folder = tempfile.gettempdir()
    return os.path.join(folder, "gusan-update-%s.log" % uuid.uuid4().hex[:8])


def prune_logs(root=None):
    """Remove the install logs (and the then empty update folder). Only when Gusan is up to date: the
    log of an install that stopped must survive until an update succeeds. Never raises."""
    root = root or cache_root()
    folder = logs_dir(root)
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        _remove(os.path.join(folder, name))
    for path in (folder, root):
        try:
            os.rmdir(path)           # only while empty: a kept installer's folder stays
        except OSError:
            pass


def install_finished(log_path):
    """Has the install that writes ``log_path`` ended - cleanly or not? True when its log says
    "Log closed." (Setup's last line), or when there is no readable log (Setup never started).
    False while Setup is still writing it. A killed Setup never writes the last line: the watcher
    judges that by the process, this by the log only."""
    if not log_path:
        return True
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            return b"Log closed." in f.read()
    except PermissionError:
        return False                 # Setup holds it open for writing: still running
    except OSError:
        return True


def setup_running():
    """Is a Gusan Setup process alive right now? True / False, None when that can't be told (no
    psutil, no access). Setup shows up in Task Manager under the installer's name (the copy it
    unpacks to %TEMP% too), which is also what the watcher looks for."""
    try:
        import psutil
        prefix = SETUP_PROCESS_PATTERN.rstrip("*").lower()
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower().startswith(prefix):
                return True
        return False
    except Exception:
        logger.debug("Could not look for a running Setup", exc_info=True)
        return None


def install_running(log_path, *, running=None, now=None):
    """Is the install that writes ``log_path`` still working? Judged when Gusan starts again: a log
    that never says "Log closed." means Setup is still going - or was killed (End task, a power cut),
    which looks the same in the log. So the process decides (``running``: setup_running()); when it
    can't be told, a log that was written in the last two minutes is a Setup that is still going."""
    if install_finished(log_path):
        return False
    alive = setup_running() if running is None else running
    if alive is not None:
        return bool(alive)
    import time
    try:
        return (now or time.time()) - os.path.getmtime(log_path) < 120
    except OSError:
        return False


def _powershell():
    """By full path, always: CreateProcess doesn't search PowerShell's folder, so a trimmed PATH must not
    decide whether Gusan comes back - and a bare "powershell.exe" would be looked for in the application's
    own folder first, which a per-user install lets the person write to. A missing file simply means no
    watcher (spawn_relauncher returns None)."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


# The watcher, in PowerShell, started as the person right after Setup starts: it reopens Gusan
# once Setup has finished - also after a FAILED install, so Gusan never just vanishes - and as the
# person, never elevated (which an elevated Setup cannot promise).
# * The paths come in through the environment, never inside the script text: no quoting can break
#   it (PowerShell ends a '...' string at typographic quotes too - a folder like D’Souza).
# * It stops waiting when the log says "Log closed.", or when no Setup process has been alive for
#   3 polls in a row - with or without a log: a killed Setup never writes the last line, and one
#   that was blocked at birth never writes a log at all - or after RELAUNCH_TIMEOUT_MIN minutes
#   whatever happens. A Setup that is alive but has no log yet is waited for.
# * The deadlines are UTC: a clock change never cuts them short.
# * It first waits (at most 90 s) for the Gusan that started it to be gone (GUSAN_RELAUNCH_PID): a Setup
#   that fails at once has closed its log before that Gusan has finished quitting, and "Gusan runs" would
#   then be the answer to "may I reopen it?" for a Gusan that is about to end.
# * A per-user Setup reopens Gusan itself a moment before its log closes: when Gusan already runs
#   again the watcher starts nothing (a second launch would only bring the window forward once more).
RELAUNCH_SCRIPT = (
    "$log = $env:GUSAN_RELAUNCH_LOG; "
    "$exe = $env:GUSAN_RELAUNCH_EXE; "
    "$setup = $env:GUSAN_RELAUNCH_SETUP; "
    "$minutes = [double]$env:GUSAN_RELAUNCH_MINUTES; "
    "$oldpid = [int]$env:GUSAN_RELAUNCH_PID; "
    "$end = [DateTime]::UtcNow.AddMinutes($minutes); "
    "$gone = 0; "
    "do { "
    "Start-Sleep -Seconds 2; "
    "$done = $false; "
    "if (@(Get-Process -Name $setup -ErrorAction SilentlyContinue).Count -eq 0) { $gone++ } else { $gone = 0 }; "
    "if ($gone -ge 3) { $done = $true }; "
    "if ((Test-Path -LiteralPath $log) -and "
    "(Select-String -LiteralPath $log -SimpleMatch 'Log closed.' -Quiet -ErrorAction SilentlyContinue)) { $done = $true } "
    "} until ($done -or ([DateTime]::UtcNow -gt $end)); "
    "$name = [IO.Path]::GetFileNameWithoutExtension($exe); "
    "$wait = [DateTime]::UtcNow.AddSeconds(90); "
    "while ($oldpid -gt 0 -and [DateTime]::UtcNow -lt $wait) { "
    "$old = Get-Process -Id $oldpid -ErrorAction SilentlyContinue; "
    "if ($null -eq $old -or $old.ProcessName -ne $name) { break }; "
    "Start-Sleep -Milliseconds 500 }; "
    "if (@(Get-Process -Name $name -ErrorAction SilentlyContinue).Count -eq 0) { "
    "Start-Process -FilePath $exe -ArgumentList '--show-settings' -WorkingDirectory (Split-Path -Parent $exe) }"
)


def spawn_relauncher(exe, log_path, popen=None):
    """Start the hidden watcher (RELAUNCH_SCRIPT) that reopens ``exe`` once the install writing
    ``log_path`` has finished. Start it once Setup is running. Returns the process, or None when it
    could not be started (PowerShell blocked by policy, say) - the caller then says Gusan will not
    come back by itself."""
    if sys.platform != "win32" and popen is None:
        return None
    env = reset_child_env(dict(os.environ))
    env.update(GUSAN_RELAUNCH_EXE=str(exe), GUSAN_RELAUNCH_LOG=str(log_path),
               GUSAN_RELAUNCH_MINUTES=str(RELAUNCH_TIMEOUT_MIN), GUSAN_RELAUNCH_SETUP=SETUP_PROCESS_PATTERN,
               GUSAN_RELAUNCH_PID=str(os.getpid()))        # the Gusan that quits: reopen only after it is gone
    cmd = [_powershell(), "-NoProfile", "-NonInteractive", "-Command", RELAUNCH_SCRIPT]
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    breakaway = 0x01000000                     # CREATE_BREAKAWAY_FROM_JOB: outlive Gusan
    popen = popen or subprocess.Popen
    for flags in (no_window | breakaway, no_window):
        try:
            return popen(cmd, creationflags=flags, close_fds=True, env=env, cwd=tempfile.gettempdir(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            logger.debug("Relauncher not started (flags %#x): %s", flags, e)
    logger.warning("Couldn't start the update relauncher")
    return None


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
