"""engine_choice: the "Runs on …" lines and the Speech & AI choices.

Pure: a cfg dict plus pro/signed_in, and a fake "is it downloaded". The
parity tests with main._resolve_action_engine / main.normalize_mistral_model
need main, which only loads under test_core's PySide6 stand-ins (the whole
tests/ run); run alone they skip.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import actions  # noqa: E402
import engine_choice as ec  # noqa: E402
import local_llm  # noqa: E402

QWEN = local_llm.QWEN_TINY_ID


def on_disk(*ids):
    return lambda m: m in ids


NOTHING = on_disk()


class TestSpeech(unittest.TestCase):
    def test_local_model_names(self):
        r = ec.runs_on({"backend": "local", "whisper_model": "small"}, "dictation")
        self.assertEqual(r["label"], "Balanced · Whisper Small · on this computer")
        self.assertEqual((r["kind"], r["choice"], r["plain"], r["model_name"], r["size"]),
                         ("local", "This computer", "Balanced", "Whisper Small", "460 MB"))
        self.assertEqual(ec.runs_on({"whisper_model": "large-v3-turbo"}, "speech")["label"],
                         "Most accurate · Whisper Large v3 Turbo · on this computer")
        self.assertEqual(ec.runs_on({}, "meetings")["plain"], "Fast")       # default base

    def test_gusan_cloud_needs_pro(self):
        cfg = {"backend": "managed", "managed_provider": "gemini", "whisper_model": "small"}
        r = ec.runs_on(cfg, "dictation", pro=True)
        self.assertEqual(r["label"], "Gusan Cloud · Gemini 2.5 Flash")
        self.assertTrue(r["pro"])
        self.assertEqual(ec.runs_on(dict(cfg, managed_provider="mistral"), "dictation", pro=True)["model_name"],
                         "Voxtral Mini")
        # Not Pro (lapsed): the proxy refuses and dictation runs on this computer.
        self.assertEqual(ec.runs_on(cfg, "dictation", pro=False)["kind"], "local")

    def test_own_key_needs_the_key(self):
        cfg = {"backend": "mistral", "mistral_api_key": "k", "mistral_stt_model": "voxtral-small-latest"}
        r = ec.runs_on(cfg, "dictation")
        self.assertEqual(r["label"], "Mistral · Voxtral Mini · your own key")
        self.assertEqual(r["model_id"], "voxtral-mini-latest")              # healed like main
        self.assertEqual(ec.runs_on({"backend": "google", "google_api_key": "g"}, "dictation")["label"],
                         "Google Gemini · Gemini 2.5 Flash · your own key")
        # No key: AudioRecorder falls back to the local model.
        self.assertEqual(ec.runs_on({"backend": "google", "google_api_key": ""}, "dictation")["kind"], "local")

    def test_privacy_is_this_computer(self):
        cfg = {"backend": "managed", "privacy_mode": True, "whisper_model": "base"}
        self.assertEqual(ec.runs_on(cfg, "dictation", pro=True)["kind"], "local")

    def test_app_object(self):
        app = SimpleNamespace(cfg={"backend": "managed"}, is_pro=lambda: True,
                              auth=SimpleNamespace(is_authenticated=True))
        self.assertEqual(ec.runs_on(app, "dictation")["kind"], "cloud")
        app.is_pro = MagicMock(side_effect=RuntimeError)
        self.assertEqual(ec.runs_on(app, "dictation")["kind"], "local")

    def test_unknown_feature(self):
        with self.assertRaises(ValueError):
            ec.runs_on({}, "teleport")


class TestWriter(unittest.TestCase):
    def test_free_rule_based_is_basic(self):
        r = ec.runs_on({"action_model": "rule_based"}, "smart_actions", downloaded=NOTHING)
        self.assertEqual((r["kind"], r["label"]), ("basic", "This computer · no AI model yet"))

    def test_pro_rule_based_goes_to_gusan_cloud(self):
        r = ec.runs_on({"action_model": "rule_based"}, "smart_actions", pro=True, downloaded=NOTHING)
        self.assertEqual(r["label"], "Gusan Cloud · DeepSeek V4.1 Flash")
        self.assertEqual(r["model_id"], actions.API_MANAGED_ID)
        # Meeting notes are written by Claude Sonnet 5.5 first (smart-action routes by mode).
        r = ec.runs_on({"action_model": "rule_based"}, "meeting_notes", pro=True, downloaded=NOTHING)
        self.assertEqual(r["label"], "Gusan Cloud · Claude Sonnet 5.5")
        # Pro but signed out (no token): stays basic, as actions.process does.
        r = ec.runs_on({"action_model": "managed"}, "smart_actions", pro=True, signed_in=False,
                       downloaded=NOTHING)
        self.assertEqual(r["kind"], "basic")

    def test_local_model(self):
        cfg = {"action_model": QWEN}
        r = ec.runs_on(cfg, "smart_actions", downloaded=on_disk(QWEN))
        self.assertEqual(r["label"], "This computer · Qwen 1.5B")
        self.assertEqual((r["plain"], r["ready"]), ("Small", True))
        r = ec.runs_on(cfg, "meeting_notes", downloaded=NOTHING)
        self.assertEqual((r["ready"], r["problem"]), (False, "needs_download"))
        # A Pro user who picked a local model keeps it.
        self.assertEqual(ec.runs_on(cfg, "smart_actions", pro=True, downloaded=on_disk(QWEN))["kind"], "local")

    def test_own_key_writer(self):
        cfg = {"action_model": actions.API_MISTRAL_ID, "mistral_api_key": "m", "action_api_model": ""}
        r = ec.runs_on(cfg, "smart_actions")
        self.assertEqual(r["label"], "Mistral · Ministral 14B · your own key")
        self.assertTrue(r["ready"])
        r = ec.runs_on({"action_model": actions.API_ANTHROPIC_ID}, "smart_actions")
        self.assertEqual((r["kind"], r["problem"]), ("own_key", "needs_key"))
        # Pro without their own key: Gusan Cloud instead (main._resolve_action_engine).
        self.assertEqual(ec.runs_on({"action_model": actions.API_ANTHROPIC_ID}, "smart_actions",
                                    pro=True)["kind"], "cloud")
        # Pro with their own key keeps it.
        self.assertEqual(ec.runs_on({"action_model": actions.API_GEMINI_ID, "google_api_key": "g"},
                                    "smart_actions", pro=True)["kind"], "own_key")

    def test_privacy_keeps_the_writer_local(self):
        cfg = {"action_model": "managed", "privacy_mode": True}
        self.assertEqual(ec.runs_on(cfg, "smart_actions", pro=True, downloaded=NOTHING)["kind"], "basic")
        cfg = {"action_model": QWEN, "privacy_mode": True}
        self.assertEqual(ec.runs_on(cfg, "smart_actions", pro=True, downloaded=on_disk(QWEN))["kind"], "local")

    def test_meeting_notes_override(self):
        r = ec.runs_on({"action_model": "rule_based"}, "meeting_notes", action_model=QWEN,
                       downloaded=on_disk(QWEN))
        self.assertEqual(r["model_name"], "Qwen 1.5B")


class TestLive(unittest.TestCase):
    def test_pro_answers_on_gusan_cloud(self):
        r = ec.runs_on({"action_model": QWEN}, "live_assistant", pro=True, downloaded=on_disk(QWEN))
        self.assertEqual(r["label"], "Gusan Cloud · DeepSeek V4.1 Flash")

    def test_free_uses_a_downloaded_model_or_nothing(self):
        r = ec.runs_on({"action_model": "rule_based"}, "live_assistant", downloaded=on_disk(local_llm.QWEN_3B_ID))
        self.assertEqual(r["model_name"], "Qwen 3B")
        r = ec.runs_on({"action_model": "rule_based"}, "live_assistant", downloaded=NOTHING)
        self.assertEqual((r["kind"], r["ready"], r["label"]), ("none", False, "No AI model yet"))

    def test_free_own_key(self):
        cfg = {"action_model": actions.API_GEMINI_ID, "google_api_key": "g"}
        self.assertEqual(ec.runs_on(cfg, "live_assistant", downloaded=NOTHING)["kind"], "own_key")

    def test_privacy(self):
        cfg = {"action_model": "managed", "privacy_mode": True}
        r = ec.runs_on(cfg, "live_assistant", pro=True, downloaded=on_disk(QWEN))
        self.assertEqual((r["kind"], r["model_id"]), ("local", QWEN))

    def test_answers_written_by_this_computer(self):
        """live_assist_writer "local" (the page's "This computer"): local even for Pro."""
        cfg = {"action_model": "managed", "live_assist_writer": "local"}
        r = ec.runs_on(cfg, "live_assistant", pro=True, downloaded=on_disk(QWEN))
        self.assertEqual((r["kind"], r["model_id"]), ("local", QWEN))
        r = ec.runs_on(cfg, "live_assistant", pro=True, downloaded=NOTHING)
        self.assertEqual((r["kind"], r["ready"]), ("none", False))      # nothing goes to the cloud
        with patch.object(local_llm, "model_downloaded", side_effect=lambda m: m == QWEN):
            self.assertEqual(actions.live_engine(actions.API_MANAGED_ID,
                                                 dict(cfg, _managed_token="t")), QWEN)
        for writer in ("", "cloud"):                                       # automatic / Gusan Cloud
            r = ec.runs_on(dict(cfg, live_assist_writer=writer), "live_assistant", pro=True,
                           downloaded=on_disk(QWEN))
            self.assertEqual(r["kind"], "cloud")

    def test_matches_actions_live_engine(self):
        """The same engine as actions.live_engine for every case it decides."""
        cases = [({"action_model": m, "google_api_key": k, "privacy_mode": p, "live_assist_writer": w},
                  tok, disk)
                 for m in ("rule_based", "managed", QWEN, actions.API_GEMINI_ID)
                 for k in ("", "g") for p in (False, True) for tok in (False, True)
                 for w in ("", "local") for disk in ((), (local_llm.QWEN_3B_ID,))]
        for cfg, tok, disk in cases:
            with self.subTest(cfg=cfg, token=tok, disk=disk):
                engine, _ = ec.resolve_action_engine(cfg, tok, tok)
                config = dict(cfg, _managed_token="t") if tok else dict(cfg)
                with patch.object(local_llm, "model_downloaded", side_effect=lambda m: m in disk):
                    want = actions.live_engine(engine, config)
                got = ec.runs_on(cfg, "live_assistant", pro=tok, signed_in=tok, downloaded=on_disk(*disk))
                self.assertEqual(got["model_id"] or None, want)


class TestAudioToText(unittest.TestCase):
    def test_no_pick_is_auto_on_this_computer(self):
        cfg = {"backend": "managed"}
        self.assertEqual(ec.runs_on(cfg, "audio_to_text", pro=True)["label"], "Auto · on this computer")
        self.assertEqual(ec.runs_on(cfg, "files", pro=True, file_model="small")["label"],
                         "Balanced · Whisper Small · on this computer")


GKEY = {"google_api_key": "g-key"}
MKEY = {"mistral_api_key": "m-key"}
BOTH = dict(GKEY, **MKEY)


def files(value, **cfg):
    """A cfg dict with Audio to text's pick."""
    return dict(cfg, file_model=value)


class TestAudioToTextModels(unittest.TestCase):
    """Cloud models in Audio to text (cfg file_model)."""

    def test_constants(self):
        self.assertEqual((ec.FILE_KEY, ec.FILE_AUTO, ec.FILE_CLOUD, ec.FILE_OWN_GOOGLE, ec.FILE_OWN_MISTRAL),
                         ("file_model", "auto", "cloud", "own:google", "own:mistral"))
        self.assertEqual(ec.FILE_CLOUD_VALUES, ("cloud", "own:google", "own:mistral"))
        self.assertFalse(set(ec.FILE_CLOUD_VALUES) & set(ec.SPEECH_MODELS))

    def test_session_matrix(self):
        for pro in (False, True):
            for privacy in (False, True):
                for keys in ({}, GKEY, MKEY, BOTH, {"google_api_key": "  ", "mistral_api_key": " "}):
                    with self.subTest(pro=pro, privacy=privacy, keys=keys):
                        base = dict(keys, privacy_mode=privacy, backend="managed")
                        s = lambda v: ec.file_session(files(v, **base), pro)   # noqa: E731
                        self.assertEqual(s("auto"), ("local", None))
                        self.assertEqual(s(""), ("local", None))
                        self.assertEqual(ec.file_session(base, pro), ("local", None))     # never picked
                        self.assertEqual(s("huge-v9"), ("local", None))
                        self.assertEqual(s("small"), ("local", "small"))
                        cloud = ("managed", "gemini") if (pro and not privacy) else ("local", None)
                        self.assertEqual(s("cloud"), cloud)
                        g = ("google", "gemini-2.5-flash") if (str(keys.get("google_api_key", "")).strip()
                                                              and not privacy) else ("local", None)
                        self.assertEqual(s("own:google"), g)
                        m = ("mistral", "voxtral-mini-latest") if (str(keys.get("mistral_api_key", "")).strip()
                                                                   and not privacy) else ("local", None)
                        self.assertEqual(s("own:mistral"), m)
        self.assertEqual(ec.file_session(None, True), ("local", None))
        for mid in ec.SPEECH_MODELS:
            self.assertEqual(ec.file_session(files(mid, privacy_mode=True), False), ("local", mid))

    def test_session_provider_and_models(self):
        self.assertEqual(ec.file_session(files("cloud", managed_provider="mistral"), True), ("managed", "mistral"))
        self.assertEqual(ec.file_session(files("cloud", managed_provider=" Mistral "), True),
                         ("managed", "mistral"))
        self.assertEqual(ec.file_session(files("cloud", managed_provider="aws"), True), ("managed", "gemini"))
        cfg = files("own:google", google_stt_model="gemini-2.5-pro", **GKEY)
        self.assertEqual(ec.file_session(cfg, False), ("google", "gemini-2.5-pro"))
        cfg = files("own:mistral", mistral_stt_model="voxtral-mini-2602", **MKEY)
        self.assertEqual(ec.file_session(cfg, False), ("mistral", "voxtral-mini-2602"))
        cfg = files("own:mistral", mistral_stt_model="voxtral-small-latest", **MKEY)
        self.assertEqual(ec.file_session(cfg, False), ("mistral", "voxtral-mini-latest"))    # healed like main

    def test_model_id(self):
        self.assertEqual(ec.file_model_id({}, "cloud"), "gemini-2.5-flash")
        self.assertEqual(ec.file_model_id({"managed_provider": "mistral"}, "cloud"), "voxtral-mini-latest")
        # Gusan Cloud runs the proxy's model, whatever the own-key models say.
        self.assertEqual(ec.file_model_id({"managed_provider": "mistral", "mistral_stt_model": "voxtral-mini-2507"},
                                          "cloud"), "voxtral-mini-latest")
        self.assertEqual(ec.file_model_id({}, "own:google"), "gemini-2.5-flash")
        self.assertEqual(ec.file_model_id({"google_stt_model": " gemini-2.5-pro "}, "own:google"), "gemini-2.5-pro")
        self.assertEqual(ec.file_model_id({"google_stt_model": "gemini-demo"}, "own:google"), "gemini-demo")
        self.assertEqual(ec.file_model_id({}, "own:mistral"), "voxtral-mini-latest")
        self.assertEqual(ec.file_model_id({"mistral_stt_model": "voxtral-mini-2507"}, "own:mistral"),
                         "voxtral-mini-2507")
        self.assertEqual(ec.file_model_id({"mistral_stt_model": "x"}, "own:mistral"), "voxtral-mini-latest")
        self.assertEqual(ec.file_model_id({}, "small"), "small")
        self.assertEqual(ec.file_model_id({}, "auto"), "")
        self.assertEqual(ec.file_model_id({}, "huge-v9"), "")
        self.assertEqual(ec.file_model_id(files("cloud", managed_provider="mistral")), "voxtral-mini-latest")
        self.assertEqual(ec.file_model_id(None), "")
        # The ids don't depend on Pro, Privacy mode or keys (file_session decides those).
        self.assertEqual(ec.file_model_id({"privacy_mode": True}, "own:google"), "gemini-2.5-flash")

    def test_every_cloud_id_has_a_real_name(self):
        ids = set(ec.CLOUD_SPEECH_IDS.values()) | ec.MISTRAL_STT_MODELS | {ec.GOOGLE_STT_DEFAULT}
        ids |= {m for m, _ in ec.WRITER_PROVIDERS[actions.API_GEMINI_ID]["models"]}    # google_stt_model picks
        for mid in ids:
            with self.subTest(model=mid):
                self.assertIn(mid, ec.MODEL_NAMES)
        for provider, name in ec.CLOUD_SPEECH_MODELS.items():
            self.assertEqual(ec.MODEL_NAMES[ec.CLOUD_SPEECH_IDS[provider]], name)
        self.assertEqual(set(ec.CLOUD_SPEECH_IDS), set(ec.CLOUD_SPEECH_MODELS))
        for value in ec.FILE_CLOUD_VALUES:
            for cfg in ({}, {"managed_provider": "mistral"}):
                self.assertIn(ec.file_model_id(cfg, value), ec.MODEL_NAMES)

    def test_the_proxy_runs_these_models(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "supabase", "functions", "transcribe-proxy", "index.ts")
        if not os.path.exists(path):
            self.skipTest("no transcribe-proxy source here")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        for mid in ec.CLOUD_SPEECH_IDS.values():
            self.assertIn(mid, source)

    def test_choices(self):
        choices = ec.file_choices()
        self.assertEqual(choices[0], ("auto", "Automatic", "Automatic", False))
        self.assertEqual([c[0] for c in choices[1:-1]], list(ec.SPEECH_MODELS))
        self.assertIn(("small", "Balanced", "Balanced · Whisper Small", False), choices)
        self.assertTrue(all(not c[3] for c in choices[:-1]))
        self.assertEqual(choices[-1], ("cloud", "Gusan Cloud", "Gusan Cloud · Gemini 2.5 Flash", True))
        self.assertEqual([c[1] for c in choices],
                         ["Automatic", "Lightest", "Fast", "Balanced", "Accurate", "Most accurate", "Maximum",
                          "Gusan Cloud"])
        self.assertEqual(ec.file_choices({"managed_provider": "mistral"})[-1][2], "Gusan Cloud · Voxtral Mini")

    def test_own_key_choices_only_with_the_key(self):
        def own(cfg):
            return [c for c in ec.file_choices(cfg) if c[0].startswith("own:")]
        self.assertEqual(own({}), [])
        self.assertEqual(own({"google_api_key": "  ", "mistral_api_key": ""}), [])
        google = ("own:google", "Google Gemini", "Google Gemini · Gemini 2.5 Flash · your own key", False)
        mistral = ("own:mistral", "Mistral", "Mistral · Voxtral Mini · your own key", False)
        self.assertEqual(own(GKEY), [google])
        self.assertEqual(own(MKEY), [mistral])
        self.assertEqual(own(BOTH), [google, mistral])
        self.assertEqual(ec.file_choices(BOTH)[-3][0], "cloud")             # Gusan Cloud, then own keys
        # The line names the model that will really run (same words as Dictation's).
        cfg = dict(BOTH, google_stt_model="gemini-2.5-pro", mistral_stt_model="voxtral-mini-2602")
        self.assertEqual([c[2] for c in own(cfg)], ["Google Gemini · Gemini 2.5 Pro · your own key",
                                                    "Mistral · Voxtral Mini 2602 · your own key"])
        self.assertEqual(own(dict(GKEY, backend="google"))[0][2],
                         ec.runs_on(dict(GKEY, backend="google"), "dictation")["label"])
        # Privacy mode keeps them listed (the page greys them out), with the real line.
        self.assertEqual(own(dict(BOTH, privacy_mode=True)), [google, mistral])
        self.assertEqual(ec.file_choices(dict(BOTH, privacy_mode=True))[-3][2], "Gusan Cloud · Gemini 2.5 Flash")

    def test_engine_gusan_cloud(self):
        cfg = files("cloud", backend="local", whisper_model="small")
        r = ec.file_engine(cfg, pro=True)
        self.assertEqual((r["feature"], r["kind"], r["label"], r["model_name"], r["model_id"], r["pro"]),
                         ("audio_to_text", "cloud", "Gusan Cloud · Gemini 2.5 Flash", "Gemini 2.5 Flash",
                          "gemini-2.5-flash", True))
        r = ec.file_engine(dict(cfg, managed_provider="mistral"), pro=True)
        self.assertEqual((r["model_name"], r["model_id"]), ("Voxtral Mini", "voxtral-mini-latest"))
        # Free (or Pro ended), or Privacy mode: Automatic on this computer.
        self.assertEqual(ec.file_engine(cfg, pro=False)["label"], "Auto · on this computer")
        r = ec.file_engine(dict(cfg, privacy_mode=True), pro=True)
        self.assertEqual((r["kind"], r["model_id"]), ("local", "auto"))

    def test_engine_own_keys(self):
        r = ec.file_engine(files("own:google", **GKEY))
        self.assertEqual((r["kind"], r["choice"], r["plain"], r["model_name"], r["model_id"], r["label"]),
                         ("own_key", "Your own key", "Google Gemini", "Gemini 2.5 Flash", "gemini-2.5-flash",
                          "Google Gemini · Gemini 2.5 Flash · your own key"))
        r = ec.file_engine(files("own:google", google_stt_model="gemini-2.5-pro", **GKEY), pro=True)
        self.assertEqual((r["model_name"], r["model_id"]), ("Gemini 2.5 Pro", "gemini-2.5-pro"))
        r = ec.file_engine(files("own:mistral", mistral_stt_model="voxtral-mini-2602", **MKEY))
        self.assertEqual((r["kind"], r["plain"], r["model_name"], r["model_id"]),
                         ("own_key", "Mistral", "Voxtral Mini 2602", "voxtral-mini-2602"))
        # Own keys don't need Pro; they need the key and no Privacy mode.
        self.assertEqual(ec.file_engine(files("own:mistral", **MKEY), pro=False)["model_name"], "Voxtral Mini")
        for cfg in (files("own:google"), files("own:google", **MKEY), files("own:mistral", **GKEY),
                    files("own:google", privacy_mode=True, **BOTH), files("own:mistral", privacy_mode=True, **BOTH)):
            with self.subTest(cfg=cfg):
                self.assertEqual(ec.file_engine(cfg, pro=True)["label"], "Auto · on this computer")

    def test_engine_local_and_auto(self):
        self.assertEqual(ec.file_engine({})["label"], "Auto · on this computer")
        self.assertEqual(ec.file_engine(None)["label"], "Auto · on this computer")
        self.assertEqual(ec.file_engine(files("huge-v9"))["label"], "Auto · on this computer")
        self.assertEqual(ec.file_engine(files("large-v3-turbo"))["model_name"], "Whisper Large v3 Turbo")
        # An explicit file_model wins over the saved one.
        cfg = files("cloud", **GKEY)
        self.assertEqual(ec.file_engine(cfg, "small", pro=True)["model_id"], "small")
        self.assertEqual(ec.file_engine(cfg, "own:google")["kind"], "own_key")
        self.assertEqual(ec.file_engine(files("small"), "auto")["label"], "Auto · on this computer")

    def test_engine_matches_the_session(self):
        for value in ("auto", "small", "cloud", "own:google", "own:mistral", "huge-v9"):
            for pro in (False, True):
                for privacy in (False, True):
                    for keys in ({}, BOTH):
                        for provider in ("gemini", "mistral"):
                            cfg = files(value, privacy_mode=privacy, managed_provider=provider, **keys)
                            with self.subTest(cfg=cfg, pro=pro):
                                backend, model = ec.file_session(cfg, pro)
                                r = ec.file_engine(cfg, pro=pro)
                                if backend == "local":
                                    self.assertEqual(r["kind"], "local")
                                    self.assertEqual(r["model_id"], model or "auto")
                                else:
                                    self.assertEqual(r["kind"], "cloud" if backend == "managed" else "own_key")
                                    self.assertEqual(r["model_id"], ec.file_model_id(cfg))
                                    self.assertEqual(r["model_name"], ec.MODEL_NAMES[r["model_id"]])
                                    if backend != "managed":
                                        self.assertEqual(r["model_id"], model)

    def test_runs_on(self):
        app = SimpleNamespace(cfg=files("cloud", managed_provider="mistral"), is_pro=lambda: True,
                              auth=SimpleNamespace(is_authenticated=True))
        self.assertEqual(ec.runs_on(app, "audio_to_text")["model_name"], "Voxtral Mini")
        app.is_pro = lambda: False
        self.assertEqual(ec.runs_on(app, "files")["label"], "Auto · on this computer")
        self.assertEqual(ec.runs_on(files("cloud"), "audio_to_text", pro=True)["kind"], "cloud")
        self.assertEqual(ec.runs_on(files("cloud"), "audio_to_text", pro=True, file_model="base")["model_id"],
                         "base")

    def test_set_file_model(self):
        ok = {"ok": True, "changes": {"file_model": "small"}, "problem": "", "upsell": ""}
        self.assertEqual(ec.set_file_model({}, "small"), ok)
        self.assertEqual(ec.set_file_model({"privacy_mode": True}, " small "), ok)     # on-device is fine
        self.assertEqual(ec.set_file_model({}, "auto")["changes"], {"file_model": "auto"})
        for value in ("huge-v9", "", None, "managed", "own:openai"):
            with self.subTest(value=value):
                r = ec.set_file_model(BOTH, value, pro=True)
                self.assertEqual((r["ok"], r["problem"], r["changes"]), (False, "unknown", {}))

    def test_set_file_model_gusan_cloud(self):
        r = ec.set_file_model({}, "cloud", pro=False)
        self.assertEqual((r["ok"], r["problem"], r["upsell"], r["changes"]),
                         (False, "pro", "Fast cloud transcription", {}))
        self.assertEqual(ec.set_file_model({}, "cloud", pro=True)["changes"], {"file_model": "cloud"})
        for pro in (False, True):
            r = ec.set_file_model({"privacy_mode": True}, "cloud", pro=pro)
            self.assertEqual((r["problem"], r["upsell"]), ("privacy_mode", ""))
        app = SimpleNamespace(cfg={}, is_pro=lambda: True, auth=SimpleNamespace(is_authenticated=True))
        self.assertTrue(ec.set_file_model(app, "cloud")["ok"])
        app.is_pro = lambda: False
        self.assertEqual(ec.set_file_model(app, "cloud")["problem"], "pro")

    def test_set_file_model_own_keys(self):
        for value, key in (("own:google", GKEY), ("own:mistral", MKEY)):
            with self.subTest(value=value):
                self.assertEqual(ec.set_file_model({}, value)["problem"], "needs_key")
                other = MKEY if key is GKEY else GKEY
                self.assertEqual(ec.set_file_model(other, value)["problem"], "needs_key")
                self.assertEqual(ec.set_file_model({k: " " for k in key}, value)["problem"], "needs_key")
                # Free users can use their own key.
                self.assertEqual(ec.set_file_model(key, value, pro=False)["changes"], {"file_model": value})
                r = ec.set_file_model(dict(key, privacy_mode=True), value, pro=True)
                self.assertEqual((r["ok"], r["problem"]), (False, "privacy_mode"))


class TestChoices(unittest.TestCase):
    def test_speech_choice_and_writer_choice(self):
        self.assertEqual(ec.speech_choice({"backend": "managed"}), "cloud")
        self.assertEqual(ec.speech_choice({"backend": "mistral"}), "own_key")
        self.assertEqual(ec.speech_choice({"backend": "managed", "privacy_mode": True}), "local")
        self.assertEqual(ec.writer_choice({"action_model": "managed"}), "cloud")
        self.assertEqual(ec.writer_choice({"action_model": actions.API_OPENAI_ID}), "own_key")
        self.assertEqual(ec.writer_choice({"action_model": "rule_based"}), "local")
        self.assertEqual(ec.local_writer({"action_model": "rule_based"}), QWEN)
        self.assertEqual(ec.local_writer({"action_model": "aibuben_balanced"}), local_llm.QWEN_3B_ID)


class TestSetSpeechSource(unittest.TestCase):
    def test_local(self):
        r = ec.set_speech_source({"whisper_model": "base"}, "local", model="small")
        self.assertEqual(r, {"ok": True, "changes": {"backend": "local", "whisper_model": "small"},
                             "problem": "", "upsell": ""})
        r = ec.set_speech_source({}, "local", model="small", downloaded=lambda m: False)
        self.assertEqual((r["ok"], r["problem"], r["changes"]), (False, "needs_download", {}))
        self.assertEqual(ec.set_speech_source({}, "local", model="huge")["problem"], "unknown")

    def test_gusan_cloud(self):
        r = ec.set_speech_source({}, "cloud", pro=False)
        self.assertEqual((r["problem"], r["upsell"]), ("pro", "Fast cloud transcription"))
        r = ec.set_speech_source({}, "cloud", pro=True)
        self.assertEqual(r["changes"], {"backend": "managed", "managed_provider": "gemini"})
        r = ec.set_speech_source({}, "cloud", provider="mistral", model="x", pro=True)
        self.assertEqual(r["changes"]["mistral_stt_model"], "voxtral-mini-latest")

    def test_privacy(self):
        cfg = {"privacy_mode": True}
        self.assertEqual(ec.set_speech_source(cfg, "cloud", pro=True)["problem"], "privacy_mode")
        r = ec.set_speech_source(cfg, "cloud", pro=True, turn_off_privacy=True)
        self.assertEqual(r["changes"]["privacy_mode"], False)
        self.assertTrue(ec.set_speech_source(cfg, "local")["ok"])

    def test_own_key(self):
        self.assertEqual(ec.set_speech_source({}, "own_key", provider="mistral")["problem"], "needs_key")
        r = ec.set_speech_source({"mistral_api_key": "m"}, "own_key", provider="mistral")
        self.assertEqual(r["changes"], {"backend": "mistral", "mistral_stt_model": "voxtral-mini-latest"})
        r = ec.set_speech_source({"google_api_key": "g"}, "own_key", provider="google")
        self.assertEqual(r["changes"], {"backend": "google"})
        self.assertEqual(ec.set_speech_source({}, "own_key", provider="aws")["problem"], "unknown")


class TestSetWriter(unittest.TestCase):
    def test_cloud(self):
        self.assertEqual(ec.set_writer({}, "cloud", pro=False)["upsell"], "Smart Actions")
        self.assertEqual(ec.set_writer({}, "cloud", pro=True)["changes"], {"action_model": "managed"})
        self.assertEqual(ec.set_writer({"privacy_mode": True}, "cloud", pro=True)["problem"], "privacy_mode")

    def test_local(self):
        self.assertEqual(ec.set_writer({}, "local", downloaded=NOTHING)["problem"], "needs_download")
        r = ec.set_writer({}, "local", model=local_llm.QWEN_7B_ID, downloaded=on_disk(local_llm.QWEN_7B_ID))
        self.assertEqual(r["changes"], {"action_model": local_llm.QWEN_7B_ID})
        # Allowed in privacy mode: it stays on this computer.
        self.assertTrue(ec.set_writer({"privacy_mode": True}, "local", downloaded=on_disk(QWEN))["ok"])

    def test_own_key(self):
        self.assertEqual(ec.set_writer({}, "own_key", provider="anthropic")["problem"], "needs_key")
        r = ec.set_writer({"mistral_api_key": "m", "action_api_base_url": "https://evil"}, "own_key",
                          provider="mistral")
        self.assertEqual(r["changes"], {"action_model": actions.API_MISTRAL_ID, "action_api_provider": "mistral",
                                        "action_api_model": "ministral-14b-2512",
                                        "action_api_base_url": "https://api.mistral.ai/v1"})
        # Gemini never keeps another provider's URL (it would break the calls).
        r = ec.set_writer({"google_api_key": "g", "action_api_base_url": "https://api.openai.com/v1",
                           "action_api_model": "gpt-4o"}, "own_key", provider="gemini")
        self.assertEqual((r["changes"]["action_api_base_url"], r["changes"]["action_api_model"]),
                         ("", "gemini-2.5-flash"))
        r = ec.set_writer({"action_api_key": "c"}, "own_key", provider=actions.API_CEREBRAS_ID,
                          base_url="https://api.openai.com/v1")
        self.assertEqual(r["changes"]["action_api_base_url"], "https://api.cerebras.ai/v1")
        r = ec.set_writer({"action_api_key": "o"}, "own_key", provider="openai",
                          model="my-model", base_url="https://openrouter.ai/api/v1")
        self.assertEqual((r["changes"]["action_api_model"], r["changes"]["action_api_base_url"]),
                         ("my-model", "https://openrouter.ai/api/v1"))
        # A saved custom URL stays; another provider's URL left by an earlier choice doesn't.
        cfg = {"action_api_key": "o", "action_api_base_url": "https://openrouter.ai/api/v1"}
        self.assertEqual(ec.set_writer(cfg, "own_key", provider="openai")["changes"]["action_api_base_url"],
                         "https://openrouter.ai/api/v1")
        cfg = {"action_api_key": "o", "action_api_base_url": "https://api.mistral.ai/v1"}
        self.assertEqual(ec.set_writer(cfg, "own_key", provider="openai")["changes"]["action_api_base_url"], "")

    def test_writer_model_for_keeps_custom_ids(self):
        self.assertEqual(ec.writer_model_for("openai", {"action_api_model": "deepseek-chat"}), "deepseek-chat")
        self.assertEqual(ec.writer_model_for("gemini", {"action_api_model": "gemini-1.5-flash"}),
                         "gemini-2.5-flash")
        self.assertEqual(ec.writer_model_for("anthropic", {"action_api_model": "gpt-5.5"}), "claude-sonnet-4-6")
        self.assertEqual([m[1] for m in ec.writer_models("cerebras")], ["Qwen 3.8 27B", "gpt-oss 120B"])

    def test_catalogs_match_the_backend(self):
        for pid in ec.WRITER_PROVIDERS:
            self.assertEqual(actions.ACTION_MODELS[pid]["kind"], "cloud")
        self.assertEqual(set(ec.LOCAL_WRITERS), set(local_llm.MODEL_CATALOG))


class TestSmartActionsSpeech(unittest.TestCase):
    """D8: Smart Actions' own speech model (cfg smart_actions_speech_model)."""

    def test_constants(self):
        self.assertEqual((ec.SMART_SPEECH_KEY, ec.SMART_SPEECH_SAME, ec.SMART_SPEECH_CLOUD),
                         ("smart_actions_speech_model", "", "cloud"))
        self.assertIn("smart_actions_speech", ec.FEATURES)
        self.assertIn("smart_actions", ec.FEATURES)          # the writer stays

    def test_session_matrix(self):
        key = ec.SMART_SPEECH_KEY
        for pro in (False, True):
            for privacy in (False, True):
                with self.subTest(pro=pro, privacy=privacy):
                    base = {"privacy_mode": privacy, "backend": "managed", "whisper_model": "base"}
                    self.assertEqual(ec.smart_speech_session(dict(base, **{key: ""}), pro), (None, None))
                    self.assertEqual(ec.smart_speech_session(base, pro), (None, None))     # key missing
                    self.assertEqual(ec.smart_speech_session(dict(base, **{key: "huge"}), pro), (None, None))
                    self.assertEqual(ec.smart_speech_session(dict(base, **{key: "small"}), pro),
                                     ("local", "small"))
                    want = ("managed", None) if (pro and not privacy) else (None, None)
                    self.assertEqual(ec.smart_speech_session(dict(base, **{key: "cloud"}), pro), want)
        self.assertEqual(ec.smart_speech_session(None, True), (None, None))
        for mid in ec.SPEECH_MODELS:
            self.assertEqual(ec.smart_speech_session({key: mid}, False), ("local", mid))

    def test_runs_on_same_as_dictation(self):
        cfg = {"backend": "local", "whisper_model": "small"}
        r = ec.runs_on(cfg, "smart_actions_speech")
        d = ec.runs_on(cfg, "dictation")
        self.assertEqual(r["feature"], "smart_actions_speech")
        self.assertEqual(dict(r, feature="dictation"), d)
        self.assertEqual(set(r), set(d))                     # the same dict shape
        cloud = {"backend": "managed", "whisper_model": "base"}
        self.assertEqual(ec.runs_on(cloud, "smart_speech", pro=True)["kind"], "cloud")
        self.assertEqual(ec.runs_on(cloud, "smart_actions_speech", pro=False)["kind"], "local")

    def test_runs_on_own_model(self):
        cfg = {"backend": "managed", "whisper_model": "base", ec.SMART_SPEECH_KEY: "large-v3-turbo"}
        r = ec.runs_on(cfg, "smart_actions_speech", pro=True)
        self.assertEqual((r["kind"], r["model_id"], r["label"]),
                         ("local", "large-v3-turbo", "Most accurate · Whisper Large v3 Turbo · on this computer"))
        self.assertTrue(r["ready"])
        r = ec.runs_on(cfg, "smart_actions_speech", downloaded=lambda m: False)
        self.assertEqual((r["ready"], r["problem"]), (False, "needs_download"))
        # The writer feature is unchanged by the speech pick.
        self.assertEqual(ec.runs_on(dict(cfg, action_model="rule_based"), "smart_actions",
                                    downloaded=NOTHING)["kind"], "basic")

    def test_runs_on_gusan_cloud(self):
        cfg = {"backend": "local", "whisper_model": "small", ec.SMART_SPEECH_KEY: "cloud",
               "managed_provider": "mistral"}
        r = ec.runs_on(cfg, "smart_actions_speech", pro=True)
        self.assertEqual((r["kind"], r["model_name"], r["pro"]), ("cloud", "Voxtral Mini", True))
        # Not Pro, or Privacy mode: it transcribes like Dictation.
        self.assertEqual(ec.runs_on(cfg, "smart_actions_speech", pro=False)["model_id"], "small")
        r = ec.runs_on(dict(cfg, privacy_mode=True), "smart_actions_speech", pro=True)
        self.assertEqual((r["kind"], r["model_id"]), ("local", "small"))

    def test_choices(self):
        choices = ec.smart_speech_choices()
        self.assertEqual(choices[0], ("", "Same as Dictation", "Same as Dictation", False))
        self.assertEqual(choices[-1], ("cloud", "Gusan Cloud", "Gusan Cloud · Gemini 2.5 Flash", True))
        middle = choices[1:-1]
        self.assertEqual([c[0] for c in middle], list(ec.SPEECH_MODELS))
        self.assertIn(("small", "Balanced", "Balanced · Whisper Small", False), middle)
        self.assertTrue(all(not c[3] for c in middle))
        self.assertEqual(ec.smart_speech_choices({"managed_provider": "mistral"})[-1][2],
                         "Gusan Cloud · Voxtral Mini")

    def test_set_smart_speech(self):
        key = ec.SMART_SPEECH_KEY
        self.assertEqual(ec.set_smart_speech({}, "small"),
                         {"ok": True, "changes": {key: "small"}, "problem": "", "upsell": ""})
        self.assertEqual(ec.set_smart_speech({}, "")["changes"], {key: ""})
        r = ec.set_smart_speech({}, "cloud", pro=False)
        self.assertEqual((r["ok"], r["problem"], r["upsell"]), (False, "pro", "Fast cloud transcription"))
        self.assertEqual(ec.set_smart_speech({}, "cloud", pro=True)["changes"], {key: "cloud"})
        self.assertEqual(ec.set_smart_speech({"privacy_mode": True}, "cloud", pro=True)["problem"],
                         "privacy_mode")
        self.assertEqual(ec.set_smart_speech({}, "huge")["problem"], "unknown")
        self.assertEqual(ec.set_smart_speech({}, "small", downloaded=lambda m: False)["problem"],
                         "needs_download")


def _stubbed_main():
    """main under test_core's stand-ins, or None (then the test skips)."""
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None or type(qtcore).__name__ != "StubModule":
        return None
    import main
    return main


class TestParityWithMain(unittest.TestCase):
    def setUp(self):
        self.main = _stubbed_main()
        if self.main is None:
            self.skipTest("needs test_core's PySide6 stand-ins (run the whole tests/ folder)")

    def test_speech_catalog_and_mistral_models(self):
        main = self.main
        self.assertEqual(set(ec.SPEECH_MODELS), set(main.MODELS))
        for mid, (_plain, _name, size, _mem) in ec.SPEECH_MODELS.items():
            self.assertEqual(size, main.MODELS[mid]["size"])
        self.assertEqual(ec.MISTRAL_STT_DEFAULT, main.MISTRAL_STT_DEFAULT)
        self.assertEqual(ec.MISTRAL_STT_MODELS, main.MISTRAL_STT_MODELS)

    def test_resolve_action_engine_matches_the_controller(self):
        main = self.main
        for model in ("rule_based", "managed", QWEN, actions.API_GEMINI_ID, actions.API_ANTHROPIC_ID):
            for keys in ({}, {"action_api_key": "a"}, {"google_api_key": "g"}):
                for pro in (False, True):
                    cfg = dict(keys, action_model=model)
                    me = SimpleNamespace(cfg=cfg, is_pro=lambda p=pro: p,
                                         auth=SimpleNamespace(get_access_token=lambda: "tok"))
                    with self.subTest(model=model, keys=keys, pro=pro):
                        want, config = main.AppController._resolve_action_engine(me)
                        got, token = ec.resolve_action_engine(cfg, pro, True)
                        self.assertEqual(got, want)
                        self.assertEqual(token, "_managed_token" in config)


if __name__ == "__main__":
    unittest.main()
