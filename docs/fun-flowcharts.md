# Fun Harness 核心流程图

> 本文是 Fun Runtime 的流程图资料，不是传统 Web API 的业务事件系统。图中的 API、应用服务和领域逻辑分别对应 CLI 输入层、Task Orchestrator 和 Agent Runtime；事件存储是 Session Event Store，工具执行、审批和模型流都必须经过 Runtime。

> 本文描述 Fun 的 Core Runtime 生命周期。带有 Queue、自动压缩、完整崩溃恢复或 web_search 的图属于 V1.x 设计预留；V1 实现必须只交付文末的 Core 范围。所有状态变化都必须先持久化为事件，再通知终端 UI；所有工具调用都有唯一 `task_id`、`turn_id`、`call_id` 和幂等键。

## 0. 统一约定

终态：

```text
completed / cancelled / rejected / failed / blocked / needs_recovery
```

恢复原则：

1. 已持久化的事实优先于模型叙述。
2. 非终态工具在崩溃后不能直接假定成功或失败。
3. 不确定的外部副作用不自动重试。
4. 所有恢复、压缩、审批、队列切换都有事件记录。

## 1. 全局 Runtime 状态

```mermaid
stateDiagram-v2
    [*] --> uninitialized
    uninitialized --> booting
    booting --> ready: config and storage valid
    booting --> safe_mode: recoverable boot error
    booting --> failed: unrecoverable boot error
    safe_mode --> ready: user repairs config
    failed --> booting: retry

    ready --> planning: new task
    planning --> running: plan accepted
    planning --> waiting_user: needs clarification
    planning --> blocked: policy or capability block

    running --> waiting_approval: tool needs approval
    waiting_approval --> running: approved
    waiting_approval --> blocked: rejected / expired

    running --> compacting: context threshold
    compacting --> running: compaction succeeded
    compacting --> blocked: compaction cannot fit

    running --> paused: user pauses
    paused --> running: resume
    running --> completed: validated result
    running --> failed: unrecoverable error
    running --> cancelled: user stops
    running --> needs_recovery: process or storage crash

    needs_recovery --> running: state reconciled
    needs_recovery --> paused: user review required
    needs_recovery --> failed: cannot reconstruct
```

## 2. 首次启动与 Workspace Onboarding

```mermaid
flowchart TD
    A([fun]) --> B{本地配置存在?}
    B -- 否 --> C[创建默认配置与本地状态库]
    B -- 是 --> D[校验配置、凭证引用、数据库版本]
    D --> E{校验通过?}
    E -- 否 --> F[显示缺失或无效项]
    F --> G{用户修复?}
    G -- 是 --> D
    G -- 否 --> Z1([退出：未完成配置])
    E -- 是 --> H[选择语言 zh-CN / en-US]
    C --> H
    H --> I[显示 Fun Harness 和安全说明]
    I --> J{用户同意?}
    J -- 否 --> Z2([退出：未同意])
    J -- 是 --> K[选择协议 OpenAI-compatible / Anthropic]
    K --> L[填写 URL 与 Key / Token]
    L --> M[测试连接]
    M --> N{连接成功?}
    N -- 否 --> O[显示可操作错误并重试]
    O --> L
    N -- 是 --> P[拉取真实模型列表]
    P --> Q{模型列表可用?}
    Q -- 是 --> R[探测能力并选择模型]
    Q -- 否 --> S[允许手动输入 Model ID]
    S --> R
    R --> T[选择默认 reasoning 与 approval]
    T --> U[选择 workspace]
    U --> V[解析真实路径、扫描 git 和敏感项]
    V --> W[显示 workspace 风险摘要]
    W --> X{用户同意此 workspace?}
    X -- 否 --> U
    X -- 是 --> Y[写入 workspace consent 和配置]
    Y --> AA[扫描项目规则、包管理器、测试入口]
    AA --> AB([进入就绪态])
```

## 3. 一次 Task 执行

```mermaid
flowchart TD
    A([用户提交消息]) --> B[创建 message / task / idempotency key]
    B --> C{当前是否有 active task?}
    C -- 否 --> D[创建新 task]
    C -- 是 --> E{消息类型}
    E -- inject --> F[绑定当前 task，等待 safe point]
    E -- queue --> G[创建 queue item]
    E -- 新任务 --> G
    G --> Z0([持久化并显示排队位置])
    F --> H[当前工具结束或模型回合结束]
    D --> I[解析目标、约束、规模]
    I --> J{是否需要计划?}
    J -- 否 --> K[直接构建回答请求]
    J -- 是 --> L[生成 2-7 步短计划]
    L --> M[写入 plan.created]
    M --> N[选择 active step]
    K --> O[构建 Context Manifest]
    N --> O
    O --> P[调用 Model Gateway 并流式消费]
    P --> Q{模型返回什么?}
    Q -- activity/text --> R[流式渲染，追加消息]
    Q -- tool call --> S[校验工具和参数]
    Q -- final --> T[校验结果、敏感信息和完成条件]
    R --> P
    S --> U{策略允许?}
    U -- 否 --> V[返回拒绝或阻断结果]
    U -- 需询问 --> W[进入 waiting_approval]
    U -- 是 --> X[执行工具]
    W --> Y{用户决定}
    Y -- approve --> X
    Y -- reject --> V
    Y -- later --> W
    X --> AA{工具结果}
    AA -- success --> AB[写入 tool.completed、指标和证据]
    AA -- retryable error --> AC[有限退避重试]
    AA -- fatal / unknown --> AD[分类错误并暂停或换策略]
    AC --> X
    AB --> AE[更新 step、memory candidate、context]
    V --> AE
    AD --> AF{可恢复?}
    AF -- 是 --> N
    AF -- 否 --> AG([blocked / failed])
    AE --> AH{任务完成?}
    AH -- 否 --> AI{需要 compaction?}
    AI -- 是 --> AJ[进入压缩流程]
    AI -- 否 --> N
    AJ --> N
    AH -- 是 --> T
    T --> AK{验收通过?}
    AK -- 否 --> N
    AK -- 是 --> AL[写入完成事件、checkpoint、usage]
    AL --> AM([completed])
```

## 4. Tool Call 与 Approval

```mermaid
flowchart TD
    A([model.tool_call]) --> B[规范化名称和参数]
    B --> C{Schema 合法?}
    C -- 否 --> D[返回 INVALID_ARGUMENTS]
    C -- 是 --> E[workspace、敏感路径、网络和风险检查]
    E --> F{Critical?}
    F -- 是 --> G[硬阻断]
    G --> Z1([rejected])
    F -- 否 --> H{Approval mode 决策}
    H -- auto allow --> I[记录自动放行原因]
    H -- auto deny --> J[记录拒绝原因]
    J --> Z1
    H -- ask --> K[展示影响范围、命令、文件和 diff 摘要]
    K --> L{用户动作}
    L -- approve once --> M[一次性授权]
    L -- allow scope --> N[会话范围授权]
    L -- reject --> J
    L -- later --> K
    I --> O[执行前再次校验]
    M --> O
    N --> O
    O --> P{参数、任务、授权仍有效?}
    P -- 否 --> Q[撤销授权并返回状态变化]
    Q --> Z2([not executed])
    P -- 是 --> R[创建 checkpoint marker 并启动工具]
    R --> S{执行结果}
    S -- success --> T[保存结果、审计和 usage]
    S -- retryable --> U{预算还有?}
    U -- 是 --> R
    U -- 否 --> V[blocked：retry budget exhausted]
    S -- failure --> W[脱敏并返回稳定 error code]
    T --> Z3([completed])
    V --> Z4([blocked])
    W --> Z5([failed / model may adapt])
```

## 5. Inject 与 Queue（V1.x 预留）

```mermaid
flowchart TD
    A([用户输入]) --> B{快捷键 / 命令}
    B -- Ctrl+Enter / inject --> C[创建 inject message]
    B -- Alt+Enter / queue --> D[创建 queue item]
    C --> E{当前是否在 safe point?}
    E -- 否 --> F[标记 pending，继续当前工具]
    E -- 是 --> G[写入当前 task context]
    F --> H[工具完成或请求完成]
    H --> G
    G --> I[模型重新判断：继续 / 改计划 / 暂停]
    I --> Z1([返回 active task])
    D --> J[继承 Session 配置并记录依赖]
    J --> K[显示队列位置]
    K --> L{当前 task 完成?}
    L -- 否 --> M{用户取消 queue?}
    M -- 是 --> N([queue cancelled])
    M -- 否 --> K
    L -- 是 --> O[重新读取 workspace revision]
    O --> P{前置条件和 workspace 一致?}
    P -- 否 --> Q[队列暂停并提示审阅]
    Q --> R{用户继续?}
    R -- 否 --> S([queue paused])
    R -- 是 --> T[重新规划 queue task]
    P -- 是 --> T
    T --> U([queue task active])
```

## 6. Context Compaction（V1：人工 `/compact`；自动流程为 V1.x）

```mermaid
flowchart TD
    A([每次模型请求前统计]) --> B{达到 70%?}
    B -- 否 --> C[继续]
    C --> A
    B -- 是 --> D[显示 context warning]
    D --> E{达到 80% 或用户 /compact?}
    E -- 否 --> A
    E -- 是 --> F{正在审批或工具执行?}
    F -- 是 --> G[等 safe point，不截断副作用]
    G --> H{超过硬阈值?}
    H -- 否 --> I[获取 safe point]
    H -- 是 --> J([blocked：等待处理])
    F -- 否 --> I
    I --> K[获取 compaction lock 和 checkpoint]
    K --> L[按目标、约束、计划、事实、否定假设、变更、验证、队列生成摘要]
    L --> M[保留 system、最新用户消息、当前工具、关键 artifact 引用]
    M --> N{摘要和引用校验通过?}
    N -- 否 --> O[规则降级压缩或减少历史范围]
    O --> P{仍失败?}
    P -- 是 --> J
    P -- 否 --> L
    N -- 是 --> Q[写入新 context snapshot 与 source seq]
    Q --> R[重新计算预算]
    R --> S{低于硬阈值?}
    S -- 否 --> O
    S -- 是 --> T[释放锁，写入 compacted 事件]
    T --> U([继续执行])
```

## 7. 崩溃与恢复（V1：基础 stop/replay；完整 unknown side-effect UI 为 V1.x）

```mermaid
flowchart TD
    A([进程崩溃 / 网络断开 / UI 重启]) --> B[读取最后 checkpoint 和 event seq]
    B --> C{版本和存储完整?}
    C -- 否 --> D[事件重放和 schema migration]
    D --> E{最小状态可重建?}
    E -- 否 --> F([failed：导出诊断或新建 Session])
    E -- 是 --> G[生成 recovery candidate]
    C -- 是 --> G
    G --> H[查找非终态 tool / approval / compaction]
    H --> I{存在非终态 tool?}
    I -- 否 --> J[恢复 Task、Queue 和 Context]
    I -- 是 --> K{工具可查询状态或幂等?}
    K -- 是 --> L[查询外部状态]
    L --> M{已完成?}
    M -- 是 --> N[采用已确认结果，不重试]
    M -- 否 --> O[安全重试或标记失败]
    K -- 否 --> P[标记 unknown，暂停并请求人工判断]
    P --> Q{用户选择}
    Q -- 已完成 --> N
    Q -- 重试 --> O
    Q -- 放弃 --> R[注入失败结果]
    N --> J
    O --> J
    R --> J
    J --> S{存在旧审批?}
    S -- 是 --> T[全部失效，重新创建审批]
    S -- 否 --> U[校验 workspace revision]
    T --> U
    U --> V{一致?}
    V -- 否 --> W([paused：显示恢复差异])
    V -- 是 --> X[写入 session.resumed]
    X --> Y{用户选择}
    Y -- 继续 --> Z([running])
    Y -- 审阅 --> W
    Y -- 停止 --> AA([stopped])
```

## 8. V1 / V1.x 实现标注

| 流程 | V1 Core | V1.x |
|---|---:|---:|
| Onboarding、单 workspace、单 active Task | ✓ | |
| 串行 Tool Call、Ask/Smart/Auto、diff、validation、checkpoint、stop | ✓ | |
| 基础事件写入和 Session replay | ✓ | |
| Inject 与 Queue | | ✓ |
| 自动 Context Compaction | | ✓ |
| 完整 unknown side-effect recovery UI | | ✓ |
| Anthropic、动态模型发现、web_search | | ✓ |

流程图展示的是完整演进目标；实现 PR 必须在标题和验收标准中声明属于 Core、V1.x 还是 Future。

## 9. 任务完成判定

任务不是模型说“完成了”就完成。Runtime 至少检查：

```text
用户目标是否有结果
计划中必须完成的步骤是否完成或明确跳过
文件变更是否记录
diff 是否可读取
相关验证是否执行或明确未执行
未解决风险是否展示
是否存在 pending approval / pending queue / unknown tool
```

只有通过这些条件，才写入 `task.completed`。否则是 `blocked`、`needs_user` 或 `failed`。
