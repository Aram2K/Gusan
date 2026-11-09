// Gusan Interpreter: the person's own voice (an ElevenLabs instant voice clone).
//
// The other people in a call hear the translation in the speaker's OWN voice. The clone is made
// from clean clips of the person alone (their meetings' own-mic tracks, or ~1 minute read aloud)
// and is stored at ElevenLabs as a voice id tied to the account (table interpreter_voices). It is
// only ever the person's own voice: "This is my own voice" must be ticked and a random sentence
// read aloud (consent + consent_phrase, stored with the voice).
//
// Contract: docs/redesign/interpreter-spec.md 8.1. POST, JSON, Authorization: Bearer <access token>.
//   get      {"action":"get"} -> {"voice": null | {voice_id, source, created_at}}
//   create   {"action":"create","clips":["<base64 wav 16 kHz mono>", ...],"source":"meetings"|"recorded",
//             "consent":true,"consent_phrase":"..."} -> {voice_id, created_at}
//            Pro only. At most MAX_CLIPS clips, MAX_CLIPS_B64 base64 characters (12 MB) and
//            MAX_AUDIO_SECONDS of audio in total. The new voice replaces the old one, and the old one
//            is deleted at ElevenLabs once the new one exists (a failed delete is logged as
//            voice_orphan with its id, for the founder to remove by hand). The row is swapped in one
//            step (swap_interpreter_voice), so two creates at the same time (two PCs) each delete
//            exactly the voice they replaced: no clone is left at ElevenLabs without a row.
//   preview  {"action":"preview","text":"...","language":"ja"} -> {"audio":"<base64 pcm_16000>","sample_rate":16000}
//            Pro only; the text is cut to MAX_PREVIEW_CHARS; spoken with INTERPRETER_TTS_MODEL.
//            A daily abuse ceiling of PREVIEW_DAILY_CHARS characters per user, counted only for a
//            request that is about to be spoken.
//   delete   {"action":"delete"} -> {"deleted":true}   ElevenLabs first, then the row; a voice
//            ElevenLabs no longer knows (404) is deleted here too. No voice = already deleted. A
//            voice another device made meanwhile keeps its row.
// get and delete need no Pro: a person can always see and remove their own voice.
// Errors {"error": code}: auth 401, pro_required 402, bad_request 400 (+ detail), no_voice 404,
// consent_required 400, quota_exceeded 429 (preview's daily ceiling), provider_credits 402,
// provider_unavailable 503; a database failure is a 500.
// create only: voice_too_short 400 (ElevenLabs wants every sample >= 4.6 s), voice_samples_rejected 400
// (any other refusal of the recordings themselves, a 400/422 from ElevenLabs).
//
// Secrets: ELEVENLABS_KEY (see _shared/elevenlabs.ts; it needs the Voices write permission),
//          INTERPRETER_TTS_MODEL (default eleven_v4_turbo).
// Auto-provided: SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY. Deploy with verify_jwt=false,
// after migration 202611090001 (swap_interpreter_voice, use_voice_preview_chars; service role only).
// Logs: counts and durations only - never audio, the consent sentence or a preview text.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { deleteVoice, elevenCall, elevenKey, failureStatus, logFail } from "../_shared/elevenlabs.ts";

const JWT_RE = /^[\w-]+\.[\w-]+\.[\w-]+$/;
const LANG_RE = /^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$/;
const MAX_CLIPS_B64 = 12 * 1024 * 1024;   // all clips together, in base64 characters
const MAX_BODY_BYTES = MAX_CLIPS_B64 + 64 * 1024;
const MAX_AUDIO_SECONDS = 200;            // ~3 minutes of speech, with room for the pauses between clips
const MAX_CLIPS = 25;                     // what an ElevenLabs instant clone takes
const MAX_PHRASE_CHARS = 500;
const MAX_PREVIEW_CHARS = 200;
// A cost-abuse ceiling, not a product quota: preview characters per user per UTC day (what ElevenLabs
// bills). A "Hear yourself in" sample or a Try it sentence is 20-60 characters.
const PREVIEW_DAILY_CHARS = 20_000;
const ADD_TIMEOUT_MS = 90_000;            // the clone (with noise removal) takes seconds, not minutes
const DELETE_TIMEOUT_MS = 10_000;
const TTS_TIMEOUT_MS = 20_000;            // a cloned voice's first words take ~3 s
const DEFAULT_TTS_MODEL = "eleven_v4_turbo";
const SAMPLE_RATE = 16_000;

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

function env(name: string): string {
  return (Deno.env.get(name) ?? "").trim();
}

function ttsModel(): string {
  const v = env("INTERPRETER_TTS_MODEL");
  return /^[a-z0-9][a-z0-9._-]{1,60}$/i.test(v) ? v : DEFAULT_TTS_MODEL;
}

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// In slices: one String.fromCharCode(...all) would overflow the stack on a few seconds of audio.
function bytesToB64(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

// The length of a WAV clip in seconds, from its header's byte rate (null: not a WAV file). The app
// writes plain 16 kHz mono PCM WAV, whose "fmt " chunk comes first.
export function wavSeconds(b: Uint8Array): number | null {
  if (b.length < 44) return null;
  const tag = (o: number) => String.fromCharCode(b[o], b[o + 1], b[o + 2], b[o + 3]);
  if (tag(0) !== "RIFF" || tag(8) !== "WAVE") return null;
  const byteRate = new DataView(b.buffer, b.byteOffset, b.byteLength).getUint32(28, true);
  if (byteRate < 8_000 || byteRate > 768_000) return null;
  return (b.length - 44) / byteRate;
}

// deno-lint-ignore no-explicit-any
type Db = any;

async function getVoice(svc: Db, userId: string) {
  return await svc.from("interpreter_voices").select("voice_id, source, created_at").eq("user_id", userId)
    .maybeSingle();
}

async function create(svc: Db, userId: string, body: Record<string, unknown>): Promise<Response> {
  const phrase = typeof body.consent_phrase === "string" ? body.consent_phrase.trim() : "";
  if (body.consent !== true || !phrase) return json({ error: "consent_required" }, 400);
  const source = body.source;
  if (source !== "meetings" && source !== "recorded") return bad("source");
  const clips = body.clips;
  if (!Array.isArray(clips) || clips.length === 0) return bad("clips");
  if (clips.length > MAX_CLIPS) return bad("too_many_clips");
  if (!clips.every((c) => typeof c === "string" && c.length > 0)) return bad("clips");
  const texts = (clips as string[]).map((c) => c.replace(/^data:audio\/[a-z0-9.+-]+;base64,/i, ""));
  if (texts.reduce((n, c) => n + c.length, 0) > MAX_CLIPS_B64) return bad("too_large");
  const files: Uint8Array[] = [];
  let seconds = 0;
  for (const c of texts) {
    let bytes: Uint8Array;
    try { bytes = b64ToBytes(c); } catch { return bad("invalid_audio"); }
    const s = wavSeconds(bytes);
    if (s === null) return bad("invalid_audio");
    seconds += s;
    files.push(bytes);
  }
  if (seconds > MAX_AUDIO_SECONDS) return bad("too_long");
  if (!elevenKey()) {
    console.error("voice-clone: no ElevenLabs key (set ELEVENLABS_KEY)");
    return json({ error: "provider_unavailable" }, 503);
  }

  const form = new FormData();
  form.append("name", `Gusan ${userId.slice(0, 8)}`);
  form.append("description", "Gusan Interpreter voice");
  form.append("remove_background_noise", "true");
  files.forEach((bytes, i) => form.append("files", new Blob([bytes], { type: "audio/wav" }), `clip${i + 1}.wav`));
  const startedAt = Date.now();
  const r = await elevenCall("add voice", "/voices/add", { method: "POST", body: form }, ADD_TIMEOUT_MS);
  if (!r.ok) {
    // ElevenLabs refused the recordings themselves (too short, not usable): say that, not "unavailable".
    if (r.code !== "provider_credits" && (r.status === 400 || r.status === 422)) {
      return json({ error: /audio_too_short|too short/i.test(r.text) ? "voice_too_short" : "voice_samples_rejected" },
                  400);
    }
    return json({ error: r.code }, failureStatus(r.code));
  }
  let voiceId = "";
  try {
    const d = await r.res.json();
    voiceId = typeof d?.voice_id === "string" ? d.voice_id : "";
  } catch { /* no usable answer */ }
  if (!voiceId) {
    logFail("add voice", 200, "no voice_id in the answer");
    return json({ error: "provider_unavailable" }, 503);
  }

  const createdAt = new Date(Date.now()).toISOString();
  // Saved, and the voice it replaced read back, in one step: a create that ran at the same time
  // gets the voice this one replaced (or this one), never the same old voice twice.
  const up = await svc.rpc("swap_interpreter_voice", {
    p_user_id: userId,
    p_voice_id: voiceId,
    p_source: source,
    p_consent_phrase: phrase.slice(0, MAX_PHRASE_CHARS),
    p_created_at: createdAt,
  });
  if (up.error) {
    // Not kept here = not kept at ElevenLabs either: a voice nobody can delete must not stay.
    console.error("voice row not saved", String(up.error.message ?? "").slice(0, 120));
    const gone = await deleteVoice(voiceId, DELETE_TIMEOUT_MS);
    if (!gone.ok) console.error(JSON.stringify({ voice_orphan: voiceId }));
    return json({ error: "server_error" }, 500);
  }
  const oldId = typeof up.data === "string" ? up.data : "";
  if (oldId && oldId !== voiceId) {
    const gone = await deleteVoice(oldId, DELETE_TIMEOUT_MS);
    if (!gone.ok) console.error(JSON.stringify({ voice_orphan: oldId }));
  }
  console.log(JSON.stringify({ voice_created: source, clips: files.length, seconds: Math.round(seconds),
                               replaced: !!oldId, ms: Date.now() - startedAt }));
  return json({ voice_id: voiceId, created_at: createdAt });
}

async function preview(svc: Db, userId: string, body: Record<string, unknown>): Promise<Response> {
  const text = typeof body.text === "string" ? body.text.trim().slice(0, MAX_PREVIEW_CHARS) : "";
  if (!text) return bad("text");
  const language = body.language === undefined || body.language === null || body.language === ""
    ? "" : body.language;
  if (typeof language !== "string" || (language && !LANG_RE.test(language))) return bad("language");
  const v = await svc.from("interpreter_voices").select("voice_id").eq("user_id", userId).maybeSingle();
  if (v.error) return json({ error: "server_error" }, 500);
  if (!v.data?.voice_id) return json({ error: "no_voice" }, 404);
  if (!elevenKey()) return json({ error: "provider_unavailable" }, 503);
  // Counted only now, so a request that can't be spoken costs nothing.
  const q = await svc.rpc("use_voice_preview_chars",
                          { p_user_id: userId, p_chars: text.length, p_max_per_day: PREVIEW_DAILY_CHARS });
  if (q.error) return json({ error: "server_error" }, 500);
  if (q.data !== true) return json({ error: "quota_exceeded" }, 429);
  const path = `/text-to-speech/${encodeURIComponent(v.data.voice_id)}?output_format=pcm_${SAMPLE_RATE}`;
  const once = (withLanguage: boolean) => elevenCall("preview", path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, model_id: ttsModel(), ...(withLanguage ? { language_code: language } : {}) }),
  }, TTS_TIMEOUT_MS);
  let r = await once(!!language);
  // A model that does not take this language code: the same text once more, the model picks.
  if (!r.ok && language && (r.status === 400 || r.status === 422) && /language/i.test(r.text)) r = await once(false);
  if (!r.ok) return json({ error: r.code }, failureStatus(r.code));
  let audio: Uint8Array;
  try { audio = new Uint8Array(await r.res.arrayBuffer()); } catch {
    logFail("preview", 200, "audio cut off");
    return json({ error: "provider_unavailable" }, 503);
  }
  return json({ audio: bytesToB64(audio), sample_rate: SAMPLE_RATE });
}

async function remove(svc: Db, userId: string): Promise<Response> {
  const v = await svc.from("interpreter_voices").select("voice_id").eq("user_id", userId).maybeSingle();
  if (v.error) return json({ error: "server_error" }, 500);
  if (!v.data) return json({ deleted: true });
  // ElevenLabs first: the row is the only record of the voice, so it stays until the voice is gone.
  const voiceId = String(v.data.voice_id ?? "");
  const gone = await deleteVoice(voiceId, DELETE_TIMEOUT_MS);
  if (!gone.ok) return json({ error: gone.code }, failureStatus(gone.code));
  // Only the row of the voice just deleted: a voice another device made meanwhile stays recorded.
  const del = await svc.from("interpreter_voices").delete().eq("user_id", userId).eq("voice_id", voiceId);
  if (del.error) return json({ error: "server_error" }, 500);
  console.log(JSON.stringify({ voice_deleted: true, already_gone: gone.missing }));
  return json({ deleted: true });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  const authHeader = req.headers.get("Authorization") ?? "";
  if (!authHeader.startsWith("Bearer ") || !JWT_RE.test(authHeader.slice(7).trim())) {
    return json({ error: "auth" }, 401);
  }
  // A declared-large body is not even read; one that declares nothing is checked once read.
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
  if (action !== "get" && action !== "create" && action !== "preview" && action !== "delete") return bad("action");

  const supa = createClient(env("SUPABASE_URL"), env("SUPABASE_ANON_KEY"),
                            { global: { headers: { Authorization: authHeader } } });
  // Making or using a voice costs ElevenLabs credits: Pro. Seeing or removing one never needs Pro.
  const needsPro = action === "create" || action === "preview";
  const [userRes, proRes] = await Promise.all([
    supa.auth.getUser(),
    needsPro ? supa.rpc("is_pro") : Promise.resolve({ data: true, error: null }),
  ]);
  if (userRes.error || !userRes.data?.user) return json({ error: "auth" }, 401);
  if (proRes.error) return json({ error: "entitlement_check_failed" }, 500);
  if (proRes.data !== true) return json({ error: "pro_required" }, 402);

  const serviceKey = env("SUPABASE_SERVICE_ROLE_KEY");
  if (!serviceKey) {
    console.error("voice-clone: SUPABASE_SERVICE_ROLE_KEY missing");
    return json({ error: "server_error" }, 500);
  }
  const svc = createClient(env("SUPABASE_URL"), serviceKey, { auth: { persistSession: false } });
  const userId = String(userRes.data.user.id);
  if (action === "get") {
    const v = await getVoice(svc, userId);
    if (v.error) return json({ error: "server_error" }, 500);
    return json({ voice: v.data ? { voice_id: v.data.voice_id, source: v.data.source, created_at: v.data.created_at }
                                : null });
  }
  if (action === "create") return create(svc, userId, body);
  if (action === "preview") return preview(svc, userId, body);
  return remove(svc, userId);
});
