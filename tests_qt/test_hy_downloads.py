"""app.downloads with the Armenian speech model (model_manager.ModelDownloads): its own kind, a download
that can be stopped, the catalog lookup on a worker with its signal on the GUI thread, and Privacy mode
read from the app's config."""
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

import model_manager as mm
import speech_hy

HY = speech_hy.MODEL_ID
WAIT = 5.0


class Ops:
    def __init__(self, gate=None, change=True):
        self.on_disk = set()
        self.gate = gate
        self.change = change
        self.started = threading.Event()
        self.calls = []
        self.cancel_seen = []

    def normalize(self, name):
        return name

    def downloaded(self, kind, name):
        return name in self.on_disk

    def download(self, kind, name, on_progress, should_cancel=None):
        self.calls.append((kind, name))
        self.started.set()
        on_progress(10, 10, 100)
        if self.gate is not None:
            while not self.gate.wait(0.01):
                if should_cancel is not None and should_cancel():
                    self.cancel_seen.append(True)
                    raise speech_hy.Cancelled()
        on_progress(100, 100, 100)
        self.on_disk.add(name)

    def remove(self, kind, name):
        self.on_disk.discard(name)

    def refresh_catalog(self, force=False):
        self.calls.append(("catalog", force, threading.get_ident()))
        return self.change


def pump(cond, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return cond()


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


def downloads(made, ops, cfg=None):
    app = SimpleNamespace(cfg=cfg if cfg is not None else {}, recorder=SimpleNamespace(unload_model=lambda n: None),
                          track=lambda e, p=None: None)
    dl = mm.ModelDownloads(app, ops=ops)
    made.append(dl)
    return dl


def test_the_armenian_model_is_a_kind_of_its_own(made):
    dl = downloads(made, Ops())
    assert dl.core.kind(HY) == "speech_hy"
    assert dl.core.kind("small") == "whisper"
    assert HY in mm.ALL_IDS and HY not in mm.SPEECH_IDS
    assert dl.ready("whisper") == []


def test_it_downloads_with_progress_on_the_gui_thread(made):
    ops = Ops()
    dl = downloads(made, ops)
    got = []
    dl.finished.connect(lambda *a: got.append((a, threading.get_ident())))
    assert dl.start(HY)
    assert pump(lambda: got)
    assert got[0][0] == (HY, "ready", "")
    assert got[0][1] == threading.get_ident()
    assert ops.calls == [("speech_hy", HY)]
    assert dl.ready("speech_hy") == [HY]


def test_cancel_stops_it_and_the_card_offers_download_again(made):
    gate = threading.Event()
    ops = Ops(gate=gate)
    dl = downloads(made, ops)
    got = []
    dl.finished.connect(lambda *a: got.append(a))
    dl.start(HY)
    assert ops.started.wait(WAIT)
    assert dl.cancel(HY) is True
    assert pump(lambda: got)
    assert got[0] == (HY, "missing", "")                  # not a failure: no error sentence
    assert ops.cancel_seen == [True]
    assert dl.error(HY) == ""
    assert dl.cancel("small") is False                    # only this download can be stopped
    assert dl.cancel(HY) is False                         # nothing is running now


def test_the_catalog_is_looked_up_on_a_worker_and_the_signal_comes_back_here(made):
    ops = Ops(change=True)
    dl = downloads(made, ops)
    seen = []
    dl.catalog_changed.connect(lambda: seen.append(threading.get_ident()))
    dl.refresh_catalog(force=True)
    assert pump(lambda: seen)
    assert seen == [threading.get_ident()]
    kind, force, worker = ops.calls[0]
    assert (kind, force) == ("catalog", True)
    assert worker != threading.get_ident()                # never on the GUI thread (it may take seconds)
    quiet = Ops(change=False)
    dl2 = downloads(made, quiet)
    heard = []
    dl2.catalog_changed.connect(lambda: heard.append(1))
    dl2.refresh_catalog()
    assert pump(lambda: quiet.calls)
    QApplication.processEvents()
    assert heard == []                                    # nothing changed: nobody is told


def test_privacy_mode_is_read_from_the_apps_config(made):
    cfg = {"privacy_mode": False}
    app = SimpleNamespace(cfg=cfg, recorder=SimpleNamespace(unload_model=lambda n: None), track=lambda e, p=None: None)
    dl = mm.ModelDownloads(app)
    made.append(dl)
    assert dl.core._ops.privacy_on() is False
    cfg["privacy_mode"] = True
    assert dl.core._ops.privacy_on() is True
