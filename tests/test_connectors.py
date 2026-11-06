"""Connectors, tested against the AWS SDK with moto and against servers using each API's documented format."""
import json
import threading
import time
from datetime import datetime, timezone
from email import message_from_string
from email import policy as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from app.integrations.kubernetes import KubernetesClient
from app.integrations.loki import LokiClient
from app.net_guard import TargetPolicy
from app.sources.azure import AzureMonitorSource
from app.sources.azure import parse_query as azure_parse
from app.sources.base import SourceError
from app.sources.cloud_auth import AwsSigV4Auth, AzureClientAuth
from app.sources.cloudwatch import CloudWatchSource
from app.sources.cloudwatch import parse_query as cw_parse
from app.sources.prometheus import PrometheusSource

LOCAL = TargetPolicy(allow_loopback=True)


@pytest.fixture(scope="module")
def moto_url():
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


def test_cloudwatch_query_parsing():
    assert cw_parse("AWS/EC2|CPUUtilization|InstanceId=i-1|Average")["dimensions"] == [{"Name": "InstanceId", "Value": "i-1"}]
    with pytest.raises(SourceError):
        cw_parse("just a metric")


async def test_cloudwatch_reads_metrics_and_alarms(moto_url, make_config, monkeypatch):
    import boto3

    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    creds = {"access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": "secret"}
    writer = boto3.client("cloudwatch", region_name="eu-west-1", endpoint_url=moto_url, aws_access_key_id=creds["access_key"], aws_secret_access_key=creds["secret_key"])
    writer.put_metric_data(Namespace="AWS/EC2", MetricData=[{"MetricName": "CPUUtilization", "Dimensions": [{"Name": "InstanceId", "Value": "i-1"}], "Value": 87.5, "Timestamp": datetime.now(timezone.utc), "Unit": "Percent"}])
    writer.put_metric_alarm(AlarmName="HighCPU", ComparisonOperator="GreaterThanThreshold", EvaluationPeriods=1, MetricName="CPUUtilization", Namespace="AWS/EC2", Period=60, Statistic="Average", Threshold=80.0, AlarmDescription="CPU is high")
    writer.set_alarm_state(AlarmName="HighCPU", StateValue="ALARM", StateReason="Threshold crossed")
    config = make_config(aws_endpoint_url=moto_url)
    query = "AWS/EC2|CPUUtilization|InstanceId=i-1|Average"
    source = CloudWatchSource({"id": 1, "name": "aws"}, {"region": "eu-west-1", "metrics": [{"title": "CPU", "query": query, "unit": "percent", "limit": 80}]}, creds, config)
    assert (await source.test())["ok"] is True
    alerts = await source.alerts()
    assert alerts[0]["labels"]["alertname"] == "HighCPU" and alerts[0]["state"] == "firing"
    assert (await source.signals(set()))[0].limit == 80
    now = time.time()
    data = await source.query_range(query, now - 900, now + 60, 60)
    assert data["resultType"] == "matrix" and float(data["result"][0]["values"][-1][1]) == 87.5
    instant = await source.query(query)
    assert float(instant["result"][0]["value"][1]) == 87.5
    assert any("CPUUtilization" in name for name in await source.metric_names())
    with pytest.raises(SourceError):
        await source.query("bad")


async def test_cloudwatch_end_to_end_in_engine(console, moto_url, monkeypatch):
    import boto3
    from dataclasses import replace

    console.ctx.config = replace(console.ctx.config, aws_endpoint_url=moto_url)
    console.ctx.engines.config = console.ctx.config
    writer = boto3.client("cloudwatch", region_name="eu-west-1", endpoint_url=moto_url, aws_access_key_id="AKIAIOSFODNN7EXAMPLE", aws_secret_access_key="s")
    writer.put_metric_data(Namespace="App", MetricData=[{"MetricName": "Errors", "Value": 5.0, "Timestamp": datetime.now(timezone.utc)}])
    client = await console.company("AwsCo", "o@aws.example")
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    engine = await console.ctx.engines.get(org_id)
    engine.config = console.ctx.config
    body = {"kind": "cloudwatch", "name": "prod-aws", "config": {"region": "eu-west-1", "metrics": [{"title": "App errors", "query": "App|Errors||Sum"}]}, "secrets": {"access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": "s"}}
    made = await client.post("/api/sources", json=body)
    assert made.status_code == 201, made.text
    assert "AKIAIOSFODNN7EXAMPLE" not in made.text and made.json()["has_secret"] is True
    await engine.refresh_now()
    assert engine.context["sources"][0]["kind"] == "cloudwatch" and engine.context["health"]["status"] == "ok"
    assert any(signal["title"] == "App errors" and signal["series"] for signal in engine.context["signals"])


class FakeAzure(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        if "/oauth2/v2.0/token" in self.path:
            params = parse_qs(body)
            if params.get("client_secret") != ["good"]:
                self._send({"error": "invalid_client"}, 401)
                return
            self._send({"access_token": "tok", "expires_in": 3600})
        elif "/query" in self.path:
            self._send({"tables": [{"columns": [{"name": "Message"}], "rows": [["disk full"]]}]})
        else:
            self._send({}, 404)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer tok":
            self._send({"error": {"message": "unauthorized"}}, 401)
            return
        path = urlparse(self.path).path
        if path.endswith("/Microsoft.AlertsManagement/alerts"):
            self._send({"value": [{"name": "SqlCpu", "properties": {"essentials": {"severity": "Sev1", "targetResourceName": "db1", "signalType": "Metric", "description": "CPU high", "startDateTime": "2026-01-01T00:00:00Z"}}}]})
        elif path.endswith("availabilityStatuses"):
            self._send({"value": [{"id": "/subscriptions/s/resourceGroups/r/providers/Microsoft.Sql/servers/db1/providers/Microsoft.ResourceHealth/availabilityStatuses/current", "properties": {"availabilityState": "Unavailable", "summary": "Outage"}}]})
        elif path.endswith("/metrics"):
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._send({"value": [{"timeseries": [{"metadatavalues": [], "data": [{"timeStamp": now, "average": 91.5}]}]}]})
        elif path.endswith("management/values"):
            self._send({"value": [{"eventTimestamp": "2026-11-04T10:00:00Z", "operationName": {"localizedValue": "Update database"}, "status": {"value": "Succeeded"}, "caller": "sam@x", "resourceId": "/a/b/db1"}]})
        else:
            self._send({}, 404)


@pytest.fixture
def azure_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAzure)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def test_azure_monitor_source(azure_url, make_config):
    config = make_config(azure_management_url=azure_url, azure_login_url=azure_url, azure_logs_url=azure_url)
    query = "/subscriptions/s/resourceGroups/r/providers/Microsoft.Sql/servers/db1|cpu_percent|Average"
    settings = {"subscription_id": "s", "tenant_id": "t", "client_id": "c", "workspace_id": "w", "metrics": [{"title": "SQL CPU", "query": query, "unit": "percent", "limit": 90}]}
    source = AzureMonitorSource({"id": 1, "name": "az"}, settings, {"client_secret": "good"}, config)
    assert (await source.test())["ok"] is True
    alerts = await source.alerts()
    assert alerts[0]["labels"]["severity"] == "critical" and alerts[0]["labels"]["alertname"] == "SqlCpu"
    targets = await source.targets()
    assert targets[0]["health"] == "down" and targets[0]["labels"]["instance"] == "db1"
    assert float((await source.query(query))["result"][0]["value"][1]) == 91.5
    assert (await source.logs("AzureDiagnostics | take 1", 30))["rows"][0]["Message"] == "disk full"
    assert (await source.change_events("2026-11-04T00:00:00Z") or [{}])
    await source.close()
    bad = AzureMonitorSource({"id": 1, "name": "az"}, settings, {"client_secret": "wrong"}, config)
    with pytest.raises(SourceError):
        await bad.alerts()
    await bad.close()
    assert azure_parse(query)["metric"] == "cpu_percent"
    with pytest.raises(SourceError):
        azure_parse("nope")


async def test_managed_prometheus_sign_in(azure_url, make_config):
    import httpx

    token = AzureClientAuth("t", "c", "good", "scope", azure_url)
    assert await token.token() == "tok"
    request = httpx.Request("POST", "https://aps-workspaces.eu-west-1.amazonaws.com/workspaces/ws-1/api/v1/query", content=b"query=up", headers={"Content-Type": "application/x-www-form-urlencoded"})
    signed = next(AwsSigV4Auth("eu-west-1", "AKIAIOSFODNN7EXAMPLE", "secret").auth_flow(request))
    assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256") and "/aps/aws4_request" in signed.headers["Authorization"]
    config = make_config()
    source = PrometheusSource({"id": 1, "name": "amp", "poll_seconds": 15}, {"scheme": "https", "host": "aps-workspaces.eu-west-1.amazonaws.com", "port": 443, "path_prefix": "workspaces/ws-1", "auth_type": "aws_sigv4", "region": "eu-west-1"}, {"access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": "s"}, config)
    assert source.describe().endswith("/workspaces/ws-1")
    await source.close()


class FakeKube(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if self.headers.get("Authorization") != "Bearer kube":
            self.send_response(401)
            self.end_headers()
            return
        if "/loki/" in path:
            if path.endswith("labels"):
                data = ["job"]
            elif path.endswith("query_range"):
                data = {"resultType": "streams", "result": [{"stream": {"job": "api"}, "values": [["1700000000000000000", "ERROR password=hunter2 failed"]]}]}
            else:
                data = {"resultType": "vector", "result": [{"metric": {"job": "api"}, "value": [1, "17"]}]}
            payload = {"status": "success", "data": data}
        elif path == "/version":
            payload = {"gitVersion": "v1.31"}
        elif path.endswith("/events"):
            payload = {"items": [{"metadata": {"namespace": "prod"}, "involvedObject": {"kind": "Pod", "name": "api-1"}, "reason": "BackOff", "message": "Back-off restarting", "count": 9, "lastTimestamp": "2026-11-04T10:00:00Z"}]}
        elif path.endswith("/pods"):
            payload = {"items": [
                {"metadata": {"namespace": "prod", "name": "api-1"}, "status": {"phase": "Running", "containerStatuses": [{"restartCount": 9, "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {"terminated": {"reason": "OOMKilled"}}}]}},
                {"metadata": {"namespace": "prod", "name": "ok-1"}, "status": {"phase": "Running", "containerStatuses": [{"restartCount": 0, "state": {"running": {}}}]}},
            ]}
        elif path.endswith("/deployments"):
            payload = {"items": [{"metadata": {"namespace": "prod", "name": "api", "annotations": {"deployment.kubernetes.io/revision": "7"}}, "spec": {"replicas": 3, "template": {"spec": {"containers": [{"image": "api:2.18"}]}}}, "status": {"readyReplicas": 1}}]}
        else:
            payload = {}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def kube_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeKube)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def test_kubernetes_client_reads_state(kube_url):
    client = KubernetesClient({"url": kube_url}, {"token": "kube"}, LOCAL)
    assert (await client.test())["ok"]
    summary = await client.summary()
    assert summary["unhealthy_pods"][0]["pod"] == "api-1" and "CrashLoopBackOff" in summary["unhealthy_pods"][0]["reasons"]
    assert len(summary["unhealthy_pods"]) == 1
    assert summary["warning_events"][0]["reason"] == "BackOff"
    assert summary["deployments_below_desired"][0]["images"] == ["api:2.18"]
    await client.close()
    bad = KubernetesClient({"url": kube_url}, {"token": "wrong"}, LOCAL)
    with pytest.raises(SourceError):
        await bad.test()
    await bad.close()


async def test_integrations_join_context_and_tools(console, prom_server, kube_url):
    from app.context.render import render_context
    from app.investigation.tools import ToolRunner

    client = await console.company("IntCo", "o@int.example")
    await client.post("/api/sources", json={"name": "lab", "config": {"host": "127.0.0.1", "port": prom_server.server_address[1]}, "secrets": {}})
    org_id = (await client.get("/api/auth/me")).json()["org_id"]
    tested = await client.post("/api/integrations/test", json={"kind": "kubernetes", "name": "k", "config": {"url": kube_url}, "secrets": {"token": "kube"}})
    assert tested.json()["ok"] is True
    assert (await client.post("/api/integrations", json={"kind": "kubernetes", "name": "k", "config": {"url": kube_url}, "secrets": {"token": "kube"}})).status_code == 201
    made = await client.post("/api/integrations", json={"kind": "loki", "name": "l", "config": {"url": kube_url, "selector": '{job="api"}'}, "secrets": {"token": "kube"}})
    assert made.status_code == 201 and "kube" not in json.dumps(made.json().get("config"))
    engine = await console.ctx.engines.get(org_id)
    engine._integrations_at = 0
    await engine.refresh_now()
    text = render_context(engine.context)
    assert "api-1" in text and "CrashLoopBackOff" in text and "job=api: 17 lines" in text
    assert "logs" not in text.split("Not available here:")[1].split(".")[0]
    runner = ToolRunner(engine, console.ctx.db, console.ctx.box, console.ctx.config)
    names = {tool["name"] for tool in await runner.definitions()}
    assert {"query_logs", "get_kubernetes_state"} <= names
    ok, logs = await runner.run("query_logs", {"query": '{job="api"}', "minutes": 5})
    assert ok and "ERROR" in logs and "hunter2" not in logs
    ok, pods = await runner.run("get_kubernetes_state", {"resource": "pods"})
    assert ok and "CrashLoopBackOff" in pods


async def test_loki_client_errors(kube_url):
    client = LokiClient({"url": kube_url}, {"token": "wrong"}, LOCAL)
    with pytest.raises(SourceError):
        await client.query('{job="x"}')
    await client.close()


# Single sign-on -------------------------------------------------------------
class FakeIdP(BaseHTTPRequestHandler):
    email = "pat@ssoco.example"

    def log_message(self, *args):
        pass

    def _send(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if "openid-configuration" in self.path:
            self._send({"authorization_endpoint": f"{base}/authorize", "token_endpoint": f"{base}/token", "userinfo_endpoint": f"{base}/userinfo"})
        elif self.path.startswith("/userinfo"):
            self._send({"sub": "u-1", "email": FakeIdP.email, "email_verified": True, "name": "Pat SSO"})
        else:
            self._send({}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = parse_qs(self.rfile.read(length).decode())
        if body.get("code") == ["good"] and body.get("client_secret") == ["sso-secret"] and body.get("code_verifier"):
            self._send({"access_token": "at"})
        else:
            self._send({"error": "invalid_grant"}, 400)


@pytest.fixture
def idp_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeIdP)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def test_single_sign_on_flow(console, idp_url):
    from urllib.parse import parse_qs as qs

    owner = await console.company("SsoCo", "boss@ssoco.example")
    assert (await owner.put("/api/org/sso", json={"enabled": True, "issuer": idp_url, "client_id": "cid", "client_secret": "sso-secret", "domains": ["other.example"]})).status_code == 422
    saved = await owner.put("/api/org/sso", json={"enabled": True, "issuer": idp_url, "client_id": "cid", "client_secret": "sso-secret", "domains": ["ssoco.example"], "default_role": "engineer"})
    assert saved.status_code == 200 and "sso-secret" not in saved.text
    rival = await console.company("Rival", "boss@rival.example")
    assert (await rival.put("/api/org/sso", json={"enabled": True, "issuer": idp_url, "client_id": "x", "domains": ["ssoco.example"]})).status_code in {409, 422}
    user = console.client()
    assert (await user.post("/api/auth/sso/start", json={"email": "nobody@unknown.example"})).status_code == 404
    start = await user.post("/api/auth/sso/start", json={"email": "pat@ssoco.example"})
    params = qs(start.json()["redirect"].split("?")[1])
    assert params["code_challenge_method"] == ["S256"] and params["client_id"] == ["cid"]
    forged = await user.get("/api/auth/sso/callback", params={"state": "forged", "code": "good"})
    assert forged.status_code == 302 and "failed" in forged.headers["location"]
    done = await user.get("/api/auth/sso/callback", params={"state": params["state"][0], "code": "good"})
    assert done.status_code == 302 and "overview" in done.headers["location"]
    me = (await user.get("/api/auth/me")).json()
    assert me["email"] == "pat@ssoco.example" and me["role"] == "engineer" and me["org_name"] == "SsoCo"
    replay = await console.client().get("/api/auth/sso/callback", params={"state": params["state"][0], "code": "good"})
    assert "failed" in replay.headers["location"]
    policies = {"password_min_length": 12, "session_hours": 12, "allowed_email_domains": [], "require_sso": True, "allow_cloud_ai": True}
    assert (await owner.put("/api/org/policies", json=policies)).status_code == 200
    blocked = await console.client().post("/api/auth/login", json={"email": "boss@ssoco.example", "password": "another-Good-Passphrase-77"})
    assert blocked.status_code == 403 and "single sign-on" in blocked.json()["detail"]


async def test_smtp_delivery_with_a_real_server(make_config):
    from aiosmtpd.controller import Controller

    from app.mailer import MailError, Mailer

    received = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            received.append(envelope)
            return "250 OK"

    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    controller = Controller(Handler(), hostname="127.0.0.1", port=port)
    controller.start()
    mailer = Mailer(make_config(smtp_host="127.0.0.1", smtp_port=port, smtp_from="noreply@x.example", smtp_tls="none"))
    await mailer.send("to@x.example", "Subject\nInjected: yes", "Body", "https://x.example/#/verify?token=abc", "Verify email")
    controller.stop()
    assert received[0].rcpt_tos == ["to@x.example"]
    data = received[0].content.decode()
    assert "Injected: yes" in data and "\nInjected" not in data.split("\r\n\r\n")[0].replace("\r\n", "\n").split("Subject:")[1].split("\n")[0]
    # The body may be encoded for transport, so read it the way a mail program does.
    parsed = message_from_string(data, policy=email_policy.default)
    assert "token=abc" in parsed.get_body(preferencelist=("plain",)).get_content()
    assert parsed.get_body(preferencelist=("html",)).get_content().count("cid:rcc-logo") == 1
    broken = Mailer(make_config(smtp_host="127.0.0.1", smtp_port=1, smtp_from="a@x.example", smtp_tls="none"))
    with pytest.raises(MailError):
        await broken.send("to@x.example", "s", "b")
