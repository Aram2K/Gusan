"""Dark mode's safety net: every main page and window is READABLE in both looks, and a switch reaches
what is already open.

1. The contrast sweep. Every main page (Home, Dictation, Smart Actions, Meetings - the list and a meeting
   open on its notes and on its transcript -, Live Assistant, Audio to text - the list and a transcript -,
   every Settings section, Account) and the windows not already measured in test_dialog_visibility.py
   (What's new, the answer card) are built through the app's own code in the light look and in the dark
   one (the ``scheme`` fixture: the OS is light / dark and app_theme is "system"), painted, and every
   visible label, button, field and combo is measured from the real pixels (test_dialog_visibility's
   helpers): text against what is really behind it, >= 4.5:1, >= 3:1 for large text (18 px, or 14 px
   bold), the known design floors of KNOWN_DESIGN, 2:1 for disabled words.
2. The live switch. Windows are opened in the light look and the look is switched with
   theme.switch("dark") - no restart: the stylesheet, the palette, an icon drawn in a token colour, a
   widget's own sheet, a page's rich text and a shadow must have changed on the widgets already open,
   and the page must be readable in its new look. And back.
"""
import datetime
import json
import os

import pytest
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QWidget

import tests_qt.test_dialog_visibility as vis
from tests_qt.conftest import make_stub_app

LARGE_BAR = 3.0


def _large(w):
    from ui import theme
    f = w.font()
    px = f.pixelSize() if f.pixelSize() > 0 else f.pointSizeF() * theme._dpi() / 72.0
    return px >= 17.9 or (px >= 13.9 and f.weight() >= 600)


@pytest.fixture(autouse=True)
def _wcag_large_text(monkeypatch):
    """Large words need 3:1 (WCAG AA), the rest what test_dialog_visibility says."""
    plain = vis._bar_for

    def bar_for(w):
        need = plain(w)
        return min(need, LARGE_BAR) if need >= vis.BAR and _large(w) else need
    monkeypatch.setattr(vis, "_bar_for", bar_for)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    import storage
    import ui.winfit
    from ui.components import row_actions
    from ui.pages import meeting_views
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)
    monkeypatch.setattr(row_actions, "ANIMATE", False)
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    monkeypatch.setattr(ui.winfit, "fit_on_screen", lambda *a, **k: None)
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    try:
        from ui.pages import meetings as page_mod
        monkeypatch.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    except Exception:
        pass
    return root


@pytest.fixture
def win(qapp, scheme, made):
    from ui import shell
    w = shell.MainWindow(make_stub_app("pro"))
    w.resize(1280, 860)
    w.show()
    made.append(w)
    QApplication.processEvents()
    return w


@pytest.fixture
def made():
    items = []
    yield items
    for w in items:
        try:
            w.hide()
            w.deleteLater()
        except RuntimeError:
            pass
    QApplication.processEvents()


def _spin(n=6):
    for _ in range(n):
        QApplication.processEvents()


def _meeting(root):
    d = root / "20261030_153400"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({
        "title": "Weekly product sync", "attendees": "Anna, David", "duration_sec": 3300,
        "started": "2026-10-30 15:34:00", "language": "en", "seen": True,
        "notes_engine_label": "Gusan Cloud · Mistral Small"}), encoding="utf-8")
    (d / "notes.md").write_text(
        "## Summary\nAram and Anna agreed to ship the **sync engine** on Friday.\n\n"
        "## Action items\n- [ ] Send the release notes (Owner: Anna, Due: Thursday)\n"
        "- [x] Book the room (Owner: You, Due: Friday)\n\n## Open questions\n- Do we need a beta?\n",
        encoding="utf-8")
    (d / "transcript.txt").write_text("You: Shall we ship on Friday?\nAnna: Yes, the sync engine is ready.\n",
                                      encoding="utf-8")
    return d


def _readable(widget, where):
    _spin()
    return vis.readable(widget, where, minimum=3)


# ═══════════════════════════ 1. the contrast sweep ═══════════════════════════
@pytest.mark.parametrize("route", ["home", "dictation", "smart_actions", "live_assistant", "account"])
def test_every_main_page_is_readable(win, route):
    win.open(route)
    _readable(win, "the %s page" % route)


def test_every_settings_section_is_readable(win):
    from ui.pages import settings as S
    for sec in S.SECTION_IDS:
        page = win.open("settings", section=sec)
        page.ensure_built()
        _readable(win, "Settings > %s" % sec)


def test_the_meetings_list_and_an_open_meeting_are_readable(win, _quiet):
    folder = _meeting(_quiet)
    for i in range(2):
        o = _quiet / ("2026092%d_110000" % (i + 1))
        o.mkdir()
        (o / "meta.json").write_text(json.dumps({"title": "Meeting %d" % i, "duration_sec": 600,
                                                 "started": "2026-09-2%d 11:00:00" % (i + 1)}), encoding="utf-8")
        (o / "notes.md").write_text("## Summary\nx", encoding="utf-8")
    win.open("meetings")
    _readable(win, "Meetings (the list)")
    page = win.open("meetings", folder=str(folder))
    _readable(win, "a meeting's notes")
    page.notes_view.stack_index(1)
    _readable(win, "a meeting's transcript")


def test_audio_to_text_and_a_transcript_are_readable(win, monkeypatch):
    import transcript_store
    import ui.pages.transcript_view as tv
    monkeypatch.setattr(tv, "_spawn", lambda fn: fn())
    transcript_store.delete_all_transcripts()
    try:
        segs = [{"start": 0.0, "end": 3.5, "text": "Thanks for coming in, Mariam.", "speaker": 0},
                {"start": 4.0, "end": 8.0, "text": "Easy, thanks. Traffic was kind today.", "speaker": 1}]
        res = {"path": os.path.join("C:/nowhere", "interview.m4a"), "name": "interview.m4a", "duration": 1460,
               "segments": segs, "speakers": True, "timestamps": True, "model": "small", "language": "en",
               "language_setting": "auto"}
        tid = os.path.basename(str(transcript_store.save_transcript(res, created=datetime.datetime.now().astimezone())))
        page = win.open("audio_to_text")
        _readable(win, "Audio to text")
        page.open_transcript(tid)
        _readable(win, "a transcript")
    finally:
        transcript_store.delete_all_transcripts()


def test_whats_new_and_the_answer_card_are_readable(qapp, scheme, made):
    import whats_new
    from ui.answer_card import AnswerCard
    from ui.whats_new_window import WhatsNewWindow
    w = WhatsNewWindow(whats_new.sections("2.1.3", "2.1.4"), current="2.1.4", previous="2.1.3",
                       release_url="https://example.invalid/r", animate=False)
    made.append(w)
    w.show()
    _spin()
    # Its rows rise in with opacity effects, which Qt misplaces when a widget is painted onto an image (the
    # Pro window's test turns them off the same way): the words are measured without them.
    from PySide6.QtWidgets import QGraphicsOpacityEffect
    for x in [w] + w.findChildren(QWidget):
        if isinstance(x.graphicsEffect(), QGraphicsOpacityEffect):
            x.graphicsEffect().setEnabled(False)
    _readable(w, "What's new")
    card = AnswerCard()
    made.append(card)
    card.show_text("**Yes.** It is polite.\n\n- warm\n- short", "answer",
                   sources=[{"title": "ECB euro reference rates", "url": "https://www.ecb.europa.eu/rates"}])
    _readable(card, "the answer card")


# ═══════════════════════════ 2. the live switch ═══════════════════════════
def _colour_of_icon(icon):
    """The colour of the strongest pixel of an icon (its stroke)."""
    img = icon.pixmap(18, 18).toImage()
    best, alpha = None, -1
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > alpha:
                best, alpha = c, c.alpha()
    return best.name()


def test_a_switch_reaches_everything_already_open(qapp, made, _quiet):
    """Light, then theme.switch("dark") with the windows open, then back: no restart."""
    from ui import theme
    from ui.components import icons, mini_prompt
    from ui.pages import meeting_views
    from tests_qt.conftest import set_os_scheme
    set_os_scheme(qapp, "light")
    folder = _meeting(_quiet)
    from ui import shell
    w = shell.MainWindow(make_stub_app("pro"))
    w.resize(1280, 860)
    w.show()
    made.append(w)
    page = w.open("meetings", folder=str(folder))
    _spin()
    nv = page.notes_view
    card = mini_prompt.MiniPrompt(w, "Delete it?", "Gone for good.", "Delete", "Cancel", icon="trash", tone="danger")
    made.append(card)
    card.show()
    _spin()
    # what is there in the light look
    sheet_light = w.styleSheet()
    card_sheet_light = card.styleSheet()
    pal_light = qapp.palette().color(QPalette.Window).name()
    labels = [lb for lb in w.findChildren(QLabel) if lb.isVisibleTo(w)]
    own_sheets = {id(lb): lb.styleSheet() for lb in w.findChildren(QLabel) if getattr(lb, "_gusan_sheet", None)}
    assert own_sheets, "no widget with a sheet of its own on the page"
    rich = [lb for lb in labels if lb.textFormat() != Qt.PlainText and "color:" in lb.text()]
    rich_light = {id(lb): lb.text() for lb in rich}
    btn = nv.export_btn if hasattr(nv, "export_btn") else None
    icon_light = _colour_of_icon(btn.icon()) if btn is not None and not btn.icon().isNull() else None
    probe = QLabel(w)
    probe.setPixmap(icons.pixmap("gear", 18, "text2"))
    probe_light = _colour_of_icon(icons.icon("gear", 18, "text2"))
    effects = [x.graphicsEffect() for x in w.findChildren(QWidget)
               if x.graphicsEffect() is not None and x.graphicsEffect().property("gusanShadow")]
    shadow_light = [e.color().name(QColor.HexArgb) for e in effects]
    try:
        assert theme.switch("dark") is True
        _spin()
        assert theme.scheme() == "dark"
        assert w.styleSheet() != sheet_light and w.styleSheet() == theme.stylesheet()        # the window's sheet
        assert card.styleSheet() != card_sheet_light and card.styleSheet() == theme.stylesheet()   # a card's
        assert qapp.palette().color(QPalette.Window).name() != pal_light                    # the palette
        changed = [i for i, s in own_sheets.items()
                   for lb in w.findChildren(QLabel) if id(lb) == i and lb.styleSheet() != s]
        assert changed, "no widget's own sheet followed the switch"
        if rich_light:
            assert any(lb.text() != rich_light[id(lb)] for lb in rich if id(lb) in rich_light), \
                "the page's rich text kept its light colours"
        assert probe.pixmap().cacheKey() == icons.pixmap("gear", 18, "text2").cacheKey()   # re-tinted in place
        assert _colour_of_icon(icons.icon("gear", 18, "text2")) != probe_light
        if icon_light is not None:
            assert _colour_of_icon(btn.icon()) != icon_light, "an open button kept its light icon"
        if effects:
            assert [e.color().name(QColor.HexArgb) for e in effects] != shadow_light          # shadows deeper
        vis.readable(card, "a question card after a live switch to dark", minimum=3)
        card.hide()                                  # (its own window: not part of the main window's picture)
        vis.readable(w, "a meeting after a live switch to dark", minimum=3)
    finally:
        theme.switch("light")
        _spin()
    assert w.styleSheet() == sheet_light and card.styleSheet() == card_sheet_light
    assert qapp.palette().color(QPalette.Window).name() == pal_light
    assert probe.pixmap().cacheKey() == icons.pixmap("gear", 18, "text2").cacheKey()


def test_settings_appearance_switches_the_open_windows(qapp, made, monkeypatch):
    """Settings > General > Appearance writes app_theme; settings_apply turns every open window at once."""
    import settings_apply
    from ui import theme
    from tests_qt.conftest import set_os_scheme
    set_os_scheme(qapp, "light")
    app = make_stub_app("pro")
    app.cfg["app_theme"] = "system"
    app.schedule_config_save = lambda: None
    app.sig_setting_changed = type("Sig", (), {"emit": lambda self, k: None})()
    app.apply_app_theme = lambda: theme.set_preference(app.cfg.get("app_theme"))
    app.set_settings = lambda updates: settings_apply.apply(app, updates)
    from ui import shell
    w = shell.MainWindow(app)
    w.resize(1280, 860)
    w.show()
    made.append(w)
    page = w.open("settings", section="general")
    _spin()
    try:
        before = w.styleSheet()
        assert settings_apply.apply(app, {"app_theme": "dark"}) == {"app_theme": True}
        _spin()
        assert theme.scheme() == "dark" and w.styleSheet() != before and app.cfg["app_theme"] == "dark"
        vis.readable(w, "Settings after picking Dark", minimum=5)
        assert settings_apply.apply(app, {"app_theme": "purple"}) == {"app_theme": False}   # not a look
        assert settings_apply.apply(app, {"app_theme": "system"}) == {"app_theme": True}
        _spin()
        assert theme.scheme() == "light"                                                    # the OS is light
    finally:
        set_os_scheme(qapp, "light")


# ═══════════════════════════ 3. Invite friends ═══════════════════════════
@pytest.fixture
def invite_fake(monkeypatch):
    """The invite function is a fake (tests_qt/test_invite_ui.py's), calls run in place, no motion (no shine, no
    confetti: the pixels hold still)."""
    import invites
    from ui import invite_hub
    from ui.components import celebrate
    from tests_qt import test_invite_ui as T
    s = T.FakeServer()
    monkeypatch.setattr(invites, "_post", s)
    monkeypatch.setattr(invites, "device_hash", lambda: "d" * 64)
    monkeypatch.setattr(invite_hub.InviteHub, "sync_default", True)
    celebrate.set_motion(False)
    yield s
    celebrate.set_motion(None)


def _large_font(f):
    from ui import theme
    px = f.pixelSize() if f.pixelSize() > 0 else f.pointSizeF() * theme._dpi() / 72.0
    return px >= 17.9 or (px >= 13.9 and f.weight() >= 600)


def _flags(align):
    return align.value if hasattr(align, "value") else int(align)


def words_pieces(widget, root, where):
    """The words a widget paints itself (its words(): text, font key, rect, colour, alignment) as pieces to measure
    in ``root`` pixels - the text's own box, cut to what is on screen."""
    from PySide6.QtGui import QFontMetricsF
    clip = vis._clip(widget, root)
    if clip is None:
        return []
    off = widget.mapTo(root, QPoint(0, 0))
    out = []
    for text, key, rect, _colour, align in widget.words():
        if not str(text).strip():
            continue
        font = widget.font_of(key)
        box = QFontMetricsF(font).boundingRect(rect, _flags(align), text).toAlignedRect().adjusted(-2, -1, 2, 1)
        r = box.translated(off).intersected(clip)
        if r.width() > 4 and r.height() > 4:
            out.append(("%s %r" % (where, str(text)[:30]), r, LARGE_BAR if _large_font(font) else vis.BAR))
    return out


def sidebar_invite_pieces(win):
    """The sidebar's READY pill (white on the brand's violet, like the PRO pill: 3:1) and the dots that mean
    something (a friend who counts, one on the way: UI marks, 3:1)."""
    from ui.components.celebrate import InviteDots
    item = win.sidebar.invite_item
    out = []
    pill = item.ready_pill
    if pill.isVisible():
        pad = pill._pad
        out.append(("the READY pill", QRect(pill.mapTo(win, QPoint(pad, pad)),
                                           QSize(pill.width() - 2 * pad, pill.height() - 2 * pad)), vis.BAR_BRAND))
    dots = item.dots
    if dots.isVisible():
        counted, progress = dots.state()
        s, gap = InviteDots.SIZE, InviteDots.GAP
        y = (dots.height() - s) // 2
        for i in range(counted + progress):
            at = dots.mapTo(win, QPoint(int(2 + i * (s + gap)) - 2, y - 2))
            out.append(("invite dot %d" % (i + 1), QRect(at, QSize(s + 4, s + 4)), LARGE_BAR))
    return out


def _invite_window(made, app, height=960):
    from ui import shell
    w = shell.MainWindow(app)
    w.resize(1280, height)
    w.show()
    made.append(w)
    _spin()
    return w


INVITE_STATES = {"progress": "locked", "ready": "ready", "active": "active", "credited": "credited"}


@pytest.mark.parametrize("state", sorted(INVITE_STATES))
def test_the_invite_page_is_readable(qapp, scheme, made, invite_fake, state):
    from tests_qt import test_invite_ui as T
    # (progress: two friends joined, and this account can still enter a friend's code - its card is measured too)
    invite_fake.me = {"progress": T.ME([T.MARIAM, T.DAVIT], can_join=True), "ready": T.READY, "active": T.ACTIVE,
                      "credited": T.CREDITED}[state]
    w = _invite_window(made, T.stub("free"))
    page = w.open("invite")
    _spin(10)
    assert page.ticket.state() == INVITE_STATES[state]
    pieces = (vis.widget_pieces(w) + words_pieces(page.slots, w, "slot") + words_pieces(page.ticket, w, "ticket")
              + sidebar_invite_pieces(w))
    rows = vis.readable(w, "the Invite page (%s)" % state, pieces=pieces, minimum=20)
    said = " ".join(r[0] for r in rows)
    assert "ticket" in said and "slot 'Mariam'" in said and "'Copy link'" in said and "ARAM-7K2Q" in said
    assert ("READY pill" in said) == (state == "ready")
    if state == "progress":
        assert "invite dot 2" in said                                     # Mariam and Davit count
        assert "Got a friend's code?" in said


def test_the_found_invite_card_is_readable(qapp, scheme, made, invite_fake):
    """Account's "Got a friend's code?" when the server found the friend's invite: "Aram invited you", Join, Not me."""
    from tests_qt import test_invite_ui as T
    invite_fake.me = T.ME(can_join=True)
    invite_fake.answers["hint"] = {"found": True, "name": "Aram", "code": "aram-7k2q"}
    w = _invite_window(made, T.stub("free"))
    page = w.open("account")
    _spin(10)
    card = page.join_card
    assert card is not None and card.isVisible() and card.mode == "found"
    box = QRect(card.mapTo(w, QPoint(0, 0)), card.size())
    pieces = [p for p in vis.widget_pieces(w) if box.contains(p[1].center())]
    rows = vis.readable(w, "Account's invite card (a friend's invite found)", pieces=pieces, minimum=4)
    assert any("Aram invited you" in r[0] for r in rows) and any("Not me" in r[0] for r in rows)


def test_invite_follows_a_live_switch(qapp, made, invite_fake):
    """The Invite page and Account's invite card, built light, after theme.switch("dark") and back: the words made
    with their own colours (the page's labels, the card's - once left light -, the link field and its rich text,
    the Claim button, the card's edge) follow, and both read in the new look."""
    from ui import theme
    from tests_qt import test_invite_ui as T
    from tests_qt.conftest import set_os_scheme
    set_os_scheme(qapp, "light")
    invite_fake.me = T.READY
    w = _invite_window(made, T.stub("free"))
    page = w.open("invite")
    _spin(10)
    invite_fake.me = T.ME(can_join=True)
    invite_fake.answers["hint"] = {"found": True, "name": "Aram", "code": "aram-7k2q"}
    w2 = _invite_window(made, T.stub("free"))
    account = w2.open("account")
    _spin(10)
    card = account.join_card
    assert card is not None and card.mode == "found"
    own = {"title": page.title_label, "lead": page.lead, "link field": page.share.field,
           "card title": card.title, "card line": card.sub}
    # (Claim my month is white with deep-violet words on the violet ticket in both looks: its sheet is kept by
    # theme.sheet, and has nothing to change)
    assert getattr(page.ticket.claim_button, "_gusan_sheet", None) is not None
    light = {k: x.styleSheet() for k, x in own.items()}
    link_light = page.share.field.text.text()
    assert theme.qss_color("ink") in light["card title"]
    try:
        assert theme.switch("dark") is True
        _spin(6)
        stale = [k for k, x in own.items() if x.styleSheet() == light[k]]
        assert not stale, "kept their light colours after a switch to dark: %s" % stale
        assert theme.qss_color("ink") in card.title.styleSheet()
        assert page.share.field.text.text() != link_light and theme.qss_color("blue-tx") in page.share.field.text.text()
        pieces = (vis.widget_pieces(w) + words_pieces(page.slots, w, "slot") + words_pieces(page.ticket, w, "ticket")
                  + sidebar_invite_pieces(w))
        vis.readable(w, "the Invite page after a live switch to dark", pieces=pieces, minimum=20)
        box = QRect(card.mapTo(w2, QPoint(0, 0)), card.size())
        vis.readable(w2, "Account's invite card after a live switch to dark",
                     pieces=[p for p in vis.widget_pieces(w2) if box.contains(p[1].center())], minimum=4)
    finally:
        theme.switch("light")
        _spin()
    assert {k: x.styleSheet() for k, x in own.items()} == light
    assert page.share.field.text.text() == link_light


# ═══════════════════════════ 4. muted words in the light look ═══════════════════════════
def test_muted_words_and_unpicked_options_read_in_the_light_look(qapp, made, _quiet):
    """Muted words straight on the window's gradient and an unpicked option on a segmented control's grey track
    read >= 4.5:1 in the light look (they were 4.46:1 and 4.27-4.35:1, allowed as a design floor; the muted token
    is a little darker now and they are measured like any other words)."""
    from PySide6.QtWidgets import QPushButton
    from ui import shell, theme
    from tests_qt.conftest import set_os_scheme
    set_os_scheme(qapp, "light")
    assert theme.qss_color("muted") not in vis.KNOWN_DESIGN and "#5f6f86" not in vis.KNOWN_DESIGN
    folder = _meeting(_quiet)
    w = shell.MainWindow(make_stub_app("pro"))
    w.resize(1280, 860)
    w.show()
    made.append(w)
    seen = {"seg": 0, "muted": 0}
    for where, go in (("Dictation", lambda: w.open("dictation")),
                      ("Settings > General", lambda: w.open("settings", section="general").ensure_built()),
                      ("a meeting", lambda: w.open("meetings", folder=str(folder)))):
        go()
        _spin(6)
        pieces = []
        for x in w.findChildren(QWidget):
            if not x.isVisibleTo(w):
                continue
            clip = vis._clip(x, w)
            if clip is None:
                continue
            if isinstance(x, QPushButton) and x.property("gusan") == "seg" and not x.isChecked() and x.text():
                pieces.append(("unpicked option %r" % x.text(), clip.adjusted(8, 5, -8, -5), vis.BAR))
                seen["seg"] += 1
            elif isinstance(x, QLabel) and x.property("textRole") in ("muted", "hint", "subtitle", "model") \
                    and x.text().strip() and x.pixmap().isNull():
                pieces.append(("muted %r" % x.text()[:30], clip, vis.BAR))
                seen["muted"] += 1
        vis.readable(w, where + " (muted words, unpicked options)", pieces=pieces, minimum=2)
    assert seen["seg"] >= 3 and seen["muted"] >= 5, seen
