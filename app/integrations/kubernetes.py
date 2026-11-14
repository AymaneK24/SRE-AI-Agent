"""Read-only Kubernetes API access.

Everything here uses GET on the kinds listed in docs/DEPLOY.md (pods, pod logs, events, deployments, replica sets,
stateful sets, daemon sets, nodes, services). Secrets and ConfigMap contents are never requested. A kind the token may not
list is reported in `forbidden` and the rest still works.
"""
from __future__ import annotations

import os
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..dependencies import env_hosts, summarize_service
from ..net_guard import TargetPolicy, check_url, guarded_transport
from ..sources.base import SourceError

IN_CLUSTER_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MAX_LOG_LINES = 200
MAX_LOG_LINE_CHARS = 500
LIST_LIMIT = 500


def in_cluster_settings() -> dict[str, str] | None:
    """The address, token, and CA that Kubernetes mounts into every pod, when the console runs inside a cluster."""
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    token_file, ca_file = IN_CLUSTER_DIR / "token", IN_CLUSTER_DIR / "ca.crt"
    if not host or not token_file.exists():
        return None
    address = f"[{host}]" if ":" in host else host
    return {"url": f"https://{address}:{port}", "token": token_file.read_text().strip(),
            "ca_cert": ca_file.read_text() if ca_file.exists() else ""}


def _tls(verify_tls: bool, ca_cert: str) -> Any:
    if not verify_tls:
        return False
    if ca_cert.strip():
        try:
            return ssl.create_default_context(cadata=ca_cert.strip())
        except (ssl.SSLError, ValueError) as error:
            raise SourceError("The CA certificate isn't a valid PEM certificate.", "bad_data") from error
    return True


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def owner_of(pod: dict[str, Any]) -> tuple[str, str]:
    """The workload a pod belongs to. A pod name changes on every restart; its workload doesn't."""
    meta = pod.get("metadata", {})
    owners = meta.get("ownerReferences") or []
    if not owners:
        return "Pod", meta.get("name", "")
    owner = owners[0]
    kind, name = owner.get("kind", ""), owner.get("name", "")
    template_hash = (meta.get("labels") or {}).get("pod-template-hash")
    if kind == "ReplicaSet" and template_hash and name.endswith(f"-{template_hash}"):
        return "Deployment", name[: -len(template_hash) - 1]
    return kind or "Pod", name


def summarize_pod(pod: dict[str, Any]) -> dict[str, Any]:
    meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
    owner_kind, owner_name = owner_of(pod)
    containers = []
    for item in status.get("containerStatuses", []) or []:
        state = item.get("state", {})
        last = (item.get("lastState") or {}).get("terminated") or {}
        waiting = state.get("waiting") or {}
        terminated = state.get("terminated") or {}
        containers.append({
            "name": item.get("name", ""), "ready": bool(item.get("ready")), "restarts": item.get("restartCount", 0),
            "image": item.get("image", ""),
            "state": "waiting" if waiting else "terminated" if terminated else "running" if "running" in state else "unknown",
            "reason": waiting.get("reason") or terminated.get("reason") or "",
            "message": (waiting.get("message") or terminated.get("message") or "")[:300],
            "last_reason": last.get("reason", ""), "last_exit_code": last.get("exitCode"),
            "last_finished": last.get("finishedAt", ""),
        })
    conditions = {item.get("type"): item for item in status.get("conditions", []) or []}
    scheduled = conditions.get("PodScheduled", {})
    ready = conditions.get("Ready", {})
    limits = {c.get("name", ""): (c.get("resources") or {}).get("limits", {}) for c in spec.get("containers", []) or []}
    return {
        "namespace": meta.get("namespace", ""), "name": meta.get("name", ""), "phase": status.get("phase", ""),
        "workload_kind": owner_kind, "workload": owner_name, "node": spec.get("nodeName", ""),
        "created": meta.get("creationTimestamp", ""), "containers": containers, "limits": limits,
        "unschedulable": scheduled.get("status") == "False", "schedule_message": (scheduled.get("message") or "")[:300],
        "ready": ready.get("status") == "True", "ready_since": ready.get("lastTransitionTime", ""),
        "restarts": sum(item["restarts"] for item in containers),
        "labels": dict(meta.get("labels") or {}),
        # Host names the containers are configured to reach, for root cause ranking. Values are never kept.
        "env_hosts": env_hosts(spec.get("containers", []) or []),
    }


def summarize_deployment(item: dict[str, Any]) -> dict[str, Any]:
    meta, spec, status = item.get("metadata", {}), item.get("spec", {}), item.get("status", {})
    conditions = {c.get("type"): c for c in status.get("conditions", []) or []}
    progressing = conditions.get("Progressing", {})
    return {
        "namespace": meta.get("namespace", ""), "name": meta.get("name", ""),
        "desired": spec.get("replicas", 1) if spec.get("replicas") is not None else 1,
        "ready": status.get("readyReplicas", 0) or 0, "updated": status.get("updatedReplicas", 0) or 0,
        "revision": (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision", ""),
        "images": [c.get("image", "") for c in spec.get("template", {}).get("spec", {}).get("containers", [])],
        "stuck": progressing.get("reason") == "ProgressDeadlineExceeded",
        "stuck_message": (progressing.get("message") or "")[:300],
        "selector": (spec.get("selector") or {}).get("matchLabels", {}),
    }


def summarize_node(item: dict[str, Any]) -> dict[str, Any]:
    conditions = {c.get("type"): c for c in item.get("status", {}).get("conditions", []) or []}
    pressure = [name for name in ("MemoryPressure", "DiskPressure", "PIDPressure") if conditions.get(name, {}).get("status") == "True"]
    ready = conditions.get("Ready", {})
    return {"name": item.get("metadata", {}).get("name", ""), "ready": ready.get("status") == "True",
            "ready_message": (ready.get("message") or "")[:300], "pressure": pressure,
            "unschedulable": bool(item.get("spec", {}).get("unschedulable"))}


def summarize_event(item: dict[str, Any]) -> dict[str, Any]:
    involved = item.get("involvedObject") or item.get("regarding") or {}
    return {
        "namespace": item.get("metadata", {}).get("namespace", ""),
        "object": f"{involved.get('kind', '')}/{involved.get('name', '')}",
        "kind": involved.get("kind", ""), "name": involved.get("name", ""),
        "reason": item.get("reason", ""), "message": (item.get("message") or item.get("note") or "")[:300],
        "count": item.get("count", 1) or 1,
        "last_seen": item.get("lastTimestamp") or item.get("eventTime") or (item.get("metadata") or {}).get("creationTimestamp") or "",
    }


class KubernetesClient:
    def __init__(self, config: dict[str, Any], secrets: dict[str, Any], allow_private: TargetPolicy | bool) -> None:
        settings = dict(config)
        token = secrets.get("token", "")
        if config.get("in_cluster"):
            mounted = in_cluster_settings()
            if not mounted:
                raise SourceError("The console isn't running inside a Kubernetes cluster, so in-cluster mode can't be used.", "bad_data")
            settings.update(url=mounted["url"], ca_cert=mounted["ca_cert"] or settings.get("ca_cert", ""))
            token = mounted["token"]
        self.url = str(settings.get("url", "")).rstrip("/")
        self.namespace = settings.get("namespace", "")
        self.allow_private = allow_private
        self.server_name = str(settings.get("tls_server_name", "")).strip()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        verify = _tls(bool(settings.get("verify_tls", True)), str(settings.get("ca_cert", "")))
        self.client = httpx.AsyncClient(base_url=self.url, headers=headers, timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
                                        trust_env=False, transport=guarded_transport(allow_private, verify))

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        await check_url(self.url, self.allow_private)
        extensions = {"sni_hostname": self.server_name} if self.server_name else None
        try:
            return await self.client.get(path, params=params or {}, extensions=extensions)
        except httpx.ConnectError as error:
            text = str(error)
            if "CERTIFICATE" in text.upper() or "SSL" in text.upper():
                raise SourceError(
                    f"The Kubernetes API's certificate isn't trusted ({text[:160]}). Paste the cluster's CA certificate, "
                    "and set the TLS server name if you connect through another address.", "tls") from error
            raise SourceError(f"Could not reach the Kubernetes API at {self.url}: {error}", "unreachable") from error
        except httpx.TransportError as error:
            raise SourceError(f"Could not reach the Kubernetes API at {self.url}: {error}", "unreachable") from error

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request(path, params)
        if response.status_code == 401:
            raise SourceError("The Kubernetes API rejected this token.", "auth")
        if response.status_code == 403:
            raise SourceError(f"This token may not read {path}. Give it get and list on that resource.", "forbidden")
        if response.status_code == 404:
            raise SourceError("Kubernetes didn't find that object.", "not_found")
        if response.status_code >= 400:
            raise SourceError(f"The Kubernetes API returned HTTP {response.status_code}.", "query")
        return response.json()

    def _ns(self, namespace: str | None, resource: str, group: str = "api/v1") -> str:
        scope = namespace or self.namespace
        return f"/{group}/namespaces/{scope}/{resource}" if scope else f"/{group}/{resource}"

    async def test(self) -> dict[str, Any]:
        version = await self._get("/version")
        await self._get(self._ns(None, "pods"), {"limit": 1})
        return {"ok": True, "detail": f"Connected to Kubernetes {version.get('gitVersion', '')}, and this token can list pods."}

    async def _list(self, resource: str, group: str = "api/v1", namespace: str | None = None, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        data = await self._get(self._ns(namespace, resource, group), {"limit": LIST_LIMIT, **(params or {})})
        return list(data.get("items", []))

    # Reads used by tools and the API ---------------------------------------------
    async def warning_events(self, namespace: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        rows = [summarize_event(item) for item in await self._list("events", namespace=namespace, params={"fieldSelector": "type=Warning"})]
        rows.sort(key=lambda row: str(row["last_seen"]), reverse=True)
        return rows[:limit]

    async def pods(self, namespace: str | None = None) -> list[dict[str, Any]]:
        return [summarize_pod(item) for item in await self._list("pods", namespace=namespace)]

    async def unhealthy_pods(self, namespace: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        rows = []
        for pod in await self.pods(namespace):
            reasons = sorted({c["reason"] for c in pod["containers"] if c["state"] == "waiting" and c["reason"]}
                             | {f"last exit: {c['last_reason']}" for c in pod["containers"] if c["last_reason"] not in {"", "Completed"}})
            if pod["phase"] not in {"Running", "Succeeded"} or reasons:
                rows.append({"namespace": pod["namespace"], "pod": pod["name"], "phase": pod["phase"], "restarts": pod["restarts"],
                             "reasons": reasons, "workload": f"{pod['workload_kind']}/{pod['workload']}"})
        rows.sort(key=lambda row: -row["restarts"])
        return rows[:limit]

    async def rollouts(self, namespace: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        rows = [summarize_deployment(item) for item in await self._list("deployments", "apis/apps/v1", namespace)]
        rows.sort(key=lambda row: row["ready"] - row["desired"])
        return [{**row, "deployment": row["name"]} for row in rows[:limit]]

    async def pod_logs(self, namespace: str, pod: str, container: str = "", previous: bool = False, lines: int = 50) -> dict[str, Any]:
        params: dict[str, Any] = {"tailLines": max(1, min(int(lines), MAX_LOG_LINES)), "timestamps": "true"}
        if container:
            params["container"] = container
        if previous:
            params["previous"] = "true"
        response = await self._request(f"/api/v1/namespaces/{namespace}/pods/{pod}/log", params)
        if response.status_code == 400 and previous:
            return {"lines": [], "note": "There is no previous container to read, so the pod hasn't restarted since it was created."}
        if response.status_code == 403:
            raise SourceError("This token may not read pod logs. Give it get on pods/log.", "forbidden")
        if response.status_code == 404:
            raise SourceError(f"Pod {namespace}/{pod} doesn't exist any more.", "not_found")
        if response.status_code >= 400:
            raise SourceError(f"The Kubernetes API returned HTTP {response.status_code} for the logs.", "query")
        lines_out = [line[:MAX_LOG_LINE_CHARS] for line in response.text.splitlines() if line.strip()]
        return {"lines": lines_out[-MAX_LOG_LINES:]}

    async def services(self, namespace: str | None = None, limit: int = 40) -> list[dict[str, Any]]:
        """Services and the pods their selector reaches. A Service that reaches no ready pod refuses connections."""
        items = await self._list("services", namespace=namespace)
        pods = await self.pods(namespace)
        rows = []
        for item in items[:limit]:
            meta, spec = item.get("metadata", {}), item.get("spec", {})
            selector = spec.get("selector") or {}
            matching = [pod for pod in pods if pod["namespace"] == meta.get("namespace") and selector
                        and all(pod["labels"].get(key) == value for key, value in selector.items())]
            rows.append({
                "namespace": meta.get("namespace", ""), "name": meta.get("name", ""), "type": spec.get("type", ""),
                "selector": selector, "ports": [f"{port.get('port')}/{port.get('protocol', 'TCP')}" for port in spec.get("ports", []) or []],
                "matching_pods": len(matching), "ready_pods": sum(1 for pod in matching if pod["ready"]),
                "note": "" if not selector else "No pod matches this selector, so connections to it are refused." if not matching
                else "No matching pod is ready." if not any(pod["ready"] for pod in matching) else "",
            })
        return rows

    async def object_events(self, namespace: str, name: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = [summarize_event(item) for item in await self._list("events", namespace=namespace, params={"fieldSelector": f"involvedObject.name={name}"})]
        rows.sort(key=lambda row: str(row["last_seen"]), reverse=True)
        return rows[:limit]

    # The periodic snapshot ---------------------------------------------------------
    async def snapshot(self) -> dict[str, Any]:
        """Everything needed to find problems, in one pass. Kinds the token can't list are reported, not fatal."""
        forbidden: list[str] = []

        async def optional(resource: str, group: str = "api/v1", cluster_wide: bool = False, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            try:
                if cluster_wide:
                    data = await self._get(f"/{group}/{resource}", {"limit": LIST_LIMIT, **(params or {})})
                    return list(data.get("items", []))
                return await self._list(resource, group, params=params)
            except SourceError as error:
                if error.kind in {"forbidden", "not_found"}:
                    forbidden.append(resource)
                    return []
                raise

        pods = [summarize_pod(item) for item in await self._list("pods")]
        deployments = [summarize_deployment(item) for item in await optional("deployments", "apis/apps/v1")]
        nodes = [summarize_node(item) for item in await optional("nodes", cluster_wide=True)] if not self.namespace else []
        events = [summarize_event(item) for item in await optional("events", params={"fieldSelector": "type=Warning"})]
        events.sort(key=lambda row: str(row["last_seen"]), reverse=True)
        services = [summarize_service(item) for item in await optional("services")]
        return {"pods": pods, "deployments": deployments, "nodes": nodes, "events": events[:100], "services": services, "forbidden": forbidden,
                "taken_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}

    async def summary(self) -> dict[str, Any]:
        """The compact view used by the live context. Kept for callers that only need the old fields."""
        snap = await self.snapshot()
        return summary_from_snapshot(snap)


def summary_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    unhealthy = []
    for pod in snap["pods"]:
        reasons = sorted({c["reason"] for c in pod["containers"] if c["state"] == "waiting" and c["reason"]}
                         | {f"last exit: {c['last_reason']}" for c in pod["containers"] if c["last_reason"] not in {"", "Completed"}})
        if pod["phase"] not in {"Running", "Succeeded"} or reasons:
            unhealthy.append({"namespace": pod["namespace"], "pod": pod["name"], "phase": pod["phase"], "restarts": pod["restarts"], "reasons": reasons})
    unhealthy.sort(key=lambda row: -row["restarts"])
    below = [{**row, "deployment": row["name"]} for row in snap["deployments"] if row["ready"] < row["desired"]][:10]
    pods, deployments, nodes = snap["pods"], snap["deployments"], snap["nodes"]
    counts = {
        "namespaces": len({pod["namespace"] for pod in pods} | {row["namespace"] for row in deployments}),
        "pods": len(pods), "pods_running": sum(1 for pod in pods if pod["phase"] == "Running" and pod["ready"]),
        "pods_unhealthy": len(unhealthy), "restarts": sum(pod["restarts"] for pod in pods),
        "deployments": len(deployments), "deployments_available": sum(1 for row in deployments if row["ready"] >= row["desired"]),
        "nodes": len(nodes), "nodes_ready": sum(1 for node in nodes if node["ready"]),
    }
    per_namespace: dict[str, dict[str, Any]] = {}
    for pod in pods:
        row = per_namespace.setdefault(pod["namespace"], {"name": pod["namespace"], "pods": 0, "ready": 0, "workloads": set()})
        row["pods"] += 1
        row["ready"] += int(pod["phase"] == "Running" and pod["ready"])
        row["workloads"].add(f"{pod['workload_kind']}/{pod['workload']}")
    for deployment in deployments:
        row = per_namespace.setdefault(deployment["namespace"], {"name": deployment["namespace"], "pods": 0, "ready": 0, "workloads": set()})
        row["workloads"].add(f"Deployment/{deployment['name']}")
    namespaces = [{**row, "workloads": len(row["workloads"])} for row in sorted(per_namespace.values(), key=lambda item: item["name"])]
    return {"unhealthy_pods": unhealthy[:10], "warning_events": snap["events"][:10], "deployments_below_desired": below,
            "nodes_not_ready": [node for node in nodes if not node["ready"] or node["pressure"]][:10],
            "forbidden": snap["forbidden"], "counts": counts, "namespaces": namespaces[:100]}
