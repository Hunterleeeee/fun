import unittest

from fun.telemetry import TelemetryClient, event_payload, install_id, model_family


class TelemetryTests(unittest.TestCase):
    def test_payload_is_匿名_and_allowlisted(self):
        payload = event_payload(event="task.finished", install=install_id("local-secret"), model="provider/gpt-4o:paid", input_tokens=10, output_tokens=4, total_tokens=14, status="completed")
        self.assertEqual(payload["model_family"], "gpt-4o")
        self.assertEqual(payload["total_tokens"], 14)
        self.assertNotIn("local-secret", str(payload))
        self.assertEqual(set(payload), set(payload) & {"event", "install_id", "fun_version", "python_version", "os", "model_family", "input_tokens", "output_tokens", "total_tokens", "tool_calls", "status"})

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
