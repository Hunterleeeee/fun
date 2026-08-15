import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fun.provider import tool_schemas
from fun.runtime import Runtime


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
    def test_model_tool_loop_returns_final_text_and_records_facts(self):
        with TemporaryDirectory() as directory:
            Path(directory, "hello.txt").write_text("hello\n", encoding="utf-8")
            provider = FakeProvider()
            runtime = Runtime(directory, "auto", provider=provider)
            runtime.create_task("inspect hello.txt")
            output = runtime.run_model_turn()
            self.assertEqual(output, "The file was inspected.")
            self.assertEqual(provider.calls, 2)
            event_types = [event.type for event in runtime.events.list()]
            self.assertIn("model.tool_call", event_types)
            self.assertIn("tool.completed", event_types)
            self.assertIn("model.completed", event_types)

    def test_tool_schemas_are_structured(self):
        schemas = tool_schemas()
        self.assertEqual({item["function"]["name"] for item in schemas}, {"explore", "read", "edit", "exec"})
        for item in schemas:
            self.assertEqual(item["type"], "function")
            self.assertEqual(item["function"]["parameters"]["type"], "object")


if __name__ == "__main__":
    unittest.main()
