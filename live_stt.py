"""Real-time speech to text on Gusan Cloud: the words while the person speaks (Gladia live, Solaria-1).

    app --POST (the person's sign-in)--> live-stt server function --(the secret key)--> Gladia: a live session
    app <-- {"url": "wss://...?token=...", "id": ...}   (a temporary token: the key never reaches the app)
    app ==16 kHz mono 16-bit PCM==> Gladia ==partial / final utterances==> app

The recorder hands every block of audio to LiveSession.feed(); the words come back through on_partial (the sentence
being spoken, replaced as it firms up) and on_final (a finished sentence). stop() ends the session and returns all
the final text. A dropped connection is reconnected once to the same session (Gladia keeps it); after that the
session is "failed" and the recorder's own pieces carry the take as before - nothing is lost either way, the
recording itself never depends on this.

Two engines speak here (the server function chooses the key, the app the engine - main.AudioRecorder._live_engine):
  "gladia"  Gladia live (Solaria-1): binary 16 kHz PCM frames, partial / final utterances, stop_recording. The
            session is deleted at Gladia once it is over (end_session).
  "openai"  OpenAI gpt-live-transcribe (Armenian on Fast: much better than Gladia in Armenian): a short-lived key
            (ek_..., the Authorization header), 24 kHz PCM as input_audio_buffer.append JSON, transcript deltas,
            input_audio_buffer.commit at Stop, then ...transcription.completed is the text. No resume after a drop.

Qt-free; everything here runs on its own threads and never raises into the recorder.
"""
import array
import base64
import hashlib
import json
import logging
import math
import os
import queue
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse

logger = logging.getLogger(__name__)

LIVE_FN_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/live-stt"
SAMPLE_RATE = 16000
SEND_EVERY_BYTES = 3200              # 100 ms of 16 kHz 16-bit mono per frame sent
OPENAI_RATE = 24000                  # OpenAI realtime takes 24 kHz PCM
OPENAI_SEND_EVERY_BYTES = 4800       # 100 ms of 24 kHz 16-bit mono
OPENAI_WSS = "wss://api.openai.com/v1/realtime?intent=transcription"
ENGINE_GLADIA, ENGINE_OPENAI = "gladia", "openai"
ENGINE_MODELS = {ENGINE_GLADIA: "solaria-1", ENGINE_OPENAI: "gpt-live-transcribe"}
BYTES_PER_SECOND = {ENGINE_GLADIA: SAMPLE_RATE * 2, ENGINE_OPENAI: OPENAI_RATE * 2}
# A meeting: a dropped or ended session (Gladia's 3 h, OpenAI's session length, a pause the server ends) is opened
# again - at most this many times; OpenAI turns are committed at a pause (after TURN_MIN_S) or every TURN_MAX_S.
MEETING_REOPENS = 20
TURN_MIN_S, TURN_MAX_S = 2.5, 15.0
QUEUE_MAX_BLOCKS = 600               # about a minute of audio waiting while the line is slow
OPEN_TIMEOUT_S = 8.0
STOP_TIMEOUT_S = 6.0                 # how long stop() waits for the last sentences
# After Gladia's end_recording, the text is ready once no sentence is half-said and nothing new came for this long
# (the connection itself closes only after Gladia's post-processing - not worth waiting for).
QUIET_AFTER_END_S = 0.1
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


class LiveUnavailable(Exception):
    """No live session could be opened (not Pro, offline, Gladia busy): the take runs on pieces as before."""

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _mask(data, key):
    n = len(data)
    if not n:
        return b""
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


class WebSocket:
    """A small RFC 6455 client: one connection, text and binary frames, ping/pong, close. Sends from any thread
    (one lock); receives on one thread."""

    def __init__(self, sock, leftover=b""):
        self.sock = sock
        self._buf = leftover
        self._send_lock = threading.Lock()
        self.closed = False
        self.close_code = None

    @classmethod
    def connect(cls, url, timeout=OPEN_TIMEOUT_S, ssl_context=None, headers=None):
        """``headers``: extra request headers (OpenAI: the session's short-lived key as Authorization)."""
        u = urllib.parse.urlsplit(url)
        extra = ""
        for name, value in (headers or {}).items():
            name, value = str(name), str(value)
            if any(c in name + value for c in "\r\n") or ":" in name:
                raise WebSocketError("bad header")
            extra += "%s: %s\r\n" % (name, value)
        if u.scheme not in ("wss", "ws") or not u.hostname:
            raise WebSocketError("not a websocket address")
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        try:
            sock = (ssl_context or _ssl_context()).wrap_socket(raw, server_hostname=u.hostname) \
                if u.scheme == "wss" else raw
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host = u.hostname if port in (80, 443) else "%s:%d" % (u.hostname, port)
            path = (u.path or "/") + ("?" + u.query if u.query else "")
            request = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nUser-Agent: Gusan\r\n%s\r\n"
                       % (path, host, key, extra))
            sock.sendall(request.encode("ascii"))
            data = b""
            while b"\r\n\r\n" not in data:
                more = sock.recv(4096)
                if not more:
                    raise WebSocketError("the server closed the connection")
                data += more
                if len(data) > 16384:
                    raise WebSocketError("the server's answer is too long")
            head, leftover = data.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
                raise WebSocketError("the server refused the connection: %s" % lines[0][:80])
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            accept = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
            if headers.get("sec-websocket-accept") != accept:
                raise WebSocketError("the server's handshake is wrong")
            sock.settimeout(None)
            return cls(sock, leftover)
        except Exception:
            try:
                raw.close()
            except OSError:
                pass
            raise

    # ── sending ──
    def _send(self, opcode, payload):
        payload = bytes(payload)
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack("!H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack("!Q", n)
        key = os.urandom(4)
        with self._send_lock:
            if self.closed:
                raise WebSocketError("the connection is closed")
            self.sock.sendall(bytes(head) + key + _mask(payload, key))

    def send_binary(self, data):
        self._send(OP_BINARY, data)

    def send_text(self, text):
        self._send(OP_TEXT, text.encode("utf-8"))

    # ── receiving ──
    def _read(self, n):
        while len(self._buf) < n:
            more = self.sock.recv(max(4096, n - len(self._buf)))
            if not more:
                raise WebSocketError("the connection dropped")
            self._buf += more
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self):
        b1, b2 = self._read(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        masked, n = bool(b2 & 0x80), b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read(8))[0]
        key = self._read(4) if masked else None
        payload = self._read(n) if n else b""
        if key:
            payload = _mask(payload, key)
        return fin, opcode, payload

    def recv(self):
        """The next message: (OP_TEXT, str) / (OP_BINARY, bytes), or (OP_CLOSE, code) when the server closed.
        Pings are answered here."""
        parts, first = [], None
        while True:
            fin, opcode, payload = self._frame()
            if opcode == OP_PING:
                try:
                    self._send(OP_PONG, payload)
                except (OSError, WebSocketError):
                    pass
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                self.close_code = code
                try:
                    self._send(OP_CLOSE, payload[:2])
                except (OSError, WebSocketError):
                    pass
                self._shut()
                return OP_CLOSE, code
            if opcode != OP_CONT:
                first, parts = opcode, []
            parts.append(payload)
            if fin:
                data = b"".join(parts)
                return (OP_TEXT, data.decode("utf-8", "replace")) if first == OP_TEXT else (OP_BINARY, data)

    def close(self, code=1000):
        try:
            self._send(OP_CLOSE, struct.pack("!H", code))
        except (OSError, WebSocketError):
            pass
        self._shut()

    def _shut(self):
        with self._send_lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


# ── a live session ──────────────────────────────────────────────────────────────────────────
def open_session(token, language="auto", languages=(), vocabulary=(), post=None, purpose="dictation",
                 engine=ENGINE_GLADIA):
    """Ask the live-stt server function for a session: (wss url, session id) for Gladia (the url carries its
    temporary token), (wss url, "", {"Authorization": "Bearer ek_..."}) for OpenAI. LiveUnavailable when it can't."""
    if not token:
        raise LiveUnavailable("signed_out")
    if post is None:
        import requests
        post = requests.post
    body = {"language": str(language or "auto"), "languages": [str(c) for c in (languages or ())][:8],
            "vocabulary": [str(t) for t in (vocabulary or ())][:100], "sample_rate": SAMPLE_RATE,
            "purpose": "meeting" if purpose == "meeting" else "dictation",
            "engine": ENGINE_OPENAI if engine == ENGINE_OPENAI else ENGINE_GLADIA}
    try:
        resp = post(LIVE_FN_URL, json=body, headers={"Authorization": "Bearer %s" % token}, timeout=OPEN_TIMEOUT_S)
    except Exception as e:
        raise LiveUnavailable("network", str(e)) from e
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code != 200:
        raise LiveUnavailable(str((data or {}).get("error") or resp.status_code))
    url = str((data or {}).get("url") or "")
    host = urllib.parse.urlsplit(url).hostname or ""
    if engine == ENGINE_OPENAI:
        key = str((data or {}).get("token") or "")
        # Only OpenAI's own address, and only a short-lived key (never a project key) in the header.
        if url != OPENAI_WSS or not key.startswith("ek_") or any(c in key for c in "\r\n "):
            raise LiveUnavailable("bad_answer")
        return url, "", {"Authorization": "Bearer " + key}
    if not url.startswith("wss://") or not (host == "gladia.io" or host.endswith(".gladia.io")):
        raise LiveUnavailable("bad_answer")
    return url, str(data.get("id") or "")


def end_session(token, session_id, post=None):
    """Ask the server to delete a finished Gladia live session (its audio and text) at Gladia. Never raises."""
    if not token or not session_id:
        return False
    try:
        if post is None:
            import requests
            post = requests.post
        resp = post(LIVE_FN_URL, json={"action": "end", "id": str(session_id)},
                    headers={"Authorization": "Bearer %s" % token}, timeout=OPEN_TIMEOUT_S)
        return getattr(resp, "status_code", 0) == 200
    except Exception:
        logger.debug("Live transcript: could not delete the session", exc_info=True)
        return False


def pcm16(samples):
    """float32 samples (-1..1, a numpy array) as 16-bit little-endian PCM bytes."""
    import numpy as np
    arr = np.asarray(samples, dtype=np.float32)
    return (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def pcm24k(samples):
    """16 kHz float32 samples as 24 kHz 16-bit PCM (OpenAI realtime): linear interpolation, block by block."""
    import numpy as np
    arr = np.asarray(samples, dtype=np.float32)
    if not len(arr):
        return b""
    x = np.arange(int(len(arr) * OPENAI_RATE / SAMPLE_RATE)) * (SAMPLE_RATE / OPENAI_RATE)
    out = np.interp(x, np.arange(len(arr)), arr)
    return (np.clip(out, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class LiveSession:
    """One take's live transcript. start() opens it (LiveUnavailable if it can't), feed() streams audio, stop()
    returns the final text. on_partial(text) / on_final(text, language) / on_failed(reason) are called from the
    session's own thread."""

    def __init__(self, token_fn, language="auto", languages=(), vocabulary=(), *, on_partial=None, on_final=None,
                 on_failed=None, connect=None, opener=None, purpose="dictation", engine=ENGINE_GLADIA,
                 ender=None, on_utterance=None):
        self._token_fn = token_fn
        self.engine = ENGINE_OPENAI if engine == ENGINE_OPENAI else ENGINE_GLADIA
        self._headers = None
        self._token = ""
        self._ender = ender or end_session
        self._oa_partial = ""        # OpenAI: the transcript deltas of the turn so far
        # Every finished sentence with its span in the recording (seconds since the session began, across re-opened
        # sessions): on_utterance(text, language, start, end) - a meeting's live lines and "You" / "Them".
        self.on_utterance = on_utterance
        self._sent_s = 0.0           # audio sent so far (seconds)
        self._session_at = 0.0       # where the current server session's own clock starts (a re-opened one: later)
        self._commit_at = 0.0        # OpenAI: where the uncommitted turn starts
        self._commit_spans = []      # OpenAI: committed turns waiting for their item id, oldest first
        self._item_spans = {}        # OpenAI: item id -> (start, end)
        self._loud_rms = 0.0         # OpenAI meetings: how loud speech is (a pause is well below it)
        self._reopens = 0
        self._session_ids = []       # every Gladia session of this take (all deleted at the end)
        self._language, self._languages, self._vocabulary = language, tuple(languages or ()), tuple(vocabulary or ())
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_failed = on_failed
        self._connect = connect or WebSocket.connect
        self._opener = opener or open_session
        self._purpose = purpose
        self._queue = queue.Queue(maxsize=QUEUE_MAX_BLOCKS)
        self._ws = None
        self._url = ""
        self.session_id = ""
        self._finals = {}            # utterance id -> (start, text)
        self._order = []
        self._lock = threading.Lock()
        self._stopping = False
        self._done = threading.Event()
        self.failed = ""             # why the session gave up ("" while it works)
        self.dropped_audio = False   # some audio never reached Gladia (the queue was full, or a reconnect)
        self.language = ""
        self.languages = []          # every language Gladia gave a finished sentence, in order
        self._open_partial = False   # a sentence is being said (a partial came after the last final)
        self._ended_at = None        # when Gladia said end_recording (monotonic)
        self._last_msg_at = 0.0
        self._stop_at = None
        self.timings = {}            # ms after Stop: end_recording, last_final, ready (the log line)

    # ── what the recorder calls ──
    def _open(self):
        """A new server session and its connection (the first one, or a re-opened one): the WebSocket."""
        token = self._token_fn() if callable(self._token_fn) else self._token_fn
        self._token = token or ""
        if self.engine == ENGINE_OPENAI:
            got = self._opener(token, self._language, self._languages, self._vocabulary, purpose=self._purpose,
                               engine=ENGINE_OPENAI)
        else:
            got = self._opener(token, self._language, self._languages, self._vocabulary, purpose=self._purpose)
        self._url, self.session_id = got[0], got[1]
        if self.session_id:
            self._session_ids.append(self.session_id)
        self._headers = got[2] if len(got) > 2 else None
        try:
            return self._connect(self._url, headers=self._headers) if self._headers else self._connect(self._url)
        except Exception as e:
            raise LiveUnavailable("connect", str(e)) from e

    def start(self):
        ws = self._open()
        self._ws = ws
        if self._done.is_set():                     # stopped or cancelled while opening
            ws.close()
            return self
        threading.Thread(target=self._writer, daemon=True, name="gusan-live-send").start()
        threading.Thread(target=self._reader, daemon=True, name="gusan-live-read").start()
        return self

    def feed(self, samples):
        """A block of the recording (float32, 16 kHz mono). Never blocks: when the line can't keep up the oldest
        audio is not sent (the pieces still have it)."""
        if self.failed or self._stopping:
            return
        try:
            self._queue.put_nowait(pcm24k(samples) if self.engine == ENGINE_OPENAI else pcm16(samples))
        except queue.Full:
            self.dropped_audio = True

    def stop(self, timeout=STOP_TIMEOUT_S):
        """End the session and return the final text (all finished sentences, in spoken order). Waits at most
        ``timeout`` for Gladia's last sentences."""
        self._stopping = True
        self._stop_at = time.monotonic()
        if self._ws is None:
            # Still opening: nothing to wait for - the pieces carry this take.
            self.failed = self.failed or "not_ready"
            self._done.set()
            self._end_later()
            return ""
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            self.dropped_audio = True
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except queue.Empty:
                pass
        end = self._stop_at + timeout
        while not self._done.is_set():
            now = time.monotonic()
            if now >= end:
                break
            if self._ended_at is not None and not self._open_partial and now - self._last_msg_at >= QUIET_AFTER_END_S:
                break                                # every sentence is final: the text is ready
            self._done.wait(0.01)
        self.timings["ready"] = int((time.monotonic() - self._stop_at) * 1000)
        text = self.text()
        logger.info("Live transcript at Stop: %s ms (end_recording %s, last final %s, closed %s, languages %s)",
                    self.timings.get("ready"), self.timings.get("end_recording", "-"),
                    self.timings.get("last_final", "-"), "yes" if self._done.is_set() else "no",
                    ",".join(self.languages) or "-")
        # The connection closes on its own after Gladia's post-processing; the reader logs a sentence that came late.
        closer = threading.Timer(STOP_TIMEOUT_S, self._close_now)
        closer.daemon = True                         # never holds the app open at quit
        closer.start()
        self._end_later()
        return text

    def _close_now(self):
        self._done.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            ws.close()

    def _end_later(self):
        """Gladia keeps a live session's audio and text: once the connection is over, the server deletes it."""
        ids = list(self._session_ids) or ([self.session_id] if self.session_id else [])
        if self.engine != ENGINE_GLADIA or not ids or getattr(self, "_ending", False):
            return
        self._ending = True

        def run():
            self._done.wait(STOP_TIMEOUT_S + 2)
            time.sleep(1.0)                          # Gladia's own post-processing ends with the close
            for sid in ids:
                if not self._ender(self._token, sid):
                    time.sleep(15)
                    self._ender(self._token, sid)
        threading.Thread(target=run, daemon=True, name="gusan-live-end").start()

    def _since_stop(self):
        return int((time.monotonic() - self._stop_at) * 1000) if self._stop_at is not None else None

    def cancel(self):
        """Drop the session now (Esc, a discarded take)."""
        self._stopping = True
        self._done.set()
        ws = self._ws
        if ws is not None:
            ws.close()
        self._end_later()

    def text(self):
        with self._lock:
            items = sorted((self._finals[i] for i in self._order), key=lambda x: x[0])
        return " ".join(t for _s, t in items if t).strip()

    # ── inside ──
    def _writer(self):
        pending = b""
        while True:
            try:
                block = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._done.is_set():
                    return
                continue
            if block is None:
                if pending:
                    self._send_audio(pending)
                if self.engine == ENGINE_OPENAI:
                    if self._sent_s - self._commit_at > 0.05:
                        self._commit()
                    elif not self._commit_spans:
                        self._done.set()                 # everything said is committed: nothing more comes
                else:
                    self._send_control({"type": "stop_recording"})
                return
            pending += block
            if len(pending) >= (OPENAI_SEND_EVERY_BYTES if self.engine == ENGINE_OPENAI else SEND_EVERY_BYTES):
                self._send_audio(pending)
                if self.engine == ENGINE_OPENAI and self._purpose == "meeting":
                    self._maybe_commit(pending)
                pending = b""

    def _commit(self):
        """OpenAI: the audio since the last commit is one turn: its text comes back as one finished sentence."""
        self._commit_spans.append((self._commit_at, self._sent_s))
        self._commit_at = self._sent_s
        self._send_control({"type": "input_audio_buffer.commit"})

    def _maybe_commit(self, block):
        """An OpenAI meeting: commit the turn at a pause (a block well below speech loudness) once it is TURN_MIN_S
        long, and every TURN_MAX_S whatever happens (OpenAI's live model has no turn detection of its own)."""
        samples = array.array("h")
        try:
            samples.frombytes(block[:len(block) - len(block) % 2])
        except (ValueError, TypeError):
            return
        if sys.byteorder != "little":
            samples.byteswap()                   # the PCM is little-endian
        if not samples:
            return
        rms = math.sqrt(sum(x * x for x in samples) / float(len(samples)))
        if rms > 600.0:
            self._loud_rms = rms if not self._loud_rms else 0.9 * self._loud_rms + 0.1 * rms
        turn = self._sent_s - self._commit_at
        quiet = rms < max(250.0, 0.25 * self._loud_rms)
        if (turn >= TURN_MIN_S and quiet) or turn >= TURN_MAX_S:
            self._commit()

    def _send_audio(self, data):
        ws = self._ws
        if ws is None or self.failed:
            return
        try:
            if self.engine == ENGINE_OPENAI:
                ws.send_text(json.dumps({"type": "input_audio_buffer.append",
                                         "audio": base64.b64encode(data).decode("ascii")}))
            else:
                ws.send_binary(data)
            self._sent_s += len(data) / float(BYTES_PER_SECOND[self.engine])
        except (OSError, WebSocketError):
            self.dropped_audio = True          # the reader reconnects (or gives up)

    def _send_control(self, message):
        ws = self._ws
        if ws is None:
            return
        try:
            ws.send_text(json.dumps(message))
        except (OSError, WebSocketError):
            pass

    def _reader(self):
        reconnects = 0
        while not self._done.is_set():
            ws = self._ws
            try:
                op, payload = ws.recv()
            except (OSError, WebSocketError) as e:
                op, payload = None, str(e)
            if op == OP_TEXT:
                self._handle(payload)
                continue
            if op == OP_BINARY:
                continue
            # closed: on purpose (we stopped) or a drop
            meeting = self._purpose == "meeting"
            if self._stopping or (op == OP_CLOSE and payload == 1000 and not meeting):
                self._done.set()
                return
            if meeting and (op == OP_CLOSE or reconnects >= 1 or self.engine == ENGINE_OPENAI) \
                    and self._reopens < MEETING_REOPENS:
                # A meeting goes on: a new server session (its sentences' times continue from here).
                self._reopens += 1
                try:
                    ws = self._open()
                    self._ws = ws
                    self._session_at = self._sent_s
                    self._commit_at, self._commit_spans, self._item_spans = self._sent_s, [], {}
                    self.dropped_audio = True
                    logger.info("Live transcript: a new session for the meeting (%d)", self._reopens)
                    continue
                except Exception:
                    logger.info("Live transcript: could not open a new session", exc_info=True)
                    self._give_up("dropped")
                    return
            if reconnects < 1 and self.engine == ENGINE_GLADIA:
                reconnects += 1
                try:
                    self._ws = self._connect(self._url)           # Gladia resumes the same session
                    self.dropped_audio = True
                    logger.info("Live transcript: reconnected")
                    continue
                except Exception:
                    logger.info("Live transcript: could not reconnect", exc_info=True)
                    if self._purpose == "meeting":
                        continue                         # a meeting: a new session next (above)
            self._give_up("dropped")
            return

    def _give_up(self, reason):
        self.failed = reason or "failed"
        self._done.set()
        cb = self.on_failed
        if cb is not None:
            try:
                cb(self.failed)
            except Exception:
                logger.debug("on_failed failed", exc_info=True)

    def _handle(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
        self._last_msg_at = time.monotonic()
        if self.engine == ENGINE_OPENAI:
            self._handle_openai(kind, msg)
            return
        if msg.get("error") and kind not in ("transcript",):
            logger.info("Live transcript: Gladia said %s", str(msg.get("error"))[:200])
        if kind == "end_session":
            self._done.set()
            return
        if kind == "end_recording":
            if self._ended_at is None:
                self._ended_at = time.monotonic()
                self.timings["end_recording"] = self._since_stop()
            return
        if kind != "transcript":
            return
        utt = data.get("utterance") if isinstance(data.get("utterance"), dict) else {}
        text = " ".join(str(utt.get("text") or "").split())
        if not text:
            return
        if data.get("is_final"):
            if "ready" in self.timings:
                logger.info("Live transcript: a sentence came %s ms after Stop, after the text was used",
                            self._since_stop())
            self._open_partial = False
            if self._stop_at is not None:
                self.timings["last_final"] = self._since_stop()
            uid = str(data.get("id") or len(self._order))
            try:
                start = float(utt.get("start") or 0.0)
            except (TypeError, ValueError):
                start = 0.0
            try:
                end = float(utt.get("end") or start)
            except (TypeError, ValueError):
                end = start
            start, end = start + self._session_at, end + self._session_at
            uid = "%d:%s" % (self._reopens, uid)
            with self._lock:
                if uid not in self._finals:
                    self._order.append(uid)
                self._finals[uid] = (start, text)
            lang = str(utt.get("language") or "")
            if lang:
                self.language = lang
                if lang not in self.languages:
                    self.languages.append(lang)
            self._utterance(text, lang, start, end)
            cb = self.on_final
            if cb is not None:
                try:
                    cb(text, lang)
                except Exception:
                    logger.debug("on_final failed", exc_info=True)
        else:
            self._open_partial = True
            cb = self.on_partial
            if cb is not None:
                try:
                    cb(text)
                except Exception:
                    logger.debug("on_partial failed", exc_info=True)


    # OpenAI realtime transcription: deltas while speaking, the text once the committed turn is complete.
    def _utterance(self, text, lang, start, end):
        cb = self.on_utterance
        if cb is not None:
            try:
                cb(text, lang, start, end)
            except Exception:
                logger.debug("on_utterance failed", exc_info=True)

    def _handle_openai(self, kind, msg):
        if kind == "input_audio_buffer.committed":
            if self._commit_spans:
                self._item_spans[str(msg.get("item_id") or "")] = self._commit_spans.pop(0)
            return
        if kind == "conversation.item.input_audio_transcription.delta":
            self._oa_partial += str(msg.get("delta") or "")
            self._open_partial = True
            cb = self.on_partial
            if cb is not None and self._oa_partial.strip():
                try:
                    cb(" ".join(self._oa_partial.split()))
                except Exception:
                    logger.debug("on_partial failed", exc_info=True)
            return
        if kind == "conversation.item.input_audio_transcription.completed":
            text = " ".join(str(msg.get("transcript") or "").split())
            self._open_partial = False
            self._oa_partial = ""
            if self._stop_at is not None:
                self.timings["last_final"] = self._since_stop()
            item = str(msg.get("item_id") or "")
            span = self._item_spans.pop(item, None) or (self._commit_spans.pop(0) if self._commit_spans else None)
            if text:
                uid = item or str(len(self._order))
                start, end = span if span else (float(len(self._order)), float(len(self._order)))
                with self._lock:
                    if uid not in self._finals:
                        self._order.append(uid)
                    self._finals[uid] = (start, text)
                lang = self._language if self._language not in ("", "auto", "multi") else ""
                if lang:
                    self.language = lang
                    if lang not in self.languages:
                        self.languages.append(lang)
                self._utterance(text, lang, start, end)
                cb = self.on_final
                if cb is not None:
                    try:
                        cb(text, lang)
                    except Exception:
                        logger.debug("on_final failed", exc_info=True)
            if self._stopping and not self._commit_spans and not self._item_spans:
                self._done.set()                     # every committed turn is complete: the text is ready
            return
        if kind == "error":
            err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
            if err.get("code") == "input_audio_buffer_commit_empty":
                if self._commit_spans:
                    self._commit_spans.pop(0)             # that turn had no audio after all
                if self._stopping and not self._commit_spans and not self._item_spans:
                    self._done.set()
                return
            logger.info("Live transcript: OpenAI said %s", str(err.get("code") or err.get("message") or "")[:160])
            if self._stopping or err.get("type") not in ("invalid_request_error",):
                self._give_up("openai_error")



def wait_until(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()
