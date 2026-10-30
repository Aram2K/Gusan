"""Richer meeting notes on screen (ui/pages/meeting_views.py), offscreen: a Discussion's topics are sub-headings, a section with
nothing in it is not shown, an action item belongs to a person or to nobody (never a "Them" chip, never "Owner: , Due: "), and
Copy / Markdown / Word say the same."""
import json

import pytest
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QLabel

from tests_qt.conftest import make_stub_app


@pytest.fixture(autouse=True)
def _inline_bg(monkeypatch):
    from ui.pages import meeting_views
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)


@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


@pytest.fixture
def shell(qapp, monkeypatch):
    from ui import shell as shell_mod
    from ui.pages import meetings as page_mod
    monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    made = []

    def build(tier="pro"):
        app = make_stub_app(tier)
        win = shell_mod.MainWindow(app)
        made.append(win)
        return win, app

    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


OLD_STYLE = """## Summary
Aram led the call.

## Key decisions
- None.

## Action items
- [ ] Send the file (Owner: Them, Due: )
- [ ] Book the room (Owner: You, Due: Friday)
- [ ] Call the bank (Owner: , Due: )
- [ ] Review it (Owner: Speaker 2, Due: within a week)
- [ ] Ask Claire (Owner: Claire Due: Monday)

## Open questions
_None._

## Discussion
### Release 2.4
- The sync engine is **three times** faster.

### An empty topic

### Pricing
- 290 euros a year.
"""


def _meeting(root, notes=OLD_STYLE, name="20261030_153400", title="Live session 15:34"):
    d = root / name
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({
        "title": title, "attendees": "Aram", "duration_sec": 3300, "started": "2026-10-30 15:34:00", "language": "fr",
        "seen": True, "source": "live_assist", "notes_engine_label": "Gusan Cloud · Mistral Small"}), encoding="utf-8")
    (d / "notes.md").write_text(notes, encoding="utf-8")
    (d / "transcript.txt").write_text("You: Bonjour.\nThem: Salut.", encoding="utf-8")
    return d


def _open(win, **opts):
    page = win.open("meetings", **opts)
    QApplication.processEvents()
    return page


def _labels(widget):
    return [w for w in widget.findChildren(QLabel)]


def _shown(widget):
    return " | ".join(w.text() for w in _labels(widget))


def _doc(me="Aram"):
    from ui.pages import meeting_views as v
    doc = v.NotesDoc()
    doc.me = me
    return doc


def test_topics_are_sub_headings_and_an_empty_section_is_not_shown(qapp):
    doc = _doc()
    try:
        doc.render(OLD_STYLE)
        texts = [w.text() for w in _labels(doc)]
        for gone in ("Key decisions", "Open questions", "An empty topic", "None."):
            assert not any(gone in t for t in texts), gone
        for kept in ("Summary", "Action items", "Discussion", "Release 2.4", "Pricing"):
            assert kept in texts, kept
        section = next(w for w in _labels(doc) if w.text() == "Discussion")
        topic = next(w for w in _labels(doc) if w.text() == "Release 2.4")
        assert topic.font().pointSizeF() < section.font().pointSizeF()      # a topic is a smaller heading
        assert topic.font().weight() >= 600 or topic.font().bold()
        assert topic.accessibleName() == "Release 2.4"
        assert section.accessibleName() == "Discussion"
    finally:
        doc.deleteLater()
        QApplication.processEvents()


def test_nothing_in_the_view_shows_raw_marks_or_empty_owner_text(qapp):
    doc = _doc()
    try:
        doc.render(OLD_STYLE)
        shown = _shown(doc)
        for raw in ("##", "###", "**", "[ ]", "Owner:", "Due:", "Them", "Speaker 2", "None.", "_"):
            assert raw not in shown, raw
        assert "<b>three times</b>" in shown                                # real bold, not stars
    finally:
        doc.deleteLater()
        QApplication.processEvents()


def test_an_action_item_is_a_person_or_nobody(qapp):
    from ui.pages import meeting_views as v
    doc = _doc("Aram")
    try:
        doc.render(OLD_STYLE)
        rows = [(t.task, t.owner, t.owner_shown(), t.due) for t in doc.tasks]
        assert rows == [
            ("Send the file", "", "", ""),                                  # Them: nobody
            ("Book the room", "You", "You", "by Friday"),
            ("Call the bank", "", "", ""),                                  # an empty owner: nobody
            ("Review it", "", "", "within a week"),                        # Speaker 2: nobody; no "by within"
            ("Ask Claire", "Claire", "Claire", "by Monday"),
        ]
        faces = [len(t.findChildren(v.Faces)) for t in doc.tasks]
        assert faces == [0, 1, 0, 0, 1]
        # no bold name in front of a task that has no person
        assert "<b" not in doc.tasks[0].text_label.text() and "<b" not in doc.tasks[2].text_label.text()
        assert "<b" in doc.tasks[4].text_label.text()
        assert [t.due_label.isVisibleTo(doc) or t.due == "" for t in doc.tasks] == [True] * 5
    finally:
        doc.deleteLater()
        QApplication.processEvents()


def test_the_person_who_recorded_is_drawn_as_you_whatever_the_notes_say(qapp):
    from ui.pages import meeting_views as v
    doc = _doc("Aram")
    try:
        doc.render("## Action items\n- [ ] One (Owner: You)\n- [ ] Two (Owner: Aram)\n- [ ] Three (Owner: Marc)")
        shown = [t.owner_shown() for t in doc.tasks]
        assert shown == ["You", "You", "Marc"]
        for task in doc.tasks[:2]:
            face = task.findChild(v.Faces)
            assert face._names == ["Aram"] and face._me == "aram"          # the blue chip, as in the sidebar
        doc.me = ""
        doc.render("## Action items\n- [ ] One (Owner: You)")
        assert doc.tasks[0].owner_shown() == "You"
        assert doc.tasks[0].findChild(v.Faces)._names == ["You"]
    finally:
        doc.deleteLater()
        QApplication.processEvents()


def test_a_heading_a_topic_or_a_due_from_the_ai_is_text_never_markup(qapp):
    from PySide6.QtCore import Qt
    doc = _doc()
    try:
        doc.render("## <b>Loud</b> heading\n### <i>Odd</i> <img src=x> topic\n- [ ] Do it (Due: <u>soon</u>)\n- point")
        by_text = {w.text(): w for w in _labels(doc)}
        for text in ("<b>Loud</b> heading", "<i>Odd</i> <img src=x> topic"):
            assert text in by_text, list(by_text)
            assert by_text[text].textFormat() == Qt.PlainText
        due = doc.tasks[0].due_label
        assert due.textFormat() == Qt.PlainText and "<u>soon</u>" in due.text()
    finally:
        doc.deleteLater()
        QApplication.processEvents()


def test_a_ticked_box_rewrites_its_own_line_though_empty_sections_are_hidden(shell, meetings_root):
    folder = _meeting(meetings_root)
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    nv = page.notes_view
    assert len(nv.doc.tasks) == 5
    nv.doc.tasks[3].box.click()                       # "Review it" (the 4th item)
    text = (folder / "notes.md").read_text(encoding="utf-8")
    lines = text.split("\n")
    assert [ln for ln in lines if ln.startswith("- [x]")] == ["- [x] Review it (Owner: Speaker 2, Due: within a week)"]
    assert text.count("[ ]") == 4
    assert "## Key decisions" in text                 # the file keeps what it had: only the view hides empty sections


def test_copy_says_what_the_view_says(shell, meetings_root):
    folder = _meeting(meetings_root)
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    page.notes_view.copy_notes()
    text = QGuiApplication.clipboard().text()
    html = QGuiApplication.clipboard().mimeData().html()
    for gone in ("Them", "Speaker 2", "Owner", "Key decisions", "Open questions", "None", "##", "**", "An empty topic"):
        assert gone not in text, gone
    assert "☐ Send the file\n" in text and "☐ Book the room (You · by Friday)" in text
    assert "☐ Review it (within a week)" in text
    assert "Release 2.4" in text and "three times faster" in text
    assert "<h3>Discussion</h3>" in html and "<h4>Release 2.4</h4>" in html
    assert "Them" not in html and "Key decisions" not in html


def test_the_markdown_export_is_clean(shell, meetings_root, tmp_path, monkeypatch):
    from ui.pages import meeting_views as v
    folder = _meeting(meetings_root)
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    out = tmp_path / "notes.md"
    monkeypatch.setattr(v.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    page.notes_view.export("markdown")
    import time
    end = time.monotonic() + 5
    while not out.exists() and time.monotonic() < end:
        QApplication.processEvents()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Live session 15:34")
    for gone in ("Owner: Them", "Owner: , Due", "Speaker 2", "None.", "_None._", "Key decisions", "An empty topic"):
        assert gone not in text, gone
    assert "- [ ] Send the file\n" in text
    assert "- [ ] Book the room (Owner: You, Due: Friday)" in text         # "You" stays: no profile name is written here
    assert "### Release 2.4" in text and "## Discussion" in text
    assert (folder / "notes.md").read_text(encoding="utf-8") == OLD_STYLE   # the saved notes are left as they were


def test_the_word_export_has_topics_as_sub_headings(shell, meetings_root, tmp_path, monkeypatch):
    import zipfile
    from ui.pages import meeting_views as v
    folder = _meeting(meetings_root)
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    out = tmp_path / "notes.docx"
    monkeypatch.setattr(v.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    page.notes_view.export("word")
    import time
    end = time.monotonic() + 5
    while not out.exists() and time.monotonic() < end:
        QApplication.processEvents()
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    notes_xml = xml.split(">Transcript<")[0]                 # the transcript below the notes says "Them:"
    for raw in ("##", "**", "Owner:", "Due:", "Them", "Key decisions", "An empty topic"):
        assert raw not in notes_xml, raw
    assert '<w:sz w:val="28"/><w:szCs w:val="28"/><w:b/></w:rPr><w:t xml:space="preserve">Discussion<' in xml
    assert '<w:sz w:val="22"/><w:szCs w:val="22"/><w:b/></w:rPr><w:t xml:space="preserve">Release 2.4<' in xml


def test_notes_with_only_the_old_four_sections_look_as_before(shell, meetings_root):
    old = "## Summary\nOne.\n\n## Key decisions\n- Ship.\n\n## Action items\n- [ ] Do it (Owner: David, Due: Wed)\n\n## Open questions\n- Why?\n"
    folder = _meeting(meetings_root, old)
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    doc = page.notes_view.doc
    texts = [w.text() for w in _labels(doc)]
    for head in ("Summary", "Key decisions", "Action items", "Open questions"):
        assert head in texts
    assert [(t.task, t.owner, t.due) for t in doc.tasks] == [("Do it", "David", "by Wed")]


def test_notes_that_are_only_empty_sections_are_shown_like_no_notes(shell, meetings_root):
    notes = "## Key decisions\n- None.\n\n## Open questions\n_None._\n"
    folder = _meeting(meetings_root, notes)
    win, _app = shell("pro")
    page = _open(win, folder=str(folder))
    nv = page.notes_view
    assert nv.doc.tasks == []
    assert nv.title_label.text() == "Live session 15:34"
    assert nv.banner.accessibleName() == "No notes yet"          # a calm line with Write notes, not a blank card
    assert "Bonjour." in _shown(nv.doc)                          # the transcript is right there
    assert not nv.copy_btn.isEnabled()
    assert (folder / "notes.md").read_text(encoding="utf-8") == notes     # nothing was changed on disk


def _busy_banner_text(shell, meetings_root, seconds):
    from tests_qt.test_page_meetings import FakeSession
    folder = _meeting(meetings_root)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    meta["duration_sec"] = seconds
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    session = FakeSession("processing")
    session.meeting_dir = folder
    session._rewrite_only = True
    session.identifying = False
    session.busy_label = "Writing notes…"
    win, _app = shell("pro")
    _app.meeting_session = session
    page = _open(win, folder=str(folder))
    page.notes_view.render()
    return page.notes_view.banner


def test_a_long_meeting_says_the_notes_can_take_a_minute(shell, meetings_root):
    banner = _busy_banner_text(shell, meetings_root, 55 * 60)
    assert banner.accessibleName() == "Writing notes…"
    text = _shown(banner)
    assert "The notes show up here when they're ready." in text
    assert "A long meeting can take a minute." in text


def test_a_short_meeting_says_no_such_thing(shell, meetings_root):
    banner = _busy_banner_text(shell, meetings_root, 3 * 60)
    assert "long meeting" not in _shown(banner)
