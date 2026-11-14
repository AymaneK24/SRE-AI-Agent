"""Which workload depends on which, read from Kubernetes, for root cause ranking (app/rootcause.py).

Two facts, both from the cluster's own configuration:

- A Service sends traffic to the workloads whose pods its selector matches.
- A workload whose environment names a Service (DATABASE_URL=postgres://postgres-primary:5432) depends on the
  workloads behind it. Only host names are kept; values that could hold passwords are never stored, and
  secret-looking variables are never read at all.

This says what a workload is set up to call, not what it calls, so it is evidence for a hypothesis, never proof.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

SECRET_NAME = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL|PRIVATE|CERT|AUTH|SALT|SIGNING", re.IGNORECASE)
HOST_NAME = re.compile(r"HOST|ADDR|URL|URI|SERVER|ENDPOINT|BROKER|DSN|SERVICE|UPSTREAM|BACKEND|NODES?$|PEERS?", re.IGNORECASE)
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST = re.compile(rf"^{LABEL}(?:\.{LABEL})*$")
URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s,;\"']+", re.IGNORECASE)
IP = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
CLUSTER_SUFFIXES = (".svc.cluster.local", ".svc", ".cluster.local")
MAX_HOSTS_PER_POD = 40

Workload = tuple[str, str]  # (namespace, name)


def _clean_host(raw: str) -> str:
    host = raw.strip().strip("[]").lower().rstrip(".")
    if "@" in host:  # user:password@host keeps only the host.
        host = host.rsplit("@", 1)[1]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host if HOST.match(host) and not IP.match(host) and len(host) <= 253 else ""


def env_hosts(containers: list[dict[str, Any]]) -> list[str]:
    """Host names a pod's containers are configured to reach. Secret-looking variables are skipped, and no value is kept."""
    found: list[str] = []
    for container in containers or []:
        for item in container.get("env", []) or []:
            name, value = str(item.get("name", "")), item.get("value")
            if not isinstance(value, str) or not value or SECRET_NAME.search(name) or len(value) > 2000:
                continue
            urls = URL.findall(value)
            for url in urls:
                try:
                    host = _clean_host(urlsplit(url).hostname or "")
                except ValueError:
                    host = ""
                if host:
                    found.append(host)
            if urls:
                continue
            parts = [part for part in re.split(r"[,\s;]+", value) if part]
            if not parts or len(parts) > 20:
                continue
            for part in parts:
                host = _clean_host(part)
                if host and ("." in host or ":" in part or HOST_NAME.search(name)):
                    found.append(host)
    return list(dict.fromkeys(found))[:MAX_HOSTS_PER_POD]


def summarize_service(item: dict[str, Any]) -> dict[str, Any]:
    meta, spec = item.get("metadata", {}), item.get("spec", {})
    return {"namespace": meta.get("namespace", ""), "name": meta.get("name", ""), "selector": dict(spec.get("selector") or {})}


def _short(host: str) -> tuple[str, str | None]:
    """A host as Kubernetes DNS sees it: (service, namespace or None)."""
    for suffix in CLUSTER_SUFFIXES:
        if host.endswith(suffix):
            host = host[: -len(suffix)]
            break
    parts = host.split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], None) if len(parts) == 1 else (host, None)


class Links:
    """Workloads, the Services in front of them, the nodes they run on, and who is configured to call whom."""

    def __init__(self, snapshots: list[dict[str, Any]]) -> None:
        self.services: dict[tuple[str, str], set[Workload]] = {}
        self.nodes: dict[Workload, set[str]] = {}
        self.workloads: set[Workload] = set()
        self.depends: dict[Workload, dict[Workload, str]] = {}
        self._pod_owner: dict[tuple[str, str], str] = {}
        for snapshot in snapshots:
            self._add(snapshot)

    def _add(self, snapshot: dict[str, Any]) -> None:
        pods = snapshot.get("pods", []) or []
        for pod in pods:
            if pod.get("workload"):
                key = (pod["namespace"], pod["workload"])
                self.workloads.add(key)
                self._pod_owner[(pod["namespace"], pod["name"])] = pod["workload"]
                if pod.get("node"):
                    self.nodes.setdefault(key, set()).add(pod["node"])
        for deployment in snapshot.get("deployments", []) or []:
            self.workloads.add((deployment["namespace"], deployment["name"]))
        for service in snapshot.get("services", []) or []:
            selector = service.get("selector") or {}
            backing = {(pod["namespace"], pod["workload"]) for pod in pods if selector and pod.get("workload") and pod["namespace"] == service["namespace"]
                       and all((pod.get("labels") or {}).get(key) == value for key, value in selector.items())}
            self.services.setdefault((service["namespace"], service["name"]), set()).update(backing)
        for pod in pods:
            if not pod.get("workload"):
                continue
            caller = (pod["namespace"], pod["workload"])
            for host in pod.get("env_hosts", []) or []:
                for callee in self.resolve(host, pod["namespace"]):
                    if callee != caller:
                        self.depends.setdefault(caller, {}).setdefault(callee, host)

    def resolve(self, host: str, namespace: str) -> set[Workload]:
        """The workloads a host name reaches from a namespace: through a Service of that name, or a workload of that name."""
        name, other = _short(host.lower())
        found: set[Workload] = set()
        for ns in ([other] if other else []) + [namespace]:
            found |= self.services.get((ns, name), set())
            if (ns, name) in self.workloads:
                found.add((ns, name))
        return found

    def owner(self, namespace: str, pod: str) -> str:
        return self._pod_owner.get((namespace, pod), "")

    def names(self, workload: Workload) -> set[str]:
        """Everything a log line or error might call this workload: its name and the Services in front of it."""
        names = {workload[1]}
        for (namespace, service), backing in self.services.items():
            if workload in backing:
                names |= {service, f"{service}.{namespace}"}
        return {name.lower() for name in names if len(name) >= 3}
