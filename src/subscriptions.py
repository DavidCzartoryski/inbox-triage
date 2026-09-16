#!/usr/bin/env python3
"""
Find subscriptions worth leaving.

Groups mail by sender across your inbox and the Filtered folder, then asks a
question the mailbox can actually answer: how much does this sender send, and
how much of it do you ever open? A sender at 87 messages and a 2% read rate is
a subscription you have already stopped reading.

    python src/subscriptions.py            # report to stdout + JSON
    python src/subscriptions.py --json     # JSON only

IMAP only — it needs the \\Seen flag per message, which AppleScript doesn't
expose usefully in bulk.

It never unsubscribes for you. It extracts the unsubscribe target from the
List-Unsubscribe header and hands it to you. Clicking an unsubscribe link in
mail you didn't ask for confirms a live address, and a one-click HTTP
unsubscribe is an outbound action taken in your name; deciding that is yours.

Two caveats on the read signal:
  - \\Seen means "opened, or scrolled past in a preview pane". A three-pane
    mail client inflates it, so the real read rate may be lower than shown.
  - The triage agent reads with BODY.PEEK and never sets \\Seen, so it does
    not pollute the numbers.
"""

import argparse
import email
import json
import re
import sys
from collections import defaultdict
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mail_backends import get_backend
from settings import load_config

FETCH_CHUNK = 300


def decode_str(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def sender_key(from_header):
    return (parseaddr(from_header)[1] or from_header).strip().lower()


def unsubscribe_target(raw_header):
    """Prefer a mailto: — it's a request, not a tracked one-click endpoint."""
    if not raw_header:
        return None, None
    mailto = re.search(r"<mailto:([^>]+)>", raw_header)
    if mailto:
        return "mailto", mailto.group(1)
    url = re.search(r"<(https?://[^>]+)>", raw_header)
    if url:
        return "url", url.group(1)
    return None, None


def scan_folder(imap, folder, since_days):
    """Headers + flags for one folder. Never marks anything read."""
    status, _ = imap.select(f'"{folder}"', readonly=True)
    if status != "OK":
        print(f"  (skipping {folder}: not found)", file=sys.stderr)
        return []

    since = (datetime.now(timezone.utc) - timedelta(days=since_days))
    status, data = imap.uid("SEARCH", None,
                            f'SINCE {since.strftime("%d-%b-%Y")}')
    if status != "OK" or not data or not data[0]:
        return []
    uids = data[0].split()

    out = []
    for i in range(0, len(uids), FETCH_CHUNK):
        chunk = b",".join(uids[i:i + FETCH_CHUNK]).decode()
        status, data = imap.uid(
            "FETCH", chunk,
            "(FLAGS BODY.PEEK[HEADER.FIELDS "
            "(FROM SUBJECT DATE LIST-UNSUBSCRIBE)])")
        if status != "OK":
            continue
        pending = ""
        for item in data:
            if isinstance(item, tuple):
                meta = item[0].decode("utf-8", "replace")
                msg = email.message_from_bytes(item[1])
                flags = re.search(r"FLAGS \(([^)]*)\)", meta + pending)
                flagstr = flags.group(1) if flags else ""
                out.append({
                    "from": decode_str(msg.get("From")),
                    "subject": decode_str(msg.get("Subject")),
                    "date": msg.get("Date"),
                    "seen": "\\Seen" in flagstr,
                    "unsub": msg.get("List-Unsubscribe"),
                    "folder": folder,
                })
            elif isinstance(item, bytes):
                pending = item.decode("utf-8", "replace")
        sys.stderr.write(f"\r  {folder}: {min(i + FETCH_CHUNK, len(uids))}"
                         f"/{len(uids)}")
        sys.stderr.flush()
    sys.stderr.write("\n")
    return out


def analyze(messages, cfg):
    """Group by sender and score how skippable each one is."""
    rules = cfg.get("unsubscribe", {})
    min_messages = rules.get("min_messages", 5)
    max_read = rules.get("max_read_rate", 0.2)

    groups = defaultdict(lambda: {
        "count": 0, "read": 0, "display": "", "subjects": [],
        "unsub_kind": None, "unsub_target": None, "last": None,
    })

    for m in messages:
        key = sender_key(m["from"])
        if not key:
            continue
        g = groups[key]
        g["count"] += 1
        g["read"] += 1 if m["seen"] else 0
        g["display"] = g["display"] or m["from"]
        if len(g["subjects"]) < 3 and m["subject"]:
            g["subjects"].append(m["subject"])
        if not g["unsub_target"]:
            kind, target = unsubscribe_target(m["unsub"])
            g["unsub_kind"], g["unsub_target"] = kind, target
        try:
            when = parsedate_to_datetime(m["date"])
            if when and (g["last"] is None or when > g["last"]):
                g["last"] = when
        except (TypeError, ValueError):
            pass

    candidates = []
    for key, g in groups.items():
        if g["count"] < min_messages:
            continue
        read_rate = g["read"] / g["count"]
        if read_rate > max_read:
            continue
        if not g["unsub_target"]:
            continue    # nothing to act on; the cleanup tool handles these
        candidates.append({
            "sender": key,
            "display": g["display"],
            "count": g["count"],
            "read": g["read"],
            "read_rate": round(read_rate, 3),
            "unread": g["count"] - g["read"],
            "unsub_kind": g["unsub_kind"],
            "unsub_target": g["unsub_target"],
            "last_seen": g["last"].date().isoformat() if g["last"] else None,
            "sample_subjects": g["subjects"],
            # Volume you ignore. 40 unread beats 6 unread, and a sender you
            # sometimes read ranks below one you never do.
            "score": round(g["count"] * (1 - read_rate), 1),
        })
    candidates.sort(key=lambda c: -c["score"])
    return candidates


def render(candidates, scanned):
    if not candidates:
        return (f"Scanned {scanned} messages. No clear unsubscribe candidates "
                f"— nothing sends you a lot that you never open.\n")
    lines = [
        f"# Unsubscribe candidates",
        "",
        f"Scanned {scanned} messages. {len(candidates)} sender(s) send you a "
        f"lot and you rarely open any of it.",
        "",
        "Nothing here has been acted on. Unsubscribing is your call — a "
        "`mailto:` target is a plain request, a `url` target is a tracked "
        "one-click endpoint that also confirms your address is live.",
        "",
    ]
    for c in candidates:
        pct = int(c["read_rate"] * 100)
        lines += [
            f"## {c['display']}",
            f"- **{c['count']} messages**, you opened **{c['read']}** ({pct}%)",
            f"- Last received: {c['last_seen'] or 'unknown'}",
            f"- Unsubscribe ({c['unsub_kind']}): `{c['unsub_target']}`",
        ]
        if c["sample_subjects"]:
            lines.append(f"- Recent: {c['sample_subjects'][0]}")
        lines.append("")
    return "\n".join(lines)


def run(as_json=False, window_days=None):
    cfg = load_config()
    window = window_days or cfg.get("unsubscribe", {}).get("window_days", 120)
    backend = get_backend()
    if backend.name != "imap":
        sys.exit("Subscriptions analysis needs MAIL_BACKEND=imap "
                 "(it reads per-message \\Seen flags in bulk).")

    try:
        imap = backend._connect()
        folders = ["INBOX", backend.filtered]
        messages = []
        for f in folders:
            messages += scan_folder(imap, f, window)
    finally:
        backend.close()

    candidates = analyze(messages, cfg)
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "scanned": len(messages),
        "window_days": window,
        "candidates": candidates,
    }
    Path("unsubscribe_candidates.json").write_text(json.dumps(payload, indent=2))

    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(render(candidates, len(messages)))
        print("Written to unsubscribe_candidates.json")


def main():
    p = argparse.ArgumentParser(description="Find subscriptions worth leaving")
    p.add_argument("--json", action="store_true", help="JSON to stdout")
    p.add_argument("--days", type=int, help="how far back to look")
    a = p.parse_args()
    run(as_json=a.json, window_days=a.days)


if __name__ == "__main__":
    main()
