import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AccountStatus:
    label: str
    style: str
    five_hours: Optional[int] = None
    week: Optional[int] = None
    five_hours_reset_at: Optional[int] = None


def remaining_quota(response):
    buckets = response.get("rateLimitsByLimitId") or {}
    snapshot = buckets.get("codex")
    if snapshot is None:
        snapshot = response.get("rateLimits") or {}
        if snapshot.get("limitId") not in (None, "codex"):
            return None, None, None
    remaining = {}
    five_hours_reset_at = None
    for name in ("primary", "secondary"):
        window = snapshot.get(name)
        if not window:
            continue
        duration = window.get("windowDurationMins")
        used = window.get("usedPercent")
        if duration in (300, 7 * 24 * 60) and type(used) is int:
            remaining[duration] = max(0, min(100, 100 - used))
        if duration == 300:
            resets_at = window.get("resetsAt")
            if type(resets_at) is int and resets_at > 0:
                five_hours_reset_at = resets_at
    return remaining.get(300), remaining.get(7 * 24 * 60), five_hours_reset_at


class StatusChecks:
    def __init__(self, read_status):
        self.read_status = read_status
        self.results = {}
        self.pending = {}
        self.work = queue.Queue()
        self.updates = queue.Queue()
        self.started = False

    def refresh(self, name, *, force=False):
        if not force and (name in self.results or name in self.pending):
            return
        ticket = object()
        self.pending[name] = ticket
        self.results.pop(name, None)
        self.work.put((name, ticket))
        if not self.started:
            threading.Thread(target=self.collect, daemon=True).start()
            self.started = True

    def forget(self, name):
        self.results.pop(name, None)
        self.pending.pop(name, None)

    def poll(self):
        while True:
            try:
                name, ticket, status = self.updates.get_nowait()
            except queue.Empty:
                return
            if self.pending.get(name) is ticket:
                self.results[name] = status
                del self.pending[name]

    def collect(self):
        while True:
            name, ticket = self.work.get()
            if self.pending.get(name) is not ticket:
                continue
            try:
                status = self.read_status(name)
            except subprocess.TimeoutExpired:
                status = AccountStatus("Timed out", "error")
            except (OSError, ValueError):
                status = AccountStatus("Check failed", "error")
            self.updates.put((name, ticket, status))
