"""The configured index path is a single, bounded native-context instruction."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from contextmd.serve import config


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "contextmd-serve"


class GreatLibraryConfigTest(unittest.TestCase):
    def test_path_corpus(self):
        cases = json.loads((ROOT / "tests" / "great-library-path-corpus.json").read_text())
        for case in cases:
            with self.subTest(case=case["name"]):
                expand = lambda value: case.get("expanded", value)
                with patch("contextmd.serve.config.os.path.expanduser", side_effect=expand):
                    if case["valid"]:
                        self.assertEqual(config.great_library_path(case["value"]),
                                         case.get("expanded", case["value"]))
                    else:
                        with self.assertRaisesRegex(ValueError, "great_library"):
                            config.great_library_path(case["value"])

    def test_absent_key_is_disabled_and_present_null_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "serving.json"
            self.assertIsNone(config.load(file).great_library)
            file.write_text('{}')
            self.assertIsNone(config.load(file).great_library)
            file.write_text('{"great_library": null}')
            with self.assertRaisesRegex(ValueError, "great_library"):
                config.load(file)

    def test_config_preserves_other_registered_libraries(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "serving.json"
            file.write_text(json.dumps({"great_library": "/tmp/knowledge index.md",
                                        "exact_keys": ["/tmp/exact"], "cue": ["/tmp/cue"]}))
            loaded = config.load(file)
            self.assertEqual(loaded.great_library, "/tmp/knowledge index.md")
            self.assertEqual(loaded.exact_keys, ["/tmp/exact"])
            self.assertEqual(loaded.cue, ["/tmp/cue"])


class GreatLibraryCheckTest(unittest.TestCase):
    def run_check(self, file):
        env = {**os.environ, "CONTEXTMD_SERVING_CONFIG": str(file),
               "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(BIN), "check"], env=env,
                                capture_output=True, text=True, timeout=15)
        return result.returncode, json.loads(result.stdout)

    def test_regular_index_is_reported_and_missing_or_directory_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = root / "README.md"
            file = root / "serving.json"
            file.write_text(json.dumps({"great_library": str(index), "exact_keys": [], "cue": []}))
            code, report = self.run_check(file)
            self.assertEqual(code, 1)
            self.assertFalse(report["great_library"]["exists"])
            index.mkdir()
            code, report = self.run_check(file)
            self.assertEqual(code, 1)
            self.assertFalse(report["great_library"]["exists"])
            index.rmdir()
            index.write_text("# Great Library\n")
            code, report = self.run_check(file)
            self.assertEqual(code, 0)
            self.assertEqual(report["great_library"], {"configured": True, "path": str(index),
                                                       "exists": True, "owner": None})

    def test_invalid_key_exits_one_with_its_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "serving.json"
            file.write_text('{"great_library": "relative/README.md"}')
            code, report = self.run_check(file)
            self.assertEqual(code, 1)
            self.assertIn("great_library", report["errors"][0])

    def test_absent_pointer_does_not_require_an_index(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "serving.json"
            file.write_text('{"exact_keys": [], "cue": []}')
            code, report = self.run_check(file)
            self.assertEqual(code, 0)
            self.assertEqual(report["great_library"], {"configured": False, "path": None,
                                                       "exists": False, "owner": None})


if __name__ == "__main__":
    unittest.main()
