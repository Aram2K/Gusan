"""Interpreter page (Gusan 2.2): AI multilingual voice clone - talk in your language, they hear
theirs, in your own voice.

    page = create(main_app)          # ui/shell.py builds it the first time the route opens

Spec: docs/redesign/interpreter-spec.md. The PRO pill sits beside the title only (calm Pro: no
upsell cards); a Free person or a guest who presses a Pro action (Try it, Clone my voice, Play)
gets the Pro window right then - never a greyed-out row or a dead click.

One page, no tabs and no Start button (the interpreter is turned on where calls happen: the
Meetings start card, the Live Assistant, the live card's chip and the shortcut):

* The set-up card: "I speak" / "They speak" pickers with a swap button; under each language what
  is said in it (row 1: what you say, which they hear in your voice; row 2: what they say, which
  you hear), so each column reads as one language; the two switches ("They hear me in <their
  language>", "I hear them in <my language>") - a switch greys out its own row - and "Try it"
  (records one sentence, shows it translated and plays it back in your voice when you have one
  and it is on: ui/interpreter_audio.try_it; how it went is said under the card, on this page).
* Under it, your voice, one card at a time: Clone your voice (From my meetings, scanned on a
  worker once the server says there is no voice / Record now; the consent box; a progress bar
  with the time left while it is made; "Keep my voice" while one exists), Record now (3 texts;
  each one: Record, Stop, then Play / Record again / Next - nothing starts or moves on by itself;
  the last text ends with the consent words), and ready (hear yourself in any language: a
  dropdown and Play; use my voice when I speak; make it again; delete my voice - a person whose
  Pro ended still sees it and can delete it).
* The rail "Interpreter settings": I hear it on, turn the call down, keep their voice underneath,
  captions, voice for them, the virtual microphone (found on a worker thread), tell people an AI
  interpreter is on (shown once the engine says it: notice_spoken), the shortcut, and a footer
  with the real model names and this month's time.

The engine is the app's one InterpreterHost (ui/interpreter_host.py, app.interpreter). Every
setting goes through app.set_setting (instant apply); nothing slow runs on the GUI thread.
"""

import datetime
import logging
import sys
import math
import os
import threading
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QFontMetrics, QIcon, QLinearGradient, QPainter, QPen,
                           QPixmap, QPolygonF)
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QProgressBar, QPushButton, QRadioButton, QSizePolicy,
                               QVBoxLayout, QWidget)

import api_keys
from interpreter import audio_io, voice_builder
from interpreter import voices as voice_presets
from interpreter.session import MESSAGES as SERVER_MESSAGES
from ui import interpreter_audio, theme
from ui import interpreter_host as ih
from ui.components import (Button, Card, Divider, IconTile, PageHeader, ProTag, Segmented, SettingRow,
                           SettingsRail, Toggle, icons, label, mini_prompt)
from ui.components.controls import keycap_labels
from ui.pages.live_assistant import HeroButton, ShortcutControl

logger = logging.getLogger("transcribe")

TITLE = "Interpreter"
SUB = "AI multilingual voice clone. They hear you in their language, in your voice."
UPSELL = ih.UPSELL
HOTKEY_KEY = "interpreter_hotkey"

START_TEXT = {ih.IDLE: "Start interpreter", ih.STARTING: "Starting…", ih.RUNNING: "Stop interpreter",
              ih.STOPPING: "Stopping…"}
MODELS = "Runs on Gusan Cloud · Scribe v2 Realtime · DeepSeek V4.1 Flash · ElevenLabs v4 Turbo"
DEFAULT_ALLOWANCE_S = 5 * 3600
CARD_W = 440                                # your voice's card: its width until it is laid out
RAIL_W = 336                                # the mockup's rail (the longest row fits on one line)
VOICE_MODES = (("Match the speaker", voice_presets.MATCH), ("One voice for everyone", voice_presets.FIXED))
VOICE_MATCH_HINT = "A woman's or a man's voice, picked once when they start talking."
OUTPUTS = (("auto", "Automatic"), ("headphones", "Headphones"), ("speakers", "Speakers"))
OUTPUT_HINT = "Speakers work too: Gusan keeps its voice out of your mic."
if sys.platform == "darwin":
    CABLE_URL = "https://existential.audio/blackhole/"
    CABLE_HINT = "Install BlackHole 2ch, then pick it as the microphone in Zoom, Teams or Meet."
else:
    CABLE_URL = "https://vb-audio.com/Cable/"
    CABLE_HINT = "Then pick CABLE Output as the microphone in Zoom, Teams or Meet."
THEM = "Anna"                                # the other person in the picture
TRY_PHASES = {"listening": "Listening…", "translating": "Translating…", "speaking": "Playing…"}
TRY_FAILED = "Couldn't try it just now. Check your connection and try again."
TRY_NOTHING = "Didn't catch that. Try it again."
# What Try it does: it plays the words back only in your own voice (the preview needs it).
TRY_HEAR = "Say one sentence, hear it back in %s."
TRY_SEE = "Say one sentence, see it in %s."
TRY_SEE_CLONE = "Say one sentence, see it in %s. Clone your voice to hear it."
SHORTCUT_REFUSED = "Couldn't use %s."

# My voice
CONSENT_TEXT = "This is my own voice. Gusan may copy it to speak my translations."
REQUIRED_RED = "#dc2626"
# The consent box: a little smaller than the app's 18 px boxes (its words are 12 px).
CONSENT_BOX_QSS = (
    "QCheckBox { spacing: 0px; background: transparent; padding-top: 1px; }"
    " QCheckBox::indicator { width: 15px; height: 15px; border: 1.5px solid %s; border-radius: 4px;"
    " background: #ffffff; }"
    " QCheckBox::indicator:hover { border-color: %s; }"
    " QCheckBox::indicator:checked { border-color: %s; background: %s; image: url(\"%s\"); }")
# "Create my voice" while the box is not ticked: plainly off (grey), not the blue-violet gradient.
HERO_DISABLED_QSS = "\nQPushButton:disabled { background: #e2e8f0; color: #94a3b8; }\n"
HOW_USED_TITLE = "How it's used"
HOW_USED = ("Kept at ElevenLabs, tied to your account.\n"
            "Used only when the interpreter speaks for you.\n"
            "Delete it any time on this page.")
KEPT_LINE = "Kept at ElevenLabs. Delete it any time."
KEEP_VOICE = "Keep my voice"
LAST_STEP = "Last text: then say the words on screen, to show it's really you."
SHORT_TAKE = "That was short. Read it once more."
MIN_TAKE_S = 5.0                 # ElevenLabs refuses samples under 4.6 s
TAKE_MAX_S = 60.0                # one text (~20 s read aloud) at a slow pace, with the consent words
# The recordings themselves were refused: they are read again (any other failure keeps the three
# takes, so Clone my voice just sends them again).
RECORDINGS_REFUSED = ("voice_too_short", "voice_samples_rejected", "consent_required")
CLONE_TITLE = "Clone your voice"
CLONE_SUB = "Others hear your translations in your own voice."
CLONE_BUTTON = "Clone my voice"
MAKING = "Cloning your voice…"
SENDING = "Sending %s of you…"
CLONING = "Making your voice at ElevenLabs…"
ALMOST = "Almost done…"
PRESS_TO_RECORD = "Press to record"
STEP_HINT = "Read it at your normal pace."
SPEAK_INK, SPEAK_TINT = "#6366f1", "#eef2ff"     # "your voice": row 1, its arrow and its switch's tile
HEAR_INK, HEAR_TINT = "#ec4899", "#fdf2f8"       # "in your ear": row 2
DELETE_TITLE = "Delete your voice?"
DELETE_TEXT = "It's deleted at ElevenLabs and here. You can make it again any time."
VOICE_FAILED = "Couldn't reach your voice. Try again in a minute."

# The picture's two lines in each language (row 1: you ask, row 2: they answer).
LINES = {
    "en": ("Can we start at three?", "That works for me."),
    "ja": ("3時に始められますか？", "大丈夫です。"),
    "fr": ("On peut commencer à trois heures ?", "Ça me va."),
    "de": ("Können wir um drei anfangen?", "Das passt mir."),
    "es": ("¿Empezamos a las tres?", "Me parece bien."),
    "it": ("Possiamo iniziare alle tre?", "Per me va bene."),
    "pt": ("Podemos começar às três?", "Por mim, tudo bem."),
    "ru": ("Можем начать в три?", "Мне подходит."),
    "uk": ("Можемо почати о третій?", "Мені підходить."),
    "hy": ("Կարո՞ղ ենք սկսել ժամը երեքին։", "Ինձ հարմար է։"),
    "zh": ("我们三点开始可以吗？", "可以。"),
    "ko": ("세 시에 시작할까요?", "좋아요."),
    "ar": ("هل يمكننا البدء الساعة الثالثة؟", "هذا يناسبني."),
    "tr": ("Saat üçte başlayabilir miyiz?", "Bana uyar."),
    "pl": ("Możemy zacząć o trzeciej?", "Pasuje mi."),
    "nl": ("Kunnen we om drie uur beginnen?", "Dat komt me goed uit."),
    "hi": ("क्या हम तीन बजे शुरू कर सकते हैं?", "मेरे लिए ठीक है।"),
    "bn": ("আমরা কি তিনটায় শুরু করতে পারি?", "আমার জন্য ঠিক আছে।"),
    "bg": ("Може ли да започнем в три?", "Това ме устройва."),
    "hr": ("Možemo li početi u tri?", "To mi odgovara."),
    "cs": ("Můžeme začít ve tři?", "To mi vyhovuje."),
    "da": ("Kan vi starte klokken tre?", "Det passer mig fint."),
    "fi": ("Voimmeko aloittaa kolmelta?", "Se sopii minulle."),
    "ka": ("შეგვიძლია სამზე დავიწყოთ?", "ჩემთვის მისაღებია."),
    "el": ("Μπορούμε να ξεκινήσουμε στις τρεις;", "Μου ταιριάζει."),
    "he": ("אפשר להתחיל בשלוש?", "זה מתאים לי."),
    "hu": ("Kezdhetünk háromkor?", "Nekem megfelel."),
    "id": ("Bisa kita mulai jam tiga?", "Itu cocok untuk saya."),
    "kk": ("Сағат үште бастай аламыз ба?", "Маған ыңғайлы."),
    "lv": ("Vai varam sākt trijos?", "Man tas der."),
    "lt": ("Ar galime pradėti trečią valandą?", "Man tinka."),
    "ms": ("Boleh kita mula pukul tiga?", "Itu sesuai untuk saya."),
    "no": ("Kan vi begynne klokka tre?", "Det passer fint for meg."),
    "fa": ("می‌توانیم ساعت سه شروع کنیم؟", "برای من مناسب است."),
    "ro": ("Putem începe la ora trei?", "Mie îmi convine."),
    "sr": ("Можемо ли да почнемо у три?", "То ми одговара."),
    "sk": ("Môžeme začať o tretej?", "Mne to vyhovuje."),
    "sw": ("Tunaweza kuanza saa tisa?", "Hiyo inanifaa."),
    "sv": ("Kan vi börja klockan tre?", "Det passar mig."),
    "tl": ("Puwede ba tayong magsimula nang alas-tres?", "Ayos lang sa akin."),
    "ta": ("நாம் மூன்று மணிக்குத் தொடங்கலாமா?", "எனக்குச் சரி."),
    "th": ("เราเริ่มตอนบ่ายสามโมงได้ไหม", "ได้เลย สะดวกดี"),
    "ur": ("کیا ہم تین بجے شروع کر سکتے ہیں؟", "میرے لیے ٹھیک ہے۔"),
    "vi": ("Mình bắt đầu lúc ba giờ được không?", "Với tôi thì được."),
}
# "Hear yourself in": one short sentence per language.
SAMPLES = {
    "en": "Hi, this is my voice in English.",
    "ja": "こんにちは。これは日本語の私の声です。",
    "fr": "Bonjour, voici ma voix en français.",
    "hy": "Բարև, սա իմ ձայնն է հայերենով։",
    "es": "Hola, esta es mi voz en español.",
    "de": "Hallo, das ist meine Stimme auf Deutsch.",
    "ru": "Привет, это мой голос на русском.",
    "it": "Ciao, questa è la mia voce in italiano.",
    "zh": "你好，这是我的中文声音。",
    "ko": "안녕하세요, 이것은 한국어로 된 제 목소리입니다.",
    "ar": "مرحبًا، هذا صوتي باللغة العربية.",
    "bn": "হ্যালো, এটা বাংলায় আমার কণ্ঠস্বর।",
    "bg": "Здравейте, това е моят глас на български.",
    "hr": "Bok, ovo je moj glas na hrvatskom.",
    "cs": "Ahoj, tohle je můj hlas v češtině.",
    "da": "Hej, det her er min stemme på dansk.",
    "nl": "Hallo, dit is mijn stem in het Nederlands.",
    "fi": "Hei, tämä on minun ääneni suomeksi.",
    "ka": "გამარჯობა, ეს ჩემი ხმაა ქართულად.",
    "el": "Γεια σας, αυτή είναι η φωνή μου στα ελληνικά.",
    "he": "שלום, זה הקול שלי בעברית.",
    "hi": "नमस्ते, यह हिंदी में मेरी आवाज़ है।",
    "hu": "Szia, ez az én hangom magyarul.",
    "id": "Halo, ini suara saya dalam bahasa Indonesia.",
    "kk": "Сәлем, бұл менің қазақша дауысым.",
    "lv": "Sveiki, šī ir mana balss latviešu valodā.",
    "lt": "Labas, tai mano balsas lietuvių kalba.",
    "ms": "Hai, ini suara saya dalam bahasa Melayu.",
    "no": "Hei, dette er stemmen min på norsk.",
    "fa": "سلام، این صدای من به فارسی است.",
    "pl": "Cześć, to jest mój głos po polsku.",
    "pt": "Olá, esta é a minha voz em português.",
    "ro": "Bună, aceasta este vocea mea în română.",
    "sr": "Здраво, ово је мој глас на српском.",
    "sk": "Ahoj, toto je môj hlas v slovenčine.",
    "sw": "Habari, hii ni sauti yangu kwa Kiswahili.",
    "sv": "Hej, det här är min röst på svenska.",
    "tl": "Kumusta, ito ang boses ko sa Tagalog.",
    "ta": "வணக்கம், இது தமிழில் என் குரல்.",
    "th": "สวัสดี นี่คือเสียงของฉันในภาษาไทย",
    "tr": "Merhaba, bu benim Türkçe sesim.",
    "uk": "Привіт, це мій голос українською.",
    "ur": "ہیلو، یہ اردو میں میری آواز ہے۔",
    "vi": "Xin chào, đây là giọng nói của tôi bằng tiếng Việt.",
}
_PREVIEW_ORDER = ("ja", "fr", "hy", "es", "de", "en")
SAMPLE_EN = "Hi, this is my voice in %s."      # translated once for a language SAMPLES lacks
_SAMPLE_CACHE = {}                              # code -> that translation (the same words every time)


def spinner_icon(step, color="#2563eb", size=12):
    """A small turning arc for a button that is loading (``step`` moves it on)."""
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color), 1.8 * dpr)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    m = 1.6 * dpr
    p.drawArc(QRectF(m, m, size * dpr - 2 * m, size * dpr - 2 * m), int(-step * 40 * 16), 270 * 16)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return QIcon(pm)


def create(main_app):
    return InterpreterPage(main_app)


# ── helpers ──────────────────────────────────────────────────────────────────
def host_for(app):
    """The app's InterpreterHost (made here for a controller that has none - a test stub)."""
    host = getattr(app, "interpreter", None)
    if host is None:
        host = ih.InterpreterHost(app)
        try:
            app.interpreter = host
        except Exception:
            pass
    return host


def run_bg(fn, then=None, error=None, name="interp-page"):
    """fn() on a worker thread, then(result) / error(exc) on the GUI thread (tests run it inline)."""
    return api_keys.run_then(fn, then=then, error=error, name=name)


def notice_spoken():
    """Does the engine say "an AI interpreter is on" when it starts (its Settings read
    interpreter_notice)? Until it does, the rail has no switch for it: a switch that does nothing
    would let people believe the others were told."""
    try:
        from interpreter import controller
        return hasattr(controller.Settings.from_config({"interpreter_notice": True}), "notice")
    except Exception:
        return False


def find_virtual_mic():
    """"Gusan Microphone" / "CABLE Output" (what the call app picks), or "" when none is installed.
    Lists the sound devices: call it on a worker thread."""
    pa = audio_io._pyaudio().PyAudio()
    try:
        idx = audio_io.find_virtual_mic_output(pa)
        if idx is None:
            return ""
        name = audio_io._name(pa.get_device_info_by_index(idx)).lower()
        if "blackhole" in name:
            return "BlackHole 2ch"
        return "Gusan Microphone" if "gusan" in name else "CABLE Output"
    finally:
        try:
            pa.terminate()
        except Exception:
            pass


def _lbl(text="", px=13.5, weight=400, color="text", parent=None, wrap=False, display=False):
    lb = QLabel(text, parent)
    lb.setTextFormat(Qt.PlainText)
    lb.setFont(theme.font(px, weight, display=display))
    lb.setStyleSheet("color: %s; background: transparent;" % theme.qss_color(color))
    if wrap:
        lb.setWordWrap(True)
    return lb


def _first_name(app):
    auth = getattr(app, "auth", None)
    if not getattr(auth, "is_authenticated", False):
        return ""
    name = (getattr(auth, "user_name", None) or "").strip()
    return name.split()[0] if name else ""


def _short_date(iso):
    """"7 Nov" from the server's created_at ("" when unreadable)."""
    try:
        d = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return "%d %s" % (d.day, d.strftime("%b"))


def preview_languages(my, their):
    """The three "Hear yourself in" languages: theirs first, then others (never mine)."""
    out = [their] if their != my else []
    for code in _PREVIEW_ORDER:
        if len(out) >= 3:
            break
        if code not in out and code != my:
            out.append(code)
    return out[:3]


# ── small widgets ────────────────────────────────────────────────────────────
class Face(QWidget):
    """A round avatar: a letter on a gradient (blue for you, pink for them)."""

    BLUE = ("#60a5fa", "#2563eb")
    PINK = ("#f472b6", "#db2777")

    def __init__(self, letter="A", size=30, colors=BLUE, parent=None):
        super().__init__(parent)
        self._letter = (letter or "?")[:1].upper()
        self._colors = colors
        self._size = size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_letter(self, letter):
        self._letter = (letter or "?")[:1].upper()
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0, 0, self._size, self._size)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, QColor(self._colors[0]))
        g.setColorAt(1, QColor(self._colors[1]))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(r)
        p.setPen(QColor("#ffffff"))
        p.setFont(theme.font(max(8, int(self._size * 0.42)), 700))
        p.drawText(r, Qt.AlignCenter, self._letter)
        p.end()


class LangCombo(QComboBox):
    """A language picker; its list is as wide as its widest row."""

    def __init__(self, accessible, parent=None):
        super().__init__(parent)
        self.setAccessibleName(accessible)
        self.setFont(theme.font(14, 500))
        self.setFixedHeight(40)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMaxVisibleItems(14)
        for code, name in ih.languages():
            self.addItem(name, code)

    def set_code(self, code):
        i = self.findData(code)
        if i < 0:
            self.addItem(ih.lang_name(code), code)
            i = self.count() - 1
        self.blockSignals(True)
        self.setCurrentIndex(i)
        self.blockSignals(False)

    def code(self):
        return self.currentData() or ""

    def showPopup(self):
        fm = QFontMetrics(self.font())
        widest = max((fm.horizontalAdvance(self.itemText(i)) for i in range(self.count())), default=0)
        self.view().setMinimumWidth(max(self.width(), widest + 60))
        super().showPopup()


def pair_after_pick(my, their, side, code):
    """The pair after a pick on one side ("mine" | "theirs"): picking the other side's language swaps
    the two, like the page's pickers and the live card's row."""
    if side == "mine":
        return (their, my) if code == their else (code, their)
    return (their, my) if code == my else (my, code)


def pair_updates(cfg, my, their):
    """What a pick writes: only the keys whose language changed (a swap writes both at once, so a
    running interpreter starts again only once)."""
    out = {}
    if my and my != ih.my_language(cfg):
        out["interpreter_my_language"] = my
    if their and their != ih.their_language(cfg):
        out["interpreter_their_language"] = their
    return out


class LangPairRow(QWidget):
    """"English ▾ ⇄ Armenian ▾", picked in place (the Meetings start card, the Live Assistant rail):
    two small searchable pickers (ui/components/lang_picker.py, the app look) and a swap button.
    Picking the other side's language swaps the two. Either way the row shows the new pair at once
    and emits ``languages_picked(my, their)``; the page writes what changed (pair_updates) and calls
    set_languages again when the write is refused."""

    languages_picked = Signal(str, str)          # (my code, their code)

    def __init__(self, parent=None):
        super().__init__(parent)
        from ui.components.lang_picker import LangPicker
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self._my, self._their = "en", ih.DEFAULT_THEIR_LANGUAGE
        self.my_picker = LangPicker(self._my, "I speak", "app", self)
        self.my_picker.picked.connect(lambda code: self._picked("mine", code))
        lay.addWidget(self.my_picker, 0, Qt.AlignVCenter)
        self.swap_button = QPushButton(self)
        self.swap_button.setFixedSize(28, 28)
        self.swap_button.setIcon(icons.icon("swap", 14, "muted", 1.8))
        self.swap_button.setIconSize(QSize(14, 14))
        self.swap_button.setCursor(Qt.PointingHandCursor)
        self.swap_button.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self.swap_button)
        self.swap_button.setAccessibleName("Swap the languages")
        self.swap_button.setToolTip("Swap the languages")
        self.swap_button.setStyleSheet(
            "QPushButton { background: transparent; border: none; border-radius: 14px; }"
            " QPushButton:hover { background: %s; }"
            " QPushButton[focusVisible=\"true\"] { border: 2px solid %s; }"
            % (theme.qss_color("bg2"), theme.qss_color("blue-l")))
        self.swap_button.clicked.connect(self._swap)
        lay.addWidget(self.swap_button, 0, Qt.AlignVCenter)
        self.their_picker = LangPicker(self._their, "They speak", "app", self)
        self.their_picker.picked.connect(lambda code: self._picked("theirs", code))
        lay.addWidget(self.their_picker, 0, Qt.AlignVCenter)
        lay.addStretch(1)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_languages(self, my, their):
        """The pair on the pickers (codes; no signal)."""
        self._my, self._their = str(my or ""), str(their or "")
        self.my_picker.set_code(self._my)
        self.their_picker.set_code(self._their)
        self.setToolTip("I speak %s, they speak %s" % (self.my_picker.text(), self.their_picker.text()))
        self._fit()

    def languages(self):
        return self._my, self._their

    def _full_width(self):
        lay = self.layout()
        return (self.my_picker.full_width() + self.swap_button.width() + self.their_picker.full_width()
                + 2 * lay.spacing())

    def _fit(self):
        """Both names when they fit, else both short codes ("EN ⇄ HY") - never one cut name."""
        compact = self.width() < self._full_width()          # re-checked on every resize
        for p in (self.my_picker, self.their_picker):
            p.set_compact(compact)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def showEvent(self, e):
        super().showEvent(e)
        self._fit()

    def close_popups(self):
        for p in (self.my_picker, self.their_picker):
            p.close_popup()

    def _picked(self, side, code):
        pair = pair_after_pick(self._my, self._their, side, code)
        self.set_languages(*pair)
        self.languages_picked.emit(*pair)

    def _swap(self):
        pair = (self._their, self._my)
        self.set_languages(*pair)
        self.languages_picked.emit(*pair)

    def hideEvent(self, e):
        self.close_popups()
        super().hideEvent(e)


class _Arrow(QWidget):
    """The picture's dashed arrow with its words ("your voice" above, "in your ear" below)."""

    OFF_COLOR = "#cbd5e1"

    def __init__(self, text, rightward, color, parent=None):
        super().__init__(parent)
        self._text = text
        self._right = rightward
        self._color = QColor(color)
        self._on = True
        self.setFixedSize(84, 40)

    def set_on(self, on):
        """Off: the arrow goes grey and says "off" (that direction is switched off)."""
        on = bool(on)
        if on != self._on:
            self._on = on
            self.update()

    def is_on(self):
        return self._on

    def label(self):
        return self._text if self._on else "off"

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        y = h / 2.0
        color = self._color if self._on else QColor(self.OFF_COLOR)
        pen = QPen(color, 1.4, Qt.DashLine)
        pen.setDashPattern([3, 3])
        p.setPen(pen)
        x0, x1 = 6.0, w - 6.0
        if self._right:
            p.drawLine(x0, y, x1 - 7, y)
            tip, back = x1, x1 - 8
        else:
            p.drawLine(x0 + 7, y, x1, y)
            tip, back = x0, x0 + 8
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPolygon(QPolygonF([QPointF(tip, y), QPointF(back, y - 4.5), QPointF(back, y + 4.5)]))
        p.setPen(color if self._on else QColor("#94a3b8"))
        p.setFont(theme.font(11, 500))
        r = QRectF(0, 0, w, y - 3) if self._right else QRectF(0, y + 3, w, h - y - 3)
        p.drawText(r, Qt.AlignHCenter | (Qt.AlignBottom if self._right else Qt.AlignTop), self.label())
        p.end()


class _ConsentLabel(QLabel):
    """The consent sentence: a click on its words ticks the box (the "How it's used" link opens
    its note instead)."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._on_link = False
        self.setCursor(Qt.PointingHandCursor)
        self.linkHovered.connect(lambda href: setattr(self, "_on_link", bool(href)))

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if e.button() == Qt.LeftButton and not self._on_link and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class _Bubble(QLabel):
    def __init__(self, text, tone, parent=None):
        super().__init__(text, parent)
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(True)
        self.setFont(theme.font(13.5))
        self.setMinimumHeight(40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._tone = tone
        self._on = None
        self.set_on(True)

    def set_on(self, on):
        """Off: grey, the words faded (that direction is switched off)."""
        on = bool(on)
        if on == self._on:
            return
        self._on = on
        if on:
            bg, line = (("#eff6ff", "#bfdbfe") if self._tone == "you" else ("#fdf2f8", "#fbcfe8"))
            ink = theme.qss_color("ink")
        else:
            bg, line, ink = "#f8fafc", "#e2e8f0", "#94a3b8"
        self.setStyleSheet("QLabel { background: %s; color: %s; border: 1px solid %s; border-radius: 10px;"
                           " padding: 8px 12px; }" % (bg, ink, line))

    def is_on(self):
        return self._on


def _swap_button(parent):
    b = QPushButton(parent)
    b.setFixedSize(38, 38)
    b.setIcon(icons.icon("swap", 16, "text2", 1.8))
    b.setIconSize(QSize(16, 16))
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.TabFocus)
    theme.focus_ring(b)
    b.setAccessibleName("Swap the languages")
    b.setToolTip("Swap the languages")
    b.setStyleSheet(
        "QPushButton { background: #ffffff; border: 1px solid %s; border-radius: 19px; }"
        " QPushButton:hover { border-color: %s; background: %s; }"
        " QPushButton[focusVisible=\"true\"] { border: 2px solid %s; }"
        % (theme.qss_color("line2"), theme.qss_color("blue-l"), theme.qss_color("bg"), theme.qss_color("blue-l")))
    return b


class ConversationCard(Card):
    """The one set-up card. "I speak" / "They speak" on top (a swap button between); under each
    language what is said in it - row 1: what you say, which they hear in your voice; row 2: what
    they say, which you hear - so each column is one language; then the two switches (each one
    greys out its own row) and Try it."""

    try_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, gap=0)
        self.setAccessibleName("Languages")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.you_label = _lbl("I speak", 12.5, 500, "muted", self)
        self.them_label = _lbl("They speak", 12.5, 500, "muted", self)
        grid.addWidget(self.you_label, 0, 0)
        grid.addWidget(self.them_label, 0, 2)
        self.my_combo = LangCombo("I speak", self)
        self.their_combo = LangCombo("They speak", self)
        self.swap_button = _swap_button(self)
        grid.addWidget(self.my_combo, 1, 0)
        grid.addWidget(self.swap_button, 1, 1, Qt.AlignCenter)
        grid.addWidget(self.their_combo, 1, 2)
        grid.setRowMinimumHeight(2, 4)
        self.mine = [_Bubble("", "you", self), _Bubble("", "you", self)]
        self.theirs = [_Bubble("", "them", self), _Bubble("", "them", self)]
        self.speak_arrow = _Arrow("your voice", True, SPEAK_INK, self)
        self.hear_arrow = _Arrow("in your ear", False, HEAR_INK, self)
        grid.addWidget(self.mine[0], 3, 0)
        grid.addWidget(self.speak_arrow, 3, 1, Qt.AlignCenter)
        grid.addWidget(self.theirs[0], 3, 2)
        grid.addWidget(self.mine[1], 4, 0)
        grid.addWidget(self.hear_arrow, 4, 1, Qt.AlignCenter)
        grid.addWidget(self.theirs[1], 4, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        self.add(grid)
        self.add(self._spacer(12))
        self.add(Divider(self))
        self.speak_row = _DirectionRow("mic", "They hear me in Armenian", self, tint=(SPEAK_INK, SPEAK_TINT))
        self.add(self.speak_row)
        self.add(Divider(self))
        self.hear_row = _DirectionRow("headphones", "I hear them in English", self, tint=(HEAR_INK, HEAR_TINT))
        self.hear_row.sub_face.hide()
        self.add(self.hear_row)
        self.add(Divider(self))
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 12, 0, 0)
        foot.setSpacing(10)
        self.hint = _lbl("", 12.5, 400, "muted", self, wrap=True)
        self._their, self._voice, self._cloned, self._note = "", False, False, ""
        foot.addWidget(self.hint, 1)
        self.try_button = Button("Try it", "secondary", "m", icon="play", parent=self)
        self.try_button.setIcon(icons.icon("play", 12, "blue-tx"))
        self.try_button.setIconSize(QSize(12, 12))
        self.try_button.setAccessibleName("Try it")
        self.try_button.clicked.connect(self.try_clicked)
        foot.addWidget(self.try_button, 0, Qt.AlignVCenter)
        self.add(foot)

    @staticmethod
    def _spacer(h):
        w = QWidget()
        w.setFixedHeight(h)
        return w

    def set_languages(self, my, their, owner=""):
        mine, theirs = LINES.get(my, LINES["en"]), LINES.get(their, LINES["en"])
        for i in range(2):
            self.mine[i].setText(mine[i])
            self.theirs[i].setText(theirs[i])
        self._their, self._note = their, ""
        self._show_hint()

    def set_voice(self, mine, cloned=True):
        """Try it plays the words back only in your own voice (``mine``: it exists and is on);
        else it shows them (``cloned`` False: no voice yet, so the line says how to hear it)."""
        self._voice, self._cloned = bool(mine), bool(cloned)
        self._show_hint()

    def set_try_note(self, text=""):
        """Try it's outcome under the card ("Didn't catch that..."), on this page only; "" brings
        back the line that says what Try it does."""
        self._note = text or ""
        self._show_hint()

    def _show_hint(self):
        lang = ih.lang_name(self._their)
        line = TRY_HEAR if self._voice else (TRY_SEE if self._cloned else TRY_SEE_CLONE)
        self.hint.setText(self._note or line % lang)

    def set_directions(self, speak, hear):
        """Row 1 (they hear me) and row 2 (I hear them) follow the two switches: off = grey."""
        for row, on, arrow in ((0, speak, self.speak_arrow), (1, hear, self.hear_arrow)):
            self.mine[row].set_on(on)
            self.theirs[row].set_on(on)
            arrow.set_on(on)

    def show_try(self, said, heard):
        """Try it's result in the first row: what you said, what they would hear."""
        if said:
            self.mine[0].setText(said)
        if heard:
            self.theirs[0].setText(heard)


class _DirectionRow(QWidget):
    """One direction: an icon tile, the words (and a small line under them), the switch."""

    def __init__(self, icon, title, parent=None, tint=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 10, 0, 10)
        lay.setSpacing(14)
        ink, bg = tint or ("muted", "bg2")
        self.tile = IconTile(icon, color=ink, bg=bg, size=34, icon_size=17, radius=10, parent=self)
        lay.addWidget(self.tile, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(3)
        self.title = _lbl(title, 14, 600, "ink", self)
        col.addWidget(self.title)
        self.sub_row = QHBoxLayout()
        self.sub_row.setSpacing(6)
        self.sub_face = Face("Y", 16, Face.BLUE, self)
        self.sub_row.addWidget(self.sub_face, 0, Qt.AlignVCenter)
        self.sub = _lbl("", 12, 400, "muted", self)
        self.sub_row.addWidget(self.sub, 0, Qt.AlignVCenter)
        self.sub_row.addStretch(1)
        col.addLayout(self.sub_row)
        lay.addLayout(col, 1)
        self.toggle = Toggle(title, True, self)
        lay.addWidget(self.toggle, 0, Qt.AlignVCenter)

    def set_title(self, text):
        self.title.setText(text)
        self.toggle.setAccessibleName(text)


class OptionCard(QFrame):
    """A radio choice drawn as a card ("From my meetings" / "Record now"): click anywhere picks it."""

    clicked = Signal()

    def __init__(self, title, sub="", parent=None):
        super().__init__(parent)
        self.setObjectName("voiceOption")
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("selected", False)
        self.setStyleSheet(
            "QFrame#voiceOption { background: #ffffff; border: 1px solid %(line)s; border-radius: 12px; }"
            " QFrame#voiceOption[selected=\"true\"] { background: #f5f8ff; border: 1px solid %(blue)s; }"
            " QRadioButton { spacing: 0px; }"
            " QRadioButton::indicator { width: 16px; height: 16px; border-radius: 9px; border: 1px solid %(faint)s;"
            " background: #ffffff; }"
            " QRadioButton::indicator:checked { border: 1px solid %(dot)s; background: qradialgradient(cx:0.5,"
            " cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 %(dot)s, stop:0.45 %(dot)s, stop:0.56 #ffffff,"
            " stop:1 #ffffff); }"
            % {"line": theme.qss_color("line"), "blue": theme.qss_color("rgba(37,99,235,0.55)"),
               "faint": theme.qss_color("faint"), "dot": theme.qss_color("blue")})
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)
        self.radio = QRadioButton(self)
        self.radio.setAccessibleName(title)
        self.radio.setFocusPolicy(Qt.TabFocus)
        lay.addWidget(self.radio, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        self.title = _lbl(title, 13.5, 600, "ink", self)
        col.addWidget(self.title)
        self.sub = _lbl(sub, 12, 400, "muted", self)
        col.addWidget(self.sub)
        self.chip_row = QHBoxLayout()
        self.chip_row.setContentsMargins(0, 5, 0, 0)
        self.chip = QLabel("", self)
        self.chip.setFont(theme.font(12, 600))
        self.chip.setFixedHeight(22)
        self.chip.hide()
        self.chip_row.addWidget(self.chip)
        self.chip_row.addStretch(1)
        col.addLayout(self.chip_row)
        col.addStretch(1)                           # side by side, the two cards are one height: text on top
        lay.addLayout(col, 1)
        self.radio.toggled.connect(self._sync)

    def set_chip(self, text, tone="ok"):
        if not text:
            self.chip.hide()
            return
        colors = {"ok": ("#dcfce7", "#15803d", "#86efac"), "amber": ("#fef3c7", "#92400e", "#fde68a")}
        bg, fg, line = colors.get(tone, colors["ok"])
        self.chip.setText(text)
        self.chip.setStyleSheet("QLabel { background: %s; color: %s; border: 1px solid %s; border-radius: 11px;"
                                " padding: 0px 9px; }" % (bg, fg, line))
        self.chip.show()

    def _sync(self, on):
        theme.set_prop(self, "selected", bool(on), children=False)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.radio.setChecked(True)
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class LevelWave(QWidget):
    """The microphone's loudness as a row of bars (blue to violet) while Record now listens."""

    BARS = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self._levels = [0.08] * self.BARS
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def push(self, level):
        self._levels = self._levels[1:] + [max(0.06, min(1.0, float(level)))]
        self.update()

    def reset(self):
        self._levels = [0.08] * self.BARS
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pitch, bar = 6.0, 3.0
        total = self.BARS * pitch
        x0 = (self.width() - total) / 2.0
        h = self.height()
        g = QLinearGradient(x0, 0, x0 + total, 0)
        g.setColorAt(0, QColor("#3b82f6"))
        g.setColorAt(1, QColor("#8b5cf6"))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        for i, lv in enumerate(self._levels):
            bh = max(4.0, lv * (h - 4))
            p.drawRoundedRect(QRectF(x0 + i * pitch, (h - bh) / 2.0, bar, bh), 1.5, 1.5)
        p.end()


class RecordButton(QAbstractButton):
    """Record now's round button (a soft red ring): a red dot to start recording, a red square to
    stop. Nothing records until it is pressed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(72, 72)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self._mode = "record"
        self.set_mode("record")

    def set_mode(self, mode):
        self._mode = "stop" if mode == "stop" else "record"
        name = "Stop recording" if self._mode == "stop" else "Start recording"
        self.setAccessibleName(name)
        self.setToolTip(name)
        self.update()

    def mode(self):
        return self._mode

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(1, 1, self.width() - 2, self.height() - 2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(239, 68, 68, 40 if self.underMouse() else 28))
        p.drawEllipse(r)
        inner = r.adjusted(9, 9, -9, -9)
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(QColor(239, 68, 68, 70), 1))
        p.drawEllipse(inner)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#dc2626"))
        c = inner.center()
        if self._mode == "stop":
            p.drawRoundedRect(QRectF(c.x() - 9, c.y() - 9, 18, 18), 3, 3)
        else:
            p.drawEllipse(QRectF(c.x() - 11, c.y() - 11, 22, 22))
        if self.hasFocus():
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(theme.qcolor("blue-l"), 2))
            p.drawEllipse(r.adjusted(1, 1, -1, -1))
        p.end()


StopButton = RecordButton


def clone_estimate_s(clips, audio_s):
    """About how long making a voice takes: sending the clips (base64 WAV over the network) and
    ElevenLabs' work on them (noise removal grows with the audio). Logged against the real time."""
    sending = audio_s * 32000 * 1.34 / 1.5e6
    return max(6.0, 3.0 + sending + 0.08 * audio_s + 0.3 * clips)


class StepBars(QWidget):
    """Three short bars: which of the three texts is being read."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._step = 0
        self.setFixedSize(78, 8)

    def set_step(self, step):
        self._step = step
        self.setAccessibleName("Step %d of 3" % (step + 1))
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        for i in range(3):
            p.setBrush(theme.qcolor("blue") if i <= self._step else theme.qcolor("line"))
            p.drawRoundedRect(QRectF(i * 28, 2, 22, 4), 2, 2)
        p.end()


class _MicTile(QWidget):
    """The round violet microphone of My voice (a green tick once the voice is ready)."""

    def __init__(self, ready=False, parent=None):
        super().__init__(parent)
        self._ready = ready
        self.setFixedSize(52, 52)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ede9fe"))
        p.drawEllipse(QRectF(0, 0, 52, 52))
        p.drawPixmap(15, 15, icons.pixmap("mic", 22, "#6d28d9", 1.9))
        if self._ready:
            p.setBrush(QColor("#ffffff"))
            p.drawEllipse(QRectF(34, 34, 18, 18))
            p.setBrush(QColor("#22c55e"))
            p.drawEllipse(QRectF(36, 36, 14, 14))
            p.drawPixmap(38, 38, icons.pixmap("check", 10, "#ffffff", 2.6))
        p.end()


class PreviewChip(QPushButton):
    """"▶ Japanese": plays your voice saying one sentence in that language."""

    def __init__(self, code, parent=None):
        super().__init__(ih.lang_name(code), parent)
        self.code = code
        self.setFont(theme.font(13.5, 600))
        self.setFixedHeight(32)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.setAccessibleName("Hear yourself in %s" % ih.lang_name(code))
        self.setIcon(icons.icon("play", 10, "blue-tx"))
        self.setIconSize(QSize(10, 10))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setStyleSheet(
            "QPushButton { background: #ffffff; color: %s; border: 1px solid %s; border-radius: 16px;"
            " padding: 0px 14px 0px 10px; }"
            " QPushButton:hover { border-color: %s; }"
            " QPushButton[playing=\"true\"] { background: %s; border-color: %s; }"
            " QPushButton[focusVisible=\"true\"] { border: 2px solid %s; }"
            % (theme.qss_color("ink"), theme.qss_color("line2"), theme.qss_color("blue-l"),
               theme.qss_color("blue-sub"), theme.qss_color("blue-line"), theme.qss_color("blue-l")))

    def set_playing(self, on):
        theme.set_prop(self, "playing", bool(on), children=False)


class VoiceCombo(QComboBox):
    """A preset voice picker: "Sarah · calm" rows (interpreter/voices.py)."""

    def __init__(self, presets, accessible, parent=None):
        super().__init__(parent)
        self.setAccessibleName(accessible)
        self.setFont(theme.font(13))
        self.setFixedHeight(32)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet("QComboBox { padding: 0px 6px 0px 10px; min-height: 30px; }")
        # Sized by the row, not by its longest name (the popup is still as wide as that).
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        for vid, _name, _gender, _style in presets:
            self.addItem(voice_presets.label(vid), vid)

    def set_id(self, voice_id):
        i = self.findData(voice_id)
        self.blockSignals(True)
        self.setCurrentIndex(i if i >= 0 else 0)
        self.blockSignals(False)

    def voice_id(self):
        return self.currentData() or ""

    def showPopup(self):
        fm = QFontMetrics(self.font())
        widest = max((fm.horizontalAdvance(self.itemText(i)) for i in range(self.count())), default=0)
        self.view().setMinimumWidth(max(self.width(), widest + 60))
        super().showPopup()


class _SampleButton(QPushButton):
    """The small round play button beside a voice: its bundled sample, played in the app."""

    def __init__(self, accessible, parent=None):
        super().__init__(parent)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        theme.focus_ring(self)
        self.setAccessibleName(accessible)
        self.set_playing(False)
        self.setStyleSheet(
            "QPushButton { background: #ffffff; border: 1px solid %s; border-radius: 15px; }"
            " QPushButton:hover { border-color: %s; background: %s; }"
            " QPushButton:disabled { background: %s; border-color: %s; }"
            " QPushButton[focusVisible=\"true\"] { border: 2px solid %s; }"
            % (theme.qss_color("line2"), theme.qss_color("blue-l"), theme.qss_color("blue-sub"),
               theme.qss_color("bg2"), theme.qss_color("line"), theme.qss_color("blue-l")))

    def set_playing(self, on):
        self.setIcon(icons.icon("stop" if on else "play", 12, "blue-tx", 2.2 if on else 1.7))
        self.setIconSize(QSize(12, 12))
        self.setToolTip("Stop" if on else "Play a sample")


class VoiceForThemRow(SettingRow):
    """"Voice for them" (interpreter/voices.py): Match the speaker - a woman's and a man's voice -
    or One voice for everyone. Each voice has a round play button for its bundled sample
    (assets/voices/<id>.wav, played on a worker thread; a voice without its file has the button
    off). Every change writes its key at once; a running interpreter takes it for the next phrase
    (InterpreterHost follows the keys)."""

    def __init__(self, page):
        super().__init__("Voice for them", None, stacked=True)
        self.page = page
        self._play_stop = None
        self._playing = None
        lay = self.layout()
        lay.setSpacing(8)
        self.mode_combo = QComboBox(self)
        self.mode_combo.setAccessibleName("Voice for them")
        self.mode_combo.setFont(theme.font(13.5))
        self.mode_combo.setFixedHeight(34)
        self.mode_combo.setCursor(Qt.PointingHandCursor)
        self.mode_combo.setFocusPolicy(Qt.StrongFocus)
        self.mode_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.mode_combo.setMinimumContentsLength(10)
        for text, value in VOICE_MODES:
            self.mode_combo.addItem(text, value)
        self.mode_combo.activated.connect(self._on_mode)
        lay.addWidget(self.mode_combo)
        females = voice_presets.of_gender("female")
        males = voice_presets.of_gender("male")
        self.female_combo, self.female_play, self.female_line = self._voice_line("Woman's voice", females)
        self.male_combo, self.male_play, self.male_line = self._voice_line("Man's voice", males)
        self.fixed_combo, self.fixed_play, self.fixed_line = self._voice_line("Voice", voice_presets.PRESETS)
        for combo, key in ((self.female_combo, "interpreter_voice_female"),
                           (self.male_combo, "interpreter_voice_male"),
                           (self.fixed_combo, "interpreter_voice_fixed")):
            combo.activated.connect(lambda _i, c=combo, k=key: self._on_voice(k, c))
        for play, combo in self._pairs():
            play.clicked.connect(lambda _=False, b=play, c=combo: self._play(b, c))
            combo.currentIndexChanged.connect(lambda _i, b=play, c=combo: self._sync_play(b, c))
        self.hint = label(VOICE_MATCH_HINT, "hint", self, wrap=True)
        lay.addWidget(self.hint)
        self.refresh(page.cfg())

    def _pairs(self):
        return ((self.female_play, self.female_combo), (self.male_play, self.male_combo),
                (self.fixed_play, self.fixed_combo))

    def _voice_line(self, text, presets):
        line = QWidget(self)
        h = QHBoxLayout(line)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        name = _lbl(text, 12.5, 500, "text2", line)
        name.setFixedWidth(92)
        h.addWidget(name, 0, Qt.AlignVCenter)
        combo = VoiceCombo(presets, text, line)
        h.addWidget(combo, 1)
        play = _SampleButton("Play a sample of the %s" % text.lower(), line)
        h.addWidget(play, 0, Qt.AlignVCenter)
        self.layout().addWidget(line)
        return combo, play, line

    def mode(self):
        return self.mode_combo.currentData() or voice_presets.MATCH

    def refresh(self, cfg):
        choice = voice_presets.VoiceChoice.from_config(cfg or {})
        i = self.mode_combo.findData(choice.mode)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(max(0, i))
        self.mode_combo.blockSignals(False)
        self.female_combo.set_id(choice.female)
        self.male_combo.set_id(choice.male)
        self.fixed_combo.set_id(choice.fixed)
        match = choice.mode == voice_presets.MATCH
        self.female_line.setVisible(match)
        self.male_line.setVisible(match)
        self.hint.setVisible(match)
        self.fixed_line.setVisible(not match)
        for play, combo in self._pairs():
            self._sync_play(play, combo)

    def _on_mode(self, _i):
        mode = self.mode()
        if self.page.set("interpreter_voice_mode", mode):
            self.refresh(dict(self.page.cfg(), interpreter_voice_mode=mode))
        else:
            self.refresh(self.page.cfg())

    def _on_voice(self, key, combo):
        if not self.page.set(key, combo.voice_id()):
            self.refresh(self.page.cfg())

    @staticmethod
    def sample_exists(voice_id):
        try:
            return bool(voice_id) and os.path.isfile(voice_presets.sample_path(voice_id))
        except Exception:
            return False

    def _sync_play(self, play, combo):
        play.setEnabled(self.sample_exists(combo.voice_id()))

    def stop_sample(self):
        if self._play_stop is not None:
            self._play_stop.set()
        if self._playing is not None:
            self._playing.set_playing(False)
            self._playing = None

    def _play(self, button, combo):
        was = self._playing
        self.stop_sample()
        if was is button:
            return                                  # a second press stops it
        path = voice_presets.sample_path(combo.voice_id())
        stop = threading.Event()
        self._play_stop = stop
        self._playing = button
        button.set_playing(True)

        def work():
            pcm = voice_builder.read_wav(path).tobytes()
            interpreter_audio.play_pcm(pcm, voice_builder.RATE, stop=stop)
            return True

        def done(_r=None, b=button):
            if self._playing is b:
                b.set_playing(False)
                self._playing = None

        run_bg(work, then=done, error=lambda _e: done())


def _card_title(text, parent):
    return _lbl(text, 18, 600, "ink", parent, display=True)


# ── My voice ─────────────────────────────────────────────────────────────────
class MyVoiceTab(QWidget):
    """No voice yet / Record now / Ready - one card at a time (see the module docstring)."""

    _level = Signal(float)
    _preview_started = Signal(object)       # the play's stop event: its sound has started

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.app = page.app
        self.host = page.host
        self.voice = None                 # {"voice_id", "source", "created_at"} once known
        self.voice_known = False
        self.found = None                 # voice_builder.Found from the meetings
        self._scan_gen = 0
        self._scanning = False
        self._scan_stop = threading.Event()
        self._get_gen = 0
        self._rec_gen = 0
        self._rec_stop = None
        self._rec_started = 0.0
        self._clips = []
        self._step = 0
        self._phrase = ""
        self._busy = False
        self._play_stop = None
        self._level.connect(self._on_level)
        self._preview_started.connect(self._preview_playing)
        self._preview_audio = {}          # (voice id, language) -> (pcm, rate): Play again is instant
        self._prefetching = {}            # key -> threading.Event, set when that fetch is over
        self._spin_step = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(80)
        self._spin_timer.timeout.connect(self._spin)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._rec_mode = "ready"
        self._take = None
        self._take_stop = None
        self._make_started = 0.0
        self._make_est = 0.0
        self._make_note = ""
        self.create_card = self._build_create()
        self.record_card = self._build_record()
        self.ready_card = self._build_ready()
        self._cards = {"create": self.create_card, "record": self.record_card, "ready": self.ready_card}
        self._state = ""
        for c in self._cards.values():
            c.hide()
            outer.addWidget(c)
        outer.addStretch(1)
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(250)
        self._rec_timer.timeout.connect(self._tick_recording)
        self._make_timer = QTimer(self)
        self._make_timer.setInterval(200)
        self._make_timer.timeout.connect(self._tick_making)
        self.show_state("create")

    # ── building ──
    def _build_create(self):
        card = Card(self, pad=(22, 22), gap=14)
        card.setAccessibleName(CLONE_TITLE)
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(_MicTile(False, card), 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(_card_title(CLONE_TITLE, card))
        col.addWidget(_lbl(CLONE_SUB, 12.5, 400, "muted", card))
        head.addLayout(col, 1)
        # Make it again: the voice still exists, so there is a way back to it.
        self.keep_button = Button(KEEP_VOICE, "link", "s", parent=card)
        self.keep_button.setFixedHeight(22)
        self.keep_button.clicked.connect(lambda: self.show_state("ready"))
        self.keep_button.hide()
        head.addWidget(self.keep_button, 0, Qt.AlignTop)
        card.add(head)
        self.opt_meetings = OptionCard("From my meetings", "", card)
        self.opt_record = OptionCard("Record now", "Read 3 short texts · 1 min", card)
        self._user_picked = False
        for opt in (self.opt_meetings, self.opt_record):
            opt.clicked.connect(self._picked_by_hand)
        self._opts = QButtonGroup(self)
        self._opts.addButton(self.opt_meetings.radio, 0)
        self._opts.addButton(self.opt_record.radio, 1)
        self._opts.idClicked.connect(lambda _i: self._picked_by_hand())
        opts = QHBoxLayout()
        opts.setSpacing(10)
        opts.addWidget(self.opt_meetings, 1)
        opts.addWidget(self.opt_record, 1)
        card.add(opts)
        consent = QHBoxLayout()
        consent.setSpacing(8)
        self.consent = QCheckBox(card)
        self.consent.setAccessibleName(CONSENT_TEXT + " Required.")
        self.consent.setFocusPolicy(Qt.TabFocus)
        self.consent.setCursor(Qt.PointingHandCursor)
        self.consent.setStyleSheet(CONSENT_BOX_QSS % (theme.qss_color("line2"), theme.qss_color("blue-l"),
                                                      theme.qss_color("blue"), theme.qss_color("blue"),
                                                      theme._asset_url("check.svg")))
        consent.addWidget(self.consent, 0, Qt.AlignTop)
        self.consent_text = _ConsentLabel(card)
        self.consent_text.setTextFormat(Qt.RichText)
        self.consent_text.setWordWrap(True)
        self.consent_text.setFont(theme.font(12))
        self.consent_text.setText(
            "<span style='color:%s'>%s</span><span style='color:%s; font-weight:700'>&nbsp;*</span> "
            "<a href='how' style='color:%s; font-weight:600; text-decoration:none'>%s</a>"
            % (theme.qss_color("text2"), CONSENT_TEXT, REQUIRED_RED, theme.qss_color("blue-tx"), HOW_USED_TITLE))
        self.consent_text.linkActivated.connect(self._how_used)
        self.consent_text.clicked.connect(self.consent.toggle)
        consent.addWidget(self.consent_text, 1)
        card.add(consent)
        self.create_button = HeroButton(CLONE_BUTTON, card)
        self.create_button.setStyleSheet(self.create_button.styleSheet() + HERO_DISABLED_QSS)
        self.create_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.create_button.setFixedHeight(46)
        self.create_button.clicked.connect(self._create_clicked)
        self.create_button.setEnabled(False)               # until "This is my own voice" is ticked
        self.consent.toggled.connect(self._consent_toggled)
        card.add(self.create_button)
        # While the voice is made: a bar that fills over the estimated time and what is happening.
        self.make_box = QWidget(card)
        ml = QVBoxLayout(self.make_box)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(6)
        self.make_bar = QProgressBar(self.make_box)
        self.make_bar.setRange(0, 1000)
        self.make_bar.setTextVisible(False)
        self.make_bar.setFixedHeight(6)
        self.make_bar.setAccessibleName("Making your voice")
        self.make_bar.setStyleSheet(
            "QProgressBar { background: %s; border: none; border-radius: 3px; }"
            " QProgressBar::chunk { border-radius: 3px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            " stop:0 #3b82f6, stop:1 #8b5cf6); }" % theme.qss_color("line"))
        ml.addWidget(self.make_bar)
        mrow = QHBoxLayout()
        mrow.setSpacing(8)
        self.make_label = _lbl("", 12, 500, "text2", self.make_box)
        mrow.addWidget(self.make_label, 1)
        self.make_eta = _lbl("", 12, 500, "muted", self.make_box)
        mrow.addWidget(self.make_eta, 0)
        ml.addLayout(mrow)
        self.make_box.hide()
        card.add(self.make_box)
        self.create_error = label("", "error", card, wrap=True)
        self.create_error.hide()
        card.add(self.create_error)
        kept = _lbl(KEPT_LINE, 12, 400, "muted", card)
        kept.setAlignment(Qt.AlignCenter)
        card.add(kept)
        return card

    def _build_record(self):
        card = Card(self, pad=(22, 20), gap=14)
        card.setAccessibleName("Record now")
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(_lbl("Read this aloud", 15, 600, "ink", card))
        head.addStretch(1)
        self.step_label = _lbl("1 of 3", 12, 500, "muted", card)
        head.addWidget(self.step_label, 0, Qt.AlignVCenter)
        self.steps = StepBars(card)
        head.addWidget(self.steps, 0, Qt.AlignVCenter)
        card.add(head)
        self.read_box = QLabel(card)
        self.read_box.setWordWrap(True)
        self.read_box.setTextFormat(Qt.RichText)
        self.read_box.setFont(theme.font(16))
        self.read_box.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.read_box.setMinimumHeight(96)
        self.read_box.setStyleSheet("QLabel { background: %s; color: %s; border: 1px solid %s; border-radius: 12px;"
                                    " padding: 14px 16px; }" % (theme.qss_color("bg"), theme.qss_color("ink"),
                                                                theme.qss_color("line")))
        card.add(self.read_box)
        self.wave = LevelWave(card)
        card.add(self.wave)
        self.rec_button = RecordButton(card)
        self.rec_button.clicked.connect(self._rec_button_clicked)
        self.stop_button = self.rec_button                  # the old name
        card.add(self.rec_button, align=Qt.AlignHCenter)
        # After a take: listen to it, read it again, or go on - only when pressed.
        self.take_actions = QWidget(card)
        ta = QHBoxLayout(self.take_actions)
        ta.setContentsMargins(0, 0, 0, 0)
        ta.setSpacing(8)
        self.play_button = Button("Play", "secondary", "m", icon="play", parent=self.take_actions)
        self.play_button.setAccessibleName("Play what I recorded")
        self.play_button.clicked.connect(self._play_take)
        ta.addWidget(self.play_button)
        self.redo_button = Button("Record again", "secondary", "m", icon="refresh", parent=self.take_actions)
        self.redo_button.clicked.connect(self._redo_take)
        ta.addWidget(self.redo_button)
        ta.addStretch(1)
        self.next_button = Button("Next", "primary", "m", parent=self.take_actions)
        self.next_button.clicked.connect(self._next_take)
        ta.addWidget(self.next_button)
        self.take_actions.hide()
        card.add(self.take_actions)
        self.rec_time = _lbl(PRESS_TO_RECORD, 12, 500, "muted", card)
        self.rec_time.setAlignment(Qt.AlignCenter)
        card.add(self.rec_time)
        self.rec_note = label("", "error", card, wrap=True)
        self.rec_note.hide()
        card.add(self.rec_note)
        card.add(Divider(card))
        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.rec_foot = _lbl(STEP_HINT, 12, 400, "muted", card, wrap=True)
        foot.addWidget(self.rec_foot, 1)
        self.rec_cancel = Button("Cancel", "link", "s", parent=card)
        self.rec_cancel.setFixedHeight(22)
        self.rec_cancel.setAccessibleName("Cancel recording")
        self.rec_cancel.clicked.connect(self._cancel_record)
        foot.addWidget(self.rec_cancel, 0, Qt.AlignTop)
        card.add(foot)
        return card

    def _build_ready(self):
        card = Card(self, pad=(22, 22), gap=12)
        card.setAccessibleName("Your voice is ready")
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(_MicTile(True, card), 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(_card_title("Your voice is ready", card))
        self.made_label = _lbl("", 12.5, 400, "muted", card)
        col.addWidget(self.made_label)
        head.addLayout(col, 1)
        card.add(head)
        # Hear yourself in any language: pick it, press Play.
        hear = QHBoxLayout()
        hear.setSpacing(10)
        hear.addWidget(_lbl("Hear yourself in", 13, 600, "text2", card), 0, Qt.AlignVCenter)
        self.preview_combo = LangCombo("Hear yourself in", card)
        self.preview_combo.setFixedWidth(200)
        self.preview_combo.activated.connect(lambda _i: self._preview_picked())
        hear.addWidget(self.preview_combo, 0, Qt.AlignVCenter)
        self.preview_play = Button("Play", "secondary", "m", icon="play", parent=card)
        self.preview_play.setAccessibleName("Play my voice")
        self.preview_play.clicked.connect(self._preview)
        hear.addWidget(self.preview_play, 0, Qt.AlignVCenter)
        hear.addStretch(1)
        card.add(hear)
        self._preview_set = False
        self.preview_note = _lbl("", 12, 400, "muted", card, wrap=True)
        self.preview_note.hide()
        card.add(self.preview_note)
        card.add(Divider(card))
        use = QHBoxLayout()
        use.setSpacing(12)
        ucol = QVBoxLayout()
        ucol.setSpacing(2)
        ucol.addWidget(_lbl("Use my voice when I speak", 13.5, 600, "ink", card))
        ucol.addWidget(_lbl("Off: a standard voice", 12, 400, "muted", card))
        use.addLayout(ucol, 1)
        self.use_toggle = Toggle("Use my voice when I speak", True, card)
        self.use_toggle.toggled.connect(self._use_toggled)
        use.addWidget(self.use_toggle, 0, Qt.AlignVCenter)
        card.add(use)
        card.add(Divider(card))
        acts = QHBoxLayout()
        acts.setSpacing(10)
        self.again_button = Button("Make it again", "secondary", "m", parent=card)
        self.again_button.clicked.connect(self._make_again)
        acts.addWidget(self.again_button)
        acts.addStretch(1)
        self.delete_button = Button("Delete my voice", "secondary", "m", parent=card)
        self.delete_button.setStyleSheet(
            "QPushButton { color: %s; border: 1px solid #fca5a5; background: #ffffff; }"
            " QPushButton:hover { background: %s; border-color: %s; }"
            % (theme.qss_color("red-tx"), theme.qss_color("red-sub"), theme.qss_color("red")))
        self.delete_button.clicked.connect(self._delete_clicked)
        acts.addWidget(self.delete_button)
        card.add(acts)
        self.ready_error = label("", "error", card, wrap=True)
        self.ready_error.hide()
        card.add(self.ready_error)
        return card

    # ── states ──
    def state(self):
        return self._state

    def show_state(self, name):
        self._state = name
        for key, card in self._cards.items():
            card.setVisible(key == name)
        if name == "ready":
            self._fill_ready()
        elif name == "create":
            self._sync_keep()
            self._sync_found()
        self._refit()

    def _sync_keep(self):
        """"Keep my voice" on the create card while a voice exists (Make it again, not yet made)."""
        self.keep_button.setVisible(bool(self.voice) and not self._busy)

    def _width(self):
        w = self.width()
        return w if w >= 300 else CARD_W

    def _refit(self):
        """The card on show is as tall as its wrapped lines need at its width (the page around it
        is sized from size hints, which measure wrapped text at another width)."""
        card = self._cards.get(self._state)
        if card is not None:
            lay = card.layout()
            lay.activate()
            w = self._width()
            need = card.heightForWidth(w) if card.hasHeightForWidth() else card.sizeHint().height()
            card.setMinimumHeight(max(need, card.minimumSizeHint().height()))
        self.updateGeometry()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if e.oldSize().width() != e.size().width():
            if self._state == "record":
                self._fit_read_box()
            self._refit()

    def on_open(self):
        """The page opened: ask the server for the voice (anyone signed in). With none, the meetings
        are looked through (once, for a person who can clone it: _voice_loaded)."""
        self.refresh_voice()

    def on_hide(self):
        self._scan_stop.set()
        for ev in (self._play_stop, self._take_stop):
            if ev is not None:
                ev.set()
        if self._rec_mode == "recording":           # never keep the mic open off the page
            self._rec_gen += 1                       # the take in flight is dropped
            if self._rec_stop is not None:
                self._rec_stop.set()
            self._rec_timer.stop()
            self._set_rec_mode("ready")              # the same text waits for Record again

    def showEvent(self, e):
        super().showEvent(e)
        # Back on screen (a page switch, or the window restored) after leaving stopped the scan:
        # look again. (Before the voice is known nothing is scanned: on_open does it then.)
        if self.state() == "create" and self.found is None and self.voice_known and not self.voice:
            self.scan_meetings()

    # ── the voice on the server ──
    def _signed_in(self):
        auth = getattr(self.app, "auth", None)
        if not getattr(auth, "is_authenticated", False):
            return False
        import account_actions
        return not account_actions.is_guest(self.app)

    def refresh_voice(self, fetch=True):
        """Ask the server for the voice. Anyone signed in can see and delete theirs (a person whose
        Pro ended too); a guest has none. An answer still on its way is for the old account: it is
        dropped (``fetch`` False: only that)."""
        self._get_gen += 1
        gen = self._get_gen
        if not fetch:
            return
        if not self._signed_in():
            self.voice, self.voice_known = None, True
            self.page.voice_changed()
            if self.state() == "ready":
                self.show_state("create")
            return
        voices = self.host.voices()
        run_bg(voices.get, then=lambda v, g=gen: self._voice_loaded(g, v),
               error=lambda e, g=gen: self._voice_failed(g, e))

    def _voice_loaded(self, gen, voice):
        if gen != self._get_gen:
            return
        self.voice = voice if isinstance(voice, dict) and voice.get("voice_id") else None
        self.voice_known = True
        self.page.voice_changed()
        if self.state() == "record":
            return
        if self.state() == "create" and self._busy:
            return                                   # a voice is being made right now
        self.show_state("ready" if self.voice else "create")
        if not self.voice:
            self.scan_meetings()

    def _voice_failed(self, gen, exc):
        if gen != self._get_gen:
            return
        logger.info("My voice: couldn't read the voice (%s)", getattr(exc, "code", type(exc).__name__))
        self.voice_known = False
        if self.state() == "create" and not self._busy:
            self.scan_meetings()                     # the create card is on show: its meetings line too

    # ── From my meetings ──
    def scan_meetings(self, again=False):
        """Look for clean parts of the person's voice in the meetings (a worker thread). Done once
        per page, for a person who can clone (Pro); ``again`` after a voice is deleted or made
        again. A scan stopped by leaving the page is only winding down: its result is dropped, and
        the next look starts afresh."""
        if not self.page.is_pro():
            self._sync_found()
            return
        running = self._scanning and not self._scan_stop.is_set()
        if running or (self.found is not None and not again):
            self._sync_found()
            return
        if again:
            self.found = None
        self._scanning = True
        self._scan_gen += 1
        gen = self._scan_gen
        self._scan_stop = threading.Event()
        stop = self._scan_stop
        self._sync_found()
        run_bg(lambda: voice_builder.find_own_voice(should_stop=stop.is_set),
               then=lambda f, g=gen, s=stop: self._found(g, None if s.is_set() else f),
               error=lambda e, g=gen: self._found(g, None))

    def _found(self, gen, found):
        if gen != self._scan_gen:
            return
        self._scanning = False
        if found is not None:
            self.found = found
        self._sync_found()

    def _sync_found(self):
        f = self.found
        opt = self.opt_meetings
        if f is None:
            opt.sub.setText("Looking in your meetings…" if self._scanning else "Clean parts where only you talk.")
            opt.set_chip("")
        else:
            mins = max(0, int(round(f.seconds / 60.0)))
            n = len(f.meetings)
            amount = ("%d min" % mins) if mins else ("%d s" % int(f.seconds))
            # (only calls count: in a meeting at a table everyone in the room is on the mic)
            opt.sub.setText("%s of you in %d meeting%s" % (amount, n, "" if n == 1 else "s") if n
                            else "No calls with your microphone yet.")
            if f.enough:
                opt.set_chip("Enough", "ok")
            else:
                opt.set_chip("Not enough yet", "amber")
        if not self._user_picked or self._opts.checkedId() < 0:
            # Enough of you in the meetings: that is the choice; else Record now.
            (self.opt_meetings if (f is not None and f.enough) else self.opt_record).radio.setChecked(True)
        self._refit()

    def _picked_by_hand(self):
        self._user_picked = True

    def source(self):
        return "meetings" if self._opts.checkedId() == 0 else "recorded"

    # ── Create my voice ──
    def _say(self, label_widget, text):
        label_widget.setText(text or "")
        label_widget.setVisible(bool(text))
        self._refit()

    def _consent_toggled(self, on):
        self.create_button.setEnabled(bool(on) and not self._busy)
        if on:
            self._say(self.create_error, "")

    def _create_clicked(self):
        if self._busy or not self.consent.isChecked():
            return
        if not self.page.is_pro():
            self.page.upsell()
            return
        self._say(self.create_error, "")
        if self.source() == "meetings":
            f = self.found
            if f is None or not f.clips:
                self._say(self.create_error, "No clean parts of your voice yet. Pick Record now.")
                return
            clips = [wav for wav, _s, _m in f.clips]
            # Nothing is read aloud on this path: the ticked sentence is the consent on record.
            self._upload(clips, "meetings", CONSENT_TEXT, meetings=len(f.meetings))
        elif len(self._clips) == 3:
            self._upload(list(self._clips), "recorded", self._phrase)   # the three takes kept from a failed try
        else:
            self._begin_record()

    def _upload(self, wavs, source, phrase, meetings=0):
        import base64
        self._busy = True
        self.show_state("create")
        self.create_button.setText(MAKING)
        self._sync_keep()
        audio_s = sum(max(0, len(w) - 44) for w in wavs) / 2.0 / voice_builder.RATE
        self._make_est = clone_estimate_s(len(wavs), audio_s)
        self._make_started = time.monotonic()
        mins = int(round(audio_s / 60.0))
        self._make_note = SENDING % (("%d min" % mins) if mins else ("%d s" % int(audio_s)))
        self.make_bar.setValue(0)
        self.make_box.show()
        self._tick_making()
        self._make_timer.start()
        clips = [base64.b64encode(w).decode("ascii") for w in wavs]
        voices = self.host.voices()
        n, est = len(wavs), self._make_est
        run_bg(lambda: voices.create(clips, source, phrase),
               then=lambda r: self._created(r, source, meetings, n, audio_s, est),
               error=self._create_failed)

    def _tick_making(self):
        """The bar fills over the estimate (never quite full before the answer); the words say what
        is happening and about how long is left."""
        t = time.monotonic() - self._make_started
        est = max(1.0, self._make_est)
        self.make_bar.setValue(int(950 * (1.0 - math.exp(-2.3 * t / est))))
        self.make_label.setText(self._make_note if t < est * 0.3 else CLONING)
        left = est - t
        self.make_eta.setText(("About %d s left" % max(1, int(round(left)))) if left >= 1.5 else ALMOST)

    def _stop_making(self):
        self._make_timer.stop()
        self.make_box.hide()

    def _created(self, result, source, meetings, clips=0, audio_s=0.0, est=0.0):
        took = time.monotonic() - self._make_started
        logger.info("My voice: made in %.1f s (%d clips, %.0f s of audio; estimate %.0f s)",
                    took, clips, audio_s, est)
        self._busy = False
        self._stop_making()
        self.create_button.setText(CLONE_BUTTON)
        self.create_button.setEnabled(self.consent.isChecked())
        self._clips = []
        result = result if isinstance(result, dict) else {}
        self.voice = {"voice_id": result.get("voice_id") or "", "source": source,
                      "created_at": result.get("created_at") or "", "meetings": meetings}
        self.voice_known = True
        self.page.track("voice_created", {"source": source})
        self.page.voice_changed()
        self.show_state("ready")                     # (its first sound is fetched right away: _fill_ready)
        self._voice_swapped()

    def _create_failed(self, exc):
        self._busy = False
        self._stop_making()
        self.create_button.setText(CLONE_BUTTON)
        self.create_button.setEnabled(self.consent.isChecked())     # (the box may have been ticked again meanwhile)
        code = getattr(exc, "code", "")
        if code in RECORDINGS_REFUSED:
            self._clips = []
        self.show_state("create")
        if code == "pro_required":
            self.page.upsell()
            return
        self._say(self.create_error, ih.message(code) if code else VOICE_FAILED)

    def _voice_swapped(self):
        """A running interpreter speaks in the voice it read when it started: a voice made or
        deleted here takes effect at once (it starts again, as for a new language)."""
        host = self.host
        if host.running and host.has_direction("speak") and self.page.cfg().get("interpreter_voice_on", True):
            host.restart()

    # ── Record now ──
    def _begin_record(self):
        self._clips = []
        self._step = 0
        self._phrase = voice_builder.consent_phrase()
        self._user_picked = True                     # a meetings scan ending now never switches the choice
        self.show_state("record")
        self._show_take()

    def _texts(self):
        return voice_builder.read_texts(ih.my_language(self.page.cfg()))

    def _show_take(self):
        """The text of this step, waiting for the Record button (nothing records by itself)."""
        texts = self._texts()
        text = texts[min(self._step, len(texts) - 1)]
        import html
        body = html.escape(text)
        last = self._step >= 2
        if last:
            body += ("<br><br><span style='color:%s; font-size:13px'>Then say:</span> "
                     "<b style='color:%s'>%s</b>" % (theme.qss_color("muted"), theme.qss_color("blue-tx"),
                                                      html.escape(self._phrase)))
        self.read_box.setText(body)
        self._fit_read_box()
        self.steps.set_step(self._step)
        self.step_label.setText("%d of 3" % (self._step + 1))
        self.rec_foot.setText(LAST_STEP if last else STEP_HINT)
        self.next_button.setText(CLONE_BUTTON if last else "Next")
        self.next_button.setAccessibleName(CLONE_BUTTON if last else "Next text")
        self._take = None
        self._say(self.rec_note, "")
        self._set_rec_mode("ready")

    def _fit_read_box(self):
        # A wrapped label in a card: its height is asked for by hand (the card is sized from hints).
        self.read_box.setFixedHeight(max(96, self.read_box.heightForWidth(self._width() - 44)))

    def _set_rec_mode(self, mode):
        """ready (Record), recording (Stop), recorded (Play / Record again / Next)."""
        self._rec_mode = mode
        self.rec_button.setVisible(mode != "recorded")
        self.rec_button.set_mode("stop" if mode == "recording" else "record")
        self.take_actions.setVisible(mode == "recorded")
        if mode == "ready":
            self.wave.reset()
            self.rec_time.setText(PRESS_TO_RECORD)
        elif mode == "recorded":
            secs = max(0, (len(self._take or b"") - 44)) / 2.0 / voice_builder.RATE
            self.rec_time.setText("%d:%02d recorded" % divmod(int(round(secs)), 60))
            self._show_playing(False)
        self._refit()

    def rec_mode(self):
        return self._rec_mode

    def _rec_button_clicked(self):
        if self._rec_mode == "recording":
            self._stop_take()
        else:
            self._start_recording()

    def _start_recording(self):
        self._stop_playback()
        self._say(self.rec_note, "")
        self._take = None
        self._set_rec_mode("recording")
        self._rec_gen += 1
        gen = self._rec_gen
        self._rec_stop = threading.Event()
        stop = self._rec_stop
        self._rec_started = time.monotonic()
        self.rec_time.setText("0:00")
        self._rec_timer.start()
        cfg = self.page.cfg()
        emit = self._level.emit
        run_bg(lambda: interpreter_audio.record_mic(stop=stop, on_level=emit, cfg=cfg, max_seconds=TAKE_MAX_S),
               then=lambda wav, g=gen: self._take_done(g, wav), error=lambda e, g=gen: self._take_failed(g, e))

    def _tick_recording(self):
        s = int(time.monotonic() - self._rec_started)
        self.rec_time.setText("%d:%02d" % divmod(s, 60))

    def _on_level(self, level):
        self.wave.push(level)

    def _stop_take(self):
        if self._rec_stop is not None:
            self._rec_stop.set()

    def _take_done(self, gen, wav):
        if gen != self._rec_gen:
            return
        self._rec_timer.stop()
        seconds = max(0, (len(wav or b"") - 44)) / 2.0 / voice_builder.RATE
        if seconds < MIN_TAKE_S:
            self._set_rec_mode("ready")
            self._say(self.rec_note, SHORT_TAKE)
            return
        self._take = wav
        self._set_rec_mode("recorded")

    def _take_failed(self, gen, exc):
        if gen != self._rec_gen:
            return
        logger.warning("Record now: the microphone failed: %s", exc)
        self._rec_timer.stop()
        if getattr(exc, "code", "") == "no_mic_sound":
            self._set_rec_mode("ready")
            self._say(self.rec_note, ih.message("no_mic_sound"))
            return
        self.show_state("create")
        self._say(self.create_error, "Couldn't open the microphone. Check it in Settings › Microphone.")

    # listening to a take
    def _show_playing(self, on):
        self.play_button.setText("Stop" if on else "Play")
        self.play_button.setIcon(icons.icon("stop" if on else "play", 12, "text2"))
        self.play_button.setAccessibleName("Stop playing" if on else "Play what I recorded")

    def _stop_playback(self):
        if self._take_stop is not None:
            self._take_stop.set()
            self._take_stop = None
        self._show_playing(False)

    def _play_take(self):
        if self._take_stop is not None:              # playing: this press stops it
            self._stop_playback()
            return
        if not self._take:
            return
        pcm = self._take[44:]
        stop = threading.Event()
        self._take_stop = stop
        self._show_playing(True)

        def done(_r=None):
            if self._take_stop is stop:
                self._take_stop = None
                self._show_playing(False)

        run_bg(lambda: interpreter_audio.play_pcm(pcm, voice_builder.RATE, stop=stop), then=done, error=done)

    def _redo_take(self):
        self._start_recording()

    def _next_take(self):
        if not self._take:
            return
        self._stop_playback()
        self._clips.append(self._take)
        self._take = None
        self._step += 1
        if self._step < 3:
            self._show_take()
            return
        self._upload(self._clips, "recorded", self._phrase)

    def _cancel_record(self):
        self._rec_gen += 1
        if self._rec_stop is not None:
            self._rec_stop.set()
        self._stop_playback()
        self._rec_timer.stop()
        self._clips = []
        self._take = None
        self.show_state("ready" if self.voice else "create")       # Make it again, cancelled: the voice is still there

    # ── Ready ──
    def _fill_ready(self):
        self._say(self.ready_error, "")
        v = self.voice or {}
        when = _short_date(v.get("created_at"))
        n = v.get("meetings") or (len(self.found.meetings) if self.found and v.get("source") == "meetings" else 0)
        if v.get("source") == "meetings":
            src = "from %d meeting%s" % (n, "" if n == 1 else "s") if n else "from your meetings"
        else:
            src = "from a recording"
        self.made_label.setText(("Made %s %s" % (when, src)) if when else ("Made " + src))
        cfg = self.page.cfg()
        if not self._preview_set:
            # What the others hear first; any language can be picked.
            self.preview_combo.set_code(ih.their_language(cfg))
        _set_toggle(self.use_toggle, bool(cfg.get("interpreter_voice_on", True)))
        self._refit()
        self._prefetch_preview()

    def _prefetch_preview(self):
        """Fetch the picked language's sound in the background, so Play starts at once. A voice
        ElevenLabs has just made is slow the first time it speaks (~8 s); this takes that wait
        while the person reads "Your voice is ready"."""
        if not self.page.is_pro() or not self.voice:
            return
        code = self.preview_combo.code() or "en"
        key = (self.voice.get("voice_id", ""), code)
        if key in self._preview_audio or key in self._prefetching:
            return
        over = threading.Event()                     # a Play pressed meanwhile waits for this one
        self._prefetching[key] = over
        voices = self.host.voices()
        kept = self._preview_audio

        def work(k=key):
            try:
                pcm, rate = voices.preview(self._sample_for(code), code)
                if pcm:
                    kept.setdefault(k, (pcm, rate))
                return True
            finally:
                over.set()

        def done(_r, k=key):
            self._prefetching.pop(k, None)

        def failed(exc, k=key):
            self._prefetching.pop(k, None)
            logger.info("Hear yourself in: couldn't fetch %s ahead (%s)", k[1], getattr(exc, "code", type(exc).__name__))

        run_bg(work, then=done, error=failed)

    def _preview_picked(self):
        self._preview_set = True
        if self._play_stop is not None:              # one is loading or playing: the pick replaces it
            self._play_stop.set()
            self._play_stop = None
            self._show_preview("idle")
        self._preview()

    @staticmethod
    def preview_text(code):
        """The sentence your voice says in ``code``: "Hi, this is my voice in <language>." - built in
        (SAMPLES) or translated once and kept ("" = not translated yet)."""
        return SAMPLES.get(code) or _SAMPLE_CACHE.get(code, "")

    def _sample_for(self, code):
        """preview_text, translating it the first time (a worker thread calls this)."""
        text = self.preview_text(code)
        if text:
            return text
        english = SAMPLE_EN % ih.lang_name(code)
        from interpreter import translate
        auth = getattr(self.app, "auth", None)
        fn = getattr(auth, "get_access_token", None)
        try:
            text = (translate.translate_phrase(english, "en", code, [], lambda _d: None,
                                               token=fn() if callable(fn) else None) or "").strip()
        except Exception:
            logger.info("Hear yourself in: couldn't translate the sentence into %s", code)
            text = ""
        if not text:
            return english                           # the words in English; not kept, tried again next time
        _SAMPLE_CACHE[code] = text
        return text

    def _preview(self):
        """Play: loading (a spinner; a press cancels), then playing (Stop), then Play again."""
        if not self.page.is_pro():
            self.page.upsell()
            return
        if self._play_stop is not None:              # loading or playing: this press stops it
            self._play_stop.set()
            self._play_stop = None
            self._show_preview("idle")
            return
        code = self.preview_combo.code() or "en"
        stop = threading.Event()
        self._play_stop = stop
        key = ((self.voice or {}).get("voice_id", ""), code)
        cached = self._preview_audio.get(key)
        coming = None if cached is not None else self._prefetching.get(key)    # already on its way
        self._show_preview("loading" if cached is None else "playing")
        voices = self.host.voices()
        started = self._preview_started.emit

        def work():
            got = cached
            if got is None and coming is not None:
                while not coming.wait(0.1):          # the same sound is being fetched: wait for it
                    if stop.is_set():
                        return None
                got = self._preview_audio.get(key)
            if got is None:
                words = self._sample_for(code)
                pcm, rate = voices.preview(words, code)
                if stop.is_set():
                    return None
                self._preview_audio[key] = (pcm, rate)
            else:
                pcm, rate = got
            if stop.is_set():
                return None
            started(stop)
            interpreter_audio.play_pcm(pcm, rate, stop=stop)
            return True

        def done(_r):
            if self._play_stop is stop:
                self._play_stop = None
                self._show_preview("idle")

        def failed(exc):
            if self._play_stop is stop:
                self._play_stop = None
                self._show_preview("idle")
            code_ = getattr(exc, "code", "")
            self._say(self.preview_note, ih.message(code_) if code_ else VOICE_FAILED)

        self._say(self.preview_note, "")
        run_bg(work, then=done, error=failed)

    def _preview_playing(self, stop):
        if self._play_stop is stop:
            self._show_preview("playing")

    def preview_state(self):
        return getattr(self, "_preview_state", "idle")

    def _show_preview(self, state):
        """idle: "▶ Play"; loading: a turning arc + "Loading…" (a press cancels); playing: "■ Stop"."""
        self._preview_state = state
        b = self.preview_play
        if state == "loading":
            self._spin_step = 0
            b.setText("Loading…")
            b.setIcon(spinner_icon(0))
            b.setToolTip("Cancel")
            b.setAccessibleName("Loading, press to cancel")
            self._spin_timer.start()
            return
        self._spin_timer.stop()
        b.setText("Stop" if state == "playing" else "Play")
        b.setIcon(icons.icon("stop" if state == "playing" else "play", 12, "text2"))
        b.setToolTip("")
        b.setAccessibleName("Stop" if state == "playing" else "Play my voice")

    def _spin(self):
        self._spin_step += 1
        self.preview_play.setIcon(spinner_icon(self._spin_step))

    def _show_preview_playing(self, on):
        self._show_preview("playing" if on else "idle")

    def _use_toggled(self, on):
        if not self.page.set("interpreter_voice_on", bool(on)):
            _set_toggle(self.use_toggle, bool(self.page.cfg().get("interpreter_voice_on", True)))

    def _make_again(self):
        if not self.page.is_pro():
            self.page.upsell()
            return
        self.consent.setChecked(False)
        self._clips = []
        self.show_state("create")
        self.scan_meetings(again=True)

    def _delete_clicked(self):
        if not mini_prompt.confirm(self, DELETE_TITLE, DELETE_TEXT, "Delete", "Cancel", icon="trash"):
            return
        self._say(self.ready_error, "")
        self.delete_button.setEnabled(False)
        voices = self.host.voices()
        run_bg(voices.delete, then=self._deleted, error=self._delete_failed)

    def _deleted(self, _ok):
        self.delete_button.setEnabled(True)
        self.voice = None
        self.page.track("voice_deleted", {})
        self.page.voice_changed()
        self.consent.setChecked(False)
        self.show_state("create")
        self.scan_meetings(again=True)
        self._voice_swapped()

    def _delete_failed(self, exc):
        self.delete_button.setEnabled(True)
        code = getattr(exc, "code", "")
        self._say(self.ready_error, ih.message(code) if code else VOICE_FAILED)

    # ── consent ──
    def _how_used(self, _href=None):
        mini_prompt.notify(self, HOW_USED_TITLE, HOW_USED, level="info", icon="shield")


def _set_toggle(toggle, on):
    """Show a value without announcing it."""
    if toggle.isChecked() != on:
        toggle.blockSignals(True)
        toggle.setChecked(on)
        toggle.blockSignals(False)
        toggle.update()


# ── the page ─────────────────────────────────────────────────────────────────
class InterpreterPage(QWidget):
    """See the module docstring. Hooks: on_open, on_setting_changed, refresh_account."""

    _mic_found = Signal(str)

    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.setProperty("gusan", "page")
        self.host = host_for(main_app)
        self._mic = None                   # "" none, a name, None unknown
        self._mic_checking = False
        self._try_stop = None
        self._try_busy = False
        self._mic_found.connect(self._show_mic)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.pro_tag = ProTag("m")
        self.pro_tag.setAccessibleName("Pro feature")
        self.header = PageHeader(TITLE, SUB, title_trail=self.pro_tag, parent=self)
        self.header.setAccessibleName("Interpreter, Pro feature")
        root.addWidget(self.header)
        root.addWidget(self._build_body(), 1)

        h = self.host
        h.state_changed.connect(self._on_state)
        h.notice.connect(self._on_notice)
        h.usage_changed.connect(self._refresh_usage)
        self._refresh_all()
        code, text = getattr(h, "last_notice", ("", ""))
        self._on_notice(code, text)

    # ── building ──
    def _build_body(self):
        body_w = QWidget(self)
        body = QHBoxLayout(body_w)
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(24)
        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(16)
        main.addWidget(self._build_notice(body_w))
        self.picture = ConversationCard(body_w)
        self.picture.try_clicked.connect(self._try_it)
        pc = self.picture
        self.my_combo, self.their_combo, self.swap_button = pc.my_combo, pc.their_combo, pc.swap_button
        self.speak_row, self.hear_row = pc.speak_row, pc.hear_row
        self.my_combo.activated.connect(lambda _i: self._pick_lang("mine"))
        self.their_combo.activated.connect(lambda _i: self._pick_lang("theirs"))
        self.swap_button.clicked.connect(self._swap)
        self.speak_row.toggle.toggled.connect(lambda on: self._direction("speak", on))
        self.hear_row.toggle.toggled.connect(lambda on: self._direction("hear", on))
        main.addWidget(self.picture)
        self.voice_tab = MyVoiceTab(self)
        self.voice_tab.setParent(body_w)
        main.addWidget(self.voice_tab)
        main.addStretch(1)
        body.addLayout(main, 1)
        self.rail = self._build_rail()
        self.rail.setParent(body_w)
        body.addWidget(self.rail, 0, Qt.AlignTop)
        return body_w

    def _build_notice(self, parent):
        """One calm sentence from the interpreter (no virtual microphone, time used up...)."""
        self.notice_box = QWidget(parent)
        notice = QHBoxLayout(self.notice_box)
        notice.setContentsMargins(2, 0, 0, 0)
        notice.setSpacing(8)
        self.notice_label = _lbl("", 13, 400, "text2", self.notice_box, wrap=True)
        notice.addWidget(self.notice_label, 1)
        self.notice_link = Button("Set up", "link", "s", parent=self.notice_box)
        self.notice_link.setFixedHeight(22)
        self.notice_link.setAccessibleName("Set up the virtual microphone")
        self.notice_link.clicked.connect(lambda: self.focus_microphone())
        notice.addWidget(self.notice_link, 0, Qt.AlignTop)
        self.notice_box.hide()
        return self.notice_box

    def _build_rail(self):
        rail = SettingsRail("Interpreter settings", width=RAIL_W)
        cfg = self.cfg()
        out = QWidget()
        ol = QVBoxLayout(out)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(6)
        self.output_seg = Segmented([t for _v, t in OUTPUTS], 0, "I hear it on", size="s")
        self.output_seg.setStyleSheet("QPushButton[gusan=\"seg\"] { padding: 0px 12px; }")
        self.output_seg.changed.connect(self._on_output)
        ol.addWidget(self.output_seg, 0, Qt.AlignLeft)
        self.output_hint = label(OUTPUT_HINT, "hint", out, wrap=True)
        ol.addWidget(self.output_hint)
        out.setAccessibleName("I hear it on")
        self.output_row = rail.add_row(SettingRow("I hear it on", out, stacked=True))
        self.duck_toggle = self._bool_row(rail, "Turn the call down while translating", "interpreter_duck")
        self.under_toggle = self._bool_row(rail, "Keep their voice underneath", "interpreter_keep_underneath")
        self.captions_toggle = self._bool_row(rail, "Captions", "interpreter_captions")
        self.voice_row = rail.add_row(VoiceForThemRow(self))
        mic = QWidget()
        ml = QVBoxLayout(mic)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(4)
        self.mic_chip = QLabel("", mic)
        self.mic_chip.setFont(theme.font(12.5, 600))
        self.mic_chip.setAlignment(Qt.AlignCenter)
        self.mic_chip.setFixedHeight(26)
        ml.addWidget(self.mic_chip, 0, Qt.AlignRight)
        self.mic_setup = Button("Set up", "link", "s", parent=mic)
        self.mic_setup.setFixedHeight(22)
        self.mic_setup.setAccessibleName("Set up the virtual microphone")
        self.mic_setup.clicked.connect(self._set_up_mic)
        ml.addWidget(self.mic_setup, 0, Qt.AlignRight)
        self.mic_row = rail.add_row(SettingRow("Microphone in the call", mic, hint="Pick it in Zoom, Teams or Meet"))
        self.mic_hint = label(CABLE_HINT, "hint", self.mic_row, wrap=True)
        self.mic_hint.hide()
        try:
            self.mic_row.layout().itemAt(0).layout().addWidget(self.mic_hint)
        except Exception:
            logger.debug("mic hint not placed", exc_info=True)
        self.notice_toggle = self._bool_row(rail, "Tell people an AI interpreter is on", "interpreter_notice",
                                            hint="Said once when you start")
        self.notice_toggle._row.setVisible(notice_spoken())
        self.shortcut = ShortcutControl(cfg.get(HOTKEY_KEY, ""))
        self.shortcut.capture._name = "Interpreter shortcut"
        self.shortcut.capture.setAccessibleName("Interpreter shortcut")
        self.shortcut.capture._show_keys()
        self.shortcut.capture.captured.connect(self._on_hotkey_captured)
        self.shortcut.capture.capturing_changed.connect(lambda on: self._call("suspend_hotkeys", bool(on)))
        self.shortcut_row = rail.add_row(SettingRow("Shortcut", self.shortcut))
        foot = QWidget()
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(8)
        self.models_label = _lbl(MODELS, 12, 400, "muted", foot, wrap=True)
        fl.addWidget(self.models_label)
        self.usage_box = QWidget(foot)
        ul = QVBoxLayout(self.usage_box)
        ul.setContentsMargins(0, 4, 0, 0)
        ul.setSpacing(6)
        urow = QHBoxLayout()
        urow.addWidget(_lbl("This month", 12, 400, "muted", self.usage_box))
        urow.addStretch(1)
        self.usage_label = _lbl("", 12, 500, "text2", self.usage_box)
        urow.addWidget(self.usage_label)
        ul.addLayout(urow)
        self.usage_bar = QProgressBar(self.usage_box)
        self.usage_bar.setRange(0, 1000)
        self.usage_bar.setTextVisible(False)
        self.usage_bar.setFixedHeight(5)
        self.usage_bar.setStyleSheet(
            "QProgressBar { background: %s; border: none; border-radius: 2px; }"
            " QProgressBar::chunk { border-radius: 2px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            " stop:0 #3b82f6, stop:1 #8b5cf6); }" % theme.qss_color("line"))
        ul.addWidget(self.usage_bar)
        fl.addWidget(self.usage_box)
        self.usage_box.hide()
        rail.set_foot(foot)
        # The rows wrap (hints): the rail's height comes from them, so its head and foot keep
        # their own height and the rows get the rest (else the head takes it and rows squeeze).
        lay = rail.layout()
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None and w is not rail._body:
                w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        return rail

    def _bool_row(self, rail, text, key, hint=None):
        t = Toggle(text, bool(self.cfg().get(key, True)))
        t.toggled.connect(lambda on, k=key, tg=t: self._apply_bool(k, on, tg))
        row = rail.add_row(SettingRow(text, t, hint=hint))
        t._row = row
        return t

    # ── app helpers ──
    def cfg(self):
        cfg = getattr(self.app, "cfg", None)
        return cfg if isinstance(cfg, dict) else {}

    def is_pro(self):
        try:
            return bool(self.app.is_pro())
        except Exception:
            return False

    def upsell(self):
        self._call("_pro_upsell", UPSELL)

    def track(self, event, props=None):
        self._call("track", event, props or {})

    def _call(self, method, *args, **kwargs):
        fn = getattr(self.app, method, None)
        if not callable(fn):
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.warning("%s() failed", method, exc_info=True)
            return None

    def set(self, key, value):
        fn = getattr(self.app, "set_setting", None)
        if callable(fn):
            try:
                return bool(fn(key, value))
            except Exception:
                logger.warning("set_setting(%s) failed", key, exc_info=True)
                return False
        self.cfg()[key] = value
        self.on_setting_changed(key)
        return True

    def set_many(self, updates):
        fn = getattr(self.app, "set_settings", None)
        if callable(fn):
            try:
                res = fn(dict(updates)) or {}
                return all(res.get(k, False) for k in updates)
            except Exception:
                logger.warning("set_settings failed", exc_info=True)
                return False
        return all([self.set(k, v) for k, v in updates.items()])

    # ── shell hooks ──
    def on_open(self, **opts):
        self._refresh_all()
        self.check_microphone()
        self.voice_tab.on_open()
        if opts.get("tab") in ("voice", "my_voice"):
            self.set_tab(1)
        if opts.get("focus") == "microphone":
            self.focus_microphone()
        elif opts.get("focus") in ("hotkey", "shortcut"):
            self.shortcut.capture.button.setFocus(Qt.TabFocusReason)

    def on_setting_changed(self, key):
        if str(key).startswith("interpreter_") or key in ("language", "privacy_mode"):
            self._refresh_all()

    def refresh_account(self):
        self._refresh_all()
        self.voice_tab.voice_known = False
        # (an answer about the old account still on its way is dropped either way)
        self.voice_tab.refresh_voice(fetch=self.isVisible())

    def hideEvent(self, e):
        self.shortcut.capture.cancel_capture()
        self.voice_row.stop_sample()
        self.voice_tab.on_hide()
        self._cancel_try()
        super().hideEvent(e)

    # ── old "My voice" tab links ──
    def set_tab(self, idx):
        """1: your voice's card, scrolled into view; 0: the top of the page."""
        self._scroll_to(self.voice_tab if idx == 1 else self.picture)

    def _scroll_to(self, widget):
        scroll = self.parentWidget()
        while scroll is not None and not hasattr(scroll, "ensureWidgetVisible"):
            scroll = scroll.parentWidget()
        if scroll is not None:
            scroll.ensureWidgetVisible(widget, 0, 24)

    # ── state ──
    def _refresh_all(self):
        cfg = self.cfg()
        my, their = ih.my_language(cfg), ih.their_language(cfg)
        self.my_combo.set_code(my)
        self.their_combo.set_code(their)
        self.speak_row.set_title("They hear me in %s" % ih.lang_name(their))
        self.hear_row.set_title("I hear them in %s" % ih.lang_name(my))
        _set_toggle(self.speak_row.toggle, bool(cfg.get("interpreter_speak", True)))
        _set_toggle(self.hear_row.toggle, bool(cfg.get("interpreter_hear", True)))
        self.hear_row.sub.setText("With captions" if cfg.get("interpreter_captions", True) else "Voice only")
        owner = _first_name(self.app)
        self.picture.set_languages(my, their, owner)
        self._refresh_picture_directions(cfg)
        self.speak_row.sub_face.set_letter(owner[:1] if owner else "Y")
        self.voice_changed()
        out = cfg.get("interpreter_output") or "auto"
        idx = next((i for i, (v, _t) in enumerate(OUTPUTS) if v == out), 0)
        self.output_seg.set_index(idx)
        for t, key in ((self.duck_toggle, "interpreter_duck"), (self.under_toggle, "interpreter_keep_underneath"),
                       (self.captions_toggle, "interpreter_captions"), (self.notice_toggle, "interpreter_notice")):
            _set_toggle(t, bool(cfg.get(key, True)))
        self.voice_row.refresh(cfg)
        self.shortcut.set_hotkey(cfg.get(HOTKEY_KEY, "") or "")
        self._refresh_usage()

    def voice_changed(self):
        """The line under "They hear me in ...": my voice (it exists and is on) or a standard one.
        Try it's line follows: it plays the words back only in that voice."""
        on = self._own_voice_on()
        self.speak_row.sub.setText("In my voice" if on else "Standard voice")
        self.speak_row.sub_face.setVisible(on)
        mine = bool(self.voice_tab.voice) if hasattr(self, "voice_tab") else False
        self.picture.set_voice(on, cloned=mine)

    def _own_voice_on(self):
        mine = bool(self.voice_tab.voice) if hasattr(self, "voice_tab") else False
        return bool(self.cfg().get("interpreter_voice_on", True)) and mine

    def _on_state(self, state):
        """No Start button on this page: the interpreter starts where calls happen."""
        return state

    def _on_notice(self, code, text):
        self.notice_label.setText(text or "")
        self.notice_link.setVisible(code == "no_virtual_mic")
        self.notice_box.setVisible(bool(text))

    def _refresh_usage(self):
        left = self.host.seconds_left
        if left is None:
            self.usage_box.hide()
            return
        total = self.host.allowance_seconds or DEFAULT_ALLOWANCE_S
        try:
            used = max(0, float(total) - float(left))
        except (TypeError, ValueError):
            self.usage_box.hide()
            return
        self.usage_label.setText("%s of %s" % (ih.minutes_text(used), ih.minutes_text(total)))
        self.usage_bar.setValue(int(1000 * min(1.0, used / float(total or 1))))
        self.usage_box.show()

    # ── actions ──
    def _pick_lang(self, side):
        cfg = self.cfg()
        my, their = ih.my_language(cfg), ih.their_language(cfg)
        new = (self.my_combo if side == "mine" else self.their_combo).code()
        if side == "mine":
            ok = (self.set_many({"interpreter_my_language": their, "interpreter_their_language": my})
                  if new == their else self.set("interpreter_my_language", new))
        else:
            ok = (self.set_many({"interpreter_my_language": their, "interpreter_their_language": my})
                  if new == my else self.set("interpreter_their_language", new))
        if not ok:
            self._refresh_all()

    def _swap(self):
        cfg = self.cfg()
        my, their = ih.my_language(cfg), ih.their_language(cfg)
        if not self.set_many({"interpreter_my_language": their, "interpreter_their_language": my}):
            self._refresh_all()

    def _direction(self, name, on):
        if not self.host.set_direction(name, on):
            self._refresh_all()
            return
        self._refresh_picture_directions()

    def _refresh_picture_directions(self, cfg=None):
        cfg = self.cfg() if cfg is None else cfg
        self.picture.set_directions(bool(cfg.get("interpreter_speak", True)),
                                    bool(cfg.get("interpreter_hear", True)))

    def _apply_bool(self, key, on, toggle):
        if not self.set(key, bool(on)):
            _set_toggle(toggle, bool(self.cfg().get(key, True)))

    def _on_output(self, idx):
        value = OUTPUTS[idx][0] if 0 <= idx < len(OUTPUTS) else "auto"
        if not self.set("interpreter_output", value):
            self._refresh_all()

    def _on_hotkey_captured(self, combo):
        if self.set(HOTKEY_KEY, combo):
            self.shortcut.set_error("")
            return
        self.shortcut.set_hotkey(self.cfg().get(HOTKEY_KEY, ""))
        self.shortcut.set_error(SHORTCUT_REFUSED % (" + ".join(keycap_labels(combo)) or combo))

    # ── the virtual microphone ──
    def check_microphone(self):
        if self._mic_checking:
            return
        self._mic_checking = True
        emit = self._mic_found.emit

        def work():
            try:
                return find_virtual_mic()
            except Exception:
                logger.debug("virtual microphone check failed", exc_info=True)
                return ""

        run_bg(work, then=emit, error=lambda _e: emit(""))
        if self._mic is None:
            self._show_mic(None)

    def _show_mic(self, name):
        if name is not None:
            self._mic_checking = False
        self._mic = name
        if name:
            self.mic_chip.setText("●  " + name)
            self.mic_chip.setStyleSheet(
                "QLabel { background: %s; color: %s; border: 1px solid %s; border-radius: 13px; padding: 0px 11px; }"
                % (theme.qss_color("ok-sub"), theme.qss_color("ok-tx"), theme.qss_color("ok-line")))
            self.mic_chip.setAccessibleName("Virtual microphone: " + name)
            self.mic_setup.hide()
            self.mic_hint.hide()
        else:
            self.mic_chip.setText("Not set up" if name == "" else "Checking…")
            self.mic_chip.setStyleSheet("QLabel { color: %s; padding: 0px 2px; }" % theme.qss_color("muted"))
            self.mic_chip.setAccessibleName("Virtual microphone: not set up")
            self.mic_setup.setVisible(name == "")

    def _set_up_mic(self):
        QDesktopServices.openUrl(QUrl(CABLE_URL))
        self.mic_hint.show()
        QTimer.singleShot(15000, self, self.check_microphone)

    def focus_microphone(self):
        """The no_virtual_mic sentence's Set up: the microphone row, in view and lit."""
        self.mic_hint.show()
        if self._mic == "":
            self.mic_setup.setFocus(Qt.TabFocusReason)
        scroll = self.parentWidget()
        while scroll is not None and not hasattr(scroll, "ensureWidgetVisible"):
            scroll = scroll.parentWidget()
        if scroll is not None:
            scroll.ensureWidgetVisible(self.mic_row)

    # ── Try it ──
    def _cancel_try(self):
        """Try it pressed again, or the page left: that run stops at its next step and is let go -
        the button is free at once, and nothing it still brings back is shown."""
        stop = self._try_stop
        if stop is None:
            return
        stop.set()
        self._try_stop = None
        self._try_busy = False
        self.picture.try_button.setText("Try it")

    def _try_it(self):
        if self._try_busy:
            self._cancel_try()
            return
        if not self.is_pro():
            self.upsell()
            return
        if self.cfg().get("privacy_mode"):
            from ui import privacy_prompt
            if not privacy_prompt.confirm_turn_off_privacy(self, ih.PRIVACY_FEATURE):
                return
            if not self.set("privacy_mode", False):
                return
        cfg = self.cfg()
        my, their = ih.my_language(cfg), ih.their_language(cfg)
        self._try_busy = True
        stop = threading.Event()
        self._try_stop = stop
        btn = self.picture.try_button
        btn.setText(TRY_PHASES["listening"])
        self.picture.set_try_note("")
        phase = _PhaseRelay(btn, self, current=lambda: self._try_stop is stop)
        # It plays the words back only in the voice the call will use: your own, when it is on.
        voices = self.host.voices() if self._own_voice_on() else None

        def finish():
            """True when this run is still the one on show (and now it is over)."""
            phase.deleteLater()
            if self._try_stop is not stop:
                return False                         # let go: pressed again, or the page was left
            self._try_stop = None
            self._try_busy = False
            btn.setText("Try it")
            return True

        def done(result):
            if not finish():
                return
            result = result or {}
            if not result.get("said"):
                self.picture.set_try_note(TRY_NOTHING)
                return
            self.picture.set_try_note("")
            self.picture.show_try(result.get("said"), result.get("heard"))

        def failed(exc):
            logger.info("Try it failed: %s", exc)
            if not finish():
                return
            code = getattr(exc, "code", "") or ""
            known = code in ih.EXTRA_MESSAGES or code in SERVER_MESSAGES
            # on this page only: the interpreter's own notices (the live card, the tray) are not for it
            self.picture.set_try_note(ih.message(code) if known else TRY_FAILED)

        run_bg(lambda: interpreter_audio.try_it(self.app, my, their, on_phase=phase.changed.emit, stop=stop,
                                                voices=voices), then=done, error=failed)


class _PhaseRelay(QWidget):
    """Carries Try it's phase from the worker to the button: ``changed`` is emitted on the worker
    and ``_show`` (a slot of this GUI-thread object) runs queued. A run that was let go
    (``current()`` False) no longer writes on the button."""
    changed = Signal(str)

    def __init__(self, button, parent, current=lambda: True):
        super().__init__(parent)
        self.hide()
        self._button = button
        self._current = current
        self.changed.connect(self._show)

    def _show(self, phase):
        if self._current():
            self._button.setText(TRY_PHASES.get(phase, self._button.text()))


__all__ = ["InterpreterPage", "LangPairRow", "MyVoiceTab", "create", "host_for", "pair_after_pick", "pair_updates",
           "preview_languages"]
