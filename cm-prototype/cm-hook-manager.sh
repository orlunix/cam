#!/bin/bash
# cm-hook-manager.sh - CM Hook 系统
# 在特定事件触发时执行自定义脚本

CM_HOOKS_DIR="$HOME/.cm/hooks"

# 触发 hook
trigger_hook() {
    local hook_name=$1
    shift
    local args=$@
    
    hook_script="$CM_HOOKS_DIR/$hook_name.sh"
    
    if [ -x "$hook_script" ]; then
        echo "[HOOK] 执行: $hook_name"
        "$hook_script" $args
        return $?
    fi
    
    return 0
}

# 可用的 hooks
# - on_session_start <session-id> <tool> <task>
# - on_session_complete <session-id> <status>
# - on_output_line <session-id> <line>
# - on_prompt_detected <session-id> <prompt-type> <prompt-text>
# - on_code_block <session-id> <language> <code>

# 示例用法
if [ "$1" = "trigger" ]; then
    shift
    trigger_hook "$@"
    exit $?
fi

# 列出可用 hooks
if [ "$1" = "list" ]; then
    echo "已安装的 Hooks:"
    if [ -d "$CM_HOOKS_DIR" ]; then
        for hook in "$CM_HOOKS_DIR"/*.sh; do
            [ -e "$hook" ] || continue
            basename "$hook"
        done
    else
        echo "  (无)"
    fi
    exit 0
fi

# 创建示例 hooks
if [ "$1" = "init" ]; then
    mkdir -p "$CM_HOOKS_DIR"
    
    # Hook 1: 自动批准提示
    cat > "$CM_HOOKS_DIR/on_prompt_detected.sh" <<'EOF'
#!/bin/bash
# 自动批准提示 Hook

session_id=$1
prompt_type=$2
prompt_text=$3

case "$prompt_type" in
    permission)
        echo "[HOOK] 自动批准权限"
        # 这里需要找到 Claude 的进程并发送输入
        # 实际实现需要进程 ID
        ;;
    option)
        echo "[HOOK] 选择默认选项 1"
        ;;
    yes_no)
        echo "[HOOK] 回答 Yes"
        ;;
esac
EOF
    chmod +x "$CM_HOOKS_DIR/on_prompt_detected.sh"
    
    # Hook 2: 完成时自动提取代码
    cat > "$CM_HOOKS_DIR/on_session_complete.sh" <<'EOF'
#!/bin/bash
# 会话完成时自动提取代码

session_id=$1
status=$2

if [ "$status" = "completed" ]; then
    echo "[HOOK] 会话完成，自动提取代码..."
    cm extract "$session_id"
fi
EOF
    chmod +x "$CM_HOOKS_DIR/on_session_complete.sh"
    
    # Hook 3: 检测到代码块时保存
    cat > "$CM_HOOKS_DIR/on_code_block.sh" <<'EOF'
#!/bin/bash
# 检测到代码块时的处理

session_id=$1
language=$2
# code 从 stdin 读取

echo "[HOOK] 检测到 $language 代码块"
# 可以在这里做实时处理
EOF
    chmod +x "$CM_HOOKS_DIR/on_code_block.sh"
    
    echo "✓ Hook 示例已创建在 $CM_HOOKS_DIR"
    ls -l "$CM_HOOKS_DIR"
    exit 0
fi

echo "用法:"
echo "  cm-hook-manager.sh init     - 创建示例 hooks"
echo "  cm-hook-manager.sh list     - 列出 hooks"
echo "  cm-hook-manager.sh trigger <hook-name> [args]  - 触发 hook"
