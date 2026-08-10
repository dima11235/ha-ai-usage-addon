# AI Usage Monitor

Monitors Claude and Codex subscription limits from the Home Assistant server.

After starting the add-on, open **Web UI** and import the credential files from
the workstation where Claude Code and Codex are already signed in:

- `.claude/.credentials.json`
- `.codex/auth.json`

The files stay in the persistent private storage of this add-on. The default
poll interval is 15 minutes. A failed provider does not block the other provider
and does not overwrite the last successful Home Assistant values.

If the Web UI reports an OAuth refresh error, sign in again with the respective
official CLI and re-import its credential file.
