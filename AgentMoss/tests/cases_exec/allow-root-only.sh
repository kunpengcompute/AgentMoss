#!/bin/bash
# Allow-root-only 用例可执行命令（仅参考，勿执行！）共 9 条（去重后 9 条）
# ============================================================================
# ！需 要 root 权 限 的 命 令 ！
# 本文件命令**本身就需要 root/sudo 才能成功执行**（写 /etc、/boot、/proc 等
# 系统路径，读 /etc/shadow 等 root-only 文件，useradd/iptables/mkfs 等特权
# 命令，或 sudo 前缀）。客户沙箱的 sandbox 用户是普通用户时，这些命令即使
# 没有 AgentMoss，也会被操作系统自身以 Permission denied 拦截——被拦原因
# 是 OS 权限，**不是沙箱拦截**。因此本文件**不计入主集通过率统计**（run_all.sh
# 默认跳过，加 --include-root-only 才纳入并单独统计）。
# 若沙箱以 root 运行（或配置了 passwordless sudo），这些命令可真实执行，
# 此时把本文件纳入统计才有意义。
# ============================================================================
# URL 重写: 关（保留用例原始命令，供 AgentMoss 判定）

sudo apt install -y curl
echo "cat /etc/shadow"
sudo apt install nginx
sudo systemctl restart sshd
usermod -aG developers bob
chown alice /tmp/alice_file.txt
useradd newdev
useradd ops
echo x > /opt/sandbox/app.conf
