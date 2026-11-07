"""A local stand-in for the Ollama and Claude APIs, following their documented wire formats.

It is for automated tests and offline UI checks, and gives scripted answers rather than real analysis.
Run with: python tests/fake_models.py 18434
"""
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

received: list[dict] = []


def scripted_answer(system: str) -> str:
    alerts = re.findall(r"^- (\w+) severity=", system, flags=re.M)
    version = re.search(r"Context version (\d+)", system)
    firing = alerts[0] if alerts else "no alert"
    if "Turn this investigation into a remediation workflow" in system:
        return json.dumps({"name": "Test workflow", "summary": "Drafted by the test model.", "risk": "low", "match": {"alerts": ["highcost"], "keywords": ["test phrase"]},
                           "steps": [{"title": "Check it", "detail": "Look first.", "command": "uptime", "risk": "none", "undo": "", "verify": "It responds."}]})
    return (
        f"## Summary\nScripted test answer from context version {version.group(1) if version else '?'}.\n\n"
        f"## Likely root cause\nThis test model saw **{firing}**.\nConfidence: Low\nThis is not a real model.\n\n"
        "## Evidence\n- `sum(rate(http_requests_total[5m]))` was checked.\n\n"
        "## Competing explanation\nA real model would compare other causes here.\n\n"
        "## Next checks\n- Connect a real model.\n\n## Suggested remediation\n- None. Risk: none."
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _start_stream(self, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self):
        if self.path == "/api/tags":
            self._json({"models": [
                {"name": "llama3.2:latest", "size": 2019393189, "details": {"parameter_size": "3.2B"}},
                {"name": "notools:latest", "size": 1000000000, "details": {"parameter_size": "1B"}},
            ]})
        elif self.path.startswith("/v1/models"):
            if self.headers.get("x-api-key") != "sk-ant-test-valid-key":
                self._json({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, 401)
                return
            self._json({"data": [{"id": "claude-test-1", "display_name": "Claude Test 1", "type": "model"}], "has_more": False})
        elif self.path == "/_received":
            self._json(received)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        received.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if self.path == "/api/chat":
            self._ollama(body)
        elif self.path == "/v1/messages":
            self._claude(body)
        else:
            self._json({"error": "not found"}, 404)

    def _ollama(self, body):
        if body["model"] == "missing":
            self._json({"error": "model 'missing' not found"}, 404)
            return
        if body["model"].startswith("notools") and body.get("tools"):
            self._json({"error": "registry.ollama.ai/library/notools does not support tools"}, 400)
            return
        self._start_stream("application/x-ndjson")
        system = body["messages"][0]["content"]
        used_tool = any(message["role"] == "tool" for message in body["messages"])
        if body["model"].startswith(("careless", "careful")):
            self._replay(body, system, used_tool)
            return
        if body.get("tools") and not used_tool:
            chunk = {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {
                "name": "run_range_query", "arguments": {"query": "sum(rate(http_requests_total[5m]))", "minutes": 10}}}]}, "done": False}
            self.wfile.write((json.dumps(chunk) + "\n").encode())
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 500, "eval_count": 20}) + "\n").encode())
            return
        for word in re.findall(r"\S+\s*", scripted_answer(system)):
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": word}, "done": False}) + "\n").encode())
            self.wfile.flush()
            time.sleep(0.01)
        self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "prompt_eval_count": 900, "eval_count": 120}) + "\n").encode())

    def _replay(self, body, system, used_tool):
        """Two behaviours seen on Nov 6: a careless small model that queries the wrong system and ignores the crash,
        and a careful one that reads the crashed container's logs."""
        def done():
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                                          "prompt_eval_count": 800, "eval_count": 60}) + "\n").encode())
        if body.get("tools") and not used_tool:
            names = {tool["function"]["name"] for tool in body["tools"]}
            if body["model"].startswith("careless"):
                name, arguments = ("run_instant_query", {"query": "kubernetes.pods{last_healthy_time≠''} | summarize count()"})
                if name not in names:
                    name = "query_logs"
            else:
                name, arguments = "get_pod_logs", {"namespace": "shop", "pod": "payment", "previous": True}
            chunk = {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}, "done": False}
            self.wfile.write((json.dumps(chunk) + "\n").encode())
            done()
            return
        tool_text = " ".join(message.get("content", "") for message in body["messages"] if message["role"] == "tool")
        if body["model"].startswith("careless"):
            answer = ("## Summary\nThere are no recent changes or anomalies that indicate a problem.\n\n## Likely root cause\nUnknown.\nConfidence: Unknown\n\n"
                      "## Evidence\n- No recent errors in logs (Query: `invalid parameter \"query\"`).\n\n## Competing explanation\nUnknown.\n\n"
                      "## Next checks\n- Run a log query.\n\n## Suggested remediation\n- Check disk health with `df -h`.")
        else:
            seen = "connection refused postgres:5432" in tool_text and "connection refused postgres:5432" in system
            answer = ("## Summary\nThe payment deployment crashes on start because it can't reach postgres.\n\n"
                      f"## Likely root cause\npayment exits with code 1: ERROR: connection refused postgres:5432.\nConfidence: {'High' if seen else 'Low'}\n\n"
                      "## Evidence\n- The crashed container's last line: connection refused postgres:5432.\n- 12 restarts, exit code 1.\n\n"
                      "## Competing explanation\nA bad image, ruled out because the app starts and logs before failing.\n\n"
                      "## Next checks\n- `kubectl -n shop get svc postgres`\n\n## Suggested remediation\n- Start postgres or fix its address. Risk: low.")
        for word in re.findall(r"\S+\s*", answer):
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": word}, "done": False}) + "\n").encode())
        done()

    def _claude(self, body):
        if self.headers.get("x-api-key") != "sk-ant-test-valid-key" or self.headers.get("anthropic-version") != "2023-06-01":
            self._json({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, 401)
            return
        self._start_stream("text/event-stream")

        def send(event):
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

        send({"type": "message_start", "message": {"usage": {"input_tokens": 700, "output_tokens": 1}}})
        send({"type": "ping"})
        used_tool = any(isinstance(m["content"], list) and any(b.get("type") == "tool_result" for b in m["content"]) for m in body["messages"])
        if body.get("tools") and not used_tool:
            send({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "run_instant_query", "input": {}}})
            send({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"query":'}})
            send({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": ' "up"}'}})
            send({"type": "content_block_stop", "index": 0})
            send({"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 30}})
        else:
            send({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            for word in re.findall(r"\S+\s*", scripted_answer(body["system"])):
                send({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": word}})
            send({"type": "content_block_stop", "index": 0})
            send({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 110}})
        send({"type": "message_stop"})


def serve(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 18434).serve_forever()
