"""Exact keys from structured tool input: command words, paths and error signatures.
Command keys keep unquoted words and quoted words whose dequoted value has no
whitespace, drop quoted prose and literal heredoc bodies, and scan executed
substitutions and dequoted shell ``-c`` scripts (toolboxmd/chromeria#232).
Shell names followed by ``-c`` options count anywhere, including a printed invocation
(``echo bash -c '...'``); Chromeria's ``Matcher.ts`` follows this rule and paths see raw words (#234).
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


# Literal markers use the same ASCII shell boundaries in both languages, including Unicode.
HEREDOC_START = re.compile(r"""<<(-?)[ \t]*(['"]?)([^ \t\r\n;&|()<>'"\\]+)\2(?=$|[ \t\r\n;&|()<>])""")
SHELL_BREAK = re.compile(r"[ \t\r\n;&|()<>]")
REDIRECTION = re.compile(r"(?:[0-9]+)?(<<<|[<>][&<>|]?)")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
ANSI_ESCAPES = {
    "a": "\x07", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
    "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\",
    "'": "'", '"': '"', "?": "?",
}
ANSI_NUMBER = re.compile(r"\\(?:([0-7]{1,3})|x([A-Fa-f0-9]{1,2})|u([A-Fa-f0-9]{1,4})|U([A-Fa-f0-9]{1,8}))")


def _quoted_end(text: str, start: int) -> int:
    """A quote inside an expansion belongs to that expansion, not the enclosing string."""
    quote, i = text[start], start + 1
    ansi = quote == "'" and start > 0 and text[start - 1] == "$"
    while i < len(text):
        if text[i] == quote:
            return i
        if (quote == '"' or ansi) and text[i] == "\\":
            i += 2
            continue
        expansion = _expansion_at(text, i) if quote == '"' else None
        i = expansion[1] if expansion else i + 1
    return i


def _expansion_end(text: str, start: int, close: str) -> int:
    """Find a balanced expansion boundary, ignoring delimiters in quotes or nested expansions."""
    i, depth = start, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if depth == 0 and text.startswith(close, i):
            return i
        # Legacy backtick boundaries are found before the body is parsed as shell code.
        if close == "`":
            i += 1
            continue
        if ch in "'\"":
            i = _quoted_end(text, i) + 1
            continue
        expansion = _expansion_at(text, i)
        if expansion:
            i = expansion[1]
            continue
        if ch == ("{" if close == "}" else "("):
            depth += 1
        elif depth > 0 and ch == close[0]:
            depth -= 1
        i += 1
    return len(text)


def _expansion_at(text: str, start: int) -> tuple[str, int, bool] | None:
    backtick = text.startswith("`", start)
    arithmetic = text.startswith("$((", start)
    parameter = text.startswith("${", start)
    if not backtick and not parameter and not text.startswith("$(", start):
        return None
    width = 1 if backtick else 3 if arithmetic else 2
    close = "`" if backtick else "}" if parameter else "))" if arithmetic else ")"
    stop = _expansion_end(text, start + width, close)
    body = text[start + width:stop]
    # Backticks remove one escape layer before the shell reads their command body.
    if backtick:
        body = re.sub(r"\\([$`\\])", r"\1", body)
    return body, min(stop + len(close), len(text)), not arithmetic and not parameter


def _substitutions(text: str) -> str:
    """Keep executable substitutions in expanded data; skip escape pairs to preserve parity."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        expansion = _expansion_at(text, i)
        if not expansion:
            i += 1
            continue
        body, i, command = expansion
        out.append(visible_command(body) if command else _substitutions(body))
    return " ; ".join(out)


def _shell_word(text: str, start: int) -> tuple[str, str, bool, str, int]:
    """One shell word: dequote its value, retaining unquoted fragments and substitutions."""
    i, quote, value, plain, runs = start, "", "", "", []
    quoted = False
    ansi_start = 0
    while i < len(text):
        ch = text[i]
        if not quote and SHELL_BREAK.fullmatch(ch):
            break
        if ch == quote or (quote == "$'" and ch == "'"):
            if quote == "$'":
                # Bash cannot keep a NUL in a word; only this ANSI-C segment ends there.
                nul = value.find("\0", ansi_start)
                if nul >= 0:
                    value = value[:nul]
            quote = ""
            i += 1
            continue
        if not quote and text.startswith("$'", i):
            quote = "$'"
            quoted = True
            ansi_start = len(value)
            i += 2
            continue
        if not quote and ch in "'\"":
            quote = ch
            quoted = True
            i += 1
            continue
        if ch == "\\" and quote != "'":
            next_char = text[i + 1:i + 2]
            if quote == "$'":
                numeric = ANSI_NUMBER.match(text, i)
                if numeric:
                    digits = next(group for group in numeric.groups() if group is not None)
                    code = int(digits, 8 if numeric.group(1) is not None else 16)
                    value += chr(code) if code <= 0x10ffff else numeric.group(0)
                    i = numeric.end()
                else:
                    value += ANSI_ESCAPES.get(next_char, ch + next_char)
                    i += 2
                continue
            if not quote or next_char in "$`\"\\\n":
                if next_char != "\n":
                    value += next_char
            else:
                value += ch + next_char
            if not quote and next_char != "\n":
                plain += next_char
            i += 2
            continue
        expansion = _expansion_at(text, i) if quote not in ("'", "$'") else None
        if expansion:
            body, end, command = expansion
            raw = text[i:end]
            value += raw
            if not quote and not command:
                plain += raw
            runs.append(visible_command(body) if command else _substitutions(body))
            i = end
            continue
        value += ch
        if not quote:
            plain += ch
        i += 1
    return value, plain, quoted, " ; ".join(runs), i


def visible_command(command: str) -> str:
    """Keep single quoted tokens anywhere; only quoted text with whitespace is a mention.
    Shell names count anywhere; only the first script word after their ``-c`` options is scanned."""
    text, out, i, heredocs = command, "", 0, []
    shell = "none"
    while i < len(text):
        ch = text[i]
        redirection = REDIRECTION.match(text, i)
        operator = redirection.end() - len(redirection.group(1)) if redirection else i
        opening = HEREDOC_START.match(text, operator) if redirection and redirection.group(1) == "<<" else None
        if opening:
            heredocs.append((opening.group(3), opening.group(2) == "", opening.group(1) == "-"))
            out += " "
            i = opening.end()
            continue
        if ch == "\n" and heredocs:
            j, kept = i + 1, ""
            for word, expands, strip_tabs in heredocs:
                # Search the body itself, so ``^`` anchors at its start (an empty heredoc).
                tabs = r"\t*" if strip_tabs else ""
                end = re.compile(rf"(^|\n){tabs}{re.escape(word)}(?=\n|$)").search(text[j:])
                body_end = j + end.start() if end else len(text)
                if expands:
                    kept += f" {_substitutions(text[j:body_end])} "
                j = j + end.end() if end else len(text)
            heredocs = []
            out += kept + "\n"
            i = j
            continue
        if ch in " \t\r\n;&|()":
            if ch in "\n;&|()":
                shell = "none"
            out += ch
            i += 1
            continue
        if redirection:
            # A here-string consumes one word, without opening a body on following lines.
            out += redirection.group(0)
            i = redirection.end()
            continue
        value, plain, quoted, runs, end = _shell_word(text, i)
        kept = ("" if re.search(r"\s", value) else value) if quoted else plain
        if shell == "script":
            kept = visible_command(value)
            shell = "none"
        elif (not quoted or not re.search(r"\s", value)) and value.rsplit("/", 1)[-1] in SHELLS:
            shell = "options"
        elif shell == "options" and re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", value):
            shell = "script"
        out += f" {kept} {runs} "
        i = end
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
