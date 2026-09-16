#!/usr/bin/env python3
"""
The things that must never be filed, in one place.

This lived in icloud_triage, which was fine while the triage loop was the only
caller. It isn't any more: the header prefilter also needs it, and needs it as
an internal guarantee rather than something the caller is trusted to check
first. A prefilter that files "Complete your online assessment" whenever the
caller forgets to pass protected=True is a loaded gun, even if today's caller
happens to hold it correctly.

So: one module, imported by both, and every layer that can file consults it.

Split into strong and weak because this outranks every filter toggle, so a
false positive here is the one kind a user can't fix from the panel. Strong
terms only ever show up in mail that matters. Weak ones are shared with
marketing copy — "Limited-time college offer ends soon" is not a job offer.
"""

import re

HARD_KEEP_STRONG = [
    r"\bonline assessment\b", r"\bcoding challenge\b", r"\bhackerrank\b",
    r"\bcodesignal\b", r"\bkarat\b", r"\bhirevue\b",
    r"\binterview\b", r"\bschedule a (call|time|chat)\b",
    r"\bnext steps?\b", r"\bdeadline\b",
    r"\baction required\b", r"\bplease (complete|confirm|respond|reply)\b",
    r"\bverification code\b", r"\bone[- ]time (code|password)\b",
    # Broader than the triage keyword list on purpose: the prefilter decides
    # without ever reading the body, so it leans harder on the subject.
    r"\bassessment\b", r"\btake[- ]home\b", r"\bavailability\b",
    r"\boffer letter\b", r"\boffer of (employment|admission|internship)\b",
    r"\b(extend|extending|extended) (you )?an offer\b", r"\byour offer\b",
]

HARD_KEEP_WEAK = [r"\boffer\b", r"\bexpires?\b", r"\blast chance\b"]

MARKETING_VETO = [
    r"limited[\s-]?time", r"offer ends", r"\d+%\s*off", r"\bsale\b",
    r"(exclusive|special|introductory)\s+offer", r"\bcoupon\b", r"\bdeal[s]?\b",
    r"\bsubscribe\b", r"free (trial|shipping)", r"act now", r"don'?t miss",
    r"black friday", r"cyber monday", r"flash sale", r"save big",
    r"\bpromo(tion)?\b", r"buy (one|now)", r"shop now", r"\bwebinar\b",
]

HARD_KEEP_PATTERNS = HARD_KEEP_STRONG + HARD_KEEP_WEAK

# Compiled once into single alternations. These run against every message on
# every pass, so k separate re.search calls is k passes over the same string
# for no reason.
_STRONG = re.compile("|".join(HARD_KEEP_STRONG), re.IGNORECASE)
_WEAK = re.compile("|".join(HARD_KEEP_WEAK), re.IGNORECASE)
_VETO = re.compile("|".join(MARKETING_VETO), re.IGNORECASE)


def subject_is_protected(subject):
    """True if this subject must stay in the inbox, whatever else decides."""
    subject = (subject or "").lower()
    if _STRONG.search(subject):
        return True
    if _WEAK.search(subject):
        # A weak term inside obviously promotional copy isn't protection,
        # it's marketing squatting on the word "offer".
        return not _VETO.search(subject)
    return False


def hard_keep(msg, never_filter=()):
    """Subject protection plus the sender allowlist.

    .get throughout: a backend that can't supply a field must not be able to
    crash triage, because a crash means nothing gets triaged at all.
    """
    sender = f"{msg.get('from', '')} {msg.get('reply_to', '')}".lower()
    if any(s in sender for s in never_filter):
        return True
    return subject_is_protected(msg.get("subject", ""))
