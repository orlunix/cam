# CM Remote Support - Implementation Plan

## 状态：准备开始实现 🚀

### 已完成的准备工作

1. ✅ **设计文档** - `REMOTE-DESIGN.md`
   - 3 种方案对比
   - 架构演进路径
   - 技术栈选择

2. ✅ **Transport 层实现** - `cm-transport.py`
   - `RemoteTransport` 抽象基类
   - `SSHTransport` - SSH 连接实现
   - `NodeTransport` - OpenClaw Node 实现
   - `LocalTransport` - 本地统一接口
   - `TransportFactory` - 工厂模式创建

3. ✅ **测试脚本** - `/tmp/test-remote-tmux.sh`
   - SSH + TMUX 概念验证
   - 延迟测量
   - 自动响应测试

---

## Phase 1: SSH Remote Support (Week 1)

### Day 1-2: 核心集成

#### 任务 1.1: 更新 Context 配置格式
**文件:** `~/.cm/contexts.json`

**扩展格式:**
```json
{
  "contexts": {
    "ctx-001": {
      "id": "ctx-001",
      "name": "local-project",
      "path": "/home/user/project",
      "machine": "local"
    },
    "ctx-002": {
      "id": "ctx-002",
      "name": "remote-project",
      "path": "/var/www/app",
      "machine": {
        "type": "ssh",
        "host": "server.example.com",
        "user": "deploy",
        "port": 22,
        "keyFile": "~/.ssh/deploy_key"
      }
    }
  }
}
```

**实现文件:** `cm-context.py` (新建)
```python
class Context:
    def __init__(self, config):
        self.id = config['id']
        self.name = config['name']
        self.path = config['path']
        self.machine = config.get('machine', 'local')
        self.transport = self._create_transport()
    
    def _create_transport(self):
        from cm_transport import TransportFactory
        return TransportFactory.create_from_config(self.machine)
    
    def is_remote(self):
        return self.machine != 'local'
```

#### 任务 1.2: 修改 cm-executor-tmux.sh
**目标:** 支持 Transport 层

**修改点:**
1. 接受 `--transport` 参数
2. 所有 tmux 命令通过 transport 执行
3. 区分本地/远程路径

**示例:**
```bash
# 原来 (本地)
tmux -S "$SOCKET" new-session -d -s "$SESSION"

# 修改后 (支持远程)
if [[ "$TRANSPORT_TYPE" == "ssh" ]]; then
    ssh "$SSH_HOST" "tmux -S '$SOCKET' new-session -d -s '$SESSION'"
else
    tmux -S "$SOCKET" new-session -d -s "$SESSION"
fi
```

**更好的方式:** 使用 Python wrapper
```python
# cm-executor-wrapper.py
transport = context.transport
transport.execute(f"tmux -S {socket} new-session -d -s {session}")
```

#### 任务 1.3: 更新 cm CLI
**文件:** `cm` (bash script)

**新命令:**
```bash
# 添加远程 context
cm ctx add remote-app \
  --host server.example.com \
  --user deploy \
  --path /var/www/app \
  --key ~/.ssh/deploy_key

# 测试连接
cm ctx test remote-app

# 启动远程任务 (与本地相同)
cm start claude "Add feature X" --ctx remote-app
```

---

### Day 3-4: 测试和优化

#### 任务 2.1: 集成测试
1. 本地 → 本地 (回归测试)
2. 本地 → 远程 (新功能)
3. 网络延迟模拟

#### 任务 2.2: 错误处理
- SSH 连接失败 → 重试机制
- 网络超时 → 合理的 timeout 设置
- 远程 TMUX 不存在 → 友好提示

#### 任务 2.3: 性能优化
- SSH ControlMaster (连接复用)
- 缓存远程状态 (减少轮询)
- 压缩传输 (大输出)

---

## Phase 2: OpenClaw Node Support (Week 2)

### 任务清单

1. **Node Transport 测试**
   ```bash
   openclaw nodes invoke --node my-vps \
     --command "tmux-create-session" \
     --params '{"session": "test"}'
   ```

2. **自动检测机制**
   - 尝试 Node API
   - 失败则回退到 SSH

3. **统一状态管理**
   - Node 定期上报状态
   - 本地缓存 + 增量更新

---

## 实现优先级

### 立即开始 (今晚/明天)

1. **创建 `cm-context.py`** ⭐️⭐️⭐️
   - Context 类
   - 集成 Transport 层
   - 配置文件读写

2. **修改 `cm` CLI** ⭐️⭐️⭐️
   - 添加 `ctx add --host` 命令
   - 添加 `ctx test` 命令

3. **简单的远程执行测试** ⭐️⭐️
   - 使用现有的 Transport 层
   - 手动测试 SSH + TMUX

### 本周完成

4. **完整集成到 executor** ⭐️⭐️⭐️
   - Python wrapper for executor
   - 自动选择 transport

5. **错误处理和重试** ⭐️⭐️
6. **文档更新** ⭐️

### 下周

7. **OpenClaw Node 集成** ⭐️
8. **高级功能** (并行、负载均衡)

---

## 技术决策

### 使用 Python 还是 Bash？

**推荐：Python + Bash Hybrid**

**理由：**
- Transport 层用 Python（更好的抽象）
- CLI 入口用 Bash（简单快速）
- Executor 核心逻辑用 Python（复杂控制）

**架构：**
```
cm (bash) → cm-*.py (python) → transport → remote/local
```

### SSH 库选择

**推荐：subprocess + ssh 命令**（当前实现）

**原因：**
- 简单，利用系统 SSH 配置
- ControlMaster 自动复用连接
- 无需额外依赖

**备选：paramiko**（如果需要更多控制）
```python
import paramiko
client = paramiko.SSHClient()
client.connect(host, username=user, key_filename=key)
stdin, stdout, stderr = client.exec_command(cmd)
```

---

## 测试策略

### 本地测试
```bash
# 创建本地 "远程" context (用于测试)
cm ctx add local-as-remote \
  --host localhost \
  --user $USER \
  --path /tmp/test-project

# 确保能 SSH 到 localhost
ssh-keygen -t rsa -f ~/.ssh/id_rsa -N ""
ssh-copy-id localhost

# 运行测试
cm start claude "Create test.txt" --ctx local-as-remote
```

### 真实远程测试
```bash
# 使用 VPS 或云服务器
cm ctx add my-vps \
  --host vps.example.com \
  --user deploy \
  --path /home/deploy/projects/myapp

cm start codex "Security audit" --ctx my-vps
```

---

## 预期挑战和解决方案

### 挑战 1: SSH 密钥管理
**问题:** 用户可能有多个 SSH 密钥

**解决:**
- 支持 `--key` 参数指定密钥
- 读取 `~/.ssh/config`
- 提示用户添加密钥

### 挑战 2: 网络延迟
**问题:** 远程监控可能很慢

**解决:**
- 自适应轮询间隔（远程 5s，本地 2s）
- 本地缓存状态
- 只传输 diff（增量更新）

### 挑战 3: 远程工具版本不一致
**问题:** 远程的 Claude/Codex 版本可能不同

**解决:**
- 启动时检测版本
- 维护版本兼容性映射
- 提示用户更新

---

## 下一步行动

### 今晚可以做的（30分钟-1小时）

1. **创建 `cm-context.py`** 
   ```bash
   cd /home/hren/.openclaw/workspace/cm-prototype
   # 创建 Context 类
   # 集成 Transport
   # 测试基础功能
   ```

2. **更新 `cm` CLI 添加 `ctx` 子命令**
   ```bash
   # 添加 ctx add/list/test/remove
   ```

3. **手动测试 SSH + Transport**
   ```bash
   # 如果有远程机器，手动测试一次完整流程
   ```

### 明天

4. **完整集成测试**
5. **推送到 GitHub**
6. **更新文档**

---

## 成功指标

**Phase 1 完成的标志:**
- ✅ 可以添加远程 context
- ✅ 可以在远程启动 Claude Code
- ✅ 可以监控远程任务状态
- ✅ 自动确认在远程也能工作
- ✅ 文件在远程正确创建

**Demo 场景:**
```bash
# 1. 添加远程服务器
cm ctx add prod-server \
  --host prod.example.com \
  --user deploy \
  --path /var/www/myapp

# 2. 启动远程任务
cm start codex "Add rate limiting to API" --ctx prod-server

# 3. 监控进度 (自动)
cm status sess-xxx
# Output:
# Session: sess-xxx
# Context: prod-server (deploy@prod.example.com)
# Status: running
# State: editing (remote:/var/www/myapp/api.py)

# 4. 完成后查看
cm logs sess-xxx
ssh prod.example.com "cat /var/www/myapp/api.py"
```

---

**准备好开始实现了吗？要不要现在就创建 `cm-context.py`？** 🚀
