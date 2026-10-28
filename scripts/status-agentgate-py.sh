#!/bin/bash
# AgentGate Python 版本 - 服务状态检查脚本

# 颜色定义
GREEN='\033[92m'
YELLOW='\033[93m'
BLUE='\033[94m'
RED='\033[91m'
RESET='\033[0m'

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo -e "${BLUE}======================================${RESET}"
echo -e "${BLUE}AgentGate 服务状态 (Python)${RESET}"
echo -e "${BLUE}======================================${RESET}"
echo

# 检查 systemd 服务
if [ -f "/etc/systemd/system/agentgate.service" ]; then
    echo -e "${GREEN}✓ systemd 服务已安装${RESET}"
    echo

    echo -e "${BLUE}服务状态:${RESET}"
    sudo systemctl status agentgate --no-pager || true
    echo

    echo -e "${BLUE}最近日志:${RESET}"
    echo "----------------------------------------"
    tail -n 20 "$PROJECT_DIR/logs/agent_gate.log" 2>/dev/null || echo "日志文件不存在"
    echo "----------------------------------------"
else
    echo -e "${YELLOW}⚠ systemd 服务未安装${RESET}"
    echo

    # 检查是否有前台进程运行
    RUNNING_PID=$(pgrep -f "agent-gate server" || true)

    if [ -n "$RUNNING_PID" ]; then
        echo -e "${GREEN}✓ 检测到前台进程运行中 (PID: $RUNNING_PID)${RESET}"
        echo
        ps -fp "$RUNNING_PID" || true
    else
        echo -e "${YELLOW}⚠ 未检测到运行中的进程${RESET}"
        echo
        echo -e "${BLUE}启动方式:${RESET}"
        echo "  前台运行: agent-gate server --host 127.0.0.1 --port 9100"
        echo "  后台运行: nohup agent-gate server --host 127.0.0.1 --port 9100 > logs/agent_gate.log 2>&1 &"
        echo "  systemd:  bash scripts/install-systemd-py.sh"
    fi
fi

echo

# 检查端口
echo -e "${BLUE}端口状态:${RESET}"
if netstat -tlnp 2>/dev/null | grep -q ":9100 "; then
    echo -e "${GREEN}✓ 端口 9100 已监听${RESET}"
    netstat -tlnp 2>/dev/null | grep ":9100 " || ss -tlnp | grep ":9100 " || true
else
    echo -e "${YELLOW}⚠ 端口 9100 未监听${RESET}"
fi

echo

# 检查健康状态
echo -e "${BLUE}健康检查:${RESET}"
if curl -s http://127.0.0.1:9100/api/v1/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓ 服务健康${RESET}"
    curl -s http://127.0.0.1:9100/api/v1/health | jq '.' || curl -s http://127.0.0.1:9100/api/v1/health
else
    echo -e "${RED}✗ 服务不可达${RESET}"
fi

echo
echo -e "${BLUE}======================================${RESET}"
