import argparse
import curses
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import ExitStack
from pathlib import Path
from time import time

from .account_status import AccountStatus, StatusChecks, remaining_quota
from .login_dialog import DOWN, ENTER, TAB, UP, box, copy_link, open_browser, sign_in
from .native_runtime import NativeRuntime, app_server, import_sessions, locked, process_env
from .release_update import REPOSITORY, VERSION, check_for_update

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parents[1]
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ACCOUNT_ACTIONS = ("open", "resume", "login", "status", "logout")
PROJECT_ACTIONS = ("open", "resume")
DASHBOARD_ACTIONS = (("add", "Add account"), ("check-status", "Check status"), ("quit", "Quit"))
SESSION_DIRECTORIES = ("sessions", "archived_sessions")
SHARED_DIRECTORIES = (*SESSION_DIRECTORIES, "thread-writer-locks")


def skip_legacy_runtime_files(directory, names):
    excluded = {"sessions", "archived_sessions", "tmp", "thread-writer-locks"}
    root = Path(directory)
    return {
        name for name in names
        if name in excluded or name.endswith((".lock", "-wal", "-shm"))
        or not ((root / name).is_dir() or (root / name).is_file())
        or (root / name).is_symlink()
    }


class MultiCodex:
    def __init__(self, workspace, docker=False):
        self.workspace = workspace.expanduser().resolve()
        self.docker = docker
        self.codex_home = (Path.home() / ".codex").resolve()
        self.data = self.codex_home / "multicodex"
        backend = self.data / "docker" if docker else self.data
        self.accounts_dir = backend / "accounts"
        self.shared = backend / "shared" if docker else self.codex_home
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        directories = (*SHARED_DIRECTORIES, "sqlite") if docker else SHARED_DIRECTORIES
        for directory in directories:
            (self.shared / directory).mkdir(parents=True, exist_ok=True)
        self.migrate(backend)
        self.status_checks = StatusChecks(self.account_status)

    def migrate(self, backend):
        marker = backend / ".migrated-xdg"
        if marker.exists():
            return
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        legacy = data_home / "multicodex"
        if not self.docker:
            legacy /= "native"
        if not (legacy / "accounts").is_dir():
            return

        busy = "Exit the previous MultiCodex window before moving its account data."
        with locked(backend / ".migration.lock", busy), ExitStack() as locks:
            if marker.exists():
                return
            old_lock = legacy / "shared/sqlite/.interactive.lock"
            if old_lock.parent.is_dir():
                locks.enter_context(locked(old_lock, busy))
            for source in sorted((legacy / "accounts").iterdir()):
                if not NAME.fullmatch(source.name) or source.is_symlink() or not (source / ".codex").is_dir():
                    continue
                target = self.accounts_dir / source.name
                if target.exists():
                    continue
                with tempfile.TemporaryDirectory(prefix=".migrate-", dir=self.accounts_dir) as folder:
                    staged = Path(folder) / "account"
                    shutil.copytree(source / ".codex", self.account_codex_home(staged), ignore=skip_legacy_runtime_files)
                    self.prepare_account(staged)
                    staged.rename(target)
            if import_sessions(legacy / "shared", self.shared, quiet=True):
                raise ValueError("Some previous session files are still changing. Retry after exiting the old Codex window.")
            marker.touch()

    def accounts(self):
        return sorted(
            path.name for path in self.accounts_dir.iterdir()
            if NAME.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
        )

    def add(self, name):
        if not NAME.fullmatch(name):
            raise ValueError("Use 1–32 lowercase letters, numbers, underscores or hyphens. Start with a letter or number.")
        home = self.accounts_dir / name
        if home.is_symlink() or (home.exists() and not home.is_dir()):
            raise ValueError("The account path is occupied.")
        self.prepare_account(home)

    def account_codex_home(self, home):
        return home / ".codex" if self.docker else home

    def prepare_account(self, home):
        private_home = self.account_codex_home(home)
        (private_home / "log").mkdir(parents=True, exist_ok=True)
        for directory in SHARED_DIRECTORIES:
            link = private_home / directory
            if self.docker:
                link.mkdir(exist_ok=True)
            else:
                target = self.shared / directory
                try:
                    link.symlink_to(target, target_is_directory=True)
                except FileExistsError:
                    if not link.is_symlink() or link.resolve() != target.resolve():
                        raise ValueError(f"Shared session directory is occupied: {link}")

    def has_credentials(self, name):
        return (self.account_codex_home(self.accounts_dir / name) / "auth.json").is_file()

    def project_mount(self):
        if self.workspace.is_relative_to(self.codex_home) or self.codex_home.is_relative_to(self.workspace):
            raise ValueError("This directory overlaps MultiCodex data. Start mcdx inside a project or use --workspace.")
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self.workspace == (ROOT / "workspace").resolve():
            return "/workspace"
        project_id = hashlib.sha256(os.fsencode(self.workspace)).hexdigest()[:24]
        return f"/workspaces/{project_id}"

    def account_home(self, name):
        if name not in self.accounts():
            raise ValueError(f"Unknown account: {name}")
        home = self.accounts_dir / name
        self.prepare_account(home)
        return home

    def compose_invocation(self, home, mount=None):
        return [
            "docker", "compose", "--env-file", "/dev/null",
            "-f", str(ROOT / "docker/compose.yaml"),
        ], {
            **process_env(),
            "MC_HOME": str(home),
            "MC_SHARED": str(self.shared),
            "MC_WORKSPACE": str(self.workspace),
            "MC_PROJECT_DIR": mount or "/workspace",
            "MC_UID": str(os.getuid()),
            "MC_GID": str(os.getgid()),
        }

    def app_server_invocation(self, name, container=None):
        home = self.account_home(name)
        args = ["app-server", "--listen", "stdio://"]
        if not self.docker:
            runtime = NativeRuntime(ROOT, self.shared, home, self.workspace)
            return runtime.command + args, {"env": runtime.env, "cwd": home}
        command, env = self.compose_invocation(home)
        command += ["run", "--rm", "--no-deps", "-T"]
        if container:
            command += ["--name", container]
        return command + [
            "--entrypoint", "codex", "codex",
            "-c", 'cli_auth_credentials_store="file"',
            "-c", 'log_dir="/home/codex/.codex/log"',
            *args,
        ], {"env": env}

    def run(self, action, name=None, thread=None):
        if self.docker and action == "browser-login":
            raise ValueError("Browser sign-in is available in native mode. Use device sign-in for Docker.")
        home = self.accounts_dir if action == "build" else self.account_home(name)

        if not self.docker:
            runtime = NativeRuntime(ROOT, self.shared, home, self.workspace)
            if action in PROJECT_ACTIONS:
                self.workspace.mkdir(parents=True, exist_ok=True)
            if action == "resume" and not thread:
                thread = curses.wrapper(choose_session, runtime.sessions())
                if thread is None:
                    return 0
            return runtime.run(action, thread)

        command, options = self.docker_invocation(action, home, thread)
        return subprocess.run(command, **options, check=False).returncode

    def docker_invocation(self, action, home, thread=None):
        mount = self.project_mount() if action in PROJECT_ACTIONS else None
        command, env = self.compose_invocation(home, mount)
        if action == "build":
            command += ["build", "codex"]
        else:
            command += ["run", "--rm", "--no-deps"]
            if action in ("status", "logout"):
                command += ["-T"]
            command += ["project" if mount else "codex", action]
            if thread:
                command.append(thread)
        return command, {"env": env}

    def account_status(self, name):
        home = self.account_home(name)
        if self.docker:
            command, options = self.docker_invocation("status", home)
        else:
            runtime = NativeRuntime(ROOT, self.shared, home, self.workspace)
            command, options = runtime.invocation("status")
        completed = subprocess.run(command, **options, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   errors="replace", timeout=10, check=False)
        if completed.returncode:
            if "Not logged in" in completed.stdout.splitlines():
                return AccountStatus("Signed out", "muted")
            return AccountStatus("Check failed", "error")
        try:
            command, options = self.app_server_invocation(name)
            with app_server(command, timeout=10, **options) as request:
                response = request("account/rateLimits/read", {"excludeResetCreditDetails": True})
            five_hours, week, five_hours_reset_at = remaining_quota(response)
        except (OSError, ValueError):
            return AccountStatus("Signed in · quota unavailable", "error")
        return AccountStatus("Signed in", "good", five_hours, week, five_hours_reset_at)


def choose(screen, manager, selected, message, update_check):
    accounts = manager.accounts()
    index = accounts.index(selected) if selected in accounts else 0
    focus = "accounts" if accounts else "actions"
    action_index = 0
    styles = dashboard_styles()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    dialog = None
    dialog_index = 0
    alias = error = ""
    shortcuts = {"a": "add", "q": "quit", "s": "check-status"}
    if manager.docker:
        shortcuts["b"] = "build"
    account_shortcuts = {"r": "resume", "l": "login", "x": "logout"}
    if not manager.docker:
        account_shortcuts["L"] = "browser-login"
    checks = manager.status_checks
    refreshing = []
    for account in accounts:
        if manager.has_credentials(account):
            checks.refresh(account)
        else:
            checks.forget(account)
    while True:
        checks.poll()
        if refreshing and all(account not in checks.pending for account in refreshing):
            failed = sum(checks.results[account].style == "error" for account in refreshing)
            message = (f"Status refresh failed for {failed}/{len(refreshing)} accounts.", True) if failed else (
                "Account status refreshed.", False
            )
            refreshing = []
        name = accounts[index] if accounts else None
        status_message = ("Refreshing account status…", False) if refreshing else message
        update_pending = not update_check.done()
        update = None if update_pending else update_check.result()
        now = int(time())
        countdown_active = any((status.five_hours_reset_at or 0) > now for status in checks.results.values())
        draw_dashboard(screen, manager, accounts, index, status_message, styles,
                       focus=focus, action_index=action_index, update=update)
        if dialog:
            draw_dialog(screen, dialog, alias if dialog == "add" else name, error, styles, dialog_index)
        screen.refresh()
        idle_timeout = 1000 if countdown_active else -1
        screen.timeout(150 if checks.pending or update_pending else idle_timeout)
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        except KeyboardInterrupt:
            if not dialog:
                raise
            dialog = None
            continue
        screen.timeout(-1)

        if dialog == "add":
            if key == "\x1b" or (key in ENTER and dialog_index == 1):
                dialog = None
            elif key in (*TAB, curses.KEY_UP, curses.KEY_DOWN):
                dialog_index = 1 - dialog_index
            elif key in ENTER:
                try:
                    manager.add(alias)
                except (OSError, ValueError) as exc:
                    error = str(exc)
                else:
                    accounts = manager.accounts()
                    index = accounts.index(alias)
                    focus, action_index = "accounts", 0
                    dialog = None
                    message = sign_in(screen, manager, accounts, index, styles, draw_dashboard, draw_text)
            elif dialog_index == 0 and key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                alias, error = alias[:-1], ""
            elif (dialog_index == 0 and isinstance(key, str)
                  and key in "abcdefghijklmnopqrstuvwxyz0123456789_-" and len(alias) < 32):
                alias, error = alias + key, ""
            continue

        if dialog == "logout":
            if key in ("y", "Y") or (key in ENTER and dialog_index == 1):
                return "logout", name
            if key in (*TAB, *UP, *DOWN, curses.KEY_LEFT, curses.KEY_RIGHT):
                dialog_index = 1 - dialog_index
            if key in ("n", "N", "\x1b", *ENTER):
                dialog = None
            continue

        if key == "u" and update:
            def redraw():
                draw_dashboard(screen, manager, accounts, index, status_message, styles,
                               focus=focus, action_index=action_index, update=update)

            view_update(screen, styles, update, redraw)
            continue
        if accounts and key in (*TAB, curses.KEY_LEFT, curses.KEY_RIGHT):
            if key in TAB:
                focus = "actions" if focus == "accounts" else "accounts"
            else:
                focus = "accounts" if key == curses.KEY_LEFT else "actions"
            continue
        if key in (*UP, *DOWN):
            step = 1 if key in DOWN else -1
            if focus == "accounts":
                index = (index + step) % len(accounts)
            else:
                action_index = (action_index + step) % len(DASHBOARD_ACTIONS)
            continue
        if key == "\x1b":
            if focus == "actions" and accounts:
                focus = "accounts"
                continue
            return "quit", name

        action = shortcuts.get(key) or (account_shortcuts.get(key) if accounts else None)
        if key in ENTER:
            if focus == "actions":
                action = DASHBOARD_ACTIONS[action_index][0]
            else:
                action = "open" if manager.has_credentials(name) else "login"
        if action == "add":
            dialog, alias, error, dialog_index = "add", "", "", 0
        elif action == "logout":
            dialog, dialog_index = "logout", 0
        elif action in ("login", "browser-login"):
            message = sign_in(screen, manager, accounts, index, styles, draw_dashboard, draw_text,
                              browser=(action == "browser-login"))
        elif action == "check-status":
            if not refreshing:
                refreshing = [account for account in accounts if manager.has_credentials(account)]
                for account in refreshing:
                    if account not in checks.pending:
                        checks.refresh(account, force=True)
                if not refreshing:
                    message = ("No accounts with saved credentials.", False)
                elif not wide_dashboard(*screen.getmaxyx()):
                    focus = "accounts"
        elif action:
            return action, name


def dashboard_styles():
    styles = {
        "accent": curses.A_BOLD, "muted": curses.A_DIM, "selected": curses.A_REVERSE,
        "good": curses.A_BOLD, "error": curses.A_BOLD,
    }
    if not curses.has_colors():
        return styles
    try:
        curses.start_color()
        curses.use_default_colors()
        colors = (
            ("accent", 81, curses.COLOR_CYAN),
            ("muted", 245, curses.COLOR_WHITE),
            ("good", 114, curses.COLOR_GREEN),
            ("error", 203, curses.COLOR_RED),
        )
        for pair, (name, rich, basic) in enumerate(colors, 1):
            curses.init_pair(pair, rich if curses.COLORS >= 256 else basic, -1)
            styles[name] |= curses.color_pair(pair)
        if curses.COLORS >= 256:
            curses.init_pair(5, 231, 24)
            styles["selected"] = curses.color_pair(5) | curses.A_BOLD
    except curses.error:
        pass
    return styles


def draw_text(screen, row, col, value, style=0, width=None, *, keep_end=False):
    rows, cols = screen.getmaxyx()
    if not (0 <= row < rows and 0 <= col < cols - 1):
        return
    limit = min(cols - col - 1, width if width is not None else cols)
    if limit <= 0:
        return
    value = "".join(char if char.isprintable() else " " for char in value)
    sizes = [0 if unicodedata.combining(char) else
             2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in value]
    if sum(sizes) > limit:
        count = 0
        for size in reversed(sizes) if keep_end else sizes:
            if size >= limit:
                break
            limit -= size
            count += 1
        value = "…" + value[len(value) - count:] if keep_end else value[:count] + "…"
    screen.addstr(row, col, value, style)


def draw_panel(screen, top, left, width, height, title, style):
    edge = "─" * (width - 2)
    draw_text(screen, top, left, "╭" + edge + "╮", style)
    draw_text(screen, top, left + 2, f" {title} ", style, width - 4)
    for row in range(top + 1, top + height - 1):
        draw_text(screen, row, left, "│", style)
        draw_text(screen, row, left + width - 1, "│", style)
    draw_text(screen, top + height - 1, left, "╰" + edge + "╯", style)


def quota_label(percent, pending=False):
    if percent is not None:
        return f"{percent}%"
    return "…" if pending else "—"


def reset_label(resets_at, now, pending=False):
    if resets_at is None:
        return "…" if pending else "—"
    seconds = max(0, resets_at - now)
    if 0 < seconds < 60:
        return "<1m"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def wide_dashboard(rows, cols):
    return cols >= 76 and rows >= 17


def draw_dashboard(screen, manager, accounts, index, message, styles, *, focus=None, action_index=0, update=None):
    screen.erase()
    rows, cols = screen.getmaxyx()
    if rows < 13 or cols < 32:
        draw_text(screen, 0, 0, "MultiCodex", styles["accent"])
        draw_text(screen, 1, 0, "Enlarge terminal", styles["muted"])
        return

    card_width = min(128, cols - 2)
    card_left = (cols - card_width) // 2
    width = card_width - 4
    left = card_left + 2
    wide = wide_dashboard(rows, cols)
    account_width = width - 32 if wide else width
    menu_width = 30 if wide else width
    side = left + account_width + 2 if wide else left
    count = len(accounts)
    show_accounts = wide or (bool(accounts) and focus != "actions")
    show_actions = wide or not show_accounts
    panel_items = max(count if show_accounts else 0, len(DASHBOARD_ACTIONS) if show_actions else 0)
    height = min(rows - 9, max(6, panel_items + 3))
    capacity = height - 3
    card_height = height + 6
    free_rows = rows - card_height
    card_top = min(round(free_rows * (1 - 0.618)), free_rows - 3)
    top = card_top + 4
    title = "MultiCodex  ·  Docker" if manager.docker else "MultiCodex"
    draw_panel(screen, card_top, card_left, card_width, card_height, title, styles["muted"])
    draw_text(screen, card_top, card_left + 3, title, styles["accent"])
    draw_text(screen, card_top + 2, left, "Project", styles["muted"])
    draw_text(screen, card_top + 2, left + 9, str(manager.workspace), width=width - 9, keep_end=True)

    if show_accounts:
        border = styles["accent"] if focus == "accounts" else styles["muted"]
        count_label = f"{index + 1}/{count}" if count > capacity else str(count)
        draw_panel(screen, top, left, account_width, height, f"ACCOUNTS  {count_label}", border)
        full_labels = account_width >= 46
        five_width, reset_width, week_width = (9, 10, 11) if full_labels else (5, 7, 5)
        week_col = left + account_width - week_width
        reset_col = week_col - reset_width
        five_col = reset_col - five_width
        draw_text(screen, top + 1, five_col, "5h left" if full_labels else "5h", styles["muted"])
        draw_text(screen, top + 1, reset_col, "5h reset" if full_labels else "Reset", styles["muted"])
        draw_text(screen, top + 1, week_col, "Week left" if full_labels else "Week", styles["muted"])
        if not accounts:
            draw_text(screen, top + 2, left + 3, "No accounts yet", styles["muted"])
        start = max(0, min(index - capacity // 2, count - capacity))
        now = int(time())
        for pos, name in enumerate(accounts[start:start + capacity], start):
            row = top + 2 + pos - start
            current = index == pos
            active = current and focus == "accounts"
            saved = manager.has_credentials(name)
            style = styles["selected"] if active else curses.A_BOLD if current else curses.A_NORMAL
            if active:
                draw_text(screen, row, left + 1, " " * (account_width - 2), style)
            draw_text(screen, row, left + 2, "›" if current else " ", style)
            draw_text(screen, row, left + 4, name, style, five_col - left - 5)
            status = manager.status_checks.results.get(name)
            quota_style = style if active else styles[status.style if status and saved else "muted"]
            if not saved or (status and status.label == "Signed out"):
                draw_text(screen, row, five_col, "Signed out", quota_style, 15)
            else:
                pending = name in manager.status_checks.pending
                draw_text(screen, row, five_col, quota_label(status.five_hours if status else None, pending),
                          quota_style, five_width - 1)
                draw_text(screen, row, reset_col,
                          reset_label(status.five_hours_reset_at if status else None, now, pending),
                          quota_style, reset_width - 1)
                draw_text(screen, row, week_col, quota_label(status.week if status else None, pending),
                          quota_style, week_width - 1)

    if show_actions:
        border = styles["accent"] if focus == "actions" else styles["muted"]
        draw_panel(screen, top, side, menu_width, height, "ACTIONS", border)
        start = max(0, min(action_index - capacity // 2, len(DASHBOARD_ACTIONS) - capacity))
        for pos, (_, label) in enumerate(DASHBOARD_ACTIONS[start:start + capacity], start):
            row = top + 2 + pos - start
            active = focus == "actions" and pos == action_index
            style = styles["selected"] if active else curses.A_NORMAL
            if active:
                draw_text(screen, row, side + 1, " " * (menu_width - 2), style)
            draw_text(screen, row, side + 2, ("› " if active else "  ") + label, style, menu_width - 4)

    footer_row = card_top + card_height + 1
    if update:
        draw_text(screen, footer_row - 1, left, f"Update {update} · u View", styles["accent"], width)
    enter_label = "Open" if focus == "accounts" and manager.has_credentials(accounts[index]) else (
        "Sign in" if focus == "accounts" else "Confirm"
    )
    footer = f"↑↓ Select   Enter {enter_label}" if wide else "↑↓ Select  Enter"
    if accounts:
        footer = ("Tab Switch panel   " if wide else "Tab Switch  ") + footer
    draw_text(screen, footer_row, left, footer, styles["muted"], width)
    if message:
        label, failed = message
        draw_text(screen, footer_row + 1, left, label,
                  styles["error" if failed else "accent"], width)
    elif not wide and not accounts:
        draw_text(screen, footer_row + 1, left, "No accounts yet", styles["muted"], width)


def view_update(screen, styles, tag, redraw):
    url = f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
    buttons = ("Open release", "Copy link", "Back")
    selection = 0
    notice = ""
    browser = None
    try:
        while True:
            if browser and browser.poll() is not None:
                notice = "Browser did not open. Copy the link to open it manually." if browser.returncode else ""
                browser = None
            body = [(f"Installed {VERSION}  ·  Available {tag}", styles["muted"]), (url, styles["accent"])]
            if notice:
                body.append((notice, styles["muted"]))
            redraw()
            box(screen, draw_text, styles, "MultiCodex update", body, "Tab Select   Enter Confirm", buttons, selection)
            screen.refresh()
            screen.timeout(150 if browser else -1)
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key == "\x1b" or (key in ENTER and selection == 2):
                return
            if key in (*TAB, *UP, *DOWN, curses.KEY_LEFT, curses.KEY_RIGHT):
                step = -1 if key in (curses.KEY_BTAB, *UP, curses.KEY_LEFT) else 1
                selection = (selection + step) % len(buttons)
            if key in ENTER:
                try:
                    if selection == 1:
                        copy_link(url)
                        notice = "Link sent to the terminal clipboard."
                    elif browser is None:
                        browser = open_browser(url)
                        notice = "Opening release…" if browser else "Copy the link to a device with a browser."
                except (OSError, ValueError) as exc:
                    notice = str(exc)
    finally:
        screen.timeout(-1)


def draw_dialog(screen, mode, value, error, styles, selection):
    rows, cols = screen.getmaxyx()
    if rows < 8 or cols < 16:
        return
    width = min(56, cols - 2)
    left, top = (cols - width) // 2, max(0, (rows - 8) // 2)
    for row in range(top + 1, top + 7):
        draw_text(screen, row, left, "│" + " " * (width - 2) + "│", styles["accent"])
    draw_text(screen, top, left, "╭" + "─" * (width - 2) + "╮", styles["accent"])
    draw_text(screen, top + 7, left, "╰" + "─" * (width - 2) + "╯", styles["accent"])
    inset = left + 3
    if mode == "add":
        draw_text(screen, top + 1, inset, "Add account", curses.A_BOLD)
        draw_text(screen, top + 2, inset, "Use lowercase letters, numbers, _ or -.", styles["muted"], width - 6)
        field = "  " + value + ("▏" if selection == 0 else "")
        draw_text(screen, top + 4, inset, field, styles["selected"] if selection == 0 else 0, width - 6)
        draw_text(screen, top + 5, inset, error, styles["error"], width - 6)
        draw_text(screen, top + 6, inset, "Enter Next   Tab", styles["muted"], width - 6)
        draw_text(screen, top + 6, inset + 18, " Cancel ", styles["selected"] if selection == 1 else 0,
                  width - 24)
    else:
        draw_text(screen, top + 1, inset, "Sign out", curses.A_BOLD)
        draw_text(screen, top + 3, inset, f"Sign out of {value}?", width=width - 6)
        draw_text(screen, top + 5, inset, " Cancel ", styles["selected"] if selection == 0 else 0, width - 6)
        draw_text(screen, top + 5, inset + 10, " Sign out ", styles["selected"] if selection == 1 else 0,
                  width - 16)
        draw_text(screen, top + 6, inset, "Tab Select   Enter Confirm", styles["muted"], width - 6)


def choose_session(screen, sessions):
    styles = dashboard_styles()
    index = 0
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    while True:
        screen.erase()
        rows, cols = screen.getmaxyx()
        width = min(104, cols - 4)
        left = (cols - width) // 2
        draw_text(screen, 1, left, "Resume a session", styles["accent"])
        draw_text(screen, 2, left, "Shared history in this project", styles["muted"])
        capacity = max(1, rows - 7)
        start = max(0, min(index - capacity // 2, len(sessions) - capacity))
        for pos, session in enumerate(sessions[start:start + capacity], start):
            title = session.get("name") or session.get("preview") or session["id"]
            current = pos == index
            row = 4 + pos - start
            style = styles["selected"] if current else curses.A_NORMAL
            if current:
                draw_text(screen, row, left, " " * width, style)
            draw_text(screen, row, left + 1, ("› " if current else "  ") + title, style, width - 2)
        if not sessions:
            draw_text(screen, 4, left, "No shared sessions in this project.")
            draw_text(screen, 5, left, "Start a session here or open mcdx in its original project.", styles["muted"], width)
        footer = "↑ ↓  Select     Enter  Resume     Esc  Back" if sessions else "Esc  Back"
        draw_text(screen, rows - 2, left, footer, styles["muted"], width)
        screen.refresh()
        key = screen.get_wch()
        if key in ("\x1b", "q") or (not sessions and key in ("\n", "\r")):
            return None
        if sessions and key in ("\n", "\r", curses.KEY_ENTER):
            return sessions[index]["id"]
        if sessions and key in (curses.KEY_UP, curses.KEY_DOWN, "k", "j"):
            index = (index + (1 if key in (curses.KEY_DOWN, "j") else -1)) % len(sessions)


def tui(manager):
    name = None
    message = None
    update_check = check_for_update()
    while True:
        action, name = curses.wrapper(choose, manager, name, message, update_check)
        if action == "quit":
            return 0
        try:
            code = manager.run(action, name)
            message = (f"{action.title()} failed (exit {code})" if code else f"{action.title()} complete", bool(code))
            if code or action in ("status", "build", "logout"):
                input("Press Enter to return to MultiCodex. ")
        except (OSError, ValueError) as err:
            message = (str(err), True)
        except KeyboardInterrupt:
            message = ("Cancelled", False)


def install_launcher():
    target = Path(sys.executable).resolve() if FROZEN else ROOT / "multicodex.py"
    launcher = Path.home() / ".local/bin/mcdx"
    if target == launcher:
        print(f"Already available at {launcher}")
        return 0
    launcher.parent.mkdir(parents=True, exist_ok=True)
    try:
        launcher.symlink_to(target)
    except FileExistsError as exc:
        if not launcher.is_symlink() or launcher.resolve() != target:
            raise ValueError(f"An existing launcher occupies {launcher}. Move it before installing.") from exc
    print(f"Installed {launcher}")
    if str(launcher.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Add {launcher.parent} to PATH to run mcdx from any project.")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="mcdx", description="Manage multiple Codex accounts and share local sessions.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--workspace", type=Path, default=Path(
        os.environ.get("MULTICODEX_WORKSPACE") or Path.cwd()
    ), help="Project directory (defaults to the current directory).")
    parser.add_argument("--docker", action="store_true", help="Run with Docker Compose.")
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("tui")
    commands.add_parser("build")
    commands.add_parser("install", help="Make mcdx available in ~/.local/bin.")
    import_command = commands.add_parser("import", help="Copy existing local Codex sessions into native shared history.")
    import_command.add_argument("--from", dest="source_home", type=Path, default=Path.home() / ".codex",
                                help="Codex home to import from (defaults to ~/.codex).")
    for action in (*ACCOUNT_ACTIONS, "add"):
        command = commands.add_parser(action)
        command.add_argument("account")
        if action == "resume":
            command.add_argument("thread", nargs="?")
        elif action == "login":
            command.add_argument("--browser", action="store_true", help="Use browser sign-in and a local callback.")
    args = parser.parse_args()

    if args.action == "install":
        return install_launcher()
    os.umask(0o077)
    manager = MultiCodex(args.workspace, docker=args.docker or args.action == "build")
    if args.action == "import":
        if manager.docker:
            raise ValueError("Session import is available in native mode.")
        return import_sessions(args.source_home.expanduser().resolve(), manager.shared)
    if args.action in (None, "tui"):
        return tui(manager)
    if args.action == "add":
        manager.add(args.account)
        return 0
    action = "browser-login" if getattr(args, "browser", False) else args.action
    return manager.run(action, getattr(args, "account", None), getattr(args, "thread", None))
