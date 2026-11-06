"""Read-only queries against Grafana Loki."""
from __future__ import annotations

import time
from typing import Any

import httpx

from ..net_guard import TargetPolicy, check_url, guarded_transport
from ..sources.base import SourceError


class LokiClient:
    def __init__(self, config: dict[str, Any], secrets: dict[str, Any], allow_private: TargetPolicy | bool) -> None:
        self.url = config.get("url", "").rstrip("/")
        self.selector = config.get("selector") or '{job=~".+"}'
        self.allow_private = allow_private
        headers = {"Authorization": f"Bearer {secrets['token']}"} if secrets.get("token") else {}
        if config.get("tenant"):
            headers["X-Scope-OrgID"] = config["tenant"]
        self.client = httpx.AsyncClient(base_url=self.url, headers=headers, timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False, transport=guarded_transport(allow_private))

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        await check_url(self.url, self.allow_private)
        try:
            response = await self.client.get(path, params=params)
        except httpx.TransportError as error:
            raise SourceError(f"Could not reach Loki at {self.url}: {error}", "unreachable") from error
        if response.status_code in {401, 403}:
            raise SourceError("Loki rejected the credentials.", "auth")
        if response.status_code >= 400:
            raise SourceError(f"Loki returned an error: {response.text[:300]}", "query")
        return response.json().get("data", {})

    async def test(self) -> dict[str, Any]:
        labels = await self._get("/loki/api/v1/labels", {})
        return {"ok": True, "detail": f"Loki answered, with {len(labels if isinstance(labels, list) else [])} labels."}

    async def query(self, logql: str, minutes: int = 30, limit: int = 40) -> dict[str, Any]:
        end = time.time()
        data = await self._get("/loki/api/v1/query_range", {
            "query": logql, "start": int((end - minutes * 60) * 1e9), "end": int(end * 1e9), "limit": limit, "direction": "backward",
        })
        lines = []
        for stream in data.get("result", []):
            labels = stream.get("stream") or stream.get("metric") or {}
            for value in stream.get("values", []):
                lines.append({"labels": labels, "ts_ns": value[0], "line": str(value[1])[:500]})
        lines.sort(key=lambda item: str(item["ts_ns"]), reverse=True)
        return {"lines": lines[:limit], "total": len(lines)}

    async def summary(self) -> dict[str, Any]:
        """Count recent error-like lines by job for the live context."""
        end = time.time()
        expression = f'sum by (job) (count_over_time({self.selector} |~ "(?i)(error|exception|fatal|panic)" [5m]))'
        data = await self._get("/loki/api/v1/query", {"query": expression, "time": int(end * 1e9)})
        rows = []
        for item in data.get("result", []):
            rows.append({"job": item.get("metric", {}).get("job", "unknown"), "errors_5m": float(item.get("value", [0, 0])[1])})
        rows.sort(key=lambda row: -row["errors_5m"])
        return {"selector": self.selector, "error_lines": rows[:10]}
