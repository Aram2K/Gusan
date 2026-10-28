"""The local Armenian speech model (speech_hy.py, Gusan 2.0.1): where it can come from, how it is
downloaded and unpacked safely, who may use it, and the recipe that reads Armenian well.

Nothing here touches the real network or the real app data: a throwaway http.server stands in for
the host (a tiny fake archive with its own pins), and a fake recognizer for sherpa-onnx. The one
real-model test (TestRealModel) is skipped when the converted files are not on this computer.
"""
import hashlib
import http.client
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Never the real settings, keyring or app data.
os.environ.setdefault("TRANSCRIBE_APP_DATA_DIR", tempfile.mkdtemp(prefix="gusan-hy-test-data-"))
os.environ.setdefault("TRANSCRIBE_DISABLE_KEYRING", "1")
os.environ.setdefault("TRANSCRIBE_SKIP_MIGRATION", "1")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import speech_hy  # noqa: E402


def _real_numpy():
    # tests/test_core.py stubs numpy suite-wide: the audio tests only run with the real one.
    try:
        import numpy as np
        return np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,)
    except Exception:
        return False


needs_numpy = unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")

SR = 16000

# What nemo_work/release/SHA256SUMS.txt says about the package (the pins in speech_hy.py must be
# exactly these: a typo there would make every download fail its check).
REAL_FILES = {
    "encoder.int8.onnx": (131_113_824, "38ca762e242295a2d57c0a0a55da628eeabb3f0b5d5309ac355aaa3385cec3c8"),
    "decoder.int8.onnx": (3_955_863, "00766e155e6236005dc16cebedac713b92dbe3d6db0b1094e2aa62bd5ef55fdb"),
    "joiner.int8.onnx": (1_408_182, "6973a71aae6e68b963eb181023e6dd4754fea4562b2fae3fa687fbb235e6ebd5"),
    "tokens.txt": (12_946, "221b1dd978a2d8f4fe8a8e185e400c09f98d71c887de9099cb2f1154d321416b"),
    "NOTICE.txt": (1_710, "46a5c5213e80b24552c39a1b6a6cfd48ddec164b1bbde1b7e648eb76bef56067"),
}
REAL_ARCHIVE = ("309509e3cac257c5c675bec5e8145d85ad25ca46c8e99d31f8f3fe7f9428ebf1", 105_462_960)

DIR_NAME = "sherpa-onnx-test-model"
FILES = {
    "encoder.int8.onnx": b"E" * 3000,
    "decoder.int8.onnx": b"D" * 700,
    "joiner.int8.onnx": b"J" * 500,
    "tokens.txt": b"a 0\nb 1\n",
    "NOTICE.txt": b"Model by NVIDIA, CC BY 4.0\n",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_zip(files=FILES, dir_name=DIR_NAME, extra=None, skip=(), dirs=True):
    """The fake archive's bytes: one top folder with the files (what tests break on purpose)."""
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if dirs:
            z.writestr(dir_name + "/", b"")
        for name, data in files.items():
            if name not in skip:
                z.writestr(dir_name + "/" + name, data)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def make_pins(archive, files=FILES, dir_name=DIR_NAME):
    return speech_hy.Pins(dir_name=dir_name, files={n: (len(d), sha(d)) for n, d in files.items()},
                          archive_sha256=sha(archive), archive_size=len(archive), needed=tuple(files))


class Host:
    """A throwaway web server on 127.0.0.1: ``routes`` maps a path to a function(handler)."""

    def __init__(self):
        self.routes = {}
        self.requests = []                       # (path, Range header)
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Range")))
                route = outer.routes.get(self.path.split("?")[0])
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    route(self)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # routes
    def serve(self, path, body, ranges=True, content_type="application/octet-stream"):
        def route(h):
            start = 0
            rng = h.headers.get("Range")
            if ranges and rng and rng.startswith("bytes="):
                start = int(rng[6:].split("-")[0])
            data = body[start:]
            h.send_response(206 if start else 200)
            if start:
                h.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(body) - 1, len(body)))
            h.send_header("Content-Type", content_type)
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
        self.routes[path] = route

    def serve_cut(self, path, body, cut):
        """Promise all of it, send ``cut`` bytes of the first request, then hang up. Later (Range)
        requests are served whole."""
        state = {"first": True}

        def route(h):
            rng = h.headers.get("Range")
            if state["first"] and not rng:
                state["first"] = False
                h.send_response(200)
                h.send_header("Content-Length", str(len(body)))
                h.end_headers()
                h.wfile.write(body[:cut])
                h.wfile.flush()
                h.close_connection = True
                return
            start = int(rng[6:].split("-")[0]) if rng else 0
            data = body[start:]
            h.send_response(206 if start else 200)
            if start:
                h.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(body) - 1, len(body)))
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)
        self.routes[path] = route

    def redirect(self, path, to):
        def route(h):
            h.send_response(302)
            h.send_header("Location", to)
            h.send_header("Content-Length", "0")
            h.end_headers()
        self.routes[path] = route

    def status(self, path, code):
        def route(h):
            h.send_response(code)
            h.send_header("Content-Length", "0")
            h.end_headers()
        self.routes[path] = route


class _Base(unittest.TestCase):
    """A fresh models folder, a runtime that "is installed", and no cached state between tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gusan-hy-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patches = [
            mock.patch.object(speech_hy, "models_dir", lambda: os.path.join(self.tmp, "models")),
            mock.patch.object(speech_hy, "_runtime_cache", ("ok", "1.13.8")),
            mock.patch.object(speech_hy, "ARMENIAN_MODEL_URL", ""),
            mock.patch.object(speech_hy, "_ENGINE", None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.archive = make_zip()
        self.pins = make_pins(self.archive)
        self.hosts = []

    def host(self):
        h = Host()
        self.hosts.append(h)
        self.addCleanup(h.close)
        return h

    def policy(self, host=None, **kw):
        base = dict(manifest_url="%s/manifest.json" % host.base if host else "https://gusan.aibuben.xyz/models/manifest.json",
                    manifest_hosts=("127.0.0.1",) if host else ("gusan.aibuben.xyz",),
                    file_hosts=("127.0.0.1",) if host else speech_hy.POLICY.file_hosts,
                    redirect_suffixes=(), require_https=not host, timeout=5.0, proxies=False)
        base.update(kw)
        return speech_hy.Policy(**base)

    def manifest(self, url, pins=None, **over):
        pins = pins or self.pins
        entry = {"url": url, "sha256": pins.archive_sha256, "size": pins.archive_size, "version": "1"}
        entry.update(over)
        return json.dumps({"models": {speech_hy.MANIFEST_KEY: entry}}).encode()


# ── what the package is ─────────────────────────────────────────────────────────

class TestPins(unittest.TestCase):
    def test_the_pinned_files_are_exactly_the_converted_package(self):
        self.assertEqual(dict(speech_hy.MODEL_FILES), REAL_FILES)
        self.assertEqual(speech_hy.ARMENIAN_MODEL_SHA256, REAL_ARCHIVE[0])
        self.assertEqual(speech_hy.ARMENIAN_MODEL_SIZE, REAL_ARCHIVE[1])
        self.assertEqual(set(speech_hy.NEEDED_FILES), set(REAL_FILES))
        self.assertEqual(speech_hy.MODEL_DIR_NAME, "sherpa-onnx-nemo-hy-fastconformer-int8")

    def test_the_names_and_the_credit(self):
        self.assertEqual(speech_hy.MODEL_ID, "hy-fastconformer")
        self.assertTrue(speech_hy.is_hy_model("hy-fastconformer"))
        self.assertTrue(speech_hy.is_hy_model(" hy-fastconformer "))
        for other in ("", None, "small", "large-v3", "hy", "cloud", "own:google"):
            self.assertFalse(speech_hy.is_hy_model(other))
        c = speech_hy.credit()
        self.assertIn("NVIDIA", c["name"])
        self.assertEqual(c["license"], "CC BY 4.0")
        self.assertTrue(c["url"].startswith("https://huggingface.co/nvidia/"))
        self.assertTrue(c["license_url"].startswith("https://creativecommons.org/"))
        self.assertIn("NVIDIA", speech_hy.ROW_HINT)
        self.assertIn("CC BY 4.0", speech_hy.ROW_HINT)
        self.assertIn("NVIDIA", speech_hy.CREDIT_TEXT)
        for word in ("ONNX", "transducer", "CTC", "token", "sherpa"):                # no jargon in what people read
            for text in (speech_hy.ROW_HINT, speech_hy.CREDIT_TEXT, speech_hy.MSG_NOT_AVAILABLE,
                         speech_hy.MSG_NO_RUNTIME, speech_hy.MSG_UNSAFE, speech_hy.MSG_INCOMPLETE,
                         speech_hy.MSG_PRIVACY, speech_hy.MSG_NOT_READY, speech_hy.MSG_NETWORK):
                self.assertNotIn(word.lower(), text.lower())

    def test_the_model_is_not_in_the_whisper_catalog(self):
        import hardware
        self.assertNotIn(speech_hy.MODEL_ID, hardware.MODEL_INFO)
        self.assertEqual(hardware.info(speech_hy.MODEL_ID).real, "NVIDIA FastConformer Armenian")
        self.assertTrue(hardware.is_speech_id("small"))
        self.assertTrue(hardware.is_speech_id(speech_hy.MODEL_ID))
        self.assertFalse(hardware.is_speech_id("cloud"))

    def test_the_minimum_runtime_is_the_first_good_one(self):
        self.assertEqual(speech_hy.MIN_SHERPA, (1, 13, 5))
        self.assertEqual(speech_hy._parse_version("1.13.8"), (1, 13, 8))
        self.assertEqual(speech_hy._parse_version("1.13.8+cuda"), (1, 13, 8))
        self.assertIsNone(speech_hy._parse_version(""))
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("sherpa-onnx>=1.13.5", text)

    def test_the_registered_name_of_the_manifest_entry(self):
        self.assertEqual(speech_hy.MANIFEST_KEY, "hy-fastconformer-int8")
        self.assertEqual(speech_hy.POLICY.manifest_url, "https://gusan.aibuben.xyz/models/manifest.json")
        self.assertEqual(speech_hy.POLICY.manifest_hosts, ("gusan.aibuben.xyz",))
        self.assertEqual(set(speech_hy.POLICY.file_hosts), {
            "gusan.aibuben.xyz", "github.com", "objects.githubusercontent.com",
            "release-assets.githubusercontent.com", "huggingface.co"})
        self.assertEqual(speech_hy.POLICY.manifest_max_bytes, 64 * 1024)
        self.assertTrue(speech_hy.POLICY.require_https)


class TestImportIsLightAndSafe(unittest.TestCase):
    """Linux CI has no sherpa_onnx, no PySide6, and nothing here may open a connection by itself."""

    def _run(self, code):
        env = dict(os.environ, TRANSCRIBE_APP_DATA_DIR=tempfile.mkdtemp(prefix="gusan-hy-sub-"),
                   TRANSCRIBE_DISABLE_KEYRING="1", TRANSCRIBE_SKIP_MIGRATION="1")
        env.pop("PYTHONPATH", None)
        res = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True,
                             timeout=120, env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res.stdout

    def test_imports_without_sherpa_onnx_qt_or_numpy_and_hides_the_model(self):
        out = self._run(
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "sys.modules['sherpa_onnx'] = None\n"
            "sys.modules['PySide6'] = None\n"
            "import speech_hy\n"
            "loaded = [m for m in ('numpy', 'faster_whisper', 'ctranslate2') if m in sys.modules]\n"
            "print('LOADED', loaded)\n"
            "print('LISTED', speech_hy.listed(), speech_hy.runtime_problem(), speech_hy.status()['state'])\n"
            "print('COVERS', speech_hy.covers('hy'), speech_hy.covers('auto'))\n")
        self.assertIn("LOADED []", out)
        self.assertIn("LISTED False missing hidden", out)
        self.assertIn("COVERS True False", out)

    def test_nothing_opens_a_connection_until_asked(self):
        out = self._run(
            "import socket, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "def boom(*a, **k):\n"
            "    raise AssertionError('network used')\n"
            "socket.socket.connect = boom\n"
            "socket.create_connection = boom\n"
            "socket.getaddrinfo = boom\n"
            "import speech_hy\n"
            "speech_hy.ready(); speech_hy.has_source(); speech_hy.listed(); speech_hy.status()\n"
            "speech_hy.credit(); speech_hy.covers('hy'); speech_hy.fallback_whisper()\n"
            "speech_hy.engine(); speech_hy.loaded(); speech_hy.maybe_unload_idle(60)\n"
            "print('QUIET')\n")
        self.assertIn("QUIET", out)


# ── where the model may come from ───────────────────────────────────────────────

class TestUrlPolicy(_Base):
    def ok(self, url, hosts=None, **kw):
        return speech_hy.check_url(url, hosts or speech_hy.POLICY.file_hosts, speech_hy.POLICY, **kw)

    def refused(self, url, hosts=None, **kw):
        with self.assertRaises(speech_hy._Refused):
            self.ok(url, hosts, **kw)

    def test_https_on_an_allowed_host(self):
        for url in ("https://gusan.aibuben.xyz/models/hy.zip", "https://github.com/Aram2K/Gusan/releases/x.zip",
                    "https://huggingface.co/x/resolve/main/a.zip",
                    "https://objects.githubusercontent.com/a/b?token=1", "https://GITHUB.com/x",
                    "https://github.com:443/x"):
            self.ok(url)

    def test_everything_else_is_refused(self):
        self.refused("http://gusan.aibuben.xyz/models/hy.zip")                  # not https
        self.refused("ftp://gusan.aibuben.xyz/x")
        self.refused("file:///etc/passwd")
        self.refused("https://evil.example.com/hy.zip")
        self.refused("https://gusan.aibuben.xyz.evil.example.com/hy.zip")       # a look-alike
        self.refused("https://evilgithub.com/x")
        self.refused("https://user:pw@github.com/x")                            # credentials
        self.refused("https://github.com@evil.example.com/x")                   # the real host is the part after @
        self.refused("https://github.com:8443/x")                               # an unusual port
        self.refused("https://github.com/a b")                                  # a space
        self.refused("")
        self.refused(None)
        self.refused("https://github.com/" + "a" * 9000)
        self.ok("https://cas-bridge.xethub.hf.co/xet-bridge-us/a/b?Policy=" + "A" * 2600 + "&Signature=" + "B" * 350,
                redirect=True)                                                  # a signed CDN address is long

    def test_a_redirect_may_reach_the_hosts_own_cdn_and_nothing_else(self):
        self.ok("https://release-assets.githubusercontent.com/x", redirect=True)
        self.ok("https://cdn-lfs.huggingface.co/x", redirect=True)
        self.ok("https://cas-bridge.xethub.hf.co/x", redirect=True)
        self.refused("https://cdn.example.com/x", redirect=True)
        self.refused("https://cdn-lfs.huggingface.co/x")                       # only as a redirect


class TestManifest(_Base):
    def parse(self, data):
        return speech_hy.parse_manifest(data, self.pins, self.policy())

    def test_a_good_entry(self):
        entry = self.parse(self.manifest("https://gusan.aibuben.xyz/models/m.zip"))
        self.assertEqual(entry.url, "https://gusan.aibuben.xyz/models/m.zip")
        self.assertEqual(entry.sha256, self.pins.archive_sha256)
        self.assertEqual(entry.size, self.pins.archive_size)
        self.assertEqual(entry.version, "1")

    def test_it_can_only_name_the_archive_the_code_was_built_for(self):
        url = "https://gusan.aibuben.xyz/models/m.zip"
        self.assertIsNone(self.parse(self.manifest(url, sha256="0" * 64)))                   # another archive
        self.assertIsNone(self.parse(self.manifest(url, sha256=sha(b"x"))))
        self.assertIsNone(self.parse(self.manifest(url, size=self.pins.archive_size + 1)))   # another size
        self.assertIsNone(self.parse(self.manifest(url, sha256="zz")))
        self.assertIsNone(self.parse(self.manifest(url, sha256=None)))
        self.assertIsNone(self.parse(self.manifest(url, size="10")))
        self.assertIsNone(self.parse(self.manifest(url, size=True)))

    def test_the_address_must_be_allowed(self):
        for url in ("http://gusan.aibuben.xyz/models/m.zip", "https://evil.example.com/m.zip",
                    "https://user@gusan.aibuben.xyz/m.zip", "file:///x", "", None, 5):
            self.assertIsNone(self.parse(self.manifest(url)), url)

    def test_broken_manifests(self):
        for data in (b"", b"not json", b"[]", b"{}", b'{"models": []}', b'{"models": {}}',
                     b'{"models": {"hy-fastconformer-int8": "x"}}', b'{"models": {"other": {}}}',
                     b"\xff\xfe", json.dumps({"models": None}).encode()):
            self.assertIsNone(self.parse(data), data)

    def test_a_manifest_over_64_kb_is_ignored(self):
        good = self.manifest("https://gusan.aibuben.xyz/models/m.zip")
        self.assertIsNotNone(self.parse(good))
        self.assertIsNone(self.parse(good + b" " * (64 * 1024)))


class TestCatalogAndVisibility(_Base):
    def write_cache(self, entry, checked):
        speech_hy._write_cache(entry, checked)

    def entry(self, url="https://gusan.aibuben.xyz/models/m.zip"):
        return speech_hy.Entry(url, self.pins.archive_sha256, self.pins.archive_size, "1")

    def test_hidden_while_there_is_nowhere_to_get_it_from(self):
        self.assertFalse(speech_hy.ready(self.pins))
        self.assertFalse(speech_hy.has_source(self.pins))
        self.assertFalse(speech_hy.listed(self.pins))
        st = speech_hy.status(self.pins)
        self.assertEqual((st["state"], st["listed"], st["ready"]), ("hidden", False, False))

    def test_a_url_in_the_code_lists_it(self):
        with mock.patch.object(speech_hy, "ARMENIAN_MODEL_URL", "https://github.com/Aram2K/Gusan/releases/x.zip"):
            self.assertTrue(speech_hy.has_source())
            self.assertTrue(speech_hy.listed())
            st = speech_hy.status()
            self.assertEqual((st["state"], st["listed"], st["ready"], st["size_text"]), ("missing", True, False, "105 MB"))
        with mock.patch.object(speech_hy, "ARMENIAN_MODEL_URL", "https://evil.example.com/x.zip"):
            self.assertFalse(speech_hy.has_source())               # a URL that is not allowed lists nothing

    def test_a_cached_manifest_entry_lists_it(self):
        self.write_cache(self.entry(), time.time())
        self.assertTrue(speech_hy.has_source(self.pins, self.policy()))
        self.assertTrue(speech_hy.listed(self.pins, self.policy()))
        self.write_cache(None, time.time())
        self.assertFalse(speech_hy.listed(self.pins, self.policy()))

    def test_a_cache_edited_by_hand_is_checked_again(self):
        bad = speech_hy.Entry("https://evil.example.com/m.zip", self.pins.archive_sha256, self.pins.archive_size)
        self.write_cache(bad, time.time())
        self.assertFalse(speech_hy.has_source(self.pins, self.policy()))
        other = speech_hy.Entry("https://github.com/x.zip", "0" * 64, self.pins.archive_size)
        self.write_cache(other, time.time())
        self.assertFalse(speech_hy.has_source(self.pins, self.policy()))

    def test_a_model_on_disk_is_listed_without_any_source(self):
        d = os.path.join(self.tmp, "models", DIR_NAME)
        os.makedirs(d)
        for name, data in FILES.items():
            Path(d, name).write_bytes(data)
        self.assertTrue(speech_hy.ready(self.pins))
        self.assertTrue(speech_hy.listed(self.pins))
        self.assertEqual(speech_hy.status(self.pins)["state"], "ready")
        Path(d, "tokens.txt").write_bytes(b"short")                  # a file of the wrong size: not ready
        self.assertFalse(speech_hy.ready(self.pins))
        Path(d, "tokens.txt").unlink()
        self.assertFalse(speech_hy.ready(self.pins))

    def test_hidden_when_this_copy_cannot_run_it(self):
        self.write_cache(self.entry(), time.time())
        for state in ("missing", "old"):
            with mock.patch.object(speech_hy, "_runtime_cache", (state, "")):
                self.assertFalse(speech_hy.listed(self.pins, self.policy()))
                self.assertEqual(speech_hy.status(self.pins, self.policy())["state"], "hidden")
                self.assertEqual(speech_hy.runtime_problem(), state)
        d = os.path.join(self.tmp, "models", DIR_NAME)
        os.makedirs(d)
        for name, data in FILES.items():
            Path(d, name).write_bytes(data)
        with mock.patch.object(speech_hy, "_runtime_cache", ("old", "1.12.30")):
            self.assertFalse(speech_hy.listed(self.pins))             # on disk, but nothing to run it with

    def test_the_runtime_check_reads_the_installed_version(self):
        with mock.patch.object(speech_hy, "_runtime_cache", None):
            with mock.patch("importlib.util.find_spec", return_value=None):
                self.assertEqual(speech_hy._detect_runtime(), ("missing", ""))
            with mock.patch("importlib.util.find_spec", return_value=object()), \
                    mock.patch("importlib.metadata.version", return_value="1.12.26"):
                self.assertEqual(speech_hy._detect_runtime(), ("old", "1.12.26"))
            with mock.patch("importlib.util.find_spec", return_value=object()), \
                    mock.patch("importlib.metadata.version", return_value="1.13.5"):
                self.assertEqual(speech_hy._detect_runtime(), ("ok", "1.13.5"))
            with mock.patch("importlib.util.find_spec", return_value=object()), \
                    mock.patch("importlib.metadata.version", side_effect=Exception("no metadata")):
                self.assertEqual(speech_hy._detect_runtime()[0], "ok")      # a frozen app has none: the load checks

    def test_a_recognizer_that_is_too_old_is_refused_at_load(self):
        fake = SimpleNamespace(__version__="1.12.30", OfflineRecognizer=SimpleNamespace(from_transducer=lambda **k: None))
        with mock.patch.dict(sys.modules, {"sherpa_onnx": fake}):
            with self.assertRaises(speech_hy.NoRuntime):
                speech_hy._build_recognizer(self.tmp, 2)

    def test_the_recognizer_is_built_the_way_the_validation_ran_it(self):
        seen = {}

        def build(**kw):
            seen.update(kw)
            return "rec"
        fake = SimpleNamespace(__version__="1.13.8", OfflineRecognizer=SimpleNamespace(from_transducer=build))
        with mock.patch.dict(sys.modules, {"sherpa_onnx": fake}):
            self.assertEqual(speech_hy._build_recognizer(os.path.join(self.tmp, "m"), 2), "rec")
        self.assertEqual(seen["num_threads"], 2)
        self.assertEqual((seen["sample_rate"], seen["feature_dim"]), (16000, 80))
        self.assertEqual((seen["decoding_method"], seen["model_type"]), ("greedy_search", "nemo_transducer"))
        for key, name in (("encoder", "encoder.int8.onnx"), ("decoder", "decoder.int8.onnx"),
                          ("joiner", "joiner.int8.onnx"), ("tokens", "tokens.txt")):
            self.assertTrue(seen[key].endswith(name), seen[key])


class TestRefreshCatalog(_Base):
    def setUp(self):
        super().setUp()
        self.h = self.host()
        self.pol = self.policy(self.h)
        self.h.serve("/manifest.json", self.manifest(self.h.base + "/model.zip"))

    def refresh(self, **kw):
        return speech_hy.refresh_catalog(policy=self.pol, pins=self.pins, **kw)

    def listed(self):
        return speech_hy.listed(self.pins, self.pol)

    def test_fetches_caches_and_lists_the_model(self):
        self.assertFalse(self.listed())
        self.assertTrue(self.refresh())                                # the listing changed
        self.assertTrue(self.listed())
        self.assertEqual(len(self.h.requests), 1)
        self.assertFalse(self.refresh())                               # asked again at once: the cache answers
        self.assertEqual(len(self.h.requests), 1)

    def test_at_most_once_a_day_unless_forced(self):
        now = time.time()
        self.refresh(now=now)
        self.refresh(now=now + 3600)
        self.assertEqual(len(self.h.requests), 1)
        self.refresh(now=now + 25 * 3600)
        self.assertEqual(len(self.h.requests), 2)
        self.refresh(now=now + 25 * 3600 + 5, force=True)
        self.assertEqual(len(self.h.requests), 3)

    def test_nothing_published_is_remembered_for_a_day_too(self):
        self.h.status("/manifest.json", 404)
        now = time.time()
        self.assertFalse(self.refresh(now=now))
        self.assertFalse(self.listed())
        self.refresh(now=now + 60)
        self.assertEqual(len(self.h.requests), 1)

    def test_a_manifest_that_is_taken_back_unlists_the_model(self):
        now = time.time()
        self.assertTrue(self.refresh(now=now))
        self.h.status("/manifest.json", 404)
        self.assertTrue(self.refresh(now=now + 25 * 3600))
        self.assertFalse(self.listed())

    def test_privacy_mode_never_asks(self):
        self.assertFalse(self.refresh(privacy=True))
        self.assertEqual(self.h.requests, [])
        self.assertFalse(self.listed())

    def test_a_url_in_the_code_makes_the_lookup_unnecessary(self):
        with mock.patch.object(speech_hy, "ARMENIAN_MODEL_URL", self.h.base + "/model.zip"):
            self.assertFalse(self.refresh())
        self.assertEqual(self.h.requests, [])

    def test_a_model_that_is_already_here_needs_no_lookup(self):
        """Nothing is phoned home for a model that is on the disk: it is listed without any manifest."""
        d = os.path.join(self.tmp, "models", DIR_NAME)
        os.makedirs(d)
        for name, data in FILES.items():
            Path(d, name).write_bytes(data)
        self.assertTrue(self.listed())
        self.assertFalse(self.refresh())
        self.assertFalse(self.refresh(force=True))
        self.assertEqual(self.h.requests, [])
        self.assertFalse(os.path.exists(speech_hy._manifest_cache_path()))      # (not even a cache file)

    def test_no_lookup_on_a_copy_that_cannot_run_the_model(self):
        with mock.patch.object(speech_hy, "_runtime_cache", ("missing", "")):
            self.assertFalse(self.refresh())
        self.assertEqual(self.h.requests, [])

    def test_offline_is_quiet_and_tried_again_in_minutes_not_a_day(self):
        self.h.close()
        now = time.time()
        self.assertFalse(self.refresh(now=now))                        # never raises
        self.assertFalse(self.listed())
        cache = speech_hy._read_cache()
        retry_at = cache["checked"] + speech_hy.POLICY.ttl_seconds
        self.assertAlmostEqual(retry_at - now, 600, delta=2)           # a few minutes, not a day
        self.assertFalse(self.refresh(now=now + 100))                 # ... and not at once

    def test_a_manifest_of_the_wrong_kind_lists_nothing(self):
        for body in (self.manifest(self.h.base + "/model.zip", sha256="0" * 64),
                     self.manifest("https://evil.example.com/model.zip"),
                     b"<html>captive portal</html>", b"{}"):
            self.h.serve("/manifest.json", body)
            shutil.rmtree(os.path.join(self.tmp, "models"), ignore_errors=True)
            self.refresh(force=True)
            self.assertFalse(self.listed(), body[:40])

    def test_a_manifest_over_64_kb_is_ignored_whatever_it_says(self):
        self.h.serve("/manifest.json", self.manifest(self.h.base + "/model.zip") + b" " * (64 * 1024 + 10))
        self.refresh()
        self.assertFalse(self.listed())
        # ... also when the server does not say how long it is
        body = self.manifest(self.h.base + "/model.zip") + b" " * (64 * 1024 + 10)

        def chunked(h):
            h.send_response(200)
            h.send_header("Connection", "close")
            h.end_headers()
            h.wfile.write(body)
            h.close_connection = True
        self.h.routes["/manifest.json"] = chunked
        self.refresh(force=True)
        self.assertFalse(self.listed())

    def test_the_manifest_must_come_from_the_manifest_host(self):
        pol = self.policy(self.h, manifest_hosts=("gusan.aibuben.xyz",))
        self.assertFalse(speech_hy.refresh_catalog(policy=pol, pins=self.pins))
        self.assertEqual(self.h.requests, [])                         # a wrong host is not even asked
        pol = self.policy(self.h, require_https=True)                 # http on the manifest address
        self.assertFalse(speech_hy.refresh_catalog(policy=pol, pins=self.pins))
        self.assertEqual(self.h.requests, [])

    def test_a_redirect_to_another_host_is_refused(self):
        self.h.redirect("/manifest.json", "http://localhost:%d/manifest.json" % self.h.port)   # 127.0.0.1 -> localhost
        self.refresh()
        self.assertFalse(self.listed())
        self.assertEqual([r[0] for r in self.h.requests], ["/manifest.json"])


# ── downloading and unpacking ──────────────────────────────────────────────────

class TestExtract(_Base):
    def extract(self, data, **kw):
        zp = os.path.join(self.tmp, "a.zip")
        Path(zp).write_bytes(data)
        root = os.path.join(self.tmp, "models")
        os.makedirs(root, exist_ok=True)
        return root, speech_hy.extract_archive(zp, root, self.pins, **kw)

    def assertRefused(self, data, text=""):
        root = os.path.join(self.tmp, "models")
        with self.assertRaises(ValueError) as cm:
            self.extract(data)
        self.assertIn(text, str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(root, DIR_NAME)), "something was installed")
        self.assertFalse(os.path.exists(os.path.join(root, DIR_NAME + ".part")), "a leftover")
        self.assertEqual([n for n in os.listdir(root) if n != "a.zip"], [])

    def test_a_good_archive_installs_exactly_the_five_files(self):
        root, out = self.extract(self.archive)
        self.assertEqual(out, os.path.join(root, DIR_NAME))
        self.assertEqual(sorted(os.listdir(out)), sorted(FILES))
        for name, data in FILES.items():
            self.assertEqual(Path(out, name).read_bytes(), data)
        self.assertFalse(os.path.exists(out + ".part"))
        self.assertTrue(speech_hy.ready(self.pins))

    def test_a_member_outside_the_folder_is_refused(self):
        for name in ("../evil.txt", DIR_NAME + "/../evil.txt", "/abs/evil.txt", "C:/evil.txt", "C:evil.txt",
                     DIR_NAME + "\\..\\evil.txt", "\\evil.txt", "//server/share/evil.txt",
                     DIR_NAME + "/sub/evil.txt", "other/encoder.int8.onnx", "encoder.int8.onnx",
                     DIR_NAME + "/evil.txt", DIR_NAME + "/encoder.int8.onnx/x", DIR_NAME + "/.."):
            self.assertRefused(make_zip(extra={name: b"x"}), "")
        for name in ("../evil.txt", "/abs/evil.txt", "C:/evil.txt"):
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_an_archive_with_a_different_top_folder_is_refused(self):
        self.assertRefused(make_zip(dir_name="other-folder"))

    def test_a_link_is_refused(self):
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in FILES.items():
                z.writestr(DIR_NAME + "/" + name, data)
            link = zipfile.ZipInfo(DIR_NAME + "/linked.txt")
            link.external_attr = (0o120777 << 16)
            z.writestr(link, "/etc/passwd")
        self.assertRefused(buf.getvalue(), "")
        buf = io.BytesIO()                                  # a link named like a real file
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in FILES.items():
                if name == "tokens.txt":
                    info = zipfile.ZipInfo(DIR_NAME + "/tokens.txt")
                    info.external_attr = (0o120777 << 16)
                    z.writestr(info, "../../x")
                else:
                    z.writestr(DIR_NAME + "/" + name, data)
        self.assertRefused(buf.getvalue(), "link")

    def test_an_extra_missing_or_repeated_file_is_refused(self):
        self.assertRefused(make_zip(extra={DIR_NAME + "/readme.exe": b"MZ"}))
        self.assertRefused(make_zip(skip=("joiner.int8.onnx",)), "missing")
        import io
        import warnings
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in FILES.items():
                z.writestr(DIR_NAME + "/" + name, data)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")                       # "Duplicate name": that is the point
                z.writestr(DIR_NAME + "/tokens.txt", b"again")
        self.assertRefused(buf.getvalue(), "duplicate")

    def test_a_file_bigger_than_its_pin_is_refused_before_it_is_written(self):
        big = dict(FILES, **{"encoder.int8.onnx": b"E" * 10_000})
        self.assertRefused(make_zip(big), "too large")

    def test_a_header_that_lies_about_the_size_is_caught_while_writing(self):
        # The archive really holds 20 000 bytes for the encoder; its header says 3 000 (the pin).
        data = make_zip(dict(FILES, **{"encoder.int8.onnx": b"E" * 20_000}))
        zp = os.path.join(self.tmp, "lying.zip")
        Path(zp).write_bytes(data)
        os.makedirs(os.path.join(self.tmp, "models"))
        real_infolist = zipfile.ZipFile.infolist
        declared = len(FILES["encoder.int8.onnx"])

        def small_lie(zf):
            out = real_infolist(zf)
            for zi in out:
                if zi.filename.endswith("encoder.int8.onnx"):
                    zi.file_size = declared
            return out
        with mock.patch.object(zipfile.ZipFile, "infolist", small_lie):
            # (Python's own zipfile stops at the declared size and fails the CRC: either way it is refused)
            with self.assertRaises((ValueError, zipfile.BadZipFile)):
                speech_hy.extract_archive(zp, os.path.join(self.tmp, "models"), self.pins)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "models")), [])
        # ... and download() turns either into the plain "didn't pass the safety check" sentence
        h = self.host()
        h.serve("/model.zip", data)
        pins = make_pins(data)
        pins = speech_hy.Pins(dir_name=pins.dir_name, files=self.pins.files, archive_sha256=pins.archive_sha256,
                              archive_size=pins.archive_size, needed=pins.needed)
        entry = speech_hy.Entry(h.base + "/model.zip", pins.archive_sha256, pins.archive_size)
        with mock.patch.object(zipfile.ZipFile, "infolist", small_lie):
            with self.assertRaises(speech_hy.DownloadFailed) as cm:
                speech_hy.download(policy=self.policy(h), pins=pins, entry=entry)
        self.assertEqual(cm.exception.reason, "unsafe")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "models", DIR_NAME)))

    def test_a_file_that_is_not_the_pinned_one_is_refused(self):
        self.assertRefused(make_zip(dict(FILES, **{"joiner.int8.onnx": b"X" * len(FILES["joiner.int8.onnx"])})),
                           "hash")
        self.assertRefused(make_zip(dict(FILES, **{"joiner.int8.onnx": b"J" * 10})), "size")

    def test_an_encrypted_entry_and_not_a_zip_are_refused(self):
        data = bytearray(self.archive)
        # set the "encrypted" flag bit in every central directory header (0x02014b50)
        pos = 0
        while True:
            pos = data.find(b"PK\x01\x02", pos)
            if pos < 0:
                break
            data[pos + 8] |= 1
            pos += 4
        self.assertRefused(bytes(data))
        with self.assertRaises(zipfile.BadZipFile):
            self.extract(b"this is not a zip at all")

    def test_too_many_entries_are_refused(self):
        extra = {DIR_NAME + "/f%d" % i: b"x" for i in range(40)}
        self.assertRefused(make_zip(extra=extra), "too many")

    def test_a_failed_install_leaves_an_earlier_one_alone(self):
        root, out = self.extract(self.archive)
        before = {n: Path(out, n).read_bytes() for n in os.listdir(out)}
        with self.assertRaises(ValueError):
            speech_hy.extract_archive(os.path.join(self.tmp, "a.zip"), root, make_pins(self.archive, dict(FILES, **{"tokens.txt": b"zz"})))
        self.assertEqual({n: Path(out, n).read_bytes() for n in os.listdir(out)}, before)
        self.assertEqual(sorted(os.listdir(root)), sorted([DIR_NAME, "a.zip"]) if os.path.exists(os.path.join(root, "a.zip")) else [DIR_NAME])

    def test_an_install_over_an_earlier_one_replaces_it_in_one_step(self):
        root, out = self.extract(self.archive)
        Path(out, "stale.txt").write_bytes(b"old")
        root, out = self.extract(self.archive)
        self.assertEqual(sorted(os.listdir(out)), sorted(FILES))
        self.assertFalse(os.path.exists(out + ".old"))

    def test_cancel_while_unpacking_leaves_nothing(self):
        with self.assertRaises(speech_hy.Cancelled):
            self.extract(self.archive, should_cancel=lambda: True)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "models", DIR_NAME)))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "models", DIR_NAME + ".part")))


class TestDownload(_Base):
    def setUp(self):
        super().setUp()
        self.h = self.host()
        self.pol = self.policy(self.h)
        self.url = self.h.base + "/model.zip"
        self.h.serve("/model.zip", self.archive)
        self.entry = speech_hy.Entry(self.url, self.pins.archive_sha256, self.pins.archive_size, "1")

    def download(self, **kw):
        kw.setdefault("entry", self.entry)
        return speech_hy.download(policy=self.pol, pins=self.pins, **kw)

    def part_path(self):
        return os.path.join(self.tmp, "models", "hy-download", DIR_NAME + ".zip.part")

    def assertNothingInstalled(self):
        self.assertFalse(speech_hy.ready(self.pins))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "models", DIR_NAME)))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "models", DIR_NAME + ".part")))

    def test_downloads_checks_unpacks_and_installs(self):
        seen = []
        out = self.download(on_progress=lambda pct, done, total: seen.append((pct, done, total)))
        self.assertEqual(out, os.path.join(self.tmp, "models", DIR_NAME))
        self.assertTrue(speech_hy.ready(self.pins))
        self.assertFalse(os.path.exists(self.part_path()))                       # the archive is not kept
        pcts = [p for p, _d, _t in seen]
        self.assertEqual(pcts, sorted(pcts))
        self.assertEqual(pcts[-1], 100)
        self.assertTrue(all(0 <= p <= 100 for p in pcts))
        self.assertEqual(seen[-1][1:], (self.pins.archive_size, self.pins.archive_size))

    def test_already_there_is_not_downloaded_again(self):
        self.download()
        self.h.requests.clear()
        seen = []
        self.download(on_progress=lambda *a: seen.append(a))
        self.assertEqual(self.h.requests, [])
        self.assertEqual(seen[-1][0], 100)

    def test_a_wrong_archive_never_gets_unpacked(self):
        bad = bytes(len(self.archive))                                           # same length, other bytes
        self.h.serve("/model.zip", bad)
        with mock.patch.object(speech_hy, "extract_archive", side_effect=AssertionError("unpacked")):
            with self.assertRaises(speech_hy.DownloadFailed) as cm:
                self.download()
        self.assertEqual(cm.exception.reason, "bad_hash")
        self.assertEqual(str(cm.exception), speech_hy.MSG_UNSAFE)
        self.assertFalse(os.path.exists(self.part_path()))                       # not kept for a resume
        self.assertNothingInstalled()

    def test_an_archive_that_hashes_right_but_holds_a_bad_member_is_refused(self):
        evil = make_zip(extra={"../evil.txt": b"x"})
        pins = make_pins(evil)
        self.h.serve("/model.zip", evil)
        entry = speech_hy.Entry(self.url, pins.archive_sha256, pins.archive_size, "1")
        with self.assertRaises(speech_hy.DownloadFailed) as cm:
            speech_hy.download(policy=self.pol, pins=pins, entry=entry)
        self.assertEqual(cm.exception.reason, "unsafe")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "models", DIR_NAME)))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "models", "evil.txt")))

    def test_an_archive_that_hashes_right_but_cannot_be_read_is_refused_in_plain_words(self):
        # Right hash, but a member uses a compression this Python does not know (zipfile raises
        # NotImplementedError) or its stream is corrupt (zlib.error): never an unhandled exception.
        import struct
        good = make_zip()
        for label, patch in (("unknown method", lambda b: b.replace(struct.pack("<H", 8), struct.pack("<H", 99))),
                             ("corrupt stream", None)):
            with self.subTest(label):
                if patch is not None:
                    evil = bytearray(good)
                    # the compression method sits at offset 8 of every local header (PK\x03\x04) and
                    # at offset 10 of every central header (PK\x01\x02)
                    for sig, off in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
                        i = evil.find(sig)
                        while i != -1:
                            evil[i + off:i + off + 2] = struct.pack("<H", 99)
                            i = evil.find(sig, i + 4)
                    evil = bytes(evil)
                else:
                    evil = bytearray(good)
                    i = evil.find(b"PK\x03\x04")
                    name_len, extra_len = struct.unpack("<HH", bytes(evil[i + 26:i + 30]))
                    # the first FILE entry (the folder entry has no data): damage the middle of its data
                    j = evil.find(b"PK\x03\x04", i + 4)
                    name_len, extra_len = struct.unpack("<HH", bytes(evil[j + 26:j + 30]))
                    start = j + 30 + name_len + extra_len
                    evil[start + 2:start + 12] = b"\xff" * 10
                    evil = bytes(evil)
                pins = make_pins(evil)
                self.h.serve("/model.zip", evil)
                entry = speech_hy.Entry(self.url, pins.archive_sha256, pins.archive_size, "1")
                with self.assertRaises(speech_hy.DownloadFailed) as cm:
                    speech_hy.download(policy=self.pol, pins=pins, entry=entry)
                self.assertEqual(cm.exception.reason, "unsafe")
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "models", DIR_NAME)))
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "models", DIR_NAME + ".part")))

    def test_a_cut_download_keeps_its_part_and_the_next_one_resumes(self):
        cut = len(self.archive) // 2
        self.h.serve_cut("/model.zip", self.archive, cut)
        with self.assertRaises(speech_hy.DownloadFailed) as cm:
            self.download()
        self.assertEqual(cm.exception.reason, "incomplete")
        self.assertEqual(os.path.getsize(self.part_path()), cut)
        self.assertNothingInstalled()
        self.h.requests.clear()
        self.download()
        self.assertEqual(self.h.requests[0][1], "bytes=%d-" % cut)               # it asked for the rest only
        self.assertTrue(speech_hy.ready(self.pins))
        self.assertFalse(os.path.exists(self.part_path()))

    def test_a_server_that_ignores_range_still_works(self):
        cut = len(self.archive) // 3
        os.makedirs(os.path.dirname(self.part_path()), exist_ok=True)
        Path(self.part_path()).write_bytes(self.archive[:cut])
        self.h.serve("/model.zip", self.archive, ranges=False)
        self.download()
        self.assertTrue(speech_hy.ready(self.pins))

    def test_a_part_that_is_not_ours_fails_the_check_once_and_is_thrown_away(self):
        os.makedirs(os.path.dirname(self.part_path()), exist_ok=True)
        Path(self.part_path()).write_bytes(b"\0" * (len(self.archive) // 2))        # garbage of a plausible size
        with self.assertRaises(speech_hy.DownloadFailed) as cm:
            self.download()
        self.assertEqual(cm.exception.reason, "bad_hash")
        self.assertFalse(os.path.exists(self.part_path()))
        self.download()                                                          # the next one starts clean
        self.assertTrue(speech_hy.ready(self.pins))

    def test_a_part_bigger_than_the_archive_is_started_over(self):
        os.makedirs(os.path.dirname(self.part_path()), exist_ok=True)
        Path(self.part_path()).write_bytes(b"\0" * (len(self.archive) + 50))
        self.download()
        self.assertTrue(speech_hy.ready(self.pins))

    def test_cancel_stops_it_and_keeps_the_part(self):
        count = [0]

        def cancel():
            count[0] += 1
            return count[0] > 2
        with self.assertRaises(speech_hy.Cancelled):
            self.download(should_cancel=cancel)
        self.assertNothingInstalled()
        self.assertTrue(os.path.exists(self.part_path()))                        # what it had is kept for the next try
        self.download()
        self.assertTrue(speech_hy.ready(self.pins))

    def test_the_cancel_flag_of_cancel_download(self):
        os.makedirs(os.path.dirname(self.part_path()), exist_ok=True)
        calls = []

        def route(h):
            calls.append(1)
            h.send_response(200)
            h.send_header("Content-Length", str(len(self.archive)))
            h.end_headers()
            h.wfile.write(self.archive[:1000])
            h.wfile.flush()
            time.sleep(0.5)
            try:
                h.wfile.write(self.archive[1000:])
            except OSError:
                pass
        self.h.routes["/model.zip"] = route
        result = []

        def run():
            try:
                self.download()
            except speech_hy.SpeechHyError as e:
                result.append(e)
        t = threading.Thread(target=run)
        t.start()
        # A slow machine (a CI runner) can take a while to start the request. A cancel asked for BEFORE
        # the download has begun is wiped by its start (cancel stops a download that is running), so
        # wait until the server has really been asked.
        for _ in range(500):
            if calls:
                break
            time.sleep(0.02)
        self.assertTrue(calls, "the download never reached the server")
        speech_hy.cancel_download()
        t.join(20)
        self.assertFalse(t.is_alive(), "the download did not stop")
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], speech_hy.Cancelled)
        self.assertNothingInstalled()

    def test_the_download_is_not_blocked_in_privacy_mode_like_a_whisper_model(self):
        self.download(privacy=True)
        self.assertTrue(speech_hy.ready(self.pins))

    def test_privacy_blocks_it_when_the_switch_says_so(self):
        with mock.patch.object(speech_hy, "DOWNLOAD_BLOCKED_IN_PRIVACY_MODE", True):
            with self.assertRaises(speech_hy.PrivacyBlocked) as cm:
                self.download(privacy=True)
        self.assertIn("Privacy mode", str(cm.exception))
        self.assertEqual(self.h.requests, [])

    def test_nowhere_to_download_from(self):
        with self.assertRaises(speech_hy.NotAvailable):
            speech_hy.download(policy=self.pol, pins=self.pins)                  # no entry, no cache, no URL
        self.assertEqual(self.h.requests, [])

    def test_this_copy_cannot_run_it(self):
        with mock.patch.object(speech_hy, "_runtime_cache", ("old", "1.12.30")):
            with self.assertRaises(speech_hy.NoRuntime):
                self.download()
        self.assertEqual(self.h.requests, [])

    def test_a_server_that_says_no_or_fails(self):
        for code, reason in ((404, "unavailable"), (403, "unavailable"), (500, "server"), (503, "server"), (429, "server")):
            self.h.status("/model.zip", code)
            with self.assertRaises(speech_hy.SpeechHyError) as cm:
                self.download()
            self.assertEqual(cm.exception.reason, reason, code)
            self.assertNothingInstalled()

    def test_a_download_address_that_is_not_allowed_is_never_asked(self):
        for url in ("http://gusan.aibuben.xyz/m.zip", "https://evil.example.com/m.zip", "file:///C:/m.zip"):
            entry = speech_hy.Entry(url, self.pins.archive_sha256, self.pins.archive_size)
            with self.assertRaises(speech_hy.NotAvailable):
                speech_hy.download(policy=speech_hy.Policy(proxies=False), pins=self.pins, entry=entry)
        pol = self.policy(self.h, require_https=True)                            # http, as the real policy has it
        with self.assertRaises(speech_hy.NotAvailable):
            speech_hy.download(policy=pol, pins=self.pins, entry=self.entry)
        self.assertEqual(self.h.requests, [])

    def test_a_redirect_to_a_host_that_is_not_allowed_is_refused(self):
        self.h.redirect("/model.zip", "http://localhost:%d/model.zip" % self.h.port)
        with self.assertRaises(speech_hy.NotAvailable):
            self.download()
        self.assertNothingInstalled()

    def test_a_redirect_inside_the_allowed_hosts_is_followed(self):
        self.h.serve("/real.zip", self.archive)
        self.h.redirect("/model.zip", self.h.base + "/real.zip")
        self.download()
        self.assertTrue(speech_hy.ready(self.pins))

    def test_a_download_of_another_size_is_refused(self):
        self.h.serve("/model.zip", self.archive + b"extra bytes", ranges=False)
        with self.assertRaises(speech_hy.SpeechHyError):
            self.download()
        self.assertNothingInstalled()

    def test_a_server_that_keeps_sending_is_stopped_at_the_pinned_size(self):
        def endless(h):
            h.send_response(200)
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for _ in range(2000):
                piece = b"x" * 1024
                h.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
        self.h.routes["/model.zip"] = endless
        with self.assertRaises(speech_hy.DownloadFailed):
            self.download()
        self.assertNothingInstalled()
        self.assertLess(os.path.getsize(self.part_path()) if os.path.exists(self.part_path()) else 0,
                        self.pins.archive_size + 1)

    def test_not_enough_disk_space_is_said_plainly(self):
        with mock.patch.object(speech_hy.shutil, "disk_usage", return_value=SimpleNamespace(free=1000)):
            with self.assertRaises(speech_hy.DownloadFailed) as cm:
                self.download()
        self.assertEqual(cm.exception.reason, "disk_full")
        self.assertEqual(self.h.requests, [])

    def test_an_error_reads_as_one_plain_sentence(self):
        for exc in (speech_hy.NotAvailable(), speech_hy.NoRuntime(), speech_hy.NotReady(), speech_hy.PrivacyBlocked(),
                    speech_hy.Cancelled(), speech_hy.InUse(), speech_hy.DownloadFailed()):
            text = str(exc)
            self.assertTrue(text.endswith("."), text)
            self.assertNotIn("Traceback", text)
            self.assertEqual(exc.user_message, text)
            self.assertTrue(exc.reason)

    def test_a_download_through_the_catalog_entry(self):
        speech_hy._write_cache(self.entry, time.time())
        out = speech_hy.download(policy=self.pol, pins=self.pins)
        self.assertEqual(out, os.path.join(self.tmp, "models", DIR_NAME))

    def test_leftovers_of_an_earlier_run_are_cleaned_up(self):
        root = os.path.join(self.tmp, "models")
        os.makedirs(os.path.join(root, DIR_NAME + ".part"))
        Path(root, DIR_NAME + ".part", "x").write_bytes(b"x")
        os.makedirs(os.path.join(root, DIR_NAME + ".old"))
        self.download()
        self.assertFalse(os.path.exists(os.path.join(root, DIR_NAME + ".part")))
        self.assertFalse(os.path.exists(os.path.join(root, DIR_NAME + ".old")))

    def test_remove_deletes_the_model_and_the_part_files(self):
        self.download()
        os.makedirs(os.path.dirname(self.part_path()), exist_ok=True)
        Path(self.part_path()).write_bytes(b"x")
        self.assertTrue(speech_hy.remove(self.pins))
        self.assertFalse(speech_hy.ready(self.pins))
        self.assertFalse(os.path.exists(os.path.dirname(self.part_path())))
        self.assertFalse(speech_hy.remove(self.pins))                               # nothing left to delete


# ── who may use it ──────────────────────────────────────────────────────────────

class TestCovers(unittest.TestCase):
    def test_only_armenian_goes_to_the_armenian_model(self):
        for value in ("hy", "HY", "hye", "hy-AM", "Armenian", " hy "):
            self.assertTrue(speech_hy.covers(value), value)
        for value in ("auto", "", None, "en", "ru", "fr", "multi", "de", "fa"):
            self.assertFalse(speech_hy.covers(value), value)

    def test_a_mix_only_when_every_language_in_it_is_armenian(self):
        self.assertTrue(speech_hy.covers("multi", ["hy"]))
        self.assertTrue(speech_hy.covers("multi", ["hy", "HY"]))
        self.assertFalse(speech_hy.covers("multi", ["hy", "en"]))
        self.assertFalse(speech_hy.covers("multi", ["en"]))
        self.assertFalse(speech_hy.covers("multi", []))
        self.assertFalse(speech_hy.covers("multi", None))
        self.assertFalse(speech_hy.covers("auto", ["hy"]))                     # a list does not make "auto" Armenian
        self.assertFalse(speech_hy.covers("en", ["hy"]))

    def test_the_whisper_that_reads_the_rest(self):
        on_disk = {"base", "tiny"}
        self.assertEqual(speech_hy.fallback_whisper(lambda m: m in on_disk), "base")
        self.assertEqual(speech_hy.fallback_whisper(lambda m: m in on_disk, "cuda"), "base")
        self.assertEqual(speech_hy.fallback_whisper(lambda m: m in {"small", "base"}), "small")
        self.assertEqual(speech_hy.fallback_whisper(lambda m: m in {"large-v3", "small"}, "cuda"), "large-v3")
        self.assertEqual(speech_hy.fallback_whisper(lambda m: m == "large-v3-turbo", "cuda"), "large-v3-turbo")
        self.assertEqual(speech_hy.fallback_whisper(lambda m: False), "base")      # the app's default, which downloads
        self.assertEqual(speech_hy.fallback_whisper(None), "base")
        self.assertEqual(speech_hy.fallback_whisper(lambda m: 1 / 0), "base")      # a broken check is not a crash


# ── the recipe ──────────────────────────────────────────────────────────────────

class TestText(unittest.TestCase):
    def test_the_and_ligature(self):
        self.assertEqual(speech_hy.tidy("եւ"), "և")
        self.assertEqual(speech_hy.tidy("Եւ ես"), "Եվ ես")                      # a sentence starts with Եվ
        self.assertEqual(speech_hy.tidy("ԵՒ ԱՅՈՒՄ"), "ԵՎ ԱՅՈՒՄ")
        self.assertEqual(speech_hy.tidy("  ես եւ դու  "), "ես և դու")
        self.assertEqual(speech_hy.tidy("Բարեւ աշխարհ։"), "Բարև աշխարհ։")      # also inside a word

    def test_output_without_a_letter_is_dropped(self):
        for noise in ("", None, "   ", "֊։", "...", "։", "- ,", "123", "!?"):
            self.assertEqual(speech_hy.tidy(noise), "", repr(noise))
        self.assertEqual(speech_hy.tidy("ա"), "ա")

    def test_punctuation_and_capitals_stay_as_the_model_writes_them(self):
        text = "Միացյալ Նահանգների համալսարանի, ինչ-որ բան։ «Ինչո՞ւ»"
        self.assertEqual(speech_hy.tidy(text), text)

    def test_the_words_a_piece_hears_twice_are_dropped_once(self):
        self.assertEqual(speech_hy.drop_repeat("ա բ գ դ", "դ ե զ"), "ե զ")
        self.assertEqual(speech_hy.drop_repeat("ա բ գ դ", "գ դ ե"), "ե")
        self.assertEqual(speech_hy.drop_repeat("ա բ գ դ", "ե զ"), "ե զ")
        self.assertEqual(speech_hy.drop_repeat("Ա բ գ։", "գ ե"), "ե")           # case and punctuation do not matter
        self.assertEqual(speech_hy.drop_repeat("", "ե զ"), "ե զ")
        self.assertEqual(speech_hy.drop_repeat("ա", ""), "")
        self.assertEqual(speech_hy.drop_repeat("ա ա ա ա ա", "ա ա ա ա ա ա ա"), "ա ա ա")   # at most 4 words
        self.assertEqual(speech_hy.join_texts([("ա բ գ", False), ("գ դ", True), ("ե", False)]), "ա բ գ դ ե")
        self.assertEqual(speech_hy.join_texts([("ա", False), ("", True), ("բ", False)]), "ա բ")
        self.assertEqual(speech_hy.join_texts([]), "")

    def test_a_piece_is_split_into_sentences_by_its_token_times(self):
        tokens = [" Ա", "բ", " գ", "դ։", " Ե", "զ", " Է", "ը", " Թ", "ժ։"]
        stamps = [0.0, 0.2, 0.4, 0.8, 1.4, 1.6, 2.4, 2.6, 5.0, 5.4]
        text = "".join(tokens).strip()
        parts = speech_hy.split_result(text, tokens, stamps, 6.0)
        self.assertEqual([t for _a, _b, t in parts], ["Աբ գդ։", "Եզ Էը", "Թժ։"])
        # one second is the least a segment is: "Եզ Էը" is 1.0 .. 3.6 s long, and tiles with its neighbours
        self.assertEqual(parts[0][:2], (0.0, 1.4))
        self.assertEqual(parts[1][:2], (1.4, 5.0))
        self.assertEqual(parts[-1][1], 6.0)
        for (a1, b1, _t1), (a2, _b2, _t2) in zip(parts, parts[1:]):
            self.assertEqual(b1, a2)

    def test_a_long_run_is_cut_at_twelve_seconds_and_a_pause_at_nearly_a_second(self):
        words = [" w%d" % i for i in range(30)]                                 # one word a second, no full stop
        stamps = [float(i) for i in range(30)]
        parts = speech_hy.split_result("".join(words).strip(), words, stamps, 30.0)
        self.assertGreaterEqual(len(parts), 2)
        for a, b, _t in parts:
            self.assertLessEqual(b - a, 13.0)
        tokens = [" ա", "բ", " գ", "դ", " ե", "զ", " է", "ը"]
        stamps = [0.0, 0.3, 0.6, 1.0, 2.5, 2.8, 3.2, 3.6]                       # 1.5 s of silence before "ե"
        parts = speech_hy.split_result("".join(tokens).strip(), tokens, stamps, 4.0)
        self.assertEqual([t for _a, _b, t in parts], ["աբ գդ", "եզ էը"])

    def test_times_that_do_not_belong_to_the_text_give_one_segment(self):
        self.assertEqual(speech_hy.split_result("ա բ", [], [], 3.0), [(0.0, 3.0, "ա բ")])
        self.assertEqual(speech_hy.split_result("ա բ", [" ա", " բ"], [0.0], 3.0), [(0.0, 3.0, "ա բ")])
        self.assertEqual(speech_hy.split_result("ա բ", [" x", " y"], [0.0, 1.0], 3.0), [(0.0, 3.0, "ա բ")])
        self.assertEqual(speech_hy.split_result("ա բ", [" ա", " բ"], ["a", "b"], 3.0), [(0.0, 3.0, "ա բ")])
        self.assertEqual(speech_hy.split_result("", [], [], 3.0), [])
        self.assertEqual(speech_hy.split_result("ա", None, None, 1.0), [(0.0, 1.0, "ա")])


class TestWindows(unittest.TestCase):
    """plan_windows works on sample numbers: no audio needed (x=None cuts at the middle)."""

    def secs(self, windows):
        return [(round(s / SR, 2), round(e / SR, 2), round(n / SR, 2), o) for s, e, n, o in windows]

    def r(self, *pairs):
        return [(int(a * SR), int(b * SR)) for a, b in pairs]

    def test_speech_with_a_pause_is_two_pieces_and_close_speech_one(self):
        self.assertEqual(self.secs(speech_hy.plan_windows(self.r((0, 3), (4.2, 8)))),
                         [(0.0, 3.0, 0.0, False), (4.2, 8.0, 4.2, False)])         # a pause of 1.2 s always ends a piece
        self.assertEqual(self.secs(speech_hy.plan_windows(self.r((0, 3), (3.6, 8)))), [(0.0, 8.0, 0.0, False)])
        self.assertEqual(self.secs(speech_hy.plan_windows(self.r((0, 7), (7.6, 12)))),
                         [(0.0, 7.0, 0.0, False), (7.6, 12.0, 7.6, False)])         # 0.6 s after a long enough piece
        self.assertEqual(speech_hy.plan_windows([]), [])

    def test_no_piece_is_longer_than_twenty_five_seconds(self):
        for regions in ([(0, 70)], [(0, 24), (24.5, 49), (49.5, 90)], [(0, 25.1)], [(5, 31)]):
            windows = speech_hy.plan_windows(self.r(*regions))
            self.assertTrue(windows)
            for s, e, _n, _o in windows:
                self.assertLessEqual((e - s) / SR, 25.5, regions)
            covered = sum(min(e, int(r[1] * SR)) - max(s, int(r[0] * SR)) for r in regions
                          for s, e, _n, _o in windows if min(e, int(r[1] * SR)) > max(s, int(r[0] * SR)))
            self.assertGreaterEqual(covered, sum(int((b - a) * SR) for a, b in regions) - 2, regions)   # nothing is lost

    def test_a_cut_in_the_middle_of_speech_starts_the_next_piece_a_little_earlier(self):
        windows = self.secs(speech_hy.plan_windows(self.r((0, 40))))
        self.assertEqual(len(windows), 2)
        first, second = windows
        self.assertFalse(first[3])
        self.assertTrue(second[3])
        self.assertAlmostEqual(second[0], second[2] - 0.3, places=2)
        self.assertAlmostEqual(first[1], second[2], places=2)

    def test_speech_regions_closer_than_a_third_of_a_second_are_one(self):
        merged = speech_hy.speech_regions([0] * 100, detector=lambda x: [(0, 8000), (10000, 20000), (40000, 48000)])
        self.assertEqual(merged, [(0, 20000), (40000, 48000)])
        self.assertEqual(speech_hy.speech_regions([0], detector=lambda x: [(5, 5), (9, 3)]), [])


# ── the engine, with a fake recognizer ──────────────────────────────────────────

class FakeRec:
    """What sherpa-onnx's OfflineRecognizer looks like from here."""

    def __init__(self, answer=None, delay=0.0):
        self.answer = answer or (lambda samples, n: ("ա բ", [], []))
        self.delay = delay
        self.inputs = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def create_stream(self):
        return SimpleNamespace(samples=None, result=SimpleNamespace(text="", tokens=[], timestamps=[]),
                               accept_waveform=self._accept)

    def _accept(self, sr, x):
        assert sr == SR
        self.inputs.append(x)

    def decode_stream(self, stream):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            x = self.inputs[-1] if self.inputs else None
            ans = self.answer(x, len(self.inputs))
            if isinstance(ans, Exception):
                raise ans
            text, tokens, stamps = ans
            stream.result = SimpleNamespace(text=text, tokens=tokens, timestamps=stamps)
        finally:
            with self.lock:
                self.active -= 1


class _EngineBase(_Base):
    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.tmp, "m")
        os.makedirs(self.dir)
        for name, data in FILES.items():
            Path(self.dir, name).write_bytes(data)
        self.rec = FakeRec()
        self.built = []
        self.now = [100.0]

    def engine(self, rec=None, detector=None, factory=None, **kw):
        rec = rec or self.rec

        def build(directory, threads):
            self.built.append((directory, threads))
            return rec
        return speech_hy.Engine(model_dir=self.dir, pins=self.pins, factory=factory or build,
                                detector=detector, clock=lambda: self.now[0], **kw)

    def audio(self, seconds, level=0.3, hz=220):
        import numpy as np
        t = np.arange(int(seconds * SR), dtype=np.float32) / SR
        return (level * np.sin(2 * np.pi * hz * t)).astype(np.float32)


@needs_numpy
class TestEngineRecipe(_EngineBase):
    def whole(self, x):
        return [(0, len(x))]

    def test_it_reads_speech_and_loads_once(self):
        eng = self.engine(detector=lambda x: [(0, len(x))])
        self.assertFalse(eng.loaded())
        self.assertEqual(eng.transcribe(self.audio(3), "hy"), "ա բ")
        self.assertEqual(eng.transcribe(self.audio(2), "hy"), "ա բ")
        self.assertEqual(len(self.built), 1)
        self.assertEqual(self.built[0], (self.dir, 2))                       # two threads, the validated setting
        self.assertTrue(eng.loaded())

    def test_a_quiet_piece_is_raised_to_half_scale_and_a_loud_one_is_left_alone(self):
        import numpy as np
        eng = self.engine(detector=lambda x: [(0, len(x))])
        eng.transcribe(self.audio(2, level=0.01), "hy")
        self.assertAlmostEqual(float(np.abs(self.rec.inputs[-1]).max()), 0.5, places=3)
        quiet = self.audio(2, level=0.003)
        eng.transcribe(quiet, "hy")
        self.assertAlmostEqual(float(np.abs(self.rec.inputs[-1]).max()), 0.5, places=3)
        loud = self.audio(2, level=0.8)
        eng.transcribe(loud, "hy")
        self.assertTrue(np.array_equal(self.rec.inputs[-1], loud))           # never lowered
        edge = self.audio(2, level=0.2)
        eng.transcribe(edge, "hy")
        self.assertAlmostEqual(float(np.abs(self.rec.inputs[-1]).max()), 0.2, places=3)

    def test_the_callers_audio_is_never_changed(self):
        import numpy as np
        x = self.audio(2, level=0.01)
        before = x.copy()
        self.engine(detector=lambda x: [(0, len(x))]).transcribe(x, "hy")
        self.assertTrue(np.array_equal(x, before))

    def test_silence_noise_and_tiny_inputs_are_never_decoded(self):
        import numpy as np
        eng = self.engine(detector=lambda x: [(0, len(x))])
        for x in (np.zeros(3 * SR, dtype=np.float32), self.audio(3, level=5e-5),
                  self.audio(0.2), np.zeros(0, dtype=np.float32), np.zeros(10, dtype=np.float32)):
            self.assertEqual(eng.transcribe(x, "hy"), "")
        self.assertEqual(self.rec.inputs, [])
        self.assertEqual(self.built, [])                                    # nothing was even loaded for them
        self.assertEqual(eng.transcribe_segments(np.zeros(SR, dtype=np.float32)), [])

    def test_what_the_voice_detector_calls_no_speech_is_never_decoded(self):
        eng = self.engine(detector=lambda x: [])
        self.assertEqual(eng.transcribe(self.audio(5), "hy"), "")           # loud noise, but not a voice
        self.assertEqual(self.rec.inputs, [])

    def test_only_the_voiced_part_is_decoded(self):
        eng = self.engine(detector=lambda x: [(SR * 2, SR * 5)])
        eng.transcribe(self.audio(8), "hy")
        self.assertEqual(len(self.rec.inputs), 1)
        self.assertAlmostEqual(len(self.rec.inputs[0]) / SR, 3.0, places=1)

    def test_letterless_output_and_an_empty_input_error_are_nothing(self):
        for answer in (lambda x, n: ("֊։", [], []), lambda x, n: ("   ", [], []), lambda x, n: ("...", [], []),
                       lambda x, n: RuntimeError("Invalid input shape {0,80}")):
            rec = FakeRec(answer)
            eng = self.engine(rec=rec, detector=lambda x: [(0, len(x))])
            self.assertEqual(eng.transcribe(self.audio(2), "hy"), "")
            self.assertEqual(eng.transcribe_segments(self.audio(2)), [])

    def test_the_text_comes_out_in_modern_spelling(self):
        rec = FakeRec(lambda x, n: ("Եւ ես եւ դու", [], []))
        eng = self.engine(rec=rec, detector=lambda x: [(0, len(x))])
        self.assertEqual(eng.transcribe(self.audio(2), "hy"), "Եվ ես և դու")

    def test_long_speech_is_read_in_pieces_never_longer_than_the_limit(self):
        rec = FakeRec(lambda x, n: ("w%d" % n, [], []))
        eng = self.engine(rec=rec, detector=lambda x: [(0, len(x))])
        text = eng.transcribe(self.audio(70), "hy")
        self.assertGreaterEqual(len(self.rec.inputs) if rec is self.rec else len(rec.inputs), 3)
        for x in rec.inputs:
            self.assertLessEqual(len(x) / SR, 30.0)
        self.assertEqual(text.split(), ["w%d" % i for i in range(1, len(rec.inputs) + 1)])

    def test_a_piece_is_cut_at_the_quietest_place_and_the_repeated_words_go(self):
        x = self.audio(40, level=0.3)
        x[int(20.0 * SR):int(20.4 * SR)] = 0.0005                           # a short pause at 20 s
        answers = iter([("ա բ գ դ", [], []), ("դ ե զ", [], [])])
        rec = FakeRec(lambda s, n: next(answers))
        eng = self.engine(rec=rec, detector=lambda x_: [(0, len(x_))])
        self.assertEqual(eng.transcribe(x, "hy"), "ա բ գ դ ե զ")
        first, second = rec.inputs
        self.assertGreater(len(first) / SR, 19.5)
        self.assertLess(len(first) / SR, 20.5)                              # cut inside the pause
        self.assertAlmostEqual((len(first) + len(second)) / SR, 40.3, places=0)

    def test_progress_and_cancel(self):
        rec = FakeRec(lambda x, n: ("a%d" % n, [], []))
        x = self.audio(60)
        seen = []
        eng = self.engine(rec=rec, detector=lambda x_: [(0, 20 * SR), (25 * SR, 45 * SR), (50 * SR, 60 * SR)])
        out = eng.transcribe_segments(x, on_progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(len(out), 3)
        self.assertEqual([d for d, _t in seen], sorted(d for d, _t in seen))
        self.assertTrue(all(t == 60.0 for _d, t in seen))
        self.assertAlmostEqual(seen[-1][0], 60.0, places=1)
        calls = []
        rec2 = FakeRec(lambda x_, n: ("b%d" % n, [], []))
        eng2 = self.engine(rec=rec2, detector=lambda x_: [(0, 20 * SR), (25 * SR, 45 * SR), (50 * SR, 60 * SR)])
        out = eng2.transcribe_segments(x, should_cancel=lambda: (calls.append(1), len(calls) > 1)[1])
        self.assertIsNone(out)                                              # cancelled: None, not []
        self.assertEqual(len(rec2.inputs), 1)

    def test_segments_are_like_whispers(self):
        rec = FakeRec(lambda x, n: ("Ա բ։ Գ դ։ Ե զ։", [" Ա", " բ։", " Գ", " դ։", " Ե", " զ։"], [0.0, 0.4, 3.0, 3.4, 7.0, 7.4]))
        eng = self.engine(rec=rec, detector=lambda x: [(SR * 1, SR * 10)])
        segs = eng.transcribe_segments(self.audio(12))
        self.assertEqual([s["text"] for s in segs], ["Ա բ։", "Գ դ։", "Ե զ։"])
        self.assertEqual(set(segs[0]), {"start", "end", "text"})
        self.assertEqual(segs[0]["start"], 1.0)                              # on the recording's clock, not the piece's
        self.assertAlmostEqual(segs[1]["start"], 4.0, places=1)
        self.assertLessEqual(segs[-1]["end"], 10.0 + 0.01)
        for a, b in zip(segs, segs[1:]):
            self.assertLessEqual(a["end"], b["start"] + 1e-6)
            self.assertLess(a["start"], a["end"])

    def test_a_recognizer_without_times_gives_one_segment_a_piece(self):
        rec = FakeRec(lambda x, n: ("ա բ գ", [], []))
        eng = self.engine(rec=rec, detector=lambda x: [(SR, 9 * SR)])
        segs = eng.transcribe_segments(self.audio(10))
        self.assertEqual(segs, [{"start": 1.0, "end": 9.0, "text": "ա բ գ"}])

    def test_overlapped_pieces_do_not_repeat_words_in_the_segments(self):
        answers = iter([("ա բ գ դ", [], []), ("դ ե զ", [], [])])
        rec = FakeRec(lambda x, n: next(answers))
        eng = self.engine(rec=rec, detector=lambda x: [(0, len(x))])
        segs = eng.transcribe_segments(self.audio(40))
        self.assertEqual([s["text"] for s in segs], ["ա բ գ դ", "ե զ"])
        self.assertLessEqual(segs[0]["end"], segs[1]["start"] + 1e-6)

    def test_every_piece_goes_through_one_decode_at_a_time(self):
        rec = FakeRec(delay=0.03)
        eng = self.engine(rec=rec, detector=lambda x: [(0, len(x))])
        results = []
        threads = [threading.Thread(target=lambda: results.append(eng.transcribe(self.audio(2), "hy"))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(results, ["ա բ"] * 6)
        self.assertEqual(rec.max_active, 1)
        self.assertEqual(len(self.built), 1)

    def test_a_long_read_lets_a_waiting_piece_go_between_its_own_pieces(self):
        # A file of an hour is hundreds of pieces. A dictation (or a live piece) that arrives meanwhile
        # waits for ONE piece, not for the whole file - and one decode still runs at a time.
        rec = FakeRec(delay=0.05)
        # eight pieces, each with a long pause after it (a pause of a second always ends a piece)
        regions = [(i * 5 * SR, i * 5 * SR + 2 * SR) for i in range(8)]
        eng = self.engine(rec=rec, detector=lambda x: [r for r in regions if r[1] <= len(x)])
        done = {}

        def long_read():
            eng.transcribe_segments(self.audio(40))
            done["long"] = time.monotonic()

        def short_read():
            time.sleep(0.12)                          # the long read is in its second or third piece
            eng.transcribe(self.audio(2), "hy")
            done["short"] = time.monotonic()
        threads = [threading.Thread(target=long_read), threading.Thread(target=short_read)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(set(done), {"long", "short"})
        self.assertLess(done["short"], done["long"], "the dictation waited for the whole file")
        self.assertEqual(rec.max_active, 1)


@needs_numpy
class TestEngineMemory(_EngineBase):
    def setUp(self):
        super().setUp()
        self.eng = self.engine(detector=lambda x: [(0, len(x))])

    def test_not_ready_and_no_runtime_say_so(self):
        eng = speech_hy.Engine(model_dir=os.path.join(self.tmp, "nothing"), pins=self.pins,
                               factory=lambda d, t: self.rec, detector=lambda x: [(0, len(x))])
        with self.assertRaises(speech_hy.NotReady):
            eng.transcribe(self.audio(2), "hy")
        with self.assertRaises(speech_hy.NotReady):
            eng.load()

        def no_sherpa(directory, threads):
            raise ImportError("No module named 'sherpa_onnx'")
        eng = self.engine(factory=no_sherpa, detector=lambda x: [(0, len(x))])
        with self.assertRaises(speech_hy.NoRuntime):
            eng.transcribe(self.audio(2), "hy")
        self.assertFalse(eng.loaded())

    def test_idle_release_follows_the_idle_rule(self):
        self.eng.transcribe(self.audio(2), "hy")
        self.assertTrue(self.eng.loaded())
        self.assertFalse(self.eng.maybe_unload_idle(600, now=self.now[0] + 599))
        self.assertTrue(self.eng.loaded())
        self.assertFalse(self.eng.maybe_unload_idle(0, now=self.now[0] + 99999))       # 0 = keep it
        self.assertFalse(self.eng.maybe_unload_idle(None, now=self.now[0] + 99999))
        self.assertTrue(self.eng.maybe_unload_idle(600, now=self.now[0] + 601))
        self.assertFalse(self.eng.loaded())
        self.assertFalse(self.eng.maybe_unload_idle(600, now=self.now[0] + 9999))       # nothing left to free
        self.eng.transcribe(self.audio(2), "hy")                                       # comes back on use
        self.assertEqual(len(self.built), 2)

    def test_a_model_in_use_is_never_freed(self):
        freed = []

        def answer(x, n):
            freed.append((self.eng.maybe_unload_idle(1, now=self.now[0] + 99999), self.eng.unload()))
            return ("ա", [], [])
        rec = FakeRec(answer)
        eng = self.engine(rec=rec, detector=lambda x: [(0, len(x))])
        self.eng = eng
        self.assertEqual(eng.transcribe(self.audio(2), "hy"), "ա")
        self.assertEqual(freed, [(False, False)])
        self.assertTrue(eng.loaded())
        self.assertEqual(eng.users, 0)

    def test_the_idle_sweep_never_waits_for_a_load(self):
        gate = threading.Event()
        started = threading.Event()

        def slow(directory, threads):
            started.set()
            gate.wait(10)
            return self.rec
        eng = self.engine(factory=slow, detector=lambda x: [(0, len(x))])
        t = threading.Thread(target=eng.load)
        t.start()
        started.wait(5)
        t0 = time.monotonic()
        self.assertFalse(eng.maybe_unload_idle(1, now=1e9))
        self.assertLess(time.monotonic() - t0, 0.5)
        gate.set()
        t.join(10)

    def test_unload_frees_it_when_idle(self):
        self.eng.load()
        self.assertTrue(self.eng.unload())
        self.assertFalse(self.eng.loaded())
        self.assertTrue(self.eng.unload())                                             # nothing loaded: fine

    def test_the_module_level_calls_follow_the_one_engine(self):
        self.assertFalse(speech_hy.loaded())
        self.assertTrue(speech_hy.unload())
        self.assertFalse(speech_hy.maybe_unload_idle(60))
        eng = speech_hy.engine()
        self.assertIs(speech_hy.engine(), eng)
        eng._factory = lambda d, t: self.rec
        eng._dir, eng._pins = self.dir, self.pins
        eng._detector = lambda x: [(0, len(x))]
        self.assertEqual(speech_hy.transcribe(self.audio(2), "hy"), "ա բ")
        self.assertTrue(speech_hy.loaded())
        self.assertEqual(len(speech_hy.transcribe_segments(self.audio(2))), 1)
        speech_hy.load()
        self.assertTrue(speech_hy.maybe_unload_idle(60, now=time.monotonic() + 3600))
        self.assertFalse(speech_hy.loaded())

    def test_the_one_engine_builds_the_voice_detector_with_the_recognizer(self):
        with mock.patch.object(speech_hy, "_warm_vad") as warm:
            eng = speech_hy.engine()
            eng._factory, eng._dir, eng._pins = (lambda d, t: self.rec), self.dir, self.pins
            eng.load()
            eng.load()                                                                   # already loaded: not again
            self.assertEqual(warm.call_count, 1)
            eng.unload()
            eng.load()
            self.assertEqual(warm.call_count, 2)
            plain = self.engine(detector=lambda x: [(0, len(x))])
            plain.load()                                                                 # an engine of its own detector, or a test's
            self.assertEqual(warm.call_count, 2)

    def test_remove_waits_for_nothing_and_never_pulls_the_model_from_under_a_piece(self):
        models = os.path.join(self.tmp, "models", DIR_NAME)
        os.makedirs(models)
        for name, data in FILES.items():
            Path(models, name).write_bytes(data)
        results = []

        def answer(x, n):
            try:
                speech_hy.remove(self.pins)
            except speech_hy.InUse as e:
                results.append(("in use", str(e)))
            return ("ա", [], [])
        rec = FakeRec(answer)
        eng = speech_hy.Engine(pins=self.pins, factory=lambda d, t: rec, detector=lambda x: [(0, len(x))])
        with mock.patch.object(speech_hy, "_ENGINE", eng):
            eng.transcribe(self.audio(2), "hy")
            self.assertEqual(results[0][0], "in use")
            self.assertTrue(os.path.isdir(models))
            self.assertTrue(speech_hy.remove(self.pins))                              # idle: now it goes
            self.assertFalse(eng.loaded())
        self.assertFalse(os.path.exists(models))


class TestAudioTestsInAFreshProcess(unittest.TestCase):
    """tests/test_core.py replaces numpy with a stand-in for the rest of a whole-suite run (as CI does),
    so the audio tests above skip there. This runs them in a clean interpreter, where numpy is real."""

    def test_the_recipe_passes_with_real_numpy(self):
        if os.environ.get("GUSAN_HY_INNER") or _real_numpy():
            self.skipTest("the audio tests run in this process")
        probe = subprocess.run([sys.executable, "-I", "-c", "import numpy; numpy.zeros(2)"], capture_output=True, timeout=120)
        if probe.returncode != 0:
            self.skipTest("numpy is not installed")
        env = dict(os.environ, GUSAN_HY_INNER="1")
        res = subprocess.run([sys.executable, "-m", "pytest", str(Path(__file__)), "-q", "-x", "-p", "no:cacheprovider",
                              "-k", "TestEngineRecipe or TestEngineMemory"],
                             capture_output=True, text=True, timeout=300, cwd=str(ROOT), env=env)
        self.assertEqual(res.returncode, 0, (res.stdout + res.stderr)[-3000:])
        self.assertIn("passed", res.stdout)
        self.assertNotIn("skipped", res.stdout.splitlines()[-1])


# ── the real model (skipped when its converted files are not on this computer) ──

# Where the converted package and the 100 FLEURS clips were made (nemo_work/); GUSAN_HY_MODEL_DIR and
# GUSAN_HY_DATA_DIR point somewhere else. Nothing here is in the repo, so on any other computer these
# tests are skipped.
_NEMO_WORK = (r"C:\Users\ARAMAD~1\AppData\Local\Temp\claude\C--Users-Aram-Adamyan-Desktop-Projects-transcribe-app"
              r"\d8c4d3d2-a0ad-4a80-ac80-4a71f134f539\scratchpad\nemo_work")
_REAL_DIR = os.environ.get("GUSAN_HY_MODEL_DIR") or os.path.join(_NEMO_WORK, "release", speech_hy.MODEL_DIR_NAME)
_REAL_DATA = os.environ.get("GUSAN_HY_DATA_DIR") or os.path.join(_NEMO_WORK, "data")


def _real_available():
    try:
        import sherpa_onnx  # noqa: F401
    except Exception:
        return False
    return (os.path.isdir(_REAL_DIR) and os.path.isfile(os.path.join(_REAL_DIR, "encoder.int8.onnx"))
            and os.path.isfile(os.path.join(_REAL_DATA, "refs.tsv")) and _real_numpy())


@unittest.skipUnless(_real_available(), "the converted Armenian model / its test clips are not on this computer")
class TestRealModel(unittest.TestCase):
    """The shipped files through OUR transcribe(): five FLEURS clips, WER under 20 %."""

    CLIPS = 5

    @classmethod
    def setUpClass(cls):
        import csv
        import numpy as np
        cls.np = np
        cls.eng = speech_hy.Engine(model_dir=_REAL_DIR)
        rows = list(csv.reader(open(os.path.join(_REAL_DATA, "refs.tsv"), encoding="utf-8", newline=""),
                               delimiter="\t", quoting=csv.QUOTE_NONE, escapechar="\\"))[1:]
        cls.rows = []
        for r in rows:                                      # the first five clips whose words have no digits
            if not any(c.isdigit() for c in r[2]):
                cls.rows.append(r)
            if len(cls.rows) == cls.CLIPS:
                break

    @staticmethod
    def _norm(text):
        import unicodedata
        text = unicodedata.normalize("NFKC", text or "").lower()
        out = []
        for ch in text:
            if ch in "\u055b\u055c\u055e\u055f\u200d\u200c\u00ad":
                continue
            cat = unicodedata.category(ch)
            out.append(" " if (cat[0] in "PSC" or cat == "Zs" or ch in "\t\r\n") else ch)
        return " ".join("".join(out).split())

    @staticmethod
    def _wer(refs, hyps):
        def edit(a, b):
            prev = list(range(len(b) + 1))
            for i, x in enumerate(a, 1):
                cur = [i]
                for j, y in enumerate(b, 1):
                    cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
                prev = cur
            return prev[-1]
        errors = sum(edit(r.split(), h.split()) for r, h in zip(refs, hyps))
        return errors / max(1, sum(len(r.split()) for r in refs))

    def _read(self, name):
        import struct
        b = open(os.path.join(_REAL_DATA, "clips", name), "rb").read()
        pos, fmt, data = 12, None, None
        while pos + 8 <= len(b):
            cid, size = b[pos:pos + 4], struct.unpack("<I", b[pos + 4:pos + 8])[0]
            body = b[pos + 8:pos + 8 + size]
            if cid == b"fmt ":
                fmt = struct.unpack("<HHIIHH", body[:16])
            elif cid == b"data":
                data = body
            pos += 8 + size + (size & 1)
        tag, _ch, rate, _br, _ba, bits = fmt
        self.assertEqual(rate, 16000)
        if tag == 3 and bits == 32:
            return self.np.frombuffer(data, dtype="<f4").astype(self.np.float32)
        return self.np.frombuffer(data, dtype="<i2").astype(self.np.float32) / 32768.0

    def test_five_clips_through_our_transcribe(self):
        refs, hyps, seconds = [], [], 0.0
        t0 = time.monotonic()
        for r in self.rows:
            audio = self._read(r[0])
            seconds += len(audio) / 16000.0
            text = self.eng.transcribe(audio, "hy")
            self.assertTrue(text, r[0])
            refs.append(self._norm(r[2]))
            hyps.append(self._norm(text))
        took = time.monotonic() - t0
        wer = self._wer(refs, hyps)
        print("\n[real Armenian model] %d clips, %.0f s of audio, %.1f s, WER %.1f %%" % (len(self.rows), seconds, took, wer * 100))
        self.assertLess(wer, 0.20)

    def test_the_final_re_read_comes_out_as_segments(self):
        np = self.np
        clips = [self._read(r[0]) for r in self.rows[:2]]
        audio = np.concatenate([clips[0], np.zeros(2 * 16000, dtype=np.float32), clips[1]])
        segs = self.eng.transcribe_segments(audio)
        self.assertGreaterEqual(len(segs), 2)
        for a, b in zip(segs, segs[1:]):
            self.assertLessEqual(a["end"], b["start"] + 1e-6)
        for s in segs:
            self.assertLess(s["start"], s["end"])
            self.assertLessEqual(s["end"], len(audio) / 16000.0 + 0.1)
            self.assertTrue(s["text"].strip())
            self.assertLessEqual(s["end"] - s["start"], 30.0)
        self.assertGreaterEqual(segs[-1]["start"], len(clips[0]) / 16000.0)              # the second clip is after the first

    def test_silence_and_noise_make_up_nothing(self):
        np = self.np
        self.assertEqual(self.eng.transcribe(np.zeros(16000, dtype=np.float32), "hy"), "")
        t = np.arange(5 * 16000, dtype=np.float32) / 16000
        self.assertEqual(self.eng.transcribe((0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), "hy"), "")
        rng = np.random.default_rng(1)
        self.assertEqual(self.eng.transcribe((0.05 * rng.standard_normal(5 * 16000)).astype(np.float32), "hy"), "")

    def test_a_quiet_recording_is_still_read(self):
        audio = self._read(self.rows[0][0])
        quiet = (audio * (0.004 / float(abs(audio).max()))).astype(self.np.float32)
        loud = self.eng.transcribe(audio, "hy")
        faint = self.eng.transcribe(quiet, "hy")
        self.assertTrue(faint)
        self.assertLess(self._wer([self._norm(loud)], [self._norm(faint)]), 0.3)


if __name__ == "__main__":
    unittest.main()
