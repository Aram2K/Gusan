> ## Documentation Index
> Fetch the complete documentation index at: https://hub.hcompany.ai/llms.txt
> Use this file to discover all available pages before exploring further.

> ## Agent Instructions
> H Platform has three products: the Agents API (managed computer-use agents, base URL https://agp.eu.hcompany.ai/api/v2 or https://agp.hcompany.ai/api/v2 for the US), the Models API (OpenAI-compatible Holo vision-language models at https://api.hcompany.ai/v1), and HoloTab (a free no-code Chrome extension that runs Holo in the user's browser, with recordable routines and schedules). The `holo` CLI (`pip install "hai-agents[cli]"`) runs Agents API agents from a terminal, drives the user's own desktop with `-a h/holo-desktop`, and serves coding agents through `holo mcp`.
> Authenticate with a bearer API key from the HAI_API_KEY environment variable. SDKs: `pip install hai-agents` (Python, `from hai_agents import Client`) and `npm install hai-agents` (TypeScript, `import { HaiAgentsClient } from "hai-agents"`). CLI: `holo`.
> Agents do work in a browser or on a desktop; describe the task as an imperative instruction. To run a task quickly, prefer the pre-built agent `h/web-surfer-flash`. Read results from the session's `latest_answer` after it reaches a terminal status.
> Sessions are the unit of work; wait for a terminal status (completed, failed, timed_out, interrupted) before reading the answer. Use webhooks or the `changes` long-poll endpoint to follow progress.

# Browser profiles

> A cloud browser's saved cookies and storage, so a session starts signed in.

## Default profiles

Every user has a **default profile** per browser flavor (for example `chromium`): a browser that remembers you across sessions. You upload nothing and track no IDs.

```json Browser that follows the default profile theme={"system"}
{ "id": "acme-browser", "kind": "web", "use_default_browser_profile": true }
```

* If you have no default yet, the session creates an empty profile and marks it as your default when it starts. Sign in to your sites once (for example in the session's [Agent View](https://hub.hcompany.ai/agents-api/observe-and-steer.md#agent-view)) and the state carries forward. Delete it and the next default-profile session starts from a fresh empty default.
* Each session writes its final state back when it ends, under the [one-writer rule](#persisting-state-back-into-the-profile).
* Name a profile with `browser_profile_id` or follow the default, not both.

## Save state back

A profile named with `browser_profile_id` is read-only by default: the session discards whatever it changes (new cookies, refreshed tokens, dismissed banners), so its logins age out. Set `persist_browser_profile: true` to write the final state back when the session ends. Sessions that follow your [default profile](#default-profiles) persist without the flag.

```json Browser that keeps its profile fresh theme={"system"}
{
  "id": "acme-browser",
  "kind": "web",
  "browser_profile_id": "a1b2c3d4-5678-90ab-cdef-1234567890ab",
  "persist_browser_profile": true
}
```

One session writes to a profile at a time. The first session to persist a profile, by flag or as a default-profile session, holds its write lock until it ends. Later sessions asking to persist still start, but read-only, instead of failing. Any number of sessions can load a profile read-only in parallel.

* Last write wins: the session's final state replaces the stored state.
* Request persistence when creating the session, not when attaching to one.
* If the write-back fails, the session still stops cleanly and the profile keeps its previous state.

## Upload your own profile

Upload a profile when you want to bring logins built outside H. You build a Chrome **user-data directory** (the folder holding cookies, local storage, saved logins, and extensions), zip it, and upload it.

Build a dedicated profile, on Linux, on the runner's Chromium:

* Never upload your personal Chrome directory (`~/Library/Application Support/Google/Chrome` on macOS, `~/.config/google-chrome` on Linux). It holds every password, token, and history entry you have. Treat the archive as a secret.
* Cookies only carry over from Linux builds. On macOS and Windows, Chrome always encrypts cookies with the OS keychain (Keychain or DPAPI), so the profile loads logged out, with only storage and extensions. No flag changes that: build in Docker.
* Sessions run Chromium 150.0.7871.124. A profile built on a newer version does not load.
* Shut Chromium down cleanly. It writes cookies to disk only on a clean shutdown or an occasional autosave. Killing it (`docker stop` sends `SIGTERM`, `docker rm -f` sends `SIGKILL`) can lose the logins you just made.

### Creating a profile

`--password-store=basic` makes cookies portable: Chrome encrypts them with a built-in key that the session runner also uses. Without it, the agent starts logged out.

**Docker (any OS)**

You need [Docker](https://www.docker.com/). The image tag is pinned to the runner's Chromium, so do not use `:latest`.

```bash theme={"system"}
docker run -d --name agent-profile -p 127.0.0.1:3000:3000 \
  -e CHROME_CLI="--password-store=basic" \
  -v "$HOME/agent-profile:/config" \
  lscr.io/linuxserver/chromium:11e602ae-ls45

# Open http://localhost:3000 and sign in to the sites the agent needs.

# Flush cookies with SIGINT, wait, then remove the container and zip.
docker exec agent-profile pkill -INT -o chromium
sleep 3
docker rm -f agent-profile
cd "$HOME/agent-profile/.config/chromium" && zip -r ~/profile.zip .
```

**Linux Chrome**

Use a Chrome no newer than the runner's. It opens a fresh profile, separate from your everyday one.

```bash theme={"system"}
google-chrome --user-data-dir="$HOME/agent-profile" --password-store=basic

# Sign in, dismiss cookie banners, install extensions. Quit Chrome cleanly, then:
cd "$HOME/agent-profile" && zip -r ~/profile.zip .
```

### Uploading a profile

**1. Initiate the upload**

[`POST /initiate-upload`](https://hub.hcompany.ai/agents-api/browser-profiles/initiate-upload) returns a `profile_id`, a presigned `upload_url`, and the `upload_fields` to send with the file.

```python Python theme={"system"}
from hai_agents import Client

client = Client()

upload = client.browser_profiles.initiate_browser_profile_upload()
```

```typescript TypeScript theme={"system"}
import { HaiAgentsClient } from "hai-agents";

const client = new HaiAgentsClient();

const upload = await client.browserProfiles.initiateBrowserProfileUpload();
```

```bash cURL theme={"system"}
INIT=$(curl -s -X POST https://agp.eu.hcompany.ai/api/v2/browser-profiles/initiate-upload \
  -H "Authorization: Bearer $HAI_API_KEY")
PROFILE_ID=$(echo "$INIT" | jq -r .profile_id)
UPLOAD_URL=$(echo "$INIT" | jq -r .upload_url)
```

**2. Upload the archive to storage**

`POST` the `.zip` to `upload_url` as `multipart/form-data`, with every entry of `upload_fields`, unmodified. The archive must be `application/zip`, at most **1 GB**. The URL and fields expire after `upload_expires_in` seconds, so upload right away.

```python Python theme={"system"}
import requests

with open("profile.zip", "rb") as f:
    resp = requests.post(
        upload.upload_url,
        data=upload.upload_fields,
        files={"file": ("profile.zip", f, "application/zip")},
    )
    resp.raise_for_status()
```

```typescript TypeScript theme={"system"}
import { readFileSync } from "fs";

const form = new FormData();
for (const [key, value] of Object.entries(upload.uploadFields)) {
  form.append(key, value as string);
}
form.append("file", new Blob([readFileSync("profile.zip")]), "profile.zip");
const resp = await fetch(upload.uploadUrl, { method: "POST", body: form });
if (!resp.ok) throw new Error(`upload failed: ${resp.status}`);
```

```bash cURL theme={"system"}
args=()
while IFS= read -r line; do
  args+=(--form-string "$line")
done <<< "$(echo "$INIT" | jq -r '.upload_fields | to_entries[] | "\(.key)=\(.value)"')"

curl -sS --fail-with-body -w "upload: HTTP %{http_code}\n" \
  -X POST "$UPLOAD_URL" "${args[@]}" -F "file=@profile.zip"

# Healthy upload will return `upload: HTTP 204`
```

**3. Complete the upload**

[`POST /{profile_id}/complete-upload`](https://hub.hcompany.ai/agents-api/browser-profiles/complete-upload) with `name`, `browser_name`, and `browser_version` **within 1 day**, or the pending upload is deleted. The platform verifies the archive and creates the profile.

```python Python theme={"system"}
profile = client.browser_profiles.complete_browser_profile_upload(
    profile_id=upload.profile_id,
    name="acme-prod-login",
    browser_name="chromium",
    browser_version="150",
)
print(profile.id)
```

```typescript TypeScript theme={"system"}
const profile = await client.browserProfiles.completeBrowserProfileUpload({
  profileId: upload.profileId,
  name: "acme-prod-login",
  browserName: "chromium",
  browserVersion: "150",
});
console.log(profile.id);
```

```bash cURL theme={"system"}
curl -X POST "https://agp.eu.hcompany.ai/api/v2/browser-profiles/$PROFILE_ID/complete-upload" \
  -H "Authorization: Bearer $HAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "acme-prod-login", "browser_name": "chromium", "browser_version": "150"}'
```

Load it by setting `browser_profile_id` on the browser, as in [Sign in](https://hub.hcompany.ai/agents-api/browser/configuration.md#sign-in). [List, retrieve, or delete](https://hub.hcompany.ai/agents-api/browser-profiles/list.md) your profiles with the browser profile endpoints.

## Choose your own default

Promote a profile you curated (for example one you [uploaded](#upload-your-own-profile)). You can only mark profiles you created yourself. Profile objects report their status in `is_default`.

| Action | Endpoint |
| - | - |
| Set (replaces the previous default for that browser) | [`PUT /browser-profiles/{profile_id}/default`](https://hub.hcompany.ai/agents-api/browser-profiles/set-default) |
| Read | [`GET /browser-profiles/default`](https://hub.hcompany.ai/agents-api/browser-profiles/get-default) |
| Clear | [`DELETE /browser-profiles/{profile_id}/default`](https://hub.hcompany.ai/agents-api/browser-profiles/unset-default) |


This documentation is built and hosted on [Mintlify](https://mintlify.com), a developer documentation platform.