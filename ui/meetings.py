# Classic "Record Meeting" window (1.x look) - a thin view over the meeting
# recorder in meeting_session.py. The recording, the processing pipeline and
# the state live in the app's one MeetingSession (app.meeting_session); this
# window only reads its widgets, calls the session and draws what it reports.
# main.py, Live Assistance and Settings still reach the meeting through
# app.meetings_win, so the 1.9 attribute names are forwarded to the session.

import html as html_mod
import os
import re
import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QTextEdit, QFrame, QMessageBox, QSplitter, QProgressBar,
    QStackedWidget, QFileDialog, QWidget, QApplication
)
from PySide6.QtGui import QFont
import logging
import actions
import action_api
import meeting_session
import meeting_store
import notion_export
import telemetry  # noqa: F401 - tests patch ui.meetings.telemetry
from meeting_session import MeetingProcessingSignal  # noqa: F401 - 1.9 name

logger = logging.getLogger("transcribe")


def ensure_recording_consent(parent, app):
    """One-time notice before the first meeting recording: capturing other
    people's voices may require their consent depending on jurisdiction. The
    user must acknowledge once; we persist the acknowledgement. Returns True
    when recording may proceed."""
    cfg = getattr(app, "cfg", None)
    if cfg is None or cfg.get("meeting_consent_ack"):
        return True
    box = QMessageBox(parent)
    box.setWindowTitle("Before you record")
    box.setIcon(QMessageBox.Information)
    box.setText("You're about to record a meeting - including other people's voices.")
    box.setInformativeText(
        "Depending on your country or state, recording a conversation may "
        "require the consent of all participants. By continuing you confirm "
        "you have any consent required where you are.\n\n"
        "This notice is shown only once."
    )
    ok = box.addButton("I understand - continue", QMessageBox.AcceptRole)
    box.addButton("Cancel", QMessageBox.RejectRole)
    box.exec()
    if box.clickedButton() is ok:
        cfg["meeting_consent_ack"] = True
        try:
            app.save_config()
        except Exception:
            pass
        return True
    return False


def _on_session(name, default=None):
    """A 1.9 window attribute that now lives on the session (main.py, Live
    Assistance and Settings read e.g. ``meetings_win.state`` and
    ``_meeting_dir``). Without a session (a window made with __new__ in
    tests) the value is kept on the window itself."""
    own = "_own" + name

    def get(self):
        session = self.__dict__.get("session")
        if session is None:
            return self.__dict__.get(own, default)
        return getattr(session, name)

    def put(self, value):
        session = self.__dict__.get("session")
        if session is None:
            self.__dict__[own] = value
        else:
            setattr(session, name, value)

    return property(get, put)


class MeetingsWindow(QDialog):
    STATE_IDLE = meeting_session.STATE_IDLE
    STATE_RECORDING = meeting_session.STATE_RECORDING
    STATE_PROCESSING = meeting_session.STATE_PROCESSING
    STATE_DONE = meeting_session.STATE_DONE

    # Result of a background Notion upload: (url, error) - one of them empty.
    sig_notion = Signal(str, str)

    # Forwarded to the session (see _on_session).
    state = _on_session("state", meeting_session.STATE_IDLE)
    _meeting_dir = _on_session("_meeting_dir")
    _meeting_title = _on_session("_meeting_title", "")
    _meeting_attendees = _on_session("_meeting_attendees", "")
    _live_text = _on_session("_live_text", "")
    _live_summary_text = _on_session("_live_summary_text", "")
    _final_notes = _on_session("_final_notes", "")
    _final_transcript = _on_session("_final_transcript", "")
    _user_notes = _on_session("_user_notes", "")
    _resume_dir = _on_session("_resume_dir")
    _session_audio_mode = _on_session("_session_audio_mode", "")
    _transcript_notices = _on_session("_transcript_notices", [])
    _summary_downgraded = _on_session("_summary_downgraded", False)

    # The session's live signals under their 1.9 names, emitted from worker
    # threads (main.py feeds Live Assistance from them).
    @property
    def sig_chunk(self):
        return self.session.sig_chunk

    @property
    def sig_summary(self):
        return self.session.sig_summary

    @property
    def sig_status(self):
        return self.session.sig_status

    def __init__(self, parent=None, main_app=None):
        super().__init__(parent)
        self.app = main_app
        self.session = meeting_session.get_session(main_app)

        self.setWindowTitle("Record Meeting")
        # QDialogs only get a Close button on Windows - add minimize/maximize
        # so a long meeting can be tucked away without the close-prompt dance.
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowMinimizeButtonHint
                            | Qt.WindowMaximizeButtonHint)
        self.setMinimumSize(640, 500)
        self.resize(800, 650)
        self.setSizeGripEnabled(True)

        # Apply global styling
        if self.app and hasattr(self.app, "style_content"):
            self.setStyleSheet(self.app.style_content)

        # Test stubs variables for backwards compatibility
        self._summary_var = ""
        self._transcript_var = ""
        self._shown_phase = meeting_session.STATE_IDLE

        self.sig_notion.connect(self._on_notion_done)
        s = self.session
        s.state_changed.connect(self._on_session_state)
        s.chunk_text.connect(self._append_live_line)
        s.recap.connect(self._set_live_summary)
        s.status.connect(self._set_proc_status)
        s.finished.connect(self._on_session_finished)

        # The REC label ticks only while a meeting records.
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._tick)

        self._build_ui()
        self._populate_audio_devices()
        # Started elsewhere (the Meetings page, Live Assistant) before this
        # window was built: show it.
        if s.phase != meeting_session.STATE_IDLE:
            self._on_session_state(s.phase)

    def showEvent(self, event):
        super().showEvent(event)
        # The window is built once at launch; Settings may have changed the
        # models since. Read-only - showing the window never writes the config.
        self._sync_setup_from_cfg()
        from ui.winfit import settle_on_screen, size_to_screen
        if not getattr(self, "_fit_positioned", False):
            size_to_screen(self, 0.44, 0.64, 680, 520, 900, 820)
        settle_on_screen(self)

    def _build_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(14)

        # ── Stack/Dynamic UI Containers based on State ──
        self.container = QStackedWidget(self)

        # 1. Idle Page: Setup Meeting details
        self.page_idle = QWidget()
        self._build_page_idle()
        self.container.addWidget(self.page_idle)

        # 2. Recording Page: Live notes & transcripts
        self.page_recording = QWidget()
        self._build_page_recording()
        self.container.addWidget(self.page_recording)

        # 3. Processing Page: Loading summaries
        self.page_processing = QWidget()
        self._build_page_processing()
        self.container.addWidget(self.page_processing)

        # 4. Done Page: View final summaries & notes
        self.page_done = QWidget()
        self._build_page_done()
        self.container.addWidget(self.page_done)

        self.main_layout.addWidget(self.container)
        self.container.setCurrentIndex(0)

    # ── Idle View: Inputs & setup ──
    def _build_page_idle(self):
        lay = QVBoxLayout(self.page_idle)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        title = QLabel("Record Meeting", self.page_idle)
        title.setObjectName("titleLabel")
        lay.addWidget(title)

        desc = QLabel("Capture any Zoom, Google Meet, or conference call. Gusan will record system loopback audio and generate beautiful AI-summarized minutes.", self.page_idle)
        desc.setObjectName("subtitleLabel")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        # Title Card
        meta_frame = QFrame(self.page_idle)
        meta_frame.setObjectName("cardFrame")
        m_lay = QVBoxLayout(meta_frame)
        m_lay.setContentsMargins(18, 18, 18, 18)
        m_lay.setSpacing(12)

        m_lay.addWidget(QLabel("Meeting Title", meta_frame))
        self.input_title = QLineEdit(meta_frame)
        self.input_title.setPlaceholderText("Product Sync / Sprint Review")
        m_lay.addWidget(self.input_title)

        m_lay.addWidget(QLabel("Attendees (Comma separated)", meta_frame))
        self.input_attendees = QLineEdit(meta_frame)
        self.input_attendees.setPlaceholderText("Aram Adamyan, Anna Sargsyan, David")
        m_lay.addWidget(self.input_attendees)
        lay.addWidget(meta_frame)

        # Devices Picker Card
        dev_frame = QFrame(self.page_idle)
        dev_frame.setObjectName("cardFrame")
        d_lay = QVBoxLayout(dev_frame)
        d_lay.setContentsMargins(18, 18, 18, 18)
        d_lay.setSpacing(12)

        d_lay.addWidget(QLabel("Meeting Recording Mode", dev_frame))
        from ui.mode_combo import MeetingModeComboBox
        self.combo_device = MeetingModeComboBox(dev_frame)
        # ``activated`` fires only for a user pick, never for the programmatic
        # re-sync from Settings - so only a real choice here is saved.
        self.combo_device.activated.connect(self._on_mode_picked)
        d_lay.addWidget(self.combo_device)

        d_lay.addWidget(QLabel("Spoken Language", dev_frame))
        self.combo_lang = QComboBox(dev_frame)
        lang_names = meeting_session._main_attr("LANG_NAMES")
        if lang_names:
            for val, label in lang_names.items():
                self.combo_lang.addItem(label, val)
        else:
            self.combo_lang.addItem("Auto-detect", "auto")
        d_lay.addWidget(self.combo_lang)

        # Transcription model (local Whisper sizes) - the speech-to-text engine.
        d_lay.addWidget(QLabel("Transcription model", dev_frame))
        self.combo_whisper = QComboBox(dev_frame)
        self.combo_whisper.activated.connect(self._on_whisper_picked)
        d_lay.addWidget(self.combo_whisper)

        # Summary & notes AI - rule-based (local), a local LLM, or cloud. Rows
        # carry a PRO / LOCAL / CLOUD pill so the cost and privacy of each
        # choice is visible before picking it.
        d_lay.addWidget(QLabel("Summary & notes AI", dev_frame))
        self.combo_action = QComboBox(dev_frame)
        self.combo_action.currentIndexChanged.connect(self._on_action_engine_changed)
        self.combo_action.activated.connect(self._on_action_engine_picked)
        d_lay.addWidget(self.combo_action)

        # Shown only for bring-your-own-key cloud engines.
        self.key_row = QWidget(dev_frame)
        k_lay = QVBoxLayout(self.key_row)
        k_lay.setContentsMargins(0, 4, 0, 0)
        k_lay.setSpacing(6)
        self.lbl_key = QLabel("API key", self.key_row)
        k_lay.addWidget(self.lbl_key)
        self.input_action_key = QLineEdit(self.key_row)
        self.input_action_key.setEchoMode(QLineEdit.Password)
        self.input_action_key.setPlaceholderText("Paste your API key")
        self.input_action_key.textChanged.connect(self._on_action_key_changed)
        k_lay.addWidget(self.input_action_key)
        self.lbl_key_hint = QLabel("", self.key_row)
        self.lbl_key_hint.setObjectName("subtitleLabel")
        self.lbl_key_hint.setWordWrap(True)
        k_lay.addWidget(self.lbl_key_hint)
        self.key_row.hide()
        d_lay.addWidget(self.key_row)

        lay.addWidget(dev_frame)

        # Start button row
        btn_lay = QHBoxLayout()
        btn_lay.addStretch()

        btn_start = QPushButton("Start Meeting", self.page_idle)
        btn_start.setObjectName("primaryButton")
        btn_start.setMinimumHeight(40)
        btn_start.clicked.connect(lambda: self._start_meeting())
        btn_lay.addWidget(btn_start)
        lay.addLayout(btn_lay)

    # ── Recording View: Dual Note Editor ──
    def _build_page_recording(self):
        lay = QVBoxLayout(self.page_recording)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # Compact REC status banner (the duration + what's being captured).
        rec_banner = QFrame(self.page_recording)
        rec_banner.setStyleSheet("background-color: #3b1818; border: 1px solid #ef4444; border-radius: 8px;")
        rb_lay = QHBoxLayout(rec_banner)
        rb_lay.setContentsMargins(12, 8, 12, 8)

        self.lbl_rec_timer = QLabel("REC  ·  00:00", rec_banner)
        self.lbl_rec_timer.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 13px;")
        rb_lay.addWidget(self.lbl_rec_timer)
        rb_lay.addStretch()
        lay.addWidget(rec_banner)

        # Vertical workspace: the prominent Live Summary on top (it builds itself
        # as people talk), with the raw transcript + your own notes beneath it.
        vsplit = QSplitter(Qt.Vertical, self.page_recording)
        vsplit.setStyleSheet("QSplitter::handle { background-color: #27272a; height: 2px; }")

        sum_frame = QFrame(vsplit)
        sum_frame.setObjectName("cardFrame")
        s_lay = QVBoxLayout(sum_frame)
        s_lay.addWidget(QLabel("Live Summary  ·  updates as the conversation goes", sum_frame))
        self.live_summary_log = QTextEdit(sum_frame)
        self.live_summary_log.setReadOnly(True)
        self.live_summary_log.setPlaceholderText(
            "A running summary of the conversation will build here as people "
            "speak — key points and decisions, updated every few seconds.")
        s_lay.addWidget(self.live_summary_log)
        vsplit.addWidget(sum_frame)

        hsplit = QSplitter(Qt.Horizontal, vsplit)
        hsplit.setStyleSheet("QSplitter::handle { background-color: #27272a; width: 2px; }")

        trans_frame = QFrame(hsplit)
        trans_frame.setObjectName("cardFrame")
        t_lay = QVBoxLayout(trans_frame)
        t_lay.addWidget(QLabel("Live Transcription  ·  speaker labels are added after you Stop", trans_frame))
        self.live_trans_log = QTextEdit(trans_frame)
        self.live_trans_log.setReadOnly(True)
        t_lay.addWidget(self.live_trans_log)
        hsplit.addWidget(trans_frame)

        notes_frame = QFrame(hsplit)
        notes_frame.setObjectName("cardFrame")
        n_lay = QVBoxLayout(notes_frame)
        n_lay.addWidget(QLabel("Your Notes (type bullets during meeting)", notes_frame))
        self.input_live_notes = QTextEdit(notes_frame)
        self.input_live_notes.setPlaceholderText("- Decided to use PySide6 for desktop client\n- Aram to finalize setup instructions\n- Sprint ends on Monday")
        # Typed notes go to the session as they are typed (saved to the
        # meeting folder, so a crash keeps them).
        self.input_live_notes.textChanged.connect(self._on_live_notes_typed)
        n_lay.addWidget(self.input_live_notes)
        hsplit.addWidget(notes_frame)
        hsplit.setSizes([400, 360])

        vsplit.addWidget(hsplit)
        vsplit.setSizes([230, 340])
        lay.addWidget(vsplit)

        # Bottom Recording Control row
        btn_lay = QHBoxLayout()
        btn_assist = QPushButton("Live Assistant", self.page_recording)
        btn_assist.setToolTip(
            "A private floating prompter: the last thing said, the running summary "
            "and AI suggestions - kept out of your screen share.")
        btn_assist.clicked.connect(
            lambda: self.app and self.app.toggle_live_assist())
        btn_lay.addWidget(btn_assist)
        btn_lay.addStretch()

        btn_stop = QPushButton("Stop & Generate Notes", self.page_recording)
        btn_stop.setObjectName("primaryButton")
        btn_stop.setStyleSheet("background-color: #ef4444; border-color: #dc2626;")
        btn_stop.clicked.connect(self._stop_meeting)
        btn_lay.addWidget(btn_stop)
        lay.addLayout(btn_lay)

    # ── Processing View: Loading spinner overlay ──
    def _build_page_processing(self):
        lay = QVBoxLayout(self.page_processing)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(20)

        lbl_proc = QLabel("Generating AI Meeting Notes...", self.page_processing)
        lbl_proc.setFont(QFont("Segoe UI", 16, QFont.Bold))
        lbl_proc.setAlignment(Qt.AlignCenter)
        lay.addWidget(lbl_proc)

        pbar = QProgressBar(self.page_processing)
        pbar.setRange(0, 0) # Infinite spinner progress bar style
        pbar.setFixedWidth(300)
        lay.addWidget(pbar)

        self.lbl_proc_desc = QLabel("Finalizing your meeting…\nThis can take a minute for long recordings (speaker ID runs locally).", self.page_processing)
        self.lbl_proc_desc.setObjectName("subtitleLabel")
        self.lbl_proc_desc.setAlignment(Qt.AlignCenter)
        self.lbl_proc_desc.setWordWrap(True)
        lay.addWidget(self.lbl_proc_desc)

    def _set_proc_status(self, text):
        # GUI thread (the session's status signal).
        if hasattr(self, "lbl_proc_desc"):
            self.lbl_proc_desc.setText(text)

    # ── Done View: Notes preview & export ──
    def _build_page_done(self):
        lay = QVBoxLayout(self.page_done)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        title_row = QHBoxLayout()
        self.lbl_done_title = QLabel("Meeting Notes Generated!", self.page_done)
        self.lbl_done_title.setObjectName("titleLabel")
        title_row.addWidget(self.lbl_done_title)

        title_row.addStretch()

        # Shown only when the AI summary failed but the transcript was saved -
        # lets the user re-run the summary without re-recording. The engine
        # picker beside it is the point: retrying the SAME engine on the same
        # transcript fails identically, so a retry must be able to switch.
        self.combo_retry_engine = QComboBox(self.page_done)
        self.combo_retry_engine.setMinimumWidth(190)
        self.combo_retry_engine.hide()
        title_row.addWidget(self.combo_retry_engine)

        self.btn_retry_summary = QPushButton("Retry Summary", self.page_done)
        self.btn_retry_summary.setObjectName("primaryButton")
        self.btn_retry_summary.clicked.connect(self._retry_summary)
        self.btn_retry_summary.hide()
        title_row.addWidget(self.btn_retry_summary)

        self.btn_notion = QPushButton("Send to Notion", self.page_done)
        self.btn_notion.clicked.connect(self._send_to_notion)
        title_row.addWidget(self.btn_notion)

        btn_copy = QPushButton("Copy Markdown", self.page_done)
        btn_copy.clicked.connect(self._copy_markdown)
        title_row.addWidget(btn_copy)

        btn_save = QPushButton("Save to File", self.page_done)
        btn_save.clicked.connect(self._save_to_file)
        title_row.addWidget(btn_save)

        btn_reset = QPushButton("Reset", self.page_done)
        btn_reset.setObjectName("primaryButton")
        btn_reset.clicked.connect(self._reset)
        title_row.addWidget(btn_reset)

        lay.addLayout(title_row)

        # Split preview between Notes & full transcript
        splitter = QSplitter(Qt.Horizontal, self.page_done)
        splitter.setStyleSheet("QSplitter::handle { background-color: #27272a; width: 2px; }")

        # Left: Generated summary & action items
        summary_frame = QFrame(splitter)
        summary_frame.setObjectName("cardFrame")
        s_lay = QVBoxLayout(summary_frame)
        s_lay.addWidget(QLabel("AI Summary & Action Items", summary_frame))
        self.txt_summary = QTextEdit(summary_frame)
        s_lay.addWidget(self.txt_summary)
        splitter.addWidget(summary_frame)

        # Right: Clean Full Transcript
        trans_frame = QFrame(splitter)
        trans_frame.setObjectName("cardFrame")
        t_lay = QVBoxLayout(trans_frame)
        t_lay.addWidget(QLabel("Full Transcript  ·  by speaker", trans_frame))
        self.txt_transcript = QTextEdit(trans_frame)
        self.txt_transcript.setReadOnly(True)
        t_lay.addWidget(self.txt_transcript)
        splitter.addWidget(trans_frame)

        splitter.setSizes([460, 300])
        lay.addWidget(splitter)

    # ── Audio Device Scan ──
    def _populate_audio_devices(self):
        # Labels carry no icon: MeetingModeComboBox paints a slate glyph inline
        # beside each word it describes (see ui/mode_combo.py).
        # Only offer system-audio capture where loopback genuinely exists
        # (Windows/WASAPI). Elsewhere - notably macOS - those modes would
        # silently record the mic anyway, so they're hidden.
        has_loopback = meeting_session.has_loopback()
        self.combo_device.clear()
        if has_loopback:
            self.combo_device.addItem("System sound + Microphone (best for meetings)",
                                      "smart_meeting")
        self.combo_device.addItem("Microphone only", "default_mic")
        if has_loopback:
            self.combo_device.addItem("System sound only (no microphone)", "system_only")

        # Get from active config (heal modes this system can't capture)
        current_dev, _healed = meeting_session.resolve_audio_mode(
            self.app.cfg.get("meeting_audio_mode") if self.app else None,
            loopback=has_loopback)

        idx = self.combo_device.findData(str(current_dev))
        if idx >= 0:
            self.combo_device.setCurrentIndex(idx)
        else:
            self.combo_device.setCurrentIndex(0)

        # Default the language selector to the app-wide spoken language.
        if hasattr(self, "combo_lang"):
            lang = self.app.cfg.get("language", "auto") if self.app else "auto"
            lidx = self.combo_lang.findData(lang)
            if lidx >= 0:
                self.combo_lang.setCurrentIndex(lidx)

        # Transcription model (local Whisper sizes).
        if hasattr(self, "combo_whisper"):
            try:
                models = meeting_session._main_attr("MODELS") or {}
                self.combo_whisper.clear()
                for name, info in models.items():
                    self.combo_whisper.addItem(
                        f"{name}  ·  {info['quality']}, {info['size']}", name)
                cur = self.app.cfg.get("whisper_model", "base") if self.app else "base"
                wi = self.combo_whisper.findData(cur)
                self.combo_whisper.setCurrentIndex(wi if wi >= 0 else 0)
            except Exception:
                pass

        # Summary & notes AI engine, each row tagged PRO / LOCAL / CLOUD.
        # Rule-based isn't offered: it's the internal safety net used when a
        # chosen engine can't run, not something to pick for meeting notes.
        if hasattr(self, "combo_action"):
            self._populate_engine_combo(self.combo_action)
            self._on_action_engine_changed()

    def _on_action_engine_changed(self, *_):
        """Reveal the API-key field only for bring-your-own-key cloud engines,
        and prefill it from whatever is already configured."""
        if not hasattr(self, "key_row"):
            return
        model_id = self.combo_action.currentData()
        info = actions.ACTION_MODELS.get(model_id, {})
        if info.get("kind") != "cloud":
            self.key_row.hide()
            return
        cfg = self.app.cfg if self.app else {}
        provider = info.get("provider", "")
        label = info.get("label", "this engine")
        # Gemini actions fall back to the Google speech key when no dedicated
        # action key is set, so show that one rather than an empty box.
        existing = (cfg.get("action_api_key") or "").strip()
        if not existing and provider == getattr(action_api, "PROVIDER_GEMINI", None):
            existing = (cfg.get("google_api_key") or "").strip()
        self.input_action_key.blockSignals(True)
        self.input_action_key.setText(existing)
        self.input_action_key.blockSignals(False)
        self.lbl_key.setText(f"{label} - API key")
        self.lbl_key_hint.setText(
            "Your own key, stored on this machine. Needed before this engine can "
            "write the notes; without it the meeting falls back to a local summary.")
        self.key_row.show()

    def _on_action_key_changed(self, text):
        """Stage the key straight onto the app config - the meeting window has
        no Save button, matching how the device/language selectors behave."""
        if not self.app:
            return
        model_id = self.combo_action.currentData()
        info = actions.ACTION_MODELS.get(model_id, {})
        if info.get("kind") != "cloud":
            return
        self.app.cfg["action_api_key"] = (text or "").strip()
        if info.get("provider"):
            self.app.cfg["action_api_provider"] = info["provider"]
        self.app.save_config()

    def _sync_setup_from_cfg(self):
        """Point the recording-mode / model / engine pickers at the current
        settings. They're filled once at launch, and Start used to save them
        back - silently reverting anything changed in Settings since. Never
        writes the config; a pick the user makes HERE is saved as it's made
        (the _on_*_picked handlers)."""
        if not self.app:
            return
        cfg = self.app.cfg

        def _select(combo, value):
            idx = combo.findData(value) if value else -1
            if idx >= 0 and idx != combo.currentIndex():
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)
            return idx

        try:
            # A mode this system can't capture isn't listed: keep the healed
            # pick from _populate_audio_devices.
            if getattr(self, "combo_device", None) is not None:
                _select(self.combo_device, cfg.get("meeting_audio_mode"))
            if getattr(self, "combo_whisper", None) is not None:
                _select(self.combo_whisper, cfg.get("whisper_model"))
            if getattr(self, "combo_action", None) is not None:
                # Rule-based isn't offered here - same fallback as the populate.
                if _select(self.combo_action,
                           actions.normalize_action_model(cfg.get("action_model"))) < 0:
                    _select(self.combo_action, self._default_engine_id())
                self._on_action_engine_changed()
        except Exception as e:
            logger.debug("meeting setup re-sync failed: %s", e)

    def _stage_pick(self, key, value):
        if self.app and value and self.app.cfg.get(key) != value:
            self.app.cfg[key] = value
            self.app.save_config()

    def _on_mode_picked(self, *_):
        self._stage_pick("meeting_audio_mode", self.combo_device.currentData())

    def _on_whisper_picked(self, *_):
        self._stage_pick("whisper_model", self.combo_whisper.currentData())

    def _on_action_engine_picked(self, *_):
        self._stage_pick("action_model", self.combo_action.currentData())

    # ── Start / Stop (the session does the work) ──
    def _start_meeting(self, language=None, *, resume=False, audio_mode=None,
                       resume_dir=None):
        """``language``: this session's transcription language (Live
        Assistance passes its own) - never written to the saved settings.
        ``audio_mode``: this session's capture mode, when this system can
        capture it (Live Assistance asks for mic + system sound) - the saved
        Record Meeting choice stays as it is.
        ``resume``/``resume_dir``: set only by resume_meeting; every other
        Start is a NEW meeting."""
        session = self.session
        if not self.app or not getattr(self.app, "recorder", None):
            return
        if session.is_busy:
            return
        # One-time legal notice: recording other participants may require their
        # consent. Must be acknowledged before the first recording ever starts.
        if not ensure_recording_consent(self, self.app):
            return

        # The pickers may predate a Settings change: read them from the config
        # first. The Whisper model and notes engine aren't written back here -
        # a pick made in this window was already saved when it was made.
        self._sync_setup_from_cfg()
        meeting_lang = language or (self.combo_lang.currentData()
                                    if hasattr(self, "combo_lang") else None)
        rec = self.app.recorder
        source = "live_assist" if getattr(rec, "fast_live_chunks", False) else "classic"
        ok, code, message = session.start(
            title=self.input_title.text().strip(),
            attendees=self.input_attendees.text().strip(),
            audio_mode=audio_mode, language=meeting_lang,
            resume_dir=(resume_dir or session._resume_dir) if resume else None,
            source=source)
        if ok:
            self.container.setCurrentIndex(1)
            return
        if code == meeting_session.ERR_FILE_JOB:
            QMessageBox.information(self, "Audio to text is running", message)
        elif code == meeting_session.ERR_FOLDER:
            QMessageBox.critical(self, "Recording Error", message)
        elif code == meeting_session.ERR_DEVICE:
            self.container.setCurrentIndex(0)
            QMessageBox.critical(self, "Recording Error", message)

    def _on_session_state(self, phase):
        # GUI thread. Whoever started the meeting (this window, the Meetings
        # page, Live Assistant), this window shows where it is.
        before = self._shown_phase
        self._shown_phase = phase
        if phase in (meeting_session.STATE_RECORDING, meeting_session.PHASE_PAUSED):
            if before not in (meeting_session.STATE_RECORDING, meeting_session.PHASE_PAUSED):
                # A new recording: fresh live views, My notes as the session has them.
                self.live_trans_log.clear()
                self.live_summary_log.clear()
                self.input_live_notes.blockSignals(True)
                self.input_live_notes.setPlainText(self.session.my_notes or "")
                self.input_live_notes.blockSignals(False)
                self._show_retry_summary(False)
            self.container.setCurrentIndex(1)
            if not self.timer.isActive():
                self.timer.start()
            self._tick()
        elif phase == meeting_session.STATE_PROCESSING:
            self.timer.stop()
            self.container.setCurrentIndex(2)
        elif phase == meeting_session.STATE_IDLE:
            self.timer.stop()
            self.container.setCurrentIndex(0)
        else:
            self.timer.stop()        # done / failed: _on_session_finished draws it

    def _on_live_notes_typed(self):
        if self.session.state == self.STATE_RECORDING:
            self.session.update_my_notes(self.input_live_notes.toPlainText())

    def _append_live_line(self, _piece=""):
        # GUI thread (the session's chunk_text). Whisper emits fixed-window
        # chunks that frequently split mid-sentence; show the running text as
        # one sentence per line, without timestamps.
        if self.session.state != self.STATE_RECORDING:
            return
        self.live_trans_log.setPlainText("\n".join(self.session.live_sentences()))
        sb = self.live_trans_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _tick(self):
        session = self.session
        if session.state != self.STATE_RECORDING:
            return
        dur = int(session.elapsed_sec())
        m, s = divmod(dur, 60)
        h, m = divmod(m, 60)
        dur_str = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        mode = session._session_audio_mode or (
            self.app.cfg.get("meeting_audio_mode", "") if self.app else "")
        mode_label = {
            "smart_meeting": "Mic + System audio",
            "system_only": "System audio only (no mic)",
            "default_mic": "Microphone only",
        }.get(mode, "Capturing")
        head = "PAUSED" if session.paused else "REC"
        self.lbl_rec_timer.setText(f"{head}  ·  {dur_str}  ·  {mode_label}")

    def _set_live_summary(self, text):
        # GUI thread (the session's recap signal).
        if self.session.state != self.STATE_RECORDING:
            return
        try:
            self.live_summary_log.setMarkdown(text)
        except Exception:
            self.live_summary_log.setPlainText(text)

    def _stop_meeting(self):
        session = self.session
        if not self.app or session.state != self.STATE_RECORDING:
            return
        # The notes run with the engine this window shows. When the saved one
        # isn't offered here (rule-based - the default, and after a Pro
        # sign-out or Privacy Mode) the shown engine is used for THIS meeting,
        # as 1.9.0 did by saving it at Start - without changing the settings.
        engine = None
        combo = getattr(self, "combo_action", None)
        if combo is not None:
            shown = combo.currentData()
            saved = actions.normalize_action_model(self.app.cfg.get("action_model"))
            if shown and combo.findData(saved) < 0:
                engine = shown
        # My notes reached the session as they were typed (another page may
        # have added to them) - the session's copy is the one kept.
        session.stop(None, notes_engine=engine)

    def _on_session_finished(self, folder, notes, error_msg):
        # GUI thread: the session saved everything; draw the outcome.
        session = self.session
        transcript = session._final_transcript or ""
        if error_msg:
            # If a transcript exists, the recording is NOT lost: land on the Done
            # page showing the transcript + a Retry. Only a true capture failure
            # (nothing transcribed) falls back to the error dialog + reset.
            if transcript.strip():
                self._clear_setup_inputs()
                self.lbl_done_title.setText(session._meeting_title or "Meeting Notes")
                notices = "".join(f"⚠️ {n}\n\n" for n in self._notices())
                self.txt_summary.setPlainText(
                    f"{notices}⚠️ The AI summary couldn't be generated:\n{error_msg}\n\n"
                    "Your full transcript is safe - it's shown on the right and "
                    f"saved to:\n{folder}\n\n"
                    'Click "Retry Summary" above to try again.'
                )
                self._render_transcript(transcript)
                self._show_retry_summary(True)
                self.container.setCurrentIndex(3)
            else:
                # A meeting this window (or Live Assistance through it)
                # started says so here, as in 1.9; one started on the
                # Meetings page is that page's to report (session.error) -
                # and its to reset: the session stays "failed" for it.
                if self.isVisible() or session._source in ("classic", "live_assist"):
                    saved = ""
                    try:
                        if folder and meeting_store.audio_parts(folder):
                            saved = ("\n\nThe recording itself was saved - find it under "
                                     "Settings → History → Meetings.")
                    except Exception:
                        pass
                    QMessageBox.critical(self, "AI Summary Error",
                                         f"Could not generate meeting notes: {error_msg}{saved}")
                    self._reset()
                else:
                    self._sync_setup_from_cfg()
                    self.container.setCurrentIndex(0)
            return

        self._clear_setup_inputs()
        # Keep Retry (and its engine picker) visible when the notes were
        # produced by the downgrade fallback - "success" via the built-in
        # formatter is exactly the case the picker exists for.
        self._show_retry_summary(bool(session._summary_downgraded))
        self.lbl_done_title.setText(session._meeting_title or "Meeting Notes")
        # The notes are markdown - render them as such (headings, bullets,
        # checkboxes) instead of showing raw ## and - [ ] markers. Transcript/
        # recording caveats go above them on screen only - they're about the
        # recording, so they stay out of the exported/copied notes.
        shown = "".join(f"> ⚠️ {n}\n\n" for n in self._notices()) + notes
        try:
            self.txt_summary.setMarkdown(shown)
        except Exception:
            self.txt_summary.setPlainText(shown)
        self._render_transcript(transcript)
        self.container.setCurrentIndex(3)
        if folder and self.isVisible():
            meeting_store.mark_seen(folder)

    def resume_meeting(self, folder):
        """Continue a saved meeting from History: same folder, the new segment
        is appended to the transcript (with a 'Resumed' marker) and saved as
        the next audio part; the notes are regenerated from everything."""
        if self.session.is_busy:
            QMessageBox.information(
                self, "Meeting in progress",
                "Stop the current meeting and let its notes finish before resuming "
                "another.")
            return
        meta = meeting_store.load_meta(folder)
        self.input_title.setText(meta.get("title") or "Untitled Meeting")
        self.input_attendees.setText(meta.get("attendees") or "")
        self.container.setCurrentIndex(0)
        self.show()
        self.raise_()
        self.activateWindow()
        self._start_meeting(resume=True, resume_dir=folder)

    def _clear_setup_inputs(self):
        """Empty the setup page's title/attendees once a session is done. The
        finished meeting keeps its own copy (the session's title/attendees) for
        the Done page, Retry and Notion; the fields are for the NEXT meeting -
        and the Live Assistance card names its session only when the title is
        empty."""
        self.input_title.clear()
        self.input_attendees.clear()

    def _notices(self):
        session = self.__dict__.get("session")
        if session is not None:
            return session.notices()
        return list(self._transcript_notices or [])

    def _render_transcript(self, transcript):
        """Readable transcript: bold slate speaker labels and real line spacing
        instead of an undifferentiated wall of text."""
        rows = []
        for line in (transcript or "").split("\n"):
            esc = html_mod.escape(line)
            m = re.match(r"^(Speaker (?:\d+|\?)|You):", esc)
            if m:
                esc = ("<span style='color:#334155; font-weight:600;'>"
                       + m.group(1) + ":</span>" + esc[m.end():])
            rows.append(esc)
        self.txt_transcript.setHtml(
            "<div style='line-height:140%; font-size:13px; color:#0f172a;'>"
            + "<br>".join(rows) + "</div>")

    # ── Notion export ──
    def _notion_config_dialog(self):
        """One-time Notion setup: integration secret + target page link.
        Returns True once both are saved."""
        from PySide6.QtWidgets import QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("Connect Notion")
        dlg.setMinimumWidth(460)
        lay = QVBoxLayout(dlg)
        info = QLabel(
            "1. Create an internal integration at "
            "<a href='https://www.notion.so/my-integrations'>notion.so/my-integrations</a>"
            " and copy its secret.<br>"
            "2. In Notion, open the page the notes should land under &rarr; "
            "<b>&#8943;</b> menu &rarr; <b>Connections</b> &rarr; add that "
            "integration (without this Notion hides the page from it).<br>"
            "3. Paste both below - saved on this machine only.", dlg)
        info.setOpenExternalLinks(True)
        info.setWordWrap(True)
        lay.addWidget(info)
        lay.addWidget(QLabel("Integration secret", dlg))
        token_in = QLineEdit(dlg)
        token_in.setEchoMode(QLineEdit.Password)
        token_in.setText(self.app.cfg.get("notion_api_key", "") if self.app else "")
        lay.addWidget(token_in)
        lay.addWidget(QLabel("Page link", dlg))
        page_in = QLineEdit(dlg)
        page_in.setPlaceholderText("https://www.notion.so/Your-Page-…")
        page_in.setText(self.app.cfg.get("notion_parent_page", "") if self.app else "")
        lay.addWidget(page_in)
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, dlg)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() != QDialog.Accepted:
            return False
        token = token_in.text().strip()
        page = page_in.text().strip()
        if not token or not notion_export.extract_page_id(page):
            QMessageBox.warning(
                self, "Notion",
                "Both the integration secret and a valid Notion page link are needed.")
            return False
        self.app.cfg["notion_api_key"] = token
        self.app.cfg["notion_parent_page"] = page
        self.app.save_config()
        return True

    def _send_to_notion(self):
        notes = (self._final_notes or "").strip()
        transcript = (self._final_transcript or "").strip()
        if not (notes or transcript):
            QMessageBox.information(
                self, "Nothing to send",
                "Record a meeting first - there are no notes or transcript yet.")
            return
        if not self.app:
            return
        cfg = self.app.cfg
        if not ((cfg.get("notion_api_key") or "").strip()
                and notion_export.extract_page_id(cfg.get("notion_parent_page"))):
            if not self._notion_config_dialog():
                return
        if cfg.get("privacy_mode"):
            # Privacy Mode promises nothing leaves the device; an explicit
            # share is allowed, but never without saying so.
            answer = QMessageBox.question(
                self, "Privacy Mode is on",
                "Privacy Mode keeps everything on this device. Send this "
                "meeting to Notion anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return

        self.btn_notion.setEnabled(False)
        self.btn_notion.setText("Sending…")
        token = cfg.get("notion_api_key", "")
        parent = cfg.get("notion_parent_page", "")
        title = self._meeting_title or "Meeting notes"
        meta_line = time.strftime("Recorded %Y-%m-%d %H:%M · Gusan")

        def _worker():
            try:
                url = notion_export.publish_meeting(
                    token, parent, title, notes,
                    transcript=transcript, meta_line=meta_line)
                err = ""
            except Exception as e:
                url, err = "", str(e)
            try:
                self.sig_notion.emit(url, err)
            except RuntimeError:
                pass    # window already destroyed

        threading.Thread(target=_worker, daemon=True).start()

    def _on_notion_done(self, url, err):
        self.btn_notion.setEnabled(True)
        self.btn_notion.setText("Send to Notion")
        if self.app:
            self.app.track("meeting_exported", {"target": "notion", "ok": not err})
        if err:
            QMessageBox.warning(self, "Notion",
                                f"Couldn't send to Notion:\n\n{err}")
            return
        box = QMessageBox(self)
        box.setWindowTitle("Sent to Notion")
        box.setTextFormat(Qt.RichText)
        box.setText("Meeting notes are in Notion:<br>"
                    f"<a href='{url}'>{html_mod.escape(url)}</a>")
        box.exec()

    def _copy_markdown(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self._final_notes)
        if self.app:
            self.app.track("meeting_exported", {"target": "clipboard", "ok": True})
        QMessageBox.information(self, "Copied", "AI Meeting notes copied to clipboard in Markdown formatting.")

    def _save_to_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Meeting Notes",
            os.path.expanduser(f"~/Documents/{self._meeting_title.replace(' ', '_')}_Notes.md"),
            "Markdown Files (*.md)"
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._final_notes)
                if self.app:
                    self.app.track("meeting_exported", {"target": "markdown", "ok": True})
                QMessageBox.information(self, "Saved", f"Successfully saved meeting notes to {os.path.basename(path)}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save notes: {e}")

    def _show_retry_summary(self, show):
        btn = getattr(self, "btn_retry_summary", None)
        if btn is not None:
            btn.setVisible(bool(show))
        combo = getattr(self, "combo_retry_engine", None)
        if combo is not None:
            if show:
                self._populate_engine_combo(combo)
            combo.setVisible(bool(show))

    def _populate_engine_combo(self, combo):
        """Fill an engine combo (minus rule-based) with PRO/LOCAL/CLOUD pills,
        preselecting the currently configured engine."""
        try:
            from ui.mode_combo import tag_engine_combo
            combo.blockSignals(True)
            combo.clear()
            for mid, info in actions.ACTION_MODELS.items():
                if mid == actions.RULE_BASED_ID:
                    continue
                combo.addItem(info.get("label", mid), mid)
            cur = (actions.normalize_action_model(self.app.cfg.get("action_model"))
                   if self.app else actions.API_MANAGED_ID)
            idx = combo.findData(cur)
            if idx < 0:
                idx = combo.findData(self._default_engine_id())
            combo.setCurrentIndex(max(idx, 0))
            combo.blockSignals(False)
            tag_engine_combo(combo)
        except Exception as e:
            logger.debug("engine combo populate failed: %s", e)

    def _default_engine_id(self):
        """Engine to preselect when none is configured (see
        meeting_session.default_engine_id)."""
        return meeting_session.default_engine_id(self.app)

    def _retry_summary(self):
        # Re-run only the summary step on the saved transcript - no re-record,
        # no re-transcribe. Used after a summary failure on the Done page.
        if not (self.app and (self._final_transcript or "").strip()):
            return
        # Apply the picker: a retry that can't change the engine would fail
        # identically (same model, same transcript, same context limit). Like
        # 1.9, the pick is saved as the notes engine.
        picked = self.combo_retry_engine.currentData()
        self._show_retry_summary(False)
        if self.session.retry_notes(picked, persist=True):
            self.container.setCurrentIndex(2)
        else:
            self._show_retry_summary(True)

    def _reset(self):
        # Reset/Abort drop any pending resume in the session, or the NEXT new
        # meeting would write into the resumed folder.
        self.session.reset()
        self.input_title.clear()
        self.input_attendees.clear()
        # Back to the saved settings (index 0 used to flip the recording mode
        # to "System sound + Microphone", which the next Start then saved).
        self._sync_setup_from_cfg()
        self.container.setCurrentIndex(0)

    def _abort(self):
        # Discard: the session stops the recorder, tells Live Assistance and
        # hides a NEW meeting's folder from History.
        self.session.abort()
        self._reset()

    def closeEvent(self, event):
        if self.state == self.STATE_RECORDING:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Record Meeting Active")
            msg_box.setText("A meeting is actively being recorded. What would you like to do?")

            btn_minimize = msg_box.addButton("Minimize to Tray (Keep Recording)", QMessageBox.AcceptRole)
            btn_stop = msg_box.addButton("Stop & Generate Notes", QMessageBox.AcceptRole)
            btn_abort = msg_box.addButton("Abort & Discard", QMessageBox.DestructiveRole)
            msg_box.addButton("Cancel", QMessageBox.RejectRole)

            msg_box.setDefaultButton(btn_minimize)
            msg_box.exec()

            clicked = msg_box.clickedButton()
            if clicked == btn_minimize:
                self.hide()
                if self.app and hasattr(self.app, "show_tray_hint"):
                    self.app.show_tray_hint(
                        "Meeting Recording Active",
                        "Gusan is still recording in the background. Right-click the tray icon to restore."
                    )
                event.ignore()
            elif clicked == btn_stop:
                self._stop_meeting()
                event.ignore()
            elif clicked == btn_abort:
                self._abort()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            event.ignore()
        else:
            super().keyPressEvent(event)

    def _build_transcript_with_markers(self, chunks):
        parts = []
        for i, chunk in enumerate(chunks):
            text = chunk.get("text", "").strip()
            if not text:
                continue
            if i > 0 and chunk.get("silence_before", 0) >= 1.4:
                text = f"[speaker change] {text}"
            parts.append(text)
        return " ".join(parts)

    def _build_llm_input(self, transcript):
        if not self._meeting_title and not self._meeting_attendees and not self._user_notes:
            return transcript
        parts = []
        if self._meeting_title:
            parts.append(f"Meeting Title: {self._meeting_title}")
        if self._meeting_attendees:
            parts.append(f"Attendees: {self._meeting_attendees}")
        if self._user_notes:
            parts.append(f"User Notes:\n{self._user_notes}")
        parts.append(f"Transcript:\n{transcript}")
        return "\n\n".join(parts)

    @staticmethod
    def _strip_markdown(md):
        lines = []
        for line in (md or "").splitlines():
            line = re.sub(r"^#+\s+", "", line)
            if line.strip().startswith("- [ ]") or line.strip().startswith("- "):
                line = re.sub(r"^\s*-\s*(\[\s*\])?\s*", "• ", line)
            line = line.replace("**", "")
            line = line.replace("`", "")
            lines.append(line)
        return "\n".join(lines)

    def _format_share(self, format_type):
        summary = self._summary_var or ""
        title = self._meeting_title or "Meeting"

        if format_type == "markdown":
            return summary

        if format_type == "email":
            clean_body = self._strip_markdown(summary)
            return (
                f"Subject: {title} - Notes\n\n"
                "Hi,\n\n"
                f"{clean_body}\n\n"
                "Best,"
            )

        if format_type == "slack":
            slack = re.sub(r"^#+\s+(.*)$", r"*\1*", summary, flags=re.MULTILINE)
            slack = slack.replace("**", "*")
            return f"*{title} - Notes*\n\n{slack}"

        return summary
