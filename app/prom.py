"""Read-only Prometheus HTTP API client. It only calls fixed query and status endpoints."""
from __future__ import annotations

import ssl
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .net_guard import TargetPolicy, guarded_transport


class PrometheusError(Exception):
    def __init__(self, message: str, kind: str = "error", status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status = status


@dataclass(frozen=True)
class ServerConfig:
    id: int | None
    name: str
    scheme: str
    host: str
    port: int
    path_prefix: str = ""
    auth_type: str = "none"
    username: str = ""
    secret: str = ""
    verify_tls: bool = True
    poll_seconds: int = 15

    @property
    def base_url(self) -> str:
        prefix = self.path_prefix.strip("/")
        return f"{self.scheme}://{self.host}:{self.port}" + (f"/{prefix}" if prefix else "")


def explain_connection_error(config: ServerConfig, error: Exception, in_container: bool) -> PrometheusError:
    """Turn a transport failure into a message that says what to check."""
    loopback = config.host in {"localhost", "127.0.0.1", "::1", "[::1]"}
    if isinstance(error, httpx.ConnectError):
        cause = str(error.__cause__ or error)
        if isinstance(error.__cause__, ssl.SSLError) or "SSL" in cause or "CERTIFICATE" in cause:
            return PrometheusError(
                f"TLS failed for {config.base_url}. Check the certificate, or turn off certificate "
                "checks only if this server is on a network you trust.",
                "tls",
            )
        if loopback and in_container:
            return PrometheusError(
                f"Nothing answered at {config.base_url}. This app is running in a container, so "
                f"{config.host} means the container itself. Use host.docker.internal or the "
                "server's IP address.",
                "unreachable",
            )
        return PrometheusError(
            f"Nothing answered at {config.base_url}. Check the IP address and port, that "
            "Prometheus is running, and that a firewall is not blocking the port.",
            "unreachable",
        )
    if isinstance(error, httpx.TimeoutException):
        return PrometheusError(
            f"{config.base_url} did not respond in time. Check the network path and firewall.",
            "timeout",
        )
    return PrometheusError(f"Could not reach {config.base_url}: {error}", "unreachable")


class PrometheusClient:
    def __init__(self, config: ServerConfig, in_container: bool = False, timeout: float = 15.0, auth: httpx.Auth | None = None, policy: Any = None) -> None:
        self.config = config
        self.in_container = in_container
        headers = {"Accept": "application/json", "User-Agent": "root-cause-console/2.0"}
        if config.auth_type == "basic":
            auth = httpx.BasicAuth(config.username, config.secret)
        elif config.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {config.secret}"
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            auth=auth,
            timeout=httpx.Timeout(timeout, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            transport=guarded_transport(policy if policy is not None else TargetPolicy(allow_loopback=True), config.verify_tls),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except (httpx.TransportError, httpx.InvalidURL) as error:
            raise explain_connection_error(self.config, error, self.in_container) from error
        if response.status_code in {401, 403}:
            raise PrometheusError(
                "Prometheus rejected the credentials. Check the sign-in method, username, and password or token.",
                "auth",
                response.status_code,
            )
        if response.status_code == 404:
            raise PrometheusError(
                f"{self.config.base_url} answered, but has no Prometheus API at {path}. Check the port and path prefix.",
                "not_prometheus",
                404,
            )
        if 300 <= response.status_code < 400:
            raise PrometheusError(
                f"The server redirected to {response.headers.get('location', 'another address')}. "
                "Use the final address, including its protocol and path prefix.",
                "redirect",
                response.status_code,
            )
        try:
            body = response.json()
        except ValueError as error:
            raise PrometheusError(
                f"{self.config.base_url} did not return Prometheus data (HTTP {response.status_code}). "
                "Check that this address is a Prometheus server.",
                "not_prometheus",
                response.status_code,
            ) from error
        if body.get("status") != "success":
            raise PrometheusError(
                body.get("error") or f"Prometheus returned HTTP {response.status_code}.",
                body.get("errorType", "query"),
                response.status_code,
            )
        return body.get("data")

    async def build_info(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/status/buildinfo")

    async def targets(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/targets", params={"state": "active"})
        return data.get("activeTargets", [])

    async def alerts(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/alerts")
        return data.get("alerts", [])

    async def rules(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/v1/rules")
        return data.get("groups", [])

    async def metric_names(self) -> list[str]:
        return await self._request("GET", "/api/v1/label/__name__/values")

    async def query(self, promql: str, at: float | None = None) -> dict[str, Any]:
        form: dict[str, Any] = {"query": promql}
        if at is not None:
            form["time"] = at
        return await self._request("POST", "/api/v1/query", data=form)

    async def query_range(self, promql: str, start: float, end: float, step: float) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/query_range",
            data={"query": promql, "start": start, "end": end, "step": step},
        )

    async def test(self) -> dict[str, Any]:
        started = time.monotonic()
        info = await self.build_info()
        targets = await self.targets()
        up = sum(1 for target in targets if target.get("health") == "up")
        return {
            "ok": True,
            "version": info.get("version", "unknown"),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "targets_total": len(targets),
            "targets_up": up,
        }
