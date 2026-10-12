"""Google Calendar in the app: the cached "Coming up" list, the "record this
meeting?" reminder and the saved bell / asked-already state.

Integration notes for Wave 4
----------------------------
Status (Wave 4, services): DONE in main.py - DEFAULTS in main.DEFAULT;
AppController._setup_calendar builds app.calendar only when
calendar_sync.configured(cfg) (else app.calendar is None), with
is_busy=_calendar_busy (checks self._reminder.is_showing()), set_owner at
start and in _on_auth_changed, stop() in quit_app; the tray's Privacy toggle
goes through set_setting. Also DONE (Wave 4, surfaces): _on_calendar_remind
shows the MeetingReminder toast (one, reused; a tray hint only if it can't
show), "Not this one" -> mark_prompted, Record ->
AppController.record_from_calendar (Pro gate, consent, meeting_session.start
with title / attendees / calendar_event_id), the tray's Next row follows
changed. Wave 4 review: Privacy Mode pauses the calendar's network reads
(network_ok in _setup_calendar; the founder can drop it to allow them).

Names: the service's methods are ``connect_calendar()`` / ``disconnect_calendar()``,
NOT connect()/disconnect(): PySide6's ``signal.connect(...)`` calls
``sender.connect`` on the QObject, so overriding those names breaks every
signal of the object (checked on PySide6 6.8.2).

1. Config defaults - add to main.DEFAULT (no side-effect entry needed in
   settings_apply; the service reads cfg live)::

       **calendar_service.DEFAULTS   # calendar_ask_to_record=True, calendar_only_with_link=True

2. Create it in AppController.__init__ after ``self._setup_tray()``
   (main.py:2539 today), pass the live cfg dict (mutated in place by set_setting).
   Construction is cheap and does no network: it reads calendar_state.json
   and, only when a client id exists, the keyring token::

       from calendar_service import CalendarService
       self.calendar = CalendarService(self.cfg, is_busy=self._calendar_busy, parent=self)
       self.calendar.remind.connect(self._on_calendar_remind)      # -> reminder toast
       self.calendar.changed.connect(self._on_calendar_changed)    # -> tray "Next:" row, Home chip
       self.calendar.disconnected.connect(self._on_calendar_disconnected)
       self.sig_setting_changed.connect(self.calendar.on_setting_changed)
       QTimer.singleShot(3000, self.calendar.start)                # first fetch after startup work

   Optional privacy decision (founder): to pause the calendar in Privacy Mode
   pass ``network_ok=lambda: not self.cfg.get("privacy_mode", False)``;
   fetches then stop and connect_calendar() reports PAUSED_TEXT. Leave it out
   to allow calendar reads in Privacy Mode. on_setting_changed("privacy_mode")
   already refetches when it turns off - but the tray's toggle_privacy_mode
   (main.py:2996) calls set_privacy_mode directly, which does NOT emit
   sig_setting_changed. Route it through ``self.set_setting("privacy_mode", ...)``
   (settings_apply already calls set_privacy_mode and then emits; it passes
   notify=False, so show the tray hint there yourself), so the calendar and
   the pages hear it once.

3. ``_calendar_busy()`` (GUI thread, cheap) - True while a reminder must not
   show: the same checks as _idle_sweep (is_rec, _busy, _file_job_running),
   a MeetingSession or Live Assistant session running, AND the reminder toast is
   already visible - that last one gives "one toast at a time" for
   overlapping meetings (the next due meeting is asked 20 s after it closes,
   if still inside its window). A due meeting is NOT marked asked while busy.

4. Account switching: call ``self.calendar.set_owner(self.auth.user_id)``
   once right after creating the service and again in ``_on_auth_changed``
   (it is idempotent). A calendar connected by a different Gusan account is
   disconnected (disconnected("account")); one connected as a guest is
   adopted by the first account that signs in; signing out keeps it.

5. ``quit_app``: call ``self.calendar.stop()`` (cancels a waiting browser
   sign-in, stops timers).

6. Reminder (Wave 3 ui/floating/meeting_reminder.py) on ``remind(meeting)``:
   the meeting is ALREADY marked asked (persisted) when the signal fires, so
   "Not this one" only closes the toast. Text: ``calendar_sync.reminder_text(m, now)``
   ("Starts in 1 min" / "Started 2 min ago"; add " · Record now?" after the
   start), meta ``" · ".join(p for p in (call, m["people"]) if p)`` where call
   is ``m["call_type"]`` ("" when there is no link; ``calendar_sync.CALL_OTHER``
   "other" has no brand: show "Video call"). Close it when
   ``calendar_sync.should_prompt(m, now)`` turns False. "Join call" only for
   ``m["link"].startswith("https://")``. Record -> new
   ``AppController.record_from_calendar(m, with_assist=False)`` (Pro gate,
   hide the toast first, prefill title=m["title"], people=m["attendees"]).

7. Meetings page (Wave 3) - hide every calendar part while
   ``not calendar.available`` (no Google client id in this build today):
   - on show: ``calendar.refresh()`` (rate-limited to once a minute);
     rows from ``calendar.meetings()`` and the ``changed(list)`` signal. Each
     row is a meeting dict plus ``muted`` (bool) and ``people``; time labels
     via ``calendar_sync.when_parts(m, now)``. Re-render every ~30 s for
     "in N min" (the service re-emits only when a row ends or a bell changes).
   - bell: ``calendar.set_muted(m["id"], not on)``.
   - Connect: ``calendar.connect_calendar()``; ``connecting(bool)`` drives
     "Waiting for Google..." + Cancel -> ``calendar.cancel_connect()``;
     ``error(str)`` carries the user-readable reason.
   - rail: ``calendar.email`` + Disconnect -> ``calendar.disconnect_calendar()``;
     toggles write cfg keys ASK_KEY / LINK_ONLY_KEY via set_setting.
   - start card prefill: ``calendar.current_meeting()`` ("From your calendar · HH:MM").
   - "Only meetings with a call link" filters BOTH the list and the reminders.
8. Home "Next:" chip and tray "Next:" row: ``calendar.next_meeting()`` on
   ``changed``; update one kept QAction instead of rebuilding the tray menu.

Signals (all delivered on the GUI thread): changed(list), connected(str email),
disconnected(str reason: "user" | "expired" | "account"), error(str),
remind(dict meeting), connecting(bool).

Layout
------
Three layers, so most of it tests without Qt:
- :class:`CalendarState`  prompted + muted event ids (each with the meeting's
  end, for pruning), the connected Google email and the Gusan account that
  connected it, in ``calendar_state.json`` (storage.path_for + atomic_write_json;
  NOT config.json, so save_config's tracking/stylesheet work never runs for it).
- :class:`CalendarCore`   every decision (what "Coming up" shows, which
  meeting is due, muting, owner checks, connect/disconnect bookkeeping) with
  an injectable clock, API module, busy check and network gate. No Qt, no
  threads, no network of its own.
- ``CalendarService``     the QObject the controller owns: worker-thread
  fetches every 5 min / on demand / after connect, a GUI QTimer every 20 s
  that checks reminders WITHOUT network, connect/disconnect on threads with
  cancel, and the signals above. Built lazily (module ``__getattr__``), so
  ``import calendar_service`` never imports PySide6.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import calendar_sync
import storage

logger = logging.getLogger("transcribe.calendar")

STATE_FILE = "calendar_state.json"
STATE_VERSION = 1
FETCH_EVERY_MS = 5 * 60 * 1000        # background refresh of "Coming up"
REMIND_EVERY_MS = 20 * 1000           # reminder check (no network)
MIN_REFRESH_S = 60                    # on-demand refresh (page shown) at most this often
PRUNE_GRACE = timedelta(minutes=10)   # keep an id this long after its meeting ended
UNKNOWN_END_KEEP = timedelta(days=2)  # muting an id that isn't in the cached list

ASK_KEY = "calendar_ask_to_record"    # rail "Ask me to record" (all reminders)
LINK_ONLY_KEY = "calendar_only_with_link"
DEFAULTS = {ASK_KEY: True, LINK_ONLY_KEY: True}
_CLIENT_KEYS = ("google_oauth_client_id", "google_oauth_client_secret")

REASON_USER = "user"
REASON_EXPIRED = "expired"
REASON_ACCOUNT = "account"

NOT_SET_UP_TEXT = "Calendar connection isn't set up in this version yet."
PAUSED_TEXT = "Turn off Privacy Mode to use Google Calendar."
CONNECT_FAILED_TEXT = "Couldn't connect Google Calendar - try again."
FETCH_FAILED_TEXT = "Couldn't read Google Calendar."


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_iso(value):
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else None


# ── saved state ──────────────────────────────────────────────────────────────
class CalendarState:
    """calendar_state.json: {"version", "email", "owner", "prompted": {id: end},
    "muted": {id: end}} with ends as ISO strings. A missing or broken file
    reads as empty (storage.read_json also tries the .bak)."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else storage.path_for(STATE_FILE)
        self.email = ""
        self.owner = ""
        self.prompted = {}
        self.muted = {}
        self.load()

    @staticmethod
    def _ids(value):
        if not isinstance(value, dict):
            return {}
        return {str(k): v for k, v in value.items() if k and isinstance(v, str)}

    def load(self):
        data = storage.read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        self.email = str(data.get("email") or "")
        self.owner = str(data.get("owner") or "")
        self.prompted = self._ids(data.get("prompted"))
        self.muted = self._ids(data.get("muted"))

    def to_dict(self):
        return {"version": STATE_VERSION, "email": self.email, "owner": self.owner,
                "prompted": dict(self.prompted), "muted": dict(self.muted)}

    def save(self):
        try:
            storage.atomic_write_json(self.path, self.to_dict())
            return True
        except Exception:
            logger.warning("Couldn't save the calendar state", exc_info=True)
            return False

    def prune(self, now):
        """Drop ids whose meeting ended (plus a grace) or whose end is
        unreadable. True when something was dropped."""
        cutoff = now - PRUNE_GRACE
        dropped = False
        for table in (self.prompted, self.muted):
            for event_id, iso in list(table.items()):
                end = _parse_iso(iso)
                if end is None or end < cutoff:
                    del table[event_id]
                    dropped = True
        return dropped

    def clear(self, keep_prefs=False):
        """Forget the account; ``keep_prefs`` keeps bells and asked ids (the
        same person will most likely connect again after an expiry)."""
        self.email = ""
        self.owner = ""
        if not keep_prefs:
            self.prompted.clear()
            self.muted.clear()


# ── decisions (no Qt) ────────────────────────────────────────────────────────
class CalendarCore:
    """What the service decides, with every outside thing injected:
    ``api`` (calendar_sync or a fake), ``clock`` (aware datetime), ``is_busy``
    and ``network_ok`` callables. GUI-thread only; holds no lock."""

    def __init__(self, cfg=None, *, api=None, state=None, state_path=None, clock=None,
                 is_busy=None, network_ok=None):
        self.cfg = cfg if cfg is not None else {}
        self.api = api if api is not None else calendar_sync
        self.state = state if state is not None else CalendarState(state_path)
        self.clock = clock or _utc_now
        self.is_busy = is_busy or (lambda: False)
        self.network_ok = network_ok or (lambda: True)
        self.cache = []
        self.owner_hint = ""
        self.connected = False
        self.refresh_connected()

    # -- status
    @property
    def available(self):
        """A Google OAuth client exists in this build (or in cfg for dev)."""
        try:
            return bool(self.api.configured(self.cfg))
        except Exception:
            return False

    def refresh_connected(self):
        """Re-read "connected" from the keyring (start-up, client id change).
        A saved email without a token (wiped elsewhere) is dropped."""
        was = self.connected
        available = self.available
        token_kept = False
        if available:                     # no keyring read in builds without a client
            try:
                token_kept = bool(self.api.is_connected())
            except Exception:
                token_kept = False
        self.connected = available and token_kept
        if available and not token_kept and self.state.email:
            self.state.clear(keep_prefs=True)
            self.state.save()
        if not self.connected:
            self.cache = []
        return self.connected != was

    def token_still_kept(self):
        """After an auth error from a fetch: True when the keyring still holds
        a token. Google's invalid_grant wipes it before the error is raised,
        so a kept token means the keyring read failed once (a macOS keychain
        hiccup) - not an expiry, and nothing to forget or revoke."""
        try:
            return bool(self.api.is_connected())
        except Exception:
            return False

    def setting(self, key):
        return bool(self.cfg.get(key, DEFAULTS.get(key, False)))

    def network_allowed(self):
        try:
            return bool(self.network_ok())
        except Exception:
            logger.debug("calendar network_ok failed", exc_info=True)
            return False

    def busy(self):
        try:
            return bool(self.is_busy())
        except Exception:
            logger.debug("calendar is_busy failed; treating as busy", exc_info=True)
            return True

    # -- the list
    def _decorate(self, m):
        d = dict(m)
        d["muted"] = m["id"] in self.state.muted
        if "people" not in d:
            d["people"] = calendar_sync.people_label(m.get("others"))
        return d

    def meetings(self, now=None):
        """The "Coming up" rows (ended meetings dropped, the call-link filter
        applied), each a copy with ``muted`` and ``people``."""
        if not self.connected:
            return []
        now = now or self.clock()
        rows = calendar_sync.visible_upcoming(self.cache, now,
                                              only_with_link=self.setting(LINK_ONLY_KEY))
        return [self._decorate(m) for m in rows]

    @staticmethod
    def rows_key(rows):
        """What a re-render depends on, to re-emit only when it changes."""
        return tuple((m["id"], m.get("muted"), m["start"], m["end"], m.get("title")) for m in rows)

    def list_key(self, now=None):
        return self.rows_key(self.meetings(now))

    def next_meeting(self, now=None):
        """The next meeting that hasn't started (Home / tray "Next:"), or None."""
        now = now or self.clock()
        return next((m for m in self.meetings(now) if m["start"] > now), None)

    def current_meeting(self, now=None):
        """The meeting on now or starting within 5 min, for the start card
        (any meeting, link or not), or None."""
        if not self.connected:
            return None
        now = now or self.clock()
        m = calendar_sync.current_meeting(calendar_sync.visible_upcoming(self.cache, now), now)
        return self._decorate(m) if m else None

    def apply_fetch(self, meetings, now=None):
        now = now or self.clock()
        self.cache = [m for m in meetings or [] if calendar_sync.is_meeting(m)]
        if self.state.prune(now):
            self.state.save()

    # -- bells and reminders
    def _end_iso(self, event_id, now):
        m = next((m for m in self.cache if m.get("id") == event_id), None)
        return (m["end"] if m else now + UNKNOWN_END_KEEP).isoformat()

    def is_muted(self, event_id):
        return event_id in self.state.muted

    def set_muted(self, event_id, muted, now=None):
        """Turn one meeting's bell off (muted=True) or on. True if it changed."""
        if not event_id:
            return False
        now = now or self.clock()
        if muted == (event_id in self.state.muted):
            return False
        if muted:
            self.state.muted[event_id] = self._end_iso(event_id, now)
        else:
            self.state.muted.pop(event_id, None)
        self.state.save()
        return True

    def mark_prompted(self, event_id, now=None):
        if not event_id or event_id in self.state.prompted:
            return False
        self.state.prompted[event_id] = self._end_iso(event_id, now or self.clock())
        self.state.save()
        return True

    def due_reminder(self, now=None):
        """The meeting due a reminder now (no side effects), or None."""
        if not self.connected or not self.setting(ASK_KEY):
            return None
        return calendar_sync.reminder_due(self.cache, now or self.clock(),
                                          muted=self.state.muted, prompted=self.state.prompted,
                                          only_with_link=self.setting(LINK_ONLY_KEY))

    def take_reminder(self, now=None):
        """The meeting to remind about now, marked asked (saved) so it's never
        asked again, even after a restart. None while busy (recording, a
        toast already showing) - nothing is marked then."""
        now = now or self.clock()
        m = self.due_reminder(now)
        if m is None or self.busy():
            return None
        self.mark_prompted(m["id"], now)
        return self._decorate(m)

    # -- account
    def on_connected(self, email):
        self.connected = True
        self.state.email = email or ""
        self.state.owner = self.owner_hint
        self.state.save()

    def forget(self, reason=REASON_USER):
        """Disconnect bookkeeping: drop the token here (no network), the cache
        and the account. Returns the refresh token to revoke at Google."""
        token = ""
        try:
            token = self.api.forget_local() or ""
        except Exception:
            logger.debug("calendar forget_local failed", exc_info=True)
        self.connected = False
        self.cache = []
        self.state.clear(keep_prefs=(reason == REASON_EXPIRED))
        self.state.save()
        return token

    def reconcile_owner(self, owner):
        """The signed-in Gusan account changed to ``owner`` (an id; "" or None
        when signed out). Returns "none", "same", "adopted" or "mismatch"
        (a calendar connected by another account: disconnect it)."""
        owner = str(owner or "").strip()
        # Who a connect made from now on belongs to: "" while signed out, so
        # a calendar connected as a guest isn't stamped with the last account.
        self.owner_hint = owner
        if not owner or not self.connected:
            return "none"
        if not self.state.owner:
            self.state.owner = owner
            self.state.save()
            return "adopted"
        return "same" if self.state.owner == owner else "mismatch"


def _start_thread(fn):
    threading.Thread(target=fn, daemon=True, name="gusan-calendar").start()


def _emit(owner, signal_name, *args):
    """Emit ``owner.<signal_name>`` from a worker thread."""
    try:
        getattr(owner, signal_name).emit(*args)
    except RuntimeError:
        pass   # the service was deleted (app quitting) while a worker ran


# ── the QObject (PySide6 imported only when it is first used) ───────────────
def _build_service_class():
    from PySide6.QtCore import QObject, QTimer, Signal, Slot

    class CalendarService(QObject):
        """Owned by AppController. See the module docstring for the wiring."""

        changed = Signal(object)        # list of meeting dicts (+ "muted", "people")
        connected = Signal(str)         # Google account email ("" if Google didn't say)
        disconnected = Signal(str)      # "user" | "expired" | "account"
        error = Signal(str)             # user-readable; a fetch error once per streak
        remind = Signal(object)         # the meeting dict to ask about
        connecting = Signal(bool)       # browser sign-in waiting (True) / over (False)
        _fetched = Signal(int, object, str, str)       # gen, meetings, kind, message
        _connect_done = Signal(int, str, str, str)     # gen, email, kind, message

        def __init__(self, cfg=None, is_busy=None, parent=None, *, api=None, clock=None,
                     network_ok=None, state_path=None, run_async=None, open_browser=None):
            super().__init__(parent)
            self.core = CalendarCore(cfg, api=api, state_path=state_path, clock=clock,
                                     is_busy=is_busy, network_ok=network_ok)
            self._run_async = run_async or _start_thread
            self._open_browser = open_browser
            self._started = False
            self._fetch_gen = 0
            self._fetching = None          # gen of the fetch in flight
            self._fetch_again = False
            self._last_fetch_ok = None
            self.last_error = ""
            self._connect_gen = 0
            self._connect_cancel = None    # threading.Event while the browser sign-in waits
            self._last_key = None
            self._fetch_timer = QTimer(self)
            self._fetch_timer.setInterval(FETCH_EVERY_MS)
            self._fetch_timer.timeout.connect(self._on_fetch_timer)
            self._remind_timer = QTimer(self)
            self._remind_timer.setInterval(REMIND_EVERY_MS)
            self._remind_timer.timeout.connect(self.check_reminders)
            # Bound slots on this object: emitted from a worker thread they are
            # queued to the GUI thread (AutoConnection); from the GUI thread
            # (tests' synchronous run_async) they run at once.
            self._fetched.connect(self._on_fetched)
            self._connect_done.connect(self._on_connect_done)

        # -- status
        @property
        def available(self):
            return self.core.available

        @property
        def is_connected(self):
            return self.core.connected

        @property
        def is_connecting(self):
            return self._connect_cancel is not None

        @property
        def email(self):
            return self.core.state.email if self.core.connected else ""

        def meetings(self):
            return self.core.meetings(self.core.clock())

        def next_meeting(self):
            return self.core.next_meeting(self.core.clock())

        def current_meeting(self):
            return self.core.current_meeting(self.core.clock())

        def is_muted(self, event_id):
            return self.core.is_muted(event_id)

        # -- lifecycle
        def start(self):
            """Begin polling if a calendar is connected (call once, after startup)."""
            self._started = True
            if self.core.connected:
                self._start_timers()
                self.refresh(force=True)

        def stop(self):
            """App quit: stop timers, let a waiting sign-in go, ignore late results."""
            self._started = False
            self.cancel_connect()
            self._stop_timers()
            self._fetch_gen += 1
            self._fetching = None
            self._fetch_again = False

        def _start_timers(self):
            if not self._fetch_timer.isActive():
                self._fetch_timer.start()
            if not self._remind_timer.isActive():
                self._remind_timer.start()

        def _stop_timers(self):
            self._fetch_timer.stop()
            self._remind_timer.stop()

        # -- fetching
        def refresh(self, force=False):
            """Fetch "Coming up" on a worker thread. Without ``force`` at most
            once a minute (a page shown repeatedly). True if a fetch runs."""
            if not (self.core.connected and self.core.available):
                return False
            if not self.core.network_allowed():
                return False
            now = self.core.clock()
            if (not force and self._last_fetch_ok is not None
                    and (now - self._last_fetch_ok).total_seconds() < MIN_REFRESH_S):
                return False
            if self._fetching is not None:
                self._fetch_again = True
                return True
            gen = self._fetch_gen
            self._fetching = gen
            api, cfg = self.core.api, self.core.cfg

            def work():
                items, kind, msg = [], "ok", ""
                try:
                    items = api.upcoming(cfg, now=now)
                except calendar_sync.CalendarAuthError as e:
                    kind, msg = "auth", str(e)
                except calendar_sync.CalendarError as e:
                    kind, msg = "error", str(e)
                except Exception:
                    logger.exception("Calendar fetch failed")
                    kind, msg = "error", FETCH_FAILED_TEXT
                _emit(self, "_fetched", gen, items, kind, msg)

            self._run_async(work)
            return True

        @Slot()
        def _on_fetch_timer(self):
            self.refresh(force=True)

        @Slot(int, object, str, str)
        def _on_fetched(self, gen, items, kind, msg):
            if gen != self._fetch_gen:
                return                                  # disconnected / stopped meanwhile
            self._fetching = None
            now = self.core.clock()
            if kind == "ok":
                self.core.apply_fetch(items, now)
                self._last_fetch_ok = now
                self.last_error = ""
                self._emit_changed(now)
                self.check_reminders()
            elif kind == "auth" and not self.core.token_still_kept():
                self.disconnect_calendar(REASON_EXPIRED)
                return
            else:
                # Offline etc.: keep the cached list, say it once per streak.
                if kind == "auth":
                    msg = FETCH_FAILED_TEXT     # the token is there: a keyring hiccup
                if msg != self.last_error:
                    self.last_error = msg
                    self.error.emit(msg)
            if self._fetch_again:
                self._fetch_again = False
                self.refresh(force=True)

        def _emit_changed(self, now=None):
            now = now or self.core.clock()
            rows = self.core.meetings(now)
            self._last_key = self.core.rows_key(rows)
            self.changed.emit(rows)

        # -- reminders
        @Slot()
        def check_reminders(self):
            """The 20 s tick: re-emit the list when a row ended, then ask about
            a due meeting. No network."""
            now = self.core.clock()
            if self.core.connected and self.core.list_key(now) != self._last_key:
                self._emit_changed(now)
            m = self.core.take_reminder(now)
            if m is not None:
                self.remind.emit(m)

        def set_muted(self, event_id, muted):
            """The row's bell: muted=True means "don't ask me to record this one"."""
            if self.core.set_muted(event_id, bool(muted)):
                self._emit_changed()
                return True
            return False

        def mark_prompted(self, event_id):
            return self.core.mark_prompted(event_id)

        # -- connect / disconnect
        def connect_calendar(self):
            """Start the browser sign-in on a worker thread. False when one is
            already waiting or it can't start (error(str) says why)."""
            if self._connect_cancel is not None:
                return False
            if not self.core.available:
                self.error.emit(NOT_SET_UP_TEXT)
                return False
            if not self.core.network_allowed():
                self.error.emit(PAUSED_TEXT)
                return False
            cancel = threading.Event()
            self._connect_cancel = cancel
            self._connect_gen += 1
            gen = self._connect_gen
            api, cfg, open_browser = self.core.api, self.core.cfg, self._open_browser
            self.connecting.emit(True)

            def work():
                email, kind, msg = "", "ok", ""
                try:
                    kw = {"cancel": cancel}
                    if open_browser is not None:
                        kw["open_browser"] = open_browser
                    email = api.connect(cfg, **kw) or ""
                except calendar_sync.CalendarCancelled:
                    kind = "cancelled"
                except calendar_sync.CalendarError as e:
                    kind, msg = "error", str(e)
                except Exception:
                    logger.exception("Calendar connect failed")
                    kind, msg = "error", CONNECT_FAILED_TEXT
                _emit(self, "_connect_done", gen, email, kind, msg)

            self._run_async(work)
            return True

        def cancel_connect(self):
            """Stop waiting for the browser sign-in (its listener closes within ~1 s)."""
            if self._connect_cancel is None:
                return False
            self._connect_cancel.set()
            self._connect_cancel = None
            self._connect_gen += 1
            self.connecting.emit(False)
            return True

        @Slot(int, str, str, str)
        def _on_connect_done(self, gen, email, kind, msg):
            if gen != self._connect_gen:
                # A cancelled attempt that still got through: keep nothing,
                # unless a newer attempt or a live connection owns the token.
                if kind == "ok" and self._connect_cancel is None and not self.core.connected:
                    api = self.core.api
                    try:
                        token = api.forget_local() or ""
                    except Exception:
                        token = ""
                    if token:
                        self._run_async(lambda: api.revoke(token))
                return
            self._connect_cancel = None
            if kind == "ok":
                self.core.on_connected(email)
                self.connecting.emit(False)
                self._last_key = None
                self._start_timers()
                self.connected.emit(email)
                self.refresh(force=True)
                return
            self.connecting.emit(False)
            if kind == "error":
                self.error.emit(msg)

        def disconnect_calendar(self, reason=REASON_USER):
            """Forget the calendar now (keyring, cache, account) and revoke the
            access at Google on a worker thread."""
            self.cancel_connect()
            self._stop_timers()
            self._fetch_gen += 1
            self._fetching = None
            self._fetch_again = False
            self._last_fetch_ok = None
            self.last_error = ""
            api = self.core.api
            token = self.core.forget(reason)
            if token:
                self._run_async(lambda: api.revoke(token))
            self._last_key = ()
            self.changed.emit([])
            self.disconnected.emit(reason)

        # -- the rest of the app
        def set_owner(self, owner):
            """The signed-in Gusan account (auth.user_id, or None)."""
            if self.core.reconcile_owner(owner) == "mismatch":
                self.disconnect_calendar(REASON_ACCOUNT)

        @Slot(str)
        def on_setting_changed(self, key):
            """Hook for AppController.sig_setting_changed."""
            if key == LINK_ONLY_KEY:
                if self.core.connected:
                    self._emit_changed()
            elif key in _CLIENT_KEYS:
                if self.core.refresh_connected():
                    if self.core.connected:
                        self._start_timers()
                        self.refresh(force=True)
                    else:
                        self._stop_timers()
                        self._last_key = ()
                        self.changed.emit([])
            elif key == "privacy_mode":
                self.refresh(force=True)

    return CalendarService


def __getattr__(name):
    if name == "CalendarService":
        cls = _build_service_class()
        globals()["CalendarService"] = cls
        return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
