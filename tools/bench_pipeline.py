#!/usr/bin/env python3
"""
Reproduce the latency numbers in the README.

    .venv/bin/python tools/bench_pipeline.py

Needs macOS with Mail.app configured. Read-only: it fetches ids, headers and
bodies, and never moves, flags or sends anything. No API key needed — the
Anthropic client is stubbed, and an idle run never calls it anyway.

Alternates the old and new idle paths so both see the same Mail.app mood, and
reports the median. Single samples here are close to meaningless: Mail.app
caches the id list but not message contents, so a warm new-path run is ~1.3s
while the first run after launch is ~9s.
"""

import contextlib
import io
import json
import os
import statistics
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ROUNDS = int(os.environ.get("BENCH_ROUNDS", "4"))


def _stub_anthropic():
    mod = types.ModuleType("anthropic")

    class Anthropic:
        def __init__(self, *a, **k):
            self.messages = None

    mod.Anthropic = Anthropic
    for name in ("AuthenticationError", "APIConnectionError"):
        setattr(mod, name, type(name, (Exception,), {}))
    sys.modules["anthropic"] = mod


def main():
    tmp = tempfile.mkdtemp()
    os.environ.update(
        MAIL_BACKEND="applescript",
        TRIAGE_STATE=f"{tmp}/state.json",
        TRIAGE_CONFIG=f"{tmp}/config.json",
        TRIAGE_CACHE=f"{tmp}/cache.json",
        DIGEST_TO="bench@example.invalid",
        ANTHROPIC_API_KEY="sk-ant-stub-not-used",
    )
    _stub_anthropic()

    import icloud_triage as t          # noqa: E402
    import mail_backends as mb         # noqa: E402
    import prefilter                   # noqa: E402
    import settings                    # noqa: E402

    backend = mb.AppleScriptBackend()
    print("warming Mail.app...")
    backend.fetch_recent_ids(count=5)

    total = len(backend.fetch_recent_ids(count=1)) and None
    ids = backend.fetch_recent_ids(count=t.APPLESCRIPT_ID_WINDOW)
    print(f"id window: {len(ids)} messages\n")

    # Everything in the window is already seen, so run_triage does the
    # cheapest thing it can: notice there's nothing new and stop.
    Path(f"{tmp}/state.json").write_text(json.dumps(
        {"last_uid": 0, "seen": ids, "queue": [], "last_digest": None}))

    new, old = [], []
    for i in range(ROUNDS):
        start = time.time()
        with contextlib.redirect_stdout(io.StringIO()):
            t.run_triage()
        new.append(time.time() - start)

        # v1 behaviour: 25 full bodies fetched on every run regardless.
        start = time.time()
        backend.fetch_recent(count=25)
        old.append(time.time() - start)
        print(f"  round {i + 1}: new {new[-1]:5.2f}s   v1 {old[-1]:5.2f}s")

    med_new, med_old = statistics.median(new), statistics.median(old)
    print(f"\nidle run, v1  : median {med_old:5.2f}s  "
          f"(min {min(old):.2f}, max {max(old):.2f})")
    print(f"idle run, now : median {med_new:5.2f}s  "
          f"(min {min(new):.2f}, max {max(new):.2f})")
    print(f"-> {med_old / med_new:.1f}x faster, "
          f"{med_old - med_new:.1f}s saved per idle run")
    print(f"-> at a 15-minute interval: "
          f"{(med_old - med_new) * 96 / 60:.0f} min/day of Apple Events avoided")

    # How much of a real batch the header layer can decide on its own.
    print("\nheader-only decisions on your newest 25:")
    cfg = settings.load_config(path=f"{tmp}/config.json")
    threshold = settings.prefilter_threshold(cfg)
    sample = backend.fetch_headers_by_ids(ids[:25], window=25)
    actions = {"file": 0, "keep": 0, "ask": 0}
    for msg in sample:
        if settings.matching_rules(msg, cfg):
            actions["file"] += 1
            continue
        actions[prefilter.decide(
            msg,
            protected=settings.allowlisted(msg, cfg),
            threshold=threshold,
        ).action] += 1

    n = len(sample) or 1
    asked = actions["ask"]
    print(f"  filed from headers : {actions['file']}/{n}")
    print(f"  kept from headers  : {actions['keep']}/{n}")
    print(f"  sent to the model  : {asked}/{n}  ({asked / n:.0%})")
    print(f"  bodies avoided     : {n - asked} "
          f"(~{(n - asked) * 0.44:.0f}s of reading)")
    print(f"  tokens avoided     : ~{(n - asked) * 600:,} of ~{n * 600:,}")


if __name__ == "__main__":
    main()
