// Invites: every account's personal invite code and link, a friend joining
// with it (the friend gets 14 days of Pro and counts at once), and the
// inviter's rewards (1 month of Pro for every 3 friends who join, at most 12 a
// year). The rules live in SQL (supabase/migrations/202611180001_invites.sql
// and 202611200001_invites_hardening.sql); this function checks who is asking,
// turns IP addresses and device ids into keyed hashes, calls the SQL with the
// service role and the user id it checked (clients cannot call those functions
// themselves), and gives paying subscribers their month as a Stripe
// customer-balance credit.
//
// POST JSON {action, ...}. Signed in (Authorization: Bearer <user JWT>):
//   me       {device?}            -> {code, link, first_name, counted, joined_total,
//                                    rewards_left_this_year, friends, rewards, gift,
//                                    invited_by, can_join}
//                                    (rewards[].status: ready | active | used | credited | crediting)
//   hint     {}                   -> {found, name?, code?}  (the caller's network)
//   join     {code, device, source: "hint"|"paste"}
//                                 -> {ok:true, name, gift_ends_at} | {ok:false, reason: bad_code |
//                                    own_code | already_joined | not_confirmed | disposable_email |
//                                    same_computer | seen_before | same_network | limit}
//   claim    {id}                 -> {ok:true, status:"active", starts_at, ends_at}
//                                    | {ok:true, status:"next_bill_free", amount, currency}
//                                    | {ok:false, reason: not_found | already_claimed |
//                                       not_eligible (lifetime or admin) | stripe_error}
// No sign-in, for the website worker (gusan.aibuben.xyz/i/<code>):
//   info     {code}               -> {valid, name?}   (limited per network)
//   visit    {code, ip}           -> {ok}  only with header x-gusan-worker = INVITE_WORKER_KEY
//
// A code may arrive as the full link. "device" is the app's sha256 hex of the
// computer's id; it is hashed again here, with the IP addresses, as
// HMAC-SHA256 keyed with INVITE_SALT. A "network" is an IPv4 address, or the
// /64 of an IPv6 one (normalizeIp): hints, joins (at most 2 from one network
// in 30 days) and the per-address limits all use it. Nothing raw is stored or
// logged: one log line per call, the action and its outcome.
//
// Claiming with a live Stripe subscription: SQL marks the reward 'crediting'
// (a second claim is already_claimed), this function reads the subscription,
// works out one MONTH of its price (yearly / 12, every-n-months / n; daily or
// weekly prices are refused), reuses a balance transaction that already
// carries this reward's id (metadata grant_id) or creates one (Idempotency-Key
// = the reward's id), then marks it 'credited'. If Stripe fails, the reward
// goes back to 'ready': {ok:false, reason:"stripe_error"}.
//
// Secrets (the founder sets them): INVITE_SALT (required: without it every
// action answers 503 {error:"not_configured"}; changing it later forgets
// recent visits and known computers), INVITE_WORKER_KEY (the same value as the
// website worker's; without it "visit" answers 503), STRIPE_SECRET_KEY (already
// set for create-checkout). SUPABASE_URL, SUPABASE_ANON_KEY and
// SUPABASE_SERVICE_ROLE_KEY are provided.
// Deploy with JWT verification DISABLED - this function checks the JWT itself,
// and info / visit carry none.

import { createClient, type SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";
import { assistantRefusal, isAssistantToken } from "../_shared/assistant_token.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const INVITE_SALT = Deno.env.get("INVITE_SALT") ?? "";
const WORKER_KEY = Deno.env.get("INVITE_WORKER_KEY") ?? "";
const STRIPE_KEY = Deno.env.get("STRIPE_SECRET_KEY") ?? "";

const SITE = "https://gusan.aibuben.xyz";
const MAX_BODY_BYTES = 4096;

const STRIPE_API = "https://api.stripe.com/v1";
const STRIPE_VERSION = "2024-06-20"; // the apiVersion create-checkout uses
const STRIPE_TIMEOUT_MS = 10_000;
const CREDIT_DESCRIPTION = "Gusan invite: 1 free month";

// Same alphabet as public.invite_ensure_code: no i, l, o, 0 or 1.
const CODE_RE = /^[a-z]{1,10}-[abcdefghjkmnpqrstuvwxyz23456789]{4}$/;
const CODE_IN_TEXT_RE = /(?:^|[^a-z0-9-])([a-z]{1,10}-[abcdefghjkmnpqrstuvwxyz23456789]{4})(?![a-z0-9-])/;
const DEVICE_RE = /^[0-9a-f]{64}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(obj: unknown, status = 200, extra: Record<string, string> = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, "Content-Type": "application/json", "Cache-Control": "no-store", ...extra },
  });
}

// Fixed words only: never a code, a name, an address, an id or an error message.
function log(action: string, outcome: string) {
  console.log("invite", action, outcome);
}

// --- input -------------------------------------------------------------------

/** "aram-7k2q", " ARAM-7K2Q ", "https://gusan.aibuben.xyz/i/aram-7k2q/" or a sentence with the code -> "aram-7k2q". */
export function parseCode(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  let text = raw.trim().toLowerCase();
  if (!text || text.length > 300) return null;
  const at = text.lastIndexOf("/i/");
  if (at >= 0) text = text.slice(at + 3).split(/[/?#\s]/)[0];
  if (CODE_RE.test(text)) return text;
  return CODE_IN_TEXT_RE.exec(text)?.[1] ?? null;
}

export function parseDevice(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const value = raw.trim().toLowerCase();
  return DEVICE_RE.test(value) ? value : null;
}

/** The caller's address as Cloudflare saw it, else the first x-forwarded-for entry. */
export function clientIp(req: Request): string {
  const cf = req.headers.get("cf-connecting-ip");
  if (cf) return cf.trim();
  return (req.headers.get("x-forwarded-for") ?? "").split(",")[0].trim();
}

/**
 * The part of an address that stays the same for one home or office: all of an
 * IPv4 address, the /64 network of an IPv6 address (its last half changes for
 * privacy every day or so). null when it is not an address.
 */
export function normalizeIp(raw: string): string | null {
  let ip = (raw || "").trim().toLowerCase();
  if (!ip || ip.length > 64) return null;
  if (ip.startsWith("[")) ip = ip.slice(1, ip.includes("]") ? ip.indexOf("]") : undefined);
  ip = ip.split("%")[0];
  const v4 = /^(?:::ffff:)?(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(ip);
  if (v4) {
    const parts = v4.slice(1).map(Number);
    return parts.every((n) => n <= 255) ? `v4:${parts.join(".")}` : null;
  }
  if (!ip.includes(":") || !/^[0-9a-f:]+$/.test(ip)) return null;
  const halves = ip.split("::");
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(":") : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const fill = halves.length === 2 ? 8 - head.length - tail.length : 0;
  if (fill < 0 || (halves.length === 2 && fill < 1)) return null;
  const groups = [...head, ...Array(fill).fill("0"), ...tail];
  if (groups.length !== 8 || groups.some((g) => !/^[0-9a-f]{1,4}$/.test(g))) return null;
  return `v6:${groups.slice(0, 4).map((g) => g.padStart(4, "0")).join(":")}`;
}

// --- keyed hashes (INVITE_SALT only: no fallback key) ----------------------------

let hmacKey: Promise<CryptoKey> | null = null;

async function hmacHex(kind: "ip" | "device", value: string): Promise<string> {
  hmacKey ??= crypto.subtle.importKey("raw", new TextEncoder().encode(INVITE_SALT), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", await hmacKey, new TextEncoder().encode(`${kind}:${value}`));
  return Array.from(new Uint8Array(sig), (b) => b.toString(16).padStart(2, "0")).join("");
}

function sameSecret(given: string, expected: string): boolean {
  if (!given || !expected || given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

// --- per-key limiter (in memory: resets on a cold start, which is fine) --------

const RATE_WINDOW_MS = 60_000;
const MAX_TRACKED = 5000;
const hits = new Map<string, { n: number; reset: number }>();

/** Seconds to wait when this key is over `limit` calls a minute, 0 when it may go on. */
function limited(id: string, limit: number, now = Date.now()): number {
  if (hits.size >= MAX_TRACKED) {
    for (const [k, hit] of hits) if (hit.reset <= now) hits.delete(k);
    if (hits.size >= MAX_TRACKED) hits.clear();
  }
  const hit = hits.get(id);
  if (!hit || hit.reset <= now) {
    hits.set(id, { n: 1, reset: now + RATE_WINDOW_MS });
    return 0;
  }
  hit.n += 1;
  return hit.n > limit ? Math.max(1, Math.ceil((hit.reset - now) / 1000)) : 0;
}

/** The limiter key for an address: its network (IPv6 by /64), so rotating the host part does not help. */
export const networkKey = (raw: string) => normalizeIp(raw) ?? "unknown";

function tooMany(action: string, wait: number) {
  log(action, "rate_limited");
  return json({ error: "rate_limited" }, 429, { "Retry-After": String(wait) });
}

// --- Stripe (plain REST, like delete-account; create-checkout's API version) -----

type StripeObject = Record<string, unknown>;

async function stripeCall(path: string, form?: Record<string, string>, idempotencyKey?: string): Promise<StripeObject | null> {
  if (!STRIPE_KEY) return null;
  const headers: Record<string, string> = { Authorization: `Bearer ${STRIPE_KEY}`, "Stripe-Version": STRIPE_VERSION };
  if (form) headers["Content-Type"] = "application/x-www-form-urlencoded";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  try {
    const res = await fetch(`${STRIPE_API}${path}`, {
      method: form ? "POST" : "GET",
      headers,
      body: form ? new URLSearchParams(form).toString() : undefined,
      signal: AbortSignal.timeout(STRIPE_TIMEOUT_MS),
    });
    if (!res.ok) return null;
    return (await res.json()) as StripeObject;
  } catch {
    return null;
  }
}

type StripeItem = {
  quantity?: number | null;
  price?: {
    unit_amount?: number | null;
    currency?: string | null;
    recurring?: { interval?: string | null; interval_count?: number | null } | null;
  } | null;
};

/**
 * One MONTH of an active subscription's current price, in its one currency: for each item
 * unit_amount x quantity, divided by 12 x n for a price billed every n years and by n for one
 * billed every n months, summed and rounded to the cent. null when it can't be told (not active,
 * a daily or weekly price, a tiered price without unit_amount, mixed currencies, nothing to credit).
 */
export function monthCharge(sub: StripeObject | null): { customer: string; amount: number; currency: string } | null {
  if (!sub || sub.status !== "active") return null;
  const rawCustomer = sub.customer as string | { id?: string } | null | undefined;
  const customer = typeof rawCustomer === "string" ? rawCustomer : rawCustomer?.id ?? "";
  const items = ((sub.items as { data?: unknown[] } | undefined)?.data ?? []) as StripeItem[];
  let total = 0;
  let currency = "";
  for (const item of items) {
    const unit = item.price?.unit_amount;
    const quantity = item.quantity ?? 1;
    const itemCurrency = String(item.price?.currency ?? "").toLowerCase();
    const interval = item.price?.recurring?.interval ?? "";
    const count = item.price?.recurring?.interval_count ?? 1;
    if (typeof unit !== "number" || !Number.isInteger(unit) || unit < 0 || !Number.isInteger(quantity) || quantity < 0) return null;
    if (!/^[a-z]{3}$/.test(itemCurrency) || (currency && itemCurrency !== currency)) return null;
    if (!Number.isInteger(count) || count < 1) return null;
    const months = interval === "month" ? count : interval === "year" ? 12 * count : 0;
    if (!months) return null; // day or week: no clean month
    currency = itemCurrency;
    total += (unit * quantity) / months;
  }
  const amount = Math.round(total);
  if (!customer.startsWith("cus_") || !currency || amount <= 0) return null;
  return { customer, amount, currency };
}

/** The customer's balance transaction made for this reward before (metadata grant_id), if any. */
export function findCredit(list: StripeObject | null, grantId: string): string | null | undefined {
  if (!list || !Array.isArray(list.data)) return undefined; // could not tell
  for (const txn of list.data as { id?: unknown; metadata?: Record<string, unknown> | null }[]) {
    if (txn?.metadata?.grant_id === grantId && typeof txn.id === "string") return txn.id;
  }
  return null;
}

/** The next bill free: a customer-balance credit of one month, then the reward is marked "credited". */
async function creditNextBill(db: SupabaseClient, userId: string, grantId: string, subscriptionId: string): Promise<Response> {
  const fail = async () => {
    // Nothing was credited: the reward is ready again.
    await db.rpc("invite_revert_crediting", { p_user: userId, p_grant_id: grantId });
    log("claim", "stripe_error");
    return json({ ok: false, reason: "stripe_error" });
  };
  const charge = monthCharge(await stripeCall(`/subscriptions/${encodeURIComponent(subscriptionId)}?expand[]=items.data.price`));
  if (!charge) return await fail();
  const customerPath = `/customers/${encodeURIComponent(charge.customer)}/balance_transactions`;
  // A credit made for this reward before (a retry after the database step failed) is used again.
  let txnId = findCredit(await stripeCall(`${customerPath}?limit=100`), grantId);
  if (txnId === undefined) return await fail();
  if (!txnId) {
    const txn = await stripeCall(
      customerPath,
      {
        amount: String(-charge.amount),
        currency: charge.currency,
        description: CREDIT_DESCRIPTION,
        "metadata[grant_id]": grantId,
      },
      grantId,
    );
    txnId = typeof txn?.id === "string" ? txn.id : "";
    if (!txnId) return await fail();
  }
  const { data: marked, error } = await db.rpc("invite_mark_credited", {
    p_user: userId,
    p_grant_id: grantId,
    p_txn: txnId,
    p_amount: charge.amount,
    p_currency: charge.currency,
  });
  if (error || !marked) {
    // The credit is on Stripe and the reward stays 'crediting': a person checks it (invite_daily logs it).
    log("claim", "mark_error");
    return json({ error: "server_error" }, 500);
  }
  log("claim", "next_bill_free");
  return json({ ok: true, status: "next_bill_free", amount: charge.amount, currency: charge.currency });
}

// --- the website worker ----------------------------------------------------------

let svc: SupabaseClient | null = null;
function service(): SupabaseClient | null {
  if (!SERVICE_KEY || !SUPABASE_URL) return null;
  svc ??= createClient(SUPABASE_URL, SERVICE_KEY, { auth: { persistSession: false } });
  return svc;
}

const fromWorker = (req: Request) => sameSecret(req.headers.get("x-gusan-worker") ?? "", WORKER_KEY);

async function info(req: Request, body: Record<string, unknown>): Promise<Response> {
  // The worker's own calls all come from Cloudflare's addresses, so with the
  // worker key the visitor's network (from the body) is the one limited.
  const visitor = fromWorker(req) && typeof body.ip === "string" ? body.ip : "";
  const wait = limited(visitor ? `info:v:${networkKey(visitor)}` : `info:${networkKey(clientIp(req))}`, 30);
  if (wait) return tooMany("info", wait);
  const code = parseCode(body.code);
  if (!code) {
    log("info", "bad_code");
    return json({ valid: false });
  }
  const db = service();
  if (!db) return json({ error: "server_misconfigured" }, 503);
  const { data, error } = await db.rpc("invite_info", { p_code: code });
  if (error) {
    log("info", "db_error");
    return json({ error: "server_error" }, 500);
  }
  const valid = !!data?.valid;
  log("info", valid ? "valid" : "unknown");
  return json(valid && data?.name ? { valid, name: String(data.name) } : { valid });
}

async function visit(req: Request, body: Record<string, unknown>): Promise<Response> {
  if (!WORKER_KEY) {
    log("visit", "no_worker_key");
    return json({ error: "not_configured" }, 503);
  }
  if (!fromWorker(req)) {
    log("visit", "unauthorized");
    return json({ error: "unauthorized" }, 401);
  }
  const code = parseCode(body.code);
  const ip = normalizeIp(typeof body.ip === "string" ? body.ip : "");
  if (!code || !ip) {
    log("visit", "bad_input");
    return json({ error: "bad_request" }, 400);
  }
  const db = service();
  if (!db) return json({ error: "server_misconfigured" }, 503);
  const { data, error } = await db.rpc("invite_record_visit", { p_code: code, p_ip_hmac: await hmacHex("ip", ip) });
  if (error) {
    log("visit", "db_error");
    return json({ error: "server_error" }, 500);
  }
  log("visit", data ? "recorded" : "skipped");
  return json({ ok: true });
}

// --- signed-in actions -------------------------------------------------------------

async function signedIn(req: Request, action: string, body: Record<string, unknown>): Promise<Response> {
  const authHeader = req.headers.get("Authorization") ?? "";
  // An AI assistant's token (the Gusan connector) may only read meetings, never act here.
  if (isAssistantToken(authHeader)) return assistantRefusal(cors);
  if (!authHeader.startsWith("Bearer ")) return json({ error: "unauthorized" }, 401);
  const db = service();
  if (!ANON_KEY || !SUPABASE_URL || !db) return json({ error: "server_misconfigured" }, 503);
  // The user's own token only proves who they are; the work runs with the service role for that id.
  const supa = createClient(SUPABASE_URL, ANON_KEY, {
    global: { headers: { Authorization: authHeader } },
    auth: { persistSession: false },
  });
  const { data: userData, error: userErr } = await supa.auth.getUser();
  if (userErr || !userData?.user) return json({ error: "unauthorized" }, 401);
  const userId = userData.user.id;
  const wait = limited(`user:${userId}`, 30);
  if (wait) return tooMany(action, wait);
  const failed = (step: string) => {
    log(action, `${step}_error`);
    return json({ error: "server_error" }, 500);
  };

  if (action === "me") {
    // Optional: the inviter's computer, so a friend on it cannot join.
    const device = parseDevice(body.device);
    const deviceHmac = device ? await hmacHex("device", device) : null;
    const { data, error } = await db.rpc("invite_me", { p_user: userId, p_device_hmac: deviceHmac });
    const me = data as Record<string, unknown> | null;
    if (error || !me?.code) return failed("me");
    log("me", "ok");
    return json({ ...me, link: `${SITE}/i/${me.code}` });
  }

  if (action === "hint") {
    const network = normalizeIp(clientIp(req));
    const netWait = limited(`hint:${network ?? "unknown"}`, 30);
    if (netWait) return tooMany("hint", netWait);
    if (!network) {
      log("hint", "no_address");
      return json({ found: false });
    }
    const { data, error } = await db.rpc("invite_hint", { p_user: userId, p_ip_hmac: await hmacHex("ip", network) });
    if (error) return failed("hint");
    const hint = (data ?? {}) as { found?: boolean; name?: string | null; code?: string };
    if (!hint.found || !hint.code) {
      log("hint", "none");
      return json({ found: false });
    }
    log("hint", "found");
    return json(hint.name ? { found: true, name: hint.name, code: hint.code } : { found: true, code: hint.code });
  }

  if (action === "join") {
    const device = parseDevice(body.device);
    if (!device) {
      log("join", "bad_device");
      return json({ error: "bad_device" }, 400);
    }
    const code = parseCode(body.code);
    if (!code) {
      log("join", "bad_code");
      return json({ ok: false, reason: "bad_code" });
    }
    const source = body.source === "hint" ? "hint" : "paste";
    const network = normalizeIp(clientIp(req));
    const { data, error } = await db.rpc("invite_join", {
      p_user: userId,
      p_code: code,
      p_device_hmac: await hmacHex("device", device),
      p_source: source,
      p_ip_hmac: network ? await hmacHex("ip", network) : null,
    });
    if (error) return failed("join");
    const result = (data ?? {}) as { ok?: boolean; reason?: string; name?: string | null; gift_ends_at?: string };
    log("join", result.ok ? "ok" : String(result.reason ?? "refused"));
    return json(result.ok
      ? { ok: true, name: result.name ?? null, gift_ends_at: result.gift_ends_at }
      : { ok: false, reason: result.reason ?? "bad_code" });
  }

  if (action === "claim") {
    const id = typeof body.id === "string" ? body.id.trim().toLowerCase() : "";
    if (!UUID_RE.test(id)) {
      log("claim", "bad_id");
      return json({ error: "bad_id" }, 400);
    }
    const { data, error } = await db.rpc("invite_claim", { p_user: userId, p_grant_id: id });
    if (error) return failed("claim");
    const result = (data ?? {}) as Record<string, unknown>;
    // A paying subscriber: SQL set the reward to 'crediting' and names the subscription to credit.
    if (result.reason === "stripe_credit" && typeof result.subscription_id === "string") {
      return await creditNextBill(db, userId, id, result.subscription_id);
    }
    log("claim", result.ok ? String(result.status) : String(result.reason ?? "refused"));
    return json(result.ok
      ? { ok: true, status: "active", starts_at: result.starts_at, ends_at: result.ends_at }
      : { ok: false, reason: String(result.reason ?? "not_found") });
  }

  return json({ error: "unknown_action" }, 400);
}

// --- entry ---------------------------------------------------------------------

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  // Required: a deploy without the secret answers this to every action, so a check can tell.
  if (!INVITE_SALT) {
    log("none", "not_configured");
    return json({ error: "not_configured" }, 503);
  }
  if (Number(req.headers.get("content-length") ?? "0") > MAX_BODY_BYTES) return json({ error: "too_large" }, 413);

  let body: Record<string, unknown>;
  try {
    const text = await req.text();
    if (text.length > MAX_BODY_BYTES) return json({ error: "too_large" }, 413);
    const parsed = JSON.parse(text || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return json({ error: "bad_request" }, 400);
    body = parsed as Record<string, unknown>;
  } catch {
    return json({ error: "bad_json" }, 400);
  }

  const action = typeof body.action === "string" ? body.action : "";
  try {
    if (action === "info") return await info(req, body);
    if (action === "visit") return await visit(req, body);
    if (["me", "hint", "join", "claim"].includes(action)) return await signedIn(req, action, body);
    log("none", "unknown_action");
    return json({ error: "unknown_action" }, 400);
  } catch (_err) {
    log(action || "none", "exception");
    return json({ error: "server_error" }, 500);
  }
});
