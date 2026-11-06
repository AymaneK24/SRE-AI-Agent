"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .net_guard import TargetPolicy

log = logging.getLogger("rcc.config")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _hosts() -> list[str]:
    raw = _env("RCC_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
    return [host.strip().lower() for host in raw.split(",") if host.strip()]


@dataclass(frozen=True)
class Config:
    host: str = field(default_factory=lambda: _env("RCC_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("RCC_PORT", "8000")))
    data_dir: Path = field(default_factory=lambda: Path(_env("RCC_DATA_DIR", "./data")))
    database_url: str = field(default_factory=lambda: _env("RCC_DATABASE_URL"))
    # "self_hosted": one company, for local use. "production": one company on a public address, with stricter checks.
    # "hosted": a shared service for many companies. It's off unless someone explicitly chooses it.
    mode: str = field(default_factory=lambda: _env("RCC_MODE", "self_hosted"))
    multi_company: bool = field(default_factory=lambda: _bool("RCC_MULTI_COMPANY"))
    company_name: str = field(default_factory=lambda: _env("RCC_COMPANY_NAME"))
    public_url: str = field(default_factory=lambda: _env("RCC_PUBLIC_URL", "http://localhost:8000").rstrip("/"))
    allowed_hosts: list[str] = field(default_factory=_hosts)
    secret_key: str = field(default_factory=lambda: _env("RCC_SECRET_KEY"))
    trust_proxy: bool = field(default_factory=lambda: _bool("RCC_TRUST_PROXY"))
    admin_password: str = field(default_factory=lambda: _env("RCC_ADMIN_PASSWORD"))
    admin_email: str = field(default_factory=lambda: _env("RCC_ADMIN_EMAIL").strip().lower())
    admin_name: str = field(default_factory=lambda: _env("RCC_ADMIN_NAME", "Administrator"))
    signup_open: bool = field(default_factory=lambda: _bool("RCC_SIGNUP_OPEN", True))
    product_operator: str = field(default_factory=lambda: _env("RCC_OPERATOR_NAME", "Aymane KENBOUCH"))
    contact_email: str = field(default_factory=lambda: _env("RCC_CONTACT_EMAIL"))
    legal_reviewed: bool = field(default_factory=lambda: _bool("RCC_LEGAL_REVIEWED"))

    smtp_host: str = field(default_factory=lambda: _env("RCC_SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("RCC_SMTP_PORT", "587")))
    smtp_username: str = field(default_factory=lambda: _env("RCC_SMTP_USERNAME"))
    smtp_password: str = field(default_factory=lambda: _env("RCC_SMTP_PASSWORD"))
    smtp_from: str = field(default_factory=lambda: _env("RCC_SMTP_FROM"))
    smtp_tls: str = field(default_factory=lambda: _env("RCC_SMTP_TLS", "starttls"))  # starttls, ssl, or none

    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_base_url: str = field(default_factory=lambda: _env("RCC_ANTHROPIC_URL", "https://api.anthropic.com"))
    default_ollama_url: str = field(default_factory=lambda: _env("RCC_OLLAMA_URL", "http://localhost:11434"))
    aws_endpoint_url: str = field(default_factory=lambda: _env("RCC_AWS_ENDPOINT_URL"))
    azure_management_url: str = field(default_factory=lambda: _env("RCC_AZURE_MANAGEMENT_URL", "https://management.azure.com"))
    azure_login_url: str = field(default_factory=lambda: _env("RCC_AZURE_LOGIN_URL", "https://login.microsoftonline.com"))
    azure_logs_url: str = field(default_factory=lambda: _env("RCC_AZURE_LOGS_URL", "https://api.loganalytics.io"))
    allow_private_targets_env: str = field(default_factory=lambda: _env("RCC_ALLOW_PRIVATE_TARGETS"))
    allow_loopback_targets: bool = field(default_factory=lambda: _bool("RCC_ALLOW_LOOPBACK_TARGETS"))
    target_allowlist: tuple[str, ...] = field(default_factory=lambda: tuple(item.strip().lower() for item in _env("RCC_TARGET_ALLOWLIST").split(",") if item.strip()))
    alert_email_wait_s: float = field(default_factory=lambda: float(_env("RCC_ALERT_EMAIL_WAIT_SECONDS", "600")))
    in_container: bool = field(default_factory=lambda: Path("/.dockerenv").exists())

    @property
    def db_path(self) -> Path:
        return self.data_dir / "console.db"

    @property
    def is_hosted(self) -> bool:
        return self.mode == "hosted"

    @property
    def allow_private_targets(self) -> bool:
        if self.allow_private_targets_env:
            return self.allow_private_targets_env.lower() in {"1", "true", "yes", "on"}
        return not self.is_hosted

    @property
    def target_policy(self) -> TargetPolicy:
        return TargetPolicy(self.allow_private_targets, self.allow_loopback_targets, self.target_allowlist)

    @property
    def single_company(self) -> bool:
        """The normal setup: this installation and its database belong to one company."""
        return not (self.multi_company or self.is_hosted)

    @property
    def accepted_hosts(self) -> list[str]:
        """Host names the app answers to. The public address always counts, so people only set it once."""
        host = (urlparse(self.public_url).hostname or "").lower()
        host = f"[{host}]" if ":" in host else host
        return [*self.allowed_hosts, host] if host else self.allowed_hosts

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    def validate(self) -> None:
        """Refuse settings that would make a public deployment unsafe."""
        if self.is_hosted or self.mode == "production":
            problems = []
            if not self.secure_cookies:
                problems.append("RCC_PUBLIC_URL must use https")
            if not self.secret_key:
                problems.append("RCC_SECRET_KEY must be set")
            if not self.database_url:
                problems.append("RCC_DATABASE_URL must point to PostgreSQL")
            if not self.admin_password:
                problems.append("RCC_ADMIN_PASSWORD must be set, because a default password isn't safe on a public address")
            if not self.admin_email:
                problems.append("RCC_ADMIN_EMAIL must be set, because people sign in with their email address")
            if self.is_hosted and not self.smtp_configured:
                problems.append("RCC_SMTP_HOST and RCC_SMTP_FROM must be set so sign-ups can be verified")
            if "*" in self.allowed_hosts:
                problems.append("RCC_ALLOWED_HOSTS must list your real host names")
            if problems:
                raise SystemExit(f"Refusing to start in {self.mode} mode: " + "; ".join(problems) + ".")
        elif not self.secure_cookies:
            log.warning("RCC_PUBLIC_URL doesn't use HTTPS, so passwords and session cookies travel unencrypted. Use this only for a short trial on a trusted network.")
