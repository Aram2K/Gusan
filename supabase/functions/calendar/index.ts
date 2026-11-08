import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

type Obj = Record<string, any>;
const env = (key: string) => Deno.env.get(key) ?? "";
const scope = "https://www.googleapis.com/auth/calendar.readonly";
const hits = new Map<string, { count: number; until: number }>();
class Failure extends Error {
  code: string;
  status: number;
  constructor(code: string, status = 400) { super(code); this.code = code; this.status = status; }
}
const headers = { "Content-Type": "application/json", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers });
const redirect = (reason?: string) => new Response(null, { status: 302, headers: { ...headers, Location: reason ? `gusan://calendar?google=error&reason=${reason}` : "gusan://calendar?google=connected" } });
function checked(result: Obj) {
  if (result.error) throw new Failure("storage_error", 500);
  return result.data;
}
function rate(key: string, limit: number) {
  const now = Date.now();
  for (const [key, value] of hits) if (value.until <= now) hits.delete(key);
  if (!hits.has(key)) {
    if (hits.size >= 5000) throw new Failure("rate_limited", 429);
    hits.set(key, { count: 0, until: now + 60000 });
  }
  if (++hits.get(key)!.count > limit) throw new Failure("rate_limited", 429);
}
const bytes = (value: string) => new TextEncoder().encode(value);
const base64 = (value: Uint8Array) => btoa(String.fromCharCode(...value));
const unbase64 = (value: string) => Uint8Array.from(atob(value), c => c.charCodeAt(0));
const random = () => base64(crypto.getRandomValues(new Uint8Array(32))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
async function digest(value: string) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes(value)));
}
async function key() {
  const value = env("CALENDAR_ENCRYPTION_KEY");
  if (!/^[0-9a-f]{64}$/i.test(value)) throw new Failure("google_not_configured", 503);
  return crypto.subtle.importKey("raw", Uint8Array.from(value.match(/../g)!, x => parseInt(x, 16)), "AES-GCM", false, ["encrypt", "decrypt"]);
}
async function seal(value: string, aad: string) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const data = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: bytes(aad) }, await key(), bytes(value));
  return base64(iv) + "." + base64(new Uint8Array(data));
}
async function open(value: string, aad: string) {
  const [iv, data] = value.split(".");
  return new TextDecoder().decode(await crypto.subtle.decrypt({ name: "AES-GCM", iv: unbase64(iv), additionalData: bytes(aad) }, await key(), unbase64(data)));
}
async function google(url: string, init: RequestInit) {
  try {
    const response = await fetch(url, { ...init, redirect: "error", signal: AbortSignal.timeout(10000) });
    const data = await response.json();
    if (!response.ok) throw new Failure(data?.error === "invalid_grant" ? "google_reconnect" : "google_error", data?.error === "invalid_grant" ? 409 : 502);
    return data;
  } catch (error) {
    if (error instanceof Failure) throw error;
    throw new Failure("google_error", 502);
  }
}
const token = (params: Obj) => google("https://oauth2.googleapis.com/token", { method: "POST", body: new URLSearchParams({ client_id: env("GOOGLE_CALENDAR_CLIENT_ID"), client_secret: env("GOOGLE_CALENDAR_CLIENT_SECRET"), ...params }) });
async function readBody(req: Request) {
  if (Number(req.headers.get("content-length")) > 8192) throw new Failure("body_too_large", 413);
  const reader = req.body?.getReader(), chunks: Uint8Array[] = [];
  let length = 0;
  while (reader) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.length;
    if (length > 8192) { await reader.cancel(); throw new Failure("body_too_large", 413); }
    chunks.push(value);
  }
  const all = new Uint8Array(length); let at = 0;
  for (const chunk of chunks) { all.set(chunk, at); at += chunk.length; }
  try {
    const body = JSON.parse(new TextDecoder().decode(all));
    if (!body || typeof body !== "object" || Array.isArray(body)) throw 0;
    return body;
  } catch { throw new Failure("bad_request"); }
}

Deno.serve(async req => {
  const callback = req.method === "GET";
  try {
    if (!callback && req.method !== "POST") return json({ error: "method_not_allowed" }, 405);
    rate("ip:" + (req.headers.get("cf-connecting-ip") ?? req.headers.get("x-forwarded-for")?.split(",")[0] ?? "unknown"), 60);
    const url = env("SUPABASE_URL"), service = env("SUPABASE_SERVICE_ROLE_KEY");
    if (!url || !service || !env("GOOGLE_CALENDAR_CLIENT_ID") || !env("GOOGLE_CALENDAR_CLIENT_SECRET")) throw new Failure("google_not_configured", 503);
    await key();
    const db = createClient(url, service, { auth: { persistSession: false } });
    const redirectURI = url.replace(/\/$/, "") + "/functions/v1/calendar";
    if (callback) {
      const params = new URL(req.url).searchParams, state = params.get("state") ?? "";
      if (!/^[A-Za-z0-9_-]{43}$/.test(state)) return redirect("denied");
      const hash = base64(await digest(state));
      // DELETE RETURNING consumes the state atomically, including denied callbacks.
      const row = checked(await db.from("calendar_oauth_states").delete().eq("state_hash", hash).gt("expires_at", new Date().toISOString()).select().maybeSingle());
      if (!row) return redirect("denied");
      if (params.has("error")) return redirect("denied");
      const code = params.get("code");
      if (!code || code.length > 4096) return redirect("exchange");
      let result: Obj;
      try {
        result = await token({ code, redirect_uri: redirectURI, grant_type: "authorization_code", code_verifier: await open(row.encrypted_verifier, `state:${hash}:${row.user_id}`) });
      } catch { return redirect("exchange"); }
      if (typeof result.scope !== "string" || !result.scope.split(/\s+/).includes(scope)) return redirect("scope");
      if (typeof result.refresh_token !== "string" || !result.refresh_token) return redirect("exchange");
      const stored = checked(await db.rpc("save_calendar_google_connection", { p_user_id: row.user_id, p_state_created_at: row.created_at, p_encrypted_refresh_token: await seal(result.refresh_token, `google:${row.user_id}`), p_scope: result.scope }));
      if (!stored) {
        try { await fetch("https://oauth2.googleapis.com/revoke", { method: "POST", body: new URLSearchParams({ token: result.refresh_token }), redirect: "error", signal: AbortSignal.timeout(5000) }); } catch {}
        return redirect("cancelled");
      }
      return redirect();
    }
    const authorization = req.headers.get("Authorization") ?? "";
    if (!authorization.startsWith("Bearer ")) throw new Failure("unauthorized", 401);
    const { data, error } = await db.auth.getUser(authorization.slice(7));
    if (error || !data?.user) throw new Failure("unauthorized", 401);
    const user = data.user.id;
    rate("user:" + user, 30);
    const body = await readBody(req);
    if (body.action === "google_start") {
      const state = random(), verifier = random(), hash = base64(await digest(state));
      checked(await db.from("calendar_oauth_states").delete().eq("user_id", user));
      checked(await db.from("calendar_oauth_states").insert({ state_hash: hash, user_id: user, encrypted_verifier: await seal(verifier, `state:${hash}:${user}`), expires_at: new Date(Date.now() + 600000).toISOString() }));
      const auth = new URL("https://accounts.google.com/o/oauth2/v2/auth");
      auth.search = new URLSearchParams({ client_id: env("GOOGLE_CALENDAR_CLIENT_ID"), redirect_uri: redirectURI, response_type: "code", scope, access_type: "offline", prompt: "consent", include_granted_scopes: "true", state, code_challenge_method: "S256", code_challenge: base64(await digest(verifier)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "") }).toString();
      return json({ url: auth.toString() });
    }
    const connection = checked(await db.from("calendar_connections").select().eq("user_id", user).eq("provider", "google").maybeSingle());
    const remove = async () => { checked(await db.from("calendar_connections").delete().eq("user_id", user).eq("provider", "google")); };
    if (body.action === "status") return json({ google: { connected: !!connection } });
    if (body.action === "disconnect") {
      if (connection) {
        try {
          const refresh = await open(connection.encrypted_refresh_token, `google:${user}`);
          await fetch("https://oauth2.googleapis.com/revoke", { method: "POST", body: new URLSearchParams({ token: refresh }), redirect: "error", signal: AbortSignal.timeout(5000) });
        } catch { /* Revocation is best effort; local deletion is mandatory. */ }
      }
      checked(await db.rpc("disconnect_calendar_google", { p_user_id: user }));
      return json({ ok: true });
    }
    if (body.action !== "busy") throw new Failure("bad_request");
    const start = typeof body.start === "string" ? Date.parse(body.start) : NaN;
    const requestedEnd = typeof body.end === "string" ? Date.parse(body.end) : NaN;
    if (!Number.isFinite(start) || !Number.isFinite(requestedEnd) || requestedEnd <= start || typeof body.include_titles !== "boolean") throw new Failure("bad_request");
    const end = Math.min(requestedEnd, start + 14 * 86400000);
    if (!connection) throw new Failure("google_reconnect", 409);
    let access: Obj;
    try { access = await token({ grant_type: "refresh_token", refresh_token: await open(connection.encrypted_refresh_token, `google:${user}`) }); }
    catch (error) { if (error instanceof Failure && error.code === "google_reconnect") await remove(); throw error; }
    if (typeof access.access_token !== "string" || !access.access_token) throw new Failure("google_error", 502);
    const auth = { Authorization: `Bearer ${access.access_token}`, "Content-Type": "application/json" };
    const from = new Date(start).toISOString(), to = new Date(end).toISOString();
    if (!body.include_titles) {
      const result = await google("https://www.googleapis.com/calendar/v3/freeBusy", { method: "POST", headers: auth, body: JSON.stringify({ timeMin: from, timeMax: to, items: [{ id: "primary" }] }) });
      const primary = result.calendars?.primary;
      if (!Array.isArray(primary?.busy) || primary.errors?.length) throw new Failure("google_error", 502);
      return json({ blocks: primary.busy.map((b: Obj) => ({ start: b.start, end: b.end, all_day: false })) });
    }
    const blocks: Obj[] = [];
    let page: string | undefined;
    for (let count = 0; count < 20; count++) {
      const query = new URLSearchParams({ timeMin: from, timeMax: to, singleEvents: "true", orderBy: "startTime", maxResults: "250", ...(page ? { pageToken: page } : {}) });
      const result = await google("https://www.googleapis.com/calendar/v3/calendars/primary/events?" + query, { headers: auth });
      if (!Array.isArray(result.items)) throw new Failure("google_error", 502);
      for (const event of result.items) {
        if (event.status === "cancelled" || event.transparency === "transparent" || event.attendees?.some((a: Obj) => a.self && a.responseStatus === "declined")) continue;
        const start = event.start?.dateTime ?? event.start?.date, end = event.end?.dateTime ?? event.end?.date;
        if (typeof start !== "string" || typeof end !== "string") continue;
        blocks.push({ start, end, all_day: !!event.start.date, ...(typeof event.summary === "string" ? { title: Array.from(event.summary).slice(0, 256).join("") } : {}) });
      }
      page = result.nextPageToken;
      if (!page) return json({ blocks });
    }
    throw new Failure("google_error", 502);
  } catch (error) {
    const failure = error instanceof Failure ? error : new Failure("calendar_error", 500);
    if (callback) return redirect(failure.code === "google_not_configured" ? "config" : "exchange");
    return json({ error: failure.code }, failure.status);
  }
});
