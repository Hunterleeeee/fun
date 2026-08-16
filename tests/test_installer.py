import os
import stat
import unittest
from pathlib import Path


class InstallerTests(unittest.TestCase):
    def test_installer_is_executable_and_has_expected_entrypoints(self):
        path = Path(__file__).parent.parent / "install.sh"
        mode = path.stat().st_mode
        if os.name != "nt":
            self.assertTrue(mode & stat.S_IXUSR)
        text = path.read_text(encoding="utf-8")
        self.assertIn("git+${REPO_URL}", text)
        self.assertIn("$BIN_DIR/fun", text)
        self.assertIn("Python 3.11", text)


if __name__ == "__main__":
    unittest.main()
