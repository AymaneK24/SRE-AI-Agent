"""Metrics the context engine tracks when the matching exporters are present."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    id: str
    title: str
    group: str
    unit: str
    requires: tuple[str, ...]
    query: str
    limit: float | None = None
    direction: str = "up"  # "up": only rises matter. "both": any large change matters.
    min_sigma: float = 0.0
    panel_priority: int = 99


_FS = 'fstype!~"tmpfs|overlay|squashfs|ramfs|devtmpfs|nsfs|fuse.*"'

SIGNALS: tuple[Signal, ...] = (
    Signal("node_cpu", "CPU use", "Hosts", "percent", ("node_cpu_seconds_total",),
           '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
           limit=90, min_sigma=5, panel_priority=1),
    Signal("node_memory", "Memory use", "Hosts", "percent",
           ("node_memory_MemAvailable_bytes", "node_memory_MemTotal_bytes"),
           "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
           limit=90, min_sigma=3, panel_priority=2),
    Signal("node_disk", "Disk space used", "Hosts", "percent",
           ("node_filesystem_avail_bytes", "node_filesystem_size_bytes"),
           f"100 * (1 - node_filesystem_avail_bytes{{{_FS}}} / node_filesystem_size_bytes{{{_FS}}})",
           limit=85, min_sigma=2, panel_priority=3),
    Signal("node_load", "Load per CPU", "Hosts", "short", ("node_load5", "node_cpu_seconds_total"),
           'node_load5 / on (instance) count by (instance) (node_cpu_seconds_total{mode="idle"})',
           limit=1.5, min_sigma=0.2, panel_priority=8),
    Signal("node_io", "Disk busy time", "Hosts", "percent", ("node_disk_io_time_seconds_total",),
           "100 * max by (instance) (rate(node_disk_io_time_seconds_total[5m]))",
           limit=90, min_sigma=5, panel_priority=9),
    Signal("node_net_errors", "Network errors", "Hosts", "per_second",
           ("node_network_receive_errs_total", "node_network_transmit_errs_total"),
           "sum by (instance) (rate(node_network_receive_errs_total[5m]) + rate(node_network_transmit_errs_total[5m]))",
           min_sigma=0.1),
    Signal("http_rate", "Request rate", "Services", "req_per_second", ("http_requests_total",),
           "sum by (job) (rate(http_requests_total[5m]))",
           direction="both", min_sigma=1, panel_priority=4),
    Signal("http_errors_code", "Failed requests", "Services", "percent", ("http_requests_total",),
           '100 * sum by (job) (rate(http_requests_total{code=~"5.."}[5m])) / sum by (job) (rate(http_requests_total[5m]))',
           limit=1, min_sigma=0.5, panel_priority=5),
    Signal("http_errors_status", "Failed requests", "Services", "percent", ("http_requests_total",),
           '100 * sum by (job) (rate(http_requests_total{status=~"5.."}[5m])) / sum by (job) (rate(http_requests_total[5m]))',
           limit=1, min_sigma=0.5, panel_priority=5),
    Signal("http_p95", "Response time, p95", "Services", "seconds", ("http_request_duration_seconds_bucket",),
           "histogram_quantile(0.95, sum by (job, le) (rate(http_request_duration_seconds_bucket[5m])))",
           min_sigma=0.05, panel_priority=6),
    Signal("http_p99", "Response time, p99", "Services", "seconds", ("http_request_duration_seconds_bucket",),
           "histogram_quantile(0.99, sum by (job, le) (rate(http_request_duration_seconds_bucket[5m])))", min_sigma=0.1),
    Signal("spring_rate", "Request rate", "Services", "req_per_second", ("http_server_requests_seconds_count",),
           "sum by (job) (rate(http_server_requests_seconds_count[5m]))", direction="both", min_sigma=1),
    Signal("spring_errors", "Failed requests", "Services", "percent", ("http_server_requests_seconds_count",),
           '100 * sum by (job) (rate(http_server_requests_seconds_count{status=~"5.."}[5m])) / sum by (job) (rate(http_server_requests_seconds_count[5m]))',
           limit=1, min_sigma=0.5),
    Signal("spring_p95", "Response time, p95", "Services", "seconds", ("http_server_requests_seconds_bucket",),
           "histogram_quantile(0.95, sum by (job, le) (rate(http_server_requests_seconds_bucket[5m])))", min_sigma=0.05),
    Signal("ingress_rate", "Ingress request rate", "Services", "req_per_second", ("nginx_ingress_controller_requests",),
           "sum by (ingress) (rate(nginx_ingress_controller_requests[5m]))", direction="both", min_sigma=1),
    Signal("ingress_errors", "Ingress failed requests", "Services", "percent", ("nginx_ingress_controller_requests",),
           '100 * sum by (ingress) (rate(nginx_ingress_controller_requests{status=~"5.."}[5m])) / sum by (ingress) (rate(nginx_ingress_controller_requests[5m]))',
           limit=1, min_sigma=0.5),
    Signal("ingress_p95", "Ingress response time, p95", "Services", "seconds", ("nginx_ingress_controller_request_duration_seconds_bucket",),
           "histogram_quantile(0.95, sum by (ingress, le) (rate(nginx_ingress_controller_request_duration_seconds_bucket[5m])))", min_sigma=0.05),
    Signal("jvm_heap", "JVM heap used", "Services", "percent", ("jvm_memory_used_bytes", "jvm_memory_max_bytes"),
           '100 * sum by (job, instance) (jvm_memory_used_bytes{area="heap"}) / sum by (job, instance) (jvm_memory_max_bytes{area="heap"})',
           limit=90, min_sigma=5),
    Signal("postgres_connections", "PostgreSQL connections", "Services", "short", ("pg_stat_activity_count",),
           "sum by (instance) (pg_stat_activity_count)", min_sigma=5),
    Signal("mysql_connections", "MySQL connections", "Services", "short", ("mysql_global_status_threads_connected",),
           "sum by (instance) (mysql_global_status_threads_connected)", min_sigma=5),
    Signal("redis_memory", "Redis memory", "Services", "bytes", ("redis_memory_used_bytes",),
           "sum by (instance) (redis_memory_used_bytes)", min_sigma=20_000_000),
    Signal("kafka_lag", "Kafka consumer lag", "Services", "short", ("kafka_consumergroup_lag",),
           "sum by (consumergroup, topic) (kafka_consumergroup_lag)", min_sigma=100),
    Signal("rabbitmq_queue", "RabbitMQ messages waiting", "Services", "short", ("rabbitmq_queue_messages",),
           "sum by (queue) (rabbitmq_queue_messages)", min_sigma=100),
    Signal("process_cpu", "Process CPU", "Services", "short", ("process_cpu_seconds_total",),
           "sum by (job, instance) (rate(process_cpu_seconds_total[5m]))", min_sigma=0.1, panel_priority=10),
    Signal("process_memory", "Process memory", "Services", "bytes", ("process_resident_memory_bytes",),
           "process_resident_memory_bytes", min_sigma=20_000_000, panel_priority=11),
    Signal("kube_restarts", "Container restarts, 15 min", "Kubernetes", "short",
           ("kube_pod_container_status_restarts_total",),
           "sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[15m])) > 0",
           limit=1, panel_priority=4),
    Signal("kube_not_ready", "Pods not ready", "Kubernetes", "short", ("kube_pod_status_ready",),
           'sum by (namespace) (kube_pod_status_ready{condition="false"}) > 0', limit=1),
    Signal("kube_oom", "Containers killed for memory", "Kubernetes", "short",
           ("kube_pod_container_status_last_terminated_reason",),
           'sum by (namespace, pod) (kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}) > 0', limit=1),
    Signal("container_cpu", "Container CPU", "Kubernetes", "short", ("container_cpu_usage_seconds_total",),
           'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{container!=""}[5m]))',
           min_sigma=0.1, panel_priority=7),
    Signal("container_memory", "Container memory", "Kubernetes", "bytes", ("container_memory_working_set_bytes",),
           'sum by (namespace, pod) (container_memory_working_set_bytes{container!=""})', min_sigma=20_000_000),
    Signal("container_throttled", "Container CPU throttling", "Kubernetes", "percent",
           ("container_cpu_cfs_throttled_periods_total", "container_cpu_cfs_periods_total"),
           '100 * sum by (namespace, pod) (rate(container_cpu_cfs_throttled_periods_total{container!=""}[5m])) / sum by (namespace, pod) (rate(container_cpu_cfs_periods_total{container!=""}[5m]))',
           limit=50, min_sigma=5),
    Signal("hpa_at_max", "Autoscalers at their maximum", "Kubernetes", "short",
           ("kube_horizontalpodautoscaler_status_current_replicas", "kube_horizontalpodautoscaler_spec_max_replicas"),
           "count(kube_horizontalpodautoscaler_status_current_replicas >= on (namespace, horizontalpodautoscaler) kube_horizontalpodautoscaler_spec_max_replicas) > 0", limit=1),
    Signal("prom_scrape_duration", "Scrape duration", "Prometheus", "seconds", ("scrape_duration_seconds",),
           "scrape_duration_seconds", min_sigma=0.1, panel_priority=12),
    Signal("prom_rule_failures", "Rule evaluation failures", "Prometheus", "per_second",
           ("prometheus_rule_evaluation_failures_total",),
           "sum(rate(prometheus_rule_evaluation_failures_total[5m])) > 0", limit=0.001),
    Signal("prom_head_series", "Active series", "Prometheus", "short", ("prometheus_tsdb_head_series",),
           "prometheus_tsdb_head_series", direction="both", min_sigma=100, panel_priority=13),
    Signal("prom_dropped_notifications", "Dropped alert notifications", "Prometheus", "per_second",
           ("prometheus_notifications_dropped_total",),
           "sum(rate(prometheus_notifications_dropped_total[5m])) > 0", limit=0.001),
)


# The four questions an SRE asks first. A signal without an entry here still counts as evidence for the model.
GOLDEN: dict[str, str] = {
    "http_p95": "latency", "http_p99": "latency", "spring_p95": "latency", "ingress_p95": "latency",
    "http_rate": "traffic", "spring_rate": "traffic", "ingress_rate": "traffic",
    "http_errors_code": "errors", "http_errors_status": "errors", "spring_errors": "errors", "ingress_errors": "errors",
    "node_cpu": "saturation", "node_memory": "saturation", "node_disk": "saturation", "container_throttled": "saturation", "jvm_heap": "saturation",
}


def available_signals(metric_names: set[str]) -> list[Signal]:
    """Return the signals whose required metrics exist on this server."""
    return [signal for signal in SIGNALS if all(name in metric_names for name in signal.requires)]


COVERAGE_CHECKS: tuple[tuple[str, str], ...] = (
    ("node_cpu_seconds_total", "host metrics from node_exporter"),
    ("http_requests_total", "HTTP request metrics"),
    ("http_request_duration_seconds_bucket", "HTTP latency histograms"),
    ("kube_pod_status_ready", "Kubernetes state metrics"),
    ("container_cpu_usage_seconds_total", "container metrics from cAdvisor"),
)
