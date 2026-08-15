import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fun.config import FunConfig
from fun.lock import WorkspaceLock, WorkspaceLockError
from fun.schema import SchemaError, validate_tool_arguments


class ConfigLockSchemaTests(unittest.TestCase):
    def test_config_round_trip_is_private(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = FunConfig("https://example.test", "secret", "model-x")
            config.save(path)
            loaded = FunConfig.load(path)
            self.assertEqual(loaded.model, "model-x")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_workspace_lock_is_exclusive(self):
        with TemporaryDirectory() as directory:
            first = WorkspaceLock(directory, directory)
            second = WorkspaceLock(directory, directory)
            first.acquire()
            with self.assertRaises(WorkspaceLockError):
                second.acquire()
            first.release()
            second.acquire()
            second.release()

    def test_tool_schema_rejects_unknown_and_wrong_types(self):
        with self.assertRaisesRegex(SchemaError, "INVALID_ARGUMENTS"):
            validate_tool_arguments("read", {"path": "a", "unexpected": True})
        with self.assertRaisesRegex(SchemaError, "INVALID_ARGUMENTS"):
            validate_tool_arguments("exec", {"command": "echo", "timeout": "fast"})


if __name__ == "__main__":
    unittest.main()
