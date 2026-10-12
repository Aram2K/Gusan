"""CalendarService (calendar_service.py): its timers, worker hand-off and
signals, driven by a fake clock and a fake calendar API. Timer ticks are
fired by hand (timeout.emit), so nothing waits 20 s or 5 min."""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import calendar_service
import calendar_sync

T = datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)


def meeting(mid, start_min=0, length_min=60, link="https://meet.google.com/abc-defg-hij", title="Sync"):
    st = T + timedelta(minutes=start_min)
    return {"id": mid, "title": title, "start": st, "end": st + timedelta(minutes=length_min),
            "attendees": ["Aram", "Anna", "David"], "others": ["Anna", "David"], "link": link,
            "call_type": "Google Meet" if link else "", "recurring_id": ""}


class FakeApi:
    def __init__(self, meetings=(), configured=True, connected=True):
        self.meetings = list(meetings)
        self._configured = configured
        self._connected = connected
        self.fail = None                 # an exception upcoming() raises
        self.fetches = []                # (thread, now) per upcoming() call
        self.revoked = []
        self.connect_mode = "ok"         # "ok" | "error" | "block"

    def configured(self, cfg=None):
        return self._configured

    def is_connected(self):
        return self._connected

    def upcoming(self, cfg=None, now=None):
        self.fetches.append((threading.current_thread(), now))
        if self.fail is not None:
            raise self.fail
        return [dict(m) for m in self.meetings]

    def connect(self, cfg=None, cancel=None, open_browser=None):
        if self.connect_mode == "error":
            raise calendar_sync.CalendarError("Google sign-in was cancelled.")
        if self.connect_mode == "block":
            cancel.wait(5)
            raise calendar_sync.CalendarCancelled("Sign-in cancelled.")
        self._connected = True
        return "aram@aibuben.xyz"

    def forget_local(self):
        token, self._connected = ("rt" if self._connected else ""), False
        return token

    def revoke(self, token):
        self.revoked.append(token)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Deferred:
    """run_async that holds the worker jobs until run() - a late result."""

    def __init__(self):
        self.jobs = []

    def __call__(self, fn):
        self.jobs.append(fn)

    def run(self):
        while self.jobs:
            self.jobs.pop(0)()


def run_now(fn):
    fn()


class Threads:
    """run_async on real threads that the test can join before teardown, so
    no worker is still emitting while the service is deleted."""

    def __init__(self):
        self.threads = []

    def __call__(self, fn):
        t = threading.Thread(target=fn, daemon=True)
        self.threads.append(t)
        t.start()

    def join(self):
        for t in self.threads:
            t.join(5)
        return not any(t.is_alive() for t in self.threads)


@pytest.fixture
def make(qapp, tmp_path):
    made = []

    def build(api=None, now=T - timedelta(minutes=2), cfg=None, busy=None, run_async=run_now, **kw):
        api = api if api is not None else FakeApi([meeting("a"), meeting("b", 90)])
        clock = Clock(now)
        svc = calendar_service.CalendarService(
            cfg if cfg is not None else {}, is_busy=busy, api=api, clock=clock,
            state_path=tmp_path / "calendar_state.json", run_async=run_async, **kw)
        log = {name: [] for name in ("changed", "connected", "disconnected", "error", "remind", "connecting")}
        for name, bucket in log.items():
            getattr(svc, name).connect(bucket.append)
        made.append(svc)
        return svc, api, clock, log

    yield build
    for svc in made:
        svc.stop()
        svc.deleteLater()
    qapp.processEvents()


def tick(svc):
    svc._remind_timer.timeout.emit()


def wait_for(pred, timeout=5.0):
    from PySide6.QtTest import QTest
    end = time.monotonic() + timeout
    while not pred() and time.monotonic() < end:
        QTest.qWait(10)
    return pred()


def test_start_runs_both_timers_and_fetches(make):
    svc, api, _, log = make()
    assert not svc._remind_timer.isActive()
    svc.start()
    assert svc._fetch_timer.isActive() and svc._fetch_timer.interval() == 5 * 60 * 1000
    assert svc._remind_timer.isActive() and svc._remind_timer.interval() == 20 * 1000
    assert len(api.fetches) == 1
    assert [m["id"] for m in log["changed"][-1]] == ["a", "b"]
    assert log["changed"][-1][0]["people"] == "Anna, David"
    assert log["remind"] == []                      # 2 min before: not yet
    svc._fetch_timer.timeout.emit()                 # the 5-minute tick refetches
    assert len(api.fetches) == 2
    assert svc.refresh() is False                   # page shown again within a minute
    assert svc.refresh(force=True) is True


def test_reminder_window_with_a_fake_clock(make, tmp_path):
    svc, _, clock, log = make()
    svc.start()
    clock.now = T - timedelta(seconds=61)
    tick(svc)
    assert log["remind"] == []
    clock.now = T - timedelta(seconds=60)
    tick(svc)
    assert [m["id"] for m in log["remind"]] == ["a"]
    clock.now = T + timedelta(seconds=30)
    tick(svc)
    assert len(log["remind"]) == 1                  # once per meeting
    assert "a" in calendar_service.CalendarState(tmp_path / "calendar_state.json").prompted
    # A restart inside the window doesn't ask again.
    again, _, clock2, log2 = make(now=T + timedelta(seconds=40))
    again.start()
    tick(again)
    assert log2["remind"] == []


def test_no_reminder_while_busy(make):
    busy = {"on": True}
    svc, _, clock, log = make(busy=lambda: busy["on"])
    svc.start()
    clock.now = T
    tick(svc)
    assert log["remind"] == []
    busy["on"] = False
    tick(svc)
    assert [m["id"] for m in log["remind"]] == ["a"]


def test_muted_meeting_is_not_asked_and_the_list_updates(make):
    svc, _, clock, log = make()
    svc.start()
    n = len(log["changed"])
    assert svc.set_muted("a", True) is True
    assert len(log["changed"]) == n + 1 and log["changed"][-1][0]["muted"] is True
    assert svc.set_muted("a", True) is False
    clock.now = T
    tick(svc)
    assert log["remind"] == []
    assert svc.is_muted("a")


def test_ask_to_record_off_means_no_reminders(make):
    cfg = {calendar_service.ASK_KEY: False}
    svc, _, clock, log = make(cfg=cfg)
    svc.start()
    clock.now = T
    tick(svc)
    assert log["remind"] == []


def test_a_row_that_ends_is_re_emitted_once(make):
    svc, _, clock, log = make()
    svc.start()
    n = len(log["changed"])
    tick(svc)
    assert len(log["changed"]) == n                 # nothing changed
    clock.now = T + timedelta(minutes=61)           # "a" ended
    tick(svc)
    assert [m["id"] for m in log["changed"][-1]] == ["b"]
    tick(svc)
    assert len(log["changed"]) == n + 1


def test_link_filter_setting_re_emits(make):
    cfg = {}
    api = FakeApi([meeting("call", 10), meeting("lunch", 20, link="")])
    svc, _, _, log = make(api=api, cfg=cfg)
    svc.start()
    assert [m["id"] for m in log["changed"][-1]] == ["call"]
    cfg[calendar_service.LINK_ONLY_KEY] = False
    svc.on_setting_changed(calendar_service.LINK_ONLY_KEY)
    assert [m["id"] for m in log["changed"][-1]] == ["call", "lunch"]
    assert svc.next_meeting()["id"] == "call"


def test_fetch_errors_keep_the_list_and_report_once(make):
    svc, api, _, log = make()
    svc.start()
    api.fail = calendar_sync.CalendarError("Couldn't reach Google Calendar.")
    svc._fetch_timer.timeout.emit()
    svc._fetch_timer.timeout.emit()
    assert log["error"] == ["Couldn't reach Google Calendar."]
    assert [m["id"] for m in svc.meetings()] == ["a", "b"]
    assert svc.is_connected
    api.fail = None
    svc._fetch_timer.timeout.emit()
    assert svc.last_error == ""


def test_expired_access_disconnects(make):
    svc, api, _, log = make()
    svc.start()
    api.fail = calendar_sync.CalendarAuthError("Calendar access expired")
    api._connected = False                          # invalid_grant wipes the keyring first
    svc._fetch_timer.timeout.emit()
    assert log["disconnected"] == ["expired"]
    assert log["changed"][-1] == []
    assert not svc.is_connected and svc.meetings() == []
    assert not svc._fetch_timer.isActive() and not svc._remind_timer.isActive()
    assert api.revoked == []                        # nothing left to give back


def test_a_keyring_hiccup_is_not_an_expiry(make):
    # The keyring answered "" once (CalendarAuthError "isn't connected"), but
    # the token is still there: keep the connection and the list, don't revoke.
    svc, api, _, log = make()
    svc.start()
    api.fail = calendar_sync.CalendarAuthError("Calendar isn't connected.")
    svc._fetch_timer.timeout.emit()
    svc._fetch_timer.timeout.emit()
    assert log["disconnected"] == [] and api.revoked == []
    assert svc.is_connected and [m["id"] for m in svc.meetings()] == ["a", "b"]
    assert log["error"] == [calendar_service.FETCH_FAILED_TEXT]
    assert api.is_connected()


def test_connect_flow(make):
    api = FakeApi([meeting("a")], connected=False)
    svc, _, _, log = make(api=api)
    svc.start()
    assert not svc._remind_timer.isActive() and api.fetches == []
    assert svc.connect_calendar() is True
    assert log["connecting"] == [True, False]
    assert log["connected"] == ["aram@aibuben.xyz"]
    assert svc.email == "aram@aibuben.xyz" and svc.is_connected
    assert svc._remind_timer.isActive() and len(api.fetches) == 1
    assert [m["id"] for m in log["changed"][-1]] == ["a"]


def test_connect_error_and_not_set_up(make):
    api = FakeApi(connected=False)
    api.connect_mode = "error"
    svc, _, _, log = make(api=api)
    assert svc.connect_calendar() is True
    assert log["error"] == ["Google sign-in was cancelled."] and log["connected"] == []
    off, _, _, log2 = make(api=FakeApi(configured=False))
    assert off.available is False
    assert off.connect_calendar() is False
    assert log2["error"] == [calendar_service.NOT_SET_UP_TEXT]


def test_network_gate_pauses_fetch_and_connect(make):
    allowed = {"on": False}
    svc, api, _, log = make(network_ok=lambda: allowed["on"])
    svc.start()
    assert api.fetches == []
    allowed["on"] = True
    svc.on_setting_changed("privacy_mode")
    assert len(api.fetches) == 1
    off, _, _, log2 = make(api=FakeApi(connected=False), network_ok=lambda: False)
    assert off.connect_calendar() is False
    assert log2["error"] == [calendar_service.PAUSED_TEXT]


def test_cancel_a_waiting_sign_in_on_a_real_thread(make):
    api = FakeApi(connected=False)
    api.connect_mode = "block"
    threads = Threads()
    svc, _, _, log = make(api=api, run_async=threads)
    assert svc.connect_calendar() is True
    assert svc.is_connecting and svc.connect_calendar() is False      # one at a time
    assert svc.cancel_connect() is True
    assert log["connecting"] == [True, False] and not svc.is_connecting
    assert threads.join()                                              # the worker saw the cancel
    wait_for(lambda: False, 0.1)                                       # deliver its (stale) result
    assert log["connected"] == [] and log["error"] == []


def test_a_sign_in_that_finishes_after_cancel_keeps_nothing(make):
    jobs = Deferred()
    api = FakeApi(connected=False)
    svc, _, _, log = make(api=api, run_async=jobs)
    svc.set_muted("x", True)
    assert svc.connect_calendar() is True
    svc.cancel_connect()
    jobs.run()                                      # Google answered anyway; then the revoke
    assert log["connected"] == [] and not svc.is_connected
    assert api.revoked == ["rt"] and not api.is_connected()
    assert svc.is_muted("x")                        # bells untouched


def test_worker_results_arrive_on_the_gui_thread(make):
    threads = Threads()
    svc, api, _, log = make(run_async=threads)
    seen = []
    svc.changed.connect(lambda rows: seen.append(threading.current_thread()))
    svc.start()
    assert wait_for(lambda: bool(seen))
    assert threads.join()
    assert api.fetches[0][0] is not threading.main_thread()
    assert seen[0] is threading.main_thread()


def test_disconnect_revokes_and_ignores_a_late_fetch(make):
    jobs = Deferred()
    svc, api, _, log = make(run_async=jobs)
    svc.start()                                     # fetch queued, not run
    svc.disconnect_calendar()
    assert log["disconnected"] == ["user"] and log["changed"] == [[]]
    jobs.run()                                      # the late fetch + the revoke
    assert api.revoked == ["rt"]
    assert log["changed"] == [[]] and svc.meetings() == []
    assert not svc.is_connected and svc.email == ""


def test_a_different_account_disconnects_the_calendar(make):
    svc, _, _, log = make()
    svc.start()
    svc.set_owner("user-1")                         # adopted
    svc.set_owner(None)                             # signed out: kept
    assert log["disconnected"] == []
    svc.set_owner("user-2")
    assert log["disconnected"] == ["account"]


def test_stop_halts_timers(make):
    svc, _, _, _ = make()
    svc.start()
    svc.stop()
    assert not svc._fetch_timer.isActive() and not svc._remind_timer.isActive()


def test_current_meeting_for_the_start_card(make):
    svc, _, clock, _ = make()
    svc.start()
    clock.now = T - timedelta(minutes=3)
    assert svc.current_meeting()["id"] == "a"
    clock.now = T + timedelta(hours=3)
    assert svc.current_meeting() is None
