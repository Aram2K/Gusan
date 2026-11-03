// Stripe Customer Portal for the signed-in user: "Manage subscription" in the
// app opens it already signed in. Card, invoices, and cancellation.
//
// A yearly plan is a 12-month commitment billed monthly. While that commitment
// is still running, the portal session is created with cancellation turned
// off, and action "cancel" schedules the end for the commitment date instead
// of the next invoice. Monthly plans cancel at period end.
//
// Flow: verify the caller's JWT -> their Stripe customer id (profiles, set by
// stripe-webhook; else the Stripe customer with their VERIFIED email, which is
// then remembered) -> a portal session URL for that customer only.
//
// Body: {} or {"action":"portal"} opens the portal.
//       {"action":"cancel"} schedules cancellation (commitment end, or period end).
//
// Secrets: STRIPE_SECRET_KEY (shared with stripe-webhook). SUPABASE_URL,
// SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY are provided by Supabase.
// Optional: BILLING_RETURN_URL (where the portal's "Return" link goes).
//
// Deploy with JWT verification DISABLED - it checks the JWT itself, like the
// other functions.

import { createClient, type SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const STRIPE_KEY = Deno.env.get("STRIPE_SECRET_KEY") ?? "";
const RETURN_URL = Deno.env.get("BILLING_RETURN_URL") ?? "https://aibuben.xyz/transcribe";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

function stripe(path: string, init: RequestInit = {}) {
  return fetch(`https://api.stripe.com/v1/${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${STRIPE_KEY}`, ...(init.headers ?? {}) },
  });
}

async function customerForEmail(email: string): Promise<string | null> {
  for (const candidate of new Set([email, email.toLowerCase()])) {
    const r = await stripe(`customers?email=${encodeURIComponent(candidate)}&limit=1`);
    if (!r.ok) continue;
    const data = await r.json();
    if (data?.data?.[0]?.id) return data.data[0].id;
  }
  return null;
}

type SubRow = {
  id: string;
  plan: string | null;
  commitment_ends_at: string | null;
  stripe_schedule_id: string | null;
  status: string;
  cancels_at: string | null;
};

function commitmentActive(sub: SubRow | null): boolean {
  if (!sub?.commitment_ends_at || sub.plan !== "yearly") return false;
  return new Date(sub.commitment_ends_at).getTime() > Date.now();
}

async function loadSub(svc: SupabaseClient, userId: string): Promise<SubRow | null> {
  const { data } = await svc.from("subscriptions")
    .select("id, plan, commitment_ends_at, stripe_schedule_id, status, cancels_at")
    .eq("user_id", userId)
    .eq("status", "active")
    .order("current_period_end", { ascending: false })
    .limit(1)
    .maybeSingle();
  return (data as SubRow | null) ?? null;
}

let flexibleConfig: string | null = null;
let commitmentConfig: string | null = null;

async function portalConfiguration(kind: "flexible" | "commitment"): Promise<string | null> {
  if (kind === "flexible" && flexibleConfig) return flexibleConfig;
  if (kind === "commitment" && commitmentConfig) return commitmentConfig;
  const body = new URLSearchParams();
  body.set("features[invoice_history][enabled]", "true");
  body.set("features[payment_method_update][enabled]", "true");
  if (kind === "flexible") {
    body.set("features[subscription_cancel][enabled]", "true");
    body.set("features[subscription_cancel][mode]", "at_period_end");
  } else {
    // The app schedules cancellation at the commitment end. The portal must
    // not offer "cancel at the end of this month" or "cancel now".
    body.set("features[subscription_cancel][enabled]", "false");
  }
  const r = await stripe("billing_portal/configurations", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || !data?.id) {
    console.error("portal configuration", r.status, data?.error?.message ?? "");
    return null;
  }
  if (kind === "flexible") flexibleConfig = data.id;
  else commitmentConfig = data.id;
  return data.id;
}

async function scheduleCancel(sub: SubRow): Promise<{ ok: boolean; cancels_at?: string; error?: string }> {
  const committed = commitmentActive(sub);
  if (committed && sub.stripe_schedule_id) {
    const form = new URLSearchParams();
    form.set("end_behavior", "cancel");
    const r = await stripe(`subscription_schedules/${sub.stripe_schedule_id}`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form,
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      console.error("schedule cancel", r.status, data?.error?.message ?? "");
      return { ok: false, error: "cancel_failed" };
    }
  }
  const form = new URLSearchParams();
  if (committed && sub.commitment_ends_at) {
    const unix = Math.floor(new Date(sub.commitment_ends_at).getTime() / 1000);
    form.set("cancel_at", String(unix));
    form.set("cancel_at_period_end", "false");
  } else {
    form.set("cancel_at_period_end", "true");
  }
  const r = await stripe(`subscriptions/${sub.id}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    // A schedule-managed subscription may reject cancel_at. The schedule's
    // end_behavior=cancel above is what actually stops billing at month 12.
    if (committed) {
      return { ok: true, cancels_at: sub.commitment_ends_at ?? undefined };
    }
    console.error("subscription cancel", r.status, data?.error?.message ?? "");
    return { ok: false, error: "cancel_failed" };
  }
  let when: string | undefined;
  if (data.cancel_at) when = new Date(data.cancel_at * 1000).toISOString();
  else if (committed && sub.commitment_ends_at) when = sub.commitment_ends_at;
  else if (data.current_period_end) when = new Date(data.current_period_end * 1000).toISOString();
  return { ok: true, cancels_at: when };
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) return json({ error: "unauthorized" }, 401);
  if (!STRIPE_KEY || !SERVICE_KEY) return json({ error: "server_misconfigured" }, 503);

  const supa = createClient(SUPABASE_URL, ANON_KEY, { global: { headers: { Authorization: authHeader } } });
  const { data: userData, error: userErr } = await supa.auth.getUser();
  if (userErr || !userData?.user) return json({ error: "unauthorized" }, 401);
  const user = userData.user;

  let body: { action?: string } = {};
  try { body = await req.json(); } catch { body = {}; }
  const action = body.action === "cancel" ? "cancel" : "portal";

  const svc = createClient(SUPABASE_URL, SERVICE_KEY);
  const { data: prof } = await svc.from("profiles")
    .select("stripe_customer_id").eq("id", user.id).maybeSingle();
  let customer: string | null = prof?.stripe_customer_id ?? null;

  if (!customer && user.email && user.email_confirmed_at) {
    customer = await customerForEmail(user.email);
    if (customer) {
      await svc.from("profiles").update({ stripe_customer_id: customer }).eq("id", user.id);
    }
  }
  if (!customer) return json({ error: "no_customer" }, 404);

  const sub = await loadSub(svc, user.id);
  const committed = commitmentActive(sub);

  if (action === "cancel") {
    if (!sub) return json({ error: "no_subscription" }, 404);
    const result = await scheduleCancel(sub);
    if (!result.ok) return json({ error: result.error || "cancel_failed" }, 502);
    if (result.cancels_at) {
      await svc.from("subscriptions").update({
        cancels_at: result.cancels_at,
        cancel_at_period_end: !committed,
      }).eq("id", sub.id);
    }
    return json({
      ok: true,
      cancels_at: result.cancels_at ?? null,
      commitment: committed,
    });
  }

  const params = new URLSearchParams({ customer, return_url: RETURN_URL });
  const config = await portalConfiguration(committed ? "commitment" : "flexible");
  if (config) params.set("configuration", config);
  const r = await stripe("billing_portal/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: params,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || !data?.url) {
    console.error("billing portal session failed", r.status, data?.error?.message ?? "");
    return json({ error: "portal_failed" }, 502);
  }
  return json({
    url: data.url,
    commitment: committed,
    commitment_ends_at: committed ? sub?.commitment_ends_at ?? null : null,
  });
});
