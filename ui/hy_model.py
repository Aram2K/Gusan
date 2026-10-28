"""The Armenian speech model on a page (Gusan 2.0.1; speech_hy.py is the engine).

Every page that lists models that run on this computer - Settings > Speech & AI, Smart Actions'
"Transcribes with", the Live Assistant's, Audio to text's "Accuracy" - draws the same row
(engine_choice.hy_row: "NVIDIA FastConformer Armenian", LOCAL, "Armenian only · about 105 MB · free,
works offline"), hidden until the model is on this computer or has somewhere to download from, and uses
the same few things from here:

    hy_model.refresh_catalog(app)       once when the page opens: look for the model's manifest entry on a
                                        worker (at most once a day, never in Privacy mode, nothing at all
                                        until a page asks); ``app.downloads.catalog_changed`` then fires
    hy_model.connect(app, owner, slot)  call ``slot`` when the model's listing or download changes
    hy_model.confirm_download(parent, who)   the one calm question before 105 MB come down
    hy_model.state(app)                 "hidden" | "missing" | "downloading" | "ready" | "failed"
    hy_model.progress_line(app)         "NVIDIA FastConformer Armenian · downloading 42%"
    hy_model.can_cancel(app) / cancel(app)   stop a running download (it keeps what it has and resumes)
    hy_model.failure(app)               the plain sentence of a download that stopped, or ""

A download is exactly like a Whisper model's (app.downloads.start(HY_ID)); Privacy mode does not stop it
(nothing of the person's is sent) - speech_hy.DOWNLOAD_BLOCKED_IN_PRIVACY_MODE is the one switch, and a
refusal is a plain sentence on the card.
"""
import logging

import hardware
import speech_hy
from ui.components import mini_prompt

logger = logging.getLogger("transcribe")

HY_ID = hardware.HY_MODEL_ID
TITLE = "Download the Armenian model?"
MORE_NOTE = "Used only when the language is Armenian."
CANCEL = "Cancel"


def is_hy(value):
    return speech_hy.is_hy_model(value)


def _downloads(app):
    dl = getattr(app, "downloads", None)
    return dl if dl is not None and callable(getattr(dl, "state", None)) else None


def refresh_catalog(app):
    """Look for the model's manifest entry on a worker (speech_hy.refresh_catalog: cached for a day,
    never in Privacy mode, never at startup). Call it when a page that lists the model opens."""
    dl = _downloads(app)
    fn = getattr(dl, "refresh_catalog", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            logger.debug("model catalog refresh did not start", exc_info=True)


def mine(args):
    """Is this signal about the Armenian model? (``catalog_changed`` carries nothing: it is.) A page's
    handler starts with ``if not hy_model.mine(args): return``."""
    return not args or not isinstance(args[0], str) or args[0] == HY_ID


def connect(app, owner, handler):
    """Call ``handler(*args)`` - a BOUND method of ``owner`` (it is dropped with the page) - when the
    model's listing changes (catalog_changed) or its download does (state_changed / finished / progress;
    the handler filters on mine(args)). Connected once per (app.downloads, owner)."""
    dl = _downloads(app)
    if dl is None:
        return None
    if getattr(owner, "_hy_connected", None) is dl:
        return dl
    try:
        for name in ("catalog_changed", "state_changed", "finished", "progress"):
            signal = getattr(dl, name, None)
            if signal is not None:
                signal.connect(handler)
        owner._hy_connected = dl
    except Exception:
        logger.debug("Armenian model signals unavailable", exc_info=True)
    return dl


def state(app):
    """The model as a page shows it: "hidden" (not listed), "downloading", "failed", "ready" or
    "missing" (it can be downloaded). From app.downloads when it knows, else from the disk."""
    if not speech_hy.listed():
        return "hidden"
    dl = _downloads(app)
    if dl is not None:
        try:
            st = dl.state(HY_ID)
            if st in ("downloading", "failed", "ready", "missing"):
                return st
        except Exception:
            pass
    return "ready" if speech_hy.ready() else "missing"


def percent(app):
    """The running download's percent (0-100), or None when it isn't known yet."""
    dl = _downloads(app)
    try:
        prog = dl.progress_of(HY_ID) if dl is not None else None
        return int(prog[0]) if prog and prog[0] is not None else None
    except Exception:
        return None


def progress_line(app):
    name = hardware.HY_INFO.real
    pct = percent(app)
    return "%s · downloading %d%%" % (name, pct) if pct is not None else "%s · downloading" % name


def can_cancel(app):
    dl = _downloads(app)
    return bool(dl is not None and callable(getattr(dl, "cancel", None)) and state(app) == "downloading")


def cancel(app):
    """Stop a running download. True when the request was made."""
    dl = _downloads(app)
    try:
        return bool(dl.cancel(HY_ID)) if dl is not None and callable(getattr(dl, "cancel", None)) else False
    except Exception:
        logger.debug("cancel of the Armenian download failed", exc_info=True)
        return False


def failure(app):
    """The plain sentence of a download that stopped ("" when there is none)."""
    dl = _downloads(app)
    try:
        return str(dl.error(HY_ID) or "") if dl is not None else ""
    except Exception:
        return ""


def question_body(who):
    return ("%s · %s. Free, works offline. %s will use it for Armenian."
            % (hardware.HY_INFO.real, hardware.HY_INFO.size, who))


def confirm_download(parent, who):
    """The question before the download: True = download it. ``who``: "The Live Assistant",
    "Smart Actions", "Audio to text", "Dictation"."""
    return bool(mini_prompt.ask(parent, TITLE, question_body(who), "Download", "Not now", icon="download"))


def start(app):
    """Start the download (app.downloads joins one already running). True when it started or
    is already on its way / on disk; False when it could not be started."""
    dl = _downloads(app)
    if dl is None:
        return False
    try:
        if dl.state(HY_ID) == "ready":
            return True
        return bool(dl.start(HY_ID)) or dl.state(HY_ID) == "downloading"
    except Exception:
        logger.warning("The Armenian model download failed to start", exc_info=True)
        return False


__all__ = ["CANCEL", "HY_ID", "MORE_NOTE", "TITLE", "can_cancel", "cancel", "confirm_download", "connect",
           "failure", "is_hy", "mine", "percent", "progress_line", "question_body", "refresh_catalog", "start",
           "state"]
