"""Offline tests. No network, no mail account, no API key required."""
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.modules.setdefault("anthropic", types.ModuleType("anthropic"))
os.environ.setdefault("MAIL_BACKEND", "applescript")

import mail_backends as mb  # noqa: E402
import icloud_triage as t  # noqa: E402
import inbox_cleanup as c  # noqa: E402
import keystore as ks  # noqa: E402
import prefilter as pf  # noqa: E402
import verdict_cache as vc  # noqa: E402
import settings as st  # noqa: E402
import subscriptions as sub  # noqa: E402

FS, RS = "\x1e", "\x1d"


def _today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date().isoformat()


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


def test_fetch_recent_ids_parses_and_skips_blanks():
    b = mb.AppleScriptBackend()
    b._run = lambda script: FS.join(["84321", "84322", "", "84323"]) + FS
    assert b.fetch_recent_ids() == ["84321", "84322", "84323"]
    b._run = lambda script: ""
    assert b.fetch_recent_ids() == []


def test_fetch_by_ids_is_free_when_nothing_is_new():
    """An idle run must not talk to Mail.app at all."""
    b = mb.AppleScriptBackend()

    def boom(script):
        raise AssertionError("should not have run any AppleScript")

    b._run = boom
    assert b.fetch_by_ids([]) == []


def test_fetch_by_ids_matches_whole_ids_only():
    """Id 123 must not be matched by the substring inside 1234."""
    b = mb.AppleScriptBackend()
    captured = {}

    def fake(script):
        captured["script"] = script
        return _record("123", "a@b.example", "", "subj", "Tue", "false", "body")

    b._run = fake
    msgs = b.fetch_by_ids(["123"])
    assert msgs[0]["uid"] == "123"
    # The id list is comma-delimited on both sides, and the comparison adds
    # commas around each candidate, so 1234 can't satisfy it.
    assert '","' not in captured["script"].split("set wanted to")[1][:12]
    assert ",123," in captured["script"]
    assert '("," & mid & ",")' in captured["script"]


def test_fetch_by_ids_rejects_non_numeric_ids():
    """Ids are interpolated into AppleScript, so they must be integers."""
    b = mb.AppleScriptBackend()
    b._run = lambda script: ""
    for bad in (["1; do shell script \"echo hi\""], ["abc"], ["1 or true"]):
        try:
            b.fetch_by_ids(bad)
            assert False, f"should have rejected {bad!r}"
        except (ValueError, TypeError):
            pass


# ---------------------------------------------------------------------------
# Filter rules
# ---------------------------------------------------------------------------

def _msg(frm, subject, unsub=False, reply_to=""):
    return {"uid": "1", "from": frm, "reply_to": reply_to, "subject": subject,
            "has_unsubscribe": unsub, "body": "", "date": ""}


def _cfg(**filters):
    cfg = st.load_config(path="/nonexistent-on-purpose")
    cfg["filters"].update(filters)
    return cfg


# The whole safety argument for shipping a no-reply@ toggle at all: it sits
# below the keyword protections, so it cannot file an assessment invite.
def test_no_reply_rule_cannot_file_an_assessment():
    cfg = _cfg(no_reply=True)
    for subject in ("Complete your online assessment",
                    "Interview scheduling for Stripe",
                    "Action required: verification code"):
        msg = _msg("no-reply@hackerrank.com", subject)
        assert t.hard_keep(msg) is True, subject
        # matching_rules would file it, which is exactly why the protection
        # has to be checked first in run_triage.
        assert "no_reply" in st.matching_rules(msg, cfg)

    # Mail the protections don't cover is still filed, so the rule does work.
    assert st.matching_rules(_msg("no-reply@app.example", "Weekly summary"),
                             cfg) == ["no_reply"]


def test_allowlist_beats_every_rule():
    # Career-services mass mail: carries an unsubscribe header and comes from
    # a no-reply address, so two enabled rules want to file it. The allowlist
    # is checked first in run_triage, which is what saves it.
    cfg = _cfg(bulk_mail=True, no_reply=True)
    cfg["allowlist"] = ["@northeastern.edu"]
    msg = _msg("no-reply@northeastern.edu", "Career fair Thursday", unsub=True)
    assert sorted(st.matching_rules(msg, cfg)) == ["bulk_mail", "no_reply"]
    assert st.allowlisted(msg, cfg) is True

    # Same mail, not allowlisted: the rules do file it.
    cfg["allowlist"] = []
    assert st.allowlisted(msg, cfg) is False


def test_job_board_rule_spares_real_recruiter_mail():
    cfg = _cfg(job_boards=True)
    digest = _msg("jobs-noreply@linkedin.com", "12 new jobs for you")
    human = _msg("recruiter@linkedin.com", "Interested in a role at Stripe?")
    assert "job_boards" in st.matching_rules(digest, cfg)
    assert st.matching_rules(human, cfg) == []   # match:"both" needs subject too


def test_disabled_rules_never_match():
    msg = _msg("notifications@instructure.com", "Grade posted")
    assert st.matching_rules(msg, _cfg(lms=True)) == ["lms"]
    assert st.matching_rules(msg, _cfg(lms=False)) == []


def test_bulk_rule_reads_the_unsubscribe_header():
    cfg = _cfg(bulk_mail=True)
    assert st.matching_rules(_msg("x@y.example", "Hi", unsub=True), cfg) == ["bulk_mail"]
    assert st.matching_rules(_msg("x@y.example", "Hi", unsub=False), cfg) == []


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_roundtrip_and_cadence_lookup():
    tmp = Path(tempfile.mkdtemp()) / "config.json"
    try:
        cfg = st.load_config(path=tmp)          # missing file -> defaults
        cfg["refresh"] = "8h"
        cfg["digests_per_day"] = "3"
        st.save_config(cfg, path=tmp)
        back = st.load_config(path=tmp)
        assert st.refresh_minutes(back) == 480
        assert st.digest_hours(back) == [8, 13, 18]
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_corrupt_config_falls_back_instead_of_stopping_triage():
    tmp = Path(tempfile.mkdtemp()) / "config.json"
    try:
        tmp.write_text("{not json at all")
        cfg = st.load_config(path=tmp)
        assert cfg["refresh"] == st.DEFAULT_CONFIG["refresh"]
        assert st.refresh_minutes(cfg) == 15
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_stale_config_cannot_enable_an_unknown_rule():
    tmp = Path(tempfile.mkdtemp()) / "config.json"
    try:
        tmp.write_text(json.dumps(
            {"filters": {"delete_everything": True, "lms": False}}))
        cfg = st.load_config(path=tmp)
        assert "delete_everything" not in cfg["filters"]
        assert cfg["filters"]["lms"] is False      # known keys still apply
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_unknown_cadence_ids_fall_back_to_safe_values():
    assert st.refresh_minutes({"refresh": "every fortnight"}) == 15
    assert st.digest_hours({"digests_per_day": "99"}) == [8, 18]


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def test_unsubscribe_target_prefers_mailto_over_tracked_url():
    header = "<https://track.example/u?id=9>, <mailto:leave@example.com>"
    assert sub.unsubscribe_target(header) == ("mailto", "leave@example.com")
    assert sub.unsubscribe_target("<https://track.example/u>") == (
        "url", "https://track.example/u")
    assert sub.unsubscribe_target(None) == (None, None)
    assert sub.unsubscribe_target("garbage") == (None, None)


def test_subscription_scoring_ranks_high_volume_never_opened_first():
    cfg = {"unsubscribe": {"min_messages": 5, "max_read_rate": 0.2}}
    messages = (
        # 30 from a sender you never open -> top candidate
        [{"from": "Alerts <a@jobs.example>", "subject": "jobs", "date": None,
          "seen": False, "unsub": "<mailto:leave@jobs.example>"}] * 30
        # 10 from a sender you never open -> also a candidate, ranked lower
        + [{"from": "Brew <b@news.example>", "subject": "news", "date": None,
            "seen": False, "unsub": "<https://news.example/u>"}] * 10
        # 20 you read most of -> not a candidate
        + [{"from": "Prof <p@univ.edu>", "subject": "class", "date": None,
            "seen": True, "unsub": "<mailto:x@univ.edu>"}] * 20
        # high volume but no unsubscribe header -> nothing to act on
        + [{"from": "ATS <ats@corp.example>", "subject": "app", "date": None,
            "seen": False, "unsub": None}] * 40
    )
    got = sub.analyze(messages, cfg)
    assert [c["sender"] for c in got] == ["a@jobs.example", "b@news.example"]
    assert got[0]["count"] == 30 and got[0]["read"] == 0


def test_a_sender_below_the_volume_floor_is_not_a_subscription():
    cfg = {"unsubscribe": {"min_messages": 5, "max_read_rate": 0.2}}
    messages = [{"from": "x@y.example", "subject": "s", "date": None,
                 "seen": False, "unsub": "<mailto:l@y.example>"}] * 4
    assert sub.analyze(messages, cfg) == []


# ---------------------------------------------------------------------------
# Keyword protection vs. marketing copy
# ---------------------------------------------------------------------------

# Regression: "Limited-time college offer ends soon" was held in the inbox
# because \boffer\b fired. This layer outranks every filter toggle, so a false
# positive here is the one kind the panel cannot fix.
def test_real_opportunities_are_always_protected():
    for subject in ("Offer letter from Stripe",
                    "We would like to extend an offer",
                    "Your offer from Google",
                    "Offer of employment - Datadog",
                    "Complete your online assessment",
                    "Interview scheduling",
                    "Your verification code is 123456",
                    "Action required: application deadline Friday",
                    "This assessment link expires in 48 hours"):
        assert t.hard_keep(_msg("x@y.example", subject)) is True, subject


def test_marketing_cannot_squat_on_protected_words():
    cfg = _cfg(marketing=True)
    for subject in ("⏰ Limited-time college offer ends soon.",
                    "Exclusive offer: 50% off textbooks",
                    "Your coupon expires tonight - shop now",
                    "Last chance! Flash sale ends at midnight",
                    "Special offer for students - act now"):
        msg = _msg("promo@shop.example", subject, unsub=True)
        assert t.hard_keep(msg) is False, subject
        assert "marketing" in st.matching_rules(msg, cfg), subject


# ---------------------------------------------------------------------------
# Placeholder guards
# ---------------------------------------------------------------------------

def test_placeholder_api_key_is_caught_before_any_request():
    saved_env = dict(os.environ)
    saved_get = ks.get_secret
    try:
        # Empty Keychain, so the result doesn't depend on the dev's machine.
        ks.get_secret = lambda name, service=ks.SERVICE: None

        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-..."
        problems = t.config_problems(require_digest_to=False)
        assert any("still the example value" in p for p in problems)

        os.environ.pop("ANTHROPIC_API_KEY", None)
        problems = t.config_problems(require_digest_to=False)
        assert any("No Anthropic API key found" in p for p in problems)
        # Both phrasings must point at how to fix it.
        assert any("keystore.py set" in p for p in problems)

        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-a-real-looking-key"
        assert t.config_problems(require_digest_to=False) == []

        # A key in the Keychain alone is enough — no .env required.
        os.environ.pop("ANTHROPIC_API_KEY", None)
        ks.get_secret = lambda name, service=ks.SERVICE: "sk-ant-stored"
        assert t.config_problems(require_digest_to=False) == []
    finally:
        ks.get_secret = saved_get
        os.environ.clear()
        os.environ.update(saved_env)


def test_digest_refuses_to_mail_the_example_address():
    """The digest carries real subject lines; a wrong recipient can't be undone."""
    tmp = Path(tempfile.mkdtemp())
    saved_path, saved_to = t.STATE_PATH, t.DIGEST_TO
    try:
        t.STATE_PATH = tmp / "state.json"
        t.STATE_PATH.write_text(json.dumps({
            "last_uid": 0, "seen": [], "last_digest": None,
            "queue": [{"uid": "1", "from": "recruiter@corp.example",
                       "subject": "Assessment for Stripe", "date": "",
                       "category": "ACTION_REQUIRED", "importance": 99,
                       "summary": "", "deadline": None}]}))
        for bad in ("you@icloud.com", ""):
            t.DIGEST_TO = bad
            try:
                t.run_digest()
                assert False, f"should have refused to send to {bad!r}"
            except SystemExit as exc:
                assert "Refusing to send" in str(exc)
        # The queue must survive the refusal, or the mail is lost silently.
        state = json.loads(t.STATE_PATH.read_text())
        assert len(state["queue"]) == 1
    finally:
        t.STATE_PATH, t.DIGEST_TO = saved_path, saved_to
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Header prefilter
# ---------------------------------------------------------------------------

BULK_HEADERS = ("List-Unsubscribe: <https://x.example/u>\r\n"
                "List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n"
                "Feedback-ID: 123:campaign:esp\r\n"
                "X-Campaign: fall-promo\r\n")


def _hdr(frm, subject, headers="", reply_to=""):
    return {"uid": "1", "from": frm, "reply_to": reply_to, "subject": subject,
            "date": "Tue", "raw_headers": headers, "body": "",
            "has_unsubscribe": "List-Unsubscribe:" in headers}


# The whole safety case for a layer that files mail it has never read.
def test_prefilter_never_files_protected_mail_even_when_it_looks_bulk():
    for subject in ("Complete your online assessment",
                    "Your HackerRank assessment expires in 48 hours",
                    "Interview availability?",
                    "Action required: verification code",
                    "Offer letter from Stripe"):
        # Maximum bulk evidence: robot sender, every campaign header.
        msg = _hdr("no-reply@notifications.example", subject, BULK_HEADERS)
        d = pf.decide(msg)
        assert d.action == "keep", f"{subject} -> {d.action}"
        assert d.confidence == 1.0


def test_prefilter_does_not_trust_the_caller_to_pass_protected():
    """decide() re-checks protections itself, so a forgetful caller is safe."""
    msg = _hdr("no-reply@ats.example", "Complete your assessment", BULK_HEADERS)
    assert pf.decide(msg, protected=False).action == "keep"


def test_bulk_infrastructure_headers_are_enough_to_file():
    msg = _hdr("newsdigest@insideapple.apple.com",
               "A possible early sign of dementia, and other stories",
               BULK_HEADERS)
    d = pf.decide(msg)
    assert d.action == "file"
    assert d.confidence >= 0.75
    assert "list_unsubscribe" in d.reasons and "feedback_id" in d.reasons


def test_local_part_tokens_match_mid_word():
    """'newsdigest@' must trip robot_local; \\bdigest@ never would."""
    noise, _, reasons = pf.score_headers(
        _hdr("newsdigest@insideapple.apple.com", "anything"))
    assert "robot_local" in reasons


def test_human_evidence_sends_it_to_the_model_instead_of_filing():
    # Same bulk headers, but a person appears to have written it.
    msg = _hdr("Alice Patel <alice.patel@northeastern.edu>",
               "Re: your question about the project", BULK_HEADERS)
    assert pf.decide(msg).action == "ask"


def test_plain_mail_with_no_signals_goes_to_the_model():
    assert pf.decide(_hdr("someone@startup.io", "quick note")).action == "ask"


def test_bulk_infra_score_is_capped():
    many = BULK_HEADERS + ("Precedence: bulk\r\nAuto-Submitted: auto-generated\r\n"
                           "List-Id: <x.example>\r\n")
    noise, _, _ = pf.score_headers(_hdr("x@y.example", "hello", many))
    # 3+2+2+2+2+2+1 = 14 raw, capped at BULK_INFRA_CAP
    assert noise <= pf.BULK_INFRA_CAP


def test_subject_template_collapses_variants():
    assert (pf.subject_template("12 new jobs for you")
            == pf.subject_template("5 new jobs for you")
            == "# new jobs for you")
    # Different shapes must not collide.
    assert pf.subject_template("Grade posted") != pf.subject_template("12 new jobs")


def test_cache_key_separates_subjects_from_the_same_sender():
    """greenhouse sends both 'application received' and 'your assessment'."""
    sender = "no-reply@greenhouse.io"
    a = pf.cache_key({"from": sender, "subject": "We received your application"})
    b = pf.cache_key({"from": sender, "subject": "Complete your assessment"})
    assert a != b
    # But the same shape with a different number is one entry.
    c = pf.cache_key({"from": sender, "subject": "12 new jobs for you"})
    d = pf.cache_key({"from": sender, "subject": "48 new jobs for you"})
    assert c == d


# ---------------------------------------------------------------------------
# Verdict cache
# ---------------------------------------------------------------------------

def test_cache_needs_two_observations_before_it_files():
    """One sighting is not a pattern."""
    msg = _hdr("mystery@unknown.example", "Some subject nobody can classify")
    key = pf.cache_key(msg)

    once = {"category": "NOISE", "n": 1, "last": _today()}
    assert pf.decide(msg, cached=once).action == "ask"

    twice = {"category": "NOISE", "n": 2, "last": _today()}
    assert pf.decide(msg, cached=twice).action == "file"


def test_cached_important_is_reused_immediately():
    """Being wrong toward the inbox is cheap, so one sighting is enough."""
    msg = _hdr("recruiter@corp.example", "Some subject")
    cached = {"category": "PERSONAL", "n": 1, "last": _today()}
    d = pf.decide(msg, cached=cached)
    assert d.action == "keep" and "cached_important" in d.reasons


def test_cache_roundtrip_and_verdict_change_resets_confidence():
    tmp = Path(tempfile.mkdtemp())
    try:
        c = vc.VerdictCache(tmp / "cache.json")
        c.record("k1", "NOISE", sender="a@b.example", template="# new jobs")
        c.record("k1", "NOISE", sender="a@b.example", template="# new jobs")
        assert c.data["k1"]["n"] == 2
        # A changed verdict must earn trust again from scratch.
        c.record("k1", "ACTION_REQUIRED", sender="a@b.example")
        assert c.data["k1"]["n"] == 1
        c.save()
        assert vc.VerdictCache(tmp / "cache.json").data["k1"]["n"] == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stale_cache_entries_are_ignored():
    tmp = Path(tempfile.mkdtemp())
    try:
        c = vc.VerdictCache(tmp / "cache.json")
        c.data["old"] = {"category": "NOISE", "n": 9, "last": "2020-01-01"}
        assert c.get("old") is None          # too old to trust
        c.data["undated"] = {"category": "NOISE", "n": 9}
        assert c.get("undated") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrupt_cache_costs_money_not_correctness():
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "cache.json").write_text("{not json")
        c = vc.VerdictCache(tmp / "cache.json")
        assert c.data == {}                  # starts over, doesn't crash
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cache_is_bounded_and_evicts_least_seen():
    tmp = Path(tempfile.mkdtemp())
    try:
        c = vc.VerdictCache(tmp / "cache.json")
        for i in range(vc.MAX_ENTRIES + 50):
            c.data[f"k{i}"] = {"category": "NOISE", "n": 1 if i < 50 else 5,
                               "last": _today()}
        c.save()
        assert len(c.data) == vc.MAX_ENTRIES
        assert "k0" not in c.data            # n=1 evicted first
        assert "k4000" in c.data
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_forget_sender_drops_its_entries():
    tmp = Path(tempfile.mkdtemp())
    try:
        c = vc.VerdictCache(tmp / "cache.json")
        c.record("a", "NOISE", sender="alerts@jobs.example", template="x")
        c.record("b", "NOISE", sender="alerts@jobs.example", template="y")
        c.record("c", "NOISE", sender="prof@univ.edu", template="z")
        assert c.forget_sender("alerts@jobs.example") == 2
        assert set(c.data) == {"c"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Secret storage
# ---------------------------------------------------------------------------

def test_environment_beats_keychain_but_placeholder_does_not():
    saved_env = dict(os.environ)
    saved_get = ks.get_secret
    try:
        ks.get_secret = lambda name, service=ks.SERVICE: "from-keychain"

        os.environ["ANTHROPIC_API_KEY"] = "from-env"
        assert ks.api_key() == "from-env"

        # The shipped placeholder must not shadow a real stored key, or a
        # half-edited .env silently breaks a working setup.
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-..."
        assert ks.api_key() == "from-keychain"

        os.environ.pop("ANTHROPIC_API_KEY", None)
        assert ks.api_key() == "from-keychain"

        ks.get_secret = lambda name, service=ks.SERVICE: None
        assert ks.api_key() is None
    finally:
        ks.get_secret = saved_get
        os.environ.clear()
        os.environ.update(saved_env)


def test_keychain_roundtrip():
    """Only meaningful on macOS; CI on Linux has no `security` binary."""
    if not ks.available():
        return
    name = "anthropic-api-key"
    had = ks.get_secret(name)          # don't clobber a real key
    try:
        ok, _ = ks.set_secret(name, "test-value-please-ignore")
        assert ok
        assert ks.get_secret(name) == "test-value-please-ignore"
        assert ks.status()[name]["source"] == "keychain"
        assert ks.delete_secret(name) is True
        assert ks.get_secret(name) is None
    finally:
        if had:
            ks.set_secret(name, had)
        else:
            ks.delete_secret(name)


def test_set_secret_rejects_empty_values():
    ok, msg = ks.set_secret("anthropic-api-key", "   ")
    assert ok is False and "empty" in msg


def test_status_never_exposes_a_value():
    saved = ks.get_secret
    try:
        ks.get_secret = lambda name, service=ks.SERVICE: "super-secret-value"
        blob = json.dumps(ks.status())
        assert "super-secret-value" not in blob
        assert ks.status()["anthropic-api-key"]["set"] is True
    finally:
        ks.get_secret = saved


# ---------------------------------------------------------------------------
# Settings panel
# ---------------------------------------------------------------------------

def test_panel_rejects_requests_without_the_token():
    """The panel writes config, so an untokened request must not reach it."""
    import ui_server
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def status(path, token=None, host=None):
        url = f"{base}{path}" + (f"?token={token}" if token else "")
        req = urllib.request.Request(url)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert status("/api/config") == 403                      # no token
        assert status("/api/config", token="wrong") == 403        # bad token
        assert status("/api/config", token=ui_server.TOKEN) == 200
        # DNS rebinding: right token, but the browser thinks it's elsewhere.
        assert status("/api/config", token=ui_server.TOKEN,
                      host="evil.example") == 403
        # The secrets endpoint is the one most worth protecting.
        assert status("/api/secrets") == 403
        assert status("/api/secrets", token="wrong") == 403
        assert status("/api/secrets", token=ui_server.TOKEN) == 200

        # And even authorized, it reports only whether a key is set.
        saved = ks.get_secret
        try:
            ks.get_secret = lambda name, service=ks.SERVICE: "leaked-key-value"
            url = f"{base}/api/secrets?token={ui_server.TOKEN}"
            with urllib.request.urlopen(url, timeout=5) as r:
                body = r.read().decode()
            assert "leaked-key-value" not in body
            assert '"set": true' in body
        finally:
            ks.get_secret = saved
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} passed")
