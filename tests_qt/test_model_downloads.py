"""app.downloads (model_manager.ModelDownloads): every signal arrives on the GUI
thread, one download per model, removal unloads the recorder's model on a
worker, wait_ready never blocks the GUI thread, and the hardware probe."""
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

import hardware
import model_manager as mm

WAIT = 5.0


class FakeOps:
    def __init__(self, on_disk=(), gate=None):
        self.on_disk = set(on_disk)
        self.gate = gate
        self.downloads = []
        self.started = threading.Event()

    def normalize(self, name):
        return hardware.LEGACY_LOCAL_AI_IDS.get(name, name)

    def downloaded(self, kind, name):
        return name in self.on_disk

    def download(self, kind, name, on_progress):
        self.downloads.append((kind, name))
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(WAIT)
        on_progress(0, 0, 0)
        on_progress(40, 40 * 2**30, 100 * 2**30)        # > 2**31 bytes: must not overflow
        on_progress(100, 100 * 2**30, 100 * 2**30)
        self.on_disk.add(name)

    def remove(self, kind, name):
        self.on_disk.discard(name)


def pump(cond, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


class Recorder:
    """Records every signal with the thread it arrived on."""

    def __init__(self, dl):
        self.items = []
        for name in ("progress", "finished", "removed", "state_changed", "scanned", "hardware_ready"):
            getattr(dl, name).connect(lambda *a, _n=name: self.items.append((_n, a, threading.get_ident())))

    def of(self, name):
        return [a for n, a, _t in self.items if n == name]

    def threads(self):
        return {t for _n, _a, t in self.items}


def make_app():
    calls = []
    app = SimpleNamespace(cfg={"whisper_model": "base"}, calls=calls,
                          recorder=SimpleNamespace(unload_model=lambda name: calls.append(
                              ("unload", name, threading.get_ident()))))
    app.track = lambda event, props=None: calls.append(("track", event, props))
    return app


@pytest.fixture
def made(qapp):
    objs = []
    yield objs
    for o in objs:
        try:
            o.deleteLater()
        except RuntimeError:
            pass
    QApplication.processEvents()


def test_class_is_built_lazily_and_is_a_qobject(qapp):
    cls = mm.ModelDownloads
    assert cls is mm.ModelDownloads
    assert issubclass(cls, QObject)
    assert cls.__name__ == "ModelDownloads"


def test_download_signals_arrive_on_the_gui_thread(made):
    app = make_app()
    ops = FakeOps()
    dl = mm.ModelDownloads(app, ops=ops)
    made.append(dl)
    rec = Recorder(dl)
    assert dl.start("small") is True
    assert pump(lambda: rec.of("finished"))
    assert rec.of("finished") == [("small", "ready", "")]
    states = [a for a in rec.of("state_changed")]
    assert states[0] == ("small", "downloading") and states[-1] == ("small", "ready")
    prog = rec.of("progress")
    assert prog and prog[-1] == ("small", 100, 100 * 2**30, 100 * 2**30)
    assert rec.threads() == {threading.get_ident()}      # all on the GUI thread
    assert dl.state("small") == "ready"
    assert dl.ready("whisper") == ["small"]
    tracked = [c[1] for c in app.calls if c[0] == "track"]
    assert tracked == ["model_download_started", "model_download_completed"]


def test_one_download_per_model(made):
    gate = threading.Event()
    ops = FakeOps(gate=gate)
    dl = mm.ModelDownloads(make_app(), ops=ops)
    made.append(dl)
    rec = Recorder(dl)
    assert dl.start("large-v3-turbo")
    assert ops.started.wait(WAIT)
    assert dl.start("large-v3-turbo")
    assert dl.downloading() == ["large-v3-turbo"]
    gate.set()
    assert pump(lambda: rec.of("finished"))
    assert ops.downloads == [("whisper", "large-v3-turbo")]
    assert len(rec.of("finished")) == 1


def test_wait_ready_never_blocks_the_gui_thread(made):
    gate = threading.Event()
    ops = FakeOps(gate=gate)
    dl = mm.ModelDownloads(make_app(), ops=ops)
    made.append(dl)
    dl.start("small")
    t0 = time.monotonic()
    assert dl.wait_ready("small", 10) is False
    assert time.monotonic() - t0 < 1.0
    # ...but a worker thread really waits.
    result = []
    worker = threading.Thread(target=lambda: result.append(dl.wait_ready("small", WAIT)))
    worker.start()
    gate.set()
    worker.join(WAIT)
    assert result == [True]


def test_wait_ready_on_the_gui_thread_never_touches_the_disk(made):
    ops = FakeOps(on_disk={"base"})
    asked = []
    ops.downloaded = lambda kind, name: asked.append(name) or True
    dl = mm.ModelDownloads(make_app(), ops=ops)
    made.append(dl)
    assert dl.wait_ready("base") is False          # not scanned yet: the map says missing
    assert asked == []
    worker = []
    t = threading.Thread(target=lambda: worker.append(dl.wait_ready("base", 0)))
    t.start()
    t.join(WAIT)
    assert worker == [True] and asked == ["base"]  # a worker re-checks the disk


def test_fetch_is_for_workers(made):
    ops = FakeOps()
    dl = mm.ModelDownloads(make_app(), ops=ops)
    made.append(dl)
    rec = Recorder(dl)
    with pytest.raises(RuntimeError):
        dl.fetch("base")                           # would block the GUI thread
    assert ops.downloads == []
    result, seen = [], []
    t = threading.Thread(target=lambda: result.append(
        dl.fetch("base", on_progress=lambda *a: seen.append(a))))
    t.start()
    t.join(WAIT)
    assert result == ["base"]
    assert seen[-1] == (100, 100 * 2**30, 100 * 2**30)
    assert pump(lambda: rec.of("finished"))        # the cards hear the same download
    assert rec.of("finished") == [("base", "ready", "")]
    assert rec.threads() == {threading.get_ident()}


def test_remove_unloads_the_recorder_model_on_a_worker(made):
    app = make_app()
    fake_main = ModuleType("fake_main")
    disk = {"small"}
    fake_main.model_downloaded = lambda name: name in disk
    fake_main.download_whisper_model = lambda name, on_progress=None: disk.add(name)
    fake_main.remove_whisper_model = lambda name: app.calls.append(("rm", name)) or disk.discard(name) or True
    # The scan also asks local_llm; a cold import of the real one (requests,
    # psutil) can take seconds and made this test time out now and then.
    fake_llm = ModuleType("local_llm")
    fake_llm.model_downloaded = lambda name: False
    with patch.dict(sys.modules, {"main": fake_main, "local_llm": fake_llm}):
        dl = mm.ModelDownloads(app)                    # the real ModelOps
        made.append(dl)
        rec = Recorder(dl)
        dl.scan()
        assert pump(lambda: rec.of("scanned"))
        assert dl.state("small") == "ready"
        assert dl.remove("small") is True
        assert pump(lambda: rec.of("removed"))
    assert rec.of("removed") == [("small", True, "")]
    assert dl.state("small") == "missing"
    unload = [c for c in app.calls if c[0] == "unload"]
    assert unload and unload[0][1] == "small"
    assert unload[0][2] != threading.get_ident()      # not on the GUI thread
    assert app.calls.index(unload[0]) < app.calls.index(("rm", "small"))
    assert ("track", "model_removed", {"kind": "whisper", "model": "small"}) in app.calls


def test_scan_reports_states(made):
    dl = mm.ModelDownloads(make_app(), ops=FakeOps(on_disk={"base", "qwen_tiny"}))
    made.append(dl)
    rec = Recorder(dl)
    assert not dl.is_scanned()
    dl.scan()
    assert pump(lambda: rec.of("scanned"))
    assert dl.is_scanned()
    assert ("base", "ready") in rec.of("state_changed")
    assert dl.ready() == ["base", "qwen_tiny"]


def _snap(model="small", **kw):
    base = dict(cpu_name="Intel Core i7-1260P", cores=12, ram_gb=15.7, ram_display_gb=16,
                gpu_name="Intel Iris Xe Graphics", gpu_discrete=False, gpu_state="unsupported",
                platform="win32", recommended=hardware.Recommendation(model))
    base.update(kw)
    return hardware.HardwareSnapshot(**base)


def test_probe_hardware(made):
    app = make_app()
    dl = mm.ModelDownloads(app, ops=FakeOps())
    made.append(dl)
    rec = Recorder(dl)
    snap = _snap()
    seen = []
    with patch.object(hardware, "snapshot", side_effect=lambda cfg, **kw: seen.append((cfg, kw)) or snap):
        assert dl.probe_hardware() is None
        assert dl.probe_hardware() is None              # one probe at a time
        assert pump(lambda: rec.of("hardware_ready"))
    assert seen == [(app.cfg, {})]
    assert rec.of("hardware_ready") == [(snap,)]
    assert dl.hardware is snap
    assert dl.recommendation.model == "small"
    assert rec.threads() == {threading.get_ident()}
    # Cached: returned at once and announced again (a page built later hears it).
    assert dl.probe_hardware() is snap
    assert pump(lambda: len(rec.of("hardware_ready")) == 2)


def test_a_force_while_probing_probes_once_more(made):
    dl = mm.ModelDownloads(make_app(), ops=FakeOps())
    made.append(dl)
    rec = Recorder(dl)
    old, new = _snap(gpu_state="available"), _snap(model="large-v3-turbo", gpu_state="ready")
    gate = threading.Event()
    answers = [old, new]
    calls = []

    def fake(cfg, **kw):
        calls.append(1)
        if len(calls) == 1:
            assert gate.wait(WAIT)                 # the first probe is slow
        return answers[len(calls) - 1]

    with patch.object(hardware, "snapshot", side_effect=fake):
        assert dl.probe_hardware() is None
        assert dl.probe_hardware(force=True) is None   # e.g. the GPU download just ended
        gate.set()
        assert pump(lambda: len(rec.of("hardware_ready")) == 2)
    assert rec.of("hardware_ready") == [(old,), (new,)]
    assert dl.hardware is new and len(calls) == 2


def test_a_cached_answer_does_not_end_a_running_probe(made):
    dl = mm.ModelDownloads(make_app(), ops=FakeOps())
    made.append(dl)
    rec = Recorder(dl)
    first, second = _snap(), _snap(model="base")
    gate = threading.Event()
    calls = []

    def fake(cfg, **kw):
        calls.append(1)
        if len(calls) == 2:
            assert gate.wait(WAIT)
        return first if len(calls) == 1 else second

    with patch.object(hardware, "snapshot", side_effect=fake):
        dl.probe_hardware()
        assert pump(lambda: rec.of("hardware_ready"))
        assert dl.probe_hardware(force=True) is None      # a slow re-probe starts
        assert dl.probe_hardware() is first               # a page asks: the cached one
        assert pump(lambda: len(rec.of("hardware_ready")) == 2)
        assert dl.probe_hardware(force=True) is None      # still running: no third probe now
        assert pump(lambda: len(calls) == 2)
        time.sleep(0.05)
        assert len(calls) == 2
        gate.set()
        # ...but one more after it: 1st, cached, 2nd, then the re-probe.
        assert pump(lambda: len(rec.of("hardware_ready")) == 4)
    assert len(calls) == 3
    assert dl.hardware is second


def test_probe_hardware_falls_back_without_the_gpu(made):
    dl = mm.ModelDownloads(make_app(), ops=FakeOps())
    made.append(dl)
    rec = Recorder(dl)
    plain = _snap(gpu_name="", gpu_state=None)

    def fake(cfg, probe_gpu=True):
        if probe_gpu:
            raise RuntimeError("CIM failed")
        return plain

    with patch.object(hardware, "snapshot", side_effect=fake):
        dl.probe_hardware()
        assert pump(lambda: rec.of("hardware_ready"))
    assert dl.hardware is plain


def test_a_deleted_manager_does_not_break_its_worker(qapp):
    import shiboken6
    gate = threading.Event()
    ops = FakeOps(gate=gate)
    dl = mm.ModelDownloads(make_app(), ops=ops)
    core = dl.core
    dl.start("tiny")
    assert ops.started.wait(WAIT)
    errors = []
    old_hook = threading.excepthook
    threading.excepthook = lambda args: errors.append(args.exc_value)
    try:
        shiboken6.delete(dl)                           # e.g. the app is quitting
        gate.set()
        assert core.wait_ready("tiny", WAIT)
    finally:
        threading.excepthook = old_hook
    QApplication.processEvents()
    assert errors == []
