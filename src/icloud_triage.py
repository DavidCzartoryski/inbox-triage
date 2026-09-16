#!/usr/bin/env python3
"""
iCloud inbox triage agent.

Connects to iCloud Mail over IMAP, classifies new messages with Claude,
moves low-value mail to a Filtered folder, flags the things that actually
need you, and emails you a digest twice a day.

Usage:
    python icloud_triage.py triage            # classify + file new mail
    python icloud_triage.py triage --dry-run  # classify + print, change nothing
    python icloud_triage.py digest            # send the digest email
    python icloud_triage.py test              # verify credentials only

Design rule: failure is always biased toward your inbox. If the API errors,
the JSON won't parse, or anything is ambiguous, the message is left exactly
where it was. Nothing is ever deleted.
"""

import argparse
import email
import email.utils
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from pathlib import Path

try:
    import anthropic
except ImportError:
    sys.exit("Missing dependency. Run: pip install anthropic")

from mail_backends import get_backend, mask

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


DIGEST_TO = (os.environ.get("DIGEST_TO")
             or os.environ.get("MAIL_EMAIL", ""))
MODEL = os.environ.get("TRIAGE_MODEL", "claude-sonnet-5")
FILTERED_FOLDER = os.environ.get("FILTERED_FOLDER", "Filtered")
STATE_PATH = Path(os.environ.get("TRIAGE_STATE", "state.json"))

# Anything from these senders or domains is never filtered, no matter what
# the model says. Add your school, your advisor, recruiters you trust.
NEVER_FILTER = [s.strip().lower() for s in os.environ.get(
    "NEVER_FILTER", ""
).split(",") if s.strip()]

# If any of these appear in the subject, the message stays in the inbox and
# gets flagged regardless of classification. Cheap insurance.
HARD_KEEP_PATTERNS = [
    r"\bonline assessment\b", r"\bcoding challenge\b", r"\bhackerrank\b",
    r"\bcodesignal\b", r"\bkarat\b", r"\bhirevue\b",
    r"\binterview\b", r"\bschedule a (call|time|chat)\b",
    r"\bnext steps?\b", r"\boffer\b", r"\bexpires?\b", r"\bdeadline\b",
    r"\baction required\b", r"\bplease (complete|confirm|respond|reply)\b",
    r"\bverification code\b", r"\bone[- ]time (code|password)\b",
]

BATCH_SIZE = 8
BODY_CHARS = 1800
MAX_MESSAGES_PER_RUN = 60

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You triage the inbox of a university student who is \
actively applying to jobs and internships. Their problem: real opportunities \
get buried under automated confirmations and course-platform noise, and they \
miss timed assessments.

Classify each email into exactly one category.

ACTION_REQUIRED — something breaks or is lost if they don't act:
  online assessment or coding challenge links (especially timed ones),
  interview scheduling or availability requests, offers, requests for
  documents/transcripts/references, application deadlines, anything from a
  human awaiting a reply, security codes and account verification.

PERSONAL — a real human wrote to this specific person, but nothing is urgent:
  a professor, advisor, recruiter, hiring manager, TA, or classmate writing
  directly. Mass mail merges from a careers office are NOT personal. A real
  recruiter's first outreach IS personal even if it's lightly templated.

FYI — matters, but requires nothing:
  rejections, application status changes, interview confirmations for
  something already scheduled, financial aid or registrar notices.

NOISE — safe to move out of the inbox:
  "we received your application", job-board alerts and recommendations,
  LMS notifications (grade posted, discussion reply, assignment submitted,
  course announcement), newsletters, marketing, receipts, social
  notifications, anything with a List-Unsubscribe header that isn't also
  time-sensitive.

Decision rules:
- When torn between two categories, pick the more important one. A false
  NOISE is far more costly than a false ACTION_REQUIRED.
- A deadline, a timer, or a link that expires always means ACTION_REQUIRED.
- "Your application was received" is NOISE. "Complete this assessment within
  5 days" is ACTION_REQUIRED, even from the same company and same system.
- Being automated does not make something NOISE. Assessment invites are
  almost always automated.

Return ONLY a JSON array, no prose and no markdown fences. One object per
email, in the order given:
[{"uid": "123", "category": "ACTION_REQUIRED", "importance": 0-100,
  "summary": "under 12 words, what it is and what to do",
  "deadline": "the stated deadline or null"}]"""

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            print("State file corrupt; starting fresh.", file=sys.stderr)
    return {"last_uid": 0, "seen": [], "queue": [], "last_digest": None}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------


def decode_str(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def get_body(msg):
    """Plain text preferred; fall back to a crude HTML strip."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                text = part.get_payload(decode=True) or b""
                text = text.decode(part.get_content_charset() or "utf-8", "replace")
                break
            if ctype == "text/html" and not text:
                raw = part.get_payload(decode=True) or b""
                raw = raw.decode(part.get_content_charset() or "utf-8", "replace")
                text = strip_html(raw)
    else:
        raw = msg.get_payload(decode=True) or b""
        raw = raw.decode(msg.get_content_charset() or "utf-8", "replace")
        text = strip_html(raw) if msg.get_content_type() == "text/html" else raw

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()[:BODY_CHARS]


def strip_html(raw):
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return html.unescape(raw)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def hard_keep(msg):
    sender = f"{msg['from']} {msg['reply_to']}".lower()
    if any(s in sender for s in NEVER_FILTER):
        return True
    subject = msg["subject"].lower()
    return any(re.search(p, subject) for p in HARD_KEEP_PATTERNS)


def render_for_model(msg):
    return (
        f"UID: {msg['uid']}\n"
        f"From: {msg['from']}\n"
        f"Reply-To: {msg['reply_to'] or '(none)'}\n"
        f"Subject: {msg['subject']}\n"
        f"Bulk mail headers: {'yes' if msg['has_unsubscribe'] else 'no'}\n"
        f"Body:\n{msg['body'] or '(empty)'}\n"
    )


def classify(client, batch):
    payload = "\n---\n".join(render_for_model(m) for m in batch)
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": payload}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
            results = json.loads(text)
            return {str(r["uid"]): r for r in results if "uid" in r}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"  unparseable response: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"  API error: {exc}", file=sys.stderr)
            time.sleep(2 ** attempt)
    return {}  # fail safe: caller leaves everything in the inbox


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


def run_triage(dry_run=False):
    state = load_state()
    backend = get_backend()
    client = anthropic.Anthropic()

    try:
        if backend.name == "applescript":
            # Mail.app exposes no monotonic UID, so dedupe against recent ids.
            # Keep the fetch small: AppleScript walks messages one at a time.
            seen = set(state.get("seen", []))
            messages = [m for m in backend.fetch_recent(count=25)
                        if m["uid"] not in seen]
        else:
            messages = backend.fetch_since(state["last_uid"])

        if not messages:
            print("No new mail.")
            return

        print(f"Fetched {len(messages)} new message(s) via {backend.name}.")

        filed = kept = skipped = 0

        for i in range(0, len(messages), BATCH_SIZE):
            batch = messages[i:i + BATCH_SIZE]
            verdicts = classify(client, batch)

            for msg in batch:
                v = verdicts.get(msg["uid"])
                if not v:
                    print(f"  [inbox   ] {msg['subject'][:60]}  (unclassified)")
                    skipped += 1
                    continue

                category = v.get("category", "FYI")
                if hard_keep(msg) and category == "NOISE":
                    category = "ACTION_REQUIRED"
                    v["summary"] = "Keyword override — " + v.get("summary", "")

                record = {
                    "uid": msg["uid"],
                    "from": msg["from"],
                    "subject": msg["subject"],
                    "date": msg["date"],
                    "category": category,
                    "importance": v.get("importance", 50),
                    "summary": v.get("summary", ""),
                    "deadline": v.get("deadline"),
                }

                if category in ("ACTION_REQUIRED", "PERSONAL"):
                    if not dry_run:
                        backend.flag(msg["uid"])
                    state["queue"].append(record)
                    kept += 1
                    print(f"  [FLAGGED ] {msg['subject'][:60]}")
                else:
                    if not dry_run:
                        if not backend.move_to_filtered(msg["uid"]):
                            print(f"  move failed, left in inbox: {msg['uid']}",
                                  file=sys.stderr)
                    state["queue"].append(record)
                    filed += 1
                    label = "filed-fyi" if category == "FYI" else "filed    "
                    print(f"  [{label}] {msg['subject'][:60]}")

        if backend.name == "applescript":
            state["seen"] = (state.get("seen", []) +
                             [m["uid"] for m in messages])[-500:]
        else:
            state["last_uid"] = max(int(m["uid"]) for m in messages)
        if dry_run:
            print(f"\nDRY RUN — nothing moved. "
                  f"Would flag {kept}, file {filed}, skip {skipped}.")
        else:
            save_state(state)
            print(f"\nFlagged {kept}, filed {filed}, left alone {skipped}.")
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def build_digest(queue):
    order = {"ACTION_REQUIRED": 0, "PERSONAL": 1, "FYI": 2, "NOISE": 3}
    buckets = {k: [] for k in order}
    for item in queue:
        buckets.get(item["category"], buckets["FYI"]).append(item)

    for items in buckets.values():
        items.sort(key=lambda x: -x.get("importance", 0))

    def esc(s):
        return html.escape(str(s or ""))

    def section(title, items, color, show_detail=True):
        if not items:
            return ""
        rows = []
        for it in items:
            deadline = ""
            if show_detail and it.get("deadline"):
                deadline = (f'<div style="color:#b34700;font-size:13px;'
                            f'margin-top:2px">Deadline: {esc(it["deadline"])}</div>')
            detail = (f'<div style="color:#555;font-size:13px;margin-top:2px">'
                      f'{esc(it.get("summary"))}</div>') if show_detail else ""
            rows.append(
                f'<li style="margin:0 0 14px 0">'
                f'<div style="font-weight:600;font-size:15px">{esc(it["subject"])}</div>'
                f'<div style="color:#777;font-size:13px">{esc(it["from"])}</div>'
                f'{detail}{deadline}</li>'
            )
        return (
            f'<h2 style="font-size:15px;text-transform:uppercase;'
            f'letter-spacing:.06em;color:{color};margin:28px 0 12px;'
            f'padding-bottom:6px;border-bottom:2px solid {color}">'
            f'{title} ({len(items)})</h2>'
            f'<ul style="list-style:none;padding:0;margin:0">{"".join(rows)}</ul>'
        )

    filed_count = len(buckets["NOISE"])
    body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,'
        '\'Helvetica Neue\',sans-serif;max-width:640px;margin:0 auto;'
        'color:#111;line-height:1.5">'
        f'<div style="color:#777;font-size:13px">'
        f'{datetime.now().strftime("%A, %B %-d · %-I:%M %p")}</div>'
        + section("Needs your attention", buckets["ACTION_REQUIRED"], "#c0392b")
        + section("From a person", buckets["PERSONAL"], "#1a5490")
        + section("Worth knowing", buckets["FYI"], "#6b6b6b")
        + (f'<p style="color:#888;font-size:13px;margin-top:28px;'
           f'padding-top:14px;border-top:1px solid #eee">'
           f'{filed_count} routine message(s) moved to '
           f'<strong>{esc(FILTERED_FOLDER)}</strong>. Nothing was deleted — '
           f'search that folder any time.</p>' if filed_count else '')
        + '</div>'
    )

    urgent = len(buckets["ACTION_REQUIRED"])
    if urgent:
        subject = f"Inbox: {urgent} need{'s' if urgent == 1 else ''} action"
    elif buckets["PERSONAL"]:
        subject = f"Inbox: {len(buckets['PERSONAL'])} personal message(s)"
    else:
        subject = "Inbox: nothing urgent"
    return subject, body


def build_plain_digest(queue):
    """Mail.app outgoing messages are plain text only."""
    order = ["ACTION_REQUIRED", "PERSONAL", "FYI"]
    titles = {"ACTION_REQUIRED": "NEEDS YOUR ATTENTION",
              "PERSONAL": "FROM A PERSON", "FYI": "WORTH KNOWING"}
    lines = []
    for cat in order:
        items = sorted((i for i in queue if i["category"] == cat),
                       key=lambda x: -x.get("importance", 0))
        if not items:
            continue
        lines.append(titles[cat])
        lines.append("-" * len(titles[cat]))
        for it in items:
            lines.append(f"* {it['subject']}")
            lines.append(f"  {it['from']}")
            if it.get("summary"):
                lines.append(f"  {it['summary']}")
            if it.get("deadline"):
                lines.append(f"  DEADLINE: {it['deadline']}")
            lines.append("")
        lines.append("")
    filed = sum(1 for i in queue if i["category"] == "NOISE")
    if filed:
        lines.append(f"{filed} routine message(s) moved to {FILTERED_FOLDER}. "
                     f"Nothing was deleted.")
    return "\n".join(lines)


def run_digest():
    state = load_state()
    if not state["queue"]:
        print("Nothing queued; no digest sent.")
        return

    subject, html_body = build_digest(state["queue"])
    plain_body = build_plain_digest(state["queue"])

    backend = get_backend()
    try:
        backend.send(DIGEST_TO, subject, plain_body,
                     html_body if backend.supports_html_digest else None)
    finally:
        backend.close()

    state["queue"] = []
    state["last_digest"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print(f"Digest sent to {DIGEST_TO}: {subject}")


def run_test():
    """Verify mail access and the API key without touching any mail."""
    backend = get_backend()
    print(f"Backend: {backend.name}")
    try:
        if backend.name == "applescript":
            msgs = backend.fetch_recent(count=3)
            print(f"Mail.app OK — read {len(msgs)} message(s), no credentials used.")
            for m in msgs:
                print(f"   {m['subject'][:60]}")
        else:
            backend._connect()
            print(f"IMAP login OK as {backend.address}")
            print(f"Password source: keychain or env, value {mask(backend.password)}")
    finally:
        backend.close()

    client = anthropic.Anthropic()
    client.messages.create(model=MODEL, max_tokens=10,
                           messages=[{"role": "user", "content": "say ok"}])
    print(f"Anthropic API OK (model: {MODEL})")
    print("\nReady. Next: python src/icloud_triage.py triage --dry-run")


def main():
    parser = argparse.ArgumentParser(description="iCloud inbox triage agent")
    parser.add_argument("command", choices=["triage", "digest", "test"])
    parser.add_argument("--dry-run", action="store_true",
                        help="classify and print without moving anything")
    args = parser.parse_args()

    if args.command == "triage":
        run_triage(dry_run=args.dry_run)
    elif args.command == "digest":
        run_digest()
    else:
        run_test()


if __name__ == "__main__":
    main()
