import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextmd.serve import keys, store  # noqa: E402

BIN = Path(__file__).resolve().parents[1] / "bin" / "contextmd-serve"


def entry(directory, name, body, **fields):
    lines = ["---"]
    for k, v in fields.items():
        for value in v if isinstance(v, list) else [v]:
            lines.append(f"{k.replace('_', '-')}: {value}")
    lines += ["---", body, ""]
    path = Path(directory) / f"{name}.md"
    path.write_text("\n".join(lines))
    return path


class SegmentsTest(unittest.TestCase):
    def test_command_key_matches_words_anywhere(self):
        def hit(command, key="gh pr create"):
            e = store.Entry("e", "v", "t", commands=[key.split()])
            return e.matches(keys.extract("Bash", {"command": command}, None)) is not None
        for command in ["gh pr create", "cd x && env A=1 /usr/local/bin/gh pr create --fill",
                        "echo ok &&\ngh pr create", "printf '%s' '>'&gh pr create",
                        "printf '%s' \\>&gh pr create", "printf '%s\\n' '<<EOF'\ngh pr create\nEOF",
                        'bash -c "gh pr create"', 'echo "$(gh pr create)"', "x=`gh pr create`",
                        "{ gh pr create; }", "printf '%s' ';' gh pr create"]:
            self.assertTrue(hit(command), command)
        for command in ["gh pr list", "gh issue create", "ghx pr create", "echo gh pr", "gh pr createx"]:
            self.assertFalse(hit(command), command)

    def test_glob(self):
        rx = keys.glob_regex("**/SKILL.md")
        self.assertTrue(rx.match("SKILL.md"))
        self.assertTrue(rx.match("skills/ops/SKILL.md"))
        self.assertFalse(rx.match("skills/ops/SKILL.md.bak"))
        self.assertTrue(keys.glob_regex("docs/*.md").match("docs/a.md"))
        self.assertFalse(keys.glob_regex("docs/*.md").match("docs/x/a.md"))


class StoreTest(unittest.TestCase):
    def test_bad_entries_are_reported_not_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            entry(d, "good", "Use the template.", command="gh pr create")
            entry(d, "nokey", "No key here.")
            entry(d, "big", "x" * (store.MAX_ENTRY_BYTES + 1), command="ls")
            (Path(d) / "README.md").write_text("not an entry")
            st = store.load([d])
            self.assertEqual([e.id for e in st.entries], ["good"])
            self.assertEqual(len(st.errors), 2)

    def test_version_changes_with_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = entry(d, "a", "one", command="ls")
            v1 = store.parse(p).version
            entry(d, "a", "two", command="ls")
            self.assertNotEqual(v1, store.parse(p).version)


class HookSeamTest(unittest.TestCase):
    """Real hook process: Claude Code event JSON on stdin, hook output on stdout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.keys_dir = root / "keys"
        self.keys_dir.mkdir()
        self.repo = root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / ".git" / "HEAD").write_text("0123abcd\n")
        self.state = root / "state"
        self.start = root / "PREFS.md"
        self.start.write_text("Prefer small diffs.")
        self.cfg = root / "serving.json"
        self.write_config()
        self.session = "s1"

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, **over):
        cfg = {"exact_keys": [str(self.keys_dir)], "state_dir": str(self.state),
               "session_start": [{"path": str(self.start), "max_chars": 4000}]}
        cfg.update(over)
        self.cfg.write_text(json.dumps(cfg))

    def run_cli(self, *args, stdin=""):
        env = dict(os.environ, CONTEXTMD_SERVING_CONFIG=str(self.cfg))
        return subprocess.run([sys.executable, "-I", str(BIN), *args], input=stdin,
                              capture_output=True, text=True, env=env, check=True)

    def fire(self, event, **fields):
        ev = {"hook_event_name": event, "session_id": self.session, "cwd": str(self.repo), **fields}
        out = self.run_cli("hook", "claude-code", stdin=json.dumps(ev)).stdout
        return json.loads(out)["hookSpecificOutput"] if out else None

    def bash(self, event, command, n, **fields):
        return self.fire(event, tool_name="Bash", tool_input={"command": command}, tool_use_id=f"t{n}", **fields)

    def log(self):
        return [json.loads(line) for line in self.run_cli("log").stdout.splitlines()]

    def test_deny_once_then_retry_passes_and_post_tool_serves_the_rest(self):
        entry(self.keys_dir, "pr-template", "Answer every PR template prompt.",
              command="gh pr create", must_know_before="true")
        entry(self.keys_dir, "pr-labels", "Label PRs with the area.", command="gh pr")
        self.fire("UserPromptSubmit", prompt="open the PR")
        first = self.bash("PreToolUse", "cd x && gh pr create --fill", 1)
        self.assertEqual(first["permissionDecision"], "deny")
        self.assertIn("Answer every PR template prompt.", first["permissionDecisionReason"])
        self.assertNotIn("Label PRs", first["permissionDecisionReason"])
        self.assertIsNone(self.bash("PreToolUse", "gh pr create --fill", 2))
        after = self.bash("PostToolUse", "gh pr create --fill", 2)
        self.assertIn("Label PRs with the area.", after["additionalContext"])
        self.assertNotIn("Answer every PR template", after["additionalContext"])
        # A new turn may serve the labels again; the deny stays spent for this version.
        self.fire("UserPromptSubmit", prompt="again")
        self.assertIsNone(self.bash("PreToolUse", "gh pr create", 3))
        # Editing the entry makes a new version, which denies once more.
        entry(self.keys_dir, "pr-template", "Answer every PR template prompt, v2.",
              command="gh pr create", must_know_before="true")
        self.assertEqual(self.bash("PreToolUse", "gh pr create", 4)["permissionDecision"], "deny")
        # Another session is denied on its own.
        self.session = "s2"
        self.assertEqual(self.bash("PreToolUse", "gh pr create", 5)["permissionDecision"], "deny")

    def test_path_and_error_keys(self):
        entry(self.keys_dir, "skills", "SKILL.md edits need a pressure scenario.", path="**/SKILL.md")
        entry(self.keys_dir, "ssh", "Use the deploy key alias.", error="Permission denied (publickey)")
        self.fire("UserPromptSubmit", prompt="x")
        out = self.fire("PostToolUse", tool_name="Edit", tool_use_id="e1",
                        tool_input={"file_path": str(self.repo / "skills/ops/SKILL.md")})
        self.assertIn("pressure scenario", out["additionalContext"])
        out = self.bash("PostToolUseFailure", "git push", 2,
                        error="git@github.com: Permission denied (publickey).\nfatal: exit 128")
        self.assertIn("deploy key alias", out["additionalContext"])
        self.assertIsNone(self.bash("PostToolUse", "git push", 3))

    def test_turn_budget_first_fit_and_cut_logged(self):
        for i in range(8):
            entry(self.keys_dir, f"e{i}", f"rule {i} " + "y" * 1490, command=f"tool{i}")
        self.fire("UserPromptSubmit", prompt="x")
        served = [self.bash("PostToolUse", f"tool{i}", i) for i in range(8)]
        self.assertEqual(sum(s is not None for s in served), 6)  # 6 x ~1,520 chars fit in 10,000
        total = sum(len(s["additionalContext"]) for s in served if s)
        self.assertLessEqual(total, 10_000)
        cuts = [s for rec in self.log() for s in rec["serves"] if s["outcome"] == "cut_budget"]
        self.assertEqual([c["entry"] for c in cuts], ["e6", "e7"])
        self.assertTrue(all(c["cut"] == "turn" for c in cuts))
        # The next turn has a fresh budget.
        self.fire("UserPromptSubmit", prompt="y")
        self.assertIsNotNone(self.bash("PostToolUse", "tool7", 9))

    def test_one_moment_stays_under_the_host_spill_limit(self):
        entry(self.keys_dir, "a", "a" * 1500, command="make")
        entry(self.keys_dir, "b", "b" * 1500, command="make")
        self.fire("UserPromptSubmit", prompt="x")
        out = self.bash("PostToolUse", "make", 1)
        self.assertLess(len(out["additionalContext"]), 2000)
        cut = [s for s in self.log()[-1]["serves"] if s["outcome"] == "cut_budget"]
        self.assertEqual([(c["entry"], c["cut"]) for c in cut], [("b", "moment")])

    def test_session_start_pack_outside_budget_and_resent_after_compaction(self):
        self.write_config(turn_budget=10)
        out = self.fire("SessionStart", source="startup")
        self.assertEqual(out["additionalContext"], "Prefer small diffs.")
        self.fire("UserPromptSubmit", prompt="x")
        out = self.fire("SessionStart", source="compact")
        self.assertEqual(out["additionalContext"], "Prefer small diffs.")
        epochs = [(r["kind"], r["epoch"]) for r in self.log()]
        self.assertEqual(epochs, [("session_start", 0), ("user_message", 0), ("session_start", 1)])

    def test_log_records_ids_and_outcomes_without_window_text(self):
        entry(self.keys_dir, "pr", "Rule text.", command="gh pr create", must_know_before="true")
        self.fire("UserPromptSubmit", prompt="SECRET-PROMPT")
        self.bash("PreToolUse", "gh pr create --body SECRET-BODY", 1)
        self.fire("PreToolUse", tool_name="mcp__x__y", tool_input={"q": "SECRET-ARG"}, tool_use_id="t2")
        self.fire("Stop", stop_hook_active=False)
        raw = self.run_cli("log").stdout
        self.assertNotIn("SECRET", raw)
        recs = self.log()
        self.assertEqual([r["kind"] for r in recs], ["user_message", "pre_tool", "pre_tool", "before_done"])
        deny = recs[1]
        self.assertEqual(deny["moment"], "pre_tool:t1")
        self.assertEqual(deny["owners"], {str(self.keys_dir): None})
        s = deny["serves"][0]
        self.assertEqual((s["lane"], s["entry"], s["outcome"], s["remote"], s["seam"]),
                         ("exact_key", "pr", "injected", "not_called", "deny"))
        self.assertEqual(len(s["version"]), 12)
        self.assertEqual(recs[2]["serves"][0]["outcome"], "unsupported_key")
        for r in recs:
            self.assertEqual({"moment", "kind", "epoch", "owners", "serves"} - set(r), set())

    def test_owner_commit_is_recorded(self):
        (self.keys_dir / ".git").mkdir()
        (self.keys_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (self.keys_dir / ".git" / "refs" / "heads").mkdir(parents=True)
        (self.keys_dir / ".git" / "refs" / "heads" / "main").write_text("feedface\n")
        entry(self.keys_dir, "a", "A.", command="ls")
        self.bash("PostToolUse", "ls", 1)
        self.assertEqual(self.log()[-1]["owners"], {str(self.keys_dir): "feedface"})

    def test_off_switch_serves_and_logs_nothing(self):
        entry(self.keys_dir, "pr", "Rule.", command="gh pr create", must_know_before="true")
        self.run_cli("off")
        self.assertEqual(json.loads(self.run_cli("status").stdout)["serving"], "off")
        self.assertIsNone(self.bash("PreToolUse", "gh pr create", 1))
        self.assertIsNone(self.fire("SessionStart", source="startup"))
        self.assertEqual(self.log(), [])
        self.run_cli("on")
        self.assertEqual(self.bash("PreToolUse", "gh pr create", 2)["permissionDecision"], "deny")

    def test_hidden_commands_are_denied(self):
        entry(self.keys_dir, "pr", "Rule.", command="gh pr create", must_know_before="true")
        for n, command in enumerate(["echo ok &&\ngh pr create", "printf '%s' '>'&gh pr create",
                                     "printf '%s' \\>&gh pr create",
                                     "printf '%s\\n' '<<EOF'\ngh pr create\nEOF"]):
            self.session = f"hidden{n}"
            out = self.bash("PreToolUse", command, n)
            self.assertEqual(out and out["permissionDecision"], "deny", command)

    def test_killed_hook_does_not_spend_the_deny(self):
        entry(self.keys_dir, "pr", "Rule.", command="gh pr create", must_know_before="true")
        ev = {"hook_event_name": "PreToolUse", "session_id": self.session, "cwd": str(self.repo),
              "tool_name": "Bash", "tool_input": {"command": "gh pr create"}, "tool_use_id": "t1"}
        env = dict(os.environ, CONTEXTMD_SERVING_CONFIG=str(self.cfg))
        r, w = os.pipe()
        os.set_blocking(w, False)
        try:
            while True:
                os.write(w, b"x" * 65536)
        except BlockingIOError:
            pass
        os.set_blocking(w, True)
        # The hook reserves, then blocks writing to the full pipe; the host kills it.
        p = subprocess.Popen([sys.executable, "-I", str(BIN), "hook", "claude-code"], stdin=subprocess.PIPE,
                             stdout=w, stderr=subprocess.DEVNULL, env=env)
        p.stdin.write(json.dumps(ev).encode())
        p.stdin.close()
        for _ in range(100):
            if (self.state / "serving.db").exists() and any(
                    s["outcome"] == "pending" for r_ in self.log() for s in r_["serves"]):
                break
            time.sleep(0.05)
        p.kill()
        p.wait()
        os.close(w)
        os.close(r)
        self.assertEqual(self.bash("PreToolUse", "gh pr create", 2)["permissionDecision"], "deny")

    def test_failed_output_releases_the_deny(self):
        entry(self.keys_dir, "pr", "Rule.", command="gh pr create", must_know_before="true")
        ev = {"hook_event_name": "PreToolUse", "session_id": self.session, "cwd": str(self.repo),
              "tool_name": "Bash", "tool_input": {"command": "gh pr create"}, "tool_use_id": "t1"}
        env = dict(os.environ, CONTEXTMD_SERVING_CONFIG=str(self.cfg))
        p = subprocess.Popen([sys.executable, "-I", str(BIN), "hook", "claude-code"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        p.stdout.close()
        p.stdin.write(json.dumps(ev).encode())
        p.stdin.close()
        self.assertEqual(p.wait(timeout=10), 0)
        p.stderr.close()
        self.assertEqual(self.log()[-1]["serves"][0]["outcome"], "delivery_failed")
        self.assertEqual(self.bash("PreToolUse", "gh pr create", 2)["permissionDecision"], "deny")
        self.assertEqual(self.log()[-1]["serves"][0]["outcome"], "injected")

    def test_interrupted_delivery_is_unverified_and_released(self):
        from contextmd.serve import config as config_mod
        from contextmd.serve.server import Moment, Serving
        entry(self.keys_dir, "pr", "Rule.", command="gh pr create", must_know_before="true")
        os.environ["CONTEXTMD_SERVING_CONFIG"] = str(self.cfg)
        try:
            cfg = config_mod.load()
        finally:
            del os.environ["CONTEXTMD_SERVING_CONFIG"]
        old = Serving(cfg, clock=lambda: 1000.0)
        # The hook process dies after reserving, before its output is written.
        reply, token = old.reserve(Moment("claude-code", self.session, "pre_tool", "pre_tool:t1", str(self.repo),
                                          "Bash", {"command": "gh pr create"}))
        self.assertIsNotNone(token)
        self.assertEqual(self.log()[-1]["serves"][0]["outcome"], "pending")
        self.assertEqual(self.bash("PreToolUse", "gh pr create", 2)["permissionDecision"], "deny")
        outcomes = [r["serves"][0]["outcome"] for r in self.log()]
        self.assertEqual(outcomes, ["delivery_unverified", "injected"])

    def test_one_output_is_bounded_in_bytes(self):
        entry(self.keys_dir, "wide", "\U0001F600" * 400, command="make")  # 1,600 bytes, permitted
        self.fire("UserPromptSubmit", prompt="x")
        out = self.run_cli("hook", "claude-code", stdin=json.dumps({
            "hook_event_name": "PostToolUse", "session_id": self.session, "cwd": str(self.repo),
            "tool_name": "Bash", "tool_input": {"command": "make"}, "tool_use_id": "t1"})).stdout
        self.assertLess(len(out.encode()), 2200)

    def test_broken_state_fails_open(self):
        self.cfg.write_text("{not json")
        self.assertIsNone(self.bash("PreToolUse", "gh pr create", 1))


if __name__ == "__main__":
    unittest.main()
