#!/usr/bin/env python3
"""
Turn the timing you picked in the panel into real launchd jobs.

    python src/schedule_agent.py install     # write plists and load them
    python src/schedule_agent.py status      # what's loaded right now
    python src/schedule_agent.py uninstall   # unload and remove

Rewrites the plists from config.json every time, so changing the cadence in
the panel is just `install` again. macOS only — on Linux this prints the cron
lines instead of installing anything.
"""

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from settings import digest_hours, load_config, refresh_minutes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AGENTS = Path.home() / "Library" / "LaunchAgents"
TRIAGE_LABEL = "com.inboxtriage.triage"
DIGEST_LABEL = "com.inboxtriage.digest"


def _program(command):
    """Run through a login shell so .env and PATH are loaded the same way."""
    env_file = ROOT / ".env"
    prefix = f"source {env_file} && " if env_file.exists() else ""
    return ["/bin/bash", "-lc",
            f"{prefix}cd {ROOT} && "
            f"{sys.executable} src/icloud_triage.py {command}"]


def build_plists(cfg):
    minutes = refresh_minutes(cfg)
    triage = {
        "Label": TRIAGE_LABEL,
        "ProgramArguments": _program("triage"),
        "StartInterval": minutes * 60,
        "StandardOutPath": str(ROOT / "logs" / "triage.log"),
        "StandardErrorPath": str(ROOT / "logs" / "triage.err"),
        # Don't fire while the Mac is asleep and then stampede on wake.
        "ProcessType": "Background",
    }
    digest = {
        "Label": DIGEST_LABEL,
        "ProgramArguments": _program("digest"),
        "StartCalendarInterval": [{"Hour": h, "Minute": 0}
                                  for h in digest_hours(cfg)],
        "StandardOutPath": str(ROOT / "logs" / "digest.log"),
        "StandardErrorPath": str(ROOT / "logs" / "digest.err"),
        "ProcessType": "Background",
    }
    return triage, digest


def _launchctl(*args, check=False):
    return subprocess.run(["launchctl", *args], capture_output=True,
                          text=True, check=check)


def install(cfg):
    (ROOT / "logs").mkdir(exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    minutes, hours = refresh_minutes(cfg), digest_hours(cfg)

    for plist in build_plists(cfg):
        path = AGENTS / f"{plist['Label']}.plist"
        _launchctl("unload", str(path))            # no-op if not loaded
        path.write_bytes(plistlib.dumps(plist))
        result = _launchctl("load", str(path))
        if result.returncode != 0:
            print(f"  failed to load {plist['Label']}: {result.stderr.strip()}",
                  file=sys.stderr)
        else:
            print(f"  loaded {plist['Label']}")

    print(f"\nTriage every {minutes} min. "
          f"Digest at {', '.join(f'{h}:00' for h in hours)}.")
    if minutes >= 480:
        print("Note: at this interval an assessment can sit unflagged for "
              f"up to {minutes // 60}h. Checking more often costs no more — "
              "you're billed per email classified, not per check.")
    if not (ROOT / ".env").exists():
        print("Warning: no .env found. The jobs will run with whatever "
              "environment launchd gives them and will likely fail. "
              "Copy .env.example to .env first.", file=sys.stderr)


def status():
    out = _launchctl("list").stdout
    for label in (TRIAGE_LABEL, DIGEST_LABEL):
        line = next((l for l in out.splitlines() if label in l), None)
        print(f"{label}: {'loaded — ' + line.split()[0] if line else 'not loaded'}")
    cfg = load_config()
    print(f"\nconfig says: every {refresh_minutes(cfg)} min, "
          f"digest at {digest_hours(cfg)}")


def uninstall():
    for label in (TRIAGE_LABEL, DIGEST_LABEL):
        path = AGENTS / f"{label}.plist"
        _launchctl("unload", str(path))
        if path.exists():
            path.unlink()
            print(f"  removed {label}")
        else:
            print(f"  {label} was not installed")


def cron_lines(cfg):
    minutes, hours = refresh_minutes(cfg), digest_hours(cfg)
    every = f"*/{minutes}" if minutes < 60 else "0"
    hour = "*" if minutes < 60 else f"*/{minutes // 60}"
    return [
        f"{every} {hour} * * * cd {ROOT} && . ./.env && "
        f"python3 src/icloud_triage.py triage >> logs/triage.log 2>&1",
        f"0 {','.join(str(h) for h in hours)} * * * cd {ROOT} && . ./.env && "
        f"python3 src/icloud_triage.py digest >> logs/digest.log 2>&1",
    ]


def main():
    p = argparse.ArgumentParser(description="Schedule the triage agent")
    p.add_argument("command", choices=["install", "status", "uninstall", "cron"])
    args = p.parse_args()
    cfg = load_config()

    if sys.platform != "darwin" and args.command != "cron":
        print("launchd is macOS only. Use these cron lines instead "
              "(`crontab -e`):\n")
        print("\n".join(cron_lines(cfg)))
        return

    if args.command == "install":
        install(cfg)
    elif args.command == "status":
        status()
    elif args.command == "uninstall":
        uninstall()
    else:
        print("\n".join(cron_lines(cfg)))


if __name__ == "__main__":
    main()
