"""The Gusan Interpreter's server side: interpreter-session, voice-clone, smart-action mode "interpret",
delete-account's voice clean-up, and the migration / analytics allow-list they need.

The REAL edge-function sources run under Node (type stripping) the way tests/test_smart_action_routes.py
runs smart-action: Deno, Supabase (a small in-memory database + the RPCs), the Anthropic SDK and fetch
(ElevenLabs and the chat engines) are faked. Every scenario gets a fresh copy of its function; one Node
process runs them all. The clock (Date.now) can be moved forward per request, so the heartbeat
arithmetic is checked against a known wall clock.
"""
import base64
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FUNCS = ROOT / "supabase" / "functions"
SOURCES = {
    "session": FUNCS / "interpreter-session" / "index.ts",
    "voice": FUNCS / "voice-clone" / "index.ts",
    "smart": FUNCS / "smart-action" / "index.ts",
    "delete": FUNCS / "delete-account" / "index.ts",
}
SHARED = FUNCS / "_shared" / "elevenlabs.ts"
MIGRATION = ROOT / "supabase" / "migrations" / "202611070001_interpreter.sql"
MIGRATION_FIXES = ROOT / "supabase" / "migrations" / "202611090001_interpreter_ceilings_voice_swap.sql"
ANALYTICS = FUNCS / "transcribe-analytics" / "index.ts"

ELEVEN_KEY = "sk_eleven_TEST_KEY_000111"
USER = "0f8e2c1a-1111-4222-8333-444455556666"
OTHER = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
SID_OTHER = "11111111-2222-4333-8444-555555555555"
SID_OLD = "22222222-3333-4444-8555-666666666666"
PRESET = "SAz9YHcvj6GT2YYXdXww"
LONG_AGO = "2020-01-01T00:00:00.000Z"


def _node_can_strip_types():
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout
        major, minor = [int(x) for x in out.strip().lstrip("v").split(".")[:2]]
    except Exception:
        return None
    return node if (major, minor) >= (22, 6) else None


# Supabase: getUser (the token "x.y.rejected" or no user = signed out), the RPCs the functions call, and
# a query builder over in-memory tables (select / insert / update / upsert / delete with eq / is filters).
# A failing table answers {message: "db down"}, with fail's value as the error code when it is a string.
STUB_SUPABASE = r"""
const S = () => globalThis.__state;
class Query {
  constructor(t) { this.t = t; this.f = []; this.op = "select"; this.one = null; this.ret = false; }
  select() { if (this.op !== "select") this.ret = true; return this; }
  eq(c, v) { this.f.push([c, v]); return this; }
  is(c, v) { this.f.push([c, v]); return this; }
  order() { return this; }
  limit() { return this; }
  insert(r) { this.op = "insert"; this.rows = [].concat(r); return this; }
  upsert(r, o) { this.op = "upsert"; this.rows = [].concat(r); this.key = o?.onConflict ?? "id"; return this; }
  update(p) { this.op = "update"; this.patch = p; return this; }
  delete() { this.op = "delete"; return this; }
  maybeSingle() { this.one = "maybe"; return this; }
  single() { this.one = "single"; return this; }
  hit(r) { return this.f.every(([c, v]) => (r[c] ?? null) === v); }
  run() {
    const s = S();
    s.log.push([this.op, this.t]);
    const f = s.fail[`${this.t}:${this.op}`] || s.fail[this.t];
    if (f) return { data: null, error: { message: "db down", ...(typeof f === "string" ? { code: f } : {}) } };
    const rows = (s.db[this.t] ??= []);
    let out;
    if (this.op === "select") out = rows.filter((r) => this.hit(r));
    else if (this.op === "insert") { for (const r of this.rows) rows.push({ ...r }); out = this.rows; }
    else if (this.op === "upsert") {
      for (const r of this.rows) {
        const i = rows.findIndex((x) => x[this.key] === r[this.key]);
        if (i >= 0) rows[i] = { ...rows[i], ...r }; else rows.push({ ...r });
      }
      out = this.rows;
    } else if (this.op === "update") {
      out = [];
      for (const r of rows) if (this.hit(r)) { Object.assign(r, this.patch); out.push(r); }
    } else {
      out = rows.filter((r) => this.hit(r));
      s.db[this.t] = rows.filter((r) => !this.hit(r));
    }
    out = structuredClone(out);
    if (this.one === "maybe") return { data: out[0] ?? null, error: null };
    if (this.one === "single") return out.length === 1 ? { data: out[0], error: null } : { data: null, error: { message: "not one" } };
    return { data: this.op === "select" || this.ret ? out : null, error: null };
  }
  then(a, b) { return Promise.resolve().then(() => this.run()).then(a, b); }
}
export function createClient(_url, key, opts) {
  const auth = opts?.global?.headers?.Authorization ?? "";
  return {
    auth: {
      getUser: async () => {
        const s = S();
        return s.user && auth !== "Bearer x.y.rejected" ? { data: { user: { id: s.user } }, error: null }
                                                        : { data: { user: null }, error: { message: "bad jwt" } };
      },
      admin: { deleteUser: async (id) => { S().log.push(["deleteUser", id]); S().deletedUsers.push(id); return { error: null }; } },
    },
    rpc: async (name, args) => {
      const s = S();
      s.rpc.push([name, args ?? null, key === "service-key" ? "service" : "user"]);
      s.log.push(["rpc", name]);
      if (s.fail[name]) return { data: null, error: { message: "boom" } };
      if (name === "is_pro") return { data: !!s.pro, error: null };
      if (name === "use_smart_action_quota") return { data: true, error: null };
      if (name === "use_interpret_quota") return { data: s.interpretQuota, error: null };
      if (name === "use_interpreter_seconds") {
        const used = (s.usage[args.p_user_id] ?? 0) + Math.max(0, args.p_seconds);
        s.usage[args.p_user_id] = used;
        return { data: Math.max(args.p_allowance - used, 0), error: null };
      }
      if (name === "use_interpreter_tokens") {
        const used = (s.tokens[args.p_user_id] ?? 0) + Math.max(0, args.p_n);
        s.tokens[args.p_user_id] = used;
        return { data: used <= args.p_max_per_day, error: null };
      }
      if (name === "use_voice_preview_chars") {
        const used = (s.previewChars[args.p_user_id] ?? 0) + Math.max(0, args.p_chars);
        s.previewChars[args.p_user_id] = used;
        return { data: used <= args.p_max_per_day, error: null };
      }
      if (name === "swap_interpreter_voice") {
        // One step, like the real one (which holds a per-user lock): save the row, return the old id.
        const rows = (s.db.interpreter_voices ??= []);
        const i = rows.findIndex((r) => r.user_id === args.p_user_id);
        const prev = i >= 0 ? rows[i].voice_id : null;
        const row = { user_id: args.p_user_id, provider: "elevenlabs", voice_id: args.p_voice_id,
                      source: args.p_source, consent_at: args.p_created_at, consent_phrase: args.p_consent_phrase,
                      created_at: args.p_created_at };
        if (i >= 0) rows[i] = row; else rows.push(row);
        return { data: prev, error: null };
      }
      return { data: null, error: { message: "unknown rpc " + name } };
    },
    from: (t) => new Query(t),
  };
}
"""

# The Anthropic SDK: smart-action imports it; the Interpreter chain never reaches Claude.
STUB_ANTHROPIC = r"""
export class APIError extends Error { constructor(status, msg) { super(msg); this.status = status; } }
export default class Anthropic {
  static APIError = APIError;
  static AuthenticationError = class extends APIError {};
  static PermissionDeniedError = class extends APIError {};
  static NotFoundError = class extends APIError {};
  constructor() {
    this.models = { retrieve: async (id) => ({ id }) };
    this.messages = { create: async () => { throw new Error("claude must not run"); },
                      stream: () => { throw new Error("claude must not run"); } };
  }
}
"""

HARNESS = r"""
import { readFileSync } from "node:fs";
import util from "node:util";
const [, , modDir, scenariosPath] = process.argv;
const scenarios = JSON.parse(readFileSync(scenariosPath, "utf-8"));
const CHAT = { "live.test": "live", "api.openai.com": "openai", "api.anthropic.com": "anthropic",
               "api.mistral.ai": "mistral", "generativelanguage.googleapis.com": "gemini" };
const realLog = { log: console.log, warn: console.warn, error: console.error };
const realSetTimeout = globalThis.setTimeout, realClearTimeout = globalThis.clearTimeout;
// The armed timers (an attempt of smart-action arms one: its budget), read when a request goes out.
const active = new Map();
globalThis.setTimeout = (fn, ms, ...a) => {
  const id = realSetTimeout(() => { active.delete(id); fn(...a); }, Math.min(ms ?? 0, 250), ...a);
  active.set(id, ms);
  return id;
};
globalThis.clearTimeout = (id) => { active.delete(id); realClearTimeout(id); };
// A clock the scenarios can move forward.
const realNow = Date.now.bind(Date);
let skew = 0;
Date.now = () => realNow() + skew;

function kindOf(u, method) {
  const url = new URL(u);
  if (url.host === "api.elevenlabs.io") {
    const p = url.pathname;
    if (p.startsWith("/v1/single-use-token/")) return "token_" + p.slice("/v1/single-use-token/".length);
    if (p === "/v1/voices/add") return "voice_add";
    if (p.startsWith("/v1/voices/") && method === "DELETE") return "voice_delete";
    if (p.startsWith("/v1/text-to-speech/")) return "tts";
    return "eleven";
  }
  return CHAT[url.host] ?? "other";
}

const calls = [];
let queues = {};
let counter = 0;
globalThis.fetch = async (url, init = {}) => {
  const u = String(url), method = init.method ?? "GET";
  const kind = kindOf(u, method);
  let body = null;
  if (typeof init.body === "string") { try { body = JSON.parse(init.body); } catch { body = init.body; } }
  else if (init.body instanceof FormData) {
    body = [...init.body.entries()].map(([k, v]) => typeof v === "string" ? [k, v] : [k, { file: v.name, size: v.size, type: v.type }]);
  }
  const h = init.headers ?? {};
  calls.push({ kind, method, url: u, body, xi: h["xi-api-key"] ?? null, auth: h.Authorization ?? null,
               redirect: init.redirect ?? null, budget: active.size ? [...active.values()][0] : 0 });
  globalThis.__state.log.push(["fetch", kind]);
  const q = queues[kind];
  const r = q && q.length ? (q.length > 1 ? q.shift() : q[0]) : {};
  if (r.advance_ms) skew += r.advance_ms;          // a slow answer: the clock moves on
  // another device changed these tables while this answer was on its way
  for (const [t, rows] of Object.entries(r.db ?? {})) globalThis.__state.db[t] = structuredClone(rows);
  if (r.throw) throw Object.assign(new Error(r.throw), { name: r.name ?? "TypeError" });
  if (r.stall) {
    return new Promise((_res, rej) => init.signal?.addEventListener("abort", () => rej(Object.assign(new Error("aborted"), { name: "AbortError" }))));
  }
  const status = r.status ?? 200;
  if (status !== 200) return new Response(r.text ?? "boom", { status });
  counter++;
  if (kind.startsWith("token_")) return Response.json(r.json ?? { token: `${kind.slice(6)}-${counter}` });
  if (kind === "voice_add") return Response.json(r.json ?? { voice_id: "voice-new", requires_verification: false });
  if (kind === "voice_delete") return Response.json({ status: "ok" });
  if (kind === "tts") return new Response(new Uint8Array(r.bytes ?? [0, 1, 2, 3, 250, 251]));
  const text = r.empty ? "" : `${kind} words`;
  if (body?.stream) {
    return new Response(`data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\ndata: [DONE]\n\n`,
                        { headers: { "Content-Type": "text/event-stream" } });
  }
  return Response.json({ choices: [{ message: { role: "assistant", content: text }, finish_reason: "stop" }] });
};

const out = {};
for (let i = 0; i < scenarios.length; i++) {
  const sc = scenarios[i];
  const ENV = { SUPABASE_URL: "http://x", SUPABASE_ANON_KEY: "anon-key", SUPABASE_SERVICE_ROLE_KEY: "service-key",
                ELEVENLABS_KEY: sc.eleven_key, ...sc.env };
  for (const k of Object.keys(ENV)) if (ENV[k] === null) delete ENV[k];
  globalThis.Deno = { env: { get: (k) => ENV[k] }, serve: (fn) => { globalThis.__handler = fn; } };
  const state = globalThis.__state = {
    user: sc.user, pro: sc.pro, usage: { ...sc.usage }, db: structuredClone(sc.db), fail: sc.fail,
    interpretQuota: sc.interpret_quota, tokens: { ...sc.tokens }, previewChars: { ...sc.preview_chars },
    rpc: [], log: [], deletedUsers: [],
  };
  skew = 0;
  const logs = [];
  for (const k of ["log", "warn", "error"]) console[k] = (...a) => logs.push(util.format(...a));
  await import(`${modDir}/${sc.fn}.mts?s=${i}`);
  const handler = globalThis.__handler;
  let lastSession = "";
  const one = async (rq) => {
    let body = rq.body;
    if (rq.clip_chars) body = { ...body, clips: ["A".repeat(rq.clip_chars)] };
    const text = JSON.stringify(body).split("$session").join(lastSession);
    const headers = { "Content-Type": "application/json" };
    if (rq.auth !== "") headers.Authorization = rq.auth ?? "Bearer aaa.bbb.ccc";
    const res = await handler(new Request("http://fn/", { method: "POST", headers, body: text }));
    const answer = await res.text();
    let json = null; try { json = JSON.parse(answer); } catch (_e) { /* an event stream */ }
    if (typeof json?.session_id === "string") lastSession = json.session_id;
    return { status: res.status, text: answer, json, ctype: res.headers.get("Content-Type"),
             family: res.headers.get("X-Answer-Family") };
  };
  const results = [];
  for (const rq of sc.requests) {
    calls.length = 0; state.rpc.length = 0; state.log.length = 0;
    skew += rq.advance_ms ?? 0;
    queues = structuredClone(rq.behave ?? sc.behave ?? {});
    const group = rq.parallel ? await Promise.all(rq.parallel.map(one)) : [await one(rq)];
    for (const g of group) {
      results.push({ ...g, calls: calls.map((c) => ({ ...c })), rpc: structuredClone(state.rpc), dblog: [...state.log],
                     db: structuredClone(state.db), usage: { ...state.usage }, deleted: [...state.deletedUsers],
                     tokens: { ...state.tokens }, preview_chars: { ...state.previewChars }, logs: logs.splice(0) });
    }
  }
  out[sc.id] = results;
}
Object.assign(console, realLog);
process.stdout.write(JSON.stringify(out), () => process.exit(0));
"""


def wav(seconds, rate=16000):
    """A silent 16-bit mono PCM WAV, base64."""
    data = b"\x00\x00" * int(rate * seconds)
    header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16, 1, 1,
                         rate, rate * 2, 2, 16, b"data", len(data))
    return base64.b64encode(header + data).decode()


CLIP = wav(1.0)
TOO_LONG = wav(201, rate=4000)          # 201 s of audio in 1.6 MB (byte rate 8000)

BOTH = {"action": "start", "hear": {"from": "ja", "to": "en"}, "speak": {"from": "en", "to": "ja"}}
HEAR = {"action": "start", "hear": {"from": "ja", "to": "en"}}
SPEAK = {"action": "start", "speak": {"from": "en", "to": "ja"}}


def beat(seconds, advance_ms=0, stop=False, session="$session", **extra):
    return {"body": {"action": "stop" if stop else "heartbeat", "session_id": session, "seconds": seconds},
            "advance_ms": advance_ms, **extra}


def own_voice(voice_id="voiceOWN123", user=USER):
    return {"user_id": user, "provider": "elevenlabs", "voice_id": voice_id, "source": "recorded",
            "consent_at": "2026-11-01T10:00:00.000Z", "consent_phrase": "Seven green lamps", "created_at":
            "2026-11-01T10:00:00.000Z"}


def create(clips=None, **over):
    body = {"action": "create", "clips": [CLIP, CLIP] if clips is None else clips, "source": "meetings",
            "consent": True, "consent_phrase": "Purple rivers sing at noon"}
    body.update(over)
    return {"body": body}


QUOTA_401 = {"status": 401, "text": '{"detail":{"status":"quota_exceeded","message":"This request exceeds your quota"}}'}

LLM_ENV = {"MISTRAL_STT_KEY": "mistral-key-0001", "GOOGLE_STT_KEY": "gemini-key-0001",
           "CLAUDE_LLM_KEY": "claude-key-0001", "LIVE_LLM_URL": "https://live.test",
           "LIVE_LLM_TOKEN": "wk-live.ws-live-0001", "LIVE_LLM_MODEL": "deepseek-ai/test",
           "OPENAI_TOKEN": "sk-test-OPENKEY-0001"}
ALL_FAIL = {h: [{"status": 500}] for h in ("live", "mistral", "openai", "gemini", "anthropic")}
PHRASE = {"mode": "interpret", "text": "Kore wa Gusan desu.", "source_lang": "ja", "target_lang": "en",
          "context": ["Hajimemashite.", "Watashi wa Aram desu."], "glossary": ["Gusan", "Aibuben"]}


def interp(**over):
    body = dict(PHRASE)
    body.update(over)
    return {"body": body}


def build_scenarios():
    s = []

    def add(id, fn, requests, env=None, db=None, user=USER, pro=True, usage=None, behave=None, fail=None,
            eleven_key=ELEVEN_KEY, interpret_quota=True, tokens=None, preview_chars=None):
        s.append({"id": id, "fn": fn, "requests": requests, "env": env or {}, "db": db or {}, "user": user,
                  "pro": pro, "usage": usage or {}, "behave": behave or {}, "fail": fail or {},
                  "eleven_key": eleven_key, "interpret_quota": interpret_quota, "tokens": tokens or {},
                  "preview_chars": preview_chars or {}})

    open_other = {"interpreter_sessions": [{"id": SID_OTHER, "user_id": OTHER, "started_at": LONG_AGO,
                                            "last_beat_at": LONG_AGO, "seconds": 0}]}
    open_old = {"interpreter_sessions": [{"id": SID_OLD, "user_id": USER, "started_at": LONG_AGO,
                                          "last_beat_at": LONG_AGO, "seconds": 0}]}

    # ── interpreter-session ───────────────────────────────────────────────
    add("s_auth", "session", [{"body": BOTH, "auth": ""}, {"body": BOTH, "auth": "Bearer garbage"},
                              {"body": BOTH, "auth": "Bearer x.y.rejected"}])
    add("s_signed_out", "session", [{"body": BOTH}], user=None)
    add("s_not_pro", "session", [{"body": BOTH},
                                 {"body": {"action": "token", "session_id": SID_OLD, "kind": "scribe"}},
                                 beat(10, session=SID_OLD), beat(0, session=SID_OLD, stop=True)], pro=False, db=open_old)
    add("s_allowance", "session", [{"body": BOTH}], usage={USER: 18000})
    add("s_allowance_custom", "session", [{"body": HEAR}], usage={USER: 18000},
        env={"INTERPRETER_MONTHLY_SECONDS": "20000"})
    add("s_start_both_own", "session", [{"body": BOTH}], db={"interpreter_voices": [own_voice()]})
    add("s_start_hear", "session", [{"body": HEAR}])
    add("s_start_hear_with_voice", "session", [{"body": HEAR}], db={"interpreter_voices": [own_voice()]})
    add("s_start_speak_preset", "session", [{"body": SPEAK}], db={"interpreter_voices": [own_voice(user=OTHER)]})
    add("s_env", "session", [{"body": BOTH}], env={"INTERPRETER_PRESET_VOICE": "PresetVoice42",
                                                   "INTERPRETER_TTS_MODEL": "eleven_flash_v2_5",
                                                   "INTERPRETER_MONTHLY_SECONDS": "600"})
    add("s_env_bad", "session", [{"body": BOTH}], env={"INTERPRETER_PRESET_VOICE": "../voices/x",
                                                       "INTERPRETER_TTS_MODEL": "eleven v4?x=1",
                                                       "INTERPRETER_MONTHLY_SECONDS": "lots"})
    add("s_bad", "session", [
        {"body": {"action": "start"}},
        {"body": {"action": "start", "hear": {"from": "ja"}}},
        {"body": {"action": "start", "hear": {"from": "ja", "to": "english please"}}},
        {"body": {"action": "start", "hear": "ja-en"}},
        {"body": {"action": "fly"}},
        {"body": ["start"]},
        beat(10, session="not-a-session"),
    ])
    add("s_auto_from", "session", [{"body": {"action": "start", "hear": {"from": "auto", "to": "en"}}}])
    add("s_beats", "session", [
        {"body": HEAR},
        beat(30, 30_000),
        beat(100, 10_000),           # reports 100 s after 10 s: 10 counted
        beat(0, 60_000),             # reports 0 after 60 s: 60 counted all the same
        beat(-3, 1_000),             # refused
        beat("10"),                  # refused
        beat(999, 2_000, stop=True), # 3 s since the last counted beat: 3 counted
        beat(60, 60_000),            # closed: nothing more
        beat(5, 1_000, stop=True),   # stop again: nothing more
    ])
    # a client that reports 0 or nothing: the server's clock counts (beats 0.9 s apart still add up)
    add("s_zero", "session", [
        {"body": HEAR},
        {"body": {"action": "heartbeat", "session_id": "$session"}, "advance_ms": 30_000},
        beat(0, 30_000),
        *[beat(0, 900) for _ in range(10)],
        beat(0, 30_000, stop=True),
    ])
    # the PC slept with the interpreter on: one beat counts at most 90 s
    add("s_sleep", "session", [
        {"body": HEAR},
        beat(30, 30_000),
        beat(8 * 3600 + 30, 8 * 3600_000),
        beat(30, 30_000),                          # awake: counted from the wake-up, not the night
        beat(8 * 3600, 8 * 3600_000, stop=True),   # Stop after another night
    ])
    add("s_race", "session", [{"body": HEAR}, {"advance_ms": 20_000, "parallel": [beat(20), beat(20)]}])
    add("s_other_user", "session", [beat(10, session=SID_OTHER),
                                     {"body": {"action": "token", "session_id": SID_OTHER, "kind": "tts"}}],
        db=open_other)
    add("s_token", "session", [
        {"body": BOTH},
        {"body": {"action": "token", "session_id": "$session", "kind": "scribe"}},
        {"body": {"action": "token", "session_id": "$session", "kind": "tts"}},
        {"body": {"action": "token", "session_id": "$session", "kind": "both"}},
        beat(0, stop=True),
        {"body": {"action": "token", "session_id": "$session", "kind": "scribe"}},
        {"body": {"action": "token", "session_id": SID_OLD, "kind": "scribe"}},
    ], db=open_old)
    add("s_token_allowance", "session", [
        {"body": HEAR},
        beat(20, 20_000),
        {"body": {"action": "token", "session_id": "$session", "kind": "scribe"}},
    ], usage={USER: 17990})
    # a reconnect counts as a heartbeat
    add("s_token_beat", "session", [
        {"body": HEAR},
        {"body": {"action": "token", "session_id": "$session", "kind": "scribe"}, "advance_ms": 40_000},
        beat(0, 10_000),
    ])
    add("s_token_beat_used", "session", [
        {"body": HEAR},
        {"body": {"action": "token", "session_id": "$session", "kind": "tts"}, "advance_ms": 30_000},
    ], usage={USER: 17980})
    # the daily token ceiling (500): a start takes 2 per direction, a reconnect 1
    add("s_tokens_left", "session", [{"body": BOTH},
                                     {"body": {"action": "token", "session_id": "$session", "kind": "tts"}}],
        tokens={USER: 496})
    add("s_tokens_spent", "session", [{"body": HEAR}], tokens={USER: 500})
    add("s_tokens_db_down", "session", [{"body": HEAR}], fail={"use_interpreter_tokens": True})
    add("s_token_provider", "session", [
        {"body": HEAR},
        {"body": {"action": "token", "session_id": "$session", "kind": "scribe"},
         "behave": {"token_realtime_scribe": [{"status": 402, "text": "payment required"}]}},
    ])
    add("s_provider", "session", [
        {"body": BOTH, "behave": {"token_realtime_scribe": [QUOTA_401]}},
        {"body": BOTH, "behave": {"token_ttd_websocket": [{"status": 402, "text": "{}"}]}},
        {"body": BOTH, "behave": {"token_ttd_websocket": [{"status": 500, "text": "oops"}]}},
        {"body": BOTH, "behave": {"token_realtime_scribe": [{"status": 401, "text":
            f'{{"detail":{{"status":"invalid_api_key","message":"Invalid API key {ELEVEN_KEY}"}}}}'}]}},
        {"body": BOTH, "behave": {"token_realtime_scribe": [{"throw": "timed out", "name": "TimeoutError"}]}},
        {"body": BOTH, "behave": {"token_ttd_websocket": [{"json": {"nothing": True}}]}},
        {"body": BOTH, "behave": {"token_realtime_scribe": [{"status": 429, "text": "too_many_concurrent_requests"}]}},
    ])
    add("s_no_key", "session", [{"body": BOTH}], eleven_key=None)
    add("s_other_key_name", "session", [{"body": HEAR}], eleven_key=None, env={"ELEVENLABS_API_KEY": "sk_other_name_123"})
    add("s_db_down", "session", [{"body": HEAR}], fail={"use_interpreter_seconds": True})

    # ── voice-clone ───────────────────────────────────────────────────────
    add("v_auth", "voice", [{**create(), "auth": ""}, {"body": {"action": "get"}, "auth": "Bearer x.y.rejected"}])
    add("v_not_pro", "voice", [create(), {"body": {"action": "preview", "text": "Hi", "language": "en"}},
                               {"body": {"action": "get"}}, {"body": {"action": "delete"}}],
        pro=False, db={"interpreter_voices": [own_voice()]})
    add("v_consent", "voice", [create(consent=False), create(consent="true"), create(consent_phrase="   "),
                               {"body": {k: v for k, v in create()["body"].items() if k != "consent"}},
                               {"body": {k: v for k, v in create()["body"].items() if k != "consent_phrase"}}])
    add("v_limits", "voice", [
        {**create(), "clip_chars": 12 * 1024 * 1024 + 4},
        create(clips=[CLIP] * 26),
        create(clips=["aGVsbG8gd29ybGQ="]),
        create(clips=[TOO_LONG]),
        create(clips=[]),
        create(clips=[CLIP, 42]),
        create(source="upload"),
        create(clips=["!!!not base64!!!"]),
    ])
    add("v_create_replace", "voice", [create()], db={"interpreter_voices": [own_voice("voiceOLD1")]})
    add("v_create_first", "voice", [create(clips=["data:audio/wav;base64," + CLIP], source="recorded")])
    add("v_create_fail", "voice", [
        {**create(), "behave": {"voice_add": [QUOTA_401]}},
        {**create(), "behave": {"voice_add": [{"status": 500}]}},
        {**create(), "behave": {"voice_add": [{"status": 400, "text": '{"detail":{"status":"voice_limit_reached"}}'}]}},
        {**create(), "behave": {"voice_add": [{"json": {"no": "id"}}]}},
        {**create(), "behave": {"voice_add": [{"status": 400, "text": '{"detail":{"type":"validation_error",'
                                               '"code":"audio_too_short","message":"All voice samples must be at '
                                               'least 4.6 seconds"}}'}]}},
        {**create(), "behave": {"voice_add": [{"status": 422, "text": '{"detail":"invalid audio"}'}]}},
    ], db={"interpreter_voices": [own_voice("voiceOLD1")]})
    add("v_create_old_stays", "voice", [create()], db={"interpreter_voices": [own_voice("voiceOLD1")]},
        behave={"voice_delete": [{"status": 500}]})
    add("v_create_row_fails", "voice", [create()], fail={"swap_interpreter_voice": True})
    # two creates at the same time (two PCs): each voice ends up either saved or deleted at ElevenLabs
    two = {"voice_add": [{"json": {"voice_id": "voiceAAA"}}, {"json": {"voice_id": "voiceBBB"}}]}
    add("v_create_race", "voice", [{"parallel": [create(), create()], "behave": two}],
        db={"interpreter_voices": [own_voice("voiceOLD1")]})
    add("v_create_race_first", "voice", [{"parallel": [create(), create()], "behave": two}])
    # Delete, while another device saved a new voice between the lookup and the row's delete
    add("v_delete_meanwhile", "voice", [{"body": {"action": "delete"}}],
        db={"interpreter_voices": [own_voice("voiceOLD1")]},
        behave={"voice_delete": [{"db": {"interpreter_voices": [own_voice("voiceNEW9")]}}]})
    add("v_delete_ok", "voice", [{"body": {"action": "delete"}}, {"body": {"action": "get"}}],
        db={"interpreter_voices": [own_voice("voiceDEL1")]})
    add("v_delete_404", "voice", [{"body": {"action": "delete"}}], db={"interpreter_voices": [own_voice("voiceGONE1")]},
        behave={"voice_delete": [{"status": 404, "text": '{"detail":{"status":"voice_not_found"}}'}]})
    add("v_delete_fail", "voice", [{"body": {"action": "delete"}}], db={"interpreter_voices": [own_voice("voiceDEL1")]},
        behave={"voice_delete": [{"status": 500}]})
    add("v_delete_none", "voice", [{"body": {"action": "delete"}}])
    add("v_get", "voice", [{"body": {"action": "get"}}], db={"interpreter_voices": [own_voice()]})
    add("v_get_none", "voice", [{"body": {"action": "get"}}])
    add("v_preview", "voice", [
        {"body": {"action": "preview", "text": "x" * 500, "language": "ja"}},
        {"body": {"action": "preview", "text": "Hello", "language": "hy"},
         "behave": {"tts": [{"status": 422, "text": "unsupported language_code"}, {}]}},
        {"body": {"action": "preview", "text": "   "}},
        {"body": {"action": "preview", "text": "Hi", "language": "Japanese"}},
        {"body": {"action": "preview", "text": "Hi"}, "behave": {"tts": [QUOTA_401]}},
    ], db={"interpreter_voices": [own_voice()]})
    add("v_preview_no_voice", "voice", [{"body": {"action": "preview", "text": "Hi", "language": "en"}}])
    # the daily preview ceiling: 20,000 characters
    add("v_preview_ceiling", "voice", [
        {"body": {"action": "preview", "text": "Hello there", "language": "en"}},   # 19,989 + 11: just fits
        {"body": {"action": "preview", "text": "Hi", "language": "en"}},            # over
    ], db={"interpreter_voices": [own_voice()]}, preview_chars={USER: 19_989})
    add("v_preview_db_down", "voice", [{"body": {"action": "preview", "text": "Hi", "language": "en"}}],
        db={"interpreter_voices": [own_voice()]}, fail={"use_voice_preview_chars": True})

    # ── smart-action mode "interpret" ─────────────────────────────────────
    add("i_all_ok", "smart", [interp()], env=LLM_ENV)
    add("i_order", "smart", [interp()], env=LLM_ENV, behave=ALL_FAIL)
    add("i_live_fails", "smart", [interp()], env=LLM_ENV, behave={"live": [{"status": 500}]})
    add("i_live_mistral_fail", "smart", [interp()], env=LLM_ENV,
        behave={"live": [{"status": 500}], "mistral": [{"status": 503}]})
    add("i_live_stalls", "smart", [interp()], env=LLM_ENV, behave={"live": [{"stall": True}]})
    add("i_budget_spent", "smart", [interp()], env=LLM_ENV,
        behave={"live": [{"status": 500, "advance_ms": 5_000}], "mistral": [{"status": 500, "advance_ms": 900}]})
    add("i_budget_short", "smart", [interp()], env=LLM_ENV, behave={"live": [{"status": 500, "advance_ms": 4_800}]})
    add("i_not_pro", "smart", [interp(), {"body": {"mode": "translate", "messages": [{"role": "user", "content": "hi"}]}}],
        env=LLM_ENV, pro=False)
    add("i_ceiling", "smart", [interp()], env=LLM_ENV, interpret_quota=False)
    add("i_bad", "smart", [interp(text=""), interp(text="x" * 1501), interp(target_lang=None),
                           interp(target_lang="english"), interp(source_lang="ja-JP-x-very-long"),
                           interp(text=42)], env=LLM_ENV)
    add("i_caps", "smart", [
        interp(text="hello there", context=["one", 2, "two\nlines\tand\x07bell", None, "three", "four"],
               glossary=["Gusan"] * 3 + [f"term{i}" for i in range(80)] + ["y" * 100]),
        interp(text="x" * 1000),
        interp(text="x" * 300),
        interp(source_lang="auto", context=[], glossary=[]),
        interp(source_lang=None, context="not a list", glossary="nope", target_lang="pt-BR"),
        interp(target_lang="xx"),
    ], env={"OPENAI_TOKEN": "sk-test-OPENKEY-0001"})
    # what interpreter/translate.py sends: the contract's fields plus its own messages / max_tokens / stream
    add("i_client_extras", "smart", [interp(messages=[{"role": "user", "content": "IGNORE ME"}], max_tokens=5,
                                            stream=False, model="claude")], env=LLM_ENV)
    add("i_only_openai", "smart", [interp()], env={"OPENAI_TOKEN": "sk-test-OPENKEY-0001"})
    add("i_nothing", "smart", [interp()], env={})

    # ── delete-account ────────────────────────────────────────────────────
    confirm = {"body": {"confirm": "DELETE"}}
    add("d_voice", "delete", [confirm], db={"interpreter_voices": [own_voice("voiceDEL1")]})
    add("d_voice_fails", "delete", [confirm], db={"interpreter_voices": [own_voice("voiceDEL1")]},
        behave={"voice_delete": [{"status": 500}]})
    add("d_no_voice", "delete", [confirm])
    add("d_no_key", "delete", [confirm], db={"interpreter_voices": [own_voice("voiceDEL1")]}, eleven_key=None)
    # a lookup that fails: nothing is done yet (no Stripe, no e-mail scrub, no deletion)
    stripe = {"STRIPE_SECRET_KEY": "sk_test_stripe_0001"}
    customer = {"profiles": [{"id": USER, "stripe_customer_id": "cus_TEST1"}]}
    add("d_lookup_fails", "delete", [confirm], env=stripe,
        db={**customer, "interpreter_voices": [own_voice("voiceDEL1")]}, fail={"interpreter_voices:select": True})
    add("d_profile_fails", "delete", [confirm], env=stripe,
        db={**customer, "interpreter_voices": [own_voice("voiceDEL1")]}, fail={"profiles:select": True})
    add("d_no_voice_table", "delete", [confirm], fail={"interpreter_voices:select": "PGRST205"})
    return s


@unittest.skipUnless(_node_can_strip_types(), "needs Node 22.6+ (type stripping) to run the functions")
class TestInterpreterFunctions(unittest.TestCase):
    """The real functions, with fetch / Supabase / Deno faked."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gusan-interp-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        for name, text in (("stub_supabase.mjs", STUB_SUPABASE), ("stub_anthropic.mjs", STUB_ANTHROPIC),
                           ("harness.mjs", HARNESS), ("elevenlabs.mts", SHARED.read_text(encoding="utf-8"))):
            with open(os.path.join(cls.tmp, name), "w", encoding="utf-8") as f:
                f.write(text)
        for fn, path in SOURCES.items():
            src = path.read_text(encoding="utf-8")
            patched = re.sub(r'from "https://esm\.sh/@supabase/supabase-js@2"', 'from "./stub_supabase.mjs"', src)
            patched = re.sub(r'from "npm:@anthropic-ai/sdk@[\d.]+"', 'from "./stub_anthropic.mjs"', patched)
            patched = patched.replace('from "../_shared/elevenlabs.ts"', 'from "./elevenlabs.mts"')
            assert "./stub_supabase.mjs" in patched, f"{fn}: the Supabase import changed"
            with open(os.path.join(cls.tmp, f"{fn}.mts"), "w", encoding="utf-8") as f:
                f.write(patched)
        scenarios = os.path.join(cls.tmp, "scenarios.json")
        with open(scenarios, "w", encoding="utf-8") as f:
            json.dump(build_scenarios(), f)
        p = subprocess.run([_node_can_strip_types(), "--no-warnings", "--experimental-strip-types",
                            os.path.join(cls.tmp, "harness.mjs"), pathlib.Path(cls.tmp).as_uri(), scenarios],
                           capture_output=True, text=True, timeout=180)
        assert p.returncode == 0, p.stderr[-2000:]
        cls.out = json.loads(p.stdout)

    # ── helpers ────────────────────────────────────────────────────────────
    def sc(self, id):
        return self.out[id]

    def kinds(self, r):
        return [c["kind"] for c in r["calls"]]

    def err(self, r):
        return (r["status"], (r["json"] or {}).get("error"))

    def sessions(self, r):
        return r["db"].get("interpreter_sessions", [])

    def voices(self, r):
        return r["db"].get("interpreter_voices", [])

    # ── interpreter-session: who may ───────────────────────────────────────
    def test_no_or_bad_token_is_401_auth_before_anything_else(self):
        for r in self.sc("s_auth") + self.sc("s_signed_out"):
            self.assertEqual(self.err(r), (401, "auth"))
            self.assertEqual(r["calls"], [])
            self.assertEqual(self.sessions(r), [])
        # a token that isn't even JWT-shaped is refused before Supabase is asked
        self.assertEqual(self.sc("s_auth")[1]["rpc"], [])

    def test_not_pro_is_402_but_a_running_session_can_still_be_counted_and_stopped(self):
        start, tok, hb, stop = self.sc("s_not_pro")
        self.assertEqual(self.err(start), (402, "pro_required"))
        self.assertEqual(start["calls"], [])
        self.assertEqual(self.sessions(start)[0]["id"], SID_OLD)        # nothing new
        self.assertEqual(len(self.sessions(start)), 1)
        self.assertEqual(self.err(tok), (402, "pro_required"))
        self.assertEqual(tok["calls"], [])
        self.assertEqual(hb["status"], 200)
        self.assertEqual(hb["json"], {"seconds_left": 18000 - 90})              # last beat long ago: 90 s at most
        self.assertEqual(stop["json"], {"seconds_left": 17910})
        self.assertTrue(self.sessions(stop)[0].get("ended_at"))
        # the Pro check is the same is_pro() RPC smart-action and transcribe-proxy use, asked as the user
        self.assertIn(["is_pro", None, "user"], start["rpc"])

    def test_allowance_used_is_429_with_seconds_left_zero(self):
        r = self.sc("s_allowance")[0]
        self.assertEqual(r["status"], 429)
        self.assertEqual(r["json"], {"error": "allowance_used", "seconds_left": 0})
        self.assertEqual(r["calls"], [])
        self.assertEqual(self.sessions(r), [])
        r = self.sc("s_allowance_custom")[0]
        self.assertEqual(r["status"], 200)
        self.assertEqual((r["json"]["seconds_left"], r["json"]["allowance_seconds"]), (2000, 20000))

    # ── interpreter-session: start ─────────────────────────────────────────
    def test_start_with_both_directions_and_an_own_voice(self):
        r = self.sc("s_start_both_own")[0]
        self.assertEqual(r["status"], 200, r["text"])
        j = r["json"]
        self.assertEqual(set(j), {"session_id", "seconds_left", "allowance_seconds", "stt_model", "tts_model",
                                  "scribe_tokens", "tts_tokens", "voices", "own_voice"})
        self.assertRegex(j["session_id"], r"^[0-9a-f-]{36}$")
        self.assertEqual((j["seconds_left"], j["allowance_seconds"]), (18000, 18000))
        self.assertEqual((j["stt_model"], j["tts_model"]), ("scribe_v2_realtime", "eleven_v4_turbo"))
        self.assertEqual(set(j["scribe_tokens"]), {"hear", "speak"})
        self.assertEqual(set(j["tts_tokens"]), {"hear", "speak"})
        self.assertTrue(all(t.startswith("realtime_scribe-") for t in j["scribe_tokens"].values()))
        self.assertTrue(all(t.startswith("ttd_websocket-") for t in j["tts_tokens"].values()))
        self.assertEqual(len(set(j["scribe_tokens"].values()) | set(j["tts_tokens"].values())), 4)   # all single-use
        self.assertEqual(j["voices"], {"hear": PRESET, "speak": "voiceOWN123"})
        self.assertIs(j["own_voice"], True)
        # one Scribe and one voice token per direction - the voice socket is text-to-dialogue, so its tokens are
        # ttd_websocket (the field keeps the contract's name tts_tokens) - POST, the key in xi-api-key, no redirects
        self.assertEqual(sorted(self.kinds(r)), ["token_realtime_scribe"] * 2 + ["token_ttd_websocket"] * 2)
        for c in r["calls"]:
            self.assertEqual((c["method"], c["xi"], c["redirect"]), ("POST", ELEVEN_KEY, "error"))
            self.assertTrue(c["url"].startswith("https://api.elevenlabs.io/v1/single-use-token/"))
        s = self.sessions(r)
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["id"], s[0]["user_id"], s[0]["seconds"]), (j["session_id"], USER, 0))
        self.assertIsNone(s[0].get("ended_at"))
        self.assertEqual(s[0]["started_at"], s[0]["last_beat_at"])

    def test_start_mints_tokens_only_for_the_directions_that_are_on(self):
        j = self.sc("s_start_hear")[0]["json"]
        self.assertEqual((set(j["scribe_tokens"]), set(j["tts_tokens"])), ({"hear"}, {"hear"}))
        self.assertEqual(j["voices"], {"hear": PRESET})
        self.assertIs(j["own_voice"], False)
        r = self.sc("s_start_hear")[0]
        self.assertEqual(sorted(self.kinds(r)), ["token_realtime_scribe", "token_ttd_websocket"])
        self.assertNotIn(["select", "interpreter_voices"], r["dblog"])    # Speak is off: no voice lookup
        # an own voice is not used for HEAR (the person hears others in the preset voice)
        j = self.sc("s_start_hear_with_voice")[0]["json"]
        self.assertEqual((j["voices"], j["own_voice"]), ({"hear": PRESET}, False))
        # SPEAK without an own voice (another user's voice is not theirs): the preset
        j = self.sc("s_start_speak_preset")[0]["json"]
        self.assertEqual((set(j["scribe_tokens"]), j["voices"], j["own_voice"]), ({"speak"}, {"speak": PRESET}, False))
        self.assertEqual(self.sc("s_auto_from")[0]["status"], 200)

    def test_the_secrets_set_the_allowance_the_preset_voice_and_the_model(self):
        j = self.sc("s_env")[0]["json"]
        self.assertEqual((j["allowance_seconds"], j["seconds_left"]), (600, 600))
        self.assertEqual(j["tts_model"], "eleven_flash_v2_5")
        self.assertEqual(j["voices"], {"hear": "PresetVoice42", "speak": "PresetVoice42"})
        j = self.sc("s_env_bad")[0]["json"]
        self.assertEqual((j["allowance_seconds"], j["tts_model"], j["voices"]["hear"]),
                         (18000, "eleven_v4_turbo", PRESET))

    def test_bad_start_requests_are_400_bad_request(self):
        for i, r in enumerate(self.sc("s_bad")):
            self.assertEqual(self.err(r), (400, "bad_request"), i)
            self.assertEqual(r["calls"], [], i)

    # ── interpreter-session: heartbeat / stop ──────────────────────────────
    def test_heartbeats_count_the_servers_clock_not_the_apps_number(self):
        r = self.sc("s_beats")
        start, b30, b100, b0, neg, text, stop, after, stop2 = r
        self.assertEqual(b30["json"], {"seconds_left": 18000 - 30})
        self.assertEqual(b100["json"], {"seconds_left": 18000 - 30 - 10})        # reports 100 after 10 s: 10
        self.assertEqual(b0["json"], {"seconds_left": 17960 - 60})               # reports 0 after 60 s: 60
        self.assertEqual(self.err(neg), (400, "bad_request"))
        self.assertEqual(self.err(text), (400, "bad_request"))
        self.assertEqual(stop["json"], {"seconds_left": 17900 - 3})              # 3 s since the last beat
        self.assertEqual(after["json"], {"seconds_left": 17897})                 # a closed session counts nothing
        self.assertEqual(stop2["json"], {"seconds_left": 17897})                 # stop is safe to repeat
        counted = [x["rpc"][-1][1]["p_seconds"] for x in (b30, b100, b0, stop, after, stop2)]
        self.assertEqual(counted, [30, 10, 60, 3, 0, 0])
        for x in (b30, stop):
            name, args, who = x["rpc"][-1]
            self.assertEqual((name, args["p_user_id"], args["p_allowance"], who),
                             ("use_interpreter_seconds", USER, 18000, "service"))
        s = self.sessions(stop2)[0]
        self.assertEqual(s["seconds"], 103)
        self.assertTrue(s["ended_at"])
        self.assertEqual(stop2["usage"][USER], 103)
        # the stop's log line keeps what the app said next to what was counted
        self.assertTrue(any('"counted":3,"reported":999' in l for l in stop["logs"]), stop["logs"])
        # heartbeat and stop never ask for Pro
        self.assertNotIn("is_pro", [x[0] for x in b30["rpc"]])

    def test_a_report_of_0_or_nothing_still_pays_for_the_time_that_passed(self):
        r = self.sc("s_zero")
        start, none, zero, *quick, stop = r
        self.assertEqual([x["status"] for x in r], [200] * len(r))
        self.assertEqual(none["json"], {"seconds_left": 18000 - 30})             # no "seconds" at all: 30
        self.assertEqual(zero["json"], {"seconds_left": 18000 - 60})             # "seconds": 0: 30 more
        # ten beats 0.9 s apart: the part of a second left over waits for the next beat, so 9 s add up
        self.assertEqual(quick[-1]["usage"][USER], 60 + 9)
        self.assertEqual(stop["json"], {"seconds_left": 18000 - 99})
        self.assertEqual(self.sessions(stop)[0]["seconds"], 99)

    def test_a_beat_after_the_pc_slept_counts_at_most_90_seconds(self):
        start, b30, woke, again, stop = self.sc("s_sleep")
        counted = [x["rpc"][-1][1]["p_seconds"] for x in (b30, woke, again, stop)]
        self.assertEqual(counted, [30, 90, 30, 90])                              # not 8 hours
        self.assertEqual(stop["json"], {"seconds_left": 18000 - 240})
        self.assertTrue(self.sessions(stop)[0]["ended_at"])

    def test_two_beats_at_the_same_time_are_counted_once(self):
        first, second = self.sc("s_race")[1:]
        self.assertEqual(sorted([first["json"]["seconds_left"], second["json"]["seconds_left"]]), [17980, 17980])
        self.assertEqual(second["usage"][USER], 20)
        self.assertEqual(self.sessions(second)[0]["seconds"], 20)

    def test_someone_elses_session_is_not_found(self):
        hb, tok = self.sc("s_other_user")
        self.assertEqual(self.err(hb), (400, "bad_request"))
        self.assertEqual(self.err(tok), (400, "bad_request"))
        self.assertEqual(hb["usage"], {})
        self.assertEqual(self.sessions(hb)[0]["seconds"], 0)
        self.assertEqual(tok["calls"], [])

    # ── interpreter-session: token ─────────────────────────────────────────
    def test_a_reconnect_gets_one_token_of_the_kind_asked(self):
        start, scribe, tts, both, stop, closed, old = self.sc("s_token")
        self.assertEqual(scribe["status"], 200)
        self.assertEqual(set(scribe["json"]), {"token"})
        self.assertEqual(self.kinds(scribe), ["token_realtime_scribe"])
        self.assertEqual(self.kinds(tts), ["token_ttd_websocket"])
        self.assertEqual(self.err(both), (400, "bad_request"))
        self.assertEqual(stop["status"], 200)
        self.assertEqual((self.err(closed), closed["json"].get("detail")), ((400, "bad_request"), "session_closed"))
        self.assertEqual(closed["calls"], [])
        self.assertEqual(old["json"].get("detail"), "session_closed")             # over 12 hours old
        r = self.sc("s_token_allowance")
        self.assertEqual(r[1]["json"], {"seconds_left": 0})
        self.assertEqual(r[2]["status"], 429)
        self.assertEqual(r[2]["json"], {"error": "allowance_used", "seconds_left": 0})
        self.assertEqual(r[2]["calls"], [])
        r = self.sc("s_token_provider")[1]
        self.assertEqual(self.err(r), (402, "provider_credits"))

    def test_a_reconnect_counts_as_a_heartbeat(self):
        start, tok, hb = self.sc("s_token_beat")
        self.assertEqual(tok["status"], 200, tok["text"])
        self.assertEqual(self.kinds(tok), ["token_realtime_scribe"])
        self.assertIn(["use_interpreter_seconds", {"p_user_id": USER, "p_seconds": 40, "p_allowance": 18000},
                       "service"], tok["rpc"])
        self.assertEqual(tok["usage"][USER], 40)
        self.assertEqual(hb["json"], {"seconds_left": 18000 - 50})               # the beat counts from the reconnect
        # the stretch a reconnect counts can use up the month: no token then
        r = self.sc("s_token_beat_used")[1]
        self.assertEqual(r["status"], 429)
        self.assertEqual(r["json"], {"error": "allowance_used", "seconds_left": 0})
        self.assertEqual(r["usage"][USER], 18010)

    def test_tokens_have_a_daily_ceiling_of_500(self):
        start, tok = self.sc("s_tokens_left")
        self.assertEqual(start["status"], 200, start["text"])                    # 496 + 4 = 500: still fine
        self.assertIn(["use_interpreter_tokens", {"p_user_id": USER, "p_n": 4, "p_max_per_day": 500}, "service"],
                      start["rpc"])
        self.assertEqual(self.err(tok), (429, "quota_exceeded"))                 # 501
        self.assertEqual(tok["calls"], [])
        self.assertIn(["use_interpreter_tokens", {"p_user_id": USER, "p_n": 1, "p_max_per_day": 500}, "service"],
                      tok["rpc"])
        r = self.sc("s_tokens_spent")[0]
        self.assertEqual(self.err(r), (429, "quota_exceeded"))
        self.assertEqual((r["calls"], self.sessions(r)), ([], []))
        self.assertEqual(r["tokens"][USER], 502)                                 # HEAR alone: 2
        r = self.sc("s_tokens_db_down")[0]
        self.assertEqual(self.err(r), (500, "server_error"))
        self.assertEqual((r["calls"], self.sessions(r)), ([], []))

    # ── interpreter-session: ElevenLabs says no ────────────────────────────
    def test_elevenlabs_failures_map_to_provider_credits_or_unavailable(self):
        want = [(402, "provider_credits"),        # a 401 that says quota_exceeded
                (402, "provider_credits"),        # a 402
                (503, "provider_unavailable"),    # a 500
                (503, "provider_unavailable"),    # a bad key (401)
                (503, "provider_unavailable"),    # a timeout
                (503, "provider_unavailable"),    # a 200 with no token
                (503, "provider_unavailable")]    # busy (429)
        for i, (r, w) in enumerate(zip(self.sc("s_provider"), want)):
            self.assertEqual(self.err(r), w, i)
            self.assertEqual(set(r["json"]), {"error"}, i)                        # never ElevenLabs' own words
            for s in self.sessions(r):
                self.assertTrue(s.get("ended_at"), f"{i}: a failed start left a session open")
            self.assertTrue(any('"eleven_fail"' in l for l in r["logs"]), i)

    def test_without_an_elevenlabs_key_nothing_is_tried(self):
        r = self.sc("s_no_key")[0]
        self.assertEqual(self.err(r), (503, "provider_unavailable"))
        self.assertEqual((r["calls"], self.sessions(r)), ([], []))
        r = self.sc("s_other_key_name")[0]
        self.assertEqual(r["status"], 200)
        self.assertEqual({c["xi"] for c in r["calls"]}, {"sk_other_name_123"})

    def test_a_database_failure_is_a_500_not_a_free_session(self):
        r = self.sc("s_db_down")[0]
        self.assertEqual(r["status"], 500)
        self.assertEqual(r["calls"], [])

    # ── voice-clone ────────────────────────────────────────────────────────
    def test_voice_clone_needs_a_user_and_pro_for_create_and_preview(self):
        for r in self.sc("v_auth"):
            self.assertEqual(self.err(r), (401, "auth"))
        create_, preview, get, delete = self.sc("v_not_pro")
        self.assertEqual(self.err(create_), (402, "pro_required"))
        self.assertEqual(self.err(preview), (402, "pro_required"))
        self.assertEqual(create_["calls"] + preview["calls"], [])
        # a person can always see and delete their own voice
        self.assertEqual(get["json"]["voice"]["voice_id"], "voiceOWN123")
        self.assertEqual(delete["json"], {"deleted": True})
        self.assertEqual(self.voices(delete), [])

    def test_create_needs_consent_and_a_consent_sentence(self):
        for i, r in enumerate(self.sc("v_consent")):
            self.assertEqual(self.err(r), (400, "consent_required"), i)
            self.assertEqual(r["calls"], [], i)
            self.assertEqual(self.voices(r), [], i)

    def test_create_size_and_format_limits(self):
        details = [r["json"].get("detail") for r in self.sc("v_limits")]
        self.assertEqual(details, ["too_large", "too_many_clips", "invalid_audio", "too_long", "clips", "clips",
                                   "source", "invalid_audio"])
        for r in self.sc("v_limits"):
            self.assertEqual(self.err(r), (400, "bad_request"))
            self.assertEqual(r["calls"], [])

    def test_create_replaces_the_old_voice_and_deletes_it_at_elevenlabs_afterwards(self):
        r = self.sc("v_create_replace")[0]
        self.assertEqual(r["status"], 200, r["text"])
        self.assertEqual(set(r["json"]), {"voice_id", "created_at"})
        self.assertEqual(r["json"]["voice_id"], "voice-new")
        self.assertEqual(self.kinds(r), ["voice_add", "voice_delete"])           # the old one goes once the new exists
        add, delete = r["calls"]
        self.assertEqual((add["method"], add["url"], add["xi"]), ("POST", "https://api.elevenlabs.io/v1/voices/add", ELEVEN_KEY))
        fields = dict((k, v) for k, v in add["body"] if k != "files")
        self.assertEqual(fields, {"name": "Gusan 0f8e2c1a", "description": "Gusan Interpreter voice",
                                  "remove_background_noise": "true"})
        files = [v for k, v in add["body"] if k == "files"]
        self.assertEqual([f["file"] for f in files], ["clip1.wav", "clip2.wav"])
        self.assertTrue(all(f["type"] == "audio/wav" and f["size"] == 44 + 32000 for f in files))
        self.assertEqual((delete["method"], delete["url"]), ("DELETE", "https://api.elevenlabs.io/v1/voices/voiceOLD1"))
        v = self.voices(r)
        self.assertEqual(len(v), 1)
        self.assertEqual((v[0]["voice_id"], v[0]["source"], v[0]["provider"], v[0]["consent_phrase"]),
                         ("voice-new", "meetings", "elevenlabs", "Purple rivers sing at noon"))
        self.assertEqual(v[0]["consent_at"], r["json"]["created_at"])
        self.assertNotIn("Purple rivers", "\n".join(r["logs"]))                  # the sentence is not logged
        # the row is swapped in one step (service role), and the voice it replaced is deleted after that
        swap = next(x for x in r["rpc"] if x[0] == "swap_interpreter_voice")
        self.assertEqual((swap[1]["p_user_id"], swap[1]["p_voice_id"], swap[2]), (USER, "voice-new", "service"))
        self.assertLess(r["dblog"].index(["rpc", "swap_interpreter_voice"]), r["dblog"].index(["fetch", "voice_delete"]))

    def test_two_creates_at_once_leave_no_voice_without_a_row(self):
        for id, old in (("v_create_race", {"voiceOLD1"}), ("v_create_race_first", set())):
            a, b = self.sc(id)
            self.assertEqual((a["status"], b["status"]), (200, 200), id)
            made = {a["json"]["voice_id"], b["json"]["voice_id"]}
            self.assertEqual(made, {"voiceAAA", "voiceBBB"}, id)
            kept = [v["voice_id"] for v in self.voices(b)]
            self.assertEqual(len(kept), 1, id)
            gone = {c["url"].rsplit("/", 1)[1] for c in b["calls"] if c["kind"] == "voice_delete"}
            # every voice is either the one saved or deleted at ElevenLabs: none is left without a row
            self.assertEqual(gone, (made | old) - set(kept), id)

    def test_delete_keeps_a_voice_another_device_saved_meanwhile(self):
        r = self.sc("v_delete_meanwhile")[0]
        self.assertEqual(r["json"], {"deleted": True})
        self.assertTrue(r["calls"][0]["url"].endswith("/v1/voices/voiceOLD1"))
        self.assertEqual([v["voice_id"] for v in self.voices(r)], ["voiceNEW9"])   # still deletable later

    def test_a_first_voice_and_a_data_url_clip(self):
        r = self.sc("v_create_first")[0]
        self.assertEqual(r["status"], 200, r["text"])
        self.assertEqual(self.kinds(r), ["voice_add"])
        self.assertEqual(self.voices(r)[0]["source"], "recorded")

    def test_a_failed_create_keeps_the_old_voice(self):
        want = [(402, "provider_credits"), (503, "provider_unavailable"), (402, "provider_credits"),
                (503, "provider_unavailable"),
                (400, "voice_too_short"),            # ElevenLabs: a sample under 4.6 s
                (400, "voice_samples_rejected")]     # any other refusal of the recordings
        self.assertEqual(len(self.sc("v_create_fail")), len(want))
        for i, (r, w) in enumerate(zip(self.sc("v_create_fail"), want)):
            self.assertEqual(self.err(r), w, i)
            self.assertEqual(self.kinds(r), ["voice_add"], i)                   # the old one is not deleted
            self.assertEqual(self.voices(r)[0]["voice_id"], "voiceOLD1", i)

    def test_an_old_voice_that_cant_be_deleted_is_logged_for_the_founder(self):
        r = self.sc("v_create_old_stays")[0]
        self.assertEqual(r["status"], 200)
        self.assertEqual(self.voices(r)[0]["voice_id"], "voice-new")
        self.assertTrue(any('{"voice_orphan":"voiceOLD1"}' in l for l in r["logs"]))

    def test_a_voice_that_cant_be_saved_here_is_removed_there(self):
        r = self.sc("v_create_row_fails")[0]
        self.assertEqual(r["status"], 500)
        self.assertEqual(self.kinds(r), ["voice_add", "voice_delete"])
        self.assertTrue(r["calls"][1]["url"].endswith("/v1/voices/voice-new"))

    def test_delete_goes_to_elevenlabs_first_then_the_row(self):
        r, get = self.sc("v_delete_ok")
        self.assertEqual(r["json"], {"deleted": True})
        self.assertEqual((r["calls"][0]["method"], r["calls"][0]["url"]),
                         ("DELETE", "https://api.elevenlabs.io/v1/voices/voiceDEL1"))
        self.assertLess(r["dblog"].index(["fetch", "voice_delete"]), r["dblog"].index(["delete", "interpreter_voices"]))
        self.assertEqual(self.voices(r), [])
        self.assertEqual(get["json"], {"voice": None})

    def test_delete_when_elevenlabs_no_longer_knows_the_voice(self):
        r = self.sc("v_delete_404")[0]
        self.assertEqual(r["json"], {"deleted": True})
        self.assertEqual(self.voices(r), [])
        self.assertFalse(any("eleven_fail" in l for l in r["logs"]))            # an expected 404 is not an error

    def test_delete_keeps_the_row_when_elevenlabs_fails(self):
        r = self.sc("v_delete_fail")[0]
        self.assertEqual(self.err(r), (503, "provider_unavailable"))
        self.assertEqual(self.voices(r)[0]["voice_id"], "voiceDEL1")
        r = self.sc("v_delete_none")[0]
        self.assertEqual((r["json"], r["calls"]), ({"deleted": True}, []))

    def test_get_returns_the_voice_without_the_consent_sentence(self):
        self.assertEqual(self.sc("v_get")[0]["json"], {"voice": {"voice_id": "voiceOWN123", "source": "recorded",
                                                                 "created_at": "2026-11-01T10:00:00.000Z"}})
        self.assertEqual(self.sc("v_get_none")[0]["json"], {"voice": None})

    def test_preview_speaks_200_characters_in_the_own_voice_as_pcm_16000(self):
        ok, retry, empty, bad_lang, credits = self.sc("v_preview")
        self.assertEqual(ok["status"], 200, ok["text"])
        self.assertEqual(ok["json"]["sample_rate"], 16000)
        self.assertEqual(list(base64.b64decode(ok["json"]["audio"])), [0, 1, 2, 3, 250, 251])
        c = ok["calls"][0]
        self.assertEqual(c["url"], "https://api.elevenlabs.io/v1/text-to-speech/voiceOWN123?output_format=pcm_16000")
        self.assertEqual(c["body"], {"text": "x" * 200, "model_id": "eleven_v4_turbo", "language_code": "ja"})
        # a language the model refuses: once more without it
        self.assertEqual(retry["status"], 200)
        self.assertEqual([x["body"].get("language_code") for x in retry["calls"]], ["hy", None])
        self.assertEqual(self.err(empty), (400, "bad_request"))
        self.assertEqual(self.err(bad_lang), (400, "bad_request"))
        self.assertEqual(self.err(credits), (402, "provider_credits"))
        r = self.sc("v_preview_no_voice")[0]
        self.assertEqual(self.err(r), (404, "no_voice"))
        self.assertEqual(r["calls"], [])

    def test_preview_has_a_daily_ceiling_in_characters(self):
        ok, retry, empty, bad_lang, credits = self.sc("v_preview")
        # counted in characters, as the service role, once per preview (the retry without a language too)
        self.assertIn(["use_voice_preview_chars", {"p_user_id": USER, "p_chars": 200, "p_max_per_day": 20000},
                       "service"], ok["rpc"])
        self.assertEqual([x[0] for x in retry["rpc"]].count("use_voice_preview_chars"), 1)
        # a request that can't be spoken costs nothing
        for r in (empty, bad_lang, self.sc("v_preview_no_voice")[0]):
            self.assertNotIn("use_voice_preview_chars", [x[0] for x in r["rpc"]])
        fits, over = self.sc("v_preview_ceiling")
        self.assertEqual(fits["status"], 200, fits["text"])
        self.assertEqual(self.kinds(fits), ["tts"])
        self.assertEqual(self.err(over), (429, "quota_exceeded"))
        self.assertEqual(over["calls"], [])
        r = self.sc("v_preview_db_down")[0]
        self.assertEqual(self.err(r), (500, "server_error"))
        self.assertEqual(r["calls"], [])

    # ── smart-action mode "interpret" ──────────────────────────────────────
    def test_interpret_goes_to_the_live_model_first_streamed(self):
        r = self.sc("i_all_ok")[0]
        self.assertEqual(r["status"], 200, r["text"])
        self.assertEqual(self.kinds(r), ["live"])
        self.assertIn("text/event-stream", r["ctype"])
        self.assertIn("live words", r["text"])
        self.assertIn("[DONE]", r["text"])
        self.assertEqual(r["family"], "deepseek")
        b = r["calls"][0]["body"]
        self.assertEqual((b["model"], b["stream"], b["temperature"]), ("deepseek-ai/test", True, 0.1))
        self.assertEqual(b["chat_template_kwargs"], {"thinking": False, "enable_thinking": False})   # thinking off
        system, user = b["messages"]
        self.assertEqual((system["role"], user["role"]), ("system", "user"))
        self.assertIn("from Japanese into English", system["content"])
        self.assertIn('"Gusan", "Aibuben"', system["content"])
        self.assertIn("ONLY the translation", system["content"])
        self.assertIn("- Hajimemashite.\n- Watashi wa Aram desu.", user["content"])
        self.assertTrue(user["content"].endswith("Phrase to translate:\nKore wa Gusan desu."))
        # its own daily ceiling, not the Smart Action one
        names = [x[0] for x in r["rpc"]]
        self.assertIn("use_interpret_quota", names)
        self.assertNotIn("use_smart_action_quota", names)
        self.assertEqual(next(x[1] for x in r["rpc"] if x[0] == "use_interpret_quota"), {"max_per_day": 20000})
        # a fast answer from the live model is not logged (one per phrase would flood the log)
        self.assertFalse(any(l.startswith("interpret") for l in r["logs"]))

    def test_interpret_walks_live_then_mistral_then_openai_and_nothing_else(self):
        r = self.sc("i_order")[0]
        self.assertEqual(self.kinds(r), ["live", "mistral", "openai"])
        self.assertEqual(self.err(r), (503, "provider_unavailable"))
        # each engine's share of the 6 s budget to its first words
        budgets = [c["budget"] for c in r["calls"]]
        self.assertEqual(budgets[:2], [2500, 2000])
        self.assertTrue(5000 < budgets[2] <= 6000, budgets)

    def test_interpret_falls_back_to_mistral_and_then_openai_nano(self):
        r = self.sc("i_live_fails")[0]
        self.assertEqual((self.kinds(r), r["status"]), (["live", "mistral"], 200))
        self.assertIn("mistral words", r["text"])
        b = r["calls"][1]["body"]
        self.assertEqual((b["model"], b["temperature"], b["stream"]), ("mistral-small-latest", 0.1, True))
        self.assertTrue(any(l.startswith("interpret ") and '"route":"mistral"' in l for l in r["logs"]))
        r = self.sc("i_live_mistral_fail")[0]
        self.assertEqual((self.kinds(r), r["status"]), (["live", "mistral", "openai"], 200))
        self.assertIn("openai words", r["text"])
        b = r["calls"][2]["body"]
        self.assertEqual((b["model"], b["reasoning_effort"], b["stream"]), ("gpt-5.4-nano", "none", True))
        r = self.sc("i_live_stalls")[0]
        self.assertEqual((self.kinds(r), r["status"]), (["live", "mistral"], 200))

    def test_interpret_keeps_the_whole_chain_inside_6_seconds(self):
        r = self.sc("i_budget_spent")[0]
        self.assertEqual(self.kinds(r), ["live", "mistral"])                     # < 0.3 s left: OpenAI not started
        self.assertEqual(self.err(r), (503, "provider_unavailable"))
        self.assertTrue(0 < r["calls"][1]["budget"] <= 1000, r["calls"][1]["budget"])
        r = self.sc("i_budget_short")[0]
        self.assertEqual(self.kinds(r), ["live", "mistral"])
        self.assertTrue(0 < r["calls"][1]["budget"] <= 1200, r["calls"][1]["budget"])   # cut to what is left

    def test_interpret_is_pro_only_with_a_402_and_has_its_own_ceiling(self):
        r, other = self.sc("i_not_pro")
        self.assertEqual(self.err(r), (402, "pro_required"))
        self.assertEqual(r["calls"], [])
        self.assertEqual(self.err(other), (403, "pro_required"))                 # the other modes keep their 403
        r = self.sc("i_ceiling")[0]
        self.assertEqual(self.err(r), (429, "quota_exceeded"))
        self.assertEqual(r["calls"], [])

    def test_interpret_refuses_a_bad_request(self):
        for i, r in enumerate(self.sc("i_bad")):
            self.assertEqual(self.err(r), (400, "bad_request"), i)
            self.assertEqual(r["calls"], [], i)

    def test_interpret_caps_context_glossary_and_tokens(self):
        r = self.sc("i_caps")
        b = r[0]["calls"][0]["body"]
        system, user = b["messages"]
        self.assertIn("- two lines and bell\n- three\n- four\n\nPhrase to translate:\nhello there", user["content"])
        self.assertNotIn("- one", user["content"])
        terms = re.search(r"these terms exactly as written: (.*)\.", system["content"]).group(1)
        terms = json.loads("[" + terms + "]")
        self.assertEqual(len(terms), 50)
        self.assertEqual(terms[:2], ["Gusan", "term0"])                           # duplicates dropped
        self.assertEqual(b["max_completion_tokens"], 64)
        self.assertEqual(r[1]["calls"][0]["body"]["max_completion_tokens"], 800)
        self.assertEqual(r[2]["calls"][0]["body"]["max_completion_tokens"], 482)
        system, user = r[3]["calls"][0]["body"]["messages"]
        self.assertIn("Translate the phrase into English.", system["content"])   # "auto": no source named
        self.assertEqual(user["content"], "Kore wa Gusan desu.")                  # no context: just the phrase
        system = r[4]["calls"][0]["body"]["messages"][0]["content"]
        self.assertIn("into Portuguese (BR).", system)
        self.assertIn("Keep names of people", system)
        self.assertIn('the language with the code "xx"', r[5]["calls"][0]["body"]["messages"][0]["content"])
        for x in r:
            self.assertEqual(x["status"], 200)

    def test_interpret_ignores_the_apps_own_messages_tokens_stream_and_model(self):
        r = self.sc("i_client_extras")[0]
        self.assertEqual((self.kinds(r), r["status"]), (["live"], 200))
        self.assertIn("text/event-stream", r["ctype"])                          # streamed all the same
        b = r["calls"][0]["body"]
        self.assertNotIn("IGNORE ME", json.dumps(b["messages"]))
        self.assertIn("Kore wa Gusan desu.", b["messages"][1]["content"])
        self.assertEqual((b["max_tokens"], b["stream"]), (64, True))

    def test_interpret_with_openai_alone_or_nothing(self):
        r = self.sc("i_only_openai")[0]
        self.assertEqual((self.kinds(r), r["status"]), (["openai"], 200))
        r = self.sc("i_nothing")[0]
        self.assertEqual(self.err(r), (503, "provider_unavailable"))

    # ── delete-account ─────────────────────────────────────────────────────
    def test_account_deletion_deletes_the_voice_at_elevenlabs_first(self):
        r = self.sc("d_voice")[0]
        self.assertEqual(r["status"], 200)
        self.assertEqual(self.kinds(r), ["voice_delete"])
        self.assertTrue(r["calls"][0]["url"].endswith("/v1/voices/voiceDEL1"))
        self.assertLess(r["dblog"].index(["fetch", "voice_delete"]), r["dblog"].index(["deleteUser", USER]))
        self.assertEqual(r["deleted"], [USER])

    def test_account_deletion_goes_on_when_the_voice_cant_be_deleted(self):
        for id in ("d_voice_fails", "d_no_key"):
            r = self.sc(id)[0]
            self.assertEqual(r["status"], 200, id)
            self.assertEqual(r["deleted"], [USER], id)
            self.assertTrue(any('{"voice_orphan":"voiceDEL1"}' in l for l in r["logs"]), id)
        r = self.sc("d_no_voice")[0]
        self.assertEqual((r["status"], r["calls"], r["deleted"]), (200, [], [USER]))

    def test_account_deletion_stops_when_the_voice_or_the_customer_cant_be_read(self):
        # the rows cascade away with the user: a failed read must not pass as "nothing there"
        for id in ("d_lookup_fails", "d_profile_fails"):
            r = self.sc(id)[0]
            self.assertEqual(self.err(r), (500, "server_error"), id)
            self.assertEqual(r["calls"], [], id)                                 # no Stripe, no ElevenLabs
            self.assertEqual(r["deleted"], [], id)
            self.assertNotIn(["update", "feedback"], r["dblog"], id)
            self.assertEqual(self.voices(r)[0]["voice_id"], "voiceDEL1", id)
        # a database without the Interpreter tables has no voice
        r = self.sc("d_no_voice_table")[0]
        self.assertEqual((r["status"], r["calls"], r["deleted"]), (200, [], [USER]))

    # ── secrets ────────────────────────────────────────────────────────────
    def test_no_key_ever_reaches_a_response_or_a_log(self):
        secrets = [ELEVEN_KEY, "sk_other_name_123", "sk-test-OPENKEY-0001", "mistral-key-0001", "wk-live.ws-live-0001"]
        for id, results in self.out.items():
            for r in results:
                blob = r["text"] + "\n" + "\n".join(r["logs"])
                for secret in secrets:
                    self.assertNotIn(secret, blob, id)


class TestInterpreterSource(unittest.TestCase):
    """Cheap checks on the migration and the allow-list (these run even without Node)."""

    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_the_three_tables_with_rls_and_read_own_policies(self):
        for t in ("interpreter_voices", "interpreter_usage", "interpreter_sessions"):
            self.assertIn(f"create table if not exists public.{t}", self.sql)
            self.assertIn(f"alter table public.{t}", self.sql)
            self.assertRegex(self.sql, rf"create policy \"[^\"]+\" on public\.{t}\s+for select using \(\(select auth\.uid\(\)\) = user_id\)")
        self.assertNotRegex(self.sql, r"for (insert|update|delete|all)")
        self.assertIn("references auth.users(id) on delete cascade", self.sql)
        self.assertIn("primary key (user_id, month)", self.sql)

    def test_the_seconds_rpc_is_service_role_only(self):
        self.assertIn("create or replace function public.use_interpreter_seconds(p_user_id uuid, p_seconds integer, "
                      "p_allowance integer)", self.sql)
        fn = self.sql.split("public.use_interpreter_seconds(", 1)[1].split("$$;", 1)[0]
        self.assertIn("security definer", fn)
        self.assertIn("on conflict (user_id, month)", fn)
        self.assertIn("revoke all on function public.use_interpreter_seconds(uuid, integer, integer) "
                      "from public, anon, authenticated;", self.sql)
        self.assertIn("grant execute on function public.use_interpreter_seconds(uuid, integer, integer) to service_role;",
                      self.sql)
        self.assertNotRegex(self.sql, r"use_interpreter_seconds\([^)]*\) to [^;]*authenticated")

    def test_the_interpret_ceiling_rpc(self):
        self.assertIn("add column if not exists interpret_requests", self.sql)
        self.assertIn("grant execute on function public.use_interpret_quota(integer) to authenticated;", self.sql)

    def test_the_ceilings_and_the_voice_swap_are_service_role_only(self):
        sql = MIGRATION_FIXES.read_text(encoding="utf-8")
        for sig in ("use_interpreter_tokens(uuid, integer, integer)", "use_voice_preview_chars(uuid, integer, integer)",
                    "swap_interpreter_voice(uuid, text, text, text, timestamptz)"):
            self.assertRegex(sql, rf"revoke all on function public\.{re.escape(sig)}\s+from public, anon, authenticated;")
            self.assertIn(f"grant execute on function public.{sig} to service_role;", sql)
            self.assertNotRegex(sql, rf"{re.escape(sig)} to [^;]*authenticated")
        self.assertEqual(sql.count("security definer"), 3)
        for col in ("interpreter_tokens", "voice_preview_chars"):
            self.assertIn(f"add column if not exists {col} integer not null default 0", sql)
        # one swap per user at a time, also for a first voice (no row to lock yet)
        swap = sql.split("public.swap_interpreter_voice(", 1)[1].split("$$;", 1)[0]
        self.assertLess(swap.index("pg_advisory_xact_lock"), swap.index("select voice_id into prev"))
        self.assertIn("on conflict (user_id) do update", swap)
        self.assertIn("return prev;", swap)

    def test_the_server_allow_lists_the_interpreter_events(self):
        ts = ANALYTICS.read_text(encoding="utf-8")
        for e in ("interpreter_started", "interpreter_stopped", "interpreter_failed", "voice_created", "voice_deleted"):
            self.assertIn(f"'{e}'", ts)


if __name__ == "__main__":
    unittest.main()
