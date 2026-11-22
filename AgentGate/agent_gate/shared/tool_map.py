"""工具名 → AgentMoss action_type 归一化（2026-11）—— 与 TS ``ts/src/shared/tool-map.ts`` 对称。

## 为什么需要

Agent 的工具名是**运行时动态**的（用户自定义、MCP 动态发现、插件市场），
网关无法预知完整清单。硬编码白名单（历史实现）在遇到新工具名时把原名直接
透传给 AgentMoss，导致 read_before_write / 组合链 / 授权等依赖 action_type
的 L2 规则整条失效（只留 L1 看命令内容）。

## 策略（优先级从高到低）

1. **显式配置**：``AGENT_GATE_TOOL_NAME_MAP``（JSON 对象：工具名 → action_type）。
   客户对推断错误的工具可精确覆盖，大小写不敏感。
2. **内置映射**：通用工具名（executeBash / read_file / write_to_file …）。
3. **名称启发式**：按词元推断 ——
   - exec/bash 类：exec/execute/bash/shell/run/terminal/command/spawn/system/process/eval
   - 写类（含外发）：write/edit/create/delete/remove/modify/update/patch/append/mkdir/
     move/rename/replace/save/put/upload/send/copy/touch/truncate/chmod/chown
   - 读类：read/list/ls/dir/search/grep/glob/find/query/get/view/cat/head/tail/
     load/fetch/download/open/show/check/inspect/stat
   **危险优先**：同一名字同时命中多类时取最危险（exec > write > read），
   防止 ``read_config_and_update`` 被误判为只读。
4. **未知**：保持原名，``recognized=False`` —— 调用方不得把未识别动作当作安全
   动作豁免，并应把未识别工具名上报（AgentMoss 在响应 degraded 中提示）。

## 安全说明

- 启发式只做**归类**，不放行：归为 read 的工具仍走完整 L1/L2 规则；
- 未知工具不享受任何白名单豁免；
- 配置来自本机管理员（env），信任级别与现有配置一致。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: 内置映射（工具名小写 → AgentMoss action_type）。
_BUILTIN_MAP: dict[str, str] = {
    # 执行类
    "executebash": "bash",
    "executezsh": "bash",
    "executepowershell": "bash",
    "bash": "bash",
    "shell": "bash",
    "terminal": "bash",
    # 写类
    "write_to_file": "write",
    "file_write": "write",
    "write": "write",
    "write_file": "write",
    "append_file": "write",
    "file_edit": "edit",
    "edit": "edit",
    "edit_file": "edit",
    "create_file": "write",
    "delete_file": "write",
    "delete_path": "write",
    "remove_file": "write",
    "move_file": "write",
    "copy_file": "write",
    "mkdir": "write",
    # 读类
    "file_read": "read",
    "read_file": "read",
    "read": "read",
    "list_dir": "list_dir",
    "list_files": "list_dir",
    "list_directory": "list_dir",
    "search_content": "grep",
    "search_file": "grep",
    "grep": "grep",
    "glob": "glob",
    "glob_search": "glob",
    "grep_search": "grep",
    "ast_search": "grep",
    # 网络类
    "web_fetch": "web_fetch",
    "web_search": "web_search",
    "browser": "web_fetch",
    # QwenPaw（2026-09 对接）
    "execute_shell_command": "bash",
}

#: 启发式词元表。顺序即危险优先级（命中即返回）。
_EXEC_TOKENS = frozenset({
    "exec", "execute", "bash", "sh", "shell", "run", "terminal", "command",
    "cmd", "spawn", "system", "process", "eval", "powershell", "zsh",
})
_WRITE_TOKENS = frozenset({
    "write", "edit", "create", "delete", "remove", "rm", "modify", "update",
    "patch", "append", "mkdir", "move", "rename", "replace", "save", "put",
    "upload", "send", "copy", "cp", "touch", "truncate", "chmod", "chown",
})
_READ_TOKENS = frozenset({
    "read", "list", "ls", "dir", "search", "grep", "glob", "find", "query",
    "get", "view", "cat", "head", "tail", "load", "fetch", "download",
    "open", "show", "check", "inspect", "stat",
})

_CONFIG_ENV = "AGENT_GATE_TOOL_NAME_MAP"
#: 配置文件路径覆盖（默认 ~/.config/agentgate/tool_name_map.json）
_FILE_ENV = "AGENT_GATE_TOOL_NAME_MAP_FILE"

_tokenize_re = re.compile(r"[^a-zA-Z0-9]+")
_camel_re = re.compile(r"([a-z0-9])([A-Z])")


def _tokenize(name: str) -> set[str]:
    """拆词：camelCase / snake_case / kebab-case / 命名空间前缀统一成小写词元。"""
    split = _camel_re.sub(r"\1_\2", name or "")
    return {p for p in _tokenize_re.split(split.lower()) if p}


def _merge_map(result: dict[str, str], data: object, source: str) -> None:
    """把一份 JSON 对象合并进结果（大小写不敏感，非字符串条目跳过）。"""
    if not isinstance(data, dict):
        logger.warning("%s 必须是 JSON 对象，已忽略", source)
        return
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        key, val = k.strip().lower(), v.strip()
        if key and val:
            result[key] = val


def config_file_path() -> Path:
    """工具名映射配置文件路径。

    默认与 auth_token 同目录：``~/.config/agentgate/tool_name_map.json``
    （``XDG_CONFIG_HOME`` 生效）；``AGENT_GATE_TOOL_NAME_MAP_FILE`` 可覆盖。
    客户只需编辑这个 JSON 文件，不需要设置环境变量。
    """
    override = (os.getenv(_FILE_ENV) or "").strip()
    if override:
        return Path(override)
    xdg = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "agentgate" / "tool_name_map.json"


def load_configured_map() -> dict[str, str]:
    """读取显式配置：**配置文件优先**，环境变量 ``AGENT_GATE_TOOL_NAME_MAP`` 覆盖同名项。

    非法内容只告警，不影响默认行为（内置映射 / 启发式）。
    """
    result: dict[str, str] = {}

    # 1) 配置文件（客户日常维护方式）
    path = config_file_path()
    try:
        if path.is_file():
            _merge_map(result, json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("工具名映射配置文件读取失败（%s）: %s", path, exc)

    # 2) 环境变量（显式覆盖，便于临时联调）
    raw = (os.getenv(_CONFIG_ENV) or "").strip()
    if raw:
        try:
            _merge_map(result, json.loads(raw), _CONFIG_ENV)
        except (ValueError, TypeError):
            logger.warning("%s 不是合法 JSON，已忽略", _CONFIG_ENV)

    return result


def _hits(tokens: set[str], keywords: frozenset[str]) -> bool:
    """词元命中判定：精确匹配，或长关键词（≥4 字符）作前缀匹配。

    前缀匹配是为了兼容无分隔符的粘连词（``writefile`` / ``readfile``），
    同时避免短词误伤（``ls``/``rm``/``sh`` 只做精确匹配；``thread`` 不会因
    以 "read" 结尾被误判）。
    """
    for token in tokens:
        if token in keywords:
            return True
        for kw in keywords:
            if len(kw) >= 4 and token.startswith(kw):
                return True
    return False


def _guess_by_name(raw: str) -> Optional[str]:
    """按词元启发式推断；危险优先（exec > write > read），无命中返回 None。"""
    tokens = _tokenize(raw)
    if _hits(tokens, _EXEC_TOKENS):
        return "bash"
    if _hits(tokens, _WRITE_TOKENS):
        return "write"
    if _hits(tokens, _READ_TOKENS):
        return "read"
    return None


def resolve_action_type(tool_name: str) -> tuple[str, bool]:
    """工具名归一化为 AgentMoss action_type。

    返回 ``(action_type, recognized)``。``recognized=False`` 表示未知工具：
    action_type 保持原名，调用方**不得**将其当作安全动作豁免，并应上报提示。
    """
    raw = (tool_name or "").strip()
    if not raw:
        return "unknown", False
    key = raw.lower()

    configured = load_configured_map()
    if key in configured:
        return configured[key], True
    if key in _BUILTIN_MAP:
        return _BUILTIN_MAP[key], True

    guessed = _guess_by_name(raw)
    if guessed is not None:
        return guessed, True

    return raw, False
