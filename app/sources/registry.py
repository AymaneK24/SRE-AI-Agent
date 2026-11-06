"""Builds a source from its stored settings."""
from __future__ import annotations

import json
from typing import Any

from ..config import Config
from ..secrets_store import SecretBox
from .azure import AzureMonitorSource
from .cloudwatch import CloudWatchSource
from .prometheus import PrometheusSource

KINDS = {"prometheus": PrometheusSource, "cloudwatch": CloudWatchSource, "azure_monitor": AzureMonitorSource}

COLLECTORS = [
    {"id": "prometheus", "name": "Prometheus", "implemented": True, "detail": "Also Amazon, Azure, and Google managed Prometheus"},
    {"id": "cloudwatch", "name": "Amazon CloudWatch", "implemented": True, "detail": "Metrics, alarms, logs, CloudTrail"},
    {"id": "azure_monitor", "name": "Azure Monitor", "implemented": True, "detail": "Metrics, alerts, logs, Activity Log"},
    {"id": "google_cloud", "name": "Google Cloud Monitoring", "implemented": False, "detail": "Not implemented yet"},
    {"id": "datadog", "name": "Datadog", "implemented": False, "detail": "Not implemented yet"},
    {"id": "opentelemetry", "name": "OpenTelemetry", "implemented": False, "detail": "Not implemented yet"},
]


def build_source(row: dict[str, Any], box: SecretBox, config: Config, *, plain_secrets: dict[str, Any] | None = None) -> Any:
    settings = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
    secrets = plain_secrets if plain_secrets is not None else box.decrypt_json(row.get("secret_enc", ""))
    return KINDS[row["kind"]](row, settings, secrets, config)
