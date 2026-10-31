"""Gusan Pro pricing rules shared by the app and (as a spec) the Supabase functions.

The edge functions in supabase/functions implement the same decisions. Tests in
tests/test_pricing_policy.py lock the behaviour: free monthly allowances, the
lifetime cap, and the 12-month commitment cancel rule.

Prices are resolved by Stripe lookup_key. The legacy price ids stay only so a
webhook for an archived price still maps to a plan.
"""

LOOKUP_MONTHLY = "gusan_pro_monthly"
LOOKUP_YEARLY = "gusan_pro_yearly_monthly"
LOOKUP_LIFETIME = "gusan_pro_lifetime"

# Archived prices. Nobody is subscribed. The webhook still understands them.
LEGACY_PRICE_PLANS = {
    "price_1TdvoiCdwD0umc3Ley0fYNFw": "monthly",
    "price_1TdvojCdwD0umc3LoypQVuS0": "annual",
}

LIFETIME_CAP = 200
FREE_MEETING_NOTES_PER_MONTH = 1
FREE_LIVE_SESSIONS_PER_MONTH = 1
COMMITMENT_MONTHS = 12
# Kept on purpose: abuse ceilings, not a product fair-use quota.
# transcribe-proxy rejects a user past DAILY_STT_CAP requests/day.
# smart-action rejects a user past DAILY_ACTION_CAP requests/day.
DAILY_STT_CAP = 3000
DAILY_ACTION_CAP = 1000

PRICE_LINE = "€15.99/mo, €9.99/mo for 12 months, or €199 once"
CHECKOUT_BUTTON = "Continue to checkout"
SIGN_IN_FIRST = "Sign in first, then choose a plan."
MONTHLY_NOTE = "€15.99 a month. Cancel anytime. Secure checkout by Stripe."
YEARLY_NOTE = ("€9.99 a month, billed every month for 12 months. "
               "You can cancel at the end of those 12 months, not before. "
               "Secure checkout by Stripe.")
LIFETIME_NOTE = "€199 once, for the first 200 people. Yours for good. Secure checkout by Stripe."

ALLOWANCE_MEETING_NOTES = "meeting_notes"
ALLOWANCE_LIVE_SESSION = "live_session"


def plan_for_price(price_id=None, lookup_key=None, interval=None, metadata=None):
    """Stripe price -> plan name, or None when it isn't one of ours.

    lookup_key wins. Legacy ids are still recognised. A commitment_months
    metadata value marks the yearly plan even if the lookup key was dropped.
    """
    key = (lookup_key or "").strip()
    if key == LOOKUP_MONTHLY:
        return "monthly"
    if key == LOOKUP_YEARLY:
        return "yearly"
    if key == LOOKUP_LIFETIME:
        return "lifetime"
    if price_id and price_id in LEGACY_PRICE_PLANS:
        return LEGACY_PRICE_PLANS[price_id]
    meta = metadata or {}
    try:
        months = int(str(meta.get("commitment_months") or "0"))
    except (TypeError, ValueError):
        months = 0
    if months >= COMMITMENT_MONTHS:
        return "yearly"
    if interval == "year":
        return "annual"
    if interval == "month":
        return "monthly"
    return None


def is_commitment_plan(plan):
    return plan == "yearly"


def commitment_cancel_override(now, commitment_end, cancel_at, cancel_at_period_end):
    """Where a cancel must land while a 12-month commitment is still running.

    Returns None when nothing should change. Otherwise
    {"cancel_at": commitment_end, "clear_period_end": True}: the subscription
    ends when the commitment ends, not at the next invoice and not today.

    ``now``, ``commitment_end`` and ``cancel_at`` are unix seconds (or None).
    """
    if not commitment_end or now is None or now >= commitment_end:
        return None
    early_at = cancel_at is not None and cancel_at < commitment_end
    if not cancel_at_period_end and not early_at:
        return None
    if not cancel_at_period_end and cancel_at == commitment_end:
        return None
    return {"cancel_at": commitment_end, "clear_period_end": True}


def lifetime_decision(granted, pending_others, already_granted, already_reserved, cap=LIFETIME_CAP):
    """Whether this user may start a lifetime checkout.

    ``granted`` is the number of paid lifetime purchases.
    ``pending_others`` is unexpired reservations held by other people.
    A person who already owns lifetime, or already holds a live reservation,
    does not take a second spot.
    """
    used = int(granted) + int(pending_others)
    if already_granted:
        return {"ok": False, "reason": "already_lifetime", "spots_left": max(0, cap - used)}
    if already_reserved:
        return {"ok": True, "reason": "reserved", "spots_left": max(0, cap - used)}
    if used >= cap:
        return {"ok": False, "reason": "sold_out", "spots_left": 0}
    return {"ok": True, "reason": "reserved", "spots_left": cap - used - 1}


def meeting_note_allowed(is_pro, granted_refs, note_ref, limit=FREE_MEETING_NOTES_PER_MONTH):
    """True when this cloud meeting note may run.

    Pro is unlimited. The same note_ref in the same month (a retry or a
    rewrite of that meeting) does not take a second allowance. A different
    ref is refused once ``limit`` distinct notes have been granted.
    """
    if is_pro:
        return True
    ref = (note_ref or "").strip()
    seen = {str(r) for r in (granted_refs or []) if str(r).strip()}
    if ref and ref in seen:
        return True
    return len(seen) < limit


def live_session_decision(is_pro, open_session_id, sessions_this_month,
                          limit=FREE_LIVE_SESSIONS_PER_MONTH):
    """What begin_live_session should do.

    One session is one Start-to-Stop run of Live Assistant. An open session
    (not ended, not expired) is returned again so a crash can resume it.
    After it ends, a free account cannot start another until next month.
    Pro always starts a new session.
    """
    if is_pro:
        return {"ok": True, "reason": "pro", "resume": False}
    if open_session_id:
        return {"ok": True, "reason": "resume", "resume": True, "session_id": open_session_id}
    if int(sessions_this_month or 0) >= limit:
        return {"ok": False, "reason": "allowance_used", "resume": False}
    return {"ok": True, "reason": "started", "resume": False}


def spots_left(granted, pending, cap=LIFETIME_CAP):
    return max(0, int(cap) - int(granted) - int(pending))
