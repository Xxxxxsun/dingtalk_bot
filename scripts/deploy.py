#!/usr/bin/env python3
"""Install MyClaw jobs as systemd user timers on a Linux server."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from myclaw.common import TIMEZONE, claude_binary, ClaudeError  # noqa: E402
from myclaw.run_job import load_jobs  # noqa: E402

UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
UNIT_PREFIX = "myclaw-"


def fail(message: str) -> None:
    print(f"✗ {message}", file=sys.stderr)
    raise SystemExit(1)


def check_prerequisites(skip_auth: bool) -> str:
    if sys.version_info < (3, 11):
        fail("需要 Python 3.11+")
    if not shutil.which("systemctl"):
        fail("未找到 systemctl；本安装器面向带 systemd 的 Linux 服务器")
    try:
        claude = claude_binary()
    except ClaudeError:
        fail("未找到 claude CLI。安装：curl -fsSL https://claude.ai/install.sh | bash")
    print(f"✓ claude: {claude}")

    webhook = os.environ.get("DINGTALK_WEBHOOK", "")
    if not webhook or "YOUR_ACCESS_TOKEN" in webhook or "access_token=" not in webhook:
        fail("DINGTALK_WEBHOOK 未填写或仍是示例值：请在 .env 中填入钉钉机器人的 Webhook 地址")
    if os.environ.get("DINGTALK_SECRET", "").startswith("SECxxxx"):
        fail("DINGTALK_SECRET 仍是示例值：请填入加签密钥，或删除该行")
    if not os.environ.get("DINGTALK_SECRET"):
        print("! 未设置 DINGTALK_SECRET：建议在钉钉机器人安全设置中启用“加签”")
    print("✓ 钉钉 webhook 已配置")

    if not skip_auth:
        if not any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")):
            print("! .env 中未设置 ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN，将使用本机 claude 登录态")
        probe = subprocess.run(
            [claude, "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
             "--setting-sources", "",
             "--model", os.environ.get("MYCLAW_CLAUDE_FAST_MODEL", "claude-sonnet-5-5"),
             "回复 ok"],
            capture_output=True, text=True, timeout=120,
        )
        if probe.returncode != 0 or '"is_error":false' not in probe.stdout.replace(" ", ""):
            fail("Claude Code 认证检查失败。服务器上请运行 `claude setup-token`，"
                 "把得到的令牌写入 .env 的 CLAUDE_CODE_OAUTH_TOKEN（或设置 ANTHROPIC_API_KEY）")
        print("✓ Claude Code 认证可用")
    return claude


def render_units(job: dict, claude: str) -> tuple[str, str]:
    name = job["name"]
    path_dirs = [str(Path(claude).parent), str(Path(sys.executable).parent),
                 "/usr/local/bin", "/usr/bin", "/bin"]
    path_value = ":".join(dict.fromkeys(path_dirs))
    timeout = int(job.get("timeoutSeconds", 600)) + 300
    service = f"""[Unit]
Description=MyClaw {name}: {job['description']}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={REPO_ROOT}
Environment=PATH={path_value}
Environment=MYCLAW_CLAUDE_BIN={claude}
ExecStart={sys.executable} -m myclaw.run_job {name}
TimeoutStartSec={timeout}
Nice=5
"""
    timer = f"""[Unit]
Description=MyClaw timer for {name}

[Timer]
OnCalendar={job['onCalendar']} {TIMEZONE.key}
Persistent=false
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
"""
    return service, timer


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], check=check, text=True,
                          capture_output=True)


def installed_units() -> list[Path]:
    return sorted(UNIT_DIR.glob(f"{UNIT_PREFIX}*.timer")) + sorted(
        UNIT_DIR.glob(f"{UNIT_PREFIX}*.service"))


def uninstall() -> int:
    for unit in installed_units():
        if unit.suffix == ".timer":
            systemctl("disable", "--now", unit.name, check=False)
        unit.unlink()
        print(f"removed {unit}")
    systemctl("daemon-reload", check=False)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print units without installing")
    parser.add_argument("--skip-auth-check", action="store_true")
    parser.add_argument("--uninstall", action="store_true", help="remove all myclaw-* units")
    args = parser.parse_args()

    if args.uninstall:
        return uninstall()

    jobs = load_jobs()
    if args.dry_run:
        claude = shutil.which("claude") or "claude"
        for job in jobs.values():
            service, timer = render_units(job, claude)
            print(f"# {UNIT_PREFIX}{job['name']}.service\n{service}")
            print(f"# {UNIT_PREFIX}{job['name']}.timer\n{timer}")
        return 0

    claude = check_prerequisites(args.skip_auth_check)
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        env_file.chmod(0o600)

    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    wanted = set()
    for job in jobs.values():
        service, timer = render_units(job, claude)
        base = f"{UNIT_PREFIX}{job['name']}"
        (UNIT_DIR / f"{base}.service").write_text(service, encoding="utf-8")
        (UNIT_DIR / f"{base}.timer").write_text(timer, encoding="utf-8")
        wanted.update({f"{base}.service", f"{base}.timer"})
        if shutil.which("systemd-analyze"):
            check = subprocess.run(
                ["systemd-analyze", "calendar", f"{job['onCalendar']} {TIMEZONE.key}"],
                capture_output=True, text=True)
            if check.returncode != 0:
                fail(f"{job['name']} 的 onCalendar 无效：{check.stderr.strip()}")

    # Drop units for jobs that were removed from schedules.json.
    for unit in installed_units():
        if unit.name not in wanted:
            if unit.suffix == ".timer":
                systemctl("disable", "--now", unit.name, check=False)
            unit.unlink()
            print(f"removed stale {unit.name}")

    systemctl("daemon-reload")
    for job in jobs.values():
        systemctl("enable", "--now", f"{UNIT_PREFIX}{job['name']}.timer")
        print(f"✓ {job['name']:32s} {job['onCalendar']} {TIMEZONE.key}")

    linger = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"],
                            capture_output=True, text=True)
    if "Linger=yes" not in linger.stdout:
        print("! 当前用户未启用 linger，退出登录后定时任务会停止。请让管理员执行：\n"
              f"  sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")
    print("\n完成。查看计划：systemctl --user list-timers 'myclaw-*'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
