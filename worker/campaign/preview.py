"""Where the preview sites live so an owner can open them from their own phone.

A preview must work from any device, without this Mac's localhost or the private Jarvis dashboard.
The $0 option implemented here is GitHub Pages (free static hosting) fed from a dedicated public
repository that you create once; Jarvis then only pushes folders into it with the git login already on
this Mac. Be clear about what that means:

  * "Unlisted" means unguessable: each preview sits at a random 20-character path, there is no index
    page, robots.txt disallows everything, and every page is noindex. Anyone who is given the link can
    open it, and a public repository's files are visible to anyone who browses the repository itself.
    Nothing private (customer records, credentials, dashboard) is ever written there.
  * Previews are served by GitHub, not by this Mac, so they stay up while the Mac sleeps or is off.
    The *pushing* needs the Mac (and so the worker) to be awake.
  * Previews are removed after their time limit (default 30 days) or on request: the folder is deleted
    and pushed.

Until a repository is configured and a test preview has been verified reachable from the public
internet, hosting reports "local only" and outreach stays blocked.
"""
from __future__ import annotations

import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

ROBOTS = "User-agent: *\nDisallow: /\n"
README = ("Independent website concept previews. Unlisted, not indexed. "
          "To remove one, reply to the email that contained its link.\n")


def new_slug() -> str:
    return secrets.token_urlsafe(15).replace("_", "x").replace("-", "y")[:20].lower()


class PreviewError(Exception):
    pass


class LocalHost:
    """Writes previews under config/worker/previews and reports that they are NOT reachable by others."""
    public = False

    def __init__(self, root: Path):
        self.root = Path(root)

    def summary(self) -> str:
        return "none configured: previews are only saved on this Mac, so outreach is blocked"

    def publish(self, slug: str, files: dict) -> str:
        d = self.root / slug
        d.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (d / name).write_text(content, encoding="utf-8")
        return (d / "index.html").resolve().as_uri()

    def remove(self, slug: str) -> None:
        shutil.rmtree(self.root / slug, ignore_errors=True)

    def expire(self, now: float, ttl_days: float) -> list[str]:
        return []

    def verify(self, url: str, slug: str) -> tuple[bool, str]:
        return False, "local file: not reachable from another device"


class GitPagesHost:
    """Static previews in a git repository served by GitHub Pages (or any git-backed static host)."""
    public = True

    def __init__(self, workdir: Path, remote_url: str, base_url: str, *,
                 run: Optional[Callable[[list[str], Path], subprocess.CompletedProcess]] = None,
                 fetch: Optional[Callable[[str], tuple[int, str]]] = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.dir, self.remote, self.base = Path(workdir), remote_url, base_url.rstrip("/")
        self._run = run or self._git
        self._fetch = fetch or self._http
        self._clock, self._sleep = clock, sleep

    def summary(self) -> str:
        return (f"{self.base} (free GitHub Pages, served by GitHub so previews stay up while this Mac sleeps; "
                "random unlisted paths, noindex, removed after their time limit)")

    @staticmethod
    def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120)

    def _g(self, *args: str) -> str:
        r = self._run(list(args), self.dir)
        if r.returncode != 0:
            raise PreviewError(f"git {args[0]} failed: {(r.stderr or r.stdout or '').strip()[:200]}")
        return r.stdout or ""

    def _ready(self) -> None:
        if not (self.dir / ".git").exists():
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            r = self._run(["clone", self.remote, str(self.dir)], self.dir.parent)
            if r.returncode != 0:
                raise PreviewError(f"could not clone the preview repository: {(r.stderr or '').strip()[:200]}")
        else:
            self._g("pull", "--rebase", "--autostash")
        for name, content in (("robots.txt", ROBOTS), ("README.md", README), (".nojekyll", ""), ("404.html", "<!doctype html><title>Not found</title>")):
            p = self.dir / name
            if not p.exists():
                p.write_text(content)

    def _push(self, message: str) -> None:
        self._g("add", "-A")
        if not self._g("status", "--porcelain").strip():
            return
        self._g("-c", "user.name=Jarvis previews", "-c", "user.email=previews@users.noreply.github.com", "commit", "-m", message)
        try:
            self._g("push")
        except PreviewError:
            self._g("pull", "--rebase", "--autostash")
            self._g("push")

    def publish(self, slug: str, files: dict) -> str:
        if not slug or "/" in slug or slug.startswith("."):
            raise PreviewError("bad slug")
        self._ready()
        d = self.dir / slug
        d.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            if "/" in name or name.startswith("."):
                raise PreviewError("bad file name")
            (d / name).write_text(content, encoding="utf-8")
        (d / ".published").write_text(str(int(self._clock())))
        self._push(f"preview {slug}")
        return f"{self.base}/{slug}/"

    def remove(self, slug: str) -> None:
        self._ready()
        shutil.rmtree(self.dir / slug, ignore_errors=True)
        self._push(f"remove {slug}")

    def expire(self, now: float, ttl_days: float) -> list[str]:
        """Delete previews older than the time limit. Returns the slugs removed."""
        self._ready()
        gone = []
        for d in self.dir.iterdir():
            marker = d / ".published"
            if d.is_dir() and not d.name.startswith(".") and marker.exists():
                try:
                    if now - float(marker.read_text() or 0) > ttl_days * 86400:
                        shutil.rmtree(d)
                        gone.append(d.name)
                except (OSError, ValueError):
                    continue
        if gone:
            self._push(f"expire {len(gone)} previews")
        return gone

    @staticmethod
    def _http(url: str) -> tuple[int, str]:
        req = urllib.request.Request(url, headers={"User-Agent": "JarvisPreviewCheck/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read(400_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, ""
        except (urllib.error.URLError, OSError):
            return 0, ""

    def verify(self, url: str, slug: str, *, attempts: int = 8, wait: float = 15.0) -> tuple[bool, str]:
        """Fetch the public URL the way an owner's phone would. GitHub Pages can take a minute or two."""
        last = "no response"
        for i in range(attempts):
            status, body = self._fetch(url)
            if status == 200 and "concept preview" in body and "noindex" in body:
                return True, "reachable from the public internet"
            last = f"HTTP {status}" if status else "no response"
            if i < attempts - 1:
                self._sleep(wait)
        return False, f"not reachable yet ({last})"


def from_settings(cfg: dict, root: Path):
    """The configured host, or LocalHost (outreach blocked) when no repository is set."""
    repo, base = (cfg.get("preview_repo_url") or "").strip(), (cfg.get("preview_base_url") or "").strip()
    if repo and base.startswith("https://"):
        return GitPagesHost(root / "previews-repo", repo, base)
    return LocalHost(root / "previews")
