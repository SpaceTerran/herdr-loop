from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock

import urllib.request
from pathlib import Path

from hl import items, serve
from hl.common import Refusal, read_json, write_json

from .support import FakeHerdr, SpawnCase

PORT = 8401
SERVER_PID = 4242


class FakeHost:
    """A machine where time moves only on sleep, and where the build comes
    up and goes down when the test says so."""

    def __init__(self):
        self.t = 1_000.0
        self.ports = {}
        self.starts = {}
        self.statuses = [None]
        self.run_rc = 0
        self.killed = []
        self.dies_on = None

    def listeners(self, port):
        return list(self.ports.get(port, []))

    def start_time(self, pid):
        return self.starts.get(pid)

    def command(self, pid):
        return f"python3 server-{pid}.py"

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        if sig == self.dies_on:
            self.down(pid)

    def url_status(self, url):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def run(self, command, cwd, timeout=120):
        return self.run_rc

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def up(self, pid=SERVER_PID, port=PORT):
        self.ports.setdefault(port, []).append(pid)
        self.starts[pid] = f"started-{pid}"

    def down(self, pid=SERVER_PID):
        self.starts.pop(pid, None)
        for holders in self.ports.values():
            if pid in holders:
                holders.remove(pid)


class ServingHerdr(FakeHerdr):
    """Running the serve script brings the build up; closing its pane takes it down."""

    def __init__(self, host, comes_up=True, dies_on_close=True):
        super().__init__()
        self.host, self.comes_up, self.dies_on_close = host, comes_up, dies_on_close

    def call(self, *args, timeout=None):
        result = super().call(*args, timeout=timeout)
        if args[:2] == ("pane", "run") and self.comes_up:
            self.host.up()
        if args[:2] == ("pane", "close") and self.dies_on_close:
            self.host.down()
        return result


class ServeCase(SpawnCase):
    def setUp(self):
        super().setUp()
        self.host = FakeHost()
        self.herdr = ServingHerdr(self.host)
        self.built("**Serve.** `PORT=8401 python3 app.py`\n**Ready.** URL `http://127.0.0.1:8401/health`\n")

    def built(self, report):
        if items.load_attempts(self.run_dir, "A-1")["build"] == 0:
            items.start_phase(self.run_dir, "A-1", "build", session="builder", engine="claude")
        self.write("A-1", "build-1.md", report)

    def start(self, **kw):
        return serve.start(self.root, self.run_dir, "A-1", self.herdr, self.host, **kw)

    def stop(self):
        return serve.stop(self.run_dir, "A-1", self.herdr, self.host)

    def record(self):
        return read_json(self.item_dir() / "serve-1.json")


class StartTest(ServeCase):
    def test_the_serve_line_is_run_under_sh_in_its_own_pane_until_ready_answers(self):
        self.host.statuses = [None, None, 200]
        code, result = self.start()
        self.assertEqual((code, result["result"], result["port"]), (0, "ready", PORT))
        script = (self.item_dir() / "serve-1.sh").read_text()
        self.assertEqual(script, f"#!/bin/sh\ncd '{self.worktree}' || exit 97\nPORT=8401 python3 app.py\n")
        tab = self.herdr.called("tab", "create")[0]
        self.assertEqual(tab[tab.index("--cwd") + 1], str(self.worktree))
        self.assertEqual(self.herdr.called("pane", "run")[0][3], f"/bin/sh '{self.item_dir()}/serve-1.sh'")
        self.assertEqual(result["pids"], {str(SERVER_PID): f"started-{SERVER_PID}"})
        self.assertRegex(result["summary"], r"^`http://127.0.0.1:8401/health` answered 200 4 s after the build was started on port 8401, at ")
        self.assertIsNone(self.record()["stopped_at"])

    def test_the_build_is_served_in_a_tab_of_the_runs_workspace_beside_its_evaluator(self):
        self.host.statuses = [200]
        _, served = self.start()
        home = read_json(self.run_dir / "workspace.json")
        self.assertEqual(served["pane"].split(":")[0], home["workspace"])
        self.assertNotEqual(served["pane"], home["pane"])
        self.assertEqual(self.herdr.tabs[self.herdr.panes[served["pane"]]["tab"]], "A-1 serve")
        self.assertEqual(self.herdr.labels(), ["canary run-1"])

    def test_a_port_already_in_use_stops_the_start_and_kills_nothing(self):
        self.host.up(pid=999)
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("port 8401 is already in use (pid 999: python3 server-999.py)", str(caught.exception))
        self.assertEqual((self.herdr.calls, self.host.killed), ([], []))
        self.assertFalse((self.item_dir() / "serve-1.json").exists())

    def test_a_long_command_line_keeps_the_end_that_says_what_the_process_is(self):
        self.host.up(pid=999)
        python = "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"
        self.host.command = lambda pid: f"{python} -m http.server 8401 --bind 127.0.0.1 --directory /private/tmp/an-empty-scratch-folder/{'x' * 200}"
        with self.assertRaises(Refusal) as caught:
            self.start()
        said = str(caught.exception)
        self.assertIn("pid 999: /opt/homebrew/Cellar/python@3.14", said)
        self.assertIn("-m http.server 8401 --bind 127.0.0.1", said)
        # A line longer than anyone would read is cut in the middle, both ends kept.
        self.host.command = lambda pid: "node " + "a" * 400 + " server.js " + "b" * 400 + " --port 8401"
        with self.assertRaises(Refusal) as caught:
            self.start()
        said = str(caught.exception)
        self.assertIn("pid 999: node aaaa", said)
        self.assertIn("bbbb --port 8401)", said)
        self.assertLess(len(said), 650)

    def test_an_item_parked_after_its_build_and_reopened_is_served(self):
        # The canary's port exercise: the port was taken, the item parked, the port freed, the item reopened.
        self.host.up(pid=999)
        with self.assertRaises(Refusal):
            self.start()
        items.set_status(self.run_dir, "A-1", "parked", "environment: port 8401 taken")
        self.host.down(pid=999)
        reopened = items.set_status(self.run_dir, "A-1", "pending", "the port is free again", reopen=True)
        self.assertEqual(reopened["status"], "in_progress")
        self.host.statuses = [200]
        code, result = self.start()
        self.assertEqual((code, result["result"]), (0, "ready"))

    def test_an_item_that_serves_nothing_only_has_its_ready_command_run(self):
        self.built("**Serve.** `true`\n**Ready.** COMMAND `test -e scripts/wordcount.py`\n")
        code, result = self.start()
        self.assertEqual((code, result["served"]), (0, False))
        self.assertIn("Nothing is served for this item. `test -e scripts/wordcount.py` exited 0", result["summary"])
        self.assertEqual(self.herdr.calls, [])
        self.assertTrue(self.stop()["already"])

    def test_a_ready_command_that_fails_is_not_ready(self):
        self.built("**Serve.** `true`\n**Ready.** COMMAND `test -e scripts/wordcount.py`\n")
        self.host.run_rc = 1
        code, result = self.start()
        self.assertEqual((code, result["result"]), (3, "not-ready"))

    def test_a_build_that_never_answers_is_stopped_and_its_last_lines_kept(self):
        self.herdr.screen = "Traceback (most recent call last):\nOSError: boom\n"
        code, result = self.start(timeout_s=20)
        self.assertEqual((code, result["result"]), (3, "not-ready"))
        self.assertIn("OSError: boom", (self.item_dir() / "serve-1.log").read_text())
        self.assertEqual(len(self.herdr.called("pane", "close")), 1)
        self.assertEqual(self.host.listeners(PORT), [])
        self.assertIsNotNone(self.record()["stopped_at"])

    def test_a_server_error_is_not_an_answer_but_a_missing_page_is(self):
        self.host.statuses = [502, 503, 404]
        code, result = self.start()
        self.assertEqual(code, 0)
        self.assertIn("answered 404 4 s after", result["summary"])

    def test_a_build_already_being_served_is_not_started_twice(self):
        self.host.statuses = [200]
        self.start()
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("already being served", str(caught.exception))

    def test_a_report_without_its_lines_is_refused(self):
        self.built("I built it.\n")
        with self.assertRaises(Refusal):
            self.start()

    def test_an_item_with_no_port_recorded_is_refused(self):
        item = items.load_item(self.run_dir, "A-1")
        del item["fields"]["port"]
        items.save_item(self.run_dir, item)
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("has no port recorded", str(caught.exception))


class StopTest(ServeCase):
    def setUp(self):
        super().setUp()
        self.host.statuses = [200]

    def test_closing_the_pane_stops_the_build_and_the_port_is_proved_free(self):
        self.start()
        result = self.stop()
        self.assertEqual((result["stopped"], result["already"], result["notes"]), (True, False, []))
        self.assertEqual(self.herdr.keys, [("ctrl+c",)])
        self.assertEqual(self.host.killed, [])
        self.assertIsNotNone(self.record()["stopped_at"])
        self.assertTrue(self.stop()["already"])

    def test_a_recorded_process_that_outlives_its_pane_is_signalled_by_pid(self):
        self.herdr.dies_on_close = False
        self.host.dies_on = signal.SIGTERM
        self.start()
        result = self.stop()
        self.assertEqual(self.host.killed, [(SERVER_PID, signal.SIGTERM)])
        self.assertEqual(result["notes"], [f"sent TERM to [{SERVER_PID}]"])

    def test_a_process_that_ignores_term_is_killed(self):
        self.herdr.dies_on_close = False
        self.host.dies_on = signal.SIGKILL
        self.start()
        self.stop()
        self.assertEqual(self.host.killed, [(SERVER_PID, signal.SIGTERM), (SERVER_PID, signal.SIGKILL)])

    def test_a_stranger_on_the_port_is_reported_and_never_signalled(self):
        self.start()
        self.herdr.dies_on_close = True
        original = self.herdr.call

        def close_then_squat(*args, timeout=None):
            result = original(*args, timeout=timeout)
            if args[:2] == ("pane", "close"):
                self.host.up(pid=999)
            return result

        self.herdr.call = close_then_squat
        with self.assertRaises(Refusal) as caught:
            self.stop()
        self.assertIn("port 8401 held by pid 999", str(caught.exception))
        self.assertIn("not this loop's to kill", str(caught.exception))
        self.assertEqual(self.host.killed, [])
        self.assertIsNone(self.record()["stopped_at"])

    def test_a_pid_that_now_belongs_to_another_process_is_not_signalled(self):
        self.herdr.dies_on_close = False
        self.start()
        # The server died by itself and its pid was given to something else that listens elsewhere.
        self.host.down()
        self.host.starts[SERVER_PID] = "started-later"
        self.stop()
        self.assertEqual(self.host.killed, [])

    def test_a_pane_id_that_now_holds_another_shell_is_not_closed(self):
        self.start()
        pane = self.record()["pane"]
        self.herdr.panes[pane]["shell_pid"] = 99999
        self.host.down()
        result = self.stop()
        self.assertEqual(self.herdr.called("pane", "close"), [])
        self.assertIn("left alone", result["notes"][0])

    def test_nothing_served_cannot_be_stopped_before_it_is_started(self):
        with self.assertRaises(Refusal):
            self.stop()


REF_PORT = 9901
REF_PID = 4343


class ReferenceHerdr(ServingHerdr):
    """The build comes up as before; running the reference script brings up its own server, and closing its pane takes it down."""

    ref_comes_up = True

    def call(self, *args, timeout=None):
        if args[:2] == ("pane", "run") and "reference-" in args[3]:
            result = FakeHerdr.call(self, *args, timeout=timeout)
            self.ref_pane = args[2]
            if self.ref_comes_up:
                self.host.up(pid=REF_PID, port=REF_PORT)
            return result
        if args[:2] == ("pane", "close") and args[2] == getattr(self, "ref_pane", None):
            result = FakeHerdr.call(self, *args, timeout=timeout)
            self.host.down(REF_PID)
            return result
        return super().call(*args, timeout=timeout)


class ReferenceTest(ServeCase):
    """An item of an app rebrand program: the browser tool refuses `file://`, so the reference a look is compared
    with was read as source. A repository whose spec names its reference directory has it served beside a
    looked-at build."""

    def setUp(self):
        super().setUp()
        self.herdr = ReferenceHerdr(self.host)
        self.host.free_port = lambda: REF_PORT
        self.host.statuses = [200]
        (self.worktree / "brand" / "family").mkdir(parents=True)
        (self.worktree / "brand" / "family" / "reference.html").write_text("<p>reference</p>\n")
        settings = read_json(self.root / "program.json")
        write_json(self.root / "program.json", {**settings, "repos": {"owner/repo": {"reference": "brand"}}})
        items.route_item(self.run_dir, "A-1", "independent", True, "a page a person sees")

    def test_a_looked_at_build_is_served_with_the_repositorys_reference_beside_it(self):
        code, result = self.start()
        self.assertEqual((code, result["result"]), (0, "ready"))
        ref = self.record()["reference"]
        self.assertEqual((ref["served"], ref["root"], ref["port"], ref["url"], ref["pids"]), (True, "brand", REF_PORT, f"http://127.0.0.1:{REF_PORT}/", {str(REF_PID): f"started-{REF_PID}"}))
        script = (self.item_dir() / "reference-1.sh").read_text()
        self.assertEqual(script, f"#!/bin/sh\ncd '{self.worktree}/brand' || exit 97\nexec '{sys.executable}' -m http.server {REF_PORT} --bind 127.0.0.1\n")
        self.assertEqual(sorted(self.herdr.tabs[self.herdr.panes[pane]["tab"]] for pane in self.herdr.tab_panes()), ["A-1 reference", "A-1 serve"])
        self.assertEqual(serve.status(self.run_dir, "A-1", self.host)["reference_listening_now"], [REF_PID])
        stopped = self.stop()
        self.assertTrue(stopped["stopped"])
        self.assertEqual((self.host.listeners(PORT), self.host.listeners(REF_PORT)), ([], []))
        self.assertTrue(self.record()["reference"]["stopped_at"])
        self.assertEqual(self.herdr.called("pane", "close")[-1][2], ref["pane"])

    def test_without_the_fact_or_a_look_nothing_more_is_served(self):
        item = items.load_item(self.run_dir, "A-1")
        item["route"] = {**item["route"], "look": False}
        items.save_item(self.run_dir, item)
        self.start()
        self.assertNotIn("reference", self.record())
        self.assertEqual(len(self.herdr.called("pane", "run")), 1)
        self.stop()
        self.assertEqual(serve.reference_dir(read_json(self.root / "program.json") | {"repos": {}}, items.load_run(self.run_dir), {"route": {"look": True}}), None)

    def test_a_reference_that_does_not_come_up_is_noted_and_the_build_is_still_served(self):
        self.herdr.ref_comes_up = False
        self.host.url_status = lambda url: None if str(REF_PORT) in url else 200
        code, result = self.start()
        self.assertEqual((code, result["result"]), (0, "ready"))
        ref = self.record()["reference"]
        self.assertEqual(ref["served"], False)
        self.assertIn(f"`brand` did not answer on port {REF_PORT} within 30 s, so no reference is served", ref["note"])
        self.assertEqual(self.herdr.called("pane", "close")[-1][2], ref["pane"])
        self.assertEqual(self.host.listeners(PORT), [SERVER_PID])
        # Stopping the build leaves the reference that never came up alone.
        self.stop()
        self.assertEqual(self.host.listeners(PORT), [])

    def test_a_reference_directory_the_checkout_lacks_is_noted(self):
        import shutil

        shutil.rmtree(self.worktree / "brand")
        self.start()
        self.assertEqual(self.record()["reference"], {"served": False, "root": "brand", "note": "`brand` is not a directory of this checkout, so no reference is served"})
        self.assertEqual(len(self.herdr.called("pane", "run")), 1)

    def test_a_start_interrupted_while_the_reference_comes_up_leaves_nothing_running_after_stop(self):
        # Found in review: nothing of the reference was on record until it answered, so a start interrupted while it
        # waited (up to 30 s) left the server up, and `stop` did not know of it.
        def answers(url):
            if str(REF_PORT) in url:
                raise KeyboardInterrupt
            return 200

        self.host.url_status = answers
        with self.assertRaises(KeyboardInterrupt):
            self.start()
        self.assertEqual(self.host.listeners(REF_PORT), [REF_PID])
        ref = self.record()["reference"]
        self.assertEqual((ref["served"], ref["port"], ref["pids"]), (True, REF_PORT, {}))
        self.stop()
        self.assertEqual((self.host.listeners(PORT), self.host.listeners(REF_PORT)), ([], []))
        self.assertTrue(self.record()["reference"]["stopped_at"] and self.record()["stopped_at"])

    def test_a_build_that_cannot_be_stopped_does_not_leave_the_reference_running(self):
        # Found in review: a stranger on the build's port refused the stop before the reference was reached.
        self.start()
        self.host.up(pid=777, port=PORT)
        with self.assertRaises(Refusal) as caught:
            self.stop()
        self.assertIn(f"build 1 of A-1 is not fully stopped: recorded processes alive [], port {PORT} held by pid 777", str(caught.exception))
        self.assertEqual(self.host.listeners(REF_PORT), [])
        record = self.record()
        self.assertTrue(record["reference"]["stopped_at"])
        self.assertIsNone(record.get("build_stopped_at"))
        self.assertIsNone(record.get("stopped_at"))
        # Once the stranger is gone, the next stop finishes the build and does not touch the reference again.
        self.host.down(777)
        closes = len(self.herdr.called("pane", "close"))
        self.assertTrue(self.stop()["stopped"])
        self.assertTrue(self.record()["stopped_at"])
        self.assertNotIn(record["reference"]["pane"], [c[2] for c in self.herdr.called("pane", "close")[closes:]])

    def test_a_reference_server_that_outlives_its_pane_is_signalled_and_a_stranger_on_its_port_refuses_the_stop(self):
        self.start()
        self.host.down(REF_PID)
        self.host.up(pid=777, port=REF_PORT)
        with self.assertRaises(Refusal) as caught:
            self.stop()
        self.assertIn(f"the reference server of build 1 of A-1 is not fully stopped: recorded processes alive [], port {REF_PORT} held by pid 777", str(caught.exception))
        self.assertNotIn((777, signal.SIGTERM), self.host.killed)
        self.assertFalse(self.record().get("stopped_at"))


class CliTest(ServeCase):
    def cli(self, *argv, herdr_fake=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = serve.main(
                ["--root", str(self.root), "--run-dir", str(self.run_dir), *argv], herdr=herdr_fake, host=self.host
            )
        return code, json.loads(out.getvalue())

    def test_start_then_status_then_stop(self):
        self.host.statuses = [200]
        self.assertEqual(self.cli("start", "--id", "A-1", herdr_fake=self.herdr)[0], 0)
        code, state = self.cli("status", "--id", "A-1")
        self.assertEqual((code, state["listening_now"]), (0, [SERVER_PID]))
        self.assertEqual(self.cli("stop", "--id", "A-1", herdr_fake=self.herdr)[0], 0)

    def test_a_taken_port_exits_2(self):
        self.host.up(pid=999)
        code, out = self.cli("start", "--id", "A-1", herdr_fake=self.herdr)
        self.assertEqual((code, out["result"]), (2, "refused"))

    def test_serving_outside_herdr_is_refused(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("HERDR_ENV", None)
            code, out = self.cli("start", "--id", "A-1")
        self.assertEqual(code, 2)
        self.assertIn("not inside a Herdr pane", out["refused"][0])


class RealHostTest(unittest.TestCase):
    """The parts that read the real machine, against a real listener."""

    def test_a_real_listener_is_found_probed_and_stopped(self):
        host = serve.Host()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertEqual(host.listeners(port), [])
        self.assertIsNone(host.url_status(f"http://127.0.0.1:{port}/"))
        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(server.wait)
        self.addCleanup(server.kill)
        deadline = time.time() + 15
        while host.url_status(f"http://127.0.0.1:{port}/") is None and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(host.url_status(f"http://127.0.0.1:{port}/"), 200)
        self.assertEqual(host.url_status(f"http://127.0.0.1:{port}/no-such-page"), 404)
        self.assertEqual(host.listeners(port), [server.pid])
        self.assertIsNotNone(host.start_time(server.pid))
        self.assertIn("http.server", host.command(server.pid))
        host.kill(server.pid, signal.SIGTERM)
        server.wait(timeout=10)
        self.assertEqual(host.listeners(port), [])
        self.assertIsNone(host.start_time(server.pid))
        host.kill(server.pid, signal.SIGTERM)  # a pid that is gone is not an error

    def test_the_reference_script_serves_its_directory_for_real(self):
        """The script `hl-serve` writes for a reference, run as a pane would run it, against the real host."""
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tree = Path(tmp.name) / "checkout"
        (tree / "brand" / "family").mkdir(parents=True)
        (tree / "brand" / "family" / "reference.html").write_text("<p>the reference</p>\n")
        host = serve.Host()
        launched = []

        class PaneRuns:
            def call(self, *args, timeout=None):
                if args[:2] == ("tab", "create"):
                    return {"root_pane": {"pane_id": "w:p1"}, "tab": {"tab_id": "w:t1"}}
                if args[:2] == ("pane", "process-info"):
                    return {"process_info": {"shell_pid": 1}}
                if args[:2] == ("pane", "run"):
                    launched.append(subprocess.Popen(["/bin/sh", "-c", args[3]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
                return {}

        cfg = {"repos": {"owner/repo": {"reference": "brand"}}}
        record = Path(tmp.name) / "serve-1.json"
        write_json(record, {"build": 1})
        ref = serve._serve_reference(cfg, {"repo": "owner/repo"}, {"id": "A-1", "route": {"look": True}}, Path(tmp.name), 1, tree, PaneRuns(), host, "w", record, timeout_s=15)
        self.assertEqual(read_json(record)["reference"], ref)
        for proc in launched:
            self.addCleanup(proc.wait)
            self.addCleanup(lambda p=proc: os.killpg(p.pid, signal.SIGKILL) if p.poll() is None else None)
        self.assertTrue(ref["served"], ref)
        self.assertEqual(host.url_status(ref["url"] + "family/reference.html"), 200)
        with urllib.request.urlopen(ref["url"] + "family/reference.html", timeout=5) as response:
            self.assertEqual(response.read(), b"<p>the reference</p>\n")
        # Bound to the loopback, and only the recorded processes hold its port.
        self.assertEqual(sorted(int(p) for p in ref["pids"]), host.listeners(ref["port"]))

    def test_a_command_is_run_under_sh_in_the_given_directory(self):
        host = serve.Host()
        self.assertEqual(host.run("test -d .", "/"), 0)
        self.assertEqual(host.run("exit 7", "/"), 7)
