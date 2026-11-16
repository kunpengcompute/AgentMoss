#!/bin/bash
# AgentGate V1 快速启动与健康检查

set -e

echo "======================================"
echo "AgentGate V1 快速启动"
echo "======================================"
echo

GREEN='\033[92m'
YELLOW='\033[93m'
BLUE='\033[94m'
RED='\033[91m'
RESET='\033[0m'

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
PORT="${AGENT_GATE_PORT:-9100}"
BASE_URL="http://127.0.0.1:${PORT}"

mkdir -p "$PROJECT_DIR/logs"

echo -e "${BLUE}检查 AgentGate V1 服务...${RESET}"
if curl -s "${BASE_URL}/api/v1/health" > /dev/null 2>&1; then
    echo -e "${GREEN}✓ AgentGate V1 已运行${RESET}"
else
    echo -e "${YELLOW}⚠ AgentGate V1 未运行，正在启动...${RESET}"

    if [ -x "$VENV_DIR/bin/agent-gate" ]; then
        GATE_BIN="$VENV_DIR/bin/agent-gate"
    else
        GATE_BIN="agent-gate"
    fi

    cd "$PROJECT_DIR"
    nohup "$GATE_BIN" server --host 127.0.0.1 --port "$PORT" \
        > "$PROJECT_DIR/logs/agent_gate.log" 2>&1 &

    echo -e "${BLUE}等待服务启动...${RESET}"
    sleep 3

    if curl -s "${BASE_URL}/api/v1/health" > /dev/null 2>&1; then
        echo -e "${GREEN}✓ AgentGate V1 启动成功${RESET}"
    else
        echo -e "${RED}✗ AgentGate V1 启动失败，请查看 $PROJECT_DIR/logs/agent_gate.log${RESET}"
        exit 1
    fi
fi

echo
echo "======================================"
echo -e "${GREEN}服务信息${RESET}"
echo "======================================"
echo "健康检查: curl ${BASE_URL}/api/v1/health"
echo "分析接口: POST ${BASE_URL}/api/v1/analyze"
echo "看板:     ${BASE_URL}/dashboard/"
echo "日志:     tail -f $PROJECT_DIR/logs/agent_gate.log"
echo
echo "AgentMoss 地址通过 MOSS_URL / AGENT_MOSS_URL 配置（默认 http://127.0.0.1:9090）。"
