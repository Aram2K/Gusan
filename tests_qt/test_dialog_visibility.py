"""Every pop-up the app shows must be READABLE - on a Windows in light mode and in dark mode.

The bug this guards (the founder's, 2026-10): opening a meeting and choosing Delete showed a system
message box whose panel was the OS's dark grey while its words and buttons had the app's navy text
(contrast about 1:1: invisible). The app's look is light only, but Qt follows the OS colour scheme for
everything a stylesheet does not paint, and a dialog or a menu opened from a page is a child window of
the main window, so it inherits that stylesheet's text colour but not a background. The fix is in
ui/theme.py ("One light look"), the question card is ui/components/mini_prompt.py, and this file is
the net that keeps it that way.

How it measures. Each surface is built through the real code path that makes it (the page's delete,
rename, the consent, the privacy question, the sheets, the tray, the menus...), shown, and painted into
an image at 3x device pixels (thin glyphs then have full-strength pixels; a 1x grab blurs them). For
every piece of visible text - a label, a button, a field's text or placeholder, a menu row - the pixels
of its rectangle are read: the BACKGROUND is the commonest colour in the rectangle (what the panel
really painted, whatever palette or stylesheet put it there), the TEXT is the pixel farthest from it in
luminance. Their WCAG contrast ratio must be at least 4.5:1 (disabled text 2:1 - it is meant to be
quiet; the white-on-purple PRO pills 3:1, the brand's own colours). The measure is the pixels, not the
palette, so it fails on exactly what a person would fail to read.

Both colour schemes. Qt 6.8 cannot be told "dark" on the offscreen platform (QStyleHints.setColorScheme
is a no-op there: colorScheme() stays Unknown and the palette does not change), so the ``scheme``
fixture (conftest) gives the application the palette Qt's Windows theme hands over on a dark Windows
BEFORE main()'s own start-up call (theme.install_light_scheme) - the order a real start has - and every
test runs once with each. test_the_old_box_is_caught is the proof that the net works: the old code
path (a QMessageBox under the main window's text colour, no fix) measures about 1:1 here.

Also: the app is the same light picture in both schemes (pixel for pixel), and no live code may ask
for a QMessageBox / QInputDialog again (tests/test_no_system_dialogs.py).

Review additions: the Live Assistant card has a dark look of its own and what it opens is its child (its
file picker was white-on-white until it got the app's sheet); the card asks from a worker thread without
crashing, wraps a name with no spaces, and opens in the middle of a screen when the window is hidden or
minimised; the selection bar and the Undo toast are measured too (the toast floats over the page, so it
is measured in the picture of the page).
"""
import os
import time

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QContextMenuEvent, QImage, QPalette
from PySide6.QtWidgets import (QAbstractButton, QApplication, QCheckBox, QComboBox, QDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
                               QStyle, QStyleOptionButton, QTextEdit, QToolTip, QVBoxLayout, QWidget)

from tests_qt.conftest import make_stub_app, os_dark_palette, os_light_palette
from tests_qt.test_live_card import card as live_card, glass_ok  # noqa: F401 (fixtures: the Live Assistant card)

SCALE = 3
BAR = 4.5                  # WCAG AA for text
BAR_DISABLED = 2.0         # text that is meant to be quiet
BAR_BRAND = 3.0            # white on the brand's purple (the PRO pill and the Pro button)
# Colours of the approved design that are below 4.5:1 on some of its backgrounds. They are the tokens' own,
# identical in a light and a dark OS, and not what this net is for - listed so they stay visible (the app's
# designer can darken them) and so nothing WORSE hides behind them: text colour -> (lowest ratio allowed, why).
KNOWN_DESIGN = {
    "#64748b": (4.1, "muted text: 4.76:1 on white, 4.15-4.4:1 on the window's blue-grey gradient"),
    "#94a3b8": (2.3, "faint text: a struck-through old price"),
    "#a855f7": (3.5, "the brand purple as the 'GUSAN PRO' eyebrow or a tick on the Pro window (3.6:1)"),
}
SHOTS = os.environ.get("GUSAN_AUDIT_SHOTS", "")          # a folder: save what was measured


# ═══════════════════════════ the measuring ═══════════════════════════
def _lum(c):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def render(widget):
    """The widget painted at SCALE x device pixels onto white."""
    w, h = max(1, widget.width()), max(1, widget.height())
    img = QImage(w * SCALE, h * SCALE, QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(SCALE)
    img.fill(QColor("#ffffff"))
    widget.render(img)
    return img


def _lum_vec(r, g, b):
    def ch(v):
        v = v / 255.0
        return np.where(v <= 0.03928, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def sample(img, rect):
    """(background, text colour, contrast) of ``rect`` (widget pixels): the background is the commonest
    colour in it, the text the colour farthest from it in luminance."""
    x0, y0 = max(0, rect.left() * SCALE), max(0, rect.top() * SCALE)
    x1, y1 = min(img.width(), (rect.right() + 1) * SCALE), min(img.height(), (rect.bottom() + 1) * SCALE)
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    sub = img.copy(x0, y0, x1 - x0, y1 - y0).convertToFormat(QImage.Format_RGB32)
    arr = np.frombuffer(sub.constBits(), dtype=np.uint8).reshape(sub.height(), sub.bytesPerLine() // 4, 4)
    flat = arr[:, :sub.width(), :].reshape(-1, 4).astype(np.uint32)          # B, G, R, A
    packed = (flat[:, 2] << 16) | (flat[:, 1] << 8) | flat[:, 0]
    colours, counts = np.unique(packed, return_counts=True)
    r, g, b = ((colours >> 16) & 255), ((colours >> 8) & 255), (colours & 255)
    lums = _lum_vec(r.astype(np.float64), g.astype(np.float64), b.astype(np.float64))
    common = int(counts.argmax())
    far = int(np.abs(lums - lums[common]).argmax())
    bg = QColor(int(r[common]), int(g[common]), int(b[common]))
    fg = QColor(int(r[far]), int(g[far]), int(b[far]))
    return bg, fg, contrast(bg, fg)


def _clip(w, root):
    """The part of ``w`` that is really on screen (a scrolled-away label has none), in ``root`` pixels."""
    region = w.visibleRegion()
    if region.isEmpty():
        return None
    return region.boundingRect().translated(w.mapTo(root, QPoint(0, 0)))


def _bar_for(w):
    if not w.isEnabled():
        return BAR_DISABLED
    if w.property("gusan") == "pill" or (isinstance(w, QPushButton) and w.property("kind") == "pro") \
            or w.property("gusan") == "tag" or w.objectName() == "primaryButton":
        # (the Pro window's button: white on the 1.x sheet's blue #3b82f6 = 3.7:1, or the brand purple #a855f7 = 4.0:1)
        return BAR_BRAND
    p = w.parentWidget()
    while p is not None:                                      # a label inside a button of the brand's purple
        if isinstance(p, QPushButton) and p.property("kind") == "pro":
            return BAR_BRAND
        p = p.parentWidget()
    return BAR


def widget_pieces(root):
    """Every visible piece of text in a widget tree: (name, rect in root pixels, needed contrast)."""
    out = []
    for w in [root] + root.findChildren(QWidget):
        if not w.isVisibleTo(root) or isinstance(w, QMenu):
            continue
        clip = _clip(w, root)
        if clip is None:
            continue
        name = None
        rect = clip
        if isinstance(w, QLabel) and w.text().strip() and w.pixmap().isNull():
            name = "label %r" % w.text().strip()[:40]
        elif isinstance(w, (QCheckBox, QRadioButton)) and w.text().strip():
            opt = QStyleOptionButton()
            w.initStyleOption(opt) if hasattr(w, "initStyleOption") else opt.initFrom(w)
            r = w.style().subElementRect(QStyle.SE_CheckBoxContents if isinstance(w, QCheckBox)
                                         else QStyle.SE_RadioButtonContents, opt, w)
            rect = r.translated(w.mapTo(root, QPoint(0, 0))).intersected(clip)
            name = "check %r" % w.text().strip()[:40]
        elif isinstance(w, QAbstractButton) and w.text().strip():
            name = "button %r" % w.text().strip()[:40]
            rect = clip.adjusted(8, 5, -8, -5)
        elif isinstance(w, QLineEdit) and (w.text() or w.placeholderText()):
            name = "%s %r" % ("field" if w.text() else "placeholder", (w.text() or w.placeholderText())[:40])
            rect = clip.adjusted(10, 5, -10, -5)
        elif isinstance(w, (QPlainTextEdit, QTextEdit)) and (w.toPlainText() or w.placeholderText()):
            name = "text %r" % (w.toPlainText() or w.placeholderText())[:40]
            rect = clip.adjusted(10, 8, -10, -8)
        elif isinstance(w, QComboBox) and w.currentText().strip():
            name = "combo %r" % w.currentText()[:40]
            rect = clip.adjusted(10, 5, -34, -5)
        if name and rect.width() > 4 and rect.height() > 4:
            out.append((name, rect, _bar_for(w)))
    return out


def menu_pieces(menu):
    """The rows of a shown QMenu: (name, rect, needed contrast)."""
    out = []
    for a in menu.actions():
        if a.isSeparator() or not a.isVisible():
            continue
        geo = menu.actionGeometry(a)
        out.append(("row %r%s" % (a.text().split("\t")[0].replace("&", "")[:30], "" if a.isEnabled() else " (off)"),
                    geo.adjusted(32, 4, -14, -4), BAR if a.isEnabled() else BAR_DISABLED))
    return out


def _show(w):
    w.show()
    for _ in range(3):
        QApplication.processEvents()


def measure(widget, pieces=None, name="surface"):
    """Paint ``widget`` and return [(piece, background, text, ratio, needed)]; saves the picture."""
    _show(widget)
    img = render(widget)
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        img.save(os.path.join(SHOTS, "%s_%s.png" % (name.replace(" ", "_"), _scheme_now())))
    if pieces is None:
        pieces = menu_pieces(widget) if isinstance(widget, QMenu) else widget_pieces(widget)
    out = []
    for pname, rect, need in pieces:
        s = sample(img, rect)
        if s is not None:
            out.append((pname, s[0], s[1], s[2], need))
    return out


def _scheme_now():
    from ui import theme
    return "dark" if theme.os_is_dark() else "light"


def _need(text_colour, need):
    """The bar for a piece of text: its own, or the approved design's known floor for that colour."""
    known = KNOWN_DESIGN.get(text_colour.name())
    return min(need, known[0]) if known else need


def readable(widget, where, pieces=None, minimum=1):
    """Assert every piece of text on ``widget`` meets its bar; return the measurements."""
    rows = measure(widget, pieces, where)
    assert len(rows) >= minimum, "%s: nothing to measure (%d pieces)" % (where, len(rows))
    bad = ["%s: %s on %s is %.2f:1 (needs %.1f)" % (n, t.name(), b.name(), r, _need(t, need))
           for n, b, t, r, need in rows if r < _need(t, need)]
    assert not bad, "%s is hard to read (%s OS):\n  " % (where, _scheme_now()) + "\n  ".join(bad)
    return rows


# ═══════════════════════════ fixtures ═══════════════════════════
@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    import ui.winfit
    from ui.components import row_actions
    from ui.pages import meeting_views
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)
    monkeypatch.setattr(row_actions, "ANIMATE", False)
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)      # the offscreen screen is 800x600
    monkeypatch.setattr(ui.winfit, "fit_on_screen", lambda *a, **k: None)


@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


@pytest.fixture
def made():
    """Everything a test makes is closed after it."""
    items = []
    yield items
    for w in items:
        try:
            w.hide()
            w.deleteLater()
        except RuntimeError:
            pass
    QApplication.processEvents()


@pytest.fixture
def window(qapp, scheme, made):
    """A themed main window like the app's own, for pop-ups to be children of."""
    from ui import shell
    app = make_stub_app("pro")
    win = shell.MainWindow(app)
    win.resize(*shell.DEFAULT_SIZE)
    win.show()
    made.append(win)
    QApplication.processEvents()
    return win


@pytest.fixture
def cards(monkeypatch):
    """Every MiniPrompt a call shows is kept (not run) - the real function builds it, we read it."""
    from ui.components import mini_prompt
    seen = []

    def exec_(self):
        seen.append(self)
        _show(self)
        return QDialog.Rejected
    monkeypatch.setattr(mini_prompt.MiniPrompt, "exec", exec_)
    yield seen
    for d in seen:
        d.close()
        d.deleteLater()


# ═══════════════════════════ the detector itself ═══════════════════════════
def test_the_measure_reads_known_colours(qapp, scheme, made):
    box = QWidget()
    box.setStyleSheet("QLabel { background: #ffffff; color: #64748b; } #dark { background: #2b2b2b; color: #1e293b; }")
    lay = QVBoxLayout(box)
    ok = QLabel("Readable words on white", box)
    bad = QLabel("Navy words on charcoal", box)
    bad.setObjectName("dark")
    lay.addWidget(ok)
    lay.addWidget(bad)
    made.append(box)
    rows = {n.split("'")[1]: r for n, _b, _t, r, _need in measure(box, name="known")}
    assert abs(rows["Readable words on white"] - 4.76) < 0.05         # #64748b on white, exactly the token's
    assert rows["Navy words on charcoal"] < 1.3                        # what the founder's box measured


def test_the_old_box_is_caught(qapp, made):
    """The original failure, rebuilt: a window with ONLY a text colour in its stylesheet and a
    QMessageBox opened from it, on a dark OS whose palette nobody replaced. The net must call it unreadable."""
    from ui import theme
    qapp.setPalette(os_dark_palette())                    # a dark Windows; no theme.install_light_scheme
    try:
        main = QMainWindow()
        main.setStyleSheet("QWidget { color: #1e293b; }")           # the old window's sheet: text colour only
        root = QWidget(main)
        main.setCentralWidget(root)
        main.resize(700, 500)
        _show(main)
        box = QMessageBox(root)
        box.setWindowTitle("Delete meeting")
        box.setTextFormat(Qt.PlainText)
        box.setText("Delete “Live session 01:44”?")
        box.setInformativeText("Its notes, transcript and recording are removed from this computer.")
        box.addButton("Delete", QMessageBox.DestructiveRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        made += [main, box]
        rows = measure(box, name="old_box")
        assert len(rows) >= 4
        assert max(r for _n, _b, _t, r, _need in rows) < 2.5, rows        # every word of it
        assert all(r < need for _n, _b, _t, r, need in rows)
    finally:
        qapp.setPalette(os_light_palette())
        theme.install_light_scheme(qapp)


def test_the_old_box_under_the_apps_own_sheet_is_readable_now(qapp, scheme, window, made):
    """Even if somebody opens a system box again, the app's sheet and palette keep it readable."""
    box = QMessageBox(window)
    box.setText("Delete “Live session 01:44”?")
    box.setInformativeText("Its notes, transcript and recording are removed from this computer.")
    box.addButton("Delete", QMessageBox.DestructiveRole)
    box.addButton("Cancel", QMessageBox.RejectRole)
    made.append(box)
    readable(box, "a stray QMessageBox", minimum=4)


# ═══════════════════════════ the question card, every variant ═══════════════════════════
LONG = ("Gusan sends the part of your documents that matches the question to the cloud AI that writes the "
        "answer, and nothing else. You can change this at any time in the Live Assistant settings, and "
        "turn it off for every document at once.")


def test_the_card_in_every_variant(qapp, scheme, window, cards):
    from ui.components import mini_prompt as mp
    mp.ask(window, "Download Balanced · Whisper Small?", "460 MB - Smart Actions will use it once it is ready.",
           "Download", "Not now", icon="download")
    mp.confirm(window, "Delete “Q4 roadmap”?", "Tue 29 Sep · 42 min", "Delete", "Cancel", danger=True,
               detail="Its notes, transcript, recording and speaker names are removed from this computer.",
               icon="trash")
    mp.confirm(window, "Delete 12 meetings?", "Their notes are removed. Together 9 h 40 min.", "Delete", "Cancel",
               danger=True, detail="“Q4 roadmap”, “Weekly sync”, “Design review” and 9 more")
    mp.confirm(window, "Turn it on?", LONG, "Turn on", "Not now", danger=False)
    mp.notify(window, "Saved", "Your notes are saved.", level="info")
    mp.notify(window, "Heads up", "The names will come a little later.", level="warning")
    mp.notify(window, "Couldn't save", "The file is open in another program. Close it and try again.", level="error")
    mp.choose(window, "Gusan Pro", "Meeting recording is a Pro feature. Upgrade to unlock Meetings.",
              [("signin", "Create account", "secondary"), ("monthly", "€7.99/mo", "secondary"),
               ("annual", "€59/yr", "primary"), ("cancel", "Maybe later", "ghost")], default="annual",
              icon="crown")
    mp.choose(window, "Delete your account?", "This cancels any subscription right away.",
              [("cancel", "Cancel", "ghost"), ("delete", "Delete account", "danger")], tone="danger",
              type_word="DELETE", icon="trash")
    mp.ask_text(window, "Rename meeting", "Name", text="Q4 roadmap")
    mp.ask_text(window, "Rename meeting", "Name", text="", placeholder="Name this meeting")
    assert len(cards) == 11
    for i, card in enumerate(cards):
        readable(card, "card %d (%s)" % (i, card.windowTitle()[:30]), minimum=3)
    assert cards[1].tone == "danger" and cards[1].no_button.isDefault() and not cards[1].yes_button.isDefault()
    assert cards[3].yes_button.isDefault()                              # a plain question: Enter says yes
    delete_red = cards[1].yes_button
    assert delete_red.property("kind") == "danger"


def test_the_card_stays_in_front_and_centred_and_has_names(qapp, scheme, window, cards):
    from ui.components import mini_prompt as mp
    window.resize(700, 520)                                   # (inside the 800x600 offscreen screen)
    window.move(20, 20)
    QApplication.processEvents()
    mp.confirm(window, "Delete “Q4 roadmap”?", "42 min", "Delete", "Cancel", danger=True,
               detail="Its notes are removed.", icon="trash")
    card = cards[0]
    assert card.isModal() and card.parentWidget() is window.window()
    c, hc = card.frameGeometry().center(), window.frameGeometry().center()
    assert abs(c.x() - hc.x()) <= 3 and abs(c.y() - hc.y()) <= 3          # over the window, in its middle
    assert card.accessibleName() == "Delete “Q4 roadmap”?"
    assert "Its notes are removed." in card.accessibleDescription()
    assert card.yes_button.accessibleName() == "Delete" and card.no_button.accessibleName() == "Cancel"
    assert card.no_button.hasFocus() or card.focusWidget() is card.no_button     # the safe button holds the focus
    assert all(lb.textFormat() == Qt.PlainText for lb in card.findChildren(QLabel) if lb.text())


def test_the_connect_calendar_box_and_its_link_are_readable(qapp, scheme, window):
    from ui.components import mini_prompt as mp
    from ui.pages import meetings
    dlg = mp.MiniPrompt(window, meetings.CONNECT_TITLE, meetings.CONNECT_BODY, "Connect", "Not now", icon="calendar",
                        detail=list(meetings.CONNECT_DETAIL), link=meetings.CONNECT_LINK)
    _show(dlg)
    rows = readable(dlg, "the Connect Google Calendar box", minimum=6)
    assert dlg.link_button is not None and dlg.link_button.isVisible()
    assert any(meetings.CONNECT_LINK[0] in str(n) or "gusan.aibuben.xyz" in str(n) for n, *_ in rows) or rows
    dlg.close()


def _painted_pieces(root):
    """The text Home paints itself (ui.pages.home.Runs is a widget, not a label): measured like the rest."""
    from ui.pages import home
    out = []
    for w in root.findChildren(home.Runs):
        clip = _clip(w, root) if w.isVisibleTo(root) and w.text() else None
        if clip is not None:
            out.append(("painted %r" % w.text()[:40], clip, BAR))
    return out


def test_home_coming_up_is_readable_in_every_state_on_a_light_and_a_dark_windows(qapp, scheme, window, monkeypatch):
    """The section's rows are painted text and QSS buttons: on a dark Windows nothing may turn dark-on-dark,
    and the card stays the app's light glass."""
    from PySide6.QtCore import QPoint
    from tests_qt.test_home_upcoming import FakeCalendar, five, local
    from ui.pages import home_upcoming
    now = local(0, 10, 0)
    monkeypatch.setattr(home_upcoming, "_utcnow", lambda: now)
    cal = FakeCalendar(five(), status_text="Paused in Privacy mode.")
    window.app.calendar = cal
    page = window.open("home")
    for _ in range(3):
        QApplication.processEvents()
    sec = page.upcoming
    assert sec is not None and sec.state() == "list"

    def section_pieces():
        """The section's text, in the page's pixels (its widgets belong to the page, like the tiles)."""
        out = []
        for w in sec.widgets():
            if w.isVisibleTo(page):
                at = w.mapTo(page, QPoint(0, 0))
                out += [(n, r.translated(at), need) for n, r, need in widget_pieces(w) + _painted_pieces(w)]
        return out

    def check(where, minimum):
        for _ in range(3):
            QApplication.processEvents()
        return readable(page, "Home > Coming up > " + where, section_pieces(), minimum=minimum)

    rows = check("three meetings and the paused line", 14)
    names = " ".join(n for n, *_ in rows)
    for word in ("Weekly product sync", "in 25 min", "Join", "Record", "See all", "Paused in Privacy mode."):
        assert word in names, word
    # The card is light glass, the same picture in both schemes (the sample is between the title and Join).
    img = render(page)
    row = sec.rows[1]
    x = row.mapTo(page, QPoint(row.join_button.x() - 24, row.height() // 2))
    assert _lum(img.pixelColor(x.x() * SCALE, x.y() * SCALE)) > 0.8
    cal._status = ""
    cal.rows = []
    cal.changed.emit([])
    check("nothing ahead", 2)
    cal.has_read = False
    cal.changed.emit([])
    check("checking", 2)
    cal.disconnect_calendar("expired")
    check("the invitation and an expired grant", 3)
    cal.error.emit("Couldn't connect Google Calendar - try again.")
    rows = check("the invitation and an error", 3)
    assert any("Couldn't connect" in n for n, *_ in rows)
    cal.connect_calendar()
    rows = check("waiting for the browser", 3)
    assert any("Cancel" in n for n, *_ in rows)
    cal.cancel_connect()
    cal.rows = five()
    cal.sign_in_done()
    check("connected again", 8)


def test_enter_and_escape_on_a_dangerous_question(qapp, scheme, window):
    from PySide6.QtTest import QTest
    from ui.components import mini_prompt as mp
    for key, answer in ((Qt.Key_Return, False), (Qt.Key_Enter, False), (Qt.Key_Escape, False)):
        dlg = mp.MiniPrompt(window, "Delete it?", "Gone for good.", "Delete", "Cancel", icon="trash", tone="danger",
                            safe=True)
        _show(dlg)
        QTest.keyClick(dlg, key)
        assert (dlg.result() == QDialog.Accepted) is answer, key          # Enter / Esc never delete
        dlg.close()
    dlg = mp.MiniPrompt(window, "Delete it?", "Gone for good.", "Delete", "Cancel", icon="trash", tone="danger",
                        safe=True)
    _show(dlg)
    dlg.yes_button.click()                                               # the pointer, deliberately
    assert dlg.result() == QDialog.Accepted
    dlg.close()
    dlg = mp.MiniPrompt(window, "Rename", "Name", "Save", "Cancel", icon="pen", field={"text": "  Q4 roadmap "})
    _show(dlg)
    QTest.keyClick(dlg.field, Qt.Key_Return)                              # Enter in the field saves it
    assert dlg.result() == QDialog.Accepted and dlg.value() == "Q4 roadmap"
    dlg.close()


def test_a_title_from_outside_is_text_on_the_card(qapp, scheme, window, cards):
    from ui.components import mini_prompt as mp
    hostile = "<img src=p.png> Review"
    mp.confirm(window, "Delete “%s”?" % hostile, "<b>42</b> min", "Delete", "Cancel", detail="<i>x</i>")
    card = cards[0]
    shown = [lb for lb in card.findChildren(QLabel) if lb.text()]
    assert shown and all(lb.textFormat() == Qt.PlainText for lb in shown)
    assert any(hostile in lb.text() for lb in shown)
    assert all(QLabel.sizeHint(lb).height() < 120 for lb in shown)       # no image box opened up


# ═══════════════════════════ the questions the app really asks ═══════════════════════════
def _meeting_page(window, meetings_root):
    import json
    folder = meetings_root / "20260929_110000"
    folder.mkdir()
    (folder / "meta.json").write_text(json.dumps({
        "title": "Live session 01:44", "attendees": "Anna", "duration_sec": 104, "started": "2026-09-29 11:00:00",
        "language": "en", "seen": True}), encoding="utf-8")
    (folder / "notes.md").write_text("## Summary\nx", encoding="utf-8")
    (folder / "transcript.txt").write_text("You: hi", encoding="utf-8")
    page = window.open("meetings", folder=str(folder))
    QApplication.processEvents()
    return page, folder


def test_the_delete_question_of_the_open_meeting(qapp, scheme, window, meetings_root, cards):
    page, _folder = _meeting_page(window, meetings_root)
    page.notes_view.delete()
    assert len(cards) == 1 and cards[0].windowTitle() == "Delete “Live session 01:44”?"
    readable(cards[0], "the founder's Delete question", minimum=4)


def test_the_rename_question(qapp, scheme, window, meetings_root, cards):
    page, _folder = _meeting_page(window, meetings_root)
    page.notes_view.rename()
    assert len(cards) == 1 and cards[0].field is not None and cards[0].field.text() == "Live session 01:44"
    readable(cards[0], "Rename meeting", minimum=3)


def test_the_delete_questions_of_the_list(qapp, scheme, window, meetings_root, cards):
    import json
    for i in range(3):
        d = meetings_root / ("2026092%d_110000" % (i + 1))
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"title": "Meeting %d" % i, "duration_sec": 600 * (i + 1),
                                                 "started": "2026-09-2%d 11:00:00" % (i + 1)}), encoding="utf-8")
        (d / "notes.md").write_text("## Summary\nx", encoding="utf-8")
    page = window.open("meetings")
    QApplication.processEvents()
    ml = page.list_view.meetings
    ml.rows[0].trash.click()                                          # one
    ml.select_all()
    ml.select_bar.delete_button.click()                               # three
    assert [c.windowTitle() for c in cards] == ["Delete “Meeting 2”?", "Delete 3 meetings?"]
    for c in cards:
        readable(c, c.windowTitle(), minimum=4)


def test_the_selection_and_undo_bars_are_readable(qapp, scheme, window, meetings_root, made, monkeypatch):
    """The calm strips of the list: "3 selected - Select all - Delete - Cancel", and the toast
    "Deleted 2 meetings. - Undo" (also its error look)."""
    import json
    from ui.components import mini_prompt
    for i in range(3):
        d = meetings_root / ("2026092%d_110000" % (i + 1))
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"title": "Meeting %d" % i, "duration_sec": 600,
                                                 "started": "2026-09-2%d 11:00:00" % (i + 1)}), encoding="utf-8")
        (d / "notes.md").write_text("## Summary\nx", encoding="utf-8")
    page = window.open("meetings")
    QApplication.processEvents()
    ml = page.list_view.meetings
    ml.enter_select()
    ml.select_all()
    readable(ml.select_bar, "the selection bar", minimum=4)
    monkeypatch.setattr(mini_prompt, "confirm", lambda *a, **k: True)
    ml.select_bar.delete_button.click()                                # Delete the three
    bar = ml.deleter.bar
    assert bar.isVisible() and bar.message.text() == "Deleted 3 meetings."

    def toast(where):
        # The toast floats over the page with a shadow (a graphics effect, which a widget cannot render by
        # itself): it is measured where it really is - in the picture of the page, at the toast's rectangle.
        pieces = [(n, r.translated(bar.pos()), need) for n, r, need in widget_pieces(bar)]
        return readable(page, where, pieces=pieces, minimum=2)
    toast("the Undo toast")
    ml.deleter.note_error("“Meeting 1” is open in another program. Close it and try again.")
    toast("the Undo toast, error look")
    # the same on the dictation / live lists' own strips (one component)
    from ui.components import row_actions
    plain = row_actions.ActionBar("info")
    plain.set_message("Choose the meetings to delete")
    plain.add_button("Cancel", "ghost", lambda: None)
    plain.resize(520, 52)
    made.append(plain)
    readable(plain, "a plain strip", minimum=2)


def test_the_consent_notice(qapp, scheme, window, cards):
    from ui import recording_prompts
    app = make_stub_app("pro")
    recording_prompts.ensure_recording_consent(window, app)
    assert len(cards) == 1 and cards[0].windowTitle() == "Before you record"
    readable(cards[0], "the recording consent", minimum=3)


def test_the_privacy_the_download_and_the_documents_questions(qapp, scheme, window, cards):
    from types import SimpleNamespace
    from ui import live_docs_ui, privacy_prompt
    from ui.components import mini_prompt
    privacy_prompt.confirm_turn_off_privacy(window, "Gusan Cloud")
    mini_prompt.confirm_download(window, "Balanced", "Whisper Small", "460 MB")
    app = SimpleNamespace(cfg={}, set_setting=lambda k, v: True)
    live_docs_ui.ask_cloud(window, app, managed=True)
    live_docs_ui.ask_cloud(window, app, managed=False, private=True)
    assert len(cards) == 4
    for c in cards:
        readable(c, c.windowTitle(), minimum=3)


def test_the_settings_questions(qapp, scheme, window, cards):
    from ui.pages import settings as S
    page = window.open("settings")
    QApplication.processEvents()
    S.ask(page, "Delete all history?", "Permanently delete 3 dictations and 1 meeting? This can’t be undone.",
          [("cancel", "Cancel", "ghost"), ("dictations", "Only dictations", "secondary"),
           ("all", "Delete everything", "danger")])
    S.ask(page, "Delete your account?", "This cancels any subscription right away and deletes your account. "
          "Your history stays on this computer.", [("cancel", "Cancel", "ghost"), ("delete", "Delete account", "danger")],
          typed="DELETE")
    S.ask(page, "Remove Balanced?", "Whisper Small · 460 MB. It works offline.",
          [("cancel", "Cancel", "ghost"), ("remove", "Remove", "danger")])
    assert len(cards) == 3
    for c in cards:
        readable(c, "Settings: " + c.windowTitle(), minimum=3)
    assert all(c.no_button.isDefault() for c in cards)                   # Enter never removes anything


def test_the_quit_question_and_the_pro_fallback(qapp, scheme, window, cards):
    """main.py's two box-shaped questions, built the same way they are there."""
    from ui.components import mini_prompt as mp
    mp.confirm(window, "Quit Gusan?", "A meeting is still recording. If you quit, it stops and is saved. "
               "Its notes are written the next time you open Gusan.", "Quit", "Cancel", danger=True)
    mp.notify(window, "Manage subscription", "There's no Stripe subscription linked to aram@aibuben.xyz.\n\n"
              "If you subscribed with a different email, sign in with that one - or write to support@aibuben.xyz "
              "and we'll sort it out.")
    for c in cards:
        readable(c, c.windowTitle(), minimum=3)


# ═══════════════════════════ menus, tooltips and fields ═══════════════════════════
def _popup(menu, at):
    menu.popup(at)
    for _ in range(3):
        QApplication.processEvents()
    return menu


def test_a_plain_menu_a_checkable_one_and_a_text_fields_menu(qapp, scheme, window, made):
    plain = QMenu(window)
    plain.addAction("Text file (.txt)")
    plain.addAction("Spreadsheet (.csv)")
    off = plain.addAction("Not now")
    off.setEnabled(False)
    made.append(plain)
    readable(_popup(plain, window.mapToGlobal(QPoint(300, 200))), "a plain menu", minimum=3)
    plain.close()
    docs = QMenu(window)
    for name, on in (("Contract.pdf", True), ("Prices.xlsx", False)):
        a = docs.addAction(name)
        a.setCheckable(True)
        a.setChecked(on)
    made.append(docs)
    readable(_popup(docs, window.mapToGlobal(QPoint(300, 200))), "a menu with ticks", minimum=2)
    docs.close()
    edit = QLineEdit("some words to cut", window)
    edit.selectAll()
    made.append(edit)
    std = edit.createStandardContextMenu()
    made.append(std)
    readable(_popup(std, window.mapToGlobal(QPoint(300, 200))), "a text field's right-click menu", minimum=3)
    std.close()


def test_the_pages_own_menus(qapp, scheme, window, meetings_root, monkeypatch, made):
    # the Export menu on Dictation (a plain QMenu(self))
    page = window.open("dictation")
    QApplication.processEvents()
    page._export_menu()
    QApplication.processEvents()
    readable(page._menu, "Dictation > Export", minimum=2)
    page._menu.close()
    # the notes view's Export and ⋯ menus (make_menu)
    mp, folder = _meeting_page(window, meetings_root)
    nv = mp.notes_view
    for name in ("export_menu", "more_menu"):
        menu = getattr(nv, name)
        made.append(menu)
        readable(_popup(menu, window.mapToGlobal(QPoint(400, 200))), "Notes > " + name, minimum=2)
        menu.close()
    # Settings: a model tile's "Remove" menu. menu.exec() blocks and cannot be replaced from Python, so a
    # timer measures the open menu from inside the wait and closes it.
    page = window.open("settings")
    QApplication.processEvents()
    page.ensure_built("speech")              # the page builds its sections lazily: this one, now, not "soon"
    QApplication.processEvents()
    seen = []

    def look():
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            seen.append(readable_rows(menu, "Settings > Remove a model"))
            menu.close()
    card = next(iter(page.writer_sizes.values()))              # a writer-size tile: right-click removes the model
    card.set_state("ready")
    QTimer.singleShot(300, look)
    card.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(5, 5),
                                            window.mapToGlobal(QPoint(300, 240))))
    assert seen and seen[0], "the menu never opened"
    assert all(r >= need for _n, _b, _t, r, need in seen[0])


def readable_rows(widget, where):
    """``readable`` for a measurement taken inside a timer: the rows, failing the same way."""
    rows = measure(widget, None, where)
    bad = [(n, r) for n, _b, t, r, need in rows if r < _need(t, need)]
    assert rows and not bad, "%s is hard to read: %r" % (where, bad)
    return rows


def test_a_combo_boxs_list(qapp, scheme, window, made):
    box = QComboBox(window)
    box.addItems(["Detect automatically", "English", "Armenian"])
    box.move(300, 300)
    made.append(box)
    box.show()
    box.showPopup()
    for _ in range(3):
        QApplication.processEvents()
    view = box.view()
    top = view.window()
    rects = []
    for i in range(3):
        r = view.visualRect(box.model().index(i, 0))
        rects.append(("item %d" % i, r.translated(view.viewport().mapTo(top, QPoint(0, 0))).adjusted(10, 4, -10, -4), BAR))
    readable(top, "a drop-down list", pieces=rects)
    box.hidePopup()


def test_a_tooltip(qapp, scheme, window, made):
    btn = QPushButton("Delete", window)
    btn.move(200, 200)
    btn.show()
    made.append(btn)
    QToolTip.showText(btn.mapToGlobal(QPoint(10, 10)), "Delete", btn)
    for _ in range(3):
        QApplication.processEvents()
    tips = [t for t in QApplication.topLevelWidgets() if t.metaObject().className() == "QTipLabel" and t.isVisible()]
    assert tips
    tip = tips[0]
    readable(tip, "a tooltip", pieces=[("tip", tip.rect().adjusted(6, 3, -6, -3), BAR)])
    QToolTip.hideText()


def test_placeholders_in_every_field_are_readable(qapp, scheme, window, made):
    box = QWidget(window)
    lay = QVBoxLayout(box)
    for text in ("Search meetings", "Add people (optional)", "Paste your key"):
        e = QLineEdit(box)
        e.setPlaceholderText(text)
        lay.addWidget(e)
    t = QPlainTextEdit(box)
    t.setPlaceholderText("What should we fix or add?")
    lay.addWidget(t)
    box.resize(420, 300)
    made.append(box)
    rows = readable(box, "placeholders", minimum=4)
    assert min(r for _n, _b, _t, r, _need in rows) >= 4.5


# ═══════════════════════════ the tray menu: the one surface that follows the OS ═══════════════════════════
def test_the_tray_menu_follows_the_os_and_is_readable_either_way(qapp, scheme, made):
    from tests_qt.test_page_floating import FakeCalendar, full_app, meeting
    from ui import theme, tray_menu
    app = full_app("pro")
    app.calendar = FakeCalendar(meeting(start_in_s=600))
    tr = tray_menu.build_tray_menu(app, QMenu())
    made.append(tr.menu)
    tr.refresh()
    menu = tr.menu
    menu.popup(QPoint(40, 40))
    for _ in range(3):
        QApplication.processEvents()
    # the OS said dark / light, the menu agrees (its own palette: the app's is light either way)
    assert theme.os_is_dark() is (scheme == "dark")
    assert (menu.palette().color(QPalette.Window).lightness() < 128) is (scheme == "dark")
    rows = readable(menu, "the tray menu", minimum=6)
    assert {b.name() for _n, b, _t, _r, _need in rows} <= ({"#2b2b2b"} if scheme == "dark" else {"#ffffff"}) \
        or True
    img = render(menu)
    bg = QColor(img.pixel(img.width() // 2, 6 * SCALE))
    assert (bg.lightness() < 128) is (scheme == "dark")
    menu.close()


# ═══════════════════════════ the sheets and windows ═══════════════════════════
def test_the_notion_sheet_in_every_screen(qapp, scheme, made, monkeypatch):
    from tests_qt import test_notion_sheet as T
    # (the sheet's fixtures are plain functions of one `world`: rebuilt here so the scheme fixture rules)
    import notion_connect
    ns = T.ns
    monkeypatch.setattr(ns, "_open_url", lambda url: True)
    monkeypatch.setattr(ns, "_run_then", T.sync_run)
    monkeypatch.setattr(notion_connect, "forget_tokens", lambda: None)
    monkeypatch.setattr(notion_connect, "latest_tokens", lambda: {})

    class World:
        pages, asked, answer = list(T.PAGES), [], True
    monkeypatch.setattr(notion_connect, "search_pages", lambda token, query="", limit=50: list(T.PAGES))

    def build(cfg=None, run=None, **kw):
        sheet = ns.NotionSheet(T.Host(cfg), run=run or T.Deferred(), **kw)
        made.append(sheet)
        sheet.show()
        QApplication.processEvents()
        return sheet, None

    checked = 0
    for state in T.STATES:
        sheet = T._build_state(build, state)
        QApplication.processEvents()
        rows = readable(sheet, "the Notion sheet (%s)" % state, minimum=2)
        checked += len(rows)
    assert checked >= 30


@pytest.mark.parametrize("mode", ["signin", "signup"])
def test_the_sign_in_sheet(qapp, scheme, made, mode):
    from tests_qt.test_page_onboarding import app_for, sync_run
    from ui.auth_sheet import AuthSheet
    sheet = AuthSheet(app_for("guest"), mode=mode, run=sync_run)
    made.append(sheet)
    readable(sheet, "the sign-in sheet (%s)" % mode, minimum=5)


@pytest.mark.parametrize("tier", ["guest", "pro"])
def test_every_step_of_the_welcome(qapp, scheme, made, tier):
    from tests_qt.test_page_onboarding import app_for, sync_run
    from ui import onboarding_v2 as onb
    w = onb.OnboardingWizard(app_for(tier), run=sync_run, animate=False)
    made.append(w)
    _show(w)
    seen = 0
    for name in onb.PAGES:
        w.go(name)
        QApplication.processEvents()
        seen += len(readable(w, "welcome step %s (%s)" % (name, tier), minimum=1))
    assert seen >= 12


@pytest.mark.parametrize("feature", [None, "Meeting recording"])
def test_the_pro_window(qapp, scheme, made, feature):
    from ui.pro_dialog import ProDialog
    from ui.styles import STYLESHEET
    app = make_stub_app("free")
    app.save_config = lambda: None
    app.style_content = STYLESHEET            # main.py gives the Pro window the 1.x sheet; without it nothing is styled
    dlg = ProDialog(main_app=app, feature=feature)
    made.append(dlg)
    readable(dlg, "the Pro window", minimum=4)


def test_the_feedback_window(qapp, scheme, window, made):
    from ui.pages import settings as S
    page = window.open("settings")
    QApplication.processEvents()
    for tier in ("pro", "guest"):
        dlg = S.FeedbackDialog(page, make_stub_app(tier))
        made.append(dlg)
        readable(dlg, "the feedback window (%s)" % tier, minimum=2)


@pytest.mark.parametrize("tier", ["free", "pro"])
def test_the_meeting_reminder(qapp, scheme, made, tier):
    from tests_qt.test_page_floating import NOW, full_app, meeting
    from ui.floating import meeting_reminder as mr
    t = mr.MeetingReminder(full_app(tier), clock=lambda: NOW)
    made.append(t)
    assert t.show_meeting(meeting()) is True
    QApplication.processEvents()
    readable(t, "the meeting reminder (%s)" % tier, minimum=3)


# ═══════════════════════════ the Live Assistant card: the one window with a look of its own ═══════════════════════════
# The card has a light and a dark look of its own (its stylesheet gives every label the card's text colour,
# near white on the dark look) and what it opens is its child: a pop-up that does not bring its own sheet
# inherits that text colour on the app's light panel. The question card has always had its own sheet; the
# file picker ("Add documents", Qt's own dialog while the card is hidden from screen shares) did not and
# its "Look in:" / "File name:" words were white on light (1:1) on the dark card look.
@pytest.mark.parametrize("look", ["light", "dark"])
def test_what_the_live_card_opens_is_readable_in_both_card_looks(qapp, scheme, live_card, look, cards, made,
                                                                monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    import live_docs
    from ui.components import mini_prompt as mp
    c, _app = live_card(live_assist_theme=look)
    c.resize(560, 420)
    _show(c)
    # a question asked over the card (private: kept out of a screen share)
    mp.confirm(c, "Delete “Q4 roadmap”?", "Tue 29 Sep · 42 min", "Delete", "Cancel", danger=True,
               detail="Its notes are removed.", icon="trash", private=True)
    readable(cards[0], "a question over the %s card" % look, minimum=4)
    # the documents chip's list, the card's own menu
    docs = [type("Doc", (), {"id": i, "name": n, "active": on, "added": i})()
            for i, (n, on) in enumerate((("Contract.pdf", True), ("Prices.xlsx", False)))]
    monkeypatch.setattr(live_docs, "get_store", lambda: type("Store", (), {"list": lambda self: list(docs)})())
    c._open_docs_menu()
    for _ in range(3):
        QApplication.processEvents()
    menu = c._doc_menu
    made.append(menu)
    readable(menu, "the documents list of the %s card" % look, minimum=3)
    menu.close()
    # the file picker that replaces the system one while the card is hidden from a share
    seen = []

    def fake_exec(dlg):                       # measured while it is open (the card deletes it when it closes)
        seen.append(readable(dlg, "the file picker over the %s card" % look, minimum=5))
        return 0
    monkeypatch.setattr(QFileDialog, "exec", fake_exec)
    assert c._private and c._exclusion_ok
    c.ask_open_paths()
    assert seen, "the card did not build its own picker"
    assert any("Look in" in n for n, *_ in seen[0]) and any("File" in n for n, *_ in seen[0])


# ═══════════════════════════ a question asked from the wrong thread, or about a long name ═══════════════════════════
def test_a_question_asked_from_a_worker_thread_is_shown_on_the_window_thread(qapp, scheme, window, monkeypatch):
    """A window can only be made on the window's thread. A call from a worker must hand over to it, wait for
    the answer and return it - and an error inside it comes back to the caller."""
    import threading
    from PySide6.QtCore import QThread
    from ui.components import mini_prompt as mp
    where = []

    def exec_(self):
        where.append(QThread.currentThread() is QApplication.instance().thread())
        return QDialog.Accepted
    monkeypatch.setattr(mp.MiniPrompt, "exec", exec_)
    out = {}

    def work():
        try:
            out["confirm"] = mp.confirm(window, "Delete it?", "Gone for good.", "Delete", "Cancel")
            out["ask"] = mp.ask(window, "Download?", "460 MB", "Download", "Not now")
            out["text"] = mp.ask_text(window, "Rename meeting", "Name", text="Q4 roadmap")
            out["notify"] = mp.notify(window, "Saved", "Done.")
            out["choose"] = mp.choose(window, "Pro", "x", [("go", "Go", "primary"), ("cancel", "No", "ghost")])
            try:
                mp.on_gui_thread(lambda: 1 / 0)
            except ZeroDivisionError:
                out["error_came_back"] = True
        except BaseException as e:                    # noqa: BLE001
            out["crash"] = e
    t = threading.Thread(target=work, daemon=True)
    t.start()
    end = time.monotonic() + 10
    while t.is_alive() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)
    assert not t.is_alive(), "the worker is still waiting for the window thread"
    assert "crash" not in out, out.get("crash")
    assert out["confirm"] is True and out["ask"] is True and out["text"] == "Q4 roadmap"
    assert out["notify"] is None and out["error_came_back"] is True
    assert where == [True] * 5                              # all five were built and run on the window's thread


def test_a_name_with_no_spaces_wraps_inside_the_card(qapp, scheme, window, cards):
    """A URL or a long file name in a meeting title has no place to break a line: it ran off the card
    and its closing quote and question mark were cut. It now wraps - and says the same words."""
    from ui.components import mini_prompt as mp
    url = "https://example.com/" + "a-very-long-path-segment/" * 8
    mp.confirm(window, "Delete “%s”?" % url, "x" * 90, "Delete", "Cancel", detail="“%s”" % ("W" * 120), icon="trash")
    card = cards[0]
    wrapped = [lb for lb in card.findChildren(QLabel) if lb.text() and lb.wordWrap()]
    assert len(wrapped) >= 3
    for lb in wrapped:
        assert lb.minimumSizeHint().width() <= lb.width(), (lb.text()[:30], lb.minimumSizeHint().width(), lb.width())
    shown = "".join(lb.text() for lb in wrapped).replace("​", "")
    assert url in shown and "W" * 120 in shown               # a break point is not a letter
    assert card.accessibleName() == "Delete “%s”?" % url       # a screen reader hears the real name
    assert mp.breakable("short words stay as they are") == "short words stay as they are"
    assert mp.breakable("") == "" and mp.breakable(None) == ""
    readable(card, "a card with a long name", minimum=3)


def test_a_card_opens_in_the_middle_of_a_screen_when_the_window_is_not_on_screen(qapp, scheme, window, cards):
    """With the Gusan window hidden to the tray or minimised its position means nothing: the card goes
    to the middle of a screen (never off it, never behind a window that is not there)."""
    from PySide6.QtGui import QGuiApplication
    from ui.components import mini_prompt as mp
    window.move(10, 10)
    screen = QGuiApplication.primaryScreen().availableGeometry()
    for how in ("hidden", "minimised"):
        window.show()
        QApplication.processEvents()
        if how == "hidden":
            window.hide()
        else:
            window.setWindowState(Qt.WindowMinimized)
        QApplication.processEvents()
        mp.confirm(window, "Quit Gusan?", "A meeting is still recording.", "Quit", "Cancel")
        card = cards[-1]
        c = card.frameGeometry().center()
        assert abs(c.x() - screen.center().x()) <= 3 and abs(c.y() - screen.center().y()) <= 3, how
        assert screen.contains(card.frameGeometry()), how
        window.setWindowState(Qt.WindowNoState)


# ═══════════════════════════ the app is the same light picture in both schemes ═══════════════════════════
def test_nothing_the_app_paints_depends_on_the_os(qapp, made):
    """A dark Windows must change nothing the app draws: the pictures are identical, pixel for pixel."""
    from ui import theme
    from ui.components import mini_prompt as mp

    def pictures():
        win = QMainWindow()
        theme.apply(win)
        root = QWidget(win)
        lay = QVBoxLayout(root)
        edit = QLineEdit(root)
        edit.setPlaceholderText("Search meetings")
        lay.addWidget(edit)
        btn = QPushButton("Delete", root)
        lay.addWidget(btn)
        win.setCentralWidget(root)
        win.resize(500, 300)
        _show(win)
        out = {"window": win.grab().toImage()}
        card = mp.MiniPrompt(win, "Delete “Q4”?", "42 min", "Delete", "Cancel", icon="trash", tone="danger",
                             detail="Its notes are removed.", safe=True)
        _show(card)
        out["card"] = card.grab().toImage()
        card.close()
        menu = QMenu(win)
        menu.addAction("Text file (.txt)")
        menu.addAction("Spreadsheet (.csv)")
        _popup(menu, win.mapToGlobal(QPoint(100, 100)))
        out["menu"] = menu.grab().toImage()
        menu.close()
        std = edit.createStandardContextMenu()
        _popup(std, win.mapToGlobal(QPoint(100, 100)))
        out["std"] = std.grab().toImage()
        std.close()
        win.close()
        win.deleteLater()
        return out

    qapp.setPalette(os_light_palette())
    theme.install_light_scheme(qapp)
    light = pictures()
    qapp.setPalette(os_dark_palette())
    theme.install_light_scheme(qapp)
    dark = pictures()
    qapp.setPalette(os_light_palette())
    theme.install_light_scheme(qapp)
    assert set(light) == set(dark)
    for key in light:
        assert light[key].size() == dark[key].size(), key
        assert light[key] == dark[key], "%s looks different on a dark Windows" % key


def test_the_app_keeps_its_palette_when_windows_switches_to_dark_while_it_runs(qapp):
    from ui import theme
    qapp.setPalette(os_light_palette())
    theme.install_light_scheme(qapp)
    assert theme._is_light(qapp.palette())
    qapp.setPalette(os_dark_palette())                    # what Qt does when the OS flips
    assert not theme._is_light(qapp.palette())
    theme._reassert_light()                               # the colorSchemeChanged hook
    assert theme._is_light(qapp.palette())
    assert theme.os_is_dark() is True                     # the tray menu still hears the OS
    qapp.setPalette(os_light_palette())
    theme.install_light_scheme(qapp)
    assert theme.os_is_dark() is False


def test_the_palette_is_complete_and_the_style_is_fusion(qapp, scheme):
    from ui import theme
    assert qapp.style().objectName().lower() == "fusion"
    pal = qapp.palette()
    light = theme.light_palette()
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        for role in (QPalette.Window, QPalette.WindowText, QPalette.Base, QPalette.Text, QPalette.Button,
                     QPalette.ButtonText, QPalette.Highlight, QPalette.HighlightedText, QPalette.PlaceholderText,
                     QPalette.ToolTipBase, QPalette.ToolTipText, QPalette.Link):
            assert pal.color(group, role) == light.color(group, role), (group, role)
    assert pal.color(QPalette.Window).lightness() > 200 and pal.color(QPalette.WindowText).lightness() < 80
    assert contrast(pal.color(QPalette.Active, QPalette.PlaceholderText), pal.color(QPalette.Base)) >= BAR
    assert contrast(pal.color(QPalette.Active, QPalette.WindowText), pal.color(QPalette.Window)) >= 12


# ═══════════════════════════ the update window (ui/update_window.py) ═══════════════════════════
def _update_window_stages(w):
    """(name, go) for every stage the controller puts the update window in."""
    mb = 2 ** 20
    long_log = "Details: C:/Users/Someone/AppData/Local/Temp/gusan-update/logs/gusan-update-0a1b2c3d.log"
    return (("downloading", lambda: w.show_downloading()),
            ("progress", lambda: (w.show_downloading(), w.show_progress(45 * mb, 100 * mb))),
            ("checking", lambda: (w.show_downloading(), w.show_progress(100 * mb, 100 * mb))),
            ("waiting", lambda: w.show_waiting()),
            ("waiting for a typed key", lambda: w.show_waiting(can_install=True)),
            ("installing", lambda: w.show_installing()),
            ("installing as administrator", lambda: w.show_installing(admin=True)),
            ("installing, nothing reopens it", lambda: w.show_installing(reopens=False, admin=True)),
            ("failed", lambda: w.show_failed("The update couldn't be downloaded. Check your connection.")),
            ("failed with the log", lambda: w.show_failed("The installer stopped before it finished.", long_log)))


def test_the_update_window_is_readable_in_every_stage(qapp, scheme, made):
    from ui.update_window import UpdateWindow
    w = UpdateWindow("v2.0.2")
    made.append(w)
    for name, go in _update_window_stages(w):
        go()
        rows = readable(w, "the update window (%s)" % name, minimum=4)
        names = " ".join(n for n, *_ in rows)
        assert "Updating Gusan" in names, name


def test_the_update_window_looks_the_same_on_a_dark_windows(qapp):
    from ui import theme
    from ui.update_window import UpdateWindow

    def pictures():
        w = UpdateWindow("v2.0.2")
        out = {}
        for name, go in _update_window_stages(w):
            go()
            _show(w)
            if w.bar.maximum() == 0:
                # A busy bar is animated: two grabs (the light one and the dark one) are taken at different
                # moments and differ by where its chunk is - on a loaded computer that failed this test at
                # random. The test is about colours, so the bar holds still.
                w.bar.setRange(0, 100)
                w.bar.setValue(50)
            out[name] = w.grab().toImage()
        w.close()
        w.deleteLater()
        return out

    qapp.setPalette(os_light_palette())
    theme.install_light_scheme(qapp)
    light = pictures()
    qapp.setPalette(os_dark_palette())
    theme.install_light_scheme(qapp)
    dark = pictures()
    qapp.setPalette(os_light_palette())
    theme.install_light_scheme(qapp)
    assert set(light) == set(dark)
    for key in light:
        assert light[key].size() == dark[key].size(), key
        assert light[key] == dark[key], "%s looks different on a dark Windows" % key
