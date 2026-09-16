#!/usr/bin/env python3
"""
The non-AI layer. Decides from headers alone, and says how sure it is.

Most mail is obvious from the envelope. A message from
notifications@instructure.com with a List-Unsubscribe header and the subject
"New reply to your discussion post" does not need a language model to
classify, and paying for one is waste. So every message is scored from its
headers first, and the model is only consulted when the score is ambiguous.

Three outcomes:

    KEEP    protected or clearly personal — stays in the inbox, no API call
    FILE    confidently bulk — filed, no API call, no body ever read
    ASK     genuinely uncertain — fetch the body and ask the model

Why headers are enough so often: on a real 4,046-message mailbox, 24 of the
newest 25 messages carried a List-Unsubscribe header. Bulk mail announces
itself.

Two deliberate asymmetries:

  - FILE needs a high bar, because filing an assessment is the one failure
    that matters. KEEP is cheap to be wrong about — a kept newsletter is
    mildly annoying.
  - Anything the keyword protections catch skips this layer entirely. The
    prefilter can never overrule them.
"""

import hashlib
import re
from email.utils import parseaddr

import protections

# ---------------------------------------------------------------------------
# Signal sets
# ---------------------------------------------------------------------------


class SignalSet:
    """Named patterns compiled into one regex that reports which ones fired.

    This is for explainability, not speed. Every decision has to be able to
    say why — the digest prints the reasons, and a rule you can't inspect is a
    rule you can't fix — so the scan returns signal *names*, not a boolean.

    It is not an optimization, and the docstring used to claim otherwise.
    Measured (tools/bench_prefilter.py): the combined pattern is ~1.19x
    SLOWER than k separate re.search calls, because finditer builds a match
    object for every hit while separate searches short-circuit on the first.
    The difference is 0.025 ms per 25-message run, against ~5,900 ms for the
    header fetch those messages needed — so it is the wrong thing to optimize
    in either direction. Clarity wins on merit.
    """

    def __init__(self, named_patterns, flags=re.IGNORECASE):
        self.members = list(named_patterns)   # kept for tests and benchmarks
        self.flags = flags
        self.weights = {name: weight for name, weight, _ in named_patterns}
        # Flags go here, not inline as (?im) inside a member pattern: Python
        # rejects a global flag that isn't at the start of the whole
        # expression, which combining patterns guarantees.
        self.pattern = re.compile(
            "|".join(f"(?P<{name}>{pat})" for name, _, pat in named_patterns),
            flags)

    def fired(self, text):
        """Set of signal names present in `text`. One pass."""
        if not text:
            return set()
        out = set()
        for match in self.pattern.finditer(text):
            name = match.lastgroup
            if name:
                out.add(name)
        return out

    def score(self, text):
        hits = self.fired(text)
        return sum(self.weights[h] for h in hits), hits


# Local-part evidence, matched against the part before the @ only. Not
# anchored to the @ itself: real senders look like "newsdigest@..." and
# "jobs-noreply@...", where the giveaway token sits mid-word with no word
# boundary around it.
LOCAL_NOISE = SignalSet([
    ("no_reply_local", 3, r"(no[-_.]?reply|donotreply|noreply|"
                          r"do[-_.]?not[-_.]?reply)"),
    ("robot_local", 2, r"(notification|alert|mailer|automated|digest|"
                       r"newsletter|marketing|campaign|bounce|"
                       r"news|promo|offers)"),
])

# Headers that only a bulk-sending platform adds. These turned out to be the
# strongest available signal: on a real inbox, 24 of 25 messages carried
# List-Unsubscribe, 24 carried Feedback-ID, and 24 carried
# List-Unsubscribe-Post (RFC 8058 one-click), which transactional mail such as
# an assessment invite essentially never sets.
BULK_INFRA = SignalSet([
    ("list_unsubscribe", 3, r"^list-unsubscribe\s*:"),
    ("one_click_unsub", 2, r"^list-unsubscribe-post\s*:"),
    ("feedback_id", 2, r"^feedback-id\s*:"),
    ("campaign_infra", 2, r"^x-(campaign|rpcampaign|broadcast-id|"
                          r"emailtype-id|marketing|mailer-id|esp|"
                          r"mailchimp|sg-eid)"),
    ("precedence_bulk", 2, r"^precedence\s*:\s*(bulk|list|junk)"),
    ("auto_submitted", 2, r"^auto-submitted\s*:\s*auto"),
    ("list_id", 1, r"^list-id\s*:"),
], flags=re.IGNORECASE | re.MULTILINE)

# Bulk infrastructure alone shouldn't be able to outvote everything else, so
# its contribution is capped. Seven campaign headers is not seven times the
# evidence of one.
BULK_INFRA_CAP = 6

# Sender-side evidence of bulk mail, matched against the whole sender string.
SENDER_NOISE = SignalSet([
    ("bulk_domain", 3, r"@[^\s>]*(linkedin|indeed|glassdoor|ziprecruiter|"
                       r"handshake|joinhandshake|monster|dice|wellfound|"
                       r"instructure|canvas|blackboard|gradescope|piazza|"
                       r"turnitin|chegg|coursehero|morningbrew|substack|"
                       r"mailchimp|sendgrid|constantcontact|hubspot|"
                       r"salesforce|marketo|klaviyo|braze)"),
    ("social_domain", 2, r"@[^\s>]*(facebookmail|instagram|twitter|tiktok|"
                         r"reddit|discord|snapchat|pinterest|quora|medium)"),
])

# Subject-side evidence of bulk mail. Written against templates, so digits are
# normalised away before matching (see subject_template).
SUBJECT_NOISE = SignalSet([
    ("job_digest", 3, r"(# new jobs?|new jobs? for you|jobs? you may|"
                      r"recommended for you|# (jobs?|opportunities))"),
    ("lms_notice", 3, r"(grade (posted|is available)|new reply to your|"
                      r"discussion (post|reply)|assignment (submitted|graded)|"
                      r"course announcement|new announcement|quiz (graded|available))"),
    ("app_received", 3, r"(we (have )?received your application|"
                        r"application (was |has been )?(received|submitted)|"
                        r"thank you for (your interest|applying))"),
    ("promo", 3, r"(#% off|sale ends|limited[\s-]?time|flash sale|"
                 r"exclusive offer|offer ends|shop now|act now|save big|"
                 r"black friday|cyber monday|don t miss|free trial)"),
    ("newsletter", 2, r"(newsletter|this week in|weekly (digest|roundup|recap)|"
                      r"daily (brief|digest)|morning brief|your \w+ digest)"),
    ("social_notice", 2, r"(liked your|commented on|started following|"
                         r"new connection|invitation to connect|"
                         r"you have # new|mentioned you)"),
    ("receipt", 2, r"(your (order|receipt|invoice)|order confirmation|"
                   r"has shipped|out for delivery|payment (received|confirmation))"),
])

# Evidence a human wrote this specifically to you.
HUMAN_SIGNALS = SignalSet([
    ("thread_reply", 3, r"^\s*(re|fwd|fw)\s*:"),
    ("direct_question", 2, r"(are you (free|available)|can you|could you|"
                           r"let me know|your thoughts|quick question|"
                           r"following up|checking in|wanted to reach)"),
])

EDU_DOMAIN = re.compile(r"@[^\s>]*\.edu\b", re.IGNORECASE)
PERSON_LOCAL = re.compile(r"^[a-z]+[._][a-z]+(\d{0,3})?$", re.IGNORECASE)

# A FILE decision needs this much net noise evidence. Tuned deliberately high:
# the cost of filing an assessment is not symmetric with keeping a newsletter.
FILE_THRESHOLD = 5
# Any human evidence at all sends it to the model instead of being filed.
HUMAN_VETO = 1


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def sender_key(from_header):
    """Bare lowercase address, for grouping and cache keys."""
    return (parseaddr(from_header or "")[1] or (from_header or "")).strip().lower()


def display_name(from_header):
    return (parseaddr(from_header or "")[0] or "").strip()


def subject_template(subject):
    """Collapse a subject to its template, so variants share a cache entry.

    "12 new jobs for you" and "5 new jobs for you" are the same email as far
    as triage is concerned, and should not each cost an API call. Digits
    become #, URLs and punctuation are dropped.
    """
    s = (subject or "").lower()
    s = re.sub(r"https?://\S+", " url ", s)
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"[^a-z#\s:]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:100]


def cache_key(msg):
    """Stable key for (sender, subject template).

    Keyed on both, never the sender alone: no-reply@greenhouse.io sends both
    "application received" and "complete your assessment". Caching by sender
    would let the first teach the agent to file the second.
    """
    digest = hashlib.sha1(
        f"{sender_key(msg.get('from'))}|{subject_template(msg.get('subject'))}"
        .encode("utf-8", "replace")).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class Decision:
    __slots__ = ("action", "confidence", "reasons", "noise", "human")

    def __init__(self, action, confidence, reasons, noise=0, human=0):
        self.action = action            # "keep" | "file" | "ask"
        self.confidence = confidence    # 0.0 - 1.0
        self.reasons = reasons          # list of short strings
        self.noise = noise
        self.human = human

    def __repr__(self):
        return (f"<{self.action} conf={self.confidence:.2f} "
                f"noise={self.noise} human={self.human} {','.join(self.reasons)}>")


def score_headers(msg):
    """Header-only evidence. Returns (noise_score, human_score, reasons)."""
    sender = f"{msg.get('from', '')} {msg.get('reply_to', '')}"
    template = subject_template(msg.get("subject"))
    reasons = []

    noise, hits = SENDER_NOISE.score(sender)
    reasons += sorted(hits)

    local = sender_key(msg.get("from")).split("@")[0]
    local_noise, local_hits = LOCAL_NOISE.score(local)
    noise += local_noise
    reasons += sorted(local_hits)

    subj_noise, subj_hits = SUBJECT_NOISE.score(template)
    noise += subj_noise
    reasons += sorted(subj_hits)

    # The best signal available, and free once headers are in hand.
    raw_headers = msg.get("raw_headers") or ""
    infra, infra_hits = BULK_INFRA.score(raw_headers)
    if not infra and msg.get("has_unsubscribe"):
        # IMAP path may hand us the flag without the raw block.
        infra, infra_hits = 3, {"list_unsubscribe"}
    noise += min(infra, BULK_INFRA_CAP)
    reasons += sorted(infra_hits)

    human, human_hits = HUMAN_SIGNALS.score(template)
    reasons += sorted(human_hits)

    if PERSON_LOCAL.match(local):
        human += 2
        reasons.append("personal_localpart")
    name = display_name(msg.get("from"))
    if name and len(name.split()) == 2 and not any(c.isdigit() for c in name):
        human += 1
        reasons.append("human_display_name")
    if EDU_DOMAIN.search(sender):
        human += 2
        reasons.append("edu_domain")

    return noise, human, reasons


def decide(msg, protected=False, cached=None):
    """What to do with this message, from headers alone.

    `protected` is the caller's allowlist/keyword verdict. It short-circuits
    everything: this layer is not allowed to overrule the protections.
    `cached` is a prior verdict for the same (sender, subject template).
    """
    # Checked here, not just by the caller. This layer files mail without ever
    # reading a body, so it has to own the guarantee itself rather than trust
    # whoever called it to have checked first.
    if protected or protections.subject_is_protected(msg.get("subject", "")):
        return Decision("keep", 1.0, ["protected"])

    noise, human, reasons = score_headers(msg)

    # A remembered verdict for this exact sender+template. Only NOISE is
    # reused for filing, and only once it's been seen more than once — a
    # single observation is not a pattern. KEEP verdicts are reused freely,
    # since being wrong that way just leaves mail in the inbox.
    if cached:
        if cached.get("category") in ("ACTION_REQUIRED", "PERSONAL"):
            return Decision("keep", 0.9, ["cached_important"] + reasons,
                            noise, human)
        if (cached.get("category") == "NOISE" and cached.get("n", 0) >= 2
                and human < HUMAN_VETO):
            return Decision("file", 0.95, ["cached_noise"] + reasons,
                            noise, human)

    if human >= HUMAN_VETO and noise < FILE_THRESHOLD + 3:
        # Looks like a person wrote it. Cheap to be wrong, so ask.
        return Decision("ask", 0.4, reasons, noise, human)

    if noise >= FILE_THRESHOLD and human < HUMAN_VETO:
        # Confidence grows with evidence but never reaches 1.0 without the
        # model; 0.75-0.95 keeps the reporting honest.
        confidence = min(0.95, 0.75 + 0.04 * (noise - FILE_THRESHOLD))
        return Decision("file", confidence, reasons, noise, human)

    return Decision("ask", 0.5 if noise or human else 0.3, reasons, noise, human)


if __name__ == "__main__":
    samples = [
        {"from": "LinkedIn <jobs-noreply@linkedin.com>", "reply_to": "",
         "subject": "12 new jobs for you", "has_unsubscribe": True},
        {"from": "Dr. Alice Patel <patel@northeastern.edu>", "reply_to": "",
         "subject": "Re: office hours tomorrow", "has_unsubscribe": False},
        {"from": "CodeSignal <no-reply@codesignal.com>", "reply_to": "",
         "subject": "Complete your assessment", "has_unsubscribe": True},
        {"from": "Some Recruiter <r@startup.io>", "reply_to": "",
         "subject": "Interested in a backend role?", "has_unsubscribe": False},
    ]
    for s in samples:
        print(f"{s['subject'][:38]:40} {decide(s)}")
