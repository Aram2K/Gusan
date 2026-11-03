"""Ask about this meeting under the Gusan 2.1 plans, in the real Meetings page (offscreen; the AI is a scripted fake).

Guest = not signed in; free = signed in without Gusan Pro. Gusan Cloud answers questions for Gusan Pro only; an own key
and a model on this computer are free (a guest too); Privacy mode never calls the cloud. A free user is shown the calm
"needs Gusan Pro" line with a way into the Pro window and a way into Settings - never a metered request, never a
server error. The fixtures are test_meeting_chat_ui.py's."""
import pytest

import meeting_chat
from tests_qt.chat_fakes import make_meeting
from tests_qt.test_meeting_chat_ui import (FakeAI, _pump, answer_rows, build,  # noqa: F401 (fixtures)
                                           meetings_root, type_and_send)

OWN_KEY = {"action_model": "api_openai_compatible", "action_api_key": "sk-test-1"}


class _CapturingAI(FakeAI):
    """FakeAI that also keeps the keyword arguments ask() was given (the recorder's name)."""

    def __call__(self, folder, question, history, **kw):
        self.kw = dict(kw)
        return super().__call__(folder, question, history, **kw)


@pytest.fixture
def ai(monkeypatch):
    fake = _CapturingAI()
    monkeypatch.setattr(meeting_chat, "ask", fake)
    yield fake
    fake.release()


def test_a_free_user_sees_the_calm_line_with_two_ways_out_and_nothing_pops_up(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, _s, app = build("free", f)
    assert card._gate == ("needs_pro", meeting_chat.NEEDS_PRO)
    box = card._gate_in_empty
    assert box.button.isVisibleTo(card) and box.button.text() == "See Gusan Pro"
    assert box.button2.isVisibleTo(card) and box.button2.text() == "Open settings"
    assert not any(c[0] == "_pro_upsell" for c in app.calls)           # calm Pro: it opens only when asked for
    assert not card.input.isEnabled() and not card.send_btn.isEnabled()


def test_see_gusan_pro_opens_the_pro_window_and_open_settings_opens_speech_and_ai(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, _s, app = build("free", f)
    card._gate_in_empty.button.click()
    assert ("_pro_upsell", "Ask about this meeting") in app.calls
    card._gate_in_empty.button2.click()
    assert ("show_settings", "speech") in app.calls


def test_the_gate_above_the_box_has_the_same_two_ways_out_once_there_is_a_chat(build, meetings_root):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "Q1"},
                                          {"role": "assistant", "text": "A1", "status": "done"}])
    win, page, card, _s, app = build("free", f)
    bar = card.gate_bar
    assert bar.isVisibleTo(card) and bar.button.text() == "See Gusan Pro" and bar.button2.text() == "Open settings"
    bar.button.click()
    assert ("_pro_upsell", "Ask about this meeting") in app.calls


def test_a_pro_user_has_no_gate_and_the_other_gates_keep_one_button(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    assert card._gate == ("ready", "") and not card._gate_in_empty.isVisibleTo(card)
    win2, page2, card2, _s, app = build("guest", f)
    assert card2._gate_in_empty.button.text() == "Sign in" and not card2._gate_in_empty.button2.isVisibleTo(card2)
    win3, page3, card3, *_ = build("pro", f, cfg={"privacy_mode": True})
    assert card3._gate_in_empty.button.text() == "Open settings" and not card3._gate_in_empty.button2.isVisibleTo(card3)


def test_a_free_user_with_an_own_key_asks_for_free_and_is_never_told_to_pay(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.kind, ai.label = "own_key", "Your key · OpenAI-compatible"
    win, page, card, _s, app = build("free", f, cfg=OWN_KEY)
    assert card._gate == ("ready", "")
    type_and_send(card, "Who owns the budget?")
    assert [c["question"] for c in ai.calls] == ["Who owns the budget?"]
    assert ai.calls[0]["pro"] is False
    assert ("track", "meeting_chat_asked", {"engine": "own_key"}) in app.calls


def test_a_guest_with_an_own_key_asks_too(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("guest", f, cfg=OWN_KEY)
    assert card._gate == ("ready", "")


def test_free_with_a_model_on_the_computer_asks_for_free(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.kind, ai.label = "local", "This computer · Qwen 1.5B"
    win, page, card, *_ = build("free", f, downloaded=("qwen_tiny",))
    assert card._gate == ("ready", "")
    type_and_send(card, "Who owns the budget?")
    assert len(ai.calls) == 1


def test_when_the_server_says_the_plan_is_needed_the_calm_line_stays_and_there_is_no_try_again(build, meetings_root,
                                                                                               ai):
    f = make_meeting(meetings_root)
    ai.push(error=("needs_pro", meeting_chat.NEEDS_PRO))
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Who owns the budget?")
    row = answer_rows(card)[-1]
    assert row.status.text() == meeting_chat.NEEDS_PRO
    assert not row.retry_btn.isVisibleTo(card)
    assert [m["role"] for m in card._msgs] == ["user"]                 # the failure is not kept, the question is


def test_the_recorders_first_name_goes_with_the_question_so_you_is_somebody(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "What did I agree to do?")
    assert ai.kw.get("me") == "Aram"                                   # the stub profile is "Aram Adamyan"


def test_a_guest_has_no_name_to_give(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("guest", f, cfg=OWN_KEY)
    type_and_send(card, "What did I agree to do?")
    assert ai.kw.get("me") == ""


def test_the_plan_changing_moves_the_gate_both_ways(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, _s, app = build("pro", f)
    assert card._gate == ("ready", "")
    app.set_tier("free")                                               # the subscription ended
    page.refresh_account()
    assert card._gate[0] == "needs_pro"
    assert card._gate_in_empty.button.text() == "See Gusan Pro"
    app.set_tier("pro")
    page.refresh_account()
    assert card._gate == ("ready", "")
    assert not card._gate_in_empty.isVisibleTo(card)
