from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hl import claim
from hl.common import Refusal


class FakeProcs:
    """A process table: pid -> (start time, parent pid, command line)."""

    def __init__(self, table=None):
        self.table = dict(table or {})

    def start(self, pid):
        row = self.table.get(pid)
        return row[0] if row else None

    def parent(self, pid):
        row = self.table.get(pid)
        return (row[1], row[2]) if row else None


class ClaimCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.procs = FakeProcs({100: ("Mon Oct 11 09:00:00 2026", 1, "claude --model x")})

    def claim(self, pid=100, pane="w1:p1"):
        return claim.claim(self.root, pid, pane, "sess", self.procs)


class ClaimTest(ClaimCase):
    def test_the_first_manager_takes_the_program(self):
        result = self.claim()
        self.assertEqual((result["claimed"], result["already"]), (True, False))
        record = json.loads((self.root / "manager.lock.d" / "claim.json").read_text())
        self.assertEqual((record["pid"], record["start"], record["pane"]), (100, "Mon Oct 11 09:00:00 2026", "w1:p1"))

    def test_a_new_claim_replaces_the_last_managers_heartbeat_with_its_own(self):
        (self.root / "heartbeat.json").write_text(json.dumps({"at": "2026-10-12T01:59:18Z", "epoch": 1, "pid": 41125, "note": "old"}))
        self.claim()
        beat = json.loads((self.root / "heartbeat.json").read_text())
        self.assertEqual((beat["pid"], beat["note"]), (100, "claimed"))
        self.assertLess(claim.status(self.root, self.procs)["heartbeat"]["age_s"], 5)
        # Claiming again is not a save: it leaves the heartbeat as it is.
        (self.root / "heartbeat.json").write_text(json.dumps({"at": "x", "epoch": 2, "pid": 100, "note": "saved the ledger"}))
        self.assertTrue(self.claim()["already"])
        self.assertEqual(json.loads((self.root / "heartbeat.json").read_text())["note"], "saved the ledger")

    def test_the_claim_records_the_shell_the_manager_was_started_from(self):
        self.procs.table[100] = ("Mon Oct 11 09:00:00 2026", 4242, "claude --model x")
        self.assertEqual(self.claim()["claim"]["parent_pid"], 4242)

    def test_a_claim_written_before_the_shell_was_recorded_learns_it_when_its_holder_claims_again(self):
        first = self.claim()["claim"]
        path = self.root / "manager.lock.d" / "claim.json"
        old = {k: v for k, v in first.items() if k != "parent_pid"}
        path.write_text(json.dumps(old))
        self.procs.table[100] = ("Mon Oct 11 09:00:00 2026", 4242, "claude --model x")
        again = self.claim()
        self.assertEqual((again["already"], again["claim"]["parent_pid"], again["claim"]["claimed_at"]), (True, 4242, first["claimed_at"]))
        self.assertEqual(json.loads(path.read_text())["parent_pid"], 4242)

    def test_the_same_manager_claiming_again_keeps_its_claim(self):
        first = self.claim()["claim"]
        again = self.claim()
        self.assertTrue(again["already"])
        self.assertEqual(again["claim"]["claimed_at"], first["claimed_at"])

    def test_a_second_live_manager_is_refused_and_told_who_holds_it(self):
        self.claim()
        self.procs.table[200] = ("Mon Oct 11 10:00:00 2026", 1, "claude")
        with self.assertRaises(Refusal) as caught:
            self.claim(pid=200, pane="w2:p1")
        self.assertIn("pid 100 in pane w1:p1", str(caught.exception))
        self.assertEqual(claim.read_claim(self.root)["pid"], 100)

    def test_a_dead_holder_is_replaced(self):
        self.claim()
        del self.procs.table[100]
        self.procs.table[200] = ("Mon Oct 11 10:00:00 2026", 1, "claude")
        result = self.claim(pid=200)
        self.assertEqual(result["replaced"]["pid"], 100)
        self.assertEqual(claim.read_claim(self.root)["pid"], 200)
        self.assertEqual([p.name for p in self.root.glob("manager.lock.d.stale-*")], [])

    def test_a_reused_pid_does_not_keep_a_dead_holder_alive(self):
        self.claim()
        # Same pid, another process: the old manager died and the pid came round again.
        self.procs.table[100] = ("Tue Oct 12 03:00:00 2026", 1, "zsh")
        self.procs.table[200] = ("Tue Oct 12 03:00:01 2026", 1, "claude")
        self.assertEqual(self.claim(pid=200)["replaced"]["pid"], 100)

    def test_a_new_process_with_the_dead_holders_pid_is_not_the_holder(self):
        first = self.claim()["claim"]
        self.procs.table[100] = ("Tue Oct 12 03:00:00 2026", 1, "claude")
        result = self.claim()
        self.assertFalse(result["already"])
        self.assertEqual(result["replaced"]["start"], first["start"])
        self.assertEqual(claim.read_claim(self.root)["start"], "Tue Oct 12 03:00:00 2026")

    def test_a_claim_being_written_right_now_is_not_treated_as_dead(self):
        (self.root / "manager.lock.d").mkdir()
        with self.assertRaises(Refusal) as caught:
            self.claim()
        self.assertIn("claiming this program right now", str(caught.exception))

    def test_a_lock_directory_left_empty_by_a_crash_is_replaced_after_the_grace(self):
        lock = self.root / "manager.lock.d"
        lock.mkdir()
        old = time.time() - claim.CLAIMING_GRACE_S - 5
        os.utime(lock, (old, old))
        self.assertTrue(self.claim()["claimed"])

    def test_a_process_that_is_not_running_cannot_claim(self):
        with self.assertRaises(Refusal):
            self.claim(pid=999)

    def test_every_claim_and_replacement_is_logged(self):
        self.claim()
        del self.procs.table[100]
        self.procs.table[200] = ("Mon Oct 11 10:00:00 2026", 1, "claude")
        self.claim(pid=200)
        events = [json.loads(line)["event"] for line in (self.root / "claims.log").read_text().splitlines()]
        self.assertEqual(events, ["claimed", "replaced-dead-claim", "claimed"])


class BeatReleaseTest(ClaimCase):
    def test_beat_stamps_the_heartbeat_for_the_holder(self):
        self.claim()
        claim.beat(self.root, 100, self.procs, note="saved the ledger")
        state = claim.status(self.root, self.procs)
        self.assertTrue(state["held"] and state["alive"])
        self.assertLess(state["heartbeat"]["age_s"], 5)

    def test_beat_is_refused_for_a_manager_that_lost_the_program(self):
        self.claim()
        self.procs.table[200] = ("Mon Oct 11 10:00:00 2026", 1, "claude")
        with self.assertRaises(Refusal) as caught:
            claim.beat(self.root, 200, self.procs)
        self.assertIn("not claimed by pid 200", str(caught.exception))
        # The heartbeat is still the holder's own, from when it claimed.
        self.assertEqual(json.loads((self.root / "heartbeat.json").read_text())["pid"], 100)

    def test_beat_is_refused_for_a_new_process_with_the_holders_pid(self):
        self.claim()
        self.procs.table[100] = ("Tue Oct 12 03:00:00 2026", 1, "claude")
        with self.assertRaises(Refusal):
            claim.beat(self.root, 100, self.procs)

    def test_beat_is_refused_when_nobody_holds_the_program(self):
        with self.assertRaises(Refusal):
            claim.beat(self.root, 100, self.procs)

    def test_release_frees_the_program_for_the_next_manager(self):
        self.claim()
        claim.release(self.root, 100, self.procs, "recycling at an item boundary")
        self.assertFalse((self.root / "manager.lock.d").exists())
        self.procs.table[200] = ("Mon Oct 11 10:00:00 2026", 1, "claude")
        self.assertFalse(self.claim(pid=200)["already"])

    def test_the_log_still_says_who_held_the_program_after_a_release(self):
        self.assertIsNone(claim.last_holder(self.root))
        self.claim(pane="w8:p1")
        claim.release(self.root, 100, self.procs, "recycling at an item boundary")
        self.assertIsNone(claim.read_claim(self.root))
        last = claim.last_holder(self.root)
        self.assertEqual((last["pid"], last["pane"]), (100, "w8:p1"))
        self.procs.table[200] = ("Mon Oct 11 10:00:00 2026", 1, "claude")
        self.claim(pid=200, pane="wK:p1")
        self.assertEqual(claim.last_holder(self.root)["pane"], "wK:p1")

    def test_a_damaged_line_in_the_log_is_passed_over(self):
        self.claim(pane="w8:p1")
        with open(self.root / "claims.log", "a") as fh:
            fh.write("not json\n" + '{"event": "note", "claim": null}\n')
        self.assertEqual(claim.last_holder(self.root)["pane"], "w8:p1")

    def test_only_the_holder_can_release(self):
        self.claim()
        self.procs.table[200] = ("Mon Oct 11 10:00:00 2026", 1, "claude")
        with self.assertRaises(Refusal):
            claim.release(self.root, 200, self.procs, "mine now")
        self.assertEqual(claim.read_claim(self.root)["pid"], 100)

    def test_status_reports_a_dead_holder(self):
        self.claim()
        del self.procs.table[100]
        state = claim.status(self.root, self.procs)
        self.assertEqual((state["held"], state["alive"], state["heartbeat"]["pid"]), (True, False, 100))
        (self.root / "heartbeat.json").unlink()
        self.assertIsNone(claim.status(self.root, self.procs)["heartbeat"])


class FindManagerTest(unittest.TestCase):
    def test_the_nearest_ancestor_with_the_agent_name_is_the_manager(self):
        procs = FakeProcs(
            {
                50: ("t", 40, "python3 hl-claim --root r claim"),
                40: ("t", 30, "/bin/zsh -c hl-claim"),
                30: ("t", 20, "claude --dangerously-skip-permissions --model claude-opus-5-5"),
                20: ("t", 1, "-zsh"),
            }
        )
        self.assertEqual(claim.find_manager_pid(procs, "claude", start_pid=50), 30)

    def test_a_full_path_to_the_agent_matches_by_its_name(self):
        procs = FakeProcs({50: ("t", 30, "sh"), 30: ("t", 1, "/Users/you/.local/bin/claude --resume x")})
        self.assertEqual(claim.find_manager_pid(procs, "claude", start_pid=50), 30)

    def test_no_such_ancestor_is_none(self):
        procs = FakeProcs({50: ("t", 40, "zsh"), 40: ("t", 1, "login")})
        self.assertIsNone(claim.find_manager_pid(procs, "claude", start_pid=50))

    def test_real_ps_knows_this_process_and_not_a_dead_one(self):
        procs = claim.Procs()
        self.assertIsNotNone(procs.start(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIsNone(procs.start(child.pid))
        ppid, command = procs.parent(os.getpid())
        self.assertEqual(ppid, os.getppid())
        self.assertTrue(command)


class PinsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Resolved, because a pin records the resolved path and macOS keeps
        # its temp directory behind a symlink.
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in claim.PINNED:
            self.tool(name, f"#!/bin/sh\necho {name} 1\n")
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.bin)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tool(self, name, text, directory=None):
        path = (directory or self.bin) / name
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_pin_records_every_binary_once(self):
        first = claim.pin(self.root)
        self.assertTrue(first["created"])
        self.assertEqual(sorted(first["bins"]), sorted(claim.PINNED))
        self.assertEqual(first["bins"]["gh"]["path"], str(self.bin / "gh"))
        self.tool("gh", "#!/bin/sh\necho changed\n")
        again = claim.pin(self.root)
        self.assertFalse(again["created"])
        self.assertEqual(again["bins"]["gh"]["sha256"], first["bins"]["gh"]["sha256"])

    def test_check_passes_on_untouched_binaries_and_gives_their_paths(self):
        claim.pin(self.root)
        self.assertEqual(claim.check_pins(self.root)["bins"]["git"], str(self.bin / "git"))

    def test_check_accepts_changed_content_and_records_it(self):
        before = claim.pin(self.root)["bins"]["gh"]
        self.tool("gh", "#!/bin/sh\necho gh 2\n")
        result = claim.check_pins(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], [{"name": "gh", "why": "content changed", "version": "gh 2"}])
        pins = claim.load_pins(self.root)
        self.assertNotEqual(pins["bins"]["gh"]["sha256"], before["sha256"])
        entry = pins["history"][-1]
        self.assertEqual((entry["name"], entry["was"], entry["auto"]), ("gh", before, True))
        self.assertIn("accepted automatically: content changed; gh 2", entry["note"])
        # Accepted once: the next check finds nothing new.
        self.assertEqual(claim.check_pins(self.root)["updated"], [])
        self.assertEqual(len(claim.load_pins(self.root)["history"]), 1)

    def test_check_accepts_an_update_whose_version_cannot_be_read(self):
        claim.pin(self.root)
        self.tool("cursor-agent", "#!/bin/sh\necho keychain locked >&2\nexit 1\n")
        result = claim.check_pins(self.root)
        self.assertEqual(result["updated"], [{"name": "cursor-agent", "why": "content changed", "version": None}])
        self.assertEqual(claim.load_pins(self.root)["history"][-1]["note"], "accepted automatically: content changed")

    def test_check_accepts_a_name_that_now_resolves_elsewhere(self):
        claim.pin(self.root)
        shim = self.tmp / "shim"
        shim.mkdir()
        self.tool("git", "#!/bin/sh\necho shim\n", directory=shim)
        with mock.patch.dict(os.environ, {"PATH": f"{shim}{os.pathsep}{self.bin}"}):
            result = claim.check_pins(self.root)
        self.assertEqual(result["bins"]["git"], str(shim / "git"))
        self.assertIn(f"now resolves to {shim / 'git'}", result["updated"][0]["why"])
        self.assertEqual(claim.load_pins(self.root)["bins"]["git"]["path"], str(shim / "git"))

    def test_a_caller_with_another_path_checks_only_the_pinned_files(self):
        claim.pin(self.root)
        shim = self.tmp / "shim"
        shim.mkdir()
        self.tool("git", "#!/bin/sh\necho other\n", directory=shim)
        with mock.patch.dict(os.environ, {"PATH": f"{shim}{os.pathsep}{self.bin}"}):
            result = claim.check_pins(self.root, ("git",), on_path=False)
        self.assertEqual((result["bins"]["git"], result["updated"]), (str(self.bin / "git"), []))
        self.tool("git", "#!/bin/sh\necho git 2\n")
        result = claim.check_pins(self.root, ("git",), on_path=False)
        self.assertEqual((result["bins"]["git"], result["updated"][0]["why"]), (str(self.bin / "git"), "content changed"))

    def test_check_accepts_a_link_moved_to_another_file(self):
        versions = self.tmp / "versions"
        versions.mkdir()
        one = self.tool("1.0", "#!/bin/sh\necho 1\n", directory=versions)
        two = self.tool("2.0", "#!/bin/sh\necho 2\n", directory=versions)
        (self.bin / "cursor-agent").unlink()
        (self.bin / "cursor-agent").symlink_to(one)
        claim.pin(self.root)
        (self.bin / "cursor-agent").unlink()
        (self.bin / "cursor-agent").symlink_to(two)
        result = claim.check_pins(self.root)
        self.assertEqual(result["updated"][0]["name"], "cursor-agent")
        self.assertIn(f"now points at {two}", result["updated"][0]["why"])
        self.assertEqual(claim.load_pins(self.root)["bins"]["cursor-agent"]["realpath"], str(two))

    def test_check_pins_a_binary_added_to_the_kit_after_the_program_was_pinned(self):
        claim.pin(self.root)
        pins = claim.load_pins(self.root)
        del pins["bins"]["herdr"]
        claim.write_json(self.root / "pins.json", pins)
        result = claim.check_pins(self.root)
        self.assertEqual(result["updated"][0]["why"], "not pinned before")

    def test_check_waits_out_a_binary_being_replaced(self):
        claim.pin(self.root)
        (self.bin / "herdr").unlink()
        sleeps = []

        def reinstall(seconds):
            sleeps.append(seconds)
            self.tool("herdr", "#!/bin/sh\necho herdr 2\n")

        with mock.patch.object(claim.time, "sleep", side_effect=reinstall):
            result = claim.check_pins(self.root)
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(result["updated"][0]["version"], "herdr 2")

    def test_check_refuses_a_binary_that_stays_missing(self):
        claim.pin(self.root)
        (self.bin / "herdr").unlink()
        with mock.patch.object(claim, "MISSING_WAIT_S", 0):
            with self.assertRaises(Refusal) as caught:
                claim.check_pins(self.root)
        self.assertIn("herdr", caught.exception.reasons[0])

    def test_check_can_be_limited_to_the_binaries_a_step_uses(self):
        claim.pin(self.root)
        self.tool("claude", "#!/bin/sh\necho upgraded\n")
        result = claim.check_pins(self.root, ("git", "gh"))
        self.assertEqual((sorted(result["bins"]), result["updated"]), (["gh", "git"], []))

    def test_repin_accepts_one_change_and_keeps_the_old_record(self):
        before = claim.pin(self.root)["bins"]["claude"]
        self.tool("claude", "#!/bin/sh\necho upgraded\n")
        result = claim.repin(self.root, "claude", "upgrade 2.1.286 to 2.1.290, seen in the release notes")
        self.assertEqual(result["was"], before)
        self.assertEqual(claim.check_pins(self.root)["updated"], [])
        history = claim.load_pins(self.root)["history"]
        self.assertEqual((history[0]["name"], history[0]["was"]["sha256"]), ("claude", before["sha256"]))

    def test_check_before_pin_is_refused(self):
        with self.assertRaises(Refusal):
            claim.check_pins(self.root)


class CliTest(ClaimCase):
    def run_cli(self, *argv):
        with mock.patch("hl.claim.emit") as emitted:
            code = claim.main(["--root", str(self.root), "--pid", "100", *argv], procs=self.procs)
        return code, emitted.call_args[0][0]

    def test_claim_then_beat_then_release(self):
        self.assertEqual(self.run_cli("claim", "--pane", "w1:p1")[0], 0)
        self.assertEqual(self.run_cli("beat")[0], 0)
        self.assertEqual(self.run_cli("release", "--note", "done")[0], 0)

    def test_beat_carries_a_seat_move_or_pause_to_the_manager(self):
        from hl.common import write_json

        a, b = str(self.root / "seat-a"), str(self.root / "seat-b")
        settings = {"program": "p", "base": "main", "worker_claude_ref": "-", "gh_token_ref": "g", "cursor_model": "c", "claude_config_dir": a, "claude_seats": [a, b]}
        write_json(self.root / "program.json", settings)
        self.run_cli("claim", "--pane", "w1:p1")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": a}):
            self.assertNotIn("seat", self.run_cli("beat")[1])
            write_json(self.root / "seat.json", {"home": b})
            told = self.run_cli("beat")[1]["seat"]
            self.assertEqual((told["action"], told["to"]), ("move", b))
            write_json(self.root / "seat.json", {"home": b, "paused_until": 1791849600})
            self.assertEqual(self.run_cli("beat")[1]["seat"]["action"], "pause")
        # The heartbeat file itself stays the heartbeat.
        self.assertNotIn("seat", json.loads((self.root / "heartbeat.json").read_text()))

    def test_a_refusal_exits_2_with_the_reasons(self):
        code, out = self.run_cli("beat")
        self.assertEqual(code, 2)
        self.assertIn("not claimed by pid 100", out["refused"][0])


if __name__ == "__main__":
    unittest.main()
