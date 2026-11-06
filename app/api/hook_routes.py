"""Incoming webhooks from Alertmanager and deployment tools. The address itself is the secret."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..context.engine import store_change_event
from ..db import sha256_text, utcnow
from ..secrets_store import scrub
from .deps import AppContext, Ctx

router = APIRouter(prefix="/api/hooks")
MAX_BODY = 1_000_000


async def _org_from_token(ctx: AppContext, token: str) -> int:
    row = await ctx.db.one("SELECT org_id FROM settings WHERE key = 'hook_token_hash' AND value = ?", (json.dumps(sha256_text(token)),))
    if not row:
        raise HTTPException(404, "Unknown webhook address.")
    org = await ctx.db.one("SELECT status FROM organizations WHERE id = ?", (row["org_id"],))
    if not org or org["status"] != "active":
        raise HTTPException(403, "This organization is suspended.")
    return row["org_id"]


async def _json(request: Request) -> Any:
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "This webhook body is too large.")
    try:
        return json.loads(body)
    except ValueError as error:
        raise HTTPException(422, "Send the webhook body as JSON.") from error


@router.post("/alertmanager/{token}", status_code=202)
async def alertmanager(token: str, request: Request, ctx: Ctx) -> dict[str, int]:
    org_id = await _org_from_token(ctx, token)
    if not ctx.limiter.allow(f"hook:{org_id}", 120, 60):
        raise HTTPException(429, "Too many webhook calls.")
    payload = await _json(request)
    accepted = 0
    engine = await ctx.engines.get(org_id)
    for alert in (payload.get("alerts") or [])[:100]:
        if alert.get("status") != "firing":
            continue
        labels = {str(k): str(v) for k, v in (alert.get("labels") or {}).items()}
        annotations = alert.get("annotations") or {}
        name = labels.get("alertname", "Alert")
        severity = "critical" if labels.get("severity", "").lower() in {"critical", "page", "error"} else "warning"
        fingerprint = "alertmanager:" + str(alert.get("fingerprint") or sha256_text(json.dumps(labels, sort_keys=True)))
        detail = {"name": name, "severity": labels.get("severity", ""), "labels": labels, "summary": scrub(str(annotations.get("summary") or annotations.get("description") or ""))}
        # When a source is connected, its own poll sends the notification, so this avoids a duplicate.
        await engine._record_event("alertmanager_alert", severity, fingerprint, f"Alertmanager: {name}", detail, None, notify=not engine.states)
        await ctx.notifier.on_critical(org_id, {"title": f"Alertmanager: {name}", "severity": severity, "fingerprint": fingerprint}, detail)
        accepted += 1
    return {"accepted": accepted}


def _github(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    repository = (payload.get("repository") or {}).get("full_name", "")
    if event == "push":
        return {"service": repository, "title": f"Push to {payload.get('ref', '')} by {(payload.get('pusher') or {}).get('name', 'unknown')}: {str((payload.get('head_commit') or {}).get('message', ''))[:120]}", "origin": "GitHub"}
    if event == "deployment":
        deployment = payload.get("deployment") or {}
        return {"service": repository, "title": f"Deployment of {deployment.get('ref', '')} to {deployment.get('environment', '')}", "origin": "GitHub"}
    if event == "release":
        return {"service": repository, "title": f"Release {(payload.get('release') or {}).get('tag_name', '')} {payload.get('action', '')}", "origin": "GitHub"}
    if event == "workflow_run" and (payload.get("workflow_run") or {}).get("status") == "completed":
        run = payload["workflow_run"]
        return {"service": repository, "title": f"Workflow {run.get('name', '')} {run.get('conclusion', '')} on {run.get('head_branch', '')}", "origin": "GitHub"}
    return None


@router.post("/changes/{token}", status_code=202)
async def changes(token: str, request: Request, ctx: Ctx) -> dict[str, bool]:
    org_id = await _org_from_token(ctx, token)
    if not ctx.limiter.allow(f"hook:{org_id}", 120, 60):
        raise HTTPException(429, "Too many webhook calls.")
    payload = await _json(request)
    event = request.headers.get("x-github-event")
    if event == "ping":
        return {"stored": False}
    if event:
        item = _github(event, payload)
        if not item:
            return {"stored": False}
    elif request.headers.get("x-gitlab-event"):
        item = {"service": (payload.get("project") or {}).get("path_with_namespace", ""), "title": f"{payload.get('object_kind', 'event')} on {payload.get('ref', '')} by {payload.get('user_name', 'unknown')}", "origin": "GitLab"}
    else:
        title = str(payload.get("title") or "").strip()
        if not title:
            raise HTTPException(422, "Include a title for this change.")
        item = {"service": str(payload.get("service") or ""), "title": title, "origin": str(payload.get("origin") or "webhook"), "detail": payload.get("detail") or {}}
        if payload.get("ts"):
            item["ts"] = str(payload["ts"])
    item.setdefault("ts", utcnow())
    return {"stored": await store_change_event(ctx.db, org_id, item)}
