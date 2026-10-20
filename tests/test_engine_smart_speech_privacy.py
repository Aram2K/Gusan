"""Smart Actions' "Transcribes with" Gusan Cloud pick: Pro is checked before
Privacy mode (a Free person gets the Pro window, not the Privacy question), and
a Pro person in Privacy mode can turn it off with the pick in one change."""
import unittest

import engine_choice as ec

KEY = ec.SMART_SPEECH_KEY


class TestSmartSpeechCloudOrder(unittest.TestCase):
    def test_free_in_privacy_mode_gets_pro_not_privacy(self):
        for turn_off in (False, True):
            r = ec.set_smart_speech({"privacy_mode": True}, "cloud", pro=False, turn_off_privacy=turn_off)
            self.assertEqual((r["ok"], r["problem"], r["upsell"]), (False, "pro", ec.UPSELL_SPEECH))
            self.assertEqual(r["changes"], {})

    def test_free_without_privacy_still_gets_pro(self):
        r = ec.set_smart_speech({}, "cloud", pro=False)
        self.assertEqual((r["problem"], r["upsell"]), ("pro", "Fast cloud transcription"))

    def test_pro_in_privacy_mode_is_asked(self):
        r = ec.set_smart_speech({"privacy_mode": True}, "cloud", pro=True)
        self.assertEqual((r["ok"], r["problem"]), (False, "privacy_mode"))

    def test_pro_can_turn_privacy_off_with_the_pick(self):
        r = ec.set_smart_speech({"privacy_mode": True}, "cloud", pro=True, turn_off_privacy=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["changes"], {"privacy_mode": False, KEY: "cloud"})

    def test_turning_privacy_off_changes_nothing_when_it_is_already_off(self):
        r = ec.set_smart_speech({}, "cloud", pro=True, turn_off_privacy=True)
        self.assertEqual(r["changes"], {KEY: "cloud"})

    def test_local_models_never_touch_privacy_mode(self):
        r = ec.set_smart_speech({"privacy_mode": True}, "small", turn_off_privacy=True)
        self.assertEqual(r["changes"], {KEY: "small"})
        self.assertEqual(ec.set_smart_speech({"privacy_mode": True}, "")["changes"], {KEY: ""})


class _App:
    """Just enough controller for settings_apply: privacy goes through set_privacy_mode."""

    def __init__(self, **cfg):
        self.cfg = {"hotkey": "alt+r", "backend": "local", "whisper_model": "base", "privacy_mode": True}
        self.cfg.update(cfg)
        self.privacy_calls = []

        class _Sig:
            def emit(self, *_a):
                pass
        self.sig_setting_changed = _Sig()

    def set_privacy_mode(self, on, notify=True):
        self.privacy_calls.append((on, notify))
        self.cfg["privacy_mode"] = bool(on)

    def schedule_config_save(self):
        pass

    def _build_tray_menu(self):
        pass

    def _preload_speech_model(self):
        pass


class TestTheChangesApplyThroughTheController(unittest.TestCase):
    """What the pages send (privacy off + the cloud pick in ONE set_settings call)
    is accepted by settings_apply, which applies Privacy mode first."""

    def test_smart_actions_pick(self):
        import settings_apply
        app = _App()
        r = ec.set_smart_speech(app.cfg, "cloud", pro=True, turn_off_privacy=True)
        res = settings_apply.apply(app, r["changes"])
        self.assertEqual(res, {"privacy_mode": True, KEY: True})
        self.assertEqual(app.privacy_calls, [(False, False)])          # through app.set_privacy_mode
        self.assertEqual((app.cfg["privacy_mode"], app.cfg[KEY]), (False, "cloud"))

    def test_dictation_toggle(self):
        import settings_apply
        app = _App()
        r = ec.set_speech_source(app.cfg, "cloud", pro=True, turn_off_privacy=True)
        res = settings_apply.apply(app, r["changes"])
        self.assertTrue(all(res.values()), res)
        self.assertEqual(app.privacy_calls, [(False, False)])
        self.assertEqual((app.cfg["privacy_mode"], app.cfg["backend"]), (False, "managed"))

    def test_the_pick_without_turning_privacy_off_is_refused(self):
        import settings_apply
        app = _App()
        res = settings_apply.apply(app, {KEY: "cloud"})
        self.assertEqual(res, {KEY: False})
        self.assertTrue(app.cfg["privacy_mode"])


class TestDictationCloudKeepsItsPrivacyHook(unittest.TestCase):
    """The Dictation toggle relies on set_speech_source's turn_off_privacy."""

    def test_pro_in_privacy_mode(self):
        cfg = {"privacy_mode": True}
        self.assertEqual(ec.set_speech_source(cfg, "cloud", pro=True)["problem"], "privacy_mode")
        r = ec.set_speech_source(cfg, "cloud", pro=True, turn_off_privacy=True)
        self.assertEqual(r["changes"], {"privacy_mode": False, "backend": "managed", "managed_provider": "gemini"})

    def test_free_in_privacy_mode_gets_pro_not_privacy(self):
        # Pro is checked first, here too (a Free person gets the Pro window).
        for turn_off in (False, True):
            r = ec.set_speech_source({"privacy_mode": True}, "cloud", pro=False, turn_off_privacy=turn_off)
            self.assertEqual((r["ok"], r["problem"], r["upsell"]), (False, "pro", ec.UPSELL_SPEECH))
            self.assertEqual(r["changes"], {})

    def test_own_key_in_privacy_mode_is_still_refused(self):
        r = ec.set_speech_source({"privacy_mode": True, "mistral_api_key": "m"}, "own_key", provider="mistral")
        self.assertEqual(r["problem"], "privacy_mode")


if __name__ == "__main__":
    unittest.main()
