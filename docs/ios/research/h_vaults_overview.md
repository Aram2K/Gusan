> ## Documentation Index
> Fetch the complete documentation index at: https://hub.hcompany.ai/llms.txt
> Use this file to discover all available pages before exploring further.

> ## Agent Instructions
> H Platform has three products: the Agents API (managed computer-use agents, base URL https://agp.eu.hcompany.ai/api/v2 or https://agp.hcompany.ai/api/v2 for the US), the Models API (OpenAI-compatible Holo vision-language models at https://api.hcompany.ai/v1), and HoloTab (a free no-code Chrome extension that runs Holo in the user's browser, with recordable routines and schedules). The `holo` CLI (`pip install "hai-agents[cli]"`) runs Agents API agents from a terminal, drives the user's own desktop with `-a h/holo-desktop`, and serves coding agents through `holo mcp`.
> Authenticate with a bearer API key from the HAI_API_KEY environment variable. SDKs: `pip install hai-agents` (Python, `from hai_agents import Client`) and `npm install hai-agents` (TypeScript, `import { HaiAgentsClient } from "hai-agents"`). CLI: `holo`.
> Agents do work in a browser or on a desktop; describe the task as an imperative instruction. To run a task quickly, prefer the pre-built agent `h/web-surfer-flash`. Read results from the session's `latest_answer` after it reaches a terminal status.
> Sessions are the unit of work; wait for a terminal status (completed, failed, timed_out, interrupted) before reading the answer. Use webhooks or the `changes` long-poll endpoint to follow progress.

# Credentials and 2FA

> Let an agent sign in with vault credentials and resolve one-time codes, without vault secrets entering its context.

A vault holds the credentials an agent signs in with, one login per item: the sites it applies to and up to five [fields](#what-an-item-holds). The agent asks for a field such as `password`, and the session picks the item from the page URL and types the value into the page, outside the agent's context. No endpoint returns secrets. To start already signed in, use a [browser profile](https://hub.hcompany.ai/agents-api/browser/profiles.md).

| `provider` | Where the secrets live | `provider_config` | `token` |
| - | - | - | - |
| `onepassword` | A [1Password](https://developer.1password.com/) vault you own. When an agent fills a field, H fetches the item from 1Password and decrypts it. | `{"provider": "onepassword", "op_vault_id": "..."}` | A 1Password [service account token](https://www.1password.dev/service-accounts) for that vault. Validated against 1Password, write-only, never returned. |
| `internal` | An H-hosted vault: H stores the items you add through the [item endpoints](https://hub.hcompany.ai/agents-api/vaults/create-item.md). | `{"provider": "internal"}` | None. |

## Set up a vault

A vault does nothing until a [browser](https://hub.hcompany.ai/agents-api/browser/configuration.md) environment binds it with `vault_id`. Create the vault, then bind it inline in an agent's `environments` or on a catalog [environment](https://hub.hcompany.ai/agents-api/environments/create.md). When a vault credential matches the current page, the agent is offered a [`fill_secret_at`](https://hub.hcompany.ai/agents-api/browser/configuration#actions) action to sign in.

* [Cloud browsers](https://hub.hcompany.ai/agents-api/browser/configuration.md) only, since secrets never leave H's infrastructure.
* Manage vaults (list, update, [rotate a 1Password token](https://hub.hcompany.ai/agents-api/vaults/rotate-token.md), delete, [health-check](https://hub.hcompany.ai/agents-api/vaults/health.md)) from the [Vaults](https://hub.hcompany.ai/agents-api/vaults/create.md) endpoints, and an H-hosted vault's items from the [item](https://hub.hcompany.ai/agents-api/vaults/list-items.md) endpoints.

### 1Password

```python Python theme={"system"}
from hai_agents import Client, VaultCreateRequestProviderConfig_Onepassword

client = Client()
vault = client.vaults.create_vault(
    name="prod-1password",
    provider_config=VaultCreateRequestProviderConfig_Onepassword(op_vault_id="abcd1234efgh5678"),
    token="ops_eyJ...",
)
client.environments.create_environment(request={"kind": "web", "id": "signed-in-browser", "vault_id": vault.id})
```

```typescript TypeScript theme={"system"}
import { HaiAgentsClient } from "hai-agents";

const client = new HaiAgentsClient();
const vault = await client.vaults.createVault({
  name: "prod-1password",
  providerConfig: { provider: "onepassword", opVaultId: "abcd1234efgh5678" },
  token: "ops_eyJ...",
});
await client.environments.createEnvironment({ body: { id: "signed-in-browser", kind: "web", vaultId: vault.id } });
```

```bash cURL theme={"system"}
VAULT_ID=$(curl -sX POST https://agp.eu.hcompany.ai/api/v2/vaults \
  -H "Authorization: Bearer $HAI_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "prod-1password",
    "provider_config": {"provider": "onepassword", "op_vault_id": "abcd1234efgh5678"},
    "token": "ops_eyJ..."
  }' | jq -r .id)

curl -X POST https://agp.eu.hcompany.ai/api/v2/environments \
  -H "Authorization: Bearer $HAI_API_KEY" -H "Content-Type: application/json" \
  -d "{\"id\": \"signed-in-browser\", \"kind\": \"web\", \"vault_id\": \"$VAULT_ID\"}"
```

### H-hosted

Create the vault, add one item per login, then bind it the same way:

```bash cURL theme={"system"}
VAULT_ID=$(curl -sX POST https://agp.eu.hcompany.ai/api/v2/vaults \
  -H "Authorization: Bearer $HAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "team-logins", "provider_config": {"provider": "internal"}}' | jq -r .id)

curl -X POST "https://agp.eu.hcompany.ai/api/v2/vaults/$VAULT_ID/items" \
  -H "Authorization: Bearer $HAI_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "Example admin",
    "urls": ["https://example.com/login"],
    "values": {"username": "admin@example.com", "password": "s3cret", "totp": "JBSWY3DPEHPK3PXP"}
  }'

curl -X POST https://agp.eu.hcompany.ai/api/v2/environments \
  -H "Authorization: Bearer $HAI_API_KEY" -H "Content-Type: application/json" \
  -d "{\"id\": \"signed-in-browser\", \"kind\": \"web\", \"vault_id\": \"$VAULT_ID\"}"
```

```python Python theme={"system"}
from hai_agents import Client, VaultCreateRequestProviderConfig_Internal

client = Client()
vault = client.vaults.create_vault(name="team-logins", provider_config=VaultCreateRequestProviderConfig_Internal())
client.vaults.create_vault_item(
    vault_id=vault.id,
    name="Example admin",
    urls=["https://example.com/login"],
    values={"username": "admin@example.com", "password": "s3cret", "totp": "JBSWY3DPEHPK3PXP"},
)
client.environments.create_environment(request={"kind": "web", "id": "signed-in-browser", "vault_id": vault.id})
```

```typescript TypeScript theme={"system"}
import { HaiAgentsClient } from "hai-agents";

const client = new HaiAgentsClient();
const vault = await client.vaults.createVault({ name: "team-logins", providerConfig: { provider: "internal" } });
await client.vaults.createVaultItem({
  vaultId: vault.id,
  name: "Example admin",
  urls: ["https://example.com/login"],
  values: { username: "admin@example.com", password: "s3cret", totp: "JBSWY3DPEHPK3PXP" },
});
await client.environments.createEnvironment({ body: { id: "signed-in-browser", kind: "web", vaultId: vault.id } });
```

## What an item holds

An item carries up to five fields, each a value the agent can fill: `username`, `password`, `email`, `totp`, and `api_key`. For `totp`, `fill_secret_at` types the current code, never the secret.

In an H-hosted vault, set them in the item's `values`. For `totp`, store the authenticator secret (base32, or an `otpauth://totp/` URI).

In 1Password, each one maps to a field on the item. Other fields are ignored:

| Field | 1Password item field |
| - | - |
| `username` | Title contains "username" |
| `password` | Password type, title contains "password" |
| `email` | Email type, or title contains "email" |
| `totp` | One-time password type |
| `api_key` | Password type, title contains "api" or "key" |

## How secrets are matched

Agents never name an item. The page URL picks one item, using the sites stored on it (a 1Password item's websites, or an H-hosted vault item's `urls`), and the agent can fill only that item's fields:

| Rule | Behavior | Example (page `test.hcompany.ai`) |
| - | - | - |
| Domain gating | Eligible only if the page hostname equals, or is a subdomain of, a site stored on the item. | `hcompany.ai` item: eligible. `prod.hcompany.ai` item (sibling): not eligible. |
| Closest host wins | Among eligible items, the most specific hostname match wins. Exact host beats parent domain. Deeper subdomain beats shallower. | `test.hcompany.ai` item beats `hcompany.ai` item. |
| Path breaks ties | Equal hostname matches: the stored path sharing the most leading segments with the page URL wins, then the one with the fewest stored segments the page doesn't share. Path is never required. | Three `hcompany.ai` items on page `test.hcompany.ai/app/login`: path `/app` beats `/`, which beats `/enterprise`. |

In an H-hosted vault, two items that still tie resolve to the most recently created one. Store one item per login so matching stays unambiguous.

## Handle two-factor authentication

### Authenticator apps via a vault

For a TOTP authenticator, store its secret in 1Password or as an H-hosted vault item's `totp` value, then [bind the vault](#set-up-a-vault). On a matching page, the session generates the code from the TOTP secret and `fill_secret_at` with `totp` types it in. The agent never sees the code.

### Email, SMS, and confirmation links

The SDKs ship a [custom tool](https://hub.hcompany.ai/agents-api/custom-tools.md) for this: the agent calls `request_otp`, your process resolves the code or link, and the session continues with that single value. Pass `otp_tool` / `otpTool` in `tools` with the handler that matches where the code arrives:

| Where the code arrives | Handler | Best for |
| - | - | - |
| You're at the keyboard | None: prompts in your terminal (Node.js only in TypeScript) | Debugging at your desk |
| An inbox you control | `imap_otp_handler` / `imapOtpHandler` | Unattended sessions |
| Slack, an SMS provider, anything else | Your own function, sync or async | Anything your code can reach |

```python Python theme={"system"}
import os

from hai_agents import Client
from hai_agents_tools import OtpRequest, imap_otp_handler, otp_tool

inbox = imap_otp_handler(
    host="imap.gmail.com",
    username="agent-inbox@gmail.com",
    password=os.environ["GMAIL_APP_PASSWORD"],
    sender="no-reply@example.com",  # optional: only mail from this address
)

def from_slack(request: OtpRequest) -> str:
    return slack.ask_user(request.prompt)

client = Client()
result = client.run_session(
    agent="h/web-surfer-flash",
    messages="Log in to example.com and check for new notifications.",
    tools=[otp_tool(inbox)],  # or otp_tool() to prompt, otp_tool(from_slack) for your own handler
)
print(result.answer)
```

```typescript TypeScript theme={"system"}
import { HaiAgentsClient, imapOtpHandler, otpTool, type OtpRequest } from "hai-agents";

// The IMAP handler needs: npm install imapflow mailparser
const inbox = imapOtpHandler({
  host: "imap.gmail.com",
  username: "agent-inbox@gmail.com",
  password: process.env.GMAIL_APP_PASSWORD!,
  sender: "no-reply@example.com", // optional: only mail from this address
});

async function fromSlack(request: OtpRequest): Promise<string> {
  return slack.askUser(request.prompt);
}

const client = new HaiAgentsClient();
const result = await client.runSession({
  agent: "h/web-surfer-flash",
  messages: "Log in to example.com and check for new notifications.",
  tools: [otpTool({ handler: inbox })], // or otpTool() to prompt, otpTool({ handler: fromSlack })
});
console.log(result.answer);
```

### Read the code from email

The IMAP handler runs in your process. It polls unread mail (newest first), extracts a code or confirmation link, marks that message read so a retry can't reuse a stale code, and returns only that value. IMAP credentials never leave your machine, and the agent never sees mailbox contents, subjects, or senders. For Gmail or Google Workspace, use an [app password](https://support.google.com/accounts/answer/185833).

| Option (Python / TypeScript) | Default | Role |
| - | - | - |
| `host`, `username`, `password` | required | IMAP login |
| `port` | `993` | IMAP over TLS |
| `mailbox` | `"INBOX"` | Mailbox to poll |
| `sender` | unset | Only consider mail from this address |
| `timeout_s` / `timeoutMs` | 2 minutes | Give up (tool error to the agent) after this long |
| `poll_interval_s` / `pollIntervalMs` | 5 seconds | Delay between polls |
| `max_age_s` / `maxAgeMs` | 15 minutes | Ignore unread mail older than this |
| `mark_seen` / `markSeen` | on | Mark the matched message read |
| `code_pattern` / `codePattern` | built-in heuristics | Override extraction. The first capture group (or the whole match) is the code |

### What the agent sends

Your handler receives the tool's fixed input schema as an `OtpRequest`:

| Field | Required | Meaning |
| - | - | - |
| `prompt` | yes | Human-readable ask, for example "Enter the 6-digit code sent to j\*\*\*@example.com" |
| `kind` | no | `"code"` (default), or `"link"` for a full confirmation URL |
| `source` | no | Where it was sent, for example `"email"`, `"sms"`, `"authenticator app"` |

Return a non-empty string. An empty value fails as a tool error, so the agent can retry or stop cleanly.


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.