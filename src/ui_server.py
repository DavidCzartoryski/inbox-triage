#!/usr/bin/env python3
"""
The settings panel. Stdlib only — no web framework, no build step.

    python src/ui_server.py           # opens a 250x350 window
    python src/ui_server.py --no-open # just serve, print the URL

Binds 127.0.0.1 and nothing else, so it is not reachable from the network.
Every request must also carry a token generated fresh at startup and present
only in the URL we open. That's what stops an arbitrary page in your browser
from POSTing new filter rules at the panel behind your back — localhost alone
wouldn't, since any page can issue requests to it.
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

import keystore  # noqa: E402
from settings import (  # noqa: E402
    DIGEST_CHOICES, PREFILTER_CHOICES, REFRESH_CHOICES, RULES,
    load_config, save_config,
)

ROOT = Path(__file__).resolve().parents[1]
PANEL = Path(__file__).resolve().parent / "ui" / "panel.html"
TOKEN = secrets.token_urlsafe(16)


def read_json_file(name):
    path = ROOT / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "inbox-triage-panel"

    def log_message(self, *args):
        pass  # the panel is chatty; stay quiet unless something breaks

    # -- helpers ----------------------------------------------------------
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        # No reason for the panel to ever be framed or sniffed.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self, query):
        # Host must be loopback: blocks DNS-rebinding, where a hostile domain
        # resolves to 127.0.0.1 and talks to us with the browser's privileges.
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return False
        return secrets.compare_digest(
            (query.get("token") or [""])[0], TOKEN)

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            return self._send(403, {"error": "bad or missing token"})

        if url.path == "/":
            if not PANEL.exists():
                return self._send(500, "panel.html missing", "text/plain")
            html = PANEL.read_text().replace("__TOKEN__", TOKEN)
            return self._send(200, html, "text/html; charset=utf-8")

        if url.path == "/api/config":
            return self._send(200, {
                "config": load_config(),
                "rules": [{k: r[k] for k in ("id", "label", "hint")}
                          for r in RULES],
                "refresh_choices": REFRESH_CHOICES,
                "digest_choices": DIGEST_CHOICES,
                "prefilter_choices": PREFILTER_CHOICES,
            })

        if url.path == "/api/subscriptions":
            data = read_json_file("unsubscribe_candidates.json")
            return self._send(200, data or {"candidates": [], "scanned": 0})

        # Deliberately reports only whether each secret is set and where it
        # came from. There is no endpoint that returns a secret's value, so a
        # leaked token can't be used to read your API key back out.
        if url.path == "/api/secrets":
            return self._send(200, {"secrets": keystore.status(),
                                    "keychain": keystore.available()})

        if url.path == "/api/status":
            state = read_json_file("state.json") or {}
            queue = state.get("queue", [])
            counts = {}
            for item in queue:
                counts[item.get("category", "?")] = counts.get(
                    item.get("category", "?"), 0) + 1
            return self._send(200, {
                "pending_digest": len(queue),
                "counts": counts,
                "last_digest": state.get("last_digest"),
                "configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
            })

        return self._send(404, {"error": "no such path"})

    def do_POST(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            return self._send(403, {"error": "bad or missing token"})

        length = int(self.headers.get("Content-Length") or 0)
        if length > 64_000:
            return self._send(413, {"error": "payload too large"})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid json"})

        if url.path == "/api/config":
            cfg = load_config()
            if isinstance(payload.get("filters"), dict):
                known = set(cfg["filters"])
                cfg["filters"].update({k: bool(v) for k, v in
                                       payload["filters"].items() if k in known})
            if payload.get("refresh") in {c["id"] for c in REFRESH_CHOICES}:
                cfg["refresh"] = payload["refresh"]
            if str(payload.get("digests_per_day")) in {c["id"] for c in DIGEST_CHOICES}:
                cfg["digests_per_day"] = str(payload["digests_per_day"])
            if payload.get("prefilter") in {c["id"] for c in PREFILTER_CHOICES}:
                cfg["prefilter"] = payload["prefilter"]
            if isinstance(payload.get("allowlist"), list):
                cfg["allowlist"] = [str(s).strip() for s in payload["allowlist"]
                                    if str(s).strip()][:200]
            save_config(cfg)
            return self._send(200, {"ok": True, "config": cfg})

        if url.path == "/api/secrets":
            name = payload.get("name")
            if name not in keystore.SECRETS:
                return self._send(400, {"error": "unknown secret name"})
            value = payload.get("value") or ""
            if payload.get("clear"):
                keystore.delete_secret(name)
                return self._send(200, {"ok": True, "secrets": keystore.status()})
            # The value arrives in the request body, never the query string, so
            # it stays out of browser history and any access log.
            ok, message = keystore.set_secret(name, value)
            del value
            return self._send(200 if ok else 400,
                              {"ok": ok, "message": message,
                               "secrets": keystore.status()})

        return self._send(404, {"error": "no such path"})


def open_window(url, width=250, height=350):
    """A real small window if we can get one, else the default browser."""
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser"):
            try:
                subprocess.run(
                    ["open", "-na", app, "--args", f"--app={url}",
                     f"--window-size={width},{height}"],
                    check=True, capture_output=True, timeout=10)
                return True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                    FileNotFoundError):
                continue
    webbrowser.open(url)
    return False


def main():
    p = argparse.ArgumentParser(description="inbox-triage settings panel")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true")
    a = p.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/?token={TOKEN}"
    print(f"Settings panel: {url}")
    print("Ctrl-C to stop.")
    if not a.no_open:
        threading.Timer(0.3, open_window, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        server.shutdown()


if __name__ == "__main__":
    main()
