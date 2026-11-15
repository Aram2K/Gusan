# H live browser view and screenshots

Reviewed against the local H API documentation on 2026-11-08. No live H request
or unauthenticated URL-access test was performed.

## Exposed fields and endpoints

| Surface | Field / endpoint | Documented access and lifetime |
| --- | --- | --- |
| Session Agent View | `agent_view_url` from `GET /api/v2/sessions/{id}` | H Platform live/replay page. The docs do not promise signed, anonymous, or viewer-only access, or specify a URL TTL. Do not expose it as a client-safe capability without that assurance. |
| Live view event | `LiveViewUrlEvent.data.live_view_url` in `/sessions/{id}/changes` or `/sessions/{id}/events` | An event indicating that a live view URL is available. Its authentication, permissions, and TTL are not documented in the reviewed sources. A URL's presence alone is not evidence that it is safe to share. |
| Observation screenshot | `AgentEvent.data.kind = observation_event`, `data.image = {type: "url", source, media_type}` | The event-object docs explicitly say to fetch `source` with the API key. `type: "base64"` is also possible. Visual browser observations (`type: "web"`) have screenshots; text-mode observations (`type: "textual_web"`) do not. |
| Screenshot download | `GET /api/v2/sessions/{id}/resources/{bucket}/{key}`, with bucket `screenshots` | Authenticated for private sessions; returns a `302` to a presigned S3 URL. No API key is needed for shared sessions. The presigned URL TTL is not specified in these docs. |
| Attached file download | `GET /api/v2/files/{file_id}` from `AttachmentEvent.data.file_id` | Same authentication/redirect behavior as resource downloads; it is not the browser screenshot stream. |

Sources: `agents-api_sessions_retrieve.md`, `agents-api_sessions_event-object.md`,
`agents-api_sessions_resources.md`, `agents-api_observe-and-steer.md` under the
local H docs directory, plus this directory's `h_sessions_create.md` and
`h_browser_configuration.md`.

## Retention is not signed-URL expiry

`delete_screenshot_after_min` specifies minutes **after session completion**
before screenshots are deleted. Omitted/null retains them with the session.
`delete_after_min` controls deletion of the session and all its events/resources.
These settings do not document how long an individual presigned URL lasts.

The current agent-action session request uses `delete_screenshot_after_min: 60`
for ordinary sessions and `10` for Vault-enabled sessions. Session deletion is
`1440` and `60` minutes respectively. Thus "about 60 minutes" is the ordinary
screenshot retention setting, not a verified live-view or signed-URL TTL.

## Decision and options

No `live_url`, `live_images`, public column, or migration is added. The docs do
not establish a safe live-view capability, and raw screenshot event URLs need
the server-held H API key. Never send that key to the client or turn a private
session public merely to make these URLs work.

Options requiring a separate decision:

1. Obtain H's viewer-only/signed-live-URL contract, including permissions,
   revocation, and TTL; then persist a verified URL and expose it only for
   owner-scoped, nonterminal task responses.
2. Resolve screenshot resource redirects server-side with the API key and
   expose only validated, short-lived HTTPS presigned targets. Confirm TTL,
   redirect-host validation, and privacy policy first; never return the
   authenticated endpoint or an Authorization header. This is a capability
   exchange, not an API-key proxy.
3. Copy selected screenshots to owner-scoped private storage with independently
   expiring signed URLs and deletion guarantees. This introduces storage,
   retention, and authorization work outside the current change.

Any future implementation should keep the latest one to three preview images
separate from `result.images`, clear live capability fields on terminal tasks,
and test ownership, Privacy Mode, expiry, and HTTPS filtering.
