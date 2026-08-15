import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fun.provider import tool_schemas
from fun.runtime import Runtime


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

    def test_approval_failure_replay_projects_ready_without_tool_fact(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("approval replay fact")
            runtime.emit("approval.pending", runtime.task.id, call_id="c1", name="exec", risk="medium", arguments={})
            runtime.emit("approval.failed", runtime.task.id, call_id="c1", name="exec", error_type="RuntimeError", error_tag="APPROVAL_CALLBACK_FAILED")
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
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.agent_state, "ready")
            recovered.stop()

    def test_tool_batch_replays_ready_agent_state(self):
        with TemporaryDirectory() as directory:
            runtime = Runtime(directory, "auto", state_dir=directory)
            runtime.create_task("tool ready")
            runtime.execute_tool_calls([{"id": "call_1", "function": {"name": "read", "arguments": '{"path":"missing.txt"}'}}])
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
            recovered = Runtime.recover(directory, directory, runtime.session_id)
            self.assertEqual(recovered.task.response_error["error_tag"], "MALFORMED_RESPONSE")
            self.assertEqual(recovered.task.agent_state, "ready")
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
