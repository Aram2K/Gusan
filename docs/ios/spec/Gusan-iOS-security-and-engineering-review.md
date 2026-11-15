# Gusan iOS — security + engineering review of "technical specification v1"

Reviewer: security engineer / second reviewer. Scope: BRIEF.md, spec v1, H Agents API docs (hub.hcompany.ai, fetched 2026-11-08), Apple developer docs (fetched 2026-11-08), and the code on `devin/1794056309-ios-keyboard`, `devin/1794085578-agent-action`, `devin/1794085675-ios-agent-tasks`, `devin/1794090252-ios-screen`. Nothing was implemented; no PR.

Tags: **[repo]** read in the code · **[H doc]** / **[Apple doc]** stated in the cited page · **[inferred]** my conclusion · **[spike]** needs a device/API test.

---

## 0. Verdict in one page

| Area | Verdict | One-line reason |
|---|---|---|
| "The agent never sees or knows a password" | **Holds for the H model, with 4 assumptions (§A2.3). Does not hold for Gusan Cloud (transient) or H (at rest, revealable).** | `fill_secret_at` types outside the model context [H doc]; the org API key can `reveal` any H-hosted item and reveal cannot be disabled [H doc]. |
| Vault design (§7) | **Right shape; 7 gaps, 2 of them design-changing** | Ephemeral per-session vault is the correct default. Gaps: session-swap complexity avoidable (S9), vault cleanup on failure paths, release authorisation, reveal exposure, keychain flags, step-URL leakage, "always ask off" mode. |
| Agent confirm gate | **Structurally sound for "nothing happens before Confirm"; prompt-level only for "only the confirmed steps happen"** | Prepare runs in H `text` mode, which cannot click or type [H doc]; execute is a fresh visual session that can go anywhere. Add an off-plan navigation kill switch (§A3). |
| App Group / pasteboard hand-off | **One high finding** | With the App Group broken and the fallback on, the Supabase access **and refresh token** sit in the team pasteboard with no expiry [repo]. Fix in §A8. |
| JWT in keyboard | Acceptable; two hardening items | Keyboard legitimately needs the JWT; vault credentials must never be reachable from it (no shared access group) — the spec already says so, make it a test. |
| Screen frames | Acceptable as designed | Frames go only on request, 10 s pasteboard copy, no logging [repo]. Provider retention + "everything on screen" caveat remain. |
| Transcription audio | Acceptable | `transcribe-proxy` logs provider/model/status only [repo]; third-party STT retention is the residual. |
| Calendar tokens | Design right; schema/RLS details missing | Split the encrypted token into a service-role-only table; single-use `state`; revoke on disconnect. |
| APNs-less notifications | Correct; one content tweak | Confirm/Cancel actions already require device auth [repo]; stop putting the plan summary in the lock-screen body. |
| Logging | Good today; two leaks to close | `agent_tasks.steps` stores full URLs incl. query strings from agent events [repo]; provider error bodies are logged up to 160 chars. |
| Spec engineering claims | Mostly correct. Wrong/optimistic: `openAppWhenRun` (deprecated), `LiveActivityStartingIntent` availability, Live Activity "iOS 16.1 interactivity", "Back Tap without visible UI", P5 estimate | §B |
| Spikes | Keep S1, S4(+S5), S8; demote S2, S3, S6, S7; add S9 (H vault mid-session binding) and S10 (AutoFill key icon) | §B4 |

---

# A. Security review

## A1. Assets, trust boundaries, attacker models

**Assets** (most to least sensitive)
1. User web credentials + TOTP seeds (plaintext on the phone in the biometric keychain; transient in `vault-action`; at rest in an H vault for minutes).
2. `H_company_computer_agents` (org key: creates sessions, creates vault items, **reveals** them, reads every session's screenshots/events), `H_AGENTS_WEBHOOK_SECRET`, Google client secret, `SUPABASE_SERVICE_ROLE_KEY`.
3. Supabase session: access token (1 h) + refresh token (long-lived, rotating) — full account takeover if stolen.
4. Agent task content: instruction, approved plan, steps (page-derived), results; H session screenshots of logged-in pages.
5. Screen frames (whatever is on screen), dictation audio/transcripts, calendar digest/events, Google refresh token.
6. Audit trail integrity.

**Trust boundaries**
- Host app ↔ keyboard extension (system-mediated: `UITextDocumentProxy` only) ↔ Gusan app (App Group file / shared keychain group / **team-scoped named pasteboards** / Darwin notifications) ↔ broadcast extension.
- Phone ↔ Gusan Cloud (Supabase edge functions, JWT via `auth.getUser()`, RLS) ↔ H (org API key) ↔ third-party web sites ↔ STT/LLM providers ↔ Google.
- Inside H: model ↔ browser runtime (`fill_secret_at` boundary) ↔ H storage (vaults, screenshots, events) ↔ H staff/org members (platform UI, `reveal`).

**Attacker models and what each reaches today**

| Attacker | Reaches | Blocked by |
|---|---|---|
| Malicious web page / prompt injection | The execute agent's behaviour (any action on any site within max_steps); the model's observations | Prompt guards in `instructions()` [repo]; domain gating of `fill_secret_at` [H doc]; prepare phase is read-only [H doc]. **Not** blocked: off-plan navigation (§A3). |
| Compromised Gusan Cloud logs | Status codes, task ids, error codes, provider error snippets, `steps` text (URLs) | No body logging [repo]. Gap: URL query strings in `steps`, 160-char provider error bodies (§A10). |
| Lost phone (locked) | Nothing in biometric keychain; `AfterFirstUnlock` session token is readable only by the app process after first unlock | Keychain classes; recommend `WhenPasscodeSetThisDeviceOnly` for vault items. |
| Lost phone (unlocked, no Face ID) | Agent tasks, Confirm (Face ID not required in the keyboard card), screen/voice features; **not** vault release (Face ID per use) | Make Confirm for `action_kind` = payment/send require `LAContext` in the app (§A3). |
| Another app on the phone, same Apple ID team (sideload) | Everything in the named pasteboards for their lifetime: **session token (no expiry)**, voice transcript (600 s), screen frame (10 s), settings | Nothing today for the session token (§A8). |
| Another app, different team | Nothing cross-process | iOS sandbox; named pasteboards are same-team only (iOS 10+). |
| H insider / any member of the H org | Plaintext of every H-hosted vault item via `reveal` (audit-logged), all screenshots/events of all sessions | Ephemeral vaults (minutes), one-member org, screenshot TTL. Cannot be eliminated with provider `internal`. |
| Sideload re-signing breaks App Groups | Forces the pasteboard fallback → enlarges the "another app" surface | `AppGroupResolver` + diagnostics [repo]; fix is §A8. |

## A2. Vault: iPhone Passwords → Face ID → keychain → ephemeral H vault → `fill_secret_at`

### A2.1 Verified against H docs
- `POST /vaults/{vault_id}/items`: "values travel in clear text inside the body… never log the request"; 422 never echoes values. **[H doc create-item]**
- `GET /vaults/{vault_id}/items/{item_id}/reveal` **exists**, returns clear-text `values` (current TOTP code, never the seed), is audit-logged, `Cache-Control: no-store`, and "any member of your organization can reveal" H-hosted items. **No documented way to disable it, scope it per key, or restrict it per vault.** The overview's "no endpoint returns secrets" is wrong for `internal`; the spec already says so — correct. **[H doc reveal-item]**
- Binding: `vault_id` is a field of the `web` environment, set at session creation (inline `agent.environments` or `overrides["agent.environments[kind=web].vault_id"]`). No endpoint mutates a running session's environments. "Cloud browsers only." **[H doc browser/configuration, sessions/create]**
- `fill_secret_at` exists **only in `visual` mode**; `text` mode "cannot click or type". **[H doc browser/configuration → Actions table]** The execute session is visual by default [repo `sessionRequest`] — fine; state it explicitly in §7.4.
- Domain gating: hostname equal to or subdomain of a stored URL; closest host wins; ties → most recently created item. **[H doc vaults/overview]** With one item per ephemeral vault there is no tie — another argument for the ephemeral design.
- 1000 H-hosted items per org; cloud environment starts 10/min/user. **[H doc plans-and-limits]**
- Custom tools: declared at session create (`agent.tools`), session goes `awaiting_tool_results`, your code posts `tool_results`; **at-least-once**; H does not validate your `tool_req` against the pending call. **[H doc custom-tools]**
- Retention knobs: `delete_after_min`, `delete_screenshot_after_min` (repo uses 1440 / 60). `agent_view_url` lets any org login watch the session. **[H doc sessions/create, retrieve]**

### A2.2 Verified against Apple docs
- No API reads/enumerates iCloud Keychain passwords. `ASAuthorizationPasswordProvider` is for the app's own associated domains (paid team only). Password AutoFill offers *any* saved login into fields with `textContentType = .username/.password` after Face ID, "even if those apps haven't been updated". **[Apple doc security/password-autofill]** The spec's §7.1 is correct. Consequence for the owner's brief: "connect iPhone Passwords" is really **"import a copy"** — Gusan stores its own copy; a password changed later in iCloud Keychain does not propagate. Say so in the UI.

### A2.3 Does "the agent never sees or knows a password" hold?
**Yes for the H model**, under these assumptions:
1. H implements `fill_secret_at` as documented (value typed by the runtime, not in the model context). Not independently verifiable; H says so in writing.
2. The target field masks its value. `fill_secret_at` types at coordinates into *whatever* input is there. If the agent picks a `type=text` field (phishing page, "show password" toggled, a search box), the next screenshot shows the password in clear and the model sees it. Domain gating bounds this to the user's own hostname + subdomains, not to the password field. **Residual, cannot be removed by Gusan.**
3. The page itself does not echo the credential (e.g. "You signed in as alice@…" is fine; echoing passwords is rare).
4. The task never asks the user to type the password anywhere the model reads (strip, chat, OTP card). `instructions()` already forbids asking for credentials [repo]; enforce in UI copy too.

**Who does know it:** the phone (biometric keychain), Gusan Cloud in memory during `release`, H at rest for the ephemeral vault's lifetime (revealable with the org key by any org member), H staff (per their own controls). Username/email is visible to the model by design (typed via `write` or visible on the page) — treat it as PII, not a secret.

**Residual risks, ranked:** (1) reveal endpoint + leaked org key or a second org member; (2) H insider reading post-login screenshots (balances, messages), not the password itself; (3) visible-field echo (assumption 2); (4) ephemeral vault not deleted on a failure path (§A2.4-G2); (5) a Gusan Cloud compromise exfiltrating during `release` (spec covers: small reviewed function; add the release token below).

### A2.4 Gaps and exact fixes

**G1 — The session swap is probably unnecessary (design-changing, saves days).** §7.4 cancels the execute session and creates a new one with `vault_id` + a persisted browser profile because "the vault must be bound at creation". Alternative: **create the ephemeral vault empty at `confirm` and bind it to the execute session from the start**, then `POST /vaults/{id}/items` only when the user approves the login. The docs bind the *vault* at creation; nothing says items are snapshotted at that moment, and domain matching is evaluated per page. **[inferred — add spike S9: API-only, 1 h]** If S9 passes: no cancel/restart, no `browser_profile_id` + `persist_browser_profile` writer lock, no "resume from step k" prompt, one fewer cloud-environment start per login (10/min/user cap). If S9 fails, keep §7.4 but note that `persist_browser_profile` saves state **only when the old session ends** [H doc browser/profiles], so the cancel must complete before the new create (serialise; expect several seconds; prompt must restate the goal, not "step k").

**G2 — Cleanup is only on the happy path.** Vault/item deletion must survive: `vault-action` crash after creating the vault, webhook not delivered, `cancel`, `expire()`, `timed_out`, H `failed`. Fix: name vaults `gusan-task-<task_id>`; store `h_vault_id` in `agent_task_runs`; delete in `expire()`, `cancel`, and in `sync()` when `patch.status` becomes terminal; add a pg_cron (or scheduled edge function) sweeper every 10 min: `GET /vaults` → delete any `gusan-task-*` whose task is terminal or older than `max_time_s + 900 s`. Treat "delete vault" (cascades items) as the primitive; log only `vault_deleted`/`vault_delete_failed` + task id.

**G3 — Release authorisation.** The release body carries plaintext, authenticated only by the user JWT. A stolen JWT (see §A8) + a task in `awaiting_login` = exfiltration channel to an attacker-chosen hostname? No — the hostname comes from the agent's tool call, not the client; but the *value* does. Fixes in `vault-action` (`release`): (a) require `agent_tasks.status = 'awaiting_login'` **and** `agent_task_runs.tool_req.hostname` equal to `vault_logins.hostname` for `login_id` owned by `auth.uid()`; (b) issue a one-time `release_nonce` (uuid, 15 min) written to `agent_task_runs` when the task enters `awaiting_login`, returned only in the `status` response, consumed on first release (compare-and-set like `save()`); (c) `≤ 3` releases per task (`agent_task_runs.release_count`); (d) body cap 64 KB; (e) `Content-Type: application/json` only; (f) `values` validated to the 5 allowed field names, ≤ 4096 chars each, and the whole body object is **never** passed to any logger, `Failure`, or thrown error (`catch (e) { console.log("vault-action", code) }` only). Add a CI test that greps `vault-action/index.ts` for `console.*(body|values|raw)` and fails.

**G4 — `request_login` must be a real custom tool with validation.** Today `sync()` rejects every pending tool call with an `error_event` [repo]. For execute sessions only: declare `request_login(hostname)` and `request_otp(source)` in `agent.tools`; in `sync()` on `awaiting_tool_results`, validate `call.tool_name` ∈ allowlist, `call.args.hostname` is a valid hostname and **matches a `vault_logins.hostname` of this user or the plan's `start_url` host**; otherwise answer `error_event` ("no login for that site") and keep going; store `call` in `agent_task_runs.tool_req` and set `status = awaiting_login` (never expose `call.id` to the client). Idempotency: key the parked call by `(task_id, hostname)`; a duplicate delivery of the same call resolves to the same result. Deadline: 15 min → `login_expired` (spec has it).

**G5 — Keychain flags.** Use `kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly` (not `WhenUnlockedThisDeviceOnly`): guarantees a passcode exists and the items are erased if the passcode is removed. `SecAccessControlCreateWithFlags(.biometryCurrentSet)` as planned; pass one `LAContext` via `kSecUseAuthenticationContext` so the Face ID prompt is shown once per release, not once per field. **No `kSecAttrAccessGroup`**, not in the App Group file, not in any pasteboard — add a GusanKit test asserting `VaultStore` writes have no access group and that `SessionStore` backends never see `Gusan.vault.*` services. Tell the user that re-enrolling Face ID invalidates stored logins (by design).

**G6 — "Always ask before using a login" off = persistent H vault.** This is the only asset the reveal endpoint exposes for longer than minutes. Do not ship it in v1 (D-decision for the owner). If shipped later: one vault per user named by a hash of `user_id`, items deleted on login deletion and account deletion, and shown in the Vault tab as "stored at H (revealable by Gusan operators)".

**G7 — Secrets-adjacent data around the vault.** (a) `vault_logins.username_hint` only, never the username (an e-mail is PII that leaks the account identity to anyone with DB read). (b) The `request_login` approval screen must show the agent's **current page URL** (from the latest `go_to_web`/`policy_event` in `/changes`) next to the requested hostname; deny on mismatch. (c) For execute sessions that performed a release: `delete_screenshot_after_min: 10`, `delete_after_min: 60`, and `overrides["agent.environments[kind=web].mode"] = {type: "visual", markdown: false}` (page text alongside screenshots would also carry the page's contents into H storage). (d) Ask H (D6) in writing: can reveal be disabled or scoped per API key; what their staff access policy to vault values and screenshots is; retention of `fill_secret_at` events. Until answered, keep the H org at **one member** and rotate the key quarterly and on any suspicion.

### A2.5 What the spec gets right (keep)
Ephemeral per-session vault as default; Face ID per use in the **app**, keyboard out of the loop; no secret columns in Postgres; `vault_logins` metadata + `vault_events` audit; domain gating by user-chosen URL; 1Password provider as the later "zero plaintext at Gusan Cloud" option (verified: H never returns the service-account token [H doc]); honest "not end-to-end" statement.

## A3. Agent confirm gate (`agent-action/index.ts`, migrations 0001/0002)

**Right [repo]:** `do` tasks run prepare in H `text` mode (read-only by construction), 300 s/60 steps; the plan is validated (`validPlan`) and stored server-side (`agent_tasks.confirmation`, `agent_task_runs.plan`); `confirm` ignores any client-supplied plan and re-reads `row.confirmation`; execute is a new session with idempotency key `${row.id}:execute`; 15 min confirm expiry; `expire()` is authoritative even if H is down; `agent_task_runs` is service-role-only; `begin_agent_task` takes an advisory lock and enforces 2 active / 40 per day; webhook HMAC with 300 s window, unknown session → 200; `Idempotency-Key` on session create; custom tool calls refused; prompts fence screen context and the plan as data; `safeResult` sanitises markdown/links/images; Confirm never where Cancel was; notification actions carry `.authenticationRequired`.

**Gaps and fixes**
1. **Execute is bounded by prompt only.** Add a structural kill switch in `sync()`: for `run.phase === "execute"`, derive an allowlist of hostnames from `plan.start_url` + any URLs in `plan.steps` + the login hostnames of this task; for each `policy_event` whose `tool_reqs[].tool_name === "go_to_web"`, compare `new URL(args.url).hostname` (suffix-match); on mismatch → `DELETE /sessions/{id}`, `status = failed`, `error_code = off_plan_navigation`. Also lower execute `max_steps` to `min(120, 15 + 10 × plan.steps.length)`.
2. **Confirm from the keyboard card has no Face ID.** Anyone holding the unlocked phone can confirm a payment. In the app, gate Confirm for `action_kind ∈ {payment, send, delete, post}` behind `LAContext.evaluatePolicy(.deviceOwnerAuthentication)`; in the keyboard, for those kinds show "Confirm in Gusan" (deep link) instead of a Confirm button. `AgentCardView` already has the slot logic.
3. **Unauthenticated 8 MiB bodies.** `Deno.serve` reads up to 8 MiB before checking `Authorization`. Move the Bearer regex check (and the `?action=webhook` branch selection) before `req.body.getReader()`; keep the cap.
4. **`stepText` stores full URLs** (`Opening ${call.args.url}`) into `agent_tasks.steps` → query strings with tokens/ids from third-party sites end up in Postgres and in the client. Strip `search` and `hash` (`new URL(u).origin + pathname`), and store no steps at all while a `request_login` is parked.
5. **Message images in prepare** (`body.images` → H `messages[].images`): confirm with H whether `delete_screenshot_after_min` covers user-supplied images (D6 list). Cap today is 4 × 6 MiB; set 2 × 1 MiB for the iPhone (frames are ≤ 450 KB).
6. **`agent_task_runs.plan` retention.** It holds the instruction and page-derived plan. Null it (`plan`, `tool_req`) when the task becomes terminal; `agent_tasks` rows older than 30 days → pg_cron delete.
7. CORS `*` and the `X-Gusan-Privacy-Mode` header are harmless for a native client; remove with §9 as planned.

## A4. Screen frames (`GusanScreen`, `ScreenChannel`)
**Right [repo]:** frames only on explicit request, JPEG ≤ 450 KB, expiry 10 s, removed on consumption, no network/disk in the extension, `Logger` emits metadata only, chip hidden in secure fields, acknowledgement expires. `smart-action` keeps only `data:` image URLs and logs route/model/ms only.
**Gaps:** (a) frames go to whichever LLM route is active (OpenAI/Claude/Gemini) — pin screen requests to one provider with a zero-data-retention agreement and say which in the "What is shared" card; (b) the 10 s pasteboard copy is readable by same-team apps — fine, but the `pasteboardFallback` toggle must also exist on the Screen page; (c) two frames (§4) doubles exposure; keep the earlier frame only if the keyboard-covered region is > 50 %; (d) `ScreenSettings.explained` version-bound acknowledgement is good — also re-ask when the idle period is changed to "always on".

## A5. Transcription audio (`transcribe-proxy`)
**Right [repo]:** JSON logs `stt_ok/stt_fail` with provider/model/status/120-char redacted detail, never audio or text; size cap; `getUser()`; refused language retried once. No transcript persisted that I could find.
**Gaps:** audio is sent to third parties (Gladia/Mistral/ElevenLabs/Gemini/OpenAI by route) — enumerate them in the privacy text and prefer providers with no-training/zero-retention terms for the default route; the `detail` snippet of a provider error can include transcribed text for some providers (e.g. moderation messages) — reduce to `status` + JSON `error.type/code` only.

## A6. Calendar tokens (§8)
**Right:** server-side OAuth with PKCE and `state`; refresh token encrypted (Supabase Vault/pgsodium); read-only `calendar.events.owned.readonly`; EventKit digest built by the app, never the keyboard; "can't request read-only EventKit" is correct [Apple doc].
**Fixes:** (a) split `connections` into `connections` (owner `select` via RLS: provider, email, scopes, status, created_at) and `connection_secrets (user_id, provider, enc_refresh_token, access_token, access_expires_at)` with `revoke all … from anon, authenticated` — same pattern as `agent_task_runs`; (b) `state` = random uuid stored in `oauth_states(user_id, state, code_verifier, expires_at)`; single-use, 10 min; (c) the `callback` request URL contains `code` — Supabase function logs record the path; verify under S8 whether the query string is logged; (d) on disconnect call `https://oauth2.googleapis.com/revoke` then delete the row; (e) cache the access token (≤ 55 min) to avoid a refresh per `smart-action` call; (f) digest titles default **off**; digest lives in the App Group file / pasteboard like settings, so it is same-team readable — acceptable at ≤ 8 KB without titles.

## A7. APNs-less notifications (§6.4, `AgentCenter`)
**Right [repo]:** local notifications only while the process runs; Confirm/Cancel actions have `.authenticationRequired`; BGAppRefresh is opportunistic and the spec says so.
**Fix:** `content.body = pending.confirmation?.summary` puts the plan ("Pay €120 to …") on the lock screen when previews are on. Use a generic body ("Gusan needs your OK — open to review") and `content.subtitle = task.title` only for research; set `interruptionLevel = .timeSensitive` for confirmations.

## A8. App Group / pasteboard hand-off (`SessionBackend.swift`, `SessionStore.swift`)

**Finding (high).** `NamedSessionPasteboard` sets `expirationDate` only when `lifetime != nil`; the `"session"` key has no lifetime → when `usePasteboard()` is true (App Group broken, `pasteboardFallback` on) the serialised `Session` — **access token, refresh token, user id, e-mail** — sits in `UIPasteboard(name: "com.aram2k.gusan.session")` indefinitely, readable by any app signed by the same team. On a free Apple ID the "team" is the owner's personal team, i.e. **every other IPA the owner sideloads with that Apple ID** (a repackaged game is enough). `.localOnly` only stops Handoff; `setPersistent` is a no-op since iOS 10. Refresh token ⇒ account takeover ⇒ `agent-action start` + Confirm (+ vault `release` would still need Face ID on the real phone — the design's one saving grace).

**Fix (code-level):**
1. Never put the **refresh token** in the pasteboard: add `Session.shareable` (access token, expiry, user id only) and write that to the "Team pasteboard" backend; the keyboard, lacking a refresh token, asks the app to refresh via Darwin notification `gusan.session.refresh.<group>` (the app already republishes on `writeRefreshed`), falling back to "Open Gusan to sign in again" when the app is not alive.
2. Give the session pasteboard a `lifetime` (≤ 15 min) and have the app re-publish it on every foreground/refresh and on the Darwin request (mirror `ScreenChannel`'s `status` heartbeat).
3. Set `SettingsStore.pasteboardFallback` default **off** when `AppGroupResolver` reports a working group; keep the toggle for the broken case and show the warning text from `SessionDiagnostics` ("readable by other apps you install with this Apple ID").
4. Voice pasteboard (600 s, carries transcript/result): drop to 120 s; it is already removed on consumption.
5. Add GusanKit tests: pasteboard backend never stores `refreshToken`; every pasteboard write has an `expirationDate`.

## A9. JWT handling in the keyboard extension
The keyboard needs the Supabase JWT (it calls `smart-action`, `transcribe-proxy`, `agent-action`). Keychain class `AfterFirstUnlockThisDeviceOnly` and the App Group file protection class are appropriate for a token the keyboard must read while the host app is in use [repo]. Keep. Hardening: (a) §A8 (no refresh token outside keychain/App Group file); (b) the vault keychain items must be unreachable from the keyboard (G5) so that a keyboard-only compromise can never `release`; (c) never place the JWT in a URL (`wait_s` is in the body — good); (d) `AuthClient.publishableKey` is public by design — RLS is the only guard, so audit `is_pro` and `use_smart_action_quota` to be `security definer` functions keyed on `auth.uid()` (not shown in the reviewed migrations). (e) Supabase: set access-token lifetime to 1 h and keep refresh-token rotation + reuse detection on (default); `AuthClient` already clears the store on a rejected refresh.

## A10. Logging — current state and hygiene checklist
**Verified [repo]:** `agent-action` logs `("agent-action", res.status)`, task id + status, error code/status; `smart-action` logs route/model/ms and provider errors through `scrub()` (keys) + `logSafe()` (160 chars, structured fields for 400/422); `transcribe-proxy` logs structured JSON without audio/text. No `console.log` of request bodies anywhere reviewed.

**Never log:** `Authorization` header/JWT, refresh tokens, `apikey`, H key, webhook secret, OAuth `code`/tokens, `vault-action` request bodies (`values`), audio, transcripts, instructions, plans, page text/`steps`, screen frames or any data URL, e-mail addresses, H `agent_view_url`.
**Redaction:** one `_shared/log.ts` with `log(event: string, fields: Record<string, string|number|boolean>)` that allowlists field names, truncates strings to 120 chars, and scrubs all known secrets (`scrub()` exists in `smart-action` — move it); ban raw `console.*` in functions via CI grep. Catch blocks log `code` only, never `e.message` from upstream.
**Platform logs:** Supabase keeps edge logs per plan (Free 1 day, Pro 7 days) and records method/path/status/headers metadata — **S8 must confirm** the `Authorization` header and query strings are not retained. Deploy with `--no-verify-jwt` as today only because every function calls `auth.getUser()`; add a test that every function returns 401 without a token.
**Retention:** `agent_tasks` 30 days (pg_cron); `agent_task_runs.plan/tool_req` nulled at terminal; `vault_events` 1 year; H sessions `delete_after_min` 1440 (60 after a login), screenshots 60 (10 after a login); STT/LLM providers: zero retention where available.
**Key rotation:** H key — quarterly + on incident (`supabase secrets set`, redeploy); H webhook secret — H supports `rotate` with a dual-verify window [H doc], do it with the H key; Google client secret — yearly; `SUPABASE_SERVICE_ROLE_KEY`/JWT secret — on incident only (invalidates all sessions). Keep a `SECRETS.md` runbook listing each secret, where it lives (Supabase secrets only), who can read it, last rotation.
**Audit table design:**
```sql
create table public.vault_events (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users on delete cascade,
  login_id uuid,                 -- nullable: survives login deletion
  task_id uuid,
  event text not null check (event in ('added','release_requested','released','denied','expired','deleted','vault_created','vault_deleted','vault_delete_failed')),
  hostname text not null,
  at timestamptz not null default now()
);
alter table public.vault_events enable row level security;
create policy vault_events_owner_select on public.vault_events for select to authenticated using ((select auth.uid()) = user_id);
revoke all on public.vault_events from public, anon, authenticated, service_role;
grant select on public.vault_events to authenticated;
grant insert on public.vault_events to service_role;   -- append-only, like agent_task_starts
```
No value columns, no username, no H item ids in clear (store `sha256(h_vault_id)` if correlation is needed). Mirror the same for `security_events` (auth failures, off-plan navigation, release denied).

---

# B. Engineering review of the spec

## B1. Claim-by-claim check

| § | Claim | Status | Note |
|---|---|---|---|
| 0.2 / 2.1 | Custom keyboards have no microphone access | **Correct** | Apple App Extension Programming Guide (archived) says exactly this; no entitlement exists for third parties. |
| 0.3 / 2.2 C | `AudioRecordingIntent` (iOS 18) lets the app record from a Shortcut/Control/Action button in the background and requires a Live Activity | **Correct as quoted**; background mic runtime is the documented purpose but **S4 is the go/no-go** | [Apple doc appintents/audiorecordingintent]: "you must start a Live Activity… If you don't start a Live Activity, the audio recording stops." |
| 2.4 diagram | `openAppWhenRun = false` | **Wrong/deprecated** | `openAppWhenRun` is deprecated; use `static let supportedModes: IntentModes = .background` (iOS 18). Setting it `true` in an extension errors. [Apple doc appintents/appintent/openappwhenrun, supportedmodes] |
| 2.2 D′ | `Button(intent:)` in a Live Activity runs a `LiveActivityIntent`; cites `liveactivitystartingintent, iOS 16.1+` | **Partly wrong** | Interactive Live Activities are iOS 17+; `LiveActivityIntent` is the protocol to adopt. `LiveActivityStartingIntent` is a separate, newer protocol — verify its minimum OS before relying on it [spike, 10 min in Xcode]. |
| 2.2 | Back Tap / Action button start recording "without a visible hop" | **Optimistic** | Back Tap runs a **Shortcut**; iOS shows the Shortcuts banner/HUD while a background intent runs. Action button → a Control (iOS 18) or a Shortcut; Controls run their intent in the app process in the background [Apple doc widgetkit/creating-controls]. Expect a brief system HUD, not a silent start. Accept, document. |
| 2.2 E | Controls perform an App Intent "from Control Center, the Lock Screen, or by using the Action button" | **Correct** | Also: Controls can require device authentication before performing — use it for "Start listening" on the Lock Screen. |
| 2.2 C′ | A keyboard cannot run an App Intent in another process; only `shortcuts://` | **Correct** | And `shortcuts://run-shortcut` foregrounds Shortcuts. |
| 2.2 B / S3 | `LSApplicationWorkspace.openApplicationWithBundleID:` | Private API; works today on the owner's phone [repo] | App Store rejection; sideload only — spec says so. |
| 3.1 | `Activity.request` foreground only, except via `LiveActivityIntent`; 8 h + 4 h; 4 KB; push-to-start iOS 17.2 needs APNs | **Correct** | [Apple doc activitykit/…]. A Live Activity cannot access the network; updates come from the app process (needs runtime) or APNs — spec's "app alive during tasks" consequence is right. |
| 3.2 | "Gusan ready" Live Activity started by the intent/app as the island anchor | **Correct and necessary** | Starting it from the backgrounded app later is impossible; the design handles it. User can disable Live Activities per app (`ActivityAuthorizationError.denied`) — handle. |
| 3.x | `aps-environment` needs Push capability → paid team | **Correct** | [Apple doc bundleresources/entitlements/aps-environment]: value comes from the provisioning profile. |
| 4.1 | Only a broadcast upload extension sees other apps; cannot start programmatically; ~50 MB limit [unverified] | **Correct / plausible** | Limit not in docs; repo logs `os_proc_available_memory()` in Debug — good. Add to S7: does the broadcast survive screen lock and incoming calls on the owner's iOS? |
| 1.1 | Keyboard jetsam ~60–70 MB [unverified] | **Plausible, device/iOS dependent** | Not a design blocker: current keyboard + two 450 KB JPEG base64 strings is ≪ 30 MB. Cheap guard: log `os_proc_available_memory()` in the keyboard in Debug like `SampleHandler` does. |
| 5.3–5.5 | `UITextDocumentProxy`: `selectedText` (iOS 11+), per-character `adjustTextPosition`/`deleteBackward`; Gmail/WKWebView gives nil selection and empty context; Notes attributed text oddities | **Plausible; correctly flagged S7** | Add to the matrix: Messages, Safari URL bar, Telegram, Slack, Teams; and the documented fact that `documentContextBeforeInput` may return only a partial context (sentence/paragraph) — the Undo validator must tolerate truncation. |
| 7.1 | No API to read iCloud Keychain; AutoFill works in any app's form after Face ID | **Correct** | [Apple doc security/password-autofill]. R12 (SwiftUI `SecureField`) — verify in S10. |
| 7.2 | Reveal endpoint; any org member; 1000 items; vault bound at creation; text-mode read-only | **Correct** | See A2.1. Add: `fill_secret_at` is visual-only. |
| 6.2 | `idle_timeout_s`; H ends idle ≥ 120 s sessions at the concurrency limit (Free 3) | **Correct** | [H doc plans-and-limits]. "Continue" after an idle kill restarts the session and is billed again. |
| 6.2 | "H has no mobile-emulation flag" | **Correct** | Only `width`/`height` [H doc browser/configuration]. |
| 8.2 | EventKit has no read-only access; `EKEventEditViewController` needs no permission | **Correct** | [Apple doc eventkit/accessing-the-event-store]. |
| 10.3 | Free Apple ID: 7-day expiry, 10 App IDs / 7 days, 3 sideloaded apps | **Correct (Apple-documented limits for personal teams)** | App Groups do work on personal teams in Xcode; Sideloadly re-signing rewrites them (observed). Widget extension = +1 App ID, total 4 — correct. |
| 10.1 | Merge #13, #18 then rebase #19, #20 | **Correct**; the `SessionStore`/settings-key changes (`privacyMode`) touch all branches — do §9 removal only after all four are merged. |

## B2. Missing pieces
1. **Custom tools plumbing** in `agent-action` (declare tools on execute sessions, parked-call storage, `awaiting_tool_results` → `awaiting_login/awaiting_code`, idempotent results, `login_expired`/`otp_timeout`) — the spec names the states but not the code path that today refuses all tools.
2. **Vault cleanup sweeper** and `h_vault_id` on `agent_task_runs` (A2.4-G2).
3. **S9 decision** before P5: binding an empty vault at execute-session creation would remove the session swap, browser-profile persistence, and the "resume from step k" prompt.
4. **Off-plan navigation guard** and execute `max_steps` derived from the plan (A3.1).
5. **Release nonce + rate limit + CI "never log" test** for `vault-action` (A2.4-G3).
6. **Session pasteboard hardening** (A8) — should land with P1, it is a one-day change in GusanKit.
7. **`connection_secrets` / `oauth_states` tables** and Google token revocation (A6).
8. **Supabase RPC audit** (`is_pro`, `use_smart_action_quota`) — not in the reviewed migrations.
9. **Account deletion** must delete: H persistent vault/profile (if any), `connections`, Google revoke, `agent_tasks`, local keychain items; `delete-account` is mentioned but not specified.
10. **"Memory"** (P4) is undefined in the spec — remove from P4 or specify.
11. **Privacy text** listing third-party processors per feature (STT providers, LLM providers, H, Google).
12. **Provider pinning** for screen frames (A4-a).

## B3. Ordering and estimation risks
- **P5 "Vault 10 d" is optimistic**: custom tools (2 d) + vault-action with nonce/cleanup/tests (3 d) + session binding or swap (1–3 d, S9 decides) + iOS Vault tab with AutoFill form, QR TOTP, Face ID, audit UI (4 d) + review/S8 (1 d) ≈ **12–14 d**; S9 first saves 2–3 d.
- **P2 depends entirely on S4.** If `AudioRecordingIntent` does not give mic runtime on the sideloaded build, P2 collapses to the Live Activity button (S5) + ready window; budget the 8 d only after S4 passes.
- **P4 (8 d)** bundles Live Activity progress, confirm card, Sessions chat, continue/stop, memory; split: Live Activity + confirm (4 d) first, chat/continue (3 d) second, drop memory.
- **P1 (7 d)** should include the A8 pasteboard fix and the A3.3/A3.4 one-line server fixes (½ d).
- H plan: Free (3 concurrent) is fine for one user; the `idle` "continue" design needs Builder once there are ≥ 2 users.
- Google verification for sensitive scopes is only needed above 100 users; irrelevant for the owner build.

## B4. Spikes — what is truly necessary
| ID | Verdict | Why |
|---|---|---|
| S1 (background audio keep-alive while paused) | **Keep** | Decides the ready-window UX and R1; 2 h. |
| S2 (battery) | **Fold into S1** | Read Settings › Battery after the S1 run. |
| S3 (`LSApplicationWorkspace`) | **Demote to a 15-min check** | Already in the repo (`autoReturn`); just confirm on the owner's iOS version. |
| S4 (`AudioRecordingIntent` from Control Center/Action button/Back Tap, sideloaded) | **Essential — go/no-go for P2** | Also record whether a HUD/banner appears and whether mic permission must be pre-granted in the foreground. |
| S5 (Live Activity button → `LiveActivityIntent` + `AudioRecordingIntent`) | **Merge into S4** | Same throwaway build; it is the fallback if S4 fails. |
| S6 (Gladia latency) | **Not a device spike** | Run from CI/desktop against `transcribe-proxy`; keep as a measurement task. |
| S7 (host-app matrix) | **Necessary but not a spike** | It is P1 acceptance testing; do a 1-h Gmail-only pre-check to decide the insert-only fallback. |
| S8 (edge logs never contain bodies) | **Essential, cheap** | Extend: `Authorization` header and query strings (`callback?code=`) absent from `edge_logs`. |
| **S9 (new)** H: create empty vault → start visual session with `vault_id` → add item mid-session → agent fills on the matching page? Also: `fill_secret_at` absent in text mode; what `delete_screenshot_after_min` covers | **Essential before P5** | API-only, ~1 h, decides the session-swap design. |
| **S10 (new)** Password AutoFill key icon appears in Gusan's Add-login form (SwiftUI `SecureField` vs `UITextField`) on the owner's iOS | **Cheap, before P5** | R12; 30 min. |

## B5. Prioritised spec changes before implementation starts
1. **§7.4** Add S9 and the "bind empty ephemeral vault at `confirm`" variant as the default if S9 passes; describe the swap only as fallback (with the serialised cancel → persist → create ordering).
2. **§7.4/7.5** Specify `vault-action release` authorisation: `awaiting_login` + tool hostname = login hostname + one-time nonce + ≤ 3/task + body cap + never-log CI test; specify the cleanup sweeper and `h_vault_id` on `agent_task_runs`.
3. **§7.1** Keychain class `WhenPasscodeSetThisDeviceOnly` + `.biometryCurrentSet` + shared `LAContext`; explicit "no access group / no App Group / no pasteboard" invariant with a test; UI copy "import a copy of this login", not "connect".
4. **§7.4** Remove "always ask off / persistent vault" from v1 (owner decision D-new); `username_hint` only.
5. **§6.2/6.5** Specify the custom-tool path (`agent.tools` on execute sessions, validation, idempotency, `tool_req` storage) and the off-plan navigation kill switch + plan-derived `max_steps`.
6. **§1.5/§9** Session hand-off: no refresh token in the pasteboard, 15-min expiry, app re-publishes on Darwin request; `pasteboardFallback` default off when the App Group works. Land in P1.
7. **§6.3** Confirm for payment/send/delete requires `LAContext` in the app; keyboard card shows "Confirm in Gusan" for those kinds. Generic lock-screen notification body.
8. **§2.4** Replace `openAppWhenRun = false` with `supportedModes = .background`; note the Shortcuts/Controls HUD; fix the `LiveActivityStartingIntent` citation (iOS 17 interactivity, protocol availability to verify).
9. **§8.1** `connections` / `connection_secrets` / `oauth_states` schema with RLS as in A6; revoke on disconnect; S8 covers the callback URL.
10. **§5.1/§4.4** Name the STT/LLM providers per feature; pin screen frames to one zero-retention provider; frame caps 2 × 1 MiB in `agent-action`.
11. **§10.4** Add tests: 401 without token for every function; `stepText` strips query strings; pasteboard never stores `refreshToken`; `vault-action` never logs; `vault_events` is insert-only for `service_role`.
12. **§10.5** Re-estimate P5 (12–14 d), split P4, make P2 conditional on S4; add R13 "session token in team pasteboard on sideload" (closed by item 6) and R14 "ephemeral vault leaks on failure path" (closed by item 2).
13. **§11 (owner decisions)** Add: D-new1 ship persistent vault? (recommend no); D-new2 H org membership = owner only + quarterly key rotation; D-new3 which STT/LLM providers are acceptable for audio/screen content.

---

## Appendix — where to look
- `supabase/functions/agent-action/index.ts`: `sessionRequest()`, `instructions()`, `sync()` (custom-tool refusal, `stepText`), `expire()`, `verifyWebhook()`, `Deno.serve` (body read before auth), `confirm` branch (idempotency `${row.id}:execute`).
- `supabase/migrations/202611070001_agent_tasks.sql`, `202611070002_agent_task_phases.sql`: RLS, `agent_task_runs` service-role-only, `begin_agent_task`.
- `supabase/functions/smart-action/index.ts`: `onlyDataImages`, `scrub`, `logSafe`, final `console.log`.
- `supabase/functions/transcribe-proxy/index.ts`: `stt_fail`/`stt_ok` logging, size caps.
- `ios/GusanKit/Sources/GusanKit/SessionBackend.swift`: `KeychainSessionBackend` (access class, access group), `FileSessionBackend`, `NamedSessionPasteboard` (expiry only with `lifetime`).
- `ios/GusanKit/Sources/GusanKit/SessionStore.swift`: `usePasteboard()`, `writeShared`, `writeRefreshed`, voice pasteboard 600 s.
- `ios/GusanKit/Sources/GusanKit/AuthClient.swift`: publishable key, refresh/clear behaviour.
- `ios/Gusan/AgentCenter.swift`: notification categories (`.authenticationRequired`), notification body.
- `ios/GusanKeyboard/AgentKeyboard.swift`: Confirm/Cancel slots, `confirmArmDelay`.
- `ios/GusanScreen/SampleHandler.swift`, `ios/GusanKit/Sources/GusanKit/Screen/ScreenChannel.swift`: lifetimes, memory logging.
- H docs: vaults/overview, vaults/create-item, vaults/reveal-item, browser/configuration, browser/profiles, sessions/create, custom-tools, webhooks/overview, plans-and-limits.
- Apple docs: appintents/audiorecordingintent, appintents/liveactivityintent, appintents/appintent/supportedmodes, activitykit/displaying-live-data-with-live-activities, activitykit/activity/request(attributes:content:pushType:), widgetkit/creating-controls-to-perform-actions-across-the-system, security/password-autofill, eventkit/accessing-the-event-store, bundleresources/entitlements/aps-environment, uikit/uitextdocumentproxy.
