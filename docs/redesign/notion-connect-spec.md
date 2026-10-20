# Notion connect - the decisive spec

Status: decided 2026-10-20. Builders follow this file; where it is silent, pick the
simplest thing that matches the surrounding code. Nothing here is committed, pushed,
deployed or tested against the real Notion API yet.

The founder's words: "one very simple button to connect with Notion. Somehow they can
connect, then activate it so notes go to Notion automatically. Very intuitive, not
complicated." The old NotionDialog was unreadable (dark text on an OS-dark window,
because it never called `theme.apply`) and asked for an "integration secret" and a page
link. Both go away from the main path.

---------------------------------------------------------------------------------------

## A. FINAL DECISIONS (binding)

1. **Connection flow (OAuth, public Notion connection "Gusan").**
   Settings or Send-to-Notion opens one sheet. The user presses "Connect Notion". The app
   starts a one-shot listener on `127.0.0.1:<random port>`, opens the system browser at
   Notion's authorize URL, the user allows access, Notion redirects to our Supabase
   function, the function answers a bare 302 to the app's listener, the app sends the
   code to the same function to swap it for tokens (the function holds the client
   secret), then the app creates the "Gusan meetings" page in the user's workspace and
   shows the connected state. No secret, no page link, no copy and paste.

2. **Loopback redirect choice: one registered https redirect + a relay (not fixed
   localhost ports).**
   - Registered in Notion (the only redirect URI):
     `https://hftcelxzfoubheqeoool.supabase.co/functions/v1/notion-oauth`
   - The app puts `state = <nonce>.<port>` in the authorize URL (nonce =
     `secrets.token_urlsafe(18)`, port = the port its listener got from the OS).
   - The function's GET handler validates `state` against
     `^[A-Za-z0-9_-]{16,64}\.[0-9]{4,5}$` and port 1024..65535, then replies
     `302 Location: http://127.0.0.1:<port>/notion?code=..&state=..` (or
     `error=access_denied&state=..`). Host and path are fixed in the function, so it is
     not an open redirect. The reply is a bare 302 (Supabase rewrites HTML from
     `supabase.co` to text/plain, a 302 is unaffected). Headers: `Cache-Control:
     no-store`, `Referrer-Policy: no-referrer`.
   - Why not the other option (three fixed `http://localhost:5368x` URIs): Notion's docs
     are silent on localhost, one third-party doc says `localhost` only and not
     `127.0.0.1`, and matching is exact. A single https URI is the documented case,
     there is no port clash and no localhost-vs-127.0.0.1 doubt. The price is that the
     single-use code passes through Supabase's request log; the code is useless without
     the client secret and our function never logs query strings.

3. **Server piece: one edge function `supabase/functions/notion-oauth/index.ts`,
   `verify_jwt=false`, anonymous (Notion stays free for guests).**
   - `GET` = the relay above.
   - `POST` JSON = `{action:"exchange", code, redirect_uri}` | `{action:"refresh",
     refresh_token}` | `{action:"revoke", token}`. Adds HTTP Basic
     `NOTION_CLIENT_ID:NOTION_CLIENT_SECRET` (Supabase secrets set by the founder), calls
     `https://api.notion.com/v1/oauth/{token|revoke}`, returns only
     `{access_token, refresh_token ('' if none), workspace_id, workspace_name,
     workspace_icon, bot_id, duplicated_template_id}`. Never the owner's email.
   - Guards: `redirect_uri` must equal the one constant exactly (else 400
     `invalid_redirect`); body max 8 KB (413); per-IP in-memory limiter 15/min (429);
     POST only besides the relay GET (405); no CORS headers at all (only the desktop app
     calls it); every response `Cache-Control: no-store`; secrets missing -> 503
     `server_misconfigured`; Notion 400 -> 400 `invalid_grant`; Notion 401 -> 503
     `server_misconfigured`; other Notion error -> 502 `notion_failed`; timeout (10 s) ->
     504 `notion_unreachable`. Errors are `{error:"snake_case"}` like the other
     functions. `console.error` logs only `"notion-oauth", action, status`, never a body,
     code or token.
   - Stores nothing. No database, no Supabase client.

4. **Config keys** (all in `main.DEFAULT`, replace the stale "plain-config like
   mistral_api_key" comment):

   | key | type | default | where | meaning |
   |---|---|---|---|---|
   | `notion_api_key` | str | `""` | **OS keyring** (secret name `notion_access_token`) | OAuth access token OR a pasted own-integration secret. Name kept so existing setups keep working |
   | `notion_refresh_token` | str | `""` | **OS keyring** (secret name `notion_refresh_token`) | OAuth only |
   | `notion_auth_mode` | str | `""` | config.json | `""` / `"oauth"` / `"own"`. Empty + a key present reads as `"own"` (legacy), derived at read time, no schema bump |
   | `notion_workspace_name` | str | `""` | config.json | shown in the rail and the sheet; never telemetry |
   | `notion_parent_page` | str | `""` | config.json | the destination page id (dashed). Legacy pasted links still work (`extract_page_id`) |
   | `notion_parent_title` | str | `""` | config.json | display name of the destination |
   | `notion_hub_page` | str | `""` | config.json | id of the "Gusan meetings" page Gusan made (hub). Destination is "the hub" when `notion_parent_page == notion_hub_page` |
   | `notion_auto_send` | bool | `False` | config.json | the switch the user flips after connecting |
   | `notion_reconnect` | bool | `False` | config.json | set when Notion rejected the token for good; the rail then shows "Reconnect". Cleared on connect |
   | `notion_owner` | str | `""` | config.json | the Gusan account (`auth.user_id`) the connection was made under; `""` = a guest (see A10). Written by `main.py` only |

   Keyring wiring (main.py): a separate machine-wide table
   `_NOTION_SECRETS = {"notion_api_key": storage.NOTION_TOKEN_SECRET,
   "notion_refresh_token": storage.NOTION_REFRESH_SECRET}`. Do NOT add them to
   `_SECRET_CFG_KEYS` (that tuple also drives the per-account stash). Follow the existing
   `_store_secret` / `load_config` / `save_config` pattern, including blanking both keys
   in **every** sanitised `disk = {...}` dict in `load_config` (otherwise a plaintext
   token is rewritten to disk during another key's migration). A plaintext legacy token
   found on load migrates into the keyring. Without a keyring (Linux) the value stays in
   config.json, same fallback as the API keys. After a successful connect call
   `flush_config_save()` so the grant is in the keyring now. Do NOT add these keys to
   `api_keys` fields (`tests/test_api_keys.py:105` must still raise), to
   `_TRACKED_SETTINGS` (except `notion_auto_send`), or to telemetry.

5. **Destination logic.**
   - OAuth: right after the token exchange, in the same worker, run
     `notion_connect.choose_destination(token, template_id)`:
     (a) if the token response has `duplicated_template_id`, use it; else
     (b) create the hub: `POST /v1/pages` with
     `parent: {"type":"workspace","workspace":true}`, title "Gusan meetings", one intro
     paragraph ("Meeting notes sent from Gusan land here, one page per meeting."). If
     Notion answers 400 validation_error, retry once with `parent: {"workspace": true}`;
     else
     (c) fall back to `search_pages` and take the most recently edited page; else
     (d) nothing: show the "Share a page first" state (token held in memory, nothing
     saved until a destination exists).
   - The hub is created at connect time (not at first export) so the sheet can show the
     destination at once and the first auto-send cannot fail on setup.
   - Default destination = the hub ("Private page in your workspace"). The connected
     sheet lists the hub plus the pages the user shared (Notion search) and applies a
     change instantly.
   - Own integration (manual path): internal integrations cannot create workspace-level
     pages, so there is no hub: after the token validates, search lists the shared pages;
     first one preselected; none -> "Share a page first".
   - If a send returns 404 `object_not_found` and the destination is the hub, recreate
     the hub once silently, update `notion_hub_page` + `notion_parent_page`, retry once.
     For a page the user picked: error "Pick another page in Meeting settings." (no
     silent change).

6. **Auto-send behaviour** (the switch `notion_auto_send`, default off, flipped by the
   user in the connected sheet or in the rail).
   - Hook: `AppController._on_meeting_finished` in main.py, called first (right after
     `notify_history_changed()`, before the early returns), through
     `AppController._maybe_auto_send_notion(self, folder, notes, error)` wrapped in its
     own try/except and gated first on `cfg.get("notion_auto_send")` so the bare
     SimpleNamespace tests in `tests/test_services_wiring.py:544-582` stay green.
   - Sends when ALL hold: not quitting; no `error`; `notes.strip()` and a folder;
     `privacy_mode` off; connected; `meta.source != "live_assist"`; the session says
     these notes are the first ones (`MeetingSession.last_notes_new`, see C); meta has no
     `notion_url` yet. Rewrites, Live Assistant sessions and already-sent meetings are
     never auto-sent.
   - Content: the notes only (no transcript). Manual "Send to Notion" keeps notes +
     transcript as today. Why: sending a whole transcript off the computer
     automatically is a bigger privacy step than sending the notes.
   - Runs on a daemon thread (`notion_sync.send_meeting(..., auto=True)`), result back
     on the GUI thread via `self._run_on_gui`. Never uses `NotesView._bg`.
   - Result: success writes meta `notion_url`, `notion_page_id`, `notion_sent_at`,
     `notion_auto`, `notion_error=None`; failure writes only `notion_error=<code>`.
     Failure: one tray hint "Couldn't send to Notion" / "<meeting title> · Open"
     (whatever `notify_when_ready` says). Success: silent when Meetings is on screen,
     else tray hint "Sent to Notion" / "<title>" only when `notify_when_ready` is on.
   - Privacy mode on: nothing is sent, nobody is asked; meta `notion_error='privacy'`;
     the rail row says "Paused while Privacy mode is on". The switch keeps its value.
   - Process-wide in-flight set of folders so auto, a manual click and a rewrite cannot
     overlap.

7. **When OAuth is not configured yet** (`notion_connect.NOTION_CLIENT_ID == ""`, which
   is how it ships until the founder finishes the portal steps in D): the sheet opens
   directly on the manual path (state 4) with no Back arrow and no "Connect Notion"
   button. Nothing says "coming soon". The rest (picker, switch, rail, auto-send) is
   identical. When the constant is filled in, the one-button state appears with no other
   code change. `NOTION_CLIENT_ID` is public (it is visible in the browser address bar)
   and is a plain constant; the founder gives it, builders paste it.

8. **Notion-Version: `2026-03-11` for everything** (one constant in `notion_export.py`
   and one in the edge function). Why: the token, refresh and revoke references list it,
   and workspace-level page creation is documented only against it. None of its breaking
   changes (`after`->`position`, `archived`->`in_trash`, `transcription` block rename)
   touch our calls (pages under a page parent, blocks children append without `after`,
   search with filter `page`). Rollback if the first live test objects: change both
   constants to `2022-06-28`.

9. **Token handling.** The REST docs give no access-token lifetime and say the refresh
   token rotates. So: on a 401 with mode `oauth`, call refresh ONCE under a module lock
   (`notion_connect._LOCK`), keep the newest pair in memory (authoritative for the
   process) and persist both through a callback before retrying the request once. On
   `invalid_grant`, or no refresh token: set `notion_reconnect=True`, error code `auth`.
   Mode `own`: no refresh; a 401 means "Notion rejected the secret". Disconnect = forget
   locally at once (cfg + keyring), then `revoke` best effort on a worker; failures are
   ignored.

10. **Account scoping: the calendar's owner rule** (changed after review; it was
    "machine-wide, no owner key"). A connection belongs to the Gusan account that made
    it: `notion_owner` (a `main.DEFAULT` key, in `settings_apply.PROTECTED_KEYS`) is
    stamped with `auth.user_id` in `AppController.set_settings` the moment a fresh
    connection is saved (an update with both `notion_api_key` and `notion_auth_mode`;
    `""` = made as a guest). `AppController._reconcile_notion_owner`, called from
    `_on_auth_changed` right after the calendar's `set_owner`: another account signing in
    forgets the connection here (all `notion_export.BLANK_SETTINGS` keys, the switch off,
    `notion_owner` cleared, tokens out of the keyring, an OAuth grant revoked on a worker);
    a guest's connection (`notion_owner == ""`) is adopted by the account that signs in;
    signed out, offline at launch (no `user_id` yet) and "nothing connected" change
    nothing. Why: on a shared computer a second account must never see the first one's
    workspace pages, nor have its meetings auto-published into it. The tokens themselves
    stay in the machine-wide keyring table `_NOTION_SECRETS`, not in the per-account
    stash. A connection from an older build has no stamp: the first account that signs
    in adopts it, exactly like a guest's.

11. **Who may use it: everyone** (guest, free, Pro). No sign-in, no Pro tag, no upsell
    noise.

12. **Telemetry** (no text, ids, titles, names, URLs, error messages, ever):
    - reuse `meeting_exported {target:"notion", ok, auto, mode, reason}` (`reason` only
      when not ok: `privacy|auth|no_access|rate|offline|other`);
    - NEW `notion_connect_result {mode:"oauth"|"own", ok, reason:""|"cancelled"|
      "timeout"|"denied"|"offline"|"port_busy"|"expired"|"server"|"other"}`;
    - NEW `notion_disconnected {mode, reason:"user"|"expired"}`;
    - `notion_auto_send` added to `main._TRACKED_SETTINGS` (reported as `settings_saved`).
    The two new names go into `telemetry.ALLOWED_EVENTS` AND `allowedEvents` in
    `supabase/functions/transcribe-analytics/index.ts` (`tests/test_telemetry.py`
    compares them). Until the founder redeploys that function the server drops them,
    which is harmless.

### Where the investigators disagreed, and the pick (one line each)
- Redirect: https relay (notion-api) over fixed `localhost` ports (server-oauth): the only URI form Notion's docs show, no port clashes.
- Notion-Version: `2026-03-11` everywhere over a mix: one constant per side, documented for every call we make, one-line rollback.
- Keyring: keep the cfg name `notion_api_key` (legacy-safe) plus a second secret for the refresh token, instead of new access/refresh cfg names.
- Destination: hub by default + a picker in the connected state (the founder asked for a picker); no template page (it adds a consent-screen step).
- Hub creation at connect time, not first export: the destination is known when the sheet says "Connected".
- Scoping: the calendar-style owner pattern over machine-wide (review finding: a second
  account on a shared PC inherited the first one's workspace and auto-sent into it).
- Auto-send content: notes only, not the transcript.
- Client id: a constant, not a cfg override (it is public; testers edit one line).
- One function for relay + exchange + refresh + revoke: one deploy, one URL, one secret pair.
- Sheet frame: native title bar like `AuthSheet` (multi-state sheet), not the frameless `MiniPrompt` (yes/no questions only).
- Manual re-send: unchanged (always a new page, notes + transcript); only auto-send dedupes by `notion_url`.

---------------------------------------------------------------------------------------

## B. THE EXPERIENCE

### B0. Principles
- One sheet, one decision per screen, left-aligned. No centred icon + headline + bullet
  list, no wordy subtitles. Calm: no Pro colours, no upsell, no confetti.
- It is modelled on `ui/auth_sheet.py` (`AuthSheet`): a `QDialog` that calls
  `theme.apply(self)` first, native frame, fixed width, `QScrollArea` + body +
  `QStackedWidget`, an injectable `run=` for the worker, ignores a cancelled attempt's
  late answer, cancels the browser wait when the sheet closes.
- Every widget is a `ui.components` widget or an explicit-token `QLabel`. Never
  `QDialogButtonBox`, `QMessageBox`, `QInputDialog` or a bare `QLabel` (that is exactly
  what made the old dialog unreadable).
- Copy: short, plain, real names ("Notion", "Gusan"). No "integration secret", "OAuth",
  "token", "workspace id" in the main path (the manual path says "secret" once, because
  that is the word on Notion's screen).

### B1. Window and shared pieces
- `NotionSheet(QDialog)`, `W = 440`, width fixed at 440; height follows the current page
  (non-current stack pages get `QSizePolicy.Ignored` vertically, call `adjustSize()` after
  each state change), capped at 640, then the scroll area scrolls. `setMinimumWidth(360)`
  so a 125/150% desktop works. Modal (`exec()`); parent is `host.window()`.
- Window title "Notion". Native frame (so on a dark Windows the title bar is dark and the
  body stays the light Gusan gradient, exactly like the sign-in sheet).
- Body margins: left/right 32, top 28, bottom 24. Content width 376.
- Background: whatever `theme.apply` paints (`window-a #eaf0f8` -> `window-b #f4f7fb` ->
  `window-c #e7eef7`).
- **MarkTile** (48 x 48, top-left of every state, accessible name "Notion"): white
  (`white`) rounded square, radius 12, 1 px border `line2 #cbd5e1`, no shadow. Inside, a
  plain "N" monogram painted with `QPainter` (not Notion's logo file): polyline
  (7,20) (7,6) (19,20) (19,6) in a 26 x 26 box centred in the tile, pen `ink #0f172a`
  2.6 px, round caps and joins. Helper `notion_pixmap(size)` in `ui/notion_sheet.py` is
  reused at 16 px for the Settings row label.
- **Title** (every state): `text_label(..., 26, 700, "ink", display=True, ls=-0.52,
  wrap=True)`. Line height about 32 px. Gap tile -> title: 20 px.
- **Secondary text**: 14 px / 400, token `text2 #334155` (never `muted` or `faint` on the
  gradient, see the contrast table).
- **Primary button**: `Button(text, "primary", "xl", full=True)` = 52 px high, radius 10,
  16 px / 600, `blue #2563eb` fill, white text (5.17:1), hover `blue-h`, keyboard focus
  ring from the theme. Smaller primary: `"m"` (36 px) or `"l"` (44 px).
- **Links**: `auth_sheet.link_button(text, px, weight, color)`; blue links use `blue-tx
  #1d4ed8`; quiet links use `text2` and underline on hover.
- **Progress line** (waiting only): custom 4 px high widget, radius 2, track `line2`, a
  96 px `blue #2563eb` segment sliding left to right and back every 1.6 s (QTimer 33 ms,
  runs only while visible). No spinner glyph, nothing busy.
- Esc closes the sheet (cancelling any wait). Enter presses the primary button.
- Tab order follows reading order; every control has an accessible name.

### B2. State 1 - Not connected (the one-button screen)

Top to bottom (y from the sheet top):
```
28   MarkTile 48x48
20
96   Title: "Send your meeting notes to Notion."          26/700 ink, wraps to 2 lines (64 px)
26
186  [ Connect Notion ]                                    primary xl, full width, 52 px
12
250  Opens Notion in your browser.                         12.5/400 text2
32
298  Use a Notion secret instead                            link, 13/500 text2, hover underline
24   (bottom margin)                                       total height about 360 px
```
- Exact strings: title `Send your meeting notes to Notion.` / button `Connect Notion` /
  caption `Opens Notion in your browser.` / link `Use a Notion secret instead`.
- "Connect Notion" accessible name: "Connect Notion". Click -> state 2.
- If privacy mode is on, the first press asks once with `mini_prompt.ask(self, "Privacy
  mode is on", "This goes online. Connect anyway?", "Connect", "Not now",
  icon="incognito")`; "Not now" stays on this screen.
- An inline message (the errors of B6) shows between title and button: `text_label(...,
  13, 400, "red-tx", wrap=True)`, 14 px above the button, hidden when empty. After an
  error the button text becomes `Try again`.
- Reconnect variant (opened with `reason="reconnect"`): message `Notion needs to be
  connected again.` and the button reads `Connect Notion`.
- The link goes to state 4. Hidden when `notion_connect.configured()` is false (then the
  sheet opens on state 4 anyway, see A7).

### B3. State 2 - Waiting for the browser

```
28   MarkTile
20
96   Title: "Finish in your browser"                        26/700 ink, 1 line (32 px)
8
136  Allow access in Notion, then come back here.          14/400 text2
22
178  ===== progress line =====                              4 px, full width
22
204  Open Notion again                    [   Cancel   ]   link blue-tx 13.5/600 | secondary m 36
24                                                          total height about 264 px
```
- Starts as soon as the browser was asked to open (`connect()` calls `on_url(url)` once
  the listener is up). `Open Notion again` re-opens the same authorize URL (same state,
  same listener). `Cancel` sets the `threading.Event`; the listener stops within about a
  second; back to state 1 with no message; telemetry reason `cancelled`.
- The wait ends after 180 s: back to state 1 with the message `That took too long.
  Nothing was connected.` and the button `Try again`.
- Sub-state **Finishing** (code received, exchanging and creating the hub, usually 2 to 4
  s): title `Setting things up…`, body `Just a moment.`, link and Cancel hidden, progress line
  keeps moving. When it ends: write `notion_api_key`, `notion_refresh_token`,
  `notion_auth_mode="oauth"`, workspace name, destination keys, `notion_reconnect=False`
  in one `set_settings` call, `flush_config_save()`, show state 3, and bring the window
  to the front (`raise_()`, `activateWindow()`).
- The browser tab shows the landing page (B7).

### B4. State 3 - Connected

```
28   [MarkTile 48]  Aram's workspace            h2 18/600 ink, elided to 300 px
                    [ check  Connected ]        Chip("Connected", "ok", icon="check"), 22 px
22
100  +-------------------------------------------------------------+   WhiteCard (white, r12,
     | Send every meeting's notes automatically          [toggle]  |   1 px line border,
     | Right after each meeting.                                    |   padding 18 / 16)
     |--------------------------------------------------------------|   Divider, 14 above/below
     | Notes go to                                                  |
     | [ Gusan meetings (private page)            v ] [refresh]    |
     | To add pages, share them with Gusan in Notion.               |
     +-------------------------------------------------------------+
22
     Disconnect                                      [    Done    ]   quiet link | primary m 36
24                                                                    total about 370 px
```
- Left of the chip row: name line = `notion_workspace_name` (fallback `Notion`). Header
  column is vertically centred on the 48 px tile (name 22 px + 4 + chip 22).
- Row A: label `Send every meeting's notes automatically` (13.5/600, `text`, wraps to 2
  lines in about 270 px), hint `Right after each meeting.` (12/400 `muted`, 4.76:1 on
  white), `Toggle("Send every meeting's notes to Notion automatically", checked)` on the
  right. Flipping it writes `notion_auto_send` at once (instant-apply).
- Row B: label `Notes go to` (13.5/600 `text`). Picker = `QComboBox` styled by the theme
  (white, radius 8, 1 px `line2`, chevron), fixed height 36, full width minus the
  refresh button; `IconButton("refresh", "Refresh pages", size=34, framed=True)` beside
  it. Items: first the hub `Gusan meetings (private page)` (OAuth only), then the
  shared pages, newest edited first, up to 50, titles elided at about 40 characters, an
  emoji prefix when the page icon is an emoji. Picking writes
  `notion_parent_page`, `notion_parent_title` at once. Search runs on a worker when the
  state is shown and on refresh; while it runs the picker shows the saved title only.
  Hint `To add pages, share them with Gusan in Notion.` (12/400 `muted`). A "Search pages" field
  appears under the picker only when the list is full (the newest 50) or while a search is on;
  Privacy mode: opening Manage shows the saved page and "Paused while Privacy mode is on." until
  Refresh is pressed and confirmed.
- Footer: left `Disconnect` (quiet link, `text2`, 13/500); right `Done` (primary m, min
  width 96, closes the sheet). When the sheet was opened from "Send to Notion" on a
  meeting, the button reads `Send this meeting` and returns True to the caller (B8).
- Disconnect: forgets locally at once, revokes in the background, returns to state 1 with
  no message.
- A manual (own integration) connection looks the same, minus the hub item; the name line
  is the workspace name from Notion (`Notion` if it is not given).

### B5. State 4 - Manual path (own integration)

```
28   [<]   (IconButton chev_l "Back", 34 px; hidden when OAuth is not configured)
14
76   Title: "Use your own integration"                       26/700 ink, 1 line
22
     (1)  Create an integration in Notion.
          [ Open Notion integrations ]                       secondary s (30 px)
18
     (2)  Open the page for your notes, click ••• then Connections, and add your integration.
18
     (3)  Paste the integration's secret here.
          [ ntn_...                                  (eye) ] SecretField, 40 px, show/hide
20
     (error line)                                            red-tx 13/400, hidden when empty
6
     [ Connect ]                                             primary l 44, full width, disabled while empty
24                                                           total about 430 px
```
- Number badge: 24 px circle, `white` fill, 1 px `line2`, digit `ink` 12.5/700; text
  column 12 px to its right; step text 14/400 `text2`; 18 px between steps.
- `Open Notion integrations` opens `https://www.notion.so/my-integrations`.
- `SecretField` = `auth_sheet.PasswordField` subclass: label hidden, placeholder `Starts
  with ntn_ or secret_`, eye accessible names `Show secret` / `Hide secret`.
- No page-link field. `Connect` (button text becomes `Checking…`, disabled) -> worker:
  `validate_token` (`GET /v1/users/me`) -> `search_pages`. Token accepted and at least one
  page -> state 3 (first page preselected). Token accepted, no pages -> state 5. Token
  refused -> the red line above the button.
- Persistence for the manual path: nothing is written until a destination exists. The
  moment state 3 is shown, write token, mode `own`, workspace name and the preselected
  destination (so closing the sheet with the window button does not lose them). In state
  5 the token is held in memory only; `Cancel` discards it.

### B6. State 5 - "Share a page first" and the errors

**No pages shared yet** (own integration, or OAuth when hub creation failed and search is
empty). Same header (tile + title), title `Share a page first` (26/700), body 14/400
`text2`: `In Notion, open the page for your notes, click ••• then Connections, and add
Gusan. Then come back here.` (manual mode says `...add your integration.`). Buttons:
primary m `Open Notion` (opens `https://www.notion.so`), secondary m `Check again`
(runs `search_pages`; the first hit moves on to state 3), link `Cancel` (closes, nothing
saved).

**Errors** are one plain sentence in the red line of the state they happened in (state 1
for OAuth, state 4 for manual). No dialogs, no codes shown to the user.

| code | where | exact sentence |
|---|---|---|
| denied (access_denied in Notion) | 1 | `You cancelled in Notion. Nothing was connected.` |
| timeout (180 s) | 1 | `That took too long. Nothing was connected.` |
| offline (cannot reach Notion or our function) | 1 / 4 | `Can't reach Notion. Check your internet and try again.` |
| port_busy (listener could not bind) | 1 | `Couldn't start the connection on this computer. Close other Gusan windows and try again.` |
| expired (`invalid_grant` on exchange) | 1 | `That Notion sign-in expired. Try again.` |
| server (503 / 502 / 504 from our function, or an unreadable answer) | 1 | `Notion couldn't finish connecting. Try again in a minute.` |
| browser (`webbrowser.open` returned False; the listener is closed) | 1 | `Couldn't open your browser. Try again.` |
| token rejected (manual) | 4 | `Notion didn't accept that secret. Copy it again from your integration.` |
| reconnect needed (later 401 on a send) | 1 | `Notion needs to be connected again.` |
| other | 1 / 4 | `Couldn't connect to Notion. Try again.` |

Send-time errors (flash in the meeting) are in B8.

### B7. The browser landing page (loopback answer)

Replaces the near-black pages of `calendar_sync` / `auth`. Inline HTML, no external
requests, `<meta name="color-scheme" content="light dark">`, `font-family: "Segoe UI
Variable","Segoe UI",system-ui,sans-serif`. Left-aligned card, max width 420 px, 40 px
from the top, 28 px padding, radius 16, 1 px border, a 44 px tile with a check (or a
cross) as inline SVG. Text is honest in every outcome because the page is shown before
the exchange finishes:
- got a code: title `Almost done.` / line `Go back to Gusan to finish. You can close this tab.`
- error other than denied: title `Notion wasn't connected.` / line `Go back to Gusan and try again.`
- denied: title `Cancelled.` / line `Nothing was connected.`

| | light | dark (`prefers-color-scheme: dark`) |
|---|---|---|
| page background | gradient `#eaf0f8` -> `#f4f7fb` | `#0f172a` |
| card | `#ffffff`, border `#e2e8f0` | `#1e293b`, border `#334155` |
| title | `#0f172a` 15.6:1 | `#ffffff` 14.6:1 on the card |
| line | `#334155` 9.0:1 | `#cbd5e1` 9.9:1 |
| check tile | `#16a34a` on `#dcfce7` | `#4ade80` on `rgba(74,222,128,0.14)` |
| cross tile | `#b91c1c` on `#fee2e2` | `#f87171` on `rgba(248,113,113,0.14)` |

Body background and colour are set explicitly (never rely on the browser default).

### B8. Outside the sheet

**Settings > Meetings rail, row "Send notes to Notion"** (`MeetingRail`, ui/pages/meetings.py):
- Leading 16 px mark (`notion_pixmap(16)` via `_label_icon`, like the Google G).
- Disconnected: label `Send notes to Notion`, no hint, control `Connect` (secondary s,
  icon-free, accessible name "Connect Notion"). Click opens the sheet.
- Connected: hint = workspace name (e.g. `Aram's workspace`; `Connected` for a legacy
  setup with no name). Control = two links, `Manage` (blue-tx 12.5/600, opens the sheet
  on state 3) and `Disconnect` (muted tone, as today). Disconnect here is immediate, no
  confirmation (it is reversible).
- A second row appears only when connected: label `Send automatically`, hint `After
  every meeting` (or `Paused while Privacy mode is on` when privacy mode is on and the
  switch is on), `Toggle("Send every meeting's notes to Notion automatically")` bound to
  `notion_auto_send`.
- `notion_reconnect` true: hint `Reconnect Notion to keep sending.`, control button
  `Reconnect` (secondary s) in place of Manage; Disconnect stays.
- The rail has no "waiting" state: the wait lives in the sheet.

**Send to Notion on a meeting (Export menu, entry text unchanged "Notion"):**
1. Nothing to send -> flash `Nothing to send yet.` (amber), as now.
2. Not connected -> `self.page.connect_notion(then="Send this meeting")` opens the sheet.
   The user connects; state 3 shows the primary button `Send this meeting`; pressing it
   closes the sheet and the send continues in the same click. Closing the sheet any
   other way leaves Notion connected and sends nothing.
3. Privacy mode on -> `mini_prompt.ask(self, "Privacy mode is on", "Send this meeting to
   Notion anyway?", "Send", "Not now", icon="shield")`. Replaces the native
   `QMessageBox.question` (same dark-OS hazard).
4. Flash `Sending to Notion…` (muted), then on success flash `Sent to Notion` (ok) and
   open the new page in the browser (as today); the meeting's meta row gets a quiet
   `In Notion` link (`Button("In Notion", "link", "s")`, opens `meta.notion_url`).
5. Failure flashes (red, shown 5 s instead of 1.8 s; use a `ms` argument on `flash`):
   - auth, OAuth: `Couldn't send to Notion. Reconnect it in Meeting settings.` (also sets
     `notion_reconnect`)
   - auth, own: `Couldn't send to Notion. Notion rejected the secret.`
   - no access (chosen page): `Couldn't send to Notion. Pick another page in Meeting settings.`
   - rate: `Notion is busy. Try again in a minute.`
   - offline: `Couldn't reach Notion. Check your internet.`
   - other: `Couldn't send to Notion.`

**Auto-send notices:** tray hints exactly as in A6.

### B9. Colour choices, light and dark

The app has no dark palette in 2.0.0 (`SHOW_APP_LOOK = False`, `ui/theme.py` has light
tokens only). So "light and dark" for this sheet means: it must look identical when
Windows is in dark mode, because every pixel is painted by the Gusan stylesheet and
explicit tokens, never the OS palette. (The native title bar follows the OS; that is
expected.) The browser page is the one surface with a real light/dark pair (B7).

Contrast (WCAG ratio, computed from `ui/theme.py` TOKENS; the sheet sits on the window
gradient, whose three stops are `#eaf0f8`, `#f4f7fb`, `#e7eef7`):

| use | token | on gradient (worst stop) | on white |
|---|---|---|---|
| titles, name, digits | `ink #0f172a` | 15.3 | 17.9 |
| secondary lines, quiet links | `text2 #334155` | 8.9 | 10.4 |
| card labels | `text #1e293b` | 12.5 | 14.6 |
| blue links | `blue-tx #1d4ed8` | 5.7 | 6.7 |
| error line | `red-tx #b91c1c` | 5.5 | 6.5 |
| primary button text | white on `blue #2563eb` | 5.17 (hover `blue-h` 6.7) | - |
| "Connected" chip text | `ok-tx #15803d` on `ok-sub #dcfce7` | 4.57 | - |
| hints in the white card | `muted #64748b` | NOT allowed on the gradient | 4.76 |

Rules that follow: on the bare gradient never use `muted` (4.07 to 4.43), `faint` (2.2)
or `ok-tx` (4.29 to 4.38); use `text2`. `muted` hints only inside the white card.
Disabled button text (`faint` on `bg2`) is exempt as disabled. Add a Qt test that
asserts every token the sheet draws as text on the gradient is at least 4.5 against all
three stops (module constant `TEXT_ON_WINDOW` in `ui/notion_sheet.py`).

---------------------------------------------------------------------------------------

## C. FILES AND OWNERS

Owners (one builder each; only edit your own files; re-read a file right before editing
it; `main.py`, `ui/pages/meetings.py`, `ui/pages/meeting_views.py` already carry other
people's uncommitted changes - never reformat or revert them; never touch the retired
`ui/meetings.py`).

| owner | files |
|---|---|
| **K (core, Qt-free)** | `notion_connect.py` (new), `notion_sync.py` (new), `notion_export.py`, `storage.py` (two constants), `tests/test_notion_connect.py`, `tests/test_notion_sync.py`, `tests/test_notion_export.py`, `docs/redesign/notion-setup-for-founder.md` |
| **S (server)** | `supabase/functions/notion-oauth/index.ts` |
| **U (ui)** | `ui/notion_sheet.py` (new), `ui/pages/meetings.py`, `ui/pages/meeting_views.py`, `tests_qt/test_notion_sheet.py`, `tests_qt/test_page_meetings.py`, `tests_qt/test_no_old_name.py` |
| **W (wiring)** | `main.py`, `meeting_session.py`, `telemetry.py`, `supabase/functions/transcribe-analytics/index.ts`, `tests/test_core.py`, `tests/test_services_wiring.py`, `tests/test_telemetry.py` (run only) |

Order: K publishes the signatures first (stubs are enough); U and W code against them
with fakes.

### C1. `notion_connect.py` (Qt-free; stdlib + requests; no storage access, no Qt)

```python
NOTION_CLIENT_ID = ""        # public; founder's value goes here
FUNCTION_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/notion-oauth"
REDIRECT_URI = FUNCTION_URL  # the one redirect URI registered in Notion
AUTH_URL = "https://api.notion.com/v1/oauth/authorize"
API_BASE = "https://api.notion.com/v1"
LOGIN_TIMEOUT = 180
HTTP_TIMEOUT = 15
HUB_TITLE = "Gusan meetings"
HUB_INTRO = "Meeting notes sent from Gusan land here, one page per meeting."
INTEGRATIONS_URL = "https://www.notion.so/my-integrations"
NOTION_HOME_URL = "https://www.notion.so"

class ConnectError(RuntimeError):
    code: str   # cancelled|denied|timeout|offline|port_busy|expired|server|browser|other
    # str(e) is the exact sentence from B6 (MESSAGES[code])
class ConnectCancelled(ConnectError): ...

MESSAGES: dict[str, str]                         # code -> the B6 sentence

def configured(cfg=None) -> bool                 # bool(NOTION_CLIENT_ID.strip())
def build_state(nonce: str, port: int) -> str    # f"{nonce}.{port}"
def parse_state(state: str) -> tuple[str, int] | None   # same regex/range as the function
def authorize_url(client_id: str, state: str) -> str
def landing_html(kind: str) -> str               # kind: "ok" | "error" | "denied" (B7)

def connect(cfg=None, open_browser=webbrowser.open, timeout=LOGIN_TIMEOUT,
            cancel=None, on_url=None) -> dict:
    """Blocking, run on a worker. Binds 127.0.0.1:0 (exclusive on Windows, copy of
    calendar_sync._LoopbackServer), handler answers 404 to anything without code/error,
    400 unless state matches (secrets.compare_digest), serves landing_html, 1 s
    handle_request slices until deadline or cancel. on_url(url) is called once the
    listener is up (for "Open Notion again"). Then exchange(). Returns the token dict
    (keys as in A3) ; raises ConnectError / ConnectCancelled."""

def exchange(code: str, redirect_uri: str = REDIRECT_URI) -> dict   # POST {action:"exchange"}
def refresh(refresh_token: str) -> dict                             # POST {action:"refresh"}; serialised by _LOCK; keeps _LATEST
def revoke(token: str) -> None                                      # POST {action:"revoke"}, never raises

# Notion REST helpers (Bearer token, Notion-Version from notion_export)
def validate_token(token: str) -> dict                  # GET /users/me -> {"workspace_name": str}; raises notion_export.NotionAuthError
def search_pages(token: str, query: str = "", limit: int = 50) -> list[dict]
    # POST /search filter {property:"object",value:"page"}, sort last_edited_time desc,
    # follows next_cursor up to `limit`; drops in_trash/archived; each item
    # {"id": dashed, "title": str, "emoji": str, "url": str, "top": bool}
    # title = join plain_text of the property whose type == "title", else "Untitled"
def create_hub(token: str, title: str = HUB_TITLE) -> dict           # {"id","title","url"}; the A5 (b) body, with the 400 retry
def choose_destination(token: str, template_id: str = "") -> dict | None
    # A5 chain; {"id","title","kind":"hub"|"template"|"page"} or None
def page_alive(token: str, page_id: str) -> bool        # GET /pages/{id}, False when 404 or in_trash/archived
```
Nothing here reads or writes config or the keyring: the caller (sheet/main) persists.
`requests` errors map to `ConnectError(code="offline")` with the fixed sentence (details
go to the log, never into the text).

### C2. `notion_export.py` changes
- `NOTION_VERSION = "2026-03-11"`.
- `class NotionError(RuntimeError)` gains attributes `status: int | None`, `code: str`
  (`auth|no_access|rate|offline|other`) and `retry_after: float | None`, all defaulted so
  existing `NotionError("text")` calls still work.
  `class NotionAuthError(NotionError)` (401, code `auth`) and
  `class NotionNotFoundError(NotionError)` (404, code `no_access`).
- `_raise_for(resp, oauth=False)`: default wording unchanged (so the existing tests keep
  passing). With `oauth=True`: 401 -> `Notion needs to be connected again.`; 404 -> `Gusan
  can't reach that Notion page anymore.`; no mention of secret, token or Connections.
  429 -> code `rate` with `retry_after` from the header; 5xx/other as today.
- Network failure -> `NotionError("Couldn't reach Notion. Check your internet.")` with
  code `offline` (no exception text in the message).
- One shared `_request(method, url, token, **kw)` that does ONE retry on 429/529 when
  `Retry-After <= 20` seconds (`time.sleep`), calling `requests.post` / `requests.patch`
  by attribute so existing tests that patch `ne.requests.post` still work.
- `publish_meeting(token, parent_page, title, notes_md, transcript="", meta_line="", *,
  oauth=False)` keeps its signature and body shape (`parent: {"page_id": ...}`). The
  empty-token message stays for the legacy path.
- `is_connected(cfg) -> bool`: `bool(token.strip() and extract_page_id(parent))`; replaces
  the three inline copies (meetings.py x2, meeting_views.py). `connection_mode(cfg) ->
  "" | "oauth" | "own"` (A4 derivation).

### C3. `notion_sync.py` (Qt-free; shared by manual export and auto-send)

```python
def meta_line(meta: dict) -> str          # "when · duration · people · Gusan" (Qt-free, uses meeting_store.format_duration)
def friendly(exc: Exception, mode: str) -> tuple[str, str]   # (code, sentence from B8)

def send_meeting(folder, cfg, *, auto=False, persist=None,
                 publish=notion_export.publish_meeting,
                 refresh=notion_connect.refresh,
                 create_hub=notion_connect.create_hub,
                 update_meta=meeting_store.update_meta) -> dict:
    """-> {"ok": bool, "url": str, "code": str, "sentence": str}.
    Loads notes (+ transcript when not auto) and meta from meeting_store; in-flight guard
    (process-wide set of normalised folders; a second call returns code "busy");
    skips (ok False, code "sent") when auto and meta has notion_url; builds the title
    (meta title or "Meeting notes") and meta_line; publishes with oauth=(mode=="oauth");
    on NotionAuthError and mode oauth: refresh once under notion_connect._LOCK, call
    persist({"notion_api_key", "notion_refresh_token"}) BEFORE retrying, retry once; if
    refresh fails: persist({"notion_reconnect": True}), code "auth";
    on NotionNotFoundError and destination == hub: create_hub, persist({"notion_hub_page",
    "notion_parent_page", "notion_parent_title"}), retry once;
    writes meta via update_meta (A6). `persist` is called on the worker thread: the
    caller makes it hop to the GUI thread (main: self._run_on_gui(self.set_settings, d))."""
```

### C4. `supabase/functions/notion-oauth/index.ts`
Exactly the behaviour in A3. Header comment in the repo style (what it does, the
secrets `NOTION_CLIENT_ID` and `NOTION_CLIENT_SECRET`, "Deploy with JWT verification
DISABLED"). `ALLOWED_REDIRECT` = `${SUPABASE_URL}/functions/v1/notion-oauth` (from
`SUPABASE_URL`, fallback to the literal project URL). Notion-Version header
`2026-03-11`, 10 s `AbortSignal.timeout`. No CORS headers. Body parsed from
`req.text()` with the 8 KB cap before `JSON.parse` in a try/catch; `code` and tokens
must be strings of at most 2000 characters. State regex and port range as in A2.

### C5. `ui/notion_sheet.py` (new)
Module docstring in the `auth_sheet` style (usage, what it does, integration notes).
```python
TEXT_ON_WINDOW = ("ink", "text2", "blue-tx", "red-tx")     # asserted >= 4.5 by tests
def notion_pixmap(size: int) -> QPixmap                    # the N monogram tile (B1)
def mark_label(size: int = 16) -> QLabel                   # for the rail row

class NotionFlow(QObject):          # like AuthFlow: the workers, injectable run=
    waiting = Signal(bool); finishing = Signal(); connected = Signal(dict)
    failed = Signal(str, str)       # (code, sentence)
    def __init__(self, host, parent=None, run=None, connect_fn=None)   # connect_fn defaults to notion_connect.connect
    def start_oauth(self); def cancel(self); def reopen(self)
    def check_secret(self, secret: str)                     # validate_token + search_pages
    def load_pages(self)                                    # search_pages for the picker
    def disconnect(self)                                    # forget local + revoke on a worker
    # attempt counter ignores a cancelled attempt's late answer (as AuthFlow)

class NotionSheet(QDialog):
    result_sent = False             # True when the caller's `then` button was pressed
    def __init__(self, host, parent=None, run=None, then="", reason="")
    # pages: intro | waiting | connected | manual | share  (QStackedWidget)
    def done(self, r)               # cancels the wait when closed (as AuthSheet.done)
    def showEvent(self, e)          # winfit.settle_on_screen

def show_notion_sheet(host, then="", reason="") -> bool
    # one sheet at a time (second call raises the open one); returns True when the
    # `then` button was pressed (or, with then="", when Notion is connected on close).
def disconnect_notion(host) -> None  # cfg blank (6 keys incl. mode, workspace, parent, hub) + revoke + track
```
`host` is the `MeetingsPage` (it already has `cfg()`, `set_settings()`, `track()`,
`call()`). `run` defaults to `api_keys.run_then`; tests pass a synchronous stand-in
(`tests_qt/test_page_onboarding.py:21-30`). After connecting, `host.call("flush_config_save")`.

### C6. Exact edits - `ui/pages/meetings.py`
1. Module docstring (line about `notion_api_key` + `notion_parent_page`): describe the new
   keys in one sentence.
2. `_SETTING_KEYS` (about line 105): replace `"notion_api_key", "notion_parent_page"` by
   `"notion_api_key", "notion_parent_page", "notion_auth_mode", "notion_workspace_name",
   "notion_parent_title", "notion_auto_send", "notion_reconnect"`.
3. `connect_notion(self)` (about 583): becomes `def connect_notion(self, then="",
   reason=""): from ui import notion_sheet; return notion_sheet.show_notion_sheet(self,
   then=then, reason=reason)`. Still the single entry point pages use, still returns a bool.
4. Delete `class NotionDialog` (about 1915-1952) and any imports only it used
   (`QDialogButtonBox`, `QLineEdit` if nothing else needs them).
5. `MeetingRail.__init__` (about 2016-2031): keep `notion_connect` (secondary s, drop the
   `icon="notion"`, add `_label_icon(self.notion_row, notion_sheet.mark_label(16))`), keep
   `notion_disconnect`, add `notion_manage` (Button "Manage", link, s, muted-free blue,
   font 12.5/600, accessible name "Manage Notion") and `notion_reconnect` (Button
   "Reconnect", secondary s). Add a second `SettingRow("Send automatically", Toggle("Send
   every meeting's notes to Notion automatically"), hint="After every meeting")` stored
   as `self.notion_auto_row` / `self.notion_auto_toggle`, bound to
   `self.page.set_setting("notion_auto_send", on)`.
6. `sync_settings` (about 2069-2072): replace the inline `notion = bool(...)` with
   `notion_export.is_connected(cfg)`; set visibility of connect / manage / reconnect /
   disconnect and the auto row; `set_hint(name or "Connected")` or the reconnect /
   paused hints of B8; set the auto toggle with `blockSignals`.
7. Handlers (about 2210-2216): `_notion_connect` -> `self.page.connect_notion()`;
   `_notion_manage` -> same (the sheet opens on state 3 when connected);
   `_notion_disconnect` -> `notion_sheet.disconnect_notion(self.page)`.

### C7. Exact edits - `ui/pages/meeting_views.py`
1. `send_to_notion` (about 1968-2004): becomes
   - empty check as now;
   - `import notion_export, notion_sync`; if `not notion_export.is_connected(cfg)`:
     `if not self.page.connect_notion(then="Send this meeting"): return`; re-read `cfg`;
   - privacy: `mini_prompt.ask(...)` as in B8 (import `ui.components.mini_prompt`),
     replacing `QMessageBox.question`;
   - flash `Sending to Notion…`; worker: `notion_sync.send_meeting(self.folder, dict(cfg),
     auto=False, persist=<hop to GUI: self.page.call("set_settings", d) via a queued
     call>)` (the manual path sends notes + transcript from the folder, same as now);
   - `done(res, err)`: telemetry `meeting_exported {target:"notion", ok, auto:False,
     mode, reason}`; success flash `Sent to Notion` + `QDesktopServices.openUrl(url)`;
     failure `self.flash(res["sentence"], "red")`.
2. `flash(self, text, tone, ms=None)`: optional duration, red errors 5000 ms.
3. `render` meta row (about 1739-1748): when `meta.get("notion_url")` append a
   `Button("In Notion", "link", "s")` that opens it, after the language part, with the
   existing `·` separators.

### C8. Wiring - `main.py`, `meeting_session.py`, `telemetry.py`, analytics function
- `main.DEFAULT`: the nine keys of A4 (near the existing notion keys).
- `storage.py` (owner K): `NOTION_TOKEN_SECRET = "notion_access_token"`,
  `NOTION_REFRESH_SECRET = "notion_refresh_token"`.
- `main.py`: `_NOTION_SECRETS` table and its handling in `load_config` / `save_config`
  (A4); `AppController._maybe_auto_send_notion(self, folder, notes, error)` and its call
  in `_on_meeting_finished` (A6); `_notion_auto_done(self, folder, title, res)` (GUI
  thread: `notify_history_changed()`, tray hint, `notion_reconnect` when auth);
  `_TRACKED_SETTINGS += ("notion_auto_send",)`.
- `meeting_session.py`: in `_on_processing_finished`, before the `_final_notes = notes`
  assignment (about line 1517): `self.last_notes_new = not (self._final_notes or
  "").strip()`; initialise `self.last_notes_new = False` in `_setup`. True for a first
  run, notes a Quit left pending, and a retry after a failure; False for "Rewrite notes".
- `telemetry.ALLOWED_EVENTS` and the TS `allowedEvents`: add `notion_connect_result`,
  `notion_disconnected`.
- `settings_apply.py`: no change (no protected keys; any key is accepted).
- `api_keys.py`: no change.

### C9. Tests
New:
- `tests/test_notion_connect.py` (Qt-free, fakes only): state build/parse; authorize URL
  params (`client_id`, `response_type=code`, `owner=user`, `redirect_uri`, `state`);
  loopback capture with a fake `open_browser` that requests the listener with the right
  state (returns the code), a wrong state (400, keeps waiting), `access_denied`
  (`ConnectError.code == "denied"`), `cancel` (`ConnectCancelled`), `timeout=1`
  (`"timeout"`); `exchange`/`refresh`/`revoke` against a patched `requests.post`
  (success, `invalid_grant`, 503, offline); refresh serialised (two threads, one POST);
  `landing_html` has `prefers-color-scheme` and no `http` resource; `search_pages` title
  extraction (key named `title` and `Name`, empty -> `Untitled`, trashed dropped,
  pagination cap); `create_hub` body (workspace parent) and the 400 retry;
  `choose_destination` chain; `validate_token`.
- `tests/test_notion_sync.py`: success writes meta; failure writes `notion_error` code;
  auto skips when `notion_url` exists; refresh-then-retry persists the new pair before
  the retry; hub recreation on not-found; in-flight guard; `meta_line`.
- `tests_qt/test_notion_sheet.py`: state 1 content (one primary "Connect Notion", no
  visible `QLineEdit`), not configured opens state 4 without Back and without Connect,
  waiting page text and Cancel, finishing then connected page (name, toggle, picker),
  timeout and denied messages, manual: Connect disabled while empty, rejected secret
  message, no pages -> "Share a page first", Done writes keys, picker change writes
  `notion_parent_page`, toggle writes `notion_auto_send`, Disconnect blanks the keys,
  `then` button text and return value, `gusan == "window"` and `styleSheet() ==
  theme.stylesheet()`, visible controls all have accessible names (copy
  `_visible_controls` from `tests_qt/test_page_onboarding.py:196-210`), contrast of
  `TEXT_ON_WINDOW`, and palette independence (set a dark `QApplication` palette before
  building anything, grab the sheet, compare a few background pixels with the light
  render; the offscreen platform ignores `setColorScheme`).
Updated / extended:
- `tests/test_notion_export.py`: the existing 16 tests stay unchanged and must still
  pass (default wording is the legacy wording). Add: typed errors and `.code`, `oauth=True`
  wording (no "secret", no "Connections"), the 429 `Retry-After` retry, the fixed offline
  message, `NOTION_VERSION` in the headers, `is_connected`, `connection_mode`.
- `tests/test_core.py` (pattern at lines 273-288): `notion_api_key` and
  `notion_refresh_token` go to the keyring, config.json keeps `""`, a plaintext legacy
  token migrates, and neither is rewritten to disk by another secret's migration.
- `tests/test_services_wiring.py` (pattern at 544-582): auto-send hook sends once when
  everything holds, and does not send when the switch is off, on error, on a rewrite
  (`last_notes_new` False), when meta has `notion_url`, in privacy mode, for
  `live_assist`; sends even when `notify_when_ready` is off or Meetings is on screen.
- `tests_qt/test_page_meetings.py`: rail rows for disconnected, connected (workspace
  name as hint, Manage + Disconnect visible), reconnect, auto toggle writes the key,
  Disconnect blanks the keys, `connect_notion` opens the sheet (monkeypatch
  `notion_sheet.show_notion_sheet`). Line 675 (`exports == ["Word", "Markdown",
  "Notion", "Audio"]`) is unchanged: the menu text does not change.
- `tests_qt/test_no_old_name.py`: add the sheet (all five states) to the rendered-text
  scan; new modules are scanned for the old product name automatically, so no string
  may contain it.
- Run only: `tests/test_telemetry.py` (allow-list sync + call-site scan),
  `tests/test_api_keys.py` (line 105 must still raise),
  `tests/test_dictation_pipeline.py:130` (schema stays 3, no bump).

Existing tests that must change to pass: none are expected to break (the legacy wording
is the default). If a test does fail, fix the test only when the failure is about the
new behaviour described here.

### C10. Visual check (U)
Copy the probes in the scratchpad (`notion/probe_dark_palette.py`). Render every state
twice, once normally and once after `QApplication.setPalette(dark)` set before any widget
exists, parented to a real `MainWindow` so the inherited stylesheet is exercised; the two
renders must be pixel-identical in the body. Also check 125% and 150% scale, a very long
workspace name (elided) and a 40-character page title (elided). Compare with
`notion_old_darkpalette.png` (the old unreadable dialog).

---------------------------------------------------------------------------------------

## D. FOUNDER SETUP STEPS (write these, in plain language, into docs/redesign/notion-setup-for-founder.md)

Nothing here needs a code change from you; you do it in Notion and in the Supabase
dashboard. Never paste a secret into chat, a file in the repo or an email.

1. Open Notion's developer page: go to https://www.notion.so/my-integrations (it
   forwards to Notion's developer portal). Choose Build, then Public connections, then
   Create new connection.
2. Name it `Gusan`. Associated workspace: your own. **Installation scope: "Any
   workspace".** You cannot change this later, and "Selected workspaces only" would stop
   customers from connecting.
3. Fill in the company part with real values: logo = `assets/icon_512.png`, website =
   `https://gusan.aibuben.xyz` (or `https://aibuben.xyz` if you prefer), privacy policy =
   `https://aibuben.xyz/privacy`, terms = `https://aibuben.xyz/terms`, support email =
   `aibuben.xyz@gmail.com`.
4. Redirect URI: add exactly
   `https://hftcelxzfoubheqeoool.supabase.co/functions/v1/notion-oauth`
   and nothing else. If Notion refuses it, stop and tell Claude.
5. Capabilities: tick **Read content** and **Insert content** only. User information:
   "No user information". Leave the optional template URL empty.
6. Save. Open the Configuration tab and copy the **OAuth client ID** and the **client
   secret**.
7. In the Supabase dashboard (project `hftcelxzfoubheqeoool`) open Edge Functions, then
   Secrets, and add `NOTION_CLIENT_ID` and `NOTION_CLIENT_SECRET` with those two values.
8. Tell Claude the Client ID only (it is public) so it goes into
   `notion_connect.NOTION_CLIENT_ID`. Claude writes and tests the function and the app
   with fakes. When you say OK, deploy `notion-oauth` with JWT verification off, like
   the other functions. (Also redeploy `transcribe-analytics` so the two new Notion
   events are counted. Skipping it is harmless.)
9. First real test, on a dev build, with your own Notion: press Connect Notion, allow
   access, see "Gusan meetings" appear under Private in Notion's sidebar, turn on the
   switch, finish a meeting, see its page arrive, then Disconnect and check that Gusan
   is gone from Notion Settings > Connections.
10. Second real test with a Notion account from a **different workspace** (another
    email): this proves "Any workspace" works without Notion reviewing the connection.
    Try it in the browser you use on Windows and, if you can, Safari on a Mac.
11. A Notion Marketplace listing is optional and not needed (it adds a 5 to 10 business
    day review). Skip it.

If step 9 shows an error about the page parent, tell Claude: the fix is a one-line version
change (see A8).

---------------------------------------------------------------------------------------

## E. RISKS AND WHAT IS DELIBERATELY LEFT OUT

Risks (each has a fallback in the design):
1. **Workspace-level page creation is untested** with a real public-connection token, and
   not with `2026-03-11`. Fallback chain in A5 (retry body, then search, then "Share a
   page first"); version rollback in A8.
2. **Notion's redirect rules and review.** https redirect is the documented case;
   whether an unreviewed public connection works for other workspaces is not stated
   clearly (a portal caption mentions review). D10 tests it with a second workspace.
3. **Token lifetime and rotation are undocumented** for the REST OAuth. Refresh is built
   in v1, serialised, persisted before retry; `invalid_grant` -> "Reconnect". If tokens
   turn out permanent the refresh branch is dead but harmless.
4. **The consent screen with zero pages selected** is undocumented. With the hub, no
   page selection is needed, so it should not block.
5. **The code travels through Supabase's request log** (single-use, useless without the
   client secret); the function also sees the access/refresh token in transit during
   exchange and refresh and stores and logs nothing. The open endpoint can be hit by
   anyone (junk requests): exact redirect check, size cap and a per-isolate limiter
   (resets on cold start); move the counter to a table if abuse ever shows.
6. **A local process can read the loopback redirect** and exchange the code. Same trust
   level as the keyring, which it could read anyway; the state check stops drive-by
   web pages.
7. **Browsers or proxies that block the redirect to `127.0.0.1`** make the wait time out
   after 180 s with a friendly message. If it shows up, add a copy-the-link fallback.
8. **Shared computers**: a connection is stamped with the Gusan account that made it
   (A10) and another account signing in forgets it, so its meetings never land in the
   first person's Notion. What is left: two people sharing ONE Gusan account (or one
   guest session) share the connection, and a connection from before this rule has no
   stamp, so the first account that signs in adopts it. Signing out alone does not
   forget it (the calendar behaves the same): the next guest session on that account's
   computer still sends to that workspace until someone else signs in or it is
   disconnected.
9. **No keyring (Linux)**: the tokens stay in config.json (same as the API keys).
10. **Enterprise Notion workspaces** may restrict who can install connections; the user
    sees Notion's own page; our timeout message covers it.
11. **The Notion marks**: only a plain "N" monogram on a tile is drawn; no Notion logo
    file is shipped.
12. **Auto-send scope**: notes only, recorded meetings only, first notes only. A rewrite
    or a continued meeting does not update or resend; there is no "Send again" for an
    auto-sent meeting except the manual Export > Notion (which makes a new page).

Deliberately left out of this version:
- Marketplace listing, PKCE, a template page, the Notion MCP, databases and database
  properties, updating a page in place after a rewrite, a "Send again"/"Update in Notion"
  menu item, the transcript in auto-send, per-account scoping, the Privacy page "where
  your data goes" row for Notion, a tray-menu "Connect Notion" entry, refreshing the token
  at app start (only on a 401).
- The other native dialogs with the same dark-OS hazard (the recording consent
  `QMessageBox` in `ui/recording_prompts.py`, rename and delete in `meeting_views.py`):
  flag them to the founder, fix separately.
- The retired `ui/meetings.py` Notion dialog stays as it is.
- A dark Gusan theme (the app has none in 2.0.0); the sheet is correct under a dark OS,
  not themed dark.
