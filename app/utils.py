from __future__ import annotations
import hashlib, re
from datetime import datetime, timezone

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def sanitize_filename(name: str | None, fallback: str = "upload.pdf") -> str:
    value = (name or "").replace("\\", "/").split("/")[-1]
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")[:200]
    return value or fallback
