"""Gusan 2.0: app_info (name, version, resource_path) and the names that must
NOT change for existing installs.

Pure: imports neither main nor ui at module level (this file is collected
before test_core installs its PySide6 stand-ins, and CI has no PySide6).
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_info  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _src(name):
    return (ROOT / name).read_text(encoding="utf-8")


class TestAppInfo(unittest.TestCase):
    def test_name_and_version(self):
        self.assertEqual(app_info.APP_NAME, "Gusan")
        self.assertEqual(app_info.APP_VERSION, "2.0.1")

    def test_version_is_plain_three_numbers(self):
        # main._parse_version turns a suffix like "-beta.1" into (0, 0, 0).
        self.assertRegex(app_info.APP_VERSION, r"^\d+\.\d+\.\d+$")

    def test_resource_path_finds_repo_assets(self):
        p = app_info.resource_path("assets", "icon_512.png")
        self.assertTrue(os.path.isfile(p), p)
        self.assertEqual(Path(p), ROOT / "assets" / "icon_512.png")

    def test_resource_path_prefers_the_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "assets").mkdir()
            (Path(tmp) / "assets" / "x.txt").write_text("bundled")
            with patch.object(sys, "_MEIPASS", tmp, create=True):
                self.assertEqual(Path(app_info.resource_path("assets", "x.txt")),
                                 Path(tmp) / "assets" / "x.txt")
                # Missing from the bundle: the source folder's copy.
                self.assertEqual(Path(app_info.resource_path("assets", "icon_512.png")),
                                 ROOT / "assets" / "icon_512.png")

    def test_app_info_has_no_qt(self):
        self.assertNotIn("PySide6", _src("app_info.py"))
        self.assertNotIn("PySide6", _src("settings_apply.py"))


class TestMainUsesAppInfo(unittest.TestCase):
    """main.APP_VERSION / main.resource_path keep working (tests patch them),
    but the values come from app_info."""

    def test_main_imports_them(self):
        src = _src("main.py")
        self.assertIn("from app_info import APP_NAME, APP_VERSION, resource_path", src)
        self.assertIsNone(re.search(r'^APP_VERSION\s*=', src, re.M))
        self.assertIsNone(re.search(r'^def resource_path\(', src, re.M))

    def test_application_name_is_set(self):
        self.assertIn("qapp.setApplicationName(APP_NAME)", _src("main.py"))


class TestKeptIdentifiers(unittest.TestCase):
    """Existing installs depend on these: renaming any strands 1.9.x users."""

    def test_single_instance_port_and_handshake(self):
        src = _src("main.py")
        self.assertIn("SINGLE_INSTANCE_PORT = 47823", src)
        self.assertIn('b"transcribe-ok"', src)
        self.assertIn('IPC_ACTIONS = ("show_settings", "show_onboarding", "show_meeting", "live_prompter")', src)

    def test_updater_asset_name(self):
        self.assertIn("TranscribeApp-Windows-Setup.exe", _src("main.py"))

    def test_storage_names(self):
        import storage
        self.assertEqual(storage.APP_DIR_NAME, "Transcribe")
        self.assertEqual(storage.SECRET_SERVICE, "TranscribeApp")


if __name__ == "__main__":
    unittest.main()
