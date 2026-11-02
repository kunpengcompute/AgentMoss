# DeepAgents 环境变量配置调整

> 使用 executing-plans 在现有隔离 worktree 内实施。

**目标：** 默认不需要独立插件 JSON；应用可复用现有配置。
**设计：** GateRuntime.load() 读取 DEEPAGENTS_GATE_*；load(mapping) / load(GateConfig) 使用显式配置，不合并环境；显式 load(Path) 仅保留旧版兼容，不自动发现文件。支持 API_TOKEN 或 API_TOKEN_FILE，二者同时非空报错；启用却缺凭据报错。布尔和数值严格解析，Token 不进入 repr。默认判定策略及历史限额不变。
**范围：** config/runtime/client、受支持 Web 接线、部署/回滚/验收脚本、README 和现有设计。仅本机，不修改 VM。

- [x] 先增加环境配置、应用配置、凭据冲突和非法值的失败测试，以及已知旧 Web 的接线升级测试。
- [x] 实现 GateConfig.from_env/from_mapping、可选文件/直接 Token；GateRuntime.load 选择明确来源，GateClient 统一读取凭据。
- [x] Web 改 load()；已知旧 Web 仅改配置入口；验收脚本默认环境变量，部署脚本旧配置备份改为可选，不创建新配置文件。
- [x] 更新文档和示例，运行插件全套 pytest、Ruff、shell 语法检查；核对主目录备份哈希后同步并记录恢复方式。


验证：新增配置用例先运行失败，最初12项因接口缺失失败；Web接线2项先失败后修正。独立评审发现旧 config_path= 关键字兼容缺口，新增用例复现 TypeError 后修正并覆盖参数冲突。最终110项测试通过，Ruff/格式/shell/diff通过；仅保留原有langsmith弃用警告。默认故障策略不变，旧显式Path接口保留兼容；无VM变更。
