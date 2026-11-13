"""Live captions, wired to the app (founder, 2026-11-11): the caption window (ui/captions_overlay.py) comes up when a
meeting starts recording with captions on (cfg captions_on, Pro) and goes when it ends; the switches apply in the
middle of a meeting - Captions on / off (the live card's switch, the Meetings card), whose words, the language and
screen sharing.

Words in - the same engine as Transcribes with (founder, 2026-11-11: quality first when you chose it):
  * Gusan Cloud Fast: the meeting's live stream, real time - the sentence being said (MeetingSession.live_partial)
    and each finished one (live_line; the finished pieces instead when the stream gave up);
  * Best accuracy, a model on this computer, your own key: that engine's finished pieces (caption_piece), a few
    seconds behind the voice, the transcript's own accurate words - still appearing word by word.
Translations: action_api.interpret_phrase (Gusan Cloud's "interpret" mode), one sentence at a time, on
captions.CaptionFeed's own thread.
"""
import logging
import time

from PySide6.QtCore import QObject

import captions

logger = logging.getLogger("transcribe")

WATCHED = frozenset({captions.CFG_ON, captions.CFG_LANGUAGE, captions.CFG_WHO, captions.CFG_IN_SHARE,
                     captions.CFG_FONT, captions.CFG_COLORS, "admin_tier_override", "privacy_mode"})


class CaptionsController(QObject):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.session = None
        self.feed = None
        self.overlay = None
        self._stats = {}

    # ── wiring ──
    def attach(self, session):
        self.session = session
        for name, slot in (("state_changed", self.sync), ("caption_line", self._on_line),
                           ("live_partial", self._on_partial), ("caption_piece", self._on_piece)):
            sig = getattr(session, name, None)
            if sig is not None:
                try:
                    sig.connect(slot)
                except Exception:
                    logger.debug("captions: %s not connected", name, exc_info=True)

    def _cfg(self):
        cfg = getattr(self.app, "cfg", None)
        return cfg if isinstance(cfg, dict) else {}

    def _pro(self):
        try:
            return bool(self.app.is_pro())
        except Exception:
            return False

    def wanted(self):
        """Captions on, Pro, and a meeting being recorded (or paused)."""
        state = str(getattr(self.session, "state", "") or "")
        return bool(self._cfg().get(captions.CFG_ON)) and self._pro() and state in ("recording", "paused")

    @property
    def running(self):
        return self.overlay is not None

    def sync(self, *_args):
        try:
            if self.wanted() and not self.running:
                self.start()
            elif not self.wanted() and self.running:
                self.stop()
        except Exception:
            logger.warning("Live captions could not start", exc_info=True)

    def start(self):
        from ui.captions_overlay import CaptionsOverlay
        # What reached the captions, for one log line at the end (where any delay came from).
        self._stats = {"t0": time.monotonic(), "first": None, "partial": 0, "line": 0, "piece": 0}
        self.feed = captions.CaptionFeed(self._cfg, translate=self._translate,
                                         spoken_language=self._source_language)
        self.overlay = CaptionsOverlay(self.feed)
        self.feed.on_change = self.overlay.nudge
        self.overlay.set_in_share(bool(self._cfg().get(captions.CFG_IN_SHARE)))
        self.overlay.set_style(self._cfg().get(captions.CFG_FONT), self._cfg().get(captions.CFG_COLORS))
        self.overlay.start()
        logger.info("Live captions on (%s, %s)", self._cfg().get(captions.CFG_LANGUAGE) or "as spoken",
                    self.feed.who())

    def stop(self):
        st = self._stats
        if st:
            first = "%.1f s" % (st["first"] - st["t0"]) if st.get("first") else "never"
            logger.info("Live captions off: %s; first words %s after the start; %d live updates, %d live sentences, "
                        "%d pieces", "real time" if self.realtime() else "from the pieces", first, st["partial"],
                        st["line"], st["piece"])
            self._stats = {}
        overlay, feed, self.overlay, self.feed = self.overlay, self.feed, None, None
        if feed is not None:
            feed.close()
        if overlay is not None:
            overlay.stop()
            overlay.deleteLater()

    def on_setting_changed(self, key):
        if key not in WATCHED:
            return
        if key in (captions.CFG_ON, "admin_tier_override", "privacy_mode"):
            self.sync()
            return
        if self.overlay is None:
            return
        if key == captions.CFG_IN_SHARE:
            self.overlay.set_in_share(bool(self._cfg().get(captions.CFG_IN_SHARE)))
        elif key in (captions.CFG_FONT, captions.CFG_COLORS):
            self.overlay.set_style(self._cfg().get(captions.CFG_FONT), self._cfg().get(captions.CFG_COLORS))
        elif key == captions.CFG_LANGUAGE:
            self.feed.clear()                             # the next sentence comes in the new language
        else:
            self.overlay.pull()                           # whose words

    # ── words in ──
    def realtime(self):
        """Fast (the live stream) - else the engine's finished pieces."""
        fn = getattr(self.session, "captions_realtime", None)
        try:
            return bool(fn()) if callable(fn) else True
        except Exception:
            return True

    def _from_pieces(self):
        fn = getattr(self.session, "captions_from_pieces", None)
        try:
            return bool(fn()) if callable(fn) else not self.realtime()
        except Exception:
            return not self.realtime()

    def _seen(self, kind):
        st = self._stats
        if st:
            st[kind] = st.get(kind, 0) + 1
            if st.get("first") is None:
                st["first"] = time.monotonic()

    def _on_line(self, text, who, lang=""):
        """A sentence the live stream finished (Fast): shown at once, in place of the words being said."""
        if self.feed is not None and self.realtime():
            self._seen("line")
            self.feed.final(text, who, lang)

    def _on_partial(self, text, who):
        if self.feed is not None and self.realtime():
            self._seen("partial")
            self.feed.partial(text, who)

    def _on_piece(self, text, who, lang=""):
        """A finished piece: the captions' words on Best accuracy / a model on this computer, and on Fast when the
        live stream gave up - let out at reading pace."""
        if self.feed is not None and self._from_pieces():
            self._seen("piece")
            self.feed.final(text, who, lang, paced=True)

    def _source_language(self):
        """The language the meeting is said in, when it is set ("" = detected)."""
        lang = str(getattr(self.session, "_session_language", "") or self._cfg().get("meeting_language")
                   or self._cfg().get("language") or "")
        return "" if lang in ("auto", "multi") else lang

    def _translate(self, text, source, target, context):
        import action_api
        rec = getattr(self.app, "recorder", None)
        getter = getattr(rec, "get_auth_token", None)
        token = getter() if callable(getter) else None
        return action_api.interpret_phrase(text, target, token, source or "auto", context)


__all__ = ["CaptionsController", "WATCHED"]
