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
            lines.append(f"{'name' if k == 'cue_name' else k.replace('_', '-')}: {value}")
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

    def test_command_key_follows_what_bash_runs_not_mentions(self):
        # Same cases as Chromeria's Matcher.ts (toolboxmd/chromeria#232, #243).
        def hit(command, key="gh pr create"):
            e = store.Entry("e", "v", "t", commands=[key.split()])
            return e.matches(keys.extract("Bash", {"command": command}, None)) is not None
        for command in ["zsh -lc 'gh pr create --fill'", '/bin/bash -c "cd x && gh pr create"',
                        "cat > notes.md <<'EOF'\nhello\nEOF\ngh pr create",
                        "gh pr create --title \"x\" --body-file - <<'EOF'\nbody\nEOF",
                        "cat <<EOF\n$(gh pr create)\nEOF", "cat <<EOF\n`gh pr create`\nEOF",
                        "bash -c \"'gh' pr create\"", "'gh' pr create", '"gh" pr create --fill', "g'h' pr create"]:
            self.assertTrue(hit(command), command)
        for command in ['echo "live check: gh pr create"', "echo 'gh pr create'",
                        "cat <<'EOF'\nthen run gh pr create\nEOF", "python3 - <<'PY'\nkeys = ['gh pr create']\nPY",
                        "python3 -c \"print('gh pr create')\"", 'gh issue create --title t --body "open it with gh pr create"',
                        "cat <<'EOF'\n$(gh pr create)\nEOF", 'cat <<"EOF"\n$(gh pr create)\nEOF',
                        'echo "\\$(gh pr create)"', 'echo "\\`gh pr create\\`"', "cat <<EOF\nrun gh pr create later\nEOF"]:
            self.assertFalse(hit(command), command)
        db = lambda command: hit(command, "chromeria-v2.sqlite")
        for command in ["sqlite3 chromeria-v2.sqlite .tables", "sqlite3 ~/.t3/userdata/chromeria-v2.sqlite",
                        "sqlite3 /Users/u/.t3/userdata/chromeria-v2.sqlite", "sqlite3 -readonly ~/.t3/userdata/chromeria-v2.sqlite .tables",
                        "sqlite3 /Users/u/.t3/userdata/chromeria-v2.sqlite 'select 1'"]:
            self.assertTrue(db(command), command)
        # A quoted path without whitespace is a real file argument and matches the bare key.
        self.assertTrue(db('sqlite3 "/Users/u/.t3/userdata/chromeria-v2.sqlite" .tables'))
        # Neither URI hits the bare command key: the basename includes ?mode=ro.
        for command in [
            "sqlite3 'file:/Users/u/.t3/userdata/chromeria-v2.sqlite?mode=ro'",
            "sqlite3 file:/Users/u/.t3/userdata/chromeria-v2.sqlite?mode=ro",
        ]:
            self.assertFalse(db(command), command)
        # Live bake mentions: candidate 1672 edits a test; 26379 writes an Issue body.
        for command in [
            "python3 - <<'EOF'\nfrom pathlib import Path\np = Path('tests/test_serve.py')\np.write_text(p.read_text().replace('state.sqlite', 'chromeria-v2.sqlite'))\nEOF",
            "gh issue create --title \"Record the bake\" --body-file - <<'EOF'\nThe entry names chromeria-v2.sqlite in a test fixture.\nEOF",
            "gh issue create --title \"Record the bake\" --body \"The entry names chromeria-v2.sqlite in a test fixture.\"",
        ]:
            self.assertFalse(db(command), command)

    def test_command_after_an_empty_heredoc_still_matches(self):
        # Sol review of #52: the terminator right at the body's start must end the heredoc.
        def hit(command):
            e = store.Entry("e", "v", "t", commands=[["gh", "pr", "create"]])
            return e.matches(keys.extract("Bash", {"command": command}, None)) is not None
        for command in ["cat <<'EOF'\nEOF\ngh pr create", "cat <<EOF\nEOF\ngh pr create",
                        "cat <<A <<B\nA\nB\ngh pr create", "cat <<A <<'B'\nx\nA\nB\ngh pr create",
                        "cat <<'A'\nA\ncat <<'B'\nB\ngh pr create"]:
            self.assertTrue(hit(command), command)
        self.assertFalse(hit("cat <<'A'\nA\ncat <<'B'\ngh pr create\nB"))

    def test_substitution_boundaries_keep_inner_quotes_and_nested_bodies(self):
        runs = [
            "echo \"$(gh pr create --title \"hello\")\"",
            "echo \"$(gh pr create $(printf x))\"",
            "echo \"$(gh pr create \"$(printf x)\")\"",
            "echo $(gh pr create --title \"hello\")",
            "echo \"`gh pr create --title \"hello\"`\"",
            "echo \"`printf \"%s\" \\`gh pr create\\``\"",
            "cat <<EOF\n$(gh pr create --title \"hello\" $(printf x))\nEOF",
        ]
        mentions = [
            "echo '$(gh pr create)'",
            "echo \"$(printf \"%s\" \"gh pr create\")\"",
            "echo \"`printf \"%s\" \"gh pr create\"`\"",
            "echo \"$(printf \"%s\" \"$(printf \"%s\" \"gh pr create\")\")\"",
            "cat <<'EOF'\n$(gh pr create --title \"hello\" $(printf x))\nEOF",
        ]
        self.assert_command_cases(runs, mentions)

    def test_substitution_escapes_use_backslash_parity(self):
        # Two/four literal backslashes run; one/three leave the operator literal.
        runs = [
            "echo \"\\\\$(gh pr create)\"",
            "echo \"\\\\\\\\$(gh pr create)\"",
            "cat <<EOF\n\\\\$(gh pr create)\nEOF",
            "cat <<EOF\n\\\\\\\\$(gh pr create)\nEOF",
            "echo \"\\\\`gh pr create`\"",
            "cat <<EOF\n\\\\`gh pr create`\nEOF",
        ]
        mentions = [
            "echo \"\\$(gh pr create)\"",
            "echo \"\\\\\\$(gh pr create)\"",
            "cat <<EOF\n\\$(gh pr create)\nEOF",
            "cat <<EOF\n\\\\\\$(gh pr create)\nEOF",
            "echo \"\\`gh pr create\\`\"",
            "cat <<EOF\n\\`gh pr create\\`\nEOF",
        ]
        self.assert_command_cases(runs, mentions)

    def test_heredoc_terminators_are_exact_except_leading_tabs_for_strip(self):
        runs = [
            "cat <<'EOF'\nEOF\ngh pr create",
            "cat <<EOF\nEOF\ngh pr create",
            "cat <<-'EOF'\n\tEOF\ngh pr create",
            "cat <<-EOF\n\tEOF\ngh pr create",
            "cat <<A <<'B'\nA\nB\ngh pr create",
            "cat <<'A'\nA\ncat <<'B'\nB\ngh pr create",
        ]
        mentions = [
            "cat <<'EOF'\n EOF\ngh pr create\nEOF",
            "cat <<'EOF'\n\tEOF\ngh pr create\nEOF",
            "cat <<'EOF'\nEOF \ngh pr create\nEOF",
            "cat <<'EOF'\nEOF\t\ngh pr create\nEOF",
            "cat <<-'EOF'\n EOF\ngh pr create\nEOF",
            "cat <<-'EOF'\n\tEOF \ngh pr create\nEOF",
            "cat <<'EOF'\nEOFX\ngh pr create\nEOF",
        ]
        self.assert_command_cases(runs, mentions)

    def test_here_strings_and_arithmetic_or_parameter_boundaries(self):
        runs = [
            "cat <<<EOF\ngh pr create",
            "echo \"$(gh pr create $((1 + (2))))\"",
            "echo \"$(gh pr create ${v:-\")\"})\"",
            "echo \"$((1 + $(gh pr create)0))\"",
            "echo \"${v:-$(gh pr create --title \"hello\")}\"",
            "cat <<EOF\n$((1 + $(gh pr create)0))\nEOF",
        ]
        mentions = [
            "cat <<<\"gh pr create\"",
            "echo \"$((1 + (2))) gh pr create\"",
            "echo \"${v:-gh pr create}\"",
            "echo \"$(printf \"%s\" \"${v:-gh pr create}\")\"",
        ]
        self.assert_command_cases(runs, mentions)

    def test_quoted_commands_after_assignments_and_wrappers(self):
        runs = [
            "FLAG=1 'gh' pr create",
            "FLAG=1 g'h' pr create",
            "FLAG='one two' OTHER=2 \"gh\" pr create",
            "env 'gh' pr create",
            "env g'h' pr create",
            "env FLAG=1 'gh' pr create",
            "env -u UNUSED 'gh' pr create",
            "sudo -n 'gh' pr create",
            "sudo -u nobody g'h' pr create",
            "time 'gh' pr create",
            "nohup 'gh' pr create",
            "command -- 'gh' pr create",
            "exec g'h' pr create",
            "exec -a gh-alias 'gh' pr create",
        ]
        mentions = [
            "FLAG=1 printf '%s' 'gh pr create'",
            "env printf '%s' 'gh pr create'",
            "env -u UNUSED printf '%s' 'gh pr create'",
            "sudo -n printf '%s' 'gh pr create'",
            "command printf '%s' 'gh pr create'",
            "exec printf '%s' 'gh pr create'",
        ]
        self.assert_command_cases(runs, mentions)

    def test_shell_scripts_remove_one_outer_word_quote_layer(self):
        runs = [
            "bash -c \"\\\"g\\\"h pr create\"",
            "sh -c \"\\\"g\\\"h pr create\"",
            "zsh -lc \"\\\"g\\\"h pr create\"",
            "bash -c \"g\"'h pr create'",
            "bash -c \"\\\"g\\\"h\"' pr create'",
            "env FLAG=1 bash -c \"\\\"g\\\"h pr create\"",
            "bash -c 'gh pr create'",
        ]
        mentions = [
            "bash -c \"echo \\\"gh pr create\\\"\"",
            "sh -c \"printf '%s' \\\"gh pr create\\\"\"",
            "zsh -lc \"echo \\\"gh pr create\\\"\"",
            "bash -c 'echo '\"\\\"gh pr create\\\"\"",
            "bash -c \"echo \\\"gh pr create\\\"\"suffix",
            "bash -c 'echo \"gh pr create\"'",
        ]
        self.assert_command_cases(runs, mentions)

    def test_heredoc_ascii_boundaries_keep_unicode_and_punctuation_bodies_literal(self):
        runs = [
            "cat <<'ÉOF'\nÉOF\ngh pr create",
            "cat <<'EÖF'\nEÖF\ngh pr create",
            "cat <<'𝔈OF'\n𝔈OF\ngh pr create",
            "cat <<ÉOF\n$(gh pr create)\nÉOF",
            "cat <<EÖF\n$(gh pr create)\nEÖF",
            "cat <<𝔈OF\n$(gh pr create)\n𝔈OF",
            "cat <<'END-MARK'\nEND-MARK\ngh pr create",
            "cat <<'EOF'\nEOF\ngh pr create",
        ]
        mentions = [
            "cat <<'ÉOF'\ngh pr create\nÉOF",
            "cat <<'EÖF'\ngh pr create\nEÖF",
            "cat <<'𝔈OF'\ngh pr create\n𝔈OF",
            "cat <<ÉOF\ngh pr create\nÉOF",
            "cat <<EÖF\ngh pr create\nEÖF",
            "cat <<𝔈OF\ngh pr create\n𝔈OF",
            "cat <<\"EÖF\"\n$(gh pr create)\nEÖF",
            "cat <<'END-MARK'\ngh pr create\nEND-MARK",
            "cat <<'END.MARK'\nENDXMARK\ngh pr create\nEND.MARK",
        ]
        self.assert_command_cases(runs, mentions)

    def test_shell_script_options_before_or_clustered_with_c(self):
        runs = [
            "bash -ce 'gh pr create'",
            "bash -cE 'gh pr create'",
            "bash -ec 'gh pr create'",
            "bash -o pipefail -c 'gh pr create'",
            "bash --noprofile -o pipefail -ce '\"g\"h pr create'",
            "env FLAG=1 bash -o pipefail -ce 'gh pr create'",
        ]
        mentions = [
            "bash -ce 'echo \"gh pr create\"'",
            "bash -o pipefail -c 'echo \"gh pr create\"'",
            "bash -o pipefail -ce 'echo \"gh pr create\"'",
        ]
        self.assert_command_cases(runs, mentions)

    def test_ansi_c_quoted_shell_script_words(self):
        runs = [
            "bash -c $'gh pr create'",
            "bash -c $'gh pr create\\0; echo mention'",
            "bash -c $'\\x67h pr create'",
            "bash -c $'\\147h pr create'",
            "bash -c $'\\u0067h pr create'",
            "bash -c $'\\x27gh\\x27 pr create'",
            "bash -c $'gh\\x20pr\\tcreate'",
            "sh -c $'\\x67h pr create'",
            "bash -c $'g'\"h pr create\"",
        ]
        mentions = [
            "bash -c $'echo \\\"gh pr create\\\"'",
            "bash -c $'printf \\'%s\\' \\'gh pr create\\''",
            "bash -c $'echo \\x22gh pr create\\x22'",
            "bash -c $'echo \\047gh pr create\\047'",
            "echo $'gh pr create'",
            "bash -c $'echo \\0; gh pr create'",
            "bash -c $'echo \"$(printf \\'%s\\' \\'gh pr create\\')\"'",
        ]
        self.assert_command_cases(runs, mentions)

    def test_quoted_single_tokens_count_in_every_word_position(self):
        # Single quoted tokens count as arguments; this does not claim they execute gh.
        runs = [
            "printf '%s ' 'gh' 'pr' 'create'",
            "echo \"gh\" pr create",
            "echo g'h' pr create",
            "echo g\"h\" pr create",
            "echo $'\\x67h' pr create",
        ]
        mentions = [
            "printf '%s ' 'gh pr create'",
            "echo \"live check: gh pr create\"",
            "echo $'gh pr create'",
            "echo g'h pr create'",
        ]
        e = store.Entry("e", "v", "t", commands=["gh pr create".split()])
        for commands, expected in ((runs, True), (mentions, False)):
            for command in commands:
                with self.subTest(command=command):
                    hit = e.matches(keys.extract("Bash", {"command": command}, None)) is not None
                    self.assertEqual(hit, expected)

    def test_numbered_redirections_preserve_shell_commands(self):
        runs = [
            "2>/dev/null 'gh' pr create",
            "2>/dev/null bash -c 'gh pr create'",
            "2>&1 env 'gh' pr create",
            ">/dev/null 'gh' pr create",
            "2>>/dev/null g'h' pr create",
            "2>&1 bash -ce 'gh pr create'",
            "3<<'EOF' bash -c 'gh pr create'\nliteral gh pr create\nEOF",
        ]
        mentions = [
            "2>/dev/null printf '%s' 'gh pr create'",
            "2>/dev/null bash -c 'echo \"gh pr create\"'",
            "2>&1 env printf '%s' 'gh pr create'",
            "3<<'EOF' cat\nliteral gh pr create\nEOF",
        ]
        self.assert_command_cases(runs, mentions)

    def test_quoted_words_after_shell_control_syntax(self):
        runs = [
            "if true; then 'gh' pr create; fi",
            "! 'gh' pr create",
            "{ 'gh' pr create; }",
            "if false; then :; else g'h' pr create; fi",
            "for n in 1; do \"gh\" pr create; done",
            "time -p 'gh' pr create",
        ]
        mentions = [
            "if true; then printf '%s' 'gh pr create'; fi",
            "! printf '%s' 'gh pr create'",
            "{ printf '%s' 'gh pr create'; }",
            "for n in 1; do printf '%s' 'gh pr create'; done",
        ]
        self.assert_command_cases(runs, mentions)

    def test_shell_options_stop_after_one_script(self):
        runs = [
            "bash -c 'gh pr create' _ -c 'echo \"gh pr create\"'",
            "bash -c 'echo done' _ -c \"$(gh pr create)\"",
            "sh -c 'gh pr create' -c 'echo \"gh pr create\"'",
        ]
        mentions = [
            "bash -c 'echo \"$@\"' _ -c 'gh pr create'",
            "bash -c 'echo done' -- -c 'gh pr create'",
            "bash -c 'echo done' -c 'gh pr create'",
            "bash -ce 'echo done' _ -ce 'gh pr create'",
            "sh -c 'echo done' _ -c 'gh pr create'",
            "zsh -lc 'echo done' _ -c 'gh pr create'",
            "bash -c $'echo done' _ -c $'gh pr create'",
        ]
        self.assert_command_cases(runs, mentions)

    def test_quoted_single_database_tokens_match_but_prose_does_not(self):
        # Single quoted tokens count as arguments; this does not claim they execute gh.
        runs = [
            "sqlite3 'chromeria-v2.sqlite' .tables",
            "sqlite3 \"/Users/u/.t3/userdata/chromeria-v2.sqlite\" .tables",
            "sqlite3 $'chromeria-v2.sqlite' .tables",
            "sqlite3 chromeria-v2'.sqlite' .tables",
        ]
        mentions = [
            "echo 'the file chromeria-v2.sqlite'",
            "echo \"open /Users/u/.t3/userdata/chromeria-v2.sqlite later\"",
            "echo $'the file chromeria-v2.sqlite'",
            "echo prefix' the file chromeria-v2.sqlite'",
        ]
        e = store.Entry("e", "v", "t", commands=["chromeria-v2.sqlite".split()])
        for commands, expected in ((runs, True), (mentions, False)):
            for command in commands:
                with self.subTest(command=command):
                    hit = e.matches(keys.extract("Bash", {"command": command}, None)) is not None
                    self.assertEqual(hit, expected)

    def test_shell_scripts_after_controls_and_command_running_tools(self):
        runs = [
          "if true; then bash -c 'gh pr create'; fi",
          "! bash -c 'gh pr create'",
          "{ bash -c 'gh pr create'; }",
          "for i in 1; do bash -c 'gh pr create'; done",
          "xargs bash -c 'gh pr create'",
          "find . -exec bash -c 'gh pr create' \\;",
          "timeout 5 bash -c 'gh pr create'",
          "ssh host bash -c 'gh pr create'",
          "xargs bash -o pipefail -ce 'gh pr create'",
          "env ksh -c 'gh pr create'"
        ]
        mentions = [
          "if true; then bash -c 'echo \"gh pr create\"'; fi",
          "! bash -c 'echo \"gh pr create\"'",
          "{ bash -c 'echo \"gh pr create\"'; }",
          "for i in 1; do bash -c 'echo \"gh pr create\"'; done",
          "xargs bash -c 'echo \"gh pr create\"'",
          "find . -exec bash -c 'echo \"gh pr create\"' \\;",
          "timeout 5 bash -c 'echo \"gh pr create\"'",
          "ssh host bash -c 'echo \"gh pr create\"'",
          "env ksh -c 'echo \"gh pr create\"'"
        ]
        self.assert_command_cases(runs, mentions)

    def test_printed_shell_invocations_match_in_the_safe_direction(self):
        # Printed shell invocations match by policy; these cases do not execute gh.
        runs = [
          "echo bash -c 'gh pr create'",
          "echo sh -c 'gh pr create'",
          "echo \"bash\" -ce 'gh pr create'",
          "echo b'a'sh -o pipefail -c 'gh pr create'",
          "echo ksh -c 'gh pr create'"
        ]
        mentions = [
          "echo bash -c 'echo \"gh pr create\"'",
          "echo 'bash -c gh pr create'",
          "echo bash -c 'echo done' _ -c 'gh pr create'"
        ]
        e = store.Entry("e", "v", "t", commands=[["gh", "pr", "create"]])
        for commands, expected in ((runs, True), (mentions, False)):
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual(e.matches(keys.extract("Bash", {"command": command}, None)) is not None, expected)

    def test_ascii_whitespace_parity_for_all_six_divergent_code_points(self):
        # Same escaped fixtures as Chromeria; none of these characters is ASCII whitespace.
        for sep, encoded in [
            ("\u0085", "\\u0085"), ("\u001c", "\\u001c"), ("\u001d", "\\u001d"),
            ("\u001e", "\\u001e"), ("\u001f", "\\u001f"), ("\ufeff", "\\ufeff"),
        ]:
            with self.subTest(code_point=encoded):
                token = f"gh{sep}pr{sep}create"
                for command in [f"echo {token}", f"echo '{token}'"]:
                    result = keys.extract("Bash", {"command": command}, None)
                    self.assertEqual(result.words, ["echo", token])
                    self.assertFalse(result.has_command(["gh", "pr", "create"]))
                self.assertEqual(
                    keys.extract("Bash", {"command": f"printf '%s' '{token}'"}, None).words,
                    ["printf", "%s", token],
                )
                self.assertEqual(
                    keys.extract("Bash", {"command": f"bash -c $'echo gh{encoded}pr{encoded}create'"}, None).words,
                    ["bash", "-c", "echo", token],
                )
                path = f"{sep}/Users/u/.t3/userdata/chromeria-v2.sqlite"
                command = f"sqlite3 '{path}'"
                result = keys.extract("Bash", {"command": command}, None)
                self.assertEqual(result.words, ["sqlite3", path])
                self.assertEqual(keys.shell_paths(keys.words(command)), [path])
                self.assertTrue(result.has_command(["chromeria-v2.sqlite"]))
                self.assertEqual(keys.normalize_error(f"{sep}GH{sep}12{sep}"), f"{sep}gh{sep}#{sep}")
        self.assertEqual(keys.words("gh \t\n\r\v\fpr create"), ["gh", "pr", "create"])
        self.assertFalse(
            keys.extract("Bash", {"command": "echo 'gh \t\n\r\v\fpr create'"}, None).has_command(["gh", "pr", "create"])
        )
        self.assertEqual(keys.normalize_error(" \t\n\r\v\fGH 12 \t\n\r\v\f"), "gh #")

    def assert_command_cases(self, runs, mentions):
        # Same fixtures as Chromeria, with /bin/bash and a PATH-local gh stub as the oracle.
        e = store.Entry("e", "v", "t", commands=[["gh", "pr", "create"]])
        for commands, expected in ((runs, True), (mentions, False)):
            for command in commands:
                with self.subTest(command=command):
                    hit = e.matches(keys.extract("Bash", {"command": command}, None)) is not None
                    self.assertEqual(hit, expected)

    def test_path_key_matches_files_a_shell_command_names(self):
        # As Chromeria's Matcher.ts (toolboxmd/chromeria#234).
        e = store.Entry("e", "v", "t", paths=["**/chromeria-v2.sqlite"])
        hit = lambda command: e.matches(keys.extract("Bash", {"command": command}, "/tmp")) is not None
        for command in ["sqlite3 /x/userdata/chromeria-v2.sqlite .tables",
                        "sqlite3 'file:/x/userdata/chromeria-v2.sqlite?mode=ro' .tables",
                        'DB=~/.t3/userdata/chromeria-v2.sqlite; sqlite3 "$DB"', "sqlite3 chromeria-v2.sqlite"]:
            self.assertTrue(hit(command), command)
        for command in ["echo hello", "curl https://example.com/chromeria-v2.sqlite.gz"]:
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
            self.assertEqual([e.id for e in st.entries], ["big", "good"])
            self.assertTrue(st.entries[0].long)
            self.assertIn("no command, path or error key", st.errors[0])

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

    def test_context_entry_can_point_to_a_record(self):
        with tempfile.TemporaryDirectory() as d:
            entry(d, "vc", "Read it.", command="git push", target="x.md")
            st = store.load([d])
            self.assertEqual(st.errors, [])
            self.assertEqual(st.entries[0].target, str(Path(d) / "x.md"))
            self.assertFalse(st.entries[0].must_know_before)


def cue_entry(directory, file, body, **fields):
    """A Cue entry file; ``name`` is a front-matter field here, not the file name."""
    return entry(directory, file, body, **{"cue_name" if k == "name" else k: v for k, v in fields.items()})


class CueStoreTest(unittest.TestCase):
    def test_cue_entry_loads_with_its_scored_text_and_resolved_target(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "procedures").mkdir()
            p = cue_entry(d, "research", "AgentsMD: follow the research procedure.", id="agentsmd-research",
                      name="Research", applies="The request asks for facts: beyond what is at hand.",
                      target="procedures/research.md")
            st = store.load_cue([d])
            self.assertEqual(st.errors, [])
            [e] = st.entries
            self.assertEqual(e.id, "agentsmd-research")
            self.assertEqual(e.name, "Research")
            self.assertEqual(e.applies, "The request asks for facts: beyond what is at hand.")
            self.assertEqual(e.doc, "Research: The request asks for facts: beyond what is at hand.")
            self.assertEqual(e.text, "AgentsMD: follow the research procedure.")
            self.assertEqual(e.target, os.path.realpath(Path(d) / "procedures" / "research.md"))
            self.assertEqual(e.version, store.parse_cue(p).version)

    def test_bad_cue_entries_are_reported_not_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            cue_entry(d, "good", "Body.", name="Good", applies="When good.")
            cue_entry(d, "noname", "Body.", applies="When.")
            cue_entry(d, "noapplies", "Body.", name="X")
            cue_entry(d, "exactkey", "Body.", name="X", applies="When.", command="git push")
            cue_entry(d, "unknown", "Body.", name="X", applies="When.", invented="no")
            cue_entry(d, "empty", "", name="X", applies="When.")
            cue_entry(d, "big", "x" * (store.MAX_ENTRY_BYTES + 1), name="X", applies="When.")
            cue_entry(d, "zdup", "Body.", id="good", name="Dup", applies="When.")
            st = store.load_cue([d])
            self.assertEqual([e.id for e in st.entries], ["big", "exactkey", "good"])
            self.assertTrue(st.entries[0].long)
            reasons = " | ".join(st.errors)
            for want in ("no name", "no applies", "unknown fields: ['invented']", "empty body", "already used"):
                self.assertIn(want, reasons)

    def test_cue_only_entry_has_no_exact_keys_and_no_cross_loader_error(self):
        with tempfile.TemporaryDirectory() as d:
            cue_entry(d, "cue", "Body.", name="X", applies="When.")
            st = store.load([d])
            self.assertEqual(st.entries, [])
            self.assertEqual(st.errors, [])
            self.assertEqual([e.id for e in st.cue], ["cue"])

    def test_check_reports_both_lists(self):
        with tempfile.TemporaryDirectory() as d:
            keys_dir, cue_dir = Path(d) / "keys", Path(d) / "cue"
            keys_dir.mkdir()
            cue_dir.mkdir()
            entry(keys_dir, "push", "Pull first.", command="git push")
            cue_entry(cue_dir, "research", "Follow it.", name="Research", applies="When facts are asked.")
            cfg = Path(d) / "serving.json"
            cfg.write_text(json.dumps({"exact_keys": [str(keys_dir)], "cue": [str(cue_dir)],
                                       "cue_url": "http://127.0.0.1:9"}))
            out = subprocess.run([sys.executable, str(BIN), "check"], capture_output=True, text=True,
                                 env={**os.environ, "CONTEXTMD_SERVING_CONFIG": str(cfg)})
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            report = json.loads(out.stdout)
            self.assertEqual([e["id"] for e in report["exact_keys"]["entries"]], ["push"])
            self.assertEqual([e["id"] for e in report["cue"]["entries"]], ["research"])
            self.assertEqual(report["cue"]["url"], "http://127.0.0.1:9")


class ConfigTest(unittest.TestCase):
    def test_cue_keys_default_to_off(self):
        from contextmd.serve import config
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "serving.json"
            cfg.write_text(json.dumps({"exact_keys": ["/x"]}))
            c = config.load(cfg)
            self.assertEqual((c.cue, c.cue_url), ([], None))
            cfg.write_text(json.dumps({"cue": ["/c"], "cue_url": "http://h:1"}))
            c = config.load(cfg)
            self.assertEqual((c.cue, c.cue_url), (["/c"], "http://h:1"))


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
        self.assertEqual(json.loads(cred.read_text())["token"], "tok1")  # a valid credential is reused
        self.assertEqual(cred.stat().st_mode & 0o777, 0o600)
        calls = self.calls.read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("--subject contextmd-hook", calls[0])
        self.assertIn("--scope orchestration:read", calls[0])

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

    def test_codex_trust_touches_only_contextmd_tables(self):
        codex = self.home / ".codex"
        codex.mkdir(parents=True)
        self.run_cli("install", "codex", "--base", str(self.base))
        hooks = codex / "hooks.json"
        key0 = f"{hooks}:e:0"
        # Our first table loses its hash and gains an inline comment; another hook's table follows it.
        toml = (codex / "config.toml").read_text()
        toml = toml.replace(f'[hooks.state.{json.dumps(key0)}]', f'[hooks.state.{json.dumps(key0)}]  # contextmd')
        lines = [l for l in toml.splitlines() if not (l.startswith("trusted_hash") and '"sha256:0"' in l)]
        lines += ["", '[hooks.state."/other/hooks.json:pre_tool_use:0:0"]', 'trusted_hash = "sha256:other"']
        (codex / "config.toml").write_text("\n".join(lines) + "\n")
        self.run_cli("install", "codex", "--base", str(self.base))
        import tomllib
        state = tomllib.loads((codex / "config.toml").read_text())["hooks"]["state"]
        self.assertEqual(state["/other/hooks.json:pre_tool_use:0:0"]["trusted_hash"], "sha256:other")
        self.assertEqual(state[key0]["trusted_hash"], "sha256:0")
        self.assertEqual((codex / "config.toml").read_text().count(json.dumps(key0)), 1)

    def test_grok_and_opencode_files(self):
        self.run_cli("install", "grok", "--base", str(self.base))
        grok = json.loads((self.home / ".grok" / "hooks" / "contextmd.json").read_text())
        self.assertIn("PostCompact", grok["hooks"])
        self.assertIn("PreToolUse", grok["hooks"])
        self.run_cli("install", "opencode", "--base", str(self.base))
        plugin = (self.home / ".config" / "opencode" / "plugins" / "contextmd.js").read_text()
        self.assertIn(json.dumps(str(self.base)), plugin)
        self.assertNotIn("const BASE = \"__BASE__\"", plugin)

    def test_reinstall_keeps_other_hooks_in_a_shared_group(self):
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        self.run_cli("install", "claude", "--base", str(self.base))
        data = json.loads(settings.read_text())
        group = data["hooks"]["PreToolUse"][0]
        group["matcher"] = "Bash"
        group["hooks"].append({"type": "command", "command": "/usr/local/bin/other-hook"})
        settings.write_text(json.dumps(data))
        self.run_cli("install", "claude", "--base", str(self.base))
        pre = json.loads(settings.read_text())["hooks"]["PreToolUse"]
        commands = [h["command"] for g in pre for h in g["hooks"]]
        self.assertIn("/usr/local/bin/other-hook", commands)
        self.assertEqual(sum("contextmd-hook" in c for c in commands), 1)
        self.assertEqual(pre[0]["matcher"], "Bash")

    def test_updates_keep_permissions_and_follow_symlinks(self):
        real = self.root / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text(json.dumps({"model": "x"}))
        real.chmod(0o600)
        link = self.home / ".claude" / "settings.json"
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        self.run_cli("install", "claude", "--base", str(self.base))
        self.assertTrue(link.is_symlink())
        self.assertIn("contextmd-hook", real.read_text())
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(real.with_name("settings.json.contextmd-backup").read_text()), {"model": "x"})

    def test_reuses_a_valid_credential_and_renews_on_request(self):
        self.run_cli("install", "grok", "--base", str(self.base))
        self.run_cli("install", "opencode", "--base", str(self.base))
        cred = self.base / "userdata" / "contextmd" / "hook-credential"
        self.assertEqual(json.loads(cred.read_text())["token"], "tok1")
        self.run_cli("install", "grok", "--base", str(self.base), "--renew")
        self.assertEqual(json.loads(cred.read_text())["token"], "tok2")
        self.assertIn(f"auth session revoke --base-dir {self.base} s1", self.calls.read_text())

    def test_a_failed_revocation_fails_the_install_and_is_retried(self):
        self.run_cli("install", "grok", "--base", str(self.base))
        real = (self.bin / "t3").read_text()
        (self.bin / "t3").write_text(real.replace("#!/bin/sh\n", '#!/bin/sh\n[ "$3" = revoke ] && exit 1\n', 1))
        done = self.run_cli("install", "grok", "--base", str(self.base), "--renew", check=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("could not revoke", done.stdout)
        self.assertNotIn("tok", done.stdout)
        cred = json.loads((self.base / "userdata" / "contextmd" / "hook-credential").read_text())
        self.assertEqual(cred["revokePending"], ["s1"])
        (self.bin / "t3").write_text(real)
        self.run_cli("install", "grok", "--base", str(self.base))
        cred = json.loads((self.base / "userdata" / "contextmd" / "hook-credential").read_text())
        self.assertNotIn("revokePending", cred)

    def test_keeps_a_foreign_hook_that_merely_mentions_contextmd_hook(self):
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        foreign = {"type": "command", "command": "/usr/local/bin/audit-hook --label contextmd-hook"}
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [foreign]}]}}))
        self.run_cli("install", "claude", "--base", str(self.base))
        self.run_cli("install", "claude", "--base", str(self.base))
        commands = [h["command"] for g in json.loads(settings.read_text())["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertIn(foreign["command"], commands)
        self.assertEqual(sum(c.startswith("/bin/sh ") for c in commands), 1)

    def test_a_failed_credential_write_keeps_the_working_session(self):
        self.run_cli("install", "grok", "--base", str(self.base))
        cred = self.base / "userdata" / "contextmd" / "hook-credential"
        (cred.parent / "hook-credential.contextmd-tmp").mkdir()  # the atomic write cannot create its temp file
        done = self.run_cli("install", "grok", "--base", str(self.base), "--renew", check=False)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(cred.read_text())["token"], "tok1")
        calls = self.calls.read_text().splitlines()
        self.assertEqual(calls[-1], f"auth session revoke --base-dir {self.base} s2")  # the new one, not s1
        self.assertNotIn(f"auth session revoke --base-dir {self.base} s1", calls)

    def test_codex_homes_follow_chromeria_resolution(self):
        custom = self.root.resolve() / "codex-main"
        custom.mkdir()
        shadow = self.base / "userdata" / "providers" / "codex" / "codexWork" / "shadow"
        shadow.mkdir(parents=True)
        (shadow / "hooks.json").symlink_to(custom / "hooks.json")
        (custom / "config.toml").write_text("")
        (shadow / "config.toml").symlink_to(custom / "config.toml")
        (self.base / "userdata" / "settings.json").write_text(json.dumps({
            "providers": {"codex": {"homePath": str(custom)}},
            "providerInstances": {"codexWork": {"driver": "codex", "config": {"homePath": str(custom)}}},
        }))
        self.run_cli("install", "codex", "--base", str(self.base))
        self.assertTrue(any("contextmd-hook" in h["command"]
                            for gs in json.loads((custom / "hooks.json").read_text())["hooks"].values()
                            for g in gs for h in g["hooks"]))
        self.assertFalse((self.home / ".codex" / "hooks.json").exists())
        import tomllib
        state = tomllib.loads((custom / "config.toml").read_text())["hooks"]["state"]
        self.assertTrue(any(k.startswith(str(custom / "hooks.json")) for k in state))
        self.assertTrue(any(k.startswith(str(shadow / "hooks.json")) for k in state))

    def test_reinstall_keeps_every_other_hook_at_its_position(self):
        # Codex keys trust by position, so foreign hooks must not move when ours is replaced.
        codex = self.home / ".codex"
        codex.mkdir(parents=True)
        old = f"/bin/sh /old/checkout/bin/contextmd-hook codex {self.base}"
        foreign = lambda n: {"hooks": [{"type": "command", "command": f"/usr/local/bin/foreign-{n}"}]}
        (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            foreign(1), {"hooks": [{"type": "command", "command": old}]}, foreign(2)]}}))
        self.run_cli("install", "codex", "--base", str(self.base))
        pre = json.loads((codex / "hooks.json").read_text())["hooks"]["PreToolUse"]
        self.assertEqual([g["hooks"][0]["command"].split()[-1] for g in pre],
                         ["/usr/local/bin/foreign-1", str(self.base), "/usr/local/bin/foreign-2"])
        self.assertNotIn("/old/checkout", json.dumps(pre))

    def test_duplicates_are_replaced_in_place_so_no_foreign_hook_moves(self):
        codex = self.home / ".codex"
        codex.mkdir(parents=True)
        old = {"type": "command", "command": f"/bin/sh /old/bin/contextmd-hook codex {self.base}"}
        foreign = {"type": "command", "command": "/usr/local/bin/foreign"}
        (codex / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"hooks": [old]}, {"hooks": [old, foreign]}, {"hooks": [foreign]}]}}))
        self.run_cli("install", "codex", "--base", str(self.base))
        pre = json.loads((codex / "hooks.json").read_text())["hooks"]["PreToolUse"]
        self.assertEqual(pre[1]["hooks"][1], foreign)  # same group, same index
        self.assertEqual(pre[2]["hooks"][0], foreign)
        self.assertEqual(len(pre), 3)
        self.assertNotIn("/old/bin", json.dumps(pre))

    def test_a_failed_rollback_names_the_session_to_revoke(self):
        self.run_cli("install", "grok", "--base", str(self.base))
        cred = self.base / "userdata" / "contextmd" / "hook-credential"
        (cred.parent / "hook-credential.contextmd-tmp").mkdir()
        real = (self.bin / "t3").read_text()
        (self.bin / "t3").write_text(real.replace("#!/bin/sh\n", '#!/bin/sh\n[ "$3" = revoke ] && exit 1\n', 1))
        done = self.run_cli("install", "grok", "--base", str(self.base), "--renew", check=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("s2", done.stdout)
        self.assertIn("revoke it by hand", done.stdout)
        self.assertIn(f"auth session revoke --base-dir {self.base} s2", done.stdout)
        self.assertNotIn("tok", done.stdout)
        self.assertEqual(json.loads(cred.read_text())["token"], "tok1")

    def test_skips_disabled_codex_instances(self):
        main = self.root.resolve() / "codex-main"
        work = self.root.resolve() / "codex-work"
        (self.base / "userdata" / "settings.json").write_text(json.dumps({
            "providers": {"codex": {"enabled": False, "homePath": str(main)}},
            "providerInstances": {"codexWork": {"driver": "codex", "config": {"enabled": False, "homePath": str(work)}}},
        }))
        from contextmd.serve import install
        self.assertEqual(install.codex_homes(self.base), [])

    def test_refuses_a_directory_that_is_not_a_chromeria_base(self):
        done = self.run_cli("install", "claude", "--base", str(self.root / "nothing"), check=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("not a Chromeria base directory", done.stdout)


PLUGIN_SCRIPT = r"""
const [pluginPath] = process.argv.slice(2);
const replies = [];      // pending moment replies, resolved by the script
const settles = [];
const releases = [];
const track = (url) => {
  if (url.includes("/release/")) {
    releases.push(url);
    return Promise.resolve({ status: 204 });
  }
  settles.push(url);
  return Promise.resolve({ status: url.includes("expired") ? 404 : 204 });
};
globalThis.fetch = (url) => {
  if (url.endsWith("/moment")) {
    return new Promise((resolve) => replies.push((body) =>
      resolve({ status: 200, json: async () => body })));
  }
  return track(url);
};
const { default: plugin } = await import(pluginPath);
const hooks = await plugin({ directory: "/tmp" });
const msgs = (text) => ({ messages: [{ info: { role: "user", id: "m1", sessionID: "s" }, parts: [] }] });
const out = {};

// A tool reply that arrives after the next user message belongs to the old turn.
const late = hooks["tool.execute.after"]({ tool: "bash", sessionID: "s", callID: "c1", args: {} }, { output: "", metadata: { exit: 0 } });
const turn = hooks["chat.message"]({ sessionID: "s" }, {});
// chat.message drops the previous turn before it asks, so its fetch is one tick later.
while (replies.length < 2) await new Promise((r) => setImmediate(r));
replies[1]({ context: "NEW TURN" });
await turn;
replies[0]({ context: "OLD TURN" });
await late;
let m = msgs();
await hooks["experimental.chat.messages.transform"]({}, m);
out.parts = m.messages[0].parts.map((p) => p.text);

// A deny is shown through the user message; the call is stopped with a short reason.
const denied = hooks["tool.execute.before"]({ tool: "bash", sessionID: "s", callID: "c2" }, { args: { command: "git commit" } });
replies[2]({ deny: "RULE" });
out.thrown = await denied.then(() => null, (e) => e.message);
m = msgs();
await hooks["experimental.chat.messages.transform"]({}, m);
out.afterDeny = m.messages[0].parts.map((p) => p.text);
out.settles = settles.length;
out.released = releases.length;

// A settle that failed in transit is retried on the next model call; a refused one is shown no more.
let failSettle = 1;
globalThis.fetch = (url) => {
  if (url.endsWith("/moment")) {
    return new Promise((resolve) => replies.push((body) => resolve({ status: 200, json: async () => body })));
  }
  if (url.includes("/release/")) {
    releases.push(url);
    return Promise.resolve({ status: 204 });
  }
  settles.push(url);
  if (failSettle-- > 0) return Promise.reject(new Error("timeout"));
  return Promise.resolve({ status: 404 });
};
const t2 = hooks["chat.message"]({ sessionID: "s" }, {});
while (replies.length < 4) await new Promise((r) => setImmediate(r));
replies[3]({ context: "SECOND" });
await t2;
const before = settles.length;
m = msgs();
await hooks["experimental.chat.messages.transform"]({}, m);   // settle fails: retried next time
m = msgs();
await hooks["experimental.chat.messages.transform"]({}, m);   // settle refused: shown this last time
out.retried = settles.length - before;
m = msgs();
await hooks["experimental.chat.messages.transform"]({}, m);
out.afterRefusal = m.messages[0].parts.map((p) => p.text);

// With the off switch, queued text is neither shown nor settled, and is forgotten.
const t3 = hooks["chat.message"]({ sessionID: "s" }, {});
while (replies.length < 5) await new Promise((r) => setImmediate(r));
replies[4]({ context: "THIRD" });
await t3;
const { mkdirSync, writeFileSync, rmSync } = await import("node:fs");
const offDir = process.argv[3];
writeFileSync(offDir + "/off", "");
const settlesBeforeOff = settles.length;
const releasesBeforeOff = releases.length;
m = msgs();
await hooks["experimental.chat.messages.transform"]({}, m);
out.whileOff = m.messages[0].parts.map((p) => p.text);
rmSync(offDir + "/off");
m = msgs();
await hooks["experimental.chat.messages.transform"]({}, m);
out.afterOff = m.messages[0].parts.map((p) => p.text);
out.settlesWhileOff = settles.length - settlesBeforeOff;
out.releasesWhileOff = releases.length - releasesBeforeOff;

// Shown, then the settle failed: the next turn must not release that reply.
let failShown = 1;
globalThis.fetch = (url) => {
  if (url.endsWith("/moment")) {
    return new Promise((resolve) => replies.push((body) => resolve({ status: 200, json: async () => body })));
  }
  if (url.includes("/release/")) {
    releases.push(url);
    return Promise.resolve({ status: 204 });
  }
  settles.push(url);
  if (failShown-- > 0) return Promise.reject(new Error("timeout"));
  return Promise.resolve({ status: 204 });
};
const seen = hooks["chat.message"]({ sessionID: "shown" }, {});
while (replies.length < 6) await new Promise((r) => setImmediate(r));
replies[5]({ context: "SEEN" });
await seen;
m = { messages: [{ info: { role: "user", id: "m2", sessionID: "shown" }, parts: [] }] };
await hooks["experimental.chat.messages.transform"]({}, m);
const releasesBeforeDrop = releases.length;
const next = hooks["chat.message"]({ sessionID: "shown" }, {});
while (replies.length < 7) await new Promise((r) => setImmediate(r));
replies[6]({ context: "NEXT" });
await next;
out.releasedAfterShown = releases.length - releasesBeforeDrop;
console.log(JSON.stringify(out));
"""


class OpenCodePluginTest(unittest.TestCase):
    def test_drops_replies_from_an_earlier_turn_and_shows_denies_in_the_user_message(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "userdata" / "contextmd").mkdir(parents=True)
            (base / "userdata" / "server-runtime.json").write_text(json.dumps({"origin": "http://127.0.0.1:1"}))
            (base / "userdata" / "contextmd" / "hook-credential").write_text(json.dumps({"token": "t"}))
            from contextmd.serve import install
            plugin = base / "contextmd.mjs"
            plugin.write_text(install.plugin_source(base))
            script = base / "run.mjs"
            script.write_text(PLUGIN_SCRIPT)
            done = subprocess.run(["node", str(script), str(plugin), str(base / "userdata" / "contextmd")],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            out = json.loads(done.stdout)
            self.assertEqual(out["parts"], ["NEW TURN"])
            self.assertIn("stopped this call", out["thrown"])
            self.assertNotIn("RULE", out["thrown"])
            self.assertEqual(out["afterDeny"], ["NEW TURN", "RULE"])
            self.assertEqual(out["settles"], 2)
            self.assertEqual(out["released"], 1)
            self.assertEqual(out["retried"], 2)
            self.assertEqual(out["afterRefusal"], [])
            self.assertEqual(
                (out["whileOff"], out["afterOff"], out["settlesWhileOff"], out["releasesWhileOff"]),
                ([], [], 0, 1),
            )
            self.assertEqual(out["releasedAfterShown"], 0)


TRUNCATION_SCRIPT = r"""
const [pluginPath] = process.argv.slice(2);
const posted = [];
globalThis.fetch = (url, init) => {
  if (url.endsWith("/moment")) posted.push(JSON.parse(init.body));
  return Promise.resolve({ status: 204, json: async () => ({}) });
};
const { default: plugin } = await import(pluginPath);
const hooks = await plugin({ directory: "/tmp" });
const after = (output, metadata) =>
  hooks["tool.execute.after"]({ tool: "read", sessionID: "s", callID: "c", args: { filePath: "/r.md" } },
                              { output, metadata });
// A line clipped early in a long read: OpenCode reports truncated: false, and the 4 KB
// slice the plugin forwards no longer contains the marker.
await after("x".repeat(100) + "... (line truncated to 2000 chars)\n" + "y".repeat(8000), { truncated: false });
await after("whole file", { truncated: true });
await after("whole file", { truncated: false });
console.log(JSON.stringify(posted.map((p) => p.output.metadata.truncated ?? null)));
"""


class OpenCodeTruncationTest(unittest.TestCase):
    def test_forwards_a_cut_read_even_when_the_marker_falls_outside_the_slice(self):
        # Pointer rules lift only on a full read (toolboxmd/chromeria#225).
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "userdata" / "contextmd").mkdir(parents=True)
            (base / "userdata" / "server-runtime.json").write_text(json.dumps({"origin": "http://127.0.0.1:1"}))
            (base / "userdata" / "contextmd" / "hook-credential").write_text(json.dumps({"token": "t"}))
            from contextmd.serve import install
            plugin = base / "contextmd.mjs"
            plugin.write_text(install.plugin_source(base))
            script = base / "run.mjs"
            script.write_text(TRUNCATION_SCRIPT)
            done = subprocess.run(["node", str(script), str(plugin)], capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout), [True, True, False])


SLOW_REPLY_SCRIPT = r"""
const [pluginPath] = process.argv.slice(2);
globalThis.fetch = (url, init) => new Promise((resolve, reject) => {
  if (!url.endsWith("/moment")) return resolve({ status: 204, json: async () => ({}) });
  const timer = setTimeout(() => resolve({ status: 200, json: async () => ({ context: "SLOW RULE" }) }), 1500);
  init.signal?.addEventListener("abort", () => { clearTimeout(timer); reject(new Error("aborted")); });
});
const { default: plugin } = await import(pluginPath);
const hooks = await plugin({ directory: "/tmp" });
await hooks["tool.execute.after"]({ tool: "bash", sessionID: "s", callID: "c", args: {} }, { output: "", metadata: { exit: 0 } });
const m = { messages: [{ info: { role: "user", id: "m1", sessionID: "s" }, parts: [] }] };
await hooks["experimental.chat.messages.transform"]({}, m);
console.log(JSON.stringify(m.messages[0].parts.map((p) => p.text)));
"""


class OpenCodeCutoffTest(unittest.TestCase):
    def test_a_reply_within_two_seconds_reaches_opencode(self):
        # Serving cut-off 2 s while Cue is new (chromeria#248); the plugin aborted at 1 s.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "userdata" / "contextmd").mkdir(parents=True)
            (base / "userdata" / "server-runtime.json").write_text(json.dumps({"origin": "http://127.0.0.1:1"}))
            (base / "userdata" / "contextmd" / "hook-credential").write_text(json.dumps({"token": "t"}))
            from contextmd.serve import install
            plugin = base / "contextmd.mjs"
            plugin.write_text(install.plugin_source(base))
            script = base / "run.mjs"
            script.write_text(SLOW_REPLY_SCRIPT)
            done = subprocess.run(["node", str(script), str(plugin)], capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout), ["SLOW RULE"])


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

    def test_a_socket_stdout_gets_the_reply_and_the_settle(self):
        # Node spawns hooks with socket stdio; Linux cannot reopen a socket through /dev/fd (#30).
        import http.server
        import socket
        import threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                seen.append(self.path.rsplit("/", 1)[0])
                out = b'{"hookSpecificOutput":{"additionalContext":"rule"}}' if self.path.endswith("/moment") else b""
                self.send_response(200 if out else 204)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        hook = BIN.parent / "contextmd-hook"
        ours, theirs = socket.socketpair()
        try:
            with tempfile.TemporaryDirectory() as d:
                base = Path(d)
                (base / "userdata" / "contextmd").mkdir(parents=True)
                (base / "userdata" / "server-runtime.json").write_text(
                    json.dumps({"origin": f"http://127.0.0.1:{server.server_port}"}))
                (base / "userdata" / "contextmd" / "hook-credential").write_text(json.dumps({"token": "tok"}))
                done = subprocess.run(["/bin/sh", str(hook), "claude", str(base)], input=b"{}",
                                      stdout=theirs, stderr=subprocess.PIPE, timeout=10)
                theirs.close()
                got = b""
                while chunk := ours.recv(4096):
                    got += chunk
                self.assertEqual((done.returncode, got), (0, b'{"hookSpecificOutput":{"additionalContext":"rule"}}'))
                self.assertEqual(seen, ["/contextmd/v1", "/contextmd/v1/settle"])
        finally:
            ours.close()
            server.shutdown()
            server.server_close()

    def test_the_claude_hook_is_silent_inside_grok(self):
        # Grok also runs Claude Code's settings.json hooks; its own hook file serves it (#32).
        import http.server
        import threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                seen.append(self.headers.get("x-contextmd-harness"))
                out = b'{"rule":1}'
                self.send_response(200)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        hook = BIN.parent / "contextmd-hook"
        try:
            with tempfile.TemporaryDirectory() as d:
                base = Path(d)
                (base / "userdata" / "contextmd").mkdir(parents=True)
                (base / "userdata" / "server-runtime.json").write_text(
                    json.dumps({"origin": f"http://127.0.0.1:{server.server_port}"}))
                (base / "userdata" / "contextmd" / "hook-credential").write_text(json.dumps({"token": "tok"}))
                env = dict(os.environ, GROK_HOOK_NAME="contextmd")
                run = lambda harness: subprocess.run(["/bin/sh", str(hook), harness, str(base)], input="{}",
                                                     capture_output=True, text=True, timeout=10, env=env)
                inside = run("claude")
                self.assertEqual((inside.returncode, inside.stdout, inside.stderr), (0, "", ""))
                self.assertEqual(seen, [])
                self.assertEqual(run("grok").stdout, '{"rule":1}')
                self.assertEqual(seen[0], "grok")
        finally:
            server.shutdown()
            server.server_close()

    def test_hooks_wait_two_seconds_and_grok_four(self):
        # The serving cut-off is 2 s while Cue is new (user decision 2026-10-13, chromeria#248):
        # a moment answered in 1.5 s now reaches every harness. Grok, which takes served text only
        # at PreToolUse and allows hooks 5 s, waits 4 s (chromeria#230).
        import http.server
        import threading
        delay = {"s": 1.5}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                if self.path.endswith("/moment"):
                    time.sleep(delay["s"])
                    out = b'{"rule":1}'
                    self.send_response(200)
                    self.send_header("content-length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                else:
                    self.send_response(204)
                    self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        hook = BIN.parent / "contextmd-hook"
        try:
            with tempfile.TemporaryDirectory() as d:
                base = Path(d)
                (base / "userdata" / "contextmd").mkdir(parents=True)
                (base / "userdata" / "server-runtime.json").write_text(
                    json.dumps({"origin": f"http://127.0.0.1:{server.server_port}"}))
                (base / "userdata" / "contextmd" / "hook-credential").write_text(json.dumps({"token": "tok"}))
                env = {k: v for k, v in os.environ.items() if k != "GROK_HOOK_NAME"}
                run = lambda harness: subprocess.run(["/bin/sh", str(hook), harness, str(base)], input="{}",
                                                     capture_output=True, text=True, timeout=10, env=env)
                self.assertEqual(run("claude").stdout, '{"rule":1}')
                delay["s"] = 2.5
                self.assertEqual(run("grok").stdout, '{"rule":1}')
                started = time.time()
                self.assertEqual(run("claude").stdout, "")
                self.assertLess(time.time() - started, 2.4)
        finally:
            server.shutdown()
            server.server_close()

    def test_an_error_status_prints_nothing(self):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                status = 404 if self.path.endswith("/moment") and self.server.flip else 500
                body = b"Not Found"
                self.send_response(status)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), H)
        server.flip = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        hook = BIN.parent / "contextmd-hook"
        try:
            with tempfile.TemporaryDirectory() as d:
                base = Path(d)
                (base / "userdata" / "contextmd").mkdir(parents=True)
                (base / "userdata" / "server-runtime.json").write_text(
                    json.dumps({"origin": f"http://127.0.0.1:{server.server_port}"}))
                (base / "userdata" / "contextmd" / "hook-credential").write_text(json.dumps({"token": "tok"}))
                for flip in (True, False):  # an older server without the route (404), then a 5xx
                    server.flip = flip
                    done = subprocess.run(["/bin/sh", str(hook), "claude", str(base)], input="{}",
                                          capture_output=True, text=True, timeout=10)
                    self.assertEqual((done.returncode, done.stdout), (0, ""))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
