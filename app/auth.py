"""Accounts: passwords, sessions, roles, policies, sign-in limits, and first-run setup."""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response

from .config import Config
from .db import Database, sha256_text, utc_after, utcnow
from .validation import sanitize_stored_name
from .workflows import seed_workflows

log = logging.getLogger("rcc.auth")
hasher = PasswordHasher()

SESSION_COOKIE = "rcc_session"
ROLE_RANK = {"viewer": 1, "engineer": 2, "admin": 3, "owner": 4}
DEFAULT_POLICIES: dict[str, Any] = {
    "password_min_length": 12,
    "session_hours": 12,
    "allowed_email_domains": [],
    "require_sso": False,
    "allow_cloud_ai": True,
}
COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd", "123456789012", "1234567890", "qwertyuiop", "qwerty123456",
    "admin", "adminadmin", "administrator", "letmein", "welcome123", "changeme", "changeme123", "iloveyou", "azertyuiop", "motdepasse",
}
DUMMY_HASH = hasher.hash("timing-protection-value")
DEFAULT_ADMIN_EMAIL = "admin@rootcause.local"
MAX_SESSIONS_PER_USER = 5


@dataclass
class Principal:
    user_id: int
    org_id: int
    email: str
    name: str
    role: str
    is_platform_admin: bool
    must_change_password: bool
    org_name: str

    @property
    def actor(self) -> str:
        return self.email

    def can(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, 0) >= ROLE_RANK[role]


class RateLimiter:
    """Limits attempts per key within a sliding window. It lives in memory on this one instance."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_s: int) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - window_s:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def count(self, key: str, window_s: int) -> int:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - window_s:
            hits.popleft()
        return len(hits)

    def record(self, key: str) -> None:
        self._hits[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)


def hash_password(password: str) -> str:
    return hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return hasher.verify(stored or DUMMY_HASH, password) and bool(stored)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(password: str, policies: dict[str, Any], *context: str) -> str | None:
    minimum = max(int(policies.get("password_min_length", 12)), 12)
    if len(password) < minimum:
        return f"Use at least {minimum} characters."
    if len(password) > 256:
        return "Use 256 characters or fewer."
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(lowered)) < 4:
        return "This password is too common. Choose something harder to guess."
    for item in context:
        word = (item or "").lower().split("@")[0]
        if len(word) >= 4 and word in lowered:
            return "Don't include your name or email address in your password."
    return None


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
        raise HTTPException(422, "Enter a valid email address.")
    return email


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
    return slug or "organization"


def client_ip(request: Request, config: Config) -> str:
    if config.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else ""


async def org_policies(db: Database, org_id: int) -> dict[str, Any]:
    row = await db.one("SELECT policies FROM organizations WHERE id = ?", (org_id,))
    stored = json.loads(row["policies"]) if row else {}
    return {**DEFAULT_POLICIES, **stored}


async def create_session(db: Database, config: Config, response: Response, request: Request, user: dict[str, Any]) -> None:
    policies = await org_policies(db, user["org_id"])
    token = secrets.token_urlsafe(32)
    hours = max(1, min(int(policies["session_hours"]), 720))
    now = utcnow()
    await db.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen_at, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sha256_text(token), user["id"], now, utc_after(hours=hours), now, client_ip(request, config), request.headers.get("user-agent", "")[:200]),
    )
    await db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user["id"]))
    # A person gets a few devices at once. The oldest session ends when they go past that.
    rows = await db.all("SELECT token_hash FROM sessions WHERE user_id = ? ORDER BY created_at DESC, token_hash", (user["id"],))
    for row in rows[MAX_SESSIONS_PER_USER:]:
        await db.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=config.secure_cookies, max_age=hours * 3600, path="/")


async def end_session(db: Database, request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await db.execute("DELETE FROM sessions WHERE token_hash = ?", (sha256_text(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")


async def load_principal(db: Database, request: Request) -> Principal | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = await db.one(
        "SELECT s.expires_at, u.*, o.name AS org_name, o.status AS org_status FROM sessions s JOIN users u ON u.id = s.user_id "
        "JOIN organizations o ON o.id = u.org_id WHERE s.token_hash = ?", (sha256_text(token),))
    if not row or not row["is_active"]:
        return None
    if datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
        await db.execute("DELETE FROM sessions WHERE token_hash = ?", (sha256_text(token),))
        return None
    if row["org_status"] != "active" and not row["is_platform_admin"]:
        return None
    return Principal(
        user_id=row["id"], org_id=row["org_id"], email=row["email"], name=row["name"],
        role=row["role"], is_platform_admin=bool(row["is_platform_admin"]), must_change_password=bool(row["must_change_password"]),
        org_name=row["org_name"],
    )


async def create_email_token(db: Database, purpose: str, user_id: int, hours: int) -> str:
    token = secrets.token_urlsafe(32)
    await db.execute("UPDATE email_tokens SET used_at = ? WHERE user_id = ? AND purpose = ? AND used_at IS NULL", (utcnow(), user_id, purpose))
    await db.insert(
        "INSERT INTO email_tokens(purpose, user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (purpose, user_id, sha256_text(token), utc_after(hours=hours), utcnow()),
    )
    return token


async def consume_email_token(db: Database, purpose: str, token: str) -> dict[str, Any] | None:
    row = await db.one(
        "SELECT t.id AS token_id, t.expires_at, t.used_at, u.* FROM email_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash = ? AND t.purpose = ?", (sha256_text(token), purpose))
    if not row or row["used_at"]:
        return None
    if datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
        return None
    changed = await db.execute("UPDATE email_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL", (utcnow(), row["token_id"]))
    return row if changed else None


async def create_organization(db: Database, name: str, contact_email: str) -> int:
    slug = slugify(name)
    base = slug
    suffix = 1
    while await db.one("SELECT id FROM organizations WHERE slug = ?", (slug,)):
        suffix += 1
        slug = f"{base}-{suffix}"
    org_id = await db.insert(
        "INSERT INTO organizations(name, slug, contact_email, policies, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, slug, contact_email, json.dumps(DEFAULT_POLICIES), utcnow()),
    )
    await seed_workflows(db, org_id)
    return org_id


async def ensure_default_admin(db: Database, config: Config) -> None:
    """Create the first administrator. The first password is temporary, wherever it came from, because it stays readable in a settings file."""
    existing = await db.one("SELECT id, email FROM users WHERE is_platform_admin = 1 LIMIT 1")
    if existing:
        # An upgraded installation had an admin with no email. RCC_ADMIN_EMAIL gives it the real one.
        if existing["email"] == DEFAULT_ADMIN_EMAIL and config.admin_email and not await db.one("SELECT id FROM users WHERE email = ?", (config.admin_email,)):
            await db.execute("UPDATE users SET email = ?, username = ? WHERE id = ?", (config.admin_email, config.admin_email, existing["id"]))
        return
    email = config.admin_email or DEFAULT_ADMIN_EMAIL
    org_id = await create_organization(db, config.company_name or "My company", config.admin_email)
    password = config.admin_password or "admin"
    await db.insert(
        "INSERT INTO users(org_id, username, email, name, password_hash, role, is_platform_admin, must_change_password, email_verified_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'owner', 1, ?, ?, ?)",
        (org_id, email, email, sanitize_stored_name(config.admin_name, "Administrator"), hash_password(password), 1, utcnow(), utcnow()),
    )
    await db.audit(org_id, "default_admin_created", f"user:{email}", {"default_password": not config.admin_password})
    log.warning("Created the administrator account %s. Its password must be changed at first sign-in.", email)
