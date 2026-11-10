"""Cost savings and focused investigations: caching, a cheaper model for query rounds, gathered evidence,
short answers, LogQL repair, and what each role sees."""
import json

import pytest
import pytest_asyncio
from conftest import Console, FakeProm
from tests_helpers import reset_database

from app.context.render import focus_context, render_context
from app.costs import cache_saving, compute_cost
from app.investigation.logql import repair_logql, simple_logql
from app.investigation.verification import missing_sections
from app.main import create_app


# LogQL repair ------------------------------------------------------------------------
@pytest.mark.parametrize(("written", "expected"), [
    ("{namespace=~'.+', app=~'payment', message='.*'} | error", '{namespace=~".+", app=~"payment"} |= "error"'),
    ("{app='payment', msg=~'refused'}", '{app="payment"} |~ "(?i)refused"'),
    ('{namespace="shop"} | "timeout"', '{namespace="shop"} |= "timeout"'),
    ("error in payment", '{job=~".+"} |~ "(?i)(error|payment)"'),
    ('{namespace!="kube-system"} | json | level="error"', '{job=~".+", namespace!="kube-system"} | json | level="error"'),
])
def test_logql_mistakes_are_corrected(written, expected):
    repaired, fixes = repair_logql(written)
    assert repaired == expected and fixes


def test_valid_logql_is_left_alone():
    for query in ('{namespace="shop", app="payment"} |= "ERROR"', '{job="shop/payment"} |~ "(?i)(error|fatal)"', 'sum by (job) (count_over_time({job=~".+"} [5m]))'):
        assert repair_logql(query) == (query, [])


def test_the_last_resort_search_always_parses():
    assert simple_logql("{namespace=~'.+', app=~'payment', message='.*'} | error") == '{namespace=~".+", app=~"payment"} |~ "(?i)error"'
    assert simple_logql("show me something") == '{job=~".+"} |~ "(?i)(error|exception|fatal|panic)"'


# Costs --------------------------------------------------------------------------------
def test_cached_tokens_cost_less_and_the_saving_is_counted():
    full = compute_cost("claude", "claude-sonnet-5-5", 100_000, 1000, {})
    cached = compute_cost("claude", "claude-sonnet-5-5", 100_000, 1000, {}, cache_read=90_000)
    assert cached < full / 3
    assert cache_saving("claude", "claude-sonnet-5-5", 100_000, 1000, {}, cache_read=90_000) == pytest.approx(full - cached)
    written = compute_cost("claude", "claude-sonnet-5-5", 100_000, 0, {}, cache_write=100_000)
    assert written == pytest.approx(compute_cost("claude", "claude-sonnet-5-5", 100_000, 0, {}) * 1.25), "writing the cache costs a quarter more once"
    assert compute_cost("ollama", "qwen2.5:3b", 10_000, 10_000, {}, cache_read=9000) == 0


def test_short_answers_need_fewer_sections():
    short = "## Summary\nx\n## Likely root cause\ny\nConfidence: Low\n## Evidence\n- z\n## Next checks\n- w"
    assert missing_sections(short, "short") == [] and "Competing explanation" in missing_sections(short)


# Focus --------------------------------------------------------------------------------
def _context():
    crash = {"fingerprint": "k8s:shop/deployment/payment:crashloop", "kind": "k8s", "severity": "critical", "title": "payment crash-looping",
             "detail": {"namespace": "shop", "workload": "payment"}}
    other = {"fingerprint": "k8s:ads/deployment/banner:oomkilled", "kind": "k8s", "severity": "critical", "title": "banner OOMKilled",
             "detail": {"namespace": "ads", "workload": "banner"}}
    alert = {"fingerprint": "alert:HighErrorRate", "name": "HighErrorRate", "severity": "warning", "labels": {"job": "checkout"}, "summary": "errors"}
    return {
        "version": 7, "as_of": "2026-11-10T12:00:00Z", "health": {"status": "ok"}, "coverage": {"has_metrics": True},
        "problems": [crash, other, {"fingerprint": alert["fingerprint"], "kind": "alert", "severity": "warning", "title": "Alert firing: HighErrorRate", "detail": alert}],
        "alerts": {"firing": [alert], "pending": []}, "targets": {"down_list": []}, "anomalies": [], "signals": [], "events": [],
        "connections": [
            {"id": 1, "kind": "kubernetes", "name": "aks", "status": "ok", "unhealthy_pods": [
                {"namespace": "shop", "pod": "payment-1", "phase": "Running", "restarts": 9, "reasons": ["CrashLoopBackOff"]},
                {"namespace": "ads", "pod": "banner-1", "phase": "Running", "restarts": 3, "reasons": ["OOMKilled"]}],
             "deployments_below_desired": [], "warning_events": []},
            {"id": 2, "kind": "loki", "name": "loki", "status": "ok", "selector": "{}", "error_lines": [{"job": "shop/payment", "errors_5m": 40}, {"job": "ads/banner", "errors_5m": 3}]},
        ],
    }


def test_focus_keeps_only_the_chosen_problems_and_their_evidence():
    context = _context()
    focused = focus_context(context, ["k8s:shop/deployment/payment:crashloop"])
    text = render_context(focused)
    assert "FOCUS: the person chose" in text and "payment crash-looping" in text
    assert "banner" not in text and "HighErrorRate" not in text, "other problems, their pods and log streams are left out"
    assert "shop/payment" in text and len(text) < len(render_context(context))
    assert focus_context(context, ["gone"]) is context, "a focus that no longer matches anything keeps the whole context"


# End to end with Claude ----------------------------------------------------------------
@pytest_asyncio.fixture
async def claude_console(make_config, fake_models):
    reset_database()  # Each test starts from an empty database, on PostgreSQL too.
    app = create_app(make_config(anthropic_base_url=fake_models))
    async with app.router.lifespan_context(app):
        yield Console(app)


async def _claude_company(console, prom_server, evidence_model=""):
    client = await console.company("SaveCo", "o@save.example")
    source = await client.post("/api/sources", json={"name": "lab", "poll_seconds": 5, "config": {"host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})
    assert source.status_code == 201, source.text
    assert (await client.put("/api/ai/claude-key", json={"api_key": "sk-ant-test-valid-key"})).status_code == 204
    settings = (await client.get("/api/ai/settings")).json()
    settings["claude"].update(model="claude-sonnet-5-5", consent=True, use_tools=True)
    for key in ("key_source", "key_configured"):
        settings["claude"].pop(key, None)
    settings["gemini"].pop("key_source", None)
    settings["gemini"].pop("key_configured", None)
    settings["provider"] = "claude"
    assert (await client.put("/api/ai/settings", json=settings)).status_code == 200
    saved = await client.put("/api/ai/savings", json={"prompt_caching": True, "gather_evidence": True, "answer_style": "short", "evidence_model": {"claude": evidence_model}})
    assert saved.status_code == 200, saved.text
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    engine = await console.ctx.engines.get(org_id)
    FakeProm.state.firing = True
    await engine.refresh_now()
    return client, org_id, engine


async def _run(client, **body):
    conversation = (await client.post("/api/conversations", json={})).json()
    run = await client.post(f"/api/conversations/{conversation['id']}/messages", json={"prompt": "Why is checkout failing?", **body})
    assert run.status_code == 202, run.text
    async with client.stream("GET", f"/api/runs/{run.json()['run_id']}/stream") as response:
        events = [json.loads(line) async for line in response.aiter_lines() if line]
    return conversation["id"], events, (await client.get(f"/api/conversations/{conversation['id']}")).json()


async def test_cheaper_model_queries_and_main_model_answers_with_caching(claude_console, prom_server):
    client, org_id, engine = await _claude_company(claude_console, prom_server, evidence_model="claude-haiku-5-5")
    focus = next(item["fingerprint"] for item in engine.context["problems"] if item["kind"] == "alert")
    conversation_id, events, detail = await _run(client, focus=[focus], answer_style="short")
    message = [item for item in detail["messages"] if item["role"] == "assistant"][-1]
    assert message["status"] == "complete" and message["answer_style"] == "short" and json.loads(message["focus"]) == [focus]
    calls = await claude_console.ctx.db.all("SELECT model, purpose, cache_read_tokens, saved_usd, request_json FROM llm_calls WHERE conversation_id = ? ORDER BY id", (conversation_id,))
    assert [row["purpose"] for row in calls][0] == "evidence" and calls[-1]["purpose"] == "answer"
    assert calls[0]["model"] == "claude-haiku-5-5" and calls[-1]["model"] == "claude-sonnet-5-5"
    assert any(row["cache_read_tokens"] for row in calls) and any((row["saved_usd"] or 0) > 0 for row in calls)
    first = json.loads(calls[0]["request_json"])
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"} and "FOCUS: the person chose" in first["system"][0]["text"]
    texts = [event["text"] for event in events if event["type"] == "text"]
    assert texts and "Scripted test answer" in "".join(texts), "only the main model's answer is shown"
    savings = (await client.get("/api/ai/savings")).json()
    assert savings["month"]["cache_saved_usd"] > 0 and savings["month"]["evidence_calls"] >= 1 and savings["month"]["focused_investigations"] == 1
    pdf = await client.get(f"/api/conversations/{conversation_id}/report.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


async def test_costs_and_failed_queries_are_for_admins(claude_console, prom_server):
    owner, org_id, engine = await _claude_company(claude_console, prom_server)
    conversation_id, _, detail = await _run(owner)
    await owner.post(f"/api/conversations/{conversation_id}/share", json={"shared": True})
    message = [item for item in detail["messages"] if item["role"] == "assistant"][-1]
    assert message["input_tokens"] and message["cost_usd"] is not None
    await claude_console.ctx.db.insert(
        "INSERT INTO tool_calls(org_id, conversation_id, message_id, name, arguments, result, ok, duration_ms, origin, created_at) VALUES (?, ?, ?, 'query_logs', '{}', 'parse error', 0, 1, 'model', 'now')",
        (org_id, conversation_id, message["id"]))
    assert (await owner.post("/api/org/members/invite", json={"email": "e@save.example", "name": "Eve Engineer", "role": "engineer"})).status_code == 201
    engineer = claude_console.client()
    token = claude_console.last_link_token("e@save.example")
    assert (await engineer.post("/api/auth/accept-invite", json={"token": token, "name": "Eve Engineer", "password": "engineer-Pass-Phrase-66"})).status_code == 200
    seen = (await engineer.get(f"/api/conversations/{conversation_id}")).json()
    answer = [item for item in seen["messages"] if item["role"] == "assistant"][-1]
    assert answer["model"] == "claude-sonnet-5-5" and answer["cost_usd"] is None and answer["input_tokens"] is None and answer["duration_ms"] is None
    assert all(tool["ok"] for tool in seen["tool_calls"]), "failed queries are hidden from engineers"
    assert any(not tool["ok"] for tool in (await owner.get(f"/api/conversations/{conversation_id}")).json()["tool_calls"])
    assert (await engineer.get("/api/ai/savings")).status_code == 403
