# AI Usage Monitor — Home Assistant add-on

Home Assistant add-on that monitors the subscription usage limits of Claude and
Codex without relying on a Windows scheduled task.

The add-on:

- polls Claude's 5-hour and weekly usage windows;
- detects Codex rate-limit windows from response headers;
- refreshes and atomically persists rotating OAuth tokens;
- updates the existing `input_number.ai_usage_*` and `input_text.ai_usage_*`
  helpers through the internal Home Assistant API;
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
6. Click **Update now** and check both provider statuses.

Credential files are validated and stored only in the add-on's persistent
`/data` directory with mode `0600`. They are excluded from the repository and
never printed to logs.

## Required Home Assistant entities

The add-on writes to the helpers supplied by the companion Home Assistant
package `packages/modules/ai_usage_limits.yaml`:

- `input_number.ai_usage_{claude,codex}_{5h,weekly}_percent`
- `input_text.ai_usage_{claude,codex}_{5h,weekly}_reset`
- `input_text.ai_usage_{claude,codex}_updated_at`
- `input_text.ai_usage_codex_plan`

## OAuth behavior

Claude and Codex subscription limits are not the same as organization API usage.
The add-on therefore uses the OAuth credentials created by the official CLIs.
Both providers rotate refresh tokens; a successful refresh is persisted before
the next request. If a refresh token is revoked or expires, re-import a fresh
credential file from the corresponding CLI.

## Development

Run unit tests from the repository root:

```bash
python -m unittest discover -s tests -v
```
