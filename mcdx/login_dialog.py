import base64
import curses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import uuid
from urllib.parse import parse_qs, urlsplit

from .native_runtime import process_env


ENTER = ("\n", "\r", curses.KEY_ENTER)
TAB = ("\t", curses.KEY_BTAB)
UP = (curses.KEY_UP, "k")
DOWN = (curses.KEY_DOWN, "j")


class LoginClient:
    def __init__(self, process, method, errors):
        self.process = process
        self.method = method
        self.errors = errors
        self.buffer = bytearray()
        self.login_id = None
        self.url = ""
        self.code = ""
        self.done = False
        self.error = None
        self.completions = {}
        self.eof = False
        os.set_blocking(process.stdout.fileno(), False)
        self.send("initialize", {"clientInfo": {"name": "multicodex", "version": "0.1.0"}}, 1)

    def send(self, method, params=None, request_id=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        if request_id is not None:
            message["id"] = request_id
        self.process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def update(self):
        if self.done:
            return
        try:
            data = os.read(self.process.stdout.fileno(), 65536) if not self.eof else b""
        except BlockingIOError:
            return
        if not data:
            self.eof = True
            if self.process.poll() is not None:
                self.errors.seek(0, os.SEEK_END)
                self.errors.seek(max(0, self.errors.tell() - 2048))
                lines = self.errors.read().decode("utf-8", errors="replace").splitlines()
                self.error = next((line.strip() for line in reversed(lines) if line.strip()),
                                  "Codex closed the sign-in service.")
                self.done = True
            return

        self.buffer.extend(data)
        lines = self.buffer.split(b"\n")
        self.buffer = lines.pop()
        for line in lines:
            if line.strip():
                self.handle(json.loads(line))
            if self.done:
                break

    def handle(self, message):
        request_id = message.get("id")
        if request_id in (1, 2) and "error" in message:
            self.done = True
            self.error = message["error"]["message"]
        elif request_id == 1 and "result" in message:
            self.send("initialized")
            self.send("account/login/start", {"type": self.method}, 2)
        elif request_id == 2 and "result" in message:
            result = message["result"]
            self.login_id = result["loginId"]
            self.url = result.get("verificationUrl") or result.get("authUrl", "")
            self.code = result.get("userCode", "")
            if self.login_id in self.completions:
                self.complete(self.completions[self.login_id])
        elif message.get("method") == "account/login/completed":
            params = message["params"]
            login_id = params.get("loginId")
            if login_id and not self.login_id:
                self.completions[login_id] = params
            elif login_id and login_id == self.login_id:
                self.complete(params)

    def complete(self, params):
        self.done = True
        if not params.get("success"):
            self.error = params.get("error") or "Sign-in did not complete."


def box(screen, draw_text, styles, heading, body, footer, buttons=(), selected=0):
    rows, cols = screen.getmaxyx()
    if rows < 8 or cols < 16:
        draw_text(screen, 0, 0, "Enlarge terminal", styles["accent"])
        draw_text(screen, 1, 0, "Esc Back", styles["muted"])
        return
    width = min(82, cols - 2)
    content_width = width - 6
    lines = [(fragment, style) for line, style in body
             for fragment in (textwrap.wrap(line, content_width, break_on_hyphens=False) or [""])]
    foot = textwrap.wrap(footer, content_width, break_on_hyphens=False)
    button_rows = int(bool(buttons))
    available = max(0, rows - 5 - len(foot) - button_rows)
    if len(lines) > available and available:
        lines = lines[:available - 1] + [("Enlarge terminal for more details.", styles["muted"])]
    height = min(rows, 5 + len(lines) + len(foot) + button_rows)
    left, top = (cols - width) // 2, (rows - height) // 2
    border = styles["accent"]
    draw_text(screen, top, left, "╭" + "─" * (width - 2) + "╮", border)
    for row in range(top + 1, top + height - 1):
        draw_text(screen, row, left, "│" + " " * (width - 2) + "│", border)
    draw_text(screen, top + height - 1, left, "╰" + "─" * (width - 2) + "╯", border)
    draw_text(screen, top + 1, left + 3, heading, curses.A_BOLD, content_width)
    for row, (line, style) in enumerate(lines[:available], top + 3):
        draw_text(screen, row, left + 3, line, style, content_width)
    footer_start = top + height - len(foot) - 1
    if buttons and footer_start > top + 2:
        col = left + 3
        visible = list(enumerate(buttons))
        if sum(len(label) + 4 for label in buttons) - 2 > content_width:
            visible = [(selected, buttons[selected])]
        for pos, label in visible:
            draw_text(screen, footer_start - 1, col, f" {label} ",
                      styles["selected"] if pos == selected else 0, left + width - 3 - col)
            col += len(label) + 4
    for row, line in enumerate(foot, footer_start):
        if row > top + 1:
            draw_text(screen, row, left + 3, line, border, content_width)


def valid_url(url):
    try:
        parsed = urlsplit(url)
        return parsed.scheme in ("https", "http") and bool(parsed.hostname)
    except ValueError:
        return False


def callback_port(url):
    try:
        redirect = parse_qs(urlsplit(url).query).get("redirect_uri", [""])[0]
        parsed = urlsplit(redirect)
        if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            return parsed.port
    except ValueError:
        pass
    return None


def open_browser(url):
    if not valid_url(url):
        return None
    browser = shutil.which("xdg-open") or (shutil.which("open") if sys.platform == "darwin" else None)
    if browser:
        return subprocess.Popen([browser, url], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=process_env())
    return None


def copy_link(url):
    if valid_url(url):
        encoded = base64.b64encode(url.encode("utf-8"))
        os.write(sys.stdout.fileno(), b"\x1b]52;c;" + encoded + b"\x1b\\")


def stop_login(process, client, container, env):
    if process.poll() is not None:
        return
    try:
        if client and client.login_id and not client.done:
            client.send("account/login/cancel", {"loginId": client.login_id}, 3)
        process.stdin.close()
    except BrokenPipeError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if container:
        try:
            subprocess.run(["docker", "container", "stop", "--time", "1", container],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env=env, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass


def wait_for_login(screen, manager, name, method, render, styles):
    container = f"mcdx-login-{uuid.uuid4().hex}" if manager.docker else None
    command, options = manager.app_server_invocation(name, container)
    with tempfile.TemporaryFile() as errors, subprocess.Popen(
        command, **options, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
    ) as process:
        client = None
        try:
            client = LoginClient(process, method, errors)
            helper = None
            notice = ""
            action_index = 0
            screen.timeout(100)
            while True:
                client.update()
                if helper and helper.poll() is not None:
                    if helper.returncode:
                        notice = "Browser did not open. Copy the link and open it manually."
                    helper = None
                if client.done:
                    return ("failed", client.error) if client.error else ("success", None)
                if client.url:
                    body = [("Open the authorization link in your browser.", styles["muted"])]
                    if client.code:
                        body = [("Enter this device code in your browser", styles["muted"]),
                                (client.code, styles["accent"]), *body]
                    elif port := callback_port(client.url):
                        body.append((f"Remote SSH login requires forwarding localhost port {port}.", styles["muted"]))
                    body += [(client.url, styles["accent"]),
                             (notice or "Waiting for authorization…", styles["muted"])]
                    buttons = ("Open browser", "Copy link", "Cancel")
                    footer = "Tab Select   Enter Confirm"
                else:
                    body = [("Contacting ChatGPT…", styles["muted"])]
                    buttons = ("Cancel",)
                    footer = "Enter Cancel"
                render(body, footer, buttons, action_index)
                try:
                    key = screen.get_wch()
                except curses.error:
                    continue
                if key in (*TAB, *UP, *DOWN, curses.KEY_LEFT, curses.KEY_RIGHT):
                    step = -1 if key in (curses.KEY_BTAB, *UP, curses.KEY_LEFT) else 1
                    action_index = (action_index + step) % len(buttons)
                    continue
                action = buttons[action_index] if key in ENTER else {
                    "\x1b": "Cancel", "c": "Copy link", "o": "Open browser",
                }.get(key)
                if action == "Cancel":
                    return "cancelled", None
                if client.url and action == "Copy link":
                    try:
                        copy_link(client.url)
                        notice = "Link sent to the terminal clipboard."
                    except (OSError, ValueError) as exc:
                        notice = str(exc)
                if client.url and action == "Open browser" and helper is None:
                    try:
                        helper = open_browser(client.url)
                        if helper is None:
                            notice = "Open the link on a device with a browser."
                    except (OSError, ValueError) as exc:
                        notice = str(exc)
        finally:
            screen.timeout(-1)
            stop_login(process, client, container, options["env"])


def sign_in(screen, manager, accounts, index, styles, dashboard, draw_text, *, browser=False):
    name = accounts[index]
    methods = [("Device code", "Authorize from any browser", "chatgptDeviceCode")]
    if not manager.docker:
        methods.append(("Browser sign-in", "Authorize with a browser callback", "chatgpt"))
    selection = 0
    result = None
    error = None
    heading = f"Sign in  ·  {name}"

    def render(body, footer, buttons=(), selected=0):
        dashboard(screen, manager, accounts, index, None, styles)
        box(screen, draw_text, styles, heading, body, footer, buttons, selected)
        screen.refresh()

    try:
        while True:
            if browser:
                selection = 1
            elif result == "success":
                render([("✓ Signed in", styles["good"])], "Enter Confirm", ("Done",))
                if screen.get_wch() in (*ENTER, "\x1b"):
                    manager.status_checks.refresh(name, force=True)
                    return (f"Signed in as {name}", False)
                continue
            else:
                body = [("Choose a sign-in method", styles["muted"])]
                body += [
                    (f"{'›' if pos == selection else ' '} {label}  ·  {detail}",
                     styles["selected"] if pos == selection else 0)
                    for pos, (label, detail, _) in enumerate(methods)
                ]
                if error:
                    body.append((error, styles["error"]))
                render(body, "↑↓ / Tab Select   Enter Continue   Esc Back")
                key = screen.get_wch()
                if key == "\x1b":
                    return (f"Sign-in failed for {name}" if error else "Sign-in cancelled", bool(error))
                if key in (*UP, *DOWN, *TAB):
                    selection = (selection + (-1 if key in (*UP, curses.KEY_BTAB) else 1)) % len(methods)
                if key not in ENTER:
                    continue
            browser = False
            try:
                result, error = wait_for_login(screen, manager, name, methods[selection][2], render, styles)
            except (OSError, ValueError, KeyError) as exc:
                result, error = "failed", str(exc)
            if result == "cancelled":
                return ("Sign-in cancelled", False)
    except KeyboardInterrupt:
        return ("Sign-in cancelled", False)
