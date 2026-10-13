"""Exact keys from structured tool input: command words, paths and error signatures.

A shell command is not parsed in general. Command keys match on the parts that
run (``visible_command``, toolboxmd/chromeria#232): quoted strings and heredoc
bodies are only mentions and are skipped, while ``sh -c`` strings, unescaped
``$(...)`` and backticks inside double quotes or an unquoted-marker heredoc, and
a quoted command word still count. The rest is cut into words at whitespace and
shell punctuation, and a key matches where its words appear in order; the first
word may be an executable path (``/usr/bin/git``), and an unquoted argument
still matches a bare key. Path keys see every word (#234). Chromeria's serving
matcher (``Matcher.ts``) follows the same rule.
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


HEREDOC_START = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1")
# An escaped ``\$(`` or backtick is literal text, not a substitution.
SUBSTITUTION = re.compile(r"(?<!\\)\$\(([^()]*)\)|(?<!\\)`([^`]*)`")
SEPARATOR = re.compile(r"(?:^|[;&|(\n]|\$\()[ \t]*\S*$")
SHELLS = {"sh", "bash", "zsh", "dash"}


def _substitutions(text: str) -> str:
    return " ; ".join(visible_command(a or b) for a, b in SUBSTITUTION.findall(text))


def visible_command(command: str) -> str:
    """The parts of a shell command that run, for command keys (as Chromeria's ``Matcher.ts``,
    toolboxmd/chromeria#232): quoted strings and heredoc bodies are dropped; the string after
    ``sh -c`` runs, as do ``$(...)`` and backticks inside double quotes or an unquoted-marker
    heredoc, unless escaped, and a quoted word in command position (``'gh' pr create``)."""
    text, out, i, heredocs = command, "", 0, []
    while i < len(text):
        ch = text[i]
        opening = HEREDOC_START.match(text, i) if text.startswith("<<", i) and not text.startswith("<<<", i) else None
        if opening:
            heredocs.append((opening.group(2), opening.group(1) == ""))
            out += " "
            i = opening.end()
            continue
        if ch == "\n" and heredocs:
            j, kept = i + 1, ""
            for word, expands in heredocs:
                # Search the body itself, so ``^`` anchors at its start (an empty heredoc).
                end = re.compile(rf"(^|\n)[ \t]*{word}[ \t]*(?=\n|$)").search(text[j:])
                body_end = j + end.start() if end else len(text)
                if expands:
                    kept += f" {_substitutions(text[j:body_end])} "
                j = j + end.end() if end else len(text)
            heredocs = []
            out += kept + "\n"
            i = j
            continue
        if ch == "\\":
            out += text[i:i + 2]
            i += 2
            continue
        if ch not in "'\"":
            out += ch
            i += 1
            continue
        j = i + 1
        while j < len(text) and text[j] != ch:
            j += 2 if ch == '"' and text[j] == "\\" else 1
        body = text[i + 1:j]
        before = out.rstrip().split()
        shell = before[-2].rsplit("/", 1)[-1] if len(before) >= 2 else ""
        if shell in SHELLS and re.fullmatch(r"-[a-z]*c", before[-1]):
            out += f" {visible_command(body)} "
        elif SEPARATOR.search(out) and not re.search(r"\s", body):
            out += body
        elif ch == '"':
            out += f" {_substitutions(body)} "
        else:
            out += " "
        i = j + 1
    return out


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
        keys.words = words(visible_command(command))
        keys.paths = [repo_relative(os.path.expanduser(p), cwd) for p in shell_paths(words(command))]
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
