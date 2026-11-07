"""Real-time speech to text (live_stt.py) against a small fake Gladia: a real WebSocket on 127.0.0.1 - the handshake,
masked audio frames in, partial / final utterances out, stop_recording, a dropped line and its one reconnect."""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import live_stt  # noqa: E402

_PCM16 = live_stt.pcm16          # the real one (setUp patches the module's for the line tests)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_exact(conn, n):
    data = b""
    while len(data) < n:
        more = conn.recv(n - len(data))
        if not more:
            raise ConnectionError("closed")
        data += more
    return data


def _read_frame(conn):
    b1, b2 = _recv_exact(conn, 2)
    opcode, n = b1 & 0x0F, b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", _recv_exact(conn, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _recv_exact(conn, 8))[0]
    key = _recv_exact(conn, 4) if b2 & 0x80 else b""
    payload = bytearray(_recv_exact(conn, n))
    if key:
        for i in range(n):
            payload[i] ^= key[i % 4]
    return opcode, bytes(payload)


def _send(conn, opcode, payload):
    n = len(payload)
    head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack("!H", n))
    conn.sendall(head + payload)


class FakeGladia:
    """One connection at a time. ``script(conn, audio)`` runs after each handshake: it gets the bytes received so far."""

    def __init__(self, on_audio=None, drop_after=None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.audio = bytearray()
        self.texts = []
        self.connections = 0
        self.on_audio = on_audio
        self.drop_after = drop_after          # close the FIRST connection after this many audio frames
        self.final_delay = 0.0                # stop_recording -> the last final
        self.close_delay = 0.0                # the last final -> the close (post-processing)
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self):
        return "ws://127.0.0.1:%d/v2/live?token=abc" % self.port

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._client, args=(conn, self.connections), daemon=True).start()

    def _client(self, conn, number):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(1024)
            key = [ln.split(":", 1)[1].strip() for ln in head.decode().split("\r\n")
                   if ln.lower().startswith("sec-websocket-key")][0]
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
            frames = 0
            while True:
                op, payload = _read_frame(conn)
                if op == 0x2:
                    self.audio += payload
                    frames += 1
                    if self.on_audio:
                        self.on_audio(lambda msg: _send(conn, 0x1, json.dumps(msg).encode()), frames)
                    if number == 1 and self.drop_after and frames >= self.drop_after:
                        conn.close()                       # the line drops (no close frame)
                        return
                elif op == 0x1:
                    self.texts.append(payload.decode())
                    if json.loads(payload).get("type") == "stop_recording":
                        _send(conn, 0x1, json.dumps({"type": "end_recording", "data": {}}).encode())
                        time.sleep(self.final_delay)
                        _send(conn, 0x1, json.dumps(_final("u9", "The end.", 9.0)).encode())
                        time.sleep(self.close_delay)               # Gladia's post-processing
                        _send(conn, 0x8, struct.pack("!H", 1000))
                        conn.close()
                        return
                elif op == 0x8:
                    conn.close()
                    return
        except (OSError, ConnectionError, IndexError):
            pass

    def close(self):
        self.sock.close()


def _final(uid, text, start, lang="en"):
    return {"type": "transcript", "data": {"id": uid, "is_final": True,
                                            "utterance": {"text": text, "start": start, "end": start + 1, "language": lang}}}


def _partial(uid, text):
    return {"type": "transcript", "data": {"id": uid, "is_final": False, "utterance": {"text": text, "start": 0}}}


def _audio(seconds=0.1):
    """A block of the recording as the session would get it, already as PCM bytes (pcm16 is patched to pass bytes
    through: these tests need no numpy - the suite may stub it)."""
    return bytes([0x10, 0x00]) * int(16000 * seconds)


def _real_numpy():
    """The numpy that pcm16 imports when the test runs (tests/test_core.py may have stubbed it since collection)."""
    try:
        cur = sys.modules.get("numpy", np)
        return cur.__name__ == "numpy" and tuple(cur.zeros(2).shape) == (2,)
    except Exception:
        return False


class TestLiveSession(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(live_stt, "pcm16", lambda samples: bytes(samples))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self, server, **kw):
        partials, finals, failed = [], [], []
        s = live_stt.LiveSession(lambda: "tok", "en", opener=lambda *a, **k: (server.url, "sess-1"),
                                 on_partial=partials.append, on_final=lambda t, l: finals.append((t, l)),
                                 on_failed=failed.append, **kw)
        return s, partials, finals, failed

    def test_words_come_back_while_speaking_and_stop_returns_every_final_in_order(self):
        def script(send, frames):
            if frames == 1:
                send(_partial("u1", "Hello"))
            if frames == 2:
                send(_final("u2", "and goodbye.", 2.0))
                send(_final("u1", "Hello everyone,", 0.0))       # arrives later, spoken first
        server = FakeGladia(on_audio=script)
        self.addCleanup(server.close)
        s, partials, finals, failed = self._session(server)
        s.start()
        for _ in range(4):
            s.feed(_audio(0.1))                                  # 100 ms blocks: one frame each
        self.assertTrue(live_stt.wait_until(lambda: len(finals) >= 2, 3))
        self.assertEqual(partials, ["Hello"])
        text = s.stop(timeout=3)
        self.assertEqual(text, "Hello everyone, and goodbye. The end.")
        self.assertEqual(failed, [])
        self.assertIn('"stop_recording"', "".join(server.texts))
        self.assertGreaterEqual(len(server.audio), 3200 * 3)         # 16-bit PCM arrived
        self.assertEqual(s.language, "en")

    def test_the_audio_is_16_bit_little_endian_pcm(self):
        if not _real_numpy():
            self.skipTest("real numpy not importable (stubbed)")
        self.assertEqual(_PCM16(np.array([0.0, 1.0, -1.0, 2.0], dtype=np.float32)),
                         struct.pack("<4h", 0, 32767, -32767, 32767))

    def test_a_dropped_line_is_reconnected_once_and_the_session_goes_on(self):
        sent = []

        def script(send, frames):
            if server.connections >= 2 and not sent:              # the first audio after the reconnect
                sent.append(1)
                send(_final("u1", "Still here.", 0.5))
        server = FakeGladia(on_audio=script, drop_after=1)
        self.addCleanup(server.close)
        s, _p, finals, failed = self._session(server)
        s.start()
        s.feed(_audio(0.1))
        self.assertTrue(live_stt.wait_until(lambda: server.connections >= 2, 3))
        end = time.monotonic() + 5
        while not finals and time.monotonic() < end:           # audio sent during the reconnect itself is lost
            s.feed(_audio(0.1))
            time.sleep(0.05)
        self.assertTrue(finals)
        self.assertEqual(s.stop(timeout=3), "Still here. The end.")
        self.assertEqual(failed, [])
        self.assertTrue(s.dropped_audio)                          # some audio may be missing: the caller knows

    def test_stop_does_not_wait_for_gladias_post_processing(self):
        def script(send, frames):
            if frames == 1:
                send(_partial("u1", "Hello"))
            if frames == 2:
                send(_final("u1", "Hello.", 0.0))
        server = FakeGladia(on_audio=script)
        server.close_delay = 3.0
        self.addCleanup(server.close)
        s, _p, finals, _f = self._session(server)
        s.start()
        for _ in range(3):
            s.feed(_audio(0.1))
        self.assertTrue(live_stt.wait_until(lambda: finals, 3))
        started = time.monotonic()
        self.assertEqual(s.stop(timeout=5), "Hello. The end.")
        self.assertLess(time.monotonic() - started, 1.0)              # not the 3 s of post-processing

    def test_a_sentence_still_being_said_at_stop_is_waited_for(self):
        def script(send, frames):
            if frames == 1:
                send(_partial("u9", "The"))                               # u9 closes only after stop_recording
        server = FakeGladia(on_audio=script)
        server.final_delay = 0.4
        self.addCleanup(server.close)
        s, partials, _f, _x = self._session(server)
        s.start()
        s.feed(_audio(0.1))
        self.assertTrue(live_stt.wait_until(lambda: partials, 3))
        self.assertEqual(s.stop(timeout=5), "The end.")
        self.assertGreaterEqual(s.timings["ready"], 350)

    def test_stop_while_still_opening_returns_at_once(self):
        gate = threading.Event()

        def opener(*a, **k):
            gate.wait(3)
            return ("ws://127.0.0.1:9/x", "")
        s = live_stt.LiveSession(lambda: "tok", opener=opener, connect=lambda url: mock.Mock(closed=False))
        threading.Thread(target=lambda: s.start(), daemon=True).start()
        s.feed(_audio(0.1))
        started = time.monotonic()
        self.assertEqual(s.stop(timeout=3), "")
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(s.failed, "not_ready")
        gate.set()

    def test_no_session_when_the_server_refuses(self):
        def opener(*a, **k):
            raise live_stt.LiveUnavailable("pro_required")
        s = live_stt.LiveSession(lambda: "tok", opener=opener)
        with self.assertRaises(live_stt.LiveUnavailable):
            s.start()

    def test_open_session_asks_the_server_and_only_accepts_a_gladia_websocket(self):
        calls = []

        class Resp:
            def __init__(self, status, data):
                self.status_code, self._data = status, data

            def json(self):
                return self._data

        def post(url, json=None, headers=None, timeout=None):
            calls.append((url, json, headers))
            return Resp(200, {"url": "wss://api.gladia.io/v2/live?token=t", "id": "s1"})
        url, sid = live_stt.open_session("tok", "fr", (), ["Gusan"], post=post, purpose="meeting")
        self.assertEqual((url, sid), ("wss://api.gladia.io/v2/live?token=t", "s1"))
        self.assertEqual(calls[0][0], live_stt.LIVE_FN_URL)
        self.assertEqual(calls[0][1]["purpose"], "meeting")
        self.assertEqual(calls[0][1]["vocabulary"], ["Gusan"])
        self.assertEqual(calls[0][2]["Authorization"], "Bearer tok")
        for bad in ("wss://evil.example/x", "ws://api.gladia.io/x", "https://api.gladia.io/v2/live"):
            with self.assertRaises(live_stt.LiveUnavailable):
                live_stt.open_session("tok", post=lambda *a, b=bad, **k: Resp(200, {"url": b}))
        with self.assertRaises(live_stt.LiveUnavailable) as e:
            live_stt.open_session("tok", post=lambda *a, **k: Resp(403, {"error": "pro_required"}))
        self.assertEqual(e.exception.code, "pro_required")
        with self.assertRaises(live_stt.LiveUnavailable):
            live_stt.open_session("")                               # signed out: nothing is asked

    def test_feeding_never_blocks_when_the_line_is_slow(self):
        s = live_stt.LiveSession(lambda: "tok", opener=lambda *a, **k: ("ws://x", ""))
        start = time.perf_counter()
        for _ in range(live_stt.QUEUE_MAX_BLOCKS + 50):            # never started: nobody drains the queue
            s.feed(_audio(0.02))
        self.assertLess(time.perf_counter() - start, 2.0)
        self.assertTrue(s.dropped_audio)


class TestServerFunction(unittest.TestCase):
    SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supabase", "functions",
                       "live-stt", "index.ts")

    def test_the_session_follows_gladias_recommended_parameters(self):
        src = open(self.SRC, encoding="utf-8").read()
        self.assertIn('endpointing: purpose === "meeting" ? 0.3 : 0.08', src)
        self.assertIn("maximum_duration_without_endpointing: 15", src)
        self.assertIn("receive_partial_transcripts: true", src)
        self.assertIn('model: "solaria-1"', src)
        self.assertIn('encoding: "wav/pcm", sample_rate: 16000, bit_depth: 16, channels: 1', src)
        self.assertIn("return { languages: [language], code_switching: false }", src)
        self.assertIn("return { languages: [], code_switching: false }", src)      # never switching over 100+ languages
        self.assertIn("custom_vocabulary: true, custom_vocabulary_config: { vocabulary, default_intensity: 0.5 }", src)

    def test_the_key_stays_on_the_server_and_only_a_gladia_websocket_goes_back(self):
        src = open(self.SRC, encoding="utf-8").read()
        self.assertIn('"x-gladia-key": key', src)
        self.assertIn('redirect: "error"', src)
        self.assertIn('wss.startsWith("wss://")', src)
        self.assertIn('if (proRes.data !== true) return json({ error: "pro_required" }, 403);', src)
        self.assertIn("use_cloud_quota", src)


if __name__ == "__main__":
    unittest.main()
