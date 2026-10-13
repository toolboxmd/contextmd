import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import test_report as fixture  # the synthetic serving log; module import keeps its tests out

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "contextmd-serve"
PROMPT = ROOT / "prompts" / "serve" / "label.md"

# A fake Agent Observer that only answers `--db LEDGER event show`, Observer's read-only lookup.
# Any other command fails, and every call is recorded, so a test can see what the card ran.
FAKE_OBSERVER = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
here = Path(__file__).parent
with open(here / "calls.jsonl", "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:1] != ["--db"] or args[2:4] != ["event", "show"]:
    sys.exit(2)
data = json.loads((here / "observer.json").read_text())
ids = [int(line) for line in sys.stdin.read().split()]
print(json.dumps([data["details"][str(i)] for i in ids if str(i) in data["details"]]))
"""

# The real AgentsMD entry, as it was served (it is public). The version the log holds is the hash of these bytes.
ENTRY_V1 = """---
id: agentsmd-elon-method
command: gh issue create
command: gh pr create
must-know-before: true
target: ../procedures/elon-method/index.md
---
AgentsMD: apply the Elon method before creating an Issue or PR.
"""
ENTRY_V2 = ENTRY_V1.replace("before creating an Issue or PR", "before creating any Issue or PR")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class CardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "userdata").mkdir()
        self.log = fixture.Log(self.tmp / "userdata" / "contextmd.sqlite")
        self.log.db.executescript("""CREATE TABLE deliveries (delivery TEXT PRIMARY KEY, harness TEXT NOT NULL,
          moment_id INTEGER, stage TEXT NOT NULL, status INTEGER NOT NULL, body TEXT, run_id TEXT,
          serve_bytes INTEGER NOT NULL DEFAULT 0, deny_bytes INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
          expires_at REAL NOT NULL, settled_at REAL);""")
        self.observer = self.tmp / "agent-observer"
        self.observer.write_text(FAKE_OBSERVER)
        self.observer.chmod(0o755)
        self.ledger_path = self.tmp / "observer.db"
        self.ledger = fixture.ledger(self.ledger_path)
        self.data = {"details": {}}
        # An owner repository: the entry was served at v1, then edited to v2 and committed.
        self.store = self.tmp / "agentsmd" / "contextmd"
        self.store.mkdir(parents=True)
        git(self.tmp / "agentsmd", "init", "-q")
        for text in (ENTRY_V1, ENTRY_V2):
            (self.store / "elon-method.md").write_text(text)
            git(self.tmp / "agentsmd", "add", ".")
            git(self.tmp / "agentsmd", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "entry")
        import hashlib
        self.v1 = hashlib.sha256(ENTRY_V1.encode()).hexdigest()[:12]
        self.head = git(self.tmp / "agentsmd", "rev-parse", "HEAD")

    def served(self, **kw):
        """A context serve of the entry on the result of `gh issue create`, like the live log's candidates."""
        kw.setdefault("seam", "context")
        kw.setdefault("kind", "tool_result")
        tool = self.log.serve(entry="agentsmd-elon-method", session="s1", at=1000.0, **kw)
        n = self.log.next
        self.log.db.execute("UPDATE candidates SET version = ?, key = 'command:gh issue create' WHERE id = ?",
                            (self.v1, n))
        self.log.db.execute("UPDATE claims SET version = ? WHERE candidate_id = ?", (self.v1, n))
        self.log.db.execute("UPDATE moments SET owners_json = ? WHERE id = ?",
                            (json.dumps({str(self.store): self.head}), n))
        self.log.db.commit()
        return n, tool

    def event(self, eid, family, ts, native_id=None, **detail):
        self.ledger.execute("INSERT INTO events (id, session_key, ts, family, native_id, name, detail_json)"
                            " VALUES (?, 'claude:s1', ?, ?, ?, ?, ?)",
                            (eid, ts, family, native_id or f"n{eid}", detail.get("tool"),
                             json.dumps(detail["trace_detail"]) if "trace_detail" in detail else None))
        self.ledger.commit()
        if family == "tool_call":
            self.data["details"][str(eid)] = {"id": eid, "family": family, "tool": detail.get("tool", "Bash"),
                                              "input": detail.get("input", {}), "status": detail.get("status", "ok"),
                                              "result": detail.get("result", "")}

    def message(self, native_id, ts, text, kind="genuine"):
        self.ledger.execute("INSERT INTO user_messages (native_id, session_key, ts, kind, text_hash, text_excerpt,"
                            " is_genuine) VALUES (?, 'claude:s1', ?, ?, 'h', ?, ?)",
                            (native_id, ts, kind, text, 1 if kind == "genuine" else 0))
        self.ledger.commit()

    def card(self, candidate, *extra, ok=True):
        (self.tmp / "observer.json").write_text(json.dumps(self.data))
        out = subprocess.run([sys.executable, str(BIN), "card", "--base", str(self.tmp), "--candidate", str(candidate),
                              "--observer-db", str(self.ledger_path), "--observer", str(self.observer), *extra],
                             capture_output=True, text=True)
        if ok:
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out

    def window_case(self):
        n, tool = self.served()
        self.event(500, "tool_call", 999.0, native_id=tool, tool="Bash",
                   input={"command": "gh issue create --title x --body-file -"}, result="https://example/issues/7")
        self.event(501, "tool_result", 999.1, native_id=tool)  # the same call again: shown once
        self.event(502, "assistant_message", 1001.0, trace_detail={"excerpt": "Filed it; now the PR."})
        self.event(503, "tool_call", 1002.0, tool="Bash", input={"command": "gh pr create --fill"}, result="pr 8")
        self.message("u1", 1002.5, "Tool output fed back to the model.", kind="synthetic")  # not a boundary
        self.message("u2", 1003.0, "You skipped the Elon record.")
        self.event(504, "tool_call", 1004.0, tool="Bash", input={"command": "echo after"}, result="after")
        return n

    def test_the_card_holds_the_rule_as_served_and_the_window_up_to_the_next_message(self):
        text = self.card(self.window_case()).stdout
        head, _, rest = text.partition("\n\n")
        lines = head.splitlines()
        self.assertTrue(lines[1].startswith("What happened: ") and lines[1].endswith("."), head)
        self.assertTrue(lines[2].startswith("Why it matters: ") and lines[2].endswith("."), head)
        self.assertTrue(lines[3].startswith("Facts: "), head)
        self.assertLessEqual(len(head.split()), 75, head)  # read in under 30 seconds
        self.assertIn("nothing proves the model saw it", lines[1])
        # The rule is the served version (v1 from Git history), not today's file (v2).
        self.assertIn("before creating an Issue or PR", rest)
        self.assertNotIn("any Issue", rest)
        events = re.findall(r"^\[(\S+)\]", rest, re.M)
        self.assertEqual(events, ["500", "502", "503", "u2"])
        self.assertIn("gh pr create --fill", rest)
        self.assertIn("You skipped the Elon record.", rest)
        self.assertNotIn("echo after", rest)  # after the next message: outside the window
        self.assertIn("ends at the next message", head)

    def test_json_card_for_the_judge(self):
        n = self.window_case()
        card = json.loads(self.card(n, "--json").stdout)
        self.assertEqual(card["candidate"], n)
        self.assertEqual(card["rule"]["source"], "store_history")
        self.assertEqual(card["window"]["end"], "next_message")
        self.assertEqual([e["id"] for e in card["events"]], [500, 502, 503, "u2"])
        self.assertEqual(card["events"][0]["role"], "trigger")
        self.assertIsNone(card["no_evidence"])

    def test_a_window_cut_at_the_cap_withholds_only_no_governed_action(self):
        from contextmd.serve import card
        n, tool = self.served()
        self.event(500, "tool_call", 999.0, native_id=tool, input={"command": "gh issue create"})
        for i in range(70):
            self.event(600 + i, "tool_call", 1001.0 + i, input={"command": f"step {i}"})
        cut = json.loads(self.card(n, "--json").stdout)
        self.assertEqual(cut["window"]["end"], "cap")
        self.assertEqual(len(cut["events"]), 60)
        self.assertIsNone(cut["no_evidence"])  # the events it shows are evidence
        self.assertEqual(cut["allowed_labels"], ["irrelevant", "wrong", "followed"])  # unproven: no ignored
        self.assertIn("ignored", card.allowed_labels({**cut, "shown": "yes"}))
        verdict = {"candidate": n, "governed_action": None, "events": [600], "reason": "x"}
        with self.assertRaises(card.LabelError):  # the action may sit past the cut
            card.check_label(cut, {**verdict, "label": "no_governed_action"})
        card.check_label(cut, {**verdict, "label": "followed", "governed_action": 600})
        card.check_label(cut, {**verdict, "label": None})
        facts = self.card(n).stdout.splitlines()[3]
        self.assertIn("cut at 60 events", facts)
        self.assertNotIn("No evidence", facts)

    def test_a_window_ended_by_a_message_keeps_no_governed_action(self):
        card = json.loads(self.card(self.window_case(), "--json").stdout)
        self.assertEqual(card["window"]["end"], "next_message")
        self.assertIn("no_governed_action", card["allowed_labels"])

    def test_a_serve_without_its_observer_record_gets_no_events(self):
        n, _ = self.served()
        card = json.loads(self.card(n, "--json").stdout)
        self.assertEqual(card["no_evidence"], "not_imported")
        self.assertEqual(card["events"], [])
        self.assertIn("No evidence: not_imported", self.card(n).stdout)

    def test_rule_text_falls_back_to_the_deny_text_then_says_unavailable(self):
        n, tool = self.served(seam="deny", kind="pre_tool", checked="shown", lift="read")
        self.log.db.execute("UPDATE moments SET owners_json = '{}' WHERE id = ?", (n,))
        self.log.db.execute("UPDATE claims SET probe = 'AgentsMD: apply the Elon method.' WHERE candidate_id = ?", (n,))
        self.log.db.commit()
        self.event(500, "tool_call", 1000.0, native_id=tool, input={"command": "gh issue create"})
        card = json.loads(self.card(n, "--json").stdout)
        self.assertEqual((card["rule"]["source"], card["rule"]["text"]), ("deny_text", "AgentsMD: apply the Elon method."))
        self.log.db.execute("UPDATE claims SET probe = NULL WHERE candidate_id = ?", (n,))
        self.log.db.commit()
        card = json.loads(self.card(n, "--json").stdout)
        self.assertEqual((card["rule"]["source"], card["no_evidence"]), ("unavailable", "rule_text"))

    def test_without_the_entry_the_rule_comes_from_the_reply_that_carried_it(self):
        n, tool = self.served()
        self.log.db.execute("UPDATE moments SET owners_json = '{}' WHERE id = ?", (n,))
        body = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
                "ContextMD, the user's own rule store, applies this rule from the user here:\n\n"
                "AgentsMD: apply the Elon method before creating an Issue or PR."}}
        self.log.db.execute("INSERT INTO deliveries (delivery, harness, moment_id, stage, status, body, created_at,"
                            " expires_at) VALUES (?, 'claude', ?, 'settled', 200, ?, 0, 0)",
                            (f"d{n}", n, json.dumps(body)))
        self.log.db.commit()
        self.event(500, "tool_call", 999.0, native_id=tool, input={"command": "gh issue create"})
        rule = json.loads(self.card(n, "--json").stdout)["rule"]
        self.assertEqual(rule["source"], "reply")
        self.assertIn("apply the Elon method", rule["text"])
        self.assertEqual(rule["rules_in_reply"], 1)

    def test_a_window_the_record_has_not_caught_up_with_is_no_evidence(self):
        n, tool = self.served()
        self.event(500, "tool_call", 999.0, native_id=tool, input={"command": "gh issue create"})
        self.event(501, "tool_call", 1001.0, input={"command": "ls"})
        self.log.moment(session="s1", at=1500.0)  # the session went on past Observer's last event
        card = json.loads(self.card(n, "--json").stdout)
        self.assertEqual((card["window"]["end"], card["no_evidence"]), ("end_of_record", "window_open"))

    def test_the_card_only_reads_observer(self):
        n = self.window_case()
        self.ledger.close()
        before = (self.ledger_path.read_bytes(), self.ledger_path.stat().st_mtime_ns)
        self.card(n)
        self.assertEqual((self.ledger_path.read_bytes(), self.ledger_path.stat().st_mtime_ns), before)
        calls = [json.loads(line) for line in (self.tmp / "calls.jsonl").read_text().splitlines()]
        self.assertEqual({tuple(c[:4]) for c in calls}, {("--db", str(self.ledger_path), "event", "show")})

    def test_an_unknown_candidate_fails_with_a_message(self):
        out = self.card(9999, ok=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("9999", out.stdout + out.stderr)
        self.assertNotIn("Traceback", out.stderr)

    def test_ignored_is_allowed_only_on_a_serve_proven_shown(self):
        from contextmd.serve import card
        n = self.window_case()  # a context serve: nothing proves the model saw it
        unproven = json.loads(self.card(n, "--json").stdout)
        self.assertEqual(unproven["shown"], "unproven")
        self.assertNotIn("ignored", unproven["allowed_labels"])
        self.assertIn("followed", unproven["allowed_labels"])
        breach = {"candidate": n, "label": "ignored", "governed_action": 503, "events": [503], "reason": "x"}
        with self.assertRaises(card.LabelError):
            card.check_label(unproven, breach)
        card.check_label(unproven, {**breach, "label": None})  # a breach nobody proved was seen: unlabeled
        self.assertIn("ignored", card.allowed_labels({**unproven, "shown": "yes"}))
        self.assertNotIn("ignored", card.allowed_labels({**unproven, "shown": "no"}))

    def test_a_shown_deny_allows_ignored(self):
        from contextmd.serve import card
        n, tool = self.served(seam="deny", kind="pre_tool", checked="shown", lift="read")
        self.event(500, "tool_call", 1000.0, native_id=tool, input={"command": "gh issue create"})
        shown = json.loads(self.card(n, "--json").stdout)
        self.assertEqual(shown["shown"], "yes")
        self.assertIn("ignored", shown["allowed_labels"])
        card.check_label(shown, {"candidate": n, "label": "ignored", "governed_action": 500, "events": [500],
                                 "reason": "x"})

    def test_a_hidden_serve_says_only_what_the_check_recorded(self):
        n, tool = self.served(seam="deny", kind="pre_tool", outcome="hidden", checked="hidden")
        self.event(500, "tool_call", 1000.0, native_id=tool, input={"command": "gh issue create"})
        happened = self.card(n).stdout.splitlines()[1]
        self.assertNotIn("not shown", happened)
        self.assertIn("Chromeria's check did not find its text in the call's recorded output", happened)

    def test_a_card_with_no_evidence_allows_no_label(self):
        from contextmd.serve import card
        n, _ = self.served()
        unanchored = json.loads(self.card(n, "--json").stdout)
        self.assertEqual(unanchored["allowed_labels"], [])
        with self.assertRaises(card.LabelError):
            card.check_label(unanchored, {"candidate": n, "label": "followed", "governed_action": None,
                                          "events": [], "reason": "x"})

    def test_the_label_prompt_names_every_label_and_the_output(self):
        from contextmd.serve import card
        prompt = PROMPT.read_text()
        for label in card.LABELS:
            self.assertIn(f"`{label}`", prompt)
        for key in ("candidate", "label", "governed_action", "events", "reason"):
            self.assertIn(f'"{key}"', prompt)
        self.assertIn("`allowed_labels`", prompt)  # the card, not the judge, decides when `ignored` may be used
        self.assertIn('`window.end` is `"cap"`', prompt)  # and why `no_governed_action` may be missing


if __name__ == "__main__":
    unittest.main()
