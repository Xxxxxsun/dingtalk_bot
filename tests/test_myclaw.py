"""Offline tests: no network, no Claude calls, nothing is sent."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from myclaw import ai_daily, common, dingtalk, hf_daily, weekly  # noqa: E402
from myclaw.run_job import load_jobs  # noqa: E402


class EnvTests(unittest.TestCase):
    def test_env_file_is_data_and_filters_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\nDINGTALK_SECRET='abc$(whoami)'\nUNRELATED=1\n"
                "CLAUDE_CODE_OAUTH_TOKEN=\n",
                encoding="utf-8",
            )
            values = common.parse_env_file(path)
        self.assertEqual(values, {"DINGTALK_SECRET": "abc$(whoami)"})


class ClaudeCommandTests(unittest.TestCase):
    def test_command_restricts_tools_and_context(self) -> None:
        command = common.build_claude_command("m", ("WebSearch", "WebFetch"), "high")
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertEqual(command[command.index("--allowedTools") + 1], "WebSearch,WebFetch")
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertEqual(json.loads(command[command.index("--settings") + 1]),
                         {"skipWebFetchPreflight": True})

    def test_no_tools_means_no_allowed_tools(self) -> None:
        command = common.build_claude_command("m", ())
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", command)

    def test_parse_output(self) -> None:
        payload = common.parse_claude_output(
            'noise\n{"type":"result","subtype":"success","is_error":false,"result":"hi"}'
        )
        self.assertEqual(payload["result"], "hi")


class DingTalkTests(unittest.TestCase):
    def test_signature(self) -> None:
        url = dingtalk.signed_url("https://x/robot/send?access_token=t", "SECabc", 1700000000000)
        expected = base64.b64encode(
            hmac.new(b"SECabc", b"1700000000000\nSECabc", hashlib.sha256).digest()
        ).decode()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["timestamp"], ["1700000000000"])
        self.assertEqual(query["sign"], [expected])
        self.assertEqual(query["access_token"], ["t"])

    def test_split_respects_limit_and_headings(self) -> None:
        body = "# 标题\n\n" + "".join(
            f"### 论文 {i}\n\n" + "中文内容。" * 400 + "\n\n" for i in range(12)
        )
        chunks = dingtalk.split_markdown(body, limit=8000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-8")), 8000)
        self.assertTrue(all(c.startswith(("#", "###")) for c in chunks))
        self.assertEqual("".join(chunks).count("### 论文"), 12)

    def test_split_handles_giant_line(self) -> None:
        chunks = dingtalk.split_markdown("## a\n" + "字" * 20000, limit=4000)
        self.assertTrue(all(len(c.encode("utf-8")) <= 4000 for c in chunks))
        self.assertEqual(sum(c.count("字") for c in chunks), 20000)


def synthetic_weekly(preset: weekly.Preset, start: dt.date, end: dt.date, papers: int = 4) -> str:
    lines = [weekly.report_title(preset), "", weekly.report_date_line(start, end), "",
             weekly.HEADINGS[0], ""]
    lines += [f"- **维度{i}**：说明。" for i in range(6)]
    lines += ["", weekly.HEADINGS[1], ""]
    for i in range(papers):
        lines += [f"### Paper {i}", ""]
        lines += [f"- **{field}**：" + "内容" * 40 for field in weekly.PAPER_FIELDS]
        lines.append("")
    lines += [weekly.HEADINGS[2], ""]
    for i in range(1, min(3, papers) + 1):
        lines += [f"### {i}. Paper {i}", "", "段落。" * 30, ""]
    lines += [weekly.HEADINGS[3], ""] + [f"- **kw{i}**：原因。" for i in range(12)]
    lines += ["", weekly.HEADINGS[4], ""] + [f"{i}. 问题？" for i in range(1, 6)]
    lines += ["", weekly.HEADINGS[5], "", "- 统计区间：..."]
    return "\n".join(lines) + "\n"


class WeeklyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile, self.presets = weekly.load_topics()
        self.start, self.end = dt.date(2026, 10, 5), dt.date(2026, 10, 11)

    def test_previous_week(self) -> None:
        self.assertEqual(
            weekly.previous_complete_week(dt.date(2026, 10, 16)),
            (dt.date(2026, 10, 5), dt.date(2026, 10, 11)),
        )

    def test_valid_report_passes(self) -> None:
        for preset in self.presets.values():
            stats = weekly.validate_report(
                synthetic_weekly(preset, self.start, self.end), preset, self.start, self.end)
            self.assertEqual(stats["selected_papers"], 4)

    def test_few_papers_allow_fewer_deep_reads(self) -> None:
        preset = next(iter(self.presets.values()))
        report = synthetic_weekly(preset, self.start, self.end, papers=2)
        self.assertEqual(weekly.validate_report(report, preset, self.start, self.end)["deep_reads"], 2)

    def test_invalid_report_fails(self) -> None:
        preset = next(iter(self.presets.values()))
        report = synthetic_weekly(preset, self.start, self.end).replace("5. 问题？", "")
        with self.assertRaises(weekly.ReportValidationError):
            weekly.validate_report(report, preset, self.start, self.end)

    def test_zero_papers_is_not_repairable(self) -> None:
        preset = next(iter(self.presets.values()))
        report = synthetic_weekly(preset, self.start, self.end, papers=0)
        with self.assertRaises(weekly.ReportValidationError) as ctx:
            weekly.validate_report(report, preset, self.start, self.end)
        self.assertFalse(ctx.exception.repairable)

    def test_preamble_is_stripped(self) -> None:
        preset = next(iter(self.presets.values()))
        report = synthetic_weekly(preset, self.start, self.end)
        self.assertEqual(weekly.strip_preamble("好的，以下是周报：\n\n" + report, preset), report)

    def test_prompt_contains_contract(self) -> None:
        for preset in self.presets.values():
            prompt = weekly.build_prompt(preset, self.profile, self.start, self.end)
            self.assertIn(weekly.report_title(preset), prompt)
            self.assertIn("WebFetch", prompt)
            for heading in weekly.HEADINGS:
                self.assertIn(heading, prompt)


class HfDailyTests(unittest.TestCase):
    def test_fallback_report_validates(self) -> None:
        papers = [
            hf_daily.Paper(i + 1, f"2610.{10000 + i}", f"Title {i}",
                           "We propose a method. Experiments show it improves results.", ("A",), 3)
            for i in range(10)
        ]
        date = dt.date(2026, 10, 16)
        report = hf_daily.fallback_report(papers, date, 25)
        hf_daily.validate_report(report, papers, date, 25)


class AiDailyTests(unittest.TestCase):
    def test_overview_titles_become_links(self) -> None:
        body = (
            "<h2>概览</h2><h3>要闻</h3><ul>"
            '<li>DeepSeek 新增 API <a href="https://example.com/a?x=1&amp;y=2">↗</a> <code>#4</code></li>'
            '<li>无链接条目 <code>#5</code></li>'
            "</ul><hr>"
        )
        markdown = ai_daily.overview_markdown(body)
        self.assertIn("- [DeepSeek 新增 API](https://example.com/a?x=1&y=2) #4", markdown)
        self.assertIn("- 无链接条目 #5", markdown)


class ConfigTests(unittest.TestCase):
    def test_schedules_reference_known_modules_and_presets(self) -> None:
        _, presets = weekly.load_topics()
        jobs = load_jobs()
        self.assertEqual(len(jobs), 8)
        for job in jobs.values():
            self.assertIn(job["module"], {"myclaw.weekly", "myclaw.ai_daily", "myclaw.hf_daily"})
            if job["module"] == "myclaw.weekly":
                self.assertIn(job["args"][job["args"].index("--preset") + 1], presets)

    def test_units_render(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import deploy  # noqa: PLC0415
        job = load_jobs()["ai-daily"]
        service, timer = deploy.render_units(job, "/opt/claude/claude")
        self.assertIn("ExecStart=", service)
        self.assertIn("-m myclaw.run_job ai-daily", service)
        self.assertIn("OnCalendar=*-*-* 14:00:00 Asia/Shanghai", timer)

    def test_example_env_has_no_real_secrets(self) -> None:
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        values = common.parse_env_file(REPO_ROOT / ".env.example")
        self.assertIn("YOUR_ACCESS_TOKEN", values["DINGTALK_WEBHOOK"])
        self.assertNotIn("sk-ant-", text)
        json.loads((REPO_ROOT / "config" / "topics.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
