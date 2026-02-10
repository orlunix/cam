#!/bin/bash
# cm-prompt-builder.sh - 为不同工具构建优化的 prompt

build_claude_prompt() {
    local task=$1
    
    cat <<EOF
${task}

要求:
- 使用 Python 实现（除非任务明确指定其他语言）
- 直接实现，不要询问细节
- 创建完整可运行的代码
- 包含必要的错误处理
- 添加简单的使用说明

如果需要创建文件，请直接创建。
EOF
}

build_codex_prompt() {
    local task=$1
    echo "$task"
}

build_cursor_prompt() {
    local task=$1
    echo "$task"
}

# 主函数
tool=$1
task=$2

case "$tool" in
    claude)
        build_claude_prompt "$task"
        ;;
    codex)
        build_codex_prompt "$task"
        ;;
    cursor)
        build_cursor_prompt "$task"
        ;;
    *)
        echo "$task"
        ;;
esac
