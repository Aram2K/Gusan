"""Gusan 2.0 Live Assistant: "Hidden on screen share", told truthfully from one
place (live_context.hide_status) for the card's chip, the Live Assistant page
and Settings › Privacy. Pure logic - no Qt; ui.glass is plain ctypes and is
patched where this computer would otherwise decide."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_context  # noqa: E402
from live_context import HIDE_KEYS, hide_status, mac_partial_tip, private_state  # noqa: E402

WIN = dict(supported=True, remote=False, mac=False)
OLD_WIN = dict(supported=False, remote=False, mac=False)
RDP = dict(supported=True, remote=True, mac=False)
MAC = dict(supported=False, remote=False, mac=True, macos26=False)

NEVER = ("invisible", "undetect", "always hidden")


class TestChipStates(unittest.TestCase):
    def test_hidden_only_when_windows_confirms_it(self):
        st = hide_status(True, excluded=True, **WIN)
        self.assertEqual((st.key, st.text, st.tone, st.icon), ("on", "Hidden", "ok", "incognito"))
        self.assertEqual(st.headline, "Hidden on screen share")
        self.assertIn("phone camera", st.tip)

    def test_off_is_red_visible_with_an_open_eye(self):
        st = hide_status(False, excluded=False, **WIN)
        self.assertEqual((st.key, st.text, st.tone, st.icon), ("off", "Visible", "red", "eye"))
        self.assertEqual(st.headline, "Visible on screen share")

    def test_windows_refusal_never_reads_hidden(self):
        st = hide_status(True, excluded=False, **WIN)
        self.assertEqual((st.key, st.text, st.tone, st.icon), ("failed", "Visible", "red", "eye"))
        self.assertIn("Windows refused", st.tip)
        self.assertEqual(hide_status(True, excluded=False, **MAC).key, "unavailable")

    def test_old_windows_and_remote_desktop_are_amber_not_available(self):
        old = hide_status(True, excluded=False, **OLD_WIN)
        rdp = hide_status(True, excluded=True, **RDP)          # even if the call "worked"
        for st in (old, rdp):
            self.assertEqual((st.key, st.text, st.tone, st.icon),
                             ("unavailable", "Not available", "amber", "eye"))
        self.assertIn("Remote Desktop", rdp.tip)
        self.assertEqual(rdp.headline, "Can't hide in Remote Desktop")
        self.assertEqual(old.headline, "Can't hide on this computer")
        if sys.platform == "win32":
            self.assertIn("Windows 10 (version 2004)", old.tip)

    def test_macos_is_may_be_hidden_never_a_promise(self):
        for macos26, caveat in ((False, "doesn't guarantee"), (True, "Not yet verified on macOS 26")):
            st = hide_status(True, excluded=True, **dict(MAC, macos26=macos26))
            self.assertEqual((st.key, st.text, st.tone, st.icon),
                             ("partial", "May be hidden", "amber", "eye_off"))
            self.assertEqual(st.headline, "May be hidden on screen share")
            for must in ("QuickTime", "single window", "second device", caveat):
                self.assertIn(must, st.tip)
        # The flag didn't stick: not available, and how to share safely.
        st = hide_status(True, excluded=False, **MAC)
        self.assertEqual((st.key, st.text), ("unavailable", "Not available"))
        self.assertIn("Share a single window", st.tip)

    def test_every_state_is_honest_and_short(self):
        cases = [hide_status(w, excluded=e, **env) for w in (True, False) for e in (True, False, None)
                 for env in (WIN, OLD_WIN, RDP, MAC)]
        self.assertEqual({st.key for st in cases}, set(HIDE_KEYS))
        for st in cases:
            self.assertLessEqual(len(st.text), 24, st.text)
            self.assertNotIn("Private", st.text)                 # the 1.x word is gone
            self.assertNotIn("click", st.tip.lower())            # the card adds its own
            for word in NEVER:
                self.assertNotIn(word, (st.text + " " + st.tip + " " + st.headline).lower())
            if st.key != "on":
                self.assertNotEqual(st.text, "Hidden")
                self.assertNotEqual(st.icon, "incognito")


class TestWithoutTheCard(unittest.TestCase):
    """The page and Settings ask before any card window exists."""

    def test_predicts_what_opening_the_card_does(self):
        self.assertEqual(hide_status(True, **WIN).key, "on")
        self.assertEqual(hide_status(False, **WIN).key, "off")
        self.assertEqual(hide_status(True, **OLD_WIN).key, "unavailable")
        self.assertEqual(hide_status(True, **RDP).key, "unavailable")
        self.assertEqual(hide_status(True, **MAC).key, "partial")

    def test_asks_glass_about_this_computer(self):
        from ui import glass
        with mock.patch.object(glass, "capture_exclusion_supported", return_value=True), \
                mock.patch.object(glass, "is_remote_session", return_value=False):
            self.assertEqual(hide_status(True, mac=False).key, "on")
        with mock.patch.object(glass, "capture_exclusion_supported", return_value=True), \
                mock.patch.object(glass, "is_remote_session", return_value=True):
            self.assertEqual(hide_status(True, mac=False).key, "unavailable")
        with mock.patch.object(glass, "capture_exclusion_supported", return_value=False), \
                mock.patch.object(glass, "is_remote_session", return_value=False):
            self.assertEqual(hide_status(True, mac=False).key, "unavailable")

    def test_no_glass_means_no_promise(self):
        with mock.patch.object(live_context, "_glass", return_value=None):
            st = hide_status(True, mac=False)
        self.assertEqual(st.key, "unavailable")

    def test_works_for_the_dictation_bar_too(self):
        # Settings › Privacy asks the same question for overlay_private.
        self.assertEqual(hide_status(False, **WIN).text, "Visible")


class TestPrivateState(unittest.TestCase):
    def test_keys_and_words(self):
        self.assertEqual(private_state(True, True, False, True), ("on", "Hidden"))
        self.assertEqual(private_state(True, True, False, False), ("failed", "Visible"))
        self.assertEqual(private_state(False, True, False, True), ("off", "Visible"))
        self.assertEqual(private_state(True, False, False, False), ("unavailable", "Not available"))
        self.assertEqual(private_state(True, True, True, True), ("unavailable", "Not available"))
        self.assertEqual(private_state(True, False, False, True, partial=True),
                         ("partial", "May be hidden"))

    def test_mac_tip_never_overclaims(self):
        tip = mac_partial_tip(False)
        self.assertIn("Apple doesn't guarantee it.", tip)
        self.assertIn("Not yet verified on macOS 26.", mac_partial_tip(True))
        for word in NEVER:
            self.assertNotIn(word, tip.lower())


class TestAnswerLanguages(unittest.TestCase):
    def test_arabic_is_named_for_the_model(self):
        ctx = live_context.rolling_context("مرحبا", output_lang="ar")
        self.assertIn("(Respond in Arabic.)", ctx)

    def test_every_offered_language_is_named(self):
        import speech_langs
        for code in speech_langs.NAMES:
            self.assertIn(code, live_context._LANG_NAMES, code)
        self.assertNotIn("Respond in", live_context.rolling_context("Hi.", output_lang="auto"))


if __name__ == "__main__":
    unittest.main()
