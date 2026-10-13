"""Read the serving log the Chromeria serving module writes, read-only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def records(db_path: Path, session: str | None = None):
    """One dict per moment, oldest first, with its candidates under ``serves``."""
    if not db_path.exists():
        return
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        query = "SELECT * FROM moments" + (" WHERE session = ?" if session else "") + " ORDER BY id"
        for m in db.execute(query, (session,) if session else ()).fetchall():
            rec = dict(m)
            rec["owners"] = json.loads(rec.pop("owners_json") or "{}")
            if "cue_json" in rec:  # Cue's service-side timing and window size, from a server with the Cue lane
                rec["cue"] = json.loads(rec.pop("cue_json") or "null")
            rec["serves"] = []
            for c in db.execute("SELECT * FROM candidates WHERE moment_id = ? ORDER BY id", (m["id"],)):
                serve = {k: v for k, v in dict(c).items() if v is not None and k not in ("id", "moment_id")}
                if "extra_json" in serve:
                    serve.update(json.loads(serve.pop("extra_json")))
                rec["serves"].append(serve)
            yield rec
    finally:
        db.close()
