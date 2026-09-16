# Setup

## 1. Install

```bash
git clone https://github.com/DavidCzartoryski/inbox-triage.git
cd inbox-triage
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Use a virtualenv, not a bare `pip install`. Homebrew and most system Pythons
are marked externally-managed (PEP 668) and will refuse to install into
themselves:

```
error: externally-managed-environment
```

The venv also pins which interpreter runs the agent, which matters later:
`schedule_agent.py` bakes the current interpreter's path into the launchd job,
so install the schedule from inside the venv and the timer uses the same
Python that has `anthropic`.

Python 3.9+, verified in CI against 3.9 and 3.12. One dependency: `anthropic`.

Every command below assumes the venv is active:

```bash
source .venv/bin/activate
```

If you'd rather not activate it, `.venv/bin/python` in place of `python`
works identically.

The test suite needs no account, no API key and no network:

```bash
python tests/test_triage.py
```

## 2. Get an API key

From [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys). Billed per use, separate from a Claude.ai subscription. At normal student mail volume, expect a few cents a day.

**Never paste a key into a source file.** Anything in the code gets committed, and a key pushed to a public repo is compromised the moment it lands. Two safe places, in order of preference:

```bash
# 1. macOS Keychain — encrypted, can't end up in a diff
.venv/bin/python src/keystore.py set anthropic-api-key   # prompts, no echo

# or set it in the panel's Setup tab, which writes to the same place
```

```bash
# 2. .env — fine, but it's plaintext on disk
export ANTHROPIC_API_KEY="sk-ant-..."
```

Check what's stored, without revealing it:

```bash
.venv/bin/python src/keystore.py status
.venv/bin/python src/keystore.py delete anthropic-api-key
```

`ANTHROPIC_API_KEY` in the environment always wins over the Keychain, so CI secrets and one-off overrides keep working. The shipped placeholder `sk-ant-...` is ignored, so a half-edited `.env` can't shadow a working stored key.

If a key ever does reach a commit, rotate it at the console — deleting the line doesn't help, since the value stays in git history.

## 3. Choose a backend

### AppleScript (recommended on a Mac)

No credentials at all. Mail.app already has your account; macOS Automation permission is what grants access, and you can revoke it in System Settings.

```bash
export MAIL_BACKEND=applescript
export MAIL_ACCOUNT="iCloud"        # the account name shown in Mail.app
```

The first run triggers a macOS prompt asking whether the script may control Mail. Approve it. If you dismiss it, macOS won't ask again — re-enable it under **System Settings → Privacy & Security → Automation**, or reset all Automation decisions with `tccutil reset AppleEvents`.

Create the `Filtered` mailbox in Mail.app by hand before the first real run (Mailbox → New Mailbox).

### IMAP (any host, or for the bulk cleanup)

Needs an app-specific password.

**iCloud:** two-factor auth must be on. Go to [account.apple.com](https://account.apple.com) → Sign-In and Security → App-Specific Passwords. Generate one.

**Gmail:** two-factor auth must be on, and IMAP enabled in Gmail settings. Generate an app password in your Google Account security settings.

**Outlook:** not supported. Microsoft requires OAuth2 for IMAP, POP, and SMTP on personal mailboxes and rejects app passwords.

Store the password in the macOS Keychain rather than a file:

```bash
security add-generic-password -a "you@icloud.com" -s "mail-triage" -w
```

Then:

```bash
export MAIL_BACKEND=imap
export MAIL_EMAIL="you@icloud.com"
export MAIL_PROVIDER=icloud      # or gmail
```

Off macOS, fall back to `MAIL_APP_PASSWORD` in the environment.

## 4. Configure

```bash
cp .env.example .env
$EDITOR .env
chmod 600 .env
source .env
```

Keep the `export` on every line — `source`ing a bare `FOO="bar"` creates a shell
variable that child processes can't see, so the agent would come up unconfigured.
Check it took:

```bash
python -c "import os; print(os.environ['MAIL_BACKEND'], os.environ['DIGEST_TO'])"
```

The setting worth thinking about is `NEVER_FILTER` — a comma-separated list of substrings matched against the sender. Anything matching stays in your inbox no matter what. Put your university domain in it, plus any recruiter you're actively talking to.

## 5. Verify

```bash
python src/icloud_triage.py test
```

Checks mail access and the API key. If IMAP auth fails on iCloud, try `MAIL_EMAIL` as just the part before the `@` — some accounts want the short form for IMAP while SMTP needs the full address.

## 6. Dry run

```bash
python src/icloud_triage.py triage --dry-run
```

Prints every decision, changes nothing. **Do this for a day or two.** Read the output. Every sender it wants to file that you'd rather see goes into `NEVER_FILTER`.

When it looks right, drop `--dry-run`.

## 7. Pick your filters

```bash
python src/ui_server.py
```

Opens a 250×350 panel. Three tabs: **Filters** (what to file), **Timing** (how
often), **Unsubs** (subscriptions worth leaving). Hit Save and it writes
`config.json`, which the agent reads on its next run — no restart needed.

Leave **no-reply@ senders** off unless you have a specific reason. Assessment
invites come from no-reply addresses; the keyword protections are the only
thing standing between that rule and a filed assessment.

The panel binds `127.0.0.1` and requires a token printed at startup, so the URL
is single-use per launch. If you want it without a browser window opening:

```bash
python src/ui_server.py --no-open      # prints the tokenised URL
```

## 8. Schedule it

The panel's Timing tab records your choice; this applies it:

```bash
python src/schedule_agent.py install     # writes and loads launchd jobs
python src/schedule_agent.py status      # what's loaded
python src/schedule_agent.py uninstall   # remove both jobs
python src/schedule_agent.py cron        # print cron lines instead
```

Re-run `install` any time you change the cadence — it rewrites both plists
from `config.json`. Logs land in `logs/`.

Prefer frequent checks and infrequent digests. You're billed per email
classified, not per check, so a 15-minute interval costs the same as a
12-hour one and flags a timed assessment hours sooner. Space out the digest
instead — that's the part that interrupts you.

### Writing the plists by hand

`schedule_agent.py install` is the easy path. If you'd rather see the plists:

### macOS (launchd)

`~/Library/LaunchAgents/com.triage.inbox.plist`, replacing `/full/path/to`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key><string>com.triage.inbox</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>source /full/path/to/.env &amp;&amp; cd /full/path/to &amp;&amp; python3 src/icloud_triage.py triage</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardErrorPath</key><string>/tmp/triage.err</string>
  <key>StandardOutPath</key><string>/tmp/triage.log</string>
</dict>
</plist>
```

`~/Library/LaunchAgents/com.triage.digest.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key><string>com.triage.digest</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>source /full/path/to/.env &amp;&amp; cd /full/path/to &amp;&amp; python3 src/icloud_triage.py digest</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
  </array>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.triage.inbox.plist
launchctl load ~/Library/LaunchAgents/com.triage.digest.plist
```

### Linux (cron)

```cron
*/15 * * * * cd /path/to && . ./.env && python3 src/icloud_triage.py triage >> triage.log 2>&1
0 8,18 * * *  cd /path/to && . ./.env && python3 src/icloud_triage.py digest >> triage.log 2>&1
```

### GitHub Actions (no server)

`.github/workflows/triage.yml` is included, with its schedule **commented out**. Enable it in this order:

1. Add these repository secrets under Settings → Secrets and variables → Actions:
   `ANTHROPIC_API_KEY`, `MAIL_EMAIL`, `MAIL_APP_PASSWORD`, `DIGEST_TO`
2. Run it once by hand from the Actions tab (`workflow_dispatch`) and confirm it succeeds.
3. Uncomment the two `cron` lines in `triage.yml`.

Don't uncomment the schedule first. Without the secrets it runs every 15 minutes, exits immediately, and emails you a failure notice each time — 96 a day.

Caveats worth knowing before you depend on it:

- Scheduled workflows are **best-effort**. GitHub delays them under load, sometimes by a lot. A 15-minute cron can become 40 minutes at busy times.
- GitHub **disables scheduled workflows** on repositories with no commit activity for 60 days.
- State lives in the Actions cache, which is evicted after 7 days of no access. Eviction means the agent re-scans the last 2 days — it may re-file mail you'd already sorted, but it won't lose anything.
- **Your app password sits in GitHub's secret store.** That's reasonable, but it's one more place it exists. If that bothers you, use launchd on your own machine.
- Make the repo **private** if you go this route.

## 9. Find subscriptions worth leaving

```bash
export MAIL_BACKEND=imap
python src/subscriptions.py              # scans INBOX + Filtered
python src/subscriptions.py --days 365   # look back further
```

Writes `unsubscribe_candidates.json`, which the panel's Unsubs tab displays.
IMAP only — it needs per-message `\Seen` flags in bulk, which AppleScript
can't supply at speed.

It lists senders and their unsubscribe links. It does not click them. A
`mailto:` target is a plain request; a `url` target is a tracked one-click
endpoint that also confirms your address is live. Which of those you trust is
your decision, so the tool leaves it to you.

Run the triage agent for a few weeks before relying on this — read rates need
history to mean anything.

## 10. Clean out the backlog

Once, with the IMAP backend:

```bash
export MAIL_BACKEND=imap
python src/inbox_cleanup.py scan --limit 200   # test on the 200 oldest first
python src/inbox_cleanup.py scan               # then the whole inbox
```

Read `cleanup_plan.md`. To override a decision, edit that sender's `action` field in `cleanup_plan.json` to `KEEP` or `ARCHIVE`. The JSON is what `apply` reads.

```bash
python src/inbox_cleanup.py apply
```

Prompts for confirmation, then moves everything marked `ARCHIVE` into `Filtered` in batches of 100. Nothing is deleted.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `error: externally-managed-environment` | You're installing into a Homebrew or system Python. Use the venv from step 1 |
| `Missing dependency: anthropic` | The interpreter you ran isn't the venv's. Use `.venv/bin/python`, or activate the venv |
| Panel says 403 | The token changes every launch. Use the URL the current process printed, not an old one |
| Panel won't open a small window | No Chromium-family browser found; it falls back to your default browser in a normal tab |
| Toggles saved but nothing changed | The agent reads `config.json` on its *next* run. Changing the schedule also needs `schedule_agent.py install` |
| Subscriptions says it needs IMAP | It does — `export MAIL_BACKEND=imap` first |
| An assessment got filed | Check the digest for `your filter:` — if a rule caught it, turn that toggle off and add the sender to the allowlist. If the model did it, add a `HARD_KEEP_PATTERNS` entry |
| `osascript` error -1743 | Automation permission denied. System Settings → Privacy & Security → Automation |
| Mail.app times out | Too many messages for AppleScript. Lower the fetch count or use `MAIL_BACKEND=imap` |
| IMAP auth fails on iCloud | Try the short username (before the `@`), and confirm you're using an app-specific password |
| `basic authentication is disabled` | You're on Outlook. Not supported — see the README |
| Everything lands in `Filtered` | `NEVER_FILTER` is empty and the prompt doesn't know your senders. Run `--dry-run` and tune |
| Nothing gets filed | Check the API key is set; unclassified mail defaults to staying in the inbox |
