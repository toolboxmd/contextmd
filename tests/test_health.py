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


# Chromeria's Cue columns and delivery table, as the live serving log has them.
CUE_SCHEMA = """
ALTER TABLE moments ADD COLUMN cue_status TEXT;
ALTER TABLE moments ADD COLUMN cue_ms REAL;
ALTER TABLE moments ADD COLUMN cue_wait_ms REAL;
ALTER TABLE moments ADD COLUMN cue_json TEXT;
CREATE TABLE deliveries (delivery TEXT PRIMARY KEY, harness TEXT NOT NULL, moment_id INTEGER,
  stage TEXT NOT NULL, status INTEGER NOT NULL, body TEXT, run_id TEXT, serve_bytes INTEGER NOT NULL DEFAULT 0,
  deny_bytes INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, expires_at REAL NOT NULL, settled_at REAL);
"""
# Agent Observer's import bookkeeping (schema 3), the columns the freshness check reads.
SOURCES_SCHEMA = """
CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, harness TEXT NOT NULL, path TEXT NOT NULL,
  sha256 TEXT NOT NULL DEFAULT '', size_bytes INTEGER NOT NULL DEFAULT 0, read_offset INTEGER NOT NULL DEFAULT 0,
  session_id TEXT, imported_at REAL NOT NULL);
"""


class HealthFixture:
    """A serving log, an Observer ledger and a fake `gh` in a temporary directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "userdata").mkdir()
        self.db = sqlite3.connect(self.tmp / "userdata" / "contextmd.sqlite")
        self.db.executescript(fixture.SCHEMA + CUE_SCHEMA + "CREATE TABLE heartbeats (at REAL PRIMARY KEY);")
        self.ledger_path = self.tmp / "observer.db"
        self.ledger = fixture.ledger(self.ledger_path)
        self.ledger.executescript(SOURCES_SCHEMA)
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
                              "--state", str(self.state), "--gh", str(self.gh), "--now", when,
                              "--observer-db", str(self.ledger_path), *extra],
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



class HealthTest(HealthFixture, unittest.TestCase):
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


H = 3600
DAY = 24 * H


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class CueHealthTest(HealthFixture, unittest.TestCase):
    """Cue, server duration and lost tool calls (#57), at the `health` seam."""

    def moment(self, harness="claude", kind="tool_result", at=NOW - 3 * H, status="ok", session="s1", call=None,
               cue=None, cue_ms=None, server_s=0.2, delivery=None):
        self.next += 1
        self.db.execute("INSERT INTO moments (id, received_at, harness, kind, session, tool_use_id, status, cue_status,"
                        " cue_ms, server_ms, delivery) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.next, at, harness, kind, session, call, status, cue, cue_ms, server_s, delivery))
        self.db.commit()
        return self.next

    def cue(self, status, n, harness="claude", kind="tool_result", at=NOW - 3 * H, cue_ms=None):
        for _ in range(n):
            self.moment(harness, kind, at=at, cue=status, cue_ms=cue_ms)

    def source(self, session, imported_at, harness="claude", read_offset=10, size=10):
        self.ledger.execute("INSERT INTO sources (harness, path, session_id, imported_at, read_offset, size_bytes)"
                            " VALUES (?, ?, ?, ?, ?, ?)", (harness, f"/x/{session}/{imported_at}", session,
                                                          imported_at, read_offset, size))
        self.ledger.commit()

    def event(self, harness, session, native_id, at, family="tool_call"):
        self.ledger.execute("INSERT INTO events (session_key, family, native_id, ts, name) VALUES (?, ?, ?, ?, 'x')",
                            (f"{harness}:{session}", family, native_id, at))
        self.ledger.commit()

    def day(self, *extra, start=NOW - DAY, ok=True):
        return self.run_health("--from", iso(start), "--to", iso(start + DAY), "--dry-run", *extra, ok=ok)

    def test_cue_buckets_are_exclusive_per_kind_and_round_trip_is_nearest_rank_over_ok(self):
        for ms in range(100, 1001, 100):
            self.moment(cue="ok", cue_ms=ms)
        self.cue("timeout", 3)
        self.cue("lost", 2)
        self.cue("not_called", 5)
        self.cue("connect_failed", 1)
        self.cue("started", 4, kind="pre_tool")
        self.cue("ok", 2, kind="tool_failure", cue_ms=50)
        text = self.day()
        self.assertIn("Cue claude tool result: scored 21, unavailable 6 (28.6%), available 15, timeout 3 (20.0%),"
                      " lost 2 (13.3%), ok 10; round trip p50/p90 500/900 ms", text)
        self.assertIn("Cue claude tool failure: scored 2, unavailable 0 (0.0%), available 2, timeout 0 (0.0%),"
                      " lost 0 (0.0%), ok 2; round trip p50/p90 50/50 ms", text)
        self.assertNotIn("Cue claude pre-tool", text)

    def test_server_duration_is_stored_in_seconds_under_either_name(self):
        for s in range(1, 11):
            self.moment(kind="pre_tool", server_s=s / 10)
        self.assertIn("Server claude pre-tool: p50/p90/max 500/900/1000 ms", self.day())
        self.db.execute("ALTER TABLE moments RENAME COLUMN server_ms TO server_seconds")
        self.db.commit()
        self.assertIn("Server claude pre-tool: p50/p90/max 500/900/1000 ms", self.day())

    def test_unconfirmed_served_replies(self):
        for i, (stage, status) in enumerate([("settled", 200)] * 3 + [("released", 200)] + [("none", 204)] * 5):
            self.db.execute("INSERT INTO deliveries (delivery, harness, stage, status, created_at, expires_at)"
                            " VALUES (?, 'claude', ?, ?, ?, ?)", (f"d{i}", stage, status, NOW - 3 * H, NOW))
        self.db.commit()
        self.assertIn("Served replies not confirmed by the hook's settle: claude 1 of 4", self.day())

    def observed_day(self):
        """Observer's window is the health window two hours back: calls at NOW - 5 h are in it."""
        at = NOW - 5 * H
        # s1: served from at to at + 100; c2 never reached the server; c4 came after its last moment.
        self.moment(kind="pre_tool", at=at, session="s1", call="c1")
        self.moment(kind="pre_tool", at=at + 100, session="s1", call="c3")
        for call, t in (("c1", at), ("c2", at + 50), ("c3", at + 100), ("c4", at + 200)):
            self.event("claude", "s1", call, t)
        self.source("s1", NOW)
        # s0: no moments at all; s2: one stale source; s3: an unread tail.
        self.event("claude", "s0", "c5", at)
        self.source("s0", NOW)
        for session, stale in (("s2", True), ("s3", False)):
            self.moment(kind="pre_tool", at=at, session=session, call=f"{session}-a")
            self.moment(kind="tool_result", at=at + 20, session=session, call=f"{session}-b")
            self.event("claude", session, f"{session}-a", at)
            self.event("claude", session, f"{session}-b", at + 10)
            self.source(session, NOW)
            self.source(session, at - 1 if stale else NOW, read_offset=5 if not stale else 10)
        # Codex: the hook's id is the exec id, on Observer's tool result; the model's call id is ignored.
        self.moment(harness="codex", kind="pre_tool", at=at, session="x1", call="exec-1")
        self.moment(harness="codex", kind="tool_result", at=at + 100, session="x1", call="exec-1")
        self.event("codex", "x1", "exec-1", at + 5, family="tool_result")
        self.event("codex", "x1", "exec-2", at + 50, family="tool_result")
        self.event("codex", "x1", "call_abc", at + 1)
        self.source("x1", NOW, harness="codex")

    def test_tool_calls_without_a_pre_tool_row_and_what_is_not_judged(self):
        self.observed_day()
        text = self.day()
        self.assertIn("Observer claude: judged 3 tool calls, server cut 0, no pre-tool row 1 (33.3%)", text)
        self.assertIn("Observer codex: judged 2 completed shell execs, server cut 0, no pre-tool row 1 (50.0%)", text)
        self.assertIn("Not judged: 1 calls in sessions with no moments (coverage unknown), 1 outside their"
                      " session's first-to-last moment, 4 in 2 sessions not synced, 0 served sessions with no"
                      " Observer source", text)

    def test_an_id_bearing_over_budget_pre_tool_is_a_server_cut_not_lost(self):
        at = NOW - 5 * H
        self.moment(kind="pre_tool", at=at, call="c1")
        self.moment(kind="pre_tool", at=at + 10, call="c2", status="over_budget")
        self.moment(kind="pre_tool", at=at + 20, call="c3")
        self.moment(kind=None, at=at + 15, session=None, status="over_budget")  # cut before its body parsed
        for call, t in (("c1", at), ("c2", at + 10), ("c3", at + 20)):
            self.event("claude", "s1", call, t)
        self.source("s1", NOW)
        text = self.day()
        self.assertIn("Observer claude: judged 3 tool calls, server cut 1, no pre-tool row 0 (0.0%)", text)
        self.assertIn("over_budget rows without ids: 1", text)

    def test_an_unreadable_observer_ledger_fails_the_run_and_keeps_the_window(self):
        self.ledger.execute("UPDATE schema_meta SET value = '99'")
        self.ledger.commit()
        out = self.run_health(ok=False)
        self.assertIn("Observer", out)
        self.assertFalse(self.state.exists())

    def lost_rate(self, start, ok, lost, harness="claude"):
        at = start + 12 * H
        self.cue("ok", ok, harness=harness, at=at, cue_ms=300)
        self.cue("lost", lost, harness=harness, at=at)

    def test_day_one_files_nothing_and_marks_the_breach(self):
        self.lost_rate(NOW - DAY, 80, 20)
        text = self.run_health("--from", iso(NOW - DAY), "--to", iso(NOW))
        self.assertIn("lost 20 (20.0%) *", text)
        self.assertIn("previous unavailable", text)
        self.assertEqual(self.writes(), [])

    def test_a_crossing_files_per_harness_even_when_the_pooled_rate_is_under_the_bar(self):
        self.lost_rate(NOW - 2 * DAY, 99, 1)
        self.lost_rate(NOW - DAY, 88, 12)
        self.lost_rate(NOW - 2 * DAY, 1000, 0, harness="grok")
        self.lost_rate(NOW - DAY, 1000, 0, harness="grok")
        self.run_health("--from", iso(NOW - DAY), "--to", iso(NOW))
        [issue] = self.filed("toolboxmd/chromeria")
        self.assertIn("ContextMD-Class: cue/lost", issue["body"])
        self.assertIn("claude tool result", issue["body"])
        self.assertNotIn("grok", issue["body"])

    def test_a_standing_rate_does_not_file_and_a_five_point_rise_does(self):
        self.lost_rate(NOW - 2 * DAY, 184, 16)  # 8%
        self.lost_rate(NOW - DAY, 182, 18)  # 9%: over the bar, but standing
        self.run_health("--from", iso(NOW - DAY), "--to", iso(NOW))
        self.assertEqual(self.writes(), [])
        self.lost_rate(NOW - DAY, 0, 12)  # 30 of 212: 14.2%, six points up
        self.run_health("--from", iso(NOW - DAY), "--to", iso(NOW))
        [issue] = self.filed("toolboxmd/chromeria")
        self.assertIn("ContextMD-Class: cue/lost", issue["body"])

    def test_unlogged_tool_calls_file_on_a_crossing_in_contextmd(self):
        for day, missing in ((2, 0), (1, 12)):
            at = NOW - day * DAY + 8 * H  # Observer's window is two hours behind the health window
            for i in range(100):
                call = f"d{day}-{i}"
                if i >= missing:
                    self.moment(kind="pre_tool", at=at + i, call=call)
                self.event("claude", "s1", call, at + i)
            self.moment(kind="pre_tool", at=at + 200, call=f"d{day}-end")
            self.event("claude", "s1", f"d{day}-end", at + 200)
        self.source("s1", NOW)
        self.run_health("--from", iso(NOW - DAY), "--to", iso(NOW))
        [issue] = self.filed("toolboxmd/contextmd")
        self.assertIn("ContextMD-Class: moment/unlogged", issue["body"])

    def test_consecutive_runs_judge_adjacent_observer_windows(self):
        edge = NOW - 6 * H - 2 * H  # the first run's end, two hours back
        self.moment(kind="pre_tool", at=edge - 100, call="a")
        self.moment(kind="pre_tool", at=edge + 100, call="b")
        for call, t in (("a", edge - 100), ("gap", edge - 1), ("b", edge + 100)):
            self.event("claude", "s1", call, t)
        self.source("s1", NOW)
        first = self.run_health(now=NOW - 6 * H)
        second = self.run_health(now=NOW)
        self.assertIn("Observer claude: judged 2 tool calls, server cut 0, no pre-tool row 1 (50.0%)", first)
        self.assertIn("Observer claude: judged 1 tool calls, server cut 0, no pre-tool row 0 (0.0%)", second)

if __name__ == "__main__":
    unittest.main()
