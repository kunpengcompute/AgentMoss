# tests/cases — AgentMoss 安全分析引擎基准测试集

> 本文档记录 `tests/cases/` 下全部测试用例的统计与实测通过率数据。
> 数据口径：对 430 个用例逐一调用 `agent_moss.engine.analyzer.analyze()`，比较实际 `decision` 与用例期望 `expected_decision`。
> 复现命令见文末。

---

## 一、用例总量

`tests/cases/` 共 **630** 个测试用例，其中 **Deny 358 个、Allow 272 个**（2026-10 修复批次后重算）。

按子目录分层（对应 AgentMoss 三层规则 + agent 专属规则 + 场景定制规则）：

| 子目录 | 层级 | 用例数 | 说明 |
|---|---|---|---|
| `l1/` | L1 特征匹配 | 453 | 危险命令正则 + 敏感路径 + 内联脚本 + 脚本分支感知（case $1 / python sys.argv） + 用户规则 |
| `os/<平台>/` | **平台专属规则** | 57 | `os/windows/`(43) + `os/macos/`(7) + `os/harmony/`(7)：子目录名即 `input.os_type`。**客户交付集默认排除**（客户沙箱是麒麟/Linux，平台用例在此判定必然失败） |
| `l2/` | L2 逻辑规则 | 46 | read_before_write / 意图一致 / 提权 / 授权 / 间接访问 / 通配 / 重定向 / 组合攻击链（combo_chain）/ 跨会话组合链（formal_engine） |
| `l3/` | L3 LLM 语义 | 28 | security skill 命中+放行，需有效 LLM key 真跑 |
| `agent/` | Agent 专属规则 | 9 | xiaoo/opendesk 的 extra_* / builtin_safe_* 看护（见下节） |
| `scenario/<id>/` | 场景定制规则 | 37 | 客户沙箱部署级约束（如 bank 的目录白名单/禁 ssh），见下节 |

> **配套真实执行环境**：`tests/cases_exec/`（与 `tests/cases/` 并列）把这里的命令转成**客户可真实执行**的版本（建文件/目录、起本地服务、替换 `AM_YOUR_HOME`），用于对比"无 AgentMoss vs 有 AgentMoss"的拦截率。详见 **`tests/cases_exec/README.md`**。

### Agent 专属规则用例（`agent/` 子目录）

**为什么单独建目录**：`l1/l2/l3` 的归一化用例跑**通用模式**（`agent_id` 空），不加载任何 agent 专属规则。而 xiaoo/opendesk 的定制规则（读 `xiaoo.env`、guardian 目录写保护、绕过审计注入词、opendesk 白名单工具）**只对特定 `agent_id` 生效**，通用模式覆盖不到，故独立成 `agent/` 子目录。

**关键机制**：每个 agent 用例的 `input.agent_id` 指定加载哪个 agent 的专属规则，空串 = 通用模式（不加载 agent 规则）。Python 加载器：
- `test_all_cases.py::TestAgentCases` 自动扫 `agent/` 目录，`_run_case` 把 `agent_id` 传给 `analyze()`。

**用例看护的两类场景**：
1. **命中（Deny）**：agent 专属规则拦截特定操作（如 xiaoo 读 `xiaoo.env`、guardian 目录写）。
2. **对照（Allow）**：同一操作在**无 agent_id** 时不拦（防 agent 规则污染通用模式）；或 agent 专属规则不误伤常规操作（防规则过宽）；或 opendesk 白名单只豁免白名单工具、不豁免危险命令。

**易错点**：`agent_id` 必须与 `rules.json` 的 `agent_id` 一致（`xiaoo`/`opendesk`）。Python `conftest.py` 的 `_clear_agent_cache` autouse fixture 每个用例清理 agent 扩展缓存，防止用例间串扰。

### 场景定制规则用例（`scenario/<id>/` 子目录）

**为什么单独建目录**：场景规则是**客户沙箱部署级约束**（如招行 bank：允许读写 `/home/sandbox`、
`/opt/sandbox`、`/tmp`，其他根目录禁止读写；禁 ssh 命令），只对特定 `scenario_id` 生效，
通用模式覆盖不到，故独立成 `scenario/<id>/` 子目录（子目录名即 scenario_id，如
`scenario/bank/*.json` → `scenario_id="bank"`）。

**关键机制**：每个场景用例的 `input.scenario_id` 指定加载哪个场景的定制规则，空串 = 通用模式。
启用判定：调用方显式 `scenario_id` > 环境变量 `AGENT_MOSS_SCENARIO`（部署级，客户沙箱设一次，
所有请求生效；可逗号分隔多场景）。Python 加载器：
- `test_all_cases.py::TestScenarioCases` 自动扫 `scenario/<id>/*.json`，`_run_case` 把
  `scenario_id` 传给 `analyze()`。

**用例看护的两类场景**（同 agent，一拦一放）：
1. **命中（Deny）**：场景规则拦截特定操作（如 bank 的 `cat /etc/passwd` 命中目录白名单外、
   `ssh root@10.0.0.5` 命中禁 ssh）。
2. **对照（Allow）**：允许目录内操作不拦（`cat /home/sandbox/data.txt`）；**无 scenario_id**
   时不拦（防场景规则污染通用模式，如 scenario-bank-07）。

**场景规则定义**：`agent_moss/scenarios/<id>/rules.json`（与 agent rules.json 同构，顶层 id
字段是 `scenario_id`）。场景规则也走 runtime_config 的 `scenario_rules` 分区（console「场景定制」
页在线编辑，env 未设时不显示不启用）。

**易错点**：`scenario_id` 必须与 `agent_moss/scenarios/<id>/rules.json` 的 `scenario_id` 一致。
`conftest.py` 的 `_clear_agent_cache` autouse fixture 同时清理 agent 与场景扩展缓存，防止
用例间串扰。

**测试口径**：`tests/cases/` 下的 JSON 用例由 Python 测试加载（`test_all_cases.py` 及按目录自动扫描的 agent / scenario / os 用例）。

裁剪版回归结果：

```bash
AGENT_MOSS_DISABLE_LLM=1 python3 -m pytest -q
# 1526 passed, 28 skipped
```

L3 LLM 用例在禁用 LLM 时 skip；判定口径以 Python 单端为准。
