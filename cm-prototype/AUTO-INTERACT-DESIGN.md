# CM 自动交互系统 - 完整设计

## 🎯 目标
自动处理 Claude/Codex 的交互提示：权限、选项、Yes/No 等，无需人工干预。

## 🏗️ 架构

```
Claude/Codex 运行
    ↓ 输出
实时监控器
    ↓ 检测提示
提示分类器
    ↓
自动应答生成器
    ↓ 输入
Claude/Codex 继续
```

## 📦 组件

### 1. cm-executor-v2.sh - 增强执行器
**功能:**
- 实时监控输出
- 检测提示类型
- 自动发送响应
- 触发 Hooks

**提示检测规则:**
```bash
permission → 检测 "permission|allow|approve" → 回答 "y"
option     → 检测 "(1) (2)" → 回答 "1"  
yes_no     → 检测 "[Y/n]|(y/n)" → 回答 "y"
continue   → 检测 "continue|press enter" → 回答 ""
```

### 2. cm-hook-manager.sh - Hook 系统
**可用 Hooks:**
- `on_session_start` - 会话开始
- `on_session_complete` - 会话完成（自动提取代码）
- `on_output_line` - 每行输出
- `on_prompt_detected` - 检测到提示
- `on_code_block` - 检测到代码块

**安装 Hooks:**
```bash
cm-hook-manager.sh init  # 创建示例 hooks
cm-hook-manager.sh list  # 列出已安装
```

### 3. claude-auto-interact.sh - 备用方案
如果 executor-v2 不work，可以用这个独立的交互处理器。

## 🚀 使用方式

### 方法 1: 使用 V2 Executor (推荐)
```bash
# 1. 创建 session
cm start claude "创建一个 web 服务器" --ctx workspace

# 2. 用 V2 执行器（自动处理交互）
/path/to/cm-executor-v2.sh sess-XXX
```

### 方法 2: 集成到 cm 命令
修改 `cm exec` 使用 v2 executor：
```bash
# 在 cm 脚本中
cm_exec() {
    session_id=$1
    "$CM_BIN/cm-executor-v2.sh" "$session_id"
}
```

### 方法 3: 启用 Hooks
```bash
# 初始化 hooks
cm-hook-manager.sh init

# 编辑 ~/.cm/hooks/on_session_complete.sh
# 添加自定义逻辑

# 执行时自动触发
cm exec sess-XXX  # 完成后自动调用 hook
```

## 📋 交互流程示例

### 场景：Claude 请求权限

```
[Claude 输出]
看起来需要文件写入权限。是否允许？(y/n)

[Executor V2 检测]
→ 匹配模式: "\(y/n\)"
→ 分类: yes_no

[自动应答]
→ 生成: "y"
→ 发送到 stdin

[Claude 继续]
已批准，创建文件 server.py...
```

## 🔧 实现细节

### 命名管道方案
```bash
# 创建输入管道
mkfifo /tmp/cm-input-$session_id

# 后台喂入器
tail -f $input_pipe &

# Claude 从管道读取输入
claude "task" < $input_pipe

# 检测到提示时写入管道
echo "y" > $input_pipe
```

### 实时检测
```bash
while IFS= read -r line; do
    # 清理 ANSI
    clean=$(strip_ansi "$line")
    
    # 检测提示
    if detect_prompt "$clean"; then
        # 自动应答
        response=$(auto_respond "$prompt_type")
        echo "$response" > $input_pipe
    fi
    
    # 记录日志
    echo "$clean" >> log
done
```

## ⚙️ 配置选项

可以在 session 或全局配置中设置：

```yaml
# ~/.cm/config.json
{
  "auto_interact": {
    "enabled": true,
    "rules": {
      "permission": "approve",      # approve / deny / ask
      "option": "first",            # first / last / ask
      "yes_no": "yes",              # yes / no / ask
      "continue": "auto"            # auto / manual
    }
  }
}
```

## 🎨 Hook 示例

### on_session_complete.sh
```bash
#!/bin/bash
session_id=$1
status=$2

if [ "$status" = "completed" ]; then
    # 自动提取代码
    cm extract "$session_id"
    
    # 发送通知
    echo "Session $session_id 完成" | \
        cm notify --channel discord
fi
```

### on_prompt_detected.sh
```bash
#!/bin/bash
session_id=$1
prompt_type=$2
prompt_text=$3

# 记录所有提示
echo "[$session_id] $prompt_type: $prompt_text" >> \
    ~/.cm/prompts.log

# 自定义应答逻辑
case "$prompt_type" in
    permission)
        # 特殊处理：检查文件路径
        if echo "$prompt_text" | grep -q "/etc/"; then
            echo "拒绝 /etc/ 权限" >&2
            exit 1  # 阻止自动批准
        fi
        ;;
esac
```

## 🚧 限制和注意事项

### 当前限制
1. **TTY 检测** - Claude 可能检测到非 TTY 并改变行为
2. **异步问题** - 提示和检测可能有时间差
3. **复杂提示** - 多选题、自由输入难以自动化

### 解决方案
1. 使用 `script` 命令模拟 TTY
2. 添加延迟和缓冲
3. 对复杂提示使用默认值或跳过

## 📊 测试计划

### Test 1: 权限自动批准
```bash
cm start claude "创建文件 test.txt" --ctx workspace
cm-executor-v2.sh sess-XXX
# 期望: 自动批准，文件被创建
```

### Test 2: 选项自动选择
```bash
cm start claude "创建 API，选择 Python/Node" --ctx webapp
cm-executor-v2.sh sess-XXX
# 期望: 自动选择第一个选项（Python）
```

### Test 3: Hook 触发
```bash
cm-hook-manager.sh init
cm exec sess-XXX
# 期望: 完成后自动提取代码
```

## 🎯 下一步

1. **集成到 cm** - 让 `cm exec` 默认使用 v2
2. **测试真实场景** - 运行多个任务验证
3. **优化提示检测** - 添加更多模式
4. **配置系统** - 让用户自定义规则
5. **文档完善** - 用户手册和示例

## 总结

通过**实时监控 + 模式匹配 + 自动应答 + Hook 系统**，实现了 Claude 的自动交互管理，无需手动干预！
