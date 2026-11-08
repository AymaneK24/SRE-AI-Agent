"""Root Cause Console: application setup."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import ai_settings
from .api import ai_routes, auth_routes, data_routes, docs_routes, hook_routes, org_routes, project_routes
from .api.deps import AppContext
from .auth import RateLimiter, ensure_default_admin, load_principal
from .config import Config
from .context.engine import EngineManager
from .costs import budget_problem
from .db import Database, utcnow
from .investigation.runner import InvestigationManager, Run
from .llm.base import ProviderError
from .mailer import Mailer
from .maintenance import maintenance_loop
from .notifications import Notifier
from .secrets_store import SecretBox
from .security import SecurityMiddleware, friendly_validation_error, strict_json

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("rcc.main")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": utcnow(), "level": record.levelname, "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)


class AutoInvestigator:
    """Starts a limited number of investigations when critical alerts arrive, if an organization turns this on."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._recent: dict[tuple[int, str], float] = {}

    async def handle(self, org_id: int, event: dict[str, Any], detail: dict[str, Any]) -> tuple[int, Any] | None:
        """Prepares an investigation if one should run. It starts only after the alert email is safely on hold, so a fast answer can't overtake it."""
        ctx = self.ctx
        settings = await ctx.db.get_setting(org_id, "auto_investigate", {}) or {}
        if not settings.get("enabled") or event.get("severity") != "critical":
            return None
        key = (org_id, event["fingerprint"])
        if time.monotonic() - self._recent.get(key, -3600) < 3600:
            return None
        if not ctx.limiter.allow(f"auto:{org_id}", int(settings.get("per_hour", 5)), 3600):
            return None
        if await budget_problem(ctx.db, org_id):
            return None
        try:
            selection = await ai_settings.select(ctx.db, org_id, ctx.box, ctx.config, None, None, None)
        except (ProviderError, ValueError) as error:
            log.warning("automatic investigation skipped: %s", getattr(error, "message", error))
            return None
        self._recent[key] = time.monotonic()
        title = str(event.get("title", "Critical alert"))[:90]
        now = utcnow()
        conversation_id = await ctx.db.insert(
            "INSERT INTO conversations(org_id, created_by, trigger, title, visibility, created_at, updated_at) VALUES (?, 'automatic', 'auto', ?, 'team', ?, ?)",
            (org_id, title, now, now),
        )
        ai = await ai_settings.load_settings(ctx.db, org_id, ctx.config)
        prompt = f"{title}. Find the root cause."

        def begin() -> None:
            ctx.investigations.start(org_id, conversation_id, prompt, selection, int(ai["max_rounds"]), actor="automatic")

        return conversation_id, begin

    async def finished(self, run: Run, result: dict[str, Any]) -> None:
        row = await self.ctx.db.one("SELECT title, trigger FROM conversations WHERE id = ?", (run.conversation_id,))
        if row and row["trigger"] == "auto":
            await self.ctx.notifier.notify_investigation(run.org_id, run.conversation_id, row["title"], result["answer"], result["verification"])

    async def failed(self, run: Run, result: dict[str, Any]) -> None:
        """The alert still has to reach people, without the investigation."""
        await self.ctx.notifier.release_held(run.conversation_id, "The automatic investigation couldn't finish, so this message has only the alert.", final=True)


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config()
    config.validate()
    configure_logging()
    db = Database(config.db_path, url=config.database_url)
    box = SecretBox(config)
    mailer = Mailer(config)
    notifier = Notifier(db, box, config, mailer)
    engines = EngineManager(db, box, config, notifier.handle_event)
    investigations = InvestigationManager(db, engines, box, config)
    ctx = AppContext(config=config, db=db, box=box, engines=engines, investigations=investigations, mailer=mailer,
                     notifier=notifier, limiter=RateLimiter())
    auto = AutoInvestigator(ctx)
    notifier.on_critical = auto.handle
    investigations.on_finished = auto.finished
    investigations.on_failed = auto.failed
    notifier.engines = engines

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await ensure_default_admin(db, config)
        await notifier.release_after_restart()
        await db.audit(0, "console_started", "", {"mode": config.mode, "database": "postgresql" if db.is_postgres else "sqlite"})
        await engines.start_active()
        maintenance = asyncio.create_task(maintenance_loop(db))
        yield
        maintenance.cancel()
        notifier.close()
        await investigations.stop_all()
        await engines.stop_all()
        db.close()

    app = FastAPI(title="Root Cause Console", version="3.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, dependencies=[Depends(strict_json)])
    app.state.ctx = ctx
    app.add_middleware(SecurityMiddleware, config=config)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": friendly_validation_error(list(error.errors()))}, status_code=422)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        await db.one("SELECT 1 AS ok")
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        # Scrapers use RCC_METRICS_TOKEN. People sign in as an owner.
        token = os.getenv("RCC_METRICS_TOKEN", "")
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        allowed = bool(token) and hmac.compare_digest(supplied.encode(), token.encode())
        if not allowed:
            principal = await load_principal(db, request)
            allowed = bool(principal and principal.can("owner"))
        if not allowed:
            raise HTTPException(401, "Sign in to continue.")
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    for module in (auth_routes, data_routes, ai_routes, org_routes, project_routes, hook_routes, docs_routes):
        app.include_router(module.router)

    # The documentation space has its own page. Its content comes from /api/docs, which needs a sign-in.
    @app.get("/docs", include_in_schema=False)
    @app.get("/docs/{slug}", include_in_schema=False)
    async def docs_page(slug: str = "") -> FileResponse:
        return FileResponse(STATIC_DIR / "docs.html", headers={"Cache-Control": "no-cache"})

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
