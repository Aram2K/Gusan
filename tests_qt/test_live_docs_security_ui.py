"""Live Assistant documents, the security review's regression tests (the Qt side).

* a drag that names a file on ANOTHER computer (a web page can hand a drag such a link) is
  never read: on Windows that would open a network connection and offer the PC's sign-in;
* answers written from the user's documents do not travel, as "earlier answers", to a
  cloud route the documents were not agreed to (switching the writer, Privacy mode off);
* a picture goes out whole, so an automatic answer takes only pictures its talk is about,
  and the one-time question says so.

The fixtures (the card, a real worker thread with a fake model, a temp store) are the ones of
tests_qt/test_live_documents_ui.py.
"""
import os

import pytest
from PySide6.QtCore import QMimeData, QUrl

from tests_qt.test_live_documents_ui import (_add, _ask_spy, _contract, _idle, _pdf, _png,  # noqa: F401
                                             _wait, answer, build, card, files, glass_ok, store)


# ── a drag that names another computer ──
def _urls(*urls):
    mime = QMimeData()
    mime.setUrls([QUrl(u) if isinstance(u, str) else u for u in urls])
    return mime


def test_a_drag_naming_another_computer_is_never_followed(tmp_path):
    from ui import live_docs_ui
    real = tmp_path / "a.pdf"
    real.write_bytes(b"%PDF-1.4")
    mime = _urls("file://evil.example/share/a.pdf", "file://EVIL/x/b.docx", "file:////evil/share/c.pdf",
                 QUrl.fromLocalFile(str(real)))
    paths, _more = live_docs_ui.dropped_paths(mime)
    assert [os.path.normcase(p) for p in paths] == [os.path.normcase(str(real))]
    assert live_docs_ui.wants_files(mime) is True                       # (the one real file)


@pytest.mark.skipif(os.name != "nt", reason="a UNC path is a Windows thing")
def test_unc_paths_in_any_spelling_are_refused_on_windows():
    from ui import live_docs_ui
    for spelling in ("file://server/share/a.pdf", "file:////server/share/a.pdf",
                     "file://localhost/share/a.pdf", "file:///%5C%5Cserver%5Cshare%5Ca.pdf"):
        paths, _more = live_docs_ui.dropped_paths(_urls(spelling))
        assert paths == [], spelling


def test_a_drag_of_only_another_computers_files_is_not_taken_at_all():
    from ui import live_docs_ui
    mime = _urls("file://evil.example/share/a.pdf")
    assert live_docs_ui.wants_files(mime) is False
    assert live_docs_ui.dropped_paths(mime) == ([], False)


# ── earlier answers that came from the documents ──
def _history(c):
    return [(q, a) for q, a in c._qa_history]


def test_an_answer_from_the_documents_does_not_follow_to_a_cloud_route_that_was_not_agreed(
        card, answer, store, files, monkeypatch):
    import live_docs
    c, app = card(live_docs_cloud_ok=None)
    _contract(store, files)
    first = answer(c, app, "local", question="What are the payment terms?")           # on this computer: fine
    assert live_docs.DOC_HEADER in first["context"]
    assert _wait(lambda: c._qa_history and c._qa_docs == [True])
    _ask_spy(monkeypatch, [False])                                                       # then: "keep on this computer"
    second = answer(c, app, "managed", question="And the delivery?")
    assert "Your earlier answers" not in second["context"]
    assert "45 days" not in second["context"] and "Payment is due" not in second["context"]
    assert live_docs.DOC_HEADER not in second["context"]


def test_without_a_yes_the_same_follow_up_leaves_the_documents_answer_out_when_asking_is_skipped(
        card, answer, store, files, monkeypatch):
    c, app = card(live_docs_cloud_ok=None)
    _contract(store, files)
    answer(c, app, "local", question="What are the payment terms?")
    assert _wait(lambda: c._qa_docs == [True])
    c._hold_until = 0.0                                                                  # (a typed answer holds the automatic ones)
    third = answer(c, app, "managed", question="", auto=True)                             # automatic: never asks
    assert "Payment is due" not in third["context"]


def test_after_a_yes_the_earlier_answers_go_along_as_they_always_did(card, answer, store, files):
    c, app = card(live_docs_cloud_ok=True)
    _contract(store, files)
    answer(c, app, "local", question="What are the payment terms?")
    assert _wait(lambda: c._qa_docs == [True])
    second = answer(c, app, "managed", question="And the delivery?")
    assert "Your earlier answers" in second["context"] and "Payment is due in 45 days." in second["context"]


def test_privacy_mode_turned_off_does_not_carry_a_private_answer_to_the_cloud(card, answer, store, files):
    c, app = card(live_docs_cloud_ok=False, privacy_mode=True)
    _contract(store, files)
    answer(c, app, "local", question="What are the payment terms?")                    # private: on this computer
    assert _wait(lambda: c._qa_docs == [True])
    app.cfg["privacy_mode"] = False
    nxt = answer(c, app, "managed", question="Anything else?")
    assert "Payment is due" not in nxt["context"]


def test_answers_that_did_not_use_documents_are_kept_for_a_follow_up(card, answer, store, files, monkeypatch):
    c, app = card(live_docs_cloud_ok=False)
    answer(c, app, "managed", question="What is the capital of France?")                 # no documents yet
    assert _wait(lambda: c._qa_docs == [False])
    _contract(store, files)
    second = answer(c, app, "managed", question="And Spain?")                            # documents declined for the cloud
    assert "Your earlier answers" in second["context"]
    assert "Payment is due in 45 days." in second["context"]                              # (the fake model's reply)


def test_the_flags_stay_in_step_with_the_last_two_answers(card, answer, store, files):
    c, app = card(live_docs_cloud_ok=True)
    _contract(store, files)
    for q in ("one?", "two?", "three?"):
        answer(c, app, "managed", question=q)
        assert _wait(lambda: len(c._qa_docs) == len(c._qa_history))
    assert len(c._qa_history) == 2 and c._qa_docs == [True, True]
    c._qa_history, c._qa_docs = [("q", "a")], []                                           # (something set the history alone)
    ctx = {"history": [("q", "a")]}
    kept = c._without_document_answers(ctx, (), "managed", {"live_docs_cloud_ok": False})
    assert kept["history"] == [("q", "a")]


def test_the_check_only_applies_to_a_cloud_route(card):
    c, _app = card()
    ctx = {"history": [("q", "a"), ("q2", "b")]}
    assert c._without_document_answers(ctx, (True, True), "local_llm", {}) is None
    assert c._without_document_answers(ctx, (True, True), "managed", {"live_docs_cloud_ok": True}) is None
    assert c._without_document_answers(ctx, (True, True), "managed", {"live_docs_cloud_ok": True,
                                                                         "privacy_mode": True})["history"] == []
    only_first = c._without_document_answers(ctx, (True, False), "cloud", {})
    assert only_first["history"] == [("q2", "b")]


# ── pictures ──
def test_an_automatic_answer_takes_only_a_picture_its_talk_is_about(card, answer, store, files):
    c, app = card(live_docs_cloud_ok=True)
    _add(store, _png(files, "whiteboard.png"))
    quiet = answer(c, app, "managed", question="", auto=True,
                   live_text="So what are the payment terms for the supplier?")
    assert "_image_png_b64" not in quiet["config"] and "Pictures from the user's" not in quiet["context"]
    about = answer(c, app, "managed", question="", auto=True,
                   live_text="Can you show us what is on the whiteboard?")
    assert isinstance(about["config"]["_image_png_b64"], list) and len(about["config"]["_image_png_b64"]) == 1


def test_a_typed_question_may_still_carry_a_picture(card, answer, store, files):
    c, app = card(live_docs_cloud_ok=True)
    _add(store, _png(files, "whiteboard.png"))
    seen = answer(c, app, "managed", question="Summarise my pictures")
    assert len(seen["config"]["_image_png_b64"]) == 1


def test_the_one_time_question_says_a_picture_goes_whole(card, store, files, monkeypatch):
    from ui import live_docs_ui
    c, app = card(live_docs_cloud_ok=None)
    calls = _ask_spy(monkeypatch, [False])
    live_docs_ui.ask_cloud(c, app, managed=True)
    assert "picture" not in calls[0]["body"]                                               # nothing to say yet
    _add(store, _png(files, "whiteboard.png"))
    live_docs_ui.ask_cloud(c, app, managed=True)
    assert "A picture you added is sent whole" in calls[1]["body"]
    assert "limit" not in calls[1]["body"].lower()


# ── a yes is for the kind of cloud route it was given for ──
def test_a_yes_for_gusan_cloud_is_not_a_yes_for_an_own_key(card, answer, store, files, monkeypatch):
    import live_docs
    c, app = card(live_docs_cloud_ok=True, live_docs_cloud_route="managed")
    _contract(store, files)
    agreed = answer(c, app, "managed")
    assert live_docs.DOC_HEADER in agreed["context"]                       # the route it was given for
    calls = _ask_spy(monkeypatch, [False])
    own = answer(c, app, "cloud", question="And the delivery?")
    assert len(calls) == 1
    assert calls[0]["title"] == "Use your documents with the AI service you added a key for?"
    assert live_docs.DOC_HEADER not in own["context"] and "45 days" not in own["context"]
    assert app.cfg["live_docs_cloud_ok"] is False and app.cfg["live_docs_cloud_route"] == ""


def test_a_yes_for_an_own_key_is_remembered_for_that_kind_only(card, answer, store, files, monkeypatch):
    import live_docs
    c, app = card(live_docs_cloud_ok=None)
    _contract(store, files)
    calls = _ask_spy(monkeypatch, [True, True])
    own = answer(c, app, "cloud")
    assert live_docs.DOC_HEADER in own["context"] and app.cfg["live_docs_cloud_route"] == "cloud"
    answer(c, app, "cloud", question="Again?")
    assert len(calls) == 1                                                 # not asked again for the same kind
    managed = answer(c, app, "managed", question="And the delivery?")
    assert len(calls) == 2 and calls[1]["title"] == "Use your documents with Gusan Cloud?"
    assert live_docs.DOC_HEADER in managed["context"]                      # (and it applies at once)
    assert app.cfg["live_docs_cloud_route"] == "managed"


def test_start_asks_again_when_the_writer_changed_to_the_other_kind(card, store, files, monkeypatch):
    import actions
    from tests_qt.test_live_documents_ui import _engine_ids
    c, app = card(live_docs_cloud_ok=True, live_docs_cloud_route="managed")
    _contract(store, files)
    calls = _ask_spy(monkeypatch, [True])
    monkeypatch.setattr(actions, "warm_up", lambda engine, cfg: None)
    monkeypatch.setattr(store, "warm", lambda: None)
    c._resolve_engine = lambda: (_engine_ids()["managed"], dict(app.cfg))
    c._warm_up_engine()
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    assert calls == []                                                     # Gusan Cloud: agreed already
    c._resolve_engine = lambda: (_engine_ids()["cloud"], dict(app.cfg))
    c._warm_up_engine()
    assert _wait(lambda: len(calls) == 1)
    assert calls[0]["title"].startswith("Use your documents with the AI service")


def test_the_late_request_is_not_taken_for_a_yes_given_for_the_other_kind(card, monkeypatch):
    import threading
    c, app = card(live_docs_cloud_ok=True, live_docs_cloud_route="managed")
    calls = _ask_spy(monkeypatch, [False])
    req = {"gen": 0, "managed": False, "event": threading.Event(), "answer": None}
    c._on_doc_ask(req)
    assert len(calls) == 1 and req["answer"] is False                       # it asked
    app.cfg.update(live_docs_cloud_ok=True, live_docs_cloud_route="managed")
    req = {"gen": 0, "managed": True, "event": threading.Event(), "answer": None}
    c._on_doc_ask(req)
    assert len(calls) == 1 and req["answer"] is True                        # agreed for this kind: no question


def test_earlier_document_answers_do_not_follow_to_the_other_kind_of_route(card):
    c, _app = card()
    ctx = {"history": [("q", "a"), ("q2", "b")]}
    cfg = {"live_docs_cloud_ok": True, "live_docs_cloud_route": "managed"}
    assert c._without_document_answers(ctx, (True, True), "managed", cfg) is None
    assert c._without_document_answers(ctx, (True, True), "cloud", cfg)["history"] == []


def test_the_footer_asks_again_for_the_other_kind_of_route(build, files):
    from ui.pages import live_documents as ld
    page, app = build("pro")                                                # Gusan Cloud writes
    sec = page.documents
    sec.add_paths([_pdf(files)])
    _idle(sec)
    app.cfg.update(live_docs_cloud_ok=True, live_docs_cloud_route="managed")
    page.on_setting_changed("live_docs_cloud_ok")
    assert sec.privacy_label.text() == ld.LINE_YES % "Gusan Cloud"
    app.cfg["live_docs_cloud_route"] = "cloud"                              # the yes was for an own key
    page.on_setting_changed("live_docs_cloud_route")
    assert sec.privacy_label.text() == ld.LINE_UNDECIDED and not sec.privacy_link.isVisibleTo(sec)


def test_allow_remembers_which_kind_it_was_given_for(build, files, monkeypatch):
    from ui.components import mini_prompt
    page, app = build("pro")
    sec = page.documents
    sec.add_paths([_pdf(files)])
    _idle(sec)
    app.cfg.update(live_docs_cloud_ok=False, live_docs_cloud_route="")
    page.on_setting_changed("live_docs_cloud_ok")
    monkeypatch.setattr(mini_prompt, "ask", lambda *a, **k: True)
    sec.privacy_link.click()
    assert app.cfg["live_docs_cloud_ok"] is True and app.cfg["live_docs_cloud_route"] == "managed"
    sec.privacy_link.click()                                                # "Don't send"
    assert app.cfg["live_docs_cloud_ok"] is False and app.cfg["live_docs_cloud_route"] == ""


def test_set_choice_without_a_controller_setting_still_records_the_route(qapp):
    from types import SimpleNamespace
    from ui import live_docs_ui
    app = SimpleNamespace(cfg={})
    assert live_docs_ui.set_choice(app, True, "cloud") is True
    assert app.cfg == {"live_docs_cloud_ok": True, "live_docs_cloud_route": "cloud"}
    live_docs_ui.set_choice(app, False, "cloud")
    assert app.cfg == {"live_docs_cloud_ok": False, "live_docs_cloud_route": ""}
    live_docs_ui.set_choice(app, True)                                      # (no kind given: any)
    assert app.cfg["live_docs_cloud_route"] == ""
