#!/bin/bash
# cm-start-integrated.sh - 使用 OpenClaw exec 启动编码工具
# 这个脚本由 OpenClaw agent 调用，使用 exec 工具

set -e

CM_DATA="$HOME/.cm"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [ $# -lt 1 ]; then
    echo "用法: $0 <session-id>"
    echo "从 ~/.cm/sessions/active/<session-id>.cmd 和 .workdir 读取任务"
    exit 1
fi

session_id=$1
session_file="$CM_DATA/sessions/active/$session_id.md"
cmd_file="$CM_DATA/sessions/active/$session_id.cmd"
workdir_file="$CM_DATA/sessions/active/$session_id.workdir"

# 检查文件
if [ ! -f "$session_file" ] || [ ! -f "$cmd_file" ] || [ ! -f "$workdir_file" ]; then
    echo -e "${RED}错误:${NC} Session 文件不完整: $session_id"
    exit 1
fi

# 读取命令和工作目录
cmd=$(cat "$cmd_file")
workdir=$(cat "$workdir_file")

echo -e "${YELLOW}=== CM 启动编码任务 ===${NC}"
echo "Session: $session_id"
echo "命令: $cmd"
echo "工作目录: $workdir"
echo ""

# 输出 OpenClaw exec 命令（供 agent 执行）
cat <<EOF
请执行以下 OpenClaw 工具调用:

\`\`\`
exec(
    pty: true,
    background: true,
    workdir: "$workdir",
    command: "$cmd"
)
\`\`\`

执行后会返回 process sessionId，然后需要:

1. 更新 session MD 文件的 process_id 字段
2. 启动监控进程: cm-monitor.sh <session-id> <process-id>
EOF

echo ""
echo -e "${GREEN}提示:${NC} 这是一个半自动化流程"
echo "需要 OpenClaw agent 使用 exec 工具来实际执行命令"
