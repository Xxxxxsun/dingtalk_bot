"""Send Markdown to a DingTalk group custom robot (webhook + 加签)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from myclaw import common  # noqa: F401  (loads .env)


# DingTalk rejects oversized markdown; stay well below the documented limit.
MAX_CHUNK_BYTES = 15000
# Custom robots are limited to 20 messages per minute per group.
SEND_INTERVAL_SECONDS = 3.5


class DingTalkError(RuntimeError):
    """The webhook request failed or DingTalk returned a non-zero errcode."""


def signed_url(webhook: str, secret: str, timestamp_ms: int | None = None) -> str:
    if not secret:
        return webhook
    timestamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}\n{secret}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp}&sign={sign}"


def _split_oversized(block: str, limit: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for line in block.splitlines(keepends=True):
        while len(line.encode("utf-8")) > limit:
            if current.strip():
                pieces.append(current.rstrip())
            current = ""
            cut = limit // 4  # a UTF-8 character is at most 4 bytes
            pieces.append(line[:cut].rstrip())
            line = line[cut:]
        if len((current + line).encode("utf-8")) > limit:
            pieces.append(current.rstrip())
            current = ""
        current += line
    if current.strip():
        pieces.append(current.rstrip())
    return [piece for piece in pieces if piece.strip()]


def split_markdown(text: str, limit: int = MAX_CHUNK_BYTES) -> list[str]:
    """Split on ## / ### headings so every message stays readable."""
    text = text.strip()
    if len(text.encode("utf-8")) <= limit:
        return [text]
    blocks = re.split(r"(?m)^(?=#{2,3} )", text)
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block.encode("utf-8")) > limit:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.extend(_split_oversized(block, limit))
            continue
        if len((current + block).encode("utf-8")) > limit:
            chunks.append(current.strip())
            current = ""
        current += block
    if current.strip():
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def message_title(text: str, fallback: str = "MyClaw 推送") -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:60]
    return fallback


def post_markdown(title: str, text: str, webhook: str, secret: str) -> None:
    body = json.dumps(
        {"msgtype": "markdown", "markdown": {"title": title, "text": text}},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        signed_url(webhook, secret),
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise DingTalkError(f"webhook request failed: {exc}") from exc
    if payload.get("errcode") != 0:
        raise DingTalkError(
            f"DingTalk errcode={payload.get('errcode')} errmsg={payload.get('errmsg')}"
        )


def send_report(text: str, *, dry_run: bool = False) -> int:
    """Send a report, split into numbered parts when needed. Returns part count."""
    webhook = os.environ.get("DINGTALK_WEBHOOK", "").strip()
    secret = os.environ.get("DINGTALK_SECRET", "").strip()
    if not dry_run and not webhook:
        raise DingTalkError("DINGTALK_WEBHOOK is not configured")
    chunks = split_markdown(text)
    base_title = message_title(text)
    for index, chunk in enumerate(chunks, start=1):
        if len(chunks) > 1:
            title = f"{base_title}（{index}/{len(chunks)}）"
            if index > 1:
                chunk = f"**（续 {index}/{len(chunks)}）**\n\n{chunk}"
        else:
            title = base_title
        if dry_run:
            print(f"----- [dry-run] part {index}/{len(chunks)}: {title} "
                  f"({len(chunk.encode('utf-8'))} bytes) -----")
            print(chunk)
            continue
        if index > 1:
            time.sleep(SEND_INTERVAL_SECONDS)
        post_markdown(title, chunk, webhook, secret)
    return len(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Markdown from stdin or --text to DingTalk.")
    parser.add_argument("--text", help="message text; defaults to stdin")
    parser.add_argument("--dry-run", action="store_true", help="print parts instead of sending")
    args = parser.parse_args()
    text = args.text if args.text is not None else sys.stdin.read()
    if not text.strip():
        parser.error("empty message")
    try:
        parts = send_report(text, dry_run=args.dry_run)
    except DingTalkError as exc:
        print(f"dingtalk send failed: {exc}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"sent {parts} message(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
