"""The Live Assistant card inside the page's "You see / They see" picture (ui/pages/live_demo.py,
drawn by ui/pages/live_assistant.py ScreensPicture): a miniature of the REAL floating card
(ui/live_assist.py), floating at the centre-right of the shared screen so that the slide's title and
chart stay in view next to the card's own text.

What is checked: the words and colours are the real card's; the layout has the real card's
paddings, button sizes and row order (the real card is built offscreen and measured); the
miniature draws every part of the card - the buttons, the quick actions, the ask row with its
icons - and nothing of the idle state ("Start", PRO); and on the glass the card sits where it
should and never covers the slide's own words or chart."""
import math
from unittest import mock

import pytest
from PySide6.QtCore import QObject, QPoint, QRectF, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import live_context
from ui.pages import live_assistant as la
from ui.pages import live_demo as demo

WORDS = ("Hidden", "May be hidden", "Visible")


# ── fixtures ──
class _App(QObject):
    sig_setting_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.cfg = {}

    def schedule_config_save(self):
        pass

    def save_config(self):
        pass

    def track(self, event, props=None):
        pass

    def _pro_upsell(self, feature=None):
        pass


@pytest.fixture
def real_card(qapp):
    """The real card, in a live call (timer and stop button), with a context, laid out offscreen."""
    import time
    from ui import glass, live_assist
    made = []
    with mock.patch.object(glass, "capture_exclusion_supported", return_value=True), \
            mock.patch.object(glass, "is_remote_session", return_value=False), \
            mock.patch.object(glass, "exclude_from_capture", return_value=True), \
            mock.patch.object(glass, "is_excluded_from_capture", return_value=True), \
            mock.patch.object(glass, "macos_26_or_later", return_value=False):
        card = live_assist.LiveAssistOverlay(main_app=_App())
        made.append(card)
        card._meeting_active = True
        card._live_since = time.time() - 842
        card._context = demo.CONTEXT_TEXT
        card._refresh_live_controls()
        card.txt_summary.setPlainText(demo.TRANSCRIPT)
        card.lbl_status.setText("Answered 14:31:07")
        card.show()
        QApplication.processEvents()
        yield card
    for c in made:
        c.hide()
        c.deleteLater()
    QApplication.processEvents()


@pytest.fixture
def spy(monkeypatch):
    """Every string QPainter.drawText is given while a picture is painted."""
    from PySide6.QtGui import QPainter
    texts = []

    class Recording(QPainter):
        def drawText(self, *a):
            texts.extend(v for v in a if isinstance(v, str))
            return super().drawText(*a)

    monkeypatch.setattr(la, "QPainter", Recording)
    return texts


@pytest.fixture
def pic(qapp, monkeypatch):
    monkeypatch.setattr(la.ScreensPicture, "AUTOPLAY", False)
    monkeypatch.setattr(la, "_animations_on", lambda: True)
    made = []

    def build(key="on", width=la.DESIGN_W):
        w = la.ScreensPicture()
        w.set_owner("Aram")
        wanted, kw = {"on": (True, dict(supported=True, remote=False, mac=False)),
                      "partial": (True, dict(supported=False, remote=False, mac=True, macos26=False)),
                      "off": (False, dict(supported=True, remote=False, mac=False))}[key]
        w.set_status(live_context.hide_status(wanted, **kw))
        w.resize(width, w.heightForWidth(width))
        w.show()
        QApplication.processEvents()
        made.append(w)
        return w

    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def _where(card, widget):
    """A real widget's rectangle in the card's own units (the window's shadow margin taken off)."""
    from ui import live_assist
    p = widget.mapTo(card, QPoint(0, 0))
    return QRectF(p.x() - live_assist.SHADOW, p.y() - live_assist.SHADOW, widget.width(), widget.height())


# ═════════════════════ it is the real card's look ═════════════════════
def test_the_card_is_the_real_cards_size_and_corner(qapp):
    from ui import live_assist
    assert demo.CARD_W == live_assist.CARD_W == 480
    assert demo.RADIUS == live_assist.RADIUS == 22
    assert 0.75 <= demo.CARD_SCALE <= 0.85           # drawn small, but not too small to read


def test_the_colours_are_the_light_glass_of_the_real_card(qapp):
    from ui import live_assist
    t = live_assist._THEMES["light"]
    assert demo.INK == QColor(t["text"]) and demo.FAINT == QColor(t["faint"]) and demo.ACCENT == QColor(t["accent"])
    assert demo.TINT_LIQUID == t["tint_liquid"] and demo.TINT_FLAT == t["tint_flat"] and demo.BORDER == t["border"]
    from ui import theme
    assert demo.FIELD == theme.qcolor(t["card"]) and demo.FIELD_BORDER == theme.qcolor(t["card_border"])
    for tone in ("ok", "red", "amber"):
        bg, tx, edge = live_assist.chip_colors(tone, "light")
        assert demo.CHIPS[tone] == (bg, tx, edge), tone
    assert demo.CHIPS["ok"][1] == "#15803d"           # the timer's green too


def test_the_words_are_the_real_cards(real_card):
    c = real_card
    assert demo.TITLE == c.lbl_title.text()
    assert demo.TRANSCRIPT_HEAD == c.lbl_sum_head.text()
    assert demo.ANSWER_HEAD == c.lbl_sug_head.text()
    assert list(demo.QUICK) == [b.text() for b in c.quick_buttons]
    assert demo.AUTO == c.btn_auto.text()
    assert demo.ASK_PLACEHOLDER == c.input_ask.placeholderText()
    assert demo.CONTEXT_TAG == c.ctx_chip.tag.text() == "LOCKED IN"
    assert demo.CONTEXT_EDIT == c.ctx_chip.edit_hint.text()
    assert demo.TIMER.startswith(c.lbl_timer.text()[:2])        # "● mm:ss"
    assert len(demo.TIMER.split()[1]) == len(c.lbl_timer.text().split()[1])
    footer = c._footer_text(None, 0.8)
    assert footer.startswith("Answered ") and footer.endswith("AI can be wrong - check facts")
    assert demo.FOOTER.startswith("Answered ") and demo.FOOTER.endswith("AI can be wrong - check facts")
    assert "Gusan Cloud" in demo.FOOTER and "first words" in demo.FOOTER
    assert c.btn_start.isHidden() and not c.btn_stop.isHidden()  # the live state: no Start, no PRO, a stop button


def test_the_answer_says_say_and_the_same_thing_as_the_page(qapp):
    assert demo.ANSWER_HTML.startswith("<b>Say:</b>") and "14 October" in demo.ANSWER_HTML
    assert la.ANSWER_HTML == demo.ANSWER_HTML


def test_the_real_cards_buttons_are_the_miniatures(real_card):
    """Positions and sizes of the parts that do not depend on the font, in the card's own units."""
    c, lay = real_card, demo.layout("Hidden").rects
    for name, widget in (("theme", c.btn_theme), ("collapse", c.btn_collapse), ("close", c.btn_close),
                         ("stop", c.btn_stop), ("screen", c.btn_screen)):
        real, mini = _where(c, widget), lay[name]
        assert (real.width(), real.height()) == (mini.width(), mini.height()) == (demo.BTN, demo.BTN), name
        assert abs(real.center().y() - mini.center().y()) <= 1.0, name
    for name, widget in (("theme", c.btn_theme), ("collapse", c.btn_collapse), ("close", c.btn_close)):
        assert abs(_where(c, widget).left() - lay[name].left()) <= 1.0, name       # the right group is right-aligned
    assert abs(_where(c, c.btn_close).right() - (demo.CARD_W - demo.MARGIN_X)) <= 1.0
    assert abs(_where(c, c.lbl_title).left() - (lay["title"].left() - 4)) <= 6.0    # the label's own padding
    # the session context: a 26 px strip across the card
    chip, mini = _where(c, c.ctx_chip), lay["context"]
    assert (chip.left(), chip.width(), chip.height()) == (mini.left(), mini.width(), demo.CONTEXT_H)
    assert abs(chip.top() - mini.top()) <= 1.0
    # the ask row: the 34 px screenshot and send buttons on the right, the box before them
    send, snip, ask = _where(c, c.btn_suggest), _where(c, c.btn_snip), _where(c, c.input_ask)
    assert (send.width(), send.height(), snip.width(), snip.height()) == (demo.ASK_H,) * 4
    assert abs(send.right() - (demo.CARD_W - demo.MARGIN_X)) <= 1.0
    assert abs(send.left() - lay["send"].left()) <= 1.0 and abs(snip.left() - lay["snip"].left()) <= 1.0
    assert abs(ask.left() - lay["ask"].left()) <= 1.0 and abs(ask.right() - lay["ask"].right()) <= 1.0
    # the ANSWER row's buttons are 22 px tall and end at the card's right margin
    assert c.btn_auto.height() == demo.ROW_H and all(b.height() == demo.ROW_H for b in c.quick_buttons)
    assert abs(_where(c, c.btn_auto).right() - lay["auto"].right()) <= 1.0


def test_the_rows_come_in_the_real_cards_order(real_card):
    c = real_card
    real = [_where(c, w).top() for w in (c.bar, c.ctx_chip, c.lbl_sum_head, c.txt_summary, c.btn_auto,
                                         c.txt_suggestion, c.input_ask, c.lbl_status)]
    lay = demo.layout().rects
    mini = [demo.TOP] + [lay[k].top() for k in ("context", "transcript_head", "transcript", "auto", "answer", "ask",
                                                 "footer")]
    assert real == sorted(real) and mini == sorted(mini)
    for r, m in zip(real[:4], mini[:4]):               # down to the transcript's top the heights are fixed too
        # (below it the real card's transcript box is as tall as the 560 px card leaves it, the miniature's is two lines)
        assert abs(r - m) <= 4.0, (r, m)
    assert abs(_where(c, c.bar).top() - demo.TOP) <= 1.0


# ═════════════════════ the layout ═════════════════════
@pytest.mark.parametrize("word", WORDS)
def test_everything_is_inside_the_card_and_nothing_collides(qapp, word):
    lay = demo.layout(word)
    card = QRectF(0, 0, demo.CARD_W, lay.height)
    for name, r in lay.rects.items():
        assert r.width() > 0 and r.height() > 0, name
        assert card.contains(r), (name, r)
    header = ["title", "timer", "stop", "chip", "screen", "theme", "collapse", "close"]
    for a, b in zip(header, header[1:]):
        assert lay.rects[a].right() <= lay.rects[b].left() + 1e-6, (a, b)     # in a row, left to right
    quick = [lay.rects["quick%d" % i] for i in range(len(demo.QUICK))] + [lay.rects["auto"]]
    for a, b in zip(quick, quick[1:]):
        assert a.right() < b.left()
    assert lay.rects["answer_head"].right() < quick[0].left()
    stacked = ["title", "context", "transcript_head", "transcript", "answer_head", "answer", "ask", "footer"]
    for a, b in zip(stacked, stacked[1:]):
        assert lay.rects[a].bottom() <= lay.rects[b].top() + 1e-6, (a, b)
    assert lay.rects["snip"].right() < lay.rects["send"].left() and lay.rects["ask"].right() < lay.rects["snip"].left()
    assert lay.height == pytest.approx(lay.rects["footer"].bottom() + demo.BOTTOM)


def test_the_title_keeps_its_whole_name_even_beside_the_longest_chip(qapp):
    for word in WORDS:
        title = demo.layout(word).rects["title"]
        assert title.width() >= demo._adv(demo.TITLE, 13, 700), word


def test_the_card_is_the_same_height_for_every_word(qapp):
    assert len({demo.layout(w).height for w in WORDS}) == 1
    size = demo.card_size()
    assert size.width() == pytest.approx(demo.CARD_W * demo.CARD_SCALE)
    assert size.height() == pytest.approx(demo.layout().height * demo.CARD_SCALE)


# ═════════════════════ what is drawn ═════════════════════
def test_a_card_draws_every_part_of_the_live_card(pic, spy):
    w = pic("on")
    w.grab()
    for text in (demo.TITLE, demo.TIMER, "Hidden", demo.CONTEXT_TAG, demo.CONTEXT_EDIT, demo.TRANSCRIPT_HEAD,
                 demo.ANSWER_HEAD, *demo.QUICK, demo.AUTO, demo.ASK_PLACEHOLDER, demo.FOOTER):
        assert text in spy, text
    assert any(demo.CONTEXT_TEXT.startswith(t.rstrip("…")) for t in spy)     # (elided to its strip)
    for idle in ("Start", "PRO", "Pro"):               # a call is on: no Start button, no PRO tag
        assert idle not in spy


@pytest.mark.parametrize("key", ["on", "partial", "off"])
def test_the_chip_says_the_pages_one_word(pic, spy, key):
    w = pic(key)
    w.grab()
    word = la.STATUS_WORDS.get(key, la.STATUS_WORD_DEFAULT)
    assert spy.count(word) == (1 if key == "on" else 2)    # "you" side always, "they" side when the card shows
    assert spy.count(demo.TITLE) == (1 if key == "on" else 2)


def _card_image(word="Hidden", backdrop="#ffffff", scale=2.0):
    return demo.card_pixmap(word, "ok", "incognito", demo.CARD_SCALE, scale, backdrop).toImage()


def test_the_icons_are_drawn_not_circles(qapp):
    """The header's round buttons carry their glyphs (screen, theme, collapse, close), the stop button its red
    square, the ask row its viewfinder and the paper plane."""
    img = _card_image()
    lay = demo.layout().rects
    k = demo.CARD_SCALE * 2.0

    def inked(name, dark=140):
        r = lay[name]
        n = 0
        for x in range(int(r.left() * k) + 3, int(r.right() * k) - 3):
            for y in range(int(r.top() * k) + 3, int(r.bottom() * k) - 3):
                c = img.pixelColor(x, y)
                if c.lightness() < dark:
                    n += 1
        return n

    for name in ("theme", "collapse", "close", "snip"):
        assert inked(name) > 8, name                               # a dark glyph on the glass disc
    red = [img.pixelColor(int(lay["stop"].center().x() * k), int(lay["stop"].center().y() * k))]
    assert red[0].red() > 200 and red[0].green() < 120             # the red stop square
    scr = img.pixelColor(int((lay["screen"].left() + 4) * k), int(lay["screen"].center().y() * k))
    assert scr.blue() > 180 and scr.red() < 90                     # the screen button is on: blue
    send = lay["send"]
    disc = img.pixelColor(int((send.left() + 5) * k), int(send.center().y() * k))
    assert disc.blue() > 200 and disc.red() < 150                  # the blue-violet disc ...
    whites = sum(1 for x in range(int(send.left() * k), int(send.right() * k))
                 for y in range(int(send.top() * k), int(send.bottom() * k))
                 if img.pixelColor(x, y).lightness() > 235)
    assert whites > 20                                             # ... with the white plane on it


def test_the_chip_mark_follows_the_status(qapp):
    green = demo.card_pixmap("Hidden", "ok", "incognito", demo.CARD_SCALE, 2.0).toImage()
    amber = demo.card_pixmap("May be hidden", "amber", "eye_off", demo.CARD_SCALE, 2.0).toImage()
    red = demo.card_pixmap("Visible", "red", "eye", demo.CARD_SCALE, 2.0).toImage()
    for img, word, hue in ((green, "Hidden", "g"), (amber, "May be hidden", "a"), (red, "Visible", "r")):
        chip = demo.layout(word).rects["chip"]
        k = demo.CARD_SCALE * 2.0
        c = img.pixelColor(int((chip.right() - 4) * k), int(chip.center().y() * k))      # the chip's fill, past its text
        if hue == "g":
            assert c.green() > c.red() and c.green() > c.blue() - 1
        elif hue == "a":
            assert c.red() > c.blue()
        else:
            assert c.red() > c.green()


def test_the_glass_is_the_cards_plate(qapp):
    flat = _card_image(backdrop=None)
    glass = _card_image(backdrop="#ffffff")
    assert flat.width() == glass.width() and flat.height() == glass.height()
    c = QColor(flat.pixelColor(flat.width() // 2, 30))       # between header rows: the plate itself
    assert c.alpha() > 200
    # the corners are rounded: the very corner is empty
    assert flat.pixelColor(0, 0).alpha() == 0 and flat.pixelColor(flat.width() - 1, flat.height() - 1).alpha() == 0
    # over a white backdrop the liquid glass is lighter than the flat plate, and both are cool slate
    g, f = glass.pixelColor(glass.width() // 2, 30), flat.pixelColor(flat.width() // 2, 30)
    assert g.lightness() > f.lightness() and g.blue() > g.red() and f.blue() > f.red()


# ═════════════════════ on the glass ═════════════════════
def test_the_card_floats_centre_right_inside_the_glass(pic):
    w = pic("on")
    r, inner = w.card_rect(), w._inner_rect()
    assert inner.contains(r)
    assert r.right() == pytest.approx(inner.right() - la.CARD_GAP_RIGHT)
    centre = (r.center().x() - inner.left()) / inner.width()
    assert 0.55 < centre < 0.80                                     # not at the edge, not the middle: centre-right
    assert abs(r.center().y() - inner.center().y()) < 20            # level with the middle of the screen
    assert r.top() - inner.top() > 40                               # the banners keep the top ...
    assert inner.bottom() - r.bottom() > 20                         # ... and it stays clear of the bottom edge
    assert (r.width(), r.height()) == pytest.approx((demo.card_size().width(), demo.card_size().height()))


def test_the_card_does_not_reach_the_slides_words_or_chart(pic):
    w = pic("on")
    inner = w._inner_rect()
    assert w.card_rect().left() >= inner.left() + (la.SLIDE_KEEP + 6) * la.K     # the title, lines and chart end first


def test_the_slides_title_and_chart_are_the_same_on_both_sides(pic):
    """Nothing of the slide that belongs to the left is covered by the card: the title, the lines and the chart
    look the same whichever side of the line they are on, and so does the card's whole height beside them."""
    w = pic("on")
    w.set_split(la.SPLIT_MAX)
    you = w.grab().toImage().convertToFormat(QImage.Format_RGB32)
    w.set_split(la.SPLIT_MIN)
    they = w.grab().toImage().convertToFormat(QImage.Format_RGB32)
    left, top = la.BEZEL, la.LABEL_H + la.LABEL_GAP + la.BEZEL
    x0, x1 = left + 10 * la.K, left + la.SLIDE_KEEP * la.K
    y0, y1 = top + 30 * la.K, top + (la.INNER_H - 14) * la.K          # below the banners, above the glass's foot
    worst = 0
    mid_y = top + la.INNER_H * la.K / 2.0
    for x in range(int(x0), int(x1), 3):
        for y in range(int(y0), int(y1), 3):
            if x < left + 50 and abs(y - mid_y) < 26:              # the handle rests here when the line is at the left end
                continue
            a, b = you.pixelColor(x, y), they.pixelColor(x, y)
            worst = max(worst, abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue()))
    assert worst < 30, worst


def test_the_card_text_is_there_while_the_line_is_in_the_middle(pic, spy):
    """Both the slide (left of the line) and the card (right of it) can be read at once at the line's rest
    when animations are off, and the card's words are painted in full on the glass."""
    w = pic("on")
    w.set_split(la.REST_STATIC)
    w.grab()
    assert demo.ANSWER_HEAD in spy and demo.TITLE in spy


@pytest.mark.parametrize("key", ["on", "partial", "off"])
def test_their_side_has_the_card_when_it_is_not_hidden(pic, key):
    w = pic(key)
    w.set_split(la.SPLIT_MAX)
    you = w.grab().toImage().convertToFormat(QImage.Format_RGB32)
    w.set_split(la.SPLIT_MIN)
    they = w.grab().toImage().convertToFormat(QImage.Format_RGB32)
    r = w.card_rect()
    x, y = int(r.left() + 40), int(r.top() + r.height() * 0.5)
    inside = [(x + dx, y + dy) for dx in range(0, int(r.width()) - 120, 7) for dy in range(0, 40, 5)]

    def mean_diff():
        t = 0
        for px, py in inside:
            a, b = you.pixelColor(px, py), they.pixelColor(px, py)
            t += abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())
        return t / (3.0 * len(inside))

    if key == "on":
        assert mean_diff() > 8                                      # hidden: only yours has the card
    elif key == "off":
        assert mean_diff() < 1                                      # visible: the same card on both
    else:
        assert 1 < mean_diff()                                      # faded: neither


@pytest.mark.parametrize("width", [la.DESIGN_W, 464, 382, 300])
def test_the_picture_scales_down_with_the_card_in_it(pic, width):
    w = pic("on", width=width)
    s = w._scale()
    assert s == pytest.approx(min(1.0, width / float(la.DESIGN_W)), abs=1.0 / la.SCALE_STEPS + 1e-9)
    assert w.height() == w.heightForWidth(width)
    img = w.grab()
    assert img.width() >= width
    assert math.ceil(la.DESIGN_H * s) == w.heightForWidth(width)


def test_the_cache_holds_the_card_in_both_sides(pic):
    w = pic("on")
    w.grab()
    assert {"you", "they", "body", "you_label", "they_label", "handle", "handle_hot"} <= set(w._cache)
    assert w._cache["you"].size() == w._cache["they"].size()


def test_the_card_shadow_is_cheap(pic):
    import time
    w = pic("on")
    w._cache.clear()
    w._cache_key = None
    t = time.perf_counter()
    w.grab()
    first = time.perf_counter() - t
    t = time.perf_counter()
    for f in (0.2, 0.5, 0.8):
        w.set_split(f)
        w.grab()
    again = (time.perf_counter() - t) / 3
    assert first < 3.0 and again < 0.5                              # built once, a frame is a few blits


# ═════════════════════ the card's look: Light / Dark ═════════════════════
def test_the_dark_look_is_the_real_cards_dark_glass(qapp):
    from ui import live_assist, theme
    t, d = live_assist._THEMES["dark"], demo.palette("dark")
    assert d.ink == QColor(t["text"]) and d.faint == QColor(t["faint"]) and d.accent == QColor(t["accent"])
    assert d.tint_liquid == t["tint_liquid"] and d.tint_flat == t["tint_flat"] and d.border == t["border"]
    assert d.field == theme.qcolor(t["card"]) and d.field_border == theme.qcolor(t["card_border"])
    assert d.spec == t["spec"]
    for tone in ("ok", "red", "amber"):
        assert d.chips[tone] == live_assist.chip_colors(tone, "dark"), tone
    # the light one is still the module's own names, and anything else is light
    assert demo.palette("light") is demo.LIGHT and demo.palette("sepia") is demo.LIGHT
    assert demo.palette(None) is demo.LIGHT
    assert demo.LIGHT.ink == demo.INK and demo.LIGHT.accent == demo.ACCENT and demo.LIGHT.chips is demo.CHIPS


def _brightest(img, rect, k):
    return max(img.pixelColor(x, y).lightness()
               for x in range(int(rect.left() * k), int(rect.right() * k))
               for y in range(int(rect.top() * k), int(rect.bottom() * k)))


def _darkest(img, rect, k):
    return min(img.pixelColor(x, y).lightness()
               for x in range(int(rect.left() * k), int(rect.right() * k))
               for y in range(int(rect.top() * k), int(rect.bottom() * k)))


def test_the_dark_card_is_dark_glass_with_light_text(qapp):
    light = demo.card_pixmap("Hidden", "ok", "incognito", demo.CARD_SCALE, 2.0, None, "light").toImage()
    dark = demo.card_pixmap("Hidden", "ok", "incognito", demo.CARD_SCALE, 2.0, None, "dark").toImage()
    assert (light.width(), light.height()) == (dark.width(), dark.height())
    k = demo.CARD_SCALE * 2.0
    plate = (int(7 * k), int(60 * k))                                 # the card's margin beside the context strip
    assert dark.pixelColor(*plate).lightness() < 80 < 200 < light.pixelColor(*plate).lightness()
    lay = demo.layout().rects
    assert _brightest(dark, lay["title"], k) > 200 and _darkest(light, lay["title"], k) < 80   # white on dark, ink on light
    assert _brightest(dark, lay["answer"], k) > 200                   # the answer's text is light too
    chip = lay["chip"]
    inside = dark.pixelColor(int((chip.right() - 4) * k), int(chip.center().y() * k))
    assert inside.green() > inside.red()                              # the Hidden chip stays green
    send = lay["send"]                                                # the same blue-violet send disc
    disc = dark.pixelColor(int((send.left() + 5) * k), int(send.center().y() * k))
    assert disc.blue() > 200 and disc.red() < 150


def test_an_unknown_look_draws_the_light_card(qapp):
    a = demo.card_pixmap("Hidden", "ok", "incognito", demo.CARD_SCALE, 1.0, None, "light").toImage()
    b = demo.card_pixmap("Hidden", "ok", "incognito", demo.CARD_SCALE, 1.0, None, "sepia").toImage()
    assert a == b


def _mean_lightness(img, r):
    x0, y0, x1, y1 = int(r.left() + 12), int(r.top() + 40), int(r.right()) - 12, int(r.bottom()) - 40
    px = [img.pixelColor(x, y).lightness() for x in range(x0, x1, 9) for y in range(y0, y1, 9)]
    return sum(px) / float(len(px))


def test_the_picture_follows_the_card_look(pic):
    w = pic("on")
    assert w.card_look() == "light"
    w.set_split(la.SPLIT_MAX)
    light = w.grab().toImage()
    cache = w._cache
    w.set_card_look("light")
    assert w._cache is cache                                        # the same look again: nothing is rebuilt
    w.set_card_look("dark")
    assert w.card_look() == "dark" and not w._cache                 # a new look: the pictures are drawn again
    dark = w.grab().toImage()
    r = w.card_rect()
    assert _mean_lightness(dark, r) < _mean_lightness(light, r) - 40       # a dark card on the white slide
    assert w.split() == la.SPLIT_MAX                                # the line did not move
    # the slide itself (its title, lines and chart) is the same under either look
    inner = w._inner_rect()
    left = QRectF(inner.left() + 10, inner.top() + 40, (la.SLIDE_KEEP - 14) * la.K, inner.height() - 60)
    assert abs(_mean_lightness(dark, left) - _mean_lightness(light, left)) < 1.0
    w.set_card_look("sepia")
    assert w.card_look() == "light"
    w.set_card_look(None)
    assert w.card_look() == "light"


@pytest.mark.parametrize("key", ["partial", "off"])
def test_the_dark_card_shows_on_their_side_too_when_it_is_not_hidden(pic, key):
    w = pic(key)
    w.set_card_look("dark")
    w.set_split(la.SPLIT_MIN)                                       # all of it is "They see"
    they = w.grab().toImage()
    w.set_card_look("light")
    light = w.grab().toImage()
    r = w.card_rect()
    assert _mean_lightness(they, r) < _mean_lightness(light, r) - 15


@pytest.fixture
def page_for(qapp, monkeypatch):
    """The page for a Pro user, with a writable app: set_setting writes cfg and tells the page, as the app does."""
    from PySide6.QtWidgets import QWidget
    from tests_qt.conftest import make_stub_app
    monkeypatch.setattr(la, "load_sessions", lambda: [])
    monkeypatch.setattr(la.ScreensPicture, "AUTOPLAY", False)
    made = []

    def build(look="light", refuse=False):
        app = make_stub_app("pro")
        app.cfg.update({"live_assist_private": True, "live_assist_theme": look, "live_assist_context": ""})
        app.live_assist = None
        holder = {}

        def set_setting(key, value):
            if refuse:
                return False
            app.cfg[key] = value
            holder["page"].on_setting_changed(key)
            return True
        app.set_setting = set_setting
        host = QWidget()
        from ui import theme
        theme.apply(host)
        page = la.create(app)
        page.setParent(host)
        holder["page"] = page
        host.resize(1048, 1000)
        page.resize(1048, 1000)
        host.show()
        page.on_open()
        QApplication.processEvents()
        made.append(host)
        return page, app

    yield build
    for h in made:
        h.hide()
        h.deleteLater()
    QApplication.processEvents()


def test_the_page_shows_the_card_look_it_was_given(page_for):
    page, app = page_for("dark")
    assert page.look.index() == 1 and page.picture.card_look() == "dark"
    page, app = page_for("light")
    assert page.look.index() == 0 and page.picture.card_look() == "light"
    page, app = page_for("odd")                                    # a value nobody knows: the light card, as the real one
    assert page.picture.card_look() == "light"


def test_pressing_card_look_changes_the_picture_at_once(page_for):
    page, app = page_for("light")
    page.look.buttons()[1].click()
    assert app.cfg["live_assist_theme"] == "dark" and page.picture.card_look() == "dark"
    page.look.buttons()[0].click()
    assert page.picture.card_look() == "light"


def test_a_refused_card_look_leaves_the_picture_as_it_was(page_for):
    page, app = page_for("light", refuse=True)
    page.look.buttons()[1].click()
    assert page.look.index() == 0 and page.picture.card_look() == "light"


def test_the_look_chosen_on_the_card_reaches_the_picture(page_for):
    page, app = page_for("light")
    app.cfg["live_assist_theme"] = "dark"                          # the card's own button wrote it
    page.on_setting_changed("live_assist_theme")
    assert page.look.index() == 1 and page.picture.card_look() == "dark"
