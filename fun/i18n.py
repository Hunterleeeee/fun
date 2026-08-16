from __future__ import annotations

TEXT = {
    "en-US": {
        "commands_hint": "Enter message · / for commands · ↑↓ choose · Ctrl+C cancel",
        "offline": "Offline mode · no model calls will be made.",
        "offline_task": "Offline mode: configure a provider before starting a task.",
        "configure": "Run `fun --configure` to configure provider, model, and credentials.",
        "api_key_hint": "API key: paste supported; input is hidden and will not echo.",
        "api_key_required": "API key is required.",
        "model_load_failed": "Could not load model list; manual model ID is available.",
        "choose_model": "Available models · ↑↓ choose · Enter accept",
        "permission": "Permission mode",
        "permission_options": "[1] ask  [2] smart (recommended)  [3] auto",
        "saved": "Setup saved · API key is stored securely, not in config.json.",
        "removed": "Saved API key, provider, and model removed.",
        "cancelled": "Configuration cancelled.",
        "unknown_command": "Unknown command. Type / to browse commands.",
        "no_provider": "Configure a provider first.",
        "thinking": "Thinking…",
        "generating": "Generating…",
        "tool_running": "Running {name}…",
        "approval_wait": "Waiting for your approval…",
        "context_compacted": "Context trimmed to keep this turn fast.",
    },
    "zh-CN": {
        "commands_hint": "输入消息 · 输入 / 浏览命令 · ↑↓ 选择 · Ctrl+C 取消",
        "offline": "离线模式 · 不会调用模型。",
        "offline_task": "离线模式：请先配置 Provider，再开始任务。",
        "configure": "运行 `fun --configure` 配置 Provider、模型和凭据。",
        "api_key_hint": "API Key：支持直接粘贴；输入不会回显。",
        "api_key_required": "必须输入 API Key。",
        "model_load_failed": "无法获取模型列表；可以手动输入模型 ID。",
        "choose_model": "可用模型 · ↑↓ 选择 · Enter 确认",
        "permission": "权限模式",
        "permission_options": "[1] ask  [2] smart（推荐）  [3] auto",
        "saved": "配置已保存 · API Key 已安全保存，不会写入 config.json。",
        "removed": "已删除保存的 API Key、Provider 和模型。",
        "cancelled": "配置已取消。",
        "unknown_command": "未知命令。输入 / 浏览可用命令。",
        "no_provider": "请先配置 Provider。",
        "thinking": "思考中…",
        "generating": "生成中…",
        "tool_running": "正在运行 {name}…",
        "approval_wait": "等待你确认…",
        "context_compacted": "已压缩上下文，保持当前请求速度。",
    },
}


def t(locale: str, key: str) -> str:
    language = "zh-CN" if locale.startswith("zh") else "en-US"
    return TEXT[language].get(key, TEXT["en-US"].get(key, key))
