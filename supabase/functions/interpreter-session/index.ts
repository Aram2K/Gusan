// Gusan Interpreter sessions - live speech-to-speech translation in calls.
//
// The app streams the audio straight to ElevenLabs (Scribe v2 Realtime for the words, the text-to-
// dialogue WebSocket /v1/text-to-dialogue/stream-input for the voice - the text-to-speech socket
// refuses eleven_v4_turbo) with SINGLE-USE tokens minted here, so the key never reaches the PC.
// The voice tokens are of type ttd_websocket (a tts_websocket token is refused by that socket);
// the answer still calls them tts_tokens, as the contract names them.
//
// This function also keeps the monthly allowance, on the SERVER's clock: each heartbeat, stop and
// reconnect token counts the time since the session's last beat, at most MAX_GAP_S at once (a longer
// gap means the app was not running: the PC slept, or was offline). The seconds the app reports are
// checked and logged, never counted, so a client that reports 0 still pays for the time that passed.
// What no formula here can count: a modified app that never calls again while its sockets stay open
// (the audio goes straight to ElevenLabs). The daily token ceiling and a credit limit on the
// ElevenLabs key bound that.
//
// Contract: docs/redesign/interpreter-spec.md 8.1. POST, JSON, Authorization: Bearer <access token>.
//   start      {"action":"start","hear":{"from":"ja","to":"en"},"speak":{"from":"en","to":"ja"}}
//              (either side may be missing = that direction is off) ->
//              {session_id, seconds_left, allowance_seconds, stt_model, tts_model,
//               scribe_tokens:{hear?,speak?}, tts_tokens:{hear?,speak?}, voices:{hear?,speak?}, own_voice}
//              One Scribe and one TTS token per direction that is on, minted in parallel. HEAR speaks
//              with the preset voice; SPEAK with the person's own cloned voice (interpreter_voices)
//              when there is one, else the preset (own_voice says which).
//   token      {"action":"token","session_id","kind":"scribe"|"tts"} -> {token}   (a reconnect: the
//              session must be the caller's, still open, under 12 hours old, with time left; it
//              counts as a heartbeat)
//   heartbeat  {"action":"heartbeat","session_id","seconds":n} -> {seconds_left}
//   stop       {"action":"stop","session_id","seconds":n} -> {seconds_left}   (closes the session)
//              Counted: the whole seconds since the last beat on the server's clock, at most
//              MAX_GAP_S; the part of a second left over counts with the next beat. n must be a
//              number from 0 up (or absent); it is logged, not counted. Two beats that arrive at the
//              same time are counted once (the update only lands if no other beat got in first).
//              A closed session counts nothing more; stop is safe to repeat.
// Errors {"error": code}: auth 401, pro_required 402, allowance_used 429 (+ seconds_left: 0),
// quota_exceeded 429 (the daily token ceiling, TOKEN_DAILY_CAP), bad_request 400 (+ detail),
// provider_credits 402, provider_unavailable 503; a database failure is a 500 (server_error /
// entitlement_check_failed).
//
// Who may: start and token need Pro - is_pro(), the rule smart-action and transcribe-proxy use for
// Pro features (a paid subscription, a lifetime purchase or admin) - and time left this month.
// heartbeat and stop only need the session's owner: a session is always counted and closed, even
// when Pro lapsed meanwhile.
//
// Tables (migration 202611070001_interpreter.sql): interpreter_sessions and interpreter_voices are
// written here with the service role; the allowance goes through use_interpreter_seconds() (service
// role only), which adds the seconds to the user's UTC month and returns the seconds left. The
// tokens go through use_interpreter_tokens() (migration 202611090001, service role only; apply it
// before deploying this version).
//
// Secrets: ELEVENLABS_KEY (or ELEVENLABS_API_KEY / ELEVEN_API_KEY / XI_API_KEY, see _shared/elevenlabs.ts),
//          INTERPRETER_MONTHLY_SECONDS (default 18000 = 5 h a month),
//          INTERPRETER_PRESET_VOICE (a premade multilingual voice id, default SAz9YHcvj6GT2YYXdXww "River", tested 2026-11-07),
//          INTERPRETER_TTS_MODEL (default eleven_v4_turbo; a value that is not a plain model id is ignored).
// Auto-provided: SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY. Deploy with verify_jwt=false.
// Logs: one line per start / stop (directions, seconds, timings), never a token, a user id or audio.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { elevenKey, failureStatus, mintToken } from "../_shared/elevenlabs.ts";

const JWT_RE = /^[\w-]+\.[\w-]+\.[\w-]+$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// "en", "pt-BR", "yue"; the side a person hears may also be "auto" (whatever they speak).
const LANG_RE = /^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$/;
const MAX_BODY_BYTES = 16 * 1024;
const TOKEN_TIMEOUT_MS = 5_000;        // a token is minted in ~0.2 s; a stall must not hold the start
const MAX_REPORT_S = 24 * 3600;        // the app's own number is only checked and logged
// One beat counts at most this (three heartbeats): a longer gap means the app was not running (the
// PC slept or was offline), and one or two failed heartbeats are still counted in full.
const MAX_GAP_S = 90;
const SESSION_MAX_MS = 12 * 3600_000;  // no new tokens for a session older than this (like live_sessions)
// A cost-abuse ceiling, not a product quota: ElevenLabs single-use tokens per user per UTC day. A
// start takes 2 per direction that is on and a reconnect 1, so a heavy day of calls is well under 100.
const TOKEN_DAILY_CAP = 500;
const DEFAULT_MONTHLY_SECONDS = 18_000;
const DEFAULT_PRESET_VOICE = "SAz9YHcvj6GT2YYXdXww";   // River (premade); spoke en/ja/hy on the dialogue socket in the M0 test
const DEFAULT_TTS_MODEL = "eleven_v4_turbo";
const STT_MODEL = "scribe_v2_realtime";
// The token type of the voice socket the app opens (text-to-dialogue stream-input, which takes v4 Turbo).
const VOICE_TOKEN = "ttd_websocket";
const DIRECTIONS = ["hear", "speak"] as const;
type Direction = typeof DIRECTIONS[number];

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

function bad(detail: string) {
  return json({ error: "bad_request", detail }, 400);
}

// Secrets are read per request (cheap, and a changed secret needs no redeploy).
function env(name: string): string {
  return (Deno.env.get(name) ?? "").trim();
}

function allowanceSeconds(): number {
  const raw = env("INTERPRETER_MONTHLY_SECONDS");
  const n = raw ? Number(raw) : NaN;
  return Number.isFinite(n) && n >= 0 && n <= 100_000_000 ? Math.floor(n) : DEFAULT_MONTHLY_SECONDS;
}

function presetVoice(): string {
  const v = env("INTERPRETER_PRESET_VOICE");
  return /^[A-Za-z0-9]{8,40}$/.test(v) ? v : DEFAULT_PRESET_VOICE;
}

function ttsModel(): string {
  const v = env("INTERPRETER_TTS_MODEL");
  return /^[a-z0-9][a-z0-9._-]{1,60}$/i.test(v) ? v : DEFAULT_TTS_MODEL;
}

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

// One direction of the start request: missing (null / absent / false) = off.
function direction(raw: unknown): { from: string; to: string } | null | "bad" {
  if (raw === undefined || raw === null || raw === false) return null;
  if (typeof raw !== "object" || Array.isArray(raw)) return "bad";
  const { from, to } = raw as { from?: unknown; to?: unknown };
  if (typeof from !== "string" || typeof to !== "string") return "bad";
  if (!(from === "auto" || LANG_RE.test(from)) || !LANG_RE.test(to)) return "bad";
  return { from, to };
}

// The seconds a beat reports (logged, not counted): absent = 0; anything but a number from 0 up is refused.
function reported(v: unknown): number | null {
  if (v === undefined || v === null) return 0;
  if (typeof v !== "number" || !Number.isFinite(v) || v < 0) return null;
  return Math.min(Math.floor(v), MAX_REPORT_S);
}

// What a beat counts: the server's own clock since the last beat, in whole seconds, at most
// MAX_GAP_S. The app's number plays no part: reporting 0 (or nothing) still pays for the time.
export function countedSeconds(lastBeatMs: number, nowMs: number): number {
  if (!Number.isFinite(lastBeatMs)) return 0;
  return Math.min(Math.max(0, Math.floor((nowMs - lastBeatMs) / 1000)), MAX_GAP_S);
}

// deno-lint-ignore no-explicit-any
type Db = any;
type OpenSession = { id: string; last_beat_at: string; seconds: number };

// Counts the time since the session's last beat into the session (stop also closes it) and returns
// the seconds counted: 0 when another beat counted this stretch first, null when the database
// failed. The part of a second not counted waits for the next beat (beats a second apart still add
// up); after a gap of MAX_GAP_S or more the clock starts again from now.
async function bill(svc: Db, s: OpenSession, stop: boolean): Promise<number | null> {
  const nowMs = Date.now();
  const lastMs = Date.parse(s.last_beat_at);
  let counted = countedSeconds(lastMs, nowMs);
  const nextMs = Number.isFinite(lastMs) && counted < MAX_GAP_S ? lastMs + counted * 1000 : nowMs;
  const patch: Record<string, unknown> = { seconds: (Number(s.seconds) || 0) + counted, last_beat_at: iso(nextMs) };
  if (stop) patch.ended_at = iso(nowMs);
  // Lands only if no other beat came in since this one read the row (same last_beat_at).
  const upd = await svc.from("interpreter_sessions").update(patch)
    .eq("id", s.id).eq("last_beat_at", s.last_beat_at).is("ended_at", null).select("id");
  if (upd.error) return null;
  if (!upd.data?.length) {
    counted = 0;                 // the other beat counted this stretch of time
    if (stop) {
      const end = await svc.from("interpreter_sessions").update({ ended_at: iso(nowMs) })
        .eq("id", s.id).is("ended_at", null);
      if (end.error) return null;
    }
  }
  return counted;
}

// Counts n single-use tokens against the user's daily ceiling: true = within it, null = the
// database failed.
async function useTokens(svc: Db, userId: string, n: number): Promise<boolean | null> {
  const { data, error } = await svc.rpc("use_interpreter_tokens",
                                        { p_user_id: userId, p_n: n, p_max_per_day: TOKEN_DAILY_CAP });
  if (error || typeof data !== "boolean") {
    console.error("interpreter token ceiling failed", String(error?.message ?? "no answer").slice(0, 120));
    return null;
  }
  return data;
}

// Adds `seconds` to the user's month and returns the seconds left (null: the database failed).
async function useSeconds(svc: Db, userId: string, seconds: number, allowance: number): Promise<number | null> {
  const { data, error } = await svc.rpc("use_interpreter_seconds",
                                        { p_user_id: userId, p_seconds: seconds, p_allowance: allowance });
  if (error || typeof data !== "number") {
    console.error("interpreter usage failed", String(error?.message ?? "no number").slice(0, 120));
    return null;
  }
  return Math.max(0, Math.floor(data));
}

async function start(svc: Db, userId: string, body: Record<string, unknown>): Promise<Response> {
  const asked = { hear: direction(body.hear), speak: direction(body.speak) };
  if (asked.hear === "bad" || asked.speak === "bad") return bad("languages");
  const on: Direction[] = DIRECTIONS.filter((d) => asked[d] !== null);
  if (!on.length) return bad("no_direction");

  const startedAt = Date.now();
  const allowance = allowanceSeconds();
  const [left, voiceRes, within] = await Promise.all([
    useSeconds(svc, userId, 0, allowance),
    asked.speak ? svc.from("interpreter_voices").select("voice_id").eq("user_id", userId).maybeSingle()
                : Promise.resolve({ data: null, error: null }),
    useTokens(svc, userId, on.length * 2),      // a Scribe and a voice token per direction
  ]);
  if (left === null || within === null) return json({ error: "server_error" }, 500);
  if (left <= 0) return json({ error: "allowance_used", seconds_left: 0 }, 429);
  if (!within) return json({ error: "quota_exceeded" }, 429);
  if (!elevenKey()) {
    console.error("interpreter start: no ElevenLabs key (set ELEVENLABS_KEY)");
    return json({ error: "provider_unavailable" }, 503);
  }
  // A voice row that can't be read is no reason to fail the call: the preset voice speaks instead.
  if (voiceRes.error) console.error("interpreter voice lookup failed", String(voiceRes.error.message ?? "").slice(0, 120));
  const ownVoice = typeof voiceRes.data?.voice_id === "string" ? voiceRes.data.voice_id : "";

  // The tokens and the session row at the same time: the start is one round trip to each.
  const sessionId = crypto.randomUUID();
  const now = iso(startedAt);
  const jobs = on.flatMap((d) => [
    mintToken("realtime_scribe", TOKEN_TIMEOUT_MS).then((r) => ({ d, kind: "scribe", r })),
    mintToken(VOICE_TOKEN, TOKEN_TIMEOUT_MS).then((r) => ({ d, kind: "tts", r })),
  ]);
  const [minted, ins] = await Promise.all([
    Promise.all(jobs),
    svc.from("interpreter_sessions").insert({ id: sessionId, user_id: userId, started_at: now, last_beat_at: now,
                                              seconds: 0 }),
  ]);
  const failed = minted.filter((m) => !m.r.ok);
  if (ins.error) {
    console.error("interpreter session insert failed", String(ins.error.message ?? "").slice(0, 120));
    return json({ error: "server_error" }, 500);
  }
  if (failed.length) {
    // Never leave a session open that the app cannot use.
    await svc.from("interpreter_sessions").update({ ended_at: now }).eq("id", sessionId);
    const code = failed.some((m) => !m.r.ok && m.r.code === "provider_credits") ? "provider_credits"
                                                                               : "provider_unavailable";
    return json({ error: code }, failureStatus(code));
  }

  const scribe: Record<string, string> = {};
  const tts: Record<string, string> = {};
  for (const m of minted) if (m.r.ok) (m.kind === "scribe" ? scribe : tts)[m.d] = m.r.token;
  const preset = presetVoice();
  const voices: Record<string, string> = {};
  if (asked.hear) voices.hear = preset;
  if (asked.speak) voices.speak = ownVoice || preset;
  const own = !!asked.speak && !!ownVoice;
  console.log(JSON.stringify({ interpreter_start: on, own_voice: own, seconds_left: left,
                               ms: Date.now() - startedAt }));
  return json({
    session_id: sessionId,
    seconds_left: left,
    allowance_seconds: allowance,
    stt_model: STT_MODEL,
    tts_model: ttsModel(),
    scribe_tokens: scribe,
    tts_tokens: tts,
    voices,
    own_voice: own,
  });
}

async function token(svc: Db, userId: string, body: Record<string, unknown>): Promise<Response> {
  const sid = typeof body.session_id === "string" ? body.session_id.trim() : "";
  if (!UUID_RE.test(sid)) return bad("session_id");
  if (body.kind !== "scribe" && body.kind !== "tts") return bad("kind");
  const allowance = allowanceSeconds();
  const [sess, left, within] = await Promise.all([
    svc.from("interpreter_sessions").select("id, started_at, last_beat_at, seconds, ended_at").eq("id", sid)
       .eq("user_id", userId).maybeSingle(),
    useSeconds(svc, userId, 0, allowance),
    useTokens(svc, userId, 1),
  ]);
  if (sess.error || left === null || within === null) return json({ error: "server_error" }, 500);
  const s = sess.data;
  if (!s || s.ended_at || !(Date.now() - Date.parse(s.started_at) < SESSION_MAX_MS)) return bad("session_closed");
  if (left <= 0) return json({ error: "allowance_used", seconds_left: 0 }, 429);
  if (!within) return json({ error: "quota_exceeded" }, 429);
  // A reconnect counts as a heartbeat, so an app that stops beating still pays while it reconnects.
  // Counted while the token is minted: the reconnect is not slower.
  const charge = async (): Promise<number | null> => {
    const counted = await bill(svc, s, false);
    if (counted === null) return null;
    return counted > 0 ? await useSeconds(svc, userId, counted, allowance) : left;
  };
  const [m, leftNow] = await Promise.all([
    mintToken(body.kind === "scribe" ? "realtime_scribe" : VOICE_TOKEN, TOKEN_TIMEOUT_MS),
    charge(),
  ]);
  if (leftNow === null) return json({ error: "server_error" }, 500);
  if (leftNow <= 0) return json({ error: "allowance_used", seconds_left: 0 }, 429);
  if (!m.ok) return json({ error: m.code }, failureStatus(m.code));
  return json({ token: m.token });
}

async function beat(svc: Db, userId: string, body: Record<string, unknown>, stop: boolean): Promise<Response> {
  const sid = typeof body.session_id === "string" ? body.session_id.trim() : "";
  if (!UUID_RE.test(sid)) return bad("session_id");
  const n = reported(body.seconds);
  if (n === null) return bad("seconds");
  const { data: s, error } = await svc.from("interpreter_sessions")
    .select("id, last_beat_at, seconds, ended_at").eq("id", sid).eq("user_id", userId).maybeSingle();
  if (error) return json({ error: "server_error" }, 500);
  if (!s) return bad("no_session");

  let counted = 0;
  if (!s.ended_at) {
    const c = await bill(svc, s, stop);
    if (c === null) return json({ error: "server_error" }, 500);
    counted = c;
  }
  const left = await useSeconds(svc, userId, counted, allowanceSeconds());
  if (left === null) return json({ error: "server_error" }, 500);
  if (stop && !s.ended_at) {
    // reported: what the app said this last stretch took (it should be close to counted)
    console.log(JSON.stringify({ interpreter_stop: true, session_seconds: (Number(s.seconds) || 0) + counted,
                                 counted, reported: n, seconds_left: left }));
  }
  return json({ seconds_left: left });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  // A user's access token is a JWT: anything else is refused before the body is read.
  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ") || !JWT_RE.test(authHeader.slice(7).trim())) {
    return json({ error: "auth" }, 401);
  }
  if (Number(req.headers.get("content-length") ?? 0) > MAX_BODY_BYTES) return bad("too_large");
  // deno-lint-ignore no-explicit-any
  let body: any;
  try {
    const raw = await req.text();
    if (raw.length > MAX_BODY_BYTES) return bad("too_large");
    body = JSON.parse(raw);
  } catch { return bad("json"); }
  if (!body || typeof body !== "object" || Array.isArray(body)) return bad("json");
  const action = body.action;
  if (action !== "start" && action !== "token" && action !== "heartbeat" && action !== "stop") return bad("action");

  const supa = createClient(env("SUPABASE_URL"), env("SUPABASE_ANON_KEY"),
                            { global: { headers: { Authorization: authHeader } } });
  const needsPro = action === "start" || action === "token";
  const [userRes, proRes] = await Promise.all([
    supa.auth.getUser(),
    needsPro ? supa.rpc("is_pro") : Promise.resolve({ data: true, error: null }),
  ]);
  if (userRes.error || !userRes.data?.user) return json({ error: "auth" }, 401);
  if (proRes.error) return json({ error: "entitlement_check_failed" }, 500);
  if (proRes.data !== true) return json({ error: "pro_required" }, 402);

  const serviceKey = env("SUPABASE_SERVICE_ROLE_KEY");
  if (!serviceKey) {
    console.error("interpreter-session: SUPABASE_SERVICE_ROLE_KEY missing");
    return json({ error: "server_error" }, 500);
  }
  const svc = createClient(env("SUPABASE_URL"), serviceKey, { auth: { persistSession: false } });
  const userId = String(userRes.data.user.id);
  if (action === "start") return start(svc, userId, body);
  if (action === "token") return token(svc, userId, body);
  return beat(svc, userId, body, action === "stop");
});
