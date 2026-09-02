# CAM Desktop 开发说明文档（交接版）

> 写于 2026-08-18，用途：新线程 / 新开发者上手 CAM Desktop 开发的全量交接。
> 当日状态：分支 `camui-desktop-v2`，desktop 版本 `0.2.29`（`apps/cam-desktop/package.json`）。
> 本文整理自当日的代码、文档与根 `AGENTS.md` 规则；如与代码冲突，**以代码为准**。

---

## 0. TL;DR

- CAM Desktop 是一个 Electron 应用，用来管理跑在**远程 SSH 节点**上的 AI coding
  agents（Claude Code / Codex / Cursor / 任意 CLI）。**agent 永远不在 Desktop 本机运行**；
  Desktop 是控制面（hub）之上的瘦客户端。
- 本机（Thinkpad，Windows + WSL）拥有 desktop 范围：`apps/cam-desktop/` 和 `web/`。
  **camc 归另一个 agent**——不要动 `src/camc`、`src/camc_pkg/`、`dist/camc`、`build_camc.py`。
- 渲染层**没有构建步骤**：`web/desktop.html` + vanilla JS ES modules，靠手工维护的
  `?v=` 查询串做缓存戳。改了一个文件就要同步 bump 所有引用处的戳。
- **Extension-first 规则**（2026-08-11 起）：新功能优先做成扩展（iframe view +
  `main.py` 远程工具 + 每扩展属性），或作为同名用户包 shadow 内置工具；
  只有 bridge 新能力 / 新 native 页面 / registry·hub·transport 修复才打新 MSI。
- 发布走 GitHub Actions：推 `github` remote + 强推 moving tag `cam-desktop-v<版本>`
  → 出 MSI（Windows）和 DMG（macOS）→ 传到 GitHub Release。
- 测试是无框架的 grep-assertion 风格；发布门槛见 §7。

---

## 1. 产品定位与总体架构

```text
Desktop UI  ──HTTP/WS──▶  CAM Hub/API  ──poll/route──▶  Remote Controller/Node  ──spawns──▶  tmux + agent runtime
```

- **CAM 是 hub（控制面）**：聚合 node/agent/context 三张表，把命令路由到正确的
  controller。
- **coding agent 跑在 controller/node 上**：节点上有 Python、tmux、`cam`/`camc` 和
  各 agent CLI。节点以 CAM context 的形式被表示。
- **Desktop 是瘦 UI**：渲染 hub 的表，通过 `CamApi` 发用户动作；终端 attach 走
  Electron 主进程的 ssh2 PTY。
- **安装后的 app 不得要求用户有 WSL / 本地 Python / host shell / 本地 `cam`**。
  用户不应需要打开终端。

两种连接模式（Settings 里只有这两个 tab）：

- **Direct（默认）**：app 自己拉起一个嵌入式 Node hub（跑在 Electron 主进程里，
  见 §3.2），绑定 loopback，token 每次启动随机生成。冷启动无 profile 时自动
  start 并落在 Agents 页（DIRECT-013）；`contexts:[]` 是合法运行态。
- **Relay**：连一个已有 hub，经过 relay 端点。两道 token 边界：client→relay 用
  用户输入的 relay token；relay→源 hub 由 relay 注入源 hub 的 CAM API token，
  客户端无需知道。Relay source 用 `camui start --profile … --relay-url …
  --relay-token …` 起（见 `docs/desktop/README.md` 的 Quick Start）。
- **Local tab 已退役**（2026-07-17）：本地会话不支持，hub 一律回
  `local_unsupported` + "run an SSH server on it and add it as an SSH node"。
  LOC-010..024 与 TERM-010..017 仅为需求 ID 稳定保留，**不得再对其派活**。

左侧导航的工作区 mode（workspace mode，与连接模式正交）：**Agents**（默认）、
**Start**、**Settings**、**Nodes**、**Extensions**，以及隐藏的 Skills / Bots /
Todos（见 §4.3 的 `HIDDEN_MODES`）。

---

## 2. 仓库布局与责任边界

### 2.1 相关目录

| 路径 | 作用 |
|---|---|
| `apps/cam-desktop/` | Electron 壳：`electron/` 主进程、`cli/` 调试 CLI、`test/` 测试、`build/` 安装包资源、`package.json`（版本 + electron-builder 配置） |
| `web/` | 三套前端 + 共享层：`desktop.html`+`js/desktop/`（本 app 渲染层）、`mobile.html`+`js/mobile/`（手机 V2）、`index.html`+`js/views/`（V1 PWA，**冻结**）、`js/api.js`/`js/state.js`/`js/shared/`（共享）、`vendor/`（xterm/marked/dompurify UMD）、`css/` |
| `extensions/` | 扩展系统：`SPEC.md`/`GUIDE.md`/`README.md`、`host/`（hub 侧运行时）、`packages/`（内置扩展）、`examples/`（可安装 demo）、`vendor/cam-assist/`（内置 assistant 的 pi agent 源码与 dist） |
| `docs/desktop/` | 需求注册表与 desktop 文档（§10 文档地图） |
| `dist/camc`、`dist/skillm` | 打进 MSI 的远程二进制（extraResources）。**git-tracked，别当构建产物"清理"** |
| `src/camc` | `dist/camc` 的同步副本（同属 camc 所有权的例外：只同步、不修改逻辑） |
| `scripts/release.sh` | **camc 的发布脚本，不是 desktop 的**（§6.5） |
| `relay/relay.py` | Relay 服务器（stdlib-only Python） |

### 2.2 所有权（根 `AGENTS.md`，2026-07-23 用户指令）

- **Kimi（本机）只拥有 desktop**：`apps/cam-desktop/` 和 `web/`。理由：这台机器有
  desktop 的构建/测试/发布环境（electron-builder、MSI/DMG、Release 流程）。
- **camc 归另一个 agent**：不要在本机编辑 `src/camc`、`src/camc_pkg/`、`dist/camc`、
  `build_camc.py`。需要 camc 改动时路由给 owning agent（本地已知 sibling：
  `cam-dev`，agent id `f1a1a661`，路径 `/home/hren/.openclaw/workspace/cam`；
  委派前用 `camc list` / `camc status f1a1a661` / `camc capture f1a1a661 --lines N`
  确认可用；用 `camc msg send f1a1a661 -t "..."` 派活，写清范围、预期文件、测试和
  可解析的完成格式）。
- 例外：用户**明确要求**超出 desktop 范围的改动时，只改那一处。
- 只有用户明确要求 commit / 明确批准时才 commit；**不得到明确授权不 push**。

### 2.3 web/ 内部边界（`web/AGENTS.md`）

- mobile 代码**不得** import `web/js/desktop/*`；desktop 渲染层对 mobile 只读参考。
- 共享文件 `js/api.js` / `js/state.js` / `js/shared/*` 改动要同时考虑 mobile。
- V1 PWA（`index.html` 树）冻结；它的 `?v=` 必须在 `index.html`、`js/app.js`、
  `sw.js` 三处同步。

---

## 3. Electron 主进程（`apps/cam-desktop/electron/`）

七个文件，全部 CommonJS 手写，**无构建步骤，就地编辑**。调用链：
渲染层（`web/js/…`）→ `CamBridge`（preload）→ `ipcMain.handle`（main）→
embedded-hub / ssh-transport / assistant-host。**秘密永不进渲染层**——渲染层只见
session id、元数据和字节流。

### 2.4 同名不同物（命名辨析，极易混）

- **`cam`**（`src/cam/`）— Python 服务端/CLI，hub 的老实现；desktop 不依赖它。
- **`camc`**（`src/camc_pkg/` → `dist/camc`）— 跑在远程节点上的 stdlib-only
  agent 管理二进制；hub 经 SSH 调它。归另一个 agent 所有。
- **`camui` / `camui-cli`**（`apps/cam-desktop/cli/camui-cli.cjs`）— desktop 的
  调试 CLI：`require` 同一份 `embedded-hub.cjs`/`credential-store.cjs`/
  `ssh-transport.cjs`，在**终端里自己起一个 in-process hub**（镜像 Electron 的
  userData 路径，所以看到的是同一套 contexts/agents 数据；见 camui-cli.cjs:56、
  :189）。
- **assistant 的 `cam` 工具**（`extensions/vendor/cam-assist/entry.js:127`）—
  LLM 工具，纯 `fetch` 客户端，调**已经在跑的** hub 的 `/api/*`。
  与 camui-cli **无代码关系**（不 spawn 它、不 import 它），只是说的是同一套
  hub HTTP 协议：camui-cli 是"自己当 hub"，cam 工具是"当 hub 的客户端"。
- **能力面对比（2026-08-18 核实）**：两者的上限都 = embedded hub 的 `/api/*`
  路由全集——cam 工具天生 generic（`{method,path,body}`），camui-cli 也有
  generic 透传 `camui api METHOD /api/path [--data JSON]`（外加
  status/start/logs/node list|add|sync/agent list 便捷命令）。但**都不等于
  "所有 cam 操作"**，三条共同天花板：
  1. hub 本身只是 CAM API 的严格子集（无 `/api/ws` 事件流，渲染层靠 5 s 轮询）；
  2. **app 层设置够不着**——Direct/Relay profile、外观等存在渲染层
     localStorage，不在 hub；hub 侧 `/api/system/config` 只读（GET）；可写的
     "设置"是 agent 属性（`PATCH /api/agents/:id`）和扩展配置
     （`PUT /api/extensions/<name>/config`）；
  3. **交互式 PTY 终端够不着**——Terminal 模式走 `CamBridge.term` → 主进程
     ssh2 `_termPool` → `camc attach`，不经 hub HTTP；两者对 agent 的"交互"都是
     异步的：`POST /api/agents/:id/input|key|upload` + 轮询 output。
     收发消息/发键/截屏的具体端点（embedded-hub.cjs:4731-4820）：
     `POST /input {text, send_enter?}`（默认补回车）、`POST /key {key}`、
     `POST /upload {filename, data(base64)}`、`GET /output?lines=N&format=plain|ansi&hash=...`
     （hash 相同返回 `{unchanged:true}`）、`GET /fulloutput`（全 scrollback）。
     前提：agent 在跑（否则 `agent_not_running`）、其 context SSH 可达且凭据可用。
  各自额外限制：cam 工具有 30 s 超时 + 12 KB 响应截断；camui-cli 无
  safeStorage（`--remember` 拒绝，密码 auth 只能 one-shot 每次 prompt）、无
  daemon 模式（每次调用短命 hub；`camui start` 前台常驻）。

### 3.1 `main.cjs`（1371 行）— 入口

- 单实例锁；创建 1280×800 `BrowserWindow`：`contextIsolation: true,
  nodeIntegration: false, sandbox: true`；`resolveWebRoot()`（main.cjs:66）以
  **`file://`** 加载 `web/desktop.html`（打包后 `process.resourcesPath/web`，开发期
  repo 的 `web/`）。阻止离开该 file URL 的导航；http(s) 链接走外部浏览器；最小
  右键复制粘贴菜单。
- **Direct Hub IPC**（`local:check/start/stop/restart/logs/getProfile`，:1265-1270）
  → 委派给 embedded-hub；`_ensureBackendsConfigured()`（:200）把
  credentialStore + sshTransport 注入 hub 和 assistantHost。
- **终端 IPC**（`term:open/ready/input/resize/close/listWindows/selectWindow/
  createWindow/copyMode/cancelCopyMode`，:1284-1293）。attach 契约 = 在 ssh2 PTY
  channel 上跑 `~/.cam/camc attach <agentId>`；会话以不透明 `t<N>-<ts>` id 存
  `_terminals`，按 WebContents 归属（`_ownedTerminal`）。内含：
  - ready-gate（`_termGateOpen/Send/Flush`，256 KB 上限、2 s 兜底，:950-999）；
  - 流式 UTF-8 解码器（:942）；
  - 远程尺寸修复：Python-over-stdin 的 tmux 脚本（:315-429）；
  - tmux client 发现，15 s 有界排队（:826-846）；
  - context 端点失败时按 agent machine 字段回退重试（:884-893）。
- **文件/剪贴板 IPC**（`files:pickPrivateKey/pickFile/saveText/saveFile/
  readClipboardText/readClipboardAttachments`，:1274-1280）；附件上限 50 MB。
- **Assistant IPC**（`assistant:*`，:1299-1317）。
- **`app:reset`**（:1239）：app 内软重置——销毁终端、清空 SSH 池、释放卡住的
  tmux-discovery gate、重启 hub、重新给 assistant 注入 hub pair。`cam:restart`
  是完整 relaunch。`diag:log`/`diag:tail` 维护 `userData/cam-desktop.log`。
- `powerMonitor.on('resume')` → `sshTransport.dropIdleEntries()`（:1333）；
  `before-quit` 销毁终端并停 hub（:1344）。

### 3.2 `embedded-hub.cjs`（5255 行，~234 KB）— Direct 模式的心脏

- Electron 主进程内的**自包含 Node HTTP 服务器**，stdlib-only。绑
  `127.0.0.1:8420` 或 8420-8469 中第一个空闲端口（再不行让 OS 分配）；bearer
  token = 每次启动 `crypto.randomBytes(24).base64url`。
- 存储：单文件 JSON `<userData>/embedded-hub.json`。
- 路由 `handle()`（:4088）实现 CAM API 的严格子集（即渲染层 `web/js/api.js`
  已经会说的那套）：`/api/system/health|config`、`/api/contexts`（含
  `/:nameOrId`，sync/DELETE 带凭据级联）、`/api/agents`（list `?refresh` →
  `_syncAllAgentContexts`；POST start → 经 SSH 跑 `camc run`）、
  `/api/agents/:id`（PATCH/DELETE/output/fulloutput/input/key/upload/cron）、
  `/api/api-models`、`/api/skillm/*`、`/api/extensions*`（安装/serve/配置）、
  `/api/system/ssh-config`（解析 ssh_config 供导入）。`/api/ws` 未实现——渲染层
  回退到 5 s REST 轮询。
- `_ensureRemoteCamc`/`_ensureRemoteSkillm`：把 bundled 二进制经 SFTP 部署到节点，
  **版本规则：永不降级更新的远端**（不是 hash 规则）。
- workspace 浏览（`_browse*`）；ProxyJump 链解析（:2514，最深 3 层）；
  `getAttachConnectOpts`（:5170）供终端 attach 使用。
- 只导出 `configure, start, stop, restart, check, getLogs, getProfile,
  getAttachConnectOpts`。

### 3.3 `ssh-transport.cjs`（1075 行）— ssh2 封装

- **每个 endpoint 两个池**：`_pool`（exec：camc list/capture/sync、SFTP、tmux
  控制）和 `_termPool`（交互式 PTY channel）——exec 超时永远不会杀掉活终端。
- keepalive 15 s×3；handshake 重试 ×1；错误分类（`connect_timeout`、
  `auth_failed`…）；ProxyJump 经 `forwardOut` 链（`_chainSock`，:298）；
  OS 唤醒后丢 idle 条目；`poolStats()`。
- 导出 `execRemote, writeRemoteFile, listRemoteFiles, readRemoteFile,
  openTerminalChannel, closeAll, dropIdleEntries, poolStats, setLogger`（+ 测试钩子）。
- 依赖 `ssh2`（^1.16）——**纯 JS 是硬要求**（MAS 安全、无 native spawn）；
  `asarUnpack` 覆盖 `ssh2` + `cpu-features`。

### 3.4 `credential-store.cjs`（220 行）

- 包 Electron `safeStorage`；存储 `<userData>/embedded-hub-credentials.json`
  （`{ref: {kind, blob_b64, saved_at}}`，0600，tmp+rename 原子写）。
- 加密不可用时**拒绝保存——永不回退明文**。ref 形如 `<contextId>:password`、
  `assistant:llm-token`；`removeWithPrefix`/`removeForContext` 做级联删除。

### 3.5 `assistant-host.cjs`（767 行）

- 拥有内置 assistant 扩展：以 `ELECTRON_RUN_AS_NODE`  spawn bundled pi 子进程
  （`cam-assist.js`）；LLM token 只存在 credential store（`assistant:llm-token`）；
  配置 + 线程 transcript 持久化在 `<userData>/ext-data/assistant/`；所有事件经
  `webContents.send('assistant:event')` 推给渲染层。`setHub({url, token})` 在每次
  hub 启动后重新注入（token 会轮换）。默认 LLM 端点
  `https://inference-api.nvidia.com/v1`（默认模型 `nvidia/moonshotai/kimi-k3`）。
- **历史是有损 live log（assistant v0.4.7 实测，三层上限叠加）**：
  1. 内存 ring buffer `MAX_EVENTS=1000`（:28，:315-316 超出即 splice 丢弃）；
  2. 磁盘 JSONL（`ext-data/assistant/threads/<id>.events.jsonl`）超过
     `EVENTS_MAX_BYTES=1.5MB` 就**重写、只留最后 `EVENTS_KEEP=300` 条**
     （:29-30，:213-216）——旧历史**物理丢弃**；加载时也只读最后 300 行（:227）；
  3. UI 首次打开或 cursor 掉队时 replay 只给**最后 200 条**（`poll()` :614）。
  放大器：每条流式 delta 都算一条事件（`_pushEvent(msg)` :415），一条长回复
  可产生上百条事件——可见**消息**数远少于事件数，丢得比直觉快。连带影响：
  `openThread` 用加载到的事件（≤300 条）给 child transcript 播种上下文
  （:663-666），丢的历史连 LLM 上下文一起丢；`reset()` 直接删事件文件（:625）。
  这是设计取舍（best-effort，"never break the chat over disk hiccups" :219），
  不是 bug。若以后要完整历史：rewrite 时归档轮换而非丢弃 + view 分页加载。
- **pi 侧没有另一份完整 session 存档**（0.4.7 核实）：child 用的是
  `@mariozechner/pi-agent-core` 的 `Agent` 类（entry.js:46,297-301），构造时
  **没传任何 session/持久化配置**——transcript 纯内存（`agent.state.messages`）；
  该包 dist 里不存在 session 文件写入（grep 实证）。pi 生态里有 session 文件
  的是 pi coding agent CLI 产品层，cam-assist 刻意只用 core（"We own this
  contract no matter how upstream pi evolves" entry.js:10-11）。重载机制是
  有的：线程切换时 host 发 `{type:'load', messages}`，child 用 pi-agent-core
  的 messages setter 替换 `agent.state.messages`（entry.js:358-371）——但
  播种源就是那份有损 events.jsonl（user/assistant echo 进日志，
  entry.js:314-316 的设计注释明确"host events.jsonl is the complete
  transcript on its own"）。所以 reload 的是"整个现存历史"，而现存历史本身
  已被三层上限裁过——**完整历史在任何地方都不存在**。
- **能力面现状（0.4.7）：无 memory、无 skills**。
  - pi-agent-core 库本身就是最小 agent loop：dist 里 grep 不到
    memory/compact/skill 任何字样（实证）——那些功能在 pi coding agent
    产品层，cam-assist 不用那层。"记忆" = 内存 `agent.state.messages`：
    无跨会话长期记忆、无 memory 文件、无 compaction；跨线程靠 host 播种，
    进程重启后从有损 events.jsonl 重建。
  - skills：child 只注册 `cam`（必有）+ `bash`（可选，需 cam-pi 桥）两个
    工具（entry.js:296）；host 侧也无 skills/persona 代码（grep 实证）。
    **skills 是设计好的未实现项**：assistant-design.md:79-80（CAM 诊断
    skills：cam-desktop-netdiag、camc-diagnose、managing-camc + 生成的
    hub-api skill）、:168-170（不烘进 bundle，首启写入 assistant
    workspace，数据文件可编辑免重建）、:468-469（roadmap）。当前平替 =
    系统提示词里硬编码的端点目录（entry.js:78-104），即 hub-api skill
    的临时形态。

### 3.6 `preload.cjs`（215 行）— 渲染层唯一的桥 `window.CamBridge`

- 暴露：`getPlatform/getAppVersion/getSystemUser/openExternal/restartApp/resetApp/
  diagLog/diagTail`，`directHub.*`（→ `local:*`），`files.*`，`net.probe`，
  `assistant.*`（+ `onEvent` 推送轨），`term.*`（+ `onData`/`onStatus` 订阅）。
- **沙箱铁律：preload 里只允许 `require('electron'|'events'|'timers'|'url')`**。
  一个非法 require 会杀死整个桥（2026-07-22 `require('os')` 事故，
  DEBUGGING.md §3.1）。有测试守着（mobile-nodes-form.test.cjs 也含 preload
  require 白名单断言）。

### 3.7 `tmux-controls.cjs`（87 行）

- 纯辅助：`tmuxMetadataForAgent`（校验 agent 记录里的 session/socket/bin——
  **不猜 `/bin/tmux`**）、client 集合 diff、状态/窗口解析、`tmuxCommand` 严格
  shell 转义（值 ≤512 字符、无元字符、绝对路径）。

---

## 4. 渲染层（`web/`）

### 4.1 `web/desktop.html`（1192 行）

- CSP（:6-7）：`default-src 'self'; script-src 'self'; connect-src *; frame-src
  http://127.0.0.1:* http://localhost:*` → **无内联 module 脚本**、无 CDN、扩展
  iframe 只允许 loopback 源。本文件没有任何内联 `<script>`。
- `<meta name="cam-version" content="v0.64.0">`。
- CSS：`css/style.css`（三端共享基底）、`css/desktop.css`（desktop 层）、
  `css/todos.css`、`vendor/xterm/xterm.css`，各带 `?v=`。
- 经典 UMD 脚本（必须先于 module 加载）：xterm 三件套（→
  `window.Terminal/FitAddon/SerializeAddon`）、marked、dompurify（Browse 模式
  markdown 预览）。
- modulepreload + 唯一入口 `<script type="module"
  src="js/desktop/app.js?v=…">`。
- 挂载点：`aside#sidebar`（`.mode-nav` 导航按钮、`#conn-bar` 连接状态、
  `#sidebar-agents-content` 项目列表）+ `main#main` 里一组
  `.mode-panel[data-mode]`（用 `[hidden]` 切换，**永不卸载**）：`#mode-agents`
  （输出模式 Plain/Rich/Terminal/Browse、**Ext▾ 按钮 `#agent-ext-btn`**、agent
  设置面板、输出窗格、composer）、`#mode-settings`、`#mode-start`、
  `#mode-nodes`、`#mode-extensions`（`#ext-list`、`#ext-view-wrap` iframe 容器、
  `#ext-edit-wrap` 属性编辑器）、`#mode-skills`、`#mode-bots`、`#mode-todos`。

### 4.2 `web/js/desktop/`（11 个文件）

| 文件 | 规模 | 职责 |
|---|---|---|
| `app.js` | 601 行 | **入口/boot**：mode 宿主、连接配置、5 s 轮询 |
| `shell.js` | 2089 行 | 侧栏 agent 列表、筛选/排序弹层、chips、user-activity overlay |
| `agent-console.js` | 4273 行 | Agent 控制台：输出模式、xterm 终端、Browse、composer、Ext▾ 菜单 |
| `settings-mode.js` | 30 KB | Settings 页（Direct/Appearance tab；Relay tab 隐藏），挂载 Diagnostics |
| `start-agent-mode.js` | 26 KB | Start 表单 → `POST /api/agents` |
| `nodes-mode.js` | 124 B | 只 re-export `../shared/nodes-mode.js` |
| `skills-mode.js` | 21 KB | Skillm 仓库/安装工作台（native 扩展页 `native: skills`） |
| `extensions-mode.js` | 387 行 | Extensions 页、`openExtensionView`、`applyExtChrome`、属性编辑器 |
| `diagnostics-mode.js` | 3.4 KB | 诊断日志查看器（`CamBridge.diagTail`） |
| `todos-mode.js` | 1.1 KB | 适配器：把 `shared/todos-workspace.js` 挂进 `.todos-workbench` |
| `bots-mode.js` | 18 KB | 只读 fixture UI；mode 隐藏 |

### 4.3 启动与导航

- `init()`（app.js:450）：从 localStorage 恢复 `cam_desktop_mode`（经
  `PERSISTENT_MODES` 校正）→ 订阅 state→`applyModeToDom` → 绑导航点击 →
  注册 `api.onEvent(handleEvent)` → 挂载所有 mode → `autoStartConnection()`
  （:406-446）：Relay profile → connect；Direct profile → connect（失败则重启
  embedded hub 试一次）；**无 profile + 有桥 → `CamBridge.directHub.start()` →
  存 `cam_server_url`/`cam_token` → connect**；无桥（浏览器开发）→ 落在
  Settings。
- 5 s `setInterval` 刷 agents（每第 6 拍刷 contexts/adapters）。
- **导航 = `setMode()` + `[hidden]` 切换**，没有 router。`MODES`（:38），
  `HIDDEN_MODES={'bots'}`（:37）。面板保持挂载，composer/滚动/xterm 状态在
  切换后存活。
- localStorage 键：`cam_server_url`、`cam_token`、`cam_relay_url`、
  `cam_relay_token`、`cam_profile_kind`、`cam_desktop_mode`、
  `cam_desktop_user_activity_at`、`cam_cache:*`（GET 缓存，5 min TTL，50 条上限）。
- "Reload app" 按钮 = `CamBridge.resetApp()` + `location.reload()`。

### 4.4 数据层 `web/js/api.js`（836 行）+ `state.js`（72 行）

`CamApi` 三模式 `direct|relay|disconnected`：

- `connect()`（:72-168）用 `Promise.any` 竞速：direct 探针（`GET /api/contexts`
  4 s）、relay-HTTP 探针（`/api/system/health` 8 s，WS 兜底）、纯 relay-WS；
  每次尝试记录在 `lastConnectDiagnostics.attempts`。
- **Direct 传输** = fetch + Bearer；超时 15 s 常规 / 120 s 慢操作
  （sync|heal|upload）/ context sync 用 45 s idle 制（Nodes 轮询器
  `syncHeartbeat()` 续命）。
- **Relay 传输** = REST-over-WebSocket：帧 `{id,method,path,headers,body}` 发往
  `<relayUrl>/client?token=…`；30 s 请求超时（input/key/upload 120 s）会强制关
  整个 WS；25 s `{ping:true}` 心跳；**`_scheduleReconnect` 每 1 s 无限重试，无
  backoff 无上限**（已知问题，mobile 侧同样存在，见 web/AGENTS.md P1）。
- GET 缓存 localStorage `cam_cache:*`（output/logs/uploads/skillm 除外）。
- 事件流：direct → 独立 WS `/api/ws`（10→60 s 退避、3 次上限、
  `document.hidden` 感知）；relay → 复用 client socket 发 WS 帧。desktop 的
  `handleEvent`（app.js:304-317）把 `status_update`/`state_change` 映射进
  `state.updateAgent`。
- REST 面：agents CRUD/output（hash 去重）/input/key/upload（JSON base64 +
  multipart 兜底）、contexts CRUD/sync/heal/sync-status、workspace files、
  ssh-config 导入、**extensions**（`listExtensions`、`extCall`、`extConfigGet/Set`）、
  skillm。

`state.js`：`AppState` 单例，`_data = {agents, contexts, selectedAgentId,
connectionMode, filters, toast}` + 每 agent `_outputCache`；全广播订阅（无 key
过滤）。

### 4.5 `web/js/shared/`（desktop 相关部分）

- `ext-bridge.js` — **父侧 postMessage 桥 + capability 门控**（desktop 扩展核心）。
- `ext-view-host.js` — sandboxed iframe 挂载器。
- `direct-settings.js`、`hub-capabilities.js` — desktop/mobile 共享。
- `nodes-mode.js`（99 KB）— 整个 Nodes 工作区，desktop 只是 re-export。
- `todos-controller.js`/`todos-workspace.js`/`worklog-core.js` — Todos 域。
- `loop-templates.js`/`workflow-templates.js`/`template-store.js`/
  `prompt-sections.js`/`workflow-yaml.js` — shell.js 的 Automation/Workflow tab 用。
- `term-bridge.js` — 终端桥抽象（desktop 上是 `window.CamBridge.term`）。
- `terminal-mount.js`（39 KB）— **mobile-only 消费者**；desktop 控制台在
  `agent-console.js` 里有自己的 xterm 代码。改它前想清楚影响面。
- `mobile-appearance.js` 等 — mobile-only。

### 4.6 CSS

- `css/style.css`（28 KB）三端共享基底。
- `css/desktop.css`（147 KB，~5750 行）desktop 层。约定：
  - 文件顶部 `[hidden]{display:none!important}` 守卫（:8-10）**是承重的**——
    组件自己的 `display` 规则会静默击败 `hidden` 属性；
  - `body.desktop` 设计 token（:12-50）；
  - **页面级规则一律用 `#mode-<mode> …` 命名空间**（237 处），绝不用通用类做
    显隐（当年一个通用选择器把页面自己的 Back 按钮藏了）；
  - 扩展 chrome：`.mode-panel > .ext-chrome` + `.mode-panel.ext-chromed >
    .settings-header{display:none}`；`.ext-view-frame`。

### 4.7 `?v=` 缓存戳纪律（血泪规则）

- desktop **没有**盖戳脚本，全靠手工：编辑了哪个文件，就把它的 `?v=` bump，
  并**同步改所有 import 处和 `desktop.html`（modulepreload 和 script 标签都要）**。
- **同一模块的每一次 import 必须用完全相同的 `?v=` URL**。ES module 按完整
  URL 键控实例：同模块两个不同戳（或一个有戳一个裸）= 模块被求值两次 =
  模块私有状态分裂 = 跨模块交接静默失败。`ext-nav.test.cjs` 守着
  extensions-mode 这条链。
- 反面教材：`agent-console.js` 里曾声明局部 `function setMode`，hoist 后遮蔽了
  app 层传入的 `setMode` 挂载参数，Ext▾ 导航死了好几周（后改名
  `setOutputMode`）。**永远不要让局部函数与挂载参数同名。**
- ⚠️ **现存 live bug（2026-08-18 仍在）**：`app.js:13` import
  `./shell.js?v=0.65.1`（与 html modulepreload 一致），但 `agent-console.js:19`
  import `./shell.js?v=0.64.0` → shell.js 现在是两个实例。后果：
  user-activity overlay（`_userActivity` Map）分裂——`agent-console.js:1500` 写
  一个实例，侧栏排序（shell.js:381）读另一个，两边还把全量 map 写同一个
  localStorage 键（last-writer-wins）。修法：统一戳。`ext-nav.test.cjs` 未覆盖
  shell.js。

### 4.8 vendor / V1 PWA

- `web/vendor/xterm/`（UMD `.js` 是实际发货的；npm 里的 `@xterm/*` 只是声明）、
  `vendor/marked/`、`vendor/dompurify/`。
- `web/manifest.json` + `web/sw.js` 只属于 V1 PWA（`desktop.html` 不引用）。
  V1 冻结；其 `?v=` 在 `index.html`/`js/app.js`/`sw.js` 三处同步。

---

## 5. 扩展系统（`extensions/`）

### 5.1 Extension-first 规则（2026-08-11 用户指令）

0.2.4 扩展稳定轮之后，**扩展工作不要再打 MSI**：

- 新工具/功能 → 做成扩展（iframe view + `main.py` 远程工具 + 每扩展属性，
  Extensions → Settings 编辑）。
- 内置工具的逻辑更新 → 以**同名用户包**发货（shadowing 机制）。
- 只有这些才值得动 app 壳（新 MSI）：新 bridge 能力、新 native 页面、
  registry/hub/transport 修复。

### 5.2 包结构与类型

一个扩展 = 一个文件夹：`manifest.yaml`（name `[a-z0-9-]{1,32}`、version、
title、description、capabilities、可选 `native:`/`mounts:`）+ 可选 `index.html`
（sandboxed iframe view）+ 可选 `main.py`（远程工具，**python3.6 stdlib-only**）。
入口解析：`index.html` 优先，否则唯一 `*.html`，否则 `view_ambiguous`；
`main.py`/`*.py` 同理。

四种类型（SPEC v2）：

- **A = iframe view 工具**：`<iframe sandbox="allow-scripts">`，src =
  `${serverUrl}/ext/<name>/<viewFile>?token=<view-token>`；view token 来自
  `GET /api/extensions/view-token`，**每次启动一个新 view token，绝不用 API
  token**。
- **B = 远程工具**：`main.py` 经分块 SFTP 部署到 SSH 主机的
  `$HOME/.cam/extensions/<name>/`，调用 `python3 main.py <method> '<json>'` →
  stdout 一个 JSON 对象，30 s 预算；**hub 本机从不跑 Python**。
- **C = 本地 agent 二进制**：`bin/<platform>/` 下，经 node-pty spawn（仅非 MAS
  构建；**v2 通道尚未实现**）。
- **N = native 页面**：一方内置（如 skills/todos/assistant），view 是 app 内建
  mode 页；包里没有 `.html`，带 `native: <mode>`。打开 = `applyExtChrome` +
  `setMode(native)`。

### 5.3 桥协议（shell ↔ iframe）

- view 里 `<script src="../client.js"></script>`（hub 在 `/ext/client.js` serve
  `extensions/host/ext-client.js`），用 `camExt.call(...)`/`camExt.onEvent(...)`。
- 父侧 `web/js/shared/ext-bridge.js`：view post `{extBridge:true, id, method,
  args}` → 父回 `{extBridge:true, response:true, id, ok, result|error}`。frame 必须
  已注册；capability 门控按 **manifest 声明**校验。
- 方法：开放读 `agents.list`/`contexts.list`/`agents.capture`；绑定 agent 后
  （`mounts:[agent]`）`app.context`/`agents.cronJobs`/`agents.workspaceList/Read`；
  `ext.call`（需 `exec` capability）；`files.read`（需 `files:read`）；
  `ext.config`（自己的属性，永远允许）；`assistant.*` 家族——**按名字门控**
  （`reg.name !== 'assistant'` → `assistant_unavailable`），转发到
  `CamBridge.assistant`；推送轨 `assistant:event` → `assistant.event` →
  `camExt.onEvent`。
- view 侧 CSP `default-src 'self'`，无 CDN，摸不到 parent/cookie/localStorage。

### 5.4 属性、数据与生命周期

- 每扩展配置是一个 JSON 对象，Extensions → Settings 编辑（`#ext-edit-wrap`，
  按 manifest `attributes` 行 `key | type | default | label | hint` 生成表单，或
  raw JSON 兜底），存在 hub 侧 `userData/extension-config.json`（**在包外**，
  重装/更新/shadowing 都存活；≤16 KB）。
- 平台统一属性只有一个：**`show_in_agent_menu`**（bool，默认 false）——agent 页
  Ext▾ 菜单完全由它驱动（初始为空）；`mounts` 只标记"可绑定 agent"。
- 扩展自用状态放 `userData/ext-data/<name>/`。完整删除级联：config + ext-data +
  credential-store 里 `<name>:` 前缀的秘密。
- 安装 = 校验 + 拷贝文件夹（或 `.tar.gz/.tgz/.tar`）到
  `userData/extensions/<name>/`；同名 = 更新；上限 8 MB 总 / 4 MB 单文件。
- **同名用户包 shadow 内置**（"built-in · updated by user copy"）——内置工具不打
  app 发布也能更新。0.2.26 起连 bundle 也能 shadow（用户装的 `cam-assist.js`
  优先于 bundled；MAS 上按 Apple 2.5.2 禁用）。删除 shadowing 用户副本会回退到
  内置并保留配置。
- 0.2.24 起内置扩展也是统一的 Open/Settings/Enable/Remove 行操作；Remove 内置
  = hub 用 store `removed` 标志隐藏 + `_extCascadeDelete` 级联（代码里**不得**再
  出现 `builtin_not_removable`）。

### 5.5 目录与参考实现

- `extensions/host/` — hub 侧运行时：`registry.cjs`（manifest 解析/校验/安装/
  注册）、`tool-proxy.cjs`（远程部署+调用）、`ext-client.js`。
- `extensions/packages/` — 内置：`assistant/`（**canonical sample**，测试用它当
  fixture；含 manifest + index.html + main.py + cam-pi.js）、`skills/`、
  `todos/`（manifest-only native 页）。
- `extensions/examples/agent-doctor/` — 可安装的 A+B demo（tar.gz 在
  `dist/ext/`）。
- `extensions/vendor/cam-assist/` — bundled pi agent 的源码与 dist（minified
  `cam-assist.js`，由 assistant-host spawn；含通用 `cam` backoffice 工具，被限制
  在 `/api/*` 路径、注入 hub pair、审计 `hub_call` 事件、token 不经渲染层）。
  - **assistant 历史里显示的 `cam GET /api/...` 不是 cam/camc CLI**：它是
    `entry.js:127-163` 定义的 LLM 工具 `cam{method, path, body}`（label "CAM
    Desktop"），generic 设计——任何现有/未来的 hub 端点都覆盖，无需重打
    bundle。执行位置：pi agent 子进程（`ELECTRON_RUN_AS_NODE`，本机、无
    shell）里 `fetch(hub.url + path)`，打到**本机 loopback 的 embedded hub**
    （`127.0.0.1:842x`，Bearer 是 host 注入的轮换 token）；涉及节点的端点
    （start/capture/sync 等）由 hub 内部再经 SSH 到远端跑 `camc`。约束：path
    必须 `/api/` 开头、30 s 超时、响应截 12000 字符、每次调用 emit
    `hub_call` → host 写 `[assistant]` 审计行进 cam-desktop.log
    （assistant-host.cjs:390）。历史行的 `METHOD /api/path` 摘要格式在
    `entry.js:256-257`（`bash` 工具则显示命令行前 80 字符——那是另一个工具，
    只有用户粘贴了 cam-pi 桥接行才注册）。
- 写扩展先读技能 `.agents/skills/cam-ext-authoring/SKILL.md`（SPEC+GUIDE 的
  浓缩流程），再读 `extensions/SPEC.md`（v2 契约）和 `GUIDE.md`（实操）。

---

## 6. 构建、版本与发布

### 6.1 npm scripts（`apps/cam-desktop/package.json`）

| 脚本 | 命令 | 说明 |
|---|---|---|
| `start` / `dev` | `electron .` | 本地跑（无头服务器：`xvfb-run -a npm run dev`） |
| `build` / `dist` | `electron-builder` | |
| `build:win` | `electron-builder --win` | |
| `build:win-msi` | `electron-builder --win msi --config.win.signAndEditExecutable=false --config.win.verifyUpdateCodeSignature=false` | 出 MSI |
| `build:mas` | `electron-builder --mac mas --universal --config.npmRebuild=false --publish=never` | MAS 线（parked） |
| `cli` | `node cli/camui-cli.cjs` | 调试 CLI（§6.6） |
| `lint:electron` | `node --check` 主进程各文件 + cli | |
| `test:hub` / `test:term` / `test:start` / `test:smoke` | 见 §7 | |

`legacy:tauri:*` 是死脚手架（`src/`、`src-tauri/`、`index.html`、`vite.config.ts`
同属 Tauri/React 遗留，**发货的 app 不加载它们**，别被迷惑）。

### 6.2 electron-builder 配置（inline 在 package.json）

`appId: com.hren.cam`，`productName: CAM Desktop`；`files: [electron/**, cli/**,
package.json]`；**extraResources**：`../../web` → `web/`、`../../dist/camc` →
`camc/camc`、`../../dist/skillm` → `skillm/skillm`、`../../extensions` →
`extensions/`（去掉 vendor node_modules）。Windows：MSI x64 only，不签名，
`msi.perMachine: false`，产物 `CAM-Desktop-${version}.msi`。mac：DMG。MAS：
独立 `appId com.hren.cam.mas` + `build/entitlements.mas*.plist`（只需
network.client/server + keychain + user-selected file read）。

### 6.3 版本规则

- 唯一事实源：`apps/cam-desktop/package.json` 的 `"version"`（当前 0.2.29），
  手工 bump，无 bump 脚本。它进 artifact 名和 `app.getVersion()`。
- `package-lock.json` 的 root version 停在 0.2.0（已知漂移）：**本地用
  `npm install`，不要 `npm ci`**（CI 上 `npm ci` 反而正常）。不要 `npm audit fix`。
- **同版本 MSI 不会替换文件**（Windows Installer 规则）：本机测试安装前要先
  卸载，或者 bump 版本。
- MSI 构建跑着的时候**不要改源码**（WiX EBUSY）；构建被杀后，先杀掉孤儿
  `electron-builder`/`light.exe`，`rm -rf dist/__msi-x64`，再重建。
- 本地构建：`cd apps/cam-desktop && npm run build:win-msi`（Windows 侧，5-30
  分钟；WSL 里用便携 Node：`export PATH="<repo>/.tools/node:$PATH"`）。
- 渲染层是**未打包**地放在 `resources/web/` 下发货的——验证 MSI 内容时 grep
  那里，不是 app.asar。

### 6.4 发布流程（`apps/cam-desktop/RELEASE.md` 为准）

1. **发布门槛**（必须全绿）：
   `npm run lint:electron && npm run test:hub && npm run test:term && npm run
   test:start` → `npm run test:smoke` → 真机 E2E attach 检查（CDP harness：
   `.tools/cdp-*.cjs` 配 `--remote-debugging-port`）。全程 minimal diff。
2. commit → `git push origin camui-desktop-v2`（GitLab 主库）+
   `git push github camui-desktop-v2`（构建农场，repo `orlunix/cam`）。
3. `git tag -f cam-desktop-v<version> <sha>` → `git push -f github
   cam-desktop-v<version>`。tag pattern `cam-desktop-v*` 触发：
   - `.github/workflows/cam-desktop-windows.yml`：windows-latest、Node 20、
     `npm ci`、`lint:electron`、`build:win-msi` → `gh release upload --clobber`。
     无签名、无 secrets。
   - `.github/workflows/cam-desktop-macos.yml`：macos-latest arm64；`CSC_LINK`
     存在时签名 + notarize（App Store Connect API key 四件套）→ DMG 上传。
   - `.github/workflows/cam-desktop-mas.yml`：parked 的 MAS pkg 线。
4. 校验：轮询 `api.github.com/repos/orlunix/cam/actions/runs`，比对 release
   asset 的 sha256（`assets[].digest`）与发货文件。
- Secrets 六个：`CSC_LINK`、`CSC_KEY_PASSWORD`、`APPLE_API_KEY{,_ID,_ISSUER}`、
  `TEAM_ID`。**p12 坑**：Apple `security import` 拒 OpenSSL-3 PBES2 的 p12——
  用 `openssl pkcs12 -export -legacy` 重导出（MAS 线上更稳的做法是直接以 PEM
  导入 identity）。
- MAS 11 条坑见 `MAS-SUBMISSION.md`（90869 arm64-only 拒审 → 必须
  `--universal`；`productbuild` 要 `-T` + `timeout-minutes: 25`；pkg 在
  `dist/mas-*/` 子目录 → glob `dist/**/*.pkg`；等等）。手动剩余项：每构建的
  export-compliance PATCH、销售地区、截图、最终提交按钮。

### 6.5 camc 联动（容易踩）

- 仓库根的 `scripts/release.sh` 发布的是 **camc**（远程 agent 二进制），不是
  desktop：Linux 上 `python3 build_camc.py` → pytest → scp 到
  `~/.cam/machines.json` 里的机器。
- **绝不在 Windows 上跑 `build_camc.py`**（CRLF/反斜杠会腐蚀 polyglot；Linux 构建
  的、已提交的 `dist/camc` 才是 artifact）。camc 变了 → Linux 重建 `dist/camc`
  → 同步 `dist/camc` → `src/camc` → **再打 MSI**（MSI 经 extraResources  bundle
  它；这个错位咬过人一次）。
- MSI 还 bundle `dist/skillm` 和整个 `extensions/`——同理，它们变了才需要新
  MSI；只改 `web/` 或扩展逻辑时优先想 shadowing / 扩展发货。

### 6.6 调试 CLI（`cli/camui-cli.cjs`，797 行，`npm run cli`）

脱离 Electron 复用同一套三模块（embedded-hub + credential-store +
ssh-transport）：`camui [--json] status|start|logs`、`node list|add|sync`、
`agent list`；Relay source 模式 `camui start --profile <name> --relay-url ws://…
--relay-token <t>`（profile 存 `~/.cam/camui/relay/<profile>/profile.json`，
0600；日志只显示 token 的 SHA-256 指纹）。无 Electron 就没有 safeStorage，
`--remember` 不可用。

---

## 7. 测试体系

- **无框架**：每个 `test/*.cjs` 是自足脚本，`assert` + 通过/失败计数，失败时
  非零退出。风格是 **grep-assertion / 静态链检查**（pin 精确源码字符串）+
  进程内 harness。
- 铁律（DEBUGGING.md §7）：行为有意改变时，**同一个 commit** 里更新被 pin 的
  字符串并加新断言——**永不靠删断言过测试**。
- 没有"一把跑全部"的脚本；发布门槛显式列出（§6.4）。

| 文件 | 跑法 | 内容 |
|---|---|---|
| `hub.test.cjs` | `npm run test:hub` | 进程内 embedded-hub 测试（mock credentialStore/sshTransport），驱动真实 HTTP `/api/*` |
| `terminal-follow.test.cjs` + `tmux-controls.test.cjs` | `npm run test:term` | xterm 滚动跟随语义、terminal-mount 链；tmux-controls 单测 |
| `start-form.test.cjs` | `npm run test:start` | Start 表单字段序 + pin 本地会话退役（`local_unsupported`） |
| `boot-smoke.test.cjs` | `npm run test:smoke` | 起真实 Electron（隔离 userData），断言 preload 加载、CamBridge 存在、hub 启动。需要 display，15-20 s |
| `ext-nav.test.cjs` | `node test/ext-nav.test.cjs` | 扩展导航稳定链（下面详列） |
| `extensions.test.cjs` | 直接 node | 扩展注册表 + hub ext 端点（fixture：`extensions/packages/assistant`） |
| `assistant.test.cjs` | 直接 node | AssistantHost + 真实 cam-assist bundle 打 mock OpenAI 端点 |
| `cam-pi.test.cjs` | 直接 node | `cam-pi.js` 本机 shell 伴随（listen 行、bearer、health、exec） |
| `ssh-jump.test.cjs` | 直接 node | ProxyJump 链（mock ssh2：池键、forwardOut、`jump_unreachable` 归因） |
| `mobile-nodes-form.test.cjs` | 直接 node | mobile Nodes 表单链 + preload require 白名单断言 |
| `mobile-rich-output-isolation.test.cjs` / `mobile-sync-host-timing.test.cjs` | 直接 node | mobile 侧 pin |
| `start-agent-tool-options.test.cjs` | 直接 node | 三端 Start 表单的 tool-option 一致性 |

**`ext-nav.test.cjs`（193 行）守护清单**——每条断言对应一个真实上线过的 bug：

- **A**：native agent-doctor 页必须彻底消失（它已是
  `extensions/examples/agent-doctor/` 下的自包含 iframe 扩展）。
- **B1**：doctor 包契约（manifest 无 `native:`、保留 `exec`、view 加载
  `../client.js`、调 `agents.list`/`app.context`/`ext.call('review')`）。
- **B2**：Ext▾ 菜单链（`x.hasView || x.native` 过滤、native 走
  `setMode(ext.native)`、选中 agent 经 `bindContext:{agentId}` 传给 iframe）。
- **B3**：`agent-console.js` **永不声明局部 `function setMode(`**（输出切换器
  必须叫 `setOutputMode`）。
- **B4**：`applyExtChrome` 由 `extensions-mode.js` 导出，agent-console 以与
  `openExtensionView` **完全相同的 `?v=` URL** import；chrome CSS 规则。
- **B5**：每扩展属性链（api.js `extConfigGet/Set` ↔ 桥 `ext.config`/`app.context`
  ↔ hub `/api/extensions/<name>/config` ↔ `#ext-edit-wrap` 编辑器 ↔
  `show_in_agent_menu`）。
- **B6**：统一行操作（Open/Settings/Enable/Remove）；hub 用 `removed` 标志隐藏
  内置 + `_extCascadeDelete`；不得有 `builtin_not_removable`。
- **B7/B7b**：内置 assistant 端到端链（committed bundle、`assistant:*` IPC、
  preload 组、桥名字门控、`ELECTRON_RUN_AS_NODE`、token 只在 credential
  store、hub 不 spawn assistant、推送轨、threads API、cam-pi 复制启动命令链）。
- **B8**：bundle 里的通用 `cam` backoffice 工具：限 `/api/*`、hub pair 注入、
  审计事件、token 不过渲染层。

---

## 8. 调试与诊断

- 日志：`userData/cam-desktop.log`（`diag:log`/`diag:tail` 维护，渲染层
  Diagnostics tab / `CamBridge.diagTail` 可看）。
- 软重置：`app:reset`（Reload 按钮）≠ `cam:restart`（完整 relaunch）。
- 无头环境 dev：`xvfb-run -a npm run dev`；或直接 curl embedded-hub 的 HTTP 面。
- CDP harness：`.tools/cdp-*.cjs` + `--remote-debugging-port`（真机 E2E 也用这套）。

**失败目录**（DEBUGGING.md 精华）：

- 桥整个没了 → preload 里出现非法 require（§3.6，allowlist 只有
  electron/events/timers/url）。
- 15 s 超时 ≠ 认证失败——先分清 `connect_timeout` 和 `auth_failed`。
- rich output 正常但 attach 失败是常态——两条传输路（exec 池 vs 终端池），
  exec 池自愈、终端池不自愈。
- tmux 窗口宽度 = 所有 client 的最小值 → 出现 ~10 列窄窗口说明有外来 client；
  查日志里的 `REPAIR_DETACHED` 行取证。
- 渲染层全死 ⇒ 渲染层异常，看 CDP 的 `PAGE-EXCEPTION`。
- tmux 可移植性：只用最老的可移植命令形，做能力探测而不是版本字符串匹配。
- 远程是 csh 系 shell 时：bash 命令要 base64 包装。

**诊断 playbook**：netstat 看 842x 端口 → CDP console → 用真 ssh 复现 → 查节点
上 tmux 状态 → 查 desktop 日志。

**终端架构八条不变量**（TERMINAL-ARCHITECTURE.md）：pane 停屏外不卸载（3.1）；
两个 SSH 池各管一摊（3.2）；xterm grid 是唯一尺寸源，≥40×4 下限（3.3）；
ready-gate 缓冲首输出直到 `term:ready`（3.4）；channel 带 generation token
（3.5）；断线是会话状态、按键重连（3.6）；流式 UTF-8 解码（3.7）；camc 新鲜度
是版本规则不是 hash 规则——永不降级（3.8）。

---

## 9. 稳定性铁律（汇总，违反必出事故）

1. 同一模块的每次 import 用**完全相同的 `?v=` URL**；编辑文件后同步 bump 所有
   引用处 + `desktop.html`（modulepreload 和 script 两处）。
2. 页面 CSS 必须 `#mode-…` 命名空间；不用通用类做显隐；`desktop.css` 顶部的
   `[hidden]{display:none!important}` 守卫不许删。
3. 局部函数永不与挂载参数同名（`setMode` 事故）。
4. preload 只准 `require('electron'|'events'|'timers'|'url')`。
5. 永不向 PTY/tmux 发送 <40 列 / <4 行（5 处 clamp 点）。
6. 永不 idle-close SSH 池；exec 池与终端池隔离。
7. 绝不在 Windows 跑 `build_camc.py`；camc 更新后同步 `dist/camc` → `src/camc`
   再打 MSI。
8. 同版本 MSI 不替换文件——测试安装先卸载或 bump 版本；MSI 构建中不改源码。
9. 本地 `npm install`，不用 `npm ci`（lockfile 漂移）、不 `npm audit fix`。
10. 测试 pin 随行为变更同 commit 更新；永不删断言过测试；minimal diff。
11. **连接层改动（ssh-transport、认证、算法）必须在打包的 Electron runtime 上
    验证**，WSL node 只作参考——Electron 是 BoringSSL，dev node 是 OpenSSL
    （2026-07-28：chacha20-poly1305 在 WSL 过、在 MSI 里毁掉所有 attach）。
    不按名字枚举 feature-detected crypto 算法；以 append 形式用纯 JS legacy
    算法扩展库的 runtime-verified 默认值。MSI 冒烟测试才是判据。
12. 新功能默认走扩展 / 同名用户包（extension-first）；新 MSI 只为 bridge 能力、
    native 页面、registry/hub/transport 修复。
13. 秘密不进渲染层；credential-store 加密不可用时拒绝保存（不落明文）。
14. camc/skillm 部署到远端用版本规则——**永不降级更新的远端**。
15. 未完成的功能面用 hide-don't-remove：`UNFINISHED-HIDDEN(<name>)` 注释 +
    `HIDDEN_MODES` 校正。

---

## 10. 文档地图（哪些可读、哪些要防）

**阅读顺序**：`docs/desktop/README.md` → `docs/desktop/requirements.md`（唯一
canonical 清单，信任其状态标记）→ `docs/desktop-ui-spec.md`（产品方向；评审引
用 Req ID 而不是它）→ 按任务读专项。

| 文档 | 状态 |
|---|---|
| `docs/desktop/requirements.md` | **canonical 需求注册表**（§11） |
| `docs/desktop/README.md` | 架构一页纸 + Relay Quick Start；Files 列表本身略滞后（缺 assistant/bots/extensions/fast-switch/tmux 等新文档——按目录 listing 而不是它的列表导航） |
| `docs/desktop-ui-spec.md` | 当前产品/里程碑 spec v2 |
| `docs/desktop/extensions-spec.md` | 6 行指针 → `extensions/`。current |
| `extensions/SPEC.md` / `GUIDE.md` | 扩展系统 v2 契约 + 实操。current |
| `docs/desktop/skillm-integration.md` | Skillm v0 契约，已实现，current |
| `docs/desktop/assistant-design.md` | assistant 权威文档。**先读状态头**（MVP IMPLEMENTED 0.2.21，changelog 到 0.2.29）；正文下半是 pivot 前的旧设计；后部（home access/反向隧道/跨节点消息/computer use）是 parked 参考 |
| `docs/desktop/bots-page-plan.md` | UI v0 已实现；hub bot API 未落地（BOTS-011/013/014 仍 proposed） |
| `docs/desktop/rich-renderer-v2-spec.md` | 已实现（OUT-021）；golden fixture 在 /tmp 不在仓库 |
| `docs/desktop/terminal-tmux-interactions-{spec,plan}.md` | spec 是设计；plan 的 checkbox 未勾——**对代码核实完成度，别当真** |
| `docs/desktop/agent-fast-switch-{spec,plan}.md`、`fast-switch-brief.md` | 已实现（TERM-004/007）；brief 是执行 runbook（MSI 机制参考价值高） |
| `docs/desktop/camui-4improvements-brief.md` | 已执行 runbook；含"renderer 未打包发货，grep resources/web/ 而非 app.asar"等硬事实 |
| `docs/desktop/local-integrated-mode-spec.md`、`local-runtime-user-guide.md` | **SUPERSEDED，不要实现、不要遵循** |
| `docs/windows-installer.md` | **stale**（Tauri 时代）；真实流程是 electron-builder |
| `extensions/README.md` | "built-ins disable-only, never removable" 一行 stale（SPEC §9：0.2.24 已恢复统一行操作） |
| `docs/architecture-full.md` | 2026-03 自动生成，服务端视角，**无 Desktop 内容** |
| `docs/todos-shared-development-guide.md` | 3-tab 模型与 TODOS-011 的 5 tab 冲突时，以 requirements.md 为准 |
| `docs/archive/`、`docs/legacy/` | 历史资料，仅背景 |
| `apps/cam-desktop/` 内：`README.md`（定向）、`RELEASE.md`（发布 playbook）、`DEBUGGING.md`（调试圣经）、`TERMINAL-ARCHITECTURE.md`（终端不变量）、`APP-STORE.md` + `MAS-*.md`（MAS 线）、`FIXES-*.md`（各次修复的硬规则）、`LOCAL-NODE-DATAPATH.md`（已退役）、`CLAUDE.md`（camc 任务简报，untracked，别提交） | 均可信，按主题查 |

## 11. 需求 ID 注册表摘要（`docs/desktop/requirements.md`）

格式 `CAM-DESK-<AREA>-<NNN>`；状态 `proposed → approved → implemented →
verified`（+ `deferred`/`superseded`）；ID 永不重编号。评审/派活引用 ID。
Phase：1（Electron 壳，verified）、2E（Direct embedded Hub）、3（rich 输出 +
终端，verified）、4（mobile 对齐）、5（SSH attach，proposed）；2A-2D（local
bootstrap）deferred。

- **壳/基线**（verified）：ARCH-001/002（Electron 运行时、复用 WebUI
  CamApi/AppState）、CONN-001（同 localStorage profile 键）、AGT-001、OUT-001、
  INP-001/002、SET-001、PKG-001..003（单 per-user MSI 锁定 VDI 可装）、
  SEC-001/002。SET-002（外观 tab）、UI-001 approved。
- **架构/hub/node**：ARCH-010..013、HUB-010..013、NODE-010..013、
  DIRECT-010..020（Direct 生命周期全族）、CRON-010（Automation 单 tab）。
- **功能面**：SKILLM-010..016、BOTS-010..015（010/012 implemented）、
  TODOS-010..017、REMOTE-012..014（010/011 superseded）、SSH-010..013（全
  proposed）、OUT-010..022（rich 输出族）、NODEUI-010..017、
  TERM-001..007（**当前**终端模式；TERM-010..017 superseded）、
  LOC-010..024（superseded，勿派活）、RUN-010..015、EDIT-010..016、
  WORKFLOW-010..017、INP-010..016（图片附件）、FILE-010..018（Browse 模式）、
  VFY-001..005。

## 12. 当前状态快照（2026-08-18）

- HEAD：merge `98ce847`。近期 desktop 提交：`1081b44`（native agent-doctor 页 +
  Ext 菜单修复 + ext 文档）、`8babd33`（extensions v1 + markdown 预览，v0.2.4）、
  `1a2ed23`（ProxyJump + Nodes 导入导出）、`2977c62`（v0.2.3 sync UX 大修）。
- **工作区有 54 个 dirty 文件**：一轮 extensions/文档改动在途中（ext 包移向
  `extensions/examples/`、ext-nav B6 更新、assistant/hub/ssh 模块被触及）。
  新线程先 `git status` / `git diff --stat` 确认再动手。
- **现存 live bug**：`shell.js` 的 `?v=` 分裂（`app.js` 用 0.65.1、
  `agent-console.js` 用 0.64.0）→ user-activity overlay 双实例。详见 §4.7。
- package-lock root version 停在 0.2.0（已知漂移，勿用 `npm ci` 本地安装）。
- MAS 线 parked；剩余手动项见 `MAS-SUBMISSION.md` 末尾。
- mobile 侧有一组 P0-P3 已知稳定问题（`web/AGENTS.md`，2.4.51 核实）——改
  `js/api.js`、`js/state.js`、`js/shared/*` 这些共享文件时必须把 mobile 影响
  考虑进去（特别是 `?v=` 戳方案：mobile 由 `apps/cam-mobile/build.sh` sed 盖戳，
  desktop 全手工）。
- 渲染层版本戳现状：`desktop.html` meta `v0.64.0`，各模块戳在
  0.64.0…0.68.0 区间，彼此不要求一致，但同一模块的所有引用必须一致。

## 13. 新线程上手顺序

1. 读本文件 → `docs/desktop/README.md` → `docs/desktop/requirements.md` 的
   架构与连接模型节。
2. `git status` 确认工作区状态（§12）；别在别人的在途改动上叠盲目改动。
3. 跑一次冒烟：`cd apps/cam-desktop && npm run lint:electron && npm run
   test:hub && npm run test:term && npm run test:start`（有 display 再
   `npm run test:smoke`）。
4. 起 dev：`npm run dev`（无头用 `xvfb-run -a`），或直接
   `node cli/camui-cli.cjs status` 摸 hub。
5. 任务定性：能用扩展做就不动壳（§5.1）；动壳先看 §9 铁律。
6. 改渲染层 → bump `?v=` 并全局同步（§4.7）；改行为 → 同 commit 更新测试
   pin（§7）。
7. 连接层改动 → 预留一次完整 MSI 构建 + 打包 runtime 验证（§9.11）。
8. 发布严格按 `apps/cam-desktop/RELEASE.md`（§6.4），不得到用户授权不 push。

## 14. 环境速查

- 机器：Thinkpad，Windows 11 + WSL（本 repo 在 Windows 盘
  `C:\Users\Thinkpad\gitlab\cam`，WSL 路径 `/mnt/c/Users/Thinkpad/gitlab/cam`）。
- 分支：`camui-desktop-v2`（master 冻结在 2.2.0）。remotes：`origin` =
  GitLab（主），`github` = `orlunix/cam`（构建农场 / Release 宿主）。
- 便携 Node 20：`export PATH="<repo>/.tools/node:$PATH"`（WSL 里跑 npm/
  electron-builder 用）。
- MSI 构建在 Windows 侧跑（WiX），5-30 分钟；产物累积在
  `apps/cam-desktop/dist/`（`CAM-Desktop-0.2.x.msi`）。
- CDP E2E harness：`.tools/cdp-*.cjs`。
- mobile 版本事实源：`apps/cam-mobile/VERSION`（当前 2.4.51）；APK：`cd apps/cam-mobile &&
  ./build.sh`。
- mobile 资产的 WebDAV（坚果云）：`https://dav.jianguoyun.com/dav/`，账号
  `renhuailu@qq.com`，应用密码不入库（问用户或看
  `/home/hren/github/.cam-images/20260725-184757-clipboard-image-20260725-104757.png`）。
- camc 委派：`camc list` / `camc status f1a1a661` / `camc msg send f1a1a661
  -t "..."`（详见根 `AGENTS.md` 与技能 `camc-messaging`）。

## 15. Assistant 修复计划（history 丢失 / 无 memory / 无 skills，2026-08-18 定）

根因：child 被设计成无状态计算器，所有状态职责在 host，而 host 的存储策略是
"够用就扔"。三个问题一套修法，按"发货路径"分层——**entry.js 和 view 的改动走
bundle shadowing（同名用户包，免 MSI）**，只有 assistant-host.cjs 的一小块需要
app 壳更新（可攒到下次正当 MSI 一起发）。

### 15.0 兼容性约束

- stdio 协议是我们自己的（entry.js:10-11），所有新增字段必须向后兼容：
  新 child + 老 host（无 `dataDir`）→ memory/skills 自动关闭；老 child +
  新 host（多一个字段）→ 忽略。
- 测试 pin（assistant.test.cjs、ext-nav B7）同 commit 更新，不删断言。

### 15.1 history 不再丢（host：assistant-host.cjs，app 壳）

1. `_persistEvent`（:206-220）：超 1.5MB 时**归档轮换**而非丢弃——把当前文件
   rename 成 `threads/archive/<id>.<ts>.jsonl`，起新文件；归档上限 20 代
   （删最老），总量有界（~30MB）。
2. `_loadEvents`（:222-239）：先读当前文件；`openThread` 播种时若消息不足
   （<100 条 user/assistant 或 <128KB 文本），按新→旧读归档补齐。
3. `poll()`（:607-619）加 `before` 游标：返回 `seq < before` 的最早 200 条
   （需要时读归档），view 加"加载更早"按钮。
- view（extensions/packages/assistant/index.html，shadowable）加分页按钮 +
  拉取逻辑。

### 15.2 memory（child：entry.js，shadowable；host 只加一个字段）

1. host 在 `configure` 消息里加 `dataDir`（= `<userData>/ext-data/assistant/`）。
   assistant-host.cjs 一行级改动。
2. child 收到 `dataDir` 后：
   - 启动读 `<dataDir>/memory.md`（没有则空），以 `## Durable memory` 段拼进
     system prompt；
   - 新增 `memory` 工具 `{op:"append"|"replace", text}`：原子写 memory.md
     （上限 32KB，超出拒绝并要求 LLM 先 replace 精简）。LLM 自主决定记什么。
3. **上下文防爆守卫**（顺带给 15.1 兜底，历史变长后必需）：`onSend` 前估算
   `agent.state.messages` 总量，超阈值（如 >400KB JSON 或 >200 条）从最老
   user/assistant 对开始丢，保 system + 最近 ≥20 条。pi core 无 compaction，
   不防就会 API 400。

### 15.3 skills（child：entry.js，shadowable；内容 = 数据文件，按设计文档落地）

按 assistant-design.md:168-170 的既定设计：skills 是数据文件，不烘进 bundle。

1. 目录：`<dataDir>/skills/<name>/SKILL.md`（用户可编辑）。首启时 child 把
   bundle 内置的默认 skills 拷进去（已存在则不动，用户编辑优先）。
2. configure 时扫描 skills 目录，解析每个 SKILL.md 的 name + 首行描述，拼成
   紧凑索引进 system prompt（只索引，不全文——省 token）。
3. 新增 `skill` 工具 `{name}` → 返回该 SKILL.md 全文（按需读取）。
4. 默认 skills 内容来源：repo 已有 `.agents/skills/`（camc-diagnose、
   managing-camc、camc-messaging…）改编 + 把 entry.js:78-104 的硬编码端点
   目录抽成 `hub-api/SKILL.md`（生成或手维护，后续可从 hub 路由表自动生成）。
5. 备选增强（可砍）：host 侧把 Skills mode（skillm）装的 workspace skills 也
   暴露给 assistant——v1 不做，先用独立目录。

### 15.4 实施顺序与验证

1. entry.js（15.2.2、15.2.3、15.3.1-3）+ 默认 skills 数据 → 本地构建 bundle
   → **同名用户包 shadow 安装即可测**（无需 MSI）。
2. assistant-host.cjs（15.1、15.2.1）→ 需要壳构建；与下次正当 MSI 合并发布。
   在此之前 15.1 的归档逻辑可以先用 feature-detection：`dataDir` 存在才启用。
3. view 分页（15.1.3）→ 随用户包一起。
4. 测试：assistant.test.cjs 加 pin——归档轮换（造 1.5MB+ 事件）、`load`
   播种条数、memory 工具写入、skill 工具读取、上下文守卫裁剪；
   mock OpenAI 端点跑全绿；ext-nav B7 同步更新。
5. 手工验证：app 里聊 → 重启 app → 历史完整；塞爆 1.5MB → 旧消息仍可
   "加载更早"；放一个自定义 SKILL.md → assistant 能说出并按它行事。

工作量估计（诚实版）：entry.js +200 行级，host +100 行级，view +50 行级，
测试 +100 行级；skills 数据文件改编半天。无新依赖（child 只用 node stdlib +
已有 typebox）。

### 15.5 上游继承方案（pi SDK，2026-08-18 调研，推荐）

**核心发现：compaction / skills / session 持久化都在 pi-coding-agent 里（不在
pi-agent-core），且 pi 有头等 SDK 可编程嵌入；memory 没有现成功能，但注入机制
可继承。** pi 已改 scope：`@mariozechner/*` → `@earendil-works/*`，latest
0.84.2（我们 vendored 的是旧 scope 0.73.1）。来源：
[npm README](https://www.npmjs.com/package/@mariozechner/pi-coding-agent)、
[docs/sdk.md](https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/coding-agent/docs/sdk.md)、
[docs/compaction.md](https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/coding-agent/docs/compaction.md)、
[docs/skills.md](https://raw.githubusercontent.com/badlogic/pi-mono/main/packages/coding-agent/docs/skills.md)。

逐项对照（ vs §15.1-15.4 的自研方案）：

| 需求 | pi 现成能力 | 继承方式 | 自研量 |
|---|---|---|---|
| compaction | pi-coding-agent 内建：auto（`contextTokens > window - reserveTokens(16384)`）+ manual + split-turn 处理 + 文件操作累计跟踪；**完整历史保留在 JSONL**（compaction 只改发给 LLM 的 context） | `AgentSession` 默认开启；`SettingsManager.inMemory({compaction:{...}})` 调参；`compaction_start/end` 事件给 view 显示 | **零**（只接事件显示） |
| session history | `SessionManager`：JSONL 树结构（id/parentId、分支、完整保留）；`create/open/continueRecent/list` | 一线程 = 一 session 文件，存 `ext-data/assistant/sessions/`；`openThread` → `SessionManager.open(file)` → **完整 reload（含 compaction 前的历史）** | **零**（host 的 events.jsonl 退化为纯 view 显示日志，§15.1 归档方案废弃） |
| skills | Agent Skills 标准实现：progressive disclosure（描述索引进 prompt，`read` 按需读全文）；发现目录含 **`~/.agents/skills/` 和项目 `.agents/skills/`——本 repo 现有的 `.agents/skills/`（camc-diagnose、managing-camc 等）直接可用**；SDK 有 `skillsOverride` 编程注入 | `DefaultResourceLoader`（`agentDir` 指到我们的数据目录）或 `skillsOverride` | **零**（内容也是现成的） |
| memory | **无现成功能**；最近似的是 context files（AGENTS.md）注入 | 读：用 `agentsFilesOverride` 把 memory.md 当 context file 注入（继承）；写：仍需 ~30 行自定义 `memory` 工具（`defineTool`） | ~10%（写回胶水） |
| 安全/合规 | `noTools:"all"` 禁掉内建 read/bash/edit/write；`customTools` 挂我们的 cam/bash；`setRuntimeApiKey` runtime-only（**token 不落盘**，正好对接 credential store）；`ModelRuntime.create()` 默认不走网络；`PI_OFFLINE=1` 关遥测/更新检查 | 直接配置 | 零 |

**推荐方案 A：entry.js 迁移到 pi-coding-agent SDK**（替代 §15.1-15.4 的自研）

1. `package.json`：deps 换成 `@earendil-works/pi-coding-agent`（^0.84.2，会带
   pi-ai/pi-agent-core 传递依赖）。entry.js imports 改 scope。
2. `configure` 时：child 在 `<dataDir>/pi/` 下写 `models.json`（custom provider，
   openai-completions 兼容，等价现在的 `_makeModel`）→ `ModelRuntime.create({
   authPath/modelsPath 指向该目录 })` → `setRuntimeApiKey`（host 给的 key，
   runtime-only）→ `createAgentSession({ model, noTools:"all",
   customTools:[camTool, bashTool?], sessionManager: SessionManager.create(
   <dataDir>/sessions), settingsManager: inMemory(compaction 默认开),
   resourceLoader: DefaultResourceLoader({ agentDir: <dataDir>/pi,
   systemPromptOverride }) })`。
3. stdio 协议不变（我们 own 这个 contract）：SDK 事件映射回现有
   `turn/delta/thinking/tool/done/status`——事件名几乎一一对应（SDK 还多了
   `compaction_start/end`，view 加一行显示即可）。`followUp` → `session.
   steer/followUp`；`reset` → `runtime.newSession()`；`load`（线程切换）→
   `SessionManager.open(对应文件)`——**完整历史 reload，用户要的正是这个**。
4. host 改动仍然只有一个字段：`configure` 加 `dataDir`（同 §15.2.1）。
   线程 id ↔ session 文件的映射表存 `<dataDir>/threads.json`（child 自管）。
5. **bundle 风险（主要工程量在这）**：pi-coding-agent 0.84.2 的 deps 含
   `pi-tui`、`@silvia-odwyer/photon-node`（**native 图像库**）、pi-client、
   pi-protocol 等 ~20 个。esbuild 从 SDK 入口打包需验证 tree-shaking；很可能要
   deep import 或 `--external` 排掉 tui/photon（native 模块对
   ELECTRON_RUN_AS_NODE 和 MAS 都是麻烦，必须排掉）。先做一个最小 spike：
   20 行脚本 import createAgentSession → esbuild → 看产物体积和缺件。
6. 发货不变：bundle → 同名用户包 shadow，免 MSI；host 一字段攒进下次 MSI。

方案 B（退路）：留在 0.73 core，按 §15.1-15.4 全自研——只在方案 A 的 bundle
spike 失败（体积爆炸/native 排不掉）时启用。

方案 C（不推荐）：直接 spawn pi CLI 的 RPC 模式（`pi --mode rpc`）——继承最
彻底但推翻现有 stdio/view 集成，还要解决 pi CLI 的分发，手术太大。

**迁移风险**：0.73→0.84 跨 11 个 minor + scope 改名，entry.js 用到的 API
（Agent 事件、followUp、state.messages setter）在 SDK 文档里都还在，形状兼容；
工作量估计：entry.js 改写 ~250 行 + bundle spike + 测试更新，view 基本不动。

### 15.6 可更新性设计（2026-08-18，用户约束："将来要是可更新的"）

**结论：直接继承 pi-coding-agent 层是对的**——compaction/skills/sessions 都在
这层，自研等于重造它。继承形态选 **SDK 依赖嵌入**（不是 fork、不是 spawn
CLI）：`@earendil-works/pi-coding-agent` 作为普通 npm dep，esbuild 打进
cam-assist.js，SDK 面就是我们和上游之间的稳定契约。

**三层更新通道**（按更新频率从高到低，每层都有既定机制）：

1. **内容层（skills / memory / system prompt 数据文件）——零构建更新**。
   全部落在 `<userData>/ext-data/assistant/` 下的数据文件，用户可直接编辑；
   pi 的 ResourceLoader 每次 configure 重扫。绝大多数"更新 assistant 能力"
   发生在这一层，什么也不用发。
2. **bundle 层（pi + entry.js = cam-assist.js）——bundle shadowing，免 MSI**。
   升级 pi 版本 = 改 package.json → rebuild bundle → 以同名用户包发货
   （0.2.26 起 shadowing 覆盖 bundle 本体；MAS 禁用）。入口：Extensions →
   Install，或后续加"从 hub 拉取"通道。
3. **host 层（assistant-host.cjs 桥）——仅 MSI**。桥协议（stdio JSON-lines）
   是我们自己的稳定 contract（entry.js:10-11），两层可独立演进：新 bundle
   配老 host 自动降级（无 dataDir 则关掉相应功能），老 bundle 配新 host
   忽略多余字段。host 改动攒到正当 MSI 一起发。

**pi 版本策略**（让上游升级便宜且安全）：

- pin 精确版本（不用 `^`），升级是有意的、可评审的 diff；11 个 minor 的
  跨度事故只允许发生一次（0.73→0.84 这次迁移），之后小步快跑。
- **升级 gate = 现有测试**：assistant.test.cjs（mock OpenAI 端点全链）+
  ext-nav B7 + 一个最小"pi compat"断言集（session 建/续、compaction 触发、
  skills 扫描）——绿了才发。
- bundle 纪律：esbuild 排除 pi-tui / photon-node（native）/ pi-client 等
  TUI 与网络附加层（spike 验证；deep import 或 `--external` 方案见 §15.5.5）；
  产物体积进测试断言（防依赖膨胀静默回归）。
- 遥测：embedded 场景必须 `PI_OFFLINE=1`（或 settings 等价物）——写进
  entry.js 启动路径，并进测试 pin。

**可选逃生舱（~10 行，非 v1）**：child 启动时先探测
`CAM_ASSIST_PI_HOME`（或 `<dataDir>/pi-override/`）里的外部 pi 安装，命中则
优先于 bundled 加载——给高级用户/开发者"不等我们发 bundle 先升 pi"的口子。
代价要写明：破坏自包含、MAS 不可用、native 风险自负；默认关闭。

**MAS 边界**：shadowing 和 external-pi 都仅非 MAS（Apple 2.5.2）；MAS 渠道
的 assistant 只能随 pkg 更新——可接受，MAS 线本来就 parked。

### 15.7 决策规则与 UI 约束（2026-08-18，用户确认）

- **UI 约束**：现有 assistant view（iframe + 事件渲染）用户已接受，**不动**。
  不采用 pi 的 TUI——SDK 仅 headless 使用；stdio 协议与事件形状在两个方案
  下都保持不变，view 侧最大改动 = compaction 事件加一行显示。
- **继承 vs 自研的判定 gate = esbuild spike**（半天内出结果）：
  1. bundle 成功，native 依赖（photon-node 等）被排除干净；
  2. 体积 ≤ ~8 MB minified（当前基线 **2.58 MB**，2026-08-17 的
     dist/cam-assist.js；超出要说明理由）；
  3. `ELECTRON_RUN_AS_NODE` 下 smoke 通过（configure + 一次 mock prompt）；
  4. assistant.test.cjs 全绿。
- **判定结果**：
  - 全过 → **方案 A**：pi-coding-agent SDK 全继承（§15.5）。
  - 体积/TUI 拖不动 → **中间方案 A′**：deep import 只取上游的
    `session-manager` + `compaction` + `skills` 三块源码模块（仍是继承，不
    拖 TUI 面）；代价：deep import 不属于 SDK 公开面，跨版本升级时要
    review 上游 diff——写进升级 checklist。
  - A′ 也不可行 → **方案 B**：§15.1-15.4 全自研（留 0.73 core）。
- 无论哪个方案：UI 不变、stdio 契约不变、三层更新通道（§15.6）不变。

### 15.8 UI 建议清单（2026-08-18，用户："喜欢现 UI 简洁够用，但接受建议"）

读完 `extensions/packages/assistant/index.html`（569 行）后的建议，按波次排；
原则：不破坏简洁、每条独立可砍、全部走用户包 shadow 发货（view 在扩展包内，
**不需要 MSI**）。

**Wave 0 — 纯 view 改动，今天就能做（无 host/entry 变更）**

1. **Stop 按钮（最大功能缺口）**：host/preload/entry 全都有 stop 链
   （`assistant.stop` → `agent.abort()`），view 从未接——streaming 时 Send
   翻转为 Stop。一次工具循环跑偏时这是唯一的刹车。~15 行。
2. **Markdown 渲染**：assistant 气泡现在是 `textContent` + pre-wrap，LLM 的
   代码块/表格/列表全是原始字符。把 vendored marked + DOMPurify 拷进包内
   （CSP `script-src 'self'` 允许同包脚本），仅 assistant `.body` 走
   marked→DOMPurify→innerHTML，代码块加复制按钮。~60 行 + 两个 vendor 文件。
3. **thinking 折叠**：长 reasoning 现在全程平铺刷屏。默认收起成
   `<details>"thinking…"</details>`，流式时展开、done 后收起。~20 行。
4. **智能跟随滚动**：现在每个事件都 `scrollTop = scrollHeight`，用户上翻
   阅读时被拽回底。改为"距底 <40px 才跟随"，否则浮一个 "↓" 回底 pill。
   ~15 行。
5. **queued 渲染成待答气泡**：现在 queued 是一条 sys 灰字，自己刚发的消息
   看着像已被答。把 queued 的用户消息渲染成普通用户气泡 + 角标"排队中"。
   ~10 行。
6. **composer 自动增高**：固定 40px，长 prompt 憋屈。auto-grow 到 ~120px。
   ~10 行。
7. **跨天时间戳**：消息只有 HH:MM，多日线程里分不清。非今天显示 MM-DD。
   ~5 行。

**Wave 1 — 随 history/memory/skills 工程一起做（§15.1-15.5）**

8. "加载更早"分页按钮（依赖 §15.1 的 poll `before`）。
9. compaction 行：SDK 时代 `compaction_start/end` 渲染成一条可折叠的
   "上下文已压缩（摘要）"系统行。
10. tool 行可展开错误摘要：entry 的 `tool` end 事件带 ~200 字符结果摘要，
    失败的调用能就地看到为什么失败（hub_call 状态/bash exit code）。
11. skills 指示：chat-head 显示已加载 skills 数（hover 列名字）——skills
    静默生效时用户需要最小可见性。

**Wave 2 — SDK 迁移后才有数据（§15.5）**

12. context 用量条（`tokensBefore`/contextWindow，仿 pi footer）——超 80%
    变黄，提示可 /new 或等 auto-compaction。
13. token/cost 累计（SDK usage 事件），放 status 行hover详情。
14. 富 tool 展示：SDK 的 `tool_execution_update` 流式输出（如需）。

**明确不做（保持简洁）**：不做消息内 reaction/编辑重发/多模型并排/主题
切换（跟随 app 外观即可）；不改三 tab 结构；transcript 保持单栏。

### 15.9 架构形态：pi 后端 + 我们的前端（adapter 模式，2026-08-18）

用户问法："种它的后端、用我们的前端" vs "pi-agent-core + 自加
memory/compact/skills + 我们的 UI"。答案：**前者对，后者是把最值钱的部分
扔掉自己重写**。

关键认知：compaction/skills/sessions 的价值不在三个模块本身，而在
**AgentSession 把它们正确接进 agent loop 的那层胶水**——compaction 的切点
规则（turn 边界、split turn 双摘要）、`firstKeptEntryId` 之后的 context 重
建、overflow 后的 recover+retry、迭代摘要时把上次摘要传入。这些边角案例是
bug 重灾区（见 docs/compaction.md 的 split-turn 节），自研 = 永久拥有它们。

**目标形态（方案 A 的准确图）**：

```
[我们的 iframe view] ─extBridge─ [assistant-host.cjs] ─stdio JSONL─ [cam-assist.js]
UI 不变                              桥不变(+dataDir 字段)            ├─ entry.js = 适配层(~400行)
                                                                      │   stdio 契约不变 / cam+bash
                                                                      │   工具 / SDK 事件→我们的事件
                                                                      └─ pi-coding-agent SDK(后端)
                                                                          AgentSession(loop+compaction)
                                                                          SessionManager(JSONL 完整历史)
                                                                          DefaultResourceLoader(skills)
                                                                          ModelRuntime(auth, runtime key)
```

即：我们的"后端"只剩一层薄适配器（stdio 契约 + 两个自定义工具 + 事件映
射），pi 的后端干其余所有事。view 无感、host 无感。

**刻度尺上的三个点**：

- **A（全继承 SDK）**：胶水全用上游的。升级 = bump 版本。推荐。
- **A′（core + deep-import 三模块）**：省 TUI/bundle 重量，但
  prepareCompaction/generateSummary/session-manager 是为 coding-agent 层写
  的，context 重建、事件、settings 的接线仍要自己写——省的是体积，不是最
  难的胶水；且 deep import 不在 SDK 公开面，升级要 review diff。仅 spike
  体积失败时选。
- **B（core + 全自研三件套）**：compaction 自研 = §15.2.3 防爆守卫的十倍复
  杂度（切点/split-turn/重建/retry）；skills 自研可行（~150 行，Agent
  Skills spec 很简单）；history 自研 = §15.1 归档。全部永久自维护，功能
  冻结在实现日。仅 A′ 也失败时选。

**澄清（用户追问"B 不是能复用 90% 吗"）**：能——但"core + 搬上游模块"那
个形态就是 A′，不是 B；B 特指连模块都搬不动时的兜底（上游 license/结构
剧变之类，概率低）。真正的分界不是复用百分之几的**代码**，而是复用
**函数**还是复用 **AgentSession 那层接好的线**：可搬的 70-90% 是容易部
分（JSONL 格式、frontmatter 解析、摘要 prompt），剩下 10-30% 的接线
（切点/重建/retry/事件/settings）才是正确性所在，而 `createAgentSession`
白送这部分。再算依赖账：一旦 import 了 session-manager/compaction，传递
依赖大头（pi-ai、proper-lockfile、yaml…）就已付掉，A′ 相比 A 只省
pi-tui/pi-client/pi-protocol/photon-node/grok-mermaid/highlight.js 那一
块——所以 A′ 的唯一正当理由 = 那一块把 bundle 搞炸，这正是 spike 要量
的。实操上刻度尺就是两点：**整层（A）或搬模块（A′）**，全自研（B）仅
作名义兜底。

另注：pi 还有 `pi --mode rpc` 子进程模式（方案 C）——后端继承最彻底，但
  要解决 pi CLI 分发（用户机器上没有 pi）+ 推翻现有 stdio/view 集成，不
 推荐。

### 15.10 FAQ：SDK 接现有 Q&A UI？直接用 pi TUI？（2026-08-18）

**Q1：pi-coding-agent 能接到我们现在的 Q&A UI 上吗？—— 能，且就是方案 A
的本意。** SDK 纯 headless，entry.js 适配层把 SDK 事件映射回现有 stdio 事
件，view 零改动（加 compaction 一行显示除外）：

| 我们的 stdio 事件（view 已认识） | SDK 来源（docs/sdk.md） |
|---|---|
| `turn` | `turn_start` |
| `thinking` / `delta`（累计文本） | `message_update` 的 `thinking_delta`/`text_delta`（适配层做累计） |
| `tool` start/end | `tool_execution_start` / `tool_execution_end` |
| `done`（含 stopReason） | `agent_end`（`event.messages` 取末条） |
| `queued` | `queue_update`（SDK 原生 steer/followUp，正好换掉我们手写的排队） |
| `status` running/idle | `agent_start` / `agent_end` |
| （新增）compaction 系统行 | `compaction_start` / `compaction_end` |
| `reset` / 新线程 | `runtime.newSession()` |
| `load`（切线程） | `runtime.switchSession(file)` / `SessionManager.open()` |
| configure（url/model/token） | `models.json` custom provider + `ModelRuntime.setRuntimeApiKey`（runtime-only，不落盘） |

**Q2：直接用 pi TUI 行不行？—— 理论上能嵌，架构上是错的方向，不推荐。**
形态只能是：bundle pi CLI → 本地 PTY 子进程 → xterm 窗格渲染 TUI。问题：

1. **token 规则破**：pi CLI 用自己的 `~/.pi/agent/auth.json`，我们"LLM
   token 只在 credential store"的硬规则没了；`setRuntimeApiKey` 是 SDK 才
   有的口子，CLI 不经过它。
2. **MAS 设计破**：MAS-SPIKE 定的"no local process spawn by design"被直接
   违反（现在唯一的本地子进程是 ELECTRON_RUN_AS_NODE 的自有 bundle，
   嵌 TUI 等于引入第二个本地进程面 + node-pty 依赖）。
3. **UI 模型破**：TUI 是终端 UX——没有我们的 Threads tab、Settings tab、
   app chrome，用户喜欢的是现在简洁的 Q&A 界面；配置也会裂成
   pi settings.json 与我们的 Settings 两个世界。
4. 终端子系统目前是纯 SSH 面向（CamBridge.term → ssh2 池），加本地 PTY
   spawn = 新 bridge 能力 = 动壳 + MAS 复审面。

结论：**SDK headless + 我们的 view 是唯一既继承功能又守住产品规则的形
态。** pi TUI 仅可留作将来的 power-user 念想（"在终端里打开同一份
session 文件"——session JSONL 是互通的），不进 v1。

### 15.11 Spike 结果与版本决策（2026-08-18，已执行）

**判定：gate 通过 → 方案 A（SDK 全继承），但用旧 scope 的 0.73.1。**

- **版本决策：`@mariozechner/pi-coding-agent@0.73.1`**（不用新 scope 的
  `@earendil-works/*@0.84.2`）。原因：0.84 要求 Node ≥22.19（实测 undici 8
  在 Node 20 下 `util.markAsUncloneable` 崩），而我们的运行时 = Electron 31
  内置 Node 20。0.73.1 engines `>=20.6`，且**与现有 vendored pi-ai/core 同
  版本线——Agent API 零漂移**，只是在其上加 SDK 层。SDK 面已核实：
  createAgentSession(FromServices/Runtime)、SessionManager、SettingsManager、
  ModelRegistry、AuthStorage、DefaultResourceLoader、defineTool 全在，
  compaction 在 agent-session 内（12 处引用）。
- **将来升级路径**：Electron 升到 Node ≥22 后迁新 scope（SDK 概念 1:1，
  0.84 的 `ModelRuntime` ≈ 0.73 的 `ModelRegistry`+`AuthStorage`；届时照
  0.84 docs/sdk.md 过一遍 rename）。
- **Spike 数据**（build.mjs 同参打包一个引全 SDK 的入口）：
  体积 **6.66 MB minified**（基线 2.58 MB，gate ≤8 MB ✅）；Node 20.18.1
  加载通过 ✅；`.node` native require 0 处 ✅；无 esbuild 警告 ✅。
- **两个打包坑及解法**（已在 `build.mjs` 固化）：
  1. pi 是纯 ESM，`import.meta.url` 在 CJS 输出为 undefined → `--define:
     import.meta.url='"file:///cam-assist.js"'` 钉成无害常量；
  2. `dist/config.js:332` 顶层无保护地 `readFileSync(getPackageJsonPath())`
     （读它自己的 package.json 取 APP_NAME/VERSION/CONFIG_DIR_NAME）→
     build.mjs 的 esbuild 插件把该行改成 try/catch 兜底
     `{name:"cam-assist",version:"0.0.0"}`；我们全部显式传 agentDir，兜底
     常量到不了我们的代码路径。插件对目标行做存在性断言——上游改动时
     build 直接报错而不是静默失效。
- 构建脚本从 CLI 一行式换成 `build.mjs`（esbuild JS API + 插件）：
  `node build.mjs [entry] [outfile]`。

### 15.12 实施记录 v0.5.0（2026-08-18，SDK 迁移已落地）

按方案 A 完成，**全部测试绿**（assistant 67、ext-nav 60、extensions 27、
cam-pi 8、hub 123、term 6+6、start 6、lint:electron）。改动文件：

- `extensions/vendor/cam-assist/entry.js`（重写为 SDK 适配层，~600 行）
- `extensions/vendor/cam-assist/build.mjs`（新，esbuild 插件构建，§15.11）
- `extensions/vendor/cam-assist/package.json`+lockfile（deps →
  `@mariozechner/pi-coding-agent@0.73.1` pin 精确版）
- `extensions/vendor/cam-assist/dist/cam-assist.js`（重建，6.68 MB）
- `extensions/packages/assistant/manifest.yaml`（version → 0.5.0）
- `apps/cam-desktop/electron/assistant-host.cjs`（一行：`load` 消息加
  `threadId` 字段——老 bundle 忽略之，新 bundle 用它做完整历史重开）
- `apps/cam-desktop/test/assistant.test.cjs`（pin v0.4.0→v0.5.0，同 commit）

**继承到手的功能**：SessionManager 每线程一份 JSONL 完整历史
（`~/.cam/assistant/sessions/`）、auto-compaction（默认开，16384/20000）、
Agent Skills 加载（`~/.cam/assistant/skills/` + `~/.agents/skills` 发现，
progressive disclosure）、memory 工具 + memory.md 注入（agentsFilesOverride
懒读）。stdio 协议零变化（view 无感），新增 compaction 事件（view 暂忽略，
Wave 1 渲染）。

**两个关键实现点**（debug 实录）：
1. `noTools:"all"` 是陷阱——0.73.1 里它让 `allowedToolNames` 变空集，custom
   tools 也被过滤光（sdk.js:152 + agent-session.js `_refreshToolRegistry`）。
   正解：`tools:[名字…]` 显式白名单。
2. skills 在系统提示里的广告门 = 必须有叫 `read` 的工具；内建 read 是全
   FS 读（不可接受）。解：自定义**作用域仅限 skills 目录**的 `read` 工具——
   注册表里 custom 覆盖同名内建（Map.set 顺序），既过门又不开放 FS。

**数据根**：`~/.cam/assistant/`（sessions/、skills/、memory.md、
threads-map.json）——child 自管，host 零新字段（threadId 那行只是提前布
点）。老 host 配新 bundle：load 无 threadId → 回退播种模式（现状行为）；
新 host（下次 MSI 起）配新 bundle → `SessionManager.open()` 完整重开。

**留给下一刀**：view 渲染 compaction 行 + Wave 0 UI 清单（§15.8，Stop 按
钮/markdown/折叠 thinking/智能滚动）；host log 的"加载更早"（§15.1 已废，
改为从 pi session 文件分页）；repo `.agents/skills` 的种子化（现在只种子了
hub-api skill）；`test:smoke` 需在 Windows 侧跑（WSL 无 display）。
**MSI 需求**：仅当要让 threadId 字段生效——可攒到下次正当 MSI；bundle 更
新走 shadowing 免 MSI（桌面安装目录直接替换 vendor dist 也有效）。

### 15.13 tar.gz 用户包：4MB 单文件上限与自解压 wrapper（2026-08-19）

§15.12 说"bundle 更新走 shadowing 免 MSI"——实操打包时发现此路**差点断
掉**：`extensions/host/registry.cjs` 的安装校验有硬上限
（`MAX_FILE_BYTES` 单文件 4MB、`MAX_TOTAL_BYTES` 整包 8MB），而 SDK 迁
移后 bundle 已 minify 仍有 **6.84MB**，直接超单文件 cap。v0.4.x 的
2.58MB 一直刚好低于 cap，所以以前从没撞过。

**解法：自解压 wrapper 作为唯一发布产物**。`build.mjs` 改为 esbuild
`write:false` 内存打包 → `gzip -9` + base64 → 生成 ~10 行的 CJS
wrapper：启动时 inflate，用 `new Function('exports','require','module',
'__filename','__dirname', src)` 按 Node 模块包装器同名自由变量求值
（`run.call(exports, …)` 保持顶层 `this===module.exports` 语义；追加
`//# sourceURL=cam-assist-bundle.js` 保住堆栈）。实测 6.84MB →
**2.18MB**，留足未来增长余量；裸包与 wrapper 行为逐字节验证一致（同一
stdin 序列输出相同）。调试可用 `CAM_ASSIST_RAW_OUT=<path> node build.mjs`
同时产出未包装 bundle 做对照。

**打包与验证**（artifact：`dist/cam-assistant-0.5.0.tar.gz`，1.66MB）：

```
stage = { manifest.yaml, index.html, cam-pi.js, main.py,   # packages/assistant
          cam-assist.js }                                  # vendor dist（wrapper 版）
tar --format=ustar -czf dist/cam-assistant-0.5.0.tar.gz -C <stage> assistant
```

- 布局：单一顶层目录 `assistant/`（`installExtension` 自动剥一层）；必须
  `--format=ustar`——registry 的 `_untar` 是极简读取器，GNU 长名扩展条目
  （type `L`）会被跳过导致文件名截断。
- 安装入口：UI「Extensions → Install package…」（`ext-install-pkg-btn`，
  与 Install from folder 并列）；API 同一端点
  `POST /api/extensions/install {path}`。
- 彩排验证（未碰真机 UI）：直接 `require('extensions/host/registry.cjs')`
  对 tar.gz 跑 `installExtension` 到临时 extRoot →
  `ok, assistant@0.5.0, entries {view:index.html, tool:main.py}`；再从"已
  安装"副本 boot smoke → `ready v0.5.0 / pong`。即应用安装路径全链路通过。
- 安装后 `<userData>/extensions/assistant/cam-assist.js` 经
  `_assistPath()` shadow 内置 bundle（assistant-host.cjs:242），重启生效；
  MAS 构建永不加载用户副本。

**待办**：Windows 真机 Install package 走一遍（UI 层未验证）；若将来
bundle 连 wrapper 也逼近 4MB，应在下次 MSI 把 `MAX_FILE_BYTES` 提到 8MB。
