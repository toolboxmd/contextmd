import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests import test_report as fixture  # the serving-log schema; module import keeps its tests out

BIN = Path(__file__).resolve().parents[1] / "bin" / "contextmd-serve"
NOW = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc).timestamp()

# A fake `gh` over a JSON file of Issues per repository. It records every call.
FAKE_GH = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
with open(here / "gh-calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\\n")
state = json.loads((here / "gh.json").read_text())
def arg(name):
    return args[args.index(name) + 1]
repo = arg("-R")
issues = state.setdefault(repo, [])
if args[:2] == ["issue", "list"]:
    phrase = arg("--search").split('"')[1]
    print(json.dumps([{"number": i["number"], "body": i["body"]} for i in issues
                      if i["state"] == "OPEN" and phrase in i["body"]]))
elif args[:2] == ["issue", "view"]:
    i = next(i for i in issues if i["number"] == int(args[2]))
    print(json.dumps({"state": i["state"], "body": i["body"], "comments": [{"body": c} for c in i["comments"]]}))
elif args[:2] == ["issue", "create"]:
    n = 100 + sum(len(v) for v in state.values())
    issues.append({"number": n, "state": "OPEN", "title": arg("--title"), "body": sys.stdin.read(), "comments": []})
    print(f"https://github.com/{repo}/issues/{n}")
elif args[:2] == ["issue", "comment"]:
    next(i for i in issues if i["number"] == int(args[2]))["comments"].append(sys.stdin.read())
    print(f"https://github.com/{repo}/issues/{args[2]}#c")
else:
    sys.exit(2)
(here / "gh.json").write_text(json.dumps(state))
"""


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "userdata").mkdir()
        self.db = sqlite3.connect(self.tmp / "userdata" / "contextmd.sqlite")
        self.db.executescript(fixture.SCHEMA + "CREATE TABLE heartbeats (at REAL PRIMARY KEY);")
        self.beat(NOW - 20)
        self.gh = self.tmp / "gh"
        self.gh.write_text(FAKE_GH)
        self.gh.chmod(0o755)
        self.issues = {}
        self.config = self.tmp / "health.json"
        self.configure()
        self.state = self.tmp / "state.json"
        self.next = 0

    def configure(self, **extra):
        self.config.write_text(json.dumps({"machine": "testbox", "expected_harnesses": ["claude"], **extra}))

    def beat(self, at):
        self.db.execute("INSERT OR REPLACE INTO heartbeats VALUES (?)", (at,))
        self.db.commit()

    def moments(self, harness, status, n, at=NOW - 3600, kind="pre_tool", reason=None):
        ids = []
        for _ in range(n):
            self.next += 1
            self.db.execute("INSERT INTO moments (id, received_at, harness, kind, session, status, reason)"
                            " VALUES (?, ?, ?, ?, 'sess-private', ?, ?)", (self.next, at, harness, kind, status, reason))
            ids.append(self.next)
        self.db.commit()
        return ids

    def candidates(self, harness, outcome, n, at=NOW - 3600):
        for m in self.moments(harness, "ok", n, at=at):
            self.db.execute("INSERT INTO candidates (moment_id, matcher, entry, key, outcome, reason)"
                            " VALUES (?, 'exact_key', 'secret-entry', 'command:secret-cmd', ?, 'secret-reason')",
                            (m, outcome))
        self.db.commit()

    def run_health(self, *extra, now=NOW, ok=True):
        (self.tmp / "gh.json").write_text(json.dumps(self.issues))
        when = datetime.fromtimestamp(now, timezone.utc).isoformat()
        out = subprocess.run([sys.executable, str(BIN), "health", "--base", str(self.tmp), "--config", str(self.config),
                              "--state", str(self.state), "--gh", str(self.gh), "--now", when, *extra],
                             capture_output=True, text=True)
        if ok:
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.issues = json.loads((self.tmp / "gh.json").read_text())
        return out.stdout

    def writes(self):
        calls = self.tmp / "gh-calls.jsonl"
        lines = calls.read_text().splitlines() if calls.exists() else []
        return [c for c in map(json.loads, lines) if c[:2] in (["issue", "create"], ["issue", "comment"])]

    def filed(self, repo):
        return [i for i in self.issues.get(repo, [])]

    def test_bad_event_moments_without_candidates_file_in_contextmd(self):
        self.moments("claude", "ok", 150)
        self.moments("claude", "bad_event", 50, kind="user_message", reason="json")
        self.run_health()
        [issue] = self.filed("toolboxmd/contextmd")
        self.assertIn("ContextMD-Class: moment/bad_event", issue["body"])
        self.assertIn("50 of 200", issue["body"])
        self.assertIn("25.0%", issue["body"])
        self.assertIn("testbox", issue["body"])
        self.assertEqual(self.filed("toolboxmd/chromeria"), [])

    def test_a_spike_repaired_between_runs_is_still_filed(self):
        self.moments("claude", "ok", 200, at=NOW - 7200)
        self.run_health(now=NOW - 6000)
        self.assertEqual(self.writes(), [])
        self.moments("claude", "bad_event", 40, at=NOW - 5000)  # broken, then fixed
        self.moments("claude", "ok", 300, at=NOW - 1000)
        self.run_health(now=NOW)
        self.assertEqual(len(self.filed("toolboxmd/contextmd")), 1)

    def test_floors_and_rate(self):
        self.moments("claude", "ok", 79)
        self.moments("claude", "bad_event", 20)  # 99 moments: under the 100-moment floor
        self.moments("grok", "ok", 991)
        self.moments("grok", "unjoined", 9)  # under 10 events
        self.moments("codex", "ok", 960)
        self.moments("codex", "unjoined", 40)  # 4%: under 5%
        self.run_health()
        self.assertEqual(self.writes(), [])
        self.moments("codex", "unjoined", 20)  # 60 of 1,020: over 5%
        self.run_health("--from", datetime.fromtimestamp(NOW - 86400, timezone.utc).isoformat(),
                        "--to", datetime.fromtimestamp(NOW, timezone.utc).isoformat())
        [issue] = self.filed("toolboxmd/chromeria")
        self.assertIn("ContextMD-Class: moment/unjoined", issue["body"])

    def test_an_expected_harness_with_no_moments(self):
        self.configure(expected_harnesses=["claude", "opencode"])
        self.moments("claude", "ok", 200)
        self.assertIn("opencode: no calls, coverage unknown", self.run_health())
        self.beat(NOW - 600)
        self.db.execute("DELETE FROM heartbeats WHERE at = ?", (NOW - 20,))
        self.db.commit()
        self.assertIn("opencode: no calls, server stale", self.run_health("--dry-run"))

    def test_reported_outcomes_are_not_filed(self):
        self.moments("claude", "ok", 200)
        for outcome in ("failed_by_output", "cut_budget", "truncated", "unsupported_key"):
            self.candidates("claude", outcome, 30)
        text = self.run_health()
        self.assertEqual(self.writes(), [])
        for outcome in ("failed_by_output", "cut_budget", "truncated", "unsupported_key"):
            self.assertIn(f"{outcome} 30", text)

    def test_delivery_unverified_and_unverified_are_separate_classes(self):
        self.moments("claude", "ok", 200)
        self.candidates("claude", "delivery_unverified", 12)
        self.candidates("claude", "unverified", 12)
        self.candidates("claude", "no_seam", 9)  # under 10 rows
        self.run_health()
        marks = sorted(line for i in self.filed("toolboxmd/chromeria") for line in i["body"].splitlines()
                       if line.startswith("ContextMD-Class:"))
        self.assertEqual(marks, ["ContextMD-Class: candidate/delivery_unverified",
                                 "ContextMD-Class: candidate/unverified"])

    def test_an_unknown_status_is_reported_not_dropped(self):
        self.moments("claude", "ok", 200)
        self.moments("claude", "brand_new", 3)
        self.assertIn("unknown status brand_new 3", self.run_health())

    def test_an_identical_rerun_writes_nothing(self):
        self.moments("claude", "ok", 150)
        self.moments("claude", "bad_event", 50)
        window = ("--from", datetime.fromtimestamp(NOW - 86400, timezone.utc).isoformat(),
                  "--to", datetime.fromtimestamp(NOW, timezone.utc).isoformat())
        self.run_health(*window)
        first = len(self.writes())
        self.run_health(*window)
        self.assertEqual(len(self.writes()), first)
        self.run_health()  # the stored window
        self.run_health()  # and again at the same time: an empty window
        self.assertEqual(len(self.writes()), first)

    def test_an_open_marked_issue_gets_a_comment_and_seeded_classes_go_to_their_issue(self):
        self.issues = {"toolboxmd/contextmd": [{"number": 7, "state": "OPEN", "comments": [],
                                                "body": "x\nContextMD-Class: moment/bad_event\n"}],
                       "toolboxmd/chromeria": [{"number": 228, "state": "OPEN", "comments": [], "body": "grok"}]}
        self.configure(seed={"candidate/hidden": "toolboxmd/chromeria#228"})
        self.moments("claude", "ok", 150)
        self.moments("claude", "bad_event", 50)
        self.candidates("grok", "hidden", 11)
        self.run_health()
        self.assertEqual(len(self.filed("toolboxmd/contextmd")), 1)
        self.assertEqual(len(self.issues["toolboxmd/contextmd"][0]["comments"]), 1)
        self.assertEqual(len(self.filed("toolboxmd/chromeria")), 1)
        self.assertEqual(len(self.issues["toolboxmd/chromeria"][0]["comments"]), 1)

    def test_another_machine_with_the_same_window_still_posts(self):
        self.moments("claude", "ok", 150)
        self.moments("claude", "bad_event", 50)
        window = ("--from", datetime.fromtimestamp(NOW - 86400, timezone.utc).isoformat(),
                  "--to", datetime.fromtimestamp(NOW, timezone.utc).isoformat())
        self.configure(machine="machine-a")
        self.run_health(*window)
        self.configure(machine="machine-b")
        text = self.run_health(*window)
        self.assertNotIn("already posted", text)
        [issue] = self.filed("toolboxmd/contextmd")
        self.assertEqual(len(issue["comments"]), 1)
        self.assertIn("machine-b", issue["comments"][0])
        self.run_health(*window)  # the same machine again: an identical observation
        self.assertEqual(len(self.filed("toolboxmd/contextmd")[0]["comments"]), 1)

    def test_classes_seeded_to_one_issue_each_post(self):
        self.issues = {"toolboxmd/chromeria": [{"number": 228, "state": "OPEN", "comments": [], "body": "x"}]}
        self.configure(seed={"candidate/hidden": "toolboxmd/chromeria#228",
                             "candidate/unverified": "toolboxmd/chromeria#228"})
        self.moments("grok", "ok", 200)
        self.candidates("grok", "hidden", 11)
        self.candidates("grok", "unverified", 11)
        self.run_health()
        comments = self.issues["toolboxmd/chromeria"][0]["comments"]
        self.assertEqual(sorted("hidden" in c for c in comments), [False, True])
        self.assertEqual(len(comments), 2)

    def test_at_most_ten_ids_per_body_and_candidate_counts_say_their_unit(self):
        for harness in ("claude", "codex"):
            self.moments(harness, "ok", 150)
            self.moments(harness, "bad_event", 50)
        self.candidates("grok", "unverified", 12)
        self.moments("grok", "ok", 100)
        self.run_health()
        for body in (i["body"] for v in self.issues.values() for i in v):
            ids = next(line for line in body.splitlines() if line.startswith("Example ids:"))
            self.assertLessEqual(len(ids.split(":", 1)[1].split(",")), 10, body)
        [cand] = self.filed("toolboxmd/chromeria")
        self.assertIn("12 candidate rows", cand["body"])
        self.assertIn("of 112 moments", cand["body"])

    def test_bodies_hold_counts_and_ids_only(self):
        self.moments("claude", "ok", 100)
        self.moments("claude", "bad_event", 30, reason="secret-reason")
        self.candidates("claude", "delivery_unverified", 12)
        self.run_health()
        texts = [i["body"] for v in self.issues.values() for i in v]
        self.assertEqual(len(texts), 2)
        for body in texts:
            for private in ("secret", "sess-private", "command:"):
                self.assertNotIn(private, body)
            ids = [line for line in body.splitlines() if line.startswith("Example ids:")]
            self.assertEqual(len(ids), 1)
            self.assertLessEqual(len(ids[0].split(":", 1)[1].split(",")), 10)

    def test_an_unreadable_log_is_filed_and_the_window_is_kept(self):
        (self.tmp / "userdata" / "contextmd.sqlite").write_bytes(b"not a database at all" * 100)
        text = self.run_health()
        self.assertIn("unreadable", text)
        [issue] = self.filed("toolboxmd/contextmd")
        self.assertIn("ContextMD-Class: database/unreadable", issue["body"])
        self.assertFalse(self.state.exists())

    def test_an_empty_window_is_a_result(self):
        text = self.run_health()
        self.assertIn("claude: no calls", text)
        self.assertNotIn("0.0%", text)
        self.assertEqual(self.writes(), [])

    def test_a_dry_run_writes_nothing(self):
        self.moments("claude", "ok", 150)
        self.moments("claude", "bad_event", 50)
        text = self.run_health("--dry-run")
        self.assertIn("would file toolboxmd/contextmd moment/bad_event", text)
        self.assertEqual(self.writes(), [])
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
