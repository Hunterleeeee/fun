# Fun V1 公共契约

> 这份文档是实现、测试、第三方 Provider、第三方 Tool 和开源贡献的最小公共基线。

## 1. V1 只保证什么

```text
单本地进程
单 workspace
单 active Task
单串行 Agent Loop
OpenAI-compatible 优先
explore / read / edit / exec
Plan + 有限 ReAct
Ask / Smart / Auto
事件日志和基础 replay
patch / diff / validation / checkpoint / stop
token / TTFT / 工具耗时 / 总耗时
单栏流式终端
```

## 2. V1 明确不保证什么

```text
Anthropic native
自动 models discovery
web_search
Queue
强制抢占式 Inject
自动 context compaction
复杂跨 Session Memory
多 Agent
后台 daemon
跨进程协作
完整 unknown side-effect 自动恢复
```

这些能力可以在协议上预留，但实现不存在时必须返回明确的 `UNSUPPORTED_CAPABILITY`，不能静默降级成错误行为。

## 3. Runtime 不可变事实

以下事实只能由 Runtime 产生：

```text
tool.started
 tool.completed
file.changed
diff.created
validation.completed
checkpoint.created
task.completed
```

模型输出的普通文本、activity、计划说明和最终答案都不是事实。

## 4. 完成条件

只有同时满足以下条件才允许产生 `task.completed`：

- 用户目标已经有结果或明确结论。
- 计划步骤已完成、跳过并说明原因，或进入明确 blocked。
- 所有 Agent 变更有 change-set。
- diff 可以读取。
- 验证已经执行，或明确记录未执行和原因。
- 没有 pending tool、pending approval 或 unknown side effect。
- 剩余风险已展示给用户。

## 5. 错误码原则

错误码稳定、面向机器；message 可本地化、面向用户：

```text
INVALID_ARGUMENTS
UNSUPPORTED_CAPABILITY
PATH_OUTSIDE_WORKSPACE
SYMLINK_ESCAPE
PROTECTED_PATH
APPROVAL_REQUIRED
APPROVAL_REJECTED
FILE_CHANGED_SINCE_READ
PATCH_FAILED
COMMAND_TIMEOUT
COMMAND_OUTPUT_LIMIT
PROVIDER_AUTH_FAILED
PROVIDER_RATE_LIMITED
PROVIDER_CONTEXT_EXCEEDED
PROVIDER_UNAVAILABLE
TOOL_UNKNOWN_AFTER_CRASH
TASK_BUDGET_EXCEEDED
TASK_BLOCKED
```

## 6. V1 Provider Contract

Provider Adapter 至少实现：

```text
testConnection()
streamChat(request)
normalizeUsage(raw)
mapReasoning(level, capabilities)
```

V1 允许手动 Model ID。Adapter 必须声明：

```text
supports_streaming
supports_tools
supports_reasoning
context_window
usage_precision
```

未知能力必须标为 `unknown`，不能伪装成支持。

## 7. V1 Tool Contract

每个 Tool 必须定义：

```text
name
version
input_schema
risk_class
cancellable
timeout
output_limit
workspace_access
recovery_semantics
```

所有调用必须携带：

```text
session_id
task_id
turn_id
call_id
operation_id
```

V1 默认串行；重复 `call_id` 拒绝；未知字段拒绝；工具事实只以 Runtime result 为准。

## 8. V1 配置生效

```text
/model       下一次 model request
/reasoning   下一次 model request
/approval    下一次 tool decision
workspace    新 Session
locale       新 renderer / 新 Session
```

当前 model request、已启动 tool 和已批准但未启动的副作用不会因为设置切换而被偷偷改写；用户停止时遵守 safe point。

## 9. V1 CLI 退出码

```text
0 completed
1 failed
2 blocked / needs user
3 cancelled
4 configuration or provider error
5 recovery required
6 invalid arguments
```

非 TTY 下不等待审批；需要人工决策的动作返回 `2`，并在 stderr 给出原因。

## 10. V1 安全底线

任何 approval mode 都不能：

- 访问 workspace 外路径。
- 通过 symlink 或 shell 逃逸。
- 修改 Fun 自己的 policy、事件或凭证存储。
- 读取或持久化明文凭证。
- 永久删除不可恢复数据。
- 执行未审计的后台进程。
- 覆盖用户或外部进程在 Agent 之后产生的修改。

## 11. V1 验收场景

1. 新 workspace onboarding。
2. 只读调查任务。
3. 普通源码 patch、diff 和测试。
4. 高风险命令 approval。
5. 文件 hash 冲突。
6. 命令超时。
7. provider auth / rate limit / context error。
8. Ctrl-C graceful stop。
9. Session 重启后读取历史状态。
10. Auto 模式尝试 Critical 操作并被阻断。
11. 80 列、无色、非 TTY 输出。
12. 中文和英文 UI。

任何功能 PR 必须说明它属于 Core、V1.x 还是 Future，并更新对应 contract、流程图、测试或 ADR。
