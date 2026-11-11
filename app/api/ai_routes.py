"""Model settings, investigations, and prompt logs."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated, Any, AsyncIterator, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import ai_settings
from ..costs import budget_problem, compute_cost, month_start, spend_settings
from ..db import sha256_text, utcnow
from ..llm.base import Finished, ProviderError, TextDelta
from ..llm.claude import FALLBACK_MODELS
from ..llm.ollama import OllamaProvider
from ..net_guard import BlockedTarget, check_url
from ..workflows import DRAFT_PROMPT, parse_draft, public_workflow, workflows_for
from .deps import Admin, AppContext, Ctx, Engineer, User, can_change_conversation, can_read_conversation

router = APIRouter(prefix="/api")


class OllamaSettings(BaseModel):
    url: str = Field(min_length=1, max_length=300, pattern=r"^https?://[^\s/@]+(:\d+)?/?$")
    model: str = Field(default="", max_length=200)
    num_ctx: int = Field(default=8192, ge=2048, le=1048576)
    use_tools: bool = False
    strict_answers: bool = False
    small_model: Literal["auto", "on", "off"] = "auto"


class ClaudeSettings(BaseModel):
    model: str = Field(default="claude-sonnet-5-5", max_length=200)
    max_tokens: int = Field(default=4096, ge=256, le=64000)
    use_tools: bool = True
    consent: bool = False
    strict_answers: bool = True


class GeminiSettings(BaseModel):
    model: str = Field(default="", max_length=200)
    max_tokens: int = Field(default=8192, ge=256, le=65536)
    use_tools: bool = True
    consent: bool = False
    strict_answers: bool = True


ProviderName = Literal["ollama", "claude", "gemini"]
CloudProvider = Literal["claude", "gemini"]


class AiSettingsBody(BaseModel):
    provider: ProviderName
    max_rounds: int = Field(default=6, ge=1, le=12)
    ollama: OllamaSettings
    claude: ClaudeSettings
    # Optional, so a client that doesn't know about Gemini keeps the saved Gemini settings.
    gemini: GeminiSettings | None = None


class ApiKeyBody(BaseModel):
    api_key: str = Field(min_length=10, max_length=512)


class ConversationBody(BaseModel):
    title: str = Field(default="", max_length=200)


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    status: Literal["open", "resolved", "false_alarm"] | None = None
    root_cause_note: str | None = Field(default=None, max_length=5000)


class MessageBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    provider: ProviderName | None = None
    model: str | None = Field(default=None, max_length=200)
    use_tools: bool | None = None
    # Fingerprints of the problems chosen in the live context. Only their evidence goes to the model.
    focus: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=20)
    answer_style: Literal["short", "report"] | None = None


class SavingsBody(BaseModel):
    prompt_caching: bool = True
    gather_evidence: bool = True
    answer_style: Literal["short", "report"] = "short"
    evidence_model: dict[Literal["claude", "gemini"], Annotated[str, Field(max_length=200)]] = Field(default_factory=dict)


class ShareBody(BaseModel):
    shared: bool


@router.get("/ai/settings")
async def get_ai_settings(user: Engineer, ctx: Ctx) -> dict[str, Any]:
    settings = await ai_settings.load_settings(ctx.db, user.org_id, ctx.config)
    sources = {}
    for name in ai_settings.CLOUD_PROVIDERS:
        try:
            _, sources[name] = await ai_settings.cloud_key(ctx.db, user.org_id, ctx.box, ctx.config, name)
        except ValueError:
            sources[name] = "unreadable"
    if not user.can("admin"):
        # Engineers pick a model, and that's all they need: no server address or key details.
        output = {"provider": settings["provider"], "ollama": {"model": settings["ollama"]["model"], "use_tools": settings["ollama"]["use_tools"]}}
        for name in ai_settings.CLOUD_PROVIDERS:
            output[name] = {"model": settings[name]["model"], "use_tools": settings[name]["use_tools"], "consent": settings[name]["consent"],
                            "key_configured": sources[name] in {"saved", "environment"}}
        return output
    for name in ai_settings.CLOUD_PROVIDERS:
        settings[name]["key_source"] = sources[name]
        settings[name]["key_configured"] = sources[name] in {"saved", "environment"}
    return settings


@router.put("/ai/settings")
async def put_ai_settings(body: AiSettingsBody, user: Admin, ctx: Ctx) -> dict[str, Any]:
    previous = await ai_settings.load_settings(ctx.db, user.org_id, ctx.config)
    payload = body.model_dump()
    if payload["gemini"] is None:
        payload["gemini"] = {key: previous["gemini"][key] for key in GeminiSettings.model_fields}
    if payload["provider"] in ai_settings.CLOUD_PROVIDERS and not payload[payload["provider"]]["consent"]:
        company = ai_settings.CLOUD_PROVIDERS[payload["provider"]]
        raise HTTPException(422, f"Allow sending telemetry to {company} before choosing {payload['provider'].capitalize()}.")
    if payload["ollama"]["url"] != ctx.config.default_ollama_url:
        try:
            await check_url(payload["ollama"]["url"], ctx.config.target_policy)
        except BlockedTarget as error:
            raise HTTPException(422, str(error)) from error
    await ctx.db.set_setting(user.org_id, "ai", payload)
    for name in ai_settings.CLOUD_PROVIDERS:
        if previous[name]["consent"] != payload[name]["consent"]:
            await ctx.db.audit(user.org_id, "cloud_data_consent_changed", name, {"allowed": payload[name]["consent"]}, actor=user.actor)
    await ctx.db.audit(user.org_id, "ai_settings_changed", payload["provider"], {
        "provider": payload["provider"], "model": payload[payload["provider"]]["model"], "ollama_url": payload["ollama"]["url"]}, actor=user.actor)
    return await get_ai_settings(user, ctx)


@router.get("/ai/savings")
async def get_savings(user: Admin, ctx: Ctx) -> dict[str, Any]:
    """The cost-saving settings, and what they saved this month."""
    settings = await ai_settings.load_savings(ctx.db, user.org_id)
    start = month_start()
    calls = await ctx.db.one(
        "SELECT count(*) AS calls, COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
        "COALESCE(SUM(saved_usd), 0) AS saved_usd, COALESCE(SUM(cost_usd), 0) AS cost_usd, "
        "SUM(CASE WHEN purpose = 'evidence' THEN 1 ELSE 0 END) AS evidence_calls, "
        "COALESCE(SUM(CASE WHEN purpose = 'evidence' THEN cost_usd ELSE 0 END), 0) AS evidence_cost_usd "
        "FROM llm_calls WHERE org_id = ? AND started_at >= ? AND provider <> 'ollama'", (user.org_id, start))
    console = await ctx.db.one("SELECT count(*) AS n FROM tool_calls WHERE org_id = ? AND origin = 'console' AND ok = 1 AND created_at >= ?", (user.org_id, start))
    focused = await ctx.db.one(
        "SELECT count(*) AS n FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.org_id = ? AND m.role = 'user' AND m.focus <> '[]' AND m.created_at >= ?", (user.org_id, start))
    input_tokens = int(calls["input_tokens"] or 0)
    cached = int(calls["cache_read_tokens"] or 0)
    return {
        **settings,
        "month": {
            "since": start, "model_calls": int(calls["calls"] or 0), "cost_usd": round(float(calls["cost_usd"] or 0), 4),
            "cached_tokens": cached, "cached_share": round(cached / input_tokens, 3) if input_tokens else 0.0,
            "cache_saved_usd": round(float(calls["saved_usd"] or 0), 4),
            "evidence_calls": int(calls["evidence_calls"] or 0), "evidence_cost_usd": round(float(calls["evidence_cost_usd"] or 0), 4),
            "console_queries": int(console["n"] or 0), "focused_investigations": int(focused["n"] or 0),
        },
    }


@router.put("/ai/savings")
async def put_savings(body: SavingsBody, user: Admin, ctx: Ctx) -> dict[str, Any]:
    payload = body.model_dump()
    payload["evidence_model"] = {name: (payload["evidence_model"].get(name) or "").strip() for name in ("claude", "gemini")}
    await ctx.db.set_setting(user.org_id, ai_settings.SAVINGS_SETTING, payload)
    await ctx.db.audit(user.org_id, "ai_savings_changed", "ai", payload, actor=user.actor)
    return await get_savings(user, ctx)


KEY_SETTINGS = {"claude": ai_settings.CLAUDE_KEY_SETTING, "gemini": ai_settings.GEMINI_KEY_SETTING}


@router.put("/ai/{name}-key", status_code=204)
async def put_api_key(name: CloudProvider, body: ApiKeyBody, user: Admin, ctx: Ctx) -> Response:
    await ctx.db.set_setting(user.org_id, KEY_SETTINGS[name], ctx.box.encrypt(body.api_key.strip()))
    await ctx.db.audit(user.org_id, f"{name}_key_saved", name, {}, actor=user.actor)
    return Response(status_code=204)


@router.delete("/ai/{name}-key", status_code=204)
async def delete_api_key(name: CloudProvider, user: Admin, ctx: Ctx) -> Response:
    await ctx.db.set_setting(user.org_id, KEY_SETTINGS[name], "")
    await ctx.db.audit(user.org_id, f"{name}_key_removed", name, {}, actor=user.actor)
    return Response(status_code=204)


@router.get("/ai/providers/{name}/models")
async def provider_models(name: ProviderName, user: Engineer, ctx: Ctx, url: str | None = None) -> dict[str, Any]:
    # Trying a new address makes this server connect somewhere, so only admins can choose one.
    if url and not user.can("admin"):
        raise HTTPException(403, "This needs the admin role or higher.")
    fallback = FALLBACK_MODELS if name == "claude" else []
    if name in ai_settings.CLOUD_PROVIDERS:
        try:
            key, _ = await ai_settings.cloud_key(ctx.db, user.org_id, ctx.box, ctx.config, name)
        except ValueError as error:
            return {"ok": False, "models": fallback, "error": str(error)}
        if not key:
            return {"ok": False, "models": fallback, "error": "Add an API key to load your available models."}
    try:
        provider, settings = await ai_settings.build_provider(ctx.db, user.org_id, ctx.box, ctx.config, name)
    except ProviderError as error:
        return {"ok": False, "models": [], "error": error.message}
    try:
        if name == "ollama" and url:
            if not re.fullmatch(r"https?://[^\s/@]+(:\d+)?/?", url):
                return {"ok": False, "models": [], "error": "Enter an address like http://localhost:11434."}
            await check_url(url, ctx.config.target_policy)
            await provider.close()
            provider = OllamaProvider(url, int(settings["ollama"]["num_ctx"]), ctx.config.in_container, ctx.config.target_policy)
        return {"ok": True, "models": await provider.list_models(), "error": None}
    except BlockedTarget as error:
        return {"ok": False, "models": [], "error": str(error)}
    except ProviderError as error:
        return {"ok": False, "models": fallback, "error": error.message}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return {"ok": False, "models": [], "error": "That address didn't answer like an Ollama server."}
    finally:
        await provider.close()


# Investigations -------------------------------------------------------------
async def _own(ctx: AppContext, org_id: int, conversation_id: int) -> dict[str, Any]:
    row = await ctx.db.one("SELECT * FROM conversations WHERE id = ? AND org_id = ?", (conversation_id, org_id))
    if not row:
        raise HTTPException(404, "This investigation no longer exists.")
    return row


async def _readable(ctx: AppContext, user: Any, conversation_id: int) -> dict[str, Any]:
    """Someone else's private investigation looks exactly like one that doesn't exist."""
    row = await _own(ctx, user.org_id, conversation_id)
    if not can_read_conversation(user, row):
        raise HTTPException(404, "This investigation no longer exists.")
    return row


async def _changeable(ctx: AppContext, user: Any, conversation_id: int) -> dict[str, Any]:
    row = await _readable(ctx, user, conversation_id)
    if not can_change_conversation(user, row):
        raise HTTPException(403, "Only the person who started this investigation, or an admin, can change it.")
    return row


@router.get("/conversations")
async def list_conversations(user: Engineer, ctx: Ctx, q: str = "", status: str = "", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    where, args = ["c.org_id = ?"], [user.org_id]
    if not user.can("admin"):
        where.append("(c.owner_id = ? OR c.visibility = 'team')")
        args.append(user.user_id)
    if status:
        where.append("c.status = ?")
        args.append(status)
    if q.strip():
        ids = await ctx.db.search_conversation_ids(user.org_id, q.split(), limit=500)
        if not ids:
            return {"items": [], "total": 0}
        where.append(f"c.id IN ({','.join('?' * len(ids))})")
        args.extend(ids)
    clause = " AND ".join(where)
    # `clause` only joins fixed fragments. All user values are bound parameters.
    total = await ctx.db.one(f"SELECT count(*) AS n FROM conversations c WHERE {clause}", args)  # noqa: S608
    rows = await ctx.db.all(
        f"""SELECT c.*,
              (SELECT count(*) FROM llm_calls l WHERE l.conversation_id = c.id) AS model_calls,
              (SELECT count(*) FROM tool_calls t WHERE t.conversation_id = c.id) AS queries_run,
              (SELECT COALESCE(SUM(l.input_tokens), 0) + COALESCE(SUM(l.output_tokens), 0) FROM llm_calls l WHERE l.conversation_id = c.id) AS tokens,
              (SELECT COALESCE(SUM(l.cost_usd), 0) FROM llm_calls l WHERE l.conversation_id = c.id) AS cost_usd,
              (SELECT m.model FROM messages m WHERE m.conversation_id = c.id AND m.role = 'assistant' ORDER BY m.id DESC LIMIT 1) AS model,
              (SELECT m.provider FROM messages m WHERE m.conversation_id = c.id AND m.role = 'assistant' ORDER BY m.id DESC LIMIT 1) AS provider
            FROM conversations c WHERE {clause} ORDER BY c.updated_at DESC LIMIT ? OFFSET ?""",  # noqa: S608
        [*args, limit, offset],
    )
    return {"items": rows, "total": total["n"]}


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationBody, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    now = utcnow()
    conversation_id = await ctx.db.insert(
        "INSERT INTO conversations(org_id, created_by, owner_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user.org_id, user.actor, user.user_id, body.title or "New investigation", now, now),
    )
    return await _own(ctx, user.org_id, conversation_id)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: int, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    row = await _readable(ctx, user, conversation_id)
    messages = await ctx.db.all("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))
    tools = await ctx.db.all("SELECT * FROM tool_calls WHERE conversation_id = ? ORDER BY id", (conversation_id,))
    if not user.can("admin"):
        # Tokens, costs and timings are for admins. Everyone sees which model answered.
        for message in messages:
            for key in ("cost_usd", "input_tokens", "output_tokens", "duration_ms", "context_version"):
                message[key] = None
        # A query that failed and was retried or replaced is noise to most readers. Admins see every query.
        tools = [tool for tool in tools if tool["ok"]]
    run = ctx.investigations.active_run(conversation_id)
    names = json.loads(row["alert_names"] or "[]")
    workflows = await workflows_for(ctx.db, user.org_id, alert_names=names, text=row["title"])
    return {**row, "messages": messages, "tool_calls": tools, "active_run_id": run.id if run else None, "can_change": can_change_conversation(user, row),
            "workflows": [{"id": item["id"], "name": item["name"], "risk": item["risk"], "status": item["status"]} for item in workflows[:4]]}


@router.get("/conversations/{conversation_id}/report.pdf")
async def conversation_pdf(conversation_id: int, user: Engineer, ctx: Ctx) -> Response:
    """The latest answer as a PDF report, with charts of the metric evidence behind it."""
    from ..investigation_report import build_pdf, collect_evidence, reviewed_workflows, split_sections

    row = await _readable(ctx, user, conversation_id)
    answer = await ctx.db.one("SELECT content, verification, created_at FROM messages WHERE conversation_id = ? AND role = 'assistant' AND status = 'complete' "
                              "AND content <> '' ORDER BY id DESC LIMIT 1", (conversation_id,))
    if not answer:
        raise HTTPException(404, "This investigation has no finished answer yet.")
    org = await ctx.db.one("SELECT name FROM organizations WHERE id = ?", (user.org_id,))
    verification = json.loads(answer["verification"] or "{}")
    event = {"title": row["title"], "ts": row["created_at"], "kind": "investigation", "severity": "", "fingerprint": ""}
    engine = await ctx.engines.get(user.org_id)
    evidence = await collect_evidence(ctx.db, engine, conversation_id, event, {})
    facts = {"title": row["title"], "ts": row["created_at"], "company": ctx.config.company_name or org["name"],
             "confidence": str(verification.get("confidence_final") or "Not stated").capitalize(),
             "status": {"open": "Open", "resolved": "Resolved", "false_alarm": "False alarm"}.get(row["status"], row["status"]),
             "subtitle": f"Investigation report. Started {row['created_at'][:16].replace('T', ' ')} UTC.", "status_note": ""}
    workflows = await reviewed_workflows(ctx.db, user.org_id, event, {})
    # Drawing charts and laying out pages takes a while, so it runs off the main loop.
    pdf = await asyncio.to_thread(build_pdf, facts, split_sections(answer["content"]), evidence, workflows)
    await ctx.db.audit(user.org_id, "investigation_pdf_downloaded", f"conversation:{conversation_id}", {}, actor=user.actor)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", row["title"])[:60].strip("-") or "investigation"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{safe}.pdf"'})


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(conversation_id: int, body: ConversationPatch, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    await _changeable(ctx, user, conversation_id)
    updates = body.model_dump(exclude_none=True)
    if updates:
        # Column names come from the ConversationPatch schema, not from request text.
        columns = ", ".join(f"{column} = ?" for column in updates)
        await ctx.db.execute(f"UPDATE conversations SET {columns}, updated_at = ? WHERE id = ? AND org_id = ?",  # noqa: S608
                             [*updates.values(), utcnow(), conversation_id, user.org_id])
        if body.root_cause_note:
            await ctx.db.index_text(user.org_id, conversation_id, body.root_cause_note)
        await ctx.db.audit(user.org_id, "investigation_updated", f"conversation:{conversation_id}", {"fields": sorted(updates)}, actor=user.actor)
    return await _own(ctx, user.org_id, conversation_id)


@router.post("/conversations/{conversation_id}/share")
async def share_conversation(conversation_id: int, body: ShareBody, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    """Opens an investigation to the team: engineers can read it, and viewers see its questions and answers."""
    row = await _changeable(ctx, user, conversation_id)
    if row["trigger"] == "auto":
        raise HTTPException(409, "Automatic investigations are always visible to the team.")
    await ctx.db.execute("UPDATE conversations SET visibility = ?, shared_by = ?, shared_at = ? WHERE id = ? AND org_id = ?",
                         ("team" if body.shared else "private", user.actor if body.shared else "", utcnow() if body.shared else None, conversation_id, user.org_id))
    await ctx.db.audit(user.org_id, "investigation_shared" if body.shared else "investigation_unshared", f"conversation:{conversation_id}", {}, actor=user.actor)
    return {"visibility": "team" if body.shared else "private"}


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: int, user: Admin, ctx: Ctx) -> Response:
    row = await _own(ctx, user.org_id, conversation_id)
    if ctx.investigations.active_run(conversation_id):
        raise HTTPException(409, "Stop the running investigation first.")
    await ctx.db.unindex(conversation_id)
    await ctx.db.execute("DELETE FROM conversations WHERE id = ? AND org_id = ?", (conversation_id, user.org_id))
    await ctx.db.audit(user.org_id, "investigation_deleted", f"conversation:{conversation_id}", {"title": row["title"]}, actor=user.actor)
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/messages", status_code=202)
async def send_message(conversation_id: int, body: MessageBody, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    row = await _changeable(ctx, user, conversation_id)
    if ctx.investigations.active_run(conversation_id):
        raise HTTPException(409, "This investigation is still running.")
    for problem in (await budget_problem(ctx.db, user.org_id),):
        if problem:
            raise HTTPException(403, problem)
    try:
        selection = await ai_settings.select(ctx.db, user.org_id, ctx.box, ctx.config, body.provider, body.model, body.use_tools, body.answer_style)
    except (ProviderError, ValueError) as error:
        raise HTTPException(422, getattr(error, "message", str(error))) from error
    if row["title"] == "New investigation":
        await ctx.db.execute("UPDATE conversations SET title = ? WHERE id = ?", (" ".join(body.prompt.split())[:90], conversation_id))
    settings = await ai_settings.load_settings(ctx.db, user.org_id, ctx.config)
    run = ctx.investigations.start(user.org_id, conversation_id, body.prompt, selection, int(settings["max_rounds"]), actor=user.actor, owner_id=row["owner_id"],
                                   focus=body.focus)
    return {"run_id": run.id}


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, user: Engineer, ctx: Ctx, after: int = -1) -> StreamingResponse:
    run = ctx.investigations.get_run(run_id, user.org_id)
    row = await ctx.db.one("SELECT * FROM conversations WHERE id = ? AND org_id = ?", (run.conversation_id, user.org_id)) if run else None
    if not run or not row or not can_read_conversation(user, row):
        raise HTTPException(404, "This run has ended. Reload the investigation to see its result.")

    async def generate() -> AsyncIterator[str]:
        async for event in run.stream(after):
            yield json.dumps(event, default=str) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"})


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: str, user: Engineer, ctx: Ctx) -> dict[str, bool]:
    run = ctx.investigations.get_run(run_id, user.org_id)
    if run:
        await _changeable(ctx, user, run.conversation_id)
    return {"stopped": await ctx.investigations.stop(run_id, user.org_id)}


@router.get("/conversations/{conversation_id}/export")
async def export_conversation(conversation_id: int, user: Engineer, ctx: Ctx) -> JSONResponse:
    record = await get_conversation(conversation_id, user, ctx)
    record["llm_calls"] = await ctx.db.all("SELECT * FROM llm_calls WHERE conversation_id = ? AND org_id = ? ORDER BY id", (conversation_id, user.org_id))
    record["exported_at"] = utcnow()
    await ctx.db.audit(user.org_id, "investigation_exported", f"conversation:{conversation_id}", {}, actor=user.actor)
    return JSONResponse(record, headers={"Content-Disposition": f'attachment; filename="investigation-{conversation_id}.json"'})


@router.post("/conversations/{conversation_id}/draft-workflow", status_code=201)
async def draft_workflow(conversation_id: int, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    """Ask the model to turn an investigation into a workflow. The draft needs human review and is never run."""
    conversation = await _readable(ctx, user, conversation_id)
    message = await ctx.db.one(
        "SELECT * FROM messages WHERE conversation_id = ? AND role = 'assistant' AND status = 'complete' ORDER BY id DESC LIMIT 1", (conversation_id,))
    if not message:
        raise HTTPException(409, "Finish an investigation before drafting a workflow.")
    problem = await budget_problem(ctx.db, user.org_id)
    if problem:
        raise HTTPException(403, problem)
    try:
        selection = await ai_settings.select(ctx.db, user.org_id, ctx.box, ctx.config, message["provider"], message["model"], False)
    except (ProviderError, ValueError) as error:
        raise HTTPException(422, getattr(error, "message", str(error))) from error
    provider = selection.provider
    prompt = f"Investigation: {conversation['title']}\n\n{message['content']}"
    request = provider.build_request(selection.model, DRAFT_PROMPT, [{"role": "user", "content": prompt}], None)
    request_json = json.dumps(request, ensure_ascii=False)
    started = utcnow()
    parts: list[str] = []
    finished = Finished()
    try:
        async for item in provider.stream(request):
            if isinstance(item, TextDelta):
                parts.append(item.text)
            elif isinstance(item, Finished):
                finished = item
    except ProviderError as error:
        raise HTTPException(502, error.message) from error
    finally:
        await provider.close()
    spend = await spend_settings(ctx.db, user.org_id)
    await ctx.db.insert(
        "INSERT INTO llm_calls(org_id, conversation_id, round, provider, model, endpoint, request_json, request_sha256, response_text, stop_reason, input_tokens, output_tokens, cost_usd, status, started_at, finished_at) "
        "VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?)",
        (user.org_id, conversation_id, provider.name, selection.model, provider.endpoint, request_json, sha256_text(request_json), "".join(parts), finished.stop_reason,
         finished.input_tokens, finished.output_tokens, compute_cost(provider.name, selection.model, finished.input_tokens, finished.output_tokens, spend["prices"]), started, utcnow()),
    )
    try:
        draft = parse_draft("".join(parts))
    except ValueError as error:
        raise HTTPException(502, str(error)) from error
    now = utcnow()
    workflow_id = await ctx.db.insert(
        "INSERT INTO workflows(org_id, name, summary, match_rules, risk, steps, status, origin, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'draft', 'ai', ?, ?, ?)",
        (user.org_id, draft["name"], draft["summary"], json.dumps(draft["match"]), draft["risk"], json.dumps(draft["steps"]), user.actor, now, now),
    )
    await ctx.db.audit(user.org_id, "workflow_drafted_by_ai", f"workflow:{workflow_id}", {"conversation": conversation_id, "model": selection.model}, actor=user.actor)
    return public_workflow(await ctx.db.one("SELECT * FROM workflows WHERE id = ?", (workflow_id,)))


# Plain results for everyone ---------------------------------------------------
@router.get("/results")
async def list_results(user: User, ctx: Ctx, q: str = "", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    """Investigations shared with the team, and automatic ones. Titles and status only, with nothing about how they ran."""
    where, args = ["org_id = ?", "visibility = 'team'"], [user.org_id]
    if q.strip():
        ids = await ctx.db.search_conversation_ids(user.org_id, q.split(), limit=500)
        if not ids:
            return {"items": [], "total": 0}
        where.append(f"id IN ({','.join('?' * len(ids))})")
        args.extend(ids)
    clause = " AND ".join(where)
    total = await ctx.db.one(f"SELECT count(*) AS n FROM conversations WHERE {clause}", args)  # noqa: S608
    rows = await ctx.db.all(f"SELECT id, title, status, trigger, updated_at FROM conversations WHERE {clause} ORDER BY updated_at DESC LIMIT ? OFFSET ?", [*args, limit, offset])  # noqa: S608
    return {"items": rows, "total": total["n"]}


@router.get("/results/{conversation_id}")
async def get_result(conversation_id: int, user: User, ctx: Ctx) -> dict[str, Any]:
    """The questions and final answers, and nothing else: no prompts, queries, models, tokens, or costs."""
    row = await ctx.db.one("SELECT id, title, status, root_cause_note, updated_at FROM conversations WHERE id = ? AND org_id = ? AND visibility = 'team'", (conversation_id, user.org_id))
    if not row:
        raise HTTPException(404, "This result no longer exists.")
    rows = await ctx.db.all("SELECT role, content, status, created_at FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))
    exchanges: list[dict[str, Any]] = []
    for message in rows:
        if message["role"] == "user":
            exchanges.append({"question": message["content"], "answer": "", "status": "running", "asked_at": message["created_at"]})
        elif exchanges:
            exchanges[-1]["answer"] = message["content"] if message["status"] == "complete" else ""
            exchanges[-1]["status"] = message["status"]
    return {**row, "exchanges": exchanges}


# Logs -----------------------------------------------------------------------
@router.get("/llm-calls")
async def list_llm_calls(user: Engineer, ctx: Ctx, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> list[dict[str, Any]]:
    limit_to = "" if user.can("admin") else "AND (c.owner_id = ? OR c.visibility = 'team') "
    scope = () if user.can("admin") else (user.user_id,)
    # `limit_to` is one of two fixed strings. All values are bound parameters.
    return await ctx.db.all(
        f"""SELECT l.id, l.conversation_id, l.message_id, l.round, l.provider, l.model, l.context_version, l.request_sha256, l.stop_reason, l.input_tokens,
                  l.output_tokens, l.cost_usd, l.first_token_ms, l.duration_ms, l.status, l.error, l.started_at, c.title
           FROM llm_calls l JOIN conversations c ON c.id = l.conversation_id WHERE l.org_id = ? {limit_to}ORDER BY l.id DESC LIMIT ? OFFSET ?""",  # noqa: S608
        (user.org_id, *scope, limit, offset),
    )


@router.get("/llm-calls/{call_id}")
async def get_llm_call(call_id: int, user: Engineer, ctx: Ctx) -> dict[str, Any]:
    row = await ctx.db.one("SELECT * FROM llm_calls WHERE id = ? AND org_id = ?", (call_id, user.org_id))
    if row:
        await _readable(ctx, user, row["conversation_id"])
    if not row:
        raise HTTPException(404, "This log entry no longer exists.")
    row["request"] = json.loads(row.pop("request_json"))
    row["response_tool_calls"] = json.loads(row["response_tool_calls"])
    if not user.can("admin"):
        row.pop("endpoint", None)
    return row
