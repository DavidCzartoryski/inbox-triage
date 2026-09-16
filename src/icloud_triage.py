#!/usr/bin/env python3
"""
Inbox triage agent.

Reads new mail through whichever backend is configured (Mail.app via
AppleScript, or IMAP), files what your filter toggles catch without an API
call, classifies the rest with Claude, flags the things that actually need
you, and emails you a digest on the schedule you picked in the panel.

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
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import anthropic
except ImportError:
    sys.exit(
        "Missing dependency: anthropic.\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -r requirements.txt\n"
        "then run this with .venv/bin/python. A bare `pip install` is refused "
        "by Homebrew\nand system Pythons (PEP 668, externally-managed)."
    )

import keystore
from mail_backends import get_backend, mask
from settings import allowlisted, load_config, matching_rules

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
#
# Split into strong and weak because this layer outranks every filter toggle,
# so a false positive here is the one kind the panel can't fix. Strong terms
# only ever show up in mail that matters. Weak ones are shared with marketing
# copy: "Limited-time college offer ends soon" is not a job offer, and
# "expires" belongs to coupons as often as to assessments.
HARD_KEEP_STRONG = [
    r"\bonline assessment\b", r"\bcoding challenge\b", r"\bhackerrank\b",
    r"\bcodesignal\b", r"\bkarat\b", r"\bhirevue\b",
    r"\binterview\b", r"\bschedule a (call|time|chat)\b",
    r"\bnext steps?\b", r"\bdeadline\b",
    r"\baction required\b", r"\bplease (complete|confirm|respond|reply)\b",
    r"\bverification code\b", r"\bone[- ]time (code|password)\b",
    # Unambiguous offer phrasings, so a real offer still can't be filed.
    r"\boffer letter\b", r"\boffer of (employment|admission|internship)\b",
    r"\b(extend|extending|extended) (you )?an offer\b", r"\byour offer\b",
]

# Weak terms count only when the subject doesn't also read like an ad.
HARD_KEEP_WEAK = [r"\boffer\b", r"\bexpires?\b", r"\blast chance\b"]

MARKETING_VETO = [
    r"limited[\s-]?time", r"offer ends", r"\d+%\s*off", r"\bsale\b",
    r"(exclusive|special|introductory)\s+offer", r"\bcoupon\b", r"\bdeal[s]?\b",
    r"\bsubscribe\b", r"free (trial|shipping)", r"act now", r"don'?t miss",
    r"black friday", r"cyber monday", r"flash sale", r"save big",
    r"\bpromo(tion)?\b", r"buy (one|now)", r"shop now", r"\bwebinar\b",
]

# Kept for anything importing the old name.
HARD_KEEP_PATTERNS = HARD_KEEP_STRONG + HARD_KEEP_WEAK

BATCH_SIZE = 8
BODY_CHARS = 1800
MAX_MESSAGES_PER_RUN = 60
# Bodies per run. AppleScript walks messages one at a time, so this stays
# small on purpose.
APPLESCRIPT_FETCH = 25

# Ids scanned per run to detect new mail. Measured on a 4,046-message unified
# inbox, warm:
#
#     25 ids           4.1s        25 bodies (old path)   14.8s
#     50 ids           8.2s        bodies for 3 new        1.8s
#    150 ids          24.3s        nothing new             0.0s
#
# Cost is dominated by indexing into the mailbox, not by reading bodies, so
# ids are only ~3.7x cheaper per message — not the order of magnitude it looks
# like. That rules out a wide window: 150 ids would cost more than the 25-body
# fetch it replaced. 50 keeps an idle run at ~8s (down from ~15s) while
# covering twice the burst the old code could see.
APPLESCRIPT_ID_WINDOW = 50

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
# Classification
# ---------------------------------------------------------------------------


def next_last_uid(current, seen_uids, unclassified):
    """Highest UID safe to mark as processed.

    Never advances past a message we failed to classify, so an API outage is
    retried on the next run instead of being silently skipped forever. Never
    moves backwards either, or a single bad batch would re-triage the mailbox.
    """
    highest = max(int(u) for u in seen_uids)
    if unclassified:
        highest = min(highest, min(int(u) for u in unclassified) - 1)
    return max(int(current or 0), highest)


def hard_keep(msg):
    # .get throughout: a backend that can't supply a field must not be able to
    # crash triage, because a crash means nothing gets triaged at all.
    sender = f"{msg.get('from', '')} {msg.get('reply_to', '')}".lower()
    if any(s in sender for s in NEVER_FILTER):
        return True
    subject = msg.get("subject", "").lower()

    if any(re.search(p, subject) for p in HARD_KEEP_STRONG):
        return True
    # A weak term in obviously promotional copy isn't protection, it's a
    # marketing email squatting on the word "offer".
    if any(re.search(p, subject) for p in HARD_KEEP_WEAK):
        return not any(re.search(v, subject) for v in MARKETING_VETO)
    return False


def render_for_model(msg):
    return (
        f"UID: {msg.get('uid', '')}\n"
        f"From: {msg.get('from', '(unknown)')}\n"
        f"Reply-To: {msg.get('reply_to') or '(none)'}\n"
        f"Subject: {msg.get('subject', '(no subject)')}\n"
        f"Bulk mail headers: {'yes' if msg.get('has_unsubscribe') else 'no'}\n"
        f"Body:\n{msg.get('body') or '(empty)'}\n"
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
    client = anthropic.Anthropic(api_key=keystore.api_key())

    try:
        if backend.name == "applescript":
            # Mail.app exposes no monotonic UID, so dedupe against recent ids.
            # Two phases: ids are cheap, bodies are not. Scan a wide id window
            # to notice a burst, then pull bodies for only the newest unseen
            # batch. Anything left over stays unseen and is picked up next run,
            # so a burst larger than one batch is delayed, never dropped.
            seen = set(state.get("seen", []))
            new_ids = [i for i in backend.fetch_recent_ids(
                count=APPLESCRIPT_ID_WINDOW) if i not in seen]
            if len(new_ids) > APPLESCRIPT_FETCH:
                print(f"{len(new_ids)} new since last run; taking the newest "
                      f"{APPLESCRIPT_FETCH} now, rest on the next run.")
            messages = backend.fetch_by_ids(new_ids[:APPLESCRIPT_FETCH],
                                            body_chars=BODY_CHARS,
                                            window=APPLESCRIPT_ID_WINDOW)
        else:
            messages = backend.fetch_since(state["last_uid"],
                                           limit=MAX_MESSAGES_PER_RUN,
                                           body_chars=BODY_CHARS)

        if not messages:
            print("No new mail.")
            return

        print(f"Fetched {len(messages)} new message(s) via {backend.name}.")

        filed = kept = skipped = 0
        handled, unclassified = [], []

        # Your toggles run first, but only on mail the protections have already
        # cleared. Anything a rule catches is filed without an API call, which
        # is both free and predictable.
        cfg = load_config()
        to_model = []
        for msg in messages:
            protected = allowlisted(msg, cfg) or hard_keep(msg)
            hits = [] if protected else matching_rules(msg, cfg)
            if not hits:
                to_model.append(msg)
                continue
            handled.append(msg["uid"])
            if not dry_run and not backend.move_to_filtered(msg["uid"]):
                print(f"  move failed, left in inbox: {msg['uid']}",
                      file=sys.stderr)
            state["queue"].append({
                "uid": msg["uid"], "from": msg["from"],
                "subject": msg["subject"], "date": msg["date"],
                "category": "NOISE", "importance": 0,
                "summary": f"your filter: {', '.join(hits)}", "deadline": None,
            })
            filed += 1
            print(f"  [rule    ] {msg['subject'][:52]}  ({hits[0]})")

        if filed:
            print(f"  {filed} filed by your filters, no API call needed.")
        messages_for_model = to_model

        for i in range(0, len(messages_for_model), BATCH_SIZE):
            batch = messages_for_model[i:i + BATCH_SIZE]
            verdicts = classify(client, batch)

            for msg in batch:
                v = verdicts.get(msg["uid"])
                if not v:
                    print(f"  [inbox   ] {msg['subject'][:60]}  (unclassified)")
                    skipped += 1
                    unclassified.append(msg["uid"])
                    continue

                handled.append(msg["uid"])

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

        # Only remember what we actually classified. An API outage leaves the
        # mail in the inbox, and the next run must pick it up again rather than
        # skipping past it forever.
        if backend.name == "applescript":
            state["seen"] = (state.get("seen", []) + handled)[-500:]
        else:
            state["last_uid"] = next_last_uid(
                state.get("last_uid", 0),
                [m["uid"] for m in messages],
                unclassified,
            )
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

    # Check before sending, not after. The digest lists real subject lines and
    # senders, so mailing it to a leftover example address is a disclosure, and
    # unlike a bad filter decision it can't be walked back.
    if not DIGEST_TO or DIGEST_TO in PLACEHOLDERS["DIGEST_TO"]:
        sys.exit(f"Refusing to send: DIGEST_TO is {DIGEST_TO or 'unset'!r}. "
                 "The digest contains your subject lines and senders. Set "
                 "DIGEST_TO to your own address in .env.")

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


# The values shipped in .env.example. Left in place they don't just fail —
# DIGEST_TO would mail a stranger a list of your subject lines.
PLACEHOLDERS = {
    "ANTHROPIC_API_KEY": ("sk-ant-...",),
    "DIGEST_TO": ("you@icloud.com",),
    "MAIL_EMAIL": ("you@icloud.com",),
    "NEVER_FILTER": ("@youruniversity.edu,advisor@,recruiting@",),
}


def config_problems(require_digest_to=True):
    """Unset or still-placeholder settings, as human-readable strings."""
    problems = []
    # keystore.api_key() checks the environment first, then the Keychain, and
    # ignores the shipped placeholder so it can't shadow a real stored key.
    if not keystore.api_key():
        raw = os.environ.get("ANTHROPIC_API_KEY", "")
        if raw and "..." in raw:
            problems.append(
                "ANTHROPIC_API_KEY is still the example value. Get a real key "
                "at https://console.anthropic.com/settings/keys, then either "
                "store it in the Keychain:\n"
                "      .venv/bin/python src/keystore.py set anthropic-api-key\n"
                "    or put it in .env")
        else:
            problems.append(
                "No Anthropic API key found. Store one in the Keychain:\n"
                "      .venv/bin/python src/keystore.py set anthropic-api-key\n"
                "    or set ANTHROPIC_API_KEY in .env and `source .env`")

    if require_digest_to:
        if not DIGEST_TO:
            problems.append("DIGEST_TO is not set")
        elif DIGEST_TO in PLACEHOLDERS["DIGEST_TO"]:
            problems.append(f"DIGEST_TO is still {DIGEST_TO!r}, which is "
                            "someone else's address — set it to yours before "
                            "any digest goes out")

    never = os.environ.get("NEVER_FILTER", "")
    if never in PLACEHOLDERS["NEVER_FILTER"]:
        problems.append("NEVER_FILTER is still the example value — put your "
                        "own school domain in it (warning only)")
    return problems


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

    problems = config_problems()
    blocking = [p for p in problems if "(warning only)" not in p]
    sys.stdout.flush()   # else these stderr lines jump ahead of the mail output
    for p in problems:
        print(f"  ! {p}", file=sys.stderr)
    if blocking:
        sys.exit("\nFix the above in .env, then `source .env` again and re-run.")

    client = anthropic.Anthropic(api_key=keystore.api_key())
    try:
        client.messages.create(model=MODEL, max_tokens=10,
                               messages=[{"role": "user", "content": "say ok"}])
    except anthropic.AuthenticationError:
        # A 20-line traceback for "the key is wrong" teaches nothing.
        sys.exit("Anthropic API rejected the key (401).\n"
                 "  - Check for a copied newline or a trailing character\n"
                 "  - Confirm the key is active at "
                 "https://console.anthropic.com/settings/keys\n"
                 "  - Confirm the workspace has credit")
    except anthropic.APIConnectionError as exc:
        sys.exit(f"Couldn't reach the Anthropic API: {exc}")
    print(f"Anthropic API OK (model: {MODEL})")
    print("\nReady. Next: .venv/bin/python src/icloud_triage.py triage --dry-run")


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
