import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextmd.capture import cites  # noqa: E402
from tests.fixtures import observer_db  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class CitesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("one\nNever wait on sleeps or polling.\nthree\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "first")
        self.first = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "tag", "v1.0.0")
        (self.repo / "b.txt").write_text("later\n")
        git(self.repo, "add", "b.txt")
        git(self.repo, "commit", "-q", "-m", "second")
        self.second = git(self.repo, "rev-parse", "HEAD")
        self.ao = observer_db(root / "observer.db", [
            {"thread": "T", "key": "codex:s", "native": "s", "events": [(7, "n7", 1.0)],
             "user_messages": [("um-1", "hello")]}])

    def tearDown(self):
        self.ao.close()
        self.tmp.cleanup()

    def check(self, cite):
        return cites.check_cite(cite, self.ao)

    def test_commit_exists_touches_path_and_is_in_version(self):
        ok = {"type": "commit", "repo": str(self.repo), "sha": self.first, "path": "a.txt", "in": "v1.0.0"}
        self.assertEqual(self.check(ok)["status"], "ok")
        self.assertIn("does not touch b.txt", self.check(dict(ok, path="b.txt"))["status"])
        self.assertIn("not in v1.0.0", self.check(dict(ok, sha=self.second, path="b.txt"))["status"])
        self.assertIn("no commit", self.check(dict(ok, sha="0" * 40))["status"])

    def test_line_holds_the_quoted_words(self):
        on_disk = {"type": "line", "file": str(self.repo / "a.txt"), "line": 2,
                   "quote": "never  wait on sleeps"}
        self.assertEqual(self.check(on_disk)["status"], "ok")
        self.assertIn("line 3 does not contain", self.check(dict(on_disk, line=3))["status"])
        at_rev = {"type": "line", "repo": str(self.repo), "rev": self.first, "path": "a.txt", "line": 2,
                  "quote": "Never wait on sleeps or polling."}
        self.assertEqual(self.check(at_rev)["status"], "ok")
        self.assertIn("not found", self.check(dict(at_rev, path="b.txt"))["status"])
        self.assertIn("not found", self.check(dict(on_disk, file=str(self.repo / "gone.txt")))["status"])

    def test_event_and_user_message_exist_in_the_ledger(self):
        self.assertEqual(self.check({"type": "event", "id": 7})["status"], "ok")
        self.assertEqual(self.check({"type": "event", "id": 7, "session_key": "codex:s"})["status"], "ok")
        self.assertIn("no event", self.check({"type": "event", "id": 8})["status"])
        self.assertIn("no event", self.check({"type": "event", "id": 7, "session_key": "codex:x"})["status"])
        self.assertEqual(self.check({"type": "user_message", "id": "um-1"})["status"], "ok")
        self.assertIn("no user message", self.check({"type": "user_message", "id": "um-2"})["status"])

    def test_other_cites_and_malformed_cites_are_not_passed(self):
        self.assertEqual(self.check({"type": "url", "ref": "https://example.com"})["status"], "unchecked")
        self.assertIn("malformed", self.check({"type": "commit", "repo": str(self.repo)})["status"])
        self.assertIn("malformed", self.check({"type": "line", "file": "x", "line": "two", "quote": "q"})["status"])

    def test_check_packets_marks_every_cite_in_place(self):
        packets = {"1": {"facts": {
            "fixed_later": {"text": "fixed by first", "cites": [
                {"type": "commit", "repo": str(self.repo), "sha": self.first}]},
            "owner": {"text": "unknown: not named", "cites": []},
            "recurrence": {"text": "none confirmed", "cites": [{"type": "event", "id": 99}]}}}}
        summary = cites.check_packets(packets, self.ao)
        facts = packets["1"]["facts"]
        self.assertEqual(facts["fixed_later"]["cites"][0]["check"], "ok")
        self.assertIn("no event", facts["recurrence"]["cites"][0]["check"])
        self.assertEqual(summary, {"ok": 1, "failed": 1, "unchecked": 0})

    def test_cli_checks_a_packets_file_in_place(self):
        import io
        import json
        from contextlib import redirect_stdout
        from contextmd import cli
        root = Path(self.tmp.name)
        cfg = root / "capture.json"
        cfg.write_text(json.dumps({"state_dir": str(root / "state"), "observer_db": str(root / "observer.db")}))
        packets = root / "packets.json"
        packets.write_text(json.dumps({"4": {"facts": {"owner": {"text": "x", "cites": [
            {"type": "user_message", "id": "um-1"}, {"type": "url", "ref": "https://example.com"}]}}}}))
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--config", str(cfg), "capture", "cites", str(packets)]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"ok": 1, "failed": 0, "unchecked": 1})
        checked = json.loads(packets.read_text())["4"]["facts"]["owner"]["cites"]
        self.assertEqual([c["check"] for c in checked], ["ok", "unchecked"])


if __name__ == "__main__":
    unittest.main()
