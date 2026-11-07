# Security model

This file is the threat model for Gusan (`Aram2K/Gusan`) and the aibuben sites (`Aram2K/aibuben`); the same file lives in both repositories. Devin security scans rerank their findings against it, so keep it current when trust boundaries, entry points or the owner's priorities change.

Scope: `Aram2K/Gusan` (desktop app + its Supabase backend) and `Aram2K/aibuben` (aibuben community site, its Supabase backend, and the Gusan marketing site in `transcribe-site/`). No separate infrastructure repository: the in-repo Supabase migrations/`config.toml` and `transcribe-site/wrangler.jsonc` are the infrastructure as code. This document was drafted from the code and the owner's answers (November 2026).

## Owner's answers (user-provided facts)
- **Assets:** all equally important — billing/Pro entitlements, users' audio/transcripts/meeting notes, server-held AI provider keys and cloud quota, aibuben member data, and integrity of the desktop app and its updates.
- **Attackers:** all of them matter — anonymous internet users, signed-in free users seeking Pro or quota bypass, aibuben writers (open sign-up), malicious content reaching the desktop app (documents, screens, meeting audio, prompt injection), and malware/other users on the user's computer.
- **Out of scope / accepted risk:** nothing ruled out.
- Consequence for triage: findings keep the scanner's severity unless the owner rules something out here.

## Components
| Component | Repo | Code |
|---|---|---|
| **Desktop App** | Gusan | `main.py`, `ui/`, `*.py` at root (recording, transcription, history/meeting stores, local LLM, doc_reader, hotkeys, Smart Actions client `action_api.py`) |
| **Desktop Integrations** | Gusan | `auth.py`, `calendar_sync.py`, `notion_connect.py`/`notion_*.py`, `api_keys.py`, `cloud_speech.py`, `cloud_files.py` — loopback OAuth listeners on 127.0.0.1, keyring, third-party APIs |
| **Updater & Distribution** | Gusan | `updater.py`, `installer/`, `build.ps1`, `.github/workflows`, `setup*.ps1`/`setup_mac.sh`, `autostart.py` |
| **Gusan Backend** | Gusan | `supabase/functions/*` (transcribe-proxy, smart-action, create-checkout, stripe-webhook, billing-portal, delete-account, notion-oauth, submit-feedback, transcribe-analytics, confirmed) and `supabase/migrations/*` (profiles, entitlements, quotas, `is_pro()`, analytics, feedback bucket) |
| **Community Site** | aibuben | `src/` (Vite/React Lovable app; blog, `src/pages/write/*` writer dashboard/admin panel, auth, `src/lib/blogDb.ts`), `vite-plugin-blog-prerender.ts` |
| **Community Backend** | aibuben | `supabase/functions/*` (mcp, sync-signup, bulk-sync-signups, member-locations, luma-participants, elevenlabs-sfx, email send/queue/unsubscribe/suppression) and `supabase/migrations/*` (RLS, `blog_posts_guard`, email infra), `supabase/config.toml` |
| **Gusan Website** | aibuben | `transcribe-site/` (static site + `worker.js` on Cloudflare Workers at gusan.aibuben.xyz, `public/_headers` CSP) |

## Trust boundaries and entry points
- **Internet → Gusan Backend.** Edge Functions deployed with `verify_jwt=false` that check the JWT themselves (`auth.getUser()`): transcribe-proxy, smart-action, create-checkout, billing-portal, delete-account, submit-feedback. Fully anonymous by design: `notion-oauth` (GET relay 302 to `http://127.0.0.1:<port>/notion`, POST exchange/refresh/revoke; per-IP limit, 8 KB cap, no CORS), `transcribe-analytics` (install-id keyed events, allowlist, 200/request), `stripe-webhook` (Stripe signature, `STRIPE_WEBHOOK_SECRET`), `confirmed` (static HTML).
- **Signed-in free user → Pro.** Pro is granted only by `stripe-webhook` writing `subscriptions` / `lifetime_purchases` with the service role; `is_pro()` excludes `trialing`. Free quotas: one cloud meeting note/month (`note_ref`), one Live Assistant session/month (`begin_live_session`), five Smart Action tries, daily abuse caps (3000 req/day on transcribe-proxy). Lifetime cap of 200 reserved in Postgres before checkout.
- **Internet → Community Backend.** `verify_jwt=false`: mcp (public, read-only community data, CORS `*`), sync-signup, bulk-sync-signups, member-locations, send-welcome-email, preview-transactional-email (gated by `LOVABLE_API_KEY`), handle-email-unsubscribe, handle-email-suppression. The browser talks to Supabase directly with the publishable key in `aibuben/.env`, so **RLS and triggers are the authorization layer**.
- **Writer → readers.** Anyone can sign up and write; markdown is rendered through `renderMarkdown(..., { untrusted: true })` (DOMPurify); publishing rights are enforced by the `blog_posts_guard` trigger, not the UI (AGENTS.md). Database posts override built-in posts by slug (`src/lib/blogDb.ts`).
- **Untrusted content → Desktop App.** Documents parsed by `doc_reader.py` (PDF/DOCX/XLSX/images, with decompression caps), audio/video files, screenshots and meeting audio fed to LLMs (prompt injection can steer Smart Actions / Live Assistant output that is typed at the cursor), Notion/Calendar data, model downloads (`model_manager.py`).
- **Local machine → Desktop App.** Single-instance IPC on `127.0.0.1:47823` (`main.py`), OAuth loopback listeners (`calendar_sync.py`, `notion_connect.py`, `auth.py`), data under `%APPDATA%\Transcribe` / `~/Library/Application Support/Transcribe`, API keys in the system keyring, autostart entries.
- **GitHub Releases → installed app.** `updater.py` downloads the latest release asset (`Gusan-Windows-Setup.exe` / legacy `TranscribeApp-Windows-Setup.exe`) and checks its `.sha256` published alongside it; builds are not code-signed. The old repo name `Aram2K/transcribe-app` redirects and must never be re-created (takeover risk for old installs).

## Assets
- Stripe billing and Pro entitlements (`subscriptions`, `lifetime_purchases`, `profiles.stripe_customer_id`, lifetime spot reservations).
- Server-held secrets: OpenAI/Anthropic/Mistral/Gemini/ElevenLabs/Modal keys, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `SUPABASE_SERVICE_ROLE_KEY`, Notion client secret, `GITHUB_TOKEN`, `LOVABLE_API_KEY`; and the cloud quota/cost they represent.
- Users' audio, transcripts, meeting notes, history, voice prints, screenshots (local), and text sent to cloud proxies.
- User accounts and PII: Gusan auth users, feedback (emails, screenshots in the public `feedback` bucket); aibuben members, sign-ups, emails, locations, email suppression list.
- Integrity of the desktop binary and update channel; integrity of the aibuben blog and the Gusan website (XSS, defacement).

## Existing controls (observed)
- Server-side JWT verification and `is_pro()` in every paid function; quotas in Postgres; service role only server-side.
- Stripe signature verification; plans resolved by `lookup_key`.
- `handle_new_user` RPC lock-down and no-card trial lock-down migrations; server-side admin.
- DOMPurify for writer content; `blog_posts_guard` trigger; CSP via `transcribe-site/public/_headers`.
- Notion relay: fixed host/path redirect, exact `redirect_uri`, per-IP limit.
- Updater SHA-256 check; localhost-only binds; keyring for API keys; Privacy mode disables cloud.
- Telemetry: anonymous install id, event allowlist, retention migration.

## Open questions
- Is the `feedback` storage bucket meant to be publicly readable (screenshots may contain personal data)?
- Who may call aibuben's `sync-signup`, `bulk-sync-signups` and `send-welcome-email` without a JWT, and how are they authenticated?
- Is the `.sha256` the only update integrity check (same release, same origin as the installer)?
