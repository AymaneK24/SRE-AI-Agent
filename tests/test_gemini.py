"""Gemini, checked against a server that speaks the documented generateContent streaming format."""
import json
import threading

import pytest
from fake_models import serve

from app.llm.base import ProviderError, TextDelta, ToolCall
from app.llm.gemini import GeminiProvider

TOOLS = [{"name": "run_instant_query", "description": "d",
          "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}]


@pytest.fixture(scope="module")
def base_url():
    server = serve(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def collect(provider, request):
    return [item async for item in provider.stream(request)]


async def test_gemini_tool_round_trip_keeps_the_thought_signature(base_url):
    provider = GeminiProvider("gemini-test-key", base_url)
    request = provider.build_request("gemini-3.8-flash", "Context version 9", [{"role": "user", "content": "why?"}], TOOLS)
    assert request["systemInstruction"]["parts"][0]["text"] == "Context version 9" and "store" not in request
    assert "additionalProperties" not in json.dumps(request["tools"]), "keys the API rejects are removed from tool schemas"
    first = await collect(provider, request)
    call = next(item for item in first if isinstance(item, ToolCall))
    finished = first[-1]
    assert call.name == "run_instant_query" and call.arguments == {"query": "up"} and finished.stop_reason == "tool_use"
    assert finished.input_tokens == 640 and finished.output_tokens == 42, "thinking tokens are billed as output"
    assert finished.assistant_blocks[0]["thoughtSignature"] == "sig-abc"
    history = [{"role": "user", "content": "why?"},
               {"role": "assistant", "content": "", "tool_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments}], "assistant_blocks": finished.assistant_blocks},
               {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": '{"series_total": 1}', "is_error": False}]
    second_request = provider.build_request("gemini-3.8-flash", "Context version 9", history, TOOLS)
    response_part = second_request["contents"][-1]["parts"][0]["functionResponse"]
    assert response_part == {"name": "run_instant_query", "response": {"series_total": 1}}
    second = await collect(provider, second_request)
    text = "".join(item.text for item in second if isinstance(item, TextDelta))
    assert "context version 9" in text and second[-1].stop_reason == "end_turn"

    without_signature = [dict(item) for item in history]
    without_signature[1] = {**history[1], "assistant_blocks": None}
    with pytest.raises(ProviderError) as error:
        await collect(provider, provider.build_request("gemini-3.8-flash", "s", without_signature, TOOLS))
    assert "thought_signature" in error.value.message, "the API refuses a function call sent back without its signature"
    assert [model["id"] for model in await provider.list_models()] == ["gemini-3.8-flash"], "only Gemini text models are listed"
    await provider.close()


async def test_gemini_bad_key_and_tool_errors(base_url):
    provider = GeminiProvider("wrong", base_url)
    with pytest.raises(ProviderError) as error:
        await collect(provider, provider.build_request("gemini-3.8-flash", "s", [{"role": "user", "content": "hi"}], None))
    assert "rejected this API key" in error.value.message
    with pytest.raises(ProviderError):
        await provider.list_models()
    await provider.close()
    request = GeminiProvider("k", base_url).build_request("m", "s", [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "gemini-call-1", "name": "q", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "gemini-call-1", "name": "q", "content": "Query error: bad", "is_error": True}], None)
    assert request["contents"][-1]["parts"][0]["functionResponse"]["response"] == {"error": "Query error: bad"}


async def test_gemini_investigation_end_to_end(console, prom_server, fake_models):
    from dataclasses import replace

    console.ctx.config = replace(console.ctx.config, gemini_base_url=fake_models)
    client = await console.company("GemCo", "o@gem.example")
    await client.post("/api/sources", json={"name": "lab", "config": {"host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    await (await console.ctx.engines.get(org_id)).refresh_now()
    assert (await client.put("/api/ai/gemini-key", json={"api_key": "gemini-test-key"})).status_code == 204
    settings = (await client.get("/api/ai/settings")).json()
    assert "gemini-test-key" not in json.dumps(settings) and settings["gemini"]["key_configured"] is True
    models = (await client.get("/api/ai/providers/gemini/models")).json()
    assert models["ok"] and models["models"][0]["id"] == "gemini-3.8-flash"
    body = {k: settings[k] for k in ("provider", "max_rounds", "ollama", "claude", "gemini")}
    for section in ("claude", "gemini"):
        for key in ("key_source", "key_configured"):
            body[section].pop(key, None)
    body["provider"] = "gemini"
    body["gemini"].update(model="gemini-3.8-flash")
    refused = await client.put("/api/ai/settings", json=body)
    assert refused.status_code == 422 and "Google" in refused.json()["detail"], "Gemini needs consent to send data to Google"
    body["gemini"]["consent"] = True
    assert (await client.put("/api/ai/settings", json=body)).status_code == 200
    without_gemini = {k: body[k] for k in ("provider", "max_rounds", "ollama", "claude")}
    assert (await client.put("/api/ai/settings", json=without_gemini)).status_code == 200
    assert (await client.get("/api/ai/settings")).json()["gemini"]["model"] == "gemini-3.8-flash", "an older client keeps the Gemini settings"

    conversation = (await client.post("/api/conversations", json={})).json()
    run = await client.post(f"/api/conversations/{conversation['id']}/messages", json={"prompt": "Why is checkout failing?"})
    assert run.status_code == 202, run.text
    async with client.stream("GET", f"/api/runs/{run.json()['run_id']}/stream") as response:
        events = [json.loads(line) async for line in response.aiter_lines() if line]
    finished = [event for event in events if event["type"] == "finished"][0]
    assert finished["status"] == "complete", finished
    calls = (await client.get("/api/llm-calls")).json()
    assert len(calls) == 2 and {call["provider"] for call in calls} == {"gemini"}
    audit = (await client.get("/api/audit")).json()
    sent = [row for row in audit if row["action"] == "model_prompt_sent"]
    assert sent and all(json.loads(row["detail"])["leaves_this_system"] is True for row in sent)
    row = await console.ctx.db.one("SELECT value FROM settings WHERE key = 'gemini_api_key_enc'")
    assert "gemini-test-key" not in row["value"]
