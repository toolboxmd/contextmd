"""Install the thin serving hook for one harness, pointed at one Chromeria base directory.

``contextmd-serve install <harness> --base ~/.t3`` writes the harness's user-level
hook configuration so every hook event goes to the serving module of the
Chromeria server that owns ``<base>``. The module matches events against the
stores named in ContextMD's serving config itself; nothing else is registered.

The hook authenticates as an ordinary Chromeria session: install runs
Chromeria's own ``t3 auth session issue`` (subject ``contextmd-hook``, scope
``orchestration:read``, 365 days) and stores the result, token included, in
``<base>/userdata/contextmd/hook-credential`` (mode 0600). Installing again
reuses it until 30 days before it expires (``--renew`` replaces it) and revokes
a session it replaces; ``t3 auth session revoke`` ends it. The hook finds the server through Chromeria's own
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

import copy
import json
import os
import shlex
import shutil
import sys
import stat
import subprocess
import time
import tomllib
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "bin" / "contextmd-hook"


def _ours(command) -> bool:
    """A hook command this installer wrote: ``/bin/sh <...>/contextmd-hook <harness> <base>``."""
    try:
        parts = shlex.split(str(command))
    except ValueError:
        return False
    return len(parts) == 4 and parts[0] == "/bin/sh" and os.path.basename(parts[1]) == "contextmd-hook"


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
    """Replace a file atomically. A symlink is followed, so its target is updated and backed
    up; an existing file keeps its permissions unless ``mode`` says otherwise."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    if backup and target.exists():
        shutil.copy2(target, target.with_name(target.name + ".contextmd-backup"))
    tmp = target.with_name(target.name + ".contextmd-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, target)


def _t3(t3: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([t3, "auth", "session", *args], capture_output=True, text=True)


def issue_credential(base: Path, t3: str, renew: bool = False) -> Path:
    """The hooks' Chromeria session, stored mode 0600 in ``hook-credential``.

    An existing credential is kept unless ``renew`` or it expires within 30 days. A
    replaced session is revoked; a session that could not be revoked stays listed
    under ``revokePending`` and is retried on the next install, which then fails
    until it succeeds, so an old year-long session never silently stays active.
    """
    p = paths(base)
    try:
        current = json.loads(p["credential"].read_text())
    except (OSError, ValueError):
        current = {}
    pending = list(current.pop("revokePending", None) or [])
    expires = current.get("expiresAt") or ""
    soon = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 30 * 86400))
    if renew or not current.get("token") or expires < soon:
        done = _t3(t3, "issue", "--base-dir", str(base), "--subject", "contextmd-hook", "--label",
                   "ContextMD hooks", "--scope", "orchestration:read", "--ttl", "365d", "--json")
        if done.returncode != 0:
            raise InstallError(f"{t3} auth session issue failed: {done.stderr.strip()[:300]}")
        issued = json.loads(done.stdout)
        if current.get("sessionId") and current["sessionId"] != issued["sessionId"]:
            pending.append(current["sessionId"])
        replacement = {k: issued[k] for k in ("sessionId", "token", "expiresAt")}
        # Save the new session and the old one's pending revocation before revoking anything,
        # so a failed write never leaves the hooks with a revoked credential.
        try:
            _write(p["credential"], json.dumps({**replacement, "revokePending": pending}) + "\n",
                   mode=0o600, backup=False)
        except OSError as exc:
            cleanup = _t3(t3, "revoke", "--base-dir", str(base), issued["sessionId"])
            if cleanup.returncode != 0:
                command = shlex.join([t3, "auth", "session", "revoke", "--base-dir", str(base), issued["sessionId"]])
                raise InstallError(f"could not save the new hook credential ({exc}), and revoking the new session "
                                   f"{issued['sessionId']} failed; revoke it by hand with `{command}`. "
                                   f"The current credential stays in use")
            raise InstallError(f"could not save the new hook credential ({exc}); the current one stays in use")
        current = replacement
    failed = [sid for sid in pending
              if _t3(t3, "revoke", "--base-dir", str(base), sid).returncode != 0]
    _write(p["credential"], json.dumps({**current, "revokePending": failed} if failed else current) + "\n",
           mode=0o600, backup=False)
    if failed:
        raise InstallError(f"could not revoke the replaced hook session(s) {', '.join(failed)}; "
                           f"they stay listed in {p['credential']} and the next install retries them")
    return p["credential"]


def _foreign_positions(hooks: dict) -> dict:
    """Every hook this installer did not write, by its ``(event, group, index)`` position."""
    return {(event, gi, hi): json.dumps(h, sort_keys=True)
            for event, groups in hooks.items() for gi, g in enumerate(groups)
            for hi, h in enumerate(g.get("hooks", [])) if not _ours(h.get("command", ""))}


def _merge_hooks(path: Path, block: dict) -> None:
    """Put ContextMD's hook into a hooks file without moving any other hook.

    Codex keys a hook's trust by its position (``event:group:hook``), so moving another
    hook would invalidate the user's trust in it. Every ContextMD entry is replaced where it
    stands (an earlier duplicate stays a duplicate rather than shifting later hooks), a
    missing one is appended after everything else, and nothing is removed. The result is
    checked: if any other hook moved or changed, the file is left as it was.
    """
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    before = _foreign_positions(hooks)
    for event, new_groups in block["hooks"].items():
        ours = new_groups[0]["hooks"][0]
        placed = False
        for g in hooks.get(event, []):
            entries = g.get("hooks", [])
            for i, h in enumerate(entries):
                if _ours(h.get("command", "")):
                    entries[i] = ours
                    placed = True
        if not placed:
            hooks.setdefault(event, []).extend(new_groups)
    if _foreign_positions(hooks) != before:
        raise InstallError(f"installing would move other hooks in {path}; left unchanged")
    _write(path, json.dumps(data, indent=2) + "\n")


def codex_homes(base: Path) -> list[tuple[Path, Path]]:
    """``(shared, effective)`` Codex homes of the enabled Chromeria Codex instances on this base.

    Mirrors Chromeria's resolution (CodexHomeLayout, CodexManagedHome): the shared home is
    ``homePath`` or ``~/.codex``; the effective home is the shadow home, set explicitly or, for
    any instance other than ``codex``, ``<base>/userdata/providers/codex/<id>/shadow``. The
    primary instance falls back to the legacy ``providers.codex`` settings.
    """
    try:
        settings = json.loads((base / "userdata" / "settings.json").read_text())
    except (OSError, ValueError):
        settings = {}
    instances = dict(settings.get("providerInstances") or {})
    if "codex" not in instances:
        instances["codex"] = {"driver": "codex", "config": (settings.get("providers") or {}).get("codex") or {}}
    homes = set()
    for instance_id, inst in instances.items():
        config = inst.get("config") or {}
        # Chromeria treats an explicit false on the instance or in its config as disabled.
        if inst.get("driver") != "codex" or inst.get("enabled") is False or config.get("enabled") is False:
            continue
        home = str(config.get("homePath") or "").strip()
        shared = Path(os.path.expanduser(home)).resolve() if home else Path.home() / ".codex"
        shadow = str(config.get("shadowHomePath") or "").strip()
        if not shadow and instance_id != "codex":
            shadow = str(base / "userdata" / "providers" / "codex" / instance_id / "shadow")
        effective = Path(os.path.expanduser(shadow)).resolve() if shadow else shared
        homes.add((shared, effective))
    return sorted(homes)


def trust_codex(shared: Path, effective: Path, cwd: Path) -> int:
    """Record the trust hash of ContextMD's hooks as Codex sees them from the effective home.

    The hooks live in ``<shared>/hooks.json``; a shadow home reaches them by its own path, so
    Codex keys and trusts them there, in ``<effective>/config.toml``. Returns how many.
    """
    home = effective
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
            if _ours(h.get("command", "")) and Path(h["sourcePath"]).resolve() == (shared / "hooks.json").resolve()]
    config = home / "config.toml"
    original = config.read_text() if config.exists() else ""
    text = _set_trust(original, {h["key"]: h["currentHash"] for h in ours})
    _write(config, text)
    return len(ours)


def _table_key(line: str) -> str | None:
    """The hook key of a ``[hooks.state."<key>"]`` header line (comments allowed), else None."""
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    try:
        parsed = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError:
        return None
    state = parsed.get("hooks", {}).get("state", {}) if isinstance(parsed.get("hooks"), dict) else {}
    if len(parsed) == 1 and isinstance(state, dict) and len(state) == 1:
        return next(iter(state))
    return None


def _set_trust(original: str, hashes: dict[str, str]) -> str:
    """Set ``trusted_hash`` in each hook's own table, keeping every other line, then prove the result
    parses and that only those hashes changed."""
    before = tomllib.loads(original)
    lines = original.splitlines()
    for key, value in hashes.items():
        line = f"trusted_hash = {json.dumps(value)}"
        header = next((i for i, l in enumerate(lines) if _table_key(l) == key), None)
        if header is None:
            lines += ["", f"[hooks.state.{json.dumps(key)}]", line]
            continue
        end = next((i for i in range(header + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        found = next((i for i in range(header + 1, end)
                      if lines[i].split("=", 1)[0].strip() == "trusted_hash"), None)
        if found is None:
            lines.insert(header + 1, line)
        else:
            lines[found] = line
    text = "\n".join(lines) + "\n"
    after = tomllib.loads(text)
    expected = copy.deepcopy(before)
    states = expected.setdefault("hooks", {}).setdefault("state", {})
    for key, value in hashes.items():
        states.setdefault(key, {})["trusted_hash"] = value
    if after != expected:
        raise InstallError("updating Codex trust hashes would change other settings; config.toml left as it was")
    return text


def install(harness: str, base: Path, t3: str = "t3", renew: bool = False) -> list[str]:
    """Install the hook for ``harness``; returns the files written."""
    base = base.expanduser().resolve()
    p = paths(base)
    if not (base / "userdata").is_dir():
        raise InstallError(f"{base} is not a Chromeria base directory (no userdata)")
    written = [str(issue_credential(base, t3, renew))]
    home = Path.home()
    if harness == "claude":
        settings = Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")) / "settings.json"
        _merge_hooks(settings, hooks_block(harness, base))
        written.append(str(settings))
    elif harness == "codex":
        for shared, effective in codex_homes(base):
            _merge_hooks(shared / "hooks.json", hooks_block(harness, base))
            written.append(str(shared / "hooks.json"))
            if not effective.is_dir():
                # A shadow home appears when its instance first starts; install again after that.
                print(f"contextmd-serve install: {effective} does not exist yet; trust its hooks by "
                      f"installing again after that Codex instance has started", file=sys.stderr)
                continue
            trust_codex(shared, effective, home)
            written.append(str(effective / "config.toml"))
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
