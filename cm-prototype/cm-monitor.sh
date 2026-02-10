#!/bin/bash
# cm-monitor.sh - 监控 OpenClaw process 输出并解析状态
# 由 OpenClaw agent 在后台运行

set -e

CM_DATA="$HOME/.cm"

session_id=$1
process_id=$2

if [ -z "$session_id" ] || [ -z "$process_id" ]; then
    echo "用法: $0 <session-id> <process-id>"
    exit 1
fi

session_file="$CM_DATA/sessions/active/$session_id.md"
log_file="$CM_DATA/sessions/active/$session_id.log"
raw_log="$CM_DATA/sessions/active/$session_id.raw.log"

echo "[$(date -Iseconds)] 开始监控 $session_id (process: $process_id)" >> "$log_file"

# ANSI strip 函数
strip_ansi() {
    sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g' | sed 's/\r\n/\n/g' | sed 's/[^\n]*\r//g'
}

# 更新 session 状态
update_status() {
    local new_status=$1
    local timestamp=$(date -Iseconds)
    
    # 使用 sed 更新 YAML front matter
    sed -i "s/^status:.*/status: $new_status/" "$session_file"
    sed -i "s/^updated:.*/updated: $timestamp/" "$session_file"
}

update_state() {
    local new_state=$1
    local timestamp=$(date -Iseconds)
    
    sed -i "s/^state:.*/state: $new_state/" "$session_file"
    sed -i "s/^updated:.*/updated: $timestamp/" "$session_file"
}

add_event() {
    local event_type=$1
    local details=$2
    local timestamp=$(date +%H:%M:%S)
    
    # 在 Timeline 表格后添加行
    # 简化版本：追加到日志
    echo "[$(date -Iseconds)] EVENT: $event_type - $details" >> "$log_file"
}

# 状态检测函数
detect_state() {
    local line=$1
    
    if echo "$line" | grep -iqE "planning|thinking"; then
        echo "planning"
    elif echo "$line" | grep -iqE "editing|making changes|modifying"; then
        echo "editing"
    elif echo "$line" | grep -iqE "testing|running tests"; then
        echo "testing"
    elif echo "$line" | grep -iqE "apply.*changes|accept.*changes|continue\?"; then
        echo "waiting_confirm"
    elif echo "$line" | grep -iqE "done|completed|✓.*applied|finished"; then
        echo "done"
    elif echo "$line" | grep -iqE "error|failed|✗"; then
        echo "failed"
    else
        echo ""
    fi
}

# 提取文件名
extract_file() {
    local line=$1
    echo "$line" | grep -oP '(?<=Editing |Making changes to |Modifying )\S+' || echo ""
}

# 模拟监控（因为无法直接调用 openclaw process log）
# 实际使用时，OpenClaw agent 会用 process log --follow 工具
cat <<'EOF'
=== 监控脚本已准备好 ===

实际监控需要 OpenClaw agent 执行:

```
process log --sessionId <process-id> --follow
```

然后将输出通过管道传给这个脚本进行解析。

完整的监控命令（需要 OpenClaw agent 在后台执行）:

```bash
openclaw process log --sessionId "$process_id" --follow | \
while IFS= read -r line; do
    # 保存原始日志
    echo "$line" >> raw_log
    
    # 清理 ANSI
    clean=$(echo "$line" | strip_ansi)
    
    # 保存清理后的日志
    echo "[$(date -Iseconds)] $clean" >> log_file
    
    # 检测状态
    new_state=$(detect_state "$clean")
    if [ -n "$new_state" ]; then
        update_state "$new_state"
        add_event "state_change" "$new_state"
        
        # 如果是 waiting_confirm，自动发送 y
        if [ "$new_state" = "waiting_confirm" ]; then
            openclaw process submit --sessionId "$process_id" --data "y"
            add_event "auto_confirmed" "$clean"
        fi
        
        # 如果完成或失败，更新 status
        if [ "$new_state" = "done" ]; then
            update_status "completed"
        elif [ "$new_state" = "failed" ]; then
            update_status "failed"
        fi
    fi
    
    # 提取文件名
    file=$(extract_file "$clean")
    if [ -n "$file" ]; then
        # 更新 current_file (需要更复杂的 sed 操作)
        add_event "file_edit" "$file"
    fi
done
```

EOF

echo ""
echo "Session: $session_id"
echo "Process: $process_id"
echo "日志: $log_file"
echo ""
echo "注意: 这个脚本展示了监控逻辑，但需要 OpenClaw agent 的 process 工具才能真正工作。"
