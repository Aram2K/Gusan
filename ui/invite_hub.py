"""The invite program's one Qt-side state: the last "me" answer, the calls (always on a worker thread), and the
signal every surface follows (the sidebar entry, the Invite page, Home's join card, the Account page, the tray).

    hub = invite_hub.hub(app)          # made on first use and kept on the app (app.invite_hub)
    hub.changed.connect(...)           # the state changed (GUI thread)
    hub.view()                         # invites.view(...) for the current answer and plan
    hub.refresh() / hub.open_page()    # "me" on a worker (open_page: with this computer's code once a day)
    hub.join(code, source, then)       # then(result) on the GUI thread
    hub.claim(reward_id, then)
    hub.track(event, props)            # the funnel's usage events (app.track: anonymous, off with analytics off)

Nothing here runs at start-up: the hub is made the first time something asks for it (the window's sidebar, the
first auth change after the session restore), its cache is read on a worker, and the first "me"
is asked a few seconds after a sign-in is known. Every call is quiet when it fails: the state stays as it was and
the page shows a calm retry.

A friend who newly counts gets one small note, once (invites.newly_counted): a toast in the Gusan window when it
is on screen, else a tray notification that opens the Invite page.
"""

import logging

from PySide6.QtCore import QObject, QTimer, Signal

import invites

logger = logging.getLogger("transcribe")

REFRESH_AFTER_SIGN_IN_MS = 3000         # the first "me" after a sign-in is known (off the start's busiest seconds)
MIN_REFRESH_S = 60                      # "me" is asked at most this often unless the page asks for it
PAGE_REFRESH_S = 20                     # the Invite page re-asks when its answer is older than this
CLAIM_RECHECK_MS = 3000                 # after a claim's server error: "me" again (it may have gone through)
WINDOW_REFRESH_S = 30 * 60              # the Gusan window coming up re-asks when the answer is older than this


def _run(fn, then=None, error=None, sync=False):
    """fn() on a worker, then(result) on the GUI thread (api_keys.run_then); sync=True runs it right here."""
    if sync:
        try:
            res = fn()
        except Exception as e:                 # noqa: BLE001 - quiet
            if error is not None:
                error(e)
            return
        if then is not None:
            then(res)
        return
    from api_keys import run_then
    run_then(fn, then=then, error=error, name="gusan-invite")


def _monotonic():
    import time
    return time.monotonic()


class InviteHub(QObject):
    changed = Signal()

    sync_default = False                    # tests / previews set it before a hub is made

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.sync_io = bool(InviteHub.sync_default)     # True: calls run right here (tests, previews)
        self._seen = None                   # (account, plan, tier) last drawn
        self._user = None                   # the account the state belongs to (user id, else email)
        self._state = {}                    # invites.load_state(...) for that account
        self._loaded = False                # the cache was read for this account
        self._loading = False
        self._fetching = False
        self._fetched_at = None             # monotonic time of the last "me" answer
        self._failed = False                # the last "me" failed and nothing is known
        self._hint = None                   # this run's "hint" answer for the account
        self._hint_asked = False
        self._gen = 0                       # bumped on every account switch: an old answer is dropped
        self._window = None                 # the Gusan window (attach_window), for the small notes
        self._joined_now = None             # after a join in this run: the inviter's name ("" when not shared)
        self._not_eligible = False          # a claim was answered "not_eligible" (Lifetime / admin): no months
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self.refresh(force=True))
        self.on_auth_changed()

    # ── who ──
    def auth(self):
        return getattr(self.app, "auth", None)

    def signed_in(self):
        try:
            import account_actions
            if account_actions.app_tier(self.app) == "guest":
                return False
        except Exception:
            pass
        return bool(getattr(self.auth(), "is_authenticated", False))

    def _user_key(self):
        if not self.signed_in():
            return None
        a = self.auth()
        return getattr(a, "user_id", None) or getattr(a, "user_email", None) or None

    def plan(self):
        a = self.auth()
        plan = str(getattr(a, "plan", None) or "").lower()
        if getattr(a, "is_admin", False) and not plan:
            plan = "admin"
        return plan

    # ── state ──
    def me(self):
        return self._state.get("me") if self._user else None

    def state(self):
        return dict(self._state)

    def loaded(self):
        return self.me() is not None

    def failed(self):
        return self._failed and self.me() is None

    def busy(self):
        return self._fetching or self._loading

    def view(self):
        me = self.me()
        credited = set(self._state.get("credited_ids") or [])
        if me and credited:                     # a subscriber's claimed month: "Your next bill is free" stays true
            me = dict(me, rewards=[dict(r, status="credited") if r.get("id") in credited else r
                                   for r in me.get("rewards") or []])
        return invites.view(me, self.plan(), can_earn=False if self._not_eligible else None)

    def sidebar_state(self):
        """{"visible", "counted", "progress", "ready"} for the sidebar entry and the tray row."""
        if not self.signed_in():
            return {"visible": True, "counted": 0, "progress": 0, "ready": False, "known": False}
        v = self.view()
        c, p = v["dots"]
        return {"visible": True, "counted": c, "progress": p, "ready": v["ready"], "known": self.loaded()}

    def can_join(self):
        me = self.me()
        return bool(me and me.get("can_join") and self.signed_in())

    def joined_name(self):
        me = self.me() or {}
        return (me.get("invited_by") or {}).get("name") or ""

    def hint(self):
        return self._hint

    def joined_now(self):
        """None, or - when this account joined an invite during this run - the inviter's name ("" when the server
        does not share it: "You joined the invite")."""
        return self._joined_now

    # ── the account ──
    def _tier(self):
        try:
            import account_actions
            return account_actions.app_tier(self.app)
        except Exception:
            return ""

    def on_auth_changed(self):
        """Cheap: only a new account (or a sign-out) changes anything here; a plan change re-draws."""
        key = self._user_key()
        seen = (key, self.plan(), self._tier())
        if key == self._user and self._seen is not None:
            if seen != self._seen:
                self._seen = seen
                self._not_eligible = False      # a new plan: the server is asked again at the next claim
                self.changed.emit()             # the plan changed: the ticket follows
            return
        self._seen = seen
        self._gen += 1
        self._user = key
        self._state = {}
        self._loaded = False
        self._fetched_at = None
        self._failed = False
        self._hint = None
        self._hint_asked = False
        self._joined_now = None
        self._not_eligible = False
        self._timer.stop()
        if key:
            self._load_cache()
            if self.sync_io:
                self.refresh(force=True)                # tests / previews: the first answer right away
            else:
                self._timer.start(REFRESH_AFTER_SIGN_IN_MS)
        self.changed.emit()

    def _load_cache(self):
        key, gen = self._user, self._gen
        self._loading = True

        def done(st, g=gen):
            if g != self._gen:
                return
            self._loading = False
            self._loaded = True
            if isinstance(st, dict):
                # A "me" that landed meanwhile is newer than the cache's.
                me = self._state.get("me")
                self._state = dict(st, **{k: v for k, v in self._state.items() if v is not None})
                if me is not None:
                    self._state["me"] = me
            self.changed.emit()

        def failed(_e, g=gen):
            if g == self._gen:
                self._loading = False
                self._loaded = True

        _run(lambda: invites.load_state(key), done, failed, sync=self.sync_io)

    def _save(self, **changes):
        """Update the in-memory state now and the cache file on a worker."""
        self._state.update(changes)
        key = self._user
        if key:
            _run(lambda: invites.update_state(key, **changes), sync=self.sync_io)

    # ── "me" ──
    def refresh(self, device=False, force=False):
        """Ask the server for "me" (a worker). Not more often than MIN_REFRESH_S unless ``force``."""
        if not self.signed_in() or not self._user:
            return False
        if self._fetching:
            return False
        if not force and self._fetched_at is not None and _monotonic() - self._fetched_at < MIN_REFRESH_S:
            return False
        auth, gen, key = self.auth(), self._gen, self._user
        self._fetching = True
        day = invites.today()

        def done(me, g=gen):
            if g != self._gen:
                return
            self._fetching = False
            self._fetched_at = _monotonic()
            if me is None:
                self._failed = True
                self.changed.emit()
                return
            self._failed = False
            fresh, announced = invites.newly_counted(self._state, me)
            changes = {"me": me, "me_at": invites.today(), "announced": announced}
            if device:
                changes["device_day"] = day
            self._save(**changes)
            self.changed.emit()
            for f in fresh:
                self._announce(f)

        def failed(_e, g=gen):
            if g == self._gen:
                self._fetching = False
                self._failed = True
                self.changed.emit()

        _run(lambda: invites.fetch_me(auth, device=device), done, failed, sync=self.sync_io)
        return True

    def open_page(self):
        """The Invite page opened: a fresh "me" (with this computer's code once a day)."""
        if not self.signed_in():
            return False
        device = invites.device_due(self._state, invites.today())
        stale = self._fetched_at is None or _monotonic() - self._fetched_at > PAGE_REFRESH_S
        if device or stale:
            return self.refresh(device=device, force=True)
        return False

    def retry(self):
        return self.refresh(force=True)

    def refresh_if_stale(self, max_age_s=WINDOW_REFRESH_S):
        """The window came up: a fresh "me" when the last one is older than ``max_age_s`` (the sidebar's dots and
        a friend's "counts" note keep up in a Gusan that runs for days in the tray)."""
        if self._fetched_at is not None and _monotonic() - self._fetched_at < max_age_s:
            return False
        if self._fetched_at is None and self._timer.isActive():
            return False                                # the first answer is on its way
        return self.refresh(force=True)

    # ── usage events ──
    def track(self, event, props=None):
        """One of the invite funnel's events through the app's telemetry (app.track -> telemetry.track: nothing
        when analytics is off, keyed by the install id). ``props`` are words from a fixed set (a channel, a
        source, a status) - never a code, a link, a name or an id. Never raises."""
        fn = getattr(self.app, "track", None)
        if callable(fn):
            try:
                fn(event, dict(props or {}))
            except Exception:
                logger.debug("invite event %s failed", event, exc_info=True)

    # ── the invited friend ──
    def ask_hint(self, then=None):
        """Once a run per account: did the server see this person open a friend's link? then(hint) on the GUI
        thread; an answer already known comes back at once."""
        if self._hint is not None:
            if then is not None:
                then(self._hint)
            return
        if not self.signed_in():
            return
        waiters = self.__dict__.setdefault("_hint_waiters", [])
        if then is not None:
            waiters.append(then)
        if self._hint_asked:
            return
        self._hint_asked = True
        auth, gen = self.auth(), self._gen

        def done(h, g=gen):
            if g != self._gen:
                return
            self._hint = h if isinstance(h, dict) else {"found": False, "name": "", "code": ""}
            cbs, self.__dict__["_hint_waiters"] = list(waiters), []
            for cb in cbs:
                try:
                    cb(self._hint)
                except RuntimeError:
                    pass                        # its card is gone

        _run(lambda: invites.fetch_hint(auth), done, lambda e: done(None), sync=self.sync_io)

    def join(self, code, source="paste", then=None):
        """Join a friend's invite (a worker), then refresh the plan and "me". then(result) on the GUI thread."""
        auth, gen = self.auth(), self._gen

        def work():
            res = invites.join(auth, code, source)
            if res.get("ok"):
                _refresh_entitlement(auth)
                me = invites.fetch_me(auth)
                return res, me
            return res, None

        def done(pair, g=gen):
            res, me = pair
            if g == self._gen and res.get("ok"):
                if me is not None:
                    self._save(me=me, me_at=invites.today())
                else:
                    cur = dict(self._state.get("me") or {})
                    cur.update(can_join=False, invited_by={"name": res.get("name") or ""},
                               gift={"active": True, "ends_at": res.get("gift_ends_at")})
                    self._save(me=cur)
                self._joined_now = res.get("name") or self.joined_name() or ""
                self.track("invite_joined", {"source": "hint" if source == "hint" else "paste"})
            # The caller's answer first (its card says "You joined ..."), then everything that redraws.
            if then is not None:
                then(res)
            if g == self._gen and res.get("ok"):
                self.changed.emit()
                self._auth_changed()

        def failed(_e):
            if then is not None:
                then({"ok": False, "reason": "network"})

        _run(work, done, failed, sync=self.sync_io)

    # ── the inviter ──
    def claim(self, reward_id, then=None):
        """Claim a ready month (a worker), refresh the plan, then "me". then(result) on the GUI thread."""
        auth, gen = self.auth(), self._gen

        def work():
            res = invites.claim(auth, reward_id)
            if res.get("ok"):
                _refresh_entitlement(auth)
                return res, invites.fetch_me(auth)
            return res, None

        def done(pair, g=gen):
            res, me = pair
            if g == self._gen and res.get("ok"):
                if me is None:                         # the answer is known: mark the month claimed here
                    cur = dict(self._state.get("me") or {})
                    rewards = [dict(r) for r in cur.get("rewards") or []]
                    for r in rewards:
                        if r.get("id") == reward_id:
                            r.update(status="credited" if res["status"] == "next_bill_free" else res["status"],
                                     starts_at=res.get("starts_at"), ends_at=res.get("ends_at"))
                    cur["rewards"] = rewards
                    me = cur
                changes = {"me": me, "me_at": invites.today()}
                if res.get("status") == "next_bill_free":     # kept, whatever word the server uses for it later
                    changes["credited_ids"] = list(self._state.get("credited_ids") or []) + [reward_id]
                self._save(**changes)
                self.track("invite_reward_claimed",
                           {"status": "next_bill_free" if res.get("status") == "next_bill_free" else "active"})
            if g == self._gen and not res.get("ok") and res.get("reason") == "not_eligible":
                self._not_eligible = True               # the ticket, the lead and the sidebar all say so now
            if then is not None:
                then(res)
            if g == self._gen and not res.get("ok") and res.get("reason") == "server":
                # The card company may have said yes before the error: the next "me" shows the month.
                if self.sync_io:
                    self.refresh(force=True)
                else:
                    QTimer.singleShot(CLAIM_RECHECK_MS, lambda: self.refresh(force=True))
            if g == self._gen and res.get("ok"):
                self.changed.emit()
                self._auth_changed()
            elif g == self._gen and self._not_eligible:
                self.changed.emit()

        def failed(_e):
            if then is not None:
                then({"ok": False, "reason": "network"})

        _run(work, done, failed, sync=self.sync_io)

    # ── notes ──
    def attach_window(self, win):
        """The Gusan window (ui/shell.MainWindow) shows the small notes while it is on screen."""
        import weakref
        self._window = weakref.ref(win) if win is not None else None

    def window(self):
        win = self._window() if self._window is not None else None
        return win if win is not None else getattr(self.app, "main_win", None)

    def _announce(self, friend):
        """One small note when a friend newly counts: a toast in the window, else the tray."""
        v = self.view()
        title, sub = invites.counted_note(friend, v["cycle_counted"])
        win = self.window()
        try:
            if win is not None and win.isVisible() and not win.isMinimized():
                from ui.components import celebrate
                celebrate.show_toast(win, title, sub, avatar=(friend.get("initial") or "", friend.get("name")),
                                     dots=v["dots"], on_click=lambda: self._open_page())
                return
        except RuntimeError:
            pass
        fn = getattr(self.app, "show_tray_hint", None)
        if callable(fn):
            try:
                fn(title, sub, on_click=self._open_page)
            except Exception:
                logger.debug("invite note failed", exc_info=True)

    def _open_page(self):
        fn = getattr(self.app, "show_main", None)
        if callable(fn):
            try:
                fn("invite")
            except Exception:
                logger.debug("open invite failed", exc_info=True)

    def _auth_changed(self):
        """After a join or a claim: the window re-reads the plan (refresh_entitlement already ran on the worker;
        the real AuthManager announces it itself - this covers a stand-in without that signal)."""
        win = self.window()
        if win is not None and not hasattr(self.app, "sig_auth_changed"):
            try:
                win.refresh_account()
            except Exception:
                logger.debug("window refresh failed", exc_info=True)


def _refresh_entitlement(auth):
    fn = getattr(auth, "refresh_entitlement", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            logger.debug("refresh_entitlement failed", exc_info=True)


def hub(app):
    """The app's one InviteHub (made on first use, kept as app.invite_hub). None without a QApplication."""
    h = getattr(app, "invite_hub", None)
    if isinstance(h, InviteHub):
        return h
    try:
        from PySide6.QtCore import QCoreApplication
        if QCoreApplication.instance() is None:
            return None
        h = InviteHub(app)
        app.invite_hub = h
        return h
    except Exception:
        logger.warning("Invite hub unavailable", exc_info=True)
        return None


def existing(app):
    """The hub when one was made already (never makes one: for the take hooks on a worker thread)."""
    h = getattr(app, "invite_hub", None)
    return h if isinstance(h, InviteHub) else None


__all__ = ["InviteHub", "hub", "existing"]
