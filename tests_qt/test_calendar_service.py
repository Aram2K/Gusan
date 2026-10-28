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


def meeting(mid, start_min=0, length_min=60, link="https://meet.google.com/abc-defg-hij", title="Sync",
            others=("Anna", "David")):
    st = T + timedelta(minutes=start_min)
    return {"id": mid, "title": title, "start": st, "end": st + timedelta(minutes=length_min),
            "attendees": ["Aram"] + list(others), "others": list(others), "link": link,
            "call_type": "Google Meet" if link else "", "recurring_id": "",
            "agenda": "", "has_others": bool(others), "private": False}


class FakeApi:
    def __init__(self, meetings=(), configured=True, connected=True):
        self.meetings = list(meetings)
        self._configured = configured
        self._connected = connected
        self.fail = None                 # an exception upcoming() raises
        self.fetches = []                # (thread, now) per upcoming() call
        self.revoked = []
        self.connect_mode = "ok"         # "ok" | "error" | "block"
        self.connect_code = None         # the code of the CalendarError connect() raises

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
            raise calendar_sync.CalendarError("Google sign-in was cancelled.", self.connect_code)
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
        log = {name: [] for name in ("changed", "connected", "disconnected", "error", "remind", "connecting",
                                     "status", "connect_result")}
        for name, bucket in log.items():
            if name != "connect_result":
                getattr(svc, name).connect(bucket.append)
        svc.connect_result.connect(lambda ok, reason: log["connect_result"].append((ok, reason)))
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
    svc, api, _, log = make(rand=lambda: 0.5)         # no jitter: exactly the fast interval
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


def test_a_row_that_ends_is_re_emitted_once(make, monkeypatch):
    monkeypatch.setattr(calendar_service, "FETCH_STALE_S", 10 ** 6)     # a 61 min jump isn't a stale cache here
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


def test_other_people_setting_re_emits(make):
    cfg = {}
    api = FakeApi([meeting("call", 10), meeting("lunch", 20, others=())])
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


# ── polling: single-shot timer, backoff, pre-meeting read, stale cache, status ──
def _fast(**kw):
    return dict(rand=lambda: 0.5, **kw)               # jitter factor exactly 1


def test_fetch_timer_single_shot_rescheduled(make):
    svc, api, _, _ = make(**_fast())
    assert svc._fetch_timer.isSingleShot()
    svc.start()
    assert svc._fetch_timer.isActive() and svc._fetch_timer.interval() == 300_000
    svc._fetch_timer.stop()                           # as after a real timeout of a single-shot timer
    assert not svc._fetch_timer.isActive()
    svc._fetch_timer.timeout.emit()
    assert len(api.fetches) == 2
    assert svc._fetch_timer.isActive() and svc._fetch_timer.interval() == 300_000    # re-armed by the result


def test_fetch_interval_follows_the_day(make):
    api = FakeApi([meeting("far", 400)])                # nothing within 2 h: a quiet day
    svc, api, clock, _ = make(api=api, **_fast())
    svc.start()
    assert svc._fetch_timer.interval() == 900_000
    api.meetings = [meeting("far", 400), meeting("soon", 60)]
    svc._fetch_timer.timeout.emit()
    assert svc._fetch_timer.interval() == 300_000
    api.meetings = []                                   # nothing at all counts as busy
    svc._fetch_timer.timeout.emit()
    assert svc._fetch_timer.interval() == 300_000


def test_fetch_interval_has_jitter(make):
    seen = set()
    for luck in (0.0, 1.0):
        svc, _, _, _ = make(rand=lambda luck=luck: luck)
        svc.start()
        seen.add(svc._fetch_timer.interval())
    assert seen == {225_000, 375_000}
    svc, _, _, _ = make()                               # the real random source
    svc.start()
    assert 225_000 <= svc._fetch_timer.interval() <= 375_000


def test_backoff_after_errors_resets_on_success(make):
    svc, api, clock, log = make(**_fast())
    svc.start()
    api.fail = calendar_sync.CalendarError("Google Calendar isn't answering. Trying again soon.")
    steps = []
    for _ in range(5):
        svc._fetch_timer.timeout.emit()
        steps.append(svc._fetch_timer.interval())
    assert steps == [600_000, 1_200_000, 1_800_000, 1_800_000, 1_800_000]       # doubled, then the cap
    assert log["error"] == ["Google Calendar isn't answering. Trying again soon."]   # said once per streak
    assert [m["id"] for m in svc.meetings()] == ["a", "b"]                      # the list is kept
    api.fail = None
    svc._fetch_timer.timeout.emit()
    assert svc._fetch_timer.interval() == 300_000                               # a success resets it
    assert svc.last_error == ""
    api.fail = calendar_sync.CalendarError("x")
    svc._fetch_timer.timeout.emit()
    assert svc._fetch_timer.interval() == 600_000                               # counting from zero again


def test_failing_reads_are_not_hammered_by_the_stale_check(make):
    svc, api, clock, _ = make(**_fast())
    svc.start()
    api.fail = calendar_sync.CalendarError("offline")
    svc._fetch_timer.timeout.emit()
    n = len(api.fetches)
    for _ in range(5):
        clock.now += timedelta(minutes=30)              # long past the stale limit
        tick(svc)
    assert len(api.fetches) == n                        # the backoff decides, not the 20 s tick


def test_a_failed_read_still_rate_limits_the_page_refresh(make):
    svc, api, clock, _ = make(**_fast())
    svc.start()
    api.fail = calendar_sync.CalendarError("offline")
    svc._fetch_timer.timeout.emit()
    n = len(api.fetches)
    clock.now += timedelta(seconds=30)
    assert svc.refresh() is False and len(api.fetches) == n        # the page shown again within a minute
    clock.now += timedelta(seconds=40)
    assert svc.refresh() is True and len(api.fetches) == n + 1


def test_pre_meeting_refresh_is_scheduled(make):
    api = FakeApi([meeting("m", 10)])
    svc, api, clock, log = make(api=api, now=T + timedelta(minutes=3), **_fast())
    svc.start()
    # Starts in 7 min: read again 150 s before the start (in 270 s), not at the 300 s tick.
    assert svc._fetch_timer.interval() == 270_000
    # The meeting is cancelled meanwhile; the read just before it catches that.
    api.meetings = []
    clock.now = T + timedelta(minutes=7, seconds=30)
    svc._fetch_timer.timeout.emit()
    assert svc.meetings() == [] and log["changed"][-1] == []
    assert svc._fetch_timer.interval() == 300_000       # nothing left to catch: the normal tick
    clock.now = T + timedelta(minutes=10)
    tick(svc)
    assert log["remind"] == []                          # no toast for a meeting that isn't there


def test_a_moved_meeting_is_asked_at_its_new_time(make):
    api = FakeApi([meeting("m", 10)])
    svc, api, clock, log = make(api=api, now=T + timedelta(minutes=3), **_fast())
    svc.start()
    api.meetings = [meeting("m", 40)]                   # moved 30 min later
    clock.now = T + timedelta(minutes=7, seconds=30)
    svc._fetch_timer.timeout.emit()
    clock.now = T + timedelta(minutes=10)
    tick(svc)
    assert log["remind"] == []
    clock.now = T + timedelta(minutes=39)
    tick(svc)
    assert [m["id"] for m in log["remind"]] == ["m"]


def test_stale_cache_refreshes_on_tick(make):
    svc, api, clock, log = make(**_fast())
    svc.start()
    assert len(api.fetches) == 1
    clock.now += timedelta(minutes=10)
    tick(svc)
    assert len(api.fetches) == 1                        # fresh enough
    clock.now += timedelta(minutes=11)                  # 21 min without a good read (sleep)
    tick(svc)
    assert len(api.fetches) == 2
    tick(svc)
    assert len(api.fetches) == 2                        # one read, not one per tick
    assert svc._fetch_timer.isActive()


def test_stale_cache_waits_while_paused_or_disconnected(make):
    allowed = {"on": True}
    svc, api, clock, _ = make(network_ok=lambda: allowed["on"], **_fast())
    svc.start()
    allowed["on"] = False
    clock.now += timedelta(hours=3)
    tick(svc)
    assert len(api.fetches) == 1                        # Privacy mode: no request
    allowed["on"] = True
    off, off_api, off_clock, _ = make(api=FakeApi(connected=False), **_fast())
    off.start()
    off_clock.now += timedelta(hours=3)
    tick(off)
    assert off_api.fetches == []


def test_status_signal_privacy_and_error(make):
    allowed = {"on": True}
    svc, api, _, log = make(network_ok=lambda: allowed["on"], **_fast())
    svc.start()
    assert svc.status_text() == "" and log["status"] == []
    allowed["on"] = False
    svc.on_setting_changed("privacy_mode")
    assert svc.status_text() == "Paused in Privacy mode."
    assert log["status"] == ["Paused in Privacy mode."]
    svc.on_setting_changed("privacy_mode")
    assert log["status"] == ["Paused in Privacy mode."]            # emitted when it changes only
    allowed["on"] = True
    api.fail = calendar_sync.CalendarError("Google is busy. Trying again soon.")
    svc.on_setting_changed("privacy_mode")                          # back on: reads again, and it fails
    assert svc.status_text() == "Couldn't read Google Calendar. Trying again."
    assert log["status"][-1] == "Couldn't read Google Calendar. Trying again."
    api.fail = None
    svc._fetch_timer.timeout.emit()
    assert svc.status_text() == "" and log["status"][-1] == ""      # cleared by the next good read
    assert svc.meetings()
    svc.disconnect_calendar()
    assert svc.status_text() == ""


def test_status_is_empty_when_nothing_is_connected(make):
    svc, _, _, log = make(api=FakeApi(connected=False), network_ok=lambda: False)
    svc.start()
    assert svc.status_text() == "" and log["status"] == []


def test_disconnect_clears_the_error_status(make):
    svc, api, _, log = make(**_fast())
    svc.start()
    api.fail = calendar_sync.CalendarError("offline")
    svc._fetch_timer.timeout.emit()
    assert log["status"][-1] == "Couldn't read Google Calendar. Trying again."
    svc.disconnect_calendar()
    assert log["status"][-1] == ""
    assert svc._failures == 0


def test_paused_ticks_keep_the_timer_alive(make):
    allowed = {"on": False}
    svc, api, _, _ = make(network_ok=lambda: allowed["on"], **_fast())
    svc.start()
    assert svc._fetch_timer.isActive()
    svc._fetch_timer.stop()                              # the single-shot timer fired ...
    svc._fetch_timer.timeout.emit()                      # ... and paused reads did nothing
    assert api.fetches == [] and svc._fetch_timer.isActive()    # still ticking, so Privacy mode off resumes


def test_now_or_next_on_the_service(make):
    svc, _, clock, _ = make(**_fast())
    svc.start()
    assert svc.now_or_next()[0] == "next" and svc.now_or_next()[1]["id"] == "a"
    clock.now = T + timedelta(minutes=1)
    kind, m = svc.now_or_next()
    assert (kind, m["id"]) == ("now", "a")


def test_the_page_open_refresh_is_still_once_a_minute(make):
    svc, api, clock, _ = make(**_fast())
    svc.start()
    assert svc.refresh() is False
    clock.now += timedelta(seconds=61)
    assert svc.refresh() is True


# ── connect_result: the reason a Connect ended ──────────────────────────────────
def test_connect_result_ok_and_reasons(make):
    svc, api, _, log = make(api=FakeApi(connected=False))
    assert svc.connect_calendar() is True
    assert log["connect_result"] == [(True, "")] and svc.last_connect_reason == ""
    api2 = FakeApi(connected=False)
    api2.connect_mode = "error"
    for code, want in (("admin", "admin"), ("denied", "denied"), ("timeout", "timeout"), ("keyring", "keyring"),
                       ("offline", "offline"), ("scope", "scope"), ("port_busy", "port_busy"),
                       ("client", "client"), ("something_new", "other"), (None, "other")):
        api2.connect_code = code
        svc2, _, _, log2 = make(api=api2)
        svc2.connect_calendar()
        assert log2["connect_result"] == [(False, want)], code
        assert svc2.last_connect_reason == want
        assert log2["error"] == ["Google sign-in was cancelled."]


def test_connect_result_for_cancel_and_early_failures(make):
    api = FakeApi(connected=False)
    api.connect_mode = "block"
    threads = Threads()
    svc, _, _, log = make(api=api, run_async=threads)
    svc.connect_calendar()
    svc.cancel_connect()
    assert log["connect_result"] == [(False, "cancelled")]
    assert threads.join()
    wait_for(lambda: False, 0.1)
    assert log["connect_result"] == [(False, "cancelled")]             # the stale answer adds nothing
    off, _, _, off_log = make(api=FakeApi(configured=False))
    off.connect_calendar()
    assert off_log["connect_result"] == [(False, "client")]
    paused, _, _, paused_log = make(api=FakeApi(connected=False), network_ok=lambda: False)
    paused.connect_calendar()
    assert paused_log["connect_result"] == [(False, "other")]


def test_quitting_or_disconnecting_reports_no_connect_result(make):
    api = FakeApi(connected=False)
    api.connect_mode = "block"
    threads = Threads()
    svc, _, _, log = make(api=api, run_async=threads)
    svc.connect_calendar()
    svc.stop()
    assert threads.join()
    assert log["connect_result"] == []
    svc2, _, _, log2 = make(api=api, run_async=threads)
    svc2.connect_calendar()
    svc2.disconnect_calendar()
    assert log2["connect_result"] == [] and log2["disconnected"] == ["user"]
    assert threads.join()


def test_a_connect_resets_the_failure_streak(make):
    svc, api, _, log = make(api=FakeApi([meeting("a")]), **_fast())
    svc.start()
    api.fail = calendar_sync.CalendarError("offline")
    svc._fetch_timer.timeout.emit()
    assert svc._failures == 1
    svc.disconnect_calendar()
    api._connected = False
    api.fail = None
    svc.connect_calendar()
    assert svc._failures == 0 and svc.status_text() == ""


# ── after a long sleep: no reminder from a stale list ───────────────────────────
def _asleep(make, **kw):
    """A service whose only meeting starts at T, last read 80 min before it,
    with the reads held (Deferred) so a test decides when the answer lands."""
    d = Deferred()
    svc, api, clock, log = make(api=FakeApi([meeting("m", 0)]), now=T - timedelta(minutes=80),
                                run_async=d, **_fast(**kw))
    svc.start()
    d.run()                                              # the first good read: "m" at T
    assert log["remind"] == [] and [m["id"] for m in svc.meetings()] == ["m"]
    clock.now = T - timedelta(minutes=1)                 # the lid opens: a stale list, "m" due in 1 min
    return svc, api, clock, log, d


def test_a_cancelled_meeting_is_not_reminded_from_a_stale_list(make):
    svc, api, clock, log, d = _asleep(make)
    api.meetings = []                                    # cancelled while the computer slept
    tick(svc)
    assert log["remind"] == [] and len(d.jobs) == 1      # held, and a read is on its way
    assert "m" not in svc.core.state.prompted            # and not marked asked
    d.run()
    assert log["remind"] == [] and svc.meetings() == []
    tick(svc)
    assert log["remind"] == []


def test_a_moved_meeting_is_still_asked_at_its_new_time_after_sleep(make):
    svc, api, clock, log, d = _asleep(make)
    api.meetings = [meeting("m", 30)]                    # moved to 16:00 meanwhile
    tick(svc)
    assert log["remind"] == [] and "m" not in svc.core.state.prompted
    d.run()
    assert log["remind"] == []                           # the fresh list says: not yet
    clock.now = T + timedelta(minutes=25)
    svc._fetch_timer.timeout.emit()                      # the normal read before the new start
    d.run()
    clock.now = T + timedelta(minutes=29)
    tick(svc)
    assert [m["id"] for m in log["remind"]] == ["m"]     # the reminder at the new time comes


def test_the_fresh_list_decides_the_reminder_when_the_meeting_is_still_on(make):
    svc, api, clock, log, d = _asleep(make)
    tick(svc)
    assert log["remind"] == []                           # held until the read is back...
    d.run()
    assert [m["id"] for m in log["remind"]] == ["m"]     # ...then asked, from _on_fetched


def test_a_failed_read_falls_back_to_the_stale_list(make):
    svc, api, clock, log, d = _asleep(make)
    api.fail = calendar_sync.CalendarError("offline")
    tick(svc)
    assert log["remind"] == []
    d.run()                                              # the read failed: nothing fresh
    assert log["remind"] == []
    tick(svc)                                            # the next tick uses the cache: nothing is lost
    assert [m["id"] for m in log["remind"]] == ["m"]


def test_a_fresh_list_reminds_at_once(make):
    d = Deferred()
    svc, api, clock, log = make(api=FakeApi([meeting("m", 0)]), now=T - timedelta(minutes=5),
                                run_async=d, **_fast())
    svc.start()
    d.run()
    clock.now = T - timedelta(minutes=1)                 # 4 min since the read: nothing to wait for
    tick(svc)
    assert [m["id"] for m in log["remind"]] == ["m"] and d.jobs == []


# ── "No meetings coming up" only after a read ───────────────────────────────────
def test_has_read_follows_the_first_good_read(make):
    allowed = {"on": False}
    svc, api, _, log = make(network_ok=lambda: allowed["on"], **_fast())
    assert svc.has_read is False
    svc.start()                                          # Privacy mode: connected, but nothing read
    assert api.fetches == [] and svc.has_read is False
    allowed["on"] = True
    svc.on_setting_changed("privacy_mode")
    assert svc.has_read is True
    api.fail = calendar_sync.CalendarError("offline")
    svc._fetch_timer.timeout.emit()
    assert svc.has_read is True                          # an earlier good read still stands
    svc.disconnect_calendar()
    assert svc.has_read is False                         # a reconnect starts unread again


def test_a_failed_first_read_is_not_a_read(make):
    api = FakeApi([meeting("a")])
    api.fail = calendar_sync.CalendarError("offline")
    svc, api, _, _ = make(api=api, **_fast())
    svc.start()
    assert svc.has_read is False


# ── a Disconnect that Google never heard is finished later ──────────────────────
class RevokeApi(FakeApi):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.revoke_ok = False           # revoke(): False = Google couldn't be reached
        self.retry_ok = True
        self.retries = 0

    def revoke(self, token):
        self.revoked.append(token)
        return self.revoke_ok

    def retry_pending_revoke(self):
        self.retries += 1
        return self.retry_ok


def test_an_unconfirmed_revoke_is_retried_after_the_next_good_read(make):
    svc, api, _, _ = make(api=RevokeApi([meeting("a")]), **_fast())
    svc.start()
    assert api.retries == 1                              # the start-up check (nothing pending: free)
    svc._fetch_timer.timeout.emit()
    assert api.retries == 1                              # nothing failed since: no more checks
    svc.disconnect_calendar()
    assert api.revoked == ["rt"]                         # Google was offline: revoke said False
    svc.connect_calendar()                               # back online: connected, first read
    assert api.retries == 2                              # told to Google now
    svc._fetch_timer.timeout.emit()
    assert api.retries == 2                              # confirmed: left alone


def test_a_retry_that_fails_waits_for_the_next_read(make):
    api = RevokeApi([meeting("a")])
    api.retry_ok = False
    svc, api, _, _ = make(api=api, **_fast())
    svc.start()
    n = api.retries
    assert n >= 1                                        # tried at start, and failed
    svc._fetch_timer.timeout.emit()
    assert api.retries == n + 1                          # still pending: tried again after the read
    api.retry_ok = True
    svc._fetch_timer.timeout.emit()
    assert api.retries == n + 2
    svc._fetch_timer.timeout.emit()
    assert api.retries == n + 2                          # done: left alone


def test_the_revoke_retry_waits_for_privacy_mode_to_end(make):
    allowed = {"on": False}
    svc, api, _, _ = make(api=RevokeApi([meeting("a")]), network_ok=lambda: allowed["on"], **_fast())
    svc.start()
    assert api.retries == 0                              # reads are paused: no request
    allowed["on"] = True
    svc.on_setting_changed("privacy_mode")
    assert api.retries == 1


def test_a_calendar_api_without_a_retry_is_left_alone(make):
    svc, api, _, _ = make(**_fast())                     # FakeApi: nothing to retry with
    svc.start()
    svc._fetch_timer.timeout.emit()
    svc.disconnect_calendar()
    assert api.revoked == ["rt"]                         # no error, no job left behind
