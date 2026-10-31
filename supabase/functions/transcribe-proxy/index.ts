// Managed cloud transcription proxy - the Pro moat.
//
// Flow: verify the caller's Supabase JWT -> confirm is_pro() server-side (paid
// subscription, lifetime purchase, or admin — not a trial) -> enforce a daily
// abuse cap (3000 requests/user/day; there is no monthly minute cap) ->
// transcribe with a SERVER-held key ->
// return the transcript. Keys never reach the client, so Pro cloud transcription
// cannot be used without a valid account + entitlement, even though the app is
// open source.
//
// Routing: a catalog of engines (CATALOG) and two profiles with an ordered list each:
//   FAST (dictation, the Live Assistant)  scribe_v2, voxtral-mini-latest, gpt-transcribe,
//                                         gpt-4o-mini-transcribe, gpt-4o-transcribe
//   BEST (the Audio to text page)         scribe_v2, gpt-transcribe, gpt-4o-transcribe,
//                                         voxtral-mini-latest, gpt-4o-mini-transcribe
// scribe_v2 is ElevenLabs Scribe (speech to text, POST /v1/speech-to-text, header xi-api-key): FIRST
// in both profiles while its credits last. It takes any language (it detects it), so no language
// excludes it; without a key it is simply skipped and nothing else changes. language_code is sent only
// when the client named ONE explicit code (a refused one is retried once without it); no audio-event
// tags, no diarization, no timestamps. Its batch endpoint is slow to answer a short clip, so its
// attempt never gets less than 8 s (unless STT_TIMEOUT_MS fixes every attempt).
// CREDITS BREAKER (per function instance, in memory): when ElevenLabs answers 402, 401 or 403, or says
// quota_exceeded / insufficient_credits / credits in its answer, it is SKIPPED (not tried, not an
// error, no quota spent for it) for STT_ELEVEN_PAUSE_MS (default 900000 = 15 min), so no request pays
// a failing call; after that the next request tries it again (the credits may be topped up). A 429
// pauses it for at most 15 s. An unhealthy Scribe has a SHORT breaker of its own: a timeout, a network
// error or a 5xx skips it for STT_ELEVEN_OUTAGE_PAUSE_MS (default 30000 = 30 s), and a 200 answer
// with no usable text (not JSON, no string `text`) for 60 s, so an outage costs one failing request,
// not every one. Neither ever shortens a longer pause. One log line {stt_pause, status, minutes} when
// a pause starts or changes kind (status 0 = a timeout or network error), never the key.
// The first engine that is usable runs; one that fails, errors or is too slow hands over to the
// next. Usable = its key exists and (Voxtral) the language is KNOWN and covered: Voxtral never runs
// for a language outside MISTRAL_LANGS (Armenian ...), nor when the language is not known - "auto",
// or "multi" with no list of at least one language - because nothing can catch its confident answer
// in another script afterwards. Those requests start on gpt-transcribe (about 0.3 s slower, it
// detects the language); STT_AUTO_VOXTRAL=1 puts Voxtral first for "auto" again (a conscious choice).
// The Gemini ids stay in the catalog and work when an order names them, but they are not in the
// default orders (that key is dead today).
//
// Request `model` (body field, else the X-Gusan-Model header; anything unknown or not a string is
// the FAST profile, never an error, and a name never reaches a URL):
//   "best" or "gemini-2.5-pro"    the BEST profile (the app's "Best accuracy" / Audio to text)
//   "fast", "gemini-2.5-flash",
//   unknown, missing              the FAST profile
//   any other catalog id          PINNED: that engine first, then the FAST order
// Old clients may also send `provider: "mistral"`: Voxtral first when no model is pinned.
// Apps 2.0.x send no `model` for Audio to text, so their files run the FAST profile (unless the
// user picked Gemini 2.5 Pro); a newer app asks for "best".
//
// Every attempt has a timeout (see attemptTimeoutMs) and the whole request has one deadline (see
// totalBudgetMs); the OpenAI language retry shares its attempt's timer. The answer says what really
// ran: `model` in the JSON and the X-Gusan-Model header. One log line per success {stt_ok, ms,
// tried} (no audio, no text) and one per failed attempt (provider, model, status, first 120 chars of
// the answer with keys and ids blanked). The client only ever gets a fixed code as `detail`
// (timeout, network_error, rate_limited, provider_auth, provider_credits, provider_unavailable,
// provider_rejected), never a provider's own text.
//
// Secrets: OPENAI_TOKEN (or OPENAI_API_KEY / OPENAI_KEY), MISTRAL_STT_KEY, GOOGLE_STT_KEY (or
//          GEMINI_KEY), ELEVENLABS_API_KEY (or ELEVENLABS_KEY / ELEVEN_API_KEY / XI_API_KEY, the first
//          one that is not empty). At least one must be set or the proxy returns 503.
//          Optional: STT_FAST_ORDER, STT_BEST_ORDER (comma-separated; an entry is a catalog id or
//          "provider:model" such as openai:gpt-transcribe-2 - provider mistral, openai, gemini or
//          elevenlabs (elevenlabs:scribe_v3) -
//          so a new model name needs no redeploy; an entry that is neither is dropped and logged;
//          nothing valid = the default order). Only these secrets can name a model outside the
//          catalog: a name a client sends is checked against the catalog.
//          STT_TIMEOUT_MS (a fixed per-attempt timeout, mostly for tests), STT_TOTAL_MS (the
//          deadline of the whole request), STT_AUTO_VOXTRAL ("1": Voxtral first for "auto"),
//          STT_ELEVEN_PAUSE_MS (how long the credits breaker skips ElevenLabs, default 900000),
//          STT_ELEVEN_OUTAGE_PAUSE_MS (the short breaker after a timeout, network error or 5xx, default 30000).
// Auto-provided by Supabase: SUPABASE_URL, SUPABASE_ANON_KEY.
// Deploy with verify_jwt=false; we validate the token via auth.getUser().

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const DAILY_CAP = 3000; // abuse ceiling, requests/user/day — not a product quota
// The largest audio accepted, in base64 characters (about 18 MB of 16 kHz WAV, 9 minutes: a file piece
// is at most 90 s, a dictation or live take a few minutes - apps before this limit sent any length, and
// OpenAI itself stops at 25 MB). A request over it is a 413 before anything is paid for.
const MAX_AUDIO_B64 = 24 * 1024 * 1024;

// Secrets are read per request (cheap, and a changed secret needs no code change).
function env(name: string): string {
  return (Deno.env.get(name) ?? "").trim();
}
const OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions";
const ELEVEN_URL = "https://api.elevenlabs.io/v1/speech-to-text";
const MISTRAL_MODEL = "voxtral-mini-latest";

type Kind = "mistral" | "openai" | "gemini" | "elevenlabs";
type Engine = { id: string; kind: Kind };

// Every engine a request can end up on, with the company that serves it. A model name a CLIENT
// sends is only ever used if it is a key here, so nothing a client sends reaches a URL or a request
// field unchecked. (A server secret may also name "provider:model", see orderFor.)
const CATALOG: Record<string, Kind> = {
  "scribe_v2": "elevenlabs",
  "voxtral-mini-latest": "mistral",
  "gpt-transcribe": "openai",
  "gpt-4o-transcribe": "openai",
  "gpt-4o-mini-transcribe": "openai",
  "whisper-1": "openai",
  "gemini-3.8-flash": "gemini",
  "gemini-3.1-pro-preview": "gemini",
  "gemini-2.5-flash": "gemini",
  "gemini-2.5-pro": "gemini",
};
const FAST_ORDER = ["scribe_v2", "voxtral-mini-latest", "gpt-transcribe", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"];
const BEST_ORDER = ["scribe_v2", "gpt-transcribe", "gpt-4o-transcribe", "voxtral-mini-latest", "gpt-4o-mini-transcribe"];
const KINDS = new Set(["mistral", "openai", "gemini", "elevenlabs"]);
const MODEL_ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;

function inCatalog(id: string): boolean {
  return Object.prototype.hasOwnProperty.call(CATALOG, id);
}

// A table lookup that never reaches Object.prototype ("constructor", "toString", ...).
function own(table: Record<string, string>, key: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

function engineKey(provider: string): string {
  if (provider === "openai") return env("OPENAI_TOKEN") || env("OPENAI_API_KEY") || env("OPENAI_KEY");
  if (provider === "mistral") return env("MISTRAL_STT_KEY");
  if (provider === "elevenlabs") {
    return env("ELEVENLABS_API_KEY") || env("ELEVENLABS_KEY") || env("ELEVEN_API_KEY") || env("XI_API_KEY");
  }
  return env("GOOGLE_STT_KEY") || env("GEMINI_KEY");
}

// A profile's order: the secret (comma-separated; duplicates dropped) when it names at least one
// engine, else the default. An entry is a catalog id or "provider:model" (the secret is ours, so it
// may name a model the catalog does not know yet - the name is still checked to be plain
// characters). Anything else is dropped, with one log line (the secret's name and the entry).
function orderFor(secret: string, fallback: string[]): Engine[] {
  const seen: Engine[] = [];
  for (const raw of env(secret).split(",")) {
    const entry = raw.trim();
    if (!entry) continue;
    let e: Engine | null = null;
    const colon = entry.indexOf(":");
    if (colon > 0) {
      const kind = entry.slice(0, colon).trim();
      const id = entry.slice(colon + 1).trim();
      if (KINDS.has(kind) && MODEL_ID.test(id)) e = { id, kind: kind as Kind };
    } else if (inCatalog(entry)) {
      e = { id: entry, kind: CATALOG[entry] };
    }
    if (!e) {
      console.error(JSON.stringify({ stt_order_dropped: secret, entry: entry.slice(0, 40) }));
      continue;
    }
    const id = e.id;
    if (!seen.some((s) => s.id === id)) seen.push(e);
  }
  return seen.length ? seen : fallback.map((id) => ({ id, kind: CATALOG[id] }));
}

// The engines to try, in order, for what the client asked.
function plan(asked: string, provider: string): Engine[] {
  const best = asked === "best" || asked === "gemini-2.5-pro";
  const pinned = !best && inCatalog(asked) && asked !== "gemini-2.5-flash";
  const head: Engine[] = pinned ? [{ id: asked, kind: CATALOG[asked] }]
    : (!best && provider === "mistral" ? [{ id: MISTRAL_MODEL, kind: "mistral" }] : []);
  const base = best ? orderFor("STT_BEST_ORDER", BEST_ORDER) : orderFor("STT_FAST_ORDER", FAST_ORDER);
  return [...head, ...base].filter((e, i, a) => a.findIndex((x) => x.id === e.id) === i);
}

// How long ONE attempt may take: 2.5 s plus 0.4 s per second of audio (16 kHz mono WAV, 32 000 bytes
// a second), at least 3.5 s, at most 60 s. The engines answer a short clip in 0.3-1.5 s, so a
// stalled one hands over to the next quickly; a long piece gets the time it needs.
function attemptTimeoutMs(audioB64: string): number {
  const fixed = Number(env("STT_TIMEOUT_MS"));
  if (Number.isFinite(fixed) && fixed > 0) return fixed;
  const seconds = (audioB64.length * 0.75) / 32000;
  return Math.min(60000, Math.max(3500, 2500 + seconds * 400));
}

// The deadline of the whole request, all engines together: under what the caller waits (dictation
// 30 s, an Audio to text piece 90 s) and Supabase's own limit (about 150 s). Twice an attempt, at
// least 20 s, at most 80 s. A request that outlives its caller is paid for twice (the app retries).
function totalBudgetMs(attemptMs: number): number {
  const fixed = Number(env("STT_TOTAL_MS"));
  if (Number.isFinite(fixed) && fixed > 0) return fixed;
  return Math.min(80000, Math.max(20000, 2 * attemptMs));
}

// Gemini thinking, as the app's cloud_speech.thinking_config: 2.5 Flash can be switched off (0), 2.5
// Pro cannot (its smallest valid budget is 128), and a 3.x model takes none at all.
function thinkingBudget(model: string): number | null {
  if (model.startsWith("gemini-2.5-pro")) return 128;
  if (model.startsWith("gemini-2.5-flash")) return 0;
  return null;
}

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(obj: unknown, status = 200, extra: Record<string, string> = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, "Content-Type": "application/json", ...extra },
  });
}

// Keys and account ids blanked: a provider's own words can name them (an OpenAI 401 shows the end of
// the key, a 429 the organisation).
function redact(s: string): string {
  let out = s.replace(/sk-[A-Za-z0-9_*.-]{4,}/g, "[key]")
    .replace(/\bsk_[A-Za-z0-9_*.-]{4,}/g, "[key]")          // an ElevenLabs key
    .replace(/key=[^&\s"]+/g, "key=[key]")
    .replace(/\b(org|proj)[-_][A-Za-z0-9_*-]+/g, "$1-***");
  // ... and our own keys, whatever they look like, if a provider's answer repeats one.
  for (const kind of ["openai", "mistral", "gemini", "elevenlabs"]) {
    const k = engineKey(kind);
    if (k.length >= 8) out = out.split(k).join("[key]");
  }
  return out;
}

// What the client may learn about a failed attempt: a fixed code, never the provider's text.
function publicDetail(status: number): string {
  if (status === 429) return "rate_limited";
  if (status === 402) return "provider_credits";
  if (status === 401 || status === 403) return "provider_auth";
  if (status >= 500) return "provider_unavailable";
  return "provider_rejected";
}

// A provider said no: one log line (provider, model, HTTP status and the first words of its answer
// with keys blanked; never the audio) so a failing model shows in the function logs, and the failure
// the caller sees (status 0 = our own "timeout" / "network_error" word, else a fixed code).
function failed(provider: string, model: string, status: number, detail: string) {
  console.error(JSON.stringify({ stt_fail: provider, model, status, detail: redact(detail).slice(0, 120) }));
  return { ok: false, status, detail: status === 0 ? detail : publicDetail(status) };
}

// Voxtral is only used when the language is KNOWN and covered: an explicit language that is not in
// MISTRAL_LANGS (Armenian, ...) would come back as confident nonsense in another script, so it is
// never sent there - the request goes to another engine (or fails if there is none). A language that
// is not known - "auto", or "multi" with no list - could be Armenian too and nothing can catch the
// wrong answer afterwards, so those start elsewhere unless STT_AUTO_VOXTRAL=1 asks for the speed
// for "auto". A mix is covered when every language in it (at least one) is.
function mistralCovers(language: string, languages: string[] | null): boolean {
  if (language === "multi") {
    const mix = languages ?? [];
    return mix.length > 0 && mix.every((c) => MISTRAL_LANGS.has(c));
  }
  if (!language || language === "auto") return env("STT_AUTO_VOXTRAL") === "1";
  return MISTRAL_LANGS.has(language);
}

// The breaker (per function instance, in memory; a new instance simply tries ElevenLabs again). While
// it is paused the engine is skipped: not tried, not an error. 402, 401, 403 or an answer that says
// quota_exceeded / insufficient_credits / credits = no credits or no access: STT_ELEVEN_PAUSE_MS
// (default 15 min). 429 = busy: at most 15 s. A 5xx, a timeout or a network error = unhealthy:
// STT_ELEVEN_OUTAGE_PAUSE_MS (default 30 s). A 200 with no usable text: 60 s. A pause only ever grows.
const ELEVEN_MIN_TIMEOUT_MS = 8000;
const ELEVEN_PAUSE_DEFAULT_MS = 900000;
const ELEVEN_BUSY_PAUSE_MS = 15000;
const ELEVEN_OUTAGE_PAUSE_DEFAULT_MS = 30000;
const ELEVEN_BAD_ANSWER_PAUSE_MS = 60000;
let elevenPause = { until: 0, detail: "provider_unavailable" };

function elevenPaused(): boolean {
  return Date.now() < elevenPause.until;
}

// Start (or lengthen) the pause. A shorter one never replaces a longer one. One log line when the
// engine was not paused, or when the pause changes kind (busy -> credits, outage -> credits, ...);
// a repeat of the same kind only extends it, silently.
function startPause(ms: number, detail: string, status: number): void {
  const now = Date.now();
  if (now + ms <= elevenPause.until) return;               // already paused for at least as long
  const log = now >= elevenPause.until || detail !== elevenPause.detail;
  elevenPause = { until: now + ms, detail };
  if (log) {
    console.error(JSON.stringify({ stt_pause: "elevenlabs", status, minutes: Number((ms / 60000).toFixed(2)) }));
  }
}

// A timeout, a network error (status 0) or a 5xx: skip it briefly, so an outage does not add its wait
// to every request.
function pauseElevenOutage(status: number): void {
  const asked = Number(env("STT_ELEVEN_OUTAGE_PAUSE_MS"));
  startPause(Number.isFinite(asked) && asked > 0 ? asked : ELEVEN_OUTAGE_PAUSE_DEFAULT_MS,
             "provider_unavailable", status);
}

function pauseElevenBadAnswer(status: number): void {
  startPause(ELEVEN_BAD_ANSWER_PAUSE_MS, "provider_unavailable", status);
}

// An answer that came back with a status below 500 and was not a success.
function pauseEleven(status: number, body: string): void {
  if (status >= 500) return pauseElevenOutage(status);
  const credits = /quota_exceeded|insufficient_credits|credits/i.test(body);
  const dead = credits || status === 402 || status === 401 || status === 403;
  if (!dead && status !== 429) return;
  const asked = Number(env("STT_ELEVEN_PAUSE_MS"));
  const full = Number.isFinite(asked) && asked > 0 ? asked : ELEVEN_PAUSE_DEFAULT_MS;
  const ms = dead ? full : Math.min(full, ELEVEN_BUSY_PAUSE_MS);
  startPause(ms, credits || status === 402 ? "provider_credits" : dead ? "provider_auth" : "rate_limited", status);
}

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

const LANG_NAMES: Record<string, string> = {
  hy: "Armenian", ru: "Russian", en: "English", fr: "French",
  de: "German", es: "Spanish", ar: "Arabic",
};
// Languages Mistral Voxtral handles well (ISO-639-1). Others are auto-detected.
const MISTRAL_LANGS = new Set(["en", "es", "fr", "de", "it", "nl", "pt", "hi", "ar", "ru"]);

// Non-Latin scripts; the rest are written in Latin letters.
const SCRIPT: Record<string, string> = { hy: "the Armenian alphabet", ru: "Cyrillic", ar: "Arabic script" };

function joinNames(items: string[]): string {
  return items.length <= 1 ? items.join("") : items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
}

// The language instruction for clients that send `languages` (v1.9.3+) - the
// same wording as speech_langs.cloud_hint in the app. Foreign words are kept
// as spoken, and "multi" (Mixed languages) lists the languages to expect
// (any language when fewer than two are picked), switching anywhere, even
// mid-sentence. Every table lookup goes through own(): only a real entry counts.
function languageHint(language: string, languages: string[]): string {
  const nm = own(LANG_NAMES, language);
  if (nm) {
    const script = own(SCRIPT, language);
    const how = script ? ` in ${script}, never transliterated into Latin letters` : "";
    return ` The speaker speaks mainly ${nm}. Write ${nm}${how}. If they use words or phrases ` +
      `from another language, keep those exactly as spoken, in that language's own script - ` +
      `don't translate them.`;
  }
  if (language === "multi") {
    const mix = languages.filter((c) => own(LANG_NAMES, c));
    if (mix.length < 2) {
      return " The speaker may switch languages, even in the middle of a sentence. " +
        "Write every word in the language it was spoken in, in that language's " +
        "own script. Never translate, and never turn the whole text into one " +
        "language.";
    }
    const latin = mix.filter((c) => !own(SCRIPT, c)).map((c) => own(LANG_NAMES, c));
    const bits = mix.filter((c) => own(SCRIPT, c)).map((c) => `${own(LANG_NAMES, c)} in ${own(SCRIPT, c)}`);
    if (latin.length) bits.push(`${joinNames(latin as string[])} in Latin letters`);
    return ` The speaker switches between ${joinNames(mix.map((c) => own(LANG_NAMES, c) as string))}, often in ` +
      `the middle of a sentence. Write every word in the language it was spoken in, in that ` +
      `language's own script (${bits.join(", ")}). Never translate, and never turn the whole ` +
      `text into one language.`;
  }
  return " If the speaker switches languages, keep every word in the language it was spoken " +
    "in, in its own script - don't translate.";
}

async function transcribeGemini(audioB64: string, language: string, languages: string[] | null,
                                model: string, timeoutMs: number) {
  const nm = own(LANG_NAMES, language);
  // Older clients (no `languages`) keep the exact old prompt.
  const hint = languages !== null
    ? languageHint(language, languages)
    : nm
    ? ` The speaker is speaking ${nm}. Transcribe in ${nm} using its native script and return only ${nm} text.`
    : "";
  // Thinking off where the model allows it: transcription gains nothing from it, and it costs time
  // and tokens. (2.5 Pro gets its smallest valid budget; a 3.x model gets no thinkingConfig.)
  const budget = thinkingBudget(model);
  const payload = {
    contents: [{
      parts: [
        { text: "Transcribe this audio verbatim. Output only the exact spoken words " +
                "with correct punctuation and capitalization, and nothing else." + hint },
        { inline_data: { mime_type: "audio/wav", data: audioB64 } },
      ],
    }],
    generationConfig: { temperature: 0, ...(budget === null ? {} : { thinkingConfig: { thinkingBudget: budget } }) },
  };
  const r = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${engineKey("gemini")}`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs) },
  );
  if (!r.ok) return failed("gemini", model, r.status, (await r.text()).slice(0, 400));
  const d = await r.json();
  const parts = d.candidates?.[0]?.content?.parts ?? [];
  return { ok: true, model, text: parts.map((p: any) => p.text ?? "").join("").trim() };
}

async function transcribeMistral(bytes: Uint8Array, language: string, model: string, timeoutMs: number) {
  const form = new FormData();
  form.append("model", model);
  form.append("file", new Blob([bytes], { type: "audio/wav" }), "audio.wav");
  if (MISTRAL_LANGS.has(language)) form.append("language", language);
  const r = await fetch("https://api.mistral.ai/v1/audio/transcriptions", {
    method: "POST",
    headers: { "Authorization": `Bearer ${engineKey("mistral")}` },
    body: form,
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!r.ok) return failed("mistral", model, r.status, (await r.text()).slice(0, 400));
  const d = await r.json();
  return { ok: true, model, text: (d.text ?? "").trim() };
}

// OpenAI: the language is sent only when the client named ONE explicit ISO code; if OpenAI refuses
// it (a 400 about the language) the same audio is sent once more without it - inside the SAME
// timer, so the retry cannot add a second timeout. No prompt/instructions.
async function transcribeOpenAI(bytes: Uint8Array, language: string, model: string, timeoutMs: number) {
  const explicit = /^[a-z]{2,3}$/.test(language) && language !== "auto" && language !== "multi";
  const signal = AbortSignal.timeout(timeoutMs);
  const once = (withLanguage: boolean) => {
    const form = new FormData();
    form.append("model", model);
    form.append("response_format", "json");
    form.append("file", new Blob([bytes], { type: "audio/wav" }), "audio.wav");
    if (withLanguage) form.append("language", language);
    return fetch(OPENAI_URL, {
      method: "POST",
      headers: { "Authorization": `Bearer ${engineKey("openai")}` },
      body: form,
      signal,
    });
  };
  let r = await once(explicit);
  if (r.status === 400 && explicit) {
    const why = (await r.text()).slice(0, 400);
    const refused = failed("openai", model, r.status, why);
    if (!/language/i.test(why)) return refused;
    r = await once(false);
  }
  if (!r.ok) return failed("openai", model, r.status, (await r.text()).slice(0, 400));
  const d = await r.json();
  return { ok: true, model, text: (d.text ?? "").trim() };
}

// ElevenLabs Scribe. language_code only for ONE explicit ISO code (else it detects the language); if it
// refuses the code (a 422 or 400 that talks about the language) the same audio is sent once more
// without it, inside the SAME timer. No audio-event tags (no "(laughter)" in the text), no speaker
// labels, no word timestamps. A usable answer is JSON whose `text` is a string (a multichannel answer
// has none: that is a failed answer, we never ask for it). Credits gone, a bad key, a busy account, an
// outage or an unusable answer start the breaker (see pauseEleven) so the next requests do not pay for
// a failing call. No redirects are followed (the key travels in a custom header).
async function transcribeElevenLabs(bytes: Uint8Array, language: string, model: string, timeoutMs: number) {
  const explicit = /^[a-z]{2,3}$/.test(language) && language !== "auto" && language !== "multi";
  const signal = AbortSignal.timeout(timeoutMs);
  const once = (withLanguage: boolean) => {
    const form = new FormData();
    form.append("model_id", model);
    form.append("file", new Blob([bytes], { type: "audio/wav" }), "audio.wav");
    form.append("tag_audio_events", "false");
    if (withLanguage) form.append("language_code", language);
    return fetch(ELEVEN_URL, {
      method: "POST",
      headers: { "xi-api-key": engineKey("elevenlabs") },
      body: form,
      signal,
      redirect: "error",     // the key is a custom header: a redirect would carry it to another host
    });
  };
  let r = await once(explicit);
  let why = "";
  if (!r.ok) {
    why = (await r.text()).slice(0, 400);
    if (explicit && (r.status === 422 || r.status === 400) && /language/i.test(why)) {
      failed("elevenlabs", model, r.status, why);       // the refusal is logged; the retry may still work
      r = await once(false);
      why = r.ok ? "" : (await r.text()).slice(0, 400);
    }
  }
  if (!r.ok) {
    pauseEleven(r.status, why);
    return failed("elevenlabs", model, r.status, why);
  }
  let d: any;
  // A 200 that is not a usable answer (it was probably billed): a short pause, or every request would
  // pay for it and then pay the next engine as well.
  try { d = await r.json(); } catch {
    pauseElevenBadAnswer(r.status);
    return failed("elevenlabs", model, r.status, "bad_json");
  }
  if (typeof d?.text !== "string") {
    pauseElevenBadAnswer(r.status);
    return failed("elevenlabs", model, r.status, "no_text");
  }
  return { ok: true, model, text: d.text.trim() };
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ")) return json({ error: "unauthorized" }, 401);

  const supa = createClient(SUPABASE_URL, ANON_KEY, {
    global: { headers: { Authorization: authHeader } },
  });
  const { data: userData, error: userErr } = await supa.auth.getUser();
  if (userErr || !userData?.user) return json({ error: "unauthorized" }, 401);

  // Entitlement: paid subscription, lifetime, or admin. Trials do not grant this.
  const { data: pro, error: proErr } = await supa.rpc("is_pro");
  if (proErr) return json({ error: "entitlement_check_failed" }, 500);
  if (pro !== true) return json({ error: "pro_required" }, 403);

  // The request itself is checked BEFORE the daily quota is spent: one that can never succeed
  // (too big, not audio, no engine for its language) must cost the caller nothing.
  const declared = Number(req.headers.get("content-length") ?? "0");
  if (Number.isFinite(declared) && declared > MAX_AUDIO_B64 + 4096) return json({ error: "audio_too_large" }, 413);
  let body: any;
  try { body = await req.json(); } catch { return json({ error: "bad_request" }, 400); }
  const audio = body?.audio;
  const language: string = typeof body?.language === "string" && body.language ? body.language : "auto";
  const languages: string[] | null = Array.isArray(body?.languages)
    ? body.languages.filter((c: unknown) => typeof c === "string").slice(0, 8)
    : null;
  const provider = typeof body?.provider === "string" ? body.provider : "gemini";
  if (!audio || typeof audio !== "string") return json({ error: "no_audio" }, 400);
  if (audio.length > MAX_AUDIO_B64) return json({ error: "audio_too_large" }, 413);
  // Decoded once for every engine that takes bytes (Gemini takes the base64 text itself).
  let bytes: Uint8Array;
  try { bytes = b64ToBytes(audio); } catch { return json({ error: "bad_request", detail: "invalid_audio" }, 400); }
  // What the client asked for: the body field, else the header (the body wins). Only a catalog id
  // or a profile word means anything; the rest is the FAST profile.
  const asked = (typeof body?.model === "string" ? body.model : (req.headers.get("X-Gusan-Model") ?? "")).trim();

  // The engines to try in order: those with a key (none at all = not configured), then those that
  // can take this language (none left = no engine for it: the 502 stt_failed the app expects, but sent
  // BEFORE the quota is spent, so retries of a request that cannot succeed cost nothing).
  const keyed = plan(asked, provider).filter((e) => engineKey(e.kind) !== "");
  if (keyed.length === 0) {
    return json({ error: "stt_not_configured" }, 503);
  }
  const available = keyed.filter((e) => e.kind !== "mistral" || mistralCovers(language, languages));
  if (available.length === 0) {
    return json({ error: "stt_failed", detail: "no engine for this language" }, 502);
  }
  // ElevenLabs while its breaker is paused is not tried at all. If that leaves nothing, the request
  // cannot succeed now: a 502 with the fixed code of the pause, sent BEFORE the quota is spent.
  const ready = available.filter((e) => e.kind !== "elevenlabs" || !elevenPaused());
  if (ready.length === 0) {
    return json({ error: "stt_failed", detail: elevenPause.detail }, 502);
  }

  // Daily quota.
  const { data: underCap, error: qErr } = await supa.rpc("use_cloud_quota", { max_per_day: DAILY_CAP });
  if (qErr) return json({ error: "quota_check_failed" }, 500);
  if (underCap !== true) return json({ error: "quota_exceeded" }, 429);

  const timeoutMs = attemptTimeoutMs(audio);
  const started = Date.now();
  const deadline = started + totalBudgetMs(timeoutMs);
  const floorMs = Math.min(1500, timeoutMs);        // not worth starting an attempt with less left
  let result: any = { ok: false, detail: "no provider" };
  let tried = 0;
  for (const { id, kind } of ready) {
    if (kind === "elevenlabs" && elevenPaused()) continue;   // paused meanwhile: not tried, not counted
    const left = deadline - Date.now();
    if (tried > 0 && left < floorMs) break;         // the whole request is out of time: stop here
    tried++;
    let t = Math.min(timeoutMs, Math.max(left, floorMs));
    // ElevenLabs' batch endpoint is not tuned for sub-second answers: at least 8 s (within what is
    // left), unless STT_TIMEOUT_MS fixes every attempt.
    if (kind === "elevenlabs" && !(Number(env("STT_TIMEOUT_MS")) > 0)) {
      t = Math.min(Math.max(t, ELEVEN_MIN_TIMEOUT_MS), Math.max(left, floorMs));
    }
    try {
      result = kind === "mistral" ? await transcribeMistral(bytes, language, id, t)
        : kind === "openai" ? await transcribeOpenAI(bytes, language, id, t)
        : kind === "elevenlabs" ? await transcribeElevenLabs(bytes, language, id, t)
        : await transcribeGemini(audio, language, languages, id, t);
    } catch (e) {
      // A timeout or a network error: never the error's own text (a URL may carry a key).
      const name = (e as any)?.name;
      result = failed(kind, id, 0, name === "TimeoutError" || name === "AbortError" ? "timeout" : "network_error");
      if (kind === "elevenlabs") pauseElevenOutage(0);
    }
    if (result.ok) break;
  }
  if (!result.ok) {
    // Nothing was tried (ElevenLabs was paused by a request running at the same time): its pause code.
    return json({ error: "stt_failed", detail: tried === 0 ? elevenPause.detail : result.detail }, 502);
  }
  console.log(JSON.stringify({ stt_ok: result.model, ms: Date.now() - started, tried }));

  const detected = language && language !== "auto" ? language : "en";
  // `model` is the one that really ran (a client shows it; an older proxy says nothing).
  return json({ text: result.text, lang: detected, model: result.model }, 200,
              { "X-Gusan-Model": result.model });
});
