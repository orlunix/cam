#!/bin/bash
# cm-executor-v2.sh - 支持自动交互的执行器
# 改进版：实时检测提示，自动发送响应

set -e

CM_DATA="$HOME/.cm"
CM_BIN="$(cd "$(dirname "$0")" && pwd)"

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
pid_file="$CM_DATA/sessions/active/$session_id.pid"

# 检查文件
if [ ! -f "$session_file" ] || [ ! -f "$cmd_file" ] || [ ! -f "$workdir_file" ]; then
    echo -e "${RED}错误:${NC} Session 文件不完整: $session_id"
    exit 1
fi

# 读取信息
cmd=$(cat "$cmd_file")
workdir=$(cat "$workdir_file")
tool=$(grep "^tool:" "$session_file" | cut -d' ' -f2-)

echo -e "${BLUE}=== CM Executor V2 (Auto-Interact) ===${NC}"
echo "Session: $session_id"
echo "Tool: $tool"
echo "Workdir: $workdir"
echo "Command: $cmd"
echo ""

# 更新状态函数
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

# 检测提示类型
detect_prompt() {
    local line=$1
    
    # 权限提示
    if echo "$line" | grep -qiE "permission|allow|approve|authorize|权限"; then
        echo "permission"
        return 0
    fi
    
    # 选项提示
    if echo "$line" | grep -qE "\(1\).*\(2\)|选项.*1.*2"; then
        echo "option"
        return 0
    fi
    
    # Yes/No 提示
    if echo "$line" | grep -qiE "\[?[Yy]/[Nn]\]?|\([Yy]/[Nn]\)"; then
        echo "yes_no"
        return 0
    fi
    
    # Continue 提示
    if echo "$line" | grep -qiE "continue|press.*enter|按.*回车"; then
        echo "continue"
        return 0
    fi
    
    return 1
}

# 生成自动应答
auto_respond() {
    local prompt_type=$1
    
    case "$prompt_type" in
        permission)
            echo "y"
            echo -e "${YELLOW}[AUTO]${NC} 批准权限" >&2
            ;;
        option)
            echo "1"
            echo -e "${YELLOW}[AUTO]${NC} 选择选项 1" >&2
            ;;
        yes_no)
            echo "y"
            echo -e "${YELLOW}[AUTO]${NC} 回答 Yes" >&2
            ;;
        continue)
            echo ""
            echo -e "${YELLOW}[AUTO]${NC} 回车继续" >&2
            ;;
    esac
}

# 开始执行
echo -e "${YELLOW}启动 $tool (自动交互模式)...${NC}"
update_field "status" "running"
update_field "state" "starting"

cd "$workdir"

# 创建命名管道用于输入
input_pipe="/tmp/cm-input-$session_id"
mkfifo "$input_pipe" 2>/dev/null || true

# 后台进程：监控输出并自动应答
(
    # 持续提供输入
    tail -f "$input_pipe"
) &
input_feeder_pid=$!

# 执行命令
(
    eval "$cmd" < "$input_pipe" 2>&1 | while IFS= read -r line; do
        # 保存原始输出
        echo "$line" >> "$raw_log_file"
        
        # 清理
        clean=$(echo "$line" | strip_ansi)
        timestamp=$(date "+%H:%M:%S")
        echo "[$timestamp] $clean" >> "$log_file"
        
        # 检测状态
        state=$(detect_state "$clean")
        if [ -n "$state" ]; then
            echo -e "${GREEN}状态:${NC} $state"
            update_field "state" "$state"
            
            if [ "$state" = "done" ]; then
                update_field "status" "completed"
            elif [ "$state" = "error" ]; then
                update_field "status" "failed"
            fi
        fi
        
        # 检测提示并自动应答
        prompt_type=$(detect_prompt "$clean")
        if [ $? -eq 0 ]; then
            echo -e "${BLUE}[PROMPT]${NC} 检测到: $prompt_type"
            response=$(auto_respond "$prompt_type")
            
            # 发送响应到输入管道
            echo "$response" > "$input_pipe"
            
            # 触发 hook
            if [ -x "$CM_BIN/cm-hook-manager.sh" ]; then
                "$CM_BIN/cm-hook-manager.sh" trigger on_prompt_detected "$session_id" "$prompt_type" "$clean"
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

# 清理
kill $input_feeder_pid 2>/dev/null || true
rm -f "$input_pipe"

# 触发完成 hook
if [ -x "$CM_BIN/cm-hook-manager.sh" ]; then
    status=$(grep "^status:" "$session_file" | cut -d' ' -f2-)
    "$CM_BIN/cm-hook-manager.sh" trigger on_session_complete "$session_id" "$status"
fi

echo ""
echo -e "${BLUE}日志:${NC} $log_file"
echo -e "${BLUE}原始日志:${NC} $raw_log_file"
