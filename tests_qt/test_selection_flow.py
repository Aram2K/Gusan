"""Smart Actions on the selected text, in the app: when the selection is used, and where the result goes - an answer
in a card (copied, never pasted), a change pasted where the cursor is, or copied when nothing can be typed there."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

import actions
import selection_reader as sr
import smart_prompt


@pytest.fixture
def controller():
    from main import AppController
    return AppController


class FakeJob:
    def __init__(self, sel):
        self.sel = sel
        self.asked = 0

    def result(self, timeout=None):
        self.asked += 1
        return self.sel


def test_the_selection_goes_to_an_ai_that_can_read_it(controller):
    job = FakeJob(sr.Selection("hello", "uia"))
    sel = controller._selection_for_take(None, job, actions.API_MANAGED_ID, {"_managed_token": "t"})
    assert sel.text == "hello"


@pytest.mark.parametrize("engine,config", [
    (actions.RULE_BASED_ID, {}),                                                # the built-in formatter
    (actions.API_MANAGED_ID, {}),                                               # no Pro token
    (actions.API_MANAGED_ID, {"_managed_token": "t", "privacy_mode": True}),    # Privacy mode
    (actions.API_OPENAI_ID, {}),                                                # no key saved
])
def test_no_ai_that_can_read_it_means_no_waiting_for_the_selection(controller, engine, config):
    job = FakeJob(sr.Selection("hello", "uia"))
    assert controller._selection_for_take(None, job, engine, config) is None
    assert job.asked == 0


def test_an_on_computer_model_reads_the_start_of_a_long_selection(controller, monkeypatch):
    import local_llm
    import main
    local = next(k for k, v in actions.ACTION_MODELS.items() if v.get("kind") == "local_llm")
    monkeypatch.setattr(local_llm, "model_downloaded", lambda _m: True)
    sel = controller._selection_for_take(None, FakeJob(sr.Selection("x" * 9000, "copy")), local, {})
    assert len(sel.text) == main.LOCAL_SELECTION_CHARS and sel.cut


def _stub(controller):
    calls = []
    overlay = SimpleNamespace(call_soon=lambda fn, *a, **k: fn(*a, **k),
                              show_done=lambda pasted, kind=None: calls.append(("done", pasted, kind)))
    app = SimpleNamespace(overlay=overlay, calls=calls,
                          _run_on_gui=lambda fn, *a: fn(*a),
                          _paste_via_clipboard=lambda text, restore=True, rich=False:
                              calls.append(("paste", text, restore, rich)) or True)
    return app


@pytest.fixture
def routed(controller, monkeypatch):
    copied, cards = [], []
    monkeypatch.setattr(controller, "_rich_copy", staticmethod(lambda text: copied.append(text) or text))
    monkeypatch.setattr(controller, "_show_answer_card", lambda self, text, title="Answer": cards.append((title, text)))
    return copied, cards


def test_an_answer_is_shown_and_copied_never_pasted(controller, routed):
    copied, cards = routed
    app = _stub(controller)
    controller._deliver_selection_result(app, smart_prompt.KIND_ANSWER, "It means *see you tomorrow*.")
    assert cards == [("Answer", "It means *see you tomorrow*.")]
    assert copied == ["It means *see you tomorrow*."]
    assert not any(c[0] == "paste" for c in app.calls)
    assert ("done", False, None) in app.calls


def test_a_change_is_pasted_where_the_cursor_is_and_stays_on_the_clipboard(controller, routed, monkeypatch):
    copied, cards = routed
    monkeypatch.setattr(sr, "focus_editable", lambda: None)            # can't tell (macOS): paste
    app = _stub(controller)
    controller._deliver_selection_result(app, smart_prompt.KIND_CHANGE, "Hello everyone.", "translate")
    assert ("paste", "Hello everyone.", False, True) in app.calls     # restore=False: the result stays copied
    assert cards == [] and copied == []
    assert ("done", True, "translate") in app.calls


def test_a_change_with_nowhere_to_type_is_copied_and_shown(controller, routed, monkeypatch):
    copied, cards = routed
    monkeypatch.setattr(sr, "focus_editable", lambda: False)           # a web page: nothing takes typing
    app = _stub(controller)
    controller._deliver_selection_result(app, smart_prompt.KIND_CHANGE, "Hello everyone.", "translate")
    assert not any(c[0] == "paste" for c in app.calls)
    assert copied == ["Hello everyone."] and cards == [("Copied", "Hello everyone.")]


def test_the_test_suite_never_starts_reading_a_selection():
    assert not sr.enabled()


def test_the_card_shows_the_answer_without_taking_the_keyboard(qapp):
    from ui.answer_card import AnswerCard
    card = AnswerCard()
    try:
        card.show_text("**Yes.** It is polite.\n\n- warm\n- short", "Answer")
        QApplication.processEvents()
        assert card.isVisible()
        assert card.title.text() == "Answer"
        assert "**" not in card.body.toPlainText() and "Yes." in card.body.toPlainText()
        assert card.windowFlags() & Qt.WindowDoesNotAcceptFocus
        assert card.testAttribute(Qt.WA_ShowWithoutActivating)
        card.copy()
        assert QGuiApplication.clipboard().text() == "**Yes.** It is polite.\n\n- warm\n- short"
        assert card.title.text() == "Copied"
    finally:
        card.hide()
        card.deleteLater()


def test_a_new_smart_action_closes_the_card(controller):
    hidden = []
    app = SimpleNamespace(_answer_card=SimpleNamespace(hide=lambda: hidden.append(1)))
    controller._hide_answer_card(app)
    assert hidden == [1]
    controller._hide_answer_card(SimpleNamespace())                    # no card yet: nothing to do


def test_history_never_keeps_the_selected_text(controller):
    src = open(controller.__init__.__code__.co_filename, encoding="utf-8").read()
    i = src.index("if action_mode == actions.ACTION_SMART_SELECTION:\n                # The pages know")
    block = src[i:i + 400]
    assert '"said": text' in block and "sel.text" not in block and "action_input" not in block
