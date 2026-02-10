# Coding Manager (CM) - Specification v1.0

## Overview

统一的编码工具管理系统，整合目录管理、工具调度、状态追踪和历史记录。

灵感来源：现有的 `dm2` 目录管理工具 + OpenClaw 的 process 管理能力

---

## Goals

1. **统一管理多种编码工具** - Codex, Claude Code, Cursor, OpenCode, Pi
2. **目录上下文管理** - 像 dm2 一样管理工作目录
3. **实时状态追踪** - 每个任务的状态、进度、输出
4. **历史记录** - 持久化所有任务的完整记录
5. **输出解析** - 自动清理 ANSI、识别状态、自动确认
6. **Markdown 文档化** - 为每个项目/目录生成 MD 文件记录

---

## Architecture

```
coding-manager/
├── cm                          # 主命令行工具
├── cm-lib.sh                   # 核心库函数
├── cm-parser.sh                # 输出解析器（ANSI strip + 状态识别）
├── cm-auto-confirm.sh          # 自动确认逻辑
├── data/
│   ├── contexts.json           # 目录上下文数据库
│   ├── sessions/               # 运行中的 session
│   │   ├── <session-id>.json   # Session 元数据
│   │   └── <session-id>.log    # 原始输出日志
│   ├── history/                # 历史记录
│   │   ├── 2026-02-10.json     # 按日期归档
│   │   └── by-project/         # 按项目组织
│   │       └── <project>.md
│   └── templates/              # MD 模板
└── docs/
    └── README.md               # 使用文档
```

---

## Core Concepts

### 1. Context (上下文)

类似 dm2 的概念，每个 context 代表一个工作目录：

```json
{
  "id": "ctx-001",
  "name": "nanobot",
  "path": "/data/tools/nanobot",
  "machine": "local",
  "tags": ["ai", "bot"],
  "created": "2026-02-09T23:00:00Z",
  "lastUsed": "2026-02-10T00:15:00Z"
}
```

**远程支持（Phase 2）：**
```json
{
  "machine": "user@server.com",
  "path": "/var/www/app"
}
```

### 2. Session (会话)

每次启动编码工具都创建一个 session：

```json
{
  "id": "sess-abc123",
  "contextId": "ctx-001",
  "tool": "codex",
  "task": "添加错误处理到 API 模块",
  "status": "running",
  "state": "editing",
  "currentFile": "src/api.js",
  "started": "2026-02-10T00:10:00Z",
  "updated": "2026-02-10T00:15:30Z",
  "processId": "openclaw-exec-xyz",
  "logPath": "data/sessions/sess-abc123.log",
  "autoConfirm": true,
  "events": [
    {"time": "00:10:05", "type": "state_change", "state": "planning"},
    {"time": "00:12:15", "type": "state_change", "state": "editing", "file": "src/api.js"},
    {"time": "00:14:30", "type": "auto_confirmed", "prompt": "Apply these changes? (y/n)"}
  ]
}
```

#### Session Status
- `starting` - 正在启动
- `running` - 运行中
- `waiting_confirm` - 等待用户确认
- `completed` - 成功完成
- `failed` - 失败
- `killed` - 手动终止

#### Session State (解析自输出)
- `planning` - 规划中
- `thinking` - 思考中
- `editing` - 编辑文件
- `testing` - 运行测试
- `committing` - 提交更改
- `waiting_confirm` - 等待确认
- `done` - 完成

### 3. History (历史记录)

每个 session 完成后归档到历史：

```json
{
  "date": "2026-02-10",
  "sessions": [
    {
      "id": "sess-abc123",
      "context": "nanobot",
      "tool": "codex",
      "task": "添加错误处理",
      "duration": "5m 30s",
      "filesChanged": ["src/api.js", "src/utils.js"],
      "result": "success",
      "summary": "成功添加了错误处理，包括 try-catch 和日志记录"
    }
  ]
}
```

**Markdown 输出：**
`data/history/by-project/nanobot.md`
```markdown
# Nanobot - Coding History

## 2026-02-10

### Session sess-abc123 (Codex) - 5m 30s
**Task:** 添加错误处理到 API 模块
**Result:** ✓ Success
**Files Changed:**
- src/api.js
- src/utils.js

**Summary:**
成功添加了错误处理，包括 try-catch 和日志记录
```

---

## Command Interface

### Context Management

```bash
# 添加新的工作目录
cm ctx add <name> <path> [--tags tag1,tag2]
cm ctx add nanobot /data/tools/nanobot --tags ai,bot

# 列出所有 context
cm ctx list
# 输出：
# ID        Name      Path                    Machine  Last Used
# ctx-001   nanobot   /data/tools/nanobot    local    5m ago
# ctx-002   oc        /data/tools/openclaw   local    1h ago

# 查看详情
cm ctx show <name|id>

# 编辑/删除
cm ctx edit <name>
cm ctx remove <name>
```

### Session Management

```bash
# 启动新任务（在当前目录）
cm start codex "添加错误处理到 API 模块"

# 在指定 context 启动
cm start codex "重构认证模块" --ctx nanobot

# 在指定路径启动（临时，不保存 context）
cm start claude "优化性能" --path ~/temp/project

# 后台模式（默认）
cm start codex "长时间任务" --ctx myapp

# 前台模式（直接显示输出，阻塞）
cm start codex "快速修复" --ctx myapp --foreground

# 指定工具选项
cm start codex "构建功能" --ctx myapp --full-auto
cm start codex "审查代码" --ctx myapp --yolo
```

### Status Monitoring

```bash
# 列出所有运行中的 session
cm status
# 输出：
# ID         Tool    Context   State      Duration  Current File
# sess-001   codex   nanobot   editing    3m 15s    src/api.js
# sess-002   claude  webapp    thinking   1m 05s    -

# 查看详细状态
cm status <session-id>
# 输出：
# Session: sess-001
# Tool: codex
# Context: nanobot (/data/tools/nanobot)
# Task: 添加错误处理到 API 模块
# Status: running
# State: editing
# Started: 2026-02-10 00:10:00
# Duration: 5m 30s
# Current File: src/api.js
# Auto-Confirm: enabled
# 
# Recent Events:
#   00:10:05  State: planning
#   00:12:15  State: editing (src/api.js)
#   00:14:30  Auto-confirmed: "Apply changes?"
#   00:15:30  State: editing (src/utils.js)

# 实时查看日志
cm logs <session-id>
cm logs <session-id> --follow  # tail -f 模式
cm logs <session-id> --raw     # 包含 ANSI 格式
```

### Interaction

```bash
# 手动确认（如果 auto-confirm 关闭）
cm confirm <session-id>

# 发送输入
cm input <session-id> "y"

# 终止 session
cm kill <session-id>

# 暂停/恢复（发送 Ctrl-Z / fg）
cm pause <session-id>
cm resume <session-id>
```

### History & Reports

```bash
# 查看今天的历史
cm history

# 查看指定日期
cm history --date 2026-02-09

# 按 context 查看
cm history --ctx nanobot

# 按工具查看
cm history --tool codex

# 生成 Markdown 报告
cm report --ctx nanobot --output ~/reports/nanobot-history.md

# 统计信息
cm stats
# 输出：
# Total sessions: 127
# Success rate: 89%
# Most used tool: codex (67 sessions)
# Most active context: nanobot (45 sessions)
```

---

## Output Parser

核心挑战：解析带 ANSI 格式的编码工具输出

### ANSI Stripping

```bash
# 正则模式匹配并移除
sed 's/\x1b\[[0-9;]*[a-zA-Z]//g'

# 或使用 Python
python3 -c "import re, sys; print(re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', sys.stdin.read()))"
```

### State Detection Patterns

基于关键词正则匹配识别状态：

```bash
# Codex patterns
"✓ Planning changes"          → state: planning
"⚡ Editing"                   → state: editing
"Running tests"               → state: testing
"❓ Apply these changes?"     → state: waiting_confirm
"✓ Changes applied"           → state: done
"✗ Error"                     → status: failed

# Claude Code patterns
"Thinking..."                 → state: thinking
"Making changes to"           → state: editing
"Done"                        → state: done

# Cursor patterns
"Generating code..."          → state: editing
"Accept changes? [Y/n]"       → state: waiting_confirm
```

### File Extraction

```bash
# 从输出中提取正在编辑的文件名
"Editing src/api.js"          → currentFile: src/api.js
"Making changes to utils.py"  → currentFile: utils.py
```

### Auto-Confirm Logic

检测到确认提示时自动回复：

```bash
# 匹配模式
"Apply these changes? (y/n)"
"Accept changes? [Y/n]"
"Continue? (yes/no)"

# 自动回复
→ 发送 "y\n" 到进程的 stdin
→ 记录 event: {type: "auto_confirmed", prompt: "..."}
```

可配置开关：
```bash
# 全局设置
cm config set auto-confirm true

# Per-session 设置
cm start codex "任务" --no-auto-confirm
```

---

## Implementation Plan

### Phase 1: Local Only (MVP)
- ✅ Context 管理（类似 dm2）
- ✅ Session 启动和追踪
- ✅ 基础状态监控（通过 OpenClaw process API）
- ✅ 简单的历史记录（JSON）
- 🚧 输出解析（ANSI strip + 状态识别）
- 🚧 自动确认

### Phase 2: Enhanced Parsing
- 实时输出流解析
- 更智能的状态识别
- 文件变更追踪
- 进度百分比估算

### Phase 3: Rich History
- Markdown 报告生成
- 按项目/日期/工具的多维查询
- Git 集成（关联 commits）
- 统计和可视化

### Phase 4: Remote Support
- SSH 隧道
- OpenClaw nodes 集成
- 跨机器的统一视图

### Phase 5: Advanced Features
- 并行任务管理（多个 session 同时运行）
- 任务队列
- 依赖管理（A 完成后启动 B）
- Web UI（实时监控面板）

---

## File Formats

### contexts.json
```json
{
  "version": 1,
  "contexts": {
    "ctx-001": {
      "id": "ctx-001",
      "name": "nanobot",
      "path": "/data/tools/nanobot",
      "machine": "local",
      "tags": ["ai", "bot"],
      "created": "2026-02-09T23:00:00Z",
      "lastUsed": "2026-02-10T00:15:00Z"
    }
  }
}
```

### sessions/<session-id>.json
```json
{
  "id": "sess-abc123",
  "contextId": "ctx-001",
  "contextName": "nanobot",
  "contextPath": "/data/tools/nanobot",
  "machine": "local",
  "tool": "codex",
  "toolOptions": ["--full-auto"],
  "task": "添加错误处理",
  "status": "running",
  "state": "editing",
  "currentFile": "src/api.js",
  "started": "2026-02-10T00:10:00Z",
  "updated": "2026-02-10T00:15:30Z",
  "processId": "openclaw-exec-xyz789",
  "logPath": "data/sessions/sess-abc123.log",
  "autoConfirm": true,
  "events": []
}
```

### history/YYYY-MM-DD.json
```json
{
  "date": "2026-02-10",
  "sessions": [
    {
      "id": "sess-abc123",
      "contextName": "nanobot",
      "tool": "codex",
      "task": "添加错误处理",
      "started": "2026-02-10T00:10:00Z",
      "completed": "2026-02-10T00:15:30Z",
      "duration": 330,
      "status": "completed",
      "filesChanged": ["src/api.js", "src/utils.js"],
      "eventsCount": 5,
      "autoConfirmsCount": 2,
      "summary": "成功添加错误处理"
    }
  ]
}
```

---

## Integration with OpenClaw

### Using exec + process APIs

```bash
# 启动 session
sessionId=$(openclaw exec --pty --background --workdir "$path" \
  "codex exec --full-auto '$task'")

# 监控输出
openclaw process log --sessionId "$sessionId" --follow | cm-parser.sh

# 发送输入（自动确认）
openclaw process submit --sessionId "$sessionId" --data "y"

# 检查状态
openclaw process poll --sessionId "$sessionId"

# 终止
openclaw process kill --sessionId "$sessionId"
```

### Parser Pipeline

```
OpenClaw process output
    ↓
ANSI strip
    ↓
State detection (regex patterns)
    ↓
Event extraction
    ↓
Update session.json
    ↓
Auto-confirm logic (if needed)
    ↓
Send input back to process
```

---

## Configuration

`~/.cm/config.json`
```json
{
  "defaultTool": "codex",
  "autoConfirm": true,
  "logRetentionDays": 30,
  "editor": "vim",
  "outputFormat": "colored",
  "tools": {
    "codex": {
      "defaultOptions": ["--full-auto"],
      "confirmPatterns": [
        "Apply these changes\\? \\(y/n\\)",
        "Continue\\? \\(yes/no\\)"
      ]
    },
    "claude": {
      "defaultOptions": [],
      "confirmPatterns": ["Accept changes\\?"]
    }
  }
}
```

---

## Example Workflow

```bash
# 1. 添加工作目录
cm ctx add myapp ~/Projects/myapp --tags web,api

# 2. 启动任务
cm start codex "添加用户认证功能" --ctx myapp
# → sess-001

# 3. 监控进度
cm status sess-001
# State: editing (src/auth.js)

# 4. 实时日志
cm logs sess-001 --follow

# 5. 完成后查看历史
cm history --ctx myapp

# 6. 生成报告
cm report --ctx myapp --output ~/myapp-dev-log.md
```

---

## Next Steps

1. **Review** - 确认 spec 符合需求
2. **Prototype** - 实现 Phase 1 MVP
3. **Test** - 在真实项目上测试
4. **Iterate** - 根据使用反馈优化

---

## Questions for Review

1. Context 管理是否足够？需要支持嵌套或分组吗？
2. Auto-confirm 的安全性 - 需要 allowlist 或 dry-run 模式吗？
3. History 的 Markdown 格式是否满足需求？
4. 是否需要支持多用户/多机器同步？
5. Web UI 的优先级如何？

---

## References

- 现有的 dm2 工具：`/home/hren/test/coderepos/dm/`
- OpenClaw coding-agent skill：`/data/home_hren/.local/lib/node_modules/openclaw/skills/coding-agent/`
- OpenClaw exec/process API 文档
