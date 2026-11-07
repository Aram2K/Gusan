from pathlib import Path

import pytest

import cloud_speech as cs
import engine_choice as ec
import settings_apply
import speech_langs
from tests.test_cloud_speech import FakePost, Resp, WAV


@pytest.mark.parametrize("profile", [ec.CLOUD_GLADIA])
def test_gladia_round_trip(profile):
    value = "cloud:" + profile
    assert ec.cloud_profile(profile) == profile
    assert ec.set_file_model({}, value, pro=True)["changes"] == {ec.FILE_KEY: value}
    assert ec.file_session({ec.FILE_KEY: value}, True) == ("managed", profile)
    assert ec.file_model_id({ec.FILE_KEY: value}) == profile
    assert ec.file_engine({ec.FILE_KEY: value}, pro=True)["model_id"] == profile
    assert ec.file_session({ec.FILE_KEY: value}, False) == ("local", None)
    assert ec.file_session({ec.FILE_KEY: value, "privacy_mode": True}, True) == ("local", None)
    assert ec.set_file_model({}, value, pro=False)["problem"] == "pro"
    assert ec.set_file_model({"privacy_mode": True}, value, pro=True)["problem"] == "privacy_mode"
    assert value in {r["value"] for r in ec.file_rows({}, pro=True)}
    assert settings_apply._file_model_ok(value, False)
    assert not settings_apply._file_model_ok(value, True)
    spec = cs.spec_from_cfg({"managed_provider": "mistral"}, "managed", profile,
                            language="en", token_fn=lambda: "token")
    post = FakePost(Resp(200, {"text": "hello", "model": "solaria-1"}))
    assert cs.send(spec, WAV, post=post)[0] == "hello"
    assert post.calls[0][1]["json"]["model"] == profile
    assert post.calls[0][1]["json"]["provider"] == "gemini"


def test_gladia_empty_and_language_answers():
    assert ec.cloud_profile("") == ec.CLOUD_FAST
    assert ec.file_session({}, False) == ("local", None)
    for model in ("solaria-1", "solaria-3"):
        assert ec.model_display_name(model).startswith("Gladia Solaria")
        assert speech_langs.answer_problem("Hello there", "en", model) == ""
    assert speech_langs.answer_problem("Բարեւ աշխարհ", "hy", "solaria-1") == ""


@pytest.mark.parametrize("model", ["solaria-1", "solaria-3", "solaria-fusion",
                                   "gladia", "Gladia Solaria-1", "Gladia Solaria-3",
                                   "Gladia Solaria Fusion"])
def test_gladia_capable_family(model):
    assert speech_langs.family_of_model(model) == speech_langs.FAMILY_GLADIA
    assert speech_langs.is_capable(model)
    assert speech_langs.answer_problem("Բարեւ աշխարհ", "hy", model) == ""
    assert speech_langs.answer_problem("Hello world this is English", "hy", model) == "script"


def test_gladia_proxy_source():
    source = (Path(__file__).resolve().parents[1] / "supabase/functions/transcribe-proxy/index.ts").read_text()
    assert 'return env("Gladia_STT") || env("GLADIA_API_KEY") || env("GLADIA_KEY") || env("GLADIA_TOKEN") || env("GLADIA_API_TOKEN");' in source
    assert '"solaria-fusion": "gladia"' not in source   # never tried: our Gladia account is refused it (403)
    for text in ('"solaria-1": "gladia"', '"solaria-3": "gladia"',
                 'orderFor("STT_GLADIA_ORDER", ["solaria-3", "solaria-1"])',
                 '["en", "fr", "de", "es", "it"].includes(language)',
                 'await transcribeGladia(bytes, language, languages, id, t, vocabulary)'):
        assert text in source
    body = source.split("async function transcribeGladia", 1)[1].split("async function transcribeElevenLabs", 1)[0]
    for text in ('form.append("audio"', '"x-gladia-key": engineKey("gladia")',
                 'languages: [language], code_switching: false', 'languages, code_switching: true',
                 'data.status === "error"', 'data.status === "done"', 'full_transcript',
                 '"no_text"', 'method: "DELETE"'):
        assert text in body
    assert body.count('redirect: "error"') == 4
    assert body.count('AbortSignal.timeout(timeoutMs)') == 1
    assert 'if (typeof text !== "string") return failed("gladia", model, 500, "no_text");' in body
    assert 'return { ok: true, text: text.trim(), model };' in body
    assert '!text.trim()' not in body
    cleanup = body.split('  } finally {', 1)[1]
    assert 'void fetch(url,' in cleanup
    assert 'if (!response.ok)' in cleanup
    assert '{ stt_cleanup_failed: "gladia", status: response.status }' in cleanup
    assert '.catch(() => { console.error(JSON.stringify({ stt_cleanup_failed: "gladia", status: 0 })); });' in cleanup
    assert 'job.id' not in cleanup
    assert 'engineKey' not in cleanup


def test_there_is_no_gladia_fusion_and_a_saved_pick_of_it_is_gladia_solaria():
    """Gladia documents Solaria-1 and Solaria-3; its solaria-fusion is refused to our account (403), so "Gladia Fusion"
    (2.1.2 test builds) is gone."""
    assert ec.CLOUD_PROFILES == (ec.CLOUD_FAST, ec.CLOUD_BEST, ec.CLOUD_GLADIA)
    assert "Gladia Fusion" not in ec.CLOUD_PROFILE_NAMES.values()
    assert ec.cloud_profile("gladia-fusion") == ec.CLOUD_GLADIA
    assert "cloud:gladia-fusion" not in ec.FILE_CLOUD_VALUES
    assert "cloud:gladia-fusion" not in settings_apply.FILE_CLOUD_VALUES
    for rows in (ec.dictation_speech_rows({}, pro=True), ec.file_rows({}, pro=True)):
        assert not [r for r in rows if "fusion" in r["value"]]


def test_gladia_is_told_the_language_and_gets_the_vocabulary():
    """A named language is THE language; "multi" sends its list with switching; the person's vocabulary is Gladia's
    custom_vocabulary, and a model that refuses it gets the job once more without it."""
    source = (Path(__file__).resolve().parents[1] / "supabase/functions/transcribe-proxy/index.ts").read_text()
    body = source.split("async function transcribeGladia", 1)[1].split("async function transcribeElevenLabs", 1)[0]
    assert "explicit ? { languages: [language], code_switching: false }" in body
    assert 'language === "multi" && languages?.length ? { languages, code_switching: true }' in body
    assert "custom_vocabulary: true, custom_vocabulary_config: { vocabulary, default_intensity: 0.5 }" in body
    assert "let init = await start(vocabulary.length > 0);" in body
    assert "(init.status === 400 || init.status === 422)" in body and "init = await start(false);" in body
    helper = source.split("function vocabularyOf", 1)[1].split("async function transcribeGladia", 1)[0]
    for text in ("Array.isArray(body?.vocabulary)", 'typeof v !== "string"', "VOCAB_TERM_MAX", "out.length >= VOCAB_MAX"):
        assert text in helper
    assert "const vocabulary = vocabularyOf(body);" in source
    # ... and the app sends it (cloud_speech: the request body's "vocabulary")
    assert 'body["vocabulary"] = list(spec.terms)' in (Path(__file__).resolve().parents[1] / "cloud_speech.py").read_text()


def test_dictation_does_not_offer_gladia_and_a_saved_pick_is_fast():
    """Gladia's recorded-audio API takes 2-5 s a dictation piece (upload, job, polling), Fast 0.25-0.4 s (the
    server's log, 2026-11-07): Gladia is for Audio to text only."""
    assert "cloud:gladia" not in {r["value"] for r in ec.dictation_speech_rows({}, pro=True)}
    assert "cloud:gladia" not in {r["value"] for r in ec.live_speech_rows({}, True)}
    for saved in ("gladia", "gladia-fusion"):
        assert ec.dictation_speech_value({"backend": "managed", ec.SPEECH_PROFILE_KEY: saved}) == "cloud"
    assert "cloud:gladia" in {r["value"] for r in ec.file_rows({}, pro=True)}


def test_a_saved_gladia_pick_is_healed_on_every_load():
    """The heal used to sit in the schema migration, which returns at once for a current file: it never ran."""
    assert ec.FILE_KEY_NAME == ec.FILE_KEY and ec.SPEECH_PROFILE_KEY_NAME == ec.SPEECH_PROFILE_KEY
    cfg = {"config_schema_version": 3, "managed_speech_profile": "gladia-fusion", "file_model": "cloud:gladia-fusion"}
    ec.heal_speech_settings(cfg)
    assert cfg["managed_speech_profile"] == "fast" and cfg["file_model"] == "cloud:gladia"
    cfg = {"managed_speech_profile": "Gladia"}
    assert ec.heal_speech_settings(cfg)["managed_speech_profile"] == "fast"
    keep = {"managed_speech_profile": "best", "file_model": "cloud:gladia"}
    assert ec.heal_speech_settings(dict(keep)) == keep
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    load = source.split("def load_config():", 1)[1].split("\ndef ", 1)[0]
    assert "engine_choice.heal_speech_settings(loaded)" in load
    assert "asked_model = engine_choice.CLOUD_FAST" in source        # the request itself never asks Gladia
