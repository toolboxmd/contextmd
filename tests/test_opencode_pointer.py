"""Run the plugin against OpenCode's pinned, configured root SDK transport."""

import subprocess
import unittest
from pathlib import Path


class OpenCodePointerTest(unittest.TestCase):
    def test_persistence_receipts_and_native_replay(self):
        root = Path(__file__).resolve().parents[1]
        done = subprocess.run(
            ["node", "--test", "tests/opencode-sdk/pointer.test.mjs"],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
