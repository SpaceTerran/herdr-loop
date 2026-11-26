"""A merge through the base's merge queue (`merge_queue: true` in the
program's spec for the repository): an item's, and an outside pull
request's. GitHub's queue is the stand-in gh's `state["queue"]`."""

from __future__ import annotations

from hl import items, merge, outside, stage, verify
from hl.common import Refusal, read_json, write_json

from . import test_stage
from .support import SpawnCase
from .test_merge import MergeCase
from .test_outside import OutsideCase

SETTINGS = {"program": "p", "base": "main", "worker_claude_ref": "op://a/b/c", "gh_token_ref": "op://a/b/c", "cursor_model": "m"}
MERGED_AT = "2026-11-20T01:30:00Z"
MERGE_SHA = "e" * 40
REMOVED = {"createdAt": "2026-11-20T01:20:00Z", "reason": "The merge group failed its required checks", "beforeCommit": {"oid": "b" * 40}}


class QueueCase(MergeCase):
    """An item graded PASS in a program whose spec says the repository merges through its queue."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.facts({"merge_queue": True})
        self.queue(enabled=True)

    def facts(self, facts):
        write_json(self.root / "program.json", {**SETTINGS, "repos": {"owner/repo": facts}})

    def queue(self, **changes):
        state = read_json(self.gh_state)
        state.setdefault("queue", {}).update(changes)
        write_json(self.gh_state, state)

    def pr(self, **changes):
        state = read_json(self.gh_state)
        state["pr"].update(changes)
        write_json(self.gh_state, state)

    def enqueued(self):
        return read_json(self.gh_state).get("queue", {}).get("enqueued", [])

    def merges(self):
        return [c for c in self.gh_calls() if c[:2] == ["pr", "merge"]]

    def run_merge(self, *extra):
        return super().run_merge("--root", str(self.root), *extra)

    def record(self):
        return read_json(self.item_dir() / "merge.json")

    def landed_by_queue(self, head=None):
        """What GitHub shows once the queue has merged the pull request."""
        self.queue(**{"in": False})
        self.pr(state="MERGED", mergeCommit={"oid": MERGE_SHA}, mergedAt=MERGED_AT, **({"headRefOid": head} if head else {}))


class ItemQueueTest(QueueCase):
    def test_without_the_fact_the_merge_is_gh_pr_merge_as_it_was(self):
        self.facts({})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))
        self.assertEqual(len(self.merges()), 1)
        self.assertEqual(self.enqueued(), [])
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["api", "graphql"]])

    def test_the_pull_request_is_queued_at_the_graded_head_and_the_item_stays_in_progress(self):
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.assertEqual(self.enqueued(), [self.head])
        self.assertEqual(self.merges(), [])
        record = self.record()
        self.assertEqual((record["state"], record["pr"], record["head_sha"], record["graded_n"]), ("QUEUED", 7, self.head, 1))
        self.assertEqual(record["entry"]["id"], "MQE_1")
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")
        self.assertIn(f"--queue A-1", result["next"])
        self.assertIn(f"--root {self.root}", result["next"])
        # The lock covers the queueing only: the queue's own wait holds nothing of the program.
        self.assertIsNone(self.remote_lock())
        self.assertFalse((self.item_dir() / "merge-lock.json").exists())

    def test_a_base_with_no_queue_is_refused_and_nothing_is_written(self):
        self.queue(enabled=False)
        with self.assertRaises(Refusal) as caught:
            self.run_merge()
        self.assertIn("`isMergeQueueEnabled` is false", str(caught.exception))
        self.assertFalse((self.item_dir() / "merge.json").exists())
        self.assertIsNone(self.remote_lock())

    def test_a_branch_behind_its_base_is_queued_with_a_warning(self):
        self.pr(mergeStateStatus="BEHIND")
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.assertIn("the merge queue tests it on", " ".join(result["warnings"]))
        self.facts({})
        self.item_dir().joinpath("merge.json").unlink()
        self.assertEqual(self.run_merge()[0], 4)

    def test_run_again_while_queued_it_says_so_and_asks_nothing_of_the_item(self):
        self.run_merge()
        # Red checks now would refuse a gate; a queued pull request is GitHub's, and no gate is run on it.
        self.set_gh_checks_red()
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.assertEqual(len(self.enqueued()), 1)
        self.assertEqual(self.record()["state"], "QUEUED")

    def set_gh_checks_red(self):
        state = read_json(self.gh_state)
        state["checks"] = [{"name": "ci", "bucket": "fail", "state": "FAILURE"}]
        write_json(self.gh_state, state)

    def test_merged_by_the_queue_it_is_recorded_as_of_githubs_time(self):
        self.run_merge()
        self.landed_by_queue()
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))
        record = self.record()
        self.assertEqual((record["state"], record["merge_sha"], record["at"], record["queued"]), ("MERGED", MERGE_SHA, MERGED_AT, True))
        self.assertNotIn("recovered", record)
        self.assertNotIn("warnings", result)
        item = items.load_item(self.run_dir, "A-1")
        self.assertEqual((item["status"], item["merged_at"]), ("merged", MERGED_AT))
        self.assertIn("merged by the merge queue", item["note"])
        # The deploy check waits from GitHub's merge, not from when it was read.
        self.assertEqual(verify.landed(items.load_run(self.run_dir), item, self.item_dir())["at"], MERGED_AT)

    def test_merged_by_the_queue_with_no_commit_named_yet_is_said_and_not_recorded(self):
        self.run_merge()
        self.landed_by_queue()
        self.pr(mergeCommit=None)
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (5, "merge-commit-unknown"))
        self.assertEqual(self.record()["state"], "QUEUED")

    def test_merged_at_another_head_than_was_queued_is_not_recorded(self):
        self.run_merge()
        self.landed_by_queue(head="d" * 40)
        with self.assertRaises(Refusal) as caught:
            self.run_merge()
        self.assertIn("merged outside the gate", str(caught.exception))
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")
        # Written as what it is, so it holds nothing more, and the item can be marked failed as the refusal says.
        record = self.record()
        self.assertEqual((record["state"], record["merged_head"], record["merge_sha"]), (merge.MERGED_ELSEWHERE, "d" * 40, MERGE_SHA))
        self.assertIsNone(merge.queued_record(self.run_dir, "A-1"))
        items.set_status(self.run_dir, "A-1", "failed", "operator: merged outside the gate")

    def test_queued_at_one_head_and_merged_at_another_it_no_longer_holds_the_run(self):
        # Before the fix the record stayed QUEUED: once the item was marked failed as told, nothing else of the run merged again.
        other = items.load_item(self.run_dir, "A-2")
        other["status"] = "in_progress"
        other["fields"]["pr"] = "7"
        items.save_item(self.run_dir, other)
        write_json(self.item_dir("A-2") / "merge.json", {"state": "QUEUED", "pr": 7, "head_sha": "c" * 40, "graded_n": 1, "at": "2026-11-20T01:00:00Z"})
        self.assertIn("A-2 waits in the merge queue", self.refusal())
        self.queue(**{"in": False})
        self.pr(state="MERGED", mergeCommit={"oid": MERGE_SHA}, mergedAt=MERGED_AT, headRefOid="d" * 40)
        opts = self.opts()
        opts.root = self.root
        with self.assertRaises(Refusal):
            merge.merge(self.run_dir, "A-2", self.repo, self.tools, opts)
        items.set_status(self.run_dir, "A-2", "failed", "operator: merged outside the gate")
        self.pr(state="OPEN", mergedAt=None, mergeCommit=None, headRefOid=self.head)
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)

    def test_taken_out_of_the_queue_is_exit_6_with_githubs_reason_and_frees_the_run(self):
        self.run_merge()
        self.queue(**{"in": False, "removed": [REMOVED]})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (merge.EXIT_REMOVED, "removed-from-queue"))
        self.assertIn("The merge group failed its required checks", result["reason"])
        record = self.record()
        self.assertEqual((record["state"], record["removed_at"]), ("REMOVED-FROM-QUEUE", REMOVED["createdAt"]))
        self.assertIsNone(merge.queued_record(self.run_dir, "A-1"))
        # Said once: run again, the same head goes back in the queue.
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.assertEqual(self.enqueued(), [self.head, self.head])

    def test_a_closed_pull_request_is_taken_out(self):
        self.run_merge()
        self.queue(**{"in": False})
        self.pr(state="CLOSED")
        code, result = self.run_merge()
        self.assertEqual((code, result["reason"]), (merge.EXIT_REMOVED, "the pull request was closed"))

    def test_a_grade_newer_than_the_queued_one_goes_on_to_queue_its_head(self):
        self.run_merge()
        self.queue(**{"in": False, "removed": [REMOVED]})
        self.commit("feature.txt", "feature, reworked\n", "A-1: rework")
        self.head = self.commit("other.txt", "more\n", "A-1: more")
        from .test_merge import git

        git(self.repo, "push", "origin", self.BRANCH)
        self.pr(headRefOid=self.head)
        self.build(session="build-2")
        self.grade(session="eval-2", head=self.head)
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.assertEqual(self.enqueued()[-1], self.head)
        self.assertEqual((self.record()["head_sha"], self.record()["graded_n"]), (self.head, 2))

    def test_a_dry_run_on_a_queued_item_reads_and_writes_nothing(self):
        self.run_merge()
        self.queue(**{"in": False, "removed": [REMOVED]})
        code, result = self.run_merge("--dry-run")
        self.assertEqual(code, merge.EXIT_REMOVED)
        self.assertEqual(self.record()["state"], "QUEUED")
        self.landed_by_queue()
        code, result = self.run_merge("--dry-run")
        self.assertEqual((code, result["result"]), (0, "queue-merged"))
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")

    def test_a_queue_github_cannot_read_is_a_refusal_to_run_again(self):
        self.run_merge()
        self.queue(error="Something went wrong while executing your query")
        with self.assertRaises(Refusal) as caught:
            self.run_merge()
        self.assertIn("Something went wrong", str(caught.exception))
        self.assertIn("run this again", str(caught.exception))
        self.assertEqual(self.record()["state"], "QUEUED")

    def test_a_queued_record_is_read_whatever_the_spec_says_now(self):
        self.run_merge()
        self.facts({})
        self.landed_by_queue()
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], self.record()["queued"]), (0, "merged", True))

    def test_while_one_item_waits_in_the_queue_nothing_else_of_its_run_merges(self):
        write_json(self.item_dir("A-2") / "merge.json", {"state": "QUEUED", "pr": 9, "head_sha": "c" * 40, "graded_n": 1, "at": "2026-11-20T01:00:00Z"})
        self.assertIn("A-2 waits in the merge queue (pull request #9", self.refusal())
        write_json(self.item_dir("A-2") / "merge.json", {"state": "REMOVED-FROM-QUEUE", "pr": 9})
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)

    def test_a_queue_that_gives_no_answer_to_the_enqueue_is_a_failed_merge_to_run_again(self):
        real = merge.enqueue

        def lost(*args, **kwargs):
            raise RuntimeError("could not put pull request #7 in the merge queue: HTTP 502")

        merge.enqueue = lost
        self.addCleanup(setattr, merge, "enqueue", real)
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (5, "merge-failed"))
        self.assertEqual(self.record()["state"], "MERGE-COMMAND-FAILED")
        self.assertIsNone(self.remote_lock())
        # GitHub had queued it all the same: run again, it is found there and not queued twice.
        merge.enqueue = real
        self.queue(**{"in": True, "entry": {"id": "MQE_1", "state": "AWAITING_CHECKS", "position": 2}})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.assertEqual(self.enqueued(), [])

    def test_only_a_merge_is_given_a_time(self):
        with self.assertRaises(Refusal):
            items.set_status(self.run_dir, "A-1", "failed", "x: y", at=MERGED_AT)


class QueueHoldTest(QueueCase):
    """What holds while a pull request is in the queue, and what clears it: from the fresh-session review of 2026-11-20."""

    def lose_answer(self, then=None):
        """GitHub queues it, and its answer to the queueing is an error; `then` runs before the error is raised."""
        real = merge.enqueue

        def queued_then_lost(tools, repo, slug, pr, head_sha):
            real(tools, repo, slug, pr, head_sha)
            if then:
                then()
            raise RuntimeError(f"could not put pull request #{pr} in the merge queue: HTTP 502")

        merge.enqueue = queued_then_lost
        self.addCleanup(setattr, merge, "enqueue", real)
        return real

    def held(self):
        """What holds the rest of the run (the gate) and outside merges (`outside.pending_items`): both read this record."""
        return merge.queued_record(self.run_dir, "A-1")

    def test_a_queued_item_is_neither_failed_nor_parked_until_hl_merge_has_read_it_out(self):
        self.run_merge()
        for status in ("failed", "parked"):
            with self.assertRaises(Refusal) as caught:
                items.set_status(self.run_dir, "A-1", status, "ci: waiting on the queue too long")
            self.assertIn("waits in the base's merge queue", str(caught.exception))
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")
        self.queue(**{"in": False, "removed": [REMOVED]})
        self.assertEqual(self.run_merge()[0], merge.EXIT_REMOVED)
        items.set_status(self.run_dir, "A-1", "parked", "ci: waiting on the queue too long")

    def test_an_item_not_in_progress_that_the_queue_merged_is_refused_with_nothing_written(self):
        # Before the fix merge.json said MERGED and the item stayed parked: no hold, and no deploy check.
        self.run_merge()
        item = items.load_item(self.run_dir, "A-1")
        item["status"] = "parked"
        items.save_item(self.run_dir, item)
        self.landed_by_queue()
        with self.assertRaises(Refusal) as caught:
            self.run_merge()
        self.assertIn("A-1 is parked", str(caught.exception))
        self.assertIn("nothing was written", str(caught.exception))
        self.assertEqual(self.record()["state"], "QUEUED")
        self.assertIsNotNone(merge.queued_record(self.run_dir, "A-1"))

    def test_the_record_holds_the_run_before_github_is_asked(self):
        real = merge.enqueue
        seen = {}

        def watching(tools, repo, slug, pr, head_sha):
            seen["record"] = read_json(self.item_dir() / "merge.json")
            seen["held"] = self.held()
            return real(tools, repo, slug, pr, head_sha)

        merge.enqueue = watching
        self.addCleanup(setattr, merge, "enqueue", real)
        self.run_merge()
        self.assertEqual(seen["record"]["state"], "QUEUED")
        self.assertIsNotNone(seen["held"])

    def test_an_error_from_github_where_it_did_queue_it_is_queued(self):
        # Before the fix this was MERGE-COMMAND-FAILED, which held nothing while the pull request could merge.
        self.lose_answer()
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.assertIn("HTTP 502", " ".join(result["warnings"]))
        self.assertEqual((self.record()["state"], self.record()["entry"]["id"]), ("QUEUED", "MQE_1"))
        self.assertIsNotNone(self.held())
        self.assertIsNone(self.remote_lock())

    def test_an_error_and_a_queue_that_cannot_be_read_leave_the_record_holding_until_it_is_read(self):
        real = self.lose_answer(then=lambda: self.queue(error="Something went wrong"))
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (5, "merge-failed"))
        self.assertIn("GitHub may have queued it", result["next"])
        self.assertEqual((self.record()["state"], self.record()["entry"]), ("QUEUED", None))
        self.assertIsNotNone(self.held())
        self.assertIsNone(self.remote_lock())
        # Read again, it is where GitHub has it; and merged by the queue first, it is recorded as of GitHub's time.
        merge.enqueue = real
        self.queue(error=None)
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], self.record()["entry"]["id"]), (0, "queued", "MQE_1"))
        self.assertEqual(self.enqueued(), [self.head])
        self.landed_by_queue()
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], self.record()["at"]), (0, "merged", MERGED_AT))
        self.assertNotIn("recovered", self.record())

    def test_a_head_that_moved_after_the_gate_is_not_queued_and_the_record_is_put_back(self):
        write_json(self.item_dir() / "merge.json", {"state": "REMOVED-FROM-QUEUE", "pr": 7, "head_sha": "c" * 40})
        real = merge.gate
        calls = []

        def gate_then_push(*args, **kwargs):
            ctx = real(*args, **kwargs)
            calls.append(ctx)
            if len(calls) == 2:
                # Pushed between the gate under the lock and the queueing.
                self.pr(headRefOid="f" * 40)
            return ctx

        merge.gate = gate_then_push
        self.addCleanup(setattr, merge, "gate", real)
        with self.assertRaises(Refusal) as caught:
            self.run_merge()
        self.assertIn("nothing was queued", str(caught.exception))
        self.assertEqual(self.enqueued(), [])
        self.assertEqual(self.record(), {"state": "REMOVED-FROM-QUEUE", "pr": 7, "head_sha": "c" * 40})
        self.assertIsNone(self.remote_lock())

    def test_a_removal_from_an_earlier_queueing_is_not_given_as_this_ones_reason(self):
        self.run_merge()
        self.queue(**{"in": False, "removed": [REMOVED]})
        self.assertEqual(self.run_merge()[0], merge.EXIT_REMOVED)
        self.queue(enqueued_at="2026-11-20T01:40:00Z")
        self.assertEqual(self.run_merge()[1]["result"], "queued")
        # Out again, and GitHub has written no removal since: the one at 01:20 is not this one's.
        self.queue(**{"in": False})
        code, result = self.run_merge()
        self.assertEqual(code, merge.EXIT_REMOVED)
        self.assertNotIn(REMOVED["createdAt"], result["reason"])
        self.assertIn("no reason since it was queued", result["reason"])
        self.assertIsNone(self.record()["removed_at"])

    def test_a_dry_run_where_the_base_has_no_queue_is_refused_as_the_run_would_be(self):
        self.queue(enabled=False)
        with self.assertRaises(Refusal) as caught:
            self.run_merge("--dry-run")
        self.assertIn("`isMergeQueueEnabled` is false", str(caught.exception))
        self.queue(enabled=True)
        self.assertEqual(self.run_merge("--dry-run")[1]["result"], "dry-run-passed")
        self.assertEqual(self.enqueued(), [])
        self.assertFalse((self.item_dir() / "merge.json").exists())

    def test_a_queued_item_is_at_work_and_not_waiting_to_merge(self):
        from hl import ledger

        attempts = items.load_attempts(self.run_dir, "A-1")
        attempts["log"][-1]["read"] = {"effective": "PASS", "at": "2026-11-20T00:50:00Z"}
        items.save_attempts(self.run_dir, "A-1", attempts)
        self.assertEqual(ledger.passed_unmerged(self.run_dir, "A-1"), "2026-11-20T00:50:00Z")
        self.run_merge()
        self.assertIsNone(ledger.passed_unmerged(self.run_dir, "A-1"))


class QueuedSpawnTest(SpawnCase):
    def test_nothing_is_started_for_an_item_whose_pull_request_is_in_the_queue(self):
        write_json(items.item_dir(self.run_dir, "A-1") / "merge.json", {"state": "QUEUED", "pr": 7, "head_sha": "c" * 40, "graded_n": 1, "at": "2026-11-20T01:00:00Z"})
        for phase in ("build", "grade"):
            with self.assertRaises(Refusal) as caught:
                self.start(phase=phase)
            self.assertIn("waits in the base's merge queue", str(caught.exception))
        self.assertEqual((self.herdr.called("tab", "create"), self.attempts()["build"]), ([], 0))
        write_json(items.item_dir(self.run_dir, "A-1") / "merge.json", {"state": "REMOVED-FROM-QUEUE", "pr": 7})
        self.assertTrue(self.start()["started"])


class QueueOnTest(QueueCase):
    def test_the_queue_is_used_only_where_the_fact_is_true_and_not_on_a_train(self):
        self.assertTrue(merge.queue_on({"merge_queue": True}, False))
        for facts, on_train in (({"merge_queue": True}, True), ({}, False), (None, False), ({"merge_queue": "true"}, False)):
            self.assertFalse(merge.queue_on(facts, on_train), (facts, on_train))


class OutsideQueueTest(OutsideCase):
    def setUp(self):
        super().setUp()
        self.settings(repos={"owner/repo": {"deploys": [], "merge_queue": True}})
        self.queue(enabled=True)

    def queue(self, **changes):
        state = read_json(self.gh_state)
        state.setdefault("queue", {}).update(changes)
        write_json(self.gh_state, state)

    def pr(self, **changes):
        state = read_json(self.gh_state)
        state["pr"].update(changes)
        write_json(self.gh_state, state)

    def d(self):
        return outside.record_dir(self.run_dir, 12)

    def enqueued(self):
        return read_json(self.gh_state).get("queue", {}).get("enqueued", [])

    def test_it_is_queued_and_its_intent_holds_the_repository_until_it_is_recorded(self):
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.assertEqual(self.enqueued(), [self.head])
        self.assertEqual(read_json(self.d() / "attempt.json")["state"], "QUEUED")
        self.assertTrue((self.d() / "intent.json").exists())
        self.assertIn("--queue-outside 12", result["next"])
        self.assertIn("waits in the merge queue", " | ".join(outside.pending(self.root, "owner/repo")))
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["pr", "merge"]])
        self.assertIsNone(self.remote_lock())

    def test_run_again_while_queued_it_says_so(self):
        self.run_merge()
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.assertEqual(len(self.enqueued()), 1)

    def test_merged_by_the_queue_it_is_recorded_as_of_githubs_time(self):
        self.run_merge()
        self.queue(**{"in": False})
        self.pr(state="MERGED", mergeCommit={"oid": MERGE_SHA}, mergedAt=MERGED_AT)
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))
        record = read_json(self.d() / "merge.json")
        self.assertEqual((record["merge_sha"], record["at"], record["queued"]), (MERGE_SHA, MERGED_AT, True))
        self.assertNotIn("recovered", record)
        self.assertFalse((self.d() / "intent.json").exists())
        self.assertFalse((self.d() / "attempt.json").exists())

    def test_taken_out_it_is_exit_6_and_no_longer_holds_the_repository(self):
        self.run_merge()
        self.queue(**{"in": False, "removed": [REMOVED]})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (merge.EXIT_REMOVED, "removed-from-queue"))
        self.assertIn("The merge group failed its required checks", result["reason"])
        self.assertEqual(read_json(self.d() / "attempt.json")["state"], "REMOVED-FROM-QUEUE")
        self.assertFalse((self.d() / "intent.json").exists())
        self.assertEqual(outside.pending(self.root, "owner/repo"), [])

    def test_an_intent_whose_pull_request_is_in_the_queue_is_kept_though_no_attempt_says_so(self):
        # A merge that queued it and died before it wrote its attempt: the intent is a merge under way, not a stale one.
        self.d().mkdir(parents=True)
        write_json(self.d() / "intent.json", {"head_sha": self.head, "run": "run-1", "at": "2026-11-20T01:00:00Z"})
        self.queue(**{"in": True, "entry": {"id": "MQE_9", "state": "QUEUED", "position": 1}})
        # Checks gone red would refuse a gate; a pull request in the queue is GitHub's, and is not gated again.
        state = read_json(self.gh_state)
        state["checks"] = [{"name": "ci", "bucket": "fail", "state": "FAILURE"}]
        write_json(self.gh_state, state)
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.assertTrue((self.d() / "intent.json").exists())
        self.assertEqual(read_json(self.d() / "attempt.json")["entry"]["id"], "MQE_9")
        self.assertEqual(self.enqueued(), [])

    def test_a_base_with_no_queue_is_refused_and_leaves_no_intent(self):
        self.queue(enabled=False)
        self.assertIn("`isMergeQueueEnabled` is false", self.refusal())
        self.assertFalse((self.d() / "intent.json").exists())
        self.assertEqual(outside.pending(self.root, "owner/repo"), [])

    def test_a_queued_item_holds_an_outside_merge(self):
        items.start_phase(self.run_dir, "A-1", "agree", session="eval-agree")
        items.set_fields(self.run_dir, "A-1", ["pr=7"])
        write_json(items.item_dir(self.run_dir, "A-1") / "merge.json", {"state": "QUEUED", "pr": 7, "head_sha": "c" * 40, "graded_n": 1, "at": "2026-11-20T01:00:00Z"})
        self.assertIn("run-1/A-1's pull request #7 waits in the merge queue", self.refusal())

    def test_a_branch_behind_its_base_is_queued(self):
        self.pr(mergeStateStatus="BEHIND")
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "queued"))
        self.settings(repos={"owner/repo": {"deploys": []}})
        self.queue(**{"in": False})
        (self.d() / "attempt.json").unlink()
        (self.d() / "intent.json").unlink()
        self.assertEqual(self.run_merge()[0], 4)

    def test_a_queued_pull_request_is_not_passed_over_and_stays_held(self):
        # Before the fix `hl-handoff decline` cleared its intent: the queue merged it, and nothing recorded it or checked its deploy.
        self.run_merge()
        gh = lambda args, timeout=None: self.tools.gh(args, self.repo, check=False)  # noqa: E731
        cfg = read_json(self.root / "program.json")
        with self.assertRaises(Refusal) as caught:
            outside.decline(cfg, "owner/repo", 12, "conflicts with HD-23", gh, root=self.root)
        self.assertIn("waits in the merge queue", str(caught.exception))
        self.assertTrue((self.d() / "intent.json").exists())
        self.assertIn("waits in the merge queue", " | ".join(outside.pending(self.root, "owner/repo")))
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["pr", "edit"]])
        # Read out of the queue, it is passed over as any other.
        self.queue(**{"in": False, "removed": [REMOVED]})
        self.assertEqual(self.run_merge()[0], merge.EXIT_REMOVED)
        self.assertEqual(outside.decline(cfg, "owner/repo", 12, "conflicts with HD-23", gh, root=self.root)["result"], "declined")

    def test_a_save_lists_it_as_in_the_queue_while_it_is(self):
        import json

        from .test_outside import fake

        listed = [{"number": 12, "url": "u12", "title": "Bump", "headRefName": "secfix/x", "baseRefName": "main", "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}]
        gh = fake({("pr", "list", "--repo", "owner/repo"): (0, json.dumps(listed), "")})
        cfg = {**read_json(self.root / "program.json"), "root": str(self.root)}
        self.assertNotIn("queued", outside.ready(cfg, gh)["prs"][0])
        self.run_merge()
        said = outside.ready(cfg, gh)["prs"][0]["queued"]
        self.assertIn("in the merge queue, put there by `hl-merge --outside` in run run-1", said)
        self.assertIn("`hl-wait --queue-outside 12`", said)

    def test_queued_from_one_run_the_answer_from_another_names_the_wait_that_reads_it(self):
        from pathlib import Path

        from hl import wait

        self.run_merge()
        other = self.root / "runs" / "run-2"
        items.init_run(other, "run-2", "owner/repo", [str(self.backlog)])
        code, result = outside.merge_outside(self.root, other, self.repo, 12, self.tools, self.opts())
        self.assertEqual(result["result"], "queued")
        self.assertIn(f"--run-dir {Path(self.run_dir).resolve()} ", result["next"])
        self.assertEqual(wait.queue_target(self.run_dir, pr=12)["pr"], 12)

    def test_a_wait_on_it_ends_when_the_queue_merges_it_and_says_once_it_is_recorded(self):
        from hl import wait

        self.run_merge()
        target = wait.queue_target(self.run_dir, pr=12)
        self.assertIsNone(wait.queue_event(self.run_dir, target, self.tools, self.root))
        self.queue(**{"in": False})
        self.pr(state="MERGED", mergeCommit={"oid": MERGE_SHA}, mergedAt=MERGED_AT)
        event = wait.queue_event(self.run_dir, target, self.tools, self.root)
        self.assertEqual((event["event"], event["outside"], event["merged"], event["merge_sha"]), ("queue", 12, True, MERGE_SHA))
        self.assertIn("--outside 12", event["next"])
        self.run_merge()
        event = wait.queue_event(self.run_dir, target, self.tools, self.root)
        self.assertEqual((event["recorded"], "next" in event), ("MERGED", False))

    def test_without_the_fact_nothing_asks_github_of_a_queue(self):
        self.settings(repos={"owner/repo": {"deploys": []}})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["api", "graphql"]])


class QueueWaitTest(QueueCase):
    """`hl-wait --queue`: it ends when the pull request is out of the merge queue, either way."""

    def wait_queue(self, *extra, target=None):
        from hl import wait

        opts = wait._parser().parse_args(["--run-dir", str(self.run_dir), "--interval", "1", "--checks-interval", "1", "--root", str(self.root), *extra])
        opts.queue_targets = [target or wait.queue_target(self.run_dir, item_id="A-1")]
        clock = {"t": 0.0}

        def sleep(s):
            clock["t"] += s

        return wait.wait(self.run_dir, [], [], None, self.tools, opts, clock=lambda: clock["t"], sleep=sleep)

    def test_a_wait_for_an_item_not_in_the_queue_is_refused(self):
        from hl import wait

        with self.assertRaisesRegex(Refusal, "A-1 is not waiting in the merge queue"):
            wait.queue_target(self.run_dir, item_id="A-1")

    def test_while_it_is_queued_the_wait_goes_on_to_its_time(self):
        self.run_merge()
        event = self.wait_queue("--max-min", "0.05")
        self.assertEqual((event["event"], event["watching"]), ("time", ["A-1:queue"]))

    def test_merged_by_the_queue_ends_the_wait_with_the_merge_and_the_command_that_records_it(self):
        self.run_merge()
        self.landed_by_queue()
        event = self.wait_queue()
        self.assertEqual((event["event"], event["item"], event["merged"], event["merge_sha"]), ("queue", "A-1", True, MERGE_SHA))
        self.assertIn(f"--id A-1` records it", event["next"])

    def test_taken_out_ends_the_wait_with_githubs_reason(self):
        self.run_merge()
        self.queue(**{"in": False, "removed": [REMOVED]})
        event = self.wait_queue()
        self.assertEqual((event["event"], event["removed"]), ("queue", True))
        self.assertIn("The merge group failed its required checks", event["reason"])

    def test_recorded_meanwhile_ends_the_wait(self):
        self.run_merge()
        self.landed_by_queue()
        from hl import wait

        target = wait.queue_target(self.run_dir, item_id="A-1")
        self.run_merge()
        event = self.wait_queue(target=target)
        self.assertEqual((event["event"], event["recorded"]), ("queue", "MERGED"))

    def test_a_removal_from_before_it_was_queued_is_not_its_reason(self):
        self.queue(removed=[REMOVED], enqueued_at="2026-11-20T01:40:00Z")
        self.run_merge()
        self.queue(**{"in": False})
        self.assertIn("no reason since it was queued", self.wait_queue()["reason"])
        newer = {**REMOVED, "createdAt": "2026-11-20T01:50:00Z", "reason": "The merge group was cancelled"}
        self.queue(removed=[REMOVED, newer])
        self.assertIn("The merge group was cancelled", self.wait_queue()["reason"])

    def test_github_failing_again_and_again_is_an_error(self):
        self.run_merge()
        self.queue(error="Something went wrong")
        event = self.wait_queue()
        self.assertEqual((event["event"], event["source"], event["item"]), ("error", "gh", "A-1"))

    def test_the_command_line_waits_on_the_queue_with_the_loops_token(self):
        import contextlib
        import io
        import json
        import os
        from unittest import mock

        from hl import wait

        self.run_merge()
        self.landed_by_queue()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HL_FAKE_GH": str(self.gh_state)}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = wait.main(["--run-dir", str(self.run_dir), "--queue", "A-1", "--gh", self.tools.gh_bin, "--checks-interval", "0", "--interval", "0"])
        self.assertEqual((code, json.loads(out.getvalue())["merged"]), (0, True))
        with mock.patch.dict(os.environ, {"HL_FAKE_GH": str(self.gh_state)}), contextlib.redirect_stdout(io.StringIO()) as said, contextlib.redirect_stderr(io.StringIO()):
            code = wait.main(["--run-dir", str(self.run_dir), "--queue-outside", "12", "--gh", self.tools.gh_bin])
        self.assertEqual((code, json.loads(said.getvalue())["event"]), (2, "refused"))


class QueuePromptTest(test_stage.StageCase):
    """The manager prompt says how a queued merge goes only where a repository of the program merges through one."""

    # Where each text goes: what the template has just before it, and just after.
    PLACES = (
        ("MERGE_QUEUE_START", "Resume at the interrupted step.", "\n6. Check capacity"),
        ("MERGE_QUEUE", "a rework as `next` says. Exit codes are on the card.", "\n   - **FAIL:** a rework."),
        ("MERGE_QUEUE_OUTSIDE", "wait for its checks, and run it again.", " Then `hl-verify … --outside <PR> --wait`"),
    )

    def prompt(self):
        return (self.root / "MANAGER-PROMPT.md").read_text()

    def test_without_a_queue_the_prompt_reads_as_it_did(self):
        self.spec["repos"] = {"owner/desk": {"text_fit_check": "full-ci / text-fit"}}
        self.write_spec()
        self.stage()
        prompt = self.prompt()
        for _key, before, after in self.PLACES:
            # Word for word: the placeholder leaves nothing behind, not even a space.
            self.assertIn(before + after, prompt)
        for words in ("--queue", "`queued`", "removed-from-queue", "merge_queue"):
            self.assertNotIn(words, prompt)

    def test_with_a_queue_each_place_says_how_a_queued_merge_goes(self):
        self.spec["repos"] = {"owner/desk": {"merge_queue": True}}
        self.write_spec()
        self.stage()
        prompt, texts = self.prompt(), stage.merge_queue_texts(read_json(self.root / "program.json"))
        for key, before, after in self.PLACES:
            self.assertTrue(texts[key].startswith(" In `owner/desk`"), key)
            self.assertIn(before + texts[key] + after, prompt)
        for key, words in (
            ("MERGE_QUEUE", "`hl-merge` puts the pull request in the base's merge queue at the graded head and answers `queued` at once"),
            ("MERGE_QUEUE", "the item stays in progress, nothing else of its run merges meanwhile, and its stream starts nothing new until it is out"),
            ("MERGE_QUEUE", "Add `--queue <ID>` to that run's wait, as `next` says"),
            ("MERGE_QUEUE", "run the same `hl-merge` again: it records the merge as of GitHub's (then Verify, step 9), or answers exit 6 `removed-from-queue`"),
            ("MERGE_QUEUE", "`references/troubles.md` says what that asks"),
            ("MERGE_QUEUE", "a branch behind its base does not hold a merge there"),
            ("MERGE_QUEUE_OUTSIDE", "`hl-merge --outside` answers `queued` in place of merging: add `--queue-outside <PR>` to that run's wait"),
            ("MERGE_QUEUE_OUTSIDE", "a check that failed in the merge group is the steward's, so pass it over (below); anything else, run it again"),
            ("MERGE_QUEUE_OUTSIDE", "Its intent holds the repository meanwhile"),
            ("MERGE_QUEUE_START", "an item whose review passed and that has not merged may be waiting in the merge queue: its `hl-merge` answers `queued` again, with the wait to arm (`--queue <ID>`)"),
            ("MERGE_QUEUE_START", "`hl-merge --outside <PR>` does the same (`--queue-outside <PR>`)"),
        ):
            self.assertIn(words, texts[key], key)

    def test_only_the_repositories_with_a_queue_are_named(self):
        texts = stage.merge_queue_texts({"repos": {"owner/b": {"merge_queue": True}, "owner/a": {"merge_queue": True}, "owner/c": {"setup": "make"}}})
        self.assertTrue(all(text.startswith(" In `owner/a` and `owner/b`,") for text in texts.values()), texts)
        self.assertEqual(stage.merge_queue_texts({"repos": {"owner/c": {}}}), {"MERGE_QUEUE": "", "MERGE_QUEUE_OUTSIDE": "", "MERGE_QUEUE_START": ""})
        self.assertEqual(stage.merge_queue_texts({}), {"MERGE_QUEUE": "", "MERGE_QUEUE_OUTSIDE": "", "MERGE_QUEUE_START": ""})
