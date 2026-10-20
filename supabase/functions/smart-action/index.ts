// Managed Smart Actions proxy - the Pro AI brain.
//
// Pro users run Smart Actions (translate / rewrite / summarize / email /
// meeting notes) and the Live Assistance through SERVER-held keys, so they never
// need their own. Flow: verify the caller + is_pro() + daily quota (in
// parallel - one round trip, not three) -> model -> text, streamed when asked.
//
// Routing - each request tries the configured engines in order, moving on if
// one is missing, fails, or (streamed) hasn't produced its first words in time:
// * Live Assistance answers (mode "live_assist"): the live model on Modal
//   (DeepSeek V4.1 Flash - vision, thinking off) -> Claude Haiku 5.5 (thinking
//   off, vision) -> Gemini Flash (thinking off) -> Mistral Small. Streamed.
// * Smart Actions (the app names their mode, SMART_MODES): the live model ->
//   Claude Haiku 5.5 -> Mistral Small -> Gemini Flash. Not streamed: the live
//   model gets SMART_LIVE_MS for the whole answer, Claude CLAUDE_SMART_MS.
// * Meeting notes (mode "meeting_notes"): Claude Sonnet 5.5 (thinking off,
//   CLAUDE_NOTES_MS for the whole notes) -> Mistral Small -> Gemini Flash.
// * Everything else (the live recap, apps before 2.0 that send no mode):
//   Mistral Small -> Gemini Flash.
// Claude runs through the official Anthropic SDK; its answers are turned into
// the same OpenAI-style JSON / event stream the app reads from the others.
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
//                          default 8000 - raise it for a scale-to-zero endpoint),
//          GEMINI_CHAT_MODEL (optional, default gemini-2.5-flash),
//          CLAUDE_LLM_KEY (the founder's Anthropic API key; optional - without
//                          it the Claude steps are skipped).
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
const LIVE_TIMEOUT_MS = Number(Deno.env.get("LIVE_LLM_TIMEOUT_MS")) || 8_000;
const LIVE_FALLBACK_MS = 12_000;  // Gemini/Mistral standing in for the live model
const START_TIMEOUT_MS = 30_000;  // hosted APIs, other streamed actions
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
const CLAUDE_NOTES_MS = 25_000;   // meeting notes, the whole answer (the app waits 45 s)
const claude = CLAUDE_KEY ? new Anthropic({ apiKey: CLAUDE_KEY, maxRetries: 0 }) : null;
// Per isolate: has the Claude key been checked yet (a free models lookup, logged).
let claudeChecked = false;
// A cost-abuse ceiling, not a usage limit: ~10x a heavy day of dictation plus
// Live Assistance answers (one per question asked in a call).
const DAILY_CAP = 1000;

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
  wholeMs?: number;         // not streamed: time for the whole answer (0 = no limit)
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
// vision tower switched off). Screenshots then go to Gemini first - no point
// paying a failed round trip, with the image upload, on every Screen answer.
let liveNoVision = false;

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

async function liveModel(): Promise<string> {
  if (LIVE_MODEL_SETTING) return LIVE_MODEL_SETTING;
  if (liveModelCache) return liveModelCache;
  try {
    const r = await fetch(`${LIVE_URL}/models`, {
      headers: { "Authorization": `Bearer ${LIVE_TOKEN}` },
      signal: AbortSignal.timeout(5_000),
    });
    if (r.ok) {
      const d = await r.json();
      liveModelCache = String(d?.data?.[0]?.id ?? "");
    } else {
      console.error("live model lookup failed", r.status);
    }
  } catch (e) {
    console.error("live model lookup unreachable", String(e).slice(0, 160));
  }
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
// (or URL) blocks, no empty text, and must end on a user turn.
function claudeImage(url: string): Record<string, unknown> | null {
  const m = /^data:(image\/[a-zA-Z0-9.+-]+);base64,(.+)$/s.exec(url);
  if (m) return { type: "image", source: { type: "base64", media_type: m[1], data: m[2] } };
  if (/^https?:\/\//.test(url)) return { type: "image", source: { type: "url", url } };
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
// route.wholeMs. A refusal, an empty answer or any API error falls through to
// the next engine.
async function attemptClaude(route: Route, stream: boolean): Promise<Attempt> {
  const ctrl = new AbortController();
  const ms = stream ? route.startMs : (route.wholeMs ?? 0);
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
    const detail = ctrl.signal.aborted ? `no answer within ${ms} ms`
      : e instanceof Anthropic.APIError && e.status ? `HTTP ${e.status}: ${String(e.message).slice(0, 140)}`
      : String(e).slice(0, 160);
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
  const ms = live ? LIVE_FALLBACK_MS : START_TIMEOUT_MS;
  const gemini = GEMINI_KEY ? geminiRoutes(messages, maxTokens, stream, ms) : [];
  const mistral = MISTRAL_KEY ? [mistralRoute(messages, maxTokens, stream, ms)] : [];
  if (notes) {
    const c = claudeRoute(CLAUDE_NOTES_MODEL, messages, maxTokens, ms, CLAUDE_NOTES_MS);
    return [...(c ? [c] : []), ...mistral, ...gemini];
  }
  if (!live && !smart) return [...mistral, ...gemini];
  const fast = claudeRoute(CLAUDE_FAST_MODEL, messages, maxTokens, ms, smart ? CLAUDE_SMART_MS : 0);
  const backup = fast ? [fast] : [];
  let first: Route[] = [];
  if (LIVE_URL && LIVE_TOKEN && !(liveNoVision && hasImage(messages))) {
    const model = await liveModel();
    if (model) {
      // Smart Actions keep the user's words, so they get Mistral's near-fixed
      // temperature rather than the live answers' sampling.
      const route = smart ? liveRoute(model, messages, maxTokens, stream, { temperature: 0.1 })
                          : liveRoute(model, messages, maxTokens, stream);
      first = [{ ...route, wholeMs: smart ? SMART_LIVE_MS : 0 }];
    }
  }
  return live ? [...first, ...backup, ...gemini, ...mistral] : [...first, ...backup, ...mistral, ...gemini];
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
// the stream handed back replays everything read so far. Not streamed: no
// time limit unless the route sets wholeMs - a non-streamed server answers
// only when the whole text is done, so a timer would cap long meeting notes.
async function attempt(route: Route, body: Record<string, unknown>, stream: boolean): Promise<Attempt> {
  const ctrl = new AbortController();
  const ms = stream ? route.startMs : (route.wholeMs ?? 0);
  const timer = ms > 0 ? setTimeout(() => ctrl.abort(), ms) : undefined;
  const scanner = sseScanner();
  try {
    const res = await call(route, body, ctrl.signal);
    if (!res.ok) {
      const detail = (await res.text()).slice(0, 160);
      console.error(`${route.name} failed`, res.status, detail);
      const kind = res.status === 400 || res.status === 422 ? "rejected" : "failed";
      return { kind, detail, reasoning: 0 };
    }
    if ((!stream || !res.body) && route.wholeMs) {
      // A Smart Action from the live model: read it inside the time limit. No
      // answer left once reasoning is removed, or one cut off at max_tokens (a
      // template-opened think block shows no tag to find), goes to the next engine.
      const d = await res.json();
      const c = d?.choices?.[0] ?? {};
      const content = String(c.message?.content ?? "");
      if (!visibleAnswer(content, !!route.family) || c.finish_reason === "length") {
        const detail = `no usable answer (finish ${c.finish_reason ?? "?"}, think tags ${/<\/?think>/.test(content)})`;
        console.error(`${route.name} failed`, detail);
        return { kind: "failed", detail, reasoning: String(c.message?.reasoning_content ?? c.message?.reasoning ?? "").length };
      }
      return { kind: "ok", res: new Response(JSON.stringify(d)), body: null };
    }
    if (!stream || !res.body) return { kind: "ok", res, body: res.body };
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
    const detail = ctrl.signal.aborted ? `no answer within ${ms} ms` : String(e).slice(0, 160);
    console.error(`${route.name} unreachable`, detail, JSON.stringify(scanner.stats));
    return { kind: "failed", detail, reasoning: scanner.stats.reasoning };
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

// Watches a relayed live answer without changing a byte: logs how long the
// first words took and whether reasoning slipped through - a live model that
// thinks is slow and can end with an empty answer.
function observeStream(name: string, startedAt: number) {
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
      const info = { route: name, first_words_ms: firstChunkMs, content_chars: s.content,
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

  let body: any;
  try { body = await req.json(); } catch { return json({ error: "bad_request" }, 400); }
  const warmup = body?.warmup === true;
  const live = body?.mode === "live_assist";
  const smart = SMART_MODES.has(body?.mode);
  const notes = body?.mode === "meeting_notes";

  const supa = createClient(SUPABASE_URL, ANON_KEY, { global: { headers: { Authorization: authHeader } } });
  const [userRes, proRes, quotaRes] = await Promise.all([
    supa.auth.getUser(),
    supa.rpc("is_pro"),
    warmup ? Promise.resolve({ data: true, error: null })
           : supa.rpc("use_smart_action_quota", { max_per_day: DAILY_CAP }),
  ]);
  if (userRes.error || !userRes.data?.user) return json({ error: "unauthorized" }, 401);
  if (proRes.error) return json({ error: "entitlement_check_failed" }, 500);
  if (proRes.data !== true) return json({ error: "pro_required" }, 403);

  if (warmup) {
    if (LIVE_URL && LIVE_TOKEN) {
      const wake = liveModel().then((model) => {
        if (!model) return;
        const route = liveRoute(model, [{ role: "user", content: "ping" }], 1, false);
        return call(route, route.body).then((r) => r.body?.cancel());
      }).catch(() => {});
      if (typeof EdgeRuntime !== "undefined") EdgeRuntime.waitUntil(wake);
    }
    return json({ ok: true }, 202);
  }

  if (quotaRes.error) return json({ error: "quota_check_failed" }, 500);
  if (quotaRes.data !== true) return json({ error: "quota_exceeded" }, 429);

  const messages = body?.messages;
  const maxTokens = Math.min(Number(body?.max_tokens) || 600, 2000);
  const stream = body?.stream === true;
  if (!Array.isArray(messages) || messages.length === 0) return json({ error: "no_messages" }, 400);

  const routes = await routesFor(live, smart, notes, messages, maxTokens, stream);
  if (!routes.length) {
    console.error("no chat engine configured: set MISTRAL_STT_KEY, GOOGLE_STT_KEY or CLAUDE_LLM_KEY");
    return json({ error: "actions_not_configured" }, 503);
  }
  const withImage = hasImage(messages);
  const startedAt = Date.now();
  let ok: { res: Response; body: ReadableStream<Uint8Array> | null } | null = null;
  let used = "";
  let usedFamily = "";
  let lastDetail = "";
  routeLoop:
  for (const route of routes) {
    const bodies = [route.body, ...(route.variants ?? [])];
    for (let i = 0; i < bodies.length; i++) {
      const variant = route.variantIdx?.[i];
      const a = route.claude ? await attemptClaude(route, stream) : await attempt(route, bodies[i], stream);
      if (a.kind === "ok") {
        if (variant !== undefined) liveVariant = variant;   // the one this endpoint takes
        ok = a;
        used = route.name;
        usedFamily = route.family ?? "";
        break routeLoop;
      }
      lastDetail = a.detail;
      // The live model can't take the screenshot: the next engine (Claude or
      // Gemini, both vision) answers this one, and image requests skip the
      // live model from now on.
      if (a.kind === "rejected" && withImage && route.name === "live" && imageRejected(a.detail)) {
        liveNoVision = true;
        console.warn("live model refused the screenshot - screen answers go to the next engine");
        break;
      }
      // A rejected field: the next variant may be accepted. Anything else
      // (auth, rate limit, server error, too slow) - on to the next engine.
      if (a.kind !== "rejected") break;
    }
  }
  if (ok === null) return json({ error: "action_failed", detail: lastDetail }, 502);
  if (stream && ok.body) {
    // Relay the OpenAI-style event stream as is - the app renders each delta.
    const relayed = live ? ok.body.pipeThrough(observeStream(used, startedAt)) : ok.body;
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
                JSON.stringify({ mode: body.mode, route: used, total_ms: Date.now() - startedAt }));
  }
  return new Response(JSON.stringify({ text }), {
    headers: { ...cors, "Content-Type": "application/json", "X-Answer-Family": usedFamily },
  });
});
