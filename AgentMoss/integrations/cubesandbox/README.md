# 在 CubeSandbox 中使用 AgentMoss

AIO 是沙箱中提供命令执行和文件读写的服务。将 AgentMoss 装入 AIO 镜像后，应用通过 AIO shell 接口提交的命令及其引用的脚本会在沙箱内接受检查。检查通过后，AIO 才会调用 Bash。沙箱内使用内置规则，无需模型账号或单独的 AgentGate 服务。

如果还需要根据用户请求和调用历史检查 Deep Agents 的工具调用，先按本文创建沙箱，再[连接 Deep Agents](../deepagents-plugin/docs/cubesandbox.md)。

## 构建镜像和模板

准备一台装有 Docker 的 Linux ARM64 构建机，以及一个可用的 CubeSandbox 集群。本文使用 AIO 1.11.0、Python 3.12 和 CubeSandbox 0.7.3 的管理命令。首次安装集群可参考 [CubeSandbox 快速入门](https://docs.cubesandbox.com/zh/guide/quickstart)。

在构建机的 AgentMoss 仓库根目录运行以下命令。将 `IMAGE` 换为构建机可推送、CubeMaster 可拉取的镜像地址，为每次发布指定新标签。

```bash
IMAGE=registry.example.com/agents/aio-agentmoss:1.11.0-r1
docker build -f AgentMoss/integrations/cubesandbox/image/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"
```

[Dockerfile](image/Dockerfile) 固定了基础镜像的摘要。Docker 需要能拉取该镜像，或已在本机缓存同一摘要。构建环境需要访问 Python 包索引。使用内部镜像源时，在 `docker build` 中加入 `--build-arg PIP_INDEX_URL=https://pypi.example.com/simple`，将地址换为构建容器可访问的索引。若提示 shell 入口不匹配，先核对基础镜像版本。

在 CubeSandbox 控制节点使用同一个镜像地址运行以下命令。它会开放 AIO 端口 8080 和管理进程 envd 的端口 49983，并等待模板构建结束。

```bash
IMAGE=registry.example.com/agents/aio-agentmoss:1.11.0-r1
cubemastercli tpl create-from-image \
  --image "$IMAGE" --writable-layer-size 8G \
  --expose-port 8080 --expose-port 49983 \
  --probe 8080 --probe-path /v1/sandbox --cpu 4000 --memory 4096
```

记录状态为 `READY` 的 `template_id`。多节点集群可用 `cubemastercli tpl info 模板ID` 查看各节点的副本状态，至少一个可调度的 ARM64 计算节点需要具备模板所需资源并能访问其根文件系统。管理命令可以在控制节点运行，由 CubeMaster 选择计算节点。镜像更新后，用新模板创建实例。[CubeSandbox 模板说明](https://docs.cubesandbox.com/zh/guide/tutorials/template-from-image)

## 创建沙箱

下面在 CubeSandbox 控制节点从模板的根文件系统冷启动一个私有实例。需要 `cubemastercli`、`curl`、`jq` 和创建实例的权限。若平台已为你创建好实例，可直接向管理员取得 CubeProxy 地址、实例 ID、路由主机名和流量令牌，跳到下一节。

将 `CUBE_TEMPLATE_ID` 替换为本次构建的模板 ID。示例在单节点部署的控制节点执行。运维人员通过 CubeMaster 管理接口（`8089`）创建和释放实例，应用通过 CubeProxy（`8080`）访问沙箱内的 AIO。集群使用其他地址或路由域名时，替换相应变量。

```bash
CUBE_TEMPLATE_ID=your-template-id
CUBE_MASTER_URL=http://127.0.0.1:8089
CUBE_DOMAIN=cube.app
export MOSS_AIO_URL=http://127.0.0.1:8080
CUBE_STATE=$(mktemp -d)

cubemastercli tpl info "$CUBE_TEMPLATE_ID" --include-request --json |
  jq --arg id "$(cat /proc/sys/kernel/random/uuid)" '
    .create_request
    | .requestID = $id
    | .timeout = 1800
    | .exposed_ports = [8080, 49983]
    | .cube_network_config.allowPublicTraffic = false
    | .annotations |= with_entries(select(.key | startswith("cube.master.appsnapshot.") | not))
    | .containers[0].probe.timeout_ms = 120000
    | .containers[0].probe.failure_threshold = 240
  ' |
  curl -fsS -H 'Content-Type: application/json' --data-binary @- \
    "$CUBE_MASTER_URL/cube/sandbox" -o "$CUBE_STATE/instance.json"

MOSS_SANDBOX_ID=$(jq -er 'if .ret.ret_code == 200 then .sandbox_id else error(.ret.ret_msg) end' "$CUBE_STATE/instance.json")
MOSS_AIO_ROUTE_HOST="8080-${MOSS_SANDBOX_ID}.${CUBE_DOMAIN}"
MOSS_AIO_TRAFFIC_TOKEN_FILE="$CUBE_STATE/traffic.token"
jq -er '.traffic_access_token | select(length > 0)' "$CUBE_STATE/instance.json" > "$MOSS_AIO_TRAFFIC_TOKEN_FILE"
export MOSS_SANDBOX_ID MOSS_AIO_ROUTE_HOST MOSS_AIO_TRAFFIC_TOKEN_FILE
```

这次请求复用模板的镜像和资源配置，并去掉恢复内存快照的标记。CubeMaster 启动新实例后，最多等待 120 秒确认 AIO 的 `/v1/sandbox` 可用。实例连续空闲 30 分钟后到期。响应包含实例 ID 和该实例的流量令牌，请保存在仅当前用户可读的目录下。管理请求返回错误时，查看响应中的 `ret.ret_msg`。后续访问使用 `MOSS_SANDBOX_ID`（本次实例 ID），模板 ID 只在创建实例时使用。

## 执行命令

如果实例由管理员提供，在应用机器上执行以下命令，将地址、实例 ID、路由主机名和令牌文件路径换成管理员提供的值。已按上一节创建实例的用户可直接使用当前终端，跳过这个配置块。

```bash
export MOSS_AIO_URL=https://cubeproxy.example.com
export MOSS_SANDBOX_ID=your-sandbox-id
export MOSS_AIO_ROUTE_HOST="8080-${MOSS_SANDBOX_ID}.cube.app"
export MOSS_AIO_TRAFFIC_TOKEN_FILE=/path/to/traffic.token
CUBE_STATE=$(mktemp -d)
```

先用流量令牌访问 AIO 的健康接口，预期为 `healthy`。然后执行 `pwd`，响应应显示 `/home/gem` 和退出码 `0`。如果应用位于另一台机器，通过受保护的连接向它提供 CubeProxy 地址、路由和流量令牌文件，令牌路径填写应用机器上的路径。

```bash
{
  printf 'Host: %s\n' "$MOSS_AIO_ROUTE_HOST"
  printf 'e2b-traffic-access-token: %s\n' "$(cat "$MOSS_AIO_TRAFFIC_TOKEN_FILE")"
} > "$CUBE_STATE/aio.headers"

curl -fsS -H "@$CUBE_STATE/aio.headers" "$MOSS_AIO_URL/health" | jq -r .status
curl -fsS -H "@$CUBE_STATE/aio.headers" -H 'Content-Type: application/json' \
  -d '{"command":"pwd","exec_dir":"/home/gem","strict":true}' \
  "$MOSS_AIO_URL/v1/shell/exec" -o "$CUBE_STATE/result.json"
jq '.data | {output, exit_code}' "$CUBE_STATE/result.json"

AIO_SESSION_ID=$(jq -er '.data.session_id' "$CUBE_STATE/result.json")
curl -fsS -X DELETE -H "@$CUBE_STATE/aio.headers" \
  "$MOSS_AIO_URL/v1/shell/sessions/$AIO_SESSION_ID"
```

镜像内的检查覆盖 `/v1/shell/exec` 及兼容命令入口 `/v1/shell/write`。文件 API、代码执行、交互终端和 CubeSandbox SDK 的 `commands.run()` 使用各自的执行路径。对这些入口有访问限制要求时，请通过沙箱权限和网络访问策略控制。

| 响应 | 如何处理 |
| --- | --- |
| HTTP 403，`AGENTMOSS_DENIED` | 命令已被镜像内规则拒绝。查看响应的 `reason`。 |
| HTTP 403，未返回上述错误码 | 核对实例 ID、路由域名以及该实例的流量令牌。 |
| HTTP 409，`AGENTMOSS_SESSION_BUSY` | 等待该 shell 会话的前一条命令结束。 |
| HTTP 503，`AGENTMOSS_UNAVAILABLE` | 检查 AIO 服务日志和镜像内的规则加载情况。 |

如需让 Deep Agents 使用这个实例，请继续阅读[连接 Deep Agents](../deepagents-plugin/docs/cubesandbox.md)。自行创建的实例在使用完毕、数据已保存后，由有权限的运维人员在控制节点释放。此操作会结束实例内的所有会话。平台提供的实例按平台约定释放或交还管理员。

```bash
cubemastercli cubebox destroy "$MOSS_SANDBOX_ID"
```
