#!/bin/bash
# claude-interactive.sh - Claude Code 自动交互包装器
# 自动回答 Claude 的提示：权限、选项等

# 配置
AUTO_APPROVE_PERMISSIONS=true
AUTO_SELECT_DEFAULT=true
AUTO_CONTINUE=true

# 启动 Claude 并处理交互
run_claude_with_auto_response() {
    local task=$1
    
    # 使用 expect 或手动处理 I/O
    # 方案1: 使用 expect (如果安装了)
    if command -v expect >/dev/null 2>&1; then
        expect_script
    else
        # 方案2: 使用命名管道
        fifo_script "$task"
    fi
}

# 使用 expect 自动应答
expect_script() {
    expect -c "
        set timeout 300
        
        spawn claude \"$task\"
        
        # 权限提示
        expect {
            \"*permission*\" { send \"y\r\"; exp_continue }
            \"*allow*\" { send \"y\r\"; exp_continue }
            \"*approve*\" { send \"y\r\"; exp_continue }
        }
        
        # 选项提示
        expect {
            \"*(1)*\" { send \"1\r\"; exp_continue }
            \"*\\[Y/n\\]*\" { send \"y\r\"; exp_continue }
            \"*\\(y/n\\)*\" { send \"y\r\"; exp_continue }
        }
        
        # 继续提示
        expect {
            \"*continue*\" { send \"\r\"; exp_continue }
            \"*press*enter*\" { send \"\r\"; exp_continue }
        }
        
        expect eof
    "
}

# 使用命名管道方案（不需要 expect）
fifo_script() {
    local task=$1
    local fifo="/tmp/claude-fifo-$$"
    
    # 创建命名管道
    mkfifo "$fifo"
    
    # 后台监控并自动回答
    (
        while true; do
            # 检测需要输入的提示
            if grep -q "permission\|allow\|approve" "$fifo" 2>/dev/null; then
                echo "y" > "$fifo"
            elif grep -q "(1)\|(Y/n)\|(y/n)" "$fifo" 2>/dev/null; then
                echo "1" > "$fifo"  # 或 "y"
            fi
            sleep 0.1
        done
    ) &
    monitor_pid=$!
    
    # 运行 Claude，输出到管道
    claude "$task" | tee "$fifo"
    
    # 清理
    kill $monitor_pid 2>/dev/null
    rm -f "$fifo"
}

# 主函数
task="$@"
run_claude_with_auto_response "$task"
