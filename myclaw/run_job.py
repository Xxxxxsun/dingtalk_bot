"""Run one scheduled job and deliver its stdout to DingTalk."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from myclaw.common import REPO_ROOT, STATE_DIR
from myclaw.dingtalk import DingTalkError, send_report


SCHEDULES_FILE = Path(
    os.environ.get("MYCLAW_SCHEDULES_FILE", REPO_ROOT / "config" / "schedules.json")
)
LAST_OUTPUT_DIR = STATE_DIR / "last-output"


def load_jobs(path: Path = SCHEDULES_FILE) -> dict[str, dict]:
    jobs = json.loads(path.read_text(encoding="utf-8"))
    return {job["name"]: job for job in jobs}


def run(job: dict, extra_args: list[str]) -> tuple[int, str, str]:
    command = [sys.executable, "-m", job["module"], *job.get("args", []), *extra_args]
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=int(job.get("timeoutSeconds", 600)),
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"job timed out after {job.get('timeoutSeconds')} seconds"
    return completed.returncode, completed.stdout, completed.stderr


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", nargs="?", help="job name from config/schedules.json")
    parser.add_argument("--list", action="store_true", help="list configured jobs")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the job but print the DingTalk messages instead of sending")
    argv = sys.argv[1:]
    # Everything after a bare "--" is forwarded verbatim to the job module.
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    extra = argv[split + 1:]

    jobs = load_jobs()
    if args.list or not args.job:
        for name, job in jobs.items():
            print(f"{name:32s} {job['onCalendar']:24s} {job['description']}")
        return 0
    if args.job not in jobs:
        parser.error(f"unknown job {args.job!r}; use --list")
    job = jobs[args.job]
    code, stdout, stderr = run(job, extra)
    if stderr.strip():
        sys.stderr.write(stderr)
    LAST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    (LAST_OUTPUT_DIR / f"{args.job}.md").write_text(stdout, encoding="utf-8")

    if code != 0 or not stdout.strip():
        print(f"[run_job] {args.job} failed with status {code}; nothing delivered", file=sys.stderr)
        if os.environ.get("MYCLAW_NOTIFY_FAILURES", "0") == "1":
            reason = (stderr.strip().splitlines() or ["empty output"])[-1][:300]
            try:
                send_report(
                    f"#### MyClaw 任务失败：{args.job}\n\n- 状态码：{code}\n- 原因：{reason}",
                    dry_run=args.dry_run,
                )
            except DingTalkError as exc:
                print(f"[run_job] failure notice not sent: {exc}", file=sys.stderr)
        return code or 1

    try:
        parts = send_report(stdout, dry_run=args.dry_run)
    except DingTalkError as exc:
        print(f"[run_job] delivery failed: {exc}", file=sys.stderr)
        return 1
    verb = "rendered (dry-run)" if args.dry_run else "delivered"
    print(f"[run_job] {args.job} {verb} in {parts} message(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
