"""The deploy check, against a real origin and clone. The Verify command is
the item's own; whether it passes depends on what the base branch holds."""

from __future__ import annotations

import contextlib
import io
import json
import os
import time
from unittest import mock

from hl import items, merge, verify
from hl.common import Refusal, read_json, write_json

from .support import RunCase, install_fake_gh
from .test_merge import git

BACKLOG = """## V-1 — Ship the flag

**Scope.** Add the flag.

**Accept.**
1. It is there.

**Verify.** `[[ -n "$BASH_VERSION" ]] && test -f flag.txt && git log -1 --format=%s`
Wait. 10

## V-2 — No deploy check

**Scope.** Nothing to verify.

**Accept.**
1. One.
"""

MERGED_AT = "2026-10-12T12:00:00Z"
T0 = verify._epoch(MERGED_AT)


class VerifyCase(RunCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"GH_TOKEN": "loop-token-for-tests"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.tmp / "root"
        self.root.mkdir()
        # Where a program's runs live.
        self.run_dir = self.root / "runs" / "run-1"
        write_json(
            self.root / "program.json",
            {"program": "canary", "base": "main", "worker_claude_ref": "op://x/y/z", "gh_token_ref": "op://x/y/z", "cursor_model": "m"},
        )
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "clone"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        git(self.tmp, "clone", str(self.origin), str(self.repo))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        self.base = self.push("README.md", "base\n", "base")
        # A stream's clone as staging leaves it: detached at the base, and never moved after.
        git(self.repo, "fetch", "origin")
        git(self.repo, "checkout", "--detach", "origin/main")
        self.tools = merge.Tools(gh=str(install_fake_gh(self.tmp)), sleep=lambda s: None)
        backlog = self.tmp / "V-ITEMS.md"
        backlog.write_text(BACKLOG)
        self.init(backlog)
        self.now = T0 + 60
        self.slept = []
        self.merged("V-1")

    def push(self, name, text, message):
        """A commit on the remote's base branch, made from a scratch clone so the stream's clone stays where it was."""
        other = self.tmp / f"other-{len(list(self.tmp.glob('other-*')))}"
        git(self.tmp, "clone", str(self.origin), str(other))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(other, "config", key, value)
        (other / name).write_text(text)
        git(other, "add", name)
        git(other, "commit", "-m", message)
        git(other, "push", "origin", "HEAD:main")
        return git(other, "rev-parse", "HEAD")

    def merged(self, item_id, sha=None):
        item = items.load_item(self.run_dir, item_id)
        item["status"] = "merged"
        items.save_item(self.run_dir, item)
        write_json(self.item_dir(item_id) / "merge.json", {"state": "MERGED", "at": MERGED_AT, "merge_sha": sha or self.base})

    def run_verify(self, *argv, item_id="V-1", runner=verify.run_command, run_dir=None):
        run_dir = run_dir or self.run_dir
        opts = verify._parser().parse_args(
            ["--root", str(self.root), "--run-dir", str(run_dir), "--repo-dir", str(self.repo), "--id", item_id, *argv]
        )

        def sleep(seconds):
            self.slept.append(seconds)
            self.now += seconds

        return verify.verify(self.root, run_dir, self.repo, item_id, self.tools, opts, clock=lambda: self.now, sleep=sleep, runner=runner)

    def field(self, item_id="V-1"):
        return items.load_item(self.run_dir, item_id)["fields"].get("verify")

    def count(self, item_id="V-1"):
        return items.load_attempts(self.run_dir, item_id)["verify"]


class VerifyTest(VerifyCase):
    def test_a_command_that_passes_is_recorded_with_what_it_ran_and_where(self):
        sha = self.push("flag.txt", "up\n", "V-1: ship the flag")
        self.merged("V-1", sha)
        code, result = self.run_verify()
        self.assertEqual((code, result["result"], result["tries"], result["base_moved"]), (0, "verified", 1, False))
        self.assertEqual((self.field(), self.count()), ("verified", 1))
        text = (self.item_dir("V-1") / "verify-1.txt").read_text()
        self.assertIn('command: [[ -n "$BASH_VERSION" ]] && test -f flag.txt && git log -1 --format=%s', text)
        self.assertIn(f"checkout: origin/main at {sha}", text)
        self.assertIn("exit: 0", text)
        # The command's own output: it ran in a checkout of the base as the remote has it now.
        self.assertIn("V-1: ship the flag", text)

    def test_the_stream_clone_is_left_as_it_was_and_the_checkout_is_gone(self):
        self.push("flag.txt", "up\n", "V-1: ship the flag")
        before = git(self.repo, "rev-parse", "HEAD")
        self.run_verify()
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertFalse((self.repo / ".worktrees" / "verify-V-1").exists())
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_a_checkout_left_by_a_run_that_was_killed_is_replaced(self):
        self.push("flag.txt", "up\n", "V-1: ship the flag")
        git(self.repo, "worktree", "add", "--detach", str(self.repo / ".worktrees" / "verify-V-1"), "HEAD")
        self.assertEqual(self.run_verify()[0], 0)
        self.assertFalse((self.repo / ".worktrees" / "verify-V-1").exists())

    def test_a_failure_before_the_deadline_decides_nothing(self):
        code, result = self.run_verify()
        self.assertEqual((code, result["result"], result["exit"], result["minutes_left"]), (3, "not-yet", 1, 9.0))
        self.assertEqual((self.field(), self.count()), (None, 1))
        d = self.item_dir("V-1")
        self.assertFalse((d / "verify-1.txt").exists())
        self.assertIn("exit: 1", (d / "verify-1.try-1.txt").read_text())
        # The deploy lands; the same attempt goes on and passes.
        self.push("flag.txt", "up\n", "V-1: ship the flag")
        self.now += 120
        code, result = self.run_verify()
        self.assertEqual((code, result["result"], result["attempt"], result["tries"]), (0, "verified", 1, 2))
        self.assertEqual((self.field(), self.count()), ("verified", 1))

    def test_a_failure_at_the_deadline_is_recorded_as_failed(self):
        self.now = T0 + 600
        code, result = self.run_verify()
        self.assertEqual((code, result["result"], result["file"]), (4, "failed", "verify-1.txt"))
        self.assertEqual(self.field(), "verify-failed")
        self.assertIn("exit: 1", (self.item_dir("V-1") / "verify-1.txt").read_text())
        # Run again, it says the same and tries nothing.
        code, again = self.run_verify()
        self.assertEqual((code, again["already"], self.count()), (4, True, 1))
        self.assertIn("--retry", again["next"])

    def test_a_failed_verify_is_tried_again_once_and_only_with_a_reason(self):
        self.now = T0 + 600
        self.run_verify()
        with self.assertRaises(Refusal) as caught:
            self.run_verify("--retry")
        self.assertIn("--note", str(caught.exception))
        self.push("flag.txt", "up\n", "fix forward")
        code, result = self.run_verify("--retry", "--note", "the fix-forward V-9 merged")
        self.assertEqual((code, result["result"], result["attempt"]), (0, "verified", 2))
        self.assertEqual((self.field(), self.count()), ("verified", 2))
        self.assertEqual(read_json(self.item_dir("V-1") / "verify-2.json")["retry_note"], "the fix-forward V-9 merged")

    def test_a_retry_has_a_deadline_of_its_own_and_a_third_attempt_is_refused(self):
        self.now = T0 + 600
        self.run_verify()
        self.now += 3600
        code, result = self.run_verify("--retry", "--note", "re-ran the deploy")
        # Ten minutes from the retry, not from the merge.
        self.assertEqual((code, result["result"], result["minutes_left"]), (3, "not-yet", 10.0))
        self.now += 601
        self.assertEqual(self.run_verify()[0], 4)
        with self.assertRaises(Refusal) as caught:
            self.run_verify("--retry", "--note", "once more")
        self.assertIn("verify bound reached", str(caught.exception))

    def test_retry_is_refused_unless_a_failure_is_on_record(self):
        for state in ("nothing tried", "not yet"):
            with self.assertRaises(Refusal) as caught:
                self.run_verify("--retry", "--note", "x")
            self.assertIn("--retry is for a Verify recorded as failed", str(caught.exception), state)
            self.run_verify()
        self.push("flag.txt", "up\n", "V-1")
        self.run_verify()
        with self.assertRaises(Refusal):
            self.run_verify("--retry", "--note", "x")

    def test_once_verified_nothing_runs_again(self):
        self.push("flag.txt", "up\n", "V-1")
        self.run_verify()
        ran = []
        code, result = self.run_verify(runner=lambda *a: ran.append(a) or (1, "no"))
        self.assertEqual((code, result["already"], ran, self.count()), (0, True, [], 1))

    def test_wait_keeps_trying_until_it_passes(self):
        tries = []

        def runner(command, cwd, env, timeout):
            tries.append(self.now)
            if len(tries) == 3:
                return 0, "completed success\n"
            return 1, "in progress\n"

        code, result = self.run_verify("--wait", "--interval", "90", runner=runner)
        self.assertEqual((code, result["result"], result["tries"]), (0, "verified", 3))
        self.assertEqual(self.slept, [90, 90])
        self.assertEqual(sorted(p.name for p in self.item_dir("V-1").glob("verify-1*")), ["verify-1.json", "verify-1.try-1.txt", "verify-1.try-2.txt", "verify-1.txt"])

    def test_wait_ends_at_the_deadline_with_a_failure(self):
        self.now = T0 + 400
        code, result = self.run_verify("--wait", "--interval", "150", runner=lambda *a: (1, "apply-main: completed failure\n"))
        self.assertEqual((code, result["result"]), (4, "failed"))
        # 200 seconds were left: one full interval, then only what remained.
        self.assertEqual(self.slept, [150, 50])
        self.assertEqual(result["last_lines"], ["apply-main: completed failure"])
        self.assertEqual(self.field(), "verify-failed")

    def test_a_run_that_died_after_writing_a_pass_and_before_recording_it_is_put_right(self):
        # Found in review: the result file said exit 0, the item said nothing, and every later run answered "failed".
        self.push("flag.txt", "up\n", "V-1")
        with mock.patch("hl.verify.items.record_verify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_verify()
        self.assertTrue((self.item_dir("V-1") / "verify-1.txt").exists())
        self.assertIsNone(self.field())
        ran = []
        code, result = self.run_verify(runner=lambda *a: ran.append(a) or (1, "must not run again"))
        self.assertEqual((code, result["result"], result["already"], ran), (0, "verified", True, []))
        self.assertEqual((self.field(), self.count()), ("verified", 1))

    def test_a_run_that_died_after_writing_a_failure_and_before_recording_it_is_put_right(self):
        self.now = T0 + 600
        with mock.patch("hl.verify.items.record_verify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_verify()
        code, result = self.run_verify()
        self.assertEqual((code, result["already"], self.field()), (4, True, "verify-failed"))
        # And it can be tried again, with a reason, as any recorded failure can.
        self.push("flag.txt", "up\n", "fix forward")
        self.assertEqual(self.run_verify("--retry", "--note", "fixed forward")[0], 0)

    def test_a_run_that_died_between_counting_the_attempt_and_writing_its_record_goes_on(self):
        # Found in review: the attempt was counted, its record was not there, and every later run crashed on it.
        with mock.patch("hl.verify.write_json", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_verify()
        self.assertEqual(self.count(), 1)
        self.assertFalse((self.item_dir("V-1") / "verify-1.json").exists())
        code, result = self.run_verify()
        # The deadline is still the merge's ten minutes: 60 seconds have passed.
        self.assertEqual((code, result["result"], result["attempt"], result["minutes_left"], self.count()), (3, "not-yet", 1, 9.0, 1))
        self.push("flag.txt", "up\n", "V-1")
        self.assertEqual(self.run_verify()[0], 0)

    def fix_forward(self, command="true", ran=True, sha=None, repo="owner/repo", where=None, merge_state="MERGED", scope="Put V-1's deploy right."):
        """A fix-forward item in a run of its own: written for V-1, merged on top of it, and with its own deploy check run (unless `ran` is false)."""
        fix_run = where or (self.root / "runs" / "fix-1")
        backlog = self.tmp / "FIX-ITEMS.md"
        backlog.write_text(f"## V-9 — Fix the deploy\n\n**Scope.** {scope}\n\n**Accept.**\n1. It deploys.\n\n**Verify.** `{command}`\nWait. 1\n")
        items.init_run(fix_run, "fix-1", repo, [str(backlog)])
        fix = items.load_item(fix_run, "V-9")
        fix["status"] = "merged"
        items.save_item(fix_run, fix)
        merged_at = "2026-10-12T13:00:00Z"
        sha = sha or self.push(f"fix-{len(list(self.tmp.glob('other-*')))}.txt", "fixed\n", "V-9")
        write_json(items.item_dir(fix_run, "V-9") / "merge.json", {"state": merge_state, "at": merged_at, "merge_sha": sha})
        if ran:
            # Past its one-minute deadline, so a command that fails is recorded as failed.
            self.now = max(self.now, verify._epoch(merged_at) + 120)
            self.run_verify(item_id="V-9", run_dir=fix_run)
        return f"{fix_run}:V-9"

    def settle(self, ref):
        return verify.fixed_by(self.root, self.run_dir, self.repo, "V-1", ref, self.tools)

    def fail_v1(self):
        self.now = T0 + 600
        self.run_verify()
        self.assertEqual(self.field(), "verify-failed")

    def test_a_failed_deploy_that_was_fixed_forward_is_settled_by_the_fix(self):
        # Found in review: after a red deploy the item's own Verify can never pass, and its run could never merge again.
        self.fail_v1()
        code, out = self.settle(self.fix_forward())
        self.assertEqual((code, out["result"], out["fixed_by"]), (0, "fixed-forward", "fix-1/V-9"))
        self.assertEqual(self.field(), "fixed-forward")
        self.assertEqual(items.load_item(self.run_dir, "V-1")["fields"]["verify_fixed_by"], "fix-1/V-9")
        # Settled: nothing runs again, and its own last result does not replace the record.
        self.assertEqual(self.run_verify()[1], {"result": "fixed-forward", "already": True, "item": "V-1"})
        self.assertEqual(self.field(), "fixed-forward")
        self.assertFalse((self.item_dir("V-1") / verify.LOCK).exists())

    def test_the_same_repository_under_another_spelling_of_its_name_is_the_same_repository(self):
        self.fail_v1()
        self.assertEqual(self.settle(self.fix_forward(repo="Owner/Repo"))[0], 0)

    def refused(self, cases):
        import shutil

        for make, why in cases:
            with self.assertRaises(Refusal) as caught:
                self.settle(make())
            self.assertIn(why, str(caught.exception), why)
            for left in (self.root / "runs" / "fix-1", self.tmp / "elsewhere", self.root / "runs" / "deeper"):
                shutil.rmtree(left, ignore_errors=True)
        self.assertEqual(self.field(), "verify-failed")

    def test_only_a_merge_on_top_of_it_whose_own_verify_passed_settles_it(self):
        self.fail_v1()
        self.refused(
            (
                (lambda: self.fix_forward(ran=False), "its own Verify has passed"),
                (lambda: self.fix_forward(command="false"), "its own Verify has passed"),
                # The same commit is not a later one.
                (lambda: self.fix_forward(sha=self.base), "not on top of V-1's merge"),
                (lambda: "no-colon", "takes <run directory>:<ID>"),
            )
        )

    def test_after_is_what_the_base_branch_says_and_not_the_time_a_record_was_written(self):
        # Found in review: a merge recorded late (one that died and was recovered) carries a late time, whenever it landed.
        first = self.base
        later = self.push("later.txt", "x\n", "V-1 lands here")
        self.merged("V-1", sha=later)
        self.fail_v1()
        self.refused(((lambda: self.fix_forward(sha=first), "not on top of V-1's merge"),))
        fix = self.fix_forward(sha="0" * 40)
        with self.assertRaises(Refusal):
            self.settle(fix)

    def test_a_fix_forward_is_an_item_of_this_program_in_the_same_repository_with_records_of_its_own(self):
        # Found in review: any directory holding two files that said "merged" and "verified" settled a red deploy.
        self.fail_v1()

        def by_hand(**kw):
            """The fields say it passed; nothing was run."""
            ref = self.fix_forward(ran=False, **kw)
            fix_run = ref.rpartition(":")[0]
            items.record_verify(fix_run, "V-9", "verified")
            return ref

        def failed_then_edited():
            """Its deploy check ran and failed; the field was changed afterwards."""
            ref = self.fix_forward(command="false")
            items.record_verify(ref.rpartition(":")[0], "V-9", "verified")
            return ref

        def unrecorded_merge():
            ref = self.fix_forward()
            (self.root / "runs" / "fix-1" / "items" / "V-9" / "merge.json").unlink()
            return ref

        self.refused(
            (
                (lambda: by_hand(), "own records do not show"),
                (failed_then_edited, "own records do not show"),
                (lambda: self.fix_forward(merge_state="CLOSED"), "own records do not show"),
                (unrecorded_merge, "own records do not show"),
                (lambda: self.fix_forward(repo="owner/other"), "deploys something else"),
                (lambda: self.fix_forward(where=self.tmp / "elsewhere" / "fix-1"), "an item of this program"),
                (lambda: self.fix_forward(where=self.root / "runs" / "deeper" / "fix-1"), "an item of this program"),
            )
        )

    def test_a_later_item_that_was_not_written_for_this_deploy_does_not_settle_it(self):
        # Found in review: any later item of the repository with a passing Verify of its own settled the failure, a docs item included.
        self.fail_v1()
        self.refused(
            (
                (lambda: self.fix_forward(scope="Correct a heading in the README."), "does not name V-1"),
                (lambda: self.fix_forward(scope="Follows V-10 and V-11."), "does not name V-1"),
            )
        )

    def test_a_deploy_is_not_settled_beside_a_verify_of_the_same_item_that_is_still_running(self):
        # Found in review: a retry in flight wrote its own result over the settlement when it ended.
        self.fail_v1()
        ref = self.fix_forward()
        with verify.Lock(self.item_dir("V-1"), 600):
            with self.assertRaises(Refusal) as caught:
                self.settle(ref)
        self.assertIn("another hl-verify is running", str(caught.exception))
        self.assertEqual(self.field(), "verify-failed")
        self.assertEqual(self.settle(ref)[0], 0)

    def lock_owned_by(self, pid, host=None, age=0):
        import socket

        lock = self.item_dir("V-1") / verify.LOCK
        lock.mkdir()
        (lock / "owner.json").write_text(json.dumps({"pid": pid, "host": host or socket.gethostname(), "started_at": time.time()}))
        if age:
            old = time.time() - age
            os.utime(lock, (old, old))
        return lock

    def dead_pid(self):
        import subprocess

        child = subprocess.Popen(["true"])
        child.wait()
        return child.pid

    def test_a_lock_whose_owner_is_dead_is_taken_over_at_once(self):
        lock = self.lock_owned_by(self.dead_pid())
        with verify.Lock(self.item_dir("V-1"), 600):
            owner = json.loads((lock / "owner.json").read_text())
            self.assertEqual(owner["pid"], os.getpid())
        self.assertFalse(lock.exists())

    def test_a_lock_whose_owner_is_alive_holds(self):
        lock = self.lock_owned_by(os.getpid())
        with self.assertRaises(Refusal) as caught:
            with verify.Lock(self.item_dir("V-1"), 600):
                pass
        self.assertIn("another hl-verify is running", str(caught.exception))
        self.assertTrue(lock.exists())

    def test_a_lock_of_another_host_is_held_until_it_ages_out(self):
        self.lock_owned_by(self.dead_pid(), host="elsewhere.invalid")
        with self.assertRaises(Refusal):
            with verify.Lock(self.item_dir("V-1"), 600):
                pass

    def test_an_old_lock_still_ages_out_whoever_owns_it(self):
        self.lock_owned_by(os.getpid(), age=700)
        with verify.Lock(self.item_dir("V-1"), 600):
            pass
        self.assertFalse((self.item_dir("V-1") / verify.LOCK).exists())

    def test_an_old_lock_with_no_owner_record_ages_out(self):
        lock = self.item_dir("V-1") / verify.LOCK
        lock.mkdir()
        old = time.time() - 700
        os.utime(lock, (old, old))
        with verify.Lock(self.item_dir("V-1"), 600):
            pass

    def test_a_verify_that_has_not_failed_is_not_settled_by_anything(self):
        with self.assertRaises(Refusal) as caught:
            self.settle(self.fix_forward())
        self.assertIn("settles a Verify recorded as failed", str(caught.exception))

    def test_a_retry_that_passed_and_died_before_its_record_is_recorded_as_passed(self):
        # Found in review: the record stayed `verify-failed`, the retry was spent, and the run could not merge again.
        self.fail_v1()
        self.push("flag.txt", "up\n", "V-1")
        with mock.patch("hl.items.record_verify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_verify("--retry", "--note", "the deploy was started again by hand")
        self.assertEqual((self.field(), self.count()), ("verify-failed", 2))
        self.assertIn("exit: 0", (self.item_dir("V-1") / "verify-2.txt").read_text())
        code, result = self.run_verify()
        self.assertEqual((code, result["result"], self.field(), self.count()), (0, "verified", "verified", 2))

    def test_a_runs_repository_facts_are_found_without_the_root_option_and_whatever_the_case_of_its_name(self):
        # Found in review: the protected paths went unread when --root was left out or the name differed in case.
        settings = read_json(self.root / "program.json")
        settings["repos"] = {"Owner/Repo": {"protected": ["gen/**"]}, "owner/other": {"protected": ["x"]}}
        write_json(self.root / "program.json", settings)
        self.assertEqual(merge._repo_facts(None, self.run_dir), {"protected": ["gen/**"]})
        self.assertEqual(merge._repo_facts(self.root, self.run_dir), {"protected": ["gen/**"]})
        # A run directory that lies in no program has no facts to read.
        self.assertEqual(merge._repo_facts(None, self.tmp / "loose-run"), {})

    def test_a_command_that_prints_bytes_that_are_not_text_is_still_read(self):
        code, said = verify.run_command("printf 'ok \\377\\376 done\\n'", str(self.tmp), dict(os.environ), 30)
        self.assertEqual(code, 0)
        self.assertIn("ok ", said)
        self.assertIn(" done", said)

    def test_what_a_command_started_is_stopped_with_it_when_it_runs_out_of_time(self):
        marker = self.tmp / "still-running"
        code, _ = verify.run_command(f"(sleep 2; touch {marker}) & sleep 30", str(self.tmp), dict(os.environ), 0.5)
        self.assertEqual(code, 124)
        time.sleep(2.5)
        self.assertFalse(marker.exists())

    def test_a_second_run_for_the_same_item_waits_its_turn(self):
        (self.item_dir("V-1") / verify.LOCK).mkdir()
        with self.assertRaises(Refusal) as caught:
            self.run_verify()
        self.assertIn("another hl-verify is running for this item", str(caught.exception))
        self.assertEqual(self.count(), 0)
        # A lock older than any run can be belonged to a run that died.
        old = time.time() - 3 * 3600
        os.utime(self.item_dir("V-1") / verify.LOCK, (old, old))
        self.assertEqual(self.run_verify()[0], 3)
        self.assertFalse((self.item_dir("V-1") / verify.LOCK).exists())

    def test_the_lock_is_released_when_a_run_is_refused_or_fails(self):
        git(self.repo, "remote", "set-url", "origin", str(self.tmp / "nowhere.git"))
        with self.assertRaises(Refusal):
            self.run_verify()
        self.assertFalse((self.item_dir("V-1") / verify.LOCK).exists())

    def test_a_command_that_runs_out_of_time_is_a_failed_try(self):
        code, said = verify.run_command("echo started; sleep 5", str(self.tmp), dict(os.environ), 0.5)
        self.assertEqual(code, 124)
        self.assertIn("no answer in", said)
        timed_out = lambda *a: (124, "[hl-verify: no answer in 1500 s]\n")
        self.assertEqual(self.run_verify(runner=timed_out)[0], 3)
        self.now = T0 + 600
        code, result = self.run_verify(runner=timed_out)
        self.assertEqual((code, result["result"], self.field()), (4, "failed", "verify-failed"))

    def test_the_command_runs_under_bash_proper(self):
        # Verify lines are written for bash. Under `sh`, which here is bash in POSIX mode, this one is a syntax error.
        code, said = verify.run_command("cat <(echo from-bash)", str(self.tmp), dict(os.environ), 30)
        self.assertEqual((code, said.strip()), (0, "from-bash"))

    def test_a_base_that_moved_past_the_merge_is_said(self):
        self.push("flag.txt", "up\n", "V-1")
        self.merged("V-1", "0" * 40)
        self.assertTrue(self.run_verify()[1]["base_moved"])

    def test_only_a_merged_item_with_a_verify_line_is_checked(self):
        with self.assertRaises(Refusal) as caught:
            self.run_verify(item_id="V-2")
        self.assertIn("V-2 is pending", str(caught.exception))
        self.merged("V-2")
        with self.assertRaises(Refusal) as caught:
            self.run_verify(item_id="V-2")
        self.assertIn("has no Verify line", str(caught.exception))
        self.assertEqual(self.count("V-2"), 0)

    def test_a_clone_that_cannot_fetch_refuses_and_spends_nothing_more(self):
        git(self.repo, "remote", "set-url", "origin", str(self.tmp / "nowhere.git"))
        with self.assertRaises(Refusal) as caught:
            self.run_verify()
        self.assertIn("could not fetch origin/main", str(caught.exception))
        self.assertIsNone(self.field())

    def test_the_result_is_never_typed_by_hand(self):
        with self.assertRaises(Refusal) as caught:
            items.set_fields(self.run_dir, "V-1", ["verify=verified"])
        self.assertIn("not set by hand", str(caught.exception))
        with self.assertRaises(Refusal):
            items.record_verify(self.run_dir, "V-1", "probably fine")
        with self.assertRaises(Refusal):
            items.record_verify(self.run_dir, "V-2", "verified")

    def test_the_command_line_needs_the_loops_own_token(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": ""}), contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = verify.main(["--root", str(self.root), "--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--id", "V-1"])
        self.assertEqual(code, 2)
        self.assertIn("GH_TOKEN is not set", json.loads(out.getvalue())["refused"][0])
