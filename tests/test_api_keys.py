"""api_keys: saving keys per account, the key tests (fake HTTP), the thread helper.

Pure: no network, no Qt (run_then has its own test in tests_qt). The key
tests are called as api_keys.test_* - importing them by name would make
pytest collect them as tests.
"""
import io
import json
import os
import sys
import threading
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions  # noqa: E402
import api_keys  # noqa: E402


def resp(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.text = json.dumps(body) if isinstance(body, (dict, list)) else (body or "")
    return r


class FakeApp:
    def __init__(self, owner="user:A", authed=True, user_id="A", **cfg):
        self.cfg = {"backend": "local", "secrets_owner": "guest"}
        self.cfg.update(cfg)
        self.auth = SimpleNamespace(is_authenticated=authed, user_id=user_id)
        self._owner = owner
        self.applied = []
        self.saves = 0

    def _user_secret_id(self):
        return self._owner

    def set_settings(self, updates):
        self.applied.append(dict(updates))
        for k, v in updates.items():
            self.cfg[k] = v
        return {k: True for k in updates}

    def schedule_config_save(self):
        self.saves += 1


class TestSaveKey(unittest.TestCase):
    def test_saves_through_set_settings_and_stamps_the_owner(self):
        app = FakeApp()
        self.assertTrue(api_keys.save_key(app, "mistral", "  sk-123456789  "))
        self.assertEqual(app.applied, [{"mistral_api_key": "sk-123456789"}])
        self.assertEqual(app.cfg["secrets_owner"], "user:A")

    def test_unchanged_key_still_saves_a_new_owner(self):
        app = FakeApp(google_api_key="g-key")
        api_keys.save_key(app, "google", "g-key")
        self.assertEqual(app.saves, 1)
        app.saves = 0
        api_keys.save_key(app, "google", "g-key")         # same owner, same key: nothing extra
        self.assertEqual(app.saves, 0)

    def test_offline_signed_in_account_is_not_stamped_guest(self):
        app = FakeApp(owner="guest", user_id=None, secrets_owner="user:A")
        api_keys.save_key(app, "writer", "k")
        self.assertEqual(app.cfg["secrets_owner"], "user:A")
        self.assertEqual(app.cfg["action_api_key"], "k")

    def test_clearing_the_speech_key_moves_speech_home(self):
        app = FakeApp(backend="mistral", mistral_api_key="m")
        self.assertTrue(api_keys.clear_key(app, "mistral_api_key"))
        self.assertEqual(app.applied, [{"mistral_api_key": "", "backend": "local"}])
        app = FakeApp(backend="managed", google_api_key="g")
        api_keys.clear_key(app, "google")
        self.assertEqual(app.cfg["backend"], "managed")      # not running on that key

    def test_clearing_the_key_audio_to_text_uses_goes_back_to_automatic(self):
        app = FakeApp(file_model="own:google", google_api_key="g")
        self.assertTrue(api_keys.clear_key(app, "google"))
        self.assertEqual(app.applied, [{"google_api_key": "", "file_model": "auto"}])
        app = FakeApp(backend="mistral", file_model="own:mistral", mistral_api_key="m")
        api_keys.clear_key(app, "mistral")
        self.assertEqual(app.applied, [{"mistral_api_key": "", "backend": "local", "file_model": "auto"}])

    def test_other_audio_to_text_picks_are_kept(self):
        for field, model in (("google", "own:mistral"), ("mistral", "own:google"), ("google", "cloud"),
                             ("mistral", "cloud"), ("google", "small"), ("mistral", "auto"),
                             ("writer", "own:mistral"), ("writer", "own:google")):
            with self.subTest(field=field, model=model):
                app = FakeApp(file_model=model, google_api_key="g", mistral_api_key="m", action_api_key="a")
                api_keys.clear_key(app, field)
                self.assertEqual(app.cfg["file_model"], model)
                self.assertNotIn("file_model", app.applied[0])
        # Saving (not removing) a key never touches it.
        app = FakeApp(file_model="own:google", google_api_key="g")
        api_keys.save_key(app, "google", "g2")
        self.assertEqual(app.applied, [{"google_api_key": "g2"}])

    def test_unknown_field(self):
        with self.assertRaises(ValueError):
            api_keys.save_key(FakeApp(), "notion_api_key", "x")

    def test_older_controller_without_set_settings(self):
        app = SimpleNamespace(cfg={}, save_config=MagicMock())
        self.assertTrue(api_keys.save_key(app, "google", "g"))
        self.assertEqual(app.cfg["google_api_key"], "g")
        app.save_config.assert_called_once_with()

    def test_key_view(self):
        self.assertEqual(api_keys.key_view({"mistral_api_key": "abcdefgh4f2"}, "mistral")["text"],
                         "Key ending 4f2")
        self.assertEqual(api_keys.key_view({}, "google")["text"], "No key yet")
        self.assertEqual(api_keys.key_view({"action_api_key": "short"}, "writer")["text"], "Key saved")


class TestKeyTests(unittest.TestCase):
    def test_empty_key_needs_no_network(self):
        boom = MagicMock(side_effect=AssertionError("no call"))
        self.assertEqual(api_keys.test_mistral(" ", post=boom), (False, "Paste a key first."))
        self.assertEqual(api_keys.test_gemini("", get=boom), (False, "Paste a key first."))
        self.assertEqual(api_keys.test_writer_key("openai", "", post=boom, get=boom)[0], False)

    def test_mistral_sends_a_real_wav(self):
        post = MagicMock(return_value=resp(200))
        self.assertEqual(api_keys.test_mistral("sk", "voxtral-small-latest", post=post), (True, "Works"))
        args, kw = post.call_args
        self.assertEqual(args[0], api_keys.MISTRAL_STT_URL)
        self.assertEqual(kw["headers"], {"Authorization": "Bearer sk"})
        self.assertEqual(kw["data"], {"model": "voxtral-mini-latest"})
        name, f, mime = kw["files"]["file"]
        with wave.open(io.BytesIO(f.read()), "rb") as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (16000, 1, 16000))

    def test_mistral_errors_are_plain(self):
        post = MagicMock(return_value=resp(401, {"message": "Unauthorized"}))
        ok, msg = api_keys.test_mistral("sk", post=post)
        self.assertFalse(ok)
        self.assertIn("invalid", msg)
        ok, msg = api_keys.test_mistral("sk", post=MagicMock(side_effect=OSError("down")))
        self.assertEqual((ok, msg), (False, "Couldn't reach Mistral. Check your connection."))

    def test_gemini_key_goes_in_a_header_not_the_url(self):
        get = MagicMock(return_value=resp(200))
        self.assertEqual(api_keys.test_gemini("AIza-secret", get=get), (True, "Works"))
        args, kw = get.call_args
        self.assertNotIn("AIza-secret", args[0])
        self.assertNotIn("key=", args[0])
        self.assertEqual(kw["headers"], {"x-goog-api-key": "AIza-secret"})
        bad = MagicMock(return_value=resp(400, {"error": {"message": "API key not valid. Please pass a valid API key."}}))
        self.assertIn("invalid", api_keys.test_gemini("x", get=bad)[1])

    def test_writer_providers(self):
        post, get = MagicMock(return_value=resp(200)), MagicMock(return_value=resp(200))
        self.assertTrue(api_keys.test_writer_key(actions.API_GEMINI_ID, "g", post=post, get=get)[0])
        self.assertIn("gemini-2.5-flash:generateContent", post.call_args[0][0])
        self.assertEqual(post.call_args[1]["headers"]["x-goog-api-key"], "g")
        api_keys.test_writer_key("anthropic", "a", post=post, get=get)
        self.assertTrue(post.call_args[0][0].endswith("/messages"))
        self.assertEqual(post.call_args[1]["headers"]["x-api-key"], "a")
        api_keys.test_writer_key("cerebras", "c", base="https://api.cerebras.ai/v1/", post=post, get=get)
        self.assertEqual(get.call_args[0][0], "https://api.cerebras.ai/v1/models")
        api_keys.test_writer_key("openai_compatible", "o", post=post, get=get)
        self.assertEqual(get.call_args[0][0], "https://api.openai.com/v1/models")
        self.assertEqual(get.call_args[1]["headers"], {"Authorization": "Bearer o"})
        api_keys.test_writer_key("openai", "o", base="https://openrouter.ai/api/v1", post=post, get=get)
        self.assertEqual(get.call_args[0][0], "https://openrouter.ai/api/v1/models")

    def test_fixed_url_providers_ignore_a_stale_url(self):
        post, get = MagicMock(return_value=resp(200)), MagicMock(return_value=resp(200))
        api_keys.test_writer_key("gemini", "g", base="https://api.openai.com/v1", post=post, get=get)
        self.assertTrue(post.call_args[0][0].startswith("https://generativelanguage.googleapis.com/v1beta/"))
        api_keys.test_writer_key("mistral", "m", base="https://api.cerebras.ai/v1", post=post, get=get)
        self.assertEqual(get.call_args[0][0], "https://api.mistral.ai/v1/models")

    def test_writer_errors(self):
        get = MagicMock(return_value=resp(403))
        self.assertEqual(api_keys.test_writer_key("mistral", "m", post=MagicMock(), get=get),
                         (False, "Mistral says this key isn't valid."))
        get = MagicMock(return_value=resp(429, {"error": {"message": "Rate limit"}}))
        self.assertIn("rate limit", api_keys.test_writer_key("openai", "o", post=MagicMock(), get=get)[1])

    def test_test_key_picks_the_right_test(self):
        calls = []
        orig = (api_keys.test_mistral, api_keys.test_gemini, api_keys.test_writer_key)
        try:
            api_keys.test_mistral = lambda key, model=None: calls.append(("m", key, model)) or (True, "")
            api_keys.test_gemini = lambda key: calls.append(("g", key)) or (True, "")
            api_keys.test_writer_key = lambda p, key, b, m: calls.append(("w", p, key, b, m)) or (True, "")
            api_keys.test_key("mistral", "k1", {"mistral_stt_model": "voxtral-mini-2602"})
            api_keys.test_key("google_api_key", "k2")
            api_keys.test_key("writer", "k3", {"action_model": actions.API_ANTHROPIC_ID,
                                                "action_api_model": "claude-opus-4-8"})
            api_keys.test_key("writer", "k4", {"action_model": "managed", "action_api_provider": "cerebras"})
        finally:
            api_keys.test_mistral, api_keys.test_gemini, api_keys.test_writer_key = orig
        self.assertEqual(calls, [("m", "k1", "voxtral-mini-2602"), ("g", "k2"),
                                 ("w", actions.API_ANTHROPIC_ID, "k3", None, "claude-opus-4-8"),
                                 ("w", "cerebras", "k4", None, None)])


class TestRunInBackground(unittest.TestCase):
    def test_result_and_error(self):
        got, done = [], threading.Event()
        api_keys.run_in_background(lambda a, b: a + b, 2, 3, on_done=lambda r: (got.append(r), done.set()))
        self.assertTrue(done.wait(5))
        self.assertEqual(got, [5])
        errs, done = [], threading.Event()

        def boom():
            raise RuntimeError("x")
        api_keys.run_in_background(boom, on_error=lambda e: (errs.append(e), done.set()))
        self.assertTrue(done.wait(5))
        self.assertIsInstance(errs[0], RuntimeError)


def _stubbed_main():
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None or type(qtcore).__name__ != "StubModule":
        return None
    import main
    return main


class TestParityWithMain(unittest.TestCase):
    def test_cloud_error_message_matches_main(self):
        main = _stubbed_main()
        if main is None:
            self.skipTest("needs test_core's PySide6 stand-ins (run the whole tests/ folder)")
        cases = [(401, '{"message": "Unauthorized"}'), (402, "no credit"), (429, '{"error": "quota"}'),
                 (400, '{"error": {"message": "bad language"}}'), (400, "oops"), (500, ""),
                 (400, '{"error": {"message": "API key not valid"}}')]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                self.assertEqual(api_keys.cloud_error_message("Mistral", status, body),
                                 main.cloud_error_message("Mistral", status, body))


if __name__ == "__main__":
    unittest.main()
