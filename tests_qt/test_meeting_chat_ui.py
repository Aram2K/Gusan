"""Ask about this meeting: the card in the notes view's right column (ui/pages/meeting_chat_ui.py), offscreen.

The AI is a scripted fake (meeting_chat.ask is replaced: no network, no model), the page is the real Meetings page in
the real main window, the meetings are folders in a temp dir. Covered: every state of the card (no transcript,
finishing, ready, thinking, streaming, done, stopped, error, signed out, needs Pro, Privacy mode), the keyboard (Enter,
Shift+Enter, Esc, Tab order), the conversation kept in the meeting's chat.json (reopened, switched, released),
hostile text, very long text, the two layouts and the columns' sizes, and that the app stays light on a dark OS."""
import json
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel

import actions
import local_llm
import meeting_chat
import meeting_store
from tests_qt.chat_fakes import FakeSession, NOTES, TRANSCRIPT, make_meeting  # noqa: F401
from tests_qt.conftest import make_stub_app
from ui.components import clipboard as rich_clipboard
from ui.components import mini_prompt


def _pump(cond=None, timeout=20.0):
    """Run the event loop: ten turns, or until ``cond`` holds (at most ``timeout`` seconds: a shared build
    machine can stall for a few seconds, and a test that passes returns as soon as ``cond`` holds)."""
    end = time.monotonic() + timeout
    turns = 0
    while time.monotonic() < end:
        QApplication.processEvents()
        turns += 1
        if cond is None and turns >= 10:
            return True
        if cond is not None and cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond() if cond is not None else True


@pytest.fixture(autouse=True)
def _inline(monkeypatch):
    from ui.pages import meeting_chat_ui, meeting_views
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", True)
    import ui.winfit
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)


@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


class FakeAI:
    """meeting_chat.ask, scripted. push() one step per question: the pieces to stream, the final text, an error,
    whether to block until release() (then run the card with SYNC_ASK off)."""

    def __init__(self):
        self.calls = []
        self.steps = []
        self.gate = threading.Event()
        self.label, self.kind = "Gusan Cloud \u00b7 Mistral Small", "cloud"

    def push(self, text="An answer.", parts=(), error=None, block=False, coverage=None):
        self.steps.append(dict(text=text, parts=parts, error=error, block=block, coverage=coverage))
        return self

    def release(self):
        self.gate.set()

    def __call__(self, folder, question, history, *, cfg, pro, resolve, on_text=None, cancelled=None, **kw):
        step = self.steps.pop(0) if self.steps else dict(text="An answer.", parts=(), error=None, block=False, coverage=None)
        self.calls.append({"folder": folder, "question": question, "history": [dict(m) for m in history], "cfg": cfg,
                           "pro": pro, "thread": threading.current_thread()})
        acc = ""
        for piece in step["parts"]:
            acc += piece
            if on_text:
                on_text(acc)
            time.sleep(0.06)
        if step["block"]:
            self.gate.wait()
            if cancelled and cancelled():
                raise meeting_chat.Cancelled(acc)
        if step["error"]:
            raise meeting_chat.ChatError(*step["error"])
        cov = step["coverage"] or meeting_chat.Coverage()
        return meeting_chat.Answer(step["text"], cov, self.label, self.kind, 1.5, 0.3)


def test_fake_ai_blocked_answer_waits_for_release(monkeypatch):
    fake = FakeAI().push("Done.", block=True)
    waiting = threading.Event()
    wait = fake.gate.wait
    timeouts, answers = [], []

    def wait_for_release(timeout=None):
        timeouts.append(timeout)
        waiting.set()
        return wait(timeout)

    monkeypatch.setattr(fake.gate, "wait", wait_for_release)
    worker = threading.Thread(target=lambda: answers.append(fake(None, "Q?", [], cfg={}, pro=True, resolve=None)))
    worker.start()
    try:
        assert waiting.wait(20)
        assert timeouts == [None]
        assert answers == []
        fake.release()
        worker.join(20)
        assert not worker.is_alive()
        assert answers[0].text == "Done."
    finally:
        fake.release()
        worker.join(20)


@pytest.fixture
def ai(monkeypatch):
    fake = FakeAI()
    monkeypatch.setattr(meeting_chat, "ask", fake)
    yield fake
    fake.release()


@pytest.fixture
def build(qapp, monkeypatch, meetings_root):
    """build(tier, folder, phase=, cfg=, downloaded=) -> (window, page, card, session, app)"""
    from ui import shell as shell_mod
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    made = []

    def make(tier="pro", folder=None, phase="idle", cfg=None, downloaded=(), width=1280, height=820, final=True):
        app = make_stub_app(tier)
        app.cfg.update(cfg or {})
        session = FakeSession(phase)
        session.final = final
        if phase == "processing":
            session.meeting_dir = folder
        app.meeting_session = session
        monkeypatch.setattr(local_llm, "model_downloaded", lambda mid: mid in downloaded)
        win = shell_mod.MainWindow(app)
        made.append(win)
        win.resize(width, height)
        win.show()
        _pump()
        opts = {"meeting": str(folder)} if folder else {}
        if phase == "processing":
            opts["writing"] = True
        page = win.open("meetings", **opts)
        _pump()
        return win, page, page.notes_view.chat, session, app

    yield make
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def rows(card):
    return [w for w in card._rows if w.isVisibleTo(card)]


def answer_rows(card):
    from ui.pages.meeting_chat_ui import _AnswerRow
    return [w for w in card._rows if isinstance(w, _AnswerRow)]


def type_and_send(card, text):
    card.input.setPlainText(text)
    QTest.keyClick(card.input, Qt.Key_Return)
    _pump()                                   # (a widget added to a layout is shown on the next turn of the loop)


def chat_file(folder):
    path = Path(folder) / "chat.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ── the card is there, and what it says ──────────────────────────────────────────────────────────────────────────

def test_the_card_sits_above_people_and_the_recording_for_every_plan(build, meetings_root):
    f = make_meeting(meetings_root)
    for tier in ("pro", "free", "guest"):
        win, page, card, *_ = build(tier, f)
        nv = page.notes_view
        assert page.current_view() == "notes"
        assert card is nv.chat
        assert card.accessibleName() == "Ask about this meeting"
        assert card.isVisibleTo(nv)
        side = nv._side.layout()
        assert side.itemAt(0).widget() is card                       # above People and the recording
        assert side.itemAt(1).widget() is nv.people_card
        assert side.stretch(0) == 1                                  # it fills the room that used to be blank
        assert card.graphicsEffect() is None                         # no shadow: a streaming repaint stays cheap


def test_ready_shows_the_intro_three_starters_and_who_answers(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    assert card._gate == ("ready", "")
    assert card.input.isEnabled() and not card.stop_btn.isVisibleTo(card)
    assert not card.send_btn.isEnabled()                      # nothing written yet: the blue button is not on offer
    card.input.setPlainText("Who spoke?")
    assert card.send_btn.isEnabled()
    card.input.clear()
    assert not card.send_btn.isEnabled()
    assert [c.text() for c in card._chips] == ["What were the key decisions?", "What did I agree to do?",
                                                "Write a follow-up email"]
    assert all(c.isVisibleTo(card) for c in card._chips)
    assert card._intro.isVisibleTo(card)
    assert not card.clear_btn.isVisibleTo(card)
    assert card.foot.full_text() == "Gusan Cloud \u00b7 Mistral Small"
    assert "Hiring" not in card.foot.full_text()


def test_no_transcript_says_so_and_asks_nothing(build, meetings_root):
    f = make_meeting(meetings_root, transcript="", notes="")
    win, page, card, *_ = build("pro", f)
    assert card._gate == ("no_transcript", meeting_chat.NO_TRANSCRIPT)
    assert not card.input.isEnabled() and not card.send_btn.isEnabled()
    assert not any(c.isVisibleTo(card) for c in card._chips)
    assert card._gate_in_empty.text.text() == meeting_chat.NO_TRANSCRIPT
    card.send("anything")
    assert card._msgs == []


def test_while_the_transcript_is_being_finished_the_chat_waits_and_then_opens_by_itself(build, meetings_root):
    f = make_meeting(meetings_root)
    (f / "chunks.jsonl").write_text(json.dumps({"index": 0, "text": "live words"}) + "\n", encoding="utf-8")
    win, page, card, session, _app = build("pro", f, phase="processing", final=False)
    assert page.current_view() == "notes"                            # opened with writing=True: not the live view
    assert card._gate == ("finishing", meeting_chat.FINISHING)
    assert not card.input.isEnabled()
    # the plain transcript.txt is there, the final one is not: still waiting
    assert (f / "transcript.txt").exists()
    # the final one lands: the page reads the meeting again and the chat opens
    session.final = True
    session.transcript_saved.emit(str(f))
    _pump(lambda: card._gate[0] == "ready")
    assert card._gate == ("ready", "")
    assert card.input.isEnabled()


def test_a_meeting_the_app_never_finished_can_be_asked_about_from_what_exists(build, meetings_root):
    f = make_meeting(meetings_root, transcript="", notes="")
    (f / "chunks.jsonl").write_text(json.dumps({"index": 0, "text": "live words only"}) + "\n", encoding="utf-8")
    win, page, card, *_ = build("pro", f)
    assert card._gate == ("ready", "")


def test_free_is_asked_for_pro_or_a_key_and_the_buttons_open_the_pro_window_and_settings(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, _s, app = build("free", f)
    assert card._gate == ("needs_pro", meeting_chat.NEEDS_PRO)
    assert not card.input.isEnabled()
    assert not any(c[0] == "_pro_upsell" for c in app.calls)         # calm Pro: no window pops up by itself
    btn = card._gate_in_empty.button2
    assert btn.isVisibleTo(card) and btn.text() == "Open settings"
    btn.click()
    assert ("show_settings", "speech") in app.calls
    card._gate_in_empty.button.click()
    assert ("_pro_upsell", "Ask about this meeting") in app.calls
    card.send("anything")
    assert card._msgs == []


def test_a_guest_is_asked_to_sign_in(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, _s, app = build("guest", f)
    assert card._gate == ("signed_out", meeting_chat.SIGNED_OUT)
    btn = card._gate_in_empty.button
    assert btn.text() == "Sign in"
    btn.click()
    assert ("show_auth_gate",) in app.calls


def test_free_with_a_model_on_the_computer_can_ask(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("free", f, downloaded=("qwen_tiny",))
    assert card._gate == ("ready", "")
    assert card.foot.full_text().startswith("This computer \u00b7 Qwen 1.5B")


def test_privacy_mode_with_no_model_says_which_to_download(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, _s, app = build("pro", f, cfg={"privacy_mode": True})
    assert card._gate == ("privacy_no_model", actions.LOCAL_ANSWERS_MESSAGE)
    assert card._gate_in_empty.button.text() == "Open settings"
    assert not card.input.isEnabled()


def test_privacy_mode_with_a_model_stays_on_the_computer(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    win, page, card, _s, app = build("pro", f, cfg={"privacy_mode": True, "action_model": "qwen_3b"},
                                     downloaded=("qwen_3b",))
    assert card._gate == ("ready", "")
    assert card.foot.full_text() == "This computer \u00b7 Qwen 3B \u00b7 Stays on this computer."
    ai.kind, ai.label = "local", "This computer \u00b7 Qwen 3B"
    card.send("Who spoke?")
    assert ("track", "meeting_chat_asked", {"engine": "local"}) in app.calls


def test_a_gate_after_the_chat_has_messages_keeps_them_and_puts_its_words_above_the_box(build, meetings_root):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "Q1"},
                                          {"role": "assistant", "text": "A1", "status": "done"}])
    win, page, card, *_ = build("free", f)
    assert len(card._msgs) == 2
    assert card.gate_bar.isVisibleTo(card) and not card._gate_in_empty.isVisibleTo(card)
    assert card.gate_bar.text.text() == meeting_chat.NEEDS_PRO
    assert not card.input.isEnabled()
    assert "A1" in answer_rows(card)[0].accessibleDescription()


def test_the_plan_changing_updates_the_card(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, _s, app = build("free", f)
    assert card._gate[0] == "needs_pro"
    app.set_tier("pro")
    page.refresh_account()
    assert card._gate == ("ready", "")
    app.cfg["privacy_mode"] = True
    page.on_setting_changed("privacy_mode")
    assert card._gate[0] == "privacy_no_model"


# ── asking ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_enter_sends_and_shift_enter_is_a_new_line(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    card.input.setFocus()
    card.input.setPlainText("first line")
    cur = card.input.textCursor()
    cur.movePosition(cur.MoveOperation.End)
    card.input.setTextCursor(cur)
    QTest.keyClick(card.input, Qt.Key_Return, Qt.ShiftModifier)
    assert card.input.toPlainText() == "first line\n"
    assert ai.calls == []
    QTest.keyClicks(card.input, "second")
    QTest.keyClick(card.input, Qt.Key_Return)
    assert [c["question"] for c in ai.calls] == ["first line\nsecond"]
    assert card.input.toPlainText() == ""
    QTest.keyClick(card.input, Qt.Key_Enter)                         # an empty box sends nothing
    assert len(ai.calls) == 1


def test_a_starter_sends_at_once_and_the_starters_go_until_the_chat_is_cleared(build, meetings_root, ai, monkeypatch):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    card._chips[0].click()
    assert ai.calls[0]["question"] == "What were the key decisions?"
    assert not card._empty.isVisibleTo(card)
    assert card.clear_btn.isVisibleTo(card)
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: True)
    card.clear_btn.click()
    assert card._msgs == [] and card._empty.isVisibleTo(card)
    assert all(c.isVisibleTo(card) for c in card._chips)
    assert chat_file(f)["messages"] == []                            # (and the file says so)


def test_a_question_and_its_answer_are_shown_and_kept_in_the_meetings_folder(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.push("The **second interview** will be planned.\n\n- one\n- two",
            coverage=meeting_chat.Coverage(False, 40, 22, 55))
    win, page, card, _s, app = build("pro", f)
    type_and_send(card, "What were the key decisions?")
    assert ai.calls[0]["pro"] is True
    assert ai.calls[0]["folder"] == str(f)
    assert ai.calls[0]["history"] == []
    kept = answer_rows(card)[-1]
    html = kept.body.text()
    assert "<b>second interview</b>" in html and "<ul" in html and "**" not in html
    assert kept.copy_btn.isVisibleTo(card) and not kept.retry_btn.isVisibleTo(card)
    assert kept.note.text() == "Long meeting: this answer used the parts closest to your question (about 22 of 55 min)."
    saved = chat_file(f)
    assert saved["version"] == 1
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][0]["text"] == "What were the key decisions?"
    a = saved["messages"][1]
    assert (a["status"], a["kind"], a["engine"]) == ("done", "cloud", "Gusan Cloud \u00b7 Mistral Small")
    assert a["coverage"] == {"complete": False, "pct": 40, "covered_min": 22, "total_min": 55}
    assert ("track", "meeting_chat_asked", {"engine": "cloud"}) in app.calls
    # not a word of the question or the answer went to analytics
    assert "key decisions" not in json.dumps([c for c in app.calls if c[0] == "track"])


def test_earlier_finished_answers_go_back_to_the_model(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.push("First answer.").push("Second answer.")
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "One?")
    type_and_send(card, "Two?")
    history = ai.calls[1]["history"]
    assert [(m["role"], m["text"]) for m in history] == [("user", "One?"), ("assistant", "First answer.")]
    assert history[1]["status"] == "done"
    assert ai.calls[1]["question"] == "Two?"


def test_the_words_stream_into_the_card_and_stop_restores_the_box(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("The key decisions were all taken.", parts=["The key **decisions** ", "were all ", "taken. **Half"],
            block=True)
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "What were the key decisions?")
    assert card._running
    assert card.stop_btn.isVisibleTo(card) and not card.send_btn.isVisibleTo(card)
    assert card.input.isEnabled()                                    # typeable while an answer is coming
    live = card._live
    assert _pump(lambda: live.body.isVisibleTo(card) and "taken" in live.body.text())
    assert "<b>decisions</b>" in live.body.text()                    # drawn with real formatting
    assert "**" not in live.body.text()                              # (and the half-written one is not shown)
    assert live.status.text() == "Writing\u2026"
    assert not live.copy_btn.isVisibleTo(card)
    ai.release()
    assert _pump(lambda: not card._running)
    assert card.send_btn.isVisibleTo(card) and not card.stop_btn.isVisibleTo(card)
    assert answer_rows(card)[-1].msg["text"] == "The key decisions were all taken."
    assert answer_rows(card)[-1].status.text() == ""


def test_thinking_shows_at_once_and_enter_does_nothing_meanwhile(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("Done.", block=True)
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Q?")
    assert card._live.status.text() == "Thinking\u2026"              # before the worker has answered anything
    card.input.setPlainText("a second question")
    QTest.keyClick(card.input, Qt.Key_Return)
    assert card.input.toPlainText() == "a second question"           # nothing sent, nothing lost
    assert len(card._msgs) == 1
    ai.release()
    assert _pump(lambda: not card._running)
    assert len(ai.calls) == 1


def test_the_work_does_not_run_on_the_window_thread(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("Done.")
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Q?")
    assert _pump(lambda: bool(ai.calls))
    assert ai.calls[0]["thread"] is not threading.main_thread()
    assert _pump(lambda: not card._running)


def test_waiting_for_a_busy_local_model_is_said(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.kind, ai.label = "local", "This computer \u00b7 Qwen 3B"
    ai.push("Done.", block=True)
    monkeypatch.setattr(local_llm, "_in_use", {"qwen_3b": 1})
    win, page, card, *_ = build("free", f, cfg={"action_model": "qwen_3b"}, downloaded=("qwen_3b",))
    type_and_send(card, "Q?")
    assert card._live.status.text() == "Waiting for the AI on this computer\u2026"
    ai.release()
    _pump(lambda: not card._running)


def test_stop_keeps_the_words_marked_stopped_and_they_are_never_sent_back(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("never finished", parts=["Half of an ", "answer"], block=True).push("The next answer.")
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Q1?")
    live = card._live
    assert _pump(lambda: "answer" in live.body.text())
    card.stop_btn.click()
    assert not card._running
    assert live.status.text() == "Stopped" and live.copy_btn.isVisibleTo(card)
    saved = chat_file(f)["messages"]
    assert [(m["role"], m.get("status")) for m in saved] == [("user", None), ("assistant", "stopped")]
    assert saved[1]["text"].startswith("Half of an")
    ai.release()                                                    # the worker ends; its late result changes nothing
    _pump()
    assert len(card._msgs) == 2
    type_and_send(card, "Q2?")
    _pump(lambda: len(ai.calls) == 2 and not card._running)
    assert ai.calls[1]["history"][-1]["status"] == "stopped"         # the card hands it over; meeting_chat drops it
    assert "Half of an" in ai.calls[1]["history"][-1]["text"]
    assert meeting_chat._history_text(ai.calls[1]["history"], False) == ""


def test_escape_stops_an_answer_that_is_coming_and_only_then(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("x", parts=["Some words "], block=True)
    win, page, card, *_ = build("pro", f)
    card.input.setFocus()
    QTest.keyClick(card.input, Qt.Key_Escape)                        # nothing is coming: the key is not taken
    assert not card._running
    type_and_send(card, "Q?")
    assert _pump(lambda: "Some words" in card._live.body.text())
    QTest.keyClick(card.input, Qt.Key_Escape)
    assert not card._running
    assert chat_file(f)["messages"][1]["status"] == "stopped"
    ai.release()


def test_stopping_before_a_single_word_leaves_the_question_unanswered_with_try_again(build, meetings_root, ai,
                                                                                    monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("late", block=True).push("Now it works.")
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Q?")
    card.stop()
    row = answer_rows(card)[-1]
    assert row.status.text() == "No answer yet" and row.retry_btn.isVisibleTo(card)
    assert [m["role"] for m in chat_file(f)["messages"]] == ["user"]     # no empty answer kept
    ai.release()
    row.retry_btn.click()
    assert _pump(lambda: not card._running and len(card._msgs) == 2)
    assert card._msgs[1]["text"] == "Now it works."
    assert len(answer_rows(card)) == 1                                # the unanswered row was replaced


def test_a_failure_is_one_calm_sentence_and_try_again_asks_the_same_question(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.push(error=("offline", meeting_chat.OFFLINE_CLOUD)).push("Back online.")
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "What was agreed?")
    row = answer_rows(card)[-1]
    assert row.status.text() == meeting_chat.OFFLINE_CLOUD
    assert row.retry_btn.isVisibleTo(card) and not row.copy_btn.isVisibleTo(card)
    assert [m["role"] for m in chat_file(f)["messages"]] == ["user"]     # the question is kept, the failure is not
    assert card.send_btn.isVisibleTo(card) and not card._running
    row.retry_btn.click()
    assert ai.calls[1]["question"] == "What was agreed?"
    assert ai.calls[1]["history"] == []
    assert [m["role"] for m in card._msgs] == ["user", "assistant"]
    assert len(answer_rows(card)) == 1 and answer_rows(card)[0].msg["text"] == "Back online."


def test_an_allowance_that_is_used_up_offers_no_try_again(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.push(error=("allowance", actions.CLOUD_ALLOWANCE_USED))
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Q?")
    row = answer_rows(card)[-1]
    assert row.status.text() == actions.CLOUD_ALLOWANCE_USED
    assert not row.retry_btn.isVisibleTo(card)


def test_an_unexpected_failure_is_still_a_calm_sentence(build, meetings_root, monkeypatch):
    f = make_meeting(meetings_root)

    def boom(*a, **k):
        raise RuntimeError("Traceback: https://x.co/secret?token=abc")
    monkeypatch.setattr(meeting_chat, "ask", boom)
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Q?")
    row = answer_rows(card)[-1]
    assert row.status.text() == meeting_chat.GENERIC
    assert "http" not in row.status.text()


def test_sending_a_second_question_removes_the_failed_attempt(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.push(error=("server", meeting_chat.SERVER)).push("OK.")
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "One?")
    type_and_send(card, "Two?")
    assert len(answer_rows(card)) == 1
    assert [m["text"] for m in card._msgs] == ["One?", "Two?", "OK."]


def test_the_send_is_refused_when_the_plan_changed_since_the_page_drew(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    win, page, card, _s, app = build("pro", f)
    app.set_tier("free")                                             # (no page refresh yet)
    card.send("Q?")
    assert ai.calls == [] and card._msgs == []
    assert card._gate[0] == "needs_pro"


# ── Copy, Clear ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_copy_puts_real_formatting_on_the_clipboard(build, meetings_root):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "Q"},
                                          {"role": "assistant", "text": "**Decisions**\n\n- one\n- two", "status": "done"}])
    win, page, card, *_ = build("pro", f)
    row = answer_rows(card)[0]
    row.copy_btn.click()
    mime = QGuiApplication.clipboard().mimeData()
    assert mime.hasHtml() and "<ul>" in mime.html()
    assert "**" not in mime.text() and "\u2022 one" in mime.text()
    assert row.copy_btn.text() == "Copied"
    row._copied.timeout.emit()
    assert row.copy_btn.text() == "Copy"


def test_clear_chat_asks_first_and_keeps_the_meeting(build, meetings_root, monkeypatch):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "Q"},
                                          {"role": "assistant", "text": "A", "status": "done"}])
    asked = []
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: asked.append((a[1:3], k)) or False)
    win, page, card, *_ = build("pro", f)
    card.clear_btn.click()
    assert asked[0][0] == ("Clear this chat?", "The questions and answers about this meeting are removed. "
                                              "The meeting itself stays.")
    assert asked[0][1].get("private") is False
    assert len(card._msgs) == 2                                       # "Keep"
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: asked.append((a[1:3], k)) or True)
    card.clear_btn.click()
    assert card._msgs == [] and chat_file(f)["messages"] == []
    assert (f / "notes.md").exists() and (f / "transcript.txt").exists()
    assert not card.clear_btn.isVisibleTo(card)


def test_the_clear_question_is_kept_out_of_a_screen_share_when_the_window_is(build, meetings_root, monkeypatch):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "Q"}])
    asked = []
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: asked.append(k) or False)
    win, page, card, *_ = build("pro", f)
    win._share_hidden = True                                          # (what hide_from_share did; no real window here)
    card.clear_btn.click()
    assert asked[0].get("private") is True


# ── the conversation is the meeting's ───────────────────────────────────────────────────────────────────────────

def test_the_conversation_comes_back_when_the_meeting_is_opened_again(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.push("The decisions.")
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "What were the decisions?")
    page.show_list(refresh=True)
    page.open_notes(str(f))
    _pump()
    assert [m["text"] for m in card._msgs] == ["What were the decisions?", "The decisions."]
    # and in a window of its own (a restart)
    win2, page2, card2, *_ = build("pro", f)
    assert [m["text"] for m in card2._msgs] == ["What were the decisions?", "The decisions."]
    assert answer_rows(card2)[0].copy_btn.isVisibleTo(card2)
    assert not card2._empty.isVisibleTo(card2)


def test_a_question_without_an_answer_comes_back_with_try_again(build, meetings_root, ai):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "Q1"},
                                          {"role": "assistant", "text": "A1", "status": "done"},
                                          {"role": "user", "text": "Q2 never answered"}])
    ai.push("Now answered.")
    win, page, card, *_ = build("pro", f)
    rs = answer_rows(card)
    assert rs[-1].status.text() == "No answer yet"
    rs[-1].retry_btn.click()
    assert [m["text"] for m in card._msgs][-2:] == ["Q2 never answered", "Now answered."]
    assert ai.calls[0]["history"][-1]["text"] == "A1"


def test_a_half_answer_from_a_file_is_never_shown_as_finished(build, meetings_root):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "Q"},
                                          {"role": "assistant", "text": "cut in the mid", "status": "weird"}])
    win, page, card, *_ = build("pro", f)
    row = answer_rows(card)[0]
    assert row.msg["status"] == "stopped"
    assert row.status.text() == "Stopped"


def test_another_meeting_has_its_own_chat_and_a_running_answer_ends_in_its_own_folder(build, meetings_root, ai,
                                                                                    monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    a = make_meeting(meetings_root, "20261030_153400", "First")
    b = make_meeting(meetings_root, "20261029_100000", "Second",
                     chat=[{"role": "user", "text": "Second's own question"}])
    ai.push("x", parts=["Words so far "], block=True)
    win, page, card, *_ = build("pro", a)
    type_and_send(card, "Question for the first?")
    assert _pump(lambda: "Words so far" in card._live.body.text())
    page.open_notes(str(b))
    _pump(lambda: [m["text"] for m in card._msgs] == ["Second's own question"])
    assert not card._running
    assert [m["text"] for m in card._msgs] == ["Second's own question"]
    first = chat_file(a)["messages"]
    assert [(m["role"], m.get("status")) for m in first] == [("user", None), ("assistant", "stopped")]
    assert first[1]["text"].startswith("Words so far")
    assert chat_file(b)["messages"] == [{"role": "user", "text": "Second's own question"}]      # untouched
    ai.release()
    _pump()
    assert chat_file(b)["messages"] == [{"role": "user", "text": "Second's own question"}]
    assert len(chat_file(a)["messages"]) == 2                          # the late result changed nothing


def test_releasing_the_meeting_mid_answer_writes_nothing_more(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("x", parts=["Half "], block=True)
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Q?")
    _pump(lambda: "Half" in card._live.body.text())
    before = chat_file(f)
    page.notes_view.release_folder(str(f))                             # the list is about to trash it
    assert not card._running and card._msgs == []
    ai.release()
    _pump()
    assert chat_file(f) == before                                      # only the question that was there
    assert [m["role"] for m in before["messages"]] == ["user"]


def test_deleting_the_meeting_leaves_no_chat_behind_and_never_makes_the_folder_again(build, meetings_root, ai):
    import shutil
    f = make_meeting(meetings_root)
    ai.push("Done.")
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Q?")
    page.notes_view.release_folder(str(f))
    shutil.rmtree(f)
    card._folder = str(f)                                              # (a save that was already on its way)
    card._msgs = [{"role": "user", "text": "late"}]
    card._save()
    assert not f.exists()


def test_the_trash_keeps_the_chat_and_undo_brings_it_back(build, meetings_root, ai):
    import meeting_trash
    f = make_meeting(meetings_root)
    ai.push("Kept.")
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Q?")
    page.notes_view.release_folder(str(f))
    t = meeting_trash.move_to_trash(f)
    back = meeting_trash.restore(t)
    assert [m["text"] for m in meeting_store.load_chat(back)] == ["Q?", "Kept."]
    page.open_notes(str(back))
    _pump()
    assert [m["text"] for m in card._msgs] == ["Q?", "Kept."]


# ── hostile and very long text ──────────────────────────────────────────────────────────────────────────────────

HOSTILE_Q = "<img src='file:///C:/secret.png'><script>alert(1)</script> **x** [a](http://evil.test) `y"
HOSTILE_A = ("<b>bold?</b> ![p](http://evil.test/p.png) [click](https://example.com) "
             "<img src=\"\\\\\\\\host\\\\share\\\\x.png\"> | a | b |\n|---|---|\n| 1 | 2 |\n\n**unclosed")


def test_a_hostile_question_and_answer_are_shown_as_text(build, meetings_root):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": HOSTILE_Q},
                                          {"role": "assistant", "text": HOSTILE_A, "status": "done"}])
    win, page, card, *_ = build("pro", f)
    bubble_label = card._rows[0].findChild(QLabel)
    assert bubble_label.textFormat() == Qt.PlainText
    assert "<img src='file:///C:/secret.png'>" in bubble_label.text().replace("\u200b", "")
    assert "<script>" in bubble_label.text().replace("\u200b", "")
    body = answer_rows(card)[0].body
    html = body.text()
    for bad in ("<img", "<script", "href", "<a "):
        assert bad not in html
    assert "&lt;b&gt;bold?&lt;/b&gt;" in html or "&lt;b&gt;" in html
    assert body.pixmap().isNull() and body.movie() is None
    assert body.openExternalLinks() is False
    assert "click (https://example.com" in html.replace("\u200b", "")


def test_a_very_long_question_is_cut_in_the_bubble_but_all_of_it_is_sent(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.push("Fine.")
    win, page, card, *_ = build("pro", f)
    q = ("Could you write me a full answer about everything we said? " * 70)[:3990]
    card.input.setPlainText(q)
    QTest.keyClick(card.input, Qt.Key_Return)
    assert ai.calls[0]["question"] == q.strip()
    shown = card._rows[0].findChild(QLabel).text().replace("\u200b", "")
    assert shown.endswith("\u2026") and len(shown) <= 602
    assert card._msgs[0]["text"] == q.strip()                          # kept whole


def test_the_question_box_is_cut_at_four_thousand_characters_and_counts_from_thirty_five_hundred(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    card.input.setPlainText("x" * 3400)
    assert not card.counter.isVisibleTo(card)
    card.input.setPlainText("x" * 3912)
    assert card.counter.isVisibleTo(card) and card.counter.text() == "3,912 / 4,000"
    card.input.setPlainText("y" * 6000)                                # pasted
    assert len(card.input.toPlainText()) == 4000
    assert card.counter.text() == "4,000 / 4,000"
    card.input.clear()
    assert not card.counter.isVisibleTo(card)


def test_a_long_unbroken_word_does_not_widen_the_card(build, meetings_root):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "w" * 3000},
                                          {"role": "assistant", "text": "https://example.com/" + "a" * 3000, "status": "done"}])
    win, page, card, *_ = build("pro", f)
    width = card.width()
    _pump()
    assert card.width() == width
    assert card._content.width() <= card.scroll.viewport().width() + 1
    assert card.scroll.horizontalScrollBar().maximum() == 0


def test_the_box_grows_to_five_lines_and_then_scrolls(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    h1 = card.input.height()
    card.input.setPlainText("one")
    assert card.input.height() == h1 == 44
    card.input.setPlainText("1\n2\n3")
    assert 44 < card.input.height() < 120
    card.input.setPlainText("\n".join(str(i) for i in range(40)))
    assert card.input.height() == 120
    assert card.input.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded
    card.input.clear()
    assert card.input.height() == 44


def test_a_tall_answer_starts_at_its_first_line_when_it_ends(build, meetings_root, ai):
    f = make_meeting(meetings_root, width=1280) if False else make_meeting(meetings_root)
    ai.push("\n\n".join("Paragraph %d of a long drafted email that keeps going for a while." % i for i in range(40)))
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Draft a long email")
    _pump()
    row = answer_rows(card)[-1]
    assert row.height() > card.scroll.viewport().height()
    bar = card.scroll.verticalScrollBar()
    top = row.mapTo(card._content, QPoint(0, 0)).y()
    assert _pump(lambda: abs(bar.value() - max(0, top - 6)) <= 2)


def test_the_conversation_follows_the_end_only_while_the_reader_is_there(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "q%d " % i + "word " * 30} for i in range(12)])
    ai.push("x" * 10, parts=["line one\n\n", "line two\n\n", "line three\n\n"] * 6, block=True)
    win, page, card, *_ = build("pro", f)
    bar = card.scroll.verticalScrollBar()
    _pump()
    assert bar.value() == bar.maximum() > 0                            # opened at the latest
    card.input.setPlainText("again?")
    card.send()                                                        # a new question: the end is where it shows
    _pump(lambda: len(card._live.body.text()) > 40)
    assert _pump(lambda: bar.value() == bar.maximum())                 # and the words are followed
    top = bar.maximum()
    bar.setValue(0)                                                    # the reader scrolls up to read
    assert card._stick is False
    ai.release()
    _pump(lambda: not card._running)
    assert bar.value() <= 4 and bar.maximum() >= top - 4              # not dragged down by what came after


# ── words, names, keyboard ──────────────────────────────────────────────────────────────────────────────────────

def test_everything_has_a_name_and_the_tab_order_follows_the_screen(build, meetings_root):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "Q"}, {"role": "assistant", "text": "A", "status": "done"}])
    win, page, card, *_ = build("pro", f)
    assert card.scroll.accessibleName() == "Conversation"
    assert card.input.accessibleName() == "Your question"
    assert card.send_btn.accessibleName() == "Send question"
    assert card.stop_btn.accessibleName() == "Stop the answer"
    assert card.clear_btn.accessibleName() == "Clear the chat"
    assert card._rows[0].accessibleName() == "Your question"
    assert answer_rows(card)[0].accessibleName() == "Answer"
    assert answer_rows(card)[0].copy_btn.accessibleName() == "Copy this answer"
    unnamed = [b for b in card.findChildren(QAbstractButton) if not b.accessibleName().strip()]
    assert unnamed == []
    # Tab: clear, (starters), the conversation, the box, Send / Stop - then People and the player
    empty_f = make_meeting(meetings_root, "20261029_100000", "Empty")
    page.open_notes(str(empty_f))
    _pump()
    order = []
    w = card.clear_btn
    for _ in range(40):
        w = w.nextInFocusChain()
        if w in (card._chips[0], card._chips[1], card._chips[2], card.scroll, card.input, card.send_btn, card.stop_btn):
            if w not in order:
                order.append(w)
    assert order == [card._chips[0], card._chips[1], card._chips[2], card.scroll, card.input, card.send_btn, card.stop_btn]
    assert card.input.tabChangesFocus()


def test_the_card_never_takes_the_keyboard_by_itself(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    assert not card.input.hasFocus()
    page.notes_view.reload()
    _pump()
    assert not card.input.hasFocus()


def test_copy_and_the_words_never_say_limit(build, meetings_root):
    f = make_meeting(meetings_root)
    for tier in ("pro", "free", "guest"):
        win, page, card, *_ = build(tier, f)
        texts = [card._intro.text(), card._gate_in_empty.text.text(), card.gate_bar.text.text(), card.foot.full_text(),
                 card.input.placeholderText()] + [c.text() for c in card._chips]
        for text in texts:
            assert "limit" not in text.lower() and "armenia" not in text.lower()


# ── looks ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_card_stays_light_on_a_dark_os(build, meetings_root, scheme, ai):
    f = make_meeting(meetings_root, chat=[{"role": "user", "text": "What were the decisions?"},
                                          {"role": "assistant", "text": "**Two** decisions.", "status": "done"}])
    win, page, card, *_ = build("pro", f)
    img = card.grab().toImage()
    def lum(x, y):
        c = img.pixelColor(x, y)
        return (c.red() + c.green() + c.blue()) / 3.0
    assert lum(card.width() // 2, 6) > 200                             # the card's own glass
    box = card.input.geometry()
    row = sorted(lum(x, box.center().y()) for x in range(box.left() + 6, box.right() - 6))
    assert row[len(row) // 2] > 230                                    # the box is white (most of a row of it: one
                                                                       # pixel can sit on a letter of the placeholder)
    assert card.input.palette().color(QPalette.Base).lightness() > 200
    assert card.input.palette().color(QPalette.Text).lightness() < 100
    user_label = card._rows[0].findChild(QLabel)
    assert user_label.palette().color(QPalette.WindowText).lightness() < 120
    for lb in (answer_rows(card)[0].body, card.foot):
        assert lb.palette().color(QPalette.WindowText).lightness() < 190


# ── the two layouts ─────────────────────────────────────────────────────────────────────────────────────────────

def _geometry(win, page, card, width, height):
    win.resize(width, height)
    _pump()
    nv = page.notes_view
    return nv


@pytest.mark.parametrize("width,height", [(1280, 820), (1100, 680), (1920, 1020), (1280, 600)])
def test_wide_windows_put_the_column_beside_the_notes(build, meetings_root, width, height):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f, width=width, height=height)
    nv = page.notes_view
    assert nv._narrow is False
    side = nv._side
    assert 320 <= side.width() <= 520
    from ui.pages import meeting_views
    assert card.height() >= meeting_views.CHAT_H                       # its minimum, or more when the window is tall
    assert card.minimumHeight() == meeting_views.CHAT_H == 420         # tall enough for a question and a short answer
    assert nv.article.width() >= 380
    assert side.geometry().left() > nv.article.geometry().right()      # beside, not below
    assert nv.copy_btn.text() == "Copy notes" and nv.continue_btn.text() == "Continue recording"
    vp = nv.parentWidget()
    assert nv.width() <= (vp.width() if vp is not None else nv.width())


def test_the_column_is_as_wide_whatever_the_card_holds(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    empty_w = page.notes_view._side.width()
    g = make_meeting(meetings_root, "20261029_100000", "Empty", transcript="", notes="")
    page.open_notes(str(g))
    _pump()
    assert page.notes_view._side.width() == empty_w                    # a card with a short footer or none: same column
    page.open_notes(str(f))
    _pump()
    assert page.notes_view._side.width() == empty_w


@pytest.mark.parametrize("width", [960, 1000, 1050])
def test_a_narrow_window_stacks_the_column_below_and_fits_the_toolbar(build, meetings_root, width):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f, width=width, height=700)
    nv = page.notes_view
    assert nv._narrow is True
    assert nv.article.geometry().bottom() < nv._side.geometry().top()      # below, not beside
    assert nv.article.minimumHeight() == 460
    assert card.height() == 440 and card.minimumHeight() == 440 and card.maximumHeight() == 440
    assert nv.copy_btn.text() == "Copy" and nv.continue_btn.text() == "Continue"
    assert nv.copy_btn.accessibleName() == "Copy notes"
    assert nv.continue_btn.accessibleName().startswith("Continue recording")
    # nothing is wider than the page: the toolbar, the cards
    holder = page._holders["notes"]
    viewport_w = holder.viewport().width()
    assert nv.minimumSizeHint().width() <= viewport_w
    for w in (nv.export_btn, nv.continue_btn, nv.more_btn, nv.people_card, card):
        assert w.mapTo(nv, QPoint(w.width(), 0)).x() <= nv.width()
    assert nv.more_btn.mapTo(nv, QPoint(nv.more_btn.width(), 0)).x() <= viewport_w


def test_the_layout_follows_the_window_back_and_forth_without_flicker(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f, width=1280, height=800)
    nv = page.notes_view
    states = []
    for w in (1280, 1000, 1280, 1070, 1050, 1070, 1100, 1280):
        win.resize(w, 800)
        _pump()
        states.append((w, nv._narrow))
    assert states[0][1] is False and states[1][1] is True and states[2][1] is False
    # the page is 232 px narrower than the window: it goes narrow below 830 and wide again from 846 - in between it
    # stays as it was (1070 -> a page of 838: wide when it came from wide, narrow when it came from narrow)
    assert [s[1] for s in states[3:6]] == [False, True, True]
    assert states[6][1] is False
    assert states[-1][1] is False
    # the copy label comes back after a "Copied"
    nv.copy_btn.setText("Copied")
    nv._end_flash()
    assert nv.copy_btn.text() == "Copy notes"
    win.resize(1000, 800)
    _pump()
    nv._end_flash()
    assert nv.copy_btn.text() == "Copy"


def test_at_one_and_a_half_times_the_scale_the_card_keeps_its_height_and_the_page_scrolls(build, meetings_root, qapp):
    # 150 % = a 1280 x 680 logical window: the right column needs more than the window gives, so the page scrolls
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f, width=1280, height=560)
    assert card.height() >= 420
    holder = page._holders["notes"]
    assert holder.verticalScrollBar().maximum() > 0
    assert card.input.isVisibleTo(card) and card.send_btn.isVisibleTo(card)


# ── the window's share bar ───────────────────────────────────────────────────────────────────────────────────────

def test_the_share_bar_says_so_and_show_lifts_it(build, meetings_root, monkeypatch):
    from ui import glass
    calls = []
    monkeypatch.setattr(glass, "exclude_from_capture", lambda w, on=True: calls.append(on) or bool(on))
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    assert not win.hidden_from_share and not win._share_bar.isVisibleTo(win)
    seen = []
    win.share_hidden_changed.connect(seen.append)
    assert win.hide_from_share(True) is True
    assert win.hidden_from_share and win._share_bar.isVisibleTo(win)
    assert win.share_show_btn.text() == "Show"
    win.share_show_btn.click()
    assert not win.hidden_from_share and not win._share_bar.isVisibleTo(win)
    assert calls == [True, False] and seen == [True, False]


def test_going_to_another_page_does_not_lift_it_but_closing_to_the_tray_and_turning_it_off_do(build, meetings_root,
                                                                                         monkeypatch):
    from ui import glass
    monkeypatch.setattr(glass, "exclude_from_capture", lambda w, on=True: bool(on))
    f = make_meeting(meetings_root)
    win, page, card, _s, app = build("pro", f)
    win.hide_from_share(True)
    win.open("settings")
    win.open("meetings")
    assert win.hidden_from_share                                       # showing more is not what a user means by it
    app.cfg["live_assist_private"] = False
    win.on_setting_changed("live_assist_private")
    assert not win.hidden_from_share
    win.hide_from_share(True)
    win.close()                                                        # to the tray
    assert not win.hidden_from_share and not win.isVisible()


def test_a_window_the_os_will_not_hide_says_it_is_not_hidden(build, meetings_root, monkeypatch):
    from ui import glass
    monkeypatch.setattr(glass, "exclude_from_capture", lambda w, on=True: False)
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    assert win.hide_from_share(True) is False
    assert not win.hidden_from_share and not win._share_bar.isVisibleTo(win)
