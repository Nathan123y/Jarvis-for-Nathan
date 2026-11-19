"""Files on disk (data/files/<request>/<random>.<ext>) plus a row in `assets`.

Uploads are checked by decoding them, not by trusting the file name or the browser's content type.
"""
import io
import secrets
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .db import DB, now

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_REFERENCE_FILES = 5
IMAGE_FORMATS = {"PNG": ("image/png", "png"), "JPEG": ("image/jpeg", "jpg"), "WEBP": ("image/webp", "webp")}
Image.MAX_IMAGE_PIXELS = 120_000_000   # production panels can be large; this still stops decompression bombs


class UploadError(ValueError):
    """A customer-friendly reason an upload was refused."""


def sniff(data: bytes, allow_pdf: bool = False):
    """(mime, ext, width, height) for a valid image (or PDF when allowed). Raises UploadError otherwise."""
    if not data:
        raise UploadError("That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"That file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if allow_pdf and data[:5] == b"%PDF-":
        return "application/pdf", "pdf", None, None
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError):
        raise UploadError("That file isn't a readable PNG, JPEG or WebP image.")
    if fmt not in IMAGE_FORMATS:
        raise UploadError("Please upload a PNG, JPEG or WebP image.")
    mime, ext = IMAGE_FORMATS[fmt]
    return mime, ext, w, h


class Storage:
    def __init__(self, db: DB, root: Path):
        self.db, self.root = db, Path(root)

    def path_of(self, asset: dict) -> Path:
        p = (self.root / asset["path"]).resolve()
        if self.root.resolve() not in p.parents:
            raise PermissionError("asset path escapes the storage folder")
        return p

    def save(self, request_id: int, kind: str, data: bytes, original_name: str = "", panel: str = "",
             allow_pdf: bool = False) -> int:
        mime, ext, w, h = sniff(data, allow_pdf=allow_pdf)
        rel = Path(str(request_id)) / f"{kind}-{secrets.token_hex(8)}.{ext}"
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)                       # never a half-written file under the final name
        safe_name = "".join(ch for ch in (original_name or "")[-120:] if ch.isprintable())
        return self.db.run(
            "INSERT INTO assets (request_id, kind, panel, path, original_name, mime, width, height, bytes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (request_id, kind, panel, str(rel), safe_name, mime, w, h, len(data), now()))

    def get(self, asset_id: int):
        return self.db.one("SELECT * FROM assets WHERE id=?", (asset_id,))

    def list(self, request_id: int, kind: str):
        return self.db.all("SELECT * FROM assets WHERE request_id=? AND kind=? ORDER BY id", (request_id, kind))
