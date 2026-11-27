# MultiCodex

MultiCodex 在同一个项目里管理多个 ChatGPT 账号。每个账号有独立的凭据、缓存和日志，同一运行模式下的账号共享本地会话。

## 立即开始

Linux x86_64 用户可以用一条命令安装或更新 GitHub 上的最新正式版。

```sh
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/athelstan189/mcdx/main/install.sh | bash' && export PATH="$HOME/.local/bin:$PATH"
```

安装后，在任意项目目录运行 `mcdx`。发行包自带 Codex 和 Python，默认使用本机模式。安装脚本会校验压缩包，把程序放在 `~/.local/opt/mcdx/`，并创建 `~/.local/bin/mcdx`。如果你已经下载了压缩包和 `.sha256` 文件，也可以在源码目录运行 `bash install.sh --from-dir 下载目录`。

手动安装时，解压发行包，在项目目录运行解压出来的 `mcdx`。Linux x86_64 发行包在 Ubuntu 24.04 构建。

```sh
cd /path/to/project
/path/to/multicodex-linux-x86_64/mcdx
```

首次打开按 Enter，输入本地别名并回车，就会弹出登录方式选择。用方向键选设备码或浏览器登录，再按 Enter。设备码登录需要在 ChatGPT 开启设备码授权。弹窗会显示授权链接、进度和结果。使用 Tab 选中「打开浏览器」「复制链接」或「取消」，再按 Enter 执行。复制链接需要终端支持剪贴板。

管理卡片包含 Accounts 和 Actions 两个面板，快捷键与状态提示在卡片下方。Accounts 显示 Codex 的 5 小时和 7 天用量窗口的剩余百分比。`5h reset` 一列显示距离 5 小时额度重置的时间，并随时间倒计时。用 ↑ / ↓ 选择账号，Enter 直接打开 Codex，尚未登录的账号会先打开登录窗口。按 Tab 切到 Actions，选中 Check status 后按一次 Enter，Accounts 中的额度和重置时间会在后台刷新。缺失的数据以 `—` 显示。窄终端一次显示一个面板。

改名时先选中账号，再按 Tab 切到 Actions，选择 **Change name**。输入新别名后按 Enter 保存。改名后可继续使用原有的登录凭据和会话。**Quit account** 退出管理界面。

启动管理界面时，mcdx 会在后台检查 GitHub 正式版。发现新版时，卡片下方会提示版本，按 `u` 可以查看、打开或复制发布页链接，更新由你决定。用 `mcdx --version` 查看当前版本。

本机模式也可以用大写 `L` 直接进入浏览器登录弹窗，或用 `mcdx login 账号别名 --browser` 启动命令行登录。通过 SSH 使用远程服务器时，先从浏览器所在电脑建立回调端口转发，例如 `ssh -L 1455:localhost:1455 user@server`，再在该 SSH 会话中启动浏览器登录。Docker 模式的弹窗提供设备码登录。

如需从任意项目直接运行 `mcdx`，在解压目录运行一次 `./mcdx install`，它会在 `~/.local/bin` 创建入口。该目录需在 `PATH` 中。请保留解压目录里的运行时和配套文件。默认工作目录是启动命令时的目录，也可用 `mcdx --workspace /path/to/project` 指定。

本机版直接读写原版 Codex 的 `~/.codex/sessions/`、`~/.codex/archived_sessions/` 和本地会话数据库。每个账号的凭据、配置、缓存和日志位于 `~/.codex/multicodex/accounts/` 下的专属目录，改名后继续使用同一个目录。旧版 MultiCodex 数据在首次启动时迁入。迁移需要退出仍在运行的旧版窗口。

本机版沿用 Codex 自带的沙箱。管理器的恢复菜单通过本地 App Server 枚举共享会话，再按 ID 在所选账号下打开。内置的 `/resume` 可通过面向 Codex 0.172.0 的 `patches/resume.patch` 放开账号筛选。

## Docker 版

已有 Docker Compose 环境的用户可以选择 `mcdx --docker`。维护者或自托管用户可以用 `mcdx build` 构建带有 `/resume` 补丁的镜像。运行需要对应的镜像和 Docker daemon 访问权限。

Docker 版的数据位于 `~/.codex/multicodex/docker/`。它的工作区及会话文件路径按容器环境记录。容器共用会话目录和 Codex 会话写锁。同一项目的两个 Docker 账号通过固定容器路径访问项目。先前默认工作区下的会话，可以回到 MultiCodex 源码目录下的 `workspace/` 打开。

Codex 默认使用 Bubblewrap。若 Docker 宿主限制嵌套沙箱，可显式运行 `MULTICODEX_CONTAINER_SANDBOX=1 mcdx --docker`，由 Docker 提供文件系统隔离并采用 `untrusted` 审批策略。获准执行的命令能访问当前容器挂载的数据。

## 开发与发布

- `mcdx/` 放账号管理器的 Python 代码，根目录的 `multicodex.py` 是启动入口。
- `docker/` 放 Compose 配置和镜像文件。
- `patches/` 放本机版和 Docker 版共用的 Codex 补丁。
- `scripts/` 放编译和打包脚本。

从源码运行需要 Python 3.9+（含 curses）和可执行的 Codex。可在 `codex-runtime/` 放入 Codex 运行时包，开发环境也可使用 `PATH` 中的 `codex`。

```sh
python3 /path/to/mcdx/multicodex.py
```

在目标平台上安装 PyInstaller 后，由维护者生成分发包。Linux 本机版可用 `scripts/build-patched-runtime.sh` 编译带补丁的主程序。该脚本需要 Rust 1.99.1、Codex 0.172.0 源码及运行时，以及对应主机目标的 upstream V8 资产。

```sh
bash scripts/build-patched-runtime.sh /path/to/codex-0.172.0-source /path/to/stock-runtime /path/to/v8-assets
bash scripts/package-release.sh ./codex-runtime /path/to/codex-0.172.0-source
```

压缩包和 SHA-256 校验文件输出到 `dist/`。同一账号可以在多个窗口同时处理不同会话。每条会话由 Codex 的写锁保护，已有窗口在写入时，另一个窗口可以查看历史。要接续同一条会话，退出占用它的窗口，再在另一窗口按 `r` 重试。

发布新版前更新 `mcdx/release_update.py` 里的 `VERSION`。将分发包和校验文件上传到 GitHub Release，并使用对应的 `v主版本.次版本.补丁版本` 标签。
