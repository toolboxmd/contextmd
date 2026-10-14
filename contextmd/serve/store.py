"""Flat Libraries: strict YAML headers, exact-key and Cue views of one entry.

Registered stores temporarily tolerate repeated keys and unquoted colon text.
New entries use strict ``parse_file``; historical card reads may opt into tolerance.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISREG

from .keys import Keys, glob_regex, normalize_error

MAX_ENTRY_BYTES = 1800
MAX_ID_CHARS = 120
KEY_FIELDS = ('command', 'path', 'error')
FIELDS = ('id', *KEY_FIELDS, 'must-know-before', 'name', 'applies', 'target')
CONTROL = re.compile(r'[\x00-\x08\x0a-\x1f\x7f-\x9f]')
PLAIN_INVALID = re.compile(r'[\ufeff\ufffe\uffff]')
ERROR_BLANK = re.compile(r'^[\u0009-\u000d\u001c-\u001f\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$')
NUMBER = re.compile(r'^(?:0o[0-7]+|[-+]?[0-9]+|0x[0-9a-fA-F]+|[-+]?\.(?:inf|Inf|INF)|\.nan|\.NaN|\.NAN|[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)[eE][-+]?[0-9]+|[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*))$')


class EntryError(ValueError):
    def __init__(self, code, reason, line=None, field=None):
        self.code, self.reason, self.line, self.field = code, reason, line, field
        super().__init__((f'line {line}: ' if line else '') + reason)


@dataclass
class Entry:
    id: str
    version: str
    text: str
    must_know_before: bool = False
    path: str = ''
    target: str | None = None
    commands: list[list[str]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    long: bool = False
    legacy: str | None = None

    def matches(self, keys: Keys) -> str | None:
        for prefix in self.commands:
            if keys.has_command(prefix):
                return 'command:' + ' '.join(prefix)
        for pattern in self.paths:
            rx = glob_regex(pattern)
            if any(rx.match(p) for p in keys.paths):
                return 'path:' + pattern
        if keys.error:
            for sig in self.errors:
                if sig in keys.error:
                    return 'error:' + sig
        return None


@dataclass
class CueEntry:
    id: str
    version: str
    name: str
    applies: str
    text: str
    path: str = ''
    target: str | None = None
    long: bool = False
    legacy: str | None = None

    @property
    def doc(self) -> str:
        return f'{self.name}: {self.applies}'


@dataclass
class Parsed:
    id: str
    version: str
    path: str
    body: str
    fields: dict
    long: bool
    legacy: str | None = None

    def views(self):
        f = self.fields
        target = _target(f['target'], Path(self.path)) if 'target' in f else None
        common = dict(id=self.id, version=self.version, text=self.body, path=self.path,
                      target=target, long=self.long, legacy=self.legacy)
        def values(name):
            v = f.get(name, [])
            return v if isinstance(v, list) else [v]
        exact = Entry(**common, must_know_before=f.get('must-know-before', False),
                      commands=[re.split(r'[ \t]+', c.strip(' \t')) for c in values('command')],
                      paths=values('path'), errors=[normalize_error(e) for e in values('error')]) if any(k in f for k in KEY_FIELDS) else None
        cue = CueEntry(**common, name=f['name'], applies=f['applies']) if 'name' in f else None
        return exact, cue


@dataclass
class Source:
    directory: str
    registered: bool = True


@dataclass
class Library:
    entries: list[Entry]
    cue: list[CueEntry]
    owners: dict[str, str | None]
    errors: list[str]
    legacy: list[str]


@dataclass
class CueStore:
    entries: list[CueEntry]
    owners: dict[str, str | None]
    errors: list[str]
    legacy: list[str]


def _scalar(value, line, tolerant=False):
    if not value:
        return None
    if value[0] == "'":
        out, i = '', 1
        while i < len(value):
            if value[i] == "'":
                if i + 1 < len(value) and value[i + 1] == "'":
                    out += "'"
                    i += 2
                    continue
                if value[i + 1:].strip(' \t'):
                    raise EntryError('syntax', 'unexpected text after quoted value', line)
                break
            out += value[i]
            i += 1
        else:
            raise EntryError('syntax', 'unclosed quoted value', line)
    elif value[0] == '"':
        try:
            # YAML permits raw tabs; the shared control policy still checks the decoded string.
            out = json.loads(value, strict=False)
        except ValueError:
            raise EntryError('syntax', 'invalid quoted value', line) from None
    else:
        if PLAIN_INVALID.search(value):
            raise EntryError('syntax', 'invalid character in plain value or comment', line)
        if re.search(r'[ \t]#', value):
            raise EntryError('syntax', 'trailing comments are unsupported; quote the value', line)
        legacy = value[0] in '*@`%' or bool(re.search(r':[ \t]|:$', value))
        if value[0] in '-?:,[]{}#&*!|>\'"%@`' or legacy:
            if not (tolerant and legacy and value[0] not in '[\'"' and value[0] not in '-?:,]{}#&!|>'):
                raise EntryError('syntax', 'quote the value', line)
        if not legacy:
            if value in ('~', 'null', 'Null', 'NULL'):
                return None
            if value in ('true', 'True', 'TRUE', 'false', 'False', 'FALSE'):
                return value.lower() == 'true'
            if NUMBER.fullmatch(value):
                return 0  # All numeric fields are rejected by the typed header below.
        out = value
    if CONTROL.search(out) or any(0xD800 <= ord(c) <= 0xDFFF for c in out):
        raise EntryError('syntax', 'control character or lone surrogate in value', line)
    return out


def _value(value, line, tolerant):
    if not value.startswith('['):
        return _scalar(value, line, tolerant)
    if not value.endswith(']'):
        raise EntryError('syntax', 'unclosed flow list', line)
    inner = value[1:-1]
    if not inner.strip(' \t'):
        return []
    parts, start, quote, i = [], 0, None, 0
    while i < len(inner):
        c = inner[i]
        if quote:
            if quote == '"' and c == '\\':
                i += 2
                continue
            if c == quote:
                if quote == "'" and i + 1 < len(inner) and inner[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        elif c in '\'"' and not inner[start:i].strip(' \t'):
            quote = c
        elif c in '[]{}':
            raise EntryError('syntax', 'nested collections are unsupported', line)
        elif c == ',':
            parts.append(inner[start:i].strip(' \t'))
            start = i + 1
        i += 1
    parts.append(inner[start:].strip(' \t'))
    if quote or any(not p for p in parts):
        raise EntryError('syntax', 'invalid flow list', line)
    return [_scalar(p, line) for p in parts]


def _strict(path, raw, tolerant):
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise EntryError('encoding', 'not UTF-8') from None
    text = text.removeprefix('\ufeff').replace('\r\n', '\n')
    if '\r' in text:
        raise EntryError('encoding', 'bare carriage return', text[:text.index('\r')].count('\n') + 1)
    lines = text.split('\n')
    if lines[0].rstrip(' \t') != '---':
        raise EntryError('no_header', 'missing front matter')
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip(' \t') == '---'), None)
    if end is None:
        raise EntryError('no_header', 'front matter is not closed')
    fields, locations = {}, {}
    for i, line in enumerate(lines[1:end], 2):
        if CONTROL.search(line):
            raise EntryError('encoding', 'control character in header', i)
        if not line.strip(' \t'):
            continue
        if re.match(r'^ *#', line):
            if PLAIN_INVALID.search(line):
                raise EntryError('syntax', 'invalid character in plain value or comment', i)
            continue
        m = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]+(.*))?', line)
        if not m:
            raise EntryError('syntax', 'expected a field at column zero', i)
        name, value = m[1], (m[2] or '').strip(' \t')
        if name in fields and not (tolerant and name in KEY_FIELDS):
            raise EntryError('duplicate_field', 'repeated field; use a flow list', i, name)
        parsed = _value(value, i, tolerant)
        if name in fields:
            old = fields[name]
            fields[name] = (old if isinstance(old, list) else [old]) + (parsed if isinstance(parsed, list) else [parsed])
        else:
            fields[name], locations[name] = parsed, i
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise EntryError('unknown_field', f'unknown fields: {unknown}')
    for name in FIELDS:
        if name not in fields:
            continue
        v = fields[name]
        if v is None or v == '' or v == []:
            raise EntryError('empty_value', 'empty field value', locations[name], name)
        values = v if isinstance(v, list) and name in KEY_FIELDS else [v]
        for item in values:
            if item is None or item == '':
                raise EntryError('empty_value', 'empty field value', locations[name], name)
            if (type(item) is not bool if name == 'must-know-before' else not isinstance(item, str)):
                raise EntryError('wrong_type', 'wrong field type', locations[name], name)
            if name == 'command' and not item.strip(' \t'):
                raise EntryError('empty_value', 'empty command key', locations[name], name)
            if name == 'error' and ERROR_BLANK.fullmatch(item):
                raise EntryError('empty_value', 'empty error key', locations[name], name)
    entry_id = fields.get('id', path.name[:-3])
    if not entry_id:
        raise EntryError('empty_value', 'empty entry id', field='id')
    if len(entry_id) > MAX_ID_CHARS:
        raise EntryError('id_too_long', f'id is {len(entry_id)} characters, over {MAX_ID_CHARS}', field='id')
    body = '\n'.join(lines[end + 1:]).strip(' \t\n')
    if not body:
        raise EntryError('empty_body', 'empty body')
    exact, cue = any(k in fields for k in KEY_FIELDS), any(k in fields for k in ('name', 'applies'))
    if not exact and not cue:
        raise EntryError('no_matcher', 'no command, path or error key or Cue fields')
    if cue and not all(k in fields for k in ('name', 'applies')):
        missing = 'name' if 'name' not in fields else 'applies'
        raise EntryError('cue_incomplete', f'no {missing}')
    if fields.get('must-know-before') and not exact:
        raise EntryError('must_know_without_key', 'must-know-before needs an exact key')
    return Parsed(entry_id, hashlib.sha256(raw).hexdigest()[:12], str(path), body, fields,
                  len(body.encode('utf-8')) > MAX_ENTRY_BYTES)


def parse_file(path: Path, raw: bytes | None = None, *, tolerant=False) -> Parsed:
    raw = path.read_bytes() if raw is None else raw
    try:
        return _strict(path, raw, False)
    except EntryError as strict:
        if not tolerant:
            raise
        result = _strict(path, raw, True)
        result.legacy = str(strict)
        return result


def _target(value: str, path: Path) -> str:
    if value == '~':
        value = str(Path.home())
    elif value.startswith(('~/', '~\\')):
        value = os.path.join(Path.home(), value[2:])
    lexical = os.path.abspath(os.path.join(path.parent, value))
    return os.path.realpath(lexical) if os.path.exists(lexical) else lexical


def parse(path: Path, raw: bytes | None = None) -> Entry:
    entry, _ = parse_file(path, raw).views()
    if entry is None:
        raise EntryError('no_matcher', 'no command, path or error key')
    return entry


def parse_cue(path: Path) -> CueEntry:
    _, cue = parse_file(path, tolerant=True).views()
    if cue is None:
        raise EntryError('cue_incomplete', 'no name or applies')
    return cue


def library_dir(thread_dir: str) -> str | None:
    original = Path(thread_dir).expanduser()
    if not original.is_dir():
        return None
    original = original.resolve()
    root = original
    for candidate in (original, *original.parents):
        dot = candidate / '.git'
        if dot.is_dir() or dot.is_file():
            root = candidate
            break
    library = root / 'library'
    return str(library.resolve()) if library.is_dir() else None


def entry_files(root: Path):
    return [p for p in _listed_entries(root) if p.is_file()]


def _listed_entries(root: Path):
    return sorted((p for p in root.iterdir() if p.name.endswith('.md') and p.name.lower() != 'readme.md'), key=lambda p: p.name)



def head_commit(directory: Path) -> str | None:
    """The commit checked out in the repository holding ``directory``, read without git."""
    try:
        d = directory.resolve()
        while True:
            dot = d / ".git"
            if dot.is_file():  # a worktree: "gitdir: <path>"
                dot = Path(dot.read_text().split(":", 1)[1].strip())
                if not dot.is_absolute():
                    dot = d / dot
            if dot.is_dir():
                head = (dot / "HEAD").read_text().strip()
                if not head.startswith("ref: "):
                    return head
                ref = head[5:]
                common = dot
                if (dot / "commondir").exists():
                    common = (dot / (dot / "commondir").read_text().strip()).resolve()
                for base in (dot, common):
                    if (base / ref).exists():
                        return (base / ref).read_text().strip()
                packed = common / "packed-refs"
                if packed.exists():
                    for line in packed.read_text().splitlines():
                        sha, _, name = line.partition(" ")
                        if name == ref:
                            return sha
                return None
            if d.parent == d:
                return None
            d = d.parent

    except (OSError, UnicodeError, IndexError):
        return None


def load(sources: list[Source | str]) -> Library:
    sources = [Source(s) if isinstance(s, str) else s for s in sources]
    registered = {os.path.realpath(os.path.expanduser(s.directory)) for s in sources if s.registered}
    entries, cue, owners, errors, legacy = [], [], {}, [], []
    roots, exact_ids, cue_ids = set(), {}, {}
    for source in sources:
        root = Path(os.path.expanduser(source.directory)).resolve()
        if root in roots:
            continue
        roots.add(root)
        if not root.is_dir():
            if source.registered:
                errors.append(f'{root}: not a directory')
            continue
        owners[str(root)] = head_commit(root)
        try:
            listed = _listed_entries(root)
        except OSError:
            errors.append(f'{root}: unreadable directory')
            continue
        for path in listed:
            try:
                if not S_ISREG(path.stat().st_mode):
                    continue
                parsed = parse_file(path, tolerant=str(root) in registered)
                exact, intent = parsed.views()
            except OSError:
                errors.append(f'{path}: unreadable')
                continue
            except EntryError as exc:
                errors.append(f'{path}: {exc}')
                continue
            prior = exact_ids.get(parsed.id) if exact else None
            prior = prior or (cue_ids.get(parsed.id) if intent else None)
            if prior:
                errors.append(f'{path}: id {parsed.id!r} already used by {prior}')
                continue
            if exact:
                exact_ids[parsed.id] = path
                entries.append(exact)
            if intent:
                cue_ids[parsed.id] = path
                cue.append(intent)
            if parsed.legacy:
                legacy.append(f'{path}: {parsed.legacy}')
    return Library(entries, cue, owners, errors, legacy)


def load_cue(directories: list[str]) -> CueStore:
    library = load(directories)
    return CueStore(library.cue, library.owners, library.errors, library.legacy)
