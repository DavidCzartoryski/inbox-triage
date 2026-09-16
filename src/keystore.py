#!/usr/bin/env python3
"""
Secrets in the macOS Keychain instead of a file.

    python src/keystore.py set anthropic-api-key    # prompts, no echo
    python src/keystore.py status
    python src/keystore.py delete anthropic-api-key

Why not just .env: a key in a file is a key you can accidentally commit, cat
in a screen share, or leave world-readable. The Keychain is encrypted at rest,
revocable from Keychain Access, and never shows up in a diff. `.env` still
works and still wins if set — this is the better default, not a replacement.

Writes feed the value through stdin rather than argv, because command-line
arguments are visible to `ps` while the process runs.
"""

import argparse
import getpass
import os
import subprocess
import sys

SERVICE = "inbox-triage"

# name -> (environment variable checked first, human label)
SECRETS = {
    "anthropic-api-key": ("ANTHROPIC_API_KEY", "Anthropic API key"),
    "mail-app-password": ("MAIL_APP_PASSWORD", "Mail app-specific password"),
}


def available():
    """True if we can talk to a Keychain at all."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["security", "-h"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_secret(name, service=SERVICE):
    """The stored value, or None. Never raises."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", name, "-s", service, "-w"],
            capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def set_secret(name, value, service=SERVICE):
    """Store or replace. Returns (ok, message) and never echoes the value."""
    if not value or not value.strip():
        return False, "empty value"
    if not available():
        return False, "no Keychain on this platform; use .env instead"
    value = value.strip()
    try:
        # -U updates an existing item. The bare -w makes `security` prompt,
        # and it asks twice, so the value goes in twice on stdin — keeping it
        # out of argv, where `ps` could read it.
        proc = subprocess.run(
            ["security", "add-generic-password", "-a", name, "-s", service,
             "-U", "-w"],
            input=f"{value}\n{value}\n", capture_output=True, text=True,
            timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"keychain write failed: {type(exc).__name__}"

    if get_secret(name, service) != value:
        detail = (proc.stderr or "").strip().splitlines()
        return False, f"keychain did not store the value: {detail[-1] if detail else '?'}"
    return True, "stored in Keychain"


def delete_secret(name, service=SERVICE):
    try:
        out = subprocess.run(
            ["security", "delete-generic-password", "-a", name, "-s", service],
            capture_output=True, text=True, timeout=10)
        return out.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def resolve(name):
    """Environment first, then Keychain.

    Environment wins so a one-off `ANTHROPIC_API_KEY=... python ...` or a CI
    secret always overrides whatever is stored locally.
    """
    env_var = SECRETS.get(name, (None, None))[0]
    if env_var:
        value = os.environ.get(env_var, "").strip()
        # Ignore the shipped placeholder so it can't shadow a real stored key.
        if value and "..." not in value:
            return value
    return get_secret(name)


def api_key():
    return resolve("anthropic-api-key")


def status():
    """Where each secret is coming from, without revealing any of it."""
    out = {}
    for name, (env_var, label) in SECRETS.items():
        env_val = os.environ.get(env_var, "").strip() if env_var else ""
        if env_val and "..." not in env_val:
            out[name] = {"label": label, "set": True, "source": "environment"}
        elif get_secret(name):
            out[name] = {"label": label, "set": True, "source": "keychain"}
        else:
            out[name] = {"label": label, "set": False, "source": None}
    return out


def main():
    p = argparse.ArgumentParser(description="Keychain-backed secrets")
    p.add_argument("command", choices=["set", "status", "delete"])
    p.add_argument("name", nargs="?", choices=sorted(SECRETS), default=None)
    a = p.parse_args()

    if a.command == "status":
        for name, info in status().items():
            where = info["source"] or "not set"
            print(f"{info['label']:34} {where}")
        return

    if not a.name:
        p.error(f"{a.command} needs a name: {', '.join(sorted(SECRETS))}")

    if a.command == "set":
        value = getpass.getpass(f"{SECRETS[a.name][1]} (not echoed): ")
        ok, msg = set_secret(a.name, value)
        print(msg if ok else f"failed: {msg}", file=sys.stdout if ok else sys.stderr)
        sys.exit(0 if ok else 1)

    print("deleted" if delete_secret(a.name) else "nothing to delete")


if __name__ == "__main__":
    main()
