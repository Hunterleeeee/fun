# Fun Harness Runtime & Agent Protocol 设计

> 本文是 Fun 的技术产品规格补充，优先级高于早期产品草案中的模糊描述。
>
> 核心原则：**Runtime 负责事实、状态、安全和恢复；模型只负责提出下一步。**

- 版本：0.2
- 适用范围：Fun V1 Core；V1.x 能力单独标注
- 状态：开源实现前协议基线

> V1 Core 只承诺单 workspace、单 active Task、OpenAI-compatible 优先、四个核心工具、有限 ReAct、事件事实源、diff/validation/checkpoint/stop 和单栏流式终端。Anthropic、动态模型发现、web_search、Inject、Queue、自动压缩和完整恢复 UI 属于 V1.x，除非章节明确标注。

---

## 1. 设计纠偏

早期 UI 草案把终端想得过于像桌面应用。Fun V1 不默认使用固定左右双屏，而采用：

```text
单栏流式正文
+ 一行运行状态栏
+ 可折叠的计划/任务状态
+ 底部输入与快捷键
```

原因：80 列终端是硬约束；终端最适合线性阅读、复制和流式追加，不适合长期占据一半宽度的导航栏。宽屏只增加信息密度，不切换成桌面式双栏。

同时，`Working Summary` 不作为一块持续刷新的长卡片。它改名为 **Activity Line / 活动行**：只显示当前动作和关键发现；完整计划、工具调用和结论按需展开。

---

## 2. Runtime 的职责边界

### 2.1 Runtime 做什么

- 维护 Session、Task、Turn 和队列状态。
- 把用户输入、模型输出和工具执行转换成事件。
- 决定工具调用是否允许执行。
- 验证 workspace 边界。
- 执行 patch、命令和搜索。
- 保存事件、记忆、指标和检查点。
- 管理上下文窗口和压缩。
- 处理插入、队列、暂停、停止、恢复和崩溃重启。
- 把供应商格式转换为统一模型事件。

### 2.2 模型做什么

- 理解用户目标。
- 提议计划。
- 选择一个工具并填写结构化参数。
- 根据工具结果更新计划或提出下一步。
- 生成简短的面向用户的进度说明。
- 生成最终结果。

### 2.3 模型不能做什么

- 不能直接访问文件系统或 shell。
- 不能自行决定越过 workspace。
- 不能修改审批策略。
- 不能把自然语言当成已经执行的事实。
- 不能写入长期记忆；只能提出记忆候选，由 Runtime 自动评估。
- 不能假设工具调用成功，必须等待 `tool.completed`。

---

## 3. 领域对象

### 3.1 Session

```json
{
  "id": "ses_01H...",
  "workspace_id": "ws_01H...",
  "locale": "zh-CN",
  "connection_id": "conn_deepseek",
  "model_id": "deepseek-v4",
  "approval_mode": "smart",
  "reasoning_effort": "auto",
  "status": "idle",
  "created_at": "2026-01-01T00:00:00Z",
  "last_event_seq": 1842
}
```

### 3.2 Task

```json
{
  "id": "task_01H...",
  "session_id": "ses_01H...",
  "goal": "修复订单创建接口的测试失败",
  "status": "running",
  "plan_id": "plan_01H...",
  "active_step_id": "step_03",
  "parent_task_id": null,
  "source": "user",
  "workspace_revision_start": "git:abc123",
  "workspace_revision_current": "agent-change-set:7",
  "created_at": "..."
}
```

### 3.3 Turn

Turn 是一次模型请求及其工具循环，不等于一次用户消息。

```json
{
  "id": "turn_01H...",
  "task_id": "task_01H...",
  "user_message_id": "msg_01H...",
  "status": "running",
  "attempt": 1,
  "model": "deepseek-v4",
  "reasoning_effort": "high",
  "started_at": "...",
  "ended_at": null
}
```

### 3.4 Plan / Step

```json
{
  "id": "plan_01H...",
  "task_id": "task_01H...",
  "revision": 3,
  "steps": [
    {
      "id": "step_01",
      "title": "理解项目结构",
      "status": "completed",
      "evidence_event_ids": ["evt_100", "evt_101"]
    },
    {
      "id": "step_02",
      "title": "定位失败测试",
      "status": "active",
      "evidence_event_ids": []
    }
  ]
}
```

计划只能由 Runtime 接受模型提出的 `plan.create` 或 `plan.update` 意图后更新。每次更新都增加 `revision`，防止 UI、模型和恢复流程看到不同版本。

---

## 4. 统一事件协议

### 4.1 Event Envelope

```json
{
  "id": "evt_01H...",
  "seq": 1842,
  "session_id": "ses_01H...",
  "task_id": "task_01H...",
  "turn_id": "turn_01H...",
  "type": "tool.completed",
  "version": 1,
  "ts": "2026-01-01T00:00:00.000Z",
  "payload": {},
  "caused_by": "evt_1841",
  "visibility": "ui_and_model"
}
```

### 4.2 事件类型分组

#### 会话

```text
session.created
session.resumed
session.settings.changed
workspace.opened
workspace.scan.completed
```

#### 任务

```text
task.created
task.started
task.paused
task.stopped
task.completed
task.failed
task.blocked
```

#### 模型

```text
model.request.started
model.reasoning.delta
model.text.delta
model.tool_call.proposed
model.request.completed
model.request.failed
```

#### 工具

```text
tool.requested
tool.approval.required
tool.started
tool.stdout.delta
tool.stderr.delta
tool.completed
tool.failed
tool.cancelled
```

#### 变更和恢复

```text
patch.proposed
patch.applied
patch.rejected
file.changed
diff.created
checkpoint.created
checkpoint.restored
```

#### 上下文和记忆

```text
context.warning
context.compaction.started
context.compacted
memory.candidate.extracted
memory.updated
memory.expired
```

#### 用户控制

```text
conversation.message.received
conversation.injected
conversation.queued
queue.item.started
queue.item.completed
queue.item.cancelled
approval.requested
approval.resolved
```

### 4.3 事件持久化规则

- 事件先写入事件表，再通知 UI。
- `seq` 在 Session 内单调递增。
- 每个消费者使用 `seq` 作为 replay cursor。
- UI 断线后从最后一个 cursor 重放。
- delta 可以合并显示，但原始重要事件不能删除。
- 事件写入采用幂等键：`id` 唯一。
- 工具结果必须包含 `request_id`，不允许只靠时间顺序关联。

### 4.4 UI 订阅协议

```json
{
  "from_seq": 1800,
  "include": ["task.*", "model.*", "tool.*", "file.*", "usage.*"],
  "delta_mode": "coalesce"
}
```

UI 收到事件后可以将连续 token 合并成 30–80ms 的绘制批次，避免每个 token 触发整屏重绘。

---

## 5. System Prompt 设计

System Prompt 不是全部逻辑。安全、路径、工具执行和状态更新由 Runtime 强制执行；Prompt 只是让模型正确使用这些能力。

### 5.1 Prompt 组装顺序

```text
A. 身份与目标
B. 不可违背的运行规则
C. 当前 workspace 信息
D. 当前任务和计划
E. 自动记忆
F. 工具协议
G. 输出格式
H. 当前对话和最近工具结果
```

每一部分独立生成，便于压缩和测试，不要把所有内容拼成无法追踪的一段字符串。

### 5.2 推荐 System Prompt 模板

```text
You are Fun, a terminal coding agent running inside a managed Runtime.
Your job is to make safe, verifiable progress on the user's coding task.

IDENTITY
- You are an execution-oriented coding agent, not a general chat assistant.
- Prefer inspecting the workspace and verifying facts before proposing changes.
- Work in small, reversible steps.

RUNTIME AUTHORITY
- The Runtime is the source of truth for workspace state, tool results,
  approvals, checkpoints, memory, token usage, and task status.
- Never claim that an action happened until the corresponding tool result says
  it succeeded.
- Never simulate a tool result.
- Never treat a proposed action as an executed action.

WORKSPACE BOUNDARY
- Work only inside the selected workspace.
- Do not access paths outside it, including through .., symlinks, shell
  expansion, git -C, redirects, mounts, or encoded paths.
- Do not read or expose secrets unless the Runtime explicitly permits it.
- Do not alter the Fun configuration, policy engine, audit log, or checkpoints.

TASK EXECUTION
- For a new substantial task, create a concise plan with 2-7 steps.
- Keep the plan adaptive: update it when evidence changes the approach.
- Within a step, inspect, act, and verify using the available tools.
- Do not make broad speculative edits.
- Prefer the smallest patch that can test the current hypothesis.
- After an edit, inspect the diff and run the narrowest useful validation.
- If a hypothesis is disproved, record that it is disproved and do not repeat it.

COMMUNICATION
- Stream short activity updates, not long internal monologues.
- Explain the next action and its reason in one or two sentences.
- Report important discoveries, risks, changed files, and validation results.
- Do not reveal hidden chain-of-thought. Provide concise, auditable work notes.
- Ask for approval only when the Runtime marks the action as requiring it.

FAILURE AND LOOP CONTROL
- If the same command or equivalent action fails repeatedly, stop and change
  strategy or report the blocker.
- Never retry an unchanged failing action more than the Runtime retry budget.
- If the task scope grows materially, update the plan before continuing.
- If unsure, inspect or ask; do not guess and edit broadly.

OUTPUT CONTRACT
- During execution, emit one short activity message before a meaningful tool call.
- At completion, report: result, files changed, validation, remaining risks,
  and recommended next action.
```

### 5.3 中文本地化 Prompt

Prompt 的规则语义保持英文或内部 canonical 版本，用户可见的 activity 文案根据 locale 生成。这样可以避免中英文 Prompt 漂移。中文 UI 文案不是靠 Prompt 翻译，而是事件 renderer 根据 message key 渲染。

### 5.4 Activity 输出约束

模型不要输出长篇“思考过程”。要求输出短消息：

```json
{
  "kind": "activity",
  "text": "先检查失败测试和 repository 接口，暂不修改文件。",
  "reason": "locate_failure",
  "risk": "read_only"
}
```

如果供应商只返回普通文本，Runtime 通过结构化 response parser 提取；解析失败时把它作为普通 assistant 文本，不把文本误认为状态事件。

---

## 6. Tool Call 协议

### 6.1 工具不是自由文本

模型只能发出结构化调用：

```json
{
  "type": "tool_call",
  "call_id": "call_01H...",
  "name": "read",
  "arguments": {
    "path": "src/auth/login.ts",
    "start_line": 1,
    "end_line": 160
  }
}
```

Runtime 必须使用 JSON Schema 校验参数。未知字段默认拒绝，不要静默忽略模型错误。

### 6.2 Tool Result

```json
{
  "type": "tool_result",
  "call_id": "call_01H...",
  "status": "success",
  "content": {
    "kind": "text",
    "text": "..."
  },
  "meta": {
    "duration_ms": 84,
    "truncated": false,
    "workspace_revision": "agent-change-set:7"
  }
}
```

错误：

```json
{
  "type": "tool_result",
  "call_id": "call_01H...",
  "status": "error",
  "error": {
    "code": "PATH_OUTSIDE_WORKSPACE",
    "message": "The requested path is outside the selected workspace.",
    "retryable": false,
    "hint": "Use a path relative to the workspace."
  }
}
```

### 6.3 第一版工具 Schema

#### explore

```json
{
  "name": "explore",
  "description": "Inspect workspace structure or search files and text.",
  "input_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "mode": {"enum": ["tree", "files", "text", "git_status"]},
      "query": {"type": "string"},
      "path": {"type": "string", "default": "."},
      "max_results": {"type": "integer", "default": 100}
    },
    "required": ["mode"]
  }
}
```

#### read

```json
{
  "name": "read",
  "description": "Read a text file range inside the workspace.",
  "input_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "path": {"type": "string"},
      "start_line": {"type": "integer", "minimum": 1},
      "end_line": {"type": "integer", "minimum": 1},
      "max_chars": {"type": "integer", "default": 30000}
    },
    "required": ["path"]
  }
}
```

#### edit

V1 不提供任意 `write_file`。只提供基于版本校验的 patch：

```json
{
  "name": "edit",
  "description": "Apply a minimal patch after verifying the expected file version.",
  "input_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "path": {"type": "string"},
      "expected_hash": {"type": "string"},
      "patch": {"type": "string"},
      "summary": {"type": "string"}
    },
    "required": ["path", "expected_hash", "patch", "summary"]
  }
}
```

执行顺序：路径检查 → 文件 hash 校验 → patch dry-run → 风险评估 → 审批 → 应用 → 生成 diff → 写入 change-set → 返回结果。

#### exec

```json
{
  "name": "exec",
  "description": "Run a command in the workspace under the active policy.",
  "input_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "command": {"type": "string"},
      "cwd": {"type": "string", "default": "."},
      "timeout_ms": {"type": "integer", "default": 120000},
      "max_output_chars": {"type": "integer", "default": 30000},
      "purpose": {"type": "string"}
    },
    "required": ["command", "purpose"]
  }
}
```

`purpose` 是审计字段，不是安全凭证。真正的命令解析、风险分类和沙箱由 Runtime 完成。

#### web_search

```json
{
  "name": "web_search",
  "description": "Search public web information when local evidence is insufficient.",
  "input_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "query": {"type": "string"},
      "domains": {"type": "array", "items": {"type": "string"}},
      "max_results": {"type": "integer", "default": 5}
    },
    "required": ["query"]
  }
}
```

Runtime 在发送前检查 secret 泄露；结果带 URL、标题、摘要和来源时间，不把网页 HTML 原样塞进上下文。

#### inspect

```json
{
  "name": "inspect",
  "description": "Inspect current task, plan, changes, validation, usage, or memory.",
  "input_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "what": {"enum": ["task", "plan", "changes", "validation", "usage", "memory", "queue"]}
    },
    "required": ["what"]
  }
}
```

### 6.4 Tool 生命周期

```text
proposed
  → schema_validated
  → policy_checked
  → approval_pending (可选)
  → started
  → streaming
  → completed / failed / cancelled
```

任何非终态的 Tool 在进程重启后都必须被标记为 `unknown`，不能直接假设失败或成功。对于 `exec`，使用 process identity 和启动记录判断是否可回收；无法判断时不自动重试，交给用户或安全恢复策略。

### 6.5 工具结果截断

- 原始结果归档到磁盘。
- 给模型的结果按 token 和字符双重限制。
- 错误行、diff、测试失败行优先保留。
- UI 显示“已截断，按 e 展开”而不是假装完整。

---

## 7. Agent Loop

### 7.1 主循环

```text
接收用户输入
  ↓
归类为当前任务插入 / 新任务 / 队列项
  ↓
读取 Runtime state + 相关记忆
  ↓
构造 Context Manifest
  ↓
请求模型
  ↓
流式渲染文本和活动
  ↓
若无 tool call：进入完成/继续判断
  ↓
若有 tool call：校验、策略判断、执行
  ↓
写入 tool result 和指标
  ↓
检查中断、预算、循环和上下文
  ↓
继续模型请求 / 更新计划 / 暂停 / 完成
```

### 7.2 单步预算

默认：

```text
单 Task 最多 60 次 tool call
同一等价动作最多 3 次
单命令最多 120 秒
单次输出最多 30k 字符给模型
单 Task 默认最长 30 分钟
```

达到预算不是异常，而是 `task.blocked`，需要用户决定是否继续增加预算。

### 7.3 等价动作指纹

```text
fingerprint = hash(tool_name + normalized_arguments + relevant_workspace_revision)
```

同一命令、同一文件同一区域的重复修改、同一错误输出对应的重复尝试均计入循环检测。参数中时间戳等噪声要归一化。

---

## 8. 插入与队列协议

### 8.1 Inject

```json
{
  "id": "msg_01H...",
  "kind": "inject",
  "task_id": "task_current",
  "text": "先不要改 auth.ts，给我看当前 diff",
  "created_at": "...",
  "status": "pending"
}
```

消费点：

- 当前工具完成后。
- 当前模型请求完成后。
- 用户强制停止时。

插入不强行终止不可中断命令；它在下一个 safe point 进入模型上下文，并生成 `conversation.injected`。

### 8.2 Queue

```json
{
  "id": "queue_01H...",
  "session_id": "ses_01H...",
  "text": "修复完成后补一个回归测试",
  "position": 1,
  "status": "queued",
  "depends_on_task_id": "task_current"
}
```

队列任务启动前：

1. 检查前一任务状态。
2. 读取最新 workspace revision。
3. 创建检查点。
4. 重新生成任务计划。
5. 继承 Session 配置，但使用当时的模型和策略快照。

### 8.3 Slash Commands

```text
/model                 打开动态模型选择器
/approval              打开放行策略选择器
/reasoning             打开推理强度选择器
/queue                 查看队列
/inject                显式将消息插入当前任务
/status                显示紧凑状态
/plan                  展开当前计划
/diff                  展开最近变更
/compact               请求上下文压缩
/pause                 暂停
/stop                  停止
```

模型、放行策略和推理强度切换都在下一个模型请求生效，不切断当前请求。

---

## 9. Context Manifest 与压缩

每次模型请求前先生成 manifest：

```json
{
  "system_prompt_version": "fun-v1",
  "workspace": {"root": "/workspace", "revision": "agent-change-set:7"},
  "task": {"goal": "...", "active_step": "..."},
  "plan_revision": 3,
  "constraints": ["..."],
  "memory_ids": ["mem_1", "mem_2"],
  "recent_event_range": [1810, 1842],
  "included_artifacts": ["artifact_diff_7", "artifact_test_4"],
  "estimated_tokens": 48200
}
```

压缩结果必须可追溯到 source event seq。压缩不是删除历史，只是改变下一次模型请求的视图。

### 9.1 压缩摘要格式

```text
TASK GOAL:
CONSTRAINTS:
CURRENT PLAN:
VERIFIED FACTS:
REJECTED HYPOTHESES:
CHANGES MADE BY FUN:
VALIDATION:
BLOCKERS:
QUEUED WORK:
NEXT SAFE ACTION:
```

### 9.2 压缩并发锁

同一 Session 同时只能有一个 compaction job。压缩期间：

- 可以继续接收消息，但标为 pending。
- 不启动新的模型请求。
- 当前工具完成后再压缩。
- 成功写入新 context snapshot 后释放锁。
- 失败保留旧 snapshot，不破坏可恢复状态。

---

## 10. 自动 Memory Pipeline

Memory 自动写入，但必须来源可追踪。

```text
事件
  ↓
候选抽取器
  ↓
事实标准化
  ↓
作用域判断
  ↓
置信度和新鲜度评分
  ↓
冲突检测
  ↓
写入或更新
```

候选来源优先级：

```text
项目文件 / 命令结果 > 重复验证 > 模型判断 > 一次性自然语言推断
```

模型不能直接调用 `memory.write`。Memory Extractor 是 Runtime 内部服务，不是模型工具。

---

## 11. Provider Adapter 与 Usage

统一内部接口：

```ts
interface ModelAdapter {
  discoverModels(connection): Promise<ModelDescriptor[]>
  stream(request): AsyncIterable<ModelEvent>
  normalizeUsage(raw): Usage
  mapReasoningEffort(level, capabilities): ProviderParams
}
```

统一 ModelEvent：

```text
text_delta
reasoning_delta
tool_call_delta
tool_call_complete
usage
completed
error
```

统一 Usage：

```json
{
  "input_tokens": 12480,
  "output_tokens": 2104,
  "reasoning_tokens": null,
  "cached_input_tokens": 0,
  "total_tokens": 14584,
  "precision": "exact",
  "ttft_ms": 1200,
  "generation_ms": 11900
}
```

供应商没有精确 usage 时：

```json
{"precision":"estimated"}
```

UI 必须显示 `~`。

---

## 12. 终端 UI 实现规格

### 12.1 默认布局

```text
┌─ Fun · <task title> ────────────────────────────────────────────────┐
│ ● running · step 2/4 · 61% · 00:42                         [Tab]    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│ 你：帮我修复登录测试                                                 │
│                                                                     │
│ ◇ 计划                                                               │
│   ✓ 扫描项目                                                         │
│   ● 定位失败                                                         │
│   ○ 修改实现                                                         │
│   ○ 验证                                                             │
│                                                                     │
│ ◌ 正在检查 src/auth/login.ts                                        │
│   检查 token 校验和错误处理……                                       │
│                                                                     │
│ ! 发现：mock 使用了旧版 repository 接口                              │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│ Enter 继续  Tab 状态  Space 暂停  Ctrl-C 停止  ? 帮助                │
└─────────────────────────────────────────────────────────────────────┘
```

### 12.2 不再使用持续 Working Summary 卡片

用三类轻量内容替代：

- `◌` 当前活动：正在做什么。
- `!` 关键发现：有什么重要事实或风险。
- `◇` 计划/结论：任务结构和最终结果。

这样更符合终端线性阅读，也减少 UI 重绘。

### 12.3 宽度适配

- 80–99 列：紧凑单栏，工具结果默认折叠。
- 100–119 列：标准单栏，计划显示完整标题。
- 120+ 列：增加右侧指标文字，但不默认启用桌面式双栏。
- 任何宽度下都可通过 `/status` 或 `Tab` 查看完整状态。

### 12.4 颜色和无色降级

浅蓝色只用于标题、活动、边界和当前选择。每个颜色都必须有符号或文字后备：

```text
◌ active   ✓ success   ! warning   × error   ○ pending
```

### 12.5 工具调用显示

默认：

```text
◌ read src/auth/login.ts  ·  84ms
```

展开：

```text
▶ read src/auth/login.ts
  lines 1–160 · 4.2k chars · read-only
```

命令默认：

```text
◌ exec pnpm test auth  ·  running
```

完成：

```text
✓ exec pnpm test auth  ·  8 passed · 2.4s
```

高风险确认：

```text
? 需要确认：将修改 3 个文件（+82 -31）
  [y] 继续  [n] 取消  [d] 查看 diff
```

---

## 13. 第一版实现顺序

### Phase 1：协议和最小 Runtime

- Event envelope。
- Session / Task / Turn。
- 单 workspace。
- `read`、`explore`、`edit`、`exec`。
- 单栏流式 UI。
- 基础检查点和停止。

### Phase 2：模型 Gateway

- OpenAI-compatible。
- Anthropic。
- 动态模型发现。
- Usage 归一化。
- 推理强度映射。
- `/model`。

### Phase 3：任务能力

- Plan create/update。
- ReAct 工具循环。
- 循环预算。
- Diff 和 validation。
- `/plan`、`/diff`、`/status`。

### Phase 4：恢复能力

- Session replay。
- Context Manifest。
- Compaction。
- 自动 Memory。
- Inject / Queue。

### Phase 5：体验打磨

- 中英文 i18n。
- 浅蓝色主题。
- 终端宽度适配。
- 断线恢复。
- usage 详情和性能指标。

---

## 14. V1 验收问题

任何实现 PR 都必须回答：

1. 工具执行到一半进程崩溃，重启如何判断状态？
2. UI 断线后如何从 `seq` 恢复？
3. 模型切换发生在当前请求中间时如何处理？
4. 用户插入消息时当前命令还在运行，消息何时生效？
5. 队列任务如何确认前一个任务的 workspace revision？
6. patch 应用前文件被外部修改，是否会覆盖？
7. context compaction 失败是否会破坏旧上下文？
8. provider 没有精确 token usage 时 UI 如何标记？
9. 同一失败动作重复三次后是否会停止？
10. Auto 模式是否仍然无法绕过 workspace 和 critical 边界？

如果这些问题没有明确答案，就不应继续堆功能。
