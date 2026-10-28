"""Gusan 2.0.1: the speech pickers say what each model IS (Live Assistant "Transcribes with",
Smart Actions "Transcribes with", Audio to text "Accuracy", Settings > Speech & AI).

Every row is a REAL model name with a small chip for where it runs - LOCAL (this computer), CLOUD
(Gusan Cloud or your own key) and PRO where a Pro plan is needed - under headings for its group
("On this computer", "Gusan Cloud", "Your own key"); Gusan Cloud is a service, never a model.
The closed box shows the selected name and its chips. The rows come from engine_choice
(speech_row dicts), drawn by ui/components/model_combo.py (fill_rows).

Set GUSAN_SHOTS_DIR to also write renders (closed and open, picker_tags_*.png).
"""
import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

SHOTS = os.environ.get("GUSAN_SHOTS_DIR", "")


def _shot(widget, name):
    """Write a render when GUSAN_SHOTS_DIR is set (a 150% run adds _150 to the name)."""
    if not SHOTS:
        return
    os.makedirs(SHOTS, exist_ok=True)
    scale = os.environ.get("QT_SCALE_FACTOR", "1")
    suffix = "" if scale in ("1", "1.0") else "_%d" % round(float(scale) * 100)
    widget.grab().save(os.path.join(SHOTS, "picker_tags_%s%s.png" % (name, suffix)))


@pytest.fixture
def host(qapp):
    from ui import theme
    made = []

    def build(w=420, h=520):
        win = QWidget()
        theme.apply(win)
        win.resize(w, h)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        win.show()
        QApplication.processEvents()
        made.append(win)
        return win
    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def _box(win, rows, width=260, current=None):
    from ui.components.model_combo import PlainNameCombo, combo
    box = combo(PlainNameCombo(win), "Transcribes with", width=width)
    box.fill_rows(rows)
    win.layout().addWidget(box, 0, Qt.AlignLeft)
    if current is not None:
        box.setCurrentIndex(max(0, box.findData(current)))
    QApplication.processEvents()
    return box


def _open_shot(box, name):
    box.showPopup()
    QApplication.processEvents()
    pop = box.view().window()
    _shot(pop, name)
    box.hidePopup()
    QApplication.processEvents()


def _cfg(**kw):
    return dict({"live_assist_language": "hy", "backend": "local", "whisper_model": "small",
                 "google_api_key": "AIza-test"}, **kw)


def _closed_pixels(box):
    return box.grab().toImage()


def _has_colour(img, rect, test):
    """Is there a pixel in ``rect`` (x0, y0, x1, y1, in logical pixels) that ``test(r, g, b)`` likes?"""
    from PySide6.QtGui import QColor
    ratio = img.devicePixelRatio() or 1.0
    x0, y0, x1, y1 = (int(v * ratio) for v in rect)
    for y in range(max(0, y0), min(img.height(), y1)):
        for x in range(max(0, x0), min(img.width(), x1)):
            c = QColor(img.pixel(x, y))
            if test(c.red(), c.green(), c.blue()):
                return True
    return False


def _purple(r, g, b):
    return r > 100 and b > 150 and g < 90          # the PRO pill's gradient


def _blue_chip(r, g, b):
    return b > 235 and 200 < r < 240 and 215 < g < 245 and b - r > 10        # CLOUD's light blue


def _green_chip(r, g, b):
    return g > 235 and r < 235 and b < 240 and g - r > 8 and g - b > 4        # LOCAL's light green


def _option(box):
    from PySide6.QtWidgets import QStyleOptionViewItem
    opt = QStyleOptionViewItem()
    opt.font = box.font()
    opt.fontMetrics = box.fontMetrics()
    return opt


# ── the combo draws what engine_choice says ─────────────────────────────────────
def test_rows_come_with_headings_and_headings_are_not_rows(qapp, host):
    import engine_choice as ec
    win = host()
    rows = ec.live_speech_rows(_cfg(), True, True)
    box = _box(win, rows, current="")
    assert box.headings() == ["On this computer", "Gusan Cloud", "Your own key"]
    assert len(box.row_indexes()) == len(rows) and box.count() == len(rows) + 3
    values = [box.itemData(i) for i in box.row_indexes()]
    assert values == [r["value"] for r in rows]
    for i in range(box.count()):
        if box.is_heading(i):
            assert box.itemData(i) is None
            flags = box.model().flags(box.model().index(i, 0))
            assert not (flags & Qt.ItemIsEnabled) and not (flags & Qt.ItemIsSelectable)   # it cannot be picked
    # a refill replaces everything and leaves the signals as they were
    seen = []
    box.currentIndexChanged.connect(lambda i: seen.append(i))
    box.fill_rows(ec.live_speech_rows({}, False, False))
    assert box.headings() == ["On this computer", "Gusan Cloud"] and seen == []


def test_a_row_without_a_group_has_no_heading_and_a_group_keeps_one_heading(qapp, host):
    win = host()
    rows = [{"value": "a", "title": "A", "line": "A", "group": "", "tags": (), "note": ""},
            {"value": "b", "title": "B", "line": "B", "group": "G", "tags": ("local",), "note": ""},
            {"value": "c", "title": "C", "line": "C", "group": "G", "tags": ("cloud",), "note": ""},
            {"value": "d", "title": "D", "line": "D", "group": "", "tags": (), "note": ""}]
    box = _box(win, rows)
    assert [box.itemText(i) for i in range(box.count())] == ["A", "G", "B", "C", "D"]
    assert box.headings() == ["G"]


def test_the_closed_box_paints_the_name_and_its_chips(qapp, host):
    import engine_choice as ec
    win = host()
    box = _box(win, ec.live_speech_rows(_cfg(), True, True), current="cloud:best")
    assert box.plain_text() == "Best accuracy" and box.closed_text() == "Best accuracy · Cloud · Pro"
    assert box.current_tags() == ("cloud", "pro")
    img = _closed_pixels(box)
    w, h = box.width(), box.height()
    assert _has_colour(img, (w - 110, 6, w - 34, h - 6), _purple), "the PRO pill is painted by the name"
    assert _has_colour(img, (w - 150, 6, w - 70, h - 6), _blue_chip), "and CLOUD's chip"
    box.setCurrentIndex(box.findData("small"))
    QApplication.processEvents()
    img = _closed_pixels(box)
    assert box.current_tags() == ("local",)
    assert _has_colour(img, (w - 110, 6, w - 34, h - 6), _green_chip)
    assert not _has_colour(img, (w - 150, 6, w - 34, h - 6), _purple)
    box.setCurrentIndex(box.findData("own:google:gemini-2.5-pro"))
    QApplication.processEvents()
    assert box.closed_text() == "Gemini 2.5 Pro · your Google key · Cloud"      # the whole title, it fits at 260
    assert not _has_colour(_closed_pixels(box), (w - 150, 6, w - 34, h - 6), _purple)   # no Pro plan needed


def _dark(r, g, b):
    return r < 90 and g < 100 and b < 120           # the name's ink


def test_a_name_is_never_cut_to_nothing_the_last_chips_give_way_first(qapp, host):
    import engine_choice as ec
    win = host(w=520)
    rows = ec.live_speech_rows(_cfg(), True, True)
    for width in (150, 170, 180, 220, 260, 320):
        box = _box(win, rows, width=width, current="own:google:gemini-3-flash-preview")
        img = _closed_pixels(box)
        w, h = box.width(), box.height()
        assert _has_colour(img, (12, 8, 70, h - 8), _dark), (width, "the name is readable")
        if width >= 220:                                               # the chip stays whenever the name keeps its room
            assert _has_colour(img, (w - 34 - 60, 6, w - 34, h - 6), _blue_chip), width
        box.deleteLater()
        QApplication.processEvents()
    # Automatic + CLOUD + PRO: whole at 260; in 170 PRO gives way to the name, in 320 nothing does
    box = _box(win, rows, width=260, current="")
    assert _has_colour(_closed_pixels(box), (260 - 34 - 100, 6, 260 - 34, 30), _purple)
    box = _box(win, rows, width=200, current="")
    img = _closed_pixels(box)                                            # PRO gives way, CLOUD stays
    assert _has_colour(img, (12, 8, 70, 30), _dark) and _has_colour(img, (200 - 34 - 70, 6, 200 - 34, 30), _blue_chip)
    assert not _has_colour(img, (12, 6, 200 - 34, 30), _purple)
    box = _box(win, rows, width=150, current="")
    img = _closed_pixels(box)                                            # nothing fits beside the name: the name
    assert _has_colour(img, (12, 8, 70, 30), _dark) and not _has_colour(img, (12, 6, 150 - 34, 30), _purple)


def test_the_open_list_shows_every_row_whole(qapp, host):
    import engine_choice as ec
    win = host(w=600)
    box = _box(win, ec.live_speech_rows(_cfg(), True, True), width=200, current="")      # a narrow closed box
    box.showPopup()
    QApplication.processEvents()
    delegate = box.itemDelegate()
    widest = max(delegate.row_width(box.model().index(i, 0), box.font()) for i in range(box.count()))
    pop = box.view().window()
    assert pop.width() >= widest, "no row is cut: the list is as wide as its widest row"
    assert box.view().minimumWidth() >= widest
    view = box.view()
    for i in range(box.count()):
        index = box.model().index(i, 0)
        size = delegate.sizeHint(_option(box), index)
        assert view.visualRect(index).height() >= size.height() - 1, i       # a two-line row gets its two lines
        assert size.width() <= pop.width(), i
    box.hidePopup()


def test_rows_stay_whole_at_a_bigger_font(qapp, host):
    import engine_choice as ec
    from ui import theme
    win = host(w=700)
    box = _box(win, ec.live_speech_rows(_cfg(), True, True), width=260)
    for px in (13.5, 16.0, 18.0):
        box.setFont(theme.font(px))
        box.showPopup()
        QApplication.processEvents()
        delegate = box.itemDelegate()
        widest = max(delegate.row_width(box.model().index(i, 0), box.font()) for i in range(box.count()))
        assert box.view().window().width() >= widest, px
        box.hidePopup()
        QApplication.processEvents()


def test_the_chips_are_drawn_again_when_the_style_changes(qapp, host):
    import engine_choice as ec
    from PySide6.QtCore import QEvent
    win = host()
    box = _box(win, ec.live_speech_rows(_cfg(), True, True), current="small")
    box.itemDelegate().chip_pixmap("local")
    assert box.itemDelegate()._chip
    QApplication.sendEvent(box, QEvent(QEvent.StyleChange))
    assert not box.itemDelegate()._chip                              # forgotten: grabbed again under the new style


def test_a_chip_is_a_pill_not_a_square(qapp, host):
    import engine_choice as ec
    win = host()
    box = _box(win, ec.live_speech_rows(_cfg(), True, True), current="cloud")
    for tag in ("local", "cloud", "pro"):
        img = box.itemDelegate().chip_pixmap(tag).toImage()
        assert img.pixelColor(0, 0).alpha() < 40, tag                # the corners are cut to the pill's radius
        mid = img.pixelColor(img.width() // 2, img.height() // 2)
        assert mid.alpha() > 200, tag


def test_every_row_is_named_for_a_screen_reader_with_its_tags_and_second_line(qapp, host):
    import engine_choice as ec
    win = host()
    box = _box(win, ec.live_speech_rows(_cfg(), True, True))
    spoken = box.itemData(box.findData("cloud:best"), Qt.AccessibleTextRole)
    assert spoken == "Best accuracy · Cloud · Pro. Most accurate · Gusan chooses the model"
    assert box.itemData(box.findData("cloud:best"), Qt.ToolTipRole) == "Runs in the cloud. Needs a Pro plan"
    assert box.itemData(box.findData("small"), Qt.ToolTipRole) == "Runs on this computer"
    box.set_choice_mark(box.findData("small"), "download", "460 MB")
    assert box.itemData(box.findData("small"), Qt.ToolTipRole) == "Runs on this computer. Not downloaded yet (460 MB)"
    assert box.itemData(box.findData("small"), Qt.AccessibleTextRole) == \
        "Whisper Small · Local, not downloaded. Balanced"


def test_a_plain_picker_is_untouched(qapp, host):
    from ui.components.model_combo import PlainNameCombo, combo
    win = host()
    box = combo(PlainNameCombo(win), "Plain", width=200)
    box.add_choice("Balanced · Whisper Small", "small", plain="Balanced")
    win.layout().addWidget(box)
    QApplication.processEvents()
    assert box.plain_text() == "Balanced" and box.closed_text() == "Balanced"
    assert box.current_tags() == () and box.headings() == []


# ── renders (set GUSAN_SHOTS_DIR) ────────────────────────────────────────────────
def test_render_the_picker_in_every_state(qapp, host):
    import engine_choice as ec
    win = host(w=420, h=130)
    rows = ec.live_speech_rows(_cfg(), True, True)
    box = _box(win, rows, current="")
    _shot(win, "live_closed_automatic")
    for value, name in (("small", "local"), ("cloud", "gusan_flash"), ("cloud:best", "gusan_pro"),
                        ("own:google:gemini-2.5-pro", "own_pro")):
        box.setCurrentIndex(box.findData(value))
        QApplication.processEvents()
        _shot(win, "live_closed_" + name)
    box.setCurrentIndex(0)
    win2 = host(w=420, h=130)
    narrow = _box(win2, rows, width=170, current="")
    _shot(win2, "live_closed_narrow_automatic")
    narrow.setCurrentIndex(narrow.findData("own:google:gemini-3-flash-preview"))
    QApplication.processEvents()
    _shot(win2, "live_closed_narrow_own_long")
    _open_shot(box, "live_open_pro_key")
    free = _box(host(w=420, h=130), ec.live_speech_rows({"live_assist_language": "hy"}, False, False), current="")
    _shot(free.window(), "live_closed_free_automatic")
    _open_shot(free, "live_open_free")


# ── the pages, as a person sees them ─────────────────────────────────────────────
from tests_qt.test_live_speech_row import build as live_build, la  # noqa: E402,F401  (fixtures)
from tests_qt.test_page_audio_to_text import build as audio_build  # noqa: E402,F401
from tests_qt.test_smart_actions_v2 import asked, live_app, page_for, tries  # noqa: E402,F401


def _rail_shot(page, box, name, above=70, below=170):
    """The part of the page around a picker (closed), then the open list."""
    from PySide6.QtCore import QPoint, QRect
    from ui import theme
    theme.apply(page)                      # the pages are themed by the main window; a bare page is not
    QApplication.processEvents()
    top_left = box.mapTo(page, QPoint(0, 0))
    area = QRect(max(0, top_left.x() - 30), max(0, top_left.y() - above), box.width() + 60,
                 box.height() + above + below)
    _shot_pixmap(page.grab(area), name + "_closed")
    box.showPopup()
    QApplication.processEvents()
    _shot(box.view().window(), name + "_open")
    box.hidePopup()
    QApplication.processEvents()


def _shot_pixmap(pm, name):
    if not SHOTS:
        return
    os.makedirs(SHOTS, exist_ok=True)
    scale = os.environ.get("QT_SCALE_FACTOR", "1")
    suffix = "" if scale in ("1", "1.0") else "_%d" % round(float(scale) * 100)
    pm.save(os.path.join(SHOTS, "picker_tags_%s%s.png" % (name, suffix)))


def test_render_the_pages(qapp, live_build, page_for, audio_build):
    page, app = live_build("pro", live_assist_language="hy", google_api_key="AIza-test")
    _rail_shot(page, page.speech.combo, "page_live_pro_armenian")
    page, app = live_build("free", live_assist_language="en")
    _rail_shot(page, page.speech.combo, "page_live_free")

    sa = page_for(live_app("pro"))
    _rail_shot(sa, sa.speech_combo, "page_smart_pro")
    sa = page_for(live_app("free"))
    _rail_shot(sa, sa.speech_combo, "page_smart_free")

    at, _app = audio_build("pro", google_api_key="AIza-test", mistral_api_key="m-test")
    _rail_shot(at, at.model_combo, "page_audio_pro")
    at, _app = audio_build("free")
    _rail_shot(at, at.model_combo, "page_audio_free")


# ── the card's footer, Settings' tiles, onboarding's card ─────────────────────────
from tests_qt.test_live_speech_row import HY as _HY, _gap_report, _report, card  # noqa: E402,F401
from tests_qt.test_page_settings_account import env  # noqa: E402,F401


def test_render_the_cards_footer_in_the_words_of_the_pickers(card):
    c, app = card(cfg=_HY, pro=True, report=_report(cloud_model="", ran_model="gemini-2.5-flash", cloud_pieces=3))
    c.set_meeting_active(True, "Call")
    c.show()
    QApplication.processEvents()
    _shot(c, "card_footer_running")
    app.report = _gap_report()
    c._on_tick()
    c.feed_transcript("Բարև ձեզ", answer=False)
    c.feed_transcript("[...]")
    QApplication.processEvents()
    _shot(c, "card_footer_gap")
    assert c.lbl_hear.text().split("\n")[0] == "Transcribing with GPT Transcribe · Cloud"


def test_settings_says_the_same_words(env):
    win, app = env.build("pro")
    win.show()
    page = win.open("settings", section="speech")
    page.ensure_built()
    QApplication.processEvents()
    hints = {k: t.hints() for k, t in page.speech_tiles.items()}
    assert hints["local"] == ["Whisper Small · Local · Balanced"]
    assert hints["cloud"] == ["Fast · Cloud · nothing to download"]
    assert hints["own_key"] == ["Mistral or Google Gemini"]
    app.cfg.update(backend="google", google_api_key="AIza-test", google_stt_model="gemini-2.5-pro")
    page.refresh()
    assert page.speech_tiles["own_key"].hints() == ["Gemini 2.5 Pro · your Google key · Cloud"]
    _shot(page.speech_tiles["local"].parentWidget().window(), "settings_speech_and_ai")


def test_onboardings_cloud_card_says_cloud(qapp):
    from ui import onboarding_v2
    card = onboarding_v2.CloudCard()
    assert card.model_label.text() == "Fast · Cloud"
    assert "Fast · Cloud" in card.accessibleName()
    _shot(card, "onboarding_cloud_card")
