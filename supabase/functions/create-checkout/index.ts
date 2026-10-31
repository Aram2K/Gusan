// Creates a Stripe Checkout Session for the signed-in user.
//
// Plans (resolved by lookup_key, never a hard-coded price id):
//   monthly  gusan_pro_monthly          subscription, cancel anytime, no trial
//   yearly   gusan_pro_yearly_monthly   subscription, billed monthly, 12-month
//                                       commitment (the webhook attaches the
//                                       schedule after checkout)
//   lifetime gusan_pro_lifetime         one-time payment, first 200 buyers
//
// The 200 cap is reserved in Postgres before the session is created, so two
// people cannot both pass a count check. A reservation expires (default 60
// minutes) and checkout.session.expired releases it.
//
// Checkout never starts a free trial.
//
// Body: { "plan": "monthly" | "yearly" | "lifetime" }
//       { "action": "spots" }  -> { spots_left } and does not check out
//
// Secrets: STRIPE_SECRET_KEY. SUPABASE_URL, SUPABASE_ANON_KEY are provided.
// Optional: CHECKOUT_SUCCESS_URL, CHECKOUT_CANCEL_URL (else BILLING_RETURN_URL).
// Deploy with JWT verification DISABLED - this function checks the JWT itself.

import Stripe from "https://esm.sh/stripe@16?target=deno";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import {
  checkoutExtras,
  LOOKUP_LIFETIME,
  LOOKUP_MONTHLY,
  LOOKUP_YEARLY,
  missingTermsUrl,
  YEARLY_BEFORE_PAY,
} from "../_shared/pricing.ts";

const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY") ?? "", {
  apiVersion: "2024-06-20",
  httpClient: Stripe.createFetchHttpClient(),
});

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const RETURN = Deno.env.get("BILLING_RETURN_URL") ?? "https://gusan.aibuben.xyz";
const SUCCESS = Deno.env.get("CHECKOUT_SUCCESS_URL") ?? RETURN;
const CANCEL = Deno.env.get("CHECKOUT_CANCEL_URL") ?? RETURN;

const PLANS: Record<string, string> = {
  monthly: LOOKUP_MONTHLY,
  yearly: LOOKUP_YEARLY,
  lifetime: LOOKUP_LIFETIME,
};

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

function stripeMessage(err: unknown): string {
  if (err && typeof err === "object" && "message" in err) return String((err as { message: unknown }).message);
  return String(err ?? "");
}

// Stripe refuses consent_collection.terms_of_service = required until a Terms
// of Service URL is saved under Settings → Public details.
const TERMS_URL_MESSAGE =
  "Stripe needs a Terms of Service URL in the account's public details (https://gusan.aibuben.xyz/terms) before Checkout can require the consent checkbox.";

async function openCheckout(
  supa: ReturnType<typeof createClient>,
  plan: string,
  params: Stripe.Checkout.SessionCreateParams,
): Promise<Stripe.Checkout.Session | Response> {
  try {
    return await stripe.checkout.sessions.create(params);
  } catch (err) {
    const message = stripeMessage(err);
    if (plan === "lifetime") {
      await supa.rpc("release_own_lifetime_reservation").catch(() => {});
    }
    if (missingTermsUrl(message)) {
      console.error("checkout needs a terms of service url", message);
      return json({ error: "terms_url_required", message: TERMS_URL_MESSAGE }, 503);
    }
    console.error("checkout session", message);
    return json({ error: "checkout_failed" }, 502);
  }
}

async function priceForLookup(lookupKey: string): Promise<Stripe.Price> {
  const list = await stripe.prices.list({ lookup_keys: [lookupKey], active: true, limit: 1 });
  const price = list.data[0];
  if (!price) throw new Error(`price_not_found:${lookupKey}`);
  return price;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) return json({ error: "unauthorized" }, 401);
  if (!Deno.env.get("STRIPE_SECRET_KEY") || !ANON_KEY) return json({ error: "server_misconfigured" }, 503);

  const supa = createClient(SUPABASE_URL, ANON_KEY, { global: { headers: { Authorization: authHeader } } });
  const { data: userData, error: userErr } = await supa.auth.getUser();
  if (userErr || !userData?.user) return json({ error: "unauthorized" }, 401);
  const user = userData.user;

  let body: { plan?: string; action?: string } = {};
  try { body = await req.json(); } catch { body = {}; }

  if (body.action === "spots") {
    const { data, error } = await supa.rpc("lifetime_spots_left");
    if (error) return json({ error: "spots_failed" }, 500);
    return json({ spots_left: data });
  }

  const plan = String(body.plan || "");
  const lookup = PLANS[plan];
  if (!lookup) return json({ error: "unknown_plan" }, 400);

  let price: Stripe.Price;
  try {
    price = await priceForLookup(lookup);
  } catch (err) {
    console.error("price lookup", (err as Error).message);
    return json({ error: "price_not_found" }, 503);
  }

  const { data: prof } = await supa.from("profiles").select("stripe_customer_id").eq("id", user.id).maybeSingle();
  const customer = prof?.stripe_customer_id ?? undefined;

  if (plan === "lifetime") {
    const { data: reserved, error: reserveErr } = await supa.rpc("reserve_lifetime_spot", { hold_minutes: 60 });
    if (reserveErr) return json({ error: "reserve_failed" }, 500);
    const decision = reserved as { ok?: boolean; reason?: string; spots_left?: number } | null;
    if (!decision?.ok) {
      const status = decision?.reason === "sold_out" ? 409 : 409;
      return json({
        error: decision?.reason || "sold_out",
        spots_left: decision?.spots_left ?? 0,
      }, status);
    }
    const opened = await openCheckout(supa, plan, {
      mode: "payment",
      customer: customer || undefined,
      customer_email: customer ? undefined : (user.email ?? undefined),
      client_reference_id: user.id,
      line_items: [{ price: price.id, quantity: 1 }],
      success_url: SUCCESS,
      cancel_url: CANCEL,
      metadata: { user_id: user.id, lookup_key: lookup, plan: "lifetime" },
      ...checkoutExtras(plan),
    });
    if (opened instanceof Response) return opened;
    await supa.rpc("attach_lifetime_checkout", { p_session_id: opened.id });
    return json({ url: opened.url, spots_left: decision.spots_left ?? null, plan: "lifetime" });
  }

  const opened = await openCheckout(supa, plan, {
    mode: "subscription",
    customer: customer || undefined,
    customer_email: customer ? undefined : (user.email ?? undefined),
    client_reference_id: user.id,
    line_items: [{ price: price.id, quantity: 1 }],
    success_url: SUCCESS,
    cancel_url: CANCEL,
    // No trial. A card is collected up front.
    payment_method_collection: "always",
    metadata: { user_id: user.id, lookup_key: lookup, plan },
    subscription_data: {
      // Invoice description. For yearly this is the same commitment sentence
      // as custom_text.submit (end_behavior "release": billing continues).
      ...(plan === "yearly" ? { description: YEARLY_BEFORE_PAY } : {}),
      metadata: {
        user_id: user.id,
        lookup_key: lookup,
        plan,
        ...(plan === "yearly" ? { commitment_months: "12" } : {}),
      },
    },
    ...checkoutExtras(plan),
  });
  if (opened instanceof Response) return opened;
  return json({ url: opened.url, plan });
});
