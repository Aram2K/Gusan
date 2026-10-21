"""Live Assistant page (Gusan 2.0): "hidden on screen share", shown with a picture.

    page = create(main_app)          # ui/shell.py builds it the first time the route opens

What is on it (the v2 LiveAssistant mockup, minus its big headline - the founder wanted
only the shortcut row at the top and the status in one word):
- Hero: "Open Live Assistant", "or" and the keycaps of cfg live_assist_hotkey
  first (no PRO pill beside the button - it sits in the header, see below),
  then the You see / They see picture - ONE screen split by a line that
  slides from end to end by itself and can be dragged (the card on your side;
  on theirs it is absent when hidden, faded to 45% when only partly hidden and
  full when visible - the line moves in every state).
- Beside the page title, for every plan: the PRO pill (the feature is Pro, but
  anyone may open the card and look; Start is what asks for Pro), then the
  TRUTHFUL status (live_context.hide_status) as ONE word in a badge: Hidden,
  May be hidden or Visible; the explanation is its tooltip and accessible
  description, never a sentence on the page.
- About my calls (live_assist_context), Documents (ui/pages/live_documents.py: the files
  the assistant may read - added with a picker or by dropping them on the page, read on a
  worker thread, each with a "Use in answers" switch; docs/redesign/live-documents-spec.md)
  and Past sessions (meeting_store.list_live_sessions(), read on a worker thread).
- The rail "Live Assistant settings": Hide from screen share, Shortcut, Answer
  questions by itself, Let it see my screen, Call language, Answer in, Answers
  written by (Gusan Cloud · DeepSeek V4.1 Flash PRO / This computer · Qwen
  1.5B), Card look; "More options" opens Card see-through, Reset card position
  and Your own key.
Every change goes through app.set_setting (applies at once, no Save button).
When the card exists (app.live_assist) the page also follows its
settings_changed(key, value) and hide_status_changed(key) signals.

Integration notes for Wave 4
----------------------------
* Open Live Assistant works for EVERYONE (Free and guests too - no Pro window
  on Open): app.show_live_assist() when the controller has it (show-only
  path, see the notes in ui/live_assist.py), else app.live_assist.open_card()
  when the card exists, else app.toggle_live_assist() (which creates it and
  shows it). The card itself asks for Pro when Start is pressed
  (_pro_upsell("Live Assistant")) and wears a PRO pill beside its title
  until then.
* "Answers written by": the selected option is always the truth,
  engine_choice.runs_on(app, "live_assistant"). Picking an option writes a NEW
  cfg key ``live_assist_writer`` ("cloud" | "local"; "" in main.DEFAULT =
  the automatic choice). Wave 4: actions.live_engine / engine_choice.
  live_engine honour "local" (as Privacy mode does: a downloaded local model
  or nothing, never the server), so a Pro user's "This computer" sticks; the
  "Gusan Cloud answers while you have Pro." line remains only as a guard. Free +
  Gusan Cloud -> _pro_upsell("Live Assistance"). "Change" -> Settings › Speech & AI.
* Call language / Answer in: "" means "Same as Dictation" / "Same as the
  call". The card still falls back to English for "" (ui/live_assist.py
  _start_listening ``or "en"``, the answer's ``output_lang ... or "en"``):
  make them follow cfg["language"] and the call's language, and consider ""
  as the main.DEFAULT for new installs (saved "en" keeps showing English).
  live_context._LANG_NAMES has no "ar" yet (the prompt would say "ar").
* Past sessions: list_live_sessions() runs on a worker (it scans the disk);
  the first question and the answer count come from
  <meeting>/live_answers.jsonl lines ({"time", "question"}) or meta keys
  "first_question" / "answers" when present - nothing writes them yet (the
  card's answer path should append to live_answers.jsonl, following
  save_history / privacy_mode). Without them a row shows the meeting title, or
  its notes' first line. A row opens app.show_main("meetings", meeting=<folder>)
  - the Meetings page's on_open should accept ``meeting``; "See all" opens
  app.show_main("meetings").
* Shortcut uses the shared ui/components/hotkey_capture.HotkeyCaptureButton
  (keyboard only: allow_mouse=False, as _setup_assist_hotkey registers keys)
  and calls app.suspend_hotkeys(True/False) around a capture when the
  controller has it (the Dictation page does the same). A refused
  set_setting("live_assist_hotkey") says "Dictation uses <keys>." or
  "Couldn't use <keys>." under the keys.
* Hidden status: the badge shows one word (STATUS_WORDS); the full status.tip
  is its tooltip and accessible description.
* The hero button is the current app's #heroButton gradient (ui/styles.py),
  set on the widget; ui/theme could grow a Button kind "hero" for it. The 40 px
  keycaps and the writer radio rows are local for the same reason.
* ui/components/icons has no open "eye": the Visible states use
  ui.icons.eye_icon.
* Navigation uses app.show_main(route, **opts) (main.py); without it the page
  falls back to app.show_settings().
"""

import datetime
import json
import logging
import math
import os
import re

from PySide6.QtCore import (QEasingCurve, QEvent, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation,
                            Signal)
from PySide6.QtGui import (QColor, QFontMetricsF, QGuiApplication, QLinearGradient, QPainter, QPainterPath,
                           QPen)
from PySide6.QtWidgets import (QBoxLayout, QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel,
                               QLayout, QPlainTextEdit, QRadioButton, QSizePolicy, QSlider,
                               QVBoxLayout, QWidget)

import live_context
from ui import live_docs_ui, theme
from ui.components import (Button, Card, Chip, IconButton, PageHeader, ProTag, Segmented,
                           SettingRow, SettingsRail, Toggle, icons, label)
from ui.components.controls import keycap_labels
from ui.components.hotkey_capture import HotkeyCaptureButton
from ui.pages import live_demo
from ui.pages.live_documents import DocumentsSection

logger = logging.getLogger("transcribe")

TITLE = "Live Assistant"
SUB = "Answers questions while you talk."
UPSELL = "Live Assistance"                  # the Pro window's feature name
SHORTCUT_REFUSED = "Couldn't use %s."             # one line under the keys
SHORTCUT_IS_DICTATION = "Dictation uses %s."
SHORTCUT_IS_SMART = "%s runs Smart Actions. Pick another."
PRO_USES_CLOUD = "Gusan Cloud answers while you have Pro."
PRIVACY_KEEPS_LOCAL = "Privacy mode keeps answers on this computer."
# The one word of the status badge. "unavailable" and "failed" cannot hide the
# card, so they say Visible; macOS's best effort never says Hidden.
STATUS_WORDS = {"on": "Hidden", "partial": "May be hidden"}
STATUS_WORD_DEFAULT = "Visible"

# value -> shown name. "" follows Dictation / the call (see the notes above).
CALL_LANGS = [("", "Same as Dictation"), ("auto", "Detect automatically"),
              ("multi", "Mixed languages"), ("en", "English"), ("ar", "Arabic"),
              ("hy", "Armenian"), ("fr", "French"), ("de", "German"), ("ru", "Russian"),
              ("es", "Spanish")]
ANSWER_LANGS = [("", "Same as the call"), ("en", "English"), ("ar", "Arabic"),
                ("hy", "Armenian"), ("fr", "French"), ("de", "German"), ("ru", "Russian"),
                ("es", "Spanish")]

CLOUD_LIVE_MODEL = "DeepSeek V4.1 Flash"    # engine_choice.CLOUD_LIVE_MODEL (read from there)
LOCAL_DEFAULT_MODEL = "Qwen 1.5B"
MAX_SESSIONS = 4
CONTEXT_SAVE_MS = 700
CONTEXT_MIN_H = 66                          # About my calls: two lines tall...
CONTEXT_MAX_LINES = 5                       # ...growing to five as the text needs

_GENERIC_TITLE = re.compile(r"^Live session \d{1,2}:\d{2}$")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def create(main_app):
    return LiveAssistantPage(main_app)


# ── small helpers ───────────────────────────────────────────────────────────
def _is_pro(app):
    try:
        return bool(app.is_pro())
    except Exception:
        return False


def _first_name(app):
    auth = getattr(app, "auth", None)
    if not bool(getattr(auth, "is_authenticated", False)):
        return ""
    name = (getattr(auth, "user_name", None) or "").strip()
    if not name:
        email = (getattr(auth, "user_email", None) or "").strip()
        name = email.split("@")[0] if email else ""
    return name.split()[0] if name else ""


def _norm_hotkey(hk):
    """'Ctrl + Alt + A' -> 'ctrl+alt+a' (settings_apply.norm_hotkey)."""
    return "+".join(p.strip().lower() for p in str(hk or "").split("+") if p.strip())


def _alive(obj):
    try:
        import shiboken6
        return shiboken6.isValid(obj)
    except Exception:
        return True


def day_and_time(started, now=None):
    """("Today" | "Yesterday" | "Fri" | "29 Sep" | "29 Sep 2025", "14:18") for a
    meeting's "YYYY-MM-DD HH:MM:SS" start."""
    try:
        dt = datetime.datetime.strptime(str(started or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(started or ""), ""
    now = now or datetime.datetime.now()
    days = (now.date() - dt.date()).days
    if days == 0:
        day = "Today"
    elif days == 1:
        day = "Yesterday"
    elif 1 < days < 7:
        day = _DAYS[dt.weekday()]
    elif dt.year == now.year:
        day = "%d %s" % (dt.day, _MONTHS[dt.month - 1])
    else:
        day = "%d %s %d" % (dt.day, _MONTHS[dt.month - 1], dt.year)
    return day, dt.strftime("%H:%M")


def length_text(seconds):
    """38 min, 1 h 5 min ("" when unknown)."""
    try:
        sec = int(seconds or 0)
    except (TypeError, ValueError):
        sec = 0
    if sec <= 0:
        return ""
    mins = max(1, int(round(sec / 60.0)))
    if mins < 60:
        return "%d min" % mins
    h, m = divmod(mins, 60)
    return "%d h %d min" % (h, m) if m else "%d h" % h


def session_meta(sess):
    """"38 min · 6 answers" for a row."""
    parts = []
    ln = length_text(sess.get("duration_sec"))
    if ln:
        parts.append(ln)
    n = sess.get("answers")
    if isinstance(n, int) and n >= 0:
        parts.append("1 answer" if n == 1 else "%d answers" % n)
    return " · ".join(parts)


def _answers_log(folder):
    """(first question, count) from <folder>/live_answers.jsonl or meta
    ("first_question", "answers"); ("", None) when nothing was kept."""
    if not folder:
        return "", None
    path = os.path.join(folder, "live_answers.jsonl")
    if os.path.isfile(path):
        first, n = "", 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    n += 1
                    if not first and isinstance(item, dict):
                        first = str(item.get("question") or "").strip()
            return first, n
        except OSError:
            pass
    try:
        import meeting_store
        meta = meeting_store.load_meta(folder)
    except Exception:
        meta = {}
    first = str(meta.get("first_question") or "").strip()
    n = meta.get("answers")
    return first, (n if isinstance(n, int) and not isinstance(n, bool) else None)


def load_sessions():
    """Past sessions for the page, newest first. Reads the disk: call it on a
    worker (the page does, through api_keys.run_then)."""
    import meeting_store
    out = []
    for m in meeting_store.list_live_sessions():
        folder = str(m.get("dir") or "")
        question, answers = _answers_log(folder)
        if question:
            text = "“%s”" % question
        else:
            title = str(m.get("title") or "").strip()
            if title and not _GENERIC_TITLE.match(title) and title != "Untitled Meeting":
                text = title
            else:
                try:
                    text = meeting_store.summary_preview(meeting_store.load_notes(folder))
                except Exception:
                    text = ""
                text = text or "Live session"
        out.append({"dir": folder, "id": m.get("id", ""),
                    "started": str(m.get("started") or m.get("timestamp") or ""),
                    "duration_sec": m.get("duration_sec") or 0,
                    "text": text, "answers": answers})
    return out


def writer_view(app):
    """What "Answers written by" shows: {"selected": "cloud" | "local",
    "cloud_model", "local_name", "local_icon", "local_model", "kind"} - the
    selection is what really answers (engine_choice.runs_on)."""
    cfg = getattr(app, "cfg", None) or {}
    pro = _is_pro(app)
    view = {"selected": "cloud" if pro and not cfg.get("privacy_mode") else "local",
            "cloud_model": CLOUD_LIVE_MODEL, "local_name": "This computer",
            "local_icon": "laptop", "local_model": LOCAL_DEFAULT_MODEL, "kind": ""}
    try:
        import engine_choice as ec
    except Exception:
        logger.debug("engine_choice unavailable", exc_info=True)
        return view
    view["cloud_model"] = ec.CLOUD_LIVE_MODEL
    try:
        info = ec.LOCAL_WRITERS.get(ec.local_writer(cfg))
        local_model = info[1] if info else LOCAL_DEFAULT_MODEL
    except Exception:
        local_model = LOCAL_DEFAULT_MODEL
    view["local_model"] = local_model
    try:
        r = ec.runs_on(app, "live_assistant")
    except Exception:
        logger.debug("runs_on(live_assistant) failed", exc_info=True)
        return view
    kind = r.get("kind")
    view["kind"] = kind
    if kind == "cloud":
        view["selected"] = "cloud"
        view["cloud_model"] = r.get("model_name") or view["cloud_model"]
    elif kind == "own_key":
        view.update(selected="local", local_name=ec.OWN_KEY, local_icon="key",
                    local_model=" · ".join(x for x in (r.get("plain"), r.get("model_name")) if x))
    elif kind == "local":
        name = r.get("model_name") or local_model
        if r.get("problem") == "needs_download":
            name += " · not downloaded"
        view.update(selected="local", local_model=name)
    else:                                   # "none": nothing that can answer yet
        view.update(selected="local", local_model=local_model + " · not downloaded")
    return view


def _status_pixmap(name, size, color, sw=1.7):
    """An icon from ui/components/icons, or the classic open eye ("eye")."""
    if name in icons.IC:
        return icons.pixmap(name, size, color, sw)
    try:
        from ui.icons import eye_icon
        return eye_icon(True, size, theme.qcolor(color)).pixmap(size, size)
    except Exception:
        return icons.pixmap("eye_off", size, color, sw)


def _rich_tip(text):
    """A tooltip that wraps (a plain one is a single very long line)."""
    if not text:
        return ""
    import html
    return "<p style='white-space:normal'>%s</p>" % html.escape(text, quote=False)


def _unverified_status(wanted):
    """When hide_status() itself fails: say so, never "Hidden"."""
    key = "unavailable" if wanted else "off"
    return live_context.HideStatus(
        key, live_context.HIDE_TEXT[key], live_context.HIDE_TONE[key], live_context.HIDE_ICON[key],
        "Can't confirm it's hidden" if wanted else "Visible on screen share",
        "Couldn't check whether it's hidden, so assume it shows in a share." if wanted
        else "Shows on a shared screen and in recordings.")


def _fallback_status(wanted):
    key = "on" if wanted else "off"
    return live_context.HideStatus(key, live_context.HIDE_TEXT[key], live_context.HIDE_TONE[key],
                                   live_context.HIDE_ICON[key],
                                   "Hidden on screen share" if wanted else "Visible on screen share", "")


# ── hero pieces ─────────────────────────────────────────────────────────────
_HERO_QSS = """
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #8b5cf6);
    color: #ffffff; border: none; border-radius: 10px; padding: 0px 22px;
}
QPushButton:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563eb, stop:1 #7c3aed); }
QPushButton:pressed { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1d4ed8, stop:1 #6d28d9); }
QPushButton[focusVisible="true"] { border: 2px solid #93c5fd; }
"""


class HeroButton(Button):
    """The current app's #heroButton (blue to violet), 44 px: Open Live Assistant."""

    def __init__(self, text, parent=None):
        super().__init__(text, "primary", "l", parent=parent)
        self.setFont(theme.font(14, 700))
        self.setStyleSheet(_HERO_QSS)


class MidKeycaps(QWidget):
    """40 px keycaps (between the small and the big ones) for the hero."""

    def __init__(self, hotkey="", parent=None):
        super().__init__(parent)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 4)       # room for the soft shadow
        self._lay.setSpacing(6)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.keys = []
        self.set_hotkey(hotkey)

    def set_hotkey(self, hotkey):
        while self._lay.count():
            w = self._lay.takeAt(0).widget()
            if w is not None:
                w.hide()
                w.deleteLater()
        self.keys = keycap_labels(hotkey)
        cap_qss = ("QLabel { background: #ffffff; color: %s; border: 1px solid %s; border-bottom: 3px solid %s; "
                   "border-radius: 9px; padding: 0px 11px; }"
                   % (theme.qss_color("ink"), theme.qss_color("line2"), theme.qss_color("line2")))
        for i, k in enumerate(self.keys):
            if i:
                plus = QLabel("+", self)
                plus.setProperty("gusan", "kbdPlus")
                plus.setFont(theme.font(14, 700))
                plus.setAccessibleName("plus")
                self._lay.addWidget(plus, 0, Qt.AlignVCenter)
            cap = QLabel(k, self)
            cap.setAlignment(Qt.AlignCenter)
            cap.setFont(theme.font(16, 700, display=True))
            cap.setStyleSheet(cap_qss)
            cap.setFixedHeight(40)
            cap.setMinimumWidth(40)
            theme.elevate(cap, "card")
            self._lay.addWidget(cap, 0, Qt.AlignVCenter)
        for i in range(self._lay.count()):
            # Shown now, not on the layout's queued show: until then a visible
            # parent measures them as empty (the hero would never wrap).
            w = self._lay.itemAt(i).widget()
            if w is not None:
                w.show()
        text = " + ".join(self.keys)
        self.setAccessibleName(text)
        self.setToolTip(text)
        self.updateGeometry()


class HideChip(Chip):
    """The status in one word - Hidden / May be hidden / Visible - with its
    icon, beside the page title. The explanation is only its tooltip."""

    def __init__(self, parent=None):
        super().__init__("Hidden", "ok", None, parent)
        self._pic = QLabel(self)
        self.layout().insertWidget(0, self._pic)
        self.set_state("Hidden", "ok", "incognito", "Hidden on screen share", "")

    def set_state(self, word, tone, icon, name, tip):
        self.setText(word)
        if self.property("tone") != tone:
            self.setProperty("tone", tone)
            for w in (self, self._text):            # the stylesheet keys on [tone]
                self.style().unpolish(w)
                self.style().polish(w)
        self._pic.setPixmap(_status_pixmap(icon, 13, self._ICON_TONE.get(tone, "text2"), 2))
        self.setAccessibleName(name)
        self.setAccessibleDescription(tip)
        self.setToolTip(_rich_tip(tip))


# ── the You see / They see picture ──────────────────────────────────────────
# ONE screen, split by a line. Left of the line is what you see (your shared
# slide with the Live Assistant card on it); right of it is what the others on
# the call see: the same slide, without the card while it is hidden. On its own
# the line slides through the card and back, so the difference shows itself;
# grab it, click the screen or use the arrow keys to look closer. The picture
# only moves while you can see it, and not at all when Windows' animation
# effects are off.
K = 1.5                      # the slide is drawn at 384 x 226 slide units, scaled up (the card has its own scale)
INNER_W, INNER_H = 384, 226
BEZEL = 6
SCREEN_W = int(INNER_W * K) + 2 * BEZEL
SCREEN_H = int(INNER_H * K) + 2 * BEZEL
LABEL_H, LABEL_GAP, STAND_H = 22, 10, 15
SHADOW_ROOM = 18            # part of the 22 px gap under the picture: the monitor's soft shadow
DESIGN_W = SCREEN_W
DESIGN_H = LABEL_H + LABEL_GAP + SCREEN_H + STAND_H + SHADOW_ROOM
GLASS_R = 7                 # the glass's corner radius (design units)
HANDLE_R = 17               # the round handle on the line
SPLIT_MIN, SPLIT_MAX = 0.0, 1.0     # as a share of the glass: the line runs from end to end
STOP_THEY = SPLIT_MIN       # the line rests here ("They see": the whole screen is theirs) ...
STOP_YOU = SPLIT_MAX        # ... or here ("You see": the whole screen is yours)
CARD_MARGIN = 14            # the banners' gap to the glass's edges, in slide units
CARD_GAP_RIGHT = 34         # the card floats centre-right: this much glass to its right (design units) ...
CARD_DROP = 6               # ... and this far under the middle (the banners keep the top)
SLIDE_KEEP = 108            # the slide's title, lines and chart end here (slide units): the card never reaches them
PAD = 4                    # room around a cached label / handle image (the faces' ring, a shadow)
STEP, BIG_STEP = 0.05, 0.20   # arrow keys / Page Up, Page Down
REST_STATIC = 0.62          # where the line waits when it never moves by itself (animations off): the card is cut, both sides show
SCALE_STEPS = 64            # the painter scale snaps down to 1/64: dragging the window edge rebuilds the cached images ~25 times, not every pixel

ANSWER_HTML = live_demo.ANSWER_HTML          # the card's example answer (the card itself: ui/pages/live_demo.py)
_FACES_YOU = (("#60a5fa", "#2563eb"),)
_FACES_THEM = (("A", "#f472b6", "#db2777"), ("D", "#94a3b8", "#475569"), ("M", "#fbbf24", "#d97706"))


_SHADOWS = {}


def _shadow_image(w, h, radius, blur, rgba):
    """A CSS box-shadow as an image (w + 2*blur by h + 2*blur): the rounded
    rect drawn crisp, averaged down and smoothed back up - a cheap blur."""
    key = (int(w), int(h), int(radius), int(blur), rgba)
    img = _SHADOWS.get(key)
    if img is not None:
        return img
    from PySide6.QtGui import QImage
    pad = int(blur)
    full = QImage(int(w) + 2 * pad, int(h) + 2 * pad, QImage.Format_ARGB32_Premultiplied)
    full.fill(Qt.transparent)
    p = QPainter(full)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(theme.qcolor(rgba))
    p.drawRoundedRect(QRectF(pad, pad, w, h), radius, radius)
    p.end()
    k = max(2, int(blur / 2.5))
    img = full
    for _ in range(3):                      # three box-ish passes ~ a gaussian
        small = img.scaled(max(1, full.width() // k), max(1, full.height() // k),
                           Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        img = small.scaled(full.width(), full.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    _SHADOWS[key] = img
    return img


def _soft_shadow(p, rect, radius, dy, blur, rgba):
    """CSS "0 dy blur rgba" under ``rect``."""
    img = _shadow_image(rect.width(), rect.height(), radius, blur, rgba)
    pad = int(blur)
    p.drawImage(QPointF(rect.left() - pad, rect.top() - pad + dy), img)


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _animations_on():
    """False when Windows' "Animation effects" are off: nothing decorative moves."""
    try:
        from ui import glass
        return bool(glass.animations_enabled())
    except Exception:
        return True


class ScreensPicture(QWidget):
    """One monitor split by a draggable line: "You see" on its left (your shared
    slide with the Live Assistant card on it), "They see" on its right (the same
    slide, no card - or with it, faded or not, when the card is NOT hidden).

    The line glides from end to end by itself (STOP_YOU <-> STOP_THEY)
    while the picture is on screen, the pointer is away from it and the app is
    active; a hand on it (drag, click, arrow keys) takes over and the glide
    picks up again RESUME_MS later. It runs from one end of the screen to the
    other in every state; when the card shows to everyone (status off / failed /
    unavailable) the card is on both sides, so the sweep changes only the
    banners. The card is a miniature of the real one (ui/pages/live_demo.py)
    floating at the centre-right of the glass, clear of the slide's title and
    chart, in the look the rail's Card look says (light or dark, set_card_look).
    Drawn with QPainter at DESIGN_W (588) wide and scaled down to fit
    narrower columns; each side, the monitor, the labels and the handle are
    painted once into pixmaps and a frame is a few blits plus the line."""

    AUTOPLAY = True              # tests switch it off
    FIRST_HOLD_MS = 1400         # the card shows for this long before the first slide
    HOLD_MS = 2200               # resting at either end
    GLIDE_MS = 1900              # one slide, end to end (shorter for a shorter way)
    RESUME_MS = 6000             # after a hand let go
    RETRY_MS = 1500              # the slide is due but can't play now (hover, tab hidden ...)

    def __init__(self, parent=None):
        super().__init__(parent)
        sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMouseTracking(True)
        self._initial = "Y"
        self._owner = ""
        self._status = _fallback_status(True)
        self._card_look = "light"    # the card's look, as the page's Card look says (light | dark)
        self._split = STOP_YOU
        self._hover = False          # the pointer is over the glass
        self._dragging = False
        self._grab = 0.0             # where on the handle it was taken hold of (design units)
        self._focus_ring = False     # keyboard focus (not a click) shows a ring on the handle
        self._cache_key = None
        self._cache = {}
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve(QEasingCurve.InOutCubic))
        self._anim.valueChanged.connect(self._on_glide)
        self._anim.finished.connect(self._glide_done)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timer_fired)
        self._apply_status()

    # data
    def set_owner(self, first_name):
        first_name = (first_name or "").strip()
        self._initial = first_name[:1].upper() if first_name else "Y"
        self._owner = first_name
        self._cache.clear()
        self._cache_key = None
        self._update_name()
        self.update()

    def set_status(self, status):
        old, self._status = self._status, status
        if all(getattr(old, f, None) == getattr(status, f, None) for f in ("key", "text", "tone", "icon")):
            return          # the page re-applies the same status on every refresh: leave the line where it is
        self._cache.clear()
        self._cache_key = None
        self._apply_status()
        self.update()

    def set_card_look(self, name):
        """The card's look, "light" or "dark" (the page's Card look): the picture's card wears it too.
        Anything else is the light one. The line stays where it is."""
        name = name if name in live_demo.PALETTES else "light"
        if name == self._card_look:
            return
        self._card_look = name
        self._cache.clear()
        self._cache_key = None
        self.update()

    def card_look(self):
        return self._card_look

    def owner_banner(self):
        return "%s’s screen" % self._owner if self._owner else "Shared screen"

    def they_see_card(self):
        return getattr(self._status, "key", "on") != "on"

    def moves(self):
        """The line can always move (kept for callers: every status moves it)."""
        return True

    def split(self):
        return self._split

    def set_split(self, value):
        value = _clamp(float(value), SPLIT_MIN, SPLIT_MAX)
        if value - SPLIT_MIN < 1e-6:            # float residue from key steps: the ends are exact
            value = SPLIT_MIN
        elif SPLIT_MAX - value < 1e-6:
            value = SPLIT_MAX
        if abs(value - self._split) > 1e-6:
            self._split = value
            self.update()

    def playing(self):
        """The line is sliding, or about to."""
        return self._anim.state() == QVariantAnimation.Running or self._timer.isActive()

    def _they_opacity(self):
        key = getattr(self._status, "key", "on")
        return 0.0 if key == "on" else (0.45 if key == "partial" else 1.0)

    def _apply_status(self):
        self.setFocusPolicy(Qt.StrongFocus)
        self._update_name()
        if not self.isVisible():
            self._split = self._rest()
        elif self._hover or self._dragging:
            self.update()               # a hand is on the line: it stays (the glide resumes when it lets go)
        else:
            self._start_loop()

    def _update_name(self):
        key = getattr(self._status, "key", "on")
        if key == "on":
            they = "What the others on the call see: the same slide, no card"
        elif key == "partial":
            they = ("What the others on the call may see: the same slide, with the card on it "
                    "(shown faded: macOS only partly hides it)")
        else:
            they = "What the others on the call see: the same slide, with the card on it"
        text = ("Your screen while you share it: the Live Assistant card is on it. " + they + ". "
                "Drag the line, or use the arrow keys, to compare the two.")
        self.setAccessibleName("You see and They see")
        self.setAccessibleDescription(text)

    # size
    def hasHeightForWidth(self):
        return True

    @staticmethod
    def _scale_for(w):
        s = min(1.0, max(1, w) / float(DESIGN_W))
        return 1.0 if s >= 1.0 else max(1, math.floor(s * SCALE_STEPS)) / float(SCALE_STEPS)

    def heightForWidth(self, w):
        return int(math.ceil(DESIGN_H * self._scale_for(w)))

    def sizeHint(self):
        return QSize(DESIGN_W, DESIGN_H)

    def minimumSizeHint(self):
        return QSize(260, self.heightForWidth(260))

    def _scale(self):
        return self._scale_for(self.width())

    # the line's own motion
    def _can_play(self):
        return bool(self.AUTOPLAY and self.moves() and self.isVisible() and not self._hover
                    and not self._dragging and _animations_on()
                    and QGuiApplication.applicationState() == Qt.ApplicationActive
                    and not self.visibleRegion().isEmpty())

    def _rest(self):
        """Where the line waits: at the "You see" end while it moves by itself,
        mid-card (both sides show) when it never will."""
        return STOP_YOU if _animations_on() else REST_STATIC

    def _start_loop(self):
        """From the top: the card shows, then the line slides through it."""
        self._stop_motion()
        if self.moves():
            self._split = self._rest()
        if self.AUTOPLAY and self.moves():
            self._timer.start(self.FIRST_HOLD_MS)
        self.update()

    def _stop_motion(self):
        self._timer.stop()
        self._anim.stop()

    def _poke(self):
        """A hand took over: stop moving by itself, and pick it up again later."""
        self._stop_motion()
        if self.AUTOPLAY and self.moves():
            self._timer.start(self.RESUME_MS)

    def _timer_fired(self):
        if not self._can_play():
            self._timer.start(self.RETRY_MS)
            return
        far = STOP_THEY if abs(self._split - STOP_THEY) > abs(self._split - STOP_YOU) else STOP_YOU
        self._glide(far)

    def _glide(self, target):
        self._anim.stop()
        start = float(self._split)
        way = abs(target - start)
        # Re-keying an animation that was stopped part-way re-emits valueChanged
        # from its OLD keys: silenced, or the line jumps there and starts from it.
        self._anim.blockSignals(True)
        try:
            self._anim.setDuration(max(500, int(self.GLIDE_MS * way / (STOP_YOU - STOP_THEY))))
            self._anim.setStartValue(start)
            self._anim.setEndValue(float(target))
        finally:
            self._anim.blockSignals(False)
        self._split = start
        self._anim.start()

    def _on_glide(self, value):
        self._split = float(value)
        self.update()

    def _glide_done(self):
        if self.AUTOPLAY and self.moves():
            self._timer.start(self.HOLD_MS)

    # events
    def showEvent(self, e):
        super().showEvent(e)
        self._start_loop()

    def hideEvent(self, e):
        self._stop_motion()
        if self.moves():
            self._split = self._rest()
        self._hover = self._dragging = False
        self.unsetCursor()
        super().hideEvent(e)

    def _design_pos(self, pos):
        s = self._scale()
        return QPointF(pos.x() / s, pos.y() / s)

    def _inner_rect(self):
        return QRectF(BEZEL, LABEL_H + LABEL_GAP + BEZEL, INNER_W * K, INNER_H * K)

    def _on_glass(self, d):
        inner = self._inner_rect()
        return inner.adjusted(-BEZEL, -BEZEL, BEZEL, BEZEL).contains(d)

    def _split_at(self, d, grab=0.0):
        """The line's position for a pointer at ``d``; ``grab`` keeps the offset
        at which the handle was taken hold of."""
        inner = self._inner_rect()
        return _clamp((d.x() + grab - inner.left()) / inner.width(), SPLIT_MIN, SPLIT_MAX)

    def _handle_hot(self):
        return self._dragging or self._hover

    def _handle_x(self, inner, split):
        """Where the handle is: on the line, but never past the glass - at either
        end it rests against the edge while the line itself goes to the end."""
        r = HANDLE_R * 1.1
        return _clamp(inner.left() + split * inner.width(), inner.left() + r, inner.right() - r)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            d = self._design_pos(e.position())
            if self._on_glass(d):
                self._dragging = True
                self._stop_motion()
                # Taking hold of the handle keeps where on it you grabbed; anywhere
                # else on the glass the line jumps to the pointer.
                inner = self._inner_rect()
                line_x = inner.left() + self._split * inner.width()
                hx = self._handle_x(inner, self._split)
                on_handle = (abs(d.x() - hx) <= HANDLE_R * 1.1
                             and abs(d.y() - inner.center().y()) <= HANDLE_R * 1.1)
                if on_handle:
                    self._grab = hx - d.x()              # the handle stays under the pointer
                elif abs(d.x() - line_x) <= 4:
                    self._grab = line_x - d.x()
                else:
                    self._grab = 0.0
                self.set_split(self._split_at(d, self._grab))
                self.update()
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        d = self._design_pos(e.position())
        if self._dragging:
            self.set_split(self._split_at(d, self._grab))
            e.accept()
            return
        over = self._on_glass(d)
        if over != self._hover:
            self._hover = over
            self.update()
            if over:
                self.setCursor(Qt.SizeHorCursor)
                self._stop_motion()             # a hand is coming: the line holds still
            else:
                self.unsetCursor()
                self._poke()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._dragging and e.button() == Qt.LeftButton:
            self._dragging = False
            # Hover was not followed while dragging: look where the pointer is now.
            over = self._on_glass(self._design_pos(e.position()))
            if over != self._hover:
                self._hover = over
                if over:
                    self.setCursor(Qt.SizeHorCursor)
                else:
                    self.unsetCursor()
            self._poke()
            self.update()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def leaveEvent(self, e):
        if self._hover:
            self._hover = False
            self.unsetCursor()
            self.update()
            self._poke()
        super().leaveEvent(e)

    def focusInEvent(self, e):
        self._focus_ring = e.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason)
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):
        self._focus_ring = False
        super().focusOutEvent(e)
        self.update()

    def keyPressEvent(self, e):
        step = {Qt.Key_Left: -STEP, Qt.Key_Down: -STEP, Qt.Key_Right: STEP, Qt.Key_Up: STEP,
                Qt.Key_PageDown: -BIG_STEP, Qt.Key_PageUp: BIG_STEP}.get(e.key())
        if step is not None and self.moves():
            self._stop_motion()
            self.set_split(self._split + step)
            self._poke()
            e.accept()
            return
        if e.key() in (Qt.Key_Home, Qt.Key_End) and self.moves():
            self._stop_motion()
            self.set_split(SPLIT_MIN if e.key() == Qt.Key_Home else SPLIT_MAX)
            self._poke()
            e.accept()
            return
        super().keyPressEvent(e)

    # paint
    def paintEvent(self, _e):
        s = self._scale()
        dpr = self.devicePixelRatioF()
        self._ensure_cache(s, dpr)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.scale(s, s)
        split = self._split
        self._paint_labels(p, split)
        y0 = LABEL_H + LABEL_GAP
        p.drawPixmap(QPointF(0, y0), self._cache["body"])
        inner = self._inner_rect()
        they, you = self._cache["they"], self._cache["you"]
        p.drawPixmap(inner.topLeft(), they)
        per_unit = you.width() / inner.width()
        cut = int(round(split * you.width()))
        if cut > 0:
            p.drawPixmap(QRectF(inner.left(), inner.top(), cut / per_unit, inner.height()), you,
                         QRectF(0, 0, cut, you.height()))
        self._paint_line(p, inner, split)
        p.end()

    def _ensure_cache(self, s, dpr):
        key = (round(s, 4), round(dpr, 3), getattr(self._status, "key", "on"),
               STATUS_WORDS.get(getattr(self._status, "key", "on"), STATUS_WORD_DEFAULT), self._owner, self._initial)
        if key == self._cache_key and self._cache:
            return
        self._cache_key = key
        font = theme.font(14, 700)
        text_w = QFontMetricsF(font).horizontalAdvance("They see")
        faces_w = 2 * 16 + 22
        they_w = faces_w + 8 + text_w + 2
        self._cache = {"body": self._render_body(s, dpr),
                       "you": self._render_scene(True, s, dpr),
                       "they": self._render_scene(False, s, dpr),
                       "you_label": self._render_label(True, 22 + 8 + QFontMetricsF(font).horizontalAdvance("You see") + 2,
                                                       s, dpr),
                       "they_label": self._render_label(False, they_w, s, dpr),
                       "they_x": DESIGN_W - they_w,
                       "handle": self._render_handle(1.0, s, dpr),
                       "handle_hot": self._render_handle(1.1, s, dpr)}

    def _pixmap(self, w, h, s, dpr):
        """A transparent pixmap of w x h design units, one device pixel per
        device pixel of the widget (the painter scales by s)."""
        from PySide6.QtGui import QPixmap
        pm = QPixmap(max(1, int(round(w * s * dpr))), max(1, int(round(h * s * dpr))))
        pm.setDevicePixelRatio(dpr * s)
        pm.fill(Qt.transparent)
        return pm

    def _render_body(self, s, dpr):
        """The monitor without its picture: soft shadow, bezel, neck and foot."""
        pm = self._pixmap(SCREEN_W, SCREEN_H + STAND_H + SHADOW_ROOM, s, dpr)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        face = QRectF(0, 0, SCREEN_W, SCREEN_H)
        _soft_shadow(p, face, 12, 8, 30, "rgba(15,23,42,0.12)")        # var(--shadow-lg), kept inside
        p.setPen(Qt.NoPen)
        p.setBrush(theme.qcolor("ink"))
        p.drawRoundedRect(face, 12, 12)
        cx = SCREEN_W / 2.0
        neck = QRectF(cx - 17, SCREEN_H, 34, 10)
        g = QLinearGradient(neck.topLeft(), neck.bottomLeft())
        g.setColorAt(0, QColor("#334155"))
        g.setColorAt(1, QColor("#475569"))
        p.fillRect(neck, g)
        p.setBrush(QColor("#475569"))
        p.drawRoundedRect(QRectF(cx - 46, SCREEN_H + 10, 92, 5), 2.5, 2.5)
        p.end()
        return pm

    def _render_scene(self, you, s, dpr):
        """One side of the glass: the slide with that side's banner, and the
        card where that side shows it."""
        pm = self._pixmap(INNER_W * K, INNER_H * K, s, dpr)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, INNER_W * K, INNER_H * K), GLASS_R, GLASS_R)
        p.setClipPath(clip)
        p.scale(K, K)
        inner = QRectF(0, 0, INNER_W, INNER_H)
        p.fillRect(inner, QColor("#ffffff"))
        self._slide(p, inner)
        if you:
            self._banner(p, inner, "Sharing", dot=True, right=False)
            self._mini_card(p, s * dpr)
        else:
            self._banner(p, inner, self.owner_banner(), dot=False, right=True)
            opacity = self._they_opacity()
            if opacity > 0:
                p.setOpacity(opacity)
                self._mini_card(p, s * dpr)
                p.setOpacity(1.0)
        p.end()
        return pm

    def _face(self, p, x, letter, a, b, ring=False):
        r = QRectF(x, 0, 22, 22)
        if ring:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#eef2f8"))
            p.drawEllipse(r.adjusted(-2, -2, 2, 2))
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, QColor(a))
        g.setColorAt(1, QColor(b))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(r)
        p.setPen(QColor("#ffffff"))
        p.setFont(theme.font(10, 700))
        p.drawText(r, Qt.AlignCenter, letter)

    def _render_label(self, you, w, s, dpr):
        """"You see" + its face, or "They see" + the three faces, as an image."""
        pm = self._pixmap(w + 2 * PAD, LABEL_H + 2 * PAD, s, dpr)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.translate(PAD, PAD)
        font = theme.font(14, 700)
        p.setPen(theme.qcolor("ink"))
        p.setFont(font)
        if you:
            a, b = _FACES_YOU[0]
            self._face(p, 0, self._initial, a, b)
            p.setPen(theme.qcolor("ink"))
            p.setFont(font)
            p.drawText(QRectF(22 + 8, 0, w, LABEL_H), Qt.AlignVCenter | Qt.AlignLeft, "You see")
        else:
            for i, (letter, fa, fb) in enumerate(_FACES_THEM):
                self._face(p, i * 16, letter, fa, fb, ring=True)
            p.setPen(theme.qcolor("ink"))
            p.setFont(font)
            p.drawText(QRectF(2 * 16 + 22 + 8, 0, w, LABEL_H), Qt.AlignVCenter | Qt.AlignLeft, "They see")
        p.end()
        return pm

    def _paint_labels(self, p, split):
        """"You see" over the left end, "They see" over the right end; the side
        the line leaves little room is dimmed."""
        you_alpha = 0.4 + 0.6 * _clamp(split / 0.35, 0.0, 1.0)
        they_alpha = 0.4 + 0.6 * _clamp((1.0 - split) / 0.35, 0.0, 1.0)
        p.setOpacity(you_alpha)
        p.drawPixmap(QPointF(-PAD, -PAD), self._cache["you_label"])
        p.setOpacity(they_alpha)
        p.drawPixmap(QPointF(self._cache["they_x"] - PAD, -PAD), self._cache["they_label"])
        p.setOpacity(1.0)

    def _render_handle(self, grow, s, dpr):
        """The round handle (shadow, white disc, "‹ ›") centred in an image."""
        r0 = HANDLE_R * 1.1
        half = r0 + 12 + 3
        pm = self._pixmap(2 * half, 2 * half, s, dpr)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = half
        base = QRectF(c - HANDLE_R, c - HANDLE_R, 2 * HANDLE_R, 2 * HANDLE_R)
        _soft_shadow(p, base, HANDLE_R, 3, 12, "rgba(15,23,42,0.35)")
        r = QRectF(c - HANDLE_R * grow, c - HANDLE_R * grow, 2 * HANDLE_R * grow, 2 * HANDLE_R * grow)
        p.setPen(QPen(QColor(15, 23, 42, 46), 1))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(r)
        p.setPen(QPen(theme.qcolor("ink"), 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        for sign in (-1, 1):                        # "‹ ›"
            path = QPainterPath(QPointF(c + sign * 4.5, c - 5.5))
            path.lineTo(QPointF(c + sign * 9.5, c))
            path.lineTo(QPointF(c + sign * 4.5, c + 5.5))
            p.drawPath(path)
        p.end()
        return pm

    def _paint_line(self, p, inner, split):
        x = inner.left() + split * inner.width()
        p.save()
        glass = QPainterPath()                  # at either end the line is the glass's edge, not a stripe on the bezel
        glass.addRoundedRect(inner, GLASS_R, GLASS_R)
        p.setClipPath(glass)
        p.setPen(QPen(QColor(255, 255, 255, 215), 4.5, Qt.SolidLine, Qt.FlatCap))
        p.drawLine(QPointF(x, inner.top()), QPointF(x, inner.bottom()))
        p.setPen(QPen(theme.qcolor("ink"), 2, Qt.SolidLine, Qt.FlatCap))
        p.drawLine(QPointF(x, inner.top()), QPointF(x, inner.bottom()))
        p.restore()
        hx = self._handle_x(inner, split)
        cy = inner.center().y()
        pm = self._cache["handle_hot" if self._handle_hot() else "handle"]
        p.drawPixmap(QPointF(hx, cy) - self._half(pm), pm)
        if self._focus_ring:
            grow = 1.1 if self._handle_hot() else 1.0
            r = QRectF(hx - HANDLE_R * grow, cy - HANDLE_R * grow, 2 * HANDLE_R * grow, 2 * HANDLE_R * grow)
            p.setPen(QPen(QColor(37, 99, 235, 150), 3))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(r.adjusted(-3.5, -3.5, 3.5, 3.5))

    @staticmethod
    def _half(pm):
        """Half an image's size in painter units, as a point."""
        ratio = pm.devicePixelRatioF() or 1.0
        return QPointF(pm.width() / ratio / 2.0, pm.height() / ratio / 2.0)

    def _slide(self, p, inner):
        """The shared slide (slide units). Its own words and the column chart keep to the left, clear of
        the card; the middle and right are a quiet roadmap the card floats over on your side."""
        ix, iy = inner.left(), inner.top()
        p.setPen(theme.qcolor("ink"))
        p.setFont(theme.font(15, 700, display=True))
        p.drawText(QRectF(ix + 18, iy + 34, 92, 18), Qt.AlignVCenter | Qt.AlignLeft, "Q4 roadmap")
        p.setPen(Qt.NoPen)
        y = iy + 34 + 18 + 9
        for frac, col in ((0.86, "#bfdbfe"), (0.64, "#e2e8f0"), (0.78, "#e2e8f0"), (0.52, "#e2e8f0")):
            p.setBrush(QColor(col))
            p.drawRoundedRect(QRectF(ix + 18, y, 88 * frac, 6), 3, 3)
            y += 6 + 9
        # the column chart, bottom left
        left, bottom = ix + 18, inner.bottom() - 20
        width = 4 + 4 * 14 + 3 * 7 + 4
        p.setBrush(QColor("#cbd5e1"))
        p.drawRect(QRectF(left, bottom - 2, width, 2))
        x = left + 4
        for h, col in ((26, "#cbd5e1"), (40, "#93c5fd"), (56, "#60a5fa"), (74, "#2563eb")):
            path = QPainterPath()
            r = QRectF(x, bottom - 2 - h, 14, h)
            path.addRoundedRect(r, 3, 3)
            path.addRect(QRectF(x, bottom - 2 - 3, 14, 3))
            p.setBrush(QColor(col))
            p.drawPath(path.simplified())
            x += 14 + 7
        # the quiet part: a roadmap in pale bars (a label, then the weeks it runs), three columns
        gx, gy, gw = ix + 134, iy + 46, 226
        p.setBrush(QColor("#f1f5f9"))
        for i in range(1, 3):
            p.drawRect(QRectF(gx + gw * i / 3.0 - 0.5, gy - 8, 1, 132))
        rows = ((0.00, 0.46, "#dbeafe"), (0.20, 0.78, "#bfdbfe"), (0.38, 0.62, "#e2e8f0"),
                (0.12, 0.40, "#e2e8f0"), (0.54, 0.94, "#ddd6fe"))
        for i, (a, b, col) in enumerate(rows):
            ry = gy + i * 26
            p.setBrush(QColor("#e2e8f0"))
            p.drawRoundedRect(QRectF(gx, ry, 26, 5), 2.5, 2.5)
            p.setBrush(QColor(col))
            p.drawRoundedRect(QRectF(gx + 34 + a * (gw - 34), ry - 4, (b - a) * (gw - 34), 13), 6.5, 6.5)

    def _banner(self, p, inner, text, dot, right):
        f = theme.font(10, 600)
        # At most its own half of the glass (the other banner sits on the other half).
        text = QFontMetricsF(f).elidedText(text, Qt.ElideRight,
                                           inner.width() / 2.0 - CARD_MARGIN - 16 - (11 if dot else 0))
        tw = QFontMetricsF(f).horizontalAdvance(text)
        w = 8 + (6 + 5 if dot else 0) + tw + 8
        x0 = inner.right() - CARD_MARGIN - w if right else inner.left() + CARD_MARGIN
        r = QRectF(x0, inner.top() + 6, w, 18)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 204))
        p.drawRoundedRect(r, 9, 9)
        x = r.left() + 8
        if dot:
            p.setBrush(QColor("#ef4444"))
            p.drawEllipse(QRectF(x, r.center().y() - 3, 6, 6))
            x += 6 + 5
        p.setPen(QColor("#ffffff"))
        p.setFont(f)
        p.drawText(QRectF(x, r.top(), tw + 2, r.height()), Qt.AlignVCenter | Qt.AlignLeft, text)

    def card_word(self):
        """The Hidden chip's word on the card (the page's one word for the status)."""
        return STATUS_WORDS.get(getattr(self._status, "key", "on"), STATUS_WORD_DEFAULT)

    def card_rect(self):
        """Where the card floats on the glass, in design units of the picture: the centre-right, level
        with the middle, clear of the slide's title and chart (which keep to the left)."""
        size = live_demo.card_size(live_demo.CARD_SCALE, self.card_word())
        inner = self._inner_rect()
        x = inner.right() - CARD_GAP_RIGHT - size.width()
        y = inner.center().y() - size.height() / 2.0 + CARD_DROP
        return QRectF(x, y, size.width(), size.height())

    def _backdrop(self, px_scale):
        """What the glass has behind it, cut to the card: the slide, as an image the card blurs."""
        from PySide6.QtGui import QImage
        rect = self.card_rect()
        inner = self._inner_rect()
        w, h = max(1, int(round(rect.width() * px_scale))), max(1, int(round(rect.height() * px_scale)))
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(QColor("#ffffff"))
        q = QPainter(img)
        q.setRenderHint(QPainter.Antialiasing, True)
        q.scale(px_scale, px_scale)
        q.translate(inner.left() - rect.left(), inner.top() - rect.top())      # the glass's own origin
        q.scale(K, K)
        self._slide(q, QRectF(0, 0, INNER_W, INNER_H))
        q.end()
        return img

    def _mini_card(self, p, px_scale):
        """The Live Assistant card on the glass (ui/pages/live_demo.py: the real card in miniature),
        with its soft shadow. ``p`` is scaled by K (the slide's units); ``px_scale`` is device
        pixels per design unit."""
        st = self._status
        rect = self.card_rect()
        p.save()
        p.scale(1.0 / K, 1.0 / K)               # the card is drawn in design units, not slide units
        p.translate(self._inner_rect().topLeft() * -1.0)   # the scene's origin is the glass's corner
        _soft_shadow(p, rect, live_demo.RADIUS * live_demo.CARD_SCALE, 10, 24, "rgba(2,6,23,0.30)")
        live_demo.paint_card(p, rect.left(), rect.top(), self.card_word(), getattr(st, "tone", "ok"),
                             getattr(st, "icon", "incognito"), live_demo.CARD_SCALE, self._backdrop(px_scale),
                             self._card_look)
        p.restore()


class HeroActions(QWidget):
    """Open Live Assistant, then "or" + the keycaps; the keycaps drop to a
    second line when the column is too narrow for one. (The PRO pill is not
    here: it sits beside the page title.)"""

    def __init__(self, button, or_label, keys, parent=None):
        super().__init__(parent)
        self._box = QBoxLayout(QBoxLayout.LeftToRight, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(10)
        first = QHBoxLayout()
        first.setSpacing(10)
        first.addWidget(button, 0, Qt.AlignVCenter)
        second = QHBoxLayout()
        second.setSpacing(10)
        second.addSpacing(6)
        second.addWidget(or_label, 0, Qt.AlignVCenter)
        second.addSpacing(6)
        second.addWidget(keys, 0, Qt.AlignVCenter)
        self._box.addLayout(first)
        self._box.addLayout(second)
        self._box.addStretch(1)
        self._box.setAlignment(first, Qt.AlignLeft | Qt.AlignVCenter)
        self._box.setAlignment(second, Qt.AlignLeft | Qt.AlignVCenter)
        # Let the column squeeze this below one line's width: resizeEvent then wraps.
        self._box.setSizeConstraint(QLayout.SetNoConstraint)
        self._first, self._second = first, second
        self._parts = (button, or_label, keys)

    def _first_line_width(self):
        return self._parts[0].sizeHint().width()

    def _one_line_width(self):
        """Everything on one line: button 10 | 6 or 6 (10 apart) keys."""
        _b, or_label, keys = self._parts
        second = 6 + or_label.sizeHint().width() + 6 + 10 + keys.sizeHint().width()
        return self._first_line_width() + 10 + second + 10

    def minimumSizeHint(self):
        return QSize(min(self._first_line_width(), 240), super().minimumSizeHint().height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def event(self, e):
        if e.type() == QEvent.LayoutRequest:          # a part changed size (new keycaps)
            self._fit()
        return super().event(e)

    def _fit(self):
        want = (QBoxLayout.LeftToRight if self.width() >= self._one_line_width()
                else QBoxLayout.TopToBottom)
        if self._box.direction() != want:
            self._box.setDirection(want)
            self.updateGeometry()

    def one_line(self):
        return self._box.direction() == QBoxLayout.LeftToRight


# ── rail pieces ─────────────────────────────────────────────────────────────
def _as_rail_link(button):
    """The mockup's small rail link (12.5 px, 24 px tall: a text link, not a 30 px button)."""
    button.setFont(theme.font(12.5, 600))
    button.setFixedHeight(24)
    return button


# The theme's 30 px right padding is room for its arrow; the arrow sits in the
# 28 px drop-down already, so the value gets the mockup's room (padding 0 10 0 12).
_COMBO_QSS = "QComboBox { padding: 0px 6px 0px 12px; }"


class ShortcutControl(QWidget):
    """The shared shortcut control (ui/components/hotkey_capture: keycaps +
    Change, "Press keys…" while capturing, Esc cancels) with a one-line
    problem under it. ``keys`` / ``change`` / ``prompt`` / ``none_label`` are
    the capture's own parts."""

    def __init__(self, hotkey="", parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)
        # Keyboard only: the Live Assistant hotkey can't be a mouse button
        # (main._setup_assist_hotkey registers keys).
        self.capture = HotkeyCaptureButton(hotkey, parent=self, accessible_name="Live Assistant shortcut",
                                           allow_mouse=False)
        self.capture.layout().setSpacing(8)             # the mockup's gap before Change
        _as_rail_link(self.capture.button)
        outer.addWidget(self.capture, 0, Qt.AlignRight)
        self.keys = self.capture.keys
        self.change = self.capture.button
        self.prompt = self.capture.prompt_label
        self.none_label = self.capture.empty_label
        self.error_label = label("", "error", self, wrap=True)
        self.error_label.setFont(theme.font(12))
        self.error_label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.error_label.hide()
        outer.addWidget(self.error_label)
        self.capture.capturing_changed.connect(self._on_capturing)
        self.capture.refused.connect(self.set_error)
        self.capture.cancelled.connect(lambda: self.set_error(""))     # Esc: nothing changed, nothing to say
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._sync_name()

    def set_hotkey(self, hotkey):
        self.capture.set_hotkey(hotkey or "")
        self._sync_name()

    def hotkey(self):
        return self.capture.hotkey()

    def is_capturing(self):
        return self.capture.is_capturing()

    def set_error(self, text):
        """A short problem under the keys ("" hides it)."""
        text = text or ""
        self.error_label.setFixedWidth(max(170, self.capture.sizeHint().width()))
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))
        self.updateGeometry()

    def error(self):
        return self.error_label.text() if self.error_label.isVisibleTo(self) else ""

    def _on_capturing(self, on):
        if on:
            self.set_error("")

    def _sync_name(self):
        self.setAccessibleName("Shortcut " + (" + ".join(keycap_labels(self.capture.hotkey())) or "not set"))

class WriterOption(QFrame):
    """One "Answers written by" choice: radio, icon, name (+ PRO), the real
    model under it, and an optional link on the right. Clicking anywhere on it
    picks it."""

    _QSS = """
    QFrame#writerOption { background: #ffffff; border: 1px solid %(line2)s; border-radius: 8px; }
    QRadioButton { spacing: 0px; }
    QRadioButton::indicator { width: 14px; height: 14px; border-radius: 8px; border: 1px solid %(faint)s; background: #ffffff; }
    QRadioButton::indicator:hover { border-color: %(blue_l)s; }
    QRadioButton::indicator:checked {
        border: 1px solid %(blue)s;
        background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
                                    stop:0 #ffffff, stop:0.32 #ffffff, stop:0.40 %(blue)s, stop:1 %(blue)s);
    }
    QRadioButton[focusVisible="true"]::indicator { border: 2px solid %(blue_l)s; }
    """

    def __init__(self, icon, name, model, pro=False, aside=None, parent=None):
        super().__init__(parent)
        self.setObjectName("writerOption")
        self.setProperty("checked", False)
        self.setStyleSheet(self._QSS % {"line2": theme.qss_color("line2"), "blue": theme.qss_color("blue"),
                                        "faint": theme.qss_color("faint"), "blue_l": theme.qss_color("blue-l")})
        self.setFixedHeight(50)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 8, 0)
        lay.setSpacing(6)           # "Qwen 1.5B · not downloaded" + Change must fit the 316 px rail whole
        self.radio = QRadioButton(self)
        self.radio.setFocusPolicy(Qt.StrongFocus)
        theme.focus_ring(self.radio)
        lay.addWidget(self.radio, 0, Qt.AlignVCenter)
        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(16, 16)
        lay.addWidget(self.icon_label, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        nrow = QHBoxLayout()
        nrow.setContentsMargins(0, 0, 0, 0)
        nrow.setSpacing(6)
        self.name_label = QLabel(name, self)
        self.name_label.setFont(theme.font(13, 600))
        self.name_label.setProperty("textRole", "label")
        self.name_label.setFixedHeight(17)                  # line-height 17px
        nrow.addWidget(self.name_label, 0, Qt.AlignVCenter)
        self.pro_tag = ProTag("s", parent=self) if pro else None
        if self.pro_tag is not None:
            nrow.addWidget(self.pro_tag, 0, Qt.AlignVCenter)
            self.radio.setAccessibleDescription("Pro")     # what the PRO tag says, for screen readers
        nrow.addStretch(1)
        col.addStretch(1)
        col.addLayout(nrow)
        self.model_label = label(model, "model", self)
        self.model_label.setFixedHeight(16)                 # line-height 16px
        col.addWidget(self.model_label)
        col.addStretch(1)
        lay.addLayout(col, 1)
        self.aside = aside
        if aside is not None:
            aside.setParent(self)
            lay.addWidget(aside, 0, Qt.AlignVCenter)
        self.set_choice(icon, name, model)

    def set_choice(self, icon, name, model):
        self.icon_label.setPixmap(icons.pixmap(icon, 16, "muted"))
        self.name_label.setText(name)
        self.model_label.setText(model)
        self.radio.setAccessibleName("%s · %s" % (name, model) if model else name)
        self.setAccessibleName(self.radio.accessibleName())

    def set_checked(self, on):
        self.radio.setChecked(bool(on))
        theme.set_prop(self, "checked", bool(on), children=False)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.radio.click()
            e.accept()
            return
        super().mousePressEvent(e)


class ElidedLabel(QLabel):
    """One line that ends in "…" when it doesn't fit (the full text is the tooltip)."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.set_full_text(text)

    def set_full_text(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self.setAccessibleName(self._full)
        self._elide()

    def full_text(self):
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        w = max(10, self.width())
        super().setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight, w))

    def minimumSizeHint(self):
        return QSize(20, super().minimumSizeHint().height())

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, super().sizeHint().height())


class SessionRow(QFrame):
    """One past session: day + time, the first question (or the title), the
    length and answers, and an Open button."""

    opened = Signal(str)

    def __init__(self, sess, parent=None):
        super().__init__(parent)
        self.sess = dict(sess)
        self.setObjectName("laSession")
        self.setStyleSheet("QFrame#laSession { background: transparent; border: none; border-top: 1px solid %s; }"
                           % theme.qss_color("line"))
        self.setFixedHeight(50)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        day, time_ = day_and_time(self.sess.get("started"))
        dcol = QVBoxLayout()
        dcol.setContentsMargins(0, 0, 0, 0)
        dcol.setSpacing(0)
        self.day_label = QLabel(day, self)
        self.day_label.setFont(theme.font(13, 600))
        self.day_label.setProperty("textRole", "h3")
        self.time_label = label(time_, "hint", self)
        dcol.addWidget(self.day_label)
        dcol.addWidget(self.time_label)
        dbox = QWidget(self)
        dbox.setLayout(dcol)
        dbox.setFixedWidth(76)
        lay.addWidget(dbox, 0, Qt.AlignVCenter)
        self.text_label = ElidedLabel(self.sess.get("text") or "", self)
        self.text_label.setFont(theme.font(13.5))
        self.text_label.setProperty("textRole", "label")
        lay.addWidget(self.text_label, 1, Qt.AlignVCenter)
        self.meta_label = label(session_meta(self.sess), "muted", self)
        lay.addWidget(self.meta_label, 0, Qt.AlignVCenter)
        self.open_button = IconButton("chev_r", "Open session", size=32, icon_size=17, color="muted", parent=self)
        self.open_button.clicked.connect(self._open)
        lay.addWidget(self.open_button, 0, Qt.AlignVCenter)
        name = ", ".join(x for x in (day, time_, self.sess.get("text") or "", session_meta(self.sess)) if x)
        self.setAccessibleName(name)
        # The button is what Tab reaches: it reads the whole row, not just "Open session".
        self.open_button.setAccessibleName("Open session: " + name if name else "Open session")
        self.open_button.setToolTip("Open session")

    def _open(self):
        self.opened.emit(self.sess.get("dir") or "")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _fit(self):
        """A narrow column keeps the question readable: the length and answer
        count step aside (they stay in the row's accessible name)."""
        meta = self.meta_label.text()
        if not meta:
            return
        self.meta_label.ensurePolished()            # the theme's font, not the default one
        meta_w = self.meta_label.fontMetrics().horizontalAdvance(meta) + 4
        room = self.width() - 76 - 32 - 3 * 12 - meta_w
        self.meta_label.setVisible(room >= 150)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self._open()
            e.accept()
            return
        super().mouseReleaseEvent(e)


def _h2(text, parent, px=16):
    lb = label(text, "h2", parent)
    lb.setFont(theme.font(px, 600, display=True))
    return lb


# ── the page ────────────────────────────────────────────────────────────────
class LiveAssistantPage(QWidget):
    """See the module docstring. Hooks: on_open, on_setting_changed, refresh_account."""

    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.setProperty("gusan", "page")
        self._card = None
        self._sessions = None               # None until the first load lands
        self._load_gen = 0
        self._loading = False
        self._context_dirty = False
        self._writer = {}
        self._status = None
        self._more_open = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.status_chip = HideChip()
        # Beside the title for every plan: the PRO pill, then the one-word status.
        self.pro_tag = ProTag("m")
        self.pro_tag.setAccessibleName("Pro feature")
        trail = QWidget()
        trail_lay = QHBoxLayout(trail)
        trail_lay.setContentsMargins(0, 0, 0, 0)
        trail_lay.setSpacing(8)
        trail_lay.addWidget(self.pro_tag, 0, Qt.AlignVCenter)
        trail_lay.addWidget(self.status_chip, 0, Qt.AlignVCenter)
        self.header = PageHeader(TITLE, SUB, title_trail=trail, parent=self)
        self.header.setAccessibleName("Live Assistant, Pro feature")
        root.addWidget(self.header)

        body = QHBoxLayout()
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(24)
        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(16)
        main.addWidget(self._build_hero())
        main.addSpacing(-12)        # the picture's own 18 px of shadow room is part of the gap below it
        main.addWidget(self._build_about())
        main.addWidget(self._build_documents())
        main.addWidget(self._build_sessions())
        main.addStretch(1)
        body.addLayout(main, 1)
        self.rail = self._build_rail()
        body.addWidget(self.rail, 0, Qt.AlignTop)
        root.addLayout(body, 1)

        self._ctx_timer = QTimer(self)
        self._ctx_timer.setSingleShot(True)
        self._ctx_timer.setInterval(CONTEXT_SAVE_MS)
        self._ctx_timer.timeout.connect(self._save_context)

        self._refresh_all()

    # ── building ──
    def _build_hero(self):
        hero = QWidget(self)
        hero.setAccessibleName("Live Assistant")
        v = QVBoxLayout(hero)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(0)                     # 22 between the parts (set below)
        self.open_button = HeroButton("Open Live Assistant", hero)
        self.open_button.clicked.connect(self._on_open_clicked)
        self.or_label = QLabel("or", hero)
        self.or_label.setFont(theme.font(14))
        self.or_label.setProperty("textRole", "muted")
        self.hero_keys = MidKeycaps("", hero)
        self.actions = HeroActions(self.open_button, self.or_label, self.hero_keys, hero)
        v.addWidget(self.actions)
        v.addSpacing(22)
        self.picture = ScreensPicture(hero)
        v.addWidget(self.picture)
        return hero

    def _build_about(self):
        card = Card(self, gap=10)
        card.layout().setContentsMargins(20, 16, 20, 16)
        card.setAccessibleName("About my calls")
        head = QHBoxLayout()
        head.setSpacing(4)
        head.addWidget(_h2("About my calls", card))
        head.addStretch(1)
        self.saved_icon = QLabel(card)
        self.saved_icon.setPixmap(icons.pixmap("check", 14, "ok-tx", sw=2.2))
        self.saved_label = label("Saved", "muted", card)
        head.addWidget(self.saved_icon, 0, Qt.AlignVCenter)
        head.addWidget(self.saved_label, 0, Qt.AlignVCenter)
        card.add(head)
        self.context_edit = QPlainTextEdit(card)
        self.context_edit.setAccessibleName("About my calls")
        self.context_edit.setPlaceholderText("Who you are and what your calls are about.")
        self.context_edit.setFont(theme.font(13.5))
        self.context_edit.setFixedHeight(CONTEXT_MIN_H)
        self.context_edit.setTabChangesFocus(True)
        self.context_edit.document().setDocumentMargin(3)
        self.context_edit.textChanged.connect(self._on_context_edited)
        self.context_edit.installEventFilter(self)
        # Re-wrapped (typing, a new value, a narrower window): fit the height,
        # after the layout pass that reported it.
        self.context_edit.document().documentLayout().documentSizeChanged.connect(
            lambda _s: QTimer.singleShot(0, self, self._fit_context))
        card.add(self.context_edit)
        return card

    def _build_documents(self):
        """The files the assistant may read (ui/pages/live_documents.py). The page takes
        files dropped anywhere on it (see dragEnterEvent); a file dropped on the About box
        would only have pasted its path, so that box hands drops over too."""
        self.documents = DocumentsSection(self.app, self)
        self.setAcceptDrops(True)
        self.context_edit.setAcceptDrops(False)       # a file dropped here is a document, not a path
        return self.documents

    def _build_sessions(self):
        card = Card(self, gap=0)
        card.setAccessibleName("Past sessions")
        self.sessions_card = card
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(10)
        head.addWidget(_h2("Past sessions", card))
        head.addStretch(1)
        self.see_all = Button("See all", "link", "s", parent=card)
        self.see_all.setFixedHeight(22)         # a text link as tall as the title (the mockup's 28 px head)
        self.see_all.setAccessibleName("See all sessions")
        self.see_all.clicked.connect(self._see_all)
        head.addWidget(self.see_all, 0, Qt.AlignVCenter)
        self._sessions_head = QWidget(card)
        self._sessions_head.setLayout(head)
        card.add(self._sessions_head)
        self.sessions_box = QWidget(card)
        self.sessions_lay = QVBoxLayout(self.sessions_box)
        self.sessions_lay.setContentsMargins(0, 0, 0, 0)
        self.sessions_lay.setSpacing(0)
        card.add(self.sessions_box)
        self.empty_row = QWidget(card)
        er = QHBoxLayout(self.empty_row)
        er.setContentsMargins(0, 0, 0, 0)
        er.setSpacing(10)
        ic = QLabel(self.empty_row)
        ic.setPixmap(icons.pixmap("incognito", 22, "faint"))
        er.addWidget(ic, 0, Qt.AlignVCenter)
        self.empty_label = QLabel("No sessions yet.", self.empty_row)
        self.empty_label.setFont(theme.font(13.5))
        self.empty_label.setProperty("textRole", "muted")
        er.addWidget(self.empty_label, 0, Qt.AlignVCenter)
        er.addStretch(1)
        card.add(self.empty_row)
        self.session_rows = []
        self._show_sessions_state("loading")
        return card

    def _build_rail(self):
        rail = SettingsRail("Live Assistant settings")
        rail.setParent(self)
        cfg = self._cfg_dict()

        self.hide_toggle = Toggle("Hide from screen share", bool(cfg.get("live_assist_private", True)))
        self.hide_toggle.toggled.connect(self._on_hide_toggled)
        self.hide_row = rail.add_row(SettingRow("Hide from screen share", self.hide_toggle))
        self.hide_icon = QLabel(self.hide_row)
        self.hide_icon.setFixedSize(17, 17)
        try:
            lrow = self.hide_row.layout().itemAt(0).layout().itemAt(0).layout()
            lrow.insertWidget(0, self.hide_icon, 0, Qt.AlignVCenter)
        except Exception:
            logger.debug("hide row icon not placed", exc_info=True)

        self.shortcut = ShortcutControl(cfg.get("live_assist_hotkey", ""))
        self.shortcut.capture.captured.connect(self._on_hotkey_captured)
        # While the user presses keys the global hotkeys must not fire (Wave 4:
        # AppController.suspend_hotkeys, as the Dictation page uses).
        self.shortcut.capture.capturing_changed.connect(lambda on: self._call("suspend_hotkeys", bool(on)))
        self.shortcut_row = rail.add_row(SettingRow("Shortcut", self.shortcut))

        self.auto_toggle = Toggle("Answer questions by itself", bool(cfg.get("live_assist_auto_answer", True)))
        self.auto_toggle.toggled.connect(lambda on: self._apply_bool("live_assist_auto_answer", on, self.auto_toggle))
        rail.add_row(SettingRow("Answer questions by itself", self.auto_toggle))

        self.screen_toggle = Toggle("Let it see my screen", bool(cfg.get("live_assist_screen_auto", True)))
        self.screen_toggle.toggled.connect(lambda on: self._apply_bool("live_assist_screen_auto", on, self.screen_toggle))
        rail.add_row(SettingRow("Let it see my screen", self.screen_toggle))

        self.call_lang = self._combo("Call language", CALL_LANGS)
        self.call_lang.activated.connect(lambda _i: self._apply_combo("live_assist_language", self.call_lang))
        rail.add_row(SettingRow("Call language", self.call_lang))
        self.answer_lang = self._combo("Answer in", ANSWER_LANGS)
        self.answer_lang.activated.connect(lambda _i: self._apply_combo("live_assist_output_language", self.answer_lang))
        rail.add_row(SettingRow("Answer in", self.answer_lang))

        writers = QWidget()
        wl = QVBoxLayout(writers)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(6)
        self.cloud_option = WriterOption("cloud", "Gusan Cloud", CLOUD_LIVE_MODEL, pro=True, parent=writers)
        self.local_change = _as_rail_link(Button("Change", "link", "s"))
        self.local_change.setAccessibleName("Change the AI writer")
        self.local_change.clicked.connect(self._open_speech_settings)
        self.local_option = WriterOption("laptop", "This computer", LOCAL_DEFAULT_MODEL, aside=self.local_change,
                                         parent=writers)
        self.writer_group = QButtonGroup(self)
        self.writer_group.setExclusive(True)
        self.writer_group.addButton(self.cloud_option.radio, 0)
        self.writer_group.addButton(self.local_option.radio, 1)
        self.writer_group.idClicked.connect(self._on_writer_clicked)
        wl.addWidget(self.cloud_option)
        wl.addWidget(self.local_option)
        self.writer_hint = label("", "hint", writers, wrap=True)
        self.writer_hint.hide()
        wl.addWidget(self.writer_hint)
        writers.setAccessibleName("Answers written by")
        self.writer_row = rail.add_row(SettingRow("Answers written by", writers, stacked=True))

        theme_name = cfg.get("live_assist_theme", "light")
        self.look = Segmented(["Light", "Dark"], 1 if theme_name == "dark" else 0, "Card look", size="s")
        self.look.changed.connect(self._on_look_changed)
        self.picture.set_card_look(theme_name)          # the picture's card wears the same look
        self.look_row = rail.add_row(SettingRow("Card look", self.look))

        # More options (collapsed): Card see-through, Reset card position, Your own key.
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(0, 45)                 # % see-through (opacity 1.0 .. 0.55)
        self.opacity_slider.setFixedWidth(120)
        self.opacity_slider.setAccessibleName("Card see-through")
        self.opacity_slider.setFocusPolicy(Qt.StrongFocus)
        self.opacity_slider.valueChanged.connect(self._on_opacity_moved)
        self._opacity_timer = QTimer(self)
        self._opacity_timer.setSingleShot(True)
        self._opacity_timer.setInterval(250)
        self._opacity_timer.timeout.connect(self._save_opacity)
        self.opacity_row = rail.add_row(SettingRow("Card see-through", self.opacity_slider))
        self.reset_button = Button("Reset", "secondary", "s")
        self.reset_button.setAccessibleName("Reset card position")
        self.reset_button.clicked.connect(self._reset_position)
        self.reset_row = rail.add_row(SettingRow("Reset card position", self.reset_button))
        self.own_key_button = _as_rail_link(Button("Set up", "link", "s"))
        self.own_key_button.setAccessibleName("Set up your own key")
        self.own_key_button.clicked.connect(self._open_speech_settings)
        self.own_key_row = rail.add_row(SettingRow("Your own key", self.own_key_button))
        self._more_rows = [self.opacity_row, self.reset_row, self.own_key_row]

        chev = QLabel()
        chev.setPixmap(icons.pixmap("chev_r", 14, "blue-tx", sw=2))
        chev.setFixedSize(14, 14)
        self._more_chev = chev
        self.more_button = Button("More options", "link", "s", trail=chev)
        self.more_button.setFont(theme.font(13, 600))
        self.more_button.setFixedWidth(self.more_button.fontMetrics().horizontalAdvance("More options") + 4 + 4 + 14)
        self.more_button.layout().setContentsMargins(0, 0, 0, 0)
        self.more_button.setStyleSheet("QPushButton { padding: 0px; }")
        self.more_button.setFixedHeight(24)
        self.more_button.setAccessibleName("More options")
        self.more_button.clicked.connect(self._toggle_more)
        rail.set_foot(self.more_button)
        self._set_more(False)
        return rail

    def _combo(self, name, options):
        cb = QComboBox()
        cb.setAccessibleName(name)
        cb.setFont(theme.font(13.5))
        cb.setFocusPolicy(Qt.StrongFocus)
        for value, text in options:
            cb.addItem(text, value)
        cb.setStyleSheet(_COMBO_QSS)
        cb.setFixedHeight(34)
        return cb

    def minimumSizeHint(self):
        # The shell's scroll area sizes the page by heightForWidth; the layout's
        # minimum height (word-wrapped lines at their narrowest) would add a
        # needless scroll bar to a page that fits.
        s = super().minimumSizeHint()
        if self.layout() is not None and self.layout().hasHeightForWidth():
            return QSize(s.width(), 0)
        return s

    # ── shell hooks ──
    def on_open(self, **opts):
        self._attach_card()
        self._refresh_all()
        self.reload_sessions()
        self.documents.reload()
        if opts.get("focus") in ("hotkey", "shortcut"):
            # Settings › General's shortcut list links here.
            self.shortcut.capture.button.setFocus(Qt.TabFocusReason)
        elif opts.get("focus") == "documents":
            self.documents.focus_add()

    def on_setting_changed(self, key):
        self._sync_key(key)

    def refresh_account(self):
        self.picture.set_owner(_first_name(self.app))
        self._refresh_writer()

    def showEvent(self, e):
        super().showEvent(e)
        # After the shell's on_open (which loads them) - one disk scan, not two.
        QTimer.singleShot(0, self, self._load_if_needed)

    def _load_if_needed(self):
        if self._sessions is None and not self._loading:
            self.reload_sessions()

    def hideEvent(self, e):
        if self._context_dirty:
            self._save_context()
        self.shortcut.capture.cancel_capture()
        self.documents.drag_leave()
        super().hideEvent(e)

    # ── files dropped on the page become documents ──
    def dragEnterEvent(self, e):
        if self.documents.drag_enter(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if live_docs_ui.wants_files(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self.documents.drag_leave()
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        if self.documents.drop(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def eventFilter(self, obj, ev):
        if obj is self.context_edit:
            t = ev.type()
            if t == QEvent.FocusOut and self._context_dirty:
                self._save_context()
            elif t == QEvent.Resize and ev.oldSize().width() != ev.size().width():
                # A new width re-wraps the text in the editor's own resizeEvent
                # (after this filter) and doesn't always announce a new size.
                QTimer.singleShot(0, self, self._fit_context)
        return super().eventFilter(obj, ev)

    # ── state ──
    def _cfg_dict(self):
        cfg = getattr(self.app, "cfg", None)
        return cfg if isinstance(cfg, dict) else {}

    def _set(self, key, value):
        """app.set_setting(key, value) -> ok. Without it (an old controller): write cfg + save."""
        fn = getattr(self.app, "set_setting", None)
        if callable(fn):
            try:
                return bool(fn(key, value))
            except Exception:
                logger.warning("set_setting(%s) failed", key, exc_info=True)
                return False
        cfg = getattr(self.app, "cfg", None)
        if isinstance(cfg, dict):
            cfg[key] = value
            for name in ("schedule_config_save", "save_config"):
                save = getattr(self.app, name, None)
                if callable(save):
                    try:
                        save()
                    except Exception:
                        logger.debug("%s failed", name, exc_info=True)
                    break
            return True
        return False

    def _call(self, method, *args, **kwargs):
        fn = getattr(self.app, method, None)
        if not callable(fn):
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.warning("%s() failed", method, exc_info=True)
            return None

    def _refresh_all(self):
        cfg = self._cfg_dict()
        self._wanted_private = bool(cfg.get("live_assist_private", True))
        self.picture.set_owner(_first_name(self.app))
        for key in ("live_assist_private", "live_assist_hotkey", "live_assist_auto_answer",
                    "live_assist_screen_auto", "live_assist_language", "live_assist_output_language",
                    "live_assist_theme", "live_assist_opacity", "live_assist_context"):
            self._sync_key(key, refresh_writer=False)
        self._refresh_writer()

    _MISSING = object()

    def _sync_key(self, key, value=_MISSING, refresh_writer=True):
        """Show the current value of ``key`` (from cfg, or ``value`` when the
        card announced it before writing cfg). Never writes."""
        cfg = self._cfg_dict()

        def val(default):
            return cfg.get(key, default) if value is self._MISSING else value

        if key == "live_assist_private":
            on = bool(val(True))
            self._wanted_private = on
            self._set_checked(self.hide_toggle, on)
            self._refresh_hide()
        elif key == "live_assist_hotkey":
            hk = val("") or ""
            self.shortcut.set_hotkey(hk)
            self.hero_keys.set_hotkey(hk)
            has = bool(self.hero_keys.keys)
            self.hero_keys.setVisible(has)
            self.or_label.setVisible(has)
        elif key == "live_assist_auto_answer":
            self._set_checked(self.auto_toggle, bool(val(True)))
        elif key == "live_assist_screen_auto":
            self._set_checked(self.screen_toggle, bool(val(True)))
        elif key == "live_assist_language":
            self._set_combo(self.call_lang, val("en"))
        elif key == "live_assist_output_language":
            self._set_combo(self.answer_lang, val("en"))
        elif key == "live_assist_theme":
            self.look.set_index(1 if val("light") == "dark" else 0)
            self.picture.set_card_look(val("light"))
        elif key == "live_assist_opacity":
            try:
                op = min(1.0, max(0.55, float(val(0.96))))
            except (TypeError, ValueError):
                op = 0.96
            self.opacity_slider.blockSignals(True)
            self.opacity_slider.setValue(int(round((1.0 - op) * 100)))
            self.opacity_slider.blockSignals(False)
        elif key == "live_assist_context":
            if not self._context_dirty:             # never over what is being typed
                text = str(val("") or "")
                # Compared as saved (trimmed, clipped): a trailing space just
                # typed is not "different" and the cursor stays where it is.
                if live_context.clip_context(text) != live_context.clip_context(self.context_edit.toPlainText()):
                    self.context_edit.blockSignals(True)
                    self.context_edit.setPlainText(text)
                    self.context_edit.blockSignals(False)
            self._show_saved()
        elif refresh_writer and not key.startswith("live_assist_") or key == "live_assist_writer":
            # privacy_mode, action_model, keys, plan...: who writes the answers may have changed.
            self._refresh_writer()

    @staticmethod
    def _set_checked(toggle, on):
        if toggle.isChecked() != on:
            toggle.blockSignals(True)
            toggle.setChecked(on)
            toggle.blockSignals(False)
            toggle.update()

    @staticmethod
    def _set_combo(cb, value):
        value = "" if value is None else str(value)
        i = cb.findData(value)
        if i < 0:
            name = live_context._LANG_NAMES.get(value, value)
            cb.addItem(name, value)
            i = cb.count() - 1
        cb.blockSignals(True)
        cb.setCurrentIndex(i)
        cb.blockSignals(False)
        # As wide as the value needs (min 150, like the mockup's select), from the
        # style itself so the stylesheet's padding and drop-down are counted.
        tw = int(math.ceil(QFontMetricsF(cb.font()).horizontalAdvance(cb.currentText()))) + 2
        try:
            from PySide6.QtWidgets import QStyle, QStyleOptionComboBox
            opt = QStyleOptionComboBox()
            cb.initStyleOption(opt)
            w = cb.style().sizeFromContents(QStyle.CT_ComboBox, opt, QSize(tw, 20), cb).width()
        except Exception:
            w = tw + 52
        cb.setFixedWidth(max(150, min(230, w)))

    # ── hidden status ──
    def hide_status(self):
        """The truthful HideStatus: the open card's own when it is showing,
        else what opening it will do (live_context.hide_status)."""
        card = self._card if self._card is not None else getattr(self.app, "live_assist", None)
        if card is not None and _alive(card):
            try:
                if card.isVisible():
                    st = card.hide_state()
                    if st is not None:
                        return st
            except Exception:
                logger.debug("card.hide_state failed", exc_info=True)
        try:
            return live_context.hide_status(self._wanted_private)
        except Exception:
            logger.debug("hide_status failed", exc_info=True)
            return _unverified_status(self._wanted_private)

    def _refresh_hide(self):
        st = self.hide_status()
        self._status = st
        self.status_chip.set_state(STATUS_WORDS.get(st.key, STATUS_WORD_DEFAULT), st.tone, st.icon,
                                   st.headline, (st.tip or "").strip())
        self.picture.set_status(st)
        on = self._wanted_private
        self.hide_icon.setPixmap(icons.pixmap("incognito", 17, "ok-tx", sw=1.9) if on
                                 else icons.pixmap("eye_off", 17, "muted", sw=1.7))

    # ── the card ──
    def _attach_card(self):
        card = getattr(self.app, "live_assist", None)
        if card is self._card:
            return
        self._detach_card()
        if card is None or not _alive(card):
            return
        self._card = card
        for name, slot in (("settings_changed", self._on_card_setting),
                           ("hide_status_changed", self._on_card_hide)):
            sig = getattr(card, name, None)
            if sig is not None:
                try:
                    sig.connect(slot)
                except Exception:
                    logger.debug("card.%s not connected", name, exc_info=True)

    def _detach_card(self):
        card, self._card = self._card, None
        if card is None or not _alive(card):
            return
        for name, slot in (("settings_changed", self._on_card_setting),
                           ("hide_status_changed", self._on_card_hide)):
            sig = getattr(card, name, None)
            if sig is not None:
                try:
                    sig.disconnect(slot)
                except Exception:
                    pass

    def _on_card_setting(self, key, value):
        self._sync_key(str(key), value)

    def _on_card_hide(self, _key=None):
        self._refresh_hide()

    # ── actions ──
    def _upsell(self):
        self._call("_pro_upsell", UPSELL)

    def _on_open_clicked(self):
        """Opens the card for everyone - Free and guests too. The card asks for
        Pro when Start is pressed, not here."""
        if callable(getattr(self.app, "show_live_assist", None)):
            self._call("show_live_assist")
        else:
            card = getattr(self.app, "live_assist", None)
            if card is not None and _alive(card) and callable(getattr(card, "open_card", None)):
                try:
                    card.open_card()
                except Exception:
                    logger.warning("open_card failed", exc_info=True)
            else:
                self._call("toggle_live_assist")
        self._attach_card()
        self._refresh_hide()
        # The card confirms its hiding once its window exists (after show()).
        QTimer.singleShot(400, self, self._refresh_hide)

    def _on_hide_toggled(self, on):
        if self._set("live_assist_private", bool(on)):
            self._wanted_private = bool(on)
        else:
            self._set_checked(self.hide_toggle, self._wanted_private)
        self._refresh_hide()

    def _apply_bool(self, key, on, toggle):
        if not self._set(key, bool(on)):
            self._set_checked(toggle, bool(self._cfg_dict().get(key, True)))

    def _apply_combo(self, key, cb):
        value = cb.currentData()
        if not self._set(key, "" if value is None else value):
            self._sync_key(key)
        else:
            self._set_combo(cb, value)

    def _on_look_changed(self, idx):
        name = "dark" if idx == 1 else "light"
        if self._set("live_assist_theme", name):
            self.picture.set_card_look(name)
        else:
            self._sync_key("live_assist_theme")

    def _on_hotkey_captured(self, combo):
        if self._set("live_assist_hotkey", combo):
            self.shortcut.set_error("")
            self._sync_key("live_assist_hotkey", combo)
            return
        self._sync_key("live_assist_hotkey")        # back to what is really set
        shown = " + ".join(keycap_labels(combo)) or combo
        cfg = self._cfg_dict()
        smart = cfg.get("smart_actions_hotkey", "alt+s")      # main.DEFAULT when the key is missing
        if _norm_hotkey(combo) and _norm_hotkey(combo) == _norm_hotkey(cfg.get("hotkey")):
            self.shortcut.set_error(SHORTCUT_IS_DICTATION % shown)
        elif _norm_hotkey(combo) and _norm_hotkey(combo) == _norm_hotkey(smart):
            self.shortcut.set_error(SHORTCUT_IS_SMART % shown)
        else:
            self.shortcut.set_error(SHORTCUT_REFUSED % shown)

    def _on_opacity_moved(self, _v):
        self._opacity_timer.start()

    def _save_opacity(self):
        op = round(1.0 - self.opacity_slider.value() / 100.0, 2)
        if not self._set("live_assist_opacity", op):
            self._sync_key("live_assist_opacity")

    def _reset_position(self):
        self._set("live_assist_pos", None)

    def _toggle_more(self):
        self._set_more(not self._more_open)

    def _set_more(self, open_):
        self._more_open = bool(open_)
        for r in self._more_rows:
            r.setVisible(self._more_open)
        self.look_row.set_last(not self._more_open)
        self._more_rows[-1].set_last(True)
        self._more_chev.setPixmap(icons.pixmap("chev_d" if self._more_open else "chev_r", 14, "blue-tx", sw=2))
        self.more_button.setAccessibleDescription("Expanded" if self._more_open else "Collapsed")

    def _open_speech_settings(self):
        if callable(getattr(self.app, "show_main", None)):
            self._call("show_main", "settings", section="speech")
        else:
            self._call("show_settings")

    # ── About my calls ──
    def _on_context_edited(self):
        self._context_dirty = True
        self._show_saved()
        self._ctx_timer.start()
        self._fit_context()

    def _fit_context(self, *_):
        """Two lines tall like the mockup; grows to five as the text needs
        (a narrow window wraps it more) instead of hiding it behind a scroll bar."""
        ed = self.context_edit
        if not _alive(ed):
            return
        try:
            # QPlainTextEdit's layout measures its height in (wrapped) lines.
            need = int(math.ceil(ed.document().documentLayout().documentSize().height()))
        except Exception:
            need = 2
        lines = max(2, min(CONTEXT_MAX_LINES, need))
        # No scroll bar until the box is at its tallest: one appearing would
        # narrow the text, re-wrap it and make the box jump.
        ed.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if need > CONTEXT_MAX_LINES
                                      else Qt.ScrollBarAlwaysOff)
        h = CONTEXT_MIN_H + (lines - 2) * max(1, ed.fontMetrics().lineSpacing())
        if ed.height() != h:
            ed.setFixedHeight(h)

    def _save_context(self):
        self._ctx_timer.stop()
        text = live_context.clip_context(self.context_edit.toPlainText())
        if self._set("live_assist_context", text):
            self._context_dirty = False
        self._show_saved()

    def _show_saved(self):
        shown = not self._context_dirty and bool(self.context_edit.toPlainText().strip())
        self.saved_icon.setVisible(shown)
        self.saved_label.setVisible(shown)

    # ── Answers written by ──
    def _refresh_writer(self):
        view = writer_view(self.app)
        self._writer = view
        self.cloud_option.set_choice("cloud", "Gusan Cloud", view["cloud_model"])
        self.local_option.set_choice(view["local_icon"], view["local_name"], view["local_model"])
        cloud = view["selected"] == "cloud"
        self.cloud_option.set_checked(cloud)
        self.local_option.set_checked(not cloud)
        if self.writer_hint.text() and not self._writer_hint_holds(self.writer_hint.text()):
            self._set_writer_hint("")       # the plan, Privacy mode or the engine changed
        # The Documents footer says where the words go: this computer, or the cloud AI.
        self.documents.set_route(view.get("kind") or ("cloud" if cloud else "local"))

    def _writer_hint_holds(self, hint):
        cfg = self._cfg_dict()
        selected = self._writer.get("selected")
        if hint == PRO_USES_CLOUD:
            return selected == "cloud" and _is_pro(self.app) and cfg.get("live_assist_writer") == "local"
        if hint == PRIVACY_KEEPS_LOCAL:
            return selected != "cloud" and bool(cfg.get("privacy_mode"))
        return False

    def _set_writer_hint(self, hint):
        self.writer_hint.setText(hint or "")
        self.writer_hint.setVisible(bool(hint))

    def _on_writer_clicked(self, idx):
        want = "cloud" if idx == 0 else "local"
        before = self._writer.get("selected")
        stored = self._cfg_dict().get("live_assist_writer") or ""
        hint = ""
        if want == "cloud" and not _is_pro(self.app):
            self._refresh_writer()                  # back to the truth before the Pro window
            self._upsell()
        elif want == "cloud" and self._cfg_dict().get("privacy_mode"):
            if want != before:
                hint = PRIVACY_KEEPS_LOCAL
        elif want != before or (stored and stored != want):
            # Also when the shown choice already matches but the saved one
            # doesn't (Pro picked This computer, then Gusan Cloud again).
            self._set("live_assist_writer", want)
        self._set_writer_hint("")
        self._refresh_writer()
        if not hint and want == "local" and self._writer.get("selected") == "cloud":
            hint = PRO_USES_CLOUD                   # until live_engine honours live_assist_writer
        self._set_writer_hint(hint)

    # ── Past sessions ──
    def reload_sessions(self):
        """Read the sessions on a worker; the list lands on the GUI thread."""
        self._load_gen += 1
        gen = self._load_gen
        self._loading = True
        try:
            import api_keys
            api_keys.run_then(load_sessions, then=lambda res, g=gen: self._sessions_loaded(g, res),
                              error=lambda exc, g=gen: self._sessions_loaded(g, [], exc),
                              name="gusan-live-sessions")
        except Exception:
            logger.warning("Past sessions couldn't load", exc_info=True)
            self._sessions_loaded(gen, [])

    def _sessions_loaded(self, gen, sessions, exc=None):
        if not _alive(self) or gen != self._load_gen:
            return
        if exc is not None:
            logger.warning("Past sessions couldn't load: %s", exc)
        self._loading = False
        self.set_sessions(sessions or [])

    def set_sessions(self, sessions):
        """Show these sessions (dicts from load_sessions), newest first."""
        self._sessions = list(sessions or [])
        for row in self.session_rows:
            row.hide()
            row.deleteLater()
        self.session_rows = []
        prev = self.see_all
        for sess in self._sessions[:MAX_SESSIONS]:
            row = SessionRow(sess, self.sessions_box)
            row.opened.connect(self._open_session)
            self.sessions_lay.addWidget(row)
            self.session_rows.append(row)
            # Rows are made after the rail: put them right after See all in
            # the Tab order (page order), not after the last rail control.
            QWidget.setTabOrder(prev, row.open_button)
            prev = row.open_button
        self._show_sessions_state("list" if self._sessions else "empty")

    def _show_sessions_state(self, state):
        lay = self.sessions_card.layout()
        if state == "list":
            lay.setContentsMargins(20, 16, 20, 6)
            lay.setSpacing(0)
            self._sessions_head.layout().setContentsMargins(0, 0, 0, 6)
        else:
            lay.setContentsMargins(20, 16, 20, 16)
            lay.setSpacing(10)
            self._sessions_head.layout().setContentsMargins(0, 0, 0, 0)
        self.see_all.setVisible(state == "list")
        self.sessions_box.setVisible(state == "list")
        self.empty_row.setVisible(state == "empty")

    def _open_session(self, folder):
        if callable(getattr(self.app, "show_main", None)):
            self._call("show_main", "meetings", meeting=folder)
        else:
            self._call("show_meeting")

    def _see_all(self):
        if callable(getattr(self.app, "show_main", None)):
            self._call("show_main", "meetings")
        else:
            self._call("show_meeting")
