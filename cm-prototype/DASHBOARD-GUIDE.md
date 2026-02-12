# 📊 Real-time Dashboard 功能说明

**创建时间**: 2026-02-11 19:53 PST  
**状态**: ✅ 已实现  
**Discord Message ID**: `1471353230092013692`

---

## 🎯 功能说明

### 什么是 Real-time Dashboard？

一个**可编辑的 Discord 消息**，显示 Code Manager 的实时状态：
- 📋 所有活跃的 sessions
- 🌐 SSH 连接状态
- 📍 已配置的 contexts
- 🕐 最后更新时间

### 核心优势

✅ **一次创建，持续使用**
- Pin 住这条消息
- 随时滚动查看
- 无需重复询问

✅ **按需刷新**
- 说 "refresh dashboard"
- 我会更新同一条消息
- 不产生新消息

✅ **轻量级**
- 不需要额外服务
- 不需要 webhook
- 纯文本展示

---

## 📱 使用方式

### 1. 查看当前状态

**方式 A**: 滚动到之前的 dashboard 消息
- 查看最后一次的状态快照
- 适合快速浏览

**方式 B**: 要求刷新
```
你: "refresh dashboard"
我: [更新消息内容]
```

### 2. Pin 消息（推荐）

在 Discord 中：
1. 找到 dashboard 消息（ID: `1471353230092013692`）
2. 右键 → Pin Message
3. 以后通过右上角 📌 图标快速访问

### 3. 定期刷新

```
你: "每小时给我更新一次dashboard"
我: [设置定时刷新]
```

---

## 🎨 Dashboard 内容

### 当前显示的信息

```
📊 Code Manager - Real-time Dashboard

🕐 Last Updated: 2026-02-11 19:53 PST

---

## 📋 Active Sessions (9 total)

✅ Running (5):
• sess-1770859314 - SSH - claude
• sess-1770859089 - SSH - claude  
• sess-1770859305 - SSH - claude
...

⏳ Pending (4):
• sess-1770859076 - SSH
...

---

## 🌐 SSH Connections

ControlMaster processes: 0 (expired)

---

## 📍 Contexts (6)

• test-local → Local
• test-remote → SSH
...

---

💡 Quick Commands:
cm-cli.py status
cm-cli.py logs <id>
cm-cli.py kill <id>
```

---

## 🔧 技术实现

### 方式 1: 手动刷新（当前）

```
1. 你说 "refresh dashboard"
2. 我执行 cm-cli.py status
3. 我用 message.edit 更新消息
4. 同一条消息，内容更新 ✅
```

### 方式 2: 自动脚本（可选）

```bash
# 本地运行 watch 模式
cd /home/hren/.openclaw/workspace/cm-prototype
python3 cm-dashboard.py --watch --interval 300

# 每 5 分钟打印一次状态
```

### 方式 3: Cron 定时（未来）

```bash
# 每小时自动更新 Discord 消息
cron:
  schedule: "0 * * * *"  # 每小时
  action: refresh-dashboard
  message-id: 1471353230092013692
```

---

## 📊 与传统方式对比

### 传统方式
```
你: "显示 sessions"
我: [创建新消息]

你: "再显示一次"
我: [又创建新消息]

你: "状态怎么样"
我: [继续创建新消息]

结果: 20 条消息，滚动查找困难
```

### Dashboard 方式
```
初次: 创建 dashboard 消息 → Pin 住

以后:
你: "refresh"
我: [更新同一条消息]

你: 滚动到 pinned messages
    查看最新状态

结果: 1 条消息，始终保持最新
```

---

## 💡 使用场景

### 场景 1: 长期监控

```
1. 启动多个 remote sessions
2. Pin dashboard 消息
3. 每 30 分钟刷新一次
4. 随时查看进度
```

### 场景 2: 调试问题

```
1. Sessions 出现问题
2. 打开 dashboard
3. 快速定位 pending/failed sessions
4. 执行 logs/kill 命令
```

### 场景 3: 项目切换

```
1. 工作在多个项目
2. Dashboard 显示所有 contexts
3. 快速查看哪些正在运行
4. 决定启动新 session 或复用
```

---

## 🔄 刷新命令

### 基本刷新
```
"refresh dashboard"
"update dashboard"
"刷新仪表板"
```

### 带选项
```
"refresh dashboard with full details"
"只刷新 sessions"
"显示最近 1 小时的状态"
```

---

## ⚙️ 高级功能（可选实现）

### 1. 自动刷新
```python
# 使用 OpenClaw cron
cron.add(
    schedule="*/30 * * * *",  # 每 30 分钟
    action="refresh-dashboard",
    message_id="1471353230092013692"
)
```

### 2. 交互式按钮
```
[🔄 Refresh] [📋 View Logs] [🗑️ Clean Up]
```
- Discord button components
- 点击直接执行操作

### 3. 智能刷新
```python
# 只在状态变化时刷新
if sessions_changed or contexts_added:
    auto_refresh_dashboard()
```

### 4. 多仪表板
```
Dashboard 1: Sessions (频繁刷新)
Dashboard 2: Contexts (偶尔刷新)
Dashboard 3: Performance Stats (每日刷新)
```

---

## 📝 实现文件

### 核心脚本
- **cm-dashboard.py** (6KB)
  - 收集 sessions/contexts/ssh 状态
  - 格式化 dashboard 内容
  - 支持 watch 模式

### 使用方法

```bash
# 1. 打印当前状态
python3 cm-dashboard.py

# 2. Watch 模式（本地）
python3 cm-dashboard.py --watch --interval 60

# 3. 通过 OpenClaw 刷新（推荐）
你: "refresh dashboard"
```

---

## 🎯 最佳实践

### 推荐工作流

1. **初始化**
   ```
   创建 dashboard → Pin 住
   ```

2. **日常使用**
   ```
   早上: 刷新 dashboard，查看状态
   启动任务: 刷新确认
   下班前: 最后刷新，检查进度
   ```

3. **问题排查**
   ```
   Dashboard 显示 pending → 查看 logs
   Dashboard 显示 failed → 执行 kill
   ```

---

## 🔍 技术细节

### Message Edit API

```python
message.edit(
    channel="discord",
    messageId="1471353230092013692",
    message=new_dashboard_content
)
```

### 数据收集

```python
# 1. 获取 sessions
subprocess.run(['python3', 'cm-cli.py', 'status'])

# 2. 获取 contexts
subprocess.run(['python3', 'cm-cli.py', 'ctx', 'list'])

# 3. 获取 SSH masters
subprocess.run(['ps', 'aux', '|', 'grep', 'ControlMaster'])

# 4. 格式化并更新
```

---

## 📊 状态示例

### 健康状态
```
✅ Running: 8
⏳ Pending: 0
🌐 SSH Masters: 2
📍 Contexts: 6

→ 一切正常
```

### 需要注意
```
✅ Running: 3
⏳ Pending: 5  ← 注意！多个 pending
🌐 SSH Masters: 0  ← SSH 连接断开
📍 Contexts: 6

→ 需要检查 pending sessions
```

### 空闲状态
```
✅ Running: 0
⏳ Pending: 0
🌐 SSH Masters: 0
📍 Contexts: 6

→ 无活跃任务
```

---

## 🎊 总结

### 已实现 ✅
- 创建 dashboard 消息
- 手动刷新功能
- 完整状态显示
- 格式化脚本

### 使用方式
1. **Pin 住 dashboard 消息**
2. **随时查看状态**
3. **说 "refresh" 更新**
4. **无需重复询问**

### 核心优势
- ✅ 轻量级（纯文本）
- ✅ 持久化（同一条消息）
- ✅ 按需刷新（不主动打扰）
- ✅ 快速访问（Pin 功能）

---

**Dashboard Message ID**: `1471353230092013692`

**刷新命令**: "refresh dashboard" 或 "update dashboard"

**推荐**: Pin 这条消息到频道顶部，随时查看！📌

---

**文档时间**: 2026-02-11 19:55 PST  
**实现状态**: ✅ 完全可用
