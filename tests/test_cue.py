import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextmd.cue import service, window as W  # noqa: E402

BIN = Path(__file__).resolve().parents[1] / "bin" / "contextmd-cue"


class WindowTest(unittest.TestCase):
    def test_message_window_matches_the_trained_layout(self):
        items = [("user", "Fix the bug"), ("tool", "Bash: pytest"), ("asst", "Done."), ("user", "Now open the PR")]
        self.assertEqual(
            W.window(items),
            "## EARLIER USER MESSAGE 1\n\nFix the bug\n\n## AGENT'S REPLY\n\nDone.\n\n"
            "## USER MESSAGE (current turn)\n\nNow open the PR")

    def test_relays_are_context_not_turns(self):
        items = [("user", "Plan the release"), ("asst", "Here is the plan."),
                 ("user", "[Message from reviewer] two findings"), ("user", "Fix them")]
        self.assertEqual(
            W.window(items),
            "## EARLIER USER MESSAGE 1\n\nPlan the release\n\n## AGENT'S REPLY\n\nHere is the plan.\n\n"
            "## RELAYED MESSAGE (another agent or a quote)\n\n[Message from reviewer] two findings\n\n"
            "## USER MESSAGE (current turn)\n\nFix them")

    def test_a_long_current_message_keeps_its_end_within_the_budget(self):
        text = " ".join(f"{k:05d}" for k in range(2000))  # 11,999 characters
        got = W.window([("user", text)])
        # The whole 10,000-character budget goes to the message: its last 9,995 characters after the mark.
        self.assertEqual(got, "## AGENT'S PREVIOUS REPLY\n\n(none)\n\n## USER MESSAGE (current turn)\n\n"
                              "[...]" + text[-9995:])

    def test_tool_moments_add_the_turn_the_call_and_a_failure(self):
        items = [("user", "Ship it"), ("asst", "Running the tests."), ("tool", "Bash: pytest")]
        base = W.window(items)
        self.assertEqual(base, "## AGENT'S PREVIOUS REPLY\n\n(none)\n\n## USER MESSAGE (current turn)\n\nShip it")
        pre = W.window(items, "pre_tool", call="Bash: git push")
        self.assertEqual(pre, base + "\n\n## AGENT'S WORK THIS TURN (latest last)\n\nRunning the tests.\n"
                                     "Tool call: Bash: pytest\n\n## AGENT'S NEXT TOOL CALL\n\nBash: git push")
        failed = W.window(items, "tool_failure", call="Bash: pytest", output="x" * 3000 + "1 failed")
        self.assertTrue(failed.startswith(base + "\n\n## AGENT'S WORK THIS TURN"))
        self.assertTrue(failed.endswith("## TOOL CALL THAT FAILED\n\nBash: pytest\n\nEnd of its output:\n[...]"
                                        + ("x" * 3000 + "1 failed")[-(W.OUTPUT_CHARS - len(W.MARK)):]))

    def test_malformed_citation_markup_stays_text(self):
        text = 'See <assistant_citations>["x"]</assistant_citations> here'
        self.assertEqual(W.window([("user", text)]),
                         f"## AGENT'S PREVIOUS REPLY\n\n(none)\n\n## USER MESSAGE (current turn)\n\n{text}")

    def test_no_user_message_is_an_error(self):
        with self.assertRaises(W.NoMessage):
            W.window([("asst", "hello")])


class FakeScorer:
    def __init__(self):
        self.calls = []
        self.gate = None

    def __call__(self, text, docs):
        self.calls.append((text, docs))
        if self.gate is not None:
            self.gate.wait(5)
        return [float(len(d)) / 10 for d in docs], {"window_tokens": 7, "window_cut": False}


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.scorer = FakeScorer()
        self.server = service.make_server("127.0.0.1", 0, service.Variant("v1", {"threshold": 0.5}), self.scorer)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, body, timeout=5):
        req = urllib.request.Request(f"{self.url}/score", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    REQ = {"items": [["user", "Open the PR"]], "moment": {"kind": "user_message"},
           "entries": [{"id": "a", "doc": "A: when"}, {"id": "b", "doc": "B: when it applies"}]}

    def test_scores_each_entry_on_the_built_window(self):
        status, out = self.post(self.REQ)
        self.assertEqual(status, 200)
        self.assertEqual(out["scores"], {"a": 0.7, "b": 1.8})
        self.assertEqual((out["adapter"], out["threshold"]), ("v1", 0.5))
        [(text, docs)] = self.scorer.calls
        self.assertEqual(text, W.window([("user", "Open the PR")]))
        self.assertEqual(docs, ["A: when", "B: when it applies"])
        with urllib.request.urlopen(f"{self.url}/health", timeout=5) as r:
            self.assertEqual(json.loads(r.read()), {"ok": True, "adapter": "v1", "threshold": 0.5})

    def test_a_scorer_failure_answers_500(self):
        def boom(text, docs):
            raise RuntimeError("CUDA out of memory")
        server = service.make_server("127.0.0.1", 0, service.Variant("v1", {"threshold": 0.5}), boom)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/score",
                                         data=json.dumps(self.REQ).encode())
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 500)
        finally:
            server.shutdown()
            server.server_close()

    def test_bad_requests_are_refused_without_scoring(self):
        for bad in ({**self.REQ, "items": []}, {**self.REQ, "entries": [{"id": "a"}]},
                    {**self.REQ, "items": [["system", "x"]]}, {**self.REQ, "items": [["asst", "no user"]]},
                    {**self.REQ, "moment": {"kind": "pre_tool", "call": {"not": "text"}}}, [1, 2]):
            with self.subTest(bad=bad):
                status, _ = self.post(bad)
                self.assertEqual(status, 400)
        self.assertEqual(self.scorer.calls, [])

    def test_a_request_that_waits_past_its_deadline_is_dropped_not_run(self):
        self.scorer.gate = threading.Event()
        first = {}
        t = threading.Thread(target=lambda: first.update(zip(("status", "out"), self.post(self.REQ))))
        t.start()
        while not self.scorer.calls:
            pass
        late = {}
        t2 = threading.Thread(target=lambda: late.update(zip(("status", "out"),
                                                             self.post({**self.REQ, "deadline_ms": 50}))))
        t2.start()
        t2.join(0.3)  # the late request is queued behind the GPU lock
        self.scorer.gate.set()
        t.join(5)
        t2.join(5)
        self.assertEqual(first["status"], 200)
        self.assertEqual((late["status"], late["out"]["error"]), (503, "expired"))
        self.assertEqual(len(self.scorer.calls), 1)


class SwitchTest(unittest.TestCase):
    def config(self, d, **extra):
        cfg = {"host": "127.0.0.1", "port": 1, "active": "new", "previous": "old",
               "deployments": str(Path(d) / "state" / "deployments.jsonl"),
               "variants": {"new": {"threshold": 0.4}, "old": {"threshold": 0.9}, "next": {"threshold": 0.5}},
               **extra}
        path = Path(d) / "service.json"
        path.write_text(json.dumps(cfg))
        return path

    def test_rollback_is_one_step_and_logged(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.config(d)
            path.chmod(0o600)
            out = subprocess.run([sys.executable, str(BIN), "rollback", str(path), "--no-restart"],
                                 capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            cfg = json.loads(path.read_text())
            self.assertEqual((cfg["active"], cfg["previous"]), ("old", "new"))
            [line] = (Path(d) / "state" / "deployments.jsonl").read_text().splitlines()
            rec = json.loads(line)
            self.assertEqual((rec["action"], rec["from"], rec["to"], rec["restart"]),
                             ("rollback", "new", "old", "skipped"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_deploy_keeps_the_active_variant_as_previous(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.config(d)
            service.switch(path, "deploy", "next")
            cfg = json.loads(path.read_text())
            self.assertEqual((cfg["active"], cfg["previous"]), ("next", "new"))
            with self.assertRaises(ValueError):
                service.switch(path, "deploy", "missing")
            with self.assertRaises(ValueError):
                service.switch(path, "deploy", "next")

    def test_rollback_without_a_previous_variant_fails_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.config(d, previous=None)
            before = path.read_text()
            with self.assertRaises(ValueError):
                service.switch(path, "rollback")
            self.assertEqual(path.read_text(), before)
            self.assertFalse((Path(d) / "state").exists())


if __name__ == "__main__":
    unittest.main()
