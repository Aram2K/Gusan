"""Live Assistant: a private, liquid-glass copilot card for live calls.

Integration notes for Wave 4 (Gusan 2.0; this module is the card, not the page)
* Kept identifiers: ACTION_LIVE_ASSIST "live_assist", telemetry
  live_prompter_opened / _started / _suggestion, IPC action "live_prompter",
  every live_assist_* config key, objectName laPrivate. Only words changed.
* Show-only path for the page, the tray and the reminder toast:
  ``card.open_card()`` (show + raise, tracks live_prompter_opened when it was
  hidden). ``toggle()`` stays for the hotkey. Done (Wave 4): the controller's
  ``show_live_assist()`` builds the card like toggle_live_assist does (fed by
  app.meeting_session.sig_chunk / sig_summary), then open_card(). Start /
  Stop drive app.meeting_session (start(source="live_assist") / stop()).
* The card opens for EVERYONE (the page, the hotkey, the tray, the Meetings
  live view): Free and guests see it with a PRO pill beside the title (hidden
  for Pro; it follows app.sig_auth_changed and refreshes on every open). Start
  is the Pro action: it shows the Pro window. Ask / Answer / Solve screen work
  with the user's own key or a downloaded local model; with neither, the answer
  box says "Answers need Pro, or your own key in Settings › Speech & AI."
* Languages: live_assist_language "" = the dictation language;
  live_assist_output_language "" = the call's own (no "Respond in" hint).
* Public setters - each updates the card at once, writes the cfg key in place,
  schedules the debounced save (app.schedule_config_save, else save_config)
  and emits app.sig_setting_changed(key) when the value changed:
      set_private(on)        live_assist_private   (Hide from screen share)
      set_auto_answer(on)    live_assist_auto_answer
      set_screen_auto(on)    live_assist_screen_auto (Let it see my screen)
      set_theme(name)        live_assist_theme ("light" | "dark")
      set_context(text)      live_assist_context (Context: who you are, what calls are about, or a rule)
      set_opacity(value)     live_assist_opacity (0.55-1.0)
      reset_position()       live_assist_pos -> None, back to the default spot
  (dragging the card saves live_assist_pos = [x, y] the same way). The sync
  only normalises: a value the card can't take (opacity out of range, an
  unknown theme, an over-long context) is written back clamped once.
  Signals: ``settings_changed(key, value)`` whenever the card's own state for
  one of those keys changes (its buttons, a drag, a setter, or a cfg sync), and
  ``hide_status_changed(key)`` when the chip's truthful state changes.
* The card follows cfg by itself: it connects app.sig_setting_changed to
  ``on_setting_changed(key)`` when the app has that signal (theme, private,
  auto_answer, screen_auto, context, opacity; live_assist_pos set to None
  re-centres it). So a page may just call app.set_setting(key, value) - or the
  card setter when the card exists; both end in the same place, and the sync
  never writes a valid value back. settings_apply already forwards live_assist_private to
  set_private (no change needed there).
* The answer footer names who wrote it ("Gusan Cloud", "<local model> on this
  computer", "your Cerebras key"). engine_choice.runs_on(app,
  "live_assistant") has the page's longer label (e.g. "Gusan Cloud · DeepSeek
  V4.1 Flash") if the two should match.
* Under the live transcript, who TRANSCRIBES it ("Transcribing with Gemini 2.5 Pro · Gusan
  Cloud" - what the recorder's speech_report() says ran, so a server that answered with
  another model, or pieces that went through this computer, are named: engine_choice.
  live_speech_status) and, when local Whisper would be transcribing a language it is poor at
  (Armenian), one plain hint naming the cloud options. The choice is the page's "Transcribes
  with" row (cfg live_assist_speech_model, ui/pages/live_speech.py).
* Hidden status without the card open: ``live_context.hide_status(wanted)``
  (Qt-free; key/text/tone/icon/headline/tip). With the card open, prefer
  ``card.hide_state()`` - it reflects what the window really got. Page hero:
  status.headline ("Hidden on screen share"); chip: status.text + status.tone
  + status.icon ("incognito" | "eye" | "eye_off"; ui/components/icons has no
  "eye" yet - add it or use ui.icons.eye_icon). Settings › Privacy can ask
  hide_status(cfg["overlay_private"]) for the dictation bar too.
* Done (Wave 4, main.py): the tray (ui/tray_menu.py) and toggle_live_assist's
  failure hint say "Live Assistant". The card calls _pro_upsell("Live
  Assistant"); main.PRO_FEATURE_EVENT keeps paywall_viewed's feature
  property at the 1.9 value "Live Assistance", and the Pro window shows
  "Live Assistant". The calendar's "Record + Live Assistant" is
  AppController.record_from_calendar(m, with_assist=True): the meeting session
  starts, then show_live_assist().

What it is (product contract):
* Floats above Zoom/Teams/Meet/anything, always on top, draggable, with a
  compact pill state and an expanded card. Its own Listen button starts the
  meeting capture (system audio + mic) so the overlay is self-sufficient.
* Shows the live transcript tail and the rolling summary from the meeting
  pipeline, and - on demand (Say next / Follow-ups / Recap / a typed question)
  or automatically - an AI suggestion. With Screen on, a screenshot rides
  along so the AI can answer about what is on the user's screen.
* A session context: before (or during) a call the user writes what it is
  about - who they are, who they're talking to, what they want. It's locked
  in for the session: every answer is shaped by it.
* Context = STANDING INSTRUCTIONS (live_rules.py, live_context.standing_block): what the user
  writes there is the first block of every prompt, on every route - "Standing instructions
  from the user - follow them in every answer; they override your defaults on language, format
  and length". With the switch "Apply to everything I hear" (cfg live_assist_rules_everything;
  off until the user turns it on - offered, never switched silently, when the text reads like
  such a rule) the instruction runs on EVERY new piece of the conversation (translate it,
  subtitle it, correct it...), not only when a question is heard: pieces heard within a couple
  of seconds go as one, one request runs at a time, the OLDEST waiting group is dropped when the
  model is slower than the talk (the feed says "catching up"), a soft cap per session keeps the
  day's cloud allowance safe, and Pause stops it. The results are a running feed in place of
  the single answer (the original muted above its result, newest first); a typed question still
  gets its own answer, with a "Live results" link back. The chip above says plainly which it
  is: "Everything you hear" or "Questions only".
* Documents: the Context editor has an Add documents button and a paper-clip in the text
  box's corner; files dropped anywhere on the card (the open editor included) become documents
  ("Drop files to add"). The picker is Qt's own dialog, excluded from screen share like the
  card, whenever the card is hidden from shares.
* A calendar invite (Google Calendar, docs/redesign/google-calendar-spec.md,
  decision 12): a session that starts for a calendar meeting - the card's own Start
  or hotkey (AppController.calendar_start_context), the reminder's Record + Live
  Assistant, the card opened mid-call - shows the meeting's title in the header and an
  INVITE chip ("Anna, David +2 · Agenda"). A click opens the agenda, editable ("Sent
  with each answer."); it goes to the model as its own section, framed as data
  (rolling_context(invite=)), and to the meeting's folder through
  session.set_agenda. It is per session and never "About my calls" (cfg
  live_assist_context). The chip's x is "Not this meeting": title, people and agenda
  stop going with the answers. Public: set_meeting_active(active, title, attendees,
  agenda=""), set_invite(agenda, others), has_invite(), invite_text().
* HIDDEN on screen share by default: excluded from screen capture
  (ui/glass.py) so it never appears on a shared screen or in a recording while
  staying visible on the user's own monitor. The chip says "Hidden" only when
  that is true (live_context.hide_status). A privacy feature for the user's
  private notes - never marketed as a way to deceive anyone; recording-consent
  guidance applies.
* An image can go with a question: a screenshot cropped inside the card
  itself (nothing new appears on the screen) or one pasted with Ctrl+V.
* Documents the user added (live_docs.py; added on the Live Assistant page or dropped on
  this card): a chip above the ask box says how many the answers can read, and a click
  lists them with a switch each. Every answer - typed or automatic - carries the parts of
  the ACTIVE ones that match it, as one delimited block of data, built on the worker thread
  once the engine is known (_documents_for_answer; live_docs.doc_use decides, Privacy mode
  is absolute, a cloud route needs the person's one-time yes - asked on Start and for a
  typed question, never in the middle of an automatic answer). Pictures from documents ride
  after the screenshot on a route that can see images. See docs/redesign/live-documents-spec.md.
* Liquid glass: because the window is excluded from capture, the pixels
  BEHIND it can be sampled (QScreen.grabWindow honours the exclusion) and
  rendered back through the shape - real blur, an edge refraction ring, a
  cursor-tracked specular highlight, rim light and a soft shadow. Where
  sampling isn't safe (Visible mode, exclusion unconfirmed, macOS, RDP) it
  paints a darker rounded plate instead - never the OS acrylic, which blurs
  the whole square window and shows as a rectangle around the card.

Threading: suggestion requests run on a worker thread and report back via
signals; every widget touch happens on the GUI thread.
"""
import base64
import logging
import os
import re
import sys
import threading
import time

from PySide6.QtCore import (
    Qt, QBuffer, QEvent, QIODevice, QObject, QPoint, QPointF, QRect, QRectF, QSize, QTimer, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QIcon, QImage, QKeySequence, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

import actions
from live_context import (  # noqa: F401 - re-exported for callers of this module
    CONTEXT_CHARS, HISTORY_TURNS, SOLVE_SCREEN, TAIL_CHARS, HideStatus, clip_context,
    hide_status, last_question, looks_like_question, mac_partial_tip, private_state,
    rolling_context, safe_answer_markdown, should_attach_screen,
)
from live_context import others_of, start_session
import invite_context
import live_docs
import live_rules
from ui import glass, live_docs_ui

logger = logging.getLogger("transcribe")

SHADOW = 16                      # transparent margin around the card (drop shadow)
RADIUS = 22
CARD_W = 480
# Window = card + shadow margins. Width is shared by both states so
# collapse/expand never jumps sideways.
EXPANDED_W, EXPANDED_H = CARD_W + 2 * SHADOW, 560 + 2 * SHADOW
COMPACT_W, COMPACT_H = CARD_W + 2 * SHADOW, 52 + 2 * SHADOW
SAMPLE_MS_REST, SAMPLE_MS_DRAG = 150, 40

_THEMES = {
    "light": {
        # Cool slate rather than milky white: darker glass, and what's behind
        # doesn't bleed through the text.
        "tint_liquid": QColor(214, 222, 233, 165),  # over the sampled backdrop
        "tint_blur": QColor(214, 222, 233, 190),    # (OS acrylic - no longer used)
        "tint_flat": QColor(206, 215, 227, 242),    # painted plate (Visible mode)
        "border": QColor(15, 23, 42, 40),
        "text": "#0f172a", "muted": "#475569", "faint": "#64748b",
        "card": "rgba(255,255,255,0.72)", "card_border": "rgba(15,23,42,0.10)",
        "accent": "#2563eb", "spec": 95,
    },
    "dark": {
        "tint_liquid": QColor(17, 24, 39, 160),
        "tint_blur": QColor(17, 24, 39, 180),
        "tint_flat": QColor(15, 20, 33, 240),
        "border": QColor(255, 255, 255, 46),
        "text": "#f8fafc", "muted": "#cbd5e1", "faint": "#94a3b8",
        "card": "rgba(17,24,39,0.62)", "card_border": "rgba(255,255,255,0.14)",
        "accent": "#60a5fa", "spec": 45,
    },
}

# Settings that change who transcribes the call (the line and the hint under the live transcript).
HEAR_KEYS = frozenset({"live_assist_speech_model", "live_assist_language", "language", "mix_languages",
                       "privacy_mode", "backend", "whisper_model", "managed_provider", "google_api_key",
                       "google_stt_model", "mistral_api_key", "mistral_stt_model",
                       "admin_tier_override"})

# What the live transcript shows for a stretch the cloud could not read (meeting_session.GAP_MARK).
GAP_MARK = "[...]"

# After an answer the user asked for, how long an automatic one waits before
# it may replace it (they are still reading it, or saying it).
USER_ANSWER_HOLD_S = 12

# Free / guest with no key of their own and no downloaded local model.
NEEDS_PRO_ANSWERS = "Answers need Pro, or your own key in Settings › Speech & AI."

ASK_PLACEHOLDER = "Ask anything about the call or your screen…"
ASK_SENT = "✓ Sent - answering…"
TRANSCRIPT_PLACEHOLDER = "Press Start. What's said shows up here."
ANSWER_PLACEHOLDER = "Answers appear here the moment someone asks you something."

# Sent with an image and no typed question.
SNIP_QUESTION = "Answer or solve what's in the attached image. Give the answer directly."

INVITE_PLACEHOLDER = "Nothing from the invite. Add what this call is about."
INVITE_HINT = "Sent with each answer."
INVITE_CHARS = invite_context.AGENDA_CHARS
CONTEXT_EMPTY = "+ Add context - what's this call about?"
CONTEXT_PLACEHOLDER = (
    "What's this call about, and how should answers go? Who you are, who you're "
    "talking to, what you want from it - or a rule like \"Translate everything "
    "you hear into French.\" Every answer follows it.\n\n"
    "e.g. Call with Acme's data team about moving their reports to our platform. "
    "I'm the solutions engineer; they care most about cost and security. Keep "
    "answers short, in my voice.")
CTX_EDIT_H = 84                  # the editor's text box (the rows under it need the room)

# Documents the user added on the Live Assistant page (live_docs.py): the chip above the ask
# box says how many answers can read, and the answer's footer says when they were used.
DOCS_OFF = "Documents off"
DOCS_TIP = "Click to choose which documents the assistant can use."
FOOT_DOCS_USED = "from your documents"
FOOT_DOCS_KEPT = "documents stayed on this computer"
FOOT_DOCS_ASK = "documents not used yet - Gusan asks when you type a question"
FOOT_PICTURES = "pictures need Gusan Cloud or your own key"
DOC_DROP = "Drop files to add"
DOC_ASK_WAIT_S = 180                        # how long an answer waits for the documents question
ADD_DOCS = "Add documents"
ADD_DOCS_TIP = "Add documents the assistant can use. You can also drop files on this card."

# "Apply to everything I hear" (live_rules.py): the running feed that replaces the single answer.
FEED_LABEL = "LIVE RESULTS"
RULES_IDLE = "Press Start. Everything you hear gets your rule, and the results show here."
RULES_LIVE = "Listening… each sentence gets your rule, and its result shows here."
AUTO_TIP = "Answer automatically the moment someone asks a question."
AUTO_TIP_RULES = ("Off while your rule applies to everything you hear: every sentence "
                  "already gets its result in Live results.")
RULES_TIP_ON = ("Everything you hear: each new sentence is handled by your rule - "
                "translated, summarised, corrected... whatever you wrote.")
RULES_TIP_OFF = ("Questions only: your rule shapes every answer, but nothing is done to "
                 "everything you hear. Turn on \"Apply to everything I hear\" for that.")
NEEDS_RULE_TEXT = "Write a rule in the context first."
LIMIT_WORDS = "Today's cloud allowance is used up. It comes back tomorrow."

QUICK_ACTIONS = (
    ("Answer", ""),
    ("Follow-ups", "Give me 3 sharp follow-up questions I could ask right now, "
                   "one line each."),
    ("Solve screen", SOLVE_SCREEN),
)
QUICK_TIPS = {
    "Answer": "Answer the latest question of the call again.",
    "Follow-ups": "Three follow-up questions you could ask now.",
    "Solve screen": "Solve or answer what's on your screen.",
}


def clamp_to_rects(x, y, w, h, rects, margin=8):
    """Keep a saved window position reachable: if (x, y) isn't on any current
    screen (monitor unplugged, resolution changed) pull it onto the first one.
    ``rects`` are (left, top, right, bottom) tuples."""
    if not rects:
        return x, y
    for l, t, r, b in rects:
        if l <= x < r - 40 and t <= y < b - 40:
            return x, y
    l, t, r, b = rects[0]
    return (max(l + margin, min(x, r - w - margin)),
            max(t + margin, min(y, b - h - margin)))


# The Hidden chip's colours (background, text, border) per card theme: the
# light glass takes the deep tints, the dark glass the bright ones (#15803d
# is unreadable on dark glass). The green also colours the live timer.
_CHIP_COLORS = {
    "light": {
        "ok": ("rgba(34,197,94,0.16)", "#15803d", "rgba(34,197,94,0.45)"),
        "red": ("rgba(239,68,68,0.14)", "#b91c1c", "rgba(239,68,68,0.45)"),
        "amber": ("rgba(245,158,11,0.16)", "#b45309", "rgba(245,158,11,0.5)"),
    },
    "dark": {
        "ok": ("rgba(34,197,94,0.16)", "#4ade80", "rgba(34,197,94,0.45)"),
        "red": ("rgba(239,68,68,0.18)", "#fca5a5", "rgba(239,68,68,0.5)"),
        "amber": ("rgba(245,158,11,0.18)", "#fcd34d", "rgba(245,158,11,0.5)"),
    },
}

# What a click on the chip does, after its explanation.
_CHIP_CLICK = {
    "on": "Click to show it in shares - e.g. for your own recording.",
    "off": "Click to hide it from shares.",
    "partial": "Click to make it visible.",
}


def chip_colors(tone, theme="light"):
    """(background, text, border) of the Hidden chip for a tone and card theme."""
    colors = _CHIP_COLORS.get(theme, _CHIP_COLORS["light"])
    return colors.get(tone, colors["amber"])


def _wrap_tip(text):
    """A plain sentence as a tooltip that wraps (Qt wraps only rich text)."""
    import html as html_mod
    return "<p style='white-space:normal'>%s</p>" % html_mod.escape(text or "", quote=False)


def _engine_label(engine):
    """Who wrote the answer, in plain words for the footer: Gusan Cloud, the
    local model's name, or the user's own key ("" when unknown)."""
    info = actions.ACTION_MODELS.get(engine or "", {})
    kind = info.get("kind")
    if kind == "managed":
        return "Gusan Cloud"
    if kind == "local_llm":
        return f"{info.get('label', engine)} on this computer"
    if kind == "cloud":
        short = {actions.API_CEREBRAS_ID: "Cerebras", actions.API_GEMINI_ID: "Gemini",
                 actions.API_ANTHROPIC_ID: "Anthropic",
                 actions.API_MISTRAL_ID: "Mistral"}.get(engine, "API")
        return f"your {short} key"
    return ""


def _looks_like_capture_hole(small_img, black_max=12, band_frac=0.14):
    """True when a horizontal band of the (downscaled) sample is pure black -
    the signature of a hardware video surface that BitBlt cannot read. A
    genuinely dark desktop is not a contiguous pure-black band."""
    try:
        w, h = small_img.width(), small_img.height()
        if w < 4 or h < 4:
            return False
        black_rows = 0
        for y in range(h):
            dark = 0
            for x in range(w):
                c = small_img.pixelColor(x, y)
                if c.red() <= black_max and c.green() <= black_max and c.blue() <= black_max:
                    dark += 1
            if dark >= w * 0.9:
                black_rows += 1
        return black_rows >= max(2, int(h * band_frac))
    except Exception:
        return False


_CODE_CSS = ("pre, code { font-family: 'Cascadia Mono', Consolas, 'Courier New', monospace; "
             "font-size: 12px; } pre { background: rgba(15,23,42,0.08); border-radius: 6px; "
             "padding: 6px; }")


def render_markdown(text_edit, md):
    """Show Markdown in a QTextEdit with monospace, boxed code blocks. Qt's
    setMarkdown has no styling hook, so the document is converted to HTML
    first and re-set with a default stylesheet for pre/code.

    An answer never loads an image: Qt's text widgets would fetch a file:// or
    \\\\host\\share one named in it (a document could make the model write one), so
    images are taken out of the Markdown first (safe_answer_markdown) and the
    answer box refuses every resource anyway (_AnswerEdit)."""
    try:
        doc = text_edit.document()
        doc.setDefaultStyleSheet(_CODE_CSS)
        doc.setMarkdown(safe_answer_markdown(md))
        html = doc.toHtml()
        text_edit.setHtml(html)
    except Exception:
        text_edit.setPlainText(md or "")


STREAM_RENDER_MS = 120      # a streaming answer is re-rendered at most this often


def streaming_markdown(md):
    """An answer that is still streaming, safe to render: an open ``` fence
    is closed, and a half-written ** or ` is left out until its pair arrives,
    so Markdown markers never flash as raw characters."""
    s = md or ""
    if s.count("```") % 2:
        return s + "\n```"
    for mark in ("**", "`"):
        if s.count(mark) % 2:
            i = s.rfind(mark)
            s = s[:i] + s[i + len(mark):]
    return s


def jpeg_b64(img, max_w=1600, quality=80):
    """A QPixmap/QImage as base64 JPEG, downscaled for upload - a third of the
    PNG size, so it reaches the model faster, yet sharp enough for code and
    small UI text. "" for an empty image."""
    if img is None or img.isNull():
        return ""
    if isinstance(img, QImage) and img.hasAlphaChannel():
        # JPEG has no alpha: transparent areas would turn black.
        flat = QImage(img.size(), QImage.Format_RGB32)
        flat.fill(QColor("#ffffff"))
        p = QPainter(flat)
        p.drawImage(0, 0, img)
        p.end()
        img = flat
    if img.width() > max_w:
        img = img.scaledToWidth(max_w, Qt.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "JPG", quality)
    return base64.b64encode(bytes(buf.data())).decode("ascii")


def capture_screen_b64(screen, max_w=1600, quality=80):
    """Screenshot of ``screen`` as base64 JPEG (see jpeg_b64). The overlay
    itself is absent when capture exclusion (Private) is active."""
    try:
        return jpeg_b64(screen.grabWindow(0), max_w, quality)
    except Exception as e:
        logger.debug("screen capture failed: %s", e)
        return ""


def screen_to_capture(overlay_screen):
    """The monitor the user is working on: the one under the mouse, since a
    question about "this" is about what they're looking at - not necessarily
    the monitor the overlay sits on."""
    try:
        return QApplication.screenAt(QCursor.pos()) or overlay_screen \
            or QApplication.primaryScreen()
    except Exception:
        return overlay_screen or QApplication.primaryScreen()


def _send_icon(size=18, color="#ffffff"):
    """A chat-style send icon (paper plane pointing right), painted as a path
    so it's crisp at any DPI."""
    scale = 3                                   # draw big, let Qt scale down smoothly
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size * scale / 24.0, size * scale / 24.0)
    path = QPainterPath(QPointF(3.4, 20.4))     # the classic send glyph on a 24 grid
    path.lineTo(QPointF(21.0, 12.0))
    path.lineTo(QPointF(3.4, 3.6))
    path.lineTo(QPointF(3.4, 10.2))
    path.lineTo(QPointF(15.0, 12.0))
    path.lineTo(QPointF(3.4, 13.8))
    path.closeSubpath()
    p.fillPath(path, QColor(color))
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


def _lock_pixmap(size=12, color="#2563eb"):
    """A small padlock - the session context is locked in - painted as a path
    so it's crisp at any DPI."""
    scale = 3
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size * scale / 24.0, size * scale / 24.0)
    pen = QPen(QColor(color), 2.6)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    shackle = QPainterPath(QPointF(7.5, 11.5))
    shackle.lineTo(QPointF(7.5, 8.0))
    shackle.arcTo(QRectF(7.5, 3.5, 9.0, 9.0), 180, -180)
    shackle.lineTo(QPointF(16.5, 11.5))
    p.drawPath(shackle)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(4.5, 10.5, 15.0, 11.0), 2.5, 2.5)
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm


class _AnswerEdit(QTextEdit):
    """The answer box. It never loads a resource (an image, a style sheet) named in
    an answer: by default Qt falls back to reading the local disk - or a network
    share - for it. A null image is a valid "answer" that stops that fallback."""

    def loadResource(self, type_, name):
        return QImage()


class _Superseded(BaseException):
    """Raised from a stream's token callback when a newer question took over.
    A BaseException so the providers' per-chunk ``except Exception`` parsing
    can't swallow it - the stale stream stops at its next token."""


class _IconButton(QPushButton):
    """Round glass button with a painted vector icon (theme / collapse /
    close). Text glyphs rendered inconsistently across fonts; a path is
    crisp at any size and follows the theme ink."""

    def __init__(self, kind, parent, tip=""):
        super().__init__(parent)
        self._kind = kind
        self._alt = False          # collapse: True when the card is collapsed
        self._hover = False
        self._theme = _THEMES["light"]
        self.setFixedSize(26, 26)
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("background: transparent; border: none;")

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def set_alt(self, alt):
        self._alt = bool(alt)
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        light = self._theme is _THEMES["light"]
        on = self.isCheckable() and self.isChecked()
        if on:
            fill = QColor(self._theme["accent"])
            border = fill
        else:
            fill = QColor(255, 255, 255, (92 if self._hover else 58) if light
                          else (46 if self._hover else 26))
            border = QColor(15, 23, 42, 34) if light else QColor(255, 255, 255, 50)
        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        p.drawEllipse(r)
        ink = QColor("#ffffff") if on else QColor(self._theme["text"])
        pen = QPen(ink, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        cx, cy = r.center().x(), r.center().y()
        if self._kind == "close":
            p.drawLine(QPointF(cx - 3.5, cy - 3.5), QPointF(cx + 3.5, cy + 3.5))
            p.drawLine(QPointF(cx + 3.5, cy - 3.5), QPointF(cx - 3.5, cy + 3.5))
        elif self._kind == "collapse":
            d = 2.2 if not self._alt else -2.2      # chevron up (collapse) / down (expand)
            path = QPainterPath(QPointF(cx - 4.2, cy + d))
            path.lineTo(QPointF(cx, cy - d))
            path.lineTo(QPointF(cx + 4.2, cy + d))
            p.drawPath(path)
        elif self._kind == "theme":
            circle = QRectF(cx - 4.6, cy - 4.6, 9.2, 9.2)
            p.drawEllipse(circle)
            p.setBrush(ink)
            p.setPen(Qt.NoPen)
            p.drawPie(circle, 90 * 16, 180 * 16)
        elif self._kind == "screen":
            # A monitor: rounded screen + stand.
            p.drawRoundedRect(QRectF(cx - 5.5, cy - 4.5, 11, 7.5), 1.6, 1.6)
            p.drawLine(QPointF(cx, cy + 3), QPointF(cx, cy + 5.2))
            p.drawLine(QPointF(cx - 3, cy + 5.2), QPointF(cx + 3, cy + 5.2))
        elif self._kind == "snip":
            # Viewfinder corners - "capture a part of the screen".
            s, k = r.width() * 0.22, r.width() * 0.11
            for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                x, y = cx + dx * s, cy + dy * s
                p.drawLine(QPointF(x, y), QPointF(x - dx * k, y))
                p.drawLine(QPointF(x, y), QPointF(x, y - dy * k))
        elif self._kind == "stop":
            # Red rounded square - the universal "stop recording".
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ef4444"))
            p.drawRoundedRect(QRectF(cx - 4.5, cy - 4.5, 9, 9), 2.2, 2.2)


class _ElidedLabel(QLabel):
    """One line that shortens itself with "…" when the bar is tight (fonts
    run wider on macOS) instead of being cut off mid-letter."""

    def __init__(self, text, parent=None):
        super().__init__(parent)
        # Always plain text: the title and the people come from a calendar invite
        # (anyone can send one), and a QLabel reads "<img src=...>" in its text as
        # markup and loads that image - from the disk, or from a network share.
        self.setTextFormat(Qt.PlainText)
        QLabel.setText(self, text)
        self._full = text

    def setText(self, text):
        self._full = text or ""
        self._elide()

    def text(self):
        return self._full

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4,
                     super().sizeHint().height())

    def minimumSizeHint(self):
        return QSize(28, super().minimumSizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event):
        super().changeEvent(event)
        self._elide()                     # a style/font change re-measures

    def _elide(self):
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(0, self.width()))
        if shown != QLabel.text(self):
            QLabel.setText(self, shown)


class _AskEdit(QLineEdit):
    """The ask box. Ctrl+V (⌘V) with an image on the clipboard attaches the
    image instead of pasting text."""
    image_pasted = Signal(QImage)

    _IMAGE_FILES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.Paste):
            img = self._clipboard_image()
            if img is not None and not img.isNull():
                self.image_pasted.emit(img)
                return
        super().keyPressEvent(e)

    def _clipboard_image(self):
        """An image on the clipboard - a screenshot, or an image file copied in
        Explorer / Finder - else None."""
        cb = QApplication.clipboard()
        mime = cb.mimeData()
        if mime is None:
            return None
        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile() if url.isLocalFile() else ""
                if path.lower().endswith(self._IMAGE_FILES):
                    return QImage(path)
        if mime.hasImage() and not mime.hasText():
            # (Excel and others put a picture next to copied text - text wins.)
            return cb.image()
        return None


class _ImageChip(QPushButton):
    """The attached image: a rounded thumbnail with a small x - click to
    remove it."""

    def __init__(self, parent):
        super().__init__(parent)
        self._thumb = QPixmap()
        self.setFixedSize(34, 34)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("background: transparent; border: none;")
        self.setToolTip("Image attached - it goes with your next question. Click to remove.")

    def set_image(self, img):
        dpr = self.devicePixelRatioF() or 1.0
        side = int(34 * dpr)
        pm = QPixmap.fromImage(img).scaled(side, side, Qt.KeepAspectRatioByExpanding,
                                           Qt.SmoothTransformation)
        x, y = max(0, (pm.width() - side) // 2), max(0, (pm.height() - side) // 2)
        self._thumb = pm.copy(x, y, side, side)
        self._thumb.setDevicePixelRatio(dpr)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(r, 8, 8)
        p.setClipPath(clip)
        if not self._thumb.isNull():
            p.drawPixmap(r.toRect(), self._thumb)
        p.setClipping(False)
        p.setPen(QPen(QColor(15, 23, 42, 60), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 8, 8)
        # The remove badge, top right.
        badge = QRectF(r.right() - 13, r.top() + 1, 12, 12)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 200))
        p.drawEllipse(badge)
        pen = QPen(QColor("#ffffff"), 1.4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        c = badge.center()
        p.drawLine(QPointF(c.x() - 2.5, c.y() - 2.5), QPointF(c.x() + 2.5, c.y() + 2.5))
        p.drawLine(QPointF(c.x() + 2.5, c.y() - 2.5), QPointF(c.x() - 2.5, c.y() + 2.5))


class _PopupPrivacy(QObject):
    """Tooltips, right-click menus and other popups are windows of their own,
    so the card's capture exclusion doesn't cover them - a tooltip over the
    card would show in a share. Each one opened from the card gets the card's
    privacy as it shows (before it's on screen), and gives it back if Qt
    reuses it for another window.

    A menu's parent is the widget it belongs to; Qt 6's tooltip window has no
    parent, so a tooltip is the card's when the last tooltip REQUEST (the
    QEvent.ToolTip sent to the widget under the mouse) came from the card."""

    def __init__(self, card):
        super().__init__(card)
        self._card = card

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.ToolTip and isinstance(obj, QWidget):
            mine = obj.window() is self._card
            self._card._tip_from_card = mine
            if mine:
                # The tooltip window may already be up (Qt reuses it while a
                # tooltip shows): guard it too, once Qt has placed the text.
                QTimer.singleShot(0, self._card._guard_visible_tips)
        elif t == QEvent.Show and obj is not self._card \
                and isinstance(obj, QWidget) and obj.isWindow() \
                and obj.windowType() in (Qt.ToolTip, Qt.Popup):
            self._card._guard_popup(obj)
        return False


class _CropView(QWidget):
    """The screenshot, inside the card: drag over the part to send. Nothing
    opens on the screen itself, so a shared screen shows nothing new (no
    full-screen picker, no crosshair sweeping across it) - the card is the
    only thing that changes, and it's private. Enter or a double-click
    attaches, Esc cancels."""
    changed = Signal()
    accepted = Signal()
    cancelled = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self._shot = QPixmap()
        self._origin = None                # drag start, 0..1 image coordinates
        self._sel = None                   # QRectF in 0..1 image coordinates
        self._sel_before_press = None      # a double-click's first click clears
        self._dbl = False                  # the press was a double-click's second
        self._dbl_restore = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.CrossCursor)
        self.setMinimumHeight(120)
        # As tall as the screenshot's shape needs (no empty bands above and
        # below it), capped so a portrait monitor still fits the card.
        pol = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        pol.setHeightForWidth(True)
        self.setSizePolicy(pol)

    MAX_H = 380
    CLICK_SLOP = 5                     # px on screen: less is a click, not a drag

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        if self._shot.isNull() or self._shot.width() <= 0:
            return 240
        return max(120, min(self.MAX_H, round(w * self._shot.height() / self._shot.width())))

    def sizeHint(self):
        return QSize(452, self.heightForWidth(452))

    def set_shot(self, pm):
        self._shot = pm
        self._sel = self._origin = None
        self.updateGeometry()                 # a new shape (another monitor)
        self.changed.emit()
        self.update()

    def has_selection(self):
        return self._sel is not None

    def _target(self):
        """Where the screenshot is drawn: fitted and centred."""
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self._shot.isNull() or r.width() <= 0 or r.height() <= 0:
            return r
        iw, ih = self._shot.width(), self._shot.height()
        scale = min(r.width() / iw, r.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF(r.x() + (r.width() - w) / 2, r.y() + (r.height() - h) / 2, w, h)

    def _norm(self, pos):
        t = self._target()
        if t.width() <= 0 or t.height() <= 0:
            return QPointF(0, 0)
        return QPointF(min(1.0, max(0.0, (pos.x() - t.x()) / t.width())),
                       min(1.0, max(0.0, (pos.y() - t.y()) / t.height())))

    @staticmethod
    def _span(a, b):
        return QRectF(QPointF(min(a.x(), b.x()), min(a.y(), b.y())),
                      QPointF(max(a.x(), b.x()), max(a.y(), b.y())))

    def crop_rect(self):
        """The selection in the screenshot's device pixels (the whole shot
        when nothing is selected)."""
        iw, ih = self._shot.width(), self._shot.height()
        if self._sel is None:
            return QRect(0, 0, iw, ih)
        s = self._sel
        x, y = round(s.x() * iw), round(s.y() * ih)
        return QRect(x, y, max(1, min(iw - x, round(s.width() * iw))),
                     max(1, min(ih - y, round(s.height() * ih))))

    def crop_image(self):
        if self._shot.isNull():
            return QImage()
        return self._shot.copy(self.crop_rect()).toImage()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        t = self._target()
        clip = QPainterPath()
        clip.addRoundedRect(t, 8, 8)
        p.setClipPath(clip)
        if not self._shot.isNull():
            p.drawPixmap(t, self._shot, QRectF(self._shot.rect()))
        if self._sel is not None:
            sel = QRectF(t.x() + self._sel.x() * t.width(), t.y() + self._sel.y() * t.height(),
                         self._sel.width() * t.width(), self._sel.height() * t.height())
            outside = QPainterPath()
            outside.addRect(t)
            outside.addRect(sel)                          # odd-even fill: a hole
            p.fillPath(outside, QColor(8, 12, 20, 130))
            p.setClipping(False)
            p.setPen(QPen(QColor("#60a5fa"), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(sel)
        p.setClipping(False)
        p.setPen(QPen(QColor(15, 23, 42, 70), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(t, 8, 8)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._dbl = False
            self._sel_before_press = self._sel
            self._origin = self._norm(e.position())
            self._sel = None
            self.changed.emit()
            self.update()
        elif e.button() == Qt.RightButton:
            self.cancelled.emit()

    def mouseMoveEvent(self, e):
        if self._origin is not None:
            self._sel = self._span(self._origin, self._norm(e.position()))
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._origin is None:
            return
        sel = self._span(self._origin, self._norm(e.position()))
        self._origin = None
        t = self._target()
        # A click (no real drag) clears the selection: the whole screen again.
        # Measured on screen, not in screenshot pixels: a 4K shot is drawn ~8x
        # smaller here, so a 1 px wobble would otherwise count as a crop. One
        # long side is enough: a thin drag over one line of text is a crop.
        dragged = max(sel.width() * t.width(), sel.height() * t.height()) >= self.CLICK_SLOP
        if self._dbl:
            self._dbl = False
            if not dragged:
                # A real double-click: attach what was picked before it.
                self._sel = self._dbl_restore
                self.changed.emit()
                self.update()
                self.accepted.emit()
                return
        self._sel = sel if dragged else None
        self.changed.emit()
        self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            # Qt delivers a quick second press as this, with no press event.
            # Decided on release: released in place it's a double-click
            # (attach); moved, it's a drag begun right after a click.
            self._dbl = True
            self._dbl_restore = self._sel_before_press
            self._origin = self._norm(e.position())
            self._sel = None
            self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accepted.emit()
        else:
            super().keyPressEvent(e)


class _ContextChip(QFrame):
    """The line under the header: the session context the answers use, or an
    invitation to add one. A click opens the editor."""
    clicked = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("laCtxChip")
        self.setAttribute(Qt.WA_Hover, True)          # :hover in the stylesheet
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(26)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 0, 9, 0)
        lay.setSpacing(6)
        self.lock = QLabel(self)
        self.lock.hide()
        lay.addWidget(self.lock)
        self.tag = QLabel("CONTEXT", self)
        self.tag.setObjectName("laCtxTag")
        lay.addWidget(self.tag)
        self.text = _ElidedLabel("", self)
        self.text.setObjectName("laCtxText")
        lay.addWidget(self.text, 1)
        # Plainly which it is: "Everything you hear" or "Questions only".
        self.scope = QLabel("", self)
        self.scope.setObjectName("laCtxScope")
        self.scope.setFixedHeight(16)
        self.scope.hide()
        lay.addWidget(self.scope)
        self.edit_hint = QLabel("Edit", self)
        self.edit_hint.setObjectName("laCtxEditHint")
        lay.addWidget(self.edit_hint)

    def mousePressEvent(self, e):
        e.accept()                                    # so the release comes here

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class _ContextEdit(QPlainTextEdit):
    """The session-context editor. Ctrl+Enter (⌘↩ on a Mac) or Esc finishes;
    a plain Enter is a new line."""
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.clip_button = None

    def attach_button(self, button):
        """A small button in the bottom-right corner, inside the frame (the paper-clip); the
        text wraps before it (a gutter on the right), so it never runs under it."""
        self.clip_button = button
        button.setParent(self)
        self.setViewportMargins(0, 0, button.width() + 8, 0)
        self._place_button()

    def _place_button(self):
        b = self.clip_button
        if b is not None:
            b.move(max(0, self.width() - b.width() - 6), max(0, self.height() - b.height() - 3))
            b.raise_()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_button()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape or (e.key() in (Qt.Key_Return, Qt.Key_Enter)
                                        and e.modifiers() & Qt.ControlModifier):
            self.finished.emit()
            return
        super().keyPressEvent(e)


class _InviteChip(_ContextChip):
    """The line under the header while the session came from a calendar invite:
    "INVITE  Anna, David +2 · Agenda". A click opens the agenda to read or edit;
    the small x says "Not this meeting" and the answers stop using the invite."""
    dismissed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.tag.setText("INVITE")
        self.setAccessibleName("Invite from your calendar")
        self.drop = _IconButton("close", self, "Not this meeting")
        self.drop.setFixedSize(20, 20)
        self.drop.setAccessibleName("Not this meeting")
        self.drop.clicked.connect(self.dismissed.emit)
        self.layout().addWidget(self.drop)


class _RuleOffer(QFrame):
    """One calm offer: "This sounds like a rule for everything you hear. Apply it to every
    sentence?  [Yes] [No]". It only ever asks - the answer is the user's."""
    yes = Signal()
    no = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("laRuleOffer")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)
        lay.setSpacing(8)
        self.text = QLabel(live_rules.OFFER_TEXT, self)
        self.text.setObjectName("laRuleOfferText")
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.PlainText)
        lay.addWidget(self.text, 1)
        self.btn_yes = QPushButton(live_rules.OFFER_YES, self)
        self.btn_yes.setObjectName("laCtxDone")
        self.btn_no = QPushButton(live_rules.OFFER_NO, self)
        for b in (self.btn_yes, self.btn_no):
            b.setFixedHeight(22)
            b.setCursor(Qt.PointingHandCursor)
            lay.addWidget(b, 0, Qt.AlignVCenter)
        self.btn_yes.setAccessibleName("Yes, apply it to every sentence")
        self.btn_no.setAccessibleName("No, questions only")
        self.btn_yes.clicked.connect(self.yes)
        self.btn_no.clicked.connect(self.no)
        self.setAccessibleName(live_rules.OFFER_TEXT)
        self.hide()


class _Switch(QPushButton):
    """A real switch (track and knob) with its words beside it. Painted, so it follows the
    card's glass theme; a ring shows the keyboard focus; Space flips it."""
    TRACK_W, TRACK_H = 30, 16

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedHeight(24)
        self._t = _THEMES["light"]
        self.setAccessibleName(text)

    def set_theme(self, t):
        self._t = t
        self.update()

    def sizeHint(self):
        return QSize(self.TRACK_W + 11 + self.fontMetrics().horizontalAdvance(self.text()) + 6, 24)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        t, on, enabled = self._t, self.isChecked(), self.isEnabled()
        h = self.height()
        track = QRectF(1.5, (h - self.TRACK_H) / 2.0, self.TRACK_W, self.TRACK_H)
        if on:
            fill = QColor(t["accent"])
        else:
            fill = QColor(t["faint"])
            fill.setAlpha(150)
        if not enabled:
            fill.setAlpha(70)
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(track, self.TRACK_H / 2.0, self.TRACK_H / 2.0)
        d = self.TRACK_H - 4
        x = track.right() - d - 2 if on else track.left() + 2
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, track.top() + 2, d, d))
        if self.hasFocus():
            ring = QPen(QColor(t["accent"]), 1.4)
            p.setPen(ring)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(track.adjusted(-1.5, -1.5, 1.5, 1.5), (self.TRACK_H + 3) / 2.0,
                              (self.TRACK_H + 3) / 2.0)
        p.setPen(QColor(t["text"] if enabled else t["faint"]))
        p.setFont(self.font())
        p.drawText(QRectF(track.right() + 10, 0, max(0, self.width() - track.right() - 10), h),
                   Qt.AlignVCenter | Qt.AlignLeft, self.text())
        p.end()


class _DocsChip(QPushButton):
    """"3 documents", above the ask box: how many of the documents the user added the
    answers can read. A click opens the list to choose from (a menu: nothing opens on
    the screen itself)."""

    def __init__(self, parent):
        super().__init__("", parent)
        self.setObjectName("laDocs")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(24)
        self.setAccessibleName("Documents")


class _DropVeil(QWidget):
    """Over the card while files are dragged onto it: they become documents."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._accent = QColor(_THEMES["light"]["accent"])
        self.hide()

    def set_theme(self, t):
        self._accent = QColor(t["accent"])
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        m = SHADOW + 6
        r = QRectF(self.rect()).adjusted(m, m, -m, -m)
        fill = QColor(self._accent)
        fill.setAlpha(36)
        pen = QPen(self._accent, 2)
        pen.setStyle(Qt.CustomDashLine)
        pen.setDashPattern([4.0, 3.0])
        p.setBrush(fill)
        p.setPen(pen)
        p.drawRoundedRect(r, RADIUS - 4, RADIUS - 4)
        f = p.font()
        f.setPixelSize(15)
        f.setBold(True)
        p.setFont(f)
        pill = QRectF(0, 0, p.fontMetrics().horizontalAdvance(DOC_DROP) + 40, 38)
        pill.moveCenter(r.center())
        p.setBrush(QColor(255, 255, 255, 235))
        p.setPen(QPen(self._accent, 1))
        p.drawRoundedRect(pill, 19, 19)
        p.setPen(self._accent)
        p.drawText(pill, Qt.AlignCenter, DOC_DROP)
        p.end()


class _DragBar(QFrame):
    """Header strip that drags the whole overlay."""

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner
        self._press = None
        self.setCursor(Qt.OpenHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint() - self._owner.frameGeometry().topLeft()
            self.setCursor(Qt.ClosedHandCursor)
            self._owner._drag_began()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None:
            self._owner.move(e.globalPosition().toPoint() - self._press)
            self._owner._guard_crop_screen()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._press is not None:
            self._press = None
            self.setCursor(Qt.OpenHandCursor)
            self._owner._drag_ended()
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        self._owner.set_expanded(not self._owner._expanded)


class LiveAssistOverlay(QWidget):
    # The int is the request generation: a newer question supersedes an answer
    # still streaming, and the stale one's late signals are dropped.
    sig_suggestion = Signal(str, str, int)   # text, error, generation
    sig_status = Signal(str)                 # short footer note from the worker
    sig_partial = Signal(str, int)           # streamed text so far, generation
    sig_export_done = Signal(str, str)       # saved path, error
    # The worker asks the GUI thread to put the one-time "use your documents with the cloud?"
    # question (a dict: gen, managed, route, event, answer - see _documents_for_answer).
    sig_doc_ask = Signal(object)
    # "Apply to everything I hear": the result of one piece, from the worker thread. The last
    # int is the rule generation: Pause, turning the mode off or a new session bumps it, and a
    # stale result is dropped.
    sig_rule_partial = Signal(int, str, int)     # entry id, the result so far, generation
    sig_rule_done = Signal(int, str, str, int)   # entry id, the result, error, generation
    # The card's own state for a live_assist_* key changed (its buttons, a
    # public setter or a cfg sync): (config key, new value). Pages listen.
    settings_changed = Signal(str, object)
    # The Hidden chip's truthful state changed: on | off | failed | partial | unavailable.
    hide_status_changed = Signal(str)

    def __init__(self, main_app=None):
        super().__init__()
        self.app = main_app
        cfg = self.app.cfg if self.app else {}
        self._hide = None                  # the chip's HideStatus (hide_state())
        self._theme_name = cfg.get("live_assist_theme", "light")
        if self._theme_name not in _THEMES:
            self._theme_name = "light"
        self._private = bool(cfg.get("live_assist_private", True))
        # Auto = answer the moment someone asks a question (on by default: an
        # instant companion is the point of the feature).
        self._auto = bool(cfg.get("live_assist_auto_answer", True))
        self._gen = 0                      # bumped by every request
        self._pending_auto = False
        self._qa_history = []              # (asked, answer) of this session
        self._qa_docs = []                 # per answer above: written from the user's documents
        self._audio_folder = None          # last session's meeting folder
        self._audio_ready = False
        self._blur_mode = ""
        self._glass_mode = "flat"          # "liquid" | "acrylic" | "flat"
        self._bd_body = None               # sampled backdrop, strong blur
        self._bd_ring = None               # sampled backdrop, mild blur (refraction)
        self._bd_refr = self._bd_glow = None
        self._spec_pos = QPointF(SHADOW + 120, SHADOW + 8)
        self._expanded = True
        self._meeting_active = False
        self._live_text = ""
        self._summary = ""
        self._suggesting = False
        self._suggest_started = 0.0
        self._last_suggest_at = 0.0
        self._text_since_suggest = 0
        self._title = ""
        self._attendees = ""
        # The calendar invite this session started from: the agenda the user sees
        # on the INVITE chip and every answer is sent (per session, in memory and in
        # the meeting's own folder; never "About my calls"), and who is on it.
        self._invite_on = False
        self._invite = ""
        self._invite_others = []
        self._image_b64 = ""               # an image attached to the next question
        self._image = None
        self._crop_shots = []              # [(screen, pixmap)] while the crop view is open
        self._crop_idx = 0
        self._crop_safe = None             # index of the card's own monitor in the shots
        # What the user wrote about the call: every answer is shaped by it.
        self._context = clip_context(str(cfg.get("live_assist_context") or ""))
        # "Apply to everything I hear" (live_rules.py): the context run on every new piece of the
        # conversation. Off until the user turns it on; the card and the page follow cfg.
        self._rules_everything = bool(cfg.get(live_rules.RULES_KEY, False))
        self._rule_feed = live_rules.RuleFeed()
        self._rule_budget = live_rules.RuleBudget()
        self._rule_entries = []            # live_rules.Entry, newest first
        self._rule_busy = False            # one request at a time
        self._rule_paused = False
        self._rule_note = ""               # why it is paused
        self._rule_fails = 0               # errors in a row
        self._rule_gen = 0                 # bumped to cancel what is running
        self._rule_next_id = 1
        self._rule_last_start = 0.0
        self._rule_engine = None           # the engine that last applied the rule
        self._view = "feed"                # "feed" | "answer" (a typed question's answer) while a rule runs
        self._docs_dir = os.path.expanduser("~")
        # The documents the user added (live_docs.py): read off the GUI thread, used by
        # answers through the worker (see _documents_for_answer).
        self._doc_queue = None             # live_docs_ui.AddQueue, made with the card
        self._doc_menu = None
        self._doc_foot = (None, "")        # (answer generation, the footer's words about documents)
        self._doc_asking = False
        self._exclusion_ok = False
        self._mac_partial = False          # macOS: sharingType none is set (best effort)
        # Who transcribes the call (engine_choice.live_speech_status): the engine dict the
        # session started with, and the two lines last shown (line, note).
        self._hear_engine = None
        self._hear_shown = ("", "")

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # macOS hides Qt.Tool windows when the app loses focus - which is
        # exactly when the user is in Zoom. No-op elsewhere.
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setWindowTitle("Live Assistant")
        self.setFixedSize(EXPANDED_W, EXPANDED_H)

        self.sig_suggestion.connect(self._on_suggestion)
        self.sig_status.connect(self._set_status)
        self.sig_partial.connect(self._on_partial)
        self.sig_export_done.connect(self._on_export_done)
        self.sig_doc_ask.connect(self._on_doc_ask)
        self.sig_rule_partial.connect(self._on_rule_partial)
        self.sig_rule_done.connect(self._on_rule_done)
        self._first_token_at = 0.0
        self._build()
        self._doc_queue = live_docs_ui.AddQueue(self)
        self._doc_queue.started.connect(self._on_doc_started)
        self._doc_queue.added.connect(self._on_doc_added)
        self._doc_queue.batch_done.connect(self._on_doc_batch_done)
        live_docs_ui.bridge().changed.connect(self._refresh_docs_chip)
        self.setAcceptDrops(True)
        for name in ("input_ask", "txt_summary", "txt_suggestion", "txt_feed", "ctx_edit", "inv_edit"):
            w = getattr(self, name, None)
            if w is not None:              # a file dropped on them reaches the card (a document),
                w.setAcceptDrops(False)    # it is never pasted as a path into what's being typed
        self._refresh_docs_chip()
        # Typing in the context: the offer ("This sounds like a rule...") follows a moment later.
        self._offer_timer = QTimer(self)
        self._offer_timer.setSingleShot(True)
        self._offer_timer.setInterval(350)
        self._offer_timer.timeout.connect(self._on_rules_typed)
        self._feed_timer = QTimer(self)
        self._feed_timer.setSingleShot(True)
        self._feed_timer.setInterval(100)
        self._feed_timer.timeout.connect(self._render_feed)
        self._tip_from_card = False
        self._popup_privacy = _PopupPrivacy(self)
        if QApplication.instance():
            QApplication.instance().installEventFilter(self._popup_privacy)
        self._richify_tooltips()
        self._apply_theme()
        self._on_rules_changed()           # the chip, the offer and the answer area as the saved context says

        pos = cfg.get("live_assist_pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            rects = [(s.availableGeometry().left(), s.availableGeometry().top(),
                      s.availableGeometry().right(), s.availableGeometry().bottom())
                     for s in QApplication.screens()]
            x, y = clamp_to_rects(int(pos[0]), int(pos[1]), EXPANDED_W, EXPANDED_H, rects)
            self.move(x, y)
        else:
            self._default_position()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(1000)
        self._sample_timer = QTimer(self)
        self._sample_timer.timeout.connect(self._sample_backdrop)
        self._sample_timer.setInterval(SAMPLE_MS_REST)
        self._spec_timer = QTimer(self)
        self._spec_timer.timeout.connect(self._track_specular)
        self._spec_timer.setInterval(60)

        # A page or Settings changed one of our keys through app.set_setting:
        # follow it (on_setting_changed never writes back).
        sig = getattr(self.app, "sig_setting_changed", None) if self.app else None
        if sig is not None:
            try:
                sig.connect(self.on_setting_changed)
            except Exception:
                logger.debug("Live Assistant: no settings sync", exc_info=True)
        # A sign-in, a sign-out or a plan change (any thread; the slot runs on
        # the GUI thread) moves the PRO pill beside the title.
        sig = getattr(self.app, "sig_auth_changed", None) if self.app else None
        if sig is not None:
            try:
                sig.connect(self._refresh_plan)
            except Exception:
                logger.debug("Live Assistant: no plan sync", exc_info=True)
        self._refresh_plan()

    # ── build ──
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(SHADOW + 14, SHADOW + 10, SHADOW + 14, SHADOW + 12)
        root.setSpacing(8)

        self.bar = _DragBar(self)
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(4, 2, 0, 2)
        bl.setSpacing(8)
        self.lbl_title = _ElidedLabel("Live Assistant", self.bar)
        bl.addWidget(self.lbl_title)
        # The PRO pill beside the title: shown until the plan is Pro (the card
        # opens for everyone; Start is the Pro action). See _refresh_plan.
        self.lbl_pro = QLabel("PRO", self.bar)
        self.lbl_pro.setObjectName("laPro")
        self.lbl_pro.setAlignment(Qt.AlignCenter)
        self.lbl_pro.setFixedHeight(18)
        self.lbl_pro.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.lbl_pro.setToolTip("Pro feature - Start needs Pro.")
        self.lbl_pro.setAccessibleName("Pro feature")
        bl.addWidget(self.lbl_pro, 0, Qt.AlignVCenter)
        # Idle: a solid Start button (the card's Pro action; the PRO pill
        # beside the title says so). Live: a green timer + a red stop button.
        self.btn_start = QPushButton("Start", self.bar)
        self.btn_start.setObjectName("laStart")
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setToolTip("Start listening to this call (its sound and your "
                                  "microphone).")
        self.btn_start.clicked.connect(self._start_listening)
        bl.addWidget(self.btn_start)
        self.lbl_timer = QLabel("● 00:00", self.bar)
        self.lbl_timer.setObjectName("laTimer")
        self.lbl_timer.hide()
        bl.addWidget(self.lbl_timer)
        self.btn_stop = _IconButton("stop", self.bar, "Stop listening and generate the notes")
        self.btn_stop.clicked.connect(self._stop_listening)
        self.btn_stop.hide()
        bl.addWidget(self.btn_stop)
        # Hide toggle: the incognito mark + "Hidden" when hidden from screen
        # share, an eye + "Visible" when not, amber when it can't be promised.
        # Always truthful (live_context.hide_status).
        self.btn_private = QPushButton("", self.bar)
        self.btn_private.setObjectName("laPrivate")
        self.btn_private.setCursor(Qt.PointingHandCursor)
        self.btn_private.clicked.connect(lambda: self.set_private(not self._private))
        bl.addWidget(self.btn_private)
        # Screen context, ON by default: every answer sees the screen you're
        # working on (the tooltip text is set by _on_screen_toggled).
        self.btn_screen = _IconButton("screen", self.bar, "")
        self.btn_screen.setCheckable(True)
        self.btn_screen.setChecked(
            bool((self.app.cfg if self.app else {}).get("live_assist_screen_auto", True)))
        self.btn_screen.toggled.connect(self._on_screen_toggled)
        self._set_screen_tooltip(self.btn_screen.isChecked())
        bl.addWidget(self.btn_screen)
        bl.addStretch()
        self.btn_theme = _IconButton("theme", self.bar, "Light / dark glass")
        self.btn_theme.clicked.connect(self._toggle_theme)
        bl.addWidget(self.btn_theme)
        self.btn_collapse = _IconButton("collapse", self.bar,
                                        "Collapse to a pill (double-click the bar too)")
        self.btn_collapse.clicked.connect(lambda: self.set_expanded(not self._expanded))
        bl.addWidget(self.btn_collapse)
        self.btn_close = _IconButton("close", self.bar, "Hide (your hotkey brings it back)")
        self.btn_close.clicked.connect(self.hide_overlay)
        bl.addWidget(self.btn_close)
        root.addWidget(self.bar)

        self.body = QWidget(self)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)

        # The calendar invite - only while the session came from one: who is on it
        # and whether it has an agenda. A click swaps it (and the transcript card)
        # for the agenda, editable; the x drops the invite for this session.
        self.inv_chip = _InviteChip(self.body)
        self.inv_chip.clicked.connect(self._open_invite_editor)
        self.inv_chip.dismissed.connect(self._drop_invite)
        self.inv_chip.hide()
        body.addWidget(self.inv_chip)
        self.inv_panel = QWidget(self.body)
        ip = QVBoxLayout(self.inv_panel)
        ip.setContentsMargins(0, 0, 0, 0)
        ip.setSpacing(8)
        inv_head = QHBoxLayout()
        inv_head.setSpacing(6)
        self.lbl_inv_head = QLabel("FROM YOUR INVITE", self.inv_panel)
        inv_head.addWidget(self.lbl_inv_head)
        inv_head.addStretch()
        self.lbl_inv_count = QLabel("", self.inv_panel)
        self.lbl_inv_count.setObjectName("laFoot")
        inv_head.addWidget(self.lbl_inv_count)
        self.btn_inv_clear = QPushButton("Clear", self.inv_panel)
        self.btn_inv_clear.setFixedHeight(22)
        self.btn_inv_clear.setCursor(Qt.PointingHandCursor)
        self.btn_inv_clear.clicked.connect(self._clear_invite_text)
        inv_head.addWidget(self.btn_inv_clear)
        self.btn_inv_done = QPushButton("Done", self.inv_panel)
        self.btn_inv_done.setObjectName("laCtxDone")
        self.btn_inv_done.setFixedHeight(22)
        self.btn_inv_done.setCursor(Qt.PointingHandCursor)
        self.btn_inv_done.setToolTip("Keep it for this session (Ctrl+Enter)"
                                     if sys.platform != "darwin"
                                     else "Keep it for this session (⌘↩)")
        self.btn_inv_done.clicked.connect(self._finish_invite_edit)
        inv_head.addWidget(self.btn_inv_done)
        ip.addLayout(inv_head)
        self.inv_edit = _ContextEdit(self.inv_panel)
        self.inv_edit.setObjectName("laCtxEdit")
        self.inv_edit.setPlaceholderText(INVITE_PLACEHOLDER)
        self.inv_edit.setAccessibleName("Agenda from your invite")
        self.inv_edit.setTabChangesFocus(True)
        self.inv_edit.setFixedHeight(128)
        self.inv_edit.textChanged.connect(self._on_invite_typed)
        self.inv_edit.finished.connect(self._finish_invite_edit)
        ip.addWidget(self.inv_edit)
        self.lbl_inv_hint = QLabel(INVITE_HINT, self.inv_panel)
        self.lbl_inv_hint.setObjectName("laFoot")
        ip.addWidget(self.lbl_inv_hint)
        self.inv_panel.hide()
        body.addWidget(self.inv_panel)

        # Session context: one line showing what the answers are told about
        # this call; a click swaps it (and the transcript card) for an editor.
        self.ctx_chip = _ContextChip(self.body)
        self.ctx_chip.clicked.connect(self._open_context_editor)
        body.addWidget(self.ctx_chip)
        # "This sounds like a rule for everything you hear. Apply it to every sentence?" - the
        # calm offer, under the chip (the editor has its own, under its switch).
        self.rule_offer = _RuleOffer(self.body)
        self.rule_offer.yes.connect(self._offer_yes)
        self.rule_offer.no.connect(self._offer_no)
        body.addWidget(self.rule_offer)
        self.ctx_panel = QWidget(self.body)
        cp = QVBoxLayout(self.ctx_panel)
        cp.setContentsMargins(0, 0, 0, 0)
        cp.setSpacing(6)
        ctx_head = QHBoxLayout()
        ctx_head.setSpacing(6)
        self.lbl_ctx_head = QLabel("CONTEXT", self.ctx_panel)
        ctx_head.addWidget(self.lbl_ctx_head)
        ctx_head.addStretch()
        self.lbl_ctx_count = QLabel("", self.ctx_panel)
        self.lbl_ctx_count.setObjectName("laFoot")
        ctx_head.addWidget(self.lbl_ctx_count)
        self.btn_ctx_clear = QPushButton("Clear", self.ctx_panel)
        self.btn_ctx_clear.setFixedHeight(22)
        self.btn_ctx_clear.setCursor(Qt.PointingHandCursor)
        self.btn_ctx_clear.clicked.connect(self._clear_context_text)
        ctx_head.addWidget(self.btn_ctx_clear)
        self.btn_ctx_done = QPushButton("Done", self.ctx_panel)
        self.btn_ctx_done.setObjectName("laCtxDone")
        self.btn_ctx_done.setFixedHeight(22)
        self.btn_ctx_done.setCursor(Qt.PointingHandCursor)
        self.btn_ctx_done.setToolTip("Lock it in for this session (Ctrl+Enter)"
                                     if sys.platform != "darwin"
                                     else "Lock it in for this session (⌘↩)")
        self.btn_ctx_done.clicked.connect(self._finish_context_edit)
        ctx_head.addWidget(self.btn_ctx_done)
        cp.addLayout(ctx_head)
        self.ctx_edit = _ContextEdit(self.ctx_panel)
        self.ctx_edit.setObjectName("laCtxEdit")
        self.ctx_edit.setPlaceholderText(CONTEXT_PLACEHOLDER)
        self.ctx_edit.setTabChangesFocus(True)
        self.ctx_edit.setFixedHeight(CTX_EDIT_H)
        self.ctx_edit.textChanged.connect(self._on_context_typed)
        self.ctx_edit.finished.connect(self._finish_context_edit)
        # The paper-clip in the box's corner: add documents.
        self.btn_ctx_clip = QPushButton("", self.ctx_edit)
        self.btn_ctx_clip.setObjectName("laClip")
        self.btn_ctx_clip.setFixedSize(26, 22)
        self.btn_ctx_clip.setCursor(Qt.PointingHandCursor)
        self.btn_ctx_clip.setToolTip(ADD_DOCS_TIP)
        self.btn_ctx_clip.setAccessibleName(ADD_DOCS)
        self.btn_ctx_clip.clicked.connect(self._pick_documents)
        self.ctx_edit.attach_button(self.btn_ctx_clip)
        cp.addWidget(self.ctx_edit)
        # Under it: the documents (Add documents, and the count with its "Use in answers" list)...
        docs_row = QHBoxLayout()
        docs_row.setSpacing(6)
        self.btn_ctx_add = QPushButton(ADD_DOCS, self.ctx_panel)
        self.btn_ctx_add.setFixedHeight(22)
        self.btn_ctx_add.setCursor(Qt.PointingHandCursor)
        self.btn_ctx_add.setToolTip(ADD_DOCS_TIP)
        self.btn_ctx_add.clicked.connect(self._pick_documents)
        docs_row.addWidget(self.btn_ctx_add)
        self.ctx_docs_chip = _DocsChip(self.ctx_panel)
        self.ctx_docs_chip.clicked.connect(self._open_docs_menu)
        docs_row.addWidget(self.ctx_docs_chip)
        docs_row.addStretch()
        cp.addLayout(docs_row)
        # ...and the rule's reach: everything you hear, or questions only.
        self.btn_rules = _Switch(live_rules.SWITCH_LABEL, self.ctx_panel)
        self.btn_rules.toggled.connect(self._on_rules_switch)
        cp.addWidget(self.btn_rules, 0, Qt.AlignLeft)
        self.ctx_offer = _RuleOffer(self.ctx_panel)
        self.ctx_offer.yes.connect(self._offer_yes)
        self.ctx_offer.no.connect(self._offer_no)
        cp.addWidget(self.ctx_offer)
        self.ctx_panel.hide()
        body.addWidget(self.ctx_panel)

        self.lbl_now_head = QLabel("NOW", self.body)
        body.addWidget(self.lbl_now_head)
        self.txt_now = QLabel("", self.body)
        self.txt_now.setWordWrap(True)
        self.txt_now.setObjectName("laNow")
        body.addWidget(self.txt_now)
        # Nothing to show before a session - no placeholder copy.
        self.lbl_now_head.hide()
        self.txt_now.hide()

        # The live transcript itself (not a summary): what was just said, so
        # the answer below can be checked against it at a glance.
        self.lbl_sum_head = QLabel("LIVE TRANSCRIPT", self.body)
        body.addWidget(self.lbl_sum_head)
        self.txt_summary = QTextEdit(self.body)
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setObjectName("laCard")
        self.txt_summary.setMaximumHeight(110)
        self.txt_summary.setPlaceholderText(TRANSCRIPT_PLACEHOLDER)
        body.addWidget(self.txt_summary)
        # Who transcribes it, in one line ("Transcribing with Gemini 2.5 Pro · Gusan Cloud"), and
        # the plain hint when local Whisper would be transcribing a language it is poor at.
        self.lbl_hear = QLabel("", self.body)
        self.lbl_hear.setObjectName("laFoot")
        self.lbl_hear.setWordWrap(True)
        self.lbl_hear.setTextFormat(Qt.PlainText)
        self.lbl_hear.setAccessibleName("Speech engine")
        self.lbl_hear.hide()
        body.addWidget(self.lbl_hear)

        head_row = QHBoxLayout()
        self.lbl_sug_head = QLabel("ANSWER", self.body)
        self.lbl_sug_head.setToolTip("AI answers can be wrong - check anything important.")
        head_row.addWidget(self.lbl_sug_head)
        head_row.addStretch()
        # One-tap actions: each is a canned question through the same path.
        self.quick_buttons = []
        for label, question in QUICK_ACTIONS:
            b = QPushButton(label, self.body)
            b.setFixedHeight(22)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(QUICK_TIPS.get(label, ""))
            b.clicked.connect(lambda _=False, q=question: self.suggest(
                q, force_screen=(q == SOLVE_SCREEN)))
            head_row.addWidget(b)
            self.quick_buttons.append(b)
        self.btn_auto = QPushButton("Auto", self.body)
        self.btn_auto.setCheckable(True)
        self.btn_auto.setChecked(self._auto)
        self.btn_auto.setToolTip(AUTO_TIP)
        self.btn_auto.toggled.connect(self._on_auto_toggled)
        self.btn_auto.setFixedHeight(22)
        head_row.addWidget(self.btn_auto)
        body.addLayout(head_row)
        # While the context applies to EVERYTHING heard: its state and Pause above the running
        # feed, which takes the single answer's place (see _apply_rules_view).
        self.feed_bar = QWidget(self.body)
        fb = QHBoxLayout(self.feed_bar)
        fb.setContentsMargins(0, 0, 0, 0)
        fb.setSpacing(6)
        self.lbl_feed_state = _ElidedLabel("", self.feed_bar)
        self.lbl_feed_state.setObjectName("laFoot")
        self.lbl_feed_state.setAccessibleName("Rule state")
        fb.addWidget(self.lbl_feed_state, 1)
        self.btn_feed_back = QPushButton("Live results", self.feed_bar)
        self.btn_feed_back.setFixedHeight(22)
        self.btn_feed_back.setCursor(Qt.PointingHandCursor)
        self.btn_feed_back.setToolTip(_wrap_tip("Back to the running results of your rule."))
        self.btn_feed_back.clicked.connect(lambda: self._set_view("feed"))
        fb.addWidget(self.btn_feed_back)
        self.btn_feed_pause = QPushButton("Pause", self.feed_bar)
        self.btn_feed_pause.setFixedHeight(22)
        self.btn_feed_pause.setCursor(Qt.PointingHandCursor)
        self.btn_feed_pause.clicked.connect(self._toggle_rule_pause)
        fb.addWidget(self.btn_feed_pause)
        self.feed_bar.hide()
        body.addWidget(self.feed_bar)
        self.txt_feed = _AnswerEdit(self.body)
        self.txt_feed.setReadOnly(True)
        self.txt_feed.setObjectName("laCard")
        self.txt_feed.setAccessibleName("Live results")
        self.txt_feed.setPlaceholderText(RULES_IDLE)
        self.txt_feed.hide()
        body.addWidget(self.txt_feed, 1)
        self.txt_suggestion = _AnswerEdit(self.body)
        self.txt_suggestion.setReadOnly(True)
        self.txt_suggestion.setObjectName("laCard")
        self.txt_suggestion.setPlaceholderText(ANSWER_PLACEHOLDER)
        body.addWidget(self.txt_suggestion, 1)

        # What the answers can read: the documents the user added on the Live Assistant page
        # (hidden while there are none). A click opens the list to choose from.
        self.docs_bar = QWidget(self.body)
        db = QHBoxLayout(self.docs_bar)
        db.setContentsMargins(0, 0, 0, 0)
        db.setSpacing(6)
        self.docs_chip = _DocsChip(self.docs_bar)
        self.docs_chip.clicked.connect(self._open_docs_menu)
        db.addWidget(self.docs_chip)
        db.addStretch()
        self.docs_bar.hide()
        body.addWidget(self.docs_bar)

        ask_row = QHBoxLayout()
        ask_row.setSpacing(8)
        self.img_chip = _ImageChip(self.body)
        self.img_chip.clicked.connect(self._clear_image)
        self.img_chip.hide()
        ask_row.addWidget(self.img_chip)
        self.input_ask = _AskEdit(self.body)
        self.input_ask.setPlaceholderText(ASK_PLACEHOLDER)
        self.input_ask.returnPressed.connect(self._ask)
        self.input_ask.image_pasted.connect(self._attach_image)
        ask_row.addWidget(self.input_ask, 1)
        # Snip a part of the screen to send with the question.
        self.btn_snip = _IconButton("snip", self.body,
                                    "Screenshot: crop it right here in the card and send it "
                                    "with your question. Nothing new appears on your screen. "
                                    "Or paste an image with "
                                    + ("⌘V." if sys.platform == "darwin" else "Ctrl+V."))
        self.btn_snip.setFixedSize(34, 34)
        self.btn_snip.clicked.connect(self._start_snip)
        ask_row.addWidget(self.btn_snip)
        # A round send button with a paper-plane icon, like a chat app.
        self.btn_suggest = QPushButton("", self.body)
        self.btn_suggest.setObjectName("laSuggest")
        self.btn_suggest.setCursor(Qt.PointingHandCursor)
        self.btn_suggest.setIcon(_send_icon(18))
        self.btn_suggest.setIconSize(QSize(18, 18))
        self.btn_suggest.setFixedSize(34, 34)
        self.btn_suggest.setToolTip("Send (Enter) - with nothing typed, answers the "
                                    "latest question")
        self.btn_suggest.clicked.connect(self._ask_or_answer)
        ask_row.addWidget(self.btn_suggest)
        body.addLayout(ask_row)

        foot_row = QHBoxLayout()
        foot_row.setSpacing(8)
        self.lbl_status = QLabel("", self.body)
        self.lbl_status.setObjectName("laFoot")
        # Wrap: a long note must never widen the layout past the card.
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextFormat(Qt.PlainText)       # file names appear here: never rich text
        self.lbl_status.setMaximumHeight(32)
        foot_row.addWidget(self.lbl_status, 1)
        # Appears once a session's recording is on disk.
        self.btn_audio = QPushButton("Download audio", self.body)
        self.btn_audio.setFixedHeight(22)
        self.btn_audio.setCursor(Qt.PointingHandCursor)
        self.btn_audio.setToolTip("Save this session's recording (MP3 or WAV). It's also "
                                  "kept with the meeting in Meetings.")
        self.btn_audio.clicked.connect(self._save_audio)
        self.btn_audio.hide()
        foot_row.addWidget(self.btn_audio)
        body.addLayout(foot_row)

        root.addWidget(self.body, 1)

        # Screenshot crop view: replaces the body while it's open (see
        # _start_snip) - the whole flow stays inside this private card.
        self.crop_panel = QWidget(self)
        crl = QVBoxLayout(self.crop_panel)
        crl.setContentsMargins(0, 0, 0, 0)
        crl.setSpacing(8)
        crop_head = QHBoxLayout()
        crop_head.setSpacing(6)
        self.lbl_crop_head = QLabel("SCREENSHOT", self.crop_panel)
        crop_head.addWidget(self.lbl_crop_head)
        crop_head.addStretch()
        self.btn_crop_screen = QPushButton("", self.crop_panel)
        self.btn_crop_screen.setFixedHeight(22)
        self.btn_crop_screen.setCursor(Qt.PointingHandCursor)
        self.btn_crop_screen.setToolTip("Show the next monitor")
        self.btn_crop_screen.clicked.connect(self._crop_next_screen)
        crop_head.addWidget(self.btn_crop_screen)
        self.btn_crop_cancel = QPushButton("Cancel", self.crop_panel)
        self.btn_crop_cancel.setFixedHeight(22)
        self.btn_crop_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_crop_cancel.clicked.connect(self._end_snip)
        crop_head.addWidget(self.btn_crop_cancel)
        self.btn_crop_attach = QPushButton("", self.crop_panel)
        self.btn_crop_attach.setObjectName("laCtxDone")
        self.btn_crop_attach.setFixedHeight(22)
        self.btn_crop_attach.setCursor(Qt.PointingHandCursor)
        self.btn_crop_attach.setToolTip("Attach it to your next question (Enter)")
        self.btn_crop_attach.clicked.connect(self._crop_attach)
        crop_head.addWidget(self.btn_crop_attach)
        crl.addLayout(crop_head)
        self.crop_view = _CropView(self.crop_panel)
        self.crop_view.changed.connect(self._refresh_crop_controls)
        self.crop_view.accepted.connect(self._crop_attach)
        self.crop_view.cancelled.connect(self._end_snip)
        crl.addWidget(self.crop_view)
        self.lbl_crop_hint = QLabel("", self.crop_panel)
        self.lbl_crop_hint.setObjectName("laFoot")
        self.lbl_crop_hint.setWordWrap(True)
        crl.addWidget(self.lbl_crop_hint)
        crl.addStretch(1)
        self.crop_panel.hide()
        root.addWidget(self.crop_panel, 1)

        # Files dragged onto the card become documents: a dashed veil says so.
        self.drop_veil = _DropVeil(self)

        cfg = self.app.cfg if self.app else {}
        try:
            self._opacity = min(1.0, max(0.55, float(cfg.get("live_assist_opacity", 0.96))))
        except (TypeError, ValueError):
            self._opacity = 0.96
        self.setWindowOpacity(self._opacity)

    def _richify_tooltips(self):
        """Qt word-wraps rich-text tooltips but not plain ones: a long plain
        tooltip becomes a single ~1000 px line. Wrap every tooltip in <p>."""
        import html as html_mod
        for w in [self] + self.findChildren(QWidget):
            tip = w.toolTip()
            if tip and not tip.lstrip().startswith("<"):
                w.setToolTip(f"<p style='white-space:normal'>{html_mod.escape(tip)}</p>")

    # ── theme / glass ──
    def _apply_theme(self):
        t = _THEMES[self._theme_name]
        accent = t["accent"]
        ok_bg, ok_fg, ok_line = chip_colors("ok", self._theme_name)
        # The PRO pill (ProTag's purple glass): a lighter rim on dark glass.
        pro_line = "rgba(255,255,255,0.45)" if self._theme_name == "dark" else "rgba(255,255,255,0.7)"
        self.setStyleSheet(f"""
            QLabel {{ color: {t['text']}; background: transparent; font-size: 12px; }}
            QToolTip {{
                background-color: #0f172a; color: #f8fafc; border: 1px solid rgba(255,255,255,0.18);
                border-radius: 6px; padding: 6px 9px; font-size: 12px;
            }}
            QLabel#laNow {{ font-size: 13px; color: {t['text']}; }}
            QTextEdit#laCard {{
                background: {t['card']}; border: 1px solid {t['card_border']};
                border-radius: 10px; color: {t['text']}; font-size: 13.5px; padding: 6px;
            }}
            QLineEdit {{
                background: {t['card']}; border: 1px solid {t['card_border']};
                border-radius: 9px; color: {t['text']}; padding: 6px 10px; font-size: 12.5px;
            }}
            QPushButton {{
                background: {t['card']}; border: 1px solid {t['card_border']};
                border-radius: 11px; color: {t['text']}; padding: 2px 11px; font-size: 12px;
            }}
            QPushButton:hover {{ border-color: {accent}; }}
            QPushButton:checked {{ background: {accent}; color: white; border-color: {accent}; }}
            QPushButton#laSuggest {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3b82f6, stop:1 #8b5cf6);
                border: none; padding: 0; border-radius: 17px;
            }}
            QPushButton#laSuggest:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2563eb, stop:1 #7c3aed);
            }}
            QPushButton#laSuggest:disabled {{ background: #94a3b8; }}
            QPushButton#laStart {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #6366f1);
                color: white; font-weight: 700; border: none; border-radius: 11px;
                padding: 3px 16px; font-size: 12px;
            }}
            QPushButton#laStart:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563eb, stop:1 #4f46e5);
            }}
            QLabel#laPro {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #a855f7, stop:1 #7e22ce);
                color: #ffffff; border: 1px solid {pro_line}; border-radius: 9px;
                padding: 0px 8px; font-size: 10px; font-weight: 700;
            }}
            QLabel#laTimer {{
                color: {ok_fg}; background: {ok_bg}; border: 1px solid {ok_line};
                border-radius: 11px; padding: 2px 10px; font-size: 12px; font-weight: 700;
            }}
            QLabel#laFoot {{ color: {t['faint']}; font-size: 11px; }}
            QLabel#laFoot[emph="true"] {{ color: {t['text']}; font-weight: 600; }}
            QFrame#laCtxChip {{
                background: {t['card']}; border: 1px solid {t['card_border']};
                border-radius: 9px;
            }}
            QFrame#laCtxChip:hover {{ border-color: {accent}; }}
            QFrame#laCtxChip[empty="true"] {{ border-style: dashed; }}
            QLabel#laCtxTag {{
                color: {accent}; font-size: 10px; font-weight: 700; letter-spacing: 1px;
            }}
            QLabel#laCtxText {{ color: {t['text']}; font-size: 12px; }}
            QLabel#laCtxText[empty="true"] {{ color: {t['muted']}; }}
            QLabel#laCtxEditHint {{ color: {accent}; font-size: 11.5px; }}
            QPlainTextEdit#laCtxEdit {{
                background: {t['card']}; border: 1px solid {accent};
                border-radius: 10px; color: {t['text']}; font-size: 12.5px; padding: 4px;
            }}
            QPushButton#laCtxDone {{
                background: {accent}; color: white; border-color: {accent}; font-weight: 600;
            }}
            QPushButton#laDocs {{
                background: {t['card']}; border: 1px solid {t['card_border']};
                border-radius: 12px; color: {t['muted']}; padding: 2px 11px 2px 9px;
                font-size: 11.5px; font-weight: 600;
            }}
            QPushButton#laDocs:hover {{ border-color: {accent}; color: {t['text']}; }}
            QPushButton#laDocs[off="true"] {{ color: {t['faint']}; border-style: dashed; }}
            QLabel#laCtxScope {{
                color: {t['muted']}; font-size: 10px; font-weight: 700;
                border: 1px solid {t['card_border']}; border-radius: 8px; padding: 0px 7px;
            }}
            QLabel#laCtxScope[on="true"] {{
                color: white; background: {accent}; border-color: {accent};
            }}
            QFrame#laRuleOffer {{
                background: {t['card']}; border: 1px solid {accent}; border-radius: 9px;
            }}
            QLabel#laRuleOfferText {{ color: {t['text']}; font-size: 11.5px; }}
            QPushButton#laClip {{ background: transparent; border: none; padding: 0; }}
            QPushButton#laClip:hover {{ background: rgba(37,99,235,0.14); border-radius: 8px; }}
        """)
        self.drop_veil.set_theme(t)
        self.btn_rules.set_theme(t)
        try:
            from ui.components import icons as comp_icons
            self.btn_ctx_clip.setIcon(comp_icons.icon("paperclip", 15, t["muted"], 1.8))
            self.btn_ctx_clip.setIconSize(QSize(15, 15))
            self.btn_ctx_add.setIcon(comp_icons.icon("paperclip", 13, t["muted"], 1.8))
            self.btn_ctx_add.setIconSize(QSize(13, 13))
        except Exception:
            logger.debug("Live Assistant: paper-clip icon", exc_info=True)
        for lbl in (self.lbl_now_head, self.lbl_sum_head, self.lbl_sug_head,
                    self.lbl_ctx_head, self.lbl_inv_head, self.lbl_crop_head):
            lbl.setStyleSheet(f"color: {t['faint']}; font-size: 10px; font-weight: 700; "
                              "letter-spacing: 1px; background: transparent;")
        self.lbl_title.setStyleSheet(f"color: {t['text']}; font-weight: 700; font-size: 13px;")
        for b in (self.btn_theme, self.btn_collapse, self.btn_close, self.btn_screen,
                  self.btn_stop, self.btn_snip, self.inv_chip.drop):
            b.set_theme(t)
        self._shadow_key = None          # shadow tint depends on theme
        self._refresh_private_chip()
        self._refresh_live_controls()
        self._refresh_docs_chip()        # its little icon wears the theme's ink
        self.update()

    def _toggle_theme(self):
        self.set_theme("dark" if self._theme_name == "light" else "light")

    # ── public settings (the Live Assistant page, Settings, the card itself) ──
    def _store(self, key, value):
        """Write one live_assist_* key in place and save it (debounced when the
        app can), then announce it on app.sig_setting_changed. Nothing happens
        when cfg already holds the value - so a cfg sync never writes back."""
        app = self.app
        if not app:
            return False
        cfg = app.cfg
        if key in cfg and cfg.get(key) == value:
            return False
        cfg[key] = value
        save = getattr(app, "schedule_config_save", None)
        try:
            (save if callable(save) else app.save_config)()
        except Exception:
            logger.warning("Saving %s failed", key, exc_info=True)
        sig = getattr(app, "sig_setting_changed", None)
        if sig is not None:
            try:
                sig.emit(key)
            except Exception:
                logger.debug("announcing %s failed", key, exc_info=True)
        return True

    def set_theme(self, name):
        """Card look: "light" or "dark" (anything else is light)."""
        name = name if name in _THEMES else "light"
        if name != self._theme_name:
            self._theme_name = name
            self._apply_theme()
            self._apply_glass()
            self.settings_changed.emit("live_assist_theme", name)
        self._store("live_assist_theme", name)

    def set_auto_answer(self, on):
        """Answer questions by itself (the Auto button)."""
        on = bool(on)
        if self.btn_auto.isChecked() != on:
            self.btn_auto.setChecked(on)       # -> _on_auto_toggled
        else:
            self._on_auto_toggled(on)

    def set_screen_auto(self, on):
        """Let it see my screen (the screen button)."""
        on = bool(on)
        if self.btn_screen.isChecked() != on:
            self.btn_screen.setChecked(on)     # -> _on_screen_toggled
        else:
            self._store("live_assist_screen_auto", on)

    def set_context(self, text):
        """Context: the standing instructions every answer follows (about the call, or a rule)."""
        if not self.ctx_panel.isHidden():
            # The editor is open: what's typed there would overwrite this on Done.
            self.ctx_edit.blockSignals(True)
            self.ctx_edit.setPlainText(clip_context(text))
            self.ctx_edit.blockSignals(False)
        self._set_context(text)

    def set_opacity(self, value):
        """Card opacity, 0.55-1.0 (live_assist_opacity)."""
        try:
            value = min(1.0, max(0.55, float(value)))
        except (TypeError, ValueError):
            value = 0.96
        # Compared with what was set, not windowOpacity(): Qt keeps it in 1/255
        # steps, so 0.96 reads back as 0.957 and would look changed every time.
        if abs(getattr(self, "_opacity", -1.0) - value) > 1e-6:
            self._opacity = value
            self.setWindowOpacity(value)
            self.settings_changed.emit("live_assist_opacity", value)
        self._store("live_assist_opacity", value)

    def reset_position(self):
        """Back to the default spot (top right of the main screen)."""
        if getattr(self, "_resetting", False):
            return                             # our own announcement coming back
        self._resetting = True
        try:
            self._default_position()
            self.settings_changed.emit("live_assist_pos", None)
            self._store("live_assist_pos", None)
        finally:
            self._resetting = False

    def on_setting_changed(self, key):
        """app.sig_setting_changed: a page or Settings changed ``key`` in cfg.
        Follow it; never writes back (the value is already there)."""
        if self.app and key in HEAR_KEYS:
            self._refresh_hear()           # who transcribes (and the language hint) follows
        if not self.app or not str(key).startswith("live_assist_"):
            return
        cfg = self.app.cfg
        try:
            if key == "live_assist_theme":
                self.set_theme(cfg.get(key, "light"))
            elif key == "live_assist_private":
                if bool(cfg.get(key, True)) != self._private:
                    self.set_private(cfg.get(key, True))
            elif key == "live_assist_auto_answer":
                self.set_auto_answer(cfg.get(key, True))
            elif key == "live_assist_screen_auto":
                self.set_screen_auto(cfg.get(key, True))
            elif key == "live_assist_context":
                if clip_context(str(cfg.get(key) or "")) != self._context:
                    self.set_context(str(cfg.get(key) or ""))
            elif key == live_rules.RULES_KEY:
                if bool(cfg.get(key, False)) != self._rules_everything:
                    self.set_rules_everything(cfg.get(key, False))
            elif key == live_rules.DECLINED_KEY:
                self._refresh_offers()
            elif key == "live_assist_opacity":
                self.set_opacity(cfg.get(key, 0.96))
            elif key == "live_assist_pos" and cfg.get(key) is None:
                self.reset_position()          # "Reset card position" on the page
        except Exception:
            logger.warning("Live Assistant: couldn't follow %s", key, exc_info=True)

    def hide_state(self):
        """The chip's truthful HideStatus (what this window really got)."""
        if self._hide is None:
            self._refresh_private_chip()
        return self._hide

    def _apply_glass(self):
        """Capture exclusion first (it decides whether sampling the backdrop is
        safe), then the glass tier. Called after every show(): Qt can recreate
        the native window when flags change, and the effects live on the HWND."""
        if not self.isVisible():
            return
        self._apply_private()
        if glass.IS_WINDOWS and self._exclusion_ok:
            # Liquid: we paint the (blurred, refracted) backdrop ourselves from
            # a screen sample that cannot contain this window.
            if self._glass_mode != "liquid":
                glass.remove_backdrop_blur(self)
                self._blur_mode = ""
            self._glass_mode = "liquid"
            self._sample_backdrop()
            self._sample_timer.start()
            self._spec_timer.start()
        else:
            # Visible in shares (or exclusion unavailable): the backdrop can't be
            # sampled, so paint a darker rounded plate ourselves. Not the OS
            # acrylic - Windows blurs the whole square window, shadow margin
            # included, which drew a rectangle around the rounded card.
            self._sample_timer.stop()
            self._spec_timer.stop()
            self._bd_body = self._bd_ring = self._bd_refr = self._bd_glow = None
            if self._blur_mode:
                glass.remove_backdrop_blur(self)
                self._blur_mode = ""
            self._glass_mode = "flat"
        self.update()

    def _apply_private(self):
        remote = glass.is_remote_session()
        supported = glass.capture_exclusion_supported()
        excluded = False
        self._mac_partial = False
        if (getattr(self, "_crop_shots", None) and self._crop_idx != self._crop_safe
                and not (self._private and supported and not remote)):
            self._crop_to_safe()               # before the exclusion is lifted
        if self._private and supported and not remote:
            excluded = glass.exclude_from_capture(self, True)
        elif self._private and glass.IS_MAC:
            # macOS: some capture paths honour it (the same flag Electron's
            # setContentProtection sets) - not all, so the chip says "May be
            # hidden", never "Private".
            self._mac_partial = bool(glass.exclude_from_capture(self, True)) and not remote
        else:
            glass.exclude_from_capture(self, False)
        self._exclusion_ok = excluded
        self._refresh_private_chip(remote, supported, excluded)
        if getattr(self, "_crop_shots", None):
            self._refresh_crop_controls()      # privacy changed mid-crop

    def _guard_popup(self, w):
        """A tooltip/menu about to show: hidden like the card when it belongs
        to the card and the card is hidden; restored if it was ours before."""
        parent = w.parentWidget()
        if parent is not None:
            mine = parent.window() is self
        else:
            mine = w.windowType() == Qt.ToolTip and self._tip_from_card
        hide = mine and self._private and (self._exclusion_ok or self._mac_partial)
        if hide or w.property("la_private"):
            try:
                w.winId()                      # the native window, before it maps
                glass.exclude_from_capture(w, hide)
                w.setProperty("la_private", hide)
            except RuntimeError:
                pass                           # already being deleted

    def _guard_visible_tips(self):
        for w in QApplication.topLevelWidgets():
            if w.windowType() == Qt.ToolTip and w.isVisible():
                self._guard_popup(w)

    def set_private(self, on):
        """Hide from screen share (live_assist_private). Always re-applies the
        exclusion: settings_apply calls this right after writing cfg."""
        on = bool(on)
        changed = on != self._private
        self._private = on
        self._store("live_assist_private", on)
        # Sampling is only safe while excluded, so the glass tier may change.
        self._apply_glass()
        if not self.isVisible():
            self._refresh_private_chip()        # what showing it will do
        if changed:
            self.settings_changed.emit("live_assist_private", on)

    def _refresh_private_chip(self, remote=None, supported=None, excluded=None):
        if remote is None:
            remote = glass.is_remote_session()
        if supported is None:
            supported = glass.capture_exclusion_supported()
        if excluded is None and self.isVisible():
            excluded = glass.is_excluded_from_capture(self)
        mac = sys.platform == "darwin"
        partial = None                          # hidden card: hide_status predicts
        if excluded is not None:
            # macOS: hiding can't be promised, but the flag may well be set.
            partial = mac and not supported and bool(excluded or self._mac_partial)
        st = hide_status(self._private, excluded=excluded, partial=partial,
                         supported=supported, remote=remote, mac=mac)
        bg, fg, border = chip_colors(st.tone, self._theme_name)
        try:
            from ui.icons import eye_icon, incognito_icon
            # The incognito mark while hidden, an eye (slashed: maybe) otherwise.
            if st.icon == "incognito":
                icon = incognito_icon(16, QColor(fg))
            else:
                icon = eye_icon(open_=(st.icon == "eye"), size=16, color=QColor(fg))
            self.btn_private.setIcon(icon)
            self.btn_private.setIconSize(QSize(16, 16))
        except Exception:
            logger.debug("chip icon failed", exc_info=True)
        self.btn_private.setText(st.text)
        self.btn_private.setAccessibleName(st.headline)
        self.btn_private.setStyleSheet(
            f"QPushButton#laPrivate {{ background: {bg}; color: {fg}; border: 1px solid "
            f"{border}; border-radius: 11px; padding: 2px 10px 2px 7px; font-size: 12px; "
            "font-weight: 600; }")
        tip = st.tip + (" " + _CHIP_CLICK[st.key] if st.key in _CHIP_CLICK else "")
        # Rich text so Qt wraps it (a plain tooltip is one ~1000 px line).
        import html as html_mod
        self.btn_private.setToolTip("<p style='white-space:normal'>"
                                    + html_mod.escape(tip, quote=False) + "</p>")
        old, self._hide = self._hide, st
        if old is None or old.key != st.key:
            self.hide_status_changed.emit(st.key)

    # ── who transcribes the call ──
    def _hear_lines(self):
        """(line, note) for the label under the live transcript. While a session runs: what the
        recorder says RAN ("Transcribing with Gemini 2.5 Pro · Gusan Cloud", engine_choice.
        live_speech_status) and, for a session started here, the plain hint when local Whisper
        is poor at the call's language. Before one: only that hint, for what Start would use."""
        app = self.app
        if not app:
            return "", ""
        try:
            import engine_choice
            cfg = app.cfg
            pro = bool(app.is_pro()) if callable(getattr(app, "is_pro", None)) else False
            signed_in = bool(getattr(getattr(app, "auth", None), "is_authenticated", pro))
            if not self._meeting_active:
                ro = engine_choice.live_speech_engine(cfg, pro, signed_in, self._call_language())
                return "", ro.get("note") or ""
            rec = getattr(app, "recorder", None)
            getter = getattr(rec, "speech_report", None)
            report = (getter() if callable(getter) else None) or {}
            ro = engine_choice.take_engine(cfg, report)
            session = getattr(app, "meeting_session", None)
            note = ""
            if getattr(session, "source", "") == "live_assist":
                note = engine_choice.live_speech_note(cfg, pro, report.get("language"), signed_in,
                                                      engine=ro, report=report)
            return engine_choice.live_speech_status(ro, report), note
        except Exception:
            logger.debug("Live Assistant: transcriber line unavailable", exc_info=True)
            return "", ""

    def _refresh_hear(self):
        """Show who transcribes the call (and the language hint); only when it changed."""
        line, note = self._hear_lines()
        if (line, note) == self._hear_shown:
            return
        self._hear_shown = (line, note)
        text = "\n".join(x for x in (line, note) if x)
        self.lbl_hear.setText(text)
        self.lbl_hear.setToolTip(text)
        self.lbl_hear.setVisible(bool(text))

    def _refresh_live_controls(self):
        live = self._meeting_active
        self.btn_start.setVisible(not live)
        self.lbl_timer.setVisible(live)
        self.btn_stop.setVisible(live)
        if live:
            self._update_timer()
        self._refresh_title()
        self._refresh_invite_chip()
        self._refresh_context_chip()

    def _update_timer(self):
        since = getattr(self, "_live_since", None)
        if not since:
            return
        s = int(time.time() - since)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        self.lbl_timer.setText(("● %d:%02d:%02d" % (h, m, sec)) if h else ("● %02d:%02d" % (m, sec)))

    def _drag_began(self):
        if self._glass_mode == "liquid":
            self._sample_timer.setInterval(SAMPLE_MS_DRAG)
        elif self._blur_mode and glass.windows_build() < 22000:
            # Acrylic lags behind a moving window on Windows 10; lift it.
            glass.remove_backdrop_blur(self)

    def _guard_crop_screen(self):
        """A crop is open while a share can see the card, and the card moved
        to another monitor: the screenshot it shows (of the monitor it left)
        must not appear in a share of this one."""
        if not getattr(self, "_crop_shots", None) or self._exclusion_ok:
            return
        safe = self._crop_safe
        shot_screen = self._crop_shots[safe][0] if safe is not None else None
        if self.screen() is not shot_screen:
            self._end_snip()
            self._set_status("Screenshot closed - the card moved to another screen.")

    def _drag_ended(self):
        self._guard_crop_screen()
        if self.app:
            # The same path as the setters: saved (debounced) and announced, so
            # the page's "Reset card position" knows the card was moved.
            pos = [self.x(), self.y()]
            if self.app.cfg.get("live_assist_pos") != pos:
                self.settings_changed.emit("live_assist_pos", pos)
            self._store("live_assist_pos", pos)
        if self._glass_mode == "liquid":
            self._sample_timer.setInterval(SAMPLE_MS_REST)
            self._sample_backdrop()
        elif self._blur_mode and glass.windows_build() < 22000:
            self._apply_glass()

    # ── liquid glass: sample what is behind the window ──
    def _sample_backdrop(self):
        # Keeps sampling during a video hole too, so it returns to liquid glass.
        if self._glass_mode not in ("liquid", "liquid-video") or not self.isVisible():
            return
        try:
            scr = self.screen() or QApplication.primaryScreen()
            if scr is None:
                return
            g, sg = self.frameGeometry(), scr.geometry()
            pm = scr.grabWindow(0, g.x() - sg.x(), g.y() - sg.y(), g.width(), g.height())
            if pm.isNull():
                return
            img = pm.toImage()
            w, h = max(1, self.width()), max(1, self.height())
            # Down-then-up scaling is a cheap, good-looking blur: /12 for the
            # body (soft), /5 for the refraction ring (content stays legible).
            body = img.scaled(max(1, w // 12), max(1, h // 12), Qt.IgnoreAspectRatio,
                              Qt.SmoothTransformation)
            if _looks_like_capture_hole(body):
                # Hardware-accelerated video (a call's webcam strip, a player)
                # comes back from BitBlt as solid BLACK. Refracting that paints
                # a black bar through the glass. Paint the plain rounded plate
                # while it's there (not the OS acrylic: that blurs the square
                # window and shows a rectangle); the next sample re-tests.
                if self._glass_mode == "liquid":
                    self._glass_mode = "liquid-video"
                    self._bd_body = self._bd_ring = self._bd_refr = self._bd_glow = None
                    self.update()
                return
            if self._glass_mode == "liquid-video":
                self._glass_mode = "liquid"
            body = body.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            ring = img.scaled(max(1, w // 5), max(1, h // 5), Qt.IgnoreAspectRatio,
                              Qt.SmoothTransformation)
            ring = ring.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            self._bd_refr, self._bd_glow = self._masked_refraction(ring, w, h)
            self._bd_body, self._bd_ring = body, ring
            self.update()
        except Exception as e:
            logger.debug("backdrop sample failed: %s", e)

    def _masked_refraction(self, ring, w, h):
        """The edge refraction as a pre-composed layer: the backdrop magnified
        about the centre, kept only near the card edge through a BLURRED mask
        that fades to nothing toward the middle. A hard ring boundary read as
        a stripe (very visibly on the 52 px pill); a soft mask reads as thick
        glass bending what's behind it. Returns (refraction, edge_glow)."""
        card = QRectF(SHADOW, SHADOW, w - 2 * SHADOW, h - 2 * SHADOW)
        edge = max(4.0, min(22.0, card.height() / 2 - 3, card.width() / 2 - 3))
        mask = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        mask.fill(Qt.transparent)
        mp = QPainter(mask)
        mp.setRenderHint(QPainter.Antialiasing, True)
        outer = QPainterPath()
        outer.addRoundedRect(card, RADIUS, RADIUS)
        inner = QPainterPath()
        inner.addRoundedRect(card.adjusted(edge, edge, -edge, -edge),
                             max(2.0, RADIUS - edge * 0.6), max(2.0, RADIUS - edge * 0.6))
        mp.fillPath(outer.subtracted(inner), QColor(255, 255, 255, 255))
        mp.end()
        small = mask.scaled(max(1, w // 6), max(1, h // 6), Qt.IgnoreAspectRatio,
                            Qt.SmoothTransformation)
        mask = small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

        refr = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        refr.fill(Qt.transparent)
        rp = QPainter(refr)
        rp.setRenderHint(QPainter.SmoothPixmapTransform, True)
        cx, cy = w / 2.0, h / 2.0
        rp.translate(cx, cy + 2)
        rp.scale(1.09, 1.09)
        rp.translate(-cx, -cy)
        rp.drawImage(QRect(0, 0, w, h), ring)
        rp.resetTransform()
        rp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        rp.drawImage(0, 0, mask)
        rp.end()

        glow = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        glow.fill(QColor(255, 255, 255, 34))
        gp = QPainter(glow)
        gp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        gp.drawImage(0, 0, mask)
        gp.end()
        return refr, glow

    def _track_specular(self):
        """The highlight drifts toward the cursor while it is over the card -
        the 'light moves across the glass' cue."""
        target = QPointF(SHADOW + CARD_W * 0.25, SHADOW + 8)
        try:
            gp = QCursor.pos()
            if self.frameGeometry().contains(gp):
                lp = self.mapFromGlobal(gp)
                target = QPointF(lp.x(), lp.y())
        except Exception:
            pass
        dx, dy = target.x() - self._spec_pos.x(), target.y() - self._spec_pos.y()
        if abs(dx) + abs(dy) > 0.8:
            self._spec_pos = QPointF(self._spec_pos.x() + dx * 0.25,
                                     self._spec_pos.y() + dy * 0.25)
            self.update()

    # ── painting: the glass surface ──
    def _shadow_image(self):
        """A genuinely blurred drop shadow (rendered once per size/theme):
        paint the card silhouette, then down/up-scale it - the same cheap
        blur as the backdrop. Rings of strokes showed their steps at the
        corners; this doesn't."""
        key = (self.width(), self.height(), self._theme_name)
        if getattr(self, "_shadow_key", None) == key:
            return self._shadow_img
        w, h = self.width(), self.height()
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 120 if self._theme_name == "light" else 170))
        sp = QPainterPath()
        sp.addRoundedRect(QRectF(SHADOW + 2, SHADOW + 7, w - 2 * SHADOW - 4, h - 2 * SHADOW - 4),
                          RADIUS, RADIUS)
        p.drawPath(sp)
        p.end()
        small = img.scaled(max(1, w // 7), max(1, h // 7), Qt.IgnoreAspectRatio,
                           Qt.SmoothTransformation)
        self._shadow_img = small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._shadow_key = key
        return self._shadow_img

    def paintEvent(self, event):
        t = _THEMES[self._theme_name]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        card = QRectF(SHADOW + 0.5, SHADOW + 0.5,
                      self.width() - 2 * SHADOW - 1, self.height() - 2 * SHADOW - 1)
        path = QPainterPath()
        path.addRoundedRect(card, RADIUS, RADIUS)

        p.drawImage(0, 0, self._shadow_image())

        p.save()
        p.setClipPath(path)
        liquid = self._glass_mode == "liquid" and self._bd_body is not None
        if liquid:
            p.drawImage(self.rect(), self._bd_body)
            # Edge refraction, pre-composed with a soft mask in
            # _masked_refraction - no boundary anywhere, at any card size.
            refr = getattr(self, "_bd_refr", None)
            if refr is not None:
                p.drawImage(0, 0, refr)
                p.drawImage(0, 0, self._bd_glow)
            p.fillPath(path, t["tint_liquid"])
        else:
            p.fillPath(path, t["tint_blur"] if self._blur_mode else t["tint_flat"])
        # Specular: a radial highlight that follows the cursor.
        rad = QRadialGradient(self._spec_pos, card.width() * 0.55)
        rad.setColorAt(0.0, QColor(255, 255, 255, t["spec"]))
        rad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, QBrush(rad))
        # Top sheen.
        sheen = QLinearGradient(card.topLeft(), QPointF(card.left(), card.top() + 80))
        sheen.setColorAt(0.0, QColor(255, 255, 255, 60 if self._theme_name == "light" else 26))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, QBrush(sheen))
        p.restore()

        # Rim light, clipped INSIDE the shape so it never doubles up with the
        # outer hairline: brightest at the top-left edge, fading around, with
        # a faint lift again at the bottom-right (light bouncing back in).
        p.save()
        p.setClipPath(path)
        rim = QLinearGradient(card.topLeft(), card.bottomRight())
        light = self._theme_name == "light"
        rim.setColorAt(0.0, QColor(255, 255, 255, 235 if light else 150))
        rim.setColorAt(0.45, QColor(255, 255, 255, 60 if light else 35))
        rim.setColorAt(1.0, QColor(255, 255, 255, 120 if light else 70))
        p.setPen(QPen(QBrush(rim), 2.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.restore()
        p.setPen(QPen(t["border"], 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    # ── visibility / geometry ──
    def _default_position(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        g = screen.availableGeometry()
        self.move(g.right() - self.width() - 16, g.top() + 70)

    def show_overlay(self):
        self._refresh_plan()                   # the plan may have changed while it was hidden
        self.show()
        self.raise_()
        QTimer.singleShot(0, self._apply_glass)

    # ── plan ──
    def _is_pro(self):
        """True / False for the user's plan; None when the app can't say (no
        is_pro: a bare controller). An error counts as not Pro, like the page."""
        fn = getattr(self.app, "is_pro", None) if self.app else None
        if not callable(fn):
            return None
        try:
            return bool(fn())
        except Exception:
            logger.debug("Live Assistant: is_pro failed", exc_info=True)
            return False

    def _refresh_plan(self):
        """The PRO pill beside the title: there until the plan is Pro."""
        self.lbl_pro.setVisible(self._is_pro() is False)
        self._refresh_hear()               # what Start would transcribe with follows the plan

    def _pro_blocks(self):
        """Free and guests: Start spends the month's one Live Assistant session.

        True when it blocked. A controller without claim_live_session (tests,
        and anything that can't ask the server) still opens the Pro window.
        Pro, and a controller that can't say its plan, are not blocked."""
        if self._is_pro() is not False:
            return False
        claim = getattr(self.app, "claim_live_session", None)
        if callable(claim):
            try:
                result = claim()
            except Exception:
                logger.debug("Live Assistant: session claim failed", exc_info=True)
                result = {"ok": False, "reason": "unavailable"}
            if isinstance(result, dict) and result.get("ok"):
                return False
            if isinstance(result, dict) and result.get("reason") == "signed_out":
                gate = getattr(self.app, "show_auth_gate", None)
                if callable(gate):
                    gate()
                    return True
            if isinstance(result, dict) and result.get("reason") == "unavailable":
                self._set_status("Couldn't check your free session. Try again.")
                return True
            if isinstance(result, dict) and result.get("reason") == "allowance_used":
                if hasattr(self.app, "_pro_upsell"):
                    self.app._pro_upsell("Live Assistant session")
                return True
        if hasattr(self.app, "_pro_upsell"):
            self.app._pro_upsell("Live Assistant")
        return True

    def hide_overlay(self):
        self._end_snip()
        self._sample_timer.stop()
        self._spec_timer.stop()
        self.hide()

    def toggle(self):
        if self.isVisible():
            self.hide_overlay()
        else:
            self.open_card()

    def open_card(self):
        """Show the card, never hide it (the page's Open, the tray, a calendar
        reminder). Tracks live_prompter_opened only when it was hidden."""
        was_hidden = not self.isVisible()
        self.show_overlay()
        if was_hidden and self.app and hasattr(self.app, "track"):
            self.app.track("live_prompter_opened")

    def hideEvent(self, event):
        self._end_snip()                       # no stale screenshot next time
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_glass)
        self._refresh_hear()

    def set_expanded(self, expanded):
        self._expanded = bool(expanded)
        if not self._expanded:
            self._end_snip()
        self.body.setVisible(self._expanded)
        self.setFixedSize(EXPANDED_W if self._expanded else COMPACT_W,
                          EXPANDED_H if self._expanded else COMPACT_H)
        self.btn_collapse.set_alt(not self._expanded)
        self._shadow_key = None
        QTimer.singleShot(0, self._apply_glass)

    # ── session context ──
    def _refresh_context_chip(self):
        chip, ctx = self.ctx_chip, self._context
        locked = bool(ctx) and self._meeting_active
        everything = self.rules_active()
        chip.lock.setVisible(locked)
        if locked:
            chip.lock.setPixmap(_lock_pixmap(12, _THEMES[self._theme_name]["accent"]))
        chip.tag.setVisible(bool(ctx))
        chip.tag.setText("LOCKED IN" if locked else "CONTEXT")
        chip.text.setText(" ".join(ctx.split())[:300] if ctx else CONTEXT_EMPTY)
        chip.edit_hint.setVisible(bool(ctx))
        # Plainly which it is: the rule runs on everything heard, or only shapes the answers.
        chip.scope.setVisible(bool(ctx))
        chip.scope.setText(live_rules.scope_text(everything))
        empty = "false" if ctx else "true"
        for w in (chip, chip.text):
            if w.property("empty") != empty:
                w.setProperty("empty", empty)          # restyle: dashed / muted
                w.style().unpolish(w)
                w.style().polish(w)
        on = "true" if everything else "false"
        if chip.scope.property("on") != on:
            chip.scope.setProperty("on", on)
            chip.scope.style().unpolish(chip.scope)
            chip.scope.style().polish(chip.scope)
        if not ctx:
            tip = ("Tell the assistant what this call is about - who you are, who "
                   "you're talking to, what you want from it. Every answer this "
                   "session uses it.")
            chip.setToolTip(f"<p style='white-space:normal'>{tip}</p>")
            return
        import html as html_mod
        head = ("<b>Locked in for this session</b> - every answer uses it. Click to change it."
                if locked else
                "<b>Session context</b> - every answer uses it until you change it. "
                "Click to edit.")
        shown = ctx if len(ctx) <= 700 else ctx[:700] + "…"
        reach = html_mod.escape(RULES_TIP_ON if everything else RULES_TIP_OFF, quote=False)
        chip.setToolTip(f"<p style='white-space:normal'>{head}</p>"
                        f"<p style='white-space:pre-wrap'>{html_mod.escape(shown)}</p>"
                        f"<p style='white-space:normal'>{reach}</p>")

    def _open_context_editor(self):
        if not self.ctx_panel.isHidden():
            return
        self._finish_invite_edit()             # one editor at a time
        self.ctx_edit.blockSignals(True)
        self.ctx_edit.setPlainText(self._context)
        self.ctx_edit.blockSignals(False)
        self._on_context_typed()
        self.ctx_chip.hide()
        self.lbl_sum_head.hide()
        self.txt_summary.hide()
        self.lbl_hear.hide()
        self.ctx_panel.show()
        self._refresh_docs_chip()              # the editor has its own row: the bar under the answer gives way
        self._sync_rules_switch()
        self._refresh_offers()
        self.activateWindow()                  # typing needs the keyboard focus
        self.ctx_edit.setFocus()
        self.ctx_edit.moveCursor(self.ctx_edit.textCursor().MoveOperation.End)

    def _on_context_typed(self):
        text = self.ctx_edit.toPlainText()
        if len(text) > CONTEXT_CHARS:
            # A pasted document: keep what the model will get.
            pos = min(self.ctx_edit.textCursor().position(), CONTEXT_CHARS)
            self.ctx_edit.blockSignals(True)
            self.ctx_edit.setPlainText(text[:CONTEXT_CHARS])
            self.ctx_edit.blockSignals(False)
            cur = self.ctx_edit.textCursor()
            cur.setPosition(pos)
            self.ctx_edit.setTextCursor(cur)
            text = text[:CONTEXT_CHARS]
        n = len(text)
        self.lbl_ctx_count.setText(f"{n:,} / {CONTEXT_CHARS:,}"
                                   if n >= CONTEXT_CHARS * 0.8 else "")
        timer = getattr(self, "_offer_timer", None)
        if timer is not None:
            timer.start()                      # the switch and the offer follow what is typed

    def _clear_context_text(self):
        self.ctx_edit.clear()
        self.ctx_edit.setFocus()

    def _finish_context_edit(self):
        if self.ctx_panel.isHidden():
            return
        self.ctx_panel.hide()
        self.ctx_chip.show()
        self.lbl_sum_head.show()
        self.txt_summary.show()
        self.lbl_hear.setVisible(bool(self.lbl_hear.text()))
        self._set_context(self.ctx_edit.toPlainText())
        self._refresh_docs_chip()
        self._on_rules_changed()               # the switch, the chip and the offer, now the editor is closed

    def _set_context(self, text):
        text = clip_context(text)
        if text == self._context:
            return
        old = self._context
        self._context = text
        self.settings_changed.emit("live_assist_context", text)
        self._store("live_assist_context", text)
        self._on_rules_changed()
        # "Apply to everything I hear" must not outlive its rule: cleared, or no longer a rule, it
        # turns itself off and says so once; a different rule that stays on says so too.
        turn_off, said = live_rules.reconcile(self._rules_everything, text, old)
        if turn_off:
            self.set_rules_everything(False)
            self._set_status(said)
        elif not text:
            self._set_status("Context cleared.")
        elif self.rules_active():
            # A rule for everything heard changes at the next sentence: the one in flight is not recalled.
            self._set_status("✓ Rule updated - it applies from the next sentence." if self._meeting_active
                             else "✓ Rule saved - it applies to everything you hear.")
        elif self._meeting_active:
            self._set_status("✓ Context locked in - every answer from now on uses it.")
        else:
            self._set_status("✓ Context saved - every answer uses it.")

    # ── Context = standing instructions: "Apply to everything I hear" (live_rules.py) ──
    def rules_everything(self):
        """True while the context is set to apply to everything heard (cfg
        live_assist_rules_everything), whether or not a rule is written yet."""
        return bool(self._rules_everything)

    def rules_active(self):
        """The mode is really on: switched on AND there is a rule to apply."""
        return bool(self._rules_everything and self._context)

    def set_rules_everything(self, on):
        """"Apply to everything I hear" (live_assist_rules_everything): updates the card at
        once, writes the key, announces it. On, it starts with the next sentence heard."""
        on = bool(on)
        changed = on != self._rules_everything
        self._rules_everything = on
        if changed:
            if on:
                self._rule_paused, self._rule_note, self._rule_fails = False, "", 0
                self._rule_budget.reset()
            else:
                self._cancel_rules()
            self.settings_changed.emit(live_rules.RULES_KEY, on)
            self._set_status("✓ Your rule now applies to everything you hear, from the next sentence."
                             if on else "Back to questions only.")
        self._store(live_rules.RULES_KEY, on)
        self._on_rules_changed()

    def _on_rules_switch(self, checked):
        self.set_rules_everything(checked)

    def _editing_context_text(self):
        """The context as it stands: what is typed while the editor is open, else what is locked in."""
        return self.ctx_edit.toPlainText() if not self.ctx_panel.isHidden() else self._context

    def _on_rules_changed(self):
        """The mode, the context or the declined mark changed: every place that shows it follows."""
        self._sync_rules_switch()
        self._refresh_context_chip()
        self._refresh_offers()
        self._apply_rules_view()

    def _on_rules_typed(self):
        self._sync_rules_switch()
        self._refresh_offers()

    def _sync_rules_switch(self):
        has = bool(self._editing_context_text().strip())
        self.btn_rules.blockSignals(True)
        self.btn_rules.setChecked(self._rules_everything)
        self.btn_rules.blockSignals(False)
        self.btn_rules.setEnabled(has)
        self.btn_rules.setToolTip(_wrap_tip(RULES_TIP_ON if self._rules_everything else RULES_TIP_OFF)
                                  if has else _wrap_tip(NEEDS_RULE_TEXT))
        self.btn_rules.update()

    def _offer_wanted(self):
        """The text reads like a rule for everything heard, the mode is off, and the user hasn't
        said No to this very text."""
        text = clip_context(self._editing_context_text())
        if not text or self._rules_everything or not live_rules.looks_like_rule(text):
            return False
        declined = str(((self.app.cfg if self.app else {}) or {}).get(live_rules.DECLINED_KEY) or "")
        return live_rules.declined_mark(text) != declined

    def _refresh_offers(self):
        want = self._offer_wanted()
        editing = not self.ctx_panel.isHidden()
        self.ctx_offer.setVisible(want and editing)
        self.rule_offer.setVisible(want and not editing and self.inv_panel.isHidden())

    def _offer_yes(self):
        self.set_rules_everything(True)

    def _offer_no(self):
        mark = live_rules.declined_mark(clip_context(self._editing_context_text()))
        self.settings_changed.emit(live_rules.DECLINED_KEY, mark)
        self._store(live_rules.DECLINED_KEY, mark)
        self._refresh_offers()

    # ── the calendar invite (this session only; never "About my calls") ──
    def has_invite(self):
        """True while the session came from a calendar invite (the INVITE chip shows)."""
        return bool(self._invite_on)

    def invite_text(self):
        """The agenda every answer of this session is sent with ("" when none)."""
        return self._invite

    def set_invite(self, agenda="", others=None):
        """This session came from a calendar invite: show the INVITE chip with who is
        on it (``others``: their names, without you) and its agenda - the text the user
        can read and edit here and every answer is sent with. Held in memory for this
        session (and, through the session, in the meeting's own folder); it is never
        written to the settings. Called by whoever started the session: the card's own
        Start, the reminder's Record + Live Assistant, the card opened mid-call."""
        self._invite_on = True
        self._invite = invite_context.clip_agenda(agenda)
        self._invite_others = invite_context.cap_names(others) if others is not None else []
        self._refresh_invite_chip()

    def _me_name(self):
        auth = getattr(self.app, "auth", None) if self.app else None
        return str(getattr(auth, "user_name", "") or "").strip()

    def _invite_summary(self):
        """"Anna, David +2 · Agenda": who is on the invite, and whether it has an agenda."""
        names = self._invite_others or others_of(self._attendees, self._me_name())
        label = ""
        if names:
            try:
                from calendar_sync import people_label
                label = people_label(names)
            except Exception:
                label = ", ".join(names[:2]) + (" +%d" % (len(names) - 2) if len(names) > 2 else "")
        return " · ".join(p for p in (label, "Agenda" if self._invite else "") if p)

    def _refresh_title(self):
        """The header: the meeting's own title while a meeting with a real one is on
        ("Live session 14:30" and "Untitled Meeting" keep "Live Assistant")."""
        shown = ""
        if self._meeting_active and not invite_context.is_generic_title(self._title):
            shown = invite_context.one_line(self._title, invite_context.MAX_TITLE_CHARS)
        self.lbl_title.setText(shown or "Live Assistant")
        import html as html_mod
        self.lbl_title.setToolTip(f"<p style='white-space:normal'>{html_mod.escape(shown)}</p>"
                                  if shown else "")

    def _refresh_invite_chip(self):
        chip = self.inv_chip
        chip.setVisible(bool(self._invite_on and self._meeting_active
                             and self.inv_panel.isHidden()))
        summary = self._invite_summary()
        chip.text.setText(summary or "Nothing on the invite")
        empty = "false" if summary else "true"
        for w in (chip, chip.text):
            if w.property("empty") != empty:
                w.setProperty("empty", empty)          # restyle: dashed / muted
                w.style().unpolish(w)
                w.style().polish(w)
        import html as html_mod
        head = ("<b>From your calendar invite</b> - sent with each answer. Click to read or "
                "edit the agenda; the x says it isn't this meeting.")
        shown = self._invite if len(self._invite) <= 500 else self._invite[:500] + "…"
        chip.setToolTip(f"<p style='white-space:normal'>{head}</p>"
                        + (f"<p style='white-space:pre-wrap'>{html_mod.escape(shown)}</p>"
                           if shown else ""))
        self._refresh_offers()

    def _open_invite_editor(self):
        if not self.inv_panel.isHidden():
            return
        self._finish_context_edit()            # one editor at a time
        self.inv_edit.blockSignals(True)
        self.inv_edit.setPlainText(self._invite)
        self.inv_edit.blockSignals(False)
        self._on_invite_typed()
        self.inv_chip.hide()
        self.ctx_chip.hide()
        self.lbl_sum_head.hide()
        self.txt_summary.hide()
        self.inv_panel.show()
        self._refresh_offers()
        self.activateWindow()                  # typing needs the keyboard focus
        self.inv_edit.setFocus()
        self.inv_edit.moveCursor(self.inv_edit.textCursor().MoveOperation.End)

    def _on_invite_typed(self):
        text = self.inv_edit.toPlainText()
        if len(text) > INVITE_CHARS:
            # A pasted document: keep what the model will get.
            pos = min(self.inv_edit.textCursor().position(), INVITE_CHARS)
            self.inv_edit.blockSignals(True)
            self.inv_edit.setPlainText(text[:INVITE_CHARS])
            self.inv_edit.blockSignals(False)
            cur = self.inv_edit.textCursor()
            cur.setPosition(pos)
            self.inv_edit.setTextCursor(cur)
            text = text[:INVITE_CHARS]
        n = len(text)
        self.lbl_inv_count.setText(f"{n:,} / {INVITE_CHARS:,}" if n >= INVITE_CHARS * 0.8 else "")

    def _clear_invite_text(self):
        self.inv_edit.clear()
        self.inv_edit.setFocus()

    def _finish_invite_edit(self, commit=True):
        """Close the agenda editor; ``commit`` False drops what was typed (a new
        session began, or the invite was dropped)."""
        if self.inv_panel.isHidden():
            return
        self.inv_panel.hide()
        self.ctx_chip.show()
        self.lbl_sum_head.show()
        self.txt_summary.show()
        if commit:
            self._set_invite_text(self.inv_edit.toPlainText())
        self._refresh_invite_chip()

    def _set_invite_text(self, text):
        """The agenda as the user left it: what answers use from now on, and what the
        meeting keeps in its folder (session.set_agenda, when the recorder has it)."""
        text = invite_context.clip_agenda(text)
        if text == self._invite:
            return
        self._invite = text
        self._refresh_invite_chip()
        session = getattr(self.app, "meeting_session", None) if self.app else None
        setter = getattr(session, "set_agenda", None)
        if callable(setter):
            try:
                setter(text)
            except Exception:
                logger.debug("Live Assistant: couldn't keep the agenda", exc_info=True)
        self._set_status("✓ Invite updated - every answer from now on uses it." if text
                         else "Invite agenda cleared.")

    def _drop_invite(self):
        """The x on the INVITE chip - "Not this meeting": for the rest of this session the
        answers stop getting its title, people and agenda."""
        self._finish_invite_edit(commit=False)
        had = self._invite
        self._invite_on, self._invite, self._invite_others = False, "", []
        self._title, self._attendees = "", ""
        session = getattr(self.app, "meeting_session", None) if self.app else None
        setter = getattr(session, "set_agenda", None)
        if had and callable(setter):
            try:
                setter("")
            except Exception:
                logger.debug("Live Assistant: couldn't clear the agenda", exc_info=True)
        self._refresh_title()
        self._refresh_invite_chip()
        self._set_status("Not this meeting - answers won't use its invite.")

    def _calendar_start(self):
        """The calendar meeting a Start from here belongs to - {"title", "attendees",
        "others", "agenda", "event_id"} - or {} (no calendar, none on now)."""
        fn = getattr(self.app, "calendar_start_context", None) if self.app else None
        if not callable(fn):
            return {}
        try:
            ctx = fn()
        except Exception:
            logger.debug("Live Assistant: calendar context unavailable", exc_info=True)
            return {}
        return ctx if isinstance(ctx, dict) and (ctx.get("title") or ctx.get("event_id")) else {}

    # ── start / stop (drives the meeting recorder) ──
    def _meeting_session(self):
        """The app's meeting recorder (meeting_session.MeetingSession) - the
        one the Meetings page and the calendar reminder drive too - or None."""
        app = self.app
        if app is None:
            return None
        s = getattr(app, "meeting_session", None)
        if s is None and getattr(app, "recorder", None) is not None:
            try:
                import meeting_session
                s = meeting_session.get_session(app)
            except Exception:
                logger.debug("No meeting session", exc_info=True)
                s = None
        return s

    def _call_language(self):
        """This session's transcription language: Call language, or "" =
        the dictation language (the saved settings are never touched)."""
        cfg = self.app.cfg if self.app else {}
        return (cfg.get("live_assist_language") or cfg.get("language") or "auto").strip() or "auto"

    def _answer_language(self):
        """Answer in: a language, or "" = the call's own ("auto": no hint)."""
        cfg = self.app.cfg if self.app else {}
        return (cfg.get("live_assist_output_language") or "auto").strip() or "auto"

    def _start_listening(self):
        # An open context editor: what's typed is this session's context.
        self._finish_context_edit()
        self._finish_invite_edit(commit=False)
        session = self._meeting_session()
        if session is None:
            if not self._pro_blocks():          # Free: the Pro window, whatever the recorder
                self._set_status("Meeting recorder isn't available.")
            return
        try:
            state = getattr(session, "state", None)
            if state == "recording":
                return
            if state == "processing":
                # Restarting the shared recorder now would lose the last recording.
                self._set_status("Still saving the last session - try again in a moment.")
                return
            if self._pro_blocks():              # Start is the Pro action
                return
            # The one-time recording notice (the same as Meetings').
            from ui.recording_prompts import ensure_recording_consent
            if not ensure_recording_consent(self, self.app):
                return
            lang = self._call_language()
            # Answers can only be as live as the transcript: short speech pieces
            # for this session (the meeting's final transcript is redone in
            # full afterwards, so accuracy of the notes is unaffected).
            self._set_fast_chunks(True)
            # Wake the answer engine now, so the first question isn't a cold start.
            threading.Thread(target=self._warm_up_engine, daemon=True).start()
            # The calendar meeting on now (or starting within 5 minutes) names this
            # session: its title, people, event id and agenda (the INVITE chip).
            cal = self._calendar_start()
            who = {}
            if cal:
                who = {"attendees": ", ".join(cal.get("attendees") or []),
                       "calendar_event_id": cal.get("event_id") or None}
            # Both sides of the call, as the Start button promises: mic +
            # system sound, whatever Meetings is set to record (when this
            # system can capture both). Always a new meeting.
            ok, code, msg = start_session(
                session, agenda=(cal or {}).get("agenda", ""),
                title=(cal or {}).get("title") or "Live session " + time.strftime("%H:%M"),
                audio_mode="smart_meeting", language=lang, source="live_assist", **who)
            if ok and cal:
                self.set_invite(cal.get("agenda", ""), cal.get("others"))
                note = getattr(self.app, "note_calendar_start", None)
                if callable(note):
                    note(cal, "live_card")
            if not ok:
                self._set_fast_chunks(False)
                if code == "device":
                    first = ("Couldn't open the microphone or the call's sound - close "
                             "other apps using them and try again.")
                else:
                    first = (msg or "").strip().split("\n", 1)[0].strip()
                self._set_status(first[:140] or "Couldn't start listening - see Meetings.")
            else:
                self.app.track("live_prompter_started", {
                    "language": lang or "auto",
                    "screen_auto": self.btn_screen.isChecked(),
                    "auto_suggest": self._auto,
                    "context": bool(self._context),
                })
        except Exception as e:
            logger.warning("Live Assistant start failed: %s", e, exc_info=True)
            self._set_fast_chunks(False)
            self._set_status(f"Couldn't start: {str(e)[:80]}")

    def _set_fast_chunks(self, on):
        rec = getattr(self.app, "recorder", None) if self.app else None
        if rec is not None:
            rec.fast_live_chunks = bool(on)

    def _warm_up_engine(self):
        # Worker thread. A scale-to-zero GPU endpoint (a dedicated Modal model) can take
        # a while to boot; this request starts it while the call gets going.
        try:
            engine, cfg = self._resolve_engine()
            if engine is not None:
                try:
                    self._prepare_documents(engine, cfg)     # before the (possibly slow) wake-up
                except Exception:
                    logger.debug("documents warm-up failed", exc_info=True)
                actions.warm_up(engine, cfg)
        except Exception:
            logger.debug("engine warm-up failed", exc_info=True)

    def _stop_listening(self):
        session = self._meeting_session()
        if session is None or getattr(session, "state", None) != "recording":
            return
        try:
            session.stop()
            release = getattr(self.app, "release_live_session", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    logger.debug("Live Assistant: session release failed", exc_info=True)
            self._set_status("Stopped - your notes will be in Meetings.")
        except Exception as e:
            logger.warning("Live Assistant stop failed: %s", e, exc_info=True)
            self._set_status(f"Couldn't stop: {str(e)[:80]}")

    # ── data feed (GUI thread) ──
    def set_meeting_active(self, active, title="", attendees="", agenda=""):
        """A session began (``active``) or ended. ``agenda``: the calendar invite's
        agenda when the session came from one (the INVITE chip shows); without it the
        session is not an invite's (``set_invite`` says so when it is)."""
        self._meeting_active = bool(active)
        if active:
            # Started from Meetings with the editor open: what's typed
            # is this session's context.
            self._finish_context_edit()
            self._finish_invite_edit(commit=False)
            self._invite = invite_context.clip_agenda(agenda)
            self._invite_on = bool(self._invite)
            self._invite_others = []
            self._live_since = time.time()
            self._live_text = ""
            self._summary = ""
            self._qa_history = []
            self._qa_docs = []
            self._reset_rules_session()
            self._audio_folder, self._audio_ready, self._audio_size = None, False, -1
            self._audio_polls = 0
            self.btn_audio.hide()
            self._title, self._attendees = title or "", attendees or ""
            self.txt_summary.clear()
            self.txt_summary.setPlaceholderText("Listening…")
            self.lbl_status.setText("Context locked in for this session." if self._context
                                    else "")
        else:
            self._set_fast_chunks(False)
            self._cancel_rules()               # what was still being worked on stops; the feed stays to read
            # The session is over: its invite chip goes and answers stop using the
            # agenda (the title and people stay for a follow-up, as they always did).
            self._finish_invite_edit(commit=False)
            self._invite_on, self._invite, self._invite_others = False, "", []
            # The recording lands in the meeting folder moments after Stop;
            # _on_tick shows Download audio once THIS session's part is fully
            # written - a resumed meeting already has earlier parts, so count
            # them now (the save worker hasn't started yet).
            session = getattr(self.app, "meeting_session", None) if self.app else None
            self._audio_folder = getattr(session, "meeting_dir", None)
            self._audio_base = len(self._audio_parts()) if self._audio_folder else 0
            self._audio_polls, self._audio_size = 0, -1
            self.lbl_status.setText("Stopped - saving the recording and the notes…")
        self._refresh_live_controls()
        self._refresh_hear()
        self._apply_rules_view()

    def discard_session_audio(self):
        """The session was discarded: nothing to offer for download."""
        self._audio_folder, self._audio_ready = None, False
        self.btn_audio.hide()
        self._set_status("Session discarded.")

    def feed_transcript(self, piece, answer=True):
        """A newly transcribed piece of the call. ``answer=False`` for bulk
        catch-up text (a resumed meeting, opening mid-call) - old questions in
        it must not trigger answers."""
        piece = (piece or "").strip()
        if not piece:
            return
        if piece == GAP_MARK:
            # A stretch the cloud could not read (a language local Whisper is poor at): shown in the
            # transcript once, as "[...]" - and never heard as speech by an answer or a rule.
            if not self._live_text.endswith(GAP_MARK):
                self._live_text = (self._live_text + " " + GAP_MARK).strip()
                self.txt_summary.setPlainText(self._live_text[-700:])
            return
        self._live_text = (self._live_text + " " + piece).strip()
        self._text_since_suggest += len(piece)
        tail = self._live_text[-700:]
        if len(self._live_text) > 700 and " " in tail:
            tail = tail.split(" ", 1)[1]
        self.txt_summary.setPlainText(tail)
        sb = self.txt_summary.verticalScrollBar()
        sb.setValue(sb.maximum())
        # "Apply to everything I hear": every new piece goes to the rule (merged with the ones heard
        # a moment ago), not only a question. Same guard as below: only while the card is on screen.
        if answer and self._meeting_active and self.isVisible() and self.rules_active():
            if not self._rule_paused:          # paused: what is said meanwhile is not worked on later
                self._rule_feed.add(piece, time.monotonic())
                self._pump_rules()
            return
        # The companion part: someone just asked something - answer right away.
        # Only while the overlay is on screen: a hidden overlay must never
        # grab screenshots or send the call to an AI behind the user's back
        # (it stays connected to every meeting once it has been opened).
        if (answer and self._auto and self._meeting_active and self.isVisible()
                and looks_like_question(piece)):
            self.suggest("", auto=True)

    def set_summary(self, text):
        # The meeting window's rolling recap isn't shown here: the overlay is
        # for answers, and the live transcript beats a recap mid-call.
        self._summary = text or ""

    # ── documents the user added (live_docs.py; the Live Assistant page manages them) ──
    def _refresh_docs_chip(self):
        """The chip above the ask box: how many documents the answers can read."""
        try:
            docs = live_docs.get_store().list()
        except Exception:
            logger.debug("Live Assistant: documents unavailable", exc_info=True)
            docs = []
        active = [d for d in docs if d.active]
        editing = not self.ctx_panel.isHidden()
        # The bar under the answer, or - while the context editor is open - the editor's own row.
        self.docs_bar.setVisible(bool(docs) and not editing)
        self.ctx_docs_chip.setVisible(bool(docs))
        text = live_docs.chip_text(len(active)) if active else DOCS_OFF
        off = "false" if active else "true"
        shown = ", ".join(d.name for d in active[:4]) + (" +%d" % (len(active) - 4) if len(active) > 4 else "")
        import html as html_mod
        tip = "<p style='white-space:normal'>%s</p>" % html_mod.escape(
            ((shown + ". ") if shown else "") + DOCS_TIP, quote=False)
        for chip in (self.docs_chip, self.ctx_docs_chip):
            chip.setText(text)
            if chip.property("off") != off:
                chip.setProperty("off", off)                    # restyle: dashed / faint
                chip.style().unpolish(chip)
                chip.style().polish(chip)
            try:
                from ui.components import icons as comp_icons
                t = _THEMES[self._theme_name]
                chip.setIcon(comp_icons.icon("file", 13, t["muted"] if active else t["faint"], 1.9))
                chip.setIconSize(QSize(13, 13))
            except Exception:
                logger.debug("Live Assistant: documents chip icon", exc_info=True)
            chip.setToolTip(tip)
            chip.setAccessibleName("%s. %s" % (text, DOCS_TIP))

    def _open_docs_menu(self):
        """The chip's list: tick the documents the answers may use (the same switches as
        on the Live Assistant page). A menu - nothing opens on the screen itself."""
        from PySide6.QtWidgets import QMenu
        try:
            docs = sorted(live_docs.get_store().list(), key=lambda d: d.added)
        except Exception:
            docs = []
        if not docs:
            return
        menu = QMenu(self)
        head = menu.addAction("Use in answers")
        head.setEnabled(False)
        menu.addSeparator()
        for d in docs:
            act = menu.addAction(d.name.replace("&", "&&"))      # a name is text, never a shortcut key
            act.setCheckable(True)
            act.setChecked(bool(d.active))
            act.setData(d.id)
            act.toggled.connect(lambda on, i=d.id, n=d.name: self._set_doc_active(i, n, on))
        menu.aboutToHide.connect(menu.deleteLater)
        self._doc_menu = menu
        anchor = self.sender() if isinstance(self.sender(), _DocsChip) else self.docs_chip
        at = anchor.mapToGlobal(QPoint(0, 0))
        menu.popup(QPoint(at.x(), at.y() - menu.sizeHint().height() - 4))

    def _set_doc_active(self, doc_id, name, on):
        try:
            live_docs.get_store().set_active(doc_id, bool(on))
        except Exception:
            logger.warning("Live Assistant: a document switch couldn't be saved", exc_info=True)
        live_docs_ui.bridge().changed.emit()           # this chip and the page's rows
        self._set_status("✓ %s is used in answers." % name if on else
                         "%s isn't used in answers." % name)

    def ask_open_paths(self):
        """The file picker (every kind of file). While the card is hidden from screen shares the
        picker is hidden too - Qt's own dialog, excluded from capture like the card - never the
        system one, which a share would show (it lists the person's folders). Tests replace this."""
        hidden = self._private and (self._exclusion_ok or self._mac_partial)
        if hidden:
            dlg = QFileDialog(self, ADD_DOCS, self._docs_dir, "All files (*)")
            dlg.setFileMode(QFileDialog.ExistingFiles)
            dlg.setOption(QFileDialog.DontUseNativeDialog, True)
            try:
                # The card's own sheet (below) gives every QLabel the card's text colour - near white
                # on the dark card look - and a dialog opened from the card is its child: the picker's
                # "Look in:" / "File name:" words came out white on the light panel (1:1). The app's own
                # sheet on the dialog wins over the inherited one (a widget's own sheet always does).
                from ui import theme
                dlg.setStyleSheet(theme.stylesheet())
            except Exception:
                logger.debug("Live Assistant: couldn't style the file picker", exc_info=True)
            try:
                dlg.winId()                    # the native window, before it maps
                glass.exclude_from_capture(dlg, True)
            except Exception:
                logger.debug("Live Assistant: couldn't hide the file picker", exc_info=True)
            paths = list(dlg.selectedFiles()) if dlg.exec() else []
            dlg.deleteLater()                  # (it is the card's child: it would otherwise stay as long as the card)
        else:
            paths, _ = QFileDialog.getOpenFileNames(self, ADD_DOCS, self._docs_dir, "All files (*)")
        if paths:
            self._docs_dir = os.path.dirname(paths[0]) or self._docs_dir
        return list(paths or [])

    def _pick_documents(self):
        """The paper-clip or Add documents: read what was picked, off the GUI thread."""
        paths = self.ask_open_paths()
        if paths:
            self._doc_queue.add(paths)

    # -- files dropped on the card become documents --
    def _takes_files(self, mime):
        return (self._expanded and self.crop_panel.isHidden()
                and live_docs_ui.wants_files(mime))

    def dragEnterEvent(self, event):
        if self._takes_files(event.mimeData()):
            self.drop_veil.setGeometry(self.rect())
            self.drop_veil.raise_()
            self.drop_veil.show()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._takes_files(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.drop_veil.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self.drop_veil.hide()
        paths, _more = live_docs_ui.dropped_paths(event.mimeData())
        if paths and self._takes_files(event.mimeData()):
            event.acceptProposedAction()
            self._doc_queue.add(paths)
        else:
            event.ignore()

    def _on_doc_started(self, name, index, total):
        self._set_status("Reading %s…" % name + (" (%d of %d)" % (index, total) if total > 1 else ""))

    def _on_doc_added(self, result):
        if getattr(result, "reason", "") != "cancelled" and self.app is not None:
            try:
                self.app.track(*live_docs.event_for(result))     # counts only: never a name or text
            except Exception:
                logger.debug("Live Assistant: document event", exc_info=True)
        if result.ok:
            self._set_status("✓ Added %s." % result.info.name + (" " + result.note if result.note else ""))
        elif result.reason != "cancelled":
            self._set_status("%s: %s" % (result.name, result.message))

    def _on_doc_batch_done(self, results, cancelled):
        if cancelled:
            self._set_status("Stopped.")
        if any(r.ok for r in results or ()):
            live_docs_ui.bridge().changed.emit()       # the chip here, the rows on the page

    # -- the one-time question, and what an answer may read --
    def _on_doc_ask(self, req):
        """GUI thread: put the "use your documents with the cloud?" question. A worker (an
        answer, or the warm-up at Start) is waiting for ``req["event"]``."""
        took = False
        try:
            cfg = self.app.cfg if self.app else {}
            managed = bool(req.get("managed", True))
            decided = live_docs.doc_use(cfg, "managed" if managed else "cloud", 1, req.get("route"))
            if not decided.ask:
                req["answer"] = bool(decided.send)      # answered meanwhile (a yes for another route is not one)
            elif self._doc_asking or (req.get("gen") and req["gen"] != self._gen):
                req["answer"] = None            # one question at a time; a newer answer took over
            else:
                took = self._doc_asking = True
                hidden = self._private and (self._exclusion_ok or self._mac_partial)
                req["answer"] = live_docs_ui.ask_cloud(self, self.app, managed=managed, private=hidden,
                                                       route=req.get("route"))
        except Exception:
            logger.warning("Live Assistant: the documents question failed", exc_info=True)
            req["answer"] = None
        finally:
            if took:
                self._doc_asking = False
            ev = req.get("event")
            if ev is not None:
                ev.set()

    def _ask_documents(self, gen, managed, route=None):
        """Worker thread: have the question put on the GUI thread and wait for the person's
        answer (a yes is for ``route``, live_docs.route_id). True / False, or None when it
        wasn't answered (a newer question took over, or too long)."""
        req = {"gen": gen, "managed": managed, "route": route, "event": threading.Event(),
               "answer": None}
        self.sig_doc_ask.emit(req)
        waited = 0.0
        while not req["event"].wait(0.25):
            waited += 0.25
            if (gen and gen != self._gen) or waited >= DOC_ASK_WAIT_S:
                return None
        return req["answer"]

    @staticmethod
    def _without_document_answers(ctx_args, history_docs, kind, cfg, route=None):
        """rolling_context's arguments without the earlier answers that were written from the
        user's documents - or None when those may go along. A follow-up carries the last
        answers, and an answer that quotes a document is the document: it goes only where the
        documents themselves may go (live_docs.doc_use: this computer, or a cloud route - the
        ``route`` service - the person agreed to; never in Privacy mode). Switching "Answers
        written by" from this computer to the cloud, or turning Privacy mode off, must not
        carry them over."""
        if kind not in ("cloud", "managed") or live_docs.doc_use(cfg, kind, 1, route).send:
            return None
        history = list(ctx_args.get("history") or ())
        flags = list(history_docs)[-len(history):] if history else []
        flags = [False] * (len(history) - len(flags)) + flags          # (the last answers line up)
        return dict(ctx_args, history=[h for h, from_docs in zip(history, flags) if not from_docs])

    def _documents_for_answer(self, context, doc_args, kind, cfg, gen, auto, route=None):
        """Worker thread. The documents for THIS answer: (context, pictures, footer words).
        ``doc_args`` are rolling_context's arguments; the context is built again with the
        reference block once the engine (so its window) is known. live_docs.doc_use is the one
        decision: Privacy mode is absolute, a cloud route needs the person's one-time yes for
        that route (``route``, live_docs.route_id: one own-key service, or Gusan Cloud) -
        asked here for a question they typed, never in the middle of an automatic answer."""
        store = live_docs.get_store()
        n = store.active_count()
        route = route or live_docs.route_id(kind)
        use = live_docs.doc_use(cfg, kind, n, route)
        if use.ask:
            if auto:
                return context, [], FOOT_DOCS_ASK
            answer = self._ask_documents(gen, kind == "managed", route)
            if answer is None:
                return context, [], FOOT_DOCS_ASK
            use = live_docs.doc_use(live_docs.with_choice(cfg, answer, route), kind, n, route)
        if not use.send:
            return context, [], (FOOT_DOCS_KEPT if use.why in ("declined", "private") else "")
        question = doc_args.get("question") or last_question(doc_args.get("live_text", ""))
        docs = store.active_docs()
        # (a picture goes out whole, so an answer nobody asked for - an automatic one - takes
        # only the pictures its talk is about; a typed question may take the newest ones)
        pictures = store.active_pictures(question, matching_only=bool(auto)) if use.pictures else []
        # This computer can't read pictures: say so, instead of silently leaving them out.
        unread = (FOOT_PICTURES if kind == "local_llm"
                  and any(d.active and d.picture for d in store.list()) else "")
        if not docs and not pictures:
            return context, [], unread
        built = rolling_context(**doc_args, documents=docs,
                                doc_chars=live_docs.doc_chars_for(kind),
                                pictures=[p.name for p in pictures])
        words = [FOOT_DOCS_USED] if (live_docs.DOC_BEGIN in built or pictures) else []
        if unread:
            words.append(unread)
        return built, [p.b64 for p in pictures], " · ".join(words)

    def _prepare_documents(self, engine, cfg):
        """Worker thread, at Start: load and index the active documents now, and - when the
        answers will come from a cloud AI and the person hasn't been asked yet - put the
        one-time question before the first question of the call (not waited for)."""
        store = live_docs.get_store()
        n = store.active_count()
        if not n:
            return
        kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
        route = live_docs.route_id(kind, engine, cfg)
        if live_docs.doc_use(cfg, kind, n, route).ask:
            self.sig_doc_ask.emit({"gen": 0, "managed": kind == "managed", "route": route,
                                   "event": threading.Event(), "answer": None})
        store.warm()

    # ── "Apply to everything I hear": the running feed ──
    def _set_view(self, view):
        self._view = "answer" if view == "answer" else "feed"
        self._apply_rules_view()

    def _apply_rules_view(self):
        """Show the running feed (and its bar) while a rule applies to everything heard, the
        single answer otherwise; a typed question's answer shows with a way back."""
        active = self.rules_active()
        if not active:
            self._view = "feed"
        feed = active and self._view == "feed"
        self.feed_bar.setVisible(active)
        self.txt_feed.setVisible(feed)
        self.txt_suggestion.setVisible(not feed)
        self.btn_feed_back.setVisible(active and not feed)
        self.lbl_sug_head.setText(FEED_LABEL if feed else "ANSWER")
        self.btn_auto.setEnabled(not active)
        self.btn_auto.setToolTip(_wrap_tip(AUTO_TIP_RULES if active else AUTO_TIP))
        self.txt_feed.setPlaceholderText(RULES_LIVE if self._meeting_active else RULES_IDLE)
        self._refresh_feed_bar()
        self._render_feed()

    def _rule_engine_words(self):
        """Who applies the rule, in plain words ("DeepSeek V4.1 Flash · Gusan Cloud"): the writer
        the answers use (Answers written by). Read from the settings only - it never asks the
        network (a sign-in refresh) on the GUI thread, which runs this every second."""
        try:
            import engine_choice
            r = engine_choice.runs_on(self.app, "live_assistant") if self.app else {}
        except Exception:
            logger.debug("Live Assistant: couldn't name the writer", exc_info=True)
            return ""
        kind, model, plain = r.get("kind"), r.get("model_name") or "", r.get("plain") or ""
        if kind in ("cloud", "own_key"):
            return " · ".join(x for x in (model, plain) if x)
        if kind == "local":
            return (model + " · on this computer") if model else "on this computer"
        return ""

    def _refresh_feed_bar(self):
        if not self.rules_active():
            return
        if self._rule_paused:
            state = "Paused" + (" - " + self._rule_note if self._rule_note else "")
        else:
            who = self._rule_engine_words()
            if self._rule_feed.catching_up:
                n = self._rule_feed.dropped_total
                lead = "Catching up" + (" - skipped %d" % n if n else "")
            elif self._rule_busy:
                lead = "Applying your rule"
            else:
                lead = "Your rule runs on everything you hear"
            state = lead + (" · " + who if who else "")
        self.lbl_feed_state.setText(state)
        self.lbl_feed_state.setToolTip(_wrap_tip(
            state + ". It uses the same writer as your answers (Answers written by, on the Live Assistant page)."))
        emph = "true" if (self._rule_paused or self._rule_feed.catching_up) else "false"
        if self.lbl_feed_state.property("emph") != emph:     # "Paused" and "Catching up" read stronger
            self.lbl_feed_state.setProperty("emph", emph)
            self.lbl_feed_state.style().unpolish(self.lbl_feed_state)
            self.lbl_feed_state.style().polish(self.lbl_feed_state)
        self.btn_feed_pause.setText("Resume" if self._rule_paused else "Pause")
        self.btn_feed_pause.setToolTip(_wrap_tip("Start applying your rule again." if self._rule_paused
                                                 else "Stop applying your rule until you resume."))

    def _schedule_feed_render(self):
        timer = getattr(self, "_feed_timer", None)
        if timer is not None and not timer.isActive():
            timer.start()

    def _render_feed(self):
        """The feed as HTML, newest first. Plain, escaped, no links or images; the scroll
        position stays where the reader left it."""
        if not self.rules_active():
            return
        t = _THEMES[self._theme_name]
        sb = self.txt_feed.verticalScrollBar()
        keep = sb.value()
        self.txt_feed.setHtml(live_rules.feed_html(
            self._rule_entries, {"text": t["text"], "muted": t["muted"], "faint": t["faint"]}))
        sb.setValue(min(keep, sb.maximum()))

    def _entry_set(self, entry_id, **kw):
        for i, e in enumerate(self._rule_entries):
            if e.id == entry_id:
                self._rule_entries[i] = e._replace(**kw)
                return True
        return False

    def _add_entry(self, src, state, note=""):
        e = live_rules.Entry(self._rule_next_id, src, "", state, note)
        self._rule_next_id += 1
        self._rule_entries.insert(0, e)
        del self._rule_entries[live_rules.FEED_KEEP:]
        return e.id

    def _reset_rules_session(self):
        """A new session: nothing waiting, nothing from the last one, the allowance fresh."""
        self._cancel_rules()
        self._rule_feed = live_rules.RuleFeed()
        self._rule_entries = []
        self._rule_paused, self._rule_note, self._rule_fails = False, "", 0
        self._rule_budget.reset()
        self._view = "feed"
        self.txt_feed.clear()
        self._apply_rules_view()

    def _cancel_rules(self):
        """Stop what is running and forget what is waiting (a result still on its way is dropped)."""
        self._rule_gen += 1
        self._rule_busy = False
        self._rule_feed.clear()
        for i, e in enumerate(self._rule_entries):
            if e.state == "working":
                self._rule_entries[i] = e._replace(state="skipped", note="Stopped.")
        self._schedule_feed_render()

    def _toggle_rule_pause(self):
        if self._rule_paused:
            self._resume_rules()
        else:
            self._pause_rules("user")

    def _pause_rules(self, why, note=""):
        self._rule_paused = True
        self._rule_note = note
        self._cancel_rules()
        self._refresh_feed_bar()

    def _resume_rules(self):
        self._rule_paused, self._rule_note, self._rule_fails = False, "", 0
        self._rule_budget.reset()
        self._refresh_feed_bar()
        self._pump_rules()

    def _pump_rules(self):
        """On every new piece and once a second: hand the next group to the model when it is free."""
        if not (self.rules_active() and self._meeting_active):
            return
        now = time.monotonic()
        self._rule_feed.poll(now)
        if self._rule_paused or self._rule_busy:
            self._refresh_feed_bar()
            return
        if now - self._rule_last_start < live_rules.MIN_GAP_S:
            return
        group = self._rule_feed.take(now, idle=True)
        if group is None:
            self._refresh_feed_bar()
            return
        if self._rule_budget.exhausted:
            self._pause_rules("budget", "stopped after %d sentences. Resume to keep going."
                              % self._rule_budget.cap)
            return
        self._start_rule_group(group, now)

    def _start_rule_group(self, group, now):
        if group.skipped:
            n = group.skipped
            self._add_entry("", "skipped", "Skipped %d part%s to catch up." % (n, "" if n == 1 else "s"))
        entry_id = self._add_entry(group.text, "working")
        # The user's standing instructions as they are RIGHT NOW (an edit applies from here on).
        context = live_rules.rule_context(
            group.text, group.recent, session_context=self._context, title=self._title,
            attendees=self._attendees, output_lang=self._answer_language())
        self._rule_budget.spend()
        self._rule_busy = True
        self._rule_last_start = now
        threading.Thread(target=self._rule_worker, args=(entry_id, context, self._rule_gen),
                         daemon=True).start()
        self._refresh_feed_bar()
        self._schedule_feed_render()

    def _emit_rule_partial(self, entry_id, text, gen):
        try:
            self.sig_rule_partial.emit(entry_id, text, gen)
        except RuntimeError:
            pass                               # the card is gone

    def _emit_rule_done(self, entry_id, text, error, gen):
        try:
            self.sig_rule_done.emit(entry_id, text, error, gen)
        except RuntimeError:
            pass

    def _rule_worker(self, entry_id, context, gen):
        """One piece, on a worker thread: the writer the answers use (actions.live_engine), the
        short "apply the rule" brief (actions.ACTION_LIVE_RULE), streamed into the feed."""
        try:
            engine, cfg = self._resolve_engine()
            if engine is None:
                self._emit_rule_done(entry_id, "", actions.no_live_engine_message(cfg), gen)
                return
            self._rule_engine = engine
            cfg = dict(cfg or {})
            acc = []
            last = [0.0]

            def on_token(delta):
                if gen != self._rule_gen:
                    raise _Superseded()        # paused, switched off, or a new session
                if isinstance(delta, actions.action_api.ReplaceText):
                    acc[:] = [str(delta)]      # what streamed so far was reasoning
                    last[0] = 0.0
                else:
                    acc.append(delta)
                now = time.time()
                if now - last[0] >= 0.1:
                    last[0] = now
                    self._emit_rule_partial(entry_id, "".join(acc), gen)

            text = actions.process_stream(context, actions.ACTION_LIVE_RULE, on_token,
                                          model=engine, config=cfg)
            self._emit_rule_done(entry_id, (text or "".join(acc)).strip(), "", gen)
        except _Superseded:
            return
        except Exception as e:
            self._emit_rule_done(entry_id, "", str(e)[:240], gen)

    def _on_rule_partial(self, entry_id, text, gen):
        if gen != self._rule_gen:
            return
        self._entry_set(entry_id, out=text)
        self._schedule_feed_render()

    def _on_rule_done(self, entry_id, text, error, gen):
        if gen != self._rule_gen:
            return                             # a result of a rule that was stopped
        self._rule_busy = False
        if error:
            self._rule_failed(entry_id, error)
        else:
            self._rule_fails = 0
            if text.strip():
                self._entry_set(entry_id, out=text, state="done")
            else:                              # filler, or nothing to do with this piece: no line
                self._rule_entries = [e for e in self._rule_entries if e.id != entry_id]
        self._schedule_feed_render()
        self._refresh_feed_bar()
        self._pump_rules()

    def _rule_failed(self, entry_id, error):
        """One piece failed: say so on its line. A limit or a missing engine stops the mode at once
        (it would fail every time); a few errors in a row pause it - Resume tries again."""
        low = (error or "").lower()
        if "daily" in low and "limit" in low:
            plain, hard = LIMIT_WORDS, True
        elif error in (actions.NO_ENGINE_MESSAGE, actions.LOCAL_ANSWERS_MESSAGE, NEEDS_PRO_ANSWERS) \
                or low.startswith("sign in"):
            plain, hard = error, True
        else:
            plain, hard = "Couldn't apply the rule to this part: %s" % (error or "try again").strip()[:140], False
        self._entry_set(entry_id, state="failed", note=plain)
        self._rule_fails += 1
        if hard:
            self._pause_rules("limit", plain)
        elif self._rule_fails >= live_rules.FAILS_BEFORE_PAUSE:
            self._pause_rules("errors", "a few errors in a row. Resume to try again.")

    # ── suggestions ──
    def _set_status(self, text):
        self.lbl_status.setText(text or "")

    def _ask(self):
        q = self.input_ask.text().strip()
        image, image_b64 = self._image, self._image_b64
        if not q and not image_b64:
            return
        gen = self._gen
        self.suggest(q or SNIP_QUESTION, image_b64=image_b64)
        if self._gen == gen:
            return                            # nothing went out
        # Sent: empty the box at once and say so - the answer streams below.
        # (If it fails, the question and image come back.)
        self._sent_question = q
        self._sent_image = image if image_b64 else None
        self._clear_image()
        self.input_ask.clear()
        self.input_ask.setPlaceholderText(ASK_SENT)
        what = q or "the image"
        extra = " + image" if (q and image_b64) else ""
        self._set_status("✓ Sent: " + (what if len(what) <= 62 else what[:59] + "…") + extra)
        self.txt_suggestion.setPlainText(f"You asked: {q or 'about the image'}{extra}\n\nThinking…")

    def _ask_or_answer(self):
        # The Ask button: the typed question and/or the attached image if there
        # is one, else answer the latest question of the call.
        if self.input_ask.text().strip() or self._image_b64:
            self._ask()
        else:
            self.suggest("")

    # ── images: snip a part of the screen, or paste one ──
    def _images_blocked(self):
        if self.app and self.app.cfg.get("privacy_mode"):
            self._set_status("Images can't be sent in Privacy Mode - answers stay on this "
                             "computer.")
            return True
        return False

    def _attach_image(self, img):
        if img is None or img.isNull() or self._images_blocked():
            return
        b64 = jpeg_b64(img, 1600, 90)
        if not b64:
            return
        self._image, self._image_b64 = QImage(img), b64
        self.img_chip.set_image(img)
        self.img_chip.show()
        self._set_status("✓ Image attached - ask about it, or just press send.")
        if self.crop_panel.isHidden():
            # Not while cropping: an image handed back by a failed answer
            # would move Enter/Esc from the crop view to the hidden ask box.
            self.input_ask.setFocus()

    def _clear_image(self):
        self._image, self._image_b64 = None, ""
        self.img_chip.hide()

    def _start_snip(self):
        """Screenshot, cropped inside the card. The grab is silent (like
        Solve screen) and the card itself is left out of it while Private."""
        if self._crop_shots or self._images_blocked():
            return
        shots = []
        for scr in QApplication.screens():
            try:
                pm = scr.grabWindow(0)
            except Exception:
                pm = QPixmap()
            if not pm.isNull():
                shots.append((scr, pm))
        if not shots:
            self._set_status("Couldn't capture the screen - try again.")
            return
        # Only the card's own monitor may show in the card while a share can
        # see the card (a share of that monitor shows it anyway); another one
        # - e.g. the one under the cursor - only while the card is hidden.
        own = self.screen()
        safe = next((i for i, (scr, _pm) in enumerate(shots) if scr is own), None)
        if safe is None and not self._exclusion_ok:
            self._set_status("Couldn't capture this screen - try again.")
            return
        under = screen_to_capture(own)
        start = safe
        if self._exclusion_ok:
            start = next((i for i, (scr, _pm) in enumerate(shots) if scr is under),
                         0 if safe is None else safe)
        self._crop_shots = shots
        self._crop_safe = safe
        self._crop_idx = start
        if not self._expanded:
            self.set_expanded(True)
        self.body.hide()
        self.crop_panel.show()
        self._show_crop_shot()
        self.activateWindow()                  # Enter / Esc go to the crop view
        self.crop_view.setFocus()

    def _show_crop_shot(self):
        self.crop_view.set_shot(self._crop_shots[self._crop_idx][1])
        n = len(self._crop_shots)
        self.btn_crop_screen.setVisible(n > 1)
        self.btn_crop_screen.setText(f"Screen {self._crop_idx + 1} of {n}")
        self._refresh_crop_controls()

    def _crop_next_screen(self):
        if self._crop_shots:
            self._crop_idx = (self._crop_idx + 1) % len(self._crop_shots)
            self._show_crop_shot()
            self.crop_view.setFocus()

    def _crop_to_safe(self):
        """Back to the card's own monitor (or out of the crop if that one
        couldn't be captured), painted NOW - before the card can be seen."""
        if self._crop_safe is None:
            self._end_snip()
            self._set_status("Screenshot closed - this card is visible in screen sharing now.")
            return
        self._crop_idx = self._crop_safe
        self._show_crop_shot()
        self.crop_view.repaint()

    def _refresh_crop_controls(self):
        if self._crop_shots and not self._exclusion_ok and self._crop_idx != self._crop_safe:
            # The card is visible to a share while it shows another monitor.
            self._crop_to_safe()               # comes back here, or ends the crop
            return
        # Showing ANOTHER monitor's screenshot in a card that a share can see
        # would put an unshared screen into the share: only while hidden.
        self.btn_crop_screen.setEnabled(self._exclusion_ok)
        self.btn_crop_screen.setToolTip(
            "Show the next monitor" if self._exclusion_ok else
            "Only while this card is hidden from screen sharing - otherwise that "
            "monitor would show in your share.")
        picked = self.crop_view.has_selection()
        self.btn_crop_attach.setText("Attach selection" if picked else "Attach whole screen")
        hint = ("Drag again to change it, or click once for the whole screen."
                if picked else "Drag over the part you want, or attach the whole screen.")
        if not self._exclusion_ok:
            hint += ("  Heads-up: this card may show in some screen shares and recordings."
                     if self._mac_partial else
                     "  Heads-up: this card isn't hidden from screen sharing right now, "
                     "so this screenshot shows in your share too.")
        self.lbl_crop_hint.setText(hint)

    def _crop_attach(self):
        if not self._crop_shots:
            return
        self._on_snip_picked(self.crop_view.crop_image())

    def _end_snip(self):
        if not self._crop_shots and self.crop_panel.isHidden():
            return
        self._crop_shots = []
        self._crop_safe = None
        self.crop_view.set_shot(QPixmap())     # don't keep the screen in memory
        self.body.setVisible(self._expanded)
        self.crop_panel.hide()
        # Hiding the focused crop view hands focus to the next widget in the
        # chain - the header's Stop/Start button, where a Space would stop or
        # start the recording. Put it back in the ask box.
        if self._expanded and self.isVisible():
            self.input_ask.setFocus()

    def _on_snip_picked(self, img):
        self._end_snip()
        self._attach_image(img)
        self.activateWindow()                  # type the question right away

    def suggest(self, question="", auto=False, force_screen=False, image_b64=""):
        """Answer now. A newer request supersedes one still streaming - the
        freshest question is the one that matters in a live call.
        ``image_b64``: an image the user attached (a snip or a pasted one) -
        sent instead of the automatic screenshot."""
        if not self.app:
            self._on_suggestion("", "No app context.")
            return
        if image_b64 and self.app.cfg.get("privacy_mode"):
            self._images_blocked()
            return
        if auto and self.rules_active():
            return                                # every sentence already gets its result (Live results)
        if self._no_answer_engine_for_free():
            return                                # said so in the answer box; nothing went out
        if auto and ((self._suggesting and not getattr(self, "_suggest_auto", False))
                     or time.time() < getattr(self, "_hold_until", 0.0)):
            # The user asked for the answer on screen (typed, Solve screen,
            # Follow-ups...): a question heard meanwhile waits its turn
            # (_on_tick) instead of cutting that answer off or replacing it.
            self._pending_auto = True
            return
        screen_on = self.btn_screen.isChecked()
        if not self._live_text.strip() and not question:
            if screen_on:
                # Nothing said yet: the screen is the only context there is.
                question, force_screen = SOLVE_SCREEN, True
            else:
                self.txt_suggestion.setPlainText(
                    "Nothing has been said yet - press Start, or type a question.")
                return
        attached = bool(image_b64)
        # Privacy Mode is on-device only: answers come from a local, text-only
        # model, so a screenshot could only ever leave via cloud OCR - none.
        if (not attached and should_attach_screen(screen_on, force_screen)
                and not self.app.cfg.get("privacy_mode")):
            image_b64 = capture_screen_b64(screen_to_capture(self.screen()))
        if force_screen and not image_b64:
            # Asking a model to "solve the screen" without one only gets a
            # confident, made-up answer.
            self.txt_suggestion.setPlainText(
                "Solve screen is off in Privacy Mode - the screen never leaves this "
                "computer." if self.app.cfg.get("privacy_mode")
                else "Couldn't capture the screen - try again.")
            return
        self._gen += 1
        gen = self._gen
        self._pending_auto = False
        if self.rules_active():
            self._set_view("answer")          # a typed question's answer, with a way back to the feed
        self._screen_note = ""                # this answer's screen status, if any
        self._suggest_auto = auto
        self._suggest_asked = bool(question)
        self._sent_question = ""              # _ask() sets it for a typed question
        self._sent_image = None
        self._suggest_question = question or last_question(self._live_text)
        self._last_attached_screen = bool(image_b64)
        self._suggesting = True
        self._suggest_started = time.time()
        self._first_token_at = 0.0
        self._text_since_suggest = 0
        self.txt_suggestion.setPlainText("Thinking…")
        ctx_args = dict(
            live_text=self._live_text, question=question, title=self._title,
            attendees=self._attendees, screen="snip" if attached else bool(image_b64),
            output_lang=self._answer_language(), history=list(self._qa_history),
            session_context=self._context, invite=self._invite)
        context = rolling_context(**ctx_args)
        # The documents the user added are added by the worker, once the engine that will
        # answer is known (its kind sets how much of them fits, and whether they may go).
        has_docs = False
        try:
            has_docs = live_docs.get_store().active_count() > 0
        except Exception:
            logger.debug("Live Assistant: documents unavailable", exc_info=True)
        self._doc_foot = (gen, "")
        threading.Thread(target=self._suggest_worker, args=(context, image_b64, gen),
                         kwargs={"doc_args": ctx_args if has_docs else None, "auto": auto,
                                 "ctx_args": ctx_args, "history_docs": tuple(self._qa_docs)},
                         daemon=True).start()

    def _no_answer_engine_for_free(self):
        """Free and guests open the card to look around: with their own key or
        a downloaded local model Ask / Answer / Solve screen work as ever; with
        neither, say so in the answer box at once (instead of "Thinking…" and a
        failure) and send nothing. True when it said so."""
        if self._is_pro() is not False:
            return False
        try:
            engine, cfg = self._resolve_engine()
        except Exception:
            logger.debug("Live Assistant: engine check failed", exc_info=True)
            return False
        if engine is not None:
            return False
        self._stop_stream_render()
        self.txt_suggestion.setPlainText(
            actions.no_live_engine_message(cfg)
            if (cfg or {}).get("privacy_mode") or (cfg or {}).get("live_assist_writer") == "local"
            else NEEDS_PRO_ANSWERS)
        return True

    def _resolve_engine(self):
        """Engine for answers (see actions.live_engine): the Pro cloud for a Pro
        user even if a small offline model is set for Smart Actions, a cloud
        engine with its own key, else a downloaded local model. (None, cfg)
        when no REAL engine can run - there is deliberately no rule-based
        fallback."""
        engine, cfg = self.app._resolve_action_engine()
        return actions.live_engine(engine, cfg), cfg

    def _suggest_worker(self, context, image_b64, gen, doc_args=None, auto=False,
                        ctx_args=None, history_docs=()):
        """The answer, on a worker thread. ``doc_args``: rolling_context's arguments when the
        user has active documents - the context is then built again with them (see
        _documents_for_answer); None = no documents, ``context`` is used as it is.
        ``ctx_args`` / ``history_docs``: the same arguments always, and which of the earlier
        answers were written from the documents (see _without_document_answers)."""
        try:
            engine, cfg = self._resolve_engine()
            if engine is None:
                # "This computer" (or Privacy Mode) with no local model: say
                # which download answers here, not sign-in / API-key advice.
                self.sig_suggestion.emit("", actions.no_live_engine_message(cfg), gen)
                return
            self._answer_engine = (gen, engine)    # named in this answer's footer
            kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
            cfg = dict(cfg or {})
            route = live_docs.route_id(kind, engine, cfg)   # a yes for the documents is for this service
            doc_images = []                        # pictures from the user's documents
            if ctx_args is not None and any(history_docs):
                try:
                    clean = self._without_document_answers(ctx_args, history_docs, kind, cfg, route)
                    if clean is not None:          # the documents may not go to this route
                        context = rolling_context(**clean)
                        ctx_args = clean
                        if doc_args is not None:
                            doc_args = clean
                except Exception:
                    logger.warning("Live Assistant: earlier answers could not be checked",
                                   exc_info=True)
                    self.sig_suggestion.emit("", "please try again.", gen)    # nothing goes out unchecked
                    return
            if doc_args is not None:
                try:
                    context, doc_images, words = self._documents_for_answer(
                        context, doc_args, kind, cfg, gen, auto, route)
                except _Superseded:
                    raise
                except Exception:
                    logger.warning("Live Assistant: the documents were left out", exc_info=True)
                    doc_images, words = [], ""     # the answer goes on without them
                if gen != self._gen:
                    return                         # a newer question took over meanwhile
                self._doc_foot = (gen, words)
            image_status = {}
            if (image_b64 or doc_images) and kind in ("cloud", "managed"):
                # The screenshot first, then pictures from the documents (a list when
                # there are both; a plain string as ever when there is only the screenshot).
                cfg["_image_png_b64"] = (([image_b64] if image_b64 else []) + doc_images
                                         if doc_images else image_b64)
                cfg["_image_status"] = image_status   # set if the model refused it
            elif image_b64:
                # Text-only engine (local model): read the screen with Mistral
                # OCR when a Mistral key exists, so screen context still works -
                # never in Privacy Mode (belt and braces: suggest() already
                # doesn't capture then).
                mkey = ("" if cfg.get("privacy_mode")
                        else (cfg.get("mistral_api_key") or "").strip())
                ocr = actions.action_api.mistral_ocr(image_b64, mkey) if mkey else ""
                # The model gets no image: drop the note that says it does.
                context = actions.action_api._SCREEN_NOTE.sub("", context)
                self._last_attached_screen = bool(ocr)
                if ocr:
                    screen = "Text visible on the user's screen (OCR):\n" + ocr[:3000]
                    self._note_screen(gen, "Screen read via OCR")
                else:
                    screen = actions.action_api._NO_SCREEN_NOTE
                    self._note_screen(gen, "Screen needs a cloud AI engine (Pro or your own "
                                           "key) - answered from the transcript only.")
                head, sep, question = context.rpartition("\n\nUser's question: ")
                context = (f"{head}\n\n{screen}{sep}{question}" if sep
                           else f"{context}\n\n{screen}")
            acc = []
            last_emit = [0.0]

            def on_token(delta):
                if gen != self._gen:
                    raise _Superseded()           # a newer question took over
                if isinstance(delta, actions.action_api.ReplaceText):
                    acc[:] = [str(delta)]         # what streamed so far was reasoning
                    last_emit[0] = 0.0
                    # "First words" counts the answer, not the reasoning before it.
                    self._first_token_at = time.time() if str(delta).strip() else 0.0
                else:
                    acc.append(delta)
                    if not self._first_token_at and delta.strip():
                        self._first_token_at = time.time()
                now = time.time()
                if now - last_emit[0] >= 0.05:      # ~20 UI updates/s
                    last_emit[0] = now
                    self.sig_partial.emit("".join(acc), gen)

            text = actions.process_stream(context, actions.ACTION_LIVE_ASSIST, on_token,
                                          model=engine, config=cfg)
            if image_status.get("dropped"):
                # The engine can't read images: the answer came from the
                # conversation alone - don't claim "screen seen".
                self._last_attached_screen = False
                if image_b64:
                    self._note_screen(gen, "Screen not read - this AI engine can't see images; "
                                           "answered from the conversation only.")
                else:
                    self._note_screen(gen, "Pictures from your documents weren't read - this AI "
                                           "engine can't see images.")
            self.sig_suggestion.emit((text or "".join(acc)).strip(), "", gen)
        except _Superseded:
            return
        except Exception as e:
            self.sig_suggestion.emit("", str(e)[:240], gen)

    def _note_screen(self, gen, text):
        """Worker thread: a screen status for answer ``gen`` only - it shows
        in place of that answer's "Answered..." line and is gone with the next
        question (an older, superseded worker changes nothing)."""
        if gen != self._gen:
            return
        self._screen_note = text
        self.sig_status.emit(text)

    def _on_partial(self, text, gen=None):
        if not self._suggesting or (gen is not None and gen != self._gen):
            return
        # (_first_token_at is set by the worker, from answer text only.)
        # The streamed answer is shown as rendered Markdown (no ** or "- "
        # flashing), at most every STREAM_RENDER_MS: rendering a whole
        # document per token would cost CPU on a long answer.
        self._stream_pending = (text, self._gen if gen is None else gen)
        wait = STREAM_RENDER_MS - (time.monotonic() - getattr(self, "_stream_rendered_at", 0.0)) * 1000.0
        if wait <= 0:
            self._render_stream()
            return
        timer = getattr(self, "_stream_timer", None)
        if timer is None:
            timer = self._stream_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._render_stream)
        if not timer.isActive():
            timer.start(int(wait) + 1)

    def _render_stream(self):
        pending, self._stream_pending = getattr(self, "_stream_pending", None), None
        if pending is None:
            return
        text, gen = pending
        if not self._suggesting or gen != self._gen:
            return          # the answer ended or a newer question took over
        self._stream_rendered_at = time.monotonic()
        render_markdown(self.txt_suggestion, streaming_markdown(text))
        sb = self.txt_suggestion.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _stop_stream_render(self):
        self._stream_pending = None
        timer = getattr(self, "_stream_timer", None)
        if timer is not None:
            timer.stop()

    def _on_suggestion(self, text, error, gen=None):
        if gen is not None and gen != self._gen:
            return      # the answer to an older question - superseded
        self._suggesting = False
        self._stop_stream_render()          # the final answer replaces the streamed one
        self.input_ask.setPlaceholderText(ASK_PLACEHOLDER)
        sent, self._sent_question = getattr(self, "_sent_question", ""), ""
        self._last_suggest_at = time.time()
        took = time.time() - self._suggest_started if self._suggest_started else 0
        if self.app:
            self.app.track("live_prompter_suggestion", {
                "ok": not error,
                "auto": bool(getattr(self, "_suggest_auto", False)),
                "asked": getattr(self, "_suggest_asked", False),
                "screen": bool(getattr(self, "_last_attached_screen", False)),
                "first_words_seconds": (round(self._first_token_at - self._suggest_started, 1)
                                        if self._first_token_at else None),
                "seconds": round(took, 1),
            })
        if error:
            self.txt_suggestion.setPlainText(f"Couldn't get an answer: {error}")
            if sent and not self.input_ask.text():
                self.input_ask.setText(sent)  # nothing lost: Enter sends it again
            sent_image = getattr(self, "_sent_image", None)
            self._sent_image = None
            if sent_image is not None and not self._image_b64:
                self._attach_image(sent_image)
            return
        render_markdown(self.txt_suggestion, text.strip() or "(no answer)")
        if text.strip():
            doc_gen, doc_words = getattr(self, "_doc_foot", (None, ""))
            from_docs = bool(FOOT_DOCS_USED in (doc_words or "") and (gen is None or doc_gen == gen))
            flags = list(getattr(self, "_qa_docs", []))
            flags += [False] * (len(self._qa_history) - len(flags))       # (kept in step with the answers)
            self._qa_history = (self._qa_history
                                + [(getattr(self, "_suggest_question", ""), text.strip())]
                                )[-HISTORY_TURNS:]
            self._qa_docs = (flags + [from_docs])[-HISTORY_TURNS:]
            if not getattr(self, "_suggest_auto", False):
                # Give the user time to read what they asked for before an
                # automatic answer may replace it.
                self._hold_until = time.time() + USER_ANSWER_HOLD_S
        note = getattr(self, "_screen_note", "")
        if note:
            self.lbl_status.setText(note)
        else:
            self.lbl_status.setText(self._footer_text(gen, took))

    def _footer_text(self, gen, took):
        """"Answered 14:31:07 · Gusan Cloud · first words 0.8s · AI can be wrong
        - check facts": when, who wrote it, how fast."""
        parts = [f"Answered {time.strftime('%H:%M:%S')}"]
        answered_gen, engine = getattr(self, "_answer_engine", (None, None))
        who = _engine_label(engine) if gen is None or answered_gen == gen else ""
        if who:
            parts.append(who)
        if self._first_token_at:
            parts.append(f"first words {self._first_token_at - self._suggest_started:.1f}s")
        else:
            parts.append(f"done {took:.1f}s")
        if getattr(self, "_last_attached_screen", False):
            parts.append("screen seen")
        doc_gen, doc_words = getattr(self, "_doc_foot", (None, ""))
        if doc_words and (gen is None or doc_gen == gen):
            parts.append(doc_words)               # "from your documents" and the like
        parts.append("AI can be wrong - check facts")
        return " · ".join(parts)

    def _on_auto_toggled(self, on):
        on = bool(on)
        if on != self._auto:
            self._auto = on
            self.settings_changed.emit("live_assist_auto_answer", on)
        self._store("live_assist_auto_answer", on)

    def _set_screen_tooltip(self, on):
        self.btn_screen.setToolTip(
            "Screen context: On. Every answer sees the screen you're working on "
            "(this card left out), so a question, task or error on it gets solved. "
            "Needs an AI engine that can read images. Click to turn off." if on else
            "Screen context: Off. Answers never see your screen - except when you "
            "press Solve screen. Click to turn on.")

    def _on_screen_toggled(self, on):
        self.btn_screen.update()
        self._set_screen_tooltip(on)
        self._richify_tooltips()
        self.settings_changed.emit("live_assist_screen_auto", bool(on))
        self._store("live_assist_screen_auto", bool(on))

    # ── the session's recording ──
    def _audio_parts(self):
        try:
            import meeting_store
            return meeting_store.audio_parts(self._audio_folder)
        except Exception:
            return []

    def _check_audio_ready(self):
        """Show Download audio once THIS session's recording is on disk and
        has stopped growing (it's written right after Stop). Gives up after a
        couple of minutes - a session too short to save writes nothing."""
        try:
            parts = self._audio_parts()
            if len(parts) <= getattr(self, "_audio_base", 0):
                self._audio_polls = getattr(self, "_audio_polls", 0) + 1
                if self._audio_polls > 150:
                    self._audio_folder = None
                return
            size = sum(p.stat().st_size for p in parts)
        except Exception:
            self._audio_folder = None
            return
        if size and size == getattr(self, "_audio_size", -1):
            self._audio_ready = True
            self.btn_audio.show()
        self._audio_size = size

    def _save_audio(self):
        import meeting_store
        from ui.recording_prompts import recording_save_path
        parts = meeting_store.audio_parts(self._audio_folder) if self._audio_folder else []
        if not parts:
            self._set_status("No recording found for this session.")
            return
        path = recording_save_path(self, "Live session " + time.strftime("%Y-%m-%d %H-%M"))
        if not path:
            return
        self.btn_audio.setEnabled(False)
        self._set_status("Saving the recording…")

        def _worker():
            try:
                import audio_export
                out = audio_export.export_recording(parts, path)
                self.sig_export_done.emit(str(out) if out else "", "" if out else "Nothing to export")
            except Exception as e:
                self.sig_export_done.emit("", str(e)[:200])

        threading.Thread(target=_worker, daemon=True).start()

    def _on_export_done(self, path, error):
        self.btn_audio.setEnabled(True)
        if error:
            self._set_status(f"Couldn't save the recording: {error}")
            return
        import os
        self._set_status(f"Recording saved: {os.path.basename(path)}")
        if self.app:
            self.app.track("meeting_exported", {"target": "audio", "ok": True})

    def _on_tick(self):
        self._pump_rules()
        if (self._view == "answer" and self.rules_active() and not self._suggesting
                and time.time() >= getattr(self, "_hold_until", 0.0)):
            self._set_view("feed")             # the typed answer has been read: back to the running results
        if (getattr(self, "_pending_auto", False) and not self._suggesting
                and time.time() >= getattr(self, "_hold_until", 0.0)):
            self._pending_auto = False
            if self._auto and self._meeting_active and self.isVisible():
                self.suggest("", auto=True)
        # Watchdog: Qt can recreate the native window (flag/parent changes)
        # and the exclusion lives on the HWND - re-apply if it went missing.
        if (self._private and self.isVisible()
                and (glass.capture_exclusion_supported() or glass.IS_MAC)
                and not glass.is_remote_session()
                and not glass.is_excluded_from_capture(self)):
            self._apply_glass()
        if self._meeting_active:
            self._update_timer()
            self._refresh_hear()          # what the cloud answered (and a piece that fell back) shows
        if self._audio_folder and not self._audio_ready:
            self._check_audio_ready()
        if self._suggesting:
            el = int(time.time() - self._suggest_started)
            if el >= 8 and not self._first_token_at:
                # A scale-to-zero model endpoint booting after a pause.
                self.txt_suggestion.setPlainText(
                    f"Waking up the AI model… {el}s (only the first answer after a "
                    "pause is slow)")
            elif el >= 3 and not self._first_token_at:
                self.txt_suggestion.setPlainText(f"Thinking… {el}s")
