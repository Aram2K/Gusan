"""Gusan 2.0: hardware.py - the 'Best for your computer' rule, the model names
and the hardware snapshot.

Pure: imports neither main nor ui (test_core may already have replaced psutil,
PySide6 and requests with stand-ins - nothing here depends on them; every probe
is patched).
"""
import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("TRANSCRIBE_APP_DATA_DIR", tempfile.mkdtemp(prefix="gusan-hw-test-"))
os.environ.setdefault("TRANSCRIBE_DISABLE_KEYRING", "1")
os.environ.setdefault("TRANSCRIBE_SKIP_MIGRATION", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hardware  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _main_models():
    """main.MODELS read from the source (importing main would start the app's setup)."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "MODELS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("MODELS not found in main.py")


class TestRecommendation(unittest.TestCase):
    def rec(self, ram, cores, gpu="unsupported", platform="win32"):
        return hardware.recommend_model(ram, cores, gpu, platform)

    def test_mockup_computer_gets_balanced(self):
        # i7-1260P · 12 cores · 16 GB (15.7 reported) · Iris Xe, no NVIDIA.
        r = self.rec(15.7, 12, "unsupported")
        self.assertEqual(r.model, "small")
        self.assertEqual(r.plain, "Balanced")
        self.assertFalse(r.suggest_cloud)
        self.assertFalse(r.gpu)

    def test_matrix(self):
        cases = [
            # ram, cores, gpu_state, platform -> model
            (16, 8, "ready", "win32", "large-v3-turbo"),
            (16, 8, "available", "win32", "large-v3-turbo"),
            (8, 4, "ready", "win32", "large-v3-turbo"),        # GPU rule ignores cores
            (16, 8, "failed", "win32", "small"),               # failed -> CPU rules
            (16, 8, "unsupported", "win32", "small"),
            (16, 8, None, "win32", "small"),                   # not probed yet
            (16, 4, "unsupported", "win32", "base"),           # 8 GB but < 6 cores
            (32, 5, "failed", "win32", "base"),
            (6, 12, "ready", "win32", "base"),                 # GPU but under 8 GB
            (6, 12, "unsupported", "win32", "base"),
            (4, 2, "unsupported", "win32", "base"),
            (3, 4, "unsupported", "win32", "tiny"),
            (2, 2, "available", "win32", "tiny"),
            (16, 10, "ready", "darwin", "small"),              # Mac never uses the GPU
            (16, 10, "available", "darwin", "small"),
            (8, 4, "ready", "darwin", "base"),
            (64, 16, "ready", "linux", "large-v3-turbo"),
        ]
        for ram, cores, gpu, plat, want in cases:
            with self.subTest(ram=ram, cores=cores, gpu=gpu, platform=plat):
                self.assertEqual(self.rec(ram, cores, gpu, plat).model, want)

    def test_ram_tolerance(self):
        # What an 8 GB / 4 GB machine really reports still counts as 8 / 4.
        self.assertEqual(self.rec(7.8, 6).model, "small")
        self.assertEqual(self.rec(7.4, 6).model, "small")
        self.assertEqual(self.rec(7.0, 6).model, "small")
        self.assertEqual(self.rec(7.8, 8, "available").model, "large-v3-turbo")
        self.assertEqual(self.rec(3.8, 2).model, "base")
        self.assertEqual(self.rec(3.5, 2).model, "base")
        # ...but a 6 GB machine is not an 8 GB one, nor 2-3 GB a 4 GB one.
        self.assertEqual(self.rec(5.8, 8).model, "base")
        self.assertEqual(self.rec(6.5, 8).model, "base")
        self.assertEqual(self.rec(2.9, 8).model, "tiny")

    def test_gpu_flags(self):
        r = self.rec(16, 8, "available")
        self.assertTrue(r.gpu)
        self.assertTrue(r.needs_gpu_download)        # offer the ~528 MB GPU download
        r = self.rec(16, 8, "ready")
        self.assertTrue(r.gpu)
        self.assertFalse(r.needs_gpu_download)
        self.assertFalse(self.rec(16, 8, "failed").needs_gpu_download)

    def test_low_memory_suggests_cloud(self):
        r = self.rec(2, 2)
        self.assertEqual((r.model, r.suggest_cloud, r.reason), ("tiny", True, "low_memory"))
        self.assertFalse(self.rec(4, 2).suggest_cloud)

    def test_unknown_values(self):
        self.assertEqual(self.rec(None, 8).model, "base")      # unknown RAM: app default
        self.assertEqual(self.rec(0, 8).model, "base")
        self.assertEqual(self.rec(16, None).model, "base")     # unknown cores: < 6
        self.assertEqual(self.rec("x", 8).reason, "unknown")

    def test_default_platform_is_this_one(self):
        with patch.object(hardware.sys, "platform", "darwin"):
            self.assertEqual(hardware.recommend_model(16, 8, "ready").model, "small")


class TestDisplayRam(unittest.TestCase):
    def test_rounds_to_the_nominal_size(self):
        for ram, want in [(15.7, 16), (7.8, 8), (7.4, 8), (13.9, 16), (11.8, 12), (31.9, 32),
                          (63.7, 64), (3.9, 4), (1.9, 2), (17.9, 18), (5.8, 6), (16, 16), (8.0, 8)]:
            with self.subTest(ram=ram):
                self.assertEqual(hardware.display_ram_gb(ram), want)

    def test_odd_sizes_round_normally(self):
        self.assertEqual(hardware.display_ram_gb(10.0), 10)
        self.assertEqual(hardware.display_ram_gb(6.5), 7)
        self.assertEqual(hardware.display_ram_gb(600), 600)

    def test_unknown(self):
        for v in (None, 0, -1, "x"):
            self.assertEqual(hardware.display_ram_gb(v), 0)


class TestModelNames(unittest.TestCase):
    def test_speech_table(self):
        want = {
            "tiny": ("Lightest", "Whisper Tiny", "75 MB", 2),
            "base": ("Fast", "Whisper Base", "140 MB", 2),
            "small": ("Balanced", "Whisper Small", "460 MB", 4),
            "medium": ("Accurate", "Whisper Medium", "1.4 GB", 6),
            "large-v3-turbo": ("Most accurate", "Whisper Large v3 Turbo", "1.6 GB", 6),
            "large-v3": ("Maximum", "Whisper Large v3", "3 GB", 8),
        }
        self.assertEqual(list(hardware.MODEL_INFO), list(want))
        for mid, (plain, real, size, mem) in want.items():
            i = hardware.MODEL_INFO[mid]
            self.assertEqual((i.plain, i.real, i.size, i.memory_gb, i.kind), (plain, real, size, mem, "speech"))
        self.assertEqual(hardware.MAIN_SPEECH_MODELS, ("base", "small", "large-v3-turbo"))
        self.assertEqual(hardware.MORE_SPEECH_MODELS, ("tiny", "medium", "large-v3"))

    def test_speech_table_matches_main(self):
        models = _main_models()
        self.assertEqual(set(models), set(hardware.MODEL_INFO))
        for mid, m in models.items():
            i = hardware.MODEL_INFO[mid]
            self.assertEqual(i.size, m["size"], mid)
            self.assertEqual(i.memory_gb, m["min_ram"], mid)
            self.assertEqual(i.speed_rank, m["speed_rank"], mid)

    def test_meters_match_the_mockup(self):
        # Onboarding cards: Fast 5/3, Balanced 4/4, Most accurate 3/5.
        got = {m: (hardware.MODEL_INFO[m].speed, hardware.MODEL_INFO[m].accuracy)
               for m in hardware.MAIN_SPEECH_MODELS}
        self.assertEqual(got, {"base": (5, 3), "small": (4, 4), "large-v3-turbo": (3, 5)})
        for i in hardware.MODEL_INFO.values():
            self.assertTrue(1 <= i.speed <= 5 and 1 <= i.accuracy <= 5, i.id)

    def test_local_ai_table(self):
        want = {"qwen_tiny": ("Small", "Qwen 1.5B", "1.12 GB", 8),
                "qwen_3b": ("Medium", "Qwen 3B", "2.1 GB", 12),
                "qwen_7b": ("Large", "Qwen 7B", "4.68 GB", 16),
                "gemma_2b": ("Alternative", "Gemma 2 2B", "1.71 GB", 8)}
        for mid, (plain, real, size, mem) in want.items():
            i = hardware.LOCAL_AI_INFO[mid]
            self.assertEqual((i.plain, i.real, i.size, i.memory_gb, i.kind), (plain, real, size, mem, "writer"))

    def test_local_ai_table_matches_local_llm(self):
        import local_llm
        self.assertEqual(set(hardware.LOCAL_AI_INFO), set(local_llm.MODEL_CATALOG))
        for mid, entry in local_llm.MODEL_CATALOG.items():
            i = hardware.LOCAL_AI_INFO[mid]
            self.assertAlmostEqual(float(i.size.split()[0]), entry["size"] / 1e9, places=2, msg=mid)
            self.assertEqual(i.memory_gb, entry["min_ram"], mid)
        self.assertEqual(hardware.LEGACY_LOCAL_AI_IDS, local_llm.LEGACY_MODEL_IDS)

    def test_cloud_names(self):
        # Gusan Cloud chooses its speech engine: the name is its fast profile (engine_choice.CLOUD_PROFILE_NAMES)
        self.assertEqual(hardware.CLOUD_SPEECH_MODEL, "Fast")
        self.assertEqual(hardware.CLOUD_WRITER_MODEL, "Mistral Small")
        self.assertEqual(hardware.CLOUD_LIVE_MODEL, "DeepSeek V4.1 Flash")
        self.assertEqual(hardware.CLOUD_SMART_MODEL, "DeepSeek V4.1 Flash")

    def test_labels(self):
        self.assertEqual(hardware.plain_name("small"), "Balanced")
        self.assertEqual(hardware.real_name("large-v3-turbo"), "Whisper Large v3 Turbo")
        self.assertEqual(hardware.model_label("small"), "Balanced · Whisper Small")
        self.assertEqual(hardware.model_label("small", with_size=True), "Balanced · Whisper Small · 460 MB")
        self.assertEqual(hardware.model_label("qwen_tiny"), "Small · Qwen 1.5B")
        self.assertEqual(hardware.real_name("aibuben_tiny"), "Qwen 1.5B")      # 1.9.x id
        self.assertEqual(hardware.plain_name("distil-large-v3"), "distil-large-v3")
        self.assertIsNone(hardware.info("nope"))

    def test_no_old_name_or_qt(self):
        src = (ROOT / "hardware.py").read_text(encoding="utf-8")
        self.assertNotIn("PySide6", src)
        self.assertNotIn("Transcribe", src)


class TestSpeedWords(unittest.TestCase):
    def test_runs_on_gpu(self):
        self.assertTrue(hardware.runs_on_gpu("ready", None, "win32"))
        self.assertFalse(hardware.runs_on_gpu("ready", False, "win32"))   # proven broken this run
        self.assertFalse(hardware.runs_on_gpu("available", None, "win32"))
        self.assertFalse(hardware.runs_on_gpu("ready", True, "darwin"))

    def test_slower_here(self):
        self.assertTrue(hardware.slower_here("large-v3-turbo", on_gpu=False))
        self.assertFalse(hardware.slower_here("large-v3-turbo", on_gpu=True))
        self.assertFalse(hardware.slower_here("small", on_gpu=False))

    def test_speed_phrase(self):
        self.assertEqual(hardware.speed_phrase(3, False), "Fast on your CPU")
        self.assertEqual(hardware.speed_phrase(3, True), "Very fast on your GPU")
        self.assertEqual(hardware.speed_phrase("x", False), "Fast on your CPU")


class TestCpuAndGpuNames(unittest.TestCase):
    def setUp(self):
        hardware.clear_cache()
        self.addCleanup(hardware.clear_cache)

    def test_clean_and_short_cpu_names(self):
        cases = [
            ("12th Gen Intel(R) Core(TM) i7-1260P", "12th Gen Intel Core i7-1260P", "i7-1260P"),
            ("Intel(R) Core(TM) i7-8650U CPU @ 1.90GHz", "Intel Core i7-8650U", "i7-8650U"),
            ("AMD Ryzen 9 5900X 12-Core Processor", "AMD Ryzen 9 5900X", "Ryzen 9 5900X"),
            ("AMD Ryzen 7 5800H with Radeon Graphics", "AMD Ryzen 7 5800H with Radeon Graphics", "Ryzen 7 5800H"),
            ("Intel(R) Core(TM) Ultra 7 155H", "Intel Core Ultra 7 155H", "Core Ultra 7 155H"),
            ("Apple M2 Pro", "Apple M2 Pro", "Apple M2 Pro"),
        ]
        for raw, clean, short in cases:
            with self.subTest(raw=raw):
                self.assertEqual(hardware.clean_cpu_name(raw), clean)
                self.assertEqual(hardware.short_cpu_name(raw), short)
        self.assertIsNone(hardware.clean_cpu_name(""))
        self.assertIsNone(hardware.short_cpu_name(None))

    def test_cpu_name_is_cached(self):
        calls = []
        with patch.object(hardware, "_probe_cpu_name", side_effect=lambda: calls.append(1) or "Intel(R) Core(TM) i5-9400"):
            self.assertEqual(hardware.cpu_name(), "Intel Core i5-9400")
            self.assertEqual(hardware.cpu_name(), "Intel Core i5-9400")
        self.assertEqual(len(calls), 1)

    def test_pick_gpu(self):
        self.assertEqual(hardware.pick_gpu(["Intel(R) Iris(R) Xe Graphics"], "win32"),
                         ("Intel Iris Xe Graphics", False))
        self.assertEqual(hardware.pick_gpu(["Intel(R) UHD Graphics 630", "NVIDIA GeForce RTX 3060"], "win32"),
                         ("NVIDIA GeForce RTX 3060", True))
        self.assertEqual(hardware.pick_gpu([], "win32"), ("", False))
        self.assertEqual(hardware.pick_gpu(["Apple M2 Pro"], "darwin"), ("Apple M2 Pro", False))

    def test_detect_gpu_keeps_the_settings_behaviour(self):
        # Integrated only -> '' (the old specs line omits the GPU part).
        with patch.object(hardware, "_probe_gpu_names", return_value=["Intel(R) Iris(R) Xe Graphics"]), \
                patch.object(hardware.sys, "platform", "win32"):
            self.assertEqual(hardware.detect_gpu(), "")
        hardware.clear_cache()
        with patch.object(hardware, "_probe_gpu_names",
                          return_value=["Microsoft Basic Display Adapter", "NVIDIA GeForce RTX 4070"]), \
                patch.object(hardware.sys, "platform", "win32"):
            self.assertEqual(hardware.detect_gpu(), "NVIDIA GeForce RTX 4070")

    def test_an_empty_answer_is_asked_again(self):
        # A probe that failed or timed out (PowerShell CIM on a busy boot) must
        # not blank the GPU chip for the whole run: the next snapshot retries.
        probe = MagicMock(side_effect=[[], ["NVIDIA GeForce RTX 3060"], ["never asked"]])
        with patch.object(hardware, "_probe_gpu_names", probe):
            self.assertEqual(hardware.detect_gpus(), [])
            self.assertEqual(hardware.detect_gpus(), ["NVIDIA GeForce RTX 3060"])
            self.assertEqual(hardware.detect_gpus(), ["NVIDIA GeForce RTX 3060"])   # now cached
        self.assertEqual(probe.call_count, 2)
        with patch.object(hardware, "_probe_cpu_name", side_effect=[None, "Apple M2"]):
            self.assertIsNone(hardware.cpu_name())
            self.assertEqual(hardware.cpu_name(), "Apple M2")

    def test_virtual_adapters_are_dropped(self):
        with patch.object(hardware, "_probe_gpu_names",
                          return_value=["Parsec Virtual Display Adapter", "Microsoft Remote Display Adapter",
                                        "Intel(R) UHD Graphics"]):
            self.assertEqual(hardware.detect_gpus(), ["Intel UHD Graphics"])

    def test_windows_gpu_probe_hides_its_console(self):
        seen = {}

        def fake_run(args, **kw):
            seen.update(kw)
            return SimpleNamespace(stdout="Intel(R) Iris(R) Xe Graphics|NVIDIA GeForce RTX 3050 Laptop GPU")

        with patch.object(hardware.sys, "platform", "win32"), patch.object(hardware.subprocess, "run", fake_run):
            names = hardware._probe_gpu_names()
        self.assertEqual(names, ["Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 3050 Laptop GPU"])
        self.assertIn("creationflags", seen)
        self.assertEqual(seen["creationflags"], getattr(hardware.subprocess, "CREATE_NO_WINDOW", 0))
        self.assertLessEqual(seen["timeout"], 6)

    def test_gpu_probe_failure_is_empty(self):
        with patch.object(hardware.sys, "platform", "win32"), \
                patch.object(hardware.subprocess, "run", side_effect=OSError("no powershell")):
            self.assertEqual(hardware._probe_gpu_names(), [])


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        hardware.clear_cache()
        self.addCleanup(hardware.clear_cache)

    def _snap(self, *, probe_gpu=True, platform="win32", ram=15.7, cores=12,
              cpu="12th Gen Intel(R) Core(TM) i7-1260P", gpus=("Intel(R) Iris(R) Xe Graphics",),
              state="unsupported"):
        self.gpu_probe = MagicMock(return_value=list(gpus))
        self.state_probe = MagicMock(return_value=state)
        ps = [patch.object(hardware, "ram_gb", return_value=ram),
              patch.object(hardware, "physical_cores", return_value=cores),
              patch.object(hardware, "_probe_cpu_name", return_value=cpu),
              patch.object(hardware, "_probe_gpu_names", self.gpu_probe),
              patch.object(hardware, "gpu_state", self.state_probe)]
        for p in ps:
            p.start()
        try:
            return hardware.snapshot({"x": 1}, probe_gpu=probe_gpu, platform=platform)
        finally:
            for p in ps:
                p.stop()

    def test_mockup_computer(self):
        s = self._snap()
        self.assertEqual(s.cpu_name, "12th Gen Intel Core i7-1260P")
        self.assertEqual((s.cores, s.ram_gb, s.ram_display_gb), (12, 15.7, 16))
        self.assertEqual((s.gpu_name, s.gpu_discrete, s.gpu_state), ("Intel Iris Xe Graphics", False, "unsupported"))
        self.assertEqual((s.cpu_chip, s.ram_chip, s.gpu_chip), ("i7-1260P · 12 cores", "16 GB memory", "Intel Iris Xe"))
        self.assertEqual(s.recommended.model, "small")
        self.assertFalse(s.on_gpu)
        d = s.to_dict()
        self.assertEqual(d["ram_chip"], "16 GB memory")
        self.assertEqual(d["recommended"]["model"], "small")

    def test_nvidia_computer(self):
        s = self._snap(ram=31.8, cores=8, gpus=("Intel(R) UHD Graphics", "NVIDIA GeForce RTX 4060 Laptop GPU"),
                       state="available")
        self.assertEqual((s.gpu_name, s.gpu_discrete), ("NVIDIA GeForce RTX 4060 Laptop GPU", True))
        self.assertEqual(s.recommended.model, "large-v3-turbo")
        self.assertTrue(s.recommended.needs_gpu_download)

    def test_without_the_gpu_probe(self):
        s = self._snap(probe_gpu=False, state="ready")
        self.gpu_probe.assert_not_called()
        self.state_probe.assert_not_called()
        self.assertEqual((s.gpu_name, s.gpu_state, s.gpu_chip), ("", None, ""))
        self.assertEqual(s.recommended.model, "small")

    def test_state_probe_gets_the_config(self):
        self._snap()
        self.state_probe.assert_called_once_with({"x": 1})

    def test_unknown_everything(self):
        s = self._snap(ram=0.0, cores=0, cpu=None, gpus=(), state="unsupported")
        self.assertEqual((s.cpu_chip, s.ram_chip, s.gpu_chip), ("", "", ""))
        self.assertEqual(s.recommended.model, "base")

    def test_gpu_state_never_raises(self):
        with patch.dict(sys.modules, {"gpu_accel": SimpleNamespace(status=lambda cfg: 1 / 0)}):
            self.assertEqual(hardware.gpu_state({}), "unsupported")
        with patch.dict(sys.modules, {"gpu_accel": SimpleNamespace(status=lambda cfg: "ready")}):
            self.assertEqual(hardware.gpu_state({}), "ready")


if __name__ == "__main__":
    unittest.main()
