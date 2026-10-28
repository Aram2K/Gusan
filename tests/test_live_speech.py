"""Gusan 2.0.1: the Live Assistant chooses what transcribes a call (cfg
live_assist_speech_model), and Armenian stops going through local Whisper.

* the choice rules (engine_choice.live_speech_*): Automatic, a Whisper model, Gusan Cloud (two
  PROFILES, Fast and Best accuracy: the server chooses the model and says which one ran), your own
  Google key; Pro first, Privacy mode never; the data-driven "poor for local Whisper" table and its
  one plain hint; THE ORDER for Armenian (cloud, then the Armenian model on this computer, then
  Whisper with a hint);
* the recorder's take: speech=(backend, model, cloud), the request it sends, what it says RAN,
  where each piece sits in the recording;
* Meetings and dictation keep Dictation's model (nothing here changes their route);
* the transcribe-proxy source (and, where Node can run it, its real behaviour).

Pure logic under test_core's stand-ins (no Qt, no audio, nothing loaded, no network)."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import cloud_speech as cs
import engine_choice as ec
import main
import speech_hy
import speech_langs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(ROOT, "supabase", "functions", "transcribe-proxy", "index.ts")

HY = {"live_assist_language": "hy", "language": "auto", "backend": "local", "whisper_model": "base"}
GKEY = {"google_api_key": "AIza-test"}


def cfg(**kw):
    return dict(HY, **kw)


# The Armenian model is on the founder's disk or not: no test here may depend on it. Everything below
# runs as if it were NOT on this computer and not listed; the tests of the order patch it in.
_HY_OFF = []


def setUpModule():
    for name in ("ready", "listed"):
        p = mock.patch.object(speech_hy, name, return_value=False)
        p.start()
        _HY_OFF.append(p)


def tearDownModule():
    while _HY_OFF:
        _HY_OFF.pop().stop()


def hy(ready=False, listed=True):
    """The Armenian model as these tests need it: listed (downloadable), and on disk or not."""
    stack = mock.patch.multiple(speech_hy, ready=lambda *a, **k: ready, listed=lambda *a, **k: listed,
                                runtime_ok=lambda: True)
    return stack


# ── the data: which languages local Whisper is poor at ───────────────────────────

class TestPoorLanguages(unittest.TestCase):
    def test_armenian_is_the_first_entry(self):
        self.assertEqual(speech_langs.local_whisper_poor("hy"), "Armenian")
        for code in ("en", "fr", "de", "ru", "es", "ar", "auto", "", None, "multi"):
            self.assertEqual(speech_langs.local_whisper_poor(code), "", code)

    def test_mixed_languages_count_when_one_of_the_mix_is_poor(self):
        self.assertEqual(speech_langs.local_whisper_poor("multi", {"mix_languages": ["en", "hy"]}), "Armenian")
        self.assertEqual(speech_langs.local_whisper_poor("multi", {"mix_languages": ["en", "fr"]}), "")
        self.assertEqual(speech_langs.local_whisper_poor("multi", {"mix_languages": ["hy"]}), "")  # < 2 ticked

    def test_adding_a_language_is_one_line_of_data(self):
        with mock.patch.dict(speech_langs.LOCAL_WHISPER_POOR, {"xx": "Xish"}):
            self.assertEqual(speech_langs.local_whisper_poor("xx"), "Xish")
            c = {"live_assist_language": "xx", "backend": "local"}
            self.assertEqual(ec.live_speech_session(c, True), ("managed", None, "fast"))
            self.assertIn("Xish", ec.runs_on(c, "live_speech", pro=False)["note"])
        self.assertEqual(ec.live_speech_session({"live_assist_language": "xx"}, True), (None, None, None))


# ── the choice rules ─────────────────────────────────────────────────────────────

class TestValues(unittest.TestCase):
    def test_the_value_space(self):
        self.assertEqual(ec.parse_live_speech(""), ("auto", ""))
        self.assertEqual(ec.parse_live_speech(None), ("auto", ""))
        self.assertEqual(ec.parse_live_speech("small"), ("local", "small"))
        self.assertEqual(ec.parse_live_speech("cloud"), ("cloud", "fast"))             # a profile, not a model
        self.assertEqual(ec.parse_live_speech("cloud:best"), ("cloud_best", "best"))
        self.assertEqual(ec.parse_live_speech("hy-fastconformer"), ("local", "hy-fastconformer"))
        self.assertEqual(ec.parse_live_speech("own:google:gemini-3-flash-preview"),
                         ("own_google", "gemini-3-flash-preview"))
        self.assertEqual(ec.parse_live_speech("own:google"), ("own_google", "gemini-2.5-flash"))
        self.assertEqual(ec.parse_live_speech("own:google", {"google_stt_model": "gemini-2.5-pro"}),
                         ("own_google", "gemini-2.5-pro"))
        for bad in ("own:google:gemini-1.0", "own:mistral", "whisper-9000", "managed"):
            self.assertEqual(ec.parse_live_speech(bad), ("unknown", ""), bad)

    def test_one_list_of_models(self):
        self.assertEqual(ec.GEMINI_SPEECH_MODELS,
                         ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash-preview"))
        for mid in ec.GEMINI_SPEECH_MODELS:                       # the Gemini ids are for YOUR OWN key only
            self.assertIn(mid, ec.MODEL_NAMES)
        self.assertEqual(ec.CLOUD_PROFILES, ("fast", "best"))     # what Gusan Cloud is asked for
        for word in ec.CLOUD_PROFILES:
            self.assertIn(word, ec.MODEL_NAMES)
        self.assertEqual(ec.CLOUD_SPEECH_IDS, {"gemini": "fast", "mistral": "fast"})
        self.assertFalse(hasattr(ec, "CLOUD_GEMINI_MODELS"))      # no Gemini id goes to Gusan Cloud any more
        self.assertFalse(hasattr(ec, "CLOUD_BEST_MODEL"))
        # every old value still means something
        for old, want in (("best", "best"), ("gemini-2.5-pro", "best"), ("fast", "fast"), ("", "fast"),
                          (None, "fast"), ("gemini-2.5-flash", "fast"), ("gemini", "fast"), ("mistral", "fast")):
            self.assertEqual(ec.cloud_profile(old), want, old)
        self.assertEqual(ec.cloud_profile("x", default="best"), "best")

    def test_saved_value_and_class(self):
        self.assertEqual(ec.live_speech_value({"live_assist_speech_model": "own:google"}),
                         "own:google:gemini-2.5-flash")
        self.assertEqual(ec.live_speech_value({"live_assist_speech_model": "junk"}), "")
        self.assertEqual(ec.live_speech_value({}), "")
        classes = {"": "auto", "small": "local", "cloud": "cloud", "cloud:best": "cloud_best",
                   "own:google:gemini-2.5-pro": "own_key", "junk": "unknown"}
        for value, want in classes.items():
            self.assertEqual(ec.live_speech_class(value), want, value)
        for value in classes:                         # a class, never a model name or any text
            self.assertNotIn("gemini", ec.live_speech_class(value))


class TestAnOldConfig(unittest.TestCase):
    """A config.json written before 2.0.1 has no live_assist_speech_model."""

    def test_the_default_is_automatic_and_an_old_config_gets_it(self):
        self.assertEqual(main.DEFAULT["live_assist_speech_model"], "")
        old = {"backend": "local", "language": "hy", "whisper_model": "small", "live_assist_language": "hy"}
        self.assertNotIn("live_assist_speech_model", old)
        with mock.patch.object(main.storage, "read_json", return_value=dict(old)):
            loaded = main.load_config()
        self.assertEqual(loaded["live_assist_speech_model"], "")
        self.assertEqual(ec.live_speech_value(loaded), "")
        self.assertEqual(ec.live_speech_class(loaded["live_assist_speech_model"]), "auto")
        # ... and behaves as Automatic: Armenian goes to the cloud for a Pro person, nothing else moves.
        self.assertEqual(ec.live_speech_session(loaded, True, "hy"), ("managed", None, "fast"))
        self.assertEqual(ec.live_speech_session(loaded, True, "en"), (None, None, None))
        self.assertEqual(ec.live_speech_session(loaded, False, "hy"), (None, None, None))
        # a value some other build wrote that this one doesn't know is Automatic, never an error
        self.assertEqual(ec.live_speech_session(dict(loaded, live_assist_speech_model="cloud:ultra"),
                                                True, "en"), (None, None, None))


class TestSession(unittest.TestCase):
    """live_speech_session: the recorder's speech=(backend, model, cloud)."""

    def test_automatic_is_dictations_for_every_language_but_armenian(self):
        for lang in ("en", "fr", "auto", "multi", "ru"):
            for pro in (True, False):
                c = cfg(live_assist_language=lang)
                self.assertEqual(ec.live_speech_session(c, pro), (None, None, None), (lang, pro))

    def test_automatic_armenian_on_local_whisper_goes_to_gusan_cloud_when_allowed(self):
        self.assertEqual(ec.live_speech_session(cfg(), True), ("managed", None, "fast"))
        # ...the dictation language counts when Call language is "Same as Dictation"
        c = cfg(live_assist_language="", language="hy")
        self.assertEqual(ec.live_speech_session(c, True), ("managed", None, "fast"))
        # ...and the session's own language (a Meetings-style start passes it) wins
        self.assertEqual(ec.live_speech_session(cfg(live_assist_language="en"), True, language="hy"),
                         ("managed", None, "fast"))

    def test_the_order_for_armenian_is_the_cloud_then_the_armenian_model_then_whisper(self):
        HYID = "hy-fastconformer"
        with hy(ready=True):
            # 1. Gusan Cloud when it is allowed (signed-in Pro, no Privacy mode)
            self.assertEqual(ec.live_speech_session(cfg(), True, signed_in=True), ("managed", None, "fast"))
            # 2. else the Armenian model that is on this computer - a Free person, a guest, Privacy mode
            self.assertEqual(ec.live_speech_session(cfg(), False), ("local", HYID, None))
            self.assertEqual(ec.live_speech_session(cfg(), True, signed_in=False), ("local", HYID, None))
            self.assertEqual(ec.live_speech_session(cfg(privacy_mode=True), True), ("local", HYID, None))
            # ...only for Armenian (or a mix of nothing else): any other language is Dictation's, as ever
            self.assertEqual(ec.live_speech_session(cfg(live_assist_language="fr"), False), (None, None, None))
            self.assertEqual(ec.live_speech_session(cfg(live_assist_language="auto"), False), (None, None, None))
            mixed = cfg(live_assist_language="multi", mix_languages=["en", "hy"])
            self.assertEqual(ec.live_speech_session(mixed, False), (None, None, None))
            # ...and never over what Dictation already runs in the cloud, or an explicit pick
            self.assertEqual(ec.live_speech_session(cfg(backend="google", **GKEY), False), (None, None, None))
            self.assertEqual(ec.live_speech_session(cfg(live_assist_speech_model="small"), False),
                             ("local", "small", None))
        with hy(ready=False):
            # 3. else local Whisper, with the hint (the Armenian model is downloadable, not here yet)
            self.assertEqual(ec.live_speech_session(cfg(), False), (None, None, None))
            self.assertEqual(ec.live_speech_session(cfg(privacy_mode=True), True), (None, None, None))
            self.assertEqual(ec.live_speech_session(cfg(), True, signed_in=True), ("managed", None, "fast"))

    def test_the_engine_line_follows_the_order(self):
        with hy(ready=True):
            r = ec.runs_on(cfg(), "live_speech", pro=False)
            self.assertEqual((r["kind"], r["model_id"], r["auto"], r["reason"]),
                             ("local", "hy-fastconformer", True, "language"))
            self.assertEqual(r["note"], "")                     # the model that reads it: nothing to warn about
            row = ec.live_speech_rows(cfg(), False)[0]
            self.assertEqual((row["value"], row["note"], row["tags"]), ("", "for Armenian", ("local",)))
            self.assertIn("NVIDIA FastConformer Armenian", row["title"])

    def test_automatic_armenian_stays_local_when_the_cloud_is_not_allowed(self):
        self.assertEqual(ec.live_speech_session(cfg(), False), (None, None, None))             # Free
        self.assertEqual(ec.live_speech_session(cfg(), True, signed_in=False), (None, None, None))
        self.assertEqual(ec.live_speech_session(cfg(privacy_mode=True), True), (None, None, None))

    def test_automatic_leaves_dictations_own_cloud_engines_alone(self):
        for conf in (cfg(backend="managed"), cfg(backend="google", **GKEY),
                     cfg(backend="mistral", mistral_api_key="k", live_assist_language="fr")):
            self.assertEqual(ec.live_speech_session(conf, True), (None, None, None), conf["backend"])
        # ...except where that engine cannot do the call's language at all: Voxtral does not do
        # Armenian (every piece would be refused), so Automatic moves the call to Gusan Cloud
        self.assertEqual(ec.live_speech_session(cfg(backend="mistral", mistral_api_key="k"), True),
                         ("managed", None, "fast"))

    def test_a_whisper_model_is_that_model(self):
        for pro in (True, False):
            self.assertEqual(ec.live_speech_session(cfg(live_assist_speech_model="small"), pro),
                             ("local", "small", None))
        # explicit, so Armenian does not move it either
        self.assertEqual(ec.live_speech_session(cfg(live_assist_speech_model="large-v3"), True),
                         ("local", "large-v3", None))

    def test_gusan_cloud_and_best_accuracy(self):
        self.assertEqual(ec.live_speech_session(cfg(live_assist_speech_model="cloud"), True),
                         ("managed", None, "fast"))
        self.assertEqual(ec.live_speech_session(cfg(live_assist_speech_model="cloud:best"), True),
                         ("managed", None, "best"))
        # English too: the pick is the pick
        self.assertEqual(ec.live_speech_session(
            cfg(live_assist_language="en", live_assist_speech_model="cloud:best"), True),
            ("managed", None, "best"))

    def test_a_cloud_pick_that_cannot_run_is_automatic(self):
        for value in ("cloud", "cloud:best"):
            c = cfg(live_assist_speech_model=value)
            self.assertEqual(ec.live_speech_session(c, False), (None, None, None))             # no Pro
            self.assertEqual(ec.live_speech_session(dict(c, privacy_mode=True), True), (None, None, None))
            en = dict(c, live_assist_language="en")
            self.assertEqual(ec.live_speech_session(en, False), (None, None, None))

    def test_your_own_google_key(self):
        c = cfg(live_assist_speech_model="own:google:gemini-2.5-pro", **GKEY)
        for pro in (True, False):                                     # no plan needed
            self.assertEqual(ec.live_speech_session(c, pro), ("google", None, "gemini-2.5-pro"))
        self.assertEqual(ec.live_speech_session(dict(c, privacy_mode=True), True), (None, None, None))
        no_key = dict(c, google_api_key="  ")
        self.assertEqual(ec.live_speech_session(no_key, False), (None, None, None))
        # the key removed under a pick behaves as Automatic - Armenian then goes to Pro's cloud
        self.assertEqual(ec.live_speech_session(no_key, True), ("managed", None, "fast"))

    def test_never_raises_on_odd_cfg(self):
        for c in (None, {}, {"live_assist_speech_model": 5}, {"live_assist_language": None}):
            self.assertEqual(ec.live_speech_session(c, True), (None, None, None))


class TestEngineLine(unittest.TestCase):
    def test_runs_on_names_what_transcribes(self):
        r = ec.runs_on(cfg(), "live_assistant_speech", pro=True)
        self.assertEqual((r["feature"], r["kind"], r["label"]),
                         ("live_assistant_speech", "cloud", "Gusan Cloud · Fast"))
        self.assertTrue(r["auto"])
        self.assertEqual(r["reason"], "language")             # Automatic chose it because of Armenian
        self.assertEqual(r["language_name"], "Armenian")
        self.assertEqual(r["note"], "")                       # on the cloud: nothing to warn about
        for alias in ("live_speech", "live_assist_speech"):
            self.assertEqual(ec.runs_on(cfg(), alias, pro=True)["label"], r["label"])
        r = ec.runs_on(cfg(live_assist_speech_model="cloud:best"), "live_speech", pro=True)
        self.assertEqual((r["label"], r["model_id"], r["auto"], r["reason"]),
                         ("Gusan Cloud · Best accuracy", "best", False, ""))
        r = ec.runs_on(cfg(live_assist_speech_model="own:google:gemini-3-flash-preview", **GKEY),
                       "live_speech", pro=False)
        self.assertEqual((r["kind"], r["label"]),
                         ("own_key", "Google Gemini · Gemini 3 Flash (preview) · your own key"))
        r = ec.runs_on(cfg(live_assist_language="en", live_assist_speech_model="small"), "live_speech")
        self.assertEqual((r["kind"], r["model_id"], r["label"]),
                         ("local", "small", "Balanced · Whisper Small · on this computer"))

    def test_automatic_without_the_cloud_is_dictations_engine(self):
        r = ec.runs_on(cfg(live_assist_language="en"), "live_speech", pro=True)
        self.assertEqual((r["kind"], r["model_id"], r["auto"], r["reason"], r["note"]),
                         ("local", "base", True, "", ""))
        same = ec.runs_on(cfg(live_assist_language="en"), "dictation", pro=True)
        self.assertEqual(r["label"], same["label"])

    def test_a_download_is_marked_only_for_a_whisper_pick(self):
        c = cfg(live_assist_speech_model="small")
        r = ec.runs_on(c, "live_speech", downloaded=lambda m: False)
        self.assertEqual((r["ready"], r["problem"]), (False, "needs_download"))
        self.assertEqual(ec.runs_on(c, "live_speech", downloaded=lambda m: True)["problem"], "")
        self.assertEqual(ec.runs_on(cfg(), "live_speech", pro=True, downloaded=lambda m: False)["problem"], "")

    def test_language_follows_the_cards_chain(self):
        c = {"live_assist_language": "", "language": "hy", "backend": "local"}
        self.assertEqual(ec.runs_on(c, "live_speech", pro=True)["language_name"], "Armenian")
        self.assertEqual(ec.runs_on(c, "live_speech", pro=True, language="en")["language_name"], "")
        self.assertEqual(ec.live_speech_language({"live_assist_language": "", "language": ""}), "auto")


class TestTheHint(unittest.TestCase):
    """The one plain line when local Whisper would transcribe a language it is poor at."""

    def test_free_names_the_cloud_options(self):
        note = ec.runs_on(cfg(), "live_speech", pro=False)["note"]
        self.assertEqual(note, "Whisper on this computer isn't reliable for Armenian. "
                               "Gusan Cloud (Pro) or your own Google key does much better.")

    def test_privacy_mode_says_why_and_names_the_way_out(self):
        note = ec.runs_on(cfg(privacy_mode=True), "live_speech", pro=True)["note"]
        self.assertIn("Privacy mode keeps transcription on this computer", note)
        self.assertIn("Armenian", note)
        self.assertIn("Gusan Cloud", note)
        self.assertIn("your own Google key", note)

    def test_a_local_pick_by_a_pro_person_points_at_automatic(self):
        note = ec.runs_on(cfg(live_assist_speech_model="small"), "live_speech", pro=True)["note"]
        self.assertIn("isn't reliable for Armenian", note)
        self.assertIn("Automatic or Gusan Cloud", note)

    def test_no_hint_when_it_is_not_local_whisper_or_not_that_language(self):
        self.assertEqual(ec.runs_on(cfg(), "live_speech", pro=True)["note"], "")          # on the cloud
        self.assertEqual(ec.runs_on(cfg(live_assist_speech_model="cloud:best"), "live_speech",
                                    pro=True)["note"], "")
        self.assertEqual(ec.runs_on(cfg(backend="google", **GKEY), "live_speech", pro=False)["note"], "")
        for lang in ("en", "fr", "auto"):
            self.assertEqual(ec.runs_on(cfg(live_assist_language=lang), "live_speech", pro=False)["note"], "")

    def test_a_cloud_take_whose_pieces_went_through_this_computer_says_so_for_armenian(self):
        e = ec.runs_on(cfg(), "live_speech", pro=True)                 # Automatic: Gusan Cloud
        self.assertEqual(e["kind"], "cloud")
        self.assertEqual(ec.live_speech_note(cfg(), True, None, True, engine=e), "")
        self.assertEqual(ec.live_speech_note(cfg(), True, None, True, engine=e, report={"local_pieces": 0}), "")
        note = ec.live_speech_note(cfg(), True, None, True, engine=e, report={"local_pieces": 2})
        self.assertEqual(note, "Whisper on this computer isn't reliable for Armenian, "
                               "so those parts may have mistakes.")
        # another language, or a take with nothing from this computer: nothing to add
        self.assertEqual(ec.live_speech_note(cfg(live_assist_language="en"), True, None, True, engine=e,
                                             report={"local_pieces": 2}), "")
        self.assertEqual(ec.live_speech_note(cfg(), True, None, True, engine=e, report={"local_pieces": "x"}), "")

    def test_the_words_are_calm_and_plain(self):
        for conf, pro in ((cfg(), False), (cfg(privacy_mode=True), True),
                          (cfg(live_assist_speech_model="small"), True)):
            note = ec.runs_on(conf, "live_speech", pro=pro)["note"]
            low = note.lower()
            for word in ("limit", "quota", "unlimited", "best language", "!", "warning", "error"):
                self.assertNotIn(word, low, (word, note))
            self.assertLessEqual(len(note), 190)


class TestPicker(unittest.TestCase):
    def test_rows_in_order(self):
        # (the Armenian model's row is hidden here: it is not on this computer and not listed)
        rows = ec.live_speech_choices({})
        self.assertEqual(rows[0], ("", "Automatic · Whisper Base", "Automatic · Whisper Base · Local", False))
        self.assertEqual([r[0] for r in rows[1:1 + len(ec.SPEECH_MODELS)]], list(ec.SPEECH_MODELS))
        self.assertTrue(all(not r[3] for r in rows[:1 + len(ec.SPEECH_MODELS)]))
        tail = rows[1 + len(ec.SPEECH_MODELS):]
        # Gusan Cloud is a service (a heading) that CHOOSES the model: its two rows are its profiles
        self.assertEqual(tail, [("cloud", "Fast", "Fast · Cloud · Pro", True),
                                ("cloud:best", "Best accuracy", "Best accuracy · Cloud · Pro", True)])

    def test_the_armenian_row_is_hidden_until_it_is_available_and_then_says_what_it_is(self):
        with hy(ready=False, listed=False):
            self.assertNotIn("hy-fastconformer", [r["value"] for r in ec.live_speech_rows({})])
        with hy(ready=False, listed=True):
            rows = ec.live_speech_rows({})
            row = next(r for r in rows if r["value"] == "hy-fastconformer")
            self.assertEqual((row["name"], row["tags"], row["group"]),
                             ("NVIDIA FastConformer Armenian", ("local",), ec.GROUP_THIS_COMPUTER))
            self.assertEqual(row["note"], "Armenian only · about 105 MB · free, works offline")
            # it sits with the other models on this computer, before Gusan Cloud
            values = [r["value"] for r in rows]
            self.assertLess(values.index("large-v3"), values.index("hy-fastconformer"))
            self.assertLess(values.index("hy-fastconformer"), values.index("cloud"))
        with hy(ready=True, listed=True):
            row = next(r for r in ec.live_speech_rows({}) if r["value"] == "hy-fastconformer")
            self.assertEqual(row["note"], "Armenian only · free, works offline")        # (no size once it is here)

    def test_own_key_rows_come_with_the_key_and_list_the_models(self):
        rows = ec.live_speech_choices(GKEY)
        own = [r for r in rows if r[0].startswith("own:google:")]
        self.assertEqual([r[0] for r in own], ["own:google:" + m for m in ec.GEMINI_SPEECH_MODELS])
        self.assertEqual(own[1], ("own:google:gemini-2.5-pro", "Gemini 2.5 Pro · your Google key",
                                  "Gemini 2.5 Pro · your Google key · Cloud", False))
        self.assertEqual([r for r in ec.live_speech_choices({"google_api_key": " "})
                          if r[0].startswith("own:")], [])

    def test_what_is_in_force_reads_automatic_when_a_pick_cannot_run(self):
        for value in ("cloud", "cloud:best"):
            c = cfg(live_assist_speech_model=value)
            self.assertEqual(ec.live_speech_pick(c, True), value)
            self.assertEqual(ec.live_speech_pick(c, False), "")
            self.assertEqual(ec.live_speech_pick(dict(c, privacy_mode=True), True), "")
        own = cfg(live_assist_speech_model="own:google:gemini-2.5-pro", **GKEY)
        self.assertEqual(ec.live_speech_pick(own, False), "own:google:gemini-2.5-pro")
        self.assertEqual(ec.live_speech_pick(dict(own, privacy_mode=True), True), "")
        self.assertEqual(ec.live_speech_pick(dict(own, google_api_key=""), True), "")
        self.assertEqual(ec.live_speech_pick(cfg(live_assist_speech_model="small"), False), "small")


class TestSetting(unittest.TestCase):
    def test_pro_is_checked_before_privacy_mode(self):
        for value in ("cloud", "cloud:best"):
            r = ec.set_live_speech(cfg(privacy_mode=True), value, pro=False)
            self.assertEqual((r["ok"], r["problem"], r["upsell"]), (False, "pro", ec.UPSELL_SPEECH))
            r = ec.set_live_speech(cfg(), value, pro=False)
            self.assertEqual(r["problem"], "pro")

    def test_privacy_mode_asks_and_can_be_turned_off_with_the_pick(self):
        r = ec.set_live_speech(cfg(privacy_mode=True), "cloud:best", pro=True)
        self.assertEqual((r["ok"], r["problem"], r["changes"]), (False, "privacy_mode", {}))
        r = ec.set_live_speech(cfg(privacy_mode=True), "cloud:best", pro=True, turn_off_privacy=True)
        self.assertEqual((r["ok"], r["changes"]),
                         (True, {"privacy_mode": False, "live_assist_speech_model": "cloud:best"}))
        own = cfg(privacy_mode=True, **GKEY)
        self.assertEqual(ec.set_live_speech(own, "own:google:gemini-2.5-pro", pro=False)["problem"],
                         "privacy_mode")
        self.assertEqual(ec.set_live_speech(own, "own:google:gemini-2.5-pro", pro=False,
                                            turn_off_privacy=True)["changes"],
                         {"privacy_mode": False, "live_assist_speech_model": "own:google:gemini-2.5-pro"})

    def test_own_key_needs_the_key(self):
        r = ec.set_live_speech(cfg(), "own:google:gemini-2.5-pro", pro=True)
        self.assertEqual((r["ok"], r["problem"]), (False, "needs_key"))
        r = ec.set_live_speech(cfg(privacy_mode=True), "own:google:gemini-2.5-pro", pro=True,
                               turn_off_privacy=True)
        self.assertEqual(r["problem"], "needs_key")            # nothing turned off for a pick that can't work
        ok = ec.set_live_speech(dict(cfg(), **GKEY), "own:google", pro=False)
        self.assertEqual(ok["changes"], {"live_assist_speech_model": "own:google:gemini-2.5-flash"})

    def test_whisper_models_automatic_and_unknown(self):
        self.assertEqual(ec.set_live_speech(cfg(), "", pro=False)["changes"], {"live_assist_speech_model": ""})
        self.assertEqual(ec.set_live_speech(cfg(), "small", pro=False)["changes"],
                         {"live_assist_speech_model": "small"})
        r = ec.set_live_speech(cfg(), "small", pro=False, downloaded=lambda m: False)
        self.assertEqual(r["problem"], "needs_download")
        self.assertTrue(ec.set_live_speech(cfg(), "small", pro=False, downloaded=lambda m: True)["ok"])
        for bad in ("whisper-9000", "own:google:nope", "managed"):
            self.assertEqual(ec.set_live_speech(cfg(), bad, pro=True)["problem"], "unknown", bad)
        # a Whisper model or Automatic is allowed in Privacy mode
        self.assertTrue(ec.set_live_speech(cfg(privacy_mode=True), "small", pro=False)["ok"])
        self.assertTrue(ec.set_live_speech(cfg(privacy_mode=True), "", pro=False)["ok"])


class TestWhatRanIsWhatIsShown(unittest.TestCase):
    """Gusan Cloud's server CHOOSES the engine and says which one ran (the answer's model, the
    X-Gusan-Model header): every line names that model, never just what was asked for."""

    def setUp(self):
        saved = dict(ec._CLOUD_SEEN)
        self.addCleanup(lambda: (ec._CLOUD_SEEN.clear(), ec._CLOUD_SEEN.update(saved)))
        ec._CLOUD_SEEN.clear()

    def test_status_line_names_the_model_and_where(self):
        e = ec.runs_on(cfg(live_assist_speech_model="cloud:best"), "live_speech", pro=True)
        # nothing has answered yet: the service and its profile
        self.assertEqual(ec.live_speech_status(e), "Transcribing with Gusan Cloud · Best accuracy")
        e = ec.runs_on(cfg(live_assist_speech_model="small"), "live_speech", pro=False)
        self.assertEqual(ec.live_speech_status(e), "Transcribing with Whisper Small · Local")
        e = ec.runs_on(cfg(live_assist_speech_model="own:google:gemini-2.5-pro", **GKEY), "live_speech")
        self.assertEqual(ec.live_speech_status(e),
                         "Transcribing with Gemini 2.5 Pro · your Google key · Cloud")

    def test_the_model_the_server_says_ran_is_the_line(self):
        e = ec.runs_on(cfg(live_assist_speech_model="cloud:best"), "live_speech", pro=True)
        for ran, shown in (("gpt-transcribe", "GPT Transcribe"), ("scribe_v2", "ElevenLabs Scribe v2"),
                           ("voxtral-mini-latest", "Voxtral Mini"), ("gpt-4o-transcribe", "GPT-4o Transcribe")):
            line = ec.live_speech_status(e, {"ran_model": ran})
            self.assertEqual(line, "Transcribing with %s · Cloud" % shown, ran)
            self.assertNotIn("available", line)           # the server choosing another model is not a downgrade
            self.assertNotIn("Gemini", line)
        # two models answered during the take (a piece fell to the next engine): both are named
        self.assertEqual(
            ec.live_speech_status(e, {"ran_models": ["scribe_v2", "gpt-transcribe"], "ran_model": "gpt-transcribe"}),
            "Transcribing with ElevenLabs Scribe v2 and GPT Transcribe · Cloud")
        self.assertEqual(ec.live_speech_status(e, {"ran_model": ""}),
                         "Transcribing with Gusan Cloud · Best accuracy")        # an older server says nothing
        self.assertEqual(ec.ran_names({"ran_models": ["a-x", "gpt-transcribe", "b-y"]}), "GPT Transcribe and b-y")
        self.assertEqual(ec.ran_names({}), "")

    def test_the_default_take_is_the_fast_profile(self):
        e = ec.take_engine(cfg(), {"backend": "managed", "cloud_model": ""})
        self.assertEqual((e["model_id"], e["model_name"], e["profile"]), ("fast", "Fast", "fast"))
        self.assertEqual(ec.live_speech_status(e, {"ran_model": "scribe_v2"}),
                         "Transcribing with ElevenLabs Scribe v2 · Cloud")
        v = ec.take_engine(dict(cfg(), managed_provider="mistral"), {"backend": "managed", "cloud_model": None})
        self.assertEqual(v["model_id"], "fast")                   # Dictation's own cloud take: the same profile
        g = ec.take_engine(dict(cfg(), **GKEY), {"backend": "google", "cloud_model": "gemini-2.5-pro"})
        self.assertEqual(g["model_id"], "gemini-2.5-pro")

    def test_your_own_key_is_not_second_guessed_when_google_names_a_version(self):
        e = ec.take_engine(dict(cfg(), **GKEY), {"backend": "google", "cloud_model": "gemini-2.5-pro"})
        self.assertEqual(ec.live_speech_status(e, {"ran_model": "gemini-2.5-pro-001"}),
                         "Transcribing with Gemini 2.5 Pro · your Google key · Cloud")

    def test_pieces_that_went_through_this_computer_are_mentioned(self):
        e = ec.runs_on(cfg(), "live_speech", pro=True)
        line = ec.live_speech_status(e, {"ran_model": "gpt-transcribe", "local_pieces": 2})
        self.assertIn("Transcribing with GPT Transcribe · Cloud.", line)
        self.assertIn("went through this computer", line)
        local = ec.runs_on(cfg(live_assist_speech_model="small"), "live_speech")
        self.assertNotIn("went through", ec.live_speech_status(local, {"local_pieces": 3}))

    def test_once_the_server_answered_the_picker_names_what_ran(self):
        c = cfg(live_assist_speech_model="cloud:best")
        r = ec.runs_on(c, "live_speech", pro=True)
        self.assertEqual((r["label"], r["model_id"]), ("Gusan Cloud · Best accuracy", "best"))
        ec.note_cloud_model("best", "gpt-transcribe")
        r = ec.runs_on(c, "live_speech", pro=True)
        self.assertEqual((r["label"], r["model_id"], r["asked_name"]),
                         ("Gusan Cloud · GPT Transcribe", "gpt-transcribe", "Best accuracy"))
        self.assertEqual(r["downgrade"], "")                       # no "wasn't available": the server chooses
        ec.note_cloud_model("best", "scribe_v2")                   # the next piece fell elsewhere
        self.assertEqual(ec.runs_on(c, "live_speech", pro=True)["label"], "Gusan Cloud · ElevenLabs Scribe v2")
        # the other profile is its own: Fast has not been answered yet
        self.assertEqual(ec.runs_on(cfg(), "live_speech", pro=True)["label"], "Gusan Cloud · Fast")
        self.assertEqual(ec.cloud_model_seen("fast"), "")
        # the picker's own rows still name the profiles, whatever ran
        self.assertEqual([r["name"] for r in ec.live_speech_rows(c, True) if r["value"].startswith("cloud")],
                         ["Fast", "Best accuracy"])

    def test_take_engine_is_what_the_recorder_reports(self):
        c = cfg(backend="local")
        e = ec.take_engine(c, {"backend": "managed", "cloud_model": "best"})
        self.assertEqual((e["kind"], e["label"], e["model_id"]),
                         ("cloud", "Gusan Cloud · Best accuracy", "best"))
        e = ec.take_engine(c, {"backend": "managed", "cloud_model": "gemini-2.5-pro"})      # an older value
        self.assertEqual(e["model_id"], "best")
        e = ec.take_engine(dict(c, managed_provider="mistral"), {"backend": "managed", "cloud_model": ""})
        self.assertEqual(e["label"], "Gusan Cloud · Fast")
        e = ec.take_engine(dict(c, managed_provider="mistral"), {"backend": "managed", "cloud_model": None})
        self.assertEqual(e["label"], "Gusan Cloud · Fast")          # Dictation's own cloud take
        e = ec.take_engine(dict(c, **GKEY), {"backend": "google", "cloud_model": "gemini-3-flash-preview"})
        self.assertEqual(e["label"], "Google Gemini · Gemini 3 Flash (preview) · your own key")
        e = ec.take_engine(c, {"backend": "local", "whisper_model": "medium"})
        self.assertEqual((e["kind"], e["model_id"]), ("local", "medium"))
        e = ec.take_engine(c, {"backend": "local", "whisper_model": "hy-fastconformer"})
        self.assertEqual((e["kind"], e["model_name"]), ("local", "NVIDIA FastConformer Armenian"))
        e = ec.take_engine(dict(c, privacy_mode=True), {"backend": "local", "whisper_model": "tiny"})
        self.assertEqual(e["kind"], "local")                                # Privacy mode never mislabels it


# ── the recorder's take ──────────────────────────────────────────────────────────

class TestRecorderTake(unittest.TestCase):
    @mock.patch("threading.Thread")
    def test_the_third_value_is_the_cloud_model_and_every_other_start_resets_it(self, _thread):
        rec = main.AudioRecorder()
        rec.audio = mock.MagicMock()
        self.assertIsNone(rec.cloud_model_override)
        try:
            with mock.patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": 1024,
                                            "input_device_index": None}):
                rec.start_recording(speech=("managed", None, "best"))
                self.assertEqual((rec.backend_override, rec.model_override, rec.cloud_model_override),
                                 ("managed", None, "best"))
                rec.start_recording(speech=("managed", None, "fast"))
                self.assertEqual((rec.backend_override, rec.cloud_model_override), ("managed", "fast"))
                rec.start_recording(speech=("local", "small", None))
                self.assertEqual((rec.backend_override, rec.model_override, rec.cloud_model_override),
                                 ("local", "small", None))
                rec.start_recording(speech=("managed", None))              # a Smart Action: two values
                self.assertIsNone(rec.cloud_model_override)
                rec.start_recording(speech=("google", None, "gemini-2.5-pro"))
                rec.start_recording(language="hy")                          # a meeting / Alt+R
                self.assertEqual((rec.backend_override, rec.model_override, rec.cloud_model_override),
                                 (None, None, None))
                self.assertEqual(rec._chunk_spans, {})
                self.assertEqual(rec._speech_ran, {"cloud_pieces": 0, "local_pieces": 0, "ran_model": ""})
        finally:
            rec.recording = False

    def test_a_cloud_models_name_never_becomes_the_local_whisper_model(self):
        me = SimpleNamespace(backend_override="managed", model_override=None,
                             cloud_model_override="gemini-2.5-pro")
        with mock.patch.dict(main.cfg, {"backend": "local", "whisper_model": "medium"}):
            self.assertEqual(main.AudioRecorder._speech_model(me), "medium")   # the fallback's model
            self.assertEqual(main.AudioRecorder._backend(me), "managed")
        # Privacy mode turned on mid-take: the rest of it stays on this computer
        with mock.patch.dict(main.cfg, {"backend": "local", "privacy_mode": True}):
            self.assertEqual(main.AudioRecorder._backend(me), "local")

    # ── Gusan Cloud ──
    def _post(self, answer, status=200, headers=None):
        resp = SimpleNamespace(status_code=status, text="", headers=headers or {},
                               json=lambda: answer)
        posts = []

        def post(url, **kw):
            posts.append((url, kw))
            return resp
        return post, posts

    def _managed_me(self, cloud, token="tok", lang="hy"):
        fallbacks = []
        me = SimpleNamespace(
            get_auth_token=lambda: token, _float_to_wav=lambda a: b"RIFF....WAVE",
            _lang_setting=lambda: lang, cloud_model_override=cloud,
            _speech_ran=main.AudioRecorder._new_speech_ran(),
            _fallback_to_local_or_error=lambda audio, backend, reason:
                fallbacks.append((backend, reason)) or ("on this computer", "en"))
        me.fallbacks = fallbacks
        return me

    def _run_managed(self, me, answer, **kw):
        post, posts = self._post(answer, **kw)
        with mock.patch("requests.post", post), \
                mock.patch.dict(main.cfg, {"sample_rate": 16000, "managed_provider": "mistral"}):
            out = main.AudioRecorder._run_managed(me, [0.0] * 8000)
        return out, posts

    def test_best_accuracy_asks_for_the_best_profile_and_remembers_what_ran(self):
        saved = dict(ec._CLOUD_SEEN)
        self.addCleanup(lambda: (ec._CLOUD_SEEN.clear(), ec._CLOUD_SEEN.update(saved)))
        ec._CLOUD_SEEN.clear()
        me = self._managed_me("best")
        out, posts = self._run_managed(me, {"text": "Բարև ձեզ", "lang": "hy", "model": "gpt-transcribe"})
        self.assertEqual(out, ("Բարև ձեզ", "hy"))
        body = posts[0][1]["json"]
        self.assertEqual((body["model"], body["provider"], body["language"]),
                         ("best", "gemini", "hy"))           # the profile, whatever Dictation's old provider says
        self.assertEqual(me._speech_ran, {"cloud_pieces": 1, "local_pieces": 0, "ran_model": "gpt-transcribe"})
        self.assertEqual(ec.cloud_model_seen("best"), "gpt-transcribe")

    def test_an_expired_session_does_not_empty_a_live_call(self):
        # Dictation / Smart Actions: the error stays (the person is asked to sign in again).
        me = self._managed_me(None, lang="fr")
        out, _posts = self._run_managed(me, {}, status=401)
        self.assertEqual(out, ("", "!managed:Session expired - sign in again."))
        self.assertEqual(me.fallbacks, [])
        me = self._managed_me(None)                       # in Armenian it is the take's gap/error
        out, _posts = self._run_managed(me, {}, status=401)
        self.assertEqual(out, ("", "!gap:signed_out"))
        self.assertEqual(me.fallbacks, [])
        # A Live Assistant take (Gusan Cloud, Best accuracy) in a language this computer reads
        # well: this computer reads the piece, so the call keeps a transcript - counted for the
        # card's line.
        for cloud in ("", "gemini-2.5-pro"):
            me = self._managed_me(cloud, lang="fr")
            out, _posts = self._run_managed(me, {}, status=401)
            self.assertEqual(out, ("on this computer", "en"), cloud)
            self.assertEqual(me.fallbacks, [("managed", "Session expired - sign in again.")], cloud)
        # Armenian: Whisper on this computer is poor at it, so the piece is a gap - never its text
        for cloud in ("", "gemini-2.5-pro"):
            me = self._managed_me(cloud)
            out, _posts = self._run_managed(me, {}, status=401)
            self.assertEqual(out, ("", "!gap:signed_out"), cloud)
            self.assertEqual(me.fallbacks, [], cloud)

    def test_an_old_server_answers_without_a_model_and_nothing_is_claimed(self):
        saved = dict(ec._CLOUD_SEEN)
        self.addCleanup(lambda: (ec._CLOUD_SEEN.clear(), ec._CLOUD_SEEN.update(saved)))
        ec._CLOUD_SEEN.clear()
        me = self._managed_me("best")
        out, _posts = self._run_managed(me, {"text": "words", "lang": "hy"})
        self.assertEqual(out, ("words", "hy"))                          # it still works
        self.assertEqual(me._speech_ran["ran_model"], "")               # no model said: none is named
        self.assertEqual(me._speech_ran["cloud_pieces"], 1)
        self.assertEqual(ec.cloud_model_seen("best"), "")
        # the model arrives in a header too
        me = self._managed_me("best")
        self._run_managed(me, {"text": "w", "lang": "hy"}, headers={"X-Gusan-Model": "scribe_v2"})
        self.assertEqual(me._speech_ran["ran_model"], "scribe_v2")
        self.assertEqual(ec.cloud_model_seen("best"), "scribe_v2")

    def test_every_request_names_its_profile_and_never_a_gemini_id(self):
        for cloud, want, provider in (("", "fast", "gemini"), ("fast", "fast", "gemini"),
                                      (None, "fast", "mistral"),             # Dictation keeps its old provider
                                      ("best", "best", "gemini"),
                                      ("gemini-2.5-pro", "best", "gemini"),   # an older value
                                      ("gemini-3-flash-preview", "fast", "gemini")):
            me = self._managed_me(cloud)
            _out, posts = self._run_managed(me, {"text": "t", "lang": "en"})
            body = posts[0][1]["json"]
            self.assertEqual((body["model"], body["provider"]), (want, provider), cloud)
            self.assertNotIn(body["model"], ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-3-flash-preview"))

    def test_a_live_piece_never_waits_long(self):
        me = self._managed_me("fast")
        _out, posts = self._run_managed(me, {"text": "t", "lang": "en"})
        self.assertEqual(posts[0][1]["timeout"], (5.0, 12.0))           # connect, then 12 s for the answer
        long_audio = [0.0] * (16000 * 100)                              # a dictation of 100 s needs more
        post, posts = self._post({"text": "t", "lang": "en"})
        with mock.patch("requests.post", post), mock.patch.dict(main.cfg, {"sample_rate": 16000}):
            main.AudioRecorder._run_managed(self._managed_me("fast", lang="en"), long_audio)
        self.assertGreater(posts[0][1]["timeout"][1], 12.0)
        self.assertLessEqual(posts[0][1]["timeout"][1], 40.0)

    def test_a_piece_that_falls_back_is_counted_for_the_card(self):
        me = self._managed_me("gemini-2.5-pro", token=None, lang="fr")  # offline: no token
        out, posts = self._run_managed(me, {})
        self.assertEqual((out, posts), (("on this computer", "en"), []))
        self.assertEqual(me.fallbacks[0][0], "managed")
        me = self._managed_me("gemini-2.5-pro", token=None)             # ...in Armenian: a gap
        out, posts = self._run_managed(me, {})
        self.assertEqual((out, posts, me.fallbacks), (("", "!gap:no_token"), [], []))
        me = SimpleNamespace(backend_override="managed", _speech_ran=main.AudioRecorder._new_speech_ran(),
                             _run_local_once=lambda a: ("x", "en"))
        with mock.patch.dict(main.cfg, {"backend": "local", "sample_rate": 16000}):
            main.AudioRecorder._run_local(me, [0.0] * 8000)
        self.assertEqual(me._speech_ran["local_pieces"], 1)
        me.backend_override = "local"                                   # a local pick: not a fallback
        with mock.patch.dict(main.cfg, {"backend": "managed", "sample_rate": 16000}):
            main.AudioRecorder._run_local(me, [0.0] * 8000)
        self.assertEqual(me._speech_ran["local_pieces"], 1)

    # ── your own Google key ──
    def _google(self, cloud, answer=None, **cfg_over):
        post, posts = self._post(answer or {"candidates": [{"content": {"parts": [{"text": "hello"}]}}],
                                            "modelVersion": cloud or "gemini-2.5-flash"})
        me = SimpleNamespace(_float_to_wav=lambda a: b"wav", _lang_setting=lambda: "hy",
                             cloud_model_override=cloud, _speech_ran=main.AudioRecorder._new_speech_ran(),
                             _fallback_to_local_or_error=lambda *a: ("local", "en"))
        conf = {"google_api_key": "AIza-test", "google_stt_model": "gemini-2.5-flash", "sample_rate": 16000}
        conf.update(cfg_over)
        with mock.patch("requests.post", post), mock.patch.dict(main.cfg, conf):
            out = main.AudioRecorder._run_google(me, [0.0] * 8000)
        return me, out, posts

    def test_own_key_takes_the_lives_models_with_a_valid_thinking_budget(self):
        want = {"gemini-2.5-flash": {"thinkingBudget": 0}, "gemini-2.5-pro": {"thinkingBudget": 128},
                "gemini-3-flash-preview": None}
        for model, thinking in want.items():
            me, out, posts = self._google(model)
            url, kw = posts[0]
            self.assertIn("/models/%s:generateContent" % model, url)
            gen = kw["json"]["generationConfig"]
            self.assertEqual(gen.get("thinkingConfig"), thinking, model)
            self.assertEqual(gen["temperature"], 0)
            self.assertEqual(out[0], "hello")
            self.assertEqual(me._speech_ran["ran_model"], model)

    def test_a_failed_request_never_puts_your_key_in_the_message(self):
        # requests quotes the URL in its errors, and the key is in the URL (?key=...)
        me = SimpleNamespace(_float_to_wav=lambda a: b"wav", _lang_setting=lambda: "fr",
                             cloud_model_override="gemini-2.5-pro",
                             _speech_ran=main.AudioRecorder._new_speech_ran(), fallbacks=[])
        me._fallback_to_local_or_error = lambda audio, backend, reason: (me.fallbacks.append(reason), ("x", "en"))[1]

        def boom(url, **kw):
            raise ConnectionError("HTTPSConnectionPool(host='generativelanguage.googleapis.com'): "
                                  "Max retries exceeded with url: /v1beta/models/gemini-2.5-pro:"
                                  "generateContent?key=AIza-secret-123 (Caused by timeout)")
        conf = {"google_api_key": "AIza-secret-123", "google_stt_model": "gemini-2.5-flash", "sample_rate": 16000}
        with mock.patch("requests.post", boom), mock.patch.dict(main.cfg, conf):
            main.AudioRecorder._run_google(me, [0.0] * 8000)
        self.assertEqual(len(me.fallbacks), 1)
        self.assertTrue(me.fallbacks[0].startswith("Google request failed:"))
        self.assertNotIn("AIza-secret-123", me.fallbacks[0])
        self.assertIn("Max retries exceeded", me.fallbacks[0])            # the rest of the reason stays

    def test_dictations_own_key_route_is_unchanged(self):
        me, _out, posts = self._google(None, google_stt_model="gemini-2.5-flash-lite")
        url, kw = posts[0]
        self.assertIn("/models/gemini-2.5-flash-lite:generateContent", url)
        self.assertEqual(kw["json"]["generationConfig"], {"temperature": 0})        # no thinking config as before

    # ── where a piece sits in the recording ──
    def _pieces_me(self, results, spans, kept):
        return SimpleNamespace(_chunk_lock=threading.Lock(), _chunk_results=dict(results),
                               _chunk_spans=dict(spans), _full_audio_len=kept)

    def test_pieces_become_timestamped_segments(self):
        me = self._pieces_me({0: "first", 1: "", 2: "  third  "},
                             {0: (0, 16000), 1: (16000, 40000), 2: (40000, 56000)}, 56000)
        with mock.patch.dict(main.cfg, {"sample_rate": 16000}):
            segs = main.AudioRecorder.piece_segments(me)
        self.assertEqual(segs, [{"start": 0.0, "end": 1.0, "text": "first"},
                                {"start": 2.5, "end": 3.5, "text": "third"}])

    def test_a_piece_past_the_recording_is_clipped_never_dropped(self):
        me = self._pieces_me({0: "a", 1: "tail"}, {0: (0, 16000), 1: (16000, 32000)}, 24000)
        with mock.patch.dict(main.cfg, {"sample_rate": 16000}):
            segs = main.AudioRecorder.piece_segments(me)
        self.assertEqual(segs[1], {"start": 1.0, "end": 1.5, "text": "tail"})
        me = self._pieces_me({0: "x"}, {0: (16000, 16000)}, 16000)               # zero length: still there
        with mock.patch.dict(main.cfg, {"sample_rate": 16000}):
            self.assertEqual(main.AudioRecorder.piece_segments(me)[0]["text"], "x")

    def test_a_piece_that_cannot_be_placed_says_so(self):
        with mock.patch.dict(main.cfg, {"sample_rate": 16000}):
            me = self._pieces_me({0: "a", 1: "b"}, {0: (0, 16000)}, 32000)       # piece 1 has no place
            self.assertIsNone(main.AudioRecorder.piece_segments(me))
            me = self._pieces_me({0: "a"}, {0: (0, 16000)}, 0)                   # nothing retained
            self.assertIsNone(main.AudioRecorder.piece_segments(me))
            me = self._pieces_me({}, {}, 0)                                      # nothing said: no segments, no error
            self.assertEqual(main.AudioRecorder.piece_segments(me), [])

    def test_joined_pieces_cover_all_their_audio(self):
        me = SimpleNamespace(_chunk_lock=threading.Lock(), _chunk_sources={},
                             _chunk_spans={3: (0, 8000), 4: (8000, 20000), 5: (20000, 24000)})
        main.AudioRecorder._on_chunks_merged(me, 3, [4, 5])
        self.assertEqual(me._chunk_spans[3], (0, 24000))
        me = SimpleNamespace(_chunk_lock=threading.Lock(), _chunk_sources={0: (1.0, 2.0)},
                             _chunk_spans={0: (0, 100), 1: (100, 300)})
        main.AudioRecorder._on_chunks_merged(me, 0, [1])
        self.assertEqual(me._chunk_spans[0], (0, 300))
        self.assertEqual(me._chunk_sources[0], (1.0, 2.0))

    def test_the_report_says_what_the_take_runs_and_what_answered(self):
        me = SimpleNamespace(backend_override="managed", model_override=None, language_override="hy",
                             cloud_model_override="gemini-2.5-pro",
                             _speech_ran={"cloud_pieces": 4, "local_pieces": 1, "ran_model": "gemini-2.5-flash"})
        with mock.patch.dict(main.cfg, {"backend": "local", "whisper_model": "small", "language": "en"}):
            rep = main.AudioRecorder.speech_report(me)
        self.assertEqual(rep, {"backend": "managed", "whisper_model": "small", "language": "hy",
                               "cloud_model": "gemini-2.5-pro", "ran_model": "gemini-2.5-flash",
                               "ran_models": [], "cloud_pieces": 4, "local_pieces": 1,
                               "gap_open": False, "gap_reason": "", "gap_language": "",
                               "gap_pieces": 0, "gap_total": 0, "gap_retries": 0,
                               "gap_recovered": 0})


class TestOnlyTheLiveAssistantMovesOffDictationsModel(unittest.TestCase):
    """Meetings and dictation: the same route as before 2.0.1."""

    def test_dictation_and_meetings_never_ask_the_live_choice(self):
        c = cfg(live_assist_speech_model="cloud:best", backend="local")
        # engine_choice's Dictation line ignores it entirely
        self.assertEqual(ec.runs_on(c, "dictation", pro=True)["kind"], "local")
        self.assertEqual(ec.runs_on(c, "meetings", pro=True)["kind"], "local")
        self.assertEqual(ec.runs_on(c, "smart_actions_speech", pro=True)["kind"], "local")
        # ...and the recorder only sees a take's own speech=
        me = SimpleNamespace(backend_override=None, model_override=None, cloud_model_override=None)
        with mock.patch.dict(main.cfg, {"backend": "local", "whisper_model": "base"}):
            self.assertEqual(main.AudioRecorder._backend(me), "local")
            self.assertEqual(main.AudioRecorder._speech_model(me), "base")

    def test_the_settings_a_pro_lapse_resets(self):
        app = mock.MagicMock()
        app.cfg = {"backend": "local", "live_assist_speech_model": "cloud:best",
                   "smart_actions_speech_model": ""}
        app.auth = SimpleNamespace(is_pro=False, entitlement_known=True, user_email="", user_id=None)
        app.calendar = None
        app.main_win = None
        main.AppController._on_auth_changed(app)
        self.assertEqual(app.cfg["live_assist_speech_model"], "")
        for value in ("small", "own:google:gemini-2.5-pro", ""):
            app.cfg["live_assist_speech_model"] = value
            main.AppController._on_auth_changed(app)
            self.assertEqual(app.cfg["live_assist_speech_model"], value)       # no plan needed for these
        app.cfg["live_assist_speech_model"] = "cloud"
        app.auth = SimpleNamespace(is_pro=False, entitlement_known=False, user_email="", user_id=None)
        main.AppController._on_auth_changed(app)                               # offline: an unknown answer
        self.assertEqual(app.cfg["live_assist_speech_model"], "cloud")


class TestPrewarm(unittest.TestCase):
    def _app(self):
        loads = []
        app = SimpleNamespace(preloads=[], loads=loads,
                              recorder=SimpleNamespace(load_model=lambda n: loads.append(n)))
        app._preload_speech_model = lambda: app.preloads.append(1)
        return app

    def _run(self, app, **kw):
        class Inline:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
                self.target = target

            def start(self):
                self.target()
        with mock.patch.object(main.threading, "Thread", Inline), \
                mock.patch.object(main, "model_downloaded", lambda name: name != "large-v3"):
            main.AppController.prewarm_speech_model(app, **kw)

    def test_no_choice_is_dictations_preload_as_before(self):
        app = self._app()
        self._run(app)
        self._run(app, speech=None)
        self.assertEqual(app.preloads, [1, 1])

    def test_a_live_whisper_model_loads_that_one_if_it_is_on_disk(self):
        app = self._app()
        self._run(app, speech=("local", "small", None))
        self._run(app, speech=("local", "large-v3", None))             # not on disk: no download here
        self.assertEqual((app.loads, app.preloads), (["small"], []))

    def test_a_cloud_take_loads_nothing(self):
        app = self._app()
        self._run(app, speech=("managed", None, ""))
        self._run(app, speech=("google", None, "gemini-2.5-pro"))
        self.assertEqual((app.loads, app.preloads), ([], []))


class TestTelemetry(unittest.TestCase):
    def test_the_choice_is_reported_by_class_only(self):
        self.assertIn("live_assist_speech_model", main.AppController._TRACKED_SETTINGS)
        c = main.AppController.__new__(main.AppController)
        for value, want in (("", "auto"), ("small", "local"), ("cloud:best", "cloud_best"),
                            ("own:google:gemini-2.5-pro", "own_key")):
            c.cfg = {"live_assist_speech_model": value}
            self.assertEqual(main.AppController._settings_snapshot(c)["live_assist_speech_model"], want)

    def test_a_change_sends_the_existing_backend_selected_event_with_the_class(self):
        sent = []
        c = main.AppController.__new__(main.AppController)
        c.cfg = {"live_assist_speech_model": ""}
        c._tracked_settings = None                                          # (under the stand-ins an unset
        c.track = lambda event, props=None: sent.append((event, props))     # attribute reads as a mock)
        main.AppController._track_setting_changes(c)                         # the first look: nothing sent
        c.cfg["live_assist_speech_model"] = "cloud:best"
        main.AppController._track_setting_changes(c)
        self.assertEqual(sent[0], ("backend_selected", {"feature": "live_assist", "choice": "cloud_best"}))
        self.assertEqual(sent[1][0], "settings_saved")
        self.assertEqual(sent[1][1]["live_assist_speech_model"], "cloud_best")
        import telemetry
        self.assertIn("backend_selected", telemetry.ALLOWED_EVENTS)           # no new event: nothing to deploy


# ── the server's source ──────────────────────────────────────────────────────────

def _source():
    if not os.path.exists(PROXY):
        raise unittest.SkipTest("no transcribe-proxy source here")
    with open(PROXY, encoding="utf-8") as f:
        return f.read()


class TestProxySource(unittest.TestCase):
    def _catalog(self, src):
        m = re.search(r"const CATALOG[^=]*= \{(.*?)\n\};", src, re.S)
        self.assertIsNotNone(m)
        return dict(re.findall(r'"([^"]+)": "([a-z]+)"', m.group(1)))

    def _order(self, src, name):
        m = re.search(r"const %s = \[([^\]]*)\]" % name, src)
        self.assertIsNotNone(m, name)
        return re.findall(r'"([^"]+)"', m.group(1))

    def test_its_catalog_and_default_orders(self):
        src = _source()
        cat = self._catalog(src)
        self.assertEqual(cat, {
            "scribe_v2": "elevenlabs",
            "voxtral-mini-latest": "mistral", "gpt-transcribe": "openai", "gpt-4o-transcribe": "openai",
            "gpt-4o-mini-transcribe": "openai", "whisper-1": "openai", "gemini-3.8-flash": "gemini",
            "gemini-3.1-pro-preview": "gemini", "gemini-2.5-flash": "gemini", "gemini-2.5-pro": "gemini"})
        # What the app asks for is a PROFILE word - the proxy knows both - never a catalog id
        for word in ec.CLOUD_PROFILES:
            self.assertIn('"%s"' % word, src)
        self.assertIn('asked === "best"', src)
        self.assertNotIn("fast", cat)
        self.assertNotIn("best", cat)
        # Every model the server can answer with has a name the app shows (X-Gusan-Model)
        for mid in cat:
            if not mid.startswith("gemini"):
                self.assertIn(mid, ec.MODEL_NAMES, mid)
        # ElevenLabs Scribe is first in BOTH profiles (it is skipped without a key).
        self.assertEqual(self._order(src, "FAST_ORDER"), [
            "scribe_v2", "voxtral-mini-latest", "gpt-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"])
        self.assertEqual(self._order(src, "BEST_ORDER"), [
            "scribe_v2", "gpt-transcribe", "gpt-4o-transcribe", "voxtral-mini-latest", "gpt-4o-mini-transcribe"])
        for mid in self._order(src, "FAST_ORDER") + self._order(src, "BEST_ORDER"):
            self.assertIn(mid, cat)
            self.assertNotEqual(cat[mid], "gemini", mid)       # the Gemini key is dead: not in a default

    def test_an_unknown_model_falls_back_and_only_the_catalog_reaches_the_url(self):
        src = _source()
        self.assertIn("inCatalog(asked)", src)
        self.assertEqual((src.count("body?.model"), src.count("body.model")), (1, 1))   # the check + the one read
        self.assertIn("models/${model}:generateContent", src)
        self.assertNotIn("models/gemini-2.5-flash:generateContent", src)   # no hard-wired model
        self.assertIn("hasOwnProperty.call(CATALOG, id)", src)             # "__proto__" is not a model

    def test_thinking_budgets(self):
        src = _source()
        self.assertIn('if (model.startsWith("gemini-2.5-pro")) return 128;', src)
        self.assertIn('if (model.startsWith("gemini-2.5-flash")) return 0;', src)
        self.assertEqual(cs.PRO_MIN_THINKING_BUDGET, 128)
        self.assertEqual(cs.thinking_config("gemini-2.5-pro"), {"thinkingBudget": 128})
        self.assertEqual(cs.thinking_config("gemini-2.5-flash"), {"thinkingBudget": 0})
        self.assertIn("thinkingConfig: { thinkingBudget: budget }", src)

    def test_every_attempt_has_a_timeout(self):
        src = _source()
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("//"))
        self.assertEqual(code.count("fetch("), 4)                       # gemini, mistral, openai, elevenlabs
        # gemini and mistral each have their own timer; OpenAI and ElevenLabs make ONE and share it with
        # their language retry (a refused language cannot add a second timeout).
        self.assertEqual(code.count("signal: AbortSignal.timeout(timeoutMs)"), 2)
        self.assertEqual(code.count("const signal = AbortSignal.timeout(timeoutMs);"), 2)
        self.assertIn("Math.min(60000, Math.max(3500, 2500 + seconds * 400))", src)
        self.assertIn("(audioB64.length * 0.75) / 32000", src)
        self.assertIn('env("STT_TIMEOUT_MS")', src)
        self.assertIn('env("STT_TOTAL_MS")', src)
        self.assertIn("Math.min(80000, Math.max(20000, 2 * attemptMs))", src)

    def test_the_header_documents_the_secrets(self):
        src = _source()
        head = src[:src.index("import { createClient }")]
        for name in ("OPENAI_TOKEN", "OPENAI_API_KEY", "OPENAI_KEY", "MISTRAL_STT_KEY", "GOOGLE_STT_KEY",
                     "GEMINI_KEY", "STT_FAST_ORDER", "STT_BEST_ORDER", "STT_TIMEOUT_MS", "STT_TOTAL_MS",
                     "STT_AUTO_VOXTRAL", "ELEVENLABS_API_KEY", "ELEVENLABS_KEY", "ELEVEN_API_KEY", "XI_API_KEY",
                     "STT_ELEVEN_PAUSE_MS", "STT_ELEVEN_OUTAGE_PAUSE_MS", "scribe_v2", "elevenlabs:scribe_v3", "provider_credits"):
            self.assertIn(name, head, name)

    def test_elevenlabs_in_the_source(self):
        src = _source()
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("//"))
        for needle in ('"https://api.elevenlabs.io/v1/speech-to-text"', '"xi-api-key": engineKey("elevenlabs")',
                       'form.append("model_id", model);', 'form.append("tag_audio_events", "false");',
                       'form.append("language_code", language);', "const ELEVEN_MIN_TIMEOUT_MS = 8000;",
                       "const ELEVEN_PAUSE_DEFAULT_MS = 900000;", "const ELEVEN_BUSY_PAUSE_MS = 15000;",
                       "const ELEVEN_OUTAGE_PAUSE_DEFAULT_MS = 30000;", "const ELEVEN_BAD_ANSWER_PAUSE_MS = 60000;",
                       'env("STT_ELEVEN_OUTAGE_PAUSE_MS")', 'redirect: "error"',
                       'env("STT_ELEVEN_PAUSE_MS")', 'if (status === 402) return "provider_credits";',
                       '"elevenlabs"', "stt_pause: \"elevenlabs\""):
            self.assertIn(needle, code, needle)
        # nothing else is asked of ElevenLabs: no speaker labels, no timestamps, no multichannel
        for banned in ("diarize", "timestamps_granularity", "use_multi_channel", "num_speakers", "keyterms"):
            self.assertNotIn(banned, code, banned)
        # the key only ever goes into the xi-api-key header, never into a log line or an answer
        self.assertEqual(code.count('engineKey("elevenlabs")'), 1)
        # no redirect is followed with the key in a custom header (fetch keeps it across origins)
        self.assertEqual(code.count('redirect: "error"'), 1)

    def test_it_says_what_ran(self):
        src = _source()
        self.assertIn("model: result.model", src)
        self.assertIn('"X-Gusan-Model": result.model', src)
        self.assertIn("transcribeMistral(bytes: Uint8Array, language: string, model: string", src)
        self.assertIn("console.error(JSON.stringify({ stt_fail: provider, model, status,", src)
        self.assertIn("detail: redact(detail).slice(0, 120)", src)
        self.assertIn("detail: status === 0 ? detail : publicDetail(status)", src)   # never a provider's text

    def test_everything_else_is_as_it_was(self):
        src = _source()
        for needle in ('{ error: "unauthorized" }, 401', '{ error: "pro_required" }, 403',
                       '{ error: "quota_exceeded" }, 429', '{ error: "no_audio" }, 400',
                       '{ error: "stt_not_configured" }, 503', "const DAILY_CAP = 3000;",
                       'const MISTRAL_MODEL = "voxtral-mini-latest";', "use_cloud_quota", 'rpc("is_pro")',
                       "GOOGLE_STT_KEY", "MISTRAL_STT_KEY", "function mistralCovers(",
                       '{ error: "audio_too_large" }, 413', "typeof body?.provider === \"string\""):
            self.assertIn(needle, src, needle)


def _node_can_strip_types():
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout
        major, minor = [int(x) for x in out.strip().lstrip("v").split(".")[:2]]
    except Exception:
        return None
    return node if (major, minor) >= (22, 6) else None


HARNESS = r"""
// The real transcribe-proxy/index.ts, run under Node with Deno, Supabase and the four speech
// providers faked. argv: module URL, scenarios file. Prints {scenario name: result}.
// Every scenario imports the module FRESH (its own module state: the ElevenLabs credits breaker
// cannot leak from one scenario into the next); a scenario may carry `then`: more requests to the
// SAME module instance, each with an optional `advance_ms` (a clock skew on Date.now, so a 15 minute
// pause is tested in no time), `env` (merged), `behave` (replaced) and `body`.
import { readFileSync } from "node:fs";
const realNow = Date.now.bind(Date);
let SKEW = 0;
Date.now = () => realNow() + SKEW;
const realTimeout = AbortSignal.timeout.bind(AbortSignal);
let lastTimeout = null;
AbortSignal.timeout = (ms) => { lastTimeout = ms; return realTimeout(ms); };
const BASE_ENV = { SUPABASE_URL: "http://x", SUPABASE_ANON_KEY: "k", GOOGLE_STT_KEY: "SECRET-google",
                   MISTRAL_STT_KEY: "SECRET-mistral", OPENAI_TOKEN: "SECRET-openai" };
const ENV = { ...BASE_ENV };
globalThis.Deno = { env: { get: (k) => ENV[k] }, serve: (fn) => { globalThis.__handler = fn; } };
const STATE = { pro: true, under: true, user: true };
globalThis.__state = STATE;
let BEHAVE = {};
let calls = [];
let logs = [];
console.log = (...a) => { logs.push(["log", a.map(String).join(" ")]); };
console.error = (...a) => { logs.push(["error", a.map(String).join(" ")]); };

function behaviour(provider, model) {
  const spec = BEHAVE[provider];
  if (spec !== null && typeof spec === "object") return spec[model] ?? spec["*"] ?? "ok";
  return spec ?? "ok";
}
globalThis.fetch = async (url, init) => {
  const u = String(url);
  let provider, model, language = null, fields = null, auth = null, xi = null, form = null;
  if (u.includes("generativelanguage")) {
    provider = "gemini"; model = /models\/([^:]+):/.exec(u)[1];
  } else if (u.includes("api.elevenlabs.io")) {
    provider = "elevenlabs";
    model = init.body.get("model_id");
    language = init.body.get("language_code");
    fields = [...init.body.keys()];
    xi = init.headers?.["xi-api-key"] ?? null;
    auth = init.headers?.Authorization ?? null;
    form = {}; for (const [k, v] of init.body.entries()) if (k !== "file") form[k] = String(v);
  } else {
    provider = u.includes("api.mistral.ai") ? "mistral" : u.includes("api.openai.com") ? "openai" : "other";
    model = init.body.get("model");
    language = init.body.get("language");
    fields = [...init.body.keys()];
    auth = init.headers?.Authorization ?? null;
  }
  const payload = typeof init.body === "string" ? JSON.parse(init.body) : null;
  calls.push({ url: u, provider, model, language, fields, auth, xi, form, timeout_ms: lastTimeout,
               method: init.method, has_signal: !!init.signal, redirect: init.redirect ?? null,
               budget: payload?.generationConfig?.thinkingConfig?.thinkingBudget ?? null,
               prompt: payload?.contents?.[0]?.parts?.[0]?.text ?? null });
  const b = behaviour(provider, model);
  if (b === "hang") {
    return await new Promise((_, reject) => {
      init.signal.addEventListener("abort", () => reject(init.signal.reason));
    });
  }
  if (b === "throw") throw new TypeError("fetch failed " + u);
  if (b === "lang400hang") {
    if (language) return new Response(JSON.stringify({ error: { message: "Language 'xx' is not supported" } }), { status: 400 });
    return await new Promise((_, reject) => { init.signal.addEventListener("abort", () => reject(init.signal.reason)); });
  }
  if (b === "lang400" && language) {
    return new Response(JSON.stringify({ error: { message: "Language 'xx' is not supported" } }), { status: 400 });
  }
  if (b === "auth401") {
    return new Response("Incorrect API key provided: sk-proj-********abcd. Rate limit reached in organization " +
      "org-ABC123XYZ on requests per min. Project `proj_Q9secret` does not have access", { status: 401 });
  }
  if (typeof b === "number") return new Response("err-" + b, { status: b });
  // ElevenLabs' own answers (the ones the docs show, and the ones a spent account may give)
  const J = (o, status) => new Response(JSON.stringify(o), { status });
  if (b === "credits402") return J({ detail: { status: "insufficient_credits", message: "Not enough credits. " +
    "Your key sk_0123456789abcdef0123 / SECRET-eleven cannot pay" } }, 402);
  if (b === "invalid401") return J({ detail: { status: "invalid_api_key", message: "Invalid API key" } }, 401);
  if (b === "quota401") return J({ detail: { status: "quota_exceeded", message: "You have no quota left" } }, 401);
  if (b === "quota400") return J({ detail: { status: "quota_exceeded", message: "no quota" } }, 400);
  if (b === "credits_words500") return J({ detail: "out of credits" }, 500);
  if (b === "perm403") return J({ detail: { status: "insufficient_permissions", message: "no" } }, 403);
  if (b === "busy429") return J({ detail: { status: "rate_limit_exceeded", message: "slow down" } }, 429);
  if (b === "lang422" && language) {
    return J({ detail: [{ loc: ["body", "language_code"], msg: "language not supported", type: "value_error" }] }, 422);
  }
  if (b === "lang422hang") {
    if (language) return J({ detail: [{ loc: ["body", "language_code"], msg: "language not supported", type: "x" }] }, 422);
    return await new Promise((_, reject) => { init.signal.addEventListener("abort", () => reject(init.signal.reason)); });
  }
  if (b === "plain422") return J({ detail: [{ loc: ["body", "file"], msg: "audio is too short", type: "value_error" }] }, 422);
  if (b === "multichannel") return J({ transcripts: [{ text: " a words ", words: [] }] }, 200);
  if (b === "empty") return J({ language_code: "eng", language_probability: 0.9, text: "", words: [] }, 200);
  if (b === "number_text") return J({ text: 5 }, 200);
  if (b === "badjson") return new Response("<html>nope", { status: 200 });
  const text = ` ${model} words `;
  if (provider === "elevenlabs") {
    return J({ language_code: "eng", language_probability: 0.98, text, words: [{ text: "x", type: "word" }] }, 200);
  }
  if (provider === "gemini") {
    return new Response(JSON.stringify({ candidates: [{ content: { parts: [{ text }] } }] }), { status: 200 });
  }
  return new Response(JSON.stringify({ text }), { status: 200 });
};
let handler = null;
const keepAlive = setInterval(() => {}, 1000);     // AbortSignal.timeout's own timer does not keep Node alive

async function run(sc) {
  for (const k of Object.keys(ENV)) delete ENV[k];
  Object.assign(ENV, BASE_ENV);
  for (const [k, v] of Object.entries(sc.env ?? {})) { if (v === null) delete ENV[k]; else ENV[k] = v; }
  Object.assign(STATE, { pro: true, under: true, user: true, quota: 0 }, sc.state ?? {});
  BEHAVE = sc.behave ?? {};
  calls = []; logs = [];
  const headers = { "Content-Type": "application/json", ...(sc.noauth ? {} : { Authorization: "Bearer t" }),
                    ...(sc.headers ?? {}) };
  const method = sc.method ?? "POST";
  const t0 = Date.now();
  const res = await handler(new Request("http://proxy/", { method, headers,
    body: method === "POST" ? (typeof sc.body === "string" ? sc.body : JSON.stringify(sc.body)) : undefined }));
  const text = await res.text();
  let json = null; try { json = JSON.parse(text); } catch (_e) {}
  return { status: res.status, json, raw: text, model_header: res.headers.get("X-Gusan-Model"),
           calls, logs, ms: Date.now() - t0, quota: STATE.quota };
}

const out = {};
let n = 0;
for (const sc of JSON.parse(readFileSync(process.argv[3], "utf-8"))) {
  SKEW = 0;
  await import(process.argv[2] + "?scenario=" + (n++));       // a fresh module: fresh breaker
  handler = globalThis.__handler;
  const first = await run(sc);
  if (sc.then) {
    first.then = [];
    for (const st of sc.then) {
      SKEW += st.advance_ms ?? 0;
      first.then.push(await run({ ...sc, ...st, env: { ...(sc.env ?? {}), ...(st.env ?? {}) }, then: undefined }));
    }
  }
  out[sc.name] = first;
}
clearInterval(keepAlive);
process.stdout.write(JSON.stringify(out));
"""

STUB_SUPABASE = r"""
export function createClient() {
  const S = () => globalThis.__state;
  return {
    auth: { getUser: async () => ({ data: { user: S().user ? { id: "u1" } : null }, error: S().user ? null : {} }) },
    rpc: async (name) => {
      if (name === "use_cloud_quota") S().quota = (S().quota ?? 0) + 1;
      return name === "is_pro" ? { data: S().pro, error: null }
           : name === "use_cloud_quota" ? { data: S().under, error: null } : { data: null, error: {} };
    },
  };
}
"""

_AUDIO = "QUJD"                                  # "ABC": the fake providers never decode it
_NO_OPENAI = {"OPENAI_TOKEN": None}
_NO_GOOGLE = {"GOOGLE_STT_KEY": None}
_ONLY_MISTRAL = {"OPENAI_TOKEN": None, "GOOGLE_STT_KEY": None}
_ONLY_OPENAI = {"MISTRAL_STT_KEY": None, "GOOGLE_STT_KEY": None}
_NOTHING = {"OPENAI_TOKEN": None, "GOOGLE_STT_KEY": None, "MISTRAL_STT_KEY": None}
_FAST4 = ["voxtral-mini-latest", "gpt-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"]
_EL = {"ELEVENLABS_API_KEY": "SECRET-eleven"}                  # the default scenarios have NO ElevenLabs key
_EL_ONLY = dict(_NOTHING, **_EL)
_SCRIBE = "scribe_v2"


def _a(**kw):
    return dict({"audio": _AUDIO}, **kw)


def _scenarios():
    s = []

    def add(name, body=None, **kw):
        s.append(dict({"name": name, "body": body if body is not None else _a()}, **kw))

    # profiles and what they pick first
    add("default")                                                       # no language, no model
    add("en", _a(language="en"))
    add("hy", _a(language="hy"))
    add("hy_langs", _a(language="hy", languages=["hy"]))
    add("auto", _a(language="auto"))
    add("multi_covered", _a(language="multi", languages=["en", "fr"]))
    add("multi_hy", _a(language="multi", languages=["hy", "en"]))
    add("best", _a(model="best", language="en"))
    add("pro", _a(model="gemini-2.5-pro", language="hy"))
    add("pro_en", _a(model="gemini-2.5-pro", language="en"))
    add("flash", _a(model="gemini-2.5-flash", language="en"))
    add("fast", _a(model="fast", language="en"))
    # pinned
    add("pin_4o", _a(model="gpt-4o-transcribe", language="en"))
    add("pin_mini", _a(model="gpt-4o-mini-transcribe"))
    add("pin_whisper", _a(model="whisper-1", language="en"))
    add("pin_gpt", _a(model="gpt-transcribe"))
    add("pin_voxtral", _a(model="voxtral-mini-latest", language="en"))
    add("pin_voxtral_hy", _a(model="voxtral-mini-latest", language="hy"))
    add("pin_gemini_flash3", _a(model="gemini-3.8-flash", language="hy"))
    add("pin_gemini_pro3", _a(model="gemini-3.1-pro-preview", language="en"))
    add("pin_4o_fails", _a(model="gpt-4o-transcribe", language="en"),
        behave={"openai": {"gpt-4o-transcribe": 500}})
    # unknown / hostile / not a string: the FAST profile, never an error, never in a URL
    for i, bad in enumerate(["gemini-9-ultra", "gpt-transcribe:evil?key=x", "../../evil", "__proto__",
                             "constructor", "toString", "", "   ", 5, None, True, ["best"], {"a": 1}]):
        add("bad_%d" % i, _a(model=bad, language="en"))
    add("bad_hy", _a(model="gemini-9-ultra", language="hy"))
    add("header_pin", _a(language="en"), headers={"X-Gusan-Model": "gpt-4o-transcribe"})
    add("header_best", _a(language="en"), headers={"X-Gusan-Model": "gemini-2.5-pro"})
    add("header_bad", _a(language="en"), headers={"X-Gusan-Model": "evil/../x"})
    add("body_wins", _a(model="fast", language="en"), headers={"X-Gusan-Model": "gpt-4o-transcribe"})
    add("body_wins_best", _a(model="gpt-transcribe", language="en"), headers={"X-Gusan-Model": "best"})
    add("header_when_body_not_string", _a(model=5, language="en"), headers={"X-Gusan-Model": "best"})
    # old clients (2.0.0 / 2.0.1 and before): language / languages / provider, maybe no model
    add("old_gemini_en", {"audio": _AUDIO, "language": "en", "provider": "gemini"})
    add("old_gemini_hy", {"audio": _AUDIO, "language": "hy", "languages": ["hy"], "provider": "gemini"})
    add("old_mistral_fr", {"audio": _AUDIO, "language": "fr", "provider": "mistral"})
    add("old_mistral_hy", {"audio": _AUDIO, "language": "hy", "provider": "mistral"})
    add("old_mistral_best", {"audio": _AUDIO, "language": "en", "provider": "mistral", "model": "best"})
    add("old_mistral_pin", {"audio": _AUDIO, "language": "en", "provider": "mistral", "model": "gpt-transcribe"})
    add("old_odd", {"audio": _AUDIO, "language": 7, "languages": "hy", "provider": 3})
    add("old_only_audio", {"audio": _AUDIO})
    # the language sent to OpenAI
    add("lang_none", _a(model="gpt-transcribe"))
    add("lang_auto", _a(model="gpt-transcribe", language="auto"))
    add("lang_multi", _a(model="gpt-transcribe", language="multi", languages=["hy", "en"]))
    add("lang_iso3", _a(model="gpt-transcribe", language="hye"))
    add("lang_junk", _a(model="gpt-transcribe", language="EN"))
    add("lang_junk2", _a(model="gpt-transcribe", language="hy-AM"))
    add("lang_400", _a(language="hy"), behave={"openai": "lang400"})
    add("other_400", _a(language="hy"), behave={"openai": 400})
    # failover
    add("voxtral_500", _a(language="en"), behave={"mistral": 500})
    add("voxtral_429", _a(language="en"), behave={"mistral": 429})
    add("voxtral_429_noopenai", _a(language="en"), behave={"mistral": 429}, env=_NO_OPENAI)
    add("openai_missing_en", _a(language="en"), env=_NO_OPENAI)
    add("openai_missing_hy", _a(language="hy"), env=_NO_OPENAI)
    add("openai_missing_hy_only_mistral", _a(language="hy"), env=_ONLY_MISTRAL)
    add("openai_missing_best_hy", _a(model="best", language="hy"), env=_ONLY_MISTRAL)
    add("key_api_key", _a(language="hy"), env={"OPENAI_TOKEN": None, "OPENAI_API_KEY": "SECRET-openai2"})
    add("key_key", _a(language="hy"), env={"OPENAI_TOKEN": None, "OPENAI_KEY": "SECRET-openai3"})
    add("key_prefers_token", _a(language="hy"), env={"OPENAI_API_KEY": "SECRET-other"})
    add("all_fail_en", _a(language="en"),
        behave={"mistral": 500, "openai": 503, "gemini": 500})
    add("all_fail_hy", _a(language="hy"), behave={"mistral": 500, "openai": 502})
    add("only_openai_en", _a(language="en"), env=_ONLY_OPENAI)
    add("whisper_last", _a(language="en"),
        behave={"mistral": 500, "openai": {"*": 500, "whisper-1": "ok"}}, env={"STT_FAST_ORDER": "voxtral-mini-latest,whisper-1"})
    # not configured
    add("nothing_configured", _a(language="en"), env=_NOTHING)
    add("nothing_configured_hy", _a(language="hy"), env=_NOTHING)
    add("only_google_default", _a(language="en"), env=dict(_NOTHING, GOOGLE_STT_KEY="SECRET-google"))
    add("only_google_in_order", _a(language="hy"),
        env=dict(_NOTHING, GOOGLE_STT_KEY="SECRET-google", STT_FAST_ORDER="gemini-3.8-flash"))
    add("only_gemini_key_name", _a(language="hy"),
        env=dict(_NOTHING, GEMINI_KEY="SECRET-gem", STT_FAST_ORDER="gemini-2.5-flash"))
    # timeouts and network errors (a tiny timeout makes a hang cheap)
    add("hang_first", _a(language="en"), behave={"mistral": "hang"}, env={"STT_TIMEOUT_MS": "150"})
    add("hang_all", _a(language="en"), behave={"mistral": "hang", "openai": "hang", "gemini": "hang"},
        env={"STT_TIMEOUT_MS": "100"})
    add("hang_openai_hy", _a(language="hy"), behave={"openai": {"gpt-transcribe": "hang"}},
        env={"STT_TIMEOUT_MS": "150"})
    add("hang_gemini_then_openai", _a(model="gemini-3.8-flash", language="hy"),
        behave={"gemini": "hang"}, env={"STT_TIMEOUT_MS": "150"})
    add("throw_first", _a(language="en"), behave={"mistral": "throw"})
    add("throw_gemini", _a(model="gemini-3.8-flash", language="hy"), behave={"gemini": "throw"})
    add("timeout_env_junk", _a(language="en"), env={"STT_TIMEOUT_MS": "soon"})
    # orders from secrets
    add("fast_order", _a(language="en"), env={"STT_FAST_ORDER": "gpt-4o-transcribe, bogus ,gpt-transcribe,gpt-4o-transcribe"})
    add("best_order", _a(model="best", language="en"), env={"STT_BEST_ORDER": "voxtral-mini-latest,whisper-1"})
    add("fast_order_junk", _a(language="en"), env={"STT_FAST_ORDER": "nope,,x,__proto__,constructor"})
    add("fast_order_empty", _a(language="en"), env={"STT_FAST_ORDER": ""})
    add("best_order_junk", _a(model="best", language="en"), env={"STT_BEST_ORDER": " , ,hasOwnProperty"})
    add("fast_order_not_best", _a(model="best", language="en"), env={"STT_FAST_ORDER": "whisper-1"})
    add("best_order_not_fast", _a(language="en"), env={"STT_BEST_ORDER": "whisper-1"})
    add("pin_then_fast_order", _a(model="gpt-4o-transcribe", language="en"),
        env={"STT_FAST_ORDER": "whisper-1,gpt-transcribe"}, behave={"openai": {"gpt-4o-transcribe": 500, "whisper-1": 500}})
    add("order_voxtral_hy", _a(language="hy"), env={"STT_FAST_ORDER": "voxtral-mini-latest,whisper-1"})
    add("order_only_voxtral_hy", _a(language="hy"), env={"STT_FAST_ORDER": "voxtral-mini-latest"})
    # a language that is not known is never Voxtral's (it could be Armenian): gpt-transcribe starts
    add("multi_none", _a(language="multi"))
    add("multi_empty", _a(language="multi", languages=[]))
    add("multi_one", _a(language="multi", languages=["en"]))
    add("multi_one_hy", _a(language="multi", languages=["hy"]))
    add("auto_optin", _a(language="auto"), env={"STT_AUTO_VOXTRAL": "1"})
    add("default_optin", _a(), env={"STT_AUTO_VOXTRAL": "1"})
    add("multi_none_optin", _a(language="multi"), env={"STT_AUTO_VOXTRAL": "1"})
    add("auto_optin_other", _a(language="auto"), env={"STT_AUTO_VOXTRAL": "yes"})
    add("auto_only_mistral", _a(language="auto"), env=_ONLY_MISTRAL)
    # what the client may learn from a failed attempt: a fixed code, never the provider's text
    add("auth401_all", _a(language="hy"), behave={"openai": "auth401"})
    add("all_fail_429", _a(language="hy"), behave={"openai": 429})
    add("all_fail_403", _a(language="hy"), behave={"openai": 403})
    # one deadline for the whole request
    add("deadline_all_hang", _a(language="en"), behave={"mistral": "hang", "openai": "hang", "gemini": "hang"},
        env={"STT_TIMEOUT_MS": "400", "STT_TOTAL_MS": "900"})
    add("lang_retry_shares_timer", _a(language="hy"), behave={"openai": "lang400hang"},
        env={"STT_TIMEOUT_MS": "400", "STT_FAST_ORDER": "gpt-transcribe"})
    # the request is checked before the quota is spent; size and audio
    add("bad_b64", {"audio": "***not base64***", "language": "en"})
    add("too_big", {"audio": "A" * (24 * 1024 * 1024 + 4), "language": "en"})
    add("too_big_header", _a(language="en"), headers={"Content-Length": str(25 * 1024 * 1024)})
    add("provider_obj", {"audio": _AUDIO, "language": "en", "provider": {"toString": 1}})
    add("provider_list", {"audio": _AUDIO, "language": "en", "provider": ["mistral"]})
    # Object.prototype names are not languages
    add("proto_lang", _a(model="gemini-3.8-flash", language="constructor"))
    add("proto_lang_list", _a(model="gemini-3.8-flash", language="constructor", languages=["toString", "__proto__"]))
    add("proto_multi", _a(model="gemini-3.8-flash", language="multi",
                          languages=["constructor", "hasOwnProperty", "hy", "en"]))
    add("proto_openai", _a(language="constructor"))
    # model names from a secret ("provider:model"); a client's name must be in the catalog
    add("order_extra", _a(language="en"), env={"STT_FAST_ORDER": "openai:gpt-transcribe-2"})
    add("order_extra_bad", _a(language="en"), env={"STT_FAST_ORDER": "openai:bad/../x, evil:gpt-x, openai:, :x"})
    add("order_extra_mistral_en", _a(language="en"), env={"STT_FAST_ORDER": "mistral:voxtral-small-latest"})
    add("order_extra_mistral_hy", _a(language="hy"),
        env={"STT_FAST_ORDER": "mistral:voxtral-small-latest,openai:gpt-transcribe-2"})
    add("client_extra_name", _a(model="gpt-transcribe-2", language="en"))
    add("client_extra_prefixed", _a(model="openai:gpt-transcribe-2", language="en"))
    # Gemini thinking: 2.5 only
    add("gem25_flash", _a(language="en"),
        env=dict(_NOTHING, GOOGLE_STT_KEY="SECRET-google", STT_FAST_ORDER="gemini-2.5-flash"))
    add("gem25_pro", _a(model="best", language="en"),
        env=dict(_NOTHING, GOOGLE_STT_KEY="SECRET-google", STT_BEST_ORDER="gemini-2.5-pro"))
    # ── ElevenLabs Scribe (scribe_v2): first in both profiles when a key exists ──
    for name, body, kw in [
        ("el_default", _a(), {}), ("el_auto", _a(language="auto"), {}), ("el_en", _a(language="en"), {}),
        ("el_hy", _a(language="hy"), {}), ("el_hy_langs", _a(language="hy", languages=["hy"]), {}),
        ("el_multi_hy", _a(language="multi", languages=["hy", "en"]), {}), ("el_multi_none", _a(language="multi"), {}),
        ("el_fast", _a(model="fast", language="en"), {}), ("el_flash", _a(model="gemini-2.5-flash", language="en"), {}),
        ("el_best", _a(model="best", language="en"), {}), ("el_best_hy", _a(model="best", language="hy"), {}),
        ("el_best_auto", _a(model="best"), {}), ("el_pro", _a(model="gemini-2.5-pro", language="hy"), {}),
        ("el_header_best", _a(language="en"), {"headers": {"X-Gusan-Model": "gemini-2.5-pro"}}),
        ("el_pin", _a(model="scribe_v2", language="en"), {}), ("el_pin_hy", _a(model="scribe_v2", language="hy"), {}),
        ("el_pin_header", _a(language="hy"), {"headers": {"X-Gusan-Model": "scribe_v2"}}),
        ("el_pin_other", _a(model="gpt-4o-transcribe", language="en"), {}),
        ("el_pin_other_fails", _a(model="gpt-4o-transcribe", language="en"),
         {"behave": {"openai": {"gpt-4o-transcribe": 500}}}),
        ("el_client_scribe3", _a(model="scribe_v3", language="en"), {}),
        ("el_old_gemini", {"audio": _AUDIO, "language": "en", "provider": "gemini"}, {}),
        ("el_old_hy", {"audio": _AUDIO, "language": "hy", "languages": ["hy"], "provider": "gemini"}, {}),
        ("el_old_mistral", {"audio": _AUDIO, "language": "en", "provider": "mistral"}, {}),
        ("el_old_mistral_fails", {"audio": _AUDIO, "language": "en", "provider": "mistral"},
         {"behave": {"mistral": 500}}),
    ]:
        add(name, body, **dict({"env": _EL}, **kw))
    add("el_pin_nokey", _a(model="scribe_v2", language="en"))             # no key: the pin is skipped
    add("el_pin_nokey_hy", _a(model="scribe_v2", language="hy"))
    # every key name; the first non-empty one wins
    add("el_key_a", _a(language="en"), env={"ELEVENLABS_KEY": "SECRET-e2"})
    add("el_key_b", _a(language="en"), env={"ELEVEN_API_KEY": "SECRET-e3"})
    add("el_key_c", _a(language="en"), env={"XI_API_KEY": "SECRET-e4"})
    add("el_key_first", _a(language="en"), env={"ELEVENLABS_API_KEY": "SECRET-e1", "ELEVENLABS_KEY": "SECRET-e2",
                                                "ELEVEN_API_KEY": "SECRET-e3", "XI_API_KEY": "SECRET-e4"})
    add("el_key_blank_skipped", _a(language="en"),
        env={"ELEVENLABS_API_KEY": "   ", "ELEVENLABS_KEY": "", "XI_API_KEY": "SECRET-e4"})
    add("el_key_blank_only", _a(language="en"), env={"ELEVENLABS_API_KEY": "   "})
    add("el_only_eleven", _a(language="hy"), env=_EL_ONLY)
    add("el_only_eleven_fails", _a(language="hy"), env=_EL_ONLY, behave={"elevenlabs": 500})
    # the request ElevenLabs gets
    add("el_lang_en", _a(language="en"), env=_EL)
    add("el_lang_auto", _a(language="auto"), env=_EL)
    add("el_lang_none", _a(), env=_EL)
    add("el_lang_multi", _a(language="multi", languages=["hy", "en"]), env=_EL)
    add("el_lang_iso3", _a(language="hye"), env=_EL)
    add("el_lang_junk", _a(language="EN"), env=_EL)
    add("el_lang_junk2", _a(language="hy-AM"), env=_EL)
    add("el_lang_proto", _a(language="constructor"), env=_EL)
    add("el_lang_422", _a(language="hy"), env=_EL, behave={"elevenlabs": "lang422"})
    add("el_lang_400", _a(language="hy"), env=_EL, behave={"elevenlabs": "lang400"})
    add("el_lang_plain422", _a(language="en"), env=_EL, behave={"elevenlabs": "plain422"},
        then=[{"behave": {}}])
    add("el_lang_retry_timer", _a(language="hy"), behave={"elevenlabs": "lang422hang"},
        env=dict(_EL, STT_TIMEOUT_MS="400", STT_FAST_ORDER="scribe_v2"))
    # failover: a failing ElevenLabs hands over to the next engine of the profile; an unhealthy one is skipped briefly
    _OUT = [{"behave": {}}, {"advance_ms": 31000, "behave": {}}]      # next request, then 31 s later
    _BAD = [{"behave": {}}, {"advance_ms": 61000, "behave": {}}]      # next request, then 61 s later
    add("el_500", _a(language="en"), env=_EL, behave={"elevenlabs": 500}, then=_OUT)
    add("el_500_hy", _a(language="hy"), env=_EL, behave={"elevenlabs": 502})
    add("el_500_best", _a(model="best", language="en"), env=_EL, behave={"elevenlabs": 500})
    add("el_hang", _a(language="en"), behave={"elevenlabs": "hang"}, env=dict(_EL, STT_TIMEOUT_MS="150"),
        then=_OUT)
    add("el_throw", _a(language="en"), behave={"elevenlabs": "throw"}, env=_EL, then=_OUT)
    add("el_multichannel", _a(language="en"), behave={"elevenlabs": "multichannel"}, env=_EL, then=_BAD)
    add("el_badjson", _a(language="en"), behave={"elevenlabs": "badjson"}, env=_EL, then=_BAD)
    add("el_number_text", _a(language="en"), behave={"elevenlabs": "number_text"}, env=_EL, then=_BAD)
    add("el_outage_env", _a(language="en"), env=dict(_EL, STT_ELEVEN_OUTAGE_PAUSE_MS="5000"),
        behave={"elevenlabs": 503}, then=[{"advance_ms": 4000, "behave": {}}, {"advance_ms": 2000, "behave": {}}])
    add("el_outage_only", _a(language="hy"), env=_EL_ONLY, behave={"elevenlabs": 500},
        then=[{"behave": {}}, {"advance_ms": 31000, "behave": {}}])
    add("el_empty", _a(language="en"), behave={"elevenlabs": "empty"}, env=_EL)
    add("el_credits_in_a_5xx", _a(language="en"), behave={"elevenlabs": "credits_words500"}, env=_EL,
        then=_OUT)
    add("el_all_fail", _a(language="en"), env=_EL,
        behave={"elevenlabs": 500, "mistral": 500, "openai": 503, "gemini": 500})
    # the credits breaker
    add("el_c402", _a(language="en"), env=_EL, behave={"elevenlabs": "credits402"},
        then=[{"behave": {}}, {"advance_ms": 14 * 60000, "behave": {}}, {"advance_ms": 2 * 60000, "behave": {}},
              {"behave": {}}])
    add("el_after_pause_probe", _a(language="en"), env=_EL)           # a new scenario: a clean breaker
    for name, b in [("el_c402_plain", 402), ("el_c401", "invalid401"), ("el_c403", "perm403"),
                    ("el_c401_plain", 401), ("el_cquota401", "quota401"), ("el_cquota400", "quota400")]:
        add(name, _a(language="en"), env=_EL, behave={"elevenlabs": b},
            then=[{"behave": {}}, {"advance_ms": 16 * 60000, "behave": {}}])
    add("el_c429", _a(language="en"), env=_EL, behave={"elevenlabs": "busy429"},
        then=[{"behave": {}}, {"advance_ms": 14000, "behave": {}}, {"advance_ms": 2000, "behave": {}}])
    add("el_c429_plain", _a(language="en"), env=_EL, behave={"elevenlabs": 429},
        then=[{"behave": {}}, {"advance_ms": 16000, "behave": {}}])
    add("el_c429_short_env", _a(language="en"), env=dict(_EL, STT_ELEVEN_PAUSE_MS="5000"),
        behave={"elevenlabs": "busy429"},
        then=[{"advance_ms": 4000, "behave": {}}, {"advance_ms": 2000, "behave": {}}])
    add("el_pause_env", _a(language="en"), env=dict(_EL, STT_ELEVEN_PAUSE_MS="60000"),
        behave={"elevenlabs": "credits402"},
        then=[{"advance_ms": 59000, "behave": {}}, {"advance_ms": 2000, "behave": {}}])
    for name, junk in [("el_pause_junk", "soon"), ("el_pause_zero", "0"), ("el_pause_negative", "-5")]:
        add(name, _a(language="en"), env=dict(_EL, STT_ELEVEN_PAUSE_MS=junk), behave={"elevenlabs": 402},
            then=[{"advance_ms": 14 * 60000, "behave": {}}, {"advance_ms": 2 * 60000, "behave": {}}])
    add("el_c402_only", _a(language="hy"), env=_EL_ONLY, behave={"elevenlabs": "credits402"},
        then=[{"behave": {}}, {"advance_ms": 16 * 60000, "behave": {}}])
    add("el_c401_only", _a(language="hy"), env=_EL_ONLY, behave={"elevenlabs": "invalid401"},
        then=[{"behave": {}}])
    add("el_c429_only", _a(language="hy"), env=_EL_ONLY, behave={"elevenlabs": "busy429"},
        then=[{"behave": {}}, {"advance_ms": 16000, "behave": {}}])
    add("el_c402_pinned", _a(model="scribe_v2", language="en"), env=_EL, behave={"elevenlabs": "credits402"},
        then=[{"behave": {}}])
    add("el_c402_hy", _a(language="hy"), env=_EL, behave={"elevenlabs": "credits402"}, then=[{"behave": {}}])
    add("el_c402_best", _a(model="best", language="en"), env=_EL, behave={"elevenlabs": "credits402"},
        then=[{"behave": {}}])
    add("el_c402_in_the_middle", _a(language="en"), env=dict(_EL, STT_FAST_ORDER="voxtral-mini-latest,scribe_v2"),
        behave={"mistral": 500, "elevenlabs": "credits402"}, then=[{"behave": {"mistral": 500}}])
    # orders from secrets with ElevenLabs in them
    add("el_order_drop", _a(language="en"), env=dict(_EL, STT_FAST_ORDER="voxtral-mini-latest,gpt-transcribe"))
    add("el_order_last", _a(language="en"), env=dict(_EL, STT_FAST_ORDER="gpt-4o-transcribe,scribe_v2"))
    add("el_order_last_fails", _a(language="en"), env=dict(_EL, STT_FAST_ORDER="gpt-4o-transcribe,scribe_v2"),
        behave={"openai": 500})
    add("el_order_scribe3", _a(language="en"), env=dict(_EL, STT_FAST_ORDER="elevenlabs:scribe_v3,voxtral-mini-latest"))
    add("el_order_scribe3_best", _a(model="best", language="en"),
        env=dict(_EL, STT_BEST_ORDER="elevenlabs:scribe_v3"))
    add("el_order_bad_kind", _a(language="en"),
        env=dict(_EL, STT_FAST_ORDER="eleven:scribe_v2,elevenlabs:bad/../x,elevenlabs:"))
    add("el_order_best_drop", _a(model="best", language="en"), env=dict(_EL, STT_BEST_ORDER="gpt-transcribe"))
    add("el_order_best_drop_fast", _a(language="en"), env=dict(_EL, STT_BEST_ORDER="gpt-transcribe"))
    add("el_order_only_scribe_nokey", _a(language="en"), env={"STT_FAST_ORDER": "scribe_v2"})
    # its timeout: at least 8 s, unless STT_TIMEOUT_MS fixes every attempt
    add("el_timeout_default", _a(language="en"), env=_EL, behave={"elevenlabs": 500})
    add("el_timeout_long", {"audio": "A" * 3000000, "language": "en"}, env=_EL, behave={"elevenlabs": 500})
    add("el_timeout_fixed", _a(language="en"), env=dict(_EL, STT_TIMEOUT_MS="150"))
    add("el_timeout_total_short", _a(language="en"), env=dict(_EL, STT_TOTAL_MS="3000"))
    # auth, quota, input (as before)
    add("not_pro", state={"pro": False})
    add("over_cap", state={"under": False})
    add("no_audio", {"language": "hy"})
    add("bad_json", "{nope")
    add("no_user", state={"user": False})
    add("no_auth", noauth=True)
    add("options", method="OPTIONS")
    add("get", method="GET")
    return s


@unittest.skipUnless(_node_can_strip_types(), "needs Node 22.6+ (type stripping) to run the proxy")
class TestProxyBehaviour(unittest.TestCase):
    """The real function, fetch and Supabase faked: what a request for each model does."""

    @classmethod
    def setUpClass(cls):
        src = _source()
        cls.tmp = tempfile.mkdtemp(prefix="gusan-proxy-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        stub = os.path.join(cls.tmp, "stub_supabase.mjs")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(STUB_SUPABASE)
        patched = re.sub(r'from "https://esm\.sh/@supabase/supabase-js@2"', 'from "./stub_supabase.mjs"', src)
        assert patched != src
        module = os.path.join(cls.tmp, "proxy.mts")
        with open(module, "w", encoding="utf-8") as f:
            f.write(patched)
        harness = os.path.join(cls.tmp, "harness.mjs")
        with open(harness, "w", encoding="utf-8") as f:
            f.write(HARNESS)
        scenarios = os.path.join(cls.tmp, "scenarios.json")
        with open(scenarios, "w", encoding="utf-8") as f:
            json.dump(_scenarios(), f)
        p = subprocess.run([_node_can_strip_types(), "--no-warnings", "--experimental-strip-types", harness,
                            pathlib.Path(module).as_uri(), scenarios],     # file:///C:/... and file:///tmp/...
                           capture_output=True, text=True, timeout=120, encoding="utf-8")
        assert p.returncode == 0, p.stderr[-800:]
        cls.R = json.loads(p.stdout)

    # helpers: which engines were contacted, in order
    def models(self, name):
        return [c["model"] for c in self.R[name]["calls"]]

    def ran(self, name, model, status=200):
        r = self.R[name]
        self.assertEqual(r["status"], status, name)
        self.assertEqual((r["json"]["model"], r["model_header"]), (model, model), name)
        self.assertEqual(r["json"]["text"], model + " words", name)           # the text is trimmed

    def urls(self, name):
        return [c["url"] for c in self.R[name]["calls"]]

    # ── which engine answers first ───────────────────────────────────────────────
    def test_the_default_request_is_fastest_first_voxtral(self):
        for name in ("en", "multi_covered", "multi_one", "flash", "fast", "old_gemini_en", "old_mistral_fr",
                     "auto_optin", "default_optin"):
            self.ran(name, "voxtral-mini-latest")
            self.assertEqual(self.models(name), ["voxtral-mini-latest"], name)
            self.assertEqual(self.urls(name), ["https://api.mistral.ai/v1/audio/transcriptions"], name)
        self.assertEqual(self.R["default"]["json"]["lang"], "en")
        self.assertEqual(self.R["en"]["json"]["lang"], "en")

    def test_a_language_that_is_not_known_never_starts_on_voxtral(self):
        # "auto", "multi" with no list or an empty one: it could be Armenian, and Voxtral's confident
        # answer in another script cannot be caught afterwards - gpt-transcribe (it detects) starts.
        for name in ("default", "auto", "old_only_audio", "old_odd", "multi_none", "multi_empty",
                     "multi_one_hy", "auto_optin_other", "multi_none_optin"):
            self.ran(name, "gpt-transcribe")
            self.assertEqual(self.models(name), ["gpt-transcribe"], name)
            self.assertNotIn("mistral", [c["provider"] for c in self.R[name]["calls"]], name)
        self.assertEqual(self.R["default"]["json"]["lang"], "en")
        # ... unless the founder asked for Voxtral-first on "auto" (a secret, no redeploy); a mix with
        # no list stays off Voxtral either way.
        self.assertEqual(self.models("auto_optin"), ["voxtral-mini-latest"])
        # Only Voxtral configured + "auto": nothing may answer (the 502 stt_failed the app expects, no quota spent).
        r = self.R["auto_only_mistral"]
        self.assertEqual((r["status"], r["json"]["detail"]), (502, "no engine for this language"))
        self.assertEqual((r["calls"], r["quota"]), ([], 0))

    def test_armenian_goes_to_gpt_transcribe_and_voxtral_is_never_contacted(self):
        for name in ("hy", "hy_langs", "multi_hy", "bad_hy", "old_gemini_hy", "old_mistral_hy", "pin_voxtral_hy",
                     "pro"):
            self.ran(name, "gpt-transcribe")
            self.assertEqual(self.models(name), ["gpt-transcribe"], name)
            self.assertEqual(self.urls(name), ["https://api.openai.com/v1/audio/transcriptions"], name)
        self.assertEqual(self.R["hy"]["json"]["lang"], "hy")
        self.assertEqual(self.R["multi_hy"]["json"]["lang"], "multi")

    def test_best_and_gemini_pro_run_the_best_profile(self):
        for name in ("best", "pro", "pro_en", "header_best", "old_mistral_best"):
            self.ran(name, "gpt-transcribe")
            self.assertEqual(self.models(name), ["gpt-transcribe"], name)
            self.assertTrue(all("generativelanguage" not in u for u in self.urls(name)), name)

    def test_a_pinned_engine_goes_first(self):
        self.ran("pin_4o", "gpt-4o-transcribe")
        self.ran("pin_mini", "gpt-4o-mini-transcribe")
        self.ran("pin_whisper", "whisper-1")
        self.ran("pin_gpt", "gpt-transcribe")
        self.ran("pin_voxtral", "voxtral-mini-latest")
        self.ran("header_pin", "gpt-4o-transcribe")
        self.ran("old_mistral_pin", "gpt-transcribe")               # a pinned model beats the old provider
        for name in ("pin_4o", "pin_mini", "pin_whisper", "pin_gpt", "pin_voxtral", "header_pin",
                     "old_mistral_pin"):
            self.assertEqual(len(self.models(name)), 1, name)

    def test_a_pinned_gemini_runs_with_its_own_thinking_budget(self):
        r = self.R["pin_gemini_flash3"]
        self.ran("pin_gemini_flash3", "gemini-3.8-flash")
        self.assertIn("models/gemini-3.8-flash:generateContent", r["calls"][0]["url"])
        self.assertIn("key=SECRET-google", r["calls"][0]["url"])
        self.assertIsNone(r["calls"][0]["budget"])                          # a 3.x model takes no thinkingConfig
        self.assertIn("Armenian", r["calls"][0]["prompt"])                 # the language hint is kept
        self.ran("pin_gemini_pro3", "gemini-3.1-pro-preview")
        self.assertIsNone(self.R["pin_gemini_pro3"]["calls"][0]["budget"])
        # 2.5: Flash thinks 0, Pro its smallest valid budget.
        self.ran("gem25_flash", "gemini-2.5-flash")
        self.assertEqual(self.R["gem25_flash"]["calls"][0]["budget"], 0)
        self.ran("gem25_pro", "gemini-2.5-pro")
        self.assertEqual(self.R["gem25_pro"]["calls"][0]["budget"], 128)

    def test_a_failing_pinned_engine_falls_to_the_fast_order(self):
        self.ran("pin_4o_fails", "voxtral-mini-latest")
        self.assertEqual(self.models("pin_4o_fails"), ["gpt-4o-transcribe", "voxtral-mini-latest"])
        self.ran("pin_then_fast_order", "gpt-transcribe")
        self.assertEqual(self.models("pin_then_fast_order"), ["gpt-4o-transcribe", "whisper-1", "gpt-transcribe"])

    def test_an_unknown_or_hostile_model_is_the_fast_profile_and_never_in_a_url(self):
        for i in range(13):
            name = "bad_%d" % i
            self.ran(name, "voxtral-mini-latest")
            self.assertEqual(self.models(name), ["voxtral-mini-latest"], name)
        joined = json.dumps([self.urls("bad_%d" % i) for i in range(13)])
        for evil in ("evil", "ultra", "__proto__", "constructor", "toString"):
            self.assertNotIn(evil, joined)
        self.ran("header_bad", "voxtral-mini-latest")
        self.assertNotIn("evil", json.dumps(self.urls("header_bad")))
        self.assertEqual(self.R["bad_hy"]["json"]["model"], "gpt-transcribe")

    def test_the_header_is_used_when_the_body_names_no_model_and_the_body_wins(self):
        self.ran("body_wins", "voxtral-mini-latest")                       # body "fast" over header 4o
        self.ran("body_wins_best", "gpt-transcribe")
        self.assertEqual(self.models("body_wins_best"), ["gpt-transcribe"])
        self.ran("header_when_body_not_string", "gpt-transcribe")          # body 5 -> header "best"

    def test_old_clients_still_get_a_200(self):
        for name in ("old_gemini_en", "old_gemini_hy", "old_mistral_fr", "old_mistral_hy", "old_odd",
                     "old_only_audio", "old_mistral_best", "old_mistral_pin"):
            r = self.R[name]
            self.assertEqual(r["status"], 200, name)
            self.assertEqual(set(r["json"]), {"text", "lang", "model"}, name)   # no field an old app dislikes
            self.assertTrue(r["json"]["text"], name)
        self.assertEqual(self.R["old_odd"]["json"]["lang"], "en")             # odd values are ignored, not errors

    # ── the language sent to OpenAI ───────────────────────────────────────────────
    def test_openai_gets_a_language_only_for_one_explicit_iso_code(self):
        want = {"lang_none": None, "lang_auto": None, "lang_multi": None, "lang_iso3": "hye",
                "lang_junk": None, "lang_junk2": None, "pin_gpt": None, "hy": "hy", "pin_4o": "en"}
        for name, language in want.items():
            call = self.R[name]["calls"][0]
            self.assertEqual(call["provider"], "openai", name)
            self.assertEqual(call["language"], language, name)

    def test_the_openai_request_has_no_prompt_and_asks_for_json(self):
        call = self.R["hy"]["calls"][0]
        self.assertEqual(sorted(call["fields"]), sorted(["model", "response_format", "file", "language"]))
        self.assertEqual(self.R["lang_none"]["calls"][0]["fields"], ["model", "response_format", "file"])
        self.assertEqual(call["auth"], "Bearer SECRET-openai")

    def test_a_refused_language_is_retried_once_without_it(self):
        self.ran("lang_400", "gpt-transcribe")
        calls = self.R["lang_400"]["calls"]
        self.assertEqual([(c["model"], c["language"]) for c in calls], [("gpt-transcribe", "hy"), ("gpt-transcribe", None)])

    def test_another_400_is_not_retried_and_moves_on(self):
        r = self.R["other_400"]
        self.assertEqual((r["status"], r["json"]["error"]), (502, "stt_failed"))
        self.assertEqual(self.models("other_400"), ["gpt-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"])

    # ── failover ─────────────────────────────────────────────────────────────────
    def test_a_failing_first_engine_hands_over_to_the_next(self):
        for name in ("voxtral_500", "voxtral_429"):
            self.ran(name, "gpt-transcribe")
            self.assertEqual(self.models(name), ["voxtral-mini-latest", "gpt-transcribe"], name)
        self.assertEqual(self.R["voxtral_500"]["json"]["lang"], "en")

    def test_a_missing_key_skips_the_engine(self):
        self.ran("openai_missing_en", "voxtral-mini-latest")
        self.assertEqual(self.models("openai_missing_en"), ["voxtral-mini-latest"])
        self.ran("only_openai_en", "gpt-transcribe")                      # no Mistral key: not even tried
        self.assertEqual(self.models("only_openai_en"), ["gpt-transcribe"])
        # With no OpenAI key a 429 from Voxtral has nowhere to go.
        r = self.R["voxtral_429_noopenai"]
        self.assertEqual((r["status"], r["json"]["error"]), (502, "stt_failed"))
        self.assertEqual(self.models("voxtral_429_noopenai"), ["voxtral-mini-latest"])

    def test_every_openai_key_name_works_and_the_first_wins(self):
        self.ran("key_api_key", "gpt-transcribe")
        self.assertEqual(self.R["key_api_key"]["calls"][0]["auth"], "Bearer SECRET-openai2")
        self.ran("key_key", "gpt-transcribe")
        self.assertEqual(self.R["key_key"]["calls"][0]["auth"], "Bearer SECRET-openai3")
        self.assertEqual(self.R["key_prefers_token"]["calls"][0]["auth"], "Bearer SECRET-openai")

    def test_voxtral_is_never_the_answer_for_a_language_it_does_not_cover(self):
        # Armenian sent to Voxtral comes back as confident nonsense in another script (seen live).
        for name in ("openai_missing_hy", "openai_missing_hy_only_mistral", "openai_missing_best_hy",
                     "order_only_voxtral_hy"):
            r = self.R[name]
            # A permanent condition: the 502 stt_failed the app expects, and it spent no quota.
            self.assertEqual((r["status"], r["json"]["error"]), (502, "stt_failed"), name)
            self.assertEqual(r["json"]["detail"], "no engine for this language", name)
            self.assertEqual(r["calls"], [], name)                         # nobody was contacted at all
            self.assertEqual(r["quota"], 0, name)
        # Mistral + Google configured, no OpenAI: Gemini (not in a default order) is not a stand-in.
        self.assertEqual(self.R["openai_missing_hy"]["status"], 502)
        # Voxtral listed first in an order, Armenian: skipped, the next one answers.
        self.ran("order_voxtral_hy", "whisper-1")
        self.assertEqual(self.models("order_voxtral_hy"), ["whisper-1"])
        for name in ("hy", "hy_langs", "multi_hy", "pro", "bad_hy", "old_mistral_hy", "pin_voxtral_hy",
                     "all_fail_hy", "lang_400", "other_400", "hang_openai_hy", "pin_gemini_flash3"):
            self.assertNotIn("mistral", [c["provider"] for c in self.R[name]["calls"]], name)

    def test_every_engine_failing_is_a_502_with_a_fixed_code_for_the_last_answer(self):
        r = self.R["all_fail_en"]
        self.assertEqual((r["status"], r["json"]["error"]), (502, "stt_failed"))
        self.assertEqual(self.models("all_fail_en"), _FAST4)
        self.assertEqual(r["json"]["detail"], "provider_unavailable")      # a code for the last answer
        self.assertIsNone(r["model_header"])
        r = self.R["all_fail_hy"]
        self.assertEqual((r["status"], r["json"]["error"]), (502, "stt_failed"))
        self.assertEqual(self.models("all_fail_hy"), ["gpt-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"])
        self.assertEqual(r["json"]["detail"], "provider_unavailable")
        self.assertNotIn("err-50", r["raw"])                                # never the provider's own text

    def test_whisper_is_a_backup_only_when_an_order_names_it(self):
        self.ran("whisper_last", "whisper-1")
        self.assertEqual(self.models("whisper_last"), ["voxtral-mini-latest", "whisper-1"])
        self.assertTrue(all("whisper-1" not in self.models(n) for n in ("all_fail_en", "voxtral_500", "default")))

    def test_nothing_configured_is_a_503(self):
        for name in ("nothing_configured", "nothing_configured_hy"):
            r = self.R[name]
            self.assertEqual((r["status"], r["json"]), (503, {"error": "stt_not_configured"}), name)
            self.assertEqual(r["calls"], [], name)

    def test_gemini_still_works_when_an_order_names_it(self):
        self.ran("only_google_in_order", "gemini-3.8-flash")
        self.ran("only_gemini_key_name", "gemini-2.5-flash")
        self.assertIn("key=SECRET-gem", self.R["only_gemini_key_name"]["calls"][0]["url"])
        # ... but a default order does not use it: only a Gemini key = nothing configured for the default.
        self.assertEqual(self.R["only_google_default"]["status"], 503)
        self.assertEqual(self.R["only_google_default"]["calls"], [])

    # ── timeouts ─────────────────────────────────────────────────────────────────
    def test_a_hanging_engine_is_cut_and_the_next_one_answers(self):
        self.ran("hang_first", "gpt-transcribe")
        self.assertEqual(self.models("hang_first"), ["voxtral-mini-latest", "gpt-transcribe"])
        self.assertLess(self.R["hang_first"]["ms"], 5000)
        self.assertGreaterEqual(self.R["hang_first"]["ms"], 100)             # it did wait for the timeout
        self.assertTrue(any('"stt_fail":"mistral"' in line and '"detail":"timeout"' in line
                            for kind, line in self.R["hang_first"]["logs"] if kind == "error"))
        self.ran("hang_openai_hy", "gpt-4o-mini-transcribe")
        self.ran("hang_gemini_then_openai", "gpt-transcribe")
        self.assertEqual(self.models("hang_gemini_then_openai"), ["gemini-3.8-flash", "gpt-transcribe"])
        self.assertTrue(all(c["has_signal"] for r in self.R.values() for c in r["calls"]))   # every attempt

    def test_everything_hanging_ends_in_a_502_not_a_hang(self):
        r = self.R["hang_all"]
        self.assertEqual((r["status"], r["json"]["error"], r["json"]["detail"]), (502, "stt_failed", "timeout"))
        self.assertEqual(self.models("hang_all"), _FAST4)
        self.assertLess(r["ms"], 5000)

    def test_a_network_error_is_a_failure_and_its_text_is_not_leaked(self):
        self.ran("throw_first", "gpt-transcribe")
        self.assertEqual(self.models("throw_first"), ["voxtral-mini-latest", "gpt-transcribe"])
        self.ran("throw_gemini", "gpt-transcribe")
        errs = [line for kind, line in self.R["throw_gemini"]["logs"] if kind == "error"]
        self.assertEqual(len(errs), 1)
        self.assertIn('"detail":"network_error"', errs[0])
        self.assertNotIn("SECRET", errs[0])                                 # the thrown text carried the URL

    def test_a_junk_timeout_secret_is_ignored(self):
        self.ran("timeout_env_junk", "voxtral-mini-latest")

    # ── orders from secrets ──────────────────────────────────────────────────────
    def test_orders_from_secrets_are_honoured_and_sanitised(self):
        self.ran("fast_order", "gpt-4o-transcribe")                       # unknown dropped, duplicate dropped
        self.assertEqual(self.models("fast_order"), ["gpt-4o-transcribe"])
        self.ran("best_order", "voxtral-mini-latest")
        self.assertEqual(self.models("best_order"), ["voxtral-mini-latest"])
        for name in ("fast_order_junk", "fast_order_empty"):              # nothing valid = the default
            self.ran(name, "voxtral-mini-latest")
        self.ran("best_order_junk", "gpt-transcribe")
        # The FAST secret does not change BEST and the other way round.
        self.ran("fast_order_not_best", "gpt-transcribe")
        self.ran("best_order_not_fast", "voxtral-mini-latest")

    # ── the answer, the logs ─────────────────────────────────────────────────────
    def test_the_model_header_always_equals_the_model_in_the_body(self):
        seen = 0
        for name, r in self.R.items():
            if r["status"] == 200 and r["json"] and "model" in r["json"]:
                seen += 1
                self.assertEqual(r["model_header"], r["json"]["model"], name)
                self.assertEqual(r["model_header"], r["calls"][-1]["model"], name)   # the one that REALLY ran
            else:
                self.assertIsNone(r["model_header"], name)
        self.assertGreater(seen, 60)

    def test_keys_never_appear_in_logs_or_answers(self):
        for name, top in self.R.items():
            for r in [top] + top.get("then", []):                          # a scenario's later requests too
                blob = json.dumps([r["raw"], r["logs"], r["model_header"]])
                self.assertNotIn("SECRET", blob, name)
                self.assertNotIn("sk_0123", blob, name)                    # an ElevenLabs-style key in an answer
                self.assertNotIn(_AUDIO, blob, name)                       # nor the audio

    def test_a_success_logs_one_line_with_the_model_the_time_and_the_attempts(self):
        def ok_lines(name):
            out = []
            for kind, line in self.R[name]["logs"]:
                if kind == "log":
                    out.append(json.loads(line))
            return out
        one = ok_lines("en")
        self.assertEqual(len(one), 1)
        self.assertEqual(set(one[0]), {"stt_ok", "ms", "tried"})
        self.assertEqual((one[0]["stt_ok"], one[0]["tried"]), ("voxtral-mini-latest", 1))
        self.assertIsInstance(one[0]["ms"], int)
        two = ok_lines("voxtral_500")
        self.assertEqual((len(two), two[0]["stt_ok"], two[0]["tried"]), (1, "gpt-transcribe", 2))
        self.assertEqual(ok_lines("all_fail_en"), [])                       # nothing succeeded
        for name, r in self.R.items():                                      # no transcript in any log
            self.assertNotIn(" words", json.dumps(r["logs"]), name)

    def test_a_failed_attempt_logs_provider_model_status_and_a_short_detail(self):
        errs = [json.loads(line) for kind, line in self.R["voxtral_500"]["logs"] if kind == "error"]
        self.assertEqual(errs, [{"stt_fail": "mistral", "model": "voxtral-mini-latest", "status": 500,
                                 "detail": "err-500"}])

    def test_the_client_only_gets_a_fixed_code_never_the_providers_words(self):
        r = self.R["auth401_all"]
        self.assertEqual((r["status"], r["json"]["error"], r["json"]["detail"]), (502, "stt_failed", "provider_auth"))
        self.assertEqual(self.R["all_fail_429"]["json"]["detail"], "rate_limited")
        self.assertEqual(self.R["all_fail_403"]["json"]["detail"], "provider_auth")
        self.assertEqual(self.R["other_400"]["json"]["detail"], "provider_rejected")
        self.assertEqual(self.R["hang_all"]["json"]["detail"], "timeout")
        for name in ("auth401_all", "all_fail_429", "other_400", "all_fail_en"):
            self.assertNotIn("err-", self.R[name]["raw"], name)
        # The 401 text names the end of the key and the account; neither reaches the answer or the log.
        blob = json.dumps([r["raw"], r["logs"]])
        for secret in ("sk-proj", "abcd", "org-ABC123XYZ", "proj_Q9secret"):
            self.assertNotIn(secret, blob, secret)
        self.assertTrue(any('"status":401' in line for kind, line in r["logs"] if kind == "error"))
        self.assertTrue(any("[key]" in line for kind, line in r["logs"] if kind == "error"))

    def test_the_whole_request_has_one_deadline(self):
        r = self.R["deadline_all_hang"]            # 400 ms per attempt, 900 ms for everything, all hang
        self.assertEqual((r["status"], r["json"]["error"], r["json"]["detail"]), (502, "stt_failed", "timeout"))
        self.assertEqual(len(r["calls"]), 2)       # a third would start with 100 ms left: not tried
        self.assertGreaterEqual(r["ms"], 700)
        self.assertLess(r["ms"], 1400)
        # The OpenAI language retry shares its attempt's timer: it cannot double it.
        r = self.R["lang_retry_shares_timer"]
        self.assertEqual([c["language"] for c in r["calls"]], ["hy", None])
        self.assertEqual((r["status"], r["json"]["detail"]), (502, "timeout"))
        self.assertGreaterEqual(r["ms"], 350)
        self.assertLess(r["ms"], 700)

    def test_a_request_that_can_never_succeed_spends_no_quota(self):
        for name in ("nothing_configured", "nothing_configured_hy", "auto_only_mistral", "bad_b64", "bad_json",
                     "no_audio", "too_big", "too_big_header", "openai_missing_hy"):
            self.assertEqual(self.R[name]["quota"], 0, name)
            self.assertEqual(self.R[name]["calls"], [], name)
        self.assertEqual(self.R["default"]["quota"], 1)
        self.assertEqual(self.R["all_fail_en"]["quota"], 1)             # one unit per request, however many engines
        self.assertEqual(self.R["over_cap"]["status"], 429)

    def test_audio_that_is_too_big_or_not_base64_is_refused_up_front(self):
        for name in ("too_big", "too_big_header"):
            r = self.R[name]
            self.assertEqual((r["status"], r["json"]), (413, {"error": "audio_too_large"}), name)
        r = self.R["bad_b64"]
        self.assertEqual((r["status"], r["json"]), (400, {"error": "bad_request", "detail": "invalid_audio"}))
        for name in ("provider_obj", "provider_list"):           # a provider that is not a string is the default
            self.ran(name, "voxtral-mini-latest")

    def test_object_prototype_names_are_not_languages(self):
        for name in ("proto_lang", "proto_lang_list", "proto_multi"):
            self.ran(name, "gemini-3.8-flash")
            prompt = self.R[name]["calls"][0]["prompt"]
            for junk in ("function", "native code", "[object", "undefined", "Object"):
                self.assertNotIn(junk, prompt, (name, junk))
        self.assertIn("Armenian", self.R["proto_multi"]["calls"][0]["prompt"])     # the real ones stay
        self.assertNotIn("constructor", self.R["proto_multi"]["calls"][0]["prompt"])
        self.assertIsNone(self.R["proto_openai"]["calls"][0]["language"])          # not a 2-3 letter code

    def test_a_secret_may_name_a_model_but_a_client_may_not(self):
        self.ran("order_extra", "gpt-transcribe-2")                  # openai:gpt-transcribe-2 from the secret
        self.assertEqual(self.models("order_extra"), ["gpt-transcribe-2"])
        self.assertEqual(self.urls("order_extra"), ["https://api.openai.com/v1/audio/transcriptions"])
        # Bad entries are dropped (and logged by name, no key): the default order runs.
        self.ran("order_extra_bad", "voxtral-mini-latest")
        dropped = [json.loads(line) for kind, line in self.R["order_extra_bad"]["logs"]
                   if kind == "error" and "stt_order_dropped" in line]
        self.assertEqual(len(dropped), 4)
        self.assertTrue(all(d["stt_order_dropped"] == "STT_FAST_ORDER" for d in dropped))
        # A mistral model from a secret follows the same language rule as Voxtral.
        self.ran("order_extra_mistral_en", "voxtral-small-latest")
        self.assertEqual(self.urls("order_extra_mistral_en"), ["https://api.mistral.ai/v1/audio/transcriptions"])
        self.ran("order_extra_mistral_hy", "gpt-transcribe-2")
        self.assertEqual(self.models("order_extra_mistral_hy"), ["gpt-transcribe-2"])
        # A client naming a model outside the catalog, even in "provider:model" form, is the FAST profile.
        for name in ("client_extra_name", "client_extra_prefixed"):
            self.ran(name, "voxtral-mini-latest")
            self.assertEqual(self.models(name), ["voxtral-mini-latest"], name)
            self.assertNotIn("transcribe-2", json.dumps(self.urls(name)), name)

    # ── ElevenLabs Scribe (scribe_v2) ────────────────────────────────────────────
    ELEVEN = "https://api.elevenlabs.io/v1/speech-to-text"

    def ran_in(self, r, model, label=""):
        self.assertEqual(r["status"], 200, label)
        self.assertEqual((r["json"]["model"], r["model_header"]), (model, model), label)
        self.assertEqual(r["json"]["text"], model + " words", label)

    def names(self, r):
        return [c["model"] for c in r["calls"]]

    def eleven_calls(self, r):
        return [c for c in r["calls"] if c["provider"] == "elevenlabs"]

    def pauses(self, r):
        return [json.loads(line) for kind, line in r["logs"] if kind == "error" and "stt_pause" in line]

    def fails(self, r):
        return [json.loads(line) for kind, line in r["logs"] if kind == "error" and "stt_fail" in line]

    def timeline(self, name):
        return [self.R[name]] + self.R[name].get("then", [])

    def test_scribe_is_first_in_both_profiles_when_a_key_exists(self):
        for name in ("el_default", "el_auto", "el_en", "el_hy", "el_hy_langs", "el_multi_hy", "el_multi_none",
                     "el_fast", "el_flash", "el_best", "el_best_hy", "el_best_auto", "el_pro", "el_header_best",
                     "el_pin", "el_pin_hy", "el_pin_header", "el_old_gemini", "el_old_hy", "el_client_scribe3"):
            self.ran(name, _SCRIBE)                       # any language, Armenian too: it detects the language
            self.assertEqual(self.models(name), [_SCRIBE], name)
            self.assertEqual(self.urls(name), [self.ELEVEN], name)
            self.assertEqual(self.R[name]["quota"], 1, name)
        self.assertEqual(self.R["el_hy"]["json"]["lang"], "hy")           # the answer's `lang` is as before
        self.assertEqual(self.R["el_default"]["json"]["lang"], "en")
        for name in ("el_old_gemini", "el_old_hy", "el_default"):          # no field an old app dislikes
            self.assertEqual(set(self.R[name]["json"]), {"text", "lang", "model"}, name)
        # a client naming a Scribe version we do not list gets the profile, never its own model_id
        self.assertEqual(self.models("el_client_scribe3"), [_SCRIBE])
        self.assertNotIn("scribe_v3", json.dumps(self.R["el_client_scribe3"]["calls"]))

    def test_the_other_pins_and_the_old_mistral_client_keep_their_place(self):
        self.ran("el_pin_other", "gpt-4o-transcribe")                     # a pin beats the profile
        self.assertEqual(self.models("el_pin_other"), ["gpt-4o-transcribe"])
        self.ran("el_pin_other_fails", _SCRIBE)                           # ... then the profile: Scribe first
        self.assertEqual(self.models("el_pin_other_fails"), ["gpt-4o-transcribe", _SCRIBE])
        self.ran("el_old_mistral", "voxtral-mini-latest")                 # provider "mistral": Voxtral first, as before
        self.ran("el_old_mistral_fails", _SCRIBE)
        self.assertEqual(self.models("el_old_mistral_fails"), ["voxtral-mini-latest", _SCRIBE])

    def test_without_a_key_scribe_is_skipped_and_nothing_else_changes(self):
        for name, r in self.R.items():                      # no scenario without a key may contact ElevenLabs
            if not name.startswith("el_"):
                self.assertEqual(self.eleven_calls(r), [], name)
        self.ran("el_pin_nokey", "voxtral-mini-latest")
        self.assertEqual(self.models("el_pin_nokey"), ["voxtral-mini-latest"])
        self.ran("el_pin_nokey_hy", "gpt-transcribe")
        self.ran("el_key_blank_only", "voxtral-mini-latest")              # a blank key is no key
        r = self.R["el_order_only_scribe_nokey"]                           # only Scribe named, no key: not configured
        self.assertEqual((r["status"], r["json"]), (503, {"error": "stt_not_configured"}))
        self.assertEqual((r["calls"], r["quota"]), ([], 0))
        # The old expectations of the default routes still hold (same names as the tests above).
        for name in ("en", "multi_covered", "fast", "old_gemini_en"):
            self.ran(name, "voxtral-mini-latest")
        for name in ("hy", "auto", "best", "pro"):
            self.ran(name, "gpt-transcribe")

    def test_every_elevenlabs_key_name_works_and_the_first_non_empty_wins(self):
        want = {"el_default": "SECRET-eleven", "el_key_a": "SECRET-e2", "el_key_b": "SECRET-e3",
                "el_key_c": "SECRET-e4", "el_key_first": "SECRET-e1", "el_key_blank_skipped": "SECRET-e4"}
        for name, key in want.items():
            self.ran(name, _SCRIBE)
            call = self.R[name]["calls"][0]
            self.assertEqual((call["xi"], call["auth"]), (key, None), name)   # the xi-api-key header only
        self.ran("el_only_eleven", _SCRIBE)                                 # Scribe alone is enough to be configured
        self.assertEqual(self.R["el_only_eleven"]["json"]["lang"], "hy")

    def test_the_elevenlabs_request_has_the_documented_shape(self):
        call = self.R["el_lang_en"]["calls"][0]
        self.assertEqual((call["url"], call["method"]), (self.ELEVEN, "POST"))
        self.assertEqual(call["fields"], ["model_id", "file", "tag_audio_events", "language_code"])
        self.assertEqual(call["form"], {"model_id": _SCRIBE, "tag_audio_events": "false", "language_code": "en"})
        for name in ("el_lang_auto", "el_lang_none", "el_lang_multi", "el_lang_junk", "el_lang_junk2",
                     "el_lang_proto"):
            c = self.R[name]["calls"][0]
            self.assertEqual(c["fields"], ["model_id", "file", "tag_audio_events"], name)   # it detects the language
            self.assertEqual(c["form"], {"model_id": _SCRIBE, "tag_audio_events": "false"}, name)
        self.assertEqual(self.R["el_lang_iso3"]["calls"][0]["language"], "hye")
        self.assertEqual(self.R["el_hy"]["calls"][0]["language"], "hy")
        for name, r in self.R.items():                      # never speaker labels, timestamps or multichannel
            for c in self.eleven_calls(r):
                self.assertTrue({"diarize", "timestamps_granularity", "use_multi_channel"}.isdisjoint(c["fields"]), name)
                self.assertEqual(c["form"]["tag_audio_events"], "false", name)
                self.assertEqual(c["auth"], None, name)
                self.assertEqual(c["redirect"], "error", name)      # the key is never sent to a redirect target

    def test_a_refused_language_is_retried_once_without_it_inside_one_timer(self):
        for name in ("el_lang_422", "el_lang_400"):
            self.ran(name, _SCRIBE)
            self.assertEqual([(c["model"], c["language"]) for c in self.R[name]["calls"]],
                             [(_SCRIBE, "hy"), (_SCRIBE, None)], name)
        self.assertEqual([f["status"] for f in self.fails(self.R["el_lang_422"])], [422])   # the refusal is logged
        # A 422 that is not about the language is not retried: the next engine answers, and no pause starts.
        r = self.R["el_lang_plain422"]
        self.ran_in(r, "voxtral-mini-latest")
        self.assertEqual(self.names(r), [_SCRIBE, "voxtral-mini-latest"])
        self.assertEqual(self.pauses(r), [])
        self.ran_in(self.R["el_lang_plain422"]["then"][0], _SCRIBE)
        # The retry shares the attempt's timer: it cannot double it.
        r = self.R["el_lang_retry_timer"]
        self.assertEqual([c["language"] for c in r["calls"]], ["hy", None])
        self.assertEqual((r["status"], r["json"]["detail"]), (502, "timeout"))
        self.assertGreaterEqual(r["ms"], 350)
        self.assertLess(r["ms"], 700)

    def test_a_failing_scribe_hands_over_to_the_next_engine_of_the_profile(self):
        r = self.R["el_500"]
        self.ran("el_500", "voxtral-mini-latest")
        self.assertEqual(self.models("el_500"), [_SCRIBE, "voxtral-mini-latest"])
        self.ran("el_500_hy", "gpt-transcribe")                           # Voxtral does not cover Armenian
        self.assertEqual(self.models("el_500_hy"), [_SCRIBE, "gpt-transcribe"])
        self.ran("el_500_best", "gpt-transcribe")                         # BEST: Scribe, then the BEST order
        self.assertEqual(self.models("el_500_best"), [_SCRIBE, "gpt-transcribe"])
        # a 5xx, a timeout or a network error skips it for 30 s (one log line, status 0 = timeout / network
        # error); the next request starts on the next engine and 31 s later it is tried again. A "credits"
        # word in a 5xx is an outage, not the 15 minute credits pause.
        for name, status in (("el_500", 500), ("el_hang", 0), ("el_throw", 0), ("el_credits_in_a_5xx", 500)):
            steps = self.timeline(name)
            self.assertEqual(self.pauses(steps[0]), [{"stt_pause": "elevenlabs", "status": status, "minutes": 0.5}], name)
            self.ran_in(steps[1], "voxtral-mini-latest", name)
            self.assertEqual(self.names(steps[1]), ["voxtral-mini-latest"], name)       # not even called
            self.assertEqual((self.pauses(steps[1]), steps[1]["quota"]), ([], 1), name)
            self.assertLess(steps[1]["ms"], 1000, name)                                 # no wait for Scribe
            self.ran_in(steps[2], _SCRIBE, name)
            self.assertEqual(self.names(steps[2]), [_SCRIBE], name)
        # the same with the pause length from the secret (5 s here) ...
        steps = self.timeline("el_outage_env")
        self.assertEqual(self.pauses(steps[0]), [{"stt_pause": "elevenlabs", "status": 503, "minutes": 0.08}])
        self.assertEqual([self.names(s) for s in steps],
                         [[_SCRIBE, "voxtral-mini-latest"], ["voxtral-mini-latest"], [_SCRIBE]])
        # ... and when only Scribe is configured the pause is a 502 with a fixed code, nobody called, no quota
        steps = self.timeline("el_outage_only")
        for r in steps[:2]:
            self.assertEqual((r["status"], r["json"]), (502, {"error": "stt_failed", "detail": "provider_unavailable"}))
        self.assertEqual((steps[1]["calls"], steps[1]["quota"]), ([], 0))
        self.ran_in(steps[2], _SCRIBE)
        self.ran("el_hang", "voxtral-mini-latest")
        self.assertEqual(self.models("el_hang"), [_SCRIBE, "voxtral-mini-latest"])
        self.assertLess(self.R["el_hang"]["ms"], 5000)
        self.assertTrue(any(f["stt_fail"] == "elevenlabs" and f["detail"] == "timeout"
                            for f in self.fails(self.R["el_hang"])))
        self.ran("el_throw", "voxtral-mini-latest")
        self.assertTrue(any(f["detail"] == "network_error" for f in self.fails(self.R["el_throw"])))
        for name in ("el_multichannel", "el_badjson", "el_number_text"):   # no string `text`: a failed answer
            self.ran(name, "voxtral-mini-latest")
            self.assertEqual(self.models(name), [_SCRIBE, "voxtral-mini-latest"], name)
            # a 200 that is no usable answer pauses it for 60 s (not 15 minutes), so the next requests do not
            # pay for it again: skipped, then tried again after the minute
            steps = self.timeline(name)
            self.assertEqual(self.pauses(steps[0]), [{"stt_pause": "elevenlabs", "status": 200, "minutes": 1}], name)
            self.assertEqual(self.names(steps[1]), ["voxtral-mini-latest"], name)
            self.assertEqual(self.pauses(steps[1]), [], name)
            self.ran_in(steps[2], _SCRIBE, name)
        self.assertEqual([f["status"] for f in self.fails(self.R["el_multichannel"])], [200])
        self.assertEqual(self.R["el_only_eleven_fails"]["json"], {"error": "stt_failed", "detail": "provider_unavailable"})
        self.assertEqual(self.names(self.R["el_only_eleven_fails"]), [_SCRIBE])
        self.assertEqual(self.names(self.R["el_all_fail"]), [_SCRIBE] + _FAST4)
        self.assertEqual(self.R["el_all_fail"]["json"]["detail"], "provider_unavailable")
        self.assertIsNone(self.R["el_all_fail"]["model_header"])

    def test_an_empty_transcript_is_a_valid_answer_as_for_the_other_engines(self):
        r = self.R["el_empty"]
        self.assertEqual((r["status"], r["json"]["text"], r["json"]["model"]), (200, "", _SCRIBE))
        self.assertEqual(self.names(r), [_SCRIBE])

    def test_no_credits_or_no_access_pauses_scribe_for_fifteen_minutes(self):
        # 402, a 401/403, or an answer that says quota_exceeded / credits: the next engine answers NOW and
        # the following requests do not call ElevenLabs at all until the pause ends.
        kinds = {"el_c402": 402, "el_c402_plain": 402, "el_c401": 401, "el_c403": 403, "el_c401_plain": 401,
                 "el_cquota401": 401, "el_cquota400": 400}
        for name, status in kinds.items():
            first, skipped = self.R[name], self.R[name]["then"][0]
            self.ran_in(first, "voxtral-mini-latest", name)
            self.assertEqual(self.names(first), [_SCRIBE, "voxtral-mini-latest"], name)
            self.assertEqual(self.pauses(first), [{"stt_pause": "elevenlabs", "status": status, "minutes": 15}], name)
            self.ran_in(skipped, "voxtral-mini-latest", name)            # not even called
            self.assertEqual(self.names(skipped), ["voxtral-mini-latest"], name)
            self.assertEqual(self.pauses(skipped), [], name)             # one line per pause, not per skip
            resumed = self.R[name]["then"][-1]                           # 16 minutes later: tried again
            self.ran_in(resumed, _SCRIBE, name)
            self.assertEqual(self.names(resumed), [_SCRIBE], name)
        # the timeline of one of them, minute by minute
        steps = self.timeline("el_c402")
        self.assertEqual([self.names(s) for s in steps],
                         [[_SCRIBE, "voxtral-mini-latest"], ["voxtral-mini-latest"], ["voxtral-mini-latest"],
                          [_SCRIBE], [_SCRIBE]])
        self.assertEqual([s["json"]["model"] for s in steps],
                         ["voxtral-mini-latest"] * 3 + [_SCRIBE] * 2)     # 0, +0, +14 min: skipped; +16 min: back
        # a skipped engine is not counted in `tried`, is not an error, and costs one quota unit like any request
        skipped = steps[1]
        oks = [json.loads(line) for kind, line in skipped["logs"] if kind == "log"]
        self.assertEqual([(o["stt_ok"], o["tried"]) for o in oks], [("voxtral-mini-latest", 1)])
        self.assertEqual([line for kind, line in skipped["logs"] if kind == "error"], [])
        self.assertEqual(skipped["quota"], 1)
        # the failed call's log line has no key and no account, and the answer to the app a fixed code only
        blob = json.dumps(steps[0]["logs"])
        for secret in ("SECRET-eleven", "sk_0123456789abcdef0123"):
            self.assertNotIn(secret, blob)
        self.assertIn("[key]", blob)

    def test_the_pause_does_not_leak_between_scenarios(self):
        # Every scenario has its own copy of the module (its own breaker): the one right after el_c402's
        # pause starts clean, and only a scenario where ElevenLabs fails (credits, access, busy, an outage
        # or an unusable answer) ever pauses.
        self.ran("el_after_pause_probe", _SCRIBE)
        self.assertEqual(self.models("el_after_pause_probe"), [_SCRIBE])
        paused = {n for n, r in self.R.items() if any(self.pauses(s) for s in [r] + r.get("then", []))}
        self.assertTrue(paused)
        for name in paused:
            self.assertTrue(name.startswith(("el_c", "el_pause", "el_500", "el_hang", "el_throw", "el_multi",
                                             "el_badjson", "el_number", "el_outage", "el_only_eleven_fails",
                                             "el_all_fail", "el_timeout", "el_lang_retry")), name)
        for name in ("el_default", "el_lang_plain422", "el_empty", "el_lang_422", "el_en", "el_best",
                     "el_after_pause_probe"):                  # a refusal, an empty text, a good answer: no pause
            self.assertNotIn(name, paused)

    def test_busy_pauses_it_for_a_few_seconds_only(self):
        for name in ("el_c429", "el_c429_plain"):
            first = self.R[name]
            self.assertEqual(self.names(first), [_SCRIBE, "voxtral-mini-latest"], name)
            self.assertEqual(self.pauses(first), [{"stt_pause": "elevenlabs", "status": 429, "minutes": 0.25}], name)
            self.assertEqual(self.names(self.R[name]["then"][0]), ["voxtral-mini-latest"], name)   # skipped
        steps = self.timeline("el_c429")                        # +0 s, +14 s: skipped; +16 s: tried again
        self.assertEqual([self.names(s) for s in steps],
                         [[_SCRIBE, "voxtral-mini-latest"], ["voxtral-mini-latest"], ["voxtral-mini-latest"], [_SCRIBE]])
        self.ran_in(self.R["el_c429_plain"]["then"][-1], _SCRIBE)
        # never longer than STT_ELEVEN_PAUSE_MS either (5 s here)
        steps = self.timeline("el_c429_short_env")
        self.assertEqual(self.names(steps[0]), [_SCRIBE, "voxtral-mini-latest"])
        self.assertLess(self.pauses(steps[0])[0]["minutes"], 0.1)
        self.assertEqual([self.names(s) for s in steps[1:]], [["voxtral-mini-latest"], [_SCRIBE]])

    def test_the_pause_length_comes_from_the_secret_or_the_default(self):
        steps = self.timeline("el_pause_env")                  # 60 s: +59 s skipped, +61 s tried again
        self.assertEqual(self.pauses(steps[0])[0]["minutes"], 1)
        self.assertEqual([self.names(s) for s in steps[1:]], [["voxtral-mini-latest"], [_SCRIBE]])
        for name in ("el_pause_junk", "el_pause_zero", "el_pause_negative"):   # not a positive number: 15 minutes
            steps = self.timeline(name)
            self.assertEqual(self.pauses(steps[0])[0]["minutes"], 15, name)
            self.assertEqual([self.names(s) for s in steps[1:]], [["voxtral-mini-latest"], [_SCRIBE]], name)

    def test_when_only_scribe_is_configured_a_pause_is_a_502_with_a_fixed_code_and_no_quota(self):
        want = {"el_c402_only": "provider_credits", "el_c401_only": "provider_auth", "el_c429_only": "rate_limited"}
        for name, code in want.items():
            first, second = self.R[name], self.R[name]["then"][0]
            for r in (first, second):
                self.assertEqual((r["status"], r["json"]), (502, {"error": "stt_failed", "detail": code}), name)
                self.assertIsNone(r["model_header"], name)
            self.assertEqual(self.names(first), [_SCRIBE], name)
            self.assertEqual((second["calls"], second["quota"]), ([], 0), name)      # nobody called, nothing spent
        self.ran_in(self.R["el_c402_only"]["then"][-1], _SCRIBE)                       # credits back after 15 min
        self.ran_in(self.R["el_c429_only"]["then"][-1], _SCRIBE)

    def test_the_pause_hits_scribe_wherever_it_stands_and_only_scribe(self):
        for name, nxt in (("el_c402_pinned", "voxtral-mini-latest"), ("el_c402_hy", "gpt-transcribe"),
                          ("el_c402_best", "gpt-transcribe")):
            first, again = self.R[name], self.R[name]["then"][0]
            self.assertEqual(self.names(first), [_SCRIBE, nxt], name)
            self.assertEqual(self.names(again), [nxt], name)
            self.ran_in(again, nxt, name)
        # Scribe second in the order: the first engine fails, Scribe pays one 402, the next request skips it.
        first, again = self.R["el_c402_in_the_middle"], self.R["el_c402_in_the_middle"]["then"][0]
        self.assertEqual((first["status"], first["json"]["detail"]), (502, "provider_credits"))
        self.assertEqual(self.names(first), ["voxtral-mini-latest", _SCRIBE])
        self.assertEqual((again["status"], again["json"]["detail"]), (502, "provider_unavailable"))
        self.assertEqual(self.names(again), ["voxtral-mini-latest"])

    def test_orders_from_secrets_can_drop_reorder_or_rename_scribe(self):
        self.ran("el_order_drop", "voxtral-mini-latest")
        self.assertEqual(self.models("el_order_drop"), ["voxtral-mini-latest"])
        self.ran("el_order_last", "gpt-4o-transcribe")
        self.ran("el_order_last_fails", _SCRIBE)
        self.assertEqual(self.models("el_order_last_fails"), ["gpt-4o-transcribe", _SCRIBE])
        for name in ("el_order_scribe3", "el_order_scribe3_best"):       # "elevenlabs:scribe_v3": a model the catalog lacks
            self.ran(name, "scribe_v3")
            self.assertEqual(self.R[name]["calls"][0]["form"]["model_id"], "scribe_v3", name)
            self.assertEqual(self.urls(name), [self.ELEVEN], name)
        self.ran("el_order_bad_kind", _SCRIBE)                            # bad entries dropped (3 log lines), default order
        dropped = [l for kind, l in self.R["el_order_bad_kind"]["logs"] if kind == "error" and "stt_order_dropped" in l]
        self.assertEqual(len(dropped), 3)
        self.ran("el_order_best_drop", "gpt-transcribe")                  # the BEST secret does not touch FAST
        self.ran("el_order_best_drop_fast", _SCRIBE)

    def test_scribe_gets_at_least_eight_seconds_per_attempt(self):
        r = self.R["el_timeout_default"]
        self.assertEqual([(c["provider"], c["timeout_ms"]) for c in r["calls"]],
                         [("elevenlabs", 8000), ("mistral", 3500)])       # the others keep their own timeout
        r = self.R["el_timeout_long"]                                      # a long clip: its own, longer, timeout
        self.assertEqual([c["timeout_ms"] for c in r["calls"]], [30625, 30625])
        self.assertEqual(self.R["el_timeout_fixed"]["calls"][0]["timeout_ms"], 150)     # STT_TIMEOUT_MS fixes all
        t = self.R["el_timeout_total_short"]["calls"][0]["timeout_ms"]    # never past the whole request's deadline
        self.assertTrue(1500 <= t <= 3000, t)

    # ── unchanged ────────────────────────────────────────────────────────────────
    def test_auth_cap_and_the_errors_are_unchanged(self):
        R = self.R
        self.assertEqual((R["not_pro"]["status"], R["not_pro"]["json"]), (403, {"error": "pro_required"}))
        self.assertEqual((R["over_cap"]["status"], R["over_cap"]["json"]), (429, {"error": "quota_exceeded"}))
        self.assertEqual((R["no_audio"]["status"], R["no_audio"]["json"]), (400, {"error": "no_audio"}))
        self.assertEqual((R["bad_json"]["status"], R["bad_json"]["json"]), (400, {"error": "bad_request"}))
        self.assertEqual((R["no_user"]["status"], R["no_user"]["json"]), (401, {"error": "unauthorized"}))
        self.assertEqual((R["no_auth"]["status"], R["no_auth"]["json"]), (401, {"error": "unauthorized"}))
        self.assertEqual((R["options"]["status"], R["options"]["raw"]), (200, "ok"))
        self.assertEqual((R["get"]["status"], R["get"]["json"]), (405, {"error": "method_not_allowed"}))
        for key in ("not_pro", "over_cap", "no_audio", "bad_json", "no_user", "no_auth", "options", "get"):
            self.assertEqual(R[key]["calls"], [], key)                      # nothing was sent to a provider
            self.assertIsNone(R[key]["model_header"], key)


# ── "when I put Armenian, it should be Armenian": the language policy of a cloud piece ─────────

try:
    from tests.test_meeting_session import _App, _Base, _Rec, _Recorder
except ImportError:                                                    # run from inside tests/
    from test_meeting_session import _App, _Base, _Rec, _Recorder
import meeting_session as msess

HY_TEXT = "Բարև ձեզ, այսօր կխոսենք մեր նախագծի մասին։"
EN_TEXT = "Good morning everyone, let's start today's meeting."
RU_TEXT = "Хорошо, давайте начнём с главного вопроса."
VOXTRAL_NONSENSE = "Парфолорин, сор, хава, квель. Болор, Хазгери. She got money. Esquema."


class _Resp:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status_code, self._data, self.text, self.headers = status, data, text, headers or {}

    def json(self):
        return self._data if self._data is not None else {}


def _answers(*items):
    """A requests.post that answers with ``items`` in order (the last one again and again); an
    exception in the list is raised. ``.calls`` lists (url, kwargs)."""
    state = {"i": 0}
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        item = items[min(state["i"], len(items) - 1)]
        state["i"] += 1
        if isinstance(item, Exception):
            raise item
        return item
    post.calls = calls
    return post


class TestLanguagePolicyOfACloudPiece(unittest.TestCase):
    """A cloud piece that fails: in a language local Whisper is poor at (Armenian) it is tried again
    and then a gap - never Whisper's text; in any other language this computer reads it as before."""

    def setUp(self):
        self.waits = []
        p = mock.patch.object(cs, "_sleep", lambda seconds: self.waits.append(seconds))
        p.start()
        self.addCleanup(p.stop)

    def _me(self, lang="hy", cloud="", token="tok", capture=None):
        fallbacks = []
        me = SimpleNamespace(
            get_auth_token=lambda: token, _float_to_wav=lambda a: b"RIFF....WAVE",
            _lang_setting=lambda: lang, cloud_model_override=cloud,
            _speech_ran=main.AudioRecorder._new_speech_ran(), _gap=main.AudioRecorder._new_gap_state(),
            _gap_audio={}, _capture_mode=capture, _abort=False, _chunk_lock=threading.Lock(),
            _chunk_errors=[], _chunk_failures=0, _chunk_results={}, on_chunk_gap=None,
            on_chunk_complete=None, _cloud_capped=False,
            _fallback_to_local_or_error=lambda audio, backend, reason:
                fallbacks.append((backend, reason)) or ("on this computer", "en"))
        me.fallbacks = fallbacks
        return me

    def _managed(self, me, post, **conf):
        with mock.patch("requests.post", post), \
                mock.patch.dict(main.cfg, dict({"sample_rate": 16000, "managed_provider": "gemini"}, **conf)):
            return main.AudioRecorder._run_managed(me, [0.0] * 8000)

    # ── the proxy fails: 502, 429, timeouts, offline ──
    def test_a_502_is_tried_once_more_with_a_short_wait_then_it_is_a_gap_never_whispers_text(self):
        me, post = self._me(), _answers(_Resp(502, text='{"error":"stt_failed"}'))
        out = self._managed(me, post)
        self.assertEqual(out, ("", "!gap:server"))
        self.assertEqual(len(post.calls), 2)                      # two tries in all (a dead proxy: ~20 s at most)
        self.assertEqual(self.waits, [1.0])
        self.assertEqual(me.fallbacks, [])                        # this computer's Whisper never reads it
        self.assertEqual((me._gap["open"], me._gap["reason"], me._gap["language"]), (True, "server", "Armenian"))

    def test_the_second_try_that_works_is_a_normal_piece_and_the_line_goes(self):
        me = self._me()
        post = _answers(_Resp(502), _Resp(200, {"text": HY_TEXT, "lang": "hy", "model": "gemini-2.5-flash"}))
        out = self._managed(me, post)
        self.assertEqual(out, (HY_TEXT, "hy"))
        self.assertEqual((len(post.calls), self.waits), (2, [1.0]))
        self.assertFalse(me._gap["open"])                         # recovered by itself: nothing to say any more
        self.assertEqual(me._speech_ran["cloud_pieces"], 1)
        self.assertEqual(me._speech_ran["ran_model"], "gemini-2.5-flash")

    def test_a_429_waits_for_retry_after_and_a_long_one_is_left_to_the_next_piece(self):
        me = self._me()
        post = _answers(_Resp(429, text="slow down", headers={"Retry-After": "2"}))
        out = self._managed(me, post)
        self.assertEqual((out, len(post.calls), self.waits), (("", "!gap:busy"), 2, [2.0]))
        self.waits.clear()
        me = self._me()
        post = _answers(_Resp(429, text="slow down", headers={"Retry-After": "30"}))
        out = self._managed(me, post)
        self.assertEqual((out, len(post.calls), self.waits), (("", "!gap:busy"), 1, []))
        self.assertEqual(me.fallbacks, [])

    def test_gusan_clouds_own_pause_is_a_gap_at_once_not_a_loop(self):
        me, post = self._me(), _answers(_Resp(429, text='{"error":"quota_exceeded"}'))
        self.assertEqual(self._managed(me, post), ("", "!gap:cap"))
        self.assertEqual((len(post.calls), self.waits, me.fallbacks), (1, [], []))
        for status, kind in ((401, "signed_out"), (403, "pro_required")):
            me, post = self._me(), _answers(_Resp(status))
            self.assertEqual(self._managed(me, post), ("", "!gap:" + kind), status)
            self.assertEqual(len(post.calls), 1, status)

    def test_a_timeout_and_no_connection_are_tried_again_then_a_gap(self):
        for exc, kind in ((TimeoutError("read timed out"), "timeout"),
                          (ConnectionError("getaddrinfo failed"), "network")):
            self.waits.clear()
            me, post = self._me(), _answers(exc)
            self.assertEqual(self._managed(me, post), ("", "!gap:" + kind), kind)
            self.assertEqual((len(post.calls), self.waits, me.fallbacks), (2, [1.0], []), kind)

    def test_the_proxys_fixed_details_decide_what_a_live_piece_does(self):
        def stt(detail):
            return _Resp(502, {"error": "stt_failed", "detail": detail}, text='{"error":"stt_failed","detail":"%s"}' % detail)
        # worth another try: an engine did not answer / is busy
        for detail, kind in (("timeout", "server"), ("network_error", "server"), ("provider_unavailable", "server"),
                             ("rate_limited", "busy")):
            self.waits.clear()
            me, post = self._me(), _answers(stt(detail))
            self.assertEqual(self._managed(me, post), ("", "!gap:" + kind), detail)
            self.assertEqual((len(post.calls), self.waits, me.fallbacks), (2, [1.0], []), detail)
        # Gusan Cloud's own accounts can't be used right now: paused - no second request for the same piece
        for detail in ("provider_auth", "provider_credits"):
            self.waits.clear()
            me, post = self._me(), _answers(stt(detail))
            self.assertEqual(self._managed(me, post), ("", "!gap:cap"), detail)
            self.assertEqual((len(post.calls), self.waits, me.fallbacks), (1, [], []), detail)
        # no engine takes the language / the audio was refused: nothing a retry fixes
        me, post = self._me(), _answers(_Resp(502, {"error": "stt_failed", "detail": "no engine for this language"},
                                              text='{"error":"stt_failed","detail":"no engine for this language"}'))
        self.assertEqual(self._managed(me, post), ("", "!gap:no_engine"))
        self.assertEqual(len(post.calls), 1)
        me, post = self._me(), _answers(stt("provider_rejected"))
        self.assertEqual(self._managed(me, post), ("", "!gap:bad_request"))
        me, post = self._me(), _answers(_Resp(413, {"error": "audio_too_large"}, text='{"error":"audio_too_large"}'))
        self.assertEqual(self._managed(me, post), ("", "!gap:too_large"))
        # another language: this computer reads the piece at once, whatever the detail
        me, post = self._me(lang="fr"), _answers(stt("provider_credits"))
        self.assertEqual(self._managed(me, post), ("on this computer", "en"))
        self.assertEqual(len(post.calls), 1)

    def test_every_gap_has_a_calm_line(self):
        for kind in ("server", "busy", "cap", "no_engine", "too_large", "bad_request", "timeout", "network"):
            line = ec.gap_line("Armenian", kind)
            self.assertIn("Armenian", line)
            self.assertIn("Local Whisper isn't reliable", line)
            for word in ("provider", "502", "proxy", "error", "failed", "limit"):
                self.assertNotIn(word, line.lower(), (kind, word))

    def test_signed_in_but_offline_has_no_token_so_it_is_a_gap_without_a_request(self):
        me, post = self._me(token=None), _answers(_Resp(200, {"text": "x"}))
        self.assertEqual(self._managed(me, post), ("", "!gap:no_token"))
        self.assertEqual((len(post.calls), me.fallbacks), (0, []))

    # ── the proxy answers, but not in the language ──
    def test_a_voxtral_looking_answer_for_armenian_is_never_kept(self):
        for answer in (_Resp(200, {"text": VOXTRAL_NONSENSE, "lang": "hy", "model": "voxtral-mini-latest"}),
                       _Resp(200, {"text": VOXTRAL_NONSENSE, "lang": "hy"}),                  # an older server
                       _Resp(200, {"text": HY_TEXT, "lang": "hy", "model": "voxtral-mini-latest"})):
            self.waits.clear()
            me, post = self._me(), _answers(answer)
            out = self._managed(me, post)
            self.assertEqual(out, ("", "!gap:off_language"))
            self.assertEqual(len(post.calls), 2)                  # asked again: the next try may be another engine
            self.assertEqual(me.fallbacks, [])
            self.assertEqual(me._speech_ran["cloud_pieces"], 0)   # it did not count as the cloud's words

    def test_nonsense_then_a_real_answer_keeps_only_the_real_one(self):
        me = self._me()
        post = _answers(_Resp(200, {"text": VOXTRAL_NONSENSE, "model": "voxtral-mini-latest"}),
                        _Resp(200, {"text": HY_TEXT, "model": "gemini-2.5-flash"}))
        self.assertEqual(self._managed(me, post), (HY_TEXT, "hy"))
        self.assertEqual(me._speech_ran["ran_model"], "gemini-2.5-flash")

    def test_the_request_always_carries_one_spelling_of_the_language(self):
        for raw in ("hy", "hye", "hy-AM", "Armenian"):
            me = self._me()
            took = SimpleNamespace(language_override=raw)              # the take's own setting, as written
            me._lang_setting = lambda took=took: main.AudioRecorder._lang_setting(took)
            post = _answers(_Resp(200, {"text": HY_TEXT, "model": "gemini-2.5-flash"}))
            with mock.patch.dict(main.cfg, {"language": "auto"}):
                self._managed(me, post)
            body = post.calls[0][1]["json"]
            self.assertEqual((body["language"], body["languages"]), ("hy", ["hy"]), raw)

    # ── every other language: as before ──
    def test_a_language_whisper_reads_well_goes_to_this_computer_at_once_without_retries(self):
        me, post = self._me(lang="fr"), _answers(_Resp(502))
        out = self._managed(me, post)
        self.assertEqual(out, ("on this computer", "en"))
        self.assertEqual((len(post.calls), self.waits), (1, []))
        self.assertEqual(len(me.fallbacks), 1)

    def test_an_answer_in_the_wrong_script_is_a_failed_piece_for_every_distinct_script(self):
        me, post = self._me(lang="ru"), _answers(_Resp(200, {"text": EN_TEXT, "model": "gemini-2.5-flash"}))
        out = self._managed(me, post)
        self.assertEqual(out, ("on this computer", "en"))         # Russian: Whisper is good at it
        self.assertEqual(len(me.fallbacks), 1)
        me, post = self._me(lang="ru"), _answers(_Resp(200, {"text": RU_TEXT, "model": "gemini-2.5-flash"}))
        self.assertEqual(self._managed(me, post), (RU_TEXT, "ru"))
        me, post = self._me(lang="fr"), _answers(_Resp(200, {"text": VOXTRAL_NONSENSE, "model": "gemini-2.5-flash"}))
        self.assertEqual(self._managed(me, post)[0], VOXTRAL_NONSENSE)    # a Latin language is never judged by letters

    def test_mixed_languages_with_armenian_in_the_mix_follow_the_armenian_policy(self):
        me = self._me(lang="multi")
        post = _answers(_Resp(502))
        out = self._managed(me, post, mix_languages=["en", "hy"])
        self.assertEqual((out, len(post.calls), me.fallbacks), (("", "!gap:server"), 2, []))
        me = self._me(lang="multi")
        out = self._managed(me, _answers(_Resp(502)), mix_languages=["en", "fr"])
        self.assertEqual(out, ("on this computer", "en"))

    # ── your own keys ──
    def test_your_google_key_follows_the_same_policy_and_its_message_has_no_key(self):
        def run(post, lang="hy"):
            me = self._me(lang=lang)
            conf = {"google_api_key": "AIza-secret-123", "google_stt_model": "gemini-2.5-flash",
                    "sample_rate": 16000}
            with mock.patch("requests.post", post), mock.patch.dict(main.cfg, conf):
                return me, main.AudioRecorder._run_google(me, [0.0] * 8000)
        me, out = run(_answers(_Resp(429, headers={"Retry-After": "1"})))
        self.assertEqual((out, me.fallbacks), (("", "!gap:rate_limit"), []))
        self.assertEqual(self.waits, [1.0])
        me, out = run(_answers(ConnectionError("Max retries exceeded with url: /v1beta/models/x:generateContent"
                                               "?key=AIza-secret-123")))
        self.assertEqual(out, ("", "!gap:network"))
        self.assertNotIn("AIza-secret-123", repr(me.__dict__.get("_gap")))
        # Gemini covers Armenian: another script from it, ONCE, is asked again; twice is the speaker's own
        # words (code-switching) and stays - never a gap. (cloud_speech.accept_off_script)
        other = _answers(_Resp(200, {"candidates": [{"content": {"parts": [{"text": VOXTRAL_NONSENSE}]}}],
                                     "modelVersion": "gemini-2.5-flash"}))
        me, out = run(other)
        self.assertEqual((out[0], len(other.calls)), (VOXTRAL_NONSENSE, 2))
        hy_then = _answers(_Resp(200, {"candidates": [{"content": {"parts": [{"text": VOXTRAL_NONSENSE}]}}],
                                       "modelVersion": "gemini-2.5-flash"}),
                           _Resp(200, {"candidates": [{"content": {"parts": [{"text": HY_TEXT}]}}],
                                       "modelVersion": "gemini-2.5-flash"}))
        me, out = run(hy_then)
        self.assertEqual((out[0], len(hy_then.calls)), (HY_TEXT, 2))
        me, out = run(_answers(_Resp(503)), lang="fr")                     # French: this computer
        self.assertEqual(out, ("on this computer", "en"))

    def test_your_mistral_key_is_never_asked_for_armenian(self):
        def run(lang, post, **conf):
            me = self._me(lang=lang)
            c = dict({"mistral_api_key": "m-test", "mistral_stt_model": "voxtral-mini-latest",
                      "sample_rate": 16000}, **conf)
            with mock.patch("requests.post", post), mock.patch.dict(main.cfg, c):
                return me, main.AudioRecorder._run_mistral(me, [0.0] * 8000)
        post = _answers(_Resp(200, {"text": VOXTRAL_NONSENSE, "language": "ru"}))
        me, out = run("hy", post)
        self.assertEqual((out, len(post.calls), me.fallbacks), (("", "!gap:language"), 0, []))
        me, out = run("multi", post, mix_languages=["en", "hy"])
        self.assertEqual((out, len(post.calls)), (("", "!gap:language"), 0))
        post = _answers(_Resp(200, {"text": "Bonjour tout le monde", "language": "fr"}))
        me, out = run("fr", post)
        self.assertEqual(out, ("Bonjour tout le monde", "fr"))
        self.assertEqual(post.calls[0][1]["data"]["language"], "fr")
        me, out = run("ka", _answers(_Resp(200, {"text": "x"})))             # Georgian: not Voxtral's either,
        self.assertEqual((out, len(me.fallbacks)), (("on this computer", "en"), 1))   # and Whisper reads it well enough

    def test_a_cancelled_take_does_not_wait_between_tries(self):
        me = self._me()
        me._abort = True
        post = _answers(_Resp(502))
        self.assertEqual(self._managed(me, post), ("", "!gap:server"))
        self.assertEqual((len(post.calls), self.waits), (1, []))


class TestWhatTheLocalEnginesAreAsked(unittest.TestCase):
    """faster-whisper only ever gets a code it knows, whatever spelling the setting has."""

    def _model(self):
        calls = []

        def transcribe(audio, **kw):
            calls.append(kw)
            return [SimpleNamespace(text="Բարև", start=0.0, end=1.0)], SimpleNamespace(
                language="hy", all_language_probs=[("hy", 0.9)])
        return SimpleNamespace(transcribe=transcribe), calls

    def test_the_language_of_a_take_is_one_spelling(self):
        for override, code in (("hye", "hy"), ("HY", "hy"), ("hy-AM", "hy"), ("Armenian", "hy"),
                               ("en-US", "en"), ("auto", "auto"), ("multi", "multi"), ("klingon", "auto"),
                               ("", "auto")):
            me = SimpleNamespace(language_override=override or None)
            with mock.patch.dict(main.cfg, {"language": "auto"}):
                self.assertEqual(main.AudioRecorder._lang_setting(me), code, override)
        me = SimpleNamespace(language_override=None)
        with mock.patch.dict(main.cfg, {"language": "hye"}):
            self.assertEqual(main.AudioRecorder._lang_setting(me), "hy")

    def test_a_live_piece_decodes_in_the_set_language(self):
        for setting in ("hy", "fr", "ru"):
            model, calls = self._model()
            me = SimpleNamespace(_lang_setting=lambda s=setting: s, _infer_lock=threading.Lock(),
                                 _session_lang=None, _capture_mode=None)
            with mock.patch.dict(main.cfg, {"sample_rate": 16000, "initial_prompt": "", "vocabulary": []}):
                main.AudioRecorder._run_local_with(me, model, [0.0] * 16000)
            self.assertEqual(calls[-1]["language"], setting, setting)

    def test_a_mix_is_detected_among_the_languages_the_user_mixes_only(self):
        probs = [("ru", 0.5), ("hy", 0.3), ("en", 0.2)]
        self.assertEqual(speech_langs.dominant_among(probs, ["en", "hy"], "ru"), "hy")     # never Russian when not in the mix
        self.assertEqual(speech_langs.dominant_among(probs, [], "ru"), "ru")

    def test_a_file_or_the_re_read_after_stop_decodes_in_the_set_language_too(self):
        for given, expected in (("hye", "hy"), ("hy-AM", "hy"), ("auto", None), ("multi", None), ("klingon", None)):
            model, calls = self._model()
            me = SimpleNamespace(_lang_setting=lambda: "en", _infer_lock=threading.Lock())
            with mock.patch.dict(main.cfg, {"sample_rate": 16000}), \
                    mock.patch.object(main.vocabulary, "load_terms", lambda c: []):
                out = main.AudioRecorder._segments_with(me, model, [0.0] * 16000, 16000, given, None, None)
            self.assertEqual(calls[-1]["language"], expected, given)
            self.assertEqual(out[0]["text"], "Բարև")
            if given == "multi":
                self.assertTrue(calls[-1].get("multilingual"))


class _FakeArray(list):
    """Just enough of a numpy array for the kept gap audio (test_core stubs numpy suite-wide)."""

    def astype(self, dtype):
        return _FakeArray(self)

    def __mul__(self, k):
        return _FakeArray(v * k for v in self)

    def __truediv__(self, k):
        return _FakeArray(v / k for v in self)


class _FakeNp:
    float32, int16 = "float32", "int16"

    @staticmethod
    def asarray(a, dtype=None):
        return _FakeArray(a)

    @staticmethod
    def clip(a, lo, hi):
        return _FakeArray(min(hi, max(lo, v)) for v in a)

    @staticmethod
    def zeros(n, dtype=None):
        return _FakeArray([0] * n)


class TestAGapInTheTake(unittest.TestCase):
    """The audio of a piece the cloud could not read is kept, marked "[...]" and never stored as words."""

    def setUp(self):
        p = mock.patch.object(main, "np", _FakeNp())
        p.start()
        self.addCleanup(p.stop)

    def _me(self, capture="smart_meeting"):
        gaps, done = [], []
        me = SimpleNamespace(
            _capture_mode=capture, _chunk_lock=threading.Lock(), _gap=main.AudioRecorder._new_gap_state(),
            _gap_audio={}, _chunk_errors=[], _chunk_failures=0, _chunk_results={}, _abort=False,
            on_chunk_gap=lambda idx, kind: gaps.append((idx, kind)),
            on_chunk_complete=lambda idx, text, lang: done.append((idx, text, lang)),
            backend_override="managed", model_override=None, language_override="hy",
            cloud_model_override="", _speech_ran=main.AudioRecorder._new_speech_ran(), _ran_models=[])
        me.gaps, me.done = gaps, done
        me._run_managed = me._run_google = me._run_mistral = None
        return me

    def test_a_meeting_keeps_the_audio_and_tells_the_live_transcript_once(self):
        me = self._me()
        me._gap.update(language="Armenian")
        audio = [0.1] * 1600
        with mock.patch.dict(main.cfg, {"sample_rate": 16000}):
            main.AudioRecorder._mark_gap(me, 4, audio, "server", "managed")
        self.assertEqual(me.gaps, [(4, "server")])
        self.assertEqual(sorted(me._gap_audio), [4])
        self.assertEqual(len(me._gap_audio[4]), 1600)
        self.assertEqual((me._gap["pieces"], me._gap["total"]), (1, 1))
        self.assertEqual(me._chunk_results, {})                   # nothing is stored as words
        self.assertEqual(me._chunk_errors, [])                    # a meeting goes on: no error for one part

    def test_the_kept_audio_has_a_cap_and_the_oldest_goes_first(self):
        me = self._me()
        with mock.patch.dict(main.cfg, {"sample_rate": 10}), mock.patch.object(main.AudioRecorder, "GAP_KEEP_SEC", 5):
            for idx in range(4):
                main.AudioRecorder._mark_gap(me, idx, [0.1] * 30, "server", "managed")
        self.assertEqual(sorted(me._gap_audio), [3])               # 50 samples is the cap: only the newest fits
        self.assertEqual(me._gap["lost"], 3)

    def test_a_dictation_fails_with_one_plain_sentence_and_keeps_nothing(self):
        me = self._me(capture=None)
        me._gap.update(language="Armenian")
        main.AudioRecorder._mark_gap(me, 0, [0.1] * 1600, "server", "managed")
        self.assertEqual(me._gap_audio, {})
        self.assertEqual(len(me._chunk_errors), 1)
        self.assertTrue(me._chunk_errors[0].startswith("!managed:Armenian couldn't be transcribed just now."))
        self.assertIn("Local Whisper isn't reliable for it", me._chunk_errors[0])
        self.assertEqual(me.gaps, [])

    def test_the_cloud_answering_again_fills_the_gap_in_its_place(self):
        me = self._me()
        me._gap.update(language="Armenian")
        with mock.patch.dict(main.cfg, {"sample_rate": 16000}):
            main.AudioRecorder._mark_gap(me, 2, [0.1] * 1600, "server", "managed")
            main.AudioRecorder._mark_gap(me, 5, [0.1] * 1600, "server", "managed")
        me._run_managed = lambda audio, retries=None: (HY_TEXT, "hy")
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "backend": "managed"}):
            filled = main.AudioRecorder._fill_gaps(me, limit=1)
        self.assertEqual(filled, 1)
        self.assertEqual(me._chunk_results, {2: HY_TEXT})          # the OLDEST, in its own place
        self.assertEqual(me.done, [(2, HY_TEXT, "hy")])
        self.assertEqual((me._gap["pieces"], me._gap["recovered"]), (1, 1))
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "backend": "managed"}):
            self.assertEqual(main.AudioRecorder._fill_gaps(me, limit=None), 1)
        self.assertEqual(sorted(me._chunk_results), [2, 5])
        self.assertEqual(me._gap_audio, {})

    def test_a_piece_that_still_fails_stays_kept_for_the_next_time(self):
        me = self._me()
        me._gap.update(language="Armenian")
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "backend": "managed"}):
            main.AudioRecorder._mark_gap(me, 1, [0.1] * 1600, "server", "managed")
            me._run_managed = lambda audio, retries=None: ("", "!gap:server")
            self.assertEqual(main.AudioRecorder._fill_gaps(me, limit=None), 0)
        self.assertEqual(sorted(me._gap_audio), [1])
        self.assertEqual(me._chunk_results, {})

    def test_the_report_says_what_the_card_and_the_page_need(self):
        me = self._me()
        me._gap.update(open=True, reason="server", language="Armenian", pieces=2, total=3, retries=4, recovered=1)
        with mock.patch.dict(main.cfg, {"backend": "local", "whisper_model": "base"}):
            rep = main.AudioRecorder.speech_report(me)
        self.assertEqual((rep["gap_open"], rep["gap_reason"], rep["gap_language"], rep["gap_pieces"],
                          rep["gap_total"], rep["gap_retries"], rep["gap_recovered"]),
                         (True, "server", "Armenian", 2, 3, 4, 1))
        self.assertEqual(rep["backend"], "managed")


class TestTheLineSaysTheTruth(unittest.TestCase):
    """The card's footer and the page row name what ran, calmly say when Armenian could not be read
    just now, and never claim another model."""

    def _report(self, **kw):
        return dict({"backend": "managed", "cloud_model": "", "ran_model": "gemini-2.5-flash",
                     "local_pieces": 0, "language": "hy", "gap_open": False, "gap_reason": "",
                     "gap_language": "Armenian"}, **kw)

    def test_armenian_that_could_not_be_read_says_so_and_goes_when_the_cloud_answers(self):
        c = cfg()
        report = self._report(gap_open=True, gap_reason="server")
        e = ec.take_engine(c, report)
        note = ec.live_speech_note(c, True, "hy", True, engine=e, report=report)
        self.assertEqual(note, "Armenian couldn't be transcribed just now - retrying. "
                               "Local Whisper isn't reliable for it.")
        self.assertEqual(ec.live_speech_note(c, True, "hy", True, engine=e, report=self._report()), "")
        # the line above it still names what ran, never another model
        self.assertEqual(ec.live_speech_status(e, report), "Transcribing with Gemini 2.5 Flash · Cloud")

    def test_the_words_follow_why_it_failed(self):
        c = cfg()
        for kind, needle in (("no_token", "no connection"), ("signed_out", "sign in again"),
                             ("pro_required", "needs Gusan Cloud or your own Google key"),
                             ("cap", "isn't available right now"), ("language", "Your Mistral key can't transcribe Armenian"),
                             ("bad_key", "check your key"), ("timeout", "retrying"), ("off_language", "retrying")):
            report = self._report(gap_open=True, gap_reason=kind)
            note = ec.live_speech_note(c, True, "hy", True, engine=ec.take_engine(c, report), report=report)
            self.assertIn(needle, note, kind)
            low = note.lower()
            for word in ("error", "warning", "!", "failed", "limit", "quota", "token"):
                self.assertNotIn(word, low, (kind, word))

    def test_other_languages_never_get_the_line(self):
        c = cfg(live_assist_language="fr")
        report = self._report(language="fr", gap_open=True, gap_reason="server")
        self.assertEqual(ec.live_speech_note(c, True, "fr", True, report=report), "")

    def test_the_summary_after_stop_counts_the_parts_left_out(self):
        self.assertEqual(ec.gap_summary("Armenian", 0), "")
        self.assertEqual(ec.gap_summary("Armenian", 1), "One part of the call couldn't be transcribed in Armenian "
                                                        "and is left out. The recording has everything.")
        self.assertEqual(ec.gap_summary("Armenian", 3), "3 parts of the call couldn't be transcribed in Armenian "
                                                        "and are left out. The recording has everything.")
        self.assertEqual(ec.gap_summary("Armenian", "x"), "")

    def test_a_dictation_says_to_try_again_not_retrying(self):
        self.assertIn("Try again in a moment", ec.gap_line("Armenian", "server", final=True))
        self.assertNotIn("retrying", ec.gap_line("Armenian", "server", final=True))
        self.assertIn("retrying", ec.gap_line("Armenian", "server"))
        self.assertIn("sign in again", ec.gap_line("Armenian", "signed_out", final=True))


class TestTheSessionAndTheGap(_Base):
    """MeetingEngine: "[...]" on the live transcript, never as speech in the final one."""

    def _rec(self, **report):
        rec = _Rec(result=(HY_TEXT, "hy"))
        rec.report = dict({"backend": "managed", "gap_pieces": 0, "gap_language": "Armenian",
                           "language": "hy", "cloud_pieces": 2}, **report)
        rec.speech_report = lambda: rec.report
        rec.on_chunk_gap = None
        return rec

    def test_the_mark_is_shown_once_per_run_and_never_stored(self):
        app = _App(recorder=self._rec())
        s = self.session(app)
        raw, lines = _Recorder(s.sig_chunk), _Recorder(s.live_line)
        s.start("x")
        self.assertEqual(app.recorder.on_chunk_gap, s._on_chunk_gap)
        s._on_chunk_transcribed(0, HY_TEXT, "hy")
        s._on_chunk_gap(1, "server")
        s._on_chunk_gap(2, "server")                               # a run of gaps is one mark
        self.assertEqual([c[0] for c in raw.calls], [HY_TEXT, "[...]"])
        s._on_chunk_transcribed(3, "Բարև", "hy")
        s._on_chunk_gap(4, "server")                               # words again: a new run
        self.assertEqual([c[0] for c in raw.calls], [HY_TEXT, "[...]", "Բարև", "[...]"])
        rows = (s.meeting_dir / "chunks.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("[...]", rows)                            # the recovery log has words only
        self.assertEqual(len(rows.splitlines()), 2)
        self.assertTrue(all("[...]" not in c["text"] for c in s._chunks))

    def test_the_final_transcript_and_the_notes_never_hold_the_placeholder(self):
        app = _App(recorder=self._rec(gap_pieces=2))
        s = self.session(app)
        s._build_attributed_transcript = lambda: ""
        s.start("x")
        s._on_chunk_transcribed(0, HY_TEXT, "hy")
        s._on_chunk_gap(1, "server")
        notes_in = []
        with mock.patch.object(msess.actions, "process", side_effect=lambda ctx, *a, **k: notes_in.append(ctx) or "notes"):
            s.stop()
        self.assertNotIn("[...]", s._final_transcript)
        self.assertNotIn("[...]", "".join(notes_in))
        self.assertTrue(any("2 parts of the call couldn't be transcribed in Armenian" in n
                            for n in s._transcript_notices))
        self.assertNotIn("[...]", (s.meeting_dir / "transcript.txt").read_text(encoding="utf-8"))
        self.assertFalse(any("[...]" in n for n in s._transcript_notices))

    def test_when_every_part_stayed_unread_the_call_is_not_called_empty(self):
        rec = self._rec(gap_pieces=3, gap_total=3)
        rec.result = ("", "hy")
        s = self.session(_App(recorder=rec))
        done = _Recorder(s.finished)
        s.start("x")
        s._on_chunk_gap(0, "server")
        s.stop()
        self.assertTrue(done.calls)
        self.assertEqual(done.calls[-1][-1], "None of the call could be transcribed in Armenian just now. "
                                             "The recording has everything.")
        # a call with nothing in it at all keeps its old words
        rec = self._rec(gap_pieces=0, gap_total=0)
        rec.result = ("", "")
        s = self.session(_App(recorder=rec))
        done = _Recorder(s.finished)
        s.start("y")
        s.stop()
        self.assertEqual(done.calls[-1][-1], "No transcription recorded. The meeting is empty.")

    def test_nothing_is_said_when_every_part_was_read(self):
        app = _App(recorder=self._rec(gap_pieces=0))
        s = self.session(app)
        s._build_attributed_transcript = lambda: ""
        s.start("x")
        with mock.patch.object(msess.actions, "process", return_value="notes"):
            s.stop()
        self.assertFalse(any("couldn't be transcribed" in n for n in s._transcript_notices))

    def test_the_recap_never_summarises_the_placeholder(self):
        app = _App(recorder=self._rec())
        s = self.session(app, inline=False)
        s.start("x")
        s._live_text = "Hello there. [...] And then more. " * 20
        s._last_summary_at = 0.0
        s._maybe_recap(now=1e9)
        self.assertTrue(s.spawned)
        self.assertNotIn("[...]", s.spawned[-1][1][0])


if __name__ == "__main__":
    unittest.main()
