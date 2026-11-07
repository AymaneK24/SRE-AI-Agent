"""Read-only queries against Grafana Loki."""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ..net_guard import TargetPolicy, check_url, guarded_transport
from ..sources.base import SourceError


# A matcher Loki accepts on its own: = or =~ with a value that can't be empty.
_POSITIVE = re.compile(r'[A-Za-z_]\w*\s*(=|=~)\s*"(?!(\.\*)?")[^"]*"')


def usable_selector(selector: str) -> str:
    """Loki refuses a selector made only of != or !~ matchers, so add one that matches every stream."""
    selector = selector.strip()
    if not selector:
        return '{job=~".+"}'
    if selector.startswith("{") and not _POSITIVE.search(selector.split("}", 1)[0]):
        inner = selector[1:].lstrip()
        return '{job=~".+"' + (", " + inner if not inner.startswith("}") else inner)
    return selector


class LokiClient:
    def __init__(self, config: dict[str, Any], secrets: dict[str, Any], allow_private: TargetPolicy | bool) -> None:
        self.url = config.get("url", "").rstrip("/")
        self.selector = usable_selector(config.get("selector") or "")
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
        # Loki logs every query it runs, including this one, and that log line contains the words searched for.
        # Leaving those lines out keeps the console from counting its own queries as errors in Loki's log.
        expression = f'sum by (job) (count_over_time({self.selector} |~ "(?i)(error|exception|fatal|panic)" != "error|exception|fatal|panic" [5m]))'
        data = await self._get("/loki/api/v1/query", {"query": expression, "time": int(end * 1e9)})
        rows = []
        for item in data.get("result", []):
            rows.append({"job": item.get("metric", {}).get("job", "unknown"), "errors_5m": float(item.get("value", [0, 0])[1])})
        rows.sort(key=lambda row: -row["errors_5m"])
        # How much each stream writes, so a stream that went silent shows up too.
        volume = await self._get("/loki/api/v1/query", {"query": f"sum by (job) (count_over_time({self.selector} [5m]))", "time": int(end * 1e9)})
        errors = {row["job"]: row["errors_5m"] for row in rows}
        streams = [{"job": item.get("metric", {}).get("job", "unknown"), "lines_5m": float(item.get("value", [0, 0])[1])} for item in volume.get("result", [])]
        streams = [{**row, "errors_5m": errors.get(row["job"], 0.0)} for row in streams]
        streams.sort(key=lambda row: (-row["errors_5m"], -row["lines_5m"]))
        return {"selector": self.selector, "error_lines": rows[:10], "streams": streams[:100]}
