"""The two small prompts around a meeting recording, shared by the Meetings
page, Live Assistant and the calendar reminder (AppController.record_from_calendar).

    ensure_recording_consent(parent, app)  # the one-time notice; True to go on
    recording_save_path(parent, stem)      # where to save a recording, "" when cancelled

They lived in the 1.x windows (ui/meetings.py, ui/meeting_detail.py), which
Gusan 2.0 retired; only these helpers were still in use.
"""
import logging
import os

from PySide6.QtWidgets import QFileDialog

logger = logging.getLogger("transcribe")

CONSENT_KEY = "meeting_consent_ack"
CONSENT_TITLE = "Before you record"
CONSENT_TEXT = "You're about to record a meeting - including other people's voices."
CONSENT_DETAIL = ("Depending on your country or state, recording a conversation may require the "
                  "consent of all participants. By continuing you confirm you have any consent "
                  "required where you are.\n\nThis notice is shown only once.")
CONSENT_YES = "I understand - continue"


def _remember_consent(app):
    """Save the acknowledgement: through set_setting (one debounced save, and
    sig_setting_changed tells the pages), else straight into cfg + save_config."""
    setter = getattr(app, "set_setting", None)
    if callable(setter):
        try:
            if setter(CONSENT_KEY, True):
                return
        except Exception:
            logger.debug("set_setting(%s) failed", CONSENT_KEY, exc_info=True)
    cfg = getattr(app, "cfg", None)
    if cfg is not None:
        cfg[CONSENT_KEY] = True
    try:
        app.save_config()
    except Exception:
        logger.debug("could not save the recording consent", exc_info=True)


def ensure_recording_consent(parent, app):
    """One-time notice before the first meeting recording: capturing other
    people's voices may require their consent depending on jurisdiction. The
    user acknowledges once and that is kept. Returns True when recording may
    proceed."""
    cfg = getattr(app, "cfg", None)
    if cfg is None or cfg.get(CONSENT_KEY):
        return True
    # (imported here: the components pull in Qt's SVG module, which the tests' stand-ins of Qt
    # do not have - a test that only patches this module's functions must still import it)
    from ui.components import mini_prompt
    if mini_prompt.confirm(parent, CONSENT_TITLE, CONSENT_TEXT, CONSENT_YES, "Cancel", danger=False,
                           detail=CONSENT_DETAIL, icon="people"):
        _remember_consent(app)
        return True
    return False


def recording_save_path(parent, stem):
    """Ask where to save a meeting recording (MP3 by default, or WAV).
    Returns the path with the right extension, or "" if cancelled."""
    import docx_export
    suggested = os.path.join(os.path.expanduser("~"), f"{docx_export.safe_filename(stem)}.mp3")
    path, chosen = QFileDialog.getSaveFileName(
        parent, "Save recording", suggested, "MP3 audio (*.mp3);;WAV audio (*.wav)")
    if not path:
        return ""
    if "wav" in chosen.lower() and not path.lower().endswith(".wav"):
        path += ".wav"
    elif not path.lower().endswith((".mp3", ".wav")):
        path += ".mp3"
    return path
