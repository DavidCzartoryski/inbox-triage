#!/usr/bin/env python3
"""
Remembered verdicts, so the same email shape isn't paid for twice.

Inboxes are repetitive. "12 new jobs for you" arrives weekly from the same
address with only the number changing, and classifying it every time is
spending money to reach a conclusion already reached. This is a memo table
keyed on (sender, subject template) — see prefilter.cache_key.

    {"<key>": {"category": "NOISE", "n": 7, "sender": "...",
               "template": "# new jobs for you", "last": "2026-09-16"}}

Deliberately not keyed on sender alone. no-reply@greenhouse.io sends both
"your application was received" and "complete your assessment"; a
sender-keyed cache would let the first teach the agent to file the second.

Bounded and self-pruning: least-used entries go first when it fills, so it
can't grow without limit on a machine nobody maintains.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

MAX_ENTRIES = 4000
# Verdicts this old are re-checked. A newsletter you used to ignore might be
# something you now read, and senders change what they send.
STALE_DAYS = 180


class VerdictCache:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        self.hits = 0
        self.misses = 0
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            if isinstance(raw, dict):
                self.data = raw.get("entries", raw)
        except (json.JSONDecodeError, OSError):
            self.data = {}   # a corrupt cache costs money, never correctness

    def save(self):
        if len(self.data) > MAX_ENTRIES:
            # Evict least-seen first; ties broken by oldest.
            ranked = sorted(self.data.items(),
                            key=lambda kv: (kv[1].get("n", 0),
                                            kv[1].get("last", "")))
            for key, _ in ranked[:len(self.data) - MAX_ENTRIES]:
                del self.data[key]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": self.data}, indent=1))
        tmp.replace(self.path)

    def get(self, key):
        entry = self.data.get(key)
        if not entry:
            self.misses += 1
            return None
        if self._stale(entry):
            self.misses += 1
            return None
        self.hits += 1
        return entry

    @staticmethod
    def _stale(entry):
        last = entry.get("last")
        if not last:
            return True
        try:
            when = datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return True
        return (datetime.now(timezone.utc) - when).days > STALE_DAYS

    def record(self, key, category, sender="", template=""):
        """Note a verdict the model actually produced."""
        entry = self.data.get(key)
        today = datetime.now(timezone.utc).date().isoformat()
        if entry and entry.get("category") == category:
            entry["n"] = entry.get("n", 1) + 1
            entry["last"] = today
        else:
            # A changed verdict resets the count: the old pattern no longer
            # holds, so it has to earn confidence again before it's reused
            # for filing.
            self.data[key] = {"category": category, "n": 1, "sender": sender,
                              "template": template, "last": today}

    def forget_sender(self, sender):
        """Drop every entry for a sender — used when you correct a mistake."""
        sender = sender.lower()
        gone = [k for k, v in self.data.items()
                if sender in (v.get("sender") or "").lower()]
        for k in gone:
            del self.data[k]
        return len(gone)

    def stats(self):
        looked = self.hits + self.misses
        return {
            "entries": len(self.data),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / looked, 3) if looked else 0.0,
        }
