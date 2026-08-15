# Fun Harness 完整产品与系统设计

> **Coding should feel good.** 让写代码变得有意思。

- 文档版本：0.2
- 目标：作为 Fun V1 的产品、Runtime、协议和交互基线
- 产品定位：开箱即用、可深度配置、面向复杂 Coding 任务的终端 Agent Runtime
- 设计原则：Runtime 负责事实、状态、安全和恢复；模型只负责提出下一步

---

## 0. 先定义 Fun 到底是什么

Fun 不是一个聊天框，也不是一个“大 Prompt + while 循环”。它是一个本地运行的、事件驱动的 Coding Agent Runtime：

```text
终端 UI
  ↓ 消费结构化事件
Session / Task Runtime
  ↓ 编排
Model Gateway + Tool Runtime + Policy Engine + Memory + Context
  ↓
Workspace / Web / Shell / Persistence
```

Fun 的核心价值不是工具数量，而是让复杂任务形成可靠闭环：

```text
理解目标 → 建立计划 → 观察证据 → 小步行动 → 验证 → 调整计划 → 完成 / 暂停 / 恢复
```

### 0.1 产品差异

- 相比 Pi：默认更完整、更容易上手，复杂任务状态和终端交互更清楚。
- 相比 Claude Code / Codex 类产品：更强调 Runtime 协议、事实源、可恢复变更、可配置模型网关和本地可审计性。
- 相比普通 Agent：模型不能直接操作环境，一切动作必须经过 Runtime。

### 0.2 V1 的克制边界

V1 必须先验证一条纵向闭环：**一个 workspace 中，一个任务能安全、透明、可验证地完成。**

V1 必须做：

- 单用户、单本地 Session、单 active Task、单 workspace。
- 一个优先完成的 Model Gateway，首选 OpenAI-compatible；允许手动 Model ID。
- Plan and Execute + 步骤内有限 ReAct。
- `explore`、`read`、`edit`、`exec` 四个核心工具。
- workspace 边界、patch hash 校验、diff、validation、checkpoint、stop。
- Ask / Smart / Auto 放行模式及不可覆盖的 Critical hard boundary。
- Runtime 事件事实源、Task/Turn/Tool 状态机、事件 replay 基础。
- 单栏流式终端 UI、浅蓝色主题、中英文 i18n 基础。
- 基础 usage：input/output token、exact/estimated、TTFT、总耗时、工具耗时。
- 人工 `/compact` 和 token budget 预警；自动压缩暂不作为首发承诺。
- 证据驱动的最小自动记忆：仅保存 workspace/task 的低风险事实，不做复杂跨项目记忆。

V1.x 紧接着做：

- Anthropic native、动态模型列表和 capability probing。
- `web_search`（可先作为只读实验开关；接入网络策略后进入默认工具）。
- Inject：只在下一个 safe point 生效。
- Queue：单级顺序队列，不做复杂依赖。
- 完整 Session replay、断线恢复和 unknown side-effect 恢复 UI。
- 自动 Context Compaction。
- 更完整的 Memory 冲突、过期、跨 Session 策略。
- usage 详情面板和更多模型能力降级。

明确不做：

- 多 Agent 协作。
- 多真人共享 Session。
- 远程 workspace。
- IDE 插件和 Web Dashboard。
- 插件市场。
- 自动部署生产环境。
- 复杂向量记忆系统。
- 默认永久保存模型原始 reasoning。

---

# 1. 用户旅程

## 1.1 首次使用

```text
运行 fun
→ 选择语言
→ 阅读并同意安全说明
→ 配置模型连接
→ 测试 URL / Key
→ 拉取真实模型
→ 选择模型和推理强度
→ 选择默认放行策略
→ 选择 workspace
→ 查看 workspace 风险摘要
→ 同意本 workspace
→ 扫描项目
→ 进入工作区
```

## 1.2 日常使用

V1 的默认日常路径先收敛为单 active Task：

```text
fun
→ 打开最近 workspace / session（可跳过）
→ 输入目标
→ Fun 给出短计划
→ 用户可直接执行或停止
→ Agent 流式工作
→ 用户可暂停、停止；Inject / Queue 在 V1.x 开放
→ diff、测试和基础 usage 持续可见
→ 任务完成，保存 Session、Task、证据、低风险 Memory 和 Checkpoint
```

一个 Session 可以保存多个历史 Task，但 V1 同时只允许一个 running Task。这样先保证状态机、事件 replay 和变更归属清晰，再增加队列调度。

## 1.3 一次性模式

```bash
fun "修复登录测试"
fun --workspace ~/code/app "解释订单模块"
fun --resume ses_xxx
fun --print "总结当前 diff"
```

一次性模式使用同一 Runtime，只在任务完成、失败或明确阻塞后退出；需要审批时仍按策略处理。

## 1.4 REPL 内控制

```text
/help       命令帮助
/model      当前连接中的模型选择器
/approval   Ask / Smart / Auto
/reasoning  Auto / Low / Medium / High
/status     紧凑状态
/plan       计划展开
/diff       变更审阅
/queue      后续任务队列
/inject     插入当前任务
/memory     查看自动记忆
/compact    请求上下文压缩
/pause      暂停
/stop       停止并保存
/resume     恢复任务
/settings   配置
/quit       退出
```

输入 `/` 后进入命令补全；用户不应被迫记住参数。

---

# 2. 配置体系

## 2.1 配置作用域

```text
Global
├── locale
├── connection metadata
├── default model
├── default reasoning
├── default approval
└── theme / keymap

Workspace
├── workspace identity
├── project instructions
├── protected paths
├── allowed command hints
├── workspace model override
└── workspace approval override

Session
├── active model
├── active reasoning
├── active approval
├── task state
└── queue
```

Session 的临时修改不覆盖 Global；用户明确保存后才写入默认值。Workspace 规则可以覆盖默认行为，但不能削弱硬安全边界。

## 2.2 凭证

- Key / Token 不写入项目文件。
- 优先使用系统 Keychain；无 Keychain 时使用权限严格的本地凭证文件。
- 普通配置只保存 `connection_id`。
- 日志、事件和错误消息必须脱敏。
- Prompt、web_search query、shell 输出不能泄露 Key。

## 2.3 连接配置

```json
{
  "id": "conn_01",
  "protocol": "openai-compatible",
  "base_url": "https://api.example.com/v1",
  "credential_ref": "keychain:fun/conn_01",
  "models_source": "discovered",
  "created_at": "..."
}
```

支持：

- OpenAI-compatible。
- Anthropic native。
- API Key / Bearer Token。
- 自定义 URL。
- 自动发现失败时手动 Model ID。

## 2.4 语言与 i18n

V1 支持 `zh-CN` 与 `en-US`。所有用户可见文本使用 message key；事件 payload 不保存已经翻译的终端文本，而保存 canonical 数据和 message key，便于恢复时按当前语言渲染。

必须提前处理：

- 中文双宽字符。
- 文案长度变化。
- 日期、数字和 token 格式。
- 无色终端。
- 未来 `zh-TW`、`ja-JP`、`ko-KR` 等 locale。

---

# 3. Workspace 模型与安全边界

## 3.1 Workspace

Workspace 是一次授权的本地根目录：

```json
{
  "id": "ws_01",
  "root": "/Users/me/project",
  "real_root": "/Users/me/project",
  "git": {"is_repo": true, "branch": "main", "revision": "abc123"},
  "consent_version": 1,
  "scan_revision": "..."
}
```

## 3.2 路径安全

所有路径必须：

1. 解析相对路径。
2. 规范化 `.`、`..`。
3. 解析 symlink 目标。
4. 比较 realpath 是否仍在 real workspace root 下。
5. 拒绝 workspace 外路径。
6. 对文件操作再次检查竞态变化。

不能只检查字符串前缀。`/project-x` 不能因为以 `/project` 开头而被放行。

## 3.3 Shell 安全

`exec` 的命令必须经过：

- cwd 边界检查。
- shell 解析或受限执行器。
- 重定向、管道、命令替换路径检查。
- `cd`、`git -C`、`find`、`cp`、`mv`、`ln` 等路径分析。
- 超时、输出上限、子进程回收。
- 风险分类。

V1 不承诺完美解析所有 shell 语法；无法判断的命令按照高风险处理，不静默放行。

## 3.4 用户已有修改

Fun 必须区分：

```text
user_change       Fun 启动前就存在
agent_change      Fun 当前 Session 产生
external_change   运行期间由其他进程产生
```

回滚只允许操作 `agent_change`。禁止默认 `git reset --hard`，因为会破坏用户原有修改。

---

# 4. Runtime 对象与状态机

## 4.1 对象关系

```text
Session
 ├── Workspace
 ├── Connection
 ├── active settings
 ├── Event stream
 ├── Memories
 └── Tasks
      ├── active task
      └── queued tasks

Task
 ├── Goal
 ├── Constraints
 ├── Plan revisions
 ├── Turns
 ├── Tool calls
 ├── Changeset
 ├── Validation
 ├── Checkpoints
 └── Usage
```

## 4.2 Task 状态

```text
created → planning → running → waiting_user
                 ↘ paused ↔ running
                 ↘ blocked
                 ↘ completed
                 ↘ failed
                 ↘ stopped
```

- `waiting_user`：需要确认、需要补充信息或等待插入消息。
- `paused`：用户主动暂停，资源释放，状态可恢复。
- `blocked`：Runtime 预算、安全、模型能力或环境问题阻止继续。
- `failed`：发生无法自动恢复的技术错误。
- `stopped`：用户主动停止，不等于失败。

## 4.3 Tool 状态

```text
proposed
→ validated
→ policy_checked
→ approval_pending
→ started
→ streaming
→ completed / failed / cancelled / unknown
```

进程崩溃时非终态工具变成 `unknown`。`unknown` 不自动重试，除非执行器能证明操作没有发生或具备幂等键。

## 4.4 Safe Point

插入、暂停、模型切换和压缩在 safe point 生效：

- 当前模型请求完成后。
- 当前工具完成后。
- 当前工具被可靠取消后。
- 当前 patch 尚未应用时。

不可强制杀掉的外部命令不会被假装立即停止；UI 显示“等待进程退出”。

---

# 5. Agent Loop 与任务策略

## 5.1 新任务启动

模型先判断任务规模：

- 简单问答：无需计划，直接回答。
- 只读调查：短计划可选。
- 涉及修改或多个模块：必须计划。
- 用户明确要求执行：直接进入计划和执行，不长篇征求“是否开始”。

初始计划 2–7 步；每一步必须有动词和可验证结果。

## 5.2 Plan and Execute

```text
Planner
  产生目标、约束、步骤和验收标准
Executor
  选择当前步骤
ReAct
  observe → decide → tool → verify
Planner
  根据证据完成、修改或阻塞步骤
```

计划变更需记录：旧 revision、新 revision、变更原因、证据事件。

## 5.3 ReAct 约束

ReAct 不是无限自由循环：

- 一次只提出一个主要动作。
- 工具结果必须进入下一次 context。
- 修改前必须有事实依据或明确用户要求。
- 修改后必须优先验证。
- 同一等价失败动作默认最多 3 次。
- 任务预算达到上限就暂停，不继续猜。

## 5.4 复杂任务的进度

Fun 不制造虚假的百分比。默认显示：

```text
step 2/5
```

只有计划步骤具备清晰完成状态时才显示百分比。工具调用数量不是任务进度。

---

# 6. System Prompt 规格

## 6.1 Prompt 的层级

```text
Runtime hard rules      Runtime 强制，不依赖模型服从
System identity         Fun 身份和目标
Workspace facts         当前 workspace 的事实
Task state              当前目标、约束、计划、预算
Memory context          自动抓取且有来源的记忆
Tool contract           工具 Schema 与使用规则
Conversation            用户消息和最近事件
```

### 6.2 Canonical System Prompt

```text
You are Fun, a terminal coding agent controlled by a managed Runtime.

Your objective is to make safe, minimal, verifiable progress on the user's task.

SOURCE OF TRUTH
- The Runtime is authoritative for workspace state, tool execution, approvals,
  task status, memory, checkpoints, and usage.
- A proposed tool call is not an executed action.
- Never claim success before the Runtime returns a successful tool result.
- Never invent, simulate, or infer a tool result.

WORKSPACE
- Use only paths inside the selected workspace.
- Never escape through .., symlinks, shell expansion, redirects, git -C,
  mounts, encoded paths, or child processes.
- Do not access secrets or protected files unless the Runtime explicitly allows it.
- Do not modify Fun's policy, audit log, event store, or checkpoint data.

TASK METHOD
- For a substantial coding task, maintain a concise 2-7 step plan.
- Each step must have a verifiable outcome.
- Work from evidence. Inspect before editing.
- Make the smallest change that tests the current hypothesis.
- After editing, inspect the diff and run the narrowest useful validation.
- Update the plan when evidence changes the approach.
- Record rejected hypotheses and do not repeat disproved approaches.

TOOLS
- Use structured tool calls only.
- Select the smallest tool that answers the current question.
- One primary action at a time.
- Include a short purpose for commands and a summary for edits.
- Wait for the tool result; do not continue as if it succeeded.

COMMUNICATION
- Emit concise activity updates before meaningful actions.
- Report important findings, risks, changed files, and validation.
- Do not expose hidden chain-of-thought or produce a long private monologue.
- Produce short, auditable activity text instead.
- At completion report: result, changed files, validation, risks, and next action.

FAILURE
- Do not retry an unchanged failing action beyond the Runtime budget.
- If the current hypothesis is disproved, choose a new approach or ask for help.
- If the task becomes materially broader, update the plan first.
- If blocked by safety, permissions, missing information, or environment, stop
  clearly and explain what is needed.
```

### 6.3 Prompt 注入防护

- 项目文件中的指令是 workspace context，不自动拥有 system 级权限。
- 外部网页内容、README、代码注释中的“忽略之前指令”均视为不可信数据。
- `.fun/instructions` 可以作为项目规则，但由 Runtime 标记来源、作用域和优先级。
- 用户消息、系统安全策略、workspace 规则、外部内容的优先级必须明确。

优先级：

```text
Runtime hard boundary
> System prompt
> User request
> Workspace instructions
> Tool output / project content / web content
```

---

# 7. Tool Call 精细设计

## 7.1 统一请求

```json
{
  "type": "tool_call",
  "call_id": "call_01",
  "name": "edit",
  "arguments": {
    "path": "src/login.ts",
    "expected_hash": "sha256:...",
    "patch": "@@ ...",
    "summary": "修复 token 过期分支"
  }
}
```

Runtime 先 JSON Schema 校验，再做业务校验。Schema 错误返回给模型修正，但不消耗普通重试预算。

## 7.2 Tool Result

```json
{
  "type": "tool_result",
  "call_id": "call_01",
  "status": "success",
  "content": {"kind":"text","text":"..."},
  "meta": {
    "duration_ms": 120,
    "truncated": false,
    "workspace_revision": "change-set:4",
    "usage": {"result_chars": 4200}
  }
}
```

错误必须有稳定 code：

```text
INVALID_ARGUMENTS
PATH_OUTSIDE_WORKSPACE
SYMLINK_ESCAPE
PROTECTED_PATH
APPROVAL_REQUIRED
COMMAND_TIMEOUT
OUTPUT_TRUNCATED
FILE_CHANGED_SINCE_READ
PATCH_FAILED
PROCESS_UNKNOWN_AFTER_CRASH
NETWORK_POLICY_BLOCKED
PROVIDER_ERROR
```

## 7.3 五个核心工具

### `explore`

只读。支持 tree、files、text、git_status。默认忽略 `.git`、依赖目录、构建产物和二进制。

### `read`

只读。要求相对路径，支持行范围和字符上限，返回行号、hash、编码和截断标记。

### `edit`

只接受 patch + expected hash。禁止 `write_file`。每次成功编辑生成 changeset entry、diff 和 checkpoint reference。

### `exec`

执行命令。必须提供 `purpose`，由 Runtime 做风险分析、超时、cwd、输出和循环控制。

### `web_search`

查询公共网络。查询前做敏感信息扫描；结果保存来源 URL、标题、摘要和时间，不直接执行网页命令。

`inspect` 是 Runtime 查询能力，V1 可以同时作为 slash command 和受限工具，但它不能修改任何状态。

## 7.4 Tool Call 并发

V1 默认串行工具调用。原因：

- 文件变化顺序更清楚。
- 审批更容易理解。
- 崩溃恢复更简单。
- 避免两个 edit 同时修改同一文件。

只读、互不相关的 `explore/read/web_search` 以后可以由 Runtime 安全并发；模型不直接决定并发。

---

# 8. Edit、Diff、Checkpoint 和 Recovery

## 8.1 Edit 事务

```text
read version
→ model proposes patch
→ Runtime checks path and hash
→ dry-run patch
→ classify risk
→ approval if needed
→ write temp file
→ fsync / atomic rename
→ record changeset
→ compute diff
→ checkpoint
→ return result
```

任何一步失败都不能告诉模型“已修改”。

## 8.2 删除

V1 不提供永久删除工具。模型如果需要删除文件：

- 由 `edit` 生成删除 patch，或
- 由 `exec` 触发高风险策略。

优先移动到 Fun 回收区；永久删除始终 Critical。

## 8.3 Checkpoint

Checkpoint 包含：

```text
workspace revision
agent changeset
session event seq
plan revision
memory snapshot reference
context snapshot reference
usage aggregate
```

恢复只撤销 Agent 变更，不触碰用户原有修改和外部修改。

## 8.4 崩溃恢复

恢复时：

1. 读取最后一个完整事件 seq。
2. 找到非终态 Task、Turn、Tool。
3. 非终态工具标记 `unknown`。
4. 检查进程和文件 hash。
5. 不自动重放不确定的 exec/edit。
6. 生成恢复提示。
7. 用户选择继续、重试或停止。

---

# 9. Memory 设计

## 9.1 自动记忆，不手动写入

Memory Extractor 由 Runtime 在事件流上运行；模型不能直接写 Memory，用户也不需要每次确认。

记忆来源：

```text
项目文件检测
命令结果
测试结果
重复验证
用户明确约束
任务结论
```

## 9.2 记忆分类

```text
session_fact       当前会话事实
 task_fact         当前任务事实
workspace_fact     项目稳定事实
user_preference    用户稳定偏好
negative_fact      已证伪或不应重复的路线
```

每条记忆带：scope、source event、confidence、freshness、status、last_used。

## 9.3 记忆生命周期

```text
candidate → active → stale → archived
                 ↘ contradicted
```

冲突时保留历史，但当前 context 只注入最新且有较高证据的事实。

## 9.4 Context 的记忆选择

每次请求只注入与当前 task 相关的少量记忆：

```text
当前 workspace 事实
当前任务事实
最近相关 negative fact
用户语言和编码偏好
```

不能把 Memory Store 全量塞给模型。

---

# 10. Context Compaction

## 10.1 Context 四层

```text
raw events        完整事实和审计
artifacts         diff、测试日志、搜索结果归档
memory            提炼事实
model context     本次请求实际注入内容
```

## 10.2 压缩触发

- 70%：状态栏提示。
- 80%：V1 提示用户执行 `/compact`，不自动改变上下文。
- 85%：V1 阻止新的模型请求并进入 `blocked: context_budget`，等待用户压缩或停止；自动压缩属于 V1.x。
- `/compact`：用户主动压缩。
- 切换小上下文模型：立即重新评估。

## 10.3 摘要结构

```text
GOAL
CONSTRAINTS
CURRENT PLAN
VERIFIED FACTS
REJECTED HYPOTHESES
AGENT CHANGES
VALIDATION
OPEN RISKS
BLOCKERS
QUEUE
NEXT SAFE ACTION
```

摘要必须带 source event range。压缩失败保留旧 context snapshot，不损坏会话。

---

# 11. 插入、队列与并发

## 11.1 Inject

Inject 是对当前 Task 的即时指令或约束：

```text
Ctrl+Enter
/inject
```

它在下一个 safe point 进入 context：

```text
当前命令完成
→ 写入 conversation.injected
→ 重新构造 context
→ 模型决定继续、回滚、调整计划或暂停
```

## 11.2 Queue

Queue 是不打断当前 Task 的后续工作：

```text
Alt+Enter
/queue
```

状态：

```text
queued → ready → running → completed / failed / cancelled / paused
```

前一个 Task 失败时，后续默认暂停；用户可以选择继续，但 Runtime 会重新检查 workspace revision、前置条件和变更。

## 11.3 模型 / 权限 / 推理热切换

```text
/model
/approval
/reasoning
```

修改在下一次模型请求生效；当前模型请求和当前 exec 不被粗暴切断。切换模型后检查：工具调用、上下文窗口、reasoning、usage 能力。

---

# 12. Provider Gateway

## 12.1 动态发现（V1.x）

V1 允许用户在连接配置中手动填写 Model ID；V1.x 再执行真实模型发现：

```text
protocol + URL + credential
→ test connection
→ list models（可选）
→ normalize model descriptors
→ capability probe
→ user selects
```

模型选择器只展示该连接实际返回的模型。没有 `/models` 时保留手动填 Model ID 的 fallback。未知能力必须标记为 `unknown`，不能当作支持。

## 12.2 Model Descriptor

```json
{
  "id": "deepseek-v4",
  "display_name": "DeepSeek V4",
  "context_window": 128000,
  "supports_tools": true,
  "supports_streaming": true,
  "supports_reasoning": true,
  "supports_vision": false,
  "usage_precision": "exact",
  "reasoning_levels": ["low","medium","high"]
}
```

## 12.3 统一流事件

```text
text_delta
reasoning_delta
activity_delta
tool_call_delta
tool_call_complete
usage
completed
error
```

供应商差异由 Adapter 吸收，不污染 Agent Loop。

---

# 13. Approval、风险和循环

## 13.1 三模式

```text
Ask    所有副作用操作询问
Smart  低风险自动，中高风险按策略询问
Auto   不打扰普通操作，但不能绕过硬边界
```

## 13.2 风险分级

```text
Low       read、explore、git status、普通测试
Medium    普通源码 patch、安装依赖、启动服务
High      敏感文件、删除、锁文件、配置、网络上传、git push
Critical  workspace 外访问、不可恢复删除、绕过 Runtime、改安全策略
```

Critical 在任何模式下拒绝或阻断。

## 13.3 变更预算

默认预算：

```text
单次 patch 最多 20 文件
单文件单次最多 200 行删除
单 Task 默认最多 60 tool calls
同一失败最多 3 次
单命令默认 120 秒
```

超预算进入 `blocked`，不自动继续。

## 13.4 循环保护

对以下内容生成 fingerprint：

```text
tool + normalized args + workspace revision + relevant result
```

重复命令、重复 patch、同一测试错误、同一假设连续出现时停止并报告：

```text
检测到重复失败，已暂停。
最近三次：pnpm test auth，结果相同。
请选择：查看日志 / 换策略 / 增加预算 / 恢复任务。
```

---

# 14. UI 规格

## 14.1 默认不是双屏

终端默认采用：

```text
单栏流式正文
+ 紧凑状态栏
+ 可折叠计划
+ 底部输入栏
```

80 列优先。宽屏增加信息密度，不切换成桌面式双栏。

## 14.2 视觉标记

```text
◇ plan / result
◌ active
! finding / warning
✓ success
× error
○ pending
▶ collapsed detail
```

颜色只是辅助，去掉颜色后仍可读。

## 14.3 默认界面

```text
┌─ Fun · 修复登录测试 ───────────────────────────────────────────────┐
│ ● running · step 2/4 · 61% · 00:42                         [Tab]    │
├─────────────────────────────────────────────────────────────────────┤
│ YOU                                                                 │
│ 帮我修复登录测试，并确保不新增依赖                                  │
│                                                                     │
│ ◇ PLAN                                                              │
│   ✓ 扫描项目                                                        │
│   ● 定位失败                                                        │
│   ○ 修改实现                                                        │
│   ○ 验证                                                            │
│                                                                     │
│ ◌ 正在检查 src/auth/login.ts                                        │
│   检查 token 校验和错误处理……                                      │
│                                                                     │
│ ! 发现：mock 使用了旧版 repository 接口                             │
│                                                                     │
│ ▶ read src/auth/login.ts · 84ms                                     │
├─────────────────────────────────────────────────────────────────────┤
│ Enter 继续  Tab 状态  Space 暂停  Ctrl-C 停止  ? 帮助                │
└─────────────────────────────────────────────────────────────────────┘
```

不再持续刷新大型 `Working Summary` 卡片；过程用活动行，重要内容用 finding，最终用 result。

## 14.4 流式渲染

- token delta 在 30–80ms 批量绘制。
- 已完成内容不反复跳动。
- 当前活动行可更新。
- 工具输出按块追加。
- 长输出折叠并保留展开入口。
- 右侧或状态面板不是默认常驻区。

## 14.5 指标

状态栏只显示少量：

```text
DeepSeek V4 · High · 38 tok/s · 18.6s
```

`/status` 或工具详情展示：

```text
Input 12.4k · Output 2.1k · Reasoning 8.2k
TTFT 1.2s · Generation 11.9s · Tools 6.7s · Total 18.6s
```

精确值和估算值必须区分；估算显示 `~`。

---

# 15. 指标、日志与可观测性

每个 Model Request、Tool Call、Task 都记录：

```text
start / end
provider / model
input / output / reasoning tokens
TTFT / generation / tool / approval / total time
retry count
status / error code
```

原始响应和工具日志可落盘，但默认 UI 不全部展开。日志必须脱敏，且能够通过 `seq`、`task_id`、`call_id` 关联。

---

# 16. 数据存储

推荐本地 SQLite：

```text
connections
sessions
workspaces
tasks
plans
turns
events
artifacts
memories
checkpoints
changesets
usage_records
queue_items
```

大内容存 Artifact Store，数据库保存 hash、size、mime、source event range 和压缩摘要。

### 16.1 数据迁移

所有表和事件有 schema version。升级时：

- 先备份数据库。
- 迁移事务化执行。
- 失败回滚。
- 旧事件保留，renderer 支持旧版本。

---

# 17. 异常和恢复矩阵

| 场景 | Runtime 行为 |
|---|---|
| 模型超时 | 记录 provider error，按有限重试策略处理 |
| API 断网 | 暂停任务，保留事件和上下文，可重试 |
| 工具命令超时 | 杀进程并记录，无法确认时标记 unknown |
| patch 前文件改变 | 拒绝应用，重新 read，不覆盖 |
| 工具执行中崩溃 | 重启后标记 unknown，不盲目重试 |
| compaction 失败 | 保留旧 snapshot，任务不损坏 |
| UI 断线 | 使用 event seq replay |
| workspace 被删除 | task blocked，要求重新选择 workspace |
| provider 切换 | 下一模型请求生效，重新检查能力 |
| 前一队列任务失败 | 后续 queue 默认暂停 |
| 用户 Ctrl-C | 尝试安全停止，保存 task stopped |
| 预算耗尽 | task blocked，给出增加预算或停止选项 |

---

# 18. 测试设计

## 18.1 协议测试

- Event seq 单调性。
- 事件幂等写入。
- Tool call schema 拒绝未知字段。
- Tool result 正确关联 call_id。
- Provider event normalization。

## 18.2 安全测试

- `..`、symlink、绝对路径、编码路径。
- shell redirect、pipe、subshell、git -C。
- 敏感文件脱敏。
- Auto 不能绕过 Critical。
- 用户已有修改不能被 rollback 清掉。

## 18.3 恢复测试

- 每个 tool 状态崩溃注入。
- exec unknown 不自动重复。
- edit atomic write。
- context compaction 中断。
- UI cursor replay。
- queue 切换崩溃。

## 18.4 Agent 行为测试

- 简单问答不强行计划。
- 多文件任务产生计划。
- 工具失败后不伪造成功。
- 相同失败达到预算后停止。
- edit 后运行验证。
- 已证伪假设不重复。

## 18.5 UI 测试

- 80、100、120、160 列。
- 中英文本宽度。
- 颜色关闭。
- 长路径和长命令截断。
- 快速 delta 不闪烁。
- approval、queue、inject 状态可理解。

---

# 19. 开发里程碑

## M0：协议冻结

Event、Tool、Task、Checkpoint、Context Manifest、错误码和状态机。

## M1：最小可用闭环

单 workspace、单模型连接、`explore/read/edit/exec`、单栏流式 UI、diff、测试、停止。

## M2：Model Gateway

OpenAI-compatible、usage、推理强度、热切换和手动 Model ID。动态发现与 Anthropic native 进入后续 V1.x。

## M3：复杂任务

Plan、ReAct、循环预算、失败分类、validation、checkpoint。

## M4：连续性

V1：Session resume 基础事件 replay、低风险 Memory、人工 Compaction。V1.x：自动 Compaction、Inject、Queue 和完整 unknown-side-effect recovery UI。

## M5：产品化

首次 onboarding、中英文、浅蓝主题、终端适配、错误体验、安装和升级。

---

# 20. 开源工程基线

Fun 作为开源项目的工程、治理、安全、许可证、贡献和发布设计见 [`fun-open-source-blueprint.md`](fun-open-source-blueprint.md)。

Fun V1 的实现契约、错误码、Provider / Tool 最小接口、CLI 退出码和验收场景见 [`fun-v1-contract.md`](fun-v1-contract.md)。

开源项目必须让陌生人能够：

```text
安装 → 配置 → 运行 → 审阅 diff → 停止 / 恢复 → 测试 → 贡献
```

因此 README、LICENSE、SECURITY、CONTRIBUTING、CHANGELOG 和迁移说明不是发布后的装饰，而是进入公开开发前的基础交付物。

---

# 21. 配套设计文档

本设计不把所有细节塞进单一文档，配套资料如下：

- [`fun-runtime-spec.md`](fun-runtime-spec.md)：Runtime、System Prompt、Tool Call、Event、Context Manifest 和协议细节。
- [`fun-flowcharts.md`](fun-flowcharts.md)：Onboarding、Task、Approval、Inject/Queue、Compaction、Crash Recovery 流程图。
- [`fun-product-design.md`](fun-product-design.md)：早期产品方向记录和视觉/体验原则。
- [`fun-harness-ui-v2.svg`](fun-harness-ui-v2.svg)：单栏流式终端 UI 视觉稿。

推荐阅读顺序：

```text
fun-complete-design.md
  → fun-runtime-spec.md
  → fun-flowcharts.md
  → UI 视觉稿
```

---

# 22. V1 完成定义

用户在一个新 workspace 中运行 `fun`，不看文档也能：

1. 选择语言。
2. 配置一个 URL 和 Key。
3. V1 验证连接并填写 Model ID；V1.x 再从该连接真实拉取模型列表。
4. 选择推理强度和放行策略。
5. 同意 workspace 风险说明。
6. 输入复杂 Coding 任务。
7. 看见简洁计划和流式活动。
8. 看见工具调用和验证结果。
9. V1 可以暂停和停止任务；V1.x 再在运行中插入消息或排队任务。
10. 随时切模型、切策略、切推理强度。
11. 看到 diff、token、耗时和速度。
12. 退出后恢复 Session。
13. V1 保存基础事件和检查点；完整 unknown-side-effect 崩溃恢复选项属于 V1.x。
14. 不会访问 workspace 外或破坏用户已有修改。

如果其中任一项依赖用户先阅读内部实现文档，说明“开箱即用”还没有达到。

---

# 23. V1 设计冻结决策补充

本节用于消除实现阶段最容易产生歧义的边界。

## 21.1 CLI 生命周期

- `fun` 首次运行进入 onboarding；已有配置时默认打开最近 workspace，但**不自动恢复正在运行的 Task**，而是显示恢复选择器。
- `fun "任务"` 使用一次性模式：任务完成、失败、阻塞或取消后退出，并返回稳定退出码。
- 非 TTY 自动关闭动画和颜色，使用纯文本事件流；需要人工审批的操作默认拒绝并以阻塞退出，不等待 stdin。
- 第一次 `Ctrl-C` 请求 graceful stop；第二次 `Ctrl-C` 在短时间内强制退出并将未完成工具标为 `unknown`。
- 退出前有限等待事件、checkpoint 和 changeset 落盘；超时则安全停止调度，不假装任务完成。
- V1 不支持 CLI 退出后继续运行后台 Agent；已启动但无法确认状态的命令进入恢复流程。
- 同一 workspace 默认单实例锁；发现活跃锁时显示 PID、Session 和接管选项，不能静默并发写入。

## 21.2 一次性模式退出码

```text
0  completed
1  task failed
2  task blocked / needs user
3  user cancelled
4  configuration or provider error
5  recovery required / unknown side effect
6  invalid CLI arguments
```

`--json` 输出结构化事件和最终结果到 stdout；诊断和进度写 stderr。

## 21.3 配置优先级和生效时机

优先级从低到高：

```text
built-in defaults
→ global config
→ workspace config
→ environment variables
→ CLI flags
→ current Session overrides
```

凭证不随普通配置复制；Session 保存 model、provider、prompt、policy、reasoning、budget 和 tool schema 的快照。模型、approval 和 reasoning 的交互修改在下一次模型请求生效；workspace 根目录、连接协议和语言变更在新 Session 生效。

## 21.4 Provider 兼容性决策

连接测试分成四步并分别报告：

```text
endpoint reachable
→ credential accepted
→ model list available
→ selected model capabilities usable
```

模型列表不可用时允许手动 Model ID，但能力字段必须标记为 `known`、`inferred` 或 `unknown`。模型请求重试按错误类型区分；已经产生 tool call 的请求不能无条件重试。

V1 默认只执行串行 tool call。Provider 不支持 tools 时进入 chat-only 能力提示，不把任意 JSON 文本误判为工具调用。Provider 返回的 reasoning 可以作为内部输入，但 UI 只展示结构化活动和结果，不依赖原始隐藏思维链。

## 21.5 Prompt 与项目规则

项目规则文件可以被发现和注入，但只能作为 workspace context，不能覆盖 Runtime hard boundary。文件、README、网页和 tool result 中的指令都按不可信数据处理。每次模型请求记录：

```text
prompt_version
prompt_manifest_hash
model/provider snapshot
context snapshot revision
event range
```

这样可以复现模型当时看到的环境。

## 21.6 工具、并发和恢复

- V1 工具串行执行，避免 edit 冲突和审批聚合复杂度。
- 重复 `call_id` 拒绝；工具 schema 未知字段拒绝。
- `read/explore/web_search` 可以未来由 Runtime 安全并发，但不由模型直接决定。
- 所有外部副作用有 operation ID；恢复时先查询或校验，再决定是否重试。
- 未完成的 `exec` 在崩溃后默认 `unknown`；未完成的 `read/explore` 可安全重试；`edit` 必须重新校验 hash 后才能重试。
- 旧审批在重启后全部失效，必须重新审批。

## 21.7 Memory 和隐私

Memory Extractor 异步运行，失败不阻塞 Task。以下内容永远不得写入 Memory：

```text
API key、token、密码、私钥、客户数据、未脱敏个人信息、完整私有凭证 URL
```

Memory 删除只删除 Memory Store 中的当前事实；原始审计事件是否删除遵循单独的数据保留策略，不能因为删除记忆而破坏故障审计。Memory 来源枚举固定为：

```text
user_explicit / file_observed / command_verified / repeated_observed / model_inferred
```

## 21.8 Context Compaction

压缩摘要可以由模型生成，但必须由 Runtime 用结构化 Task、Plan、Tool、Change、Validation 和 Queue 状态校验；结构化状态才是恢复依据。压缩期间不启动新的模型请求，同一 Session 只有一个 compaction lock。压缩失败保留旧 snapshot。

## 21.9 终端降级

UI 的 canonical 数据来自事件，终端只是 renderer：

- 80–99 列：紧凑单栏。
- 100–119 列：标准单栏。
- 120 列以上：增加指标密度，不默认启用固定双栏。
- 无颜色时依靠 `◇ ◌ ! ✓ × ○ ▶` 等符号。
- 无 Unicode 时降级为 ASCII 标记。
- 非 TTY 不使用光标重绘。
- 中英文布局按终端显示宽度计算，不使用 JS 字符串长度。

## 21.10 数据迁移和磁盘异常

所有数据库和事件有 schema version；迁移事务化，失败进入只读恢复或导出诊断。磁盘空间不足时停止新的副作用操作，先保证事件和安全状态落盘；不能出现“文件已经修改但没有审计事件”的继续执行状态。

---

# 24. V1 设计冻结前的必备产物

在开始大量实现前，必须冻结以下文件或等价的 contract test：

1. Session / Task / Turn / Tool / Approval / Queue / Checkpoint 状态机。
2. Event catalog：每个事件的 schema、版本、敏感字段和幂等键。
3. Tool catalog：参数、风险、取消、超时、输出限制和恢复策略。
4. Approval matrix：工具风险 × Ask / Smart / Auto。
5. Provider compatibility matrix：OpenAI-compatible / Anthropic / fallback。
6. Prompt manifest 和上下文版本规范。
7. 数据保留、脱敏、Memory 删除和 Artifact 清理策略。
8. 配置 schema、迁移和 CLI 退出码规范。
9. 终端降级和 80/100/120 列快照测试。
10. 崩溃注入、unknown side effect 和恢复测试矩阵。

如果这些产物没有冻结，继续增加工具、UI 面板或模型适配器只会把不确定性推迟到实现阶段。
