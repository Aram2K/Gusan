// Vault metadata and per-use login release. POST application/json, <=64 KiB.
// Bearer JWT -> getUser -> is_pro; deploy with verify_jwt=false.
// list/add/delete manage metadata only; pending is the only nonce disclosure.
// release consumes a nonce before sending allowlisted values directly to H;
// deny resumes with an error. Values never enter storage, logs or responses.
// sweep requires X-Gusan-Sweep-Secret (AGENT_VAULT_SWEEP_SECRET), not a JWT.
// H_company_computer_agents and optional H_AGENTS_REGION=us match agent-action.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

type Obj = Record<string, any>;
const env = (name: string) => Deno.env.get(name) ?? "";
const BASE = env("H_AGENTS_REGION") === "us"
  ? "https://agp.hcompany.ai/api/v2" : "https://agp.eu.hcompany.ai/api/v2";
const KEY = env("H_company_computer_agents");
const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type, x-gusan-privacy-mode, x-gusan-sweep-secret",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const terminal = (row: Obj) => ["completed", "failed", "cancelled", "timed_out"].includes(row.status);
class Failure extends Error {
  code: string;
  status: number;
  constructor(code: string, status = 400) { super(code); this.code = code; this.status = status; }
}
function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { ...cors, "Content-Type": "application/json" } });
}
function checked(result: Obj) {
  if (result.error) throw new Failure("storage_error", 500);
  return result.data;
}
function hostname(value: unknown) {
  if (typeof value !== "string" || value.length > 2048) throw new Failure("invalid_hostname");
  try {
    const input = value.trim();
    const u = new URL(input.includes("://") ? input : `https://${input}`);
    const host = u.hostname.toLowerCase().replace(/\.$/, "");
    // Reject explicit ports, including the scheme's default port normalized by URL.
    const authority = input.replace(/^https?:\/\//i, "").split(/[/?#]/)[0];
    if (!["https:", "http:"].includes(u.protocol) || /:/.test(authority) || u.username || u.password ||
      !/^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(host)) throw 0;
    return host;
  } catch { throw new Failure("invalid_hostname"); }
}
async function h(path: string, method = "GET", payload?: unknown) {
  try {
    const response = await fetch(`${BASE}${path}`, {
      method, redirect: "error", signal: AbortSignal.timeout(8000),
      headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
    });
    if (!response.ok && !(method === "DELETE" && response.status === 404) &&
      !(path.endsWith("/pause") && [404, 409].includes(response.status))) throw 0;
    // Never read a response body that could contain submitted credentials.
    if (method !== "GET") return null;
    return await response.json();
  } catch { throw new Failure("vault_error", 502); }
}
async function audit(db: any, row: Obj, event: string, host = "", login_id: string | null = null) {
  checked(await db.from("vault_events").insert({ user_id: row.user_id, task_id: row.id ?? null, login_id, hostname: host, event }));
}
async function cleanup(db: any, row: Obj, run: Obj) {
  if (!run.h_vault_id) return;
  let event = "vault_deleted";
  try { await h(`/vaults/${encodeURIComponent(run.h_vault_id)}`, "DELETE"); }
  catch { event = "vault_delete_failed"; }
  try {
    // Keep the vault id after a failed delete so later cleanups and the sweeper retry it.
    checked(await db.from("agent_task_runs").update({ ...(event === "vault_deleted" ? { h_vault_id: null } : {}), release_nonce: null, release_nonce_expires_at: null }).eq("task_id", row.id));
    await audit(db, row, event);
  } catch { console.log("agent-vault", "cleanup_failed"); }
}
function task(row: Obj) {
  return { task_id: row.id, status: row.status, title: row.title, login_request: row.login_request ?? null, error_code: row.error_code ?? null };
}
async function secretMatches(candidate: string, expected: string) {
  const digest = async (s: string) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  const [a, b] = await Promise.all([digest(candidate), digest(expected)]);
  let different = 0;
  for (let i = 0; i < a.length; i++) different |= a[i] ^ b[i];
  return different === 0;
}
async function sweep(db: any) {
  const pending = checked(await db.from("agent_task_runs").select().eq("session_cleanup_pending", true));
  for (const run of pending) {
    const row = checked(await db.from("agent_tasks").select().eq("id", run.task_id).maybeSingle());
    if (!row || !terminal(row) || !run.h_session_id) continue;
    try {
      const path = `/sessions/${encodeURIComponent(run.h_session_id)}`;
      await h(`${path}/pause`, "POST");
      await h(path, "DELETE");
      checked(await db.from("agent_task_runs").update({ session_cleanup_pending: false })
        .eq("task_id", run.task_id).eq("h_session_id", run.h_session_id));
    } catch { console.log("agent-vault", "session_cleanup_failed"); }
  }
  const vaults: Obj[] = [];
  for (let offset = 0;; offset += 100) {
    const page = await h(`/vaults?limit=100&offset=${offset}`);
    if (!Array.isArray(page?.vaults)) throw new Failure("vault_error", 502);
    vaults.push(...page.vaults);
    if (!page.vaults.length || offset + page.vaults.length >= page.total) break;
  }
  let deleted = 0, failed = 0;
  for (const vault of vaults) {
    const id = typeof vault.name === "string" ? vault.name.slice("gusan-task-".length) : "";
    if (!vault.name?.startsWith("gusan-task-") || !uuid.test(id)) continue;
    const row = checked(await db.from("agent_tasks").select().eq("id", id).maybeSingle());
    if (row && !terminal(row) && !(Date.now() > Date.parse(row.deadline_at) + 900000)) continue;
    let event = "vault_deleted";
    try { await h(`/vaults/${encodeURIComponent(vault.id)}`, "DELETE"); deleted++; }
    catch { event = "vault_delete_failed"; failed++; }
    if (row) {
      if (event === "vault_deleted") checked(await db.from("agent_task_runs").update({ h_vault_id: null, release_nonce: null, release_nonce_expires_at: null }).eq("task_id", row.id).eq("h_vault_id", vault.id));
      await audit(db, row, event);
    }
    // Deleted tasks have no remaining owner to associate with an audit event.
  }
  return { scanned: vaults.length, deleted, failed };
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  try {
    if (req.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json") throw new Failure("unsupported_media_type", 415);
    const cap = 64 * 1024;
    if (Number(req.headers.get("content-length")) > cap) throw new Failure("body_too_large", 413);
    const reader = req.body?.getReader(), chunks: Uint8Array[] = [];
    let length = 0;
    if (reader) for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.length;
      if (length > cap) { await reader.cancel(); throw new Failure("body_too_large", 413); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    let body: Obj;
    try { body = JSON.parse(new TextDecoder().decode(bytes)); }
    catch { throw new Failure("bad_request"); }
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Failure("bad_request");
    const isSweep = body.action === "sweep";
    let userId = "";
    if (isSweep) {
      const secret = env("AGENT_VAULT_SWEEP_SECRET");
      if (!secret) throw new Failure("not_found", 404);
      if (!await secretMatches(req.headers.get("X-Gusan-Sweep-Secret") ?? "", secret)) throw new Failure("unauthorized", 401);
    } else {
      if (["true", "1"].includes((req.headers.get("X-Gusan-Privacy-Mode") ?? "").toLowerCase()) || body.privacy_mode === true) throw new Failure("privacy_mode", 403);
      const Authorization = req.headers.get("Authorization") ?? "";
      if (!/^Bearer [A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(Authorization)) throw new Failure("unauthorized", 401);
      const supa = createClient(env("SUPABASE_URL"), env("SUPABASE_ANON_KEY"), { global: { headers: { Authorization } } });
      const { data, error } = await supa.auth.getUser();
      if (error || !data.user) throw new Failure("unauthorized", 401);
      userId = data.user.id;
      const pro = await supa.rpc("is_pro");
      if (pro.error) throw new Failure("entitlement_check_failed", 500);
      if (!pro.data) throw new Failure("pro_required", 403);
    }
    const db = createClient(env("SUPABASE_URL"), env("SUPABASE_SERVICE_ROLE_KEY"), { auth: { persistSession: false } });
    if (isSweep) {
      if (!KEY) throw new Failure("agent_not_configured", 503);
      return json(await sweep(db));
    }
    if (body.action === "list") {
      const rows = checked(await db.from("vault_logins").select("id,hostname,username_hint,has_totp,created_at,last_used_at").eq("user_id", userId).order("created_at", { ascending: false }));
      const events = checked(await db.from("vault_events").select("event,hostname,task_id,login_id,at").eq("user_id", userId).order("at", { ascending: false }).limit(50));
      return json({ logins: rows.map((r: Obj) => ({ login_id: r.id, hostname: r.hostname, username_hint: r.username_hint, has_totp: r.has_totp, created_at: r.created_at, last_used_at: r.last_used_at })), events });
    }
    if (body.action === "add") {
      const host = hostname(body.hostname);
      if (body.username_hint !== undefined && body.username_hint !== null && (typeof body.username_hint !== "string" || body.username_hint.length > 64)) throw new Failure("invalid_username_hint");
      if (body.has_totp !== undefined && typeof body.has_totp !== "boolean") throw new Failure("bad_request");
      const rows = checked(await db.from("vault_logins").select("id,hostname").eq("user_id", userId));
      if (rows.length >= 50 && !rows.some((r: Obj) => r.hostname === host)) throw new Failure("login_limit", 409);
      const saved = await db.from("vault_logins").upsert({ user_id: userId, hostname: host, ...(body.username_hint !== undefined ? { username_hint: body.username_hint } : {}), ...(body.has_totp !== undefined ? { has_totp: body.has_totp } : {}) }, { onConflict: "user_id,hostname" }).select().single();
      if (saved.error?.code === "P0001") throw new Failure("login_limit", 409);
      const login = checked(saved);
      await audit(db, { user_id: userId }, "added", host, login.id);
      return json({ login_id: login.id, hostname: login.hostname, username_hint: login.username_hint, has_totp: login.has_totp, created_at: login.created_at, last_used_at: login.last_used_at });
    }
    if (body.action === "delete") {
      if (!uuid.test(body.login_id ?? "")) throw new Failure("bad_request");
      const login = checked(await db.from("vault_logins").delete().eq("id", body.login_id).eq("user_id", userId).select().maybeSingle());
      if (!login) throw new Failure("not_found", 404);
      await audit(db, { user_id: userId }, "deleted", login.hostname, login.id);
      return json({ deleted: true });
    }
    if (body.action === "pending") {
      const rows = checked(await db.from("agent_tasks").select().eq("user_id", userId).eq("status", "awaiting_login"));
      const pending = [];
      for (const row of rows) {
        const run = checked(await db.from("agent_task_runs").select().eq("task_id", row.id).maybeSingle());
        if (run?.release_nonce && Date.parse(run.release_nonce_expires_at) > Date.now()) pending.push({ task_id: row.id, title: row.title, hostname: run.tool_req.hostname, page_url: run.tool_req.page_url, login_id: run.tool_req.login_id, release_nonce: run.release_nonce, expires_at: run.release_nonce_expires_at });
      }
      return json({ pending });
    }
    if (!["release", "deny"].includes(body.action)) throw new Failure("bad_request");
    if (!uuid.test(body.task_id ?? "") || !uuid.test(body.release_nonce ?? "")) throw new Failure("bad_request");
    if (body.action === "release") {
      if (!uuid.test(body.login_id ?? "")) throw new Failure("bad_request");
      const fields = body.values;
      if (!fields || typeof fields !== "object" || Array.isArray(fields) || !Object.keys(fields).length ||
        Object.entries(fields).some(([key, value]) => !["username", "password", "email", "totp", "api_key"].includes(key) || typeof value !== "string" || value.length < 1 || value.length > 4096)) throw new Failure("invalid_fields");
    }
    if (!KEY) throw new Failure("agent_not_configured", 503);
    const row = checked(await db.from("agent_tasks").select().eq("id", body.task_id).eq("user_id", userId).maybeSingle());
    if (!row) throw new Failure("not_found", 404);
    if (row.status !== "awaiting_login") throw new Failure("not_awaiting_login", 409);
    const run = checked(await db.from("agent_task_runs").select().eq("task_id", row.id).maybeSingle());
    if (!run?.h_vault_id || !run.tool_req?.call || run.release_nonce !== body.release_nonce ||
      !(Date.parse(run.release_nonce_expires_at) > Date.now()) || run.release_count >= 3) throw new Failure("invalid_nonce", 409);
    let login: Obj | null = null;
    if (body.action === "release") {
      login = checked(await db.from("vault_logins").select().eq("id", body.login_id).eq("user_id", userId).maybeSingle());
      if (!login || login.id !== run.tool_req.login_id || login.hostname !== run.tool_req.hostname) throw new Failure("login_mismatch", 409);
    }
    const claimed = checked(await db.from("agent_task_runs").update({ release_nonce: null, release_nonce_expires_at: null,
      ...(body.action === "release" ? { release_count: run.release_count + 1 } : {}) })
      .eq("task_id", row.id).eq("release_nonce", body.release_nonce).eq("release_count", run.release_count)
      .lt("release_count", 3).gt("release_nonce_expires_at", new Date().toISOString()).eq("h_vault_id", run.h_vault_id).select().maybeSingle());
    if (!claimed) throw new Failure("invalid_nonce", 409);
    try {
      // A cancellation after nonce claim must not recreate deleted credentials.
      const current = checked(await db.from("agent_tasks").select().eq("id", row.id).eq("user_id", userId).maybeSingle());
      if (!current || current.status !== "awaiting_login") { await cleanup(db, row, run); throw new Failure("not_awaiting_login", 409); }
      if (body.action === "release") {
        await h(`/vaults/${encodeURIComponent(run.h_vault_id)}/items`, "POST", { name: `login-${login!.id}`, urls: [`https://${login!.hostname}`], values: body.values });
        await h(`/sessions/${encodeURIComponent(run.h_session_id)}/tool_results`, "POST", {
          kind: "tool_result", tool_req: run.tool_req.call,
          result: `A saved login for ${login!.hostname} is available. Use fill_secret_at for the username/password fields; never type them.`,
        });
        checked(await db.from("vault_logins").update({ last_used_at: new Date().toISOString() }).eq("id", login!.id).eq("user_id", userId));
      } else {
        await h(`/sessions/${encodeURIComponent(run.h_session_id)}/tool_results`, "POST", { kind: "error_event", origin: "custom_tools", tool_req: run.tool_req.call, error: "The user declined to share the login." });
      }
      const updated = checked(await db.from("agent_tasks").update({ status: "running", login_request: null, updated_at: new Date().toISOString(), deadline_at: new Date(Date.now() + 780000).toISOString() }).eq("id", row.id).eq("user_id", userId).eq("status", "awaiting_login").select().maybeSingle());
      await audit(db, row, body.action === "release" ? "released" : "denied", run.tool_req.hostname, run.tool_req.login_id);
      if (!updated) { await cleanup(db, row, run); return json(task(checked(await db.from("agent_tasks").select().eq("id", row.id).maybeSingle()) ?? row)); }
      return json(task(updated));
    } catch (error) {
      if (error instanceof Failure && error.code === "not_awaiting_login") throw error;
      let failed = { ...row, status: "failed", login_request: null, error_code: "vault_error" };
      try {
        failed = checked(await db.from("agent_tasks").update({ status: "failed", error_code: "vault_error", login_request: null, finished_at: new Date().toISOString(), updated_at: new Date().toISOString() }).eq("id", row.id).eq("user_id", userId).eq("status", "awaiting_login").select().maybeSingle()) ?? failed;
      } finally { await cleanup(db, row, run); }
      return json(task(failed));
    }
  } catch (error) {
    const code = error instanceof Failure ? error.code : "internal";
    console.log("agent-vault", code);
    return json({ error: code }, error instanceof Failure ? error.status : 500);
  }
});
