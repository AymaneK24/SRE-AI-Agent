"""A small Kubernetes API server for tests, following the documented list, log, and event formats.

Tests change `ClusterState` while the server runs, to make workloads fail and recover.
"""
from __future__ import annotations

import json
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "kube-token"


def iso(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def pod(namespace: str, name: str, deployment: str | None = None, *, waiting: str | None = None, last_reason: str | None = None,
        exit_code: int | None = None, restarts: int = 0, phase: str = "Running", ready: bool = True, image: str = "app:1",
        unschedulable: str | None = None, limits: dict | None = None, created_minutes_ago: float = 30) -> dict:
    labels, owners = {}, []
    if deployment:
        labels["pod-template-hash"] = "5b5568b987"
        owners = [{"kind": "ReplicaSet", "name": f"{deployment}-5b5568b987"}]
    state = {"waiting": {"reason": waiting, "message": f"{waiting} message"}} if waiting else {"running": {"startedAt": iso(1)}}
    last = {"terminated": {"reason": last_reason, "exitCode": exit_code, "finishedAt": iso(2)}} if last_reason else {}
    conditions = [{"type": "Ready", "status": "True" if ready else "False", "lastTransitionTime": iso(created_minutes_ago)}]
    if unschedulable:
        conditions.append({"type": "PodScheduled", "status": "False", "message": unschedulable})
    return {
        "metadata": {"namespace": namespace, "name": name, "labels": labels, "ownerReferences": owners, "creationTimestamp": iso(created_minutes_ago)},
        "spec": {"nodeName": "" if unschedulable else "node-1", "containers": [{"name": "app", "image": image, "resources": {"limits": limits or {}}}]},
        "status": {"phase": phase, "conditions": conditions,
                   "containerStatuses": [] if unschedulable else [{"name": "app", "ready": ready, "restartCount": restarts, "image": image, "state": state, "lastState": last}]},
    }


def deployment(namespace: str, name: str, desired: int = 1, ready: int = 1, image: str = "app:1", stuck: bool = False) -> dict:
    conditions = [{"type": "Progressing", "reason": "ProgressDeadlineExceeded" if stuck else "NewReplicaSetAvailable",
                   "message": "ReplicaSet has timed out progressing." if stuck else ""}]
    return {"metadata": {"namespace": namespace, "name": name, "annotations": {"deployment.kubernetes.io/revision": "3"}},
            "spec": {"replicas": desired, "selector": {"matchLabels": {"app": name}}, "template": {"spec": {"containers": [{"name": "app", "image": image}]}}},
            "status": {"readyReplicas": ready, "updatedReplicas": ready, "conditions": conditions}}


class ClusterState:
    def __init__(self) -> None:
        self.pods: list[dict] = []
        self.deployments: list[dict] = []
        self.nodes: list[dict] = [{"metadata": {"name": "node-1"}, "spec": {}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
        self.events: list[dict] = []
        self.logs: dict[tuple[str, str, bool], str] = {}
        self.forbid: set[str] = set()
        self.down = False
        self.log_requests: list[str] = []
        self.lock = threading.Lock()

    def crash(self, namespace: str = "shop", name: str = "payment", log: str = "payment starting\nERROR: connection refused postgres:5432") -> None:
        with self.lock:
            self.deployments = [deployment(namespace, name, 1, 0, image="busybox:1.36")]
            self.pods = [pod(namespace, f"{name}-5b5568b987-h5zzq", name, waiting="CrashLoopBackOff", last_reason="Error", exit_code=1, restarts=12, ready=False, image="busybox:1.36")]
            self.events = [{"metadata": {"namespace": namespace}, "involvedObject": {"kind": "Pod", "name": f"{name}-5b5568b987-h5zzq"},
                            "reason": "BackOff", "message": "Back-off restarting failed container app", "count": 40, "lastTimestamp": iso(1), "type": "Warning"}]
            self.logs[(namespace, f"{name}-5b5568b987-h5zzq", True)] = "\n".join(f"2026-11-07T00:17:4{index}Z {line}" for index, line in enumerate(log.splitlines()))
            self.logs[(namespace, f"{name}-5b5568b987-h5zzq", False)] = "2026-11-07T00:18:00Z payment starting"

    def heal(self, namespace: str = "shop", name: str = "payment") -> None:
        with self.lock:
            self.deployments = [deployment(namespace, name, 1, 1)]
            self.pods = [pod(namespace, f"{name}-5b5568b987-zzzzz", name)]
            self.events = []


class FakeKube(BaseHTTPRequestHandler):
    state = ClusterState()

    def log_message(self, *args):
        pass

    def _send(self, status: int, payload, text: bool = False) -> None:
        body = (payload if text else json.dumps(payload)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if text else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        state = self.state
        if state.down:
            self._send(503, {"message": "unavailable"})
            return
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"message": "Unauthorized"})
            return
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        resource = path.rstrip("/").split("/")[-1]
        if resource in state.forbid:
            self._send(403, {"message": "forbidden"})
            return
        with state.lock:
            if path == "/version":
                self._send(200, {"gitVersion": "v1.34.1"})
            elif path.endswith("/log"):
                parts = path.split("/")
                namespace, name = parts[4], parts[6]
                previous = query.get("previous", ["false"])[0] == "true"
                state.log_requests.append(f"{namespace}/{name}:{'previous' if previous else 'current'}")
                text = state.logs.get((namespace, name, previous))
                if text is None:
                    self._send(400 if previous else 404, {"message": "previous terminated container not found" if previous else "not found"})
                else:
                    tail = int(query.get("tailLines", ["50"])[0])
                    self._send(200, "\n".join(text.splitlines()[-tail:]), text=True)
            elif resource == "pods":
                namespace = path.split("/namespaces/")[1].split("/")[0] if "/namespaces/" in path else None
                self._send(200, {"items": [item for item in state.pods if namespace in (None, item["metadata"]["namespace"])]})
            elif resource == "deployments":
                self._send(200, {"items": state.deployments})
            elif resource == "nodes":
                self._send(200, {"items": state.nodes})
            elif resource == "events":
                selector = query.get("fieldSelector", [""])[0]
                items = state.events
                if selector.startswith("involvedObject.name="):
                    wanted = selector.split("=", 1)[1]
                    items = [item for item in items if item["involvedObject"]["name"] == wanted]
                self._send(200, {"items": items})
            else:
                self._send(404, {"message": "not found"})


def serve(tls: ssl.SSLContext | None = None) -> tuple[ThreadingHTTPServer, str]:
    FakeKube.state = ClusterState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeKube)
    if tls:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    scheme = "https" if tls else "http"
    return server, f"{scheme}://127.0.0.1:{server.server_address[1]}"
