"""Local AI model downloads (local_llm.download_model): the right file for
every model, a correct resume, and never a cut-off model installed. Nothing
real is downloaded (TRANSCRIBE_NET_TESTS=1 checks the live links)."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

# Never the real app data.
os.environ.setdefault("TRANSCRIBE_APP_DATA_DIR", tempfile.mkdtemp(prefix="transcribe-test-data-"))
os.environ.setdefault("TRANSCRIBE_DISABLE_KEYRING", "1")
os.environ.setdefault("TRANSCRIBE_SKIP_MIGRATION", "1")

import local_llm


class _ModelResp:
    """A fake Hugging Face reply; records the request headers."""

    def __init__(self, status, body, headers):
        self.status_code, self.body, self.headers = status, body, headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            err = RuntimeError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), 300):
            yield self.body[i:i + 300]


FILE = bytes(range(256)) * 4                                 # a 1024-byte "model"


class TestModelDownloads(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        p = mock.patch.object(local_llm, "model_dir", lambda m: self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.requests = []

    def _serve(self, reply):
        def get(url, headers=None, **kw):
            self.requests.append(dict(headers or {}))
            return reply(dict(headers or {}))
        p = mock.patch.object(local_llm.requests, "get", get)
        p.start()
        self.addCleanup(p.stop)

    def _partial(self, n):
        local_llm.partial_path("qwen_3b").write_bytes(FILE[:n])

    def _dest(self):
        return local_llm.model_path("qwen_3b")

    def test_qwen_7b_comes_from_a_single_file_now(self):
        url = local_llm.model_url("qwen_7b")
        self.assertIn("bartowski/Qwen2.5-7B-Instruct-GGUF", url)
        self.assertTrue(url.endswith("Qwen2.5-7B-Instruct-Q4_K_M.gguf"))
        self.assertNotIn("-of-", url)
        self.assertNotIn("legacy_filenames", local_llm.MODEL_CATALOG["qwen_7b"])

    def test_every_model_comes_from_a_pinned_upload(self):
        # Never "main": a re-upload can't splice into a resumed download.
        for mid, info in local_llm.MODEL_CATALOG.items():
            url = local_llm.model_url(mid)
            self.assertRegex(info["revision"], r"^[0-9a-f]{40}$", mid)
            self.assertIn(f"/resolve/{info['revision']}/", url)
            self.assertNotIn("/resolve/main/", url)

    def test_settings_can_ask_about_downloads_while_a_model_loads(self):
        # A model load holds _llm_lock for seconds; the GUI-thread check must
        # not wait for it.
        result = []
        with local_llm._llm_lock:
            t = threading.Thread(target=lambda: result.append(local_llm.downloading("qwen_3b")))
            t.start()
            t.join(2)
            self.assertFalse(t.is_alive(), "downloading() waited for the model lock")
        self.assertEqual(result, [False])

    def test_a_fresh_download(self):
        self._serve(lambda h: _ModelResp(200, FILE, {"Content-Length": str(len(FILE))}))
        progress = []
        local_llm.download_model("qwen_3b", on_progress=lambda *a: progress.append(a))
        self.assertEqual(self._dest().read_bytes(), FILE)
        self.assertNotIn("Range", self.requests[0])
        self.assertEqual(progress[-1], (100, len(FILE), len(FILE)))
        self.assertFalse(local_llm.partial_path("qwen_3b").exists())

    def test_a_stopped_download_resumes_where_it_stopped(self):
        self._partial(400)
        self._serve(lambda h: _ModelResp(206, FILE[400:], {
            "Content-Length": str(len(FILE) - 400),
            "Content-Range": f"bytes 400-{len(FILE) - 1}/{len(FILE)}"}))
        progress = []
        local_llm.download_model("qwen_3b", on_progress=lambda *a: progress.append(a))
        self.assertEqual(self.requests[0]["Range"], "bytes=400-")
        self.assertEqual(self._dest().read_bytes(), FILE)
        # Progress counts the whole file, not just the rest.
        self.assertEqual(progress[0][1:], (700, len(FILE)))

    def test_a_resume_cut_off_again_keeps_everything_so_far(self):
        self._partial(400)
        self._serve(lambda h: _ModelResp(206, FILE[400:700], {
            "Content-Length": str(len(FILE) - 400),
            "Content-Range": f"bytes 400-{len(FILE) - 1}/{len(FILE)}"}))
        with self.assertRaises(local_llm.LocalLLMError) as cm:
            local_llm.download_model("qwen_3b")
        self.assertIn("picks up", str(cm.exception))
        self.assertFalse(self._dest().exists())
        self.assertEqual(local_llm.partial_path("qwen_3b").read_bytes(), FILE[:700])

    def test_a_server_that_ignores_the_resume_restarts_cleanly(self):
        # It sends the whole file again (200): written from the start, not
        # appended - and not reported as a failed download.
        self._partial(400)
        self._serve(lambda h: _ModelResp(200, FILE, {"Content-Length": str(len(FILE))}))
        local_llm.download_model("qwen_3b")
        self.assertEqual(self._dest().read_bytes(), FILE)

    def test_a_reply_for_the_wrong_range_is_never_appended(self):
        # Bytes 500+ after our 400: appended, the file would be short (kept
        # to "resume") and silently scrambled.
        self._partial(400)
        self._serve(lambda h: _ModelResp(206, FILE[500:], {
            "Content-Range": f"bytes 500-{len(FILE) - 1}/{len(FILE)}"}))
        with self.assertRaises(local_llm.LocalLLMError) as cm:
            local_llm.download_model("qwen_3b")
        self.assertIn("starts over", str(cm.exception))
        self.assertFalse(local_llm.partial_path("qwen_3b").exists())
        self.assertFalse(self._dest().exists())

    def test_an_already_complete_partial_is_installed(self):
        self._partial(len(FILE))
        self._serve(lambda h: _ModelResp(416, b"", {"Content-Range": f"bytes */{len(FILE)}"}))
        local_llm.download_model("qwen_3b")
        self.assertEqual(self._dest().read_bytes(), FILE)

    def test_a_too_long_partial_starts_over(self):
        local_llm.partial_path("qwen_3b").write_bytes(FILE + b"junk")
        self._serve(lambda h: _ModelResp(416, b"", {"Content-Range": f"bytes */{len(FILE)}"}))
        with self.assertRaises(local_llm.LocalLLMError):
            local_llm.download_model("qwen_3b")
        self.assertFalse(local_llm.partial_path("qwen_3b").exists())
        self.assertFalse(self._dest().exists())

    def test_more_than_the_server_announced_is_never_installed(self):
        self._serve(lambda h: _ModelResp(200, FILE + b"extra", {"Content-Length": str(len(FILE))}))
        with self.assertRaises(local_llm.LocalLLMError) as cm:
            local_llm.download_model("qwen_3b")
        self.assertIn("starts over", str(cm.exception))
        self.assertFalse(self._dest().exists())
        self.assertFalse(local_llm.partial_path("qwen_3b").exists())

    def test_a_cut_off_model_download_is_kept_to_resume_not_installed(self):
        self._serve(lambda h: _ModelResp(200, FILE[:400], {"Content-Length": str(len(FILE))}))
        with self.assertRaises(local_llm.LocalLLMError):
            local_llm.download_model("qwen_3b")
        self.assertFalse(self._dest().exists())
        self.assertEqual(local_llm.partial_path("qwen_3b").stat().st_size, 400)

    def test_one_download_per_model_at_a_time(self):
        seen = {}

        def reply(h):
            # While the first download runs: a second one is refused, and
            # Settings can tell it's running.
            seen["running"] = local_llm.downloading("qwen_3b")
            with self.assertRaises(local_llm.LocalLLMError) as cm:
                local_llm.download_model("qwen_3b")
            seen["second"] = str(cm.exception)
            with self.assertRaises(local_llm.LocalLLMError):
                local_llm.remove_model("qwen_3b")
            return _ModelResp(200, FILE, {"Content-Length": str(len(FILE))})
        self._serve(reply)
        local_llm.download_model("qwen_3b")
        self.assertTrue(seen["running"])
        self.assertIn("already downloading", seen["second"])
        self.assertFalse(local_llm.downloading("qwen_3b"))
        self.assertEqual(self._dest().read_bytes(), FILE)

    def test_a_failed_download_frees_the_model_for_the_next_try(self):
        def reply(h):
            raise ConnectionError("reset")
        self._serve(reply)
        with self.assertRaises(ConnectionError):
            local_llm.download_model("qwen_3b")
        self.assertFalse(local_llm.downloading("qwen_3b"))

    def test_a_cut_off_download_reads_as_one_for_people(self):
        import model_manager
        self._serve(lambda h: _ModelResp(200, FILE[:400], {"Content-Length": str(len(FILE))}))
        with self.assertRaises(local_llm.LocalLLMError) as cm:
            local_llm.download_model("qwen_3b")
        self.assertEqual(model_manager.failure_message(cm.exception),
                         ("incomplete", model_manager.MSG_INCOMPLETE))

    @unittest.skipUnless(os.environ.get("TRANSCRIBE_NET_TESTS"), "set TRANSCRIBE_NET_TESTS=1 to check live URLs")
    def test_every_model_url_is_live(self):
        import requests
        for mid, info in local_llm.MODEL_CATALOG.items():
            r = requests.head(local_llm.model_url(mid), allow_redirects=True, timeout=30)
            self.assertEqual(r.status_code, 200, mid)
            self.assertEqual(int(r.headers.get("Content-Length", 0)), info["size"], mid)


if __name__ == "__main__":
    unittest.main()
