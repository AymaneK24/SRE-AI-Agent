"""Read-only Kubernetes API access: events, unhealthy pods, and rollout state."""
from __future__ import annotations

from typing import Any

import httpx

from ..net_guard import TargetPolicy, check_url, guarded_transport
from ..sources.base import SourceError


class KubernetesClient:
    def __init__(self, config: dict[str, Any], secrets: dict[str, Any], allow_private: TargetPolicy | bool) -> None:
        self.url = config.get("url", "").rstrip("/")
        self.namespace = config.get("namespace", "")
        self.allow_private = allow_private
        headers = {"Authorization": f"Bearer {secrets['token']}"} if secrets.get("token") else {}
        verify: Any = bool(config.get("verify_tls", True))
        self.client = httpx.AsyncClient(base_url=self.url, headers=headers, timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False, transport=guarded_transport(allow_private, verify))

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await check_url(self.url, self.allow_private)
        try:
            response = await self.client.get(path, params=params or {})
        except httpx.TransportError as error:
            raise SourceError(f"Could not reach the Kubernetes API at {self.url}: {error}", "unreachable") from error
        if response.status_code in {401, 403}:
            raise SourceError("The Kubernetes API rejected this token. It needs read access to pods, events, and deployments.", "auth")
        if response.status_code >= 400:
            raise SourceError(f"The Kubernetes API returned HTTP {response.status_code}.", "query")
        return response.json()

    def _ns(self, namespace: str | None, resource: str, group: str = "api/v1") -> str:
        scope = namespace or self.namespace
        return f"/{group}/namespaces/{scope}/{resource}" if scope else f"/{group}/{resource}"

    async def test(self) -> dict[str, Any]:
        version = await self._get("/version")
        return {"ok": True, "detail": f"Connected to Kubernetes {version.get('gitVersion', '')}."}

    async def warning_events(self, namespace: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        data = await self._get(self._ns(namespace, "events"), {"fieldSelector": "type=Warning", "limit": 200})
        rows = [{
            "namespace": item.get("metadata", {}).get("namespace", ""),
            "object": f"{item.get('involvedObject', {}).get('kind', '')}/{item.get('involvedObject', {}).get('name', '')}",
            "reason": item.get("reason", ""), "message": (item.get("message") or "")[:300],
            "count": item.get("count", 1), "last_seen": item.get("lastTimestamp") or item.get("eventTime") or "",
        } for item in data.get("items", [])]
        rows.sort(key=lambda row: str(row["last_seen"]), reverse=True)
        return rows[:limit]

    async def unhealthy_pods(self, namespace: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        data = await self._get(self._ns(namespace, "pods"), {"limit": 500})
        rows = []
        for pod in data.get("items", []):
            status = pod.get("status", {})
            reasons, restarts = [], 0
            for container in status.get("containerStatuses", []) or []:
                restarts += container.get("restartCount", 0)
                state = container.get("state", {})
                if "waiting" in state:
                    reasons.append(state["waiting"].get("reason", "Waiting"))
                last = container.get("lastState", {}).get("terminated")
                if last and last.get("reason") not in {None, "Completed"}:
                    reasons.append(f"last exit: {last.get('reason')}")
            phase = status.get("phase", "")
            if phase not in {"Running", "Succeeded"} or reasons:
                rows.append({"namespace": pod.get("metadata", {}).get("namespace", ""), "pod": pod.get("metadata", {}).get("name", ""),
                             "phase": phase, "restarts": restarts, "reasons": sorted(set(reasons))})
        rows.sort(key=lambda row: -row["restarts"])
        return rows[:limit]

    async def rollouts(self, namespace: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        data = await self._get(self._ns(namespace, "deployments", "apis/apps/v1"), {"limit": 500})
        rows = []
        for item in data.get("items", []):
            spec, status = item.get("spec", {}), item.get("status", {})
            desired, ready = spec.get("replicas", 0), status.get("readyReplicas", 0) or 0
            images = [c.get("image", "") for c in spec.get("template", {}).get("spec", {}).get("containers", [])]
            rows.append({"namespace": item.get("metadata", {}).get("namespace", ""), "deployment": item.get("metadata", {}).get("name", ""),
                         "desired": desired, "ready": ready, "revision": item.get("metadata", {}).get("annotations", {}).get("deployment.kubernetes.io/revision", ""),
                         "images": images})
        rows.sort(key=lambda row: row["ready"] - row["desired"])
        return rows[:limit]

    async def summary(self) -> dict[str, Any]:
        pods = await self.unhealthy_pods(limit=10)
        events = await self.warning_events(limit=10)
        deployments = [row for row in await self.rollouts(limit=30) if row["ready"] < row["desired"]][:10]
        return {"unhealthy_pods": pods, "warning_events": events, "deployments_below_desired": deployments}
