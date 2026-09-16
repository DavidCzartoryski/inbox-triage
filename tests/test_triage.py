"""Offline tests. No network, no mail account, no API key required."""
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
os.environ.setdefault("MAIL_BACKEND", "applescript")

import mail_backends as mb  # noqa: E402
import icloud_triage as t  # noqa: E402
import inbox_cleanup as c  # noqa: E402

FS, RS = "\x1e", "\x1d"


def _record(*fields):
    return FS.join(fields) + RS


def test_applescript_parsing_roundtrip():
    raw = (_record("84321", "Dr. Patel <patel@univ.edu>", "", "Re: office hours",
                   "Tue Sep 15 2026", "false", "Hi,\n\nMove to 3pm?")
           + _record("84322", "CodeSignal <no-reply@codesignal.com>",
                     "recruiting@stripe.com", "Complete your assessment",
                     "Mon Sep 14 2026", "true", 'You have 5 days. "Start" now.'))
    b = mb.AppleScriptBackend()
    b._run = lambda script: raw
    msgs = b.fetch_recent()
    assert len(msgs) == 2
    assert msgs[0]["uid"] == "84321" and msgs[0]["read"] is False
    assert "Move to 3pm" in msgs[0]["body"]
    assert msgs[1]["read"] is True
    assert msgs[1]["reply_to"] == "recruiting@stripe.com"
    assert msgs[1]["subject"] == "Complete your assessment"


# Regression: every key the triage path reads must be supplied by every
# backend. This is checked against real parser output rather than hand-built
# dicts, because a hand-built fixture can hold a key the backend never sets.
def test_applescript_output_satisfies_triage_contract():
    raw = _record("1", "recruiter@corp.example", "", "Online assessment",
                  "Tue", "false", "Finish within 5 days")
    b = mb.AppleScriptBackend()
    b._run = lambda script: raw
    msg = b.fetch_recent()[0]
    rendered = t.render_for_model(msg)      # KeyError here = triage crashes
    assert "Online assessment" in rendered
    assert t.hard_keep(msg) is True


def test_imap_output_satisfies_triage_contract():
    raw = (b"From: Dr. Patel <patel@univ.edu>\r\n"
           b"Reply-To: patel-assistant@univ.edu\r\n"
           b"Subject: Re: your interview\r\n"
           b"Date: Tue, 15 Sep 2026 09:00:00 -0400\r\n"
           b"List-Unsubscribe: <https://x.example/u>\r\n"
           b"Content-Type: text/plain\r\n\r\nCan you meet Thursday?\r\n")

    class FakeIMAP:
        def uid(self, cmd, *args):
            if cmd == "SEARCH":
                return "OK", [b"101"]
            if cmd == "FETCH":
                return "OK", [(b"101 (BODY[] {%d}" % len(raw), raw)]
            return "OK", [b""]

    b = mb.IMAPBackend.__new__(mb.IMAPBackend)   # skip credential lookup
    b._connect = lambda: FakeIMAP()
    msg = b.fetch_since(100)[0]
    assert msg["reply_to"] == "patel-assistant@univ.edu"
    assert msg["has_unsubscribe"] is True
    rendered = t.render_for_model(msg)
    assert "Can you meet Thursday?" in rendered
    assert "patel-assistant@univ.edu" in rendered


def test_unclassified_mail_is_retried_next_run():
    # 3 and 4 failed to classify, so we must not advance past 3.
    assert t.next_last_uid(0, ["1", "2", "3", "4"], ["3", "4"]) == 2
    # Nothing failed: advance to the newest message.
    assert t.next_last_uid(0, ["1", "2", "3"], []) == 3
    # A wholly failed batch must not rewind state and re-triage the mailbox.
    assert t.next_last_uid(50, ["51", "52"], ["51", "52"]) == 50


def test_malformed_applescript_output_is_skipped():
    b = mb.AppleScriptBackend()
    truncated = _record("1", "a@b.example", "", "subj", "Tue", "false")  # no body
    for junk in ("", "garbage" + RS, FS.join(["1", "2"]) + RS, truncated):
        b._run = lambda script, j=junk: j
        assert b.fetch_recent() == []


def test_password_is_never_echoed():
    assert mb.mask("hunter2") == "****"
    assert mb.mask("") == "(unset)"


def test_hard_keep_overrides_classification():
    msg = {"from": "no-reply@ats.example", "reply_to": "",
           "subject": "Complete your online assessment"}
    assert t.hard_keep(msg) is True
    assert t.hard_keep({"from": "alerts@jobs.example", "reply_to": "",
                        "subject": "12 new jobs for you"}) is False


def test_digest_excludes_noise_and_surfaces_deadlines():
    queue = [
        {"uid": "1", "from": "CodeSignal", "subject": "Assessment for Stripe",
         "date": "", "category": "ACTION_REQUIRED", "importance": 98,
         "summary": "Timed", "deadline": "Sept 19"},
        {"uid": "2", "from": "Canvas", "subject": "Grade posted", "date": "",
         "category": "NOISE", "importance": 3, "summary": "", "deadline": None},
    ]
    subject, html = t.build_digest(queue)
    plain = t.build_plain_digest(queue)
    assert "1 needs action" in subject
    assert "Stripe" in html and "Grade posted" not in html
    assert "DEADLINE: Sept 19" in plain and "Grade posted" not in plain


def test_sender_grouping_is_case_insensitive():
    assert c.sender_key("Dr. Patel <Patel@Univ.EDU>") == "patel@univ.edu"


def test_recent_mail_is_protected_from_cleanup():
    assert c.msg_age_days("Mon, 14 Jan 2024 10:00:00 -0400") > 600
    assert c.msg_age_days("not a date") == 9999  # undateable is treated as old


def test_cleanup_report_lists_both_actions():
    plan = [
        {"sender": "alerts@jobs.example", "display": "Job Alerts", "count": 412,
         "action": "ARCHIVE", "category": "job board", "reason": "automated",
         "sample_subjects": [], "uids": ["1"]},
        {"sender": "patel@univ.edu", "display": "Dr. Patel", "count": 31,
         "action": "KEEP", "category": "person", "reason": "real person",
         "sample_subjects": [], "uids": ["2"]},
    ]
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp()            # don't litter the real /tmp or the repo
    os.chdir(tmp)
    try:
        c.write_report(plan, protected=147)
        report = (Path(tmp) / "cleanup_plan.md").read_text()
        assert "Job Alerts" in report and "Dr. Patel" in report
        assert "147" in report
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} passed")
