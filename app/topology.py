"""The service dependency map: who calls whom, and so which problem is the cause and which are its symptoms.

Two kinds of evidence, kept apart because they don't deserve the same trust:

- **Configured**, read from Kubernetes: a workload whose environment points at a Service (DATABASE_URL=postgres://
  postgres-primary:5432) depends on the workloads behind that Service. It says what a workload is set up to call,
  not what it calls. Only host names are kept; the values themselves, which can hold passwords, are never stored.
- **Observed**, read from metrics when they exist: the service graph that Tempo or the OpenTelemetry collector
  builds from traces, or Istio and Linkerd request metrics. These are real calls, with their rate and errors.

The map is rebuilt as the console polls, so it follows deployments without anyone drawing it.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any
from urllib.parse import urlsplit


# Kept here rather than imported: the Kubernetes reader imports this module, and the diagnosis imports that reader.
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
# Environment variables whose values are secrets: never read, even for a host name.
SECRET_NAME = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL|PRIVATE|CERT|AUTH|SALT|SIGNING", re.IGNORECASE)
# A plain value such as "redis" counts as a host only under a name that says it is one.
HOST_NAME = re.compile(r"HOST|ADDR|URL|URI|SERVER|ENDPOINT|BROKER|DSN|SERVICE|UPSTREAM|BACKEND|NODES?$|PEERS?", re.IGNORECASE)
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST = re.compile(rf"^{LABEL}(?:\.{LABEL})*$")
URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s,;\"']+", re.IGNORECASE)
IP = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
CLUSTER_SUFFIXES = (".svc.cluster.local", ".svc", ".cluster.local")
# The last label of an outside host: a country code, or a common generic domain.
PUBLIC_SUFFIX = re.compile(r"^(?:[a-z]{2}|com|net|org|io|dev|app|cloud|ai|co|tech|info|biz|xyz|online|site|services|systems)$")
MAX_HOSTS_PER_POD = 40
MAX_DEPTH = 4
MAX_NODES = 400

# Observed calls, newest first in preference: (name, query, client labels, server labels).
OBSERVED_QUERIES = [
    ("traces", "sum by (client, server) (rate(traces_service_graph_request_total[10m]))",
     "sum by (client, server) (rate(traces_service_graph_request_failed_total[10m]))", ("client",), ("server",)),
    ("istio", 'sum by (source_workload, source_workload_namespace, destination_workload, destination_workload_namespace) (rate(istio_requests_total{reporter="source"}[10m]))',
     'sum by (source_workload, source_workload_namespace, destination_workload, destination_workload_namespace) (rate(istio_requests_total{reporter="source",response_code=~"5.."}[10m]))',
     ("source_workload_namespace", "source_workload"), ("destination_workload_namespace", "destination_workload")),
    ("linkerd", 'sum by (namespace, deployment, dst_namespace, dst_deployment) (rate(response_total{direction="outbound"}[10m]))',
     'sum by (namespace, deployment, dst_namespace, dst_deployment) (rate(response_total{direction="outbound",classification="failure"}[10m]))',
     ("namespace", "deployment"), ("dst_namespace", "dst_deployment")),
]
SOURCE_LABELS = {"config": "Kubernetes configuration", "traces": "traces (service graph)", "istio": "Istio", "linkerd": "Linkerd"}


# Reading hosts out of a pod -------------------------------------------------------
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
                    host = urlsplit(url).hostname or ""
                except ValueError:
                    host = ""
                if _clean_host(host):
                    found.append(_clean_host(host))
            if urls:
                continue
            # host, host:port, or a list of them, such as Kafka brokers.
            parts = [part for part in re.split(r"[,\s;]+", value) if part]
            if not parts or len(parts) > 20:
                continue
            for part in parts:
                host = _clean_host(part)
                if host and ("." in host or ":" in part or HOST_NAME.search(name)):
                    found.append(host)
    unique = list(dict.fromkeys(found))
    return unique[:MAX_HOSTS_PER_POD]


def summarize_service(item: dict[str, Any]) -> dict[str, Any]:
    meta, spec = item.get("metadata", {}), item.get("spec", {})
    return {"namespace": meta.get("namespace", ""), "name": meta.get("name", ""), "type": spec.get("type", ""),
            "selector": dict(spec.get("selector") or {}), "external_name": spec.get("externalName", "")}


# Building the map -------------------------------------------------------------------
def _node_id(cluster_id: int, namespace: str, name: str) -> str:
    return f"{cluster_id}:{namespace}/{name}"


class Topology:
    """Nodes are workloads (and outside hosts or traced services that match none). An edge runs from caller to callee."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[str, str], dict[str, Any]] = {}
        self.sources: set[str] = set()
        # (cluster_id, namespace, workload) -> node id, and lookups by name for evidence that carries no namespace.
        self._by_workload: dict[tuple[int, str, str], str] = {}
        self._by_name: dict[str, set[str]] = {}
        self._services: dict[tuple[int, str, str], list[str]] = {}
        self.clusters: dict[int, str] = {}
        self.main_cluster: int | None = None

    # Nodes ---------------------------------------------------------------------
    def _add_node(self, node_id: str, **fields: Any) -> str:
        if node_id not in self.nodes:
            if len(self.nodes) >= MAX_NODES:
                return ""
            self.nodes[node_id] = {"id": node_id, **fields}
            self._by_name.setdefault(str(fields.get("name", "")).lower(), set()).add(node_id)
        return node_id

    def _add_edge(self, caller: str, callee: str, source: str, **fields: Any) -> None:
        if not caller or not callee or caller == callee:
            return
        edge = self.edges.setdefault((caller, callee), {"from": caller, "to": callee, "sources": [], "hosts": []})
        if source not in edge["sources"]:
            edge["sources"].append(source)
        for key, value in fields.items():
            if key == "host":
                if value not in edge["hosts"]:
                    edge["hosts"].append(value)
            else:
                edge[key] = value
        self.sources.add(source)

    # From Kubernetes -----------------------------------------------------------
    def add_cluster(self, cluster_id: int, cluster_name: str, snapshot: dict[str, Any], main: bool) -> None:
        self.clusters[cluster_id] = cluster_name
        if main:
            self.main_cluster = cluster_id
        pods = snapshot.get("pods", [])
        for deployment in snapshot.get("deployments", []):
            self._workload(cluster_id, cluster_name, deployment["namespace"], "Deployment", deployment["name"])
        for pod in pods:
            if pod.get("workload") and pod.get("workload_kind") not in {"Job", "Pod"}:
                self._workload(cluster_id, cluster_name, pod["namespace"], pod["workload_kind"], pod["workload"])
        # Service -> the workloads whose pods its selector reaches.
        for service in snapshot.get("services", []):
            selector = service.get("selector") or {}
            backing: list[str] = []
            if selector:
                for pod in pods:
                    if pod["namespace"] == service["namespace"] and all(pod.get("labels", {}).get(key) == value for key, value in selector.items()):
                        node = self._by_workload.get((cluster_id, pod["namespace"], pod.get("workload", "")))
                        if node and node not in backing:
                            backing.append(node)
            elif service.get("external_name"):
                backing.append(self._external(service["external_name"]))
            self._services[(cluster_id, service["namespace"], service["name"])] = [node for node in backing if node]
            for node in backing:
                if node in self.nodes:
                    services = self.nodes[node].setdefault("services", [])
                    if service["name"] not in services:
                        services.append(service["name"])
        # Configured calls: a workload's environment names a Service.
        for pod in pods:
            caller = self._by_workload.get((cluster_id, pod["namespace"], pod.get("workload", "")))
            if not caller:
                continue
            for host in pod.get("env_hosts", []):
                for callee in self._resolve_host(cluster_id, pod["namespace"], host):
                    self._add_edge(caller, callee, "config", host=host)

    def _workload(self, cluster_id: int, cluster_name: str, namespace: str, kind: str, name: str) -> str:
        key = (cluster_id, namespace, name)
        if key not in self._by_workload:
            node = self._add_node(_node_id(cluster_id, namespace, name), name=name, namespace=namespace, kind=kind,
                                  cluster_id=cluster_id, cluster=cluster_name)
            if node:
                self._by_workload[key] = node
        return self._by_workload.get(key, "")

    def _external(self, host: str) -> str:
        return self._add_node(f"ext:{host}", name=host, namespace="", kind="External", cluster_id=None, cluster="")

    def _resolve_host(self, cluster_id: int, namespace: str, host: str) -> list[str]:
        """A host name as Kubernetes DNS would resolve it from this namespace, or an outside host."""
        short = host
        for suffix in CLUSTER_SUFFIXES:
            if short.endswith(suffix):
                short = short[: -len(suffix)]
                break
        parts = short.split(".")
        candidates = []
        if len(parts) == 1:
            candidates.append((namespace, parts[0]))
        elif len(parts) == 2:
            candidates.append((parts[1], parts[0]))
        if len(parts) == 2:
            candidates.append((namespace, parts[1]))  # pod.service, the address of one pod behind a headless Service.
        for ns, name in candidates:
            backing = self._services.get((cluster_id, ns, name))
            if backing:
                return backing
        # Only a name under a public domain is an outside service. Anything else is a cluster name that matches no
        # Service the console can see, such as one in a namespace its token can't read, and isn't guessed at.
        # A two-letter last label could be a namespace such as "db", so a country domain needs three labels.
        if host.endswith(CLUSTER_SUFFIXES) or len(parts) < 2 or not PUBLIC_SUFFIX.match(parts[-1]) or (len(parts[-1]) == 2 and len(parts) < 3):
            return []
        return [self._external(host)]

    # From metrics --------------------------------------------------------------
    def add_observed(self, kind: str, calls: list[dict[str, Any]]) -> None:
        """Calls seen in traces or a service mesh: {client, server, client_ns, server_ns, rps, errors}."""
        for call in calls:
            caller = self._resolve_name(call["client"], call.get("client_ns", ""))
            callee = self._resolve_name(call["server"], call.get("server_ns", ""))
            if caller and callee:
                previous = self.edges.get((caller, callee), {})
                rps = float(previous.get("rps") or 0) + float(call.get("rps") or 0)
                errors = float(previous.get("errors_rps") or 0) + float(call.get("errors") or 0)
                self._add_edge(caller, callee, kind, rps=round(rps, 4), errors_rps=round(errors, 4),
                               error_ratio=round(errors / rps, 4) if rps > 0 else 0.0)

    def _resolve_name(self, name: str, namespace: str) -> str:
        """A traced service or mesh workload, matched to a workload when the match is unambiguous."""
        name = (name or "").strip()
        if not name or name in {"unknown", "user"}:
            return ""
        lowered = name.lower()
        clusters = [self.main_cluster] if self.main_cluster is not None else list(self.clusters)
        for cluster_id in clusters:
            if namespace:
                found = self._by_workload.get((cluster_id, namespace, name)) or next(iter(self._services.get((cluster_id, namespace, name), [])), "")
                if found:
                    return found
        named = self._by_name.get(lowered, set())
        matches = [node for node in named if self.nodes[node]["kind"] != "External"]
        if not matches and len(named) == 1:
            return next(iter(named))  # An outside host also seen in traces, such as api.stripe.com, stays one box.
        if not matches:
            via_service = {node for key, nodes in self._services.items() if key[2] == lowered for node in nodes}
            matches = sorted(via_service)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return ""  # The same name in several namespaces: no guess.
        return self._add_node(f"svc:{lowered}", name=name, namespace=namespace, kind="Service", cluster_id=None, cluster="")

    # Questions -----------------------------------------------------------------
    def node_for(self, namespace: str, workload: str, cluster_id: int | None = None) -> str:
        if cluster_id is None:
            cluster_id = self.main_cluster if self.main_cluster is not None else next(iter(self.clusters), None)
        if cluster_id is None:
            return ""
        return self._by_workload.get((cluster_id, namespace, workload), "")

    def dependencies(self, node: str) -> list[str]:
        return [edge["to"] for (caller, _), edge in self.edges.items() if caller == node]

    def dependents(self, node: str) -> list[str]:
        return [edge["from"] for (_, callee), edge in self.edges.items() if callee == node]

    def path(self, start: str, goal: str) -> list[str]:
        """The shortest chain of calls from start to goal, as node ids."""
        previous: dict[str, str | None] = {start: None}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if node == goal:
                chain = []
                current: str | None = node
                while current is not None:
                    chain.append(current)
                    current = previous[current]
                return chain[::-1]
            for nxt in self.dependencies(node):
                if nxt not in previous:
                    previous[nxt] = node
                    queue.append(nxt)
        return []

    def reachable(self, start: str, forward: bool = True, depth: int = MAX_DEPTH) -> dict[str, int]:
        """Nodes reachable from start, with their distance, along calls (forward) or against them."""
        seen = {start: 0}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if seen[node] >= depth:
                continue
            for nxt in (self.dependencies(node) if forward else self.dependents(node)):
                if nxt not in seen:
                    seen[nxt] = seen[node] + 1
                    queue.append(nxt)
        seen.pop(start)
        return seen

    def name(self, node: str) -> str:
        item = self.nodes.get(node, {})
        return f"{item['namespace']}/{item['name']}" if item.get("namespace") else str(item.get("name", node))


# Problems on the map ----------------------------------------------------------------
def problem_node(topology: Topology, item: dict[str, Any]) -> str:
    """The workload a problem is about, if the map has it."""
    detail = item.get("detail") or {}
    if item.get("kind") == "k8s":
        if not detail.get("namespace") or detail.get("workload_kind") == "Node":
            return ""
        return topology.node_for(detail["namespace"], detail.get("workload", ""), detail.get("integration_id"))
    labels = detail.get("labels") or {}
    namespace = labels.get("namespace") or labels.get("exported_namespace") or ""
    for key in ("deployment", "statefulset", "daemonset", "destination_workload", "workload"):
        if namespace and labels.get(key):
            found = topology.node_for(namespace, labels[key])
            if found:
                return found
    if namespace and labels.get("service"):
        cluster = topology.main_cluster if topology.main_cluster is not None else next(iter(topology.clusters), None)
        backing = topology._services.get((cluster, namespace, labels["service"]), []) if cluster is not None else []
        if len(backing) == 1:
            return backing[0]
    if namespace and labels.get("pod"):
        prefix = labels["pod"].rsplit("-", 2)[0]
        found = topology.node_for(namespace, prefix)
        if found:
            return found
    if labels.get("service_name"):
        return topology._resolve_name(labels["service_name"], namespace) if topology._by_name.get(labels["service_name"].lower()) else ""
    return ""


def link_problems(topology: Topology, current: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Marks each problem whose workload depends on another broken workload as a likely symptom of it.

    The likely cause is the nearest broken dependency that has no broken dependency of its own. A loop of
    broken workloads, where each depends on the others, has no clear cause, so none of them is marked.
    Returns {fingerprint: link}, where a link holds caused_by or affects, and the problem's node.
    """
    by_node: dict[str, list[str]] = {}
    nodes: dict[str, str] = {}
    for fingerprint, item in current.items():
        if item.get("merged_into"):
            continue
        node = problem_node(topology, item)
        if node:
            nodes[fingerprint] = node
            by_node.setdefault(node, []).append(fingerprint)
    if not by_node:
        return {}

    def worst(node: str) -> str:
        return max(by_node[node], key=lambda key: (SEVERITY_RANK.get(current[key]["severity"], 0), key))

    broken_below: dict[str, dict[str, int]] = {node: {other: distance for other, distance in topology.reachable(node).items() if other in by_node} for node in by_node}
    links: dict[str, dict[str, Any]] = {fingerprint: {"node": node} for fingerprint, node in nodes.items()}
    for node, below in broken_below.items():
        roots = [(distance, other) for other, distance in below.items() if not broken_below.get(other) and node not in broken_below.get(other, {})]
        if not roots:
            continue
        _, root = min(roots)
        cause = worst(root)
        chain = topology.path(node, root)
        for fingerprint in by_node[node]:
            links[fingerprint]["caused_by"] = {"fingerprint": cause, "title": current[cause]["title"], "node": root,
                                               "path": [topology.name(step) for step in chain]}
    for link in list(links.values()):
        cause = link.get("caused_by", {}).get("fingerprint")
        if cause and cause in links:
            links[cause].setdefault("affects", [])
            name = topology.name(link["node"])
            if name not in links[cause]["affects"]:
                links[cause]["affects"].append(name)
    for link in links.values():
        if "caused_by" not in link:
            # Workloads that call this one, broken or not: what this problem can reach.
            impact = topology.reachable(link["node"], forward=False)
            if impact:
                link["impact"] = sorted(topology.name(node) for node in impact)[:20]
    return links


def public_map(topology: Topology, current: dict[str, dict[str, Any]], links: dict[str, dict[str, Any]], built_at: str | None) -> dict[str, Any]:
    """The map as the page draws it: each node with its status and problems, each edge with its evidence."""
    problems_on: dict[str, list[dict[str, Any]]] = {}
    for fingerprint, link in links.items():
        item = current[fingerprint]
        problems_on.setdefault(link["node"], []).append({
            "fingerprint": fingerprint, "title": item["title"], "severity": item["severity"],
            "caused_by": link.get("caused_by"), "affects": link.get("affects", [])})
    nodes = []
    for node_id, node in topology.nodes.items():
        problems = sorted(problems_on.get(node_id, []), key=lambda row: -SEVERITY_RANK.get(row["severity"], 0))
        status = problems[0]["severity"] if problems else "ok"
        role = ""
        if problems:
            role = "symptom" if all(row["caused_by"] for row in problems) else "cause" if any(row["affects"] for row in problems) else "problem"
        nodes.append({**{key: value for key, value in node.items() if key != "cluster_id"}, "cluster_id": node.get("cluster_id"),
                      "status": status, "role": role, "problems": problems})
    edges = []
    for edge in topology.edges.values():
        edges.append({**edge, "observed": any(source != "config" for source in edge["sources"])})
    observed = sorted(source for source in topology.sources if source != "config")
    return {"nodes": nodes, "edges": edges, "built_at": built_at, "sources": sorted(topology.sources),
            "source_labels": SOURCE_LABELS, "observed": bool(observed), "clusters": [{"id": key, "name": value} for key, value in topology.clusters.items()]}


def parse_observed(kind: str, total: dict[str, Any], failed: dict[str, Any] | None, client_keys: tuple[str, ...], server_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Prometheus vectors from one of OBSERVED_QUERIES as a list of calls."""
    def key(metric: dict[str, str]) -> tuple[str, str, str, str]:
        client_ns = metric.get(client_keys[0], "") if len(client_keys) > 1 else ""
        server_ns = metric.get(server_keys[0], "") if len(server_keys) > 1 else ""
        return (client_ns, metric.get(client_keys[-1], ""), server_ns, metric.get(server_keys[-1], ""))

    def value(row: dict[str, Any]) -> float:
        try:
            return float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return 0.0

    errors = {key(row.get("metric", {})): value(row) for row in (failed or {}).get("result", [])}
    calls = []
    for row in total.get("result", []):
        client_ns, client, server_ns, server = key(row.get("metric", {}))
        rps = value(row)
        if client and server and rps > 0:
            calls.append({"client": client, "server": server, "client_ns": client_ns, "server_ns": server_ns,
                          "rps": rps, "errors": errors.get((client_ns, client, server_ns, server), 0.0)})
    return calls
