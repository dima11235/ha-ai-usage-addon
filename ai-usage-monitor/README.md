# AI Usage Monitor

Monitors Claude, Codex, and z.ai subscription limits from Home Assistant.

After starting the add-on, open **Web UI** and import the credential files from
the workstation where Claude Code and Codex are already signed in, then enter
the z.ai Coding Plan key:

- `.claude/.credentials.json`
- `.codex/auth.json`
- z.ai Coding Plan API key (paste it into the password field)

For Claude, press **Войти в Claude** in the Web UI instead of importing a file:
approve the sign-in on the page that opens and paste the code it shows. The
add-on then owns a separate OAuth grant. A copy of the workstation
`.credentials.json` keeps sharing the refresh token with the CLI, and Claude
rotates it on every refresh, so whichever side refreshes second fails with
`invalid_grant`. File import remains as a fallback.

For Codex, press **Войти в Codex**: open the OpenAI page it links to and enter
the short code shown in the panel; the add-on waits for the approval itself.
If OpenAI reports that device code login is disabled, enable device code
authorization for Codex in the ChatGPT security settings.

The credentials stay in the persistent private storage of this add-on. The
default poll interval is 2 minutes, with an extra poll just after the nearest
known reset. A failed provider does not block the other providers and does not
overwrite the last successful Home Assistant values.

The companion **AI Usage Monitor** custom integration must be installed and
configured in Home Assistant. The add-on publishes updates for all three
providers through the internal Home Assistant event API; no helpers are required.

If the Web UI reports an OAuth refresh error, sign in again with the respective
official CLI and re-import its credential file.
