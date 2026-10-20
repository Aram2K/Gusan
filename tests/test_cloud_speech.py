"""cloud_speech: one Audio to text piece on Gusan Cloud or an own key.

Pure: fake post functions, no network, no Qt. The parity check with
main.MANAGED_PROXY_URL / main.MISTRAL_LANGS imports main under test_core's
PySide6 stand-ins (the whole tests/ run); run alone it reads the same
literals from main.py's source instead.
"""
import ast
import base64
import dataclasses
import importlib
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cloud_speech as cs  # noqa: E402
import engine_choice as ec  # noqa: E402
import speech_langs  # noqa: E402
import vocabulary  # noqa: E402

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt fake-piece"
GKEY = "AIzaSECRET-google-key-123"
MKEY = "mk-SECRET-mistral-key-456"
TOKEN = "eyJ-SECRET-access-token-789"
SECRETS = (GKEY, MKEY, TOKEN)


# Named like requests' exceptions: test_core puts a stand-in in place of
# requests for the whole tests/ run, so the real classes aren't always there.
class Timeout(OSError):
    pass


class ReadTimeout(Timeout):
    pass


class ChunkedEncodingError(OSError):
    pass


def real_requests_exceptions():
    """requests.exceptions, or None under test_core's stand-in."""
    try:
        rx = importlib.import_module("requests.exceptions")
        if isinstance(rx.ReadTimeout, type) and issubclass(rx.ReadTimeout, Exception):
            return rx
    except Exception:
        pass
    return None


class Resp:
    def __init__(self, status=200, payload=None, text=None, headers=None):
        self.status_code = status
        self._payload = payload
        if text is None:
            text = json.dumps(payload) if isinstance(payload, (dict, list)) else ""
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class FakePost:
    """Records every call; answers with ``resp`` (or raises ``exc``)."""

    def __init__(self, resp=None, exc=None):
        self.resp = resp if resp is not None else Resp(200, {"text": "hello"})
        self.exc = exc
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.resp


def gusan(cfg=None, provider="gemini", language="auto", token=TOKEN):
    return cs.spec_from_cfg(cfg if cfg is not None else {}, "managed", provider,
                            language=language, token_fn=lambda: token)


def google(cfg=None, model=None, language="auto"):
    cfg = dict({"google_api_key": GKEY}, **(cfg or {}))
    return cs.spec_from_cfg(cfg, "google", model, language=language)


def mistral(cfg=None, model=None, language="auto"):
    cfg = dict({"mistral_api_key": MKEY}, **(cfg or {}))
    return cs.spec_from_cfg(cfg, "mistral", model, language=language)


# ── the job's settings ───────────────────────────────────────────────────────

class TestSpecFromCfg(unittest.TestCase):
    def test_gusan_cloud_runs_the_providers_real_model(self):
        s = gusan()
        self.assertEqual((s.engine, s.kind, s.name, s.provider), ("gusan", "cloud", "Gusan Cloud", "gemini"))
        self.assertEqual((s.model_id, s.model_name, s.label),
                         ("gemini-2.5-flash", "Gemini 2.5 Flash", "Gusan Cloud · Gemini 2.5 Flash"))
        self.assertEqual(s.api_key, "")
        m = gusan(provider="mistral")
        self.assertEqual((m.provider, m.model_id, m.model_name, m.label),
                         ("mistral", "voxtral-mini-latest", "Voxtral Mini", "Gusan Cloud · Voxtral Mini"))

    def test_gusan_provider_from_real_id_or_cfg(self):
        self.assertEqual(cs.spec_from_cfg({}, "managed", "voxtral-mini-latest").provider, "mistral")
        self.assertEqual(cs.spec_from_cfg({"managed_provider": "mistral"}, "managed").provider, "mistral")
        self.assertEqual(cs.spec_from_cfg({"managed_provider": "Mistral"}, "cloud", None).provider, "mistral")
        self.assertEqual(cs.spec_from_cfg({"managed_provider": "openai"}, "managed").provider, "gemini")

    def test_model_id_is_what_engine_choice_stores(self):
        for provider in ("gemini", "mistral"):
            cfg = {"managed_provider": provider}
            self.assertEqual(cs.spec_from_cfg(cfg, *ec.file_session(dict(cfg, file_model="cloud"), True)).model_id,
                             ec.file_model_id(cfg, "cloud"))
        cfg = {"google_api_key": GKEY, "google_stt_model": "gemini-2.5-flash-lite", "file_model": "own:google"}
        self.assertEqual(cs.spec_from_cfg(cfg, *ec.file_session(cfg, False)).model_id, "gemini-2.5-flash-lite")
        cfg = {"mistral_api_key": MKEY, "mistral_stt_model": "voxtral-small-latest", "file_model": "own:mistral"}
        self.assertEqual(cs.spec_from_cfg(cfg, *ec.file_session(cfg, False)).model_id, "voxtral-mini-latest")

    def test_own_keys_read_the_key_and_model(self):
        g = google({"google_api_key": f"  {GKEY}  "})
        self.assertEqual((g.engine, g.kind, g.name, g.provider), ("google", "own_key", "Google Gemini", "gemini"))
        self.assertEqual((g.api_key, g.model_id, g.model_name), (GKEY, "gemini-2.5-flash", "Gemini 2.5 Flash"))
        self.assertEqual(google({"google_stt_model": "gemini-2.5-pro"}).model_id, "gemini-2.5-pro")
        self.assertEqual(google(model="gemini-3.5-flash").model_name, "Gemini 3.5 Flash")
        m = mistral({"mistral_stt_model": "voxtral-mini-2602"})
        self.assertEqual((m.engine, m.kind, m.name, m.api_key, m.model_id), ("mistral", "own_key", "Mistral",
                                                                          MKEY, "voxtral-mini-2602"))
        self.assertEqual(mistral(model="voxtral-large-latest").model_id, "voxtral-mini-latest")   # healed
        self.assertEqual(cs.spec_from_cfg({"google_api_key": GKEY}, "own:google").engine, "google")
        self.assertEqual(cs.spec_from_cfg({"mistral_api_key": MKEY}, "own:mistral").engine, "mistral")

    def test_labels_are_speech_engines(self):
        for backend, spec in (("google", google({"google_stt_model": "gemini-2.5-flash-lite"})),
                              ("mistral", mistral()), ("managed", gusan(provider="mistral"))):
            cfg = dict(spec.cfg or {}, backend=backend, managed_provider=spec.provider)
            res = ec.speech_engine(cfg, True, "audio_to_text")
            self.assertEqual((spec.label, spec.model_name), (res["label"], res["model_name"]))

    def test_languages(self):
        self.assertEqual(gusan(language="auto").languages, ())
        self.assertEqual(gusan(language=None).language, "auto")
        self.assertEqual(gusan(language="fr").languages, ("fr",))
        self.assertEqual(gusan(language="it").languages, ())          # not one the app offers
        mix = {"mix_languages": ["hy", "en"]}
        self.assertEqual(gusan(mix, language="multi").languages, ("en", "hy"))     # picker order
        self.assertEqual(gusan({"mix_languages": ["hy"]}, language="multi").languages, ())

    def test_vocabulary_is_gated_but_spellings_stay_local(self):
        cfg = {"vocabulary": ["Gusan", "Doctolib"]}
        s = gusan(cfg)
        self.assertEqual(s.terms, ("Gusan", "Doctolib"))
        self.assertEqual(s.vocab_hint, vocabulary.cloud_transcription_hint(cfg))
        self.assertIn("Doctolib", s.vocab_hint)
        self.assertEqual(s.spellings, ("Gusan", "Doctolib"))
        for gate in ({"privacy_mode": True}, {"vocabulary_share_with_cloud": False}):
            p = gusan(dict(cfg, **gate))
            self.assertEqual((p.terms, p.vocab_hint), ((), ""))
            self.assertEqual(p.spellings, ("Gusan", "Doctolib"))       # fixed on this computer

    def test_unknown_backend(self):
        for backend in ("local", "", None, "openai"):
            with self.assertRaises(ValueError):
                cs.spec_from_cfg({}, backend, None)

    def test_keeps_the_live_cfg(self):
        cfg = {"google_api_key": GKEY}
        self.assertIs(cs.spec_from_cfg(cfg, "google").cfg, cfg)

    def test_repr_has_no_secrets_or_vocabulary(self):
        cfg = {"google_api_key": GKEY, "vocabulary": ["Akopian"]}
        for spec in (cs.spec_from_cfg(cfg, "google"), gusan(cfg)):
            text = repr(spec)
            for secret in SECRETS + ("Akopian", "<function", "<lambda"):
                self.assertNotIn(secret, text)
            self.assertIn(spec.model_id, text)
        self.assertNotIn(GKEY, str(dataclasses.replace(google(), language="fr")))


# ── building requests ────────────────────────────────────────────────────────

class TestBuildRequest(unittest.TestCase):
    def test_gusan_cloud_body(self):
        spec = gusan({"mix_languages": ["en", "hy"]}, language="multi")
        req = cs.build_request(spec, WAV, TOKEN)
        self.assertEqual(req["url"], cs.PROXY_URL)
        self.assertEqual(req["headers"], {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(req["timeout"], (10, 90))
        body = req["json"]
        self.assertEqual(set(body), {"audio", "sample_rate", "language", "provider", "languages"})
        self.assertEqual(base64.b64decode(body["audio"]), WAV)
        self.assertEqual((body["sample_rate"], body["language"], body["provider"], body["languages"]),
                         (16000, "multi", "gemini", ["en", "hy"]))
        self.assertEqual(cs.build_request(gusan(language="ru"), WAV, TOKEN)["json"]["languages"], ["ru"])
        self.assertEqual(cs.build_request(gusan(provider="mistral"), WAV, TOKEN)["json"]["provider"],
                         "mistral")

    def test_gusan_cloud_vocabulary_only_when_shared(self):
        cfg = {"vocabulary": "Gusan, Doctolib"}
        self.assertEqual(cs.build_request(gusan(cfg), WAV, TOKEN)["json"]["vocabulary"], ["Gusan", "Doctolib"])
        self.assertNotIn("vocabulary", cs.build_request(gusan(dict(cfg, privacy_mode=True)), WAV, TOKEN)["json"])

    def test_gemini_key_only_in_the_header(self):
        req = cs.build_request(google(), WAV)
        self.assertEqual(req["url"], "https://generativelanguage.googleapis.com/v1beta/models/"
                                     "gemini-2.5-flash:generateContent")
        self.assertNotIn("key=", req["url"])
        self.assertNotIn(GKEY, req["url"])
        self.assertNotIn(GKEY, json.dumps(req["json"]))
        self.assertEqual(req["headers"], {"x-goog-api-key": GKEY})
        self.assertEqual(req["timeout"], (10, 120))
        self.assertEqual(cs.build_request(google(), WAV, "other-key")["headers"], {"x-goog-api-key": "other-key"})

    def test_gemini_prompt_and_audio(self):
        cfg = {"vocabulary": ["Doctolib"], "mix_languages": ["hy", "en"]}
        spec = google(cfg, language="multi")
        parts = cs.build_request(spec, WAV)["json"]["contents"][0]["parts"]
        expected = (cs.PROMPT + speech_langs.cloud_hint("multi", ["en", "hy"])
                    + vocabulary.cloud_transcription_hint(spec.cfg))
        self.assertEqual(parts[0]["text"], expected)
        self.assertIn("Armenian", parts[0]["text"])
        self.assertIn("Doctolib", parts[0]["text"])
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "audio/wav")
        self.assertEqual(base64.b64decode(parts[1]["inline_data"]["data"]), WAV)
        for lang in ("auto", "hy", "en"):
            text = cs.build_request(google(language=lang), WAV)["json"]["contents"][0]["parts"][0]["text"]
            self.assertEqual(text, cs.PROMPT + speech_langs.cloud_hint(lang, []))

    def test_thinking_off_only_for_gemini_2_5_flash(self):
        def gen(model):
            return cs.build_request(google(model=model), WAV)["json"]["generationConfig"]
        for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-flash-preview-09-2025"):
            self.assertEqual(gen(model), {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}}, model)
        for model in ("gemini-2.5-pro", "gemini-3.5-flash", "gemini-3.1-flash-lite"):
            self.assertEqual(gen(model), {"temperature": 0}, model)

    def test_mistral_multipart_and_language_rule(self):
        req = cs.build_request(mistral(language="fr"), WAV)
        self.assertEqual(req["url"], "https://api.mistral.ai/v1/audio/transcriptions")
        self.assertEqual(req["headers"], {"Authorization": f"Bearer {MKEY}"})
        self.assertEqual(req["files"], {"file": ("audio.wav", WAV, "audio/wav")})
        self.assertEqual(req["data"], {"model": "voxtral-mini-latest", "language": "fr"})
        self.assertEqual(req["timeout"], (10, 120))
        self.assertNotIn("json", req)
        for lang in ("auto", "multi", "hy"):
            self.assertEqual(cs.build_request(mistral(language=lang), WAV)["data"],
                             {"model": "voxtral-mini-latest"}, lang)
        for lang in ("en", "ar", "ru", "de", "es"):
            self.assertEqual(cs.build_request(mistral(language=lang), WAV)["data"]["language"], lang)

    def test_pure(self):
        spec = gusan(token="never-called")
        calls = []
        spec = dataclasses.replace(spec, token_fn=lambda: calls.append(1))
        self.assertEqual(cs.build_request(spec, WAV, TOKEN), cs.build_request(spec, WAV, TOKEN))
        self.assertEqual(calls, [])
        self.assertEqual(cs.build_request(mistral(), bytearray(WAV))["files"]["file"][1], WAV)
        with self.assertRaises(ValueError):
            cs.build_request(cs.Spec(engine="openai"), WAV)

    def test_prompt_words_are_dictations(self):
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertIn(cs.PROMPT, strings)


# ── reading answers ──────────────────────────────────────────────────────────

class TestParseResponse(unittest.TestCase):
    def test_gemini_joins_parts(self):
        data = {"candidates": [{"content": {"parts": [{"text": " Hello "}, {"text": "there. "},
                                                      {"text": "thinking", "thought": True}]},
                                "finishReason": "STOP"}]}
        self.assertEqual(cs.parse_response(google(), data), ("Hello there.", None))
        self.assertEqual(cs.parse_response(google(language="multi"), data), ("Hello there.", None))
        self.assertEqual(cs.parse_response(google(language="fr"), data), ("Hello there.", "fr"))

    def test_gemini_no_candidates_is_no_words(self):
        with self.assertLogs("transcribe.cloud", "INFO") as logs:
            self.assertEqual(cs.parse_response(google(), {"promptFeedback": {"blockReason": "SAFETY"}}),
                             ("", None))
        self.assertIn("SAFETY", "\n".join(logs.output))
        with self.assertLogs("transcribe.cloud", "INFO") as logs:
            self.assertEqual(cs.parse_response(google(), {"candidates": [{"finishReason": "RECITATION"}]}),
                             ("", None))
        self.assertIn("RECITATION", "\n".join(logs.output))

    def test_mistral_text_and_detected_language(self):
        self.assertEqual(cs.parse_response(mistral(), {"text": " Bonjour. ", "language": "fr"}),
                         ("Bonjour.", "fr"))
        self.assertEqual(cs.parse_response(mistral(), {"text": "Hi", "language": None}), ("Hi", None))
        self.assertEqual(cs.parse_response(mistral(language="de"), {"text": "Hallo"}), ("Hallo", "de"))

    def test_proxy_lang_is_an_echo(self):
        self.assertEqual(cs.parse_response(gusan(), {"text": " Hi. ", "lang": "en"}), ("Hi.", None))
        self.assertEqual(cs.parse_response(gusan(language="multi"), {"text": "Hi", "lang": "multi"}),
                         ("Hi", None))
        self.assertEqual(cs.parse_response(gusan(language="hy"), {"text": "Բարև", "lang": "hy"}),
                         ("Բարև", "hy"))
        self.assertEqual(cs.parse_response(gusan(), {"text": None}), ("", None))

    def test_odd_answers(self):
        for data in (None, [], "text", {}):
            self.assertEqual(cs.parse_response(gusan(), data)[0], "")
        self.assertEqual(cs.parse_response(google(), {"candidates": [None]})[0], "")
        self.assertEqual(cs.parse_response(google(), {"candidates": [{"content": {"parts": None}}]})[0], "")


# ── errors ───────────────────────────────────────────────────────────────────

class TestClassify(unittest.TestCase):
    def kind(self, spec, status, body="", headers=None):
        err = cs.classify(spec, status=status, body=body, headers=headers)
        return err.kind, err.retryable, err.retry_after

    def test_gusan_cloud_table(self):
        g = gusan()
        self.assertEqual(self.kind(g, 401, {"error": "unauthorized"}), ("signed_out", False, None))
        self.assertEqual(self.kind(g, 403, '{"error":"pro_required"}'), ("pro_required", False, None))
        self.assertEqual(self.kind(g, 429, '{"error":"quota_exceeded"}', {"Retry-After": "5"}),
                         ("cap", False, None))
        self.assertEqual(self.kind(g, 400, '{"error":"no_audio"}'), ("bad_request", False, None))
        self.assertEqual(self.kind(g, 404, "not found"), ("bad_request", False, None))
        self.assertEqual(self.kind(g, 408), ("timeout", True, None))
        for status in (500, 502, 503, 504):
            self.assertEqual(self.kind(g, status, '{"error":"stt_failed"}'), ("server", True, None))
        self.assertEqual(self.kind(g, 503, "", {"Retry-After": "3"}), ("server", True, 3.0))

    def test_own_key_table(self):
        for spec in (google(), mistral()):
            self.assertEqual(self.kind(spec, 401), ("bad_key", False, None))
            self.assertEqual(self.kind(spec, 403, "PERMISSION_DENIED"), ("bad_key", False, None))
            self.assertEqual(self.kind(spec, 402, "Insufficient credit"), ("no_credit", False, None))
            self.assertEqual(self.kind(spec, 429), ("rate_limit", True, None))
            self.assertEqual(self.kind(spec, 429, "", {"Retry-After": "7"}), ("rate_limit", True, 7.0))
            self.assertEqual(self.kind(spec, 429, "", {"retry-after": "120"}), ("rate_limit", True, 60.0))
            self.assertEqual(self.kind(spec, 429, "", {"Retry-After": "soon"}), ("rate_limit", True, None))
            self.assertEqual(self.kind(spec, 429, "", {"Retry-After": "-4"}), ("rate_limit", True, None))
            self.assertEqual(self.kind(spec, 400, '{"message": "Language hy is not supported"}'),
                             ("language", False, None))
            self.assertEqual(self.kind(spec, 422, b'{"detail": "unsupported language"}'),
                             ("language", False, None))
            self.assertEqual(self.kind(spec, 400, '{"error": {"message": "Request too large"}}'),
                             ("bad_request", False, None))
            self.assertEqual(self.kind(spec, 404, "model not found"), ("bad_request", False, None))
            self.assertEqual(self.kind(spec, 408), ("timeout", True, None))
            self.assertEqual(self.kind(spec, 500), ("server", True, None))
            self.assertEqual(self.kind(spec, 503, "", {"Retry-After": "90"}), ("server", True, 60.0))

    def test_gemini_bad_key_comes_as_400(self):
        body = json.dumps({"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                     "message": "API key not valid. Please pass a valid API key.",
                                     "details": [{"reason": "API_KEY_INVALID"}]}})
        self.assertEqual(self.kind(google(), 400, body), ("bad_key", False, None))
        self.assertEqual(self.kind(google(), 400, "API key expired. Please renew the API key."),
                         ("bad_key", False, None))

    def test_exceptions(self):
        g = google()
        cases = [(ReadTimeout("read timed out"), "timeout"), (TimeoutError(), "timeout"),
                 (ConnectionError("Max retries exceeded with url: /x"), "network"),
                 (ChunkedEncodingError(), "network"), (OSError("unreachable"), "network"),
                 (ValueError("odd"), "network")]
        rx = real_requests_exceptions()
        if rx is not None:
            cases += [(rx.ReadTimeout("read timed out"), "timeout"),
                      (rx.ConnectTimeout("connect timed out"), "timeout"),
                      (rx.ConnectionError("Max retries exceeded with url: /x"), "network"),
                      (rx.ChunkedEncodingError(), "network")]
        for exc, kind in cases:
            err = cs.classify(g, exc=exc)
            self.assertEqual((err.kind, err.retryable), (kind, True), exc)
        self.assertEqual(cs.classify(g).kind, "network")

    def test_fallback_and_retry_kinds(self):
        self.assertEqual(cs.LOCAL_FALLBACK_KINDS, {"cap", "pro_required", "no_token"})
        self.assertEqual(cs.RETRY_KINDS, {"timeout", "network", "server", "rate_limit", "bad_response"})
        self.assertTrue(cs.RETRY_KINDS <= set(cs.MESSAGES))
        self.assertTrue(cs.LOCAL_FALLBACK_KINDS <= set(cs.MESSAGES))
        self.assertFalse(cs.RETRY_KINDS & cs.LOCAL_FALLBACK_KINDS)

    def test_sentences_are_short_plain_and_ours(self):
        for kind in cs.MESSAGES:
            for name in cs.NAMES.values():
                msg = cs.error_message(kind, name)
                self.assertLess(len(msg), 130, msg)
                self.assertTrue(msg.endswith("."), msg)
                self.assertNotIn("{", msg)
                low = msg.lower()
                for word in ("http", "://", "limit", "quota", "error", "api", "1 hour", "too long"):
                    self.assertNotIn(word, low, msg)
        self.assertEqual(cs.error_message("network", "Mistral"),
                         "Couldn't reach Mistral. Check your internet connection and try again.")
        self.assertEqual(cs.error_message("no_token"),
                         "Couldn't reach Gusan Cloud. Check your internet connection and try again.")
        self.assertEqual(cs.error_message("cap"),
                         "Gusan Cloud couldn't finish this file. Try again, or pick a model on this computer.")
        self.assertEqual(cs.error_message("signed_out"),
                         "Sign in to use Gusan Cloud, or pick a model on this computer.")
        self.assertEqual(cs.error_message("pro_required"),
                         "Gusan Cloud is part of Pro. Pick a model on this computer to convert this file.")
        self.assertEqual(cs.error_message("no_such_kind", "Mistral"), cs.error_message("server", "Mistral"))

    def test_messages_name_the_service(self):
        err = cs.classify(google(), status=401, body="")
        self.assertEqual(err.message, "Google Gemini didn't accept your key. Check it in Settings, "
                                      "or pick a model on this computer.")
        self.assertEqual(str(err), err.message)
        self.assertIn("Mistral", cs.classify(mistral(), status=402).message)
        self.assertIn("Gusan Cloud", cs.classify(gusan(), status=500).message)

    def test_no_provider_text_url_or_secret_in_errors(self):
        body = json.dumps({"error": {"message": f"API key {GKEY} https://example.test/x?key={GKEY}"}})
        exc = ConnectionError(f"HTTPSConnectionPool url: /x?key={GKEY} token {TOKEN}")
        errors = [cs.classify(spec, status=status, body=body, headers={"Retry-After": "2"})
                  for spec in (gusan(), google(), mistral())
                  for status in (400, 401, 402, 403, 404, 408, 422, 429, 500, 503)]
        errors += [cs.classify(spec, exc=exc) for spec in (gusan(), google(), mistral())]
        for err in errors:
            for text in (str(err), repr(err), err.message):
                for secret in SECRETS + ("example.test", "://", "HTTPSConnectionPool"):
                    self.assertNotIn(secret, text)


# ── sending ──────────────────────────────────────────────────────────────────

class TestSend(unittest.TestCase):
    def test_gusan_cloud_round_trip(self):
        post = FakePost(Resp(200, {"text": " Hello. ", "lang": "en"}))
        spec = gusan()
        self.assertEqual(cs.send(spec, WAV, post=post), ("Hello.", None))
        (url, kwargs), = post.calls
        expected = cs.build_request(spec, WAV, TOKEN)
        self.assertEqual(url, expected.pop("url"))
        self.assertEqual(kwargs, expected)

    def test_own_keys_round_trip(self):
        post = FakePost(Resp(200, {"candidates": [{"content": {"parts": [{"text": "Hi."}]}}]}))
        self.assertEqual(cs.send(google(language="en"), WAV, post=post), ("Hi.", "en"))
        self.assertEqual(post.calls[0][1]["headers"], {"x-goog-api-key": GKEY})
        post = FakePost(Resp(200, {"text": "Hola.", "language": "es"}))
        self.assertEqual(cs.send(mistral(), WAV, post=post), ("Hola.", "es"))
        self.assertEqual(post.calls[0][1]["files"]["file"], ("audio.wav", WAV, "audio/wav"))

    def test_token_asked_for_every_piece(self):
        calls = []
        spec = dataclasses.replace(gusan(), token_fn=lambda: calls.append(1) or f"tok{len(calls)}")
        post = FakePost()
        cs.send(spec, WAV, post=post)
        cs.send(spec, WAV, post=post)
        self.assertEqual(len(calls), 2)                     # no cache: auth.get_access_token caches
        self.assertEqual([c[1]["headers"]["Authorization"] for c in post.calls], ["Bearer tok1", "Bearer tok2"])

    def test_no_token_stops_before_sending(self):
        def boom():
            raise RuntimeError(f"refresh failed for {TOKEN}")
        for fn in (lambda: None, lambda: "", boom, None):
            post = FakePost()
            spec = dataclasses.replace(gusan(), token_fn=fn)
            with self.assertRaises(cs.CloudSpeechError) as ctx:
                cs.send(spec, WAV, post=post)
            self.assertEqual(ctx.exception.kind, "no_token")
            self.assertIn(ctx.exception.kind, cs.LOCAL_FALLBACK_KINDS)
            self.assertFalse(ctx.exception.retryable)
            self.assertEqual(ctx.exception.message,
                             "Couldn't reach Gusan Cloud. Check your internet connection and try again.")
            self.assertEqual(post.calls, [])

    def test_token_fn_is_called_one_at_a_time(self):
        state = {"now": 0, "most": 0}
        lock = threading.Lock()

        def token():
            with lock:
                state["now"] += 1
                state["most"] = max(state["most"], state["now"])
            time.sleep(0.02)
            with lock:
                state["now"] -= 1
            return TOKEN

        spec = dataclasses.replace(gusan(), token_fn=token)
        threads = [threading.Thread(target=cs.send, args=(spec, WAV), kwargs={"post": FakePost()})
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(state["most"], 1)

    def test_http_errors_raise_typed(self):
        cases = ((gusan(), Resp(401, {"error": "unauthorized"}), "signed_out"),
                 (gusan(), Resp(429, {"error": "quota_exceeded"}), "cap"),
                 (gusan(), Resp(403, {"error": "pro_required"}), "pro_required"),
                 (gusan(), Resp(502, {"error": "stt_failed", "detail": "x"}), "server"),
                 (google(), Resp(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, headers={"Retry-After": "4"}),
                  "rate_limit"),
                 (mistral(), Resp(401, {"message": "Unauthorized"}), "bad_key"))
        for spec, resp, kind in cases:
            with self.assertLogs("transcribe.cloud", "WARNING"):
                with self.assertRaises(cs.CloudSpeechError) as ctx:
                    cs.send(spec, WAV, post=FakePost(resp))
            self.assertEqual(ctx.exception.kind, kind)
        self.assertEqual(ctx.exception.retry_after, None)

    def test_retry_after_reaches_the_caller(self):
        resp = Resp(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, headers={"Retry-After": "4"})
        with self.assertLogs("transcribe.cloud", "WARNING"):
            with self.assertRaises(cs.CloudSpeechError) as ctx:
                cs.send(google(), WAV, post=FakePost(resp))
        self.assertEqual((ctx.exception.kind, ctx.exception.retryable, ctx.exception.retry_after),
                         ("rate_limit", True, 4.0))

    def test_post_exceptions(self):
        for exc, kind in ((ReadTimeout(), "timeout"), (ConnectionError(), "network")):
            with self.assertLogs("transcribe.cloud", "WARNING"):
                with self.assertRaises(cs.CloudSpeechError) as ctx:
                    cs.send(mistral(), WAV, post=FakePost(exc=exc))
            self.assertEqual((ctx.exception.kind, ctx.exception.retryable), (kind, True))

    def test_answer_that_isnt_json(self):
        with self.assertLogs("transcribe.cloud", "WARNING"):
            with self.assertRaises(cs.CloudSpeechError) as ctx:
                cs.send(gusan(), WAV, post=FakePost(Resp(200, None, text="<html>captive portal</html>")))
        self.assertEqual((ctx.exception.kind, ctx.exception.retryable), ("bad_response", True))

    def test_privacy_mode_turned_on_mid_job_stops_sending(self):
        for spec in (gusan({}), google(), mistral()):
            spec.cfg["privacy_mode"] = True
            post = FakePost()
            with self.assertRaises(cs.CloudSpeechError) as ctx:
                cs.send(spec, WAV, post=post)
            self.assertEqual((ctx.exception.kind, ctx.exception.retryable), ("privacy", False))
            self.assertEqual(post.calls, [])

    def test_removed_key_stops_sending(self):
        for spec, field in ((google(), "google_api_key"), (mistral(), "mistral_api_key")):
            spec.cfg[field] = "  "
            post = FakePost()
            with self.assertRaises(cs.CloudSpeechError) as ctx:
                cs.send(spec, WAV, post=post)
            self.assertEqual((ctx.exception.kind, ctx.exception.retryable), ("no_key", False))
            self.assertIn(spec.name, ctx.exception.message)
            self.assertEqual(post.calls, [])

    def test_sends_with_the_current_key(self):
        spec = google()
        spec.cfg["google_api_key"] = "AIza-new-key"
        post = FakePost(Resp(200, {"candidates": []}))
        cs.send(spec, WAV, post=post)
        self.assertEqual(post.calls[0][1]["headers"], {"x-goog-api-key": "AIza-new-key"})

    def test_spec_without_cfg_uses_its_own_key(self):
        spec = cs.Spec(engine="mistral", model_id="voxtral-mini-latest", api_key=MKEY)
        post = FakePost(Resp(200, {"text": "ok"}))
        self.assertEqual(cs.send(spec, WAV, post=post), ("ok", None))
        self.assertEqual(post.calls[0][1]["headers"], {"Authorization": f"Bearer {MKEY}"})
        with self.assertRaises(cs.CloudSpeechError) as ctx:
            cs.send(dataclasses.replace(spec, api_key=""), WAV, post=post)
        self.assertEqual(ctx.exception.kind, "no_key")

    def test_default_post_is_requests(self):
        with mock.patch("requests.post", return_value=Resp(200, {"text": "hi"})) as post:
            self.assertEqual(cs.send(gusan(), WAV), ("hi", None))
        self.assertEqual(post.call_args[0][0], cs.PROXY_URL)

    def test_secrets_never_reach_the_log(self):
        echo = f"bad key {GKEY} / {MKEY} / {TOKEN}"
        cases = ((google(), FakePost(Resp(400, {"error": {"message": echo}}))),
                 (mistral(), FakePost(Resp(500, text=echo))),
                 (gusan(), FakePost(Resp(502, text=echo))),
                 (gusan(), FakePost(Resp(200, None, text=echo))),
                 (google(), FakePost(exc=ConnectionError(echo))))
        for spec, post in cases:
            with self.assertLogs("transcribe.cloud", "INFO") as logs:
                with self.assertRaises(cs.CloudSpeechError) as ctx:
                    cs.send(spec, WAV, post=post)
            out = "\n".join(logs.output)
            self.assertIn("***", out)
            secret = TOKEN if spec.engine == "gusan" else spec.api_key
            self.assertNotIn(secret, out)
            for text in (str(ctx.exception), repr(ctx.exception)):
                for s in SECRETS:
                    self.assertNotIn(s, text)


# ── parity with main, and a light import ─────────────────────────────────────

def _main_constants():
    """(MANAGED_PROXY_URL, MISTRAL_LANGS) from main: imported under test_core's
    stand-ins, else read from main.py's source (a real import loads the app)."""
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is not None and type(qtcore).__name__ == "StubModule":
        import main
        return main.MANAGED_PROXY_URL, main.MISTRAL_LANGS
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("MANAGED_PROXY_URL", "MISTRAL_LANGS"):
                found[node.targets[0].id] = ast.literal_eval(node.value)
    return found["MANAGED_PROXY_URL"], found["MISTRAL_LANGS"]


class TestParityWithMain(unittest.TestCase):
    def test_proxy_url_and_mistral_langs_match_main(self):
        url, langs = _main_constants()
        self.assertEqual(cs.PROXY_URL, url)
        self.assertEqual(cs.MISTRAL_LANGS, langs)

    def test_imports_without_qt_numpy_requests_or_main(self):
        code = ("import sys, cloud_speech; "
                "bad = sorted({m.split('.')[0] for m in sys.modules} & "
                "{'PySide6', 'numpy', 'main', 'requests', 'engine_choice', 'ui'}); "
                "print(bad); sys.exit(1 if bad else 0)")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
