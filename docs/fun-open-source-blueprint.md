# Fun Harness 开源工程蓝图

> **Coding should feel good.**

本文不是功能愿望清单，而是 Fun 作为开源项目进入实现、审查、贡献和发布阶段时必须遵守的工程契约。

---

## 1. 开源项目定义

Fun 是一个本地优先的终端 Coding Agent Runtime。它把模型、工具、workspace、审批、事件、任务状态和终端 UI 组织成一个可审计、可恢复、可扩展的运行时。

Fun 的开源价值不是把某个模型 API 包一层，而是公开一套清晰的 Runtime 边界：

```text
模型提出意图
  ↓
Runtime 校验意图
  ↓
Policy 决定是否允许
  ↓
Executor 产生真实副作用
  ↓
Event Store 记录事实
  ↓
UI 投影状态
```

任何实现都不得绕过这条链路。

### 1.1 项目目标

- 让第一次使用的人不读文档也能完成一次安全 Coding 任务。
- 让高级用户能够配置模型、策略、Prompt、主题和扩展点。
- 让贡献者可以在不理解全部 Runtime 的情况下，独立开发 Provider、Tool 或 Renderer。
- 让安全研究者可以审查 workspace 边界、命令执行和恢复语义。
- 让每个重要状态都能从事件和 artifact 中解释、重放或恢复。

### 1.2 不做的事情

- 不把 Fun 绑定到某一个模型供应商。
- 不把隐藏思维链当成产品协议。
- 不允许插件绕过 Policy、Workspace Guard 或 Event Store。
- 不用“模型说成功了”替代工具事实和验证结果。
- 不为了功能数量牺牲工具边界、恢复能力和终端可读性。

---

## 2. V1 / V1.x / Future 范围冻结

### 2.1 V1：一个可交付的纵向切片

V1 的验收目标是：新用户在一个 workspace 中，可以在 10 分钟内完成一次有文件变更、验证结果和可追踪事件的 Coding 任务。

| 能力 | V1 决策 |
|---|---|
| Workspace | 单 workspace；路径边界和 symlink 检查必须有 |
| Session | 本地持久化；一个 Session 可有历史 Task |
| Active Task | 同时只允许一个 running Task |
| Provider | 首发 OpenAI-compatible；内部保留 Adapter 接口 |
| Model | 支持手动 Model ID；模型列表动态发现为 V1.x |
| Tools | `explore`、`read`、`edit`、`exec` |
| Plan | 2–7 步任务计划，步骤可更新但有证据要求 |
| ReAct | 只允许 step 内有限 Observe → Act → Validate |
| Approval | Ask / Smart / Auto，Critical 永久阻断 |
| Files | patch + expected hash；diff；Agent change-set |
| Validation | 至少支持用户项目已有的测试、类型检查或命令验证 |
| Recovery | stop、checkpoint、基础事件 replay；unknown side effect 不自动重试 |
| Context | token 预警、Context Manifest、人工 `/compact` |
| Memory | 低风险、证据驱动的 workspace/task facts |
| UI | 单栏流式终端；80 列可用；无色可读 |
| Usage | input/output、精确/估算标记、TTFT、工具耗时、总耗时 |
| i18n | 中英文资源和布局基础 |

### 2.2 V1.x：连续性和覆盖面

- Anthropic native Adapter。
- `/models` 动态发现和 capability probing。
- `web_search`，带独立网络策略和敏感查询防护。
- Inject：下一个 safe point 生效。
- Queue：单级顺序队列。
- 完整 Session replay 和恢复 UI。
- 自动 context compaction。
- 更完整的 Memory freshness、冲突和归档。
- 更详细的 usage 面板。

### 2.3 Future：不为 V1 设计假实现

- 多 Agent。
- 多人共享 Session。
- 远程 workspace。
- IDE / Web UI。
- 插件市场。
- 多级队列依赖和并行任务。
- 生产部署和自动发布。
- 向量记忆和跨项目长期用户画像。

可以为 Future 预留接口，但不得在 V1 中伪装成已支持能力。

---

## 3. 推荐仓库结构

```text
fun/
├── README.md
├── LICENSE
├── SECURITY.md
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── CHANGELOG.md
├── package.json
├── apps/
│   └── cli/                    # fun 命令、TTY 检测、生命周期
├── packages/
│   ├── runtime/                # Task Orchestrator、状态机、事件驱动循环
│   ├── protocol/               # Event、Tool、Provider、Context schemas
│   ├── model-gateway/          # Provider Adapter 与统一流事件
│   ├── tools/                  # explore/read/edit/exec
│   ├── policy/                 # approval、风险、workspace guard
│   ├── workspace/              # 路径、git、change-set、checkpoint
│   ├── persistence/            # SQLite、artifact、migration、replay
│   ├── memory/                 # 证据抽取、去重、作用域、清理
│   ├── context/                # manifest、token budget、compaction
│   ├── renderer-terminal/      # 单栏 UI、宽度、颜色、输入、快捷键
│   └── i18n/                   # zh-CN / en-US message resources
├── schemas/
│   ├── events/
│   ├── tools/
│   ├── providers/
│   └── config/
├── docs/
│   ├── fun-complete-design.md
│   ├── fun-runtime-spec.md
│   ├── fun-flowcharts.md
│   ├── fun-open-source-blueprint.md
│   ├── decisions/              # ADR
│   └── examples/
├── tests/
│   ├── contract/
│   ├── unit/
│   ├── integration/
│   ├── recovery/
│   ├── security/
│   ├── providers/
│   └── snapshots/
└── fixtures/
    ├── providers/
    ├── projects/
    └── crashes/
```

### 3.1 依赖方向

```text
protocol
  ↑
persistence ← runtime → model-gateway
                     ↓
                tools / policy / workspace
                     ↓
              renderer-terminal / cli
```

约束：

- `protocol` 不依赖 UI、Provider 或具体数据库。
- `runtime` 不直接 import 终端输出函数。
- `tools` 不直接读取用户配置绕过 Policy。
- `renderer-terminal` 只能消费事件和只读 Runtime query。
- Provider Adapter 不得决定工具是否执行。
- persistence 不包含业务判断。

---

## 4. 开源边界：什么可以扩展，什么不能绕过

### 4.1 可扩展点

- Model Provider Adapter。
- Read-only Tool。
- Tool schema 和 renderer。
- Prompt section。
- Memory extractor。
- Context summarizer。
- Terminal theme。
- Slash command。
- Event consumer。

### 4.2 不可绕过的底座

以下接口必须由 Runtime 持有，扩展只能调用受限 API：

```text
WorkspaceGuard
PolicyEngine
ToolExecutor
EventStore
CheckpointManager
CredentialRedactor
```

扩展不得：

- 直接执行 shell。
- 直接写 workspace。
- 直接写或删除事件。
- 修改 Critical deny list。
- 读取未授权凭证。
- 伪造 `tool.completed`、`file.changed` 或 `task.completed`。
- 将普通模型文本伪装成 Runtime 事实。

---

## 5. 公共协议版本策略

### 5.1 三种版本

```text
protocol version：事件和 Tool/Provider contract
schema version：具体 JSON schema
application version：Fun 发布版本
```

三者不能混为一个字符串。

### 5.2 兼容策略

- 新增可选字段：minor compatible。
- 修改字段语义或删除字段：major change。
- 事件必须保留旧版本 upcaster。
- Tool 的历史调用必须记录 tool version。
- Provider 的 raw response 不作为跨版本公共协议。
- UI renderer 对未知事件显示可读 fallback，不应直接崩溃。

### 5.3 ADR 要求

任何下列变更必须新增 ADR：

- 改变 Task / Tool 状态迁移。
- 增加新的副作用工具。
- 修改 workspace 边界。
- 修改默认 approval。
- 修改事件 payload。
- 改变 compaction 或 recovery 语义。
- 新增 Provider 特殊行为。

ADR 模板：

```text
# ADR-XXXX: 标题

Status: proposed | accepted | deprecated
Context:
Decision:
Consequences:
Alternatives:
Migration:
Security impact:
```

---

## 6. 开源安全基线

### 6.1 威胁模型

必须考虑：

- 恶意项目文件和 Prompt Injection。
- 恶意网页和搜索结果。
- 模型错误或幻觉。
- Shell 命令越界。
- symlink / race condition。
- Provider 返回恶意内容。
- 插件绕过策略。
- 日志、Memory 和 artifact 泄露凭证。
- 崩溃后重复执行副作用。

### 6.2 安全原则

```text
默认最小权限
Runtime 强制而非 Prompt 约束
不确定就阻断
不覆盖用户修改
事实可追溯
副作用可识别
敏感数据默认不持久化
```

### 6.3 安全报告

开源仓库必须提供 `SECURITY.md`，至少说明：

- 支持的版本。
- 私下报告渠道。
- 不要公开披露未修复的 workspace escape、credential leak、任意执行漏洞。
- 安全问题的复现信息格式。
- 修复、披露和 CVE 处理原则。

### 6.4 安全测试门槛

任何涉及以下代码的 PR 必须有测试：

```text
path canonicalization
symlink / link handling
shell execution
credential handling
approval policy
patch / checkpoint / restore
artifact cleanup
```

---

## 7. 隐私和数据生命周期

### 7.1 默认本地存储

Fun 默认本地保存：

- Session metadata。
- 结构化事件。
- Task / Plan 状态。
- Agent change-set。
- 经过脱敏的 usage。
- 低风险 Memory。

### 7.2 外发数据

用户必须知道以下数据可能发送给外部 Provider：

- 用户消息。
- 被选入 context 的文件片段。
- 工具结果。
- 计划和结构化任务状态。
- web_search query（V1.x）。

Fun 不应默认上传整个 workspace。

### 7.3 默认禁止持久化

```text
API key / token / password
private key / certificate secret
未脱敏客户数据
完整敏感命令输出
原始隐藏 reasoning
```

### 7.4 删除语义

- 删除 Memory 不等于删除审计事件。
- 删除 Session 需要明确删除 metadata、artifact、memory 引用还是全部原始事件。
- `forget` 操作本身写入审计事件，但不保存被删除的秘密原文。
- 清理 artifact 前检查是否仍被 checkpoint、event 或 recovery 引用。

---

## 8. 贡献流程

### 8.1 Issue 分类

```text
bug
security
provider
runtime
ui
protocol
performance
documentation
feature
```

Feature issue 必须说明：

- 用户问题。
- 为什么现有能力不够。
- 是否属于 V1 / V1.x。
- 对状态机、事件、权限和恢复的影响。
- 验收标准。

### 8.2 Pull Request 必填项

```text
[ ] 说明用户价值
[ ] 标明影响的 Runtime 层
[ ] 更新协议或 ADR（如需要）
[ ] 增加测试
[ ] 检查事件和状态迁移
[ ] 检查敏感信息和日志脱敏
[ ] 检查非 TTY / 无色终端
[ ] 更新文档和 CHANGELOG
[ ] 说明是否改变默认行为
```

### 8.3 评审优先级

1. 数据丢失、越界、凭证泄露和重复副作用。
2. 状态机和事件一致性。
3. 用户已有修改保护。
4. Provider 兼容和失败恢复。
5. 终端可用性和国际化。
6. 性能和代码风格。

### 8.4 Commit 建议

采用 Conventional Commits：

```text
feat(runtime): add task step state
fix(workspace): reject symlink escape
feat(provider): add anthropic adapter
docs(protocol): define tool result error codes
test(recovery): cover unknown exec
```

---

## 9. 测试与 CI 门槛

### 9.1 必须通过

```text
format
lint
typecheck
unit tests
protocol contract tests
security path tests
recovery tests
terminal snapshot tests
```

### 9.2 Provider 测试

- 默认 CI 不使用真实 API Key。
- Provider 使用脱敏 fixture 和本地 mock server。
- live test 必须显式 opt-in。
- fixture 覆盖 stream 分片、工具参数跨 chunk、usage 缺失、429、5xx、context overflow 和 malformed response。

### 9.3 Recovery 注入

必须在以下点注入进程退出并验证状态：

```text
tool.requested 写入后
tool.started 写入后
patch apply 前后
checkpoint 写入前后
model stream 中
approval pending
compaction lock 中
queue item 切换中
```

### 9.4 终端快照

至少覆盖：

```text
80 / 100 / 120 / 160 columns
zh-CN / en-US
color / no-color
Unicode / ASCII fallback
TTY / non-TTY
long path / long command / long error
approval / diff / blocked / recovery
```

---

## 10. 发布策略

### 10.1 版本阶段

```text
0.x：协议和默认行为可能变化，适合早期贡献
1.0：V1 公共协议冻结，安全和恢复契约稳定
1.x：兼容增加 Provider、Tool 和 UI 能力
2.0：仅在公共协议或状态语义无法兼容时升级
```

### 10.2 Release 必须包含

- CHANGELOG。
- 迁移说明。
- 协议变更说明。
- 安全修复说明。
- 已知限制。
- Provider 兼容矩阵。
- 数据库 migration。
- 可复现构建或锁定依赖。

### 10.3 默认行为变更

改变以下默认值必须在 release note 中明确：

- approval mode。
- workspace deny list。
- token / timeout budget。
- context threshold。
- 默认模型参数。
- 数据保留周期。
- 网络访问策略。

---

## 11. 许可证决策

许可证必须在第一次公开发布前确定，不建议仓库长期处于“代码可见但授权不清”的状态。

候选方向：

| 方向 | 适合情况 |
|---|---|
| Apache-2.0 | 希望企业使用，并明确专利授权和贡献者保护 |
| MIT | 追求最简、最宽松的使用和再发布 |
| MPL-2.0 | 希望修改文件保持开源，但允许组合使用 |
| AGPL-3.0 | 希望网络服务形态的修改也回馈开源，但会降低部分企业采用意愿 |

建议在社区和潜在贡献者讨论后选择；在没有决定前，不应在 README 中声称“开源许可”而不提供 LICENSE。

---

## 12. 开源项目的 README 首屏应该回答什么

```text
Fun 是什么？
为什么比普通 coding agent 不同？
10 分钟如何安装并完成第一次任务？
支持哪些模型连接？
默认会访问和修改什么？
如何停止、审阅 diff 和恢复？
V1 做什么、不做什么？
如何贡献？
如何报告安全问题？
使用什么许可证？
```

README 不应一开始塞入完整 Runtime 内部细节。推荐结构：

```text
一句话定位
截图 / 终端 GIF
快速开始
安全说明
核心能力
V1 范围
配置模型
开发者文档
贡献
安全
许可证
```

---

## 13. 开源项目完成定义

Fun 不应以“代码能运行”作为开源准备完成，而应满足：

- 新用户能安装、配置、运行和退出。
- 贡献者能跑完测试并理解仓库分层。
- Provider 可以通过公共 Adapter contract 增加。
- Tool 可以增加但不能绕过安全底座。
- 事件可以 replay，状态机有 contract test。
- 安全问题有报告渠道和处理流程。
- 文档、许可证、CHANGELOG、迁移和版本策略齐全。
- V1/V1.x 的能力边界与代码、流程图、README 一致。
- 任何危险默认行为都在文档和 UI 中明确可见。

开源不是把内部草稿上传到 GitHub；开源是让陌生人可以安全地理解、运行、审查、修改和贡献这个项目。
