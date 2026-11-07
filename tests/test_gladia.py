from pathlib import Path

import pytest

import cloud_speech as cs
import engine_choice as ec
import settings_apply
import speech_langs
from tests.test_cloud_speech import FakePost, Resp, WAV


@pytest.mark.parametrize("profile", [ec.CLOUD_GLADIA, ec.CLOUD_GLADIA_FUSION])
def test_gladia_round_trip(profile):
    value = "cloud:" + profile
    assert ec.cloud_profile(profile) == profile
    assert ec.dictation_speech_source(value) == "cloud"
    change = ec.set_dictation_speech({}, value, pro=True)
    assert change["ok"]
    cfg = change["changes"]
    assert cfg[ec.SPEECH_PROFILE_KEY] == profile
    assert ec.dictation_speech_value(cfg) == value
    assert value in {r["value"] for r in ec.dictation_speech_rows({}, pro=True)}
    assert ec.set_dictation_speech({}, value, pro=False)["problem"] == "pro"
    assert ec.set_dictation_speech({"privacy_mode": True}, value, pro=True)["problem"] == "privacy_mode"
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
    for model in ("solaria-1", "solaria-3", "solaria-fusion"):
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
    for text in ('"solaria-1": "gladia"', '"solaria-3": "gladia"', '"solaria-fusion": "gladia"',
                 'orderFor("STT_GLADIA_ORDER", ["solaria-3", "solaria-1"])',
                 'orderFor("STT_GLADIA_FUSION_ORDER", ["solaria-fusion", "solaria-1"])',
                 '["en", "fr", "de", "es", "it"].includes(language)',
                 'await transcribeGladia(bytes, language, languages, id, t)'):
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
