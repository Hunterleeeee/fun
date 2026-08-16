import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fun.provider import ModelConfig, OpenAICompatible, ProviderError, tool_schemas
from fun.runtime import Runtime


class ModelsHandler(BaseHTTPRequestHandler):
    status = 200
    payload = {"data": [{"id": "model-z"}, {"id": "model-a"}, {"id": "model-a"}, {"name": "ignored"}]}
    seen_auth = None

    def do_GET(self):
        self.__class__.seen_auth = self.headers.get("Authorization")
        body = json.dumps(self.__class__.payload).encode()
        self.send_response(self.__class__.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class MarkerSSEHandler(BaseHTTPRequestHandler):
    marker = "default"
    calls = 0

    def do_POST(self):
        self.__class__.calls += 1
        length = int(self.headers.get("Content-Length", "0"))
        json.loads(self.rfile.read(length))
        body = f'data: {{"choices":[{{"delta":{{"content":"{self.__class__.marker}"}}}}]}}\n\ndata: [DONE]\n\n'.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class SmokeHandler(BaseHTTPRequestHandler):
    seen_auth = None

    def do_POST(self):
        self.__class__.seen_auth = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length", "0"))
        json.loads(self.rfile.read(length))
        body = b'data: {"choices":[{"delta":{"content":"FUN_SMOKE_OK"}}]}\n\ndata: [DONE]\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class InvalidPlanProvider:
    def stream(self, messages, tools=None):
        yield {"plan": ["", 4, "valid"], "choices": [{"delta": {"content": "Kept the safe plan."}}]}


class PlanProvider:
    def __init__(self, in_delta=False):
        self.in_delta = in_delta

    def stream(self, messages, tools=None):
        delta = {"content": "Plan updated."}
        if self.in_delta:
            delta["plan"] = ["inspect", "verify"]
            yield {"choices": [{"delta": delta}]}
        else:
            yield {"plan": ["inspect", "verify"], "choices": [{"delta": delta}]}


class FakeProvider:
    def __init__(self):
        self.calls = 0

    def stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read", "arguments": '{"path":"hello.txt"}'}}]}}]}
        else:
            yield {"choices": [{"delta": {"content": "The file was inspected."}}]}


class AgentLoopTests(unittest.TestCase):
    def test_list_models_real_http_deduplicates_and_sorts(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = OpenAICompatible(ModelConfig(f"http://127.0.0.1:{server.server_port}/v1", "models-secret", "unused"))
            self.assertEqual(provider.list_models(), ["model-a", "model-z"])
            self.assertEqual(ModelsHandler.seen_auth, "Bearer models-secret")
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()

    def test_list_models_auth_failure_is_stable(self):
        ModelsHandler.status = 401
        server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = OpenAICompatible(ModelConfig(f"http://127.0.0.1:{server.server_port}/v1", "bad", "unused"))
            with self.assertRaisesRegex(ProviderError, "PROVIDER_AUTH_FAILED"):
                provider.list_models()
        finally:
            ModelsHandler.status = 200
            server.shutdown()
            thread.join(2)
            server.server_close()

    def test_runtime_uses_new_provider_after_switch(self):
        first = type("First", (MarkerSSEHandler,), {"marker": "OLD_PROVIDER", "calls": 0})
        second = type("Second", (MarkerSSEHandler,), {"marker": "NEW_PROVIDER", "calls": 0})
        servers = [ThreadingHTTPServer(("127.0.0.1", 0), first), ThreadingHTTPServer(("127.0.0.1", 0), second)]
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads: thread.start()
        try:
            with TemporaryDirectory() as directory:
                old = OpenAICompatible(ModelConfig(f"http://127.0.0.1:{servers[0].server_port}/v1", "key", "old"))
                new = OpenAICompatible(ModelConfig(f"http://127.0.0.1:{servers[1].server_port}/v1", "key", "new"))
                runtime = Runtime(directory, provider=old)
                runtime.create_task("switch provider")
                self.assertEqual(runtime.run_model_turn(), "OLD_PROVIDER")
                runtime.provider = new
                runtime.model = "new"
                self.assertEqual(runtime.run_model_turn(), "NEW_PROVIDER")
                self.assertEqual(first.calls, 1)
                self.assertEqual(second.calls, 1)
                runtime.stop()
        finally:
            for server in servers: server.shutdown(); server.server_close()
            for thread in threads: thread.join(2)

    def test_recovered_task_continues_model_turn_after_discard(self):
        with TemporaryDirectory() as directory:
            original = Runtime(directory, state_dir=directory)
            original.create_task("continue after recovery")
            original.emit("approval.pending", original.task.id, call_id="call_pending", name="exec", risk="medium", arguments={"command": "echo hi"})
            session_id = original.session_id
            original.close()
            provider = FakeProvider()
            recovered = Runtime.recover(directory, directory, session_id, provider=provider, approval="auto")
            self.assertEqual(recovered.task.status, "recovery_required")
            recovered.acknowledge_recovery("discard")
            output = recovered.run_model_turn()
            self.assertEqual(output, "The file was inspected.")
            self.assertEqual(provider.calls, 2)
            self.assertEqual(recovered.task.status, "running")
            recovered.stop()

    def test_tool_lifecycle_reports_status_and_elapsed_time(self):
        statuses = []
        class ToolProvider:
            def __init__(self):
                self.calls = 0
            def stream(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_tool", "function": {"name": "read", "arguments": '{"path":"missing.txt"}'}}]}}]}
                else:
                    yield {"choices": [{"delta": {"content": "done"}}]}
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, provider=ToolProvider(), approve=lambda name, risk: True)
            runtime.create_task("inspect")
            runtime.run_model_turn(on_status=lambda kind, payload: statuses.append((kind, payload)))
            self.assertIn("tool.executing", [kind for kind, _ in statuses])
            pending = [payload for kind, payload in statuses if kind == "approval.pending"]
            if pending:
                self.assertIn("arguments", pending[0])
            completed = next(payload for kind, payload in statuses if kind == "tool.failed")
            self.assertIsInstance(completed["elapsed_ms"], int)
            self.assertIn("text", completed)
            event = next(event for event in runtime.events.list() if event.type == "tool.failed")
            self.assertIn("elapsed_ms", event.payload)

    def test_model_step_records_start_first_token_and_completion_timing(self):
        class TimingProvider:
            def stream(self, messages, tools=None):
                yield {"_meta": {"ttft_ms": 7}, "choices": [{"delta": {"content": "done"}}]}
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, provider=TimingProvider())
            runtime.create_task("timing")
            runtime.run_model_turn()
            types = [event.type for event in runtime.events.list()]
            self.assertIn("model.step_started", types)
            self.assertIn("model.first_token", types)
            completed = next(event for event in runtime.events.list() if event.type == "model.completed")
            self.assertEqual(completed.payload["timing"]["ttft_ms"], 7)
            self.assertIsInstance(completed.payload["timing"]["step_ms"], int)

    def test_model_request_compacts_oversized_context(self):
        class CaptureProvider:
            def __init__(self):
                self.messages = None
            def stream(self, messages, tools=None):
                self.messages = messages
                yield {"choices": [{"delta": {"content": "ok"}}]}
        with TemporaryDirectory() as directory:
            provider = CaptureProvider()
            runtime = Runtime(directory, provider=provider)
            runtime.create_task("compact")
            runtime.task.messages = [{"role": "system", "content": "system"}] + [{"role": "user", "content": "x" * 20000} for _ in range(5)]
            list(runtime.request_model())
            self.assertLessEqual(sum(len(str(item.get("content", ""))) for item in provider.messages), 32000)
            self.assertIn("context.compacted", [event.type for event in runtime.events.list()])

    def test_local_sse_provider_smoke_script(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), SmokeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = os.environ | {
                "FUN_API_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "FUN_API_KEY": "smoke-secret",
                "FUN_MODEL": "smoke-model",
            }
            result = subprocess.run([sys.executable, "scripts/provider_smoke.py"], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("stream_chunks=1", result.stdout)
            self.assertEqual(SmokeHandler.seen_auth, "Bearer smoke-secret")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_provider_stream_handles_done_token_split_across_chunks(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
                yield b'data: [DO'
                yield b'NE]\n\n'
                yield b'data: not-json-secret\n\n'

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            items = list(provider.stream([], []))
        self.assertEqual(len(items), 1)

    def test_provider_stream_stops_after_done_event(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
                yield b'data: [DONE]\n\n'
                yield b'data: not-json-secret\n\n'

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            items = list(provider.stream([], []))
        self.assertEqual(len(items), 1)

    def test_provider_stream_handles_crlf_split_across_chunks(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'data: {"choices": [{"delta": {"content": "ok"}}]}\r'
                yield b'\ndata: [DONE]\r\n'

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            items = list(provider.stream([], []))
        self.assertEqual(items[0]["choices"][0]["delta"]["content"], "ok")

    def test_provider_stream_joins_multiline_data_events(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'data: {"choices": [\n'
                yield b'data: {"delta": {"content": "ok"}}]}\n'
                yield b'\n'
                yield b'data: [DONE]\n\n'

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            items = list(provider.stream([], []))
        self.assertEqual(items[0]["choices"][0]["delta"]["content"], "ok")

    def test_provider_stream_ignores_sse_comments_and_accepts_done_spacing(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b': keepalive\r\n'
                yield b'event: message\r\n'
                yield b'data: {"choices": [{"delta": {"content": "ok"}}]}\r\n'
                yield b'data:   [DONE]  \r\n'

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            items = list(provider.stream([], []))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["choices"][0]["delta"]["content"], "ok")

    def test_provider_normalizes_status_types_safely(self):
        class Response:
            status = "401"
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'secret'
        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaises(ProviderError) as context:
                list(provider.stream([], []))
        self.assertEqual(context.exception.error_tag, "PROVIDER_AUTH_FAILED")

    def test_provider_rejects_conflicting_status_and_code(self):
        class Response:
            status = 200
            code = 401
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __iter__(self): yield b'secret'
        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaises(ProviderError) as context:
                list(provider.stream([], []))
        self.assertEqual(context.exception.error_tag, "PROVIDER_INVALID_STATUS")

    def test_provider_rejects_fractional_or_padded_status(self):
        class Response:
            headers = {}
            def __init__(self, status): self.status = status
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __iter__(self): yield b'secret'
        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        for status in (401.5, " 401 "):
            with patch("urllib.request.urlopen", return_value=Response(status)):
                with self.assertRaises(ProviderError) as context:
                    list(provider.stream([], []))
            self.assertEqual(context.exception.error_tag, "PROVIDER_INVALID_STATUS")

    def test_provider_rejects_invalid_status_type(self):
        class Response:
            status = "unknown"
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'secret'
        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaises(ProviderError) as context:
                list(provider.stream([], []))
        self.assertEqual(context.exception.error_tag, "PROVIDER_INVALID_STATUS")

    def test_provider_rejects_non_success_status_without_body(self):
        class Headers:
            def get(self, name, default=""):
                return "application/json"
        class Response:
            status = 401
            headers = Headers()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'{"secret":"body"}'

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaises(ProviderError) as context:
                list(provider.stream([], []))
        self.assertEqual(context.exception.error_tag, "PROVIDER_AUTH_FAILED")
        self.assertNotIn("body", str(context.exception))

    def test_provider_tolerates_non_string_content_type_header(self):
        class Headers:
            def get(self, name, default=""):
                return None
        class Response:
            headers = Headers()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            self.assertEqual(len(list(provider.stream([], []))), 1)

    def test_provider_rejects_non_sse_response_without_body(self):
        class Headers:
            def get(self, name, default=""):
                return "application/json"
        class Response:
            headers = Headers()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'{"secret":"response"}'

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaises(ProviderError) as context:
                list(provider.stream([], []))
        self.assertEqual(context.exception.error_tag, "PROVIDER_UNEXPECTED_CONTENT_TYPE")
        self.assertNotIn("response", str(context.exception))

    def test_provider_stream_rejects_malformed_event_without_body(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'data: not-secret-json\n'

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaises(ProviderError) as context:
                list(provider.stream([], []))
        self.assertEqual(context.exception.error_tag, "PROVIDER_MALFORMED_EVENT")
        self.assertNotIn("not-secret-json", str(context.exception))

    def test_provider_stream_handles_split_sse_lines(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield b'data: {"choices": [{"delta": {"content": "hel'
                yield b'lo"}}]}\n'
                yield b'data: [DONE]\n'

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", return_value=Response()):
            self.assertEqual(list(provider.stream([], []))[0]["choices"][0]["delta"]["content"], "hello")

    def test_provider_rejects_oversized_payload(self):
        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model", max_payload_bytes=10))
        with self.assertRaises(ProviderError) as context:
            next(provider.stream([{"content": "x" * 100}]))
        self.assertEqual(context.exception.error_tag, "PROVIDER_PAYLOAD_TOO_LARGE")

    def test_provider_rejects_non_serializable_payload(self):
        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with self.assertRaises(ProviderError) as context:
            next(provider.stream([{"content": object()}]))
        self.assertEqual(context.exception.error_tag, "PROVIDER_INVALID_PAYLOAD")

    def test_provider_rejects_invalid_request_collections(self):
        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with self.assertRaises(ProviderError) as messages_error:
            next(provider.stream("not-a-list"))
        self.assertEqual(messages_error.exception.error_tag, "PROVIDER_INVALID_MESSAGES")
        with self.assertRaises(ProviderError) as tools_error:
            next(provider.stream([], ["not-a-tool"]))
        self.assertEqual(tools_error.exception.error_tag, "PROVIDER_INVALID_TOOLS")

    def test_provider_rejects_insecure_or_empty_endpoint(self):
        with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_ENDPOINT"):
            OpenAICompatible(ModelConfig("file:///tmp/provider", "key", "model"))
        with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_ENDPOINT"):
            OpenAICompatible(ModelConfig("", "key", "model"))
        with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_ENDPOINT"):
            OpenAICompatible(ModelConfig("https://provider.invalid?api_key=secret", "key", "model"))
        with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_ENDPOINT"):
            OpenAICompatible(ModelConfig("https://user:pass@provider.invalid", "key", "model"))
        OpenAICompatible(ModelConfig("https://[::1]/api", "key", "model"))
        with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_ENDPOINT"):
            OpenAICompatible(ModelConfig("https://provider.invalid:0", "key", "model"))
        with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_ENDPOINT"):
            OpenAICompatible(ModelConfig("https://provider.invalid:65536", "key", "model"))
        with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_ENDPOINT"):
            OpenAICompatible(ModelConfig("https://provider.invalid:notaport", "key", "model"))
        for model in ("", "   ", None, 123):
            with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_MODEL"):
                OpenAICompatible(ModelConfig("https://provider.invalid", "key", model))
        for api_key in ("", "   ", None, 123):
            with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_API_KEY"):
                OpenAICompatible(ModelConfig("https://provider.invalid", api_key, "model"))
        for max_payload in (0, -1, True, 1.5, 16 * 1024 * 1024 + 1):
            with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_PAYLOAD_LIMIT"):
                OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model", max_payload_bytes=max_payload))
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaisesRegex(ValueError, "INVALID_PROVIDER_TIMEOUT"):
                OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model", timeout=timeout))

    def test_provider_http_status_like_oserror_is_classified(self):
        class StatusError(OSError):
            code = 401

        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "key", "model"))
        with patch("urllib.request.urlopen", side_effect=StatusError("secret http body")):
            with self.assertRaises(ProviderError) as context:
                next(provider.stream([], []))
        self.assertEqual(context.exception.error_tag, "PROVIDER_AUTH_FAILED")
        self.assertNotIn("secret http body", str(context.exception))

    def test_provider_error_classification_is_safe(self):
        provider = OpenAICompatible(ModelConfig("https://provider.invalid", "secret-key", "model"))
        with patch("urllib.request.urlopen", side_effect=TimeoutError("secret response")):
            with self.assertRaises(ProviderError) as context:
                next(provider.stream([], []))
        self.assertEqual(context.exception.error_tag, "PROVIDER_TIMEOUT")
        self.assertNotIn("secret response", str(context.exception))

    def test_invalid_model_plan_is_rejected_without_breaking_turn(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=InvalidPlanProvider())
            runtime.create_task("keep plan")
            original = list(runtime.task.plan)
            output = runtime.run_model_turn()
            self.assertEqual(output, "Kept the safe plan.")
            self.assertEqual(runtime.task.plan, original)
            self.assertIn("plan.rejected", [event.type for event in runtime.events.list()])
            self.assertEqual(runtime.task.plan_error, "INVALID_PLAN")
            self.assertEqual(runtime.task.plan_error_summary["count"], 3)
            rejected = next(event for event in runtime.events.list() if event.type == "plan.rejected")
            self.assertEqual(rejected.payload["summary"]["count"], 3)
            self.assertNotIn("Kept the safe plan", str(rejected.payload))

    def test_model_plan_proposal_replaces_runtime_plan(self):
        for provider in (PlanProvider(), PlanProvider(in_delta=True)):
            with TemporaryDirectory() as directory:
                runtime = Runtime(directory, "auto", provider=provider)
                runtime.create_task("plan this")
                runtime.run_model_turn()
                self.assertEqual(runtime.task.plan, ["inspect", "verify"])
                self.assertIn("plan.replaced", [event.type for event in runtime.events.list()])
            parsed = next(event for event in runtime.events.list() if event.type == "response.parsed")
            self.assertEqual(parsed.payload["summary"]["tool_calls"], 0)

    def test_stale_approval_resolution_does_not_change_new_pending_tool(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("stale resolution")
            runtime.emit("tool.executing", runtime.task.id, call_id="new", name="exec")
            runtime.emit("approval.resolved", runtime.task.id, call_id="old", name="exec", allowed=True)
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.pending_tool["call_id"], "new")
            self.assertEqual(recovered.task.agent_state, "tool.executing")
            recovered.stop()

    def test_stale_approval_rejection_does_not_clear_new_pending_tool(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("stale rejection")
            runtime.emit("tool.executing", runtime.task.id, call_id="new", name="exec")
            runtime.emit("approval.rejected", runtime.task.id, call_id="old", name="exec", reason="callback_denied")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.pending_tool["call_id"], "new")
            self.assertEqual(recovered.task.agent_state, "tool.executing")
            recovered.stop()

    def test_stale_tool_failure_does_not_clear_new_pending_tool(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("stale tool")
            runtime.emit("tool.executing", runtime.task.id, call_id="new", name="read")
            runtime.emit("tool.failed", runtime.task.id, call_id="old", name="read", ok=False, error_tag="TOOL_EXECUTION_FAILED")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.pending_tool["call_id"], "new")
            self.assertEqual(recovered.task.agent_state, "tool.executing")
            recovered.stop()

    def test_stale_approval_failure_does_not_clear_new_pending_tool(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("stale approval")
            runtime.emit("approval.pending", runtime.task.id, call_id="new", name="exec", risk="medium", arguments={})
            runtime.emit("approval.failed", runtime.task.id, call_id="old", name="exec", error_type="RuntimeError", error_tag="APPROVAL_CALLBACK_FAILED")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "approval.pending")
            self.assertEqual(recovered.task.pending_tool["call_id"], "new")
            recovered.stop()

    def test_approval_failure_then_tool_failure_replay_stays_ready(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("approval duplicate terminal")
            runtime.emit("approval.pending", runtime.task.id, call_id="c1", name="exec", risk="medium", arguments={})
            runtime.emit("approval.failed", runtime.task.id, call_id="c1", name="exec", error_type="RuntimeError", error_tag="APPROVAL_CALLBACK_FAILED")
            runtime.emit("tool.failed", runtime.task.id, call_id="c1", name="exec", ok=False, error_type="RuntimeError", error_tag="APPROVAL_CALLBACK_FAILED")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "ready")
            self.assertIsNone(recovered.task.pending_tool)
            recovered.stop()

    def test_approval_failure_replay_projects_ready_without_tool_fact(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("approval replay fact")
            runtime.emit("approval.pending", runtime.task.id, call_id="c1", name="exec", risk="medium", arguments={})
            runtime.emit("approval.failed", runtime.task.id, call_id="c1", name="exec", error_type="RuntimeError", error_tag="APPROVAL_CALLBACK_FAILED")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "ready")
            self.assertIsNone(recovered.task.pending_tool)
            recovered.stop()

    def test_approval_failure_facts_are_atomic(self):
        class FailingBatch:
            def append_many(self, events):
                raise OSError("disk full")

        def broken(name, risk):
            raise RuntimeError("callback")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "smart", approve=broken)
            runtime.create_task("approval facts")
            runtime.events._durable = FailingBatch()
            with self.assertRaises(OSError):
                runtime.run_tool("exec", command="echo hi")
            self.assertIsNone(runtime.task.pending_tool)

    def test_approval_failure_ready_persistence_keeps_pending_until_ready(self):
        class FailingStore:
            def append(self, event):
                if event.type == "agent.node" and event.payload.get("node") == "ready":
                    raise OSError("disk full")

        def broken(name, risk):
            raise RuntimeError("approval callback")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "smart", approve=broken)
            runtime.create_task("approval atomic")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.run_tool("exec", command="echo hi")
            self.assertIsNotNone(runtime.task.pending_tool)

    def test_approval_rejection_replays_without_pending_tool(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "smart", approve=lambda name, risk: False, state_dir=directory)
            runtime.create_task("approval reject replay")
            result = runtime.run_tool("exec", command="echo hi")
            self.assertFalse(result.ok)
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertIsNone(recovered.task.pending_tool)
            self.assertEqual(recovered.task.agent_state, "ready")
            recovered.stop()

    def test_approval_callback_non_bool_cannot_bypass_policy(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "smart", approve=lambda name, risk: "yes", state_dir=directory)
            runtime.create_task("approval type")
            with self.assertRaisesRegex(TypeError, "must return bool"):
                runtime.run_tool("exec", command="echo hi")
            failed = next(event for event in runtime.events.list() if event.type == "approval.failed")
            self.assertEqual(failed.payload["error_tag"], "APPROVAL_CALLBACK_FAILED")
            self.assertIsNone(runtime.task.pending_tool)
            self.assertEqual(runtime.task.agent_state, "ready")
            runtime.close()

    def test_approval_callback_failure_records_safe_fact_and_ready(self):
        def broken_approval(name, risk):
            raise RuntimeError("approval secret")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "smart", approve=broken_approval, state_dir=directory)
            runtime.create_task("approval failure")
            with self.assertRaisesRegex(RuntimeError, "approval secret"):
                runtime.run_tool("exec", command="echo hi")
            failed = next(event for event in runtime.events.list() if event.type == "approval.failed")
            self.assertEqual(failed.payload["error_tag"], "APPROVAL_CALLBACK_FAILED")
            tool_failed = next(event for event in runtime.events.list() if event.type == "tool.failed")
            self.assertEqual(tool_failed.payload["error_tag"], "APPROVAL_CALLBACK_FAILED")
            self.assertNotIn("approval secret", str(failed.payload))
            self.assertEqual(runtime.task.agent_state, "ready")
            self.assertIsNone(runtime.task.pending_tool)
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "ready")
            recovered.stop()

    def test_rejected_tool_ready_persistence_failure_keeps_previous_state(self):
        class FailingStore:
            def append(self, event):
                if event.type == "agent.node" and event.payload.get("node") == "ready":
                    raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("ready persistence")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.run_tool("read", path=3)
            self.assertEqual(runtime.task.agent_state, "ready")

    def test_schema_failure_replays_ready_state(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("schema failure")
            result = runtime.run_tool("read", path=3)
            self.assertFalse(result.ok)
            self.assertEqual(runtime.task.agent_state, "ready")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "ready")
            recovered.stop()

    def test_tool_result_clears_pending_tool(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("tool cleanup")
            runtime.run_tool("read", path="missing.txt")
            self.assertIsNone(runtime.task.pending_tool)
            runtime.run_tool("unknown")
            self.assertIsNone(runtime.task.pending_tool)

    def test_invalid_tool_arguments_record_failure_without_arguments(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("invalid args")
            runtime.execute_tool_calls([{"id": "call_bad", "function": {"name": "read", "arguments": "not-json-secret"}}])
            failed = [event for event in runtime.events.list() if event.type == "tool.failed"][-1]
            self.assertEqual(failed.payload["error_tag"], "INVALID_TOOL_ARGUMENTS")
            self.assertNotIn("not-json-secret", str(failed.payload))
            self.assertIsNone(runtime.task.pending_tool)
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertIsNone(recovered.task.pending_tool)
            recovered.stop()

    def test_tool_exception_records_ready_state_before_propagating(self):
        class BrokenTools:
            def read(self, **kwargs):
                raise RuntimeError("tool exploded")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("tool exception")
            runtime.tools.read = BrokenTools().read
            with self.assertRaisesRegex(RuntimeError, "tool exploded"):
                runtime.execute_tool_calls([{"id": "call_1", "function": {"name": "read", "arguments": "{\"path\":\"missing.txt\"}"}}])
            failed = next(event for event in runtime.events.list() if event.type == "tool.failed")
            self.assertEqual(failed.payload["error_type"], "RuntimeError")
            self.assertEqual(failed.payload["error_tag"], "TOOL_EXECUTION_FAILED")
            self.assertNotIn("tool exploded", str(failed.payload))
            self.assertEqual(runtime.task.agent_state, "ready")
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "ready")
            recovered.stop()

    def test_tool_batch_replays_ready_agent_state(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("tool ready")
            runtime.execute_tool_calls([{"id": "call_1", "function": {"name": "read", "arguments": '{"path":"missing.txt"}'}}])
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "ready")
            recovered.stop()

    def test_model_tool_loop_returns_final_text_and_records_facts(self):
        with TemporaryDirectory() as directory:
            Path(directory, "hello.txt").write_text("hello\n", encoding="utf-8")
            provider = FakeProvider()
            runtime = Runtime(directory, "auto", provider=provider)
            runtime.create_task("inspect hello.txt")
            output = runtime.run_model_turn()
            self.assertEqual(output, "The file was inspected.")
            self.assertEqual(provider.calls, 2)
            self.assertEqual(runtime.task.agent_state, "ready")
            event_types = [event.type for event in runtime.events.list()]
            self.assertIn("model.tool_call", event_types)
            self.assertIn("tool.completed", event_types)
            self.assertIn("model.completed", event_types)

    def test_response_failure_summary_counts_distinct_tool_calls(self):
        class BrokenChunkProvider:
            def stream(self, messages, tools=None):
                yield {"choices": [{"delta": {"content": "partial", "tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read", "arguments": "{"}}]}}]}
                yield None

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=BrokenChunkProvider())
            runtime.create_task("partial response")
            with self.assertRaises(AttributeError):
                runtime.run_model_turn()
            failed = next(event for event in runtime.events.list() if event.type == "response.failed")
            self.assertEqual(runtime.task.response_error["error_type"], "AttributeError")
            self.assertEqual(failed.payload["error_type"], "AttributeError")
            self.assertEqual(failed.payload["error_tag"], "MALFORMED_RESPONSE")
            self.assertEqual(failed.payload["summary"]["content_length"], 7)
            self.assertEqual(failed.payload["summary"]["tool_calls"], 1)
            self.assertEqual(failed.payload["summary"]["chunk_types"], ["dict", "NoneType"])

    def test_malformed_response_records_failure_and_ready(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto")
            runtime.create_task("malformed response")
            with self.assertRaises(AttributeError):
                runtime.parse_model_response([None])
            self.assertEqual(runtime.task.agent_state, "ready")
            self.assertIn("response.failed", [event.type for event in runtime.events.list()])
            failed = next(event for event in runtime.events.list() if event.type == "response.failed")
            self.assertEqual(failed.payload["summary"]["content_length"], 0)
            self.assertEqual(failed.payload["summary"]["tool_calls"], 0)
            self.assertEqual(failed.payload["summary"]["chunk_types"], ["NoneType"])
            self.assertNotIn("malformed response", str(failed.payload))
            self.assertNotIn("None has no attribute", str(failed.payload))

    def test_successful_response_replays_ready_agent_state(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("ready replay")
            runtime.parse_model_response([{"choices": [{"delta": {"content": "ok"}}]}])
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "ready")
            recovered.stop()

    def test_successful_response_clears_previous_response_error(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("response recovery")
            with self.assertRaises(AttributeError):
                runtime.parse_model_response([None])
            runtime.parse_model_response([{"choices": [{"delta": {"content": "ok"}}]}])
            self.assertIsNone(runtime.task.response_error)
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertIsNone(recovered.task.response_error)
            recovered.stop()

    def test_successful_model_turn_clears_previous_model_error(self):
        class Provider:
            def __init__(self):
                self.calls = 0
            def stream(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary")
                yield {"choices": [{"delta": {"content": "recovered"}}]}

        with TemporaryDirectory() as directory:
            provider = Provider()
            runtime = Runtime(directory, "auto", provider=provider, state_dir=directory)
            runtime.create_task("recover model")
            with self.assertRaises(RuntimeError):
                next(runtime.request_model())
            runtime.run_model_turn()
            self.assertIsNone(runtime.task.model_error)
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertIsNone(recovered.task.model_error)
            recovered.stop()

    def test_model_failure_error_replays(self):
        class BrokenProvider:
            def stream(self, messages, tools=None):
                raise RuntimeError("secret provider response")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=BrokenProvider(), state_dir=directory)
            runtime.create_task("model replay")
            with self.assertRaises(RuntimeError):
                runtime.request_model()
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.model_error["error_type"], "RuntimeError")
            self.assertNotIn("secret provider response", str(recovered.task.model_error))
            recovered.stop()

    def test_malformed_response_error_replays(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("response replay")
            with self.assertRaises(AttributeError):
                runtime.parse_model_response([None])
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.response_error["error_tag"], "MALFORMED_RESPONSE")
            self.assertEqual(recovered.task.agent_state, "ready")
            recovered.stop()

    def test_provider_error_tag_replays_without_response_body(self):
        class BrokenProvider:
            def stream(self, messages, tools=None):
                raise ProviderError("PROVIDER_AUTH_FAILED", cause=RuntimeError("secret body"))

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=BrokenProvider(), state_dir=directory)
            runtime.create_task("provider tag")
            with self.assertRaises(ProviderError):
                runtime.request_model()
            runtime.close()
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.model_error["error_tag"], "PROVIDER_AUTH_FAILED")
            self.assertNotIn("secret body", str(recovered.task.model_error))
            recovered.stop()

    def test_provider_failure_records_model_failed_and_ready(self):
        class BrokenProvider:
            def stream(self, messages, tools=None):
                raise RuntimeError("provider down")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=BrokenProvider())
            runtime.create_task("provider failure")
            with self.assertRaisesRegex(RuntimeError, "provider down"):
                runtime.request_model()
            self.assertEqual(runtime.task.agent_state, "ready")
            self.assertEqual(runtime.task.model_error["error_tag"], "MODEL_REQUEST_FAILED")
            self.assertIn("model.failed", [event.type for event in runtime.events.list()])
            self.assertEqual(runtime.events.list()[-1].payload["node"], "ready")

    def test_model_node_failure_does_not_change_agent_state(self):
        class FailingStore:
            def append(self, event):
                if event.type == "agent.node":
                    raise OSError("disk full")

        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=PlanProvider())
            runtime.create_task("model node atomic")
            runtime.events._durable = FailingStore()
            with self.assertRaises(OSError):
                runtime.request_model()
            self.assertEqual(runtime.task.agent_state, "ready")

    def test_model_nodes_can_be_called_independently(self):
        with TemporaryDirectory() as directory:
            Path(directory, "hello.txt").write_text("hello\n", encoding="utf-8")
            runtime = Runtime(directory, "auto", provider=FakeProvider())
            runtime.create_task("inspect")
            content, calls = runtime.parse_model_response(runtime.request_model())
            self.assertEqual(content, "")
            self.assertEqual(calls[0]["function"]["name"], "read")
            runtime.task.messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            runtime.execute_tool_calls(calls)
            self.assertIn("tools.executing", [event.payload.get("node") for event in runtime.events.list() if event.type == "agent.node"])

    def test_pause_stops_before_next_provider_request(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", provider=FakeProvider())
            runtime.create_task("pause")
            runtime.pause()
            with self.assertRaisesRegex(RuntimeError, "TASK_NOT_RUNNING"):
                runtime.run_model_turn()
            self.assertEqual(runtime.provider.calls, 0)

    def test_tool_schemas_are_structured(self):
        schemas = tool_schemas()
        self.assertEqual({item["function"]["name"] for item in schemas}, {"explore", "read", "edit", "exec"})
        for item in schemas:
            self.assertEqual(item["type"], "function")
            self.assertEqual(item["function"]["parameters"]["type"], "object")


if __name__ == "__main__":
    unittest.main()
