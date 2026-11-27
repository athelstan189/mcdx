import json
import re
from concurrent.futures import Future
from threading import Thread
from urllib.request import Request, urlopen


VERSION = "0.1.0"
REPOSITORY = "athelstan189/mcdx"
VERSION_TAG = re.compile(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)")


def latest_release():
    request = Request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest", headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"mcdx/{VERSION}",
    })
    try:
        with urlopen(request, timeout=4) as response:
            release = json.loads(response.read(1024 * 1024))
        if not isinstance(release, dict) or release.get("prerelease") or release.get("draft"):
            return None
        tag = release.get("tag_name")
        version = VERSION_TAG.fullmatch(tag) if isinstance(tag, str) else None
        if version and tuple(map(int, version.groups())) > tuple(map(int, VERSION.split("."))):
            return tag
    except (OSError, ValueError):
        pass
    return None


def check_for_update():
    result = Future()

    def check():
        result.set_result(latest_release())

    Thread(target=check, daemon=True).start()
    return result
