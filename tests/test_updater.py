"""updater: the check (dismissed versions stay quiet), the install (sha256 checked),
the in-app update pieces (a download with progress and Cancel, which copy updates itself,
the quiet Setup command line, the watcher that reopens Gusan) and the kept identifiers
1.9.x installs update through. Fake HTTP, temp files - no Qt, no Windows needed."""
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater  # noqa: E402


def resp(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


class _Body(io.BytesIO):
    """What urlopen hands back: the bytes, and (like a real response) the headers."""

    def __init__(self, data, headers=None):
        super().__init__(data)
        if headers is not None:
            self.headers = headers


class FakeOpener:
    """urlopen stand-in: url -> bytes, or an exception to raise. ``announce``: the installer's
    response carries its Content-Length (a real one does; a bare BytesIO has no headers)."""

    def __init__(self, files, announce=False, on_open=None):
        self.files = files
        self.urls = []
        self.timeouts = []
        self.announce = announce
        self.on_open = on_open

    def __call__(self, url, timeout=60):
        self.urls.append(url)
        self.timeouts.append(timeout)
        if self.on_open is not None:
            self.on_open(url)
        data = self.files.get(url)
        if data is None or isinstance(data, Exception):
            raise data or OSError("404")
        if self.announce and url.endswith(updater.INSTALLER_NAME):
            size = len(data) if self.announce is True else int(self.announce)       # (an int: announces more than it sends)
            return _Body(data, {"Content-Length": str(size)})
        return _Body(data)


def _small_installers(case):
    """These tests use a 17 KB stand-in installer: the 1 MB floor (an error page is not an
    installer) is switched off for them, and tested on its own."""
    patcher = mock.patch.object(updater, "MIN_INSTALLER_BYTES", 0)
    patcher.start()
    case.addCleanup(patcher.stop)


class TestKeptIdentifiers(unittest.TestCase):
    def test_repo_api_and_asset_names(self):
        self.assertEqual(updater.RELEASES_API, "https://api.github.com/repos/Aram2K/Gusan/releases/latest")
        self.assertEqual(updater.INSTALLER_NAME, "TranscribeApp-Windows-Setup.exe")
        self.assertEqual(updater.installer_url("v2.0.1"),
                         "https://github.com/Aram2K/Gusan/releases/download/v2.0.1/"
                         "TranscribeApp-Windows-Setup.exe")
        self.assertEqual(updater.checksum_url("v2.0.1"), updater.installer_url("v2.0.1") + ".sha256")


class TestCheck(unittest.TestCase):
    def test_statuses(self):
        get = MagicMock(return_value=resp(200, {"tag_name": "v2.0.1"}))
        self.assertEqual(updater.check_latest({}, get=get, current="2.0.0"), ("available", "v2.0.1"))
        self.assertEqual(get.call_args[0][0], updater.RELEASES_API)
        self.assertEqual(get.call_args[1]["proxies"], {"http": None, "https": None})
        self.assertEqual(updater.check_latest({}, get=get, current="2.0.1"), ("latest", "v2.0.1"))
        self.assertEqual(updater.check_latest({}, get=MagicMock(return_value=resp(500)), current="2.0.0"),
                         ("error", ""))
        self.assertEqual(updater.check_latest({}, get=MagicMock(side_effect=OSError), current="2.0.0"),
                         ("error", ""))
        self.assertEqual(updater.check_latest({}, get=MagicMock(return_value=resp(200, {})), current="2.0.0"),
                         ("error", ""))

    def test_dismissed_is_quiet_unless_checked_by_hand(self):
        cfg = {"dismissed_update_version": "v2.0.1"}
        get = MagicMock(return_value=resp(200, {"tag_name": "v2.0.1"}))
        self.assertEqual(updater.check_latest(cfg, get=get, current="2.0.0"), ("dismissed", "v2.0.1"))
        self.assertEqual(updater.check_latest(cfg, manual=True, get=get, current="2.0.0"), ("available", "v2.0.1"))
        self.assertFalse(updater.should_prompt(cfg, "v2.0.1", current="2.0.0"))
        self.assertTrue(updater.should_prompt(cfg, "v2.0.2", current="2.0.0"))
        self.assertEqual(updater.dismiss_changes("v2.0.1"), {"dismissed_update_version": "v2.0.1"})

    def test_pending_and_props(self):
        self.assertEqual(updater.pending_after_check({}, "available", "v2.0.1"), "v2.0.1")
        self.assertIsNone(updater.pending_after_check({"pending_update_version": "v2.0.1"}, "available", "v2.0.1"))
        self.assertEqual(updater.pending_after_check({"pending_update_version": "v2.0.1"}, "latest", "v2.0.1"), "")
        self.assertIsNone(updater.pending_after_check({}, "latest", "v2.0.0"))
        self.assertIsNone(updater.pending_after_check({"pending_update_version": "v2.0.1"}, "error", ""))
        self.assertEqual(updater.check_props("error", True), {"manual": True, "failed": True})
        self.assertEqual(updater.check_props("latest"), {"manual": False, "available": False})

    def test_status_line(self):
        self.assertEqual(updater.status_line("latest", "v2.0.0", current="2.0.0"), ("Gusan 2.0.0", "Up to date"))
        self.assertEqual(updater.status_line("available", "v2.0.1", current="2.0.0")[1], "Update available · v2.0.1")
        self.assertEqual(updater.status_line("error", current="2.0.0")[1], "Couldn't check right now")

    def test_versions(self):
        self.assertTrue(updater.is_newer("v2.0.0", "1.9.4"))
        self.assertFalse(updater.is_newer("v1.9.4", "2.0.0"))
        self.assertFalse(updater.is_newer("", "2.0.0"))
        self.assertTrue(updater.valid_tag("v2.0.1"))
        self.assertFalse(updater.valid_tag("v2.0.1/../../evil"))
        self.assertFalse(updater.valid_tag(""))


class TestInstall(unittest.TestCase):
    def setUp(self):
        _small_installers(self)
        self.dir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.exe = b"MZ fake installer" * 1000
        self.sha = hashlib.sha256(self.exe).hexdigest()
        self.tag = "v2.0.1"

    def _cleanup(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def _opener(self, checksum):
        files = {updater.installer_url(self.tag): self.exe}
        if checksum is not None:
            files[updater.checksum_url(self.tag)] = checksum
        return FakeOpener(files)

    def test_verified_install_runs_the_installer(self):
        started = []
        opener = self._opener(f"{self.sha}  TranscribeApp-Windows-Setup.exe\n".encode())
        ok, path = updater.install(self.tag, urlopen=opener, startfile=started.append, dest_dir=self.dir)
        self.assertTrue(ok)
        self.assertEqual(started, [path])
        self.assertEqual(os.path.basename(path), "TranscribeApp-Windows-Setup.exe")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.exe)

    def test_damaged_download_is_refused_and_removed(self):
        started = []
        opener = self._opener(("0" * 64).encode())
        ok, msg = updater.install(self.tag, urlopen=opener, startfile=started.append, dest_dir=self.dir)
        self.assertEqual((ok, msg), (False, "The download was damaged. Try again."))
        self.assertEqual(started, [])
        self.assertEqual(os.listdir(self.dir), [])

    def test_release_without_a_checksum_still_installs(self):
        started = []
        ok, _ = updater.install(self.tag, urlopen=self._opener(None), startfile=started.append, dest_dir=self.dir)
        self.assertTrue(ok)
        self.assertEqual(len(started), 1)

    def test_download_failure_and_bad_tag(self):
        opener = FakeOpener({})
        ok, msg = updater.install(self.tag, urlopen=opener, startfile=lambda p: None, dest_dir=self.dir)
        self.assertFalse(ok)
        self.assertIn("Couldn't download", msg)
        self.assertEqual(os.listdir(self.dir), [])
        ok, _ = updater.install("v1/../../x", urlopen=opener, startfile=lambda p: None, dest_dir=self.dir)
        self.assertFalse(ok)

    def test_installer_that_wont_start(self):
        def boom(path):
            raise OSError("blocked")
        ok, msg = updater.install(self.tag, urlopen=self._opener(None), startfile=boom, dest_dir=self.dir)
        self.assertEqual((ok, msg), (False, "Couldn't start the installer."))

    def test_release_page(self):
        self.assertEqual(updater.release_page("v2.0.1"),
                         "https://github.com/Aram2K/Gusan/releases/tag/v2.0.1")
        self.assertEqual(updater.release_page(None), updater.RELEASES_URL)


class TestKeptAcrossRuns(unittest.TestCase):
    """"Install updates when I quit": one download per release, reused by the
    next runs only when its sha256 still checks out."""

    def setUp(self):
        _small_installers(self)
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.exe = b"MZ fake installer" * 1000
        self.sha = hashlib.sha256(self.exe).hexdigest()
        self.tag = "v2.0.1"

    def _opener(self, checksum=True, installer=True):
        files = {}
        if installer:
            files[updater.installer_url(self.tag)] = self.exe
        if checksum:
            files[updater.checksum_url(self.tag)] = f"{self.sha}  {updater.INSTALLER_NAME}\n".encode()
        return FakeOpener(files)

    def _get(self, opener):
        return updater.cached_download(self.tag, urlopen=opener, root=self.root)

    def test_downloaded_once_then_reused(self):
        first = self._opener()
        ok, path = self._get(first)
        self.assertTrue(ok)
        self.assertEqual(path, os.path.join(self.root, self.tag, updater.INSTALLER_NAME))
        self.assertIn(updater.installer_url(self.tag), first.urls)
        self.assertEqual(updater._read_record(path + ".sha256"), self.sha)
        second = self._opener()                                    # the next launch
        self.assertEqual(self._get(second), (True, path))
        self.assertNotIn(updater.installer_url(self.tag), second.urls)   # not 100 MB again
        self.assertIn(updater.checksum_url(self.tag), second.urls)       # checked against the release

    def test_a_changed_file_is_downloaded_again(self):
        ok, path = self._get(self._opener())
        with open(path, "ab") as f:
            f.write(b"tampered")
        again = self._opener()
        self.assertEqual(self._get(again), (True, path))
        self.assertIn(updater.installer_url(self.tag), again.urls)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.exe)

    def test_offline_it_trusts_only_the_checksum_noted_at_download(self):
        ok, path = self._get(self._opener())
        offline = FakeOpener({})
        self.assertEqual(self._get(offline), (True, path))           # the noted checksum matches
        with open(path, "ab") as f:
            f.write(b"x")
        ok, msg = self._get(FakeOpener({}))
        self.assertFalse(ok)                                          # changed and can't re-fetch
        self.assertFalse(os.path.exists(path))

    def test_other_releases_are_removed(self):
        old = os.path.join(self.root, "v2.0.0")
        os.makedirs(old)
        with open(os.path.join(old, updater.INSTALLER_NAME), "wb") as f:
            f.write(b"old")
        with open(os.path.join(self.root, updater.INSTALLER_NAME), "wb") as f:
            f.write(b"flat copy from a pre-release build")
        self.assertTrue(self._get(self._opener())[0])
        self.assertEqual(os.listdir(self.root), [self.tag])
        updater.prune_cache(root=self.root)                           # up to date: none kept
        self.assertEqual(os.listdir(self.root), [])

    def test_a_damaged_download_is_not_kept(self):
        opener = FakeOpener({updater.installer_url(self.tag): self.exe,
                             updater.checksum_url(self.tag): ("0" * 64).encode()})
        ok, msg = self._get(opener)
        self.assertEqual((ok, msg), (False, "The download was damaged. Try again."))
        self.assertEqual(os.listdir(os.path.join(self.root, self.tag)), [])
        self.assertFalse(updater.cached_download("v1/../../x", urlopen=opener, root=self.root)[0])

    def test_the_default_folder_is_per_release_under_temp(self):
        self.assertEqual(updater.cache_root(), os.path.join(tempfile.gettempdir(), "gusan-update"))

    def test_progress_and_cancel_go_through_the_kept_download(self):
        seen = []
        opener = self._opener()
        opener.announce = True
        ok, path = updater.cached_download(self.tag, urlopen=opener, root=self.root,
                                           on_progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(ok)
        self.assertEqual(seen[-1], (len(self.exe), len(self.exe)))
        shutil.rmtree(self.root)
        self.assertEqual(updater.cached_download(self.tag, urlopen=self._opener(), root=self.root,
                                                 should_cancel=lambda: True), (False, updater.CANCELLED))
        self.assertFalse(os.path.exists(os.path.join(self.root, self.tag)))        # nothing was started
        big = FakeOpener({updater.installer_url(self.tag): b"x" * (updater.CHUNK * 2 + 10)})
        asked = []
        self.assertEqual(updater.cached_download(self.tag, urlopen=big, root=self.root, verify=False,
                                                 should_cancel=lambda: asked.append(1) or len(asked) > 2),
                         (False, updater.CANCELLED))
        self.assertEqual(os.listdir(os.path.join(self.root, self.tag)), [])         # no partial file left behind

    def test_a_kept_file_that_is_reused_reports_done(self):
        self._get(self._opener())
        seen = []
        ok, _path = updater.cached_download(self.tag, urlopen=self._opener(), root=self.root,
                                            on_progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(ok)
        self.assertEqual(seen[-1][0], seen[-1][1])            # nothing to wait for: a full bar

    def test_a_locked_leftover_never_blocks_the_download_it_gets_its_own_file(self):
        ok, kept = self._get(self._opener())
        with open(kept, "ab") as f:
            f.write(b"damaged")                               # fails its checksum: must be replaced...
        real_remove = os.remove

        def locked(path):                                     # ...but a running installer holds it
            if os.path.basename(path) == updater.INSTALLER_NAME:
                raise PermissionError(32, "being used by another process")
            real_remove(path)
        with mock.patch.object(updater.os, "remove", locked):
            ok, path = self._get(self._opener())
        self.assertTrue(ok)
        self.assertNotEqual(path, kept)
        self.assertEqual(os.path.dirname(path), os.path.dirname(kept))       # the same release folder
        self.assertRegex(os.path.basename(path), r"^TranscribeApp-Windows-Setup-[0-9a-f]{8}\.exe$")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.exe)
        self.assertEqual(updater._read_record(path + ".sha256"), self.sha)

    def test_an_earlier_download_of_the_same_release_is_cleaned_up(self):
        folder = os.path.join(self.root, self.tag)
        os.makedirs(folder)
        stale = os.path.join(folder, "TranscribeApp-Windows-Setup-0123abcd.exe")
        with open(stale, "wb") as f:
            f.write(b"x")
        ok, path = self._get(self._opener())
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(sorted(os.listdir(folder)), sorted([updater.INSTALLER_NAME, updater.INSTALLER_NAME + ".sha256"]))

    def test_pruning_keeps_the_logs_of_an_install_that_stopped(self):
        self._get(self._opener())
        log = updater.new_log_path(self.root)
        with open(log, "w") as f:
            f.write("Setup started")
        updater.prune_cache(root=self.root)                   # up to date: no installer kept...
        self.assertEqual(os.listdir(self.root), ["logs"])
        self.assertTrue(os.path.exists(log))                  # ...but its log stays until an update succeeds
        updater.prune_cache(keep=self.tag, root=self.root)
        self.assertEqual(os.listdir(self.root), ["logs"])


class TestDownload(unittest.TestCase):
    """The download behind the update window: progress, Cancel, what a broken one says."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        # A real size (over the 1 MB floor) in five chunks: Cancel and progress have something to happen between.
        self.exe = b"MZ" + bytes(range(256)) * 4200
        self.sha = hashlib.sha256(self.exe).hexdigest()
        self.tag = "v2.0.1"

    def _opener(self, body=None, checksum="good", announce=True, **kw):
        files = {updater.installer_url(self.tag): self.exe if body is None else body}
        if checksum == "good":
            files[updater.checksum_url(self.tag)] = f"{self.sha}  {updater.INSTALLER_NAME}\n".encode()
        elif checksum:
            files[updater.checksum_url(self.tag)] = checksum.encode()
        return FakeOpener(files, announce=announce, **kw)

    def _download(self, opener, **kw):
        return updater.download(self.tag, urlopen=opener, dest_dir=self.dir, **kw)

    def _left(self):
        return [n for n in os.listdir(self.dir) if n.endswith(".exe")]

    def test_progress_ends_at_the_full_size_with_or_without_an_announced_size(self):
        for announce in (True, False):
            seen = []
            ok, path = self._download(self._opener(announce=announce), on_progress=lambda d, t: seen.append((d, t)))
            self.assertTrue(ok, announce)
            self.assertEqual(seen[-1], (len(self.exe), len(self.exe)), announce)
            self.assertGreater(len(seen), 2)                          # a bar that moves, not one jump
            self.assertEqual([d for d, _t in seen], sorted(d for d, _t in seen))
            if announce:
                self.assertTrue(all(t == len(self.exe) for _d, t in seen))
            else:
                self.assertEqual({t for _d, t in seen[:-1]}, {0})     # size unknown: a busy bar
            os.remove(path)

    def test_a_checked_download_is_the_installer_byte_for_byte(self):
        ok, path = self._download(self._opener())
        self.assertTrue(ok)
        self.assertEqual(os.path.basename(path), updater.INSTALLER_NAME)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.exe)

    def test_the_read_timeout_is_short_enough_for_cancel_to_be_noticed(self):
        opener = self._opener()
        self._download(opener)
        self.assertIn(updater.READ_TIMEOUT, opener.timeouts)
        self.assertLessEqual(updater.READ_TIMEOUT, 30)

    def test_a_damaged_download_is_refused_and_removed(self):
        ok, msg = self._download(self._opener(checksum="0" * 64))
        self.assertEqual((ok, msg), (False, "The download was damaged. Try again."))
        self.assertEqual(self._left(), [])

    def test_cancel_before_the_first_byte(self):
        self.assertEqual(self._download(self._opener(), should_cancel=lambda: True), (False, updater.CANCELLED))
        self.assertEqual(self._left(), [])

    def test_cancel_half_way_removes_the_partial_file(self):
        calls = []

        def cancel_after_one_chunk():
            calls.append(1)
            return len(calls) > 1                                     # asked once per chunk
        seen = []
        result = self._download(self._opener(), should_cancel=cancel_after_one_chunk,
                                on_progress=lambda d, t: seen.append(d))
        self.assertEqual(result, (False, updater.CANCELLED))
        self.assertEqual(seen, [updater.CHUNK])                       # it stopped after the first chunk
        self.assertEqual(self._left(), [])

    def test_a_connection_error_after_cancel_is_a_cancel_not_an_error(self):
        # They pressed Cancel because it stalled; the stall then times out.
        flag = []
        opener = self._opener(on_open=lambda url: flag.append(1) if url.endswith(updater.INSTALLER_NAME) else None)
        opener.files[updater.installer_url(self.tag)] = ConnectionError("timed out")
        self.assertEqual(self._download(opener, should_cancel=lambda: bool(flag)), (False, updater.CANCELLED))
        self.assertEqual(self._left(), [])

    def test_a_dropped_connection_is_worded_for_people(self):
        opener = self._opener()
        opener.files[updater.installer_url(self.tag)] = ConnectionError("reset by peer")
        ok, msg = self._download(opener)
        self.assertEqual((ok, msg), (False, "Couldn't download the update. Check your connection and try again."))

    def test_a_body_that_stops_early_is_never_installed(self):
        cut = self.exe[:len(self.exe) // 2]               # announces the whole file, delivers half
        ok, msg = self._download(self._opener(body=cut, checksum=None, announce=len(self.exe)))
        self.assertEqual((ok, msg), (False, "Couldn't download the update. Check your connection and try again."))
        self.assertEqual(self._left(), [])

    def test_an_error_page_is_not_an_installer(self):
        self.assertEqual(updater.MIN_INSTALLER_BYTES, 1024 * 1024)
        ok, msg = self._download(self._opener(body=b"<html>Not found</html>", checksum=None))
        self.assertFalse(ok)
        self.assertIn("Couldn't download", msg)
        self.assertEqual(self._left(), [])
        # an explicit floor, as the caller's own choice
        ok, _ = self._download(self._opener(body=b"tiny", checksum=None), min_bytes=0)
        self.assertTrue(ok)

    def test_a_missing_installer_says_so(self):
        opener = self._opener()
        opener.files[updater.installer_url(self.tag)] = urllib.error.HTTPError(
            updater.installer_url(self.tag), 404, "Not Found", {}, None)
        ok, msg = self._download(opener)
        self.assertEqual((ok, msg), (False, "This version's installer isn't on GitHub yet - try again in a few minutes."))

    def test_a_full_disk_says_so(self):
        for error in (OSError(28, "No space left on device"), _winerror(OSError(22, "disk full"), 112),
                      _winerror(OSError(22, "handle disk full"), 39)):
            with mock.patch.object(updater, "open", create=True, side_effect=error):
                ok, msg = self._download(self._opener())
            self.assertEqual((ok, msg), (False, "There isn't enough free disk space for the update."))

    def test_a_bad_tag_is_refused_before_anything_is_asked(self):
        opener = self._opener()
        for tag in ("v2.0.1/../../x", "", None):
            self.assertEqual(updater.download(tag, urlopen=opener, dest_dir=self.dir),
                             (False, "That isn't a release we can install."))
        self.assertEqual(opener.urls, [])

    def test_a_progress_callback_that_fails_does_not_end_the_download(self):
        def broken(done, total):
            raise RuntimeError("the window is gone")
        ok, path = self._download(self._opener(), on_progress=broken)
        self.assertTrue(ok)

    def test_the_download_has_its_own_name_when_asked(self):
        ok, path = self._download(self._opener(), name="TranscribeApp-Windows-Setup-abcd1234.exe")
        self.assertEqual(os.path.basename(path), "TranscribeApp-Windows-Setup-abcd1234.exe")


def _winerror(err, code):
    err.winerror = code
    return err


class TestInstallKind(unittest.TestCase):
    """Which copy updates itself, and for whom."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_only_an_installed_copy_updates_itself(self):
        exe = os.path.join(self.dir, "TranscribeApp.exe")
        with mock.patch.object(updater.sys, "platform", "win32"):
            self.assertFalse(updater.can_self_install(exe))           # the portable zip: no uninstaller
            with open(os.path.join(self.dir, "unins000.exe"), "wb") as f:
                f.write(b"x")
            self.assertTrue(updater.can_self_install(exe))
            self.assertFalse(updater.can_self_install(os.path.join(self.dir, "missing", "TranscribeApp.exe")))

    def test_this_program_must_be_the_frozen_one(self):
        with open(os.path.join(self.dir, "unins000.exe"), "wb") as f:
            f.write(b"x")
        with mock.patch.object(updater.sys, "platform", "win32"), \
                mock.patch.object(updater.sys, "executable", os.path.join(self.dir, "TranscribeApp.exe")):
            with mock.patch.object(updater.sys, "frozen", False, create=True):
                self.assertFalse(updater.can_self_install())          # python main.py: a source checkout
            with mock.patch.object(updater.sys, "frozen", True, create=True):
                self.assertTrue(updater.can_self_install())

    def test_a_mac_or_linux_copy_never_updates_itself(self):
        with open(os.path.join(self.dir, "unins000.exe"), "wb") as f:
            f.write(b"x")
        for platform in ("darwin", "linux"):
            with mock.patch.object(updater.sys, "platform", platform):
                self.assertFalse(updater.can_self_install(os.path.join(self.dir, "TranscribeApp.exe")))

    def _all_users(self, exe, registry, program_files=""):
        env = {"ProgramFiles": program_files, "ProgramFiles(x86)": "", "ProgramW6432": ""}
        with mock.patch.dict(os.environ, env):
            return updater.is_all_users_install(exe, registry=registry)

    def test_all_users_install_detection(self):
        pf = os.path.join(self.dir, "Program Files")
        mine = os.path.join(self.dir, "Users", "a", "Programs", "Gusan", "TranscribeApp.exe")
        elsewhere = os.path.join(self.dir, "Apps", "Gusan", "TranscribeApp.exe")
        under_pf = os.path.join(pf, "Gusan", "TranscribeApp.exe")
        self.assertTrue(self._all_users(under_pf, lambda: {}, pf))                 # Program Files
        self.assertFalse(self._all_users(mine, lambda: {}, pf))                    # this user's own folder
        # Another account's all-users copy elsewhere is not THIS copy.
        self.assertFalse(self._all_users(mine, lambda: {"HKLM": [os.path.dirname(under_pf)]}, pf))
        # Installed for all users outside Program Files: the entry points at this folder (a trailing slash too).
        self.assertTrue(self._all_users(elsewhere, lambda: {"HKLM": [os.path.dirname(elsewhere) + os.sep]}))
        # Both entries point here (odd, but): the per-user one wins.
        both = {"HKCU": [os.path.dirname(elsewhere)], "HKLM": [os.path.dirname(elsewhere)]}
        self.assertFalse(self._all_users(elsewhere, lambda: both))
        self.assertFalse(self._all_users(elsewhere, lambda: {"HKCU": [os.path.dirname(elsewhere)]}))

    def test_a_registry_that_cannot_be_read_means_per_user(self):
        def broken():
            raise OSError("registry unavailable")
        self.assertFalse(self._all_users(os.path.join(self.dir, "x", "TranscribeApp.exe"), broken))

    @unittest.skipUnless(os.name == "nt", "Windows paths are not case sensitive")
    def test_windows_paths_ignore_case(self):
        self.assertTrue(self._all_users(r"D:\Apps\Gusan\TranscribeApp.exe",
                                        lambda: {"HKLM": ["d:\\apps\\gusan\\"]}))
        self.assertTrue(self._all_users(r"C:\PROGRAM FILES\Gusan\TranscribeApp.exe", lambda: {},
                                        r"C:\Program Files"))

    def test_the_registry_lookup(self):
        found = updater._registry_install_folders()
        if sys.platform == "win32":
            self.assertEqual(set(found), {"HKCU", "HKLM"})
        else:
            self.assertEqual(found, {})                                # nothing to read off Windows


class TestLaunch(unittest.TestCase):
    """The quiet Setup command line - for exactly this copy, never the start-at-login task."""

    def setUp(self):
        self.setup = os.path.join(tempfile.gettempdir(), "gusan-t", "setup.exe")
        self.log = os.path.join(tempfile.gettempdir(), "gusan-t", "x.log")
        self.tmp = tempfile.mkdtemp()                      # where %TEMP% is, for a log nobody named
        patcher = mock.patch.object(updater.tempfile, "gettempdir", return_value=self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _start(self, **kw):
        started = []
        ok, info = updater.launch_installer(self.setup, log_path=self.log,
                                            start=lambda *a, **k: started.append((a, k)), environ={}, **kw)
        return ok, info, started

    def test_a_quiet_install_for_this_user_with_its_own_log(self):
        ok, info, started = self._start(all_users=False, relaunch=True)
        self.assertEqual((ok, info), (True, self.setup))
        (path, verb, args), kw = started[0]
        self.assertEqual((path, verb), (self.setup, "open"))
        for flag in ("/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NOCANCEL", "/NORESTART", "/NORESTARTAPPLICATIONS",
                     '/MERGETASKS="!startup"', '/LOG="%s"' % self.log, "/CURRENTUSER", "/relaunch=1"):
            self.assertIn(flag, args.split(" "))
        self.assertNotIn("/ALLUSERS", args)
        self.assertNotIn("/VERYSILENT", args)             # Setup's own small progress window stays
        self.assertEqual(os.path.normpath(kw["cwd"]), os.path.normpath(os.path.dirname(self.setup)))   # not Gusan's folder

    def test_the_start_at_login_task_is_never_applied_again(self):
        self.assertIn('/MERGETASKS="!startup"', updater.INSTALL_ARGS)
        self.assertNotIn("/TASKS", " ".join(updater.INSTALL_ARGS))

    def test_the_installer_gets_a_fresh_environment_too(self):
        env = {"_PYI_PARENT_PROCESS_LEVEL": "1", "_MEIPASS2": "C:/m", "PATH": "x"}
        updater.launch_installer("C:/t/setup.exe", start=lambda *a, **k: None, environ=env)
        self.assertEqual(env, {"PATH": "x", "PYINSTALLER_RESET_ENVIRONMENT": "1"})

    def test_no_relaunch_flag_unless_asked_for(self):
        _ok, _info, started = self._start(relaunch=False)
        self.assertNotIn("/relaunch", started[0][0][2])
        self.assertIn("/LOG=", started[0][0][2])            # a log even when the caller named none
        ok, _info, started = self._start(all_users=False)
        self.assertNotIn("/relaunch", started[0][0][2])

    def test_an_all_users_install_asks_for_administrator_rights_up_front(self):
        _ok, _info, started = self._start(all_users=True, relaunch=True)
        (_path, verb, args), _kw = started[0]
        self.assertEqual(verb, "runas")
        self.assertIn("/ALLUSERS", args)
        self.assertNotIn("/CURRENTUSER", args)
        self.assertNotIn("/relaunch", args)                # an elevated Setup cannot start Gusan as the person

    def test_the_log_is_named_when_none_is_given(self):
        started = []
        updater.launch_installer(self.setup, start=lambda *a, **k: started.append(a), environ={})
        self.assertRegex(started[0][2], r'/LOG="[^"]*gusan-update-[0-9a-f]{8}\.log"')
        self.assertEqual(os.path.dirname(started[0][2].split('/LOG="')[1].split('"')[0]),
                         os.path.join(self.tmp, "gusan-update", "logs"))

    def test_saying_no_to_the_permission_prompt_is_worded_for_people(self):
        def refuse(*a, **k):
            raise _winerror(OSError(22, "The operation was canceled by the user"), 1223)
        self.assertEqual(updater.launch_installer("C:/t/setup.exe", all_users=True, start=refuse, environ={}),
                         (False, "Windows asked for permission, and it wasn't given."))

    def test_an_antivirus_that_blocks_it_is_named(self):
        def refuse(*a, **k):
            raise _winerror(OSError(22, "virus"), 225)
        self.assertEqual(updater.launch_installer("C:/t/setup.exe", start=refuse, environ={}),
                         (False, "Your antivirus blocked the installer."))

    def test_windows_refusing_to_start_it(self):
        for error in (OSError(2, "not found"), RuntimeError("odd")):
            def refuse(*a, error=error, **k):
                raise error
            self.assertEqual(updater.launch_installer("C:/t/setup.exe", start=refuse, environ={}),
                             (False, "Windows wouldn't start the installer."))

    def test_off_windows_there_is_nothing_to_run(self):
        with mock.patch.object(updater.sys, "platform", "darwin"):
            self.assertEqual(updater.launch_installer("/tmp/setup.exe", all_users=False), (False, updater.UNSUPPORTED))
            self.assertEqual(updater.launch_installer("/tmp/setup.exe", all_users=True), (False, updater.UNSUPPORTED))

    def test_the_command_is_what_launch_installer_runs(self):
        cmd = updater.install_command("C:/t/setup.exe", all_users=False, log_path="C:/t/x.log", relaunch=True)
        self.assertEqual(cmd[:2], ("C:/t/setup.exe", "open"))
        self.assertTrue(cmd[2].startswith("/SILENT /SP- /SUPPRESSMSGBOXES /NOCANCEL /NORESTART /NORESTARTAPPLICATIONS"))
        self.assertTrue(cmd[2].endswith("/CURRENTUSER /relaunch=1"))


@unittest.skipUnless(os.name == "nt", "needs Windows (ShellExecute)")
class TestLaunchForReal(unittest.TestCase):
    """launch_installer through the real os.startfile, with a harmless .cmd file in the installer's place
    that writes down what it was given and where it ran (never a real Setup): the command line reaches
    the program as written, quotes and spaces included."""

    def test_the_command_line_arrives_as_written(self):
        folder = tempfile.mkdtemp(prefix="gusan setup ")           # a space: the paths are quoted
        self.addCleanup(shutil.rmtree, folder, True)
        setup = os.path.join(folder, "TranscribeApp-Windows-Setup.cmd")
        with open(setup, "w", encoding="ascii") as f:
            f.write('@echo off\r\n>"%~dp0args.txt" echo %*\r\n>"%~dp0cwd.txt" cd\r\n')    # (redirect first: "1>" would eat the last 1)
        log = os.path.join(folder, "logs", "gusan update.log")
        ok, info = updater.launch_installer(setup, all_users=False, log_path=log, relaunch=True, environ={})
        self.assertEqual((ok, info), (True, setup))
        args, cwd = os.path.join(folder, "args.txt"), os.path.join(folder, "cwd.txt")
        end = time.time() + 60
        while time.time() < end and not (os.path.exists(args) and os.path.exists(cwd)):
            time.sleep(0.2)
        time.sleep(0.3)
        with open(args, "r", encoding="ascii", errors="ignore") as f:
            got = f.read().strip()
        self.assertEqual(got, " ".join(updater.INSTALL_ARGS) + ' /LOG="%s" /CURRENTUSER /relaunch=1' % log)
        with open(cwd, "r", encoding="ascii", errors="ignore") as f:
            self.assertEqual(os.path.normcase(f.read().strip()), os.path.normcase(folder))   # the installer's folder


class TestLogs(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_every_attempt_gets_its_own_log_in_one_folder(self):
        a, b = updater.new_log_path(self.root), updater.new_log_path(self.root)
        self.assertNotEqual(a, b)
        self.assertEqual(os.path.dirname(a), os.path.join(self.root, "logs"))
        self.assertTrue(os.path.isdir(os.path.dirname(a)))                  # Setup needs the folder to exist
        with mock.patch.object(updater.tempfile, "gettempdir", return_value=self.root):
            self.assertEqual(os.path.dirname(updater.new_log_path()),
                             os.path.join(self.root, "gusan-update", "logs"))

    def test_logs_go_only_when_asked_and_a_kept_installer_stays(self):
        a = updater.new_log_path(self.root)
        with open(a, "w") as f:
            f.write("x")
        kept = os.path.join(self.root, "v2.0.1")
        os.makedirs(kept)
        updater.prune_cache(keep="v2.0.1", root=self.root)
        self.assertTrue(os.path.exists(a))                                  # not an installer: stays
        updater.prune_logs(self.root)
        self.assertFalse(os.path.exists(a))
        self.assertFalse(os.path.exists(os.path.join(self.root, "logs")))
        self.assertTrue(os.path.isdir(kept))                                # a release folder still in use stays
        shutil.rmtree(kept)
        updater.prune_logs(self.root)                                       # nothing left: the folder goes too
        self.assertFalse(os.path.exists(self.root))
        updater.prune_logs(self.root)                                       # and never raises

    def _log(self, text):
        path = updater.new_log_path(self.root)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_an_install_that_wrote_its_last_line_has_finished(self):
        self.assertTrue(updater.install_finished(self._log("2026-10-29 Setup started\n2026-10-29 Log closed.\n")))
        # a long log: the last line is at the end
        self.assertTrue(updater.install_finished(self._log("line\n" * 20000 + "Log closed.\n")))

    def test_a_log_that_is_still_being_written_is_not_finished(self):
        self.assertFalse(updater.install_finished(self._log("2026-10-29 Setup started\n")))
        self.assertFalse(updater.install_finished(self._log("")))

    def test_no_log_means_setup_never_started(self):
        self.assertTrue(updater.install_finished(os.path.join(self.root, "never.log")))
        self.assertTrue(updater.install_finished(""))
        self.assertTrue(updater.install_finished(None))

    def test_a_killed_setup_is_not_a_running_one(self):
        # End task, a power cut: Setup never writes its last line, so the log alone can't tell.
        log = self._log("2026-10-29 Setup started\n")
        self.assertFalse(updater.install_running(log, running=False))
        self.assertTrue(updater.install_running(log, running=True))
        # ...and a log that closed is over whatever else runs (another Setup, a slow antivirus).
        self.assertFalse(updater.install_running(self._log("Log closed.\n"), running=True))
        self.assertFalse(updater.install_running(os.path.join(self.root, "never.log"), running=True))

    def test_when_the_process_cannot_be_looked_at_the_age_of_the_log_decides(self):
        log = self._log("2026-10-29 Setup started\n")
        with mock.patch.object(updater, "setup_running", return_value=None):
            self.assertTrue(updater.install_running(log))                           # written a moment ago
            self.assertFalse(updater.install_running(log, now=time.time() + 600))   # quiet for ten minutes

    def test_setup_is_found_by_the_name_task_manager_shows(self):
        def procs(*names):
            return mock.patch("psutil.process_iter",
                              lambda attrs=None: [mock.Mock(info={"name": n}) for n in names])
        with procs("explorer.exe", "TranscribeApp.exe", "TranscribeApp-Windows-Setup.exe"):
            self.assertTrue(updater.setup_running())
        with procs("explorer.exe", "transcribeapp-windows-setup-0a1b2c3d.EXE"):      # its own file name, any case
            self.assertTrue(updater.setup_running())
        with procs("explorer.exe", "TranscribeApp-Windows-Setup.tmp", None):          # the copy Setup unpacks
            self.assertTrue(updater.setup_running())
        with procs("explorer.exe", "TranscribeApp.exe"):                              # never Gusan itself
            self.assertFalse(updater.setup_running())

        def broken(attrs=None):
            raise OSError("access denied")
        with mock.patch("psutil.process_iter", broken):
            self.assertIsNone(updater.setup_running())                                # can't tell


class TestRelauncher(unittest.TestCase):
    """The watcher that reopens Gusan once Setup has finished - also after a failed install."""

    def _spawn(self, exe, log, **env):
        calls = []
        with mock.patch.dict(os.environ, {"_PYI_APPLICATION_HOME_DIR": "x", "_MEIPASS2": "y", **env}):
            proc = updater.spawn_relauncher(exe, log, popen=lambda cmd, **kw: calls.append((cmd, kw)) or "proc")
        return proc, calls

    def test_it_waits_for_setup_then_reopens_gusan(self):
        exe = "C:\\Users\\D\u2019Souza\\Programs\\Gusan\\TranscribeApp.exe"
        log = r"C:\Users\O'Neil\Temp\gusan-update\logs\gusan-update-ab.log"
        proc, calls = self._spawn(exe, log)
        self.assertEqual(proc, "proc")
        cmd, kw = calls[0]
        self.assertTrue(cmd[0].lower().endswith("powershell.exe"))
        self.assertNotIn("-ExecutionPolicy", cmd)                    # no "hidden bypass" look
        self.assertIn("-NoProfile", cmd)
        script = cmd[-1]
        self.assertEqual(script, updater.RELAUNCH_SCRIPT)
        # No path inside the script text - no quote can break it; they come through the environment.
        for word in ("Souza", "O'Neil", "TranscribeApp.exe", "gusan-update-ab"):
            self.assertNotIn(word, script)
        env = kw["env"]
        self.assertEqual(env["GUSAN_RELAUNCH_EXE"], exe)
        self.assertEqual(env["GUSAN_RELAUNCH_LOG"], log)
        self.assertEqual(env["GUSAN_RELAUNCH_MINUTES"], str(updater.RELAUNCH_TIMEOUT_MIN))
        self.assertEqual(env["GUSAN_RELAUNCH_SETUP"], "TranscribeApp-Windows-Setup*")
        for name in ("$env:GUSAN_RELAUNCH_EXE", "$env:GUSAN_RELAUNCH_LOG", "$env:GUSAN_RELAUNCH_MINUTES",
                     "$env:GUSAN_RELAUNCH_SETUP"):
            self.assertIn(name, script)
        self.assertNotIn("TRANSCRIBE_RELAUNCH", script)
        # A NEW Gusan, not a child of this bundle.
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", env)
        self.assertNotIn("_MEIPASS2", env)
        self.assertTrue(kw["creationflags"] & 0x08000000)            # no window
        self.assertNotEqual(os.path.normpath(kw["cwd"]), os.path.normpath(os.path.dirname(exe)))   # never holds the app's folder

    def test_gusan_that_already_runs_again_is_not_started_a_second_time(self):
        script = updater.RELAUNCH_SCRIPT
        self.assertIn("GetFileNameWithoutExtension($exe)", script)
        self.assertLess(script.index("Get-Process -Name $name"), script.index("Start-Process"))

    def test_the_script_knows_when_setup_is_done(self):
        script = updater.RELAUNCH_SCRIPT
        self.assertIn("'Log closed.'", script)                       # Setup's last line
        self.assertIn("Get-Process -Name $setup", script)            # a killed Setup never writes it
        self.assertIn("$gone -ge 3", script)                         # 3 polls without Setup, in a row
        self.assertIn("Min(3.0, $minutes)", script)                  # a log that never appears: 3 minutes
        self.assertIn("UtcNow", script)                              # a clock change can't cut it short
        self.assertIn("'--show-settings'", script)
        self.assertIn("Start-Process -FilePath $exe", script)
        self.assertNotIn("\n", script)                               # one line: nothing for a command line to mangle
        self.assertNotIn('"', script)                                # single quotes only: nothing to escape
        import fnmatch                                               # the process names Task Manager shows for Setup
        for name in ("TranscribeApp-Windows-Setup", "TranscribeApp-Windows-Setup-0a1b2c3d", "TranscribeApp-Windows-Setup.tmp"):
            self.assertTrue(fnmatch.fnmatch(name, updater.SETUP_PROCESS_PATTERN), name)
        self.assertTrue(fnmatch.fnmatch(updater.INSTALLER_NAME[:-len(".exe")], updater.SETUP_PROCESS_PATTERN))
        self.assertFalse(fnmatch.fnmatch("TranscribeApp", updater.SETUP_PROCESS_PATTERN))   # never Gusan itself

    def test_the_watcher_gives_up_after_twenty_minutes(self):
        self.assertEqual(updater.RELAUNCH_TIMEOUT_MIN, 20)

    @unittest.skipUnless(os.name == "nt", "Windows")
    def test_powershell_by_full_path(self):
        self.assertTrue(os.path.isabs(updater._powershell()))

    def test_without_job_breakaway_it_still_starts(self):
        flags = []

        def popen(cmd, creationflags=0, **kw):
            flags.append(creationflags)
            if creationflags & 0x01000000:
                raise PermissionError(5, "Access is denied")
            return "proc"
        self.assertEqual(updater.spawn_relauncher("C:/a.exe", "C:/l.log", popen=popen), "proc")
        self.assertEqual(len(flags), 2)

    def test_none_when_it_cant_start(self):
        def popen(*a, **kw):
            raise FileNotFoundError(2, "powershell.exe")
        self.assertIsNone(updater.spawn_relauncher("C:/a.exe", "C:/l.log", popen=popen))

    def test_off_windows_there_is_no_watcher(self):
        with mock.patch.object(updater.sys, "platform", "linux"):
            self.assertIsNone(updater.spawn_relauncher("/a/exe", "/l.log"))


@unittest.skipUnless(os.name == "nt" and os.path.exists(updater._powershell()), "needs Windows PowerShell")
class TestWatcherForReal(unittest.TestCase):
    """RELAUNCH_SCRIPT run by the real PowerShell, against a fake log and a harmless program: a .cmd file
    that writes down how it was started (never the real app, never a real Setup). Slow (the watcher polls
    every 2 s), so each test is one case: Setup finished / was killed / still runs / never started / Gusan
    already runs again."""

    NO_WINDOW = 0x08000000

    def setUp(self):
        # A folder with a space and a typographic quote: the paths travel through the environment.
        self.dir = tempfile.mkdtemp(prefix="gusan watcher D\u2019Souza ")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.tag = "gusanprobe_" + os.urandom(4).hex()
        self.exe = os.path.join(self.dir, self.tag + ".cmd")
        self.marker = os.path.join(self.dir, "started.txt")
        with open(self.exe, "w", encoding="ascii") as f:
            f.write('@echo off\r\necho %*> "%~dp0started.txt"\r\n')
        self.log = os.path.join(self.dir, "setup.log")

    def _end(self, proc):
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            pass

    def _watch(self, setup_pattern, minutes="20"):
        env = updater.reset_child_env(dict(os.environ))
        env.update(GUSAN_RELAUNCH_EXE=self.exe, GUSAN_RELAUNCH_LOG=self.log,
                   GUSAN_RELAUNCH_MINUTES=minutes, GUSAN_RELAUNCH_SETUP=setup_pattern)
        proc = subprocess.Popen([updater._powershell(), "-NoProfile", "-NonInteractive", "-Command",
                                 updater.RELAUNCH_SCRIPT], env=env, creationflags=self.NO_WINDOW,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._end, proc)
        return proc

    def _idle_program(self, name):
        """A program that just sits there under the name ``name`` (a renamed copy of ping.exe, which this
        test started and ends itself): stands in for a running Setup, or for Gusan."""
        path = os.path.join(self.dir, name + ".exe")
        shutil.copy(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "PING.EXE"), path)
        proc = subprocess.Popen([path, "-n", "90", "127.0.0.1"], creationflags=self.NO_WINDOW,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._end, proc)
        time.sleep(0.5)
        self.assertIsNone(proc.poll(), "the stand-in program did not start")
        return proc

    def _started(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if os.path.exists(self.marker):
                time.sleep(0.2)
                with open(self.marker, "r", encoding="ascii", errors="ignore") as f:
                    return f.read().strip()
            time.sleep(0.2)
        return None

    def _write_log(self, text):
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(text)

    def test_it_reopens_gusan_once_setup_closed_its_log(self):
        self._write_log("2026-10-29 Setup started\n2026-10-29 Log closed.\n")
        self._watch("gusannosuchsetup_" + os.urandom(4).hex() + "*")
        self.assertEqual(self._started(60), "--show-settings")

    def test_it_reopens_gusan_when_setup_was_killed_and_never_closed_its_log(self):
        self._write_log("2026-10-29 Setup started\n")                 # and no Setup process: 3 polls, then it goes
        proc = self._watch("gusannosuchsetup_" + os.urandom(4).hex() + "*")
        self.assertEqual(self._started(90), "--show-settings")
        proc.wait(30)

    def test_it_keeps_waiting_while_setup_runs_and_goes_when_its_log_closes(self):
        setup = "gusansetup_" + os.urandom(4).hex()
        self._idle_program(setup)
        self._write_log("2026-10-29 Setup started\n")
        self._watch(setup + "*")
        self.assertIsNone(self._started(9))                            # Setup is alive: not before
        self._write_log("2026-10-29 Log closed.\n")
        self.assertEqual(self._started(60), "--show-settings")

    def test_a_log_that_never_appears_does_not_keep_gusan_away_for_ever(self):
        # (the real limits are 3 and 20 minutes; the minutes come in through the environment)
        self._watch("gusannosuchsetup_" + os.urandom(4).hex() + "*", minutes="0.05")
        self.assertEqual(self._started(60), "--show-settings")

    def test_gusan_that_already_runs_again_is_left_alone(self):
        self._idle_program(self.tag)                                   # a program named like the app, running
        self._write_log("2026-10-29 Log closed.\n")
        proc = self._watch("gusannosuchsetup_" + os.urandom(4).hex() + "*")
        proc.wait(60)                                                  # the watcher ended...
        time.sleep(1)
        self.assertFalse(os.path.exists(self.marker))                  # ...and started nothing


def _stubbed_main():
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None or type(qtcore).__name__ != "StubModule":
        return None
    import main
    return main


class TestParityWithMain(unittest.TestCase):
    def setUp(self):
        self.main = _stubbed_main()
        if self.main is None:
            self.skipTest("needs test_core's PySide6 stand-ins (run the whole tests/ folder)")

    def test_same_constants_and_helpers(self):
        main = self.main
        self.assertEqual(updater.RELEASES_API, main.RELEASES_API)
        self.assertEqual(updater.PROJECT_GITHUB_URL, main.PROJECT_GITHUB_URL)
        self.assertEqual(updater.RELEASES_URL, main.RELEASES_URL)
        self.assertEqual(updater.RELEASES_MANIFEST_URL, main.RELEASES_MANIFEST_URL)
        for v in ("v1.2.3", "2.0.0", "1.0", "garbage", "v2.0.0-beta.1"):
            self.assertEqual(updater.parse_version(v), main._parse_version(v))
        for u in ("https://github.com/x", "http://github.com/x", "https://example.com/x"):
            self.assertEqual(updater.trusted_update_url(u), main._trusted_update_url(u))
        text = "a" * 64 + "  TranscribeApp-Windows-Setup.exe"
        self.assertEqual(updater.sha256_from_checksum_text(text), main._sha256_from_checksum_text(text))

    def test_release_info_matches(self):
        main = self.main
        data = {"tag_name": "v9.0.0", "html_url": "https://github.com/Aram2K/transcribe-app/releases/tag/v9.0.0",
                "assets": [{"name": "TranscribeApp-Windows-Setup.exe", "browser_download_url": "https://github.com/a.exe"},
                           {"name": "TranscribeApp-Windows-Setup.exe.sha256",
                            "browser_download_url": "https://github.com/a.exe.sha256"}]}
        dismissed = main.cfg.get("dismissed_update_version", "")
        self.assertEqual(updater.update_info_from_release(data, main.APP_VERSION, dismissed),
                         main._update_info_from_release(data))
        manifest = {"version": "9.0.0", "windows": {"installer": {"url": "https://github.com/a.exe", "sha256": "b" * 64}}}
        self.assertEqual(updater.update_info_from_manifest(manifest, main.APP_VERSION, dismissed),
                         main._update_info_from_manifest(manifest))


if __name__ == "__main__":
    unittest.main()
