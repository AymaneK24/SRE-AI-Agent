"""Checks the Ollama and Claude adapters against a server speaking their documented formats."""
import json
import threading

import pytest

from fake_models import serve

from app.llm.base import Finished, ProviderError, TextDelta, ToolCall, ToolsUnsupported
from app.llm.claude import ClaudeProvider
from app.llm.ollama import OllamaProvider

TOOLS = [{"name": "run_instant_query", "description": "d", "input_schema": {"type": "object", "properties": {}}}]


@pytest.fixture(scope="module")
def base_url():
    server = serve(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def collect(provider, request):
    return [item async for item in provider.stream(request)]


async def test_ollama_streams_text_and_usage(base_url):
    provider = OllamaProvider(base_url)
    request = provider.build_request("llama3.2:latest", "Context version 5", [{"role": "user", "content": "hi"}], None)
    assert request["options"]["num_ctx"] == 8192
    assert request["messages"][0]["role"] == "system"
    items = await collect(provider, request)
    assert "context version 5" in "".join(i.text for i in items if isinstance(i, TextDelta))
    assert isinstance(items[-1], Finished) and items[-1].output_tokens == 120
    assert (await provider.list_models())[0]["id"] == "llama3.2:latest"
    await provider.close()


async def test_ollama_tool_call_and_unsupported_model(base_url):
    provider = OllamaProvider(base_url)
    items = await collect(provider, provider.build_request("llama3.2:latest", "s", [{"role": "user", "content": "hi"}], TOOLS))
    assert any(isinstance(i, ToolCall) and i.name == "run_range_query" for i in items)
    with pytest.raises(ToolsUnsupported):
        await collect(provider, provider.build_request("notools:latest", "s", [{"role": "user", "content": "hi"}], TOOLS))
    with pytest.raises(ProviderError, match="ollama pull missing"):
        await collect(provider, provider.build_request("missing", "s", [{"role": "user", "content": "hi"}], None))
    await provider.close()


async def test_ollama_unreachable_explains_container_address():
    provider = OllamaProvider("http://localhost:1", in_container=True)
    with pytest.raises(ProviderError, match="host.docker.internal"):
        await provider.list_models()
    await provider.close()


async def test_claude_tool_round_trip(base_url):
    provider = ClaudeProvider("sk-ant-test-valid-key", base_url)
    messages = [{"role": "user", "content": "hi"}]
    request = provider.build_request("claude-test-1", "Context version 9", messages, TOOLS)
    assert request["system"] == [{"type": "text", "text": "Context version 9", "cache_control": {"type": "ephemeral"}}] and request["stream"] is True
    assert request["tools"][-1]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in TOOLS[-1], "marks a copy, not the shared definitions"
    assert request["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    plain = ClaudeProvider("sk-ant-test-valid-key", base_url, prompt_caching=False).build_request("claude-test-1", "Context version 9", messages, TOOLS)
    assert plain["system"] == "Context version 9" and "cache_control" not in json.dumps(plain)
    items = await collect(provider, request)
    call = next(i for i in items if isinstance(i, ToolCall))
    assert call.arguments == {"query": "up"}
    finished = items[-1]
    assert finished.stop_reason == "tool_use" and finished.input_tokens == 700 and finished.output_tokens == 30
    messages += [
        {"role": "assistant", "content": "", "tool_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments}], "assistant_blocks": finished.assistant_blocks},
        {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": "{}", "is_error": False},
    ]
    second = provider.build_request("claude-test-1", "Context version 9", messages, TOOLS)
    assert second["messages"][1]["content"][0]["type"] == "tool_use"
    assert second["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": call.id, "content": "{}", "cache_control": {"type": "ephemeral"}}
    items = await collect(provider, second)
    assert "context version 9" in "".join(i.text for i in items if isinstance(i, TextDelta))
    assert items[-1].stop_reason == "end_turn"
    await provider.close()


async def test_claude_bad_key_message(base_url):
    provider = ClaudeProvider("wrong", base_url)
    with pytest.raises(ProviderError, match="rejected this API key"):
        await collect(provider, provider.build_request("m", "s", [{"role": "user", "content": "x"}], None))
    with pytest.raises(ProviderError, match="rejected this API key"):
        await provider.list_models()
    await provider.close()
