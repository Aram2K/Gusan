"""ElevenLabs realtime clients: live speech-to-text (Scribe v2 Realtime) and the streamed voice.

Both connect straight from the PC to ElevenLabs with single-use tokens that our server mints
(interpreter-session); the API key never reaches the PC. Facts checked live on 2026-11-07:

* Speech-to-text: wss://api.elevenlabs.io/v1/speech-to-text/realtime, token type
  ``realtime_scribe`` in the ``token`` query parameter; audio goes up as JSON
  {"message_type": "input_audio_chunk", "audio_base_64", "commit": false, "sample_rate"};
  answers are partial_transcript / committed_transcript (commit_strategy=vad cuts at pauses).
* Voice: eleven_v4_turbo is NOT allowed on the text-to-speech websocket, only on the
  text-to-dialogue one: wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input with a
  ``ttd_websocket`` token in ``single_use_token``. First message {"voices": [voice_id]}, then per
  phrase {"inputs": [{"text", "voice_id", "new_turn": true}]} + {"flush": true}; audio arrives
  as {"audio": base64} until {"is_final_audio_for_turn": true}. The server closes an idle socket
  after 20 s unless it gets {"keep_alive": true}. Kept open, a phrase starts in ~0.09 s
  (first phrase on a new socket ~0.34 s).

The websocket library is imported lazily so the rest of the app (and the tests) never need it.
"""
import base64
import json
import logging
import re
import threading
import time
import urllib.parse

logger = logging.getLogger(__name__)

STT_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
TTD_URL = "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input"
STT_MODEL = "scribe_v2_realtime"
TTS_MODEL = "eleven_v4_turbo"
SAMPLE_RATE = 16000
# Scribe Realtime refuses the WHOLE session when one keyterm is longer than this ("invalid_request:
# Each keyterm must be at most 20 characters", seen 2026-11-09 with a Dictionary e-mail address).
KEYTERM_MAX_CHARS = 20
KEYTERMS_MAX = 50
_KEYTERM_SPLIT = re.compile(r"[\s@./\\_:;,+|()\[\]{}-]+")


def clean_keyterms(terms, limit=KEYTERMS_MAX):
    """The Dictionary words as Scribe takes them: each at most KEYTERM_MAX_CHARS, no repeats, at most
    ``limit``. A longer term gives its parts instead ("claire.dubois@telva.fr" -> "claire", "dubois",
    "telva"; a long phrase -> its words), parts shorter than 3 characters left out."""
    out = []
    seen = set()
    for term in terms or ():
        t = " ".join(str(term or "").split())
        parts = [t] if 0 < len(t) <= KEYTERM_MAX_CHARS else [p for p in _KEYTERM_SPLIT.split(t) if len(p) >= 3]
        for p in parts:
            if not p or len(p) > KEYTERM_MAX_CHARS or p.lower() in seen:
                continue
            seen.add(p.lower())
            out.append(p)
            if len(out) >= limit:
                return out
    return out


class VoiceError(RuntimeError):
    pass


def default_connect(url, ping_s=20):
    from websockets.sync.client import connect      # lazy: only the interpreter needs it
    return connect(url, max_size=None, open_timeout=8, close_timeout=2, ping_interval=ping_s,
                   ping_timeout=ping_s)


def stt_connect(url):
    """The speech-to-text socket: a link that died without a word is noticed within ~10 s."""
    return default_connect(url, ping_s=5)


def _closed_error():
    try:
        from websockets.exceptions import ConnectionClosed
        return ConnectionClosed
    except Exception:  # noqa: BLE001
        return OSError


def _quiet_close(ws):
    try:
        ws.close()
    except Exception:  # noqa: BLE001
        pass


class ScribeRealtime:
    """Live speech-to-text for one direction.

    on_event(kind, text): "partial" (live words), "committed" (a finished phrase),
    "error" (a message), "dropped" (the connection broke and a reconnect follows: words not
    committed yet never will be), "closed" (gone for good; text: why, e.g. "quota_exceeded" or the
    token error's code, "" when the network stayed down). Called on the reader thread.
    token_provider() -> a fresh realtime_scribe token, used for a reconnect after a drop.

    A drop is tried again for ``retry_window_s`` (a Wi-Fi switch, a laptop waking up), waiting
    longer each time (up to ``retry_max_s``); the window starts again once words come back (or a
    connection lasted ``healthy_s``). An error a new connection would only get again (no credit,
    a refused token, an ended session) is not tried again.
    """

    CHUNK_S = 0.1
    FATAL = frozenset(("auth_error", "quota_exceeded", "unaccepted_terms"))
    FATAL_TOKEN = frozenset(("auth", "pro_required", "allowance_used", "bad_request", "provider_credits"))

    def __init__(self, token, language, on_event, *, token_provider=None, keyterms=(),
                 vad_silence=0.4, sample_rate=SAMPLE_RATE, connect=None, max_reconnects=None,
                 retry_window_s=60.0, retry_s=0.3, retry_max_s=5.0, healthy_s=20.0):
        self._token = token
        self.language = language
        self._on_event = on_event
        self._token_provider = token_provider
        self.keyterms = clean_keyterms(keyterms)
        self.vad_silence = vad_silence
        self.sample_rate = sample_rate
        self._connect = connect or stt_connect
        self.max_reconnects = max_reconnects      # None: as many as fit in the window
        self.retry_window_s = retry_window_s
        self.retry_s = retry_s
        self.retry_max_s = retry_max_s
        self.healthy_s = healthy_s
        self._ws = None
        self._buf = bytearray()
        self._closing = False
        self._fatal = ""
        self._reopen = False                      # reconnect at once (the keyterms were refused)
        self._send_lock = threading.Lock()
        self._reader = None
        self.session_config = None

    def url(self, token):
        q = [("model_id", STT_MODEL), ("token", token), ("audio_format", f"pcm_{self.sample_rate}"),
             ("commit_strategy", "vad"), ("vad_silence_threshold_secs", str(self.vad_silence))]
        if self.language:
            q.append(("language_code", self.language))
        q += [("keyterms", k) for k in self.keyterms]
        return STT_URL + "?" + urllib.parse.urlencode(q)

    def start(self):
        self._ws = self._connect(self.url(self._token))
        self._reader = threading.Thread(target=self._read_loop, name="scribe-reader", daemon=True)
        self._reader.start()

    def send_audio(self, pcm):
        if self._closing or self._ws is None:
            return
        self._buf.extend(pcm)
        step = int(self.sample_rate * self.CHUNK_S) * 2
        while len(self._buf) >= step:
            chunk = bytes(self._buf[:step])
            del self._buf[:step]
            msg = json.dumps({"message_type": "input_audio_chunk",
                              "audio_base_64": base64.b64encode(chunk).decode("ascii"),
                              "commit": False, "sample_rate": self.sample_rate})
            try:
                with self._send_lock:
                    self._ws.send(msg)
            except Exception:  # noqa: BLE001 - the reader notices the drop and reconnects
                logger.debug("scribe send failed", exc_info=True)
                return

    def close(self):
        self._closing = True
        ws = self._ws
        if ws is not None:
            _quiet_close(ws)

    def _read_loop(self):
        closed_exc = _closed_error()
        down_since = None            # when this outage began (None: the link works)
        tries = 0
        why = ""
        while not self._closing:
            ws, opened, heard = self._ws, time.monotonic(), False
            if ws is not None:
                try:
                    for raw in ws:
                        heard = self._handle(raw) or heard
                        if self._fatal or self._reopen:
                            break
                except closed_exc:
                    pass
                except Exception:  # noqa: BLE001
                    logger.debug("scribe reader stopped", exc_info=True)
                _quiet_close(ws)
            if self._closing:
                return
            self._ws = None                           # audio meanwhile is dropped, not queued
            self._reopen = False                      # (the reconnect below opens it without keyterms)
            if self._fatal or not self._token_provider:
                self._on_event("closed", self._fatal)
                return
            if heard or time.monotonic() - opened >= self.healthy_s:
                down_since, tries = None, 0           # it worked: a new drop, a new window
            if down_since is None:
                down_since = time.monotonic()
                self._on_event("dropped", "")
            # dropped mid-call: try again with a fresh token, waiting longer each time
            if time.monotonic() - down_since >= self.retry_window_s or (
                    self.max_reconnects is not None and tries >= self.max_reconnects):
                self._on_event("closed", why)
                return
            tries += 1
            if not self._wait(min(self.retry_s * 2 ** (tries - 1), self.retry_max_s)):
                return
            try:
                ws = self._connect(self.url(self._token_provider()))
            except Exception as e:  # noqa: BLE001
                why = str(getattr(e, "code", "") or "")
                logger.warning("scribe reconnect failed: %s", why or e)
                if why in self.FATAL_TOKEN:           # the session or the account is over
                    self._on_event("closed", why)
                    return
                continue
            self._ws = ws
            if self._closing:                         # stopped while it was connecting
                _quiet_close(ws)
                return
            logger.info("scribe reconnected (%d)", tries)

    def _wait(self, seconds):
        """Sleep, waking at once when closed; False when closed."""
        end = time.monotonic() + seconds
        while not self._closing:
            left = end - time.monotonic()
            if left <= 0:
                return True
            time.sleep(min(left, 0.05))
        return False

    def _handle(self, raw):
        """Pass one message on; True when it carried words (the connection works)."""
        try:
            d = json.loads(raw)
        except (TypeError, ValueError):
            return False
        mt = d.get("message_type")
        if mt == "partial_transcript":
            self._on_event("partial", d.get("text") or "")
            return True
        if mt == "committed_transcript":
            self._on_event("committed", d.get("text") or "")
            return True
        if mt == "session_started":
            self.session_config = d.get("config")
        elif mt in ("committed_transcript_with_timestamps", "warning"):
            pass
        elif d.get("error") or mt:
            err = str(d.get("error") or "")
            if mt == "invalid_request":
                if self.keyterms and "keyterm" in err.lower():
                    # A Dictionary word the service still refuses: open the session again without
                    # the words (the reconnect follows the close) rather than not at all - quietly.
                    logger.warning("scribe refused the keyterms (%s): reconnecting without them", err[:160])
                    self.keyterms = []
                    self._reopen = True                   # the read loop closes this socket and reconnects
                    return False
                self._fatal = mt                      # the same request would only be refused again
            # quota_exceeded, auth_error, session_time_limit_exceeded, ...: tell the caller
            elif mt in self.FATAL:
                self._fatal = mt
            self._on_event("error", f"{mt}: {err}".strip(": "))
        return False


class DialogueVoice:
    """The translated voice for one direction over one kept-open text-to-dialogue socket.

    token_provider() -> a fresh ttd_websocket token (each token opens one socket).
    voice_id: one voice id, or a list (e.g. a woman's and a man's voice). All are registered when
    the socket opens and each phrase picks one (checked live 2026-11-07: switching voices between
    turns on one socket starts in ~0.09 s). A voice the socket does not know reopens it.
    speak(text, on_audio, should_stop, voice_id=None) streams 16-bit PCM chunks to on_audio and
    returns the seconds of audio. One phrase at a time (a lock), turns are separate (new_turn).
    A turn fails when no audio comes for ``turn_timeout`` (a long turn that keeps talking is fine,
    up to ``turn_cap``). close() is final: nothing opens the socket again after it."""

    def __init__(self, token_provider, voice_id, language, *, model=TTS_MODEL,
                 sample_rate=SAMPLE_RATE, connect=None, keepalive_s=15.0, turn_timeout=12.0,
                 turn_cap=120.0, warm_text="Okay."):
        self._token_provider = token_provider
        ids = [voice_id] if isinstance(voice_id, str) or voice_id is None else list(voice_id)
        self.voice_ids = [v for v in ids if v]
        self.voice_id = self.voice_ids[0] if self.voice_ids else None
        self._registered = []
        self.language = language
        self.model = model
        self.sample_rate = sample_rate
        self._connect = connect or default_connect
        self.keepalive_s = keepalive_s
        self.turn_timeout = turn_timeout
        self.turn_cap = turn_cap
        self.warm_text = warm_text
        self._ws = None
        self._lock = threading.Lock()          # one turn at a time + no keepalive mid-turn
        self._last_send = 0.0
        self._closing = False
        self._keepalive = None

    def url(self, token):
        q = {"model_id": self.model, "output_format": f"pcm_{self.sample_rate}",
             "single_use_token": token}
        if self.language:
            q["language_code"] = self.language
        return TTD_URL + "?" + urllib.parse.urlencode(q)

    def open(self, warm=True):
        if self._closing:
            raise VoiceError("voice is closed")
        ws = self._connect(self.url(self._token_provider()))
        ws.send(json.dumps({"voices": list(self.voice_ids)}))
        self._registered = list(self.voice_ids)
        self._ws = ws
        if self._closing:                       # closed while it was connecting
            self._close_socket()
            raise VoiceError("voice is closed")
        self._last_send = time.monotonic()
        if warm and self.warm_text:
            # The first phrase on a new socket (and a new cloned voice) is slow; pay that now,
            # before anyone is waiting. The audio is thrown away.
            try:
                self.speak(self.warm_text, lambda _pcm: None)
            except Exception:  # noqa: BLE001
                logger.debug("voice warm-up failed", exc_info=True)
        if self._keepalive is None or not self._keepalive.is_alive():
            self._keepalive = threading.Thread(target=self._keepalive_loop, name="voice-keepalive", daemon=True)
            self._keepalive.start()

    def reopen(self):
        self._close_socket()
        self.open(warm=False)

    def set_voices(self, voice_ids):
        """A new choice (e.g. another woman's voice): used from the next phrase; the socket
        reopens then only if a voice is new to it."""
        ids = [v for v in voice_ids if v]
        if ids:
            self.voice_ids = ids
            self.voice_id = ids[0]

    def close(self):
        self._closing = True
        self._close_socket()

    def _close_socket(self):
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            ws.send(json.dumps({"close_socket": True}))
        except Exception:  # noqa: BLE001
            pass
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass

    def speak(self, text, on_audio, should_stop=lambda: False, voice_id=None):
        text = (text or "").strip()
        if not text:
            return 0.0
        vid = voice_id or self.voice_id
        if vid not in self.voice_ids:
            self.voice_ids = self.voice_ids + [vid]
        if self._ws is not None and any(v not in self._registered for v in self.voice_ids):
            with self._lock:
                self._close_socket()
            self.open(warm=False)
        with self._lock:
            ws = self._ws
            if ws is None:
                raise VoiceError("voice socket is closed")
            ws.send(json.dumps({"inputs": [{"text": text, "voice_id": vid, "new_turn": True}]}))
            ws.send(json.dumps({"flush": True}))
            self._last_send = time.monotonic()
            nbytes = 0
            cap = time.monotonic() + self.turn_cap
            deadline = time.monotonic() + self.turn_timeout
            while True:
                left = min(deadline, cap) - time.monotonic()
                if left <= 0:
                    raise VoiceError("voice timed out")
                try:
                    raw = ws.recv(timeout=left)
                except TimeoutError:
                    raise VoiceError("voice timed out")
                except Exception as e:  # noqa: BLE001 - socket closed
                    raise VoiceError(f"voice connection lost: {e}")
                try:
                    d = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if d.get("audio"):
                    deadline = time.monotonic() + self.turn_timeout     # still talking
                    pcm = base64.b64decode(d["audio"])
                    nbytes += len(pcm)
                    if not should_stop():
                        on_audio(pcm)
                elif d.get("is_final_audio_for_turn") or d.get("is_final"):
                    break
                elif d.get("error") or (d.get("message") and "audio" not in d):
                    raise VoiceError(f"{d.get('error') or ''} {d.get('message') or ''}".strip())
            return nbytes / 2 / self.sample_rate

    def _keepalive_loop(self):
        while not self._closing:
            time.sleep(1.0)
            if self._closing or self._ws is None:
                continue
            if time.monotonic() - self._last_send < self.keepalive_s:
                continue
            if not self._lock.acquire(timeout=0.5):
                continue
            try:
                if self._ws is not None:
                    self._ws.send(json.dumps({"keep_alive": True}))
                    self._last_send = time.monotonic()
            except Exception:  # noqa: BLE001 - the next speak() reopens
                logger.debug("voice keepalive failed", exc_info=True)
            finally:
                self._lock.release()
