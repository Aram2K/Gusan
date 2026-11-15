> ## Documentation Index
> Fetch the complete documentation index at: https://hub.hcompany.ai/llms.txt
> Use this file to discover all available pages before exploring further.

> ## Agent Instructions
> H Platform has three products: the Agents API (managed computer-use agents, base URL https://agp.eu.hcompany.ai/api/v2 or https://agp.hcompany.ai/api/v2 for the US), the Models API (OpenAI-compatible Holo vision-language models at https://api.hcompany.ai/v1), and HoloTab (a free no-code Chrome extension that runs Holo in the user's browser, with recordable routines and schedules). The `holo` CLI (`pip install "hai-agents[cli]"`) runs Agents API agents from a terminal, drives the user's own desktop with `-a h/holo-desktop`, and serves coding agents through `holo mcp`.
> Authenticate with a bearer API key from the HAI_API_KEY environment variable. SDKs: `pip install hai-agents` (Python, `from hai_agents import Client`) and `npm install hai-agents` (TypeScript, `import { HaiAgentsClient } from "hai-agents"`). CLI: `holo`.
> Agents do work in a browser or on a desktop; describe the task as an imperative instruction. To run a task quickly, prefer the pre-built agent `h/web-surfer-flash`. Read results from the session's `latest_answer` after it reaches a terminal status.
> Sessions are the unit of work; wait for a terminal status (completed, failed, timed_out, interrupted) before reading the answer. Use webhooks or the `changes` long-poll endpoint to follow progress.

# Create a session

> Run an agent on a task.

## Behavior

With a free slot, the session starts `pending` and moves to `running` once the agent launches. Above your [concurrency limit](https://hub.hcompany.ai/agents-api/plans-and-limits.md#concurrent-sessions) it is accepted as `queued` and starts when a slot frees up (set [`queue: false`](#param-queue) to get a `429` instead).

## Headers

**Idempotency-Key** `string`

Optional idempotency key (1 to 255 characters). Safe retry within 24 hours: the same key returns the original session. Reusing it with a different body returns `422`.

## Request body

**agent** `string | object` *required*

A catalog identifier (for example `"h/web-surfer-flash"`) or an inline [agent](https://hub.hcompany.ai/agents-api/agents/create.md#request-body), which follows the same rules: `environments` is required (at most one per kind) and may be `[]` only with `subagents` or custom `tools`. The session reads environments, skills, and subagents from the agent.

```json theme={"system"}
"agent": "h/web-surfer-flash"
```

A catalog ID runs the agent's latest [revision](https://hub.hcompany.ai/agents-api/agents/overview.md#revisions). Append `:N` to run a fixed one, for example `"agent": "my-research-bot:3"`.

```json theme={"system"}
"agent": {
  "name": "weather-agent",
  "description": "Looks up weather by city",
  "environments": [
    {
      "id": "browser",
      "kind": "web",
      "mode": {"type": "visual", "width": 1280, "height": 720},
      "start_url": "https://www.bing.com/"
    }
  ]
}
```

**messages** `string | object | object[]`

The initial task, queued before the agent's first step: a plain string (one user message), one message object, or a list.

```json theme={"system"}
"messages": [
  {"type": "user_message", "message": "Book a flight from Paris to Tokyo on June 15"}
]
```

**type** `string`

`"user_message"`, the default.

**message** `string` *required*

The instruction or task.

**images** `string[]`

Base64 data URIs to attach (for example `data:image/png;base64,...`).

**files** `object[]`

Files to attach, up to 10 and 7 MB in total. Each is `{ name, source, type: "base64", media_type? }` (`media_type` is guessed if omitted). Saved under `files/` in the agent's working directory on a desktop with `shell: true`, and `~/Downloads` on a cloud browser. Other environments can't receive files.

**caller\_id** `string`

The sender. Defaults to `user`. Leave it unset for normal user input.

**skills** `object[]`

[Skills for this message only](https://hub.hcompany.ai/agents-api/sessions/send-messages.md#param-skills), as inline [skill objects](https://hub.hcompany.ai/agents-api/skills/create.md#request-body).

**timezone** `string | null`

IANA timezone the agent uses for dates and times, for example `"Europe/Paris"`. `null` or omitted means UTC. An unknown name returns `422`. Child sessions a [manager](https://hub.hcompany.ai/agents-api/multi-agent.md) spawns inherit it.

**max\_steps** `integer`

Cap on steps, where each step is one decide-and-act cycle. Minimum `1`. At the cap the agent is asked for a final answer from what it has so far (it is not hard-killed). Omit to run uncapped.

**max\_time\_s** `number`

Cap on wall-clock seconds per answer, greater than `0`. Idle time between answers doesn't count. At the cap, as with `max_steps`, the agent is asked for a final answer. If it is still running 60 seconds later, the session ends `failed`, not `timed_out`, with `error_code` `timeout`. Omit to run uncapped.

**idle\_timeout\_s** `integer`

Switches between one-shot and interactive. Leave it `null` for a one-shot task: the session ends when the agent answers. Set it (seconds, greater than `0`) to keep the session open for follow-up [messages](https://hub.hcompany.ai/agents-api/sessions/send-messages.md): after each answer the session goes [`idle`](https://hub.hcompany.ai/agents-api/sessions/overview#lifecycle) and waits this long for your next message. At your concurrency limit, a session idle for 120 seconds can end sooner to [make room](https://hub.hcompany.ai/agents-api/plans-and-limits.md#idle-sessions-make-room) for your next one.

**delete\_after\_min** `integer | null`

Minutes after the session finishes before it is deleted, with its events and screenshots. Minimum `1`. Omit it to keep the session for the retention period in your privacy settings. A value deletes it sooner. `null` adds no expiry beyond that period.

**delete\_screenshot\_after\_min** `integer | null`

Minutes after the session finishes before its screenshots are deleted, to expire visual data sooner than the session. Minimum `1`. Omit it or pass `null` to keep screenshots as long as the session.

**queue** `boolean` (default: true)

At your [concurrency limit](https://hub.hcompany.ai/agents-api/plans-and-limits.md#concurrent-sessions), accept the session as [`queued`](https://hub.hcompany.ai/agents-api/sessions/overview#lifecycle) instead of rejecting it with `429`. Queued sessions don't count against your quota and start oldest first as running sessions finish. Set `false` to fail fast.

**group\_id** `string`

Tag for grouping related sessions. Filter on it with `GET /sessions?group_id=...`.

**parent\_session\_id** `string`

Parent session ID, for [subagent](https://hub.hcompany.ai/agents-api/multi-agent.md) sessions. The parent's status lists this session in `subagent_session_ids`. Child sessions never queue: at capacity the create fails with `429` even when `queue` is `true`, since a child queued behind its own parent would deadlock it.

**overrides** `object`

Per-session tweaks applied after the agent (and its environments, skills, and subagents) are resolved, so you can [adjust a catalog agent for one session](https://hub.hcompany.ai/agents-api/sessions/overview.md#overrides). Each key is a path into the resolved request, and its value replaces what the path points to:

* Dots walk into objects: `agent.instructions`, `agent.model`, `agent.answer_format` ([structured answer](https://hub.hcompany.ai/agents-api/structured-output.md)).
* `[field=value]` picks a list member: `agent.environments[kind=web].start_url`, `agent.environments[kind=web].mode`.

Each value must match the type of the field it targets: an unknown path or a wrong type returns `422` at creation, before the agent runs.

```json theme={"system"}
"overrides": {
  "agent.environments[kind=web].start_url": "https://www.bing.com/",
  "agent.answer_format": {
    "type": "object",
    "properties": {"price": {"type": "number"}},
    "required": ["price"]
  }
}
```

## Response

`201` with the created [session object](https://hub.hcompany.ai/agents-api/sessions/retrieve.md#response), status `pending` or `queued`.

```bash CLI theme={"system"}
# `holo run` creates the session and blocks until the agent answers
holo run "Find the best-rated sushi restaurants in San Francisco" \
  --agent h/web-surfer-flash
```

```python Python theme={"system"}
from hai_agents import Client

client = Client()

session = client.sessions.create_session(
    agent="h/web-surfer-flash",
    messages="Find the best-rated sushi restaurants in San Francisco",
)
print(session.id)
```

```typescript TypeScript theme={"system"}
import { HaiAgentsClient } from "hai-agents";

const client = new HaiAgentsClient();

const session = await client.sessions.createSession({
  body: {
    agent: "h/web-surfer-flash",
    messages: "Find the best-rated sushi restaurants in San Francisco",
  },
});
console.log(session.id);
```

```bash cURL theme={"system"}
curl -X POST https://agp.eu.hcompany.ai/api/v2/sessions \
  -H "Authorization: Bearer $HAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "h/web-surfer-flash",
    "messages": [
      {"type": "user_message", "message": "Find the best-rated sushi restaurants in San Francisco"}
    ]
  }'
```

```json 201 theme={"system"}
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "request": {
    "agent": {"name": "h/web-surfer-flash", "model": "holo4-35b-a3b", "...": "..."},
    "messages": [
      {"type": "user_message", "message": "Find the best-rated sushi restaurants in San Francisco"}
    ],
    "...": "..."
  },
  "status": {
    "status": "pending",
    "error": null,
    "error_code": null,
    "outcome": null,
    "steps": 0,
    "usage_per_model": [],
    "subagent_session_ids": []
  },
  "agent_view_url": "https://platform.hcompany.ai/agents/sessions/a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "latest_answer": null,
  "created_at": "2026-05-07T14:30:00Z",
  "started_at": null,
  "finished_at": null
}
```

## More examples

### With idempotency key

```python Python theme={"system"}
session = client.sessions.create_session(
    agent="h/web-surfer-flash",
    messages="Search for direct flights from CDG to NRT on June 15",
    idempotency_key="my-unique-key-123",
)
```

```typescript TypeScript theme={"system"}
const session = await client.sessions.createSession({
  idempotencyKey: "my-unique-key-123",
  body: {
    agent: "h/web-surfer-flash",
    messages: "Search for direct flights from CDG to NRT on June 15",
  },
});
```

```bash cURL theme={"system"}
curl -X POST https://agp.eu.hcompany.ai/api/v2/sessions \
  -H "Authorization: Bearer $HAI_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: my-unique-key-123" \
  -d '{
    "agent": "h/web-surfer-flash",
    "messages": [
      {"type": "user_message", "message": "Search for direct flights from CDG to NRT on June 15"}
    ]
  }'
```

### With an inline agent

Pass an inline agent instead of a catalog ID to set environments (or any other field) per session. Environments nest under `agent`.

```python Python theme={"system"}
session = client.sessions.create_session(
    agent={
        "name": "web-price-finder",
        "description": "Web browsing agent with a custom browser config",
        "environments": [
            {
                "id": "browser",
                "kind": "web",
                "mode": {"type": "visual", "width": 1280, "height": 720},
                "start_url": "https://www.bing.com",
            }
        ],
    },
    messages="Find the current price of the Framework 13 laptop and report the lowest you find",
)
```

```typescript TypeScript theme={"system"}
const session = await client.sessions.createSession({
  body: {
    agent: {
      name: "web-price-finder",
      description: "Web browsing agent with a custom browser config",
      environments: [
        {
          id: "browser",
          kind: "web",
          mode: { type: "visual", width: 1280, height: 720 },
          startUrl: "https://www.bing.com",
        },
      ],
    },
    messages: "Find the current price of the Framework 13 laptop and report the lowest you find",
  },
});
```

```bash cURL theme={"system"}
curl -X POST https://agp.eu.hcompany.ai/api/v2/sessions \
  -H "Authorization: Bearer $HAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": {
      "name": "web-price-finder",
      "description": "Web browsing agent with a custom browser config",
      "environments": [
        {"id": "browser", "kind": "web", "mode": {"type": "visual", "width": 1280, "height": 720}, "start_url": "https://www.bing.com"}
      ]
    },
    "messages": [
      {"type": "user_message", "message": "Find the current price of the Framework 13 laptop and report the lowest you find"}
    ]
  }'
```

### Child session

A child session of a [manager](https://hub.hcompany.ai/agents-api/multi-agent.md):

```python Python theme={"system"}
session = client.sessions.create_session(
    agent="h/web-surfer-flash",
    parent_session_id=parent_session_id,
    group_id="trip-planning-001",
    messages="Find hotels near Shinjuku station under $150/night",
)
```

```typescript TypeScript theme={"system"}
const session = await client.sessions.createSession({
  body: {
    agent: "h/web-surfer-flash",
    parentSessionId,
    groupId: "trip-planning-001",
    messages: "Find hotels near Shinjuku station under $150/night",
  },
});
```

```bash cURL theme={"system"}
curl -X POST https://agp.eu.hcompany.ai/api/v2/sessions \
  -H "Authorization: Bearer $HAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "h/web-surfer-flash",
    "parent_session_id": "'"$PARENT_SESSION_ID"'",
    "group_id": "trip-planning-001",
    "messages": [
      {"type": "user_message", "message": "Find hotels near Shinjuku station under $150/night"}
    ]
  }'
```

## Errors

| Status | Cause |
| - | - |
| `400` | The resolved request is invalid (for example an override producing an impossible configuration). |
| `403` | An environment kind that isn't available publicly yet. `detail[].type` is `environment_not_public`. |
| `404` | The agent is not found, or not in your organization. |
| `409` | An `Idempotency-Key` from a still in-flight request was reused before it completed. Retry after `Retry-After`. |
| `410` | The `agent` pins a `name:N` revision whose stored spec fails current validation. |
| `422` | Body failed validation (for example an unknown `timezone`), `files` were attached but no environment can receive them, or an `Idempotency-Key` was reused with a different body. |
| `429` | Concurrency quota exceeded with [`queue: false`](#param-queue), the queue itself is full, or the create carries a [`parent_session_id`](#param-parent-session-id) while at capacity. `detail[].type` says which [limit](https://hub.hcompany.ai/agents-api/errors.md#quota-errors-429) was hit. |


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.