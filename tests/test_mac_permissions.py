"""mac_permissions.py: which permissions count as missing, when the hotkey is
blocked, the restart of the installed app, and the Mac build's name and icon.

The system calls themselves need a Mac and a person to flip switches; these
tests cover the rules around them, so they run on every CI."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import mac_permissions as P  # noqa: E402

ON = {P.MICROPHONE: True, P.ACCESSIBILITY: True, P.INPUT_MONITORING: True}


class TestRules(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(P, "IS_MAC", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_all_on_is_nothing_missing(self):
        self.assertEqual(P.missing(ON), [])
        self.assertFalse(P.hotkeys_blocked(ON))

    def test_not_decided_counts_as_missing(self):
        st = dict(ON, **{P.MICROPHONE: None})
        self.assertEqual(P.missing(st), [P.MICROPHONE])
        self.assertFalse(P.hotkeys_blocked(st))     # the mic never blocks the shortcut

    def test_accessibility_off_blocks_the_hotkey(self):
        for value in (False, None):
            self.assertTrue(P.hotkeys_blocked(dict(ON, **{P.ACCESSIBILITY: value})))

    def test_input_monitoring_refused_blocks_the_hotkey(self):
        self.assertTrue(P.hotkeys_blocked(dict(ON, **{P.INPUT_MONITORING: False})))
        # Not asked yet: macOS asks when the listener starts, so it is not "blocked".
        self.assertFalse(P.hotkeys_blocked(dict(ON, **{P.INPUT_MONITORING: None})))

    def test_missing_keeps_the_guide_order(self):
        st = {P.MICROPHONE: False, P.ACCESSIBILITY: False, P.INPUT_MONITORING: False}
        self.assertEqual(P.missing(st), list(P.ALL))

    def test_every_permission_has_its_settings_pane(self):
        for name in P.ALL:
            self.assertTrue(P.SETTINGS_URLS[name].startswith("x-apple.systempreferences:"))
        self.assertIn("Privacy_ListenEvent", P.SETTINGS_URLS[P.INPUT_MONITORING])


class TestOffMac(unittest.TestCase):
    def test_nothing_happens_off_a_mac(self):
        with mock.patch.object(P, "IS_MAC", False):
            self.assertEqual(P.status(), {k: None for k in P.ALL})
            self.assertEqual(P.missing(), [])
            self.assertFalse(P.hotkeys_blocked())
            self.assertFalse(P.request(P.ACCESSIBILITY))
            self.assertFalse(P.open_settings(P.MICROPHONE))
            self.assertIsNone(P.app_bundle("/Applications/TranscribeApp.app/Contents/MacOS/TranscribeApp"))


class TestRestart(unittest.TestCase):
    EXE = "/Applications/TranscribeApp.app/Contents/MacOS/TranscribeApp"

    def test_bundle_of_the_installed_app(self):
        with mock.patch.object(P, "IS_MAC", True), mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(P.app_bundle(self.EXE), "/Applications/TranscribeApp.app")

    def test_no_bundle_from_source(self):
        with mock.patch.object(P, "IS_MAC", True):
            if hasattr(sys, "frozen"):
                self.skipTest("frozen interpreter")
            self.assertIsNone(P.app_bundle(self.EXE))

    def test_relaunch_waits_for_this_process_then_opens_the_app(self):
        calls = []
        ok = P.relaunch_after_exit(pid=4242, bundle="/Applications/TranscribeApp.app",
                                   popen=lambda args, **kw: calls.append((args, kw)))
        self.assertTrue(ok)
        args, kw = calls[0]
        self.assertEqual(args[0], "/bin/sh")
        self.assertIn("kill -0", args[2])
        self.assertIn('open "$2"', args[2])
        self.assertEqual(args[-2:], ["4242", "/Applications/TranscribeApp.app"])
        self.assertTrue(kw.get("start_new_session"))

    def test_no_relaunch_without_a_bundle(self):
        with mock.patch.object(P, "app_bundle", return_value=None):
            self.assertFalse(P.relaunch_after_exit(popen=lambda *a, **k: self.fail("started")))


class TestMacBuild(unittest.TestCase):
    """The Mac .app shows the Gusan logo and the name Gusan in Launchpad, the
    Dock and Finder, while the file and bundle id stay TranscribeApp."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, ".github", "workflows", "build.yml"), encoding="utf-8") as f:
            text = f.read()
        cls.mac = text[text.index("build-macos:"):]

    def test_icon_is_made_and_passed_to_pyinstaller(self):
        self.assertIn("installer/make_icns.py", self.mac)
        self.assertIn("--icon assets/icon.icns", self.mac)
        self.assertLess(self.mac.index("installer/make_icns.py"), self.mac.index("-m PyInstaller"))

    def test_localized_name_is_written_before_signing(self):
        self.assertIn("LSHasLocalizedDisplayName bool true", self.mac)
        self.assertIn('"CFBundleDisplayName" = "Gusan";', self.mac)
        self.assertIn("InfoPlist.strings", self.mac)
        self.assertLess(self.mac.index("InfoPlist.strings"), self.mac.index("codesign --force"))

    def test_the_bundle_keeps_its_old_name(self):
        self.assertIn("--name TranscribeApp", self.mac)

    def test_make_icns_draws_the_logo_on_apples_grid(self):
        # A clean interpreter: other tests put a stand-in PIL in sys.modules.
        import subprocess
        import tempfile
        probe = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "try:\n    from PIL import Image\nexcept ImportError:\n    print('skip'); sys.exit()\n"
            "import make_icns\n"
            "out = make_icns.make_icns(sys.argv[2], sys.argv[3])\n"
            "img = Image.open(out); fmt = img.format\n"
            "img.size = (1024, 1024); img.load(); big = img.convert('RGBA')\n"   # the 512@2x entry
            "print(fmt, big.getpixel((10, 10))[3], big.getpixel((512, 512))[3])\n")
        with tempfile.TemporaryDirectory() as d:
            res = subprocess.run([sys.executable, "-c", probe, os.path.join(ROOT, "installer"),
                                  os.path.join(ROOT, "assets", "icon_1024.png"), os.path.join(d, "icon.icns")],
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        if res.stdout.strip() == "skip":
            self.skipTest("Pillow not installed")
        fmt, corner, middle = res.stdout.split()
        self.assertEqual(fmt, "ICNS")
        self.assertEqual(int(corner), 0)        # transparent margin
        self.assertGreater(int(middle), 0)      # the logo


if __name__ == "__main__":
    unittest.main()
