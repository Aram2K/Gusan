# speed-bench: the benchmark's temporary server function

`index.ts` is a Supabase edge function (slug `speed-bench`) that lets the Interpreter benchmark on a PC
call the same providers production uses, with the project's server keys, without a Gusan account:

| action | what it does |
|---|---|
| `ping` | which providers have keys, the models and budgets production uses. Calls nothing. |
| `tts` | ElevenLabs text to speech, 16 kHz PCM (corpus voices, synthesized translations). At most 600 characters. |
| `tokens` | the single-use ElevenLabs tokens `interpreter-session` mints (`realtime_scribe`, `ttd_websocket`), so the bench can open the same live sockets the app opens. |
| `translate` | the Interpreter's phrase translation, **the exact request smart-action's mode `interpret` sends** (prompt, model, parameters, fallback chain and budgets). `route` forces one engine to compare them. |
| `stt` | batch speech to text, **the exact requests transcribe-proxy sends** to Scribe v2, Voxtral, OpenAI and Gladia, plus word timings when the engine gives them. |
| `judge` | an LLM grades translations (adequacy and fluency, 0-100) against reference translations. |
| `usage` | the ElevenLabs characters used this month and the limit (to keep the benchmark inside its budget). |

The full request and answer shapes are at the top of `index.ts`. All answers are JSON with `server_ms` (the
time spent inside the function, counted from the request's arrival) and `read_ms` (the part of it spent
receiving the body: for `stt` that is the audio upload, which the app pays too), except `translate`, which
streams plain text.

## Why it is temporary

It holds no users: anyone with its token can spend the project's ElevenLabs, OpenAI, Mistral, Gladia and
DeepSeek credit. So it is deployed only for a benchmark run and replaced by `stub_410.ts` right after.

- Every request needs the header `x-bench-token`. The check runs first and in constant time; without the
  right token the answer is a bare 401 and nothing else happens (no body read, no log line, no provider call).
- The source holds a placeholder instead of a token. While it is there (or the token is under 24 characters),
  every request is refused, so deploying the file unprepared exposes nothing.
- POST only, a body of at most 25 MB, provider errors come back with keys blanked, and the logs hold one
  line per request (action, status, timings, sizes): never a text, audio, the token or a key.
- Deploy with `verify_jwt = false`: the token is the check.

## Run it

Deploy only with the founder's OK, and stub it again the same day.

1. **Prepare** (writes local files only, never prints the token):

   ```
   python bench/interpreter/server/prepare_deploy.py
   ```

   This makes `bench/interpreter/.bench_token` (a random 43-character token, gitignored; `--new` makes a new
   one) and the deploy copy `server/.deploy/supabase/functions/speed-bench/index.ts` with the token in place
   (gitignored: `server/.gitignore`). The bench client reads the token from `.bench_token` or `GUSAN_BENCH_TOKEN`.

2. **Deploy** the deploy copy as `speed-bench` on project `hftcelxzfoubheqeoool`, `verify_jwt = false`:
   - Supabase MCP tool `deploy_edge_function`: `project_id: "hftcelxzfoubheqeoool"`, `name: "speed-bench"`,
     `entrypoint_path: "index.ts"`, `verify_jwt: false`, `files: [{"name": "index.ts", "content": <the deploy
     copy's text>}]`. It needs no other file (no imports, no deno.json). The token travels inside that
     content, so it shows in the tool call: one more reason to stub the function the same day.
   - or the Supabase CLI: `supabase functions deploy speed-bench --project-ref hftcelxzfoubheqeoool
     --no-verify-jwt --workdir bench/interpreter/server/.deploy`
   - or the dashboard (Edge Functions > Deploy a new function > via Editor): name `speed-bench`, paste the
     deploy copy, then switch "Enforce JWT Verification" off in the function's settings.

   No secret has to be set: the function reads the project's existing ones (the same as smart-action,
   transcribe-proxy and interpreter-session).

3. **Check**: `{"action": "ping"}` with the token must answer `ok: true` and list the providers; without the
   token it must be a 401.

4. **Run** the benchmark (`run_stt.py`, `run_interpreter.py`, see `bench/interpreter/README.md`).

5. **Stub it** right after:

   ```
   python bench/interpreter/server/prepare_deploy.py --stub
   ```

   then deploy the deploy copy (now the 410 stub) exactly as in step 2. Every request answers 410 from then
   on; the founder can delete the function in the Supabase dashboard. `--check` says which one the deploy copy
   is.

## What things cost

- `tts`: ElevenLabs characters (the answer's `chars`; `billed_chars` when ElevenLabs reports it - flash and
  turbo models may bill less than one credit per character). `usage` shows the month's real count.
- `stt`: audio time at the engine's price (Scribe and Gladia bill per hour of audio; Scribe uses ElevenLabs
  credits too - compare `usage` before and after a run).
- `tokens`: nothing to mint; the live sockets opened with them bill as usual.
- `translate`, `judge`: LLM tokens (fractions of a cent per phrase).

## Reading the answers

- `translate` streams the raw `delta.content` pieces the app's `interpreter/translate.py` reads. Time to the
  first chunk is time to the first words. A `<think>` block a model leaks is passed on as is (the app filters
  it with `action_api.ThinkFilter` when `x-answer-family` is `deepseek` or `qwen`); the function log line says
  `think_tags: true` when it happens. Headers:
  - `x-server-ms`: time in the function until the first words (auth, the chain, fallbacks)
  - `x-bench-route` / `x-bench-model` / `x-answer-family`: the engine that answered
  - `x-bench-first-ms`: that engine's own time to its first words
  - `x-bench-budget-ms`: the time production gives that engine before falling back (2500 DeepSeek, 2000
    Mistral, OpenAI whatever is left of the chain's 6000). With a forced `route` the engine gets 20 s, so a
    slow engine is measured, and a first-words time over this budget means production would have moved on.
  - `x-bench-attempts`: the engines that failed first, `[{route, kind, error, ms}]` (how often production falls back)
  - `x-bench-variant`, `x-bench-max-tokens`: the "don't think" variant DeepSeek took, the token ceiling
- `stt` runs the engine it is asked for, even where production would not route (Voxtral for Armenian):
  `covered: false` says so. `production_timeout_ms` is the time production gives one try; the bench gives
  3x that (30-120 s) so slow answers are measured, not cut. `language` is what the engine detected (null if it
  does not say), `language_sent` what was sent to it. Options beyond production: `diarize` (Scribe, Gladia),
  `model` (another model of the same engine), `vocabulary` (Gladia, as the app sends the Dictionary).
- `judge` uses DeepSeek by default (the translation model itself, so it may favour its own wording); pass
  `"route": "openai"` (gpt-5.4-mini) or `"mistral"` for a cross-check. A score that could not be read is
  `null` with the note `unparsed`; an empty translation scores 0 without asking.

## Keeping it true to production

The translation code between the `copied from smart-action` markers is smart-action's own text, and the stt
code mirrors transcribe-proxy. `node bench/interpreter/selftest_server.mjs` (Node 22.13+, offline) checks that:

- every copied declaration is still smart-action's text, and smart-action's own `interpretRequest`, cut out of
  the file and run, writes the same prompts as the bench's;
- transcribe-proxy's speech functions, `interpreter/translate.py` and `interpreter-session` still match what the
  bench assumes (fingerprints; `--print-hashes` prints the current ones);
- the function itself, under a fake network: every request to every provider, the fallbacks and budgets, the
  error shapes, and that no log line holds a text, a key or the token.

When a PARITY check fails, copy the changed declaration from smart-action into `index.ts`. When a DRIFT check
fails, compare the changed transcribe-proxy function with the bench's stt code, update it if the request
changed, then add the new fingerprint to `KNOWN_PROXY` in the self-test.

The bench measures the requests of the checkout it is deployed from. If the smart-action or transcribe-proxy
that is live was deployed from another version (compare the versions in the Supabase dashboard), the numbers
describe this checkout, not what users get today.

Gladia: this branch's transcribe-proxy has no Gladia yet; production (2.1.2, main) does, and the bench's
Gladia code follows main's `transcribeGladia` (Solaria-3 for en/fr/de/es/it, else Solaria-1). The self-test
checks it once the branch has it.
