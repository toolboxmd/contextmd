import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextmd.capture import compare, config, items, kinds, observer, transcript  # noqa: E402
from contextmd.capture.chromeria import Item, Thread  # noqa: E402
from contextmd.capture.run import Capture, RunError  # noqa: E402
from contextmd.capture.store import Busy, Store  # noqa: E402
from tests.fixtures import chromeria_db, observer_db  # noqa: E402

DT = "thread:delegated-task:command%3Amcp%3Ax%3Adelegate-task%3A"


def epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def item(tid, n, ts, type_, **payload):
    return {"id": f"{tid}-i{n}", "thread": tid, "ordinal": n, "ts": ts, "type": type_, "payload": payload}


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
        text, numbered = transcript.document("# h", ctx, body)
        self.assertIn("## CONTEXT", text)
        self.assertEqual(sorted(numbered), [3, 4, 5])
        self.assertIn("[3] user", text)


class ParseTest(unittest.TestCase):
    def test_extract_fields_and_no_items(self):
        got = items.parse_extract(md(("Use rsync excludes", 4, "failure")))
        self.assertEqual(got[0]["title"], "Use rsync excludes")
        self.assertEqual(items.event_number(got[0]["event"]), 4)
        self.assertEqual(items.learned(got[0]["learned_from"]), "failure")
        self.assertEqual(items.parse_extract("NO ITEMS"), [])
        with self.assertRaises(items.InvalidOutput):
            items.parse_extract("I could not read the file.")

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


class RunTest(unittest.TestCase):
    """A run over fake Chromeria and Observer records, driven like the thread drives it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        U, W, B = "U", f"{DT}fix-db-1", f"{DT}big-job-1"
        self.U, self.W, self.B = U, W, B
        threads = [{"id": "P", "created_by": "user"}, {"id": U, "created_by": "user", "title": "Main"},
                   {"id": W, "parent": "P", "title": "Fix the db"}, {"id": B, "parent": "P", "title": "Big job"}]
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
        ] + [{"thread": f"{DT}contextmd-capture-child-{n}", "key": f"codex:c{n}", "native": f"c{n}",
              "responses": [("gpt-6.1-sol-2026", 1000)]} for n in range(3)])
        self.cfg = dict(config.DEFAULTS, machine="rocky", state_dir=str(self.tmp / "state"),
                        segment_chars=2000, context_chars=500)
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
        self.assertNotIn("tomorrow", text)
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
        self.assertIn("contract: extract-v1", brief)
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
        self.assertFalse(Path(job["output"]).exists())

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

    def test_cursor_moves_only_past_leading_committed_segments(self):
        run = self.prepare()
        big = [s["id"] for s in run["segments"] if s["thread"] == self.B]
        self.dispatch_all("extract")
        self.finish_segment(run, big[1])  # second segment ready, first not
        self.cap.commit()
        self.assertNotIn(self.B, self.cap.store.cursors())
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
        self.assertEqual(self.cap.store.cursors(), {})

    def test_one_run_at_a_time(self):
        self.prepare()
        with self.assertRaises(RunError):
            self.prepare()
        store = Store(self.cfg["state_dir"])
        with store.locked():
            with self.assertRaises(Busy):
                self.new().next_jobs()

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
