"""The provider contract used by the investigation loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class ToolsUnsupported(ProviderError):
    """Raised when a model cannot accept tool definitions."""


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Finished:
    stop_reason: str = ""
    # All prompt tokens, cached or not. The cached part is also counted below, because it's billed at a lower rate.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    assistant_blocks: list[dict[str, Any]] = field(default_factory=list)


class Provider(Protocol):
    name: str
    endpoint: str
    is_remote: bool

    def build_request(
        self, model: str, system: str | list[str], messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, allow_tools: bool = True,
    ) -> dict[str, Any]:
        """`allow_tools=False` keeps the tools declared, so earlier tool calls in the history stay valid, but tells
        the model it must answer now. Providers without that switch leave the tools out."""
        ...

    def stream(self, request: dict[str, Any]) -> AsyncIterator[TextDelta | ToolCall | Finished]: ...

    async def list_models(self) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...
