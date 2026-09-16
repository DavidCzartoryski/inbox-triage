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

## Three layers of protection

Classification is a judgment call, so it isn't the only thing standing between you and a missed deadline.

1. **Allowlist.** Senders matching `NEVER_FILTER` are never filed. Put your university domain, your advisor, and any recruiter you're talking to here.
2. **Keyword override.** Subject lines matching `HARD_KEEP_PATTERNS` stay in the inbox and get flagged even if the classifier said noise. Assessment platform names, "deadline", "expires", "action required", verification codes.
3. **Fail-safe default.** Anything that can't be classified stays put.

## How it works

```
every 15 min ──> fetch new mail ──> classify in batches ──> flag or file
                                          │
                                          └──> queue for digest
8am & 6pm ────> send digest ──────────────┘
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
pip install -r requirements.txt
export MAIL_BACKEND=applescript
export ANTHROPIC_API_KEY=sk-ant-...
export DIGEST_TO=you@icloud.com

python src/icloud_triage.py test              # verify connections
python src/icloud_triage.py triage --dry-run  # classify without acting
```

**Run the dry run for a day or two before letting it act.** It prints every decision and changes nothing. That's when you find out it wants to file your career center's emails, and you add them to `NEVER_FILTER`.

## Tests

```bash
python tests/test_triage.py
```

No account, no API key, no network. Both mail backends are driven through their
real parsers with fake transports, so the tests cover the thing most likely to
break silently: a backend that doesn't hand triage every field it reads.

## Tuning

If it files something you wanted, in order of what to try:

1. Add the sender to `NEVER_FILTER`
2. Add a subject pattern to `HARD_KEEP_PATTERNS`
3. Edit the category descriptions in `SYSTEM_PROMPT` — plain English edits work, and naming actual senders ("Handshake digests", "Canvas notifications") works better than abstract rules

The prompt is the real brain here. It's written for a specific person — a student applying to jobs — and it will get better the more it knows about yours.

## Known limitations

- **It can't read attachments.** An assessment link buried in a PDF will be missed.
- **Threading is ignored.** Each message is judged alone, so a reply deep in a thread loses the context of what came before.
- **First run looks back 2 days only**, capped at 60 messages. The cleanup tool handles everything older.
- **AppleScript mode needs the Mac awake** and Mail.app running. A sleeping laptop means delayed triage.
- **Classification is probabilistic.** The overrides exist because it will be wrong sometimes. Read the digest.

## Roadmap

- Google Apps Script port, for a genuinely serverless deployment with no host at all
- Thread awareness
- Attachment text extraction for assessment links
- A feedback loop: mark something as misfiled and have it update the allowlist automatically

## License

MIT
