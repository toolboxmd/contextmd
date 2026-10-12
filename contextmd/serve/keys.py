"""Exact keys from structured tool input: command prefixes, paths and error signatures.

A shell command is split into segments on ``&&``, ``||``, ``;``, ``|``,
newlines and subshell brackets. Heredoc bodies are data and are dropped.
Leading ``cd <dir>`` segments, ``env`` and ``NAME=value`` prefixes are
ignored, and the executable is reduced to its basename, so
``cd repo && env A=1 /usr/bin/git commit -m x`` yields ``git commit -m x``.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

SHELL_TOOLS = {"Bash"}
PATH_FIELDS = ("file_path", "notebook_path", "path")
SEPARATORS = {"&&", "||", ";", "|", "&", "\n", "(", ")", "$", ";;", "|&"}
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


def _tokens(command: str) -> list[str]:
    command = HEREDOC.sub(lambda m: "<<HEREDOC" + m.group(3), command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as exc:  # unbalanced quotes
        raise Unsupported(f"shell: {exc}") from None


def segments(command: str) -> list[list[str]]:
    out, cur = [], []
    for tok in _tokens(command) + ["\n"]:
        if tok in SEPARATORS:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    cleaned = []
    for seg in out:
        while seg and (seg[0] == "env" or ASSIGNMENT.match(seg[0])):
            seg = seg[1:]
        if not seg or seg[0] == "cd":
            continue
        cleaned.append([os.path.basename(seg[0])] + seg[1:])
    return cleaned


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
