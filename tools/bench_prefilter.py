#!/usr/bin/env python3
"""Is the combined-regex SignalSet actually faster than k separate searches?

Run:  .venv/bin/python tools/bench_prefilter.py
"""
import re
import sys
import timeit
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import prefilter  # noqa: E402

SUBJECTS = [
    "12 new jobs for you", "Re: office hours tomorrow",
    "A possible early sign of dementia, California's license-plate rule",
    "Complete your online assessment", "Your order has shipped",
    "New reply to your discussion post", "Limited-time offer ends soon",
    "Interested in a backend role at Stripe?",
] * 8   # 64 subjects, roughly two batches' worth

SETS = [prefilter.SUBJECT_NOISE, prefilter.HUMAN_SIGNALS]
# The obvious implementation: each member pattern compiled on its own.
SEPARATE = [[(w, re.compile(p, s.flags)) for (_, w, p) in s.members]
            for s in SETS]
TEMPLATES = [prefilter.subject_template(s) for s in SUBJECTS]


def combined():
    for t in TEMPLATES:
        for sset in SETS:
            sset.score(t)


def separate():
    for t in TEMPLATES:
        for pats in SEPARATE:
            score = 0
            for weight, rx in pats:
                if rx.search(t):
                    score += weight


if __name__ == "__main__":
    n = 300
    combined(); separate()          # warm caches
    c = timeit.timeit(combined, number=n)
    s = timeit.timeit(separate, number=n)
    scans = len(TEMPLATES) * n
    patterns = sum(len(p) for p in SEPARATE)
    print(f"{scans:,} subject scans against {patterns} patterns")
    print(f"  combined SignalSet : {c:.3f}s  ({c/scans*1e6:6.2f} us/subject)")
    print(f"  k separate regexes : {s:.3f}s  ({s/scans*1e6:6.2f} us/subject)")
    winner = "combined" if c < s else "separate"
    print(f"  -> {winner} wins by {max(s/c, c/s):.2f}x")
    per_run = abs(s - c) / n / len(TEMPLATES) * 25 * 1000
    print(f"\nOver a 25-message run that is {per_run:.3f} ms of difference.")
    print("For scale, one AppleScript header fetch for 25 messages is "
          "~5,900 ms, so this is not where the time goes.")
