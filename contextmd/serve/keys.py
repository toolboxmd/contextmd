"""Exact keys from structured tool input: command prefixes, paths and error signatures.

A shell command is split into segments on unquoted ``&``, ``|``, ``;``,
newlines and subshell brackets; quoted text is always an argument. Command
substitutions become segments of their own. Heredoc bodies are data and are
dropped.
Leading ``cd <dir>`` segments, ``env`` and ``NAME=value`` prefixes are
ignored, and the executable is reduced to its basename, so
``cd repo && env A=1 /usr/bin/git commit -m x`` yields ``git commit -m x``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SHELL_TOOLS = {"Bash"}
PATH_FIELDS = ("file_path", "notebook_path", "path")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n.*?\n\s*\2[ \t]*(?=\n|$)", re.S)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class Unsupported(ValueError):
    """Tool input whose shape the exact-key lane cannot read."""


@dataclass
class Keys:
    commands: list[list[str]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    error: str | None = None


def normalize_error(text: str) -> str:
    """Error signature: lowercase, digits collapsed, whitespace collapsed."""
    text = re.sub(r"\d+", "#", text.lower())
    return re.sub(r"\s+", " ", text).strip()


class _Scanner:
    """Quote-aware POSIX shell scanner: words per segment, operators only when unquoted.

    Command substitutions (``$(...)`` and backticks, also inside double quotes)
    run commands, so their contents become segments of their own and the
    enclosing word keeps a ``$()`` or ````` placeholder.
    """

    def __init__(self, text: str):
        self.text, self.i = text, 0
        self.segments: list[list[str]] = []
        self.inner: list[list[str]] = []
        self.cur: list[str] = []
        self.word: list[str] | None = None

    def add(self, chars: str) -> None:
        if self.word is None:
            self.word = []
        self.word.append(chars)

    def end_word(self) -> None:
        if self.word is not None:
            self.cur.append("".join(self.word))
            self.word = None

    def end_segment(self) -> None:
        self.end_word()
        if self.cur:
            self.segments.append(self.cur)
        self.cur = []

    def balanced(self, start: int) -> int:
        """Index just past the ``)`` closing a ``$(`` whose body starts at ``start``."""
        t, i, depth = self.text, start, 1
        while i < len(t):
            c = t[i]
            if c == "\\":
                i += 2
                continue
            if c == "'":
                j = t.find("'", i + 1)
                if j < 0:
                    break
                i = j + 1
                continue
            if c == '"':
                i = self.close_double(i + 1)
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise Unsupported("shell: unclosed $(")

    def close_double(self, i: int) -> int:
        t = self.text
        while i < len(t):
            if t[i] == "\\":
                i += 2
            elif t[i] == '"':
                return i + 1
            elif t.startswith("$(", i):
                i = self.balanced(i + 2)
            else:
                i += 1
        raise Unsupported("shell: unclosed double quote")

    def substitution(self) -> None:
        t = self.text
        if t.startswith("$(", self.i):
            end = self.balanced(self.i + 2)
            body, placeholder = t[self.i + 2:end - 1], "$()"
        else:
            end = self.i + 1
            while end < len(t) and t[end] != "`":
                end += 2 if t[end] == "\\" else 1
            if end >= len(t):
                raise Unsupported("shell: unclosed backtick")
            body, placeholder, end = t[self.i + 1:end], "``", end + 1
        self.inner.extend(segments(body))
        self.add(placeholder)
        self.i = end

    def double(self) -> None:
        t = self.text
        self.i += 1
        self.add("")
        while self.i < len(t):
            c = t[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\" and self.i + 1 < len(t):
                nxt = t[self.i + 1]
                if nxt != "\n":
                    self.add(nxt if nxt in '$`"\\' else c + nxt)
                self.i += 2
            elif t.startswith("$(", self.i) or c == "`":
                self.substitution()
            else:
                self.add(c)
                self.i += 1
        raise Unsupported("shell: unclosed double quote")

    def run(self) -> list[list[str]]:
        t = self.text
        while self.i < len(t):
            c = t[self.i]
            if c == "\\":
                if self.i + 1 < len(t) and t[self.i + 1] != "\n":
                    self.add(t[self.i + 1])
                self.i += 2
            elif c == "'":
                j = t.find("'", self.i + 1)
                if j < 0:
                    raise Unsupported("shell: unclosed single quote")
                self.add(t[self.i + 1:j])
                self.i = j + 1
            elif c == '"':
                self.double()
            elif c in " \t\r":
                self.end_word()
                self.i += 1
            elif c == "#" and self.word is None:
                nl = t.find("\n", self.i)
                self.i = len(t) if nl < 0 else nl
            elif c == "&" and self.word and self.word[-1].endswith((">", "<")):
                self.add(c)  # 2>&1 is a redirection, not a separator
                self.i += 1
            elif t.startswith("$(", self.i) or c == "`":
                self.substitution()
            elif c in "&|;\n()":
                self.end_segment()
                self.i += 1
            else:
                self.add(c)
                self.i += 1
        self.end_segment()
        return self.segments


def segments(command: str) -> list[list[str]]:
    scanner = _Scanner(HEREDOC.sub(lambda m: "<<HEREDOC" + m.group(3), command))
    cleaned = []
    for seg in scanner.run():
        while seg and (seg[0] == "env" or ASSIGNMENT.match(seg[0])):
            seg = seg[1:]
        if not seg or seg[0] == "cd":
            continue
        cleaned.append([os.path.basename(seg[0])] + seg[1:])
    return cleaned + scanner.inner


def repo_relative(path: str, cwd: str | None) -> str:
    """A path relative to the repository holding it, else the absolute path."""
    if cwd and not os.path.isabs(path):
        path = os.path.join(cwd, path)
    path = os.path.normpath(path)
    d = os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return os.path.relpath(path, d)
        parent = os.path.dirname(d)
        if parent == d:
            return path
        d = parent


def extract(tool: str, tool_input, cwd: str | None, error: str | None = None) -> Keys:
    """Keys for one tool call. Raises ``Unsupported`` for shapes it cannot read."""
    if not isinstance(tool_input, dict):
        raise Unsupported(f"{tool}: input is not an object")
    keys = Keys(error=normalize_error(error) if error else None)
    if tool in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise Unsupported(f"{tool}: no command")
        keys.commands = segments(command)
        return keys
    for name in PATH_FIELDS:
        value = tool_input.get(name)
        if isinstance(value, str) and value:
            keys.paths.append(repo_relative(value, cwd))
    if not keys.paths and keys.error is None:
        raise Unsupported(f"{tool}: no command or path")
    return keys


def glob_regex(pattern: str) -> re.Pattern:
    """``**`` spans directories, ``*`` and ``?`` stay inside one path part."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")
