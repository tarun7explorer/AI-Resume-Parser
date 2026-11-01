"""Validated environment configuration for the web and worker services."""
from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Mapping


class ConfigError(ValueError):
    pass


def _integer(env: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(env.get(key, str(default)))
    except ValueError:
        raise ConfigError(f"{key} must be an integer") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{key} is outside its allowed range")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    environment: str = "development"
    log_level: str = "INFO"
    enable_docs: bool = False
    static_dir: str = "static"
    origins_list: tuple[str, ...] = ()
    trusted_proxy_networks: tuple[IPv4Network | IPv6Network, ...] = ()
    pepper_value: str = ""
    token_pepper: str | None = None
    max_request_bytes: int = 125829120
    max_upload_bytes: int = 10 * 1024 * 1024
    max_jd_chars: int = 20000
    max_resumes_per_job: int = 10
    job_max_attempts: int = 3
    retention_days: int = 14
    access_token_ttl_hours: int = 24 * 14
    poll_after_seconds: int = 2
    submit_rate_limit_per_minute: int = 5
    status_rate_limit_per_minute: int = 60

    def __post_init__(self):
        if self.token_pepper and not self.pepper_value:
            object.__setattr__(self, "pepper_value", self.token_pepper)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        database_url = e.get("DATABASE_URL", "").strip()
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ConfigError("DATABASE_URL must be a PostgreSQL URL")
        environment = e.get("APP_ENV", "development").strip().lower()
        pepper = e.get("TOKEN_PEPPER", "")
        origins = tuple(x.strip() for x in e.get("ALLOWED_ORIGINS", "").split(",") if x.strip())
        try:
            proxies = tuple(ip_network(x.strip(), strict=False) for x in e.get("TRUSTED_PROXY_CIDRS", "").split(",") if x.strip())
        except ValueError:
            raise ConfigError("TRUSTED_PROXY_CIDRS contains an invalid network") from None
        if environment == "production" and (len(pepper) < 32 or "*" in origins):
            raise ConfigError("Production requires a strong TOKEN_PEPPER and explicit origins")
        return cls(
            database_url=database_url, environment=environment,
            log_level=e.get("LOG_LEVEL", "INFO").upper(),
            enable_docs=e.get("ENABLE_DOCS", "false").lower() in ("1", "true", "yes"),
            static_dir=e.get("STATIC_DIR", "static"), origins_list=origins,
            trusted_proxy_networks=proxies, pepper_value=pepper,
            max_request_bytes=_integer(e,"MAX_REQUEST_BYTES",125829120,1024,200000000),
            max_upload_bytes=_integer(e,"MAX_UPLOAD_BYTES",10485760,1024,52428800),
            max_jd_chars=_integer(e,"MAX_JD_CHARS",20000,20,100000),
            max_resumes_per_job=_integer(e,"MAX_RESUMES_PER_JOB",10,1,50),
            job_max_attempts=_integer(e,"JOB_MAX_ATTEMPTS",3,1,10),
            retention_days=_integer(e,"RETENTION_DAYS",14,1,3650),
            access_token_ttl_hours=_integer(e,"ACCESS_TOKEN_TTL_HOURS",336,1,87600),
            poll_after_seconds=_integer(e,"POLL_AFTER_SECONDS",2,1,30),
            submit_rate_limit_per_minute=_integer(e,"SUBMIT_RATE_LIMIT_PER_MINUTE",5,1,1000),
            status_rate_limit_per_minute=_integer(e,"STATUS_RATE_LIMIT_PER_MINUTE",60,1,10000),
        )


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    return Settings.from_env(env)


def get_settings() -> Settings:
    return load_settings()
