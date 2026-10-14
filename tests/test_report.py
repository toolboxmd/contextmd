import copy
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
CREATE TABLE carries (id INTEGER PRIMARY KEY, harness TEXT NOT NULL, session TEXT NOT NULL,
  run_id TEXT NOT NULL, entry TEXT NOT NULL, version TEXT NOT NULL, text TEXT NOT NULL,
  candidate_id INTEGER NOT NULL, state TEXT NOT NULL, delivery TEXT, created_at REAL NOT NULL);
"""

# Agent Observer's ledger tables the report reads, as Observer 1.0.1 (schema 3) creates them.
LEDGER_SCHEMA = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, session_key TEXT NOT NULL DEFAULT '',
  ordinal_num INTEGER, ts REAL, family TEXT NOT NULL, native_id TEXT NOT NULL, turn_id TEXT, name TEXT,
  target TEXT, status TEXT, duration_ms INTEGER, size_bytes INTEGER, truncated INTEGER, fingerprint TEXT,
  detail_json TEXT, UNIQUE (session_key, family, native_id));
CREATE TABLE user_messages (
  native_id TEXT PRIMARY KEY, alias_id TEXT, source_id INTEGER, session_key TEXT, turn_id TEXT,
  ordinal_num INTEGER, ts REAL, kind TEXT NOT NULL DEFAULT 'genuine', text_hash TEXT NOT NULL,
  text_excerpt TEXT NOT NULL, is_genuine INTEGER NOT NULL);
"""

CLAUDE_SHOWN = {
    "v": 1, "source": "claude_request_replay", "checked_at": 1003.0,
    "record": {"id": "attachment-1", "slot": 0, "at": 1000.0, "turn": None},
    "consumed": {"id": "msg-1", "call": "toolu_received", "at": 1001.0, "request": "request-1"},
}


def ledger(path, version="3"):
    """An Observer ledger fixture; returns an open connection for adding events."""
    db = sqlite3.connect(path)
    db.executescript(LEDGER_SCHEMA)
    db.execute("INSERT INTO schema_meta VALUES ('schema_version', ?)", (version,))
    db.commit()
    return db


class Log:
    """A synthetic serving log: each serve() adds one served candidate the way Chromeria records it."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.next = 0

    def moment(self, *, kind="pre_tool", harness="claude", session="s1", at=1000.0, epoch=1):
        self.next += 1
        n = self.next
        self.db.execute("INSERT INTO moments (id, received_at, harness, kind, session, tool_use_id, epoch, status)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, 'ok')", (n, at, harness, kind, session, f"toolu_{n}", epoch))
        self.db.commit()
        return n

    def serve(self, *, entry="agentsmd-version-control", seam="deny", kind="pre_tool", harness="claude",
              session="s1", at=1000.0, outcome="injected", checked=None, lift=None, epoch=1, carried_from=None, delivery=None):
        n = self.moment(kind=kind, harness=harness, session=session, at=at, epoch=epoch)
        tool_use_id = f"toolu_{n}"
        delivery = delivery or f"d{n}"
        self.db.execute("INSERT INTO candidates (id, moment_id, matcher, entry, version, key, seam, stage, outcome,"
                        " reason, served_at) VALUES (?, ?, 'exact_key', ?, 'v1', 'command:git commit', ?, 'settled',"
                        " ?, ?, ?)", (n, n, entry, seam, outcome, "pointer" if seam == "deny" else None, at))
        # Chromeria unsettles the claim of a deny that another hook's deny replaced.
        settled = 0 if checked == "hidden" else 1
        self.db.execute("INSERT INTO claims (delivery, kind, harness, session, epoch, entry, version, whole, settled,"
                        " candidate_id, evidence, tool_use_id, checked) VALUES (?, ?, ?, ?, ?, ?, 'v1', 1, ?, ?, ?, ?, ?)",
                        (delivery, "deny" if seam == "deny" else "serve", harness, session, epoch, entry, settled, n,
                         "projection" if checked else "none", tool_use_id, checked))
        if seam == "deny":
            self.db.execute("INSERT OR REPLACE INTO deny_state (harness, session, epoch, entry, version, rule_kind,"
                            " denies, lifted, lift_outcome) VALUES (?, ?, ?, ?, 'v1', 'pointer', 1, ?, ?)",
                            (harness, session, epoch, entry, 1 if lift else 0, lift))
        if carried_from is not None:  # a Grok carry: selected at an earlier moment, printed at this one
            self.db.execute("INSERT INTO carries (harness, session, run_id, entry, version, text, candidate_id, state,"
                            " delivery, created_at) VALUES (?, ?, 'r', ?, 'v1', 't', ?, 'printed', ?, ?)",
                            (harness, session, entry, carried_from, delivery, at))
        self.db.commit()
        return tool_use_id

    def carry(self, *, entry="agentsmd-version-control", kind="tool_result", harness="grok", session="g1", at=1000.0):
        """The candidate a Grok carry was selected as, before it is printed."""
        n = self.moment(kind=kind, harness=harness, session=session, at=at)
        self.db.execute("INSERT INTO candidates (id, moment_id, matcher, entry, version, key, seam, outcome)"
                        " VALUES (?, ?, 'exact_key', ?, 'v1', 'command:git commit', 'carry', 'carried')", (n, n, entry))
        self.db.commit()
        return n

    def native_fact(self, candidate, raw, fact="shown"):
        """A producer fact, joined by the moment's own delivery rather than a candidate claim."""
        self.db.execute("""CREATE TABLE IF NOT EXISTS deliveries (delivery TEXT PRIMARY KEY, harness TEXT NOT NULL,
            moment_id INTEGER, stage TEXT NOT NULL, status INTEGER NOT NULL, body TEXT,
            created_at REAL NOT NULL, expires_at REAL NOT NULL)""")
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(deliveries)")}
        for name in ("shown", "shown_json"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE deliveries ADD COLUMN {name} TEXT")
        delivery, harness, moment = self.db.execute(
            "SELECT cl.delivery, m.harness, m.id FROM claims cl JOIN candidates c ON c.id = cl.candidate_id"
            " JOIN moments m ON m.id = c.moment_id WHERE c.id = ?", (candidate,)).fetchone()
        self.db.execute("UPDATE moments SET delivery = ? WHERE id = ?", (delivery, moment))
        self.db.execute("""INSERT INTO deliveries (delivery, harness, moment_id, stage, status, created_at,
                           expires_at, shown, shown_json) VALUES (?, ?, ?, 'settled', 200, 1000, 1005, ?, ?)
                           ON CONFLICT(delivery) DO UPDATE SET shown = excluded.shown, shown_json = excluded.shown_json""",
                        (delivery, harness, moment, fact, raw))
        self.db.commit()


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "userdata").mkdir()
        self.log = Log(self.tmp / "userdata" / "contextmd.sqlite")
        self.ledger_path = self.tmp / "observer.db"
        self.ledger = ledger(self.ledger_path)

    def run_report(self, *extra, db=None):
        out = subprocess.run([sys.executable, str(BIN), "report", "--base", str(self.tmp),
                              "--observer-db", str(db or self.ledger_path), *extra], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def rows(self, *extra):
        return [json.loads(line) for line in self.run_report("--json", *extra).splitlines()]

    def see(self, session, tool_use_id, ts=1000.5, harness="claude", family="tool_call"):
        cur = self.ledger.execute("INSERT INTO events (session_key, ts, family, native_id) VALUES (?, ?, ?, ?)",
                                  (f"{harness}:{session}", ts, family, tool_use_id))
        self.ledger.commit()
        return cur.lastrowid

    def test_every_served_outcome_is_counted_and_unserved_ones_are_not(self):
        for n, outcome in enumerate(["injected", "truncated", "unverified", "awaiting_evidence", "hidden",
                                     "passed", "deduped", "cut_budget", "delivery_unverified", "carried"]):
            self.log.serve(outcome=outcome, session=f"s{n}", checked="hidden" if outcome == "hidden" else None)
        self.assertEqual([r["outcome"] for r in self.rows()],
                         ["injected", "truncated", "unverified", "awaiting_evidence", "hidden"])

    def test_shown_is_proven_per_row(self):
        self.log.serve(checked="shown", lift="read", session="a")
        self.log.serve(checked=None, lift="read", session="b", harness="codex")
        self.log.serve(outcome="hidden", checked="hidden", session="c")
        self.log.serve(outcome="unverified", checked="unverified", session="d")
        self.log.serve(seam="context", kind="tool_result", entry="other-fact", session="e")
        self.assertEqual([r["shown"] for r in self.rows()], ["yes", "unproven", "no", "unproven", "unproven"])

    def test_native_context_proof_reaches_the_json_report(self):
        self.log.serve(seam="context", checked="hidden")  # a claim is not the context's proof
        self.log.native_fact(1, json.dumps(CLAUDE_SHOWN))
        row = self.rows()[0]
        self.assertEqual(row["shown"], "yes")
        self.assertEqual(row["shown_source"], "claude_request_replay")
        self.assertEqual(row["shown_request"], "request-1")
        self.assertEqual(row["consumed"]["at"], 1001.0)
        self.assertEqual(row["consumed"]["call"], "toolu_received")
        self.assertIsNone(row["shown_reason"])

    def test_legacy_context_claims_never_grant_shown(self):
        self.log.serve(seam="context", checked="shown")
        row = self.rows()[0]
        self.assertEqual(row["shown"], "unproven")

    def test_native_fact_uses_only_the_moment_delivery_key(self):
        self.log.serve(seam="context")
        self.log.native_fact(1, json.dumps(CLAUDE_SHOWN))
        self.log.db.execute("UPDATE moments SET delivery = 'another-delivery' WHERE id = 1")
        self.log.db.commit()
        self.assertEqual(self.rows()[0]["shown"], "unproven")

    def test_candidates_in_one_delivery_share_one_fact_without_duplicate_rows(self):
        for entry in ("rule-a", "rule-b"):
            self.log.serve(seam="context", entry=entry, delivery="batch")
            self.log.native_fact(self.log.next, json.dumps(CLAUDE_SHOWN))
        rows = self.rows()
        self.assertEqual([(r["entry"], r["shown"]) for r in rows], [("rule-a", "yes"), ("rule-b", "yes")])

    def test_validated_native_consumption_accepts_both_sources_and_small_times(self):
        from contextmd.serve import report
        claude = copy.deepcopy(CLAUDE_SHOWN)
        claude["record"]["at"], claude["consumed"]["at"], claude["checked_at"] = 1, 2, 3
        codex = copy.deepcopy(claude)
        codex["source"] = "codex_rollout"
        codex["record"]["turn"] = "native-turn-1"
        codex["consumed"].pop("request")
        for harness, ev in (("claude", claude), ("codex", codex)):
            with self.subTest(harness=harness):
                value = report.consumption(harness, "shown", json.dumps(ev))
                self.assertIsNotNone(value)
                self.assertEqual(value.at, 2.0)
                self.assertEqual(value.id, "msg-1")

    def test_incomplete_or_malformed_native_proof_never_grants_consumption(self):
        from contextmd.serve import report
        invalid = [None, "{", "null", "[]", '"text"']
        changes = [("v", True), ("v", 1.0), ("v", "1"), ("v", 2), ("source", None),
                   ("source", "codex_rollout"), ("consumed", None), ("consumed", [])]
        for key, value in changes:
            ev = copy.deepcopy(CLAUDE_SHOWN); ev[key] = value
            invalid.append(json.dumps(ev))
        for part, key, value in [("record", "id", None), ("record", "id", ""), ("record", "slot", True),
                                 ("record", "slot", -1), ("record", "slot", 0.5), ("record", "turn", "other-turn"),
                                 ("consumed", "id", None), ("consumed", "call", []),
                                 ("consumed", "request", None), ("record", "at", 1002),
                                 ("consumed", "at", 1004)]:
            ev = copy.deepcopy(CLAUDE_SHOWN); ev[part][key] = value
            invalid.append(json.dumps(ev))
        for value in (True, "1000", float("nan"), float("inf"), 10 ** 1000):
            for part in ("record", "consumed", None):
                ev = copy.deepcopy(CLAUDE_SHOWN)
                if part:
                    ev[part]["at"] = value
                else:
                    ev["checked_at"] = value
                invalid.append(json.dumps(ev))
        invalid.append('{"v":1,"checked_at":' + "1" * 5000 + '}')
        for raw in invalid:
            with self.subTest(raw=str(raw)[:80]):
                self.assertIsNone(report.consumption("claude", "shown", raw))
        for harness in ("grok", "opencode", "cursor", "antigravity", None, []):
            self.assertIsNone(report.consumption(harness, "shown", json.dumps(CLAUDE_SHOWN)))
        codex = copy.deepcopy(CLAUDE_SHOWN); codex["source"] = "codex_rollout"
        self.assertIsNone(report.consumption("codex", "shown", json.dumps(codex)))
        codex["record"]["turn"] = "native-turn-1"  # a request field still belongs only to Claude
        self.assertIsNone(report.consumption("codex", "shown", json.dumps(codex)))

    def test_unknown_native_reason_is_reported_without_treating_it_as_hidden(self):
        self.log.serve(seam="context")
        self.log.native_fact(1, '{"v":1,"source":null,"reason":"request_unverifiable"}', fact="unknown")
        row = self.rows()[0]
        self.assertEqual(row["shown"], "unproven")
        self.assertEqual(row["shown_reason"], "request_unverifiable")
        self.assertIn("Reasons: request_unverifiable 1.", self.run_report())
        self.log.native_fact(1, "[]", fact="shown")
        self.assertEqual(self.rows()[0]["shown"], "unproven")

    def test_lift_outcome_of_each_deny(self):
        self.log.serve(checked="shown", lift="read", session="a")
        self.log.serve(checked="shown", lift="not_read_capped", session="b")
        self.log.serve(checked="shown", lift=None, session="c", at=1000.0)
        self.log.moment(session="c", at=1001.0)  # the session went on without reading the target
        self.log.serve(checked="shown", lift=None, session="d", at=1000.0)  # nothing after it yet
        self.log.serve(seam="context", kind="tool_result", entry="other-fact", session="e")
        self.assertEqual([r["lift"] for r in self.rows()], ["read", "not_read_capped", "open", "pending", None])

    def test_lift_is_joined_on_the_deny_epoch(self):
        self.log.serve(checked="shown", lift="read", session="a", epoch=1)
        self.log.serve(checked="shown", lift=None, session="a", epoch=2, at=1001.0)
        self.assertEqual([r["lift"] for r in self.rows()], ["read", "pending"])

    def test_a_rule_passed_without_reading_counts_once(self):
        for at in (1000.0, 1001.0, 1002.0):  # three denies of one rule, one lift state
            self.log.serve(checked="shown", lift="not_read_capped", session="a", at=at)
        self.assertIn("## Passed without reading: 1\n", self.run_report())

    def test_a_must_know_before_rule_served_on_its_own_result_is_late(self):
        self.log.serve(seam="deny", checked="shown", lift="read", session="a")
        self.log.serve(seam="context", kind="tool_result", session="b")  # same entry, after its command ran
        self.log.serve(seam="context", kind="tool_result", entry="other-fact", session="c")
        self.log.serve(seam="context", kind="user_message", session="d")  # served before any command
        self.assertEqual([r["late"] for r in self.rows()], [False, True, False, False])

    def test_a_grok_carry_is_late_only_when_selected_on_a_tool_result(self):
        self.log.serve(seam="deny", checked="shown", lift="read", session="a")
        on_result = self.log.carry(kind="tool_result", session="g1")
        self.log.serve(seam="carry_print", harness="grok", session="g1", carried_from=on_result, at=1001.0)
        on_prompt = self.log.carry(kind="user_message", session="g2")
        self.log.serve(seam="carry_print", harness="grok", session="g2", carried_from=on_prompt, at=1002.0)
        self.assertEqual([r["late"] for r in self.rows()], [False, True, False])

    def test_carries_printed_together_keep_their_own_origin(self):
        # Both entries are must-know, so a carry given the other's origin would flip its `late`.
        self.log.serve(seam="deny", checked="shown", lift="read", session="a")
        self.log.serve(seam="deny", checked="shown", lift="read", session="b", entry="other-fact")
        on_prompt = self.log.carry(kind="user_message", session="g1", entry="other-fact")
        on_result = self.log.carry(kind="tool_result", session="g1")
        self.log.serve(seam="carry_print", harness="grok", session="g1", entry="other-fact",
                       carried_from=on_prompt, delivery="batch", at=1001.0)
        self.log.serve(seam="carry_print", harness="grok", session="g1",
                       carried_from=on_result, delivery="batch", at=1001.0)
        self.assertEqual([(r["entry"], r["late"]) for r in self.rows()],
                         [("agentsmd-version-control", False), ("other-fact", False),
                          ("other-fact", False), ("agentsmd-version-control", True)])

    def test_cards_claim_shown_or_ignored_only_with_proof(self):
        for at in (1000.0, 1001.0, 1002.0):  # capped, but no deny was proven shown
            self.log.serve(outcome="unverified", checked="unverified", lift="not_read_capped", session="a", at=at)
        self.log.serve(seam="deny", checked="shown", lift="read", session="b")
        self.log.serve(seam="context", kind="tool_result", session="c")  # late, unproven
        text = self.run_report()
        self.assertNotIn("ignored rule", text)
        self.assertIn("Proven shown: 0 of 1.", text)
        late = next(c for c in text.split("\n\n") if c.startswith("## Late"))
        self.assertNotIn("was shown", late)
        self.assertIn("served after", late)

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
        self.assertEqual(rows[0]["event_id"], 1)
        self.assertTrue(all(r["event_id"] is None for r in rows[1:]))

    def test_an_observer_failure_is_reported_not_hidden(self):
        self.log.serve()
        missing = self.tmp / "absent" / "observer.db"
        rows = [json.loads(line) for line in self.run_report("--json", db=missing).splitlines()]
        self.assertEqual(rows[0]["evidence"], "observer_failed")
        self.assertFalse(missing.exists())  # a read-only report never creates Observer's ledger
        self.assertFalse(missing.parent.exists())

    def test_the_report_never_writes_the_observer_ledger(self):
        self.see("a", self.log.serve(session="a"))
        self.log.serve(session="b")
        self.ledger.close()
        def state():
            return (self.ledger_path.read_bytes(), self.ledger_path.stat().st_mtime_ns,
                    self.ledger_path.stat().st_mode, sorted(p.name for p in self.tmp.iterdir()))
        before = state()
        self.assertEqual([r["evidence"] for r in self.rows()], ["anchored", "not_imported"])
        self.run_report()
        self.assertEqual(state(), before)

    def test_another_ledger_schema_is_not_read_or_migrated(self):
        self.log.serve()
        old = self.tmp / "old.db"
        ledger(old, version="2").close()
        before = old.read_bytes()
        rows = [json.loads(line) for line in self.run_report("--json", db=old).splitlines()]
        self.assertEqual(rows[0]["evidence"], "observer_failed")
        self.assertEqual(old.read_bytes(), before)

    def test_the_anchor_is_the_tool_call_when_its_result_shares_the_id(self):
        tool = self.log.serve(session="a")
        self.see("a", tool, family="tool_result")
        call = self.see("a", tool, family="tool_call")
        self.assertEqual(self.rows()[0]["event_id"], call)

    def test_canary_serves_are_left_out(self):
        self.log.serve(entry="live-canary-deny", session="a")
        self.assertIn("1 canary serve left out", self.run_report())  # also when nothing else was served
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
        self.log.serve(outcome="hidden", checked="hidden", session="c")
        self.log.serve(checked="shown", lift="not_read_capped", session="d", entry="agentsmd-delivery")
        self.log.serve(checked=None, lift=None, session="e", harness="codex")
        self.log.moment(session="e", harness="codex", at=1001.0)
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

    def test_bad_input_fails_with_a_message_not_a_traceback(self):
        self.log.serve()
        for args, words in ((["--since", "yesterday"], "--since"),):
            out = subprocess.run([sys.executable, str(BIN), "report", "--base", str(self.tmp),
                                  "--observer-db", str(self.ledger_path), *args], capture_output=True, text=True)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn(words, out.stdout + out.stderr)
            self.assertNotIn("Traceback", out.stderr)
        self.log.db.execute("DROP TABLE deny_state")
        self.log.db.commit()
        out = subprocess.run([sys.executable, str(BIN), "report", "--base", str(self.tmp),
                              "--observer-db", str(self.ledger_path)], capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("serving log", out.stdout + out.stderr)
        self.assertNotIn("Traceback", out.stderr)

    def test_missing_log_reports_nothing_served(self):
        (self.tmp / "userdata" / "contextmd.sqlite").unlink()
        self.assertIn("No serving log", self.run_report())


if __name__ == "__main__":
    unittest.main()
