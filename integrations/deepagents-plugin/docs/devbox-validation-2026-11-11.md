# devbox 验证记录（CubeSandbox 实例联通待完成）

验证环境：devbox `controller`，Linux ARM64，Python 3.12.16。验证代码来自 `codex/deepagents-aio` 分支，插件版本 0.6.0。引擎 0.17.6、Gate 0.9.0。真实 AIO 端使用 devbox 已有的 `aio-agentmoss:1.11.0-moss0.17.6-arm64` 镜像；脚本内检查来自该镜像，不是本轮重新构建的镜像。

## 已取得的证据

| 检查 | 结果及范围 |
| --- | --- |
| 插件完整 pytest | **213 passed**，0 failed、0 skipped；其中包含原有回归、新增 AIO 单元用例、wheel 用例，以及外部 Gate/Moss + 真实 AIO 镜像的联调用例。独立直连联调为 **9 passed**。模型使用仓库的 ScriptedModel，未调用外部模型 API。 |
| Gate V1 回归 | **240 passed、1 skipped**。 |
| Moss 引擎回归 | 在隔离的普通用户环境、独立挂载命名空间内为测试补齐 `/etc/hosts` 后，**3126 passed、138 skipped**。宿主机 `/etc` 未改。devbox 原生 root 环境中曾有 **13 failed、3113 passed、138 skipped**；普通用户但仍缺 `/etc/hosts` 时也有环境相关失败，故不把这些原始运行说成通过。 |
| Ruff | `ruff check src tests examples` 无错误。 |
| 交付包 | wheel 和 sdist 均构建成功；两个 wheel 内容测试通过，可选 `agent-sandbox==0.0.30` 元数据及 Apache-2.0 文本正确。本次验证的 wheel SHA-256 为 `ab757c494af64933eeb7d936e3807a31831359c62da6d70462abf4d5e81bc7d2`；验证时的 wheel 中生产源码与 devbox 工作区逐文件相同。 |

真实联调使用独立临时端口启动外部 Moss 与 Gate，没有覆盖现有服务。AIO shell 使用 devbox 上独立的 Docker 验证容器。覆盖了普通命令、非零退出码、文件读写/编辑/搜索和二进制传输、Gate 先拒绝而不执行、外部放行后镜像内 Moss 对脚本的拒绝、无效 Gate token 的 fail-closed、子代理的调用和审计、硬超时后拒绝继续使用会话、正常会话清理。脚本内容的拒绝用例只读取容器内 `/etc/shadow` 并丢弃输出；真实执行前由镜像内 L1.2 拦截。

恢复连接后再次运行 9 项真实 AIO 联调：首次 **7 passed、2 failed**，失败均为外部 Gate 请求超过插件默认的 5 秒超时而触发 fail-closed；Gate 日志中的单次分析耗时最高约 9.25 秒。仅在 devbox 验证配置中把 `gate.timeout_seconds` 设为 **30**，并使用全新的 Gate 数据目录重跑，得到 **9 passed（32.62 秒）**。因此在这台 devbox 上部署时应显式配置该超时；默认 5 秒在此机器负载下不稳定。插件生产源码未因本次复测修改。

实际 AIO shell 使用 PTY，会去掉 DeepAgents 原 shell grep 解析器所需的 NUL 分隔符。先新增回归用例并观察失败，再改用 AIO 结构化文件搜索接口；针对该用例的重跑、插件完整回归及真实文件搜索均通过。SDK 文件读取接口会省略末尾换行，文档和用例据其返回语义验证行内容，并以二进制下载接口验证原始字节。

验证源文件与回收的本地文件曾按 31 个 Python 文件的 SHA-256 清单逐项核对一致。其后没有修改插件实现，仅整理此报告和测试状态说明。

## 尚未通过的端到端步骤

**真实 CubeProxy → CubeSandbox 实例的路由和会话没有完成验证。** 从 CubeMaster 使用原有 AIO 模板创建沙箱，启动时报 `PortBindingFailed: context deadline exceeded`；创建新的 Moss rootfs 模板虽然返回 READY，但经 CubeAPI 创建沙箱得到 HTTP 500、`no more resource`，恢复连接后重试仍报相同错误；从历史模板存储请求重建副本时又报 `upsert template replica ... invalid connection`。这些错误发生在实例可被 AIO SDK 使用之前，所以 9 项联调证明的是插件与真实 AIO 镜像的行为，不能证明 CubeProxy 的实例路由已经可用。

后续需要先恢复 devbox 的模板/节点调度连接，创建一个已就绪的 `aio-agentmoss` CubeSandbox 实例，再把同一组联调参数指向实际 CubeProxy 和沙箱 ID，验证 200、403、409、503、超时与会话清理。验证时应确认镜像内 Moss 实际启用；CubeAPI 的健康响应本身不足以证明这一点。

## 清理和限制

恢复连接后已删除独立验证容器 `agentmoss-aio-verify-20261111`。本轮还创建了用于排障的模板 `tpl-01a51886c9334444ad34caf1`（READY），暂留用于恢复 CubeSandbox 调度后的实例联通验证。未改动 `/root/AgentMoss` 或现有生产 Gate/Moss 服务。

模型语义 L3、实际用户模型生成结果、独立后台子代理及未接入 Moss 的 AIO 镜像不在本次验证范围；现有 API 链路联调明确关闭了 LLM 检测。
