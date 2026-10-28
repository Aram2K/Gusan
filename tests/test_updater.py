"""updater: the check (dismissed versions stay quiet), the install (sha256 checked),
and the kept identifiers 1.9.x installs update through. Fake HTTP, temp files."""
import hashlib
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater  # noqa: E402


def resp(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


class FakeOpener:
    """urlopen stand-in: url -> bytes, or an exception to raise."""

    def __init__(self, files):
        self.files = files
        self.urls = []

    def __call__(self, url, timeout=60):
        self.urls.append(url)
        data = self.files.get(url)
        if data is None or isinstance(data, Exception):
            raise data or OSError("404")
        return io.BytesIO(data)


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
        import shutil
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
