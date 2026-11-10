# MyClaw for DingTalk：组内论文与 AI 新闻自动推送（Claude Code 版）

参考 [Hunter-Wrynn/MyClaw](https://github.com/Hunter-Wrynn/MyClaw) 改写，三处主要变化：

| | 原版 | 本仓库 |
| --- | --- | --- |
| 联网检索 / 摘要 | Codex CLI `codex exec --search` | Claude Code `claude -p`（WebSearch + WebFetch） |
| 调度 | OpenClaw Cron + 网关 | systemd user timer（无常驻进程） |
| 投递 | 飞书机器人 | 钉钉群自定义机器人（Webhook + 加签） |

周报专题按 [陈明帅老师（浙大形式化验证组）](https://fiction-zju.github.io/author/mingshuai-chen/)
的研究方向重新设计，见 `config/topics.json`。

## 推送安排（Asia/Shanghai）

| 时间 | 内容 |
| --- | --- |
| 周一 11:40 | AI for Formal Methods：LLM 规约/不变式合成、神经定理证明、可验证代码生成 |
| 周二 11:40 | 概率程序与定量验证：终止性、期望运行时间、weakest preexpectation、鞅方法 |
| 周三 11:40 | 混成与信息物理系统：障碍证书、可达性分析、安全控制综合、NN 控制器验证 |
| 周四 11:40 | 量子程序与系统：量子程序验证、电路综合/等价性、纠错解码 |
| 周五 11:40 | 程序语言与机械化语义：Coq/Lean/Isabelle、分离逻辑、终止性、抽象解释、eBPF |
| 周六 11:40 | 自动推理与综合：SMT/SAT、CHC 求解、IC3/PDR 模型检验、SyGuS 与反应式综合 |
| 每天 14:00 | Juya AI Daily 新闻概览（纯抓取，不调用模型） |
| 每天 14:05 | Hugging Face Daily Papers Top 10 中文摘要 |

周报统计上一完整自然周（周一至周日），固定六节结构：专题概览、精选论文、精读推荐、
关键词趋势、组内讨论问题、检索说明。每篇论文都要打开原始页面核验，不凑数。
格式不合格时自动返修，最多两次（只改格式，不新增事实），仍不合格就不投递，
草稿留在本地。HF 中文摘要不可用时，退回官方英文摘要节选。

## 部署（Linux 服务器）

需要：Python 3.11+、systemd、Claude Code CLI、能访问 Anthropic API 和钉钉的网络。

```bash
# 1. 安装并认证 Claude Code
curl -fsSL https://claude.ai/install.sh | bash
claude setup-token          # 生成长期令牌（Claude 订阅）；或改用 ANTHROPIC_API_KEY

# 2. 配置
git clone <本仓库> myclaw-dingtalk && cd myclaw-dingtalk
cp .env.example .env && chmod 600 .env
# 填写 DINGTALK_WEBHOOK、DINGTALK_SECRET、CLAUDE_CODE_OAUTH_TOKEN（或 ANTHROPIC_API_KEY）

# 3. 安装定时器
bash install.sh --dry-run   # 只打印将生成的 systemd 单元
bash install.sh             # 检查依赖与认证，写入 ~/.config/systemd/user/myclaw-*.{service,timer}
sudo loginctl enable-linger $USER   # 退出登录后继续运行
```

重复执行 `install.sh` 会更新已有单元，并删除 `schedules.json` 里已经去掉的任务。
`bash install.sh --uninstall` 删除全部任务。

### 钉钉机器人

群设置 → 机器人 → 添加机器人 → **自定义**。安全设置选“加签”，把 `SEC...` 填进
`DINGTALK_SECRET`，Webhook 地址填进 `DINGTALK_WEBHOOK`。自定义机器人限制每分钟 20 条，
所以长周报会按 `##`/`###` 标题切成约 15KB 一段，带“（续 i/n）”标记依次发送。

## 本地测试与补发

```bash
python3 -m myclaw.ai_daily                                   # 只输出到终端
python3 -m myclaw.hf_daily --diagnostics
python3 -m myclaw.hf_daily --force-fallback                  # 不调用 Claude
python3 -m myclaw.weekly --preset monday-ai4fm --print-prompt
python3 -m myclaw.weekly --preset tuesday-probabilistic \
  --start YYYY-MM-DD --end YYYY-MM-DD --diagnostics

python3 -m myclaw.run_job --list
python3 -m myclaw.run_job hf-daily-top10 --dry-run           # 运行任务，但只打印钉钉消息
python3 -m myclaw.run_job ai-daily                           # 注意：会真的发到群里
python3 -m myclaw.run_job weekly-cps-wednesday -- --start 2026-10-05 --end 2026-10-11

systemctl --user list-timers 'myclaw-*'
journalctl --user -u myclaw-weekly-ai4fm-monday -n 100
```

运行状态写在 `~/.local/state/myclaw/`：`last-output/` 存最近一次输出，
`artifacts/` 存校验失败的草稿，`claude-workdir/` 是 Claude 运行用的空目录。

## 自定义专题

编辑 `config/topics.json`。`group_profile` 是写进提示词的课题组介绍；每个 preset 包含
`topic`（报告标题）、`keywords`、`overview_dimensions`、`focus`（筛选标准）和
`group_connections`（精读与讨论问题要联系的组内工作）。新增 preset 后，在
`config/schedules.json` 里加一个任务，再重新执行 `bash install.sh`。

## Claude Code 调用方式与安全边界

每次调用都是无状态的 headless 运行：

```
claude -p --output-format json --model <model> \
  --tools WebSearch,WebFetch --allowedTools WebSearch,WebFetch \
  --strict-mcp-config --setting-sources "" --no-session-persistence --disable-slash-commands
```

- `--tools` 只开放 WebSearch/WebFetch：不能执行 shell，也不能读写文件。HF 摘要和格式返修完全不开放工具。
- 不加载用户/项目设置、hooks、MCP 和 CLAUDE.md，工作目录是一个空目录。
- 带 `--settings '{"skipWebFetchPreflight": true}'`。WebFetch 默认会先请求 claude.ai
  预检每个域名，这个请求在服务器或公司网络里经常被拦，结果是**所有**页面都打不开，
  周报只能得到 0 篇已核验论文。0 篇时直接判定失败，不再做格式返修。
- 提示词明确告诉模型：网页和论文摘要是不可信输入，只当数据看待。
- 周报默认用 `claude-opus-5-5`、effort high；HF 摘要用 `claude-sonnet-5-5`、effort low。
  可在 `.env` 修改。使用 API key 时，可设 `MYCLAW_MAX_BUDGET_USD` 限制单次费用。

## 测试

```bash
python3 -m unittest discover -s tests -v   # 离线：不联网、不调用 Claude、不发消息
```
