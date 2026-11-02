# AgentMoss

多层级 AI Agent 安全分析引擎。输入「下一步动作 + 上下文」，输出 `Allow` / `Deny`、
风险级别与原因。可直接作为 Python 库嵌入，也可以启动 HTTP 服务由 AgentGate 调用。

本仓库是 **Python** 裁剪版：

- 只包含 Python 实现，不包含 TypeScript 版；
- 不包含 Brain 自学习模块（规则 / Skill 自动提取、curator、`/api/v1/brain/*`
  接口与 `agent-moss brain` 子命令均已移除）；
- L1 / L2 / L3 检测、规则、Skill、场景与 agent 扩展均保留。

## 三层防御

| 层 | 内容 |
|---|---|
| L1.1 | 特征匹配：危险命令、注入检测、用户敏感规则 |
| L1.2 | 脚本内容预扫描：递归追踪脚本调用链 |
| L2.1 | 逻辑规则：read-before-write、意图一致性、敏感路径、危险模式 |
| L2.2 | 形式化验证：行为图 + 性质验证，含跨会话组合链 |
| L2.3 | 脚本内容 L2 扫描 |
| L3.1 | LLM + Skill 深度语义分析（可用 `AGENT_MOSS_DISABLE_LLM=1` 关闭） |

## 安装

要求 Python >= 3.10。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

安装后会得到 `agent-moss` 命令行。

## 快速开始

### CLI

```bash
# 生成输入模板
agent-moss init -o input.json

# 分析输入文件
agent-moss analyze input.json

# 或直接分析一条命令
agent-moss analyze "cat ~/.ssh/id_rsa" --json

# 启动 HTTP 服务（默认 127.0.0.1:9090）
agent-moss server --host 0.0.0.0 --port 9090

# 服务状态
agent-moss status

# 安装 / 卸载 systemd 服务
agent-moss install --enable

# 规则目录自查与模板生成
agent-moss rules status
agent-moss rules init --dir ./rules
```

常用环境变量：

```bash
AGENT_MOSS_DISABLE_LLM=1      # 只跑 L1/L2，不调用 LLM
AGENT_MOSS_LLM_BASE_URL=...   # OpenAI 兼容接口
AGENT_MOSS_LLM_API_KEY=...
AGENT_MOSS_LLM_MODEL=...
AGENT_MOSS_CONFIG_PATH=...    # 指定配置文件
AGENT_MOSS_DATA_DIR=...       # 指定数据目录
```

### Python import

```python
from agent_moss.engine.analyzer import judge_security, analyze
from agent_moss.infra.config import get_default_config

config = get_default_config()

# 只做安全判定
judgment = judge_security(
    prompt_session="list files in the project",
    action_history=[],
    a_next={"action_type": "bash", "action_detail": "ls -la"},
    reason="list files",
    config=config,
)
print(judgment.allowed, judgment.reason, judgment.risk_level)

# 判定 + 允许时生成最小 policy
result = analyze(
    session_id="sess-001",
    prompt_session="list files in the project",
    action_history=[],
    a_next={"action_type": "bash", "action_detail": "ls -la"},
    reason="list files",
    config=config,
)
print(result["decision"], result["reason"])
```

也可以直接拿到 FastAPI 应用：

```python
from agent_moss.server.app import create_app

app = create_app()
```

### HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查 |
| POST | `/api/v1/analyze` | 安全分析 |
| POST | `/api/v1/review` | 低频全量回顾（供 AgentGate 触发） |
| GET | `/console` | 运行时控制台 |

`POST /api/v1/analyze` 请求示例：

```json
{
  "session_id": "sess-001",
  "prompt_session": "list files",
  "action_history": [],
  "a_next": {
    "action_type": "bash",
    "action_detail": "ls -la"
  },
  "reason": "list files",
  "agent_id": "",
  "os_type": "linux",
  "cwd": "/home/user/project"
}
```

## 规则与扩展

| 路径 | 内容 |
|---|---|
| `agent_moss/rules/*.json` | L1 / L2 内置规则 |
| `agent_moss/skills/*.md` | L3 Skill 规则（注入 LLM prompt） |
| `agent_moss/agents/<agent>/rules.json` | agent 专属扩展（如 `xiaoo`、`opendesk`） |
| `agent_moss/scenarios/<id>/rules.json` | 场景定制规则（如 `bank` 沙箱） |
| `agent_moss/profiles/` | linux / windows / macos 平台特征 |
| `agent_moss/data/` | 运行时数据模板与规则模板 |

规则目录支持 `extra.json` / `override.json`：

```bash
export AGENT_MOSS_EXTRA_RULES_DIR=/path/to/rules
agent-moss rules status
```

也可以用 `AGENT_MOSS_CUSTOM_RULES` 直接传 JSON 规则数组。

## 配置

配置加载优先级（从高到低）：

1. 环境变量；
2. `AGENT_MOSS_CONFIG_PATH` 指定的文件；
3. 用户配置 `~/.config/agentmoss/config.json`；
4. 包内默认配置。

运行时开关（Console 写入）位于 `~/.config/agentmoss/agent_moss_runtime.json`，
会覆盖源码内置规则。L3 是否真正执行由环境变量 / 配置文件 + LLM 连通性探测共同决定。

更多环境变量：

| 环境变量 | 说明 |
|---|---|
| `AGENT_MOSS_DISABLE_HEURISTIC` | 关闭 L1 特征匹配 |
| `AGENT_MOSS_DISABLE_LOGIC_RULES` | 关闭 L2 逻辑规则 |
| `AGENT_MOSS_DISABLE_LLM` | 关闭 L3 LLM |
| `AGENT_MOSS_LLM_TIMEOUT` | LLM 超时（秒） |
| `AGENT_MOSS_SCENARIO` | 部署级默认场景 ID |
| `AGENT_MOSS_DATA_DIR` | 数据目录 |
| `AGENT_MOSS_LOG_PATH` | LLM prompt 日志路径 |

## 开发与测试

```bash
AGENT_MOSS_DISABLE_LLM=1 python3 -m pytest -q
```

当前裁剪版回归结果：**1526 passed, 28 skipped**。

测试语料位于 `tests/cases/`，双端一致性 golden 数据位于 `tests/parity/`
（裁剪后只校验 Python 侧行为，不依赖 TypeScript 源码）。

## 目录结构

```
agent_moss/
├── cli.py              # agent-moss init / analyze / server / status / install / rules
├── engine/             # L1 / L2 / L3 检测与协调器
│   ├── coordinator.py
│   ├── heuristic.py
│   ├── logic_rules.py
│   ├── llm_analyzer.py
│   ├── skill_engine.py
│   └── formal_engine/
├── infra/              # 配置、LLM 客户端、日志、运行时配置
├── profiles/           # 平台特征（linux / windows / macos）
├── rules/              # 内置规则 JSON
├── skills/             # L3 Skill Markdown
├── agents/             # agent 专属扩展
├── scenarios/          # 场景定制扩展
├── server/             # FastAPI 服务与路由
└── console/            # 运行时控制台
tests/                  # 回归测试与用例
config/                 # 示例配置
integrations/           # 集成示例（QwenPaw 插件）
```

## License

MIT
