"""Unsupported platforms and failed native calls must be harmless."""
import os
import sys
import unittest
from unittest import mock

import foreground


class TestForeground(unittest.TestCase):
    def test_linux_and_empty_tokens(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(foreground.remember())
            for token in (None, object(), foreground.Target("win32", 123, "Notepad", 42)):
                self.assertFalse(foreground.restore(token))
                self.assertIsNone(foreground.monitor_rect(token))

    def test_failed_native_calls_never_escape(self):
        for platform, helper in (("win32", "_windows"), ("darwin", "_mac")):
            with mock.patch.object(sys, "platform", platform), \
                    mock.patch.object(foreground, helper, side_effect=OSError("unavailable")):
                token = foreground.Target(platform, os.getpid() + 100, "Other", 42)
                self.assertIsNone(foreground.remember())
                self.assertFalse(foreground.restore(token))
                self.assertIsNone(foreground.monitor_rect(token))

    def test_own_process_is_not_a_paste_target(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertFalse(foreground.restore(foreground.Target("win32", os.getpid(), "Gusan", 42)))
