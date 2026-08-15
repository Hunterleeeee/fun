# Fun Harness 产品设计文档

> **Coding should feel good.**  
> **让写代码变得有意思。**

- 文档版本：0.1
- 产品状态：概念设计 / 第一版基础设计
- 目标形态：终端优先的 Coding Agent Runtime
- 目标用户：希望开箱即用，同时保留深度定制能力的个人开发者与小型团队

---

## 1. 产品定义

Fun 是一个开箱即用、面向复杂 Coding 任务的终端 Agent Runtime。它以终端为入口，以单栏流式工作台为交互界面，以 **Plan and Execute + 局部 ReAct** 为任务执行模式，并提供事件事实源、工具审计、工作区边界和可恢复变更。自动记忆、上下文压缩和完整会话恢复按 V1/V1.x 边界逐步交付。

Fun 不追求第一版“什么都能做”，而追求一条基础链路足够可靠：

```text
启动 Fun
  → 配置连接与语言
  → 选择 workspace
  → 输入任务
  → 生成计划
  → 流式执行
  → 查看工具过程与结构化工作摘要
  → 审阅 diff
  → 运行验证
  → 保存记忆与会话
  → 随时恢复、插入或排队新对话
```

### 1.1 产品承诺

1. **开箱即用**：首次运行有引导，不要求用户先写配置文件。
2. **开放可折腾**：高级用户可以自定义 URL、模型、策略、主题、扩展和规则。
3. **复杂任务可推进**：任务有目标、计划、步骤、假设、证据和验证结果。
4. **过程透明**：流式展示活动行、工具调用、命令结果、变更和指标。
5. **随时接管**：V1 支持暂停、停止、切换模型、切换放行策略和推理强度；Inject / Queue 按 V1.x 交付。
6. **安全有边界**：自动模式也不能绕过 workspace 边界、敏感路径和不可逆操作保护。
7. **事实自动沉淀**：V1 自动保存有证据的 workspace/task facts；复杂跨项目记忆按 V1.x 交付。

---

## 2. 目标与非目标

### 2.1 第一版目标

- 支持 REPL 与一次性任务入口。
- 首发优先支持 OpenAI-compatible；Anthropic 作为 V1.x 接入，内部协议预留统一适配层。
- 首发允许用户依据 URL、Key 和手动 Model ID 建立连接；动态模型拉取作为 V1.x 能力，不以静态模型列表冒充真实可用性。
- 支持中文、英文，并从第一天建立 i18n 基础。
- 支持 Ask、Smart、Auto 三种放行策略。
- 支持 Low / Medium / High / Auto 推理强度。
- 支持 Plan and Execute + ReAct。
- 支持结构化工作摘要，而不是依赖原始隐藏思维链。
- 支持低风险、证据驱动的 workspace/task facts；提供 token 预警和人工 `/compact`，自动压缩与复杂 Memory 属于 V1.x。
- 支持 token、输入输出、耗时、首字延迟、速度、工具耗时等指标。
- 支持单栏终端 UI 和全链路流式事件；宽屏只增加信息密度，不默认固定双屏。
- V1 支持暂停、停止和恢复；Inject 与 Queue 作为 V1.x 的 safe-point 能力预留。
- 提供少量但高质量的默认工具。

### 2.2 第一版非目标

- 多 Agent 协作。
- 多真人实时共享同一会话。
- IDE 插件和 Web 控制台。
- 自动部署生产环境。
- 远程执行和云端 workspace。
- 插件市场。
- 复杂向量数据库记忆检索。
- 让模型直接执行不可审计的任意代码。

---

## 3. 核心用户体验

### 3.1 用户爽点排序

#### P0：Agent 真的在工作

输入任务后不能长时间黑屏。用户应立即看到：

```text
收到任务
正在扫描 workspace
正在建立计划
正在检查相关测试
```

#### P0：复杂任务有进度

用户始终知道：

- 当前任务是什么。
- 计划完成到第几步。
- 当前正在验证什么假设。
- 下一步是什么。
- 已改了哪些文件。
- 测试是否变好。

#### P0：用户随时可以接管

在 Agent 工作时，用户可以：

- 发送一条消息插入当前对话。
- 发送一条新任务排队等待。
- 暂停或停止任务。
- 改模型。
- 改放行规则。
- 改推理强度。
- 查看当前 diff。

#### P1：结果透明且可恢复

每次修改都有 patch、diff、验证结果和检查点。失败时可以恢复到 Agent 自己的变更之前，不破坏用户原有修改。

#### P1：性能与消耗透明

显示输入输出 token、推理 token（如果供应商提供）、上下文占用、首字延迟、输出速度、工具耗时和总耗时。

#### P2：越用越顺

自动捕获项目事实、任务事实、已证伪假设和稳定用户偏好，减少重复解释。

---

## 4. 产品信息架构

```text
Fun CLI
├── Onboarding
│   ├── Language
│   ├── Safety acknowledgement
│   ├── Model connection
│   ├── Model discovery
│   ├── Model selection
│   ├── Approval policy
│   └── Workspace selection
├── Workspace shell
│   ├── Process pane
│   ├── Information pane
│   ├── Composer
│   └── Slash command palette
├── Runtime
│   ├── Session
│   ├── Task
│   ├── Plan
│   ├── ReAct loop
│   ├── Queue
│   ├── Interrupt channel
│   ├── Memory
│   ├── Context manager
│   ├── Model gateway
│   ├── Tool runtime
│   ├── Policy engine
│   ├── Workspace guard
│   ├── Checkpoint manager
│   └── Event store
└── Persistence
    ├── Connections
    ├── Sessions
    ├── Tasks
    ├── Events
    ├── Memories
    └── Checkpoints
```

---

## 5. Runtime 设计

Runtime 是 Fun 的核心，不是一个 Prompt 加 while 循环。

### 5.1 Runtime 分层

```text
┌──────────────────────────────────────────┐
│              Fun Agent Runtime            │
├──────────────────────────────────────────┤
│ Session Manager                          │
│ Task / Plan Manager                      │
│ Conversation Router                      │
│ Queue & Interrupt Manager                │
│ Memory Manager                           │
│ Context Manager                          │
│ Model Gateway                            │
│ Tool Runtime                             │
│ Policy Engine + Workspace Guard          │
│ Checkpoint / Recovery Manager             │
│ Event Store + Metrics                    │
└──────────────────────────────────────────┘
```

### 5.2 事件驱动

Runtime 的所有重要状态变化产生结构化事件，UI 只消费事件，不从自然语言猜状态。

```text
session.created
onboarding.completed
workspace.opened
task.created
plan.created
plan.updated
step.started
step.completed
working_summary.created
model.request.started
model.delta
model.request.completed
tool.requested
tool.started
tool.delta
tool.completed
approval.requested
approval.resolved
conversation.interrupted
conversation.queued
file.patch.proposed
file.changed
diff.created
checkpoint.created
validation.started
validation.completed
context.compaction.started
context.compacted
memory.extracted
memory.updated
task.paused
task.completed
task.failed
```

事件需要持久化。终端 UI、历史恢复、上下文压缩、审计和未来其他 UI 都使用同一事件源。

### 5.3 Session、Task、Turn 的关系

```text
Session
├── workspace
├── connection / active model
├── approval policy
├── reasoning effort
├── conversation stream
└── Tasks
    ├── Task A
    │   ├── Plan
    │   ├── Steps
    │   ├── Evidence
    │   ├── Changes
    │   └── Validation
    └── Task B (queued)
```

- **Session**：用户和 Fun 的长期工作空间。
- **Task**：一个明确目标，例如“修复登录测试”。
- **Turn**：一次用户输入或 Agent 响应循环。
- **Event**：所有可追踪的运行事实。

---

## 6. Plan and Execute + ReAct

### 6.1 分工

```text
Task
  ↓
Planner：建立和维护任务计划
  ↓
Executor：执行当前计划步骤
  ↓
ReAct：在一个步骤内观察、行动、验证、调整
```

Plan 负责宏观推进，ReAct 负责局部灵活性。计划不是锁死的流程，执行中可以增删和重新排序步骤，但所有计划变化都要通过事件展示。

### 6.2 计划数据结构

```json
{
  "goal": "修复支付创建接口测试失败",
  "constraints": ["不修改 .env", "不新增依赖"],
  "steps": [
    {"id":"s1", "title":"理解项目结构", "status":"completed"},
    {"id":"s2", "title":"定位失败测试", "status":"completed"},
    {"id":"s3", "title":"修复实现", "status":"active"},
    {"id":"s4", "title":"运行验证", "status":"pending"}
  ],
  "current_step": "s3",
  "revision": 2
}
```

### 6.3 ReAct 单步循环

```text
Observe：读取工具结果、错误、文件状态
  ↓
Summarize：生成结构化工作摘要
  ↓
Hypothesize：提出当前假设
  ↓
Act：调用一个工具或更新计划
  ↓
Validate：检查结果、变更和风险
  ↓
Continue / Re-plan / Pause
```

每个假设需要记录：

```text
假设内容
依据
验证动作
结果
是否已证伪
```

保存已证伪假设是防止上下文压缩后重复试错的关键。

---

## 7. 对话插入与队列

这是 Fun 相对于普通 Agent 的重要交互能力。

### 7.1 两种输入通道

#### 插入（Interrupt / Inject）

插入是对当前运行任务的即时干预，目标是改变当前步骤或补充上下文。

例子：

```text
用户：先不要改 auth.ts，先给我看当前发现。
用户：这个项目不能新增依赖。
用户：改用中文解释。
```

语义：尽可能在当前安全边界内，于下一个可中断点消费。正在执行的不可中断系统调用不强行切断，命令结束后立即处理。

UI 状态：

```text
[Injected] 将在当前工具完成后处理
```

插入消息默认进入当前 Task，不创建新 Task。

#### 队列（Queue）

队列是用户提前提交的后续任务，不打断当前任务。

例子：

```text
用户：当前修复完成后，再帮我补一个回归测试。
用户：然后更新 README。
```

队列项拥有独立状态：

```text
queued → ready → running → completed / failed / cancelled
```

队列任务默认继承 workspace、Session、记忆和安全策略，但在开始前重新检查 workspace 状态和前一任务的变更。

### 7.2 输入识别

建议提供显式快捷方式，避免语义歧义：

- `Ctrl+Enter`：插入当前任务。
- `Alt+Enter`：加入任务队列。
- `/queue`：查看、重排、取消队列。
- `/inject`：将当前输入标记为插入。

在普通输入模式下，用户也可以自然语言表达，但 UI 应在提交前显示目标：

```text
Send as: [Inject current task] [Queue next task]
```

### 7.3 队列管理

```text
/queue
/queue list
/queue move 3 1
/queue cancel 2
/queue run-now 2
/queue clear
```

右侧信息栏显示：

```text
Queue 2
1. Add regression test      waiting
2. Update README             waiting
```

### 7.4 一致性规则

- 插入不能覆盖用户已经明确确认的安全边界。
- 队列任务开始前重新读取当前 git 状态。
- 队列任务不得假设前一个任务一定成功，必须检查前置状态。
- 队列任务之间创建检查点。
- 如果前一任务失败，后续任务默认暂停并显示原因。
- 用户可以选择“继续队列”或“暂停队列”。

---

## 8. Memory 设计

Memory 完全由 Runtime 自动抓取，不要求用户填写，也不要求每次写入时手动确认。

### 8.1 记忆类型

```text
Session Memory       当前会话事实
Task Memory          当前任务状态、证据、假设
Workspace Memory     项目长期事实
User Memory          跨项目稳定偏好
Negative Memory      已证伪假设和不可重复路线
```

### 8.2 自动提取流程

```text
Event Store
  ↓
Memory Extractor
  ↓
候选事实
  ↓
去重 / 冲突检测 / 重要性评分 / 作用域判断
  ↓
Memory Store
```

记忆对象：

```json
{
  "id": "mem_123",
  "content": "项目使用 pnpm 作为包管理器",
  "scope": "workspace",
  "source": "package-manager-detection",
  "confidence": 0.98,
  "status": "active",
  "created_at": "...",
  "last_used_at": "..."
}
```

### 8.3 自动记忆的质量控制

系统自动写入，但需要：

- 证据来源。
- 置信度。
- 作用域。
- 时间戳。
- 冲突检测。
- 过期和淘汰策略。

临时推断不能直接变成永久项目事实。稳定事实优先来自项目文件、命令结果、重复验证和明确用户输入。

### 8.4 用户可见但不被打扰

用户不需要参与写入过程，但可查看和删除：

```text
/memory
/memory search pnpm
/memory forget mem_123
```

UI 只需显示：

```text
Workspace memory: 12 facts
```

展开后查看明细。

---

## 9. 上下文管理与压缩

### 9.1 四层数据

```text
当前上下文：直接提供给模型
任务状态：结构化目标、计划、证据和约束
事件日志：完整事实和审计
原始归档：完整工具输出、命令日志和 diff
```

所有历史不等于记忆；所有记忆不等于当前上下文。

### 9.2 压缩触发

- 达到模型上下文窗口的 70%：预警。
- 达到 80%：自动准备压缩。
- 达到 85%：执行压缩或由模型完成当前安全步骤后压缩。
- 用户可通过 `/compact` 手动触发。
- 切换到上下文窗口较小的模型时重新计算阈值。

### 9.3 压缩保留内容

```text
用户目标
明确约束
当前计划
已验证事实
已证伪假设
已修改文件和关键 diff
测试结果
当前阻塞点
队列任务
重要用户偏好
```

原始日志继续落盘，不全部塞回模型。压缩后产生 `context.compacted` 事件并创建检查点。

---

## 10. Model Gateway

### 10.1 协议支持

第一版支持：

- OpenAI-compatible。
- Anthropic native。

以后可增加：

- 本地模型。
- OAuth / device login。
- 厂商原生适配器。

### 10.2 动态模型发现

模型不能使用静态列表作为主流程。流程是：

```text
用户填写协议、API URL、Key / Token
  ↓
测试连接
  ↓
拉取模型列表
  ↓
探测模型能力
  ↓
展示真实可用模型
  ↓
用户选择当前模型
```

如果 endpoint 不支持模型列表，提供手动填写 Model ID 的 fallback。

### 10.3 能力声明

```text
supports_tools
supports_streaming
supports_reasoning
supports_vision
supports_prompt_cache
supports_parallel_tool_calls
context_window
max_output_tokens
usage_precision
```

### 10.4 随时切换模型

```text
/model
/model list
/model <id>
```

当前请求不会被硬切断。切换默认在下一个模型请求生效；正在运行的工具完成后，下一次 Agent 请求使用新模型。切换后重新检查工具、上下文窗口和推理能力。

### 10.5 推理强度

```text
/reasoning auto
/reasoning low
/reasoning medium
/reasoning high
```

Runtime 统一抽象为：

```text
reasoning_effort = auto | low | medium | high
```

由 Adapter 映射到不同供应商的参数。模型不支持时降级为结构化工作摘要，并明确提示。

---

## 11. 默认工具集

第一版工具保持克制：

1. `explore`：目录、文件搜索、内容搜索、git 状态和项目概况。
2. `read`：带行号读取文件片段，处理大文件和二进制拒绝。
3. `edit`：基于旧内容校验的 patch 修改，自动生成 diff。
4. `exec`：受 Policy Engine、Workspace Guard、超时和输出限制保护的命令执行。
5. `web_search`：外部文档和最新事实检索。
6. `inspect`：查看任务、计划、变更、记忆、指标和运行状态。

`inspect` 既可作为模型工具，也可作为 UI / slash command 的 Runtime 查询入口。

### 11.1 web_search 规则

- 默认提供。
- 优先在本地信息不足、需要官方最新文档或版本信息时使用。
- 搜索词中检测潜在密钥、密码、私有代码并阻断或脱敏。
- 搜索结果作为参考，不自动视为可信事实。
- 网页内容不得直接变成 shell 命令。
- 网络策略独立于文件 workspace 策略。

---

## 12. 安全和放行策略

### 12.1 三种模式

| 模式 | 行为 |
|---|---|
| Ask | 所有有副作用的操作都询问 |
| Smart | 低风险自动，高风险询问，中风险由策略判断 |
| Auto | 不打扰普通操作，但硬安全规则持续生效 |

### 12.2 永久硬边界

- 不允许访问 workspace 外文件。
- 不允许通过符号链接逃逸。
- 不允许修改 Fun 自身安全策略。
- 不允许永久不可恢复删除。
- 不允许绕过审计和检查点。
- 敏感文件和密钥路径默认保护。
- 命令必须受超时、输出和循环预算限制。

### 12.3 随时切换放行策略

```text
/approval
/approval ask
/approval smart
/approval auto
```

切换立即影响后续动作，不追溯已经发出的操作。状态栏实时显示当前策略。

---

## 13. 指标与可观测性

### 13.1 Runtime 统一 usage

```json
{
  "input_tokens": 12480,
  "output_tokens": 2104,
  "reasoning_tokens": 8200,
  "cached_input_tokens": 0,
  "total_tokens": 14584,
  "precision": "exact"
}
```

支持 `exact` 与 `estimated`，估算值必须在 UI 中使用 `~` 标识。

### 13.2 时间与速度

```text
ttft_ms                 首个输出时间
generation_ms           模型生成耗时
output_tokens_per_sec   输出速度
tool_time_ms            工具总耗时
approval_wait_ms        等待用户确认时间
total_elapsed_ms        总耗时
```

### 13.3 UI 展示层级

状态栏简洁展示：

```text
DeepSeek V4 · High · 38 tok/s · 18.6s
```

详情面板展示：

```text
Input       12.4k
Output       2.1k
Reasoning    8.2k
TTFT         1.2s
Generation  11.9s
Tools        6.7s
Total       18.6s
```

任务完成展示：

```text
Duration: 2m 41s · 86.4k in · 14.8k out · 23 tool calls · 4 files changed
```

指标的作用是反馈透明、消耗透明和性能透明，而不是用数字制造噪音。

---

## 14. 单栏终端 UI（已调整）

早期设想的固定左右双屏在 80–100 列终端中会导致路径、工具输出和自然语言频繁换行，不适合作为默认界面。Fun V1 改为：

```text
单栏流式正文
+ 一行可折叠运行状态
+ 可展开计划/任务状态
+ 底部极简快捷键
```

宽屏只增加信息密度，不默认切换成桌面式双栏。完整设计见 [`docs/fun-runtime-spec.md`](fun-runtime-spec.md)。

### 14.1 默认结构

```text
┌─ Fun · task title ────────────────────────────────────────────────┐
│ ● running · step 2/4 · 61% · 00:42                    [Tab] status │
├───────────────────────────────────────────────────────────────────┤
│ YOU                                                               │
│ 帮我修复登录测试                                                    │
│                                                                   │
│ ◇ PLAN                                                            │
│   ✓ 扫描项目                                                       │
│   ● 定位失败                                                       │
│   ○ 修改实现                                                       │
│   ○ 验证                                                           │
│                                                                   │
│ ◌ 正在检查 src/auth/login.ts                                      │
│   检查 token 校验和错误处理……                                     │
│                                                                   │
│ ! 发现：mock 使用了旧版 repository 接口                            │
├───────────────────────────────────────────────────────────────────┤
│ Enter 继续  Tab 状态  Space 暂停  Ctrl-C 停止  ? 帮助              │
└───────────────────────────────────────────────────────────────────┘
```

`Working Summary` 不再作为持续刷新的信息卡片。它被拆成三种轻量事件：

- `◌ ACTIVE`：当前正在做什么。
- `! FINDING`：重要发现、风险或阻塞。
- `◇ PLAN / RESULT`：计划和最终结论。

### 14.2 视觉语言

- 主色：浅蓝色。
- 深色背景：降低长时间终端使用疲劳。
- 蓝色：活动、当前步骤、可交互元素。
- 绿色：成功和完成。
- 黄色：等待确认、警告、队列。
- 红色：失败、阻断、高风险。
- 灰色：历史和低优先级细节。

建议色板：

```text
Primary      #7DD3FC
Accent       #38BDF8
Soft Blue    #E0F2FE
Background   #0B1220
Panel        #111C2E
Success      #86EFAC
Warning      #FDE68A
Danger       #FDA4AF
```

### 14.3 流式原则

Runtime 事件一产生，UI 立即渲染：

- 模型 delta 流式追加到单栏正文。
- 工具启动立即显示活动行。
- 命令输出按行或按块流式显示。
- 状态栏和 `/status` 的指标实时更新。
- 大段输出默认折叠，但不丢失。
- diff 以卡片形式显示，可展开。

### 14.4 工作摘要

不承诺展示原始隐藏思维链。Fun 展示结构化、可审计的工作摘要：

```text
目标：定位订单创建测试失败
观察：UserRepository mock 返回 undefined
假设：实现调用了旧版 find 方法
行动：检查 repository 接口和 mock
风险：只读操作，不会修改文件
```

---

## 15. 首次启动与配置流程

```text
fun
  ↓
Fun Harness 欢迎页
  ↓
语言选择：简体中文 / English
  ↓
风险说明并同意
  ↓
选择协议：OpenAI-compatible / Anthropic
  ↓
填写 API URL、Key / Token
  ↓
测试连接并拉取模型
  ↓
选择真实模型
  ↓
选择默认推理强度
  ↓
选择默认放行策略
  ↓
选择 workspace
  ↓
显示 workspace 风险摘要并确认
  ↓
扫描项目并进入工作区
```

全局连接保存凭证引用，Key / Token 放系统密钥存储；workspace 配置只引用 `connectionId`，不把秘密写进项目目录。

---

## 16. 国际化

第一版支持：

- `zh-CN`
- `en-US`

所有用户可见文案必须使用翻译 key，不得散落硬编码。资源结构：

```text
locales/
├── zh-CN/messages.json
└── en-US/messages.json
```

提前预留：

- 文案长度变化。
- 中英文混排宽度计算。
- 日期、数字、token 格式。
- 快捷键和帮助文案。
- 右侧状态栏截断策略。
- 未来繁体中文、日文、韩文等 locale。

---

## 17. Slash Command 控制面

第一版：

```text
/help
/model
/approval
/reasoning
/status
/plan
/diff
/memory
/queue
/compact
/pause
/stop
/resume
/settings
/quit
```

输入 `/` 时显示可搜索命令面板；命令参数尽量使用选择器，不要求用户记住复杂语法。

---

## 18. 持久化模型

建议使用本地 SQLite 或等价的事务型存储，至少包含：

```text
connections
sessions
tasks
plans
turns
events
memories
checkpoints
file_changes
usage_records
queue_items
```

原始工具输出和大文件日志可以单独以内容寻址文件存储，数据库保存引用和摘要。

---

## 19. 第一版验收标准

### 启动和配置

- `fun` 能进入欢迎页。
- 首次运行可以选择中英。
- 填写 URL 和 Key 后能测试连接。
- V1 允许手动 Model ID；动态拉取模型并显示能力属于 V1.x，失败时必须明确提示，不使用静态列表伪装可用性。
- endpoint 不支持模型列表时能手动填写模型 ID。
- 能选择 workspace 并看到风险摘要。

### 任务执行

- 能建立计划并逐步执行。
- 计划执行过程中可以局部 ReAct。
- 单栏流式显示过程；宽屏只增加信息密度，不默认固定双屏。
- `/status` 或可折叠状态区域显示 workspace、任务、上下文和 usage。
- 工具调用、命令结果和 diff 可展开。

### 控制

- `/model` 可以切换模型。
- `/approval` 可以切换放行策略。
- `/reasoning` 可以切换推理强度。
- V1 支持 `/pause`、`/stop` 和基础 `/resume`。
- Inject 与 Queue 的命令、重排和取消属于 V1.x。
- `/stop` 能安全停止。

### 记忆和恢复

- 自动生成有证据的 workspace/task facts；复杂 negative memory 生命周期属于 V1.x。
- V1 提供 token 预警、Context Manifest 和人工 `/compact`；自动压缩属于 V1.x。
- 会话退出后可恢复。
- 检查点和 Agent 自己的变更可恢复。

### 安全

- workspace 外路径拒绝。
- 符号链接逃逸拒绝。
- 受保护文件有明确策略。
- Auto 模式仍阻断 Critical 操作。
- 命令有超时、输出限制和循环保护。

---

## 20. 品牌与 slogan

### 产品名

**Fun**

### 产品展示名

**FUN HARNESS**

### 英文 slogan

> **Coding should feel good.**

### 中文 slogan

> **让写代码变得有意思。**

“有意思”不是把工具做成玩具，而是让复杂工作拥有清晰反馈、持续推进、可控操作和完成后的成就感。

---

## 21. 设计原则总结

```text
少而精，而不是多而杂
流式反馈，而不是黑屏等待
计划推进，而不是盲目循环
自动记忆，而不是要求用户维护
可随时接管，而不是只能等待
默认安全，而不是把风险交给用户
开放可配置，而不是配置先行
UI 是 Runtime 的投影，而不是最后添加的装饰
```
