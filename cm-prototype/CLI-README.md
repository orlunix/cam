# CM CLI - Command Line Interface

## 🎉 Phase 4: CLI Integration - In Progress

### 已完成

- [x] Context Manager (cm-context.py)
- [x] CLI Framework (cm-cli.py)
- [x] Context 命令 (add/list/show/test/remove)
- [x] Start 命令框架

### 待完成

- [ ] Start 命令完整实现
- [ ] Status 命令
- [ ] Logs 命令
- [ ] Kill 命令

---

## 快速开始

### 1. 添加 Context

**本地 context**:
```bash
python3 cm-cli.py ctx add local-proj /home/hren/.openclaw/workspace
```

**远程 SSH context**:
```bash
python3 cm-cli.py ctx add remote-proj /var/www/app \
  --host server.example.com \
  --user deploy \
  --port 22 \
  --key ~/.ssh/deploy_key
```

**Agent Server context**:
```bash
python3 cm-cli.py ctx add agent-proj /home/user/project \
  --agent \
  --host agent.example.com \
  --user deploy \
  --agent-port 9876 \
  --token your-secret-token
```

### 2. 列出 Contexts

```bash
python3 cm-cli.py ctx list
```

输出：
```
ID           Name                 Type       Machine                        Path                          
------------------------------------------------------------------------------------------------------
ctx-001      local-project        local      -                              /home/hren/.openclaw/workspace
ctx-002      remote-server        ssh        deploy@example.com             /var/www/app                  
ctx-003      agent-server         agent      agent.example.com              /home/user/project            
```

### 3. 查看 Context 详情

```bash
python3 cm-cli.py ctx show local-project
```

输出：
```
Context: local-project
  ID: ctx-001
  Path: /home/hren/.openclaw/workspace
  Tags: local, test
  Created: 2026-02-11T02:30:00
  Last used: never
```

### 4. 测试连接

```bash
python3 cm-cli.py ctx test remote-server
```

### 5. 启动任务

```bash
# 本地
python3 cm-cli.py start claude "Add logging to API" --ctx local-project

# 远程
python3 cm-cli.py start claude "Refactor auth" --ctx remote-server

# Agent Server
python3 cm-cli.py start codex "Security audit" --ctx agent-proj
```

---

## Context 配置格式

### 本地 Context
```json
{
  "id": "ctx-001",
  "name": "local-project",
  "path": "/home/user/project",
  "machine": "local",
  "tags": ["local", "dev"]
}
```

### SSH Context
```json
{
  "id": "ctx-002",
  "name": "remote-server",
  "path": "/var/www/app",
  "machine": {
    "type": "ssh",
    "host": "server.example.com",
    "user": "deploy",
    "port": 22,
    "keyFile": "~/.ssh/deploy_key"
  },
  "tags": ["remote", "production"]
}
```

### Agent Server Context
```json
{
  "id": "ctx-003",
  "name": "agent-server",
  "path": "/home/user/project",
  "machine": {
    "type": "agent",
    "host": "agent.example.com",
    "user": "deploy",
    "agentPort": 9876,
    "authToken": "your-secret-token"
  },
  "tags": ["remote", "agent"]
}
```

---

## 命令参考

### ctx add
添加新的工作上下文

**语法**:
```bash
cm-cli.py ctx add <name> <path> [options]
```

**选项**:
- `--host HOST` - 远程主机
- `--user USER` - SSH 用户
- `--port PORT` - SSH 端口 (默认: 22)
- `--key FILE` - SSH 密钥文件
- `--agent` - 使用 Agent Server
- `--agent-port PORT` - Agent 端口 (默认: 9876)
- `--token TOKEN` - Agent 认证 token
- `--tags TAGS` - 标签（逗号分隔）

**示例**:
```bash
# 本地
cm-cli.py ctx add myapp ~/projects/myapp

# SSH
cm-cli.py ctx add prod /var/www/app --host prod.com --user deploy

# Agent
cm-cli.py ctx add staging /app --agent --host staging.com --token xxx
```

### ctx list
列出所有 contexts

**语法**:
```bash
cm-cli.py ctx list
```

### ctx show
显示 context 详细信息

**语法**:
```bash
cm-cli.py ctx show <name|id>
```

### ctx test
测试 context 连接

**语法**:
```bash
cm-cli.py ctx test <name|id>
```

### ctx remove
删除 context

**语法**:
```bash
cm-cli.py ctx remove <name|id>
```

### start
启动编码任务

**语法**:
```bash
cm-cli.py start <tool> <task> [--ctx context]
```

**工具**:
- `claude` - Claude Code
- `codex` - Codex CLI
- `cursor` - Cursor (if available)

**示例**:
```bash
cm-cli.py start claude "Add error handling" --ctx myapp
cm-cli.py start codex "Fix security issues" --ctx prod
```

### status
显示 session 状态

**语法**:
```bash
cm-cli.py status [session-id]
```

---

## 配置文件位置

### Contexts
```
~/.cm/contexts.json
```

### Sessions
```
~/.cm/sessions/active/<session-id>.json
```

### History
```
~/.cm/history/YYYY-MM-DD.json
```

---

## 集成示例

### 完整工作流

```bash
# 1. 添加 contexts
cm-cli.py ctx add dev ~/projects/myapp
cm-cli.py ctx add prod /var/www/myapp --host prod.com --user deploy

# 2. 在开发环境测试
cm-cli.py start claude "Add feature X" --ctx dev
cm-cli.py status

# 3. 验证通过后，部署到生产
cm-cli.py start claude "Deploy feature X" --ctx prod

# 4. 查看状态
cm-cli.py status
```

### 多环境并行

```bash
# 同时在多个环境执行安全审计
for ctx in dev staging prod; do
  cm-cli.py start codex "Security audit" --ctx $ctx &
done
wait

# 查看所有结果
cm-cli.py status
```

---

## 下一步开发

### 待实现功能

1. **Start 命令完整实现**
   - 本地: 调用 cm-executor-tmux.sh
   - SSH: 通过 SSH transport 执行
   - Agent: 连接 cm-manager-client

2. **Status 命令**
   - 列出所有 active sessions
   - 显示详细状态
   - 实时更新

3. **Logs 命令**
   - 查看 session 日志
   - Follow 模式
   - 过滤和搜索

4. **Kill 命令**
   - 终止 session
   - 清理资源

5. **History 命令**
   - 查看历史记录
   - 按日期/context 过滤
   - 生成报告

---

## 贡献

欢迎贡献！请参考 CONTRIBUTING.md

---

**最后更新**: 2026-02-11 02:30 PST  
**状态**: Phase 4 In Progress (50%)
