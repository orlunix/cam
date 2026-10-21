# CAM Server 版本、安装与更新

CAM TUI 0.1.6 / CAM Server 0.1.3 起采用本流程。Server 是每个 Linux
主机、每个用户的单实例服务；Client 连接多个 Server。新增 Node 不要求用户
预先安装 Server、填写内部 ID、拼 transport 命令或复制 token。

测试结果及已知回归限制见 [验收记录](testing/cam-server-setup-update-20261021.md)。

## Add / Connect

1. 用户提供 SSH Host，Port 默认 22，Username 和 key 可选。原生 OpenSSH
   负责 known_hosts、密码和 key 口令；CAM 不保存这些登录秘密。
2. 认证后检查远端 `cam-server version`、已有配置、运行进程版本和 SDK。
   SSH bootstrap 是单行 Python 调用，兼容 bash、sh、tcsh/csh 登录 shell；
   release 数据走 SSH stdin，不经过 shell 展开。
3. binary 版本达到当前 Client 要求，且服务/SDK 可用：直接 owner pair 和
   connect。保留较新的 binary；不复制、不降级、不重启健康服务。
4. binary 缺失、过旧或 SDK 缺失：从 Client 配套 release 同步静态 binary
   和匹配 SDK，核对 CPU、版本、长度、SHA-256，并验证 SDK 可导入。
   不支持的架构、错误校验和、危险归档路径、并发 setup 或身份不符均明确失败。
5. 新 Node 初始化一次；已有 Node 保留 ID、alias、runtime、grants、peers
   和其它设置。服务未启动时启动；需升级的进程只在 UID、config 路径和
   `/proc` 启动时间匹配时终止。不能确认进程身份则拒绝重启。
6. 等待 socket、Node 身份和 SDK ready，然后 pair 和保存 Client endpoint。
   发布新 binary 入口前必须完成验证。失败保留旧 release，恢复已有配置；
   若已停止旧服务，尝试用备份 executable 恢复服务，原始失败仍返回用户。

Add、Edit 保存以及显式 Connect 共用此流程。后台 tunnel 重连只重连，不
因短暂掉线反复部署。token 仅通过认证后的 SSH 流传回，并写入 Client 私有
credential 文件；UI、错误和发布包不包含 token、密码或 key 口令。

## 版本与管理命令

```sh
cam-server version
cam-server --version
cam-server version --json
cam-server status                         # 默认 hostname 配置
cam-server status --config /private/server.json
cam-server setup --bundle /release/cam-server.bundle
cam-server update --bundle /new-release/cam-server.bundle
```

`version --json` 返回软件 `version`、Linux `target` 和 `protocol`。
运行中的 `status` 返回独立的 `server_version`；帧的 `version: 1` 仍是协议版本。
setup/update 使用与 Client 相同的校验、安装和启动流程，幂等且不降级。
省略 `--bundle` 时读取 executable 同目录的 bundle；更新到其它版本需提供
新的 release 文件。不会擅自访问公网下载，也不修改 cron/systemd。

与 camc 一样使用版本目录和原子入口切换，而不是覆盖正在执行的文件：

```text
~/.cam/server/releases/<target>/v<version>-<sha256-prefix>/
  cam-server
  cam-server.bundle
  cam-server.bundle.json
  sdk/
~/.cam/server/current-<hostname> -> release
~/.local/bin/cam-server -> release/cam-server
~/.cam/server/service-<hostname>.json
~/.cam/server/logs/<hostname>.log
```

私有目录 0700，配置、凭据、SDK 文件和日志 0600，executable 0700。
原配置升级前保存在同路径 `.before-update`；需要重启时额外保留
`~/.cam/server/previous-<hostname>/cam-server`。旧 release 不自动清除。
共享 Home 的服务配置、current 和日志按 hostname 区分；当前通用 binary
入口仍应由同 CPU 架构的机器使用。

构建脚本 `components/cam-router/scripts/build-linux.sh` 产出四个静态 Rust
程序、`cam-server.bundle`、`cam-server.bundle.json` 和 `SHA256SUMS`。
部署 TUI 时 bundle 与 manifest 必须跟 `cam-clientd` 放在同目录，不能只复制
UI executable。SDK 源自相同 checkout，包含 adapter configs 和内置 skills，
不打包用户 Home、配置、agents.json 或凭据。

## 边界

- 静态 binary 不依赖系统 glibc；管理 SDK 仍需远端 Python 3.6+、tmux 和工具。
  Client 只能同步其发布包所含架构；不做跨架构编译或在线下载。
- 更新 Server 不重启 agent、monitor 或 tmux，也不改 agent registry。Server
  连接会暂时中断；正在执行的管理操作可能返回 unknown，应查询原 request ID，
  不能把请求自动再执行一次。
- 一台机器的同一用户仅运行一个生产 Server。已有其它 config 的服务由原有
  单实例锁拒绝，不会绕过该锁启动第二个 Server。
- 不声称系统重启后自启动，也不新增 update 定时任务。安装/启动失败须检查
  明确错误码和私有 Server 日志；不会退回另一个 Node 或偷偷更换身份。
