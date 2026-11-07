"""Claude provider, using the Messages API over HTTPS with an API key."""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx

from .base import Finished, ProviderError, TextDelta, ToolCall

API_VERSION = "2023-06-01"
FALLBACK_MODELS = [
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "detail": ""},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "detail": ""},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5", "detail": ""},
]
RETRYABLE_STATUS = {429, 500, 502, 503, 529}


class ClaudeProvider:
    name = "claude"
    is_remote = True

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com", max_tokens: int = 4096) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/v1/messages"
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION},
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=5.0),
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def build_request(self, model: str, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, allow_tools: bool = True) -> dict[str, Any]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message["content"]}
                if message.get("is_error"):
                    block["is_error"] = True
                if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                    converted[-1]["content"].append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
            elif message["role"] == "assistant":
                if message.get("assistant_blocks"):
                    converted.append({"role": "assistant", "content": message["assistant_blocks"]})
                elif message.get("tool_calls"):
                    blocks: list[dict[str, Any]] = []
                    if message.get("content"):
                        blocks.append({"type": "text", "text": message["content"]})
                    blocks += [
                        {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]}
                        for call in message["tool_calls"]
                    ]
                    converted.append({"role": "assistant", "content": blocks})
                elif message.get("content"):
                    converted.append({"role": "assistant", "content": message["content"]})
            else:
                converted.append({"role": "user", "content": message["content"]})
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": converted,
            "stream": True,
        }
        if tools:
            request["tools"] = tools
            if not allow_tools:
                request["tool_choice"] = {"type": "none"}
        return request

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[TextDelta | ToolCall | Finished]:
        attempt = 0
        while True:
            produced = False
            try:
                async for item in self._stream_once(request):
                    produced = True
                    yield item
                return
            except ProviderError as error:
                attempt += 1
                if error.retryable and not produced and attempt <= 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise

    async def _stream_once(self, request: dict[str, Any]) -> AsyncIterator[TextDelta | ToolCall | Finished]:
        finished = Finished()
        blocks: dict[int, dict[str, Any]] = {}
        partial_json: dict[int, str] = {}
        try:
            async with self._client.stream("POST", "/v1/messages", json=request) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")
                    raise _status_error(response.status_code, body)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:].strip())
                    kind = event.get("type")
                    if kind == "message_start":
                        usage = event.get("message", {}).get("usage", {})
                        finished.input_tokens = usage.get("input_tokens")
                    elif kind == "content_block_start":
                        blocks[event["index"]] = dict(event.get("content_block", {}))
                    elif kind == "content_block_delta":
                        delta = event.get("delta", {})
                        index = event["index"]
                        block = blocks.setdefault(index, {})
                        if delta.get("type") == "text_delta":
                            block["text"] = block.get("text", "") + delta.get("text", "")
                            yield TextDelta(delta.get("text", ""))
                        elif delta.get("type") == "input_json_delta":
                            partial_json[index] = partial_json.get(index, "") + delta.get("partial_json", "")
                        elif delta.get("type") == "thinking_delta":
                            block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                        elif delta.get("type") == "signature_delta":
                            block["signature"] = delta.get("signature", "")
                    elif kind == "content_block_stop":
                        index = event["index"]
                        block = blocks.get(index, {})
                        if block.get("type") == "tool_use":
                            raw = partial_json.get(index, "")
                            try:
                                block["input"] = json.loads(raw) if raw else {}
                            except ValueError:
                                block["input"] = {}
                            yield ToolCall(block.get("id", ""), block.get("name", ""), block["input"])
                    elif kind == "message_delta":
                        finished.stop_reason = event.get("delta", {}).get("stop_reason") or finished.stop_reason
                        finished.output_tokens = event.get("usage", {}).get("output_tokens", finished.output_tokens)
                    elif kind == "error":
                        error = event.get("error", {})
                        raise ProviderError(
                            f"Claude API error: {error.get('message', 'unknown error')}",
                            retryable=error.get("type") in {"overloaded_error", "api_error"},
                        )
                    # Other event types, including ping, need no handling.
        except httpx.ConnectError as error:
            raise ProviderError(
                "Could not reach the Claude API. Check this machine's internet connection and firewall.",
                retryable=True,
            ) from error
        except httpx.TimeoutException as error:
            raise ProviderError("The Claude API stopped responding.", retryable=True) from error
        except httpx.TransportError as error:
            raise ProviderError(f"The connection to the Claude API failed: {error}", retryable=True) from error
        finished.assistant_blocks = [blocks[index] for index in sorted(blocks)]
        yield finished

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            response = await self._client.get("/v1/models", params={"limit": 100}, timeout=15.0)
        except httpx.TransportError as error:
            raise ProviderError(
                "Could not reach the Claude API. Check this machine's internet connection and firewall."
            ) from error
        if response.status_code != 200:
            raise _status_error(response.status_code, response.text)
        return [
            {"id": item.get("id"), "label": item.get("display_name") or item.get("id"), "detail": ""}
            for item in response.json().get("data", [])
        ]


def _status_error(status: int, body: str) -> ProviderError:
    try:
        detail = json.loads(body).get("error", {}).get("message", "")
    except ValueError:
        detail = ""
    if status == 401:
        return ProviderError("The Claude API rejected this API key. Check the key in Connections.")
    if status == 403:
        return ProviderError(f"This API key isn't allowed to do that. {detail}".strip())
    if status == 404:
        return ProviderError(f"The Claude API can't find that model. {detail}".strip())
    if status == 429:
        return ProviderError(f"The Claude API rate limit was reached. {detail}".strip(), retryable=True)
    if status == 529:
        return ProviderError("The Claude API is overloaded. Try again shortly.", retryable=True)
    return ProviderError(
        f"The Claude API returned HTTP {status}. {detail}".strip(), retryable=status in RETRYABLE_STATUS
    )
