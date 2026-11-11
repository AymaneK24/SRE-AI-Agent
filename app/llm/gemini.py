"""Gemini provider, using Google's generateContent API over HTTPS with an API key.

The API is stateless: each request carries the whole conversation, and nothing is stored on Google's side for
later use by this console. That matches how Claude and Ollama are called here, so the investigation loop doesn't change.

Gemini 3 and later return a thought signature next to each function call. It must be sent back unchanged with
the next request, or the API refuses it, so a model turn is kept exactly as received (`assistant_blocks`).
Docs: https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

import httpx

from .base import Finished, ProviderError, TextDelta, ToolCall

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# Models that can't hold a text conversation with tools: they control a computer, speak, draw, or embed.
NOT_FOR_CHAT = ("computer-use", "tts", "image", "embedding", "live", "native-audio", "robotics", "aqa", "learnlm")
# Keys the generateContent schema format doesn't accept in function parameters.
UNSUPPORTED_SCHEMA_KEYS = {"additionalProperties", "$schema", "default", "examples"}


def _schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _schema(item) for key, item in value.items() if key not in UNSUPPORTED_SCHEMA_KEYS}
    if isinstance(value, list):
        return [_schema(item) for item in value]
    return value


def _result_object(text: str, is_error: bool) -> dict[str, Any]:
    """functionResponse.response must be an object. Tool results here are JSON text, so pass objects through."""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and not is_error:
        return parsed
    return {"error": text} if is_error else {"result": parsed if parsed is not None else text}


class GeminiProvider:
    name = "gemini"
    is_remote = True

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, max_tokens: int = 8192) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/v1beta/models"
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers={"x-goog-api-key": api_key},
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=5.0), follow_redirects=False, trust_env=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def build_request(self, model: str, system: str | list[str], messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, allow_tools: bool = True) -> dict[str, Any]:
        # The stable part comes first, so a repeated prompt start can be reused (Gemini's implicit cache, Ollama's prompt cache).
        system = system if isinstance(system, str) else "\n\n".join(part for part in system if part)
        contents: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        for message in messages:
            if message["role"] == "tool":
                part = {"functionResponse": {"name": message.get("name") or names.get(message["tool_call_id"], ""),
                                             "response": _result_object(message["content"], bool(message.get("is_error")))}}
                if message["tool_call_id"] and not message["tool_call_id"].startswith("gemini-call-"):
                    part["functionResponse"]["id"] = message["tool_call_id"]
                if contents and contents[-1]["role"] == "user" and "functionResponse" in contents[-1]["parts"][0]:
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
            elif message["role"] == "assistant":
                for call in message.get("tool_calls") or []:
                    names[call["id"]] = call["name"]
                if message.get("assistant_blocks"):
                    contents.append({"role": "model", "parts": message["assistant_blocks"]})
                elif message.get("tool_calls"):
                    parts: list[dict[str, Any]] = [{"text": message["content"]}] if message.get("content") else []
                    parts += [{"functionCall": {"name": call["name"], "args": call["arguments"]}} for call in message["tool_calls"]]
                    if message.get("other_model"):
                        # Calls made by another model carry no thought signature of this one. Google documents this value for that case.
                        for call_part in parts:
                            if "functionCall" in call_part:
                                call_part["thoughtSignature"] = "skip_thought_signature_validator"
                    contents.append({"role": "model", "parts": parts})
                elif message.get("content"):
                    contents.append({"role": "model", "parts": [{"text": message["content"]}]})
            else:
                contents.append({"role": "user", "parts": [{"text": message["content"]}]})
        request: dict[str, Any] = {
            "model": model,
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        if tools:
            request["tools"] = [{"functionDeclarations": [
                {"name": tool["name"], "description": tool["description"], "parameters": _schema(tool["input_schema"])} for tool in tools]}]
            if not allow_tools:
                request["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
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
        body = {key: value for key, value in request.items() if key != "model"}
        model = str(request["model"]).removeprefix("models/")
        finished = Finished()
        parts: list[dict[str, Any]] = []
        calls = 0
        try:
            async with self._client.stream("POST", f"/v1beta/models/{model}:streamGenerateContent", params={"alt": "sse"}, json=body) as response:
                if response.status_code != 200:
                    raise _status_error(response.status_code, (await response.aread()).decode(errors="replace"))
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = json.loads(line[5:].strip())
                    if "error" in chunk:
                        error = chunk["error"]
                        raise ProviderError(f"Gemini API error: {error.get('message', 'unknown error')}", retryable=error.get("code") in RETRYABLE_STATUS)
                    usage = chunk.get("usageMetadata") or {}
                    if usage:
                        finished.input_tokens = usage.get("promptTokenCount", finished.input_tokens)
                        # Gemini caches a repeated prompt start by itself (implicit caching) and reports how much it reused.
                        finished.cache_read_tokens = int(usage.get("cachedContentTokenCount") or finished.cache_read_tokens or 0)
                        finished.output_tokens = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0) or finished.output_tokens
                    feedback = chunk.get("promptFeedback") or {}
                    if feedback.get("blockReason"):
                        finished.stop_reason = "refusal"
                    for candidate in chunk.get("candidates", [])[:1]:
                        for part in (candidate.get("content") or {}).get("parts", []) or []:
                            parts.append(part)
                            if "functionCall" in part:
                                call = part["functionCall"]
                                calls += 1
                                call_id = call.get("id") or f"gemini-call-{calls}"
                                yield ToolCall(call_id, call.get("name", ""), call.get("args") or {})
                            elif part.get("text") and not part.get("thought"):
                                yield TextDelta(part["text"])
                        reason = candidate.get("finishReason")
                        if reason:
                            finished.stop_reason = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens", "SAFETY": "refusal",
                                                    "RECITATION": "refusal", "PROHIBITED_CONTENT": "refusal"}.get(reason, reason.lower())
        except httpx.ConnectError as error:
            raise ProviderError("Could not reach the Gemini API. Check this machine's internet connection and firewall.", retryable=True) from error
        except httpx.TimeoutException as error:
            raise ProviderError("The Gemini API stopped responding.", retryable=True) from error
        except httpx.TransportError as error:
            raise ProviderError(f"The connection to the Gemini API failed: {error}", retryable=True) from error
        if calls:
            finished.stop_reason = "tool_use"
        # Kept exactly as received, thought signatures included, to send back with the next round.
        finished.assistant_blocks = parts
        yield finished

    async def list_models(self) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        token = ""
        for _ in range(5):
            params: dict[str, Any] = {"pageSize": 100}
            if token:
                params["pageToken"] = token
            try:
                response = await self._client.get("/v1beta/models", params=params, timeout=15.0)
            except httpx.TransportError as error:
                raise ProviderError("Could not reach the Gemini API. Check this machine's internet connection and firewall.") from error
            if response.status_code != 200:
                raise _status_error(response.status_code, response.text)
            data = response.json()
            for item in data.get("models", []):
                if "generateContent" not in item.get("supportedGenerationMethods", []):
                    continue
                model_id = str(item.get("name", "")).removeprefix("models/")
                if not model_id.startswith("gemini") or any(word in model_id for word in NOT_FOR_CHAT):
                    continue
                models.append({"id": model_id, "label": item.get("displayName") or model_id, "detail": ""})
            token = data.get("nextPageToken", "")
            if not token:
                break
        return models


def _status_error(status: int, body: str) -> ProviderError:
    try:
        detail = json.loads(body).get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        detail = ""
    if status in {401, 403} or "API key not valid" in detail:
        return ProviderError("The Gemini API rejected this API key. Check the key in Connections.")
    if status == 404:
        return ProviderError(f"The Gemini API can't find that model. {detail}".strip())
    if status == 429:
        return ProviderError(f"The Gemini API rate limit or quota was reached. {detail}".strip(), retryable=True)
    if status == 400:
        return ProviderError(f"The Gemini API refused the request. {detail}".strip())
    return ProviderError(f"The Gemini API returned HTTP {status}. {detail}".strip(), retryable=status in RETRYABLE_STATUS)
