// "Connect Notion" for the desktop app: the one server piece of the Notion
// OAuth flow (public connection "Gusan"). It holds the connection's client
// secret so the app never has to, and it stores nothing: no database, no
// Supabase client, no token or code is ever kept or logged.
//
// GET  = the relay. Notion redirects the user's browser here after they allow
//        access (the one redirect URI registered in Notion). The app's state is
//        "<nonce>.<port>"; we answer a bare 302 to http://127.0.0.1:<port>/notion
//        with the code (or the error) so the app's one-shot listener gets it.
//        Host and path are fixed here, so this is not an open redirect.
// POST = JSON, only the desktop app calls it (no CORS headers on purpose):
//          {"action":"exchange","code":"..","redirect_uri":".."}
//          {"action":"refresh","refresh_token":".."}
//          {"action":"revoke","token":".."}
//        exchange / refresh answer {access_token, refresh_token ("" when Notion
//        gave none), workspace_id, workspace_name, workspace_icon, bot_id,
//        duplicated_template_id} - never the owner's name or email. revoke
//        answers {ok:true}. Errors are {error:"snake_case"} (+ an optional
//        notion_code, for the logs only).
//
// Anonymous on purpose, so Notion works for guests too. Guards: the exact
// redirect_uri, an 8 KB body cap, a per-IP limit and no CORS.
//
// Secrets (set by the founder in the Supabase dashboard, see
// docs/redesign/notion-setup-for-founder.md): NOTION_CLIENT_ID,
// NOTION_CLIENT_SECRET. SUPABASE_URL is provided by Supabase.
// Deploy with JWT verification DISABLED - Notion's browser redirect carries no
// Supabase token, and the other functions are deployed the same way.

// Notion API version for the token calls. Notion's reference pages for
// /oauth/token and /oauth/revoke list only this version, so keep it even if
// notion_export.py ever rolls its own constant back to "2022-06-28".
const NOTION_VERSION = "2026-03-11";
const NOTION_OAUTH = "https://api.notion.com/v1/oauth";
const NOTION_TIMEOUT_MS = 10_000;

const FALLBACK_BASE = "https://hftcelxzfoubheqeoool.supabase.co";
const FUNCTION_NAME = "notion-oauth";

// The only redirect URI registered in Notion, and the only one we accept.
const BASE_URL = (Deno.env.get("SUPABASE_URL") ?? "").replace(/\/+$/, "");
const ALLOWED_REDIRECT = `${BASE_URL.startsWith("https://") ? BASE_URL : FALLBACK_BASE}/functions/v1/${FUNCTION_NAME}`;

const MAX_BODY_BYTES = 8 * 1024;
const MAX_FIELD_CHARS = 2000;
const RATE_LIMIT = 15; // POSTs per IP per window
const RATE_WINDOW_MS = 60_000;
const MAX_TRACKED_IPS = 5000;

// "<nonce>.<port>": the app's random nonce and the port its listener got.
const STATE_RE = /^[A-Za-z0-9_-]{16,64}\.[0-9]{4,5}$/;

// Notion 400s that mean our own setup is wrong, not that the user's code or
// refresh token is bad.
const CONFIG_CODES = new Set(["unauthorized_client", "unsupported_grant_type", "invalid_scope"]);

type Action = "exchange" | "refresh" | "revoke";
type Fields = Record<string, unknown>;

const baseHeaders = {
  "Cache-Control": "no-store",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};

function json(obj: unknown, status = 200, extra: Record<string, string> = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...baseHeaders, "Content-Type": "application/json", ...extra },
  });
}

// Every error goes through here. Logs fixed words and numbers only - never a
// body, a query string, a code, a token or an exception message (a JSON parse
// error quotes the text it choked on).
function fail(action: string, status: number, error: string, extra: Fields = {}, headers: Record<string, string> = {}) {
  console.error(FUNCTION_NAME, action, status, error);
  return json({ error, ...extra }, status, headers);
}

// --- Per-IP limiter (in memory: resets on a cold start, which is fine) ----

const hits = new Map<string, { n: number; reset: number }>();

// Seconds to wait when this IP is over the limit, 0 when it may go on.
function limited(ip: string, now = Date.now()): number {
  if (hits.size >= MAX_TRACKED_IPS) {
    for (const [key, hit] of hits) if (hit.reset <= now) hits.delete(key);
    if (hits.size >= MAX_TRACKED_IPS) hits.clear();
  }
  const hit = hits.get(ip);
  if (!hit || hit.reset <= now) {
    hits.set(ip, { n: 1, reset: now + RATE_WINDOW_MS });
    return 0;
  }
  hit.n += 1;
  return hit.n > RATE_LIMIT ? Math.max(1, Math.ceil((hit.reset - now) / 1000)) : 0;
}

function clientIp(req: Request): string {
  const cf = req.headers.get("cf-connecting-ip");
  if (cf) return cf.trim();
  const forwarded = (req.headers.get("x-forwarded-for") ?? "").split(",")[0].trim();
  return forwarded || "unknown";
}

// --- GET: relay Notion's redirect to the app's loopback listener ----------

function relay(url: URL): Response {
  const state = url.searchParams.get("state") ?? "";
  const port = STATE_RE.test(state) ? Number(state.split(".")[1]) : 0;
  if (port < 1024 || port > 65535) return fail("relay", 400, "invalid_state");

  const code = url.searchParams.get("code") ?? "";
  const error = url.searchParams.get("error") ?? "";
  let query: string;
  if (code) {
    if (code.length > MAX_FIELD_CHARS) return fail("relay", 400, "invalid_request");
    query = `code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`;
  } else if (error) {
    // access_denied when the user cancelled in Notion; keep other standard
    // OAuth errors readable, anything odd becomes plain "error".
    const safe = /^[a-z_]{1,40}$/.test(error) ? error : "error";
    query = `error=${safe}&state=${encodeURIComponent(state)}`;
  } else {
    return fail("relay", 400, "invalid_request");
  }
  return new Response(null, {
    status: 302,
    headers: { ...baseHeaders, Location: `http://127.0.0.1:${port}/notion?${query}` },
  });
}

// --- POST: exchange / refresh / revoke -------------------------------------

// The body as text, or null when it is bigger than the cap (checked on the
// declared length first, then while reading, so a liar cannot make us buffer it).
async function readBody(req: Request): Promise<string | null> {
  const declared = Number(req.headers.get("content-length") ?? "0");
  if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) return null;
  if (!req.body) return "";
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel().catch(() => {});
      return null;
    }
    chunks.push(value);
  }
  const all = new Uint8Array(size);
  let at = 0;
  for (const chunk of chunks) {
    all.set(chunk, at);
    at += chunk.byteLength;
  }
  return new TextDecoder().decode(all);
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_FIELD_CHARS ? value : null;
}

// HTTP Basic "client_id:client_secret", or null when a secret is missing.
function basicAuth(): string | null {
  const id = (Deno.env.get("NOTION_CLIENT_ID") ?? "").trim();
  const secret = (Deno.env.get("NOTION_CLIENT_SECRET") ?? "").trim();
  if (!id || !secret) return null;
  try {
    return `Basic ${btoa(`${id}:${secret}`)}`;
  } catch (_e) {
    return null; // a non-ASCII character in a secret
  }
}

async function callNotion(path: "token" | "revoke", payload: Fields, auth: string) {
  const res = await fetch(`${NOTION_OAUTH}/${path}`, {
    method: "POST",
    headers: {
      "Authorization": auth,
      "Content-Type": "application/json",
      "Notion-Version": NOTION_VERSION,
      "Accept": "application/json",
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(NOTION_TIMEOUT_MS),
  });
  let data: Fields = {};
  try {
    const parsed = JSON.parse(await res.text());
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) data = parsed as Fields;
  } catch (_e) {
    // not JSON: handled by the caller through the status
  }
  return { status: res.status, data };
}

function str(value: unknown, max: number): string {
  return typeof value === "string" ? value.slice(0, max) : "";
}

// Only what the app needs. No owner (name, email), no request id.
function tokenAnswer(action: Action, data: Fields): Response {
  const access = str(data.access_token, 10_000);
  if (!access) return fail(action, 502, "notion_failed");
  return json({
    access_token: access,
    refresh_token: str(data.refresh_token, 10_000),
    workspace_id: str(data.workspace_id, 64),
    workspace_name: str(data.workspace_name, 200),
    workspace_icon: str(data.workspace_icon, 500),
    bot_id: str(data.bot_id, 64),
    duplicated_template_id: str(data.duplicated_template_id, 64),
  });
}

// Notion said no. 400 = the code / refresh token is bad (the app asks the user
// to try again or reconnect); 401 and the "setup is wrong" 400s = our client id
// or secret is wrong; the rest = Notion trouble.
function notionRefused(action: Action, status: number, data: Fields): Response {
  const notionCode = typeof data.code === "string" && /^[a-z_]{1,40}$/.test(data.code) ? data.code : "";
  const extra = notionCode ? { notion_code: notionCode } : {};
  if (status === 401 || (status === 400 && CONFIG_CODES.has(notionCode))) {
    return fail(action, 503, "server_misconfigured", extra);
  }
  if (status === 400) return fail(action, 400, "invalid_grant", extra);
  return fail(action, 502, "notion_failed", extra);
}

async function post(req: Request): Promise<Response> {
  const wait = limited(clientIp(req));
  if (wait) return fail("post", 429, "rate_limited", {}, { "Retry-After": String(wait) });

  const raw = await readBody(req);
  if (raw === null) return fail("post", 413, "payload_too_large");

  let body: Fields;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
    body = parsed as Fields;
  } catch (_e) {
    return fail("post", 400, "invalid_json");
  }

  const action = body.action;
  if (action !== "exchange" && action !== "refresh" && action !== "revoke") {
    return fail("post", 400, "invalid_action");
  }

  let path: "token" | "revoke";
  let payload: Fields;
  if (action === "exchange") {
    if (body.redirect_uri !== ALLOWED_REDIRECT) return fail(action, 400, "invalid_redirect");
    const code = text(body.code);
    if (!code) return fail(action, 400, "invalid_request");
    path = "token";
    payload = { grant_type: "authorization_code", code, redirect_uri: ALLOWED_REDIRECT };
  } else if (action === "refresh") {
    const refreshToken = text(body.refresh_token);
    if (!refreshToken) return fail(action, 400, "invalid_request");
    path = "token";
    payload = { grant_type: "refresh_token", refresh_token: refreshToken };
  } else {
    const token = text(body.token);
    if (!token) return fail(action, 400, "invalid_request");
    path = "revoke";
    payload = { token };
  }

  const auth = basicAuth();
  if (!auth) return fail(action, 503, "server_misconfigured");

  let result: { status: number; data: Fields };
  try {
    result = await callNotion(path, payload, auth);
  } catch (_e) {
    // Timeout, DNS, connection reset: nothing worth logging beyond the status.
    return fail(action, 504, "notion_unreachable");
  }
  if (result.status < 200 || result.status >= 300) return notionRefused(action, result.status, result.data);
  return path === "revoke" ? json({ ok: true }) : tokenAnswer(action, result.data);
}

Deno.serve(async (req: Request) => {
  try {
    if (req.method === "GET") return relay(new URL(req.url));
    if (req.method === "POST") return await post(req);
    return fail("method", 405, "method_not_allowed", {}, { "Allow": "GET, POST" });
  } catch (_e) {
    return fail("unexpected", 500, "internal_error");
  }
});
