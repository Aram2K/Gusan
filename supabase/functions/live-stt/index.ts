// Real-time speech to text on Gusan Cloud (Gladia live, Solaria-1): words while the person speaks.
//
// The app asks for a session (POST, the person's sign-in); this function checks Pro and the daily cap, opens a
// Gladia live session with the SECRET key and returns its WebSocket URL - which carries a temporary token, so the
// key never reaches the app. The app then streams 16 kHz mono 16-bit PCM straight to Gladia and reads partial and
// final utterances back (live_stt.py). Nothing of the audio passes through here: no time limit for a long meeting.
//
// Body: {language: "fr" | "auto" | "multi", languages: [...] (with "multi"), vocabulary: [...] (Settings >
// Vocabulary), purpose: "dictation" | "meeting"}. Settings follow Gladia's recommended parameters
// (docs.gladia.io/chapters/live-stt/recommended-parameters): dictation closes a sentence after 0.08 s of silence
// (fast, like a voice agent), a meeting after 0.3 s (fewer split sentences); 15 s at most without a pause; partial
// transcripts on; one named language is THE language (no switching); "multi" is its list with code switching;
// "auto" detects (no switching over 100+ languages: Gladia warns it misdetects). The vocabulary is custom_vocabulary
// (intensity 0.5); refused (400/422), the session is opened again without it.
//
// Secrets: Gladia_STT (or GLADIA_API_KEY / GLADIA_KEY / GLADIA_TOKEN / GLADIA_API_TOKEN). Optional GLADIA_REGION
// ("eu-west" / "us-west": the region closest to most users). Deploy with verify_jwt=false (auth.getUser() here).
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const DAILY_CAP = 3000;                    // abuse ceiling (shared with transcribe-proxy's use_cloud_quota)
const MAX_BODY_BYTES = 64 * 1024;
const VOCAB_MAX = 100;
const VOCAB_TERM_MAX = 80;
const LANG_CODE = /^[a-z]{2,3}$/;

function env(name: string): string {
  return (Deno.env.get(name) ?? "").trim();
}
function gladiaKey(): string {
  return env("Gladia_STT") || env("GLADIA_API_KEY") || env("GLADIA_KEY") || env("GLADIA_TOKEN") || env("GLADIA_API_TOKEN");
}

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });
}

export function vocabularyOf(body: any): string[] {
  if (!Array.isArray(body?.vocabulary)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of body.vocabulary) {
    if (typeof v !== "string") continue;
    const term = v.replace(/\s+/g, " ").trim().slice(0, VOCAB_TERM_MAX);
    if (!term || seen.has(term.toLowerCase())) continue;
    seen.add(term.toLowerCase());
    out.push(term);
    if (out.length >= VOCAB_MAX) break;
  }
  return out;
}

export function languageConfig(language: string, languages: string[]): { languages: string[]; code_switching: boolean } {
  if (LANG_CODE.test(language) && language !== "auto") return { languages: [language], code_switching: false };
  const mix = languages.filter((c) => LANG_CODE.test(c)).slice(0, 8);
  if (language === "multi" && mix.length) return { languages: mix, code_switching: mix.length > 1 };
  return { languages: [], code_switching: false };
}

export function sessionBody(purpose: string, language: string, languages: string[], vocabulary: string[]) {
  return {
    encoding: "wav/pcm", sample_rate: 16000, bit_depth: 16, channels: 1, model: "solaria-1",
    endpointing: purpose === "meeting" ? 0.3 : 0.08,
    maximum_duration_without_endpointing: 15,
    language_config: languageConfig(language, languages),
    messages_config: {
      receive_partial_transcripts: true, receive_final_transcripts: true, receive_speech_events: false,
      receive_pre_processing_events: false, receive_realtime_processing_events: false,
      receive_post_processing_events: false, receive_acknowledgments: false, receive_errors: true,
      receive_lifecycle_events: true,
    },
    ...(vocabulary.length
      ? { realtime_processing: { custom_vocabulary: true, custom_vocabulary_config: { vocabulary, default_intensity: 0.5 } } }
      : {}),
  };
}

function publicDetail(status: number): string {
  if (status === 429) return "rate_limited";
  if (status === 402) return "provider_credits";
  if (status === 401 || status === 403) return "provider_auth";
  if (status >= 500) return "provider_unavailable";
  return "provider_rejected";
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  const authHeader = req.headers.get("Authorization") ?? "";
  if (!/^Bearer [\w-]+\.[\w-]+\.[\w-]+$/.test(authHeader.trim())) return json({ error: "unauthorized" }, 401);
  if (Number(req.headers.get("content-length") ?? 0) > MAX_BODY_BYTES) return json({ error: "too_large" }, 413);

  let body: any;
  try {
    const raw = await req.text();
    if (raw.length > MAX_BODY_BYTES) return json({ error: "too_large" }, 413);
    body = JSON.parse(raw || "{}");
  } catch { return json({ error: "bad_request" }, 400); }

  const supa = createClient(SUPABASE_URL, ANON_KEY, { global: { headers: { Authorization: authHeader } } });
  const [userRes, proRes] = await Promise.all([supa.auth.getUser(), supa.rpc("is_pro")]);
  if (userRes.error || !userRes.data?.user) return json({ error: "unauthorized" }, 401);
  if (proRes.error) return json({ error: "entitlement_check_failed" }, 500);
  if (proRes.data !== true) return json({ error: "pro_required" }, 403);

  const key = gladiaKey();
  if (!key) return json({ error: "live_not_configured" }, 503);
  const { data: underCap, error: qErr } = await supa.rpc("use_cloud_quota", { max_per_day: DAILY_CAP });
  if (qErr) return json({ error: "quota_check_failed" }, 500);
  if (underCap !== true) return json({ error: "quota_exceeded" }, 429);

  const language = typeof body?.language === "string" && body.language ? body.language.trim().toLowerCase() : "auto";
  const languages = Array.isArray(body?.languages)
    ? body.languages.filter((c: unknown) => typeof c === "string").map((c: string) => c.trim().toLowerCase())
    : [];
  const purpose = body?.purpose === "meeting" ? "meeting" : "dictation";
  const vocabulary = vocabularyOf(body);
  const region = env("GLADIA_REGION");
  const url = "https://api.gladia.io/v2/live" + (/^(eu|us)-west$/.test(region) ? `?region=${region}` : "");
  const started = Date.now();
  const open = (withVocabulary: boolean) => fetch(url, {
    method: "POST",
    headers: { "x-gladia-key": key, "Content-Type": "application/json" },
    body: JSON.stringify(sessionBody(purpose, language, languages, withVocabulary ? vocabulary : [])),
    signal: AbortSignal.timeout(8000),
    redirect: "error",                    // the key is a custom header: never followed to another host
  });
  let resp: Response;
  try {
    resp = await open(vocabulary.length > 0);
    if (!resp.ok && vocabulary.length > 0 && (resp.status === 400 || resp.status === 422)) {
      console.error(JSON.stringify({ live_vocabulary_refused: resp.status }));
      resp = await open(false);
    }
  } catch (e) {
    const name = (e as any)?.name;
    console.error(JSON.stringify({ live_fail: name === "TimeoutError" ? "timeout" : "network_error" }));
    return json({ error: "live_unavailable", detail: name === "TimeoutError" ? "timeout" : "network_error" }, 502);
  }
  if (!resp.ok) {
    console.error(JSON.stringify({ live_fail: resp.status }));
    return json({ error: "live_unavailable", detail: publicDetail(resp.status) }, 502);
  }
  const d = await resp.json().catch(() => ({}));
  const wss = typeof d?.url === "string" ? d.url : "";
  let host = "";
  try { host = new URL(wss).hostname; } catch { /* not a URL */ }
  if (!wss.startsWith("wss://") || !(host === "gladia.io" || host.endsWith(".gladia.io"))) {
    return json({ error: "live_unavailable", detail: "bad_answer" }, 502);
  }
  console.log(JSON.stringify({ live_ok: Date.now() - started, purpose, language: language.slice(0, 5) }));
  return json({ url: wss, id: typeof d?.id === "string" ? d.id : "" });
});
