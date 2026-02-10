#!/bin/bash
# cm-parser.sh - 解析编码工具输出，提取状态信息
# 从 stdin 读取，输出结构化信息

# Strip ANSI
strip_ansi() {
    sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g' | sed 's/\r\n/\n/g' | sed 's/[^\n]*\r//g'
}

# 检测状态
detect_state() {
    local line=$1
    
    # Claude Code 模式
    if echo "$line" | grep -qiE "thinking|analyzing"; then
        echo "planning"
    elif echo "$line" | grep -qiE "editing|modifying|writing|creating file"; then
        echo "editing"
    elif echo "$line" | grep -qiE "done|completed|finished"; then
        echo "done"
    
    # Codex 模式
    elif echo "$line" | grep -qiE "planning changes|✓ planning"; then
        echo "planning"
    elif echo "$line" | grep -qiE "⚡ editing|making changes"; then
        echo "editing"
    elif echo "$line" | grep -qiE "apply.*changes|accept.*changes"; then
        echo "waiting_confirm"
    elif echo "$line" | grep -qiE "✓ changes applied"; then
        echo "done"
    
    # 通用错误
    elif echo "$line" | grep -qiE "error|failed|exception|✗"; then
        echo "error"
    fi
}

# 提取文件名
extract_file() {
    local line=$1
    # 尝试多种模式
    echo "$line" | grep -oP '(?<=Editing |Making changes to |Creating |Writing |Modifying )\S+' || \
    echo "$line" | grep -oP '(?<=file )\S+' || \
    echo ""
}

# 主循环
while IFS= read -r line; do
    # 清理
    clean=$(echo "$line" | strip_ansi)
    
    # 检测状态
    state=$(detect_state "$clean")
    if [ -n "$state" ]; then
        echo "STATE:$state"
    fi
    
    # 提取文件
    file=$(extract_file "$clean")
    if [ -n "$file" ]; then
        echo "FILE:$file"
    fi
    
    # 输出清理后的行
    echo "LINE:$clean"
done
