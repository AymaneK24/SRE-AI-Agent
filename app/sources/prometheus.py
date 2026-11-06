"""Prometheus and managed Prometheus services."""
from __future__ import annotations

from typing import Any

import httpx

from ..config import Config
from ..context.signals import Signal, available_signals
from ..net_guard import check_host
from ..prom import PrometheusClient, PrometheusError, ServerConfig
from .base import SourceError
from .cloud_auth import AwsSigV4Auth, AzureClientAuth, GoogleServiceAccountAuth


def _wrap(error: PrometheusError) -> SourceError:
    return SourceError(error.message, error.kind)


class PrometheusSource:
    kind = "prometheus"
    query_language = "PromQL"
    logs_language = ""

    def __init__(self, row: dict[str, Any], config: dict[str, Any], secrets: dict[str, Any], app_config: Config) -> None:
        self.id = row.get("id") or 0
        self.name = row["name"]
        self.app_config = app_config
        self.settings = config
        auth_type = config.get("auth_type", "none")
        auth: httpx.Auth | None = None
        if auth_type == "aws_sigv4":
            auth = AwsSigV4Auth(config.get("region", ""), secrets.get("access_key", ""), secrets.get("secret_key", ""), secrets.get("session_token", ""))
        elif auth_type == "azure_entra":
            auth = AzureClientAuth(config.get("tenant_id", ""), config.get("client_id", ""), secrets.get("client_secret", ""),
                                   "https://prometheus.monitor.azure.com/.default", app_config.azure_login_url)
        elif auth_type == "google":
            if not secrets.get("service_account_json"):
                raise SourceError("Add the Google service account JSON.")
            auth = GoogleServiceAccountAuth(secrets["service_account_json"])
        self.server = ServerConfig(
            id=self.id, name=self.name, scheme=config.get("scheme", "http"), host=config["host"], port=int(config["port"]),
            path_prefix=config.get("path_prefix", ""), auth_type=auth_type if auth_type in {"basic", "bearer"} else "none",
            username=config.get("username", ""), secret=secrets.get("secret", ""), verify_tls=bool(config.get("verify_tls", True)),
            poll_seconds=int(row.get("poll_seconds") or 15),
        )
        self.client = PrometheusClient(self.server, app_config.in_container, auth=auth, policy=app_config.target_policy)
        self.extra_metrics: list[Signal] = []

    def describe(self) -> str:
        return self.server.base_url

    async def guard(self) -> None:
        await check_host(self.server.host, self.app_config.target_policy)

    async def _call(self, coroutine: Any) -> Any:
        try:
            return await coroutine
        except PrometheusError as error:
            raise _wrap(error) from error

    async def info(self) -> dict[str, Any]:
        return await self._call(self.client.build_info())

    async def test(self) -> dict[str, Any]:
        await self.guard()
        return await self._call(self.client.test())

    async def targets(self) -> list[dict[str, Any]]:
        return await self._call(self.client.targets())

    async def alerts(self) -> list[dict[str, Any]]:
        return await self._call(self.client.alerts())

    async def rules(self) -> list[dict[str, Any]]:
        return await self._call(self.client.rules())

    async def metric_names(self) -> list[str]:
        return await self._call(self.client.metric_names())

    async def signals(self, known_metrics: set[str]) -> list[Signal]:
        return available_signals(known_metrics)

    async def query(self, expression: str) -> dict[str, Any]:
        return await self._call(self.client.query(expression))

    async def query_range(self, expression: str, start: float, end: float, step: float) -> dict[str, Any]:
        return await self._call(self.client.query_range(expression, start, end, step))

    async def logs(self, query: str, minutes: int) -> dict[str, Any]:
        raise SourceError("Prometheus doesn't store logs. Add a Loki connection in Connections.")

    async def change_events(self, since_iso: str) -> list[dict[str, Any]]:
        return []

    async def close(self) -> None:
        await self.client.close()
