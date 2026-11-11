"""Shared request helpers: the application context and permission checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from ..auth import Principal, RateLimiter, load_principal
from ..config import Config
from ..context.engine import ContextEngine
from ..context.manager import EngineManager
from ..db import Database
from ..heartbeat import Heartbeat
from ..investigation.runner import InvestigationManager
from ..mailer import Mailer
from ..notifications import Notifier
from ..secrets_store import SecretBox

PASSWORD_CHANGE_PATHS = {"/api/auth/me", "/api/auth/change-password", "/api/auth/logout"}


@dataclass
class AppContext:
    config: Config
    db: Database
    box: SecretBox
    engines: EngineManager
    investigations: InvestigationManager
    mailer: Mailer
    notifier: Notifier
    limiter: RateLimiter
    heartbeat: Heartbeat


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def current_user(request: Request, ctx: Ctx) -> Principal:
    principal = await load_principal(ctx.db, request)
    if principal is None:
        raise HTTPException(401, "Sign in to continue.")
    if principal.must_change_password and request.url.path not in PASSWORD_CHANGE_PATHS:
        raise HTTPException(403, "password_change_required")
    return principal


User = Annotated[Principal, Depends(current_user)]


def require(role: str) -> Any:
    async def check(user: User) -> Principal:
        if not user.can(role):
            raise HTTPException(403, f"This needs the {role} role or higher.")
        return user

    return Depends(check)


Viewer = User
Engineer = Annotated[Principal, require("engineer")]
Admin = Annotated[Principal, require("admin")]
Owner = Annotated[Principal, require("owner")]


def can_read_conversation(user: Principal, row: dict[str, Any]) -> bool:
    """Investigations are private to whoever started them. Admins oversee all of them, and sharing opens one to engineers."""
    if user.can("admin") or row["owner_id"] == user.user_id:
        return True
    return user.can("engineer") and row["visibility"] == "team"


def can_change_conversation(user: Principal, row: dict[str, Any]) -> bool:
    """Automatic investigations belong to the on-call team. Anything else stays with its owner and the admins."""
    if user.can("admin") or row["owner_id"] == user.user_id:
        return True
    return user.can("engineer") and row["owner_id"] is None and row["visibility"] == "team"


async def engine_for(ctx: AppContext, user: Principal) -> ContextEngine:
    return await ctx.engines.get(user.org_id)
