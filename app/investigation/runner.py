"""Runs an investigation: refreshes the context, calls the model, runs read-only tools, checks the answer, and logs everything."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

from prometheus_client import Counter

from ..ai_settings import Selection
from ..config import Config
from ..context.manager import EngineManager
from ..context.summary import material_hash
from ..context.render import focus_context, render_context
from ..costs import cache_saving, compute_cost, spend_settings
from ..db import Database, sha256_text, utcnow
from ..llm.base import Finished, ProviderError, TextDelta, ToolCall, ToolsUnsupported
from ..secrets_store import SecretBox
from ..workflows import matching_workflows
from .prompts import FALLBACK_MARK, build_system, compact_tools, fallback_answer
from . import evidence
from .tools import ToolRunner, related_investigations
from .verification import expects_diagnosis, missing_sections, verify_answer

log = logging.getLogger("rcc.investigation")

MODEL_CALLS = Counter("rcc_model_calls_total", "Model calls by provider and result", ["provider", "status"])
RUN_RETENTION_S = 300
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]{3,}")


MAX_TOOL_CALLS = 10
MAX_CALLS_PER_ROUND = 4


def _skipped(call: ToolCall, text: str) -> dict[str, Any]:
    """A tool result that runs nothing, so the model still gets one answer per call it made."""
    return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": text, "is_error": True}


class Run:
    def __init__(self, org_id: int, conversation_id: int, owner_id: int | None = None) -> None:
        self.id = uuid.uuid4().hex
        self.org_id = org_id
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.task: asyncio.Task | None = None
        self.sent_context: dict[str, Any] = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost: float | None = 0.0
        self.tool_results: list[str] = []
        self.rendered = ""
        self.focus: list[str] = []
        self.cache_read_tokens = 0
        self.saved_usd = 0.0
        self._changed = asyncio.Condition()

    async def emit(self, kind: str, **data: Any) -> None:
        async with self._changed:
            self.events.append({"seq": len(self.events), "type": kind, **data})
            self._changed.notify_all()

    async def finish(self) -> None:
        async with self._changed:
            self.done = True
            self._changed.notify_all()

    async def stream(self, after: int = -1) -> AsyncIterator[dict[str, Any]]:
        position = after + 1
        while True:
            async with self._changed:
                if position >= len(self.events) and not self.done:
                    try:
                        await asyncio.wait_for(self._changed.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        pass
                pending = self.events[position:]
                finished = self.done
            if not pending and not finished:
                yield {"type": "heartbeat"}
            for event in pending:
                position = event["seq"] + 1
                yield event
            if finished and position >= len(self.events):
                return


FinishHook = Callable[[Run, dict[str, Any]], Awaitable[None]]


class InvestigationManager:
    def __init__(self, db: Database, engines: EngineManager, box: SecretBox, config: Config) -> None:
        self.db, self.engines, self.box, self.config = db, engines, box, config
        self.runs: dict[str, Run] = {}
        self.on_finished: FinishHook | None = None
        self.on_failed: FinishHook | None = None

    def active_run(self, conversation_id: int) -> Run | None:
        for run in self.runs.values():
            if run.conversation_id == conversation_id and not run.done:
                return run
        return None

    def get_run(self, run_id: str, org_id: int) -> Run | None:
        run = self.runs.get(run_id)
        return run if run and run.org_id == org_id else None

    def start(self, org_id: int, conversation_id: int, prompt: str, selection: Selection, max_rounds: int, actor: str = "system", owner_id: int | None = None,
              focus: list[str] | None = None) -> Run:
        run = Run(org_id, conversation_id, owner_id)
        run.focus = list(focus or [])[:20]
        self.runs[run.id] = run
        run.task = asyncio.create_task(self._execute(run, prompt, selection, max_rounds, actor))
        return run

    async def stop(self, run_id: str, org_id: int) -> bool:
        run = self.get_run(run_id, org_id)
        if not run or run.done or not run.task:
            return False
        run.task.cancel()
        return True

    async def stop_all(self) -> None:
        tasks = [run.task for run in self.runs.values() if run.task and not run.done]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute(self, run: Run, prompt: str, selection: Selection, max_rounds: int, actor: str) -> None:
        provider = selection.provider
        conversation_id = run.conversation_id
        now = utcnow()
        started = time.monotonic()
        await self.db.insert(
            "INSERT INTO messages(conversation_id, role, content, provider, model, author, focus, answer_style, created_at) VALUES (?, 'user', ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, prompt, provider.name, selection.model, actor, json.dumps(run.focus), selection.answer_style, now),
        )
        await self.db.index_text(run.org_id, conversation_id, prompt)
        message_id = await self.db.insert(
            "INSERT INTO messages(conversation_id, role, content, status, provider, model, focus, answer_style, created_at) VALUES (?, 'assistant', '', 'running', ?, ?, ?, ?, ?)",
            (conversation_id, provider.name, selection.model, json.dumps(run.focus), selection.answer_style, now),
        )
        await self.db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))

        answer_parts: list[str] = []
        notices: list[str] = []
        status, error_text = "complete", ""
        await run.emit("started", message_id=message_id, provider=provider.name, model=selection.model)
        try:
            await self._loop(run, message_id, prompt, selection, max_rounds, answer_parts, notices, actor)
        except asyncio.CancelledError:
            status, error_text = "stopped", "Stopped by the user."
        except ProviderError as error:
            status, error_text = "failed", error.message
        except Exception as error:  # The run must always end with a recorded outcome.
            log.exception("investigation failed")
            status, error_text = "failed", f"The investigation failed: {error}"

        answer = "".join(answer_parts).strip()
        if status == "complete" and not answer:
            status, error_text = "failed", "The model returned no text."
        verification: dict[str, Any] = {}
        if status == "complete":
            verification = await self._verify(run, prompt, answer)
            if run.saved_usd:
                verification["cache_saved_usd"] = round(run.saved_usd, 6)
        context = run.sent_context
        duration = round((time.monotonic() - started) * 1000)
        await self.db.execute(
            "UPDATE messages SET content = ?, status = ?, error = ?, context_version = ?, context_as_of = ?, notices = ?, verification = ?, "
            "cost_usd = ?, input_tokens = ?, output_tokens = ?, duration_ms = ? WHERE id = ?",
            (answer, status, error_text, context.get("version"), context.get("as_of"), json.dumps(notices), json.dumps(verification),
             run.cost, run.input_tokens, run.output_tokens, duration, message_id),
        )
        await self.db.index_text(run.org_id, conversation_id, answer)
        await self.db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (utcnow(), conversation_id))
        await provider.close()
        await run.emit("finished", status=status, error=error_text, message_id=message_id, verification=verification,
                       cost_usd=run.cost, input_tokens=run.input_tokens, output_tokens=run.output_tokens, duration_ms=duration,
                       cache_read_tokens=run.cache_read_tokens, saved_usd=round(run.saved_usd, 6))
        if self.on_finished and status == "complete":
            try:
                await self.on_finished(run, {"answer": answer, "verification": verification})
            except Exception:
                log.exception("post-investigation hook failed")
        elif self.on_failed and status != "complete":
            try:
                await self.on_failed(run, {"status": status, "error": error_text})
            except Exception:
                log.exception("post-investigation hook failed")
        await run.finish()
        asyncio.get_running_loop().call_later(RUN_RETENTION_S, self.runs.pop, run.id, None)

    async def _verify(self, run: Run, prompt: str, answer: str) -> dict[str, Any]:
        engine = await self.engines.get(run.org_id)

        async def run_query(query: str) -> dict[str, Any]:
            return await engine.source(None).query(query)

        has_source = bool(engine.states)
        try:
            problems = (run.sent_context or {}).get("problems", [])
            return await verify_answer(prompt, answer, [run.rendered, *run.tool_results], run_query if has_source else None, problems)
        except Exception:
            log.exception("answer check failed")
            return {"kind": "error", "confidence_final": None}

    async def _loop(self, run: Run, message_id: int, prompt: str, selection: Selection, max_rounds: int,
                    answer_parts: list[str], notices: list[str], actor: str) -> None:
        conversation_id = run.conversation_id
        org_id = run.org_id
        engine = await self.engines.get(org_id)
        tools_runner = ToolRunner(engine, self.db, self.box, self.config, run.owner_id)
        tool_definitions = await tools_runner.definitions()
        if selection.compact:
            tool_definitions = compact_tools(tool_definitions)
        use_tools = selection.use_tools and engine.has_connections
        spend = await spend_settings(self.db, org_id)
        prices = spend["prices"]
        token_limit = spend["max_tokens_per_investigation"]

        await run.emit("status", text="Refreshing the live context")
        if not engine.is_fresh():
            await engine.refresh_now()
        context = engine.context
        started_hash = material_hash(context)
        started_events = {event["id"] for event in context.get("events", [])}
        await self._save_alert_names(org_id, conversation_id, context)
        past = await related_investigations(self.db, org_id, _search_terms(prompt, context), exclude_id=conversation_id, owner_id=run.owner_id)
        if past:
            await run.emit("past", items=past)
        workflows = await matching_workflows(self.db, org_id, prompt, context)
        if workflows:
            await run.emit("workflows", items=[{"id": item["id"], "name": item["name"], "risk": item["risk"], "status": item["status"]} for item in workflows])
        messages = await self._history(conversation_id, selection.history_chars)
        repaired = False
        tool_calls_made = 0
        seen_calls: dict[str, int] = {}
        evidence_text = ""
        if selection.gather_evidence and engine.has_connections:
            evidence_text = await self._gather(run, tools_runner, engine, message_id)
        rendered_hash = ""
        # A cheaper model may run the query rounds; the main model then writes the answer from what it found.
        use_evidence_model = bool(selection.evidence_model) and use_tools
        final_round = False

        for round_number in range(1, max_rounds + 2):
            context = self._focused(engine.context, run.focus)
            run.sent_context = context
            # The context is rendered again only when something material changed. An identical prompt start on
            # every round is what lets the provider's cache serve it.
            current_hash = material_hash(engine.context)
            if current_hash != rendered_hash:
                rendered_hash = current_hash
                run.rendered = render_context(context, selection.context_chars)
            changes = [f"{event['ts']} {event['title']}" for event in context.get("events", []) if event["id"] not in started_events]
            if material_hash(engine.context) != started_hash and round_number > 1:
                notice = "The live state changed during this investigation, so the model received the update."
                if notice not in notices:
                    notices.append(notice)
                    await run.emit("notice", text=notice)
            await run.emit("context", version=context.get("version"), as_of=context.get("as_of"), round=round_number)

            over_tokens = bool(token_limit) and run.input_tokens + run.output_tokens >= int(token_limit)
            if over_tokens and not any("token limit" in item for item in notices):
                notice = "This investigation reached its token limit, so the model was asked to finish now."
                notices.append(notice)
                await run.emit("notice", text=notice)
            out_of_calls = tool_calls_made >= MAX_TOOL_CALLS
            if out_of_calls and use_tools and not any("queries allowed" in item for item in notices):
                notice = f"The model used the {MAX_TOOL_CALLS} queries allowed in one investigation, so it was asked to answer now."
                notices.append(notice)
                await run.emit("notice", text=notice)
            last_round = round_number >= max_rounds or over_tokens or out_of_calls or final_round
            # On the last round the tools stay declared, so earlier calls in the history remain valid, but the
            # model is told it may not call them. Some models ignore a missing tool list and call tools anyway.
            tools = tool_definitions if use_tools else None
            system = build_system(run.rendered, past, changes, use_tools, last_round and use_tools, workflows, selection.compact, selection.answer_style, evidence_text)
            evidence_round = use_evidence_model and not last_round
            outcome, call_id = await self._send(run, message_id, round_number, selection, system, messages, tools, context, answer_parts, prices, actor,
                                                allow_tools=not last_round, model=selection.evidence_model if evidence_round else None)
            if outcome is None:  # The model can't use tools, so ask again without them.
                use_tools = False
                notice = f"{selection.model} can't run queries, so it answered from the live context only."
                notices.append(notice)
                await run.emit("notice", text=notice)
                use_evidence_model = evidence_round = False
                system = build_system(run.rendered, past, changes, False, False, workflows, selection.compact, selection.answer_style, evidence_text)
                outcome, call_id = await self._send(run, message_id, round_number, selection, system, messages, None, context, answer_parts, prices, actor)
                if outcome is None:
                    raise ProviderError("The model rejected the request.")

            round_text, tool_calls, finished = outcome
            if evidence_round and not tool_calls:
                # The cheaper model has gathered what it needs. Its draft isn't shown: the main model writes the answer.
                final_round = True
                await run.emit("status", text=f"Evidence gathered. {selection.model} is writing the answer")
                continue
            if finished.stop_reason == "max_tokens":
                notice = "The answer hit the model's length limit and may be cut off."
                notices.append(notice)
                await run.emit("notice", text=notice)
            elif finished.stop_reason == "refusal":
                notice = "The model declined to continue this request."
                notices.append(notice)
                await run.emit("notice", text=notice)
            if tool_calls and last_round:
                # The model asked for more data when it had to answer. Never end without an answer.
                if not "".join(answer_parts).strip():
                    answer_parts.clear()
                    answer_parts.append(fallback_answer(context))
                    await run.emit("text_reset")
                    await run.emit("text", text=answer_parts[0])
                notice = "The model kept asking for more data instead of answering, so this answer was written by the console from its own checks."
                if not "".join(answer_parts).startswith(FALLBACK_MARK):
                    notice = "The model asked for more data after its answer. The answer it gave is shown."
                notices.append(notice)
                await run.emit("notice", text=notice)
                return
            if not tool_calls:
                answer = "".join(answer_parts)
                missing = missing_sections(answer, selection.answer_style)
                if selection.repair and not repaired and missing and expects_diagnosis(prompt, answer) and round_text.strip():
                    repaired = True
                    messages.append({"role": "assistant", "content": round_text})
                    messages.append({"role": "user", "content": (
                        f"Your answer is missing {', '.join(missing)}. Rewrite the complete answer in the required format. "
                        "Use only numbers from the data, and say Confidence: Unknown if the evidence is too weak.")})
                    answer_parts.clear()
                    await run.emit("text_reset")
                    await run.emit("status", text="Asking the model to complete its answer")
                    continue
                return

            messages.append({
                "role": "assistant", "content": round_text,
                "tool_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in tool_calls],
                # Blocks are model-specific (thinking, signatures), so calls made by the evidence model are passed on plainly.
                "assistant_blocks": None if evidence_round else (finished.assistant_blocks or None),
                "other_model": evidence_round,
            })
            for index, call in enumerate(tool_calls):
                key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}"
                if index >= MAX_CALLS_PER_ROUND:
                    messages.append(_skipped(call, f"Skipped: at most {MAX_CALLS_PER_ROUND} queries run per round. Ask again only if you still need it."))
                elif key in seen_calls:
                    messages.append(_skipped(call, f"You already ran this exact query in round {seen_calls[key]}. Its result is above, so use it instead of asking again."))
                else:
                    seen_calls[key] = round_number
                    tool_calls_made += 1
                    messages.append(await self._run_tool(run, tools_runner, call, conversation_id, message_id, call_id))
        # Reached only if a provider returns nothing usable on every round. Still give the person an answer.
        answer_parts.clear()
        answer_parts.append(fallback_answer(engine.context))
        await run.emit("text_reset")
        await run.emit("text", text=answer_parts[0])
        notice = "The model didn't produce an answer, so this answer was written by the console from its own checks."
        notices.append(notice)
        await run.emit("notice", text=notice)

    async def _send(self, run: Run, message_id: int, round_number: int, selection: Selection, system: str | list[str], messages: list[dict[str, Any]],
                    tools: list[dict[str, Any]] | None, context: dict[str, Any], answer_parts: list[str], prices: dict[str, Any], actor: str,
                    allow_tools: bool = True, model: str | None = None) -> tuple[Any, int]:
        provider = selection.provider
        engine = await self.engines.get(run.org_id)
        model = model or selection.model
        purpose = "answer" if model == selection.model else "evidence"
        request = provider.build_request(model, system, messages, tools, allow_tools)
        snapshot_id = await engine.save_snapshot(context, "sent_to_model")
        request_json = json.dumps(request, ensure_ascii=False)
        request_hash = sha256_text(request_json)
        call_id = await self.db.insert(
            "INSERT INTO llm_calls(org_id, conversation_id, message_id, round, provider, model, endpoint, snapshot_id, context_version, request_json, request_sha256, purpose, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run.org_id, run.conversation_id, message_id, round_number, provider.name, model, provider.endpoint,
             snapshot_id, context.get("version"), request_json, request_hash, purpose, utcnow()),
        )
        await self.db.audit(run.org_id, "model_prompt_sent", f"conversation:{run.conversation_id}", {
            "llm_call_id": call_id, "provider": provider.name, "model": model, "endpoint": provider.endpoint,
            "leaves_this_system": provider.is_remote, "request_sha256": request_hash, "context_version": context.get("version"),
        }, actor=actor)
        await run.emit("status", text=f"{model} is looking for evidence" if purpose == "evidence" else f"Waiting for {model}", round=round_number)
        # Text from an evidence round is working notes, not the answer, so it isn't shown.
        outcome = await self._call_model(run, call_id, provider, request, answer_parts if purpose == "answer" else [], model, prices, visible=purpose == "answer")
        return outcome, call_id

    async def _call_model(self, run: Run, call_id: int, provider: Any, request: dict[str, Any], answer_parts: list[str], model: str, prices: dict[str, Any],
                          visible: bool = True) -> tuple[str, list[ToolCall], Finished] | None:
        started = time.monotonic()
        first_token_ms: int | None = None
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        finished = Finished()
        status, error_text = "complete", ""
        try:
            async for item in provider.stream(request):
                if isinstance(item, TextDelta):
                    if first_token_ms is None:
                        first_token_ms = round((time.monotonic() - started) * 1000)
                    text_parts.append(item.text)
                    answer_parts.append(item.text)
                    if visible:
                        await run.emit("text", text=item.text)
                elif isinstance(item, ToolCall):
                    if first_token_ms is None:
                        first_token_ms = round((time.monotonic() - started) * 1000)
                    tool_calls.append(item)
                elif isinstance(item, Finished):
                    finished = item
        except ToolsUnsupported:
            status, error_text = "tools_unsupported", "Model does not support tools."
            return None
        except ProviderError as error:
            status, error_text = "failed", error.message
            raise
        except asyncio.CancelledError:
            status, error_text = "stopped", "Stopped by the user."
            raise
        finally:
            MODEL_CALLS.labels(provider.name, status).inc()
            cost = compute_cost(provider.name, model, finished.input_tokens, finished.output_tokens, prices, finished.cache_read_tokens, finished.cache_write_tokens)
            saved = cache_saving(provider.name, model, finished.input_tokens, finished.output_tokens, prices, finished.cache_read_tokens, finished.cache_write_tokens)
            run.input_tokens += finished.input_tokens or 0
            run.output_tokens += finished.output_tokens or 0
            run.cache_read_tokens += finished.cache_read_tokens or 0
            run.saved_usd += max(saved or 0.0, 0.0)
            if status == "complete":
                run.cost = None if cost is None or run.cost is None else run.cost + cost
            await asyncio.shield(self.db.execute(
                "UPDATE llm_calls SET response_text = ?, response_tool_calls = ?, stop_reason = ?, input_tokens = ?, output_tokens = ?, cost_usd = ?, "
                "first_token_ms = ?, duration_ms = ?, status = ?, error = ?, cache_read_tokens = ?, cache_write_tokens = ?, saved_usd = ?, finished_at = ? WHERE id = ?",
                ("".join(text_parts),
                 json.dumps([{"id": call.id, "name": call.name, "arguments": call.arguments} for call in tool_calls], ensure_ascii=False),
                 finished.stop_reason, finished.input_tokens, finished.output_tokens, cost, first_token_ms,
                 round((time.monotonic() - started) * 1000), status, error_text, finished.cache_read_tokens, finished.cache_write_tokens, saved, utcnow(), call_id),
            ))
        return "".join(text_parts), tool_calls, finished

    async def _run_tool(self, run: Run, runner: ToolRunner, call: ToolCall, conversation_id: int, message_id: int, call_id: int | None,
                        origin: str = "model") -> dict[str, Any]:
        await run.emit("tool_start", id=call.id, name=call.name, arguments=call.arguments, origin=origin)
        started = time.monotonic()
        ok, result = await runner.run(call.name, call.arguments)
        duration = round((time.monotonic() - started) * 1000)
        if ok:
            run.tool_results.append(result)
        await self.db.insert(
            "INSERT INTO tool_calls(org_id, conversation_id, message_id, llm_call_id, name, arguments, result, ok, duration_ms, origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run.org_id, conversation_id, message_id, call_id, call.name, json.dumps(call.arguments, ensure_ascii=False), result, int(ok), duration, origin, utcnow()),
        )
        await run.emit("tool_result", id=call.id, name=call.name, arguments=call.arguments, ok=ok, result=result, duration_ms=duration, origin=origin)
        return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result, "is_error": not ok}

    @staticmethod
    def _focused(context: dict[str, Any], focus: list[str]) -> dict[str, Any]:
        return focus_context(context, focus) if focus else context

    async def _gather(self, run: Run, runner: ToolRunner, engine: Any, message_id: int) -> str:
        """Run the obvious first queries for the problems in view, before the model starts."""
        context = self._focused(engine.context, run.focus)
        problems = context.get("problems", [])
        if not problems:
            return ""
        kinds = engine.integration_kinds
        planned = evidence.plan(context, problems, "loki" in kinds, "kubernetes" in kinds, bool(engine.states))
        if not planned:
            return ""
        await run.emit("status", text="Gathering evidence for the problems in view")
        results = []
        for index, item in enumerate(planned, start=1):
            arguments = {key: value for key, value in item["arguments"].items() if value is not None}
            call = ToolCall(f"console-{index}", item["name"], arguments)
            message = await self._run_tool(run, runner, call, run.conversation_id, message_id, None, origin="console")
            results.append({**item, "arguments": arguments, "ok": not message["is_error"], "result": message["content"]})
        return evidence.section(results)

    async def _history(self, conversation_id: int, max_chars: int) -> list[dict[str, Any]]:
        """Load earlier turns, newest first, up to the budget. The newest prompt is always kept."""
        rows = await self.db.all(
            "SELECT role, content FROM messages WHERE conversation_id = ? AND status = 'complete' AND content <> '' ORDER BY id DESC", (conversation_id,))
        selected: list[dict[str, Any]] = []
        used = 0
        for index, row in enumerate(rows):
            if index > 0 and used + len(row["content"]) > max_chars:
                break
            selected.append({"role": row["role"], "content": row["content"]})
            used += len(row["content"])
        selected.reverse()
        while selected and selected[0]["role"] != "user":
            selected.pop(0)
        return selected

    async def _save_alert_names(self, org_id: int, conversation_id: int, context: dict[str, Any]) -> None:
        names = sorted({alert["name"] for alert in context.get("alerts", {}).get("firing", [])})
        await self.db.execute("UPDATE conversations SET alert_names = ? WHERE id = ? AND alert_names = '[]'", (json.dumps(names), conversation_id))
        if names:
            await self.db.index_text(org_id, conversation_id, " ".join(names))


def _search_terms(prompt: str, context: dict[str, Any]) -> list[str]:
    terms = [alert["name"] for alert in context.get("alerts", {}).get("firing", [])]
    terms += [target["job"] for target in context.get("targets", {}).get("down_list", [])]
    terms += WORD_RE.findall(prompt)
    return list(dict.fromkeys(terms))[:24]
