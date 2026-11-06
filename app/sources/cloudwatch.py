"""Amazon CloudWatch metrics, alarms, logs, and CloudTrail changes. All calls are read-only.

Query format: namespace|metric|dimension=value,dimension=value|statistic
Example: AWS/EC2|CPUUtilization|InstanceId=i-0abc|Average
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Config
from ..context.signals import Signal
from .base import SourceError


def parse_query(expression: str) -> dict[str, Any]:
    parts = [part.strip() for part in expression.split("|")]
    if len(parts) != 4 or not all((parts[0], parts[1], parts[3])):
        raise SourceError("Write CloudWatch queries as namespace|metric|dimension=value|statistic.", "bad_data")
    dimensions = []
    for item in filter(None, (piece.strip() for piece in parts[2].split(","))):
        if "=" not in item:
            raise SourceError(f"The dimension '{item}' needs the form name=value.", "bad_data")
        name, value = item.split("=", 1)
        dimensions.append({"Name": name.strip(), "Value": value.strip()})
    return {"namespace": parts[0], "metric": parts[1], "dimensions": dimensions, "stat": parts[3]}


def _labels(parsed: dict[str, Any]) -> dict[str, str]:
    labels = {"namespace": parsed["namespace"], "metric": parsed["metric"]}
    labels.update({item["Name"]: item["Value"] for item in parsed["dimensions"]})
    return labels


class CloudWatchSource:
    kind = "cloudwatch"
    query_language = "CloudWatch (namespace|metric|dimension=value|statistic)"
    logs_language = "CloudWatch Logs Insights, written as log-group-name|query"

    def __init__(self, row: dict[str, Any], config: dict[str, Any], secrets: dict[str, Any], app_config: Config) -> None:
        import boto3
        from botocore.config import Config as BotoConfig

        self.id = row.get("id") or 0
        self.name = row["name"]
        self.region = config.get("region", "")
        if not self.region:
            raise SourceError("Enter the AWS region.")
        self.tracked: list[dict[str, Any]] = config.get("metrics", [])
        session = boto3.Session(
            aws_access_key_id=secrets.get("access_key") or None, aws_secret_access_key=secrets.get("secret_key") or None,
            aws_session_token=secrets.get("session_token") or None, region_name=self.region,
        )
        extra: dict[str, Any] = {"config": BotoConfig(retries={"max_attempts": 2}, connect_timeout=5, read_timeout=20)}
        if app_config.aws_endpoint_url:
            extra["endpoint_url"] = app_config.aws_endpoint_url
        self.cw = session.client("cloudwatch", **extra)
        self.logs_client = session.client("logs", **extra)
        self.trail = session.client("cloudtrail", **extra)

    def describe(self) -> str:
        return f"CloudWatch in {self.region}"

    async def guard(self) -> None:
        return None

    async def _run(self, function: Any, **kwargs: Any) -> Any:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return await asyncio.to_thread(function, **kwargs)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            message = error.response.get("Error", {}).get("Message", str(error))
            if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "InvalidClientTokenId", "SignatureDoesNotMatch", "ExpiredToken"}:
                raise SourceError(f"AWS rejected this request ({code}). Check the credentials and read-only permissions.", "auth") from error
            raise SourceError(f"AWS returned an error: {message}", "query") from error
        except BotoCoreError as error:
            raise SourceError(f"Could not reach AWS: {error}", "unreachable") from error

    async def info(self) -> dict[str, Any]:
        return {"version": f"CloudWatch {self.region}"}

    async def test(self) -> dict[str, Any]:
        started = time.monotonic()
        alarms = await self._run(self.cw.describe_alarms, MaxRecords=50)
        return {
            "ok": True, "version": f"CloudWatch {self.region}", "latency_ms": round((time.monotonic() - started) * 1000),
            "targets_total": len(alarms.get("MetricAlarms", [])), "targets_up": 0,
            "detail": f"Read {len(alarms.get('MetricAlarms', []))} alarms.",
        }

    async def targets(self) -> list[dict[str, Any]]:
        return []

    async def alerts(self) -> list[dict[str, Any]]:
        response = await self._run(self.cw.describe_alarms, StateValue="ALARM", MaxRecords=100)
        output = []
        for alarm in response.get("MetricAlarms", []) + response.get("CompositeAlarms", []):
            updated = alarm.get("StateUpdatedTimestamp")
            labels = {"alertname": alarm["AlarmName"], "severity": "warning", "namespace": alarm.get("Namespace", "")}
            labels.update({item["Name"]: item["Value"] for item in alarm.get("Dimensions", [])})
            output.append({
                "labels": labels,
                "annotations": {"summary": alarm.get("StateReason", ""), "description": alarm.get("AlarmDescription") or ""},
                "state": "firing", "activeAt": updated.isoformat() if updated else None, "value": alarm.get("Threshold"),
            })
        return output

    async def rules(self) -> list[dict[str, Any]]:
        response = await self._run(self.cw.describe_alarms, MaxRecords=100)
        rules = [{"type": "alerting", "name": alarm["AlarmName"],
                  "health": "err" if alarm.get("StateValue") == "INSUFFICIENT_DATA" else "ok",
                  "lastError": "This alarm has insufficient data." if alarm.get("StateValue") == "INSUFFICIENT_DATA" else ""}
                 for alarm in response.get("MetricAlarms", [])]
        return [{"name": "CloudWatch alarms", "rules": rules}] if rules else []

    async def metric_names(self) -> list[str]:
        response = await self._run(self.cw.list_metrics)
        names = {f"{item['Namespace']}|{item['MetricName']}" for item in response.get("Metrics", [])}
        return sorted(names)

    async def signals(self, known_metrics: set[str]) -> list[Signal]:
        output = []
        for index, item in enumerate(self.tracked):
            output.append(Signal(f"cw_{index}", item.get("title", item["query"]), "AWS", item.get("unit", "short"), (),
                                 item["query"], limit=item.get("limit"), direction="both", panel_priority=index + 1))
        return output

    async def query_range(self, expression: str, start: float, end: float, step: float) -> dict[str, Any]:
        parsed = parse_query(expression)
        period = max(60, int(step // 60) * 60 or 60)
        response = await self._run(
            self.cw.get_metric_data,
            MetricDataQueries=[{"Id": "m1", "MetricStat": {
                "Metric": {"Namespace": parsed["namespace"], "MetricName": parsed["metric"], "Dimensions": parsed["dimensions"]},
                "Period": period, "Stat": parsed["stat"]}, "ReturnData": True}],
            StartTime=datetime.fromtimestamp(start, timezone.utc), EndTime=datetime.fromtimestamp(end, timezone.utc),
            ScanBy="TimestampAscending",
        )
        result = response["MetricDataResults"][0]
        values = [[moment.timestamp(), str(value)] for moment, value in zip(result.get("Timestamps", []), result.get("Values", []), strict=False)]
        values.sort(key=lambda item: item[0])
        aligned = [[start + round((stamp - start) / step) * step, value] for stamp, value in values]
        return {"resultType": "matrix", "result": [{"metric": _labels(parsed), "values": aligned}] if aligned else []}

    async def query(self, expression: str) -> dict[str, Any]:
        # Ask slightly past now so the newest datapoint's period is included.
        end = time.time() + 60
        data = await self.query_range(expression, end - 960, end, 60)
        output = []
        for series in data["result"]:
            if series["values"]:
                output.append({"metric": series["metric"], "value": series["values"][-1]})
        return {"resultType": "vector", "result": output}

    async def logs(self, query: str, minutes: int) -> dict[str, Any]:
        if "|" not in query:
            raise SourceError("Write CloudWatch Logs queries as log-group-name|Logs Insights query.", "bad_data")
        group, statement = query.split("|", 1)
        end = int(time.time())
        started = await self._run(self.logs_client.start_query, logGroupName=group.strip(), startTime=end - minutes * 60,
                                  endTime=end, queryString=statement.strip(), limit=50)
        for _ in range(30):
            result = await self._run(self.logs_client.get_query_results, queryId=started["queryId"])
            if result.get("status") in {"Complete", "Failed", "Cancelled", "Timeout"}:
                rows = [{cell["field"]: cell["value"] for cell in row} for row in result.get("results", [])]
                return {"status": result.get("status"), "rows": rows[:50]}
            await asyncio.sleep(1)
        return {"status": "Timeout", "rows": []}

    async def change_events(self, since_iso: str) -> list[dict[str, Any]]:
        since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        since = max(since, datetime.now(timezone.utc) - timedelta(hours=6))
        try:
            response = await self._run(
                self.trail.lookup_events, StartTime=since, MaxResults=50,
                LookupAttributes=[{"AttributeKey": "ReadOnly", "AttributeValue": "false"}],
            )
        except SourceError:
            return []  # CloudTrail access is optional.
        return [{
            "ts": item["EventTime"].astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "origin": "AWS CloudTrail", "service": item.get("EventSource", ""),
            "title": f"{item.get('EventName', 'Change')} by {item.get('Username', 'unknown')}",
        } for item in response.get("Events", [])]

    async def close(self) -> None:
        return None
