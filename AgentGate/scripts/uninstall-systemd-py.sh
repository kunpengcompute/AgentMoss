#!/bin/bash
# AgentGate Python 版本 - systemd 服务卸载脚本

set -e

# 颜色定义
GREEN='\033[92m'
YELLOW='\033[93m'
BLUE='\033[94m'
RED='\033[91m'
RESET='\033[0m'

echo -e "${BLUE}======================================${RESET}"
echo -e "${BLUE}AgentGate systemd 服务卸载 (Python)${RESET}"
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

# 检查服务是否安装
if [ ! -f "/etc/systemd/system/agentgate.service" ]; then
    echo -e "${YELLOW}警告: systemd 服务未安装${RESET}"
    exit 0
fi

# 显示服务状态
echo -e "${BLUE}当前服务状态:${RESET}"
$SUDO systemctl status agentgate --no-pager || true
echo

# 询问是否继续
read -p "$(echo -e ${YELLOW}是否卸载 systemd 服务? [y/N]: ${RESET})" -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}已取消卸载${RESET}"
    exit 0
fi

# 停止服务（如果运行）
if $SUDO systemctl is-active --quiet agentgate; then
    echo -e "${BLUE}停止服务...${RESET}"
    $SUDO systemctl stop agentgate
fi

# 禁用开机自启动（如果启用）
if $SUDO systemctl is-enabled --quiet agentgate; then
    echo -e "${BLUE}禁用开机自启动...${RESET}"
    $SUDO systemctl disable agentgate
fi

# 删除服务文件
echo -e "${BLUE}删除服务文件...${RESET}"
$SUDO rm -f /etc/systemd/system/agentgate.service

# 重载 systemd
$SUDO systemctl daemon-reload

echo -e "${GREEN}✓ 服务已卸载${RESET}"
echo

echo -e "${GREEN}======================================${RESET}"
echo -e "${GREEN}卸载完成！${RESET}"
echo -e "${GREEN}======================================${RESET}"
echo
echo -e "${BLUE}注意:${RESET}"
echo "  - 日志文件仍保留在 logs/ 目录"
echo "  - 数据库文件仍保留在 ~/.local/share/agent_gate/"
echo "  - 如需清理，请手动删除"
echo