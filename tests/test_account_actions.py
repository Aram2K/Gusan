"""account_actions: the Account & plan view-model, delete account, feedback (fake HTTP)."""
import os
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import account_actions as aa  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def auth(tier="free", plan=None, status=None, period_end=None, cancel=False, admin=False,
         name="Aram", email="aram@aibuben.xyz", token="tok"):
    signed_in = tier != "guest"
    return SimpleNamespace(
        is_authenticated=signed_in, is_pro=(tier == "pro"), plan=plan, subscription_status=status,
        period_end=period_end, cancel_at_period_end=cancel, is_admin=admin, user_id="U" if signed_in else None,
        user_name=name if signed_in else None, user_email=email if signed_in else None,
        get_access_token=lambda: token)


def resp(status):
    r = MagicMock()
    r.status_code = status
    return r


class _NoDevTier(unittest.TestCase):
    def setUp(self):
        p = patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("TRANSCRIBE_DEV_TIER", None)


class TestAccountView(_NoDevTier):
    def test_guest(self):
        v = aa.account_view(auth("guest"), {}, now=NOW)
        self.assertEqual((v["signed_in"], v["name"], v["email"], v["initial"], v["tier"], v["badge"]),
                         (False, "Guest", "", "", "guest", "GUEST"))
        self.assertEqual(v["smart_text"], "Sign up to try them")
        self.assertEqual(v["buttons"], {"sign_in": True, "sign_out": False, "upgrade": True,
                                        "manage": False, "delete": False, "admin": False})

    def test_free(self):
        v = aa.account_view(auth("free"), {}, now=NOW, smart_remaining=4)
        self.assertEqual((v["name"], v["initial"], v["badge"], v["plan_label"], v["plan_line"]),
                         ("Aram", "A", "FREE", "Free", "Free"))
        self.assertEqual((v["smart_tries_left"], v["smart_tries_total"], v["smart_text"]), (4, 5, "4 of 5 left"))
        self.assertTrue(v["buttons"]["upgrade"] and v["buttons"]["sign_out"] and v["buttons"]["delete"])
        self.assertFalse(v["buttons"]["manage"])

    def test_name_falls_back_to_the_email(self):
        v = aa.account_view(auth("free", name=None, email="mariam@example.com"), {}, smart_remaining=0)
        self.assertEqual((v["name"], v["initial"], v["smart_text"]), ("mariam", "M", "0 of 5 left"))

    def test_pro_annual(self):
        v = aa.account_view(auth("pro", plan="annual", status="active", period_end="2027-10-04T09:00:00Z"),
                            {}, now=NOW)
        self.assertEqual((v["badge"], v["plan_label"], v["plan_line"], v["smart_text"]),
                         ("PRO", "Annual", "Annual · renews 4 Oct 2027", "Unlimited"))
        self.assertTrue(v["buttons"]["manage"])
        self.assertFalse(v["buttons"]["upgrade"])
        v = aa.account_view(auth("pro", plan="monthly", period_end="2026-11-01T00:00:00+00:00", cancel=True),
                            {}, now=NOW)
        self.assertEqual(v["plan_line"], "Monthly · ends 1 Nov 2026")

    def test_stripe_trial_uses_status(self):
        a = auth("pro", plan="annual", status="trialing", period_end="2026-10-09T10:00:00Z")
        v = aa.account_view(a, {}, now=NOW)
        self.assertTrue(v["on_trial"])
        self.assertEqual((v["trial_days_left"], v["plan_line"]), (2, "Pro trial · 2 days left"))
        self.assertTrue(v["buttons"]["manage"])             # the trial is cancelled in the portal
        a.period_end = "2026-10-08T00:00:00Z"
        self.assertEqual(aa.account_view(a, {}, now=NOW)["plan_line"], "Pro trial · 1 day left")

    def test_old_in_app_trial_and_admin(self):
        v = aa.account_view(auth("pro", plan="trial", period_end="2026-10-08T12:00:00Z"), {}, now=NOW)
        self.assertEqual(v["plan_line"], "Pro trial · 1 day left")
        self.assertFalse(v["buttons"]["manage"])            # no Stripe customer behind it
        v = aa.account_view(auth("pro", plan="admin", admin=True), {"admin_tier_override": "auto"}, now=NOW)
        self.assertEqual((v["plan_line"], v["is_admin"], v["buttons"]["admin"]), ("Admin", True, True))

    def test_admin_preview_follows_the_forced_tier(self):
        a = auth("pro", plan="admin", admin=True)
        v = aa.account_view(a, {"admin_tier_override": "free"}, smart_remaining=3)
        self.assertEqual((v["pro"], v["badge"], v["admin_tier"], v["smart_text"]), (False, "FREE", "free", "3 of 5 left"))
        # Previewing Guest: a guest can't use Smart Actions until they sign up.
        v = aa.account_view(a, {"admin_tier_override": "guest"}, smart_remaining=3)
        self.assertEqual((v["badge"], v["smart_tries_left"], v["smart_text"]), ("GUEST", None, "Sign up to try them"))

    def test_dates_and_days(self):
        self.assertEqual(aa.format_date("2027-10-04"), "4 Oct 2027")
        self.assertEqual(aa.format_date("not a date"), "")
        self.assertEqual(aa.trial_days_left(SimpleNamespace(period_end=None), NOW), 0)
        self.assertEqual(aa.trial_days_left(SimpleNamespace(period_end="2026-10-01T00:00:00Z"), NOW), 0)


class TestDeleteAccount(unittest.TestCase):
    def test_confirm_word(self):
        self.assertTrue(aa.confirm_delete(" DELETE "))
        self.assertFalse(aa.confirm_delete("delete"))

    def test_posts_with_the_token(self):
        post = MagicMock(return_value=resp(200))
        self.assertEqual(aa.delete_account(auth("free"), post=post), (True, "Your account was deleted."))
        args, kw = post.call_args
        self.assertEqual(args[0], aa.DELETE_ACCOUNT_URL)
        self.assertEqual(kw["json"], {"confirm": "DELETE"})
        self.assertEqual(kw["headers"], {"Authorization": "Bearer tok"})

    def test_failures(self):
        self.assertFalse(aa.delete_account(auth("guest"), post=MagicMock())[0])
        self.assertIn("session expired", aa.delete_account(auth("free", token=None), post=MagicMock())[1])
        self.assertIn("HTTP 500", aa.delete_account(auth("free"), post=MagicMock(return_value=resp(500)))[1])
        self.assertIn("connection", aa.delete_account(auth("free"), post=MagicMock(side_effect=OSError))[1])


class TestFeedback(unittest.TestCase):
    def test_sends_message_version_and_images(self):
        post = MagicMock(return_value=resp(200))
        imgs = aa.encode_images([b"\x89PNG one", b"\x89PNG two"])
        ok, msg = aa.send_feedback(auth("free"), "  Add Welsh  ", imgs, version="2.0.0", post=post)
        self.assertTrue(ok)
        payload = post.call_args[1]["json"]
        self.assertEqual(payload["message"], "Add Welsh")
        self.assertEqual(payload["app_version"], "2.0.0")
        self.assertEqual(payload["category"], "feedback")
        self.assertEqual(len(payload["images"]), 2)
        self.assertEqual(post.call_args[0][0], aa.FEEDBACK_URL)

    def test_checks_and_status_messages(self):
        self.assertEqual(aa.send_feedback(auth("free"), " ", post=MagicMock()), (False, "Type a message first."))
        self.assertFalse(aa.send_feedback(auth("guest"), "hi", post=MagicMock())[0])
        for status, word in ((429, "tomorrow"), (503, "isn't set up"), (401, "expired"), (500, "HTTP 500")):
            with self.subTest(status=status):
                self.assertIn(word, aa.send_feedback(auth("free"), "hi", post=MagicMock(return_value=resp(status)))[1])

    def test_image_limits(self):
        self.assertEqual(aa.image_problem(b"x", already=0), "")
        self.assertIn("up to 4", aa.image_problem(b"x", already=4))
        self.assertIn("too large", aa.image_problem(b"x" * (aa.FEEDBACK_MAX_IMAGE_BYTES + 1)))
        self.assertEqual(len(aa.encode_images([b"a"] * 6)), 4)


def _stubbed_main():
    qtcore = sys.modules.get("PySide6.QtCore")
    if qtcore is None or type(qtcore).__name__ != "StubModule":
        return None
    import main
    return main


class TestParityWithMain(unittest.TestCase):
    def test_urls_match_main(self):
        main = _stubbed_main()
        if main is None:
            self.skipTest("needs test_core's PySide6 stand-ins (run the whole tests/ folder)")
        self.assertEqual(aa.DELETE_ACCOUNT_URL, main.DELETE_ACCOUNT_URL)
        self.assertEqual(aa.FEEDBACK_URL, main.FEEDBACK_URL)
        self.assertEqual(aa.SUPPORT_EMAIL, main.SUPPORT_EMAIL)


if __name__ == "__main__":
    unittest.main()
