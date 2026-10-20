"""The Live Assistant card, drawn small: the card on the "You see / They see" picture of the
Live Assistant page (ui/pages/live_assistant.py, class ScreensPicture).

It is a miniature of the REAL floating card (ui/live_assist.py, class LiveAssistOverlay), in
the state you meet it in during a call: the title, the green timer and the stop button, the
Hidden chip, the screen button, the theme / collapse / close buttons; the session context; the
live transcript; ANSWER with its quick buttons (Answer, Follow-ups, Solve screen, Auto) and a
"Say:" answer; the ask box with the screenshot and send buttons; the footer. Everything is laid
out in the REAL card's units (a 480 wide card, its own paddings, spacings, radii, font sizes
and colours of the light glass, or of the dark one: the page's "Card look") and then drawn at
CARD_SCALE, so the proportions are the real ones. Nothing here is a widget: one painter call
draws it, and the picture caches the result.

    paint_card(painter, x, y, word, tone, icon, backdrop=image_or_None, look="light" | "dark")

The look follows ui/live_assist.py (``_THEMES``, ``chip_colors``, ``_IconButton``,
``_send_icon``, ``_lock_pixmap``, the paintEvent's glass); tests/test_live_demo_picture.py
checks the colours and the words against it, so a change there shows up as a failing test
here. Importing ui.live_assist from here would pull the whole controller in for a picture.
"""
import math
from collections import namedtuple

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import (QBrush, QColor, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath,
                           QPen, QRadialGradient, QTextDocument)

from ui import theme

# ── the card's own units (ui/live_assist.py: CARD_W, RADIUS, the root layout's margins) ──
CARD_W = 480
RADIUS = 22
MARGIN_X = 14                   # the root layout: 14 left and right ...
TOP, BOTTOM = 10, 12            # ... 10 above the bar, 12 under the last row
GAP = 8                         # between the bar, the context, the boxes and the rows
BAR_H = 30                      # the 26 px buttons + the bar's 2 + 2
BTN = 26                        # the header's round buttons
CONTEXT_H = 26
ROW_H = 22                      # the ANSWER row's buttons
ASK_H = 34                      # the ask row's round buttons
INPUT_H = 30
TRANSCRIPT_H = 58              # two lines of the live transcript
ANSWER_H = 46                   # one line of answer (the real box takes the rest of the card)
CARD_SCALE = 0.75             # how small the miniature is drawn, against the real card

# ── what it says (the real card's words where they are fixed; the call is an example) ──
TITLE = "Live Assistant"
TIMER = "● 14:02"
CONTEXT_TAG = "LOCKED IN"
CONTEXT_TEXT = "Call with Acme about the Q4 roadmap. Keep answers short."
CONTEXT_EDIT = "Edit"
TRANSCRIPT_HEAD = "LIVE TRANSCRIPT"
TRANSCRIPT = "…so we are aligned on the scope for October. When will the Q4 roadmap be ready to share?"
ANSWER_HEAD = "ANSWER"
QUICK = ("Answer", "Follow-ups", "Solve screen")
AUTO = "Auto"
ANSWER_HTML = "<b>Say:</b> “It’s ready for 14 October if we freeze features on Friday.”"
ASK_PLACEHOLDER = "Ask anything about the call or your screen…"
FOOTER = "Answered 14:31:07 · Gusan Cloud · first words 0.8s · AI can be wrong - check facts"

# ── the light glass (ui/live_assist.py _THEMES["light"] and _CHIP_COLORS["light"]) ──
INK = QColor("#0f172a")
FAINT = QColor("#64748b")
ACCENT = QColor("#2563eb")
TINT_LIQUID = QColor(214, 222, 233, 165)       # over the blurred backdrop
TINT_FLAT = QColor(206, 215, 227, 242)         # the plate where the backdrop can't be sampled
BORDER = QColor(15, 23, 42, 40)
FIELD = QColor(255, 255, 255, 184)             # rgba(255,255,255,0.72)
FIELD_BORDER = QColor(15, 23, 42, 26)          # rgba(15,23,42,0.10)
BUTTON_BORDER = QColor(15, 23, 42, 34)
CHIPS = {   # tone -> (background, text, border)
    "ok": ("rgba(34,197,94,0.16)", "#15803d", "rgba(34,197,94,0.45)"),
    "red": ("rgba(239,68,68,0.14)", "#b91c1c", "rgba(239,68,68,0.45)"),
    "amber": ("rgba(245,158,11,0.16)", "#b45309", "rgba(245,158,11,0.5)"),
}
SEND_FROM, SEND_TO = QColor("#3b82f6"), QColor("#8b5cf6")

# Everything that changes with the card's look (the page's "Card look": Light / Dark). The names above
# are the light glass; PALETTES holds both, as ui/live_assist.py has them (_THEMES, _CHIP_COLORS and the
# paintEvent's highlight, sheen and rim alphas).
Palette = namedtuple("Palette", "name ink faint accent tint_liquid tint_flat border field field_border "
                                "button_fill button_border chips spec sheen rim")
LIGHT = Palette("light", INK, FAINT, ACCENT, TINT_LIQUID, TINT_FLAT, BORDER, FIELD, FIELD_BORDER,
                QColor(255, 255, 255, 58), BUTTON_BORDER, CHIPS, 95, 60, (235, 60, 120))
DARK = Palette("dark", QColor("#f8fafc"), QColor("#94a3b8"), QColor("#60a5fa"),
               QColor(17, 24, 39, 160), QColor(15, 20, 33, 240), QColor(255, 255, 255, 46),
               QColor(17, 24, 39, 158),                       # rgba(17,24,39,0.62)
               QColor(255, 255, 255, 36),                     # rgba(255,255,255,0.14)
               QColor(255, 255, 255, 26), QColor(255, 255, 255, 50),
               {"ok": ("rgba(34,197,94,0.16)", "#4ade80", "rgba(34,197,94,0.45)"),
                "red": ("rgba(239,68,68,0.18)", "#fca5a5", "rgba(239,68,68,0.5)"),
                "amber": ("rgba(245,158,11,0.18)", "#fcd34d", "rgba(245,158,11,0.5)")},
               45, 26, (150, 35, 70))
PALETTES = {"light": LIGHT, "dark": DARK}

Layout = namedtuple("Layout", "height rects texts")


def palette(name):
    """The palette of a card look ("light" for any name it does not know)."""
    return PALETTES.get(name, LIGHT)


def _font(px, weight=400, spacing=None):
    return theme.font(px, weight, letter_spacing=spacing)


def _adv(text, px, weight=400, spacing=None):
    return QFontMetricsF(_font(px, weight, spacing)).horizontalAdvance(text)


def _height(px, weight=400):
    return QFontMetricsF(_font(px, weight)).height()


def _chip_look(tone, pal=LIGHT):
    bg, tx, edge = pal.chips.get(tone, pal.chips["amber"])
    return theme.qcolor(bg), QColor(tx), theme.qcolor(edge)


# ── the layout, in the real card's units ──
_LAYOUTS = {}


def layout(word="Hidden"):
    """Where everything is, in the real card's units (a 480 wide card). ``rects`` names
    each element (the quick buttons are ``quick0``..); ``texts`` is what is written where.
    Worked out once per word and font (the picture asks for it on every rebuild)."""
    key = (word, theme.body_family())
    lay = _LAYOUTS.get(key)
    if lay is None:
        lay = _LAYOUTS[key] = _layout(word)
    return lay


def _layout(word):
    r, texts = {}, {}
    x0, x1 = MARGIN_X, CARD_W - MARGIN_X
    cy = TOP + BAR_H / 2.0

    def centred(x, w, h):
        return QRectF(x, cy - h / 2.0, w, h)

    # the bar: title, timer, stop, Hidden chip, screen ... theme, collapse, close
    right = x1
    for name in ("close", "collapse", "theme"):
        right -= BTN
        r[name] = centred(right, BTN, BTN)
        right -= GAP
    x = x0 + 4
    timer_w = _adv(TIMER, 12, 700) + 22
    stop = BTN
    chip_w = 7 + 16 + 4 + _adv(word, 12, 600) + 10 + 2
    rest = GAP + timer_w + GAP + stop + GAP + chip_w + GAP + BTN        # everything after the title, up to the screen button
    # (a long chip - "May be hidden" - leaves the stretch between the two groups only a hair: the title
    # keeps its whole name rather than the "Live Assis..." an elided label would show)
    title_w = min(_adv(TITLE, 13, 700) + 2, max(30.0, right + GAP - 2 - x - rest))
    r["title"] = centred(x, title_w, 20)
    x += title_w + GAP
    r["timer"] = centred(x, timer_w, _height(12, 700) + 6)
    x += timer_w + GAP
    r["stop"] = centred(x, stop, BTN)
    x += stop + GAP
    r["chip"] = centred(x, chip_w, max(16, _height(12, 600)) + 6)
    x += chip_w + GAP
    r["screen"] = centred(x, BTN, BTN)
    # the session context
    y = TOP + BAR_H + GAP
    r["context"] = QRectF(x0, y, x1 - x0, CONTEXT_H)
    y += CONTEXT_H + GAP
    # the live transcript
    r["transcript_head"] = QRectF(x0 + 1, y, 200, _height(10, 700))
    y += r["transcript_head"].height() + GAP
    r["transcript"] = QRectF(x0, y, x1 - x0, TRANSCRIPT_H)
    y += TRANSCRIPT_H + GAP
    # the answer's header row: its label, the quick buttons and Auto
    r["answer_head"] = QRectF(x0 + 1, y, 100, ROW_H)
    bx = x1
    for i, label in reversed(list(enumerate(QUICK + (AUTO,)))):
        w = _adv(label, 12) + 24
        bx -= w
        r["auto" if label == AUTO else "quick%d" % i] = QRectF(bx, y, w, ROW_H)
        bx -= 9
    y += ROW_H + GAP
    r["answer"] = QRectF(x0, y, x1 - x0, ANSWER_H)
    y += ANSWER_H + GAP
    # the ask row: the box, the screenshot button, the round send button
    send_x = x1 - ASK_H
    snip_x = send_x - GAP - ASK_H
    r["send"] = QRectF(send_x, y, ASK_H, ASK_H)
    r["snip"] = QRectF(snip_x, y, ASK_H, ASK_H)
    r["ask"] = QRectF(x0, y + (ASK_H - INPUT_H) / 2.0, snip_x - GAP - x0, INPUT_H)
    y += ASK_H + GAP
    # the footer: one line of the answer's note, two when it is long
    fm = QFontMetricsF(_font(11))
    foot = fm.boundingRect(QRectF(0, 0, x1 - x0, 1000), int(Qt.TextWordWrap), FOOTER)
    r["footer"] = QRectF(x0 + 1, y, x1 - x0 - 1, min(32.0, math.ceil(foot.height())))
    y += r["footer"].height() + BOTTOM
    texts.update(title=TITLE, timer=TIMER, chip=word, context_tag=CONTEXT_TAG, context=CONTEXT_TEXT,
                 transcript_head=TRANSCRIPT_HEAD, transcript=TRANSCRIPT, answer_head=ANSWER_HEAD,
                 ask=ASK_PLACEHOLDER, footer=FOOTER)
    return Layout(y, r, texts)


def card_size(scale=CARD_SCALE, word="Hidden"):
    """The card as it is drawn: (width, height) in the painter's units."""
    return QSizeF(CARD_W * scale, layout(word).height * scale)


# ── what is behind the glass ──
def blur(image, passes=3, ratio=8):
    """A cheap blur: the image averaged down and smoothed back up, a few times (the same trick as
    the card's own shadow, ui/live_assist.py _shadow_image)."""
    img = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    w, h = img.width(), img.height()
    k = max(2, int(ratio))
    for _ in range(passes):
        small = img.scaled(max(1, w // k), max(1, h // k), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        img = small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return img


# ── the drawing ──
_DOCS = {}


def _document(html, width, px, weight=400, ink=INK):
    """A text document of the card's text size, wrapped at ``width`` (cached)."""
    key = (html, round(width, 1), px, weight, ink.name())
    doc = _DOCS.get(key)
    if doc is None:
        doc = QTextDocument()
        doc.setDocumentMargin(4)                     # a QTextEdit's own
        doc.setDefaultFont(_font(px, weight))
        doc.setDefaultStyleSheet("body { color: %s; }" % ink.name())
        doc.setHtml("<body>%s</body>" % html)
        doc.setTextWidth(width)
        _DOCS[key] = doc
    return doc


def _box(p, rect, radius, pal=LIGHT):
    p.setPen(QPen(pal.field_border, 1))
    p.setBrush(pal.field)
    p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


def _text(p, rect, text, px, weight=400, color=INK, align=Qt.AlignVCenter | Qt.AlignLeft, spacing=None):
    p.setPen(color)
    p.setFont(_font(px, weight, spacing))
    p.drawText(rect, int(align), text)


def _round_button(p, rect, kind, on=False, pal=LIGHT):
    """A header / ask-row button: ui/live_assist.py _IconButton.paintEvent, on a 26 (or 34) px disc."""
    r = rect.adjusted(1.5, 1.5, -1.5, -1.5)
    fill = pal.accent if on else pal.button_fill
    p.setPen(QPen(fill if on else pal.button_border, 1))
    p.setBrush(fill)
    p.drawEllipse(r)
    ink = QColor("#ffffff") if on else pal.ink
    pen = QPen(ink, 1.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    cx, cy = r.center().x(), r.center().y()
    if kind == "close":
        p.drawLine(QPointF(cx - 3.5, cy - 3.5), QPointF(cx + 3.5, cy + 3.5))
        p.drawLine(QPointF(cx + 3.5, cy - 3.5), QPointF(cx - 3.5, cy + 3.5))
    elif kind == "collapse":
        d = 2.2
        path = QPainterPath(QPointF(cx - 4.2, cy + d))
        path.lineTo(QPointF(cx, cy - d))
        path.lineTo(QPointF(cx + 4.2, cy + d))
        p.drawPath(path)
    elif kind == "theme":
        circle = QRectF(cx - 4.6, cy - 4.6, 9.2, 9.2)
        p.drawEllipse(circle)
        p.setBrush(ink)
        p.setPen(Qt.NoPen)
        p.drawPie(circle, 90 * 16, 180 * 16)
    elif kind == "screen":
        p.drawRoundedRect(QRectF(cx - 5.5, cy - 4.5, 11, 7.5), 1.6, 1.6)
        p.drawLine(QPointF(cx, cy + 3), QPointF(cx, cy + 5.2))
        p.drawLine(QPointF(cx - 3, cy + 5.2), QPointF(cx + 3, cy + 5.2))
    elif kind == "snip":
        s, k = r.width() * 0.22, r.width() * 0.11
        for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            x, y = cx + dx * s, cy + dy * s
            p.drawLine(QPointF(x, y), QPointF(x - dx * k, y))
            p.drawLine(QPointF(x, y), QPointF(x, y - dy * k))
    elif kind == "stop":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ef4444"))
        p.drawRoundedRect(QRectF(cx - 4.5, cy - 4.5, 9, 9), 2.2, 2.2)


def _send_button(p, rect):
    """The round send button: the blue-to-violet disc with the paper plane (_send_icon)."""
    g = QLinearGradient(rect.topLeft(), rect.bottomRight())
    g.setColorAt(0, SEND_FROM)
    g.setColorAt(1, SEND_TO)
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawEllipse(rect)
    p.save()
    p.translate(rect.center().x() - 9, rect.center().y() - 9)
    p.scale(18 / 24.0, 18 / 24.0)
    path = QPainterPath(QPointF(3.4, 20.4))
    path.lineTo(QPointF(21.0, 12.0))
    path.lineTo(QPointF(3.4, 3.6))
    path.lineTo(QPointF(3.4, 10.2))
    path.lineTo(QPointF(15.0, 12.0))
    path.lineTo(QPointF(3.4, 13.8))
    path.closeSubpath()
    p.fillPath(path, QColor("#ffffff"))
    p.restore()


def _lock(p, rect, color):
    """The small padlock of "LOCKED IN" (ui/live_assist.py _lock_pixmap), 12 px."""
    p.save()
    p.translate(rect.topLeft())
    p.scale(rect.width() / 24.0, rect.height() / 24.0)
    pen = QPen(color, 2.6)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    shackle = QPainterPath(QPointF(7.5, 11.5))
    shackle.lineTo(QPointF(7.5, 8.0))
    shackle.arcTo(QRectF(7.5, 3.5, 9.0, 9.0), 180, -180)
    shackle.lineTo(QPointF(16.5, 11.5))
    p.drawPath(shackle)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(4.5, 10.5, 15.0, 11.0), 2.5, 2.5)
    p.restore()


def _chip_icon(p, rect, icon, color):
    """The Hidden chip's mark: the incognito mark while hidden, an eye (slashed: maybe) otherwise,
    as ui/live_assist.py draws it (ui.icons), at the sharpness this painter is going to show it."""
    from ui import icons
    px = max(8, int(round(rect.width() * p.worldTransform().m11() * (p.device().devicePixelRatioF() or 1.0))))
    ratio = icons.incognito_pixmap(16).devicePixelRatio() or 1.0
    size = max(8, int(round(px / ratio)))
    if icon == "incognito":
        pm = icons.incognito_pixmap(size, color)
    else:
        pm = icons.eye_icon(open_=(icon == "eye"), size=size, color=color).pixmap(size, size)
    p.drawPixmap(rect, pm, QRectF(pm.rect()))


def paint_plate(p, rect, backdrop=None, pal=LIGHT):
    """The card's glass, ui/live_assist.py paintEvent: the (blurred) backdrop under the cool tint,
    the cursor's highlight, the top sheen, the rim light and the hairline. ``rect`` is the card."""
    path = QPainterPath()
    path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
    p.save()
    p.setClipPath(path)
    if backdrop is not None:
        p.drawImage(rect, backdrop)
        p.fillPath(path, pal.tint_liquid)
    else:
        p.fillPath(path, pal.tint_flat)
    rad = QRadialGradient(QPointF(rect.left() + 120, rect.top() + 8), rect.width() * 0.55)
    rad.setColorAt(0.0, QColor(255, 255, 255, pal.spec))
    rad.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(path, QBrush(rad))
    sheen = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + 80))
    sheen.setColorAt(0.0, QColor(255, 255, 255, pal.sheen))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(path, QBrush(sheen))
    p.restore()
    p.save()
    p.setClipPath(path)
    rim = QLinearGradient(rect.topLeft(), rect.bottomRight())
    rim.setColorAt(0.0, QColor(255, 255, 255, pal.rim[0]))
    rim.setColorAt(0.45, QColor(255, 255, 255, pal.rim[1]))
    rim.setColorAt(1.0, QColor(255, 255, 255, pal.rim[2]))
    p.setPen(QPen(QBrush(rim), 2.0))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.restore()
    p.setPen(QPen(pal.border, 1))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)


def paint_card(p, x, y, word="Hidden", tone="ok", icon="incognito", scale=CARD_SCALE, backdrop=None,
               look="light"):
    """Draw the card with its top-left corner at (x, y) of the painter, ``scale`` times the real size.
    ``word``, ``tone`` and ``icon`` are the Hidden chip's (live_context.hide_status); ``backdrop`` is
    what is behind the glass, cut to exactly the card (blurred here), or None for the flat plate;
    ``look`` is the card's look, "light" or "dark" (the page's Card look)."""
    pal = palette(look)
    lay = layout(word)
    r = lay.rects
    p.save()
    p.translate(x, y)
    p.scale(scale, scale)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    paint_plate(p, QRectF(0, 0, CARD_W, lay.height), blur(backdrop) if backdrop is not None else None, pal)
    # ── the bar ──
    title = QFontMetricsF(_font(13, 700)).elidedText(TITLE, Qt.ElideRight, int(r["title"].width()))
    _text(p, r["title"], title, 13, 700, pal.ink)
    ok_bg, ok_tx, ok_edge = _chip_look("ok", pal)
    timer = r["timer"]
    p.setPen(QPen(ok_edge, 1))
    p.setBrush(ok_bg)
    p.drawRoundedRect(timer.adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)
    _text(p, timer, TIMER, 12, 700, ok_tx, Qt.AlignCenter)
    _round_button(p, r["stop"], "stop", pal=pal)
    bg, tx, edge = _chip_look(tone, pal)
    chip = r["chip"]
    p.setPen(QPen(edge, 1))
    p.setBrush(bg)
    p.drawRoundedRect(chip.adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)
    _chip_icon(p, QRectF(chip.left() + 7, chip.center().y() - 8, 16, 16), icon, tx)
    _text(p, QRectF(chip.left() + 7 + 16 + 4, chip.top(), chip.width() - 27, chip.height()), word, 12, 600, tx)
    _round_button(p, r["screen"], "screen", on=True, pal=pal)
    for name in ("theme", "collapse", "close"):
        _round_button(p, r[name], name, pal=pal)
    # ── the session context ──
    ctx = r["context"]
    _box(p, ctx, 9, pal)
    x = ctx.left() + 9
    _lock(p, QRectF(x, ctx.center().y() - 6, 12, 12), pal.accent)
    x += 12 + 6
    tag_w = _adv(CONTEXT_TAG, 10, 700, 1.0) + 1
    _text(p, QRectF(x, ctx.top(), tag_w, ctx.height()), CONTEXT_TAG, 10, 700, pal.accent, spacing=1.0)
    x += tag_w + 6
    edit_w = _adv(CONTEXT_EDIT, 11.5)
    shown = QFontMetricsF(_font(12)).elidedText(CONTEXT_TEXT, Qt.ElideRight, int(ctx.right() - 9 - edit_w - 6 - x))
    _text(p, QRectF(x, ctx.top(), ctx.right() - 9 - edit_w - 6 - x, ctx.height()), shown, 12, 400, pal.ink)
    _text(p, QRectF(ctx.right() - 9 - edit_w, ctx.top(), edit_w + 1, ctx.height()), CONTEXT_EDIT, 11.5, 400,
          pal.accent)
    # ── the live transcript ──
    _text(p, r["transcript_head"], TRANSCRIPT_HEAD, 10, 700, pal.faint, spacing=1.0)
    box = r["transcript"]
    _box(p, box, 10, pal)
    p.save()
    p.setClipRect(box.adjusted(1, 1, -1, -1))
    p.translate(box.left() + 1 + 6, box.top() + 1 + 6)
    _document(TRANSCRIPT, box.width() - 2 * 7, 13.5, 400, pal.ink).drawContents(p)
    p.restore()
    # ── the answer ──
    _text(p, r["answer_head"], ANSWER_HEAD, 10, 700, pal.faint, spacing=1.0)
    for i, label in enumerate(QUICK):
        b = r["quick%d" % i]
        _box(p, b, 11, pal)
        _text(p, b, label, 12, 400, pal.ink, Qt.AlignCenter)
    auto = r["auto"]
    p.setPen(QPen(pal.accent, 1))
    p.setBrush(pal.accent)
    p.drawRoundedRect(auto.adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)
    _text(p, auto, AUTO, 12, 400, QColor("#ffffff"), Qt.AlignCenter)
    box = r["answer"]
    _box(p, box, 10, pal)
    p.save()
    p.setClipRect(box.adjusted(1, 1, -1, -1))
    p.translate(box.left() + 1 + 6, box.top() + 1 + 6)
    _document(ANSWER_HTML, box.width() - 2 * 7, 13.5, 400, pal.ink).drawContents(p)
    p.restore()
    # ── the ask row ──
    ask = r["ask"]
    _box(p, ask, 9, pal)
    _text(p, QRectF(ask.left() + 11, ask.top(), ask.width() - 22, ask.height()), ASK_PLACEHOLDER, 12.5, 400,
          QColor(pal.ink.red(), pal.ink.green(), pal.ink.blue(), 140))
    _round_button(p, r["snip"], "snip", pal=pal)
    _send_button(p, r["send"])
    # ── the footer ──
    foot = r["footer"]
    p.setPen(pal.faint)
    p.setFont(_font(11))
    p.drawText(foot, int(Qt.AlignTop | Qt.AlignLeft | Qt.TextWordWrap), FOOTER)
    p.restore()


def card_pixmap(word="Hidden", tone="ok", icon="incognito", scale=CARD_SCALE, dpr=1.0, backdrop="#ffffff",
                look="light"):
    """The miniature on its own, on a transparent pixmap (for renders and tests): the glass over a
    plain backdrop of one colour, or the flat plate when ``backdrop`` is None."""
    from PySide6.QtGui import QPixmap
    size = card_size(scale, word)
    pm = QPixmap(max(1, int(math.ceil(size.width() * dpr))), max(1, int(math.ceil(size.height() * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    back = None
    if backdrop is not None:
        back = QImage(max(1, int(round(size.width() * dpr))), max(1, int(round(size.height() * dpr))),
                      QImage.Format_ARGB32_Premultiplied)
        back.fill(QColor(backdrop))
    p = QPainter(pm)
    paint_card(p, 0, 0, word, tone, icon, scale, back, look)
    p.end()
    return pm
