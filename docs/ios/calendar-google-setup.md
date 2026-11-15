# Google Calendar connection setup

This is separate incremental consent, not the Google/Supabase sign-in provider.
No Google credentials or refresh tokens are sent to the iPhone. Nothing is
deployed automatically by this PR.

## Google Cloud

1. Select or create the Google Cloud project that will own Gusan's Calendar
   integration. Enable **Google Calendar API** in APIs & Services → Library.
2. Configure Google Auth Platform / OAuth consent screen: app name, support
   email, developer contact, audience and required branding/privacy-policy URLs.
   Add `https://www.googleapis.com/auth/calendar.readonly` under Data Access.
   This is a **sensitive scope**. For a hackathon keep publishing status
   **Testing** and explicitly add every tester's Google account as a test user.
   Testing-mode refresh tokens for this scope generally expire after seven days;
   reconnect when requested. For public production access submit the required
   Google verification; do not assume that Supabase sign-in verification covers it.
3. Create an OAuth client of type **Web application**, not iOS. Add exactly:

   ```text
   https://<PROJECT_REF>.supabase.co/functions/v1/calendar
   ```

   Replace `<PROJECT_REF>` with the owner's actual Supabase project reference.
   This must equal `<SUPABASE_URL>/functions/v1/calendar`. There is no client-
   supplied redirect URI, localhost redirect or `gusan://` entry in Google Cloud.
   The server redirects to the fixed `gusan://calendar` app callback afterward.
   No JavaScript origin is needed for the native ASWebAuthenticationSession flow.

## Supabase

1. Apply both migrations, in order, through the normal reviewed DB workflow:
   `202611090001_calendar_connections.sql` and
   `202611090002_calendar_disconnect_cutoff.sql`. For a linked project, the
   equivalent CLI operation is `supabase db push`; inspect all pending migrations
   before running it. They create service-role-only RLS tables and RPCs.
2. Provision these Edge Function secrets through Supabase's secret manager:

   - `GOOGLE_CALENDAR_CLIENT_ID`: the Web OAuth client ID.
   - `GOOGLE_CALENDAR_CLIENT_SECRET`: its secret.
   - `CALENDAR_ENCRYPTION_KEY`: 64 hexadecimal characters representing **32
     cryptographically random bytes**, generated locally using a trusted CSPRNG.
     Keep this key stable and backed up in the owner's secret manager. Changing it
     makes existing encrypted tokens/verifiers unreadable; reconnect is required.

   `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are Supabase-provided runtime
   values. Never put service/Google/encryption keys in the app, repository, logs
   or shell history. Agent Vault uses an external per-task credential service,
   not Supabase Vault secret storage, so this integration uses AES-256-GCM with
   random nonces and user-bound authenticated data instead.
3. After configuring the above, the **owner** deploys:

   ```sh
   supabase functions deploy calendar --no-verify-jwt
   ```

   Gateway JWT verification must be OFF for Google's GET callback. POST still
   validates its bearer with `supabase.auth.getUser`. No CORS headers are exposed.
   No provider setup or deployment was performed during implementation.
4. Open Settings → Connections → Google Calendar → Connect on a signed-in iPhone.
   Grant Calendar read-only consent. Missing secrets produce “Not set up yet”.
   Rejected/expired Google refresh grants produce “Reconnect required”.

## Data lifecycle and operational limits

- Refresh tokens and PKCE verifiers are encrypted at rest. States are hashed,
  expire after ten minutes and are consumed by atomic DELETE RETURNING.
- Disconnect best-effort revokes the token and atomically records a persistent
  cutoff with deletion. A previously consumed callback cannot restore the
  connection: the locked save RPC rejects it and the function revokes its token.
- Deleting the auth user cascades through all three tables, so `delete-account`
  needs no additional database cleanup. Sign-out clears on-device snapshots and
  settings, not the independent server connection; Disconnect revokes that link.
- Schedule ordinary retention cleanup for expired `calendar_oauth_states`
  (indexed `expires_at`); a new consent attempt also replaces that user's states.
- Busy queries cover up to 14 days and 20 selected/visible calendars. Titles mode
  excludes freeBusyReader-only calendars, fetches with four workers and 250 events
  per page, and fails rather than silently reporting availability if pagination
  or the 2,000-block cap is exhausted. Busy-only mode never calls events.list.
- In-memory rate limits are per warm function isolate, matching notion-oauth's
  approach, not a global abuse quota. No event/token/email/code data is logged.
- Google OAuth redirect URLs contain a code/state. Keep platform request-log
  access restricted and configure platform URL/query redaction where supported.
  Application logs deliberately never record request URLs or provider bodies.

## API

All POST requests use the Gusan Supabase bearer token and JSON:

- `{"action":"google_start"}` → `{"url":"https://accounts.google.com/…"}`
- `{"action":"status"}` → `{"google":{"connected":true}}`
- `{"action":"busy","start":"2026-11-09T00:00:00Z","end":"2026-11-17T00:00:00Z","include_titles":false}`
  → `{"blocks":[{"start":"…","end":"…","all_day":false}]}`.
  Titles mode may add `title` (summary only); all-day dates use `YYYY-MM-DD` and
  are interpreted in the device timezone.
- `{"action":"disconnect"}` → `{"ok":true}`
- Revoked/expired grant → HTTP 409 `{"error":"google_reconnect"}`.
- Missing configuration → HTTP 503 `{"error":"google_not_configured"}`.

GET callback redirects only to `gusan://calendar?google=connected` or
`gusan://calendar?google=error&reason=denied|scope|exchange|config|cancelled`.
The app routes this host separately from sign-in and voice callbacks.
