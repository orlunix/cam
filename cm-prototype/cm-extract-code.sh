#!/bin/bash
# cm-extract-code.sh - 从 Claude 输出中提取代码并创建文件
# 用法: cm-extract-code.sh <session-id>

session_id=$1
log_file="$HOME/.cm/sessions/active/$session_id.log"

if [ ! -f "$log_file" ]; then
    echo "错误: 日志文件不存在"
    exit 1
fi

# 查找代码块
in_code=false
current_file=""
current_lang=""
code_content=""

while IFS= read -r line; do
    # 移除时间戳
    line=$(echo "$line" | sed 's/^\[[0-9:]*\] //')
    
    # 检测代码块开始
    if echo "$line" | grep -q '^```'; then
        if [ "$in_code" = false ]; then
            # 开始代码块
            in_code=true
            current_lang=$(echo "$line" | sed 's/^```//')
            code_content=""
        else
            # 结束代码块，保存文件
            in_code=false
            
            # 尝试从之前的行找文件名
            if [ -n "$current_file" ]; then
                echo "创建文件: $current_file"
                echo "$code_content" > "$current_file"
                current_file=""
            else
                echo "代码块 ($current_lang):"
                echo "$code_content"
                echo ""
            fi
            code_content=""
        fi
    elif [ "$in_code" = true ]; then
        # 累积代码内容
        code_content+="$line"$'\n'
    else
        # 检测文件名提示
        if echo "$line" | grep -qE "创建.*文件|文件.*:|`.*\..*`"; then
            # 提取文件名
            fname=$(echo "$line" | grep -oP '`\K[^`]+(?=`)' | grep '\.' | head -1)
            if [ -n "$fname" ]; then
                current_file="$fname"
            fi
        fi
    fi
done < "$log_file"

echo "代码提取完成"
