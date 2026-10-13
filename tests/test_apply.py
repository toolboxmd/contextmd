"""capture apply: one entry, one commit with receipts, add-only, resumable.

The store check is the real ``contextmd-serve check``: set CONTEXTMD_SERVE_BIN,
or run from a checkout that has ``bin/contextmd-serve``.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextmd.capture import apply  # noqa: E402
from contextmd.capture.passes import CaptureError  # noqa: E402
from contextmd.capture.store import Store, write_json  # noqa: E402
from tests.fixtures import observer_db  # noqa: E402

REPO_SERVE = Path(__file__).resolve().parents[1] / "bin" / "contextmd-serve"
SERVE_BIN = os.environ.get("CONTEXTMD_SERVE_BIN") or (str(REPO_SERVE) if REPO_SERVE.exists() else None)

NOW = time.time()
DAY = 86400
SOURCE = "codex:source"

ENTRY = """---
id: chromeria-db-read-only
command: chromeria-v2.sqlite
must-know-before: true
---
Open `chromeria-v2.sqlite` read-only: it is a live WAL database. (Chromeria, 2026-10-10)
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(SERVE_BIN, "needs contextmd-serve: set CONTEXTMD_SERVE_BIN")
class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.repo = self.tmp / "promachos"
        git(self.tmp, "clone", "-q", str(self.remote), str(self.repo))
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        self.store_dir = self.repo / "library"
        self.store_dir.mkdir(parents=True)
        (self.store_dir / "README.md").write_text("# Great Library\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        self.state = self.tmp / "state"
        self.cfg = {"state_dir": str(self.state), "observer_db": str(self.tmp / "observer.db"),
                    "serve_bin": SERVE_BIN, "machine": "host"}
        self.sessions = [
            {"thread": "T-src", "key": SOURCE, "native": "n-src",
             "events": [(1, "c1", NOW - 3 * DAY, "sqlite3 -readonly ~/.t3/userdata/chromeria-v2.sqlite 'select 1'")]},
        ]
        self.item = {"key": {"machine": "host", "thread": "T-src", "event": "c1", "index": 0},
                     "pass": {"pass_id": "20261012T170229Z", "segment": "s046"},
                     "grade": "keep", "title": "read the projection database read-only",
                     "moment": {"observer": {"session_key": SOURCE, "ref": "event", "id": 1}}}
        self.name = Store.key_name(self.item["key"])
        self.inbox_item = self.state / "inbox" / f"{self.name}.json"
        write_json(self.inbox_item, self.item)
        self.entry = self.tmp / "shaped.md"
        self.entry.write_text(ENTRY)

    def run_apply(self, entry_text=None):
        db = Path(self.cfg["observer_db"])
        db.unlink(missing_ok=True)
        observer_db(db, self.sessions).close()
        if entry_text is not None:
            self.entry.write_text(entry_text)
        return apply.apply(self.cfg, self.name, self.entry, self.store_dir)

    def remote_log(self):
        return git(self.remote, "log", "--format=%H", "main").splitlines()

    def assert_refused(self, entry_text, reason):
        before = self.remote_log()
        with self.assertRaises(apply.Refused) as cm:
            self.run_apply(entry_text)
        self.assertIn(reason, str(cm.exception))
        self.assertEqual(self.remote_log(), before)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before[0])
        self.assertEqual(git(self.repo, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertTrue(self.inbox_item.exists())

    # 1. One entry file and one commit carrying the receipts.
    def test_entry_lands_as_one_pushed_commit_with_capture_trailers(self):
        before = self.remote_log()
        out = self.run_apply()
        after = self.remote_log()
        self.assertEqual(after[1:], before)
        sha = after[0]
        self.assertEqual(out["sha"], sha)
        self.assertEqual(git(self.repo, "show", "--name-only", "--format=", sha), "library/chromeria-db-read-only.md")
        self.assertEqual((self.store_dir / "chromeria-db-read-only.md").read_text(), ENTRY)
        trailers = git(self.repo, "log", "-1", "--format=%(trailers:only,unfold)", sha).splitlines()
        self.assertIn(f"Capture-Key: {self.name}", trailers)
        self.assertIn("Capture-Pass: 20261012T170229Z", trailers)
        self.assertFalse(self.inbox_item.exists())
        applied = json.loads((self.state / "applied" / f"{self.name}.json").read_text())
        self.assertEqual(applied["applied"]["sha"], sha)
        self.assertEqual(applied["key"], self.item["key"])

    # 2. Key words are checked against Observer.
    def test_key_word_absent_from_source_and_recent_sessions_is_refused(self):
        self.sessions[0]["events"] = [(1, "c1", NOW - 3 * DAY, "sqlite3 'file:/x/chromeria-v2.sqlite?mode=ro'")]
        self.sessions.append({"thread": "T-a", "key": "codex:a", "native": "n-a",
                              "events": [(2, "c2", NOW - DAY, "cp ~/.t3/userdata/chromeria-v2.sqlite /tmp/")]})
        self.sessions.append({"thread": "T-old", "key": "codex:old", "native": "n-old",
                              "events": [(3, "c3", NOW - 20 * DAY, "ls ~/.t3/userdata/chromeria-v2.sqlite")]})
        self.assert_refused(None, "command: chromeria-v2.sqlite")

    def test_key_word_in_two_recent_sessions_passes_without_the_source(self):
        self.sessions[0]["events"] = [(1, "c1", NOW - 3 * DAY, "echo unrelated")]
        for n in (2, 3):
            self.sessions.append({"thread": f"T-{n}", "key": f"codex:{n}", "native": f"n-{n}",
                                  "events": [(n, f"c{n}", NOW - n * DAY, "ls -l ~/.t3/userdata/chromeria-v2.sqlite")]})
        self.assertEqual(len(self.run_apply()["sha"]), 40)

    def test_path_and_error_keys_are_refused_as_not_checkable(self):
        self.assert_refused(ENTRY.replace("command: chromeria-v2.sqlite", "path: **/chromeria-v2.sqlite"),
                            "only command keys")

    # 3. Refusals.
    def test_store_failing_the_serve_check_is_refused(self):
        (self.store_dir / "broken.md").write_text("no front matter\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "broken")
        git(self.repo, "push", "-q")
        self.assert_refused(None, "contextmd-serve check")

    def test_entry_failing_the_serve_check_is_refused_and_removed(self):
        self.assert_refused(ENTRY.replace("must-know-before: true", "applies: always"), "contextmd-serve check")

    def test_absolute_users_target_is_refused(self):
        text = ENTRY.replace("must-know-before: true\n", "must-know-before: true\ntarget: /Users/someone/notes/chromeria.md\n")
        self.assert_refused(text, "absolute target")

    def test_duplicate_id_is_refused(self):
        (self.store_dir / "other.md").write_text(ENTRY)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "existing")
        git(self.repo, "push", "-q")
        self.assert_refused(None, "already used")

    def test_body_over_1800_bytes_is_refused(self):
        body = "Open `chromeria-v2.sqlite` read-only. " + "é" * 900
        self.assertGreater(len(body.encode()), 1800)
        self.assert_refused(ENTRY.split("---\n")[0] + "---\n" + ENTRY.split("---\n")[1] + "---\n" + body + "\n",
                            "1,800")

    def test_only_kept_items_are_applied(self):
        write_json(self.inbox_item, {**self.item, "grade": "state"})
        self.assert_refused(None, "keep")

    # 4. Re-running is a no-op once the key is in the log.
    def test_rerun_with_capture_key_in_log_adds_nothing(self):
        sha = self.run_apply()["sha"]
        write_json(self.inbox_item, self.item)  # as if the move after the push never happened
        out = self.run_apply()
        self.assertEqual(out, {"state": "already_applied", "sha": sha,
                               "entry": str(self.store_dir / "chromeria-db-read-only.md")})
        self.assertEqual(self.remote_log()[0], sha)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), sha)
        self.assertFalse(self.inbox_item.exists())
        self.assertEqual(self.run_apply()["state"], "already_applied")
        self.assertEqual(self.remote_log()[0], sha)

    # Publication is limited to the receipt commit.
    def unrelated_local_commit(self):
        (self.repo / "AGENTS.md").write_text("persona draft\n")
        git(self.repo, "add", "AGENTS.md")
        git(self.repo, "commit", "-q", "-m", "persona: unpublished")
        return git(self.repo, "rev-parse", "HEAD")

    def test_unpushed_unrelated_commit_is_refused_and_stays_unpublished(self):
        before = self.remote_log()
        local = self.unrelated_local_commit()
        with self.assertRaises(apply.Refused) as cm:
            self.run_apply()
        self.assertIn("not on its upstream", str(cm.exception))
        self.assertEqual(self.remote_log(), before)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), local)
        self.assertFalse((self.store_dir / "chromeria-db-read-only.md").exists())
        self.assertTrue(self.inbox_item.exists())

    def test_resume_refuses_to_push_a_commit_made_after_the_receipt(self):
        before = self.remote_log()
        git(self.repo, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        with self.assertRaises(CaptureError):
            self.run_apply()
        git(self.repo, "remote", "set-url", "origin", str(self.remote))
        self.unrelated_local_commit()
        with self.assertRaises(apply.Refused) as cm:
            self.run_apply()
        self.assertIn("not on its upstream", str(cm.exception))
        self.assertEqual(self.remote_log(), before)
        self.assertTrue(self.inbox_item.exists())

    # A failed check after the write removes only this run's file, so a retry works.
    def test_check_failing_without_a_report_after_the_write_allows_a_retry(self):
        calls = self.tmp / "calls"
        wrapper = self.tmp / "serve-wrapper"
        wrapper.write_text(f"""#!/bin/sh
n=$(cat {calls} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {calls}
[ "$n" -eq 2 ] && {{ echo boom >&2; exit 1; }}
exec {SERVE_BIN} "$@"
""")
        wrapper.chmod(0o755)
        (self.store_dir / "user-note.txt").write_text("not mine\n")
        self.cfg["serve_bin"] = str(wrapper)
        before = self.remote_log()
        with self.assertRaises(CaptureError) as cm:
            self.run_apply()
        self.assertIn("without a report", str(cm.exception))
        self.assertFalse((self.store_dir / "chromeria-db-read-only.md").exists())
        self.assertTrue((self.store_dir / "user-note.txt").exists())
        self.assertEqual(self.remote_log(), before)
        self.assertTrue(self.inbox_item.exists())
        (self.store_dir / "user-note.txt").unlink()
        self.assertEqual(self.run_apply()["state"], "applied")

    # 5. A crash between commit and push resumes with the push.
    def test_failed_push_resumes_with_the_push_and_no_second_commit(self):
        before = self.remote_log()
        git(self.repo, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        with self.assertRaises(CaptureError):
            self.run_apply()
        local = git(self.repo, "rev-parse", "HEAD")
        self.assertNotEqual(local, before[0])
        self.assertEqual(self.remote_log(), before)
        self.assertTrue(self.inbox_item.exists())
        git(self.repo, "remote", "set-url", "origin", str(self.remote))
        out = self.run_apply()
        self.assertEqual(out["state"], "pushed")
        self.assertEqual(out["sha"], local)
        self.assertEqual(self.remote_log(), [local] + before)
        self.assertFalse(self.inbox_item.exists())


if __name__ == "__main__":
    unittest.main()
