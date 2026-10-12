"""Markdown shown, copied and exported the way people read it (Gusan 2.0,
D9 / D10) on the surfaces outside the Smart Actions page: Home's Recent and
the tray (which shows Home's rows), the meeting notes view, the clipboard.

Markdown stays the machine format (history, notes.md); these surfaces render
it through rich_text. A plain dictation (exact words) is never changed."""
from datetime import datetime

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QLabel, QMenu

ENTRIES = [
    {"text": "## Q4 roadmap - Meeting Notes\n\n- [ ] Ship", "backend": "meeting",
     "timestamp": "2026-10-04 10:00:00"},
    {"text": "1. **Polish** the deck\n2. Send it", "action": "smart_auto", "timestamp": "2026-10-04 09:00:00"},
    {"text": "Bonjour à tous, **vraiment**.", "action": "translate", "timestamp": "2026-10-04 08:30:00"},
    {"text": "Merci à tous.", "action": "translate", "timestamp": "2026-10-04 08:00:00"},
    {"text": "# not a heading, I said hash", "timestamp": "2026-10-04 07:00:00"},
]


def test_home_rows_render_only_smart_and_meeting_markdown():
    from ui.pages import home as H
    items = H.recent_items(ENTRIES, [], [])
    assert [(it["kind"], it["main"]) for it in items] == [
        ("meeting", "Q4 roadmap"),
        ("smart", "1. Polish the deck 2. Send it"),
        ("smart", "Bonjour à tous, vraiment."),
        ("smart", "Merci à tous."),                          # prose: byte-identical
        ("dictation", "# not a heading, I said hash"),       # exact words: never rendered
    ]
    assert [bool(it.get("rich")) for it in items] == [True, True, True, True, False]
    assert H.plain_line("Line one\nline two") == "Line one line two"


def test_the_tray_shows_home_rows_without_markdown(qapp, monkeypatch):
    from tests_qt.test_page_floating import full_app
    from ui import tray_menu
    from ui.pages import home
    monkeypatch.setattr(home, "load_sources", lambda: (list(ENTRIES), [], []))
    tr = tray_menu.build_tray_menu(full_app("pro"), QMenu())
    try:
        tr.refresh_recent(sync=True)
        rows = [t for _k, t in tr.visible_texts(tr.recent)]
        assert "1. Polish the deck 2. Send it" in rows and "Q4 roadmap" in rows
        assert not any(mark in t for t in rows for mark in ("**", "##", "[ ]"))
    finally:
        tr.menu.deleteLater()
        QApplication.processEvents()


def test_copy_rich_puts_html_and_nice_text_and_leaves_prose_alone(qapp):
    from ui.pages import home as H
    cb = QGuiApplication.clipboard()
    assert H.copy_rich("## Plan\n- [ ] Call David (Owner: Anna)\n- [x] Book the room")
    assert cb.text() == "Plan\n☐ Call David (Anna)\n☑ Book the room"
    html = cb.mimeData().html()
    assert "<h3>Plan</h3>" in html and "☐&nbsp;Call David" in html
    prose = "Hi Anna,\n\nThe timeline is attached.\n\nBest,"
    assert H.copy_rich(prose)
    assert cb.text() == prose and not cb.mimeData().hasHtml()


def test_meeting_views_keeps_its_note_helpers_on_rich_text():
    import rich_text
    from ui.pages import meeting_views as v
    assert v.parse_notes is rich_text.parse_notes and v.notes_plain is rich_text.notes_plain
    assert v.toggle_task_line is rich_text.toggle_task_line and v.parse_task is rich_text.parse_task
    assert v.md_inline is rich_text.inline_html
    md = "## S\n1. [ ] Numbered task\n2. Second\n- [x] Done (Owner: Al)\n```\ncode\n```"
    assert v.parse_notes(md) == [("head", "S"), ("task", (False, "Numbered task", 1)), ("bullet", "Second"),
                                 ("task", (True, "Done (Owner: Al)", 3)), ("para", "code")]
    # line_index is the notes.md line: ticking rewrites exactly that one.
    assert v.toggle_task_line(md, 1, True).splitlines()[1] == "1. [x] Numbered task"
    assert v.toggle_task_line(md, 3, False).splitlines()[3] == "- [ ] Done (Owner: Al)"
    assert v.strip_md("**Bold** and *it*") == "Bold and it"


def test_the_notes_view_never_shows_markdown_symbols(qapp):
    from ui.pages import meeting_views as v
    doc = v.NotesDoc()
    try:
        doc.render("## Summary\nWe **agreed** on `May`.\n\n**Decisions:**\n1. Ship it\n"
                   "- [ ] Call the printer (Owner: Al, Due: Fri)\n> Maybe later")
        shown = " ".join(w.text() for w in doc.findChildren(QLabel))
        for raw in ("##", "**", "[ ]", "`", "1. Ship"):
            assert raw not in shown, raw
        assert "<b>agreed</b>" in shown and "Decisions" in shown and "Ship it" in shown
        assert [(t.task, t.owner, t.due) for t in doc.tasks] == [("Call the printer", "Al", "by Fri")]
        # The transcript shown when there are no notes: given as blocks, rich text kept.
        doc.render("", blocks=[("head", "Transcript"), ("html", "<b>Speaker 1</b>&nbsp; Hello &amp; welcome.")])
        shown = [w.text() for w in doc.findChildren(QLabel) if "Hello" in w.text()]
        assert shown and "<b>Speaker 1</b>" in shown[0] and doc.tasks == []
    finally:
        doc.deleteLater()
        QApplication.processEvents()


def test_transcript_rows_pack_long_lines_and_keep_labels():
    from ui.pages import meeting_views as v
    talk = " ".join("Sentence %d ends here." % i for i in range(60))
    rows = v.transcript_rows("Anna: " + talk + "\n\n— The recording stopped here —\nBob: Ok.")
    assert rows[0][0] == "Anna" and all(r[0] == "" for r in rows[1:-2])
    assert rows[-2] == ("", "— The recording stopped here —", True) and rows[-1] == ("Bob", "Ok.", False)
    body = " ".join(r[1] for r in rows[:-2])
    assert body == talk                                       # every word, in order
    assert all(len(r[1]) <= 550 for r in rows)
    # An Armenian line without spaces after a full stop still splits at "։ ".
    hy = " ".join("Սա %d նախադասություն է։" % i for i in range(60))
    assert len(v.transcript_rows(hy)) > 1
    html = v.transcript_html("Anna: " + talk)
    assert html.count("<p ") == len(v.transcript_rows("Anna: " + talk)) and ">Anna</b>" in html
    assert v.transcript_html("") .count("No transcript for this meeting.") == 1


def test_home_read_copy_kinds(qapp, tmp_path, monkeypatch):
    import meeting_store
    from ui.pages import home as H
    monkeypatch.setattr(meeting_store, "load_notes", lambda ref: "## S\n- [ ] x" if ref == "with" else "")
    monkeypatch.setattr(meeting_store, "load_transcript", lambda ref: "You: Hi. " * 3)
    assert H.read_copy(("meeting", "with")) == ("## S\n- [ ] x", True)
    assert H.read_copy(("meeting", "without")) == ("You: Hi. You: Hi. You: Hi.", False)
    assert H.read_copy("text") == ("text", False)
    assert H.read_copy_text(("meeting", "with")) == "## S\n- [ ] x"
    assert H.time_text(datetime(2026, 10, 3, 18, 40), datetime(2026, 10, 4, 12, 0)) == "18:40"
