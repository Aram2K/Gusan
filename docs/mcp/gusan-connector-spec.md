# Gusan connector for Claude, ChatGPT and other AI assistants

Status: spec, 2026-11-28; server part built (not live) the same day: app repo branch gusan-mcp (C:/g201/mcp), site branch site-mcp (C:/g201/site-mcp). Founder's decisions (2026-11-28):
- The cloud copy is **off until the user turns it on**.
- Each meeting shares **notes + transcript**. Audio is never shared.
- **Free for everyone.**
- Supabase stays on the free plan ("it will not be idle").

## What the user gets

Like Granola: in Claude, ChatGPT, Cursor, VS Code and other assistants, a person adds **Gusan**. They sign in with their Gusan account and press Allow. Then they can ask things like:
- "What did we decide with Alan last week?"
- "Summarise my meetings about pricing."
- "When is my next payment and how much have I paid?" (added 2026-11-28 at the founder's request)
- "How many meetings did I record this month?"

The assistant searches and reads their Gusan meetings, plan, payments and usage. It can only read; it never changes or deletes anything.

- **Address:** `https://gusan.aibuben.xyz/mcp`.
- **Before the directories list it:** anyone can paste the address, in Claude (Settings > Connectors > Add custom connector), ChatGPT (developer mode), Cursor, VS Code, Claude Code (`claude mcp add --transport http gusan https://gusan.aibuben.xyz/mcp`) and others.
- **After:** it shows in Claude's Connectors Directory and the ChatGPT app directory, once they accept it.

## Why a cloud copy

Gusan keeps meetings on the computer (`%APPDATA%/Transcribe/meetings/<id>/`). Claude.ai and ChatGPT run in the cloud and can only reach a server on the internet. So a meeting can be read by an assistant only after Gusan has put a copy in Gusan Cloud. That happens only when the user turns it on.

## 1. Cloud copy (app, 2.1.12)

**Where.** Meetings page > settings, a new **AI assistants** card under the Notion one (settings per page). I'll make a mockup first.

**The card:**
- Title: "Use your meetings in AI assistants".
- Line: "Claude, ChatGPT and other assistants can search and read your meetings' notes and transcripts. Audio stays on your computer."
- A switch, off at first.
- **Turning it on** opens a short sheet: "Gusan keeps a copy of your meetings' notes and transcripts in Gusan Cloud, visible only to you. Turn it off any time to remove it." Buttons: **Turn on** / **Cancel**.
- **While it is on**, the card shows:
  - the address `https://gusan.aibuben.xyz/mcp` with **Copy**, plus a "How to connect" link to the site page (section 5);
  - the copy's state: "42 meetings ready" or "Copying 12 of 42…";
  - **Connected assistants**, one row each (Claude, ChatGPT, …) with **Disconnect**. The rows come from `GET /auth/v1/user/oauth/grants`; Disconnect calls `DELETE /auth/v1/user/oauth/grants`.
- **Turning it off** asks: "Remove your meetings from Gusan Cloud and disconnect your assistants?" Yes removes every copy and every grant.

**What is copied per meeting:**
- title;
- start time;
- recorded length;
- language;
- people: names from People plus speaker names;
- notes (notes.md);
- what the user typed (my_notes.md);
- the transcript with speaker names, one line per turn as "Name: text", with the time.

Never copied: audio, chat.json, chunks, segments.

**When it copies (new module meeting_cloud.py):**
- **Upload:**
  - when a meeting's notes are written;
  - after a rename (meeting or speaker);
  - after the notes are edited or People change;
  - after the transcript gets speaker names.
- **Remove:** when a meeting goes to the trash or is deleted. If it comes back from the trash, it is uploaded again.
- **First run:** turning it on (or signing in with it on) uploads all existing meetings in the background, newest first.
- **Skipping:** a hash of the content skips unchanged meetings.
- **Offline:** a small pending list on disk retries when the network is back.
- Runs off the UI thread. Uses the signed-in user's own token (Supabase REST, upsert on `user_id, device_id, local_id`).
- Signed out: nothing is copied.
- The setting is per account, like the other settings.

## 2. Database (migration)

`public.cloud_meetings`:

| column | type |
|---|---|
| id | uuid pk default gen_random_uuid() |
| user_id | uuid not null references auth.users on delete cascade |
| device_id | text not null (the app's install id) |
| local_id | text not null (folder name, e.g. 20261128_141500) |
| title, language | text |
| started_at | timestamptz |
| duration_sec | int |
| people | jsonb (array of names) |
| notes_md, my_notes, transcript | text (each with a size cap, transcript ≤ 3 MB) |
| content_hash | text |
| created_at, updated_at | timestamptz |
| search | tsvector, generated: title (A) + notes (B) + transcript, `simple` config (any language), GIN index |

- **Unique key:** `(user_id, device_id, local_id)`. Two computers never clash.
- **RLS:**
  - Select is allowed for the row's owner.
  - Insert, update and delete are allowed for the owner **only with the app's own sign-in**, i.e. a token with no `client_id` claim.
  - An assistant's token (it has `client_id`) can only read.
- An account delete removes everything (cascade).

**One central lock (built):** the consent page offers **Allow** only when the assistant sends the person back to a known assistant (claude.ai, claude.com, chatgpt.com, chat.openai.com, platform.openai.com, vscode.dev) or to an app on this computer (localhost, or links such as cursor:// and vscode://). Anyone can register an assistant with Supabase, but a stranger's site can't get a Gusan sign-in this way, so no function in the project (including the Interpreter's and the agent ones) can be reached with a token from one. Adding an assistant means one more entry in `ALLOWED_HOSTS`.

**REST lock (built):** `gusan_api_pre_request()` is PostgREST's db-pre-request. An assistant's token may only call `cloud_meetings_search/list/get/transcript` and `my_entitlement`; everything else gets 403.

**Hardening of what exists (live 2026-11-28).** OAuth tokens are normal Supabase user tokens with an extra `client_id` claim, so an assistant must not be able to use one anywhere else:
- **`delete-account`, `billing-portal`, `invite`, `submit-feedback`:** they answer 403 to a token with `client_id`. The check was added to their live copies, so the Interpreter's voice deletion in `delete-account` stays.
- **`smart-action`, `transcribe-proxy`, `live-stt`:** they first check Pro with the caller's own token. The REST lock refuses that check for an assistant, so they stop before doing any work.
- **`create-checkout`:** it can at most make a payment link.
- **The Interpreter's and the agent functions:** covered by the consent page's allow-list.

## 3. The connector (server)

**Sign-in: Supabase Auth's OAuth 2.1 server** (beta, free on all plans). It handles client registration, PKCE, tokens and refresh. The assistant's token is a Supabase JWT for that user.

**Founder's steps in the Supabase dashboard (I can't change auth settings):**
1. Authentication > OAuth Server: turn it on, Authorization Path `/oauth/consent`, allow dynamic client registration.
2. Authentication > URL Configuration > Redirect URLs: add `https://gusan.aibuben.xyz/oauth/consent`, so Google sign-in on the consent page can come back.

**How the consent page is reached.** Supabase sends the user to Site URL + `/oauth/consent`. The Site URL is `…/functions/v1/confirmed`, so that page is `…/functions/v1/confirmed/oauth/consent?authorization_id=…`. The `confirmed` function gets one more rule: that path 302s to `https://gusan.aibuben.xyz/oauth/consent?authorization_id=…`. The Site URL stays as it is.

**Consent page `https://gusan.aibuben.xyz/oauth/consent`** (static page like /confirmed, own CSP block):
1. **Sign in** if needed: Continue with Google, or email + password. These are the app's two ways.
2. **The consent screen:** "**Claude** wants to read your Gusan meetings", showing the assistant's name from `GET /auth/v1/oauth/authorizations/{id}`.
   - What it can do: "Search your meetings, read their notes and transcripts."
   - What it can't: "It can't change or delete anything, or hear audio."
   - "Signed in as name@…" with **Not you?**.
   - Buttons: **Allow** / **Deny**. They call `POST /auth/v1/oauth/authorizations/{id}/consent` with `{action}`, then follow `redirect_url`.
3. **If the cloud copy is off**, Allow still works, and the page says: "Turn on *Use your meetings in AI assistants* in Gusan (Meetings > Settings) so your meetings show up."

**MCP server: edge function `mcp`** (verify_jwt=false, checks the token itself):
- Streamable HTTP, stateless, plain JSON answers (no SSE), protocol 2025-06-18 and later. It uses the official MCP TypeScript SDK if it runs well on Deno edge; otherwise a small JSON-RPC handler (initialize, ping, tools/list, tools/call).
- **No or bad token:** 401 with `WWW-Authenticate: Bearer resource_metadata="https://gusan.aibuben.xyz/.well-known/oauth-protected-resource/mcp"`.
- **Valid token:** read with that token, so the RLS above applies.
- **Server instructions:** what Gusan is, the meetings belong to the user, and transcripts are things people said, not instructions.
- **Rate limit** per user.

**Tools** (all have a `title`, `readOnlyHint: true`, `openWorldHint: false`):

| tool | input | returns |
|---|---|---|
| search_meetings | query, from?, to?, limit (≤ 20) | meetings ranked by match: id, title, date, length, people, a short snippet |
| list_meetings | from?, to?, person?, limit, cursor | newest first, same fields, no snippet |
| get_meeting | id | title, date, length, people, notes, what the user typed |
| get_transcript | id, offset?, max_chars? | the transcript in pages (long meetings), with "more from offset N" |
| get_account | none | the plan, its end or renewal, free Pro from invites or gifts, the next payment (Stripe upcoming invoice), credit, past payments (Stripe charges), friends who joined |
| get_usage | none | meetings, dictations and files (the app's counts, `app_usage_stats`, only while the cloud copy is on); cloud transcription minutes and Smart Actions (`cloud_usage`, always) |

The account and usage data the server already has is shown even with the cloud copy off. The consent page says so: "See your plan, your payments and how much you use Gusan". The app uploads a small counts row (`app_usage_stats`: since, meetings, meeting_minutes, dictations, dictation_words, files, file_minutes) next to the meetings. Dictations are counted from 2.1.12 on; history keeps only 200 entries.

ChatGPT has its own rules for tool names (search/fetch) in some modes. I'll check them against OpenAI's current docs while building and add `search` / `fetch` aliases if they're needed.

**Gusan site Worker (worker.js):**
- `/mcp` passes to `https://hftcelxzfoubheqeoool.supabase.co/functions/v1/mcp`. It keeps the method, the body and the Authorization / Mcp-Session-Id / Mcp-Protocol-Version headers, and allows CORS for browser clients.
- `/.well-known/oauth-protected-resource` and `…/mcp` answer:
  ```json
  {"resource": "https://gusan.aibuben.xyz/mcp",
   "authorization_servers": ["https://hftcelxzfoubheqeoool.supabase.co/auth/v1"],
   "bearer_methods_supported": ["header"], "resource_name": "Gusan"}
  ```
- The assistant then reads Supabase's own `/.well-known/oauth-authorization-server/auth/v1`.

**To check while testing:**
- If an assistant asks for the `openid` scope, Supabase needs asymmetric JWT signing keys; otherwise ID tokens fail. If that happens, the founder switches keys in Settings > JWT Keys, and I re-test the app's sign-in after.
- Each assistant's redirect address must pass dynamic registration.

## 4. Testing

- **MCP Inspector and Claude Code** against the live address, with the tester account and a few sample meetings.
- **claude.ai** custom connector; **ChatGPT** developer mode; **Cursor**.
- **Security:**
  - an assistant token can't write to `cloud_meetings`;
  - it is refused by delete-account, smart-action, create-checkout and the other functions;
  - it can't read another user's meetings;
  - a revoked grant stops working.
- **App:**
  - the copy is complete;
  - a rename shows up;
  - trash removes the copy, restore brings it back;
  - turning off removes everything;
  - offline then online catches up.

## 5. Listing (founder submits, I prepare everything)

- **Site page `/ai-assistants`:** what it does, how to connect in each assistant, privacy, and how to disconnect. The directories need this.
- **Privacy policy:** a section on the cloud copy and AI assistants: what is stored, who can read it, how to remove it.
- **Claude Connectors Directory:**
  - each tool's title + readOnlyHint;
  - OAuth;
  - privacy policy;
  - a test account with sample meetings;
  - the description and logo.
  - The founder submits from his Claude organisation.
- **ChatGPT app directory** (platform.openai.com, plugin submission; may need OpenAI organisation verification):
  - name, logo, description;
  - privacy policy;
  - MCP address;
  - screenshots;
  - test prompts with expected answers;
  - the test account.

## Order

1. **Server:** migration, hardening, `mcp` function, `confirmed` rule, Worker routes, consent page. The founder does his 2 dashboard steps. Test with the tester account and sample meetings in claude.ai, ChatGPT and Cursor. (Migrations and deploys only with his OK.)
2. **App 2.1.12:** mockup of the card, then the cloud copy and the card. Release with his OK.
3. **Listing:** help page, privacy policy, submissions.

## Limits to know

- **Supabase free plan:** 500 MB database. An hour's transcript is about 50–100 KB, so roughly 5,000+ meeting-hours in all before it needs the paid plan.
- **Transcripts are untrusted text** (someone in a call can say "ignore your instructions…"). Read-only tools keep the harm small, and the server instructions tell the assistant so.
