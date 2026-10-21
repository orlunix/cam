# CAM TUI Nodes、终端 tabs 与 skill 验证

日期：2026-10-20。分支：`cam-tui`。TUI/Client daemon 0.1.5，Server 0.1.2，camc 1.2.192。

## 变更

- Nodes 普通新增只填 Host、Port、可选 Username/key/显示名称。原生 OpenSSH 处理密码、加密 key 口令和 host-key 确认，认证后自动发现/配对已运行的 Server。内部 Node ID 无需用户填写；保留 Local / advanced 与既有 grant。
- 左侧按本机最近访问排序，后台输出/状态刷新不改变顺序。记录按 Node＋agent 持久化，有界保留。
- 右上 tabs 是当前 agent 的终端。Agent 固定主 pane `0.0`；辅助 shell 在独立 tmux window 新建，可切换、关闭、刷新，主 pane 禁止关闭。按 session 验证 pane ID，绑定 launch ID；创建去重，未知结果不自动重放。
- 新增 `managing-cam-tui`，加入 camc 内置库存、skillm 本地库和 Home 全局 skills。指导 Node 连接、启动跟进到 started、终端/状态操作、history/copy 与 daemon 排障。

## 验证记录

日志目录：`/tmp/cam-tui-panes-20261020/`。

| 检查 | 结果与证据 |
| --- | --- |
| Rust workspace | 28 通过：22 TUI、4 Server、2 protocol，`rust-tests.log` |
| Python SDK、协议、内置 skill 与安装路径 | 87 通过；包含真实 tmux 辅助 shell、主 pane 保护、跨 session 拒绝、去重与 8-pane 上限 |
| 完整 UI 功能集 | 14 个场景；`features.log` 的 12 个通过，更新旧 UI 定位后的两项分别在 `sidebar-final.log`、`clipboard-final.log` 通过 |
| 原生 OpenSSH 登录 | 2 通过，`ssh.log`：真实 OpenSSH 对隔离 SSH 协议服务，分别使用加密 key＋显式用户名、密码＋留空用户名；实际 host-key/口令提示，自动配对、断开/重连、无密码持久化 |
| 路由回归 | 15 个场景通过：`router.log` 中 14 个通过；生产单实例用例在服务切换窗口另行通过，见 `production-singleton.log` |
| pane 端到端 | `panes.log`：真实 UI 创建两个 shell，执行 printf、切换/恢复历史、切换 agent、关闭辅助 pane、保护主 pane；新增不缩成 80×24，resize 后所有窗口尺寸一致；退出 UI 保留 shell |
| 剪贴板 | 私有 Xvfb＋真实 xclip：中文多行选区、系统剪贴板内容、保留选区、粘贴不补 Enter、无选区 Ctrl+C、alternate screen、resize；不读取用户桌面剪贴板 |
| 构建产物的内置 skill | `check-built-skill.py`：6 个内置 skills，新 skill 内容与源文件一致；从构建产物实际安装到临时 `.claude` / `.agents`，内容一致；Python 3.6 语法检查通过 |
| skillm | `skillm-result.json`：加入 myskills 本地库，链接 Home 的 Claude/Codex/Cursor 和 `.agents/skills`；库中 4 个无关未提交文件保持原样，未 push |
| SDK 控制管道隔离 | `test_cam_server_worker.py` 在 Python 3.6/3.12 下均通过。子进程 stdin 得到 EOF、普通 helper/子进程输出不进入控制通道，并发与后续请求正常；旧 worker 在相同测试中均复现协议污染，见 `worker-before-fix.log` |
| 隔离修复后端到端复测 | `worker-suite-results.json`：真实 panes、X11 剪贴板和跨 Client 的 native agent 生命周期全部通过 |

初次 UI 回归的两处失败来自测试仍按旧界面定位：剪贴板测试点击旧 agent tab；Help 测试把欢迎页新增的 `×` 说明误认为对话框未关闭。已更新测试定位，产品代码无额外修改。

`build_camc.py --verify` 的三个 help 对比通过；version 对比因源包 `(dev)` 与构建戳不同返回非零。这是现有比较器行为；单独验证了相同的语义版本 1.2.192、构建语法、完整 skill 清单与实际安装内容。未改动构建比较器。

## 部署与边界

部署脚本先记录原 agent ID、备份程序/Server 配置/服务记录，再安装不可变发布目录与匹配 SDK；只变更 backend 路径，保留 Node 身份、授权、路由和其它配置。切换连接服务不停止 agent。结果和逐 Node 版本见日志目录的 `deployment.json`、`result.json`。

最终发布目录为 `~/.cam/releases/cam-tui-v0.1.5-b5a1aea130a9`。七个 Node 均安装 TUI/Client daemon 0.1.5、Server 0.1.2、个人 camc 1.2.192，全部宣告 `terminal.panes`；Client daemon healthy，65/65 原有 agent 身份保留。随后持续观察 42 秒，所有有 agent 的 Node 快照均再次刷新。测试进程清理结果见 `cleanup-result.json`。

部署前本机旧 Server 的连接/心跳正常，但 worker 不再返回 agent 快照。独立运行其已安装 SDK 能正常读取 16 个本机 agent，已把这些 ID 与其余 Node 的快照合并保留，基线共 65 个。服务切换后检查全部 Node 的状态同步恢复，不把缺失快照当作 agent dead。

发现的 SDK 缺陷：管理 helper 的子进程默认继承 worker 的协议 stdin/stdout，可能读走请求或混入非协议输出。worker 现在持有私有控制描述符，默认 stdin/stdout 指向 `/dev/null`；控制描述符不传给 exec 子进程。业务操作的显式输出捕获保持不变。信号诊断与重现使用私有实例/日志，不记录请求内容；最终生产服务恢复普通启动方式。

新辅助 shell 在 agent 所属 Node 本机运行；不会自动进入 agent 使用的远端 LSF allocation。tmux 2.7 的同 session 窗口共享尺寸，主 pane writer 视图决定尺寸，输入权仍按 pane 分配。pane 管理需要 Server 0.1.2＋匹配 SDK，旧 Server 保留主终端可用。

新 SSH 连接需要已安装并运行的 Server；不负责远端安装。首次自动配对需要 Server 0.1.2；旧 Server 仅复用当前 Client 既有管理 grant，不扩大 forwarding scopes。测试 SSH 服务是隔离的本机协议 fixture，不声称覆盖所有物理机器的 sshd/PAM/MFA 配置。

鼠标选区与外层终端右键 Copy 的兼容性仍保留已知限制；本次没有宣称修复所有终端的鼠标复制。测试完成回收临时 agent、tmux、SSH fixture 与私有 Xvfb，并恢复生产 Client daemon。CAM 仓库未 commit/push。
