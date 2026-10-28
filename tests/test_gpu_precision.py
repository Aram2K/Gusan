"""NVIDIA precision (gpu_precision.py + main.AudioRecorder, Gusan 2.0.1): float16 when the card runs it
and has the memory, int8_float16 and int8 below that, the processor last; a level that runs out of
memory steps down one and is remembered for that model. The processor stays int8. Fake CTranslate2 and
fake Whisper models only - no card, no real model.
"""
import logging
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Never the real settings, keyring or app data (main loads config at import).
os.environ.setdefault("TRANSCRIBE_APP_DATA_DIR", tempfile.mkdtemp(prefix="gusan-gpuprec-data-"))
os.environ.setdefault("TRANSCRIBE_DISABLE_KEYRING", "1")
os.environ.setdefault("TRANSCRIBE_SKIP_MIGRATION", "1")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gpu_accel  # noqa: E402
import gpu_precision  # noqa: E402
import main  # noqa: E402

OOM = "CUDA failed with error out of memory"

# What CTranslate2's get_supported_compute_types("cuda") says for a few cards.
CC_89 = ["bfloat16", "float16", "float32", "int8", "int8_bfloat16", "int8_float16", "int8_float32"]   # RTX 40xx
CC_75 = ["float16", "float32", "int8", "int8_float16", "int8_float32"]                                  # RTX 20xx, GTX 16xx
CC_61 = ["float32", "int8", "int8_float32"]                                                           # GTX 10xx: fp16 runs as fp32
CC_OLD = ["float32"]


def fake_ct2(types=CC_75, count=1):
    def supported(device):
        if types is None:
            raise RuntimeError("CUDA failed with error CUDA driver version is insufficient")
        return set(types) if device == "cuda" else {"float32", "int8", "int8_float32"}
    return SimpleNamespace(get_cuda_device_count=lambda: count, get_supported_compute_types=supported)


class TestLadder(unittest.TestCase):
    def test_what_the_card_can_run(self):
        self.assertEqual(sorted(gpu_precision.supported_cuda_types(fake_ct2(CC_75))), sorted(CC_75))
        self.assertIsNone(gpu_precision.supported_cuda_types(fake_ct2(None)))                # no driver: cannot be asked
        self.assertIsNone(gpu_precision.supported_cuda_types(SimpleNamespace()))             # an old build without the call
        self.assertIsNone(gpu_precision.supported_cuda_types(object()))

    def test_the_best_precision_per_card(self):
        best = gpu_precision.best_cuda_type
        self.assertEqual(best(fake_ct2(CC_89)), "float16")
        self.assertEqual(best(fake_ct2(CC_75)), "float16")
        self.assertEqual(best(fake_ct2(["float32", "int8", "int8_float16"])), "int8_float16")
        self.assertEqual(best(fake_ct2(CC_61)), "int8")              # float16 would run as float32 there
        self.assertIsNone(best(fake_ct2(CC_OLD)))                   # the processor is better
        self.assertEqual(best(fake_ct2(None)), "int8_float16")       # cannot be asked: what 2.0.0 always used
        self.assertEqual(best(SimpleNamespace()), "int8_float16")

    def test_the_steps_below_a_level(self):
        low = gpu_precision.lower_types
        self.assertEqual(low("float16"), ["int8_float16", "int8"])
        self.assertEqual(low("int8_float16"), ["int8"])
        self.assertEqual(low("int8"), [])
        self.assertEqual(low("float16", ("float16", "int8")), ["int8"])                       # only what is supported
        self.assertEqual(low("float16", ("float16", "int8_float16", "int8"), skip=("int8_float16",)), ["int8"])
        self.assertEqual(low("float32"), [])                                                  # not on the ladder
        self.assertEqual(low(None), [])
        self.assertEqual(gpu_precision.LADDER, ("float16", "int8_float16", "int8"))

    def test_the_memory_a_model_needs(self):
        need = gpu_precision.need_mb
        self.assertGreater(need("large-v3", "float16"), need("large-v3", "int8_float16"))
        self.assertEqual(need("large-v3", "int8_float16"), need("large-v3", "int8"))
        self.assertGreater(need("large-v3", "float16"), 3000)                                 # 3 GB of weights plus the maths
        self.assertLess(need("tiny", "float16"), 1100)
        self.assertIsNone(need("a-model-nobody-knows", "float16"))
        self.assertIsNone(need("hy-fastconformer", "float16"))

    def test_the_plan_for_a_model(self):
        plan = gpu_precision.plan
        self.assertEqual(plan("small", "float16"), (["float16", "int8_float16", "int8"], ""))
        self.assertEqual(plan("small", "float16", free_mb=8000), (["float16", "int8_float16", "int8"], ""))
        self.assertEqual(plan("small", "int8_float16"), (["int8_float16", "int8"], ""))
        self.assertEqual(plan("small", "int8"), (["int8"], ""))
        self.assertEqual(plan("small", "float16", supported=("float16", "int8")), (["float16", "int8"], ""))
        self.assertEqual(plan("small", "float16", skip=("float16",)), (["int8_float16", "int8"], ""))
        self.assertEqual(plan("small", "float16", skip=("float16", "int8_float16", "int8")), ([], ""))
        self.assertEqual(plan("small", None), ([], ""))
        self.assertEqual(plan("small", "float32"), (["float32"], ""))                         # not ours to judge

    def test_float16_is_left_out_when_the_card_is_short_of_memory(self):
        chain, why = gpu_precision.plan("large-v3", "float16", free_mb=2000)
        self.assertEqual(chain, ["int8_float16", "int8"])
        self.assertIn("2000", why)
        self.assertIn("float16", why)
        chain, why = gpu_precision.plan("large-v3", "float16", free_mb=gpu_precision.need_mb("large-v3", "float16"))
        self.assertEqual(chain[0], "float16")                                                 # exactly enough: float16
        self.assertEqual(gpu_precision.plan("large-v3", "float16", free_mb=None)[0][0], "float16")   # not known: tried
        self.assertEqual(gpu_precision.plan("mystery", "float16", free_mb=1)[0][0], "float16")       # not known: tried

    def test_free_video_memory_from_nvidia_smi(self):
        def run(out, code=0, exc=None):
            def fake(cmd, **kw):
                if exc:
                    raise exc
                return SimpleNamespace(returncode=code, stdout=out)
            return fake
        free = gpu_precision.free_vram_mb
        self.assertEqual(free(run("7312\n"), paths=["nvidia-smi"]), 7312)
        self.assertEqual(free(run("7312\n2048\n"), paths=["nvidia-smi"]), 7312)                # the first card
        self.assertIsNone(free(run("N/A\n"), paths=["nvidia-smi"]))
        self.assertIsNone(free(run("", 1), paths=["nvidia-smi"]))
        self.assertIsNone(free(run("", exc=FileNotFoundError()), paths=["nvidia-smi"]))
        self.assertIsNone(free(run("", exc=subprocess.TimeoutExpired("nvidia-smi", 4)), paths=["nvidia-smi"]))
        self.assertIsNone(free(run("100"), paths=[]))                                          # no nvidia-smi on this computer
        seen = []
        free(lambda cmd, **kw: seen.append((cmd, kw)) or SimpleNamespace(returncode=0, stdout="1"), paths=["x"])
        self.assertIn("--query-gpu=memory.free", seen[0][0])
        self.assertLessEqual(seen[0][1]["timeout"], 5)
        self.assertTrue(seen[0][1]["capture_output"])


class TestDeviceChoice(unittest.TestCase):
    """AudioRecorder._whisper_device: where and at what precision."""

    def device(self, types=CC_75, count=1, libs="C:/gpu", failed=None):
        ct2 = fake_ct2(types, count)
        with mock.patch.dict(sys.modules, {"ctranslate2": ct2}), \
                mock.patch.object(main.sys, "platform", "win32"), \
                mock.patch.object(main.gpu_accel, "libs_dir", lambda: libs), \
                mock.patch.dict(main.cfg, {}, clear=False):
            main.cfg.pop(gpu_accel.CFG_FAILED, None)
            if failed:
                main.cfg[gpu_accel.CFG_FAILED] = failed
            return main.AudioRecorder._whisper_device()

    def test_float16_on_a_card_that_runs_it(self):
        self.assertEqual(self.device(CC_89), ("cuda", "float16"))
        self.assertEqual(self.device(CC_75), ("cuda", "float16"))

    def test_int8_on_an_old_card(self):
        self.assertEqual(self.device(CC_61), ("cuda", "int8"))

    def test_int8_float16_when_only_that_is_offered_or_the_card_cannot_be_asked(self):
        self.assertEqual(self.device(["float32", "int8", "int8_float16"]), ("cuda", "int8_float16"))
        self.assertEqual(self.device(None), ("cuda", "int8_float16"))

    def test_the_processor_stays_int8(self):
        self.assertEqual(self.device(count=0), ("cpu", "int8"))
        self.assertEqual(self.device(CC_OLD), ("cpu", "int8"))
        self.assertEqual(self.device(libs=None), ("cpu", "int8"))                  # no cuBLAS yet (the optional GPU download)
        self.assertEqual(self.device(failed="driver too old"), ("cpu", "int8"))     # the GPU proved unusable

    def test_on_a_computer_that_is_not_windows_no_gpu_download_is_needed(self):
        ct2 = fake_ct2(CC_75)
        with mock.patch.dict(sys.modules, {"ctranslate2": ct2}), mock.patch.object(main.sys, "platform", "linux"), \
                mock.patch.dict(main.cfg, {}, clear=False):
            main.cfg.pop(gpu_accel.CFG_FAILED, None)
            self.assertEqual(main.AudioRecorder._whisper_device(), ("cuda", "float16"))


def recorder():
    """An AudioRecorder without PyAudio, as test_idle_unload builds it."""
    rec = main.AudioRecorder.__new__(main.AudioRecorder)
    rec._model_lock = threading.Lock()
    rec._users_lock = threading.Lock()
    rec._infer_lock = threading.Lock()
    rec._model = None
    rec._model_name = None
    rec._model_users = 0
    rec._last_model_use = 0.0
    rec.recording = False
    rec._chunk_threads = []
    rec._add_cuda_dll_dirs = lambda: None
    return rec


class FakeWhisper:
    """A WhisperModel: builds unless ``fail`` says no, computes unless ``compute_fail`` says no."""
    built = []
    refs = []                 # weak references to every model built so far
    alive_at_build = []       # how many earlier models were still alive when each one was built
    fail = staticmethod(lambda name, device, ct, local_only: None)
    compute_fail = staticmethod(lambda self_: None)

    def __init__(self, name, device="cpu", compute_type="default", local_files_only=True, **kw):
        FakeWhisper.alive_at_build.append(sum(1 for r in FakeWhisper.refs if r() is not None))
        FakeWhisper.refs.append(weakref.ref(self))
        FakeWhisper.built.append((name, device, compute_type, local_files_only))
        err = FakeWhisper.fail(name, device, compute_type, local_files_only)
        if err:
            raise RuntimeError(err)
        self.name, self.device, self.compute_type = name, device, compute_type

    def transcribe(self, *a, **k):
        err = FakeWhisper.compute_fail(self)
        if err:
            raise RuntimeError(err)
        return iter([]), SimpleNamespace(language="en")


class TestPrecisionSteps(unittest.TestCase):
    def setUp(self):
        FakeWhisper.built = []
        FakeWhisper.fail = staticmethod(lambda name, device, ct, local_only: None)
        FakeWhisper.compute_fail = staticmethod(lambda self_: None)
        self.rec = recorder()
        self.reported = []
        self.rec.on_cuda_failed = self.reported.append
        self.free = None
        patches = [
            mock.patch.object(gpu_precision, "free_vram_mb", lambda *a, **k: self.free),
            mock.patch.object(gpu_precision, "supported_cuda_types", lambda ct2=None: tuple(CC_75)),
            mock.patch.object(main, "_looks_like_whisper_cache_error", lambda e: False),
            mock.patch.object(main.logger, "warning", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def load(self, name="small", device=("cuda", "float16"), rec=None):
        rec = rec or self.rec
        rec._whisper_device = lambda: device
        with mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=FakeWhisper)}):
            rec.load_model(name)
        return rec

    def tries(self):
        return [(d, c) for _n, d, c, _l in FakeWhisper.built]

    def test_float16_first_on_a_card_that_runs_it(self):
        self.load()
        self.assertEqual(self.tries(), [("cuda", "float16")])
        self.assertEqual((self.rec._model.device, self.rec._model.compute_type), ("cuda", "float16"))
        self.assertEqual(self.rec._model._gusan_compute, "float16")
        self.assertIs(self.rec._cuda_usable, True)

    def test_the_processor_is_int8_always(self):
        self.load(device=("cpu", "int8"))
        self.assertEqual(self.tries(), [("cpu", "int8")])
        self.assertEqual(self.rec._model._gusan_compute, "int8")

    def test_an_old_card_starts_at_int8(self):
        self.load(device=("cuda", "int8"))
        self.assertEqual(self.tries(), [("cuda", "int8")])

    def test_out_of_memory_steps_down_one_level_and_remembers_it(self):
        FakeWhisper.fail = staticmethod(lambda n, d, ct, lo: OOM if (n, d, ct) == ("large-v3", "cuda", "float16") else None)
        self.load("large-v3")
        self.assertEqual(self.tries(), [("cuda", "float16"), ("cuda", "int8_float16")])      # not twice at float16
        self.assertEqual(self.rec._model.compute_type, "int8_float16")
        self.assertEqual(self.rec._oom_types(), {"large-v3": {"float16"}})
        self.assertNotIn("large-v3", self.rec._oom_models())                                  # it still uses the GPU
        self.assertIsNot(self.rec.__dict__.get("_cuda_usable"), False)
        self.assertEqual(self.reported, [])                                                   # not the GPU's fault
        FakeWhisper.built.clear()
        self.rec.unload_model()
        self.load("large-v3")                                                                 # the next load goes straight there
        self.assertEqual(self.tries(), [("cuda", "int8_float16")])
        FakeWhisper.built.clear()
        self.load("tiny")                                                                     # another model: float16 again
        self.assertEqual(self.tries(), [("cuda", "float16")])

    def test_every_level_out_of_memory_leaves_it_on_the_processor_for_that_model_only(self):
        FakeWhisper.fail = staticmethod(lambda n, d, ct, lo: OOM if d == "cuda" and n == "large-v3" else None)
        self.load("large-v3")
        self.assertEqual(self.tries(), [("cuda", "float16"), ("cuda", "int8_float16"), ("cuda", "int8"), ("cpu", "int8")])
        self.assertEqual((self.rec._model.device, self.rec._model.compute_type), ("cpu", "int8"))
        self.assertIn("large-v3", self.rec._oom_models())
        self.assertIsNot(self.rec.__dict__.get("_cuda_usable"), False)
        FakeWhisper.built.clear()
        self.rec.unload_model()
        self.load("large-v3")
        self.assertEqual(self.tries(), [("cpu", "int8")])                                      # no second doomed attempt
        FakeWhisper.built.clear()
        self.load("small")
        self.assertEqual(self.tries(), [("cuda", "float16")])                                  # smaller models still use the GPU

    def test_a_card_that_cannot_run_the_gpu_stack_is_not_asked_a_second_level(self):
        FakeWhisper.compute_fail = staticmethod(
            lambda m: "Library cublas64_12.dll is not found or cannot be loaded" if m.device == "cuda" else None)
        self.load()
        self.assertEqual(self.tries(), [("cuda", "float16"), ("cpu", "int8")])
        self.assertIs(self.rec._cuda_usable, False)
        self.assertEqual(len(self.reported), 1)
        self.assertEqual(self.rec._oom_types(), {})

    def test_out_of_memory_on_the_first_computation_steps_down_too(self):
        FakeWhisper.compute_fail = staticmethod(lambda m: OOM if (m.device, m.compute_type) == ("cuda", "float16") else None)
        self.load("medium")
        self.assertEqual(self.tries(), [("cuda", "float16"), ("cuda", "int8_float16")])
        self.assertEqual(self.rec._model.compute_type, "int8_float16")
        self.assertEqual(self.reported, [])

    def test_a_level_that_failed_gives_its_video_memory_back_before_the_next_one_loads(self):
        """The first computation runs out of memory: the model that was just built is still held by the
        exception's traceback. If it stayed alive while the next level loads, the card (which had no room
        for float16) would need room for both and every level below would fail too."""
        FakeWhisper.refs.clear()
        FakeWhisper.alive_at_build.clear()
        FakeWhisper.compute_fail = staticmethod(
            lambda m: OOM if m.device == "cuda" and m.compute_type in ("float16", "int8_float16") else None)
        self.load("large-v3")
        self.assertEqual(self.tries(), [("cuda", "float16"), ("cuda", "int8_float16"), ("cuda", "int8")])
        self.assertEqual(self.rec._model.compute_type, "int8")
        self.assertEqual(FakeWhisper.alive_at_build, [0, 0, 0], "a failed model was still alive when the next one loaded")

    def test_a_cache_that_is_not_there_yet_is_not_the_gpus_fault(self):
        class NotThere(Exception):
            pass

        def fail(n, d, ct, lo):
            if lo:
                raise NotThere("pass 'local_files_only=False'")
        FakeWhisper.fail = staticmethod(fail)
        self.load()
        self.assertEqual(FakeWhisper.built[0][1:], ("cuda", "float16", True))
        self.assertEqual(FakeWhisper.built[1][1:], ("cuda", "float16", False))                # online, same level
        self.assertEqual(self.rec._model.compute_type, "float16")

    def test_a_card_short_of_memory_skips_float16_before_trying_it(self):
        self.free = 1500
        self.load("large-v3")
        self.assertEqual(self.tries(), [("cuda", "int8_float16")])
        self.free = 20000
        FakeWhisper.built.clear()
        self.rec.unload_model()
        self.load("large-v3")
        self.assertEqual(self.tries(), [("cuda", "float16")])

    def test_the_choice_is_logged_once_per_model(self):
        self.free = 1500
        with self.assertLogs("transcribe", level="INFO") as logs:
            self.load("large-v3")
            self.rec.unload_model()
            self.load("large-v3")
        lines = [m for m in logs.output if "GPU precision for large-v3" in m]
        self.assertEqual(len(lines), 1)
        self.assertIn("int8_float16", lines[0])
        self.assertIn("1500", lines[0])

    def test_without_the_card_there_is_no_plan_at_all(self):
        with mock.patch.object(gpu_precision, "plan", side_effect=AssertionError("planned")):
            self.load(device=("cpu", "int8"))
        self.assertEqual(self.tries(), [("cpu", "int8")])


class TestStepDownWhileWorking(unittest.TestCase):
    """The card runs out of memory in the middle of a dictation or a file."""

    def setUp(self):
        FakeWhisper.built = []
        FakeWhisper.fail = staticmethod(lambda name, device, ct, local_only: None)
        FakeWhisper.compute_fail = staticmethod(lambda self_: None)
        self.rec = recorder()
        self.reported = []
        self.rec.on_cuda_failed = self.reported.append
        self.rec._cuda_usable = True                       # the card is proven: no warm-up at build
        self.rec._whisper_device = lambda: ("cuda", "float16")
        patches = [
            mock.patch.object(gpu_precision, "free_vram_mb", lambda *a, **k: None),
            mock.patch.object(gpu_precision, "supported_cuda_types", lambda ct2=None: tuple(CC_75)),
            mock.patch.object(main, "_looks_like_whisper_cache_error", lambda e: False),
            mock.patch.object(main.logger, "warning", lambda *a, **k: None),
            mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=FakeWhisper)}),
            mock.patch.dict(main.cfg, {"whisper_model": "large-v3", "sample_rate": 16000}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.np = mock.patch.object(main, "np", SimpleNamespace(asarray=lambda a, dtype=None: a, float32=None))
        self.np.start()
        self.addCleanup(self.np.stop)

    def levels(self):
        return [(d, c) for _n, d, c, _l in FakeWhisper.built]

    def segments_fn(self, fail_levels, final=None):
        out = final if final is not None else [{"start": 0.0, "end": 1.0, "text": "hello"}]

        def segments(rec, model, audio, sr, language, on_progress, should_cancel, gpu_errors=False):
            if (model.device, model.compute_type) in fail_levels:
                try:
                    raise RuntimeError(OOM)
                except RuntimeError as e:
                    if gpu_errors:
                        raise main._GpuDecodeError(str(e))
                    return []
            return out
        return segments

    def test_a_file_steps_down_float16_to_int8_float16_to_int8_and_never_says_no_speech(self):
        fail = {("cuda", "float16"), ("cuda", "int8_float16")}
        with mock.patch.object(main.AudioRecorder, "_segments_with", self.segments_fn(fail)):
            out = main.AudioRecorder.transcribe_segments(self.rec, [0.0] * 16000, model_name="large-v3")
        self.assertEqual(out, [{"start": 0.0, "end": 1.0, "text": "hello"}])
        self.assertEqual(self.levels(), [("cuda", "float16"), ("cuda", "int8_float16"), ("cuda", "int8")])
        self.assertEqual(self.rec._oom_types(), {"large-v3": {"float16", "int8_float16"}})
        self.assertNotIn("large-v3", self.rec._oom_models())
        self.assertEqual(self.reported, [])

    def test_when_even_int8_does_not_fit_the_processor_reads_it(self):
        fail = {("cuda", "float16"), ("cuda", "int8_float16"), ("cuda", "int8")}
        with mock.patch.object(main.AudioRecorder, "_segments_with", self.segments_fn(fail)):
            out = main.AudioRecorder.transcribe_segments(self.rec, [0.0] * 16000, model_name="large-v3")
        self.assertEqual(out[0]["text"], "hello")
        self.assertEqual(self.levels(), [("cuda", "float16"), ("cuda", "int8_float16"), ("cuda", "int8"), ("cpu", "int8")])
        self.assertIn("large-v3", self.rec._oom_models())
        self.assertIs(self.rec._cuda_usable, True)                  # out of memory is about the model, not the card

    def test_something_else_going_wrong_on_the_card_goes_straight_to_the_processor(self):
        def segments(rec, model, audio, sr, language, on_progress, should_cancel, gpu_errors=False):
            if model.device == "cuda":
                try:
                    raise RuntimeError("CUDA failed with error unspecified launch failure")
                except RuntimeError as e:
                    raise main._GpuDecodeError(str(e))
            return [{"start": 0.0, "end": 1.0, "text": "cpu"}]
        with mock.patch.object(main.AudioRecorder, "_segments_with", segments):
            out = main.AudioRecorder.transcribe_segments(self.rec, [0.0] * 16000, model_name="large-v3")
        self.assertEqual(out[0]["text"], "cpu")
        self.assertEqual(self.levels(), [("cuda", "float16"), ("cpu", "int8")])
        self.assertIs(self.rec._cuda_usable, False)
        self.assertEqual(len(self.reported), 1)

    def test_a_dictation_piece_steps_down_and_is_read_again(self):
        calls = []

        def run_with(model, audio):
            calls.append((model.device, model.compute_type))
            if model.compute_type == "float16":
                raise RuntimeError(OOM)
            return "hello", "en"
        self.rec._run_local_with = run_with
        self.rec._gpu_failed_mid_use = lambda failed, name, err: main.AudioRecorder._gpu_failed_mid_use(
            self.rec, failed, name, err)
        with mock.patch.dict(main.cfg, {"whisper_model": "large-v3"}):
            out = main.AudioRecorder._run_local_once(self.rec, [0.0] * 16000)
        self.assertEqual(out, ("hello", "en"))
        self.assertEqual(calls, [("cuda", "float16"), ("cuda", "int8_float16")])

    def test_a_model_without_a_recorded_level_goes_to_the_processor_as_before(self):
        class Plain:
            device = "cuda"
        self.rec._model = Plain()
        self.rec._model_name = "large-v3"
        main.AudioRecorder._gpu_failed_mid_use(self.rec, self.rec._model, "large-v3", OOM)
        self.assertIn("large-v3", self.rec._oom_models())
        self.assertIsNone(self.rec._model)


class TestLogLine(unittest.TestCase):
    def test_the_build_still_says_what_it_runs_on(self):
        FakeWhisper.built = []
        rec = recorder()
        rec._whisper_device = lambda: ("cuda", "float16")
        with mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=FakeWhisper)}), \
                mock.patch.object(gpu_precision, "free_vram_mb", lambda *a, **k: None), \
                mock.patch.object(gpu_precision, "supported_cuda_types", lambda ct2=None: tuple(CC_75)):
            with self.assertLogs("transcribe", level="INFO") as logs:
                rec.load_model("small")
        self.assertTrue(any("Whisper on cuda (float16" in m for m in logs.output), logs.output)


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
