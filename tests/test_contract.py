"""Serving contract: the real hook against a Chromeria dev server with the serving module.

PR #23's hook-seam tests, ported: each test posts real harness events through
``bin/contextmd-hook`` to a Chromeria server on a temporary base directory and
port, and reads the serving log. Runs only when ``CONTEXTMD_CHROMERIA`` names a
Chromeria checkout with the serving module (``apps/server/src/contextmd``) and
its dependencies installed; Node must be on PATH. Chromeria resolves a moment
to a run by the run attempt that is ``running`` for the hook's session, so each
test writes those rows into the dev server's own database. The server reads the
test's store through ``CONTEXTMD_SERVING_CONFIG``.
"""

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextmd.serve import install  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "contextmd-serve"
HOOK = ROOT / "bin" / "contextmd-hook"
CHROMERIA = os.environ.get("CONTEXTMD_CHROMERIA")
SCRUB = ("CLAUDE", "T3CODE_", "T3_", "ELECTRON_RUN_AS_NODE", "AI_AGENT")


def entry(directory, name, body, **fields):
    lines = ["---"]
    for k, v in fields.items():
        for value in v if isinstance(v, list) else [v]:
            lines.append(f"{k.replace('_', '-')}: {value}")
    lines += ["---", body, ""]
    path = Path(directory) / f"{name}.md"
    path.write_text("\n".join(lines))
    return path


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """One Chromeria dev server on its own base directory and port."""

    def __init__(self, root: Path, cfg: Path | None):
        self.base = root / "base"
        (self.base / "userdata").mkdir(parents=True)
        self.t3 = root / "t3"
        self.t3.write_text(f"#!/bin/sh\nexec node {CHROMERIA}/apps/server/src/bin.ts \"$@\"\n")
        self.t3.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUB)}
        if cfg is not None:
            env["CONTEXTMD_SERVING_CONFIG"] = str(cfg)
        work = root / "work"
        work.mkdir()
        self.log = open(root / "server.log", "w")
        self.proc = subprocess.Popen(
            ["node", f"{CHROMERIA}/apps/server/src/bin.ts", "serve", "--base-dir", str(self.base),
             "--port", str(free_port()), "--host", "127.0.0.1", "--no-browser"],
            cwd=work, env=env, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        runtime = self.base / "userdata" / "server-runtime.json"
        for _ in range(600):
            if runtime.exists() and str(self.proc.pid) in runtime.read_text():
                break
            if self.proc.poll() is not None:
                raise RuntimeError(f"dev server exited; see {root / 'server.log'}")
            time.sleep(0.1)
        else:
            raise RuntimeError("dev server did not start")
        install.issue_credential(self.base, str(self.t3))
        self.db = self.base / "userdata" / "chromeria-v2.sqlite"

    def stop(self):
        os.killpg(self.proc.pid, signal.SIGKILL)  # the process group this test started
        self.proc.wait()
        self.log.close()

    def attempt(self, session, run, status="running"):
        payload = json.dumps({"id": f"attempt-{run}", "runId": run, "nativeThreadId": session, "status": status})
        with sqlite3.connect(self.db, timeout=5) as db:
            db.execute(
                "INSERT INTO orchestration_v2_projection_run_attempts (attempt_id, thread_id, run_id, attempt_ordinal,"
                " root_node_id, provider, provider_instance_id, provider_thread_id, provider_turn_id, status,"
                " payload_json) VALUES (?, ?, ?, 1, 'node', 'claudeAgent', 'claudeAgent', 'pt', NULL, ?, ?)",
                (f"attempt-{run}", f"thread-{session}", run, status, payload))

    def end(self, run):
        with sqlite3.connect(self.db, timeout=5) as db:
            db.execute("UPDATE orchestration_v2_projection_run_attempts SET status = 'completed' WHERE run_id = ?",
                       (run,))

    def project_denied_call(self, session, tool_use_id, shown):
        """What Claude Code's adapter projects for a denied call: the text the model saw."""
        payload = json.dumps({"type": "command_execution", "output": f"Error: PreToolUse:Bash hook error: {shown}",
                              "nativeItemRef": {"driver": "claudeAgent", "nativeId": tool_use_id, "strength": "strong"}})
        with sqlite3.connect(self.db, timeout=5) as db:
            db.execute(
                "INSERT INTO orchestration_v2_projection_turn_items (turn_item_id, thread_id, run_id, ordinal, type,"
                " status, updated_at, payload_json) VALUES (?, ?, NULL, 1, 'command_execution', 'failed', '', ?)",
                (f"turn-item:provider:claudeAgent:native-item:{tool_use_id}", f"thread-{session}", payload))


@unittest.skipUnless(CHROMERIA, "set CONTEXTMD_CHROMERIA to a Chromeria checkout with the serving module")
class ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.keys_dir = root / "keys"
        cls.keys_dir.mkdir()
        cls.repo = root / "repo"
        (cls.repo / ".git").mkdir(parents=True)
        (cls.repo / ".git" / "HEAD").write_text("0123abcd\n")
        cls.start = root / "PREFS.md"
        cls.start.write_text("Prefer small diffs.")
        cls.cfg = root / "serving.json"
        cls.write_config()
        cls.server = Server(root / "chromeria", cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmp.cleanup()

    @classmethod
    def write_config(cls, **over):
        cfg = {"exact_keys": [str(cls.keys_dir)], "session_start": [{"path": str(cls.start), "max_chars": 4000}]}
        cfg.update(over)
        cls.cfg.write_text(json.dumps(cfg))

    def setUp(self):
        for p in self.keys_dir.iterdir():
            if p.is_file():
                p.unlink()
        self.write_config()
        self.n = 0
        self.session = f"{self._testMethodName}-s1"
        self.run_id = f"{self._testMethodName}-r1"
        self.server.attempt(self.session, self.run_id)

    def new_turn(self):
        self.server.end(self.run_id)
        self.run_id += "x"
        self.server.attempt(self.session, self.run_id)

    def raw(self, event, **fields):
        return {"hook_event_name": event, "session_id": self.session, "cwd": str(self.repo), **fields}

    def fire_raw(self, harness, raw, base=None):
        done = subprocess.run(["/bin/sh", str(HOOK), harness, str(base or self.server.base)], input=json.dumps(raw),
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0)
        return done.stdout

    def fire(self, event, **fields):
        out = self.fire_raw("claude", self.raw(event, **fields))
        return json.loads(out)["hookSpecificOutput"] if out else None

    def bash(self, event, command, **fields):
        self.n += 1
        return self.fire(event, tool_name="Bash", tool_input={"command": command}, tool_use_id=f"t{self.n}", **fields)

    def log(self, session=True):
        args = ["--session", self.session] if session else []
        out = subprocess.run([sys.executable, "-I", str(BIN), "log", "--base", str(self.server.base), *args],
                             capture_output=True, text=True, check=True).stdout
        return [json.loads(line) for line in out.splitlines()]

    def outcomes(self, entry_id):
        return [s["outcome"] for r in self.log() for s in r["serves"] if s.get("entry") == entry_id]

    def test_pointer_deny_lifts_on_read_and_post_tool_serves_the_rest(self):
        rule = self.repo / "PR-RULES.md"
        rule.write_text("Answer every PR template prompt.")
        entry(self.keys_dir, "pr-template", "A PR rule applies.", command="gh pr create", must_know_before="true",
              target=str(rule))
        entry(self.keys_dir, "pr-labels", "Label PRs with the area.", command="gh pr")
        self.fire("UserPromptSubmit", prompt="open the PR")
        first = self.bash("PreToolUse", "cd x && gh pr create --fill")
        self.assertEqual(first["permissionDecision"], "deny")
        self.assertIn(f"Read {os.path.realpath(rule)}", first["permissionDecisionReason"])
        self.assertNotIn("Label PRs", first["permissionDecisionReason"])
        # Not read yet: the retry is denied again.
        self.assertEqual(self.bash("PreToolUse", "gh pr create --fill")["permissionDecision"], "deny")
        self.fire("PostToolUse", tool_name="Read", tool_input={"file_path": str(rule)}, tool_use_id="read1",
                  tool_response={})
        self.assertIsNone(self.bash("PreToolUse", "gh pr create --fill"))
        after = self.bash("PostToolUse", "gh pr create --fill", tool_response={"stdout": "ok"})
        self.assertIn("Label PRs with the area.", after["additionalContext"])
        # A new run may serve the labels again; the read still holds for this context epoch.
        self.new_turn()
        self.assertIsNone(self.bash("PreToolUse", "gh pr create"))
        # Editing the entry makes a new version, which denies again until read again.
        entry(self.keys_dir, "pr-template", "A PR rule applies, v2.", command="gh pr create", must_know_before="true",
              target=str(rule))
        self.assertIsNone(self.bash("PreToolUse", "gh pr create"))  # the read of the target counts for v2 too
        # Another session is denied on its own.
        self.session = self.session + "-other"
        self.server.attempt(self.session, self.run_id + "-other")
        self.assertEqual(self.bash("PreToolUse", "gh pr create")["permissionDecision"], "deny")

    def test_a_path_key_matches_files_a_shell_command_names(self):
        # toolboxmd/chromeria#234: the forms agents use for one database (rocky Observer).
        db = self.repo / "userdata" / "chromeria-v2.sqlite"
        entry(self.keys_dir, "db", "Open the projection database read-only.", path="**/chromeria-v2.sqlite",
              must_know_before="true")
        forms = [
            f"sqlite3 {db} 'select 1'",
            f"sqlite3 'file:{db}?mode=ro' 'select 1'",
            f'DB={db}; sqlite3 -readonly "$DB" .tables',
            "sqlite3 -readonly ~/.t3/userdata/chromeria-v2.sqlite .tables",
            "cd userdata && sqlite3 ../userdata/chromeria-v2.sqlite .tables",
        ]
        for i, command in enumerate(forms):
            self.session = f"{self._testMethodName}-form{i}"
            self.server.attempt(self.session, f"{self.run_id}-{i}")
            out = self.bash("PreToolUse", command)
            self.assertEqual((out or {}).get("permissionDecision"), "deny", command)
        self.session = f"{self._testMethodName}-plain"
        self.server.attempt(self.session, f"{self.run_id}-plain")
        self.assertIsNone(self.bash("PreToolUse", "curl -s https://example.com/chromeria-v2.sqlite.gz"))

    def test_inline_deny_lifts_only_when_the_projection_shows_it(self):
        entry(self.keys_dir, "trailer", "Commits carry Refs: WID-42.", command="git commit", must_know_before="true")
        first = self.bash("PreToolUse", "git commit -m x")
        self.assertEqual(first["permissionDecision"], "deny")
        # Another hook's reason is what the model saw: the rule stays armed.
        self.server.project_denied_call(self.session, f"t{self.n}", "OTHER-HOOK: read something else")
        second = self.bash("PreToolUse", "git commit -m x")
        self.assertIn("Refs: WID-42", second["permissionDecisionReason"])
        self.assertEqual(self.outcomes("trailer"), ["hidden", "awaiting_evidence"])
        self.server.project_denied_call(self.session, f"t{self.n}", second["permissionDecisionReason"])
        self.assertIsNone(self.bash("PreToolUse", "git commit -m x"))

    def test_inline_deny_lifts_on_settlement_without_evidence(self):
        entry(self.keys_dir, "trailer", "Commits carry Refs: WID-42.", command="git commit", must_know_before="true")
        codex = lambda: self.fire_raw("codex", {"hook_event_name": "PreToolUse", "session_id": self.session,
                                                "cwd": str(self.repo), "tool_name": "Bash", "tool_use_id": "exec-1",
                                                "tool_input": {"command": "git commit -m x"}})
        self.assertIn("deny", codex())
        self.assertEqual(codex(), "")

    def test_path_and_error_keys_including_a_printed_exit(self):
        entry(self.keys_dir, "skills", "SKILL.md edits need a pressure scenario.", path="**/SKILL.md")
        entry(self.keys_dir, "ssh", "Use the deploy key alias.", error="Permission denied (publickey)")
        self.fire("UserPromptSubmit", prompt="x")
        out = self.fire("PostToolUse", tool_name="Edit", tool_use_id="e1", tool_response={},
                        tool_input={"file_path": str(self.repo / "skills/ops/SKILL.md")})
        self.assertIn("pressure scenario", out["additionalContext"])
        out = self.bash("PostToolUseFailure", "git push", error="git@github.com: Permission denied (publickey).")
        self.assertIn("deploy key alias", out["additionalContext"])
        self.new_turn()
        out = self.bash("PostToolUse", 'git push; echo "exit=$?"',
                        tool_response={"stdout": "Permission denied (publickey).\nexit=128", "stderr": ""})
        self.assertIn("deploy key alias", out["additionalContext"])
        self.assertIsNone(self.bash("PostToolUse", "git status", tool_response={"stdout": "clean"}))

    def test_serve_budget_first_fit_and_cut_logged(self):
        for i in range(8):
            entry(self.keys_dir, f"e{i}", f"rule {i} " + "y" * 1490, command=f"tool{i}")
        served = [self.bash("PostToolUse", f"tool{i}", tool_response={}) for i in range(8)]
        self.assertEqual(sum(s is not None for s in served), 5)
        self.assertLessEqual(sum(len(s["additionalContext"].encode()) for s in served if s), 8_000)
        cuts = [s for r in self.log() for s in r["serves"] if s["outcome"] == "cut_budget"]
        self.assertEqual([(c["entry"], c["reason"]) for c in cuts], [("e5", "turn"), ("e6", "turn"), ("e7", "turn")])
        self.new_turn()
        self.assertIsNotNone(self.bash("PostToolUse", "tool7", tool_response={}))

    def test_one_output_stays_under_the_harness_limit_in_bytes(self):
        entry(self.keys_dir, "a", "a" * 1500, command="make")
        entry(self.keys_dir, "b", "\U0001F600" * 375, command="make")
        out = self.bash("PostToolUse", "make", tool_response={})
        self.assertLess(len(out["additionalContext"].encode()), 2000)
        self.assertEqual(self.outcomes("b"), ["cut_budget"])

    def test_session_start_pack_and_compaction(self):
        self.assertEqual(self.fire("SessionStart", source="startup")["additionalContext"], "Prefer small diffs.")
        self.fire("UserPromptSubmit", prompt="x")
        self.assertEqual(self.fire("SessionStart", source="compact")["additionalContext"], "Prefer small diffs.")
        self.assertEqual([(r["kind"], r["epoch"]) for r in self.log()],
                         [("session_start", 0), ("user_message", 0), ("session_start", 1)])

    def test_log_records_ids_and_outcomes_without_window_text(self):
        entry(self.keys_dir, "pr", "Rule text.", command="gh pr create", must_know_before="true")
        self.fire("UserPromptSubmit", prompt="SECRET-PROMPT")
        self.bash("PreToolUse", "gh pr create --body SECRET-BODY")
        self.fire("PreToolUse", tool_name="mcp__x__y", tool_input={"q": "SECRET-ARG"}, tool_use_id="t9")
        self.fire("Stop", stop_hook_active=False)
        raw = json.dumps(self.log())
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("Rule text.", raw)
        recs = self.log()
        self.assertEqual([r["kind"] for r in recs], ["user_message", "pre_tool", "pre_tool", "before_done"])
        deny = recs[1]
        self.assertEqual((deny["tool_use_id"], deny["run_id"], deny["status"]), ("t1", self.run_id, "ok"))
        s = deny["serves"][0]
        self.assertEqual((s["matcher"], s["entry"], s["outcome"], s["seam"]), ("exact_key", "pr", "awaiting_evidence", "deny"))
        self.assertEqual(len(s["version"]), 12)
        self.assertEqual(recs[2]["serves"][0]["outcome"], "unsupported_key")

    def test_owner_commit_is_recorded(self):
        git = self.keys_dir / ".git"
        (git / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        (git / "refs" / "heads" / "main").write_text("feedface\n")
        try:
            entry(self.keys_dir, "a", "A.", command="ls")
            self.bash("PostToolUse", "ls", tool_response={})
            self.assertEqual(self.log()[-1]["owners"], {str(self.keys_dir): "feedface"})
        finally:
            subprocess.run(["rm", "-rf", str(git)], check=True)

    def test_off_switch_serves_and_logs_nothing(self):
        entry(self.keys_dir, "pr", "Rule.", command="gh pr create", must_know_before="true")
        base = ["--base", str(self.server.base)]
        subprocess.run([sys.executable, "-I", str(BIN), "off", *base], check=True, capture_output=True)
        try:
            self.assertIsNone(self.bash("PreToolUse", "gh pr create"))
            self.assertEqual(self.log(), [])
        finally:
            subprocess.run([sys.executable, "-I", str(BIN), "on", *base], check=True, capture_output=True)
        self.assertEqual(self.bash("PreToolUse", "gh pr create")["permissionDecision"], "deny")

    def test_hidden_commands_are_denied(self):
        entry(self.keys_dir, "pr", "Rule.", command="gh pr create", must_know_before="true")
        for command in ["echo ok &&\ngh pr create", "printf '%s' '>'&gh pr create",
                        "printf '%s\\n' '<<EOF'\ngh pr create\nEOF", 'bash -c "gh pr create"']:
            # One session each: the fourth deny in one session would pass under the cap of 3.
            self.session = f"hidden-{self.n}"
            self.server.attempt(self.session, f"hidden-run-{self.n}")
            out = self.bash("PreToolUse", command)
            self.assertEqual(out and out["permissionDecision"], "deny", command)

    def test_killed_hook_does_not_spend_the_deny(self):
        entry(self.keys_dir, "pr", "P" * 600, command="gh pr create", must_know_before="true")
        self.n += 1
        raw = self.raw("PreToolUse", tool_name="Bash", tool_input={"command": "gh pr create"}, tool_use_id="tk")
        r, w = os.pipe()
        os.set_blocking(w, False)
        try:
            while True:
                os.write(w, b"x" * 65536)  # a full pipe: the hook blocks writing the reply
        except BlockingIOError:
            pass
        os.set_blocking(w, True)
        p = subprocess.Popen(["/bin/sh", str(HOOK), "claude", str(self.server.base)], stdin=subprocess.PIPE,
                             stdout=w, stderr=subprocess.DEVNULL, start_new_session=True)
        p.stdin.write(json.dumps(raw).encode())
        p.stdin.close()
        for _ in range(100):
            if self.outcomes("pr") == ["reserved"]:
                break
            time.sleep(0.05)
        else:
            self.fail("the hook never reserved")
        os.killpg(p.pid, signal.SIGKILL)  # the hook and its curl, started above
        p.wait()
        os.close(w)
        os.close(r)
        time.sleep(2.2)  # past the release window
        out = self.bash("PreToolUse", "gh pr create")
        self.assertEqual(out and out["permissionDecision"], "deny")
        # A Claude Code deny stays awaiting_evidence until the projection shows what the model saw.
        self.assertEqual(self.outcomes("pr"), ["delivery_unverified", "awaiting_evidence"])

    def test_closed_stdout_leaves_the_deny_unsettled(self):
        entry(self.keys_dir, "pr", "Rule.", command="gh pr create", must_know_before="true")
        p = subprocess.Popen(["/bin/sh", str(HOOK), "claude", str(self.server.base)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p.stdout.close()
        p.stdin.write(json.dumps(self.raw("PreToolUse", tool_name="Bash", tool_input={"command": "gh pr create"},
                                          tool_use_id="tc")).encode())
        p.stdin.close()
        self.assertEqual(p.wait(timeout=10), 0)
        time.sleep(2.2)
        self.assertEqual(self.bash("PreToolUse", "gh pr create")["permissionDecision"], "deny")
        # A Claude Code deny stays awaiting_evidence until the projection shows what the model saw.
        self.assertEqual(self.outcomes("pr"), ["delivery_unverified", "awaiting_evidence"])

    def test_broken_config_fails_open(self):
        entry(self.keys_dir, "pr", "Rule.", command="gh pr create", must_know_before="true")
        self.cfg.write_text("{not json")
        self.assertIsNone(self.bash("PreToolUse", "gh pr create"))
        last = self.log(session=False)[-1]
        self.assertEqual((last["status"], last["session"]), ("bad_config", None))

    def test_an_unjoined_session_is_not_served(self):
        entry(self.keys_dir, "pr", "Rule.", command="gh pr create", must_know_before="true")
        self.session = "nobody"
        self.assertIsNone(self.bash("PreToolUse", "gh pr create"))
        self.assertEqual(self.log()[-1]["status"], "unjoined")

    def test_a_second_server_rejects_this_servers_credential(self):
        other = Server(Path(self.tmp.name) / f"second-{time.time_ns()}", None)
        try:
            token = json.loads(install.paths(self.server.base)["credential"].read_text())["token"]
            url = json.loads((other.base / "userdata" / "server-runtime.json").read_text())["origin"]
            done = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
                                   "-H", f"authorization: Bearer {token}", "-H", "x-contextmd-harness: claude",
                                   "-H", "x-contextmd-delivery: d", "--data-binary", "{}",
                                   f"{url}/contextmd/v1/moment"], capture_output=True, text=True)
            self.assertEqual(done.stdout, "401")
        finally:
            other.stop()

    def test_server_down_fails_open_fast(self):
        down = Path(self.tmp.name) / "down"
        (down / "userdata" / "contextmd").mkdir(parents=True)
        (down / "userdata" / "server-runtime.json").write_text(json.dumps({"origin": f"http://127.0.0.1:{free_port()}"}))
        (down / "userdata" / "contextmd" / "hook-credential").write_text(json.dumps({"token": "t"}))
        started = time.time()
        self.assertEqual(self.fire_raw("claude", self.raw("PreToolUse", tool_name="Bash",
                                                          tool_input={"command": "ls"}), base=down), "")
        self.assertLess(time.time() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
