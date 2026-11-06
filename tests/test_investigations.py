"""Live context, investigations, answer checks, costs, workflows, alerts, and webhooks."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.ai_settings import Selection
from app.context.engine import compute_baseline, headline_for, material_hash, robust_z
from app.context.render import render_context
from app.costs import compute_cost
from app.investigation.verification import check_numbers, corpus_numbers, missing_sections, verify_answer
from app.llm.base import Finished, TextDelta, ToolCall
from app.secrets_store import SecretBox, scrub
from app.workflows import parse_draft
from conftest import FakeProm


async def _company_with_source(console, prom_server, email="o@test.example", name="TestCo"):
    client = await console.company(name, email)
    source = await client.post("/api/sources", json={"name": "lab", "poll_seconds": 5, "config": {"host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})
    assert source.status_code == 201, source.text
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    engine = await console.ctx.engines.get(org_id)
    await engine.refresh_now()
    return client, org_id, engine


async def _set_ollama(client, url, tools=False, model="llama3.2:latest", strict=False):
    settings = (await client.get("/api/ai/settings")).json()
    settings["ollama"].update(url=url, model=model, use_tools=tools, strict_answers=strict)
    for key in ("key_source", "key_configured"):
        settings["claude"].pop(key, None)
    response = await client.put("/api/ai/settings", json={k: settings[k] for k in ("provider", "max_rounds", "ollama", "claude")})
    assert response.status_code == 200, response.text


async def _ask(client, prompt="Why is checkout failing?", **extra):
    conversation = (await client.post("/api/conversations", json={})).json()
    run = await client.post(f"/api/conversations/{conversation['id']}/messages", json={"prompt": prompt, **extra})
    assert run.status_code == 202, run.text
    async with client.stream("GET", f"/api/runs/{run.json()['run_id']}/stream") as response:
        events = [json.loads(line) async for line in response.aiter_lines() if line]
    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    return conversation["id"], events, detail


def test_secret_scrub():
    cleaned = scrub('failed password=hunter2 url=http://bob:pw@host token: abc123 Bearer abcdefghijkl sk-ant-api03-secretvalue AKIAABCDEFGHIJKLMNOP')
    for secret in ["hunter2", "bob:pw", "abc123", "abcdefghijkl", "secretvalue", "AKIAABCD"]:
        assert secret not in cleaned


def test_secret_encryption(make_config):
    box = SecretBox(make_config())
    encrypted = box.encrypt("hunter2")
    assert "hunter2" not in encrypted and box.decrypt(encrypted) == "hunter2"


def test_baseline_flags_real_jump_but_not_noise():
    baseline = compute_baseline([10 + (index % 3) * 0.1 for index in range(60)])
    assert abs(robust_z(10.1, baseline, 0)) < 1 and robust_z(40, baseline, 0) > 4
    assert compute_baseline([1, 2, 3]) is None


def test_headline_reports_problems_first():
    assert "firing" in headline_for("ok", {"down": 0, "total": 1}, {"firing": [{}]}, [])["text"]
    assert headline_for("ok", {"down": 0, "total": 3}, {"firing": []}, [])["tone"] == "ok"
    assert "out of date" in headline_for("unreachable", {}, {}, [])["text"]
    assert "source" in headline_for("partial", {"down": 0}, {"firing": []}, [], ["aws"])["text"]


def test_render_keeps_alerts_when_budget_is_small():
    context = {
        "version": 7, "as_of": "2026-01-01T00:00:00Z", "sources": [{"id": 1, "name": "x", "kind": "prometheus", "status": "ok"}], "health": {"status": "ok"},
        "alerts": {"firing": [{"name": "ImportantAlert", "severity": "critical", "labels": {}, "summary": "s"}], "pending": []},
        "targets": {"up": 1, "down": 0, "unknown": 0, "total": 1, "down_list": []}, "rules": {"failing": []}, "anomalies": [], "events": [],
        "coverage": {"metric_count": 1, "missing": []},
        "signals": [{"title": f"S{i}", "unit": "short", "query": "q" * 200, "total_series": 1, "series": [{"labels": {"a": "b"}, "value": 1.0}]} for i in range(200)],
    }
    text = render_context(context, 1500)
    assert len(text) <= 1500 and "ImportantAlert" in text and "left out" in text


async def test_engine_tracks_changes_and_scrubs(console, prom_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    first = engine.context["version"]
    assert engine.context["health"]["status"] == "ok"
    FakeProm.state.down = True
    FakeProm.state.firing = True
    await engine.refresh_now()
    assert engine.context["version"] > first and engine.context["alerts"]["firing"][0]["name"] == "HighErrorRate"
    assert "hunter2" not in engine.context["targets"]["down_list"][0]["error"]
    FakeProm.state.down = False
    FakeProm.state.firing = False
    await engine.refresh_now()
    kinds = {row["kind"] for row in await console.ctx.db.all("SELECT kind FROM events WHERE org_id = ?", (org_id,))}
    assert {"alert_started", "target_started", "alert_cleared", "target_cleared"} <= kinds


async def test_unknown_target_is_not_reported_as_recovered(console, prom_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    FakeProm.state.down = True
    await engine.refresh_now()
    FakeProm.state.unknown = True
    await engine.refresh_now()
    assert await console.ctx.db.all("SELECT * FROM events WHERE org_id = ? AND kind = 'target_cleared'", (org_id,)) == []
    FakeProm.state.unknown = False
    FakeProm.state.no_targets = True
    await engine.refresh_now()
    cleared = await console.ctx.db.one("SELECT title FROM events WHERE org_id = ? AND kind = 'target_cleared'", (org_id,))
    assert cleared["title"].startswith("Target no longer reported")


async def test_engine_marks_data_stale_when_source_fails(console, prom_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    good = engine.context["as_of"]
    prom_server.shutdown()
    prom_server.server_close()
    await engine.refresh_now()
    assert engine.context["health"]["status"] == "unreachable" and engine.context["stale"] is True and engine.context["as_of"] == good
    assert "WARNING" in render_context(engine.context)


def test_number_check_catches_invented_values():
    corpus = corpus_numbers(["Failed requests = 30.3%, connections 100 of 100, response 2.41 s, 1,250 req/s"])
    good = check_numbers("Errors reached 30.3% with 100 connections, and latency of 2410 ms at 1,250 req/s.", corpus)
    assert good["checked"] == 4 and not good["unmatched"]
    bad = check_numbers("Errors reached 77.7% and latency hit 9.9 s.", corpus)
    assert bad["checked"] == 2 and len(bad["unmatched"]) == 2
    skipped = check_numbers("Over `rate(x[5m])`, 2 targets on 12:03 with version 2.18.0.", corpus)
    assert skipped["checked"] == 0


async def test_verification_lowers_confidence_without_proof():
    invented = "## Summary\nx\n## Likely root cause\nThe pool.\nConfidence: High\n## Evidence\n- Errors at 88.8% and 77.7%.\n## Next checks\nnone"
    result = await verify_answer("why is this failing", invented, ["errors 12.5%"], None)
    assert result["confidence_stated"] == "High" and result["confidence_final"] == "Low" and result["lowered"] is True
    assert any("competing" in reason for reason in result["reasons"]) and any("numbers" in reason for reason in result["reasons"])
    good = "## Summary\nx\n## Likely root cause\nThe pool.\nConfidence: High\n## Evidence\n- Errors at 12.5%.\n## Competing explanation\nTraffic, ruled out."
    result = await verify_answer("why is this failing", good, ["errors 12.5%"], None)
    assert result["confidence_final"] == "High" and result["lowered"] is False
    unknown = "## Likely root cause\nCan't tell.\nConfidence: Unknown\n## Evidence\n- none\n## Competing explanation\nAny."
    assert (await verify_answer("why", unknown, [], None))["confidence_final"] == "Unknown"
    assert (await verify_answer("list the jobs", "There are three jobs.", [], None))["kind"] == "direct"
    assert missing_sections(invented) == ["Competing explanation"]


async def test_verification_runs_cited_queries():
    async def run_query(query):
        if "bad" in query:
            raise ValueError("parse error")
        return {"result": [1]}

    answer = "## Likely root cause\nX\nConfidence: High\n## Evidence\n- `rate(good_total[5m])` and `rate(bad_total[5m])`.\n## Competing explanation\nY"
    result = await verify_answer("why", answer, [], run_query)
    assert result["queries"]["checked"] == 2 and len(result["queries"]["invalid"]) == 1 and result["confidence_final"] == "Low"


def test_cost_calculation():
    assert compute_cost("ollama", "any", 1000, 1000, {}) == 0
    assert compute_cost("claude", "claude-sonnet-5-5", 1_000_000, 1_000_000, {}) == 12.0
    assert compute_cost("claude", "claude-haiku-5-5", 200_000, 0, {}) == pytest.approx(0.10)
    assert compute_cost("claude", "unknown-model", 100, 100, {}) is None
    assert compute_cost("claude", "custom", 1_000_000, 0, {"custom": {"input": 1, "output": 1}}) == 1.0


async def test_investigation_logs_everything_with_cost(console, prom_server, fake_models):
    client, org_id, engine = await _company_with_source(console, prom_server)
    await _set_ollama(client, fake_models, tools=True)
    FakeProm.state.firing = True
    await engine.refresh_now()
    conversation_id, events, detail = await _ask(client)
    message = [item for item in detail["messages"] if item["role"] == "assistant"][-1]
    assert message["status"] == "complete" and message["input_tokens"] > 0 and message["duration_ms"] >= 0 and message["cost_usd"] == 0
    verification = json.loads(message["verification"])
    assert verification["kind"] == "diagnosis" and verification["has_competing_explanation"] is True and verification["confidence_final"] == "Low"
    calls = (await client.get("/api/llm-calls")).json()
    assert len(calls) == 2
    full = (await client.get(f"/api/llm-calls/{calls[0]['id']}")).json()
    assert "HighErrorRate" in json.dumps(full["request"])
    user_message = [item for item in detail["messages"] if item["role"] == "user"][0]
    assert user_message["author"] == "o@test.example"
    audit = (await client.get("/api/audit")).json()
    assert any(row["action"] == "model_prompt_sent" and row["actor"] == "o@test.example" for row in audit)
    assert (await client.get("/api/audit/verify")).json()["intact"] is True
    usage = (await client.get("/api/org/usage")).json()
    assert usage["investigations"] == 1 and usage["by_model"][0]["calls"] == 2
    export = await client.get("/api/audit/export?format=csv")
    assert export.status_code == 200 and "model_prompt_sent" in export.text


async def test_claude_requires_consent_and_key_is_never_returned(console, fake_models, make_config):
    client = await console.company("KeyCo", "o@key.example")
    await client.put("/api/ai/claude-key", json={"api_key": "sk-ant-private-value"})
    body = (await client.get("/api/ai/settings")).text
    assert "sk-ant-private-value" not in body and json.loads(body)["claude"]["key_configured"] is True
    conversation = (await client.post("/api/conversations", json={})).json()
    response = await client.post(f"/api/conversations/{conversation['id']}/messages", json={"prompt": "hi", "provider": "claude", "model": "m"})
    assert response.status_code == 422 and "turned off" in response.json()["detail"]
    row = await console.ctx.db.one("SELECT value FROM settings WHERE key = 'claude_api_key_enc'")
    assert "sk-ant-private-value" not in row["value"]


async def test_budget_blocks_new_investigations(console, prom_server, fake_models):
    client, org_id, engine = await _company_with_source(console, prom_server)
    await _set_ollama(client, fake_models)
    assert (await client.put("/api/org/spend", json={"monthly_budget_usd": 0.5, "prices": {}})).status_code == 200
    await console.ctx.db.insert(
        "INSERT INTO conversations(org_id, title, created_at, updated_at) VALUES (?, 't', 'x', 'x')", (org_id,))
    conv = await console.ctx.db.one("SELECT id FROM conversations WHERE org_id = ? LIMIT 1", (org_id,))
    from app.db import utcnow
    await console.ctx.db.insert(
        "INSERT INTO llm_calls(org_id, conversation_id, round, provider, model, endpoint, request_json, request_sha256, cost_usd, status, started_at) VALUES (?, ?, 1, 'claude', 'm', 'e', '{}', 'h', 1.0, 'complete', ?)",
        (org_id, conv["id"], utcnow()))
    conversation = (await client.post("/api/conversations", json={})).json()
    blocked = await client.post(f"/api/conversations/{conversation['id']}/messages", json={"prompt": "x"})
    assert blocked.status_code == 403 and "budget" in blocked.json()["detail"]
    usage = (await client.get("/api/org/usage")).json()
    assert usage["spent_usd"] == 1.0 and usage["forecast_usd"] > 0


class ScriptedProvider:
    name = "ollama"
    endpoint = "http://stub/api/chat"
    is_remote = False

    def __init__(self, answers=None, on_first_tool=None, loop_forever=False):
        self.requests, self.answers, self.on_first_tool, self.loop_forever = [], list(answers or []), on_first_tool, loop_forever

    def build_request(self, model, system, messages, tools):
        return {"model": model, "system": system, "messages": [dict(m) for m in messages], "tools": bool(tools)}

    async def stream(self, request):
        self.requests.append(request)
        if request["tools"] and (self.loop_forever or len(self.requests) == 1):
            yield ToolCall(f"c{len(self.requests)}", "run_instant_query", {"query": "up"})
            yield Finished("tool_calls", 10, 2)
            if self.on_first_tool:
                await self.on_first_tool()
            return
        answer = self.answers.pop(0) if self.answers else "The answer. Done."
        yield TextDelta(answer)
        yield Finished("stop", 20, 5)

    async def close(self):
        pass


async def _run(run):
    async for _ in run.stream():
        pass


async def test_context_updates_reach_the_model_between_rounds(console, prom_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    conversation = (await client.post("/api/conversations", json={})).json()

    async def fire():
        FakeProm.state.firing = True
        await engine.poll()

    provider = ScriptedProvider(on_first_tool=fire)
    manager = console.ctx.investigations
    run = manager.start(org_id, conversation["id"], "what is the status", Selection(provider, "m", True, 8000, 8000), 4)
    await _run(run)
    assert "HighErrorRate" not in provider.requests[0]["system"]
    assert "HighErrorRate" in provider.requests[-1]["system"]
    assert "Changed since this investigation started" in provider.requests[-1]["system"]


async def test_tool_rounds_are_limited(console, prom_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    conversation = (await client.post("/api/conversations", json={})).json()
    provider = ScriptedProvider(loop_forever=True)
    run = console.ctx.investigations.start(org_id, conversation["id"], "go", Selection(provider, "m", True, 8000, 8000), 3)
    await _run(run)
    assert len(provider.requests) == 3 and provider.requests[-1]["tools"] is False
    assert "used all your queries" in provider.requests[-1]["system"]


async def test_incomplete_answer_is_repaired_once(console, prom_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    conversation = (await client.post("/api/conversations", json={})).json()
    complete = "## Likely root cause\nThe pool.\nConfidence: Medium\n## Evidence\n- none\n## Competing explanation\nTraffic."
    provider = ScriptedProvider(answers=["The pool, probably.", complete])
    run = console.ctx.investigations.start(org_id, conversation["id"], "why is it failing", Selection(provider, "m", False, 8000, 8000, True), 4)
    await _run(run)
    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    message = [item for item in detail["messages"] if item["role"] == "assistant"][-1]
    assert message["content"] == complete and len(provider.requests) == 2
    assert any(event["type"] == "text_reset" for event in run.events)


async def test_tool_errors_return_to_the_model(console, prom_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    from app.investigation.tools import ToolRunner

    runner = ToolRunner(engine, console.ctx.db, console.ctx.box, console.ctx.config)
    assert (await runner.run("nope", {}))[0] is False
    assert (await runner.run("run_instant_query", {}))[0] is False
    ok, text = await runner.run("run_instant_query", {"query": "up"})
    assert ok and "42.5" in text
    assert {tool["name"] for tool in await runner.definitions()} >= {"run_instant_query", "get_recent_changes"}
    assert "get_kubernetes_state" not in {tool["name"] for tool in await runner.definitions()}


# Workflows ------------------------------------------------------------------
async def test_workflows_are_seeded_matched_and_reviewed(console):
    client = await console.company("WfCo", "o@wf.example")
    workflows = (await client.get("/api/workflows")).json()
    assert len(workflows) >= 7 and all(item["status"] == "approved" for item in workflows)
    matched = (await client.get("/api/workflows/match?alert=HighErrorRate")).json()
    assert any(item["name"] == "Error rate is high" for item in matched)
    assert (await client.get("/api/workflows/match?signal=node_disk")).json()[0]["name"] == "Disk filling up"
    made = (await client.post("/api/workflows", json={"name": "Mine", "steps": [{"title": "Check"}], "match_rules": {"keywords": ["thing"]}})).json()
    assert made["status"] == "draft"
    assert (await client.post(f"/api/workflows/{made['id']}/status", json={"status": "approved"})).json()["reviewed_by"] == "o@wf.example"
    edited = (await client.put(f"/api/workflows/{made['id']}", json={"name": "Mine 2", "steps": [{"title": "Check more"}]})).json()
    assert edited["status"] == "draft" and edited["reviewed_by"] == ""
    # Only a reviewed workflow can be followed.
    assert (await client.post(f"/api/workflows/{made['id']}/runs", json={})).status_code == 409
    await client.post(f"/api/workflows/{made['id']}/status", json={"status": "approved"})
    run = (await client.post(f"/api/workflows/{made['id']}/runs", json={})).json()
    await client.post(f"/api/workflow-runs/{run['id']}/steps/0", json={"action": "done"})
    assert (await client.post(f"/api/workflow-runs/{run['id']}/finish", json={"note": "Ran by hand", "outcome": "helped"})).status_code == 200
    assert (await client.get(f"/api/workflows/{made['id']}/records")).json()[0]["actor"] == "o@wf.example"


async def test_ai_drafts_a_workflow_for_review(console, prom_server, fake_models):
    client, org_id, engine = await _company_with_source(console, prom_server)
    await _set_ollama(client, fake_models)
    conversation_id, _, _ = await _ask(client)
    draft = await client.post(f"/api/conversations/{conversation_id}/draft-workflow")
    assert draft.status_code == 201, draft.text
    body = draft.json()
    assert body["status"] == "draft" and body["origin"] == "ai" and body["steps"][0]["command"] == "uptime"
    with pytest.raises(ValueError):
        parse_draft("not json at all")


def test_code_has_no_way_to_run_commands():
    """Workflows must never run. No application module may start processes or remote shells."""
    root = Path(__file__).parent.parent / "app"
    for path in root.rglob("*.py"):
        text = path.read_text()
        for forbidden in ("subprocess", "os.system", "os.popen", "paramiko", "asyncssh", "pty.spawn", "exec_command", "send_command("):
            assert forbidden not in text, f"{forbidden} found in {path}"


def test_cloud_sources_only_call_read_apis():
    root = Path(__file__).parent.parent / "app" / "sources" / "cloudwatch.py"
    text = root.read_text()
    calls = {name for name in ("put_", "delete_", "disable_", "enable_", "set_alarm_state", "create_") if f".{name}" in text}
    assert not calls


# Alerts ---------------------------------------------------------------------
class Hook(BaseHTTPRequestHandler):
    received: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Hook.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()


@pytest.fixture
def hook_server():
    Hook.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()


async def test_alerts_reach_channels_read_only_and_once(console, prom_server, hook_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    made = await client.post("/api/org/channels", json={"kind": "slack", "name": "ops", "url": hook_server, "min_severity": "warning"})
    assert made.status_code == 201, made.text
    await client.post("/api/org/channels", json={"kind": "email", "name": "mail", "recipients": ["o@test.example"], "min_severity": "critical"})
    assert (await client.post(f"/api/org/channels/{made.json()['id']}/test")).json()["status"] == "sent"
    assert "test" in Hook.received[-1]["text"].lower()
    Hook.received.clear()
    FakeProm.state.firing = True
    await engine.refresh_now()
    await asyncio.sleep(0.3)
    assert len(Hook.received) == 1
    text = Hook.received[0]["text"]
    assert "HighErrorRate" in text and "Read-only" in text and "Error rate is high" in text and "didn't change anything" in text
    emails = [m for m in console.outbox if m["To"] == "o@test.example" and "HighErrorRate" in m["Subject"]]
    assert len(emails) == 1
    await engine.refresh_now()
    await asyncio.sleep(0.2)
    assert len(Hook.received) == 1  # No repeat while the alert keeps firing.
    FakeProm.state.firing = False
    await engine.refresh_now()
    await asyncio.sleep(0.3)
    assert len(Hook.received) == 2 and "RESOLVED" in Hook.received[1]["text"]
    deliveries = (await client.get("/api/org/deliveries")).json()
    assert len(deliveries) >= 3 and all(item["status"] == "sent" for item in deliveries)
    channels = (await client.get("/api/org/channels")).json()
    assert hook_server not in json.dumps(channels)


async def test_alerts_already_active_on_connect_are_not_sent(console, prom_server, hook_server):
    FakeProm.state.firing = True
    client = await console.company("EarlyCo", "o@early.example")
    await client.post("/api/org/channels", json={"kind": "slack", "name": "ops", "url": hook_server})
    await client.post("/api/sources", json={"name": "lab", "config": {"host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    await (await console.ctx.engines.get(org_id)).refresh_now()
    await asyncio.sleep(0.2)
    assert Hook.received == []
    events = (await client.get("/api/events")).json()
    assert any(event["title"].startswith("Already active") for event in events)


async def test_hosted_mode_only_accepts_official_webhooks(make_config, tmp_path):
    from app.db import Database
    from app.notifications import validate_channel

    db = Database(tmp_path / "x.db")
    hosted = make_config(mode="hosted", database_url="")
    assert await validate_channel(db, hosted, 1, "slack", {"url": "http://127.0.0.1/internal"})
    assert await validate_channel(db, hosted, 1, "slack", {"url": "https://hooks.slack.com/services/a"}) is None
    assert await validate_channel(db, hosted, 1, "google_chat", {"url": "https://chat.googleapis.com/v1/spaces/x"}) is None
    assert await validate_channel(db, hosted, 1, "teams", {"url": "https://x.webhook.office.com/a"}) is None
    assert await validate_channel(db, hosted, 1, "email", {"recipients": ["stranger@elsewhere.example"]})
    db.close()


async def test_auto_investigation_notifies_and_is_limited(console, prom_server, fake_models, hook_server):
    client, org_id, engine = await _company_with_source(console, prom_server)
    await _set_ollama(client, fake_models)
    await client.post("/api/org/channels", json={"kind": "slack", "name": "ops", "url": hook_server})
    assert (await client.put("/api/org/auto-investigate", json={"enabled": True, "per_hour": 1})).status_code == 200
    FakeProm.state.firing = True
    await engine.refresh_now()
    for _ in range(40):
        await asyncio.sleep(0.1)
        if any("INVESTIGATION" in str(item) or "investigation finished" in str(item) for item in Hook.received):
            break
    assert any("AI investigation finished" in item["text"] for item in Hook.received)
    rows = (await client.get("/api/conversations")).json()["items"]
    assert len(rows) == 1 and rows[0]["trigger"] == "auto"


# Webhooks -------------------------------------------------------------------
async def test_alertmanager_and_change_webhooks(console, prom_server, fake_models):
    client, org_id, engine = await _company_with_source(console, prom_server)
    urls = (await client.post("/api/org/webhooks/rotate")).json()
    path = urls["alertmanager"].replace("http://localhost", "")
    anon = console.client()
    assert (await anon.post("/api/hooks/alertmanager/wrong-token", json={})).status_code == 404
    response = await anon.post(path, json={"alerts": [{"status": "firing", "labels": {"alertname": "DiskFull", "severity": "critical"}, "annotations": {"summary": "password=hunter2 disk"}},
                                                       {"status": "resolved", "labels": {"alertname": "Old"}}]})
    assert response.status_code == 202 and response.json()["accepted"] == 1
    events = (await client.get("/api/events")).json()
    assert any("DiskFull" in event["title"] for event in events)
    changes = urls["changes"].replace("http://localhost", "")
    assert (await anon.post(changes, json={"service": "checkout", "title": "Deployed 2.18.0"})).json()["stored"] is True
    github = await anon.post(changes, json={"repository": {"full_name": "acme/api"}, "ref": "refs/heads/main", "pusher": {"name": "sam"}, "head_commit": {"message": "fix"}}, headers={"X-GitHub-Event": "push"})
    assert github.json()["stored"] is True
    assert (await anon.post(changes, json={}, headers={"X-GitHub-Event": "ping"})).json()["stored"] is False
    await engine.refresh_now()
    titles = [item["title"] for item in engine.context["changes"]]
    assert "Deployed 2.18.0" in titles and any("Push to" in title for title in titles)
    assert "Deployed 2.18.0" in render_context(engine.context)
    old = urls["changes"].split("/")[-1]
    await client.post("/api/org/webhooks/rotate")
    assert (await anon.post(f"/api/hooks/changes/{old}", json={"title": "x"})).status_code == 404


# Accuracy scoring -----------------------------------------------------------
def test_material_hash_ignores_changing_values():
    base = {"health": {"status": "ok"}, "alerts": {"firing": []}, "targets": {"down_list": []}, "rules": {"failing": []}, "anomalies": [], "version": 1, "signals": [1]}
    assert material_hash(base) == material_hash({**base, "version": 2, "signals": [2]})
    assert material_hash(base) != material_hash({**base, "alerts": {"firing": [{"fingerprint": "x"}]}})
