"""Install the thin serving hook for one harness, pointed at one Chromeria base directory.

``contextmd-serve install <harness> --base ~/.t3`` writes the harness's user-level
hook configuration so every hook event goes to the serving module of the
Chromeria server that owns ``<base>``. The module matches events against the
stores named in ContextMD's serving config itself; nothing else is registered.

The hook authenticates as an ordinary Chromeria session: install runs
Chromeria's own ``t3 auth session issue`` (subject ``contextmd-hook``, scope
``orchestration:read``, 365 days) and stores the result, token included, in
``<base>/userdata/contextmd/hook-credential`` (mode 0600). Installing again
issues a new session and revokes the one it replaces; ``t3 auth session
revoke`` ends it. The hook finds the server through Chromeria's own
``<base>/userdata/server-runtime.json``. Installing again replaces only
ContextMD's own hooks; other hooks stay. Every file it changes is first copied
to ``<file>.contextmd-backup``.

- Claude Code: ``~/.claude/settings.json`` (or ``$CLAUDE_CONFIG_DIR``).
- Codex: ``hooks.json`` in every Codex home the Chromeria Codex instances use
  (``~/.codex`` by default), and each new hook's trust hash in ``config.toml``.
- Grok Build: ``~/.grok/hooks/contextmd.json``.
- OpenCode: ``~/.config/opencode/plugins/contextmd.js``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "bin" / "contextmd-hook"
MARK = "contextmd-hook"


class InstallError(RuntimeError):
    pass


def paths(base: Path) -> dict[str, Path]:
    d = base / "userdata" / "contextmd"
    return {"dir": d, "credential": d / "hook-credential", "off": d / "off",
            "runtime": base / "userdata" / "server-runtime.json"}


def hook_command(harness: str, base: Path) -> str:
    return " ".join(shlex.quote(a) for a in ("/bin/sh", str(HOOK), harness, str(base)))


# The hook events each command-hook harness sends; they must match the harness profiles in
# Chromeria's serving module (apps/server/src/contextmd/Harnesses.ts).
EVENTS = {
    "claude": ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"],
    "codex": ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostCompact", "Stop"],
    "grok": ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostCompact",
             "Stop"],
}
HARNESSES = sorted([*EVENTS, "opencode"])


def hooks_block(harness: str, base: Path) -> dict:
    """The ``hooks`` block (Claude Code, Codex, Grok) that forwards every serving event."""
    group = {"hooks": [{"type": "command", "command": hook_command(harness, base), "timeout": 5}]}
    return {"hooks": {e: [group] for e in EVENTS[harness]}}


def plugin_source(base: Path) -> str:
    src = (Path(__file__).resolve().parent / "opencode" / "contextmd.js").read_text()
    return src.replace('"__BASE__"', json.dumps(str(base)))


def _write(path: Path, text: str, mode: int | None = None, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_name(path.name + ".contextmd-backup"))
    tmp = path.with_name(path.name + ".contextmd-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode if mode is not None else 0o644)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def issue_credential(base: Path, t3: str) -> Path:
    """Issue the hooks' Chromeria session and store it; revoke the session it replaces."""
    p = paths(base)
    old = None
    try:
        old = json.loads(p["credential"].read_text()).get("sessionId")
    except (OSError, ValueError):
        pass
    done = subprocess.run([t3, "auth", "session", "issue", "--base-dir", str(base), "--subject", "contextmd-hook",
                           "--label", "ContextMD hooks", "--scope", "orchestration:read", "--ttl", "365d", "--json"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise InstallError(f"{t3} auth session issue failed: {done.stderr.strip()[:300]}")
    issued = json.loads(done.stdout)
    _write(p["credential"], json.dumps({k: issued[k] for k in ("sessionId", "token", "expiresAt")}) + "\n",
           mode=0o600, backup=False)
    if old and old != issued["sessionId"]:
        subprocess.run([t3, "auth", "session", "revoke", "--base-dir", str(base), old], capture_output=True)
    return p["credential"]


def _merge_hooks(path: Path, block: dict) -> None:
    """Replace ContextMD's groups in a hooks file, keeping every other hook."""
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    for event, groups in hooks.items():
        hooks[event] = [g for g in groups
                        if not any(MARK in str(h.get("command", "")) for h in g.get("hooks", []))]
    for event, groups in block["hooks"].items():
        hooks.setdefault(event, []).extend(groups)
    data["hooks"] = {e: g for e, g in hooks.items() if g}
    _write(path, json.dumps(data, indent=2) + "\n")


def codex_homes(base: Path) -> list[Path]:
    """Codex homes of the Chromeria Codex instances on this base (default ``$CODEX_HOME`` or ``~/.codex``)."""
    default = Path(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))
    settings = base / "userdata" / "settings.json"
    homes = []
    try:
        instances = json.loads(settings.read_text()).get("providerInstances") or {}
    except (OSError, ValueError):
        instances = {}
    for inst in instances.values():
        if inst.get("driver") == "codex" and inst.get("enabled", True):
            home = (inst.get("config") or {}).get("homePath") or ""
            homes.append(Path(os.path.expanduser(home)) if home else default)
    return sorted(set(homes or [default]))


def trust_codex(home: Path, cwd: Path) -> int:
    """Record the trust hash of ContextMD's hooks in ``<home>/config.toml``; returns how many."""
    proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env={**os.environ, "CODEX_HOME": str(home)}, text=True)

    def call(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if "id" not in msg:
            return None
        for line in proc.stdout:
            reply = json.loads(line)
            if reply.get("id") == msg["id"]:
                if "error" in reply:
                    raise InstallError(f"codex {msg['method']}: {reply['error']}")
                return reply.get("result") or {}
        raise InstallError("codex app-server closed")

    try:
        call({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "contextmd", "version": "1"}}})
        call({"method": "initialized"})
        listed = call({"id": 2, "method": "hooks/list", "params": {"cwds": [str(cwd)]}})
    finally:
        proc.terminate()
    ours = [h for d in listed.get("data", []) for h in d.get("hooks", [])
            if MARK in str(h.get("command", "")) and Path(h["sourcePath"]).resolve() == (home / "hooks.json").resolve()]
    config = home / "config.toml"
    lines = config.read_text().splitlines() if config.exists() else []
    for h in ours:
        table = f"[hooks.state.{json.dumps(h['key'])}]"
        value = f"trusted_hash = {json.dumps(h['currentHash'])}"
        if table in lines:
            i = lines.index(table)
            j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("trusted_hash")), None)
            if j is not None:
                lines[j] = value
            else:
                lines.insert(i + 1, value)
        else:
            lines += ["", table, value]
    _write(config, "\n".join(lines) + "\n")
    return len(ours)


def install(harness: str, base: Path, t3: str = "t3") -> list[str]:
    """Install the hook for ``harness``; returns the files written."""
    base = base.expanduser().resolve()
    p = paths(base)
    if not (base / "userdata").is_dir():
        raise InstallError(f"{base} is not a Chromeria base directory (no userdata)")
    written = [str(issue_credential(base, t3))]
    home = Path.home()
    if harness == "claude":
        settings = Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")) / "settings.json"
        _merge_hooks(settings, hooks_block(harness, base))
        written.append(str(settings))
    elif harness == "codex":
        for codex_home in codex_homes(base):
            _merge_hooks(codex_home / "hooks.json", hooks_block(harness, base))
            trust_codex(codex_home, home)
            written += [str(codex_home / "hooks.json"), str(codex_home / "config.toml")]
    elif harness == "grok":
        target = home / ".grok" / "hooks" / "contextmd.json"
        _write(target, json.dumps(hooks_block(harness, base), indent=2) + "\n")
        written.append(str(target))
    elif harness == "opencode":
        target = home / ".config" / "opencode" / "plugins" / "contextmd.js"
        _write(target, plugin_source(base))
        written.append(str(target))
    else:
        raise InstallError(f"unknown harness {harness!r}")
    return written
