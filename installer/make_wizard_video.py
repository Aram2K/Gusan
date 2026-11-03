"""Draws the VIDEO the Windows installer plays on its Installing page (installer/wizard/gusan_intro.mp4).

The five scenes of make_wizard_art.py (Dictation, Smart Actions, Meetings, Live Assistant, Audio to text) as real
motion: keys that press, a bar that listens, text that types itself, cards that slide in, a progress bar that
fills. 30 frames a second, 1000x400, one loop of 27 s; every scene is 6 s long and two scenes share 0.6 s of
cross-fade, so the last frame leads straight into the first.

Nothing here runs in CI and nothing at install time: run it by hand after a design change and commit the mp4.

    venv\\Scripts\\python.exe installer\\make_wizard_video.py                 # the whole loop
    venv\\Scripts\\python.exe installer\\make_wizard_video.py --sheet out.png # a contact sheet to look at, no video
    venv\\Scripts\\python.exe installer\\make_wizard_video.py --fast          # 2x paint scale: quicker, a bit softer

The look comes from make_wizard_art.py (the same chrome, the same painted pieces); this file only moves them.
The player is installer/player/GusanSlides.cs, the picture slideshow of make_wizard_art.py is the fallback.
"""
import argparse
import math
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_wizard_art as art  # noqa: E402  (sets up Qt and the app's fonts; see there)
from make_wizard_art import (SLIDES, SLIDE_H, SLIDE_OUT, SLIDE_W, SX, SY, SW, SH, Image, QColor, QPointF,  # noqa: E402
                             QPen, QRectF, Qt, glass, icon, keys, pill, q, shadow, text, text_w)

OUT = art.OUT / "gusan_intro.mp4"
FPS = 30
SLIDE_S = 6.0                 # how long one scene lives (cross-fades included)
FADE_S = 0.6                  # how long two scenes share the screen
PERIOD_S = SLIDE_S - FADE_S   # a new scene starts every 5.4 s
LOOP_S = PERIOD_S * len(SLIDES)
BUDGET_BYTES = 8 * 1024 * 1024


# ── motion basics ────────────────────────────────────────────────────────────
def clamp01(x):
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def prog(t, a, b):
    """0 before a, 1 after b, linear between."""
    return clamp01((t - a) / float(b - a))


def ease(x):
    x = clamp01(x)
    return x * x * (3.0 - 2.0 * x)


def ease_out(x):
    x = clamp01(x)
    return 1.0 - (1.0 - x) ** 3


def appear(t, t0, dur=0.3):
    return ease_out(prog(t, t0, t0 + dur))


def bump(t, t0, dur):
    """0 outside [t0, t0+dur], a smooth 0 -> 1 -> 0 hill inside (a key press, a pop)."""
    x = (t - t0) / float(dur)
    return math.sin(math.pi * x) if 0.0 <= x <= 1.0 else 0.0


def pop(t, t0, dur=0.3):
    """A small overshoot 0 -> 1.12 -> 1 (a badge popping in)."""
    x = prog(t, t0, t0 + dur)
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return ease_out(x) + 0.12 * math.sin(math.pi * x)


@contextmanager
def layer(p, a=1.0, dx=0.0, dy=0.0, s=1.0, cx=0.0, cy=0.0):
    """Everything drawn inside is faded by ``a``, moved by (dx, dy) and scaled by ``s`` around (cx, cy)."""
    p.save()
    p.setOpacity(p.opacity() * clamp01(a))
    if dx or dy:
        p.translate(dx, dy)
    if s != 1.0:
        p.translate(cx, cy)
        p.scale(s, s)
        p.translate(-cx, -cy)
    try:
        yield
    finally:
        p.restore()


def typed(s, n):
    return s[: max(0, int(n))]


def typed_lines(lines, n):
    out, left = [], int(max(0, n))
    for ln in lines:
        out.append(ln[:left])
        left = max(0, left - len(ln))
    return out


def wrap_lines(s, px, weight, width):
    words, lines, cur = s.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if not cur or text_w(trial, px, weight) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def caret(p, x, y, h=14.0, color="blue"):
    p.fillRect(QRectF(x, y, 1.3, h), q(color))


def blink(t):
    return int(t * 2.2) % 2 == 0


def mix(c1, c2, k):
    a, b = QColor(c1), QColor(c2)
    return QColor(int(a.red() + (b.red() - a.red()) * k), int(a.green() + (b.green() - a.green()) * k),
                  int(a.blue() + (b.blue() - a.blue()) * k))


# ── the keys and the bar, with the cross-fades between them ───────────────────
def key_bar_row(p, t, cx, by, key_b, t_in, t_press, t_rec, t_trans, t_done, rec_t0, done_state, done_label,
                label=None):
    """Keys Alt+<key_b> appear, one press, then the bar listens (recording), thinks (transcribing) and says it is
    done. All times in seconds of the scene. ``label`` is the Smart Actions wording of the second bar state."""
    cy = by + 18
    a_in = appear(t, t_in, 0.35)
    a_keys = a_in * (1.0 - prog(t, t_rec, t_rec + 0.3))
    if a_keys > 0.004:
        press = bump(t, t_press, 0.22)
        with layer(p, a_keys, dy=(1.0 - a_in) * 8.0, s=1.0 - 0.05 * press, cx=cx, cy=cy):
            keys(p, cx, cy, "Alt", key_b, lit=(t >= t_press + 0.02))
    a_rec = prog(t, t_rec, t_rec + 0.3) * (1.0 - prog(t, t_trans, t_trans + 0.25))
    if a_rec > 0.004:
        with layer(p, a_rec, s=0.94 + 0.06 * prog(t, t_rec, t_rec + 0.3), cx=cx, cy=cy):
            art.bar(p, cx, by, "recording", t=t - rec_t0)
    a_tr = prog(t, t_trans, t_trans + 0.25) * (1.0 - prog(t, t_done, t_done + 0.2))
    if a_tr > 0.004:
        with layer(p, a_tr):
            art.bar(p, cx, by, "transcribing", label, t=t - t_trans)
    a_done = prog(t, t_done, t_done + 0.2)
    if a_done > 0.004:
        with layer(p, a_done, s=0.96 + 0.04 * a_done, cx=cx, cy=cy):
            art.bar(p, cx, by, done_state, done_label)


# ── scene 1: Dictation ───────────────────────────────────────────────────────
MAIL = ["Hi Anna,", "Can we move the review to Tuesday", "at 3? I’ll bring the notes from the call."]


def scene1(p, t):
    n = max(0.0, (t - 4.1) * 95.0)
    lines = typed_lines(MAIL, n)
    win = QRectF(SX, SY, SW, 118)
    art.mail_window(p, win, "Anna", lines, caret=False)
    if 4.1 <= t < 5.25 or blink(t):
        i = max([k for k, ln in enumerate(lines) if ln] or [0])
        x = win.left() + 11 + (text_w(lines[i], 12, 400) + 1.0 if lines[i] else 0.0)
        caret(p, x, win.top() + 53 + i * 17 + 1.5)
    key_bar_row(p, t, SX + SW / 2, 142, "R", t_in=0.15, t_press=1.1, t_rec=1.55, t_trans=3.4, t_done=4.1,
                rec_t0=1.55, done_state="pasted", done_label=None)


# ── scene 2: Smart Actions ───────────────────────────────────────────────────
QUOTE = "“Tell Sam it moved to Thursday. Be polite.”"
REPLY = ["Hi Sam, the demo has moved to Thursday.", "Sorry for the change, and thanks!"]


def scene2(p, t):
    strip = QRectF(SX, 12, SW, 32)
    glass(p, strip, 10, 235, with_shadow=False)
    a_say = prog(t, 1.6, 1.9)
    with layer(p, 1.0 - a_say):
        icon(p, "keyboard", QRectF(strip.left() + 11, strip.center().y() - 8, 16, 16), q("faint"), 1.6)
        text(p, QRectF(strip.left() + 34, strip.top(), strip.width() - 44, strip.height()),
             "Press, say what you want, press again.", 11.5, 400, "muted")
    if a_say > 0.004:
        with layer(p, a_say):
            icon(p, "mic", QRectF(strip.left() + 11, strip.center().y() - 8, 16, 16), q("pro"), 1.8)
            text(p, QRectF(strip.left() + 34, strip.top() + 1, strip.width() - 44, strip.height() - 2),
                 typed(QUOTE, (t - 1.85) * 40.0), 11.5, 400, "text2", italic=True)
    a_chat = appear(t, 0.0, 0.5)
    reply = []
    if t >= 3.9:
        reply = typed_lines(REPLY, (t - 3.9) * 72.0)
    with layer(p, a_chat, dy=(1.0 - a_chat) * 6.0):
        art.chat_window(p, QRectF(SX, 48, SW, 100), "Sam", "Is the demo still on Tuesday?", reply)
    key_bar_row(p, t, SX + SW / 2, 152, "S", t_in=0.15, t_press=1.1, t_rec=1.5, t_trans=3.0, t_done=3.9,
                rec_t0=1.5, done_state="pasted", done_label="Reply pasted", label="Writing your reply")


# ── scene 3: Meetings ────────────────────────────────────────────────────────
ROWS3 = [("A", "Can we move the launch to Friday?"), ("D", "Yes, if QA signs off on Thursday."),
         ("A", "Great. I’ll tell the team today.")]
SUMMARY = "The launch moves to Friday if QA signs off on Thursday."


def scene3(p, t):
    card = QRectF(SX, SY, SW, SH)
    glass(p, card, 16)
    L, R = card.left() + 16, card.right() - 16
    # 1) the record button and what it records
    a1 = 1.0 - prog(t, 1.3, 1.6)
    if a1 > 0.004:
        with layer(p, a1):
            bx, by = card.left() + 54, card.center().y()
            ph = (t * 0.8) % 1.0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(233, 21, 45, int(34 * (1.0 - ph))))
            p.drawEllipse(QPointF(bx, by), 33 + ph * 12, 33 + ph * 12)
            with layer(p, 1.0, s=1.0 - 0.08 * bump(t, 1.0, 0.3), cx=bx, cy=by):
                art.rec_button(p, bx, by, 26)
            pill(p, QRectF(card.left() + 100, 70, 142, 24), "#ffffff", "#cbd5e1")
            icon(p, "headset", QRectF(card.left() + 109, 74, 16, 16), q("muted"), 1.6)
            text(p, QRectF(card.left() + 130, 70, 112, 24), "Both sides of the call", 10.5, 600, "text2")
            text(p, QRectF(card.left() + 100, 100, 160, 34), "Let everyone know you’re recording.", 10.5, 400,
                 "muted", Qt.AlignLeft | Qt.AlignTop, wrap=True)
    # 2) the live call: the clock, the words, then the notes being written
    a2 = prog(t, 1.3, 1.6) * (1.0 - prog(t, 3.5, 3.8))
    if a2 > 0.004:
        with layer(p, a2):
            k_note = prog(t, 3.05, 3.25)
            with layer(p, 1.0 - k_note):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(233, 21, 45, int(255 * (0.55 + 0.45 * (0.5 + 0.5 * math.sin(t * 6.0))))))
                p.drawEllipse(QPointF(L + 5, card.top() + 25), 4.5, 4.5)
                text(p, QRectF(L + 16, card.top() + 14, 120, 22), "00:%02d" % min(12, int((t - 1.3) * 6.5)), 12,
                     700, "ink", tabular=True)
            if k_note > 0.004:
                with layer(p, k_note):
                    art.spinner(p, L + 6, card.top() + 25, 13, "meet", t - 3.05)
                    text(p, QRectF(L + 20, card.top() + 14, 150, 22), "Writing notes", 12, 700, "ink")
            p.fillRect(QRectF(L, card.top() + 42, card.width() - 32, 1), q("line"))
            k_av = prog(t, 3.1, 3.4)
            for i, (who, line) in enumerate(ROWS3):
                t0 = 1.55 + i * 0.5
                ap = appear(t, t0, 0.3)
                if ap < 0.004:
                    continue
                y = card.top() + 56 + i * 34
                with layer(p, ap, dy=(1.0 - ap) * 6.0):
                    with layer(p, 1.0 - k_av):
                        p.setPen(Qt.NoPen)
                        p.setBrush(q("line2"))
                        p.drawEllipse(QPointF(L + 10, y + 12), 3, 3)
                    if k_av > 0.004:
                        with layer(p, k_av, s=0.7 + 0.3 * ease_out(k_av), cx=L + 10, cy=y + 12):
                            art.avatar(p, L + 10, y + 12, 20, who)
                    text(p, QRectF(L + 28, y, R - L - 28, 24), typed(line, (t - t0) * 48.0), 12, 400, "text")
    # 3) the notes
    a3 = prog(t, 3.5, 3.8)
    if a3 > 0.004:
        with layer(p, a3):
            art.avatar(p, L + 10, card.top() + 23, 20, "A")
            art.avatar(p, L + 26, card.top() + 23, 20, "D")
            text(p, QRectF(L + 44, card.top() + 12, 130, 22), "Launch plan", 12.5, 700, "ink")
            text(p, QRectF(R - 60, card.top() + 12, 60, 22), "12 min", 11, 400, "muted",
                 Qt.AlignRight | Qt.AlignVCenter)
            p.fillRect(QRectF(L, card.top() + 38, card.width() - 32, 1), q("line"))
            text(p, QRectF(L, card.top() + 47, 150, 14), "SUMMARY", 9.5, 800, "text2", spacing=1.3)
            sl = typed_lines(wrap_lines(SUMMARY, 12, 400, R - L), (t - 3.75) * 85.0)
            for i, ln in enumerate(sl):
                text(p, QRectF(L, card.top() + 62 + i * 16, R - L, 17), ln, 12, 400, "text")
            ap = appear(t, 4.4, 0.3)
            if ap > 0.004:
                with layer(p, ap, dy=(1.0 - ap) * 5.0):
                    text(p, QRectF(L, card.top() + 101, 150, 14), "ACTION ITEMS", 9.5, 800, "text2", spacing=1.3)
            for i, (who, name, task) in enumerate((("D", "David", "QA sign-off by Thursday"),
                                                   ("A", "Anna", "Tell the team"))):
                ap = appear(t, 4.55 + i * 0.28, 0.3)
                if ap < 0.004:
                    continue
                y = card.top() + 118 + i * 21
                with layer(p, ap, dy=(1.0 - ap) * 5.0):
                    on = (i == 1 and t >= 5.1)
                    with layer(p, 1.0, s=1.0 + 0.25 * bump(t, 5.1, 0.25) if i == 1 else 1.0, cx=L + 7, cy=y + 9):
                        art.check_box(p, QRectF(L, y + 2, 14, 14), on=on)
                    art.avatar(p, L + 31, y + 9, 14, who, ring=False)
                    text(p, QRectF(L + 44, y, 44, 18), name, 11.5, 700, "ink")
                    text(p, QRectF(L + 44 + text_w(name, 11.5, 700) + 6, y, 160, 18), task, 11.5, 400, "text")


# ── scene 4: Live Assistant ──────────────────────────────────────────────────
CAPTION = "Mariam: When will the Q4 roadmap be ready?"
ANSWER = "Say: “It’s ready for 14 October if we freeze features on Friday.”"


def assistant_card(p, t, x, y, w, h):
    """The Live Assistant card: its name and green Hidden chip, the question, three bouncing dots, then the answer
    typed out. Text is 9.5 px or more (it is read at about half size)."""
    r = QRectF(x, y, w, h)
    glass(p, r, 10, 250)
    text(p, QRectF(r.left() + 10, r.top() + 5, 74, 18), "Live Assistant", 9.5, 700, "ink")
    chip = QRectF(r.right() - 10 - 56, r.top() + 6, 56, 17)
    pill(p, chip, "#dcfce7", "#86efac")
    icon(p, "incognito", QRectF(chip.left() + 5, chip.top() + 3.5, 10, 10), q("ok-tx"), 1.8)
    text(p, QRectF(chip.left() + 18, chip.top(), 36, chip.height()), "Hidden", 9.5, 700, "ok-tx")
    inner_w = w - 20
    ask = QRectF(r.left() + 10, r.top() + 28, inner_w, 30)
    pill(p, ask, "bg2", radius=7)
    text(p, ask.adjusted(7, 0, -7, 0), "When will the Q4 roadmap be ready?", 9.5, 400, "text", wrap=True)
    ans = QRectF(r.left() + 10, ask.bottom() + 5, inner_w, r.bottom() - 8 - (ask.bottom() + 5))
    pill(p, ans, "#eff6ff", "#bfdbfe", radius=7)
    full = wrap_lines(ANSWER, 9.5, 500, inner_w - 14)
    n = (t - 3.0) * 62.0
    if n <= 0:
        p.setPen(Qt.NoPen)
        p.setBrush(q("faint"))
        for i in range(3):
            bounce = abs(math.sin(t * 5.0 - i * 0.7)) * 3.2
            p.drawEllipse(QPointF(ans.left() + 13 + i * 9, ans.center().y() - bounce + 1.5), 2.2, 2.2)
        return
    lh = 12.5
    top = ans.center().y() - len(full) * lh / 2.0
    for i, ln in enumerate(typed_lines(full, n)):
        text(p, QRectF(ans.left() + 7, top + i * lh, inner_w - 14, lh), ln, 9.5, 500, "ink")


def scene4(p, t):
    win = QRectF(SX, SY, SW, SH)
    inner = art.call_window(p, win)
    # Mariam is speaking: a ring that breathes around her tile
    gap = 6.0
    tw, th = (inner.width() - 3 * gap) / 2, (inner.height() - 3 * gap) / 2
    tile = QRectF(inner.left() + gap, inner.top() + gap, tw, th)
    speak = 1.0 - prog(t, 1.55, 1.8)
    if speak > 0.004:
        k = 0.5 + 0.5 * math.sin(t * 5.5)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, int(speak * (110 + 110 * k))), 2.2))
        p.drawRoundedRect(tile.adjusted(-1.5, -1.5, 1.5, 1.5), 7, 7)
    a_cap = appear(t, 0.2, 0.3) * (1.0 - prog(t, 1.55, 1.8))
    if a_cap > 0.004:
        with layer(p, a_cap):
            cap = QRectF(inner.left() + 12, inner.bottom() - 36, inner.width() - 24, 26)
            art.pill(p, cap, QColor(15, 23, 42, 205), radius=8)
            text(p, cap.adjusted(10, 0, -8, 0), typed(CAPTION, (t - 0.3) * 48.0), 10.5, 500, "#ffffff")
    a_card = appear(t, 1.95, 0.45)
    if a_card > 0.004:
        card_h = 111.0
        with layer(p, a_card, dy=(1.0 - a_card) * 14.0):
            assistant_card(p, t, inner.left() + 9, inner.bottom() - card_h - 9, 150.0, card_h)
    # the line that splits the screen: what you see | what they see
    k_line = prog(t, 4.15, 4.6)
    if k_line > 0.004:
        split = 0.64
        x = win.left() + win.width() * split
        mid = win.center().y()
        p.setPen(QPen(QColor("#0f172a"), 1.6))
        p.drawLine(QPointF(x, win.top() + 4), QPointF(x, win.top() + 4 + (win.height() - 8) * ease(k_line)))
        k_h = pop(t, 4.5, 0.3)
        if k_h > 0.004:
            with layer(p, 1.0, s=k_h, cx=x, cy=mid):
                shadow(p, QRectF(x - 11, mid - 11, 22, 22), 11, 30, 4, 2)
                p.setPen(QPen(QColor("#0f172a"), 1))
                p.setBrush(QColor("#ffffff"))
                p.drawEllipse(QPointF(x, mid), 11, 11)
                p.setPen(QPen(QColor("#0f172a"), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                for d in (-1, 1):
                    p.drawPolyline([QPointF(x + d * 2.2, mid - 3.8), QPointF(x + d * 5.2, mid),
                                    QPointF(x + d * 2.2, mid + 3.8)])
        a_ch = appear(t, 4.7, 0.3)
        if a_ch > 0.004:
            with layer(p, a_ch):
                art.label_chip(p, QRectF(win.left() + 12, win.top() + 12, 54, 19), "You see")
                art.label_chip(p, QRectF(win.right() - 66, win.top() + 12, 54, 19), "They see")
        k_hid = pop(t, 5.0, 0.35)
        if k_hid > 0.004:
            cw = 9 + 16 + 5 + text_w("Hidden", 11.5, 700) + 10
            mid_x = win.left() + win.width() * (split + 1) / 2
            chip = QRectF(max(mid_x - cw / 2, x + 16), mid - 13, cw, 26)
            with layer(p, min(1.0, k_hid), s=k_hid, cx=chip.center().x(), cy=mid):
                shadow(p, chip, 13, 24, 5, 3)
                pill(p, chip, "#dcfce7", "#86efac")
                icon(p, "incognito", QRectF(chip.left() + 9, chip.top() + 5, 16, 16), q("ok-tx"), 1.7)
                text(p, QRectF(chip.left() + 30, chip.top(), cw - 30, 26), "Hidden", 11.5, 700, "ok-tx")


# ── scene 5: Audio to text ───────────────────────────────────────────────────
ROWS5 = (("00:00", "Speaker 1", "#1d4ed8", "Thanks for joining, Mariam."),
         ("00:04", "Speaker 2", "#15803d", "Happy to. The Q4 roadmap is ready."),
         ("00:09", "Speaker 1", "#1d4ed8", "Great. Walk me through it."))


def scene5(p, t):
    # 1) the drop zone and a file that arrives
    a1 = 1.0 - prog(t, 1.9, 2.3)
    if a1 > 0.004:
        zone = QRectF(SX, SY + 8, SW, SH - 16)
        with layer(p, a1):
            art.drop_zone(p, zone, active=(t >= 1.15))
            k = ease_out(prog(t, 0.2, 1.2))
            cx = (SX + SW - 62) + (-38.0) * k
            cy = 18 + 36.0 * k
            drop = prog(t, 1.65, 2.05)
            with layer(p, 1.0 - drop, dy=drop * 26.0, s=1.0 - 0.1 * drop, cx=cx, cy=cy):
                art.file_chip(p, cx, cy, angle=-9.0 * (1.0 - k))
    # 2) the work: the bar fills, the sound is read, the speakers are found
    a2 = prog(t, 1.9, 2.3) * (1.0 - prog(t, 4.0, 4.3))
    if a2 > 0.004:
        k = ease(prog(t, 2.3, 3.8))
        done = prog(t, 3.8, 3.95)
        with layer(p, a2, dy=(1.0 - prog(t, 1.9, 2.3)) * 8.0):
            card = QRectF(SX, SY + 10, SW, 84)
            glass(p, card, 14)
            tile = QRectF(card.left() + 14, card.top() + 14, 34, 34)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#e8f0fe"))
            p.drawRoundedRect(tile, 10, 10)
            icon(p, "mic", tile.adjusted(8, 8, -8, -8), q("blue"), 1.9)
            x0 = card.left() + 58
            text(p, QRectF(x0, card.top() + 12, 150, 20), "team_call_mariam.m4a", 12, 700, "ink")
            kb = pop(t, 3.8, 0.3)
            if kb > 0.004:
                with layer(p, min(1.0, kb), s=kb, cx=card.right() - 23, cy=card.top() + 22):
                    p.setPen(Qt.NoPen)
                    p.setBrush(art.PILL_DONE)
                    p.drawEllipse(QRectF(card.right() - 32, card.top() + 13, 18, 18))
                    icon(p, "check", QRectF(card.right() - 28, card.top() + 17, 10, 10), QColor("#ffffff"), 3.0)
            track = QRectF(x0, card.top() + 37, card.width() - 58 - 16, 8)
            art.pill(p, track, "#e2e8f0")
            art.pill(p, QRectF(track.left(), track.top(), max(8.0, track.width() * k), track.height()),
                     mix("#2563eb", "#22c55e", done))
            if done >= 0.5:
                status = "Done · 2 speakers · Whisper Small · Local"
            else:
                status = "Transcribing · %d%% · Whisper Small · Local" % int(k * 100)
            text(p, QRectF(x0, card.top() + 50, card.width() - 70, 22), status, 10.5, 400, "text2")
            wy, ww = SY + 140, SW - 24
            n = len(art.WAVE)
            step = ww / n
            for i, v in enumerate(art.WAVE):
                is_done = (i + 0.5) / n <= k
                live = 1.0 if done > 0.5 else 0.82 + 0.18 * math.sin(t * 7.0 + i * 0.8)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(37, 99, 235, 175) if is_done else QColor(148, 163, 184, 90))
                hh = v * 1.45 * live
                p.drawRoundedRect(QRectF(SX + 12 + i * step + step * 0.2, wy - hh / 2, step * 0.55, hh), 1.6, 1.6)
    # 3) the transcript, then the Word file
    a3 = prog(t, 4.0, 4.3)
    if a3 > 0.004:
        paper = QRectF(SX, SY + 2, SW, SH - 4)
        with layer(p, a3, dy=(1.0 - a3) * 8.0):
            glass(p, paper, 14)
            L = paper.left() + 16
            k_dim = prog(t, 5.0, 5.3)
            with layer(p, 1.0 - 0.3 * k_dim):
                text(p, QRectF(L, paper.top() + 12, 170, 18), "team_call_mariam.m4a", 12.5, 700, "ink")
                text(p, QRectF(L, paper.top() + 30, 190, 16), "English · 24 min · 2 speakers", 10.5, 400, "muted")
                p.fillRect(QRectF(L, paper.top() + 52, paper.width() - 32, 1), q("line"))
                for i, (tm, who, c, line) in enumerate(ROWS5):
                    ap = appear(t, 4.3 + i * 0.28, 0.3)
                    if i == 2:
                        ap *= 1.0 - k_dim
                    if ap < 0.004:
                        continue
                    y = paper.top() + 62 + i * 34
                    with layer(p, ap, dy=(1.0 - ap) * 5.0):
                        text(p, QRectF(L, y, 40, 14), tm, 9.5, 400, "faint", tabular=True)
                        text(p, QRectF(L + 38, y, 90, 14), who, 10, 700, c)
                        text(p, QRectF(L, y + 14, paper.width() - 32, 17), line, 12, 400, "text")
            bw = 98
            b = QRectF(paper.right() - 16 - bw, paper.top() + 12, bw, 28)
            pressed = t >= 5.0
            with layer(p, 1.0, s=1.0 - 0.06 * bump(t, 5.0, 0.22), cx=b.center().x(), cy=b.center().y()):
                p.setPen(QPen(q("blue-h") if pressed else q("line2"), 1))
                p.setBrush(q("blue") if pressed else QColor("#ffffff"))
                p.drawRoundedRect(b.adjusted(.5, .5, -.5, -.5), 8, 8)
                icon(p, "file", QRectF(b.left() + 9, b.top() + 6, 16, 16), QColor("#ffffff") if pressed else q("text2"),
                     1.7)
                text(p, QRectF(b.left() + 30, b.top(), bw - 30, b.height()), "Save Word", 11, 600,
                     "#ffffff" if pressed else "text")
            kc = pop(t, 5.15, 0.3)
            if kc > 0.004:
                cx, cy = paper.center().x(), paper.top() + 146
                with layer(p, min(1.0, kc), dy=(1.0 - ease_out(prog(t, 5.15, 5.45))) * 10.0, s=0.9 + 0.1 * min(1.0, kc),
                           cx=cx, cy=cy):
                    art.file_chip(p, cx, cy, "team_call_mariam.docx", 0.0, "file", "#dbeafe", "blue-h", check=True)


SCENES = {1: scene1, 2: scene2, 3: scene3, 4: scene4, 5: scene5}


# ── frames and the video ─────────────────────────────────────────────────────
def slide_image(n, t, scale):
    c = art.Canvas(SLIDE_W, SLIDE_H, scale)
    art.slide_chrome(c.p, n)
    SCENES[n](c.p, t)
    return c.pil().convert("RGB").resize((SLIDE_W * SLIDE_OUT, SLIDE_H * SLIDE_OUT), Image.LANCZOS)


def loop_frame(i, scale):
    """Frame ``i`` of the loop: the scene that is on screen, cross-faded with the next one near its end."""
    T = i / float(FPS)
    k = int(T // PERIOD_S) % len(SLIDES)
    v = T - (T // PERIOD_S) * PERIOD_S                      # 0 .. 5.4 inside the period of scene k+1
    n = k + 1
    im = slide_image(n, v + FADE_S, scale)                  # the scene has already run its first 0.6 s
    if v >= PERIOD_S - FADE_S:
        nxt = n % len(SLIDES) + 1
        w = ease((v - (PERIOD_S - FADE_S)) / FADE_S)
        im = Image.blend(im, slide_image(nxt, v - (PERIOD_S - FADE_S), scale), w)
    return im


def write_video(path, scale):
    import av
    import numpy as np
    total = int(round(LOOP_S * FPS))
    path.parent.mkdir(parents=True, exist_ok=True)
    out = av.open(str(path), mode="w", options={"movflags": "+faststart"})
    stream = out.add_stream("libx264", rate=FPS)
    stream.width, stream.height = SLIDE_W * SLIDE_OUT, SLIDE_H * SLIDE_OUT
    stream.pix_fmt = "yuv420p"
    stream.options = {"crf": "15", "preset": "slow", "tune": "animation", "profile": "high", "level": "4.0",
                      "x264-params": "colorprim=bt709:transfer=bt709:colormatrix=bt709:fullrange=off:keyint=60"}
    for i in range(total):
        arr = np.asarray(loop_frame(i, scale))
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        frame = frame.reformat(format="yuv420p", dst_colorspace=av.video.reformatter.Colorspace.ITU709)
        for pkt in stream.encode(frame):
            out.mux(pkt)
        if i % 90 == 0:
            print("frame %d / %d" % (i, total), flush=True)
    for pkt in stream.encode():
        out.mux(pkt)
    out.close()
    size = path.stat().st_size
    print("%s: %d frames, %.2f s, %.2f MB" % (path, total, total / float(FPS), size / 1048576.0))
    assert size < BUDGET_BYTES, "the video is over the %d byte budget" % BUDGET_BYTES


def contact_sheet(path, scale, cols=6):
    """One picture with the moments worth looking at (scene n at a few seconds), 1/2 size."""
    marks = {1: (0.7, 1.2, 2.4, 3.7, 4.4, 5.3), 2: (0.7, 1.2, 2.4, 3.4, 4.4, 5.3), 3: (0.7, 1.8, 2.6, 3.3, 4.3, 5.4),
             4: (0.7, 1.9, 2.8, 3.8, 4.5, 5.4), 5: (0.7, 1.4, 3.0, 3.9, 4.8, 5.4)}
    w, h = SLIDE_W * SLIDE_OUT // 2, SLIDE_H * SLIDE_OUT // 2
    sheet = Image.new("RGB", (w * cols, h * len(marks)), "white")
    for r, (n, ts) in enumerate(sorted(marks.items())):
        for c, t in enumerate(ts):
            sheet.paste(slide_image(n, t, scale).resize((w, h), Image.LANCZOS), (c * w, r * h))
    sheet.save(path)
    print("wrote", path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sheet", metavar="PNG", help="write a contact sheet of key moments instead of the video")
    ap.add_argument("--fast", action="store_true", help="paint at 2x (quicker, a little softer)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    scale = 2 if args.fast else art.SS
    if args.sheet:
        contact_sheet(args.sheet, scale)
    else:
        write_video(Path(args.out), scale)


if __name__ == "__main__":
    main()
