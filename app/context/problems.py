"""What is wrong right now, what changed since the last poll, and which changes are announced."""
from __future__ import annotations

import time
from typing import Any

from ..db import utcnow
from ..secrets_store import scrub
from .state import ANOMALY_SUSTAIN_POLLS, BLIND_AFTER_POLLS, CONSOLE_FINGERPRINT, K8S_CLEAR_AFTER_S, TARGET_LIST_LIMIT, EngineState
from .summary import alert_workload, alert_severity, label_text, series_key


class ProblemTracking(EngineState):
    def still_active(self, fingerprint: str) -> bool:
        """Whether a problem announced a moment ago is still there, for the grace period."""
        if fingerprint in self._active:
            return True
        if fingerprint == CONSOLE_FINGERPRINT:
            return self.failed_polls_in_a_row >= BLIND_AFTER_POLLS
        if fingerprint.startswith("source:"):
            return any(state.failures > 0 for state in self.states.values() if f"source:{state.source.id}" == fingerprint)
        return False

    def _summarize_targets(self, targets: list[tuple[Any, dict[str, Any], bool]], now_iso: str) -> dict[str, Any]:
        up = unknown = 0
        down_list = []
        listed: list[dict[str, Any]] = []
        jobs: dict[str, dict[str, int]] = {}
        self._targets_up = set()
        self._targets_unknown = set()
        for source, target, multi in targets:
            labels = target.get("labels", {})
            job = labels.get("job") or target.get("scrapePool", "unknown")
            if multi:
                job = f"{source.name}/{job}"
            bucket = jobs.setdefault(job, {"up": 0, "down": 0, "unknown": 0})
            health = target.get("health", "unknown")
            instance = labels.get("instance") or target.get("scrapeUrl", "")
            fingerprint = f"target:{source.id}:{job}:{instance}"
            if len(listed) < TARGET_LIST_LIMIT:
                # What is watched, and by which source: the address a person recognises, not the collector's own name.
                listed.append({"instance": instance, "job": labels.get("job") or target.get("scrapePool", "unknown"), "health": health,
                               "source_id": source.id, "source": source.name, "last_scrape": target.get("lastScrape"),
                               "error": scrub(target.get("lastError", ""))})
            if health == "up":
                up += 1
                bucket["up"] += 1
                self._targets_up.add(fingerprint)
            elif health == "down":
                bucket["down"] += 1
                since = self._active.get(fingerprint, {}).get("since", now_iso)
                down_list.append({
                    "job": job, "instance": instance, "error": scrub(target.get("lastError", "")),
                    "last_scrape": target.get("lastScrape"), "since": since, "fingerprint": fingerprint, "source_id": source.id,
                })
            else:
                unknown += 1
                bucket["unknown"] += 1
                self._targets_unknown.add(fingerprint)
        return {
            "total": len(targets), "up": up, "down": len(down_list), "unknown": unknown, "down_list": down_list,
            "jobs": [{"job": job, **counts} for job, counts in sorted(jobs.items())],
            "list": sorted(listed, key=lambda row: ({"down": 0, "unknown": 1}.get(row["health"], 2), row["source"], row["job"], row["instance"])),
        }

    def _find_anomalies(self, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found = []
        seen = set()
        for signal in signals:
            for row in signal["series"]:
                if row["state"] == "normal":
                    continue
                fingerprint = f"signal:{signal['source_id']}:{signal['id']}:{series_key(row['labels'])}:{row['state']}"
                seen.add(fingerprint)
                streak = self._anomaly_streak.get(fingerprint, 0) + 1
                self._anomaly_streak[fingerprint] = streak
                if row["state"] == "unusual" and streak < ANOMALY_SUSTAIN_POLLS:
                    continue
                found.append({
                    "fingerprint": fingerprint, "signal_id": signal["id"], "source_id": signal["source_id"],
                    "title": signal["title"], "unit": signal["unit"], "query": signal["query"], "labels": row["labels"],
                    "value": row["value"], "baseline": row.get("baseline"), "z": row.get("z"), "limit": signal.get("limit"), "kind": row["state"],
                })
        for fingerprint in list(self._anomaly_streak):
            if fingerprint not in seen:
                del self._anomaly_streak[fingerprint]
        found.sort(key=lambda item: (item["kind"] != "over_limit", -abs(item.get("z") or 0)))
        return found

    def _current_problems(self, targets: dict[str, Any], alerts: dict[str, Any], rules: dict[str, Any], anomalies: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Everything wrong right now, from every connection, keyed by a fingerprint that stays the same while it lasts."""
        current: dict[str, dict[str, Any]] = {}
        workloads = self._k8s_problem_index()
        for alert in alerts["firing"]:
            # A Prometheus alert about a workload the console already reports, such as KubePodCrashLooping for a
            # crash loop it found itself, is the same problem. It joins that problem instead of becoming a second one.
            about = alert_workload(alert, self._pod_owners())
            merged_into = workloads.get(about) if about else None
            alert.pop("merged_into", None)
            if merged_into:
                alert["merged_into"] = merged_into
            current[alert["fingerprint"]] = {
                "kind": "alert", "severity": alert_severity(alert), "title": f"Alert firing: {alert['name']}",
                "clear": f"Alert resolved: {alert['name']}", "detail": alert, "merged_into": merged_into,
            }
        for target in targets["down_list"]:
            current[target["fingerprint"]] = {
                "kind": "target", "severity": "critical", "title": f"Target down: {target['instance']} ({target['job']})",
                "clear": f"Target back up: {target['instance']} ({target['job']})", "detail": target,
            }
        also_firing: dict[str, list[str]] = {}
        for alert in alerts["firing"]:
            if alert.get("merged_into"):
                also_firing.setdefault(alert["merged_into"], []).append(alert["name"])
        for problem in self.k8s_problems:
            detail = {key: value for key, value in problem.items() if key not in {"title", "clear"}}
            if problem.get("diagnosis"):
                detail["summary"] = problem["diagnosis"]["cause"]
            if problem["fingerprint"] in also_firing:
                detail["also_firing"] = sorted(set(also_firing[problem["fingerprint"]]))
            current[problem["fingerprint"]] = {
                "kind": "k8s", "severity": problem["severity"], "title": problem["title"], "clear": problem["clear"], "detail": detail,
            }
        for anomaly in anomalies:
            wording = "over its limit" if anomaly["kind"] == "over_limit" else "unusual for this server"
            where = label_text(anomaly["labels"])
            current[anomaly["fingerprint"]] = {
                "kind": "signal", "severity": "warning", "title": f"{anomaly['title']} is {wording}: {where}",
                "clear": f"{anomaly['title']} is back to normal: {where}", "detail": anomaly,
            }
        for rule in rules["failing"]:
            fingerprint = f"rule:{rule['group']}:{rule['name']}"
            current[fingerprint] = {
                "kind": "rule", "severity": "warning", "title": f"Rule failing: {rule['name']}",
                "clear": f"Rule evaluating again: {rule['name']}", "detail": rule,
            }
        return current

    async def _diff_and_record(self, current: dict[str, dict[str, Any]]) -> None:
        k8s_unknown = "kubernetes" in self.integration_kinds and not self._integration_ok("kubernetes")
        # Problems seen on the first poll started at an unknown earlier time, so don't date them as new.
        # The first pass only counts as done once every problem is recorded, so a failure part-way is finished
        # by the next poll under the same rules instead of announcing the rest as new.
        first_poll = not self._state_known
        # Problems still open from before a restart were announced when they started, so they aren't sent again.
        # Everything else found on the first poll is sent: a broken deployment is just as urgent when the
        # console only sees it at startup, or when its cluster was just connected.
        open_before: set[str] = set()
        if first_poll:
            latest = await self._latest_events()
            open_before = {row["fingerprint"] for row in latest if row["kind"].endswith("_started") and row["handled"]}
            await self._close_problems_that_ended_offline(current, latest)
        # One problem, one notification: a problem already announced under another name isn't announced again.
        announced_alerts = {key for key, value in current.items() if value.get("merged_into") and key in self._active and not self._active[key].get("silent")}
        covered = {current[key]["merged_into"] for key in announced_alerts}
        # Every new problem is marked active before any is announced, so the alert for one can already see
        # the others, such as a symptom whose likely cause started in the same poll.
        started = []
        for fingerprint, item in current.items():
            self._k8s_missing_since.pop(fingerprint, None)
            if fingerprint not in self._active:
                silent = bool(item.get("merged_into")) or fingerprint in covered
                self._active[fingerprint] = {"since": utcnow(), "clear": item["clear"], "kind": item["kind"], "source_id": item["detail"].get("source_id"), "silent": silent}
                started.append((fingerprint, item, silent))
        for fingerprint, item, silent in started:
            title = f"Already active when the console connected. {item['title']}" if first_poll else item["title"]
            # Log lines stay in the live context only. Events keep the diagnosis, which is short and computed.
            detail = {key: value for key, value in item["detail"].items() if key != "logs"}
            await self.record_event(f"{item['kind']}_started", item["severity"], fingerprint, title, detail, item["detail"].get("source_id"),
                                     notify=not silent and not (first_poll and fingerprint in open_before))
        self._state_known = True
        for fingerprint in [key for key in self._active if key not in current]:
            # Prometheus reports targets as unknown until it scrapes them, such as after a restart.
            # That isn't evidence of recovery, so the target stays marked as a known problem.
            if fingerprint in self._targets_unknown:
                continue
            # A cluster that didn't answer gives no evidence that its problems ended.
            if k8s_unknown and fingerprint.startswith("k8s:"):
                continue
            if fingerprint.startswith("k8s:"):
                missing_since = self._k8s_missing_since.setdefault(fingerprint, time.monotonic())
                if time.monotonic() - missing_since < K8S_CLEAR_AFTER_S:
                    continue
                self._k8s_missing_since.pop(fingerprint, None)
            item = self._active.pop(fingerprint)
            title = item["clear"]
            if item.get("kind") == "target" and fingerprint not in self._targets_up:
                title = title.replace("Target back up:", "Target no longer reported:")
            await self.record_event(f"{item['kind']}_cleared", "info", fingerprint, title, {}, item.get("source_id"), notify=not item.get("silent"))

    def _pod_owners(self) -> dict[tuple[str, str], str]:
        owners: dict[tuple[str, str], str] = {}
        for snapshot in self.k8s_snapshots.values():
            for pod in snapshot.get("pods", []):
                if pod.get("workload"):
                    owners[(pod["namespace"], pod["name"])] = pod["workload"]
        return owners

    def _k8s_problem_index(self) -> dict[tuple[str, str], str]:
        """The console's own Kubernetes problems by (namespace, workload), and nodes by ("node", name)."""
        index: dict[tuple[str, str], str] = {}
        for problem in self.k8s_problems:
            fingerprint = problem["fingerprint"]
            if fingerprint.startswith("k8s:node/") or "/node/" in fingerprint.split(":", 1)[-1][:12]:
                name = fingerprint.split("node/", 1)[1].split(":", 1)[0]
                index.setdefault(("node", name), fingerprint)
            elif problem.get("namespace") and problem.get("workload"):
                index.setdefault((problem["namespace"], problem["workload"]), fingerprint)
        return index

    async def _latest_events(self) -> list[dict[str, Any]]:
        """The last event of every problem this organization has had."""
        return await self.db.all(
            "SELECT fingerprint, kind, title, source_id, handled FROM events WHERE id IN (SELECT MAX(id) FROM events WHERE org_id = ? GROUP BY fingerprint)", (self.org_id,))

    async def _close_problems_that_ended_offline(self, current: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        """A problem left open at the last shutdown has ended if it isn't active now, so reports don't count it as running."""
        for row in rows:
            if not row["kind"].endswith("_started") or row["fingerprint"] in current:
                continue
            if row["fingerprint"] == CONSOLE_FINGERPRINT:
                # This poll succeeded, so the console is checking again. The channels that heard it couldn't are told.
                if not self._blind_announced:
                    await self.record_event("polling_cleared", "info", CONSOLE_FINGERPRINT, "The console completes its checks again", {})
                continue
            if row["fingerprint"].startswith("k8s:"):
                if not self._integration_ok("kubernetes"):
                    continue
            else:
                state = self.states.get(row["source_id"])
                if not state or not state.last:
                    continue  # Without an answer from its source, there's no evidence that it ended.
            name = row["title"].removeprefix("Already active when the console connected. ")
            await self.record_event(row["kind"].replace("_started", "_cleared"), "info", row["fingerprint"], f"Ended while the console was offline: {name}", {}, row["source_id"], notify=False)
