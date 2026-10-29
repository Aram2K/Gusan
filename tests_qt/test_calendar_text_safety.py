"""A calendar invite is written by whoever sent it, and anyone can send one: its title
and the guests' display names must be shown as text, never as markup.

A QLabel (and a tooltip) reads "<img src=...>" in its text as rich text and loads that
image - from the disk, or from a network share (a Windows UNC path is a connection to the
sender's host, which can leak the PC's sign-in). The reminder toast opens on its own one
minute before a meeting, so an invite is enough. These tests feed markup through every
place the title and the names are shown, with a real 150 px picture next to the working
directory so a label that does read it grows to 150 px."""
import html
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QLabel

import calendar_sync

TITLE = "<img src=p.png> Review"
GUEST = "J. <img src=p.png>"
BOLD = "<b>x</b>"
NOW = datetime(2026, 10, 7, 14, 29, tzinfo=timezone.utc)


@pytest.fixture
def picture(tmp_path, monkeypatch):
    """p.png (150 x 150, red) in the working directory, where Qt looks for a relative src."""
    img = QImage(150, 150, QImage.Format_RGB32)
    img.fill(QColor("red"))
    assert img.save(str(tmp_path / "p.png"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _plain_height(label, text):
    """The height of ``text`` as one plain line in ``label``'s font."""
    ref = QLabel()
    ref.setTextFormat(Qt.PlainText)
    ref.setFont(label.font())
    ref.setText(text)
    h = QLabel.sizeHint(ref).height()
    ref.deleteLater()
    return h


def _event(summary, names):
    """A Google event as parse_event reads it, starting in a minute."""
    start = NOW + timedelta(minutes=1)
    return {"id": "ev1", "status": "confirmed", "summary": summary,
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
            "organizer": {"self": True},
            "attendees": [{"email": "aram@example.com", "self": True}]
                         + [{"email": "g%d@example.com" % i, "displayName": n} for i, n in enumerate(names)]}


def _tip_is_text(tip, markup):
    """A tooltip holding ``markup`` shows it as text: escaped, in a paragraph of its own."""
    assert tip.startswith("<p"), tip
    assert html.escape(markup, quote=False) in tip, tip
    assert "<img" not in tip and "<b>" not in tip, tip


def test_the_premise_a_plain_label_with_markup_grows_to_the_picture(qapp, picture):
    rich = QLabel(TITLE)
    assert QLabel.sizeHint(rich).height() >= 150           # it did load p.png: the tests below mean something
    rich.deleteLater()


# ── the reminder toast ───────────────────────────────────────────────────────
@pytest.fixture
def toast(qapp):
    from ui.floating import meeting_reminder
    made = []

    def build(m):
        t = meeting_reminder.MeetingReminder(None, clock=lambda: NOW)
        made.append(t)
        assert t.show_meeting(m) is True
        QApplication.processEvents()
        return t

    yield build
    for t in made:
        t.hide()
        t.deleteLater()
    QApplication.processEvents()


def test_the_toast_shows_a_hostile_title_and_names_as_text(toast, picture):
    m = calendar_sync.parse_event(_event(TITLE, [GUEST, BOLD, "Anna"]))
    assert m is not None and "<img" in m["title"]           # nothing removed it on the way in
    t = toast(m)
    for lb in (t.title_label, t.meta_label, t.when_label):
        assert lb.textFormat() == Qt.PlainText
    assert t.title_label.full_text() == m["title"]
    assert QLabel.sizeHint(t.title_label).height() == _plain_height(t.title_label, m["title"])
    assert QLabel.sizeHint(t.title_label).height() < 100
    assert QLabel.sizeHint(t.meta_label).height() < 100
    assert "J. <img src=p.png>" in t.meta_label.full_text()        # the name is there, as text
    # An elided line keeps its full text as a tooltip - and that is text too.
    t.title_label.resize(40, 20)
    t.title_label._fit()
    _tip_is_text(t.title_label.toolTip(), "<img src=p.png>")


def test_the_toast_shows_markup_in_a_guest_name_as_text(toast, picture):
    t = toast({"id": "ev1", "title": "Sync", "people": GUEST, "start": NOW + timedelta(minutes=1),
               "end": NOW + timedelta(minutes=31), "link": "", "call_type": ""})
    assert t.meta_label.textFormat() == Qt.PlainText
    assert QLabel.sizeHint(t.meta_label).height() < 100
    t.meta_label.resize(30, 20)
    t.meta_label._fit()
    _tip_is_text(t.meta_label.toolTip(), "<img src=p.png>")


def test_the_toast_join_tooltip_is_text(toast, picture):
    t = toast({"id": "ev1", "title": "Sync", "people": "Anna", "start": NOW + timedelta(minutes=1),
               "end": NOW + timedelta(minutes=31), "link": "https://<b>x</b>.example/j/1", "call_type": "Zoom"})
    tip = t.join_button.toolTip()
    assert "<b>" not in tip and (not tip or tip.startswith("<p"))


# ── the Live Assistant card ──────────────────────────────────────────────────
class _App:
    def __init__(self):
        self.cfg = {}
        self.sig = None

    def schedule_config_save(self):
        pass

    def save_config(self):
        pass

    def track(self, event, props=None):
        pass


@pytest.fixture
def card(qapp):
    from ui import glass, live_assist
    made = []
    with mock.patch.object(glass, "capture_exclusion_supported", return_value=True), \
            mock.patch.object(glass, "is_remote_session", return_value=False), \
            mock.patch.object(glass, "exclude_from_capture", return_value=True), \
            mock.patch.object(glass, "is_excluded_from_capture", return_value=True), \
            mock.patch.object(glass, "macos_26_or_later", return_value=False):
        def build():
            c = live_assist.LiveAssistOverlay(main_app=_App())
            made.append(c)
            return c
        yield build
    for c in made:
        c.hide()
        c.deleteLater()
    QApplication.processEvents()


@pytest.mark.parametrize("title", [TITLE, BOLD])
def test_the_live_card_header_and_invite_chip_are_text(card, picture, title):
    c = card()
    c.set_meeting_active(True, title, "Aram, " + GUEST)
    c.set_invite("", [GUEST, BOLD, "Anna"])
    for lb in (c.lbl_title, c.inv_chip.text):
        assert lb.textFormat() == Qt.PlainText
    assert c.lbl_title.text() == title
    assert QLabel.sizeHint(c.lbl_title).height() < 100
    assert QLabel.sizeHint(c.inv_chip.text).height() < 100
    # the header's tooltip: the title as text
    _tip_is_text(c.lbl_title.toolTip(), title)
    # the chip: the guests as text (and the agenda, when there is one)
    c.set_invite("Review <img src=p.png>\n<b>x</b>", [GUEST, BOLD])
    tip = c.inv_chip.toolTip()
    assert "<img" not in tip and "<b>x</b>" not in tip
    assert "&lt;img src=p.png&gt;" in tip


def test_the_live_card_label_class_is_plain_from_its_first_text(qapp, picture):
    from ui import live_assist
    lb = live_assist._ElidedLabel(TITLE)                  # constructed with the text, as the chip is
    assert lb.textFormat() == Qt.PlainText and lb.text() == TITLE
    assert QLabel.sizeHint(lb).height() < 100
    lb.setText(TITLE)
    assert lb.textFormat() == Qt.PlainText
    lb.deleteLater()


def test_the_live_assistant_pages_session_row_title_is_text(qapp, picture):
    from ui.pages import live_assistant
    lb = live_assistant.ElidedLabel(TITLE)
    assert lb.textFormat() == Qt.PlainText
    assert QLabel.sizeHint(lb).height() < 100
    lb.resize(30, 20)
    lb.set_full_text(TITLE)
    _tip_is_text(lb.toolTip(), "<img src=p.png>")
    lb.deleteLater()


# ── the pages ────────────────────────────────────────────────────────────────
def test_plain_tip_escapes_and_wraps(qapp):
    from ui import theme
    assert theme.plain_tip("") == "" and theme.plain_tip(None) == ""
    tip = theme.plain_tip('<img src="//host/share/x.png"> & <b>x</b>')
    assert tip == "<p style='white-space:normal'>&lt;img src=\"//host/share/x.png\"&gt; &amp; &lt;b&gt;x&lt;/b&gt;</p>"


def test_the_page_header_shows_a_live_meetings_title_and_people_as_text(qapp, picture):
    from ui.components import PageHeader
    h = PageHeader("Meeting", "")
    h.set_title(TITLE)
    h.set_sub("Anna, " + GUEST)
    for lb in (h.title_label, h.sub_label):
        assert lb.textFormat() == Qt.PlainText
    assert h.title_label.text() == TITLE
    assert QLabel.sizeHint(h.sub_label).height() < 100
    h.deleteLater()


# ── Home: "Coming up" ────────────────────────────────────────────────────────
def test_home_coming_up_shows_a_hostile_title_and_names_as_text(qapp, picture, monkeypatch):
    """Home's section sits on the first page people see, so an invite alone reaches it: the title and the
    guests are painted as text (Runs), tooltips are escaped, no label reads markup."""
    import numpy as np
    from tests_qt.conftest import make_stub_app
    from ui.pages import home, home_upcoming

    class Cal(QObject):
        changed = Signal(object)
        connected = Signal(str)
        disconnected = Signal(str)
        connecting = Signal(bool)
        status = Signal(str)
        error = Signal(str)

        def __init__(self, rows):
            super().__init__()
            self.available, self.is_connected, self.is_connecting, self.has_read = True, True, False, True
            self.rows = rows

        def meetings(self):
            return [dict(m, people=calendar_sync.people_label(m["others"])) for m in self.rows]

        def status_text(self):
            return ""

    monkeypatch.setattr(home_upcoming, "_utcnow", lambda: NOW)
    long_title = (TITLE + " ") * 4
    rows = [calendar_sync.parse_event(_event(long_title.strip(), [GUEST, BOLD, "Anna"]))]
    assert rows[0] is not None and "<img" in rows[0]["title"]                     # nothing removed it on the way in
    rows[0]["link"] = "https://<b>x</b>.example/j/1"
    app = make_stub_app("pro")
    app.show_main = lambda *a, **k: None
    app.calendar = Cal(rows)
    page = home.create(app)
    page.sync_io = True
    page.resize(900, 900)
    page.show()
    for _ in range(3):
        QApplication.processEvents()
    sec = page.upcoming
    row = sec.rows[0]
    assert row.title.text() == rows[0]["title"]                                    # whole, as text
    assert "<img" in row.meta.text()                                               # the guest's name, as text
    for lb in row.findChildren(QLabel):
        if lb.text():
            assert lb.textFormat() == Qt.PlainText
            assert QLabel.sizeHint(lb).height() < 100
    assert row.height() == 58
    _tip_is_text(row.toolTip(), GUEST)                                             # "everyone", escaped
    _tip_is_text(row.title.toolTip(), "<img src=p.png>")                           # an elided title's full text
    jt = row.join_button.toolTip()
    assert "<b>" not in jt and (not jt or jt.startswith("<p"))
    assert rows[0]["title"] in row.record_button.accessibleName() or \
        row.record_button.accessibleName().startswith("Record <img")
    # What is on screen is words: the 150 px red picture is not in the section's pixels.
    img = sec.grab().toImage().convertToFormat(QImage.Format_RGB32)
    arr = np.frombuffer(img.constBits(), dtype=np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)
    red = (arr[:, :img.width(), 2] > 240) & (arr[:, :img.width(), 1] < 20) & (arr[:, :img.width(), 0] < 20)
    assert int(red.sum()) == 0
    assert sec.height() < 400
    page.hide()
    page.deleteLater()
    QApplication.processEvents()
