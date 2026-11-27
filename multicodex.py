#!/usr/bin/env python3
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

from native_runtime import NativeRuntime, import_sessions, locked, process_env

ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ACCOUNT_ACTIONS = ("open", "resume", "login", "status", "logout")
PROJECT_ACTIONS = ("open", "resume")
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

    def run(self, action, name=None, thread=None):
        if self.docker and action == "browser-login":
            raise ValueError("Browser sign-in is available in native mode. Use device sign-in for Docker.")
        home = self.accounts_dir
        if action != "build":
            if name not in self.accounts():
                raise ValueError(f"Unknown account: {name}")
            home /= name
            self.prepare_account(home)

        if not self.docker:
            runtime = NativeRuntime(ROOT, self.shared, home, self.workspace)
            if action in PROJECT_ACTIONS:
                self.workspace.mkdir(parents=True, exist_ok=True)
            if action == "resume" and not thread:
                thread = curses.wrapper(choose_session, runtime.sessions())
                if thread is None:
                    return 0
            return runtime.run(action, thread)

        command = [
            "docker", "compose", "--env-file", "/dev/null",
            "-f", str(ROOT / "compose.yaml"),
        ]
        mount = self.project_mount() if action in PROJECT_ACTIONS else None
        if action == "build":
            command += ["build", "codex"]
        else:
            command += ["run", "--rm", "--no-deps"]
            if action in ("status", "logout"):
                command += ["-T"]
            command += ["project" if mount else "codex", action]
            if thread:
                command.append(thread)
        return subprocess.run(command, env={
            **process_env(),
            "MC_HOME": str(home),
            "MC_SHARED": str(self.shared),
            "MC_WORKSPACE": str(self.workspace),
            "MC_PROJECT_DIR": mount or "/workspace",
            "MC_UID": str(os.getuid()),
            "MC_GID": str(os.getgid()),
        }, check=False).returncode


def choose(screen, manager, selected, message):
    accounts = manager.accounts()
    index = accounts.index(selected) if selected in accounts else 0
    styles = dashboard_styles()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    dialog = None
    alias = error = ""
    shortcuts = {"r": "resume", "l": "login", "s": "status", "x": "logout"}
    if not manager.docker:
        shortcuts["L"] = "browser-login"
    enter = ("\n", "\r", curses.KEY_ENTER)
    while True:
        name = accounts[index] if accounts else None
        draw_dashboard(screen, manager, accounts, index, message, styles)
        if dialog:
            draw_dialog(screen, dialog, alias if dialog == "add" else name, error, styles)
        screen.refresh()
        try:
            key = screen.get_wch()
        except KeyboardInterrupt:
            if not dialog:
                raise
            dialog = None
            continue

        if dialog == "add":
            if key == "\x1b" or (key in enter and not alias):
                dialog = None
            elif key in enter:
                try:
                    manager.add(alias)
                except (OSError, ValueError) as exc:
                    error = str(exc)
                else:
                    accounts = manager.accounts()
                    index = accounts.index(alias)
                    return "login", alias
            elif key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                alias, error = alias[:-1], ""
            elif isinstance(key, str) and key in "abcdefghijklmnopqrstuvwxyz0123456789_-" and len(alias) < 32:
                alias, error = alias + key, ""
            continue

        if dialog == "logout":
            if key in ("y", "Y"):
                return "logout", name
            if key in ("n", "N", "\x1b", *enter):
                dialog = None
            continue

        if key in ("q", "\x1b"):
            return "quit", name
        if key == "a":
            dialog, alias, error = "add", "", ""
        if key == "b" and manager.docker:
            return "build", name
        if not accounts:
            if key in enter:
                dialog, alias, error = "add", "", ""
            continue
        if key in enter:
            return "open", name
        if key == "x":
            dialog = "logout"
        elif key in shortcuts:
            return shortcuts[key], name
        elif key in (curses.KEY_UP, curses.KEY_DOWN, "k", "j"):
            index = (index + (1 if key in (curses.KEY_DOWN, "j") else -1)) % len(accounts)


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


def draw_dashboard(screen, manager, accounts, index, message, styles):
    screen.erase()
    rows, cols = screen.getmaxyx()
    width = min(104, cols - 4)
    left = (cols - width) // 2
    if rows < 13 or cols < 32:
        draw_text(screen, 0, 0, "MultiCodex", styles["accent"])
        draw_text(screen, 1, 0, "Enlarge terminal · q Quit", styles["muted"])
        return

    draw_text(screen, 1, left, "MultiCodex", styles["accent"])
    if manager.docker:
        draw_text(screen, 1, left + 12, "Docker", styles["muted"])
    draw_text(screen, 2, left, "Project", styles["muted"])
    draw_text(screen, 2, left + 9, str(manager.workspace), width=width - 9, keep_end=True)
    draw_text(screen, 4, left, "─" * width, styles["muted"])

    wide = width >= 70 and rows >= 17
    panel = width - 30 if wide else width
    draw_text(screen, 5, left, "ACCOUNTS", curses.A_BOLD)
    capacity = max(1, rows - (10 if wide else 12))
    count = len(accounts)
    position = f"{index + 1} / {count}" if count > capacity else str(count)
    draw_text(screen, 5, left + 10, position, styles["muted"])
    start = max(0, min(index - capacity // 2, count - capacity))
    for offset, name in enumerate(accounts[start:start + capacity]):
        row = 7 + offset
        current = index == start + offset
        saved = manager.has_credentials(name)
        state = "● Key saved" if saved else "○ Signed out"
        style = styles["selected"] if current else curses.A_NORMAL
        if current:
            draw_text(screen, row, left, " " * panel, style)
        draw_text(screen, row, left + 1, "›" if current else " ", style)
        draw_text(screen, row, left + 3, name, style, panel - 18)
        draw_text(screen, row, left + panel - 14, state,
                  style if current else styles["good" if saved else "muted"])
    if not accounts:
        draw_text(screen, 7, left + 1, "No accounts yet", curses.A_BOLD)
        if capacity > 1 and not wide:
            draw_text(screen, 8, left + 1, "Press Enter to sign in.", styles["muted"])

    if wide:
        side = left + panel + 4
        draw_text(screen, 5, side, "ACTIONS", curses.A_BOLD)
        actions = (("a" if accounts else "Enter", "Add & sign in"),)
        if manager.docker:
            actions += (("b", "Build image"),)
        if accounts:
            login_hint = ("l", "Sign in") if manager.docker else ("l / L", "Device / browser")
            actions = (("Enter", "Open Codex"), ("r", "Resume here"), login_hint,
                       ("s", "Check login"), ("x", "Sign out"), *actions)
        for row, (key, label) in enumerate(actions, 7):
            draw_text(screen, row, side, key, styles["accent"])
            draw_text(screen, row, side + 8, label)
        footer = "↑ ↓  Select account     q  Quit" if accounts else "q  Quit"
        draw_text(screen, rows - 2, left, footer, styles["muted"])
    else:
        login_keys = "l Login" if manager.docker else "l/L Login"
        hints = (f"↵ Open  r Resume  {login_keys}", "s Status  x Logout  a Add",
                 "↑↓ Select   b Build   q Quit" if manager.docker else "↑↓ Select   q Quit")
        if not accounts:
            hints = ("↵ Add & sign in", "b Build   q Quit" if manager.docker else "q Quit")
        for row, hint in enumerate(hints, rows - 4):
            draw_text(screen, row, left, hint, styles["muted"], width)
    if message:
        label, failed = message
        draw_text(screen, rows - (3 if wide else 5), left, label,
                  styles["error" if failed else "accent"], width)


def draw_dialog(screen, mode, value, error, styles):
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
        draw_text(screen, top + 4, inset, "  " + value + "▏", styles["selected"], width - 6)
        draw_text(screen, top + 5, inset, error, styles["error"], width - 6)
        draw_text(screen, top + 6, inset, "Enter Sign in     Esc Cancel", styles["accent"], width - 6)
    else:
        draw_text(screen, top + 1, inset, "Sign out", curses.A_BOLD)
        draw_text(screen, top + 3, inset, f"Sign out of {value}?", width=width - 6)
        draw_text(screen, top + 6, inset, "Y Sign out     Esc Cancel", styles["accent"], width - 6)


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
    while True:
        action, name = curses.wrapper(choose, manager, name, message)
        if action == "quit":
            return 0
        try:
            code = manager.run(action, name)
            label = "Browser sign-in" if action == "browser-login" else action.title()
            message = (f"{label} failed (exit {code})" if code else f"{label} complete", bool(code))
            if code and action == "login" and not manager.docker:
                message = ("Device sign-in failed. Press l to retry or L for browser sign-in.", True)
            if code or action in ("status", "build", "login", "browser-login", "logout"):
                input("Press Enter to return to MultiCodex. ")
        except (OSError, ValueError) as err:
            message = (str(err), True)
        except KeyboardInterrupt:
            message = ("Cancelled", False)


def install_launcher():
    target = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
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


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, EOFError, curses.error) as err:
        raise SystemExit(str(err))
    except KeyboardInterrupt:
        raise SystemExit(130)
