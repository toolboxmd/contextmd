import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextmd.capture import compare, config, dedupe, items, kinds, observer, report, transcript  # noqa: E402
from contextmd.capture.chromeria import Item, Thread  # noqa: E402
from contextmd.capture.run import Capture, RunError, _contract  # noqa: E402
from contextmd.capture.store import Busy, Store  # noqa: E402
from tests.fixtures import chromeria_db, observer_db  # noqa: E402

DT = "thread:delegated-task:command%3Amcp%3Ax%3Adelegate-task%3A"


def epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def item(tid, n, ts, type_, status="completed", **payload):
    return {"id": f"{tid}-i{n}", "thread": tid, "ordinal": n, "ts": ts, "type": type_, "status": status,
            "payload": payload}


def md(*entries):
    """Extractor Markdown; each entry is (title, event number, learned_from)."""
    out = []
    for title, event, learned in entries:
        out.append(f"## rule: {title}\n\n**What:** {title} always.\n**Why:** it broke.\n"
                   f"**So what:** do it.\n**Event:** [{event}]\n**Learned from:** {learned}\n"
                   f"**Scope:** any\n**Applies when:** always\n")
    return "\n".join(out) if out else "NO ITEMS"


class KindsTest(unittest.TestCase):
    def thread(self, tid, parent=None, title="t", created_by="agent"):
        return Thread(tid, title, "", created_by, "mcp", parent, "codex", "m")

    def test_four_siblings_on_one_brief_are_worker_batch_three_are_not(self):
        ts = {"P": self.thread("P", created_by="user")}
        for i in range(4):
            ts[f"{DT}final-v3-a-{i}"] = self.thread(f"{DT}final-v3-a-{i}", "P")
        for x in "abc":
            ts[f"{DT}council-recheck-{x}-0"] = self.thread(f"{DT}council-recheck-{x}-0", "P", "Council recheck")
        ts[f"{DT}contextmd-capture-r1-s000-judge-1"] = self.thread(f"{DT}contextmd-capture-r1-s000-judge-1", "P")
        k = kinds.classify(ts, "contextmd-capture-", 4)
        self.assertEqual(k["P"], "user-started")
        self.assertEqual({k[f"{DT}final-v3-a-{i}"] for i in range(4)}, {"worker-batch"})
        self.assertEqual(k[f"{DT}council-recheck-a-0"], "research")
        self.assertEqual(k[f"{DT}contextmd-capture-r1-s000-judge-1"], "capture")


class SegmentsTest(unittest.TestCase):
    def lines(self, n, size):
        return [transcript.Line("user" if i % 3 == 0 else "assistant", "2026-10-10T00:00:00Z",
                                "x" * size, f"it{i}", i) for i in range(n)]

    def test_segments_respect_cap_and_keep_every_line_once(self):
        ls = self.lines(30, 100)
        segs = transcript.segments(ls, 1000)
        self.assertGreater(len(segs), 1)
        self.assertEqual([l for s in segs for l in s], ls)
        for s in segs:
            self.assertLessEqual(transcript.size(s), 1000)

    def test_lines_of_one_item_never_split(self):
        ls = [transcript.Line("call", "t", "x" * 400, "same", 1),
              transcript.Line("output", "t", "y" * 400, "same", 1)]
        segs = transcript.segments(ls, 500)
        self.assertEqual(len(segs), 1)

    def test_context_lines_are_numbered_but_not_extractable(self):
        ctx = self.lines(2, 10)
        body = self.lines(3, 10)
        text, numbered, ahead = transcript.document("# h", ctx, body)
        self.assertIn("## CONTEXT", text)
        self.assertEqual(sorted(numbered), [3, 4, 5])
        self.assertEqual(ahead, set())
        self.assertIn("[3] user", text)


class ParseTest(unittest.TestCase):
    def test_item_missing_a_field_is_invalid(self):
        with self.assertRaises(items.InvalidOutput):
            items.parse_extract("## rule: half\n\n**What:** something\n")

    def test_extract_fields_and_no_items(self):
        got = items.parse_extract(md(("Use rsync excludes", 4, "failure")))
        self.assertEqual(got[0]["title"], "Use rsync excludes")
        self.assertEqual(items.event_number(got[0]["event"]), 4)
        self.assertEqual(items.learned(got[0]["learned_from"]), "failure")
        self.assertEqual(items.parse_extract("NO ITEMS"), [])
        with self.assertRaises(items.InvalidOutput):
            items.parse_extract("I could not read the file.")

    def test_defect_grade_and_outcomes_are_carried(self):
        got = items.parse_extract(md(("broken setting", 3, "failure")) + "**Outcome:** still open at the end\n")
        self.assertEqual(got[0]["outcome"], "still open at the end")
        ids = {"A01": ("sol", 0), "B01": ("grok", 0)}
        g = items.parse_judge('{"a":["A01"],"b":["B01"],"grade":"defect","learned_from":"failure",'
                              '"outcome":"not fixed","owner":"toolboxmd/x"}\n', ids)
        m = items.merge(g, ids, {"sol": got, "grok": [{"t": 1}]})
        self.assertEqual((m[0]["grade"], m[0]["outcome"], m[0]["owner"]), ("defect", "not fixed", "toolboxmd/x"))

    def test_judge_must_cover_every_id_exactly_once(self):
        ids = {"A01": ("sol", 0), "B01": ("grok", 0), "B02": ("grok", 1)}
        ok = ('{"a":["A01"],"b":["B01"],"best":"B01","grade":"keep","unsupported":false,"learned_from":"user"}\n'
              '{"a":[],"b":["B02"],"best":"B02","grade":"junk","unsupported":true,"learned_from":"discovered"}\n')
        groups = items.parse_judge(ok, ids)
        merged = items.merge(groups, ids, {"sol": [{"t": 1}], "grok": [{"t": 2}, {"t": 3}]})
        self.assertEqual(merged[0]["found_by"], ["grok", "sol"])
        self.assertEqual(merged[0]["item"], {"t": 2})
        self.assertEqual(merged[1]["found_by"], ["grok"])
        with self.assertRaises(items.InvalidOutput):
            items.parse_judge(ok.splitlines()[0], ids)  # B02 left out
        with self.assertRaises(items.InvalidOutput):
            items.parse_judge(ok + '{"a":["A01"],"b":[],"grade":"keep","learned_from":"user"}\n', ids)


def moved(cursors):
    """Cursors that moved past captured events (not the start markers prepare writes)."""
    return {t: c for t, c in cursors.items() if not c.get("baseline")}


class RunTest(unittest.TestCase):
    """A run over fake Chromeria and Observer records, driven like the thread drives it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        U, W, B, R = "U", f"{DT}fix-db-1", f"{DT}big-job-1", f"{DT}still-going-1"
        self.U, self.W, self.B, self.R = U, W, B, R
        threads = [{"id": "P", "created_by": "user"}, {"id": U, "created_by": "user", "title": "Main"},
                   {"id": W, "parent": "P", "title": "Fix the db"}, {"id": B, "parent": "P", "title": "Big job"},
                   {"id": R, "parent": "P", "title": "Still going", "run": ("2026-10-10T13:00:00Z", None)},
                   {"id": f"{DT}stale-item-1", "parent": "P", "title": "Done long ago",
                    "run": ("2026-10-10T14:00:00Z", "2026-10-10T15:00:00Z")}]
        threads += [{"id": f"{DT}final-v3-a-{i}", "parent": "P"} for i in range(4)]
        threads += [{"id": f"{DT}contextmd-capture-old-s000-judge-1", "parent": "P"}]
        threads += [{"id": f"{DT}contextmd-capture-child-{n}", "parent": "P",
                     "run": ("2026-10-12T10:00:00Z", "2026-10-12T10:02:00Z")} for n in range(3)]
        threads += [{"id": f"{DT}contextmd-capture-busy", "parent": "P", "run": ("2026-10-12T10:00:00Z", None)}]
        its = [
            item(U, 1, "2026-10-09T23:00:00Z", "user_message", text="old question", createdBy="user"),
            item(U, 2, "2026-10-10T09:00:00Z", "user_message", text="never use rm -rf", createdBy="user"),
            item(U, 3, "2026-10-10T09:01:00Z", "assistant_message", text="ok"),
            item(U, 4, "2026-10-10T09:02:00Z", "command_execution", input="ls", output="a b",
                 nativeItemRef={"nativeId": "call-4"}),
            item(U, 5, "2026-10-11T01:00:00Z", "user_message", text="tomorrow", createdBy="user"),
            item(W, 1, "2026-10-10T10:00:00Z", "user_message", text="brief: fix db", createdBy="agent"),
            item(W, 2, "2026-10-10T10:05:00Z", "assistant_message", text="fixed"),
        ]
        its += [item(B, n, f"2026-10-10T11:{n:02d}:00Z", "assistant_message", text="y" * 300)
                for n in range(1, 21)]
        its += [item(R, 1, "2026-10-10T13:00:00Z", "assistant_message", text="first"),
                item(R, 2, "2026-10-10T13:05:00Z", "assistant_message", status="running", text="half a sen"),
                item(R, 3, "2026-10-10T13:10:00Z", "assistant_message", text="later")]
        its += [item(f"{DT}stale-item-1", 1, "2026-10-10T14:00:00Z", "assistant_message", text="one"),
                item(f"{DT}stale-item-1", 2, "2026-10-10T14:05:00Z", "subagent", status="running", title="left behind"),
                item(f"{DT}stale-item-1", 3, "2026-10-10T14:10:00Z", "assistant_message", text="three")]
        its += [item(f"{DT}final-v3-a-{i}", 1, "2026-10-10T12:00:00Z", "assistant_message", text="label")
                for i in range(4)]
        its += [item(f"{DT}contextmd-capture-old-s000-judge-1", 1, "2026-10-10T12:00:00Z",
                     "assistant_message", text="judged")]
        self.ch = chromeria_db(self.tmp / "ch.sqlite", threads, its)
        self.ao = observer_db(self.tmp / "ao.sqlite", [
            {"thread": U, "key": "codex:u", "native": "u", "submissions": [("sub-2", "never use rm -rf")],
             "events": [(11, "call-4", epoch("2026-10-10T09:02:00Z")), (12, "x", epoch("2026-10-10T09:01:30Z"))]},
            {"thread": W, "key": "codex:w", "native": "w", "events": [(21, "y", epoch("2026-10-10T10:06:00Z"))]},
            {"thread": B, "key": "codex:b", "native": "b", "events": [(31, "z", epoch("2026-10-10T11:30:00Z"))]},
            {"thread": R, "key": "codex:r", "native": "r", "events": [(41, "q", epoch("2026-10-10T13:00:00Z"))]},
            {"thread": f"{DT}stale-item-1", "key": "codex:s", "native": "s",
             "events": [(51, "q_1", epoch("2026-10-10T14:00:00Z"))]},
        ] + [{"thread": f"{DT}contextmd-capture-child-{n}", "key": f"codex:c{n}", "native": f"c{n}",
              "responses": [("gpt-6.1-sol-2026", 1000)]} for n in range(3)])
        self.cfg = dict(config.DEFAULTS, machine="m1", state_dir=str(self.tmp / "state"),
                        segment_chars=2000, context_chars=500, overlap_chars=700)
        self.syncs = []
        self.cap = self.new()

    def new(self, sync=None):
        return Capture(self.cfg, chromeria_con=self.ch, observer_con=self.ao,
                       sync=sync or (lambda: self.syncs.append(1)))

    def prepare(self):
        return self.cap.prepare("2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z")

    def lines(self, run, seg_id):
        return json.loads((self.cap.store.runs / run["run_id"] / "seg" / seg_id / "lines.json").read_text())

    def dispatch_all(self, step, child=0):
        """Record a task for every due job of one step; returns the jobs."""
        due = [j for j in self.cap.next_jobs()["due"] if j["step"] == step]
        for n, j in enumerate(due):
            self.cap.record(j["job"], task_id=f"task-{j['job']}",
                            child_thread=f"{DT}contextmd-capture-child-{child}")
        return due

    def write(self, job, text):
        Path(job["output"]).write_text(text)

    def finish_segment(self, run, seg_id, keep=True):
        """Both extractors find one lesson at the first body line; the judge pairs them."""
        first = min(int(n) for n in self.lines(run, seg_id))
        for name in ("sol", "grok"):
            out = self.cap.store.runs / run["run_id"] / "seg" / seg_id / f"extract-{name}.md"
            out.write_text(md((f"lesson {seg_id}", first, "user")))
        for j in self.cap.next_jobs()["due"]:  # writes the blind judge inputs
            if j["job"] == f"{seg_id}-judge":
                self.cap.record(j["job"], task_id="task-judge", child_thread=f"{DT}contextmd-capture-child-1")
        sdir = self.cap.store.runs / run["run_id"] / "seg" / seg_id
        (sdir / "judge.jsonl").write_text(json.dumps(
            {"a": ["A01"], "b": ["B01"], "best": "A01", "grade": "keep" if keep else "junk",
             "unsupported": False, "learned_from": "user", "reason": "r"}) + "\n")

    def test_skips_batch_and_own_threads_and_counts_them(self):
        run = self.prepare()
        st = {t: e["status"] for t, e in run["threads"].items()}
        self.assertEqual(sum(1 for s in st.values() if s == "skipped:worker-batch"), 4)
        self.assertEqual(st[f"{DT}contextmd-capture-old-s000-judge-1"], "skipped:capture")
        self.assertEqual(st[self.U], "captured")
        self.assertEqual(self.syncs, [1])

    def test_window_and_context(self):
        run = self.prepare()
        seg = next(s for s in run["segments"] if s["thread"] == self.U)
        text = (self.cap.store.runs / run["run_id"] / "seg" / seg["id"] / "transcript.txt").read_text()
        self.assertIn("old question", text.split("## EVENTS")[0])
        self.assertIn("never use rm -rf", text.split("## EVENTS")[1])
        # Later events are shown only read-only, after the extractable lines.
        self.assertIn("tomorrow", text.split("## FOLLOWING")[1])
        self.assertNotIn("tomorrow", text.split("## FOLLOWING")[0])
        self.assertNotIn("tomorrow", json.dumps(self.lines(run, seg["id"])))
        self.assertEqual(seg["last"]["ordinal"], 4)
        big = [s for s in run["segments"] if s["thread"] == self.B]
        self.assertGreater(len(big), 1)

    def test_extract_then_judge_then_commit_writes_complete_items_and_moves_cursor(self):
        run = self.prepare()
        due = self.cap.next_jobs()["due"]
        self.assertEqual({j["step"] for j in due}, {"extract"})
        self.assertEqual(len(due), 2 * len(run["segments"]))
        sol = next(j for j in due if j["job"].endswith("extract-sol"))
        self.assertEqual(sol["target"]["model"], "gpt-6.1-sol")
        self.assertTrue(sol["clientRequestId"].startswith("contextmd-capture-"))
        brief = Path(sol["task"].split("brief at ", 1)[1].split(" and do", 1)[0]).read_text()
        self.assertIn(sol["output"], brief)
        self.assertIn(f"contract: {_contract('extract.md')}", brief)
        self.dispatch_all("extract")
        useg = next(s for s in run["segments"] if s["thread"] == self.U)["id"]
        for s in run["segments"]:
            self.finish_segment(run, s["id"])
        result = self.cap.commit()
        self.assertEqual(result["state"], "complete")
        self.assertIsNone(self.cap.store.active_run())
        q = [i for i in self.cap.store.queued() if i["key"]["thread"] == self.U]
        self.assertEqual(len(q), 1)
        it = q[0]
        self.assertEqual(it["found_by"], ["grok", "sol"])
        self.assertEqual(it["state"], "extracted")
        self.assertEqual(it["key"]["event"], f"{self.U}-i2")
        self.assertEqual(it["moment"]["observer"], {"session_key": "codex:u", "ref": "submission",
                                                    "id": "sub-2", "anchor": "exact"})
        self.assertTrue(observer.exists(self.ao, it["moment"]["observer"]))
        self.assertEqual(it["models"]["extractors"]["sol"]["observed"], ["gpt-6.1-sol-2026"])
        self.assertEqual(it["models"]["judge"]["requested"], "claude-opus-5-5")
        for field in ("source_time", "scope", "learned_from", "grade", "found_by"):
            self.assertTrue(it[field], field)
        self.assertEqual(self.cap.store.cursors()[self.U]["ordinal"], 4)
        # The same window again finds nothing new for captured threads.
        run2 = self.prepare()
        self.assertEqual(run2["threads"][self.U]["status"], "no-new-events")
        self.assertIn(useg, [s["id"] for s in run["segments"]])

    def test_resume_never_redispatches_recorded_jobs(self):
        self.prepare()
        first = self.dispatch_all("extract")
        # The planner thread stops here. A new process resumes from saved task ids.
        resumed = self.new().next_jobs()
        self.assertEqual(resumed["due"], [])
        self.assertEqual(sorted(w["job"] for w in resumed["waiting"]), sorted(j["job"] for j in first))
        self.assertEqual(resumed["waiting"][0]["task_id"], "task-" + resumed["waiting"][0]["job"])
        with self.assertRaises(RunError):
            self.cap.record(first[0]["job"], task_id="another-task")

    def test_failed_job_is_handed_out_again_as_a_new_attempt(self):
        self.prepare()
        job = self.dispatch_all("extract")[0]
        self.write(job, "I failed")
        nxt = self.cap.next_jobs()
        self.assertIn(job["job"], [i["job"] for i in nxt["invalid"]])
        self.cap.record(job["job"], failed=True, note="output not in contract")
        again = [j for j in self.cap.next_jobs()["due"] if j["job"] == job["job"]]
        self.assertEqual(len(again), 1)
        self.assertTrue(again[0]["clientRequestId"].endswith("-2"))
        # Each attempt has its own output file, so the failed attempt cannot overwrite the new one.
        self.assertNotEqual(again[0]["output"], job["output"])
        self.assertTrue(again[0]["output"].endswith(".a2.md"))
        self.assertTrue(Path(job["output"]).exists())

    def test_half_written_output_waits_for_the_run_and_stale_judge_inputs_are_redone(self):
        run = self.prepare()
        seg = next(s for s in run["segments"] if s["thread"] == self.U)["id"]
        sdir = self.cap.store.runs / run["run_id"] / "seg" / seg
        first = min(int(n) for n in self.lines(run, seg))
        self.cap.record(f"{seg}-extract-sol", task_id="t-sol", child_thread=f"{DT}contextmd-capture-child-0")
        self.cap.record(f"{seg}-extract-grok", task_id="t-grok", child_thread=f"{DT}contextmd-capture-busy")
        (sdir / "extract-sol.md").write_text(md(("one", first, "user")))
        (sdir / "extract-grok.md").write_text(md(("one", first, "user")))  # grok still writing
        nxt = self.cap.next_jobs()
        self.assertNotIn(f"{seg}-judge", [j["job"] for j in nxt["due"]])
        self.assertIn({"job": f"{seg}-extract-grok", "task_id": "t-grok", "output": "running"}, nxt["waiting"])
        # The pre-fix race: judge inputs built from the half-written list, then grok adds an item.
        self.cap.record(f"{seg}-extract-grok", task_id="t-grok", child_thread=f"{DT}contextmd-capture-child-0")
        self.cap.next_jobs()
        self.cap.record(f"{seg}-judge", task_id="t-judge", child_thread=f"{DT}contextmd-capture-child-1")
        (sdir / "extract-grok.md").write_text(md(("one", first, "user"), ("two", first, "user")))
        (sdir / "judge.jsonl").write_text(json.dumps(
            {"a": ["A01"], "b": ["B01"], "grade": "keep", "learned_from": "user"}) + "\n")
        self.assertIn(f"{seg}-judge", [i["job"] for i in self.cap.next_jobs()["invalid"]])
        self.cap.record(f"{seg}-judge", failed=True, note="stale inputs")
        again = [j for j in self.cap.next_jobs()["due"] if j["job"] == f"{seg}-judge"]
        self.assertEqual(len(again), 1)
        listed = sum((sdir / f"judge-{side}.jsonl").read_text().count("\n") for side in "AB")
        self.assertEqual(listed, 3)

    def test_record_requires_the_child_thread(self):
        self.prepare()
        job = self.cap.next_jobs()["due"][0]["job"]
        with self.assertRaises(RunError):
            self.cap.record(job, task_id="t1")

    def test_extractor_failed_after_judge_wrote_does_not_crash_and_is_redone(self):
        run = self.prepare()
        seg = next(s for s in run["segments"] if s["thread"] == self.U)["id"]
        self.dispatch_all("extract")
        self.finish_segment(run, seg)
        self.cap.record(f"{seg}-extract-sol", failed=True, note="redo")
        nxt = self.cap.next_jobs()
        self.assertIn(f"{seg}-extract-sol", [j["job"] for j in nxt["due"]])
        self.assertNotIn(f"{seg}-judge", [j["job"] for j in nxt["due"] + nxt["invalid"]])
        self.cap.commit()
        self.assertFalse(next(s for s in self.cap.load()["segments"] if s["id"] == seg)["committed"])

    def test_same_count_redo_makes_judge_inputs_stale(self):
        run = self.prepare()
        seg = next(s for s in run["segments"] if s["thread"] == self.U)["id"]
        sdir = self.cap.store.runs / run["run_id"] / "seg" / seg
        self.dispatch_all("extract")
        self.finish_segment(run, seg)
        self.cap.record(f"{seg}-extract-grok", failed=True, note="redo")
        redo = next(j for j in self.cap.next_jobs()["due"] if j["job"] == f"{seg}-extract-grok")
        self.cap.record(redo["job"], task_id="t-redo", child_thread=f"{DT}contextmd-capture-child-0")
        first = min(int(n) for n in self.lines(run, seg))
        Path(redo["output"]).write_text(md(("a different lesson", first, "failure")))  # same count
        self.assertIn(f"{seg}-judge", [i["job"] for i in self.cap.next_jobs()["invalid"]])
        self.cap.record(f"{seg}-judge", failed=True, note="stale")
        self.assertIn(f"{seg}-judge", [j["job"] for j in self.cap.next_jobs()["due"]])
        self.assertIn("a different lesson", (sdir / "judge-A.jsonl").read_text() + (sdir / "judge-B.jsonl").read_text())

    def test_failed_child_run_is_reported_not_waited_on(self):
        run = self.prepare()
        seg = run["segments"][0]["id"]
        self.ch.execute("INSERT INTO orchestration_v2_projection_runs VALUES(?,?,?,?)",
                        ("run-dead", f"{DT}dead", 1, json.dumps({"status": "failed", "completedAt": "x"})))
        self.cap.record(f"{seg}-extract-sol", task_id="t", child_thread=f"{DT}dead")
        out = self.cap._paths(self.cap.load(), self.cap.load()["jobs"][f"{seg}-extract-sol"])["output"]
        out.write_text("## half")
        self.assertIn({"job": f"{seg}-extract-sol", "task_id": "t", "output": "run-failed"},
                      self.cap.next_jobs()["invalid"])
        # A status between running and completed is waited on, not failed.
        self.ch.execute("UPDATE orchestration_v2_projection_runs SET payload_json=? WHERE run_id='run-dead'",
                        (json.dumps({"status": "completing"}),))
        self.assertIn({"job": f"{seg}-extract-sol", "task_id": "t", "output": "running"},
                      self.cap.next_jobs()["waiting"])

    def test_recapture_with_two_lessons_at_one_event_keeps_every_lesson(self):
        run = self.prepare()
        useg = next(s for s in run["segments"] if s["thread"] == self.U)["id"]
        self.dispatch_all("extract")
        self.finish_segment(run, useg)
        self.cap.commit()
        self.cap.abandon("rest stuck")
        self.cap.store.set_cursor(self.U, {"ordinal": 1, "baseline": True})
        run2 = self.prepare()
        seg2 = next(s for s in run2["segments"] if s["thread"] == self.U)["id"]
        sdir = self.cap.store.runs / run2["run_id"] / "seg" / seg2
        first = min(int(n) for n in self.lines(run2, seg2))
        self.dispatch_all("extract")
        for name in ("sol", "grok"):
            (sdir / f"extract-{name}.md").write_text(md(("new A", first, "user"), ("new B", first, "user")))
        for j in self.cap.next_jobs()["due"]:
            if j["job"] == f"{seg2}-judge":
                self.cap.record(j["job"], task_id="tj", child_thread=f"{DT}contextmd-capture-child-1")
        (sdir / "judge.jsonl").write_text(
            json.dumps({"a": ["A01"], "b": ["B01"], "grade": "keep", "learned_from": "user"}) + "\n" +
            json.dumps({"a": ["A02"], "b": ["B02"], "grade": "keep", "learned_from": "user"}) + "\n")
        self.cap.commit()
        # A crash after the items were written but before run.json was saved: commit again.
        run2 = self.cap.load(run2["run_id"])
        next(s for s in run2["segments"] if s["id"] == seg2)["committed"] = False
        self.cap.save(run2)
        self.cap.commit()
        mine = [i for i in self.cap.store.queued() if i["key"]["thread"] == self.U]
        self.assertEqual(sorted(i["title"] for i in mine), sorted(["lesson " + useg, "new A", "new B"]))

    def test_item_past_the_window_stops_the_segment_even_if_later_items_are_earlier(self):
        self.ch.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES(?,?,?,?,?,?,?)",
                        (f"{self.W}-i3", self.W, 3, "assistant_message", "completed", "2026-10-11T00:00:05Z",
                         json.dumps({"startedAt": "2026-10-11T00:00:05Z", "text": "after midnight"})))
        self.ch.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES(?,?,?,?,?,?,?)",
                        (f"{self.W}-i4", self.W, 4, "assistant_message", "completed", "2026-10-10T23:59:00Z",
                         json.dumps({"startedAt": "2026-10-10T23:59:00Z", "text": "clock went back"})))
        run = self.prepare()
        seg = next(s for s in run["segments"] if s["thread"] == self.W)
        self.assertEqual(seg["last"]["ordinal"], 2)

    def test_leftover_running_item_in_a_finished_thread_does_not_block_it(self):
        run = self.prepare()
        tid = f"{DT}stale-item-1"
        self.assertNotIn("held_back", run["threads"][tid])
        seg = next(s for s in run["segments"] if s["thread"] == tid)
        self.assertEqual(seg["last"]["ordinal"], 3)

    def test_native_id_match_is_literal(self):
        ref = observer.anchor(self.ao, "codex:s", native_id="qx1", kind="call", text=None,
                              ts="2026-10-10T14:00:00Z")
        self.assertEqual(ref["anchor"], "next")  # "q_1" must not match "qx1" as a LIKE pattern
        self.assertEqual(observer.anchor(self.ao, "codex:s", native_id="q_1", kind="call", text=None,
                                         ts="2026-10-10T14:00:00Z")["anchor"], "exact")

    def test_items_still_being_written_are_held_back(self):
        run = self.prepare()
        seg = next(s for s in run["segments"] if s["thread"] == self.R)
        self.assertEqual(seg["last"]["ordinal"], 1)
        self.assertTrue(run["threads"][self.R]["held_back"])
        text = (self.cap.store.runs / run["run_id"] / "seg" / seg["id"] / "transcript.txt").read_text()
        self.assertNotIn("half a sen", text)
        self.assertNotIn("later", text.split("## FOLLOWING")[0])
        self.assertEqual([l["item_id"] for l in self.lines(run, seg["id"]).values()], [f"{self.R}-i1"])

    def test_lesson_without_observer_anchor_keeps_its_segment_open(self):
        self.ao.execute("DELETE FROM events WHERE session_key='codex:w'")
        run = self.prepare()
        wseg = next(s for s in run["segments"] if s["thread"] == self.W)["id"]
        self.dispatch_all("extract")
        for s in run["segments"]:
            self.finish_segment(run, s["id"])
        result = self.cap.commit()
        self.assertEqual(result["state"], "open")
        seg = next(s for s in self.cap.load()["segments"] if s["id"] == wseg)
        self.assertFalse(seg["committed"])
        self.assertIn("no Observer event", seg["error"])
        self.assertNotIn(self.W, moved(self.cap.store.cursors()))
        self.assertTrue(all(i["moment"]["observer"] for i in self.cap.store.queued()))
        self.assertIn(wseg, result["errors"])
        self.ao.execute("INSERT INTO events VALUES(?,?,?,?,?)", (22, "codex:w", epoch("2026-10-10T10:06:00Z"),
                                                                   "tool_call", "y"))
        self.assertEqual(self.cap.commit()["state"], "complete")

    def test_both_extractors_empty_commits_zero_items_without_a_judge(self):
        run = self.prepare()
        seg = next(s for s in run["segments"] if s["thread"] == self.W)["id"]
        self.dispatch_all("extract")
        for name in ("sol", "grok"):
            (self.cap.store.runs / run["run_id"] / "seg" / seg / f"extract-{name}.md").write_text("NO ITEMS")
        self.assertNotIn(f"{seg}-judge", [j["job"] for j in self.cap.next_jobs()["due"]])
        self.cap.commit()
        seg_rec = next(s for s in self.cap.load()["segments"] if s["id"] == seg)
        self.assertTrue(seg_rec["committed"])
        self.assertEqual(seg_rec["items"], 0)
        self.assertEqual(self.cap.store.cursors()[self.W]["ordinal"], 2)

    def test_abandoned_first_capture_is_retried_by_a_later_window(self):
        run = self.prepare()
        out = self.cap.abandon("stuck job")
        self.assertIsNone(self.cap.store.active_run())
        self.assertEqual(len(out["open_segments"]), len(run["segments"]))
        self.assertEqual(moved(self.cap.store.cursors()), {})
        # The next day's run starts each thread where the abandoned run started it.
        later = self.cap.prepare("2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z")
        useg = next(s for s in later["segments"] if s["thread"] == self.U)
        text = (self.cap.store.runs / later["run_id"] / "seg" / useg["id"] / "transcript.txt").read_text()
        self.assertIn("never use rm -rf", text.split("## EVENTS")[-1])
        self.assertIn("tomorrow", text)

    def test_recapture_after_abandon_never_replaces_another_runs_lesson(self):
        run = self.prepare()
        useg = next(s for s in run["segments"] if s["thread"] == self.U)["id"]
        self.dispatch_all("extract")
        self.finish_segment(run, useg)
        self.cap.commit()
        first = [i for i in self.cap.store.queued() if i["key"]["thread"] == self.U]
        self.cap.abandon("rest stuck")
        self.cap.store.set_cursor(self.U, {"ordinal": 1, "baseline": True})  # as if U had not committed
        run2 = self.prepare()
        seg2 = next(s for s in run2["segments"] if s["thread"] == self.U)["id"]
        self.dispatch_all("extract")
        self.finish_segment(run2, seg2)
        self.cap.commit()
        mine = [i for i in self.cap.store.queued() if i["key"]["thread"] == self.U]
        self.assertEqual(len(mine), 2)
        self.assertEqual(sorted(i["key"]["index"] for i in mine), [0, 1])
        self.assertEqual({i["run"]["run_id"] for i in mine}, {run["run_id"], run2["run_id"]})
        self.assertEqual(len(first), 1)

    def test_report_counts_match_the_queue(self):
        run = self.prepare()
        self.dispatch_all("extract")
        for s in run["segments"]:
            self.finish_segment(run, s["id"])
        self.cap.commit()
        r = report.build(self.cap.load(run["run_id"]), self.cap.store.queued())
        self.assertEqual(r["steps"]["lessons"], len(run["segments"]))
        self.assertEqual(r["steps"]["keep_found_by"], {"grok+sol": len(run["segments"])})
        self.assertEqual(r["steps"]["threads_skipped"], {"skipped:worker-batch": 4, "skipped:capture": 1})
        self.assertEqual(r["steps"]["threads_held_back"], 1)
        self.assertEqual(r["cost"]["sol"]["observed_models"], {"gpt-6.1-sol-2026": len(run["segments"])})

    def test_segments_overlap_and_lessons_cited_in_the_overlap_are_kept(self):
        run = self.prepare()
        big = [s for s in run["segments"] if s["thread"] == self.B]
        first, second = big[0], big[1]
        lines1 = self.lines(run, first["id"])
        ahead = [n for n, l in lines1.items() if not l["owned"]]
        owned = [n for n, l in lines1.items() if l["owned"]]
        self.assertTrue(ahead)
        # The overlap is the start of the next segment: the same turn items.
        nxt = [l["item_id"] for n, l in sorted(self.lines(run, second["id"]).items(), key=lambda x: int(x[0]))]
        self.assertEqual([lines1[n]["item_id"] for n in sorted(ahead, key=int)], nxt[:len(ahead)])
        self.assertTrue(all(l["owned"] for l in self.lines(run, big[-1]["id"]).values()))
        self.assertEqual(first["last"]["ordinal"], max(lines1[n]["ordinal"] for n in owned))
        # One lesson cited only in the overlap, one cited in the overlap by its best item
        # but in an owned line by the other extractor.
        sdir = self.cap.store.runs / run["run_id"] / "seg" / first["id"]
        self.dispatch_all("extract")
        a, o = int(ahead[0]), int(owned[0])
        (sdir / "extract-sol.md").write_text(md(("crosses the cut", a, "failure"), ("both", a, "user")))
        (sdir / "extract-grok.md").write_text(md(("both", o, "user")))
        sides = None
        for j in self.cap.next_jobs()["due"]:
            if j["job"] == f"{first['id']}-judge":
                self.cap.record(j["job"], task_id="tj", child_thread=f"{DT}contextmd-capture-child-1")
        sides = json.loads((sdir / "judge-sides.json").read_text())
        sol, grok = ("A", "B") if sides["A"] == "sol" else ("B", "A")
        g = lambda **kw: json.dumps({"grade": "keep", "learned_from": "user", **kw}) + "\n"
        (sdir / "judge.jsonl").write_text(
            g(**{sol.lower(): [f"{sol}01"]}, best=f"{sol}01") +
            g(**{sol.lower(): [f"{sol}02"], grok.lower(): [f"{grok}01"]}, best=f"{sol}02"))
        self.cap.commit()
        seg = next(s for s in self.cap.load()["segments"] if s["id"] == first["id"])
        self.assertTrue(seg["committed"])
        self.assertEqual(seg["in_overlap"], 1)
        kept = {i["title"]: i for i in self.cap.store.queued() if i["run"]["segment"] == first["id"]}
        self.assertEqual(sorted(kept), ["both", "crosses the cut"])
        self.assertEqual(kept["both"]["key"]["event"], lines1[str(o)]["item_id"])
        self.assertEqual(kept["crosses the cut"]["moment"]["cited"], "overlap")
        self.assertEqual(kept["crosses the cut"]["key"]["event"], lines1[str(a)]["item_id"])
        # The next segment's own lesson at that same event keeps its own key.
        self.finish_segment(run, second["id"])
        self.cap.commit()
        same = [i for i in self.cap.store.queued() if i["key"]["event"] == lines1[str(a)]["item_id"]]
        self.assertEqual(sorted(i["title"] for i in same), sorted(["crosses the cut", f"lesson {second['id']}"]))
        self.assertEqual(sorted(i["key"]["index"] for i in same), [0, 1])

    def test_replay_uses_the_same_transcripts_with_another_prompt_and_leaves_the_queue(self):
        run = self.prepare()
        self.dispatch_all("extract")
        for s_ in run["segments"]:
            self.finish_segment(run, s_["id"])
        self.cap.commit()
        before = len(self.cap.store.queued())
        seg = run["segments"][0]["id"]
        rep = self.cap.replay(run["run_id"], [seg], "v1", "extract-v1.md")
        self.assertEqual(rep["contracts"]["extract"], "extract-v1")
        src = (self.cap.store.runs / run["run_id"] / "seg" / seg / "transcript.txt").read_text()
        self.assertEqual((self.cap.store.runs / rep["run_id"] / "seg" / seg / "transcript.txt").read_text(), src)
        job = next(j for j in self.cap.next_jobs()["due"] if j["job"] == f"{seg}-extract-sol")
        brief = Path(job["task"].split("brief at ", 1)[1].split(" and do", 1)[0]).read_text()
        self.assertIn("contract: extract-v1", brief)
        self.assertIn("Do not keep", brief)
        self.dispatch_all("extract")
        self.finish_segment(rep, seg)
        self.assertEqual(self.cap.commit()["state"], "complete")
        self.assertEqual(len(self.cap.store.queued()), before)
        self.assertEqual(len(list((self.cap.store.runs / rep["run_id"] / "items").glob("*.json"))), 1)

    def test_replay_can_rerun_only_the_judge_on_the_same_extracts(self):
        run = self.prepare()
        self.dispatch_all("extract")
        for s_ in run["segments"]:
            self.finish_segment(run, s_["id"])
        self.cap.commit()
        seg = run["segments"][0]["id"]
        rep = self.cap.replay(run["run_id"], [seg], "rejudge", reuse_extracts=True)
        self.assertEqual(rep["contracts"]["judge"], _contract("judge.md"))
        src = (self.cap.store.runs / run["run_id"] / "seg" / seg / "extract-sol.md").read_text()
        self.assertEqual((self.cap.store.runs / rep["run_id"] / "seg" / seg / "extract-sol.md").read_text(), src)
        self.assertEqual([j["job"] for j in self.cap.next_jobs()["due"]], [f"{seg}-judge"])

    def test_dedupe_merges_found_by_across_segments(self):
        run = self.prepare()
        big = [s_["id"] for s_ in run["segments"] if s_["thread"] == self.B][:2]
        self.dispatch_all("extract")
        for s_ in run["segments"]:
            self.finish_segment(run, s_["id"])
        self.cap.commit()
        files = dedupe.item_files(self.cap.store.runs / run["run_id"], self.cap.store.queue, run["run_id"])
        # Make the two B lessons single-extractor: one Sol-only, one Grok-only.
        for (p, it), who in zip([f for f in files if f[1]["run"]["segment"] in big], (["sol"], ["grok"])):
            it["found_by"] = who
            p.write_text(json.dumps(it))
        files = dedupe.item_files(self.cap.store.runs / run["run_id"], self.cap.store.queue, run["run_id"])
        d = self.cap.store.runs / run["run_id"] / "dedupe"
        dedupe.prepare(d, files)
        ids = json.loads((d / "ids.json").read_text())
        pair = [i for i, name in ids.items()
                if any(p.name == name and it["run"]["segment"] in big for p, it in files)]
        (d / "groups.jsonl").write_text(json.dumps({"ids": pair, "lesson": "same"}) + "\n")
        out = dedupe.apply(d, files)
        self.assertEqual(out["distinct_lessons"], out["keep_lessons"] - 1)
        r = report.build(self.cap.load(run["run_id"]), self.cap.store.queued())
        self.assertEqual(r["steps"]["keep_found_by"].get("sol"), 1)
        self.assertEqual(r["steps"]["keep_found_by_distinct"].get("grok+sol"), out["distinct_lessons"])
        (d / "groups.jsonl").write_text(json.dumps({"ids": [pair[0], pair[0]]}) + "\n")
        with self.assertRaises(ValueError):
            dedupe.apply(d, files)

    def test_cursor_moves_only_past_leading_committed_segments(self):
        run = self.prepare()
        big = [s["id"] for s in run["segments"] if s["thread"] == self.B]
        self.dispatch_all("extract")
        self.finish_segment(run, big[1])  # second segment ready, first not
        self.cap.commit()
        self.assertNotIn(self.B, moved(self.cap.store.cursors()))
        self.assertIsNotNone(self.cap.store.active_run())
        self.finish_segment(run, big[0])
        self.cap.commit()
        cur = self.cap.store.cursors()[self.B]
        last = next(s for s in self.cap.load(run["run_id"])["segments"] if s["id"] == big[1])["last"]
        self.assertEqual(cur["ordinal"], last["ordinal"])

    def test_sync_failure_fails_prepare_and_commit(self):
        def fail():
            raise observer.SyncFailed("boom")
        with self.assertRaises(observer.SyncFailed):
            self.new(sync=fail).prepare("2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z")
        self.assertIsNone(self.cap.store.active_run())
        run = self.prepare()
        self.dispatch_all("extract")
        for s in run["segments"]:
            self.finish_segment(run, s["id"])
        with self.assertRaises(observer.SyncFailed):
            self.new(sync=fail).commit()
        self.assertEqual(self.cap.store.queued(), [])
        self.assertEqual(moved(self.cap.store.cursors()), {})

    def test_one_run_at_a_time(self):
        self.prepare()
        with self.assertRaises(RunError):
            self.prepare()
        store = Store(self.cfg["state_dir"])
        with store.locked():
            for call in (lambda c: c.next_jobs(), lambda c: c.commit(), lambda c: c.abandon("x"),
                         lambda c: c.record("s000-judge", failed=True)):
                with self.assertRaises(Busy):
                    call(self.new())

    def test_experiment_keeps_items_in_run_and_cursors_still(self):
        run = self.cap.prepare("2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z",
                               thread_ids=[self.B], whole=True, experiment="whole")
        self.assertEqual(len(run["segments"]), 1)
        self.dispatch_all("extract")
        self.finish_segment(run, run["segments"][0]["id"])
        self.cap.commit()
        self.assertEqual(self.cap.store.queued(), [])
        self.assertEqual(self.cap.store.cursors(), {})
        self.assertEqual(len(list((self.cap.store.runs / run["run_id"] / "items").glob("*.json"))), 1)


class CliLockTest(unittest.TestCase):
    def test_report_waits_for_no_one_and_fails_while_locked(self):
        from contextmd import cli
        d = Path(tempfile.mkdtemp())
        cfg = d / "c.json"
        cfg.write_text(json.dumps({"state_dir": str(d / "state")}))
        with Store(d / "state").locked():
            self.assertEqual(cli.main(["--config", str(cfg), "capture", "report"]), 1)


class SyncGateTest(unittest.TestCase):
    def script(self, body):
        p = Path(tempfile.mkdtemp()) / "ao"
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
        return str(p)

    def test_nonzero_exit_and_failed_sources_fail(self):
        with self.assertRaises(observer.SyncFailed):
            observer.sync(self.script("exit 3\n"))
        failed = json.dumps({"harnesses": [{"harness": "grok", "failed": ["x"]}]})
        with self.assertRaises(observer.SyncFailed):
            observer.sync(self.script(f"echo '{failed}'\n"))
        ok = json.dumps({"harnesses": [{"harness": "grok", "failed": []}]})
        self.assertEqual(observer.sync(self.script(f"echo '{ok}'\n"))["harnesses"][0]["harness"], "grok")


class CompareTest(unittest.TestCase):
    def it(self, title, ts, grade="keep", thread="T", seg="s000"):
        return {"key": {"thread": thread}, "grade": grade, "title": title, "what": title,
                "so_what": "", "source_time": ts, "run": {"segment": seg}}

    def test_lost_extra_and_wrong_with_distance_to_cut(self):
        d = Path(tempfile.mkdtemp())
        whole = [self.it("a", "2026-10-10T10:00:00Z"), self.it("b", "2026-10-10T11:58:00Z"),
                 self.it("junk", "2026-10-10T10:00:00Z", grade="junk"), self.it("other", "2026-10-10T10:00:00Z", thread="X")]
        seg = [self.it("a2", "2026-10-10T10:00:00Z"), self.it("c", "2026-10-10T12:03:00Z", seg="s001")]
        info = compare.prepare(d, "T", whole, seg)
        self.assertEqual((info["whole"], info["segmented"]), (2, 2))
        (d / "pairs.jsonl").write_text(
            '{"w":["W01"],"s":["S01"],"segmented_wrong":true,"note":"stale belief"}\n'
            '{"w":["W02"],"s":[],"note":"cut"}\n{"w":[],"s":["S02"],"note":"new"}\n')
        r = compare.result(d, ["2026-10-10T12:00:00Z"])
        self.assertEqual((r["matched"], r["lost_by_segmenting"], r["only_segmented"], r["segmented_wrong"]),
                         (1, 1, 1, 1))
        self.assertEqual(r["lost"][0]["minutes_from_cut"], 2.0)
        (d / "pairs.jsonl").write_text('{"w":["W01"],"s":["S01"]}\n')
        with self.assertRaises(ValueError):
            compare.result(d, [])


if __name__ == "__main__":
    unittest.main()
