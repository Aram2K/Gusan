"""Gusan 2.0: a Notion connection belongs to the Gusan account that made it.

On a shared computer another account signing in must never inherit the previous
person's workspace: not the "Connected" row, not the page picker, and above all
not their meetings landing in it (auto-send). The rule is the calendar's:
the connection is stamped (notion_owner) when it is made; a different account
signing in forgets it; one made as a guest is adopted by the first account that
signs in; signed out (or offline at launch, no user_id yet) changes nothing.

main.AppController._reconcile_notion_owner and the stamp in set_settings, with a
fake controller: no Qt, no network."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main
import notion_connect
import notion_export
import settings_apply

PAGE = "0123456789abcdef0123456789abcdef"
CONNECTION = {"notion_api_key": "tok-A", "notion_refresh_token": "ref-A", "notion_auth_mode": "oauth",
              "notion_workspace_name": "A's workspace", "notion_parent_page": PAGE,
              "notion_parent_title": "Gusan meetings", "notion_hub_page": PAGE,
              "notion_auto_send": True, "notion_reconnect": False}


class _Signal:
    def emit(self, *a):
        pass


def controller(user_id=None, **cfg):
    """The parts of AppController the owner rule touches, with the real methods bound."""
    me = SimpleNamespace(cfg={"notion_owner": "", **cfg}, auth=SimpleNamespace(user_id=user_id),
                         saves=0, scheduled=0, sig_setting_changed=_Signal())
    me.save_config = lambda: setattr(me, "saves", me.saves + 1)
    me.schedule_config_save = lambda: setattr(me, "scheduled", me.scheduled + 1)
    me.set_settings = lambda updates: main.AppController.set_settings(me, updates)
    me._reconcile_notion_owner = lambda: main.AppController._reconcile_notion_owner(me)
    return me


class _Threads:
    """Thread stand-in that records what was started (and runs nothing)."""

    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.name = target, args, name

    def start(self):
        self.started.append((self.target, self.args, self.name))


class TestOwnerStamp(unittest.TestCase):
    def test_a_fresh_connection_is_stamped_with_the_account_that_made_it(self):
        me = controller("user-A")
        main.AppController.set_settings(me, dict(CONNECTION))
        self.assertEqual(me.cfg["notion_owner"], "user-A")
        self.assertEqual(me.cfg["notion_api_key"], "tok-A")

    def test_a_connection_made_as_a_guest_is_stamped_with_nobody(self):
        me = controller(None, notion_owner="user-A")             # a stale stamp from an earlier connection
        main.AppController.set_settings(me, dict(CONNECTION))
        self.assertEqual(me.cfg["notion_owner"], "")

    def test_a_token_refresh_does_not_change_whose_it_is(self):
        me = controller("user-A", **CONNECTION, notion_owner="user-A")
        main.AppController.set_settings(me, {"notion_api_key": "tok-2", "notion_refresh_token": "ref-2"})
        self.assertEqual(me.cfg["notion_owner"], "user-A")
        me = controller(None, **CONNECTION, notion_owner="user-A")      # ...nor does a guest's refresh
        main.AppController.set_settings(me, {"notion_api_key": "tok-2", "notion_refresh_token": "ref-2"})
        self.assertEqual(me.cfg["notion_owner"], "user-A")

    def test_other_settings_never_touch_it(self):
        me = controller("user-B", **CONNECTION, notion_owner="user-A")
        main.AppController.set_settings(me, {"notion_auto_send": False, "notion_parent_page": PAGE})
        self.assertEqual(me.cfg["notion_owner"], "user-A")

    def test_a_page_can_never_write_the_stamp(self):
        self.assertIn("notion_owner", settings_apply.PROTECTED_KEYS)
        me = controller("user-A", notion_owner="user-A")
        res = settings_apply.apply(me, {"notion_owner": "user-B"})
        self.assertEqual(res, {"notion_owner": False})
        self.assertEqual(me.cfg["notion_owner"], "user-A")

    def test_it_is_a_known_setting(self):
        self.assertEqual(main.DEFAULT["notion_owner"], "")


class TestReconcile(unittest.TestCase):
    def setUp(self):
        _Threads.started = []
        p = patch.object(main.threading, "Thread", _Threads)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(notion_connect, "forget_tokens")
        self.forgot = p.start()
        self.addCleanup(p.stop)

    def test_the_same_account_changes_nothing(self):
        me = controller("user-A", **CONNECTION, notion_owner="user-A")
        before = dict(me.cfg)
        me._reconcile_notion_owner()
        self.assertEqual((me.cfg, me.saves, _Threads.started), (before, 0, []))
        self.forgot.assert_not_called()

    def test_a_connection_made_as_a_guest_is_adopted_by_the_account_that_signs_in(self):
        me = controller("user-A", **CONNECTION)                    # stamp "": made as a guest
        me._reconcile_notion_owner()
        self.assertEqual(me.cfg["notion_owner"], "user-A")
        self.assertEqual(me.saves, 1)
        self.assertEqual(me.cfg["notion_api_key"], "tok-A")        # kept, with the switch as it was
        self.assertTrue(me.cfg["notion_auto_send"])
        self.assertEqual(_Threads.started, [])                     # nothing revoked
        me._reconcile_notion_owner()                               # and it stays adopted, quietly
        self.assertEqual(me.saves, 1)

    def test_another_account_forgets_the_connection_and_the_switch(self):
        me = controller("user-B", **CONNECTION, notion_owner="user-A", notify_when_ready=True)
        me._reconcile_notion_owner()
        for key, blank in notion_export.BLANK_SETTINGS.items():
            self.assertEqual(me.cfg[key], blank, key)
        self.assertIs(me.cfg["notion_auto_send"], False)
        self.assertIs(me.cfg["notion_reconnect"], False)
        self.assertEqual(me.cfg["notion_owner"], "")
        self.assertIs(me.cfg["notify_when_ready"], True)           # nothing else is touched
        self.assertFalse(notion_export.is_connected(me.cfg))
        self.forgot.assert_called_once_with()
        self.assertGreaterEqual(me.scheduled, 1)                   # saved, so the keyring is emptied too

    def test_the_other_accounts_grant_is_given_back_at_notion_off_the_gui_thread(self):
        me = controller("user-B", **CONNECTION, notion_owner="user-A")
        with patch.object(notion_connect, "latest_tokens", return_value={}):
            me._reconcile_notion_owner()
        self.assertEqual(_Threads.started, [(notion_connect.revoke, ("tok-A",), "gusan-notion-revoke")])

    def test_it_revokes_the_newest_token_when_the_saved_one_is_stale(self):
        me = controller("user-B", **CONNECTION, notion_owner="user-A")
        with patch.object(notion_connect, "latest_tokens", return_value={"access_token": "tok-newest"}):
            me._reconcile_notion_owner()
        self.assertEqual(_Threads.started[0][1], ("tok-newest",))

    def test_a_pasted_secret_is_forgotten_but_never_sent_to_our_server(self):
        own = {"notion_api_key": "ntn_secret", "notion_auth_mode": "own", "notion_parent_page": PAGE}
        me = controller("user-B", **own, notion_owner="user-A")
        me._reconcile_notion_owner()
        self.assertEqual(me.cfg["notion_api_key"], "")
        self.assertEqual(_Threads.started, [])

    def test_signed_out_changes_nothing(self):
        for user_id in (None, ""):
            me = controller(user_id, **CONNECTION, notion_owner="user-A")
            before = dict(me.cfg)
            me._reconcile_notion_owner()
            self.assertEqual((me.cfg, me.saves, _Threads.started), (before, 0, []), user_id)

    def test_a_computer_with_nothing_connected_changes_nothing(self):
        me = controller("user-B", notion_owner="user-A")           # A disconnected; the stamp lingers
        before = dict(me.cfg)
        me._reconcile_notion_owner()
        self.assertEqual((me.cfg, me.saves), (before, 0))

    def test_the_account_that_connects_after_that_is_the_owner(self):
        me = controller("user-B", notion_owner="user-A")
        main.AppController.set_settings(me, dict(CONNECTION))
        me._reconcile_notion_owner()
        self.assertEqual((me.cfg["notion_owner"], me.cfg["notion_api_key"]), ("user-B", "tok-A"))

    def test_account_changes_run_the_check_after_the_calendar_and_survive_its_failure(self):
        me = SimpleNamespace(_reconcile_user_secrets=lambda: False, auth=SimpleNamespace(
            is_pro=False, entitlement_known=False, user_id="user-B", user_email=""),
            cfg={}, calendar=None, main_win=None)
        calls = []
        me._track_pro_activation = lambda *a: None
        me._reconcile_notion_owner = lambda: calls.append("notion") or (_ for _ in ()).throw(RuntimeError("boom"))
        me._build_tray_menu = lambda: calls.append("tray")
        main.AppController._on_auth_changed(me)
        self.assertEqual(calls, ["notion", "tray"])                # a failure there never stops the rest


if __name__ == "__main__":
    unittest.main()
