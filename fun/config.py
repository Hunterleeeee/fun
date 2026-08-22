from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _keychain_get() -> str:
    if shutil.which("security") is None:
        return ""
    try:
        result = subprocess.run(["security", "find-generic-password", "-a", "fun", "-s", "fun-api-key", "-w"], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _keychain_delete() -> bool:
    if shutil.which("security") is None:
        return False
    try:
        result = subprocess.run(["security", "delete-generic-password", "-a", "fun", "-s", "fun-api-key"], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=3)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _keychain_run(argv: list[str]) -> bool:
    """Run ``security`` with no way for it to reach the terminal.

    ``stdin=DEVNULL`` matters: any ``security`` sub-command that decides it
    needs input must fail instead of blocking, and nothing it prints may land
    on the screen — this program owns the terminal.
    """
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _keychain_set(value: str) -> bool:
    """Store the key, and confirm it can be read back.

    ``security add-generic-password`` has exactly two ways to take a password:
    ``-w <value>``, or a bare ``-w`` that prompts.  The prompt is *not* stdin —
    ``security`` opens ``/dev/tty`` directly — so piping the key into it wrote
    ``password data for new item:`` straight across this program's rendered
    screen, stored nothing, and swallowed the next keystrokes the user typed.
    That leaves the value form as the only usable one.  Its cost is one call's
    worth of exposure in the process argument table; on macOS a process's
    arguments are readable only by the same user (and root), which is the same
    boundary that already protects the 0600 config file, so the trade is
    acceptable and deliberate.  Either way the result is verified by reading it
    back rather than by trusting an exit code.
    """
    if shutil.which("security") is None:
        return False
    argv = ["security", "add-generic-password", "-a", "fun", "-s", "fun-api-key", "-U", "-w", value]
    return _keychain_run(argv) and _keychain_get() == value


@dataclass
class FunConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    #: Models the user picked in the picker, kept so ``/model`` can offer that
    #: shortlist without another network round trip.  The active one is
    #: ``model``; this is the set they said they care about.
    models: list[str] = field(default_factory=list)
    approval: str = "smart"
    locale: str = "en-US"
    telemetry: bool = False
    telemetry_endpoint: str = ""
    system_prompt: str = ""
    theme: str = "sky"
    # Not persisted.  True when the key came from FUN_API_KEY this run, and
    # therefore must not be written into durable storage on the user's behalf,
    # and True when the Keychain could not be read so the endpoint and model on
    # disk must not be overwritten with the blanks that produced.
    from_env: bool = field(default=False, repr=False, compare=False)
    keychain_unreadable: bool = field(default=False, repr=False, compare=False)
    keychain_backed: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def load(cls, path: str | Path) -> "FunConfig":
        target = Path(path).expanduser()
        if not target.exists():
            # Still honour the environment: a first run driven entirely by
            # FUN_API_KEY previously returned a blank config and reported the
            # provider as unconfigured.
            from_env = os.getenv("FUN_API_KEY") or ""
            return cls(api_key=from_env, from_env=bool(from_env))
        data = json.loads(target.read_text(encoding="utf-8"))
        allowed = {"base_url", "api_key", "model", "models", "approval", "locale", "telemetry", "telemetry_endpoint", "system_prompt", "theme"}
        values = {key: value for key, value in data.items() if key in allowed}
        # A hand-edited config can put anything here; a string would otherwise
        # be iterated character by character into the model picker.
        values["models"] = [str(item) for item in values.get("models") or [] if isinstance(item, str)]
        loaded = cls(**values)
        from_env = os.getenv("FUN_API_KEY") or ""
        stored = "" if from_env else _keychain_get()
        loaded.api_key = from_env or stored or loaded.api_key
        loaded.from_env = bool(from_env)
        loaded.keychain_backed = bool(stored and data.get("api_key_store") == "macos-keychain")
        if not loaded.api_key and data.get("api_key_store") == "macos-keychain":
            # The Keychain says a key is stored but we cannot read it — locked
            # login keychain, an SSH session, a denied prompt.  That is "cannot
            # read right now", not "never configured", so the endpoint and model
            # are kept in memory and marked unwritable rather than blanked and
            # then persisted as blanks by the next unrelated save.
            loaded.keychain_unreadable = True
        return loaded

    def storage(self, path: str | Path) -> str:
        """Where the saved key actually lives: keychain, config file or nowhere."""
        target = Path(path).expanduser()
        if not target.exists():
            return "none"
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "none"
        if data.get("api_key_store") == "macos-keychain":
            return "keychain"
        if data.get("api_key_store") == "config-file" and data.get("api_key"):
            return "config-file"
        if data.get("api_key_env"):
            return "environment"
        return "none"

    def save(self, path: str | Path) -> tuple[bool, bool]:
        """Persist the config.  Returns ``(key_written, durable)``.

        The return value exists so the caller can tell the user the truth: the
        message used to say "stored securely" unconditionally, including on
        every machine without a Keychain, where the key was in fact nowhere but
        this process.
        """
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        previous: dict[str, object] = {}
        if target.exists():
            try:
                previous = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = {}
        data = asdict(self)
        for transient in ("api_key_store", "api_key_env", "from_env", "keychain_unreadable", "keychain_backed"):
            data.pop(transient, None)
        if self.keychain_unreadable:
            # Keep whatever is already on disk for the fields we could not
            # verify, instead of overwriting them with what we failed to load.
            for field_name in ("base_url", "model"):
                if not data.get(field_name) and previous.get(field_name):
                    data[field_name] = previous[field_name]
            if previous.get("api_key_store"):
                data["api_key_store"] = previous["api_key_store"]
        key_written = False
        durable = False
        key = data.pop("api_key", "")
        if key and self.from_env:
            # A key that arrived in the environment is the caller's to manage.
            # Promoting it into the Keychain silently made a CI or shared key
            # permanent on the user's machine.
            data["api_key_env"] = "FUN_API_KEY"
        elif key and self.keychain_backed:
            data["api_key_store"] = "macos-keychain"
            durable = True
        elif key:
            key_written = True
            if _keychain_set(key):
                data["api_key_store"] = "macos-keychain"
                durable = True
            else:
                # The key has to end up *somewhere*.  Recording only
                # "use FUN_API_KEY next time" meant it lived in this process and
                # nowhere else, so the next launch asked for it again — which is
                # a worse outcome than a file only this user can read.
                data["api_key"] = key
                data["api_key_store"] = "config-file"
                durable = True
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return key_written, durable

    def clear_credentials(self, path: str | Path) -> bool:
        """Forget the credentials.  Returns whether the Keychain entry is gone.

        The result used to be discarded, so a locked Keychain reported "removed"
        while the key stayed on the machine indefinitely.
        """
        had_entry = bool(_keychain_get())
        deleted = _keychain_delete() or not had_entry
        self.api_key = ""
        self.base_url = ""
        self.model = ""
        self.keychain_unreadable = False
        self.keychain_backed = False
        self.from_env = False
        self.save(path)
        return deleted and not _keychain_get()

    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)
