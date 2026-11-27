# MultiCodex

MultiCodex 在同一个项目里管理多个 ChatGPT 账号。每个账号有独立的凭据、缓存和日志，同一运行模式下的账号共享本地会话。

## 立即开始

解压与你的平台对应的发行包，在项目目录运行解压出来的 `mcdx`。发行包含 Codex 和 Python 运行时。Linux x86_64 发行包在 Ubuntu 24.04 构建。

```sh
cd /path/to/project
/path/to/multicodex-linux-x86_64/mcdx
```

首次打开按 Enter，输入本地别名并回车，就会进入 ChatGPT 设备码登录。账号需启用设备码登录。选中账号后，Enter 启动 Codex，`r` 打开当前项目的跨账号会话列表。还可以按 `a` 添加并登录账号、`l` 重新登录、`s` 检查登录状态、`x` 登出。账号列表的凭据标记表示本地已有凭据文件。

本机模式也可以用大写 `L` 或 `mcdx login 账号别名 --browser` 打开浏览器登录。通过 SSH 使用远程服务器时，先从浏览器所在电脑建立回调端口转发，例如 `ssh -L 1455:localhost:1455 user@server`，再在该 SSH 会话中启动浏览器登录。以 Codex 显示的回调端口为准。

如需从任意项目直接运行 `mcdx`，在解压目录运行一次 `./mcdx install`，它会在 `~/.local/bin` 创建入口。该目录需在 `PATH` 中。请保留解压目录里的运行时和配套文件。默认工作目录是启动命令时的目录，也可用 `mcdx --workspace /path/to/project` 指定。

本机版直接读写原版 Codex 的 `~/.codex/sessions/`、`~/.codex/archived_sessions/` 和本地会话数据库。每个账号的凭据、配置、缓存和日志位于 `~/.codex/multicodex/accounts/<别名>/`。旧版 MultiCodex 数据在首次启动时迁入。迁移需要退出仍在运行的旧版窗口。

本机版沿用 Codex 自带的沙箱。管理器的 `r` 通过本地 App Server 枚举共享会话，再按 ID 在所选账号下打开。内置的 `/resume` 可通过面向 Codex 0.172.0 的 `resume.patch` 放开账号筛选。

## Docker 版

已有 Docker Compose 环境的用户可以选择 `mcdx --docker`。维护者或自托管用户可以用 `mcdx build` 构建带有 `/resume` 补丁的镜像。运行需要对应的镜像和 Docker daemon 访问权限。

Docker 版的数据位于 `~/.codex/multicodex/docker/`。它的工作区及会话文件路径按容器环境记录。容器共用会话目录和 Codex 会话写锁。同一项目的两个 Docker 账号通过固定容器路径访问项目。先前默认工作区下的会话，可以回到 MultiCodex 源码目录下的 `workspace/` 打开。

Codex 默认使用 Bubblewrap。若 Docker 宿主限制嵌套沙箱，可显式运行 `MULTICODEX_CONTAINER_SANDBOX=1 mcdx --docker`，由 Docker 提供文件系统隔离并采用 `untrusted` 审批策略。获准执行的命令能访问当前容器挂载的数据。

## 开发与发布

从源码运行需要 Python 3.9+（含 curses）和可执行的 Codex。可在 `codex-runtime/` 放入 Codex 运行时包，开发环境也可使用 `PATH` 中的 `codex`。

```sh
python3 /path/to/mcdx/multicodex.py
```

在目标平台上安装 PyInstaller 后，由维护者生成分发包。Linux 本机版可用 `build-patched-runtime.sh` 编译带补丁的主程序。该脚本需要 Rust 1.99.1、Codex 0.172.0 源码及运行时，以及对应主机目标的 upstream V8 资产：

```sh
bash build-patched-runtime.sh /path/to/codex-0.172.0-source /path/to/stock-runtime /path/to/v8-assets
bash package-release.sh ./codex-runtime /path/to/codex-0.172.0-source
```

压缩包和 SHA-256 校验文件输出到 `dist/`。同一账号可以在多个窗口同时处理不同会话。每条会话由 Codex 的写锁保护，已有窗口在写入时，另一个窗口可以查看历史。要接续同一条会话，退出占用它的窗口，再在另一窗口按 `r` 重试。
