# 安全判定回归语料

`corpus.json` 保存固定输入及预期的安全判定、风险等级和风险类型。
`tests/test_parity_corpus.py` 调用 AgentMoss，逐条比较实际输出，帮助发现规则变化引起的行为差异。

## 覆盖内容

- 命令连接符及危险命令组合，包括管道、命令替换和换行。
- 字面文本、帮助查询、日志检索等应当放行的操作。
- 工具名称伪装、路径混淆和只读命令中的额外动作。
- Windows 服务、注册表和系统操作。
- 授权历史、工作目录、Agent 专属规则及场景规则。

检查同时比较 `decision`、`risk_level` 和 `risk_type`。
同样返回 `Deny` 的两次判定也可能有不同的风险分类，需要分别核对。

## 运行

按[开发与测试](../../README.md#开发与测试)准备并激活测试环境，在 `AgentMoss/` 组件目录执行：

```bash
AGENT_MOSS_ROOT="$PWD" AGENT_MOSS_DISABLE_LLM=1 python -m pytest \
  tests/test_parity_corpus.py tests/test_case_metadata.py -q
```

这些测试只调用分析引擎，不执行语料中的 Shell 命令。

## 用例元数据检查

`known_risk_type_drift.json` 记录用例声明的风险类型与实际分类之间已知的差异。
`tests/test_case_metadata.py` 检查 `tests/cases/l1/`、`l2/` 和 `agent/`，读取清单中的
`case`、`expected` 和 `python` 字段。

新增差异、差异内容改变，或者清单中记录的差异已消失，都会导致测试失败。
先判断是用例声明、规则行为还是规则优先级需要调整，再更新相应内容；不能直接放宽断言。

## 维护语料

固定语料是回归参照，仍需结合用例意图判断结果是否正确。
有意修改规则行为时，逐条检查受影响的判定、风险级别和分类，将审查后的预期结果与规则一起提交。

`scripts/gen_parity_corpus.py` 依赖额外的 TypeScript 工程，当前项目无法独立运行该生成器。
日常验证使用上述 Python 测试，不需要安装 Node.js。不要为了消除测试失败而批量覆盖预期结果。
