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
╭─ fun ───────────────────────────────────────────── ses_059f9c58 ╮
│   ● 你                                                          │
│   │  修复登录测试                                               │
│   │                                                             │
│   ✓ read  path=src/auth/login.py                          124ms │
│   │  def login(user):                                           │
│   │      token = cache.get(user.id)                             │
│   │                                                             │
│   ⚠ exec  command=pytest -q tests/test_login.py                 │
│   │  需要授权   medium                                          │
│   │  ● 允许一次  y                                              │
│   │  ○ 本会话始终允许  a                                        │
│   │  ○ 拒绝  n                                                  │
│   │                                                             │
│   ◇ Plan                                                    2/4 │
│   │  ✓ inspect workspace                                        │
│   │  ● apply a minimal change                                   │
│                                                                 │
│    Build     Plan     Review                                    │
│   ▌                                                             │
│   ▌  描述你想做的事，/ 命令，@ 文件                             │
│   ▌  Build · gpt-4o · smart · in 1.2k · out 340 · ttft 210ms    │
│   ▌                                                             │
│   Enter send · Ctrl-N newline · / commands · Ctrl-C exit        │
╰─ ~/work/fun ─────────────────────── Build · smart · v1.0.0a6 ───╯
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

要求 Python 3.11+。**Fun 零第三方依赖**——运行时、终端 UI、SSE 解析、事件存储全部只用标准库。

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

`FUN_API_KEY` 只在当前进程中使用，`fun --configure` 不会把环境变量里的共享/CI 凭据偷偷写进系统 Keychain。手工输入的 key 才会尝试进入 macOS Keychain；Keychain 锁定或拒绝访问时，CLI 和 Dashboard 都会明确显示凭据当前不可用，而不是误报 provider ready。切换主题、模型、权限、语言或 prompt 不会重复写 key，也不会丢失已有的 Keychain 记录。

### Approval 模式

```bash
fun --approval ask "review and fix the failing test"    # 每个风险操作询问
fun --approval smart "clean up this module"             # 中高风险操作询问
fun --approval auto "run the focused checks"            # 自动执行策略允许的操作
```

## 终端 UI

Fun 的界面**零第三方依赖**——布局、颜色、Markdown、语法高亮、模糊补全、光标编辑全部只用标准库。

### 全屏（默认）

```bash
fun
```

进入 alternate screen 接管终端，画布铺满主题底色，退出后终端完全恢复原样。重绘是**逐行 diff**：空闲时刷新 spinner 只改写一行，而不是重画整屏。

代价要知道：alternate screen 没有滚动缓冲区，历史用 PgUp / PgDn 在应用内翻页，**不能用鼠标选中复制**。

### 流式

```bash
fun --stream
```

历史按普通程序输出写进终端滚动缓冲区，只有底部 dock 原地重绘。想用终端自己的滚动条、想选中复制、想 pipe 到文件时用它。

### 事件脊柱

会话视图画成一条竖脊，每个事件一个状态节点：

```text
● 你
│  修复登录测试
│
✓ read  path=src/auth/login.py                              124ms
│  def login(user):
│      token = cache.get(user.id)
│
⚠ exec  command=pytest -q
│  需要授权   medium
│  ● 允许一次  y
│  ○ 本会话始终允许  a
│  ○ 拒绝  n
│
● Fun
│  修复结果
│
◇ Plan                                                        2/4
│  ✓ inspect workspace
│  ● apply a minimal change
```

只扫左边一列就知道**发生了什么、什么顺序、结论如何**；细节挂在右边，`Ctrl+O` 折叠。这与 Fun 的内核一致：Runtime 才是权威，一切都是可重放的持久事件。

### 输入

完整的光标状态机，不是"只能末尾追加"：

```text
← →            移动光标            Ctrl+A / Ctrl+E   行首 / 行尾
Alt+F / Alt+B  按词跳转            Ctrl+W / Alt+D    删前词 / 删后词
Ctrl+K / Ctrl+U 删到行尾 / 行首     Ctrl+Y           粘回
↑ ↓            多行内移动光标；到边界才翻历史
Ctrl+N         换行                Enter            发送
```

位置按**显示列**计算，光标停在汉字上不会错位。终端真光标会被定位到输入处，所以 macOS 中文输入法的候选框跟在你打字的地方。

### 补全

- `/` 浮出内联命令菜单；`Ctrl+P` 打开分组命令面板（搜索框、分区、右对齐快捷键、整行高亮）
- 每轮回复下方带一行回执：模式 · 模型 · 输出 token · 耗时
- `@` 模糊搜索工作区文件并插入引用（fzf 风格：连续命中、路径段开头、开头锚定各有加分）
- `↑↓` 选择，`Tab` / `Enter` 采用，`Esc` 取消；收窄候选时高亮停在原项上

补全按**光标位置**判定，在句子中间打 `@src` 补的是那个 token，不会吃掉整行。

### 模式

`Tab` 在 Build / Plan / Review 之间切换。这是**真实的能力边界**，不是标签：Plan 和 Review 在 Policy 层禁用 `edit` 和 `exec`，Runtime 拦截并发出 `MODE_FORBIDS_TOOL` 事件。

### 渲染

- 模型回答按 Markdown 渲染：标题、列表、引用、行内码、带高亮的代码块
- 语法高亮支持 Python / JS / TS / Go / Rust / Bash / JSON / YAML / TOML / SQL；`read` 按文件后缀着色，`edit` 按 diff 着色
- 四套主题：`sky`（默认）、`dawn`（浅色终端）、`ember`（暖色）、`mono`（灰阶）。`fun --theme ember` 或 `/theme` 切换
- 颜色按能力降级：truecolor → 256 → 16 → 无色。遵循 `NO_COLOR` 与 `FORCE_COLOR`，`--no-color` 可强制关闭
- **东亚字宽按列计算**，中文、全角标点、组合字符都不会撑破边框；ANSI 转义不计入宽度
- 非 UTF-8 环境自动降级为 ASCII 字形

管道、`TERM=dumb`、Windows 控制台会回落到纯文本前端，命令行为完全一致。Windows 的重定向 pipe / console 输入走独立等待路径，不依赖仅支持 socket 的 `select()`；工作区文件列表统一使用 `/`，因此模型上下文和输出在各平台一致。

## Alpha 已具备的能力

### 可恢复的 Runtime，而不是黑盒 Agent

- SQLite Event Store：事实先持久化，再更新内存状态
- Runtime 支持上下文管理器，`with Runtime(...)` 退出时自动关闭 durable store
- **跨进程恢复后可继续执行**：事件 ID 使用 UUID，序列号从已恢复事件之后继续，避免重启后新事件被静默吞掉
- **事件写入是线程安全的**：模型 worker、后台 sub-agent 和 UI 线程可以并发 emit，重复检查与写入在同一把锁内完成
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

- 工作区 cwd 和路径边界保护，符号链接不能越界
- 受保护路径由 Policy 声明（默认覆盖 `.env*`、`.git`、`*.pem`、`*.key`、SSH 私钥、`.aws`、`.npmrc`、`.netrc` 等），调用方可以扩展而不必改 guard；**匹配不区分大小写**，因为 macOS 卷默认也不区分
- `shell=False`、`shlex.split`
- **命令先解析，后判定**：逐层剥掉 `env` / `command` / `nohup` / `nice` 这类透明包装器，每剥一层检查一层；拒绝的是**整个程序**而不是参数形状——任何写法的 shell（`bash -c`、`bash -lc`、`env bash -c` 一视同仁）、以及 `xargs` / `timeout` 这类需要猜测语法才能定位真实命令的程序。遇到本代码没有建模的包装器，选择拒绝而不是猜
- **只判定两件能判定的事，其余承认不知道**：
  - 只读取和汇报的命令（`ls` / `cat` / `grep` / `wc`…）——一份短的、每条都能手工核对的清单，路径参数仍然走越界检查；清单匹配的是解析到工作区外的真实 executable，不是 basename，因此 `./cat`、`env ./cat`、`/tmp/echo` 或 PATH 前置的同名程序都不会冒充免审批命令
  - 可枚举的不可逆操作（递归删除、`git reset --hard` / `clean` / `push -f`、提权、联网取回执行、`find -exec`、参数越界）
  - **其余一律算"陌生程序"**：在**任何**审批模式下都会问一次，批准后可以按程序名记住本会话；不可逆的那类每次都问，永远不记忆
  - `git` 故意不在免审批清单里——别名和 hook 让它本质上是个启动器。宣布它安全就是在重犯"清单没有判据"的错
- `python -c` / `-m` 直接拒绝，而不是扫描代码字符串里的关键词
- 参数解析后落在工作区之外，会抬升到 approval 门槛
- 子进程组超时终止
- 输出上限和截断标记
- Approval boundary，`a`（本会话始终允许）真正被记住；public `Tools.exec()` 没有可伪造的 `approved=True` 开关，Runtime 审批绑定同一个已分类 `CommandPlan`
- 不把 Prompt、代码、命令、Tool 参数或 API Key 上传到 Telemetry

> `exec` 是**受监督的能力，不是沙箱**。它只能对 argv 做判断；程序内部自己拼出来的路径它看不见。

### Dynamic Plan + bounded Repair

- Runtime 控制 Plan 和 PlanStep
- 模型可以提出结构化计划，但非法计划会被拒绝
- PlanStep 具备 `pending / active / done / blocked`
- Validation 结果可 Replay
- Repair 次数有上限，重启后不会绕过预算

### 有界上下文

Runtime 在每次请求前会把消息裁剪到长度预算内：始终保留 system prompt 和最新一轮，超长单条消息会被截断并标记，被裁剪的轮次通过 `context.compacted` 事件记录下来。

这是**确定性的长度裁剪**，不是基于模型的摘要压缩；后者仍在 V1.x 范围。

### Provider-ready streaming

- OpenAI-compatible SSE streaming
- 跨 chunk、CRLF、多行 `data:`、`[DONE]` 兼容
- Provider timeout / network / auth / HTTP / malformed event 分类
- 非 SSE 响应和错误状态不会解析或持久化响应正文
- Endpoint、API key、model、payload、timeout 配置边界校验

资源安全使用示例：

```python
from fun.runtime import Runtime

with Runtime(".", state_dir=".fun") as runtime:
    runtime.create_task("run a focused validation")
    # stop / complete / fail 也会自动释放 durable store
```

## 命令

所有斜杠命令注册在同一张表里，每个前端行为完全一致。输入 `/` 浮出内联补全，支持唯一前缀（`/mod` → `/model`）；`Ctrl+P` 打开同一份注册表的分组面板，输入即筛选——命中命令名时不再混入描述匹配，需要参数的命令会被填进输入框而不是直接执行。

```text
/cancel <id>   取消后台任务
/checkpoint    创建工作区 checkpoint
/clear         清空当前 transcript
/config        配置 Provider、凭据和模型（别名 /setup）
/diff          查看工作区 diff
/exit          退出（别名 /quit）
/goal [text]   查看或设置当前目标
/help          查看命令列表
/logout        删除已保存凭据并进入离线模式
/mode [name]   切换 Build / Plan / Review
/model [id]    切换模型
/pause         暂停当前任务
/permissions   修改审批模式
/plan          查看 PlanStep
/prompt [text] 查看或设置自定义 system prompt 偏好（默认安全规则始终保留）
/recover <a>   处理 pending 恢复：resume / discard / mark_failed / stop
/resume        继续已暂停的任务
/status        查看 task、agent、usage、recovery、后台任务和最近 timing
/stop          停止当前任务
/theme [name]  切换主题：sky / dawn / ember / mono
/usage         查看 token / TTFT
```

按键：

```text
Tab      切换 Build / Plan / Review        Ctrl+P   命令面板
Ctrl+T   显示/隐藏右侧信息栏
/agent   让只读子 Agent 在后台回答一个问题
Ctrl+O   折叠 / 展开工具输出                Ctrl+C   清空草稿 → 中断任务 → 再按退出
PgUp/PgDn 翻阅历史                         Ctrl+D   空缓冲区时退出
```

重启后恢复持久化会话：

```bash
fun --resume-session <session-id>
```

本地调试 Dashboard：

```bash
fun --dashboard
# http://127.0.0.1:8765
```

Dashboard 只读取本地 `~/.fun/events.db`，只监听 `127.0.0.1`，不是全体用户数据看板。Telemetry 默认关闭；启用时也只发送匿名、粗粒度、聚合数据，不发送 Prompt、代码、路径、命令、Tool 参数、API Key 或完整模型响应。

## 架构

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

模块分层：

```text
cli.py          参数解析与装配
commands.py     统一斜杠命令注册表（每个前端共用）
frontends.py    交互式 / 纯文本前端 + goal runner
runtime.py      Task 状态机、Agent 节点、Plan、Validation、Repair
events.py       事件事实与内存投影（线程安全）
persistence.py  SQLite durable store
policy.py       风险分级、审批边界、工作区保护、模式能力边界
tools.py        explore / read / edit / exec
provider.py     OpenAI-compatible SSE 适配
ui/
  text.py       列宽感知的字符串原语（东亚字宽、ANSI 安全）
  theme.py      颜色能力探测、四套主题、渐变
  wordmark.py   启动页的像素字标
  layout.py     事件脊柱、画布外框、居中合成器
  components.py 可复用块：工具输出、计划、审批、补全菜单
  editor.py     带光标的文本缓冲区（readline 键位）
  completion.py 模糊匹配、命令与文件候选
  markdown.py   终端 Markdown 渲染（跨片段按列折行）
  syntax.py     零依赖分词高亮器
  state.py      前端渲染的 UI 状态模型
  screen.py     增量帧写入器（只重画变化行）
  fullscreen.py alternate screen 前端（默认）
  stream.py     滚动缓冲区前端（--stream）
  modal.py      覆盖层对话框
  input.py      按键解码与 raw mode
  app.py        单写者应用循环
```

UI 的单写者规则：Runtime 回调运行在模型 worker 和后台 sub-agent 线程上，它们**不允许直接触碰屏幕**，只能 `App.post()` 入队，由 UI 线程在每帧绘制前统一消费。这是流式 token、工具状态和审批提示不会在转义序列中间相互穿插的原因。

## Alpha 边界

Fun 目前是公开 Alpha，不是 feature-complete 1.0。暂不承诺：

- Anthropic 原生协议适配
- Web Search
- Inject / Queue
- 基于模型的上下文摘要压缩（确定性长度裁剪已具备）
- 跨会话 Memory
- 多 Agent 协作（Runtime 已有可取消的后台 sub-agent 原语，但没有协作编排）

请先在测试仓库或可恢复的工作区使用，不要对未经审阅的破坏性自动化授予无限权限。

## 开发

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q fun tests
python3 -m pip install -e .
```

当前测试套件 608 个用例。覆盖 Runtime/Event Replay、审批与命令解析、Keychain 配置语义、SQLite 并发、Terminal UI、Windows pipe/console 与跨平台路径行为。UI 相关用例会在开关颜色两种条件下验证布局宽度，因此建议至少跑一次：

```bash
NO_COLOR=1 python3 -m unittest discover -s tests -q
FORCE_COLOR=3 python3 -m unittest discover -s tests -q
```

每次提交都会通过 GitHub Actions 的 Python 3.11 / 3.12 测试矩阵、Windows 逐模块矩阵和 package install smoke test。package smoke 会校验 artifact checksum，并分别验证 wheel 及 sdist 构建/安装。发布由版本 tag 触发，并附带 wheel、sdist 和 SHA256 checksums；当前最新 Alpha 为 `v1.0.0a6`。`main` 上的后续修复会在下一次 Alpha tag 发布后进入可下载 artifact，Release 包不会假装包含未发布提交。

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
