# AgentMoss 安全分析引擎

AgentMoss 接收 Agent 准备执行的动作、用户请求和调用历史，返回 `Allow` 或 `Deny` 及原因。既可以将它作为服务供 AgentGate 调用，也可以在本地用 CLI 分析一条命令，或在 Python 应用中直接调用。

> **本场景规则范围**：只内置 `bank` 场景规则；不包含 `xiaoo`、`opendesk` 的 agent 定制规则。

| 检测层 | 检查内容 |
| --- | --- |
| L1 特征检测 | 匹配命令和注入特征，检查脚本内容 |
| L2 行为检测 | 结合敏感路径、用户意图和历史动作检查风险，可识别跨会话的动作组合 |
| L3 模型分析 | 使用模型结合安全规则分析语义，需要配置模型接口和凭据 |

## 安装

接入 Agent 或部署 HTTP 服务，请从[项目快速开始](../README.md#快速开始)安装并启动 AgentMoss 和 AgentGate。只使用 CLI 或 Python 库时，仅需 Git、uv 和 Python 3.10 及以上版本。在仓库根目录执行以下命令。

```bash
uv venv --python 3.12 ./AgentMoss/.venv
uv pip install --python ./AgentMoss/.venv/bin/python -e ./AgentMoss
. ./AgentMoss/.venv/bin/activate
```

## 分析一条命令

下面的命令使用 L1、L2 规则，分析 `pwd` 是否可以执行。默认规则返回 `Allow`。传入 `--json` 可查看原因、风险级别和命中的检测层。

```bash
export AGENT_MOSS_DISABLE_LLM=1
agent-moss analyze 'pwd' --json
```

要带上用户请求、工作目录或会话历史，可以先生成示例文件，编辑其中的 `a_next` 等字段，再提交整份文件。生成模板会写入指定路径，请使用新文件名。

```bash
agent-moss init -o input.json
agent-moss analyze input.json --no-file
```

CLI 的退出码 `0` 表示允许，`1` 表示拒绝。`--no-file` 让文件模式只输出结果。省略这一选项时，CLI 还会把策略或拒绝原因写到当前目录。

## 在 Python 中使用

在上述虚拟环境中运行。把检查放在工具函数之前，并传入工具实际使用的工作目录。

```python
from pathlib import Path

from agent_moss.engine.analyzer import analyze

result = analyze(
    session_id="my-session-001",
    prompt_session="查看当前工作目录",
    action_history=[],
    a_next={"action_type": "bash", "action_detail": "pwd"},
    reason="显示工作目录",
    cwd=str(Path.cwd()),
)
if result["decision"] != "Allow":
    raise PermissionError(result["reason"])
print(result["decision"])
```

只需要判定时也可以使用 `agent_moss.engine.analyzer.judge_security`，传入 `get_default_config()` 返回的配置，读取结果的 `allowed`、`risk_level` 和 `reason`。嵌入 FastAPI 时可使用 `agent_moss.server.app.create_app()`。

## HTTP API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/health` | 查看状态、版本和实际监听端口。`degraded` 列出降级原因 |
| POST | `/api/v1/analyze` | 分析待执行的动作 |
| POST | `/api/v1/review` | 回顾多个会话，检查动作组合 |
| GET | `/metrics` | 获取 Prometheus 指标 |
| GET | `/console` | 在浏览器查看规则和运行配置 |

直连引擎时，请使用 AgentMoss 令牌。默认首次启动会生成 `~/.config/agentmoss/auth_token`，设置了 `AGENT_MOSS_DATA_DIR` 时则位于该目录。以下示例使用同机端口和默认鉴权。

```bash
MOSS_TOKEN=$(cat "${AGENT_MOSS_DATA_DIR:-$HOME/.config/agentmoss}/auth_token")
curl -fsS http://127.0.0.1:9090/api/v1/analyze \
  -H "Authorization: Bearer $MOSS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"list-dir-1","prompt_session":"查看当前工作目录","a_next":{"action_type":"bash","action_detail":"pwd"}}'
```

### 分析请求与响应

`session_id` 和 `a_next` 必填。`a_next.action_type` 填动作类型，如 `bash`，`a_next.action_detail` 填工具即将执行的内容。根据应用掌握的上下文还可以提供以下字段。

| 字段 | 说明 |
| --- | --- |
| `prompt_session`、`reason`、`cwd` | 最近一条用户请求、执行理由、工具工作目录 |
| `agent_id`、`instance_id`、`os_type` | Agent 类型、安装实例、工具所在的操作系统 |
| `action_history` | 已发生的动作，每项含 `action_type`、`action_detail`，可附 ISO 8601 时间 `called_at` 和工具输出 `output` |
| `prompt_history` | 之前各轮的用户请求 `text` 和对应动作列表 `actions` |
| `session_history` | 其他会话的 `session_id` 和 `action_history` |
| `metadata` | 可选扩展，如本次请求的模型配置 |

响应中的 `decision` 决定是否放行。`reason` 是判定依据，`risk_level`、`risk_type` 和 `violated_layers` 分别给出风险级别、类别和命中的检测层。`l3_outcome` 表示本次模型分析结果，受限的检测能力会列在 `degraded` 中。组合风险还可附带 `violation_chain` 和 `violation_graph`。完整字段见[接口模型](agent_moss/server/models.py)。

`POST /api/v1/review` 可以直接接收 `sessions`，格式与 `session_history` 相同，也支持通过 `db_path` 读取服务端允许访问的 AgentGate SQLite 文件或记录目录。响应中的 `verified` 表示验证结果，`violations` 列出发现的问题。

指定 `db_path` 时，AgentMoss 优先读取该路径。文件不存在时，有有效动作的 `sessions` 可作为备用历史，否则返回 `404`。可正常读取的空库返回 `200`，有备用历史时使用备用历史。路径超出允许目录返回 `403`，库损坏等读取错误返回 `500`。

默认的控制台地址为 `http://127.0.0.1:9090/console/`。页面提示鉴权时，输入运行该服务的 AgentMoss 令牌。

## 配置

服务配置示例见 [agent_moss.yaml](config/agent_moss.yaml)。通过 `AGENT_MOSS_CONFIG_PATH` 指定 JSON 或 YAML 文件。默认配置位于数据目录下，模型地址、名称和密钥可通过进程环境变量设置。

| 环境变量 | 用途 |
| --- | --- |
| `AGENT_MOSS_DATA_DIR` | 运行账户可读写的数据目录，默认为 `~/.config/agentmoss` |
| `AGENT_MOSS_AUTH_TOKEN`、`AGENT_MOSS_AUTH_TOKEN_FILE` | 显式指定服务令牌或令牌文件，供直接调用方和 AgentGate 使用 |
| `AGENT_MOSS_AGENT_TOKENS` | 按 Agent 分配令牌，格式为 `agentA=令牌A,agentB=令牌B` |
| `AGENT_MOSS_GATE_AUTH_TOKEN` | 可选的 AgentGate 专用令牌，仅用于 `/api/v1/analyze` 和 `/api/v1/review`；默认关闭，启用时与 AgentGate 的 `AGENT_GATE_MOSS_GATE_TOKEN` 取相同值，且不能与全局、Agent、Console 令牌相同；`AGENT_MOSS_AUTH_MODE=off` 时不可启用 |
| `AGENT_MOSS_DISABLE_LLM` | 设为 `1` 时只用本地规则 |
| `AGENT_MOSS_LLM_BASE_URL`、`AGENT_MOSS_LLM_MODEL`、`AGENT_MOSS_LLM_API_KEY` | 模型服务的 OpenAI 兼容 API 地址、模型名和密钥 |
| `AGENT_MOSS_LLM_FAIL_MODE` | L3 调用失败或被硬预算跳过时，默认 `fail_open` 会放行通过前两层的动作，设为 `fail_closed` 则拒绝这次调用 |
| `AGENT_MOSS_SCENARIO` | 为部署选择一组场景规则 |
| `AGENT_MOSS_RULES_MODE` | `hot`（默认）支持控制台调整，`cold` 在启动时读取规则，`builtin` 只使用内置规则 |

启用 L3 时，在启动 AgentMoss 的环境中设置模型地址、名称和密钥，取消 `AGENT_MOSS_DISABLE_LLM=1` 并重启进程。在客户端或另一个终端修改环境变量不会改变运行中的服务。鉴权文件不可读或自动令牌创建失败时，服务会报告错误并拒绝启动，请检查运行账户和数据目录权限。

Gate 专用身份是可选的：AgentMoss 设置 `AGENT_MOSS_GATE_AUTH_TOKEN`，AgentGate 设置相同值的 `AGENT_GATE_MOSS_GATE_TOKEN`，两边一致才生效。它用于区分 Gate 的 analyze/review 调用并启用 call-id 关联，不是隔离边界；普通令牌仍可调用其他接口。`AGENT_MOSS_AUTH_MODE=off` 与 Gate 专用身份互斥，同时配置会拒绝启动。

当前用户请求和脚本分析报告进入 L3 时受各自的字符上限约束。设置正数 `AGENT_MOSS_LLM_MAX_PROMPT_TOKENS` 可启用估算预算，默认在裁剪后仍超预算时跳过 L3 安全判断。配置项和响应含义见 [L3 Prompt 预算配置](docs/deploy/ENV_VARS.md) 和 [L3 Prompt 接入说明](docs/integrations/INTEGRATION_GUIDE.md)。

## 扩展规则

在已激活的环境中执行以下命令，生成模板并查看当前目录的规则。将 `AGENT_MOSS_EXTRA_RULES_DIR` 同样设置在启动检测服务的环境中。

```bash
export AGENT_MOSS_EXTRA_RULES_DIR="$PWD/moss-rules"
agent-moss rules init --dir "$AGENT_MOSS_EXTRA_RULES_DIR"
agent-moss rules status
```

在 `extra.json` 中追加规则，在 `override.json` 中替换指定类别。字段及示例见[规则模板](agent_moss/data/rules_template/README.md)。大量命令规则可以启用[预筛加速](docs/预筛加速使用说明.md)。

## 许可证

[MIT](LICENSE) · [Deep Agents 插件第三方声明](integrations/deepagents-plugin/THIRD_PARTY_NOTICES.md)
