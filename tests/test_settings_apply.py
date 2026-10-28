"""Gusan 2.0 instant-apply settings: settings_apply.apply (behind
AppController.set_setting / set_settings) and the second-launch dispatcher.

settings_apply is pure, so these run with fakes. The controller wiring tests
need main, which is only imported here under test_core's PySide6 stand-ins
(the full `pytest tests` run); run alone they skip instead of loading main
against the real profile.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings_apply  # noqa: E402


class FakeSignal:
    def __init__(self):
        self.emitted = []

    def emit(self, value):
        self.emitted.append(value)


class FakeApp:
    def __init__(self, hotkey_ok=True, assist_ok=True, **cfg):
        self.cfg = {"hotkey": "alt+r", "live_assist_hotkey": "ctrl+alt+a", "backend": "local",
                    "whisper_model": "base", "privacy_mode": False, "save_history": True}
        self.cfg.update(cfg)
        self.hotkey_ok = hotkey_ok
        self.assist_ok = assist_ok
        self.calls = []
        self.saves = 0
        self.sig_setting_changed = FakeSignal()
        self.overlay = SimpleNamespace(apply_prefs=lambda: self.calls.append(("overlay",)))
        self.live_assist = None
        self.tray_icon = object()

    def _setup_hotkey(self, hk):
        self.calls.append(("hotkey", hk))
        return self.hotkey_ok if not callable(self.hotkey_ok) else self.hotkey_ok(hk)

    def _setup_assist_hotkey(self, hk):
        self.calls.append(("assist", hk))
        return self.assist_ok

    def _build_tray_menu(self):
        self.calls.append(("tray",))

    def _preload_speech_model(self):
        self.calls.append(("preload",))

    def set_privacy_mode(self, on, notify=True):
        self.calls.append(("privacy", on, notify))
        self.cfg["privacy_mode"] = on
        if on:
            self.cfg["backend"] = "local"

    def schedule_config_save(self):
        self.saves += 1

    def names(self):
        return [c[0] for c in self.calls]


class FakeAutostart:
    def __init__(self, ok=True, registered=False):
        self.ok, self.registered, self.asked = ok, registered, []

    def set_enabled(self, on):
        self.asked.append(on)
        return self.ok

    def is_registered(self):
        return self.registered


class TestPlainKeys(unittest.TestCase):
    def test_writes_in_place_saves_once_and_announces(self):
        app = FakeApp()
        cfg = app.cfg
        res = settings_apply.apply(app, {"save_history": False, "language": "fr"})
        self.assertEqual(res, {"save_history": True, "language": True})
        self.assertIs(app.cfg, cfg)                 # never rebound: AudioRecorder reads this dict
        self.assertEqual(cfg["save_history"], False)
        self.assertEqual(cfg["language"], "fr")
        self.assertEqual(app.saves, 1)              # one debounced save for the burst
        self.assertEqual(app.sig_setting_changed.emitted, ["save_history", "language"])
        self.assertEqual(app.calls, [])             # no side effects for these

    def test_unchanged_value_is_quiet(self):
        app = FakeApp()
        self.assertEqual(settings_apply.apply(app, {"save_history": True}), {"save_history": True})
        self.assertEqual(app.saves, 0)
        self.assertEqual(app.sig_setting_changed.emitted, [])

    def test_protected_keys_are_refused(self):
        app = FakeApp(secrets_owner="user:1")
        res = settings_apply.apply(app, {"secrets_owner": "user:2", "user_secrets": {}})
        self.assertEqual(res, {"secrets_owner": False, "user_secrets": False})
        self.assertEqual(app.cfg["secrets_owner"], "user:1")
        self.assertEqual(app.saves, 0)


class TestHotkeys(unittest.TestCase):
    def test_dictation_hotkey_registers_both_once(self):
        app = FakeApp()
        res = settings_apply.apply(app, {"hotkey": "Ctrl + Shift + D"})
        self.assertTrue(res["hotkey"])
        self.assertEqual(app.cfg["hotkey"], "ctrl+shift+d")
        self.assertEqual(app.calls, [("hotkey", "ctrl+shift+d"), ("assist", "ctrl+alt+a"), ("tray",)])
        self.assertEqual(app.sig_setting_changed.emitted, ["hotkey"])
        self.assertEqual(app.saves, 1)

    def test_failed_registration_rolls_back(self):
        app = FakeApp(hotkey_ok=lambda hk: hk == "alt+r")
        res = settings_apply.apply(app, {"hotkey": "ctrl+q"})
        self.assertFalse(res["hotkey"])
        self.assertEqual(app.cfg["hotkey"], "alt+r")
        self.assertIn(("hotkey", "alt+r"), app.calls)       # the old one is back
        self.assertEqual(app.sig_setting_changed.emitted, [])

    def test_bare_typing_key_is_refused(self):
        app = FakeApp()
        self.assertFalse(settings_apply.apply(app, {"hotkey": "space"})["hotkey"])
        self.assertEqual(app.cfg["hotkey"], "alt+r")
        self.assertEqual(app.calls, [])

    def test_mouse_button_is_fine(self):
        app = FakeApp()
        self.assertTrue(settings_apply.apply(app, {"hotkey": "mouse:middle"})["hotkey"])
        self.assertEqual(app.cfg["hotkey"], "mouse:middle")

    def test_assist_hotkey_alone_registers_only_itself(self):
        app = FakeApp()
        res = settings_apply.apply(app, {"live_assist_hotkey": "Ctrl + Alt + L"})
        self.assertTrue(res["live_assist_hotkey"])
        self.assertEqual(app.cfg["live_assist_hotkey"], "ctrl+alt+l")
        self.assertEqual(app.calls, [("assist", "ctrl+alt+l"), ("tray",)])

    def test_assist_hotkey_equal_to_dictation_is_refused(self):
        app = FakeApp()
        self.assertFalse(settings_apply.apply(app, {"live_assist_hotkey": "Alt+R"})["live_assist_hotkey"])
        self.assertEqual(app.cfg["live_assist_hotkey"], "ctrl+alt+a")
        self.assertEqual(app.calls, [])

    def test_assist_hotkey_that_wont_register_rolls_back(self):
        app = FakeApp(assist_ok=False)
        self.assertFalse(settings_apply.apply(app, {"live_assist_hotkey": "ctrl+alt+l"})["live_assist_hotkey"])
        self.assertEqual(app.cfg["live_assist_hotkey"], "ctrl+alt+a")     # the old one is kept
        self.assertEqual(app.calls[-2:], [("assist", "ctrl+alt+a"), ("tray",)])
        self.assertEqual(app.sig_setting_changed.emitted, [])

    def test_clearing_assist_hotkey_is_ok(self):
        app = FakeApp(assist_ok=False)
        self.assertTrue(settings_apply.apply(app, {"live_assist_hotkey": ""})["live_assist_hotkey"])

    def test_dictation_hotkey_equal_to_assist_is_refused(self):
        # Two-way check: taking the Live Assistant's key would silently unbind it.
        app = FakeApp()
        self.assertFalse(settings_apply.apply(app, {"hotkey": "Ctrl+Alt+A"})["hotkey"])
        self.assertEqual(app.cfg["hotkey"], "alt+r")
        self.assertEqual(app.calls, [])

    def test_swapping_both_hotkeys_in_one_call(self):
        app = FakeApp()
        res = settings_apply.apply(app, {"hotkey": "ctrl+alt+a", "live_assist_hotkey": "alt+r"})
        self.assertEqual(res, {"hotkey": True, "live_assist_hotkey": True})
        self.assertEqual((app.cfg["hotkey"], app.cfg["live_assist_hotkey"]), ("ctrl+alt+a", "alt+r"))
        # The Live Assistant lets go of ctrl+alt+a before dictation takes it.
        self.assertEqual(app.calls, [("assist", ""), ("hotkey", "ctrl+alt+a"), ("assist", "alt+r"), ("tray",)])
        self.assertEqual(app.sig_setting_changed.emitted, ["hotkey", "live_assist_hotkey"])

    def test_failed_swap_restores_both(self):
        app = FakeApp(hotkey_ok=lambda hk: hk == "alt+r",
                      assist_ok=True)
        res = settings_apply.apply(app, {"hotkey": "ctrl+alt+a", "live_assist_hotkey": "alt+r"})
        self.assertEqual(res, {"hotkey": False, "live_assist_hotkey": False})
        self.assertEqual((app.cfg["hotkey"], app.cfg["live_assist_hotkey"]), ("alt+r", "ctrl+alt+a"))
        self.assertEqual(app.calls, [("assist", ""), ("hotkey", "ctrl+alt+a"), ("hotkey", "alt+r"),
                                     ("assist", "ctrl+alt+a"), ("tray",)])
        self.assertEqual(app.sig_setting_changed.emitted, [])


class SmartApp(FakeApp):
    """A controller with the Smart Actions shortcut (main._setup_smart_hotkey)."""

    def __init__(self, smart_ok=True, **cfg):
        super().__init__(**cfg)
        self.smart_ok = smart_ok

    def _setup_smart_hotkey(self, hk):
        self.calls.append(("smart", hk))
        return self.smart_ok if not callable(self.smart_ok) else self.smart_ok(hk)


class TestSmartActionsShortcut(unittest.TestCase):
    def test_it_registers_and_is_announced(self):
        app = SmartApp()
        res = settings_apply.apply(app, {"smart_actions_hotkey": "Ctrl + Shift + S"})
        self.assertTrue(res["smart_actions_hotkey"])
        self.assertEqual(app.cfg["smart_actions_hotkey"], "ctrl+shift+s")
        self.assertEqual(app.calls, [("smart", "ctrl+shift+s")])
        self.assertEqual(app.sig_setting_changed.emitted, ["smart_actions_hotkey"])
        self.assertEqual(app.saves, 1)

    def test_another_shortcuts_keys_are_refused(self):
        for keys in ("Alt+R", "ctrl+alt+a", "s", "mouse:middle"):
            app = SmartApp(smart_actions_hotkey="")
            self.assertFalse(settings_apply.apply(app, {"smart_actions_hotkey": keys})["smart_actions_hotkey"], keys)
            self.assertEqual(app.cfg["smart_actions_hotkey"], "")
            self.assertEqual(app.calls, [])

    def test_the_other_shortcuts_cant_take_its_keys(self):
        app = SmartApp(smart_actions_hotkey="ctrl+shift+s")
        self.assertFalse(settings_apply.apply(app, {"hotkey": "ctrl+shift+s"})["hotkey"])
        self.assertFalse(settings_apply.apply(app, {"live_assist_hotkey": "Ctrl+Shift+S"})["live_assist_hotkey"])
        self.assertEqual((app.cfg["hotkey"], app.cfg["live_assist_hotkey"]), ("alt+r", "ctrl+alt+a"))
        self.assertEqual(app.calls, [])

    def test_one_that_wont_register_rolls_back(self):
        app = SmartApp(smart_ok=lambda hk: hk != "ctrl+shift+s", smart_actions_hotkey="ctrl+shift+x")
        self.assertFalse(settings_apply.apply(app, {"smart_actions_hotkey": "ctrl+shift+s"})["smart_actions_hotkey"])
        self.assertEqual(app.cfg["smart_actions_hotkey"], "ctrl+shift+x")     # the old one is kept
        self.assertEqual(app.calls, [("smart", "ctrl+shift+s"), ("smart", "ctrl+shift+x")])
        self.assertEqual(app.sig_setting_changed.emitted, [])

    def test_clearing_it_is_ok(self):
        app = SmartApp(smart_ok=False, smart_actions_hotkey="ctrl+shift+s")
        self.assertTrue(settings_apply.apply(app, {"smart_actions_hotkey": ""})["smart_actions_hotkey"])
        self.assertEqual(app.cfg["smart_actions_hotkey"], "")

    def test_a_new_dictation_hotkey_registers_it_again(self):
        app = SmartApp(smart_actions_hotkey="ctrl+shift+s")
        self.assertTrue(settings_apply.apply(app, {"hotkey": "ctrl+shift+d"})["hotkey"])
        self.assertEqual(app.calls, [("hotkey", "ctrl+shift+d"), ("assist", "ctrl+alt+a"), ("tray",),
                                     ("smart", "ctrl+shift+s")])


class TestSmartActionsSpeechModel(unittest.TestCase):
    KEY = "smart_actions_speech_model"

    def test_known_values_are_written_and_never_preload(self):
        for value in ("small", "cloud", "large-v3-turbo", ""):
            app = FakeApp(smart_actions_speech_model="base")
            self.assertTrue(settings_apply.apply(app, {self.KEY: value})[self.KEY], value)
            self.assertEqual(app.cfg[self.KEY], value)
            # The one model slot keeps Dictation's model: no preload here.
            self.assertEqual(app.calls, [], value)
            self.assertEqual(app.sig_setting_changed.emitted, [self.KEY])

    def test_unknown_values_are_refused(self):
        for value in ("whisper-9000", "managed", "local"):
            app = FakeApp(smart_actions_speech_model="")
            self.assertFalse(settings_apply.apply(app, {self.KEY: value})[self.KEY], value)
            self.assertEqual(app.cfg[self.KEY], "")
            self.assertEqual(app.saves, 0)

    def test_the_cloud_is_refused_in_privacy_mode(self):
        app = FakeApp(privacy_mode=True, smart_actions_speech_model="")
        self.assertFalse(settings_apply.apply(app, {self.KEY: "cloud"})[self.KEY])
        self.assertEqual(app.cfg[self.KEY], "")
        self.assertTrue(settings_apply.apply(app, {self.KEY: "small"})[self.KEY])   # on-device is fine
        # Privacy going off in the same call allows it (privacy is applied first).
        app = FakeApp(privacy_mode=True, smart_actions_speech_model="")
        res = settings_apply.apply(app, {self.KEY: "cloud", "privacy_mode": False})
        self.assertEqual(res, {"privacy_mode": True, self.KEY: True})


class TestLiveAssistantSpeechModel(unittest.TestCase):
    KEY = "live_assist_speech_model"

    def test_known_values_are_written_and_never_preload(self):
        for value in ("small", "cloud", "cloud:best", "own:google:gemini-2.5-pro", "own:google", "",
                      "large-v3-turbo"):
            app = FakeApp(live_assist_speech_model="base")
            self.assertTrue(settings_apply.apply(app, {self.KEY: value})[self.KEY], value)
            self.assertEqual(app.cfg[self.KEY], value)
            self.assertEqual(app.calls, [], value)            # the one model slot keeps Dictation's model
            self.assertEqual(app.sig_setting_changed.emitted, [self.KEY])

    def test_unknown_values_are_refused(self):
        for value in ("whisper-9000", "managed", "local", "own:google:gemini-1.0", "own:mistral",
                      "cloud:other"):
            app = FakeApp(live_assist_speech_model="")
            self.assertFalse(settings_apply.apply(app, {self.KEY: value})[self.KEY], value)
            self.assertEqual(app.cfg[self.KEY], "")
            self.assertEqual(app.saves, 0)

    def test_the_cloud_and_your_own_key_are_refused_in_privacy_mode(self):
        for value in ("cloud", "cloud:best", "own:google:gemini-2.5-flash"):
            app = FakeApp(privacy_mode=True, live_assist_speech_model="")
            self.assertFalse(settings_apply.apply(app, {self.KEY: value})[self.KEY], value)
            self.assertEqual(app.cfg[self.KEY], "")
        for value in ("small", ""):                           # on-device is fine
            app = FakeApp(privacy_mode=True, live_assist_speech_model="base")
            self.assertTrue(settings_apply.apply(app, {self.KEY: value})[self.KEY], value)
        # Privacy going off in the same call allows it (privacy is applied first).
        app = FakeApp(privacy_mode=True, live_assist_speech_model="")
        res = settings_apply.apply(app, {self.KEY: "cloud:best", "privacy_mode": False})
        self.assertEqual(res, {"privacy_mode": True, self.KEY: True})


class TestSpokenLanguage(unittest.TestCase):
    """"hye", "HY" and "hy-AM" are all Armenian: one spelling is saved, whichever setting it is."""

    def test_every_spelling_is_saved_as_one_code(self):
        for key in ("language", "live_assist_language", "meeting_language"):
            for value, saved in (("hy", "hy"), ("hye", "hy"), ("HY", "hy"), ("hy-AM", "hy"), ("Armenian", "hy"),
                                 ("en-US", "en"), ("fr_FR", "fr"), ("auto", "auto"), ("multi", "multi"),
                                 ("Mixed languages", "multi"), ("uk", "uk")):
                app = FakeApp(**{key: "en"})
                self.assertTrue(settings_apply.apply(app, {key: value})[key], (key, value))
                self.assertEqual(app.cfg[key], saved, (key, value))

    def test_a_language_nothing_knows_is_refused_and_nothing_changes(self):
        for key in ("language", "live_assist_language", "meeting_language"):
            for value in ("klingon", "xx", "123", "armenian-ish"):
                app = FakeApp(**{key: "fr"})
                self.assertFalse(settings_apply.apply(app, {key: value})[key], (key, value))
                self.assertEqual(app.cfg[key], "fr")
                self.assertEqual(app.saves, 0)

    def test_empty_means_the_default_of_each_setting(self):
        app = FakeApp(language="fr")
        self.assertTrue(settings_apply.apply(app, {"language": ""})["language"])
        self.assertEqual(app.cfg["language"], "auto")                 # Dictation's own language: detect
        for key in ("live_assist_language", "meeting_language"):
            app = FakeApp(**{key: "hy"})
            self.assertTrue(settings_apply.apply(app, {key: None})[key])
            self.assertEqual(app.cfg[key], "")                        # "" = the same as Dictation


class TestAudioToTextModel(unittest.TestCase):
    KEY = "file_model"

    def test_constants_match_engine_choice(self):
        import engine_choice
        self.assertEqual(settings_apply.FILE_MODEL, engine_choice.FILE_KEY)
        self.assertEqual(settings_apply.FILE_AUTO, engine_choice.FILE_AUTO)
        self.assertEqual(settings_apply.FILE_CLOUD_VALUES, engine_choice.FILE_CLOUD_VALUES)

    def test_known_values_are_written_and_never_preload(self):
        for value in ("auto", "small", "large-v3", "cloud", "own:google", "own:mistral"):
            app = FakeApp(file_model="base")
            self.assertTrue(settings_apply.apply(app, {self.KEY: value})[self.KEY], value)
            self.assertEqual(app.cfg[self.KEY], value)
            self.assertEqual(app.calls, [], value)
            self.assertEqual(app.sig_setting_changed.emitted, [self.KEY])
            self.assertEqual(app.saves, 1)

    def test_values_are_trimmed(self):
        app = FakeApp(file_model="auto")
        self.assertTrue(settings_apply.apply(app, {self.KEY: " own:mistral "})[self.KEY])
        self.assertEqual(app.cfg[self.KEY], "own:mistral")

    def test_unknown_values_are_refused(self):
        for value in ("huge-v9", "", None, "managed", "google", "own:openai", "Automatic"):
            app = FakeApp(file_model="small")
            self.assertFalse(settings_apply.apply(app, {self.KEY: value})[self.KEY], value)
            self.assertEqual(app.cfg[self.KEY], "small")
            self.assertEqual(app.saves, 0)
            self.assertEqual(app.sig_setting_changed.emitted, [])

    def test_cloud_values_are_refused_in_privacy_mode(self):
        for value in ("cloud", "own:google", "own:mistral"):
            app = FakeApp(privacy_mode=True, file_model="auto")
            self.assertFalse(settings_apply.apply(app, {self.KEY: value})[self.KEY], value)
            self.assertEqual(app.cfg[self.KEY], "auto")
        app = FakeApp(privacy_mode=True, file_model="auto")
        self.assertTrue(settings_apply.apply(app, {self.KEY: "medium"})[self.KEY])   # on-device is fine
        # Privacy going off in the same call allows it (privacy is applied first).
        app = FakeApp(privacy_mode=True, file_model="auto")
        res = settings_apply.apply(app, {self.KEY: "cloud", "privacy_mode": False})
        self.assertEqual(res, {"privacy_mode": True, self.KEY: True})
        self.assertEqual(app.cfg[self.KEY], "cloud")
        # ... and privacy going on in the same call refuses it.
        app = FakeApp(file_model="auto")
        res = settings_apply.apply(app, {self.KEY: "own:google", "privacy_mode": True})
        self.assertEqual(res, {"privacy_mode": True, self.KEY: False})
        self.assertEqual(app.cfg[self.KEY], "auto")


class TestPrivacy(unittest.TestCase):
    def test_privacy_goes_through_set_privacy_mode(self):
        app = FakeApp(backend="managed")
        res = settings_apply.apply(app, {"privacy_mode": True})
        self.assertTrue(res["privacy_mode"])
        # Dictation moved onto the local model: warm it up.
        self.assertEqual(app.calls, [("privacy", True, False), ("preload",)])
        self.assertEqual(app.cfg["backend"], "local")
        self.assertEqual(app.sig_setting_changed.emitted, ["privacy_mode", "backend"])
        self.assertEqual(app.saves, 0)              # set_privacy_mode saved already

    def test_privacy_is_applied_first_whatever_the_order(self):
        app = FakeApp()
        res = settings_apply.apply(app, {"backend": "managed", "privacy_mode": True})
        self.assertEqual(res, {"privacy_mode": True, "backend": False})
        self.assertEqual(app.cfg["backend"], "local")
        self.assertEqual(app.sig_setting_changed.emitted, ["privacy_mode"])   # each key once

    def test_cloud_backend_refused_while_private(self):
        app = FakeApp(privacy_mode=True)
        self.assertFalse(settings_apply.apply(app, {"backend": "managed"})["backend"])
        self.assertEqual(app.cfg["backend"], "local")

    def test_cloud_backend_ok_when_privacy_goes_off_too(self):
        app = FakeApp(privacy_mode=True)
        res = settings_apply.apply(app, {"privacy_mode": False, "backend": "managed"})
        self.assertEqual(res, {"privacy_mode": True, "backend": True})
        self.assertEqual(app.cfg["backend"], "managed")


class TestLaunchAtLogin(unittest.TestCase):
    def test_enable(self):
        app = FakeApp()
        auto = FakeAutostart(ok=True)
        self.assertTrue(settings_apply.apply(app, {"launch_at_login": True}, autostart_mod=auto)["launch_at_login"])
        self.assertEqual(auto.asked, [True])
        self.assertTrue(app.cfg["launch_at_login"])
        self.assertEqual(app.sig_setting_changed.emitted, ["launch_at_login"])
        self.assertEqual(app.saves, 1)

    def test_failure_records_what_the_os_has(self):
        app = FakeApp(launch_at_login=True)
        auto = FakeAutostart(ok=False, registered=True)
        res = settings_apply.apply(app, {"launch_at_login": False}, autostart_mod=auto)
        self.assertFalse(res["launch_at_login"])
        self.assertTrue(app.cfg["launch_at_login"])          # still registered: shown as on
        self.assertEqual(app.sig_setting_changed.emitted, [])

    def test_exception_is_a_failure_not_a_crash(self):
        app = FakeApp()
        auto = FakeAutostart()
        auto.set_enabled = MagicMock(side_effect=OSError("denied"))
        self.assertFalse(settings_apply.apply(app, {"launch_at_login": True}, autostart_mod=auto)["launch_at_login"])
        self.assertFalse(app.cfg["launch_at_login"])


class TestOtherSideEffects(unittest.TestCase):
    def test_model_keys_preload_once(self):
        app = FakeApp()
        settings_apply.apply(app, {"backend": "local", "whisper_model": "small"})
        self.assertEqual(app.calls, [("preload",)])
        app2 = FakeApp()
        settings_apply.apply(app2, {"whisper_model": "small"})
        self.assertEqual(app2.calls, [("preload",)])

    def test_overlay_keys_restyle_the_bar(self):
        app = FakeApp()
        settings_apply.apply(app, {"overlay_theme": "dark", "overlay_private": False})
        self.assertEqual(app.calls, [("overlay",)])

    def test_live_assist_private_reaches_an_open_card(self):
        app = FakeApp()
        app.live_assist = MagicMock()
        settings_apply.apply(app, {"live_assist_private": False})
        app.live_assist.set_private.assert_called_once_with(False)

    def test_failing_side_effect_never_raises(self):
        app = FakeApp()
        app._preload_speech_model = MagicMock(side_effect=RuntimeError("boom"))
        res = settings_apply.apply(app, {"whisper_model": "small"})
        self.assertTrue(res["whisper_model"])        # the value is saved; only the preload failed
        self.assertEqual(app.cfg["whisper_model"], "small")
        self.assertEqual(app.sig_setting_changed.emitted, ["whisper_model"])


def _stubbed_main():
    """main under test_core's stand-ins, or None (then the test skips)."""
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None or type(qtcore).__name__ != "StubModule":
        return None
    import main
    return main


class TestControllerWiring(unittest.TestCase):
    def setUp(self):
        self.main = _stubbed_main()
        if self.main is None:
            self.skipTest("needs test_core's PySide6 stand-ins (run the whole tests/ folder)")

    def test_set_setting_delegates_and_debounces(self):
        main = self.main
        timer = MagicMock()
        me = FakeApp()
        me._save_timer = timer
        me.schedule_config_save = lambda: main.AppController.schedule_config_save(me)
        me.set_settings = lambda updates: main.AppController.set_settings(me, updates)
        self.assertTrue(main.AppController.set_setting(me, "language", "de"))
        self.assertEqual(me.cfg["language"], "de")
        timer.start.assert_called_once_with()
        self.assertEqual(me.sig_setting_changed.emitted, ["language"])

    def test_flush_writes_and_tracks(self):
        main = self.main
        me = SimpleNamespace(cfg={"a": 1}, _save_timer=MagicMock(), _track_setting_changes=MagicMock())
        with patch.object(main, "save_config") as save:
            main.AppController.flush_config_save(me)
        save.assert_called_once_with(me.cfg)
        me._track_setting_changes.assert_called_once_with()
        me._save_timer.stop.assert_called_once_with()

    def test_pending_save_is_flushed_on_quit(self):
        main = self.main
        me = SimpleNamespace(_save_timer=MagicMock(), flush_config_save=MagicMock())
        me._save_timer.isActive.return_value = True
        main.AppController._flush_pending_save(me)
        me.flush_config_save.assert_called_once_with()
        me._save_timer.isActive.return_value = False
        main.AppController._flush_pending_save(me)
        me.flush_config_save.assert_called_once_with()

    def test_second_launch_actions(self):
        main = self.main
        for action, method in (("show_settings", "show_main"), ("", "show_main"),
                               ("anything", "show_main"), ("show_onboarding", "show_onboarding"),
                               ("show_meeting", "show_meeting"), ("live_prompter", "toggle_live_assist")):
            c = MagicMock()
            main.dispatch_ipc_action(c, action)
            getattr(c, method).assert_called_once_with()
            for other in ("show_main", "show_onboarding", "show_meeting", "toggle_live_assist",
                          "show_settings"):
                if other != method:
                    getattr(c, other).assert_not_called()

    def test_show_main_falls_back_to_classic_settings(self):
        # Wave 4: the 1.x Settings window is retired - a Gusan window that
        # can't be built says so in a notification instead of opening it.
        main = self.main
        me = MagicMock()
        me._ensure_main_window.side_effect = RuntimeError("no window")
        self.assertIsNone(main.AppController.show_main(me, "home"))
        me._show_classic_settings.assert_not_called()
        me.show_tray_hint.assert_called_once()
        self.assertIn("couldn't open", me.show_tray_hint.call_args[0][0])
        # show_settings opens the Settings PAGE (it would land back here).
        me.show_settings.assert_not_called()
        me._bring_to_front.assert_not_called()

    def test_show_main_opens_the_page(self):
        main = self.main
        win = MagicMock()
        win.current_route.return_value = None
        me = MagicMock()
        me._ensure_main_window.return_value = win
        self.assertIs(main.AppController.show_main(me, None), win)
        win.open.assert_called_once_with("home")
        me._bring_to_front.assert_called_once_with(win)
        win.reset_mock()
        win.current_route.return_value = "meetings"
        main.AppController.show_main(me, None)
        win.open.assert_called_once_with("meetings")
        win.reset_mock()
        main.AppController.show_main(me, "settings", section="speech")
        win.open.assert_called_once_with("settings", section="speech")


if __name__ == "__main__":
    unittest.main()
