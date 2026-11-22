// Stripe webhook → Supabase entitlement sync.
//
// Security model:
//   * Verifies the Stripe signature with STRIPE_WEBHOOK_SECRET (raw body).
//   * Writes to the `subscriptions` and `lifetime_purchases` tables with the
//     SERVICE ROLE key (bypasses RLS) - the only path allowed to grant Pro.
//     The client never can.
//   * Links a Stripe customer to a Supabase user by `client_reference_id`
//     (the app passes the signed-in user's id into Checkout), falling back to
//     matching the email on the `profiles` row.
//
// Plans are resolved by price lookup_key (gusan_pro_monthly,
// gusan_pro_yearly_monthly, gusan_pro_lifetime). Archived prices
// price_1Tdvoi… / price_1Tdvoj… still map, so an old event does not 500.
//
// The yearly price is billed monthly. After checkout this handler turns that
// subscription into a 12-iteration schedule. A cancel that would take effect
// before the commitment ends is moved to the commitment end.
//
// A lifetime Checkout (mode=payment) grants permanent Pro once it is paid:
// checkout.session.completed with payment_status "paid", or, for a payment
// method that settles later, checkout.session.async_payment_succeeded.
// checkout.session.expired and checkout.session.async_payment_failed release
// the reservation that was holding one of the 200 spots. A lifetime payment
// refunded in full (charge.refunded) or disputed (charge.dispute.created) is
// no longer Pro: revoke_lifetime_purchase, found by its payment intent (kept at
// grant time), or by the Checkout Session that took it for older purchases.
//
// Events to send to this endpoint (Stripe dashboard → Developers → Webhooks):
//   checkout.session.completed, checkout.session.expired,
//   checkout.session.async_payment_succeeded, checkout.session.async_payment_failed,
//   customer.subscription.created / .updated / .deleted,
//   charge.refunded, charge.dispute.created.
//
// There is no trial: checkout is created without trial_period_days, and
// is_pro() does not treat status "trialing" as access.
//
// Required secrets (set in Supabase → Edge Functions → Secrets):
//   STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
// Auto-provided by Supabase: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
//
// Deploy with JWT verification DISABLED (Stripe can't send a Supabase JWT).

import Stripe from "https://esm.sh/stripe@16?target=deno";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import {
  COMMITMENT_MONTHS,
  commitmentCancelOverride,
  isCommitmentPlan,
  LOOKUP_LIFETIME,
  planForPrice,
  type PriceLike,
} from "../_shared/pricing.ts";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY") ?? "", {
  apiVersion: "2024-06-20",
  httpClient: Stripe.createFetchHttpClient(),
});
const webhookSecret = Deno.env.get("STRIPE_WEBHOOK_SECRET") ?? "";
const supabase = createClient(
  Deno.env.get("SUPABASE_URL") ?? "",
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "",
);

function planFor(price: PriceLike | null | undefined): string | null {
  return planForPrice(price);
}

async function userIdForEmail(email?: string | null): Promise<string | null> {
  if (!email) return null;
  const { data } = await supabase
    .from("profiles")
    .select("id")
    .ilike("email", email)
    .maybeSingle();
  return data?.id ?? null;
}

async function rememberCustomer(userId: string, customer: string | null) {
  if (!customer) return;
  await supabase.from("profiles").update({ stripe_customer_id: customer }).eq("id", userId);
}

async function upsertSubscription(sub: Stripe.Subscription, userId: string, extra: Record<string, unknown> = {}) {
  const item = sub.items.data[0];
  const price = item?.price as (Stripe.Price & PriceLike) | undefined;
  const priceId = price?.id ?? null;
  const interval = price?.recurring?.interval ?? null;
  const periodEndUnix = (item as { current_period_end?: number } | undefined)?.current_period_end
    ?? (sub as unknown as { current_period_end?: number }).current_period_end
    ?? null;
  const metaEnd = Number(sub.metadata?.commitment_end || "");
  const scheduleId = typeof sub.schedule === "string"
    ? sub.schedule
    : (sub.schedule as { id?: string } | null)?.id ?? null;

  const row: Record<string, unknown> = {
    id: sub.id,
    user_id: userId,
    status: sub.status,
    price_id: priceId,
    plan: planFor(price) ?? (interval === "year" ? "annual" : interval === "month" ? "monthly" : null),
    current_period_end: periodEndUnix ? new Date(periodEndUnix * 1000).toISOString() : null,
    cancel_at_period_end: sub.cancel_at_period_end,
    updated_at: new Date().toISOString(),
    ...extra,
  };
  if (scheduleId) row.stripe_schedule_id = scheduleId;
  if (Number.isFinite(metaEnd) && metaEnd > 0 && row.commitment_ends_at == null) {
    row.commitment_ends_at = new Date(metaEnd * 1000).toISOString();
  }
  if (sub.cancel_at) {
    row.cancels_at = new Date(sub.cancel_at * 1000).toISOString();
  } else if (!extra.cancels_at) {
    row.cancels_at = null;
  }

  await supabase.from("subscriptions").upsert(row);
  if (sub.customer) await rememberCustomer(userId, String(sub.customer));
}

async function resolveUserId(customerId: string): Promise<string | null> {
  const { data } = await supabase
    .from("profiles")
    .select("id")
    .eq("stripe_customer_id", customerId)
    .maybeSingle();
  if (data?.id) return data.id;
  try {
    const cust = await stripe.customers.retrieve(customerId);
    return await userIdForEmail((cust as Stripe.Customer).email);
  } catch {
    return null;
  }
}

async function loadSubscription(id: string): Promise<Stripe.Subscription> {
  return await stripe.subscriptions.retrieve(id, { expand: ["items.data.price"] });
}

async function priceFromSession(session: Stripe.Checkout.Session): Promise<PriceLike | null> {
  const listed = await stripe.checkout.sessions.listLineItems(session.id, {
    limit: 1,
    expand: ["data.price"],
  });
  const price = listed.data[0]?.price as Stripe.Price | string | null | undefined;
  if (price && typeof price !== "string") return price as PriceLike;
  if (typeof price === "string") return await stripe.prices.retrieve(price) as PriceLike;
  return null;
}

const paymentIntentOf = (pi: string | { id?: string } | null | undefined): string | null =>
  typeof pi === "string" ? pi : pi?.id ?? null;

async function grantLifetime(userId: string, sessionId: string, paymentIntent: string | null) {
  const { error } = await supabase.rpc("grant_lifetime_purchase", {
    p_user_id: userId,
    p_session_id: sessionId,
    p_payment_intent: paymentIntent,
  });
  if (error) throw new Error(error.message);
}

async function userForSession(session: Stripe.Checkout.Session): Promise<string | null> {
  const userId = session.client_reference_id ?? session.metadata?.user_id ?? null;
  if (userId) return userId;
  return await userIdForEmail(session.customer_details?.email ?? session.customer_email ?? null);
}

/** Lifetime only when the money is in: a delayed payment method grants on async_payment_succeeded. */
async function grantLifetimeIfPaid(session: Stripe.Checkout.Session, userId: string, price: PriceLike | null) {
  if (planFor(price) !== "lifetime" && price?.lookup_key !== LOOKUP_LIFETIME) return;
  if (session.payment_status !== "paid") {
    console.log("lifetime checkout not paid yet", session.payment_status);
    return;
  }
  await grantLifetime(userId, session.id, paymentIntentOf(session.payment_intent as string | { id?: string } | null));
}

/** A lifetime payment refunded in full or disputed: that purchase is released. */
async function revokeLifetime(paymentIntent: string | null) {
  if (!paymentIntent) return;
  const { data, error } = await supabase.rpc("revoke_lifetime_purchase", { p_session_id: null, p_payment_intent: paymentIntent });
  if (error) throw new Error(error.message);
  if (data) return;
  // Bought before the payment intent was kept: the Checkout Session that took this payment.
  const sessions = await stripe.checkout.sessions.list({ payment_intent: paymentIntent, limit: 1 });
  const sessionId = sessions.data[0]?.id;
  if (!sessionId) return;
  const { error: bySession } = await supabase.rpc("revoke_lifetime_purchase", { p_session_id: sessionId, p_payment_intent: paymentIntent });
  if (bySession) throw new Error(bySession.message);
}

async function releaseLifetime(sessionId: string) {
  await supabase.rpc("release_lifetime_reservation", { p_session_id: sessionId });
}

function commitmentEndFromSchedule(schedule: Stripe.SubscriptionSchedule): number | null {
  const phase = schedule.phases?.[0];
  return phase?.end_date ?? null;
}

function phaseCoversCommitment(phase: { start_date: number; end_date: number | null } | undefined): boolean {
  if (!phase?.start_date || !phase.end_date) return false;
  // A single monthly invoice is ~31 days. Twelve iterations are ~360 days.
  return phase.end_date - phase.start_date >= 330 * 24 * 3600;
}

async function attachCommitment(sub: Stripe.Subscription, priceId: string): Promise<{ scheduleId: string; end: number } | null> {
  let scheduleId = typeof sub.schedule === "string" ? sub.schedule : sub.schedule?.id ?? null;
  let schedule: Stripe.SubscriptionSchedule;
  if (!scheduleId) {
    try {
      schedule = await stripe.subscriptionSchedules.create({ from_subscription: sub.id });
    } catch (err) {
      const fresh = await loadSubscription(sub.id);
      const existing = typeof fresh.schedule === "string" ? fresh.schedule : fresh.schedule?.id ?? null;
      if (!existing) throw err;
      schedule = await stripe.subscriptionSchedules.retrieve(existing);
    }
    scheduleId = schedule.id;
  } else {
    schedule = await stripe.subscriptionSchedules.retrieve(scheduleId);
  }
  const start = schedule.phases?.[0]?.start_date;
  if (!start) return null;
  // from_subscription copies one billing period. Stretch it to 12 iterations
  // unless a previous delivery already did (webhook retries).
  // "release" is the default: when the 12th month ends, the subscription leaves
  // the schedule and keeps billing €9.99 every month until the customer cancels.
  // "cancel" is only set later, when they ask to stop at the end of the commitment.
  if (!phaseCoversCommitment(schedule.phases?.[0])) {
    schedule = await stripe.subscriptionSchedules.update(scheduleId, {
      end_behavior: schedule.end_behavior === "cancel" ? "cancel" : "release",
      phases: [{
        items: [{ price: priceId, quantity: 1 }],
        start_date: start,
        iterations: COMMITMENT_MONTHS,
        proration_behavior: "none",
      }],
    });
  }
  const end = commitmentEndFromSchedule(schedule);
  if (!end) return null;
  try {
    await stripe.subscriptions.update(sub.id, {
      metadata: { ...sub.metadata, commitment_end: String(end), commitment_months: String(COMMITMENT_MONTHS) },
    });
  } catch (err) {
    console.error("commitment metadata", (err as Error).message);
  }
  return { scheduleId, end };
}

async function enforceCommitment(sub: Stripe.Subscription, commitmentEnd: number | null) {
  const now = Math.floor(Date.now() / 1000);
  const override = commitmentCancelOverride(now, commitmentEnd, sub.cancel_at ?? null, sub.cancel_at_period_end);
  if (!override || !commitmentEnd) return sub;
  const scheduleId = typeof sub.schedule === "string" ? sub.schedule : sub.schedule?.id ?? null;
  if (scheduleId) {
    try {
      await stripe.subscriptionSchedules.update(scheduleId, { end_behavior: "cancel" });
    } catch (err) {
      console.error("schedule end_behavior", (err as Error).message);
    }
  }
  try {
    sub = await stripe.subscriptions.update(sub.id, {
      cancel_at_period_end: false,
      cancel_at: override.cancelAt,
    });
  } catch (err) {
    console.error("push cancel to commitment end", (err as Error).message);
  }
  return sub;
}

Deno.serve(async (req) => {
  const sig = req.headers.get("stripe-signature");
  const body = await req.text();

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(body, sig ?? "", webhookSecret);
  } catch (err) {
    return new Response(`Signature verification failed: ${(err as Error).message}`, { status: 400 });
  }

  try {
    switch (event.type) {
      case "checkout.session.completed": {
        const session = event.data.object as Stripe.Checkout.Session;
        const userId = await userForSession(session);
        if (session.customer && userId) await rememberCustomer(userId, String(session.customer));
        const price = await priceFromSession(session);
        if (userId) await grantLifetimeIfPaid(session, userId, price);
        if (userId && session.subscription) {
          let sub = await loadSubscription(String(session.subscription));
          const subPrice = sub.items.data[0]?.price as PriceLike | undefined;
          const subPlan = planFor(subPrice);
          let extra: Record<string, unknown> = {};
          if (isCommitmentPlan(subPlan) && subPrice?.id) {
            const attached = await attachCommitment(sub, subPrice.id);
            if (attached) {
              extra = {
                commitment_ends_at: new Date(attached.end * 1000).toISOString(),
                stripe_schedule_id: attached.scheduleId,
                plan: "yearly",
              };
              sub = await loadSubscription(sub.id);
            }
          }
          await upsertSubscription(sub, userId, extra);
        }
        break;
      }
      case "checkout.session.async_payment_succeeded": {
        const session = event.data.object as Stripe.Checkout.Session;
        const userId = await userForSession(session);
        if (userId) await grantLifetimeIfPaid(session, userId, await priceFromSession(session));
        break;
      }
      case "checkout.session.expired":
      case "checkout.session.async_payment_failed": {
        const session = event.data.object as Stripe.Checkout.Session;
        await releaseLifetime(session.id);
        break;
      }
      case "charge.refunded": {
        const charge = event.data.object as Stripe.Charge;
        // Only a full refund; a partial one leaves the purchase as it is.
        if (charge.refunded) await revokeLifetime(paymentIntentOf(charge.payment_intent as string | { id?: string } | null));
        break;
      }
      case "charge.dispute.created": {
        const dispute = event.data.object as Stripe.Dispute;
        let paymentIntent = paymentIntentOf(dispute.payment_intent as string | { id?: string } | null);
        if (!paymentIntent && dispute.charge) {
          const charge = await stripe.charges.retrieve(typeof dispute.charge === "string" ? dispute.charge : dispute.charge.id);
          paymentIntent = paymentIntentOf(charge.payment_intent as string | { id?: string } | null);
        }
        await revokeLifetime(paymentIntent);
        break;
      }
      case "customer.subscription.created":
      case "customer.subscription.updated":
      case "customer.subscription.deleted": {
        let sub = event.data.object as Stripe.Subscription;
        if (!sub.items?.data?.[0]?.price || typeof sub.items.data[0].price === "string") {
          sub = await loadSubscription(sub.id);
        }
        const price = sub.items.data[0]?.price as PriceLike | undefined;
        const plan = planFor(price);
        const metaEnd = Number(sub.metadata?.commitment_end || "");
        let commitmentEnd = Number.isFinite(metaEnd) && metaEnd > 0 ? metaEnd : null;
        let extra: Record<string, unknown> = {};
        if (isCommitmentPlan(plan) && price?.id && sub.status !== "canceled") {
          if (!commitmentEnd) {
            const attached = await attachCommitment(sub, price.id);
            if (attached) {
              commitmentEnd = attached.end;
              extra.stripe_schedule_id = attached.scheduleId;
              sub = await loadSubscription(sub.id);
            }
          }
          if (commitmentEnd) {
            extra.commitment_ends_at = new Date(commitmentEnd * 1000).toISOString();
            extra.plan = "yearly";
            const before = sub.cancel_at;
            sub = await enforceCommitment(sub, commitmentEnd);
            if ((sub.cancel_at ?? null) !== (before ?? null) || sub.cancel_at) {
              extra.cancels_at = sub.cancel_at ? new Date(sub.cancel_at * 1000).toISOString() : null;
            }
          }
        }
        const userId = await resolveUserId(String(sub.customer));
        if (userId) await upsertSubscription(sub, userId, extra);
        break;
      }
      default:
        break;
    }
    return new Response(JSON.stringify({ received: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err) {
    return new Response(`Handler error: ${(err as Error).message}`, { status: 500 });
  }
});
