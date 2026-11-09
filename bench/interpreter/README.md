# Gusan Interpreter and meeting-transcription bench

A reusable setup to test the Interpreter (live speech-to-speech translation in calls) and meeting
transcription on **simulated calls**: multi-speaker, multilingual conversations (with mixed-language
lines, overlaps, numbers, names) voiced with ElevenLabs, laid on exact timelines, then played through the
real engines. It measures

- **speed**: every stage of the interpreter (speech end -> committed text -> first translated words ->
  whole translation -> first voice audio -> heard: *mouth to ear*) and batch STT time / real-time factor;
- **accuracy**: STT word / character error rate per track, per language and on mixed-language lines;
  translation chrF++ and an LLM judge; whether the voice matched the speaker's gender; echo loops in
  speakers mode; dropped / merged phrases;

and compares configurations (engines, language modes, translation routes, headphones vs speakers) so the
numbers can back a recommendation. Everything also runs offline with fakes (`--dry-run`).

## Files

| file | what |
|---|---|
| `corpus/conversations/*.json` | one conversation per file (script, speakers, voices, reference translations, gaps) |
| `corpus/voices.json` | ElevenLabs premade voices the corpus uses |
| `generate.py` | voices the corpus into `corpus/audio/<id>/` (tracks + `timeline.json`) |
| `run_stt.py` | meeting transcription benchmark (Scribe v2, Voxtral, OpenAI, Gladia) |
| `run_interpreter.py` | runs the real interpreter engine on the conversations (simulated devices: `sim_audio.py`) |
| `report.py` | `results/report_<stamp>.md`: tables, a diff with the previous run, plain suggestions |
| `bench_client.py` | the bench function's client, `FakeBench`, the character budget, ledger and TTS cache |
| `metrics.py` | WER / CER / chrF++ / transcript-vs-timeline scoring / summary stats |
| `benchaudio.py` | WAV, levels, café noise, the speakers-mode room leak, the fake voice |
| `server/` | the temporary Supabase function `speed-bench` (source, deploy helper, 410 stub): see `server/README.md` |
| `selftest_offline.py`, `selftest_interpreter.py`, `selftest_corpus.py`, `selftest_server.mjs` | offline self-tests |
| `results/` | run outputs `<kind>_<YYYYmmdd-HHMMSS>.json` (local) and the reports (kept) |

## What it costs (read before a real run)

**ElevenLabs characters: about 20,000 this month for everything** - the corpus voices *and* the
translations the interpreter speaks during runs.

- Every TTS call is counted against `--max-chars` (`CharBudget`: a call that would go over is refused
  before anything is spent) and written to a monthly ledger, `results/chars_ledger.json`. Scripts also
  stop before the month's ledger passes `--monthly-cap` (default 20000, env `GUSAN_BENCH_MONTHLY_CHARS`).
  A try that failed after the text may already have been voiced (a timeout, a dropped connection, a crash)
  stays counted, its retry is paid again (and refused when the budget has no room), and the ledger lists those
  characters as `... unsure`. Errors ElevenLabs answered (a refusal, a rate limit, no key) are refunded.
- `python bench_client.py ledger` shows this month's count; `python bench_client.py usage` asks ElevenLabs
  for the account's own count (only works if the key may read the subscription).
- **The corpus**: `python generate.py --estimate` prints the exact cost (today about 6,600 characters for
  the 10 conversations, once). Clips are cached in `corpus/cache/` by sha1(model|voice|lang|text): a clip is
  never paid for twice, and rebuilding all the tracks from the cache costs nothing. **Do not delete
  `corpus/cache/`** (back it up instead: it is the paid part, not in git).
- **Interpreter runs** speak every translated phrase, roughly the corpus size again per full run with both
  directions. Use `--captions-only` (no voice, 0 characters), `--tts-sample 0.25` (speak a quarter of the
  phrases) and `--max-chars`. They also stream both directions into ElevenLabs' live Scribe (Realtime),
  billed by audio time: about 20 minutes for the whole corpus (`stt_audio_s` in the results), whatever
  `--max-chars` says.
- **STT** bills audio time at each engine's price. A full `run_stt.py` pass (10 conversations, clean audio,
  every input and mode) sends about 3.3 hours of audio: ~45 min per engine, ~1 h for Gladia (its *multi*
  mode); `noisy` and `speakers` add about as much again. The script prints the minutes per engine first. Scribe is ElevenLabs: it may draw on the same monthly credits, so
  `run_stt.py` reads the account's count before and after a real run and prints (and reports) the
  difference. Start with a small run (`--only c01 --engines scribe_v2`) and look at that number first.
- Translation and the judge cost LLM tokens: fractions of a cent per phrase.

## Run it

Deploys only with the founder's OK, and the function is stubbed again the same day. Commands are run from
`bench/interpreter/` with the app's venv Python.

0. **Offline check** (no network, no characters):

   ```
   python selftest_corpus.py
   python selftest_offline.py
   python selftest_interpreter.py
   node selftest_server.mjs
   python generate.py --dry-run
   python run_stt.py --dry-run
   python run_interpreter.py --dry-run --speed 4
   python report.py
   ```

   `selftest_interpreter.py` runs its dry runs side by side at clock x4; on a busy machine a simulation
   can fall behind real time, and a run whose checks then fail is run again alone at a slower clock (it
   says so). `node selftest_server.mjs` needs Node 22.13+.

1. **Deploy the temporary function** (outside these scripts; details in `server/README.md`):

   ```
   python server/prepare_deploy.py
   ```

   writes a random token to `.bench_token` (gitignored; or set env `GUSAN_BENCH_TOKEN`) and the deploy
   copy `server/.deploy/supabase/functions/speed-bench/index.ts` (gitignored) with the token in place of
   `"__TOKEN__"`. Deploy that one file as `speed-bench` on project `hftcelxzfoubheqeoool` with
   `verify_jwt = false` (no other file: it imports nothing), then check it:

   ```
   python bench_client.py ping
   ```

   It must answer `ok: true`, and the providers the run needs `true` (the function reads the project's
   existing secrets, the ones smart-action, transcribe-proxy and interpreter-session use: `false` = no such
   key in the project). A request without the token must be a 401.

2. **Voice the corpus** (default `eleven_v3`, which falls back to `eleven_v4_turbo` for a language v3
   refuses; `--max-chars 8000`):

   ```
   python generate.py --estimate
   python generate.py
   python generate.py --only c03,c08          # some conversations
   ```

   If it notes that a model "did not take language 'ka'", that language was voiced without its language
   code: listen to those turns (`corpus/audio/<id>/system.wav`, times in `timeline.json`) and, if they sound
   wrong, regenerate with `--model-for ka=eleven_v4_turbo` (only the new clips cost characters). A quick
   check of the voices before the full bench: `python run_stt.py --engines scribe_v2 --modes fixed` - one
   language far worse than the rest usually means a bad voice, not a bad engine.

3. **Meeting transcription**:

   ```
   python run_stt.py                                          # clean audio, all engines, inputs, modes
   python run_stt.py --variants clean,noisy,speakers --only c01,c08
   python run_stt.py --engines scribe_v2,gladia --inputs mix
   ```

   Inputs: `system` (the call), `mic` (me), `mix`. Modes: `fixed` (the track's language is sent),
   `auto` (detected), `multi` (Gladia only: the track's languages with code switching). The
   conversation's glossary goes along as custom vocabulary (`--no-vocabulary` to drop it).

4. **Interpreter** (see `python run_interpreter.py --help`):

   ```
   python run_interpreter.py --captions-only                  # speed + accuracy, no characters
   python run_interpreter.py --only c01 --variant speakers --tts-sample 0.25 --max-chars 800
   python run_interpreter.py --route deepseek --judge
   ```

   Every phrase is translated by the app's own client (`interpreter/translate.translate_phrase`: the
   request it builds, its timeouts, its think filter and cleaning), sent to the function's `translate`
   instead of smart-action: ONE try per phrase, like the app, so a failed phrase shows in
   `translate_errors`, never as a hidden retry. `--judge-route openai` (or `mistral`) cross-checks the
   judge, whose default is DeepSeek, the translator itself.

   A real run pings the function first and does not start (nothing spent) when it is stubbed or lacks a
   key the run needs; a refusal mid-run (401 / 404 / 410) skips the remaining conversations. Real runs
   default to `--max-chars 1500`. Offline variants: `--dry-run` (= `oracle`: fake ElevenLabs clients),
   `--dry-run sockets` (the real `eleven.py` clients on in-process fake sockets), `--dry-run sockets
   --fault stt_drop@12,voice_drop@20` (sockets that hang up at those seconds: do the reconnects work, what
   is lost), and `--dry-run --url http://127.0.0.1:PORT/...` (tokens, translations and the judge over HTTP
   through the real `BenchClient`, against a local stand-in; voices and live STT stay fake).

5. **Report**:

   ```
   python report.py
   ```

   writes `results/report_<stamp>.md` from the newest results of each kind, with what changed since the
   previous results of the same kind (`--baseline-stt FILE` / `--baseline-interpreter FILE` to pick one).
   Real runs win over newer dry runs (`--dry` reports the dry ones; `--stt FILE` / `--interpreter FILE`
   pick exact files).

6. **Stub the function** right after the run:

   ```
   python server/prepare_deploy.py --stub
   ```

   and deploy the deploy copy (now `server/stub_410.ts`) as `speed-bench` the same way. Every request then
   answers 410 and the scripts say "the bench function is stubbed".

## The simulated calls

Each conversation (`corpus/conversations/<id>.json`) names `me` (the person using Gusan: their voice is the
**mic** track; everyone else is the **system** track, the call), the interpreter pair (`my_lang`,
`their_lang`), the speakers (name, gender, ElevenLabs voice: not Sarah or Eric, the app's own Hear voices),
a glossary and the turns: text, its languages (more than one = a mixed-language line), reference
translations and the gap before it (negative = a small overlap, at least -0.6 s).

`generate.py` makes, per conversation, in `corpus/audio/<id>/`:

| variant | files | what |
|---|---|---|
| clean | `mic.wav`, `system.wav`, `mix.wav` | each speaker levelled to the same loudness, silences trimmed |
| noisy | `mic_noisy.wav`, `system_noisy.wav`, `mix_noisy.wav` | café noise at 15 dB SNR: 6-talker babble made from the conversation's own turns, pink + brown noise, clinks (seeded) |
| speakers | `mic_speakers.wav`, `mix_speakers.wav` | laptop speakers: the call leaks into the mic at 0.3x after 35 ms with a short room reverb |

Every conversation gets every variant (they are made locally from the voiced clips: no characters).
The corpus tags `speakers` (c01) and `noisy` (c06) mark the conversations written for those variants, so
start there: `run_stt.py --variants speakers --only c01`, `run_interpreter.py --variant speakers --only c01`.

`timeline.json` holds every turn's exact start / end, track, languages, text and references, the
variant files, and `leak` (the room settings + `leak_ir.npy`): in speakers mode the interpreter's own
played voice must leak into the mic the same way (`benchaudio.apply_leak`), or echo loops can't show up.
16 kHz mono 16-bit throughout.

## Reading the numbers

- **WER** counts words; Japanese / Chinese / Thai characters count one each, so mixed lines like
  "demoのリンク" are fair. **CER** counts characters with spaces removed; it is the main number for
  ja / zh / th. Text is normalised first: case, punctuation (incl. Armenian ։ ՞ ՛ ՜ and CJK / Arabic
  marks), quotes, dashes, Arabic vowel marks; numbers are kept (1,000 = 1000, Arabic-Indic ١٥٠ = 150); a
  word is split where its script changes, so "Gusanի", "Gusan-ի" and "Gusan ի" all count the same. v3 audio
  tags like `[laughs]` are not part of the reference. Addresses count the same written or read out
  ("r.moss@northpeak.io" = "r dot moss at northpeak dot io"; also собака / точка, arobase / point,
  arroba / punto, 艾特 / 点 inside an address), and a word spelled out is one word ("M-O-S-S" = "MOSS").
  Not normalised: numbers said in words. The Armenian and Georgian lines write their numbers as words (a
  voice may read digits there in English), so an engine that writes digits loses those words in hy / ka;
  every engine pays it alike, but read hy / ka WER with that in mind.
- **Best engine per language**: the lowest WER (CER for ja / zh / th) per language on clean audio, any mode.
  *fixed* means the track's language was sent; when an engine does not take a language (Voxtral: hy, ka, ja,
  zh, ...) nothing is sent and the report says so: those *fixed* runs are really *auto*.
- **Per turn**: words go to turns by their time stamps when the engine gives them, else by the alignment.
  **WER by turn** scores each turn on its own, so cross-talk an engine writes in another order is not
  counted twice. **Dropped turns**: under 20 % of the turn's words came back. **Bag F1**: word overlap
  ignoring order. **Echo words**: words of the call found in the mic transcript (speakers mode).
- **Speed (STT)**: wall time of one batch request from this PC (upload included; when a 429 / 5xx was
  retried, the time of the try that answered); RTF = the engine's seconds per second of audio.
- **Interpreter stages** (seconds, from the end of the phrase's speech): `stt_commit`, `translate_first`,
  `translate_total`, `mouth_to_ear`; `tts_first_audio` is the voice's own time to its first audio. The spec
  expects about 1.2-1.8 s mouth to ear. In `interpreter_*.json` they are `latency.stt`, `first_delta`,
  `final`, `ear` and `stages.ttfb` per direction (`hear`: the call -> you, `speak`: you -> the call).
  The bench function skips what smart-action does before it asks a model (the sign-in, Pro and quota
  checks, one parallel round of calls), so in the app every translation starts that much later; and it
  measures the smart-action / transcribe-proxy code of the checkout it was deployed from, which may not be
  what is live (compare the versions in the Supabase dashboard).
- **chrF++** (0-100, character 6-grams + word 2-grams, beta 2; character-only for ja / zh / th) against the
  reference translation; the **judge** scores adequacy and fluency 0-100 (an answer the function could not
  read comes back `null` and is left out of the means; the report counts them).
- **Interpreter behaviour** (per direction in `interpreter_*.json`, tables in the report): `stt_input.muted_ratio`
  = the share of the track's own speech that reached its live STT as silence (the listen gate muted it:
  those words can never be translated); `stt_input.gusan_reached_stt_s` = seconds of Gusan's own voice
  that got into that STT; `counts.echo / echo_mixed / echo_suspect` (summed as `echo_loops`, split in
  `echo_loops_by_kind`) = phrases that were Gusan's voice, carried some of it with my words, or came while
  only Gusan played; `crosstalk` = the call heard by the mic (speakers); `reconnects` = fresh single-use
  tokens after the start (STT / voice); `speech_end_by` = how each phrase's speech end was found (turn end,
  pause, gate; phrases with only an estimate are left out of the delays); `stt_audio_s` = audio fed to the
  live STT (Scribe bills it); `chars_charged` = characters the budget let the run speak (the ledger);
  `skipped` = conversations not run after the function refused.
- Results of a dry run are marked **DRY RUN** in the report: those numbers come from fakes and only prove
  the bench works. Dry runs write to `corpus/audio_dry/` and `corpus/cache_dry/`; real runs refuse fake audio.

## Shared contract (for anyone extending the bench)

- Bench function: `POST https://hftcelxzfoubheqeoool.supabase.co/functions/v1/speed-bench`, header
  `x-bench-token`, JSON `{"action": "ping" | "tts" | "tokens" | "translate" | "stt" | "judge" | "usage", ...}`
  (shapes at the top of `server/index.ts`). `BenchClient` retries 429 / 5xx (3 times, with backoff),
  except where a call says `retries=0`: the interpreter's translations (the app makes one try).
- `timeline.json`: `{id, sample_rate, duration, turns: [{i, speaker, track: mic|system, lang, langs, text,
  ref, gender, start, end}], variants: {clean|noisy|speakers: {mic, system, mix}}, chars, ...}`.
- Results: `results/<kind>_<stamp>.json` = `{kind: stt|interpreter, stamp, config, git, conversations,
  summary}`; `report.py` reads them.
- Every TTS caller charges a `CharBudget` first (`BenchClient.tts(..., budget=...)` will not run without one);
  `bench_client.cached_tts` adds the cache. `FakeBench` has the same methods, offline and deterministic.
- The self-tests are scripts named `selftest_*` (never `test_*.py`): pytest never collects them.

## Committing the bench

`bench/` is not part of the app, but `tests/test_release_2_0_0.py` (`TestBundling`) walks every `.py`
outside its `_NOT_APP_DIRS` and fails for any module `main.py` does not import: `"bench"` has to be on
that list before these files are committed, or CI's `pytest tests/` fails. Never commit `.bench_token`,
`server/.deploy/`, `corpus/cache/` (the paid clips: back them up), `corpus/audio*/` or `results/*.json`
(the `.gitignore` files here keep them out); the markdown reports are meant to be kept.
