> ## Documentation Index
> Fetch the complete documentation index at: https://hub.hcompany.ai/llms.txt
> Use this file to discover all available pages before exploring further.

> ## Agent Instructions
> H Platform has three products: the Agents API (managed computer-use agents, base URL https://agp.eu.hcompany.ai/api/v2 or https://agp.hcompany.ai/api/v2 for the US), the Models API (OpenAI-compatible Holo vision-language models at https://api.hcompany.ai/v1), and HoloTab (a free no-code Chrome extension that runs Holo in the user's browser, with recordable routines and schedules). The `holo` CLI (`pip install "hai-agents[cli]"`) runs Agents API agents from a terminal, drives the user's own desktop with `-a h/holo-desktop`, and serves coding agents through `holo mcp`.
> Authenticate with a bearer API key from the HAI_API_KEY environment variable. SDKs: `pip install hai-agents` (Python, `from hai_agents import Client`) and `npm install hai-agents` (TypeScript, `import { HaiAgentsClient } from "hai-agents"`). CLI: `holo`.
> Agents do work in a browser or on a desktop; describe the task as an imperative instruction. To run a task quickly, prefer the pre-built agent `h/web-surfer-flash`. Read results from the session's `latest_answer` after it reaches a terminal status.
> Sessions are the unit of work; wait for a terminal status (completed, failed, timed_out, interrupted) before reading the answer. Use webhooks or the `changes` long-poll endpoint to follow progress.

# Cloud browser

> The browser H runs for your agent.

## Define a browser

The Browser (`kind: "web"`) is a web browser H provisions per session in the cloud, or [Chrome on your own machine](https://hub.hcompany.ai/agents-api/your-browser.md). Inline in an agent's `environments`, only `id` is required. Every field on this page goes in that object. Sign-in, proxies, `lazy`, and viewport size are cloud only: [your browser](https://hub.hcompany.ai/agents-api/your-browser.md) uses your Chrome's own logins, network, and window.

This one starts on Wikipedia and reads pages as text:

```python Python theme={"system"}
from hai_agents import Client

client = Client()
result = client.run_session(
    agent={
        "name": "wiki-reader",
        "description": "Answers questions from Wikipedia.",
        "environments": [{"id": "wiki", "kind": "web", "start_url": "https://en.wikipedia.org", "mode": {"type": "text"}}],
    },
    messages="When was the Eiffel Tower completed?",
)
print(result.answer)
```

```typescript TypeScript theme={"system"}
import { HaiAgentsClient } from "hai-agents";

const client = new HaiAgentsClient();
const result = await client.runSession({
  agent: {
    name: "wiki-reader",
    description: "Answers questions from Wikipedia.",
    environments: [{ id: "wiki", kind: "web", startUrl: "https://en.wikipedia.org", mode: { type: "text" } }],
  },
  messages: "When was the Eiffel Tower completed?",
});
console.log(result.answer);
```

```bash cURL theme={"system"}
curl -X POST https://agp.eu.hcompany.ai/api/v2/sessions \
  -H "Authorization: Bearer $HAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": {
      "name": "wiki-reader",
      "description": "Answers questions from Wikipedia.",
      "environments": [{"id": "wiki", "kind": "web", "start_url": "https://en.wikipedia.org", "mode": {"type": "text"}}]
    },
    "messages": "When was the Eiffel Tower completed?"
  }'
```

Or reference the [pre-built](https://hub.hcompany.ai/agents-api/environments/overview.md#pre-built-environments) `h/browser` by name: `"environments": ["h/browser"]`.

## Modes

At each step the agent gets a fresh observation of the page, then picks one or more actions. `mode` sets what the observation contains and which [actions](#actions) exist.

| Mode | The agent sees | The agent acts | Use it for |
| - | - | - | - |
| `visual` (default) | A viewport screenshot, the URL, and open tabs. With `markdown: true`, also the page text. | Points at on-screen targets, and the platform resolves exact click and type coordinates. | General web work: clicking, forms, anything that needs the rendered page. |
| `text` | The page as text only, in chunks of `chunk_size` characters. Each observation says which chunk the agent is on. | Reads, pages through chunks instead of scrolling, follows links by URL. Read-only: it cannot click or type. | Reading and research at scale: search, scraping, link-heavy navigation. |

Cost tracks screenshots. `text` sends no images, so it uses the fewest tokens and runs fastest. `markdown: true` adds page text to each screenshot. Default to `visual`, and switch to `text` for pure reading.

### Mode fields

`mode` is an object selected by `type`, carrying only that mode's fields.

| `type` | Field | Default | Meaning |
| - | - | - | - |
| `visual` | `width`, `height` | `1200` | Viewport size in pixels. Positive integers. |
| `visual` | `markdown` | `false` | Also include the viewport's text as markdown with each screenshot. |
| `text` | `chunk_size` | `20000` | Characters of page text per chunk. Positive integer. Larger fits more per step, at more tokens per observation. |

```json Text-mode browser theme={"system"}
{ "id": "research-browser", "kind": "web", "mode": { "type": "text", "chunk_size": 20000 } }
```

## Sign in

Use a [vault](https://hub.hcompany.ai/agents-api/vaults/overview.md) to sign in mid-session, a [profile](https://hub.hcompany.ai/agents-api/browser/profiles.md) to start signed in.

| Field | Default | Meaning |
| - | - | - |
| `vault_id` | `null` | Vault whose secrets the agent can fill into login forms. |
| `browser_profile_id` | `null` | Profile to load (cookies, storage), so the session starts signed in. |
| `use_default_browser_profile` | `false` | Load your [default profile](https://hub.hcompany.ai/agents-api/browser/profiles.md#default-profiles) and save state back when the session ends. Mutually exclusive with `browser_profile_id`. |
| `persist_browser_profile` | `false` | [Save the final state back](https://hub.hcompany.ai/agents-api/browser/profiles.md#persisting-state-back-into-the-profile) into the loaded profile. Requires `browser_profile_id` or `use_default_browser_profile`. |

## Browser profiles

A browser profile is a cloud browser's saved state: cookies, local storage, and session data. Load one and the session starts signed in. Default profiles, saving state back, and uploading your own are in [Browser profiles](https://hub.hcompany.ai/agents-api/browser/profiles.md).

## Route through a proxy

Use a proxy when a site blocks datacenter IPs or geofences by region, or when a session needs traffic from a specific network. Set `network` to exactly one of `managed_proxy` or `proxy_url`.

### Managed proxy

H provisions the proxy. You pick pool, country, and stickiness. H resolves credentials server-side, so they never appear in your requests, session data, or logs.

| Field | Type | Default | Meaning |
| - | - | - | - |
| `pool` | string | `"residential"` | `residential` (real-ISP addresses, best for sites that block datacenter ranges) or `datacenter`. A pool not available in your environment fails with `400`. |
| `country` | string \| null | `null` | Two-letter ISO 3166-1 code (like `"US"`). Omit for any location. Invalid codes fail with `422`. |
| `sticky` | boolean | `true` | Keep the same exit IP for the whole session. Residential pools only. Datacenter endpoints manage rotation themselves. |

```json Managed proxy theme={"system"}
{ "network": { "managed_proxy": { "pool": "residential", "country": "US" } } }
```

### Bring your own proxy

For a proxy you already operate. `network.proxy_url` (string, default `null`) is its URL with credentials inline, like `http://user:pass@host:port`. HTTP, HTTPS, and SOCKS schemes work. Treat the value as a secret.

```json Bring your own theme={"system"}
{ "network": { "proxy_url": "http://user:pass@proxy.example.com:8080" } }
```

To reuse a proxied browser across agents, [create it in your catalog](https://hub.hcompany.ai/agents-api/environments/create.md) with the same `network` object.

## Fields

| Field | Default | Meaning |
| - | - | - |
| `id` | required | Catalog identifier. |
| `kind` | `"web"` | Environment type. A browser is `web`. |
| `start_url` | `"https://www.bing.com"` | First URL to open. |
| `headless` | `false` | Run without a visible window. |
| `mode` | `{"type": "visual"}` | How the agent sees and acts: `visual` or `text`. See [Modes](#modes). |
| `lazy` | `false` | Start the browser on the agent's first browser action, not at session start. |
| `settle_s` | `1.0` | Seconds the agent waits after each action, so the next observation shows the settled page. `0` to `10`. |
| `host` | `"cloud"` | `cloud` (H infrastructure) or `user_device` (Chrome on your machine, see [Your browser](https://hub.hcompany.ai/agents-api/your-browser.md)). |
| `session_id` | `null` | Command channel of a `user_device` browser. The Python SDK sets it. Set it yourself only to attach to [`holo local browser`](https://hub.hcompany.ai/agents-api/your-browser#run-a-session). |
| `vault_id`, `browser_profile_id`, `use_default_browser_profile`, `persist_browser_profile` | | See [Sign in](#sign-in). |
| `network` | `null` | Proxy egress. See [Route through a proxy](#route-through-a-proxy). |

## Actions

The agent picks actions on its own, and the set is fixed. To shape how it uses them, set the agent's [`instructions`](https://hub.hcompany.ai/agents-api/agents/overview). To add your own functions, use [custom tools](https://hub.hcompany.ai/agents-api/custom-tools.md).

| Action | Description | Visual | Text |
| - | - | :-: | :-: |
| `go_to_web` | Navigate to a URL. | ✓ | ✓ |
| `go_back_web` | Go back in the browser history. | ✓ | ✓ |
| `refresh_web` | Refresh the current page. | ✓ | ✓ |
| `switch_tab_web` | Switch to another tab, or open a new one. | ✓ | ✓ |
| `close_tab_web` | Close a tab. | ✓ | ✓ |
| `click_web` | Click at viewport coordinates. | ✓ | |
| `write` | Focus an input at coordinates and type into it. | ✓ | |
| `fill_secret_at` | Fill a vault secret. See [Vaults](https://hub.hcompany.ai/agents-api/vaults/overview.md). | ✓ | |
| `select_option` | Pick an option from a native `<select>` dropdown. | ✓ | |
| `move_mouse_web` | Move the mouse to reveal hovers, tooltips, or menus. | ✓ | |
| `press_keys_web` | Press keys or keyboard shortcuts. | ✓ | |
| `scroll_web` | Scroll the page or a nested scrollable container. | ✓ | |
| `ctrl_f_web` | Jump to the next on-page match of a text query. | ✓ | |
| `reader_mode` | Extract the page's main content as clean markdown. | ✓ | |
| `find_in_page` | Find a text query and jump to the chunk that contains it. | | ✓ |
| `switch_chunk` | Page forward or backward through the page's text chunks. | | ✓ |
| `wait_web` | Pause for the page to settle (up to 60 seconds). | ✓ | ✓ |


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.