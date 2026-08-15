import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fun.provider import tool_schemas
from fun.runtime import Runtime


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
    def test_model_plan_proposal_replaces_runtime_plan(self):
        for provider in (PlanProvider(), PlanProvider(in_delta=True)):
            with TemporaryDirectory() as directory:
                runtime = Runtime(directory, "auto", provider=provider)
                runtime.create_task("plan this")
                runtime.run_model_turn()
                self.assertEqual(runtime.task.plan, ["inspect", "verify"])
                self.assertIn("plan.replaced", [event.type for event in runtime.events.list()])

    def test_model_tool_loop_returns_final_text_and_records_facts(self):
        with TemporaryDirectory() as directory:
            Path(directory, "hello.txt").write_text("hello\n", encoding="utf-8")
            provider = FakeProvider()
            runtime = Runtime(directory, "auto", provider=provider)
            runtime.create_task("inspect hello.txt")
            output = runtime.run_model_turn()
            self.assertEqual(output, "The file was inspected.")
            self.assertEqual(provider.calls, 2)
            self.assertEqual(runtime.task.agent_state, "response.parsed")
            event_types = [event.type for event in runtime.events.list()]
            self.assertIn("model.tool_call", event_types)
            self.assertIn("tool.completed", event_types)
            self.assertIn("model.completed", event_types)

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
