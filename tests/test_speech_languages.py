"""Gusan 2.0.1: "when I put Armenian, it should be Armenian" - the one table of languages every
engine path reads (speech_langs), the script check that refuses a cloud answer in the wrong
letters, and the retry rules a live piece follows (cloud_speech).

Pure logic: no Qt, no audio, no network (fabricated strings, fake requests, a fake clock)."""
import os
import sys
import unittest
from types import SimpleNamespace

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import cloud_speech as cs
import speech_langs as sl

# Fabricated sentences (nothing of anyone's speech): one per script.
HY = "Բարև ձեզ, այսօր կխոսենք մեր նախագծի մասին։"
RU = "Хорошо, давайте начнём с главного вопроса."
EL = "Καλημέρα σας, ξεκινάμε τη σημερινή συνάντηση."
HE = "בוקר טוב לכולם, בואו נתחיל את הפגישה."
AR = "صباح الخير جميعا، لنبدأ اجتماع اليوم الآن."
HI = "नमस्ते सभी को, आज की बैठक शुरू करते हैं।"
TH = "สวัสดีครับทุกคน เรามาเริ่มการประชุมกันเลย"
JA = "皆さんおはようございます、会議を始めましょう。"
KO = "여러분 안녕하세요, 오늘 회의를 시작하겠습니다."
ZH = "大家早上好，我们现在开始今天的会议。"
KA = "დილა მშვიდობისა ყველას, დავიწყოთ დღევანდელი შეხვედრა."
EN = "Good morning everyone, let's start today's meeting."
FR = "Bonjour à tous, commençons la réunion d'aujourd'hui."
# What Voxtral wrote for Armenian speech in the founder's live test: Cyrillic and Latin nonsense.
NONSENSE = "Парфолорин, сор, хава, квель. Болор, Хазгери. She got money. Esquema."


class TestOneSpelling(unittest.TestCase):
    def test_every_spelling_of_armenian_is_hy(self):
        for value in ("hy", "HY", " hy ", "hy-AM", "hy_AM", "hye", "arm", "Armenian", "armenian", "hy-Latn"):
            self.assertEqual(sl.normalize(value), "hy", value)

    def test_region_codes_and_three_letter_codes(self):
        for value, code in (("en-US", "en"), ("en_GB", "en"), ("fr-CA", "fr"), ("pt-BR", "pt"),
                            ("zh-Hant", "zh"), ("zh-CN", "zh"), ("ru-RU", "ru"), ("deu", "de"),
                            ("fra", "fr"), ("rus", "ru"), ("French", "fr"), ("Russian", "ru"),
                            ("iw", "he"), ("ukr", "uk"), ("jpn", "ja")):
            self.assertEqual(sl.normalize(value), code, value)

    def test_auto_and_multi_words(self):
        for value in ("auto", "AUTO", "Auto-detect", "detect", "automatic"):
            self.assertEqual(sl.normalize(value), "auto", value)
        for value in ("multi", "Mixed", "mixed languages", "MULTILINGUAL"):
            self.assertEqual(sl.normalize(value), "multi", value)

    def test_nothing_or_nonsense_is_the_default(self):
        for value in ("", None, "   ", "xx", "klingon", "123"):
            self.assertEqual(sl.normalize(value, default="auto"), "auto", repr(value))
            self.assertEqual(sl.normalize(value), "", repr(value))

    def test_faster_whisper_only_gets_a_code_it_knows(self):
        # a code it does not know raises inside faster-whisper: so "auto" / "multi" / junk detect instead
        self.assertEqual(sl.whisper_language("hye"), "hy")
        self.assertEqual(sl.whisper_language("en-US"), "en")
        self.assertEqual(sl.whisper_language("Armenian"), "hy")
        for value in ("auto", "multi", "", None, "xx", "klingon"):
            self.assertIsNone(sl.whisper_language(value), repr(value))

    def test_names_for_the_card_and_the_notes(self):
        self.assertEqual(sl.language_name("hy"), "Armenian")
        self.assertEqual(sl.language_name("hye"), "Armenian")
        self.assertEqual(sl.language_name("uk"), "Ukrainian")
        self.assertEqual(sl.language_name("klingon"), "klingon")        # as written, never invented
        self.assertEqual(sl.language_name(None), "")


class TestTheTable(unittest.TestCase):
    """Which engine family can transcribe which language - data, one place."""

    def test_whisper_and_gemini_know_every_whisper_language_armenian_included(self):
        for family in (sl.FAMILY_WHISPER, sl.FAMILY_GEMINI):
            self.assertEqual(sl.LANGUAGE_SUPPORT[family], frozenset(sl.WHISPER_NAMES))
            self.assertTrue(sl.supports(family, "hy"))
            self.assertTrue(sl.supports(family, "hye"))
        self.assertTrue(set(sl.NAMES) <= set(sl.WHISPER_NAMES))          # every language the app offers

    def test_voxtral_is_only_its_own_list_and_never_armenian(self):
        self.assertEqual(sl.LANGUAGE_SUPPORT[sl.FAMILY_VOXTRAL], sl.MISTRAL_LANGS)
        self.assertFalse(sl.supports(sl.FAMILY_VOXTRAL, "hy"))
        self.assertFalse(sl.supports(sl.FAMILY_VOXTRAL, "Armenian"))
        self.assertFalse(sl.supports(sl.FAMILY_VOXTRAL, "ka"))
        for code in ("en", "fr", "de", "es", "ru", "ar", "hi", "it", "nl", "pt"):
            self.assertTrue(sl.supports(sl.FAMILY_VOXTRAL, code), code)
        # The app claims Voxtral for exactly the ten languages transcribe-proxy sends it: never more
        self.assertEqual(sl.MISTRAL_LANGS, frozenset({"en", "es", "fr", "de", "it", "nl", "pt", "hi", "ar", "ru"}))
        for code in ("uk", "pl", "tr", "ja", "ko", "zh", "ro", "cs", "el"):
            self.assertFalse(sl.supports(sl.FAMILY_VOXTRAL, code), code)

    def test_the_voxtral_table_is_the_proxys_table(self):
        import os
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "supabase", "functions", "transcribe-proxy", "index.ts")
        if not os.path.exists(path):
            self.skipTest("no transcribe-proxy source here")
        with open(path, encoding="utf-8") as f:
            m = re.search(r"const MISTRAL_LANGS = new Set\(\[(.*?)\]\)", f.read(), re.S)
        self.assertIsNotNone(m)
        self.assertEqual(frozenset(re.findall(r'"([a-z]+)"', m.group(1))), sl.MISTRAL_LANGS)

    def test_gpt_and_scribe_cover_what_whisper_covers_and_are_capable(self):
        for family in (sl.FAMILY_GPT, sl.FAMILY_SCRIBE):
            self.assertEqual(sl.LANGUAGE_SUPPORT[family], frozenset(sl.WHISPER_NAMES))
            self.assertTrue(sl.supports(family, "hy"))
        for model in ("gpt-transcribe", "gpt-4o-transcribe", "GPT Transcribe", "scribe_v2",
                      "ElevenLabs Scribe v2", "gemini-2.5-flash"):
            self.assertTrue(sl.is_capable(model), model)
        for model in ("voxtral-mini-latest", "small", "whisper-1", "", None, "unknown-thing"):
            self.assertFalse(sl.is_capable(model), model)

    def test_nothing_is_refused_when_nothing_is_asked(self):
        for family in (sl.FAMILY_VOXTRAL, sl.FAMILY_GEMINI, "", "something-else"):
            for value in ("auto", "", None):
                self.assertTrue(sl.engine_covers(family, value), (family, value))
        self.assertTrue(sl.supports("a family nobody knows", "hy"))

    def test_mixed_languages_need_every_language_covered(self):
        self.assertTrue(sl.engine_covers(sl.FAMILY_VOXTRAL, "multi", ["en", "fr"]))
        self.assertFalse(sl.engine_covers(sl.FAMILY_VOXTRAL, "multi", ["en", "hy"]))
        self.assertTrue(sl.engine_covers(sl.FAMILY_VOXTRAL, "multi", []))           # any language may come: nothing refused
        self.assertTrue(sl.engine_covers(sl.FAMILY_GEMINI, "multi", ["en", "hy"]))
        self.assertFalse(sl.engine_covers(sl.FAMILY_VOXTRAL, "hy"))

    def test_the_family_of_a_model(self):
        for model, family in (("gemini-2.5-pro", sl.FAMILY_GEMINI), ("Gemini 2.5 Flash", sl.FAMILY_GEMINI),
                              ("voxtral-mini-latest", sl.FAMILY_VOXTRAL), ("Voxtral Mini", sl.FAMILY_VOXTRAL),
                              ("small", sl.FAMILY_WHISPER), ("large-v3-turbo", sl.FAMILY_WHISPER),
                              ("Whisper Small", sl.FAMILY_WHISPER), ("", ""), (None, ""),
                              # a gpt-* model is never Whisper: it is its own engine (and covers Armenian)
                              ("gpt-4o", sl.FAMILY_GPT), ("gpt-transcribe", sl.FAMILY_GPT),
                              ("gpt-4o-mini-transcribe", sl.FAMILY_GPT), ("GPT Transcribe", sl.FAMILY_GPT),
                              ("scribe_v2", sl.FAMILY_SCRIBE), ("ElevenLabs Scribe v2", sl.FAMILY_SCRIBE),
                              ("whisper-1", sl.FAMILY_WHISPER)):
            self.assertEqual(sl.family_of_model(model), family, model)

    def test_local_whisper_is_poor_at_armenian_only_and_the_table_is_data(self):
        self.assertEqual(sl.local_whisper_poor("hy"), "Armenian")
        self.assertEqual(sl.local_whisper_poor("hye"), "Armenian")
        for value in ("en", "fr", "ru", "auto", "", None):
            self.assertEqual(sl.local_whisper_poor(value), "", value)
        mix = {"mix_languages": ["en", "hy"]}
        self.assertEqual(sl.local_whisper_poor("multi", mix), "Armenian")
        self.assertEqual(sl.local_whisper_poor("multi", {"mix_languages": ["en", "fr"]}), "")
        self.assertEqual(sl.local_whisper_poor("multi", {"mix_languages": ["hy"]}), "")     # fewer than two: no mix
        self.assertEqual(sl.local_quality("hy"), "poor")
        self.assertEqual(sl.local_quality("fr"), "good")
        self.assertEqual(sl.local_quality("auto"), "good")
        self.assertEqual(sl.local_quality("klingon"), "none")


class TestTheScriptCheck(unittest.TestCase):
    """A cloud answer whose letters are not the language's own is a failed piece."""

    def test_armenian_text_is_armenian(self):
        self.assertTrue(sl.script_matches(HY, "hy"))
        self.assertTrue(sl.script_matches(HY, "hye"))
        self.assertTrue(sl.script_matches(HY, "Armenian"))

    def test_the_founders_voxtral_nonsense_is_not(self):
        self.assertFalse(sl.script_matches(NONSENSE, "hy"))
        self.assertEqual(sl.script_name(NONSENSE), "cyrillic")          # (Latin and Cyrillic: the bigger share)
        self.assertFalse(sl.script_matches(EN, "hy"))
        self.assertFalse(sl.script_matches(RU, "hy"))

    def test_a_few_foreign_words_inside_the_languages_own_text_are_fine(self):
        mixed = HY + " Okay, we ship on Friday. " + HY
        self.assertTrue(sl.script_matches(mixed, "hy"))
        self.assertTrue(sl.script_matches(HY + " iPhone", "hy"))

    def test_each_distinct_script(self):
        right = {"hy": HY, "ru": RU, "uk": RU, "bg": RU, "el": EL, "he": HE, "ar": AR, "fa": AR,
                 "hi": HI, "th": TH, "ja": JA, "ko": KO, "zh": ZH, "ka": KA}
        for code, text in right.items():
            self.assertTrue(sl.has_distinct_script(code), code)
            self.assertTrue(sl.script_matches(text, code), code)
            self.assertFalse(sl.script_matches(EN, code), code)          # English letters are never these
            if code not in ("ru", "uk", "bg"):                           # (the nonsense is mostly Cyrillic)
                self.assertFalse(sl.script_matches(NONSENSE, code), code)
        self.assertFalse(sl.script_matches(HY, "ru"))                    # Armenian letters are not Cyrillic
        self.assertFalse(sl.script_matches(RU, "el"))
        self.assertTrue(sl.script_matches(JA + " " + ZH, "ja"))          # kana + kanji both count for Japanese
        self.assertFalse(sl.script_matches(ZH, "ko"))

    def test_never_applied_to_auto_multi_or_latin_languages(self):
        for value in ("auto", "multi", "", None):
            for text in (HY, RU, EN, NONSENSE):
                self.assertTrue(sl.script_matches(text, value), (value, text))
        for code in ("en", "fr", "de", "es", "it", "pt", "nl", "pl", "tr", "sv", "vi", "id"):
            self.assertFalse(sl.has_distinct_script(code), code)
            for text in (HY, RU, ZH, NONSENSE):
                self.assertTrue(sl.script_matches(text, code), (code, text))     # (a French meeting may quote anyone)
        self.assertTrue(sl.script_matches(FR, "fr"))
        self.assertTrue(sl.script_matches(EN, "klingon"))                # a language nothing knows: nothing to judge

    def test_a_short_piece_or_no_letters_is_never_judged(self):
        for text in ("", None, "Okay", "Yes.", "123 456", "...", "ok ok", "Да", "  "):
            self.assertTrue(sl.script_matches(text, "hy"), repr(text))

    def test_answer_problem_names_why(self):
        self.assertEqual(sl.answer_problem(NONSENSE, "hy", "voxtral-mini-latest"), "engine")
        self.assertEqual(sl.answer_problem(HY, "hy", "voxtral-mini-latest"), "engine")        # the model, even in Armenian letters
        self.assertEqual(sl.answer_problem(NONSENSE, "hy", "gemini-2.5-flash"), "script")
        self.assertEqual(sl.answer_problem(NONSENSE, "hy", ""), "script")                      # an older server says nothing
        self.assertEqual(sl.answer_problem(HY, "hy", "gemini-2.5-pro"), "")
        self.assertEqual(sl.answer_problem(HY, "hy", ""), "")
        self.assertEqual(sl.answer_problem(RU, "fr", "voxtral-mini-latest"), "")               # French: Voxtral is fine, Latin unchecked
        self.assertEqual(sl.answer_problem(NONSENSE, "auto", "voxtral-mini-latest"), "")
        self.assertEqual(sl.answer_problem(NONSENSE, "", "gemini-2.5-flash"), "")
        # a mix with Armenian in it is not Voxtral's; a mix of covered languages is
        self.assertEqual(sl.answer_problem(EN, "multi", "voxtral-mini-latest", ["en", "hy"]), "engine")
        self.assertEqual(sl.answer_problem(EN, "multi", "voxtral-mini-latest", ["en", "fr"]), "")
        self.assertEqual(sl.answer_problem(EN, "multi", "gemini-2.5-flash", ["en", "hy"]), "")
        self.assertEqual(sl.answer_problem(RU, "ru", "voxtral-mini-latest"), "")               # Russian: covered, in Cyrillic
        self.assertEqual(sl.answer_problem(EN, "ru", "voxtral-mini-latest"), "script")

    def test_the_log_never_gets_the_words(self):
        self.assertEqual(sl.script_name(HY), "armenian")
        self.assertEqual(sl.script_name(EN), "latin")
        self.assertEqual(sl.script_name(""), "")
        self.assertEqual(sl.script_name("123 !?"), "")


class TestWhatIsSentToEachEngine(unittest.TestCase):
    """The language reaches every engine as one spelling - and never as something it would refuse."""

    def spec(self, backend="managed", language="hy", cfg=None, model=None):
        return cs.spec_from_cfg(cfg or {}, backend, model, language=language, token_fn=lambda: "tok")

    def test_gusan_cloud_gets_the_normalized_language_and_the_languages_list(self):
        wav = b"RIFF....WAVE"
        for value in ("hy", "hye", "HY", "hy-AM", "Armenian"):
            body = cs.build_request(self.spec(language=value), wav, "tok")["json"]
            self.assertEqual((body["language"], body["languages"]), ("hy", ["hy"]), value)
        body = cs.build_request(self.spec(language="auto"), wav, "tok")["json"]
        self.assertEqual((body["language"], body["languages"]), ("auto", []))
        body = cs.build_request(self.spec(language="klingon"), wav, "tok")["json"]
        self.assertEqual((body["language"], body["languages"]), ("auto", []))           # never a code nothing knows
        body = cs.build_request(self.spec(language="en-US"), wav, "tok")["json"]
        self.assertEqual((body["language"], body["languages"]), ("en", ["en"]))

    def test_multi_sends_the_mix_or_nothing_never_a_made_up_list(self):
        wav = b"RIFF....WAVE"
        mix = {"mix_languages": ["en", "hy", "ru"]}
        body = cs.build_request(self.spec(language="multi", cfg=mix), wav, "tok")["json"]
        self.assertEqual(body["language"], "multi")
        self.assertEqual(sorted(body["languages"]), ["en", "hy", "ru"])
        body = cs.build_request(self.spec(language="multi", cfg={"mix_languages": ["hy"]}), wav, "tok")["json"]
        self.assertEqual((body["language"], body["languages"]), ("multi", []))         # one language is not a mix
        body = cs.build_request(self.spec(language="multi"), wav, "tok")["json"]
        self.assertEqual(body["languages"], [])

    def test_your_google_key_gets_the_language_in_its_prompt_in_the_right_script(self):
        wav = b"RIFF....WAVE"
        spec = self.spec("google", "hye", {"google_api_key": "AIza-test"})
        req = cs.build_request(spec, wav, "AIza-test")
        text = req["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Armenian", text)
        self.assertIn("Armenian alphabet", text)
        self.assertIn("never transliterated", text)
        self.assertNotIn("AIza-test", req["url"])                                     # the key is a header, never the URL
        spec = self.spec("google", "multi", {"google_api_key": "k", "mix_languages": ["en", "hy"]})
        text = cs.build_request(spec, wav, "k")["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("English and Armenian", text)

    def test_your_mistral_key_gets_a_language_only_voxtral_covers(self):
        wav = b"RIFF....WAVE"
        cfg = {"mistral_api_key": "m-test"}
        fr = cs.build_request(self.spec("mistral", "fr-FR", cfg), wav, "m-test")
        self.assertEqual(fr["data"]["language"], "fr")
        hy = cs.build_request(self.spec("mistral", "hy", cfg), wav, "m-test")
        self.assertNotIn("language", hy["data"])          # (and send() refuses it before this: language_problem)
        self.assertEqual(cs.language_problem(self.spec("mistral", "hy", cfg)), "language")
        self.assertEqual(cs.language_problem(self.spec("mistral", "fr", cfg)), "")
        self.assertEqual(cs.language_problem(self.spec("mistral", "auto", cfg)), "")
        self.assertEqual(cs.language_problem(self.spec("mistral", "multi", dict(cfg, mix_languages=["en", "hy"]))),
                         "language")
        self.assertEqual(cs.language_problem(self.spec("google", "hy", {"google_api_key": "k"})), "")
        self.assertEqual(cs.language_problem(self.spec("managed", "hy")), "")          # Gusan Cloud decides on its server


class FakeResp:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status_code, self._data, self.text, self.headers = status, data, text, headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class TestWhatTheCloudAnswers(unittest.TestCase):
    """cloud_speech.send_ex on the answers the founder's call produced (and the ones next to them)."""

    def go(self, answer, language="hy", backend="managed", cfg=None):
        spec = cs.spec_from_cfg(cfg or {}, backend, None, language=language, token_fn=lambda: "tok")
        if callable(answer):
            post = answer
        else:
            post = lambda url, **kw: answer           # noqa: E731
        return cs.send_ex(spec, b"RIFF....WAVE", post=post)

    def test_a_voxtral_looking_answer_for_armenian_is_a_failed_piece(self):
        for answer in (FakeResp(200, {"text": NONSENSE, "model": "voxtral-mini-latest"}),
                       FakeResp(200, {"text": NONSENSE}, headers={"X-Gusan-Model": "voxtral-mini-latest"}),
                       FakeResp(200, {"text": NONSENSE}),                              # an older server: the letters tell
                       FakeResp(200, {"text": HY, "model": "voxtral-mini-latest"})):   # even in Armenian letters: not Voxtral's
            with self.assertRaises(cs.CloudSpeechError) as ctx:
                self.go(answer)
            self.assertEqual(ctx.exception.kind, "off_language")
            self.assertTrue(ctx.exception.retryable)
            self.assertIn("Armenian", ctx.exception.message)
            self.assertNotIn("Парфолорин", ctx.exception.message)                     # never the words

    def test_a_refused_answer_carries_what_a_file_needs_to_keep_it(self):
        with self.assertRaises(cs.CloudSpeechError) as ctx:
            self.go(FakeResp(200, {"text": EN, "language": "en", "model": "gpt-transcribe"}))
        e = ctx.exception
        self.assertEqual((e.why, e.model), ("script", "gpt-transcribe"))
        self.assertEqual(e.answer, (EN, "hy", "gpt-transcribe"))          # (the language that was asked for)
        self.assertTrue(cs.accept_off_script(e.why, e.model, 2))          # a capable model, the second time
        self.assertFalse(cs.accept_off_script(e.why, e.model, 1))         # not on the first answer
        with self.assertRaises(cs.CloudSpeechError) as ctx:
            self.go(FakeResp(200, {"text": EN, "model": "voxtral-mini-latest"}))
        self.assertEqual(ctx.exception.why, "engine")                      # Voxtral does not cover it: never kept

    def test_a_good_armenian_answer_goes_through(self):
        text, lang, model = self.go(FakeResp(200, {"text": " " + HY + " ", "model": "gemini-2.5-flash"}))
        self.assertEqual((text, lang, model), (HY, "hy", "gemini-2.5-flash"))

    def test_other_languages_are_never_second_guessed_by_their_letters_when_latin(self):
        for language in ("fr", "en", "de", "auto"):
            text, _lang, _model = self.go(FakeResp(200, {"text": NONSENSE, "model": "gemini-2.5-flash"}), language)
            self.assertEqual(text, NONSENSE, language)
        with self.assertRaises(cs.CloudSpeechError):
            self.go(FakeResp(200, {"text": EN, "model": "gemini-2.5-flash"}), "ru")      # a distinct script is checked

    def test_the_servers_502_and_the_other_failures_are_typed(self):
        cases = ((FakeResp(502, text='{"error":"stt_failed"}'), "server", True),
                 (FakeResp(500, text="boom"), "server", True),
                 (FakeResp(429, text="slow down", headers={"Retry-After": "3"}), "busy", True),
                 (FakeResp(429, text='{"error":"quota"}'), "cap", False),
                 (FakeResp(401, text=""), "signed_out", False),
                 (FakeResp(403, text=""), "pro_required", False),
                 (FakeResp(408, text=""), "timeout", True),
                 (FakeResp(200, None, text="<html>"), "bad_response", True))
        for resp, kind, retryable in cases:
            with self.assertRaises(cs.CloudSpeechError) as ctx:
                self.go(resp)
            self.assertEqual((ctx.exception.kind, ctx.exception.retryable), (kind, retryable), resp.status_code)
        with self.assertRaises(cs.CloudSpeechError) as ctx:
            self.go(FakeResp(429, text="slow", headers={"Retry-After": "3"}))
        self.assertEqual(ctx.exception.retry_after, 3.0)
        with self.assertRaises(cs.CloudSpeechError) as ctx:
            self.go(FakeResp(429, text="slow", headers={"Retry-After": "9999"}))
        self.assertEqual(ctx.exception.retry_after, float(cs.RETRY_AFTER_MAX))

    def test_a_timeout_and_no_connection(self):
        def timeout(url, **kw):
            raise TimeoutError("read timed out")

        def offline(url, **kw):
            raise ConnectionError("Failed to establish a new connection: [Errno 11001] getaddrinfo failed")
        for post, kind in ((timeout, "timeout"), (offline, "network")):
            with self.assertRaises(cs.CloudSpeechError) as ctx:
                self.go(post)
            self.assertEqual((ctx.exception.kind, ctx.exception.retryable), (kind, True))
        spec = cs.spec_from_cfg({}, "managed", None, language="hy", token_fn=lambda: None)   # signed in, but offline: no token
        with self.assertRaises(cs.CloudSpeechError) as ctx:
            cs.send_ex(spec, b"RIFF", post=lambda *a, **k: FakeResp())
        self.assertEqual(ctx.exception.kind, "no_token")
        self.assertFalse(ctx.exception.retryable)

    def test_your_own_mistral_key_is_never_asked_for_armenian(self):
        calls = []
        spec = cs.spec_from_cfg({"mistral_api_key": "m-test"}, "mistral", None, language="hy")
        with self.assertRaises(cs.CloudSpeechError) as ctx:
            cs.send_ex(spec, b"RIFF", post=lambda *a, **k: calls.append(1) or FakeResp())
        self.assertEqual((ctx.exception.kind, calls), ("language", []))


class TestRetryRules(unittest.TestCase):
    """A live piece: try again (up to twice, a short wait each, Retry-After honoured), then give up."""

    def err(self, kind="server", retry_after=None):
        return cs.CloudSpeechError(kind, "x", retry_after)

    def run_piece(self, outcomes, **kw):
        """attempt_with_retries over scripted outcomes (an error to raise, or a value to return)."""
        waits, calls = [], []
        now = [0.0]

        def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        def once():
            calls.append(1)
            out = outcomes[min(len(calls), len(outcomes)) - 1]
            if isinstance(out, Exception):
                now[0] += 1.0                                      # a failed request takes a second
                raise out
            return out
        value, err = cs.attempt_with_retries(once, sleep=sleep, clock=lambda: now[0], **kw)
        return value, err, waits, len(calls)

    def test_the_first_try_that_works_is_the_only_one(self):
        self.assertEqual(self.run_piece(["ok"]), ("ok", None, [], 1))

    def test_a_live_piece_gets_two_tries_not_three(self):
        # (a dead proxy must not hold the call's one worker for a minute: LIVE_RETRIES is 1)
        self.assertEqual(cs.LIVE_RETRIES, 1)
        value, err, waits, tries = self.run_piece([self.err(), "ok"])
        self.assertEqual((value, err, tries), ("ok", None, 2))
        self.assertEqual(waits, [1.0])                              # short backoff
        value, err, waits, tries = self.run_piece([self.err(), self.err("timeout"), "ok"])
        self.assertEqual((value, err.kind, tries, waits), (None, "timeout", 2, [1.0]))

    def test_more_retries_are_still_possible_when_asked(self):
        value, err, waits, tries = self.run_piece([self.err(), self.err("timeout"), "ok"], retries=2)
        self.assertEqual((value, err, tries), ("ok", None, 3))
        self.assertEqual(waits, [1.0, 2.5])

    def test_it_gives_up_after_the_retries_with_the_last_error(self):
        value, err, waits, tries = self.run_piece([self.err("server"), self.err("busy"), self.err("network")])
        self.assertIsNone(value)
        self.assertEqual((err.kind, tries, waits), ("busy", 2, [1.0]))
        value, err, waits, tries = self.run_piece([self.err("server"), self.err("busy"), self.err("network")],
                                                  retries=2)
        self.assertEqual((err.kind, tries, waits), ("network", 3, [1.0, 2.5]))

    def test_retry_after_is_honoured_up_to_what_a_live_piece_can_afford(self):
        value, err, waits, tries = self.run_piece([self.err("busy", 4.0), "ok"])
        self.assertEqual((value, tries, waits), ("ok", 2, [4.0]))
        value, err, waits, tries = self.run_piece([self.err("busy", 4.0), self.err("busy", 5.0), "ok"], retries=2)
        self.assertEqual((value, tries, waits), ("ok", 3, [4.0, 5.0]))
        value, err, waits, tries = self.run_piece([self.err("busy", 30.0), "ok"])          # longer than it can wait
        self.assertEqual((value, err.kind, tries, waits), (None, "busy", 1, []))             # the next piece tries again
        value, err, waits, tries = self.run_piece([self.err("busy", 0.2), "ok"])
        self.assertEqual((value, waits), ("ok", [1.0]))                                      # never less than the backoff

    def test_an_error_a_retry_cannot_fix_is_not_retried(self):
        for kind in ("signed_out", "pro_required", "cap", "bad_key", "no_credit", "language", "privacy", "no_key",
                     "no_token", "bad_request"):
            value, err, waits, tries = self.run_piece([self.err(kind), "ok"])
            self.assertEqual((value, err.kind, tries, waits), (None, kind, 1, []), kind)

    def test_off_language_is_retried(self):
        value, err, _waits, tries = self.run_piece([self.err("off_language"), "ok"])
        self.assertEqual((value, tries), ("ok", 2))

    def test_a_cancelled_take_stops_at_once(self):
        value, err, waits, tries = self.run_piece([self.err(), "ok"], stop=lambda: True)
        self.assertEqual((value, err.kind, tries, waits), (None, "server", 1, []))

    def test_the_whole_piece_has_a_budget(self):
        value, err, waits, tries = self.run_piece([self.err(), self.err(), "ok"], budget=1.5)
        self.assertEqual((value, tries), (None, 1))                 # the 1 s it took + the 1 s wait would pass the budget
        value, err, waits, tries = self.run_piece([self.err(), self.err(), "ok"], budget=2.0)
        self.assertEqual((value, tries), (None, 2))                 # the first wait fits, the second does not
        value, err, waits, tries = self.run_piece([self.err(), self.err(), "ok"], retries=0)
        self.assertEqual((value, tries, waits), (None, 1, []))

    def test_the_callback_hears_each_retry(self):
        seen = []
        cs.attempt_with_retries(lambda: (_ for _ in ()).throw(self.err("server")), sleep=lambda s: None,
                                clock=lambda: 0.0, on_retry=lambda n, e, w: seen.append((n, e.kind, w)))
        self.assertEqual(seen, [(1, "server", 1.0)])
        seen.clear()
        cs.attempt_with_retries(lambda: (_ for _ in ()).throw(self.err("server")), sleep=lambda s: None,
                                retries=2, clock=lambda: 0.0,
                                on_retry=lambda n, e, w: seen.append((n, e.kind, w)))
        self.assertEqual(seen, [(1, "server", 1.0), (2, "server", 2.5)])
        cs.attempt_with_retries(lambda: (_ for _ in ()).throw(self.err("server")), sleep=lambda s: None,
                                clock=lambda: 0.0, on_retry=lambda *a: 1 / 0)                  # a bad callback never breaks it


if __name__ == "__main__":
    unittest.main()
