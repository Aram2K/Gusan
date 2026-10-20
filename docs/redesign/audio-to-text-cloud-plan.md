# Audio to text: cloud models (Gusan Cloud PRO + your own key)

Approved by the founder on 2026-10-13 ("activate so I can test"). Read the FINAL DECISIONS first: they override the plan and the critique below wherever they differ.

## FINAL DECISIONS (binding)

1. `file_model` values: "auto", a Whisper id (hardware.MODEL_INFO), "cloud" (Gusan Cloud, Pro), "own:google", "own:mistral". Constants live in engine_choice (FILE_*). file_jobs keeps them in options (cloud values are NOT turned into None by options_from_cfg); a cloud value reaching the local path without a cloud spec runs as Automatic.
2. Picker: Whisper options kept; "Gusan Cloud · <real model>" always listed, with the "  PRO" suffix for non-Pro users only (smart_actions.py pattern); own-key options only when that key exists, labelled like speech_engine ("Google Gemini · Gemini 2.5 Flash · your own key", "Mistral · Voxtral Mini · your own key"). Privacy mode: cloud/own items disabled + the Smart Actions privacy hint. Free user picking Gusan Cloud: combo snaps back, app._pro_upsell(UPSELL_SPEECH) opens the Pro window, nothing saved.
3. Footer line: local "On this computer. Nothing is uploaded." (unchanged), Gusan Cloud "Sent to Gusan Cloud to transcribe.", own key "Sent to {Google Gemini|Mistral} with your key."
4. New Qt-free modules: `cloud_speech.py` (one request per piece, never falls back, typed CloudSpeechError, Spec dataclass with api_key/token_fn `field(repr=False)`, Gemini key in the x-goog-api-key header, thinkingConfig {thinkingBudget: 0} for gemini-2.5-flash ids, short plain error sentences of our own; provider text only in the log; its own PROXY_URL constant equal to main.MANAGED_PROXY_URL, checked by a test — main.py is NOT edited for this) and `cloud_files.py` (VAD on 5-minute blocks via faster_whisper.vad lazily, energy fallback; ONE streaming ChunkPlanner; split_sentences; spread_segments; transcribe_chunks with a thread pool; transcribe_file).
5. Piece profiles: gusan target 60 s (cut 45–75, min 30, hard_max 90, 4 workers); google and mistral target 120 s (cut 90–150, min 60, hard_max 240, 2 workers). 429 / 5xx / timeout / network: retried up to 4 attempts with backoff 2, 6, 20 s (+-30% jitter, or Retry-After capped at 60 s), waiting on the cancel event; a rate limit drops to 1 worker for the rest of the job. No 1.1 s Mistral gap, no RetryInfo parsing, no quota_day kind.
6. 401 from Gusan Cloud = fatal immediately (no token cache, no refresh path; a plain lock around token_fn). No token while signed in (offline refresh) = "Couldn't reach Gusan Cloud..." sentence.
7. If Gusan Cloud cannot be used AT THE FIRST PIECE (cap 429 quota_exceeded, 403 pro_required, no token), the job continues on THIS COMPUTER with the already-decoded audio (normal local steps 3-4, Automatic model), so models are never mixed; the card shows the Whisper name. A failure later in the file fails the job with one plain sentence (no silent local fill).
8. Before every send, re-check cfg privacy_mode and (own key) that the key still exists; if not, stop the job with one plain sentence.
9. Every cloud piece's text goes through vocabulary.correct_spellings(text, vocabulary.load_terms(cfg)) on this computer (check exact names in vocabulary.py). The proxy never received vocabulary; do not claim it does.
10. Speaker labels run AFTER all pieces (same place as local step 5), so cancel stays simple. Timestamps on cloud are estimates (sentences spread over the piece's speech by character count, snapped to pauses).
11. Model naming: result/transcript store `model` = the real id (gemini-2.5-flash, voxtral-mini-latest, or the user's google_stt_model / mistral_stt_model); display names via engine_choice.MODEL_NAMES. NO new transcript_store fields. Local vs cloud = `model in WHISPER_MODELS`. The job card shows the plain model name (e.g. "Gemini 2.5 Flash"), not the whole label. Telemetry: add `engine` to started/completed (update the test that compares the exact dict). No pieces/retries telemetry. _note_local_wait and the dictation-model restore only for local jobs. Use result.get(...) with defaults so fake runners in tests keep working.
12. Busy messages (BUSY_FOR_DICTATION / BUSY_FOR_MEETING / MSG_RECORDING) stay as they are for local jobs; while a CLOUD job runs use a neutral sentence (e.g. "Gusan is converting a file. Try again when it's done."). Avoid the phrases "1 hour", "up to 5 hours", "too long" anywhere in file_jobs.py (a test greps for them).
13. Not in scope: refactoring dictation's _run_google/_run_mistral/_run_managed, moving constants out of main, any server change (the founder's "thinking off" proxy change is done separately by the main session), Voxtral segment timestamps, partial transcripts, runs_on pro plumbing for audio_to_text, settings_apply/api_keys changes beyond: settings_apply accepts the new file_model values (cloud values only when privacy is off); api_keys resets file_model to "auto" when the key it uses is removed.
14. Copy rules: short, plain, no limit wording, real model names, calm Pro (PRO tag only, no upsell banners). Rename/fix the page test and docstring that say "no upsell".

---

## THE PLAN (from the design pass)

# Plan: cloud models in Audio to text (Gusan 2.0.0, branch prep-2.0.0)

I checked the anchors below against the files on disk. Several line numbers from the investigations were a few lines off, and the ones here are the corrected ones.

## 0. Decisions

Where the investigators disagreed or left a choice open, I picked one:

1. **Stored values.** The existing `file_model` key holds `"auto"`, a Whisper id, `"cloud"`, `"own:google"` or `"own:mistral"`.
   - I used `"own:google"` because it matches cfg `backend="google"`, `google_api_key` and `_run_google`.
   - No new opts key: tests compare exact opts dicts.
2. **PRO tag.** Gusan Cloud's list item gets the plain `"  PRO"` suffix, for Free users only. This is how combo boxes already do it (smart_actions.py:1193-1195, pinned by tests_qt/test_smart_actions_v2.py:114). Pro users see no tag, which keeps Pro calm.
3. **Free user picks Gusan Cloud.** The combo snaps back and `app._pro_upsell("Fast cloud transcription")` opens the existing Pro window. Nothing else happens.
4. **Own-key labels** reuse `speech_engine` (engine_choice.py:305-314):
   - "Google Gemini · Gemini 2.5 Flash · your own key"
   - "Mistral · Voxtral Mini · your own key"
   
   This keeps one builder and the same wording as Dictation and Settings. If the founder wants "your key", one change in `speech_engine` covers everywhere.
5. **Privacy mode.** Cloud and own-key items stay in the list but are disabled, and the row shows "Privacy mode keeps it on this computer." This copies Smart Actions (smart_actions.py:1283-1291). A saved cloud pick reads and runs as Automatic.
6. **Dictation code is not touched.** A new Qt-free `cloud_speech.py` builds and sends the file requests. `AudioRecorder._run_google`, `_run_mistral` and `_run_managed` are not refactored now: dictation speed comes first, and about 30 tests patch `requests.post` on fake recorders.
7. **No server change is needed.** 60 s WAV pieces (2.56 MB as base64) fit today's transcribe-proxy.
8. **Piece sizes.**
   - Gusan Cloud: about 60 s, 4 in parallel.
   - Own keys: about 120 s, 2 in parallel. Personal keys are limited by requests per minute and per day. 120 s rather than 300 s keeps the timing spread small enough for speaker labels.
9. **Timestamps.** One method for every engine:
   - Split each piece's text into sentences.
   - Spread the sentences by character count over the speech parts of that piece (found by VAD), and snap the boundaries to pauses.
   - On a synthetic test this labelled 96% of sentences with the right speaker, against 91% when spreading over clock time.
   - Gemini timestamps are not used (they are unreliable). Voxtral segment timestamps are left for later: the response shape is unverified, and they can't be combined with a set language.
10. **A piece that keeps failing.** It is retried, and if it still fails the job fails with one plain sentence. There is no silent fill with local Whisper: that would mix models under a cloud name and need the shared Whisper lock.
11. **Empty answer for a piece.** Accepted as "no words", logged and counted. If every piece comes back empty, the job shows `MSG_NO_SPEECH`. The proxy can't tell a blocked answer from an empty one today.
12. **A cloud pick that can't run at job start** (Pro ended, Privacy mode on, key removed) runs as Automatic on this computer, and the card shows the real Whisper name. This is the same rule as `smart_speech_session` (engine_choice.py:319-331).
13. **Dictation and meetings stay blocked** while any file job runs, cloud or not. `refusal_message` is unchanged.

## 1. engine_choice.py (Qt-free)

- **New constants** after :95:
  - `FILE_KEY="file_model"`, `FILE_AUTO="auto"`, `FILE_CLOUD="cloud"`
  - `FILE_OWN_GOOGLE="own:google"`, `FILE_OWN_MISTRAL="own:mistral"`
  - `FILE_CLOUD_VALUES` (the three cloud values)
- **New functions** after :381, modelled on the Smart Actions block :318-381:
  - `file_session(cfg, pro) -> (backend, model)`:
    - `("managed", provider)` for cloud when Pro and Privacy mode is off.
    - `("google", google_stt_model or GOOGLE_STT_DEFAULT)` / `("mistral", normalize_mistral_model(...))` when Privacy mode is off and the stripped key is in cfg.
    - `("local", id)` for a `MODEL_INFO` id; anything else is `("local", None)`, which means Automatic.
  - `file_choices(cfg) -> [(value, plain, line, pro)]`:
    - `("auto","Automatic","Automatic",False)`, then every `MODEL_INFO` entry.
    - Always `("cloud","Gusan Cloud",f"Gusan Cloud · {CLOUD_SPEECH_MODELS[provider]}",True)`.
    - `own:google` / `own:mistral` only when that key exists. Their line is `speech_engine(dict(cfg, backend=b, privacy_mode=False), False, "audio_to_text")["label"]`. Forcing `privacy_mode=False` here is needed, otherwise the label falls back to local.
  - `set_file_model(app_or_cfg, value, *, pro=None) -> _outcome(...)`:
    - auto or a `MODEL_INFO` id → ok.
    - cloud: Privacy mode → `"privacy_mode"`; not Pro → `"pro"` with upsell `UPSELL_SPEECH` (:94).
    - own key: Privacy mode → `"privacy_mode"`; no key → `"needs_key"`.
    - anything else → `"unknown"`.
- **`file_engine` (:496-502)** becomes `file_engine(cfg, file_model=None, pro=False)`:
  - `None` reads `cfg[FILE_KEY]`.
  - Cloud and own values go through `file_session`, then `speech_engine(dict(cfg, backend=b), pro, "audio_to_text")`, the same reuse as :342.
  - Otherwise the current local and Auto results.
  - Update the docstring at :497 and the module note at :12.
- **`runs_on`** (:516-517): `return file_engine(cfg, file_model, pro)`.
- tests/test_engine_choice.py:181-186 still passes: with no `file_model`, it stays Auto on this computer.

## 2. Settings writes

- **settings_apply.py:**
  - Add `FILE_MODEL="file_model"` near :53.
  - Add `_file_model_ok(value, private)` next to `_smart_speech_ok` (:73-81). It accepts `"auto"`, `MODEL_INFO` ids, and `FILE_CLOUD_VALUES` only when Privacy mode is off. Pro is checked at job start.
  - Branch on it in `apply()` next to :177-181, and update the docstring table at :26-31.
- **api_keys.py `_speech_fallback`** (:94-100): also return `{"file_model": "auto"}` when the key being removed is the one cfg `file_model` uses. Only then, so tests/test_api_keys.py:76 keeps passing.

## 3. New `cloud_speech.py` (Qt-free, no numpy, requests imported lazily)

It sends one HTTP request per piece, never falls back, and raises typed errors.

- **Constants:**
  - `PROXY_URL` and `MISTRAL_LANGS` move here; main.py:73 and :587-588 import them back, with no behaviour change.
  - `GEMINI_URL`, `MISTRAL_URL`.
  - `PROMPT`, with the same words as main.py:2420-2422.
- **`@dataclass Spec`:** `engine` (gusan | google | mistral), `kind` (cloud | own_key), `model_id`, `label`, `model_name`, `provider`, `api_key`, `token_fn`, `language`, `languages`, `vocab_hint`, `terms`.
  - `spec_from_cfg(cfg, backend, model_id, *, language, token_fn)` reads secrets once at job start.
  - The language hint comes from `speech_langs.cloud_hint` and `mix_languages`. Vocabulary comes from `vocabulary.cloud_transcription_hint` and `cloud_terms`, which are already empty in Privacy mode.
- **`build_request(spec, wav, token)` (pure):**
  - gusan: JSON body `{audio, sample_rate:16000, language, provider, languages, vocabulary?}`, Bearer token, timeout (10, 90).
  - google: key in the `x-goog-api-key` header, never `?key=`. `generationConfig {temperature:0}`, plus `thinkingConfig {thinkingBudget:0}` only for model ids starting `gemini-2.5-flash`. Timeout (10, 120).
  - mistral: multipart `audio.wav` with `model`, and `language` only when it is in `MISTRAL_LANGS`. Timeout (10, 120).
- **`parse_response`:**
  - Gemini: joins the parts' text; no candidates gives `""` and the finishReason is logged.
  - Proxy: uses `text`. Its `lang` is ignored for auto/multi (it just echoes "en"). An optional `model` field is read if the server ever returns one.
  - Mistral: `text` and `language`.
- **`CloudSpeechError(kind, message, retry_after)` and a pure `classify(...)`:**
  - Retried:
    - timeout, network errors and 5xx.
    - Own-key 429, except a per-day quota. Wait the Retry-After or Gemini `RetryInfo.retryDelay`, at most 60 s.
  - Fatal:
    - `signed_out`: no token, or a gusan 401 after one token refresh.
    - `pro_required` (403) and `cap` (proxy 429 `quota_exceeded`).
    - `bad_key` (own 401/403), `no_credit` (402), `quota_day`, `language` (400) and `bad_request`.
  - Own-key sentences come from `api_keys.cloud_error_message` (api_keys.py:146, already Qt-free).
  - Exception text, which can contain URLs, is never shown.
- **`send(spec, wav, *, post)`:** `post` is a per-thread `requests.Session().post`. The token goes through a 60 s cache behind a lock, so 4 workers don't all refresh it at once.
- **Card sentences** (no limit wording):
  - "Sign in to use Gusan Cloud, or pick a model on this computer."
  - "Gusan Cloud is part of Pro. Pick a model on this computer to convert this file."
  - "{name} couldn't finish this file. Try again, or pick a model on this computer." (used for cap, repeated 5xx and bad requests)
  - "Couldn't reach {name}. Check your internet connection and try again."

## 4. New `cloud_files.py` (Qt-free; numpy at top; faster_whisper.vad imported lazily)

- **Profiles:** `Profile(target, cut_lo, cut_hi, min_s, hard_max, workers, min_interval)`.

  | Engine | Target | Cut window | Min piece | Hard max | Workers | Gap between request starts |
  |---|---|---|---|---|---|---|
  | gusan | 60 s | 45–75 s | 30 s | 120 s | 4 | none |
  | google | 120 s | 90–150 s | 60 s | 240 s | 2 | none |
  | mistral | 120 s | 90–150 s | 60 s | 240 s | 2 | 1.1 s |

- **`encode_wav(audio, s, e)`:** clips to [-1, 1], then 16-bit mono WAV. This is the one place to switch to Opus later.
- **`speech_probs(audio, block_s=300, cancel)`:**
  - Runs `get_vad_model()` on 5-minute blocks padded to 512 samples.
  - Never call `get_speech_timestamps` or `collect_chunks` on the whole array: at 5 h that adds about 2.6 GB of memory.
  - Fallback `energy_probs()` uses main's RMS rule, `max(4×p10, 0.006)`.
- **`RegionTracker(thr=0.4, off=0.25, min_silence=0.3, pad=0.2)`:** turns the probabilities into speech regions and carries an open region across block edges.
- **`plan_chunks(regions, profile)` and a streaming `ChunkPlanner`** that gives the same result. Each piece is `Chunk(index, start, end, speech)`. Rules:
  - Cut only in pauses. Once a piece reaches `cut_lo`, close it at the longest pause that keeps it at or under `cut_hi`.
  - A pause of 2.5 s or more closes the piece once it is at least `min_s`, and that pause is never sent.
  - A region longer than `hard_max` is cut at the lowest-probability 32 ms window in [0.6×max, max].
  - A short tail joins the previous piece if the result stays within `hard_max`.
  - Pieces are contiguous slices of the array (views, no copy).
- **`split_sentences`:** splits after `. ! ? … ։ ؟ 。！？` and newlines; merges pieces under 12 characters; splits pieces over 250 characters at a comma or semicolon near the middle.
- **`spread_segments(chunk, text)`:** weight = letters and digits + 4, spread over the piece's speech timeline, with boundaries snapped to a pause of at least 0.25 s within ±1.5 s. Output is monotonic and stays inside the piece.
- **`transcribe_chunks(audio, chunk_queue, spec, profile, *, send, cancel_event, on_progress, sleep, clock)`:**
  - A VAD producer thread fills a `queue.Queue` with pieces, so the first upload starts after the first block.
  - The coordinator runs on the job thread:
    - It uses `ThreadPoolExecutor(workers)` and polls with `wait(..., 0.25, FIRST_COMPLETED)`.
    - It is the only caller of `on_progress`, because `emit` in run_file_job is not thread-safe.
    - It assembles results in piece order.
  - Workers encode, then send:
    - Up to 4 attempts, backing off 2, 6 and 20 s with ±30% jitter (or Retry-After), sleeping on `cancel_event.wait`.
    - A rate limit sets a shared "not before" time and drops the worker count to 1 for the rest of the job.
    - Mistral request starts are at least 1.1 s apart.
  - Fatal error or cancel: set the event, `shutdown(wait=False, cancel_futures=True)`, raise. Late results are dropped.
  - No speech regions → `NoSpeech` before any request is sent. Every piece empty → `NoSpeech`.
- **`transcribe_file(audio, spec, *, speakers, diarizer, emit, cancelled, send=None)`:**
  - Starts a diarization thread right after decode when speaker labels are on: download the speaker model if missing, then `diarize(audio, on_progress)`.
  - Progress:
    - frac = (seconds of finished pieces + silence the VAD has passed) / duration.
    - It is mapped from `_P_READ_END` to 78 with speaker labels, or 96 without, under the stage "Transcribing".
    - ETA = elapsed × (1 − frac) / frac, once at least `workers` pieces are done and more than 5 s have passed. With speaker labels, ETA = max(cloud ETA, diarization ETA).
    - If pieces finish before diarization, the stage becomes "Finding speakers" (78 → 96).
  - Returns `{segments, turns, language, model_id, stats}`.
  - Language: the set language if it is not auto/multi; otherwise the most common Mistral detection; otherwise `None`.

## 5. file_jobs.py wiring

- **Light imports:** file_jobs must still import without numpy or main (test :388-395), so import cloud modules and engine_choice inside functions only.
- **New constant** `CLOUD_MODELS = ("cloud","own:google","own:mistral")`. Update the comment at :164.
- **`options_from_cfg`** (:272-274): keep `CLOUD_MODELS` values; `"huge-v9"` still becomes `None`.
- **`JobError`** docstring (:210): add reason `"cloud"`.
- **`run_file_job`** (:435): new kwargs `cloud=None, cloud_send=None`, and update the docstring at :438.
  - After :503, if `cloud` is set, call `cloud_files.transcribe_file`.
  - Map errors: `CloudFileError` → `JobError("cloud", msg)`, `Cancelled` → `JobCancelled`, `NoSpeech` → `JobError("no_speech", MSG_NO_SPEECH)`.
  - The cloud path skips steps 3-4 (:505-569: pick, download, load).
  - Step 5 (:571-589): if `turns` were already computed, only run `attribute_segments`.
  - Result (:602-615) adds `"model_name"` and `"engine"`. For cloud, `"model"` is the real id and `decode_seconds` is `None`. Send `info(model=..., model_name=...)` so the card updates.
- **`FileJobQueue`:**
  - `_new_item` (:738): for a cloud value, `model_name` comes from `file_engine(...)["label"]`.
  - `_start_next` (:765-782, GUI thread):
    - Resolve `file_session`. A local result sets `opts["model"]=None` (Automatic).
    - Otherwise set `item["cloud"] = spec_from_cfg(..., token_fn=recorder.get_auth_token)` (set at main.py:2799) and `item["engine"]`.
    - Add `engine` to the start telemetry (:776-777).
  - `_work` (:799-801): pass `cloud=` only when it is set.
  - `_on_info` (:852-854): prefer `info["model_name"]`.
  - `_on_done` (:873-875): use `result["model_name"]`. Call `_note_local_wait` (:876-883) only when `engine == "local"`. Add `engine`, `pieces` and `retries` to the telemetry at :884-890.
  - `_restore_dictation_model` (:914-925): return early for cloud jobs.

## 6. Transcript store and view

- **transcript_store.py:** `save_transcript` (:154) also saves `model_name` and `engine`. `_summary` (:203) reads them with `""` / `"local"` defaults, so old transcripts still show as before. `_EDITABLE` (:46) is unchanged.
- **transcript_view.py:** `model_text(doc)` (:355-356) returns `doc.get("model_name")`, or else `hardware.real_name(doc.get("model"))`. Update its caller at :736.
- The job card already prints `item["model_name"]`, so it needs no change.

## 7. Audio to text page (ui/pages/audio_to_text.py)

- **Docstring** :1-5 and :17-18: same page for every plan; Gusan Cloud marked PRO for Free users; own-key options listed once a key is saved; Privacy mode greys them out.
- **Constants** near :95:
  - Keep `RAIL_FOOT` unchanged (a test pins it).
  - `RAIL_FOOT_CLOUD = "Sent to Gusan Cloud to transcribe."`
  - `RAIL_FOOT_OWN = "Sent to {name} with your key."`
  - `PRIVACY_HINT` and `UPSELL_SPEECH` (same text as smart_actions.py:114 and :102).
  - `MODEL_WATCH = (privacy_mode, google_api_key, mistral_api_key, managed_provider, google_stt_model, mistral_stt_model, admin_tier_override)`.
- **Building the combo** (:1131-1138): replace the fixed list with `_fill_model_combo()`, copied from smart_actions.py:1181-1197.
  - Block signals while refilling.
  - Item text is `line + "  PRO"` for Free users when the option needs Pro.
  - List width = max(260, longest line + 40).
  - Connect the signal after filling.
- **Rail rows:** keep the Accuracy row (:1143) as `self.model_row` so it can show a hint, and the footer icon (:1171) as `self.rail_foot_icon`.
- **New helpers:** `_fill_key()` (plan, provider, which keys exist, model ids) and `model_value()`, copied from smart_actions.py:1225-1239, so the combo shows what will really run.
- **`_sync_settings`** (:1182-1200):
  - Refill when the fill key changed.
  - Select `model_value()` instead of :1187.
  - Grey out cloud and own-key items with `set_choice_enabled` when Privacy mode is on, and show the hint.
- **`_on_model_changed`** (:1222-1223):
  - Return early while syncing.
  - Call `set_file_model`. If it refuses: `_sync_settings()`, and when the problem is `"pro"`, call `app._pro_upsell(...)`.
  - If it accepts: one `_set(K_MODEL, value)` call, so the order pinned at test :324 still holds.
- **`_update_model_line`** (:1250-1256):
  - For cloud and own-key values, show `file_engine(...)["model_name"]` and never pass these values to `hardware.real_name`.
  - Footer: shield + `RAIL_FOOT` (local), "cloud" icon + `RAIL_FOOT_CLOUD`, or "key" icon + `RAIL_FOOT_OWN` with Google or Mistral.
- **`on_setting_changed`** (:888-890): also react to the `MODEL_WATCH` keys. `refresh_account` (:892-893): refill, then `_sync_settings()`.
- **Elsewhere:**
  - ui/pages/settings.py:2504: change the subtitle to "Dictation and meetings."
  - main.py:3654-3655: fix the docstring that says file jobs always run locally.

## 8. Server

Nothing is required.

- **Fit:** 60 s pieces are about 2.6 MB of JSON. The client's 90 s read timeout stays under Supabase's 150 s idle limit.
- **Daily cap (3000 requests, shared with dictation):**
  - A 5 h file is about 250–300 pieces after long silences are skipped, about 10% of the cap.
  - The worst case, with 30 s minimum pieces, is about 600 pieces (20%).
  - Retries only happen on failures.

An optional server follow-up, approved and deployed separately, in this order:

1. Gemini `thinkingBudget: 0`.
2. A `fetch` timeout of about 100 s per provider call.
3. Return the `model` that actually served the piece; the client already reads it.
4. Accept a `mime` field, so the app can send Opus (about 12× less upload).
5. Don't charge failed calls.

## 9. Tests

**New (tests/, Qt-free):**

- **tests/test_cloud_speech.py:**
  - Requests for each engine: Gemini key only in the header; thinkingBudget only for 2.5 Flash; Mistral language rule; proxy body fields.
  - Response parsing.
  - The `classify` table: gusan 401 refresh then fatal, 403, 429 cap fatal, own-key 429 retried with Retry-After or RetryInfo, per-day 429 fatal, 402, 400 language, 5xx, Timeout and ConnectionError.
  - No message ever contains the key.
  - main uses `PROXY_URL` and `MISTRAL_LANGS` from this module.
- **tests/test_cloud_files.py:**
  - RegionTracker across block edges.
  - `plan_chunks`: cut window, hard-max split, silences of 2.5 s or more not sent, tail merge, a synthetic 5 h Gusan file stays at 600 pieces or fewer.
  - The streaming planner gives the same pieces as `plan_chunks`.
  - `split_sentences`: `.`, `?`, `։`, `。`, newlines.
  - `spread_segments`: monotonic, inside the piece, snapped to pauses.
  - With a fake `send` and fake `sleep`:
    - order is kept;
    - retries and backoff happen;
    - a rate limit drops to 1 worker;
    - Mistral starts are spaced;
    - a fatal error stops pending work;
    - cancel returns quickly and drops late results;
    - empty pieces are counted, and all-empty gives `NoSpeech`;
    - no speech means zero requests.
  - Progress is monotonic and ETA stays `None` early.
  - A guard that `get_speech_timestamps` is never called on the full array.
  - `encode_wav` clips.

**Updates:**

- **tests/test_engine_choice.py:** `file_choices`, `file_session`, `set_file_model` and `file_engine` with cloud/Pro/Free/Privacy/keys/provider cases; keep :181-186.
- **tests/test_file_jobs.py:**
  - :309-324: cloud values are kept.
  - Cloud runs with a fake `cloud_send` and diarizer: no download or load calls, speaker labels applied, `model_name`/`engine`/`model`, language rules, cancel, reason `"cloud"`.
  - Queue: lapsed Pro at start runs Automatic; no `_note_local_wait` or dictation-model restore for cloud jobs; telemetry `engine`.
  - `CLOUD_MODELS == FILE_CLOUD_VALUES`.
  - :388 still passes.
- **tests/test_transcript_store.py:** `model_name`/`engine` round trip, and defaults for old transcripts.
- **tests/test_settings_apply.py:** `file_model` accepted and refused.
- **tests/test_api_keys.py:** removing the key resets `file_model`; :76 unchanged.
- **tests_qt/test_page_audio_to_text.py:**
  - :339-341 now has 8 plain names (adds "Gusan Cloud"). Free item text ends in `"  PRO"`; Pro's doesn't.
  - Free pick: `_pro_upsell("Fast cloud transcription")`, no `set_setting`, combo snaps back.
  - Pro pick: `set_setting("file_model","cloud")`, model line "Gemini 2.5 Flash", cloud footer.
  - Own-key items appear after the key is set and `on_setting_changed` runs; own-key footer.
  - Privacy mode greys the items and shows the hint.
  - Free user with a saved cloud pick sees Automatic and the local footer.
  - `refresh_account` adds or removes the PRO mark.
  - Keep :145-161 and :168-179.
- **tests_qt (transcript view):** a cloud transcript shows its `model_name` in the meta line.

## 10. Order of work

1. engine_choice.
2. settings_apply and api_keys.
3. cloud_speech.
4. cloud_files.
5. file_jobs wiring.
6. Transcript store and view.
7. Page, Settings subtitle, Qt tests.
8. main.py import lines and docstring.
9. Run all of tests/ and tests_qt/, then restart the dev build safely: check it's idle first, never mid-take.
10. The founder runs the paid real-world checks (we make no paid calls): 10 min, 1 h and 5 h files on Gusan Cloud and on each own key. Record wall time, pieces, retries and speaker-label quality compared with local.
11. Add a line to the release notes. No commit without an explicit OK.

## 11. Risks

- **Speaker labels are approximate on cloud.** Sentence times are spread, not measured, so quick back-and-forth is less precise than with local Whisper segments. 96% was measured on synthetic audio only.
- **Wrong model name possible.** The proxy can quietly switch from Gemini to Voxtral, so "Gusan Cloud · Gemini 2.5 Flash" may be wrong for some pieces until the server returns `model`.
- **Upload size.** WAV through the proxy is about 154 MB per hour (about 770 MB for 5 h). On slow connections cloud can be slower than a GPU on this computer. Latency per piece is untested.
- **Shared daily cap.** Heavy dictation plus several long files on the same day can hit it; the job then stops with the generic sentence.
- **Supabase request size limit** is undocumented. 60 s pieces leave a wide margin.
- **Free Google keys** have very low daily request limits, so long files on them can fail with Google's own sentence.
- **CPU.** VAD plus speaker labels can become the bottleneck on weak machines.
- **Silent word loss.** A Gemini safety or recitation block on real speech is treated as "no words". It is counted in telemetry so we can see if it happens.
- **Unsupported Mistral languages.** `MISTRAL_LANGS` (main.py:587-588) includes some languages Voxtral may reject (uk, pl, tr, ro, cs, el). That stops the job with a clear message.

## 12. Deliberately left out

- Moving dictation's three cloud calls onto `cloud_speech`, including taking the Google key out of the URL at main.py:2415-2416. This should be its own follow-up.
- All server changes: Opus upload, thinking off, timeouts, returning the model, metering by seconds, a separate counter for files.
- Voxtral segment timestamps and Mistral `diarize`; Gemini timestamps or language detection.
- Filling failed pieces locally, partial transcripts, resuming a failed job, a "Finish on this computer" button, checking remaining quota before starting.
- Letting dictation run during a cloud file job. The busy message still says "same speech model".
- Other providers' keys as speech engines (OpenAI, Anthropic and other writer keys).
- A cloud icon on the transcript's model chip.
---

## THE CRITIQUE (holes found in the plan; FINAL DECISIONS above say which fixes apply)

**Plan critique: cloud models in Audio to text (checked against the files on disk, branch prep-2.0.0)**

Almost every anchor matches the code. The one exception is main.py:73, which defines `MANAGED_PROXY_URL`, not `PROXY_URL`.

## Holes (most severe first)

1. **The queue can get stuck, and dictation with it.**
   - Breaks: `FileJobQueue._start_next` (file_jobs.py:765-782) has no try/except, and it already set `_current` and `_file_job_running=True` before spawning the worker. If the new resolution code raises there, the worker never starts and the flag stays up. Dictation, meetings and the idle sweep then refuse until the app restarts. This will happen: `item["cloud"]=spec_from_cfg(..., token_fn=recorder.get_auth_token)` raises AttributeError with the test fakes (`recorder = object()` in tests/test_file_jobs.py:405 and tests_qt/test_file_jobs_qt.py:48), and the FakeApp has no `is_pro`.
   - Fix: resolve only when `opts["model"] in CLOUD_MODELS`. Use `getattr` for `is_pro` and `get_auth_token`. Do the resolution at the top of `_work`, inside its try, so an error becomes a failed job and `_on_done` clears the flag. Or wrap it in `_start_next` and fall back to Automatic on any error.

2. **Tests that will break and aren't listed.**
   - tests/test_file_jobs.py:523-524 compares the exact started-telemetry dict `{"model","speakers","timestamps"}`. Adding `engine` breaks it.
   - tests/test_file_jobs.py:516-517 and :530, and tests_qt/test_file_jobs_qt.py:102: the fake runners return `make_result`/`_result`, which have no `model_name` or `engine`.
     - `_on_done` reading `result["model_name"]` raises KeyError.
     - "`_note_local_wait` only when engine == local" then never fires, so the `waits` assertions fail.
     - Fix: use `result.get("model_name") or whisper_name(model)` and `result.get("engine", "local")`.
   - tests/test_file_jobs.py:296-306 searches file_jobs.py's source for "1 hour", "up to 5 hours" and "too long". New comments in file_jobs must avoid those phrases.
   - tests_qt/test_page_audio_to_text.py:168-179 still passes, but its name ("no limits or upsell") and the page docstring at :4-5 ("no upgrade prompt") become untrue once a Free pick calls `_pro_upsell`. Rename the test and fix the docstring.

3. **Running speaker labels in parallel breaks cancel.**
   - Breaks: `diarization.diarize` can't be stopped. It holds the module `_lock` (diarization.py:178) and up to 8 CPU threads, and keeps the whole audio array alive (about 1.15 GB for 5 h).
   - After "cancel returns quickly" or a fatal cloud error, `_file_job_running` drops while diarize keeps running. The next queued job or a meeting's own speaker step then blocks on `_lock`, and the CPU stays busy.
   - Fix: the job thread always waits for the speaker thread to end before returning (the card shows "Stopping" until then). Or run speaker labels after the pieces, as local step 5 does today.

4. **Privacy mode or a removed key during a job.**
   - Breaks: the Spec is built once at job start, so turning Privacy mode on mid-job keeps uploading for possibly hours. Removing the key mid-job keeps sending with the key the user just removed.
   - Fix: the coordinator checks `cfg["privacy_mode"]` and reads the key from cfg before each send. If Privacy mode is on or the key is gone, stop the job with one plain sentence.

5. **Wrong sentence for an offline Pro user.**
   - Breaks: `auth.get_access_token()` returns None when the token refresh fails offline (auth.py:662-672). The plan maps that to `signed_out`: "Sign in to use Gusan Cloud…". Signed-out users never reach the cloud path (is_pro is False at start, so the job runs locally), so this sentence will mostly be shown to signed-in users who are offline.
   - Fix: no token while `auth.is_authenticated` is true → show the "Couldn't reach Gusan Cloud…" sentence instead.

6. **"401, refresh once, then fatal" can't be built as described.**
   - Breaks: `token_fn` is `auth.get_access_token`, which returns the cached token until it expires and has no force-refresh. The plan's extra 60 s cache makes this worse.
   - Fix: treat 401 as fatal right away (dictation does the same, main.py:2504-2505). Replace the cache with a plain lock around `token_fn`.

7. **The daily cap reached mid-file (3000 requests/day, shared with dictation).**
   - Breaks: proxy 429 `quota_exceeded` leads to "…couldn't finish this file. Try again…". Trying again fails until midnight UTC. Every queued cloud job also fails, each after a full decode. Dictation in the same situation quietly falls back to local (main.py:2493-2499).
   - Fix: on `cap`, `pro_required`, or no token at the first piece, continue into the local steps 3-4 with the audio already decoded. The whole job runs on Whisper, so models are never mixed, and the card shows the Whisper name. No limit wording needed.

8. **Custom vocabulary is silently lost.**
   - Breaks: transcribe-proxy never reads `body.vocabulary` (index.ts:159-188), and the Mistral request carries no terms. Local file jobs correct every segment with `vocabulary.correct_spellings` (main.py:2175, :2209). Picking Gusan Cloud therefore drops the user's vocabulary. Dictation had this exact bug before (main.py:2409-2411).
   - Fix: run `vocabulary.correct_spellings(text, vocabulary.load_terms(cfg))` on every cloud piece. It runs on this computer, so Privacy mode isn't affected. Remove the plan's claim that vocabulary reaches the proxy.

9. **The job card's model text is inconsistent.**
   - Breaks: `_new_item` uses `file_engine(...)["label"]`, so the card's meta line (already joined with " · ") gets "Gusan Cloud · Gemini 2.5 Flash" or "Google Gemini · Gemini 2.5 Flash · your own key". Local jobs show just "Whisper Small", and the rail uses `["model_name"]`.
   - Fix: use `["model_name"]` on the card. When `_start_next` falls back to Automatic, reset `item["model_name"]=""`.

10. **Busy messages become untrue.** `BUSY_FOR_DICTATION`, `BUSY_FOR_MEETING` and `MSG_RECORDING` (file_jobs.py:192-196) say "uses the same speech model", which is false for a cloud job. Use a neutral sentence while the running job is cloud. Letting dictation run during a cloud job is the bigger speed win, as a follow-up.

11. **Own-key error copy.**
    - Breaks: `api_keys.cloud_error_message` returns raw provider text ("Google rejected the request: {msg[:90]}", "error (HTTP 500). …"). For 429 it says "rate limit or quota reached. Wait a moment", which is also wrong for a per-day quota. This goes against plain copy and no limit wording.
    - Fix: write 3-4 short sentences in cloud_speech, and send the provider text to the log only.

12. **A cloud value can leak into the local path.** If `run_file_job` gets `opts["model"]=="cloud"` without a Spec (the Spec build failed, or another caller passes opts), step 3 runs `model_downloaded("cloud")` and then tries to download a model called "cloud". The card says "Whisper cloud couldn't be downloaded". Fix: map cloud values to None (Automatic) when `cloud is None`.

13. **The API key can end up in logs.** The Spec on `item` is emitted to the UI through signals and kept in the page's `_outcome`, and the default dataclass repr prints `api_key`. Fix: `field(repr=False)` on `api_key` and `token_fn`, or keep the Spec out of `item` and pass it to `_work` directly.

14. **Piece size contradicts the server analysis.** Gusan's `hard_max` is 120 s, which means bodies up to about 5.1 MB. Sections 0.7 and 8 only argue that 60 s pieces fit. Either set hard_max to 90 s or check the 5 MB case.

15. **Timestamps are estimates on cloud, not only speaker labels.** The Timestamps toggle and the .srt export get spread times. Add this to the Risks section.

16. **Speed on Gusan Cloud.** The proxy calls Gemini 2.5 Flash with thinking at its default (index.ts:109), while own-key requests turn thinking off. So the Pro option will be slower and costlier than a user's own key. Recommend shipping the one-line server change with this feature, once approved and deployed.

## What to drop

- **Moving `PROXY_URL` and `MISTRAL_LANGS` out of main**, plus the matching main.py import edits and test. It touches dictation's file for nothing, and the name doesn't match `MANAGED_PROXY_URL` anyway.
- **The 60 s token cache and the 401 refresh path** (see hole 6).
- **One of the two chunk planners.** Keep only the streaming `ChunkPlanner`, test it with a list input, and drop the equivalence test. Also drop the `get_speech_timestamps` guard test.
- **Mistral's 1.1 s gap between request starts, Gemini `RetryInfo` parsing and the `quota_day` error kind.** Generic 429 backoff plus dropping to one worker covers them.
- **Reading an optional `model` field from the proxy.** The server never sends it.
- **The new `model_name`/`engine` fields in transcript_store and the `model_text(doc)` change.**
  - Save `model` as the real id: `gemini-2.5-flash`, or `voxtral-mini-latest` for Gusan with provider mistral.
  - Fall back to `engine_choice.MODEL_NAMES` (engine_choice.py:186 and :198 already have both ids).
  - Tell local from cloud with `model in WHISPER_MODELS`, for `_note_local_wait` and the dictation-model restore.
  - This also removes passing `model_name` through info, the result, `_on_info` and `_on_done`, and avoids the test breaks in hole 2.
- **`pieces` and `retries` telemetry.** Keep `engine` and update test :523.
- **The `runs_on`/`pro` plumbing for audio_to_text.** Nothing in the UI calls `runs_on(..., "audio_to_text")`.
- **The risk about Voxtral rejecting uk, pl, tr, ro, cs or el.** Audio to text only offers auto, mixed, en, ar, hy, fr, de, ru and es (ui/pages/transcript_view.py:54-56).