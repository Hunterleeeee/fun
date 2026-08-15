import unittest
from unittest import mock

from fun.runtime import Runtime
from fun.cli import build_parser
from fun.telemetry import TelemetryClient, event_payload, install_id, load_or_create_install_id, model_family


class TelemetryTests(unittest.TestCase):
    def test_payload_is_匿名_and_allowlisted(self):
        payload = event_payload(event="task.finished", install=install_id("local-secret"), model="provider/gpt-4o:paid", input_tokens=10, output_tokens=4, total_tokens=14, status="completed")
        self.assertEqual(payload["model_family"], "gpt-4o")
        self.assertEqual(payload["total_tokens"], 14)
        self.assertNotIn("local-secret", str(payload))
        self.assertEqual(set(payload), set(payload) & {"event", "install_id", "fun_version", "python_version", "os", "model_family", "input_tokens", "output_tokens", "total_tokens", "tool_calls", "status"})

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
