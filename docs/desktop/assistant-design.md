# CAM Desktop Assistant — 设计方案（暂缓实施）

状态：**DESIGN ONLY，未实施**。主线是 extensions；本文档沉淀 Assistant 的
完整设计，待主线告一段落后按此实现。

## 目标

一个 local、方便、不复杂的智能助手：全局视野（所有节点的 agent 状态）、
对话式诊断/问答、受控执行 app 级操作。是 cam desktop 的一部分，不是
独立产品。

## UI

- 导航栏一等入口 **Assistant** 页（与 Nodes/Settings 同级）
- 三态：
  1. **未配置** → 设置卡片（LLM URL / key / model / 运行位置）+ 本机节点引导
  2. **已配置、运行中** → 钉死的终端（复用现有 xterm attach 组件），
     无需从 agents 列表里找
  3. **停止/丢失** → Start assistant 按钮
- 快捷动作按钮（fleet report / diagnose）走一次性 ext.call，无需开会话
- **单持久 session**：assistant 常驻，跨天记得舰队上下文；"重置对话"
  = 重启 agent。不做多会话管理

## 运行时路线（按构建分轨）

| 构建 | 路线 |
|---|---|
| **DMG / MSI**（非沙盒） | **本地 PTY**：main 进程 node-pty 起 pi standalone
二进制，Assistant 页终端直连。hub/SSH datapath 零改动；专用 IPC，
不开通用本地 exec API |
| **MAS**（沙盒） | **SSH 节点路线**：pi 作为 camc agent 跑在用户选的
节点（本机 Mac = 开 Remote Login 后的 local 节点；Windows = wsl-local）。 |
| 任何路线 | pi 二进制 = Bun 编译的 standalone 单文件，目标机**零依赖**
（不需要 node/python 的新版本） |

`process.mas` 运行时门控两条路线；MAS 构建摘掉本地终端代码，审核面不变。

## pi 的分发（standalone 二进制）

优先级：

1. **远端/本机 bootstrap 下载**（默认）：extension 的 python 壳在目标机从
   GitHub releases 下载对应平台二进制。app 零膨胀
2. **用户指定 URL/镜像**（Settings 覆盖项）：内网场景
3. **打进 app 包**（暂缓）：+50MB/平台，等真实离线需求再做

## 能力模型（助手提议 → app 执行）

- **只读随便查**：agents/capture/sync 状态/Diagnostics 日志
- **写操作需确认**：add node、sync host、start/stop agent、发 prompt——
  助手发结构化动作请求，app 弹确认后执行并回喂结果
- **gen ssh key**：app 侧纯 JS 生成（Node crypto），私钥进 Keychain/
  用户选路径，不走 shell
- **桥接新增**（extensions 主线顺手做）：bridge `agents.start`（带确认）

## 边界（明确不做）

- **hub 永不本地执行**（MAS + 产品纪律）；唯一的本地进程是 DMG/MSI 版
  Assistant 终端里那个 pi 二进制（用户显式启动的助手本身）
- 助手**永远不能自己初始化本机访问**（鸡生蛋）：macOS Remote Login /
  Windows WSL 组件的第一次开启是用户的一次手动动作，app 做引导卡片
- 不接管已在运行的自由进程（Windows 控制台不可附着；psmux spike 已验证
  17/17 兼容，Windows 原生节点待 camc Windows shim——cam-dev 的活）
- 不做在线扩展商店（MAS：无下载执行）

## 依赖与前置

- extensions 主线：tool-proxy 目录部署、bridge `agents.start`
- pi standalone 二进制的 release 形态待验证（repo 文档确认了源码构建，
  预编译产物待查 releases 页；若无预编译则 bootstrap 退化为 npm 路径，
  host 需 node）
- 全局摘要喂送：hub 周期打包 agent 状态 → camc msg 推给 assistant agent

## 实施顺序（解冻后）

1. pi-assistant 扩展包（bootstrap 壳 + 配置写入）
2. Assistant 页三态 + Settings 存储
3. DMG/MSI 本地 PTY（node-pty 集成 + 专用 IPC）
4. MAS 引导卡片（Remote Login 深链 + 测试连接）
5. 全局摘要喂送
6. MSI/DMG 实测
