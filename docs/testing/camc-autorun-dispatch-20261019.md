# autorun allocation 复用格式修复（2026-10-19）

用户在 SITE-A064 启动 Claude 时，已有 `1729124669` 在 `site-a3-sim-e14-027` 运行，却走到了新的 qsub。问题不是没有调用 bjobs，而是长记录校验只接受 `Started on <host>`，遗漏了交互作业实际使用的 `Dispatched to <host>`。

## 证据

使用 launcher 原样的结构化 bjobs 格式查询，这条作业为：

```text
1729124669|RUN|autorun_sla|Y|site-a3-sim-e14-027|...
```

同一作业的 `bjobs -l -UF` 包含当前用户、`autorun_sla`、`RUN`、`Interactive pseudo-terminal shell mode`，执行事件为：

```text
Wed Sep  2 04:09:14: Dispatched to <site-a3-sim-e14-027>;
```

在 SITE-A064 对同一条真实长记录比较，v1.2.189 校验为 false，修复后为 true。实际 `battach -L` 探测具有 `LSB_JOBID=1729124669`，Claude `--version` 成功并解析到 `/home/tools_ai/anthropic-ai/claude/latest/claude`；显式禁用自动更新，没有重装工具。

修复后的 auto 在用户的实际工作目录 `/home/scratch.hren_gpu_2/rn102g_rv32_signoff` 中返回：

```text
battach: bjobs found 1 autorun candidate(s)
battach: probing job 1729124669 on site-a3-sim-e14-027
battach: selected job 1729124669 on site-a3-sim-e14-027
```

解析结果为 `/usr/local/lsf/bin/battach -L {command} 1729124669`。诊断禁止 qsub 提交和 nvproject 写入，未创建新 job。这验证分配复用与工具版本探测，不冒充新 agent 的模型回复验收。

## 修改与验证

- self 和已有 allocation 的校验共用执行主机解析，兼容 `Started on` 与 `Dispatched to`。
- 只解析执行事件行，重调度时采用最后一次事件；inline 描述不能充当执行主机证据。
- 保留 RUN、当前用户、autorun_sla、交互模式与主机校验；没有放宽为仅凭 host 或 LSB_JOBID 就启动。
- brief stdout 显示 bjobs 候选数，以及长记录校验失败的 job，避免静默跳过后直接看到 qsub。
- 新回归在修复前复现四项失败，修复后 Python 全套 **1727 passed、1 skipped**，242.18 秒；34 个模块通过 Python 3.6 语法检查，独立 bundle 的 `Dispatched to` 校验通过。

已发布个人版本 **v1.2.190**：`cam-20261019-v1.2.190-b19ae8eb28`。此前配置的 7 个 Node 通过 release/个人入口哈希和 Server 单实例检查，SDK 均刷新；7 个 Node ready、元数据 healthy，原有 65 个 agent ID 全部保留，Client 配置哈希不变。没有 reboot agent，也没有取消用户已提交的 `1936576709` 或原有 allocation。已提交的排队命令继续原流程，后续新启动使用修复后的逻辑。

私有原始记录：`/tmp/cam-autorun-reuse-20261019/`、`/tmp/cam-autorun-reuse-release-20261019/`；不整体提交这些目录。修复后的 self 分支由回归测试覆盖；真实附加容器没有 `/usr/bin/python3`，因此未将诊断脚本的可选 Python 上下文检查记为通过，Claude 版本探测与 auto 选择结果分别单独核实。

## SITE-A046 本机 battach 实测

从 `site-a-container-xterm-046` 执行
`battach -L /bin/hostname 1729124669`，返回 `site-a3-sim-e14-027`、退出码 0。
重新核实该作业为本人 RUN、交互式 `autorun_sla`，虚拟队列
`o_pri_interactive_cpu_16G`、16 GB，应用为 `c75docker`。

本机已安装 v1.2.190 的 auto 解析器也实际选中同一 battach 作业，
Claude 路径为 `/home/site_share/tools/claude-code/bin/claude`。
测试禁止进入 qsub 准备流程，并禁用 Claude 自动更新。

临时 Claude agent `55260f5d` 使用仅限该 job 的自定义 battach 启动器，
避免测试失败时提交新 allocation；这是实际 battach 与工具交互测试，
不是整条 auto fallback 链的实测。终端记录
`LSB_JOBID=1729124669` 与远端主机，Claude 2.1.296 实际回复
`CAM_BATTACH_CLAUDE_OK`，camc 在约 39 秒时观测到 `alive / started`。
随后发送 `/exit`，工具退出码 0；只移除测试 agent、tmux 和临时目录，
保留原有 allocation，未提交新 qsub。

Codex 未通过该 allocation 的版本探测：默认解析到
`/home/site_share/tools/codex/bin/codex`，依赖的 Node 在 CentOS 7.5 中报告
缺少 `GLIBC_2.28`、`GLIBC_2.27`、`GLIBCXX_3.4.21` 等运行库。
这不是 battach 连接失败；该环境不能用于当前 Codex，不能把 Codex 启动
记为通过。未修改或重装共享工具。

本次私有测试证据：`/tmp/cam-battach-local-20261019/`。

## 交互 16G 默认与 v1.2.191 同步

已将两处自动 qsub 路径的默认虚拟队列改为
`o_pri_interactive_cpu_16G`，保留 `autorun_sla`、`-Is`、Rocky 8 应用参数
及已有 allocation 优先复用的顺序。用户自定义 launcher 的队列不变。
`bqueues` 确认 SITE-A 的物理 `o_pri_interactive` 为 Open:Active；
`qsub --debug` 核实映射、16 GB 内存及 `autorun_sla`，未提交新 job。
此项队列修正不代表旧 job `1936576709` 的退出码 255 已定位或修复。

发布前 Python 全套 **1728 passed、1 skipped**（213.38 秒），构建产物
通过 Python 3.6 语法、CLI smoke、Dispatched-to 校验及两处默认队列检查。

已发布个人版本 **v1.2.191**：`cam-20261019-v1.2.191-73a53f8039`，
内容摘要 `73a53f80392931e8a7d7940d1cc43aea4432a7c791e7d1b19c17f3cc30ca57d3`。
SITE-A046、064、086、106，SITE-B001、009 和 SITE-C 共 7 台 Server 的个人 camc
入口及 SDK 后端全部更新；每台 63 个包文件和安装入口哈希校验通过，
各台仍为单 Server，7 个 Node ready、元数据 healthy，daemon healthy。
升级前后原有 65 个 agent ID 全部保留，Client 连接配置哈希不变。
没有 reboot 现有 agent，也没有提交或取消 LSF job。已有启动命令继续原流程，
新启动及后续 reboot 使用新默认。

发布证据：`/tmp/cam-interactive16g-release-20261019/`。未 commit 或 push。
