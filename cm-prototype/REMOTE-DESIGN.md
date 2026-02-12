# Code Manager - Remote Support Design

## 当前架构（Local Only）

```
User → CM CLI → TMUX Session → Coding Tool (Claude/Codex)
                    ↓
              Local Filesystem
```

**限制：**
- 只能管理本地机器的任务
- 无法跨机器协作
- 无法利用远程计算资源

---

## Remote 支持方案

### 方案 A：SSH + Remote TMUX ⭐️ 推荐

**架构：**
```
Local Machine                   Remote Machine
┌─────────────────┐            ┌──────────────────────┐
│  CM CLI         │            │                      │
│    ↓            │            │                      │
│  SSH Tunnel ────┼───────────→│  Remote TMUX Session │
│    ↓            │            │       ↓              │
│  Monitor        │←───────────│  Coding Tool         │
│  (capture-pane) │            │       ↓              │
└─────────────────┘            │  Remote Filesystem   │
                               └──────────────────────┘
```

**关键点：**
1. **SSH 执行远程命令**
   ```bash
   ssh user@remote "tmux -S $SOCKET new-session ..."
   ```

2. **远程 TMUX 控制**
   ```bash
   ssh user@remote "tmux -S $SOCKET send-keys ..."
   ssh user@remote "tmux -S $SOCKET capture-pane ..."
   ```

3. **本地监控远程 session**
   - CM Monitor 通过 SSH 定期 capture-pane
   - 状态检测逻辑不变
   - Auto-confirm 通过 SSH send-keys

**优势：**
- ✅ 简单直接，基于成熟的 SSH
- ✅ 安全（SSH 加密）
- ✅ 复用现有 TMUX 架构
- ✅ 最小修改量

**挑战：**
- SSH 密钥管理
- 网络延迟
- SSH 连接稳定性

---

### 方案 B：OpenClaw Nodes Integration

**架构：**
```
Local Machine                   Remote Node (OpenClaw)
┌─────────────────┐            ┌──────────────────────┐
│  CM CLI         │            │  OpenClaw Gateway    │
│    ↓            │            │       ↓              │
│  Gateway API ───┼───────────→│  Node Handler        │
│    ↓            │            │       ↓              │
│  Monitor        │←───────────│  TMUX Session        │
│  (pull state)   │            │       ↓              │
└─────────────────┘            │  Coding Tool         │
                               └──────────────────────┘
```

**关键点：**
1. **使用 OpenClaw 的 nodes 功能**
   ```bash
   openclaw nodes invoke --node remote-node \
     --command "tmux-session-create" \
     --params "{\"tool\": \"claude\", \"task\": \"...\"}"
   ```

2. **统一的 API**
   - 本地和远程使用相同接口
   - OpenClaw 处理路由和认证

3. **状态同步**
   - Node 定期上报 session 状态
   - 本地 CM 拉取状态更新

**优势：**
- ✅ 统一管理（本地+远程）
- ✅ 内置认证和加密
- ✅ 跨平台（OpenClaw nodes 支持多种设备）
- ✅ 更好的状态管理

**挑战：**
- 需要在远程机器安装 OpenClaw
- 更复杂的设置
- 依赖 OpenClaw nodes 功能

---

### 方案 C：Hybrid (SSH + OpenClaw) ⭐️⭐️ 最佳

**架构：**
```
Local Machine                   Remote Machine
┌─────────────────┐            ┌──────────────────────┐
│  CM CLI         │            │  OpenClaw Node       │
│    ↓            │            │  (optional)          │
│  Transport ─────┼───────────→│       OR             │
│  Layer          │            │  Direct SSH          │
│  ↓              │            │       ↓              │
│  • SSH          │            │  Remote TMUX         │
│  • Node API     │            │  Session Manager     │
│  • Auto-detect  │            │       ↓              │
│    ↓            │            │  Coding Tool         │
│  Monitor        │←───────────│                      │
└─────────────────┘            └──────────────────────┘
```

**关键设计：**
1. **抽象的 Transport 层**
   ```python
   class RemoteTransport(ABC):
       def execute(self, command: str) -> str: pass
       def send_keys(self, session: str, keys: str): pass
       def capture_pane(self, session: str) -> str: pass
   
   class SSHTransport(RemoteTransport):
       # SSH implementation
   
   class NodeTransport(RemoteTransport):
       # OpenClaw node implementation
   ```

2. **自动检测**
   - 检测远程是否有 OpenClaw → 使用 Node API
   - 否则 → 使用 SSH

3. **统一接口**
   - CM CLI 不关心传输方式
   - Context 配置指定连接方式

**优势：**
- ✅ 灵活：支持多种连接方式
- ✅ 渐进式：可以先 SSH，后升级 Node
- ✅ 向后兼容：本地 session 不受影响

---

## 设计细节

### 1. Context 配置扩展

**当前（Local）：**
```json
{
  "id": "ctx-001",
  "name": "myproject",
  "path": "/path/to/project",
  "machine": "local"
}
```

**扩展（Remote）：**
```json
{
  "id": "ctx-002",
  "name": "remote-project",
  "path": "/home/user/project",
  "machine": {
    "type": "ssh",
    "host": "server.example.com",
    "user": "deploy",
    "port": 22,
    "keyFile": "~/.ssh/id_rsa"
  }
}
```

或者使用 OpenClaw Node：
```json
{
  "id": "ctx-003",
  "name": "node-project",
  "path": "/home/user/project",
  "machine": {
    "type": "openclaw-node",
    "nodeId": "my-vps",
    "gatewayUrl": "https://my-gateway.com",
    "token": "..."
  }
}
```

---

### 2. Executor 适配

**接口抽象：**
```python
class RemoteExecutor:
    def __init__(self, transport: RemoteTransport):
        self.transport = transport
    
    def create_session(self, config):
        cmd = f"tmux -S {socket} new-session ..."
        self.transport.execute(cmd)
    
    def send_task(self, session_id, task):
        cmd = f"tmux -S {socket} send-keys ..."
        self.transport.execute(cmd)
    
    def capture_output(self, session_id):
        cmd = f"tmux -S {socket} capture-pane ..."
        return self.transport.execute(cmd)
```

**本地和远程统一：**
```bash
# Local
cm start claude "task" --ctx local-project

# Remote (SSH)
cm start claude "task" --ctx remote-project

# Remote (Node)
cm start claude "task" --ctx node-project
```

---

### 3. Monitor 适配

**轮询逻辑：**
```python
def monitor_loop(session_id, transport):
    while True:
        # 通过 transport 获取输出
        output = transport.capture_pane(session_id)
        
        # 状态检测（本地逻辑，不变）
        state = detect_state(output)
        
        # Auto-confirm（通过 transport）
        if should_auto_confirm(output):
            transport.send_keys(session_id, "y\n")
        
        # 网络延迟补偿
        if transport.is_remote():
            sleep(5)  # 远程稍长间隔
        else:
            sleep(2)  # 本地短间隔
```

---

### 4. SSH 连接管理

**连接池：**
```python
class SSHConnectionPool:
    def __init__(self):
        self.connections = {}
    
    def get_connection(self, host, user):
        key = f"{user}@{host}"
        if key not in self.connections:
            self.connections[key] = paramiko.SSHClient()
            # 配置和连接
        return self.connections[key]
    
    def execute(self, host, user, command):
        conn = self.get_connection(host, user)
        stdin, stdout, stderr = conn.exec_command(command)
        return stdout.read().decode()
```

**保持连接：**
- 使用 SSH ControlMaster（复用连接）
- 定期发送 keepalive
- 自动重连机制

---

### 5. 安全考虑

**SSH 密钥管理：**
```bash
# CM 配置文件
~/.cm/ssh-keys/
  ├── server1.key
  ├── server2.key
  └── config.json
```

**权限验证：**
- 远程机器需要相同的 allowlist 配置
- 每个 machine 独立的 exec approvals
- 审计日志（本地 + 远程）

**敏感数据：**
- SSH 密钥加密存储
- Token 使用 keyring
- 日志脱敏

---

## 实现路线图

### Phase 1: SSH 基础支持 (1-2周)

**Week 1:**
- [ ] Context 配置扩展（支持 SSH）
- [ ] SSH Transport 实现
- [ ] 基础远程 TMUX 控制

**Week 2:**
- [ ] Remote Monitor 实现
- [ ] 连接池和重连机制
- [ ] 基础测试

### Phase 2: 完善和优化 (1周)

- [ ] 错误处理和恢复
- [ ] 网络延迟优化
- [ ] SSH 密钥管理 UI
- [ ] 完整测试覆盖

### Phase 3: OpenClaw Node 集成 (1-2周)

- [ ] Node Transport 实现
- [ ] 自动检测逻辑
- [ ] 统一状态管理
- [ ] 跨机器任务调度

### Phase 4: 高级功能 (后续)

- [ ] 多机器并行任务
- [ ] 负载均衡
- [ ] 故障转移
- [ ] Web UI 远程管理

---

## CLI 命令设计

### Context 管理

```bash
# 添加远程 context (SSH)
cm ctx add remote-app \
  --host server.example.com \
  --user deploy \
  --path /var/www/app \
  --key ~/.ssh/deploy.key

# 添加 Node context
cm ctx add node-app \
  --node my-node \
  --path /home/user/app

# 测试连接
cm ctx test remote-app
# Output:
# ✅ SSH connection: OK
# ✅ Remote TMUX: OK
# ✅ Remote path: /var/www/app (exists)
# ✅ Tools available: claude, codex

# 列出所有 context
cm ctx list
# ID         Name         Machine              Status
# ctx-001    local-proj   local                active
# ctx-002    remote-app   deploy@server.com    online
# ctx-003    node-app     node:my-node         online
```

### 任务执行

```bash
# 在远程执行（与本地相同）
cm start claude "Add logging" --ctx remote-app

# 查看状态（自动显示机器信息）
cm status sess-001
# Session: sess-001
# Context: remote-app (deploy@server.example.com)
# Tool: claude
# Status: running
# State: editing (remote:src/api.js)
# Network latency: 45ms

# 实时查看（通过 SSH）
cm logs sess-001 --follow
```

### 多机器管理

```bash
# 同时在多个机器运行
cm batch start \
  --ctx local-app,remote-app,node-app \
  --tool codex \
  --task "Run security audit"

# 查看所有机器的状态
cm status --all
# Machine              Active  Completed  Failed
# local                2       15         1
# deploy@server.com    1       8          0
# node:my-node         3       22         2
```

---

## 技术栈建议

### Python 实现（推荐）

**优势：**
- 更好的抽象和类型系统
- 丰富的 SSH 库（paramiko, fabric）
- 更容易集成 OpenClaw SDK

**库选择：**
```python
import paramiko          # SSH 连接
from fabric import Connection  # 高级 SSH 操作
import asyncio           # 异步监控多个 session
```

### Bash + Python Hybrid

**架构：**
- Bash: CLI 入口和简单操作
- Python: 复杂逻辑（SSH, 状态管理）

```bash
# cm (bash)
#!/bin/bash
case "$1" in
  start)
    python3 ~/.cm/cm-start.py "$@"
    ;;
  status)
    python3 ~/.cm/cm-status.py "$@"
    ;;
esac
```

---

## 测试策略

### 单元测试
```python
def test_ssh_transport():
    transport = SSHTransport("testhost", "user")
    result = transport.execute("echo test")
    assert result == "test\n"

def test_remote_tmux_session():
    executor = RemoteExecutor(transport)
    session = executor.create_session(config)
    assert session.id is not None
```

### 集成测试
```bash
# 需要真实远程机器或 Docker 容器
docker run -d --name cm-remote-test openssh-server
cm ctx add test-remote --host localhost --port 2222 ...
cm start claude "test task" --ctx test-remote
```

### 端到端测试
```bash
# 完整流程
./test-e2e.sh
# 1. 创建远程 context
# 2. 启动任务
# 3. 监控完成
# 4. 验证结果文件
# 5. 清理
```

---

## 风险和挑战

### 技术风险
1. **SSH 稳定性** - 长时间连接可能断开
   - 解决：重连机制 + ControlMaster
   
2. **网络延迟** - 影响监控实时性
   - 解决：调整轮询间隔 + 本地缓存状态
   
3. **远程工具版本** - Claude/Codex 版本不一致
   - 解决：版本检测 + 适配层

### 安全风险
1. **SSH 密钥泄露**
   - 解决：加密存储 + 权限控制
   
2. **远程命令注入**
   - 解决：参数验证 + 转义

3. **日志敏感信息**
   - 解决：脱敏 + 访问控制

---

## 推荐方案总结

**短期（现在开始）：方案 A - SSH**
- 简单快速
- 复用现有架构
- 满足基本远程需求

**中期（2-3周后）：方案 C - Hybrid**
- 保留 SSH 支持
- 添加 Node 集成
- 提供更多灵活性

**长期（3个月+）：完整的分布式系统**
- 多机器协调
- 负载均衡
- 高可用

---

## 立即行动建议

1. **今晚/明天：设计验证**
   - 写一个简单的 SSH TMUX 概念验证
   - 测试网络延迟影响

2. **本周：基础实现**
   - Context 配置扩展
   - SSH Transport 类
   - 远程 Monitor 适配

3. **下周：完整测试**
   - 在真实远程机器测试
   - 性能和稳定性验证

---

**要不要现在就写一个 SSH TMUX 的概念验证？** 🚀
