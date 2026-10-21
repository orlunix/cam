# CAM TUI E2E 复测（2026-10-19）

测试版本：camc `1.2.189`、Rust Server/Client daemon/TUI `0.1.0`，分支 `cam-tui`。
使用已部署二进制与 Python SDK；本轮没有重装共享 Claude/Codex，也没有修改 launcher 源码。

## 回归结果

| 测试 | 本轮结果 |
| --- | --- |
| 真实 Server/daemon/tmux/TUI/PTY 功能 E2E | **31 passed**，164.991 秒 |
| Python 全套 | **1723 passed、1 skipped**，257.90 秒；12 个既有 warning |
| Rust workspace，Linux x86_64 musl release | **18 passed** |
| Rustfmt、暂存 diff 格式检查 | 通过 |

31 项进程测试覆盖路由与多路径、消息去重/TTL/权限/背压、stdout 回程、跨 agent mailbox、生命周期、Nodes、终端输入顺序与 resize、多 tab/history、真实 X11/CJK 剪贴板与多行粘贴、任务调度与 loop、健康检查、断线重连、Server/daemon 重启和两个 UI 的 writer 交接。剪贴板用例实际运行，没有跳过。

测试使用隔离 Home 和专用进程。为遵守 Server 单实例规则，进程套件运行期间暂时停止本机生产 Server/daemon；finally 路径恢复当前配置的 v1.2.189 SDK 和服务。其它 Node 未为本轮测试重新部署。

## 生产 TUI/SDK 与真实 qsub

生产客户端通过本机 Server 创建两个新 agent，工作目录均位于共享的仓库 scratch 路径。Codex 由 TUI New 表单创建，Claude 由同一生产 SDK 创建后在 TUI 打开。Claude 显式使用 `DISABLE_AUTOUPDATER=1`。

自定义 launcher 前缀如下，`JOB_NAME` 为每个测试独有名称：

```sh
/home/nv/utils/nvprojectname/bin/nvprojectname run gpu_rn102_hw_mmplex_riscv \
  /home/nv/bin/qsub -q o_cpu_4G --qsla autorun_sla \
  -app r89docker -app affinity -J JOB_NAME -Is
```

| 工具 | agent | LSF job | 提交阶段 |
| --- | --- | --- | --- |
| Codex | `bace1e67` | `1936422550` | TUI New 表单至记录可见 3.441 秒，包含表单交互时间 |
| Claude | `15c07db7` | `1936432340` | SDK run 0.904 秒返回 |

两者均真实提交到 LSF，在约 3 分钟的观察窗口中等待 dispatch，agent 保持 `alive/initializing`。排队未触发原来的 20 秒 boot timeout；TUI 和 SDK 的 status/capture 仍可工作。观察窗口结束后由测试脚本主动回收，不是产品启动计时到期。

本轮通过的是异步提交和排队阶段。两个作业均未到工具 exec receipt，因此没有验证新的 started 或模型回答；不能将其记为完整 Claude/Codex 模型交互通过。此前已有模型回答的证据见 [fleet 验收](cam-tui-fleet-20261019.md)，不将旧结果冒充本轮结果。本轮也没有重新执行所有真实 auto fallback 分支。

## SDK worker 检查

本轮准确跟踪的本机 SDK worker 为 PID `3528449`，父 Server 为 `3528125`。独立检查确认进程启动时间未变，仍存活，status 与 capture stdout 均能返回。没有复现此前 worker 退出，但这不等于此前独立问题的根因已经修复。

私有 native 测试脚本初版把 Server 的所有子进程都当作 worker，新增 terminal observer 时产生一次 `SDK worker changed/exited` 假阳性。核对 `/proc` 的 module argv、PID 和启动时间后排除此误报；脚本已改为只匹配 `camc_pkg.server_worker`。这不是产品代码修复。

## 回收与记录

两个测试 job 均核实为 **EXIT**；两个测试 agent、隔离 Codex home 和共享工作目录已删除，没有本轮 agent 的本地 worker 残留。原有 **64 个 Node/agent ID 完全不变**，**7/7 Node ready**，daemon healthy，定时任务为 0。本机 SDK worker 仍为同一 PID；生产服务保留运行。

原始日志、终端内容和连接健康数据位于私有目录 `/tmp/cam-e2e-rerun-20261019/`，不纳入提交。原生模型脚本因未获得模型回答返回非零，不能与上述 31 项功能 E2E 的全通过混为一谈。
