"""Request checks that apply to every response: host validation, cross-site protection, limits, errors, and headers."""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from .config import Config

log = logging.getLogger("rcc.security")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_BODY_BYTES = 5 * 1024 * 1024
SECURITY_HEADERS = [
    (b"content-security-policy",
     b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
     b"font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]
HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


class _BodyTooLarge(Exception):
    pass


def _strip_port(host: str) -> str:
    host = host.lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def _bad_path(scope: dict[str, Any]) -> bool:
    raw = scope.get("raw_path") or b""
    return "\x00" in scope["path"] or any(ord(character) < 32 for character in scope["path"]) or b"%00" in raw


class SecurityMiddleware:
    def __init__(self, app: Callable[..., Awaitable[Any]], config: Config) -> None:
        self.app = app
        self.config = config
        self.headers = SECURITY_HEADERS + ([HSTS] if config.secure_cookies else [])

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            # This product has no WebSocket routes. Live updates use ordinary HTTP.
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode().lower(): value.decode("latin-1") for key, value in scope["headers"]}
        path = scope["path"]
        method = scope["method"]
        host = _strip_port(headers.get("host", ""))

        if _bad_path(scope):
            await self._reject(send, 400, "This address isn't valid.")
            return
        # Rejecting unknown Host headers stops DNS rebinding and forged links.
        # Health checks arrive by IP address from Docker and Kubernetes, and these two routes return nothing private.
        if path not in {"/healthz", "/readyz"} and "*" not in self.config.allowed_hosts and host not in self.config.accepted_hosts:
            await self._reject(send, 400, "Open the console at the address in RCC_PUBLIC_URL, or add this name to RCC_ALLOWED_HOSTS.")
            return
        # Webhooks come from other servers and use their own secret, so they skip browser checks.
        if method not in SAFE_METHODS and not path.startswith("/api/hooks/"):
            origin = headers.get("origin")
            if origin and _strip_port(urlparse(origin).netloc) != host:
                await self._reject(send, 403, "Cross-site requests aren't allowed.")
                return
            if path.startswith("/api/") and method in {"POST", "PUT", "PATCH"}:
                content_type = headers.get("content-type", "").split(";")[0].strip()
                if content_type and content_type != "application/json":
                    await self._reject(send, 415, "Send requests as application/json.")
                    return
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > MAX_BODY_BYTES:
            await self._reject(send, 413, "This request is too large.")
            return

        received = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise _BodyTooLarge
            return message

        started = False

        async def send_with_headers(message: dict[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + self.headers
                if path.startswith("/api/"):
                    message["headers"].append((b"cache-control", b"no-store"))
            await send(message)

        # Unexpected errors still carry every security header, and never show internal details.
        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _BodyTooLarge:
            if not started:
                await self._reject(send, 413, "This request is too large.")
        except Exception:
            log.exception("Unhandled error on %s %s", method, path)
            if not started:
                await self._reject(send, 500, "Something went wrong on the server. Try again, or check the server log.")
            else:
                raise

    async def _reject(self, send: Any, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode()
        extra = [(b"cache-control", b"no-store")]
        await send({
            "type": "http.response.start", "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())] + self.headers + extra,
        })
        await send({"type": "http.response.body", "body": body})


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise HTTPException(400, "This request repeats a field.")
        seen[key] = value
    return seen


async def strict_json(request: Request) -> None:
    """Refuse repeated JSON keys, so a filter in front of this app and this app can't read different values."""
    if request.method in SAFE_METHODS or request.url.path.startswith("/api/hooks/"):
        return
    body = await request.body()
    if not body:
        return
    try:
        json.loads(body, object_pairs_hook=_no_duplicate_keys)
    except ValueError:
        return  # Malformed JSON gets its normal error from the route.


def friendly_validation_error(errors: list[dict[str, Any]]) -> str:
    """One readable sentence. It doesn't repeat the input or describe the internal structure."""
    if not errors:
        return "Check what you entered and try again."
    first = errors[0]
    if first.get("type") == "value_error":
        return str(first.get("msg", "")).removeprefix("Value error, ")
    field = next((str(part) for part in reversed(first.get("loc", ())) if isinstance(part, str) and part not in {"body", "query", "path"}), "")
    label = field.replace("_", " ")
    return f"Check the {label} field and try again." if label else "Check what you entered and try again."
