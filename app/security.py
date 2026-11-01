"""Access tokens, token hashing/verification, rate limiting, and trusted-proxy IP parsing."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import time
import tempfile
from pathlib import Path
from collections import deque
from collections.abc import Callable, Sequence
from ipaddress import IPv4Network, IPv6Network, ip_address
from typing import Union, NamedTuple

from starlette.requests import Request

from app.exceptions import AuthenticationRequired

TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_BEARER_RE = re.compile(r"^Bearer\s+(\S+)$", re.IGNORECASE)

Network = Union[IPv4Network, IPv6Network]


def generate_access_token() -> str:
    """256-bit URL-safe random token. Shown to the client exactly once."""
    return "rp_" + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str, pepper: str = "") -> str:
    """SHA-256 of the token; HMAC-SHA-256 keyed by the pepper when one is configured."""
    data = token.encode("utf-8")
    if pepper:
        return hmac.new(pepper.encode("utf-8"), data, hashlib.sha256).hexdigest()
    return hashlib.sha256(data).hexdigest()


def verify_token(token: str, stored_hash: str, pepper: str = "") -> bool:
    return hmac.compare_digest(hash_token(token, pepper), stored_hash)


def extract_bearer_token(authorization: str | None) -> str:
    """Parse 'Authorization: Bearer <token>'; raise AuthenticationRequired if absent/malformed."""
    if not authorization:
        raise AuthenticationRequired()
    match = _BEARER_RE.match(authorization.strip())
    if not match or not _TOKEN_RE.match(match.group(1)):
        raise AuthenticationRequired()
    return match.group(1)


class RateLimiter:
    """In-memory sliding-window limiter, per process.

    With several web instances each keeps its own counters, so the effective limit is
    per instance. Use an edge/WAF limit if a global limit is required.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, int]:
        """Record a hit. Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        cutoff = now - self.window
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return LimitResult(False, max(1, math.ceil(hits[0] + self.window - now)))
        hits.append(now)
        if len(self._hits) > self.max_keys:
            self._prune(cutoff)
        return LimitResult(True, 0)

    def _prune(self, cutoff: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
            del self._hits[k]
        while len(self._hits) > self.max_keys:
            del self._hits[next(iter(self._hits))]


def _is_trusted(ip: object, networks: Sequence[Network]) -> bool:
    return any(ip in net for net in networks)  # type: ignore[operator]


def resolve_client_ip(request: Request | str | None, trusted_networks: Sequence[Network] | str, legacy_networks: Sequence[Network] | None = None) -> str:
    """Client IP for rate limiting. X-Forwarded-For is honored only from trusted proxies.

    Walks the header right-to-left, skipping trusted proxy hops, and returns the first
    untrusted address (the closest address a trusted proxy actually observed).
    """
    legacy = legacy_networks is not None
    if legacy:
        peer = request if isinstance(request, str) else None
        header = trusted_networks if isinstance(trusted_networks, str) else ""
        networks = legacy_networks
    else:
        peer = request.client.host if request is not None and not isinstance(request, str) and request.client else None
        header = request.headers.get("x-forwarded-for", "") if request is not None and not isinstance(request, str) else ""
        networks = trusted_networks if not isinstance(trusted_networks, str) else ()
    if not peer:
        return "unknown"
    try:
        peer_ip = ip_address(peer)
    except ValueError:
        return peer[:64]
    if not networks or not _is_trusted(peer_ip, networks):
        return str(peer_ip)
    if not header:
        return str(peer_ip)
    candidate = str(peer_ip)
    for raw in reversed([p.strip() for p in header.split(",")][-20:]):
        try:
            hop = ip_address(raw)
        except ValueError:
            return str(peer_ip)
        candidate = str(hop)
        if not _is_trusted(hop, networks):
            return candidate
    return candidate


class AuthorizationError(Exception):
    def __init__(self, status_code: int): self.status_code=status_code; super().__init__("authorization failed")
class RateLimitExceeded(Exception): pass
class LimitResult(NamedTuple):
    allowed: bool
    retry_after_seconds: int
SlidingWindowRateLimiter = RateLimiter
generate_token = generate_access_token
parse_bearer = extract_bearer_token

def authorize_job(conn, job_id: str, authorization: str | None, settings, now=None):
    from datetime import datetime, timezone
    from uuid import UUID
    try: jid=UUID(job_id)
    except (ValueError, TypeError): raise AuthorizationError(404) from None
    try: token=extract_bearer_token(authorization)
    except AuthenticationRequired: raise AuthorizationError(401) from None
    with conn.cursor() as cur:
        cur.execute("SELECT token_hash, expires_at, revoked_at FROM public.job_access_tokens WHERE job_id = %s", (jid,))
        rows=cur.fetchall()
    pepper=getattr(settings,"pepper_value",getattr(settings,"token_pepper",""))
    current=now or datetime.now(timezone.utc)
    for row in rows:
        if verify_token(token,row[0],pepper) and row[1] > current and row[2] is None: return True
    raise AuthorizationError(404)

def _limiter_enforce(self, key):
    allowed,retry=self.check(key)
    if not allowed: raise RateLimitExceeded(retry)
RateLimiter.enforce = _limiter_enforce

def safe_temp_path(directory, suffix=".pdf"):
    if not isinstance(suffix,str) or not re.fullmatch(r"\.[A-Za-z0-9]{1,8}",suffix): raise ValueError("invalid suffix")
    root=Path(directory).resolve(); root.mkdir(parents=True,exist_ok=True)
    path=(root/(secrets.token_hex(16)+suffix)).resolve()
    if root not in path.parents: raise ValueError("path escapes temp directory")
    return path

def sanitize_filename(value: str | None) -> str:
    if not value: return "upload"
    value=value.replace("\\","/").split("/")[-1]
    value=re.sub(r"[^A-Za-z0-9._-]+","_",value).strip(".")[:100]
    return value or "upload"

def security_headers():
    return {"Content-Security-Policy":"default-src 'self'; script-src 'self'; object-src 'none'",
            "X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Referrer-Policy":"no-referrer"}
