# CAM Desktop MSI 构建备忘（WSL + Windows node）

> 记录在本机（Thinkpad / Windows 11 + WSL）从 WSL 侧触发 Windows MSI 构建的真实命令和踩坑过程。
> 最后更新：2026-08-24

---

## 环境

- 机器：Thinkpad，Windows 11 + WSL2
- Repo 路径：
  - Windows：`C:\Users\Thinkpad\gitlab\cam`
  - WSL：`/mnt/c/Users/Thinkpad/gitlab/cam`
- 自带 Node 20：`C:\Users\Thinkpad\gitlab\cam\.tools\node\node.exe`（v20.18.1）
  - 这个目录里同时有 `npm.cmd` / `npx.cmd`（Windows 版）和 `npm` / `npx`（Git Bash shell 脚本）。
- 系统 PATH 里没有可用的 `node` / `npm`，因此必须从 repo 的 `.tools/node` 调用。

---

## 正确的构建命令

因为 `electron-builder` 会在 Windows cmd 子进程里调用 `node` 和 WiX 工具链，所以要从 WSL 里通过 `cmd.exe /c` 启动，并显式把 `.tools\node` 放到 PATH 最前面：

```bash
cd /mnt/c/Users/Thinkpad/gitlab/cam/apps/cam-desktop
cmd.exe /c "set PATH=C:\Users\Thinkpad\gitlab\cam\.tools\node;C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem && npm run build:win-msi"
```

等价于以下 npm script（来自 `apps/cam-desktop/package.json`）：

```json
"build:win-msi": "electron-builder --win msi --config.win.signAndEditExecutable=false --config.win.verifyUpdateCodeSignature=false"
```

产物位置：

```text
apps/cam-desktop/dist/CAM-Desktop-0.2.30.msi
```

---

## 踩坑记录

### 坑 1：在 WSL bash 里直接用 `npm run build:win-msi`

```bash
export PATH="/mnt/c/Users/Thinkpad/gitlab/cam/.tools/node:$PATH"
cd /mnt/c/Users/Thinkpad/gitlab/cam/apps/cam-desktop
npm run build:win-msi
```

**失败：**

```text
> cam-desktop@0.2.30 build:win-msi
> electron-builder --win msi --config.win.signAndEditExecutable=false --config.win.verifyUpdateCodeSignature=false

'"node"' is not recognized as an internal or external command,
operable program or batch file.
```

**原因：**
WSL bash 默认解析到的 `npm` 是 Linux 版 npm（例如 `/home/hren/.local/node20/bin/npm`），不是 `.tools/node/npm.cmd`。electron-builder 在 Windows cmd 子进程里找 `node` 时，PATH 没有正确携带 Windows 风格的 `.tools\node`，于是报找不到 node。

**解法：** 见上文「正确的构建命令」，通过 `cmd.exe /c` 显式设置 Windows PATH 并调用 `npm.cmd`。

---

## 构建历史

| 时间 | 版本 | 命令 | 结果 | 备注 |
|---|---|---|---|---|
| 2026-08-24 | 0.2.30 | WSL bash 直接 `npm run build:win-msi` | ❌ 失败：找不到 node | PATH 未传对 |
| 2026-08-24 | 0.2.30 | `cmd.exe /c "set PATH=... && npm run build:win-msi"` | ✅ 成功 | 产物 `CAM-Desktop-0.2.30.msi`（≈90 MB）|

---

## 参考

- `apps/cam-desktop/package.json` 里的 `build:win-msi` script
- `docs/desktop/development-guide.md` §14「环境速查」
