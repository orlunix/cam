# CAM Server setup / update 验证，2026-10-21

实现见 [Server 生命周期 spec](../cam-server-lifecycle-spec.md)。构建版本为
CAM Server 0.1.3、CAM TUI / Client daemon 0.1.6；未安装到用户/共享入口，
验收时没有重启生产 Server、Client daemon 或 agent；提交/推送由后续用户指令单独执行。

## 最终构建验证

- Rust workspace 单元测试：28 passed（protocol 2、Server 4、TUI 22）。
- `test_server_setup.py` 和 `test_tui_ssh_nodes.py`：13 passed。
- SDK `test_cam_server_management.py`、`test_cam_server_worker.py`：21 passed。
- 静态 x86_64 musl 四个 executable 无 INTERP / NEEDED；Server 和 Client
  binary 内嵌的 setup helper 与最终源码逐字节一致。
- bundle 4,454,400 bytes；包含 binary、SDK、adapter configs 和内置 skills。
  manifest 长度和 SHA-256 已核对；Python helper 通过 3.6 grammar 检查，
  SSH fixture 远端 PATH 使用本机 `/usr/bin/python3` 3.6.8。

专项使用真实 OpenSSH、临时 Paramiko SSH 服务、真实 CAM 协议/SDK、
独立 Client/Server fixture 和 tmux。覆盖：

1. 原生密码登录、Username 留空、加密 SSH key 登录。
2. csh/tcsh 单行 bootstrap、缺失 Server 的安装/初始化/连接。
3. 旧版升级后 Node ID、clients、peers、runtime、待投递消息和已有 agent 保留。
   agent 启动至 ready 后，升级前后 tmux pane PID 相同。
4. 已有当前/更高版本时无 bundle 仍可连接，Server epoch/config 不变。
5. 停止的当前版本服务启动，不重复复制。
6. 错误身份、CPU 架构、损坏 bundle、归档路径穿越均拒绝，旧服务不变。
7. 新服务启动失败恢复原配置/旧服务，且不发布新 binary 入口。
8. 本地 `setup` / `update` / 默认 `status` 与 SSH 共用流程；版本普通输出、
   `--version`、JSON 输出一致。
9. 共享 Home 中磁盘 binary 已新、进程仍旧的 probe 分支（注入运行版本）。

测试 fixture 的隔离入口只存在于 `test-harness` example。生产命令保留
机器/用户单实例锁；没有用环境变量跳过生产锁。所有临时服务/agent 由 fixture
回收。

```sh
CAM_ROUTER_BIN_DIR=/tmp/cam-router-target/x86_64-unknown-linux-musl/release \
CAM_ROUTER_TEST_NODE_BIN=/tmp/cam-router-target/x86_64-unknown-linux-musl/release/examples/router_test_node \
CAM_CLIENT_TEST_DAEMON_BIN=/tmp/cam-router-target/x86_64-unknown-linux-musl/release/examples/router_test_client \
python3 -m pytest -q components/cam-router/tests/test_server_setup.py \
  components/cam-router/tests/test_tui_ssh_nodes.py
```

## 大范围回归限制

扩大 Router/TUI 回归的首轮结果为 45 passed、4 failed、2 deselected。
其中升级用例新增的 pane PID 断言错误地读取空的旧 `tmux_socket` 字段，
已改读 `runtime.tmux.socket`，并包含在最终专项的 13 passed 中。

其余三项未在本次 Server 安装流程中修复：

- `test_real_terminal_control_two_tabs_and_ui_exit_leaves_daemon_alive` 和
  `test_tool_mouse_passthrough_delivers_press_and_release` 仍点击固定第 8 行。
  当前第 8 行是 Filter 区域，未点击到 agent，因此等待 agent 画面超时；
  这两项旧坐标用例需要改用当前布局的 label/row 选择。
- `test_real_clipboard_unicode_multiline_paste_and_interrupt` 发现本机
  `DISPLAY=my_unix_box:0.0` 不可连接。直接 xclip 只读诊断返回
  `Can't open display`；没有读取/打印用户剪贴板内容，不能据此验收物理剪贴板。

两项 deselected 是生产机器单实例锁用例和生产 demo，用于避免干扰正在运行的
真实服务。没有将大范围回归标记为全部通过。

## 构建产物

当前 checkout 对应的可审阅产物位于：
`/tmp/cam-node-setup-20261021/release/`，含四个 executable、
`cam-server.bundle`、`cam-server.bundle.json` 和 `SHA256SUMS`。
最终专项日志：`/tmp/cam-node-setup-20261021/focused-final.log`。
本次仅完成源码、构建、测试和 spec；生产入口仍使用此前发布版本。
