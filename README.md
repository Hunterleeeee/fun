<div align="center">

# Fun

**Coding should feel good.**

安全优先、可恢复的终端 Coding Agent。

[![Alpha](https://img.shields.io/badge/status-alpha-f5b642.svg)](https://github.com/Hunterleeeee/fun)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-2ea44f.svg)](LICENSE)

</div>

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Hunterleeeee/fun/main/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
fun
```

或者从源码安装：

```bash
git clone https://github.com/Hunterleeeee/fun.git
cd fun
python3 -m pip install -e .
fun
```

查看本机使用总览（仅监听 `127.0.0.1`）：

```bash
fun --dashboard
# open http://127.0.0.1:8765
```

Dashboard 读取本地 `~/.fun/events.db`，展示 Sessions（当前作为用户近似）、Tasks、token、Tool 调用和最近活动，不上传数据。

配置 OpenAI-compatible Provider：

```bash
fun --configure
```

也可以使用：

```bash
export FUN_API_URL="https://api.openai.com/v1"
export FUN_API_KEY="your-api-key"
export FUN_MODEL="your-model"
```

## What is Fun?

Fun 让模型负责提出下一步动作，让 Runtime 负责事实、安全和执行：

```text
目标 → Plan → Explore / Read / Edit / Exec → Validation → Diff
```

```text
> fix the failing login test

◇ PLAN
  ○ inspect workspace
  ○ locate relevant code
  ○ apply a minimal change
  ○ run focused validation

◌ read
✓ tool.completed
? Allow edit (medium)? [y/N]
✓ validation.completed
```

## Current Alpha

- OpenAI-compatible streaming Agent Loop
- `explore` / `read` / `edit` / `exec`
- Ask / Smart / Auto approval
- Workspace boundary and Safe Exec limits (no shell execution)
- SQLite Event Store and Runtime Replay
- Checkpoint / restore foundations
- Pause / resume / stop
- Dynamic PlanStep evidence
- Bounded validation / repair
- Single-column terminal UI
- One active task per workspace

## Commands

```text
/goal         查看当前目标
/goal <text>  设置目标（当前任务结束后）
/status       task、agent、usage、recovery 状态
/plan         查看 PlanStep
/diff         查看当前 diff
/usage        查看 token / TTFT
fun --dashboard  启动本地 HTML 总览台
/pause        暂停
/resume       继续
/checkpoint   创建 checkpoint
/recover      恢复 pending 状态
/recover stop 停止 pending 状态
/stop         停止任务
/quit         退出
```

## Status

Fun 目前是 **Alpha**，还不是 feature-complete 1.0。

暂未承诺：Anthropic 原生支持、Web Search、Inject、Queue、自动 Compaction、跨会话 Memory、多 Agent。

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q fun tests
```

详细契约和设计见 [`docs/README.md`](docs/README.md)。

## Links

- [V1 Contract](docs/fun-v1-contract.md)
- [Runtime Spec](docs/fun-runtime-spec.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE)
