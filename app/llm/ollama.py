"""Ollama provider, using the /api/chat streaming endpoint."""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

import httpx

from ..net_guard import TargetPolicy, guarded_transport
from .base import Finished, ProviderError, TextDelta, ToolCall, ToolsUnsupported


class OllamaProvider:
    name = "ollama"
    is_remote = False

    def __init__(self, base_url: str, num_ctx: int = 8192, in_container: bool = False, policy: TargetPolicy | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/api/chat"
        self.num_ctx = num_ctx
        self.in_container = in_container
        # Local models can spend minutes loading or generating on a CPU.
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0),
            follow_redirects=False,
            trust_env=False,
            transport=guarded_transport(policy if policy is not None else TargetPolicy(allow_loopback=True)),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _unreachable(self) -> ProviderError:
        hint = "Check that Ollama is running."
        if self.in_container and ("localhost" in self.base_url or "127.0.0.1" in self.base_url):
            hint = "This app is in a container, so use http://host.docker.internal:11434 or the host's IP address."
        return ProviderError(f"Could not reach Ollama at {self.base_url}. {hint}")

    def build_request(self, model: str, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, allow_tools: bool = True) -> dict[str, Any]:
        converted: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for message in messages:
            if message["role"] == "tool":
                converted.append({"role": "tool", "tool_name": message["name"], "content": message["content"]})
            elif message["role"] == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": message.get("content", "")}
                if message.get("tool_calls"):
                    item["tool_calls"] = [
                        {"function": {"name": call["name"], "arguments": call["arguments"]}}
                        for call in message["tool_calls"]
                    ]
                converted.append(item)
            else:
                converted.append({"role": "user", "content": message["content"]})
        request: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "stream": True,
            "options": {"num_ctx": self.num_ctx},
        }
        if tools and allow_tools:
            request["tools"] = [
                {"type": "function", "function": {
                    "name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"],
                }}
                for tool in tools
            ]
        return request

    async def stream(self, request: dict[str, Any]) -> AsyncIterator[TextDelta | ToolCall | Finished]:
        try:
            async with self._client.stream("POST", "/api/chat", json=request) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")
                    message = _error_text(body) or f"HTTP {response.status_code}"
                    if "does not support tools" in message:
                        raise ToolsUnsupported(message)
                    if response.status_code == 404:
                        raise ProviderError(
                            f"Ollama doesn't have this model: {request['model']}. Run: ollama pull {request['model']}"
                        )
                    raise ProviderError(f"Ollama returned an error: {message}")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise ProviderError(f"Ollama returned an error: {data['error']}")
                    message = data.get("message") or {}
                    if message.get("content"):
                        yield TextDelta(message["content"])
                    for call in message.get("tool_calls") or []:
                        function = call.get("function") or {}
                        arguments = function.get("arguments") or {}
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except ValueError:
                                arguments = {"_invalid_arguments": arguments}
                        yield ToolCall(uuid.uuid4().hex[:12], function.get("name", ""), arguments)
                    if data.get("done"):
                        yield Finished(
                            stop_reason=data.get("done_reason", "stop"),
                            input_tokens=data.get("prompt_eval_count"),
                            output_tokens=data.get("eval_count"),
                        )
                        return
        except httpx.ConnectError as error:
            raise self._unreachable() from error
        except httpx.TimeoutException as error:
            raise ProviderError(
                "Ollama stopped responding for 10 minutes. The model may be too large for this machine."
            ) from error
        except httpx.TransportError as error:
            raise ProviderError(f"The connection to Ollama failed: {error}") from error

    async def list_models(self) -> list[dict[str, Any]]:
        try:
            response = await self._client.get("/api/tags", timeout=5.0)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise ProviderError(f"Ollama returned HTTP {error.response.status_code}.") from error
        except httpx.TransportError as error:
            raise self._unreachable() from error
        return [
            {
                "id": item.get("name"),
                "label": item.get("name"),
                "detail": _ollama_detail(item),
            }
            for item in response.json().get("models", [])
        ]


def _ollama_detail(item: dict[str, Any]) -> str:
    size = item.get("size") or 0
    params = (item.get("details") or {}).get("parameter_size") or ""
    return " ".join(part for part in (params, f"{size / 2**30:.1f} GB" if size else "") if part)


def _error_text(body: str) -> str:
    try:
        return json.loads(body).get("error", body)
    except ValueError:
        return body[:500]
