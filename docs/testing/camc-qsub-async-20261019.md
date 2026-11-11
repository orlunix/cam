# qsub 异步交付与排队计时修复（2026-10-19）

run/New/reboot 把启动请求交给 tmux 后即可返回；qsub 的资源等待由 pane 中的 launch worker 承担。调用方可以报告 submitted/queuing，agent 保持 alive/running/initializing。只有工具真正启动并通过 ready 检查后才标 started。

## 修改

- 自定义 launcher 前缀复用已有目标端 resolve/exec 脚本，在解析、验证工具后、exec 前写入本次启动独有的 `.launcher` receipt。
- auto 与自定义 launcher 的 monitor 均等待该 receipt；排队不会消耗 adapter 的 20 秒 boot timeout。
- foreground launch worker 同样等 receipt 才开始工具启动 deadline；600 秒上限不再覆盖排队阶段。
- 等待时仍检查 agent 生命周期和启动代次，支持 stop/kill/rm；每 2 秒检查一次，避免排队期间每 0.2 秒读取 registry。
- 未知工具的 prompt 保持由 launch worker 单独投递；短命令在两次排队检查之间执行完毕时，以目标端 receipt 确认启动，不再向退出后的 shell 粘贴 prompt。
- 无 launcher 的本地启动、native autorun 参数以及真实工具启动后未 ready 的超时仍保留。

## 验证

聚焦 launcher/lifecycle/monitor/arbitrary-tool 回归：239 passed。覆盖排队时间长于启动 deadline、dispatch 后启动计时、取消排队、快速退出的自定义工具、prompt 不重复与启动参数不变。

Python 全套回归：1722 passed、1 skipped，193.99 秒；额外的短命令退出用例包含在上述最终聚焦回归中。34 个模块通过 Python 3.6 语法检查，独立 bundle 在 Python 3.6 上执行 receipt 检查通过。构建脚本的原始 `--verify` 仅因 source `(dev)` 与发布构建时间戳不同而返回 mismatch；规范化预期时间戳后，四个 help/version 对照均一致。

真实 NVIDIA qsub 测试使用共享目录、独立 Home、实际 tmux、已注册项目 `gpu_proj-a_hw_mmplex_riscv`，通过自定义前缀提交 `autorun_sla` 作业。目标为专用测试脚本，没有启动真实模型、重装共享工具或修改原有 agent。

- agent：`fdb27bfa`；job：`1936277048`。
- run 在 **0.502 秒**返回。
- **30 秒**观察中作业仍在等待 dispatch，agent 保持 **alive/initializing**，没有被 20 秒 boot timeout 判失败。
- 测试 agent、隔离 Home、共享工作目录已删除；job 最终状态为 **EXIT**。
- 私有记录：`/tmp/cam-qsub-async-e2e-92a10eab/`。

本次验证了异步交付和真实排队，不把 PEND 当成工具 ready 或模型回答成功。此前 SDK worker 退出的根因是独立未决问题，本次没有声称修复。

后续通过生产 TUI/SDK 提交真实 Codex/Claude qsub 的约 3 分钟排队复测，以及完整功能回归结果，见 [CAM TUI E2E 复测](cam-tui-rerun-20261019.md)。

## 发布

已发布到此前配置的 7 个可连接个人 Node：camc `1.2.189`，release `cam-20261019-v1.2.189-751bcc9eb9`。复用已验证的 Rust `0.1.0` 二进制，重载 Server 的 Python SDK。7 台的 63 个 release 文件和个人入口哈希检查、Server 单实例检查均通过；发布后 7 个 Node ready、元数据 healthy，原有 64 个 agent 的 Node/ID 清单完全一致，测试定时任务为 0。未 commit/push。
