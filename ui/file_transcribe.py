"""Classic "Transcribe Files" tab (1.x Settings): drop an audio file, get a Word document.

Kept working until the Gusan 2.0 Audio to text page replaces it (Wave 4). The
job itself now lives in file_jobs.run_file_job (Qt-free, shared with the new
FileJobController); the transcript text and Word export in transcript_store.
The helpers that used to live here are re-exported for old callers.

Product shape:
* Everything runs LOCALLY through the same tuned Whisper pipeline as dictation
  (recorder.transcribe_segments), so quality matches the rest of the app and
  no audio leaves the machine.
* "Auto" picks the best model this computer can realistically run.
* Progress is honest: distinct stages drive one determinate bar, with a live
  time-left estimate derived from actual throughput. Cancel works while the
  file is read and mid-transcription.
* Any length, for everyone.
"""
import os
import threading
import time

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QCheckBox, QFrame, QTextEdit, QProgressBar, QFileDialog, QMessageBox,
    QApplication,
)

import transcript_store
from file_jobs import (  # noqa: F401  (re-exported for old callers and tests)
    AUDIO_EXTS, probe_duration, pick_auto_model, _fmt_dur, _fmt_eta,
    _P_READ_END, _P_DOWNLOAD_END, _P_TRANSCRIBE_END_WITH_SPK,
    _P_TRANSCRIBE_END_NO_SPK, _P_SPEAKERS_END,
    run_file_job, refusal_message, JobCancelled, JobError,
    BUSY_TITLE, MSG_OTHER_JOB,
)


class FileTranscribeTab(QWidget):
    sig_progress = Signal(int, str, str)     # percent, stage, eta text
    sig_finished = Signal(object, str)       # result dict | None, error
    sig_file_info = Signal(str)              # probed duration/size line

    def __init__(self, parent=None, main_app=None):
        super().__init__(parent)
        self.app = main_app
        self._path = ""
        self._running = False
        self._cancel = False
        self._result = None
        self.setAcceptDrops(True)

        self.sig_progress.connect(self._on_progress)
        self.sig_finished.connect(self._on_finished)
        self.sig_file_info.connect(self._on_file_info)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        title = QLabel("Audio file → Word document", self)
        title.setObjectName("titleLabel")
        lay.addWidget(title)
        sub = QLabel("Interviews, lectures, voice memos - transcribed on this "
                     "computer with speaker labels, delivered as a .docx you "
                     "can send to anyone. Nothing is uploaded.", self)
        sub.setObjectName("subtitleLabel")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        # Drop zone
        self.drop = QFrame(self)
        self.drop.setObjectName("dropZone")
        self.drop.setMinimumHeight(110)
        self.drop.setCursor(Qt.PointingHandCursor)
        dz = QVBoxLayout(self.drop)
        dz.setAlignment(Qt.AlignCenter)
        self.lbl_drop = QLabel("Drop an audio file here  ·  or click to browse", self.drop)
        self.lbl_drop.setAlignment(Qt.AlignCenter)
        self.lbl_drop.setStyleSheet("font-size: 14px; font-weight: 600; color: #334155;")
        dz.addWidget(self.lbl_drop)
        self.lbl_file = QLabel("MP3, WAV, M4A, FLAC, video files…", self.drop)
        self.lbl_file.setAlignment(Qt.AlignCenter)
        self.lbl_file.setStyleSheet("color: #64748b; font-size: 11px;")
        dz.addWidget(self.lbl_file)
        self.drop.mousePressEvent = lambda e: self._browse()
        lay.addWidget(self.drop)

        # Options
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Model", self))
        self.combo_model = QComboBox(self)
        self._populate_models()
        opts.addWidget(self.combo_model, 1)
        self.chk_speakers = QCheckBox("Speaker labels", self)
        self.chk_speakers.setChecked(True)
        self.chk_speakers.setToolTip(
            "Identify who is speaking (Speaker 1/2/3) - runs locally, adds a "
            "few minutes for long files.")
        opts.addWidget(self.chk_speakers)
        self.chk_timestamps = QCheckBox("Timestamps", self)
        self.chk_timestamps.setChecked(True)
        opts.addWidget(self.chk_timestamps)
        lay.addLayout(opts)

        self.btn_go = QPushButton("Transcribe file", self)
        self.btn_go.setObjectName("heroButton")
        self.btn_go.setMinimumHeight(42)
        self.btn_go.setEnabled(False)
        self.btn_go.clicked.connect(self._start)
        lay.addWidget(self.btn_go)

        # Progress
        self.prog_box = QWidget(self)
        pv = QVBoxLayout(self.prog_box)
        pv.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.lbl_stage = QLabel("", self.prog_box)
        row.addWidget(self.lbl_stage)
        row.addStretch()
        self.lbl_eta = QLabel("", self.prog_box)
        self.lbl_eta.setStyleSheet("color: #64748b;")
        row.addWidget(self.lbl_eta)
        self.btn_cancel = QPushButton("Cancel", self.prog_box)
        self.btn_cancel.clicked.connect(self._request_cancel)
        row.addWidget(self.btn_cancel)
        pv.addLayout(row)
        self.bar = QProgressBar(self.prog_box)
        self.bar.setRange(0, 100)
        pv.addWidget(self.bar)
        self.prog_box.hide()
        lay.addWidget(self.prog_box)

        # Result
        self.result_box = QWidget(self)
        rv = QVBoxLayout(self.result_box)
        rv.setContentsMargins(0, 0, 0, 0)
        self.preview = QTextEdit(self.result_box)
        self.preview.setReadOnly(True)
        rv.addWidget(self.preview, 1)
        btns = QHBoxLayout()
        self.btn_save = QPushButton("Save Word document (.docx)", self.result_box)
        self.btn_save.setObjectName("heroButton")
        self.btn_save.setMinimumHeight(38)
        self.btn_save.clicked.connect(self._save_docx)
        btns.addWidget(self.btn_save)
        self.btn_copy = QPushButton("Copy text", self.result_box)
        self.btn_copy.clicked.connect(self._copy_text)
        btns.addWidget(self.btn_copy)
        btns.addStretch()
        rv.addLayout(btns)
        self.result_box.hide()
        lay.addWidget(self.result_box, 1)
        lay.addStretch()

    # ── file picking ──
    def dragEnterEvent(self, event):
        if self._running:
            return
        urls = event.mimeData().urls()
        if urls and os.path.splitext(urls[0].toLocalFile())[1].lower() in AUDIO_EXTS:
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self._set_file(urls[0].toLocalFile())

    def _browse(self):
        if self._running:
            return
        exts = " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an audio file", "", f"Audio files ({exts});;All files (*)")
        if path:
            self._set_file(path)

    def _set_file(self, path):
        if not path or not os.path.isfile(path):
            return
        self._path = path
        self._result = None
        self.result_box.hide()
        name = os.path.basename(path)
        try:
            size_mb = os.path.getsize(path) / (1024 * 1024)
            info = f"{name}  ·  {size_mb:.0f} MB" if size_mb >= 1 else name
        except OSError:
            info = name
        self.lbl_drop.setText(name)
        self.lbl_file.setText(info + "  ·  reading duration…")
        self.btn_go.setEnabled(True)
        threading.Thread(target=self._probe_duration, args=(path, info),
                         daemon=True).start()

    def _probe_duration(self, path, info):
        """Show the length right after picking, via the fast probe."""
        line = info
        dur = probe_duration(path)
        if dur:
            line = f"{info}  ·  {_fmt_dur(dur)}"
        try:
            self.sig_file_info.emit(line)
        except RuntimeError:
            pass

    def _on_file_info(self, line):
        self.lbl_file.setText(line)

    # ── helpers ──

    def _populate_models(self):
        from main import MODELS, model_downloaded
        self.combo_model.clear()
        self.combo_model.addItem("Auto - best for this computer (recommended)", None)
        for name, info in MODELS.items():
            state = "downloaded" if model_downloaded(name) else "will download"
            self.combo_model.addItem(
                f"{name}  ·  {info['quality']}, {info['size']}  ·  {state}", name)

    # ── job control ──
    def _start(self):
        if self._running or not self._path:
            return
        # The recorder (and its GPU/model) is shared with live dictation,
        # meetings and the Audio to text queue - never fight them for it.
        busy = refusal_message(self.app)
        if busy:
            title = BUSY_TITLE if busy == MSG_OTHER_JOB else "Recording in progress"
            QMessageBox.information(self, title, busy)
            return
        self._running = True
        self._cancel = False
        self._result = None
        # Two-way exclusion: dictation/meetings refuse to start while this flag
        # is up (they'd otherwise block on the shared model's inference lock
        # for the entire file - potentially an hour of "Finalising…").
        if self.app is not None:
            self.app._file_job_running = True
        self.result_box.hide()
        self.prog_box.show()
        self.btn_cancel.setEnabled(True)
        self.btn_go.setEnabled(False)
        self.bar.setValue(0)
        self.lbl_stage.setText("Starting…")
        self.lbl_eta.setText("")
        args = (self._path, self.combo_model.currentData(),
                self.chk_speakers.isChecked(), self.chk_timestamps.isChecked())
        self._job_started = time.time()
        if self.app is not None:
            self.app.track("file_transcription_started", {
                "model": args[1], "speakers": args[2], "timestamps": args[3]})
        threading.Thread(target=self._run_job, args=args, daemon=True).start()

    def _request_cancel(self):
        self._cancel = True
        self.btn_cancel.setEnabled(False)
        self.lbl_stage.setText("Stopping…")

    def _emit(self, pct, stage, eta=""):
        try:
            self.sig_progress.emit(int(pct), stage, eta)
        except RuntimeError:
            pass

    # ── the worker (background thread; UI only via signals) ──
    def _run_job(self, path, model_choice, want_speakers, want_ts):
        """The shared Qt-free job (file_jobs.run_file_job); results and errors
        come back through sig_finished as before."""
        try:
            import main as _m
            recorder = self.app.recorder if self.app else _m.AudioRecorder()
            # The global language, passed explicitly: the recorder's own default
            # can be a stale per-meeting override.
            cfg = getattr(self.app, "cfg", None) or {}
            opts = {"model": model_choice, "language": cfg.get("language") or "auto",
                    "speakers": want_speakers, "timestamps": want_ts}

            def on_progress(pct, stage, eta):
                self._emit(pct, stage, _fmt_eta(eta) if eta is not None else "")
            result = run_file_job(path, opts, recorder, on_progress,
                                  lambda: self._cancel)
            self.sig_finished.emit(result, "")
        except JobCancelled:
            self.sig_finished.emit(None, "cancelled")
        except JobError as e:
            self.sig_finished.emit(None, e.message)
        except Exception as e:
            self.sig_finished.emit(None, str(e)[:300])

    # ── GUI-thread slots ──
    def _on_progress(self, pct, stage, eta):
        self.bar.setValue(pct)
        self.lbl_stage.setText(stage)
        self.lbl_eta.setText(eta)

    def _on_finished(self, result, error):
        self._running = False
        if self.app is not None:
            self.app._file_job_running = False
        self.prog_box.hide()
        self.btn_go.setEnabled(bool(self._path))
        if self.app is not None:
            took_min = round((time.time() - getattr(self, "_job_started", time.time())) / 60, 1)
            if error:
                reason = "cancelled" if error == "cancelled" else "error"
                self.app.track("file_transcription_failed", {"reason": reason, "took_minutes": took_min})
            elif result:
                # A long local job on the processor: maybe suggest the GPU.
                if hasattr(self.app, "_note_local_wait"):
                    # Decode time only - not the model download or diarization.
                    decode = result.get("decode_seconds")
                    self.app._note_local_wait(
                        "file", decode if decode is not None else
                        time.time() - getattr(self, "_job_started", time.time()),
                        result.get("model"))
                self.app.track("file_transcription_completed", {
                    "model": result.get("model"),
                    "audio_minutes": round(result.get("duration", 0) / 60, 1),
                    "took_minutes": took_min,
                    "speakers": result.get("speaker_count", 0),
                    "timestamps": result.get("timestamps", False),
                })
        if error == "cancelled":
            return
        if error:
            QMessageBox.warning(self, "Transcription failed", error)
            return
        self._result = result
        self._show_preview(result)
        self.result_box.show()

    def _preview_paragraph_lines(self, result):
        return transcript_store.paragraph_lines(result)

    def _show_preview(self, result):
        import html as html_mod
        from file_jobs import whisper_name
        n_par = len(result["paragraphs"])
        spk = result["speaker_count"]
        head = (f"{_fmt_dur(result['duration'])} of audio · "
                f"{n_par} paragraphs" + (f" · {spk} speakers" if spk else "")
                + f" · {whisper_name(result['model'])}")
        rows = [f"<p style='color:#64748b; font-size:11px;'>{html_mod.escape(head)}</p>"]
        for prefix, text in self._preview_paragraph_lines(result):
            row = ""
            if prefix:
                row += ("<span style='color:#334155; font-weight:600;'>"
                        + html_mod.escape(prefix) + "</span> ")
            row += html_mod.escape(text)
            rows.append(f"<p style='line-height:135%;'>{row}</p>")
        self.preview.setHtml("".join(rows))

    def _plain_text(self):
        if not self._result:
            return ""
        return transcript_store.plain_text(self._result)

    def _copy_text(self):
        QApplication.clipboard().setText(self._plain_text())
        if self.app is not None:
            self.app.track("file_transcription_saved", {"format": "clipboard"})

    def _save_docx(self):
        if not self._result:
            return
        src = self._result["path"]
        suggested = os.path.join(os.path.dirname(src),
                                 transcript_store.suggested_name(self._result, "docx"))
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Word document", suggested, "Word document (*.docx)")
        if not path:
            return
        try:
            transcript_store.save_word(self._result, path)
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        if self.app is not None:
            self.app.track("file_transcription_saved", {"format": "docx"})
        box = QMessageBox(self)
        box.setWindowTitle("Saved")
        box.setText(f"Word document saved:\n{path}")
        open_btn = box.addButton("Open document", QMessageBox.AcceptRole)
        box.addButton("Done", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            # Not os.startfile: it does nothing on Mac.
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
