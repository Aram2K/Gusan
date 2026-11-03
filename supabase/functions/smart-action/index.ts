// Managed Smart Actions proxy - the Pro AI brain.
//
// Pro users run Smart Actions (translate / rewrite / summarize / email /
// meeting notes) and the Live Assistance through SERVER-held keys, so they never
// need their own. Flow: verify the caller + entitlement + daily quota (in
// parallel - one round trip, not three) -> model -> text, streamed when asked.
//
// Free accounts are not Pro. They may still call this function for:
//   * one cloud meeting note per calendar month (mode "meeting_notes",
//     note_ref = the meeting id; a retry of the same id does not count again)
//   * Live Assistant answers inside the one open session they started
//     (mode "live_assist", session_id from begin_live_session)
// Other modes stay Pro-only. The daily cap below is an abuse ceiling, not a
// fair-use quota, and it still applies to Pro.
//
// Routing - each request tries the configured engines in order, moving on if
// one is missing, fails, or (streamed) hasn't produced its first words in time
// (not streamed: hasn't answered within its whole-answer budget):
// * Live Assistance answers (mode "live_assist"): the live model on Modal
//   (DeepSeek V4.1 Flash - vision, thinking off) -> OpenAI (OPENAI_LIVE_MODEL,
//   reasoning off, vision) -> Claude Haiku 5.5 (thinking off, vision) -> Mistral
//   Small -> Gemini Flash (thinking off). Streamed.
// * Smart Actions (the app names their mode, SMART_MODES): the live model ->
//   OpenAI (OPENAI_FAST_MODEL) -> Claude Haiku 5.5 -> Mistral Small -> Gemini
//   Flash. The app streams them: every engine gets SMART_START_MS to its first
//   words. Not streamed: the live model gets SMART_LIVE_MS for the whole answer,
//   OpenAI OPENAI_SMART_MS, Claude CLAUDE_SMART_MS.
// * Meeting notes (mode "meeting_notes"): OpenAI (OPENAI_NOTES_MODEL,
//   OPENAI_NOTES_MS for the whole notes) -> Claude Sonnet 5.5 (thinking off,
//   CLAUDE_NOTES_MS) -> Mistral Small -> Gemini Flash (NOTES_TAIL_MS each).
// * Everything else (the live recap, apps before 2.0 that send no mode):
//   Mistral Small -> OpenAI (OPENAI_FAST_MODEL) -> Gemini Flash.
// Not streamed, EVERY engine has a whole-answer time limit (its own, else
// WHOLE_CAP_MS), and the chain as a whole stays inside NON_STREAM_DEADLINE_MS,
// so the app's 45 s wait always ends with an answer or a clean error.
// Claude runs through the official Anthropic SDK; its answers are turned into
// the same OpenAI-style JSON / event stream the app reads from the others.
// Without a key an engine is simply left out of the chain.
// * {"warmup": true} wakes the live endpoint (Pro users only) and returns at
//   once; the app sends it when a session starts. A Modal Shared Endpoint
//   never sleeps, but a dedicated one can scale to zero.
//
// Secrets (at least one of the chat keys must be set):
//          MISTRAL_STT_KEY (the founder's Mistral key; also used for speech),
//          GOOGLE_STT_KEY or GEMINI_KEY (a Google AI Studio / Gemini key; also speech),
//          LIVE_LLM_URL   (the Modal endpoint URL, with or without /v1),
//          LIVE_LLM_TOKEN (Modal proxy token: "<token id>.<token secret>",
//                          i.e. "wk-....ws-..."),
//          LIVE_LLM_MODEL (e.g. deepseek-ai/DeepSeek-V4.1-Flash; when unset the
//                          endpoint's /v1/models is asked),
//          LIVE_LLM_TIMEOUT_MS (optional: first-words budget for the live model,
//                          1000-60000, default 8000 - raise it for a scale-to-zero
//                          endpoint),
//          GEMINI_CHAT_MODEL (optional, default gemini-2.5-flash),
//          CLAUDE_LLM_KEY (the founder's Anthropic API key; optional - without
//                          it the Claude steps are skipped),
//          OPENAI_TOKEN   (the founder's OpenAI key; OPENAI_API_KEY or OPENAI_KEY
//                          work too; optional - without it the OpenAI steps are
//                          skipped),
//          OPENAI_FAST_MODEL  (optional, default gpt-5.4-mini: Smart Actions and
//                          the generic fallback),
//          OPENAI_LIVE_MODEL  (optional, default gpt-5.4-nano: Live Assistance),
//          OPENAI_NOTES_MODEL (optional, default gpt-4.1: meeting notes; e.g.
//                          gpt-5.5 for more quality). A model name is used only
//                          if it is a plain model id, else the default is.
// Auto-provided: SUPABASE_URL, SUPABASE_ANON_KEY. Deploy with verify_jwt=false.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import Anthropic from "npm:@anthropic-ai/sdk@0.133.0";

declare const EdgeRuntime: { waitUntil(p: Promise<unknown>): void } | undefined;

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const MISTRAL_KEY = Deno.env.get("MISTRAL_STT_KEY") ?? "";
const MODEL = "mistral-small-latest"; // good-enough, fast + cheap
const LIVE_URL = liveBaseUrl(Deno.env.get("LIVE_LLM_URL") ?? "");
const LIVE_TOKEN = (Deno.env.get("LIVE_LLM_TOKEN") ?? "").trim();
const LIVE_MODEL_SETTING = (Deno.env.get("LIVE_LLM_MODEL") ?? "").trim();
const GEMINI_KEY = Deno.env.get("GOOGLE_STT_KEY") || Deno.env.get("GEMINI_KEY") || ""; // either name works
const GEMINI_MODEL = Deno.env.get("GEMINI_CHAT_MODEL") ?? "gemini-2.5-flash";
const GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions";
// Budgets for a STREAMED request's first words (headers alone don't count: an
// OpenAI-compatible server sends 200 + event-stream headers before the request
// even reaches its queue). Past it, the next engine answers instead.
const LIVE_TIMEOUT_MS = (() => {
  const n = Number(Deno.env.get("LIVE_LLM_TIMEOUT_MS"));
  return Number.isFinite(n) && n >= 1_000 && n <= 60_000 ? Math.floor(n) : 8_000;   // a bad value = the default
})();
const LIVE_FALLBACK_MS = 12_000;  // Gemini/Mistral standing in for the live model
const START_TIMEOUT_MS = 30_000;  // hosted APIs, other streamed actions
const SMART_START_MS = 6_000;     // a streamed Smart Action: a warm engine starts within ~1 s
// Not streamed: no engine may hold the chain. WHOLE_CAP_MS is the whole-answer
// limit of an engine without a budget of its own; the chain as a whole stays
// inside NON_STREAM_DEADLINE_MS (the app waits 45 s), but every engine keeps at
// least MIN_LEFT_MS.
const WHOLE_CAP_MS = 20_000;
const NON_STREAM_DEADLINE_MS = 40_000;
const MIN_LEFT_MS = 3_000;
const NOTES_TAIL_MS = 10_000;     // Mistral / Gemini behind the two premium engines for meeting notes
// Alt+S and the actions it picks (actions.py ACTION_MODES). A Smart Action is
// short and arrives whole, so a stalled GPU queue gets SMART_LIVE_MS before
// Mistral answers instead - well inside the app's 45 s.
const SMART_MODES = new Set(["smart_auto", "write_email", "make_todo_list", "translate", "summarize"]);
const SMART_LIVE_MS = 15_000;
// Claude (the founder's key). Haiku 5.5 is the fast backup for Smart Actions
// and Live Assistance; Sonnet 5.5 writes meeting notes first. Both with
// thinking off - speed first. The router does its own fallbacks, so the SDK
// never retries (a retry would only delay the next engine).
const CLAUDE_KEY = (Deno.env.get("CLAUDE_LLM_KEY") ?? "").trim();
const CLAUDE_FAST_MODEL = "claude-haiku-5-5";
const CLAUDE_NOTES_MODEL = "claude-sonnet-5-5";
const CLAUDE_SMART_MS = 12_000;   // a Smart Action from Claude, the whole answer
const CLAUDE_NOTES_MS = 14_000;   // meeting notes, the whole answer (with OPENAI_NOTES_MS and NOTES_TAIL_MS under the app's 45 s)
// OpenAI (the founder's key). Chat completions, reasoning switched off by model
// family (see openaiRoute). The model names are secrets so they can change
// without a deploy; a name that isn't a plain model id is ignored.
const OPENAI_KEY = [Deno.env.get("OPENAI_TOKEN"), Deno.env.get("OPENAI_API_KEY"), Deno.env.get("OPENAI_KEY")]
  .map((v) => (v ?? "").trim()).find((v) => v) ?? "";
function openaiModelName(setting: string | undefined, fallback: string): string {
  const v = (setting ?? "").trim();
  return /^[a-z0-9][a-z0-9._-]{1,60}$/i.test(v) ? v : fallback;
}
const OPENAI_FAST_MODEL = openaiModelName(Deno.env.get("OPENAI_FAST_MODEL"), "gpt-5.4-mini");
const OPENAI_LIVE_MODEL = openaiModelName(Deno.env.get("OPENAI_LIVE_MODEL"), "gpt-5.4-nano");
const OPENAI_NOTES_MODEL = openaiModelName(Deno.env.get("OPENAI_NOTES_MODEL"), "gpt-4.1");
const OPENAI_URL = "https://api.openai.com/v1/chat/completions";
const OPENAI_SMART_MS = 12_000;   // a Smart Action from OpenAI, the whole answer
const OPENAI_NOTES_MS = 18_000;   // meeting notes, the whole answer (the two premium engines together stay under the app's 45 s)
const claude = CLAUDE_KEY ? new Anthropic({ apiKey: CLAUDE_KEY, maxRetries: 0 }) : null;
// Per isolate: has the Claude key been checked yet (a free models lookup, logged).
let claudeChecked = false;
// A cost-abuse ceiling, not a product quota: ~10x a heavy day of dictation plus
// Live Assistance answers (one per question asked in a call). Pro has no
// monthly minute cap; this per-day ceiling stays so a loop cannot run up the bill.
const DAILY_CAP = 1000;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// A long meeting transcript is well under 1 MB; screenshots are downscaled JPEGs.
const MAX_BODY_BYTES = 8 * 1024 * 1024;
const MAX_MESSAGES = 200;

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

// "https://x.modal.run", ".../v1", ".../v1/" and ".../v1/chat/completions" all
// mean the same endpoint - the secret is pasted from Modal's dashboard.
export function liveBaseUrl(raw: string): string {
  let url = raw.trim().replace(/\/+$/, "");
  if (!url) return "";
  url = url.replace(/\/chat\/completions$/, "");
  return /\/v1$/.test(url) ? url : `${url}/v1`;
}

type Route = {
  name: string; url: string; key: string; body: Record<string, unknown>; startMs: number;
  // Same request with other ways of saying "don't think", tried in turn when
  // the server rejects a field (HTTP 400/422); variantIdx[i] is the
  // NO_THINK_VARIANTS index of [body, ...variants][i].
  variants?: Record<string, unknown>[];
  variantIdx?: number[];
  family?: string;          // "deepseek" / "qwen": a template-opened thinking model
  wholeMs?: number;         // not streamed: time for the whole answer (unset / 0: capMs, else WHOLE_CAP_MS)
  capMs?: number;           // not streamed: the whole-answer limit of an engine with no wholeMs
  claude?: Record<string, unknown>;   // a Claude request (Anthropic SDK), not an OpenAI one
};

function modelFamily(model: string): string {
  const m = model.toLowerCase();
  return m.includes("deepseek") ? "deepseek" : m.includes("qwen") ? "qwen" : "";
}

function mistralRoute(messages: unknown[], maxTokens: number, stream: boolean, startMs: number): Route {
  return {
    name: "mistral",
    url: "https://api.mistral.ai/v1/chat/completions",
    key: MISTRAL_KEY,
    body: { model: MODEL, messages, temperature: 0.1, max_tokens: maxTokens, stream },
    startMs,
  };
}

// ── OpenAI ──────────────────────────────────────────────────────────────────
// Reasoning must not run (it delays the first word by seconds): gpt-5.x and
// gpt-6 take reasoning_effort "none", gpt-5 / -mini / -nano need "minimal", the
// o-series' lowest is "low", and the gpt-4 family has no such field at all (a
// 400). Only the gpt-4 family takes a temperature - the gpt-5 / o models refuse
// a custom one. If a model still rejects reasoning_effort (HTTP 400/422), the
// same request without it is the one variant; it has no variantIdx, so it
// never touches the live model's variant state.
function openaiEffort(model: string): string | null {
  const m = model.toLowerCase();
  if (/^gpt-5\.\d|^gpt-6/.test(m)) return "none";
  if (/^gpt-5(-|$)/.test(m)) return "minimal";
  if (/^o\d/.test(m)) return "low";
  return null;
}

function openaiRoute(model: string, messages: unknown[], maxTokens: number, stream: boolean,
                     startMs: number, wholeMs: number, temperature?: number): Route {
  const effort = openaiEffort(model);
  const plain: Record<string, unknown> = { model, messages, max_completion_tokens: maxTokens, stream };
  if (effort === null && temperature !== undefined && /^gpt-4/i.test(model)) plain.temperature = temperature;
  return {
    name: "openai",
    url: OPENAI_URL,
    key: OPENAI_KEY,
    body: effort ? { ...plain, reasoning_effort: effort } : plain,
    variants: effort ? [plain] : undefined,
    startMs,
    wholeMs,
  };
}

// ── the live model (Modal) ──────────────────────────────────────────────────
// Thinking must be OFF: DeepSeek V4.x thinks by default, and with the short
// max_tokens of a live answer the reasoning trace eats the whole budget and
// the answer comes back empty. vLLM's DeepSeek V4 template reads
// chat_template_kwargs {thinking} (Qwen3 reads {enable_thinking}; both keys may
// be sent as long as they agree); newer servers also take the OpenAI field
// reasoning_effort "none". Servers differ in what they accept, so the ways of
// asking are tried in order and the first one the endpoint takes is kept. The
// first carries every switch and the others are subsets, so only a REJECTED
// field (HTTP 400/422) moves on - a leak can't be fixed by sending less.
const NO_THINK_VARIANTS: Record<string, unknown>[] = [
  { chat_template_kwargs: { thinking: false, enable_thinking: false }, reasoning_effort: "none" },
  { chat_template_kwargs: { thinking: false, enable_thinking: false } },
  { reasoning_effort: "none" },
];
let liveVariant = 0;          // per isolate: the variant the endpoint accepted last
let liveModelCache = "";      // per isolate: model id discovered via /v1/models
// Per isolate: the live endpoint refused an image (a text-only model, or its
// vision tower switched off). Screenshots then skip it for a while - no point
// paying a failed round trip, with the image upload, on every Screen answer. It
// is only for a while: one bad image from one user must not switch the live
// model off for everybody until the isolate recycles.
let liveNoVisionUntil = 0;
const LIVE_NO_VISION_MS = 5 * 60_000;

// Only inline images go upstream: the live endpoint (and Claude) would fetch any
// other URL from inside our network. Other image parts are dropped; a message
// left with no parts is dropped too.
function onlyDataImages(messages: unknown[]): unknown[] {
  const out: unknown[] = [];
  for (const m of messages) {
    const c = (m as { content?: unknown })?.content;
    if (!Array.isArray(c)) { out.push(m); continue; }
    const kept = c.filter((p: { type?: string; image_url?: { url?: unknown } | string }) => {
      if (p?.type !== "image_url") return true;
      const u = typeof p.image_url === "string" ? p.image_url : p.image_url?.url;
      return typeof u === "string" && /^data:image\/[a-zA-Z0-9.+-]+;base64,/.test(u);
    });
    if (kept.length === c.length) out.push(m);
    else if (kept.length) out.push({ ...(m as object), content: kept });
  }
  return out;
}

function hasImage(messages: unknown[]): boolean {
  return messages.some((m) => Array.isArray((m as { content?: unknown })?.content) &&
    ((m as { content: { type?: string }[] }).content).some((p) => p?.type === "image_url"));
}

// An HTTP 400/422 about the image, not about a "don't think" field.
function imageRejected(detail: string): boolean {
  return /image|vision|multimodal|multi-modal|visual|pixel|mm_|modalit/i.test(detail);
}

function liveSampling(model: string): Record<string, unknown> {
  const m = model.toLowerCase();
  // Qwen3.x: the model card's non-thinking sampling.
  if (m.includes("qwen")) return { temperature: 0.7, top_p: 0.8, top_k: 20, presence_penalty: 1.5 };
  // DeepSeek (card: temperature 1.0 / top_p 0.95 for thinking benchmarks) -
  // a little cooler for short, factual live answers.
  if (m.includes("deepseek")) return { temperature: 0.6, top_p: 0.95 };
  return { temperature: 0.3 };
}

// A failed lookup is not repeated for LIVE_MODEL_RETRY_MS (an endpoint that is
// down must not cost every request a wait); a warmup (force) tries again, and
// gives a cold endpoint longer. Setting LIVE_LLM_MODEL skips the lookup.
let liveModelFailedAt = 0;
const LIVE_MODEL_RETRY_MS = 30_000;

async function liveModel(force = false): Promise<string> {
  if (LIVE_MODEL_SETTING) return LIVE_MODEL_SETTING;
  if (liveModelCache) return liveModelCache;
  if (!force && liveModelFailedAt && Date.now() - liveModelFailedAt < LIVE_MODEL_RETRY_MS) return "";
  try {
    const r = await fetch(`${LIVE_URL}/models`, {
      headers: { "Authorization": `Bearer ${LIVE_TOKEN}` },
      signal: AbortSignal.timeout(force ? 10_000 : 2_000),
    });
    if (r.ok) {
      const d = await r.json();
      liveModelCache = String(d?.data?.[0]?.id ?? "");
    } else {
      console.error("live model lookup failed", r.status);
    }
  } catch (e) {
    console.error("live model lookup unreachable", scrub(String(e), LIVE_TOKEN).slice(0, 160));
  }
  if (!liveModelCache) liveModelFailedAt = Date.now();
  return liveModelCache;
}

function liveRoute(model: string, messages: unknown[], maxTokens: number, stream: boolean,
                   sampling: Record<string, unknown> = liveSampling(model)): Route {
  const base = { model, messages, max_tokens: maxTokens, stream, ...sampling };
  const n = NO_THINK_VARIANTS.length;
  const start = liveVariant;
  const idx = Array.from({ length: n }, (_, i) => (start + i) % n);
  const bodies = idx.map((i) => ({ ...base, ...NO_THINK_VARIANTS[i] }));
  return {
    name: "live",
    url: `${LIVE_URL}/chat/completions`,
    key: LIVE_TOKEN,
    body: bodies[0],
    variants: bodies.slice(1),
    variantIdx: idx,
    startMs: LIVE_TIMEOUT_MS,
    family: modelFamily(model),
  };
}

// Gemini through Google's OpenAI-compatible endpoint: same messages (images
// included) and the same event stream as Mistral. 2.5 Flash with thinking
// off answers fastest; if that model is retired, the "latest Flash" alias
// (which can't switch thinking off, only keep it low) takes over.
function geminiRoutes(messages: unknown[], maxTokens: number, stream: boolean, startMs: number): Route[] {
  const base = { url: GEMINI_URL, key: GEMINI_KEY, startMs };
  return [
    { ...base, name: "gemini", body: { model: GEMINI_MODEL, messages, max_tokens: maxTokens,
      stream, temperature: 0.2, reasoning_effort: "none" } },
    { ...base, name: "gemini-latest", body: { model: "gemini-flash-latest", messages,
      max_tokens: maxTokens, stream, temperature: 0.2, reasoning_effort: "low" } },
  ];
}

// ── Claude (Anthropic SDK) ──────────────────────────────────────────────────
// The app sends OpenAI-style messages: system turns, text, and screenshots as
// image_url data URLs. Claude takes the system text apart, images as base64
// blocks, no empty text, and must end on a user turn.
function claudeImage(url: string): Record<string, unknown> | null {
  const m = /^data:(image\/[a-zA-Z0-9.+-]+);base64,(.+)$/s.exec(url);
  if (m) return { type: "image", source: { type: "base64", media_type: m[1], data: m[2] } };
  return null;
}

function claudeMessages(messages: unknown[]): { system: string; messages: Record<string, unknown>[] } | null {
  const system: string[] = [];
  const out: Record<string, unknown>[] = [];
  for (const raw of messages) {
    const m = raw as { role?: string; content?: unknown };
    const parts: Record<string, unknown>[] = [];
    if (typeof m?.content === "string") {
      if (m.content.trim()) parts.push({ type: "text", text: m.content });
    } else if (Array.isArray(m?.content)) {
      for (const p of m.content as { type?: string; text?: string; image_url?: { url?: string } | string }[]) {
        if (p?.type === "text" && typeof p.text === "string" && p.text.trim()) {
          parts.push({ type: "text", text: p.text });
        } else if (p?.type === "image_url") {
          const url = typeof p.image_url === "string" ? p.image_url : p.image_url?.url ?? "";
          const img = claudeImage(url);
          if (img) parts.push(img);
        }
      }
    }
    if (!parts.length) continue;
    if (m.role === "system") {
      for (const p of parts) if (p.type === "text") system.push(String(p.text));
    } else if (m.role === "user" || m.role === "assistant") {
      out.push({ role: m.role, content: parts });
    }
  }
  // Claude takes no assistant prefill: the last turn must be the user's.
  if (!out.length || out[0].role !== "user" || out[out.length - 1].role !== "user") return null;
  return { system: system.join("\n\n"), messages: out };
}

function claudeRoute(model: string, messages: unknown[], maxTokens: number, startMs: number,
                     wholeMs: number): Route | null {
  if (!claude) return null;
  const conv = claudeMessages(messages);
  if (!conv) return null;
  // Thinking off: Haiku 5.5 takes {type: "disabled"} at effort low-high;
  // Sonnet 5.5's lowest setting is {type: "between_tools"} (no other field).
  // Neither takes a temperature other than its default, so none is sent.
  const fast = model === CLAUDE_FAST_MODEL;
  const params: Record<string, unknown> = {
    model,
    max_tokens: maxTokens,
    messages: conv.messages,
    thinking: fast ? { type: "disabled" } : { type: "between_tools" },
    // Speed first: Haiku at "low"; Sonnet's notes at "medium" (between_tools
    // is accepted at "high" or below) so long notes finish well inside
    // CLAUDE_NOTES_MS.
    output_config: { effort: fast ? "low" : "medium" },
    ...(conv.system ? { system: conv.system } : {}),
  };
  return { name: fast ? "claude-haiku" : "claude-sonnet", url: "", key: "", body: {}, startMs, wholeMs,
           claude: params };
}

// One OpenAI-style event-stream chunk, as the app and observeStream() read it.
function sseChunk(delta: Record<string, unknown>, finish: string | null = null): string {
  return `data: ${JSON.stringify({ choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`;
}

function openaiFinish(stop: string | null | undefined): string {
  return stop === "max_tokens" ? "length" : "stop";
}

// One Claude request, with the same rules as attempt(): streamed, it succeeds
// once the first words arrive within route.startMs and the returned stream
// replays them; not streamed, the whole answer must arrive within
// its whole-answer budget (wholeBudget). A refusal, an empty answer or any API
// error falls through to the next engine.
async function attemptClaude(route: Route, stream: boolean, leftMs: number): Promise<Attempt> {
  const ctrl = new AbortController();
  const ms = stream ? route.startMs : wholeBudget(route, leftMs);
  const timer = ms > 0 ? setTimeout(() => ctrl.abort(), ms) : undefined;
  try {
    if (!stream) {
      // deno-lint-ignore no-explicit-any
      const msg: any = await claude!.messages.create(route.claude as any, { signal: ctrl.signal });
      const text = (msg.content ?? []).filter((b: { type: string }) => b.type === "text")
        .map((b: { text: string }) => b.text).join("").trim();
      if (msg.stop_reason === "refusal" || !text) {
        const detail = `no usable answer (stop ${msg.stop_reason ?? "?"})`;
        console.error(`${route.name} failed`, detail);
        return { kind: "failed", detail, reasoning: 0 };
      }
      const d = { choices: [{ index: 0, message: { role: "assistant", content: text },
                              finish_reason: openaiFinish(msg.stop_reason) }] };
      return { kind: "ok", res: new Response(JSON.stringify(d)), body: null };
    }
    // deno-lint-ignore no-explicit-any
    const s: any = claude!.messages.stream(route.claude as any, { signal: ctrl.signal });
    const events: AsyncIterator<any> = s[Symbol.asyncIterator]();
    let first = "";
    while (!first) {
      const { done, value: ev } = await events.next();
      if (done) {
        console.error(`${route.name} failed`, "stream ended without an answer");
        return { kind: "failed", detail: "stream ended without an answer", reasoning: 0 };
      }
      if (ev?.type === "content_block_delta" && ev.delta?.type === "text_delta") first = ev.delta.text ?? "";
      if (ev?.type === "message_delta" && ev.delta?.stop_reason === "refusal") {
        console.error(`${route.name} failed`, "refusal");
        return { kind: "failed", detail: "refusal", reasoning: 0 };
      }
    }
    const enc = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(c) { c.enqueue(enc.encode(sseChunk({ role: "assistant", content: first }))); },
      async pull(c) {
        try {
          while (true) {
            const { done, value: ev } = await events.next();
            if (done) {
              // The SDK ends its iterator quietly on an error no read was
              // waiting for - a cut-off answer must not look finished.
              if (s.errored) { c.error(new Error(`${route.name} stream failed`)); return; }
              c.enqueue(enc.encode("data: [DONE]\n\n")); c.close(); return;
            }
            if (ev?.type === "content_block_delta" && ev.delta?.type === "text_delta" && ev.delta.text) {
              c.enqueue(enc.encode(sseChunk({ content: ev.delta.text })));
              return;
            }
            if (ev?.type === "message_delta" && ev.delta?.stop_reason === "refusal") {
              // Declined after the first words: what streamed is a fragment,
              // so the app shows this error instead of a half answer.
              console.warn(`${route.name} refused mid-answer`);
              c.enqueue(enc.encode(sseChunk({}, "content_filter")));
              c.enqueue(enc.encode(`data: ${JSON.stringify({ error: "Claude declined to answer this one." })}\n\n`));
              c.close();
              return;
            }
            if (ev?.type === "message_delta" && ev.delta?.stop_reason) {
              c.enqueue(enc.encode(sseChunk({}, openaiFinish(ev.delta.stop_reason))));
              return;
            }
          }
        } catch (e) { c.error(e); }
      },
      cancel() { try { s.abort(); } catch { /* already finished */ } },
    });
    return { kind: "ok", res: new Response(null), body };
  } catch (e) {
    // (A connection error or an error event mid-stream is an APIError with no status.)
    // Never the SDK's message: an API error text can quote the request.
    const detail = ctrl.signal.aborted ? `no answer within ${ms} ms`
      : e instanceof Anthropic.APIError && e.status
        // deno-lint-ignore no-explicit-any
        ? `HTTP ${e.status} ${String((e.error as any)?.error?.type ?? "").slice(0, 40)}`.trim()
      : scrub(String(e), CLAUDE_KEY).slice(0, 160);
    console.error(`${route.name} unreachable`, detail);
    return { kind: "failed", detail, reasoning: 0 };
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

// Once per isolate, before anything else: is the Claude key good, and can it
// use both models? A models lookup costs nothing; the result goes to the log
// ("claude key ok" / "claude key check failed"), never the key. Each lookup
// gets 10 s (the SDK's default is 10 minutes), so a hung connection can't keep
// the isolate's background work open.
async function checkClaudeKey(): Promise<void> {
  if (!claude) { console.log("claude key not set - Claude steps skipped"); return; }
  const found: string[] = [];
  for (const id of [CLAUDE_FAST_MODEL, CLAUDE_NOTES_MODEL]) {
    try {
      const m = await claude.models.retrieve(id, {}, { timeout: 10_000 });
      found.push(String(m?.id ?? id));
    } catch (e) {
      const why = e instanceof Anthropic.AuthenticationError ? "key rejected (401)"
        : e instanceof Anthropic.PermissionDeniedError ? "no access (403)"
        : e instanceof Anthropic.NotFoundError ? "model not found (404)"
        : e instanceof Anthropic.APIError && e.status ? `HTTP ${e.status}`
        : String(e).slice(0, 120);
      console.error("claude key check failed", JSON.stringify({ model: id, why }));
      return;
    }
  }
  console.log("claude key ok", JSON.stringify({ models: found }));
}

async function routesFor(live: boolean, smart: boolean, notes: boolean, messages: unknown[],
                         maxTokens: number, stream: boolean): Promise<Route[]> {
  const ms = live ? LIVE_FALLBACK_MS : smart ? SMART_START_MS : START_TIMEOUT_MS;
  const gemini = GEMINI_KEY ? geminiRoutes(messages, maxTokens, stream, ms) : [];
  const mistral = MISTRAL_KEY ? [mistralRoute(messages, maxTokens, stream, ms)] : [];
  const openai = (model: string, wholeMs: number, temperature?: number): Route[] =>
    OPENAI_KEY ? [openaiRoute(model, messages, maxTokens, stream, ms, wholeMs, temperature)] : [];
  if (notes) {
    const c = claudeRoute(CLAUDE_NOTES_MODEL, messages, maxTokens, ms, CLAUDE_NOTES_MS);
    const tail = [...mistral, ...gemini].map((r) => ({ ...r, capMs: NOTES_TAIL_MS }));
    return [...openai(OPENAI_NOTES_MODEL, OPENAI_NOTES_MS), ...(c ? [c] : []), ...tail];
  }
  if (!live && !smart) return [...mistral, ...openai(OPENAI_FAST_MODEL, OPENAI_SMART_MS), ...gemini];
  const fast = claudeRoute(CLAUDE_FAST_MODEL, messages, maxTokens, ms, smart ? CLAUDE_SMART_MS : 0);
  const backup = fast ? [fast] : [];
  const gpt = live ? openai(OPENAI_LIVE_MODEL, OPENAI_SMART_MS) : openai(OPENAI_FAST_MODEL, OPENAI_SMART_MS, 0.1);
  let first: Route[] = [];
  if (LIVE_URL && LIVE_TOKEN && !(Date.now() < liveNoVisionUntil && hasImage(messages))) {
    const model = await liveModel();
    if (model) {
      // Smart Actions keep the user's words, so they get Mistral's near-fixed
      // temperature rather than the live answers' sampling.
      const route = smart ? liveRoute(model, messages, maxTokens, stream, { temperature: 0.1 })
                          : liveRoute(model, messages, maxTokens, stream);
      first = [{ ...route, wholeMs: smart ? SMART_LIVE_MS : 0 }];
    }
  }
  return [...first, ...gpt, ...backup, ...mistral, ...gemini];
}

function call(route: Route, body: Record<string, unknown>, signal?: AbortSignal) {
  return fetch(route.url, {
    method: "POST",
    headers: { "Authorization": `Bearer ${route.key}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

// ── event-stream bookkeeping (lengths only, never text) ─────────────────────
function sseScanner() {
  const decoder = new TextDecoder();
  let buf = "";
  const s = { content: 0, reasoning: 0, finish: "", thinkTags: false };
  const scan = (line: string) => {
    if (!line.startsWith("data:")) return;
    const data = line.slice(5).trim();
    if (!data || data === "[DONE]") return;
    try {
      const choice = JSON.parse(data)?.choices?.[0] ?? {};
      const delta = choice.delta ?? {};
      if (typeof delta.content === "string") {
        s.content += delta.content.length;
        // Reasoning written into the answer itself (a server with no
        // reasoning parser): "<think>..." or a template-opened "...</think>".
        if (delta.content.includes("</think>") || delta.content.includes("<think>")) s.thinkTags = true;
      }
      const r = typeof delta.reasoning_content === "string" ? delta.reasoning_content
        : typeof delta.reasoning === "string" ? delta.reasoning : "";
      s.reasoning += r.length;
      if (choice.finish_reason) s.finish = String(choice.finish_reason);
    } catch { /* keep-alives and partial lines */ }
  };
  return {
    stats: s,
    push(chunk: Uint8Array) {
      buf += decoder.decode(chunk, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop() ?? "";
      for (const l of lines) scan(l.trim());
    },
    end() {
      buf += decoder.decode();
      if (buf) scan(buf.trim());
      buf = "";
    },
  };
}

// The chunks read so far, then the rest of the upstream stream. Cancelling it
// (the app closed the connection) cancels the upstream request too.
function replay(held: Uint8Array[], reader: ReadableStreamDefaultReader<Uint8Array>) {
  return new ReadableStream<Uint8Array>({
    start(c) { for (const b of held) c.enqueue(b); },
    async pull(c) {
      try {
        const { done, value } = await reader.read();
        if (done) c.close(); else c.enqueue(value);
      } catch (e) { c.error(e); }
    },
    cancel(reason) { return reader.cancel(reason); },
  });
}

// What the app keeps of an answer (action_api.strip_think): a leading <think>
// block goes, closed or not, and for a template-opened model everything up to
// its "</think>".
function visibleAnswer(content: string, templateOpened: boolean): string {
  let s = content.trimStart();
  if (s.startsWith("<think>")) {
    const end = s.indexOf("</think>");
    return end < 0 ? "" : s.slice(end + 8).trim();
  }
  if (templateOpened && s.includes("</think>")) s = s.slice(s.indexOf("</think>") + 8);
  return s.trim();
}

type Attempt =
  | { kind: "ok"; res: Response; body: ReadableStream<Uint8Array> | null }
  | { kind: "rejected" | "failed"; detail: string; reasoning: number };

// One request to one engine. Streamed: succeeds only once the first words of
// the answer have arrived within route.startMs (a queued request, or a model
// that thinks instead of answering, falls through to the next engine), and
// the stream handed back replays everything read so far. Not streamed: the
// whole answer (headers AND body) must arrive within wholeBudget(), whatever the
// engine; the validation of the answer is the same for every engine.
// An upstream error text only goes to the server log (never to the app), and no
// key may be in it (an API may echo the key it was sent, whole or masked).
function scrub(text: string, key: string): string {
  let s = text;
  for (const k of new Set([key, OPENAI_KEY, MISTRAL_KEY, GEMINI_KEY, LIVE_TOKEN, CLAUDE_KEY])) {
    if (k.length >= 8) s = s.split(k).join("[key]");
  }
  return s.replace(/\bsk-[A-Za-z0-9_*.-]{4,}/g, "[key]").replace(/Bearer\s+[A-Za-z0-9._~+\/=-]{8,}/gi, "Bearer [key]");
}

// What of a 400/422 body goes to the log: a validation error can quote the
// user's own text ("input"), so only its kind and the field it is about.
function logSafe(status: number, text: string): string {
  if (status !== 400 && status !== 422) return text.slice(0, 160);
  const all = (re: RegExp) => [...text.matchAll(re)].map((m) => m[1]).slice(0, 3).join(",");
  const parts = [`type=${all(/"type":\s*"([a-z_.]{1,40})"/g)}`, `code=${all(/"code":\s*"([a-z_.]{1,40})"/g)}`,
                 `param=${all(/"param":\s*"([a-z_.]{1,40})"/g)}`, `loc=${all(/"loc":\s*(\[[^\]]{0,80}\])/g)}`];
  return `rejected ${parts.join(" ")}`;
}

// The time a non-streamed engine gets for its whole answer: its own budget, else
// WHOLE_CAP_MS - and never more than what is left of the chain's deadline.
function wholeBudget(route: Route, leftMs: number): number {
  return Math.min(route.wholeMs || route.capMs || WHOLE_CAP_MS, Math.max(leftMs, MIN_LEFT_MS));
}

async function attempt(route: Route, body: Record<string, unknown>, stream: boolean, leftMs: number): Promise<Attempt> {
  const ctrl = new AbortController();
  const ms = stream ? route.startMs : wholeBudget(route, leftMs);
  const timer = ms > 0 ? setTimeout(() => ctrl.abort(), ms) : undefined;
  const scanner = sseScanner();
  try {
    const res = await call(route, body, ctrl.signal);
    if (!res.ok) {
      const full = scrub((await res.text()).slice(0, 4096), route.key);
      const detail = full.slice(0, 160);
      console.error(`${route.name} failed`, res.status, logSafe(res.status, full));
      const kind = res.status === 400 || res.status === 422 ? "rejected" : "failed";
      return { kind, detail, reasoning: 0 };
    }
    if (!stream || !res.body) {
      // Read the whole answer inside the time limit (a stalled body counts). No
      // answer left once reasoning is removed goes to the next engine - and so
      // does one cut off at max_tokens from a template-opened think model (no
      // tag to find). A cut-off answer from any other engine is a usable answer
      // (long meeting notes can fill their budget): it is kept, not rewritten.
      const d = await res.json();
      const c = d?.choices?.[0] ?? {};
      const content = String(c.message?.content ?? "");
      if (!visibleAnswer(content, !!route.family) || (route.family && c.finish_reason === "length")) {
        const detail = `no usable answer (finish ${c.finish_reason ?? "?"}, think tags ${/<\/?think>/.test(content)})`;
        console.error(`${route.name} failed`, detail);
        return { kind: "failed", detail, reasoning: String(c.message?.reasoning_content ?? c.message?.reasoning ?? "").length };
      }
      return { kind: "ok", res: new Response(JSON.stringify(d)), body: null };
    }
    const reader = res.body.getReader();
    const held: Uint8Array[] = [];
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        scanner.end();
        const detail = `stream ended without an answer (finish ${scanner.stats.finish || "?"})`;
        console.error(`${route.name} failed`, detail, JSON.stringify(scanner.stats));
        return { kind: "failed", detail, reasoning: scanner.stats.reasoning };
      }
      held.push(value);
      scanner.push(value);
      if (scanner.stats.content > 0) return { kind: "ok", res, body: replay(held, reader) };
    }
  } catch (e) {
    const detail = ctrl.signal.aborted ? `no answer within ${ms} ms` : scrub(String(e), route.key).slice(0, 160);
    console.error(`${route.name} unreachable`, detail, JSON.stringify(scanner.stats));
    return { kind: "failed", detail, reasoning: scanner.stats.reasoning };
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

// Watches a relayed live answer without changing a byte: logs how long the
// first words took and whether reasoning slipped through - a live model that
// thinks is slow and can end with an empty answer.
function observeStream(name: string, startedAt: number, model = "") {
  const scanner = sseScanner();
  let firstChunkMs = -1;
  return new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, ctrl) {
      ctrl.enqueue(chunk);
      if (firstChunkMs < 0) firstChunkMs = Date.now() - startedAt;
      scanner.push(chunk);
    },
    flush() {
      scanner.end();
      const s = scanner.stats;
      const info = { route: name, model, first_words_ms: firstChunkMs, content_chars: s.content,
                     reasoning_chars: s.reasoning, think_tags: s.thinkTags, finish: s.finish,
                     total_ms: Date.now() - startedAt };
      if (s.reasoning > 0 || s.thinkTags || s.content === 0) {
        // Thinking leaked (a reasoning field, or tags in the text - the app
        // strips those) or nothing came back. Logged for the founder only.
        console.warn("live answer degraded", JSON.stringify(info));
      } else {
        console.log("live answer", JSON.stringify(info));
      }
    },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  if (!claudeChecked) {
    // Once per isolate, in the background: log whether the Claude key works.
    claudeChecked = true;
    const check = checkClaudeKey().catch((e) => console.error("claude key check failed", String(e).slice(0, 120)));
    if (typeof EdgeRuntime !== "undefined") EdgeRuntime.waitUntil(check);
  }

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) return json({ error: "unauthorized" }, 401);
  // A user's access token is a JWT: anything else is dropped before the body is
  // read and before any backend call.
  if (!/^[\w-]+\.[\w-]+\.[\w-]+$/.test(authHeader.slice(7).trim())) return json({ error: "unauthorized" }, 401);
  // A cap on the request: a declared-large body is not even read; one that
  // declares nothing (chunked) is checked once read. Before the quota is spent.
  if (Number(req.headers.get("content-length") ?? 0) > MAX_BODY_BYTES) return json({ error: "too_large" }, 413);

  let body: any;
  try {
    const raw = await req.text();
    if (raw.length > MAX_BODY_BYTES) return json({ error: "too_large" }, 413);
    body = JSON.parse(raw);
  } catch { return json({ error: "bad_request" }, 400); }
  const warmup = body?.warmup === true;
  const live = body?.mode === "live_assist";
  const smart = SMART_MODES.has(body?.mode);
  const notes = body?.mode === "meeting_notes";

  const supa = createClient(SUPABASE_URL, ANON_KEY, { global: { headers: { Authorization: authHeader } } });
  const sessionId = typeof body?.session_id === "string" ? body.session_id.trim() : "";
  const noteRef = typeof body?.note_ref === "string" ? body.note_ref.slice(0, 200) : "";
  const [userRes, proRes, quotaRes] = await Promise.all([
    supa.auth.getUser(),
    supa.rpc("is_pro"),
    warmup ? Promise.resolve({ data: true, error: null })
           : supa.rpc("use_smart_action_quota", { max_per_day: DAILY_CAP }),
  ]);
  if (userRes.error || !userRes.data?.user) return json({ error: "unauthorized" }, 401);
  if (proRes.error) return json({ error: "entitlement_check_failed" }, 500);
  if (proRes.data !== true) {
    // Free: one cloud meeting note a month, and Live Assistant only inside
    // the session begin_live_session() already opened. Everything else is Pro.
    if (notes) {
      const { data: allowed, error } = await supa.rpc("consume_meeting_note", { note_ref: noteRef });
      if (error) return json({ error: "quota_check_failed" }, 500);
      if (allowed !== true) {
        return json({ error: "allowance_used", allowance: "meeting_notes" }, 402);
      }
    } else if (live || warmup) {
      const id = UUID_RE.test(sessionId) ? sessionId : null;
      const { data: open, error } = await supa.rpc("live_session_open", { p_session_id: id });
      if (error) return json({ error: "quota_check_failed" }, 500);
      if (open !== true) {
        return json({ error: "allowance_used", allowance: "live_session" }, 402);
      }
    } else {
      return json({ error: "pro_required" }, 403);
    }
  }

  if (warmup) {
    if (LIVE_URL && LIVE_TOKEN) {
      const wake = liveModel(true).then((model) => {
        if (!model) return;
        const route = liveRoute(model, [{ role: "user", content: "ping" }], 1, false);
        // A cold start can take a while - but not forever.
        return call(route, route.body, AbortSignal.timeout(90_000)).then((r) => r.body?.cancel());
      }).catch(() => {});
      if (typeof EdgeRuntime !== "undefined") EdgeRuntime.waitUntil(wake);
    }
    return json({ ok: true }, 202);
  }

  if (quotaRes.error) return json({ error: "quota_check_failed" }, 500);
  if (quotaRes.data !== true) return json({ error: "quota_exceeded" }, 429);

  // A whole number from 1 to 2000; anything else (negative, zero, fractional,
  // infinite, text) is the default - never an error, never "no limit".
  const rawMax = Math.trunc(Number(body?.max_tokens));
  const maxTokens = Number.isFinite(rawMax) && rawMax >= 1 ? Math.min(rawMax, 2000) : 600;
  const stream = body?.stream === true;
  if (!Array.isArray(body?.messages) || body.messages.length === 0 || body.messages.length > MAX_MESSAGES) {
    return json({ error: "no_messages" }, 400);
  }
  const messages = onlyDataImages(body.messages);
  if (messages.length === 0) return json({ error: "no_messages" }, 400);

  const startedAt = Date.now();
  const routes = await routesFor(live, smart, notes, messages, maxTokens, stream);
  if (!routes.length) {
    console.error("no chat engine configured: set MISTRAL_STT_KEY, GOOGLE_STT_KEY, OPENAI_TOKEN or CLAUDE_LLM_KEY");
    return json({ error: "actions_not_configured" }, 503);
  }
  const withImage = hasImage(messages);
  let ok: { res: Response; body: ReadableStream<Uint8Array> | null } | null = null;
  let used = "";
  let usedFamily = "";
  let usedModel = "";
  routeLoop:
  for (const route of routes) {
    const bodies = [route.body, ...(route.variants ?? [])];
    for (let i = 0; i < bodies.length; i++) {
      const variant = route.variantIdx?.[i];
      const left = NON_STREAM_DEADLINE_MS - (Date.now() - startedAt);
      const a = route.claude ? await attemptClaude(route, stream, left) : await attempt(route, bodies[i], stream, left);
      if (a.kind === "ok") {
        if (variant !== undefined) liveVariant = variant;   // the one this endpoint takes
        ok = a;
        used = route.name;
        usedFamily = route.family ?? "";
        usedModel = String((route.claude ?? route.body).model ?? "");
        break routeLoop;
      }
      // The live model can't take the screenshot: the next engine (Claude or
      // Gemini, both vision) answers this one, and image requests skip the
      // live model from now on.
      if (a.kind === "rejected" && withImage && route.name === "live" && imageRejected(a.detail)) {
        liveNoVisionUntil = Date.now() + LIVE_NO_VISION_MS;
        console.warn("live model refused the screenshot - screen answers skip it for a while");
        break;
      }
      // A rejected field: the next variant may be accepted. Anything else
      // (auth, rate limit, server error, too slow) - on to the next engine.
      if (a.kind !== "rejected") break;
    }
  }
  // What the engines said stays in the log: it can hold account and quota state.
  if (ok === null) return json({ error: "action_failed" }, 502);
  if (stream && ok.body) {
    // Relay the OpenAI-style event stream as is - the app renders each delta.
    const relayed = live ? ok.body.pipeThrough(observeStream(used, startedAt, usedModel)) : ok.body;
    return new Response(relayed, {
      headers: { ...cors, "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                 "X-Answer-Family": usedFamily },
    });
  }
  const d = await ok.res.json();
  const text = (d.choices?.[0]?.message?.content ?? "").trim();
  // Which engine wrote it and how long it took (no text) - for the founder.
  if (smart || notes) {
    console.log(smart ? "smart action" : "meeting notes",
                JSON.stringify({ mode: body.mode, route: used, model: usedModel, total_ms: Date.now() - startedAt }));
  }
  return new Response(JSON.stringify({ text }), {
    headers: { ...cors, "Content-Type": "application/json", "X-Answer-Family": usedFamily },
  });
});
