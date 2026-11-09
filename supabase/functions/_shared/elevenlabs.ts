// ElevenLabs helpers shared by interpreter-session, voice-clone and delete-account.
//
// The key is the secret transcribe-proxy already reads: ELEVENLABS_API_KEY, else ELEVENLABS_KEY /
// ELEVEN_API_KEY / XI_API_KEY (the first that is not empty), so the Interpreter needs no new key
// name. The founder gives that key the Text to Speech, Speech to Text and Voices (write) permissions.
//
// Every request: header xi-api-key, redirect "error" (the key travels in a custom header, so a
// redirect must never carry it to another host) and a timeout. A failure reaches the app only as a
// fixed code, never ElevenLabs' own text (it can name the account or quote the request):
//   provider_credits (402)      ElevenLabs answered 402, or its answer says quota_exceeded /
//                               insufficient_credits / credits / voice_limit_reached - the plan
//                               ran out (ElevenLabs sends an out-of-credits answer as a 401 too)
//   provider_unavailable (503)  anything else: a bad key (401/403), busy (429), an outage (5xx),
//                               a timeout or a network error
// One log line per failure {eleven_fail, status, detail} with keys blanked, never audio or text.

export const ELEVEN_API = "https://api.elevenlabs.io/v1";

export type ElevenFailure = "provider_credits" | "provider_unavailable";
export type ElevenResult =
  | { ok: true; res: Response }
  | { ok: false; status: number; code: ElevenFailure; text: string };

function env(name: string): string {
  return (Deno.env.get(name) ?? "").trim();
}

export function elevenKey(): string {
  return env("ELEVENLABS_API_KEY") || env("ELEVENLABS_KEY") || env("ELEVEN_API_KEY") || env("XI_API_KEY");
}

export function failureCode(status: number, body: string): ElevenFailure {
  if (status === 402 || /quota_exceeded|insufficient_credits|credits|voice_limit_reached/i.test(body)) {
    return "provider_credits";
  }
  return "provider_unavailable";
}

export function failureStatus(code: ElevenFailure): number {
  return code === "provider_credits" ? 402 : 503;
}

// Keys blanked: an ElevenLabs key (sk_...), and our own key whatever it looks like.
export function redact(s: string): string {
  let out = s.replace(/\bsk_[A-Za-z0-9_*.-]{4,}/g, "[key]").replace(/sk-[A-Za-z0-9_*.-]{4,}/g, "[key]");
  const k = elevenKey();
  if (k.length >= 8) out = out.split(k).join("[key]");
  return out;
}

export function logFail(what: string, status: number, detail: string): void {
  console.error(JSON.stringify({ eleven_fail: what, status, detail: redact(detail).slice(0, 120) }));
}

// One request to ElevenLabs. `quiet` lists statuses the caller expects (a 404 when deleting a voice
// that is already gone): they come back as a failure, without a log line.
export async function elevenCall(what: string, path: string, init: RequestInit, timeoutMs: number,
                                 quiet: number[] = []): Promise<ElevenResult> {
  const key = elevenKey();
  if (!key) {
    logFail(what, 0, "no key (set ELEVENLABS_KEY)");
    return { ok: false, status: 0, code: "provider_unavailable", text: "" };
  }
  try {
    const res = await fetch(`${ELEVEN_API}${path}`, {
      ...init,
      headers: { ...(init.headers as Record<string, string> ?? {}), "xi-api-key": key },
      redirect: "error",
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (res.ok) return { ok: true, res };
    const text = (await res.text()).slice(0, 400);
    if (!quiet.includes(res.status)) logFail(what, res.status, text);
    return { ok: false, status: res.status, code: failureCode(res.status, text), text };
  } catch (e) {
    // Never the error's own text: a URL or a header can be in it.
    const name = (e as { name?: string })?.name;
    logFail(what, 0, name === "TimeoutError" || name === "AbortError" ? "timeout" : "network_error");
    return { ok: false, status: 0, code: "provider_unavailable", text: "" };
  }
}

// A single-use token the app opens one realtime connection with (15 minutes, consumed on use):
// token_type "realtime_scribe" (Scribe v2 Realtime), "ttd_websocket" (the text-to-dialogue socket,
// /v1/text-to-dialogue/stream-input - the one that takes eleven_v4_turbo) or "tts_websocket" (the
// older text-to-speech socket, which refuses v4 Turbo). A token only opens its own kind of socket.
export async function mintToken(tokenType: "realtime_scribe" | "ttd_websocket" | "tts_websocket", timeoutMs: number):
    Promise<{ ok: true; token: string } | { ok: false; code: ElevenFailure }> {
  const what = `token ${tokenType}`;
  const r = await elevenCall(what, `/single-use-token/${tokenType}`, { method: "POST" }, timeoutMs);
  if (!r.ok) return { ok: false, code: r.code };
  let token = "";
  try {
    const d = await r.res.json();
    token = typeof d?.token === "string" ? d.token : "";
  } catch { /* not JSON, or the body timed out: no token */ }
  if (!token) {
    logFail(what, 200, "no token in the answer");
    return { ok: false, code: "provider_unavailable" };
  }
  return { ok: true, token };
}

// Delete a cloned voice. A voice ElevenLabs no longer knows (404, or a 400 that says so) counts as
// deleted: the goal - the voice is gone - is reached.
export async function deleteVoice(voiceId: string, timeoutMs: number):
    Promise<{ ok: true; missing: boolean } | { ok: false; code: ElevenFailure }> {
  const r = await elevenCall("delete voice", `/voices/${encodeURIComponent(voiceId)}`, { method: "DELETE" },
                            timeoutMs, [404]);
  if (r.ok) {
    try { await r.res.body?.cancel(); } catch { /* nothing to read */ }
    return { ok: true, missing: false };
  }
  if (r.status === 404 || (r.status === 400 && /voice_does_not_exist|voice_not_found/i.test(r.text))) {
    return { ok: true, missing: true };
  }
  return { ok: false, code: r.code };
}
