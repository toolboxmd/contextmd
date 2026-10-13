"""The exact-key store: Markdown entries keyed by command, path or error.

Each entry is one ``.md`` file in a store directory::

    ---
    id: pr-template
    command: gh pr create
    path: .github/**
    error: permission denied (publickey)
    must-know-before: true
    ---
    Answer every prompt of the PR template before opening a PR.

``command``, ``path`` and ``error`` may repeat; any one key matching serves the
entry. ``id`` defaults to the file name. A must-know-before entry with
``target: <file>`` is a pointer rule: its deny names the file, and it lifts once
the agent is seen reading that file. Without ``target`` it is an inline rule,
whose text is in the deny itself. A relative target is relative to the entry's
directory. The version is derived from the file
bytes, so editing an entry makes it new for deny-once. Store directories are
usually owner repositories (a Library, the Great Library); the commit each
was loaded at goes into the serving log.

Cue entries live in their own directories (the ``cue`` list of the serving
config), never inside an exact-key directory, because each loader rejects the
other's fields::

    ---
    id: agentsmd-research
    name: Research
    applies: The request asks a question whose answer is facts beyond what is at hand. Not: ...
    target: ../skills/operations/workflows/research/index.md
    ---
    AgentsMD: follow the research procedure.

Cue scores ``"<name>: <applies>"`` (the text it was trained on, so it is never
rewritten for display). ``target`` is optional; the served text ends with
"Read <target> in full."
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .keys import Keys, glob_regex, normalize_error

# Claude Code moves hook context over its limit into a file and shows the model
# a 2 KB preview (canary, #5), so one entry must fit well inside that, in UTF-8 bytes.
MAX_ENTRY_BYTES = 1800
# Entry ids appear in logs and in the short deny pointer, which must stay small.
MAX_ID_CHARS = 120
KEY_FIELDS = ("command", "path", "error")
OTHER_FIELDS = ("id", "must-know-before", "target")
CUE_FIELDS = ("id", "name", "applies", "target")


@dataclass
class Entry:
    id: str
    version: str
    text: str
    must_know_before: bool = False
    path: str = ""
    target: str | None = None
    commands: list[list[str]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def matches(self, keys: Keys) -> str | None:
        """The first key of this entry that matches, as ``kind:value``, else None."""
        for prefix in self.commands:
            if keys.has_command(prefix):
                return "command:" + " ".join(prefix)
        for pattern in self.paths:
            rx = glob_regex(pattern)
            if any(rx.match(p) for p in keys.paths):
                return "path:" + pattern
        if keys.error:
            for sig in self.errors:
                if sig in keys.error:
                    return "error:" + sig
        return None


@dataclass
class Store:
    entries: list[Entry]
    owners: dict[str, str | None]
    errors: list[str]


@dataclass
class CueEntry:
    id: str
    version: str
    name: str
    applies: str
    text: str
    path: str = ""
    target: str | None = None

    @property
    def doc(self) -> str:
        """What Cue scores: the entry as it was trained on."""
        return f"{self.name}: {self.applies}"


@dataclass
class CueStore:
    entries: list[CueEntry]
    owners: dict[str, str | None]
    errors: list[str]


def _front_matter(path: Path, allowed) -> tuple[bytes, dict[str, list[str]], str]:
    """The file bytes, its front-matter fields and its body; raises on a malformed entry."""
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing front matter")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("front matter is not closed") from None
    fields: dict[str, list[str]] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"not a 'name: value' line: {line!r}")
        fields.setdefault(name.strip(), []).append(value.strip())
    unknown = set(fields) - set(allowed)
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    body = "\n".join(lines[end + 1:]).strip()
    if not body:
        raise ValueError("empty body")
    if len(body.encode()) > MAX_ENTRY_BYTES:
        raise ValueError(f"body is {len(body.encode())} UTF-8 bytes, over {MAX_ENTRY_BYTES}")
    return raw, fields, body


def _entry_id(fields: dict[str, list[str]], path: Path) -> str:
    entry_id = (fields.get("id") or [path.stem])[-1]
    if len(entry_id) > MAX_ID_CHARS:
        raise ValueError(f"id is {len(entry_id)} characters, over {MAX_ID_CHARS}")
    return entry_id


def _target(value: str, path: Path) -> str:
    return os.path.realpath(os.path.join(path.parent, os.path.expanduser(value)))


def parse(path: Path) -> Entry:
    raw, fields, body = _front_matter(path, {*OTHER_FIELDS, *KEY_FIELDS})
    flag = (fields.get("must-know-before") or ["false"])[-1].lower()
    if flag not in ("true", "false"):
        raise ValueError("must-know-before is not true or false")
    target = (fields.get("target") or [None])[-1]
    if target is not None:
        if flag != "true":
            raise ValueError("target is only for must-know-before entries")
        target = _target(target, path)
    entry = Entry(
        id=_entry_id(fields, path),
        version=hashlib.sha256(raw).hexdigest()[:12],
        text=body,
        must_know_before=flag == "true",
        path=str(path),
        target=target,
        commands=[c.split() for c in fields.get("command", []) if c.split()],
        paths=[p for p in fields.get("path", []) if p],
        errors=[normalize_error(e) for e in fields.get("error", []) if e],
    )
    if not (entry.commands or entry.paths or entry.errors):
        raise ValueError("no command, path or error key")
    return entry


def parse_cue(path: Path) -> CueEntry:
    raw, fields, body = _front_matter(path, CUE_FIELDS)
    name = (fields.get("name") or [""])[-1]
    applies = (fields.get("applies") or [""])[-1]
    if not name:
        raise ValueError("no name")
    if not applies:
        raise ValueError("no applies")
    target = (fields.get("target") or [None])[-1]
    return CueEntry(
        id=_entry_id(fields, path),
        version=hashlib.sha256(raw).hexdigest()[:12],
        name=name,
        applies=applies,
        text=body,
        path=str(path),
        target=_target(target, path) if target else None,
    )


def head_commit(directory: Path) -> str | None:
    """The commit checked out in the repository holding ``directory``, read without git."""
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


def _load(directories: list[str], parser):
    entries, owners, errors, seen = [], {}, [], {}
    for directory in directories:
        root = Path(os.path.expanduser(directory))
        if not root.is_dir():
            errors.append(f"{root}: not a directory")
            continue
        owners[str(root)] = head_commit(root)
        for path in sorted(root.rglob("*.md")):
            if ".git" in path.parts or path.name.lower() == "readme.md":
                continue
            try:
                entry = parser(path)
            except (ValueError, UnicodeDecodeError) as exc:
                errors.append(f"{path}: {exc}")
                continue
            if entry.id in seen:
                errors.append(f"{path}: id {entry.id!r} already used by {seen[entry.id]}")
                continue
            seen[entry.id] = path
            entries.append(entry)
    return entries, owners, errors


def load(directories: list[str]) -> Store:
    return Store(*_load(directories, parse))


def load_cue(directories: list[str]) -> CueStore:
    return CueStore(*_load(directories, parse_cue))
