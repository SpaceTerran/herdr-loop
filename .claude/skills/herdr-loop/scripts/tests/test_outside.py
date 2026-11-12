"""Pull requests from outside the program: what a save lists, the merge
gate and its lock, the deploy check, and passing one over. Git is real (a
local bare origin; the lock is a test ref, never a real repository's), and
`gh` is a stand-in that answers from a JSON file."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hl import items, ledger, merge, outside
from hl.common import Refusal, read_json, utc_now, write_json

from .support import BACKLOG_A
from .test_merge import TEST_REF, MergeCase, git

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
path = os.environ["HL_FAKE_GH"]
state = json.load(open(path))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def done(code=0):
    json.dump(state, open(path, "w"))
    sys.exit(code)
def flag(name):
    return args[args.index(name) + 1] if name in args else None
if args[:2] == ["pr", "view"]:
    fields = args[args.index("--json") + 1].split(",")
    shown = state.get("other_prs", {}).get(args[2], state["pr"])
    print(json.dumps({k: shown.get(k) for k in fields}))
    done()
if args[:2] == ["pr", "checks"]:
    if state.get("checks") is None:
        sys.stderr.write("no required checks reported on the 'main' branch\n")
        done(1)
    print(json.dumps(state["checks"]))
    done()
if args[:2] == ["pr", "list"]:
    repo = flag("--repo")
    if repo in state.get("list_fails", []):
        sys.stderr.write("HTTP 502\n")
        done(1)
    print(json.dumps(state.get("lists", {}).get(repo, [])))
    done()
if args[:1] == ["api"] and args[1].endswith("/protection/required_status_checks"):
    # What the base branch's protection requires; none named means GitHub's 404 for an unprotected branch.
    if state.get("required_names") is None:
        sys.stderr.write("gh: Branch not protected (HTTP 404)\n")
        done(1)
    print(json.dumps(state["required_names"]))
    done()
if args[:1] == ["api"] and "/rules/branches/" in args[1]:
    print(json.dumps(state.get("ruleset_names", [])))
    done()
if args[:1] == ["api"] and "/events" in args[1]:
    print(json.dumps(state.get("events", [])))
    done()
if args[:1] == ["api"] and args[1].endswith("/comments"):
    print(json.dumps(state.get("issue_comments", [])))
    done()
if args[:2] == ["api", "user"]:
    print(state.get("me", "loop-bot"))
    done()
if args[:1] == ["api"]:
    sys.stdout.write(state.get("merged_message", "Bump pillow (#12)\n"))
    done()
if args[:2] == ["pr", "merge"]:
    if state.get("merge_rc", 0):
        sys.stderr.write("GraphQL: Pull request is not mergeable\n")
        done(state["merge_rc"])
    state["pr"]["state"] = "MERGED"
    state["pr"]["mergeCommit"] = {"oid": "f" * 40}
    done()
if args[:2] == ["run", "list"] and "--workflow" not in args:
    print(json.dumps(state.get("any_runs", [])))
    done()
if args[:2] == ["run", "list"]:
    key = "branch_runs" if "--branch" in args else "runs"
    print(json.dumps(state.get(key, {}).get(flag("--workflow"), [])))
    done()
if args[:2] == ["label", "create"]:
    if state.get("label_exists"):
        sys.stderr.write('label with name "rollout-held" already exists; use `--force` to update its color and description\n')
        done(1)
    done()
if args[:2] == ["pr", "edit"]:
    done(state.get("edit_rc", 0))
if args[:2] == ["pr", "comment"]:
    done()
sys.stderr.write("fake gh: unexpected " + " ".join(args) + "\n")
done(64)
'''

BRANCH = "secfix/pillow-13"


def install_gh(directory) -> Path:
    path = Path(directory) / "gh"
    path.write_text(FAKE_GH)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class OutsideCase(unittest.TestCase):
    """A program root with one run in owner/repo, an origin and a clone, and a
    steward's branch pushed with one commit."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        patcher = mock.patch.dict(os.environ, {"GH_TOKEN": "loop-token-for-tests"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = self.tmp / "loops" / "p"
        self.root.mkdir(parents=True)
        self.settings(outside_prs="adopt", outside_pr_vetters=["steward-bot"])
        backlog = self.tmp / "A-ITEMS.md"
        backlog.write_text(BACKLOG_A)
        self.backlog = backlog
        self.run_dir = self.root / "runs" / "run-1"
        items.init_run(self.run_dir, "run-1", "owner/repo", [str(backlog)])

        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "clone"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        git(self.tmp, "clone", str(self.origin), str(self.repo))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        self.commit("README.md", "base\n", "base")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "checkout", "-b", BRANCH)
        self.head = self.commit("requirements.txt", "pillow==13.0.0\n", "Bump pillow to 13.0.0")
        git(self.repo, "push", "origin", BRANCH)

        self.gh_state = self.tmp / "gh.json"
        self.gh = install_gh(self.tmp)
        self.tools = merge.Tools(gh=str(self.gh), env={**os.environ, "HL_FAKE_GH": str(self.gh_state)}, sleep=lambda s: None)
        self.set_gh()

    def settings(self, **changes):
        path = self.root / "program.json"
        base = read_json(path) if path.exists() else {
            "program": "p",
            "base": "main",
            "worker_claude_ref": "-",
            "gh_token_ref": "op://x/y/z",
            "cursor_model": "composer",
            "streams": {"main": {"repo": "owner/repo", "clone": str(self.tmp / "clone")}},
            "repos": {"owner/repo": {"deploys": []}},
        }
        merged = {**base, **changes}
        for key, value in changes.items():
            if value is None:
                merged.pop(key)
        write_json(path, merged)

    def commit(self, name, text, message):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(self.repo, "add", name)
        git(self.repo, "commit", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def set_gh(self, pr=None, **top):
        state = {
            "pr": {
                "number": 12,
                "url": "https://example.invalid/pull/12",
                "state": "OPEN",
                "isDraft": False,
                "title": "Bump pillow to 13.0.0",
                "body": "Research: the release notes list no change this repository uses.",
                "headRefName": BRANCH,
                "headRefOid": self.head,
                "baseRefName": "main",
                "mergeStateStatus": "CLEAN",
                "mergeable": "MERGEABLE",
                "autoMergeRequest": None,
                "mergeCommit": None,
                "labels": [{"name": "rollout-ready"}],
                "isCrossRepository": False,
            },
            "issue_comments": [self.vetted_comment(self.head)],
            "events": [{"event": "labeled", "label": {"name": "rollout-ready"}, "actor": {"login": "steward-bot"}}],
            "checks": [{"name": "ci", "bucket": "pass", "state": "SUCCESS"}],
        }
        state["pr"].update(pr or {})
        state.update(top)
        self.gh_state.write_text(json.dumps(state))

    def gh_calls(self):
        return read_json(self.gh_state).get("calls", [])

    @staticmethod
    def vetted_comment(sha, login="steward-bot", edited=False):
        return {"user": {"login": login}, "body": f"Research: minor.\n\nrollout-ready-head: {sha}", "created_at": "2026-11-01T10:00:00Z", "updated_at": "2026-11-01T10:05:00Z" if edited else "2026-11-01T10:00:00Z"}

    def opts(self, *extra):
        argv = ["--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--outside", "12", "--lock-ref", TEST_REF]
        return merge._parser().parse_args(argv + ["--lock-wait", "0.3", "--lock-poll", "0.05", *extra])

    def run_merge(self, *extra):
        return outside.merge_outside(self.root, self.run_dir, self.repo, 12, self.tools, self.opts(*extra))

    def refusal(self, *extra) -> str:
        with self.assertRaises(Refusal) as caught:
            self.run_merge(*extra)
        return " | ".join(caught.exception.reasons)

    def remote_lock(self):
        return merge.lock_remote_sha(self.tools, self.repo, TEST_REF)

    def record(self, run_dir=None, pr=12, verify=None, state="MERGED"):
        """A merge of an outside pull request this program made earlier."""
        d = outside.record_dir(run_dir or self.run_dir, pr)
        d.mkdir(parents=True, exist_ok=True)
        write_json(d / "merge.json", {"pr": pr, "repo": "owner/repo", "branch": BRANCH, "head_sha": "a" * 40, "run": "run-1", "state": state, "merge_sha": "c" * 40, "at": utc_now()})
        if verify:
            write_json(d / "verify.json", {"result": verify})
        return d


class MergeOutsideTest(OutsideCase):
    def test_a_ready_pull_request_is_squashed_under_the_lock_and_recorded(self):
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["outside"]), (0, "merged", True))
        merge_call = next(c for c in self.gh_calls() if c[:2] == ["pr", "merge"])
        self.assertIn("--squash", merge_call)
        self.assertEqual(merge_call[merge_call.index("--match-head-commit") + 1], self.head)
        self.assertIn("rollout-ready", merge_call[merge_call.index("--body") + 1])
        record = read_json(outside.record_dir(self.run_dir, 12) / "merge.json")
        self.assertEqual((record["state"], record["merge_sha"], record["head_sha"], record["run"]), ("MERGED", "f" * 40, self.head, "run-1"))
        self.assertIsNone(record["attribution_on_base"])
        self.assertIsNone(self.remote_lock())
        for leftover in ("merge-lock.json", "intent.json"):
            self.assertFalse((outside.record_dir(self.run_dir, 12) / leftover).exists())
        # Its deploy check is now what the repository waits on.
        self.assertEqual(len(outside.pending(self.root, "owner/repo")), 1)

    def test_a_dry_run_checks_the_gates_and_merges_nothing(self):
        code, result = self.run_merge("--dry-run")
        self.assertEqual((code, result["result"]), (0, "dry-run-passed"))
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))
        self.assertFalse((outside.record_dir(self.run_dir, 12) / "merge.json").exists())

    def test_a_program_that_does_not_adopt_refuses_and_merges_nothing(self):
        for value in (None, "off"):
            self.settings(outside_prs=value)
            said = self.refusal()
            self.assertIn("does not adopt outside pull requests (`outside_prs` is 'off' in program.json)", said)
        self.settings(outside_prs="adopt", outside_pr_vetters=None)
        self.assertIn("names no steward logins (`outside_pr_vetters`)", self.refusal())
        # Only the pull request was read, to settle a merge this program may have begun.
        self.assertEqual({tuple(c[:2]) for c in self.gh_calls()}, {("pr", "view")})
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))

    def test_only_a_pull_request_a_steward_marked_ready_is_adopted(self):
        self.set_gh(pr={"labels": []})
        self.assertIn("does not carry the label `rollout-ready`", self.refusal())
        self.set_gh(pr={"labels": [{"name": "rollout-ready"}, {"name": "rollout-held"}]})
        self.assertIn("carries `rollout-held`", self.refusal())

    def test_a_branch_no_steward_or_renovate_opens_is_refused(self):
        git(self.repo, "checkout", "-b", "feature/mine")
        git(self.repo, "push", "origin", "feature/mine")
        self.set_gh(pr={"headRefName": "feature/mine"})
        self.assertIn("only a branch starting secfix/, steward/, renovate/ is adopted", self.refusal())

    def test_a_pull_request_into_another_branch_is_refused(self):
        git(self.repo, "push", "origin", "main:release")
        self.set_gh(pr={"baseRefName": "release"})
        self.assertIn("not this program's base `main`", self.refusal())

    def test_checks_that_are_not_green_or_not_there_are_refused(self):
        self.set_gh(checks=[{"name": "ci", "bucket": "fail", "state": "FAILURE"}])
        self.assertIn("required checks are not green: ci=fail", self.refusal())
        self.set_gh(checks=[{"name": "ci", "bucket": "pending", "state": "IN_PROGRESS"}])
        self.assertIn("ci=pending", self.refusal())
        self.set_gh(checks=None)
        self.assertIn("no required checks are reported", self.refusal())

    def test_a_branch_behind_its_base_answers_4_and_merges_nothing(self):
        self.set_gh(pr={"mergeStateStatus": "BEHIND"})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (4, "needs-update"))
        self.assertIn("gh pr update-branch 12 --repo owner/repo", result["next"])
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))
        self.assertIsNone(self.remote_lock())

    def test_a_conflict_is_the_stewards_to_resolve(self):
        self.set_gh(pr={"mergeStateStatus": "DIRTY", "mergeable": "CONFLICTING"})
        self.assertIn("conflicts with its base: the steward that opened it resolves that", self.refusal())

    def test_a_path_the_spec_protects_is_refused_with_no_exemption(self):
        self.commit("brand/family/logo.svg", "<svg/>\n", "Touch the brand")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "origin", BRANCH)
        self.set_gh(pr={"headRefOid": self.head})
        self.settings(repos={"owner/repo": {"protected": ["brand/family/**"], "deploys": []}})
        self.assertIn("changes paths the spec protects, which no outside pull request may: brand/family/logo.svg", self.refusal())

    def test_attribution_on_the_pull_request_is_refused(self):
        self.set_gh(pr={"body": "Co-authored-by: Claude <noreply@anthropic.com>"})
        self.assertIn("pull request body carries AI attribution", self.refusal())

    def test_nothing_merges_on_top_of_an_items_deploy_check_that_has_not_passed(self):
        other = self.root / "runs" / "run-2"
        items.init_run(other, "run-2", "owner/repo", [str(self.backlog)])
        item = items.load_item(other, "A-1")
        item["status"] = "merged"
        item["fields"]["verify"] = "verify-failed"
        items.save_item(other, item)
        self.assertIn("run-2/A-1 is merged and its Verify is not recorded as passed (verify=verify-failed)", self.refusal())

    def test_nothing_merges_on_top_of_an_item_whose_pull_request_github_shows_merged(self):
        item = items.load_item(self.run_dir, "A-1")
        item["status"] = "in_progress"
        item["fields"]["pr"] = "7"
        items.save_item(self.run_dir, item)
        self.set_gh(other_prs={"7": {"state": "MERGED", "autoMergeRequest": None}})
        self.assertIn("run-1/A-1's pull request #7 is merged on GitHub and not recorded", self.refusal())
        self.set_gh(other_prs={"7": {"state": "OPEN", "autoMergeRequest": {"enabledAt": "x"}}})
        self.assertIn("is armed to merge itself", self.refusal())
        # Open and not armed: it has not landed, whatever local files say.
        write_json(items.item_dir(self.run_dir, "A-1") / "merge.json", {"state": "MERGE-COMMAND-FAILED"})
        self.set_gh(other_prs={"7": {"state": "OPEN", "autoMergeRequest": None}})
        self.assertEqual(self.run_merge()[0], 0)

    def test_nothing_merges_on_top_of_another_outside_deploy_check_that_has_not_passed(self):
        self.record(pr=9)
        self.assertIn("outside pull request #9 (merged by run run-1, deploy check not run)", self.refusal())
        self.record(pr=9, verify="verified")
        code, _ = self.run_merge()
        self.assertEqual(code, 0)

    def test_a_repository_that_runs_a_train_is_refused(self):
        self.settings(repos={"owner/repo": {"train": {}, "deploys": []}})
        said = self.refusal()
        self.assertIn("runs a train", said)
        self.assertIn("hl-handoff decline", said)

    def test_a_repository_other_than_the_runs_is_refused(self):
        said = self.refusal("--repo", "owner/other")
        self.assertIn("--repo owner/other is not this run's repository (owner/repo)", said)

    def test_a_merge_recorded_already_is_said_and_not_made_again(self):
        self.run_merge()
        merges = sum(1 for c in self.gh_calls() if c[:2] == ["pr", "merge"])
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["merge_sha"]), (0, "already-recorded", "f" * 40))
        self.assertEqual(sum(1 for c in self.gh_calls() if c[:2] == ["pr", "merge"]), merges)

    def test_a_merge_that_died_after_github_merged_is_recorded_by_running_it_again(self):
        d = outside.record_dir(self.run_dir, 12)
        d.mkdir(parents=True)
        token = merge.acquire_lock(self.tools, self.repo, "run-1", "outside-12", ref=TEST_REF, wait_s=0.3, poll_s=0.05)
        write_json(d / "merge-lock.json", token)
        write_json(d / "intent.json", {"head_sha": self.head, "run": "run-1"})
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["recovered"], result["merge_sha"]), (0, "merged", True, "c" * 40))
        self.assertIsNone(self.remote_lock())
        self.assertFalse((d / "merge-lock.json").exists())

    def test_a_pull_request_merged_by_anyone_else_is_not_recorded(self):
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        self.assertIn("is merged already, and not by this program", self.refusal())
        self.assertFalse((outside.record_dir(self.run_dir, 12) / "merge.json").exists())

    def test_a_merge_command_that_fails_keeps_its_intent_until_a_re_run_settles_it(self):
        self.set_gh(merge_rc=1)
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (5, "merge-failed"))
        self.assertIn("run `hl-merge --outside 12` again", result["next"])
        self.assertIsNone(self.remote_lock())
        self.assertFalse((outside.record_dir(self.run_dir, 12) / "merge.json").exists())
        self.assertEqual(len(outside.pending(self.root, "owner/repo")), 1)
        # Still open on the re-run: it never landed, and is cleared before the merge is tried again.
        self.set_gh()
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))

    def test_a_merge_command_that_fails_after_github_merged_is_recorded(self):
        real = self.tools.gh

        def gh(args, cwd, check=True):
            if args[:2] == ["pr", "merge"]:
                state = read_json(self.gh_state)
                state["pr"].update(state="MERGED", mergeCommit={"oid": "d" * 40})
                self.gh_state.write_text(json.dumps(state))
                return subprocess.CompletedProcess(args, 1, "", "HTTP 502: Bad Gateway")
            return real(args, cwd, check)

        with mock.patch.object(self.tools, "gh", side_effect=gh):
            code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["merge_sha"]), (0, "merged", "d" * 40))
        self.assertTrue(any("GitHub shows it merged" in w for w in result["warnings"]))
        self.assertIn("deploy check not run", outside.pending(self.root, "owner/repo")[0])

    def test_the_lock_is_the_same_lock_an_item_merge_takes(self):
        held = merge.acquire_lock(self.tools, self.repo, "run-9", "B-1", ref=TEST_REF, wait_s=0.3, poll_s=0.05)
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["holder"]), (3, "lock-timeout", "run-9/B-1"))
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))
        merge.release_lock(self.tools, self.repo, held)

    def test_session_start_frees_what_a_dead_outside_merge_left_and_an_items_recovery_does_not(self):
        d = outside.record_dir(self.run_dir, 12)
        d.mkdir(parents=True)
        token = merge.acquire_lock(self.tools, self.repo, "outside:run-1", "outside-12", ref=TEST_REF, wait_s=0.3, poll_s=0.05)
        write_json(d / "merge-lock.json", token)
        # An item's recovery in the same run frees what names its run, which an outside merge never does.
        self.assertEqual(merge.release_own(self.tools, self.repo, self.run_dir, ref=TEST_REF), [])
        self.assertIsNotNone(self.remote_lock())
        freed = outside.release_dead(self.tools, self.repo, self.run_dir, ref=TEST_REF, only_dead=True)
        self.assertTrue(any("outside-12" in line for line in freed), freed)
        self.assertIsNone(self.remote_lock())
        self.assertFalse((d / "merge-lock.json").exists())

    def test_the_lock_names_the_outside_merge_and_its_run(self):
        seen = {}
        real = merge.acquire_lock

        def acquire(tools, repo, run_id, item_id, **kw):
            seen.update(run_id=run_id, item_id=item_id)
            return real(tools, repo, run_id, item_id, **kw)

        with mock.patch.object(merge, "acquire_lock", side_effect=acquire):
            self.run_merge()
        self.assertEqual(seen, {"run_id": "outside:run-1", "item_id": "outside-12"})

    def test_the_command_line_takes_outside_alone(self):
        argv = ["--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--git", "/usr/bin/git", "--gh", str(self.gh), "--lock-ref", TEST_REF]
        with mock.patch.dict(os.environ, {"HL_FAKE_GH": str(self.gh_state)}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(merge.main(argv + ["--outside", "12", "--id", "A-1"]), 2)
            self.assertEqual(merge.main(argv + ["--id", "A-1", "--repo", "owner/repo"]), 2)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = merge.main(argv + ["--outside", "12", "--dry-run"])
        self.assertEqual((code, json.loads(out.getvalue())["result"]), (0, "dry-run-passed"))
        with mock.patch.dict(os.environ, {"HL_FAKE_GH": str(self.gh_state)}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(merge.main(argv + ["--outside", "12", "--allow-no-required-checks"]), 2)


class VettedHeadTest(OutsideCase):
    """The head merged is the one the steward vetted, or that head with only the base taken in."""

    def test_no_line_naming_the_vetted_commit_is_refused(self):
        self.set_gh(issue_comments=[{"user": {"login": "steward-bot"}, "body": "Looks fine to me.", "created_at": "t", "updated_at": "t"}])
        self.assertIn("has no `rollout-ready-head: <sha>` line from a steward's login", self.refusal())

    def test_a_head_pushed_after_the_steward_looked_is_refused(self):
        vetted = self.head
        self.head = self.commit("requirements.txt", "pillow==13.1.0\n", "Bump pillow to 13.1.0")
        git(self.repo, "push", "origin", BRANCH)
        self.set_gh(pr={"headRefOid": self.head}, issue_comments=[self.vetted_comment(vetted)])
        said = self.refusal()
        self.assertIn(f"head is {self.head[:12]} but the steward vetted {vetted[:12]}, and commits not on the base were pushed since", said)
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))

    def test_a_head_that_only_took_the_base_in_is_the_same_change(self):
        vetted = self.head
        git(self.repo, "checkout", "main")
        self.commit("OTHER.md", "other\n", "Something else landed on main")
        git(self.repo, "push", "origin", "main")
        git(self.repo, "checkout", BRANCH)
        git(self.repo, "merge", "--no-edit", "main")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "origin", BRANCH)
        self.set_gh(pr={"headRefOid": self.head}, issue_comments=[self.vetted_comment(vetted)])
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))
        self.assertTrue(any("by taking in the base only" in w for w in result["warnings"]), result["warnings"])

    def test_the_last_line_a_steward_wrote_is_the_one_that_counts(self):
        self.set_gh(issue_comments=[self.vetted_comment(self.head), self.vetted_comment("e" * 40)])
        self.assertIn(f"but the steward vetted {'e' * 12}", self.refusal())

    def test_a_line_or_a_label_from_anyone_but_a_steward_does_not_count(self):
        vetted = self.head
        self.head = self.commit("requirements.txt", "pillow==13.1.0\n", "Bump pillow to 13.1.0")
        git(self.repo, "push", "origin", BRANCH)
        forged = [self.vetted_comment(vetted), self.vetted_comment(self.head, login="someone")]
        self.set_gh(pr={"headRefOid": self.head}, issue_comments=forged)
        self.assertIn(f"but the steward vetted {vetted[:12]}", self.refusal())
        # A steward's newest comment edited afterwards (by anyone with write access) counts for nothing, and an older
        # one does not come back in its place.
        self.set_gh(pr={"headRefOid": self.head}, issue_comments=[self.vetted_comment(vetted), self.vetted_comment(self.head, edited=True)])
        self.assertIn("newest `rollout-ready-head:` comment was edited after it was written", self.refusal())
        self.set_gh(events=[{"event": "labeled", "label": {"name": "rollout-ready"}, "actor": {"login": "someone"}}])
        self.assertIn("`rollout-ready` was last put on by someone, not one of the stewards' logins (steward-bot)", self.refusal())

    def test_a_head_restored_by_a_later_commit_is_not_the_vetted_one(self):
        # The steward vetted A; B came and went (its vetting deleted), and a commit C restores A's tree on top.
        vetted = self.head
        self.commit("requirements.txt", "pillow==13.1.0\n", "Bump pillow to 13.1.0")
        self.head = self.commit("requirements.txt", "pillow==13.0.0\n", "Back to 13.0.0")
        git(self.repo, "push", "origin", BRANCH)
        self.set_gh(pr={"headRefOid": self.head}, issue_comments=[self.vetted_comment(vetted)])
        self.assertIn("commits not on the base were pushed since", self.refusal())

    def test_a_force_push_after_the_vetting_voids_it(self):
        self.set_gh(events=[
            {"event": "labeled", "label": {"name": "rollout-ready"}, "actor": {"login": "steward-bot"}, "created_at": "2026-11-01T09:00:00Z"},
            {"event": "head_ref_force_pushed", "actor": {"login": "someone"}, "created_at": "2026-11-01T11:00:00Z"},
        ])
        self.assertIn("force-pushed at 2026-11-01T11:00:00Z, after the steward vetted it", self.refusal())

    def test_a_steward_login_the_loop_also_acts_as_is_refused_unless_accepted(self):
        self.set_gh(me="steward-bot")
        said = self.refusal()
        self.assertIn("the loop's own token acts as steward-bot, which is also a steward's login", said)
        self.settings(outside_pr_shared_login="accept")
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["shared_login_accepted"]), (0, "merged", True))
        self.assertTrue(any("program.json accepts that a worker could have marked this ready" in w for w in result["warnings"]))
        self.assertIn("shared login accepted", ledger.items_text(self.root, "p", "now"))

    def test_a_repository_whose_spec_does_not_say_what_deploys_it_is_refused(self):
        self.settings(repos={"owner/repo": {}})
        self.assertIn("the spec does not say what deploys owner/repo", self.refusal())
        self.settings(repos={"owner/repo": {"deploys": ["apply-mian.yml"]}})
        self.assertIn("`deploys` names apply-mian.yml, which origin/main of owner/repo does not have", self.refusal())

    def test_a_fork_is_refused_before_anything_is_fetched(self):
        self.set_gh(pr={"isCrossRepository": True})
        said = self.refusal()
        self.assertIn("is from a fork", said)
        self.assertFalse(any(c[:1] == ["api"] and c[1].endswith("/comments") for c in self.gh_calls()))

    def test_a_label_or_base_that_changes_during_the_check_stops_the_merge(self):
        real = self.tools.gh
        looks = {"n": 0}

        def gh(args, cwd, check=True):
            if args[:3] == ["pr", "view", "12"] and "labels" in args[-1]:
                looks["n"] += 1
                if looks["n"] == 3:  # the last look before the merge
                    state = read_json(self.gh_state)
                    state["pr"]["labels"] = [{"name": "rollout-held"}]
                    self.gh_state.write_text(json.dumps(state))
            return real(args, cwd, check)

        with mock.patch.object(self.tools, "gh", side_effect=gh):
            said = self.refusal()
        self.assertIn("changed while it was being checked (its labels are now rollout-held)", said)
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in self.gh_calls()))
        self.assertFalse((outside.record_dir(self.run_dir, 12) / "intent.json").exists())
        self.assertIsNone(self.remote_lock())


class BegunMergeTest(OutsideCase):
    """A merge that began and has no record holds the repository until a re-run settles it."""

    def begin(self, run_dir=None, head=None):
        d = outside.record_dir(run_dir or self.run_dir, 12)
        d.mkdir(parents=True, exist_ok=True)
        write_json(d / "intent.json", {"head_sha": head or self.head, "run": "run-1", "at": utc_now()})
        return d

    def test_it_holds_the_repository_and_a_re_run_records_it(self):
        d = self.begin()
        said = outside.pending(self.root, "owner/repo")
        self.assertEqual(len(said), 1)
        self.assertIn("began to merge in run run-1 and has no record, so it may have landed", said[0])
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["recovered"]), (0, "merged", True))
        self.assertFalse((d / "intent.json").exists())
        self.assertEqual(len(outside.pending(self.root, "owner/repo")), 1)  # now its deploy check holds it
        self.assertIn("deploy check not run", outside.pending(self.root, "owner/repo")[0])

    def test_a_re_run_from_another_run_records_it_where_it_began(self):
        other = self.root / "runs" / "run-2"
        items.init_run(other, "run-2", "owner/repo", [str(self.backlog)])
        d = self.begin(run_dir=other)
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["run"]), (0, "merged", "run-2"))
        self.assertTrue((d / "merge.json").exists())
        self.assertFalse((d / "intent.json").exists())

    def test_a_pull_request_closed_without_landing_clears_it(self):
        d = self.begin()
        self.set_gh(pr={"state": "CLOSED"})
        said = self.refusal()
        self.assertIn("is CLOSED: nothing to adopt; the begun merge recorded in run-1 is cleared", said)
        self.assertFalse((d / "intent.json").exists())
        self.assertEqual(outside.pending(self.root, "owner/repo"), [])

    def test_a_second_run_never_clears_the_intent_of_a_merge_in_flight(self):
        d = self.begin()
        write_json(d / "merge-lock.json", {"held_sha": "a" * 40, **outside._holder()})
        self.set_gh(pr={"state": "CLOSED"})
        said = self.refusal()
        self.assertNotIn("is cleared", said)
        self.assertTrue((d / "intent.json").exists())

    def test_a_reused_pid_is_not_taken_for_the_merge_that_died(self):
        d = self.begin()
        # The pid is alive (it is this test's), but it started at another time than the one the token names.
        write_json(d / "merge-lock.json", {"held_sha": "a" * 40, **outside._holder(), "start": "Mon Jan  1 00:00:00 2001"})
        self.set_gh(pr={"state": "CLOSED"})
        self.assertIn("is cleared, since it never landed", self.refusal())
        self.assertFalse((d / "intent.json").exists())

    def test_a_dry_run_clears_nothing(self):
        d = self.begin()
        self.set_gh(pr={"state": "CLOSED"})
        self.refusal("--dry-run")
        self.assertTrue((d / "intent.json").exists())

    def test_its_own_re_run_while_still_open_is_not_held_by_it(self):
        self.begin()
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))

    def test_a_decline_clears_a_begun_merge_that_never_landed(self):
        d = self.begin()
        gh = lambda args, timeout=None: self.tools.gh(args, self.repo, check=False)  # noqa: E731
        cfg = read_json(self.root / "program.json")
        outside.decline(cfg, "owner/repo", 12, "conflicts with HD-23", gh, root=self.root)
        self.assertFalse((d / "intent.json").exists())
        self.assertEqual(outside.pending(self.root, "owner/repo"), [])

    def test_a_program_turned_off_still_settles_what_it_began(self):
        d = self.begin()
        self.settings(outside_prs="off")
        self.assertIn("does not adopt", self.refusal())
        self.assertFalse((d / "intent.json").exists())  # open, nothing armed: it never landed
        self.begin()
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))

    def test_a_recovered_merge_keeps_the_shared_login_acceptance(self):
        d = self.begin()
        intent = read_json(d / "intent.json")
        write_json(d / "intent.json", {**intent, "shared_login_accepted": True})
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge()
        self.assertEqual((code, result["recovered"], result["shared_login_accepted"]), (0, True, True))

    def test_a_begun_merge_that_landed_at_another_head_is_recorded_and_said(self):
        d = self.begin(head="a" * 40)
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (0, "merged"))
        self.assertIn("tell the operator", result["warning"])
        self.assertEqual(read_json(d / "merge.json")["landed_at_another_head"]["began"], "a" * 40)
        self.assertIn("deploy check not run", outside.pending(self.root, "owner/repo")[0])

    def test_another_pull_request_is_held_by_it(self):
        d = outside.record_dir(self.run_dir, 9)
        d.mkdir(parents=True)
        write_json(d / "intent.json", {"head_sha": "a" * 40})
        self.assertIn("outside pull request #9 began to merge in run run-1", self.refusal())

    def test_a_merge_github_did_not_finish_keeps_its_intent(self):
        def gh(args, cwd, check=True):
            if args[:2] == ["pr", "view"] and args[-1] == "state,mergeCommit":
                return subprocess.CompletedProcess(args, 0, json.dumps({"state": "OPEN", "mergeCommit": None}), "")
            return real(args, cwd, check)

        real = self.tools.gh
        with mock.patch.object(self.tools, "gh", side_effect=gh):
            code, result = self.run_merge("--merge-wait", "0")
        self.assertEqual((code, result["result"]), (5, "merge-not-observed"))
        self.assertTrue((outside.record_dir(self.run_dir, 12) / "intent.json").exists())
        self.assertEqual(len(outside.pending(self.root, "owner/repo")), 1)
        self.assertIsNone(self.remote_lock())

    def test_no_merge_commit_named_is_not_recorded_as_merged(self):
        def gh(args, cwd, check=True):
            if args[:2] == ["pr", "view"] and args[-1] == "state,mergeCommit":
                return subprocess.CompletedProcess(args, 0, json.dumps({"state": "MERGED", "mergeCommit": None}), "")
            return real(args, cwd, check)

        real = self.tools.gh
        with mock.patch.object(self.tools, "gh", side_effect=gh):
            code, result = self.run_merge()
        self.assertEqual((code, result["result"]), (5, "merge-commit-unknown"))
        self.assertFalse((outside.record_dir(self.run_dir, 12) / "merge.json").exists())
        self.assertTrue((outside.record_dir(self.run_dir, 12) / "intent.json").exists())
        # GitHub names it later: a re-run records it.
        code, result = self.run_merge()
        self.assertEqual((code, result["result"], result["merge_sha"]), (0, "merged", "f" * 40))

    def test_a_recovery_mid_session_leaves_a_live_holders_lock(self):
        d = self.begin()
        token = merge.acquire_lock(self.tools, self.repo, "outside:run-1", "outside-12", ref=TEST_REF, wait_s=0.3, poll_s=0.05)
        write_json(d / "merge-lock.json", {**token, **outside._holder()})
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge()
        self.assertEqual((code, result["released"]), (0, []))
        self.assertEqual(self.remote_lock(), token["held_sha"])
        merge.release_lock(self.tools, self.repo, token)

    def test_a_recovery_frees_a_dead_holders_lock(self):
        d = self.begin()
        token = merge.acquire_lock(self.tools, self.repo, "outside:run-1", "outside-12", ref=TEST_REF, wait_s=0.3, poll_s=0.05)
        write_json(d / "merge-lock.json", {**token, "pid": 999999, "host": __import__("socket").gethostname()})
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge()
        self.assertEqual(code, 0)
        self.assertTrue(any("outside-12" in line for line in result["released"]), result["released"])
        self.assertIsNone(self.remote_lock())

    def test_a_recovery_frees_only_its_own_lock(self):
        d = self.begin()
        held = merge.acquire_lock(self.tools, self.repo, "run-1", "A-3", ref=TEST_REF, wait_s=0.3, poll_s=0.05)
        self.set_gh(pr={"state": "MERGED", "mergeCommit": {"oid": "c" * 40}})
        code, result = self.run_merge()
        self.assertEqual((code, result["released"]), (0, []))
        self.assertEqual(self.remote_lock(), held["held_sha"])
        merge.release_lock(self.tools, self.repo, held)
        self.assertTrue((d / "merge.json").exists())

    def test_unreadable_records_hold_every_repository(self):
        d = outside.record_dir(self.run_dir, 12)
        d.mkdir(parents=True)
        (d / "merge.json").write_text("{not json")
        said = outside.pending(self.root, "owner/repo")
        self.assertIn("could not be read", said[0])
        self.assertIn("## Outside pull requests", ledger.items_text(self.root, "p", "now"))

    def test_a_record_with_no_state_holds_the_repository_and_never_fails_a_save(self):
        d = outside.record_dir(self.run_dir, 12)
        d.mkdir(parents=True)
        write_json(d / "merge.json", {})
        self.assertIn("could not be read", outside.pending(self.root, "owner/repo")[0])
        self.assertIn("unreadable", ledger.items_text(self.root, "p", "now"))

    def test_a_run_directory_outside_the_programs_runs_is_refused(self):
        stray = self.tmp / "elsewhere" / "run-x"
        items.init_run(stray, "run-x", "owner/repo", [str(self.backlog)])
        with self.assertRaises(Refusal) as caught:
            outside.merge_outside(self.root, stray, self.repo, 12, self.tools, self.opts())
        self.assertIn("is not a run of the program", str(caught.exception))


class ItemGateTest(MergeCase):
    """An item's own merge gate, in a run under a program root (`<root>/runs/<run>`, as staging makes it), in a
    program that merged an outside pull request into the same repository."""

    def init(self, *files):
        self.root = self.tmp / "loops" / "p"
        self.root.mkdir(parents=True, exist_ok=True)
        write_json(self.root / "program.json", {"program": "p", "base": "main", "worker_claude_ref": "-", "gh_token_ref": "x", "cursor_model": "c"})
        self.run_dir = self.root / "runs" / "run-1"
        return super().init(*files)

    def outside_record(self, verify=None):
        d = outside.record_dir(self.run_dir, 12)
        d.mkdir(parents=True, exist_ok=True)
        write_json(d / "merge.json", {"pr": 12, "state": "MERGED", "merge_sha": "c" * 40, "at": utc_now()})
        if verify:
            write_json(d / "verify.json", {"result": verify})

    def test_an_item_does_not_merge_on_top_of_an_outside_deploy_check_that_has_not_passed(self):
        self.outside_record()
        with self.assertRaises(Refusal) as caught:
            merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        said = str(caught.exception)
        self.assertIn("outside pull request #12 (merged by run run-1, deploy check not run)", said)
        self.assertIn("nothing else merges into owner/repo until it has passed", said)
        self.outside_record(verify="no-run")
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)

    def test_the_item_that_fixes_a_failed_outside_deploy_forward_is_the_one_let_through(self):
        self.outside_record(verify="failed")
        with self.assertRaises(Refusal):
            merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        # A plain field does nothing: only the manager's guarded record opens the hold.
        items.set_fields(self.run_dir, "A-1", ["fixes_outside=12"])
        with self.assertRaises(Refusal):
            merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        self.record_fix_forward(why=None)
        passed = merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        self.assertEqual(passed["pr"], 7)
        self.assertTrue(any("merging past outside pull request #12's failed deploy check" in w for w in passed["warnings"]))
        # Only while it is failed: one whose check has not run yet still holds it.
        self.outside_record()
        (outside.record_dir(self.run_dir, 12) / "verify.json").unlink()
        with self.assertRaises(Refusal):
            merge.gate(self.run_dir, "A-1", self.repo, self.tools)

    def record_fix_forward(self, why=None, pr=12):
        from hl import claim

        args = items._parser().parse_args(["--run-dir", str(self.run_dir), "fixes-outside", "--id", "A-1", "--pr", str(pr), "--reason", "pins pillow back"])
        with mock.patch.object(claim, "read_claim", return_value={"pid": 1, "session": "s"}), mock.patch.object(claim, "started_by_holders_shell", return_value=why):
            return items._fixes_outside_from_cli(args, procs=mock.Mock())

    def test_the_fix_forward_is_recorded_only_by_the_manager_and_only_for_a_failed_deploy(self):
        with self.assertRaises(Refusal) as caught:
            self.record_fix_forward()
        self.assertIn("no outside pull request #12 with a failed deploy check", str(caught.exception))
        self.outside_record(verify="failed")
        with self.assertRaises(Refusal) as caught:
            self.record_fix_forward(why="started by a script, not the holder's shell")
        self.assertIn("recorded only by the manager that holds the program", str(caught.exception))
        item = self.record_fix_forward()
        self.assertEqual((item["fixes_outside"]["pr"], item["fixes_outside"]["granted_by"]["session"]), (12, "s"))

    def test_nor_on_a_merge_that_began_and_has_no_record(self):
        d = outside.record_dir(self.run_dir, 12)
        d.mkdir(parents=True)
        write_json(d / "intent.json", {"head_sha": "a" * 40})
        with self.assertRaises(Refusal) as caught:
            merge.gate(self.run_dir, "A-1", self.repo, self.tools)
        self.assertIn("may have landed", str(caught.exception))

    def test_a_program_with_no_outside_merges_reads_nothing_and_adds_no_reason(self):
        with mock.patch.object(outside.items, "load_run", wraps=outside.items.load_run) as loads:
            self.assertEqual(outside.pending(self.root, "owner/repo"), [])
        self.assertEqual(loads.call_count, 0)
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.repo, self.tools)["pr"], 7)


class VerifyOutsideTest(OutsideCase):
    def setUp(self):
        super().setUp()
        self.d = self.record()
        # The deploy workflows the tests name, on the base.
        git(self.repo, "checkout", "main")
        for name in ("apply-main.yml", "desk-apply.yml"):
            self.commit(f".github/workflows/{name}", "on: push\n", f"add {name}")
        git(self.repo, "push", "origin", "main")
        git(self.repo, "checkout", BRANCH)

    def verify(self, *, now=None, wait=False, retry=False, deploy_wait=600):
        opts = mock.Mock(wait=wait, retry=retry, note="re-ran it", interval=1, deploy_wait=deploy_wait)
        clock = (lambda: now) if now is not None else time.time
        return outside.verify_outside(self.root, self.run_dir, self.repo, 12, self.tools, opts, clock=clock, sleep=lambda s: None)

    def runs(self, *found):
        self.set_gh(runs={"apply-main.yml": list(found)})

    def test_a_repository_whose_spec_says_nothing_deploys_has_none_to_check(self):
        code, result = self.verify()
        self.assertEqual((code, result["result"]), (0, "no-deploy"))
        self.assertIn("deploys nothing", result["fact"])
        self.assertEqual(outside.pending(self.root, "owner/repo"), [])

    def test_a_spec_that_does_not_say_what_deploys_is_never_read_as_no_deploy(self):
        self.settings(repos={"owner/repo": {}})
        with self.assertRaises(Refusal) as caught:
            self.verify()
        self.assertIn("the spec does not say what deploys owner/repo", str(caught.exception))
        self.assertEqual(len(outside.pending(self.root, "owner/repo")), 1)

    def test_a_cancelled_deploy_whose_successor_is_still_running_is_waited_for(self):
        git(self.repo, "checkout", "main")
        merged = self.commit("merged.txt", "merged\n", "Bump pillow (#12)")
        later = self.commit("later.txt", "later\n", "Something after it")
        git(self.repo, "push", "origin", "main")
        record = read_json(self.d / "merge.json")
        write_json(self.d / "merge.json", {**record, "merge_sha": merged})
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        self.set_gh(
            runs={"apply-main.yml": [{"databaseId": 5, "status": "completed", "conclusion": "cancelled", "headSha": merged}]},
            branch_runs={"apply-main.yml": [{"databaseId": 7, "status": "in_progress", "conclusion": None, "headSha": later}]},
        )
        self.assertEqual(self.verify()[1]["result"], "not-yet")
        self.set_gh(
            runs={"apply-main.yml": [{"databaseId": 5, "status": "completed", "conclusion": "cancelled", "headSha": merged}]},
            branch_runs={"apply-main.yml": [{"databaseId": 7, "status": "completed", "conclusion": "success", "headSha": later}]},
        )
        self.assertEqual(self.verify()[1]["result"], "verified")

    def test_a_cancelled_deploy_whose_successor_failed_is_a_failure_with_its_successor_named(self):
        git(self.repo, "checkout", "main")
        merged = self.commit("merged.txt", "merged\n", "Bump pillow (#12)")
        later = self.commit("later.txt", "later\n", "Something after it")
        git(self.repo, "push", "origin", "main")
        record = read_json(self.d / "merge.json")
        write_json(self.d / "merge.json", {**record, "merge_sha": merged})
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        self.set_gh(
            runs={"apply-main.yml": [{"databaseId": 5, "status": "completed", "conclusion": "cancelled", "headSha": merged}]},
            branch_runs={"apply-main.yml": [{"databaseId": 7, "status": "completed", "conclusion": "failure", "headSha": later}]},
        )
        code, result = self.verify()
        self.assertEqual((code, result["result"], result["workflows"][0]["by_sha"]), (4, "failed", later))
        self.assertIn("never re-run the deploy of an older commit", result["workflows"][0]["fact"])

    def test_a_deploy_run_that_passed_verifies_it(self):
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        self.runs({"databaseId": 5, "status": "completed", "conclusion": "success", "url": "u", "event": "push"})
        code, result = self.verify()
        self.assertEqual((code, result["result"]), (0, "verified"))
        call = next(c for c in self.gh_calls() if c[:2] == ["run", "list"])
        self.assertEqual(call[call.index("--commit") + 1], "c" * 40)
        self.assertEqual(read_json(self.d / "verify.json")["result"], "verified")

    def test_a_failed_deploy_is_recorded_and_held_until_a_retry(self):
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        self.runs({"databaseId": 5, "status": "completed", "conclusion": "failure", "url": "u", "event": "push"})
        self.assertEqual(self.verify()[0], 4)
        self.assertEqual(len(outside.pending(self.root, "owner/repo")), 1)
        # Read again without --retry: the recorded failure stands.
        self.runs({"databaseId": 6, "status": "completed", "conclusion": "success", "url": "u", "event": "push"})
        code, result = self.verify()
        self.assertEqual((code, result["result"]), (4, "failed"))
        code, result = self.verify(retry=True)
        self.assertEqual((code, result["result"], result["retry"]["note"]), (0, "verified", "re-ran it"))

    def test_not_yet_before_the_deadline_and_no_run_after_it(self):
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        self.runs()
        code, result = self.verify()
        self.assertEqual((code, result["result"]), (3, "not-yet"))
        self.assertFalse((self.d / "verify.json").exists())
        code, result = self.verify(now=time.time() + 3600)
        self.assertEqual((code, result["result"]), (0, "no-run"))
        self.assertIn("nothing was deployed", result["fact"])

    def test_a_skipped_run_is_final_and_a_missing_one_is_waited_for(self):
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml", "desk-apply.yml"]}})
        self.set_gh(runs={"apply-main.yml": [{"databaseId": 5, "status": "completed", "conclusion": "skipped"}], "desk-apply.yml": [{"databaseId": 6, "status": "completed", "conclusion": "success"}]})
        self.assertEqual(self.verify()[1]["result"], "verified")
        (self.d / "verify.json").unlink()
        self.set_gh(runs={"apply-main.yml": [{"databaseId": 5, "status": "completed", "conclusion": "skipped"}]})
        self.assertEqual(self.verify()[1]["result"], "not-yet")
        self.assertEqual(self.verify(now=time.time() + 3600)[1]["result"], "no-run")
        (self.d / "verify.json").unlink()
        self.set_gh(runs={"apply-main.yml": [{"databaseId": 5, "status": "completed", "conclusion": "skipped"}], "desk-apply.yml": [{"databaseId": 6, "status": "completed", "conclusion": "skipped"}]})
        self.assertEqual(self.verify()[1]["result"], "no-run")

    def test_a_deploy_still_running_at_the_deadline_is_a_failure(self):
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        self.runs({"databaseId": 5, "status": "in_progress", "conclusion": None, "url": "u", "event": "push"})
        code, result = self.verify(now=time.time() + 3600)
        self.assertEqual((code, result["result"]), (4, "failed"))
        self.assertIn("still running", result["fact"])

    def test_wait_reads_again_until_it_settles(self):
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        seen = iter([[{"databaseId": 5, "status": "in_progress", "conclusion": None}], [{"databaseId": 5, "status": "completed", "conclusion": "success"}]])

        def gh(args, cwd, check=True):
            if args[:2] == ["run", "list"]:
                return subprocess.CompletedProcess(args, 0, json.dumps(next(seen)), "")
            return self.tools.gh(args, cwd, check)

        with mock.patch.object(self.tools, "gh", side_effect=gh):
            code, result = self.verify(wait=True)
        self.assertEqual((code, result["result"]), (0, "verified"))

    def test_nothing_recorded_is_refused(self):
        with self.assertRaises(Refusal) as caught:
            outside.verify_outside(self.root, self.run_dir, self.repo, 99, self.tools, mock.Mock(retry=False, deploy_wait=None, note=None))
        self.assertIn("no outside merge of #99 is recorded", str(caught.exception))

    def test_a_retry_has_a_deadline_of_its_own_and_says_what_changed(self):
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        self.runs({"databaseId": 5, "status": "in_progress", "conclusion": None})
        late = time.time() + 3600
        self.assertEqual(self.verify(now=late)[1]["result"], "failed")
        with self.assertRaises(Refusal) as caught:
            outside.verify_outside(self.root, self.run_dir, self.repo, 12, self.tools, mock.Mock(retry=True, note=None, deploy_wait=600, wait=False))
        self.assertIn("--retry needs --note", str(caught.exception))
        # Re-run, and still running: not yet, against the retry's own deadline, not the merge's.
        code, result = self.verify(now=late, retry=True)
        self.assertEqual((code, result["result"]), (3, "not-yet"))
        self.assertEqual(read_json(self.d / "verify.json")["result"], "retrying")
        self.runs({"databaseId": 6, "status": "completed", "conclusion": "success"})
        code, result = self.verify(now=late + 60)
        self.assertEqual((code, result["result"], result["retry"]["note"]), (0, "verified", "re-ran it"))

    def test_a_deadline_shorter_than_the_floor_is_refused(self):
        with self.assertRaises(Refusal) as caught:
            self.verify(deploy_wait=0)
        self.assertIn("is less than 600 s", str(caught.exception))

    def test_a_later_deploy_that_carries_the_merge_settles_it(self):
        # A real base: the merge commit, and a later commit on main whose deploy cancelled ours and passed.
        git(self.repo, "checkout", "main")
        merged = self.commit("merged.txt", "merged\n", "Bump pillow (#12)")
        later = self.commit("later.txt", "later\n", "Something after it")
        git(self.repo, "push", "origin", "main")
        record = read_json(self.d / "merge.json")
        write_json(self.d / "merge.json", {**record, "merge_sha": merged})
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        self.set_gh(
            runs={"apply-main.yml": [{"databaseId": 5, "status": "completed", "conclusion": "cancelled", "headSha": merged}]},
            branch_runs={"apply-main.yml": [{"databaseId": 7, "status": "completed", "conclusion": "success", "headSha": later}]},
        )
        code, result = self.verify()
        self.assertEqual((code, result["result"]), (0, "verified"))
        self.assertEqual((result["workflows"][0]["state"], result["workflows"][0]["by_sha"]), ("superseded", later))

    def test_a_later_deploy_still_running_is_waited_for_and_one_that_does_not_carry_it_is_not_counted(self):
        git(self.repo, "checkout", "main")
        merged = self.commit("merged.txt", "merged\n", "Bump pillow (#12)")
        git(self.repo, "push", "origin", "main")
        record = read_json(self.d / "merge.json")
        write_json(self.d / "merge.json", {**record, "merge_sha": merged})
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        base_only = git(self.repo, "rev-parse", "HEAD~1")
        self.set_gh(branch_runs={"apply-main.yml": [{"databaseId": 7, "status": "completed", "conclusion": "success", "headSha": base_only}]})
        self.assertEqual(self.verify()[1]["result"], "not-yet")
        later = self.commit("later.txt", "later\n", "Something after it")
        git(self.repo, "push", "origin", "main")
        self.set_gh(branch_runs={"apply-main.yml": [{"databaseId": 8, "status": "in_progress", "conclusion": None, "headSha": later}]})
        code, result = self.verify(now=time.time() + 3600)
        self.assertEqual((code, result["result"]), (4, "failed"))

    def test_the_command_line(self):
        from hl import claim, verify

        bins = {"bins": {"git": "/usr/bin/git", "gh": str(self.gh)}}
        argv = ["--root", str(self.root), "--run-dir", str(self.run_dir), "--repo-dir", str(self.repo), "--outside", "12"]
        out = io.StringIO()
        with mock.patch.object(claim, "check_pins", return_value=bins), mock.patch.object(merge, "Tools", return_value=self.tools), contextlib.redirect_stdout(out):
            self.assertEqual(verify.main(argv), 0)
        self.assertEqual(json.loads(out.getvalue())["result"], "no-deploy")
        with mock.patch.object(claim, "check_pins", return_value=bins), mock.patch.object(merge, "Tools", return_value=self.tools), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verify.main(argv + ["--deploy-wait", "60"]), 2)
            self.assertEqual(verify.main(argv + ["--fixed-by", "x:A-1"]), 2)

    def test_a_newer_skipped_run_does_not_hide_a_failed_or_running_one_on_the_same_commit(self):
        self.settings(repos={"owner/repo": {"deploys": ["apply-main.yml"]}})
        self.runs({"databaseId": 9, "status": "completed", "conclusion": "skipped"}, {"databaseId": 5, "status": "completed", "conclusion": "failure"})
        self.assertEqual(self.verify(), (4, mock.ANY))
        (self.d / "verify.json").unlink()
        self.runs({"databaseId": 9, "status": "completed", "conclusion": "skipped"}, {"databaseId": 5, "status": "in_progress", "conclusion": None})
        self.assertEqual(self.verify()[1]["result"], "not-yet")

    def test_a_deploy_workflow_the_base_does_not_have_is_refused(self):
        self.settings(repos={"owner/repo": {"deploys": ["apply-mian.yml"]}})
        with self.assertRaises(Refusal) as caught:
            self.verify()
        self.assertIn("`deploys` names apply-mian.yml, which origin/main of owner/repo does not have", str(caught.exception))

def fake(responses: dict):
    """A `gh` for `ready` and `decline`: each call answered by the first key its arguments start with."""
    calls = []

    def gh(args):
        calls.append(args)
        for key, answer in responses.items():
            if tuple(args[: len(key)]) == key and (len(answer) < 4 or answer[3] in args):
                return subprocess.CompletedProcess(args, answer[0], answer[1], answer[2])
        return subprocess.CompletedProcess(args, 0, "[]", "")

    gh.calls = calls
    return gh


class ReadyTest(unittest.TestCase):
    CFG = {"program": "p", "outside_prs": "adopt", "outside_pr_vetters": ["steward-bot"], "repos": {"owner/desk": {}}, "streams": {"a": {"repo": "owner/desk"}, "b": {"repo": "Owner/Id"}}}

    def test_the_ready_pull_requests_of_every_repository_are_listed_with_what_github_says(self):
        listed = [
            {"number": 12, "url": "u12", "title": "Bump pillow", "headRefName": "secfix/pillow", "baseRefName": "main", "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"},
            {"number": 13, "url": "u13", "title": "Mine", "headRefName": "feature/x", "baseRefName": "main", "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND"},
        ]
        gh = fake(
            {
                ("pr", "list", "--repo", "owner/desk"): (0, json.dumps(listed), ""),
                ("pr", "list", "--repo", "Owner/Id"): (1, "", "HTTP 502"),
                ("pr", "checks", "12"): (0, json.dumps([{"name": "ci", "bucket": "pass"}]), ""),
                ("pr", "checks", "13"): (0, json.dumps([{"name": "ci", "bucket": "fail"}, {"name": "e2e", "bucket": "pending"}]), ""),
            }
        )
        out = outside.ready(self.CFG, gh)
        self.assertTrue(out["adopt"])
        self.assertEqual([(p["pr"], p["checks"], p["behind"], p["branch_allowed"]) for p in out["prs"]], [(12, "green", False, True), (13, "red: ci", True, False)])
        self.assertEqual(out["errors"], ["Owner/Id: could not list pull requests: HTTP 502"])
        self.assertIn(["pr", "list", "--repo", "owner/desk", "--label", "rollout-ready", "--state", "open", "--json", outside.LIST_FIELDS], gh.calls)

    def test_the_read_stops_at_its_budget_and_says_what_it_did_not_reach(self):
        ticks = iter([0, 0, 100, 100, 100])
        out = outside.ready(self.CFG, fake({}), budget_s=10, clock=lambda: next(ticks))
        self.assertEqual(out["errors"], ["not read within 10 s: Owner/Id"])

    def test_checks_are_said_in_one_word_and_the_names_that_matter(self):
        done = lambda rc, out, err="": subprocess.CompletedProcess([], rc, out, err)  # noqa: E731
        self.assertEqual(outside.checks_summary(done(1, "", "no required checks reported on the 'main' branch")), "none required")
        self.assertEqual(outside.checks_summary(done(8, json.dumps([{"name": "b", "bucket": "pending"}, {"name": "a", "bucket": "pass"}]))), "pending: b")
        self.assertEqual(outside.checks_summary(done(1, "", "HTTP 401")), "unread: HTTP 401")

    def test_a_program_staged_before_the_setting_adopts_nothing(self):
        self.assertFalse(outside.ready({"repos": {}}, fake({}))["adopt"])
        said = outside.ready({"outside_prs": "adopt", "repos": {}}, fake({}))
        self.assertEqual((said["adopt"], "names no steward logins" in said["adopt_off_because"]), (False, True))
        with self.assertRaises(Refusal) as caught:
            outside.decline({"program": "p", "repos": {"o/r": {}}}, "o/r", 1, "later", fake({}))
        self.assertIn("declines none", str(caught.exception))
        self.assertEqual(outside.setting({}), "off")
        self.assertIsNone(outside.setting_problem(None))
        self.assertIn("must be one of off, adopt", outside.setting_problem("yes"))

    def test_a_program_that_does_not_adopt_reads_nothing_at_a_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp) / "program.json", {"program": "p", "base": "main", "worker_claude_ref": "-", "gh_token_ref": "x", "cursor_model": "c", "repos": {"o/r": {}}})
            gh = fake({})
            said = outside.ready_or_said(tmp, gh=gh)
        self.assertEqual((said["adopt"], said["prs"], gh.calls), (False, [], []))

    def test_the_save_fact_never_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            said = outside.ready_or_said(tmp)
            self.assertIn("program.json could not be read", said["errors"][0])
            write_json(Path(tmp) / "program.json", {"program": "p", "base": "main", "worker_claude_ref": "-", "gh_token_ref": "x", "cursor_model": "c", "outside_prs": "adopt", "outside_pr_vetters": ["steward-bot"], "repos": {"owner/desk": {}}})
            said = outside.ready_or_said(tmp, no_token="GH_TOKEN is not set")
            self.assertEqual((said["adopt"], said["not_checked"]), (True, "not read, because the loop's own token could not be had: GH_TOKEN is not set"))
            # No pins in this root: the read is said, not raised.
            said = outside.ready_or_said(tmp)
            self.assertTrue(said["errors"][0].startswith("could not be read:"))

            def broken(args):
                raise RuntimeError("boom")

            self.assertEqual(outside.ready_or_said(tmp, gh=broken)["errors"], ["could not be read: boom"])


class DeclineTest(unittest.TestCase):
    CFG = {"program": "helpdesk-catalog", "outside_prs": "adopt", "outside_pr_vetters": ["steward-bot"], "repos": {"Owner/helpdesk": {}}, "streams": {}}

    READY = {("pr", "view"): (0, json.dumps({"labels": [{"name": "rollout-ready"}], "state": "OPEN"}), "")}

    def test_it_is_relabeled_and_the_reason_posted(self):
        gh = fake(dict(self.READY))
        out = outside.decline(self.CFG, "owner/helpdesk", 800, "  conflicts with HD-23,\n in review now  ", gh)
        self.assertEqual((out["result"], out["repo"], out["reason"]), ("declined", "Owner/helpdesk", "conflicts with HD-23, in review now"))
        self.assertEqual(gh.calls[1][:3], ["label", "create", "rollout-held"])
        self.assertEqual(gh.calls[2], ["pr", "edit", "800", "--repo", "Owner/helpdesk", "--remove-label", "rollout-ready", "--add-label", "rollout-held"])
        body = gh.calls[3][gh.calls[3].index("--body") + 1]
        self.assertIn("Passed over for now by the helpdesk-catalog loop: conflicts with HD-23, in review now", body)

    def test_a_label_that_exists_is_used(self):
        gh = fake({**self.READY, ("label", "create"): (1, "", 'label with name "rollout-held" already exists')})
        self.assertEqual(outside.decline(self.CFG, "Owner/helpdesk", 800, "later", gh)["result"], "declined")

    def test_what_is_refused(self):
        for repo, reason, said in (
            ("Owner/app-backend", "later", "is not one of this program's repositories"),
            ("Owner/helpdesk", "   ", "--reason is required"),
            ("Owner/helpdesk", "Generated with [Claude Code]", "the reason carries AI attribution"),
        ):
            with self.assertRaises(Refusal) as caught:
                outside.decline(self.CFG, repo, 800, reason, fake(dict(self.READY)))
            self.assertIn(said, str(caught.exception))
        with self.assertRaises(Refusal) as caught:
            outside.decline(self.CFG, "Owner/helpdesk", 800, "later", fake({**self.READY, ("pr", "edit"): (1, "", "HTTP 403")}))
        self.assertIn("could not be relabeled: HTTP 403", str(caught.exception))
        # An item's own pull request, or anything else not marked ready, is never relabeled here.
        gh = fake({("pr", "view"): (0, json.dumps({"labels": [], "state": "OPEN"}), "")})
        with self.assertRaises(Refusal) as caught:
            outside.decline(self.CFG, "Owner/helpdesk", 801, "later", gh)
        self.assertIn("does not carry `rollout-ready`", str(caught.exception))
        self.assertFalse(any(c[:2] in (["pr", "edit"], ["label", "create"]) for c in gh.calls))

    def test_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp) / "program.json", {"program": "p", "base": "main", "worker_claude_ref": "-", "gh_token_ref": "x", "cursor_model": "c", **self.CFG, "program": "p"})
            gh = fake(dict(self.READY))
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"GH_TOKEN": "t"}), contextlib.redirect_stdout(out):
                code = outside.main(["--root", tmp, "decline", "--repo", "Owner/helpdesk", "--pr", "800", "--reason", "later"], gh=gh)
            self.assertEqual((code, json.loads(out.getvalue())["result"]), (0, "declined"))
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"GH_TOKEN": "t"}), contextlib.redirect_stdout(out):
                code = outside.main(["--root", tmp, "list"], gh=gh)
            self.assertEqual((code, json.loads(out.getvalue())["prs"]), (0, []))
            with mock.patch.dict(os.environ, {"GH_TOKEN": ""}), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(outside.main(["--root", tmp, "list"], gh=gh), 2)


class LedgerTest(OutsideCase):
    def test_the_tables_show_outside_merges_only_when_there_are_any(self):
        self.assertNotIn("## Outside pull requests", ledger.items_text(self.root, "p", "now"))
        self.record(verify="verified")
        text = ledger.items_text(self.root, "p", "now")
        self.assertIn("## Outside pull requests", text)
        self.assertIn(f"| #12 | owner/repo | run-1 | {BRANCH} |", text)
        self.assertIn("| verified |", text)


if __name__ == "__main__":
    unittest.main()
