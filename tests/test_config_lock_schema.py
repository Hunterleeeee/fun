import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from fun.config import FunConfig, _keychain_set
from fun.lock import WorkspaceLock, WorkspaceLockError
from fun.schema import SchemaError, validate_tool_arguments


class ConfigLockSchemaTests(unittest.TestCase):
    def test_config_round_trip_is_private(self):
        # Patched: unpatched, this writes "secret" into the developer's real
        # macOS login keychain.  It is green on Linux only because
        # shutil.which("security") is None there — i.e. green because CI cannot
        # reach the code it claims to exercise.
        with patch("fun.config._keychain_set", return_value=True), patch("fun.config._keychain_get", return_value="secret"), TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = FunConfig("https://example.test", "secret", "model-x")
            config.save(path)
            loaded = FunConfig.load(path)
            self.assertEqual(loaded.model, "model-x")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            config.telemetry = True
            config.telemetry_endpoint = "http://private.test/telemetry"
            config.save(path)
            loaded = FunConfig.load(path)
            self.assertTrue(loaded.telemetry)
            self.assertEqual(loaded.telemetry_endpoint, "http://private.test/telemetry")

    def test_config_save_persists_approval_mode(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = FunConfig(approval="ask")
            config.save(path)
            self.assertEqual(FunConfig.load(path).approval, "ask")

    def test_missing_keychain_binding_clears_provider_ready_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"base_url":"https://example.test/v1","model":"m","api_key_store":"macos-keychain"}\n', encoding="utf-8")
            with patch("fun.config._keychain_get", return_value=""):
                loaded = FunConfig.load(path)
            self.assertFalse(loaded.ready())
            self.assertTrue(loaded.keychain_unreadable)

    def test_an_unreadable_keychain_does_not_destroy_the_saved_endpoint(self):
        """A locked Keychain is "cannot read now", not "never configured"."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"base_url":"https://example.test/v1","model":"m","api_key_store":"macos-keychain"}\n', encoding="utf-8")
            with patch("fun.config._keychain_get", return_value=""):
                loaded = FunConfig.load(path)
                loaded.theme = "ember"      # any unrelated setting change
                loaded.save(path)
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["base_url"], "https://example.test/v1")
            self.assertEqual(on_disk["model"], "m")
            self.assertEqual(on_disk["api_key_store"], "macos-keychain")

    def test_an_environment_key_is_never_promoted_into_the_keychain(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch.dict(os.environ, {"FUN_API_KEY": "sk-shared-ci"}, clear=False):
                with patch("fun.config._keychain_get", return_value=""), patch("fun.config._keychain_set") as setter:
                    loaded = FunConfig.load(path)
                    self.assertTrue(loaded.from_env)
                    written, durable = loaded.save(path)
                    setter.assert_not_called()
            self.assertFalse(written)
            self.assertFalse(durable)
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("api_key", on_disk)
            self.assertEqual(on_disk.get("api_key_env"), "FUN_API_KEY")

    def test_save_reports_where_the_key_actually_reached_durable_storage(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = FunConfig(base_url="https://x/v1", model="m", api_key="sk-abc")
            with patch("fun.config._keychain_set", return_value=False), patch("fun.config._keychain_get", return_value=""):
                # A failed keychain write is not a failed save: the key falls
                # back to the 0600 config file, so it is still durable.
                self.assertEqual(config.save(path), (True, True))
                self.assertEqual(config.storage(path), "config-file")
            with patch("fun.config._keychain_set", return_value=True), patch("fun.config._keychain_get", return_value="sk-abc"):
                self.assertEqual(config.save(path), (True, True))
                self.assertEqual(config.storage(path), "keychain")

    def test_a_key_survives_a_restart_when_the_keychain_write_fails(self):
        # The regression this pins: the key was configured, the keychain write
        # silently did nothing, save() recorded only "use FUN_API_KEY", and the
        # next morning the app asked for the key again.
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FUN_API_KEY", None)
                with patch("fun.config._keychain_set", return_value=False), patch("fun.config._keychain_get", return_value=""):
                    FunConfig(base_url="https://x/v1", model="m", api_key="sk-real").save(path)
                    reloaded = FunConfig.load(path)
            self.assertEqual(reloaded.api_key, "sk-real")
            self.assertTrue(reloaded.ready())
            self.assertFalse(reloaded.keychain_unreadable)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_storage_names_every_place_the_key_can_live(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = FunConfig(base_url="https://x/v1", model="m")
            self.assertEqual(config.storage(path), "none")
            config.save(path)
            self.assertEqual(config.storage(path), "none")
            with patch.dict(os.environ, {"FUN_API_KEY": "sk-env"}, clear=False):
                with patch("fun.config._keychain_get", return_value=""), patch("fun.config._keychain_set", return_value=False):
                    FunConfig.load(path).save(path)
            self.assertEqual(config.storage(path), "environment")

    def test_the_save_message_names_the_place_rather_than_claiming_success(self):
        from fun.i18n import key_location_message, saved_message

        for locale in ("en-US", "zh-CN"):
            self.assertNotEqual(saved_message(locale, "config-file", "/tmp/c.json"), saved_message(locale, "keychain", "/tmp/c.json"))
            self.assertIn("/tmp/c.json", saved_message(locale, "config-file", "/tmp/c.json"))
            # An unknown location must degrade to the honest warning, never to
            # the reassuring one.
            self.assertEqual(saved_message(locale, "???", "/tmp/c.json"), saved_message(locale, "none", "/tmp/c.json"))
            self.assertEqual(key_location_message(locale, "???", "/tmp/c.json"), key_location_message(locale, "none", "/tmp/c.json"))

    def test_logout_reports_failure_instead_of_claiming_success(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = FunConfig(base_url="https://x/v1", model="m", api_key="sk-abc")
            with patch("fun.config._keychain_get", return_value="sk-abc"), patch("fun.config._keychain_delete", return_value=False):
                self.assertFalse(config.clear_credentials(path))
            with patch("fun.config._keychain_get", return_value=""), patch("fun.config._keychain_delete", return_value=True):
                self.assertTrue(FunConfig(api_key="sk-abc").clear_credentials(path))

    def test_the_keychain_write_can_never_prompt_on_the_terminal(self):
        # A bare ``-w`` makes ``security`` prompt on /dev/tty, which printed
        # "password data for new item:" across the running UI, stored nothing,
        # and ate the user's next keystrokes.  The value form is the only
        # usable one, and stdin is closed so no sub-command can block on input.
        with patch("fun.config.shutil.which", return_value="/usr/bin/security"), patch("fun.config.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch("fun.config._keychain_get", return_value="sk-secret-value"):
                self.assertTrue(_keychain_set("sk-secret-value"))
            argv = run.call_args.args[0]
            self.assertEqual(argv[-2:], ["-w", "sk-secret-value"])
            self.assertIsNone(run.call_args.kwargs.get("input"))
            self.assertEqual(run.call_args.kwargs.get("stdin"), subprocess.DEVNULL)

    def test_a_write_that_stores_nothing_is_reported_as_a_failure(self):
        # Exit code 0 is not proof: the read-back is.
        with patch("fun.config.shutil.which", return_value="/usr/bin/security"), patch("fun.config.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch("fun.config._keychain_get", return_value=""):
                self.assertFalse(_keychain_set("sk-secret-value"))

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

    def test_stale_lock_is_reclaimed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.lock"
            path.write_text('{"pid": 999999999, "workspace": "x"}', encoding="utf-8")
            lock = WorkspaceLock(directory, directory)
            lock.acquire()
            self.assertTrue(lock.held)
            lock.release()

    def test_tool_schema_rejects_unknown_and_wrong_types(self):
        with self.assertRaisesRegex(SchemaError, "INVALID_ARGUMENTS"):
            validate_tool_arguments("read", {"path": "a", "unexpected": True})
        with self.assertRaisesRegex(SchemaError, "INVALID_ARGUMENTS"):
            validate_tool_arguments("exec", {"command": "echo", "timeout": "fast"})


if __name__ == "__main__":
    unittest.main()


class ProtectedPathTests(unittest.TestCase):
    def test_default_patterns_block_secrets_and_git_internals(self):
        from fun.policy import Policy, PolicyError, WorkspaceGuard

        with TemporaryDirectory() as directory:
            guard = WorkspaceGuard(directory)
            policy = Policy()
            for name in (".env", ".env.local", ".git/config", "certs/server.pem", "certs/server.key"):
                target = Path(directory) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x", encoding="utf-8")
                with self.assertRaises(PolicyError, msg=name):
                    guard.check_name(guard.resolve(name), policy)

    def test_ordinary_paths_are_allowed(self):
        from fun.policy import Policy, WorkspaceGuard

        with TemporaryDirectory() as directory:
            guard = WorkspaceGuard(directory)
            target = Path(directory) / "src" / "environment.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
            guard.check_name(guard.resolve("src/environment.py"), Policy())

    def test_the_policy_owns_the_pattern_list(self):
        """A caller can widen protection without editing the guard."""
        from fun.policy import Policy, PolicyError, WorkspaceGuard

        with TemporaryDirectory() as directory:
            guard = WorkspaceGuard(directory)
            target = Path(directory) / "vendor"
            target.mkdir()
            guard.check_name(guard.resolve("vendor"), Policy())
            with self.assertRaises(PolicyError):
                guard.check_name(guard.resolve("vendor"), Policy(protected_names=("vendor",)))


class InitialPlanTests(unittest.TestCase):
    def test_short_greeting_gets_the_lightweight_plan(self):
        from fun.runtime import Runtime

        self.assertEqual(Runtime._initial_plan("你好啊"), ["understand the request", "respond"])
        self.assertEqual(Runtime._initial_plan("thanks"), ["understand the request", "respond"])

    def test_short_mutating_goals_are_not_mistaken_for_chatter(self):
        """'fix login' is nine characters but is still real work."""
        from fun.runtime import Runtime

        self.assertIn("apply a minimal change", Runtime._initial_plan("fix login"))
        self.assertIn("apply a minimal change", Runtime._initial_plan("修复登录"))

    def test_latin_verbs_match_on_word_boundaries(self):
        from fun.runtime import Runtime

        self.assertNotIn("apply a minimal change", Runtime._initial_plan("describe the fixture loading order in this repo"))

    def test_questions_get_the_investigation_plan(self):
        from fun.runtime import Runtime

        self.assertIn("report verified findings", Runtime._initial_plan("what does the runtime do?"))
        self.assertIn("report verified findings", Runtime._initial_plan("这个项目是怎么组织的"))

    def test_chinese_sentences_are_measured_by_display_width(self):
        """Eight CJK characters are sixteen columns, not a one-word greeting."""
        from fun.runtime import Runtime

        self.assertNotEqual(Runtime._initial_plan("请帮我看看这个项目的整体结构"), ["understand the request", "respond"])
