# Setup

## 1. Install

```bash
git clone https://github.com/DavidCzartoryski/inbox-triage.git
cd inbox-triage
pip install -r requirements.txt
```

Python 3.9+, verified in CI against 3.9 and 3.12. One dependency: `anthropic`.

The test suite needs no account, no API key and no network:

```bash
python tests/test_triage.py
```

## 2. Get an API key

From [console.anthropic.com](https://console.anthropic.com). This is billed per use and is separate from a Claude.ai subscription. At normal student mail volume, expect a few cents a day.

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

## 7. Schedule it

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

`.github/workflows/triage.yml` is included but runs only if you add these repository secrets under Settings → Secrets and variables → Actions:

`ANTHROPIC_API_KEY`, `MAIL_EMAIL`, `MAIL_APP_PASSWORD`, `DIGEST_TO`

Caveats worth knowing before you depend on it:

- Scheduled workflows are **best-effort**. GitHub delays them under load, sometimes by a lot. A 15-minute cron can become 40 minutes at busy times.
- GitHub **disables scheduled workflows** on repositories with no commit activity for 60 days.
- State lives in the Actions cache, which is evicted after 7 days of no access. Eviction means the agent re-scans the last 2 days — it may re-file mail you'd already sorted, but it won't lose anything.
- **Your app password sits in GitHub's secret store.** That's reasonable, but it's one more place it exists. If that bothers you, use launchd on your own machine.
- Make the repo **private** if you go this route.

## 8. Clean out the backlog

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
| `osascript` error -1743 | Automation permission denied. System Settings → Privacy & Security → Automation |
| Mail.app times out | Too many messages for AppleScript. Lower the fetch count or use `MAIL_BACKEND=imap` |
| IMAP auth fails on iCloud | Try the short username (before the `@`), and confirm you're using an app-specific password |
| `basic authentication is disabled` | You're on Outlook. Not supported — see the README |
| Everything lands in `Filtered` | `NEVER_FILTER` is empty and the prompt doesn't know your senders. Run `--dry-run` and tune |
| Nothing gets filed | Check the API key is set; unclassified mail defaults to staying in the inbox |
