#!/bin/bash
# claude-auto.sh - Claude Code 包装器，自动处理权限
# 用法: claude-auto.sh "task description"

# 设置环境变量（如果 Claude 支持）
export CLAUDE_AUTO_APPROVE=1
export CLAUDE_SKIP_PERMISSIONS=1

# 使用最宽松的权限模式（不用 exec，让输出可以被捕获）
claude --permission-mode bypassPermissions --dangerously-skip-permissions "$@"
