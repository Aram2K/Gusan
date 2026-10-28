"""The Live Assistant page's ONE Context card (ui/pages/live_documents.py ContextCard).

The text box on top (what the user writes: who they are, what the calls are about, or a RULE like
"Translate everything you hear into French"), a paper-clip in its corner, the switch "Apply to
everything I hear" with its plain words and chip, the calm offer (never switching by itself, a No
remembered), and directly beneath: the documents with their "Use in answers" switches - all in one
card, which is itself the drop target ("Drop files to add"). The documents' own rows, notes,
privacy footer and worker-thread reading are in test_live_documents_ui.py.
"""
import time

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QWidget

from tests import live_docs_fixtures as fx
from tests_qt.conftest import make_stub_app

RULE = "Everything you hear, translate it into French."
LIVE_CFG = {"live_assist_private": True, "live_assist_auto_answer": True,
            "live_assist_screen_auto": True, "live_assist_language": "",
            "live_assist_output_language": "", "live_assist_theme": "light",
            "live_assist_opacity": 0.96, "live_assist_context": ""}


def _wait(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


@pytest.fixture
def store(tmp_path, monkeypatch):
    import live_docs
    st = live_docs.DocStore(tmp_path / "live_docs")
    monkeypatch.setattr(live_docs, "get_store", lambda: st)
    return st


@pytest.fixture
def files(tmp_path):
    root = tmp_path / "incoming"
    root.mkdir()

    def write(name, data):
        path = root / name
        path.write_bytes(data)
        return str(path)
    return write


@pytest.fixture
def build(qapp, store, monkeypatch):
    """build(tier="pro", width=1100, **cfg) -> (page, app). set_setting writes cfg and tells the page,
    as the controller and the shell do."""
    from ui import theme
    from ui.pages import live_assistant
    monkeypatch.setattr(live_assistant, "load_sessions", lambda: [])
    made = []

    def _build(tier="pro", width=1100, **cfg):
        app = make_stub_app(tier)
        app.cfg.update(LIVE_CFG)
        app.cfg.update(cfg)
        app.live_assist = None
        holder = {}

        def set_setting(key, value):
            app.calls.append(("set_setting", key, value))
            app.cfg[key] = value
            page = holder.get("page")
            if page is not None:
                page.on_setting_changed(key)
            return True
        app.set_setting = set_setting
        app.show_main = lambda *a, **k: app.calls.append(("show_main",) + a)
        host = QWidget()
        theme.apply(host)
        host.resize(width, 1500)
        from ui.pages import live_assistant as la
        page = la.create(app)
        page.setParent(host)
        page.resize(width, 1500)
        holder["page"] = page
        host.show()
        page.on_open()
        QApplication.processEvents()
        assert _wait(lambda: page.documents._loaded)
        made.append(host)
        return page, app

    yield _build
    for h in made:
        try:
            h.hide()
            h.deleteLater()
        except RuntimeError:
            pass
    QApplication.processEvents()


def _writes(app, key=None):
    return [c for c in app.calls if c[0] == "set_setting" and (key is None or c[1] == key)]


def _mime(*paths, urls=()):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths] + [QUrl(u) for u in urls])
    return mime


def _drag_enter(widget, mime):
    ev = QDragEnterEvent(QPoint(120, 120), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    widget.dragEnterEvent(ev)
    return ev


def _drop(widget, mime):
    ev = QDropEvent(QPointF(120, 120), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    widget.dropEvent(ev)
    return ev


def _idle(section):
    assert _wait(lambda: not section.is_reading()), "still reading"


def _pdf(files, name="Q3 plan.pdf"):
    return files(name, fx.make_pdf(["Budget and hiring plan."] * 3))


def _docx(files, name="Supplier contract.docx"):
    return files(name, fx.make_docx(["The supplier delivers within 30 days."]))


# ═══════════════════ one card ═══════════════════
def test_the_text_box_and_the_documents_are_one_card_the_box_on_top(build):
    page, _ = build()
    card = page.context_card
    assert card.accessibleName() == "Context"
    assert card.isAncestorOf(page.context_edit) and card.isAncestorOf(page.documents)
    assert page.documents is card.documents and page.context_edit is card.context_edit
    top = page.context_edit.mapTo(card, QPoint(0, 0)).y()
    docs = page.documents.mapTo(card, QPoint(0, 0)).y()
    assert top < docs
    # no separate "Documents" card any more: its own heading sits inside this one
    titles = [w.text() for w in page.findChildren(QWidget) if hasattr(w, "text") and callable(w.text)
              and w.text() == "Context"]
    assert titles == ["Context"]
    assert page.documents.add_button.text() == "Add documents"
    assert "About my calls" not in " ".join(w.text() for w in page.findChildren(QWidget)
                                            if hasattr(w, "text") and callable(w.text) and isinstance(w.text(), str))


def test_the_placeholder_says_what_to_write_including_a_rule(build):
    page, _ = build()
    hint = page.context_edit.placeholderText()
    assert "Who you are and what your calls are about" in hint
    assert "Translate everything you hear into French" in hint


def test_a_paper_clip_sits_in_the_box_corner_and_adds_documents(build, store, files, monkeypatch):
    page, _ = build()
    box, clip = page.context_edit, page.context_edit.clip
    assert clip.parent() is box and clip.accessibleName() == "Add documents"
    assert clip.x() > box.width() // 2 and clip.y() >= box.height() // 3     # bottom-right, inside the frame
    assert box.viewport().width() <= box.width() - clip.width()              # the text wraps before it
    paths = [_pdf(files), _docx(files)]
    monkeypatch.setattr(page.documents, "ask_open_paths", lambda: paths)
    clip.click()
    _idle(page.documents)
    assert store.count() == 2


def test_the_add_documents_button_and_the_clip_open_the_same_picker(build, monkeypatch):
    page, _ = build()
    calls = []
    monkeypatch.setattr(page.documents, "ask_open_paths", lambda: calls.append("picker") or [])
    page.documents.add_button.click()
    page.context_edit.clip.click()
    assert calls == ["picker", "picker"]


# ═══════════════════ the switch ═══════════════════
def test_the_switch_waits_for_a_rule_and_says_so(build):
    from ui.pages import live_documents as ld
    page, app = build()
    card = page.context_card
    assert not card.rules_toggle.isChecked() and not card.rules_toggle.isEnabled()
    assert card.rules_hint.text() == ld.RULE_HINT_NONE == "Write a rule above first."
    assert not card.scope_chip.isVisibleTo(card)
    page.context_edit.setPlainText("Call with Acme's data team.")
    assert card.rules_toggle.isEnabled() and not card.rules_toggle.isChecked()
    assert card.rules_hint.text() == ld.RULE_HINT_OFF
    assert card.scope_chip.isVisibleTo(card) and card.scope_chip.text() == "Questions only"
    assert card.rules_toggle.accessibleName() == "Apply to everything I hear"


def test_typing_never_writes_the_switch_only_a_click_does(build):
    page, app = build()
    page.context_edit.setPlainText(RULE)
    page.context_edit.setPlainText(RULE + " Keep names.")
    assert _writes(app, "live_assist_rules_everything") == []
    assert _writes(app, "live_assist_rules_declined") == []
    assert app.cfg.get("live_assist_rules_everything", False) is False


def test_turning_it_on_says_everything_you_hear_and_is_remembered(build):
    from ui.pages import live_documents as ld
    page, app = build()
    card = page.context_card
    page.context_edit.setPlainText(RULE)
    card.rules_toggle.click()
    assert _writes(app, "live_assist_rules_everything") == [("set_setting", "live_assist_rules_everything", True)]
    assert card.rules_toggle.isChecked() and card.rules_active()
    assert card.scope_chip.text() == "Everything you hear" and card.scope_chip.property("tone") == "blue"
    assert card.rules_hint.text() == ld.RULE_HINT_ON
    assert not card.offer.isVisibleTo(card)
    card.rules_toggle.click()
    assert app.cfg["live_assist_rules_everything"] is False
    assert card.scope_chip.text() == "Questions only" and card.scope_chip.property("tone") == "neutral"


def test_it_shows_how_the_settings_have_it_when_the_page_opens(build):
    page, app = build(live_assist_context=RULE, live_assist_rules_everything=True)
    card = page.context_card
    assert card.rules_toggle.isChecked() and card.scope_chip.text() == "Everything you hear"
    assert page.context_edit.toPlainText() == RULE
    assert not card.offer.isVisibleTo(card)


def test_the_floating_card_changing_it_reaches_the_page(build):
    page, app = build(live_assist_context=RULE)
    card = page.context_card
    assert not card.rules_toggle.isChecked()
    app.cfg["live_assist_rules_everything"] = True                           # the floating card announced it
    page.on_setting_changed("live_assist_rules_everything")
    assert card.rules_toggle.isChecked() and card.scope_chip.text() == "Everything you hear"
    assert _writes(app, "live_assist_rules_everything") == []                # the page never wrote it back
    app.cfg["live_assist_context"] = "Summarise each part in one line."
    page.on_setting_changed("live_assist_context")
    assert page.context_edit.toPlainText() == "Summarise each part in one line."


def test_clearing_the_text_turns_the_words_back_to_nothing_to_apply(build):
    page, app = build(live_assist_context=RULE, live_assist_rules_everything=True)
    card = page.context_card
    page.context_edit.setPlainText("")
    assert not card.rules_toggle.isEnabled() and not card.rules_active()
    assert card.rules_hint.text() == "Write a rule above first."
    assert not card.scope_chip.isVisibleTo(card)


# ═══════════════════ the offer ═══════════════════
def test_a_rule_text_gets_a_calm_offer_that_asks_never_acts(build):
    page, app = build()
    card = page.context_card
    page.context_edit.setPlainText(RULE)
    assert card.offer.isVisibleTo(card)
    assert card.offer_text.text() == "This sounds like a rule for everything you hear. Apply it to every sentence?"
    assert (card.offer_yes.text(), card.offer_no.text()) == ("Yes", "No")
    assert card.offer_text.textFormat() == Qt.PlainText
    assert not card.rules_toggle.isChecked()
    assert app.cfg.get("live_assist_rules_everything", False) is False
    page.context_edit.setPlainText("Call with Acme's data team. Keep answers short.")
    assert not card.offer.isVisibleTo(card)


def test_yes_turns_it_on(build):
    page, app = build()
    card = page.context_card
    page.context_edit.setPlainText(RULE)
    card.offer_yes.click()
    assert app.cfg["live_assist_rules_everything"] is True
    assert card.rules_toggle.isChecked() and not card.offer.isVisibleTo(card)
    assert card.scope_chip.text() == "Everything you hear"


def test_no_is_remembered_for_that_text_not_asked_twice(build):
    import live_rules
    page, app = build()
    card = page.context_card
    page.context_edit.setPlainText(RULE)
    card.offer_no.click()
    assert app.cfg["live_assist_rules_declined"] == live_rules.declined_mark(RULE)
    assert not card.offer.isVisibleTo(card) and not card.rules_toggle.isChecked()
    page.context_edit.setPlainText(RULE + " ")                               # the same words
    assert not card.offer.isVisibleTo(card)
    page.context_edit.setPlainText("Translate everything you hear into German.")
    assert card.offer.isVisibleTo(card)                                      # new words: asked again


def test_the_offer_is_gone_once_it_is_on(build):
    page, app = build(live_assist_context=RULE, live_assist_rules_everything=True)
    assert not page.context_card.offer.isVisibleTo(page.context_card)


# ═══════════════════ the whole card takes files ═══════════════════
def test_files_held_over_the_page_veil_the_context_card_and_say_so(build, files):
    from ui.pages import live_documents as ld
    page, _ = build()
    card = page.context_card
    assert page.acceptDrops() and not page.context_edit.acceptDrops()        # a drop is a document, not a pasted path
    assert ld.DROP == "Drop files to add"
    assert not card._overlay.isVisibleTo(card)
    assert _drag_enter(page, _mime(_pdf(files))).isAccepted()
    assert card._overlay.isVisibleTo(card)
    assert card._overlay.geometry() == card.rect()                           # over the whole card
    assert not page.documents._overlay.isVisibleTo(page.documents)           # (one veil, not two)
    card.drag_leave()
    assert not card._overlay.isVisibleTo(card)


def test_any_kind_of_file_dropped_is_taken_the_unreadable_one_gets_a_calm_note(build, store, files):
    page, _ = build()
    junk = files("old.xyz", b"\x00\x01\x02garbage\xff" * 80)
    assert _drop(page, _mime(_pdf(files), junk)).isAccepted()
    _idle(page.documents)
    assert store.count() == 1
    assert [n.name for n in page.documents._notes] == ["old.xyz"]
    assert "can't read this kind of file yet" in page.documents._notes[0].message
    assert not page.context_card._overlay.isVisibleTo(page.context_card)


def test_a_drop_on_the_card_itself_adds_them_too(build, store, files):
    page, _ = build()
    card = page.context_card
    assert card.drag_enter(_mime(_docx(files))) is True
    assert card.drop(_mime(_docx(files))) == 1
    _idle(page.documents)
    assert store.count() == 1 and not card._overlay.isVisibleTo(card)


def test_links_and_text_are_not_files(build, store):
    page, _ = build()
    assert not _drag_enter(page, _mime(urls=["https://example.com/a.pdf"])).isAccepted()
    text = QMimeData()
    text.setText("just words")
    assert not _drag_enter(page, text).isAccepted()
    assert not page.context_card._overlay.isVisibleTo(page.context_card)
    assert store.count() == 0


def test_hiding_the_page_clears_the_veil(build, files):
    page, _ = build()
    _drag_enter(page, _mime(_pdf(files)))
    assert page.context_card._overlay.isVisibleTo(page.context_card)
    page.hide()
    assert not page.context_card._overlay.isVisibleTo(page.context_card)


# ═══════════════════ with documents, and in a narrow window ═══════════════════
def test_the_documents_sit_under_the_rule_with_their_switches(build, store, files):
    page, _ = build(live_assist_context=RULE, live_assist_rules_everything=True)
    card = page.context_card
    page.documents.add_paths([_pdf(files), _docx(files)])
    _idle(page.documents)
    assert len(page.documents._rows) == 2
    box_bottom = page.context_edit.mapTo(card, QPoint(0, page.context_edit.height())).y()
    for row in page.documents._rows.values():
        assert row.mapTo(card, QPoint(0, 0)).y() > box_bottom
        assert row.toggle.accessibleName().startswith("Use ") and row.toggle.accessibleName().endswith(" in answers")
    assert page.documents.col_label.text() == "Use in answers"


def test_a_narrow_window_keeps_everything_inside_the_card(build, store, files):
    page, _ = build(width=700, live_assist_context=RULE)
    card = page.context_card
    page.documents.add_paths([_pdf(files, "A very long quarterly report about everything the company did in the third quarter.pdf"),
                              _docx(files)])
    _idle(page.documents)
    QApplication.processEvents()
    assert card.width() <= page.width()
    for w in (card.context_edit, card.rules_row, card.offer, card.documents.add_button,
              card.scope_chip, card.rules_toggle):
        if not w.isVisibleTo(card):
            continue
        r = w.mapTo(card, QPoint(0, 0))
        assert r.x() >= 0 and r.x() + w.width() <= card.width() + 1, w
    for row in page.documents._rows.values():
        assert row.mapTo(card, QPoint(0, 0)).x() + row.width() <= card.width() + 1
        assert row.name_label.text().endswith("…") or len(row.name_label.full_text()) < 40


# ═══════════════════ "Apply to everything I hear" must not outlive its rule ═══════════════════
def test_clearing_the_text_turns_the_switch_off_and_says_so_once(build):
    import live_rules
    page, app = build("pro", live_assist_context=RULE, live_assist_rules_everything=True)
    card = page.context_card
    assert card.rules_toggle.isChecked() and card.rules_active()
    page.context_edit.setPlainText("")
    page._save_context()
    assert app.cfg["live_assist_rules_everything"] is False
    assert _writes(app, "live_assist_rules_everything")[-1] == ("set_setting", "live_assist_rules_everything", False)
    assert not card.rules_toggle.isChecked()
    assert card.rules_hint.text() == live_rules.SWITCHED_OFF_EMPTY          # one calm line, in the switch's own hint
    page.context_edit.setPlainText("Translate everything you hear into German.")   # typing goes on: the note is old news
    assert card.rules_hint.text() != live_rules.SWITCHED_OFF_EMPTY
    assert not card.rules_toggle.isChecked()                                 # and a new rule never switches itself on


def test_a_text_that_is_no_longer_a_rule_turns_the_switch_off(build):
    import live_rules
    page, app = build("pro", live_assist_context=RULE, live_assist_rules_everything=True)
    page.context_edit.setPlainText("Call with Acme about the migration.")
    page._save_context()
    assert app.cfg["live_assist_rules_everything"] is False
    assert page.context_card.rules_hint.text() == live_rules.SWITCHED_OFF_NOT_RULE
    assert app.cfg["live_assist_context"] == "Call with Acme about the migration."       # the text itself is kept


def test_a_different_rule_keeps_the_switch_on_and_says_so(build):
    import live_rules
    page, app = build("pro", live_assist_context=RULE, live_assist_rules_everything=True)
    page.context_edit.setPlainText("Translate everything you hear into German.")
    page._save_context()
    assert app.cfg["live_assist_rules_everything"] is True
    assert page.context_card.rules_toggle.isChecked()
    assert page.context_card.rules_hint.text() == live_rules.RULE_CHANGED     # on, but never silently


def test_with_the_switch_off_nothing_is_said_when_the_text_changes(build):
    import live_rules
    page, app = build("pro", live_assist_context=RULE, live_assist_rules_everything=False)
    page.context_edit.setPlainText("")
    page._save_context()
    assert _writes(app, "live_assist_rules_everything") == []
    assert page.context_card.rules_hint.text() not in (live_rules.SWITCHED_OFF_EMPTY, live_rules.RULE_CHANGED)
