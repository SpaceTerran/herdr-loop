"""The merge gate and the lock, against real git repositories and a stand-in
for `gh` that answers from a JSON file. Nothing here touches a network or the
live lock ref of any real repository."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from hl import claim, items, merge
from hl.common import Refusal, read_json, write_json

from .support import FAKE_GH, RunCase, allow_exemptions, install_fake_gh
from .test_claim import FakeProcs

TEST_REF = "refs/hl-test/merge-lock"


def git(cwd, *args, env=None):
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


class MergeCase(RunCase):
    """An origin, a clone with one branch pushed, and an item graded PASS on
    that branch's head."""

    BRANCH = "hl/run-1/A-1"

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"GH_TOKEN": "loop-token-for-tests"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "clone"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        git(self.tmp, "clone", str(self.origin), str(self.repo))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        self.commit("README.md", "base\n", "base")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "checkout", "-b", self.BRANCH)
        self.head = self.commit("feature.txt", "feature\n", "A-1: add the feature")
        git(self.repo, "push", "origin", self.BRANCH)

        self.gh_state = self.tmp / "gh.json"
        fake = install_fake_gh(self.tmp)
        self.tools = merge.Tools(gh=str(fake), env={**os.environ, "HL_FAKE_GH": str(self.gh_state)}, sleep=lambda s: None)
        self.set_gh()

        self.init()
        self.agree()
        self.build()
        items.set_fields(self.run_dir, "A-1", ["pr=7", f"branch={self.BRANCH}"])
        self.grade(head=self.head)

    def commit(self, name, text, message):
        (self.repo / name).write_text(text)
        git(self.repo, "add", name)
        git(self.repo, "commit", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def set_gh(self, pr=None, **top):
        state = {
            "pr": {
                "number": 7,
                "url": "https://example.invalid/pull/7",
                "state": "OPEN",
                "isDraft": False,
                "title": "A-1: add the feature",
                "body": "## Criteria\n\n1. one\n",
                "headRefName": self.BRANCH,
                "headRefOid": getattr(self, "head", None),
                "baseRefName": "main",
                "mergeStateStatus": "CLEAN",
                "autoMergeRequest": None,
                "mergeCommit": None,
            },
            "checks": [{"name": "ci", "bucket": "pass", "state": "SUCCESS"}],
        }
        state["pr"].update(pr or {})
        state.update(top)
        self.gh_state.write_text(json.dumps(state))

    def gh_calls(self):
        return read_json(self.gh_state).get("calls", [])

    def opts(self, *extra):
        argv = ["--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--id", "A-1", "--lock-ref", TEST_REF]
        return merge._parser().parse_args(argv + ["--lock-wait", "0.3", "--lock-poll", "0.05", *extra])

    def run_merge(self, *extra):
        return merge.merge(self.run_dir, "A-1", self.repo, self.tools, self.opts(*extra))

    def refusal(self):
        with self.assertRaises(Refusal) as caught:
            merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        return " | ".join(caught.exception.reasons)

    def remote_lock(self, ref=TEST_REF):
        return merge.lock_remote_sha(self.tools, self.repo, ref)

    def rewrite_head(self, *commit_args):
        """Amend the branch's one commit, keeping its patch, and push it as the pull request's head."""
        git(self.repo, "commit", "--amend", "--no-edit", *commit_args)
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "--force", "origin", self.BRANCH)
        self.set_gh(pr={"headRefOid": self.head})


class GateTest(MergeCase):
    def test_a_graded_pass_on_a_green_open_pr_passes_every_gate(self):
        ctx = merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        self.assertEqual((ctx["pr"], ctx["head_sha"], ctx["needs_update"]), (7, self.head, False))
        self.assertEqual(ctx["subject"], "A-1: First item (#7)")

    def test_a_fail_verdict_is_refused(self):
        self.grade(session="eval-2", verdict="FAIL", results=["fail", "pass"], head=self.head)
        self.assertIn("the latest grade is FAIL, not PASS", self.refusal())

    def merged_before(self, verify, item_id="A-2", command="true"):
        """Another item of the run, merged earlier, with or without a Verify line and with its deploy check as given."""
        other = items.load_item(self.run_dir, item_id)
        other["status"] = "merged"
        other["verify_command"] = command
        if verify:
            other["fields"]["verify"] = verify
        items.save_item(self.run_dir, other)

    def test_nothing_merges_on_top_of_a_merge_whose_deploy_check_has_not_passed(self):
        self.merged_before(None)
        self.assertIn("A-2 is merged and its Verify is not recorded as passed (verify=None)", self.refusal())
        self.merged_before("verify-failed")
        self.assertIn("A-2 is merged and its Verify is not recorded as passed (verify=verify-failed)", self.refusal())
        self.merged_before("verified")
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)
        # A red deploy that a later item fixed forward is settled too, or its run could never merge again.
        self.merged_before("fixed-forward")
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)

    def test_an_earlier_merge_with_no_verify_line_holds_nothing_up(self):
        self.merged_before(None, command=None)
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)

    def test_an_item_that_has_not_merged_holds_nothing_up(self):
        other = items.load_item(self.run_dir, "A-2")
        other["verify_command"] = "true"
        items.save_item(self.run_dir, other)
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)

    def test_counts_are_held_to_the_runs_own_bounds(self):
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "bounds": {"build": 3}}})
        self.build(session="build-2")
        self.build(session="build-3")
        self.grade(session="eval-3", head=self.head)
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["graded_n"], 2)
        # The same record under the kit's own bound of two builds does not merge.
        write_json(self.run_dir / "run.json", run)
        self.assertIn("build count 3 is past its bound of 2", self.refusal())

    def test_a_pass_with_a_criterion_not_checked_is_refused(self):
        self.grade(session="eval-2", results=["pass", "not_checked"], head=self.head)
        self.assertIn("the latest grade is INCONCLUSIVE", self.refusal())

    def test_a_head_that_moved_with_a_different_patch_is_refused(self):
        new_head = self.commit("feature.txt", "feature, changed after the grade\n", "late change")
        git(self.repo, "push", "origin", self.BRANCH)
        self.set_gh(pr={"headRefOid": new_head})
        self.assertIn("the patch differs: grade the new head", self.refusal())

    def test_a_head_that_moved_only_by_taking_main_is_accepted(self):
        git(self.repo, "checkout", "main")
        self.commit("other.txt", "another stream's work\n", "B-1: other stream")
        git(self.repo, "push", "origin", "main")
        git(self.repo, "checkout", self.BRANCH)
        git(self.repo, "merge", "--no-edit", "main")
        new_head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "origin", self.BRANCH)
        self.set_gh(pr={"headRefOid": new_head})
        ctx = merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        self.assertEqual(ctx["head_sha"], new_head)
        self.assertIn("identical patch", ctx["warnings"][0])

    def test_armed_auto_merge_is_refused(self):
        self.set_gh(pr={"autoMergeRequest": {"mergeMethod": "SQUASH"}})
        self.assertIn("auto-merge is armed", self.refusal())

    def test_a_commit_carrying_a_tools_trailer_is_refused(self):
        # What a Cursor worker did on the mini with both attribution settings off.
        self.rewrite_head("--trailer", "Co-authored-by: Cursor <cursoragent@cursor.com>")
        said = self.refusal()
        self.assertIn(f"commit {self.head[:12]} carries AI attribution", said)
        self.assertIn("Co-authored-by: Cursor", said)

    def test_a_commit_authored_under_a_tools_name_is_refused(self):
        self.rewrite_head("--author", "Cursor Agent <cursoragent@cursor.com>")
        self.assertIn(f"commit {self.head[:12]} has the author 'Cursor Agent <cursoragent@cursor.com>'", self.refusal())

    def test_an_attribution_free_rewrite_of_the_same_patch_is_accepted(self):
        graded = self.head
        self.rewrite_head("-m", "A-1: add the feature, reworded")
        ctx = merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        self.assertEqual((ctx["graded_sha"], ctx["head_sha"]), (graded, self.head))

    def test_a_branch_that_cannot_be_fetched_is_refused(self):
        self.set_gh(pr={"headRefName": "hl/run-1/gone"})
        with self.assertRaises(Refusal) as caught:
            merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        self.assertIn("could not fetch", str(caught.exception))

    def test_a_closed_or_draft_or_foreign_branch_pr_is_refused(self):
        self.set_gh(pr={"state": "CLOSED", "isDraft": True, "headRefName": "someone/else"})
        reasons = self.refusal()
        for expected in ("is CLOSED, not OPEN", "is a draft", "expected 'hl/run-1/A-1'"):
            self.assertIn(expected, reasons)

    def test_one_session_for_builder_and_evaluator_is_refused(self):
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        attempts["log"][-1]["session"] = "build-1"
        path.write_text(json.dumps(attempts))
        self.assertIn("also a builder's session", self.refusal())

    def test_a_counter_past_its_bound_is_refused(self):
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        attempts["build"] = 3
        attempts["log"][-1]["build"] = 3
        path.write_text(json.dumps(attempts))
        self.assertIn("build count 3 is past its bound of 2", self.refusal())

    def test_a_contract_changed_after_the_grade_is_refused(self):
        with open(self.item_dir() / "contract.md", "a") as fh:
            fh.write("\n3. An easier criterion added later.\n")
        self.assertIn("not the contract that was graded", self.refusal())

    def test_a_build_newer_than_the_latest_grade_is_refused(self):
        self.build()  # rework after a grade, not graded again
        self.assertIn("build 2 has not been graded", self.refusal())

    def test_attribution_in_the_pr_body_is_refused(self):
        self.set_gh(pr={"body": "Done.\n\nCo-authored-by: Cursor <cursoragent@cursor.com>"})
        self.assertIn("pull request body carries AI attribution", self.refusal())

    def push_head(self, name, text, message="A-1: more of the feature"):
        """One more commit on the pull request's branch, pushed as its head. A text of None adds what is already written."""
        if text is None:
            git(self.repo, "add", name)
            git(self.repo, "commit", "-m", message)
            self.head = git(self.repo, "rev-parse", "HEAD")
        else:
            self.head = self.commit(name, text, message)
        git(self.repo, "push", "origin", self.BRANCH)
        self.set_gh(pr={"headRefOid": self.head})

    def test_attribution_added_on_the_branch_is_refused(self):
        self.push_head("README.md", "base\nGenerated with [Claude Code](https://claude.com)\n")
        self.assertIn("the diff carries AI attribution", self.refusal())

    def test_attribution_hidden_from_a_text_conversion_is_still_read(self):
        # A clone may set a textconv driver; the diff scanned is the file's own lines.
        (self.repo / ".git" / "info" / "attributes").write_text("*.txt diff=blank\n")
        git(self.repo, "config", "diff.blank.textconv", "true")
        self.push_head("notes.txt", "Generated with [Claude Code](https://claude.com)\n")
        self.assertIn("the diff carries AI attribution", self.refusal())

    def test_the_diff_is_read_from_the_clone_not_from_github(self):
        # GitHub gives no diff of more than 300 files (the fake gh answers `pr diff` as it does then);
        # such a pull request passes all the same.
        for i in range(301):
            (self.repo / f"f{i:03}.txt").write_text(f"line {i}\n")
        self.push_head(".", None, "A-1: many files")
        self.grade(session="eval-2", head=self.head)
        merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        self.assertFalse([call for call in self.gh_calls() if call[:2] == ["pr", "diff"]])

    def test_attribution_only_on_the_base_is_not_the_pull_requests(self):
        # The diff runs from the merge base: what the base took in since the branch left it is not in the pull request.
        git(self.repo, "checkout", "main")
        self.commit("CHANGELOG.md", "Generated with [Claude Code](https://claude.com)\n", "base: a later change")
        git(self.repo, "push", "origin", "main")
        git(self.repo, "checkout", self.BRANCH)
        merge.gate(self.run_dir, "A-1", self.repo, self.tools)

    def test_a_head_the_clone_does_not_have_is_refused(self):
        self.set_gh(pr={"headRefOid": "0" * 40})
        self.assertIn("could not read the pull request diff: git merge-base", self.refusal())

    def test_a_human_coauthor_is_not_attribution(self):
        self.set_gh(pr={"body": "Co-authored-by: Pat Example <pat@example.invalid>"})
        merge.gate(self.run_dir, "A-1", self.repo, self.tools)

    def test_required_checks_must_exist_and_be_green(self):
        self.set_gh(checks=[{"name": "ci", "bucket": "pending", "state": "IN_PROGRESS"}], checks_rc=8)
        self.assertIn("required checks are not green: ci=pending", self.refusal())
        self.set_gh(checks=None)
        self.assertIn("no required checks are reported", self.refusal())
        merge.gate(self.run_dir, "A-1", self.repo, self.tools, allow_no_required_checks=True)

    def test_a_required_check_that_has_not_reported_yet_is_pending_not_missing(self):
        # `gh pr checks --required` lists only the required checks that have reported. A help-desk program's CI check
        # reports once its shards finish, so it was missing and the list read green before it was (a decision in the
        # help-desk catalog program's ledger).
        self.set_gh(required_names=["ci", "full-ci"], ruleset_names=["secret-scan"])
        self.assertIn("required checks are not green: full-ci=pending, secret-scan=pending", self.refusal())
        self.set_gh(required_names=["ci", "full-ci"], checks=[{"name": "ci", "bucket": "pass", "state": "SUCCESS"}, {"name": "full-ci", "bucket": "pass", "state": "SUCCESS"}])
        merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        # Before any has reported, each one the branch requires is named.
        self.set_gh(required_names=["ci"], checks=None)
        self.assertIn("required checks are not green: ci=pending", self.refusal())

    def test_the_checks_a_branch_requires_are_its_protections_and_its_rulesets(self):
        self.set_gh(required_names=["b", "a"], ruleset_names=["a", "c"])
        self.assertEqual(merge.required_names(self.tools, self.repo, "owner/repo", "release/2026"), ["a", "b", "c"])
        paths = [c[1] for c in self.gh_calls() if c[:1] == ["api"]]
        self.assertEqual(paths, ["repos/owner/repo/branches/release%2F2026/protection/required_status_checks", "repos/owner/repo/rules/branches/release%2F2026"])
        self.set_gh()
        self.assertEqual(merge.required_names(self.tools, self.repo, "owner/repo", "main"), [], "an unprotected branch (404) requires none")
        self.set_gh(protection_error="gh: Must have admin rights to Repository. (HTTP 403)")
        with self.assertRaises(RuntimeError) as caught:
            merge.required_names(self.tools, self.repo, "owner/repo", "main")
        self.assertIn("HTTP 403", str(caught.exception))

    def test_a_conflicting_pr_is_refused(self):
        self.set_gh(pr={"mergeStateStatus": "DIRTY"})
        self.assertIn("conflicts with its base", self.refusal())

    BLOCKED = "GitHub reports the pull request blocked (merge state BLOCKED) although "

    def test_a_pull_request_github_calls_blocked_is_refused_though_every_check_it_lists_is_green(self):
        # Every required check is green, and GitHub still waits for something else the branch requires.
        self.set_gh(pr={"mergeStateStatus": "BLOCKED"})
        said = self.refusal()
        self.assertEqual(said.count(" | "), 0)
        self.assertIn(self.BLOCKED + "every required check it lists is green. The base branch requires something else that has not happened", said)
        self.assertIn('troubles.md, "GitHub says blocked"', said)
        # The states GitHub merges in are let through as before.
        for state in ("CLEAN", "UNSTABLE", "HAS_HOOKS"):
            self.set_gh(pr={"mergeStateStatus": state})
            self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["merge_state"], state)

    def behind_its_checks(self, looks):
        """GitHub as it is a moment after a check turned green: its merge
        state still says what it said before, for this many more looks, and
        then CLEAN. Returns the looks taken and the pauses between them."""
        real, taken, pauses = self.tools.gh, [], []

        def gh(args, cwd, check=True):
            if args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "mergeStateStatus":
                taken.append(args)
                if len(taken) > looks:
                    self.set_gh()
            return real(args, cwd, check)

        self.tools.gh = gh
        self.tools.sleep = pauses.append
        return taken, pauses

    def test_a_merge_state_that_has_not_caught_up_with_green_checks_is_asked_again_and_not_taken_for_blocked(self):
        # The state is read before the checks are, and GitHub works it out a moment after a check changes.
        self.set_gh(pr={"mergeStateStatus": "BLOCKED"})
        taken, pauses = self.behind_its_checks(2)
        ctx = merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        self.assertEqual((ctx["merge_state"], ctx["needs_update"], ctx["warnings"]), ("CLEAN", False, []))
        self.assertEqual((len(taken), pauses), (3, [merge.BLOCKED_PAUSE_S] * 2))

    def test_a_merge_state_that_stays_blocked_is_asked_a_few_times_and_then_believed(self):
        self.set_gh(pr={"mergeStateStatus": "BLOCKED"})
        taken, pauses = self.behind_its_checks(99)
        self.assertIn(self.BLOCKED + "every required check it lists is green", self.refusal())
        self.assertEqual((len(taken), pauses), (merge.BLOCKED_LOOKS, [merge.BLOCKED_PAUSE_S] * (merge.BLOCKED_LOOKS - 1)))
        self.assertEqual((merge.BLOCKED_LOOKS, merge.BLOCKED_PAUSE_S), (4, 5))

    def test_a_merge_state_github_is_still_working_out_is_not_taken_for_unblocked(self):
        self.set_gh(pr={"mergeStateStatus": "BLOCKED"})
        real, taken = self.tools.gh, []

        def gh(args, cwd, check=True):
            if args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "mergeStateStatus":
                taken.append(args)
                self.set_gh(pr={"mergeStateStatus": "UNKNOWN"})
            return real(args, cwd, check)

        self.tools.gh = gh
        self.assertIn(self.BLOCKED, self.refusal())
        self.assertEqual(len(taken), merge.BLOCKED_LOOKS)

    def test_a_state_that_settles_as_behind_or_conflicting_is_answered_as_that(self):
        for settled, shown in (("BEHIND", None), ("DIRTY", "conflicts with its base")):
            self.set_gh(pr={"mergeStateStatus": "BLOCKED"})
            real = self.tools.gh

            def gh(args, cwd, check=True, settled=settled, real=real):
                if args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "mergeStateStatus":
                    self.set_gh(pr={"mergeStateStatus": settled})
                return real(args, cwd, check)

            self.tools.gh = gh
            if shown:
                said = self.refusal()
                self.assertIn(shown, said)
                self.assertNotIn("GitHub reports", said)
            else:
                self.assertTrue(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["needs_update"])
            self.tools.gh = real

    def test_a_draft_is_refused_for_being_a_draft_and_is_not_called_blocked(self):
        self.set_gh(pr={"mergeStateStatus": "BLOCKED", "isDraft": True})
        taken, _ = self.behind_its_checks(99)
        said = self.refusal()
        self.assertEqual((said, taken), ("pull request is a draft", []))

    def test_a_blocked_pull_request_whose_checks_are_not_green_is_refused_for_the_checks(self):
        self.set_gh(pr={"mergeStateStatus": "BLOCKED"}, checks=[{"name": "ci", "bucket": "pending", "state": "IN_PROGRESS"}], checks_rc=8)
        taken, _ = self.behind_its_checks(99)
        said = self.refusal()
        self.assertIn("required checks are not green: ci=pending", said)
        self.assertNotIn("GitHub reports", said)
        # Nothing is asked again: the checks themselves say why it is blocked.
        self.assertEqual(taken, [])

    def test_a_blocked_pull_request_is_refused_even_where_no_required_checks_were_said_to_be_expected(self):
        self.set_gh(pr={"mergeStateStatus": "BLOCKED"}, checks=None)
        said = self.refusal()
        self.assertIn("no required checks are reported", said)
        self.assertIn(self.BLOCKED + "it lists no required check", said)
        # The option answers "none are reported". It does not answer GitHub saying the pull request is blocked.
        with self.assertRaises(Refusal) as caught:
            merge.gate(self.run_dir, "A-1", self.repo, self.tools, allow_no_required_checks=True)
        self.assertEqual(caught.exception.reasons, [merge.blocked_reason("the pull request", [])])
        self.assertIn(self.BLOCKED + "it lists no required check", caught.exception.reasons[0])
        self.set_gh(checks=None)
        merge.gate(self.run_dir, "A-1", self.repo, self.tools, allow_no_required_checks=True)

    def test_a_closed_pull_request_is_refused_for_being_closed_and_not_for_its_merge_state(self):
        self.set_gh(pr={"state": "CLOSED", "mergeStateStatus": "BLOCKED"})
        said = self.refusal()
        self.assertIn("pull request #7 is CLOSED, not OPEN", said)
        self.assertNotIn("GitHub reports", said)

    def test_an_item_that_is_not_in_progress_is_refused(self):
        items.set_status(self.run_dir, "A-1", "parked", "operator: decision")
        self.assertIn("only an item in progress can merge", self.refusal())

    def test_the_merge_reads_the_protected_paths_and_the_items_exemption_as_they_stand_now(self):
        # Found in review: an exemption withdrawn after `built` let the merge through, because only the check after a
        # build read protected paths at all.
        facts = {"protected": ["feature.txt"]}
        allow_exemptions(self.run_dir, "feature.txt")
        with self.assertRaises(Refusal) as caught:
            merge.gate(self.run_dir, "A-1", self.repo, self.tools, repo_facts=facts)
        self.assertIn("the pull request changes paths no item may change: feature.txt", " | ".join(caught.exception.reasons))
        items.exempt_path(self.run_dir, "A-1", "feature.txt", "the item exists to change it", facts["protected"])
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools, repo_facts=facts)["pr"], 7)
        items.exempt_path(self.run_dir, "A-1", "feature.txt", "granted by mistake", facts["protected"], withdraw=True)
        with self.assertRaises(Refusal) as caught:
            merge.gate(self.run_dir, "A-1", self.repo, self.tools, repo_facts=facts)
        self.assertIn("the pull request changes paths no item may change: feature.txt", " | ".join(caught.exception.reasons))
        # Without protected paths on record the gate is as it was.
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools, repo_facts={})["pr"], 7)

    def test_the_merge_takes_the_protected_paths_from_the_programs_settings(self):
        root = self.tmp / "root"
        root.mkdir()
        settings = {"program": "p", "base": "main", "worker_claude_ref": "op://a/b/c", "gh_token_ref": "op://a/b/c", "cursor_model": "m"}
        write_json(root / "program.json", {**settings, "repos": {"owner/repo": {"protected": ["feature.txt"]}}})
        with self.assertRaises(Refusal) as caught:
            self.run_merge("--dry-run", "--root", str(root))
        self.assertIn("the pull request changes paths no item may change: feature.txt", " | ".join(caught.exception.reasons))
        write_json(root / "program.json", settings)
        self.assertEqual(self.run_merge("--dry-run", "--root", str(root))[1]["result"], "dry-run-passed")

    def test_every_failing_gate_is_named_in_one_refusal(self):
        self.set_gh(pr={"autoMergeRequest": {"x": 1}, "isDraft": True}, checks=None)
        self.assertEqual(self.refusal().count(" | "), 2)


class ReviewGateTest(MergeCase):
    """In a run whose evaluators review the code, a pass merges only when
    the reviewer's own transcript shows the change was exercised."""

    def setUp(self):
        super().setUp()
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "code"}})
        verdict_path = self.item_dir() / "verdict-1.json"
        write_json(verdict_path, {**read_json(verdict_path), "fix_list": []})
        write_json(self.item_dir() / "serve-1.json", {"served": True, "port": 8401})
        # Read, as `hl-flow reviewed` leaves a review that did nothing to its checkout.
        items.record_read(self.run_dir, "A-1", 1, {"checkout": [], "dirty_before": []})
        self.home = self.tmp / "home"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def reviewed(self, *calls):
        from .test_review import write_transcript

        write_transcript(self.home, "eval-1", list(calls))

    def test_a_pass_from_a_review_that_called_the_served_build_merges(self):
        self.reviewed(("Bash", {"command": "git diff origin/main...HEAD"}), ("Bash", {"command": "curl -s http://127.0.0.1:8401/ping"}))
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)

    def test_a_pass_with_a_criterion_finding_that_names_no_criterion_merges(self):
        # The case the verdict's shape now allows: such a finding is a note about the item's own text, and the pass stands.
        verdict_path = self.item_dir() / "verdict-1.json"
        note = {"kind": "criterion", "criterion": None, "severity": "low", "confidence": 0.7, "summary": "the Scope says 3 rows; the contract and the build say 4", "evidence": []}
        write_json(verdict_path, {**read_json(verdict_path), "findings": [note]})
        self.reviewed(("Bash", {"command": "curl -s http://127.0.0.1:8401/ping"}))
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)
        # One that names a criterion with anything but its number is still refused at the gate.
        write_json(verdict_path, {**read_json(verdict_path), "findings": [{**note, "criterion": "2"}]})
        self.assertIn("a finding of kind criterion has criterion '2'", self.refusal())

    def test_a_pass_from_a_review_that_only_read_the_diff_does_not_merge(self):
        self.reviewed(("Bash", {"command": "git diff origin/main...HEAD | cat"}), ("Read", {"file_path": "feature.txt"}))
        self.assertIn("review 1: the reviewer ran nothing", self.refusal())

    def test_a_pass_from_a_review_that_never_called_the_served_build_does_not_merge(self):
        self.reviewed(("Bash", {"command": "python3 -m pytest tests/test_feature.py"}))
        self.assertIn("review 1: the build was served on port 8401 and nothing in the review called it", self.refusal())

    def test_a_pass_whose_reviewer_left_no_transcript_does_not_merge(self):
        self.assertIn("transcript (session eval-1) cannot be found", self.refusal())

    def test_an_item_a_person_looks_at_does_not_merge_on_a_review_that_did_not_look(self):
        self.reviewed(("Bash", {"command": "curl -s http://127.0.0.1:8401/"}))
        item = items.load_item(self.run_dir, "A-1")
        item["route"] = {"premise": "independent", "look": True, "reason": "a page", "at": "2026-10-12T00:00:00Z"}
        items.save_item(self.run_dir, item)
        self.assertIn("this item changes something a person looks at, and the review did not look", self.refusal())

    def test_a_verdict_in_the_old_shape_does_not_merge_in_such_a_run(self):
        self.reviewed(("Bash", {"command": "curl -s http://127.0.0.1:8401/ping"}))
        verdict_path = self.item_dir() / "verdict-1.json"
        data = read_json(verdict_path)
        del data["fix_list"]
        write_json(verdict_path, data)
        self.assertIn("verdict-1.json: fix_list must be a list", self.refusal())
        # Only a confirm's refusal names the full review that follows it.
        self.assertNotIn("review the head in full", self.refusal())

    def read(self, **read):
        """What the first read of the review's checkout left on its attempt, as it would be after another read."""
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        entry = attempts["log"][-1]
        entry.pop("read", None)
        if read:
            entry["read"] = {"checkout": [], "dirty_before": [], "at": "2026-10-13T00:00:00Z", **read}
        write_json(path, attempts)

    def test_a_review_that_was_never_read_does_not_merge(self):
        # What the reader sees of the checkout, nothing can see later; so a merge waits for the read.
        self.reviewed(("Bash", {"command": "curl -s http://127.0.0.1:8401/ping"}))
        self.read()
        self.assertEqual(self.refusal(), "review 1 has not been read: `hl-flow reviewed --id A-1` reads it against its transcript and its checkout, and a merge rests on what that finds")
        # A file beside the attempt that says it was read, and read clean, is not the read.
        write_json(self.item_dir() / "review-1.json", {"problems": [], "checkout": [], "effective": "PASS"})
        self.assertIn("review 1 has not been read", self.refusal())

    def test_a_pass_that_was_read_over_a_checkout_that_changed_does_not_merge(self):
        self.reviewed(("Bash", {"command": "curl -s http://127.0.0.1:8401/ping"}))
        edited = "tracked files were changed in the checkout during the review: feature.txt"
        self.read(checkout=[edited])
        self.assertEqual(
            self.refusal(),
            f"review 1 does not count for a merge: {edited}; it never was a review of the head, so give the attempt back "
            f'(`hl-items void --id A-1 --phase grade --n 1 --note "<what changed the checkout>"`) and review the head again '
            '(`hl-flow review --id A-1`; troubles.md, "A review that does not count")',
        )
        self.read(checkout=[edited, "the checkout moved during the review", "a third"])
        self.assertIn(f"does not count for a merge: {edited} (and 2 more); it never was", self.refusal())
        # A file that already differed when the review began, and was left as it was, holds nothing up.
        self.read(dirty_before=["package-lock.json"])
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)

    def given_back(self, *notes):
        """Reviews of this item that passed over a changed checkout and were given back, as the attempt log keeps them."""
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        for k, note in enumerate(notes):
            voided = {"at": "2026-10-13T00:00:00Z", "note": note, "given_back": True, "set_aside": None}
            attempts["log"].insert(-1, {"phase": "grade", "n": 1, "build": 1, "session": f"gone-{k}", "head_sha": self.head, "voided": voided})
        write_json(path, attempts)

    def test_a_pass_over_a_changed_checkout_after_the_items_one_give_back_is_the_operators(self):
        # Found in the third review: the refusal named the void every time, and the void could be run every time.
        self.reviewed(("Bash", {"command": "curl -s http://127.0.0.1:8401/ping"}))
        edited = "tracked files were changed in the checkout during the review: feature.txt"
        self.given_back("the reviewer edited feature.txt")
        self.read(checkout=[edited])
        self.assertEqual(
            self.refusal(),
            f"review 1 does not count for a merge: {edited}; give_back bound reached for A-1: 1 of 1, so it is not given back again. "
            "Something changes the checkout whenever this item is reviewed: park it for the operator "
            f'(`hl-items set --id A-1 --status parked --note "environment: <what changes the checkout during a review>"`; '
            'troubles.md, "A tracked file changes during every review")',
        )
        # One given back is within the bound, and a review that then read clean merges.
        self.read()
        items.record_read(self.run_dir, "A-1", 1, {"checkout": [], "dirty_before": []})
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)
        # More on the log than the run's bound allows is not something the scripts made.
        self.given_back("again")
        self.assertEqual(self.refusal(), "give_back count 2 is past its bound of 1")

    def test_a_review_that_did_not_pass_is_refused_for_what_it_is_whatever_happened_to_its_checkout(self):
        self.reviewed(("Bash", {"command": "curl -s http://127.0.0.1:8401/ping"}))
        verdict_path = self.item_dir() / "verdict-1.json"
        data = read_json(verdict_path)
        fix = {"criterion": 1, "what": "prints 2", "where": "out.txt", "evidence": []}
        write_json(verdict_path, {**data, "verdict": "FAIL", "criteria": [{**c, "result": "fail" if c["n"] == 1 else c["result"]} for c in data["criteria"]], "fix_list": [fix]})
        self.read(checkout=["tracked files were changed in the checkout during the review: feature.txt"])
        self.assertEqual(self.refusal(), "the latest grade is FAIL, not PASS")

    def test_a_run_whose_evaluators_grade_blind_is_held_to_what_it_was(self):
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "blind"}})
        # No transcript, no exercise check: the kit's first behaviour.
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)
        # A grade read by hand has no read on its attempt, and merges as it always did.
        self.read()
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)

    def test_a_blind_grade_that_was_read_over_a_checkout_that_changed_does_not_merge_either(self):
        # `hl-flow reviewed` reads a blind grade's checkout too: a grade of an edited checkout is not a grade of the head.
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "blind"}})
        moved = "the checkout moved during the review: it is at 0123456789ab, the commit under review is ba9876543210"
        self.read(checkout=[moved])
        self.assertIn(f"review 1 does not count for a merge: {moved}; it never was a review of the head", self.refusal())


class MergeTest(MergeCase):
    def test_merge_squashes_the_graded_commit_and_records_it(self):
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))
        merge_call = next(c for c in self.gh_calls() if c[:2] == ["pr", "merge"])
        self.assertIn("--squash", merge_call)
        self.assertEqual(merge_call[merge_call.index("--match-head-commit") + 1], self.head)
        self.assertNotIn("--auto", merge_call)
        record = read_json(self.item_dir() / "merge.json")
        self.assertEqual((record["state"], record["merge_sha"]), ("MERGED", "f" * 40))
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "merged")
        self.assertIsNone(self.remote_lock())
        self.assertFalse((self.item_dir() / "merge-lock.json").exists())

    def test_attribution_that_github_put_on_the_merged_commit_is_recorded(self):
        self.set_gh(merged_message="A-1: First item (#7)\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n")
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))
        self.assertIn("the merged commit carries AI attribution", result["attribution_on_base"])

    def test_a_clean_merged_commit_records_no_attribution(self):
        self.assertIsNone(self.run_merge()[1]["attribution_on_base"])

    def test_a_dry_run_checks_the_gates_and_touches_nothing(self):
        code, result = self.run_merge("--dry-run")
        self.assertEqual((code, result["result"]), (0, "dry-run-passed"))
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")

    def test_a_merge_that_died_after_github_merged_is_recorded_by_running_it_again(self):
        # The process was killed between GitHub's answer and merge.json: the pull request is merged, the item does not know.
        token = merge.acquire_lock(self.tools, self.repo, "run-1", "A-1", ref=TEST_REF, wait_s=0.3, poll_s=0.05)
        (self.item_dir() / "merge-lock.json").write_text(json.dumps(token))
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}, "mergeStateStatus": "UNKNOWN"})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["recovered"], result["merge_sha"]), (0, "merged", True, "c" * 40))
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))
        record = read_json(self.item_dir() / "merge.json")
        self.assertEqual((record["state"], record["recovered"], record["head_sha"]), ("MERGED", True, self.head))
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "merged")
        # The lock its dead process left is freed with it.
        self.assertIsNone(self.remote_lock())
        self.assertFalse((self.item_dir() / "merge-lock.json").exists())

    def test_a_merged_head_that_took_main_after_the_grade_is_still_the_graded_patch(self):
        git(self.repo, "checkout", "main")
        self.commit("other.txt", "other\n", "B-1: something else landed")
        git(self.repo, "push", "origin", "main")
        git(self.repo, "checkout", self.BRANCH)
        git(self.repo, "merge", "--no-edit", "main")
        moved = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "origin", self.BRANCH)
        self.set_gh(pr={"state": "MERGED", "headRefOid": moved, "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["recovered"]), (0, "merged", True))

    def test_a_pull_request_merged_at_a_commit_nobody_graded_is_not_recorded_as_merged(self):
        other = self.commit("sneaked.txt", "not graded\n", "A-1: one more thing")
        git(self.repo, "push", "origin", self.BRANCH)
        self.set_gh(pr={"state": "MERGED", "headRefOid": other, "mergeCommit": {"oid": "c" * 40}})
        with self.assertRaises(Refusal) as caught:
            self.run_merge()
        said = str(caught.exception)
        self.assertIn(f"pull request #7 was merged outside this script at {other[:12]}, which is not the commit that was graded ({self.head[:12]})", said)
        self.assertIn("operator: merged outside the gate", said)
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")
        self.assertFalse((self.item_dir() / "merge.json").exists())

    def test_a_merged_pull_request_without_a_pass_is_refused_before_github_is_read(self):
        self.grade(session="eval-2", head=self.head, verdict="FAIL", results=["fail", "pass"])
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        with self.assertRaises(Refusal) as caught:
            self.run_merge()
        self.assertIn("the latest grade is FAIL, not PASS", str(caught.exception))
        self.assertEqual(self.gh_calls(), [])

    def test_a_dry_run_says_a_pull_request_is_already_merged_and_records_nothing(self):
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge("--dry-run")
        self.assertEqual((code, result["result"]), (0, "already-merged"))
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")
        self.assertFalse((self.item_dir() / "merge.json").exists())

    def test_a_closed_pull_request_is_still_refused(self):
        self.set_gh(pr={"state": "CLOSED"})
        with self.assertRaises(Refusal) as caught:
            self.run_merge()
        self.assertIn("pull request #7 is CLOSED, not OPEN", str(caught.exception))

    def test_a_branch_behind_its_base_is_sent_back_for_an_update(self):
        self.set_gh(pr={"mergeStateStatus": "BEHIND"})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (4, "needs-update"))
        self.assertIsNone(self.remote_lock())

    def test_a_held_lock_stops_the_merge_and_names_the_holder(self):
        merge.acquire_lock(self.tools, self.repo, "other-run", "B-9", ref=TEST_REF)
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["holder"]), (3, "lock-timeout", "other-run/B-9"))
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))
        self.assertIsNotNone(self.remote_lock())

    def test_a_failed_merge_command_releases_the_lock_and_leaves_the_item_open(self):
        self.set_gh(merge_rc=1)
        code, result = self.run_merge()
        self.assertEqual((code, result["state"]), (5, "MERGE-COMMAND-FAILED"))
        self.assertIsNone(self.remote_lock())
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")

    def test_a_blocked_pull_request_is_not_merged_and_the_lock_is_not_taken(self):
        self.set_gh(pr={"mergeStateStatus": "BLOCKED"})
        for extra in ((), ("--dry-run",), ("--allow-no-required-checks",)):
            with self.assertRaisesRegex(Refusal, "GitHub reports the pull request blocked"):
                self.run_merge(*extra)
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))
        self.assertIsNone(self.remote_lock())
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")

    def test_a_pull_request_that_turns_blocked_while_the_lock_is_waited_for_is_not_merged(self):
        real_acquire = merge.acquire_lock

        def acquire_then_block(*args, **kwargs):
            token = real_acquire(*args, **kwargs)
            # A required check was added to the branch's protection while this merge waited.
            self.set_gh(pr={"mergeStateStatus": "BLOCKED"})
            return token

        with mock.patch("hl.merge.acquire_lock", side_effect=acquire_then_block):
            with self.assertRaisesRegex(Refusal, "GitHub reports the pull request blocked"):
                self.run_merge()
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))
        self.assertIsNone(self.remote_lock())
        self.assertFalse((self.item_dir() / "merge-lock.json").exists())

    def test_the_cli_reports_a_blocked_pull_request_as_a_refusal_with_its_reason(self):
        self.set_gh(pr={"mergeStateStatus": "BLOCKED"})
        argv = ["--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--id", "A-1", "--gh", self.tools.gh_bin, "--lock-ref", TEST_REF]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HL_FAKE_GH": str(self.gh_state)}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = merge.main(argv)
        answer = json.loads(out.getvalue())
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertIn("The base branch requires something else that has not happened", answer["refused"][0])

    def test_a_gate_that_fails_under_the_lock_releases_it(self):
        real_gate = merge.gate
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise Refusal("auto-merge was armed while waiting for the lock")
            return real_gate(*args, **kwargs)

        merge.gate = flaky
        self.addCleanup(setattr, merge, "gate", real_gate)
        with self.assertRaisesRegex(Refusal, "armed while waiting"):
            self.run_merge()
        self.assertIsNone(self.remote_lock())
        self.assertFalse((self.item_dir() / "merge-lock.json").exists())

    def test_the_cli_reports_a_refusal_as_exit_2(self):
        self.set_gh(pr={"isDraft": True})
        argv = ["--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--id", "A-1", "--gh", self.tools.gh_bin]
        out = io.StringIO()
        os.environ["HL_FAKE_GH"] = str(self.gh_state)
        self.addCleanup(os.environ.pop, "HL_FAKE_GH", None)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = merge.main(argv)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["result"], "refused")

    def cli_with_root(self):
        """Run a dry-run merge through the CLI with --root, the fake gh first on PATH."""
        argv = ["--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--id", "A-1", "--dry-run"]
        out = io.StringIO()
        env = {"HL_FAKE_GH": str(self.gh_state), "PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}"}
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = merge.main(argv + ["--root", str(self.root)])
        return code, json.loads(out.getvalue())

    def pin_git_and_gh(self):
        self.root = self.tmp / "root"
        self.root.mkdir()
        with mock.patch.dict(os.environ, {"PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}"}):
            bins = {name: claim.describe_binary(name) for name in ("git", "gh")}
        write_json(self.root / "pins.json", {"bins": bins, "history": []})
        self.assertEqual(bins["gh"]["path"], self.tools.gh_bin)

    def test_with_a_root_the_pinned_binaries_are_used(self):
        self.pin_git_and_gh()
        code, result = self.cli_with_root()
        self.assertEqual((code, result["result"]), (0, "dry-run-passed"))
        self.assertTrue(self.gh_calls())

    def test_a_gh_that_updated_itself_is_accepted_recorded_and_used(self):
        self.pin_git_and_gh()
        Path(self.tools.gh_bin).write_text(FAKE_GH + "# updated\n")
        code, result = self.cli_with_root()
        self.assertEqual((code, result["result"]), (0, "dry-run-passed"))
        self.assertTrue(self.gh_calls())
        pins = claim.load_pins(self.root)
        self.assertEqual(pins["bins"]["gh"]["sha256"], claim.describe_binary("gh", self.tools.gh_bin)["sha256"])
        self.assertEqual([(e["name"], e["auto"]) for e in pins["history"]], [("gh", True)])

    def test_without_the_loops_own_token_nothing_runs(self):
        argv = ["--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--id", "A-1", "--gh", self.tools.gh_bin]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HL_FAKE_GH": str(self.gh_state)}):
            del os.environ["GH_TOKEN"]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = merge.main(argv)
        self.assertEqual(code, 2)
        self.assertIn("GH_TOKEN is not set", json.loads(out.getvalue())["refused"][0])
        self.assertEqual(self.gh_calls(), [])

    def test_a_root_and_explicit_binaries_together_are_refused(self):
        self.pin_git_and_gh()
        argv = ["--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--id", "A-1"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(merge.main(argv + ["--root", str(self.root), "--gh", self.tools.gh_bin]), 2)


class TrainGateTest(MergeCase):
    """An item of a run on a train: it merges into the train's branch on its
    review, and the repository's required checks are not asked of it."""

    TRAIN = "hl-train/canary"

    def on_train(self, *item_checks):
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"merge": "train", "train": {"branch": self.TRAIN, "item_checks": list(item_checks)}}})
        # The train as the remote has it once the first item was prepared: made from the base.
        git(self.repo, "push", "-q", "origin", f"refs/remotes/origin/main:refs/heads/{self.TRAIN}")
        self.set_gh(pr={"baseRefName": self.TRAIN})

    def gate(self, **kw):
        return merge.gate(self.run_dir, "A-1", self.repo, self.tools, **kw)

    def refusal(self, **kw):
        with self.assertRaises(Refusal) as caught:
            self.gate(**kw)
        return " | ".join(caught.exception.reasons)

    def test_an_item_on_a_train_merges_on_its_review_without_the_required_checks(self):
        self.on_train()
        self.set_gh(pr={"baseRefName": self.TRAIN}, checks=None)
        self.assertEqual(self.gate()["head_sha"], self.head)
        # Nothing was asked of GitHub about checks: none is this item's to show.
        self.assertEqual([c for c in self.gh_calls() if c[:2] == ["pr", "checks"]], [])
        # The same item, in a run that merges into the base, is held to them.
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {}})
        self.set_gh(checks=None)
        self.assertIn("no required checks are reported", self.refusal())

    def test_an_item_on_a_train_is_not_held_to_githubs_merge_state_any_more_than_before(self):
        # The train's branch is unprotected. Whatever GitHub says of a pull request into it is a warning, as it was.
        self.on_train()
        self.set_gh(pr={"baseRefName": self.TRAIN, "mergeStateStatus": "BLOCKED"}, checks=None)
        ctx = self.gate()
        self.assertEqual((ctx["head_sha"], ctx["warnings"]), (self.head, ["merge state is BLOCKED"]))
        self.on_train("lint")
        self.set_gh(pr={"baseRefName": self.TRAIN, "mergeStateStatus": "BLOCKED"}, checks=[{"name": "lint", "bucket": "pass", "state": "SUCCESS"}])
        self.assertEqual(self.gate()["warnings"], ["merge state is BLOCKED"])
        # One that points at the base is refused for where it points, and for nothing else.
        self.set_gh(pr={"baseRefName": "main", "mergeStateStatus": "BLOCKED"}, checks=[{"name": "lint", "bucket": "pass", "state": "SUCCESS"}])
        said = self.refusal()
        self.assertIn("the pull request's base is `main`", said)
        self.assertEqual((said.count(" | "), "GitHub reports" in said), (0, False))

    def test_a_pull_request_into_the_base_is_not_merged_for_a_run_on_a_train(self):
        self.on_train()
        self.set_gh(pr={"baseRefName": "main"})
        self.assertIn("the pull request's base is `main`; this run's items merge into `hl-train/canary`", self.refusal())

    def test_the_checks_a_repository_names_for_an_item_are_held_and_no_others(self):
        self.on_train("lint", "secret-scan")
        check = lambda name, bucket: {"name": name, "bucket": bucket, "state": bucket.upper()}  # noqa: E731
        self.set_gh(pr={"baseRefName": self.TRAIN}, checks=[check("lint", "pass"), check("secret-scan", "skipping"), check("app-tests", "fail"), check("terraform-plan", "pending")])
        self.assertEqual(self.gate()["head_sha"], self.head)
        self.assertEqual([c for c in self.gh_calls() if c[:2] == ["pr", "checks"]][-1], ["pr", "checks", "7", "--repo", "owner/repo", "--json", "name,bucket,state,link"])
        self.set_gh(pr={"baseRefName": self.TRAIN}, checks=[check("lint", "fail"), check("secret-scan", "pass")])
        self.assertIn("the checks this repository asks of an item are not green: lint=fail", self.refusal())
        # One that has not reported is not green, whether others have or none has.
        self.set_gh(pr={"baseRefName": self.TRAIN}, checks=[check("lint", "pass")])
        self.assertIn("not green: secret-scan=pending", self.refusal())
        self.set_gh(pr={"baseRefName": self.TRAIN}, checks=None)
        self.assertIn("not green: lint=pending, secret-scan=pending", self.refusal())

    def test_an_earlier_merge_whose_deploy_check_has_not_passed_holds_nothing_on_a_train(self):
        # On a train a merge deploys nothing; the rule is held where a batch is promoted.
        other = items.load_item(self.run_dir, "A-2")
        other.update(status="merged", verify_command="true")
        items.save_item(self.run_dir, other)
        self.assertIn("A-2 is merged and its Verify is not recorded as passed", self.refusal())
        self.on_train()
        self.assertEqual(self.gate()["head_sha"], self.head)

    def test_nothing_more_merges_into_a_train_that_is_at_its_limit(self):
        self.on_train()
        self.assertEqual(self.gate(train_ahead=(3, 4))["head_sha"], self.head)
        for ahead in ((4, 4), (9, 4)):
            self.assertIn(f"the train holds {ahead[0]} merged items that have not been promoted, and its limit is 4", self.refusal(train_ahead=ahead))
        # A run that merges into the base has no such count.
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {}})
        self.set_gh()
        self.assertEqual(self.gate(train_ahead=(9, 4))["head_sha"], self.head)

    def test_a_run_that_repairs_the_train_merges_into_a_full_one(self):
        self.on_train()
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "repair": True})
        with mock.patch("hl.merge._train_ahead", return_value=(9, 4)) as counted:
            code, out = self.run_merge()
        self.assertEqual((code, out["result"]), (0, "merged"))
        counted.assert_not_called()

    def test_a_run_whose_work_is_a_merge_has_its_pull_request_merged_whole(self):
        self.on_train()
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "repair": True, "land": "merge"})
        self.assertEqual(self.run_merge()[0], 0)
        asked = [c for c in self.gh_calls() if c[:2] == ["pr", "merge"]][0]
        self.assertEqual(("--merge" in asked, "--squash" in asked), (True, False))

    def test_a_merge_on_a_train_goes_through_the_same_lock_and_is_recorded_the_same_way(self):
        self.on_train()
        # The count of what the train holds is read by the merge itself, not typed by whoever runs it.
        with mock.patch("hl.merge._train_ahead", return_value=(4, 4)):
            with self.assertRaises(Refusal) as caught:
                self.run_merge()
        self.assertIn("its limit is 4", str(caught.exception))
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")
        with mock.patch("hl.merge._train_ahead", return_value=(0, 4)) as counted:
            code, out = self.run_merge()
        self.assertEqual((code, out["result"]), (0, "merged"))
        counted.assert_called_once()
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "merged")
        self.assertIsNone(self.remote_lock())
        self.assertIn("--squash", [c for c in self.gh_calls() if c[:2] == ["pr", "merge"]][0])


class AfterBuildTest(MergeCase):
    """The check run the moment a builder says it is done."""

    def setUp(self):
        super().setUp()
        self.write("A-1", "build-1.done", "")
        self.contract = (self.item_dir() / "contract.md").read_text()
        self.set_gh(pr={"body": self.contract + "\n**Serve.** `true`\n"})

    def forget_pr(self):
        item = items.load_item(self.run_dir, "A-1")
        del item["fields"]["pr"]
        items.save_item(self.run_dir, item)

    def check(self):
        return merge.after_build(self.run_dir, "A-1", self.repo, self.tools)

    def not_finished(self):
        with self.assertRaises(merge.NotFinished) as caught:
            self.check()
        return " | ".join(caught.exception.reasons)

    def test_a_pull_request_opened_against_the_base_goes_back_to_a_builder_on_a_train(self):
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"merge": "train", "train": {"branch": "hl-train/canary", "item_checks": []}}})
        said = self.not_finished()
        self.assertIn("the pull request's base is `main`; this run's items go into `hl-train/canary`: change the base", said)
        self.assertIn("gh api --method PATCH repos/owner/repo/pulls/7 -f base=hl-train/canary", said)
        self.set_gh(pr={"body": self.contract + "\n**Serve.** `true`\n", "baseRefName": "hl-train/canary"})
        git(self.repo, "push", "-q", "origin", "refs/remotes/origin/main:refs/heads/hl-train/canary")
        self.assertEqual(self.check()["result"], "built")

    def cli(self):
        argv = ["--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--id", "A-1", "--after-build"]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HL_FAKE_GH": str(self.gh_state)}):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = merge.main(argv + ["--gh", self.tools.gh_bin])
        return code, json.loads(out.getvalue())

    def test_a_pull_request_that_changes_a_path_no_item_may_change_is_sent_back(self):
        facts = {"protected": ["feature.txt", "brand/**"]}
        with self.assertRaises(merge.NotFinished) as caught:
            merge.after_build(self.run_dir, "A-1", self.repo, self.tools, repo_facts=facts)
        self.assertIn("the pull request changes paths no item may change: feature.txt", " | ".join(caught.exception.reasons))
        sent = merge.send_back(self.run_dir, "A-1", caught.exception.reasons)
        self.assertIn("put it back as the base branch has it", Path(sent["fix_file"]).read_text())
        # A spec that protects other paths, or none, lets the same build through.
        self.write("A-1", "build-1.done", "")
        self.assertEqual(merge.after_build(self.run_dir, "A-1", self.repo, self.tools, repo_facts={"protected": ["brand/**"]})["result"], "built")
        self.assertEqual(merge.after_build(self.run_dir, "A-1", self.repo, self.tools, repo_facts={})["result"], "built")

    def test_a_protected_path_goes_through_only_for_the_item_the_manager_exempted_by_name(self):
        # The gate this loosens, and only this far. An item of a help-desk hardening program had to change a protected
        # file, and the only way through was to take the file out of `protected` for the whole program.
        facts = {"protected": ["feature.txt", "brand/**"]}
        allow_exemptions(self.run_dir, "feature.txt", "brand/**")

        def refused():
            self.write("A-1", "build-1.done", "")
            with self.assertRaises(merge.NotFinished) as caught:
                merge.after_build(self.run_dir, "A-1", self.repo, self.tools, repo_facts=facts)
            return " | ".join(caught.exception.reasons)

        self.assertIn("the pull request changes paths no item may change: feature.txt", refused())
        items.exempt_path(self.run_dir, "A-1", "feature.txt", "the item exists to change this file", facts["protected"], granted_by={"pid": 100})
        self.write("A-1", "build-1.done", "")
        built = merge.after_build(self.run_dir, "A-1", self.repo, self.tools, repo_facts=facts)
        self.assertEqual((built["result"], built["protected_exempted"]), ("built", ["feature.txt"]))
        # The record is the item's own: another item of the run has none, and the path stays protected for it.
        self.assertEqual(items.exemptions(items.load_item(self.run_dir, "A-2")), {})
        # A second protected path in the same pull request is refused, and the refusal says what is exempted.
        git(self.repo, "checkout", "-q", self.BRANCH)
        (self.repo / "brand").mkdir()
        self.head = self.commit("brand/logo.svg", "<svg/>\n", "A-1: and the logo")
        git(self.repo, "push", "-q", "origin", self.BRANCH)
        self.set_gh(pr={"body": self.contract + "\n**Serve.** `true`\n", "headRefOid": self.head})
        said = refused()
        self.assertIn("the pull request changes paths no item may change: brand/logo.svg (of the protected paths, this item may change only feature.txt)", said)
        self.assertNotIn("feature.txt,", said)
        # Withdrawn, the first path is refused again, as it was before any record.
        items.exempt_path(self.run_dir, "A-1", "feature.txt", "granted by mistake", facts["protected"], withdraw=True, granted_by={"pid": 100})
        self.assertIn("the pull request changes paths no item may change: brand/logo.svg, feature.txt", refused())
        history = items.load_item(self.run_dir, "A-1")["protected_exemption_history"]
        self.assertEqual([(h["path"], h["action"], h["by"]) for h in history], [("feature.txt", "granted", {"pid": 100}), ("feature.txt", "withdrawn", {"pid": 100})])

    def test_an_exemption_names_one_protected_file_with_a_reason_and_nothing_wider(self):
        protected = ["feature.txt", "brand/**"]
        allow_exemptions(self.run_dir, *protected)

        def refusal(path, reason="needed", **kw):
            with self.assertRaises(Refusal) as caught:
                items.exempt_path(self.run_dir, "A-1", path, reason, protected, **kw)
            return str(caught.exception)

        self.assertIn("--reason is required", refusal("feature.txt", reason="  "))
        for pattern in ("brand/*", "brand/**", "*.txt", "brand/{a,b}.svg", "brand/?.svg"):
            self.assertIn("is a pattern: an exemption names one file", refusal(pattern))
        for odd in ("brand/", "/feature.txt", "brand/../feature.txt", "./feature.txt", ""):
            self.assertIn("--path takes one file's path in the repository", refusal(odd))
        self.assertIn("'README.md' is not a protected path of owner/repo (its spec protects: feature.txt, brand/**): there is nothing to exempt", refusal("README.md"))
        self.assertIn("A-1 has no exemption for 'brand/logo.svg' (it has: none)", refusal("brand/logo.svg", withdraw=True))
        # Where the base branch can say, a directory is refused: an exemption is matched as one exact path, and a
        # directory's name would match nothing while reading as a grant.
        self.assertIn("'brand/family' is a directory on the base branch", refusal("brand/family", tree_kind=lambda path: "tree"))
        self.assertEqual(items.exemptions(items.load_item(self.run_dir, "A-1")), {})
        # A file under a protected directory is one file, and is recorded as one; so is a file whose name holds brackets.
        item = items.exempt_path(self.run_dir, "A-1", "brand/logo.svg", "the item replaces the logo", protected, tree_kind=lambda path: "blob")
        self.assertEqual(list(items.exemptions(item)), ["brand/logo.svg"])
        self.assertEqual(items.exemptions(item)["brand/logo.svg"]["reason"], "the item replaces the logo")
        item = items.exempt_path(self.run_dir, "A-1", "brand/[id]/page.tsx", "a route file", protected, tree_kind=lambda path: None)
        self.assertIn("brand/[id]/page.tsx", items.exemptions(item))
        items.set_status(self.run_dir, "A-2", "skipped", "not needed")
        with self.assertRaises(Refusal) as caught:
            items.exempt_path(self.run_dir, "A-2", "feature.txt", "needed", protected)
        self.assertIn("A-2 is skipped; nothing is exempted for it any more", str(caught.exception))

    def test_a_path_with_brackets_is_matched_as_the_one_file_it_names(self):
        # `[id]` is a class of characters to a pattern; an exemption for `brand/[id]/page.tsx` lets that file through and not `brand/i/page.tsx`.
        facts = {"protected": ["brand/**"]}
        allow_exemptions(self.run_dir, "brand/**")
        git(self.repo, "checkout", "-q", self.BRANCH)
        (self.repo / "brand" / "i").mkdir(parents=True)
        (self.repo / "brand" / "[id]").mkdir(parents=True)
        self.commit("brand/[id]/page.tsx", "page\n", "A-1: the route")
        self.head = self.commit("brand/i/page.tsx", "page\n", "A-1: and another")
        git(self.repo, "push", "-q", "origin", self.BRANCH)
        self.set_gh(pr={"body": self.contract + "\n**Serve.** `true`\n", "headRefOid": self.head})
        items.exempt_path(self.run_dir, "A-1", "brand/[id]/page.tsx", "the item adds this route", facts["protected"])
        with self.assertRaises(merge.NotFinished) as caught:
            merge.after_build(self.run_dir, "A-1", self.repo, self.tools, repo_facts=facts)
        self.assertIn("the pull request changes paths no item may change: brand/i/page.tsx (of the protected paths, this item may change only brand/[id]/page.tsx)", " | ".join(caught.exception.reasons))

    def test_a_run_created_without_exemptable_paths_takes_no_exemption(self):
        # The kit before exemptions, for every run created without the spec's `exemptable`: a restaged program's
        # running runs among them.
        with self.assertRaises(Refusal) as caught:
            items.exempt_path(self.run_dir, "A-1", "feature.txt", "needed", ["feature.txt"])
        self.assertIn("this run takes no exemption for 'feature.txt': when it was created, its repository's spec named no path under `exemptable`", str(caught.exception))
        allow_exemptions(self.run_dir, "brand/**")
        with self.assertRaises(Refusal) as caught:
            items.exempt_path(self.run_dir, "A-1", "feature.txt", "needed", ["feature.txt", "brand/**"])
        self.assertIn("its repository's spec named `brand/**` under `exemptable`", str(caught.exception))
        # A record written by hand for a path the run does not allow counts for nothing at the check after a build.
        item = items.load_item(self.run_dir, "A-1")
        item["protected_exemptions"] = {"feature.txt": {"reason": "written by hand", "at": "2026-10-28T00:00:00Z"}}
        items.save_item(self.run_dir, item)
        with self.assertRaises(merge.NotFinished) as caught:
            merge.after_build(self.run_dir, "A-1", self.repo, self.tools, repo_facts={"protected": ["feature.txt"]})
        self.assertIn("the pull request changes paths no item may change: feature.txt", " | ".join(caught.exception.reasons))

    def test_a_malformed_exemption_record_is_refused_and_never_read_as_far_as_it_goes(self):
        allow_exemptions(self.run_dir, "feature.txt")
        facts = {"protected": ["feature.txt"]}
        for raw in ([["feature.txt", "x"]], {"feature.txt": None}, "feature.txt", 3, {"feature.txt": {"reason": " "}}, {"": {"reason": "x"}}, {"feature.txt": "x"}):
            item = items.load_item(self.run_dir, "A-1")
            item["protected_exemptions"] = raw
            items.save_item(self.run_dir, item)
            with self.assertRaises(Refusal) as caught:
                items.exemptions(item)
            self.assertIn("A-1's protected_exemptions in item.json is not a record `hl-items exempt` wrote", str(caught.exception), raw)
            self.write("A-1", "build-1.done", "")
            with self.assertRaises(Refusal) as caught:
                merge.after_build(self.run_dir, "A-1", self.repo, self.tools, repo_facts=facts)
            self.assertNotIsInstance(caught.exception, merge.NotFinished, raw)
            self.assertIn("is not a record `hl-items exempt` wrote", str(caught.exception), raw)

    def test_only_the_manager_that_holds_the_program_records_an_exemption_and_the_record_names_it(self):
        root = self.tmp / "root"
        root.mkdir()
        settings = {"program": "p", "base": "main", "worker_claude_ref": "op://a/b/c", "gh_token_ref": "op://a/b/c", "cursor_model": "m"}
        write_json(root / "program.json", {**settings, "repos": {"Owner/Repo": {"protected": ["feature.txt"]}}})
        allow_exemptions(self.run_dir, "feature.txt")
        me, manager_pid, builder_pid, flow_pid = os.getppid(), 4100, 4200, 4300
        write_json(root / "manager.lock.d" / "claim.json", {"pid": manager_pid, "start": "Wed Oct 28 09:00:00 2026", "pane": "w1:p1", "session": "manager-session", "claimed_at": "2026-10-28T15:00:00Z"})
        table = {
            manager_pid: ("Wed Oct 28 09:00:00 2026", 1, "claude --model claude-opus-5-5"),
            builder_pid: ("Wed Oct 28 10:00:00 2026", 1, "claude --model claude-sonnet-5-5"),
            flow_pid: ("Wed Oct 28 10:05:00 2026", manager_pid, "/opt/homebrew/bin/python3 hl-flow --root r review --id A-1"),
        }

        def cli(shell_parent, shell="/bin/zsh -c eval 'hl-items …'", *argv):
            # This test's own parent stands for the process that started `hl-items`.
            procs = FakeProcs({**table, me: ("Wed Oct 28 10:10:00 2026", shell_parent, shell)})
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = items.main(["--run-dir", str(self.run_dir), "exempt", "--id", "A-1", *argv], procs=procs)
            return code, json.loads(out.getvalue())

        grant = ("--path", "feature.txt", "--reason", "the item rewrites it", "--root", str(root))
        # A builder's shell: its own `claude` above it, which holds no claim.
        code, out = cli(builder_pid, "/bin/zsh -c eval 'hl-items …'", *grant)
        self.assertEqual(code, 2)
        self.assertIn(f"was started by pid {builder_pid}, not by the agent that holds this program (pid {manager_pid})", out["refused"][0])
        # A shell one of the kit's scripts started under the manager (a build's Ready line under `hl-flow review`).
        code, out = cli(flow_pid, "/bin/sh -c hl-items …", *grant)
        self.assertEqual(code, 2)
        self.assertIn(f"was started by pid {flow_pid}, not by the agent that holds this program (pid {manager_pid})", out["refused"][0])
        # The kit's script itself, with no shell between.
        code, out = cli(manager_pid, "/opt/homebrew/bin/python3 hl-flow --root r review --id A-1", *grant)
        self.assertEqual(code, 2)
        self.assertIn("not straight from a shell", out["refused"][0])
        self.assertEqual(items.exemptions(items.load_item(self.run_dir, "A-1")), {})
        # The manager's own shell tool: a shell its `claude` started.
        code, out = cli(manager_pid, "/bin/zsh -c eval 'hl-items …'", *grant)
        self.assertEqual((code, list(out["protected_exemptions"])), (0, ["feature.txt"]))
        self.assertEqual(out["protected_exemptions"]["feature.txt"]["granted_by"], {"pid": manager_pid, "session": "manager-session", "pane": "w1:p1", "claimed_at": "2026-10-28T15:00:00Z"})
        manager = FakeProcs({**table, me: ("Wed Oct 28 10:10:00 2026", manager_pid, "-zsh")})
        # Without --root the program is the one the run directory lies in, and there is none here: said, nothing recorded.
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = items.main(["--run-dir", str(self.run_dir), "exempt", "--id", "A-1", "--path", "feature.txt", "--reason", "again", "--withdraw"], procs=manager)
        self.assertEqual(code, 2)
        self.assertIn("no program.json at", json.loads(out.getvalue())["refused"][0])
        self.assertEqual(list(items.exemptions(items.load_item(self.run_dir, "A-1"))), ["feature.txt"])

    def test_a_ready_line_run_under_the_managers_claude_cannot_grant_an_exemption_and_the_managers_own_shell_can(self):
        """Real processes. The reviewer's probe: a build's Ready command, run by `hl-flow review` (here the kit's own
        `serve.Host.run`, through `/bin/sh -c`) under the manager's `claude`, found that `claude` as its nearest one,
        passed the claim check, and wrote a grant that named the manager."""
        import shlex
        import shutil
        import subprocess
        import sys as sys_mod

        zsh = shutil.which("zsh") or "/bin/zsh"
        if not Path(zsh).exists():
            self.skipTest("no zsh on this host")
        root = self.tmp / "root"
        root.mkdir()
        settings = {"program": "p", "base": "main", "worker_claude_ref": "op://a/b/c", "gh_token_ref": "op://a/b/c", "cursor_model": "m"}
        write_json(root / "program.json", {**settings, "repos": {"owner/repo": {"protected": ["feature.txt"]}}})
        allow_exemptions(self.run_dir, "feature.txt")
        scripts = Path(merge.__file__).resolve().parent.parent
        # The manager: a process whose name is `claude` (a shell under that name), which claims the program for itself.
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "claude").symlink_to(zsh)
        py = shlex.quote(sys_mod.executable)
        claimer = self.tmp / "claimer.py"
        claimer.write_text(
            f"import os, sys\nsys.path.insert(0, {str(scripts)!r})\nfrom hl import claim\n"
            f"claim.claim({str(root)!r}, os.getppid(), 'w1:p1', 'manager-session', claim.Procs())\n"
        )
        exempt = f"{py} {shlex.quote(str(scripts / 'hl-items'))} --run-dir {shlex.quote(str(self.run_dir))} exempt --id A-1 --path feature.txt --reason {{reason}} --root {shlex.quote(str(root))}"
        prober = self.tmp / "ready.py"
        prober.write_text(
            f"import sys\nsys.path.insert(0, {str(scripts)!r})\nfrom hl import serve\n"
            f"print('ready-rc', serve.Host().run({exempt.format(reason='planted-by-a-ready-line')!r}, '/'))\n"
        )

        def as_manager(line: str) -> subprocess.CompletedProcess:
            # The manager's `claude` claims, then runs one line; `; true` keeps the shell from becoming what it runs.
            body = f"{py} {shlex.quote(str(claimer))} && {line}; true"
            return subprocess.run([str(bin_dir / "claude"), "-c", body], capture_output=True, text=True, timeout=120)

        # The probe: `hl-flow review` running a Ready command under the manager's `claude`. Refused, nothing recorded.
        ran = as_manager(f"{py} {shlex.quote(str(prober))}")
        self.assertIn("ready-rc 2", ran.stdout, ran.stderr)
        self.assertEqual(items.exemptions(items.load_item(self.run_dir, "A-1")), {})
        # The manager's own shell tool: a shell its `claude` starts, running the command as Claude Code does.
        tool = shlex.quote("eval " + shlex.quote(exempt.format(reason="the-item-rewrites-it")) + " < /dev/null && pwd -P > /dev/null")
        ran = as_manager(f"{shlex.quote(zsh)} -c {tool}")
        granted = items.exemptions(items.load_item(self.run_dir, "A-1"))
        self.assertEqual(list(granted), ["feature.txt"], ran.stdout + ran.stderr)
        self.assertEqual((granted["feature.txt"]["reason"], granted["feature.txt"]["granted_by"]["session"]), ("the-item-rewrites-it", "manager-session"))

    def test_the_command_line_refuses_a_directory_the_base_branch_holds(self):
        # The stream's clone says what the base holds at the path.
        base = self.tmp / "base-tree"
        git(self.repo, "worktree", "add", "-q", "--detach", str(base), "origin/main")
        (base / "brand").mkdir()
        (base / "brand" / "logo.svg").write_text("<svg/>\n")
        git(base, "add", "brand/logo.svg")
        git(base, "commit", "-q", "-m", "brand")
        git(base, "push", "-q", "origin", "HEAD:main")
        git(self.repo, "fetch", "-q", "origin")
        root = self.tmp / "root"
        root.mkdir()
        settings = {"program": "p", "base": "main", "worker_claude_ref": "op://a/b/c", "gh_token_ref": "op://a/b/c", "cursor_model": "m"}
        write_json(root / "program.json", {**settings, "streams": {"main": {"repo": "owner/repo", "clone": str(self.repo)}}, "repos": {"owner/repo": {"protected": ["brand"]}}})
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "stream": "main"})
        allow_exemptions(self.run_dir, "brand")
        me = os.getppid()
        write_json(root / "manager.lock.d" / "claim.json", {"pid": 4100, "start": "S", "pane": "w1:p1", "session": "m", "claimed_at": "t"})
        procs = FakeProcs({4100: ("S", 1, "claude"), me: ("S2", 4100, "/bin/zsh -c eval 'hl-items …'")})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = items.main(["--run-dir", str(self.run_dir), "exempt", "--id", "A-1", "--path", "brand", "--reason", "all of it", "--root", str(root)], procs=procs)
        self.assertEqual(code, 2)
        self.assertIn("'brand' is a directory on the base branch", json.loads(out.getvalue())["refused"][0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = items.main(["--run-dir", str(self.run_dir), "exempt", "--id", "A-1", "--path", "brand/logo.svg", "--reason", "the logo", "--root", str(root)], procs=procs)
        self.assertEqual((code, list(json.loads(out.getvalue())["protected_exemptions"])), (0, ["brand/logo.svg"]))

    def test_the_command_line_takes_protected_paths_from_the_programs_settings(self):
        root = self.tmp / "root"
        root.mkdir()
        settings = {"program": "p", "base": "main", "worker_claude_ref": "op://a/b/c", "gh_token_ref": "op://a/b/c", "cursor_model": "m"}
        write_json(root / "program.json", {**settings, "repos": {"owner/repo": {"protected": ["feature.txt"]}}})
        self.assertEqual(merge._repo_facts(str(root), self.run_dir), {"protected": ["feature.txt"]})
        self.assertEqual(merge._repo_facts(None, self.run_dir), {})
        write_json(root / "program.json", settings)
        self.assertEqual(merge._repo_facts(str(root), self.run_dir), {})

    def test_a_finished_build_is_recorded_with_its_pull_request_and_head(self):
        result = self.check()
        self.assertEqual((result["result"], result["pr"], result["head_sha"]), ("built", 7, self.head))
        self.assertEqual((result["serve"], result["ready_kind"], result["ready"]), ("true", "COMMAND", "true"))
        fields = items.load_item(self.run_dir, "A-1")["fields"]
        self.assertEqual((fields["pr"], fields["head_sha"]), ("7", self.head))

    def test_the_pull_request_is_found_by_its_branch(self):
        self.forget_pr()
        self.assertEqual(self.check()["pr"], 7)
        list_call = next(c for c in self.gh_calls() if c[:2] == ["pr", "list"])
        self.assertEqual(list_call[list_call.index("--head") + 1], self.BRANCH)

    def test_no_pull_request_is_not_finished(self):
        self.forget_pr()
        self.set_gh(pr_list=[])
        self.assertIn("expected exactly one open pull request from branch hl/run-1/A-1, found 0", self.not_finished())

    def test_armed_auto_merge_is_not_finished(self):
        self.set_gh(pr={"body": self.contract, "autoMergeRequest": {"mergeMethod": "SQUASH"}})
        self.assertIn("auto-merge is armed", self.not_finished())

    def test_a_commit_trailer_is_not_finished(self):
        self.rewrite_head("--trailer", "Co-authored-by: Cursor <cursoragent@cursor.com>")
        self.set_gh(pr={"headRefOid": self.head, "body": self.contract})
        self.assertIn("carries AI attribution", self.not_finished())

    def test_a_body_without_the_contract_is_not_finished(self):
        self.set_gh(pr={"body": "## Criteria\n\n1. `make test` exits 0.\n\nI did the rest too.\n"})
        self.assertIn("criteria [2] are not in it", self.not_finished())

    def test_a_body_that_rewraps_the_contract_still_carries_it(self):
        self.set_gh(pr={"body": self.contract.replace("\n", "\r\n").replace("prints `1`", "prints\r\n   `1`")})
        self.assertEqual(self.check()["result"], "built")

    # As written in a contract: a backslash, `u` and four hex digits, as text.
    ESCAPES = r"`printf '\u0000 \u001b \u0085 \u007f \t'` prints nothing to the terminal."

    def with_escapes(self):
        """A third criterion holding escapes as text, installed the one way a contract changes."""
        last = "2. `grep -c first out.txt` prints `1`.\n"
        self.assertIn(last, self.contract)
        self.contract = self.contract.replace(last, f"{last}3. {self.ESCAPES}\n")
        items._install_contract(self.run_dir, items.load_item(self.run_dir, "A-1"), self.contract, {"source": "amended", "file": "correction-1.md"})
        self.assertEqual(items.load_item(self.run_dir, "A-1")["contract"]["criteria"], 3)

    @staticmethod
    def as_gh_reads(text):
        # What gh 2.100 gave back of a pull request whose body GitHub holds as sent; \u007f and \t it leaves alone.
        return text.replace("\\u0000", "\\^@").replace("\\u001b", "\\^[").replace("\\u0085", "\\^E")

    def test_escapes_gh_reads_as_caret_notation_still_carry_the_contract(self):
        self.with_escapes()
        body = self.as_gh_reads(self.contract)
        self.assertIn("`printf '\\^@ \\^[ \\^E \\u007f \\t'`", body)
        self.set_gh(pr={"body": body})
        self.assertEqual(self.check()["result"], "built")
        # A gh that gave the body back as GitHub holds it would be read the same.
        self.set_gh(pr={"body": self.contract})
        self.assertEqual(self.check()["result"], "built")

    def test_a_criterion_with_escapes_worded_differently_is_still_not_in_the_body(self):
        self.with_escapes()
        for changed in (
            self.contract.replace("prints nothing", "prints little"),
            self.contract.replace("\\u007f", "\\u007e"),
            # The escape without its backslash, or its backslash with a space, is not the escape.
            self.contract.replace("\\u001b", "u001b"),
            self.contract.replace("\\u0085", "\\ "),
        ):
            self.set_gh(pr={"body": self.as_gh_reads(changed)})
            self.assertIn("criteria [3] are not in it", self.not_finished())
        self.set_gh(pr={"body": self.as_gh_reads(self.contract).replace("\\^[", "^[")})
        self.assertIn("criteria [3] are not in it", self.not_finished())

    def test_a_body_without_the_criterion_with_escapes_is_not_finished(self):
        self.with_escapes()
        self.set_gh(pr={"body": self.as_gh_reads(self.contract.replace(self.ESCAPES, ""))})
        self.assertIn("criteria [3] are not in it", self.not_finished())

    def test_text_is_read_the_way_gh_reads_it(self):
        # Each pair as gh 2.100 gave it back, the first written as text in a body GitHub stored as sent.
        seen = {
            "\\u0000": "\\^@", "\\u0008": "\\^H", "\\u000c": "\\^L", "\\u001B": "\\^[", "\\u001c": "\\^\\", "\\u001f": "\\^_",
            "\\u0080": "\\^@", "\\u0085": "\\^E", "\\u009f": "\\^_", "\\\\u001b": "\\\\^[",
            "\\u0009": "\\u0009", "\\u000a": "\\u000a", "\\u000b": "\\u000b", "\\u000d": "\\u000d", "\\u0020": "\\u0020",
            "\\u007f": "\\u007f", "\\u00a0": "\\u00a0", "\\U001b": "\\U001b", "\\u001": "\\u001", "\\t": "\\t",
            "x\x1by": "x^[y", "x\x85y": "x^Ey", "x\x07y": "x^Gy", "\\\x1b": "\\^[",
            "\x08": "\x08", "\t": "\t", "\x0b": "\x0b", "\x0c": "\x0c", "\x7f": "\x7f", "\xa0": "\xa0",
        }  # fmt: skip
        self.assertEqual({sent: merge._as_gh_reads(sent) for sent in seen}, seen)
        self.assertEqual([merge._as_gh_reads(read) for read in seen.values()], list(seen.values()))

    def test_a_report_without_its_serve_line_is_not_finished(self):
        self.write("A-1", "build-1.md", "I built it.\n**Ready.** COMMAND `true`\n")
        self.assertIn("build-1.md: expected exactly one '**Serve.**", self.not_finished())

    def route_with_look(self):
        item = items.load_item(self.run_dir, "A-1")
        item["route"] = {"premise": "no", "look": True, "reason": "a page", "at": "now"}
        items.save_item(self.run_dir, item)

    def test_an_item_with_a_look_is_sent_back_for_a_serve_line_that_starts_nothing(self):
        self.route_with_look()
        for line in ("true", ":", "exit 0", "echo ok", " "):
            self.write("A-1", "build-1.md", f"**Serve.** `{line}`\n**Ready.** URL `http://127.0.0.1:1/`\n")
            self.assertIn("starts nothing", self.not_finished(), line)

    def test_an_item_with_a_look_and_a_real_serve_line_is_built(self):
        self.route_with_look()
        self.write("A-1", "build-1.md", "**Serve.** `npm run start -- --port 8401`\n**Ready.** URL `http://127.0.0.1:8401/`\n")
        self.assertEqual(self.check()["result"], "built")

    def use_worktree(self):
        """The build was made in a worktree of its own, as the loop makes them; the clone stays on its base."""
        worktree = self.tmp / "worktree"
        git(self.repo, "checkout", "--detach", "origin/main")
        git(self.repo, "worktree", "add", str(worktree), self.BRANCH)
        items.set_fields(self.run_dir, "A-1", [f"worktree={worktree}"])
        return worktree

    def test_a_clean_worktree_at_the_pull_requests_head_is_finished(self):
        self.use_worktree()
        self.assertEqual(self.check()["result"], "built")

    def test_files_the_builder_left_in_its_worktree_are_not_finished_and_are_named(self):
        worktree = self.use_worktree()
        (worktree / "scripts" / "__pycache__").mkdir(parents=True)
        (worktree / "scripts" / "__pycache__" / "x.pyc").write_text("x")
        (worktree / "feature.txt").write_text("changed after the push\n")
        said = self.not_finished()
        self.assertIn("the worktree is not clean: feature.txt, scripts/", said)
        self.assertNotIn("head_sha", items.load_item(self.run_dir, "A-1")["fields"])

    def test_what_the_clone_ignores_does_not_make_a_worktree_dirty(self):
        worktree = self.use_worktree()
        (self.repo / ".git" / "info" / "exclude").write_text(".scratch/\n")
        (worktree / ".scratch").mkdir()
        (worktree / ".scratch" / "tests.log").write_text("12 passed\n")
        self.assertEqual(self.check()["result"], "built")

    def test_a_commit_that_was_not_pushed_is_not_finished(self):
        worktree = self.use_worktree()
        (worktree / "more.txt").write_text("more\n")
        git(worktree, "add", "more.txt")
        git(worktree, "commit", "-m", "A-1: one more thing")
        said = self.not_finished()
        self.assertIn(f"but the pull request's head is {self.head[:12]}: push what you committed", said)
        self.assertNotIn("not clean", said)

    def test_a_worktree_that_is_gone_is_not_finished(self):
        items.set_fields(self.run_dir, "A-1", [f"worktree={self.tmp / 'nowhere'}"])
        self.assertIn("nowhere does not exist", self.not_finished())

    def test_the_fix_list_for_a_dirty_worktree_says_what_to_do_with_each_kind_of_file(self):
        worktree = self.use_worktree()
        (worktree / "coverage.xml").write_text("x")
        code, result = self.cli()
        fix = Path(result["fix_file"]).read_text()
        self.assertEqual((code, result["result"]), (2, "sent-back"))
        self.assertIn("- the worktree is not clean: coverage.xml", fix)
        self.assertIn("`git status --porcelain` must then print nothing", fix)

    def test_a_build_with_no_done_file_is_refused_without_bothering_the_builder(self):
        (self.item_dir() / "build-1.done").unlink()
        with self.assertRaises(Refusal) as caught:
            self.check()
        self.assertNotIsInstance(caught.exception, merge.NotFinished)

    def test_the_cli_sends_an_unfinished_build_back_with_a_short_pointer(self):
        self.set_gh(pr={"body": self.contract, "isDraft": True})
        code, result = self.cli()
        self.assertEqual((code, result["result"], result["times_sent_back"]), (2, "sent-back", 1))
        self.assertFalse((self.item_dir() / "build-1.done").exists())
        self.assertTrue((self.item_dir() / "build-1.done.refused-1").exists())
        fix = Path(result["fix_file"]).read_text()
        self.assertIn("- pull request is a draft", fix)
        self.assertIn(str(self.item_dir() / "build-1.done"), fix)
        # Herdr delivers a long prompt as pasted content; the pointer must stay a plain line.
        self.assertLess(len(result["prompt"]), 400)
        self.assertNotIn("head_sha", items.load_item(self.run_dir, "A-1")["fields"])

    def test_a_second_send_back_is_counted(self):
        self.set_gh(pr={"body": self.contract, "isDraft": True})
        self.cli()
        self.write("A-1", "build-1.done", "")
        self.assertEqual(self.cli()[1]["times_sent_back"], 2)
        self.assertTrue((self.item_dir() / "build-1.fix-2.md").exists())

    def test_the_cli_passes_a_finished_build(self):
        code, result = self.cli()
        self.assertEqual((code, result["result"]), (0, "built"))
        self.assertTrue((self.item_dir() / "build-1.done").exists())


class LockTest(MergeCase):
    def acquire(self, run_id="run-1", item_id="A-1", **kw):
        kw.setdefault("wait_s", 0.3)
        kw.setdefault("poll_s", 0.05)
        return merge.acquire_lock(self.tools, self.repo, run_id, item_id, ref=TEST_REF, **kw)

    def test_a_second_holder_waits_and_then_gives_up(self):
        self.acquire("run-a", "A-1")
        with self.assertRaises(merge.LockTimeout) as caught:
            self.acquire("run-b", "B-1")
        self.assertEqual(caught.exception.holder, "run-a/A-1")

    def test_release_frees_the_lock_for_the_next_holder(self):
        token = self.acquire("run-a", "A-1")
        self.assertTrue(merge.release_lock(self.tools, self.repo, token))
        self.assertIsNone(self.remote_lock())
        self.acquire("run-b", "B-1")

    def test_a_lock_older_than_the_abandon_bound_is_reclaimed(self):
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 2 * 3600)) + "+0000"
        env = {**os.environ, "GIT_COMMITTER_DATE": old, "GIT_AUTHOR_DATE": old}
        stale = git(self.repo, "commit-tree", merge.EMPTY_TREE, "-m", "looprunner merge-lock\n\nrun_id: dead\nitem_id: X-1\n", env=env)
        git(self.repo, "push", "origin", f"{stale}:{TEST_REF}")
        token = self.acquire("run-b", "B-1")
        self.assertEqual(token["reclaimed_from"], stale)
        self.assertEqual(self.remote_lock(), token["held_sha"])

    def test_releasing_a_lock_someone_else_reclaimed_does_not_touch_theirs(self):
        mine = self.acquire("run-a", "A-1")
        theirs = git(self.repo, "commit-tree", merge.EMPTY_TREE, "-m", "looprunner merge-lock\n\nrun_id: run-b\nitem_id: B-1\n")
        git(self.repo, "push", "--force", "origin", f"{theirs}:{TEST_REF}")
        self.assertFalse(merge.release_lock(self.tools, self.repo, mine))
        self.assertEqual(self.remote_lock(), theirs)

    def test_release_own_frees_a_token_left_by_a_merge_that_died(self):
        token = self.acquire("run-1", "A-1")
        (self.item_dir() / "merge-lock.json").write_text(json.dumps(token))
        freed = merge.release_own(self.tools, self.repo, self.run_dir, ref=TEST_REF)
        self.assertEqual(len(freed), 1)
        self.assertIsNone(self.remote_lock())
        self.assertFalse((self.item_dir() / "merge-lock.json").exists())

    def test_release_own_frees_a_remote_lock_naming_this_run_and_no_other(self):
        self.acquire("run-1", "A-1")  # token file lost with the dead session
        self.assertEqual(len(merge.release_own(self.tools, self.repo, self.run_dir, ref=TEST_REF)), 1)
        self.assertIsNone(self.remote_lock())
        self.acquire("run-10", "A-1")  # a different run whose id merely starts the same
        self.assertEqual(merge.release_own(self.tools, self.repo, self.run_dir, ref=TEST_REF), [])
        self.assertIsNotNone(self.remote_lock())


LOOPRUNNER_SRC = os.environ.get("LOOPRUNNER_SRC")


@unittest.skipUnless(LOOPRUNNER_SRC and Path(LOOPRUNNER_SRC, "looprunner", "git_ops.py").exists(), "set LOOPRUNNER_SRC to a LoopRunner checkout")
@unittest.skipUnless(sys.version_info >= (3, 10), "LoopRunner's own code needs Python 3.10 or later")
class LoopRunnerCompatTest(MergeCase):
    """Both systems must exclude each other on the real ref name."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, LOOPRUNNER_SRC)
        self.addCleanup(sys.path.remove, LOOPRUNNER_SRC)
        from looprunner import git_ops

        self.git_ops = git_ops

    def test_looprunner_waits_while_herdr_loop_holds(self):
        token = merge.acquire_lock(self.tools, self.repo, "run-1", "A-1", wait_s=0.3, poll_s=0.05)
        with self.assertRaises(self.git_ops.MergeLockTimeout):
            self.git_ops.acquire_merge_lock(self.repo, "lr-run", "DK-1", env=dict(os.environ), timeout_s=0.3, poll_interval_s=0.05)
        merge.release_lock(self.tools, self.repo, token)
        lr_token = self.git_ops.acquire_merge_lock(self.repo, "lr-run", "DK-1", env=dict(os.environ), timeout_s=0.3, poll_interval_s=0.05)
        self.git_ops.release_merge_lock(self.repo, lr_token, env=dict(os.environ))
        self.assertIsNone(self.remote_lock(merge.LOCK_REF))

    def test_herdr_loop_waits_while_looprunner_holds_and_names_it(self):
        lr_token = self.git_ops.acquire_merge_lock(self.repo, "lr-run", "DK-1", env=dict(os.environ), timeout_s=0.3, poll_interval_s=0.05)
        with self.assertRaises(merge.LockTimeout) as caught:
            merge.acquire_lock(self.tools, self.repo, "run-1", "A-1", wait_s=0.3, poll_s=0.05)
        self.assertEqual(caught.exception.holder, "lr-run/DK-1")
        self.git_ops.release_merge_lock(self.repo, lr_token, env=dict(os.environ))
        merge.acquire_lock(self.tools, self.repo, "run-1", "A-1", wait_s=0.3, poll_s=0.05)
