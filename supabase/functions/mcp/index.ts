// The Gusan connector: an MCP server (Streamable HTTP, stateless, JSON answers) for Claude, ChatGPT and other AI
// assistants. Public address https://gusan.aibuben.xyz/mcp (the site's worker.js passes it here).
// docs/mcp/gusan-connector-spec.md; the protocol and the tools are in server.ts.
//
// Sign-in: Supabase Auth's OAuth 2.1 server. An assistant without a valid token gets 401 with the address of
// this resource's metadata (served by the site), which names Supabase Auth as the authorization server; the
// person signs in on https://gusan.aibuben.xyz/oauth/consent and allows it. The token is checked with
// /auth/v1/user on every request (cached for a minute).
//
// Reading, always for the token's person only:
//   * meetings: the cloud_meetings_* functions with the person's own token, so row-level security applies
//     (and gusan_api_pre_request lets an assistant's token reach only those, plus my_entitlement);
//   * plan, usage and payments: my_entitlement with their token; profiles, subscriptions, pro_grants,
//     invites, cloud_usage and app_usage_stats with the service role, filtered on their user id; Stripe
//     (read only: customer balance, charges, upcoming invoice) for their own customer id.
// Nothing is ever written. Deploy with verify_jwt=false.

import { type AccountInfo, type Backend, handleBody, type MeetingFull, type MeetingRow, type Payment, type UsageInfo }
  from "./server.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const STRIPE_KEY = Deno.env.get("STRIPE_SECRET_KEY") ?? "";
const STRIPE_VERSION = "2024-06-20";

const RESOURCE = "https://gusan.aibuben.xyz/mcp";
const RESOURCE_METADATA = "https://gusan.aibuben.xyz/.well-known/oauth-protected-resource/mcp";
const MAX_BODY = 256 * 1024;
const PER_MINUTE = 120;

const cors: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
  "Access-Control-Allow-Headers":
    "authorization, content-type, accept, mcp-protocol-version, mcp-session-id, last-event-id, x-client-info, apikey",
  "Access-Control-Expose-Headers": "mcp-session-id, mcp-protocol-version, www-authenticate",
  "Access-Control-Max-Age": "86400",
};

function json(body: unknown, status = 200, extra: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...cors, "Content-Type": "application/json", "Cache-Control": "no-store", ...extra },
  });
}

function unauthorized(reason: "missing" | "invalid"): Response {
  const challenge = reason === "invalid"
    ? `Bearer error="invalid_token", error_description="Sign in to Gusan again", resource_metadata="${RESOURCE_METADATA}"`
    : `Bearer resource_metadata="${RESOURCE_METADATA}"`;
  return json({ error: reason === "invalid" ? "invalid_token" : "unauthorized", resource: RESOURCE }, 401, {
    "WWW-Authenticate": challenge,
  });
}

// ── Who is calling ──

type Who = { id: string; email: string | null };
const seen = new Map<string, { who: Who; until: number }>();

async function tokenKey(token: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function whoIs(token: string): Promise<Who | null> {
  const key = await tokenKey(token);
  const hit = seen.get(key);
  if (hit && hit.until > Date.now()) return hit.who;
  const res = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
    headers: { apikey: ANON_KEY, Authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(8000),
  });
  if (!res.ok) return null;
  const user = await res.json().catch(() => null);
  if (!user?.id) return null;
  const who = { id: String(user.id), email: typeof user.email === "string" ? user.email : null };
  if (seen.size > 2000) seen.clear();
  seen.set(key, { who, until: Date.now() + 60_000 });
  return who;
}

const calls = new Map<string, number[]>();
function tooMany(userId: string): boolean {
  const now = Date.now();
  const recent = (calls.get(userId) ?? []).filter((t) => now - t < 60_000);
  recent.push(now);
  calls.set(userId, recent);
  if (calls.size > 5000) calls.clear();
  return recent.length > PER_MINUTE;
}

// ── Reading ──

class ReadError extends Error {}

async function rpc<T>(token: string, name: string, args: Record<string, unknown>): Promise<T> {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/rpc/${name}`, {
    method: "POST",
    headers: { apikey: ANON_KEY, Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(args),
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new ReadError(`${name}: ${res.status} ${await res.text().catch(() => "")}`.slice(0, 300));
  return await res.json() as T;
}

async function service<T>(path: string): Promise<T> {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    headers: { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}` },
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new ReadError(`${path.split("?")[0]}: ${res.status}`);
  return await res.json() as T;
}

async function stripe<T>(path: string): Promise<T | null> {
  if (!STRIPE_KEY) return null;
  const res = await fetch(`https://api.stripe.com/v1/${path}`, {
    headers: { Authorization: `Bearer ${STRIPE_KEY}`, "Stripe-Version": STRIPE_VERSION },
    signal: AbortSignal.timeout(8000),
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new ReadError(`stripe ${path.split("?")[0]}: ${res.status}`);
  return await res.json() as T;
}

const iso = (unix: number | null | undefined) => (unix ? new Date(unix * 1000).toISOString() : null);
const enc = encodeURIComponent;

function backendFor(token: string, who: Who): Backend {
  const uid = enc(who.id);
  return {
    async searchMeetings(a) {
      return await rpc<MeetingRow[]>(token, "cloud_meetings_search", {
        p_query: a.query, p_from: a.from ?? null, p_to: a.to ?? null, p_limit: a.limit,
      });
    },
    async listMeetings(a) {
      const rows = await rpc<(MeetingRow & { total: number })[]>(token, "cloud_meetings_list", {
        p_from: a.from ?? null, p_to: a.to ?? null, p_person: a.person ?? null, p_limit: a.limit, p_offset: a.offset,
      });
      let total = rows[0]?.total ?? 0;
      if (!rows.length && a.offset) {
        const first = await rpc<{ total: number }[]>(token, "cloud_meetings_list", {
          p_from: a.from ?? null, p_to: a.to ?? null, p_person: a.person ?? null, p_limit: 1, p_offset: 0,
        });
        total = first[0]?.total ?? 0;
      }
      return { rows, total: Number(total) };
    },
    async getMeeting(id) {
      const rows = await rpc<MeetingFull[]>(token, "cloud_meetings_get", { p_id: id });
      return rows[0] ?? null;
    },
    async getTranscript(id, offset, chars) {
      const rows = await rpc<{ part: string | null; total: number }[]>(token, "cloud_meetings_transcript", {
        p_id: id, p_offset: offset, p_chars: chars,
      });
      return rows[0] ? { part: rows[0].part ?? "", total: Number(rows[0].total ?? 0) } : null;
    },
    async cloudCopyOn() {
      const [m, s] = await Promise.all([
        service<unknown[]>(`cloud_meetings?user_id=eq.${uid}&select=id&limit=1`),
        service<unknown[]>(`app_usage_stats?user_id=eq.${uid}&select=user_id&limit=1`),
      ]);
      return m.length > 0 || s.length > 0;
    },
    async account(): Promise<AccountInfo> {
      const now = new Date().toISOString();
      const [ent, profiles, grants, invites, lifetime] = await Promise.all([
        rpc<any[]>(token, "my_entitlement", {}),
        service<any[]>(`profiles?id=eq.${uid}&select=email,created_at,stripe_customer_id`),
        service<any[]>(`pro_grants?user_id=eq.${uid}&status=eq.active&ends_at=gt.${enc(now)}&select=starts_at,ends_at,source`),
        service<any[]>(`invites?inviter_id=eq.${uid}&counted_at=not.is.null&select=counted_at`),
        service<any[]>(`lifetime_purchases?user_id=eq.${uid}&status=eq.granted&select=payment_intent_id`),
      ]);
      const e = ent[0] ?? {};
      const profile = profiles[0] ?? {};
      const giftUntil = grants.length
        ? grants.map((g) => String(g.ends_at)).sort().at(-1) ?? null
        : null;
      const info: AccountInfo = {
        email: who.email ?? profile.email ?? null,
        member_since: profile.created_at ?? null,
        plan: e.plan ?? null,
        status: e.status ?? null,
        is_pro: Boolean(e.is_pro),
        period_end: e.current_period_end ?? null,
        cancel_at_period_end: Boolean(e.cancel_at_period_end),
        cancels_at: e.cancels_at ?? null,
        commitment_ends_at: e.commitment_ends_at ?? null,
        gift_until: giftUntil,
        next_payment: null,
        credit: null,
        payments: [],
        friends_joined: invites.length,
      };
      const customer = String(profile.stripe_customer_id ?? "");
      if (!customer) return info;
      const lifetimeIntents = new Set(lifetime.map((l) => String(l.payment_intent_id ?? "")).filter(Boolean));
      try {
        const [cust, charges, upcoming] = await Promise.all([
          stripe<any>(`customers/${enc(customer)}`),
          stripe<any>(`charges?customer=${enc(customer)}&limit=50&expand[]=data.invoice`),
          stripe<any>(`invoices/upcoming?customer=${enc(customer)}`).catch(() => null),
        ]);
        if (cust && typeof cust.balance === "number" && cust.balance < 0) {
          info.credit = { amount: -cust.balance, currency: String(cust.currency ?? "eur") };
        }
        const payments: Payment[] = [];
        for (const c of charges?.data ?? []) {
          if (c.status !== "succeeded" || !c.paid) continue;
          const line = c.invoice && typeof c.invoice === "object" ? c.invoice.lines?.data?.[0]?.description : null;
          const label = line ||
            (lifetimeIntents.has(String(c.payment_intent ?? "")) ? "Gusan Pro Lifetime" : (c.description || "Gusan Pro"));
          payments.push({
            date: iso(c.created) ?? "", amount: Number(c.amount) || 0, currency: String(c.currency ?? "eur"),
            label: String(label).slice(0, 120), refunded: Number(c.amount_refunded) || 0,
          });
        }
        info.payments = payments;
        if (upcoming && Number(upcoming.amount_due) >= 0 && !info.cancel_at_period_end && !info.cancels_at) {
          info.next_payment = {
            date: iso(upcoming.next_payment_attempt ?? upcoming.period_end),
            amount: Number(upcoming.amount_due) || 0,
            currency: String(upcoming.currency ?? "eur"),
          };
        }
      } catch (err) {
        console.error("mcp: stripe read failed", String(err).slice(0, 200));
        info.payments = null;
      }
      return info;
    },
    async usage(): Promise<UsageInfo> {
      const month = new Date().toISOString().slice(0, 7) + "-01";
      const [stats, cloudRows, meetings] = await Promise.all([
        service<any[]>(`app_usage_stats?user_id=eq.${uid}&select=stats,updated_at`),
        service<any[]>(`cloud_usage?user_id=eq.${uid}&select=day,requests,seconds_used,action_requests`),
        fetch(`${SUPABASE_URL}/rest/v1/cloud_meetings?user_id=eq.${uid}&select=id`, {
          method: "HEAD",
          headers: { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}`, Prefer: "count=exact" },
          signal: AbortSignal.timeout(10_000),
        }).then((r) => Number((r.headers.get("content-range") ?? "*/0").split("/")[1]) || 0),
      ]);
      let app: UsageInfo["app"] = null;
      if (stats.length) {
        app = { since: null, meetings: 0, meeting_minutes: 0, dictations: 0, dictation_words: 0, files: 0, file_minutes: 0, updated_at: null };
        for (const row of stats) {
          const s = row.stats ?? {};
          for (const k of ["meetings", "meeting_minutes", "dictations", "dictation_words", "files", "file_minutes"] as const) {
            app[k] += Number(s[k]) || 0;
          }
          const since = typeof s.since === "string" ? s.since : null;
          if (since && (!app.since || since < app.since)) app.since = since;
          if (row.updated_at && (!app.updated_at || row.updated_at > app.updated_at)) app.updated_at = row.updated_at;
        }
      }
      const cloud = { month_minutes: 0, total_minutes: 0, month_requests: 0, total_requests: 0, month_actions: 0, total_actions: 0 };
      for (const r of cloudRows) {
        const mins = (Number(r.seconds_used) || 0) / 60;
        const inMonth = String(r.day) >= month;
        cloud.total_minutes += mins;
        cloud.total_requests += Number(r.requests) || 0;
        cloud.total_actions += Number(r.action_requests) || 0;
        if (inMonth) {
          cloud.month_minutes += mins;
          cloud.month_requests += Number(r.requests) || 0;
          cloud.month_actions += Number(r.action_requests) || 0;
        }
      }
      return { app, cloud_meetings: meetings, cloud };
    },
  };
}

// ── HTTP ──

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  if (req.method !== "POST") {
    // No server-to-client stream and no sessions: everything is a POST with a JSON answer.
    return json({ error: "method_not_allowed" }, 405, { Allow: "POST, OPTIONS" });
  }
  if (!SUPABASE_URL || !ANON_KEY || !SERVICE_KEY) return json({ error: "server_misconfigured" }, 503);

  const auth = req.headers.get("Authorization") ?? "";
  const token = auth.replace(/^Bearer\s+/i, "").trim();
  if (!auth || !/^[\w-]+\.[\w-]+\.[\w-]+$/.test(token)) return unauthorized(auth ? "invalid" : "missing");
  let who: Who | null = null;
  try {
    who = await whoIs(token);
  } catch (err) {
    console.error("mcp: token check failed", String(err).slice(0, 200));
    return json({ jsonrpc: "2.0", id: null, error: { code: -32603, message: "Gusan can't check the sign-in just now." } }, 503);
  }
  if (!who) return unauthorized("invalid");
  if (tooMany(who.id)) return json({ jsonrpc: "2.0", id: null, error: { code: -32000, message: "Too many requests; wait a minute." } }, 429, { "Retry-After": "60" });

  if (Number(req.headers.get("content-length") ?? 0) > MAX_BODY) return json({ error: "too_large" }, 413);
  const raw = await req.text();
  if (raw.length > MAX_BODY) return json({ error: "too_large" }, 413);
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }, 400);
  }

  const out = await handleBody(body, backendFor(token, who), (e) => console.error("mcp: tool failed", String(e).slice(0, 300)));
  if (out === null) return new Response(null, { status: 202, headers: cors });
  return json(out);
});
