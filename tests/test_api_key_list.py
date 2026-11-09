"""Settings › My API keys (api_keys.py): any number of named keys - add, edit, remove, test, the
move of keys saved the old way, the per-account isolation, the keyring - and the Gusan Cloud
AI writer the person picked, sent to the server (action_api "model")."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import actions  # noqa: E402
import action_api  # noqa: E402
import api_keys  # noqa: E402
import entitlements  # noqa: E402


class FakeApp:
    def __init__(self, **cfg):
        self.cfg = {"api_keys": [], "api_key_secrets": {}, "speech_key_id": "", "writer_key_id": "",
                    "backend": "local", "mistral_api_key": "", "google_api_key": "", "action_api_key": "",
                    "action_model": "qwen_tiny"}
        self.cfg.update(cfg)
        self.applied = []

    def set_settings(self, changes):
        self.applied.append(dict(changes))
        self.cfg.update(changes)
        return {k: True for k in changes}


class TestEntries(unittest.TestCase):
    def test_add_edit_remove(self):
        app = FakeApp()
        key_id = api_keys.add_entry(app, "My OpenAI", "api_openai_compatible", "gpt-5.4-mini", "sk-abcdefgh123",
                                    base_url="https://api.openai.com/v1/")
        self.assertTrue(key_id)
        e = api_keys.entry(app.cfg, key_id)
        self.assertEqual((e["name"], e["model"], e["base_url"]), ("My OpenAI", "gpt-5.4-mini", ""))  # default URL
        self.assertEqual(api_keys.secret_of(app.cfg, key_id), "sk-abcdefgh123")
        view = api_keys.entry_view(app.cfg, e)
        self.assertEqual((view["masked"], view["speech"]), ("Key ending 123", False))
        self.assertNotIn("sk-abcdefgh123", json.dumps(app.cfg["api_keys"]))   # the secret never sits in the list
        self.assertTrue(api_keys.update_entry(app, key_id, "Work", "api_openai_compatible", "gpt-5.5", key=None,
                                              base_url="https://llm.example.com/v1"))
        e = api_keys.entry(app.cfg, key_id)
        self.assertEqual((e["name"], e["model"], e["base_url"]), ("Work", "gpt-5.5", "https://llm.example.com/v1"))
        self.assertEqual(api_keys.secret_of(app.cfg, key_id), "sk-abcdefgh123")          # kept
        self.assertTrue(api_keys.remove_entry(app, key_id))
        self.assertEqual((app.cfg["api_keys"], app.cfg["api_key_secrets"]), ([], {}))

    def test_a_google_or_mistral_key_fills_its_empty_slot(self):
        app = FakeApp()
        key_id = api_keys.add_entry(app, "G", "api_gemini", "", "AIza-1234567")
        self.assertEqual(app.cfg["google_api_key"], "AIza-1234567")          # Live Assistant, Audio to text see it
        app.cfg["google_api_key"] = "AIza-other"
        api_keys.add_entry(app, "G2", "api_gemini", "", "AIza-7654321")
        self.assertEqual(app.cfg["google_api_key"], "AIza-other")            # a filled slot is never overwritten
        app.cfg["google_api_key"] = "AIza-1234567"
        api_keys.remove_entry(app, key_id)
        self.assertEqual(app.cfg["google_api_key"], "")                      # and emptied with its key

    def test_no_key_no_entry(self):
        self.assertEqual(api_keys.add_entry(FakeApp(), "x", "api_mistral", "", "  "), "")
        self.assertEqual(api_keys.add_entry(FakeApp(), "x", "not_a_provider", "", "k"), "")

    def test_a_name_defaults_to_the_provider(self):
        app = FakeApp()
        key_id = api_keys.add_entry(app, "", "api_anthropic", "", "sk-ant-123456")
        e = api_keys.entry(app.cfg, key_id)
        self.assertEqual(e["name"], "Anthropic Claude")
        self.assertTrue(e["model"])                                           # the provider's default model

    def test_removing_the_key_speech_uses_goes_back_to_this_computer(self):
        app = FakeApp()
        key_id = api_keys.add_entry(app, "M", "api_mistral", "", "mk-1234567890")
        app.cfg.update(backend="mistral", mistral_api_key="mk-1234567890", speech_key_id=key_id)
        api_keys.remove_entry(app, key_id)
        self.assertEqual((app.cfg["backend"], app.cfg["mistral_api_key"], app.cfg["speech_key_id"]),
                         ("local", "", ""))

    def test_removing_the_writers_key_empties_its_slot(self):
        app = FakeApp()
        key_id = api_keys.add_entry(app, "G", "api_gemini", "", "AIza-1234567")
        app.cfg.update(action_model=actions.API_GEMINI_ID, google_api_key="AIza-1234567", writer_key_id=key_id)
        api_keys.remove_entry(app, key_id)
        self.assertEqual((app.cfg["google_api_key"], app.cfg["writer_key_id"]), ("", ""))

    def test_editing_the_key_a_card_uses_copies_it_again(self):
        app = FakeApp()
        key_id = api_keys.add_entry(app, "M", "api_mistral", "", "mk-old-000000")
        app.cfg.update(backend="mistral", mistral_api_key="mk-old-000000", speech_key_id=key_id)
        api_keys.update_entry(app, key_id, "M", "api_mistral", "", key="mk-new-111111")
        self.assertEqual(app.cfg["mistral_api_key"], "mk-new-111111")


class TestReviewFixes(unittest.TestCase):
    """What a review of the key list found: each case stays fixed."""

    def test_switching_between_compatible_keys_never_keeps_the_other_address(self):
        import engine_choice as ec
        app = FakeApp()
        e1 = api_keys.add_entry(app, "Self-hosted", "api_openai_compatible", "m1", "sk-111111111",
                                base_url="https://llm.example.com/v1")
        e2 = api_keys.add_entry(app, "OpenAI", "api_openai_compatible", "gpt-5.4-mini", "sk-222222222")
        app.cfg.update(ec.set_writer_value(app.cfg, "key:" + e1)["changes"])
        self.assertEqual(app.cfg["action_api_base_url"], "https://llm.example.com/v1")
        app.cfg.update(ec.set_writer_value(app.cfg, "key:" + e2)["changes"])
        self.assertEqual(app.cfg["action_api_base_url"], "https://api.openai.com/v1")     # its own, not E1's
        self.assertEqual(app.cfg["action_api_key"], "sk-222222222")

    def test_speech_and_a_gemini_writer_share_the_google_key_and_say_so(self):
        import engine_choice as ec
        app = FakeApp()
        s = api_keys.add_entry(app, "Speech G", "api_gemini", "", "AIza-speech-1")
        w = api_keys.add_entry(app, "Writer G", "api_gemini", "", "AIza-writer-2")
        app.cfg.update(ec.set_dictation_speech(app.cfg, "key:" + s)["changes"])
        app.cfg.update(ec.set_writer_value(app.cfg, "key:" + w)["changes"])
        self.assertEqual(app.cfg["google_api_key"], "AIza-writer-2")
        self.assertEqual(app.cfg["speech_key_id"], w)                       # the card shows the key really used

    def test_editing_a_secret_replaces_it_in_every_slot(self):
        app = FakeApp()
        key_id = api_keys.add_entry(app, "M", "api_mistral", "", "mk-old-0000000")
        self.assertEqual(app.cfg["mistral_api_key"], "mk-old-0000000")      # filled when it was added
        api_keys.update_entry(app, key_id, "M", "api_mistral", "", key="mk-new-1111111")
        self.assertEqual(app.cfg["mistral_api_key"], "mk-new-1111111")
        api_keys.remove_entry(app, key_id)
        self.assertEqual(app.cfg["mistral_api_key"], "")

    def test_the_writer_chip_follows_what_really_writes(self):
        app = FakeApp()
        key_id = api_keys.add_entry(app, "O", "api_openai_compatible", "", "sk-333333333")
        app.cfg.update(writer_key_id=key_id, action_model="api_openai_compatible")
        self.assertEqual(api_keys.entry_view(app.cfg, api_keys.entry(app.cfg, key_id))["in_use"], "AI writer")
        app.cfg["action_model"] = "qwen_tiny"                                # the writer moved to this computer
        self.assertEqual(api_keys.entry_view(app.cfg, api_keys.entry(app.cfg, key_id))["in_use"], "")

    def test_a_stashed_accounts_keys_get_their_own_keyring_items(self):
        import main
        store = {}
        cfg = {**main.DEFAULT, "api_keys": [], "api_key_secrets": {},
               "user_secrets": {"user:b": {"api_keys": [{"id": "kb", "name": "B", "provider": "api_mistral",
                                                         "model": "", "base_url": ""}],
                                           "api_key_secrets": {"kb": "mk-b-secret"}}}}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(main, "CONFIG_PATH", os.path.join(d, "config.json")), \
                mock.patch.object(main.storage, "write_secret", side_effect=lambda n, v: store.__setitem__(n, v) or True):
            main.save_config(cfg)
            with open(main.CONFIG_PATH, encoding="utf-8") as f:
                disk = f.read()
        self.assertNotIn("mk-b-secret", disk)
        self.assertEqual(store[main.storage.api_key_secret_name("kb")], "mk-b-secret")
        self.assertNotIn("mk-b-secret", store.get(main.storage.USER_SECRETS_SECRET, ""))   # never in the one blob
        self.assertEqual(cfg["user_secrets"]["user:b"]["api_key_secrets"], {"kb": "mk-b-secret"})  # live cfg kept


class TestOldKeysMove(unittest.TestCase):
    def test_the_old_slots_become_entries_once(self):
        cfg = {"mistral_api_key": "mk-aaaaaaaaaa", "backend": "mistral", "google_api_key": "AIza-bbbbbbb",
               "action_model": actions.API_OPENAI_ID, "action_api_key": "sk-cccccccc",
               "action_api_model": "gpt-5.4", "action_api_base_url": ""}
        moved = api_keys.slot_migration(cfg)
        cfg.update(moved)
        providers = sorted(e["provider"] for e in api_keys.entries(cfg))
        self.assertEqual(providers, sorted([actions.API_MISTRAL_ID, actions.API_GEMINI_ID, actions.API_OPENAI_ID]))
        self.assertTrue(cfg["speech_key_id"] and cfg["writer_key_id"])
        self.assertEqual(api_keys.secret_of(cfg, cfg["speech_key_id"]), "mk-aaaaaaaaaa")
        self.assertEqual(api_keys.slot_migration(cfg), {})                     # nothing moves twice

    def test_nothing_to_move(self):
        self.assertEqual(api_keys.slot_migration({}), {})

    def test_a_key_removed_after_the_move_never_comes_back(self):
        cfg = {"action_api_key": "sk-old-0000000", "action_api_provider": "api_openai_compatible",
               "action_model": "qwen_tiny"}
        cfg.update(api_keys.slot_migration(cfg))
        self.assertTrue(cfg[api_keys.MIGRATED_KEY] and len(api_keys.entries(cfg)) == 1)
        cfg.update(api_keys=[], api_key_secrets={})                            # removed in the list
        self.assertEqual(api_keys.slot_migration(cfg), {})


class TestTest(unittest.TestCase):
    def test_mistral_checks_transcription_the_rest_ping_the_chat(self):
        with mock.patch.object(api_keys, "test_mistral", return_value=(True, "Works")) as m, \
                mock.patch.object(api_keys, "test_writer_key", return_value=(False, "no")) as w:
            self.assertEqual(api_keys.test_entry(actions.API_MISTRAL_ID, "mk"), (True, "Works"))
            self.assertEqual(api_keys.test_entry(actions.API_OPENAI_ID, "sk", "gpt-5.5", "https://x/v1"),
                             (False, "no"))
        m.assert_called_once_with("mk")
        w.assert_called_once_with(actions.API_OPENAI_ID, "sk", "https://x/v1", "gpt-5.5")


class TestPerAccount(unittest.TestCase):
    def test_another_account_never_sees_the_keys(self):
        cfg = {"secrets_owner": "user:a", "api_keys": [{"id": "k", "name": "A's", "provider": "api_mistral",
                                                        "model": "", "base_url": ""}],
               "api_key_secrets": {"k": "mk-secret"}, "speech_key_id": "k"}
        self.assertTrue(entitlements.reconcile_user_secrets(cfg, "user:b"))
        self.assertEqual((cfg["api_keys"], cfg["api_key_secrets"], cfg["speech_key_id"]), ([], {}, ""))
        cfg["api_keys"].append({"id": "x"})                                    # B's list is B's own
        self.assertEqual(entitlements.USER_SCOPED_SECRET_DEFAULTS["api_keys"], [])
        self.assertTrue(entitlements.reconcile_user_secrets(cfg, "user:a"))
        self.assertEqual(cfg["api_key_secrets"], {"k": "mk-secret"})


class TestKeyring(unittest.TestCase):
    def test_the_secrets_go_to_the_keyring_and_come_back(self):
        import main
        store = {}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(main, "CONFIG_PATH", os.path.join(d, "config.json")), \
                mock.patch.object(main.storage, "write_secret", side_effect=lambda n, v: store.__setitem__(n, v) or True), \
                mock.patch.object(main.storage, "read_secret", side_effect=lambda n: store.get(n, "")):
            cfg = {**main.DEFAULT, "api_keys": [{"id": "k", "name": "N", "provider": "api_mistral", "model": "",
                                                 "base_url": ""}], "api_key_secrets": {"k": "mk-secret"}}
            main.save_config(cfg)
            with open(main.CONFIG_PATH, encoding="utf-8") as f:
                disk = json.load(f)
            self.assertEqual(disk["api_key_secrets"], {})
            self.assertNotIn("mk-secret", json.dumps(disk))
            self.assertEqual(store[main.storage.api_key_secret_name("k")], "mk-secret")   # one item per key
            loaded = main.load_config()
            self.assertEqual(loaded["api_key_secrets"], {"k": "mk-secret"})
            main.save_config(dict(cfg, api_keys=[], api_key_secrets={}))                  # the key was removed
            self.assertEqual(store[main.storage.api_key_secret_name("k")], "")             # its item is deleted


class TestCloudWriterChoice(unittest.TestCase):
    def test_the_pick_rides_along(self):
        self.assertEqual(action_api._managed_extra(prefer="claude"), {"model": "claude"})
        self.assertEqual(action_api._managed_extra(prefer=""), {})
        self.assertEqual(action_api._managed_extra(prefer="something-else"), {})     # never an unknown name

    def test_actions_pass_the_settings_pick(self):
        seen = {}

        def fake(text, mode, token, **kw):
            seen.update(kw)
            return "ok"
        with mock.patch.object(actions.action_api, "run_managed_action", side_effect=fake):
            actions.process("hello there", actions.ACTION_SMART_AUTO, model=actions.API_MANAGED_ID,
                            config={"_managed_token": "T", "cloud_writer_model": "gemini"})
        self.assertEqual(seen.get("prefer"), "gemini")

    def test_the_server_moves_the_pick_first(self):
        src = open(os.path.join(ROOT, "supabase", "functions", "smart-action", "index.ts"), encoding="utf-8").read()
        self.assertIn("function preferRoutes(", src)
        # meeting notes and the Interpreter's phrases (mode "interpret") keep their own order
        self.assertIn("const routes = notes || interpret ? chain : preferRoutes(chain, body?.model);", src)
        for fam in action_api.CLOUD_WRITER_FAMILIES:
            self.assertIn('"%s"' % fam, src)


if __name__ == "__main__":
    unittest.main()
