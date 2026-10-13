"""Exact keys from structured tool input: command words, paths and error signatures.

A shell command is not parsed. It is cut into words at whitespace, quotes,
backslashes and shell punctuation, and a command key matches wherever its words
appear in that order, including inside quotes, heredocs, ``bash -c`` strings
and substitutions. The first word of a key also matches an executable path
(``/usr/bin/git``). Words that only appear as data therefore also match: a rare
false deny costs one model round and still delivers the rule, while modeling
shell syntax missed commands that ran (review rounds 1 and 2 on PR #23).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SHELL_TOOLS = {"Bash"}
PATH_FIELDS = ("file_path", "notebook_path", "path")
WORD_BREAK = re.compile(r"[\s;&|()<>`$\"'\\{}]+")


class Unsupported(ValueError):
    """Tool input whose shape the exact-key lane cannot read."""


@dataclass
class Keys:
    words: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    error: str | None = None

    def has_command(self, key: list[str]) -> bool:
        """``key`` appears as consecutive words; its first word may be an executable path."""
        n = len(key)
        for i, w in enumerate(self.words):
            if (w == key[0] or os.path.basename(w) == key[0]) and self.words[i + 1:i + n] == key[1:]:
                return True
        return False


def normalize_error(text: str) -> str:
    """Error signature: lowercase, digits collapsed, whitespace collapsed."""
    text = re.sub(r"\d+", "#", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def words(command: str) -> list[str]:
    return [w for w in WORD_BREAK.split(command) if w]


def shell_paths(ws: list[str]) -> list[str]:
    """Words of a shell command that name a file (as Chromeria's ``Matcher.ts``, #234): a
    ``/`` or leading ``~``, a ``file:`` URI without its query, ``VAR=path``, or a bare name
    with an extension. A word is a path by its shape; web URLs are not paths."""
    out: list[str] = []
    for word in ws:
        w = re.sub(r"^[A-Za-z_][A-Za-z0-9_]*=", "", word)
        if w.startswith("file:"):
            w = re.sub(r"[?#].*$", "", re.sub(r"^file:(?://)?", "", w))
        elif "://" in w or w.startswith("-"):
            continue
        shaped = "/" in w or w.startswith("~") or re.fullmatch(r"[^/]+\.[A-Za-z0-9]{1,10}", w) is not None
        if shaped and w and w not in out:
            out.append(w)
    return out


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
        keys.words = words(command)
        keys.paths = [repo_relative(os.path.expanduser(p), cwd) for p in shell_paths(keys.words)]
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
