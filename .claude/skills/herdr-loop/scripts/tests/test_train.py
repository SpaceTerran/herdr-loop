"""A repository's train: what merges into it, the batches that are tested,
and what a promotion leaves on the base. Git is real; GitHub is the stand-in
in train_support.py, which makes its merges in the same bare repository."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from unittest import mock

from hl import backlog, flow, items, ledger, merge, program, spawn, train, verify, wait
from hl.common import Refusal, read_json, write_json

from .test_merge import git
from .train_support import GREEN, REPO, TEST_REF, TrainCase

RED = [{"name": "app-tests", "bucket": "fail", "state": "FAILURE", "link": "https://example.invalid/o/r/actions/runs/4242/job/9"}]
RUNNING = [{"name": "app-tests", "bucket": "pending", "state": "IN_PROGRESS", "link": ""}]


class TrainBranchTest(TrainCase):
    def test_the_train_is_made_from_the_base_the_first_time_it_is_wanted(self):
        self.assertEqual(train.ensure(self.cfg, REPO, self.clone, self.tools), self.TRAIN)
        self.assertEqual(self.sha(self.TRAIN), self.sha("main"))
        # The clone knows the branch by its own name, whatever it was told to follow.
        self.assertEqual(git(self.clone, "rev-parse", f"refs/remotes/origin/{self.TRAIN}"), self.sha("main"))
        self.assertEqual(train.load_state(self.root, REPO)["branch"], self.TRAIN)
        # Wanted again, it is left where it is.
        run = self.run_of("r1", "A-1")
        merged = self.land(run, "A-1")
        train.ensure(self.cfg, REPO, self.clone, self.tools)
        self.assertEqual(self.sha(self.TRAIN), merged)

    def test_a_repository_without_a_train_has_none(self):
        self.settings(repos={REPO: {}})
        with self.assertRaises(Refusal) as caught:
            self.status()
        self.assertIn("has no train", str(caught.exception))
        self.assertIsNone(program.train_settings(self.cfg, REPO))
        self.settings(repos={REPO: {"train": {}}})
        self.assertEqual(program.train_settings(self.cfg, "Owner/Repo"), {"batch": 5, "limit": 15})

    def test_a_program_name_that_cannot_be_a_branch_name_is_refused(self):
        for name in ("my program", "a/b", "-x", "é", "", "a..b", "x.lock", "x."):
            with self.assertRaises(Refusal, msg=name):
                program.train_branch({"program": name})
        self.assertEqual(program.train_branch({"program": "app-rebrand.v2_x"}), "hl-train/app-rebrand.v2_x")
        self.assertEqual(program.batch_branch({"program": "p"}, 3), "hl-batch/p/3")


class StatusTest(TrainCase):
    def test_what_has_merged_and_not_been_promoted_is_counted_across_the_repositorys_runs(self):
        first, second = self.run_of("r1", "A-1", "A-2"), self.run_of("r2", "B-1", "B-2", "B-3")
        said = self.status()
        self.assertEqual((said["ahead"], said["due"], said["full"], said["head"]), (0, False, False, None))
        self.land(first, "A-1")
        said = self.status()
        self.assertEqual((said["ahead"], said["items"], said["due"], said["full"]), (1, ["A-1"], False, False))
        self.land(second, "B-1")
        said = self.status()
        self.assertEqual((said["ahead"], said["items"], said["due"], said["full"], said["missing"]), (2, ["A-1", "B-1"], True, False, []))
        self.land(first, "A-2")
        self.land(second, "B-2")
        self.assertEqual((self.status()["ahead"], self.status()["full"]), (4, True))

    def test_a_run_of_another_repository_or_one_that_merges_into_the_base_is_not_on_this_train(self):
        on = self.run_of("r1", "A-1")
        self.land(on, "A-1")
        elsewhere = self.run_of("r2", "B-1")
        run = items.load_run(elsewhere)
        run["repo"] = "owner/other"
        write_json(elsewhere / "run.json", run)
        plain = self.run_of("r3", "C-1")
        run = items.load_run(plain)
        del run["policy"]["merge"], run["policy"]["train"]
        write_json(plain / "run.json", run)
        for run_dir, item_id in ((elsewhere, "B-1"), (plain, "C-1")):
            item = items.load_item(run_dir, item_id)
            item["status"] = "merged"
            items.save_item(run_dir, item)
        self.assertEqual(self.status()["items"], ["A-1"])

    def test_an_open_batch_is_shown_with_what_it_holds_and_what_merged_after_it(self):
        run = self.run_of("r1", "A-1", "A-2", "A-3")
        self.land(run, "A-1")
        self.land(run, "A-2")
        self.open_batch()
        self.land(run, "A-3")
        said = self.status()
        self.assertEqual((said["open"]["n"], said["open"]["items"], said["untested"], said["due"]), (1, ["A-1", "A-2"], ["A-3"], False))


class TestBatchTest(TrainCase):
    def setUp(self):
        super().setUp()
        self.run = self.run_of("r1", "A-1", "A-2", "A-3", "A-4")

    def test_a_batch_pins_the_trains_head_and_asks_for_the_bases_checks_on_exactly_that(self):
        self.land(self.run, "A-1")
        second = self.land(self.run, "A-2")
        code, out = self.open_batch("two risky items")
        self.assertEqual((code, out["result"], out["batch"], out["items"], out["head"]), (0, "testing", 1, ["A-1", "A-2"], second))
        self.assertEqual(self.sha("hl-batch/canary/1"), second)
        pull = self.pr(out["pr"])
        self.assertEqual((pull["headRefName"], pull["baseRefName"], pull["state"]), ("hl-batch/canary/1", "main", "OPEN"))
        self.assertEqual(pull["title"], "canary: batch 1 (A-1, A-2)")
        self.assertIn("`A-1` Item A-1", pull["body"])
        record = read_json(train.batch_path(self.root, REPO, 1))
        self.assertEqual((record["state"], record["head"], record["base_sha"], record["note"]), ("testing", second, self.sha("main"), "two risky items"))
        # What merges afterwards moves the train, and not the batch.
        self.land(self.run, "A-3")
        self.assertEqual(self.sha("hl-batch/canary/1"), second)

    def test_with_nothing_merged_there_is_nothing_to_test(self):
        with self.assertRaises(Refusal) as caught:
            self.open_batch()
        self.assertIn("there is nothing to test", str(caught.exception))
        self.assertEqual(self.calls("api", "--method", "POST"), [])

    def test_one_batch_is_tested_at_a_time(self):
        self.land(self.run, "A-1")
        self.open_batch()
        self.land(self.run, "A-2")
        with self.assertRaises(Refusal) as caught:
            self.open_batch()
        self.assertIn("batch 1 is being tested", str(caught.exception))

    def test_a_batch_that_was_being_opened_when_the_script_died_is_taken_up_under_its_own_number(self):
        self.land(self.run, "A-1")
        with mock.patch("hl.train._open_pr", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.open_batch()
        self.assertEqual(read_json(train.batch_path(self.root, REPO, 1))["state"], "opening")
        code, out = self.open_batch()
        self.assertEqual((code, out["batch"]), (0, 1))
        # And a pull request that was made before it died is the batch's, not a second one.
        record = read_json(train.batch_path(self.root, REPO, 1))
        record["state"] = "opening"
        write_json(train.batch_path(self.root, REPO, 1), record)
        self.assertEqual(self.open_batch()[1]["pr"], out["pr"])
        self.assertEqual(len(self.calls("api", "--method", "POST")), 2)  # A-1's own pull request, and the batch's

    def test_a_base_that_moved_is_taken_into_the_train_before_anything_is_pinned(self):
        self.land(self.run, "A-1")
        elsewhere = self.push("main", "renovate.txt", "bumped\n", "a dependency bump merged by something else")
        self.assertTrue(self.status()["base_moved"])
        code, out = self.open_batch()
        self.assertEqual((code, out["brought_in"]), (0, elsewhere))
        head = out["head"]
        self.assertEqual(git(self.origin, "show", f"{head}:renovate.txt"), "bumped")
        self.assertEqual(git(self.origin, "show", f"{head}:A-1.txt"), "A-1")
        # The merge is on record as the script's own, so the promotion does not take it for a stranger's commit.
        self.assertEqual(train.load_state(self.root, REPO)["merge_backs"], [head])
        self.assertEqual(self.promote()[0], 0)

    def test_a_base_that_does_not_merge_cleanly_is_left_for_an_item_to_resolve(self):
        self.land(self.run, "A-1", name="shared.txt", text="the item's line\n")
        self.push("main", "shared.txt", "someone else's line\n", "a change to the same file")
        before = self.sha(self.TRAIN)
        code, out = self.open_batch()
        self.assertEqual((code, out["result"], out["files"]), (4, "conflict", ["shared.txt"]))
        self.assertEqual(self.sha(self.TRAIN), before)
        self.assertIsNone(train.load_state(self.root, REPO)["open"])
        # Nothing half-merged is left in the stream's clone.
        self.assertEqual(git(self.clone, "worktree", "list").count("\n"), 0)

    def test_a_commit_on_the_train_that_this_loop_did_not_merge_stops_the_test(self):
        self.land(self.run, "A-1")
        stranger = self.push(self.TRAIN, "slipped-in.txt", "x\n", "pushed straight to the train")
        with self.assertRaises(Refusal) as caught:
            self.open_batch()
        self.assertIn(f"{stranger[:12]} (pushed straight to the train)", str(caught.exception))
        self.assertIn("did not merge", str(caught.exception))

    def test_a_merge_on_record_that_is_not_on_the_train_stops_the_test(self):
        self.land(self.run, "A-1")
        # The train rewritten to the base by something else: the merge is gone from it.
        git(self.origin, "update-ref", f"refs/heads/{self.TRAIN}", self.sha("main"))
        self.assertEqual(self.status()["missing"], ["A-1"])
        with self.assertRaises(Refusal) as caught:
            self.open_batch()
        self.assertIn("not on hl-train/canary: A-1", str(caught.exception))


class PromoteTest(TrainCase):
    def setUp(self):
        super().setUp()
        self.run = self.run_of("r1", "A-1", "A-2", "A-3", "A-4", "A-5")

    def batch(self, *ids):
        for item_id in ids:
            self.land(self.run, item_id)
        return self.open_batch()[1]

    def field(self, item_id, name="promoted"):
        return items.load_item(self.run, item_id)["fields"].get(name)

    def test_a_green_batch_is_merged_into_the_base_and_the_base_is_then_what_was_tested(self):
        opened = self.batch("A-1", "A-2")
        before = self.sha("main")
        code, out = self.promote()
        self.assertEqual((code, out["result"], out["batch"], out["items"], out["verify"]), (0, "promoted", 1, ["A-1", "A-2"], []))
        # GitHub is told which commit it may merge, so a push after the last look merges nothing.
        asked = self.calls("pr", "merge")[-1]
        self.assertEqual((asked[asked.index("--match-head-commit") + 1], "--merge" in asked), (opened["head"], True))
        landed = self.sha("main")
        self.assertEqual(out["promoted_sha"], landed)
        # A merge commit: the base's old head and the tested commit are its parents, and its tree is the tested tree.
        self.assertEqual(git(self.origin, "log", "-1", "--format=%P", landed).split(), [before, opened["head"]])
        self.assertEqual(self.tree("main"), self.tree(opened["head"]))
        self.assertEqual(git(self.origin, "log", "-1", "--format=%s", landed), "canary: batch 1 (A-1, A-2)")
        # Each item's own merge is on the base as itself, so one of them can be reverted alone.
        self.assertEqual(git(self.origin, "log", "--format=%s", f"{before}..{landed}").splitlines()[1:], ["A-2: Item A-2 (#2)", "A-1: Item A-1 (#1)"])
        self.assertEqual((self.field("A-1"), self.field("A-2"), self.field("A-1", "promoted_sha")), ("1", "1", landed))
        self.assertTrue(self.field("A-1", "promoted_at"))
        record = read_json(train.batch_path(self.root, REPO, 1))
        self.assertEqual((record["state"], record["promoted_sha"], record["attribution_on_base"]), ("promoted", landed, None))
        # The train moves up to the promotion, and nothing is left open.
        self.assertEqual(self.sha(self.TRAIN), landed)
        said = self.status()
        self.assertEqual((said["ahead"], said["open"], said["base_moved"]), (0, None, False))
        self.assertNotIn("refs/heads/hl-batch/canary/1", git(self.origin, "for-each-ref", "--format=%(refname)"))
        self.assertIsNone(merge.lock_remote_sha(self.tools, self.clone, TEST_REF))

    def test_what_merged_while_a_batch_was_tested_stays_on_the_train_for_the_next(self):
        self.batch("A-1")
        later = self.land(self.run, "A-2")
        code, out = self.promote()
        self.assertEqual((code, out["items"], out["ahead"], out["train"]["how"]), (0, ["A-1"], 1, "merged"))
        self.assertEqual((self.field("A-1"), self.field("A-2")), ("1", None))
        # The base does not hold what was not tested, and the train holds both the promotion and the later merge.
        self.assertNotIn("A-2.txt", git(self.origin, "ls-tree", "--name-only", "main"))
        head = self.sha(self.TRAIN)
        self.assertEqual(git(self.origin, "merge-base", "--is-ancestor", self.sha("main"), head), "")
        self.assertEqual(git(self.origin, "merge-base", "--is-ancestor", later, head), "")
        code, out = self.open_batch()
        self.assertEqual((out["batch"], out["items"]), (2, ["A-2"]))
        self.assertEqual(self.promote()[0], 0)
        self.assertEqual(self.field("A-2"), "2")

    def test_checks_still_running_promote_nothing(self):
        opened = self.batch("A-1")
        self.checks(opened["pr"], *RUNNING)
        before = self.sha("main")
        code, out = self.promote()
        self.assertEqual((code, out["result"], out["checks"]), (5, "pending", [{"name": "app-tests", "bucket": "pending"}]))
        self.assertEqual((self.sha("main"), self.field("A-1")), (before, None))
        self.assertEqual(read_json(train.batch_path(self.root, REPO, 1))["state"], "testing")

    def test_a_red_check_promotes_nothing_and_closes_nothing(self):
        # It may have failed for a reason that is not the batch's; running it again is cheaper than testing a new batch.
        opened = self.batch("A-1", "A-2")
        self.checks(opened["pr"], *RED)
        before = self.sha("main")
        code, out = self.promote()
        self.assertEqual((code, out["result"], out["batch"], out["failed"], out["links"]), (4, "red", 1, ["app-tests"], [RED[0]["link"]]))
        self.assertEqual((self.sha("main"), self.field("A-1")), (before, None))
        self.assertEqual((self.pr(opened["pr"])["state"], read_json(train.batch_path(self.root, REPO, 1))["state"]), ("OPEN", "testing"))
        self.assertEqual(self.sha("hl-batch/canary/1"), opened["head"])
        self.assertFalse(train.red_file(self.root, REPO).exists())
        # Run again and green, the same batch is promoted.
        self.checks(opened["pr"], *GREEN)
        self.assertEqual(self.promote()[1]["items"], ["A-1", "A-2"])

    def test_a_repair_is_written_only_for_a_batch_whose_checks_have_failed(self):
        with self.assertRaises(Refusal) as caught:
            self.repair()
        self.assertIn("there is nothing to repair", str(caught.exception))
        opened = self.batch("A-1")
        for checks, why in ((GREEN, "are green: `promote` it"), (RUNNING, "are still running"), ((), "are not reported")):
            self.checks(opened["pr"], *checks)
            if not checks:
                self.gh(required={})
            with self.assertRaises(Refusal) as caught:
                self.repair()
            self.assertIn(why, str(caught.exception))
        self.assertEqual((self.pr(opened["pr"])["state"], read_json(train.batch_path(self.root, REPO, 1))["state"]), ("OPEN", "testing"))

    def test_a_red_batch_is_closed_with_what_failed_and_a_repair_to_run(self):
        opened = self.batch("A-1", "A-2")
        self.checks(opened["pr"], *RED, {"name": "lint", "bucket": "pass", "state": "SUCCESS", "link": ""})
        self.gh(required={"main": ["app-tests", "lint"]})
        before = self.sha("main")
        self.assertEqual(self.promote()[0], 4)
        code, out = self.repair()
        self.assertEqual((code, out["result"], out["batch"], out["failed"], out["repair_id"]), (0, "repair", 1, ["app-tests"], "TRN-1"))
        self.assertEqual((self.sha("main"), self.field("A-1")), (before, None))
        self.assertEqual(self.pr(opened["pr"])["state"], "CLOSED")
        self.assertNotIn("hl-batch/canary/1", git(self.origin, "for-each-ref", "--format=%(refname)"))
        said = (train.state_dir(self.root, REPO) / "batch-1-red.md").read_text()
        self.assertIn("- `app-tests`: fail. https://example.invalid/o/r/actions/runs/4242/job/9 Its log: `gh run view 4242 --repo owner/repo --log-failed`.", said)
        self.assertIn("Green: `lint`.", said)
        self.assertIn("- `A-1` Item A-1", said)
        self.assertIn("`A-2.txt`", said)
        # While it is red, briefs can say what fails.
        self.assertEqual(train.red_file(self.root, REPO).read_text(), said)
        state = train.load_state(self.root, REPO)
        self.assertEqual((state["red"], state["open"]), (1, None))
        # The repair is a backlog of one item that the loop's own parser takes.
        parsed = backlog.parse_backlog(Path(out["repair_file"]).read_text())
        self.assertEqual([(it.id, len(it.accept), it.verify_command) for it in parsed], [("TRN-1", 1, None)])
        self.assertIn("batch-1-red.md", parsed[0].text)
        self.assertIn("`app-tests`", parsed[0].accept[0])
        self.assertIn("--file " + out["repair_file"], out["next"])

    def test_after_a_repair_the_next_batch_carries_everything_and_a_green_one_clears_the_red(self):
        opened = self.batch("A-1")
        self.checks(opened["pr"], *RED)
        self.repair()
        repair = self.root / "runs" / "repair-1"
        policy = items.policy(items.load_run(self.run))
        items.init_run(repair, "repair-1", REPO, [str(train.state_dir(self.root, REPO) / "repair-1-ITEMS.md")], policy={k: policy[k] for k in ("premise", "review", "bounds", "merge", "train")}, stream="app")
        self.land(repair, "TRN-1")
        code, out = self.open_batch()
        self.assertEqual((code, out["batch"], out["items"]), (0, 2, ["A-1", "TRN-1"]))
        code, out = self.promote()
        self.assertEqual((code, out["items"]), (0, ["A-1", "TRN-1"]))
        self.assertFalse(train.red_file(self.root, REPO).exists())
        self.assertIsNone(train.load_state(self.root, REPO)["red"])
        self.assertEqual(items.load_item(repair, "TRN-1")["fields"]["promoted"], "2")

    def test_a_base_that_moved_after_the_test_closes_the_batch_untested_code_never_lands(self):
        opened = self.batch("A-1")
        moved = self.push("main", "other.txt", "x\n", "merged into the base by something else")
        code, out = self.promote()
        self.assertEqual((code, out["result"], out["base_sha"]), (6, "base-moved", moved))
        self.assertEqual((self.sha("main"), self.field("A-1")), (moved, None))
        self.assertEqual((self.pr(opened["pr"])["state"], read_json(train.batch_path(self.root, REPO, 1))["state"]), ("CLOSED", "abandoned"))
        # The next batch takes the base in, and is what gets tested and promoted.
        code, out = self.open_batch()
        self.assertEqual((out["batch"], out["brought_in"]), (2, moved))
        self.assertEqual(self.promote()[0], 0)
        self.assertEqual(self.tree("main"), self.tree(out["head"]))

    def test_a_commit_in_the_batch_that_no_record_accounts_for_stops_the_promotion_too(self):
        # The test read the train's history when it pinned the batch; the promotion reads it again, from the records as they are now.
        self.batch("A-1", "A-2")
        record = items.item_dir(self.run, "A-2") / "merge.json"
        kept = read_json(record)
        write_json(record, {**kept, "merge_sha": "0" * 40})
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("the batch carries commits this loop did not merge", str(caught.exception))
        self.assertIn(kept["merge_sha"][:12], str(caught.exception))
        self.assertIsNone(self.field("A-1"))

    def test_a_batch_whose_branch_was_pushed_to_is_not_what_was_tested(self):
        self.batch("A-1")
        self.push("hl-batch/canary/1", "late.txt", "x\n", "pushed onto the batch after its checks")
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("was tested", str(caught.exception))
        self.assertIsNone(self.field("A-1"))

    def test_a_check_that_turns_red_while_the_lock_is_waited_for_is_seen_under_the_lock(self):
        opened = self.batch("A-1")
        real = merge.acquire_lock

        def late(*args, **kwargs):
            self.checks(opened["pr"], *RED)
            return real(*args, **kwargs)

        before = self.sha("main")
        with mock.patch("hl.merge.acquire_lock", side_effect=late):
            code, out = self.promote()
        self.assertEqual((code, out["result"]), (4, "red"))
        self.assertEqual(self.sha("main"), before)
        self.assertIsNone(merge.lock_remote_sha(self.tools, self.clone, TEST_REF))
        self.assertFalse((train.state_dir(self.root, REPO) / train.LOCK_FILE).exists())
        self.assertEqual(read_json(train.batch_path(self.root, REPO, 1))["state"], "testing")

    def test_nothing_more_is_promoted_on_top_of_a_promotion_whose_deploy_check_has_not_passed(self):
        checked = self.run_of("r2", "V-1", verify="true")
        self.land(checked, "V-1")
        self.open_batch()
        code, out = self.promote()
        self.assertEqual(out["verify"], [{"run_dir": str(checked), "id": "V-1"}])
        self.batch("A-1")
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("V-1 (batch 1, verify=None): its Verify is not recorded as passed", str(caught.exception))
        self.assertEqual(self.status()["verify_pending"], ["V-1 (batch 1, verify=None)"])
        items.record_verify(checked, "V-1", "verify-failed")
        with self.assertRaises(Refusal):
            self.promote()
        items.record_verify(checked, "V-1", "fixed-forward")
        self.assertEqual(self.promote()[0], 0)

    def test_attribution_in_a_commit_of_the_batch_stops_the_promotion(self):
        self.land(self.run, "A-1")
        signed = self.push(self.TRAIN, "x.txt", "x\n", "A-9: something\n\nCo-authored-by: Cursor <cursoragent@cursor.com>")
        # On record as an item's merge, so only its message is at fault.
        late = self.run_of("r9", "A-9")
        item = items.load_item(late, "A-9")
        item["status"] = "merged"
        items.save_item(late, item)
        write_json(items.item_dir(late, "A-9") / "merge.json", {"state": "MERGED", "at": "2026-10-12T12:30:00Z", "merge_sha": signed})
        self.open_batch()
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn(signed[:12], str(caught.exception))
        self.assertIn("Co-authored-by", str(caught.exception))

    def test_a_promotion_github_already_made_is_recorded_and_one_of_another_commit_is_not(self):
        opened = self.batch("A-1")
        # The merge that died between GitHub's answer and its own record.
        self.tools.gh(["pr", "merge", str(opened["pr"]), "--repo", REPO, "--merge", "--subject", "canary: batch 1 (A-1)", "--body", "x"], self.clone)
        code, out = self.promote()
        self.assertEqual((code, out["result"], out["promoted_sha"]), (0, "promoted", self.sha("main")))
        self.assertTrue(read_json(train.batch_path(self.root, REPO, 1))["recovered"])
        self.assertEqual(self.field("A-1"), "1")

        opened = self.batch("A-2")
        self.push("hl-batch/canary/2", "late.txt", "x\n", "pushed onto the batch")
        self.tools.gh(["pr", "merge", str(opened["pr"]), "--repo", REPO, "--merge", "--subject", "by hand", "--body", "x"], self.clone)
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("merged outside this script", str(caught.exception))
        self.assertIsNone(self.field("A-2"))

    def test_a_batch_closed_by_something_else_is_given_up_and_tested_again(self):
        opened = self.batch("A-1")
        self.tools.gh(["api", "--method", "PATCH", f"repos/{REPO}/pulls/{opened['pr']}", "-f", "state=closed"], self.clone)
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("is CLOSED", str(caught.exception))
        with self.assertRaises(Refusal):
            train.abandon(self.root, REPO, self.clone, self.tools, " ")
        code, out = train.abandon(self.root, REPO, self.clone, self.tools, "closed on GitHub by hand")
        self.assertEqual((code, out["result"], out["batch"]), (0, "abandoned", 1))
        self.assertEqual(read_json(train.batch_path(self.root, REPO, 1))["note"], "closed on GitHub by hand")
        self.assertEqual(self.open_batch()[1]["batch"], 2)
        self.assertEqual(self.promote()[0], 0)
        with self.assertRaises(Refusal) as caught:
            train.abandon(self.root, REPO, self.clone, self.tools, "nothing is open now")
        self.assertIn("no batch is open", str(caught.exception))

    BLOCKED = "GitHub reports the batch's pull request blocked (merge state BLOCKED) although "

    def test_a_batch_github_calls_blocked_is_not_promoted_though_every_check_it_lists_is_green(self):
        # Every required check is green, and GitHub still waits for something else the branch requires.
        opened = self.batch("A-1", "A-2")
        self.gh(merge_state={str(opened["pr"]): "BLOCKED"})
        before = self.sha("main")
        merges = len(self.calls("pr", "merge"))
        for extra in ((), ("--dry-run",)):
            with self.assertRaises(Refusal) as caught:
                self.promote(*extra)
            self.assertEqual(caught.exception.reasons, [merge.blocked_reason("the batch's pull request", GREEN)])
        self.assertIn(self.BLOCKED + "every required check it lists is green. The base branch requires something else that has not happened", caught.exception.reasons[0])
        # Nothing moved, nothing was closed, and the lock was not taken.
        self.assertEqual((self.sha("main"), self.field("A-1"), len(self.calls("pr", "merge"))), (before, None, merges))
        self.assertEqual((self.pr(opened["pr"])["state"], read_json(train.batch_path(self.root, REPO, 1))["state"]), ("OPEN", "testing"))
        self.assertIsNone(merge.lock_remote_sha(self.tools, self.clone, TEST_REF))
        # Once GitHub no longer says so, the same batch is promoted.
        self.gh(merge_state={})
        self.assertEqual(self.promote()[0], 0)

    def behind_its_checks(self, pr, looks):
        """GitHub a moment after the batch's last check turned green: BLOCKED for this many more looks at its state, then CLEAN."""
        real, taken, pauses = self.tools.gh, [], []

        def gh(args, cwd, check=True):
            if args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "mergeStateStatus":
                taken.append(args)
                if len(taken) > looks:
                    self.gh(merge_state={})
            return real(args, cwd, check)

        self.tools.gh = gh
        self.tools.sleep = pauses.append
        self.gh(merge_state={str(pr): "BLOCKED"})
        return taken, pauses

    def test_a_batch_whose_merge_state_has_not_caught_up_with_its_green_checks_is_promoted(self):
        opened = self.batch("A-1")
        taken, pauses = self.behind_its_checks(opened["pr"], 1)
        code, out = self.promote()
        self.assertEqual((code, out["result"], out["items"]), (0, "promoted", ["A-1"]))
        self.assertEqual((len(taken), pauses[:1]), (2, [merge.BLOCKED_PAUSE_S]))

    def test_a_batch_that_stays_blocked_is_asked_a_few_times_and_then_not_promoted(self):
        opened = self.batch("A-1")
        taken, pauses = self.behind_its_checks(opened["pr"], 99)
        with self.assertRaisesRegex(Refusal, "GitHub reports the batch's pull request blocked"):
            self.promote("--dry-run")
        self.assertEqual((len(taken), pauses), (merge.BLOCKED_LOOKS, [merge.BLOCKED_PAUSE_S] * (merge.BLOCKED_LOOKS - 1)))

    def test_a_batch_whose_state_settles_as_conflicting_is_refused_for_the_conflict(self):
        opened = self.batch("A-1")
        self.gh(merge_state={str(opened["pr"]): "BLOCKED"})
        real = self.tools.gh

        def gh(args, cwd, check=True):
            if args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "mergeStateStatus":
                self.gh(merge_state={str(opened["pr"]): "DIRTY"})
            return real(args, cwd, check)

        self.tools.gh = gh
        with self.assertRaises(Refusal) as caught:
            self.promote("--dry-run")
        self.assertEqual(caught.exception.reasons, ["the batch's pull request conflicts with its base"])

    def test_a_batch_whose_pull_request_was_made_a_draft_is_refused_for_that_and_is_not_called_blocked(self):
        opened = self.batch("A-1")
        state = read_json(self.gh_state)
        state["prs"][str(opened["pr"])]["isDraft"] = True
        self.gh(prs=state["prs"])
        taken, _ = self.behind_its_checks(opened["pr"], 99)
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertEqual((caught.exception.reasons, taken), (["the batch's pull request is a draft or is armed to merge itself"], []))

    def test_a_blocked_batch_whose_checks_are_running_or_red_is_answered_as_it_was(self):
        opened = self.batch("A-1")
        self.gh(merge_state={str(opened["pr"]): "BLOCKED"})
        self.checks(opened["pr"], *RUNNING)
        code, out = self.promote()
        self.assertEqual((code, out["result"]), (5, "pending"))
        self.checks(opened["pr"], *RED)
        code, out = self.promote()
        self.assertEqual((code, out["result"], out["failed"]), (4, "red", ["app-tests"]))

    def test_a_blocked_batch_is_not_promoted_even_where_the_base_was_said_to_require_no_checks(self):
        opened = self.batch("A-1")
        self.gh(required={}, merge_state={str(opened["pr"]): "BLOCKED"})
        with self.assertRaises(Refusal) as caught:
            self.promote("--allow-no-required-checks")
        self.assertEqual(caught.exception.reasons, [merge.blocked_reason("the batch's pull request", [])])
        self.assertIn(self.BLOCKED + "it lists no required check", caught.exception.reasons[0])
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertEqual(len(caught.exception.reasons), 2)
        self.assertIn("no required checks are reported for the batch's pull request", caught.exception.reasons[0])

    def test_a_batch_that_turns_blocked_while_the_lock_is_waited_for_is_not_promoted(self):
        opened = self.batch("A-1")
        before = self.sha("main")
        real_acquire = merge.acquire_lock

        def acquire_then_block(*args, **kwargs):
            token = real_acquire(*args, **kwargs)
            self.gh(merge_state={str(opened["pr"]): "BLOCKED"})
            return token

        with mock.patch("hl.train.merge.acquire_lock", side_effect=acquire_then_block):
            with self.assertRaisesRegex(Refusal, "GitHub reports the batch's pull request blocked"):
                self.promote()
        self.assertEqual((self.sha("main"), self.pr(opened["pr"])["state"]), (before, "OPEN"))
        self.assertIsNone(merge.lock_remote_sha(self.tools, self.clone, TEST_REF))

    def test_a_base_that_requires_no_checks_promotes_nothing_unless_told_it_has_none(self):
        self.batch("A-1")
        self.gh(required={})
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("no required checks are reported for the batch's pull request", str(caught.exception))
        self.assertEqual(self.promote("--allow-no-required-checks")[0], 0)

    def test_a_dry_run_checks_everything_and_merges_nothing(self):
        self.batch("A-1")
        before = self.sha("main")
        code, out = self.promote("--dry-run")
        self.assertEqual((code, out["result"]), (0, "dry-run-passed"))
        self.assertEqual((self.sha("main"), self.calls("pr", "merge")[1:]), (before, []))

    def test_a_held_lock_is_waited_for_and_then_said(self):
        self.batch("A-1")
        token = merge.acquire_lock(self.tools, self.clone, "someone-else", "X-1", ref=TEST_REF)
        before = self.sha("main")
        code, out = self.promote()
        self.assertEqual((code, out["result"]), (3, "lock-timeout"))
        self.assertIn("someone-else", out["holder"])
        self.assertEqual(self.sha("main"), before)
        merge.release_lock(self.tools, self.clone, token)
        self.assertEqual(self.promote()[0], 0)

    def test_the_lock_a_dead_promotion_left_is_freed_by_the_next_call(self):
        self.batch("A-1")
        token = merge.acquire_lock(self.tools, self.clone, "train:canary", "batch-1", ref=TEST_REF)
        write_json(train.state_dir(self.root, REPO) / train.LOCK_FILE, token)
        self.assertEqual(self.promote()[0], 0)
        self.assertIsNone(merge.lock_remote_sha(self.tools, self.clone, TEST_REF))

    def test_a_merge_github_refuses_leaves_the_batch_as_it_was(self):
        self.batch("A-1")
        self.gh(merge_rc=1)
        before = self.sha("main")
        code, out = self.promote()
        self.assertEqual((code, out["result"]), (2, "refused"))
        self.assertIn("GitHub did not merge", out["refused"][0])
        self.assertEqual((self.sha("main"), self.field("A-1"), read_json(train.batch_path(self.root, REPO, 1))["state"]), (before, None, "testing"))
        self.assertIsNone(merge.lock_remote_sha(self.tools, self.clone, TEST_REF))
        self.gh(merge_rc=0)
        self.assertEqual(self.promote()[0], 0)

    def test_with_no_batch_open_there_is_nothing_to_promote(self):
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("no batch is being tested", str(caught.exception))

    def test_a_promotion_is_a_record_and_cannot_be_typed(self):
        self.land(self.run, "A-1")
        for pair in ("promoted=1", "promoted_sha=abc", "promoted_at=2026-10-12T12:00:00Z"):
            with self.assertRaises(Refusal) as caught:
                items.set_fields(self.run, "A-1", [pair])
            self.assertIn("is not set by hand", str(caught.exception))
        with self.assertRaises(Refusal):
            items.record_promoted(self.run, "A-2", 1, "abc", "2026-10-12T12:00:00Z")


class CutShortTest(TrainCase):
    """What a fresh review found: every way a command could be cut short, or
    two could meet, and leave a train that no command could move again."""

    def setUp(self):
        super().setUp()
        self.run = self.run_of("r1", "A-1", "A-2", "A-3", "A-4", "A-5")

    def field(self, item_id, name="promoted"):
        return items.load_item(self.run, item_id)["fields"].get(name)

    def abandon(self, note="given up"):
        return train.abandon(self.root, REPO, self.clone, self.tools, note)

    def test_a_promotion_cut_short_while_it_was_being_recorded_is_finished_by_the_next_promote(self):
        self.land(self.run, "A-1")
        self.land(self.run, "A-2")
        self.open_batch()
        real, calls = items.record_promoted, []

        def dying(*args, **kwargs):
            if calls:
                raise KeyboardInterrupt
            calls.append(1)
            return real(*args, **kwargs)

        with mock.patch("hl.items.record_promoted", dying):
            with self.assertRaises(KeyboardInterrupt):
                self.promote()
        landed = self.sha("main")
        self.assertEqual((self.field("A-1"), self.field("A-2")), ("1", None))
        record = read_json(train.batch_path(self.root, REPO, 1))
        self.assertEqual((record["state"], record["promoted_sha"], train.load_state(self.root, REPO)["open"]), ("landed", landed, 1))
        # Nothing but a promote may touch it now: giving it up would leave A-2 on the base and off the records for good.
        for command in (self.abandon, self.open_batch, self.repair):
            with self.assertRaises(Refusal) as caught:
                command()
            self.assertIn("is merged into the base and its records are not finished: `promote` finishes them", str(caught.exception))
        code, out = self.promote()
        self.assertEqual((code, out["result"], out["promoted_sha"], out["items"]), (0, "promoted", landed, ["A-1", "A-2"]))
        self.assertEqual((self.field("A-1"), self.field("A-2"), self.field("A-2", "promoted_sha")), ("1", "1", landed))
        self.assertEqual((read_json(train.batch_path(self.root, REPO, 1))["state"], train.load_state(self.root, REPO)["open"]), ("promoted", None))
        # GitHub was asked to merge once.
        self.assertEqual(len([c for c in self.calls("pr", "merge") if "--merge" in c]), 1)
        self.assertEqual(self.status()["ahead"], 0)

    def test_a_batch_github_has_merged_cannot_be_given_up(self):
        # The merge was made and its answer never seen: `promote` says to run it again, and an `abandon` here used to strand the item.
        self.land(self.run, "A-1")
        opened = self.open_batch()[1]
        real = self.tools.gh

        def unseen(argv, *args, **kwargs):
            answer = real(argv, *args, **kwargs)
            if argv[:2] == ["pr", "view"] and "state,mergeCommit" in argv:
                return mock.Mock(returncode=0, stdout=json.dumps({"state": "OPEN", "mergeCommit": None}), stderr="")
            return answer

        self.tools.gh = unseen
        code, out = self.promote()
        self.tools.gh = real
        self.assertEqual(code, 2)
        self.assertIn("was not seen to complete", out["refused"][0])
        self.assertEqual(self.pr(opened["pr"])["state"], "MERGED")
        with self.assertRaises(Refusal) as caught:
            self.abandon()
        self.assertIn("is merged, so the batch cannot be given up: `promote` records it", str(caught.exception))
        self.assertEqual(read_json(train.batch_path(self.root, REPO, 1))["state"], "testing")
        code, out = self.promote()
        self.assertEqual((code, out["result"], self.field("A-1")), (0, "promoted", "1"))

    def test_the_scripts_own_merge_of_the_base_is_on_record_before_it_is_pushed(self):
        # Killed between the push and the record, it used to be taken for a stranger's commit, and nothing could be tested again.
        self.land(self.run, "A-1")
        self.push("main", "elsewhere.txt", "x\n", "merged into the base by something else")
        real = self.tools.git

        def dying(args, cwd, *more, **kwargs):
            done = real(args, cwd, *more, **kwargs)
            if args[:2] == ["push", "origin"] and args[2].endswith(f":refs/heads/{self.TRAIN}") and ".worktrees/train-" in str(cwd):
                raise KeyboardInterrupt
            return done

        self.tools.git = dying
        with self.assertRaises(KeyboardInterrupt):
            self.open_batch()
        self.tools.git = real
        head = self.sha(self.TRAIN)
        self.assertEqual(git(self.origin, "show", f"{head}:elsewhere.txt"), "x")
        self.assertIn(head, train.load_state(self.root, REPO)["merge_backs"])
        code, out = self.open_batch()
        self.assertEqual((code, out["head"], out["items"]), (0, head, ["A-1"]))

    def test_a_merge_recorded_without_its_commit_gets_it_from_github(self):
        merged = self.land(self.run, "A-1")
        record = items.item_dir(self.run, "A-1") / "merge.json"
        write_json(record, {**read_json(record), "merge_sha": None})
        code, out = self.open_batch()
        self.assertEqual((code, out["items"], read_json(record)["merge_sha"]), (0, ["A-1"], merged))
        self.abandon()
        # Where GitHub names none either, that is said, and not that the train was rewritten.
        write_json(record, {**read_json(record), "merge_sha": None})
        state = read_json(self.gh_state)
        state["prs"]["1"]["mergeCommit"] = None
        self.gh_state.write_text(json.dumps(state))
        with self.assertRaises(Refusal) as caught:
            self.open_batch()
        self.assertIn("with no commit named, and GitHub names none for their pull requests: A-1", str(caught.exception))
        self.assertNotIn("rewritten", str(caught.exception))

    def test_a_merge_that_is_on_the_base_and_off_the_records_is_recorded_from_its_batch_or_said_plainly(self):
        self.land(self.run, "A-1")
        self.land(self.run, "A-2")
        self.open_batch()
        landed = self.promote()[1]["promoted_sha"]
        # Records damaged by an older version of this script: the batch says A-2 was promoted, the item does not.
        item = items.load_item(self.run, "A-2")
        for name in ("promoted", "promoted_sha", "promoted_at"):
            del item["fields"][name]
        items.save_item(self.run, item)
        self.land(self.run, "A-3")
        code, out = self.open_batch()
        self.assertEqual((code, out["items"]), (0, ["A-3"]))
        self.assertEqual((self.field("A-2"), self.field("A-2", "promoted_sha")), ("1", landed))
        self.abandon()
        # With no batch on record that took it there, something else merged it: said so, and not as a rewritten train.
        (train.batch_path(self.root, REPO, 1)).unlink()
        item = items.load_item(self.run, "A-2")
        for name in ("promoted", "promoted_sha", "promoted_at"):
            del item["fields"][name]
        items.save_item(self.run, item)
        with self.assertRaises(Refusal) as caught:
            self.open_batch()
        self.assertIn("on main already, and no promotion on record put them there: A-2", str(caught.exception))
        self.assertNotIn("rewritten", str(caught.exception))

    def test_a_merge_made_after_the_head_was_read_waits_for_the_next_batch_and_is_no_fault(self):
        first = self.land(self.run, "A-1")
        later = self.land(self.run, "A-2")
        entries = [e for e in train.unpromoted(self.root, REPO) if e["id"] == "A-2"]
        train.fetch(self.tools, self.clone, "main", self.TRAIN)
        self.assertEqual(train.not_in_batch(self.root, REPO, self.clone, self.tools, self.cfg, first, self.sha("main"), entries), [])
        self.assertNotEqual(first, later)

    def test_a_failed_deploy_does_not_hold_up_the_batch_that_carries_its_fix(self):
        # Found in review: nothing could be promoted until the Verify passed, and the fix could not be verified until it was promoted.
        checked = self.run_of("v", "V-1", verify="true")
        self.land(checked, "V-1")
        self.open_batch()
        self.promote()
        items.record_verify(checked, "V-1", "verify-failed")
        self.land(self.run, "A-1")
        self.open_batch()
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("V-1 (batch 1, verify=verify-failed): its Verify failed, and this batch holds no item that fixes it forward", str(caught.exception))
        self.abandon("the fix has to go out with it")
        # An ordinary item that names it, as one that depends on it does, is not its fix.
        after = self.run_of("after", "D-1", scope="Builds on V-1. Depends: V-1.")
        self.land(after, "D-1")
        self.open_batch()
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("this batch holds no item that fixes it forward (an item of a `--repair` run whose text names it)", str(caught.exception))
        self.abandon("a dependent is not a fix")
        # Nor is a repair that is about something else.
        unrelated = self.run_of("repair-9", "TRN-9", scope="Repair batch 9.", repair=True)
        self.land(unrelated, "TRN-9")
        self.open_batch()
        with self.assertRaises(Refusal):
            self.promote()
        self.abandon("not this deploy's fix")
        fix = self.run_of("fix-1", "F-1", verify="true", scope="Put right the deploy that V-1 broke.", repair=True)
        self.land(fix, "F-1")
        self.open_batch()
        code, out = self.promote()
        self.assertEqual((code, out["items"]), (0, ["A-1", "D-1", "TRN-9", "F-1"]))
        # A Verify that has not been run at all still holds everything, a fix in the batch or not.
        item = items.load_item(checked, "V-1")
        del item["fields"]["verify"]
        items.save_item(checked, item)
        other = self.run_of("fix-2", "F-2", scope="Also for V-1.", repair=True)
        self.land(other, "F-2")
        self.open_batch()
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("V-1 (batch 1, verify=None): its Verify is not recorded as passed", str(caught.exception))

    def test_a_base_that_moved_in_the_moment_of_the_merge_is_seen_and_said(self):
        # Nothing here can stop a push to the base between the last look and GitHub's merge; it must not pass unseen.
        self.land(self.run, "A-1")
        opened = self.open_batch()[1]
        real, pushed = self.tools.gh, []

        def gh(argv, *args, **kwargs):
            if argv[:2] == ["pr", "merge"] and not pushed:
                pushed.append(self.push("main", "sneaked.txt", "x\n", "pushed straight to the base"))
            return real(argv, *args, **kwargs)

        self.tools.gh = gh
        code, out = self.promote()
        self.tools.gh = real
        self.assertEqual((code, out["result"], out["tree_is_the_tested_one"]), (0, "promoted", False))
        self.assertIn("its tree is not the tree the checks ran on", out["warning"])
        self.assertNotEqual(self.tree("main"), self.tree(opened["head"]))
        self.assertFalse(read_json(train.batch_path(self.root, REPO, 1))["tree_is_the_tested_one"])
        # And a promotion nothing interfered with says the tree is the tested one, and warns of nothing.
        self.land(self.run, "A-2")
        self.open_batch()
        out = self.promote()[1]
        self.assertEqual((out["tree_is_the_tested_one"], "warning" in out), (True, False))

    def test_a_tested_tree_that_cannot_be_read_is_not_taken_for_the_same(self):
        self.land(self.run, "A-1")
        self.open_batch()
        with mock.patch("hl.train._tree", return_value=""):
            out = self.promote()[1]
        self.assertEqual((out["result"], out["tree_is_the_tested_one"], "warning" in out), ("promoted", False, True))

    def test_a_batch_merged_somewhere_other_than_the_base_is_not_a_promotion(self):
        self.land(self.run, "A-1")
        opened = self.open_batch()[1]
        state = read_json(self.gh_state)
        state["prs"][str(opened["pr"])]["baseRefName"] = self.TRAIN
        self.gh_state.write_text(json.dumps(state))
        self.tools.gh(["pr", "merge", str(opened["pr"]), "--repo", REPO, "--merge", "--subject", "by hand", "--body", "x"], self.clone)
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("merged outside this script, and not as the commit that was tested", str(caught.exception))
        self.assertIsNone(self.field("A-1"))

    def test_one_command_at_a_time_for_a_repository_and_a_live_promotions_lock_is_not_freed(self):
        self.land(self.run, "A-1")
        self.open_batch()
        token = merge.acquire_lock(self.tools, self.clone, "train:canary", "batch-1", ref=TEST_REF)
        write_json(train.state_dir(self.root, REPO) / train.LOCK_FILE, token)
        with train.one_command(self.root, REPO):
            for command in (self.promote, self.abandon, self.repair, self.open_batch):
                with self.assertRaises(Refusal) as caught:
                    command()
                self.assertIn("another hl-train command is at work on this repository", str(caught.exception))
            # The promotion that holds the command still holds its merge lock.
            self.assertEqual(merge.lock_remote_sha(self.tools, self.clone, TEST_REF), token["held_sha"])
            self.assertTrue((train.state_dir(self.root, REPO) / train.LOCK_FILE).exists())
        # Once it has ended (here: died), the next command frees what it left.
        self.assertEqual(self.promote()[0], 0)

    def test_what_a_command_cut_short_at_its_last_line_left_is_finished_by_the_next(self):
        # Found in review: the batch said promoted while the train still called it open and still called itself red.
        self.land(self.run, "A-1")
        opened = self.open_batch()[1]
        self.checks(opened["pr"], *RED)
        self.repair()
        self.assertEqual((train.load_state(self.root, REPO)["red"], train.red_file(self.root, REPO).exists()), (1, True))
        self.checks("*", *GREEN)
        self.open_batch()
        with mock.patch("hl.train._closed", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.promote()
        state = train.load_state(self.root, REPO)
        self.assertEqual((read_json(train.batch_path(self.root, REPO, 2))["state"], state["open"], state["red"], self.field("A-1")), ("promoted", 2, 1, "2"))
        # Any next command settles it before it does anything else.
        with self.assertRaises(Refusal) as caught:
            self.promote()
        self.assertIn("no batch is being tested", str(caught.exception))
        state = train.load_state(self.root, REPO)
        self.assertEqual((state["open"], state["red"], state["last_promoted"], train.red_file(self.root, REPO).exists()), (None, None, 2, False))
        # A batch that is still being tested is left alone by it.
        self.land(self.run, "A-2")
        self.open_batch()
        train.settle(self.root, REPO)
        self.assertEqual(train.load_state(self.root, REPO)["open"], 3)

    def test_two_conflicts_before_any_batch_get_an_item_each(self):
        self.land(self.run, "A-1", name="shared.txt", text="the item's line\n")
        self.push("main", "shared.txt", "someone else's line\n", "a change to the same file")
        first = self.open_batch()[1]
        second = self.open_batch()[1]
        self.assertEqual((first["repair_id"], second["repair_id"]), ("TRC-1", "TRC-2"))
        self.assertNotEqual(first["repair_file"], second["repair_file"])
        self.assertTrue(Path(first["repair_file"]).exists() and Path(second["repair_file"]).exists())

    def test_github_not_answering_for_one_merge_is_said_as_what_it_is(self):
        self.land(self.run, "A-1")
        record = items.item_dir(self.run, "A-1") / "merge.json"
        write_json(record, {**read_json(record), "merge_sha": None})
        with mock.patch("hl.train._gh_json", side_effect=RuntimeError("gh pr view failed: 502")):
            with self.assertRaises(Refusal) as caught:
                self.open_batch()
        self.assertIn("with no commit named", str(caught.exception))

    def test_a_change_to_the_trains_state_is_one_step(self):
        seen = train.update_state(self.root, REPO, lambda state: state["merge_backs"].append("a" * 40))
        train.update_state(self.root, REPO, lambda state: state.update(red=3))
        self.assertEqual((seen["merge_backs"], train.load_state(self.root, REPO)["merge_backs"], train.load_state(self.root, REPO)["red"]), (["a" * 40], ["a" * 40], 3))

    def test_a_base_that_no_longer_merges_into_the_train_comes_with_the_item_that_brings_it_in(self):
        self.land(self.run, "A-1", name="shared.txt", text="the item's line\n")
        self.push("main", "shared.txt", "someone else's line\n", "a change to the same file")
        code, out = self.open_batch()
        self.assertEqual((code, out["result"], out["repair_id"]), (4, "conflict", "TRC-1"))
        parsed = backlog.parse_backlog(Path(out["repair_file"]).read_text())
        self.assertEqual([(it.id, len(it.accept)) for it in parsed], [("TRC-1", 2)])
        self.assertIn("`shared.txt`", parsed[0].text)
        self.assertIn("git merge-base --is-ancestor origin/main HEAD", parsed[0].accept[0])
        self.assertIn("--repair --land merge", out["next"])
        self.assertIn("do not rebase and do not squash it", parsed[0].text)
        # Squashed in, the resolved file is on the train and the two histories are still apart: the conflict comes back.
        squashed = self.run_of("conflict-squashed", "TRC-9", repair=True)
        self.land(squashed, "TRC-9", name="shared.txt", text="someone else's line\nthe item's line\n")
        self.assertEqual(self.open_batch()[0], 4)
        # Merged whole, the train holds the base's history, and the batch is tested with everything in it.
        resolver = self.root / "runs" / "conflict-1"
        policy = items.policy(items.load_run(self.run))
        items.init_run(resolver, "conflict-1", REPO, [out["repair_file"]], policy={k: policy[k] for k in ("premise", "review", "bounds", "merge", "train")}, stream="app", repair=True, land="merge")
        self.assertEqual((items.load_run(resolver)["land"], items.load_run(resolver)["repair"]), ("merge", True))
        self.land(resolver, "TRC-1", name="shared.txt", text="someone else's line\nthe item's line\n", joined="main")
        code, out = self.open_batch()
        self.assertEqual((code, out["result"], out["items"]), (0, "testing", ["A-1", "TRC-9", "TRC-1"]))
        code, out = self.promote()
        self.assertEqual((code, out["tree_is_the_tested_one"]), (0, True))
        self.assertEqual(git(self.origin, "show", "main:shared.txt"), "someone else's line\nthe item's line")
        with self.assertRaises(Refusal) as caught:
            items.init_run(self.root / "runs" / "x", "x", REPO, [out["repair_file"]] if False else [str(self.tmp / "r1-ITEMS.md")], land="rebase")
        self.assertIn("land by squash or merge", str(caught.exception))


class AroundTheTrainTest(TrainCase):
    """What the rest of the loop does differently for a run on a train."""

    def setUp(self):
        super().setUp()
        self.run = self.run_of("r1", "A-1", "A-2", "A-3", "A-4", "A-5", verify="test -f A-1.txt && echo $HL_MERGE_SHA $HL_BASE_SHA")

    def test_the_merge_counts_what_the_train_holds_from_the_programs_own_records(self):
        run = items.load_run(self.run)
        self.assertEqual(merge._train_ahead(self.root, self.run, run), (0, 4))
        self.land(self.run, "A-1")
        self.land(self.run_of("r2", "B-1"), "B-1")
        # Without --root the program is the one the run directory lies in.
        self.assertEqual(merge._train_ahead(None, self.run, run), (2, 4))
        self.open_batch()
        self.promote()
        self.assertEqual(merge._train_ahead(None, self.run, run), (0, 4))
        loose = self.tmp / "loose-run"
        with self.assertRaises(Refusal) as caught:
            merge._train_ahead(None, loose, run)
        self.assertIn("its program was not found", str(caught.exception))

    def test_a_new_run_in_a_repository_with_a_train_merges_into_it_and_one_without_does_not(self):
        self.settings(repos={REPO: {"train": {"batch": 2, "limit": 4}, "item_checks": ["lint"]}, "owner/plain": {"facts": ["x"]}})
        self.assertEqual(
            program.run_policy(self.cfg, "Owner/Repo"),
            {"premise": "routed", "review": "code", "bounds": {}, "merge": "train", "train": {"branch": "hl-train/canary", "item_checks": ["lint"]}},
        )
        self.assertEqual(program.run_policy(self.cfg, "owner/plain"), {"premise": "routed", "review": "code", "bounds": {}})
        file = self.tmp / "C-ITEMS.md"
        file.write_text("## C-1 — x\n\n**Scope.** s\n\n**Accept.**\n1. one\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            items.main(["--run-dir", str(self.root / "runs" / "c"), "init", "--root", str(self.root), "--stream", "app", "--run-id", "c", "--repo", REPO, "--file", str(file)])
        said = json.loads(out.getvalue())["policy"]
        self.assertEqual((said["merge"], said["train"]), ("train", {"branch": "hl-train/canary", "item_checks": ["lint"]}))
        self.assertEqual(items.base_of(items.load_run(self.root / "runs" / "c"), "main"), "hl-train/canary")
        self.assertEqual(items.base_of({"policy": {}}, "main"), "main")

    def test_a_run_that_repairs_the_train_says_so_and_is_not_held_to_its_limit(self):
        # Found in review: a full train with a red batch could not take the one merge that would empty it.
        file = self.tmp / "R-ITEMS.md"
        file.write_text("## TRN-1 — x\n\n**Scope.** s\n\n**Accept.**\n1. one\n")
        with contextlib.redirect_stdout(io.StringIO()):
            items.main(["--run-dir", str(self.root / "runs" / "repair-1"), "init", "--root", str(self.root), "--stream", "app", "--run-id", "repair-1", "--repo", REPO, "--file", str(file), "--repair"])
        self.assertTrue(items.load_run(self.root / "runs" / "repair-1")["repair"])
        self.assertNotIn("repair", items.load_run(self.run))
        self.assertIn("| repair-1 | app |  | routed, code, train, repair |", ledger.items_text(self.root, "canary", "2026-10-13T03:00:00Z"))
        # Both are for a run on a train: one that merges into the base behaves exactly as it always has.
        for extra in ({"repair": True}, {"land": "merge"}):
            with self.assertRaises(Refusal) as caught:
                items.init_run(self.root / "runs" / "plain-x", "plain-x", REPO, [str(file)], policy={"premise": "routed", "review": "code", "bounds": {}}, **extra)
            self.assertIn("are for a run in a repository that has a train", str(caught.exception))
        self.assertFalse((self.root / "runs" / "plain-x" / "run.json").exists())

    def test_a_named_check_that_never_reports_can_be_dropped_for_a_run_with_a_reason(self):
        run = items.load_run(self.run)
        run["policy"]["train"]["item_checks"] = ["lint", "secret-scan"]
        write_json(self.run / "run.json", run)
        for name, note, why in (("lint", " ", "--note is required"), ("app-tests", "x", "is not one of the checks this run asks of an item (lint, secret-scan)")):
            with self.assertRaises(Refusal) as caught:
                items.drop_item_check(self.run, name, note)
            self.assertIn(why, str(caught.exception))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            items.main(["--run-dir", str(self.run), "drop-item-check", "--name", "lint", "--note", "its workflow runs only for pull requests into main"])
        self.assertEqual(json.loads(out.getvalue())["item_checks"], ["secret-scan"])
        run = items.load_run(self.run)
        self.assertEqual(items.policy(run)["train"]["item_checks"], ["secret-scan"])
        self.assertEqual((run["events"][-1]["status"], run["events"][-1]["note"]), ("item-check-dropped", "lint: its workflow runs only for pull requests into main"))
        plain = self.run_of("plain", "P-1")
        run = items.load_run(plain)
        del run["policy"]["merge"], run["policy"]["train"]
        write_json(plain / "run.json", run)
        with self.assertRaises(Refusal) as caught:
            items.drop_item_check(plain, "lint", "x")
        self.assertIn("this run merges into the base", str(caught.exception))

    def test_a_run_on_a_train_needs_reviews_that_read_the_code_and_a_branch(self):
        ok = {"premise": "routed", "review": "code", "merge": "train", "train": {"branch": "hl-train/p", "item_checks": [" lint "]}}
        self.assertEqual(items.check_policy(ok)["train"], {"branch": "hl-train/p", "item_checks": ["lint"]})
        for change, why in (
            ({"review": "blind"}, "needs reviews that read the code"),
            ({"train": {}}, "needs the train's branch"),
            ({"train": {"branch": " "}}, "needs the train's branch"),
            ({"train": {"branch": "b", "item_checks": "lint"}}, "must be a list of check names"),
            ({"train": {"branch": "b", "item_checks": ["", "x"]}}, "must be a list of check names"),
            ({"merge": "queue"}, "merge mode 'queue' is not one of item, train"),
        ):
            with self.assertRaises(Refusal) as caught:
                items.check_policy({**ok, **change})
            self.assertIn(why, str(caught.exception))
        # A run that merges into the base keeps no train, whatever was passed beside it.
        self.assertNotIn("train", items.check_policy({"premise": "routed", "review": "code", "train": {"branch": "b"}}))

    def verify_opts(self, *argv):
        return verify._parser().parse_args(["--root", str(self.root), "--run-dir", str(self.run), "--repo-dir", str(self.clone), "--id", "A-1", *argv])

    def run_verify(self, now, runner=verify.run_command):
        return verify.verify(self.root, self.run, self.clone, "A-1", self.tools, self.verify_opts(), clock=lambda: now, sleep=lambda s: None, runner=runner)

    def test_the_deploy_check_waits_for_the_promotion_and_counts_its_deadline_from_it(self):
        merged = self.land(self.run, "A-1")
        code, out = self.run_verify(0)
        self.assertEqual((code, out["result"]), (3, "not-promoted"))
        self.assertEqual(items.load_attempts(self.run, "A-1")["verify"], 0)
        self.open_batch()
        promoted = self.promote()[1]["promoted_sha"]
        at = verify._epoch(items.load_item(self.run, "A-1")["fields"]["promoted_at"])
        seen = {}

        def runner(command, cwd, env, timeout):
            seen.update(merge=env["HL_MERGE_SHA"], base=env["HL_BASE_SHA"])
            return verify.run_command(command, cwd, env, timeout)

        # Hours after the item's own merge, and a minute after the promotion: the five-minute wait has four left.
        code, out = self.run_verify(at + 60, runner)
        self.assertEqual((code, out["result"]), (0, "verified"))
        self.assertEqual(seen, {"merge": merged, "base": promoted})
        said = (items.item_dir(self.run, "A-1") / "verify-1.txt").read_text()
        self.assertIn(f"merge commit: {merged}\npromoted as: {promoted}\n", said)
        self.assertIn(f"{merged} {promoted}", said)
        self.assertEqual(read_json(items.item_dir(self.run, "A-1") / "verify-1.json")["deadline"], at + 300)

    def test_a_run_is_not_finished_while_a_merge_of_it_waits_on_the_train(self):
        for item_id in ("A-2", "A-3", "A-4", "A-5"):
            items.set_status(self.run, item_id, "skipped", "not in this test")
        self.land(self.run, "A-1")
        self.assertEqual(spawn.unshipped(self.run), ["A-1 is merged into the train and its batch has not been promoted"])
        self.open_batch()
        self.promote()
        self.assertEqual(spawn.unshipped(self.run), ["A-1 is merged but its Verify is not recorded as passed (field verify=None)"])
        items.record_verify(self.run, "A-1", "verified")
        self.assertEqual(spawn.unshipped(self.run), [])

    def test_the_ledger_says_which_merges_wait_on_the_train_and_which_batch_took_the_others(self):
        self.land(self.run, "A-1")
        self.open_batch()
        self.promote()
        self.land(self.run, "A-2")
        for item_id in ("A-1", "A-2"):
            item = items.load_item(self.run, item_id)
            item["started_at"] = "2026-10-12T11:00:00Z"
            items.save_item(self.run, item)
        self.assertEqual((ledger.item_record(self.run, "A-1")["promoted"], ledger.item_record(self.run, "A-2")["promoted"], ledger.item_record(self.run, "A-3")["promoted"]), ("1", "no", None))
        text = ledger.items_text(self.root, "canary", "2026-10-12T13:00:00Z")
        self.assertIn("| r1 | app |  | routed, code, train |", text)
        self.assertIn("| A-1 | r1 | merged (batch 1) |", text)
        self.assertIn("| A-2 | r1 | merged (on the train) |", text)

    def wait_opts(self, *argv):
        return wait._parser().parse_args(["--run-dir", str(self.run), "--root", str(self.root), "--train", REPO, "--interval", "0", "--checks-interval", "0", *argv])

    def test_a_wait_on_a_train_ends_when_the_batchs_checks_have_all_finished(self):
        with self.assertRaises(Refusal) as caught:
            wait.batch_event(self.root, REPO, self.tools, 0, 300)
        self.assertIn("no batch of owner/repo is being tested", str(caught.exception))
        self.land(self.run, "A-1")
        opened = self.open_batch()[1]
        self.checks(opened["pr"], {"name": "app-tests", "bucket": "pending", "state": "IN_PROGRESS", "link": ""})
        self.assertIsNone(wait.batch_event(self.root, REPO, self.tools, 0, 300))
        event = wait.wait(self.run, [], [], None, self.tools, self.wait_opts("--max-min", "0"), sleep=lambda s: None)
        self.assertEqual((event["event"], event["watching"]), ("time", ["owner/repo:batch"]))
        self.checks(opened["pr"], *GREEN)
        event = wait.wait(self.run, [], [], None, self.tools, self.wait_opts(), sleep=lambda s: None)
        self.assertEqual((event["event"], event["batch"], event["pr"], event["passed"]), ("batch", 1, opened["pr"], True))
        self.assertIn("promote", event["next"])
        self.checks(opened["pr"], *RED)
        self.assertFalse(wait.batch_event(self.root, REPO, self.tools, 0, 300)["passed"])
        # A base that requires nothing reports nothing: waited out, then said.
        self.gh(required={})
        self.assertIsNone(wait.batch_event(self.root, REPO, self.tools, 10, 300))
        event = wait.batch_event(self.root, REPO, self.tools, 301, 300)
        self.assertEqual((event["passed"], event["none_reported"]), (False, True))

    def test_a_wait_on_a_train_needs_the_program(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = wait.main(["--run-dir", str(self.run), "--train", REPO])
        self.assertEqual(code, 2)
        self.assertIn("--train needs --root", json.loads(out.getvalue())["refused"][0])


class PrepareOnATrainTest(TrainCase):
    def opts(self, item_id):
        return flow._parser().parse_args(["--root", str(self.root), "--run-dir", str(self.run), "prepare", "--id", item_id, "--port", "none"])

    def setUp(self):
        super().setUp()
        self.run = self.run_of("r1", "A-1", "A-2")

    def test_an_item_is_built_on_the_train_which_is_made_when_the_first_item_is_prepared(self):
        self.assertNotIn(self.TRAIN, git(self.origin, "for-each-ref", "--format=%(refname:short)"))
        code, out = flow.prepare(self.root, self.run, "A-1", self.tools, self.opts("A-1"))
        self.assertEqual((code, out["result"], out["base"], out["merge"]), (0, "prepared", self.TRAIN, "train"))
        self.assertEqual(self.sha(self.TRAIN), self.sha("main"))
        # The second item starts from what the first merged, which the base does not hold yet.
        first = self.land(self.run, "A-1")
        code, out = flow.prepare(self.root, self.run, "A-2", self.tools, self.opts("A-2"))
        worktree = out["worktree"]
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), first)
        self.assertNotEqual(first, self.sha("main"))
        self.assertEqual(items.load_item(self.run, "A-2")["fields"]["base_sha"], first)

    def test_a_clone_told_to_follow_only_the_base_still_gets_the_train(self):
        git(self.clone, "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
        self.land(self.run, "A-1")
        git(self.clone, "update-ref", "-d", f"refs/remotes/origin/{self.TRAIN}")
        code, out = flow.prepare(self.root, self.run, "A-2", self.tools, self.opts("A-2"))
        self.assertEqual(git(out["worktree"], "rev-parse", "HEAD"), self.sha(self.TRAIN))

    def test_whether_an_item_on_a_train_has_checks_to_wait_for_is_what_its_repository_says(self):
        run = items.load_run(self.run)
        # The pull request is there first: whether it conflicts with the train is asked of it before any check.
        self.land(self.run, "A-1")
        self.assertEqual(flow._checks_state(self.tools, self.clone, run, 1), ("green", []))
        self.assertEqual(self.calls("pr", "checks"), [])
        run["policy"]["train"]["item_checks"] = ["lint"]
        self.assertEqual(flow._checks_state(self.tools, self.clone, run, 1)[0], "running")
        self.checks(1, {"name": "lint", "bucket": "pass", "state": "SUCCESS", "link": ""}, {"name": "app-tests", "bucket": "fail", "state": "FAILURE", "link": ""})
        self.assertEqual(flow._checks_state(self.tools, self.clone, run, 1)[0], "green")
        self.checks(1, {"name": "lint", "bucket": "fail", "state": "FAILURE", "link": ""})
        self.assertEqual(flow._checks_state(self.tools, self.clone, run, 1)[0], "red")


    def test_a_pull_request_that_conflicts_with_the_train_is_said_even_where_no_check_is_asked(self):
        run = items.load_run(self.run)
        self.land(self.run, "A-1")
        state = read_json(self.gh_state)
        write_json(self.gh_state, {**state, "merge_state": {"1": "DIRTY"}})
        self.assertEqual(flow._checks_state(self.tools, self.clone, run, 1), ("conflicting", [{"base": self.TRAIN}]))
        self.assertEqual(self.calls("pr", "checks"), [])


class CommandLineTest(TrainCase):
    def call(self, *argv):
        out = io.StringIO()
        with mock.patch("hl.claim.check_pins", return_value={"bins": {"git": self.tools.git_bin, "gh": self.tools.gh_bin}}):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = train.main(["--root", str(self.root), "--repo", REPO, "--lock-ref", TEST_REF, "--merge-poll", "0.01", *argv])
        return code, json.loads(out.getvalue())

    def test_the_four_commands_from_the_command_line(self):
        run = self.run_of("r1", "A-1", "A-2")
        self.land(run, "A-1")
        self.land(run, "A-2")
        code, said = self.call("status")
        self.assertEqual((code, said["due"], said["items"]), (0, True, ["A-1", "A-2"]))
        code, said = self.call("test", "--note", "due")
        self.assertEqual((code, said["result"]), (0, "testing"))
        code, said = self.call("abandon", "--note", "to show it")
        self.assertEqual((code, said["result"]), (0, "abandoned"))
        self.assertEqual(self.call("test")[1]["batch"], 2)
        code, said = self.call("repair")
        self.assertEqual(code, 2)
        self.assertIn("are green", said["refused"][0])
        code, said = self.call("promote")
        self.assertEqual((code, said["result"], said["items"]), (0, "promoted", ["A-1", "A-2"]))
        code, said = self.call("promote")
        self.assertEqual((code, said["result"]), (2, "refused"))

    def test_the_clone_used_is_one_of_the_repositorys_own_streams(self):
        other = self.tmp / "other-clone"
        git(self.tmp, "clone", "-q", str(self.origin), str(other))
        self.settings(streams={"docs": {"repo": "owner/docs", "clone": str(other)}, "app": {"repo": "Owner/Repo", "clone": str(self.clone)}})
        self.assertEqual(train.clone_of(self.cfg, REPO), self.clone)
        self.assertEqual(train.clone_of(self.cfg, "owner/docs"), other)
        self.assertEqual(train.clone_of(self.cfg, "owner/elsewhere", given=str(other)), other)
        with self.assertRaises(Refusal) as caught:
            train.clone_of(self.cfg, REPO, given=str(self.tmp))
        self.assertIn("is not a clone of owner/repo", str(caught.exception))

    def test_a_repository_no_stream_works_in_needs_a_clone_named(self):
        self.settings(streams={})
        code, said = self.call("status")
        self.assertEqual(code, 2)
        self.assertIn("names no stream in owner/repo", said["refused"][0])
