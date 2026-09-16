#!/usr/bin/env python3
"""
Bulk inbox cleanup — for the backlog, not for incoming mail.

Two phases, because moving 3,000 messages on a guess is a bad idea.

    python inbox_cleanup.py scan     # read headers, group by sender, propose a plan
    <edit cleanup_plan.json by hand>
    python inbox_cleanup.py apply    # execute exactly what the plan says

The scan groups your inbox by sender and classifies each SENDER, not each
message. 3,000 emails is usually 150-300 unique senders, so this costs about
2% of what classifying every message would, and gives you something you can
actually read and correct before anything moves.

Nothing is deleted. Everything goes to a folder you can search.
Protected by default: flagged messages, anything newer than 30 days, and
anything matching NEVER_FILTER.

Works with iCloud and Gmail. Outlook requires OAuth and is not supported.
"""

import argparse
import email
import email.utils
import imaplib
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path

import keystore

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

PROVIDERS = {
    "icloud": ("imap.mail.me.com", 993),
    "gmail": ("imap.gmail.com", 993),
}

PROVIDER = os.environ.get("MAIL_PROVIDER", "icloud").lower()
if PROVIDER not in PROVIDERS:
    sys.exit(f"MAIL_PROVIDER must be one of: {', '.join(PROVIDERS)}")
IMAP_HOST, IMAP_PORT = PROVIDERS[PROVIDER]

EMAIL = os.environ.get("ICLOUD_EMAIL") or os.environ.get("MAIL_EMAIL", "")
APP_PASSWORD = (os.environ.get("ICLOUD_APP_PASSWORD")
                or os.environ.get("MAIL_APP_PASSWORD", ""))
MODEL = os.environ.get("TRIAGE_MODEL", "claude-sonnet-5")
ARCHIVE_FOLDER = os.environ.get("FILTERED_FOLDER", "Filtered")
PROTECT_DAYS = int(os.environ.get("PROTECT_DAYS", "30"))
PLAN_PATH = Path("cleanup_plan.json")
REPORT_PATH = Path("cleanup_plan.md")

NEVER_FILTER = [s.strip().lower() for s in os.environ.get(
    "NEVER_FILTER", ""
).split(",") if s.strip()]

FETCH_CHUNK = 250
MOVE_CHUNK = 100
SENDERS_PER_CALL = 20

SYSTEM_PROMPT = """You are cleaning out the inbox backlog of a university \
student who applies to a lot of jobs and internships. You are given SENDERS, \
each with how many messages they've sent and a few sample subject lines.

For each sender decide ARCHIVE or KEEP.

ARCHIVE — bulk, automated, or expired. Job-board alerts and recommendations,
"we received your application" confirmations, LMS notifications (grades
posted, discussion replies, assignment receipts, course announcements),
newsletters, marketing, social network notifications, receipts for small
purchases, expired promotions, calendar invite spam, no-reply addresses
sending templated mail.

KEEP — anything a human might need later. Individual people (professors,
advisors, recruiters, hiring managers, classmates), university
administrative mail (registrar, financial aid, student accounts, housing),
employers and prospective employers writing about a specific application,
anything about money owed or received, legal or tax documents, account
security and password resets, medical, housing, immigration or visa matters.

Rules:
- When genuinely unsure, KEEP. A cluttered folder costs nothing; losing a
  financial aid notice costs a lot.
- Volume alone doesn't mean ARCHIVE. A professor who sent 200 emails is KEEP.
- A no-reply address can still be KEEP if the content matters (an employer's
  applicant tracking system sending interview details, for example).
- Judge the sender as a whole from the samples given.

Return ONLY a JSON array, no prose and no markdown fences:
[{"sender": "exact sender string given", "action": "ARCHIVE",
  "category": "job-board alerts", "reason": "under 10 words"}]"""


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------

def connect():
    if not EMAIL or not APP_PASSWORD:
        sys.exit("Set your email and app password env vars first.")
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        imap.login(EMAIL, APP_PASSWORD)
    except imaplib.IMAP4.error:
        imap.login(EMAIL.split("@")[0], APP_PASSWORD)
    return imap


def ensure_folder(imap, name):
    imap.create(f'"{name}"')  # harmless if it already exists


def decode_str(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def fetch_headers(imap, uids):
    """Headers and flags only — fast, and never marks anything read."""
    out = []
    total = len(uids)
    for i in range(0, total, FETCH_CHUNK):
        chunk = uids[i:i + FETCH_CHUNK]
        uid_set = ",".join(str(u) for u in chunk)
        status, data = imap.uid(
            "FETCH", uid_set,
            "(FLAGS BODY.PEEK[HEADER.FIELDS "
            "(FROM SUBJECT DATE LIST-UNSUBSCRIBE)])"
        )
        if status != "OK":
            continue
        pending_flags = ""
        for item in data:
            if isinstance(item, tuple):
                meta = item[0].decode("utf-8", "replace")
                msg = email.message_from_bytes(item[1])
                uid_match = re.search(r"UID (\d+)", meta + pending_flags)
                flags = re.search(r"FLAGS \(([^)]*)\)", meta + pending_flags)
                out.append({
                    "uid": uid_match.group(1) if uid_match else None,
                    "from": decode_str(msg.get("From")),
                    "subject": decode_str(msg.get("Subject")),
                    "date": decode_str(msg.get("Date")),
                    "flagged": "\\Flagged" in (flags.group(1) if flags else ""),
                    "bulk": bool(msg.get("List-Unsubscribe")),
                })
            elif isinstance(item, bytes):
                pending_flags = item.decode("utf-8", "replace")
        sys.stdout.write(f"\r  read headers: {min(i + FETCH_CHUNK, total)}/{total}")
        sys.stdout.flush()
    print()
    return [m for m in out if m["uid"]]


def move_uids(imap, uids, folder):
    moved = 0
    for i in range(0, len(uids), MOVE_CHUNK):
        chunk = ",".join(str(u) for u in uids[i:i + MOVE_CHUNK])
        ok = False
        try:
            status, _ = imap.uid("MOVE", chunk, f'"{folder}"')
            ok = status == "OK"
        except imaplib.IMAP4.error:
            ok = False
        if not ok:
            status, _ = imap.uid("COPY", chunk, f'"{folder}"')
            if status == "OK":
                imap.uid("STORE", chunk, "+FLAGS", "(\\Deleted)")
                imap.expunge()
                ok = True
        if ok:
            moved += len(uids[i:i + MOVE_CHUNK])
        time.sleep(0.3)  # be polite to the server
    return moved


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def msg_age_days(date_str):
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 9999  # undateable mail is treated as old, but still classified


def sender_key(from_header):
    addr = parseaddr(from_header)[1].lower()
    return addr or from_header.lower()


def classify_senders(client, groups):
    keys = list(groups)
    verdicts = {}
    for i in range(0, len(keys), SENDERS_PER_CALL):
        batch = keys[i:i + SENDERS_PER_CALL]
        lines = []
        for k in batch:
            g = groups[k]
            samples = "; ".join(s[:70] for s in g["subjects"][:3])
            lines.append(
                f"Sender: {k}\nDisplay name: {g['display']}\n"
                f"Message count: {g['count']}\n"
                f"Bulk headers: {'yes' if g['bulk'] else 'no'}\n"
                f"Sample subjects: {samples}\n"
            )
        payload = "\n---\n".join(lines)

        parsed = None
        for attempt in range(3):
            try:
                resp = client.messages.create(
                    model=MODEL, max_tokens=3000,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": payload}],
                )
                text = "".join(b.text for b in resp.content if b.type == "text")
                text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
                parsed = json.loads(text)
                break
            except Exception as exc:
                print(f"\n  retry ({exc})", file=sys.stderr)
                time.sleep(2 ** attempt)

        for r in (parsed or []):
            s = str(r.get("sender", "")).lower()
            if s in groups:
                verdicts[s] = r
        sys.stdout.write(f"\r  classified senders: "
                         f"{min(i + SENDERS_PER_CALL, len(keys))}/{len(keys)}")
        sys.stdout.flush()
    print()
    return verdicts


def run_scan(limit=None):
    imap = connect()
    client = anthropic.Anthropic(api_key=keystore.api_key())
    try:
        imap.select("INBOX")
        status, data = imap.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            print("Inbox is empty.")
            return
        uids = [int(u) for u in data[0].split()]
        if limit:
            uids = sorted(uids)[:limit]
        print(f"Inbox has {len(uids)} message(s).")

        messages = fetch_headers(imap, uids)

        groups = defaultdict(lambda: {"count": 0, "uids": [], "subjects": [],
                                      "display": "", "bulk": False})
        protected = 0
        for m in messages:
            if m["flagged"] or msg_age_days(m["date"]) < PROTECT_DAYS:
                protected += 1
                continue
            key = sender_key(m["from"])
            if any(n in m["from"].lower() for n in NEVER_FILTER):
                protected += 1
                continue
            g = groups[key]
            g["count"] += 1
            g["uids"].append(m["uid"])
            g["display"] = g["display"] or m["from"]
            g["bulk"] = g["bulk"] or m["bulk"]
            if len(g["subjects"]) < 5 and m["subject"]:
                g["subjects"].append(m["subject"])

        print(f"{protected} message(s) protected (flagged, recent, or allowlisted).")
        print(f"{len(groups)} unique sender(s) to classify.\n")

        verdicts = classify_senders(client, groups)

        plan = []
        for key, g in sorted(groups.items(), key=lambda kv: -kv[1]["count"]):
            v = verdicts.get(key, {})
            plan.append({
                "sender": key,
                "display": g["display"],
                "count": g["count"],
                # Unclassified senders default to KEEP — fail toward your inbox.
                "action": v.get("action", "KEEP"),
                "category": v.get("category", "unclassified"),
                "reason": v.get("reason", "no verdict returned; kept by default"),
                "sample_subjects": g["subjects"][:3],
                "uids": g["uids"],
            })

        PLAN_PATH.write_text(json.dumps(plan, indent=2))
        write_report(plan, protected)

        arch = sum(p["count"] for p in plan if p["action"] == "ARCHIVE")
        keep = sum(p["count"] for p in plan if p["action"] != "ARCHIVE")
        print(f"\nPlan written to {PLAN_PATH} and {REPORT_PATH}")
        print(f"  archive: {arch} message(s) across "
              f"{sum(1 for p in plan if p['action'] == 'ARCHIVE')} senders")
        print(f"  keep:    {keep} message(s)")
        print(f"\nRead {REPORT_PATH}. To change a decision, edit the \"action\" "
              f"field in {PLAN_PATH}, then run: python inbox_cleanup.py apply")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def write_report(plan, protected):
    archive = [p for p in plan if p["action"] == "ARCHIVE"]
    keep = [p for p in plan if p["action"] != "ARCHIVE"]

    def table(rows):
        out = ["| Count | Sender | Why |", "|---:|---|---|"]
        for p in rows:
            name = p["display"].replace("|", "/")[:55]
            out.append(f"| {p['count']} | {name} | {p['reason']} |")
        return "\n".join(out)

    body = [
        "# Inbox cleanup plan",
        "",
        f"Generated {datetime.now().strftime('%B %-d, %Y at %-I:%M %p')}",
        "",
        f"- **{sum(p['count'] for p in archive)}** messages proposed for "
        f"archiving, from {len(archive)} senders",
        f"- **{sum(p['count'] for p in keep)}** messages staying put",
        f"- **{protected}** messages untouched (flagged, newer than "
        f"{PROTECT_DAYS} days, or on your allowlist)",
        "",
        "Nothing is deleted. Archived mail moves to the "
        f"`{ARCHIVE_FOLDER}` folder and stays searchable.",
        "",
        "## Proposed for archive",
        "",
        table(archive) if archive else "_Nothing._",
        "",
        "## Staying in your inbox",
        "",
        table(keep) if keep else "_Nothing._",
        "",
        "---",
        "",
        "To override any decision, edit the `action` field for that sender in "
        "`cleanup_plan.json` (`ARCHIVE` or `KEEP`), then run "
        "`python inbox_cleanup.py apply`.",
    ]
    REPORT_PATH.write_text("\n".join(body))


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def run_apply(assume_yes=False):
    if not PLAN_PATH.exists():
        sys.exit("No cleanup_plan.json found. Run the scan first.")
    plan = json.loads(PLAN_PATH.read_text())
    targets = [p for p in plan if p["action"] == "ARCHIVE"]
    total = sum(p["count"] for p in targets)

    if not total:
        print("Plan archives nothing. Done.")
        return

    print(f"About to move {total} message(s) from {len(targets)} senders "
          f"into '{ARCHIVE_FOLDER}'.")
    print("Nothing will be deleted; all of it stays searchable.")
    if not assume_yes:
        if input("Proceed? [y/N] ").strip().lower() != "y":
            print("Cancelled. Nothing moved.")
            return

    imap = connect()
    try:
        imap.select("INBOX")
        ensure_folder(imap, ARCHIVE_FOLDER)
        moved = 0
        for p in targets:
            n = move_uids(imap, p["uids"], ARCHIVE_FOLDER)
            moved += n
            print(f"  {n:>5} moved · {p['display'][:50]}")
        print(f"\nDone. {moved} message(s) moved to '{ARCHIVE_FOLDER}'.")
        if moved < total:
            print(f"{total - moved} could not be moved (likely already gone "
                  f"or moved by hand). They were left alone.")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Bulk inbox cleanup")
    ap.add_argument("command", choices=["scan", "apply"])
    ap.add_argument("--limit", type=int,
                    help="only scan the N oldest messages (for testing)")
    ap.add_argument("--yes", action="store_true", help="skip confirmation")
    args = ap.parse_args()

    if args.command == "scan":
        run_scan(limit=args.limit)
    else:
        run_apply(assume_yes=args.yes)


if __name__ == "__main__":
    main()
