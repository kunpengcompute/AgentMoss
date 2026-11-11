# 商业版 main 同步记录

核对日期：2026-11-11。

- 原分支：`init_branch`。
- 同步前备份：`backup/pre-commercial-sync-20261111`，提交 `3ebf568`。包含当时已跟踪文件和未忽略的新文件。
- AgentMoss 上游：`kenhkl/AgentMoss-commercial` 的 main，`c01b6eb52b5b36178e2aca52f22449b54d2e7e9e`。
- AgentGate 上游：`kenhkl/AgentGate-commercial` 的 main，`16ff75ecfaa1516fa922d072d1382f3a456e3b42`。
- 两个目录逐文件核对：AgentMoss 上游 1506 个文件、AgentGate 上游 69 个文件，除 README 修订外内容一致。额外保留 `AgentMoss/integrations/deepagents-plugin/`。
- 本地硬件加速扩展保存在备份分支，不加入同步后的代码。

## README 修订

总览改为 Python 运行时安全方案，移除当前版本不包含的硬件加速文档入口；标明上游来源和裁剪范围。补充真实分析验收、健康检查局限和 Console 鉴权说明。组件文档补充测试依赖、规则提取器所需的 `AGENT_MOSS_ROOT`，AgentGate 的引擎安装改为同仓库本地安装，避免安装到其他发行版本。

上游未提供 uv.lock。安装生成的锁文件用于本次验证，不随此次源码同步提交；首次使用先执行 README 的 `uv sync` 命令，再使用 `--locked`。

## 验证结果

- 根 README 的两条 `uv sync --project ... --python 3.12` 安装命令成功。
- AgentMoss CLI、AgentGate CLI 的 server 参数与 README 一致；AgentMoss Python 判定和策略生成示例均成功。
- 使用临时数据目录和自动分配的空闲端口启动两个真实服务（默认端口已有服务占用）；健康响应分别为 `0.17.6`、`0.9.0`，Gate 的 `moss_status=healthy`。
- 经 Gate 调用 Moss：`ls -la` 返回 Allow，`cat ~/.ssh/id_rsa` 返回 Deny / file_access；均非服务故障回退结果。
- Dashboard 的查询令牌换取 Cookie 后可访问；Console 页面返回 200。
- 四份 README 的本地文件链接全部存在；`git diff --check` 通过。
- AgentMoss：3126 passed、138 skipped。测试命令（仓库根目录）：

```bash
AGENT_MOSS_ROOT="$PWD/AgentMoss" AGENT_MOSS_DISABLE_LLM=1 \
  uv run --project ./AgentMoss --with pytest --with pytest-asyncio \
  python -m pytest -q AgentMoss/tests
```

- AgentGate：240 passed、1 skipped。

```bash
uv run --project ./AgentGate --with pytest --with pytest-asyncio \
  python -m pytest -q AgentGate/agent_gate/tests/v1
```

- 保留的 DeepAgents 插件：152 passed。

```bash
PYTHONPATH="$PWD/AgentMoss" AGENT_MOSS_DISABLE_LLM=1 \
  uv run --project AgentMoss/integrations/deepagents-plugin \
  --with pytest --with pytest-asyncio python -m pytest -q \
  AgentMoss/integrations/deepagents-plugin/tests
```

测试关闭 LLM，未使用模型凭据或调用外部模型。回归存在上游依赖弃用警告；跳过项未作为通过项统计。systemd 安装、真实模型 L3 判定未执行。
