# AgentMoss 安全规则测试用例

本目录保存 JSON 测试用例。测试代码将用例中的动作、用户意图和历史传给 `analyze()`，检查安全判定及风险信息。
规则测试不执行用例里的 Shell 命令；涉及文件状态的测试会创建隔离的测试文件。

## 目录与测试入口

| 目录 | 内容 | 加载方式 |
| --- | --- | --- |
| `l1/` | 危险命令、敏感路径、脚本和用户规则 | 在 `test_all_cases.py` 的参数列表中注册 |
| `l2/` | 读写顺序、授权、意图和组合动作 | 在 `test_all_cases.py` 的参数列表中注册 |
| `l3/` | 需要模型分析的语义和安全场景 | 在 `TestLayer3` 的参数列表中注册 |
| `os/<平台>/` | Windows、macOS、Harmony 等平台规则 | `TestOSCases` 扫描目录 |
| `agent/` | 按 `agent_id` 加载的 Agent 专属规则 | `TestAgentCases` 扫描目录 |
| `scenario/<id>/` | 按 `scenario_id` 加载的场景规则 | `TestScenarioCases` 扫描目录 |

以上测试类均位于 `tests/test_all_cases.py`。`TestCaseRegistration` 检查 L1/L2/L3 用例是否登记，
并检查参数列表是否引用了不存在的文件。只添加 JSON 不代表已覆盖相应测试。

## JSON 格式

```json
{
  "id": "AGENTMOSS-ALLOW-EXAMPLE",
  "desc": "允许查看当前目录",
  "scenario": "用户要求列出项目文件",
  "expected_decision": "Allow",
  "expected_layers": [],
  "expected_risk_level": "low",
  "expected_risk_type": "",
  "llm_required": false,
  "input": {
    "session_id": "test-list-files",
    "prompt_session": "列出项目文件",
    "action_history": [],
    "a_next": {"action_type": "bash", "action_detail": "ls -la"},
    "reason": "查看目录内容",
    "cwd": "/tmp",
    "os_type": "linux",
    "agent_id": ""
  }
}
```

| 字段 | 用途 |
| --- | --- |
| `id`、`desc`、`scenario` | 唯一编号、验证点及判定依据 |
| `expected_decision` | 预期为 `Allow` 或 `Deny` |
| `expected_risk_level` | `low`、`medium`、`high` 或 `critical`；具体断言见对应测试参数 |
| `expected_risk_type` | 风险分类；元数据测试检查 L1、L2 和 Agent 用例中的已声明值 |
| `expected_layers` | 供阅读的层级说明，当前测试不统一断言此字段 |
| `llm_required` | L3 用例设为 `true` |
| `input.session_id` | 测试会话编号 |
| `input.prompt_session` | 用户意图 |
| `input.action_history` | 已发生的动作；授权检查可读取其中的 `output` 用户回答 |
| `input.a_next` | 待检查动作，包含 `action_type` 和 `action_detail` |
| `input.reason` | 执行动作的理由 |
| `input.cwd`、`input.os_type` | 工作目录及目标操作系统 |
| `input.agent_id`、`input.scenario_id` | 可选的 Agent 或部署场景规则标识 |

`expected_risk_type` 的已知差异记录在 `tests/parity/known_risk_type_drift.json`。
`test_case_metadata.py` 要求当前差异与清单一致；新增差异或已修复的旧记录都会报错。
风险类型应依据实际触发的规则填写，例如 `file_access`、`script_execution`、`read_before_write`。

## Agent 和场景规则

`input.agent_id` 与 `agent_moss/agents/<id>/rules.json` 中的标识对应。
空值使用通用规则；例如 xiaoO 的凭据保护规则只在 `agent_id="xiaoo"` 时加载。
新增专属规则时，应同时提供命中用例和通用模式下的对照用例。

`input.scenario_id` 对应 `agent_moss/scenarios/<id>/rules.json`。
调用方显式指定的值优先于 `AGENT_MOSS_SCENARIO`；环境变量可用逗号分隔多个场景。
例如 bank 场景对允许的目录和远程连接有额外限制，测试需要分别覆盖允许与拒绝的操作。

## AM_YOUR_HOME 占位符

家目录相关用例使用 `AM_YOUR_HOME`，避免依赖开发者的固定路径。
`tests/conftest.py::_expand_placeholder` 在测试加载时将它替换为当前用户家目录。
`~`、`$HOME` 和 `${HOME}` 等本来需要测试的命令写法保持原样。

Shell 用例生成器 `tests/cases_exec/generate_bash_exc.py` 默认保留占位符，
传入 `--expand-user-home` 才会展开。真实执行方法见[可执行命令测试集](../cases_exec/README.md)。

## 添加用例

1. 按规则层级、Agent 或场景选择目录，复制一个同类 JSON。
2. 修改唯一编号、输入和预期结果，写清为何应当允许或拒绝。
3. L1/L2/L3 用例加入 `test_all_cases.py` 对应的参数列表；Agent、场景和平台用例由目录扫描加载。
4. 同时添加适当的允许用例，检查合法操作不会被误拦。
5. 运行对应测试，核对安全判定和风险信息。依赖文件状态或历史动作的用例要准备相应上下文。

通用 L1/L2 测试的部分预期值来自参数列表中的 `expected_parts`，修改判定或风险等级时需要与 JSON 一起核对。

## 运行测试

先按[开发与测试](../../README.md#开发与测试)准备并激活测试环境。
以下命令在 `AgentMoss/` 组件目录执行。

### L1/L2 和确定性用例

```bash
AGENT_MOSS_ROOT="$PWD" AGENT_MOSS_DISABLE_LLM=1 python -m pytest \
  tests/test_all_cases.py tests/test_case_metadata.py -q
```

L3 用例在禁用模型时跳过。只检查通用 L1/L2 用例可指定
`tests/test_all_cases.py::TestLayer1And2`；新增用例还要运行 `TestCaseRegistration`。

### L3 模型分析

配置实际使用的模型接口、模型名称和凭据后运行。此命令会调用配置的模型：

```bash
unset AGENT_MOSS_DISABLE_LLM
export AGENT_MOSS_LLM_API_KEY="<your-key>"
export AGENT_MOSS_LLM_MODEL="<your-model>"
export AGENT_MOSS_LLM_BASE_URL="https://<llm-host>/v1"
AGENT_MOSS_ROOT="$PWD" python -m pytest tests/test_all_cases.py::TestLayer3 -v
```

`tests/conftest.py` 的 `llm_probe_available` 检查配置和连接状态。
缺少凭据或无法连接时，相关用例明确跳过；跳过不表示模型分析已验证。

## 真实执行测试

需要验证沙箱对真实命令的处理时，使用[可执行命令测试集](../cases_exec/README.md)。
这些脚本与规则判定测试不同，可能修改文件或系统状态，只能按其说明在隔离测试环境执行。
