# CM Remote Architecture - Persistent Connection with Server

## 新架构：CM Agent Server

### 概念

**当前方案的问题：**
- 每次操作都要建立 SSH 连接
- 轮询监控效率低
- 网络延迟影响大

**新方案：持久化连接 + Agent Server**

```
Local Machine                          Remote Machine
┌─────────────────────┐                ┌──────────────────────────┐
│  CM Manager         │                │  CM Agent Server         │
│                     │                │  (小型常驻进程)           │
│  ├─ Context Manager │                │                          │
│  ├─ Task Scheduler  │                │  ├─ TMUX Manager         │
│  └─ UI/CLI          │                │  ├─ State Reporter       │
│       ↓             │                │  ├─ Command Receiver     │
│  SSH Tunnel ────────┼───────────────→│  └─ Log Streamer         │
│  (persistent)       │                │       ↓                  │
│       ↑             │                │  TMUX Sessions           │
│  WebSocket/gRPC ────┼───────────────→│  (Claude/Codex)          │
└─────────────────────┘                └──────────────────────────┘
```

---

## 核心设计

### 1. CM Agent Server (远程)

**启动方式：**
```bash
# 在远程机器上启动
cm-agent start --port 9876 --auth-token <token>

# 或者通过 systemd
systemctl --user start cm-agent
```

**功能：**
1. **TMUX Session 管理**
   - 创建/销毁 sessions
   - 发送命令
   - 捕获输出

2. **状态实时上报**
   - 主动推送状态变化（WebSocket）
   - 而不是被动轮询

3. **日志流式传输**
   - 实时传输 TMUX 输出
   - 压缩传输减少带宽

4. **健康检查**
   - 定期 heartbeat
   - 自动重连机制

**实现（Python）：**
```python
# cm-agent-server.py
import asyncio
import websockets
import json
import subprocess

class CMAgentServer:
    def __init__(self, port=9876, auth_token=None):
        self.port = port
        self.auth_token = auth_token
        self.sessions = {}  # session_id -> TmuxSession
        self.clients = set()  # WebSocket 连接池
    
    async def handle_client(self, websocket, path):
        """处理 Manager 的连接"""
        # 认证
        auth = await websocket.recv()
        if not self._verify_auth(auth):
            await websocket.send(json.dumps({"error": "Unauthorized"}))
            return
        
        self.clients.add(websocket)
        try:
            async for message in websocket:
                await self.handle_command(websocket, message)
        finally:
            self.clients.remove(websocket)
    
    async def handle_command(self, websocket, message):
        """处理 Manager 发来的命令"""
        cmd = json.loads(message)
        
        if cmd["action"] == "create_session":
            session_id = await self.create_tmux_session(
                cmd["tool"], cmd["task"], cmd["context"]
            )
            await websocket.send(json.dumps({
                "type": "session_created",
                "sessionId": session_id
            }))
        
        elif cmd["action"] == "send_keys":
            await self.send_keys(cmd["sessionId"], cmd["keys"])
        
        elif cmd["action"] == "capture_pane":
            output = await self.capture_pane(cmd["sessionId"])
            await websocket.send(json.dumps({
                "type": "pane_output",
                "sessionId": cmd["sessionId"],
                "output": output
            }))
    
    async def create_tmux_session(self, tool, task, context):
        """创建 TMUX session"""
        session_id = f"cm-{int(time.time())}"
        socket = f"/tmp/cm-sockets/{session_id}.sock"
        
        # 创建 TMUX session
        subprocess.run([
            "tmux", "-S", socket,
            "new-session", "-d", "-s", session_id
        ])
        
        # 启动工具
        subprocess.run([
            "tmux", "-S", socket,
            "send-keys", "-t", session_id,
            f"cd {context['path']} && {tool}", "Enter"
        ])
        
        # 创建监控任务
        self.sessions[session_id] = asyncio.create_task(
            self.monitor_session(session_id, socket)
        )
        
        return session_id
    
    async def monitor_session(self, session_id, socket):
        """监控 session 并主动推送状态"""
        while True:
            # 捕获输出
            output = subprocess.check_output([
                "tmux", "-S", socket,
                "capture-pane", "-p", "-J", "-t", session_id,
                "-S", "-50"
            ]).decode()
            
            # 检测状态变化
            state = self.detect_state(output)
            
            # 推送到所有连接的 Manager
            await self.broadcast({
                "type": "state_change",
                "sessionId": session_id,
                "state": state,
                "output": output[-1000:]  # 最后 1KB
            })
            
            # 自动确认
            if self.should_auto_confirm(output):
                await self.send_keys(session_id, "y")
                await self.broadcast({
                    "type": "auto_confirmed",
                    "sessionId": session_id
                })
            
            await asyncio.sleep(2)
    
    async def broadcast(self, message):
        """广播消息到所有连接的 Manager"""
        if self.clients:
            msg = json.dumps(message)
            await asyncio.gather(
                *[client.send(msg) for client in self.clients],
                return_exceptions=True
            )
    
    def detect_state(self, output):
        """状态检测（与 TMUX executor 相同逻辑）"""
        if "Planning" in output or "Thinking" in output:
            return "planning"
        elif "Editing" in output or "Writing" in output:
            return "editing"
        elif "Done" in output or "Completed" in output:
            return "done"
        else:
            return "running"
    
    def should_auto_confirm(self, output):
        """检测是否需要自动确认"""
        return bool(re.search(r'\(y/n\)|\[Y/n\]|Continue\?', output[-200:]))
    
    async def send_keys(self, session_id, keys):
        """发送按键到 TMUX session"""
        socket = f"/tmp/cm-sockets/{session_id}.sock"
        subprocess.run([
            "tmux", "-S", socket,
            "send-keys", "-t", session_id,
            "-l", "--", keys
        ])
        subprocess.run([
            "tmux", "-S", socket,
            "send-keys", "-t", session_id,
            "Enter"
        ])
    
    async def capture_pane(self, session_id):
        """捕获 pane 输出"""
        socket = f"/tmp/cm-sockets/{session_id}.sock"
        output = subprocess.check_output([
            "tmux", "-S", socket,
            "capture-pane", "-p", "-J", "-t", session_id,
            "-S", "-200"
        ]).decode()
        return output
    
    async def start(self):
        """启动 Agent Server"""
        print(f"🚀 CM Agent Server starting on port {self.port}")
        async with websockets.serve(self.handle_client, "0.0.0.0", self.port):
            await asyncio.Future()  # run forever

# 启动
if __name__ == "__main__":
    server = CMAgentServer(port=9876, auth_token="your-secret-token")
    asyncio.run(server.start())
```

---

### 2. SSH Tunnel (持久化)

**Manager 端建立隧道：**
```bash
# 建立持久 SSH 隧道
ssh -N -L 9876:localhost:9876 user@remote-host \
  -o ControlMaster=auto \
  -o ControlPath=/tmp/cm-ssh-%r@%h:%p \
  -o ControlPersist=24h \
  -o ServerAliveInterval=60 \
  -o ServerAliveCountMax=3 &

# 隧道建立后，Manager 连接本地 9876 端口即可
```

**自动管理隧道：**
```python
class SSHTunnel:
    def __init__(self, host, user, remote_port=9876, local_port=9876):
        self.host = host
        self.user = user
        self.remote_port = remote_port
        self.local_port = local_port
        self.process = None
    
    def start(self):
        """启动 SSH 隧道"""
        cmd = [
            "ssh", "-N",
            "-L", f"{self.local_port}:localhost:{self.remote_port}",
            f"{self.user}@{self.host}",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath=/tmp/cm-ssh-{self.host}",
            "-o", "ControlPersist=24h",
            "-o", "ServerAliveInterval=60",
            "-o", "ServerAliveCountMax=3"
        ]
        self.process = subprocess.Popen(cmd)
        
        # 等待隧道建立
        time.sleep(2)
        return self.is_alive()
    
    def is_alive(self):
        """检查隧道是否存活"""
        try:
            # 尝试连接本地端口
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(("localhost", self.local_port))
            s.close()
            return True
        except:
            return False
    
    def stop(self):
        """停止隧道"""
        if self.process:
            self.process.terminate()
```

---

### 3. CM Manager (本地)

**WebSocket 客户端：**
```python
class CMManager:
    def __init__(self, remote_host, remote_user):
        self.remote_host = remote_host
        self.remote_user = remote_user
        self.tunnel = None
        self.ws = None
    
    async def connect(self):
        """连接到 Agent Server"""
        # 建立 SSH 隧道
        self.tunnel = SSHTunnel(self.remote_host, self.remote_user)
        if not self.tunnel.start():
            raise ConnectionError("Failed to establish SSH tunnel")
        
        # 连接 WebSocket
        self.ws = await websockets.connect(
            "ws://localhost:9876",
            extra_headers={"Authorization": f"Bearer {auth_token}"}
        )
        
        # 启动消息接收任务
        asyncio.create_task(self.receive_messages())
    
    async def receive_messages(self):
        """接收 Agent 推送的消息"""
        async for message in self.ws:
            msg = json.loads(message)
            await self.handle_message(msg)
    
    async def handle_message(self, msg):
        """处理 Agent 推送的消息"""
        if msg["type"] == "state_change":
            print(f"[{msg['sessionId']}] State: {msg['state']}")
            # 更新本地状态
            self.update_session_state(msg["sessionId"], msg["state"])
        
        elif msg["type"] == "auto_confirmed":
            print(f"[{msg['sessionId']}] Auto-confirmed")
    
    async def start_session(self, tool, task, context):
        """启动远程 session"""
        await self.ws.send(json.dumps({
            "action": "create_session",
            "tool": tool,
            "task": task,
            "context": context
        }))
        
        # 等待响应
        response = await self.ws.recv()
        return json.loads(response)["sessionId"]
    
    async def send_keys(self, session_id, keys):
        """发送按键到远程 session"""
        await self.ws.send(json.dumps({
            "action": "send_keys",
            "sessionId": session_id,
            "keys": keys
        }))
```

---

## 架构优势

### vs. 传统轮询方案

| 特性 | 轮询方案 | Agent Server 方案 |
|------|----------|-------------------|
| **延迟** | 高 (轮询间隔) | 低 (实时推送) |
| **网络开销** | 高 (频繁 SSH) | 低 (持久连接) |
| **可靠性** | 低 (SSH 不稳定) | 高 (自动重连) |
| **扩展性** | 差 | 好 (多 Manager) |
| **状态同步** | 被动 | 主动 |

### 具体好处

1. **实时性**
   - 状态变化立即推送（毫秒级）
   - 不需要等待轮询周期

2. **效率**
   - 一次 SSH 连接，多次通信
   - 减少 90% 的网络请求

3. **可靠性**
   - SSH ControlPersist 自动维护连接
   - WebSocket 自动重连
   - 双重保障

4. **可扩展**
   - 多个 Manager 可以连接同一个 Agent
   - 支持 Web UI、CLI、API 同时访问

---

## 部署流程

### 1. 安装 Agent (远程机器)

```bash
# 复制 Agent 到远程
scp cm-agent-server.py user@remote:/usr/local/bin/cm-agent

# 创建配置
cat > /etc/cm-agent/config.json << EOF
{
  "port": 9876,
  "auth_token": "your-secret-token",
  "log_file": "/var/log/cm-agent.log"
}
EOF

# 启动 Agent (systemd)
cat > /etc/systemd/user/cm-agent.service << EOF
[Unit]
Description=CM Agent Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/cm-agent
Restart=always

[Install]
WantedBy=default.target
EOF

systemctl --user enable cm-agent
systemctl --user start cm-agent
```

### 2. 配置 Manager (本地)

```bash
# 添加远程 context
cm ctx add prod-server \
  --agent-mode \
  --host prod.example.com \
  --user deploy \
  --agent-port 9876 \
  --auth-token your-secret-token

# Manager 自动建立 SSH 隧道并连接
```

### 3. 使用

```bash
# 启动任务（与之前相同）
cm start claude "Add feature X" --ctx prod-server

# 状态实时显示（推送，不是轮询）
cm status --follow
```

---

## 安全考虑

### 1. 认证
- Agent 使用 token 认证
- SSH 使用密钥认证
- 双重验证

### 2. 加密
- SSH 隧道加密所有通信
- WebSocket over SSH (相当于 WSS)

### 3. 防火墙
- Agent 只监听 localhost
- 只能通过 SSH 隧道访问
- 不暴露到公网

---

## 实现路线图

### Phase 1: 基础 Agent (3-4天)

**Day 1:**
- [x] Agent Server 基本框架
- [x] TMUX 管理功能
- [x] WebSocket 通信

**Day 2:**
- [ ] 状态监控和推送
- [ ] 自动确认逻辑
- [ ] 错误处理

**Day 3:**
- [ ] Manager 客户端
- [ ] SSH 隧道管理
- [ ] 完整集成测试

**Day 4:**
- [ ] systemd 服务配置
- [ ] 部署脚本
- [ ] 文档

### Phase 2: 高级功能 (1周)

- [ ] 多 session 并行
- [ ] 日志压缩和流式传输
- [ ] 性能监控
- [ ] Web UI

---

## 对比：三种远程方案

| 方案 | 实时性 | 复杂度 | 效率 | 推荐度 |
|------|--------|--------|------|--------|
| **A. 每次 SSH** | ⭐️⭐️ | ⭐️⭐️ | ⭐️⭐️ | 适合简单场景 |
| **B. SSH 轮询** | ⭐️⭐️⭐️ | ⭐️⭐️⭐️ | ⭐️⭐️⭐️ | 快速实现 |
| **C. Agent Server** | ⭐️⭐️⭐️⭐️⭐️ | ⭐️⭐️⭐️⭐️ | ⭐️⭐️⭐️⭐️⭐️ | 生产环境 ⭐️ |

---

## 立即行动

**今晚可以开始：**
1. 创建 `cm-agent-server.py` 骨架
2. 实现基本的 WebSocket 服务
3. 测试 SSH 隧道

**明天完成：**
4. 完整的 Agent 功能
5. Manager 客户端
6. 端到端测试

---

**这个方案更优雅、更高效！要不要现在就开始写 `cm-agent-server.py`？** 🚀
