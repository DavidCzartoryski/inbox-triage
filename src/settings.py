#!/usr/bin/env python3
"""
User-editable configuration: filter toggles and how often the agent runs.

Lives in config.json next to the repo. The settings panel writes it, the
triage agent reads it. Pure functions only, so the rule engine can be tested
without a mailbox.

Rule precedence, highest first:

    1. allowlist          — never filed, whatever else matches
    2. keyword protection — assessment / interview / deadline / offer / code
    3. your toggles       — deterministic, free, no API call
    4. the model          — judgment call on whatever is left
    5. any failure        — stays in the inbox

Toggles sit BELOW keyword protection on purpose. "Filter no-reply@" is the
most requested rule and the most dangerous one, because assessment invites
are almost always sent from a no-reply address. Putting the toggles under the
keyword layer is what makes the rule safe to switch on.
"""

import json
import os
import re
from pathlib import Path

CONFIG_PATH = Path(os.environ.get(
    "TRIAGE_CONFIG", Path(__file__).resolve().parents[1] / "config.json"))

# ---------------------------------------------------------------------------
# Refresh cadence
# ---------------------------------------------------------------------------

# Triage frequency barely affects cost — you pay per message classified, not
# per run — so checking often is close to free and is what catches a timed
# assessment quickly. The digest is the thing worth spacing out.
REFRESH_CHOICES = [
    {"id": "15m", "label": "Every 15 minutes", "minutes": 15,
     "note": "Recommended. Fastest catch, and costs the same as any other."},
    {"id": "1h", "label": "Hourly", "minutes": 60, "note": ""},
    {"id": "4h", "label": "Every 4 hours", "minutes": 240, "note": ""},
    {"id": "6h", "label": "4× a day (every 6h)", "minutes": 360, "note": ""},
    {"id": "8h", "label": "3× a day (every 8h)", "minutes": 480,
     "note": "An assessment can sit unflagged for up to 8 hours."},
    {"id": "12h", "label": "2× a day (every 12h)", "minutes": 720,
     "note": "An assessment can sit unflagged for up to 12 hours."},
]

# How much of the work the non-AI layer is allowed to do. This is a real
# tradeoff and not ours to decide for someone: reading everything with the
# model is more accurate and costs more, and some people would rather pay than
# risk a heuristic filing something.
#
# "off" does not disable the protections or your own toggles — those are yours
# and cost nothing. It disables only the scored guessing.
PREFILTER_CHOICES = [
    {"id": "off", "threshold": None,
     "label": "AI reads every email",
     "note": "Most accurate, most expensive. Every message that isn't caught "
             "by your own toggles gets its body read and sent to the model."},
    {"id": "balanced", "threshold": 5,
     "label": "Skip the obvious bulk (recommended)",
     "note": "Files mail carrying unsubscribe and campaign headers without "
             "asking the model. On a real inbox this cut tokens ~92%."},
    {"id": "aggressive", "threshold": 4,
     "label": "Skip more, pay less",
     "note": "Lower bar for filing. Cheaper and faster, and more likely to "
             "file something you'd have wanted to see."},
]

DIGEST_CHOICES = [
    {"id": "1", "label": "Once a day", "hours": [8]},
    {"id": "2", "label": "Twice a day", "hours": [8, 18]},
    {"id": "3", "label": "3× a day", "hours": [8, 13, 18]},
    {"id": "4", "label": "4× a day", "hours": [8, 12, 16, 20]},
]

# ---------------------------------------------------------------------------
# Filter catalog
# ---------------------------------------------------------------------------

# Each rule matches on the sender, the subject, or bulk-mail headers. Keep the
# patterns readable — you are going to want to edit these.
RULES = [
    {
        "id": "application_confirmations",
        "label": "Application confirmations",
        "hint": "\"We received your application\", ATS auto-replies",
        "default": True,
        "subject": r"(we (have )?received your application|application (was )?"
                   r"received|thank you for (your interest|applying)|"
                   r"application (has been )?submitted|your application to)",
    },
    {
        "id": "lms",
        "label": "Canvas & course platforms",
        "hint": "Grade posted, discussion replies, assignment submitted",
        "default": True,
        "sender": r"(instructure|canvas|blackboard|moodle|gradescope|piazza|"
                  r"brightspace|turnitin|mymathlab|cengage|mcgraw)",
    },
    {
        "id": "job_boards",
        "label": "Job board alerts",
        "hint": "LinkedIn, Indeed, Handshake, ZipRecruiter digests",
        "default": True,
        "sender": r"(linkedin|indeed|glassdoor|ziprecruiter|handshake|"
                  r"joinhandshake|monster|dice\.com|wellfound|angellist|"
                  r"builtin|simplyhired|lever\.co|jobalerts)",
        "subject": r"(jobs? (for|matching) you|new jobs?|job alert|"
                   r"\d+ new (jobs?|opportunities)|recommended for you|"
                   r"jobs you may be interested)",
        "match": "both",   # sender AND subject, so real recruiter mail survives
    },
    {
        "id": "social",
        "label": "Social notifications",
        "hint": "Likes, follows, connection requests, mentions",
        "default": True,
        "sender": r"(facebookmail|instagram|twitter|notify@x\.com|tiktok|"
                  r"reddit|discord|snapchat|pinterest|quora|medium\.com)",
    },
    {
        "id": "marketing",
        "label": "Marketing & promotions",
        "hint": "Sales, discounts, webinars, product announcements",
        "default": True,
        # Hyphens matter here: real subject lines say "Limited-time offer",
        # not "limited time offer".
        "subject": r"(\d+%\s*off|sale ends|last chance|limited[\s-]?time|"
                   r"free (trial|webinar)|register now|don'?t miss|"
                   r"(exclusive|special|introductory)\s+offer|offer ends|"
                   r"black friday|cyber monday|flash sale|shop now|"
                   r"save big|act now|promo code)",
    },
    {
        "id": "news",
        "label": "News & newsletters",
        "hint": "Morning Brew, Substack, NYT, Axios, The Skimm",
        "default": False,
        "sender": r"(morningbrew|substack|nytimes|wsj\.com|washingtonpost|"
                  r"cnn\.com|bbc\.co|axios|politico|bloomberg|theskimm|"
                  r"thehustle|reuters|apnews|newsletter@|briefing@)",
    },
    {
        "id": "receipts",
        "label": "Receipts & orders",
        "hint": "Order confirmations, invoices, shipping updates",
        "default": False,
        "subject": r"(your (order|receipt|invoice)|order (confirmation|"
                   r"#?\d+)|has shipped|out for delivery|payment received|"
                   r"thanks for your (order|purchase))",
    },
    {
        "id": "bulk_mail",
        "label": "Anything with an unsubscribe link",
        "hint": "Very broad. Catches most mass mail, and some you want.",
        "default": False,
        "bulk": True,
    },
    {
        "id": "no_reply",
        "label": "no-reply@ senders",
        "hint": "DANGEROUS. Assessment invites come from no-reply "
                "addresses — the keyword protections are the only thing "
                "keeping this rule from filing one.",
        "default": False,
        "sender": r"(no[-_.]?reply|donotreply|do[-_.]?not[-_.]?reply|"
                  r"noreply|automated@|notification[s]?@|mailer-daemon)",
    },
]

DEFAULT_CONFIG = {
    "refresh": "15m",
    "prefilter": "balanced",
    "digests_per_day": "3",
    "filters": {r["id"]: r["default"] for r in RULES},
    "allowlist": [],
    "unsubscribe": {
        "min_messages": 5,      # fewer than this isn't a subscription
        "max_read_rate": 0.2,   # you open 20% or less of what they send
        "window_days": 120,
    },
}


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_config(path=None):
    """Config from disk, merged onto the defaults. Never raises."""
    path = Path(path or CONFIG_PATH)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if not path.exists():
        return cfg
    try:
        stored = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return cfg  # a corrupt config must not stop triage
    for key, value in stored.items():
        if key == "filters" and isinstance(value, dict):
            # Keep unknown ids out, so a stale config can't enable a rule
            # that no longer exists.
            cfg["filters"].update(
                {k: bool(v) for k, v in value.items() if k in cfg["filters"]})
        elif key in cfg:
            cfg[key] = value
    return cfg


def save_config(cfg, path=None):
    path = Path(path or CONFIG_PATH)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(path)
    return path


def refresh_minutes(cfg):
    choice = cfg.get("refresh", "15m")
    for c in REFRESH_CHOICES:
        if c["id"] == choice:
            return c["minutes"]
    return 15


def prefilter_threshold(cfg):
    """Score needed to file without the model, or None when the layer is off."""
    choice = cfg.get("prefilter", "balanced")
    for c in PREFILTER_CHOICES:
        if c["id"] == choice:
            return c["threshold"]
    return 5


def digest_hours(cfg):
    choice = str(cfg.get("digests_per_day", "2"))
    for c in DIGEST_CHOICES:
        if c["id"] == choice:
            return c["hours"]
    return [8, 18]


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------

def _rule_matches(rule, sender, subject, bulk):
    if rule.get("bulk"):
        return bool(bulk)
    sender_hit = bool(re.search(rule["sender"], sender)) if rule.get("sender") else None
    subject_hit = bool(re.search(rule["subject"], subject)) if rule.get("subject") else None
    if rule.get("match") == "both":
        return bool(sender_hit and subject_hit)
    return bool(sender_hit or subject_hit)


def matching_rules(msg, cfg):
    """Ids of every enabled rule this message trips. Cheap and deterministic."""
    sender = f"{msg.get('from', '')} {msg.get('reply_to', '')}".lower()
    subject = (msg.get("subject") or "").lower()
    bulk = msg.get("has_unsubscribe")
    enabled = cfg.get("filters", {})
    return [r["id"] for r in RULES
            if enabled.get(r["id"]) and _rule_matches(r, sender, subject, bulk)]


def allowlisted(msg, cfg):
    sender = f"{msg.get('from', '')} {msg.get('reply_to', '')}".lower()
    entries = [e.strip().lower() for e in cfg.get("allowlist", []) if e.strip()]
    return any(e in sender for e in entries)


if __name__ == "__main__":
    cfg = load_config()
    print(f"config: {CONFIG_PATH}")
    print(f"refresh: every {refresh_minutes(cfg)} min")
    print(f"digest hours: {digest_hours(cfg)}")
    on = [k for k, v in cfg["filters"].items() if v]
    print(f"filters on ({len(on)}): {', '.join(on) or 'none'}")
