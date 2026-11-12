from __future__ import annotations

import base64
import contextlib
import http.server
import io
import json
import os
import threading
import time
from datetime import datetime, timezone
from unittest import mock

from hl import claim, watchdog
from hl.common import Refusal, write_json
from hl.herdr import HerdrError

from .support import SpawnCase

SESSION = "99999999-2222-3333-4444-555555555555"
MANAGER_PID = 300


class FakeProcs:
    def __init__(self):
        self.table = {}

    def start(self, pid):
        return self.table.get(pid)

    def parent(self, pid):
        return None


class FakeSystem:
    def __init__(self):
        self.t = time.time()
        self.merging = False
        self.sent = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def merge_running(self, root):
        return self.merging

    def notify(self, cfg, title, body):
        self.sent.append((title, body))
        return True


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WatchdogCase(SpawnCase):
    def setUp(self):
        super().setUp()
        self.system = FakeSystem()
        self.procs = FakeProcs()
        (self.root / "MANAGER-PROMPT.md").write_text("You are the manager.\n")
        write_json(self.root / "pins.json", {"bins": {}, "history": []})
        watchdog.set_armed(self.root, True, "test")
        home = self.tmp / "home"
        (home / ".claude" / "projects" / "-root").mkdir(parents=True)
        self.home = home
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tick(self):
        return watchdog.tick(self.root, self.herdr, self.system, self.procs)

    def hold(self, claimed_min_ago, beat_min_ago=None, alive=True, pane="w1:p9", parent_pid=77):
        """A manager holding the program, in a pane of the operator's own
        workspace: its claim, its heartbeat, and whether its process lives."""
        now = self.system.t
        record = {"pid": MANAGER_PID, "start": "boot", "pane": pane, "claimed_at": stamp(now - claimed_min_ago * 60)}
        record["parent_pid"] = parent_pid
        if not any(w["workspace_id"] == pane.split(":")[0] for w in self.herdr.spaces):
            self.herdr.open_space(pane.split(":")[0], "New-Looprunner")
        write_json(self.root / "manager.lock.d" / "claim.json", record)
        if beat_min_ago is not None:
            write_json(self.root / "heartbeat.json", {"epoch": int(now - beat_min_ago * 60), "pid": MANAGER_PID})
        if alive:
            self.procs.table[MANAGER_PID] = "boot"
        running = [{"argv0": "claude", "pid": MANAGER_PID}] if alive else []
        self.herdr.panes[pane] = {"shell_pid": 77, "processes": running}

    def state(self):
        return watchdog.load_state(self.root)


class TickTest(WatchdogCase):
    def test_a_disarmed_watchdog_does_nothing(self):
        watchdog.set_armed(self.root, False, "paused")
        self.assertEqual(self.tick()["action"], "disarmed")
        self.assertEqual(self.herdr.calls, [])

    def test_a_manager_that_does_not_take_its_prompt_is_a_failed_start(self):
        self.herdr.fates = ["stuck"]
        result = self.tick()
        self.assertEqual(result["action"], "start-failed")
        self.assertIn("did not take its prompt", result["detail"])
        self.assertEqual((self.herdr.panes, len(self.state()["starts"]), self.system.sent), ({}, 1, []))

    def test_with_nobody_holding_the_program_a_manager_is_started_in_the_loop_root(self):
        result = self.tick()
        self.assertEqual((result["action"], result["agent"]), ("started", "canary-mgr-1"))
        # No workspace of the manager's is left, so one named for the program is opened and the manager takes its first pane.
        made = self.herdr.called("workspace", "create")[0]
        self.assertEqual((made[made.index("--label") + 1], made[made.index("--cwd") + 1]), ("canary", str(self.root)))
        self.assertEqual(self.herdr.called("tab", "create"), [])
        self.assertEqual(list(self.herdr.panes), [result["pane"]])
        start = self.herdr.called("agent", "start")[0]
        argv = list(start[start.index("--") + 1 :])
        self.assertEqual(argv[:5], ["--dangerously-skip-permissions", "--model", "claude-opus-5-5", "--effort", "high"])
        self.assertEqual(argv[5], "--session-id")
        prompt = self.herdr.called("agent", "prompt")[0][3]
        self.assertTrue(prompt.startswith(f"Read {self.root}/MANAGER-PROMPT.md and follow it."))
        self.assertLessEqual(len(prompt), 500)
        self.assertEqual(self.system.sent[0][0], "canary: manager started")
        self.assertEqual(self.state()["manager"]["session"], argv[6])

    def test_the_programs_own_arguments_are_added_to_the_managers_launch_line(self):
        from hl.common import read_json

        settings = read_json(self.root / "program.json")
        args = ["--tools", "Bash,Read,Edit,Write,Agent,TaskStop", "--disable-slash-commands", "--strict-mcp-config", "--mcp-config", "{{KIT}}/references/mcp-none.json"]
        write_json(self.root / "program.json", {**settings, "manager_args": args})
        self.tick()
        start = self.herdr.called("agent", "start")[0]
        argv = list(start[start.index("--") + 1 :])
        # The operator's own line first, with the model and effort the program names; then what the program adds.
        self.assertEqual(argv[:5], ["--dangerously-skip-permissions", "--model", "claude-opus-5-5", "--effort", "high"])
        self.assertEqual(argv[7:11], ["--tools", "Bash,Read,Edit,Write,Agent,TaskStop", "--disable-slash-commands", "--strict-mcp-config"])
        kit = (self.root / ".claude" / "skills" / "herdr-loop").resolve()
        self.assertEqual(argv[11:], ["--mcp-config", f"{kit}/references/mcp-none.json"])

    def test_a_manager_that_saved_recently_is_left_alone(self):
        self.hold(claimed_min_ago=300, beat_min_ago=10)
        self.assertEqual(self.tick(), {"action": "healthy", "pid": MANAGER_PID, "quiet_min": 10})
        self.assertEqual(self.herdr.calls, [])

    def test_a_manager_that_has_only_just_claimed_is_not_yet_stale(self):
        self.hold(claimed_min_ago=5)
        self.assertEqual(self.tick()["action"], "healthy")

    def test_an_old_heartbeat_from_the_last_manager_does_not_condemn_a_new_one(self):
        self.hold(claimed_min_ago=5, beat_min_ago=200)
        self.assertEqual(self.tick()["action"], "healthy")

    def test_a_dead_manager_is_replaced(self):
        self.hold(claimed_min_ago=30, beat_min_ago=3, alive=False)
        result = self.tick()
        self.assertEqual(result["action"], "started")
        self.assertIn(f"the manager (pid {MANAGER_PID}) is gone", result["reason"])
        self.assertIsNone(result["closed"])
        self.assertEqual(self.herdr.called("pane", "close"), [])

    def test_a_manager_alive_but_silent_for_too_long_is_closed_and_replaced(self):
        self.hold(claimed_min_ago=300, beat_min_ago=50)
        result = self.tick()
        self.assertEqual((result["action"], result["closed"]), ("started", "closed pane w1:p9"))
        self.assertIn("saved nothing for 50 minutes", result["reason"])
        order = [c[:2] for c in self.herdr.calls]
        self.assertLess(order.index(("pane", "close")), order.index(("agent", "start")))
        # The new tab is opened first, so the workspace the two share is never empty.
        self.assertLess(order.index(("tab", "create")), order.index(("pane", "close")))
        self.assertEqual(result["pane"].split(":")[0], "w1")
        self.assertEqual(self.herdr.labels(), ["New-Looprunner"])

    def test_a_silent_manager_is_not_touched_while_a_merge_runs(self):
        self.hold(claimed_min_ago=300, beat_min_ago=50)
        self.system.merging = True
        self.assertEqual(self.tick()["action"], "merge-running")
        self.assertEqual(self.herdr.calls, [])

    def test_a_pane_that_no_longer_runs_the_manager_is_not_closed(self):
        self.hold(claimed_min_ago=300, beat_min_ago=50)
        self.herdr.panes["w1:p9"]["processes"] = [{"argv0": "vim", "pid": 4}]
        result = self.tick()
        self.assertIn("left alone", result["closed"])
        self.assertEqual(self.herdr.called("pane", "close"), [])
        self.assertEqual(result["action"], "started")

    def test_a_manager_stopped_on_a_usage_limit_is_waited_for_and_reported_once(self):
        self.hold(claimed_min_ago=300, beat_min_ago=50)
        state = self.state()
        state["manager"] = {"session": SESSION}
        write_json(self.root / "watchdog.json", state)
        record = {
            "type": "assistant",
            "uuid": "limit-1",
            "error": "rate_limit",
            "isApiErrorMessage": True,
            "message": {"content": [{"type": "text", "text": "You've hit your weekly limit"}]},
            "quotaLimits": {"resetsAt": int(self.system.t) + 3600, "rateLimitType": "seven_day"},
        }
        (self.home / ".claude" / "projects" / "-root" / f"{SESSION}.jsonl").write_text(json.dumps(record) + "\n")
        self.assertEqual(self.tick()["action"], "limit")
        self.assertEqual(self.tick()["action"], "limit")
        self.assertEqual([title for title, _ in self.system.sent], ["canary: manager is on a usage limit"])
        self.assertEqual(self.herdr.called("agent", "start"), [])
        # Once the limit has reset, a manager that is still silent is replaced.
        self.system.t += 2 * 3600
        self.hold(claimed_min_ago=420, beat_min_ago=170)
        self.assertEqual(self.tick()["action"], "started")

    def test_three_starts_within_an_hour_halt_the_watchdog_until_it_is_armed_again(self):
        for _ in range(3):
            self.assertEqual(self.tick()["action"], "started")
            self.system.t += 300
        halted = self.tick()
        self.assertEqual(halted["action"], "halted")
        self.assertIn("3 managers were started within an hour", halted["reason"])
        self.assertEqual(self.system.sent[-1][0], "canary: watchdog halted")
        self.assertEqual(self.tick()["action"], "halted")
        self.assertEqual(len(self.herdr.called("agent", "start")), 3)
        watchdog.set_armed(self.root, True, "looked: the account was signed out")
        self.assertEqual(self.tick()["action"], "started")

    def test_starts_spread_over_hours_do_not_halt(self):
        for _ in range(5):
            self.assertEqual(self.tick()["action"], "started")
            self.system.t += 1900
        self.assertIsNone(self.state()["halted"])

    def test_a_start_that_fails_closes_its_pane_and_counts(self):
        self.herdr.fail[("agent", "start")] = [HerdrError("exit_1", "claude: command not found")] * 3
        for _ in range(3):
            result = self.tick()
            self.assertEqual(result["action"], "start-failed")
        self.assertEqual(self.herdr.panes, {})
        self.assertEqual(self.tick()["action"], "halted")

    def test_the_pane_of_the_last_manager_it_started_is_closed_before_the_next(self):
        first = self.tick()
        self.system.t += 1900
        second = self.tick()
        self.assertNotIn(first["pane"], self.herdr.panes)
        self.assertIn(second["pane"], self.herdr.panes)
        self.assertEqual(second["agent"], "canary-mgr-2")
        # The second is a tab in the first one's workspace: one workspace throughout.
        self.assertEqual(second["pane"].split(":")[0], first["pane"].split(":")[0])
        self.assertEqual((self.herdr.labels(), len(self.herdr.called("workspace", "create"))), (["canary"], 1))


class HandOverTest(WatchdogCase):
    """A manager that releases its claim at an item boundary asked to be
    replaced. That is not one of the hour's three starts, and it has a brake
    of its own."""

    def setUp(self):
        super().setUp()
        # One clock for the claims log, the watchdog's record and the tick.
        for module in ("hl.claim.utc_now", "hl.watchdog.utc_now"):
            patcher = mock.patch(module, side_effect=lambda: stamp(self.system.t))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pid = 500
        self.moved = 0

    def work_and_hand_over(self, moved=True, minutes=10, note="recycling at an item boundary"):
        """The manager that was started claims the program, works for a while
        (a worker's done file appears, unless `moved` is false), and releases it."""
        self.pid += 1
        self.procs.table[self.pid] = "boot"
        self.system.t += 60
        claim.claim(self.root, self.pid, None, SESSION, self.procs)
        self.system.t += minutes * 60
        if moved:
            self.moved += 1
            self.write("A-1", f"agree-{self.moved + 1}.done", "")
        claim.release(self.root, self.pid, self.procs, note)
        del self.procs.table[self.pid]
        self.system.t += 60

    def test_four_planned_hand_overs_in_an_hour_do_not_halt(self):
        self.assertEqual(self.tick()["action"], "started")
        for _ in range(4):
            self.work_and_hand_over()
            result = self.tick()
            self.assertEqual((result["action"], result["handover"]), ("started", True))
        state = self.state()
        self.assertIsNone(state["halted"])
        # Five managers inside the hour: one start, four hand-overs.
        self.assertEqual((len(state["starts"]), len(state["handovers"]), len(self.herdr.called("agent", "start"))), (1, 4, 5))
        self.assertLess(self.system.t - state["starts"][0], watchdog.WINDOW_S)

    def test_the_manager_is_told_it_took_over_and_the_operator_that_it_was_planned(self):
        self.tick()
        self.work_and_hand_over(note="context at 210k tokens, between items")
        result = self.tick()
        self.assertEqual(result["reason"], "the last manager handed over (context at 210k tokens, between items)")
        self.assertIn("the last manager handed over", self.herdr.called("agent", "prompt")[-1][3])
        self.assertEqual([title for title, _ in self.system.sent], ["canary: manager started", "canary: manager handed over"])

    def test_a_fourth_crash_restart_in_an_hour_is_still_refused_with_hand_overs_between(self):
        self.assertEqual(self.tick()["action"], "started")
        self.work_and_hand_over()
        self.assertEqual(self.tick()["action"], "started")
        for _ in range(2):
            # The manager that was started dies without a word: no claim, no release.
            self.system.t += 300
            result = self.tick()
            self.assertEqual((result["action"], result["handover"]), ("started", False))
        self.work_and_hand_over()
        halted = self.tick()
        self.assertEqual(halted["action"], "halted")
        self.assertIn("3 managers were started within an hour", halted["reason"])

    def test_a_manager_that_was_started_and_never_claimed_is_not_a_hand_over(self):
        self.tick()
        self.work_and_hand_over()
        self.assertTrue(self.tick()["handover"])
        # That manager never claims. The log's last line is still the older release.
        self.system.t += 300
        result = self.tick()
        self.assertEqual((result["action"], result["handover"], result["reason"]), ("started", False, "no manager holds the program"))
        self.assertEqual(len(self.state()["starts"]), 2)

    def test_a_dead_manager_is_a_crash_whatever_the_log_says_last(self):
        self.tick()
        self.work_and_hand_over()
        # The next manager took the claim and died before the log learned of it.
        self.hold(claimed_min_ago=1, alive=False)
        result = self.tick()
        self.assertEqual((result["action"], result["handover"]), ("started", False))
        self.assertIn("is gone", result["reason"])
        self.assertEqual(len(self.state()["starts"]), 2)

    def test_managers_that_keep_handing_over_with_nothing_moved_halt_the_watchdog(self):
        self.tick()
        self.work_and_hand_over()
        for _ in range(3):
            self.assertEqual(self.tick()["action"], "started")
            self.work_and_hand_over(moved=False)
        halted = self.tick()
        self.assertEqual(halted["action"], "halted")
        self.assertIn("3 managers in a row handed over and nothing in the run directories moved", halted["reason"])
        self.assertEqual(self.system.sent[-1][0], "canary: watchdog halted")
        started = len(self.herdr.called("agent", "start"))
        self.assertEqual(self.tick()["action"], "halted")
        self.assertEqual(len(self.herdr.called("agent", "start")), started)
        watchdog.set_armed(self.root, True, "looked: its prompt pointed at a missing file")
        self.assertEqual(self.tick()["action"], "started")

    def test_one_idle_hand_over_between_working_ones_does_not_count_up(self):
        self.tick()
        for moved in (True, False, True, False, True, False, True):
            self.work_and_hand_over(moved=moved, minutes=30)
            self.assertEqual(self.tick()["action"], "started")
        self.assertIsNone(self.state()["halted"])

    def test_a_hand_over_start_that_fails_counts_against_the_hour(self):
        self.tick()
        self.work_and_hand_over()
        self.herdr.fail[("agent", "start")] = [HerdrError("exit_1", "claude: command not found")] * 2
        for _ in range(2):
            self.assertEqual(self.tick()["action"], "start-failed")
        self.assertEqual(self.tick()["action"], "halted")

    def test_managers_whose_every_worker_dies_at_once_are_not_making_progress(self):
        # Found in review: each manager starts a builder, the builder dies (a quota, a sign-in), the manager voids
        # the attempt and hands over. The attempt log grows every time and nothing was done.
        from hl import items

        self.tick()
        self.work_and_hand_over()
        for k in range(3):
            self.assertEqual(self.tick()["action"], "started", k)
            items.start_phase(self.run_dir, "A-1", "build", session=f"dead-{k}", engine="claude")
            items.void_attempt(self.run_dir, "A-1", "build", None, "the worker hit a usage limit at once")
            self.work_and_hand_over(moved=False)
        self.assertEqual(self.tick()["action"], "halted")

    def test_an_attempt_that_was_voided_does_not_move_the_mark_and_one_that_counts_does(self):
        from hl import items

        before = watchdog.progress_mark(self.root)
        items.start_phase(self.run_dir, "A-1", "build", session="b-1", engine="claude")
        started = watchdog.progress_mark(self.root)
        self.assertNotEqual(started, before)
        items.void_attempt(self.run_dir, "A-1", "build", None, "pane went away")
        self.assertEqual(watchdog.progress_mark(self.root), before)

    def test_the_progress_mark_moves_with_a_status_a_phase_a_done_file_and_a_deploy_check(self):
        marks = [watchdog.progress_mark(self.root)]
        self.assertEqual(marks[0], watchdog.progress_mark(self.root))
        self.write("A-1", "agree-1.done", "")
        marks.append(watchdog.progress_mark(self.root))
        self.build()
        marks.append(watchdog.progress_mark(self.root))
        self.ship("A-1", verify=None)
        marks.append(watchdog.progress_mark(self.root))
        self.ship("A-1")
        marks.append(watchdog.progress_mark(self.root))
        self.assertEqual(len(set(marks)), 5)


class PlacementTest(WatchdogCase):
    """The manager is one tab of a workspace, and its replacement stays there."""

    def test_a_dead_managers_replacement_is_a_tab_in_the_workspace_it_ran_in(self):
        self.hold(claimed_min_ago=30, beat_min_ago=3, alive=False)
        result = self.tick()
        self.assertEqual((result["action"], result["pane"].split(":")[0]), ("started", "w1"))
        tab = self.herdr.called("tab", "create")[0]
        self.assertEqual((tab[tab.index("--label") + 1], tab[tab.index("--cwd") + 1]), ("canary manager", str(self.root)))
        self.assertEqual(self.herdr.called("workspace", "create"), [])
        # The dead manager's pane is the operator's and is left as it is.
        self.assertIn("w1:p9", self.herdr.panes)

    def test_a_manager_that_recycled_itself_is_replaced_in_the_workspace_it_ran_in(self):
        # It claimed from the operator's pane, saved, and released its claim before it exited.
        self.procs.table[MANAGER_PID] = "boot"
        self.procs.parent = lambda pid: (77, "zsh")
        self.herdr.open_space("w1", "New-Looprunner")
        self.herdr.panes["w1:p9"] = {"shell_pid": 77, "processes": []}
        claim.claim(self.root, MANAGER_PID, "w1:p9", SESSION, self.procs)
        claim.release(self.root, MANAGER_PID, self.procs, "recycling at an item boundary")
        del self.procs.table[MANAGER_PID]
        result = self.tick()
        self.assertEqual((result["action"], result["reason"]), ("started", "the last manager handed over (recycling at an item boundary)"))
        self.assertEqual((result["pane"].split(":")[0], self.herdr.labels()), ("w1", ["New-Looprunner"]))
        tab = self.herdr.called("tab", "create")[0]
        self.assertEqual(tab[tab.index("--label") + 1], "canary manager")

    def test_a_released_claim_whose_pane_is_gone_places_the_manager_by_the_programs_name(self):
        self.procs.table[MANAGER_PID] = "boot"
        self.procs.parent = lambda pid: (77, "zsh")
        claim.claim(self.root, MANAGER_PID, "w1:p9", SESSION, self.procs)
        claim.release(self.root, MANAGER_PID, self.procs, "recycling")
        self.tick()
        self.assertEqual(self.herdr.labels(), ["canary"])

    def test_a_stuck_manager_whose_claim_names_no_shell_is_still_found_by_its_process(self):
        # A claim written before the shell was recorded: the pane is the manager's because it runs the manager's pid.
        self.hold(claimed_min_ago=300, beat_min_ago=50, parent_pid=None)
        self.herdr.panes["w1:p9"]["shell_pid"] = 4040
        result = self.tick()
        self.assertEqual((result["action"], result["pane"].split(":")[0]), ("started", "w1"))
        self.assertEqual(self.herdr.labels(), ["New-Looprunner"])

    def test_a_pane_id_that_now_holds_another_shell_does_not_place_the_manager(self):
        self.hold(claimed_min_ago=30, beat_min_ago=3, alive=False)
        self.herdr.panes["w1:p9"]["shell_pid"] = 99999
        result = self.tick()
        self.assertEqual(self.herdr.labels(), ["New-Looprunner", "canary"])
        self.assertNotEqual(result["pane"].split(":")[0], "w1")

    def test_a_claim_without_its_shell_places_a_dead_managers_replacement_by_the_programs_name(self):
        self.hold(claimed_min_ago=30, beat_min_ago=3, alive=False, parent_pid=None)
        self.tick()
        self.assertEqual(self.herdr.labels(), ["New-Looprunner", "canary"])

    def test_a_workspace_already_named_for_the_program_takes_the_manager_as_a_tab(self):
        self.herdr.open_space("w5", "canary")
        self.herdr.panes["w5:p90"] = {"shell_pid": 12, "processes": []}
        result = self.tick()
        self.assertEqual((result["pane"].split(":")[0], self.herdr.called("workspace", "create")), ("w5", []))

    def test_a_pane_id_that_does_not_carry_its_workspace_falls_back_to_the_programs_name(self):
        now = self.system.t
        write_json(self.root / "manager.lock.d" / "claim.json", {"pid": MANAGER_PID, "start": "boot", "pane": "pane-9", "parent_pid": 77, "claimed_at": stamp(now - 1800)})
        self.herdr.panes["pane-9"] = {"shell_pid": 77, "processes": []}
        result = self.tick()
        self.assertEqual((result["action"], self.herdr.labels()), ("started", ["canary"]))

    def test_a_workspace_that_closed_under_the_manager_is_not_reused_by_id(self):
        self.hold(claimed_min_ago=30, beat_min_ago=3, alive=False)
        self.herdr.spaces.clear()
        del self.herdr.panes["w1:p9"]
        result = self.tick()
        self.assertEqual((result["action"], self.herdr.labels()), ("started", ["canary"]))

    def test_a_start_that_fails_after_the_stuck_manager_was_closed_leaves_no_pane_of_its_own(self):
        self.hold(claimed_min_ago=300, beat_min_ago=50)
        self.herdr.fail[("agent", "start")] = [HerdrError("exit_1", "claude: command not found")]
        result = self.tick()
        self.assertEqual((result["action"], result["closed"]), ("start-failed", "closed pane w1:p9"))
        self.assertEqual(self.herdr.panes, {})


class StartTest(WatchdogCase):
    """`start` is how a program's first manager comes up: no tick, no arming, nothing pinned yet."""

    def start(self, note="first start"):
        return watchdog.start_now(self.root, self.herdr, self.system, note, self.procs)

    def test_the_first_manager_is_started_in_a_workspace_named_for_the_program_and_told_why(self):
        watchdog.set_armed(self.root, False, "not armed yet")
        result = self.start()
        self.assertEqual((result["action"], result["agent"], self.herdr.labels()), ("started", "canary-mgr-1", ["canary"]))
        prompt = self.herdr.called("agent", "prompt")[0][3]
        self.assertEqual(prompt, f"Read {self.root}/MANAGER-PROMPT.md and follow it. You were started by hl-watchdog: first start.")
        self.assertEqual(self.state()["manager"]["pane"], result["pane"])
        self.assertFalse(self.state()["armed"])

    def test_nothing_is_started_beside_a_live_manager(self):
        self.hold(claimed_min_ago=5, beat_min_ago=1)
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn(f"a manager already holds this program (pid {MANAGER_PID}, pane w1:p9)", str(caught.exception))
        self.assertEqual(self.herdr.called("agent", "start"), [])

    def test_a_program_that_was_not_staged_is_not_started(self):
        (self.root / "MANAGER-PROMPT.md").unlink()
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("stage the program first", str(caught.exception))
        self.assertEqual(self.herdr.calls, [])

    def test_a_start_that_fails_closes_the_pane_it_opened(self):
        self.herdr.fail[("agent", "start")] = [HerdrError("exit_1", "claude: command not found")]
        with self.assertRaises(HerdrError):
            self.start()
        self.assertEqual((self.herdr.panes, self.herdr.labels()), ({}, []))

    def test_the_folder_trust_dialog_of_a_new_root_is_answered(self):
        self.herdr.trust_dialog = True
        self.herdr.screen = " Quick safety check\n ❯ No, exit\n   Yes, I trust this folder\n"
        self.assertEqual(self.start()["action"], "started")
        self.assertEqual(self.herdr.keys, [("down", "enter")])

    def test_in_a_new_root_the_managers_prompt_waits_for_the_input_box(self):
        # 2026-10-12: the first manager of a new root sat with its prompt unsent until Enter was pressed by hand.
        self.herdr.trust_dialog = True
        self.herdr.screen = " Quick safety check\n \u276f No, exit\n   Yes, I trust this folder\n"
        self.herdr.box_at = 5
        result = self.start()
        prompt = f"Read {self.root}/MANAGER-PROMPT.md and follow it. You were started by hl-watchdog: first start."
        self.assertEqual(self.herdr.taken, {result["agent"]: [prompt]})

    def test_a_manager_that_does_not_take_its_prompt_is_closed_and_the_start_refused(self):
        self.herdr.fates = ["stuck"]
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("canary-mgr-1 was started and did not take its prompt", str(caught.exception))
        self.assertEqual((self.herdr.panes, self.herdr.labels()), ({}, []))
        self.assertIsNone(self.state()["manager"])

    def test_the_command_line_starts_a_manager_with_the_herdr_it_is_given_before_anything_is_pinned(self):
        (self.root / "pins.json").unlink()
        out = io.StringIO()
        with mock.patch("hl.watchdog.Herdr", return_value=self.herdr) as made, mock.patch("hl.watchdog.claim.Procs", return_value=self.procs):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = watchdog.main(["--root", str(self.root), "--herdr", "/opt/bin/herdr", "start", "--note", "first start"])
        made.assert_called_once_with("/opt/bin/herdr")
        self.assertEqual((code, json.loads(out.getvalue())["action"]), (0, "started"))


class LockTest(WatchdogCase):
    def test_a_tick_does_not_run_beside_another(self):
        (self.root / "watchdog.lock.d").mkdir()
        self.assertEqual(watchdog._tick_locked(self.root, self.herdr, self.system), {"action": "another-tick-running"})
        self.assertEqual(self.herdr.calls, [])

    def test_a_lock_left_by_a_dead_tick_is_taken_over(self):
        lock = self.root / "watchdog.lock.d"
        lock.mkdir()
        old = self.system.t - watchdog.TICK_LOCK_STALE_S - 60
        os.utime(lock, (old, old))
        with mock.patch("hl.watchdog.claim.Procs", return_value=self.procs):
            self.assertEqual(watchdog._tick_locked(self.root, self.herdr, self.system)["action"], "started")
        self.assertFalse(lock.exists())


class NotifyTest(WatchdogCase):
    def test_a_notice_goes_out_with_basic_auth_and_the_secret_is_read_not_stored(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["auth"] = self.headers["Authorization"]
                seen["title"] = self.headers["Title"]
                seen["body"] = self.rfile.read(int(self.headers["Content-Length"])).decode()
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.handle_request, daemon=True).start()
        cfg = {"ntfy_url": f"http://127.0.0.1:{server.server_port}/topic", "ntfy_user": "loop-notices", "ntfy_password_ref": "op://x/y/z"}
        system = watchdog.System()
        with mock.patch.object(watchdog.System, "secret", return_value=("s3cret", None)) as read:
            self.assertTrue(system.notify(cfg, "canary: manager started", "because"))
        # No `secrets_reader`: the kit's own reader.
        read.assert_called_once_with("op://x/y/z", None)
        self.assertEqual(seen["auth"], "Basic " + base64.b64encode(b"loop-notices:s3cret").decode())
        self.assertEqual((seen["title"], seen["body"]), ("canary: manager started", "because"))

    def test_no_channel_or_no_secret_means_no_notice_and_no_error(self):
        system = watchdog.System()
        self.assertFalse(system.notify({"ntfy_url": None}, "t", "b"))
        with mock.patch.object(watchdog.System, "secret", return_value=(None, "hl-read-secret: unknown reference scheme")):
            self.assertFalse(system.notify({"ntfy_url": "http://127.0.0.1:9/x", "ntfy_user": "u", "ntfy_password_ref": "r"}, "t", "b"))

    def test_the_secret_is_read_with_the_programs_reader_or_the_kits(self):
        with mock.patch("hl.watchdog.keeper.read_secret", return_value=("v", None)) as read:
            self.assertEqual(watchdog.System().secret("file:/x"), ("v", None))
            self.assertEqual(watchdog.System().secret("file:/x", ["my-reader", "--quiet"]), ("v", None))
        self.assertEqual(read.call_args_list, [mock.call("file:/x", None), mock.call("file:/x", ["my-reader", "--quiet"])])


class CliTest(WatchdogCase):
    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = watchdog.main(["--root", str(self.root), *argv])
        return code, json.loads(out.getvalue())

    def test_arm_disarm_and_status(self):
        self.assertEqual(self.cli("disarm", "--note", "program complete"), (0, {"armed": False}))
        code, status = self.cli("status")
        self.assertEqual((code, status["watchdog"]["armed"], status["program"]["held"]), (0, False, False))
        self.assertEqual(self.cli("arm", "--note", "go")[1], {"armed": True})
        log = (self.root / "watchdog.log").read_text()
        self.assertIn("disarmed: program complete", log)

    def test_arming_before_the_binaries_are_pinned_is_refused(self):
        (self.root / "pins.json").unlink()
        code, out = self.cli("arm", "--note", "go")
        self.assertEqual(code, 2)
        self.assertIn("run `hl-claim pin` first", out["refused"][0])

    def test_a_disarmed_tick_is_silent_and_needs_nothing(self):
        self.cli("disarm", "--note", "paused")
        (self.root / "pins.json").unlink()
        self.assertEqual(self.cli("tick"), (0, {"action": "disarmed"}))

    def test_a_tick_accepts_an_updated_binary_and_logs_it(self):
        checked = {"ok": True, "bins": {"herdr": "/bin/herdr", "claude": "/bin/claude"},
                   "updated": [{"name": "claude", "why": "content changed", "version": "2.1.307 (Claude Code)"}]}
        with mock.patch.object(watchdog.claim, "check_pins", return_value=checked), \
                mock.patch.object(watchdog, "_tick_locked", return_value={"action": "healthy"}):
            self.assertEqual(self.cli("tick"), (0, {"action": "healthy"}))
        log = (self.root / "watchdog.log").read_text()
        self.assertIn("accepted an update of claude: content changed (2.1.307 (Claude Code))", log)
        self.assertNotIn("refused", log)

    def test_a_root_without_its_settings_is_refused(self):
        (self.root / "program.json").unlink()
        code, out = self.cli("arm", "--note", "go")
        self.assertEqual(code, 2)
        self.assertIn("program.json does not exist", out["refused"][0])


class ClaudeHomeTest(WatchdogCase):
    """A program whose sessions run on another Claude sign-in (`claude_config_dir`)."""

    def setUp(self):
        super().setUp()
        from hl.common import read_json

        self.team = self.home / ".claude-team"
        self.team.mkdir()
        write_json(self.team / ".claude.json", {"oauthAccount": {"emailAddress": "loops@example.invalid"}})
        write_json(self.root / "program.json", {**read_json(self.root / "program.json"), "claude_config_dir": str(self.team)})

    def test_the_manager_is_started_on_the_programs_sign_in(self):
        result = self.tick()
        self.assertEqual(result["action"], "started")
        ran = self.herdr.panes[result["pane"]]["ran"]
        self.assertEqual(len(ran), 1)
        self.assertIn(f"export CLAUDE_CONFIG_DIR={self.team};", ran[0])
        order = [c[:2] for c in self.herdr.calls]
        self.assertLess(order.index(("pane", "run")), order.index(("agent", "start")))

    def test_a_sign_in_that_does_not_load_starts_no_manager(self):
        self.herdr.home_say = f"hl-home FAILED {{nonce}}: no Claude sign-in in {self.team}"
        result = self.tick()
        self.assertEqual(result["action"], "start-failed")
        self.assertIn("did not load", result["detail"])
        self.assertEqual((self.herdr.called("agent", "start"), self.herdr.panes), ([], {}))

    def test_a_program_without_the_setting_types_nothing_before_its_manager(self):
        from hl.common import read_json

        settings = read_json(self.root / "program.json")
        settings.pop("claude_config_dir")
        write_json(self.root / "program.json", settings)
        result = self.tick()
        self.assertEqual(result["action"], "started")
        self.assertNotIn("ran", self.herdr.panes[result["pane"]])


class SeatTest(WatchdogCase):
    """A program with two Claude seats: it moves off a full one, pauses while
    both are full, and resumes on the first that resets (`seat.py`)."""

    def setUp(self):
        super().setUp()
        from hl.common import read_json

        for module in ("hl.claim.utc_now", "hl.watchdog.utc_now"):
            patcher = mock.patch(module, side_effect=lambda: stamp(self.system.t))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.a, self.b = str(self.home / ".claude-a"), str(self.home / ".claude-b")
        for home in (self.a, self.b):
            os.mkdir(home)
            write_json(os.path.join(home, ".claude.json"), {"oauthAccount": {"emailAddress": f"{os.path.basename(home)[8:]}@example.invalid"}})
        settings = read_json(self.root / "program.json")
        write_json(self.root / "program.json", {**settings, "worker_claude_ref": "-", "claude_config_dir": self.a, "claude_seats": [self.a, self.b]})
        self.pid = 500

    def usage(self, home, five=10, week=10, five_resets=2 * 3600, week_resets=3 * 86400, session="s1"):
        now = self.system.t
        os.makedirs(os.path.join(home, "hl-usage"), exist_ok=True)
        write_json(
            os.path.join(home, "hl-usage", f"{session}.json"),
            {
                "observed_at": int(now),
                "rate_limits": {
                    "five_hour": {"used_percentage": five, "resets_at": int(now + five_resets)},
                    "seven_day": {"used_percentage": week, "resets_at": int(now + week_resets)},
                },
            },
        )

    def seat_record(self):
        from hl.common import read_json

        path = self.root / "seat.json"
        return read_json(path) if path.exists() else {}

    def started_on(self, result):
        ran = self.herdr.panes[result["pane"]]["ran"]
        return next(line.split("export CLAUDE_CONFIG_DIR=")[1].split(";")[0] for line in ran if "export CLAUDE_CONFIG_DIR=" in line)

    def manager_on_limit(self, home, resets_in=3 * 3600):
        """The last manager started here, on `home`, stopped on a usage limit."""
        state = self.state()
        state["manager"] = {"session": SESSION, "home": home, "started_at": stamp(self.system.t - 600)}
        write_json(self.root / "watchdog.json", state)
        record = {
            "type": "assistant",
            "uuid": "limit-seat",
            "error": "rate_limit",
            "isApiErrorMessage": True,
            "message": {"content": [{"type": "text", "text": "You've hit your limit"}]},
            "quotaLimits": {"resetsAt": int(self.system.t) + resets_in, "rateLimitType": "five_hour"},
        }
        (self.home / ".claude" / "projects" / "-root" / f"{SESSION}.jsonl").write_text(json.dumps(record) + "\n")

    def release(self, note):
        """A manager that claimed after the last start gives the program up."""
        self.pid += 1
        self.procs.table[self.pid] = "boot"
        self.system.t += 60
        claim.claim(self.root, self.pid, None, SESSION, self.procs)
        self.system.t += 60
        claim.release(self.root, self.pid, self.procs, note)
        del self.procs.table[self.pid]
        self.system.t += 60

    def test_the_first_manager_starts_on_the_first_seat_and_records_it(self):
        self.usage(self.a)
        self.usage(self.b)
        result = self.tick()
        self.assertEqual((result["action"], self.started_on(result)), ("started", self.a))
        self.assertEqual(self.state()["manager"]["home"], self.a)

    def test_a_tick_removes_the_readings_whose_every_window_has_reset(self):
        self.usage(self.a, five_resets=-3600, week_resets=-3600, session="spent")
        self.usage(self.a, session="live")
        self.usage(self.b)
        self.tick()
        self.assertEqual(sorted(os.listdir(os.path.join(self.a, "hl-usage"))), ["live.json"])

    def test_a_full_seat_moves_the_program_and_the_next_manager_starts_on_the_other(self):
        self.usage(self.a, week=99)
        self.usage(self.b, week=60)
        result = self.tick()
        self.assertEqual((result["action"], self.started_on(result)), ("started", self.b))
        self.assertEqual(self.seat_record()["home"], self.b)
        self.assertEqual(self.system.sent[0][0], "canary: moved to another Claude seat")
        self.assertIn("a@example.invalid is full", self.system.sent[0][1])
        # Said once: the next tick finds the program already where it should be.
        self.system.t += 1900
        self.tick()
        self.assertEqual([t for t, _ in self.system.sent].count("canary: moved to another Claude seat"), 1)

    def test_a_healthy_manager_on_the_old_seat_is_left_to_hand_over_itself(self):
        self.hold(claimed_min_ago=20, beat_min_ago=2)
        state = self.state()
        state["manager"] = {"session": SESSION, "home": self.a, "started_at": stamp(self.system.t - 1200)}
        write_json(self.root / "watchdog.json", state)
        self.usage(self.a, week=99)
        self.usage(self.b, week=60)
        self.assertEqual(self.tick()["action"], "healthy")
        self.assertEqual(self.seat_record()["home"], self.b)
        self.assertEqual(self.herdr.called("agent", "start"), [])

    def test_a_manager_stopped_on_the_old_seats_limit_is_replaced_at_once_on_the_new_seat(self):
        # Its heartbeat is fresh: it stopped minutes ago, and waiting 45 minutes would waste them.
        self.hold(claimed_min_ago=30, beat_min_ago=3)
        self.manager_on_limit(self.a)
        self.usage(self.a, five=97, week=50)
        self.usage(self.b, week=60)
        result = self.tick()
        self.assertEqual(result["action"], "started")
        self.assertIn("stopped on a usage limit of a@example.invalid", result["reason"])
        self.assertEqual(self.started_on(result), self.b)
        self.assertIn("closed pane", result["closed"])

    def test_a_manager_stopped_on_a_limit_with_nowhere_to_go_pauses_the_program(self):
        self.hold(claimed_min_ago=30, beat_min_ago=3)
        self.manager_on_limit(self.a, resets_in=3 * 3600)
        self.usage(self.a, five=97, week=50)
        self.usage(self.b, week=99.5, week_resets=40 * 3600)
        result = self.tick()
        self.assertEqual(result["action"], "seat-paused")
        self.assertEqual(result["until"], stamp(int(self.system.t) + 3 * 3600 + 120))

    def test_every_seat_full_pauses_starts_nothing_and_says_so_once(self):
        self.hold(claimed_min_ago=20, beat_min_ago=2)
        self.usage(self.a, week=99, week_resets=40 * 3600)
        self.usage(self.b, five=99, week=70, five_resets=3 * 3600)
        first = self.tick()
        self.assertEqual(first["action"], "seat-paused")
        self.assertEqual(first["until"], stamp(int(self.system.t) + 3 * 3600 + 120))
        # The manager saves, gives the program up and stops; nothing replaces it while the pause holds.
        claim.release(self.root, MANAGER_PID, self.procs, "seat pause until " + first["until"])
        del self.procs.table[MANAGER_PID]
        for _ in range(3):
            self.system.t += 1900
            self.assertEqual(self.tick()["action"], "seat-paused")
        self.assertEqual(self.herdr.called("agent", "start"), [])
        self.assertEqual([t for t, _ in self.system.sent], ["canary: paused: every Claude seat is full"])

    def test_once_a_seat_resets_the_program_resumes_on_it(self):
        self.usage(self.a, week=99, week_resets=40 * 3600)
        self.usage(self.b, five=99, week=70, five_resets=3 * 3600)
        self.assertEqual(self.tick()["action"], "seat-paused")
        self.release("seat pause")
        self.system.t = self.system.t + 3 * 3600 + 200
        result = self.tick()
        self.assertEqual((result["action"], self.started_on(result)), ("started", self.b))
        self.assertIn("b@example.invalid is open again", result["reason"])
        self.assertIsNone(self.seat_record()["paused_until"])
        self.assertEqual(self.system.sent[-2][0], "canary: resumed: a Claude seat has reset")
        # The resumed manager is a hand-over, not one of the hour's three starts.
        self.assertTrue(result["handover"])

    def test_a_seat_whose_every_window_has_reset_resumes_the_program_at_the_pause_end(self):
        # The mini, 2026-11-06: b was full on its week, its 5-hour window had
        # already reset, and no session on b drew a new reading. At the pause
        # end every window of b's readings has reset: b is open, not unknown.
        self.usage(self.a, week=99, week_resets=72 * 3600)
        self.usage(self.b, five=28, week=99, five_resets=-3600, week_resets=3 * 3600)
        self.assertEqual(self.tick()["action"], "seat-paused")
        self.release("seat pause")
        self.system.t = self.system.t + 3 * 3600 + 200
        result = self.tick()
        self.assertEqual(result["action"], "started", result)
        self.assertEqual(self.started_on(result), self.b)
        self.assertIn("b@example.invalid is open again", result["reason"])
        self.assertIsNone(self.seat_record()["paused_until"])

    def test_hand_overs_the_seats_asked_for_never_count_as_managers_that_do_no_work(self):
        self.usage(self.a)
        self.usage(self.b)
        self.assertEqual(self.tick()["action"], "started")
        for n in range(watchdog.MAX_IDLE_HANDOVERS + 2):
            self.release(f"seat move to b ({n})")
            result = self.tick()
            self.assertEqual(result["action"], "started", result)
        self.assertIsNone(self.state()["halted"])

    def test_the_status_command_shows_the_seats(self):
        self.usage(self.a, week=40)
        self.usage(self.b, week=60)
        with mock.patch("hl.watchdog.emit") as emitted:
            self.assertEqual(watchdog.main(["--root", str(self.root), "status"]), 0)
        shown = emitted.call_args[0][0]["seat"]
        self.assertEqual((shown["seat"], shown["decision"]["action"]), (self.a, "stay"))
