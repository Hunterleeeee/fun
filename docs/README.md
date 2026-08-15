# Fun 文档索引

Fun 的设计文档分成“产品方向、公共契约、Runtime 规格和开源工程”四层。实现和贡献时，以公共契约为准，不以早期讨论稿为准。

## 阅读顺序

1. [`fun-complete-design.md`](fun-complete-design.md) — 产品、Runtime 和 V1 纵向闭环总设计。
2. [`fun-v1-contract.md`](fun-v1-contract.md) — V1 必须实现的公共契约、错误码、退出码和验收场景。
3. [`fun-runtime-spec.md`](fun-runtime-spec.md) — System Prompt、Tool Call、Event、Context、状态和恢复细节。
4. [`fun-open-source-blueprint.md`](fun-open-source-blueprint.md) — 仓库分层、开源治理、安全、隐私、测试、发布和许可证。
5. [`fun-flowcharts.md`](fun-flowcharts.md) — Core Runtime 与 V1.x 预留流程图。
6. [`fun-harness-ui-v2.svg`](fun-harness-ui-v2.svg) — 当前推荐的单栏流式终端 UI。

## 文档权威级别

```text
fun-v1-contract.md
  > fun-runtime-spec.md
  > fun-complete-design.md
  > fun-product-design.md
```

当文档冲突时，优先遵循 `fun-v1-contract.md`；需要改变公共语义时，必须新增 ADR、更新 schema、测试和流程图。

## 版本范围

### V1 Core

```text
单 workspace
单 active Task
OpenAI-compatible 优先
手动 Model ID
explore / read / edit / exec
Plan + 有限 ReAct
Ask / Smart / Auto
事件事实源
patch / diff / validation / checkpoint / stop
基础 usage
单栏流式 UI
```

### V1.x

```text
Anthropic native
动态模型发现
web_search
Inject / Queue
自动 Context Compaction
完整 unknown-side-effect recovery UI
更完整 Memory
```

## 当前 UI 方向

默认不是固定双屏，而是：

```text
单栏流式正文
+ 紧凑状态栏
+ 可折叠计划
+ 底部输入栏
```

`fun-harness-ui.svg` 是早期探索稿；当前设计以 `fun-harness-ui-v2.svg` 和 Runtime 规格中的单栏布局为准。

## 开源发布前检查

在公开发布前还必须创建并维护：

```text
README.md
LICENSE
SECURITY.md
CONTRIBUTING.md
CODE_OF_CONDUCT.md
CHANGELOG.md
```

其内容要求见 [`fun-open-source-blueprint.md`](fun-open-source-blueprint.md)。
