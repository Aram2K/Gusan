"""Smart Actions on the selected text, in the app: when the selection is used, and where the result goes - an answer
in a card (copied, never pasted), a change pasted where the cursor is, or copied when nothing can be typed there."""
import time
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


def _stub(controller, pastes=True):
    calls = []
    overlay = SimpleNamespace(call_soon=lambda fn, *a, **k: fn(*a, **k),
                              show_done=lambda pasted, kind=None, web_sources=0: calls.append(("done", pasted, kind)))
    app = SimpleNamespace(overlay=overlay, calls=calls,
                          _run_on_gui=lambda fn, *a: fn(*a),
                          _paste_via_clipboard=lambda text, restore=True, rich=False:
                              calls.append(("paste", text, restore, rich)) or pastes)
    return app


@pytest.fixture
def routed(controller, monkeypatch):
    copied, cards = [], []
    monkeypatch.setattr(controller, "_rich_copy", staticmethod(lambda text: copied.append(text) or text))
    monkeypatch.setattr(controller, "_show_answer_card", lambda self, text, kind="answer", sources=(): cards.append((kind, text)))
    return copied, cards


def test_an_answer_is_shown_and_copied_never_pasted(controller, routed):
    copied, cards = routed
    app = _stub(controller)
    controller._deliver_selection_result(app, smart_prompt.KIND_ANSWER, "It means *see you tomorrow*.")
    assert cards == [("answer", "It means *see you tomorrow*.")]
    assert copied == ["It means *see you tomorrow*."]
    assert not any(c[0] == "paste" for c in app.calls)
    assert ("done", False, None) in app.calls


def test_a_change_is_pasted_where_the_cursor_is_and_stays_on_the_clipboard(controller, routed, monkeypatch):
    copied, cards = routed
    monkeypatch.setattr(sr, "focus_editable", lambda: None)            # can't tell (macOS): paste
    monkeypatch.setattr(controller, "_own_window_takes_typing", lambda self: None)
    app = _stub(controller)
    controller._deliver_selection_result(app, smart_prompt.KIND_CHANGE, "Hello everyone.", "translate")
    assert ("paste", "Hello everyone.", True, True) in app.calls      # (rich; the result stays on the clipboard)
    assert cards == [] and copied == []
    assert ("done", True, "translate") in app.calls


# ── every Smart Action, not only the ones about a selection (founder 2026-11-14) ──
def test_a_smart_action_with_nowhere_to_type_is_copied_and_shown_never_called_pasted(controller, routed, monkeypatch):
    """"When the cursor is not somewhere where it can paste, it's just saying pasted, but I don't see anything ...
    if it's not pasted, then it should show the pop-up screen and always copy on the clipboard." """
    copied, cards = routed
    monkeypatch.setattr(controller, "_own_window_takes_typing", lambda self: None)     # another program in front
    monkeypatch.setattr(sr, "focus_editable", lambda: False)           # ... where nothing takes typing
    app = _stub(controller)
    controller._deliver_smart_result(app, "Dear Anna, yes.", "email")
    assert not any(c[0] == "paste" for c in app.calls)
    assert copied == ["Dear Anna, yes."] and cards == [("copied", "Dear Anna, yes.")]
    assert ("done", False, "email") in app.calls                       # the pill: "copied", not "pasted"


def test_a_smart_action_is_pasted_where_it_can_be_or_cannot_be_told(controller, routed, monkeypatch):
    copied, cards = routed
    monkeypatch.setattr(controller, "_own_window_takes_typing", lambda self: None)
    for editable in (True, None):                                      # a text box; a browser or a Mac: paste
        monkeypatch.setattr(sr, "focus_editable", lambda e=editable: e)
        app = _stub(controller)
        controller._deliver_smart_result(app, "Dear Anna, yes.", "email")
        assert ("paste", "Dear Anna, yes.", True, True) in app.calls   # (rich; it stays on the clipboard)
        assert ("done", True, "email") in app.calls
    assert cards == [] and copied == []


def test_in_gusans_own_window_only_a_text_box_takes_the_result(controller, routed, monkeypatch):
    copied, cards = routed
    monkeypatch.setattr(sr, "focus_editable", lambda: True)            # (never asked: Gusan knows its own window)
    monkeypatch.setattr(controller, "_own_window_takes_typing", lambda self: False)    # e.g. the Home page
    app = _stub(controller)
    controller._deliver_smart_result(app, "Hello.", None)
    assert not any(c[0] == "paste" for c in app.calls) and cards == [("copied", "Hello.")]
    monkeypatch.setattr(controller, "_own_window_takes_typing", lambda self: True)     # the cursor in a text box
    app = _stub(controller)
    controller._deliver_smart_result(app, "Hello.", None)
    assert ("paste", "Hello.", True, True) in app.calls and len(cards) == 1


def test_when_the_paste_keys_cannot_be_sent_the_card_shows_it_too(controller, routed, monkeypatch):
    copied, cards = routed
    monkeypatch.setattr(controller, "_own_window_takes_typing", lambda self: None)
    monkeypatch.setattr(sr, "focus_editable", lambda: None)
    app = _stub(controller, pastes=False)                              # (macOS without Accessibility, a blocked key)
    controller._deliver_smart_result(app, "Hello.", "translate")
    assert ("done", False, "translate") in app.calls and cards == [("copied", "Hello.")]


def test_gusans_own_window_says_whether_a_text_box_has_the_keyboard(controller, qapp):
    from PySide6.QtWidgets import QLabel, QLineEdit, QTextBrowser, QVBoxLayout, QWidget
    app = SimpleNamespace(_run_on_gui=lambda fn, *a: fn(*a))
    assert controller._own_window_takes_typing(app) is None           # no window of Gusan's is in front
    win = QWidget()
    lay = QVBoxLayout(win)
    edit, text, plain = QLineEdit(win), QTextBrowser(win), QLabel("x", win)
    for w in (edit, text, plain):
        lay.addWidget(w)
    win.show()
    win.activateWindow()
    qapp.processEvents()
    if qapp.activeWindow() is win:                                     # (a window manager may refuse the focus)
        edit.setFocus()
        qapp.processEvents()
        assert controller._own_window_takes_typing(app) is True
        text.setFocus()                                                # something to read, not to type in
        qapp.processEvents()
        assert controller._own_window_takes_typing(app) is False
        edit.setReadOnly(True)
        edit.setFocus()
        qapp.processEvents()
        assert controller._own_window_takes_typing(app) is False
    win.close()
    win.deleteLater()


def test_a_change_with_nowhere_to_type_is_copied_and_shown(controller, routed, monkeypatch):
    copied, cards = routed
    monkeypatch.setattr(sr, "focus_editable", lambda: False)           # a web page: nothing takes typing
    monkeypatch.setattr(controller, "_own_window_takes_typing", lambda self: None)
    app = _stub(controller)
    controller._deliver_selection_result(app, smart_prompt.KIND_CHANGE, "Hello everyone.", "translate")
    assert not any(c[0] == "paste" for c in app.calls)
    assert copied == ["Hello everyone."] and cards == [("copied", "Hello everyone.")]


def test_the_test_suite_never_starts_reading_a_selection():
    assert not sr.enabled()


def test_the_card_shows_the_answer_without_taking_the_keyboard(qapp):
    from ui.answer_card import AnswerCard
    card = AnswerCard()
    try:
        card.show_text("**Yes.** It is polite.\n\n- warm\n- short", "answer")
        QApplication.processEvents()
        assert card.isVisible()
        assert card.title.text() == "Answer"
        assert "**" not in card.body.text() and "Yes." in card.body.text()
        assert card.windowFlags() & Qt.WindowDoesNotAcceptFocus
        assert card.testAttribute(Qt.WA_ShowWithoutActivating)
    finally:
        card.hide()
        card.deleteLater()


def test_copy_says_copied_then_goes_back(qapp, monkeypatch):
    from ui import answer_card
    monkeypatch.setattr(answer_card, "COPIED_MS", 50)
    card = answer_card.AnswerCard()
    try:
        card.show_text("Hello everyone.", "copied")
        assert card.title.text().startswith("Copied · press ")
        assert card.copy_button.text() == "Copy"
        card.copy_button.click()
        assert QGuiApplication.clipboard().text() == "Hello everyone."
        assert card.copy_button.text() == "Copied" and card.copy_button.accessibleName() == "Copied"
        end = time.monotonic() + 3                                   # (COPIED_MS is 50 ms here)
        while card.copy_button.text() != "Copy" and time.monotonic() < end:
            QApplication.processEvents()
        assert card.copy_button.text() == "Copy"
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


def test_the_pill_forgets_on_your_selection_at_the_next_take(qapp):
    from ui.overlay import Overlay
    ov = Overlay()
    try:
        ov.set_task("translate", "English", {"writer": "Gusan Cloud", "on_selection": True})
        assert ov._ctx["on_selection"] is True
        ov.set_task("translate", "English", {"writer": "Gusan Cloud"})
        assert ov._ctx["on_selection"] is False
    finally:
        ov.hide()
        ov.deleteLater()


def test_the_card_lists_the_web_sources_as_links(qapp):
    from ui.answer_card import AnswerCard
    card = AnswerCard()
    try:
        card.show_text("1 EUR = 1.08 USD today.", "answer",
                       sources=[{"title": "ECB euro reference rates", "url": "https://www.ecb.europa.eu/rates"},
                                {"title": "", "url": "javascript:alert(1)"}])
        assert card.sources.isVisibleTo(card)
        html = card.sources.text()
        assert html.startswith("Sources · 1 ") and 'href="https://www.ecb.europa.eu/rates"' in html
        assert "javascript" not in html                                   # only http(s) addresses
        card.show_text("No web here.", "answer")
        assert not card.sources.isVisibleTo(card)
    finally:
        card.hide()
        card.deleteLater()


def test_the_pill_shows_a_globe_while_it_searches(qapp):
    from ui.overlay import Overlay
    ov = Overlay()
    try:
        ov.set_task(None, "", {"writer": "Gusan Cloud", "web": True})
        assert ov._busy_icon("processing") == "web"
        ov.set_task("email", "", {"writer": "Gusan Cloud"})
        assert ov._busy_icon("processing") == "email"
    finally:
        ov.hide()
        ov.deleteLater()
