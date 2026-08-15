import unittest
from unittest import mock

from fun.runtime import Runtime
from fun.telemetry import TelemetryClient, event_payload, install_id, model_family


class TelemetryTests(unittest.TestCase):
    def test_payload_is_匿名_and_allowlisted(self):
        payload = event_payload(event="task.finished", install=install_id("local-secret"), model="provider/gpt-4o:paid", input_tokens=10, output_tokens=4, total_tokens=14, status="completed")
        self.assertEqual(payload["model_family"], "gpt-4o")
        self.assertEqual(payload["total_tokens"], 14)
        self.assertNotIn("local-secret", str(payload))
        self.assertEqual(set(payload), set(payload) & {"event", "install_id", "fun_version", "python_version", "os", "model_family", "input_tokens", "output_tokens", "total_tokens", "tool_calls", "status"})

    def test_runtime_does_not_send_without_client(self):
        with mock.patch.object(TelemetryClient, "send") as send:
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                runtime = Runtime(directory)
                runtime.create_task("private")
                runtime.complete("done")
            send.assert_not_called()

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
