#!/bin/bash
# cm-executor.sh - 执行编码工具并监控输出
# 支持 Claude Code, Codex, Cursor

set -e

CM_DATA="$HOME/.cm"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

if [ $# -lt 1 ]; then
    echo "用法: $0 <session-id>"
    exit 1
fi

session_id=$1
session_file="$CM_DATA/sessions/active/$session_id.md"
cmd_file="$CM_DATA/sessions/active/$session_id.cmd"
workdir_file="$CM_DATA/sessions/active/$session_id.workdir"
log_file="$CM_DATA/sessions/active/$session_id.log"
raw_log_file="$CM_DATA/sessions/active/$session_id.raw.log"

# 检查文件
if [ ! -f "$session_file" ] || [ ! -f "$cmd_file" ] || [ ! -f "$workdir_file" ]; then
    echo -e "${RED}错误:${NC} Session 文件不完整: $session_id"
    exit 1
fi

# 读取信息
cmd=$(cat "$cmd_file")
workdir=$(cat "$workdir_file")
tool=$(awk '/^---$/,/^---$/ {if ($1 == "tool:") {$1=""; print; exit}}' "$session_file" | xargs)

echo -e "${BLUE}=== CM Executor ===${NC}"
echo "Session: $session_id"
echo "Tool: $tool"
echo "Workdir: $workdir"
echo "Command: $cmd"
echo ""

# 更新状态为 running
update_field() {
    local field=$1
    local value=$2
    local timestamp=$(date -Iseconds)
    
    sed -i "s|^${field}:.*|${field}: ${value}|" "$session_file"
    sed -i "s|^updated:.*|updated: ${timestamp}|" "$session_file"
}

# Strip ANSI
strip_ansi() {
    sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g' | sed 's/\r\n/\n/g' | sed 's/[^\n]*\r//g'
}

# 状态检测
detect_state() {
    local line=$1
    
    if echo "$line" | grep -qiE "planning|thinking|analyzing"; then
        echo "planning"
    elif echo "$line" | grep -qiE "editing|modifying|writing|creating"; then
        echo "editing"
    elif echo "$line" | grep -qiE "testing|running.*test"; then
        echo "testing"
    elif echo "$line" | grep -qiE "请告诉我|告诉我您的|您希望|please.*tell|please.*approve|需要您的确认"; then
        echo "waiting_input"
    elif echo "$line" | grep -qiE "done|completed|finished|success"; then
        echo "done"
    elif echo "$line" | grep -qiE "error|failed|exception"; then
        echo "error"
    fi
}

# 开始执行
echo -e "${YELLOW}启动 $tool...${NC}"
update_field "status" "running"
update_field "state" "starting"

cd "$workdir"

# 执行命令并捕获输出
(
    eval "$cmd" 2>&1 | while IFS= read -r line; do
        # 保存原始输出
        echo "$line" >> "$raw_log_file"
        
        # 清理并保存
        clean=$(echo "$line" | strip_ansi)
        timestamp=$(date "+%H:%M:%S")
        echo "[$timestamp] $clean" >> "$log_file"
        
        # 检测状态
        state=$(detect_state "$clean")
        if [ -n "$state" ]; then
            echo -e "${GREEN}状态:${NC} $state"
            update_field "state" "$state"
            
            # 如果完成或失败，更新 status
            if [ "$state" = "done" ]; then
                update_field "status" "completed"
            elif [ "$state" = "error" ]; then
                update_field "status" "failed"
            fi
        fi
        
        # 实时输出
        echo "$clean"
    done
    
    exit_code=$?
    if [ $exit_code -eq 0 ]; then
        update_field "status" "completed"
        update_field "state" "done"
        echo -e "${GREEN}✓ 完成${NC}"
    else
        update_field "status" "failed"
        update_field "state" "error"
        echo -e "${RED}✗ 失败 (exit code: $exit_code)${NC}"
    fi
) || {
    update_field "status" "failed"
    update_field "state" "error"
    echo -e "${RED}✗ 执行失败${NC}"
}

echo ""
echo -e "${BLUE}日志:${NC} $log_file"
echo -e "${BLUE}原始日志:${NC} $raw_log_file"
