"""Windows shortcuts registered with the system (win_hotkeys.py): the app in front never sees Alt+S - with the old
hook (suppress=False) it also typed an "s" in a text box (the founder, 2026-11-08)."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win_hotkeys as wh  # noqa: E402


class TestParse(unittest.TestCase):
    def test_the_shortcuts_settings_produces(self):
        self.assertEqual(wh.parse("alt+s"), (wh.MOD_ALT, ord("S")))
        self.assertEqual(wh.parse("alt+r"), (wh.MOD_ALT, ord("R")))
        self.assertEqual(wh.parse("ctrl+alt+i"), (wh.MOD_CONTROL | wh.MOD_ALT, ord("I")))
        self.assertEqual(wh.parse("ctrl+shift+space"), (wh.MOD_CONTROL | wh.MOD_SHIFT, 0x20))
        self.assertEqual(wh.parse("Ctrl + Shift + F9"), (wh.MOD_CONTROL | wh.MOD_SHIFT, 0x78))
        self.assertEqual(wh.parse("win+1"), (wh.MOD_WIN, ord("1")))
        self.assertEqual(wh.parse("f13"), (0, 0x7C))

    def test_what_windows_cannot_take_keeps_the_hook(self):
        for text in ("", "mouse:middle", "alt", "alt+s+d", "alt+unknownkey", None):
            self.assertIsNone(wh.parse(text), text)

    def test_the_test_runs_never_take_real_shortcuts(self):
        self.assertEqual(os.environ.get("TRANSCRIBE_NO_WIN_HOTKEYS"), "1")
        self.assertFalse(wh.available())
        self.assertIsNone(wh.register("alt+s", lambda: None))


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class TestRealRegistration(unittest.TestCase):
    """A combination nobody uses (Ctrl+Alt+Shift+F23), registered for a moment: Windows takes it once, refuses it
    a second time (another owner), and frees it again."""

    def test_register_refuse_and_free(self):
        with mock.patch.dict(os.environ, {"TRANSCRIBE_NO_WIN_HOTKEYS": "0"}):
            first = wh.register("ctrl+alt+shift+f23", lambda: None)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(wh.register("ctrl+alt+shift+f23", lambda: None))      # taken
            finally:
                self.assertTrue(wh.unregister(first))
            again = wh.register("ctrl+alt+shift+f23", lambda: None)
            self.assertIsNotNone(again)
            wh.unregister(again)


class TestTheApp(unittest.TestCase):
    def test_windows_takes_the_shortcut_else_the_hook(self):
        try:
            from tests import test_core  # noqa: F401
        except ImportError:
            import test_core  # noqa: F401
        import main
        AC = main.AppController
        added, removed = [], []
        kbd = SimpleNamespace(add_hotkey=lambda hk, fn, **kw: added.append((hk, kw)),
                              remove_hotkey=lambda hk: removed.append(hk))
        c = SimpleNamespace()
        with mock.patch.dict(sys.modules, {"keyboard": kbd}), \
                mock.patch.object(wh, "register", lambda hk, fn: 7), \
                mock.patch.object(wh, "unregister", lambda hid: removed.append(("win", hid))):
            self.assertTrue(AC._add_win_hotkey(c, "alt+s", lambda: None))
            AC._remove_win_hotkey(c, "alt+s")
        self.assertEqual((added, removed), ([], [("win", 7)]))
        with mock.patch.dict(sys.modules, {"keyboard": kbd}), mock.patch.object(wh, "register", lambda hk, fn: None):
            self.assertFalse(AC._add_win_hotkey(c, "alt+s", lambda: None))           # taken elsewhere: the hook
            AC._remove_win_hotkey(c, "alt+s")
        self.assertEqual(added, [("alt+s", {"suppress": False, "trigger_on_release": False})])
        self.assertEqual(removed[-1], "alt+s")


if __name__ == "__main__":
    unittest.main()
