"""Meetings' own "Transcribes with" (founder 2026-11-11: a meeting set to Armenian was written by Whisper Base on
this computer - "this is not good as there is no Armenian"). The Meetings card has the Live Assistant's row
(ui/pages/live_speech.py, meeting=True): the same rows and Automatic rules, read for a meeting - its own pick
(cfg meeting_speech_model) and the meeting language.
"""
from PySide6.QtCore import QPoint

from tests_qt.conftest import make_stub_app
from tests_qt.test_page_meetings import _open, meetings_root, shell  # noqa: F401  (the Meetings page fixtures)

BASE = {"backend": "local", "whisper_model": "base", "language": "en", "meeting_language": "",
        "meeting_speech_model": "", "live_assist_speech_model": "", "live_assist_language": ""}


def _row(tier="pro", **cfg):
    app = make_stub_app(tier)
    app.cfg.update(BASE)
    app.cfg.update(cfg)

    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        app.cfg[key] = value
        return True
    app.set_setting = set_setting
    app.set_settings = lambda updates: {k: set_setting(k, v) for k, v in updates.items()}
    from ui.pages.live_speech import LiveSpeechRow
    return app, LiveSpeechRow(app, meeting=True)


def test_automatic_moves_an_armenian_meeting_to_gusan_cloud_on_pro(qapp):
    app, row = _row("pro", meeting_language="hy")
    assert row.combo.currentData() == ""                                  # Automatic
    assert row.line.text().endswith("for Armenian")
    assert "Cloud" in row.line.text()
    assert not row.note.isVisibleTo(row)                                  # no "Whisper isn't reliable" hint


def test_it_follows_the_meeting_language_not_the_call_language(qapp):
    app, row = _row("pro", meeting_language="fr", live_assist_language="hy")
    assert "Armenian" not in row.line.text()
    assert row.line.text().startswith("Whisper Base")                     # Dictation's, as before
    app.cfg["meeting_language"] = "hy"
    row.on_setting_changed("meeting_language")
    assert row.line.text().endswith("for Armenian")
    # "" = the dictation language
    app.cfg.update(meeting_language="", language="hy")
    row.on_setting_changed("language")
    assert row.line.text().endswith("for Armenian")


def test_a_pick_is_the_meetings_own_never_the_live_assistants(qapp):
    app, row = _row("pro")
    row.combo.setCurrentIndex(row.combo.findData("cloud:best"))
    assert ("set_setting", "meeting_speech_model", "cloud:best") in app.calls
    assert app.cfg["live_assist_speech_model"] == ""
    row.refresh()
    assert row.combo.currentData() == "cloud:best"
    assert row.watches("meeting_speech_model") and row.watches("meeting_language")
    assert not row.watches("live_assist_speech_model")


def test_without_pro_an_armenian_meeting_says_what_does_better(qapp, monkeypatch):
    import engine_choice
    monkeypatch.setattr(engine_choice, "hy_usable", lambda *a, **k: False)
    app, row = _row("free", meeting_language="hy")
    assert row.line.text().startswith("Whisper Base")
    assert "isn't reliable for Armenian" in row.note.text()
    # Gusan Cloud asks for Pro, and nothing is saved
    row.combo.setCurrentIndex(row.combo.findData("cloud"))
    assert not any(c[:2] == ("set_setting", "meeting_speech_model") for c in app.calls)


def test_the_meetings_card_has_it_between_the_language_and_the_notes(shell, meetings_root):
    win, app = shell("pro")
    page = _open(win)
    rail = page.list_view.rail
    assert rail.speech.row.isVisibleTo(rail)

    def y(w):
        return w.mapTo(rail, QPoint(0, 0)).y()
    assert y(rail.lang_combo) < y(rail.speech.combo) < y(rail.opt_cloud)
    assert rail.speech.combo.accessibleName() == "Transcribes with"
