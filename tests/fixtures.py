"""Tiny Chromeria V2 and Agent Observer databases with the columns capture reads."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def chromeria_db(path: Path, threads: list[dict], items: list[dict]) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT PRIMARY KEY, payload_json TEXT);
    CREATE TABLE orchestration_v2_projection_turn_items (turn_item_id TEXT PRIMARY KEY, thread_id TEXT,
      ordinal INTEGER, type TEXT, status TEXT, updated_at TEXT, payload_json TEXT);
    CREATE TABLE orchestration_v2_projection_runs (run_id TEXT PRIMARY KEY, thread_id TEXT,
      ordinal INTEGER, payload_json TEXT);
    """)
    for t in threads:
        payload = {"title": t.get("title", t["id"]), "createdAt": "2026-10-10T00:00:00Z",
                   "createdBy": t.get("created_by", "agent"), "creationSource": "mcp",
                   "modelSelection": {"instanceId": t.get("provider", "codex"), "model": t.get("model", "m")},
                   "lineage": {"parentThreadId": t["parent"]} if t.get("parent") else {}}
        con.execute("INSERT INTO orchestration_v2_projection_threads VALUES(?,?)",
                    (t["id"], json.dumps(payload)))
        if t.get("run"):
            start, end = t["run"]
            con.execute("INSERT INTO orchestration_v2_projection_runs VALUES(?,?,?,?)",
                        (f"run-{t['id']}", t["id"], 1, json.dumps(
                            {"startedAt": start, "completedAt": end,
                             "status": t.get("run_status", "completed" if end else "running")})))
    for i in items:
        payload = {"startedAt": i["ts"], "type": i["type"], **i.get("payload", {})}
        con.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES(?,?,?,?,?,?,?)",
                    (i["id"], i["thread"], i["ordinal"], i["type"], i.get("status", "completed"), i["ts"],
                     json.dumps(payload)))
    con.commit()
    con.row_factory = sqlite3.Row
    return con


def observer_db(path: Path, sessions: list[dict]) -> sqlite3.Connection:
    """sessions: thread, key, native, events [(id, native_id, epoch)], submissions [(native_id, text)]."""
    from contextmd.capture.observer import text_hash
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE t3_threads (thread_id TEXT PRIMARY KEY, root_thread_id TEXT, provider TEXT,
      adapter_key TEXT, native_session TEXT);
    CREATE TABLE sessions (session_key TEXT PRIMARY KEY, harness TEXT, native_id TEXT,
      project_dir TEXT, updated_at REAL);
    CREATE TABLE events (id INTEGER PRIMARY KEY, session_key TEXT, ts REAL, family TEXT, native_id TEXT);
    CREATE TABLE submissions (native_id TEXT PRIMARY KEY, session_key TEXT, text_hash TEXT);
    CREATE TABLE responses (response_id TEXT PRIMARY KEY, session_key TEXT, model TEXT,
      input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
      reasoning_output_tokens INTEGER, total_tokens INTEGER, is_overlap INTEGER DEFAULT 0);
    """)
    for s in sessions:
        con.execute("INSERT INTO t3_threads VALUES(?,?,?,?,?)",
                    (s["thread"], s["thread"], "codex", "codex", s["native"]))
        con.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                    (s["key"], "codex", s["native"], s.get("project", "/repo"), 1.0))
        for eid, native, ts in s.get("events", []):
            con.execute("INSERT INTO events VALUES(?,?,?,?,?)", (eid, s["key"], ts, "tool_call", native))
        for native, text in s.get("submissions", []):
            con.execute("INSERT INTO submissions VALUES(?,?,?)", (native, s["key"], text_hash(text)))
        for n, (model, tokens) in enumerate(s.get("responses", [])):
            con.execute("INSERT INTO responses VALUES(?,?,?,?,?,?,?,?,0)",
                        (f"{s['key']}-{n}", s["key"], model, tokens, 0, tokens // 10, 0, tokens))
    con.commit()
    con.row_factory = sqlite3.Row
    return con
