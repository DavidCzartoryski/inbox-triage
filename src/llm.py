#!/usr/bin/env python3
"""
Ask the model for JSON, with whichever credential you have.

Two routes, tried in order:

  api          — the Anthropic API, when an API key is set (environment or
                 Keychain). Billed per token.
  claude-code  — the `claude` CLI in headless mode, when it's installed and
                 logged in. Runs on your Claude subscription, so a student
                 with Claude Code and no API credit can still use the agent.

The CLI route runs with --safe-mode and no tools: it's a plain model call,
with none of your CLAUDE.md, plugins, hooks or MCP servers loaded and nothing
it can execute. Both routes validate the reply against a JSON schema.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import keystore


# Session plumbing from a parent Claude Code process. Inherited, these make a
# headless run behave as a child of whatever launched it, so they're dropped.
PARENT_SESSION_ENV = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH")


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_ENV}


class LLMError(RuntimeError):
    """The model couldn't be reached, refused, or returned something unusable."""


def claude_bin():
    """Path to the Claude Code CLI, or None.

    launchd hands jobs a minimal PATH, so look in the usual install
    locations as well as on PATH.
    """
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        return explicit if Path(explicit).exists() else None
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "claude",
                      Path.home() / ".claude" / "local" / "claude",
                      Path("/opt/homebrew/bin/claude"),
                      Path("/usr/local/bin/claude")):
        if candidate.exists():
            return str(candidate)
    return None


def backend():
    """'api', 'claude-code', or None when there's no way to reach a model.

    LLM_BACKEND forces one; otherwise an API key wins because it's what the
    project has always used, and the CLI is the fallback.
    """
    forced = os.environ.get("LLM_BACKEND", "").lower()
    if forced in ("api", "claude-code"):
        return forced
    if keystore.api_key():
        return "api"
    if claude_bin():
        return "claude-code"
    return None


def ask_json(system, user, schema, model=None, effort=None,
             max_tokens=16000, timeout=600):
    """One model call whose reply must match `schema`. Returns the parsed JSON.

    `schema` must describe an object; wrap arrays in a property.
    Raises LLMError on any failure, so callers can fail safe.
    """
    route = backend()
    if route == "api":
        return _ask_api(system, user, schema, model, effort, max_tokens)
    if route == "claude-code":
        return _ask_cli(system, user, schema, model, effort, timeout)
    raise LLMError("No Anthropic API key and no Claude Code CLI found. Store a "
                   "key with `src/keystore.py set anthropic-api-key`, or "
                   "install Claude Code and log in.")


def _ask_api(system, user, schema, model, effort, max_tokens):
    import anthropic

    client = anthropic.Anthropic(api_key=keystore.api_key())
    output_config = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        output_config["effort"] = effort
    try:
        resp = client.beta.messages.create(
            model=model or "claude-opus-5-5",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
            # On a policy decline the API re-runs the request on Anthropic's
            # recommended fallback model instead of returning a refusal.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise LLMError("Anthropic API rejected the key (401)") from exc
    except anthropic.RateLimitError as exc:
        raise LLMError("rate limited by the Anthropic API") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"Anthropic API error {exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(f"couldn't reach the Anthropic API: {exc}") from exc

    if resp.stop_reason == "refusal":
        raise LLMError("the model declined this request")
    if resp.stop_reason == "max_tokens":
        raise LLMError("the reply was cut off at max_tokens")
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"unparseable reply: {exc}") from exc


def _ask_cli(system, user, schema, model, effort, timeout):
    exe = claude_bin()
    if not exe:
        raise LLMError("Claude Code CLI not found (set CLAUDE_BIN)")
    cmd = [exe, "-p", "--safe-mode", "--tools", "", "--no-session-persistence",
           "--output-format", "json", "--system-prompt", system,
           "--json-schema", json.dumps(schema)]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    try:
        # The prompt goes over stdin: it can hold a resume and hundreds of
        # postings, and stdin keeps it out of `ps` output.
        out = subprocess.run(cmd, input=user, capture_output=True, text=True,
                             timeout=timeout, cwd=str(Path.home()),
                             env=clean_env())
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"claude CLI timed out after {timeout}s") from exc
    except OSError as exc:
        raise LLMError(f"couldn't run the claude CLI: {exc}") from exc
    try:
        envelope = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        detail = (out.stderr or out.stdout).strip()[:300]
        raise LLMError(f"claude CLI failed (exit {out.returncode}): "
                       f"{detail}") from exc
    if envelope.get("is_error"):
        raise LLMError(f"claude CLI error: {str(envelope.get('result'))[:300]}")
    data = envelope.get("structured_output")
    if data is None:
        try:
            data = json.loads(envelope.get("result") or "")
        except json.JSONDecodeError as exc:
            raise LLMError("claude CLI returned no structured output") from exc
    return data


if __name__ == "__main__":
    route = backend()
    print(f"LLM backend: {route or 'none'}")
    if route == "claude-code":
        print(f"  claude CLI: {claude_bin()}")
