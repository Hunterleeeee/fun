<div align="center">

# Fun

### Coding should feel good.

一个安全优先、可恢复、Runtime-first 的终端 Coding Agent。

[![CI](https://github.com/Hunterleeeee/fun/actions/workflows/ci.yml/badge.svg)](https://github.com/Hunterleeeee/fun/actions/workflows/ci.yml)
[![Alpha](https://img.shields.io/badge/status-alpha-f5b642.svg)](https://github.com/Hunterleeeee/fun/releases/tag/v1.0.0a6)
[![Release](https://img.shields.io/github/v/release/Hunterleeeee/fun?include_prereleases&label=latest%20alpha)](https://github.com/Hunterleeeee/fun/releases)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-2ea44f.svg)](LICENSE)

**让写代码变得有意思。**

</div>

## 为什么是 Fun？

大多数 Coding Agent 让模型直接“做事”。Fun 选择把模型和执行权分开：

> **模型提出下一步，Runtime 决定什么是真实、什么安全、什么可以执行。**

你得到的不是一段神秘的自动化，而是一条可观察、可暂停、可恢复、可 Replay 的工程流程：

```text
目标 → Plan → Explore / Read → Edit / Exec → Validation → Diff → 完成
             ↑                         ↓
             └────── Event Replay ─────┘
```

```text
$ fun "fix the failing login test"

◇ PLAN
  ○ inspect workspace
  ○ locate relevant code
  ○ apply a minimal change
  ○ run focused validation

◌ read  src/auth/login.py
? Allow edit (medium)? [y/N] y
✓ tool.completed
✓ validation.completed

Coding should feel good.
```

## 现在就开始

### 安装

当前公开 Alpha：**v1.0.0a6**。推荐从 [GitHub Releases](https://github.com/Hunterleeeee/fun/releases/tag/v1.0.0a6) 下载 wheel / sdist，并使用 `SHA256SUMS` 校验；也可以直接安装源码：

```bash
curl -fsSL https://raw.githubusercontent.com/Hunterleeeee/fun/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
fun --version
```

或从源码安装：

```bash
git clone https://github.com/Hunterleeeee/fun.git
cd fun
python3 -m pip install -e .
fun --version
```

要求 Python 3.11+。Fun 核心运行时零第三方运行时依赖。

### 配置模型

Fun 支持 OpenAI-compatible Chat Completions streaming provider：

```bash
fun --configure
```

或使用环境变量：

```bash
export FUN_API_URL="https://api.openai.com/v1"
export FUN_API_KEY="your-api-key"
export FUN_MODEL="your-model"
fun "inspect this project and fix the smallest safe issue"
```

### Approval 模式

```bash
fun --approval ask "review and fix the failing test"    # 每个风险操作询问
fun --approval smart "clean up this module"             # 中高风险操作询问
fun --approval auto "run the focused checks"             # 自动执行策略允许的操作
```

## Alpha 已具备的能力

### 可恢复的 Runtime，而不是黑盒 Agent

- SQLite Event Store：事实先持久化，再更新内存状态
- **跨进程恢复后可继续执行**：事件 ID 使用 UUID，序列号从已恢复事件之后继续，避免重启后新事件被静默吞掉
- Durable Store 对相同事件保持幂等，对内容冲突显式失败并回滚
- Event Replay：重启后恢复 Task、Plan、Tool、Validation、Repair 和 Agent 节点
- Checkpoint、Pause、Resume、Stop、Recovery required
- 不确定的副作用不会自动重跑
- 原子批量事件和 workspace lock 恢复

### 真正的 Agent 状态节点

```text
model.requested → response.parsed → tools.executing
       ↓                 ↓                 ↓
  model.failed     response.failed      tool.completed
                                            ↓
                                      validation.started
                                            ↓
                                          ready
```

每个关键阶段都有安全事件、可 Replay 状态和明确终结路径。旧事件、乱序事件和重复失败不会污染新的 Tool Call。

### 安全执行边界

- 工作区 cwd 和路径边界保护
- `shell=False`、`shlex.split`
- 阻断危险命令和 wrapper 绕过
- 子进程组超时终止
- 输出上限和截断标记
- Approval boundary
- 不把 Prompt、代码、命令、Tool 参数或 API Key 上传到 Telemetry

### Dynamic Plan + bounded Repair

- Runtime 控制 Plan 和 PlanStep
- 模型可以提出结构化计划，但非法计划会被拒绝
- PlanStep 具备 `pending / active / done / blocked`
- Validation 结果可 Replay
- Repair 次数有上限，重启后不会绕过预算

### Provider-ready streaming

- OpenAI-compatible SSE streaming
- 跨 chunk、CRLF、多行 `data:`、`[DONE]` 兼容
- Provider timeout / network / auth / HTTP / malformed event 分类
- 非 SSE 响应和错误状态不会解析或持久化响应正文
- Endpoint、API key、model、payload、timeout 配置边界校验

## 常用命令

```text
/goal         查看当前目标
/goal <text>  设置目标
/status       查看 task、agent、usage、recovery
/plan         查看 PlanStep
/diff         查看当前 diff
/usage        查看 token / TTFT
/pause        暂停
/resume       继续
/checkpoint   创建 checkpoint
/recover      恢复 pending 状态
/recover stop 停止 pending 状态
/stop         停止任务
/quit         退出
```

本地调试 Dashboard：

```bash
fun --dashboard
# http://127.0.0.1:8765
```

Dashboard 只读取本地 `~/.fun/events.db`，只监听 `127.0.0.1`，不是全体用户数据看板。Telemetry 默认关闭；启用时也只发送匿名、粗粒度、聚合数据，不发送 Prompt、代码、路径、命令、Tool 参数、API Key 或完整模型响应。

## 架构原则

Fun **不依赖 LangChain 或 LangGraph**。核心是轻量的 Fun Runtime：

```text
Model proposal
      ↓
Runtime policy + durable events
      ↓
Tool execution + checkpoints + recovery
      ↓
CLI projections + validation + diff
```

模型可以犯错；Runtime 必须知道发生了什么。所有重要副作用都必须有事实、策略边界和可恢复路径。

## Alpha 边界

Fun 目前是公开 Alpha，不是 feature-complete 1.0。暂不承诺：

- Anthropic 原生协议适配
- Web Search
- Inject / Queue
- 自动上下文 Compaction
- 跨会话 Memory
- 多 Agent 协作

请先在测试仓库或可恢复的工作区使用，不要对未经审阅的破坏性自动化授予无限权限。

## 开发

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q fun tests
python3 -m pip install -e .
```

每次提交都会通过 GitHub Actions 的 Python 3.11 / 3.12 测试矩阵和 package install smoke test。package smoke 会校验 artifact checksum，并分别验证 wheel 及 sdist 构建/安装。发布由版本 tag 触发，并附带 wheel、sdist 和 SHA256 checksums；当前最新 Alpha 为 `v1.0.0a6`。`main` 上的后续修复会在下一次 Alpha tag 发布后进入可下载 artifact，Release 包不会假装包含未发布提交。当前 `v1.0.0a6` 包含最新的 Runtime 生命周期与恢复可靠性修复。

详细契约和设计：

- [`docs/README.md`](docs/README.md)
- [`docs/alpha-release-checklist.md`](docs/alpha-release-checklist.md) — Alpha tag、CI 和 artifact 验收清单
- [`docs/fun-v1-contract.md`](docs/fun-v1-contract.md)
- [`docs/fun-runtime-spec.md`](docs/fun-runtime-spec.md)
- [`SECURITY.md`](SECURITY.md)
- [`CONTRIBUTING.md`](CONTRIBUTING.md)
- [`CHANGELOG.md`](CHANGELOG.md)

## License

[MIT](LICENSE)
