"""AppController's dictation-path seams: cleanup and clipboard restore.

Built via AppController.__new__ with a hand-set .cfg, so no Qt/recorder/tray is
constructed. Reuses the heavy-import stubs installed by test_core.
"""
import os
import sys
import unittest

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main
import rich_text  # noqa: F401  (imported once, outside the sys.modules patches below)


def _fake_rich_clipboard(set_rich_blocking):
    """sys.modules entries that make `from ui.components import clipboard`
    a stand-in (the real package needs real Qt)."""
    import types
    clip = types.ModuleType("ui.components.clipboard")
    clip.set_rich_blocking = set_rich_blocking
    pkg = types.ModuleType("ui.components")
    pkg.clipboard = clip
    # The paste chord's keys (pynput is a stand-in here).
    kb = types.ModuleType("pynput.keyboard")
    kb.Key = types.SimpleNamespace(ctrl="ctrl", cmd="cmd")
    return {"ui.components": pkg, "ui.components.clipboard": clip, "pynput.keyboard": kb}


def controller(**cfg):
    c = main.AppController.__new__(main.AppController)
    c.cfg = {**main.DEFAULT, **cfg}
    return c


class TestPostprocessTranscript(unittest.TestCase):
    def test_collapses_hallucination_loop(self):
        c = controller()
        self.assertEqual(
            c._postprocess_transcript("Thank you. Thank you. Thank you. Thank you."),
            "Thank you.")

    def test_leaves_normal_dictation_alone(self):
        c = controller()
        text = "Let's ship the release on Friday."
        self.assertEqual(c._postprocess_transcript(text), text)

    def test_applies_user_replacements(self):
        c = controller(cleanup_replacements=[{"from": "pyside 6", "to": "PySide6"}])
        self.assertEqual(c._postprocess_transcript("i use pyside 6"), "i use PySide6")

    def test_master_switch_off_stops_second_guessing_the_recognizer(self):
        c = controller(cleanup_enabled=False)
        text = "Thank you. Thank you. Thank you."
        self.assertEqual(c._postprocess_transcript(text), text)

    def test_master_switch_off_still_applies_replacements(self):
        # The UI labels these "always replace these words" - a deliberate
        # correction is not a cleanup heuristic, so it must survive the switch.
        c = controller(cleanup_enabled=False,
                       cleanup_replacements=[{"from": "gorge", "to": "George"}])
        self.assertEqual(c._postprocess_transcript("hi gorge"), "hi George")

    def test_master_switch_off_leaves_artifacts_alone(self):
        c = controller(cleanup_enabled=False)
        self.assertEqual(c._postprocess_transcript("[Applause] hello"),
                         "[Applause] hello")

    def test_empty_input(self):
        self.assertEqual(controller()._postprocess_transcript(""), "")
        self.assertEqual(controller()._postprocess_transcript(None), "")

    def test_never_raises_on_a_broken_config(self):
        # A malformed config must degrade to the raw transcript, not lose it.
        c = controller(cleanup_replacements="not-a-list")
        self.assertEqual(c._postprocess_transcript("hello"), "hello")

    def test_effective_cfg_is_the_interception_point(self):
        c = controller()
        self.assertIs(c._effective_cfg(), c.cfg)


class TestShouldRestoreClipboard(unittest.TestCase):
    def _call(self, **kw):
        args = {"pasted": True, "restore_enabled": True,
                "prev_was_text": True, "still_ours": True}
        args.update(kw)
        return main.AppController.should_restore_clipboard(**args)

    def test_restores_in_the_normal_case(self):
        self.assertTrue(self._call())

    def test_not_when_disabled(self):
        self.assertFalse(self._call(restore_enabled=False))

    def test_not_when_paste_failed(self):
        # The overlay says "Copied to clipboard" - the text must stay there.
        self.assertFalse(self._call(pasted=False))

    def test_not_when_previous_clipboard_was_not_text(self):
        # Never clear an image/file clipboard to an empty string.
        self.assertFalse(self._call(prev_was_text=False))

    def test_not_when_someone_else_changed_the_clipboard(self):
        self.assertFalse(self._call(still_ours=False))


class TestNewConfigDefaults(unittest.TestCase):
    def test_cleanup_defaults(self):
        self.assertTrue(main.DEFAULT["cleanup_enabled"])
        self.assertTrue(main.DEFAULT["cleanup_strip_hallucinations"])
        self.assertTrue(main.DEFAULT["cleanup_strip_artifacts"])
        # Filler removal changes the user's words, so it must be opt-in.
        self.assertFalse(main.DEFAULT["cleanup_remove_fillers"])
        self.assertEqual(main.DEFAULT["cleanup_replacements"], [])

    def test_vocabulary_defaults(self):
        self.assertEqual(main.DEFAULT["vocabulary"], [])
        self.assertTrue(main.DEFAULT["vocabulary_share_with_cloud"])

    def test_clipboard_defaults(self):
        self.assertTrue(main.DEFAULT["restore_clipboard"])
        self.assertEqual(main.DEFAULT["clipboard_restore_delay_ms"], 400)

    def test_schema_version_present(self):
        # 2 = Gusan 2.0, 3 = the Smart Actions shortcut (main._migrate_config
        # brings 1.9.x and early 2.0 configs up).
        self.assertEqual(main.DEFAULT["config_schema_version"], 3)

    def test_smart_actions_defaults(self):
        self.assertEqual(main.DEFAULT["smart_actions_hotkey"], "alt+s")
        self.assertEqual(main.DEFAULT["smart_actions_speech_model"], "")     # same as Dictation
        self.assertEqual(main.DEFAULT["output_action"], "transcribe_only")  # 1.9.x key, kept


class TestRichPaste(unittest.TestCase):
    """D9: a Smart Action's paste puts HTML + nice plain text on the clipboard;
    plain dictation is never changed. pyperclip, the rich clipboard and the
    keyboard are mocks."""

    MD = "## Plan\n\n- [ ] Call David\n- **Send** the slides"

    def _paste(self, text, rich, set_rich=True, clipboard_after=None, prev="old clip"):
        from unittest import mock
        c = controller(restore_clipboard=True, clipboard_restore_delay_ms=0)
        c.kbd = mock.MagicMock()
        copies, posted = [], []
        state = {"clip": prev}

        def copy(value):
            copies.append(value)
            state["clip"] = value

        def paste():
            return state["clip"] if clipboard_after is None else clipboard_after(state["clip"])

        def blocking(plain, html, timeout=0.6):
            posted.append((plain, html))
            if set_rich is True:
                state["clip"] = plain.replace("\n", "\r\n")      # Qt writes Windows line ends
                return True
            if isinstance(set_rich, Exception):
                raise set_rich
            return False

        class _Now:                                              # the restore thread, inline
            def __init__(self, target=None, daemon=None, **kw):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.dict(sys.modules, _fake_rich_clipboard(blocking)), \
                mock.patch.object(main.pyperclip, "copy", copy), \
                mock.patch.object(main.pyperclip, "paste", paste), \
                mock.patch.object(main.threading, "Thread", _Now), \
                mock.patch.object(main.time, "sleep", lambda s: None):
            pasted = c._paste_via_clipboard(text, rich=rich) if rich else c._paste_via_clipboard(text)
        return pasted, copies, posted, state["clip"], c.kbd

    def test_a_smart_action_goes_as_html_and_nice_plain_text(self):
        import rich_text
        pasted, copies, posted, clip, kbd = self._paste(self.MD, rich=True)
        self.assertTrue(pasted)
        plain, html = posted[0]
        self.assertEqual(plain, rich_text.to_plain(self.MD))
        self.assertEqual(html, rich_text.to_html(self.MD))
        self.assertIn("☐ Call David", plain)
        self.assertNotIn("**", plain)
        self.assertIn("<b>Send</b>", html)
        # Ctrl+V went out, and the user's clipboard came back: the check
        # compares with the plain text that was really put there.
        self.assertTrue(kbd.press.called)
        self.assertEqual(copies, ["old clip"])
        self.assertEqual(clip, "old clip")

    def test_no_rich_clipboard_falls_back_to_the_plain_text(self):
        import rich_text
        for failure in (False, RuntimeError("no GUI thread")):
            pasted, copies, posted, clip, _kbd = self._paste(self.MD, rich=True, set_rich=failure)
            self.assertTrue(pasted)
            self.assertEqual(copies, [rich_text.to_plain(self.MD), "old clip"], failure)

    def test_someone_elses_clipboard_is_never_overwritten(self):
        _p, copies, _posted, clip, _k = self._paste(self.MD, rich=True,
                                                    clipboard_after=lambda now: "they copied this")
        self.assertEqual(copies, [])                 # nothing restored over their copy

    def test_prose_without_markup_is_pasted_as_is(self):
        text = "Sure, Thursday works. See you then."
        _p, copies, posted, _c, _k = self._paste(text, rich=True)
        self.assertEqual(posted, [])                 # no HTML for plain prose
        self.assertEqual(copies, [text, "old clip"])

    def test_dictation_is_never_rendered(self):
        _p, copies, posted, _c, _k = self._paste(self.MD, rich=False)
        self.assertEqual(posted, [])
        self.assertEqual(copies, [self.MD, "old clip"])        # the exact words, byte for byte


if __name__ == "__main__":
    unittest.main()
