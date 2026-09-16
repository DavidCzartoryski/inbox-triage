#!/usr/bin/env python3
"""
Two interchangeable mail backends behind one interface.

  AppleScriptBackend — drives Mail.app on macOS. No password anywhere;
                       macOS Automation permission is the credential.
                       Fast for small batches, slow on big mailboxes.

  IMAPBackend        — speaks IMAP/SMTP to iCloud or Gmail. Needs an
                       app-specific password. Filters server-side, so it
                       stays fast at any scale.

Pick with MAIL_BACKEND=applescript|imap. The rule of thumb: AppleScript for
incremental triage, IMAP for bulk work. Driving Mail.app through AppleScript
iterates messages one at a time and will hit the Apple Event timeout on a
mailbox with thousands of messages.

Credentials, when needed, are read from the macOS Keychain by default and
never written to disk in plaintext:

    security add-generic-password -a "you@icloud.com" -s "mail-triage" -w
"""

import email
import imaplib
import json
import os
import re
import smtplib
import subprocess
import sys
from email.header import decode_header, make_header
from email.message import EmailMessage

FIELD_SEP = "\x1e"
RECORD_SEP = "\x1d"

IMAP_HOSTS = {
    "icloud": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "gmail": ("imap.gmail.com", 993, "smtp.gmail.com", 587),
}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def get_password(account, service="mail-triage"):
    """Keychain first, environment second. Never a file."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", account,
             "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # not macOS, or no keychain access

    pw = os.environ.get("MAIL_APP_PASSWORD") or os.environ.get("ICLOUD_APP_PASSWORD")
    if not pw:
        sys.exit(
            "No password found. Either store one in the Keychain:\n"
            f'  security add-generic-password -a "{account}" '
            f'-s "mail-triage" -w\n'
            "or set MAIL_APP_PASSWORD in the environment.\n"
            "Or switch to MAIL_BACKEND=applescript, which needs no password."
        )
    return pw


def mask(secret):
    """For diagnostics. Never prints the value."""
    return "****" if secret else "(unset)"


# ---------------------------------------------------------------------------
# AppleScript backend
# ---------------------------------------------------------------------------

class AppleScriptBackend:
    """No credentials. Requires macOS, Mail.app, and Automation permission."""

    name = "applescript"
    supports_html_digest = False  # Mail.app outgoing messages are plain text

    def __init__(self, account=None, filtered_folder="Filtered", timeout=300):
        self.account = account or os.environ.get("MAIL_ACCOUNT", "iCloud")
        self.filtered = filtered_folder
        self.timeout = timeout

    def _run(self, script):
        try:
            out = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=self.timeout,
            )
        except FileNotFoundError:
            sys.exit("osascript not found — the AppleScript backend needs macOS.")
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                "Mail.app did not respond in time. AppleScript walks messages "
                "one by one; lower the fetch count or use MAIL_BACKEND=imap."
            )
        if out.returncode != 0:
            err = out.stderr.strip()
            if "-1743" in err or "not authorized" in err.lower():
                sys.exit(
                    "macOS denied Automation access to Mail.app.\n"
                    "Approve it under System Settings > Privacy & Security > "
                    "Automation, then run again."
                )
            raise RuntimeError(f"AppleScript failed: {err}")
        return out.stdout

    def fetch_recent_ids(self, count=50):
        """Just the ids of the newest `count` messages, newest first.

        Cheaper than fetching bodies, but only about 3.7x per message — the
        expensive part is indexing into the mailbox, not reading content. So
        this is worth doing (an idle run drops from ~15s to ~8s, and pulls no
        bodies at all), but the window can't be widened indefinitely: 150 ids
        cost more than the 25-body fetch this replaced. See the measurements
        in icloud_triage.APPLESCRIPT_ID_WINDOW.
        """
        script = f'''
        set fs to (ASCII character 30)
        set output to ""
        with timeout of {self.timeout} seconds
          tell application "Mail"
            set box to inbox
            set n to count of messages of box
            if n is 0 then return ""
            if n > {count} then set n to {count}
            repeat with i from 1 to n
              set output to output & (id of message i of box as string) & fs
            end repeat
          end tell
        end timeout
        return output
        '''
        raw = self._run(script)
        return [p.strip() for p in raw.split(FIELD_SEP) if p.strip()]

    def fetch_by_ids(self, ids, body_chars=1800, window=50):
        """Full records for specific ids. Reading content does not mark read."""
        if not ids:
            return []
        # Walk only the newest `window` messages, not the whole mailbox: the
        # ids came from that window, and scanning 4,000 messages to find 25
        # takes minutes. Stop as soon as everything wanted has been found.
        #
        # Ids are wrapped in commas on both sides before matching, so id 123
        # can't be matched by the substring inside 1234.
        wanted = "," + ",".join(str(int(i)) for i in ids) + ","
        script = f'''
        set fs to (ASCII character 30)
        set rs to (ASCII character 29)
        set wanted to "{wanted}"
        set target to {len(ids)}
        set found to 0
        set output to ""
        with timeout of {self.timeout} seconds
          tell application "Mail"
            set box to inbox
            set n to count of messages of box
            if n > {window} then set n to {window}
            repeat with i from 1 to n
              set m to message i of box
              set mid to (id of m as string)
              if wanted contains ("," & mid & ",") then
                try
                  set theBody to content of m
                on error
                  set theBody to ""
                end try
                try
                  set theReplyTo to reply to of m
                  if theReplyTo is missing value then set theReplyTo to ""
                on error
                  set theReplyTo to ""
                end try
                set output to output & mid & fs & ¬
                  (sender of m) & fs & theReplyTo & fs & (subject of m) & fs & ¬
                  ((date received of m) as string) & fs & ¬
                  (read status of m as string) & fs & theBody & rs
                set found to found + 1
                if found is greater than or equal to target then exit repeat
              end if
            end repeat
          end tell
        end timeout
        return output
        '''
        return self._parse_records(self._run(script), body_chars)

    def _parse_records(self, raw, body_chars=1800):
        messages = []
        for record in raw.split(RECORD_SEP):
            parts = record.split(FIELD_SEP)
            if len(parts) < 7:
                continue
            messages.append({
                "uid": parts[0].strip(),
                "from": parts[1].strip(),
                "reply_to": parts[2].strip(),
                "subject": parts[3].strip(),
                "date": parts[4].strip(),
                "read": parts[5].strip().lower() == "true",
                "body": re.sub(r"\n{3,}", "\n\n", parts[6]).strip()[:body_chars],
                "has_unsubscribe": False,  # not exposed by AppleScript
            })
        return messages

    def fetch_recent(self, count=25, body_chars=1800):
        """Newest `count` inbox messages. Reading content does not mark read."""
        script = f'''
        set fs to (ASCII character 30)
        set rs to (ASCII character 29)
        set output to ""
        with timeout of {self.timeout} seconds
          tell application "Mail"
            set box to inbox
            set n to count of messages of box
            if n is 0 then return ""
            if n > {count} then set n to {count}
            repeat with i from 1 to n
              set m to message i of box
              try
                set theBody to content of m
              on error
                set theBody to ""
              end try
              try
                set theReplyTo to reply to of m
                if theReplyTo is missing value then set theReplyTo to ""
              on error
                set theReplyTo to ""
              end try
              set output to output & (id of m as string) & fs & ¬
                (sender of m) & fs & theReplyTo & fs & (subject of m) & fs & ¬
                ((date received of m) as string) & fs & ¬
                (read status of m as string) & fs & theBody & rs
            end repeat
          end tell
        end timeout
        return output
        '''
        raw = self._run(script)
        messages = []
        for record in raw.split(RECORD_SEP):
            parts = record.split(FIELD_SEP)
            if len(parts) < 7:
                continue
            messages.append({
                "uid": parts[0].strip(),
                "from": parts[1].strip(),
                "reply_to": parts[2].strip(),
                "subject": parts[3].strip(),
                "date": parts[4].strip(),
                "read": parts[5].strip().lower() == "true",
                "body": re.sub(r"\n{3,}", "\n\n", parts[6]).strip()[:body_chars],
                "has_unsubscribe": False,  # not exposed by AppleScript
            })
        return messages

    def flag(self, uid):
        self._run(f'''
        tell application "Mail"
          set m to first message of inbox whose id is {int(uid)}
          set flagged status of m to true
        end tell
        ''')

    def move_to_filtered(self, uid):
        """File into the Filtered mailbox of the message's OWN account.

        Mail.app's `inbox` is the unified inbox across every configured
        account. Filing everything into one named account would physically
        move mail between providers — a university Exchange message would end
        up inside a personal iCloud account, out of the school mailbox
        entirely. So resolve the account per message and create Filtered there
        on first use.
        """
        script = f'''
        tell application "Mail"
          set m to first message of inbox whose id is {int(uid)}
          set acct to account of (mailbox of m)
          try
            set targetBox to mailbox "{self.filtered}" of acct
          on error
            try
              make new mailbox with properties {{name:"{self.filtered}"}} at acct
              set targetBox to mailbox "{self.filtered}" of acct
            on error
              set targetBox to mailbox "{self.filtered}"
            end try
          end try
          set mailbox of m to targetBox
        end tell
        '''
        try:
            self._run(script)
            return True
        except RuntimeError as exc:
            print(f"  move failed ({exc}); left in inbox", file=sys.stderr)
            return False

    def send(self, to_addr, subject, plain_body, html_body=None):
        def esc(s):
            return s.replace("\\", "\\\\").replace('"', '\\"')
        self._run(f'''
        tell application "Mail"
          set msg to make new outgoing message with properties ¬
            {{subject:"{esc(subject)}", content:"{esc(plain_body)}", visible:false}}
          tell msg
            make new to recipient at end of to recipients ¬
              with properties {{address:"{esc(to_addr)}"}}
          end tell
          send msg
        end tell
        ''')

    def close(self):
        pass


# ---------------------------------------------------------------------------
# IMAP backend
# ---------------------------------------------------------------------------

class IMAPBackend:
    """Needs an app-specific password. Fast at any mailbox size."""

    name = "imap"
    supports_html_digest = True

    def __init__(self, address, provider="icloud", filtered_folder="Filtered"):
        if provider not in IMAP_HOSTS:
            sys.exit(f"Unsupported provider '{provider}'. "
                     f"Outlook requires OAuth2 and is not supported.")
        self.address = address
        self.filtered = filtered_folder
        (self.imap_host, self.imap_port,
         self.smtp_host, self.smtp_port) = IMAP_HOSTS[provider]
        self.password = get_password(address)
        self.imap = None

    def _connect(self):
        if self.imap:
            return self.imap
        self.imap = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        try:
            self.imap.login(self.address, self.password)
        except imaplib.IMAP4.error:
            self.imap.login(self.address.split("@")[0], self.password)
        self.imap.select("INBOX")
        return self.imap

    @staticmethod
    def _decode(value):
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    def fetch_since(self, last_uid, limit=60, body_chars=1800):
        imap = self._connect()
        status, data = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if status != "OK" or not data or not data[0]:
            return []
        uids = sorted(int(u) for u in data[0].split() if int(u) > last_uid)[-limit:]

        out = []
        for uid in uids:
            status, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email.message_from_bytes(data[0][1])
            out.append({
                "uid": str(uid),
                "from": self._decode(msg.get("From")),
                "reply_to": self._decode(msg.get("Reply-To")),
                "subject": self._decode(msg.get("Subject")),
                "date": self._decode(msg.get("Date")),
                "has_unsubscribe": bool(msg.get("List-Unsubscribe")),
                "body": self._extract_body(msg)[:body_chars],
            })
        return out

    @staticmethod
    def _extract_body(msg):
        text = ""
        if msg.is_multipart():
            for part in msg.walk():
                if "attachment" in str(part.get("Content-Disposition") or ""):
                    continue
                if part.get_content_type() == "text/plain":
                    raw = part.get_payload(decode=True) or b""
                    text = raw.decode(part.get_content_charset() or "utf-8", "replace")
                    break
        else:
            raw = msg.get_payload(decode=True) or b""
            text = raw.decode(msg.get_content_charset() or "utf-8", "replace")
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def flag(self, uid):
        self._connect().uid("STORE", uid, "+FLAGS", "(\\Flagged)")

    def move_to_filtered(self, uid):
        imap = self._connect()
        imap.create(f'"{self.filtered}"')
        try:
            status, _ = imap.uid("MOVE", uid, f'"{self.filtered}"')
            if status == "OK":
                return True
        except imaplib.IMAP4.error:
            pass
        status, _ = imap.uid("COPY", uid, f'"{self.filtered}"')
        if status != "OK":
            return False
        imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        imap.expunge()
        return True

    def send(self, to_addr, subject, plain_body, html_body=None):
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.address
        msg["To"] = to_addr
        msg.set_content(plain_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            server.starttls()
            server.login(self.address, self.password)
            server.send_message(msg)

    def close(self):
        if self.imap:
            try:
                self.imap.logout()
            except Exception:
                pass
            self.imap = None


# ---------------------------------------------------------------------------

def get_backend():
    choice = os.environ.get("MAIL_BACKEND", "applescript").lower()
    filtered = os.environ.get("FILTERED_FOLDER", "Filtered")
    if choice == "applescript":
        return AppleScriptBackend(filtered_folder=filtered)
    if choice == "imap":
        address = os.environ.get("MAIL_EMAIL") or os.environ.get("ICLOUD_EMAIL")
        if not address:
            sys.exit("Set MAIL_EMAIL for the IMAP backend.")
        return IMAPBackend(
            address,
            provider=os.environ.get("MAIL_PROVIDER", "icloud"),
            filtered_folder=filtered,
        )
    sys.exit(f"MAIL_BACKEND must be 'applescript' or 'imap', got '{choice}'.")


if __name__ == "__main__":
    b = get_backend()
    print(f"Backend: {b.name}")
    if b.name == "applescript":
        msgs = b.fetch_recent(count=3)
        print(f"Read {len(msgs)} message(s) from Mail.app with no credentials.")
        for m in msgs:
            print(f"  {m['subject'][:60]}")
    else:
        print(f"Account: {b.address}  password: {mask(b.password)}")
        b._connect()
        print("IMAP login OK")
    b.close()
