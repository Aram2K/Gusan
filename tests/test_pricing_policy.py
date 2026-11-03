"""Gusan Pro pricing: free allowances, the lifetime cap, the 12-month commitment,
and the checkout client. The SQL migration and the edge functions follow the
same rules (pricing_policy.py / supabase/functions/_shared/pricing.ts)."""
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import pricing_policy as pp
import quotas


ROOT = Path(__file__).resolve().parents[1]


def _resp(status, payload):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = ""
    return r


class TestPlanResolution(unittest.TestCase):
    def test_lookup_keys_win_over_legacy_ids(self):
        self.assertEqual(pp.plan_for_price(lookup_key=pp.LOOKUP_MONTHLY), "monthly")
        self.assertEqual(pp.plan_for_price(lookup_key=pp.LOOKUP_YEARLY), "yearly")
        self.assertEqual(pp.plan_for_price(lookup_key=pp.LOOKUP_LIFETIME), "lifetime")
        # A new price that reused an old id would still follow the lookup key.
        self.assertEqual(
            pp.plan_for_price("price_1TdvoiCdwD0umc3Ley0fYNFw", lookup_key=pp.LOOKUP_YEARLY),
            "yearly")

    def test_archived_prices_still_map(self):
        self.assertEqual(pp.plan_for_price("price_1TdvoiCdwD0umc3Ley0fYNFw"), "monthly")
        self.assertEqual(pp.plan_for_price("price_1TdvojCdwD0umc3LoypQVuS0"), "annual")
        self.assertIsNone(pp.plan_for_price("price_unknown"))

    def test_commitment_metadata_marks_the_yearly_plan(self):
        self.assertEqual(pp.plan_for_price(interval="month", metadata={"commitment_months": "12"}), "yearly")
        self.assertTrue(pp.is_commitment_plan("yearly"))
        self.assertFalse(pp.is_commitment_plan("monthly"))


class TestCommitmentCancel(unittest.TestCase):
    END = 1_800_000_000
    NOW = END - 100 * 86400

    def test_cancel_at_period_end_is_pushed_to_the_commitment(self):
        got = pp.commitment_cancel_override(self.NOW, self.END, None, True)
        self.assertEqual(got, {"cancel_at": self.END, "clear_period_end": True})

    def test_an_earlier_cancel_at_is_pushed_out(self):
        got = pp.commitment_cancel_override(self.NOW, self.END, self.NOW + 86400, False)
        self.assertEqual(got["cancel_at"], self.END)

    def test_already_at_the_commitment_end_is_left_alone(self):
        self.assertIsNone(pp.commitment_cancel_override(self.NOW, self.END, self.END, False))

    def test_after_the_commitment_a_normal_cancel_stands(self):
        self.assertIsNone(pp.commitment_cancel_override(self.END + 10, self.END, self.END + 86400, True))

    def test_no_cancel_request_does_nothing(self):
        self.assertIsNone(pp.commitment_cancel_override(self.NOW, self.END, None, False))


class TestLifetimeCap(unittest.TestCase):
    def test_the_200th_buyer_is_the_last(self):
        # 199 granted, this person is new: they may buy, and none are left after them.
        got = pp.lifetime_decision(199, 0, False, False)
        self.assertEqual(got, {"ok": True, "reason": "reserved", "spots_left": 0})

    def test_a_pending_reservation_counts_toward_the_cap(self):
        got = pp.lifetime_decision(199, 1, False, False)
        self.assertEqual(got, {"ok": False, "reason": "sold_out", "spots_left": 0})

    def test_an_existing_reservation_does_not_take_a_second_spot(self):
        got = pp.lifetime_decision(10, 0, False, True)
        self.assertTrue(got["ok"])
        self.assertEqual(got["spots_left"], 190)

    def test_someone_who_already_owns_lifetime_is_refused(self):
        got = pp.lifetime_decision(50, 0, True, False)
        self.assertEqual(got["reason"], "already_lifetime")
        self.assertFalse(got["ok"])

    def test_spots_left(self):
        self.assertEqual(pp.spots_left(10, 3), 187)
        self.assertEqual(pp.spots_left(200, 0), 0)


class TestFreeAllowances(unittest.TestCase):
    def test_one_cloud_meeting_note_a_month_and_a_retry_is_free(self):
        self.assertTrue(pp.meeting_note_allowed(False, [], "meet-1"))
        self.assertTrue(pp.meeting_note_allowed(False, ["meet-1"], "meet-1"))
        self.assertFalse(pp.meeting_note_allowed(False, ["meet-1"], "meet-2"))
        self.assertTrue(pp.meeting_note_allowed(True, ["meet-1"], "meet-2"))

    def test_one_live_session_resumes_and_then_blocks(self):
        started = pp.live_session_decision(False, None, 0)
        self.assertTrue(started["ok"])
        self.assertEqual(started["reason"], "started")
        resumed = pp.live_session_decision(False, "sess-1", 1)
        self.assertEqual(resumed, {"ok": True, "reason": "resume", "resume": True, "session_id": "sess-1"})
        blocked = pp.live_session_decision(False, None, 1)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "allowance_used")
        self.assertTrue(pp.live_session_decision(True, None, 40)["ok"])


class TestCheckoutConsent(unittest.TestCase):
    def test_every_plan_requires_the_withdrawal_checkbox(self):
        for plan in ("monthly", "yearly", "lifetime"):
            extra = pp.checkout_extras(plan)
            self.assertEqual(extra["consent_collection"], {"terms_of_service": "required"})
            message = extra["custom_text"]["terms_of_service_acceptance"]["message"]
            self.assertEqual(message, pp.WITHDRAWAL_CONSENT)
            self.assertIn("https://gusan.aibuben.xyz/terms", message)
            self.assertIn("14-day right of withdrawal", message)
            self.assertIn("start immediately", message)
            self.assertNotIn("trial", message.lower())

    def test_yearly_states_the_commitment_before_payment(self):
        extra = pp.checkout_extras("yearly")
        submit = extra["custom_text"]["submit"]["message"]
        self.assertEqual(submit, pp.YEARLY_BEFORE_PAY)
        self.assertIn("12-month commitment", submit)
        self.assertIn("€9.99", submit)
        self.assertIn("€119.88", submit)
        self.assertIn("until you cancel", submit)
        # end_behavior "release": it keeps billing; it does not stop after month 12.
        self.assertIn("billing continues", submit)
        self.assertNotIn("submit", pp.checkout_extras("monthly")["custom_text"])
        self.assertNotIn("submit", pp.checkout_extras("lifetime")["custom_text"])

    def test_the_upgrade_window_shows_the_same_wording(self):
        self.assertIn(pp.YEARLY_BEFORE_PAY, pp.YEARLY_NOTE)
        self.assertIn(pp.WITHDRAWAL_CONSENT, pp.YEARLY_NOTE)
        self.assertIn(pp.WITHDRAWAL_CONSENT, pp.LIFETIME_NOTE)
        self.assertIn("One-time digital content", pp.LIFETIME_NOTE)
        self.assertNotIn("free", pp.YEARLY_NOTE.lower())

    def test_a_missing_terms_url_is_recognised(self):
        stripe = ("You must save a Terms of Service URL in your account settings "
                  "before requiring customers to accept the terms of service.")
        self.assertTrue(pp.missing_terms_url(stripe))
        self.assertFalse(pp.missing_terms_url("No such price"))
        self.assertFalse(pp.missing_terms_url("card was declined"))

    def test_the_edge_function_sends_those_params(self):
        shared = (ROOT / "supabase/functions/_shared/pricing.ts").read_text(encoding="utf-8")
        src = (ROOT / "supabase/functions/create-checkout/index.ts").read_text(encoding="utf-8")
        self.assertIn(pp.WITHDRAWAL_CONSENT, shared)
        self.assertIn(pp.YEARLY_BEFORE_PAY, shared)
        self.assertIn('terms_of_service: "required"', shared)
        self.assertIn("checkoutExtras(plan)", src)
        self.assertIn("terms_url_required", src)
        self.assertIn("release_own_lifetime_reservation", src)
        self.assertIn("description: YEARLY_BEFORE_PAY", src)
        sql = (ROOT / "supabase/migrations/202610300001_gusan_pro_pricing.sql").read_text(encoding="utf-8")
        self.assertIn("release_own_lifetime_reservation", sql)


class TestCheckoutClient(unittest.TestCase):
    def test_checkout_refuses_anything_but_stripe_and_reports_the_cap(self):
        post = MagicMock(return_value=_resp(200, {"url": "https://checkout.stripe.com/c/pay/cs_test"}))
        url, problem, info = quotas.create_checkout("tok", "yearly", post=post, url="https://example.test/checkout")
        self.assertEqual((url, problem), ("https://checkout.stripe.com/c/pay/cs_test", ""))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body, {"plan": "yearly"})
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer tok")

        post = MagicMock(return_value=_resp(200, {"url": "https://evil.example/pay"}))
        self.assertEqual(quotas.create_checkout("tok", "monthly", post=post, url="https://example.test/checkout")[1],
                         "unavailable")

        post = MagicMock(return_value=_resp(409, {"error": "sold_out", "spots_left": 0}))
        url, problem, info = quotas.create_checkout("tok", "lifetime", post=post, url="https://example.test/checkout")
        self.assertEqual((url, problem, info.get("spots_left")), (None, "sold_out", 0))

        post = MagicMock(return_value=_resp(503, {
            "error": "terms_url_required",
            "message": "Stripe needs a Terms of Service URL",
        }))
        url, problem, info = quotas.create_checkout("tok", "monthly", post=post, url="https://example.test/checkout")
        self.assertEqual(url, None)
        self.assertEqual(problem, "terms_url_required")
        self.assertIn("Terms of Service URL", info["message"])

    def test_allowance_status(self):
        self.assertEqual(quotas.allowance_from_status(402, {"error": "allowance_used", "allowance": "meeting_notes"}),
                         "meeting_notes")
        self.assertEqual(quotas.allowance_from_status(402, {"error": "allowance_used", "allowance": "live_session"}),
                         "live_session")
        self.assertIsNone(quotas.allowance_from_status(429, {"error": "quota_exceeded"}))

    def test_begin_live_session_reads_the_rpc(self):
        post = MagicMock(return_value=_resp(200, {"ok": True, "reason": "started", "session_id": "abc"}))
        got = quotas.begin_live_session("tok", post=post)
        self.assertEqual(got["session_id"], "abc")
        self.assertTrue(got["ok"])
        self.assertIn("/rpc/begin_live_session", post.call_args.args[0])


class TestServerSources(unittest.TestCase):
    def test_is_pro_does_not_grant_a_trial(self):
        sql = (ROOT / "supabase/migrations/202610300001_gusan_pro_pricing.sql").read_text(encoding="utf-8")
        start = sql.index("create or replace function public.is_pro()")
        end = sql.index("revoke all on function public.is_pro()")
        body = sql[start:end]
        self.assertNotIn("trialing", body)
        self.assertNotIn("pro_trial_ends_at", body)
        self.assertIn("lifetime_purchases", body)
        self.assertIn("status = 'active'", body)

    def test_allowances_and_the_cap_are_in_the_migration(self):
        sql = (ROOT / "supabase/migrations/202610300001_gusan_pro_pricing.sql").read_text(encoding="utf-8")
        self.assertIn("consume_meeting_note", sql)
        self.assertIn("begin_live_session", sql)
        self.assertIn("if n >= 1 then", sql)
        self.assertIn("200", sql)
        self.assertIn("reserve_lifetime_spot", sql)
        self.assertIn("grant_lifetime_purchase", sql)

    def test_checkout_has_no_trial_and_uses_lookup_keys(self):
        src = (ROOT / "supabase/functions/create-checkout/index.ts").read_text(encoding="utf-8")
        self.assertNotIn("trial_period_days", src)
        self.assertNotIn("price_1UWPf", src)
        self.assertIn("LOOKUP_MONTHLY", src)
        self.assertIn("LOOKUP_YEARLY", src)
        self.assertIn("LOOKUP_LIFETIME", src)
        self.assertIn("reserve_lifetime_spot", src)
        self.assertIn('mode: "payment"', src)
        self.assertIn('mode: "subscription"', src)

    def test_webhook_handles_commitment_lifetime_and_old_prices(self):
        src = (ROOT / "supabase/functions/stripe-webhook/index.ts").read_text(encoding="utf-8")
        shared = (ROOT / "supabase/functions/_shared/pricing.ts").read_text(encoding="utf-8")
        self.assertIn("subscriptionSchedules", src)
        self.assertIn("grant_lifetime_purchase", src)
        self.assertIn("release_lifetime_reservation", src)
        self.assertIn("commitmentCancelOverride", src)
        self.assertIn("iterations: COMMITMENT_MONTHS", src)
        self.assertIn("price_1TdvoiCdwD0umc3Ley0fYNFw", shared)
        self.assertIn("price_1TdvojCdwD0umc3LoypQVuS0", shared)
        self.assertIn('end_behavior: "cancel"', src)

    def test_pro_keeps_the_daily_abuse_caps_and_not_a_minute_cap(self):
        proxy = (ROOT / "supabase/functions/transcribe-proxy/index.ts").read_text(encoding="utf-8")
        action = (ROOT / "supabase/functions/smart-action/index.ts").read_text(encoding="utf-8")
        self.assertIn("const DAILY_CAP = 3000", proxy)
        self.assertNotIn("use_cloud_seconds", proxy)
        self.assertIn("const DAILY_CAP = 1000", action)
        self.assertIn("allowance_used", action)
        self.assertIn("consume_meeting_note", action)
        self.assertIn("live_session_open", action)

    def test_the_app_does_not_offer_a_trial_or_the_old_prices(self):
        dialog = (ROOT / "ui/pro_dialog.py").read_text(encoding="utf-8")
        self.assertNotIn("3-day", dialog)
        self.assertNotIn("7.99", dialog)
        self.assertNotIn("€59", dialog)
        self.assertIn("12-month commitment", dialog)
        self.assertIn("€15.99", dialog)
        self.assertIn("€199", dialog)
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("buy.stripe.com", main)
        self.assertNotIn("3-day", main)


if __name__ == "__main__":
    unittest.main()
