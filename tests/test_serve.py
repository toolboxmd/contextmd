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

    def test_long_entry_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            entry(d, "long", "Rule.", id="x" * 121, command="gh pr create")
            st = store.load([d])
            self.assertEqual(st.entries, [])
            self.assertIn("id is 121 characters, over 120", st.errors[0])

    def test_version_changes_with_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = entry(d, "a", "one", command="ls")
            v1 = store.parse(p).version
            entry(d, "a", "two", command="ls")
            self.assertNotEqual(v1, store.parse(p).version)



class TargetTest(unittest.TestCase):
    def test_target_makes_a_pointer_rule_relative_to_the_entry(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "procedures").mkdir()
            p = entry(d, "vc", "Read the procedure.", command="git push", must_know_before="true",
                      target="procedures/vc.md")
            e = store.parse(p)
            self.assertEqual(e.target, os.path.realpath(Path(d) / "procedures" / "vc.md"))

    def test_target_needs_must_know_before(self):
        with tempfile.TemporaryDirectory() as d:
            entry(d, "vc", "Read it.", command="git push", target="x.md")
            st = store.load([d])
            self.assertEqual(st.entries, [])
            self.assertIn("target is only for must-know-before entries", st.errors[0])


class InstallTest(unittest.TestCase):
    """Install against a temporary home, with stand-ins for Chromeria's `t3` and `codex`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.base = self.root.resolve() / "t3"
        (self.base / "userdata").mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "t3-calls"
        (self.bin / "t3").write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> {self.calls}\n'
            'case "$3" in issue) n=$(wc -l < ' + str(self.calls) + ' | tr -d " ");'
            ' echo "{\\"sessionId\\": \\"s$n\\", \\"token\\": \\"tok$n\\", \\"expiresAt\\": \\"2027-01-01\\"}";; esac\n')
        (self.bin / "t3").chmod(0o755)
        (self.bin / "codex").write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "hooks = os.path.join(os.environ['CODEX_HOME'], 'hooks.json')\n"
            "for line in sys.stdin:\n"
            "    m = json.loads(line)\n"
            "    if 'id' not in m: continue\n"
            "    res = {}\n"
            "    if m['method'] == 'hooks/list':\n"
            "        cmds = [h['command'] for gs in json.load(open(hooks))['hooks'].values() for g in gs for h in g['hooks']]\n"
            "        res = {'data': [{'hooks': [{'key': f'{hooks}:e:{i}', 'command': c, 'sourcePath': hooks,\n"
            "                 'currentHash': f'sha256:{i}'} for i, c in enumerate(cmds)]}]}\n"
            "    print(json.dumps({'id': m['id'], 'result': res}), flush=True)\n")
        (self.bin / "codex").chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, check=True):
        env = dict(os.environ, HOME=str(self.home), PATH=f"{self.bin}:{os.environ['PATH']}")
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("CODEX_HOME", None)
        return subprocess.run([sys.executable, "-I", str(BIN), *args], capture_output=True, text=True, env=env,
                              check=check, timeout=30)

    def test_claude_install_keeps_other_hooks_and_is_idempotent(self):
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        other = {"hooks": [{"type": "command", "command": "/usr/local/bin/other-hook"}]}
        settings.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [other]}}))
        self.run_cli("install", "claude", "--base", str(self.base))
        self.run_cli("install", "claude", "--base", str(self.base))
        data = json.loads(settings.read_text())
        self.assertEqual(data["model"], "x")
        pre = data["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], other)
        self.assertEqual(sum("contextmd-hook" in g["hooks"][0]["command"] for g in pre), 1)
        self.assertIn(str(self.base), pre[1]["hooks"][0]["command"])
        cred = self.base / "userdata" / "contextmd" / "hook-credential"
        self.assertEqual(json.loads(cred.read_text())["token"], "tok2")
        self.assertEqual(cred.stat().st_mode & 0o777, 0o600)
        calls = self.calls.read_text().splitlines()
        self.assertIn("--subject contextmd-hook", calls[0])
        self.assertIn("--scope orchestration:read", calls[0])
        self.assertEqual(calls[1:], [f"auth session issue --base-dir {self.base} --subject contextmd-hook --label "
                                     f"ContextMD hooks --scope orchestration:read --ttl 365d --json",
                                     f"auth session revoke --base-dir {self.base} s1"])

    def test_codex_install_trusts_only_contextmd_hooks(self):
        codex = self.home / ".codex"
        codex.mkdir(parents=True)
        (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "/usr/local/bin/other-hook"}]}]}}))
        self.run_cli("install", "codex", "--base", str(self.base))
        toml = (codex / "config.toml").read_text()
        hooks = json.loads((codex / "hooks.json").read_text())["hooks"]
        n_ours = sum(1 for gs in hooks.values() for g in gs if "contextmd-hook" in g["hooks"][0]["command"])
        self.assertEqual(toml.count("trusted_hash"), n_ours)
        self.assertNotIn(":e:0\"]", toml)  # the other hook stays untrusted by us

    def test_grok_and_opencode_files(self):
        self.run_cli("install", "grok", "--base", str(self.base))
        grok = json.loads((self.home / ".grok" / "hooks" / "contextmd.json").read_text())
        self.assertIn("PostCompact", grok["hooks"])
        self.assertIn("PreToolUse", grok["hooks"])
        self.run_cli("install", "opencode", "--base", str(self.base))
        plugin = (self.home / ".config" / "opencode" / "plugins" / "contextmd.js").read_text()
        self.assertIn(json.dumps(str(self.base)), plugin)
        self.assertNotIn("const BASE = \"__BASE__\"", plugin)

    def test_refuses_a_directory_that_is_not_a_chromeria_base(self):
        done = self.run_cli("install", "claude", "--base", str(self.root / "nothing"), check=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("not a Chromeria base directory", done.stdout)


class HookScriptTest(unittest.TestCase):
    """The shell hook against a stand-in server: credential, settle and failing open."""

    def test_prints_the_reply_settles_and_fails_open(self):
        import http.server
        import threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                seen.append((self.path, self.headers.get("authorization"), self.headers.get("x-contextmd-harness"),
                             body))
                if self.path.endswith("/moment"):
                    out = b'{"hookSpecificOutput":{"additionalContext":"rule"}}'
                    self.send_response(200)
                    self.send_header("content-length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                else:
                    self.send_response(204)
                    self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        hook = BIN.parent / "contextmd-hook"
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "userdata" / "contextmd").mkdir(parents=True)
            (base / "userdata" / "server-runtime.json").write_text(
                json.dumps({"port": server.server_port, "origin": f"http://127.0.0.1:{server.server_port}"}))
            (base / "userdata" / "contextmd" / "hook-credential").write_text(json.dumps({"token": "tok"}))
            run = lambda: subprocess.run(["/bin/sh", str(hook), "claude", str(base)], input='{"x": 1}',
                                         capture_output=True, text=True, timeout=10)
            done = run()
            self.assertEqual((done.returncode, done.stdout), (0, '{"hookSpecificOutput":{"additionalContext":"rule"}}'))
            self.assertEqual([s[0].rsplit("/", 1)[0] for s in seen], ["/contextmd/v1", "/contextmd/v1/settle"])
            self.assertEqual(seen[0][1:], ("Bearer tok", "claude", b'{"x": 1}'))
            (base / "userdata" / "contextmd" / "off").touch()
            self.assertEqual(run().stdout, "")
            self.assertEqual(len(seen), 2)
            (base / "userdata" / "contextmd" / "off").unlink()
            server.shutdown()
            server.server_close()
            started = time.time()
            down = run()
            self.assertEqual((down.returncode, down.stdout), (0, ""))
            self.assertLess(time.time() - started, 1.5)


if __name__ == "__main__":
    unittest.main()
