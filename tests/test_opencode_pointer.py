"""Run the plugin against OpenCode 1.x's pinned root SDK transport and a stand-in OpenCode 2.x context."""

import subprocess
import unittest
from pathlib import Path


class OpenCodePointerTest(unittest.TestCase):
    def test_persistence_receipts_and_native_replay(self):
        root = Path(__file__).resolve().parents[1]
        done = subprocess.run(
            ["node", "--test", "tests/opencode-sdk/pointer.test.mjs", "tests/opencode-sdk/opencode2.test.mjs"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
