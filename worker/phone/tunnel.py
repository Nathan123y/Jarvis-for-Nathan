"""A public https address for the phone line, through a Cloudflare quick tunnel.

`cloudflared tunnel --url http://127.0.0.1:PORT` prints a random
https://<words>.trycloudflare.com address that forwards to this Mac. No account
or domain is needed; the address changes each time the tunnel starts, which is
why the service re-points the Twilio number at every start.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# The tunnel's own address; "api.trycloudflare.com" also appears in its logs and is not it.
_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
BIN = Path(__file__).resolve().parents[2] / "config" / "bin" / "cloudflared"


def binary() -> Optional[str]:
    if BIN.exists():
        return str(BIN)
    return shutil.which("cloudflared")


def find_url(line: str) -> str:
    m = _URL.search(line or "")
    return m.group(0) if m else ""


class Tunnel:
    """Keeps one quick tunnel running and reports each new address."""

    def __init__(self, port: int, on_url: Callable[[str], None], log: Callable[[str], None] = print,
                 popen=subprocess.Popen):
        self.port, self.on_url, self.log, self.popen = port, on_url, log, popen
        self.url = ""
        self._proc = None
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._keep_running, daemon=True, name="phone-tunnel").start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def _keep_running(self) -> None:
        delay = 5.0
        while not self._stop.is_set():
            exe = binary()
            if not exe:
                self.log("[Phone] cloudflared isn't installed; the phone line can't be reached.")
                return
            started = time.monotonic()
            self._run_once(exe)
            if self._stop.is_set():
                return
            # A tunnel that ran a while and dropped restarts quickly; one that keeps failing backs off.
            delay = 5.0 if time.monotonic() - started > 300 else min(delay * 2, 300.0)
            self.log(f"[Phone] tunnel stopped; restarting in {delay:.0f}s")
            self._stop.wait(delay)

    def _run_once(self, exe: str) -> None:
        self._proc = self.popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in self._proc.stdout:
            url = find_url(line)
            if url and url != self.url:
                self.url = url
                try:
                    self.on_url(url)
                except Exception as exc:
                    self.log(f"[Phone] couldn't use the new tunnel address: {exc!r}")
            if self._stop.is_set():
                break
        self._proc.wait()
