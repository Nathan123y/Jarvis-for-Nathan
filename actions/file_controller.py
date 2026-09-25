import os
import shutil
import platform
import heapq
import re
import subprocess
import time
import zipfile
from pathlib import Path
from datetime import datetime
from xml.etree import ElementTree as ET

try:
    import send2trash
    _SEND2TRASH = True
except ImportError:
    _SEND2TRASH = False

from core.undo import push_undo

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

# Undo keeps a file's previous contents in memory so `write` can be reversed.
# Above this size it does not — a 200 MB log would sit in RAM for the rest of
# the session to protect an edit nobody is going to take back.
_UNDO_CONTENT_LIMIT = 1_000_000
_MAX_READ_CHARS = 80_000
_MAX_SEARCH_FILE = 4 * 1024 * 1024
_MAX_SEARCH_SECONDS = 15.0
_MAX_RESULTS = 100

_SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".Trash",
    ".cache", "Caches", "DerivedData", ".venv", "venv",
}
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".html", ".htm", ".css",
    ".scss", ".swift", ".c", ".h", ".cpp", ".hpp", ".java", ".kt", ".sh",
    ".zsh", ".fish", ".sql", ".tex", ".m", ".r", ".rb", ".go", ".rs",
}
_SENSITIVE_NAMES = {
    ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "id_rsa", "id_ed25519", "api_keys.json", "credentials.json",
}
_SENSITIVE_PARTS = {".ssh", "keychains", "cookies", "login data", "passwords"}


def _permission_denied(path: str) -> str:
    if _OS != "Darwin":
        return f"Permission denied: {path}"
    target = Path(path).expanduser()
    home = Path.home()
    for folder in ("Desktop", "Documents", "Downloads"):
        if target == home / folder or (home / folder) in target.parents:
            return (
                f"Permission denied: {path}. Check System Settings → Privacy & "
                f"Security → Files & Folders → Jarvis (or Python) → {folder}, "
                "then quit and reopen Jarvis."
            )
    return (
        f"Permission denied: {path}. Check the folder's access rights in Finder. "
        "If macOS protects this location, check System Settings → Privacy & "
        "Security → Files & Folders for Jarvis (or Python)."
    )


def _undo_move(src: Path, dst: Path):
    """Reverse of a move: put it back where it came from."""
    def _fn():
        if not dst.exists():
            return f"'{dst.name}' is no longer there — nothing moved back."
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
        return f"'{src.name}' is back in {src.parent.name}/."
    return _fn


def _undo_create(target: Path):
    """Reverse of a create: remove what we made — and only if we still made it.

    Deliberately refuses to touch a directory that has since been filled: the
    undo for 'create a folder' is not 'delete whatever ended up in it'."""
    def _fn():
        if not target.exists():
            return f"'{target.name}' is already gone."
        if target.is_dir():
            if any(target.iterdir()):
                return (f"'{target.name}' is not empty any more — "
                        f"leaving it alone rather than deleting your files.")
            target.rmdir()
        else:
            target.unlink()
        return f"Removed '{target.name}'."
    return _fn


def _undo_write(target: Path, previous: str | None):
    """Reverse of a write: restore the old contents, or remove a file that did
    not exist before the write created it."""
    def _fn():
        if previous is None:
            if target.exists():
                target.unlink()
                return f"Removed '{target.name}' — it did not exist before."
            return f"'{target.name}' is already gone."
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(previous, encoding="utf-8")
        return f"Restored the previous contents of '{target.name}'."
    return _fn


def _restore_from_trash(original: Path) -> str:
    """Best-effort undelete.

    delete_file uses send2trash, which is the right call: the file lands in the
    Recycle Bin / Trash where the person can also find it themselves. Getting it
    back out again is shell work and only reliable on Windows, where pywin32 is
    already a dependency. Everywhere else this says where the file is instead of
    pretending it failed — the file is not lost either way."""
    if _OS == "Windows":
        try:
            import win32com.client
            shell = win32com.client.Dispatch("Shell.Application")
            bin_folder = shell.NameSpace(10)      # ssfBITBUCKET
            for item in bin_folder.Items():
                if str(bin_folder.GetDetailsOf(item, 1)).strip().lower() == \
                        str(original.parent).strip().lower():
                    if str(item.Name).strip().lower() == original.name.strip().lower():
                        item.InvokeVerb("UNDELETE")
                        return f"'{original.name}' restored from the Recycle Bin."
        except Exception as e:
            print(f"[file] Recycle Bin restore failed: {e}")
    return (f"'{original.name}' is in the Recycle Bin — I could not pull it back "
            f"automatically, but it is there and can be restored by hand.")


_SAFE_ROOTS: list[Path] = [
    Path.home(),
]

if _OS == "Darwin":
    # External drives are user-controlled data too. The volume root itself is
    # still protected below; files and folders inside a mounted drive may be
    # changed when macOS permissions allow it.
    _SAFE_ROOTS.append(Path("/Volumes"))


def _is_sensitive(target: Path) -> bool:
    parts = {part.casefold() for part in target.parts}
    name = target.name.casefold()
    return (name in _SENSITIVE_NAMES
            or any(part in parts for part in _SENSITIVE_PARTS)
            or name.endswith((".pem", ".key", ".p12", ".pfx")))


def _is_readable_path(target: Path) -> bool:
    """Full Disk Access decides reach; this prevents credential exfiltration.

    Listing a folder is allowed even if it contains a protected credential
    file, but reading or content-searching that file is not.
    """
    try:
        return target.expanduser().resolve().is_absolute()
    except Exception:
        return False


def _can_return_contents(target: Path) -> bool:
    return _is_readable_path(target) and not _is_sensitive(target)


def _protected_root(target: Path) -> bool:
    try:
        resolved = target.resolve()
        roots = {root.resolve() for root in _SAFE_ROOTS}
        protected = {
            Path.home().resolve(), _get_desktop().resolve(), _get_downloads().resolve(),
            _get_documents().resolve(), _get_pictures().resolve(), _get_music().resolve(),
            _get_videos().resolve(),
        }
        if _OS == "Darwin":
            protected.add(Path("/Volumes").resolve())
        return resolved in roots or resolved in protected
    except Exception:
        return True

def _is_safe_path(target: Path) -> bool:
    """Is the given path inside _SAFE_ROOTS? If not, reject the operation."""
    try:
        resolved = target.resolve()
        return any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in _SAFE_ROOTS
        )
    except Exception:
        return False

def _get_desktop() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DESKTOP_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Desktop"

def _get_downloads() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOWNLOAD_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Downloads"

def _get_documents() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOCUMENTS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Documents"

def _get_pictures() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_PICTURES_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Pictures"

def _get_music() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_MUSIC_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Music"

def _get_videos() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_VIDEOS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Videos"


def _resolve_path(raw: str) -> Path:
    shortcuts: dict[str, Path] = {
        "desktop":   _get_desktop(),
        "downloads": _get_downloads(),
        "documents": _get_documents(),
        "pictures":  _get_pictures(),
        "music":     _get_music(),
        "videos":    _get_videos(),
        "home":      Path.home(),
    }
    raw   = raw.strip().strip('"').strip("'")
    lower = raw.lower()
    if lower in shortcuts:
        return shortcuts[lower]

    # "desktop/notes/a.md" and "desktop\notes\a.md" — a shortcut followed by a
    # sub-path.  Without this branch the whole string falls through to the
    # relative-path return below and is resolved against the process CWD instead
    # of the real Desktop: an "Access denied" when the project lives outside the
    # home directory, or — worse — a silent write into a stray "desktop" folder
    # inside the project when it lives inside it.
    head, sep, rest = raw.replace("\\", "/").partition("/")
    if sep and head.lower() in shortcuts:
        rest = rest.strip("/")
        return shortcuts[head.lower()] / rest if rest else shortcuts[head.lower()]

    return Path(raw).expanduser()

def _format_size(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _bounded_walk(root: Path, deadline: float):
    """Yield files without following symlinks or crawling noisy build folders."""
    for current, dirs, files in os.walk(root, followlinks=False):
        if time.monotonic() > deadline:
            return
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for filename in files:
            if time.monotonic() > deadline:
                return
            yield Path(current) / filename


def _redact_secrets(text: str) -> str:
    """Remove likely credentials before file text is sent to the model."""
    patterns = [
        (r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
         "[REDACTED PRIVATE KEY]", re.DOTALL),
        (r"(?im)^\s*(api[_-]?key|secret|token|password|passwd|client[_-]?secret)\s*[:=]\s*[^\r\n]+",
         r"\1=[REDACTED]", 0),
        (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED AWS KEY]", 0),
        (r"\b(?:ghp|github_pat|sk)-[A-Za-z0-9_-]{20,}\b", "[REDACTED TOKEN]", 0),
    ]
    for pattern, replacement, flags in patterns:
        text = re.sub(pattern, replacement, text, flags=flags)
    return text


def _extract_document_text(target: Path) -> str:
    """Extract text from common local document formats."""
    suffix = target.suffix.casefold()
    if suffix in _TEXT_EXTENSIONS or not suffix:
        return target.read_text(encoding="utf-8", errors="replace")

    if suffix == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(target) as pdf:
                return "\n\n".join(page.extract_text() or "" for page in pdf.pages)
        except ImportError:
            from PyPDF2 import PdfReader
            return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(target)).pages)

    if suffix == ".docx":
        try:
            from docx import Document
            doc = Document(str(target))
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            with zipfile.ZipFile(target) as archive:
                root = ET.fromstring(archive.read("word/document.xml"))
            return "\n".join("".join(node.itertext()) for node in root.iter()
                             if node.tag.endswith("}p"))

    if suffix == ".xlsx":
        from openpyxl import load_workbook
        workbook = load_workbook(target, read_only=True, data_only=True)
        chunks = []
        try:
            for sheet in workbook.worksheets:
                chunks.append(f"[{sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    if any(value is not None for value in row):
                        chunks.append("\t".join("" if value is None else str(value)
                                                for value in row))
        finally:
            workbook.close()
        return "\n".join(chunks)

    if suffix == ".pptx":
        try:
            from pptx import Presentation
            presentation = Presentation(str(target))
            chunks = []
            for index, slide in enumerate(presentation.slides, 1):
                chunks.append(f"[Slide {index}]")
                chunks.extend(shape.text for shape in slide.shapes if hasattr(shape, "text"))
            return "\n".join(chunks)
        except ImportError:
            with zipfile.ZipFile(target) as archive:
                slides = sorted(name for name in archive.namelist()
                                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name))
                return "\n".join(" ".join(ET.fromstring(archive.read(name)).itertext())
                                  for name in slides)

    if suffix == ".rtf" and _OS == "Darwin":
        result = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(target)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode == 0:
            return result.stdout

    raise ValueError(f"Unsupported file type '{suffix or 'unknown'}'.")

def _safe_trash(target: Path) -> str:

    if not _SEND2TRASH:
        return (
            "send2trash is not installed. "
            "Run: pip install send2trash — "
            "Permanent deletion is disabled for safety."
        )
    send2trash.send2trash(str(target))
    return f"Moved to Trash: {target.name}"


def list_files(path: str = "desktop", show_hidden: bool = False,
               max_results: int = 100) -> str:
    try:
        target = _resolve_path(path)
        if not _is_readable_path(target):
            return f"Access denied: {target}"
        try:
            target.stat()  # Path.exists() can hide macOS permission errors.
        except FileNotFoundError:
            return f"Path not found: {target}"
        if not target.is_dir():
            return f"Not a directory: {target}"

        items = []
        all_items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        for item in all_items:
            if not show_hidden and item.name.startswith("."):
                continue
            if item.is_dir():
                items.append(f"📁 {item.name}/")
            else:
                size = _format_size(item.stat().st_size)
                items.append(f"📄 {item.name} ({size})")
            if len(items) >= max(1, min(int(max_results), _MAX_RESULTS)):
                break

        if not items:
            return f"Directory is empty: {target.name}/"

        visible_count = sum(1 for item in all_items
                            if show_hidden or not item.name.startswith("."))
        result = f"Contents of {target.name or target}/ ({visible_count} items):\n" + "\n".join(items)
        if visible_count > len(items):
            result += f"\n[Showing {len(items)} of {visible_count}; narrow the folder for more.]"
        return result

    except PermissionError:
        return _permission_denied(str(target) if "target" in locals() else path)
    except Exception as e:
        return f"Error listing files: {e}"


def create_file(path: str, name: str = "", content: str = "",
                overwrite: bool = False) -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if target.exists() and not overwrite:
            return (f"Already exists: {target.name}. Set overwrite=true only when "
                    "the user explicitly asks to replace it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        previous = None
        if existed:
            try:
                previous = target.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                previous = None
        target.write_text(content, encoding="utf-8")
        push_undo(f"created {target.name}",
                  _undo_write(target, previous) if existed else _undo_create(target))
        return f"File created: {target.name}"
    except PermissionError:
        return _permission_denied(str(target) if "target" in locals() else path)
    except Exception as e:
        return f"Could not create file: {e}"


def create_folder(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        already = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        # Only offer to undo a folder we actually made. "mkdir -p" on something
        # that was already there is not a change, and undoing it would delete a
        # directory the user has had for years.
        if not already:
            push_undo(f"created folder {target.name}", _undo_create(target))
        return f"Folder created: {target.name}"
    except Exception as e:
        return f"Could not create folder: {e}"


def delete_file(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        if _protected_root(target):
            return f"Protected directory, cannot delete: {target.name}"

        original = target.resolve()
        result   = _safe_trash(target)
        if result.startswith("Moved to Trash"):
            push_undo(f"deleted {original.name}",
                      lambda p=original: _restore_from_trash(p))
        return result

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Could not delete: {e}"


def move_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base   = _resolve_path(path)
        src    = (base / name) if name else base
        dst    = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name

        if dst.exists():
            return f"Destination already exists: {dst}"

        dst.parent.mkdir(parents=True, exist_ok=True)
        origin = src.resolve()
        shutil.move(str(src), str(dst))
        push_undo(f"moved {origin.name} to {dst.parent.name}/",
                  _undo_move(origin, dst.resolve()))
        return f"Moved: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"Could not move: {e}"


def copy_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base = _resolve_path(path)
        src  = (base / name) if name else base
        dst  = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_readable_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name

        if dst.exists():
            return f"Destination already exists: {dst}"

        dst.parent.mkdir(parents=True, exist_ok=True)

        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))

        # The undo for a copy is deleting the copy — never the original.
        _copy = dst.resolve()
        def _undo_copy():
            if not _copy.exists():
                return f"The copy '{_copy.name}' is already gone."
            if _copy.is_dir():
                shutil.rmtree(_copy)
            else:
                _copy.unlink()
            return f"Removed the copy in {_copy.parent.name}/."
        push_undo(f"copied {src.name} to {dst.parent.name}/", _undo_copy)

        return f"Copied: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"Could not copy: {e}"


def rename_file(path: str, name: str = "", new_name: str = "") -> str:
    try:
        base     = _resolve_path(path)
        target   = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"
        if not new_name:
            return "No new name provided."

        new_path = target.parent / new_name
        if new_path.exists():
            return f"A file named '{new_name}' already exists here."

        old_path = target.resolve()
        target.rename(new_path)
        push_undo(f"renamed {old_path.name} to {new_name}",
                  _undo_move(old_path, new_path.resolve()))
        return f"Renamed: {target.name} → {new_name}"

    except Exception as e:
        return f"Could not rename: {e}"


def read_file(path: str, name: str = "", max_chars: int = 12000) -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_readable_path(target):
            return f"Access denied: {target}"
        try:
            target.stat()
        except FileNotFoundError:
            return f"File not found: {target.name}"
        if not target.is_file():
            return f"Not a file: {target.name}"
        if not _can_return_contents(target):
            return (f"Protected credential file: {target.name}. Jarvis will not place "
                    "its contents into an AI request.")

        max_chars = max(1, min(int(max_chars), _MAX_READ_CHARS))
        content = _redact_secrets(_extract_document_text(target))
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[Truncated — {len(content)} total chars]"
        return content

    except PermissionError:
        return _permission_denied(str(target) if "target" in locals() else path)
    except Exception as e:
        return f"Could not read file: {e}"


def write_file(path: str, name: str = "", content: str = "",
               append: bool = False, overwrite: bool = False) -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if target.exists() and not append and not overwrite:
            return (f"Already exists: {target.name}. Use append=true, or set "
                    "overwrite=true only when the user explicitly asks to replace it.")
        target.parent.mkdir(parents=True, exist_ok=True)

        # Snapshot before writing. None means "did not exist", which is a
        # different undo (delete it) from "existed and had this in it".
        previous: str | None = None
        undoable = True
        if target.exists():
            try:
                if target.stat().st_size > _UNDO_CONTENT_LIMIT:
                    undoable = False       # too large to hold in memory
                else:
                    previous = target.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                undoable = False           # binary, locked, unreadable

        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content)

        action = "Appended to" if append else "Written to"
        if undoable:
            push_undo(f"wrote to {target.name}", _undo_write(target, previous))
            return f"{action}: {target.name}"
        return (f"{action}: {target.name}. "
                f"(Too large to keep a copy of the old contents, so this one "
                f"cannot be undone.)")
    except PermissionError:
        return _permission_denied(str(target) if "target" in locals() else path)
    except Exception as e:
        return f"Could not write file: {e}"


def find_files(name: str = "", extension: str = "",
               path: str = "home", max_results: int = 20) -> str:
    try:
        search_path = _resolve_path(path)
        if not _is_readable_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Search path not found: {path}"

        limit = max(1, min(int(max_results), _MAX_RESULTS))
        extension = extension.casefold()
        if extension and not extension.startswith("."):
            extension = "." + extension
        candidates: list[Path] = []

        # Spotlight makes a home-wide macOS search fast. The bounded walker is
        # the portable fallback and also covers unindexed external drives.
        if _OS == "Darwin" and name:
            escaped = name.replace("\\", "\\\\").replace('"', '\\"')
            query = f'kMDItemFSName == "*{escaped}*"cd'
            result = subprocess.run(
                ["mdfind", "-onlyin", str(search_path), query],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode == 0:
                candidates = [Path(line) for line in result.stdout.splitlines() if line]

        if not candidates:
            deadline = time.monotonic() + _MAX_SEARCH_SECONDS
            candidates = list(_bounded_walk(search_path, deadline))

        results = []
        for item in candidates:
            try:
                if not item.is_file():
                    continue
                if extension and item.suffix.casefold() != extension:
                    continue
                if name and name.casefold() not in item.name.casefold():
                    continue
                results.append(f"📄 {item.name} ({_format_size(item.stat().st_size)}) — {item.parent}")
                if len(results) >= limit:
                    break
            except (OSError, PermissionError):
                continue

        if not results:
            query = name or extension or "files"
            return f"No {query} found in {search_path.name}/"

        return f"Found {len(results)} file(s):\n" + "\n".join(results)

    except Exception as e:
        return f"Search error: {e}"


def search_file_contents(query: str, path: str = "home",
                         extension: str = "", max_results: int = 20) -> str:
    """Search text inside local files, with strict time and size bounds."""
    if not query.strip():
        return "No content search query provided."
    root = _resolve_path(path)
    if not _is_readable_path(root):
        return f"Access denied: {root}"
    if not root.exists():
        return f"Search path not found: {root}"

    limit = max(1, min(int(max_results), _MAX_RESULTS))
    extension = extension.casefold()
    if extension and not extension.startswith("."):
        extension = "." + extension
    needle = query.casefold()
    deadline = time.monotonic() + _MAX_SEARCH_SECONDS
    matches = []

    for item in _bounded_walk(root, deadline):
        try:
            if _is_sensitive(item) or item.stat().st_size > _MAX_SEARCH_FILE:
                continue
            if extension and item.suffix.casefold() != extension:
                continue
            if item.suffix.casefold() not in _TEXT_EXTENSIONS:
                continue
            text = item.read_text(encoding="utf-8", errors="ignore")
            folded = text.casefold()
            position = folded.find(needle)
            if position < 0:
                continue
            start = max(0, position - 90)
            end = min(len(text), position + len(query) + 130)
            snippet = _redact_secrets(text[start:end].replace("\n", " ").strip())
            matches.append(f"📄 {item}\n   …{snippet}…")
            if len(matches) >= limit:
                break
        except (OSError, PermissionError, UnicodeError):
            continue

    if not matches:
        return f"No text containing '{query}' found in {root}."
    return f"Found text in {len(matches)} file(s):\n" + "\n".join(matches)


def show_tree(path: str = "home", depth: int = 2,
              show_hidden: bool = False, max_results: int = 100) -> str:
    root = _resolve_path(path)
    if not _is_readable_path(root):
        return f"Access denied: {root}"
    if not root.exists() or not root.is_dir():
        return f"Folder not found: {root}"
    depth = max(1, min(int(depth), 5))
    limit = max(1, min(int(max_results), _MAX_RESULTS))
    lines = [f"📁 {root.name or root}/"]

    def visit(folder: Path, level: int):
        if level > depth or len(lines) >= limit:
            return
        try:
            children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except (OSError, PermissionError):
            lines.append("  " * level + "[permission denied]")
            return
        for child in children:
            if len(lines) >= limit:
                return
            if not show_hidden and child.name.startswith("."):
                continue
            lines.append("  " * level + ("📁 " if child.is_dir() else "📄 ") + child.name)
            if child.is_dir() and child.name not in _SKIP_DIRS:
                visit(child, level + 1)

    visit(root, 1)
    if len(lines) >= limit:
        lines.append(f"[Stopped at {limit} entries. Narrow the path or depth.]")
    return "\n".join(lines)


def get_recent_files(path: str = "home", count: int = 20) -> str:
    root = _resolve_path(path)
    if not _is_readable_path(root):
        return f"Access denied: {root}"
    if not root.exists():
        return f"Path not found: {root}"
    count = max(1, min(int(count), 50))
    deadline = time.monotonic() + _MAX_SEARCH_SECONDS
    recent = []
    for item in _bounded_walk(root, deadline):
        try:
            stat = item.stat()
            entry = (stat.st_mtime, str(item), stat.st_size)
            if len(recent) < count:
                heapq.heappush(recent, entry)
            elif entry[0] > recent[0][0]:
                heapq.heapreplace(recent, entry)
        except (OSError, PermissionError):
            continue
    if not recent:
        return "No files found."
    lines = [f"Most recently modified files in {root}:"]
    for modified, filename, size in sorted(recent, reverse=True):
        stamp = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {stamp}  {_format_size(size):>10}  {filename}")
    return "\n".join(lines)


def open_path(path: str, name: str = "", reveal: bool = False) -> str:
    base = _resolve_path(path)
    target = (base / name) if name else base
    if not _is_readable_path(target) or not target.exists():
        return f"Path not found or inaccessible: {target}"
    try:
        if _OS == "Darwin":
            command = ["open", "-R", str(target)] if reveal else ["open", str(target)]
        elif _OS == "Windows":
            if reveal:
                command = ["explorer", "/select,", str(target)]
            else:
                os.startfile(str(target))  # type: ignore[attr-defined]
                return f"Opened: {target}"
        else:
            command = ["xdg-open", str(target.parent if reveal else target)]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"{'Revealed in Finder' if reveal and _OS == 'Darwin' else 'Opened'}: {target}"
    except Exception as e:
        return f"Could not open path: {e}"


def get_largest_files(path: str = "downloads", count: int = 10) -> str:
    count = max(1, min(count, 50))
    try:
        search_path = _resolve_path(path)
        if not _is_readable_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Path not found: {path}"

        files = []
        deadline = time.monotonic() + _MAX_SEARCH_SECONDS
        for item in _bounded_walk(search_path, deadline):
            try:
                entry = (item.stat().st_size, str(item))
                if len(files) < count:
                    heapq.heappush(files, entry)
                elif entry[0] > files[0][0]:
                    heapq.heapreplace(files, entry)
            except (OSError, PermissionError):
                continue

        top = sorted(files, reverse=True)

        if not top:
            return "No files found."

        lines = [f"Top {len(top)} largest files in {search_path.name}/:"]
        for size, filename in top:
            item = Path(filename)
            lines.append(f"  {_format_size(size):>10}  {item.name}  ({item.parent})")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"


def get_disk_usage(path: str = "home") -> str:
    try:
        target = _resolve_path(path)
        usage  = shutil.disk_usage(target)
        pct    = usage.used / usage.total * 100
        return (
            f"Disk usage ({target}):\n"
            f"  Total : {_format_size(usage.total)}\n"
            f"  Used  : {_format_size(usage.used)} ({pct:.1f}%)\n"
            f"  Free  : {_format_size(usage.free)}"
        )
    except Exception as e:
        return f"Could not get disk usage: {e}"


def organize_desktop() -> str:
    type_map = {
        "Images":    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
        "Documents": {".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx",
                      ".ppt", ".pptx", ".csv", ".odt", ".ods", ".odp"},
        "Videos":    {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
        "Music":     {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
        "Archives":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
        "Code":      {".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
                      ".cpp", ".java", ".cs", ".go", ".rs", ".sh"},
    }

    desktop = _get_desktop()
    moved, skipped = [], []
    journal: list[tuple[Path, Path]] = []   # (where it was, where it went)

    try:
        for item in desktop.iterdir():
            # Leave folders, hidden files and organize-folders untouched
            if item.is_dir() or item.name.startswith("."):
                continue
            if item.name in {k for k in type_map}:
                continue

            ext        = item.suffix.lower()
            target_dir = desktop / "Others"
            for folder, exts in type_map.items():
                if ext in exts:
                    target_dir = desktop / folder
                    break

            target_dir.mkdir(exist_ok=True)
            new_path = target_dir / item.name

            if new_path.exists():
                skipped.append(item.name)
                continue

            origin = item.resolve()
            shutil.move(str(item), str(new_path))
            journal.append((origin, new_path.resolve()))
            moved.append(f"{item.name} → {target_dir.name}/")

        # One command, dozens of moves — so one undo that reverses all of them.
        # Without this, "organize my desktop" is the single least reversible
        # thing the assistant can do to a person's files, and it was completely
        # ungated.
        if journal:
            def _undo_organize(entries=tuple(journal)):
                restored = 0
                for origin, moved_to in entries:
                    try:
                        if moved_to.exists():
                            origin.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(moved_to), str(origin))
                            restored += 1
                    except Exception as e:
                        print(f"[file] undo organize: {moved_to.name}: {e}")
                # Clear away the folders we created, but only while they are
                # empty — anything the user put in since stays.
                for folder in {m.parent for _o, m in entries}:
                    try:
                        if folder.exists() and folder.is_dir() and not any(folder.iterdir()):
                            folder.rmdir()
                    except Exception:
                        pass
                return f"{restored} file(s) put back on the desktop."
            push_undo(f"organized the desktop ({len(journal)} files)", _undo_organize)

        result = f"Desktop organized: {len(moved)} files moved."
        if moved:
            preview = moved[:8]
            result += "\n" + "\n".join(preview)
            if len(moved) > 8:
                result += f"\n... and {len(moved) - 8} more."
        if skipped:
            result += f"\n{len(skipped)} file(s) skipped (name conflict)."
        return result

    except Exception as e:
        return f"Could not organize desktop: {e}"


def get_file_info(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_readable_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        stat = target.stat()
        info = {
            "Name":      target.name,
            "Type":      "Folder" if target.is_dir() else "File",
            "Size":      _format_size(stat.st_size),
            "Location":  str(target.parent),
            "Created":   datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
            "Modified":  datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "Extension": target.suffix or "—",
        }
        return "\n".join(f"  {k}: {v}" for k, v in info.items())

    except Exception as e:
        return f"Could not get file info: {e}"

def file_controller(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    path   = params.get("path", "desktop")
    name   = params.get("name", "")

    if player:
        player.write_log(f"[file] {action} {name or path}")

    try:
        if action == "list":
            return list_files(
                path,
                show_hidden=bool(params.get("show_hidden", False)),
                max_results=int(params.get("max_results", 100)),
            )

        elif action == "tree":
            return show_tree(
                path,
                depth=int(params.get("depth", 2)),
                show_hidden=bool(params.get("show_hidden", False)),
                max_results=int(params.get("max_results", 100)),
            )

        elif action == "create_file":
            return create_file(
                path, name=name, content=params.get("content", ""),
                overwrite=bool(params.get("overwrite", False)),
            )

        elif action == "create_folder":
            return create_folder(path, name=name)

        elif action == "delete":
            return delete_file(path, name=name)

        elif action == "move":
            return move_file(path, name=name, destination=params.get("destination", ""))

        elif action == "copy":
            return copy_file(path, name=name, destination=params.get("destination", ""))

        elif action == "rename":
            return rename_file(path, name=name, new_name=params.get("new_name", ""))

        elif action == "read":
            return read_file(path, name=name, max_chars=int(params.get("max_chars", 12000)))

        elif action == "write":
            return write_file(
                path, name=name,
                content=params.get("content", ""),
                append=bool(params.get("append", False)),
                overwrite=bool(params.get("overwrite", False)),
            )

        elif action == "find":
            return find_files(
                name=name or params.get("name", ""),
                extension=params.get("extension", ""),
                path=path,
                max_results=min(int(params.get("max_results", 20)), 50),
            )

        elif action == "search_contents":
            return search_file_contents(
                query=params.get("query", ""),
                extension=params.get("extension", ""),
                path=path,
                max_results=int(params.get("max_results", 20)),
            )

        elif action == "recent":
            return get_recent_files(path=path, count=int(params.get("count", 20)))

        elif action == "largest":
            return get_largest_files(
                path=path,
                count=int(params.get("count", 10)),
            )

        elif action == "disk_usage":
            return get_disk_usage(path)

        elif action == "organize_desktop":
            return organize_desktop()

        elif action == "info":
            return get_file_info(path, name=name)

        elif action == "open":
            return open_path(path, name=name, reveal=False)

        elif action == "reveal":
            return open_path(path, name=name, reveal=True)

        else:
            return f"Unknown action: '{action}'"

    except Exception as e:
        return f"File controller error ({action}): {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "file_controller",
    "description": (
        "Browses and searches the Mac, reads common documents, opens or reveals files, "
        "and safely manages user files. Read-only actions can inspect any macOS location "
        "allowed by Full Disk Access. Changes are limited to the home folder and mounted "
        "external drives; credentials are never returned to the model."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "list | tree | find | search_contents | recent | read | info | open | reveal | create_file | create_folder | write | copy | move | rename | delete | largest | disk_usage | organize_desktop"
            },
            "path": {
                "type": "STRING",
                "description": "Absolute file/folder path, or shortcut: home, desktop, downloads, documents, pictures, music, videos"
            },
            "destination": {
                "type": "STRING",
                "description": "Destination path for move/copy"
            },
            "new_name": {
                "type": "STRING",
                "description": "New name for rename"
            },
            "content": {
                "type": "STRING",
                "description": "Content for create_file/write"
            },
            "name": {
                "type": "STRING",
                "description": "File name to search for"
            },
            "extension": {
                "type": "STRING",
                "description": "File extension to search (e.g. .pdf)"
            },
            "count": {
                "type": "INTEGER",
                "description": "Number of results for largest/recent"
            },
            "query": {
                "type": "STRING",
                "description": "Text to find inside files for search_contents"
            },
            "max_results": {
                "type": "INTEGER",
                "description": "Maximum result count, capped at 100"
            },
            "max_chars": {
                "type": "INTEGER",
                "description": "Maximum characters returned by read, capped at 80000"
            },
            "depth": {
                "type": "INTEGER",
                "description": "Folder depth for tree, capped at 5"
            },
            "show_hidden": {
                "type": "BOOLEAN",
                "description": "Include hidden files in list/tree"
            },
            "append": {
                "type": "BOOLEAN",
                "description": "Append to a text file instead of replacing it"
            },
            "overwrite": {
                "type": "BOOLEAN",
                "description": "Replace an existing text file; use only when the user explicitly asks"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": file_controller,
}
