import ipaddress
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.config import Settings, ConfigError, load_settings
from app.security import (
    AuthorizationError, RateLimitExceeded, SlidingWindowRateLimiter, authorize_job,
    generate_token, hash_token, parse_bearer, resolve_client_ip, safe_temp_path,
    sanitize_filename, security_headers,
)

PEPPER = "p" * 40
SETTINGS = Settings(token_pepper=PEPPER)
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)


def _row(token, expires=NOW + timedelta(hours=1), revoked=None):
    return (hash_token(token, PEPPER), expires, revoked)


def _auth(conn, token, job=None):
    authorize_job(conn, job or str(uuid4()), f"Bearer {token}", SETTINGS, NOW)


def test_token_is_random_and_hash_is_not_token():
    a, b = generate_token(), generate_token()
    assert a != b and a.startswith("rp_") and len(a) == 46
    h = hash_token(a, PEPPER)
    assert len(h) == 64 and a not in h
    assert hash_token(a, PEPPER) == h
    assert hash_token(a, "q" * 40) != h


def test_authorize_valid_token():
    t = generate_token()
    _auth(FakeConn([_row(t)]), t)


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer short", "Bearer " + "x" * 500])
def test_missing_or_malformed_is_401(header):
    with pytest.raises(AuthorizationError) as e:
        authorize_job(FakeConn([]), str(uuid4()), header, SETTINGS, NOW)
    assert e.value.status_code == 401


def test_wrong_token_unknown_job_expired_revoked_are_404():
    t, other = generate_token(), generate_token()
    for rows, tok in [
        ([_row(t)], other),
        ([], t),
        ([_row(t, expires=NOW - timedelta(seconds=1))], t),
        ([_row(t, revoked=NOW - timedelta(minutes=1))], t),
    ]:
        with pytest.raises(AuthorizationError) as e:
            _auth(FakeConn(rows), tok)
        assert e.value.status_code == 404


def test_invalid_job_id_is_404():
    t = generate_token()
    with pytest.raises(AuthorizationError) as e:
        authorize_job(FakeConn([_row(t)]), "1; DROP TABLE x", f"Bearer {t}", SETTINGS, NOW)
    assert e.value.status_code == 404


def test_parse_bearer_accepts_valid():
    t = generate_token()
    assert parse_bearer(f"bearer {t}") == t


def test_rate_limiter_blocks_and_recovers():
    clock = [0.0]
    rl = SlidingWindowRateLimiter(2, 10, clock=lambda: clock[0])
    assert rl.check("a").allowed and rl.check("a").allowed
    blocked = rl.check("a")
    assert not blocked.allowed and 0 < blocked.retry_after_seconds <= 10
    assert rl.check("b").allowed
    clock[0] = 11
    assert rl.check("a").allowed
    with pytest.raises(RateLimitExceeded):
        rl.enforce("b"); rl.enforce("b"); rl.enforce("b")


def test_rate_limiter_bounds_memory():
    rl = SlidingWindowRateLimiter(1, 60, max_keys=5)
    for i in range(50):
        rl.check(f"k{i}")
    assert len(rl._hits) <= 5


TRUSTED = [ipaddress.ip_network("10.0.0.0/8")]


def test_xff_ignored_from_untrusted_peer():
    assert resolve_client_ip("203.0.113.9", "1.2.3.4", TRUSTED) == "203.0.113.9"


def test_xff_ignored_when_no_trusted_proxies():
    assert resolve_client_ip("10.0.0.1", "1.2.3.4", []) == "10.0.0.1"


def test_xff_used_from_trusted_peer_and_spoofing_resisted():
    # client spoofed "6.6.6.6"; our proxy appended the real address
    assert resolve_client_ip("10.0.0.1", "6.6.6.6, 198.51.100.7", TRUSTED) == "198.51.100.7"
    assert resolve_client_ip("10.0.0.1", "198.51.100.7, 10.0.0.2", TRUSTED) == "198.51.100.7"


def test_malformed_xff_falls_back_to_peer():
    assert resolve_client_ip("10.0.0.1", "not-an-ip", TRUSTED) == "10.0.0.1"
    assert resolve_client_ip(None, "1.1.1.1", TRUSTED) == "unknown"


def test_sanitize_filename_blocks_traversal():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("..\\..\\win.ini") == "win.ini"
    # "/" is a path separator, so only the final segment survives, then is sanitized
    assert sanitize_filename("<script>alert(1)</script>.pdf") == "script_.pdf"
    assert sanitize_filename("<img onerror=x>.pdf") == "_img_onerror_x_.pdf"
    assert sanitize_filename("...") == "upload" and sanitize_filename(None) == "upload"
    assert len(sanitize_filename("a" * 500)) == 100


def test_safe_temp_path_stays_inside(tmp_path):
    p = safe_temp_path(tmp_path)
    assert tmp_path.resolve() in p.parents and p.suffix == ".pdf"
    with pytest.raises(ValueError):
        safe_temp_path(tmp_path, suffix="/../x")


def test_security_headers():
    h = security_headers()
    assert "script-src 'self'" in h["Content-Security-Policy"]
    assert h["X-Content-Type-Options"] == "nosniff"


def test_production_config_requires_pepper_and_rejects_wildcard_origin():
    base = {"APP_ENV": "production", "DATABASE_URL": "postgresql://u:p@h/db"}
    with pytest.raises(ConfigError):
        load_settings(base)
    with pytest.raises(ConfigError):
        load_settings({**base, "TOKEN_PEPPER": PEPPER, "ALLOWED_ORIGINS": "*"})
    assert load_settings({**base, "TOKEN_PEPPER": PEPPER}).is_production
    with pytest.raises(ConfigError):
        load_settings({"TRUSTED_PROXY_CIDRS": "nonsense"})