"""Audio to text page (Gusan 2.0): "Convert audio to text".

Boards: Files.dc.html (the library) and FileResult.dc.html (a transcript open,
ui/pages/transcript_view.py). The same page for Free, Pro and guests, with no
limits anywhere. The one Pro item is Gusan Cloud in Accuracy: a plain "PRO"
after its name for Free users, and picking it opens the Pro window.

    page = create(main_app)
    page.on_open()                                   # the library, list refreshed
    page.on_open(paths=["C:/rec/interview.m4a"])     # queue files (tray, drops, IPC)
    page.on_open(transcript_id="20261007_112000")    # open one (e.g. "Transcript ready")

Library: a big drop zone (🎧 → 📝; drop several files anywhere on the page, or
Choose a file), the running job bound to app.file_jobs (progress, stage, "Ready
in about N min", Cancel; then Ready · Open, or the failure with Try again),
"Your files" from transcript_store (🎤 / 🎬 / 📞 by type; Open; ⋯ Rename / Open
Word file / Show in folder / Delete), and the "Audio to text settings" rail
(Accuracy: Automatic, the Whisper models, Gusan Cloud, and each own key once
it is saved, with the real model under the plain name; Language, Speaker
labels, Timestamps, Save as Word; the footer says where the audio goes) -
every change goes through app.set_setting and applies at once. Privacy mode
greys the cloud items out. All disk work (listing, opening, renaming,
deleting, saving) runs on a worker thread.

Integration notes for Wave 4
============================
1. Services the page uses (all optional - without them it shows a calm state
   and never crashes): ``app.file_jobs`` (file_jobs.FileJobController: enqueue /
   cancel / current / pending / track_saved and its signals queued, started,
   progress, finished, failed, cancelled, running_changed), ``app.downloads``
   (model_manager.ModelDownloads: ready("whisper"), .hardware, and the signals
   state_changed / scanned / hardware_ready - only for the model named under
   "Automatic"), ``app.recorder._cuda_usable``, ``app.cfg``,
   ``app.set_setting``, ``app.is_pro()`` and ``app._pro_upsell`` (Gusan
   Cloud in Accuracy, note 11). The page connects to file_jobs / downloads when it is
   built and again on every on_open(), so a controller that creates them after
   the window still gets a live page.
2. FILE_DEFAULTS (file_jobs) must be in main.DEFAULT: the rail reads
   file_model / file_language / file_speakers / file_timestamps / file_save_word
   from app.cfg (missing keys fall back to FILE_DEFAULTS) and writes them with
   app.set_setting(key, value) (plain keys, no side effects). A False return
   puts the control back to the cfg value; on_setting_changed(key) re-reads it.
3. show_file_transcribe(paths=None) -> ``main_window.open("audio_to_text",
   paths=paths)``; the tray "Transcript ready" click (file_jobs note 6) ->
   ``open("audio_to_text", transcript_id=item["transcript_id"])``.
   A plain open() (the sidebar) shows the library.
4. Drops: the page accepts drops of audio / video files (file_jobs.is_audio_file)
   anywhere on itself. A drop elsewhere on the main window (sidebar) is not
   handled here; forward it with open("audio_to_text", paths=...) if wanted.
5. Without app.file_jobs, Choose a file / drops show "Audio to text isn't
   ready yet." inline (the page never calls show_file_transcribe, which Wave 4
   points back at this page).
6. Deleting a row removes only the transcript folder (transcript_store); the
   audio and any Word copy stay. "Show in folder" opens the folder of the Word
   copy, else the audio's folder, else the transcript's own folder.
7. (Wave 4: the shell's placeholder page and its classic-window links are
   gone; the 1.x file tab retired with the old Settings window.)
8. Not in 2.0.0 (implementation plan, section 4): the mockup's Summary tab on
   the transcript view. Speaker names stay "Speaker N" (no naming UI).
9. Queue: when a file fails and the next queued file starts, the failure moves
   from the card to the inline notice ("board_call.mp3: <message>") so it is
   not lost. Back from a transcript refreshes the list (a Save Word there adds
   "Open Word file" to that row's menu).
10. Theme asks (ui/theme.py is not edited here): a 32 px small QComboBox (the
   stylesheet's is 34 px and reserves 72 px for padding + chevron, so the
   Language select needs 202 px instead of the mockup's 172); a token for the
   drop-zone fill rgba(219,234,254,0.40); radius-14 white cards; QMenu rules
   (make_menu builds them from tokens); the h2 display face reads lighter than
   the mockup's Outfit 600 until Outfit is bundled.
11. Accuracy (cfg file_model) comes from engine_choice: file_choices fills the
   list (own-key items only once that key is saved), set_file_model checks a
   pick (refused: the combo snaps back; a Free user's Gusan Cloud also calls
   app._pro_upsell(UPSELL_SPEECH), and nothing is saved), file_session picks
   what the combo shows (a cloud pick that can't run now reads Automatic) and
   file_engine names the model. The list follows on_setting_changed for
   MODEL_WATCH and refresh_account (the PRO marks follow the plan).
"""

import logging
import os

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                               QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

import engine_choice
import file_jobs
import hardware
import speech_langs
import transcript_store
from ui import hy_model, theme
from ui.components import (Button, Card, IconButton, PageHeader, SettingRow, SettingsRail, Toggle,
                           WhiteCard, icons, label, row_actions)
from ui.components.glass_orb import GlassOrb
from ui.components.model_combo import PLAIN_ROLE, PlainNameCombo, combo as _combo  # noqa: F401 (re-exported)
from ui.pages.transcript_view import (HEADPHONES, KIND_EMOJI, KIND_LABEL, LANGUAGES, MEMO,
                                      ElidedLabel, EmojiTile, ShadowPainter, SoftShadow,
                                      TranscriptView, emoji_font, make_menu, open_path, run_bg,
                                      sep_label, short_duration, when_text)

logger = logging.getLogger("transcribe")

TITLE = "Convert audio to text"
DROP_TITLE = "Drop audio or video here"
DROP_FORMATS = "MP3, WAV, M4A, FLAC or video · any length"
RAIL_TITLE = "Audio to text settings"
MODEL_COMBO_WIDTH = 260                  # room for a real model name and its chips
RAIL_FOOT = "On this computer. Nothing is uploaded."
RAIL_FOOT_CLOUD = "Sent to Gusan Cloud to transcribe."
RAIL_FOOT_OWN = "Sent to {name} with your key."
PRIVACY_HINT = "Privacy mode keeps it on this computer."    # as on Smart Actions
UPSELL_SPEECH = engine_choice.UPSELL_SPEECH                 # "Fast cloud transcription"
FILES_TITLE = "Your files"
EMPTY_TEXT = "Files you convert show up here."
NOT_READY = "Audio to text isn't ready yet. Try again in a moment."
NO_AUDIO_DROP = "Drop an audio or video file."
OPEN_FAILED = "This transcript couldn't be opened."
DELETE_ASK = "Delete this transcript? The audio file stays."
WORD_MISSING = "The Word file was moved or deleted."

K_MODEL, K_LANG, K_SPK, K_TS, K_WORD = ("file_model", "file_language", "file_speakers",
                                        "file_timestamps", "file_save_word")
RAIL_KEYS = (K_MODEL, K_LANG, K_SPK, K_TS, K_WORD)
# Other settings that change the Accuracy list or what a pick runs.
MODEL_WATCH = ("privacy_mode", "google_api_key", "mistral_api_key", "managed_provider",
               "google_stt_model", "mistral_stt_model", "admin_tier_override")
AUTO = "auto"
AUTO_PLAIN = "Automatic"
LIST_STEP = 50                      # rows shown, then "Show more"

# The drop zone's own values from the mockup: blue-l at 45 % (dashed edge), blue-100 at 40 % (fill).
_DROP_EDGE_ALPHA = 0.45
_DROP_FILL = (219, 234, 254)
_DROP_FILL_ALPHA = 0.40


def _cfg(app):
    cfg = getattr(app, "cfg", None)
    return cfg if isinstance(cfg, dict) else {}


def _is_pro(app):
    try:
        return bool(app.is_pro())
    except Exception:
        return False


def _signed_in(app):
    value = getattr(getattr(app, "auth", None), "is_authenticated", None)
    return bool(value) if value is not None else None


class MetaLine(QLabel):
    """A row's meta line, '1 h 40 min · Today 09:12': muted bits, faint dots 6 px
    apart (the mockup's), and an ellipsis when the row is narrow - never clipped
    mid-word. text() is always the whole line (tests, screen readers)."""

    GAP = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        theme.set_role(self, "muted")
        self._parts = []
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_parts(self, parts):
        self._parts = [str(p) for p in parts if p]
        super().setText(" · ".join(self._parts))
        self.setAccessibleName(self.text())
        self.updateGeometry()
        self.update()

    def setText(self, text):
        self.set_parts([text])

    def sizeHint(self):
        fm = self.fontMetrics()
        w = sum(fm.horizontalAdvance(p) for p in self._parts)
        w += max(0, len(self._parts) - 1) * (2 * self.GAP + fm.horizontalAdvance("·"))
        return QSize(w, super().sizeHint().height())

    def minimumSizeHint(self):
        return QSize(12, super().minimumSizeHint().height())

    def paintEvent(self, _e):
        p = QPainter(self)
        fm = self.fontMetrics()
        r = self.contentsRect()
        right = r.right() + 1
        x = r.left()
        ink = self.palette().color(self.foregroundRole())
        faint = theme.qcolor("faint")
        flags = int(Qt.AlignLeft | Qt.AlignVCenter)
        cut = False
        for i, part in enumerate(self._parts):
            if i:
                dot = fm.horizontalAdvance("·")
                if x + 2 * self.GAP + dot >= right:
                    cut = True
                    break
                x += self.GAP
                p.setPen(faint)
                p.drawText(QRect(x, r.top(), dot, r.height()), flags, "·")
                x += dot + self.GAP
            full = fm.horizontalAdvance(part)
            shown = part if x + full <= right else fm.elidedText(part, Qt.ElideRight, right - x)
            p.setPen(ink)
            p.drawText(QRect(x, r.top(), right - x, r.height()), flags, shown)
            if shown != part:
                cut = True
                break
            x += full
        p.end()
        self.setToolTip(self.text() if cut else "")


class _LibraryContent(QWidget):
    """The library's scroll content: paints the soft shadows of the job card and
    the "Your files" card (SoftShadow - no blur on every hover or scroll step)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("gusan", "scrollContent")
        self.shadows = ShadowPainter(self)

    def paintEvent(self, _e):
        p = QPainter(self)
        self.shadows.paint(p)
        p.end()


def _model_text(item):
    """What a file card says it was (or is being) converted with: the model's real name and
    where it ran ("Whisper Small · Local", "Gemini 2.5 Flash · Cloud") - the same words as the
    Accuracy picker."""
    name = item.get("model_name") or ""
    if not name:
        return ""
    try:
        text = engine_choice.model_tagged(item.get("model"))
    except Exception:
        text = ""
    return text if text.startswith(name) else name


def stage_text(item):
    """'Transcribing · 42%' for the running card."""
    stage = item.get("stage") or ""
    if stage in ("", file_jobs.STAGE_WAITING, file_jobs.STAGE_STARTING):
        return "Starting"
    if stage == file_jobs.STAGE_STOPPING:
        return "Stopping"
    return "%s · %d%%" % (stage, int(item.get("pct") or 0))


# ── drop zone ─────────────────────────────────────────────────────────────────
class DropZone(QFrame):
    """The big dashed target: 🎧 → 📝, the title, the formats line and Choose a file."""

    ART_MIN_WIDTH = 600              # narrower than this, the two circles step aside

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = False
        self.setAccessibleName(DROP_TITLE)
        self.setMinimumSize(260, 220)          # explicit: the art hides when narrow
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(30, 26, 30, 26)
        lay.setSpacing(30)

        # The circles are the zone's own children (not a box of their own) so
        # their soft shadows aren't clipped by a tight parent.
        art = QHBoxLayout()
        art.setContentsMargins(0, 0, 0, 0)
        art.setSpacing(12)
        arrow = label("", "body", self)
        arrow.setPixmap(icons.pixmap("arrow_r", 26, "blue-l", sw=2.4))
        arrow.setAccessibleName("to")
        self.art = [self._circle(HEADPHONES, 54, "Audio"), arrow, self._circle(MEMO, 50, "Text")]
        for w in self.art:
            art.addWidget(w, 0, Qt.AlignVCenter)
        lay.addLayout(art, 0)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        col.addStretch(1)                      # centred by stretches, so the title can wrap
        self.title = label(DROP_TITLE, "title", self, wrap=True)    # wraps in a narrow window
        self.title.setFont(theme.font(24, 700, display=True, letter_spacing=-0.24))
        self.title.setMinimumHeight(30)
        col.addWidget(self.title)
        self.formats = label(DROP_FORMATS, "muted", self, wrap=True)
        self.formats.setFont(theme.font(13))
        self.formats.setMinimumHeight(20)
        col.addWidget(self.formats)
        col.addSpacing(4)                      # gap 6 + margin-top 10
        self.choose_button = Button("Choose a file", "primary", "m", icon="upload", parent=self)
        col.addWidget(self.choose_button, 0, Qt.AlignLeft)
        col.addStretch(1)
        lay.addLayout(col, 1)

    def _circle(self, ch, px, name):
        """A liquid-glass orb (ui/components/glass_orb.py) holding the emoji."""
        return GlassOrb(ch, px, diameter=104, name=name, font=emoji_font(px), parent=self)

    def set_active(self, on):
        """Highlight while files are dragged over the page (the orbs glow too)."""
        if bool(on) != self._active:
            self._active = bool(on)
            for w in self.art:
                if isinstance(w, GlassOrb):
                    w.set_active(self._active)
            self.update()

    def is_active(self):
        return self._active

    def art_visible(self):
        return not self.art[0].isHidden()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        wide = self.width() >= self.ART_MIN_WIDTH
        for w in self.art:
            w.setVisible(wide)
        # Wide: one line, 30 px (CSS line-height 30). Narrow: the title may wrap.
        self.title.setWordWrap(not wide)
        if wide:
            self.title.setFixedHeight(30)
        else:
            self.title.setMinimumHeight(30)
            self.title.setMaximumHeight(16777215)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        fill = QColor(*_DROP_FILL)
        fill.setAlphaF(0.85 if self._active else _DROP_FILL_ALPHA)
        edge = theme.qcolor("blue-l")
        edge.setAlphaF(1.0 if self._active else _DROP_EDGE_ALPHA)
        pen = QPen(edge)
        pen.setWidthF(2.0)
        pen.setStyle(Qt.CustomDashLine)
        pen.setDashPattern([3.0, 2.0])
        p.setPen(pen)
        p.setBrush(fill)
        p.drawRoundedRect(r, 18, 18)
        p.end()


# ── an inline message (refusals, errors) ──────────────────────────────────────
class Notice(WhiteCard):
    def __init__(self, parent=None):
        super().__init__(parent, pad=(14, 10), gap=0)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.icon = label("", "body", self)
        row.addWidget(self.icon, 0, Qt.AlignTop)
        self.text = label("", "body", self, wrap=True)
        self.text.setFont(theme.font(13.5))
        row.addWidget(self.text, 1)
        self.close_button = IconButton("x", "Dismiss", size=26, icon_size=14, parent=self)
        self.close_button.clicked.connect(self.hide)
        row.addWidget(self.close_button, 0, Qt.AlignTop)
        self.add(row)
        self.tone = "info"
        self.hide()

    def show_message(self, text, tone="info"):
        self.tone = tone
        err = tone == "error"
        self.icon.setPixmap(icons.pixmap("alert" if err else "info", 18, "red-tx" if err else "muted"))
        theme.set_role(self.text, "error" if err else "body")
        self.text.setFont(theme.font(13.5))
        self.text.setText(text)
        self.setAccessibleName(text)
        self.show()

    def message(self):
        return self.text.text() if self.isVisibleTo(self.parentWidget()) else ""


# ── the running job ───────────────────────────────────────────────────────────
class JobCard(WhiteCard):
    """Running: name, Ready in about N min, the bar, 'Transcribing · 42% · 24 min ·
    Whisper Small', Cancel. Then Ready · Open, or the failure and Try again."""

    def __init__(self, parent=None):
        # Its shadow is painted by the page (SoftShadow): progress updates never re-blur it.
        super().__init__(parent, pad=(16, 14), gap=10)
        self.setStyleSheet('QFrame[gusan="whiteCard"] { border-radius: 14px; }')
        self.state = None
        self.item = None
        row = QHBoxLayout()
        row.setSpacing(14)
        self.tile = EmojiTile(KIND_EMOJI["voice"], KIND_LABEL["voice"], size=44, emoji_px=22,
                              bg="blue-sub", radius=11, parent=self)
        row.addWidget(self.tile, 0, Qt.AlignVCenter)

        mid = QVBoxLayout()
        mid.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.name = ElidedLabel("", "h3", self)
        self.name.setFont(theme.font(14, 700))
        self.name.setFixedHeight(20)
        top.addWidget(self.name)
        top.addStretch(1)
        self.eta = ElidedLabel("", "muted", self, min_width=12)
        self.eta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.eta.setFixedHeight(20)
        top.addWidget(self.eta)
        mid.addLayout(top)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setAccessibleName("Progress")
        mid.addWidget(self.bar)
        meta = QHBoxLayout()
        meta.setSpacing(6)
        self.ready = label("Ready", "label", self)
        self.ready.setFont(theme.font(12.5, 600))
        self.ready.setStyleSheet("color: %s;" % theme.qss_color("ok-tx"))
        # Shrinkable (they end in '…'), so a narrow window never pushes the rail off-screen.
        self.stage = ElidedLabel("", "body", self, min_width=12)
        self.stage.setFont(theme.font(12.5, 600))
        self.length = ElidedLabel("", "muted", self, min_width=12)
        self.model = ElidedLabel("", "model", self, min_width=12)
        self.model.setFont(theme.font(12.5))
        self.waiting = ElidedLabel("", "muted", self, min_width=12)
        self._meta = [self.ready, self.stage, self.length, self.model, self.waiting]
        for w in self._meta:
            w.setFixedHeight(20)
        self._seps = []
        for i, w in enumerate(self._meta):
            if i:
                s = sep_label(self)
                self._seps.append(s)
                meta.addWidget(s)
            meta.addWidget(w)
        meta.addStretch(1)
        mid.addLayout(meta)
        self.error = label("", "error", self, wrap=True)
        mid.addWidget(self.error)
        row.addLayout(mid, 1)

        self.cancel_button = Button("Cancel", "secondary", "s", parent=self)
        self.open_button = Button("Open", "primary", "s", parent=self)
        self.retry_button = Button("Try again", "secondary", "s", parent=self)
        self.close_button = IconButton("x", "Dismiss", size=30, icon_size=15, parent=self)
        for b in (self.cancel_button, self.open_button, self.retry_button, self.close_button):
            row.addWidget(b, 0, Qt.AlignVCenter)
        self.add(row)
        self.hide()

    COMPACT_WIDTH = 560        # narrower: only the name, the bar and the stage (the rest in the tooltip)

    def _set_meta(self, eta="", **texts):
        """texts: ready/stage/length/model/waiting -> str ('' hides it)."""
        self._texts = dict(texts, eta=eta)
        self._apply_meta()

    def _apply_meta(self):
        texts = getattr(self, "_texts", {})
        compact = self.width() < self.COMPACT_WIDTH
        self.eta.setText(texts.get("eta", ""))
        self.eta.setVisible(bool(texts.get("eta")) and not compact)
        vis = []
        for name, w in zip(("ready", "stage", "length", "model", "waiting"), self._meta):
            t = texts.get(name, "")
            if name != "ready":
                w.setText(t)
            on = bool(t) and not (compact and name in ("length", "model", "waiting"))
            w.setVisible(on)
            vis.append(on)
        for i, s in enumerate(self._seps):
            s.setVisible(any(vis[:i + 1]) and vis[i + 1])
        extra = [texts.get(k, "") for k in ("eta", "length", "model", "waiting")]
        self.setToolTip(" · ".join(t for t in extra if t) if compact else "")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if (e.oldSize().width() < self.COMPACT_WIDTH) != (self.width() < self.COMPACT_WIDTH):
            self._apply_meta()

    def meta_texts(self):
        """The visible bits of the card (tests, accessibility)."""
        out = [self.name.text()]
        if self.eta.isVisibleTo(self):
            out.append(self.eta.text())
        out += [w.text() for w in self._meta if w.isVisibleTo(self)]
        return out

    def _common(self, item):
        self.item = item
        kind = item.get("kind") or "voice"
        self.tile.set_emoji(KIND_EMOJI.get(kind, KIND_EMOJI["voice"]), KIND_LABEL.get(kind, "Voice recording"))
        self.name.setText(item.get("name") or os.path.basename(str(item.get("path") or "")))

    def _buttons(self, *shown):
        for b in (self.cancel_button, self.open_button, self.retry_button, self.close_button):
            b.setVisible(b in shown)

    def show(self):
        was_hidden = self.isHidden()
        super().show()
        parent = self.parentWidget()
        if was_hidden and parent is not None and parent.layout() is not None:
            parent.layout().activate()          # real width now, so compact mode is right at once
            self._apply_meta()

    def show_running(self, item, waiting=0):
        self._common(item)
        self.state = "running"
        stopping = item.get("stage") == file_jobs.STAGE_STOPPING
        self.bar.setValue(max(0, min(100, int(item.get("pct") or 0))))
        self.bar.show()
        stage = stage_text(item)
        model = _model_text(item)
        if model and (item.get("model_name") or "") in stage:   # "Downloading Whisper Small · 12%": named once
            model = ""
        self._set_meta(eta="" if stopping else (item.get("eta") or ""),
                       stage=stage, length=short_duration(item.get("duration")), model=model,
                       waiting=("%d more waiting" % waiting) if waiting else "")
        self.error.hide()
        self.cancel_button.setEnabled(not stopping)
        self.cancel_button.setAccessibleName("Cancel " + self.name.text())
        self._buttons(self.cancel_button)
        self.setAccessibleName("Converting " + self.name.text())
        self.show()

    def show_done(self, item):
        self._common(item)
        self.state = "done"
        self.bar.hide()
        self._set_meta(ready="Ready", length=short_duration(item.get("duration")),
                       model=_model_text(item))
        problem = item.get("save_error") or ""
        self.error.setText(problem)
        self.error.setVisible(bool(problem))
        self.open_button.setAccessibleName("Open " + self.name.text())
        self._buttons(*([self.open_button] if item.get("transcript_id") else []), self.close_button)
        self.setAccessibleName(self.name.text() + " is ready")
        self.show()

    def show_failed(self, item, message):
        self._common(item)
        self.state = "failed"
        self.bar.hide()
        self._set_meta()
        self.error.setText(message or file_jobs.MSG_ERROR)
        self.error.show()
        self.retry_button.setAccessibleName("Try again with " + self.name.text())
        self._buttons(self.retry_button, self.close_button)
        self.setAccessibleName(self.error.text())
        self.show()

    def clear(self):
        self.state = None
        self.item = None
        self.hide()


# ── Your files ────────────────────────────────────────────────────────────────
class FileRow(QFrame):
    """One transcript: emoji by type, name, 'length · when', Open and ⋯."""

    def __init__(self, page, summary, parent=None):
        super().__init__(parent)
        self.page = page
        self.summary = dict(summary)
        self.tid = summary.get("id")
        self.setProperty("gusan", "row")
        self.setProperty("last", False)
        self._editing = False
        title = summary.get("title") or summary.get("source_name") or "Transcript"
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 11, 0, 11)
        lay.setSpacing(12)
        kind = summary.get("kind") or "voice"
        self.tile = EmojiTile(KIND_EMOJI.get(kind, KIND_EMOJI["voice"]), KIND_LABEL.get(kind, "Voice recording"),
                              size=40, emoji_px=21, bg="bg2", radius=11, parent=self)
        lay.addWidget(self.tile, 0, Qt.AlignVCenter)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        self.name = ElidedLabel(title, "h3", self)
        col.addWidget(self.name)
        self.edit = QLineEdit(title, self)
        self.edit.setFont(theme.font(14, 600))
        self.edit.setFixedHeight(30)
        self.edit.setAccessibleName("New name for " + title)
        self.edit.setMaxLength(200)
        self.edit.editingFinished.connect(self._commit_rename)
        self.edit.installEventFilter(self)
        self.edit.hide()
        col.addWidget(self.edit)
        self.meta = MetaLine(self)                 # ends in '…' when the row is narrow
        self._meta_parts = [short_duration(summary.get("duration_sec")), when_text(summary.get("created"))]
        self.meta.set_parts(self._meta_parts)
        col.addWidget(self.meta)
        lay.addLayout(col, 1)

        right = QHBoxLayout()
        right.setSpacing(4)
        self.open_button = Button("Open", "secondary", "s", parent=self)
        self.open_button.setAccessibleName("Open " + title)
        self.open_button.clicked.connect(lambda _=False: self.page.open_transcript(self.tid, self.summary))
        right.addWidget(self.open_button)
        self.more_button = IconButton("more", "More for " + title, size=30, icon_size=16, parent=self)
        self.more_button.clicked.connect(self._show_menu)
        right.addWidget(self.more_button)
        # A trash icon while the pointer is on the row: the same Delete / Keep question the ⋯ menu asks,
        # right here in the row (ui/components/row_actions.py).
        self.trash = row_actions.attach_trash(self, right, title, self.ask_delete)
        self.delete_button = Button("Delete", "danger", "s", parent=self)
        self.delete_button.setAccessibleName("Delete " + title)
        self.delete_button.clicked.connect(self._delete_now)
        right.addWidget(self.delete_button)
        self.keep_button = Button("Keep", "secondary", "s", parent=self)
        self.keep_button.setAccessibleName("Keep " + title)
        self.keep_button.clicked.connect(self._end_confirm)
        right.addWidget(self.keep_button)
        self.delete_button.hide()
        self.keep_button.hide()
        lay.addLayout(right)
        self.setAccessibleName(title)

    def title(self):
        return self.name.text()

    # ⋯ menu
    def build_menu(self):
        m = make_menu(self)
        m.addAction("Rename", self.start_rename)
        word = self.summary.get("word_path") or ""
        if word:
            m.addAction("Open Word file", self.open_word)
        m.addAction("Show in folder", self.show_in_folder)
        m.addSeparator()
        m.addAction("Delete", self.ask_delete)
        return m

    def _show_menu(self):
        self._menu = self.build_menu()
        self._menu.popup(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))

    # rename (inline)
    def start_rename(self):
        self._editing = True
        self.edit.setText(self.name.text())
        self.name.hide()
        self.edit.show()
        self.edit.setFocus(Qt.OtherFocusReason)
        self.edit.selectAll()

    def is_renaming(self):
        return self._editing

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() == ev.Type.KeyPress and ev.key() == Qt.Key_Escape:
            self.cancel_rename()
            return True
        return False

    def cancel_rename(self):
        self._editing = False
        self._end_rename()
        self.page.files.flush()

    def _end_rename(self):
        if self.edit.hasFocus():
            self.more_button.setFocus(Qt.OtherFocusReason)    # keep keyboard users in the row
        self.edit.hide()
        self.name.show()

    def _commit_rename(self):
        """Enter, or focus leaving the field: save the new name (blank = no change)."""
        if not self._editing:
            return
        self._editing = False
        new = " ".join(self.edit.text().split())
        self._end_rename()
        if new and new != self.name.text():
            self.name.setText(new)
            self.summary["title"] = new
            self.page.rename(self.tid, new)          # refreshes the list when saved
        else:
            self.page.files.flush()

    # delete (inline confirm)
    def ask_delete(self):
        self.meta.setText(DELETE_ASK)
        for b in (self.open_button, self.more_button):
            b.hide()
        self.trash.reveal.enabled = False                   # the question has its own buttons
        self.trash.reveal.refresh(animate=False)
        self.delete_button.show()
        self.keep_button.show()
        self.keep_button.setFocus(Qt.OtherFocusReason)      # the safe choice; keyboard stays in the row

    def _end_confirm(self):
        refocus = self.keep_button.hasFocus() or self.delete_button.hasFocus()
        self.meta.set_parts(self._meta_parts)
        self.delete_button.hide()
        self.keep_button.hide()
        self.open_button.show()
        self.more_button.show()
        self.trash.reveal.enabled = True
        self.trash.reveal.refresh()
        if refocus:
            self.more_button.setFocus(Qt.OtherFocusReason)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.keep_button.isVisibleTo(self):
            self._end_confirm()                 # Esc = Keep
            return
        super().keyPressEvent(e)

    def _delete_now(self):
        self.hide()
        self.page.delete(self.tid)

    def open_word(self):
        word = self.summary.get("word_path") or ""
        if word and os.path.isfile(word):
            open_path(word)
        else:
            self.page.notice.show_message(WORD_MISSING, "error")

    def folder(self):
        """Where 'Show in folder' goes: the Word copy, the audio, else the transcript."""
        for p in (self.summary.get("word_path"), self.summary.get("source_path")):
            if p and os.path.isfile(p):
                return os.path.dirname(p)
        return self.summary.get("dir") or ""

    def show_in_folder(self):
        open_path(self.folder())


class FilesSection(QWidget):
    """'Your files' and its glass card of rows (newest first, LIST_STEP at a time)."""

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self._limit = LIST_STEP
        self._summaries = []
        self.rows = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.heading = label(FILES_TITLE, "h2", self)
        self.heading.setFixedHeight(24)
        lay.addWidget(self.heading)
        self.card = Card(self, pad=(18, 2), gap=0, shadow=False)     # shadow: the page paints it
        lay.addWidget(self.card)
        self.empty = label(EMPTY_TEXT, "muted", self.card)
        self.empty.setFont(theme.font(13.5))
        self.empty.setMinimumHeight(52)
        self.empty.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.card.add(self.empty)
        self.more_button = Button("Show more", "link", "s", parent=self)
        self.more_button.clicked.connect(self._show_more)
        self.more_button.hide()
        lay.addWidget(self.more_button, 0, Qt.AlignLeft)
        self.hide()                                  # until the first list arrives

    def set_rows(self, summaries):
        if any(r.is_renaming() for r in self.rows):
            self._pending = list(summaries or [])      # don't pull the field from under the user
            return
        self._pending = None
        self._summaries = list(summaries or [])
        for r in self.rows:
            r.hide()
            r.deleteLater()
        self.rows = []
        shown = self._summaries[:self._limit]
        for s in shown:
            row = FileRow(self.page, s, self.card)
            self.card.add(row)
            self.rows.append(row)
        if self.rows:
            self.rows[-1].setProperty("last", True)
            theme.repolish(self.rows[-1], children=False)
        self.empty.setVisible(not self.rows)
        self.more_button.setVisible(len(self._summaries) > len(shown))
        self._chain_focus()
        self.show()

    def _chain_focus(self):
        """Tab goes through the rows right after the job card (rows are built
        later than the rail, so by default they would come last)."""
        prev = self.page.job_card.close_button
        for r in self.rows:
            for w in (r.edit, r.open_button, r.more_button, r.delete_button, r.keep_button):
                QWidget.setTabOrder(prev, w)
                prev = w
        QWidget.setTabOrder(prev, self.more_button)

    def flush(self):
        """Apply a list that arrived while a row was being renamed."""
        if getattr(self, "_pending", None) is not None:
            self.set_rows(self._pending)

    def _show_more(self):
        self._limit += LIST_STEP
        self.set_rows(self._summaries)

    def row(self, tid):
        return next((r for r in self.rows if r.tid == tid), None)


# ── the page ──────────────────────────────────────────────────────────────────
class AudioToTextPage(QWidget):
    wants_scroll = False             # the library scrolls itself; the transcript has its own paper

    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.setAcceptDrops(True)
        self.setAccessibleName(TITLE)
        self._list_gen = 0
        self._open_gen = 0
        self._summaries = []
        self._outcome = None                 # ("done" | "failed", item, message) after a job ends
        self._fj_seen = None
        self._dl_seen = None
        self._syncing = False
        self._last_dir = ""
        self.ask_open_paths = self._ask_open_paths      # replaceable (tests, Wave 4)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.stack = QStackedWidget(self)
        root.addWidget(self.stack)

        # library
        self.library = QScrollArea(self)
        self.library.setProperty("gusan", "scroll")
        self.library.setWidgetResizable(True)
        self.library.setFrameShape(QFrame.NoFrame)
        self.library.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.library.viewport().setAutoFillBackground(False)
        content = _LibraryContent()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.header = PageHeader(TITLE, parent=content)
        cl.addWidget(self.header)
        body = QHBoxLayout()
        body.setContentsMargins(32, 0, 32, 28)
        body.setSpacing(24)
        main = QVBoxLayout()
        main.setSpacing(16)
        self.drop_zone = DropZone(content)
        self.drop_zone.choose_button.clicked.connect(lambda _=False: self.choose_files())
        main.addWidget(self.drop_zone)
        self.notice = Notice(content)
        main.addWidget(self.notice)
        self.job_card = JobCard(content)
        self.job_card.cancel_button.clicked.connect(self.cancel_job)
        self.job_card.open_button.clicked.connect(self._open_finished)
        self.job_card.retry_button.clicked.connect(self._retry_failed)
        self.job_card.close_button.clicked.connect(self._dismiss_outcome)
        main.addWidget(self.job_card)
        self.files = FilesSection(self, content)
        main.addWidget(self.files)
        main.addStretch(1)
        body.addLayout(main, 1)
        self.rail = self._build_rail(content)
        body.addWidget(self.rail, 0, Qt.AlignTop)
        cl.addLayout(body, 1)
        content.shadows.add(self.job_card, SoftShadow("card", radius=14))
        content.shadows.add(self.files.card, SoftShadow("card", radius=16))
        self.library.setWidget(content)
        self.stack.addWidget(self.library)

        # one transcript open
        self.transcript = TranscriptView(main_app, self)
        self.transcript.back.connect(self._back_to_library)
        self.stack.addWidget(self.transcript)

        self._connect_services()
        self._sync_settings()
        self._sync_job_card()
        self.refresh_list()

    # ── services ──
    def _fj(self):
        return getattr(self.app, "file_jobs", None)

    def _connect_services(self):
        fj = self._fj()
        if fj is not None and fj is not self._fj_seen:
            self._fj_seen = fj
            for name, slot in (("queued", self._on_job_event), ("started", self._on_job_started),
                               ("progress", self._on_job_event), ("finished", self._on_job_finished),
                               ("failed", self._on_job_failed), ("cancelled", self._on_job_cancelled),
                               ("running_changed", self._on_job_event)):
                sig = getattr(fj, name, None)
                if sig is not None and hasattr(sig, "connect"):
                    try:
                        sig.connect(slot)
                    except Exception:
                        logger.debug("file_jobs.%s: no connect", name, exc_info=True)
        dl = getattr(self.app, "downloads", None)
        if dl is not None and dl is not self._dl_seen:
            self._dl_seen = dl
            for name in ("state_changed", "scanned", "hardware_ready", "progress", "finished"):
                sig = getattr(dl, name, None)
                if sig is not None and hasattr(sig, "connect"):
                    try:
                        sig.connect(self._update_model_line)
                    except Exception:
                        logger.debug("downloads.%s: no connect", name, exc_info=True)
            catalog = getattr(dl, "catalog_changed", None)
            if catalog is not None and hasattr(catalog, "connect"):
                try:
                    catalog.connect(self._sync_settings)      # the Armenian model became available (or went)
                except Exception:
                    logger.debug("downloads.catalog_changed: no connect", exc_info=True)

    # ── shell hooks ──
    def on_open(self, **opts):
        self._connect_services()
        hy_model.refresh_catalog(self.app)          # a worker: once a day at most, never in Privacy mode
        tid = opts.get("transcript_id")
        if tid:
            self.open_transcript(tid)
        else:
            self.show_library()
        paths = opts.get("paths") or opts.get("path")
        if paths:
            self.add_files([paths] if isinstance(paths, (str, os.PathLike)) else list(paths))
        self._sync_job_card()
        self._update_model_line()
        self.refresh_list()

    def on_setting_changed(self, key):
        if key in RAIL_KEYS or key in MODEL_WATCH:
            self._sync_settings()

    def refresh_account(self):
        """Pro started or ended: the PRO marks in Accuracy follow, and a Gusan
        Cloud pick that can't run now reads Automatic."""
        self._sync_settings()

    # ── views ──
    def current_view(self):
        return "transcript" if self.stack.currentWidget() is self.transcript else "library"

    def show_library(self):
        self.stack.setCurrentWidget(self.library)

    def _back_to_library(self):
        """Back from a transcript: the list again, refreshed (a Save Word there
        adds 'Open Word file' to that row's menu)."""
        self.show_library()
        self.refresh_list()

    def open_transcript(self, tid, summary=None):
        """Show one transcript (loaded on a worker)."""
        if not tid:
            return
        self._open_gen += 1
        gen = self._open_gen
        if summary is None:
            summary = next((s for s in self._summaries if s.get("id") == tid), None)
        self.transcript.show_loading(summary or {})
        self.stack.setCurrentWidget(self.transcript)
        run_bg(self, lambda: transcript_store.load_transcript(tid),
               lambda doc, err: self._on_doc(gen, doc, err))

    def _on_doc(self, gen, doc, err):
        if gen != self._open_gen:
            return
        if err is not None or not doc:
            self.show_library()
            self.notice.show_message(OPEN_FAILED, "error")
            self.refresh_list()
            return
        self.transcript.show_doc(doc)

    # ── the list ──
    def refresh_list(self):
        self._list_gen += 1
        gen = self._list_gen
        run_bg(self, transcript_store.list_transcripts,
               lambda rows, err: self._on_list(gen, rows, err))

    def _on_list(self, gen, rows, err):
        if gen != self._list_gen:
            return
        if err is not None:
            rows = []
        self._summaries = list(rows or [])
        self.files.set_rows(self._summaries)

    def rename(self, tid, title):
        run_bg(self, lambda: transcript_store.rename_transcript(tid, title),
               lambda ok, err: self._after_change(ok, err, "That name couldn't be saved."))

    def delete(self, tid):
        run_bg(self, lambda: transcript_store.delete_transcript(tid),
               lambda ok, err: self._after_change(ok, err, "This transcript couldn't be deleted."))

    def _after_change(self, ok, err, failed):
        if err is not None or not ok:
            self.notice.show_message(failed, "error")
        self.refresh_list()

    # ── adding files ──
    def _ask_open_paths(self):
        exts = " ".join("*%s" % e for e in sorted(file_jobs.AUDIO_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose audio or video files", self._last_dir,
            "Audio and video (%s);;All files (*)" % exts)
        if paths:
            self._last_dir = os.path.dirname(paths[0])
        return paths

    def choose_files(self):
        paths = self.ask_open_paths()
        if paths:
            self.add_files(paths)

    def add_files(self, paths):
        """Queue files on app.file_jobs; a refusal shows inline."""
        paths = [os.fspath(p) for p in (paths or []) if p]
        if not paths:
            return []
        self.show_library()
        fj = self._fj()
        if fj is None:
            self.notice.show_message(NOT_READY, "error")
            return []
        self._connect_services()
        try:
            items, msg = fj.enqueue(paths)
        except Exception:
            logger.warning("Audio to text: enqueue failed", exc_info=True)
            items, msg = [], file_jobs.MSG_ERROR
        if items:
            self._outcome = None
        if msg:
            self._show_refusal(msg)
        else:
            self.notice.hide()
        self._sync_job_card()
        return items

    def _show_refusal(self, msg):
        """Busy (a recording, another job) is calm info; anything else is an error."""
        busy = msg in (file_jobs.MSG_RECORDING, file_jobs.MSG_OTHER_JOB)
        self.notice.show_message(msg, "info" if busy else "error")

    def _drop_paths(self, mime):
        if mime is None or not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, e):
        if any(file_jobs.is_audio_file(p) for p in self._drop_paths(e.mimeData())):
            e.acceptProposedAction()
            self.drop_zone.set_active(True)
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self.drop_zone.is_active():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self.drop_zone.set_active(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self.drop_zone.set_active(False)
        paths = self._drop_paths(e.mimeData())
        audio = [p for p in paths if file_jobs.is_audio_file(p)]
        if audio:
            e.acceptProposedAction()
            self.add_files(audio)
        elif paths:
            self.notice.show_message(NO_AUDIO_DROP, "error")

    # ── the job card ──
    def _on_job_event(self, *_a):
        self._sync_job_card()

    def _on_job_started(self, *_a):
        if self._outcome and self._outcome[0] == "failed":
            # The next queued file takes the card: keep the failure readable above it.
            _state, item, msg = self._outcome
            name = (item or {}).get("name") or ""
            self.notice.show_message("%s: %s" % (name, msg) if name else msg, "error")
        self._outcome = None
        self._sync_job_card()

    def _on_job_finished(self, item):
        self._outcome = ("done", item or {}, "")
        self._sync_job_card()
        self.refresh_list()

    def _on_job_failed(self, item, message=""):
        item = item or {}
        self._outcome = ("failed", item, message or item.get("error") or file_jobs.MSG_ERROR)
        self._sync_job_card()

    def _on_job_cancelled(self, item):
        if self._outcome and self._outcome[1] is item:
            self._outcome = None
        self._sync_job_card()

    def _sync_job_card(self):
        fj = self._fj()
        cur, pending = None, []
        if fj is not None:
            try:
                cur = fj.current() if callable(getattr(fj, "current", None)) else None
                pending = list(fj.pending() or []) if callable(getattr(fj, "pending", None)) else []
            except Exception:
                logger.debug("file_jobs state unavailable", exc_info=True)
        if cur:
            self.job_card.show_running(cur, len(pending))
        elif self._outcome:
            state, item, msg = self._outcome
            if state == "done":
                self.job_card.show_done(item)
            else:
                self.job_card.show_failed(item, msg)
        else:
            self.job_card.clear()

    def cancel_job(self):
        fj = self._fj()
        item = self.job_card.item
        if fj is None or not item:
            return
        try:
            fj.cancel(item.get("id"))
        except Exception:
            logger.warning("Audio to text: cancel failed", exc_info=True)
        self._sync_job_card()

    def _open_finished(self):
        item = self.job_card.item or {}
        tid = item.get("transcript_id")
        self._outcome = None
        self._sync_job_card()
        if tid:
            self.open_transcript(tid)

    def _retry_failed(self):
        item = self.job_card.item or {}
        path = item.get("path")
        self._outcome = None
        fj = self._fj()
        if not path or fj is None:
            self._sync_job_card()
            return
        try:
            items, msg = fj.enqueue([path], item.get("opts") or None)
        except Exception:
            logger.warning("Audio to text: retry failed", exc_info=True)
            items, msg = [], file_jobs.MSG_ERROR
        if msg:
            self._show_refusal(msg)
        self._sync_job_card()

    def _dismiss_outcome(self):
        self._outcome = None
        self._sync_job_card()

    # ── the rail ──
    def _build_rail(self, parent):
        rail = SettingsRail(RAIL_TITLE, icon="gear", parent=parent)
        acc = QWidget(rail)
        av = QVBoxLayout(acc)
        av.setContentsMargins(0, 0, 0, 0)
        av.setSpacing(3)
        # Automatic / a Whisper model [LOCAL] / Gusan Cloud's model [CLOUD] [PRO] / your own key [CLOUD]:
        # real model names with a chip for where each runs (engine_choice.file_rows).
        self.model_combo = _combo(PlainNameCombo(acc), "Accuracy", width=MODEL_COMBO_WIDTH)
        self._fill_model_combo()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        av.addWidget(self.model_combo)
        self.model_line = label("", "model", acc)
        self.model_line.setContentsMargins(13, 0, 0, 0)
        av.addWidget(self.model_line)
        # While the Armenian model downloads: a way to stop it.
        self.model_cancel = Button(hy_model.CANCEL + " download", "link", "s", parent=acc)
        self.model_cancel.setAccessibleName("Cancel the Armenian model download")
        self.model_cancel.clicked.connect(self._cancel_model_download)
        self.model_cancel.hide()
        av.addWidget(self.model_cancel, 0, Qt.AlignLeft)
        self.model_row = rail.add_row(SettingRow("Accuracy", acc, stacked=True))     # under its name: a real name and its chips need the room

        self.lang_combo = QComboBox(rail)
        for code, name in LANGUAGES:
            self.lang_combo.addItem(name, code)
        _combo(self.lang_combo, "Language", width=150)
        # As wide as its longest name needs: the stylesheet (not applied yet at
        # build time) leaves about width - 76 px for the text (padding, border, chevron).
        fm = self.lang_combo.fontMetrics()
        longest = max(fm.horizontalAdvance(name) for _c, name in LANGUAGES)
        self.lang_combo.setFixedWidth(max(150, longest + 78))
        self.lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        rail.add_row(SettingRow("Language", self.lang_combo))

        self.speakers_toggle = Toggle("Speaker labels", True, rail)
        self.speakers_toggle.toggled.connect(lambda on: self._set(K_SPK, bool(on)))
        rail.add_row(SettingRow("Speaker labels", self.speakers_toggle, hint="Who said what"))
        self.timestamps_toggle = Toggle("Timestamps", True, rail)
        self.timestamps_toggle.toggled.connect(lambda on: self._set(K_TS, bool(on)))
        rail.add_row(SettingRow("Timestamps", self.timestamps_toggle))
        self.word_toggle = Toggle("Save as Word", True, rail)
        self.word_toggle.toggled.connect(lambda on: self._set(K_WORD, bool(on)))
        rail.add_row(SettingRow("Save as Word", self.word_toggle))

        foot = QWidget(rail)
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(8)
        self.rail_foot_icon = label("", "muted", foot)       # shield / cloud / key
        self._foot_icon = "shield"
        self.rail_foot_icon.setPixmap(icons.pixmap("shield", 15, "muted"))
        fl.addWidget(self.rail_foot_icon)
        self.rail_foot = label(RAIL_FOOT, "muted", foot)
        fl.addWidget(self.rail_foot, 1)
        rail.set_foot(foot)
        return rail

    def _model_rows(self):
        """The Accuracy rows (engine_choice.file_rows)."""
        try:
            return engine_choice.file_rows(_cfg(self.app), _is_pro(self.app), self._language(),
                                           _signed_in(self.app))
        except Exception:
            logger.debug("file_rows failed", exc_info=True)
            rows = [{"value": AUTO, "name": AUTO_PLAIN, "title": AUTO_PLAIN, "line": AUTO_PLAIN, "note": "",
                     "tags": (), "pro": False, "group": "", "tip": ""}]
            for mid, i in hardware.MODEL_INFO.items():
                rows.append({"value": mid, "name": i.real, "title": i.real, "line": i.label(), "note": i.plain,
                             "tags": (), "pro": False, "group": "", "tip": ""})
            return rows

    def _fill_model_combo(self, rows=None):
        """The Accuracy list from engine_choice.file_rows: headings, names, chips."""
        rows = self._model_rows() if rows is None else rows
        box = self.model_combo
        box.blockSignals(True)
        box.fill_rows(rows)
        box.blockSignals(False)
        self._model_fill = rows

    def _fill_key(self):
        """What the list's items depend on: its rows (the plan, the Gusan Cloud provider's model
        and the own keys with their models)."""
        return self._model_rows()

    def model_value(self):
        """What really converts a file now, as an Accuracy value: a cloud pick
        that can't run (Privacy mode, Pro ended, the key removed) reads
        Automatic, and so does an unknown value."""
        cfg = _cfg(self.app)
        value = str(self._value(K_MODEL) or AUTO).strip()
        try:
            backend, model = engine_choice.file_session(dict(cfg, **{K_MODEL: value}), _is_pro(self.app))
        except Exception:
            logger.debug("file_session failed", exc_info=True)
            return AUTO
        if backend != "local":
            return value
        return model or AUTO

    def _value(self, key):
        return _cfg(self.app).get(key, file_jobs.FILE_DEFAULTS[key])

    def _sync_settings(self):
        """Controls from app.cfg (FILE_DEFAULTS for missing keys), without writing back."""
        self._syncing = True
        try:
            box = self.model_combo
            if self._fill_key() != getattr(self, "_model_fill", None):
                self._fill_model_combo()            # PRO marks, the cloud model, own keys
            box.setCurrentIndex(max(0, box.findData(self.model_value())))
            privacy = bool(_cfg(self.app).get("privacy_mode"))
            for value in engine_choice.FILE_CLOUD_VALUES:
                i = box.findData(value)
                if i >= 0:
                    box.set_choice_enabled(i, not privacy)     # Privacy mode keeps it on this computer
            self.model_row.set_hint(self._model_note())
            lang = self._value(K_LANG) or "auto"
            i = self.lang_combo.findData(lang)
            if i < 0:
                self.lang_combo.addItem(str(lang).upper(), lang)
                i = self.lang_combo.count() - 1
            self.lang_combo.setCurrentIndex(i)
            self.speakers_toggle.setChecked(bool(self._value(K_SPK)))
            self.timestamps_toggle.setChecked(bool(self._value(K_TS)))
            self.word_toggle.setChecked(bool(self._value(K_WORD)))
        finally:
            self._syncing = False
        self._update_model_line()

    def _set(self, key, value):
        if self._syncing:
            return
        fn = getattr(self.app, "set_setting", None)
        cfg = getattr(self.app, "cfg", None)
        if callable(fn):
            try:
                ok = fn(key, value) is not False
            except Exception:
                logger.warning("set_setting(%s) failed", key, exc_info=True)
                ok = False
        elif isinstance(cfg, dict):
            cfg[key] = value                # no controller pipeline: at least the next job uses it
            ok = True
        else:
            ok = False
        if not ok:
            self._sync_settings()           # refused: show the value that is really set
        self._update_model_line()

    def _on_model_changed(self, _i):
        if self._syncing:
            return
        value = self.model_combo.currentData() or AUTO
        try:
            res = engine_choice.set_file_model(self.app, value)
        except Exception:
            logger.warning("set_file_model failed", exc_info=True)
            res = {"ok": False, "problem": "error", "changes": {}, "upsell": ""}
        if not res.get("ok"):
            self._sync_settings()                   # back to what is really set; nothing saved
            if res.get("problem") == "pro":
                upsell = getattr(self.app, "_pro_upsell", None)
                if callable(upsell):
                    try:
                        upsell(res.get("upsell") or UPSELL_SPEECH)
                    except Exception:
                        logger.warning("_pro_upsell failed", exc_info=True)
            return
        if hy_model.is_hy(value) and hy_model.state(self.app) in ("missing", "failed"):
            # 105 MB: ask first (Audio to text starts the download now rather than at the first file)
            if not hy_model.confirm_download(self, "Audio to text"):
                self._sync_settings()               # back to what is really set
                return
            if not hy_model.start(self.app):
                self._sync_settings()
                return
        self._set(K_MODEL, value)

    def _cancel_model_download(self):
        """Stop the Armenian model's download; a pick of it that is not on this computer would only start
        the download again with the next file, so Automatic is put back."""
        if not hy_model.cancel(self.app):
            return
        if str(self._value(K_MODEL) or "") == hy_model.HY_ID:
            self._set(K_MODEL, AUTO)
        self._update_model_line()

    def _on_lang_changed(self, _i):
        self._set(K_LANG, self.lang_combo.currentData() or "auto")

    def auto_model(self):
        """The model Automatic would use now (file_jobs.pick_auto_model on what the
        download manager knows - no disk scan here), or None when unknown."""
        dl = getattr(self.app, "downloads", None)
        ready_fn = getattr(dl, "ready", None)
        if not callable(ready_fn):
            return None
        try:
            ready = set(ready_fn("whisper") or [])
            snap = getattr(dl, "hardware", None)
            rec = getattr(self.app, "recorder", None)
            on_gpu = False
            if snap is not None:
                on_gpu = hardware.runs_on_gpu(getattr(snap, "gpu_state", None),
                                              getattr(rec, "_cuda_usable", None),
                                              getattr(snap, "platform", None))
            return file_jobs.pick_auto_model(rec, device="cuda" if on_gpu else "cpu",
                                             downloaded=lambda m: m in ready)
        except Exception:
            logger.debug("auto model unknown", exc_info=True)
            return None

    def _language(self):
        return str(self._value(K_LANG) or "auto")

    def _engine(self, value):
        """engine_choice.file_engine for a value with the job's language (Automatic follows the order
        for a language Whisper is poor at), or {}."""
        try:
            return engine_choice.file_engine(_cfg(self.app), value, _is_pro(self.app), self._language(),
                                             _signed_in(self.app))
        except Exception:
            logger.debug("file_engine failed", exc_info=True)
            return {}

    def _cloud_engine(self, value):
        """engine_choice.file_engine for a cloud value (or Automatic that moved to Gusan Cloud for the
        language), or {} (it runs locally)."""
        ro = self._engine(value)
        return ro if ro.get("kind") in ("cloud", "own_key") else {}

    def _model_note(self):
        """The plain hint under the picker: for a file in a language Whisper is poor at that Whisper
        would convert - the same words as the Live Assistant's (in Privacy mode they say why the cloud is
        off and offer the Armenian model, so they come first) - else Privacy mode's, else nothing."""
        note = ""
        try:
            note = engine_choice.file_note(_cfg(self.app), _is_pro(self.app), self._language(),
                                           _signed_in(self.app), self.model_combo.currentData() or AUTO)
        except Exception:
            logger.debug("file_note failed", exc_info=True)
        if note:
            return note
        return PRIVACY_HINT if _cfg(self.app).get("privacy_mode") else ""

    def _update_model_line(self, *_a):
        mid = self.model_combo.currentData() or AUTO
        ro = self._cloud_engine(mid) if (mid in engine_choice.FILE_CLOUD_VALUES or mid == AUTO) else {}
        if ro:
            text = ro.get("tagged") or ro.get("model_name") or ""    # "Best accuracy · Cloud", never a Whisper name
            if ro.get("auto") and ro.get("reason") == "language":
                text += " · for %s" % (speech_langs.local_whisper_poor(self._language(), _cfg(self.app))
                                       or speech_langs.language_name(self._language()) or "this language")
        else:
            if mid == AUTO:
                local = self._engine(AUTO)               # Automatic may be the Armenian model, for an Armenian file
                mid = (local.get("model_id") if hy_model.is_hy(local.get("model_id")) else None) or self.auto_model()
            text = engine_choice.model_tagged(mid) if mid and mid not in engine_choice.FILE_CLOUD_VALUES else ""
            if mid and hy_model.is_hy(mid):
                if hy_model.state(self.app) == "downloading":
                    text = hy_model.progress_line(self.app)
                elif str(self._value(K_MODEL) or "") == hy_model.HY_ID:
                    text += " · Armenian only"           # (any other language of a file: Whisper reads it)
        self.model_line.setText(text)
        self.model_line.setVisible(bool(text))
        self.model_cancel.setVisible(hy_model.can_cancel(self.app))
        self.model_row.set_hint(self._model_note())
        self.model_combo.setAccessibleDescription(text)
        # The footer says where the audio goes.
        if ro.get("kind") == "cloud":
            icon, foot = "cloud", RAIL_FOOT_CLOUD
        elif ro.get("kind") == "own_key":
            icon, foot = "key", RAIL_FOOT_OWN.format(name=ro.get("plain") or "")
        else:
            icon, foot = "shield", RAIL_FOOT
        self.rail_foot.setText(foot)
        if icon != self._foot_icon:
            self._foot_icon = icon
            self.rail_foot_icon.setPixmap(icons.pixmap(icon, 15, "muted"))


def create(main_app):
    return AudioToTextPage(main_app)
