"""A capture run: prepare segments, hand out model jobs, commit judged items.

A run is driven by hand from a Chromeria thread:

1. ``prepare`` syncs Agent Observer, inventories the window's threads,
   skips worker-batch and capture's own threads, renders each remaining
   thread's events since its cursor and cuts them into segments.
2. ``next`` lists the jobs that are due (two extractors per segment, then one
   judge once both lists exist). The thread dispatches each through T3
   ``delegate_task`` with the given target and ``clientRequestId``, then
   saves the task id with ``record``. A job with a saved task id is never
   handed out again unless it is recorded as failed, so a stopped run
   resumes without re-dispatching finished or running work.
3. ``commit`` syncs Observer again, merges each judged segment into queue
   items, and moves a thread's cursor only past segments whose items are all
   in the queue.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import chromeria, items as items_mod, kinds as kinds_mod, observer, transcript
from .store import Store, read_json, write_json

PROMPTS = Path(__file__).resolve().parents[2] / "prompts" / "capture"


def _contract(name: str) -> str:
    """A prompt's contract id from its first line, ``<!-- contract: x -->``."""
    first = (PROMPTS / name).read_text().split("\n", 1)[0]
    return first.split("contract:", 1)[1].split("-->")[0].strip() if "contract:" in first else name


class RunError(RuntimeError):
    pass


class AnchorMissing(RunError):
    """A lesson's moment has no Observer event to anchor to; its segment stays open."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _secs(a, b):
    try:
        return (datetime.fromisoformat(b.replace("Z", "+00:00")) -
                datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds()
    except (AttributeError, ValueError):
        return None


def _fill(template: str, **values) -> str:
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


class Capture:
    def __init__(self, cfg: dict, *, chromeria_con=None, observer_con=None, sync=None):
        self.cfg = cfg
        self.store = Store(cfg["state_dir"])
        self._ch = chromeria_con
        self._ao = observer_con
        self._sync = sync or (lambda: observer.sync(cfg["observer_bin"]))

    # Connections are opened after the sync so they see the fresh ledger.
    @property
    def ch(self):
        if self._ch is None:
            self._ch = chromeria.connect(self.cfg["chromeria_db"])
        return self._ch

    @property
    def ao(self):
        if self._ao is None:
            self._ao = observer.connect(self.cfg["observer_db"])
        return self._ao

    def _run_dir(self, run_id: str) -> Path:
        return self.store.runs / run_id

    def load(self, run_id: str | None = None) -> dict:
        run_id = run_id or self.store.active_run()
        if not run_id:
            raise RunError("no active capture run; start one with prepare")
        run = read_json(self._run_dir(run_id) / "run.json")
        if run is None:
            raise RunError(f"run {run_id} has no run.json")
        return run

    def save(self, run: dict) -> None:
        write_json(self._run_dir(run["run_id"]) / "run.json", run)

    # ------------------------------------------------------------------ prepare
    def _new_run_id(self, experiment: str | None) -> str:
        stem = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + (f"-{experiment}" if experiment else "")
        run_id, n = stem, 1
        while self._run_dir(run_id).exists():  # two runs in one second must not share a directory
            n += 1
            run_id = f"{stem}-{n}"
        return run_id

    def prepare(self, since: str, until: str, *, thread_ids=None, whole=False,
                experiment: str | None = None) -> dict:
        with self.store.locked():
            active = self.store.active_run()
            if active:
                raise RunError(f"run {active} is still open; finish it with next/record/commit")
            self._sync()
            run_id = self._new_run_id(experiment)
            run = self._plan(run_id, since, until, thread_ids, whole, experiment)
            self._set_prompts(run, "extract.md")
            self.save(run)
            self.store.set_active_run(run_id)
            return run

    def replay(self, from_run: str, segment_ids: list[str], experiment: str,
               extract_prompt: str = "extract.md") -> dict:
        """An experiment run over exact segment transcripts of an earlier run.

        Prompt comparisons need the same input, so the transcripts and line maps
        are copied, not re-rendered. Cursors and the queue are not touched.
        """
        with self.store.locked():
            active = self.store.active_run()
            if active:
                raise RunError(f"run {active} is still open; finish it with next/record/commit")
            self._sync()
            src = self.load(from_run)
            run_id = self._new_run_id(experiment)
            segments, threads = [], {}
            for sid in segment_ids:
                seg = next((s for s in src["segments"] if s["id"] == sid), None)
                if seg is None:
                    raise RunError(f"run {from_run} has no segment {sid}")
                to = self._run_dir(run_id) / "seg" / sid
                to.mkdir(parents=True, exist_ok=True)
                for name in ("transcript.txt", "lines.json"):
                    shutil.copyfile(self._run_dir(from_run) / "seg" / sid / name, to / name)
                segments.append({**{k: seg[k] for k in ("id", "thread", "chars", "last")},
                                 "committed": False, "source": {"run": from_run, "segment": sid}})
                threads.setdefault(seg["thread"], {**src["threads"][seg["thread"]], "segments": []})
                threads[seg["thread"]]["segments"].append(sid)
            run = {"run_id": run_id, "machine": self.cfg["machine"], "since": src["since"],
                   "until": src["until"], "whole": False, "experiment": experiment, "created_at": _now(),
                   "replay_of": from_run, "targets": {"extractors": self.cfg["extractors"],
                                                      "judge": self.cfg["judge"]},
                   "threads": threads, "segments": segments, "jobs": self._jobs(segments), "state": "open"}
            self._set_prompts(run, extract_prompt)
            self.save(run)
            self.store.set_active_run(run_id)
            return run

    def _set_prompts(self, run: dict, extract_prompt: str) -> None:
        if not (PROMPTS / extract_prompt).is_file():
            raise RunError(f"no prompt {extract_prompt} in {PROMPTS}")
        run["prompts"] = {"extract": extract_prompt, "judge": "judge.md"}
        run["contracts"] = {"extract": _contract(extract_prompt), "judge": _contract("judge.md")}

    def _jobs(self, segments: list[dict]) -> dict:
        jobs = {}
        for seg in segments:
            for name in self.cfg["extractors"]:
                jobs[f"{seg['id']}-extract-{name}"] = {"step": "extract", "segment": seg["id"],
                                                        "extractor": name, "attempts": []}
            jobs[f"{seg['id']}-judge"] = {"step": "judge", "segment": seg["id"], "attempts": []}
        return jobs

    def _plan(self, run_id, since, until, thread_ids, whole, experiment) -> dict:
        cfg = self.cfg
        active = chromeria.active_thread_ids(self.ch, since, until)
        if thread_ids:
            active = [t for t in active if t in set(thread_ids)]
        everything = chromeria.threads(self.ch)
        kinds = kinds_mod.classify(everything, cfg["own_task_prefix"], cfg["batch_min_siblings"])
        cursors = {} if experiment else self.store.cursors()
        rdir = self._run_dir(run_id)
        threads, segments = {}, []
        max_chars = 0 if whole else cfg["segment_chars"]
        for tid in active:
            t = everything.get(tid)
            kind = kinds.get(tid, "worker")
            entry = {"kind": kind, "title": t.title if t else "", "status": None, "segments": []}
            threads[tid] = entry
            if kind in kinds_mod.SKIPPED:
                entry["status"] = f"skipped:{kind}"
                continue
            session = observer.session_of(self.ao, tid)
            if not session:
                entry["status"] = "skipped:no-observer-session"
                continue
            entry["session_key"] = session
            entry["project_dir"] = observer.project_of(self.ao, session)
            entry["provider"], entry["model"] = (t.provider, t.model) if t else (None, None)
            all_items = chromeria.items(self.ch, tid)
            cur = cursors.get(tid)
            def after_start(it):
                return it.ordinal > cur["ordinal"] if cur else it.ts >= since
            fresh = [it for it in all_items if after_start(it)]
            # Stop at the first item past the window, so an earlier-timed item after it
            # cannot move the cursor over it (timestamps do not always rise with ordinals).
            late = next((i for i, it in enumerate(fresh) if it.ts >= until), None)
            if late is not None:
                fresh = fresh[:late]
            old = [it for it in all_items if not after_start(it) and it.ts < until]
            if fresh and not cur and not experiment:
                # Record where capture starts, so a first capture that never commits
                # is retried from here instead of from the next window's start.
                start = min(it.ordinal for it in fresh) - 1  # ordinals, not times, order a thread
                self.store.set_cursor(tid, {"ordinal": start, "item_id": None, "ts": since,
                                            "run": run_id, "baseline": True})
            # While the thread is still running, stop before its first unfinished item;
            # the next run starts there. A finished thread's leftover items are final.
            if chromeria.run_state(self.ch, tid) in ("running", "queued"):
                busy = next((i for i, it in enumerate(fresh) if it.status in chromeria.UNFINISHED), None)
                if busy is not None:
                    fresh = fresh[:busy]
                    entry["held_back"] = True
            body_lines = transcript.render(fresh)
            if not body_lines:
                entry["status"] = "no-new-events"
                continue
            context = transcript.render(old)
            entry["status"] = "captured"
            entry["events"] = len(fresh)
            cuts = transcript.segments(body_lines, max_chars)
            for i, seg_lines in enumerate(cuts):
                seg_id = f"s{len(segments):03d}"
                ctx = transcript.context_before(context, cfg["context_chars"])
                ahead = transcript.head(cuts[i + 1], cfg["overlap_chars"]) if i + 1 < len(cuts) else []
                header = (f"# session {tid} | agent {entry['provider']}/{entry['model']} | kind {kind}"
                          f" | machine {cfg['machine']} | title {entry['title']} | segment {seg_id}")
                text, numbered, lookahead = transcript.document(header, ctx, seg_lines, ahead)
                sdir = rdir / "seg" / seg_id
                sdir.mkdir(parents=True, exist_ok=True)
                (sdir / "transcript.txt").write_text(text, encoding="utf-8")
                write_json(sdir / "lines.json", {str(n): {
                    "item_id": l.item_id, "ordinal": l.ordinal, "ts": l.ts, "kind": l.kind,
                    "text": l.text, "owned": n not in lookahead} for n, l in numbered.items()})
                last = seg_lines[-1]
                segments.append({"id": seg_id, "thread": tid, "chars": transcript.size(seg_lines),
                                 "last": {"ordinal": last.ordinal, "item_id": last.item_id, "ts": last.ts},
                                 "committed": False})
                entry["segments"].append(seg_id)
                context = context + seg_lines
        return {"run_id": run_id, "machine": cfg["machine"], "since": since, "until": until,
                "whole": whole, "experiment": experiment, "created_at": _now(), "jobs": self._jobs(segments),
                "targets": {"extractors": cfg["extractors"], "judge": cfg["judge"]},
                "threads": threads, "segments": segments, "state": "open"}

    # --------------------------------------------------------------------- jobs
    @staticmethod
    def _live(job: dict) -> dict | None:
        """The job's current attempt: the last one, unless it was recorded failed."""
        last = job["attempts"][-1] if job["attempts"] else None
        return last if last and last.get("status") != "failed" else None

    def _paths(self, run: dict, job: dict) -> dict:
        """Paths of the job's current attempt (the live one, else the next one).

        Each attempt writes its own file, so a superseded attempt that is
        still running can never overwrite the output of the one that replaced it.
        """
        sdir = self._run_dir(run["run_id"]) / "seg" / job["segment"]
        n = len(job["attempts"]) if self._live(job) else len(job["attempts"]) + 1
        base, ext = (f"extract-{job['extractor']}", ".md") if job["step"] == "extract" else ("judge", ".jsonl")
        name = f"{base}{ext}" if n <= 1 else f"{base}.a{n}{ext}"
        return {"dir": sdir, "transcript": sdir / "transcript.txt", "output": sdir / name}

    def _extract_job(self, run: dict, seg_id: str, name: str) -> dict:
        return run["jobs"][f"{seg_id}-extract-{name}"]

    def _extract_items(self, run: dict, seg_id: str, name: str):
        p = self._paths(run, self._extract_job(run, seg_id, name))["output"]
        if not p.exists():
            return None
        return items_mod.parse_extract(p.read_text(encoding="utf-8"))

    def _output_state(self, run: dict, job: dict) -> str:
        """done, missing, running, run-failed or invalid for the job's current attempt.

        Only a dispatched attempt's output counts, and only once its Chromeria
        run has completed: a model may write its file in several steps.
        """
        live = self._live(job)
        out = self._paths(run, job)["output"]
        if live is None or not out.exists():
            return "missing"
        state = chromeria.run_state(self.ch, live["child_thread"])
        if state in (None, "running", "queued"):
            return "running"
        if state != "completed":
            return "run-failed"
        if job["step"] == "judge" and any(
                self._output_state(run, self._extract_job(run, job["segment"], n)) != "done"
                for n in run["targets"]["extractors"]):
            return "missing"
        try:
            if job["step"] == "extract":
                self._extract_items(run, job["segment"], job["extractor"])
            else:
                self._judged(run, job["segment"])
        except items_mod.InvalidOutput:
            return "invalid"
        return "done"

    def _extract_hashes(self, run: dict, seg_id: str) -> dict:
        out = {}
        for n in run["targets"]["extractors"]:
            p = self._paths(run, self._extract_job(run, seg_id, n))["output"]
            out[n] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        return out

    def _judge_inputs(self, run: dict, seg_id: str) -> dict:
        sdir = self._run_dir(run["run_id"]) / "seg" / seg_id
        extracts = {n: self._extract_items(run, seg_id, n) for n in run["targets"]["extractors"]}
        sides, ids = items_mod.blind_lists(extracts, f"{run['run_id']}-{seg_id}")
        texts = items_mod.judge_input(extracts, sides)
        for side, text in texts.items():
            (sdir / f"judge-{side}.jsonl").write_text(text, encoding="utf-8")
        write_json(sdir / "judge-sides.json", {**sides, "hashes": self._extract_hashes(run, seg_id)})
        return {"extracts": extracts, "sides": sides, "ids": ids}

    def _inputs_stale(self, run: dict, seg_id: str) -> bool:
        """True when the judge's lists were built from other extractor outputs than the current ones."""
        sides = read_json(self._run_dir(run["run_id"]) / "seg" / seg_id / "judge-sides.json")
        if sides is None:
            return False
        return sides.get("hashes") != self._extract_hashes(run, seg_id)

    def _judged(self, run: dict, seg_id: str):
        extracts = {n: self._extract_items(run, seg_id, n) for n in run["targets"]["extractors"]}
        if any(v is None for v in extracts.values()):
            raise items_mod.InvalidOutput("an extractor list is missing")
        _, ids = items_mod.blind_lists(extracts, f"{run['run_id']}-{seg_id}")
        if not any(extracts.values()):
            return extracts, ids, []
        if self._inputs_stale(run, seg_id):
            raise items_mod.InvalidOutput("judge inputs are older than the extractor outputs")
        out = self._paths(run, run["jobs"][f"{seg_id}-judge"])["output"]
        groups = items_mod.parse_judge(out.read_text(encoding="utf-8"), ids)
        return extracts, ids, groups

    def _task_text(self, run: dict, job_id: str, job: dict) -> str:
        p = self._paths(run, job)
        if job["step"] == "extract":
            name = run.get("prompts", {}).get("extract", "extract.md")
            return _fill((PROMPTS / name).read_text(), transcript=p["transcript"],
                         output=p["output"])
        sdir = p["dir"]
        return _fill((PROMPTS / "judge.md").read_text(), transcript=p["transcript"],
                     items_a=sdir / "judge-A.jsonl", items_b=sdir / "judge-B.jsonl",
                     output=p["output"])

    def _brief(self, run: dict, job_id: str, job: dict) -> str:
        """Write the job's full prompt beside its segment; the task text points at it."""
        path = self._paths(run, job)["output"].with_suffix(".brief.md")
        path.write_text(self._task_text(run, job_id, job), encoding="utf-8")
        return (f"Read the brief at {path} and do exactly what it says. "
                "Read only the files it names and write only its output file.")

    def next_jobs(self) -> dict:
        """Jobs to dispatch now (due), dispatched jobs still working (waiting), and
        dispatched jobs whose output or run failed (invalid: record them failed to retry)."""
        with self.store.locked():
            run = self.load()
            due, waiting, invalid = [], [], []
            for job_id, job in run["jobs"].items():
                seg = self._segment(run, job["segment"])
                if seg["committed"]:
                    continue
                state = self._output_state(run, job)
                if state == "done":
                    continue
                live = self._live(job)
                if job["step"] == "judge":
                    if any(self._output_state(run, self._extract_job(run, job["segment"], n)) != "done"
                           for n in run["targets"]["extractors"]):
                        continue
                    extracts = {n: self._extract_items(run, job["segment"], n)
                                for n in run["targets"]["extractors"]}
                    if not any(extracts.values()):
                        continue  # nothing to judge; commit records zero items
                    if live is None and (self._inputs_stale(run, job["segment"]) or not (
                            self._paths(run, job)["dir"] / "judge-sides.json").exists()):
                        self._judge_inputs(run, job["segment"])
                if live is not None:
                    entry = {"job": job_id, "task_id": live["task_id"], "output": state}
                    (invalid if state in ("invalid", "run-failed") else waiting).append(entry)
                    continue
                attempt = len(job["attempts"]) + 1
                target = (run["targets"]["extractors"][job["extractor"]] if job["step"] == "extract"
                          else run["targets"]["judge"])
                due.append({
                    "job": job_id, "step": job["step"], "target": target,
                    "clientRequestId": f"{self.cfg['own_task_prefix']}{run['run_id']}-{job_id}-{attempt}",
                    "title": f"contextmd capture {job_id}",
                    "output": str(self._paths(run, job)["output"]),
                    "task": self._brief(run, job_id, job)})
            return {"run_id": run["run_id"], "due": due, "waiting": waiting, "invalid": invalid}

    def _segment(self, run: dict, seg_id: str) -> dict:
        return next(s for s in run["segments"] if s["id"] == seg_id)

    def record(self, job_id: str, *, task_id: str | None = None, child_thread: str | None = None,
               failed: bool = False, note: str | None = None) -> dict:
        with self.store.locked():
            run = self.load()
            job = run["jobs"].get(job_id)
            if job is None:
                raise RunError(f"no job {job_id} in run {run['run_id']}")
            if failed:
                if self._live(job) is None:
                    raise RunError(f"job {job_id} has no live attempt to mark failed")
                job["attempts"][-1]["status"] = "failed"
                job["attempts"][-1]["note"] = note
            else:
                if not task_id or not child_thread:
                    raise RunError("record needs --task-id and --child-thread, or --failed")
                live = self._live(job)
                if live and live["task_id"] == task_id:
                    live["child_thread"] = child_thread
                elif live:
                    raise RunError(f"job {job_id} already has live task {live['task_id']}")
                else:
                    job["attempts"].append({"task_id": task_id, "child_thread": child_thread,
                                            "recorded_at": _now(), "status": "dispatched"})
            self.save(run)
            return job

    # ------------------------------------------------------------------- commit
    def commit(self) -> dict:
        with self.store.locked():
            run = self.load()
            self._sync()
            committed = 0
            for seg in run["segments"]:
                if seg["committed"]:
                    continue
                jobs = [self._extract_job(run, seg["id"], n) for n in run["targets"]["extractors"]]
                if any(self._output_state(run, j) != "done" for j in jobs):
                    continue
                extracts = {n: self._extract_items(run, seg["id"], n) for n in run["targets"]["extractors"]}
                if any(extracts.values()):
                    if self._output_state(run, run["jobs"][f"{seg['id']}-judge"]) != "done":
                        continue
                    extracts, ids, groups = self._judged(run, seg["id"])
                    lessons = items_mod.merge(groups, ids, extracts)
                else:
                    lessons = []
                try:
                    queue_items = self._queue_items(run, seg, lessons)
                except AnchorMissing as exc:
                    seg["error"] = str(exc)
                    self.save(run)
                    continue
                seg.pop("error", None)
                taken: set = set()
                for it in queue_items:
                    self._free_key(it, taken, self._run_dir(run["run_id"]) / "items"
                                   if run["experiment"] else self.store.queue)
                    if run["experiment"]:
                        write_json(self._run_dir(run["run_id"]) / "items" /
                                   f"{Store.key_name(it['key'])}.json", it)
                    else:
                        self.store.put(it)
                seg["committed"] = True
                seg["items"] = len(queue_items)
                seg["committed_at"] = _now()
                committed += 1
                self.save(run)
            if not run["experiment"]:
                self._advance_cursors(run)
            if all(s["committed"] for s in run["segments"]):
                run["state"] = "complete"
                run["completed_at"] = _now()
                self.store.set_active_run(None)
            self.save(run)
            return {"run_id": run["run_id"], "committed_segments": committed, "state": run["state"],
                    "open_segments": sum(1 for s in run["segments"] if not s["committed"]),
                    "errors": {s["id"]: s["error"] for s in run["segments"] if s.get("error")}}

    def _free_key(self, item: dict, taken: set, where: Path) -> None:
        """Bump the item's index past keys another run or segment holds, or this commit used.

        A segment captured again after ``abandon``, or a lesson cited in the overlap
        with the next segment, must not replace another segment's lessons; a repeated
        commit of the same segment still rewrites its own items.
        """
        while True:
            name = Store.key_name(item["key"])
            held = read_json(where / f"{name}.json")
            if name not in taken and (held is None or held["run"] == item["run"]):
                taken.add(name)
                return
            item["key"]["index"] += 1

    def abandon(self, reason: str) -> dict:
        """Close the open run; the next run captures its uncommitted segments' events again."""
        with self.store.locked():
            run = self.load()
            run["state"] = "abandoned"
            run["abandoned"] = {"at": _now(), "reason": reason,
                                "open_segments": [s["id"] for s in run["segments"] if not s["committed"]]}
            self.save(run)
            self.store.set_active_run(None)
            return {"run_id": run["run_id"], **run["abandoned"]}

    def _advance_cursors(self, run: dict) -> None:
        """Move each thread's cursor past its leading run of committed segments."""
        by_thread = defaultdict(list)
        for seg in run["segments"]:
            by_thread[seg["thread"]].append(seg)
        for tid, segs in by_thread.items():
            last = None
            for seg in segs:
                if not seg["committed"]:
                    break
                last = seg["last"]
            if last:
                self.store.set_cursor(tid, {**last, "run": run["run_id"]})

    def _model_evidence(self, run: dict, job: dict) -> dict:
        att = next((a for a in reversed(job["attempts"]) if a.get("status") != "failed"), None)
        out = {"requested": None, "observed": None, "task_id": att and att["task_id"]}
        out["requested"] = (run["targets"]["extractors"][job["extractor"]]["model"]
                            if job["step"] == "extract" else run["targets"]["judge"]["model"])
        if att and att.get("child_thread"):
            usage = observer.job_usage(self.ao, att["child_thread"])
            if usage:
                out["observed"] = usage["models"]
                out["usage"] = usage
            start, end = chromeria.run_window(self.ch, att["child_thread"])
            out["started_at"], out["completed_at"] = start, end
            out["wall_s"] = _secs(start, end)
        job["evidence"] = out
        return out

    def _queue_items(self, run: dict, seg: dict, lessons: list[dict]) -> list[dict]:
        sdir = self._run_dir(run["run_id"]) / "seg" / seg["id"]
        lines = read_json(sdir / "lines.json", {})
        thread = run["threads"][seg["thread"]]
        models = {"extractors": {n: self._model_evidence(run, run["jobs"][f"{seg['id']}-extract-{n}"])
                                 for n in run["targets"]["extractors"]}}
        if run["jobs"][f"{seg['id']}-judge"]["attempts"]:
            models["judge"] = self._model_evidence(run, run["jobs"][f"{seg['id']}-judge"])
        per_event = Counter()
        out = []
        seg["in_overlap"] = 0
        first_line = lines[min(lines, key=int)] if lines else None
        for lesson in lessons:
            item = lesson["item"]
            cited, line = "item", None
            # The best item's citation first, then the other members', preferring lines
            # this segment owns. A lesson cited only in the overlap is kept too: the next
            # segment may not find it, and dedupe merges it when it does.
            cited_lines = [lines[str(n)] for n in map(items_mod.event_number, lesson["events"])
                           if n is not None and str(n) in lines]
            line = next((l for l in cited_lines if l.get("owned", True)), None)
            if line is None and cited_lines:
                cited, line = "overlap", cited_lines[0]
                seg["in_overlap"] += 1
            if line is None:
                cited, line = "segment-start", first_line
            ref = observer.anchor(self.ao, thread["session_key"], native_id=None,
                                  kind=line["kind"], text=None, ts=line["ts"])
            native = self._native_id(line["item_id"])
            if native or line["kind"] in ("user", "brief"):
                exact = observer.anchor(self.ao, thread["session_key"], native_id=native,
                                        kind=line["kind"], text=self._item_text(line["item_id"]),
                                        ts=line["ts"])
                ref = exact or ref
            if ref is None:
                raise AnchorMissing(f"no Observer event for {line['item_id']} in {thread['session_key']}")
            index = per_event[line["item_id"]]
            per_event[line["item_id"]] += 1
            out.append({
                "key": {"machine": run["machine"], "thread": seg["thread"], "event": line["item_id"],
                        "index": index},
                "state": "extracted",
                "moment": {"machine": run["machine"], "thread": seg["thread"], "turn_item": line["item_id"],
                           "ts": line["ts"], "line_kind": line["kind"], "cited": cited,
                           "observer": ref},
                "source_time": line["ts"],
                "scope": {"project_dir": thread.get("project_dir"), "harness": thread.get("provider"),
                          "session_model": thread.get("model"), "machine": run["machine"],
                          "stated": item.get("scope")},
                "thread_kind": thread["kind"],
                "kind": item.get("kind"), "title": item.get("title"), "what": item.get("what"),
                "why": item.get("why"), "so_what": item.get("so_what"),
                "applies_when": item.get("applies_when"),
                "found_by": lesson["found_by"], "best_from": lesson["best_from"],
                "learned_from": lesson["learned_from"],
                "extractor_learned_from": items_mod.learned(item.get("learned_from")),
                "grade": lesson["grade"], "unsupported": lesson["unsupported"],
                "judge_reason": lesson["reason"], "members": lesson["members"],
                "models": models,
                "run": {"run_id": run["run_id"], "segment": seg["id"], "contracts": run["contracts"]},
            })
        return out

    def _turn_payload(self, item_id: str) -> dict:
        row = self.ch.execute(f"SELECT payload_json FROM {chromeria.ITEMS} WHERE turn_item_id=?",
                              (item_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _native_id(self, item_id: str) -> str | None:
        ref = self._turn_payload(item_id).get("nativeItemRef") or {}
        return ref.get("nativeId") if isinstance(ref, dict) else None

    def _item_text(self, item_id: str) -> str | None:
        return self._turn_payload(item_id).get("text")
