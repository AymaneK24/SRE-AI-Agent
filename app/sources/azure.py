"""Azure Monitor metrics, alerts, Log Analytics, Resource Health, and Activity Log. Read-only.

Query format: resource-id|metric|aggregation
Example: /subscriptions/x/resourceGroups/y/providers/Microsoft.Sql/servers/z/databases/d|cpu_percent|Average
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import Config
from ..context.signals import Signal
from .base import SourceError
from .cloud_auth import AzureClientAuth


def parse_query(expression: str) -> dict[str, str]:
    parts = [part.strip() for part in expression.split("|")]
    if len(parts) != 3 or not all(parts) or not parts[0].startswith("/"):
        raise SourceError("Write Azure Monitor queries as /resource/id|metric|Average.", "bad_data")
    return {"resource": parts[0], "metric": parts[1], "aggregation": parts[2]}


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _interval(step: float) -> str:
    for seconds, text in ((60, "PT1M"), (300, "PT5M"), (900, "PT15M"), (1800, "PT30M"), (3600, "PT1H"), (21600, "PT6H")):
        if step <= seconds:
            return text
    return "P1D"


class AzureMonitorSource:
    kind = "azure_monitor"
    query_language = "Azure Monitor (/resource/id|metric|aggregation)"
    logs_language = "KQL for Log Analytics"

    def __init__(self, row: dict[str, Any], config: dict[str, Any], secrets: dict[str, Any], app_config: Config) -> None:
        self.id = row.get("id") or 0
        self.name = row["name"]
        self.subscription = config.get("subscription_id", "")
        self.workspace = config.get("workspace_id", "")
        self.tracked: list[dict[str, Any]] = config.get("metrics", [])
        if not self.subscription:
            raise SourceError("Enter the Azure subscription ID.")
        tenant, client_id = config.get("tenant_id", ""), config.get("client_id", "")
        login = app_config.azure_login_url
        self.client = httpx.AsyncClient(
            base_url=app_config.azure_management_url, timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False,
            auth=AzureClientAuth(tenant, client_id, secrets.get("client_secret", ""), f"{app_config.azure_management_url.rstrip('/')}/.default", login),
        )
        self.logs_client = httpx.AsyncClient(
            base_url=app_config.azure_logs_url, timeout=httpx.Timeout(30, connect=5), follow_redirects=False, trust_env=False,
            auth=AzureClientAuth(tenant, client_id, secrets.get("client_secret", ""), f"{app_config.azure_logs_url.rstrip('/')}/.default", login),
        )

    def describe(self) -> str:
        return f"Azure subscription {self.subscription}"

    async def guard(self) -> None:
        return None

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            response = await self.client.get(path, params=params)
        except httpx.TransportError as error:
            raise SourceError(f"Could not reach Azure: {error}", "unreachable") from error
        return self._body(response)

    @staticmethod
    def _body(response: httpx.Response) -> Any:
        if response.status_code in {401, 403}:
            raise SourceError("Azure rejected this request. Check the credentials and the Monitoring Reader role.", "auth")
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", "")
            except ValueError:
                detail = ""
            raise SourceError(f"Azure returned HTTP {response.status_code}. {detail}".strip(), "query")
        return response.json()

    async def info(self) -> dict[str, Any]:
        return {"version": "Azure Monitor"}

    async def test(self) -> dict[str, Any]:
        started = time.monotonic()
        alerts = await self.alerts()
        return {"ok": True, "version": "Azure Monitor", "latency_ms": round((time.monotonic() - started) * 1000),
                "targets_total": 0, "targets_up": 0, "detail": f"Read {len(alerts)} active alerts."}

    async def targets(self) -> list[dict[str, Any]]:
        data = await self._get(f"/subscriptions/{self.subscription}/providers/Microsoft.ResourceHealth/availabilityStatuses",
                               {"api-version": "2020-05-01"})
        output = []
        for item in data.get("value", []):
            state = item.get("properties", {}).get("availabilityState", "Unknown")
            resource = item.get("id", "").split("/providers/Microsoft.ResourceHealth")[0]
            output.append({
                "labels": {"job": "azure-resource-health", "instance": resource.rsplit("/", 1)[-1] or resource},
                "health": {"Available": "up", "Unavailable": "down", "Degraded": "down"}.get(state, "unknown"),
                "lastError": item.get("properties", {}).get("summary", "") if state != "Available" else "",
                "scrapeUrl": resource,
            })
        return output

    async def alerts(self) -> list[dict[str, Any]]:
        data = await self._get(f"/subscriptions/{self.subscription}/providers/Microsoft.AlertsManagement/alerts",
                               {"api-version": "2019-03-01", "monitorCondition": "Fired", "pageCount": 100})
        output = []
        for item in data.get("value", []):
            essentials = item.get("properties", {}).get("essentials", {})
            severity = {"Sev0": "critical", "Sev1": "critical", "Sev2": "warning"}.get(essentials.get("severity", ""), "warning")
            output.append({
                "labels": {"alertname": item.get("name", "Azure alert"), "severity": severity,
                           "resource": essentials.get("targetResourceName", ""), "signal_type": essentials.get("signalType", "")},
                "annotations": {"summary": essentials.get("description") or essentials.get("alertRule", "")},
                "state": "firing", "activeAt": essentials.get("startDateTime"), "value": None,
            })
        return output

    async def rules(self) -> list[dict[str, Any]]:
        return []

    async def metric_names(self) -> list[str]:
        return sorted({item["query"] for item in self.tracked})

    async def signals(self, known_metrics: set[str]) -> list[Signal]:
        return [Signal(f"az_{index}", item.get("title", item["query"]), "Azure", item.get("unit", "short"), (), item["query"],
                       limit=item.get("limit"), direction="both", panel_priority=index + 1) for index, item in enumerate(self.tracked)]

    async def query_range(self, expression: str, start: float, end: float, step: float) -> dict[str, Any]:
        parsed = parse_query(expression)
        data = await self._get(f"{parsed['resource']}/providers/Microsoft.Insights/metrics", {
            "api-version": "2018-01-01", "metricnames": parsed["metric"], "aggregation": parsed["aggregation"],
            "timespan": f"{_iso(start)}/{_iso(end)}", "interval": _interval(step),
        })
        key = parsed["aggregation"].lower()
        output = []
        for metric in data.get("value", []):
            for series in metric.get("timeseries", []):
                values = []
                for point in series.get("data", []):
                    if point.get(key) is None:
                        continue
                    stamp = datetime.fromisoformat(point["timeStamp"].replace("Z", "+00:00")).timestamp()
                    values.append([start + round((stamp - start) / step) * step, str(point[key])])
                if values:
                    labels = {"resource": parsed["resource"].rsplit("/", 1)[-1], "metric": parsed["metric"]}
                    labels.update({item["name"]["value"]: item["value"] for item in series.get("metadatavalues", [])})
                    output.append({"metric": labels, "values": values})
        return {"resultType": "matrix", "result": output}

    async def query(self, expression: str) -> dict[str, Any]:
        end = time.time()
        data = await self.query_range(expression, end - 900, end, 60)
        return {"resultType": "vector", "result": [
            {"metric": series["metric"], "value": series["values"][-1]} for series in data["result"]]}

    async def logs(self, query: str, minutes: int) -> dict[str, Any]:
        if not self.workspace:
            raise SourceError("Add a Log Analytics workspace ID to query logs.", "bad_data")
        try:
            response = await self.logs_client.post(f"/v1/workspaces/{self.workspace}/query", json={"query": query, "timespan": f"PT{minutes}M"})
        except httpx.TransportError as error:
            raise SourceError(f"Could not reach Log Analytics: {error}", "unreachable") from error
        tables = self._body(response).get("tables", [])
        if not tables:
            return {"rows": []}
        columns = [column["name"] for column in tables[0].get("columns", [])]
        return {"rows": [dict(zip(columns, row, strict=False)) for row in tables[0].get("rows", [])[:50]]}

    async def change_events(self, since_iso: str) -> list[dict[str, Any]]:
        since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        since = max(since, datetime.now(timezone.utc) - timedelta(hours=6))
        try:
            data = await self._get(f"/subscriptions/{self.subscription}/providers/Microsoft.Insights/eventtypes/management/values", {
                "api-version": "2015-04-01",
                "$filter": f"eventTimestamp ge '{since.strftime('%Y-%m-%dT%H:%M:%SZ')}'",
                "$select": "eventTimestamp,operationName,resourceId,caller,status,level",
            })
        except SourceError:
            return []  # Activity Log access is optional.
        output = []
        for item in data.get("value", []):
            if item.get("status", {}).get("value") not in {"Succeeded", "Failed"}:
                continue
            output.append({
                "ts": item.get("eventTimestamp", ""), "origin": "Azure Activity Log", "service": (item.get("resourceId") or "").rsplit("/", 1)[-1],
                "title": f"{item.get('operationName', {}).get('localizedValue', 'Change')} {item.get('status', {}).get('value', '')} by {item.get('caller', 'unknown')}",
            })
        return output[:50]

    async def close(self) -> None:
        await self.client.aclose()
        await self.logs_client.aclose()
