"""api_keys.run_then: work on a worker thread, the answer back on the GUI thread
(what the Speech & AI key tests, delete account, feedback and "Check now" use)."""
import threading
import time

import api_keys


def _wait(qapp, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    return cond()


def test_result_arrives_on_the_gui_thread(qapp):
    gui = threading.get_ident()
    seen = {}

    def work(a, b):
        seen["worker"] = threading.get_ident()
        return a * b

    def then(result):
        seen["result"] = result
        seen["then"] = threading.get_ident()

    api_keys.run_then(work, 6, 7, then=then)
    assert _wait(qapp, lambda: "result" in seen)
    assert seen["result"] == 42
    assert seen["worker"] != gui
    assert seen["then"] == gui
    # One long-lived relay on the GUI thread (no QObject made or deleted per call).
    relay = api_keys._relay()
    assert relay is api_keys._relay()
    assert relay.thread() is qapp.thread()


def test_a_failing_callback_does_not_stop_later_ones(qapp):
    seen = []

    def bad(_result):
        raise RuntimeError("page bug")
    api_keys.run_then(lambda: 1, then=bad)
    api_keys.run_then(lambda: 2, then=seen.append)
    assert _wait(qapp, lambda: seen == [2])


def test_first_use_from_a_worker_still_delivers_on_the_gui_thread(qapp, monkeypatch):
    monkeypatch.setattr(api_keys, "_RELAY", None)
    gui = threading.get_ident()
    seen = {}
    t = threading.Thread(target=lambda: api_keys.run_then(
        lambda: "ok", then=lambda r: seen.update(result=r, thread=threading.get_ident())))
    t.start()
    t.join(5)
    assert _wait(qapp, lambda: "result" in seen)
    assert (seen["result"], seen["thread"]) == ("ok", gui)


def test_errors_arrive_on_the_gui_thread(qapp):
    gui = threading.get_ident()
    seen = {}

    def boom():
        raise ValueError("no network")

    api_keys.run_then(boom, then=lambda r: seen.setdefault("then", r),
                      error=lambda e: seen.update(error=e, thread=threading.get_ident()))
    assert _wait(qapp, lambda: "error" in seen)
    assert isinstance(seen["error"], ValueError)
    assert seen["thread"] == gui
    assert "then" not in seen


def test_key_test_through_run_then(qapp):
    """The page pattern: a key test with a fake HTTP call, answer on the GUI thread."""
    class Resp:
        status_code = 200
        text = ""
    seen = []
    api_keys.run_then(api_keys.test_gemini, "AIza-x", get=lambda url, **kw: Resp(), then=seen.append)
    assert _wait(qapp, lambda: bool(seen))
    assert seen == [(True, "Works")]
