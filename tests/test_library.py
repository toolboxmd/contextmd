"""The Library contract, independent of a running Chromeria server."""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from contextmd.serve import store

CORPUS = Path(__file__).with_name('library-corpus.json')
BIN = Path(__file__).resolve().parents[1] / 'bin/contextmd-serve'


def fixture(root, case):
    for directory in case['dirs']:
        (root / directory).mkdir(parents=True, exist_ok=True)
    for file, text in case['files'].items():
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        (root / file).write_text(text)
    for file, target in case['links'].items():
        (root / file).parent.mkdir(parents=True, exist_ok=True)
        (root / file).symlink_to(root / target, target_is_directory=True)


class LibraryCorpusTest(unittest.TestCase):
    def test_independent_entry_contract(self):
        for case in json.loads(CORPUS.read_text())['entries']:
            with self.subTest(case=case['name']):
                raw = base64.b64decode(case['raw_b64']) if 'raw_b64' in case else case['text'].encode()
                p = Path('/tmp/library-contract') / case['file']
                try:
                    parsed = store.parse_file(p, raw, tolerant=case['mode'] == 'legacy')
                except store.EntryError as error:
                    self.assertEqual({'code': error.code, 'line': error.line, 'field': error.field}, case.get('error'))
                    continue
                self.assertNotIn('error', case)
                exact, cue = parsed.views()
                self.assertEqual(parsed.fields, case['fields'])
                self.assertEqual({'id': parsed.id, 'body': parsed.body, 'long': parsed.long,
                                  'exact': exact is not None, 'cue': cue is not None,
                                  'legacy': parsed.legacy is not None}, case['expect'])
                self.assertEqual(parsed.version, hashlib.sha256(raw).hexdigest()[:12])

    def test_independent_store_contract(self):
        for case in json.loads(CORPUS.read_text())['stores']:
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as d:
                root = Path(d).resolve()
                fixture(root, case)
                library = store.load([store.Source(str(root / s['directory']), s['registered'])
                                      for s in case['sources']])
                observed = {'exact': [e.id for e in library.entries],
                            'cue': [e.id for e in library.cue],
                            'bodies': [e.text for e in [*library.entries, *library.cue]],
                            'errors': [str(Path(e.split(': ', 1)[0]).relative_to(root)) for e in library.errors],
                            'legacy': len(library.legacy),
                            'owners': [str(Path(p).relative_to(root)) for p in library.owners]}
                self.assertEqual(observed, case['expect'])

    def test_independent_discovery_contract(self):
        for case in json.loads(CORPUS.read_text())['discovery']:
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as d:
                root = Path(d).resolve()
                fixture(root, case)
                found = store.library_dir(str(root / case['thread']))
                self.assertEqual(str(Path(found).relative_to(root)) if found else None, case['expect'])

    @unittest.skipIf(os.geteuid() == 0, 'root bypasses directory permissions')
    def test_unreadable_directory_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'library'
            root.mkdir()
            root.chmod(0)
            try:
                library = store.load([str(root)])
                self.assertEqual(library.entries, [])
                self.assertEqual(library.errors, [f'{root}: unreadable directory'])
            finally:
                root.chmod(0o700)

    def test_corpus_copy_is_identical_without_starting_a_server(self):
        if not os.environ.get('CONTEXTMD_CHROMERIA'):
            self.skipTest('set CONTEXTMD_CHROMERIA for the file-only twin check')
        twin = Path(os.environ['CONTEXTMD_CHROMERIA']) / 'apps/server/src/contextmd/library-corpus.json'
        self.assertEqual(CORPUS.read_bytes(), twin.read_bytes())

    def test_known_normalizer_difference_is_reported_at_its_owner(self):
        # #69 owns this inherited matcher difference; the raw parsed field is shared.
        raw = '---\nerror: Erreur ١٢, id 34\n---\nBody.'.encode()
        parsed = store.parse_file(Path('/tmp/unicode.md'), raw)
        self.assertEqual(parsed.fields['error'], 'Erreur ١٢, id 34')
        self.assertEqual(parsed.views()[0].errors, ['erreur #, id #'])

    def test_target_expands_either_home_separator_but_not_other_users(self):
        for target in ('~/record.md', '~\\record.md'):
            raw = f"---\ncommand: x\ntarget: '{target}'\n---\nBody.".encode()
            self.assertEqual(store.parse_file(Path('/tmp/library/e.md'), raw).views()[0].target,
                             str(Path.home() / 'record.md'))
        raw = b"---\ncommand: x\ntarget: '~user/record.md'\n---\nBody."
        self.assertEqual(store.parse_file(Path('/tmp/library/e.md'), raw).views()[0].target,
                         '/tmp/library/~user/record.md')


class LibraryAdmissionTest(unittest.TestCase):
    def test_strict_admission_cannot_inherit_registered_tolerance(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'legacy.md'
            p.write_text('---\ncommand: git push\ncommand: git commit\n---\nBody.')
            self.assertEqual(store.load([d]).errors, [])
            result = subprocess.run([sys.executable, '-B', str(BIN), 'check', '--entry', str(p)],
                                    env={**os.environ, 'CONTEXTMD_SERVING_CONFIG': str(Path(d) / 'bad.json')},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn(str(p), json.loads(result.stdout)['errors'][0])
            self.assertIn('repeated field', json.loads(result.stdout)['errors'][0])

    def test_project_discovery_uses_checkout_boundaries_and_worktree_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'repo'
            (root / '.git').mkdir(parents=True)
            (root / 'library').mkdir()
            sub = root / 'sub'
            sub.mkdir()
            self.assertEqual(store.library_dir(str(sub)), str(root / 'library'))
            nested = sub / 'nested'
            nested.mkdir()
            (nested / '.git').write_text('gitdir: /unused')
            (nested / 'library').mkdir()
            child = nested / 'child'
            child.mkdir()
            self.assertEqual(store.library_dir(str(child)), str(nested / 'library'))
            plain = Path(d) / 'plain'
            (plain / 'library').mkdir(parents=True)
            self.assertEqual(store.library_dir(str(plain)), str(plain / 'library'))
            self.assertIsNone(store.library_dir(str(Path(d) / 'missing')))

    def test_flat_sources_deduplicate_aliases_and_reject_both_entry_wholly(self):
        with tempfile.TemporaryDirectory() as d:
            project, shared = Path(d) / 'project', Path(d) / 'shared'
            project.mkdir()
            shared.mkdir()
            (project / 'a.md').write_text('---\nid: same\ncommand: x\n---\nProject.')
            (shared / 'both.md').write_text('---\nid: same\ncommand: y\nname: Cue\napplies: When.\n---\nShared.')
            (project / 'sub').mkdir()
            (project / 'sub' / 'ignored.md').write_text('not an entry')
            alias = Path(d) / 'alias'
            alias.symlink_to(project, target_is_directory=True)
            library = store.load([store.Source(str(project), False), str(alias), str(shared)])
            self.assertEqual([e.text for e in library.entries], ['Project.'])
            self.assertEqual(library.cue, [])
            self.assertEqual(len(library.owners), 2)
            self.assertEqual(len(library.errors), 1)
            self.assertIn('already used', library.errors[0])


class LibraryRegressionTest(unittest.TestCase):
    def test_cue_history_keeps_its_legacy_text_and_record_target(self):
        from contextmd.serve.card import rule_text
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            library = root / 'library'
            library.mkdir()
            p = library / 'cue.md'
            old = b'---\nid: cue\nname: Cue\napplies: When: this holds.\ntarget: ../record.md\n---\nOld knowledge.'
            p.write_bytes(old)
            for args in (['init', '-q'], ['add', '.'],
                         ['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'cue']):
                subprocess.run(['git', '-C', d, *args], check=True, capture_output=True)
            p.write_text('---\nid: cue\nname: Cue\napplies: "When this holds."\n---\nNew knowledge.')
            row = {'entry': 'cue', 'version': hashlib.sha256(old).hexdigest()[:12], 'owners': {str(library): None}}
            recovered = rule_text(row, root / 'unused.sqlite')
            self.assertEqual(recovered['source'], 'store_history')
            self.assertEqual(recovered['text'], 'Old knowledge.')
            self.assertEqual(recovered['target'], str(root / 'record.md'))

    def test_flow_list_keeps_separate_command_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'push.md'
            p.write_text('---\ncommand: [git push, "gh pr create"]\n---\nPull first.\n')
            self.assertEqual(store.parse(p).commands, [['git', 'push'], ['gh', 'pr', 'create']])

    def test_both_matchers_share_a_flat_folder(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'push.md'
            p.write_text('---\ncommand: git push\nname: Push\napplies: "When pushing. Not: a fetch."\n---\nPull first.\n')
            exact, cue = store.load([d]), store.load_cue([d])
            self.assertEqual(exact.errors, [])
            self.assertEqual(cue.errors, [])
            self.assertEqual([e.id for e in exact.entries], ['push'])
            self.assertEqual([e.id for e in cue.entries], ['push'])
            self.assertEqual(exact.entries[0].version, cue.entries[0].version)

    def test_long_knowledge_keeps_its_authored_target(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'long.md'
            p.write_text('---\ncommand: git push\nmust-know-before: true\ntarget: "record.md"\n---\n' + 'x' * 1801)
            e = store.parse(p)
            self.assertEqual(e.text, 'x' * 1801)
            self.assertEqual(e.target, str(Path(d) / 'record.md'))


if __name__ == '__main__':
    unittest.main()
