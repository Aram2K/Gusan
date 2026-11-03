"""Ask about this meeting - what the three independent reviews found in the card and the page, pinned (offscreen).

The AI is a scripted fake, the page is the real Meetings page in the real main window (see test_meeting_chat_ui.py,
whose fixtures these tests use). Each test failed on the first build of the branch."""
import json

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

import meeting_chat
import meeting_store
from tests_qt.chat_fakes import make_meeting
from tests_qt.test_meeting_chat_ui import (FakeAI, _inline, _pump, answer_rows, build,  # noqa: F401 (fixtures)
                                           chat_file, meetings_root, type_and_send)
from ui.components import mini_prompt


class FakeAI2(FakeAI):
    """FakeAI whose answers can say the connection dropped, or that they reached the longest answer."""

    def push(self, *a, dropped=False, cut=False, **kw):
        super().push(*a, **kw)
        self.steps[-1].update(dropped=dropped, cut=cut)
        return self

    def __call__(self, *a, **kw):
        step = dict(self.steps[0]) if self.steps else {}
        ans = super().__call__(*a, **kw)
        ans.dropped, ans.cut = bool(step.get("dropped")), bool(step.get("cut"))
        return ans


@pytest.fixture
def ai(monkeypatch):
    fake = FakeAI2()
    monkeypatch.setattr(meeting_chat, "ask", fake)
    yield fake
    fake.release()


def Q(text):
    return {"role": "user", "text": text}


def A(text, status="done", **kw):
    return dict({"role": "assistant", "text": text, "status": status}, **kw)


# ── no raw symbols, whatever way an answer ended ────────────────────────────────────────────────────────────────

def test_a_stopped_answer_and_one_cut_short_never_draw_raw_markers_even_reopened(build, meetings_root):
    f = make_meeting(meetings_root, chat=[
        Q("What were the key points?"), A("Key points: **Hir", "stopped"),
        Q("Et en francais ?"), A("Deux: **Alpha** et **Be"),
        Q("Code?"), A("Voici:\n```\nSELECT 1", "stopped")])
    win, page, card, *_ = build("pro", f)
    for _round in range(2):
        for row in answer_rows(card):
            assert "**" not in row.body.text()
            assert "```" not in row.body.text()
        card._rebuild()                                                   # (and when the meeting is opened again)
    html = answer_rows(card)[0].body.text()
    assert "Hir" in html and "Key points" in html                         # the words stay, only the symbols go
    assert "<b>Alpha</b>" in answer_rows(card)[1].body.text()


def test_an_answer_that_was_stopped_hides_a_half_written_code_mark_but_a_finished_one_keeps_its_own(build,
                                                                                                  meetings_root):
    f = make_meeting(meetings_root, chat=[
        Q("Which command?"), A("Run `pip insta", "stopped"),
        Q("Which key?"), A("Press the ` key, then Enter.")])
    win, page, card, *_ = build("pro", f)
    stopped, finished = answer_rows(card)
    assert "`" not in stopped.body.text() and "pip insta" in stopped.body.text()    # half-written: left out until it is whole
    assert "`" in finished.body.text()                                              # a finished answer may mean its mark


def test_the_conversation_a_hostile_answer_ended_in_a_marker_shows_no_symbols(build, meetings_root):
    f = make_meeting(meetings_root, chat=[Q("Hi"), A("<b>bold?</b> **unclosed")])
    win, page, card, *_ = build("pro", f)
    text = answer_rows(card)[0].body.text()
    assert "**" not in text and "&lt;b&gt;" in text                       # escaped, never read as markup


# ── a connection that dropped, an answer that reached the longest it may be ─────────────────────────────────────

def test_a_connection_that_dropped_is_kept_as_stopped_never_as_finished(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.push("The action items are: 1. send the **job", dropped=True)
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "What are the action items?")
    saved = chat_file(f)["messages"]
    assert (saved[1]["status"], saved[1].get("why")) == ("stopped", "dropped")
    row = answer_rows(card)[-1]
    assert row.status.text() == meeting_chat.DROPPED
    assert row.copy_btn.isVisibleTo(card) and row.retry_btn.isVisibleTo(card)
    assert "**" not in row.body.text() and "job" in row.body.text()
    ai.push("The whole answer.")
    type_and_send(card, "Again?")
    sent = ai.calls[1]["history"]                                         # (meeting_chat leaves a stopped answer out)
    assert [(m["role"], m.get("status"), m.get("why")) for m in sent] == [("user", None, None),
                                                                           ("assistant", "stopped", "dropped")]
    assert not row.retry_btn.isVisibleTo(card)                            # a newer question: its Try again is gone
    card._rebuild()                                                       # (reopened: only the last answer offers it)
    assert [r.retry_btn.isVisibleTo(card) for r in answer_rows(card)] == [False, False]


def test_try_again_after_a_dropped_connection_replaces_the_cut_answer(build, meetings_root, ai):
    f = make_meeting(meetings_root, chat=[Q("What are the action items?"),
                                          A("1. send the job", "stopped", why="dropped")])
    ai.push("1. Send the job description. 2. Share dates.")
    win, page, card, *_ = build("pro", f)
    row = answer_rows(card)[-1]
    assert row.retry_btn.isVisibleTo(card)
    row.retry_btn.click()
    _pump()
    assert [m["text"] for m in card._msgs] == ["What are the action items?", "1. Send the job description. 2. Share dates."]
    assert [(m["role"], m.get("status"), m.get("why")) for m in chat_file(f)["messages"]] == [
        ("user", None, None), ("assistant", "done", None)]
    assert len(answer_rows(card)) == 1
    assert ai.calls[0]["question"] == "What are the action items?"


def test_an_answer_that_reached_the_longest_it_may_be_is_kept_with_a_note(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    ai.push("Dear Anna,\n\nHere is the **plan", cut=True)
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Write a follow-up email")
    saved = chat_file(f)["messages"][1]
    assert (saved["status"], saved.get("cut")) == ("done", True)
    row = answer_rows(card)[-1]
    assert meeting_chat.CUT_SHORT in row.note.text() and row.note.isVisibleTo(card)
    assert "**" not in row.body.text() and not row.retry_btn.isVisibleTo(card)
    card._rebuild()
    assert meeting_chat.CUT_SHORT in answer_rows(card)[-1].note.text()
    ai.push("Next.")
    type_and_send(card, "And?")
    assert ai.calls[1]["history"][-1]["text"].startswith("Dear Anna")       # a whole thought: it may go back


# ── copying ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_selected_words_copy_without_the_invisible_break_points(build, meetings_root, qapp):
    text = ("Write to aram.adamyan@institutimagine.org or see https://example.com/some/long/path/here/today "
            "for details")
    f = make_meeting(meetings_root, chat=[Q("Where?"), A(text)])
    win, page, card, *_ = build("pro", f)
    body = answer_rows(card)[0].body
    assert "\u200b" in body.text()                                         # the label really has break points
    QGuiApplication.clipboard().setText("before")
    QTest.mousePress(body, Qt.LeftButton, Qt.NoModifier, QPoint(1, 2))
    QTest.mouseMove(body, QPoint(body.width() - 2, body.height() - 2))
    QTest.mouseRelease(body, Qt.LeftButton, Qt.NoModifier, QPoint(body.width() - 2, body.height() - 2))
    assert body.hasSelectedText()
    QTest.keyClick(body, Qt.Key_C, Qt.ControlModifier)
    got = QGuiApplication.clipboard().text()
    assert "\u200b" not in got
    assert "aram.adamyan@institutimagine.org" in got and "https://example.com/some/long/path/here/today" in got


def test_the_right_click_menu_copies_the_selection_clean_and_is_not_offered_without_one(build, meetings_root, qapp):
    from PySide6.QtWidgets import QMenu
    text = "Write to aram.adamyan@institutimagine.org or see https://example.com/some/long/path/here/today now"
    f = make_meeting(meetings_root, chat=[Q("Where?"), A(text)])
    win, page, card, *_ = build("pro", f)
    body = answer_rows(card)[0].body

    def menus():
        return [w for w in QApplication.topLevelWidgets() if isinstance(w, QMenu) and w.isVisible()]
    body._menu(QPoint(3, 3))
    _pump()
    assert menus() == []                                                   # nothing selected: nothing to offer
    QTest.mousePress(body, Qt.LeftButton, Qt.NoModifier, QPoint(1, 2))
    QTest.mouseMove(body, QPoint(body.width() - 2, body.height() - 2))
    QTest.mouseRelease(body, Qt.LeftButton, Qt.NoModifier, QPoint(body.width() - 2, body.height() - 2))
    assert body.hasSelectedText()
    QGuiApplication.clipboard().setText("before")
    body._menu(QPoint(3, 3))
    _pump()
    shown = menus()
    assert len(shown) == 1 and [a.text() for a in shown[0].actions()] == ["Copy"]
    shown[0].actions()[0].trigger()
    got = QGuiApplication.clipboard().text()
    assert "​" not in got and "https://example.com/some/long/path/here/today" in got
    shown[0].close()
    _pump()


def test_the_copy_button_copies_the_raw_text(build, meetings_root, qapp):
    f = make_meeting(meetings_root, chat=[Q("Mail?"), A("Write **now** to aram.adamyan@institutimagine.org")])
    win, page, card, *_ = build("pro", f)
    answer_rows(card)[0].copy_btn.click()
    assert "aram.adamyan@institutimagine.org" in QGuiApplication.clipboard().text()
    assert "\u200b" not in QGuiApplication.clipboard().text()


# ── the look: bubbles, header, buttons, columns ─────────────────────────────────────────────────────────────────

def test_a_short_question_sits_in_a_bubble_as_wide_as_its_words_and_a_long_one_wraps_inside_the_row(build, meetings_root):
    long_q = "Et qui doit envoyer la description du poste ? Je ne me souviens plus de ce qui a ete decide."
    f = make_meeting(meetings_root, chat=[Q("What were the key decisions?"), A("Several."), Q(long_q), A("Vous.")])
    win, page, card, *_ = build("pro", f)
    user_rows = [r for r in card._rows if r.accessibleName() == "Your question"]
    short, long_row = user_rows
    lb = short.findChildren(QLabel)[0]
    fm = lb.fontMetrics()
    assert lb.width() >= fm.horizontalAdvance(lb.text())                  # all of it on one line ...
    assert lb.height() < fm.lineSpacing() * 1.6                           # ... not squeezed into a narrow tall box
    lb2 = long_row.findChildren(QLabel)[0]
    assert lb2.height() > fm.lineSpacing() * 1.6                          # wraps
    bubble = lb2.parentWidget()
    assert bubble.geometry().right() <= long_row.width()                  # but never wider than the row
    assert bubble.width() > long_row.width() * 0.6                        # and it uses the room it has


def test_the_copy_under_an_answer_has_less_air_on_its_left_than_a_plain_ghost_button(build, meetings_root):
    from ui.components import Button
    f = make_meeting(meetings_root, chat=[Q("Who?"), A("Aram.")])
    win, page, card, *_ = build("pro", f)
    copy_btn = answer_rows(card)[0].copy_btn
    plain = Button("Copy", "ghost", "s", icon="copy")
    try:
        plain.ensurePolished()
        copy_btn.ensurePolished()
        assert plain.sizeHint().width() - copy_btn.sizeHint().width() >= 6    # its icon sits nearer the answer's edge
        assert copy_btn.width() >= copy_btn.sizeHint().width() - 1            # and its words still fit
    finally:
        plain.deleteLater()


def test_the_title_and_the_box_do_not_move_when_clear_chat_comes_and_goes_or_an_answer_starts(build, meetings_root, ai,
                                                                                            monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    title = [lb for lb in card.findChildren(QLabel) if lb.text() == "Ask about this meeting"][0]

    def where():
        return (title.mapTo(card, QPoint(0, 0)).y(), card.scroll.mapTo(card, QPoint(0, 0)).y(), card.input.width())
    empty = where()
    ai.push("x", parts=["Words "], block=True)
    card.input.setPlainText("Q?")
    card.send()
    _pump(lambda: card._running and "Words" in card._live.body.text())
    running = where()
    assert card.stop_btn.width() == card.send_btn.width()
    ai.release()
    _pump(lambda: not card._running)
    done = where()
    assert empty == running == done                                       # no jump: not the title, the history, the box


def test_the_stop_button_is_wide_enough_for_its_icon_and_its_word_and_send_is_as_wide(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    for btn in (card.send_btn, card.stop_btn):
        btn.ensurePolished()
        assert btn.width() >= btn.sizeHint().width()                      # "Stop" is not cut to "Stor"
    assert card.stop_btn.width() == card.send_btn.width()                 # the box keeps its size when an answer starts


def test_send_waits_for_words_and_a_starter_does_not_throw_away_a_draft(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    assert not card.send_btn.isEnabled()
    card.input.setPlainText("My own draft")
    assert card.send_btn.isEnabled()
    card._chips[0].click()
    _pump()
    assert ai.calls[0]["question"] == card._chips[0].text()
    assert card.input.toPlainText() == "My own draft"                     # what was typed is still there
    type_and_send(card, "Now this")
    assert card.input.toPlainText() == ""                                 # asking from the box empties it
    assert not card.send_btn.isEnabled()


def test_a_message_keeps_a_real_time_in_the_file(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "Q")
    stamps = [m.get("ts") for m in chat_file(f)["messages"]]
    assert all(isinstance(t, int) and t > 1_700_000_000 for t in stamps)


@pytest.mark.parametrize("width,height", [(1920, 1080), (1600, 900), (1280, 820)])
def test_the_two_cards_stay_on_the_titles_left_edge_whatever_the_screen(build, meetings_root, width, height):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f, width=width, height=height)
    nv = page.notes_view
    left = nv.article.mapTo(nv, QPoint(0, 0)).x()
    assert left == nv.title_label.mapTo(nv, QPoint(0, 0)).x() == 32      # under the title, not pushed in
    gap = nv._side.mapTo(nv, QPoint(0, 0)).x() - (left + nv.article.width())
    assert gap == 24                                                      # and never spread apart
    assert nv._side.width() <= 520 and nv.article.width() <= 620


def test_at_the_minimum_width_a_link_takes_the_reader_to_the_chat(build, meetings_root):
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f, width=960, height=600)
    nv = page.notes_view
    assert nv._narrow and nv._ask_row.isVisibleTo(nv)
    assert nv._cols.x() + nv._cols.width() == nv.width() - 32               # the column has every pixel, to the margin
    assert nv.ask_link.accessibleName() == "Go to the questions about this meeting"
    holder = page._holders["notes"]
    assert holder.verticalScrollBar().value() == 0
    win.activateWindow()
    nv.ask_link.click()
    _pump()
    assert holder.verticalScrollBar().value() > 0                         # the page scrolled down to the card
    assert card.input.hasFocus()
    win.resize(1280, 820)
    _pump()
    assert not nv._narrow and not nv._ask_row.isVisibleTo(nv)            # beside the notes it needs no link


# ── the keyboard ────────────────────────────────────────────────────────────────────────────────────────────────

def _focus_walk(card, start, stop_at):
    out, w = [], start
    for _ in range(80):
        w = w.nextInFocusChain()
        if w is stop_at:
            break
        if w.focusPolicy() != Qt.NoFocus and w.isVisibleTo(card):
            out.append(w)
    return out


def test_each_answers_copy_comes_right_after_the_conversation_not_after_the_whole_page(build, meetings_root):
    f = make_meeting(meetings_root, chat=[Q("One?"), A("First."), Q("Two?"), A("Second.", "stopped", why="dropped")])
    win, page, card, *_ = build("pro", f)
    rs = answer_rows(card)
    walk = _focus_walk(card, card.scroll, card.input)
    assert walk == [rs[0].copy_btn, rs[1].copy_btn, rs[1].retry_btn]
    # ... and a new answer joins the chain too
    walk_back = _focus_walk(card, card.clear_btn, card.scroll)
    assert card._earlier not in walk_back and walk_back == []             # (nothing else between Clear chat and the box)


def test_the_conversation_shows_a_ring_when_the_keyboard_is_on_it(build, meetings_root):
    f = make_meeting(meetings_root, chat=[Q("One?"), A("First.")])
    win, page, card, *_ = build("pro", f)
    win.activateWindow()
    _pump()
    card.scroll.setFocus(Qt.TabFocusReason)
    _pump()
    assert card.scroll.hasFocus()
    assert card.scroll.property("focusVisible") is True
    assert 'QScrollArea[chat="history"][focusVisible="true"]' in card.styleSheet()
    card.input.setFocus(Qt.TabFocusReason)
    _pump()
    assert card.scroll.property("focusVisible") is False
    card.scroll.setFocus(Qt.MouseFocusReason)                             # a click is not a keyboard visit
    _pump()
    assert card.scroll.property("focusVisible") is False


# ── a prompt over a window hidden from the share is hidden too ─────────────────────────────────────────────────────

class _Dlg:
    def __init__(self, parent):
        self.parent = parent
        self.shown = False

    def parentWidget(self):                                              # noqa: N802
        return self.parent

    def winId(self):                                                     # noqa: N802
        return 1

    def exec(self):
        self.shown = True
        return 0

    def deleteLater(self):                                               # noqa: N802
        pass


def test_a_prompt_over_a_hidden_window_is_kept_out_of_the_share_and_over_a_normal_one_is_not(qapp, monkeypatch):
    from ui import glass
    seen = []
    monkeypatch.setattr(glass, "exclude_from_capture", lambda w, on=True: seen.append((w, on)) or True)
    host = QWidget()
    host.hidden_from_share = True
    dlg = _Dlg(host)
    mini_prompt._run(dlg, False)
    assert dlg.shown and seen == [(dlg, True)]
    host.hidden_from_share = False
    mini_prompt._run(_Dlg(host), False)
    mini_prompt._run(_Dlg(None), False)
    assert len(seen) == 1                                                  # nothing else was hidden
    mini_prompt._run(_Dlg(host), True)                                     # (the Live card still asks for it itself)
    assert len(seen) == 2
    host.deleteLater()


def test_rename_and_delete_prompts_in_the_real_window_follow_the_window(build, meetings_root, monkeypatch):
    from ui import glass
    seen = []
    monkeypatch.setattr(glass, "exclude_from_capture", lambda w, on=True: seen.append(on) or True)
    monkeypatch.setattr(mini_prompt.MiniPrompt, "exec", lambda self: 0)
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    mini_prompt.confirm(page.notes_view, "Delete?", "text")
    assert seen == []                                                      # a normal window: a normal prompt
    win._share_hidden = True                                               # (what hide_from_share(True) sets)
    mini_prompt.confirm(page.notes_view, "Delete \"Live session 15:34\"?", "text")
    mini_prompt.ask_text(page.notes_view, "Rename meeting", "Name", text="Live session 15:34", ok_label="Save")
    assert seen == [True, True]
    win._share_hidden = False


def test_the_save_dialog_is_not_offered_the_meetings_name_while_the_window_is_hidden(build, meetings_root,
                                                                                    monkeypatch):
    from ui.pages import meeting_views
    asked = []
    monkeypatch.setattr(meeting_views.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: asked.append(a[2]) or ("", "")))
    f = make_meeting(meetings_root, title="Interview Anna Martin")
    win, page, card, *_ = build("pro", f)
    page.notes_view.export("markdown")
    assert "Interview Anna Martin" in asked[0]
    win._share_hidden = True
    page.notes_view.export("markdown")
    page.notes_view.export("word")
    win._share_hidden = False
    assert all("Anna" not in a and "Interview" not in a for a in asked[1:]) and len(asked) == 3
    assert asked[1].endswith("Meeting notes.md") and asked[2].endswith("Meeting notes.docx")


# ── the page and the card keep going whatever happens ──────────────────────────────────────────────────────────────

def test_a_chat_gate_that_breaks_never_stops_the_page_from_finishing(build, meetings_root, monkeypatch):
    from ui.pages import meeting_speakers_ui
    f = make_meeting(meetings_root)
    win, page, card, *_ = build("pro", f)
    done = []
    monkeypatch.setattr(meeting_speakers_ui, "after_render", lambda view: done.append(1))
    monkeypatch.setattr(meeting_chat, "pick_engine", lambda *a, **k: (_ for _ in ()).throw(TypeError("odd config")))
    page.notes_view.reload()
    _pump()
    assert done == [1]                                                     # the speaker banners were still drawn
    assert page.notes_view.copy_btn.isEnabled()                            # and so was the rest of the page


def test_clear_chat_clears_the_meeting_it_was_asked_about_when_the_page_moved_on_inside_the_prompt(build,
                                                                                     meetings_root, monkeypatch):
    a = make_meeting(meetings_root, "20261030_153400", "First", chat=[Q("A question"), A("A answer")])
    b = make_meeting(meetings_root, "20261029_100000", "Second", chat=[Q("B question"), A("B answer")])
    win, page, card, *_ = build("pro", a)

    def navigate(*args, **kw):
        page.open_notes(str(b))                                           # a tray click, the calendar, a Stop ...
        _pump(lambda: [m["text"] for m in card._msgs][:1] == ["B question"])
        return True                                                       # ... and then the prompt is confirmed
    monkeypatch.setattr(mini_prompt, "confirm", navigate)
    card.clear()
    assert [m["text"] for m in card._msgs] == ["B question", "B answer"]
    assert [m["text"] for m in chat_file(b)["messages"]] == ["B question", "B answer"]
    assert _pump(lambda: chat_file(a)["messages"] == [])                 # the chat the user said yes to is the one cleared


def test_turning_privacy_mode_on_stops_an_answer_that_is_coming_from_the_cloud(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("x", parts=["Words from the cloud "], block=True)
    win, page, card, session, app = build("pro", f)
    type_and_send(card, "Q?")
    assert _pump(lambda: card._running and "Words from the cloud" in card._live.body.text())
    app.cfg["privacy_mode"] = True
    page.on_setting_changed("privacy_mode")                               # how a settings change reaches the page
    assert not card._running                                              # the connection is closed, not left to finish
    saved = chat_file(f)["messages"]
    assert (saved[1]["status"], saved[1]["text"].startswith("Words from the cloud")) == ("stopped", True)
    assert card.stop_btn.isVisibleTo(card) is False
    ai.release()
    _pump()
    assert len(chat_file(f)["messages"]) == 2                              # the late result changed nothing


def test_signing_out_stops_an_answer_that_is_coming_from_the_cloud(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("x", parts=["Words from the cloud "], block=True)
    win, page, card, session, app = build("pro", f)
    type_and_send(card, "Q?")
    assert _pump(lambda: card._running and "Words from the cloud" in card._live.body.text())
    app.set_tier("guest")                                                  # signed out from another window
    page.refresh_account()                                                 # how the page hears about it
    assert not card._running
    assert chat_file(f)["messages"][1]["status"] == "stopped"
    assert card._gate[0] == "signed_out"
    ai.release()


def test_a_cloud_answer_goes_on_when_a_setting_that_does_not_touch_the_cloud_changes(build, meetings_root, ai,
                                                                                    monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("Whole answer.", parts=["Whole "], block=True)
    win, page, card, session, app = build("pro", f)
    type_and_send(card, "Q?")
    assert _pump(lambda: card._running and "Whole" in card._live.body.text())
    card.refresh_gate()
    assert card._running
    ai.release()
    assert _pump(lambda: not card._running)
    assert chat_file(f)["messages"][1]["status"] == "done"


def test_the_engine_a_stopped_answer_is_labelled_with_is_the_one_that_was_running(build, meetings_root, ai, monkeypatch):
    from ui.pages import meeting_chat_ui
    monkeypatch.setattr(meeting_chat_ui, "SYNC_ASK", False)
    f = make_meeting(meetings_root)
    ai.push("x", parts=["Some words "], block=True)
    win, page, card, session, app = build("pro", f)
    type_and_send(card, "Q?")
    assert _pump(lambda: card._running and "Some words" in card._live.body.text())
    running = card._choice.label                                           # who is writing this answer
    app.cfg["action_model"] = "api_openai_compatible"                      # another AI is picked while it writes
    app.cfg["action_api_key"] = "sk-1"
    card.refresh_gate()
    assert card._choice.label != running                                   # what would answer next is another one
    card.stop()
    assert chat_file(f)["messages"][1]["engine"] == running
    ai.release()


# ── a chat that could not be read is never written over ───────────────────────────────────────────────────────────

def test_a_chat_file_that_cannot_be_read_is_kept_aside_when_the_conversation_goes_on(build, meetings_root, ai):
    f = make_meeting(meetings_root)
    older = '{"version": 2, "messages": [{"role": "user", "text": "from a newer Gusan"}]}'
    (f / "chat.json").write_text(older, encoding="utf-8")
    win, page, card, *_ = build("pro", f)
    assert card._msgs == [] and card._saveable
    type_and_send(card, "A new question")
    assert chat_file(f)["messages"][0]["text"] == "A new question"
    assert "from a newer Gusan" in (f / "chat.json.unreadable").read_text(encoding="utf-8")


def test_a_chat_file_that_cannot_even_be_put_aside_is_left_alone(build, meetings_root, ai, monkeypatch):
    f = make_meeting(meetings_root)
    older = "{bad json"
    (f / "chat.json").write_text(older, encoding="utf-8")
    monkeypatch.setattr(meeting_store, "keep_unreadable_chat", lambda folder: False)
    win, page, card, *_ = build("pro", f)
    assert not card._saveable
    type_and_send(card, "A new question")
    assert [m["text"] for m in card._msgs][0] == "A new question"           # the conversation still works ...
    assert (f / "chat.json").read_text(encoding="utf-8") == older           # ... but the old file is not touched


# ── a long chat stays quick ────────────────────────────────────────────────────────────────────────────────────────

def test_a_long_chat_opens_with_its_newest_messages_and_shows_earlier_ones_on_request(build, meetings_root, ai):
    from ui.pages import meeting_chat_ui
    msgs = []
    for i in range(35):
        msgs += [Q("Question %d" % i), A("Answer %d" % i)]
    f = make_meeting(meetings_root, chat=msgs)
    win, page, card, *_ = build("pro", f)
    n = meeting_chat_ui.SHOWN_MESSAGES
    drawn = [r for r in card._rows]
    assert len(drawn) <= n + 1 and card._earlier.isVisibleTo(card)
    assert drawn[0].accessibleName() == "Your question"                     # an answer never comes without its question
    assert len(card._msgs) == 70                                            # all of it is kept in memory
    first = card._first
    card.show_earlier()
    _pump()
    assert card._first < first and len(card._rows) > len(drawn)
    while card._earlier.isVisibleTo(card):
        card.show_earlier()
        _pump()
    assert card._first == 0 and len(card._rows) == 70 and not card._earlier.isVisibleTo(card)
    ai.push("Newest answer.")
    type_and_send(card, "Newest question")
    saved = chat_file(f)["messages"]
    assert len(saved) == 72 and saved[0]["text"] == "Question 0"            # the file always has all of it


def test_a_new_question_in_a_long_chat_keeps_every_earlier_message_in_the_file(build, meetings_root, ai):
    msgs = []
    for i in range(40):
        msgs += [Q("Question %d" % i), A("Answer %d" % i)]
    f = make_meeting(meetings_root, chat=msgs)
    ai.push("Fresh.")
    win, page, card, *_ = build("pro", f)
    type_and_send(card, "One more")
    saved = chat_file(f)["messages"]
    assert [m["text"] for m in saved][:2] == ["Question 0", "Answer 0"]
    assert [m["text"] for m in saved][-2:] == ["One more", "Fresh."]
    assert json.dumps(saved)                                                 # (and it is still one valid file)


# ── the history's top edge, and the air between list items ───────────────────────────────────────────────────────

def test_a_hairline_marks_the_top_of_the_history_only_while_it_is_scrolled(build, meetings_root):
    msgs = []
    for i in range(8):
        msgs += [Q("Question number %d: what was said about the topic?" % i),
                 A("The key decisions were:\n\n- A **second interview** is planned.\n- The role stays.\n\nMore words here.")]
    f = make_meeting(meetings_root, chat=msgs)
    win, page, card, *_ = build("pro", f)
    bar = card.scroll.verticalScrollBar()
    assert bar.maximum() > 0
    rule = card._rule
    assert rule.height() == 1 and rule.width() == card.scroll.viewport().width()
    bar.setValue(0)
    _pump()
    assert rule.property("scrolled") is False                              # at the top: nothing to mark
    bar.setValue(bar.maximum() // 2)
    _pump()
    assert rule.property("scrolled") is True
    x = card.scroll.viewport().width() - 40                                # (right of the bubbles: nothing but the card)
    with_rule = card.scroll.viewport().grab().toImage().pixelColor(x, 0)
    rule.setProperty("scrolled", False)                                    # the same moment, the line switched off
    rule.style().unpolish(rule)
    rule.style().polish(rule)
    without = card.scroll.viewport().grab().toImage().pixelColor(x, 0)
    assert with_rule != without                                            # the stylesheet really paints the top row
    card._sync_rule()                                                      # (back to what the scroll says)
    bar.setValue(0)
    _pump()
    assert rule.property("scrolled") is False                              # back at the top: it goes again
    assert rule.testAttribute(Qt.WA_TransparentForMouseEvents)             # (it never takes a click)


def test_the_items_of_a_list_in_an_answer_have_a_little_air_between_them(build, meetings_root):
    f = make_meeting(meetings_root, chat=[Q("Decisions?"), A("- First point.\n- Second point.\n\n1. One\n2. Two")])
    win, page, card, *_ = build("pro", f)
    html = answer_rows(card)[0].body.text()
    assert html.count("<li style=\"margin-top:3px;\">") == 4              # bullets and numbers alike
    assert "<li>" not in html
