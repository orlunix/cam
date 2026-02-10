# Coding Manager - 快速演示

刚才我创建了 **Coding Manager (cm)** 的第一个原型！

## 已实现的功能

### ✅ Context 管理
```bash
cm ctx add <name> <path> [--tags]   # 添加工作目录
cm ctx list                          # 列出所有
cm ctx show <name>                   # 查看详情
```

### ✅ Session 创建
```bash
cm start <tool> "<task>" --ctx <name> [--full-auto|--yolo]
```

### ✅ 数据结构
- Markdown + YAML front matter 存储
- Context 文件: `~/.cm/contexts/<name>.md`
- Session 文件: `~/.cm/sessions/active/<session-id>.md`

## 演示

```bash
# 1. 初始化
$ cm init
✓ 初始化完成: /home/hren/.cm

# 2. 添加工作目录
$ cm ctx add workspace ~/.openclaw/workspace --tags test
✓ Context 已添加: workspace → /home/hren/.openclaw/workspace

# 3. 列出 contexts
$ cm ctx list
Contexts:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  workspace        /home/hren/.openclaw/workspace
                  Tags: test

# 4. 创建任务
$ cm start codex "创建API错误处理脚本" --ctx workspace --full-auto
启动 codex 任务:
  Context: workspace (/home/hren/.openclaw/workspace)
  Task: 创建API错误处理脚本
  Options:  --full-auto
  Session: sess-1770728525

✓ Session 已创建: sess-1770728525
```

## 已创建的文件

### Context 文件示例
`~/.cm/contexts/workspace.md`:
```markdown
---
name: workspace
path: /home/hren/.openclaw/workspace
machine: local
created: 2026-02-10T04:55:25-08:00
lastUsed: 2026-02-10T04:55:25-08:00
tags: test
---

# Context: workspace

**Path:** `/home/hren/.openclaw/workspace`  
**Machine:** local  
**Created:** 2026-02-10T04:55:25-08:00  
**Tags:** test

## Statistics
- Total Sessions: 0
- Success Rate: N/A

## Recent Sessions
_None yet_
```

### Session 文件示例
`~/.cm/sessions/active/sess-1770728525.md`:
```markdown
---
id: sess-1770728525
context: workspace
context_path: /home/hren/.openclaw/workspace
tool: codex
status: starting
state: initializing
created: 2026-02-10T04:55:45-08:00
process_id: 
auto_confirm: true
---

# Session sess-1770728525

## Task
创建API错误处理脚本

## Status
🟡 **Starting**

## Timeline
| Time     | Event          | Details                    |
|----------|----------------|----------------------------|
| 04:55:45 | started        | Session created            |
```

## 下一步

### Phase 1.5 - 集成 OpenClaw exec ⚡
需要你（OpenClaw agent）来完成实际执行：

```bash
# CM 生成任务后，OpenClaw agent 读取并执行
session_id="sess-1770728525"
cmd=$(cat ~/.cm/sessions/active/$session_id.cmd)
workdir=$(cat ~/.cm/sessions/active/$session_id.workdir)

# 使用 exec 工具启动
exec pty:true background:true workdir:"$workdir" command:"$cmd"
# → 返回 process_id

# 更新 session 文件
yq -i ".process_id = \"$process_id\"" ~/.cm/sessions/active/$session_id.md

# 启动监控
exec pty:true background:true command:"cm-monitor-session $session_id"
```

### Phase 2 - 输出解析和状态追踪
- 实时解析 `process log` 输出
- ANSI strip
- 状态识别（planning/editing/done）
- 自动确认

### Phase 3 - 完整的工作流
- 监控守护进程
- 历史归档
- Markdown 报告生成

## 文件位置

所有代码在: `/home/hren/.openclaw/workspace/cm-prototype/`
- `cm` - 主脚本（可执行）
- 数据目录: `~/.cm/`

## 总结

✅ 核心数据结构 - Markdown + YAML  
✅ Context 管理 - 完成  
✅ Session 创建 - 完成  
🚧 OpenClaw 集成 - 需要 agent 支持  
🚧 输出监控 - 待实现  
🚧 状态解析 - 待实现  

这是一个可工作的框架！下一步是让它真正运行 Codex/Claude 并监控输出。
