from __future__ import annotations

import contextlib
import io
import json
import os
import stat
from pathlib import Path
from unittest import mock

from hl import herdr, items, merge, wait
from hl.common import Refusal, read_json, utc_now, write_json
from hl.herdr import HerdrError

from .support import RunCase, install_fake_gh

SESSION = "11111111-2222-3333-4444-555555555555"

# A stand-in for `herdr` that answers `agent get` from the JSON file named by
# HL_FAKE_HERDR: {"agents": {name: AgentInfo}}.
FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["HL_FAKE_HERDR"]))
args = sys.argv[1:]
if args[:2] == ["agent", "get"]:
    info = state["agents"].get(args[2])
    if info is None:
        print(json.dumps({"id": "1", "error": {"code": "agent_not_found", "message": "no agent named " + args[2]}}))
        sys.exit(1)
    print(json.dumps({"id": "1", "result": {"agent": info}}))
    sys.exit(0)
sys.stderr.write("fake herdr: unexpected " + " ".join(args) + "\n")
sys.exit(64)
'''


class Clock:
    """Time that moves only when the code under test sleeps."""

    def __init__(self):
        self.t = 2_000_000_000.0
        self.on_sleep = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        if self.on_sleep:
            self.on_sleep.pop(0)()


class ScriptedHerdr:
    """Answers `agent` from a list, repeating the last answer. An exception
    in the list is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def agent(self, name):
        self.calls += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def info(status="working", seq=1):
    return {"agent_status": status, "state_change_seq": seq, "pane_id": "w9:p1"}


class WaitCase(RunCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.agree()
        self.build()
        self.clock = Clock()
        self.home = self.tmp / "home"
        (self.home / ".claude" / "projects" / "-some-worktree").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def worker(self, phase="build", n=1, kind="claude", session=SESSION, started_at=None, pid=None):
        record = {"agent": f"b-a1-{n}", "kind": kind, "pane": "w9:p1", "started_at": started_at or utc_now(), "pid": pid}
        if session:
            record["session"] = session
        herdr.write_worker(self.item_dir(), phase, n, record)

    def transcript(self, age_s, session=SESSION):
        path = self.home / ".claude" / "projects" / "-some-worktree" / f"{session}.jsonl"
        path.write_text("{}\n")
        os.utime(path, (self.clock.t - age_s, self.clock.t - age_s))
        return path

    def wait(self, herdr_fake=None, watch=("A-1:build-1",), checks=(), tools=None, *extra, shells=None):
        argv = ["--run-dir", str(self.run_dir), "--interval", "20", *extra]
        opts = wait._parser().parse_args(argv)
        watches = [wait.Watch(self.run_dir, spec) for spec in watch]
        return wait.wait(
            self.run_dir, watches, list(checks), herdr_fake, tools, opts, clock=self.clock.now, sleep=self.clock.sleep, shells=shells
        )

    def sub_agent(self, age_s, session=SESSION, name="agent-a1"):
        """A sub-agent's transcript, where Claude Code keeps it: beside its session's."""
        path = self.home / ".claude" / "projects" / "-some-worktree" / session / "subagents" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
        os.utime(path, (self.clock.t - age_s, self.clock.t - age_s))
        return path


class FakeShells:
    def __init__(self, running=True):
        self.answer = running
        self.asked = []

    def running(self, pid):
        self.asked.append(pid)
        return self.answer


class WorkerWaitTest(WaitCase):
    def test_a_done_file_ends_the_wait_without_asking_herdr(self):
        self.worker()
        self.write("A-1", "build-1.done", "done\n")
        fake = ScriptedHerdr(HerdrError("exit_1", "server not running"))
        event = self.wait(fake)
        self.assertEqual((event["event"], event["item"], event["phase"], event["n"]), ("done", "A-1", "build", 1))
        self.assertEqual(fake.calls, 0)

    def test_a_done_file_that_appears_later_ends_the_wait(self):
        self.worker()
        self.transcript(age_s=0)
        self.clock.on_sleep = [lambda: None, lambda: self.write("A-1", "build-1.done", "done\n")]
        event = self.wait(ScriptedHerdr(info()))
        self.assertEqual((event["event"], event["waited_s"]), ("done", 40))

    def test_attempt_1s_done_file_does_not_end_a_wait_for_attempt_2(self):
        self.worker(n=1)
        self.write("A-1", "build-1.done", "done\n")
        items.start_phase(self.run_dir, "A-1", "grade", session="eval-1", head_sha="a" * 40)
        items.start_phase(self.run_dir, "A-1", "build", session="build-1", engine="claude")
        self.worker(n=2)
        self.transcript(age_s=0)
        # The session keeps working: its transcript is touched at every look.
        self.clock.on_sleep = [lambda: self.transcript(age_s=0)] * 100
        event = self.wait(ScriptedHerdr(info()), ("A-1:build-2",))
        self.assertEqual((event["event"], event["watching"]), ("time", ["A-1:build-2"]))

    def test_a_worker_herdr_no_longer_has_is_gone(self):
        self.worker()
        event = self.wait(ScriptedHerdr(None))
        self.assertEqual((event["event"], event["agent"], event["pane"]), ("gone", "b-a1-1", "w9:p1"))

    def test_a_dialog_is_reported_once_and_again_only_when_it_is_a_new_one(self):
        self.worker()
        self.transcript(age_s=0)
        self.clock.on_sleep = [lambda: self.transcript(age_s=0)] * 500
        self.assertEqual(self.wait(ScriptedHerdr(info("blocked", seq=4)))["event"], "blocked")
        self.assertEqual(self.wait(ScriptedHerdr(info("blocked", seq=4)))["event"], "time")
        self.assertEqual(self.wait(ScriptedHerdr(info("blocked", seq=9)))["event"], "blocked")

    def test_a_claude_worker_whose_transcript_stopped_is_quiet(self):
        self.worker()
        self.transcript(age_s=11 * 60)
        event = self.wait(ScriptedHerdr(info("working")), shells=FakeShells(running=True))
        self.assertEqual((event["event"], event["idle_min"], event["status"]), ("quiet", 11, "working"))
        # Herdr already reads it as working: nothing was learned from the process list.
        self.assertNotIn("command_running", event)

    def test_a_claude_worker_reading_as_working_is_given_the_longer_time(self):
        # A long foreground command: herdr says working and the transcript is silent meanwhile.
        self.worker()
        self.transcript(age_s=4 * 60)
        self.assertEqual(self.wait(ScriptedHerdr(info("working")), ("A-1:build-1",), (), None, "--max-min", "3")["event"], "time")

    def test_a_claude_worker_that_ended_its_turn_without_a_done_file_is_stopped(self):
        self.worker()
        self.transcript(age_s=4 * 60)
        event = self.wait(ScriptedHerdr(info("done")))
        self.assertEqual((event["event"], event["idle_min"], event["status"]), ("stopped", 4, "done"))
        self.assertEqual(self.wait(ScriptedHerdr(info("done")), ("A-1:build-1",), (), None, "--max-min", "2")["event"], "time")

    def test_a_sub_agent_at_work_is_the_workers_own_progress(self):
        # The worker handed a long run to a sub-agent: its own transcript stands still, the sub-agent's moves.
        self.worker()
        self.transcript(age_s=15 * 60)
        for status in ("working", "done"):
            self.sub_agent(age_s=30)
            event = self.wait(ScriptedHerdr(info(status)), ("A-1:build-1",), (), None, "--max-min", "2")
            self.assertEqual(event["event"], "time", status)

    def test_a_sub_agent_that_went_silent_too_does_not_hide_a_stall(self):
        self.worker()
        self.transcript(age_s=15 * 60)
        self.sub_agent(age_s=12 * 60)
        event = self.wait(ScriptedHerdr(info("working")))
        self.assertEqual((event["event"], event["idle_min"]), ("quiet", 12))

    def test_a_worker_whose_turn_ended_with_a_command_still_running_has_not_stopped(self):
        self.worker(pid=4242)
        self.transcript(age_s=4 * 60)
        shells = FakeShells(running=True)
        event = self.wait(ScriptedHerdr(info("done")), ("A-1:build-1",), (), None, "--max-min", "2", shells=shells)
        self.assertEqual(event["event"], "time")
        self.assertEqual(set(shells.asked), {4242})

    def test_a_command_that_runs_past_the_longer_time_is_reported_as_quiet_and_says_so(self):
        self.worker(pid=4242)
        self.transcript(age_s=11 * 60)
        event = self.wait(ScriptedHerdr(info("done")), shells=FakeShells(running=True))
        self.assertEqual((event["event"], event["status"], event["command_running"]), ("quiet", "done", True))

    def test_with_no_command_running_a_turn_that_ended_is_still_stopped(self):
        self.worker(pid=4242)
        self.transcript(age_s=4 * 60)
        event = self.wait(ScriptedHerdr(info("done")), shells=FakeShells(running=False))
        self.assertEqual((event["event"], event["status"]), ("stopped", "done"))
        self.assertNotIn("command_running", event)

    def test_the_process_list_is_not_read_while_the_worker_is_within_its_time(self):
        self.worker(pid=4242)
        self.transcript(age_s=60)
        shells = FakeShells(running=True)
        self.wait(ScriptedHerdr(info("done")), ("A-1:build-1",), (), None, "--max-min", "1", shells=shells)
        self.assertEqual(shells.asked, [])

    def test_a_cursor_worker_is_not_asked_about_shell_commands(self):
        self.worker(kind="cursor", session=None, pid=4242)
        shells = FakeShells(running=True)
        self.clock.on_sleep = [lambda: None] * 20
        event = self.wait(ScriptedHerdr(info("done")), shells=shells)
        self.assertEqual((event["event"], shells.asked), ("stopped", []))

    def test_a_running_shell_command_is_told_from_the_process_list_as_the_mini_prints_it(self):
        # Two lines read from `ps -axo ppid=,command=` on the mini, 2026-10-12: a manager's two background waits.
        listing = (
            "91444 /bin/zsh -c source /Users/you/.claude/shell-snapshots/snapshot-zsh-1791841203252-strjar.sh 2>/dev/null || true && setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL 2>/dev/null || true && eval 'R=/Users/you/loops/x'\n"
            "98152 claude --dangerously-skip-permissions --model claude-sonnet-5-5 --effort high --tools Bash,Read,Edit,Write,Glob,Grep\n"
            "98256 npm exec @playwright/mcp@0.0.82 --headless --isolated\n"
            "  777 /bin/zsh -c echo .claude/shell-snapshots/snapshot- is only text here\n"
        )
        with mock.patch("hl.wait.subprocess.run", return_value=mock.Mock(stdout=listing)):
            shells = wait.Shells()
            self.assertTrue(shells.running(91444))
            # An MCP server is a child too, and is not a command the worker is waiting on.
            self.assertFalse(shells.running(98256))
            self.assertFalse(shells.running(98152))
            self.assertTrue(shells.running(777))
            self.assertFalse(shells.running(None))

    def test_the_same_silence_is_not_reported_twice_but_a_new_one_is(self):
        self.worker()
        self.transcript(age_s=11 * 60)
        self.assertEqual(self.wait(ScriptedHerdr(info()))["event"], "quiet")
        self.assertEqual(self.wait(ScriptedHerdr(info()))["event"], "time")
        # It moved, then stopped again.
        self.transcript(age_s=12 * 60)
        self.assertEqual(self.wait(ScriptedHerdr(info()))["event"], "quiet")

    def test_a_claude_worker_that_never_wrote_a_transcript_is_quiet(self):
        self.worker(started_at="2020-01-01T00:00:00Z")
        self.assertEqual(self.wait(ScriptedHerdr(info()))["event"], "quiet")

    def test_a_claude_worker_reading_idle_with_a_live_transcript_is_not_quiet(self):
        # Herdr reads a Claude session as done or idle while a background task still runs.
        self.worker()
        self.transcript(age_s=0)
        self.clock.on_sleep = [lambda: self.transcript(age_s=0)] * 100
        self.assertEqual(self.wait(ScriptedHerdr(info("done")))["event"], "time")

    def test_a_cursor_worker_not_working_for_three_minutes_is_stopped(self):
        # Herdr reads a Cursor session waiting on a question as done, not blocked.
        self.worker(kind="cursor", session=None)
        event = self.wait(ScriptedHerdr(info("done", seq=3)))
        self.assertEqual((event["event"], event["idle_min"], event["waited_s"]), ("stopped", 3, 200))

    def test_a_cursor_worker_that_goes_back_to_work_resets_the_count(self):
        self.worker(kind="cursor", session=None)
        answers = [info("idle")] * 5 + [info("working")] + [info("idle", seq=2)]
        event = self.wait(ScriptedHerdr(*answers))
        # Five looks idle (to 80 s), working at 100 s, idle again from 120 s: a fresh three minutes.
        self.assertEqual((event["event"], event["waited_s"]), ("stopped", 120 + 200))

    def test_a_working_cursor_worker_is_left_to_the_clock(self):
        self.worker(kind="cursor", session=None)
        self.assertEqual(self.wait(ScriptedHerdr(info("working")))["event"], "time")

    def limit_record(self, uuid="u-1"):
        return {
            "type": "assistant",
            "uuid": uuid,
            "error": "rate_limit",
            "isApiErrorMessage": True,
            "apiErrorStatus": 429,
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your weekly limit"}]},
            "quotaLimits": {"status": "rejected", "resetsAt": 1791849600, "rateLimitType": "seven_day"},
        }

    def write_transcript(self, *records):
        path = self.transcript(age_s=0)
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        os.utime(path, (self.clock.t, self.clock.t))
        return path

    def test_a_claude_worker_stopped_on_a_usage_limit_says_when_it_resets(self):
        self.worker()
        self.write_transcript({"type": "user"}, self.limit_record(), {"type": "system", "subtype": "turn_duration"})
        event = self.wait(ScriptedHerdr(info("done")))
        self.assertEqual((event["event"], event["limit_type"]), ("limit", "seven_day"))
        self.assertEqual(event["resets_at"], "2026-10-13T00:00:00Z")
        self.assertIn("weekly limit", event["text"])
        # The seat it hit, so the manager can tell whether the program has since moved off it.
        self.assertEqual(event["seat"], str(self.home / ".claude"))

    def test_the_same_limit_is_reported_once(self):
        self.worker()
        self.write_transcript(self.limit_record())
        self.clock.on_sleep = [lambda: self.write_transcript(self.limit_record())] * 100
        self.assertEqual(self.wait(ScriptedHerdr(info("done")))["event"], "limit")
        self.assertEqual(self.wait(ScriptedHerdr(info("done")), ("A-1:build-1",), (), None, "--max-min", "2")["event"], "time")

    def test_a_limit_the_session_has_since_got_past_is_not_reported(self):
        self.worker()
        normal = {"type": "assistant", "uuid": "u-2", "message": {"content": [{"type": "text", "text": "back"}]}}
        self.write_transcript(self.limit_record(), {"type": "user"}, normal)
        self.clock.on_sleep = [lambda: self.write_transcript(self.limit_record(), normal)] * 100
        self.assertEqual(self.wait(ScriptedHerdr(info("working")), ("A-1:build-1",), (), None, "--max-min", "2")["event"], "time")

    def test_a_transcript_line_that_is_not_json_is_skipped(self):
        self.worker()
        path = self.write_transcript(self.limit_record())
        with open(path, "a") as fh:
            fh.write("{half a rec")
        os.utime(path, (self.clock.t, self.clock.t))
        self.assertEqual(self.wait(ScriptedHerdr(info("done")))["event"], "limit")

    def test_herdr_failing_five_looks_in_a_row_is_reported(self):
        self.worker()
        event = self.wait(ScriptedHerdr(HerdrError("exit_1", "connection refused")))
        self.assertEqual((event["event"], event["source"], event["waited_s"]), ("error", "herdr", 80))
        self.assertIn("connection refused", event["detail"])

    def test_herdr_failing_briefly_is_not_reported(self):
        self.worker()
        error = HerdrError("timeout", "no answer")
        event = self.wait(ScriptedHerdr(error, error, error, error, None))
        self.assertEqual(event["event"], "gone")

    def test_the_clock_ends_a_wait_with_nothing_to_report(self):
        self.worker()
        self.transcript(age_s=0)
        self.clock.on_sleep = [lambda: self.transcript(age_s=0)] * 100
        event = self.wait(ScriptedHerdr(info()), ("A-1:build-1",), (), None, "--max-min", "5")
        self.assertEqual((event["event"], event["waited_s"]), ("time", 300))

    def test_with_nothing_to_watch_it_is_a_timer(self):
        event = self.wait(None, (), (), None, "--max-min", "10")
        self.assertEqual((event["event"], event["waited_s"], event["watching"]), ("time", 600, []))


class WatchSpecTest(WaitCase):
    def test_a_watch_names_one_attempt(self):
        self.worker()
        watch = wait.Watch(self.run_dir, "A-1:build-1")
        self.assertEqual((watch.item_id, watch.phase, watch.n), ("A-1", "build", 1))
        self.assertEqual(watch.done_file, self.item_dir() / "build-1.done")

    def test_a_contract_draft_is_watched_by_its_own_done_file(self):
        self.worker(phase="contract-draft")
        watch = wait.Watch(self.run_dir, "A-1:contract-draft-1")
        self.assertEqual((watch.phase, watch.n), ("contract-draft", 1))
        self.assertEqual(watch.done_file.name, "contract-draft-1.done")

    def test_a_pattern_is_refused(self):
        for spec in ("A-1", "A-1:build", "A-1:build-*", "A-1:build-1.done"):
            with self.assertRaises(Refusal, msg=spec):
                wait.Watch(self.run_dir, spec)

    def test_an_attempt_with_no_worker_record_is_refused(self):
        with self.assertRaises(Refusal) as caught:
            wait.Watch(self.run_dir, "A-1:build-1")
        self.assertIn("build-1.worker.json does not exist", str(caught.exception))

    def test_an_unknown_item_is_refused(self):
        with self.assertRaises(Refusal):
            wait.Watch(self.run_dir, "Z-9:build-1")


class ChecksWaitTest(WaitCase):
    def setUp(self):
        super().setUp()
        self.gh_state = self.tmp / "gh.json"
        fake = install_fake_gh(self.tmp)
        self.tools = merge.Tools(gh=str(fake), env={**os.environ, "HL_FAKE_GH": str(self.gh_state)})
        items.set_fields(self.run_dir, "A-1", ["pr=7"])

    def set_checks(self, checks, required=None, rulesets=()):
        state = {"pr": {"headRefOid": "b" * 40, "baseRefName": "main"}, "checks": checks, "required_names": required, "ruleset_names": list(rulesets)}
        self.gh_state.write_text(json.dumps(state))

    def check(self, bucket):
        return [{"name": "ci", "bucket": bucket, "state": bucket.upper()}]

    def wait_checks(self, *extra):
        return self.wait(None, (), ("A-1",), self.tools, *extra)

    def on_train(self, *item_checks):
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"merge": "train", "train": {"branch": "hl-train/canary", "item_checks": list(item_checks)}}})

    def test_an_item_on_a_train_that_is_asked_no_check_has_nothing_to_wait_for(self):
        self.on_train()
        self.set_checks(self.check("pending"))
        event = self.wait_checks()
        self.assertEqual((event["event"], event["passed"], event["none_asked"]), ("checks", True, True))

    def test_an_item_on_a_train_waits_for_the_checks_its_repository_names_and_no_others(self):
        self.on_train("lint")
        other = {"name": "app-tests", "bucket": "pending", "state": "IN_PROGRESS"}
        self.set_checks([{"name": "lint", "bucket": "pass", "state": "SUCCESS"}, other])
        event = self.wait_checks()
        self.assertEqual((event["passed"], event["checks"]), (True, [{"name": "lint", "bucket": "pass"}]))
        self.set_checks([{"name": "lint", "bucket": "fail", "state": "FAILURE", "link": "https://x/actions/runs/9/job/1"}, other])
        event = self.wait_checks()
        self.assertFalse(event["passed"])
        self.assertIn("`lint`: fail", Path(event["checks_file"]).read_text())

    def test_a_named_check_that_never_reports_is_said_after_the_grace_and_not_waited_for_without_end(self):
        self.on_train("lint")
        self.set_checks([{"name": "app-tests", "bucket": "pass", "state": "SUCCESS"}])
        self.assertEqual(self.wait_checks("--max-min", "0.001")["event"], "time")
        event = self.wait_checks("--checks-grace-min", "0")
        self.assertEqual((event["event"], event["passed"], event["not_reported"]), ("checks", False, ["lint"]))

    def test_a_required_check_that_has_not_reported_yet_holds_the_wait(self):
        # `gh pr checks --required` lists only the required checks that have reported: a help-desk program's CI check
        # reports once its shards finish, and the wait answered green before it had (a decision in the help-desk
        # catalog program's ledger).
        self.set_checks(self.check("pass"), required=["ci", "full-ci"])
        self.assertEqual(self.wait_checks("--max-min", "0.001")["event"], "time")
        event = self.wait_checks("--checks-grace-min", "0")
        self.assertEqual((event["event"], event["passed"], event["not_reported"]), ("checks", False, ["full-ci"]))
        self.set_checks(self.check("pass") + [{"name": "full-ci", "bucket": "pass", "state": "SUCCESS"}], required=["ci", "full-ci"])
        self.assertTrue(self.wait_checks()["passed"])

    def test_a_check_a_ruleset_requires_is_waited_for_too(self):
        self.set_checks(self.check("pass"), required=["ci"], rulesets=["secret-scan"])
        event = self.wait_checks("--checks-grace-min", "0")
        self.assertEqual((event["passed"], event["not_reported"]), (False, ["secret-scan"]))

    def test_green_checks_end_the_wait_with_the_head_they_ran_on(self):
        self.set_checks(self.check("pass"))
        event = self.wait_checks()
        self.assertEqual((event["event"], event["passed"], event["head_sha"]), ("checks", True, "b" * 40))
        self.assertEqual(event["checks"], [{"name": "ci", "bucket": "pass"}])

    def test_green_checks_write_nothing_for_the_builder(self):
        self.set_checks(self.check("pass"))
        self.assertNotIn("checks_file", self.wait_checks())
        self.assertEqual(list(self.item_dir().glob("checks-*")), [])

    def test_red_checks_are_written_down_for_the_builder_with_how_to_read_each_log(self):
        self.set_checks(
            [
                {"name": "app-tests", "bucket": "fail", "state": "FAILURE", "link": "https://github.com/owner/repo/actions/runs/4242/job/9"},
                {"name": "secret-scan", "bucket": "pass", "state": "SUCCESS", "link": "https://github.com/owner/repo/actions/runs/4243/job/1"},
                {"name": "external", "bucket": "cancel", "state": "CANCELLED", "link": ""},
            ]
        )
        event = self.wait_checks()
        self.assertFalse(event["passed"])
        path = self.item_dir() / "checks-1.md"
        self.assertEqual(event["checks_file"], str(path))
        text = path.read_text()
        self.assertIn("Required checks on pull request #7 of owner/repo: not green", text)
        self.assertIn("on commit `" + "b" * 40 + "`", text)
        self.assertIn("- `app-tests`: fail. https://github.com/owner/repo/actions/runs/4242/job/9 Its log: `gh run view 4242 --repo owner/repo --log-failed`.", text)
        self.assertIn("- `external`: cancel.", text)
        self.assertIn("Green: `secret-scan`.", text)
        self.assertNotIn("- `secret-scan`", text)

    def test_checks_that_went_green_on_a_rerun_leave_nothing_for_the_builder(self):
        # Found in review: red, then re-run to green, and the next build's brief still pointed at the red file.
        self.set_checks(self.check("fail"))
        red = Path(self.wait_checks()["checks_file"])
        self.assertTrue(red.exists())
        self.set_checks(self.check("pass"))
        event = self.wait_checks()
        self.assertEqual((event["passed"], event["cleared_checks_file"]), (True, str(red)))
        self.assertFalse(red.exists())

    def test_the_red_checks_file_is_named_for_the_build_they_ran_on(self):
        self.grade()
        self.build(session="build-2")
        self.set_checks(self.check("fail"))
        self.assertTrue(self.wait_checks()["checks_file"].endswith("checks-2.md"))

    def test_pending_checks_keep_it_waiting_until_they_finish(self):
        self.set_checks(self.check("pending"))
        self.clock.on_sleep = [lambda: None, lambda: None, lambda: self.set_checks(self.check("fail"))]
        event = self.wait_checks()
        # The checks finish at 60 s; GitHub is asked every 30 s on a 20 s look, so next at 80 s.
        self.assertEqual((event["event"], event["passed"], event["waited_s"]), ("checks", False, 80))

    def test_one_check_still_pending_among_finished_ones_keeps_it_waiting(self):
        self.set_checks(self.check("pass") + [{"name": "slow", "bucket": "pending", "state": "IN_PROGRESS"}])
        self.assertEqual(self.wait_checks("--max-min", "3")["event"], "time")

    def test_no_checks_yet_is_waited_out_then_reported(self):
        self.set_checks(None)
        event = self.wait_checks()
        self.assertEqual((event["event"], event["passed"], event["none_reported"]), ("checks", False, True))
        self.assertGreaterEqual(event["waited_s"], 300)

    def test_checks_that_appear_within_the_grace_are_read_normally(self):
        self.set_checks(None)
        self.clock.on_sleep = [lambda: None, lambda: self.set_checks(self.check("pass"))]
        self.assertTrue(self.wait_checks()["passed"])

    def test_a_pull_request_that_conflicts_with_its_base_is_reported_at_once_not_waited_on(self):
        # An item of a help-desk feature program: a pull request GitHub reported CONFLICTING got no checks, and the
        # wait said only `none_reported` once its grace ran out. A conflict is a rework, and is said as one.
        for pr in ({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}, {"mergeStateStatus": "DIRTY"}, {"mergeable": "CONFLICTING"}):
            self.gh_state.write_text(json.dumps({"pr": {"headRefOid": "b" * 40, "baseRefName": "main", **pr}, "checks": None}))
            event = self.wait_checks()
            self.assertEqual((event["event"], event["passed"], event["conflicting"], event["head_sha"], event["waited_s"]), ("checks", False, True, "b" * 40, 0))
            self.assertIn("conflicts with `main`: a rework. Write in `rework-2.md`", event["next"])
            self.assertIn("merges `origin/main` into its branch (never a rebase or a force-push)", event["next"])
        # Green checks on a head that conflicts since are no reason to merge either.
        self.gh_state.write_text(json.dumps({"pr": {"headRefOid": "b" * 40, "baseRefName": "main", "mergeable": "CONFLICTING"}, "checks": self.check("pass")}))
        self.assertTrue(self.wait_checks()["conflicting"])

    def test_a_merge_state_github_has_not_worked_out_yet_is_no_conflict(self):
        self.gh_state.write_text(json.dumps({"pr": {"headRefOid": "b" * 40, "baseRefName": "main", "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}, "checks": self.check("pass")}))
        event = self.wait_checks()
        self.assertEqual((event["passed"], "conflicting" in event), (True, False))

    def test_an_item_with_no_pull_request_recorded_is_refused(self):
        items.set_fields(self.run_dir, "A-2", ["branch=x"])
        self.set_checks(self.check("pass"))
        with self.assertRaises(Refusal) as caught:
            self.wait(None, (), ("A-2",), self.tools)
        self.assertIn("A-2 has no pr field", str(caught.exception))

    def test_gh_failing_five_looks_in_a_row_is_reported(self):
        self.gh_state.write_text("not json")
        event = self.wait_checks()
        self.assertEqual((event["event"], event["source"], event["item"]), ("error", "gh", "A-1"))

    def test_a_worker_and_its_checks_can_be_watched_together(self):
        self.worker()
        self.transcript(age_s=0)
        self.set_checks(self.check("pending"))
        self.clock.on_sleep = [lambda: self.write("A-1", "build-1.done", "done\n")]
        event = self.wait(ScriptedHerdr(info()), ("A-1:build-1",), ("A-1",), self.tools)
        self.assertEqual(event["event"], "done")


class CliTest(WaitCase):
    def setUp(self):
        super().setUp()
        self.herdr_state = self.tmp / "herdr.json"
        self.fake_herdr = self.tmp / "herdr"
        self.fake_herdr.write_text(FAKE_HERDR)
        self.fake_herdr.chmod(self.fake_herdr.stat().st_mode | stat.S_IXUSR)
        patcher = mock.patch.dict(os.environ, {"HL_FAKE_HERDR": str(self.herdr_state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = wait.main(["--run-dir", str(self.run_dir), "--herdr", str(self.fake_herdr), *argv])
        return code, json.loads(out.getvalue())

    def test_the_herdr_binary_is_asked_and_a_missing_agent_is_gone(self):
        self.worker()
        self.herdr_state.write_text(json.dumps({"agents": {}}))
        code, event = self.cli("--for", "A-1:build-1")
        self.assertEqual((code, event["event"], event["agent"]), (0, "gone", "b-a1-1"))

    def test_the_herdr_binary_reporting_a_dialog_ends_the_wait(self):
        self.worker()
        self.herdr_state.write_text(json.dumps({"agents": {"b-a1-1": info("blocked", seq=2)}}))
        code, event = self.cli("--for", "A-1:build-1")
        self.assertEqual((code, event["event"], event["pane"]), (0, "blocked", "w9:p1"))
        self.assertEqual(json.loads((self.run_dir / "wait-seen.json").read_text()), {"A-1:build-1:blocked": "seq@2"})

    def test_a_bad_watch_exits_2(self):
        code, event = self.cli("--for", "A-1:build-*")
        self.assertEqual((code, event["event"]), (2, "refused"))

    def test_the_real_wrapper_reads_an_agent_and_a_missing_one(self):
        self.herdr_state.write_text(json.dumps({"agents": {"b-a1-1": info("idle")}}))
        client = herdr.Herdr(str(self.fake_herdr))
        self.assertEqual(client.agent("b-a1-1")["agent_status"], "idle")
        self.assertIsNone(client.agent("nobody"))
        with self.assertRaises(HerdrError) as caught:
            client.call("pane", "list")
        self.assertEqual(caught.exception.code, "exit_64")


class TranscriptLookupTest(WaitCase):
    def test_a_transcript_is_found_by_session_id_whatever_its_directory(self):
        path = self.transcript(age_s=0)
        self.assertEqual(herdr.claude_transcript(SESSION), Path(path))
        self.assertIsNone(herdr.claude_transcript("00000000-0000-0000-0000-000000000000"))

    def write_in(self, home, session=SESSION):
        path = Path(home) / "projects" / "-a-worktree" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
        return path

    def test_a_transcript_on_another_sign_in_beside_the_default_is_found(self):
        path = self.write_in(self.home / ".claude-team")
        self.assertEqual(herdr.claude_transcript(SESSION), path)
        # The default is still read, for sessions on either side of a program's move.
        other = "11111111-1111-1111-1111-111111111111"
        self.assertEqual(herdr.claude_transcript(other), None)
        self.assertEqual(herdr.claude_transcript(SESSION), path)

    def test_a_transcript_in_the_directory_this_process_was_started_with_is_found(self):
        elsewhere = self.tmp / "elsewhere"
        path = self.write_in(elsewhere)
        self.assertIsNone(herdr.claude_transcript(SESSION))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(elsewhere)}):
            self.assertEqual(herdr.claude_transcript(SESSION), path)
            self.assertEqual(herdr.claude_homes()[:2], [elsewhere, self.home / ".claude"])

    def test_a_sub_agents_transcript_is_found_beside_its_session_on_another_sign_in(self):
        main = self.write_in(self.home / ".claude-team")
        sub = main.parent / SESSION / "subagents" / "agent-a1.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text("{}\n")
        self.assertEqual(herdr.claude_transcripts(SESSION), [main, sub])


class SeatWaitTest(WaitCase):
    """`seat`: the program moved off this manager's Claude seat, or every seat is full and it pauses."""

    def setUp(self):
        super().setUp()
        self.loop = self.tmp / "loop"
        self.loop.mkdir()
        self.a, self.b = str(self.home / ".claude-a"), str(self.home / ".claude-b")
        settings = {
            "program": "canary",
            "base": "main",
            "worker_claude_ref": "-",
            "gh_token_ref": "op://Loop/gh/password",
            "cursor_model": "composer",
            "claude_config_dir": self.a,
            "claude_seats": [self.a, self.b],
        }
        write_json(self.loop / "program.json", settings)
        self.worker()
        self.transcript(age_s=0)

    def wait_as(self, own, *extra):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": own}):
            return self.wait(ScriptedHerdr(info("working")), ("A-1:build-1",), (), None, "--root", str(self.loop), "--max-min", "1", *extra)

    def test_a_manager_on_the_programs_seat_waits_as_before(self):
        self.assertEqual(self.wait_as(self.a)["event"], "time")

    def test_a_manager_on_the_seat_the_program_left_is_woken_at_once_and_again_on_every_wait(self):
        write_json(self.loop / "seat.json", {"home": self.b, "why": "a@example.invalid is full"})
        for _ in range(2):
            event = self.wait_as(self.a)
            self.assertEqual((event["event"], event["action"], event["from"], event["to"]), ("seat", "move", self.a, self.b))
            self.assertEqual(event["waited_s"], 0)
        self.assertEqual(self.wait_as(self.b)["event"], "time")

    def test_a_pause_wakes_a_manager_on_any_seat(self):
        write_json(self.loop / "seat.json", {"paused_until": 1791849600, "why": "every seat is full"})
        for own in (self.a, self.b):
            event = self.wait_as(own)
            self.assertEqual((event["event"], event["action"], event["until"]), ("seat", "pause", "2026-10-13T00:00:00Z"))

    def test_a_pause_that_comes_while_waiting_ends_the_wait(self):
        self.clock.on_sleep = [lambda: write_json(self.loop / "seat.json", {"paused_until": 1791849600})] + [lambda: None] * 10
        event = self.wait_as(self.a)
        self.assertEqual((event["event"], event["action"]), ("seat", "pause"))
        self.assertGreater(event["waited_s"], 0)

    def test_without_the_root_the_seats_are_not_looked_at(self):
        write_json(self.loop / "seat.json", {"paused_until": 1791849600})
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.a}):
            event = self.wait(ScriptedHerdr(info("working")), ("A-1:build-1",), (), None, "--max-min", "1")
        self.assertEqual(event["event"], "time")
