"""Organization administration: members, policies, usage, alerts, workflows, webhooks, audit, and SSO."""
from __future__ import annotations

import csv
import io
import json
import secrets
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..auth import DEFAULT_POLICIES, create_email_token, normalize_email, org_policies
from ..costs import spend_settings, usage_summary
from ..db import sha256_text, utcnow
from ..net_guard import BlockedTarget, check_url
from ..notifications import validate_channel
from ..validation import Label, PersonName
from ..workflows import new_run_steps, public_run, public_workflow, workflows_for
from .deps import Admin, AppContext, Ctx, Engineer, Owner, User

router = APIRouter(prefix="/api")
Role = Literal["viewer", "engineer", "admin", "owner"]


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    name: PersonName = Field(max_length=120)
    role: Role = "viewer"


class MemberPatch(BaseModel):
    role: Role | None = None
    is_active: bool | None = None


class PoliciesBody(BaseModel):
    password_min_length: int = Field(default=12, ge=12, le=64)
    session_hours: int = Field(default=12, ge=1, le=720)
    allowed_email_domains: list[str] = Field(default_factory=list, max_length=50)
    require_sso: bool = False
    allow_cloud_ai: bool = True


class SpendBody(BaseModel):
    monthly_budget_usd: float | None = Field(default=None, ge=0, le=1_000_000)
    max_tokens_per_investigation: int | None = Field(default=None, ge=1000, le=100_000_000)
    prices: dict[str, dict[str, float]] = Field(default_factory=dict)


class RetentionBody(BaseModel):
    retention_days: int = Field(ge=7, le=3650)


class ChannelBody(BaseModel):
    kind: Literal["slack", "email", "google_chat", "teams"]
    name: Label = Field(max_length=80)
    min_severity: Literal["info", "warning", "critical"] = "warning"
    url: str = Field(default="", max_length=500)
    recipients: list[str] = Field(default_factory=list, max_length=20)


class NoiseBody(BaseModel):
    """Only the fields sent change. The others keep their saved value."""
    grace_critical_s: int | None = Field(default=None, ge=0, le=3600)
    grace_warning_s: int | None = Field(default=None, ge=0, le=7200)
    group_window_s: int | None = Field(default=None, ge=0, le=900)
    max_per_hour: int | None = Field(default=None, ge=1, le=500)
    flapping: bool | None = None
    respect_verdicts: bool | None = None


class AutoInvestigateBody(BaseModel):
    enabled: bool = False
    per_hour: int = Field(default=5, ge=1, le=60)


class WorkflowStep(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=2000)
    command: str = Field(default="", max_length=2000)
    risk: str = Field(default="none", max_length=10)
    undo: str = Field(default="", max_length=1000)
    verify: str = Field(default="", max_length=1000)


class WorkflowBody(BaseModel):
    name: Label = Field(max_length=120)
    summary: str = Field(default="", max_length=500)
    risk: Literal["low", "medium", "high"] = "medium"
    match_rules: dict[str, list[str]] = Field(default_factory=dict)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=30)
    # Where this runbook applies: a project, and the servers or workloads it was written for (host names, IPs, workload names).
    project_id: int | None = None
    targets: list[Label] = Field(default_factory=list, max_length=20)


class WorkflowStatusBody(BaseModel):
    status: Literal["draft", "approved", "archived"]


class RunStartBody(BaseModel):
    conversation_id: int | None = None


class RunStepBody(BaseModel):
    action: Literal["done", "skipped", "reopen"]
    note: str = Field(default="", max_length=2000)
    # Steps that change something need the person to say they've read the risk and how to undo it.
    risk_accepted: bool = False


class RunFinishBody(BaseModel):
    outcome: Literal["helped", "partly", "did_not_help", "abandoned"]
    note: str = Field(default="", max_length=2000)


class SsoBody(BaseModel):
    enabled: bool = False
    issuer: str = Field(default="", max_length=300)
    client_id: str = Field(default="", max_length=300)
    client_secret: str = Field(default="", max_length=1000)
    domains: list[str] = Field(default_factory=list, max_length=20)
    default_role: Literal["viewer", "engineer"] = "viewer"


# Members --------------------------------------------------------------------
@router.get("/org")
async def get_org(user: User, ctx: Ctx) -> dict[str, Any]:
    if not user.can("admin"):
        return {"name": user.org_name}
    org = await ctx.db.one("SELECT id, name, slug, status, contact_email, created_at FROM organizations WHERE id = ?", (user.org_id,))
    return {**org, "policies": await org_policies(ctx.db, user.org_id)}


@router.get("/org/members")
async def members(user: Admin, ctx: Ctx) -> list[dict[str, Any]]:
    rows = await ctx.db.all(
        "SELECT id, email, name, role, is_active, email_verified_at, sso_subject, last_login_at, created_at, password_hash FROM users WHERE org_id = ? ORDER BY id", (user.org_id,))
    output = []
    for row in rows:
        pending = not row.pop("password_hash") and not row["sso_subject"]
        output.append({**row, "is_active": bool(row["is_active"]), "pending_invite": pending, "uses_sso": bool(row.pop("sso_subject"))})
    return output


@router.post("/org/members/invite", status_code=201)
async def invite(body: InviteBody, user: Admin, ctx: Ctx) -> dict[str, Any]:
    if body.role == "owner" and not user.can("owner"):
        raise HTTPException(403, "Only an owner can invite another owner.")
    email = normalize_email(body.email)
    policies = await org_policies(ctx.db, user.org_id)
    domains = [item.lower() for item in policies.get("allowed_email_domains", [])]
    if domains and email.split("@")[-1] not in domains:
        raise HTTPException(422, f"Your organization's policy only allows these email domains: {', '.join(domains)}.")
    if await ctx.db.one("SELECT id FROM users WHERE email = ?", (email,)):
        raise HTTPException(409, "This email address already has an account.")
    user_id = await ctx.db.insert("INSERT INTO users(org_id, username, email, name, role, is_active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)", (user.org_id, email, email, body.name, body.role, utcnow()))
    token = await create_email_token(ctx.db, "invite", user_id, 72)
    await ctx.mailer.send(email, f"Join {user.org_name} on Root Cause Console",
                          f"Hello {body.name},\n\n{user.name} has invited you to join {user.org_name} on Root Cause Console, with the {body.role} role.\n\n"
                          "To accept, choose a password using the button below. This invitation is personal to you and expires in 72 hours.",
                          f"{ctx.config.public_url}/#/accept?token={token}", "Accept invitation",
                          security_notice=True, subtitle=f"An invitation from {user.name}", title=f"Join {user.org_name}")
    await ctx.db.audit(user.org_id, "member_invited", f"user:{user_id}", {"email": email, "role": body.role}, actor=user.actor)
    result = {"id": user_id, "email_sent": ctx.mailer.configured}
    if not ctx.mailer.configured:
        # With no email server on this network, the admin passes the link on directly.
        result["invite_link"] = f"{ctx.config.public_url}/#/accept?token={token}"
    return result


async def _other_owners(ctx: AppContext, org_id: int, user_id: int) -> int:
    row = await ctx.db.one("SELECT count(*) AS n FROM users WHERE org_id = ? AND role = 'owner' AND is_active = 1 AND id <> ?", (org_id, user_id))
    return row["n"]


@router.patch("/org/members/{member_id}")
async def update_member(member_id: int, body: MemberPatch, user: Admin, ctx: Ctx) -> dict[str, bool]:
    target = await ctx.db.one("SELECT * FROM users WHERE id = ? AND org_id = ?", (member_id, user.org_id))
    if not target:
        raise HTTPException(404, "This member no longer exists.")
    if (target["role"] == "owner" or body.role == "owner") and not user.can("owner"):
        raise HTTPException(403, "Only an owner can change an owner's access.")
    losing_owner = target["role"] == "owner" and ((body.role and body.role != "owner") or body.is_active is False)
    if losing_owner and not await _other_owners(ctx, user.org_id, member_id):
        raise HTTPException(409, "An organization needs at least one active owner.")
    if body.role:
        await ctx.db.execute("UPDATE users SET role = ? WHERE id = ? AND org_id = ?", (body.role, member_id, user.org_id))
    if body.is_active is not None:
        await ctx.db.execute("UPDATE users SET is_active = ? WHERE id = ? AND org_id = ?", (int(body.is_active), member_id, user.org_id))
    if body.is_active is False or body.role:
        await ctx.db.execute("DELETE FROM sessions WHERE user_id = ?", (member_id,))
    await ctx.db.audit(user.org_id, "member_updated", f"user:{member_id}", body.model_dump(exclude_none=True), actor=user.actor)
    return {"updated": True}


@router.delete("/org/members/{member_id}", status_code=204)
async def remove_member(member_id: int, user: Admin, ctx: Ctx, delete_investigations: bool = False) -> Response:
    """Removes a person. With delete_investigations, also deletes their private investigations, for a deletion request."""
    target = await ctx.db.one("SELECT * FROM users WHERE id = ? AND org_id = ?", (member_id, user.org_id))
    if not target:
        raise HTTPException(404, "This member no longer exists.")
    if target["is_platform_admin"]:
        raise HTTPException(409, "The platform administrator can't be removed.")
    if target["role"] == "owner" and (not user.can("owner") or not await _other_owners(ctx, user.org_id, member_id)):
        raise HTTPException(409, "An organization needs at least one active owner.")
    deleted = 0
    if delete_investigations:
        private = await ctx.db.all(
            "SELECT id FROM conversations WHERE org_id = ? AND owner_id = ? AND visibility = 'private' AND trigger = 'manual'", (user.org_id, member_id))
        for row in private:
            if ctx.investigations.active_run(row["id"]):
                raise HTTPException(409, "One of their investigations is still running. Stop it first.")
        for row in private:
            await ctx.db.unindex(row["id"])
            await ctx.db.execute("DELETE FROM conversations WHERE id = ? AND org_id = ?", (row["id"], user.org_id))
        deleted = len(private)
    await ctx.db.execute("DELETE FROM users WHERE id = ? AND org_id = ?", (member_id, user.org_id))
    await ctx.db.audit(user.org_id, "member_removed", f"user:{member_id}", {"email": target["email"], "private_investigations_deleted": deleted}, actor=user.actor)
    return Response(status_code=204)


# Policies and usage ---------------------------------------------------------
@router.put("/org/policies")
async def put_policies(body: PoliciesBody, user: Owner, ctx: Ctx) -> dict[str, Any]:
    values = {**DEFAULT_POLICIES, **body.model_dump()}
    values["allowed_email_domains"] = [item.strip().lower() for item in body.allowed_email_domains if item.strip()]
    if values["require_sso"]:
        sso = await ctx.db.get_setting(user.org_id, "sso", {})
        if not sso or not sso.get("enabled"):
            raise HTTPException(422, "Set up single sign-on before requiring it.")
    await ctx.db.execute("UPDATE organizations SET policies = ? WHERE id = ?", (json.dumps(values), user.org_id))
    await ctx.db.audit(user.org_id, "security_policies_changed", "organization", values, actor=user.actor)
    return values


@router.get("/org/usage")
async def usage(user: Admin, ctx: Ctx) -> dict[str, Any]:
    return await usage_summary(ctx.db, user.org_id)


@router.put("/org/spend")
async def put_spend(body: SpendBody, user: Admin, ctx: Ctx) -> dict[str, Any]:
    await ctx.db.set_setting(user.org_id, "spend", body.model_dump())
    await ctx.db.audit(user.org_id, "spend_limits_changed", "organization", {"monthly_budget_usd": body.monthly_budget_usd, "max_tokens_per_investigation": body.max_tokens_per_investigation}, actor=user.actor)
    return await spend_settings(ctx.db, user.org_id)


@router.get("/org/retention")
async def get_retention(user: Admin, ctx: Ctx) -> dict[str, int]:
    return {"retention_days": int(await ctx.db.get_setting(user.org_id, "retention_days", 365))}


@router.put("/org/retention")
async def put_retention(body: RetentionBody, user: Owner, ctx: Ctx) -> dict[str, int]:
    await ctx.db.set_setting(user.org_id, "retention_days", body.retention_days)
    await ctx.db.audit(user.org_id, "retention_changed", "organization", {"retention_days": body.retention_days}, actor=user.actor)
    return {"retention_days": body.retention_days}


@router.delete("/org", status_code=204)
async def delete_org(request: Request, user: Owner, ctx: Ctx) -> Response:
    body = await request.json()
    org = await ctx.db.one("SELECT name FROM organizations WHERE id = ?", (user.org_id,))
    if ctx.config.single_company:
        raise HTTPException(409, "This installation has one organization, so it can't be deleted here. Remove the installation instead.")
    if user.is_platform_admin:
        raise HTTPException(409, "The platform administrator's organization can't be deleted.")
    if body.get("confirm_name") != org["name"]:
        raise HTTPException(422, "Type the organization's exact name to confirm.")
    await ctx.engines.remove(user.org_id)
    for table in ("project_reports", "projects", "settings", "events", "change_events", "context_snapshots", "deliveries", "search_index", "llm_calls", "tool_calls", "workflow_records"):
        await ctx.db.execute(f"DELETE FROM {table} WHERE org_id = ?", (user.org_id,))  # noqa: S608
    await ctx.db.execute("DELETE FROM organizations WHERE id = ?", (user.org_id,))
    await ctx.db.audit(0, "organization_deleted", f"org:{user.org_id}", {"name": org["name"]}, actor=user.actor)
    return Response(status_code=204)


# Alert channels -------------------------------------------------------------
def public_channel(ctx: AppContext, row: dict[str, Any]) -> dict[str, Any]:
    settings = ctx.box.decrypt_json(row["config_enc"]) if row["config_enc"] else {}
    return {"id": row["id"], "kind": row["kind"], "name": row["name"], "enabled": bool(row["enabled"]), "min_severity": row["min_severity"],
            "recipients": settings.get("recipients", []), "has_url": bool(settings.get("url")), "created_at": row["created_at"]}


@router.get("/org/channels")
async def list_channels(user: Admin, ctx: Ctx) -> list[dict[str, Any]]:
    rows = await ctx.db.all("SELECT * FROM channels WHERE org_id = ? ORDER BY id", (user.org_id,))
    return [public_channel(ctx, row) for row in rows]


@router.post("/org/channels", status_code=201)
async def create_channel(body: ChannelBody, user: Admin, ctx: Ctx) -> dict[str, Any]:
    settings: dict[str, Any] = {"recipients": [normalize_email(item) for item in body.recipients]} if body.kind == "email" else {"url": body.url.strip()}
    problem = await validate_channel(ctx.db, ctx.config, user.org_id, body.kind, settings)
    if problem:
        raise HTTPException(422, problem)
    now = utcnow()
    channel_id = await ctx.db.insert(
        "INSERT INTO channels(org_id, kind, name, min_severity, config_enc, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user.org_id, body.kind, body.name, body.min_severity, ctx.box.encrypt_json(settings), now, now),
    )
    await ctx.db.audit(user.org_id, "alert_channel_created", f"channel:{channel_id}", {"kind": body.kind, "name": body.name}, actor=user.actor)
    return public_channel(ctx, await ctx.db.one("SELECT * FROM channels WHERE id = ?", (channel_id,)))


@router.delete("/org/channels/{channel_id}", status_code=204)
async def delete_channel(channel_id: int, user: Admin, ctx: Ctx) -> Response:
    await ctx.db.execute("DELETE FROM channels WHERE id = ? AND org_id = ?", (channel_id, user.org_id))
    await ctx.db.audit(user.org_id, "alert_channel_deleted", f"channel:{channel_id}", {}, actor=user.actor)
    return Response(status_code=204)


@router.post("/org/channels/{channel_id}/test")
async def test_channel(channel_id: int, user: Admin, ctx: Ctx) -> dict[str, Any]:
    row = await ctx.db.one("SELECT * FROM channels WHERE id = ? AND org_id = ?", (channel_id, user.org_id))
    if not row:
        raise HTTPException(404, "This channel no longer exists.")
    message = {"subject": "[TEST] Root Cause Console alert channel", "text": "This is a test from Root Cause Console. Alerts for your organization will arrive here.\nRead-only notification. Root Cause Console didn't change anything."}
    return await ctx.notifier.deliver(row, user.org_id, "test", message)


@router.get("/org/deliveries")
async def deliveries(user: Admin, ctx: Ctx, limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    return await ctx.db.all("SELECT * FROM deliveries WHERE org_id = ? ORDER BY id DESC LIMIT ?", (user.org_id, limit))


@router.get("/org/auto-investigate")
async def get_auto(user: Admin, ctx: Ctx) -> dict[str, Any]:
    return await ctx.db.get_setting(user.org_id, "auto_investigate", {"enabled": False, "per_hour": 5})


@router.put("/org/auto-investigate")
async def put_auto(body: AutoInvestigateBody, user: Admin, ctx: Ctx) -> dict[str, Any]:
    await ctx.db.set_setting(user.org_id, "auto_investigate", body.model_dump())
    await ctx.db.audit(user.org_id, "auto_investigate_changed", "organization", body.model_dump(), actor=user.actor)
    return body.model_dump()


@router.get("/org/noise")
async def get_noise(user: Admin, ctx: Ctx) -> dict[str, Any]:
    """Noise control settings, and what they held back in the last 7 days."""
    from .. import noise
    from ..db import utc_after

    since = utc_after(days=-7)
    started = await ctx.db.one("SELECT count(*) AS n FROM events WHERE org_id = ? AND kind LIKE '%_started' AND ts >= ? AND title NOT LIKE 'Already active%'",
                               (user.org_id, since))
    rows = await ctx.db.all("SELECT status, count(*) AS n FROM deliveries WHERE org_id = ? AND created_at >= ? GROUP BY status", (user.org_id, since))
    counts = {row["status"]: int(row["n"]) for row in rows}
    held = {key.split(":", 1)[1]: value for key, value in counts.items() if key.startswith("suppressed:")}
    return {**await noise.load(ctx.db, user.org_id, ctx.config),
            "week": {"problems_started": int(started["n"] or 0), "messages_sent": counts.get("sent", 0), "failed": counts.get("failed", 0),
                     "held_back": held, "held_back_total": sum(held.values())}}


@router.put("/org/noise")
async def put_noise(body: NoiseBody, user: Admin, ctx: Ctx) -> dict[str, Any]:
    from .. import noise
    changes = body.model_dump(exclude_none=True)
    settings = {**await noise.load(ctx.db, user.org_id, ctx.config), **changes}
    await ctx.db.set_setting(user.org_id, "noise", settings)
    await ctx.db.audit(user.org_id, "noise_control_changed", "organization", changes, actor=user.actor)
    return await get_noise(user, ctx)


# Workflows ------------------------------------------------------------------
async def _own_workflow(ctx: AppContext, org_id: int, workflow_id: int) -> dict[str, Any]:
    row = await ctx.db.one("SELECT * FROM workflows WHERE id = ? AND org_id = ?", (workflow_id, org_id))
    if not row:
        raise HTTPException(404, "This workflow no longer exists.")
    return row


async def _check_project(ctx: AppContext, org_id: int, project_id: int | None) -> None:
    if project_id is not None and not await ctx.db.one("SELECT id FROM projects WHERE id = ? AND org_id = ?", (project_id, org_id)):
        raise HTTPException(422, "That project no longer exists.")


@router.get("/workflows")
async def list_workflows(user: User, ctx: Ctx) -> list[dict[str, Any]]:
    # Viewers can study reviewed runbooks, but not drafts, and they can't follow or change them.
    status_filter = "status <> 'archived'" if user.can("engineer") else "status = 'approved'"
    rows = await ctx.db.all(f"SELECT * FROM workflows WHERE org_id = ? AND {status_filter} ORDER BY status, name", (user.org_id,))  # noqa: S608
    tally = await ctx.db.all("SELECT workflow_id, outcome, count(*) AS n FROM workflow_records WHERE org_id = ? GROUP BY workflow_id, outcome", (user.org_id,))
    stats: dict[int, dict[str, int]] = {}
    for item in tally:
        stats.setdefault(item["workflow_id"], {})[item["outcome"]] = item["n"]
    return [{**public_workflow(row), "results": stats.get(row["id"], {})} for row in rows]


@router.get("/workflows/match")
async def match_workflows(user: User, ctx: Ctx, text: str = "", alert: str = "", signal: str = "") -> list[dict[str, Any]]:
    found = await workflows_for(ctx.db, user.org_id, alert_names=[alert] if alert else [], signal_ids=[signal] if signal else [], text=text)
    return found if user.can("engineer") else [item for item in found if item["status"] == "approved"]


@router.post("/workflows", status_code=201)
async def create_workflow(body: WorkflowBody, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    await _check_project(ctx, user.org_id, body.project_id)
    now = utcnow()
    workflow_id = await ctx.db.insert(
        "INSERT INTO workflows(org_id, name, summary, match_rules, risk, steps, status, origin, created_by, created_at, updated_at, project_id, targets) "
        "VALUES (?, ?, ?, ?, ?, ?, 'draft', 'manual', ?, ?, ?, ?, ?)",
        (user.org_id, body.name, body.summary, json.dumps(body.match_rules), body.risk, json.dumps([step.model_dump() for step in body.steps]), user.actor, now, now,
         body.project_id, json.dumps(sorted(set(body.targets)))),
    )
    await ctx.db.audit(user.org_id, "workflow_created", f"workflow:{workflow_id}", {"name": body.name}, actor=user.actor)
    return public_workflow(await _own_workflow(ctx, user.org_id, workflow_id))


@router.put("/workflows/{workflow_id}")
async def update_workflow(workflow_id: int, body: WorkflowBody, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    await _own_workflow(ctx, user.org_id, workflow_id)
    await _check_project(ctx, user.org_id, body.project_id)
    # Any edit returns the workflow to draft, so changed steps are reviewed again.
    await ctx.db.execute(
        "UPDATE workflows SET name = ?, summary = ?, match_rules = ?, risk = ?, steps = ?, status = 'draft', reviewed_by = '', updated_at = ?, project_id = ?, targets = ? "
        "WHERE id = ? AND org_id = ?",
        (body.name, body.summary, json.dumps(body.match_rules), body.risk, json.dumps([step.model_dump() for step in body.steps]), utcnow(),
         body.project_id, json.dumps(sorted(set(body.targets))), workflow_id, user.org_id),
    )
    await ctx.db.audit(user.org_id, "workflow_updated", f"workflow:{workflow_id}", {"name": body.name}, actor=user.actor)
    return public_workflow(await _own_workflow(ctx, user.org_id, workflow_id))


@router.post("/workflows/{workflow_id}/status")
async def set_workflow_status(workflow_id: int, body: WorkflowStatusBody, user: Admin, ctx: Ctx) -> dict[str, Any]:
    await _own_workflow(ctx, user.org_id, workflow_id)
    await ctx.db.execute("UPDATE workflows SET status = ?, reviewed_by = ?, updated_at = ? WHERE id = ? AND org_id = ?",
                         (body.status, user.actor if body.status == "approved" else "", utcnow(), workflow_id, user.org_id))
    await ctx.db.audit(user.org_id, f"workflow_{body.status}", f"workflow:{workflow_id}", {}, actor=user.actor)
    return public_workflow(await _own_workflow(ctx, user.org_id, workflow_id))


@router.get("/workflows/{workflow_id}/records")
async def workflow_records(workflow_id: int, user: Engineer, ctx: Ctx) -> list[dict[str, Any]]:
    await _own_workflow(ctx, user.org_id, workflow_id)
    return await ctx.db.all("SELECT actor, note, outcome, created_at FROM workflow_records WHERE workflow_id = ? AND org_id = ? ORDER BY id DESC LIMIT 50", (workflow_id, user.org_id))


# Following a workflow, one step at a time. All of this only records what people did by hand. Nothing here runs anything.
async def _own_run(ctx: AppContext, org_id: int, run_id: int) -> dict[str, Any]:
    row = await ctx.db.one("SELECT * FROM workflow_runs WHERE id = ? AND org_id = ?", (run_id, org_id))
    if not row:
        raise HTTPException(404, "This run no longer exists.")
    return row


@router.get("/workflows/{workflow_id}/runs")
async def list_workflow_runs(workflow_id: int, user: Engineer, ctx: Ctx) -> list[dict[str, Any]]:
    await _own_workflow(ctx, user.org_id, workflow_id)
    rows = await ctx.db.all("SELECT * FROM workflow_runs WHERE workflow_id = ? AND org_id = ? ORDER BY id DESC LIMIT 20", (workflow_id, user.org_id))
    return [public_run(row) for row in rows]


@router.post("/workflows/{workflow_id}/runs", status_code=201)
async def start_workflow_run(workflow_id: int, body: RunStartBody, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    workflow = await _own_workflow(ctx, user.org_id, workflow_id)
    if workflow["status"] != "approved":
        raise HTTPException(409, "This workflow hasn't been reviewed yet. An admin needs to review it before anyone follows it.")
    if await ctx.db.one("SELECT id FROM workflow_runs WHERE workflow_id = ? AND org_id = ? AND status = 'running'", (workflow_id, user.org_id)):
        raise HTTPException(409, "Someone is already following this workflow. Continue that run, or finish it first.")
    now = utcnow()
    run_id = await ctx.db.insert(
        "INSERT INTO workflow_runs(org_id, workflow_id, workflow_name, conversation_id, steps, started_by, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user.org_id, workflow_id, workflow["name"], body.conversation_id, json.dumps(new_run_steps(json.loads(workflow["steps"]))), user.actor, now, now),
    )
    await ctx.db.audit(user.org_id, "workflow_run_started", f"workflow:{workflow_id}", {"run": run_id}, actor=user.actor)
    return public_run(await _own_run(ctx, user.org_id, run_id))


@router.post("/workflow-runs/{run_id}/steps/{index}")
async def update_run_step(run_id: int, index: int, body: RunStepBody, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    run = await _own_run(ctx, user.org_id, run_id)
    if run["status"] != "running":
        raise HTTPException(409, "This run has finished.")
    steps = json.loads(run["steps"])
    if not 0 <= index < len(steps):
        raise HTTPException(404, "This step doesn't exist.")
    pending = next((position for position, step in enumerate(steps) if step["state"] == "pending"), len(steps))
    step = steps[index]
    if body.action == "reopen":
        if index != pending - 1:
            raise HTTPException(409, "Only the most recent step can be reopened.")
        step.update(state="pending", note="", by="", at=None)
    else:
        if index != pending:
            raise HTTPException(409, "Complete the steps in order. The next step is number " + str(pending + 1) + ".")
        if body.action == "skipped" and not body.note.strip():
            raise HTTPException(422, "Say why you're skipping this step.")
        if body.action == "done" and step.get("risk") in {"medium", "high"} and not body.risk_accepted:
            raise HTTPException(422, "This step changes something. Confirm that you've read its risk and how to undo it.")
        step.update(state=body.action, note=body.note.strip(), by=user.actor, at=utcnow())
    await ctx.db.execute("UPDATE workflow_runs SET steps = ?, updated_at = ? WHERE id = ? AND org_id = ?", (json.dumps(steps), utcnow(), run_id, user.org_id))
    await ctx.db.audit(user.org_id, f"workflow_step_{body.action}", f"workflow:{run['workflow_id']}", {"run": run_id, "step": index + 1, "title": step["title"]}, actor=user.actor)
    return public_run(await _own_run(ctx, user.org_id, run_id))


@router.post("/workflow-runs/{run_id}/finish")
async def finish_workflow_run(run_id: int, body: RunFinishBody, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    run = await _own_run(ctx, user.org_id, run_id)
    if run["status"] != "running":
        raise HTTPException(409, "This run has already finished.")
    complete = all(step["state"] != "pending" for step in json.loads(run["steps"]))
    if body.outcome != "abandoned" and not complete:
        raise HTTPException(409, "Finish or skip every step first, or end this run as abandoned.")
    now = utcnow()
    await ctx.db.execute("UPDATE workflow_runs SET status = ?, outcome = ?, note = ?, finished_by = ?, finished_at = ?, updated_at = ? WHERE id = ? AND org_id = ?",
                         ("abandoned" if body.outcome == "abandoned" else "finished", body.outcome, body.note.strip(), user.actor, now, now, run_id, user.org_id))
    if body.outcome != "abandoned":
        await ctx.db.insert("INSERT INTO workflow_records(org_id, workflow_id, actor, note, outcome, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (user.org_id, run["workflow_id"], user.actor, body.note.strip(), body.outcome, now))
    await ctx.db.audit(user.org_id, "workflow_run_finished", f"workflow:{run['workflow_id']}", {"run": run_id, "outcome": body.outcome}, actor=user.actor)
    return public_run(await _own_run(ctx, user.org_id, run_id))


# Webhook addresses ----------------------------------------------------------
@router.get("/org/webhooks")
async def get_webhooks(user: Admin, ctx: Ctx) -> dict[str, Any]:
    has = await ctx.db.get_setting(user.org_id, "hook_token_hash", "")
    return {"configured": bool(has)}


@router.post("/org/webhooks/rotate")
async def rotate_webhooks(user: Admin, ctx: Ctx) -> dict[str, str]:
    token = secrets.token_urlsafe(32)
    await ctx.db.set_setting(user.org_id, "hook_token_hash", sha256_text(token))
    await ctx.db.audit(user.org_id, "webhook_address_rotated", "organization", {}, actor=user.actor)
    base = ctx.config.public_url
    return {"alertmanager": f"{base}/api/hooks/alertmanager/{token}", "changes": f"{base}/api/hooks/changes/{token}"}


# SSO ------------------------------------------------------------------------
@router.get("/org/sso")
async def get_sso(user: Owner, ctx: Ctx) -> dict[str, Any]:
    settings = await ctx.db.get_setting(user.org_id, "sso", {}) or {}
    return {"enabled": bool(settings.get("enabled")), "issuer": settings.get("issuer", ""), "client_id": settings.get("client_id", ""),
            "domains": settings.get("domains", []), "default_role": settings.get("default_role", "viewer"), "has_secret": bool(settings.get("client_secret_enc")),
            "redirect_uri": f"{ctx.config.public_url}/api/auth/sso/callback"}


@router.put("/org/sso")
async def put_sso(body: SsoBody, user: Owner, ctx: Ctx) -> dict[str, Any]:
    current = await ctx.db.get_setting(user.org_id, "sso", {}) or {}
    domains = [item.strip().lower() for item in body.domains if item.strip()]
    if body.enabled:
        if not body.issuer or not body.client_id or not domains:
            raise HTTPException(422, "Enter the issuer, client ID, and at least one email domain.")
        try:
            await check_url(body.issuer, ctx.config.target_policy)
        except BlockedTarget as error:
            raise HTTPException(422, str(error)) from error
        rows = await ctx.db.all("SELECT org_id, value FROM settings WHERE key = 'sso' AND org_id <> ?", (user.org_id,))
        for row in rows:
            taken = json.loads(row["value"])
            if taken.get("enabled") and set(domains) & {item.lower() for item in taken.get("domains", [])}:
                raise HTTPException(409, "Another organization already uses one of these email domains for single sign-on.")
        # Proof of domain ownership: the domain must match this owner's verified email.
        if not user.is_platform_admin and user.email.split("@")[-1] not in domains:
            raise HTTPException(422, "Include your own verified email domain, because it shows you control this organization's domain.")
    values = {"enabled": body.enabled, "issuer": body.issuer.rstrip("/"), "client_id": body.client_id, "domains": domains, "default_role": body.default_role,
              "client_secret_enc": ctx.box.encrypt(body.client_secret) if body.client_secret else current.get("client_secret_enc", "")}
    await ctx.db.set_setting(user.org_id, "sso", values)
    await ctx.db.audit(user.org_id, "sso_changed", "organization", {"enabled": body.enabled, "issuer": values["issuer"], "domains": domains}, actor=user.actor)
    return await get_sso(user, ctx)


# Audit ----------------------------------------------------------------------
@router.get("/audit")
async def list_audit(user: Admin, ctx: Ctx, limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    return await ctx.db.all("SELECT * FROM audit_log WHERE org_id = ? ORDER BY id DESC LIMIT ?", (user.org_id, limit))


@router.get("/audit/verify")
async def verify_audit(user: Admin, ctx: Ctx) -> dict[str, Any]:
    return await ctx.db.verify_audit(user.org_id)


@router.get("/audit/export")
async def export_audit(user: Admin, ctx: Ctx, format: Literal["csv", "json"] = "csv") -> Response:
    rows = await ctx.db.all("SELECT id, ts, actor, action, target, detail, prev_hash, hash FROM audit_log WHERE org_id = ? ORDER BY id", (user.org_id,))
    await ctx.db.audit(user.org_id, "audit_log_exported", "organization", {"format": format, "entries": len(rows)}, actor=user.actor)
    if format == "json":
        return Response(json.dumps(rows, ensure_ascii=False, indent=1), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="audit-log.json"'})
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["id", "ts", "actor", "action", "target", "detail", "prev_hash", "hash"])
    writer.writeheader()
    for row in rows:
        # A leading =, +, -, @, tab, or return would run as a formula in a spreadsheet.
        writer.writerow({key: ("'" + value if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r") else value) for key, value in row.items()})
    return PlainTextResponse(buffer.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'})


__all__ = ["router"]
