#!/usr/bin/env python3
"""
Job scout: new postings from the speedyapply 2027 lists, ranked against your
resume, each waiting on a yes or a no from you. Yes starts a tailored resume.

    python src/jobs.py scan              # fetch, filter, rank, store
    python src/jobs.py scan --dry-run    # same, but print only
    python src/jobs.py list              # recommendations waiting on you
    python src/jobs.py yes <id> [<id>]   # tailor a resume for these
    python src/jobs.py no <id> [<id>]    # pass on these
    python src/jobs.py status            # tailoring progress, finished PDFs

Ids can be shortened to any unique prefix, like git hashes.

Cheapest test first, as in triage. Postings you've already seen, postings
older than `max_age_days`, and titles matching an exclude pattern (PhD, senior,
clearance-only...) are dropped for free. Only what's left is sent to the model,
as one line per posting, and scored against your resume and profile.md.

What the model sees about you: resume.tex from your resume repo with the
heading cut off, so no phone number or email address, plus profile.md if
you've written one. Nothing from your mailbox.
"""

import argparse
import contextlib
import fcntl
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm  # noqa: E402
from settings import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = Path(os.environ.get("JOBS_STATE", ROOT / "jobs_state.json"))
PROFILE_PATH = Path(os.environ.get("JOBS_PROFILE", ROOT / "profile.md"))
RESUME_DIR = Path(os.environ.get("RESUME_DIR", Path.home() / "resume")).expanduser()
MODEL = os.environ.get("JOBS_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("JOBS_EFFORT", "low")

RAW = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/"
SOURCES = {
    "intern_usa": {"label": "USA internships", "kind": "intern",
                   "url": RAW + "README.md"},
    "new_grad_usa": {"label": "USA new grad", "kind": "new_grad",
                     "url": RAW + "NEW_GRAD_USA.md"},
    "intern_intl": {"label": "International internships", "kind": "intern",
                    "url": RAW + "INTERN_INTL.md"},
    "new_grad_intl": {"label": "International new grad", "kind": "new_grad",
                      "url": RAW + "NEW_GRAD_INTL.md"},
}
KIND_LABEL = {"intern": "internship", "new_grad": "new grad"}

RANK_BATCH = 60
RANK_WORKERS = 3
SEEN_DAYS = 120          # postings leave the list long before this
EXPIRE_DAYS = 30         # an unanswered recommendation goes stale

# ---------------------------------------------------------------------------
# Parsing the lists
# ---------------------------------------------------------------------------

TABLE_RE = re.compile(
    r"<!--\s*TABLE_(?:([A-Z]+)_)?START\s*-->(.*?)<!--\s*TABLE_(?:[A-Z]+_)?END\s*-->",
    re.S)
SECTIONS = {"FAANG": "FAANG+", "QUANT": "Quant", None: "Other"}


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _text(cell):
    cell = re.sub(r"<[^>]+>", "", cell or "")
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)   # markdown links
    return html.unescape(cell.replace("**", "")).strip()


def _href(cell):
    m = (re.search(r'href="(https?://[^"]+)"', cell or "")
         or re.search(r"\]\((https?://[^)\s]+)\)", cell or ""))
    return html.unescape(m.group(1)) if m else None


def job_id(url):
    url = url.strip().rstrip("/")
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def parse_listing(markdown, source_id, kind):
    """Every posting in one speedyapply file, across its FAANG+, Quant and
    Other tables. Rows that don't match their header are skipped, never
    guessed at."""
    jobs, last_company = [], ""
    for m in TABLE_RE.finditer(markdown):
        section = SECTIONS.get(m.group(1), (m.group(1) or "Other").title())
        header = None
        for line in m.group(2).splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = _cells(line)
            if header is None:
                header = [c.lower() for c in cells]
                continue
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if len(cells) != len(header):
                continue
            row = dict(zip(header, cells))
            url = _href(row.get("posting", ""))
            if not url:
                continue
            company = _text(row.get("company", ""))
            if company in ("", "↳"):
                company = last_company
            last_company = company
            age = re.match(r"(\d+)\s*d", _text(row.get("age", "")))
            jobs.append({
                "id": job_id(url),
                "company": company,
                "company_url": _href(row.get("company", "")),
                "title": _text(row.get("position", "")),
                "location": _text(row.get("location", "")),
                "salary": _text(row.get("salary", "")) or None,
                "url": url,
                "age_days": int(age.group(1)) if age else None,
                "section": section,
                "kind": kind,
                "source": source_id,
            })
    return jobs


def fetch_listing(source_id, timeout=30):
    src = SOURCES[source_id]
    req = urllib.request.Request(src["url"],
                                 headers={"User-Agent": "inbox-triage-jobs/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read(5_000_000).decode("utf-8", "replace")
    return parse_listing(text, source_id, src["kind"])


# ---------------------------------------------------------------------------
# Settings and the free filters
# ---------------------------------------------------------------------------

DEFAULT_EXCLUDE = [
    r"\bph\.?\s?d\b",
    r"\bhigh school\b",
    r"\b(senior|sr\.?|staff|principal|lead|manager|director)\b",
    # Roles that need an active clearance. Remove this line if you hold one.
    r"\b(ts/sci|top secret|polygraph|poly|clearance|ctj)\b",
]


def job_settings(cfg=None):
    cfg = cfg or load_config()
    j = cfg.get("jobs") or {}
    sources = [s for s in j.get("sources", ["intern_usa", "new_grad_usa"])
               if s in SOURCES]
    return {
        "enabled": bool(j.get("enabled", True)),
        "sources": sources or ["intern_usa", "new_grad_usa"],
        "max_age_days": int(j.get("max_age_days", 14)),
        "min_score": int(j.get("min_score", 70)),
        "digest_max": int(j.get("digest_max", 10)),
        "exclude": list(j.get("exclude", DEFAULT_EXCLUDE)),
        "locations": [s for s in j.get("locations", []) if str(s).strip()],
        "scan_hours": list(j.get("scan_hours", [7, 12])),
    }


def _same_role(job):
    return (re.sub(r"\W+", " ", job["company"].lower()).strip(),
            re.sub(r"\W+", " ", job["title"].lower()).strip())


def prefilter(jobs, settings, seen):
    """Drop, for free, what the model never needs to see.

    Returns (keep, dropped_counts, copies). One role posted once per city
    is kept once: `copies` maps each extra posting's id to the id kept, and
    the kept job lists the other locations.
    """
    excludes = [re.compile(p, re.I) for p in settings["exclude"]]
    locations = [s.lower() for s in settings["locations"]]
    keep, dropped, ids = [], {"seen": 0, "old": 0, "excluded": 0,
                              "location": 0, "duplicate": 0}, set()
    by_role, copies = {}, {}
    for job in jobs:
        primary = by_role.get(_same_role(job))
        if job["id"] in seen:
            dropped["seen"] += 1
        elif job["id"] in ids:
            dropped["duplicate"] += 1
        elif primary is not None:
            dropped["duplicate"] += 1
            copies[job["id"]] = primary["id"]
            if job["location"] and job["location"] != primary["location"]:
                primary.setdefault("other_locations", []).append(job["location"])
        elif (job["age_days"] is not None
              and job["age_days"] > settings["max_age_days"]):
            dropped["old"] += 1
        elif any(p.search(job["title"]) for p in excludes):
            dropped["excluded"] += 1
        elif locations and not any(l in job["location"].lower()
                                   for l in locations + ["remote"]):
            dropped["location"] += 1
        else:
            job = dict(job)
            keep.append(job)
            ids.add(job["id"])
            by_role[_same_role(job)] = job
    return keep, dropped, copies


def tailored_before(job, resume_dir=None):
    """Tailored resume folders that look like they're for this company, so a
    recommendation can say you've been here before."""
    base = Path(resume_dir or RESUME_DIR) / "build" / "tailored"
    company = re.sub(r"[^a-z0-9]+", "-", job["company"].lower()).strip("-")
    first = company.split("-")[0]
    if not base.is_dir() or len(first) < 3:
        return []
    return sorted(d.name for d in base.iterdir()
                  if d.is_dir() and (d.name.startswith(company + "-")
                                     or d.name.startswith(first + "-")))


# ---------------------------------------------------------------------------
# State: what's been seen, what's recommended, what you decided
# ---------------------------------------------------------------------------

def _empty_state():
    return {"seen": {}, "jobs": {}, "planned": [], "last_scan": None}


def load_state(path=None):
    path = Path(path or STATE_PATH)
    if not path.exists():
        return _empty_state()
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        print("Jobs state corrupt; starting fresh.", file=sys.stderr)
        return _empty_state()
    for key, value in _empty_state().items():
        state.setdefault(key, value)
    return state


def save_state(state, path=None):
    path = Path(path or STATE_PATH)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


@contextlib.contextmanager
def locked_state(path=None):
    """Load, yield, save, under an exclusive lock.

    The scan, the panel and the tailoring worker all write this file, and
    without the lock a "yes" clicked mid-scan would be overwritten.
    """
    path = Path(path or STATE_PATH)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = load_state(path)
            yield state
            save_state(state, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prune(state, today=None):
    today = today or date.today()
    cutoff = (today - timedelta(days=SEEN_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    stale = (today - timedelta(days=EXPIRE_DAYS)).isoformat()
    for job in state["jobs"].values():
        if job["status"] == "new" and job.get("recommended", "") < stale:
            job["status"] = "expired"


def resolve(state, prefix):
    """A job by id or unique id prefix. Returns (job, error)."""
    prefix = prefix.strip().lower()
    hits = [j for k, j in state["jobs"].items() if k.startswith(prefix)]
    if not hits:
        return None, f"no recommended job matches {prefix!r}"
    if len(hits) > 1:
        return None, f"{prefix!r} matches {len(hits)} jobs; use more characters"
    return hits[0], None


# ---------------------------------------------------------------------------
# What the model is told about you
# ---------------------------------------------------------------------------

def resume_text(tex):
    """resume.tex as plain text, heading removed.

    The heading holds the phone number and email address, and the model has
    no use for either.
    """
    # The preamble defines macros that mention \section too, so look for the
    # first real section after \begin{document}.
    tex = tex.split("\\begin{document}", 1)[-1]
    tex = tex.split("\\end{document}")[0]
    first = re.search(r"\\section\*?\{", tex)
    if first:
        tex = tex[first.start():]
    tex = re.sub(r"(?<!\\)%.*", "", tex)
    tex = re.sub(r"\\section\*?\{([^}]*)\}", r"\n## \1\n", tex)
    tex = re.sub(r"\\resumeItem\{", "\n- {", tex)
    tex = re.sub(r"\\href\{[^}]*\}", "", tex)
    tex = re.sub(r"\$\|\$", "|", tex)
    tex = re.sub(r"\\\\", "\n", tex)
    tex = tex.replace("}{", " | ")
    tex = re.sub(r"\\(?:[a-zA-Z]+)\*?", " ", tex)
    tex = re.sub(r"\\([&%$#_])", r"\1", tex)
    tex = tex.replace("{", "").replace("}", "").replace("--", "-")
    lines = [re.sub(r"[ \t]+", " ", l).strip(" |") for l in tex.splitlines()]
    return "\n".join(l for l in lines if l.strip())


def candidate_profile():
    parts = []
    tex = RESUME_DIR / "resume.tex"
    if tex.exists():
        parts.append("# Resume\n" + resume_text(tex.read_text()))
    if PROFILE_PATH.exists():
        parts.append("# What they're looking for\n" + PROFILE_PATH.read_text())
    if not parts:
        raise SystemExit(
            f"Nothing to rank against: no {tex} and no {PROFILE_PATH}. Set "
            "RESUME_DIR to your resume repo, or write profile.md.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

RANK_SYSTEM = """You screen job postings for one candidate, a university \
student. You get their resume and what they're looking for, then postings, one \
per line: id | company | title | location | pay | type | days since posted.

Score each posting 0-100 for how worth their time an application is:
- Fit between the role and the work they have actually done, weighted toward
  the roles they say they want.
- Eligibility: their graduation date and level against the posting. A PhD
  internship, a role needing years of experience, or a graduation window they
  miss scores low however good the fit.
- Their stated preferences, if any: location, company type, anything ruled out.

Be selective. Most postings should score under 60. 80+ means apply this week.
"why" names the specific overlap in under 15 words, for example "GPU
collectives work maps to the NCCL team; Python and CUDA". Never credit them
with experience the resume doesn't show. Score every id you're given."""

RANK_SCHEMA = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"},
                       "score": {"type": "integer"},
                       "why": {"type": "string"}},
        "required": ["id", "score", "why"],
        "additionalProperties": False,
    }}},
    "required": ["scores"],
    "additionalProperties": False,
}


def _posting_line(job):
    age = f"{job['age_days']}d" if job["age_days"] is not None else "?"
    return " | ".join([job["id"], job["company"], job["title"],
                       job["location"] or "-", job["salary"] or "-",
                       KIND_LABEL.get(job["kind"], job["kind"]), age])


def rank_batch(profile, batch, ask=None):
    """{id: (score, why)} for one batch. Raises llm.LLMError on failure."""
    ask = ask or llm.ask_json
    user = (f"<candidate>\n{profile}\n</candidate>\n\n<postings>\n"
            + "\n".join(_posting_line(j) for j in batch) + "\n</postings>")
    data = ask(RANK_SYSTEM, user, RANK_SCHEMA, model=MODEL, effort=EFFORT)
    wanted = {j["id"] for j in batch}
    out = {}
    for s in data.get("scores", []):
        if isinstance(s, dict) and s.get("id") in wanted:
            try:
                score = max(0, min(100, int(s.get("score", 0))))
            except (TypeError, ValueError):
                continue
            out[s["id"]] = (score, str(s.get("why", ""))[:160])
    return out


def rank(profile, jobs, ask=None, workers=RANK_WORKERS):
    """Scores for every job a batch came back for, plus the ids whose batch
    failed. Failed ids aren't marked seen, so the next scan retries them."""
    batches = [jobs[i:i + RANK_BATCH] for i in range(0, len(jobs), RANK_BATCH)]
    scores, failed = {}, []

    def one(batch):
        try:
            return batch, rank_batch(profile, batch, ask), None
        except llm.LLMError as exc:
            return batch, {}, exc

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for batch, result, err in pool.map(one, batches):
            if err:
                print(f"  ranking failed for {len(batch)} posting(s): {err}",
                      file=sys.stderr)
            scores.update(result)
            failed += [j["id"] for j in batch if j["id"] not in result]
    return scores, failed


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def notify(title, text):
    """A macOS notification. Best effort: a missing one costs nothing."""
    if sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(text)}" '
                        f'with title "{esc(title)}"'],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_scan(dry_run=False, cfg=None, fetch=None, ask=None, today=None):
    settings = job_settings(cfg)
    if not settings["enabled"]:
        print("Job scout is off in config.json (jobs.enabled).")
        return []
    fetch = fetch or fetch_listing
    today = today or date.today()

    postings = []
    for source_id in settings["sources"]:
        try:
            got = fetch(source_id)
        except Exception as exc:
            print(f"  couldn't fetch {SOURCES[source_id]['label']}: {exc}",
                  file=sys.stderr)
            continue
        print(f"  {SOURCES[source_id]['label']}: {len(got)} postings")
        postings += got
    if not postings:
        print("No postings fetched; nothing to do.")
        return []

    seen = load_state()["seen"]
    candidates, dropped, copies = prefilter(postings, settings, seen)
    print(f"  {len(candidates)} new to rank; skipped for free: "
          + ", ".join(f"{v} {k}" for k, v in dropped.items() if v))
    if not candidates:
        print("Nothing new since the last scan.")
        if not dry_run:
            with locked_state() as state:
                state["last_scan"] = datetime.now().isoformat(timespec="seconds")
        return []

    print(f"  ranking with {llm.backend() or 'no backend'} "
          f"({len(candidates)} posting(s), {MODEL})...")
    scores, failed = rank(candidate_profile(), candidates, ask=ask)

    picks = []
    for job in candidates:
        if job["id"] not in scores:
            continue
        score, why = scores[job["id"]]
        if score >= settings["min_score"]:
            picks.append(dict(job, score=score, why=why, status="new",
                              recommended=today.isoformat(),
                              tailored_before=tailored_before(job)))
    picks.sort(key=lambda j: -j["score"])

    for j in picks:
        print(f"  [{j['score']:3}] {j['id']}  {j['company']}: {j['title']}"
              f"  ({j['location']})\n        {j['why']}")
    if dry_run:
        print(f"\nDRY RUN. {len(picks)} of {len(scores)} ranked would be "
              f"recommended; nothing saved.")
        return picks

    with locked_state() as state:
        for job in candidates:
            if job["id"] in scores:
                state["seen"][job["id"]] = today.isoformat()
        for copy_id, primary_id in copies.items():
            if primary_id in scores:
                state["seen"][copy_id] = today.isoformat()
        for j in picks:
            # Never overwrite a decision you've already made.
            if j["id"] not in state["jobs"]:
                state["jobs"][j["id"]] = j
        state["last_scan"] = datetime.now().isoformat(timespec="seconds")
        prune(state, today)
    print(f"\n{len(picks)} recommended of {len(scores)} ranked."
          + (f" {len(failed)} will be retried next scan." if failed else ""))
    if picks:
        notify("Job scout", f"{len(picks)} new job(s) worth a look. "
                            f"Top: {picks[0]['company']}")
    return picks


def decide(ids, decision):
    """Record yes/no. A yes queues a tailored resume and starts the worker."""
    results, queued = [], False
    with locked_state() as state:
        for raw in ids:
            job, err = resolve(state, raw)
            if err:
                results.append((raw, False, err))
                continue
            if decision == "yes":
                if job["status"] in ("queued", "tailoring"):
                    results.append((job["id"], True, "already being tailored"))
                    continue
                if job["status"] == "ready":
                    results.append((job["id"], True,
                                    f"already done: {job.get('resume_pdf')}"))
                    continue
                job["status"] = "queued"
                job.pop("error", None)
                queued = True
                msg = "queued for a tailored resume"
            else:
                job["status"] = "no"
                msg = "passed"
            job["decided"] = datetime.now().isoformat(timespec="seconds")
            results.append((job["id"], True,
                            f"{job['company']}: {job['title']} — {msg}"))
    if queued:
        import tailor
        started = tailor.start_worker()
        results.append(("", True, "tailoring worker started" if started
                        else "tailoring worker already running"))
    return results


def where(job):
    extra = job.get("other_locations") or []
    return job["location"] + (f" (+{len(extra)} more)" if extra else "")


def pending(state):
    return sorted((j for j in state["jobs"].values() if j["status"] == "new"),
                  key=lambda j: -j["score"])


def run_list(show_all=False):
    state = load_state()
    jobs = (sorted(state["jobs"].values(), key=lambda j: -j["score"])
            if show_all else pending(state))
    if not jobs:
        print("No recommendations waiting. Run: python src/jobs.py scan")
        return
    for j in jobs:
        status = "" if j["status"] == "new" else f"  [{j['status']}]"
        pay = f"  {j['salary']}" if j.get("salary") else ""
        print(f"{j['id']}  {j['score']:3}  {j['company']}: {j['title']}{status}\n"
              f"          {KIND_LABEL.get(j['kind'], j['kind'])} · "
              f"{where(j)}{pay} · {j['age_days']}d old on "
              f"{j['recommended']}\n"
              f"          {j['why']}\n          {j['url']}")
        if j.get("tailored_before"):
            print(f"          tailored before: {', '.join(j['tailored_before'])}")
    if not show_all:
        print("\nyes: python src/jobs.py yes <id>    no: python src/jobs.py no <id>")


def run_status():
    state = load_state()
    active = [j for j in state["jobs"].values()
              if j["status"] in ("queued", "tailoring", "ready", "failed")]
    if not active:
        print("Nothing tailored yet. Say yes to a job: python src/jobs.py yes <id>")
        return
    for j in sorted(active, key=lambda j: j.get("decided", "")):
        print(f"{j['id']}  {j['status']:9}  {j['company']}: {j['title']}")
        for key, label in (("resume_pdf", "pdf"), ("desktop_pdf", "desktop"),
                           ("report", "report"), ("error", "error")):
            if j.get(key):
                print(f"          {label}: {j[key]}")
        if j.get("session_id"):
            print(f"          continue: cd {RESUME_DIR} && "
                  f"claude --resume {j['session_id']}")


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def digest_items(state, limit=10):
    """What the next digest should mention: new recommendations not yet
    mailed, and resumes finished (or failed) since the last digest."""
    recs = [j for j in pending(state) if not j.get("digested")]
    done = [j for j in state["jobs"].values()
            if j["status"] in ("ready", "failed") and not j.get("done_digested")]
    return recs[:limit], len(recs), done


def mark_digested(recs, done):
    with locked_state() as state:
        for j in recs:
            if j["id"] in state["jobs"]:
                state["jobs"][j["id"]]["digested"] = True
        for j in done:
            if j["id"] in state["jobs"]:
                state["jobs"][j["id"]]["done_digested"] = True


HOW_TO_ANSWER = ("Say yes in the panel's Jobs tab, or run "
                 "python src/jobs.py yes <id>. Yes starts a tailored resume.")


def digest_plain(recs, total, done):
    lines = []
    if done:
        lines += ["RESUMES", "-------"]
        for j in done:
            lines.append(f"* {j['company']}: {j['title']}")
            if j["status"] == "ready":
                lines.append(f"  Ready: {j.get('desktop_pdf') or j.get('resume_pdf')}")
                lines.append(f"  Apply: {j['url']}")
            else:
                lines.append(f"  Failed: {j.get('error', 'see logs/tailor.log')}")
            lines.append("")
    if recs:
        title = "JOBS TO APPLY TO"
        lines += [title, "-" * len(title)]
        for j in recs:
            pay = f" · {j['salary']}" if j.get("salary") else ""
            lines.append(f"* [{j['id']}] {j['company']}: {j['title']}  ({j['score']})")
            lines.append(f"  {KIND_LABEL.get(j['kind'], j['kind'])} · "
                         f"{where(j)}{pay}")
            lines.append(f"  {j['why']}")
            if j.get("tailored_before"):
                lines.append(f"  You've tailored for them before: "
                             f"{', '.join(j['tailored_before'])}")
            lines.append(f"  {j['url']}")
            lines.append("")
        if total > len(recs):
            lines.append(f"...and {total - len(recs)} more: python src/jobs.py list")
        lines.append(HOW_TO_ANSWER)
    return "\n".join(lines)


def digest_html(recs, total, done):
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731

    def section(title, color, rows):
        return (f'<h2 style="font-size:15px;text-transform:uppercase;'
                f'letter-spacing:.06em;color:{color};margin:28px 0 12px;'
                f'padding-bottom:6px;border-bottom:2px solid {color}">'
                f'{title}</h2><ul style="list-style:none;padding:0;margin:0">'
                f'{"".join(rows)}</ul>')

    out = ""
    if done:
        rows = []
        for j in done:
            if j["status"] == "ready":
                detail = (f'Ready: {esc(j.get("desktop_pdf") or j.get("resume_pdf"))}'
                          f'<br><a href="{esc(j["url"])}">Apply</a>')
            else:
                detail = f'Failed: {esc(j.get("error", "see logs/tailor.log"))}'
            rows.append(f'<li style="margin:0 0 14px 0"><div style="font-weight:600;'
                        f'font-size:15px">{esc(j["company"])}: {esc(j["title"])}</div>'
                        f'<div style="color:#555;font-size:13px">{detail}</div></li>')
        out += section("Resumes", "#16a34a", rows)
    if recs:
        rows = []
        for j in recs:
            pay = f" · {esc(j['salary'])}" if j.get("salary") else ""
            rows.append(
                f'<li style="margin:0 0 14px 0"><div style="font-weight:600;'
                f'font-size:15px"><a href="{esc(j["url"])}">{esc(j["company"])}: '
                f'{esc(j["title"])}</a> <span style="color:#777">({j["score"]})'
                f'</span></div><div style="color:#777;font-size:13px">'
                f'{esc(KIND_LABEL.get(j["kind"], j["kind"]))} · {esc(where(j))}'
                f'{pay} · id {esc(j["id"])}</div><div style="color:#555;'
                f'font-size:13px;margin-top:2px">{esc(j["why"])}</div>'
                + (f'<div style="color:#b34700;font-size:13px">Tailored for them '
                   f'before: {esc(", ".join(j["tailored_before"]))}</div>'
                   if j.get("tailored_before") else "") + '</li>')
        more = (f'<p style="color:#888;font-size:13px">...and {total - len(recs)} '
                f'more in the panel.</p>' if total > len(recs) else "")
        out += (section("Jobs to apply to", "#7c3aed", rows) + more
                + f'<p style="color:#888;font-size:13px">{esc(HOW_TO_ANSWER)}</p>')
    return out


def main():
    p = argparse.ArgumentParser(description="Job scout")
    sub = p.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("scan")
    scan.add_argument("--dry-run", action="store_true")
    lst = sub.add_parser("list")
    lst.add_argument("--all", action="store_true")
    for name in ("yes", "no"):
        sp = sub.add_parser(name)
        sp.add_argument("ids", nargs="+")
    sub.add_parser("status")
    a = p.parse_args()

    if a.command == "scan":
        run_scan(dry_run=a.dry_run)
    elif a.command == "list":
        run_list(show_all=a.all)
    elif a.command in ("yes", "no"):
        for job, ok, msg in decide(a.ids, a.command):
            print(f"  {'ok ' if ok else 'ERR'} {job}  {msg}".rstrip())
    else:
        run_status()


if __name__ == "__main__":
    main()
