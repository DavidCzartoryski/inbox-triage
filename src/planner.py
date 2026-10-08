#!/usr/bin/env python3
"""
Calendar events and to-dos from the mail that needs you.

The classifier already reads the body of every message it's asked about, so
the same call also says what belongs on your calendar or your to-do list. An
interview at a fixed time becomes a Calendar event with an alert; an
assessment due in five days becomes a reminder with that due date. No extra
API call.

    python src/planner.py test    # create the calendar and list, trigger
                                  # the macOS permission prompts once
    python src/planner.py list    # what the agent has added so far

Everything goes into a calendar and a Reminders list of its own ("Job Hunt" by
default), so the agent's additions sit in one place, can be hidden with one
checkbox, and never mix with events you made yourself. It only ever adds.
Nothing here edits or deletes an event or a reminder.

Run `test` from Terminal once before scheduling: macOS asks for Automation
permission the first time, and a launchd job can't answer that prompt.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ always has it
    ZoneInfo = None

sys.path.insert(0, str(Path(__file__).resolve().parent))

from settings import load_config  # noqa: E402

KINDS = ("event", "todo")
EVENT_MINUTES = 60          # an interview with no stated end
PLANNED_KEEP = 500          # dedupe memory, newest kept

# The schema the classifier fills in for each message. Kept here so the
# planner owns what it accepts.
PLAN_SCHEMA = {
    "anyOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS)},
                "title": {"type": "string"},
                "start": {"type": ["string", "null"]},
                "end": {"type": ["string", "null"]},
                "due": {"type": ["string", "null"]},
                "tz": {"type": ["string", "null"]},
                "location": {"type": ["string", "null"]},
                "link": {"type": ["string", "null"]},
            },
            "required": ["kind", "title", "start", "end", "due", "tz",
                         "location", "link"],
            "additionalProperties": False,
        },
    ]
}

PLAN_PROMPT = """
Also decide whether the email belongs on the user's calendar or to-do list,
in a "plan" field. Use null unless one of these holds:

  event — a confirmed interview, phone screen, call, info session or onsite
          at a specific date and time. "start" is required; "end" if stated.
  todo  — something the user must do: complete an assessment or coding
          challenge, pick interview times, send documents, reply to a person,
          respond to an offer. "due" is the deadline if one is stated or can
          be computed from the email's Date ("within 5 days" of an email
          dated Oct 8 is due Oct 13); null if none.

Rules for "plan":
- Dates and times are ISO 8601 without an offset: "2026-10-15T14:00", or
  "2026-10-15" when only a date is given. Put the time zone the email states
  in "tz" as an IANA name ("PT" -> "America/Los_Angeles", "ET" ->
  "America/New_York"); null if the email names none.
- "title" is under 8 words and leads with the company: "Stripe online
  assessment (CodeSignal)", "Ramp interview, Platform team".
- "link" is the assessment, scheduling or meeting URL copied exactly from the
  body, or null. "location" is a room, address or "Zoom"; null if none.
- Never invent a date. Verification codes, receipts, rejections and
  "application received" get null. So does anything with no action for the
  user and no fixed time.
"""


# ---------------------------------------------------------------------------
# Turning the model's plan into something safe to write
# ---------------------------------------------------------------------------

def _parse_when(value, tz_name, local_tz=None):
    """ISO string -> (local naive datetime or date, is_all_day). None if bad."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value), True
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None and tz_name and ZoneInfo is not None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        except Exception:
            pass  # unknown zone name: treat the time as local
    if dt.tzinfo is not None:
        dt = dt.astimezone(local_tz).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0), False


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def normalize(plan, msg, now=None, local_tz=None):
    """Validate the model's plan for one message. None means don't add it.

    Everything the model returns is checked here, because it goes into the
    user's calendar: a malformed date, a past event or a link that isn't in
    the email is dropped rather than written.
    """
    if not isinstance(plan, dict) or plan.get("kind") not in KINDS:
        return None
    title = re.sub(r"\s+", " ", str(plan.get("title") or "")).strip()[:80]
    if not title:
        return None
    now = now or datetime.now()
    tz_name = plan.get("tz")
    item = {"kind": plan["kind"], "title": title,
            "location": (plan.get("location") or "")[:200] or None,
            "link": None,
            "uid": msg.get("uid"), "from": msg.get("from", ""),
            "subject": msg.get("subject", "")}

    # A link goes in only if it's really in the email. The model copying a
    # URL is fine; the model producing one is not.
    link = (plan.get("link") or "").strip()
    if link.startswith(("https://", "http://")) and link in (msg.get("body") or ""):
        item["link"] = link

    if plan["kind"] == "event":
        start = _parse_when(plan.get("start"), tz_name, local_tz)
        if not start:
            return None
        start_at, all_day = start
        end = _parse_when(plan.get("end"), tz_name, local_tz)
        if all_day:
            if start_at < now.date():
                return None
            item.update(start=start_at.isoformat(), end=None, all_day=True)
        else:
            if start_at < now - timedelta(hours=1):
                return None      # already happened: a confirmation of the past
            end_at = end[0] if end and not end[1] else None
            if not end_at or end_at <= start_at or end_at - start_at > timedelta(hours=10):
                end_at = start_at + timedelta(minutes=EVENT_MINUTES)
            item.update(start=start_at.isoformat(timespec="minutes"),
                        end=end_at.isoformat(timespec="minutes"), all_day=False)
        item["key"] = f"event:{item['start']}"
        return item

    due = _parse_when(plan.get("due"), tz_name, local_tz)
    if due:
        due_at, all_day = due
        cutoff = now.date() if all_day else now - timedelta(hours=12)
        if due_at < cutoff:
            return None          # the deadline has passed
        item.update(due=due_at.isoformat() if all_day
                    else due_at.isoformat(timespec="minutes"), all_day=all_day)
    else:
        item.update(due=None, all_day=False)
    item["key"] = f"todo:{_slug(title)[:40]}:{(item['due'] or 'none')[:10]}"
    return item


def remind_at(item, now=None):
    """When a reminder should go off: 9:00 the morning before the deadline,
    so a test due at 11:59 PM leaves a full day to do it. Never in the past;
    if the morning before has gone, five minutes from now."""
    now = now or datetime.now()
    if not item.get("due"):
        return None
    if item.get("all_day"):
        due = datetime.fromisoformat(item["due"] + "T23:59")
    else:
        due = datetime.fromisoformat(item["due"])
    ahead = (due - timedelta(days=1)).replace(hour=9, minute=0)
    if ahead > now:
        return ahead
    soon = now + timedelta(minutes=5)
    return soon if soon < due else None


def describe(item):
    """One line for the digest and the log."""
    if item["kind"] == "event":
        when = datetime.fromisoformat(item["start"])
        stamp = (when.strftime("%a %b %-d") if item.get("all_day")
                 else when.strftime("%a %b %-d, %-I:%M %p"))
        return f"Calendar: {stamp}"
    if item.get("due"):
        when = datetime.fromisoformat(item["due"])
        stamp = (when.strftime("%a %b %-d") if item.get("all_day")
                 else when.strftime("%a %b %-d, %-I:%M %p"))
        return f"To-do, due {stamp}"
    return "To-do"


# ---------------------------------------------------------------------------
# AppleScript
# ---------------------------------------------------------------------------

def _q(text):
    """An AppleScript string literal."""
    text = str(text or "")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _date_lines(var, when):
    """Build a date without parsing a string, which is locale-dependent.

    Day goes to 1 first so that moving from, say, Jan 31 to February can't
    overflow into March.
    """
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    if not isinstance(when, datetime):
        when = datetime(when.year, when.month, when.day)
    return "\n".join([
        f"set {var} to current date",
        f"set day of {var} to 1",
        f"set year of {var} to {when.year}",
        f"set month of {var} to {when.month}",
        f"set day of {var} to {when.day}",
        f"set time of {var} to {when.hour * 3600 + when.minute * 60}",
    ])


def _notes(item):
    lines = []
    if item.get("link"):
        lines.append(item["link"])
    if item.get("note"):
        lines.append(item["note"])
    if item.get("from"):
        lines.append(f"From: {item['from']}")
    if item.get("subject"):
        lines.append(f"Subject: {item['subject']}")
    lines.append("Added by inbox-triage")
    return "\n".join(lines)


def event_script(calendar_name, item, alert_minutes=30):
    start = datetime.fromisoformat(item["start"])
    end = (datetime.fromisoformat(item["end"]) if item.get("end")
           else start + timedelta(days=1))
    props = [f"summary:{_q(item['title'])}", "start date:startDate",
             "end date:endDate", f"description:{_q(_notes(item))}"]
    if item.get("all_day"):
        props.append("allday event:true")
    if item.get("location"):
        props.append(f"location:{_q(item['location'])}")
    if item.get("link"):
        props.append(f"url:{_q(item['link'])}")
    alarm = ""
    if alert_minutes and not item.get("all_day"):
        alarm = (f"\n  make new display alarm at end of display alarms of ev "
                 f"with properties {{trigger interval:-{int(alert_minutes)}}}")
    return f'''
{_date_lines("startDate", start)}
{_date_lines("endDate", end)}
tell application "Calendar"
  if not (exists calendar {_q(calendar_name)}) then
    make new calendar with properties {{name:{_q(calendar_name)}}}
  end if
  set ev to make new event at end of events of calendar {_q(calendar_name)} ¬
    with properties {{{", ".join(props)}}}{alarm}
end tell
'''


def reminder_script(list_name, item, now=None):
    lines = []
    if item.get("due"):
        lines.append(_date_lines("dueDate", item["due"]))
    alert = remind_at(item, now)
    if alert:
        lines.append(_date_lines("alertDate", alert))
    sets = []
    if item.get("due"):
        prop = "allday due date" if item.get("all_day") else "due date"
        sets.append(f"    set {prop} of r to dueDate")
    if alert:
        sets.append("    set remind me date of r to alertDate")
    setters = ("\n" + "\n".join(sets)) if sets else ""
    return f'''
{chr(10).join(lines)}
tell application "Reminders"
  if not (exists list {_q(list_name)}) then
    make new list with properties {{name:{_q(list_name)}}}
  end if
  tell list {_q(list_name)}
    set r to make new reminder with properties ¬
      {{name:{_q(item['title'])}, body:{_q(_notes(item))}, priority:1}}{setters}
  end tell
end tell
'''


def _osascript(script, timeout=60):
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True,
                             text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("osascript not found: Calendar and Reminders need macOS")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Calendar or Reminders didn't respond in time")
    if out.returncode != 0:
        err = out.stderr.strip()
        if "-1743" in err or "not authorized" in err.lower():
            raise RuntimeError(
                "macOS denied Automation access. Run `python src/planner.py "
                "test` from Terminal once and approve it, or allow it under "
                "System Settings > Privacy & Security > Automation.")
        raise RuntimeError(f"AppleScript failed: {err}")
    return out.stdout


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------

def planner_settings(cfg=None):
    cfg = cfg or load_config()
    p = cfg.get("planner") or {}
    return {
        "enabled": bool(p.get("enabled", True)),
        "calendar": p.get("calendar") or "Job Hunt",
        "reminders_list": p.get("reminders_list") or "Job Hunt",
        "alert_minutes": int(p.get("alert_minutes", 30) or 0),
    }


class Planner:
    """Adds items once each. Remembers what it added in the triage state."""

    def __init__(self, state, cfg=None, run=None, now=None):
        self.state = state
        self.settings = planner_settings(cfg)
        self.run = run or _osascript
        self.now = now
        state.setdefault("planned", [])

    @property
    def enabled(self):
        return self.settings["enabled"]

    def seen(self, key):
        return any(p.get("key") == key for p in self.state["planned"])

    def add(self, item, dry_run=False):
        """Write one item. Returns (status, description).

        status is "added", "duplicate", "dry-run" or "failed". A failure
        never raises: the message is still flagged in the inbox, and the
        digest says the item couldn't be added so it isn't lost silently.
        """
        what = describe(item)
        if self.seen(item["key"]):
            return "duplicate", what
        if dry_run:
            return "dry-run", what
        try:
            if item["kind"] == "event":
                self.run(event_script(self.settings["calendar"], item,
                                      self.settings["alert_minutes"]))
            else:
                self.run(reminder_script(self.settings["reminders_list"], item,
                                         self.now))
        except RuntimeError as exc:
            print(f"  couldn't add to {item['kind']}: {exc}", file=sys.stderr)
            return "failed", what
        self.state["planned"].append({
            "key": item["key"], "kind": item["kind"], "title": item["title"],
            "when": item.get("start") or item.get("due"),
            "added": datetime.now().isoformat(timespec="seconds")})
        self.state["planned"] = self.state["planned"][-PLANNED_KEEP:]
        return "added", what


def add_todo(title, due=None, link=None, note="", state=None, dry_run=False):
    """A to-do from outside the mail path, e.g. "apply to this job".

    `state` is any dict to dedupe against; jobs.py passes its own.
    """
    item = {"kind": "todo", "title": title[:80], "due": due,
            "all_day": bool(due) and len(due) == 10, "link": link,
            "location": None, "note": note,
            "key": f"todo:{_slug(title)[:40]}:{(due or 'none')[:10]}"}
    planner = Planner(state if state is not None else {})
    if not planner.enabled:
        return "disabled", ""
    return planner.add(item, dry_run=dry_run)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_test():
    s = planner_settings()
    print(f"Calendar: {s['calendar']!r}   Reminders list: {s['reminders_list']!r}")
    for app, script in (
        ("Calendar", f'''tell application "Calendar"
  if not (exists calendar {_q(s["calendar"])}) then
    make new calendar with properties {{name:{_q(s["calendar"])}}}
  end if
  return name of calendar {_q(s["calendar"])}
end tell'''),
        ("Reminders", f'''tell application "Reminders"
  if not (exists list {_q(s["reminders_list"])}) then
    make new list with properties {{name:{_q(s["reminders_list"])}}}
  end if
  return name of list {_q(s["reminders_list"])}
end tell'''),
    ):
        try:
            name = _osascript(script).strip()
            print(f"  {app} OK: {name!r} is ready")
        except RuntimeError as exc:
            print(f"  {app} FAILED: {exc}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description="Calendar and Reminders writer")
    p.add_argument("command", choices=["test", "list"])
    args = p.parse_args()
    if args.command == "test":
        run_test()
        return
    import os
    state_path = Path(os.environ.get("TRIAGE_STATE", "state.json"))
    planned = []
    if state_path.exists():
        planned = json.loads(state_path.read_text()).get("planned", [])
    if not planned:
        print("Nothing added yet.")
    for p_ in planned:
        print(f"  {p_['added'][:16]}  {p_['kind']:5}  {p_.get('when') or '-':16}  "
              f"{p_['title']}")


if __name__ == "__main__":
    main()
