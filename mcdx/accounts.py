import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
NAME_FILE = ".mcdx-name"


@dataclass(frozen=True)
class Account:
    name: str
    home: Path


def validate_name(name):
    if not NAME.fullmatch(name):
        raise ValueError("Start with a letter or digit. Use 1–32 lowercase letters, digits, _ or -.")


class AccountStore:
    def __init__(self, directory):
        self.directory = directory

    def accounts(self):
        with self._locked(fcntl.LOCK_SH):
            return self._read_accounts()

    def _read_accounts(self):
        accounts = {}
        for home in self.directory.iterdir():
            if not NAME.fullmatch(home.name) or home.is_symlink() or not home.is_dir():
                continue
            try:
                name = (home / NAME_FILE).read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                name = home.name
            validate_name(name)
            if name in accounts:
                raise ValueError(f"Duplicate account name: {name}")
            accounts[name] = Account(name, home)
        return accounts

    @contextmanager
    def _locked(self, mode):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, mode)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def editing(self):
        # Serialize directory edits without locking any account's Codex processes.
        with self._locked(fcntl.LOCK_EX):
            yield self._read_accounts()

    def add(self, name, prepare):
        validate_name(name)
        with self.editing() as accounts:
            existing = accounts.get(name)
            if existing:
                prepare(existing.home)
                return existing
            home = self.directory / name
            if home.is_symlink() or (home.exists() and not home.is_dir()):
                raise ValueError("The account path is occupied.")
            if home.exists():
                home = self.directory / uuid4().hex
            with tempfile.TemporaryDirectory(prefix=".add-", dir=self.directory) as folder:
                staged = Path(folder) / "account"
                prepare(staged)
                if name != home.name:
                    (staged / NAME_FILE).write_text(name + "\n", encoding="utf-8")
                staged.rename(home)
            return Account(name, home)

    def rename(self, account, name):
        validate_name(name)
        with self.editing() as accounts:
            if accounts.get(account.name) != account:
                raise ValueError("The account name changed in another window. Reopen MultiCodex.")
            if name == account.name:
                return account
            if name in accounts:
                raise ValueError("That name is already in use.")
            with tempfile.TemporaryDirectory(prefix=".rename-", dir=account.home) as folder:
                staged = Path(folder) / NAME_FILE
                staged.write_text(name + "\n", encoding="utf-8")
                staged.replace(account.home / NAME_FILE)
            return Account(name, account.home)
