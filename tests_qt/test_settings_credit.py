"""Settings › Updates & about: the credit NVIDIA's licence (CC BY 4.0) asks for. It is shown while the
Armenian speech model is listed anywhere in the app (speech_hy.listed: on this computer, or ready to
download) and is not there - no row, no words - while the model is hidden."""
import pytest

import speech_hy
from tests_qt.test_page_settings_account import _settings, _spin, env  # noqa: F401  (the fixture and helpers)


def _credit_texts(page):
    row = page.row_credit
    return row.label.text(), row.hint_label.text(), page.credit_button.text()


def test_the_credit_is_not_there_while_the_model_is_hidden(env, monkeypatch):
    monkeypatch.setattr(speech_hy, "listed", lambda *a, **k: False)
    win, _app = env.build("free")
    page = _settings(win, section="updates")
    assert page.row_credit.isHidden()


def test_the_credit_names_nvidia_the_licence_and_links_to_the_model(env, monkeypatch):
    monkeypatch.setattr(speech_hy, "listed", lambda *a, **k: True)
    win, _app = env.build("free")
    page = _settings(win, section="updates")
    assert not page.row_credit.isHidden()
    title, hint, button = _credit_texts(page)
    assert title == "Armenian speech model"
    assert "NVIDIA" in hint and "CC BY 4.0" in hint
    assert "not made or endorsed by NVIDIA" in hint
    assert button == "NVIDIA FastConformer Armenian"
    assert page.credit_button.accessibleName() == "Open the page of NVIDIA FastConformer Armenian"
    page.credit_button.click()
    assert env.opened[-1] == speech_hy.credit()["url"]
    assert env.opened[-1].startswith("https://huggingface.co/nvidia/")
    for word in ("ONNX", "transducer", "token", "sherpa"):
        assert word.lower() not in (hint + button).lower()


def test_the_credit_follows_the_listing_when_the_model_appears(env, monkeypatch):
    state = {"listed": False}
    monkeypatch.setattr(speech_hy, "listed", lambda *a, **k: state["listed"])
    win, _app = env.build("free")
    page = _settings(win, section="updates")
    assert page.row_credit.isHidden()
    state["listed"] = True                               # the manifest named the model (or it was installed)
    page.refresh()
    _spin()
    assert not page.row_credit.isHidden()
    state["listed"] = False
    page.refresh()
    _spin()
    assert page.row_credit.isHidden()


@pytest.mark.parametrize("tier", ["free", "pro", "guest"])
def test_the_about_card_builds_with_the_credit_for_everyone(env, monkeypatch, tier):
    monkeypatch.setattr(speech_hy, "listed", lambda *a, **k: True)
    win, _app = env.build(tier)
    page = _settings(win, section="updates")
    assert page.is_built("updates")
    assert page.credit_button.text()
