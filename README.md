# inbox-triage

[![CI](https://github.com/DavidCzartoryski/inbox-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/DavidCzartoryski/inbox-triage/actions/workflows/ci.yml)

**An email agent for students in the middle of a job hunt.**

Apply to 100 jobs and you don't get 100 emails. You get 300. A confirmation from the company, an account verification from whatever applicant tracking system they use, a "do not reply to this message" from the ATS itself, then a status update three weeks later. Stack Canvas notifications on top of that — grade posted, assignment submitted, someone replied to your discussion post — plus LinkedIn job alerts and Handshake digests, and your inbox stops being an inbox. It's a feed. And somewhere in that feed is a timed online assessment that expires in five days.

That's the actual failure mode. Not clutter for its own sake — clutter with a real cost, because the one email that mattered looked exactly like the 40 that didn't.

This agent reads every incoming message, decides whether a human needs to see it, and acts on that decision. Assessments, interview scheduling, and emails from actual people stay in your inbox and get flagged. Everything else moves to a folder you can search but never have to look at. Twice a day you get a digest of what came in.

## What it keeps and what it files

| Category | Examples | What happens |
|---|---|---|
| **Action required** | Online assessments and coding challenges, interview scheduling, offers, document requests, deadlines, verification codes | Stays in inbox, flagged, top of the digest |
| **Personal** | A professor, advisor, recruiter, hiring manager, or classmate writing to you specifically | Stays in inbox, flagged, in the digest |
| **FYI** | Rejections, application status changes, registrar and financial aid notices | Filed, listed in the digest |
| **Noise** | "We received your application", account verifications, LMS notifications, discussion replies, job alerts, newsletters | Filed, counted in the digest |

The distinction that matters most is between *automated* and *unimportant*, because they are not the same thing. An assessment invite from HackerRank is automated, no-reply, and templated — and it is the single most important email you will get that week. Volume and tone don't determine importance. Consequence does.

## Nothing is deleted

Filed mail moves to a `Filtered` folder. It stays searchable from every device, forever. There is no delete path anywhere in this codebase.

This is deliberate. An agent that deletes on a misclassification turns a recoverable mistake into an unrecoverable one, and the whole point is to stop missing things. The failure modes are ranked:

1. **Worst:** an assessment gets filed and you miss the window.
2. **Bad:** a rejection stays in your inbox.
3. **Fine:** the `Filtered` folder is bigger than it needs to be.

Every ambiguous call is resolved downward. When the model is unsure it keeps. When the API errors out or returns unparseable JSON, the message is left exactly where it was. When a subject line matches a hard-coded keyword like *assessment* or *interview*, it stays in your inbox regardless of what the classifier decided.

## The settings panel

```bash
python src/ui_server.py
```

A 250×350 window: filter toggles, how often it checks, how often it digests,
and your unsubscribe candidates. Stdlib only — no framework, no build step, no
Electron. It writes `config.json`; the agent reads it on the next run.

```
┌─────────────────────────────┐
│ Triage            3 queued  │
│ Filters │ Timing │ Unsubs   │
├─────────────────────────────┤
│ FILE THESE OUT OF MY INBOX  │
│ ☑ Application confirmations │
│ ☑ Canvas & course platforms │
│ ☑ Job board alerts          │
│ ☑ Social notifications      │
│ ☑ Marketing & promotions    │
│ ☐ News & newsletters        │
│ ☐ Receipts & orders         │
│ ☐ Anything with unsubscribe │
│ ☐ no-reply@ senders  ⚠      │
│                             │
│ NEVER FILE THESE SENDERS    │
│ ┌─────────────────────────┐ │
│ │ @youruniversity.edu     │ │
│ └─────────────────────────┘ │
├─────────────────────────────┤
│ [         Save          ]   │
└─────────────────────────────┘
```

The **Setup** tab takes your Anthropic API key and writes it to the macOS
Keychain — not to a file, and never into the source. The panel can set or
clear it and nothing more: no endpoint returns a stored secret, so the panel's
token can't be used to read your key back out.

```bash
.venv/bin/python src/keystore.py set anthropic-api-key    # or do it in the panel
.venv/bin/python src/keystore.py status                   # where it's coming from
```

`ANTHROPIC_API_KEY` in the environment still wins, so CI and one-off overrides
work unchanged. Keys in source files are the one arrangement to avoid: the code
gets committed, and this repo is public.

It binds `127.0.0.1` only, and every request needs a token minted at startup
and present just in the URL it opens. Localhost alone wouldn't be enough: any
page in your browser can make requests to localhost, and the panel rewrites
your filter rules.

### A warning about the no-reply@ toggle

It's the most obvious rule to want and the most dangerous one to enable.
HackerRank, CodeSignal, Workday, and Greenhouse all send assessment invites
from `no-reply@`. A naive "file all no-reply mail" rule files the exact email
this project exists to catch.

So the rules are layered, highest priority first:

| | Layer | Wins over |
|---|---|---|
| 1 | Your allowlist | everything |
| 2 | Keyword protection — *assessment, interview, deadline, offer, verification code* | rules + model |
| 3 | Your toggles | the model |
| 4 | The model | — |
| 5 | Any failure → stays in inbox | — |

Toggles sit at layer 3, under the keyword protection. That ordering is what
makes the no-reply rule safe to switch on at all, and it's covered by a test
that fails if the layers are ever reordered.

Rules are also free. A message a rule catches is filed without an API call, so
turning toggles on makes the agent both cheaper and more predictable — in a
7-message sample with five rules on, 4 were filed deterministically and only 3
reached the model.

## Unsubscribe candidates

```bash
MAIL_BACKEND=imap python src/subscriptions.py
```

Groups mail across your inbox and `Filtered` by sender, then asks the one
question a mailbox can actually answer: how much does this sender send, and
how much of it do you ever open?

```
LinkedIn Job Alerts <noreply@linkedin.com>
  312 received · opened 4 (1%) · last 2026-09-16
  URL  https://www.linkedin.com/comm/psettings/email-unsubscribe?...

Morning Brew <crew@morningbrew.com>
  118 received · opened 9 (8%) · last 2026-09-16
  MAILTO  unsubscribe@morningbrew.com
```

Ranked by volume you ignore, so 300 unread beats 6 unread. The results show up
in the panel's Unsubs tab.

**It never unsubscribes for you.** It extracts the target from the
`List-Unsubscribe` header and hands you the link. Clicking unsubscribe in mail
you didn't ask for confirms your address is live, and a one-click HTTP
unsubscribe is an outbound action in your name — a `mailto:` target is a plain
request, a `url` target is a tracked endpoint. Which to trust is your call, so
the tool doesn't make it. It prefers showing you the `mailto:` when both exist.

Two honest caveats on the read signal:

- `\Seen` means "opened, **or** scrolled past in a preview pane". A three-pane
  mail client inflates it, so your real read rate may be lower than shown.
- The triage agent reads with `BODY.PEEK` and never sets `\Seen`, so it doesn't
  pollute its own numbers.

## How often it runs

Set it in the panel's Timing tab, then apply it:

```bash
python src/schedule_agent.py install    # writes launchd jobs from config.json
python src/schedule_agent.py status
python src/schedule_agent.py cron       # prints cron lines instead, for Linux
```

Checking for mail and notifying you are separate settings, and they want
opposite answers:

| | Options | Good choice |
|---|---|---|
| **Check for new mail** | 15m · 1h · 4h · 6h · 8h · 12h | **15 minutes** |
| **Email me a digest** | 1× · 2× · 3× · 4× a day | **3× a day** |

Checking every 15 minutes costs the same as checking every 8 hours. You're
billed per email classified, not per check, and the mail arrives either way —
so a slow interval buys you nothing and can leave a timed assessment unflagged
for 8 hours. Space out the *digest* instead. That's the thing that interrupts
you, and 3× a day is about right.

The panel warns you inline when you pick an interval of 8 hours or more.

## Where the protections live in code

Classification is a judgment call, so it isn't the only thing standing between you and a missed deadline.

1. **Allowlist.** The panel's "never file these senders" box, plus the `NEVER_FILTER` environment variable. Substring match on sender and reply-to.
2. **Keyword override.** `HARD_KEEP_PATTERNS` in [`src/icloud_triage.py`](src/icloud_triage.py) — assessment platform names, "deadline", "expires", "action required", verification codes. Edit this list directly; it's plain regex.
3. **Your toggles.** `RULES` in [`src/settings.py`](src/settings.py), also plain regex. Add your own.
4. **Fail-safe default.** Anything that can't be classified stays put.

## How it works

```
                          ┌─ allowlisted or protected keyword? ─> keep, flag
every 15 min ─> fetch ────┼─ matches one of your toggles? ─────> file (free)
                          └─ otherwise ──> ask the model ──────> flag or file
                                                  │
                                                  └──> queue for digest
3× a day ────> send digest ───────────────────────┘
```

Messages are fetched with `BODY.PEEK` (IMAP) or read without setting read status (AppleScript), so **nothing is ever marked as read** by the agent. Your unread count stays honest.

Classification happens in batches of 8 to keep token costs down. Each message contributes roughly 600 tokens. At normal student volume this runs a few cents a day.

## Two backends

Set `MAIL_BACKEND` to choose how the agent reaches your mail.

| | `applescript` | `imap` |
|---|---|---|
| Credentials | **None.** macOS Automation permission is the credential | App-specific password, read from the macOS Keychain |
| Runs on | macOS only, Mail.app must be running | Anything — Mac, Raspberry Pi, VPS, GitHub Actions |
| Speed | Walks messages one at a time; fine for ~25, times out on thousands | Server-side filtering, fast at any mailbox size |
| Digest | Plain text | HTML |
| Best for | Incremental triage | Bulk cleanup, or any non-Mac host |

Default is `applescript`. Having no password on disk is the safer posture, and small-batch incremental triage is exactly the case AppleScript handles well.

The credential handling and the local/cloud split are adapted from [MrGo2/icloud-mcp](https://github.com/MrGo2/icloud-mcp), which documents the AppleScript timeout limitation that shaped the division of labour here.

## Provider support

| Provider | Status |
|---|---|
| iCloud | Supported, both backends |
| Gmail | Supported via IMAP (app password, IMAP enabled in settings) |
| Outlook / Microsoft 365 | **Not supported.** Microsoft requires OAuth2 for IMAP, POP, and SMTP on personal mailboxes; app passwords are refused outright |

If you're on Outlook, the practical route is forwarding to a Gmail or iCloud address.

## Cleaning out the backlog

Separate tool, run once. `inbox_cleanup.py` groups your inbox **by sender** rather than by message — 3,000 emails is usually 150–300 unique senders, so classifying senders costs a fraction of classifying every message and produces a report short enough to actually read.

```bash
python src/inbox_cleanup.py scan     # writes cleanup_plan.md and cleanup_plan.json
# read the report, edit any decision you disagree with
python src/inbox_cleanup.py apply    # executes exactly what the plan says
```

Two phases on purpose. Nothing moves until you've seen the plan. Protected automatically: flagged messages, anything from the last 30 days, anything on your allowlist, and any sender the model failed to classify.

IMAP-only by design — pushing thousands of messages through AppleScript will exceed the Apple Event timeout.

## Setup

See **[docs/SETUP.md](docs/SETUP.md)** for the full walkthrough: credentials, first run, scheduling with launchd or cron, and running it free on GitHub Actions instead of a server.

The short version:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export MAIL_BACKEND=applescript
export ANTHROPIC_API_KEY=sk-ant-...
export DIGEST_TO=you@icloud.com

python src/icloud_triage.py test              # verify connections
python src/ui_server.py                       # pick your filters and cadence
python src/icloud_triage.py triage --dry-run  # classify without acting
python src/schedule_agent.py install          # run it on a timer
```

**Run the dry run for a day or two before letting it act.** It prints every decision and changes nothing. That's when you find out it wants to file your career center's emails, and you add them to the allowlist.

## Tests

```bash
python tests/test_triage.py
```

No account, no API key, no network. Both mail backends are driven through their
real parsers with fake transports, so the tests cover the thing most likely to
break silently: a backend that doesn't hand triage every field it reads.

## Tuning

If it files something you wanted, in order of what to try:

1. Add the sender to the allowlist in the panel
2. Turn off whichever toggle caught it — the digest names the rule that fired
3. Add a subject pattern to `HARD_KEEP_PATTERNS` in [`src/icloud_triage.py`](src/icloud_triage.py)
4. Edit the category descriptions in `SYSTEM_PROMPT` — plain English edits work, and naming actual senders ("Handshake digests", "Canvas notifications") works better than abstract rules

The prompt is the real brain here. It's written for a specific person — a student applying to jobs — and it will get better the more it knows about yours.

Mail filed by a rule says so in the digest (`your filter: job_boards`), so you can always tell a rule decision from a model decision.

## Known limitations

- **It can't read attachments.** An assessment link buried in a PDF will be missed.
- **Threading is ignored.** Each message is judged alone, so a reply deep in a thread loses the context of what came before.
- **First run looks back 2 days only**, capped at 60 messages. The cleanup tool handles everything older.
- **AppleScript mode needs the Mac awake** and Mail.app running. A sleeping laptop means delayed triage.
- **Classification is probabilistic.** The overrides exist because it will be wrong sometimes. Read the digest.
- **The panel is not a running app.** It's a local server you start when you want to change something, not a menu bar item that's always there.
- **`\Seen` is an imperfect read signal.** Preview panes mark mail read, so unsubscribe candidates may be undercounted. Subscription analysis is IMAP-only.

## Roadmap

- Google Apps Script port, for a genuinely serverless deployment with no host at all
- Thread awareness
- Attachment text extraction for assessment links
- A feedback loop: mark something as misfiled and have it update the allowlist automatically
- Menu bar app, so the panel is always a click away instead of a command

## License

MIT
