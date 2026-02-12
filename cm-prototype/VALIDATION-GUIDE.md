# 🧪 Code Manager - 手动验证指南

## 目标
验证两个 session：
1. **Local Session** - 本地 TMUX 执行
2. **Remote Session** - SSH 到 NVIDIA container

---

## 准备工作

### 1. 进入项目目录
```bash
cd /home/hren/.openclaw/workspace/cm-prototype
```

### 2. 检查依赖
```bash
# Python 3
python3 --version

# TMUX
tmux -V

# Websockets (可选，Agent mode 需要)
python3 -c "import websockets" 2>/dev/null && echo "✅ Installed" || echo "⚠️ Not installed"
```

---

## 测试 1: Local Session ✅

### 步骤 1: 添加 Local Context
```bash
python3 cm-cli.py ctx add test-local \
    /home/hren/.openclaw/workspace/cm-test
```

**预期输出**:
```
✅ Context added: test-local
   Type: local
   Path: /home/hren/.openclaw/workspace/cm-test
```

### 步骤 2: 查看 Contexts
```bash
python3 cm-cli.py ctx list
```

**预期输出**:
```
Contexts: 1

ID                Type        Path
---------------------------------------------------------------
test-local        local       /home/hren/.openclaw/workspace/cm-test
```

### 步骤 3: 启动 Local Session
```bash
python3 cm-cli.py start claude \
    "Create a file named test-$(date +%s).txt with content 'Local test passed'" \
    --ctx test-local
```

**预期输出**:
```
🚀 Starting claude session...
   Context: test-local
   Path: /home/hren/.openclaw/workspace/cm-test
   Task: Create a file named test-xxxxx.txt...
   
   Mode: Local TMUX
   Executor: cm-executor-tmux.sh
   
✅ Session started: sess-1234567890
   PID: 12345
   Path: /home/hren/.openclaw/workspace/cm-test
   Tool: claude

📝 Session Info:
   ID: sess-1234567890
   Mode: local
   Status: pending

💡 Check status: python3 cm-cli.py status sess-1234567890
```

### 步骤 4: 查看状态
```bash
# 所有 sessions
python3 cm-cli.py status

# 特定 session
python3 cm-cli.py status sess-1234567890
```

### 步骤 5: 查看日志
```bash
# 查看最后 50 行
python3 cm-cli.py logs sess-1234567890

# Follow 模式
python3 cm-cli.py logs sess-1234567890 -f
# (Ctrl+C 退出)
```

### 步骤 6: 验证结果
```bash
# 检查文件是否创建
ls -la /home/hren/.openclaw/workspace/cm-test/test-*.txt

# 查看内容
cat /home/hren/.openclaw/workspace/cm-test/test-*.txt
```

### 步骤 7: 清理
```bash
python3 cm-cli.py kill sess-1234567890
```

---

## 测试 2: Remote Session 🌐

### 步骤 1: 添加 Remote Context
```bash
python3 cm-cli.py ctx add test-remote /tmp \
    --host pdx-container-xterm-110.prd.it.nvidia.com \
    --port 3859 \
    --user hren
```

**预期输出**:
```
✅ Context added: test-remote
   Type: ssh
   Path: /tmp
   Host: pdx-container-xterm-110.prd.it.nvidia.com:3859
   User: hren
```

### 步骤 2: 测试连接
```bash
python3 cm-cli.py ctx test test-remote
```

**如果需要密码/密钥**:
```bash
# 手动测试 SSH 连接
ssh -p 3859 hren@pdx-container-xterm-110.prd.it.nvidia.com "echo 'SSH OK'"
```

### 步骤 3: 启动 Remote Session

**注意**: Remote session 需要 SSH 密钥或 Agent Server。

**选项 A - 使用 SSH 模式 (框架完成，需要补全)**:
```bash
python3 cm-cli.py start claude \
    "Create a file named remote-test-$(date +%s).txt" \
    --ctx test-remote
```

**选项 B - 使用 Agent 模式 (完整实现)**:

在远程机器上启动 Agent:
```bash
ssh -p 3859 hren@pdx-container-xterm-110.prd.it.nvidia.com

# 在远程机器上
python3 cm-agent-server.py --port 9876 --token YOUR_SECRET_TOKEN
```

然后更新 context 为 Agent 模式:
```bash
python3 cm-cli.py ctx add test-remote-agent /tmp \
    --agent \
    --host pdx-container-xterm-110.prd.it.nvidia.com \
    --port 3859 \
    --user hren \
    --token YOUR_SECRET_TOKEN \
    --agent-port 9876
```

启动 Agent session:
```bash
python3 cm-cli.py start claude \
    "Create a file named remote-test-$(date +%s).txt" \
    --ctx test-remote-agent
```

---

## 验证检查清单

### Local Session
- [ ] Context 创建成功
- [ ] Session 启动成功
- [ ] TMUX session 运行
- [ ] 日志可以查看
- [ ] 文件成功创建
- [ ] Session 可以终止

### Remote Session
- [ ] Context 创建成功
- [ ] SSH 连接成功
- [ ] Session 启动 (取决于实现模式)
- [ ] 远程文件创建 (如果执行成功)

---

## 调试命令

### 查看 TMUX Sessions
```bash
# 列出所有 TMUX sessions
tmux ls

# 附加到特定 session (查看实际执行)
tmux -S /tmp/cm-tmux-sockets/sess-xxxxx.sock attach -t sess-xxxxx
# (Ctrl+B, D 退出)
```

### 查看日志文件
```bash
# Session 配置
cat ~/.cm/sessions/active/sess-xxxxx.json

# Session 日志
tail -f ~/.cm/sessions/active/sess-xxxxx.log
```

### 查看 Contexts
```bash
cat ~/.cm/contexts.json | python3 -m json.tool
```

---

## 已知问题

### Local Session
- ✅ 应该完全正常工作
- 如果失败，检查 TMUX 和 executor 脚本

### Remote Session  
- ⚠️ SSH 模式框架完成，但需要完整实现
- ✅ Agent 模式完整，需要先部署 Agent Server
- 建议使用 Agent 模式进行完整测试

---

## 成功标准

### Minimal Success (最低要求)
- ✅ Local session 完整工作
- ✅ Context 管理功能正常
- ✅ CLI 命令全部可用

### Full Success (完整成功)
- ✅ Local session 完整工作
- ✅ Remote Agent session 工作
- ✅ 所有日志和监控功能正常
- ✅ 无严重 bug

---

## 下一步

根据测试结果：

### 如果 Local 测试成功
→ 继续完善 Remote 功能

### 如果发现 Bug
→ 修复并重新测试

### 如果全部成功
→ 项目完成，可以开始使用或添加高级功能！

---

**开始测试**: 复制上面的命令，逐步执行  
**报告结果**: 告诉我哪些成功，哪些失败  
**我会帮助**: 解决遇到的任何问题！

🚀 Let's validate Code Manager! 💪
