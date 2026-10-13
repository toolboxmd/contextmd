import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin" / "contextmd-serve"

# The tables of Chromeria's serving log that the report reads, as Chromeria creates them.
SCHEMA = """
CREATE TABLE moments (id INTEGER PRIMARY KEY, received_at REAL NOT NULL, harness TEXT,
  kind TEXT, session TEXT, thread_id TEXT, run_id TEXT, tool_use_id TEXT, epoch INTEGER,
  holdout INTEGER, status TEXT NOT NULL, reason TEXT, owners_json TEXT,
  cue_adapter TEXT, server_ms REAL, delivery TEXT);
CREATE TABLE candidates (id INTEGER PRIMARY KEY, moment_id INTEGER NOT NULL,
  matcher TEXT NOT NULL, entry TEXT, version TEXT, key TEXT, seam TEXT, stage TEXT, outcome TEXT NOT NULL,
  reason TEXT, score REAL, bytes INTEGER, extra_json TEXT, served_at REAL);
CREATE TABLE claims (delivery TEXT NOT NULL, kind TEXT NOT NULL, harness TEXT NOT NULL,
  session TEXT NOT NULL, epoch INTEGER NOT NULL, run_id TEXT, entry TEXT NOT NULL, version TEXT NOT NULL,
  rule TEXT, whole INTEGER NOT NULL, settled INTEGER NOT NULL DEFAULT 0, candidate_id INTEGER NOT NULL,
  evidence TEXT, tool_use_id TEXT, probe TEXT, checked TEXT);
CREATE TABLE deny_state (harness TEXT NOT NULL, session TEXT NOT NULL, epoch INTEGER NOT NULL,
  entry TEXT NOT NULL, version TEXT NOT NULL, rule_kind TEXT NOT NULL, target TEXT,
  denies INTEGER NOT NULL DEFAULT 0, lifted INTEGER NOT NULL DEFAULT 0, lift_outcome TEXT,
  first_denied_at REAL, lifted_at REAL, PRIMARY KEY (harness, session, epoch, entry, version));
"""

# A fake Agent Observer: `trace --session KEY --json --limit N` prints that session's events.
FAKE_OBSERVER = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
args = sys.argv[1:]
data = json.loads((Path(__file__).parent / "observer.json").read_text())
if data.get("fail"):
    sys.exit(2)
assert args[0] == "trace", args
key = args[args.index("--session") + 1]
limit = int(args[args.index("--limit") + 1])
print(json.dumps({"_view": "trace", "events": data["sessions"].get(key, [])[:limit]}))
"""


class Log:
    """A synthetic serving log: each serve() adds one served candidate the way Chromeria records it."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.next = 0

    def serve(self, *, entry="agentsmd-version-control", seam="deny", kind="pre_tool", harness="claude",
              session="s1", at=1000.0, outcome="injected", checked=None, lift=None):
        self.next += 1
        n = self.next
        tool_use_id = f"toolu_{n}"
        self.db.execute("INSERT INTO moments (id, received_at, harness, kind, session, tool_use_id, epoch, status)"
                        " VALUES (?, ?, ?, ?, ?, ?, 1, 'ok')", (n, at, harness, kind, session, tool_use_id))
        self.db.execute("INSERT INTO candidates (id, moment_id, matcher, entry, version, key, seam, stage, outcome,"
                        " reason, served_at) VALUES (?, ?, 'exact_key', ?, 'v1', 'command:git commit', ?, 'settled',"
                        " ?, ?, ?)", (n, n, entry, seam, outcome, "pointer" if seam == "deny" else None, at))
        self.db.execute("INSERT INTO claims (delivery, kind, harness, session, epoch, entry, version, whole, settled,"
                        " candidate_id, evidence, tool_use_id, checked) VALUES (?, ?, ?, ?, 1, ?, 'v1', 1, 1, ?, ?, ?, ?)",
                        (f"d{n}", "deny" if seam == "deny" else "serve", harness, session, entry, n,
                         "projection" if checked else "none", tool_use_id, checked))
        if seam == "deny":
            self.db.execute("INSERT OR REPLACE INTO deny_state (harness, session, epoch, entry, version, rule_kind,"
                            " denies, lifted, lift_outcome) VALUES (?, ?, 1, ?, 'v1', 'pointer', 1, ?, ?)",
                            (harness, session, entry, 1 if lift else 0, lift))
        self.db.commit()
        return tool_use_id


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "userdata").mkdir()
        self.log = Log(self.tmp / "userdata" / "contextmd.sqlite")
        self.observer = self.tmp / "agent-observer"
        self.observer.write_text(FAKE_OBSERVER)
        self.observer.chmod(0o755)
        self.events = {}

    def run_report(self, *extra):
        (self.tmp / "observer.json").write_text(json.dumps({"sessions": self.events}))
        out = subprocess.run([sys.executable, str(BIN), "report", "--base", str(self.tmp),
                              "--observer", str(self.observer), *extra], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def rows(self, *extra):
        return [json.loads(line) for line in self.run_report("--json", *extra).splitlines()]

    def see(self, session, tool_use_id, ts=1000.5, harness="claude"):
        events = self.events.setdefault(f"{harness}:{session}", [])
        events.append({"id": 100 + len(events), "native_id": tool_use_id, "ts": ts, "family": "tool_call"})

    def test_shown_is_proven_per_row(self):
        self.log.serve(checked="shown", lift="read", session="a")
        self.log.serve(checked=None, lift="read", session="b", harness="codex")
        self.log.serve(outcome="hidden", session="c")
        self.log.serve(seam="context", kind="tool_result", entry="other-fact", session="d")
        self.assertEqual([r["shown"] for r in self.rows()], ["yes", "unproven", "no", "unproven"])

    def test_lift_outcome_of_each_deny(self):
        self.log.serve(checked="shown", lift="read", session="a")
        self.log.serve(checked="shown", lift="not_read_capped", session="b")
        self.log.serve(checked="shown", lift=None, session="c")
        self.log.serve(seam="context", kind="tool_result", entry="other-fact", session="d")
        self.assertEqual([r["lift"] for r in self.rows()], ["read", "not_read_capped", "open", None])

    def test_a_must_know_before_rule_served_on_its_own_result_is_late(self):
        self.log.serve(seam="deny", checked="shown", lift="read", session="a")
        self.log.serve(seam="context", kind="tool_result", session="b")  # same entry, after its command ran
        self.log.serve(seam="context", kind="tool_result", entry="other-fact", session="c")
        self.assertEqual([r["late"] for r in self.rows()], [False, True, False])

    def test_observer_evidence_and_its_gaps(self):
        self.see("a", self.log.serve(session="a", at=1000.0))
        self.log.serve(session="b", at=1000.0)  # Observer never imported the session
        self.log.serve(session="c", at=1000.0)  # imported, but only up to before the moment
        self.see("c", "toolu_other", ts=900.0)
        self.log.serve(session="d", at=1000.0)  # imported past the moment, no record of the call
        self.see("d", "toolu_other", ts=1500.0)
        rows = self.rows()
        self.assertEqual([r["evidence"] for r in rows],
                         ["anchored", "not_imported", "not_imported", "no_native_record"])
        self.assertEqual(rows[0]["event_id"], 100)
        self.assertTrue(all(r["event_id"] is None for r in rows[1:]))

    def test_an_observer_failure_is_reported_not_hidden(self):
        self.log.serve()
        (self.tmp / "observer.json").write_text(json.dumps({"fail": True}))
        out = subprocess.run([sys.executable, str(BIN), "report", "--base", str(self.tmp),
                              "--observer", str(self.observer), "--json"], capture_output=True, text=True)
        self.assertEqual(json.loads(out.stdout)["evidence"], "observer_failed")

    def test_canary_serves_are_left_out(self):
        self.log.serve(entry="live-canary-deny", session="a")
        self.log.serve(session="b")
        self.assertEqual([r["entry"] for r in self.rows()], ["agentsmd-version-control"])
        self.assertIn("1 canary serve left out", self.run_report())

    def test_since_keeps_later_moments(self):
        self.log.serve(at=1_791_800_000.0, session="old")  # 2026-10-12
        self.log.serve(at=1_791_900_000.0, session="new")  # 2026-10-13
        self.assertEqual([r["session"] for r in self.rows("--since", "2026-10-13")], ["new"])

    def test_cards_read_in_under_thirty_seconds(self):
        self.log.serve(seam="deny", checked="shown", lift="read", session="a")
        self.log.serve(seam="context", kind="tool_result", session="b")
        self.log.serve(outcome="hidden", session="c")
        self.log.serve(checked="shown", lift="not_read_capped", session="d", entry="agentsmd-delivery")
        self.log.serve(checked=None, lift=None, session="e", harness="codex")
        cards = [c for c in self.run_report().split("\n\n") if c.startswith("## ")]
        titles = [c.splitlines()[0] for c in cards]
        for title in ("## Late", "## Not proven shown", "## Hidden", "## Passed without reading",
                      "## Never lifted", "## No Observer record"):
            self.assertTrue(any(t.startswith(title) for t in titles), (title, titles))
        for card in cards:
            lines = card.splitlines()
            # A title, one sentence on what happened, one on why it matters, then the facts.
            self.assertTrue(lines[1].startswith("What happened: ") and lines[1].endswith("."), card)
            self.assertTrue(lines[2].startswith("Why it matters: ") and lines[2].endswith("."), card)
            self.assertTrue(lines[3].startswith("Facts: "), card)
            self.assertLessEqual(len(card.split()), 75, card)  # about 30 seconds at a slow reading pace

    def test_no_card_for_a_clean_log(self):
        self.see("s1", self.log.serve(checked="shown", lift="read"))
        text = self.run_report()
        self.assertNotIn("## ", text)
        self.assertIn("1 serve", text)

    def test_missing_log_reports_nothing_served(self):
        (self.tmp / "userdata" / "contextmd.sqlite").unlink()
        self.assertIn("No serving log", self.run_report())


if __name__ == "__main__":
    unittest.main()
