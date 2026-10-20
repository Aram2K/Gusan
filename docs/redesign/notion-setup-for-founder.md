# Notion connection: what you do once

This is the one-time setup behind the "Connect Notion" button. It takes about
10 minutes. You do two things (Notion, then Supabase), then tell Claude one
value. Claude does the rest.

Never paste the Notion secret into chat, a file in the repo, a note or an email.
It goes into the Supabase dashboard and nowhere else.

## In Notion

1. Open https://www.notion.so/my-integrations. It forwards to Notion's developer
   portal. Create a new connection and choose **Public**.
   (Notion may word the buttons a little differently. The values below are what
   matter.)

2. Fill in the basics:

   | Field | Enter exactly |
   |---|---|
   | Name | `Gusan` |
   | Associated workspace | your own |
   | Installation scope | **Any workspace** |

   Pick **Any workspace** with care: it cannot be changed later. "Selected
   workspaces only" would stop every customer from connecting.

3. Fill in the company part:

   | Field | Enter exactly |
   |---|---|
   | Logo | `assets/icon_512.png` from the app folder |
   | Website | `https://gusan.aibuben.xyz` |
   | Privacy policy | `https://aibuben.xyz/privacy` |
   | Terms of use | `https://aibuben.xyz/terms` |
   | Support email | `aibuben.xyz@gmail.com` |

4. Redirect URI. Add this one and nothing else:

   `https://hftcelxzfoubheqeoool.supabase.co/functions/v1/notion-oauth`

   Copy it whole: it starts with `https://`, has no space and no `/` at the end.
   If Notion refuses it, stop and tell Claude.

5. Capabilities: tick **Read content** and **Insert content**. Nothing else.
   User information: **No user information**. Leave the optional template URL
   empty.

6. Save. Open the **Configuration** tab. You will see an **OAuth client ID** and
   an **OAuth client secret**. Keep this tab open for the next step.

   If Notion says the connection must be reviewed before other people can use
   it, do not start a review and do not make a Marketplace listing. Stop and
   tell Claude what the screen says.

## In Supabase

7. Open the secrets page of the Gusan project:
   https://supabase.com/dashboard/project/hftcelxzfoubheqeoool/functions/secrets
   (Dashboard, project `hftcelxzfoubheqeoool`, Edge Functions, Secrets.)
   Add two secrets. Copy the values from Notion's Configuration tab. No quotes,
   no spaces before or after.

   | Name (capitals, exactly) | Value |
   |---|---|
   | `NOTION_CLIENT_ID` | the OAuth client ID |
   | `NOTION_CLIENT_SECRET` | the OAuth client secret |

   Save. Adding a secret is not a deploy: nothing changes for users yet.

## Tell Claude

8. Send Claude the **client ID only**. It is public (it shows in the browser
   address bar when anyone connects), so it is fine to type in chat. Do not send
   the secret. Claude puts the ID into `notion_connect.NOTION_CLIENT_ID`.
   Until then the app shows the "use your own integration" screen instead of
   the one-button screen, so nothing looks half-finished.

9. When the app side is tested, say OK. Claude then deploys `notion-oauth` with
   JWT verification off, like your other functions. It also redeploys
   `transcribe-analytics` so the two new Notion events are counted (skipping that
   is harmless). Nothing is deployed before your OK.

   Quick check after the deploy: opening
   `https://hftcelxzfoubheqeoool.supabase.co/functions/v1/notion-oauth` in a
   browser shows `{"error":"invalid_state"}`. That is the right answer.

## Test it twice

10. On a dev build, with your own Notion: press **Connect Notion**, allow access,
    and see **Gusan meetings** appear under Private in Notion's sidebar. Turn on
    the automatic switch, finish a meeting, and see its page arrive. Then press
    Disconnect and check that Gusan is gone from Notion, Settings, Connections.

11. Second test with a Notion account from a **different workspace** (another
    email). This proves "Any workspace" works for customers without a review.
    Use the browser you normally use on Windows and, if you can, Safari on a Mac.

A Notion Marketplace listing is optional and not needed. It adds a 5 to 10
business day review. Skip it.

## If something looks wrong

| What you see | What it means | What to do |
|---|---|---|
| Notion's page complains about the redirect | The address in step 4 is not exactly the one above | Fix it in Notion, then try again |
| After Allow, the browser shows "site can't be reached" | The function is not deployed yet, or the Supabase project is paused (the free plan pauses when idle) | Open the Supabase dashboard and restore the project, or ask Claude to deploy |
| Gusan says "Notion couldn't finish connecting. Try again in a minute." | Usually the two secrets in step 7 are missing or have a typo. Sometimes Notion itself is down | Check the names and values, then try again |
| Gusan says "That Notion sign-in expired. Try again." every time | Notion refused the sign-in code | Tell Claude |
| Connected, but "Gusan meetings" does not appear, or the app says "Share a page first" | Notion did not accept creating the page | Tell Claude: the fix is a one-line version change |
| It works for you but not for other people | The installation scope was "Selected workspaces only" | It cannot be changed: create a new connection with **Any workspace**, then repeat steps 4 to 8 with its new ID and secret |

If the secret is ever exposed, create a new one in Notion's Configuration tab,
put it into the `NOTION_CLIENT_SECRET` secret in Supabase, and tell Claude.
