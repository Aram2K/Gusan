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
        # Gusan Cloud chooses the engine itself: the row names the PROFILE it asks for ("Fast"), and
        # whatever really answered is named from the server's answer (see test_live_speech)
        self.assertEqual(r["label"], "Gusan Cloud · Fast")
        self.assertEqual(r["model_id"], "fast")
        self.assertTrue(r["pro"])
        self.assertEqual(ec.runs_on(dict(cfg, managed_provider="mistral"), "dictation", pro=True)["model_name"],
                         "Fast")                     # (a 1.9 "mistral" provider is the same profile now)
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
        # Meeting notes are written by Mistral Small (smart-action routes by mode; Claude Sonnet 5.5 comes first once its key has a workspace).
        r = ec.runs_on({"action_model": "rule_based"}, "meeting_notes", pro=True, downloaded=NOTHING)
        self.assertEqual(r["label"], "Gusan Cloud · Mistral Small")
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
                        cloud = ("managed", "best") if (pro and not privacy) else ("local", None)
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
        # Audio to text asks Gusan Cloud for its BEST profile, whatever the old provider setting says
        self.assertEqual(ec.file_session(files("cloud", managed_provider="mistral"), True), ("managed", "best"))
        self.assertEqual(ec.file_session(files("cloud", managed_provider=" Mistral "), True),
                         ("managed", "best"))
        self.assertEqual(ec.file_session(files("cloud", managed_provider="aws"), True), ("managed", "best"))
        cfg = files("own:google", google_stt_model="gemini-2.5-pro", **GKEY)
        self.assertEqual(ec.file_session(cfg, False), ("google", "gemini-2.5-pro"))
        cfg = files("own:mistral", mistral_stt_model="voxtral-mini-2602", **MKEY)
        self.assertEqual(ec.file_session(cfg, False), ("mistral", "voxtral-mini-2602"))
        cfg = files("own:mistral", mistral_stt_model="voxtral-small-latest", **MKEY)
        self.assertEqual(ec.file_session(cfg, False), ("mistral", "voxtral-mini-latest"))    # healed like main

    def test_model_id(self):
        # Gusan Cloud: the profile a file asks for (cloud_files then reports the model that really ran)
        self.assertEqual(ec.file_model_id({}, "cloud"), "best")
        self.assertEqual(ec.file_model_id({"managed_provider": "mistral"}, "cloud"), "best")
        # ...whatever the own-key models say.
        self.assertEqual(ec.file_model_id({"managed_provider": "mistral", "mistral_stt_model": "voxtral-mini-2507"},
                                          "cloud"), "best")
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
        self.assertEqual(ec.file_model_id(files("cloud", managed_provider="mistral")), "best")
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
        # every model the server can answer with has a real name (X-Gusan-Model is shown as it says)
        for mid in ("scribe_v2", "voxtral-mini-latest", "gpt-transcribe", "gpt-4o-transcribe",
                    "gpt-4o-mini-transcribe", "whisper-1"):
            self.assertIn(mid, ec.MODEL_NAMES)
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
        # a client asks for a PROFILE: the proxy knows both words, and no Gemini id is ever sent
        for word in ec.CLOUD_PROFILES:
            self.assertIn('"%s"' % word, source)
        for mid in ("scribe_v2", "voxtral-mini-latest", "gpt-transcribe", "gpt-4o-transcribe"):
            self.assertIn('"%s"' % mid, source)             # the models it answers with are all named here
            self.assertIn(mid, ec.MODEL_NAMES)

    def test_live_assistant_speech_is_one_more_feature_with_the_same_keys_everywhere(self):
        self.assertIn("live_assistant_speech", ec.FEATURES)
        self.assertIn("live_assistant", ec.FEATURES)          # the answer writer stays
        self.assertEqual(ec.LIVE_SPEECH_KEY, "live_assist_speech_model")
        import settings_apply
        self.assertEqual(settings_apply.LIVE_SPEECH, ec.LIVE_SPEECH_KEY)
        self.assertEqual((ec.LIVE_SPEECH_AUTO, ec.LIVE_SPEECH_CLOUD, ec.LIVE_SPEECH_BEST),
                         ("", "cloud", "cloud:best"))
        self.assertEqual(ec.runs_on({}, "live_speech", pro=False)["feature"], "live_assistant_speech")

    def test_choices(self):
        choices = ec.file_choices()
        self.assertEqual(choices[0], ("auto", "Automatic", "Automatic · Local", False))
        self.assertEqual([c[0] for c in choices[1:-1]], list(ec.SPEECH_MODELS))
        # a REAL model name and where it runs - never a plain word, never "Gusan Cloud" as a model
        self.assertIn(("small", "Whisper Small", "Whisper Small · Local", False), choices)
        self.assertTrue(all(not c[3] for c in choices[:-1]))
        # Gusan Cloud is a heading and a PROFILE: its server chooses the model
        self.assertEqual(choices[-1], ("cloud", "Best accuracy", "Best accuracy · Cloud · Pro", True))
        self.assertEqual([c[1] for c in choices],
                         ["Automatic", "Whisper Tiny", "Whisper Base", "Whisper Small", "Whisper Medium",
                          "Whisper Large v3 Turbo", "Whisper Large v3", "Best accuracy"])
        self.assertEqual(ec.file_choices({"managed_provider": "mistral"})[-1][2],
                         "Best accuracy · Cloud · Pro")

    def test_rows_group_the_models_and_wear_tags(self):
        rows = ec.file_rows(BOTH)
        self.assertEqual([r["group"] for r in rows],
                         [""] + [ec.GROUP_THIS_COMPUTER] * len(ec.SPEECH_MODELS)
                         + [ec.GROUP_GUSAN_CLOUD, ec.GROUP_OWN_KEY, ec.GROUP_OWN_KEY])
        by = {r["value"]: r for r in rows}
        self.assertEqual(by["small"]["tags"], ("local",))
        self.assertEqual(by["small"]["note"], "Balanced")
        self.assertEqual(by["cloud"]["tags"], ("cloud", "pro"))
        self.assertTrue(by["cloud"]["pro"])
        self.assertEqual(by["own:google"]["tags"], ("cloud",))
        self.assertEqual(by["own:google"]["title"], "Gemini 2.5 Flash · your Google key")
        self.assertEqual(by["own:mistral"]["title"], "Voxtral Mini · your Mistral key")
        self.assertEqual(by["auto"]["tags"], ("local",))
        self.assertEqual(by["auto"]["kind"], "auto")

    def test_automatic_never_says_local_for_a_file_that_goes_to_the_cloud(self):
        # An Armenian file under Automatic goes to Gusan Cloud for a signed-in Pro person (file_session's
        # order): the row says CLOUD and PRO and names the profile. Every other case stays LOCAL.
        def auto(cfg, pro, language, signed_in=None):
            return ec.file_rows(cfg, pro, language, signed_in)[0]
        with patch.object(ec, "hy_usable", return_value=False):
            hy = auto({}, True, "hy")
            self.assertEqual((hy["value"], hy["kind"], hy["tags"], hy["title"], hy["note"]),
                             ("auto", "auto", ("cloud", "pro"), "Automatic · Best accuracy", "for Armenian"))
            self.assertEqual(auto({}, True, "hye")["title"], "Automatic · Best accuracy")      # any spelling
            self.assertEqual(auto({"mix_languages": ["en", "hy"]}, True, "multi")["note"], "for Armenian")
            for cfg, pro, language, signed_in in (
                    ({}, True, "fr", None),                                  # Whisper reads French well
                    ({}, True, "auto", None),                                # nothing known to be poor
                    ({}, True, None, None),                                  # no language asked
                    ({}, False, "hy", None),                                 # no Pro plan
                    ({}, True, "hy", False),                                 # signed out
                    ({"privacy_mode": True}, True, "hy", None)):             # Privacy mode: nothing leaves
                row = auto(cfg, pro, language, signed_in)
                self.assertEqual((row["tags"], row["title"], row["note"]),
                                 (("local",), "Automatic", "Picked for this computer"), (cfg, pro, language))
        self.assertEqual(ec.file_choices({})[0], ("auto", "Automatic", "Automatic · Local", False))

    def test_the_pro_chip_is_one_switch(self):
        # Rows that need a Pro plan wear PRO for everyone (it says why the row is there); the flag
        # is the one place to make that "only for people without the plan".
        for user_pro in (False, True):
            rows = {r["value"]: r for r in ec.file_rows({}, user_pro)}
            self.assertEqual(rows["cloud"]["tags"], ("cloud", "pro"))
        with patch.object(ec, "SHOW_PRO_TO_PRO", False):
            self.assertEqual({r["value"]: r for r in ec.file_rows({}, True)}["cloud"]["tags"], ("cloud",))
            self.assertEqual({r["value"]: r for r in ec.file_rows({}, False)}["cloud"]["tags"], ("cloud", "pro"))
            self.assertTrue({r["value"]: r for r in ec.file_rows({}, True)}["cloud"]["pro"])      # still a Pro row

    def test_own_key_choices_only_with_the_key(self):
        def own(cfg):
            return [c for c in ec.file_choices(cfg) if c[0].startswith("own:")]
        self.assertEqual(own({}), [])
        self.assertEqual(own({"google_api_key": "  ", "mistral_api_key": ""}), [])
        google = ("own:google", "Gemini 2.5 Flash · your Google key",
                  "Gemini 2.5 Flash · your Google key · Cloud", False)
        mistral = ("own:mistral", "Voxtral Mini · your Mistral key",
                   "Voxtral Mini · your Mistral key · Cloud", False)
        self.assertEqual(own(GKEY), [google])
        self.assertEqual(own(MKEY), [mistral])
        self.assertEqual(own(BOTH), [google, mistral])
        self.assertEqual(ec.file_choices(BOTH)[-3][0], "cloud")             # Gusan Cloud, then own keys
        # The row names the model that will really run (same names as Dictation's).
        cfg = dict(BOTH, google_stt_model="gemini-2.5-pro", mistral_stt_model="voxtral-mini-2602")
        self.assertEqual([c[2] for c in own(cfg)],
                         ["Gemini 2.5 Pro · your Google key · Cloud",
                          "Voxtral Mini 2602 · your Mistral key · Cloud"])
        self.assertEqual(ec.runs_on(dict(GKEY, backend="google"), "dictation")["tagged"],
                         "Gemini 2.5 Flash · your Google key · Cloud")
        # Privacy mode keeps them listed (the page greys them out), with the real names.
        self.assertEqual(own(dict(BOTH, privacy_mode=True)), [google, mistral])
        self.assertEqual(ec.file_choices(dict(BOTH, privacy_mode=True))[-3][2],
                         "Best accuracy · Cloud · Pro")

    def test_engine_gusan_cloud(self):
        cfg = files("cloud", backend="local", whisper_model="small")
        r = ec.file_engine(cfg, pro=True)
        self.assertEqual((r["feature"], r["kind"], r["label"], r["model_name"], r["model_id"], r["pro"]),
                         ("audio_to_text", "cloud", "Gusan Cloud · Best accuracy", "Best accuracy",
                          "best", True))
        r = ec.file_engine(dict(cfg, managed_provider="mistral"), pro=True)
        self.assertEqual((r["model_name"], r["model_id"]), ("Best accuracy", "best"))
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
        self.assertEqual(ec.runs_on(app, "audio_to_text")["model_name"], "Best accuracy")
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
        self.assertEqual((r["kind"], r["model_name"], r["pro"]), ("cloud", "Fast", True))
        # Not Pro, or Privacy mode: it transcribes like Dictation.
        self.assertEqual(ec.runs_on(cfg, "smart_actions_speech", pro=False)["model_id"], "small")
        r = ec.runs_on(dict(cfg, privacy_mode=True), "smart_actions_speech", pro=True)
        self.assertEqual((r["kind"], r["model_id"]), ("local", "small"))

    def test_choices(self):
        choices = ec.smart_speech_choices()
        # "Same as Dictation" names what Dictation runs now (here: no cfg = Whisper Base on this computer)
        self.assertEqual(choices[0], ("", "Same as Dictation · Whisper Base",
                                      "Same as Dictation · Whisper Base · Local", False))
        self.assertEqual(choices[-1], ("cloud", "Fast", "Fast · Cloud · Pro", True))
        middle = choices[1:-1]
        self.assertEqual([c[0] for c in middle], list(ec.SPEECH_MODELS))
        self.assertIn(("small", "Whisper Small", "Whisper Small · Local", False), middle)
        self.assertTrue(all(not c[3] for c in middle))
        self.assertEqual(ec.smart_speech_choices({"managed_provider": "mistral"})[-1][2],
                         "Fast · Cloud · Pro")

    def test_same_as_dictation_wears_the_tags_of_what_dictation_runs(self):
        rows = ec.smart_speech_rows({"backend": "managed", "managed_provider": "gemini"}, True)
        self.assertEqual(rows[0]["title"], "Same as Dictation · Fast")
        self.assertEqual(rows[0]["tags"], ("cloud", "pro"))
        self.assertFalse(rows[0]["pro"])                       # picking it never needs a plan
        rows = ec.smart_speech_rows({"backend": "managed", "privacy_mode": True}, True)
        self.assertEqual(rows[0]["tags"], ("local",))          # Privacy mode: Dictation is local

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


class TestArmenianModelHints(unittest.TestCase):
    """The Armenian model reads Armenian only: a place that has it picked while the language is not
    Armenian says so (the recorder reads such speech with Whisper) - and says nothing when it reads."""

    HY = "hy-fastconformer"

    def listed(self, value=True, ready=True):
        import speech_hy
        for name, v in (("listed", value), ("ready", ready), ("runtime_ok", True)):
            p = patch.object(speech_hy, name, lambda *a, _v=v, **k: _v)
            p.start()
            self.addCleanup(p.stop)

    def test_dictation_with_the_model_picked_says_what_reads_what(self):
        self.listed()
        cfg = {"backend": "local", "whisper_model": self.HY}
        self.assertEqual(ec.dictation_note(dict(cfg, language="hy"), False), "")             # it reads this
        self.assertEqual(ec.dictation_note(dict(cfg, language="auto"), False), ec.HY_NOTE_AUTO)
        self.assertEqual(ec.dictation_note(dict(cfg, language="en"), False),
                         "This model reads Armenian only. English is read by Whisper instead.")
        self.assertEqual(ec.dictation_note(dict(cfg, language="multi", mix_languages=["hy", "en"]), False),
                         ec.HY_NOTE_MIX)
        self.assertEqual(ec.dictation_note(dict(cfg, language="multi", mix_languages=["hy"]), False),
                         ec.HY_NOTE_MIX)            # (fewer than two picked: "any language" - not Armenian only)
        # a Whisper pick: nothing about the Armenian model
        self.assertEqual(ec.dictation_note(dict(cfg, whisper_model="small", language="en"), False), "")

    def test_a_pick_that_cannot_be_used_says_so(self):
        self.listed(value=False, ready=False)
        cfg = {"backend": "local", "whisper_model": self.HY, "language": "hy"}
        self.assertEqual(ec.dictation_note(cfg, False), ec.HY_NOTE_UNAVAILABLE)

    def test_the_cloud_and_own_keys_say_nothing_about_it(self):
        self.listed()
        cfg = {"backend": "managed", "whisper_model": self.HY, "language": "en"}
        self.assertEqual(ec.dictation_note(cfg, True, True), "")

    def test_audio_to_text_and_the_live_assistant_say_it_too(self):
        self.listed()
        self.assertEqual(ec.file_note({"file_model": self.HY, "file_language": "auto"}, False),
                         ec.HY_NOTE_AUTO)
        self.assertEqual(ec.file_note({"file_model": self.HY, "file_language": "fr"}, False),
                         "This model reads Armenian only. French is read by Whisper instead.")
        self.assertEqual(ec.file_note({"file_model": self.HY, "file_language": "hy"}, False), "")
        live = {"live_assist_speech_model": self.HY}
        self.assertEqual(ec.live_speech_note(dict(live, live_assist_language="de"), False, None, False),
                         "This model reads Armenian only. German is read by Whisper instead.")
        self.assertEqual(ec.live_speech_note(dict(live, live_assist_language="hy"), False, None, False), "")
        # Automatic and a Whisper pick keep their own words
        self.assertEqual(ec.live_speech_note({"live_assist_speech_model": "small", "live_assist_language": "de"},
                                             False, None, False), "")


if __name__ == "__main__":
    unittest.main()
