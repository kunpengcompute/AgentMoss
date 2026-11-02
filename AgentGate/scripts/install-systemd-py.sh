#!/bin/bash
# AgentGate Python 版本 - systemd 服务安装脚本

set -e

# 颜色定义
GREEN='\033[92m'
YELLOW='\033[93m'
BLUE='\033[94m'
RED='\033[91m'
RESET='\033[0m'

echo -e "${BLUE}======================================${RESET}"
echo -e "${BLUE}AgentGate systemd 服务安装 (Python)${RESET}"
echo -e "${BLUE}======================================${RESET}"
echo

# 检查是否为 root 或有 sudo 权限
if [ "$EUID" -ne 0 ]; then
    if ! command -v sudo &> /dev/null; then
        echo -e "${RED}错误: 需要 root 权限或 sudo${RESET}"
        exit 1
    fi
    SUDO="sudo"
else
    SUDO=""
fi

# 获取项目目录
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME=$(whoami)
GROUP_NAME=$(id -gn)

echo -e "${BLUE}项目目录: ${PROJECT_DIR}${RESET}"
echo -e "${BLUE}用户: ${USER_NAME}${RESET}"
echo -e "${BLUE}组: ${GROUP_NAME}${RESET}"
echo

# 检查 Python 版本是否安装
if [ ! -f "$PROJECT_DIR/venv/bin/agent-gate" ]; then
    echo -e "${YELLOW}警告: Python 版本未安装或未激活虚拟环境${RESET}"
    echo -e "${BLUE}正在安装...${RESET}"
    cd "$PROJECT_DIR"
    python3 -m venv venv
    source venv/bin/activate
    pip install -e ".[dev]"
    echo -e "${GREEN}✓ 安装完成${RESET}"
    echo
fi

# 创建日志目录
mkdir -p "$PROJECT_DIR/logs"

# 生成 systemd 服务文件
SERVICE_FILE="/tmp/agentgate.service"
TEMPLATE_FILE="$PROJECT_DIR/deploy/agentgate.service.template"

if [ ! -f "$TEMPLATE_FILE" ]; then
    echo -e "${RED}错误: 服务模板文件不存在: $TEMPLATE_FILE${RESET}"
    exit 1
fi

echo -e "${BLUE}生成 systemd 服务文件...${RESET}"
sed -e "s|__USER__|$USER_NAME|g" \
    -e "s|__GROUP__|$GROUP_NAME|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$TEMPLATE_FILE" > "$SERVICE_FILE"

echo -e "${GREEN}✓ 服务文件已生成: $SERVICE_FILE${RESET}"
echo

# 显示服务文件内容
echo -e "${BLUE}服务配置:${RESET}"
echo "----------------------------------------"
grep -E "^(Description|ExecStart|User|WorkingDirectory)" "$SERVICE_FILE"
echo "----------------------------------------"
echo

# 询问是否继续
read -p "$(echo -e ${YELLOW}是否安装 systemd 服务? [y/N]: ${RESET})" -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}已取消安装${RESET}"
    rm -f "$SERVICE_FILE"
    exit 0
fi

# 安装服务
echo -e "${BLUE}安装 systemd 服务...${RESET}"
$SUDO cp "$SERVICE_FILE" /etc/systemd/system/agentgate.service
$SUDO systemctl daemon-reload

echo -e "${GREEN}✓ 服务已安装${RESET}"
echo

# 询问是否立即启动
read -p "$(echo -e ${YELLOW}是否立即启动服务? [Y/n]: ${RESET})" -n 1 -r
echo
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    echo -e "${BLUE}启动服务...${RESET}"
    $SUDO systemctl start agentgate
    sleep 2
    $SUDO systemctl status agentgate --no-pager
fi

# 清理临时文件
rm -f "$SERVICE_FILE"

echo
echo -e "${GREEN}======================================${RESET}"
echo -e "${GREEN}安装完成！${RESET}"
echo -e "${GREEN}======================================${RESET}"
echo
echo -e "${BLUE}服务管理命令:${RESET}"
echo "  启动:   sudo systemctl start agentgate"
echo "  停止:   sudo systemctl stop agentgate"
echo "  重启:   sudo systemctl restart agentgate"
echo "  状态:   sudo systemctl status agentgate"
echo "  日志:   tail -f $PROJECT_DIR/logs/agent_gate.log"
echo
echo -e "${BLUE}开机自启动:${RESET}"
echo "  启用:   sudo systemctl enable agentgate"
echo "  禁用:   sudo systemctl disable agentgate"
echo
echo -e "${BLUE}Dashboard: http://127.0.0.1:9100/dashboard/${RESET}"
echo