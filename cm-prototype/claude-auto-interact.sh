#!/bin/bash
# claude-auto-interact.sh - Claude 自动交互处理器
# 实时监控输出，检测提示，自动发送输入

task=$1
shift
options=$@

# 临时文件
output_log="/tmp/claude-output-$$.log"
input_fifo="/tmp/claude-input-$$"

# 清理函数
cleanup() {
    rm -f "$output_log" "$input_fifo"
}
trap cleanup EXIT

# 创建命名管道用于输入
mkfifo "$input_fifo"

# 自动应答规则
auto_respond() {
    local line=$1
    
    # 权限提示 → 自动批准
    if echo "$line" | grep -qiE "permission|allow|approve|authorize"; then
        echo "y"
        echo "[AUTO] 批准权限" >&2
        return 0
    fi
    
    # 选项提示 (1) (2) (3) → 选择 1
    if echo "$line" | grep -qE "\(1\)|\[1\]|1\."; then
        echo "1"
        echo "[AUTO] 选择选项 1" >&2
        return 0
    fi
    
    # Y/n 提示 → Y
    if echo "$line" | grep -qE "\[Y/n\]|\(Y/n\)"; then
        echo "y"
        echo "[AUTO] 回答 Yes" >&2
        return 0
    fi
    
    # y/n 提示 → y
    if echo "$line" | grep -qE "\(y/n\)|\[y/n\]"; then
        echo "y"
        echo "[AUTO] 回答 yes" >&2
        return 0
    fi
    
    # Continue / Press Enter → 回车
    if echo "$line" | grep -qiE "continue|press.*enter|按.*回车"; then
        echo ""
        echo "[AUTO] 回车继续" >&2
        return 0
    fi
    
    # 需要更多信息的提示 → 回答 "使用默认"
    if echo "$line" | grep -qiE "which|what|how|请.*选择|您.*希望"; then
        echo "使用默认选项"
        echo "[AUTO] 使用默认" >&2
        return 0
    fi
    
    return 1
}

# 后台监控器
monitor_and_respond() {
    local buffer=""
    
    while IFS= read -r line; do
        # 输出到日志
        echo "$line" | tee -a "$output_log"
        
        # 累积缓冲区（最近3行）
        buffer="$buffer\n$line"
        buffer=$(echo "$buffer" | tail -3)
        
        # 检查是否需要响应
        response=$(auto_respond "$line")
        if [ $? -eq 0 ]; then
            # 发送响应
            echo "$response" > "$input_fifo"
        fi
    done
}

echo "[CM] 启动 Claude 自动交互模式"
echo "[CM] 任务: $task"
echo ""

# 启动 Claude，输入来自管道，输出到监控器
(
    # 持续从管道读取并发送到 Claude 的 stdin
    cat "$input_fifo"
) | claude --permission-mode bypassPermissions "$task" 2>&1 | monitor_and_respond

echo ""
echo "[CM] Claude 执行完成"
