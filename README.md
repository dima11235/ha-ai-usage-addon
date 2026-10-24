# AI Usage Monitor — Home Assistant add-on

Home Assistant add-on that monitors the subscription usage limits of Claude,
Codex, and z.ai without relying on a Windows scheduled task.

The add-on:

- polls Claude's 5-hour and weekly usage windows;
- manages independent Codex accounts: add, rename, sign in, or delete each through Ingress;
- detects Codex rate-limit windows from response headers;
- reads z.ai Coding Plan 5-hour and weekly quota windows;
- polls every 2 minutes and schedules an extra poll just after a known reset;
- refreshes and atomically persists rotating OAuth tokens;
- publishes usage updates to the companion `ai_usage_monitor` Home Assistant
  integration through the internal event API;
- provides an Ingress page for importing credentials and running a manual poll.

## Installation

1. In Home Assistant open **Settings → Apps → App store**.
2. Open **Repositories** and add:
   `https://github.com/dima11235/ha-ai-usage-addon`
3. Install **AI Usage Monitor**.
4. Start the add-on and enable **Start on boot** and **Watchdog** if desired.
5. Open the add-on Web UI and import:
   - Claude: `.claude/.credentials.json`
   - Codex: `.codex/auth.json`
   - z.ai: Coding Plan API key entered directly in the Ingress page
6. Click **Update now** and check all provider statuses.

Credential files are validated and stored only in the add-on's persistent
`/data` directory with mode `0600`. They are excluded from the repository and
never printed to logs.

## Required Home Assistant integration

Install the companion custom integration from the Home Assistant configuration
repository and add **AI Usage Monitor** under **Settings → Devices & services**.
It listens for `ai_usage_monitor_update` events and owns these native entities:

- `sensor.ai_usage_monitor_{claude,codex,codex2,zai}_usage_{5h,weekly}`
- `sensor.ai_usage_monitor_{claude,codex,codex2,zai}_usage_{5h,weekly}_reset`

No Home Assistant helpers are required.

## OAuth behavior

Version 0.7.0 requires companion integration 0.3.0+ for dynamic Codex accounts.
Use **Добавить аккаунт** in Ingress, give it a name, then click **Войти** in its
section. Approve the device code while signed into the intended OpenAI account
(use a separate browser profile if needed). The original account remains in
/data/codex_auth.json; additional accounts use /data/codex2_auth.json, etc.
Names and monotonically increasing IDs persist in /data/codex_accounts.json.
Renaming preserves entity IDs. Deleting removes credentials, cancels pending
login, and removes its sensors from HA on the next account sync. The full list
is published every poll so deletion also reconciles after HA downtime.
z.ai monitoring remains available independently of dashboard visibility.

Claude and Codex subscription limits are not the same as organization API usage.
The add-on therefore uses the OAuth credentials created by the official CLIs.
Both providers rotate refresh tokens; a successful refresh is persisted before
the next request. If a refresh token is revoked or expires, re-import a fresh
credential file from the corresponding CLI.

Codex usage is read from the non-consuming `GET /backend-api/wham/usage`
endpoint. Explicit server-side exhaustion is reported as 100% even when the
provider's rounded `used_percent` remains below 100.

z.ai uses a long-lived Coding Plan API key. The add-on sends it only to
`https://api.z.ai/api/monitor/usage/quota/limit` and stores it with mode `0600`.

## Development

Run unit tests from the repository root:

```bash
python -m unittest discover -s tests -v
```
