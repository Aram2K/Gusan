# Vault: logins the agent never sees (implementation notes)

Implements spec v2 §7 with the security review §A2 fixes (G1–G7). Not deployed.

## Flow
1. **Add (iPhone):** Vault tab → Site + Username + Password fields (`UITextField` with
   `textContentType` `.URL/.username/.password`, so iOS Password AutoFill offers the key icon →
   Face ID → any saved login). Saved to the app's own keychain only:
   `kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly` + `SecAccessControl(.biometryCurrentSet)`,
   no `kSecAttrAccessGroup`, never in the App Group, pasteboard, or keyboard extension.
   Gusan Cloud receives metadata only (`hostname`, `username_hint`, `has_totp`).
2. **Bind (confirm):** `agent-action confirm` creates an EMPTY H vault `gusan-task-<task_id>`
   and binds it to the execute session (`vault_id` on the web environment) with one custom tool
   `request_login(hostname)`. Vault creation failure fails closed (`vault_error`).
3. **Ask:** the agent calls `request_login`. The hostname must match one of the user's saved
   logins, else the call is answered with an error. The task becomes `awaiting_login`; a one-time
   `release_nonce` (15 min) is stored server-side (service-role table) and returned only to the
   owner through `agent-vault pending`.
4. **Release (iPhone, per use):** the app shows "<hostname> asks you to sign in", Face ID reads the
   keychain item, and `agent-vault release` sends the values once. The function checks owner,
   `awaiting_login`, hostname = login hostname, nonce (compare-and-set), ≤3 releases/task, a 64 KB
   body cap, and the field allowlist; creates one item `{urls:[https://<hostname>], values}` in the
   task's vault; answers the tool call ("login available, use fill_secret_at"). The values exist only in
   that invocation's memory and are never logged, stored, or echoed.
5. **Fill:** H's runtime types the value via `fill_secret_at` on matching pages only, outside the model's
   context.
6. **Delete:** the vault (and its item) is deleted on every terminal path (completed, failed, cancel,
   expiry, login timeout) and by `agent-vault sweep` (scheduled) for any leftover `gusan-task-*` vault.
   Sessions that received a login use `delete_screenshot_after_min: 10`, `delete_after_min: 60`.

## Audit
`vault_events` (append-only, service-role insert, owner select): event, hostname, login/task ids, time.
No values, no usernames, no H ids.

## Residual risks (cannot be removed by Gusan)
- H's `reveal` endpoint lets any member of the H org read in-flight items (minutes). Keep the H org at
  one member and rotate `H_company_computer_agents`.
- `fill_secret_at` types into whatever field is targeted; a non-masked field would show the value in
  the next screenshot. Domain gating limits this to the user's own site.
- Gusan Cloud sees plaintext in memory during `release` (not end-to-end; 1Password provider later).
- Spike S9 (items added to an already-bound vault are fillable) is unverified against the live API.
