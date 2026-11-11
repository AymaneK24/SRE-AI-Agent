"""Reads log stores and Kubernetes clusters, finds what is wrong in each cluster, and adds logs and a diagnosis."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..diagnosis.problems import find_problems
from ..diagnosis.rules import diagnose
from ..integrations.kubernetes import summary_from_snapshot
from ..net_guard import BlockedTarget
from ..secrets_store import scrub
from ..sources.base import SourceError
from .connections import build_integration, main_connections, pick_main
from .state import CRASH_LOG_CHARS, CRASH_LOG_LINES, CRASH_LOG_PROBLEMS, INTEGRATION_EVERY_S, INTEGRATIONS_AT_ONCE, EngineState, log


class IntegrationWatch(EngineState):
    async def _load_integration_kinds(self) -> None:
        rows = await self.db.all("SELECT kind FROM integrations WHERE org_id = ? AND enabled = 1", (self.org_id,))
        self.integration_kinds = {row["kind"] for row in rows}

    async def _refresh_integrations(self) -> None:
        if time.monotonic() - self._integrations_at < INTEGRATION_EVERY_S:
            return
        self._integrations_at = time.monotonic()
        rows = await self.db.all("SELECT * FROM integrations WHERE org_id = ? AND enabled = 1 ORDER BY id", (self.org_id,))
        self.integration_kinds = {row["kind"] for row in rows}
        self.main_connections = await main_connections(self.db, self.org_id)
        clusters = [row for row in rows if row["kind"] == "kubernetes"]
        main_cluster = pick_main(clusters, self.main_connections.get("kubernetes"))
        snapshots: dict[int, dict[str, Any]] = {}
        problems: list[dict[str, Any]] = []
        wanted_logs: dict[str, dict[str, Any]] = {}
        # Connections are read at the same time, a few at once, so one slow cluster doesn't delay the others' alerts.
        limiter = asyncio.Semaphore(INTEGRATIONS_AT_ONCE)

        async def read(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
            client = None
            base = {"id": row["id"], "kind": row["kind"], "name": row["name"]}
            snapshot, found = None, []
            async with limiter:
                try:
                    client = build_integration(row, self.box, self.config)
                    if row["kind"] == "kubernetes":
                        snapshot = await client.snapshot()
                        found = find_problems(snapshot)
                        # A second cluster can hold a namespace with the same name, so its problems need their own fingerprints.
                        if len(clusters) > 1:
                            for problem in found:
                                problem["cluster"], problem["integration_id"] = row["name"], row["id"]
                                problem["title"] = f"{problem['title']} ({row['name']})"
                                problem["clear"] = f"{problem['clear']} ({row['name']})"
                                if row["id"] != main_cluster:
                                    problem["fingerprint"] = problem["fingerprint"].replace("k8s:", f"k8s:#{row['id']}/", 1)
                        await self._enrich_k8s_problems(client, found, snapshot.get("events", []), wanted_logs)
                        item = {**base, "status": "ok", **summary_from_snapshot(snapshot), "problems": found}
                        for namespace in item["namespaces"]:
                            namespace["problems"] = sum(1 for problem in found if problem["namespace"] == namespace["name"])
                    else:
                        item = {**base, "status": "ok", **(await client.summary())}
                except (SourceError, BlockedTarget, ValueError) as error:
                    snapshot, found = None, []
                    item = {**base, "status": "error", "error": scrub(getattr(error, "message", str(error)))}
                except Exception as error:  # A bad answer from one connection must not stop the others.
                    log.exception("integration refresh failed for %s", row["kind"])
                    snapshot, found = None, []
                    item = {**base, "status": "error", "error": scrub(f"Refresh failed: {error}")}
                finally:
                    if client:
                        await client.close()
            return item, snapshot, found

        results = await asyncio.gather(*(read(row) for row in rows))
        items: list[dict[str, Any]] = []
        for row, (item, snapshot, found) in zip(rows, results, strict=True):
            items.append(item)
            if snapshot is not None:
                snapshots[row["id"]] = snapshot
                problems.extend(found)
        self._crash_logs = wanted_logs
        self.k8s_snapshots = snapshots
        self.k8s_snapshot = snapshots.get(main_cluster) if main_cluster else None
        self.k8s_problems = problems
        summary: dict[str, Any] = {}
        for kind in ("loki", "kubernetes"):
            of_kind = [item for item in items if item["kind"] == kind]
            main_id = pick_main(of_kind, self.main_connections.get(kind))
            for item in of_kind:
                item["main"] = item["id"] == main_id
                if item["main"]:
                    summary[kind] = item
        self.integration_list = items
        self.integration_summary = summary

    async def _enrich_k8s_problems(self, client: Any, problems: list[dict[str, Any]], events: list[dict[str, Any]], wanted: dict[str, dict[str, Any]]) -> None:
        """Add the crashed container's last lines and a rule-based diagnosis to the worst problems.

        Logs are read again only when a pod restarts, so a crash loop costs one log call per restart, not per poll.
        A failure here never hides the problems themselves.
        """
        read_here = 0
        for problem in problems:
            logs = None
            if problem["problem"] in {"crashloop", "oomkilled", "not_ready"} and problem["pods"] and read_here < CRASH_LOG_PROBLEMS:
                read_here += 1
                pod = problem["pods"][0]
                container = problem["facts"].get("container", "")
                previous = problem["problem"] != "not_ready"
                key = f"{problem.get('integration_id', '')}/{problem['namespace']}/{pod}/{container}/{problem['facts'].get('restarts')}/{previous}"
                logs = self._crash_logs.get(key)
                if logs is None:
                    try:
                        result = await client.pod_logs(problem["namespace"], pod, container, previous, CRASH_LOG_LINES)
                        lines = [scrub(line) for line in result.get("lines", [])]
                        while lines and sum(len(line) for line in lines) > CRASH_LOG_CHARS:
                            lines.pop(0)
                        logs = {"pod": pod, "which": "the crashed container" if previous else "the running container",
                                "lines": lines, "note": result.get("note", "")}
                    except SourceError as error:
                        logs = {"pod": pod, "lines": [], "note": f"Couldn't read the logs: {error.message}"}
                wanted[key] = logs
                problem["logs"] = logs
            try:
                problem["diagnosis"] = diagnose(problem, (logs or {}).get("lines"), events)
            except Exception:  # A rule that trips on odd data must not stop the poll.
                log.exception("diagnosis failed for %s", problem["fingerprint"])
