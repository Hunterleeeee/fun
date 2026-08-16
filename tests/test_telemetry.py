import os
import tempfile
import unittest
from unittest import mock

from fun.runtime import Runtime
from fun.cli import build_parser, resolve_command_prefix
from fun.i18n import t
from fun.telemetry import TelemetryClient, event_payload, install_id, load_or_create_install_id, model_family, valid_endpoint


class TelemetryTests(unittest.TestCase):
    def test_payload_is_匿名_and_allowlisted(self):
        payload = event_payload(event="task.finished", install=install_id("local-secret"), model="provider/gpt-4o:paid", input_tokens=10, output_tokens=4, total_tokens=14, status="completed")
        self.assertEqual(payload["model_family"], "gpt-4o")
        self.assertEqual(payload["total_tokens"], 14)
        self.assertNotIn("local-secret", str(payload))
        self.assertEqual(set(payload), set(payload) & {"event", "install_id", "fun_version", "python_version", "os", "model_family", "input_tokens", "output_tokens", "total_tokens", "tool_calls", "status"})

    def test_endpoint_must_be_http_or_https(self):
        self.assertTrue(valid_endpoint("https://private.example/telemetry"))
        self.assertTrue(valid_endpoint("http://127.0.0.1:9000/events"))
        self.assertFalse(valid_endpoint("file:///tmp/events"))
        self.assertFalse(valid_endpoint("private.example/events"))
        self.assertFalse(TelemetryClient(enabled=True, endpoint="file:///tmp/events").enabled)

    def test_slash_command_prefix_resolution(self):
        commands = {"/model", "/status", "/stop"}
        self.assertEqual(resolve_command_prefix("/model", commands), ("/model", []))
        self.assertEqual(resolve_command_prefix("/mod", commands), ("/model", []))
        self.assertEqual(resolve_command_prefix("/st", commands), (None, ["/status", "/stop"]))
        self.assertEqual(resolve_command_prefix("hello", commands), ("hello", []))
        self.assertEqual(resolve_command_prefix("/unknown", commands), (None, []))

    def test_command_menu_locales_are_not_english_only(self):
        self.assertIn("命令", t("zh-CN", "commands_title"))
        self.assertIn("配置", t("zh-CN", "cmd_config"))
        self.assertIn("Commands", t("en-US", "commands_title"))

    def test_cli_approval_can_use_saved_config(self):
        self.assertIsNone(build_parser().parse_args([]).approval)
        self.assertEqual(build_parser().parse_args(["--approval", "ask"]).approval, "ask")

    def test_cli_exposes_explicit_telemetry_switches(self):
        self.assertTrue(build_parser().parse_args(["--telemetry"]).telemetry)
        self.assertFalse(build_parser().parse_args(["--no-telemetry"]).telemetry)
        self.assertIsNone(build_parser().parse_args([]).telemetry)

    def test_install_id_is_stable_and_private(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            first = load_or_create_install_id(directory)
            second = load_or_create_install_id(directory)
            self.assertEqual(first, second)
            path = Path(directory) / "telemetry_id"
            self.assertEqual(path.read_text(encoding="utf-8").strip(), first)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_runtime_does_not_send_without_client(self):
        with mock.patch.object(TelemetryClient, "send") as send:
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                runtime = Runtime(directory)
                runtime.create_task("private")
                runtime.complete("done")
            send.assert_not_called()

    def test_runtime_reports_coarse_model_family(self):
        telemetry = TelemetryClient(enabled=True, endpoint="http://127.0.0.1:1/telemetry", install="test")
        with mock.patch.object(telemetry, "send", return_value=True) as send:
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                runtime = Runtime(directory, telemetry=telemetry, model="private-provider/gpt-4o:secret")
                runtime.create_task("model metric")
                runtime.complete("done")
            self.assertEqual(send.call_args.args[0]["model_family"], "gpt-4o")
            self.assertNotIn("private-provider", str(send.call_args.args[0]))

    def test_terminal_lock_releases_when_telemetry_raises(self):
        class BrokenTelemetry:
            install = "anon"
            def send(self, payload):
                raise RuntimeError("network")

        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", telemetry=BrokenTelemetry())
            runtime.create_task("stop")
            runtime.stop()
            self.assertFalse(runtime.lock.held)

    def test_telemetry_retries_after_transient_failure(self):
        class FlakyTelemetry:
            install = "anon"
            def __init__(self):
                self.calls = 0
            def send(self, payload):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("network")
                return True

        telemetry = FlakyTelemetry()
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(directory, telemetry=telemetry)
            runtime.create_task("retry telemetry")
            runtime.stop()
            runtime._send_telemetry("stopped")
            self.assertEqual(telemetry.calls, 2)
            self.assertTrue(runtime._telemetry_sent)

    def test_terminal_paths_close_store_when_telemetry_fails(self):
        telemetry = TelemetryClient(enabled=True, endpoint="http://127.0.0.1:1/telemetry", install="test")
        with mock.patch.object(telemetry, "send", side_effect=RuntimeError("network")):
            with tempfile.TemporaryDirectory() as directory:
                stopped = Runtime(directory, state_dir=directory, telemetry=telemetry)
                stopped.create_task("stop telemetry failure")
                durable = stopped.events._durable
                stopped.stop()
                with self.assertRaisesRegex(Exception, "closed"):
                    durable.list()

                failed = Runtime(directory, state_dir=directory, telemetry=telemetry)
                failed.create_task("fail telemetry failure")
                durable = failed.events._durable
                failed.fail("broken")
                with self.assertRaisesRegex(Exception, "closed"):
                    durable.list()

    def test_runtime_reports_stopped_once(self):
        telemetry = TelemetryClient(enabled=True, endpoint="http://127.0.0.1:1/telemetry", install="test")
        with mock.patch.object(telemetry, "send", return_value=True) as send:
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                runtime = Runtime(directory, telemetry=telemetry)
                runtime.create_task("stop me")
                runtime.stop()
                runtime.stop()
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0]["status"], "stopped")
            self.assertIsInstance(send.call_args.args[0]["duration_ms"], int)
            self.assertNotIn("started_at", send.call_args.args[0])

    def test_runtime_reports_failure_once(self):
        telemetry = TelemetryClient(enabled=True, endpoint="http://127.0.0.1:1/telemetry", install="test")
        with mock.patch.object(telemetry, "send", return_value=True) as send:
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                runtime = Runtime(directory, telemetry=telemetry)
                runtime.create_task("fail me")
                runtime.fail("broken")
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0]["status"], "failed")

    def test_disabled_client_does_not_create_install_file(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse((Path(directory) / "telemetry_id").exists())
            self.assertFalse(TelemetryClient().send(event_payload(event="x", install="local")))
            self.assertFalse((Path(directory) / "telemetry_id").exists())

    def test_sender_is_disabled_without_explicit_opt_in_and_endpoint(self):
        payload = event_payload(event="task.finished", install="local")
        self.assertFalse(TelemetryClient().send(payload))
        self.assertFalse(TelemetryClient(enabled=True).send(payload))

    def test_sender_failures_are_isolated(self):
        payload = event_payload(event="task.finished", install="local")
        client = TelemetryClient(enabled=True, endpoint="http://127.0.0.1:1/telemetry")
        self.assertFalse(client.send(payload))

    def test_model_family_is_coarse(self):
        self.assertEqual(model_family("openai/gpt-4o:latest"), "gpt-4o")
        self.assertEqual(model_family(""), "unknown")


if __name__ == "__main__":
    unittest.main()
