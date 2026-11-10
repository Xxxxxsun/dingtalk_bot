"""Shared configuration loading and the Claude Code headless runner."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_KEYS = {
    "DINGTALK_WEBHOOK",
    "DINGTALK_SECRET",
    "MYCLAW_CLAUDE_MODEL",
    "MYCLAW_CLAUDE_FAST_MODEL",
    "MYCLAW_CLAUDE_BIN",
    "MYCLAW_MAX_BUDGET_USD",
    "MYCLAW_TIMEZONE",
    "MYCLAW_HF_ENDPOINT",
    "MYCLAW_FETCH_PROXY",
    "MYCLAW_STATE_DIR",
    "MYCLAW_NOTIFY_FAILURES",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
}


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines as data; nothing is evaluated by a shell."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in ENV_KEYS and value:
            values[key] = value
    return values


def load_env() -> None:
    """Load the repo .env; real environment variables take precedence."""
    env_path = Path(os.environ.get("MYCLAW_ENV_FILE", REPO_ROOT / ".env"))
    for key, value in parse_env_file(env_path).items():
        os.environ.setdefault(key, value)


load_env()

TIMEZONE = ZoneInfo(os.environ.get("MYCLAW_TIMEZONE", "Asia/Shanghai"))
DEFAULT_MODEL = os.environ.get("MYCLAW_CLAUDE_MODEL", "claude-opus-5-5")
FAST_MODEL = os.environ.get("MYCLAW_CLAUDE_FAST_MODEL", "claude-sonnet-5-5")
STATE_DIR = Path(
    os.environ.get("MYCLAW_STATE_DIR", Path.home() / ".local" / "state" / "myclaw")
)
# Claude runs from an empty directory so no project CLAUDE.md or files leak in.
CLAUDE_WORKDIR = STATE_DIR / "claude-workdir"


def fetch_opener() -> urllib.request.OpenerDirector:
    """Opener for source fetching (Juya / HF) only; never used for Claude or DingTalk."""
    proxy = os.environ.get("MYCLAW_FETCH_PROXY", "").strip()
    if not proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )


class ClaudeError(RuntimeError):
    """Claude Code failed, timed out, or returned no result."""


@dataclass(frozen=True)
class ClaudeResult:
    text: str
    cost_usd: float
    num_turns: int
    duration: float
    web_searches: int
    web_fetches: int


def claude_binary() -> str:
    candidate = os.environ.get("MYCLAW_CLAUDE_BIN") or shutil.which("claude")
    fallback = Path.home() / ".local" / "bin" / "claude"
    if not candidate and os.access(fallback, os.X_OK):
        candidate = str(fallback)
    if not candidate:
        raise ClaudeError("claude CLI was not found in PATH")
    return candidate


def build_claude_command(
    model: str,
    tools: tuple[str, ...],
    effort: str | None = None,
) -> list[str]:
    tool_list = ",".join(tools)
    command = [
        claude_binary(),
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        # --tools limits what exists; --allowedTools pre-approves it headlessly.
        "--tools",
        tool_list,
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--disable-slash-commands",
    ]
    if tools:
        command.extend(["--allowedTools", tool_list])
    if "WebFetch" in tools:
        # WebFetch otherwise asks claude.ai to vet each domain first; that call is
        # often blocked on servers / corporate networks and makes every fetch fail.
        command.extend(["--settings", json.dumps({"skipWebFetchPreflight": True})])
    if effort:
        command.extend(["--effort", effort])
    budget = os.environ.get("MYCLAW_MAX_BUDGET_USD", "").strip()
    if budget:
        command.extend(["--max-budget-usd", budget])
    return command


def parse_claude_output(stdout: str) -> dict:
    stdout = stdout.strip()
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # Tolerate stray lines before the final JSON object.
        last = stdout.splitlines()[-1] if stdout else ""
        try:
            payload = json.loads(last)
        except json.JSONDecodeError as exc:
            raise ClaudeError(f"claude returned non-JSON output: {stdout[-500:]}") from exc
    if isinstance(payload, list):
        results = [e for e in payload if isinstance(e, dict) and e.get("type") == "result"]
        payload = results[-1] if results else {}
    if not isinstance(payload, dict):
        raise ClaudeError("claude returned an unexpected JSON payload")
    return payload


def run_claude(
    prompt: str,
    *,
    model: str,
    timeout_seconds: int,
    tools: tuple[str, ...] = (),
    effort: str | None = None,
    diagnostics: bool = False,
    label: str = "myclaw",
) -> ClaudeResult:
    command = build_claude_command(model, tools, effort)
    CLAUDE_WORKDIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=CLAUDE_WORKDIR,
        start_new_session=True,
    )
    deadline = started + timeout_seconds
    supplied_input = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            raise ClaudeError(f"claude timed out after {timeout_seconds} seconds")
        try:
            stdout, stderr = process.communicate(
                input=None if supplied_input else prompt,
                timeout=min(60, remaining),
            )
            break
        except subprocess.TimeoutExpired:
            supplied_input = True
            if diagnostics:
                elapsed = int(time.monotonic() - started)
                print(f"[{label}] claude still running: {elapsed}s", file=sys.stderr, flush=True)

    duration = time.monotonic() - started
    if process.returncode != 0 and not stdout.strip():
        tail = stderr.strip()[-2000:]
        raise ClaudeError(f"claude exited with status {process.returncode}: {tail}")

    payload = parse_claude_output(stdout)
    if payload.get("is_error") or payload.get("subtype") != "success":
        detail = payload.get("result") or payload.get("subtype") or stderr.strip()[-1000:]
        raise ClaudeError(f"claude reported an error: {detail}")
    text = payload.get("result")
    if not isinstance(text, str) or not text.strip():
        raise ClaudeError("claude completed without a final result")

    server_tools = (payload.get("usage") or {}).get("server_tool_use") or {}
    return ClaudeResult(
        text=text.strip(),
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        num_turns=int(payload.get("num_turns") or 0),
        duration=duration,
        web_searches=int(server_tools.get("web_search_requests") or 0),
        web_fetches=int(server_tools.get("web_fetch_requests") or 0),
    )
