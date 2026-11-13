# AgentMoss 安全规则测试用例

本目录保存引擎的 JSON 测试用例。每个用例声明输入、期望判定和风险信息，由测试加载器调用 `analyze()` 检查。

## 一、目录结构

| 子目录 | 内容 |
| --- | --- |
| `l1/` | 危险命令、敏感路径、脚本和用户规则 |
| `l2/` | 读后写、意图一致性、授权、间接访问和组合动作 |
| `l3/` | 需要模型分析的工具语义和安全场景 |
| `os/<平台>/` | Windows、macOS、Harmony 等平台规则 |
| `agent/` | 按 `agent_id` 加载的 Agent 专属规则 |
| `scenario/<id>/` | 按 `scenario_id` 加载的场景规则 |

真实执行命令的环境和使用方法见 [cases_exec 说明](../cases_exec/README.md)。

### Agent 专属规则用例（`agent/` 子目录）

**为什么单独建目录**：`l1/l2/l3` 的归一化用例跑**通用模式**（`agent_id` 空），不加载任何 agent 专属规则。而 xiaoo/opendesk 的定制规则（读 `xiaoo.env`、guardian 目录写保护、绕过审计注入词、opendesk 白名单工具）**只对特定 `agent_id` 生效**，通用模式覆盖不到，故独立成 `agent/` 子目录。

**关键机制**：每个 agent 用例的 `input.agent_id` 指定加载哪个 agent 的专属规则，空串 = 通用模式（不加载 agent 规则）。双端加载器都支持：
- TS `json-cases.test.ts` 自动扫 `agent/` 目录，`toAnalysisInput` 把 `agent_id` → `agentId` 传给引擎。
- Python `test_all_cases.py::TestAgentCases` 自动扫 `agent/` 目录，`_run_case` 把 `agent_id` 传给 `analyze()`。

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
所有请求生效；可逗号分隔多场景）。双端加载器都支持：
- TS `json-cases.test.ts` 递归扫 `scenario/` 目录，`toAnalysisInput` 把 `scenario_id` → `scenarioId`。
- Python `test_all_cases.py::TestScenarioCases` 自动扫 `scenario/<id>/*.json`，`_run_case` 把
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

## 二、JSON 用例字段说明（人工添加用例必读）

> 本节是**唯一**的字段权威说明。人工添加用例时，按本表填字段即可，不用猜。
> 每个 JSON 是两层结构：**顶层字段** + 嵌套的 **`input` 对象**。

### JSON 结构总览

```json
{
  "id": "AGENTMOSS-ALLOW-01",          // 顶层：用例标识
  "desc": "一句话描述",
  "scenario": "场景背景（为何应/不应拦截）",
  "expected_decision": "Allow",        // Allow | Deny  ← 两端都断言
  "expected_layers": [],
  "expected_risk_level": "low",        // 可选
  "expected_risk_type": "",
  "llm_required": false,               // 仅 l3/ 用例需要 true
  "note": "补充说明（可选）",
  "input": {
    "session_id": "test-xxx",
    "prompt_session": "",              // 可选
    "action_history": [],              // [{action_type, action_detail}]
    "a_next": {"action_type": "bash", "action_detail": "..."},
    "reason": "执行理由",
    "cwd": "",                         // 可选
    "os_type": "linux",                // 可选
    "agent_id": ""                     // 可选
  }
}
```

### 顶层字段

| 字段 | 必填 | 含义 | 测试是否断言 |
|---|---|---|---|
| `id` | ✅ | 唯一标识，格式 `AGENTMOSS-{DENY,ALLOW,LOGIC,LLM,EDGE,AGENT-XIAOO,AGENT-OPENDESK}-NN`（agent 用例用 `AGENT-<AGENTID>-NN`）| 否（仅展示）|
| `desc` | ✅ | 一句话描述该用例验证什么 | 否 |
| `scenario` | ✅ | 场景背景，说明为何应/不应拦截（供 reviewer 理解意图）| 否 |
| `expected_decision` | ✅ | 期望判定：`Allow` 或 `Deny` | **是（Python + TS 两端）** |
| `expected_layers` | ✅ | 期望命中的层（**参考性**，见下方层编号；当前测试**不自动断言**它）| 否 |
| `expected_risk_level` | 可选 | 期望风险等级：`low`/`medium`/`high`/`critical` | **是（仅 L1/L2 用例，TS 端）** |
| `expected_risk_type` | 可选 | 期望风险类型（见取值表）| **是（`l1/ l2/ agent/` 两端；已知偏差清单豁免）** |
| `llm_required` | L3 必填 | `true` 表示该用例需 LLM 真跑（只能配在 `l3/` 目录）| 控制 L3 gate |
| `note` | 可选 | 补充说明 / 历史修正记录 | 否 |

> **断言范围说明**：
> - `expected_decision`：两端都断言。
> - `expected_risk_level`：L1/L2 用例断言（TS 端 + Python 端；L3 有非确定性，不断言）。
> - `expected_risk_type`：执行断言（`tests/test_case_metadata.py` /
>   `ts/src/__tests__/json-cases.test.ts`），范围限 `l1/ l2/ agent/`（确定性判定）。
>   已知偏差登记在
>   `tests/parity/known_risk_type_drift.json`，偏差集合必须与清单**完全一致**：
>   新漂移 → 失败；清单过期 → 失败并提示删除。清理历史债时从清单删对应条目即可。
> - `expected_layers`：**仍不断言**（层标签写法不统一，且多余命中时 `violated_layers`
>   内容不稳定）。填它仍是好习惯，但别指望测试帮你抓错。

### input 对象字段

| 字段 | 必填 | 含义 |
|---|---|---|
| `session_id` | ✅ | 会话唯一标识 |
| `prompt_session` | 可选 | 用户输入的原始 prompt。**L3/L2 意图判断的关键输入**（如"统计磁盘" vs 实际 `cp -r` 外传的偏离检测）|
| `action_history` | ✅ | 历史动作数组，元素为 `{action_type, action_detail}`。**read_before_write 等 L2 规则依赖它** |
| `a_next` | ✅ | 下一步动作，`{action_type, action_detail}`，**这是被测的核心动作** |
| `reason` | ✅ | 本次执行的理由（LLM 判意图时参考）|
| `cwd` | 可选 | 工作目录。**indirect_file_access 用例需要**（加载器会注入含 `.env` 凭据的临时目录）|
| `os_type` | 可选 | `linux` / `windows`（默认 `linux`；当前无用例显式设置）|
| `agent_id` | 可选 | 代理唯一标识（`xiaoo`/`opendesk` 等，加载该 agent 专属规则；默认空 = 通用模式，不加载 agent 规则）。**`agent/` 子目录用例必填**，值须与 `agent_moss/agents/<id>/rules.json` 的 `agent_id` 一致 |
| `scenario_id` | 可选 | 场景唯一标识（`bank` 等，加载该场景定制规则；默认空 = 通用模式，不加载场景规则；空时还可由环境变量 `AGENT_MOSS_SCENARIO` 兜底）。**`scenario/<id>/` 子目录用例必填**，值须与 `agent_moss/scenarios/<id>/rules.json` 的 `scenario_id` 一致 |

### `AM_YOUR_HOME` 占位符（路径可移植性）

**背景**：用例里硬编码 `/home/me`、`/home/user` 等绝对路径，客户在别的机器跑时路径不存在，命令无法真实执行；且 `/home/xxx` 写死了用户名，不通用。

**规则**：涉及用户家目录的路径，**统一写成 `AM_YOUR_HOME` 占位符**（裸词，不带 `$`），例如：
- `python3 AM_YOUR_HOME/project/build.py`（原 `/home/me/project/build.py`）
- `rm -rf AM_YOUR_HOME`（原 `rm -rf /home/user`）
- `cd AM_YOUR_HOME && rm -rf *`（原 `cd /home/me && rm -rf *`）

**为什么不用 `~` / `$HOME` / `${HOME}`**：这些可能是**被测的关键词**（引擎要识别 `~/.bashrc`、`$HOME/config` 等路径语义），直接占坑会与测例冲突。`AM_YOUR_HOME` 是自定义裸词，不与任何被测关键词冲突。

**加载器展开**：双端加载器读到 `AM_YOUR_HOME` 后，展开成**当前用户家目录的绝对路径**（如 `/home/hkl`）再传给引擎：
- Python `tests/conftest.py::_expand_placeholder`（`os.path.expanduser("~")`）
- TS `ts/src/__tests__/json-cases.test.ts::expandPlaceholder`（`os.homedir()`）

**判定稳定性**：`AM_YOUR_HOME` 展开成绝对路径后，引擎判定与原来的 `/home/me`、`/home/user` **完全一致**（这些路径本身不敏感，判定不依赖具体用户名）。**注意**：不能展开成 `~` —— `rm -rf ~` 不含 `/`，不命中 L1 的 `rm -rf` 正则会变 Allow；展开成绝对路径 `/home/xxx` 才命中 Deny。

**生成器**：`tests/cases_exec/generate_bash_exc.py` 生成 allow.sh/deny.sh 时，默认**保留 `AM_YOUR_HOME` 原样**输出，加 `--expand-user-home` 时展开成**当前用户家目录绝对路径**。详见 `tests/cases_exec/README.md`。

**哪些路径不用改**：`~`、`$HOME`、`${HOME}` 开头的路径（如 `cat ~/.ssh/id_rsa`）**保持原样**——它们本身可移植，引擎已稳定处理，且是潜在被测关键词。

### `action_type` 取值（`a_next` / `action_history` 元素）

| 取值 | 说明 | 出现次数 |
|---|---|---|
| `bash` | shell / 命令执行 | 286 |
| `write` | 写文件 | 5 |
| `read` | 读文件 | 3 |
| `skill` | 技能安装/调用 | 2 |
| `email` | 邮件操作 | 2 |
| `custom_tool` | 自定义工具调用 | 2 |
| `browser_navigate` | 浏览器导航 | 2 |
| `search-WebSearch` | 联网搜索 | 1 |
| `grep` | 文本搜索 | 1 |
| `memory-addMemory` | 记忆写入 | 1 |
| `mailbox-getEmailList` | 邮箱列表 | 1 |

### `expected_risk_type` 取值（27 种）

`file_access`(125)、`script_execution`(64)、`sensitive_action`(23)、`supply_chain`(18)、`privilege_escalation`(16)、`resource_exhaustion`(12)、`lateral_movement`(12)、`data_exfiltration`(9)、`persistence`(6)、`network_access`(5)、`read_before_write`(5)、`prompt_injection`(4)、`config_tampering`(3)、`intent_deviation`(3)、`combo_chain`(2)、`consent_missing`(2)、`indirect_file_access`(1)、`formal_verification`(1)、`dangerous_redirect`(1)、`dangerous_wildcard_operation`(1)、`indirect_access`(1)、`skill_installation`(1)、`email_operation`(1)、`general_tool_risk`(1)、`browser_web_access`(1)、`persistence_backdoor`(1)、`defense_evasion`(1)

> 注：括号里是**用例数量快照**，随用例增减而过期，别当权威统计。
> `consent_missing` 目前**不出现在任何用例的 `expected_risk_type` 里** ——
> 改密 / 删用户的 L1 规则（`\buserdel\s`、`\|\s*passwd\b` 等）先于 L2 授权规则命中，
> 实际 `risk_type` 是 `script_execution`。L2 授权规则作为**纵深防御**保留（控制台
> 关掉 L1 规则时兜底），其判定强度由 `tests/test_consent_strength.py` /
> `ts/src/__tests__/consent-strength.test.ts` 直接覆盖。

### `expected_layers` 层编号系统

| 层编号 | 对应引擎层 | 含义 |
|---|---|---|
| `1.1` | L1 特征匹配 | 危险命令 / 敏感路径 / 横向移动 / 提权等静态规则短路 |
| `1.2` | L2 逻辑规则 + 内联脚本 | read_before_write / 意图一致 / 提权 / 授权 / 间接访问 / 通配 / 重定向 / 内联脚本分析 |
| `1.3` | L3 LLM 语义 | LLM 深度语义判断（意图偏离、供应链、钓鱼等）|

> 注：L3 用例的 `expected_layers` 写法不统一（`["3"]` / `[]` / `["1.1"]` / `["1.3"]` 都有），且不被断言。**新增 L3 用例建议写 `[]` 或 `["3"]`**，不要纠结层号。

---

## 三、人工添加测试用例完整流程

**核心原则：新增用例只改一个 JSON，双端自动读取并跑，无需改任何测试代码。**

### 步骤

1. **按层选目录**
   - 静态规则（危险命令/敏感路径/提权/写文件等）→ `tests/cases/l1/`
   - 逻辑规则（read_before_write/意图/通配/重定向等）→ `tests/cases/l2/`
   - 需 LLM 语义判断（意图偏离/供应链/钓鱼/反弹 shell 等）→ `tests/cases/l3/`

2. **复制现有 JSON 作模板**（比从零写更不容易漏字段）
   ```bash
   # 加一个 Deny 用例：复制目录内现有 deny 用例
   cp tests/cases/l1/deny-01-chmod-777.json tests/cases/l1/deny-99-my-new-case.json
   ```

3. **按上一节字段表填内容**
   - `id` 改为 `AGENTMOSS-DENY-99`
   - `desc` / `scenario` 写清验证点
   - `expected_decision` 判 Allow 还是 Deny（对照上节断言说明）
   - `expected_risk_level` / `expected_risk_type` 按场景填（L1/L2 会被断言 risk_level）
   - `input.a_next.action_detail` 填**要测的命令/动作**（核心）
   - 若规则依赖历史动作（如 read_before_write），填 `input.action_history`

4. **双端自动跑验证**（新建 JSON 两端自动加载）
   ```bash
   # Python（禁 LLM，L3 会 skip）
   AGENT_MOSS_DISABLE_LLM=1 python3 -m pytest tests/test_all_cases.py -q

   # TS（禁 LLM）
   cd ts && AGENT_MOSS_DISABLE_LLM=1 npx vitest run src/__tests__/json-cases.test.ts
   ```

5. **L3 用例特殊要求**
   - 必须在 `tests/cases/l3/` 目录
   - 必须设 `"llm_required": true`
   - 需 LLM 判定源可用（env 或 `~/.config/agentmoss/config.json` 的 key）+ 连通性探测通过才真跑验证；否则 skip

### 设计要点（写用例时注意）

- **Deny 用例**：`expected_decision: "Deny"`，且最好能命中**更早的层**（L1 → L2 → L3 优先级），越早拦截越安全。
- **Allow 用例**：`expected_decision: "Allow"`，用于防误报（合法运维不该被拦）。**L1/L2 用例会被强制禁 LLM 测试**，只测静态结果。
- **对照用例**：强烈建议对每种风险配一个对应 Allow 对照（如 `chmod 777` Deny ↔ `chmod 644` Allow），防止规则过宽。
- **risk_level**：L1/L2 用例被 TS 断言 risk_level，必须与引擎实际判定一致（不确定就跑一次看实际值再填）。
- **action_detail 构造**：用真实命令，注意 `2>/dev/null`、`-o /dev/null`、引号内字面量等边界（这些是历史 FP/FN 重灾区，见 `docs/MAINTENANCE_WORKFLOW.md`）。

---

## 四、场景分类

| 场景 | 内容 |
| --- | --- |
| 敏感凭据访问 | 私钥、密码和凭据配置，以及公开文件的放行对照 |
| 数据外泄 | curl、wget、scp 等读取并发送数据的操作 |
| 持久化后门 | shell 启动配置、定时任务和认证配置写入 |
| 破坏性操作 | 删除、磁盘写入、数据库破坏和资源耗尽 |
| 用户与权限管理 | 账户修改、提权和危险权限设置 |
| 远程代码执行 | 下载后执行、解码执行和写入后执行 |
| 横向移动 | 远程连接、扫描和隧道 |
| 系统配置修改 | 防火墙、系统目录和服务配置 |
| 文件操作 | 文件写入、重定向和间接访问 |
| 意图与调用历史 | 动作与用户意图、授权和读写顺序的关系 |
| 供应链风险 | 软件包、Skill 和安装来源 |
| 浏览器与邮件 | 站点导航、邮件发送和删除 |
| 工具参数注入 | Shell 注入、命令替换和通用工具参数 |

## 五、运行测试

### L1+L2（禁用 LLM）

```bash
python -m pytest tests/test_all_cases.py::TestLayer1And2 -v
```

### L3（需要模型配置）

L3 是否真跑由**统一判定源 + 连通性探测**决定（Python `tests/conftest.py` 的 `llm_probe_available` fixture / TS `config.ts` 的 `probeLLMConnection()`）：
- 判定源：`AGENT_MOSS_LLM_API_KEY` env → 否则 `~/.config/agentmoss/config.json` 的 `llm.api_key`
- 有 key 再发一次 `max_tokens=1` ping，连通才真跑；key 无效/网络不通 → 明确提示 + skip（不假通过）

```bash
# key/地址/model 可来自 env 或 ~/.config/agentmoss/config.json，无需手动传
python -m pytest tests/test_all_cases.py::TestLayer3 -v

# 若需覆盖 config.json，显式传环境变量
export AGENT_MOSS_LLM_API_KEY="<your-key>"
export AGENT_MOSS_LLM_MODEL="GLM-5.2"
export AGENT_MOSS_LLM_BASE_URL="http://<llm-host>/v1"
```

平台用例位于 `os/<平台>/`，子目录名对应输入的 `os_type`。Shell 命令提取器不包含这些平台用例。

## 六、真实执行环境

`tests/cases/` 的 JSON 是**引擎判定基准**（文本规则，不依赖文件真实存在）。若要**真实执行**这些命令（对比"无 AgentMoss vs 有 AgentMoss"的拦截率），见 **`tests/cases_exec/README.md`**——那里有完整的可执行命令测试集（prep.sh / run_all.sh / clean.sh / 生成器 / 恶意样本 / 提取规则 / 安全边界）。
