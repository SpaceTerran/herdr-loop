"""A delta confirm, through the commands a manager runs: `hl-flow review`
decides whether a rework gets one, `hl-flow reviewed` reads its verdict
against its transcript, and `hl-merge` asks again, from git and the run's
records, whether it may stand in for a full review. Git, the worktree and
the run directory are real; Herdr, `gh` and the served build are the
stand-ins the other tests use."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import time
from pathlib import Path
from unittest import mock

from hl import confirm, flow, items, ledger, merge, program, review, spawn, verdict
from hl.common import Refusal, read_json, write_json

from .support import RunCase, SpawnCase
from .test_flow import SERVE, FlowCase
from .test_merge import TEST_REF, git
from .test_review import PNG, bash, navigate, write_transcript

APP = "".join(f"line {i}\n" for i in range(1, 41))
FIX = {"criterion": 2, "what": "prints 2, the item asks for 1", "where": "out.txt", "evidence": []}
CURL = bash("curl -s http://127.0.0.1:8401/health")
TRAIN = "hl-train/canary"
IN_FULL = "; review the head in full (`hl-flow review --id A-1 --full`)"


class Cut(BaseException):
    """The command was killed at this point."""


def confirm_verdict(word="PASS", answers=("fixed",), delta="clean", **extra) -> dict:
    return {
        "verdict": word,
        "answers": [{"entry": k, "result": r, "evidence": [], "note": "ran it and saw it"} for k, r in enumerate(answers, 1)],
        "delta": {"result": delta, "note": "the fix changes one line and the tests of what it touches pass"},
        "fix_list": [],
        "findings": [],
        "how_run": "curl the served build",
        **extra,
    }


class ConfirmCase(FlowCase):
    """An item whose first review was a full review that failed it with a
    fix list, in a run whose reworks may get a delta confirm."""

    POLICY: dict = {}
    REPO: dict = {}
    FILES: tuple = (("feature.txt", "feature\n"), ("app.py", APP))
    BASE = "main"

    def setUp(self):
        super().setUp()
        self.policy(**{"review": "code", "confirm": "delta", **self.POLICY})
        if self.REPO:
            self.repo(**self.REPO)
        self.before()
        self.first = self.build(files=self.FILES)
        self.built()
        self.review()

    def before(self):
        """What is so before the item's branch is cut."""

    def set_gh(self, head, **pr):
        super().set_gh(head, **{"baseRefName": self.BASE, **pr})

    @property
    def wt(self) -> Path:
        """The item's worktree, as `hl-flow prepare` made it."""
        return Path(self.fields()["worktree"])

    def reinstall(self, change):
        """The contract changed the one way it can be: another one installed (here as a correction's amended contract is)."""
        text = (self.item_dir() / "contract.md").read_text()
        items._install_contract(self.run_dir, items.load_item(self.run_dir, "A-1"), change(text), {"source": "amended", "file": "correction-1.md"})

    def reviewer(self, n):
        return read_json(self.item_dir() / f"grade-{n}.worker.json")["session"]

    def entry(self, n):
        return next(e for e in confirm.reviews(self.run_dir, "A-1") if e["n"] == n)

    def review_fails(self, n=1, fix_list=(FIX,), results=("pass", "fail"), calls=(CURL,), word="FAIL", **extra):
        """A full review that failed: its commands, its verdict, its done file, and `hl-flow reviewed`."""
        write_transcript(self.home, self.reviewer(n), list(calls))
        data = {
            "verdict": word,
            "criteria": [{"n": i + 1, "result": r, "evidence": [], "note": "ran it"} for i, r in enumerate(results)],
            "fix_list": list(fix_list),
            "findings": [],
            "how_run": "curl the served build",
            **extra,
        }
        self.write("A-1", f"verdict-{n}.json", json.dumps(data))
        self.write("A-1", f"grade-{n}.done", "")
        return self.reviewed()

    def commit(self, *files, message="A-1: fix what the review found"):
        for name, text in files:
            (self.wt / name).parent.mkdir(parents=True, exist_ok=True)
            (self.wt / name).write_text(text)
            git(self.wt, "add", name)
        git(self.wt, "commit", "-m", message)
        return git(self.wt, "rev-parse", "HEAD")

    def fix(self, *files, how=None, build=2):
        """A builder's fix on the same branch: the next build started, committed, pushed, reported and accepted."""
        self.start()
        if how:
            how()
        else:
            self.commit(*(files or (("feature.txt", "feature, fixed\n"),)))
            git(self.wt, "push", "origin", self.BRANCH)
        head = git(self.wt, "rev-parse", "HEAD")
        self.write("A-1", f"build-{build}.md", SERVE)
        self.write("A-1", f"build-{build}.done", "")
        self.set_gh(head)
        code, out = self.built()
        self.assertEqual(code, 0, out)
        return head

    def patch(self, head, start=None, *more):
        """The fix as `git diff` prints it."""
        return subprocess.run(["git", "-C", str(self.wt), "diff", *more, start or self.first, head], capture_output=True, text=True, check=True).stdout

    def prints(self, head, start=None):
        """The reviewer runs the diff of the fix as its brief writes it, and is shown it."""
        return bash(f"git -C {self.wt} diff --no-renames {start or self.first} {head}", self.patch(head, start, "--no-renames"))

    def confirmed(self, head, calls=None, n=2, **said):
        """The confirm's reviewer at work: its commands, its verdict, its done file; then `hl-flow reviewed`."""
        write_transcript(self.home, self.reviewer(n), [self.prints(head), CURL] if calls is None else list(calls))
        self.write("A-1", f"verdict-{n}.json", json.dumps(confirm_verdict(**said)))
        self.write("A-1", f"grade-{n}.done", "")
        return self.reviewed()

    def on_record(self, head, session="confirm-by-hand", confirms=1, start=None, **said):
        """A delta confirm that passed, put on the record whether or not the
        script would have started it: what the merge gate has to judge for itself."""
        failed = read_json(self.item_dir() / f"verdict-{confirms}.json")
        plan = {"confirms": confirms, "from_sha": start or self.first, "verdict_sha": verdict.digest(failed)}
        entry = items.start_phase(self.run_dir, "A-1", "grade", session=session, head_sha=head, confirm=plan, how_run=spawn.served_as(self.run_dir, "A-1"))
        write_transcript(self.home, session, [self.prints(head, start), bash("python3 -m pytest tests/test_feature.py -q"), CURL])
        self.write("A-1", f"verdict-{entry['n']}.json", json.dumps(confirm_verdict(**said)))
        # Read, as `hl-flow reviewed` leaves a review that did nothing to its checkout.
        items.record_read(self.run_dir, "A-1", entry["n"], {"checkout": [], "dirty_before": []})
        return entry

    def unread(self, n):
        """Take the first read off a review's attempt: a review `hl-flow reviewed` never read."""
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        for entry in attempts["log"]:
            if entry["phase"] == "grade" and entry["n"] == n:
                entry.pop("read", None)
        write_json(path, attempts)

    def facts(self):
        return program.repo_facts(program.load(self.root), "owner/repo")

    def gate(self, **kw):
        kw.setdefault("repo_facts", self.facts())
        return merge.gate(self.run_dir, "A-1", self.clone, self.tools, **kw)

    def refusal(self, **kw):
        with self.assertRaises(Refusal) as caught:
            self.gate(**kw)
        return " | ".join(caught.exception.reasons)

    def gives_full(self, why, *extra):
        """`hl-flow review` starts a full review of the head, and says why it is not a delta confirm."""
        code, out = self.review(*extra)
        n = items.load_attempts(self.run_dir, "A-1")["grade"]
        self.assertEqual(code, 0, out)
        self.assertTrue(out["confirm"].startswith("full: "), out["confirm"])
        self.assertIn(why, out["confirm"])
        self.assertNotIn("\n", out["confirm"])
        self.assertNotIn("kind", self.entry(n))
        self.assertFalse(confirm.plan_path(self.item_dir(), n).exists())
        self.assertTrue((self.item_dir() / f"grade-{n}.brief.md").read_text().startswith(f"# Review brief: A-1, review {n}"))
        return out

    def gate_refuses(self, why, head, **kw):
        """A confirm of that head on the record, passed, does not merge, for the same reason."""
        self.on_record(head, **kw)
        said = self.refusal()
        self.assertIn("is a delta confirm that cannot stand in for a full review: ", said)
        self.assertIn(why, said)
        return said

    def push_to(self, branch, name, text, message="another item"):
        """A commit on a branch of the origin, by somebody else."""
        other = self.tmp / f"other-{len(list(self.tmp.glob('other-*')))}"
        git(self.tmp, "clone", "-q", str(self.origin), str(other))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(other, "config", key, value)
        git(other, "checkout", "-q", "-B", "work", f"origin/{branch}")
        (other / name).write_text(text)
        git(other, "add", name)
        git(other, "commit", "-q", "-m", message)
        git(other, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return other


class DeltaTest(ConfirmCase):
    """The path the setting is for: one full review, one fix, one confirm, a merge."""

    def test_a_small_fix_after_a_failed_review_is_given_a_delta_confirm(self):
        self.review_fails()
        head = self.fix()
        code, out = self.review()
        self.assertEqual((code, out["result"], out["watch"]), (0, "reviewing", "A-1:grade-2"))
        self.assertEqual(
            out["confirm"],
            f"delta: review 1 failed at {self.first[:12]} with 1 fix-list entry, and the fix since is 2 changed lines in 1 file (a confirm may cover 40)",
        )
        entry = self.entry(2)
        failed = read_json(self.item_dir() / "verdict-1.json")
        self.assertEqual(
            (entry["kind"], entry["confirms"], entry["from_sha"], entry["head_sha"], entry["verdict_sha"], entry["build"]),
            ("confirm", 1, self.first, head, verdict.digest(failed), 2),
        )
        # A fresh session: not the first reviewer's, not the builder's, and started, not resumed.
        builder = next(e["session"] for e in items.load_attempts(self.run_dir, "A-1")["log"] if e["phase"] == "build")
        self.assertNotIn(entry["session"], (self.entry(1)["session"], builder))
        self.assertIn("--session-id", self.agent_argv())
        self.assertNotIn("--resume", self.agent_argv())
        plan = read_json(confirm.plan_path(self.item_dir(), 2))
        self.assertEqual((plan["kind"], plan["confirms"], plan["from_sha"], plan["head_sha"]), ("delta", 1, self.first, head))
        self.assertEqual([f["path"] for f in plan["facts"]["files"]], ["feature.txt"])

    def test_a_confirm_that_passes_merges_the_fixed_head(self):
        self.review_fails()
        head = self.fix()
        self.review()
        code, out = self.confirmed(head)
        self.assertEqual((code, out["effective"], out["problems"]), (0, "PASS", []))
        self.assertEqual((out["confirms"], out["answers"], out["delta"]), (1, {"fixed": [1], "not_fixed": [], "not_checked": []}, "clean"))
        self.assertEqual((out["exercise"]["fix_files"], out["exercise"]["fix_files_shown"]), (1, 1))
        body = (self.item_dir() / "verdict-2.comment.md").read_text()
        self.assertIn("**A-1, review 2, a confirm of the fix made since review 1: PASS**", body)
        self.assertIn("| Entry | Result | Note | Evidence |", body)
        self.assertIn("| 1 | fixed | ran it and saw it |", body)
        self.assertIn("The fix beyond the fix list: clean. the fix changes one line", body)
        self.assertIn("Its own commands showed it the fix's diff of 1 of the 1 files the fix changes.", body)
        self.assertNotIn("Still to change:", body)

        opts = merge._parser().parse_args(["--run-dir", str(self.run_dir), "--repo-dir", str(self.clone), "--id", "A-1", "--lock-ref", TEST_REF, "--lock-wait", "0.3", "--lock-poll", "0.05"])
        code, merged = merge.merge(self.run_dir, "A-1", self.clone, self.tools, opts)
        self.assertEqual((code, merged["result"], merged["head_sha"], merged["graded_n"]), (0, "merged", head, 2))
        asked = [c for c in self.gh_calls() if c[:2] == ["pr", "merge"]][0]
        self.assertEqual(asked[asked.index("--match-head-commit") + 1], head)
        self.assertIn(f"Review 1 was a full review of {self.first[:12]} and failed it; review 2 confirmed the fix at {head[:12]}, contract ", asked[asked.index("--body") + 1])
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "merged")
        self.assertEqual(ledger.item_record(self.run_dir, "A-1")["reviews"], ["FAIL", "PASS (confirm)"])

    def test_a_run_that_gives_every_rework_a_full_review_is_as_it_was(self):
        self.policy(review="code")
        self.review_fails()
        self.fix()
        code, out = self.review()
        self.assertEqual(code, 0)
        self.assertNotIn("confirm", out)
        self.assertNotIn("kind", self.entry(2))
        self.assertFalse(confirm.plan_path(self.item_dir(), 2).exists())
        self.assertTrue((self.item_dir() / "grade-2.brief.md").read_text().startswith("# Review brief: A-1, review 2"))

    def test_the_first_review_of_an_item_is_a_full_one_and_says_so(self):
        self.assertNotIn("kind", self.entry(1))
        first = read_json(self.item_dir() / "grade-1.facts.json")
        self.assertEqual(first["head"], self.first)
        # Asked again for the same item before any review has run, it says why this one is full.
        # An item with no head on record, or no checkout to read a fix in, has nothing to decide from yet.
        nothing = {"kind": "full", "why": None}
        plan = lambda: spawn.review_plan(program.load(self.root), self.run_dir, "A-2")  # noqa: E731
        self.assertEqual(plan(), nothing)
        items.set_fields(self.run_dir, "A-2", ["head_sha=" + self.first])
        self.assertEqual(plan(), nothing)
        items.set_fields(self.run_dir, "A-2", [f"worktree={self.tmp / 'gone'}"])
        self.assertEqual(plan(), nothing)
        item = items.load_item(self.run_dir, "A-2")
        item["fields"] = {}
        items.save_item(self.run_dir, item)
        self.prepare(item_id="A-2")
        self.assertEqual(plan(), nothing)
        items.set_fields(self.run_dir, "A-2", ["head_sha=" + self.first])
        self.assertEqual(plan(), {"kind": "full", "why": "no review of this item has run yet"})

    def test_a_manager_may_always_ask_for_a_full_review(self):
        self.review_fails()
        self.fix()
        out = self.gives_full("a full review was asked for", "--full")
        self.assertEqual(out["confirm"], "full: a full review was asked for")

    def test_the_command_line_takes_the_flag_and_says_which_review_it_gave(self):
        self.review_fails()
        self.fix()
        argv = ["--root", str(self.root), "--run-dir", str(self.run_dir), "review", "--id", "A-1"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = flow.main(argv + ["--full"], herdr=self.herdr, host=self.host, tools=self.tools, envs=self.envs)
        self.assertEqual((code, json.loads(out.getvalue())["confirm"]), (0, "full: a full review was asked for"))
        self.assertNotIn("kind", self.entry(2))


class ReasonInWordsTest(ConfirmCase):
    FILES = (("feature.txt", "feature\n"), ("app.py", APP), ("one.txt", "a\n"))

    def test_one_of_each_reads_as_one_and_more_as_more(self):
        self.review_fails()
        self.fix(("one.txt", "a\nb\n"))
        code, out = self.review()
        self.assertEqual(out["confirm"], f"delta: review 1 failed at {self.first[:12]} with 1 fix-list entry, and the fix since is 1 changed line in 1 file (a confirm may cover 40)")
        self.assertEqual((confirm._count(0, "file", "files"), confirm._count(2, "file", "files")), ("0 files", "2 files"))


class PinnedGitTest(ConfirmCase):
    """What a review's brief says of the diff is read with the program's
    pinned git, like everything else the kit reads from a repository. A
    confirm later rests on the merge base those facts record. Found by the
    session that built the trial harness: they were read with whatever
    `git` was first on PATH."""

    def test_the_facts_of_a_review_started_by_hl_flow_are_read_with_the_git_it_hands_down(self):
        self.review_fails()
        self.fix()
        with mock.patch("hl.spawn.merge.Tools", side_effect=AssertionError("a git that is not the pinned one")):
            code, out = self.review("--full")
        self.assertEqual(code, 0, out)
        self.assertEqual(read_json(self.item_dir() / "grade-2.facts.json")["head"], self.fields()["head_sha"])

    def test_a_brief_rendered_by_hand_reads_them_with_the_programs_pinned_git(self):
        self.review_fails()
        self.fix()
        pinned = {"herdr": "/bin/herdr", "git": self.tools.git_bin, "gh": self.tools.gh_bin}
        self.check_pins.return_value = {"ok": True, "bins": pinned}
        argv = ["--root", str(self.root), "--run-dir", str(self.run_dir), "brief", "--id", "A-1", "--phase", "grade", "--set", "READY_SUMMARY=answered 200", "--full"]
        with mock.patch("hl.spawn.merge.Tools", wraps=merge.Tools) as made, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(spawn.main(argv), 0)
        self.assertTrue(made.call_args_list)
        self.assertTrue(all(call == mock.call(git=pinned["git"], gh=pinned["gh"]) for call in made.call_args_list), made.call_args_list)
        self.assertTrue(any(call.args[1] == ("git", "gh") for call in self.check_pins.call_args_list), self.check_pins.call_args_list)

    def test_a_reviewer_started_by_hand_has_its_checkout_read_with_the_programs_pinned_git(self):
        self.review_fails()
        self.fix()
        pinned = {"herdr": "/bin/herdr", "claude": "/bin/claude", "git": self.tools.git_bin, "gh": self.tools.gh_bin}
        self.check_pins.return_value = {"ok": True, "bins": pinned}
        opts = spawn._parser().parse_args(["--root", str(self.root), "--run-dir", str(self.run_dir), "start", "--id", "A-1", "--phase", "grade", "--engine", "claude", "--full"])
        opts.sets = {"READY_SUMMARY": "nothing is served"}
        with mock.patch("hl.spawn.merge.Tools", wraps=merge.Tools) as made:
            self.assertTrue(spawn.start(self.root, self.run_dir, "A-1", "grade", "claude", self.herdr, opts, envs=self.envs)["started"])
        self.assertGreaterEqual(len(made.call_args_list), 3)
        self.assertTrue(all(call == mock.call(git=pinned["git"], gh=pinned["gh"]) for call in made.call_args_list), made.call_args_list)
        self.assertEqual(self.entry(2)["baseline"], {"head": self.fields()["head_sha"], "dirty": {}})


class OnceTest(ConfirmCase):
    def test_a_confirm_that_fails_is_the_items_one_and_the_next_rework_gets_a_full_review(self):
        self.policy(review="code", confirm="delta", bounds={"build": 3})
        self.review_fails()
        head = self.fix()
        self.review()
        still = {"criterion": 2, "what": "still prints 2", "where": "out.txt", "evidence": []}
        code, out = self.confirmed(head, word="FAIL", answers=("not_fixed",), fix_list=[still])
        self.assertEqual((code, out["effective"], out["answers"]["not_fixed"], out["fix_list"]), (0, "FAIL", [1], 1))
        self.assertIn("Still to change:\n- criterion 2: still prints 2 (out.txt)", (self.item_dir() / "verdict-2.comment.md").read_text())
        # The builder is sent back with the confirm's own fix list.
        self.fix(("feature.txt", "feature, fixed again\n"), build=3)
        self.assertIn("verdict-2.json", (self.item_dir() / "build-3.brief.md").read_text())
        self.gives_full("review 2 was this item's one delta confirm")

    def test_the_review_a_confirm_follows_is_the_latest_one(self):
        self.policy(review="code", confirm="delta", bounds={"build": 3})
        self.review_fails()
        second = self.fix()
        self.review("--full")
        self.review_fails(n=2)
        third = self.fix(("feature.txt", "feature, fixed again\n"), build=3)
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith(f"delta: review 2 failed at {second[:12]} with 1 fix-list entry"), out["confirm"])
        entry = self.entry(3)
        self.assertEqual((entry["confirms"], entry["from_sha"], entry["head_sha"]), (2, second, third))
        self.assertIn(f"diff --no-renames {second} {third}`", (self.item_dir() / "grade-3.brief.md").read_text())

    def test_a_confirm_whose_worker_never_came_up_is_not_the_items_one(self):
        self.review_fails()
        self.fix()
        self.envs.absent.add("CLAUDE_CODE_OAUTH_TOKEN")
        with self.assertRaises(Refusal):
            self.review()
        self.assertEqual(len(confirm.reviews(self.run_dir, "A-1")), 1)
        # What it left, its brief and its plan, is set aside with the attempt.
        self.assertFalse(confirm.plan_path(self.item_dir(), 2).exists())
        self.envs.absent.clear()
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "))

    def test_a_second_confirm_on_the_record_does_not_merge(self):
        self.policy(review="code", confirm="delta", bounds={"build": 3})
        self.review_fails()
        second = self.fix()
        self.on_record(second, session="confirm-1", word="FAIL", answers=("not_fixed",), fix_list=[FIX])
        # A full review of the same head, which fails it again.
        self.review()
        self.review_fails(n=3)
        third = self.fix(("feature.txt", "feature, fixed again\n"), build=3)
        self.on_record(third, session="confirm-2", confirms=3, start=second)
        self.assertIn("A-1 has had more than one delta confirm; an item gets one", self.refusal())


class FirstReviewTest(ConfirmCase):
    """What the review before the fix has to have been: only a full review
    that did its work and failed with a usable fix list is built on."""

    def test_a_review_that_was_inconclusive_is_followed_by_a_full_review(self):
        self.review_fails(word="INCONCLUSIVE", results=("pass", "not_checked"), fix_list=())
        self.fix()
        self.gives_full("review 1 is INCONCLUSIVE, not a fail with a fix list")

    def test_a_confirm_on_the_record_of_a_review_that_did_not_fail_does_not_merge(self):
        # It named something to fix, and could not check a criterion: not a fail, and nothing a confirm can stand on.
        self.review_fails(word="INCONCLUSIVE", results=("pass", "not_checked"))
        said = self.gate_refuses("review 1 is INCONCLUSIVE, not a fail with a fix list", self.fix())
        self.assertIn("review 2 is a delta confirm that cannot stand in for a full review: review 1 is INCONCLUSIVE", said)

    def test_a_fail_that_names_nothing_to_fix_is_followed_by_a_full_review(self):
        # A verdict that says PASS over a failed criterion amounts to a fail, and has no fix list.
        self.review_fails(word="PASS", fix_list=())
        self.fix()
        self.gives_full("review 1 failed without a fix list")

    def test_a_review_with_no_usable_verdict_is_followed_by_a_full_review(self):
        self.review_fails(results=("pass",))
        self.fix()
        self.gives_full("review 1 left no usable verdict (criteria must be exactly [1, 2] in that order, got [1])")

    def test_a_review_that_left_a_criterion_unchecked_is_followed_by_a_full_review(self):
        both = [{**FIX, "criterion": 1}]
        self.review_fails(results=("fail", "not_checked"), fix_list=both)
        head = self.fix()
        self.gives_full("review 1 left criteria [2] unchecked, and a confirm does not go through the criteria")

    def test_a_failed_criterion_that_no_entry_names_is_followed_by_a_full_review(self):
        self.review_fails(fix_list=[{**FIX, "criterion": None}])
        head = self.fix()
        self.gives_full("criteria [2] failed in review 1 and no entry of its fix list names them")

    def test_a_review_that_exercised_nothing_is_not_built_on(self):
        self.review_fails(calls=[bash("git diff origin/main...HEAD | cat")])
        self.fix()
        self.gives_full("review 1 does not count: the reviewer ran nothing")

    def test_a_review_whose_checkout_was_edited_under_it_is_not_built_on(self):
        # The fail stands, and its build goes back like after any fail. What a confirm builds on is a review of the commit.
        (self.wt / "feature.txt").write_text("edited during the review\n")
        code, out = self.review_fails()
        edited = "tracked files were changed in the checkout during the review: feature.txt"
        self.assertEqual((code, out["effective"], out["problems"], out["checkout"]), (0, "FAIL", [], [edited]))
        git(self.wt, "checkout", "--", "feature.txt")
        self.fix()
        self.gives_full(f"review 1 does not count: {edited}")

    def test_a_review_that_was_never_read_is_not_built_on(self):
        self.review_fails()
        self.unread(1)
        self.fix()
        # The file `hl-flow reviewed` writes says it was read, and clean. What counts is on the attempt.
        self.assertEqual(read_json(self.item_dir() / "review-1.json")["problems"], [])
        self.gives_full("review 1 does not count: it was never read (`hl-flow reviewed` reads a review, and notes on its attempt what it did to its checkout)")

    def test_a_contract_that_changed_since_the_review_means_a_full_review(self):
        self.review_fails()
        self.reinstall(lambda text: text.replace("exits 0", "exits 0, at once"))
        self.fix()
        self.gives_full("the contract changed after review 1")

    def test_a_review_that_left_no_record_of_its_diff_is_not_built_on(self):
        self.review_fails()
        (self.item_dir() / "grade-1.facts.json").unlink()
        self.fix()
        self.gives_full("review 1 left no record of the diff it was given (grade-1.facts.json)")

    def test_a_record_of_the_diff_of_another_commit_is_not_that_reviews(self):
        self.review_fails()
        facts = self.item_dir() / "grade-1.facts.json"
        write_json(facts, {**read_json(facts), "head": "c" * 40})
        self.fix()
        self.gives_full("review 1 left no record of the diff it was given (grade-1.facts.json)")


FIX_ONE = {"criterion": 1, "what": "make test fails: the import is missing", "where": "app.py", "evidence": []}


class PinnedAtTheFirstReadTest(ConfirmCase):
    """What a confirm takes from the failed review is what `hl-flow reviewed`
    read of it, kept on the review's attempt. Found in the fifth review:
    the verdict was read from its file again when the confirm was decided,
    and in between runs the rework, whose builder is pointed at that file
    and can write it. A fix list with an entry taken out was the one the
    confirm answered, and the gate passed it."""

    def shortened(self):
        code, out = self.review_fails(fix_list=(FIX_ONE, FIX), results=("fail", "fail"))
        self.assertEqual((out["effective"], out["fix_list"]), ("FAIL", 2))
        path = self.item_dir() / "verdict-1.json"
        data = read_json(path)
        data["fix_list"] = [f for f in data["fix_list"] if f["criterion"] != 1]
        data["criteria"][0]["result"] = "pass"
        path.write_text(json.dumps(data))

    def test_a_fix_list_shortened_during_the_rework_gets_no_confirm(self):
        self.shortened()
        self.fix()
        self.gives_full("verdict-1.json is not the verdict `hl-flow reviewed` read of review 1: it was changed afterwards, or is gone")

    def test_a_confirm_on_the_record_of_a_shortened_fix_list_does_not_merge_and_the_full_review_it_names_does(self):
        self.shortened()
        head = self.fix()
        said = self.gate_refuses("verdict-1.json is not the verdict `hl-flow reviewed` read of review 1: it was changed afterwards, or is gone", head)
        self.assertTrue(said.endswith(IN_FULL), said)
        self.assertEqual(self.run_named(said), ["hl-flow review --id A-1 --full"])
        self.assertTrue((self.item_dir() / "grade-3.brief.md").read_text().startswith("# Review brief: A-1, review 3"))

    def test_a_confirm_whose_fix_list_changed_under_it_is_refused_with_the_full_review_that_follows(self):
        self.review_fails()
        head = self.fix()
        self.review()
        self.assertEqual(self.confirmed(head)[0], 0)
        path = self.item_dir() / "verdict-1.json"
        data = read_json(path)
        path.write_text(json.dumps({**data, "fix_list": data["fix_list"] + [{"criterion": None, "what": "one more", "where": "x", "evidence": []}]}))
        self.assertEqual(self.refusal(), "verdict-2.json: verdict-1.json is not the verdict this confirm was given to answer: the file changed after the confirm started" + IN_FULL)

    def test_a_verdict_that_is_gone_gets_no_confirm(self):
        self.review_fails()
        (self.item_dir() / "verdict-1.json").unlink()
        self.fix()
        self.gives_full("verdict-1.json is not the verdict `hl-flow reviewed` read of review 1")

    def test_the_first_read_keeps_what_the_verdict_and_the_transcript_said(self):
        self.review_fails()
        read = self.entry(1)["read"]
        self.assertEqual((read["verdict_sha"], read["effective"], read["exercise"], read["looked"]), (verdict.file_sha(self.item_dir(), 1), "FAIL", [], False))
        self.assertIsNone(verdict.file_sha(self.item_dir(), 9))

    def test_what_the_first_read_made_of_the_transcript_stands(self):
        # A record of what was served, or a transcript, that says more later than it did when the review was read.
        self.review_fails(calls=[bash("git diff origin/main...HEAD | cat")])
        self.assertIn("the reviewer ran nothing", self.entry(1)["read"]["exercise"][0])
        write_transcript(self.home, self.reviewer(1), [CURL])
        self.assertEqual(review.check(self.run_dir, "A-1", 1)["problems"], [])
        self.fix()
        self.gives_full("review 1 does not count: the reviewer ran nothing")

    def test_a_record_of_the_diff_that_was_rewritten_is_held_against_git(self):
        # `grade-1.facts.json` is a worker's to write too. What it says of the merge base has to be what git says now, so nothing rests on the file alone.
        self.review_fails()
        path = self.item_dir() / "grade-1.facts.json"
        base = read_json(path)["base"]
        write_json(path, {**read_json(path), "base": self.first})
        self.fix()
        self.gives_full(f"the pull request's merge base moved from {self.first[:12]} to {base[:12]} since review 1")


class HowItIsServedTest(ConfirmCase):
    """A rework that changes how the build is served or found ready gets a
    full review. Found in the fifth review: neither line is in git, so a
    one-line fix served with `PAYMENTS=mock SKIP_AUTH=1` got a confirm
    whose brief showed the new line only."""

    OTHER = "**Serve.** `PAYMENTS=mock SKIP_AUTH=1 PORT=8401 python3 app.py`\n**Ready.** URL `http://127.0.0.1:8401/health`\n"

    def reworked(self, report):
        self.start()
        head = self.commit(("feature.txt", "feature, fixed\n"))
        git(self.wt, "push", "origin", self.BRANCH)
        self.write("A-1", "build-2.md", report)
        self.write("A-1", "build-2.done", "")
        self.set_gh(head)
        self.assertEqual(self.built()[0], 0)
        return head

    def test_a_rework_served_another_way_is_reviewed_in_full(self):
        self.review_fails()
        self.assertEqual(self.entry(1)["how_run"], spawn.served_as(self.run_dir, "A-1"))
        self.reworked(self.OTHER)
        self.assertNotEqual(self.entry(1)["how_run"], spawn.served_as(self.run_dir, "A-1"))
        out = self.gives_full("the rework was not served and found ready the way review 1 was")
        self.assertEqual(out["confirm"], "full: the rework was not served and found ready the way review 1 was (the Serve and Ready lines of its report): how a build is served is outside the fix a confirm is given")

    def test_a_rework_whose_report_is_put_back_once_its_build_is_up_is_held_to_what_was_served(self):
        # Found in the second look: the value was taken from the report after the build was served, and the Serve line itself can write the report.
        self.review_fails()
        self.reworked(self.OTHER)
        self.herdr.on_serve = lambda: self.write("A-1", "build-2.md", SERVE)
        self.gives_full("the rework was not served and found ready the way review 1 was")
        self.assertEqual((self.item_dir() / "build-2.md").read_text(), SERVE)
        self.assertEqual(self.entry(2)["how_run"], spawn.how_run_of("PAYMENTS=mock SKIP_AUTH=1 PORT=8401 python3 app.py", "URL", "http://127.0.0.1:8401/health"))

    def test_a_reviewer_started_by_hand_has_nothing_on_record_of_how_its_build_was_served(self):
        self.review_fails()
        self.fix()
        opts = spawn._parser().parse_args(["--root", str(self.root), "--run-dir", str(self.run_dir), "start", "--id", "A-1", "--phase", "grade", "--engine", "claude"])
        opts.sets = {"READY_SUMMARY": "nothing is served"}
        out = spawn.start(self.root, self.run_dir, "A-1", "grade", "claude", self.herdr, opts, envs=self.envs, tools=self.tools)
        self.assertTrue(out["confirm"].startswith("full: the rework was not served and found ready the way review 1 was"), out["confirm"])
        self.assertNotIn("how_run", self.entry(2))

    def test_a_rework_found_ready_another_way_is_reviewed_in_full(self):
        self.review_fails()
        self.reworked(SERVE.replace("/health", "/"))
        self.gives_full("the rework was not served and found ready the way review 1 was")

    def test_a_confirm_on_the_record_of_a_rework_served_another_way_does_not_merge(self):
        self.review_fails()
        head = self.reworked(self.OTHER)
        self.gate_refuses("the rework was not served and found ready the way review 1 was", head)

    def test_a_rework_whose_report_says_the_same_in_other_words_around_it_gets_a_confirm(self):
        self.review_fails()
        self.reworked("What I changed: one line.\n\n" + SERVE + "\nNothing else.\n")
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "), out["confirm"])
        self.assertEqual(self.entry(2)["how_run"], self.entry(1)["how_run"])

    def test_two_reviews_with_nothing_on_record_of_how_they_were_served_are_not_taken_for_the_same(self):
        self.review_fails()
        head = self.fix()
        self.on_record(head)
        self.assertEqual(self.gate()["head_sha"], head)
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        for entry in attempts["log"]:
            entry.pop("how_run", None)
        write_json(path, attempts)
        self.assertIn("the rework was not served and found ready the way review 1 was", self.refusal())

    def test_a_report_that_does_not_say_how_it_is_served_has_no_value_for_it(self):
        self.write("A-1", "build-1.md", "no such lines\n")
        self.assertIsNone(spawn.served_as(self.run_dir, "A-1"))
        (self.item_dir() / "build-1.md").unlink()
        self.assertIsNone(spawn.served_as(self.run_dir, "A-1"))


INDENTED = "def pay(order):\n    if order.ok:\n        log(order)\n        charge(order)\n    return order\n"
DEDENTED = "def pay(order):\n    if order.ok:\n        log(order)\n    charge(order)\n    return order\n"


class WhiteSpaceTest(ConfirmCase):
    """What merges is the commit that was reviewed, or the same patch on a
    base that moved. Found in the fifth review, and older than the delta
    confirm: the patches were compared with white space left out, and in
    Python an indentation is the change."""

    FILES = (("feature.txt", "feature\n"), ("pay.py", INDENTED))

    def test_a_head_pushed_after_the_review_that_differs_only_in_white_space_does_not_merge(self):
        self.review_fails()
        reviewed = self.fix()
        self.review()
        self.assertEqual(self.confirmed(reviewed)[0], 0)
        self.assertEqual(self.gate()["head_sha"], reviewed)
        pushed = self.commit(("pay.py", DEDENTED), message="A-1: tidy")
        git(self.wt, "push", "origin", self.BRANCH)
        self.set_gh(pushed)
        self.assertEqual(self.refusal(), f"head is {pushed[:12]} but {reviewed[:12]} was graded, and the patch differs: grade the new head")

    def test_the_same_patch_on_a_base_that_moved_still_merges(self):
        self.review_fails()
        reviewed = self.fix()
        self.review()
        self.confirmed(reviewed)
        # The base takes somebody else's commit, and the branch takes the base in: the patch against the base is what it was.
        self.push_to(self.BASE, "other.txt", "another item\n")
        git(self.wt, "fetch", "-q", "origin")
        git(self.wt, "merge", "-q", "--no-edit", f"origin/{self.BASE}")
        git(self.wt, "push", "origin", self.BRANCH)
        merged = git(self.wt, "rev-parse", "HEAD")
        self.set_gh(merged)
        gated = self.gate()
        self.assertEqual((gated["head_sha"], gated["graded_sha"]), (merged, reviewed))
        self.assertEqual(gated["warnings"], [f"head moved from {reviewed[:12]} to {merged[:12]} with an identical patch"])


class OnlyTheFixTest(ConfirmCase):
    """The change a confirm reads is the fix and nothing else: commits on top
    of the commit the failed review read, none of them a merge, in a pull
    request whose merge base is where it was."""

    def test_a_head_that_has_not_moved_is_reviewed_in_full(self):
        self.review_fails()
        self.gives_full(f"the head has not moved since review 1: it is still {self.first[:12]}")

    def test_a_confirm_on_the_record_of_the_head_the_review_read_does_not_merge(self):
        self.review_fails()
        self.on_record(self.first)
        self.assertEqual(self.refusal(), f"review 2: the fix between {self.first[:12]} and {self.first[:12]} adds and removes no line: there is nothing a reviewer could have been shown")

    def rewritten(self):
        git(self.wt, "commit", "--amend", "-m", "A-1: add the feature, and fix it")
        (self.wt / "feature.txt").write_text("feature, fixed\n")
        git(self.wt, "add", "feature.txt")
        git(self.wt, "commit", "--amend", "--no-edit")
        git(self.wt, "push", "--force", "origin", self.BRANCH)

    def test_a_branch_that_was_rewritten_is_reviewed_in_full(self):
        self.review_fails()
        head = self.fix(how=self.rewritten)
        self.gives_full(f"{self.first[:12]}, the commit review 1 was of, is not an ancestor of {head[:12]}: the branch was rewritten")

    def test_a_confirm_on_the_record_of_a_rewritten_branch_does_not_merge(self):
        self.review_fails()
        head = self.fix(how=self.rewritten)
        self.gate_refuses("is not an ancestor of", head)

    def with_the_base_merged_in(self):
        self.push_to(self.BASE, "other.txt", "another stream's work\n")
        git(self.wt, "fetch", "-q", "origin", f"+refs/heads/{self.BASE}:refs/remotes/origin/{self.BASE}")
        git(self.wt, "merge", "-q", "--no-edit", f"origin/{self.BASE}")
        self.merged = git(self.wt, "rev-parse", "HEAD")
        self.commit(("feature.txt", "feature, fixed\n"))
        git(self.wt, "push", "origin", self.BRANCH)

    def test_a_fix_that_holds_a_merge_from_the_base_is_reviewed_in_full(self):
        self.review_fails()
        self.fix(how=self.with_the_base_merged_in)
        out = self.gives_full(f"the fix holds a merge ({self.merged[:12]}): it carries work that is not the fix")
        # The merge moved the pull request's merge base too, and both are said.
        self.assertIn("the pull request's merge base moved from ", out["confirm"])

    def test_a_confirm_on_the_record_of_a_fix_that_holds_a_merge_does_not_merge(self):
        self.review_fails()
        head = self.fix(how=self.with_the_base_merged_in)
        said = self.gate_refuses("the fix holds a merge", head)
        self.assertIn("the pull request's merge base moved from ", said)

    def base_takes_the_reviewed_commit(self):
        """Somebody put the commit the review read onto the base by hand: the pull request is now only the fix."""
        git(self.clone, "push", "-q", "origin", f"{self.first}:refs/heads/{self.BASE}")

    def test_a_pull_request_whose_merge_base_moved_is_reviewed_in_full(self):
        self.review_fails()
        base = read_json(self.item_dir() / "grade-1.facts.json")["base"]
        self.base_takes_the_reviewed_commit()
        self.fix()
        self.gives_full(f"the pull request's merge base moved from {base[:12]} to {self.first[:12]} since review 1")

    def test_a_confirm_does_not_merge_once_the_pull_requests_merge_base_has_moved(self):
        self.review_fails()
        head = self.fix()
        self.on_record(head)
        self.assertEqual(self.gate()["head_sha"], head)
        self.base_takes_the_reviewed_commit()
        self.assertIn("cannot stand in for a full review: the pull request's merge base moved from ", self.refusal())

    def test_a_confirm_the_gate_refuses_is_followed_by_a_full_review_of_the_same_head_which_merges(self):
        # The way on that `troubles.md` names for a confirm that can no longer stand: `hl-flow review --id <ID> --full`.
        self.review_fails()
        head = self.fix()
        self.review()
        self.assertEqual(self.confirmed(head)[0], 0)
        self.base_takes_the_reviewed_commit()
        self.assertIn("review 2 is a delta confirm that cannot stand in for a full review: ", self.refusal())
        out = self.gives_full("a full review was asked for", "--full")
        write_transcript(self.home, self.reviewer(3), [CURL])
        self.verdict(n=3, fix_list=[])
        code, out = self.reviewed()
        self.assertEqual((code, out["review"], out["effective"], out["problems"]), (0, 3, "PASS", []))
        self.assertIn(f"Graded PASS at {head[:12]} (grade 3)", self.gate()["body"])

    def test_a_confirm_does_not_merge_where_the_base_shares_no_history_with_the_head(self):
        self.review_fails()
        head = self.fix()
        self.on_record(head)
        other = self.push_to("main", "x.txt", "x\n")
        git(other, "checkout", "-q", "--orphan", "fresh")
        git(other, "commit", "-q", "-m", "a base with another history")
        git(other, "push", "-q", "--force", "origin", "HEAD:refs/heads/main")
        self.assertIn("the pull request's merge base moved from ", self.refusal())
        self.assertIn(" to none since review 1", self.refusal())

    def test_a_fix_that_leaves_every_file_as_it_was_is_reviewed_in_full(self):
        self.review_fails()

        def nothing():
            git(self.wt, "commit", "--allow-empty", "-m", "A-1: nothing to change")
            git(self.wt, "push", "origin", self.BRANCH)

        head = self.fix(how=nothing)
        self.gives_full(f"nothing differs between {self.first[:12]} and {head[:12]}")

    def test_a_confirm_on_the_record_of_a_fix_that_changed_nothing_does_not_merge(self):
        self.review_fails()

        def nothing():
            git(self.wt, "commit", "--allow-empty", "-m", "A-1: nothing to change")
            git(self.wt, "push", "origin", self.BRANCH)

        self.on_record(self.fix(how=nothing))
        self.assertIn("adds and removes no line: there is nothing a reviewer could have been shown", self.refusal())


def more(lines: int) -> str:
    return APP + "".join(f"added {i}\n" for i in range(lines))


class BoundTest(ConfirmCase):
    """Past the run's bound a fix is new work. The first review read 41
    changed lines in two files."""

    def bound(self, **bound):
        self.policy(review="code", confirm="delta", confirm_bound=bound)

    def test_the_kits_own_bound_lets_forty_lines_through_and_not_one_more(self):
        self.review_fails()
        self.fix(("app.py", more(40)))
        code, out = self.review()
        self.assertIn("the fix since is 40 changed lines in 1 file (a confirm may cover 40)", out["confirm"])

    def test_a_fix_one_line_over_the_bound_is_reviewed_in_full(self):
        self.review_fails()
        self.fix(("app.py", more(41)))
        self.gives_full("the fix changes 41 lines, more than the 40 a confirm may cover (0.25 of the 41 in the diff review 1 was given, or 40)")

    def test_a_confirm_on_the_record_of_a_fix_over_the_bound_does_not_merge(self):
        self.review_fails()
        self.gate_refuses("the fix changes 41 lines, more than the 40 a confirm may cover", self.fix(("app.py", more(41))))

    def test_the_share_of_what_the_review_read_is_the_bound_where_it_is_the_larger(self):
        self.bound(share=0.5, lines=3)
        self.review_fails()
        self.fix(("app.py", more(20)))
        code, out = self.review()
        self.assertIn("is 20 changed lines in 1 file (a confirm may cover 20)", out["confirm"])

    def test_one_line_past_the_share_is_reviewed_in_full(self):
        self.bound(share=0.5, lines=3)
        self.review_fails()
        self.fix(("app.py", more(21)))
        self.gives_full("the fix changes 21 lines, more than the 20 a confirm may cover (0.5 of the 41 in the diff review 1 was given, or 3)")

    def test_removed_lines_count_as_changed_lines(self):
        self.bound(share=0.01, lines=3)
        self.review_fails()
        self.fix(("app.py", "".join(APP.splitlines(keepends=True)[:-4])))
        self.gives_full("the fix changes 4 lines, more than the 3 a confirm may cover")

    def test_a_fix_may_touch_two_files_the_review_did_not_read(self):
        self.review_fails()
        self.fix(("feature.txt", "feature, fixed\n"), ("tests/test_feature.py", "def test_it():\n    assert True\n"), ("caller.py", "import app\n"))
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "), out["confirm"])
        self.assertIn("in 3 files", out["confirm"])

    def spread(self):
        return self.fix(("tests/test_feature.py", "def test_it():\n    assert True\n"), ("caller.py", "import app\n"), ("other.py", "x = 1\n"))

    def test_a_fix_that_spread_to_a_third_new_file_is_reviewed_in_full(self):
        self.review_fails()
        self.spread()
        self.gives_full("the fix touches 3 files that were not in the diff review 1 was given (caller.py, other.py, tests/test_feature.py), more than the 2 a confirm may cover")

    def test_a_confirm_on_the_record_of_a_fix_that_spread_does_not_merge(self):
        self.review_fails()
        self.gate_refuses("the fix touches 3 files that were not in the diff review 1 was given", self.spread())

    def test_the_runs_own_bound_on_files_is_the_one_held(self):
        self.bound(files=0)
        self.review_fails()
        self.fix(("caller.py", "import app\n"))
        self.gives_full("the fix touches 1 files that were not in the diff review 1 was given (caller.py), more than the 0 a confirm may cover")


class BoundOfAChangeThatRemovesTest(ConfirmCase):
    """The first review read 41 lines added and two changed: 43 added and 2 removed."""

    FILES = (("feature.txt", "feature\n"), ("app.py", APP), ("README.md", "changed\n"), ("requirements.txt", "pytest==9\n"))

    def test_the_lines_a_review_read_are_those_added_and_those_removed(self):
        self.policy(review="code", confirm="delta", confirm_bound={"share": 0.5, "lines": 3})
        self.review_fails()
        self.fix(("app.py", more(22)))
        code, out = self.review()
        self.assertIn("is 22 changed lines in 1 file (a confirm may cover 22)", out["confirm"])


NOTES = "".join(f"-- note {i}\n" for i in range(60))


ROWS = "".join(f"row {i}\n" for i in range(1, 11))


class RenamedTest(ConfirmCase):
    """A fix in which git finds a rename or a copy gets a full review, like
    one that changes a file's mode. Found in the third review: the
    reviewer's `git diff` followed the rename and printed one `index` line
    and the line that differs, while the check reads the fix with no rename
    followed, as one file deleted and one added. An honest confirm could
    not pass, and the item's one confirm was spent finding that out."""

    FILES = (("feature.txt", "feature\n"), ("app.py", APP), ("lib.py", ROWS))

    def renamed(self):
        (self.wt / "app.py").write_text(APP + "line 41\n")
        git(self.wt, "mv", "lib.py", "core.py")
        (self.wt / "core.py").write_text(ROWS.replace("row 3\n", "row three\n"))
        (self.wt / "feature.txt").write_text("feature, fixed\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-m", "A-1: fix what the review found")
        git(self.wt, "push", "origin", self.BRANCH)

    def copied(self):
        (self.wt / "copy.py").write_text(ROWS)
        (self.wt / "lib.py").write_text(ROWS.replace("row 3\n", "row three\n"))
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-m", "A-1: fix what the review found")
        git(self.wt, "push", "origin", self.BRANCH)

    def test_a_fix_that_renames_a_file_is_reviewed_in_full_before_a_confirm_is_spent(self):
        self.review_fails()
        head = self.fix(how=self.renamed)
        # What a reviewer's own `git diff` prints for it: one part, under the new name, with one `index` line.
        followed = self.patch(head)
        self.assertIn("rename from lib.py\nrename to core.py\nindex ", followed)
        self.assertEqual(followed.count("\ndiff --git "), 2)
        # Small enough to be confirmed, were it not for the rename: that is the one reason.
        out = self.gives_full("the fix renames or copies a file")
        self.assertEqual(out["confirm"], "full: the fix renames or copies a file as git reads it (`lib.py` to `core.py`), and a confirm is held to the lines of a diff that follows neither")
        self.assertEqual(len(confirm.confirms_had(self.run_dir, "A-1")), 0)

    def test_a_fix_that_copies_a_file_is_reviewed_in_full(self):
        self.review_fails()
        self.fix(how=self.copied)
        out = self.gives_full("the fix renames or copies a file")
        self.assertEqual(out["confirm"], "full: the fix renames or copies a file as git reads it (`lib.py` to `copy.py`), and a confirm is held to the lines of a diff that follows neither")

    def test_a_confirm_on_the_record_of_a_fix_that_renames_a_file_does_not_merge(self):
        self.review_fails()
        head = self.fix(how=self.renamed)
        self.gate_refuses("the fix renames or copies a file as git reads it (`lib.py` to `core.py`)", head)

    def test_a_fix_that_adds_and_deletes_files_git_takes_for_neither_is_confirmed(self):
        self.review_fails()
        head = self.fix(("extra.py", "nothing like the others\n"), ("feature.txt", "feature, fixed\n"))
        self.assertEqual(review.moved_files(self.tools, self.clone, self.first, head), [])
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "), out["confirm"])
        self.assertEqual(self.confirmed(head)[1]["problems"], [])


class NoCountOfLinesTest(ConfirmCase):
    """The bound is in lines, so a fix that holds a change no count of lines
    shows is not within it, whatever else it changes. Found in review: 500
    removed SQL comments and a rewritten binary file counted as nothing,
    and a change of mode or an empty file rode along beside a line of text."""

    FILES = (
        ("feature.txt", "feature\n"),
        ("app.py", APP),
        ("m.sql", NOTES + "create table t (id int);\n"),
        ("b.bin", "\x00\x01binary\n" * 200),
        ("data.txt", "plain text\n"),
        ("blank.txt", ""),
        ("odd\tname.sql", NOTES),
        ("odd\nname.py", "x = 1\n"),
    )
    EDIT = ("feature.txt", "feature, fixed\n")

    def test_removed_lines_that_begin_like_a_patchs_own_header_are_counted(self):
        self.review_fails()
        self.fix(("m.sql", "create table t (id int);\n"))
        self.gives_full("the fix changes 60 lines, more than the 41 a confirm may cover (0.25 of the 164 in the diff review 1 was given, or 40)")

    def test_lines_are_counted_in_a_file_whose_name_holds_a_tab(self):
        self.review_fails()
        self.fix(("odd\tname.sql", "-- one note\n"))
        self.gives_full("the fix changes 61 lines, more than the 41 a confirm may cover")

    def test_a_small_fix_to_a_file_whose_name_holds_a_newline_is_confirmed_like_any_other(self):
        self.review_fails()
        head = self.fix(("odd\nname.py", "x = 2\n"))
        code, out = self.review()
        self.assertIn("the fix since is 2 changed lines in 1 file", out["confirm"])
        code, out = self.confirmed(head)
        self.assertEqual((code, out["problems"], out["exercise"]["fix_files_shown"]), (0, [], 1))
        self.assertEqual(self.gate()["head_sha"], head)

    def unmeasured(self, *files, how=None, why):
        """A fix that also edits a line of text: `hl-flow review` gives it a full review, and a confirm of it on the record does not merge."""
        self.review_fails()
        head = self.fix(self.EDIT, *files) if how is None else self.fix(how=how)
        self.gives_full(f"the fix holds a change no count of lines shows ({why})")
        return head

    def test_a_fix_that_changes_a_binary_file_is_reviewed_in_full(self):
        self.unmeasured(("b.bin", "\x00\x02other\n" * 5000), why="`b.bin`, a binary file")

    def test_a_fix_that_turns_a_text_file_into_a_binary_one_is_reviewed_in_full(self):
        self.unmeasured(("data.txt", "\x00\x01\x02 no longer text\n" * 50), why="`data.txt`, a binary file")

    def test_a_fix_that_adds_a_file_with_no_line_in_it_is_reviewed_in_full(self):
        self.unmeasured(("marker", ""), why="`marker`, added with no line in it")

    def with_a_mode(self):
        self.commit(self.EDIT)
        os.chmod(self.wt / "app.py", 0o755)
        git(self.wt, "add", "app.py")
        git(self.wt, "commit", "-m", "A-1: make it runnable")
        git(self.wt, "push", "origin", self.BRANCH)

    def test_a_change_of_mode_beside_a_line_of_text_is_reviewed_in_full(self):
        self.unmeasured(how=self.with_a_mode, why="`app.py`, its mode, 100644 to 100755")

    def test_a_fix_that_deletes_a_file_with_no_line_in_it_is_reviewed_in_full(self):
        def without_it():
            git(self.wt, "rm", "-q", "blank.txt")
            self.commit(self.EDIT)
            git(self.wt, "push", "origin", self.BRANCH)

        self.unmeasured(how=without_it, why="`blank.txt`, deleted with no line in it")

    def test_a_confirm_on_the_record_of_a_fix_with_a_change_of_mode_does_not_merge(self):
        self.review_fails()
        self.gate_refuses("the fix holds a change no count of lines shows (`app.py`, its mode, 100644 to 100755)", self.fix(how=self.with_a_mode))

    def test_a_fix_that_is_only_a_change_of_mode_is_reviewed_in_full(self):
        self.review_fails()

        def mode_only():
            os.chmod(self.wt / "feature.txt", 0o755)
            git(self.wt, "add", "feature.txt")
            git(self.wt, "commit", "-m", "A-1: make it runnable")
            git(self.wt, "push", "origin", self.BRANCH)

        self.fix(how=mode_only)
        self.gives_full("the fix holds a change no count of lines shows (`feature.txt`, its mode, 100644 to 100755)")


class ProtectedTest(ConfirmCase):
    FILES = (("feature.txt", "feature\n"), ("app.py", APP), ("pinned.lock", "pinned\n"))

    def restored(self):
        # The path became one no item may change after the first build was accepted, and the fix puts it back.
        self.repo(protected=["pinned.lock"])

        def put_back():
            git(self.wt, "rm", "-q", "pinned.lock")
            self.commit(("feature.txt", "feature, fixed\n"))
            git(self.wt, "push", "origin", self.BRANCH)

        return self.fix(how=put_back)

    def test_a_fix_that_touches_a_path_no_item_may_change_is_reviewed_in_full(self):
        self.review_fails()
        self.restored()
        self.gives_full("the fix touches a path no item may change (pinned.lock)")

    def test_a_confirm_on_the_record_of_such_a_fix_does_not_merge(self):
        self.review_fails()
        self.gate_refuses("the fix touches a path no item may change (pinned.lock)", self.restored())

    def test_the_merge_reads_the_protected_paths_from_the_programs_own_settings(self):
        self.review_fails()
        head = self.restored()
        self.on_record(head)
        opts = merge._parser().parse_args(["--run-dir", str(self.run_dir), "--repo-dir", str(self.clone), "--id", "A-1", "--lock-ref", TEST_REF])
        with self.assertRaises(Refusal) as caught:
            merge.merge(self.run_dir, "A-1", self.clone, self.tools, opts)
        self.assertIn("the fix touches a path no item may change (pinned.lock)", str(caught.exception))
        # A run whose program cannot be found has no such list to read, and that is not taken for an empty one.
        opts = merge._parser().parse_args(["--run-dir", str(self.run_dir), "--repo-dir", str(self.clone), "--id", "A-1", "--lock-ref", TEST_REF, "--root", str(self.tmp / "nowhere")])
        with self.assertRaises(Refusal) as caught:
            merge.merge(self.run_dir, "A-1", self.clone, self.tools, opts)
        self.assertIn("the program of run-1 was not found, so the paths a person sees and those no item may change are unknown: pass --root", str(caught.exception))


ALERTS = "http://127.0.0.1:8401/app/alerts"


class LookTest(ConfirmCase):
    """A confirm is not a way around a look: what the failed review looked
    at is looked at again, and a fix that reaches a page nobody looked at
    gets a full review."""

    REPO = {"seen": ["web/**"]}
    FILES = (("feature.txt", "feature\n"), ("app.py", APP), ("web/page.html", "<p>old</p>\n"))

    def picture(self, n, name, mode="dark", url=ALERTS, result="match"):
        path = self.item_dir() / f"evidence-{n}" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        return {"mode": mode, "url": url, "screenshot": f"evidence-{n}/{name}", "result": result, "note": "against the reference"}

    def looked_first(self, **route):
        """The first review looked at one page in two modes, and failed the item on a criterion."""
        if route:
            items.route_item(self.run_dir, "A-1", "independent", route["look"], "a page")
        looks = [self.picture(1, "alerts-dark.png"), self.picture(1, "alerts-light.png", mode="light")]
        return self.review_fails(calls=[CURL, navigate(ALERTS)], looks=looks, look_needed=True)

    def test_the_confirm_is_told_the_pages_the_failed_review_looked_at(self):
        self.looked_first(look=True)
        self.fix()
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "), out["confirm"])
        brief = (self.item_dir() / "grade-2.brief.md").read_text()
        self.assertIn("**A person looks at what this item changes.** Review 1 looked at the pages below.", brief)
        self.assertIn(f"  - `{ALERTS}`, mode `dark`\n  - `{ALERTS}`, mode `light`", brief)
        self.assertNotIn("recorded no look at a page", brief)

    def test_a_confirm_that_looked_at_each_of_them_again_passes_and_merges(self):
        self.looked_first(look=True)
        head = self.fix()
        self.review()
        # The same pages, whatever host name the reviewer typed, and one more besides.
        looks = [self.picture(2, "a.png", url="http://localhost:8401/app/alerts/"), self.picture(2, "b.png", mode="Light"), self.picture(2, "c.png", url="http://127.0.0.1:8401/app")]
        code, out = self.confirmed(head, calls=[self.prints(head), CURL, navigate(ALERTS)], looks=looks)
        self.assertEqual((code, out["effective"], out["problems"], out["exercise"]["looked"]), (0, "PASS", [], True))
        self.assertEqual(self.gate()["head_sha"], head)

    def test_a_confirm_that_did_not_look_does_not_count(self):
        self.looked_first(look=True)
        head = self.fix()
        self.review()
        code, out = self.confirmed(head)
        self.assertEqual(code, 2)
        self.assertTrue(any("this item changes something a person looks at, and the review did not look: its transcript shows no page of the served build" in p for p in out["problems"]), out["problems"])
        self.assertIn("review 2: this item changes something a person looks at, and the review did not look", self.refusal())

    def test_a_confirm_that_skipped_a_page_or_a_mode_does_not_count(self):
        self.looked_first(look=True)
        head = self.fix()
        self.review()
        looks = [self.picture(2, "a.png"), self.picture(2, "b.png", url="http://127.0.0.1:8401/app/other", mode="light")]
        code, out = self.confirmed(head, calls=[self.prints(head), CURL, navigate(ALERTS)], looks=looks)
        self.assertEqual(code, 2)
        self.assertIn("review 2: review 1 looked at pages this confirm did not look at again: /app/alerts (light)", out["problems"])
        self.assertIn("review 1 looked at pages this confirm did not look at again: /app/alerts (light)", self.refusal())

    def test_two_pages_that_differ_only_after_the_question_mark_are_two_pages(self):
        tab = "http://127.0.0.1:8401/app?tab=alerts"
        self.review_fails(calls=[CURL, navigate(tab)], looks=[self.picture(1, "tab.png", url=tab)], look_needed=True)
        head = self.fix()
        self.review()
        code, out = self.confirmed(head, calls=[self.prints(head), CURL, navigate(tab)], looks=[self.picture(2, "tab.png", url="http://127.0.0.1:8401/app?tab=rules")])
        self.assertIn("review 2: review 1 looked at pages this confirm did not look at again: /app?tab=alerts (dark)", out["problems"])

    def test_pages_the_failed_review_looked_at_are_looked_at_again_whatever_the_route_says(self):
        # Routed as nothing a person sees; the first reviewer found otherwise and looked.
        self.looked_first()
        head = self.fix()
        self.review()
        self.assertIn("Review 1 looked at the pages below.", (self.item_dir() / "grade-2.brief.md").read_text())
        code, out = self.confirmed(head)
        self.assertEqual((code, out["exercise"]["look_required"]), (2, True))
        self.assertTrue(any("the review did not look" in p for p in out["problems"]))

    def test_a_confirm_after_a_review_that_looked_at_nothing_is_asked_to_check_that(self):
        self.review_fails()
        head = self.fix()
        self.review()
        brief = (self.item_dir() / "grade-2.brief.md").read_text()
        self.assertIn("**Review 1 recorded no look at a page.** Check the fix against that.", brief)
        self.assertNotIn("Review 1 looked at the pages below", brief)
        code, out = self.confirmed(head)
        self.assertEqual((code, out["exercise"]["look_required"], out["exercise"]["looked"]), (0, False, False))

    def test_a_fix_to_a_page_the_failed_review_read_and_looked_at_gets_a_confirm(self):
        self.looked_first(look=True)
        self.fix(("web/page.html", "<p>new</p>\n"))
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "), out["confirm"])
        self.assertIn("Changed paths this repository's spec says a person sees: `web/page.html`.", (self.item_dir() / "grade-2.brief.md").read_text())

    def test_a_fix_that_reaches_a_page_the_failed_review_never_read_is_reviewed_in_full(self):
        self.looked_first(look=True)
        self.fix(("web/other.html", "<p>new page</p>\n"))
        self.gives_full("the fix touches a path a person sees that was not in the diff review 1 was given (web/other.html)")

    def test_a_confirm_on_the_record_of_such_a_fix_does_not_merge(self):
        self.looked_first(look=True)
        head = self.fix(("web/other.html", "<p>new page</p>\n"))
        looks = [self.picture(2, "a.png"), self.picture(2, "b.png", mode="light")]
        self.on_record(head, looks=looks)
        write_transcript(self.home, "confirm-by-hand", [self.prints(head), CURL, navigate(ALERTS)])
        write_json(self.item_dir() / "serve-2.json", {"served": True, "port": 8401})
        self.assertIn("cannot stand in for a full review: the fix touches a path a person sees that was not in the diff review 1 was given (web/other.html)", self.refusal())

    def test_what_the_first_read_made_of_a_reviews_looks_stands(self):
        # A picture counts when the review took it. One that was older than the review when the review was read does not begin to count later.
        look = self.picture(1, "alerts-dark.png")
        picture = self.item_dir() / look["screenshot"]
        os.utime(picture, (time.time() - 3600,) * 2)
        self.review_fails(calls=[CURL, navigate(ALERTS)], looks=[look], look_needed=True)
        self.assertIs(self.entry(1)["read"]["looked"], False)
        os.utime(picture)
        self.assertIs(review.check(self.run_dir, "A-1", 1)["looked"], True)
        self.fix(("web/page.html", "<p>new</p>\n"))
        self.gives_full("the fix touches what a person sees (web/page.html) and review 1 did not look")

    def test_a_fix_to_a_page_after_a_review_that_did_not_look_is_reviewed_in_full(self):
        self.review_fails()
        self.fix(("web/page.html", "<p>new</p>\n"))
        self.gives_full("the fix touches what a person sees (web/page.html) and review 1 did not look")

    def test_a_confirm_on_the_record_of_a_fix_to_a_page_nobody_looked_at_does_not_merge(self):
        self.review_fails()
        self.gate_refuses("the fix touches what a person sees (web/page.html) and review 1 did not look", self.fix(("web/page.html", "<p>new</p>\n")))

    def test_an_item_routed_with_a_look_after_a_review_that_did_not_look_is_reviewed_in_full(self):
        self.review_fails()
        items.route_item(self.run_dir, "A-1", "independent", True, "it shows on a page after all")
        self.fix()
        self.gives_full("review 1 does not count: this item changes something a person looks at, and the review did not look")


class ShownTheFixTest(ConfirmCase):
    """The instrument that reads a reviewer's transcript, as it is for a
    confirm: for every file the fix changes, one result of the reviewer's
    own commands must hold that file's part of the fix as this fix's own
    diff. The fix here takes one line out of `feature.txt` and puts one in,
    and adds three to `app.py`."""

    FIX = (("feature.txt", "feature, fixed\n"), ("app.py", APP + "one\ntwo  \nthree\n"))

    def started(self, *fix):
        self.review_fails()
        self.head = self.fix(*(fix or self.FIX))
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "), out["confirm"])
        return self.head

    def git(self, *args):
        """What a git command prints in the checkout, as the reviewer is shown it."""
        return subprocess.run(["git", "-C", str(self.wt), *args], capture_output=True, text=True).stdout

    def shown(self, *calls, sub_agents=()):
        """What `hl/review.py` makes of a confirm whose reviewer ran these commands and was shown what each printed."""
        write_transcript(self.home, self.reviewer(2), [*calls, CURL], sub_agents=sub_agents)
        seen = review.check(self.run_dir, "A-1", 2, self.tools, self.clone)
        return seen["fix_files_shown"], [p for p in seen["problems"] if "nothing shows" in p]

    def test_a_confirm_that_was_shown_the_whole_pull_request_and_not_the_fix_does_not_count_and_does_not_merge(self):
        head = self.started()
        # Every line the fix adds is in it. It is not this fix's diff: neither file's part is the change from one commit to the other.
        whole = bash(f"git -C {self.wt} diff origin/main...HEAD", self.patch(head, "origin/main"))
        code, out = self.confirmed(head, calls=[whole, CURL])
        self.assertEqual((code, out["effective"], out["exercise"]["fix_files"], out["exercise"]["fix_files_shown"]), (2, "PASS", 2, 0))
        said = (
            f"the reviewer's own commands showed it the fix's diff of 0 of the 2 files the fix changes between {self.first[:12]} and {head[:12]} "
            "(not of `app.py`: no output holds, in one file's part of a diff, that file's `index` line of this diff and every line the fix adds or removes): "
            "nothing shows the whole fix was printed to the reviewer"
        )
        self.assertIn(f"review 2: {said}", out["problems"])
        self.assertIn("**This review does not count for a merge:**", (self.item_dir() / "verdict-2.comment.md").read_text())
        self.assertIn(f"review 2: {said}", self.refusal())

    def test_a_fix_made_of_lines_another_diff_already_holds_is_not_shown_by_that_diff(self):
        # Found in the second review: a fix that repeats a line the build added was "shown" by the first review's own diff.
        head = self.started(("app.py", APP + "line 5\n"))
        base = git(self.clone, "rev-parse", "origin/main")
        first = self.git("diff", base, self.first)
        self.assertIn("\n+line 5\n", first)
        self.assertEqual(self.shown(bash(f"git -C {self.wt} diff {base} {self.first}", first))[0], 0)
        self.assertEqual(self.shown(self.prints(head)), (1, []))

    def test_a_command_that_names_the_right_commits_and_prints_no_patch_shows_nothing(self):
        head = self.started()
        for flags in (["--stat"], ["--raw"], ["--dirstat"], ["--check"], ["-s"], ["--name-only"], ["--numstat"], ["--summary"]):
            shown, problems = self.shown(bash(f"git -C {self.wt} diff {' '.join(flags)} {self.first} {head}", self.git("diff", *flags, self.first, head)))
            self.assertEqual((shown, len(problems)), (0, 1), flags)
        # Sent to a file nobody opened, or to nowhere, the same diff was not shown either.
        for command in (f"git -C {self.wt} diff {self.first} {head} > /dev/null", f"git -C {self.wt} diff --output=/dev/null {self.first} {head}", f"git -C {self.wt} diff {self.first} {head} > fix.patch"):
            self.assertEqual(self.shown(bash(command, ""))[0], 0, command)

    def test_a_diff_of_other_commits_is_not_the_fix(self):
        head = self.started()
        base = git(self.clone, "rev-parse", "origin/main")
        for start, end in ((base, self.first), (self.first, self.first), (f"{self.first}~1", self.first), (base, head)):
            shown, problems = self.shown(bash(f"git -C {self.wt} diff {start} {end}", self.git("diff", start, end)))
            self.assertEqual((shown, len(problems)), (0, 1), (start, end))

    def test_whatever_command_printed_the_fixs_own_diff_the_reviewer_was_shown_it(self):
        head = self.started()
        patch = self.patch(head)
        for name, calls in (
            ("the diff itself", [self.prints(head)]),
            ("with more around each change", [bash("git diff -U10", self.patch(head, None, "-U10"))]),
            ("with whole blob names", [bash("git diff --full-index", self.patch(head, None, "--full-index"))]),
            ("the commit, shown", [bash(f"git show {head}", self.git("show", head))]),
            ("the log with its patch", [bash("git log -p", self.git("log", "-p", f"{self.first}..{head}"))]),
            ("a file at a time", [bash("git diff -- feature.txt", self.git("diff", self.first, head, "--", "feature.txt")), bash("git diff -- app.py", self.git("diff", self.first, head, "--", "app.py"))]),
            ("written to a file and read with the file tool", [bash(f"git diff {self.first} {head} > /ev/fix.patch", ""), ("Read", {"file_path": "/ev/fix.patch"}, "".join(f"{i}\t{line}\n" for i, line in enumerate(patch.split("\n"), 1)))]),
            ("numbered by cat", [bash("git diff | cat -n", "".join(f"{i:6d}\t{line}\n" for i, line in enumerate(patch.split("\n"), 1)))]),
            ("as a list of text blocks", [("mcp__files__read", {"path": "/ev/fix.patch"}, [{"type": "text", "text": patch}])]),
            ("with the end of the output trimmed", [bash("git diff", patch.rstrip())]),
            ("with the end of each line trimmed", [bash("git diff", "\n".join(line.rstrip() for line in patch.split("\n")))]),
        ):
            self.assertEqual(self.shown(*calls), (2, []), name)

    def test_a_files_part_of_the_fix_counts_only_whole_and_with_the_line_that_says_whose_diff_it_is(self):
        head = self.started()
        patch = self.patch(head)
        lines = patch.split("\n")
        # Cut into pages in the middle of the second file's part: the first file was shown whole, the second in neither page.
        cut = next(i for i, line in enumerate(lines) if line == "+two  ")
        shown, problems = self.shown(bash("git diff | head", "\n".join(lines[:cut])), bash("git diff | tail", "\n".join(lines[cut:])))
        self.assertEqual((shown, len(problems)), (1, 1))
        # Every line of the fix, with the lines that say which blobs it lies between taken out or printed apart.
        bare = "\n".join(line for line in lines if not line.startswith("index "))
        self.assertEqual(self.shown(bash("git diff | grep -v ^index", bare))[0], 0)
        self.assertEqual(self.shown(bash("git diff | grep ^index", "\n".join(line for line in lines if line.startswith("index "))), bash("git diff | grep -v ^index", bare))[0], 0)
        # The right line for one end of the change only is another diff's.
        blobs = {f["path"]: f for f in review.fix_files(self.tools, self.clone, self.first, head)}
        other, own = "0" * 40, self.git("diff", self.first, head, "--", "app.py")
        self.assertEqual(self.shown(bash("git diff -- app.py", own))[0], 1)
        for before, after in ((blobs["app.py"]["before"], other), (other, blobs["app.py"]["after"])):
            forged = "\n".join(f"index {before[:7]}..{after[:7]} 100644" if line.startswith("index ") else line for line in own.split("\n"))
            self.assertEqual(self.shown(bash("git diff -- app.py", forged))[0], 0)

    def test_only_what_came_back_from_a_tool_was_shown(self):
        head = self.started()
        patch = self.patch(head)
        transcript = self.home / ".claude" / "projects" / "-x" / "s.jsonl"
        transcript.parent.mkdir(parents=True)
        blocks = [{"type": "text", "text": patch}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": patch}}, {"type": "document", "content": patch}]
        transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": patch}}) + "\n" + json.dumps({"message": {"content": blocks}}) + "\n")
        self.assertEqual(review.shown_results(transcript), [])
        results = [{"type": "tool_result", "tool_use_id": "t1", "content": "+one\n-two  \n"}, "a string", {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "7\t+three"}]}]
        transcript.write_text(json.dumps({"message": {"content": results}}) + "\n")
        self.assertEqual(review.shown_results(transcript), [[{"+one", "-two", ""}], [{"+three"}]])
        self.assertEqual(review.shown_results(self.home / "no-such-file.jsonl"), [])

    def test_a_result_is_read_in_the_parts_a_diff_has_one_for_each_file(self):
        transcript = self.home / ".claude" / "projects" / "-x" / "s.jsonl"
        transcript.parent.mkdir(parents=True)
        text = "before any diff\ndiff --git a/x b/x\nindex 1111111..2222222 100644\n+one  \n    5\tdiff --git a/y b/y\n    6\t+two\n +diff --git is text here\n"
        transcript.write_text(json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}}) + "\n")
        self.assertEqual(
            review.shown_results(transcript),
            [[{"before any diff"}, {"diff --git a/x b/x", "index 1111111..2222222 100644", "+one"}, {"diff --git a/y b/y", "+two", " +diff --git is text here", ""}]],
        )

    def test_the_line_that_says_whose_diff_it_is_counts_only_for_the_lines_of_its_own_part(self):
        # Found in the third review: `git diff H1 H2 | head -3; git diff <base> <first>` printed the `index` line
        # of the fix and, further down the same output, the same lines as another diff has them.
        head = self.started(("app.py", APP + "line 5\n"))
        base = git(self.clone, "rev-parse", "origin/main")
        fix, first = self.patch(head), self.git("diff", base, self.first)
        self.assertIn("\n+line 5\n", first)
        top = "\n".join(fix.split("\n")[:3]) + "\n"
        self.assertRegex(top, r"\Adiff --git a/app.py b/app.py\nindex [0-9a-f]+\.\.[0-9a-f]+ 100644\n--- a/app.py\n\Z")
        command = f"git -C {self.wt} diff {self.first} {head} | head -3; git -C {self.wt} diff {base} {self.first}"
        self.assertEqual(self.shown(bash(command, top + first)), (0, [mock.ANY]))
        # The other way round, and with the `index` line printed alone, it is no different.
        self.assertEqual(self.shown(bash("…", first + top))[0], 0)
        self.assertEqual(self.shown(bash("…", "\n".join(line for line in fix.split("\n") if line.startswith("index ")) + "\n" + first))[0], 0)
        # The fix's own diff after another diff in the same output is the fix's own diff.
        self.assertEqual(self.shown(bash(f"git diff {base} {self.first}; git diff {self.first} {head}", first + fix)), (1, []))
        code, out = self.confirmed(head, calls=[bash(command, top + first), CURL])
        self.assertEqual((code, out["exercise"]["fix_files_shown"]), (2, 0))
        self.assertIn("nothing shows the whole fix was printed to the reviewer", self.refusal())

    def test_a_part_with_two_lines_that_say_whose_diff_it_is_shows_nothing(self):
        # Two diffs run together with the lines that part them taken out: not one file's part of one diff.
        head = self.started(("app.py", APP + "line 5\n"))
        base = git(self.clone, "rev-parse", "origin/main")
        fix, first = self.patch(head), self.git("diff", base, self.first, "--", "app.py")
        named = next(line for line in fix.split("\n") if line.startswith("index "))
        joined = named + "\n" + "\n".join(line for line in first.split("\n") if not line.startswith("diff --git "))
        self.assertIn("\n+line 5\n", joined)
        self.assertEqual(self.shown(bash("git diff … | grep ^index; git diff … | grep -v '^diff --git'", joined))[0], 0)
        # A file's part of the fix's own diff, printed without the line it begins with, is still that file's part.
        self.assertEqual(self.shown(bash(f"git diff {self.first} {head} | tail -n +2", "\n".join(fix.split("\n")[1:])))[0], 1)

    def test_the_part_of_an_output_that_was_cut_off_was_not_shown(self):
        head = self.started()
        patch = self.patch(head)
        shown, problems = self.shown(bash("git diff | head -12", "\n".join(patch.split("\n")[:12])))
        self.assertEqual((shown, len(problems)), (1, 1))

    def test_what_a_sub_agent_was_shown_the_reviewer_was_not(self):
        head = self.started()
        shown, problems = self.shown(sub_agents=[[self.prints(head)]])
        self.assertEqual((shown, len(problems)), (0, 1))
        # It still counts as the reviewer's own exercise of the change, as for any review.
        self.assertEqual(review.check(self.run_dir, "A-1", 2, self.tools, self.clone)["sub_agents"], 1)

    def test_a_confirm_that_was_shown_the_fix_and_ran_nothing_does_not_count(self):
        head = self.started()
        code, out = self.confirmed(head, calls=[self.prints(head)])
        self.assertEqual(code, 2)
        self.assertTrue(any("the reviewer ran nothing" in p for p in out["problems"]))
        self.assertFalse(any("nothing shows the whole fix was printed" in p for p in out["problems"]))

    def test_a_fix_git_cannot_read_was_not_shown_to_anyone(self):
        head = self.started()
        write_transcript(self.home, self.reviewer(2), [self.prints(head), CURL])
        self.assertEqual(review.check(self.run_dir, "A-1", 2)["problems"], [f"the fix between {self.first[:12]} and {head[:12]} could not be taken from git, so nothing shows it was printed to the reviewer"])
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        attempts["log"][-1]["from_sha"] = "f" * 40
        write_json(path, attempts)
        self.assertIn("could not be taken from git", review.check(self.run_dir, "A-1", 2, self.tools, self.clone)["problems"][0])

    def test_a_full_review_is_not_asked_whether_it_was_shown_a_fix(self):
        self.review_fails()
        seen = review.check(self.run_dir, "A-1", 1, self.tools, self.clone)
        self.assertEqual(seen["problems"], [])
        self.assertNotIn("fix_files", seen)


class ReadOfAConfirmTest(ConfirmCase):
    """A confirm is read by `hl-flow reviewed` like any review, and the merge
    rests on what that read found."""

    def test_a_confirm_that_passed_over_an_edited_checkout_is_given_back_and_was_still_the_items_one(self):
        self.review_fails()
        head = self.fix()
        self.review()
        (self.wt / "app.py").write_text("edited under the confirm's reviewer\n")
        code, out = self.confirmed(head)
        edited = "tracked files were changed in the checkout during the review: app.py"
        self.assertEqual((code, out["effective"], out["problems"]), (2, "PASS", [edited]))
        git(self.wt, "checkout", "--", "app.py")
        (self.item_dir() / "review-2.json").unlink()
        self.assertEqual(self.reviewed()[1]["problems"], [edited])
        said = self.refusal()
        self.assertIn(f"review 2 does not count for a merge: {edited}; it never was a review of the head, so give the attempt back", said)
        # Found in the third review: given back, the confirm was no longer the item's one, and the head got a confirm
        # again, round after round. Giving it back gives the build its review again, and that review is a full one.
        self.run_named(said)
        self.assertEqual((self.attempts()["grade"], [e["n"] for e in confirm.confirms_had(self.run_dir, "A-1")]), (2, [2]))
        self.assertNotIn("kind", self.entry(2))
        brief = (self.item_dir() / "grade-2.brief.md").read_text()
        self.assertTrue(brief.startswith("# Review brief: A-1, review 2"), brief[:60])
        self.assertFalse(confirm.plan_path(self.item_dir(), 2).exists())
        # Nothing of the confirm is what the full review is read against, and the full review is what merges.
        write_transcript(self.home, self.reviewer(2), [CURL])
        self.verdict(n=2, fix_list=[])
        code, out = self.reviewed()
        self.assertEqual((code, out["effective"], out["problems"]), (0, "PASS", []))
        self.assertNotIn("confirms", out)
        self.assertIn(f"Graded PASS at {head[:12]} (grade 2)", self.gate()["body"])

    def test_after_a_confirm_was_given_back_the_script_says_why_the_next_review_is_a_full_one(self):
        self.review_fails()
        head = self.fix()
        self.review()
        (self.wt / "app.py").write_text("edited under the confirm's reviewer\n")
        self.confirmed(head)
        git(self.wt, "checkout", "--", "app.py")
        items.void_attempt(self.run_dir, "A-1", "grade", 2, "the reviewer edited app.py")
        self.gives_full("review 2, given back after it passed over a changed checkout, was this item's one delta confirm")

    def test_a_second_confirm_on_the_record_after_one_was_given_back_does_not_merge(self):
        self.review_fails()
        head = self.fix()
        self.review()
        (self.wt / "app.py").write_text("edited under the confirm's reviewer\n")
        self.confirmed(head)
        git(self.wt, "checkout", "--", "app.py")
        items.void_attempt(self.run_dir, "A-1", "grade", 2, "the reviewer edited app.py")
        self.on_record(head)
        self.assertEqual(self.refusal(), "A-1 has had more than one delta confirm; an item gets one; review the head in full (`hl-flow review --id A-1 --full`)")

    def test_a_confirm_that_did_not_pass_stands_whatever_happened_to_its_checkout(self):
        self.review_fails()
        head = self.fix()
        self.review()
        (self.wt / "app.py").write_text("edited under the confirm's reviewer\n")
        still = {"criterion": 2, "what": "still prints 2", "where": "out.txt", "evidence": []}
        code, out = self.confirmed(head, word="FAIL", answers=("not_fixed",), fix_list=[still])
        self.assertEqual((code, out["effective"], out["problems"]), (0, "FAIL", []))
        with self.assertRaises(Refusal) as caught:
            items.void_attempt(self.run_dir, "A-1", "grade", 2, "the reviewer edited app.py")
        self.assertIn("only a pass over such a checkout is given back", str(caught.exception))
        self.assertEqual(self.refusal(), "the latest grade is FAIL, not PASS")

    def test_a_confirm_that_was_never_read_does_not_merge(self):
        self.review_fails()
        head = self.fix()
        self.on_record(head)
        self.unread(2)
        self.assertEqual(self.refusal(), "review 2 has not been read: `hl-flow reviewed --id A-1` reads it against its transcript and its checkout, and a merge rests on what that finds")


class FreshBaseTest(ConfirmCase):
    """What a confirm is decided from is the base as the remote has it, as
    at the merge gate. Found in review: decided from the clone's own copy, a
    confirm was started, refused at the gate, and the item's one confirm spent."""

    def test_a_base_that_moved_since_the_clone_last_looked_is_seen_before_a_confirm_is_started(self):
        self.review_fails()
        self.fix()
        before = git(self.clone, "rev-parse", "origin/main")
        # Somebody else puts the commit the review read onto the base. The stream's clone has not looked since.
        elsewhere = self.tmp / "elsewhere"
        git(self.tmp, "clone", "-q", str(self.origin), str(elsewhere))
        git(elsewhere, "push", "-q", "origin", f"{self.first}:refs/heads/main")
        self.assertEqual(git(self.clone, "rev-parse", "origin/main"), before)
        self.gives_full(f"the pull request's merge base moved from {before[:12]} to {self.first[:12]} since review 1")
        self.assertEqual(git(self.clone, "rev-parse", "origin/main"), self.first)

    def test_a_base_that_cannot_be_fetched_means_a_full_review(self):
        self.review_fails()
        self.fix()
        git(self.clone, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        out = self.gives_full("`main` could not be fetched, so where the pull request's base is now is not known (")
        self.assertIn("gone.git", out["confirm"])

    def test_hl_flow_fetches_with_its_own_git_and_no_other(self):
        self.review_fails()
        self.fix()
        with mock.patch("hl.spawn.merge.Tools", side_effect=AssertionError("a git that is not the pinned one")):
            code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "), out["confirm"])

    def test_without_the_loops_own_token_nothing_is_fetched_and_the_review_is_a_full_one(self):
        # `hl-spawn brief` by hand, in a shell that holds no token: git would fall back to the keyring's.
        self.review_fails()
        self.fix()
        before = git(self.clone, "rev-parse", "origin/main")
        self.push_to("main", "other.txt", "another stream's work\n")
        argv = ["--root", str(self.root), "--run-dir", str(self.run_dir), "brief", "--id", "A-1", "--phase", "grade", "--set", "READY_SUMMARY=answered 200"]
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GH_TOKEN": ""}), contextlib.redirect_stdout(out):
            self.assertEqual(spawn.main(argv), 0)
        self.assertIn("full: `main` could not be fetched, so where the pull request's base is now is not known (GH_TOKEN is not set", json.loads(out.getvalue())["confirm"])
        self.assertEqual(git(self.clone, "rev-parse", "origin/main"), before)
        self.assertTrue((self.item_dir() / "grade-2.brief.md").read_text().startswith("# Review brief: A-1, review 2"))


class ChainOnRecordTest(ConfirmCase):
    """What the merge gate asks of the record itself, whoever wrote it."""

    def passed(self):
        self.review_fails()
        head = self.fix()
        self.on_record(head)
        self.assertEqual(self.gate()["head_sha"], head)
        return head

    def rewrite(self, n, **changes):
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        for entry in attempts["log"]:
            if entry["phase"] == "grade" and entry["n"] == n:
                entry.update(changes)
        write_json(path, attempts)

    def test_a_confirm_in_a_run_that_gives_reworks_a_full_review_does_not_merge(self):
        self.passed()
        self.policy(review="code")
        self.assertIn("review 2 is recorded as a delta confirm, and this run confirms a rework with a full review", self.refusal())

    def test_a_confirm_in_a_run_whose_evaluators_grade_blind_does_not_merge(self):
        # A run file edited by hand: no script makes one. Read as a blind grade, a verdict with criteria would pass
        # with no transcript read and no fix list answered.
        self.passed()
        self.policy(review="blind", confirm="delta")
        self.assertIn("verdict-2.json: criteria must be a non-empty list", self.refusal())
        blind = {"verdict": "PASS", "criteria": [{"n": n, "result": "pass", "evidence": [], "note": "seen"} for n in (1, 2)], "findings": [], "how_run": "make test"}
        self.write("A-1", "verdict-2.json", json.dumps(blind))
        self.assertEqual(self.refusal(), "review 2 is recorded as a delta confirm, and this run confirms a rework with a full review; review the head in full (`hl-flow review --id A-1 --full`)")

    def test_a_confirm_without_the_programs_settings_does_not_merge(self):
        self.passed()
        self.assertIn("the program of run-1 was not found", self.refusal(repo_facts=None))

    def test_a_confirm_that_names_another_review_than_the_one_before_it_does_not_merge(self):
        # Two full reviews failed, the second of the fixed head; a confirm that reaches back past it to the first.
        self.review_fails()
        head = self.fix()
        self.review("--full")
        self.review_fails(n=2)
        self.on_record(head, confirms=1)
        self.assertIn(f"review 3 is a delta confirm, and the review before it is not the failed full review at {self.first[:12]} that it names", self.refusal())

    def test_a_confirm_that_names_an_earlier_review_of_the_same_commit_does_not_merge(self):
        self.review_fails()
        self.review()
        self.review_fails(n=2)
        head = self.fix()
        self.on_record(head, confirms=1)
        self.assertIn(f"review 3 is a delta confirm, and the review before it is not the failed full review at {self.first[:12]} that it names", self.refusal())

    def test_a_confirm_that_names_another_commit_than_the_one_that_review_read_does_not_merge(self):
        # It says it read the change from the base, and its reviewer was shown exactly that.
        head = self.passed()
        base = git(self.clone, "rev-parse", "origin/main")
        self.rewrite(2, from_sha=base)
        write_transcript(self.home, "confirm-by-hand", [self.prints(head, base), CURL])
        self.assertEqual(self.refusal(), f"review 2 is a delta confirm, and the review before it is not the failed full review at {base[:12]} that it names; review the head in full (`hl-flow review --id A-1 --full`)")

    def test_a_confirm_of_a_confirm_does_not_merge(self):
        self.passed()
        self.rewrite(1, kind="confirm")
        self.assertIn("the review before it is not the failed full review", self.refusal())

    def test_a_confirm_with_no_review_before_it_does_not_merge(self):
        self.passed()
        self.rewrite(1, voided={"at": "2026-10-12T00:00:00Z", "note": "by hand"})
        self.assertIn("the review before it is not the failed full review", self.refusal())

    def test_a_confirm_by_the_first_reviewers_own_session_does_not_merge(self):
        self.passed()
        self.rewrite(1, session="confirm-by-hand")
        self.assertIn("review 1 and its confirm must be two sessions, neither of them a builder's", self.refusal())

    def test_a_first_review_by_a_builders_session_is_not_built_on(self):
        self.passed()
        builder = next(e["session"] for e in items.load_attempts(self.run_dir, "A-1")["log"] if e["phase"] == "build")
        write_transcript(self.home, builder, [CURL])
        self.rewrite(1, session=builder)
        self.assertIn("review 1 and its confirm must be two sessions, neither of them a builder's", self.refusal())
        self.rewrite(1, session=None)
        self.assertIn("review 1 and its confirm must be two sessions", self.refusal())

    def test_a_first_review_by_the_session_that_drafted_the_contract_is_not_built_on(self):
        self.passed()
        reviewer = self.entry(1)["session"]
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        next(e for e in attempts["log"] if e["phase"] == "agree")["drafter_session"] = reviewer
        write_json(path, attempts)
        self.assertIn("review 1 and its confirm must be two sessions, neither of them a builder's", self.refusal())

    def test_a_confirm_by_a_builders_session_does_not_merge(self):
        self.passed()
        builder = next(e["session"] for e in items.load_attempts(self.run_dir, "A-1")["log"] if e["phase"] == "build")
        self.rewrite(2, session=builder)
        self.assertIn("the evaluator's session is missing or is also a builder's session", self.refusal())

    def test_a_failed_reviews_verdict_that_was_changed_after_the_confirm_started_does_not_merge(self):
        self.passed()
        path = self.item_dir() / "verdict-1.json"
        kept = read_json(path)
        for changed in ({"fix_list": [{**FIX, "what": "something easier"}]}, {"verdict": "INCONCLUSIVE"}, {"looks": []}):
            write_json(path, {**kept, **changed})
            self.assertIn("verdict-2.json: verdict-1.json is not the verdict this confirm was given to answer", self.refusal(), changed)

    def test_a_first_review_whose_transcript_is_gone_is_not_built_on(self):
        self.passed()
        for path in (self.home / ".claude" / "projects").glob(f"*/{self.entry(1)['session']}.jsonl"):
            path.unlink()
        self.assertIn("review 1 does not count: the reviewer's transcript", self.refusal())

    def test_a_contract_changed_between_the_review_and_its_confirm_does_not_merge(self):
        self.review_fails()
        head = self.fix()
        self.reinstall(lambda text: text.replace("exits 0", "exits 0, at once"))
        self.gate_refuses("the contract changed after review 1", head)

    def test_a_head_that_moved_after_the_confirm_is_held_to_the_same_patch_as_any_review(self):
        head = self.passed()
        late = self.commit(("feature.txt", "feature, changed after the confirm\n"), message="late change")
        git(self.wt, "push", "origin", self.BRANCH)
        self.set_gh(late)
        self.assertIn(f"head is {late[:12]} but {head[:12]} was graded, and the patch differs: grade the new head", self.refusal())


class AlreadyMergedTest(ConfirmCase):
    def merge_opts(self):
        return merge._parser().parse_args(["--run-dir", str(self.run_dir), "--repo-dir", str(self.clone), "--id", "A-1", "--lock-ref", TEST_REF, "--lock-wait", "0.3", "--lock-poll", "0.05"])

    def test_a_merge_of_a_confirmed_head_that_died_before_its_record_is_recorded(self):
        self.review_fails()
        head = self.fix()
        self.on_record(head)
        # The base holds the whole pull request now, as a merge made whole leaves it: its merge base cannot be where it was.
        git(self.clone, "push", "-q", "origin", f"{head}:refs/heads/main")
        self.assertIn("the pull request's merge base moved", self.refusal())
        self.set_gh(head, state="MERGED", mergeCommit={"oid": "f" * 40})
        code, out = merge.merge(self.run_dir, "A-1", self.clone, self.tools, self.merge_opts())
        self.assertEqual((code, out["result"], out["recovered"], out["merge_sha"]), (0, "merged", True, "f" * 40))

    def test_a_merged_pull_request_whose_confirm_cannot_stand_is_not_recorded_as_merged(self):
        self.review_fails()
        head = self.fix()
        self.on_record(head)
        for path in (self.home / ".claude" / "projects").glob(f"*/{self.entry(1)['session']}.jsonl"):
            path.unlink()
        self.set_gh(head, state="MERGED", mergeCommit={"oid": "f" * 40})
        with self.assertRaises(Refusal) as caught:
            merge.merge(self.run_dir, "A-1", self.clone, self.tools, self.merge_opts())
        self.assertIn("cannot stand in for a full review: review 1 does not count: the reviewer's transcript", str(caught.exception))
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")


class BriefTest(ConfirmCase):
    def brief(self, *extra):
        # Rendered before the build is served, a review's brief is told what the served build answered.
        argv = ["--root", str(self.root), "--run-dir", str(self.run_dir), "brief", "--id", "A-1", "--phase", "grade", "--set", "READY_SUMMARY=answered 200", *extra]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(spawn.main(argv), 0)
        self.said = json.loads(out.getvalue())
        return (self.item_dir() / "grade-2.brief.md").read_text()

    def test_the_confirms_brief_is_the_fix_list_and_the_fix(self):
        second = {"criterion": None, "what": "the test was rewritten\nto skip {{PORT}}", "where": "tests/test_x.py:3", "evidence": []}
        self.review_fails(fix_list=[FIX, second])
        head = self.fix()
        text = self.brief()
        self.assertNotIn("{{", text)
        self.assertTrue(text.startswith("# Confirm brief: A-1, review 2\n"))
        self.assertIn(f"Review 1 was a full review of the change at commit `{self.first}`", text)
        # Each entry on a line of its own, numbered as it is to be answered, and a reviewer's words never a placeholder.
        self.assertIn("  1. Criterion 2: prints 2, the item asks for 1 (`out.txt`)\n  2. No criterion: the test was rewritten to skip { {PORT}} (`tests/test_x.py:3`)\n", text)
        self.assertIn(f"The fix is `git -C {self.wt} diff --no-renames {self.first} {head}`.", text)
        self.assertIn(f"(`… diff --no-renames {self.first} {head} -- <path>`)", text)
        self.assertIn("  - 1 files changed, 1 lines added and 1 removed: `feature.txt`.", text)
        self.assertIn("**Required checks are not yours to run or to wait for.**", text)
        self.assertIn(f"`{self.item_dir()}/grade-2.done`", text)
        self.assertIn('"answers": [', text)
        self.assertNotIn('"criteria"', text)
        self.assertEqual(read_json(confirm.plan_path(self.item_dir(), 2))["head_sha"], head)
        # Rendering starts nothing and counts nothing, and says which brief it is and why.
        self.assertEqual(self.said["brief"], str(self.item_dir() / "grade-2.brief.md"))
        self.assertTrue(self.said["confirm"].startswith(f"delta: review 1 failed at {self.first[:12]} with 2 fix-list entries"), self.said)
        self.assertEqual(self.attempts()["grade"], 1)

    def test_only_a_reviews_brief_is_one_or_the_other(self):
        self.review_fails()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(spawn.main(["--root", str(self.root), "--run-dir", str(self.run_dir), "brief", "--id", "A-1", "--phase", "build"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"brief": str(self.item_dir() / "build-2.brief.md")})

    def test_asked_for_in_full_the_brief_is_a_reviews_and_nothing_says_confirm(self):
        self.review_fails()
        self.fix()
        self.brief()
        self.assertTrue(confirm.plan_path(self.item_dir(), 2).exists())
        text = self.brief("--full")
        self.assertTrue(text.startswith("# Review brief: A-1, review 2\n"))
        self.assertFalse(confirm.plan_path(self.item_dir(), 2).exists())
        self.assertEqual(self.said["confirm"], "full: a full review was asked for")

    def test_a_brief_the_reworks_builder_left_in_the_item_directory_is_not_sent(self):
        # Found in the fifth review's second look: a brief already at its place was sent as it was. The rework's builder
        # left one with an entry taken out, and its plan; the confirm was briefed by the builder and the gate passed.
        self.review_fails(fix_list=(FIX_ONE, FIX), results=("fail", "fail"))
        head = self.fix()
        path = self.item_dir() / "grade-2.brief.md"
        path.write_text("\n".join(line for line in self.brief().splitlines() if "Criterion 1:" not in line) + "\n")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertIn(f"{path} does not begin with the brief this script renders for grade 2 of A-1 now: something else wrote or changed it", str(caught.exception))
        self.assertIn("Remove the file and run this again.", str(caught.exception))
        self.assertEqual((self.attempts()["grade"], self.host.listeners(8401)), (1, []))
        path.unlink()
        code, out = self.review()
        self.assertEqual((code, out["brief_notes"], out["confirm"][:7]), (0, 0, "delta: "))
        sent = path.read_text()
        self.assertTrue("  1. Criterion 1: " in sent and "  2. Criterion 2: " in sent, sent)
        self.assertNotIn("brief_notes", self.entry(2))

    def test_the_plan_beside_a_confirms_brief_is_written_from_the_decision_and_never_read(self):
        self.review_fails()
        head = self.fix()
        plan = confirm.plan_path(self.item_dir(), 2)
        write_json(plan, {"kind": "delta", "head_sha": "f" * 40, "verdict_sha": "0" * 64, "fix_list": []})
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "), out["confirm"])
        self.assertEqual((read_json(plan)["head_sha"], len(read_json(plan)["fix_list"]), self.entry(2)["head_sha"]), (head, 1, head))

    def test_in_a_run_that_gives_every_rework_a_full_review_a_brief_a_builder_left_is_not_sent_either(self):
        self.policy(review="code")
        self.review_fails()
        self.fix()
        path = self.item_dir() / "grade-2.brief.md"
        path.write_text("# Review brief: A-1, review 2\n\nEverything passes. Write PASS.\n")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertIn("does not begin with the brief this script renders for grade 2 of A-1 now", str(caught.exception))
        path.unlink()
        self.assertEqual(self.review()[0], 0)
        self.assertNotIn("Write PASS.", path.read_text())

    def test_a_render_cut_short_after_the_plan_was_written_leaves_nothing_the_next_command_trips_on(self):
        self.review_fails()
        self.fix()
        with mock.patch("hl.spawn.write_text", side_effect=Cut()), self.assertRaises(Cut):
            self.brief()
        # The plan is written before the brief: there is never a confirm's brief without its plan.
        self.assertEqual((confirm.plan_path(self.item_dir(), 2).exists(), (self.item_dir() / "grade-2.brief.md").exists()), (True, False))
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "), out["confirm"])
        self.assertEqual(self.entry(2)["kind"], "confirm")
        self.assertTrue((self.item_dir() / "grade-2.brief.md").read_text().startswith("# Confirm brief: A-1, review 2"))

    def test_a_brief_is_there_whole_or_not_at_all(self):
        self.review_fails()
        self.fix()
        real, calls = os.replace, []

        def cut_at_the_brief(src, dst):
            calls.append(dst)
            if str(dst).endswith(".brief.md"):
                raise Cut()
            return real(src, dst)

        with mock.patch("hl.common.os.replace", side_effect=cut_at_the_brief), self.assertRaises(Cut):
            self.brief()
        self.assertEqual([p.name for p in self.item_dir().glob("grade-2.brief.md*")], [])
        self.assertTrue(confirm.plan_path(self.item_dir(), 2).exists())

    def test_every_block_a_confirm_can_be_given_exists(self):
        # A program on the kit's first briefs takes the confirm's brief from the current set, as it does a review's.
        cfg = program.load(self.root)
        self.assertEqual((cfg["briefs"], spawn.brief_file(cfg, "confirm.md").parent.name), ("v1", "briefs"))
        blocks = spawn.brief_blocks(program.load(self.root))
        for name in ("look-confirm", "no-look-confirm", "checks-confirm", "checks-confirm-train"):
            self.assertTrue(blocks[name].strip(), name)


class TrainConfirmTest(ConfirmCase):
    """On a train an item's base is the train's branch, which moves as other
    items merge. That alone is no reason for a full review; the item's own
    branch taking the train in is."""

    POLICY = {"merge": "train", "train": {"branch": TRAIN, "item_checks": []}}
    REPO = {"train": {"batch": 2, "limit": 4}}
    BASE = TRAIN

    def before(self):
        # Another item is on the train already: the item's branch is cut from it, not from the base.
        from hl import train

        train.ensure(program.load(self.root), "owner/repo", self.clone, self.tools)
        self.push_to(TRAIN, "earlier.txt", "an item that merged before this one\n")

    def test_a_train_that_moved_on_is_no_reason_for_a_full_review(self):
        self.review_fails()
        cut = read_json(self.item_dir() / "grade-1.facts.json")["base"]
        self.assertEqual(cut, git(self.clone, "rev-parse", f"origin/{TRAIN}"))
        # Two more items merge into the train, and the train takes the base in, while the fix is made.
        self.push_to(TRAIN, "later.txt", "an item that merged meanwhile\n")
        self.push_to("main", "hotfix.txt", "merged into the base by another hand\n")
        other = self.push_to(TRAIN, "later-2.txt", "one more\n")
        git(other, "fetch", "-q", "origin", "main")
        git(other, "merge", "-q", "--no-ff", "--no-edit", "origin/main")
        git(other, "push", "-q", "origin", f"HEAD:refs/heads/{TRAIN}")
        head = self.fix()
        code, out = self.review()
        self.assertTrue(out["confirm"].startswith("delta: "), out["confirm"])
        brief = (self.item_dir() / "grade-2.brief.md").read_text()
        self.assertNotIn("{{", brief)
        self.assertIn(f"**The repository's full checks are not held on this pull request.** It merges into `{TRAIN}`", brief)
        self.assertIn("No check is held on this pull request.", brief)
        code, out = self.confirmed(head)
        self.assertEqual((code, out["effective"], out["problems"]), (0, "PASS", []))
        # The gate fetches the train as it is now, and asks the same of the pull request's merge base.
        self.assertEqual(self.gate(train_ahead=(3, 4))["head_sha"], head)
        self.assertNotEqual(cut, git(self.clone, "rev-parse", f"origin/{TRAIN}"))
        self.assertIn("its limit is 4", self.refusal(train_ahead=(4, 4)))

    def take_the_train_in(self):
        self.push_to(TRAIN, "later.txt", "an item that merged meanwhile\n")
        git(self.wt, "fetch", "-q", "origin", f"+refs/heads/{TRAIN}:refs/remotes/origin/{TRAIN}")
        git(self.wt, "merge", "-q", "--no-edit", f"origin/{TRAIN}")
        self.commit(("feature.txt", "feature, fixed\n"))
        git(self.wt, "push", "origin", self.BRANCH)

    def test_a_fix_that_took_the_train_in_is_reviewed_in_full(self):
        self.review_fails()
        self.fix(how=self.take_the_train_in)
        out = self.gives_full("the fix holds a merge (")
        self.assertIn("the pull request's merge base moved from ", out["confirm"])

    def test_a_confirm_on_the_record_of_a_fix_that_took_the_train_in_does_not_merge(self):
        self.review_fails()
        said = self.gate_refuses("the fix holds a merge (", self.fix(how=self.take_the_train_in))
        self.assertIn("the pull request's merge base moved from ", said)

    def rewrite_the_train(self):
        """The train's branch put back to the base: the item that was on it before this one is gone from it."""
        git(self.clone, "push", "-q", "--force", "origin", "refs/remotes/origin/main:refs/heads/" + TRAIN)

    def test_a_train_that_was_rewritten_under_the_pull_request_means_a_full_review(self):
        self.review_fails()
        cut = read_json(self.item_dir() / "grade-1.facts.json")["base"]
        self.rewrite_the_train()
        self.fix()
        self.gives_full(f"the pull request's merge base moved from {cut[:12]} to {git(self.clone, 'rev-parse', 'origin/main')[:12]} since review 1")

    def test_a_confirm_does_not_merge_into_a_train_that_was_rewritten_since(self):
        self.review_fails()
        head = self.fix()
        self.on_record(head)
        self.assertEqual(self.gate(train_ahead=(0, 4))["head_sha"], head)
        self.rewrite_the_train()
        self.assertIn("cannot stand in for a full review: the pull request's merge base moved from ", self.refusal(train_ahead=(0, 4)))


SKIPPED = {"criterion": None, "what": "the test was rewritten to skip", "where": "tests/test_x.py:3", "evidence": []}


class ConfirmVerdictShapeTest(ConfirmCase):
    def test_a_confirms_verdict_with_a_list_of_criteria_is_refused_in_words(self):
        # Found in the fifth review: such a list, with an entry that had no evidence, ended `hl-flow reviewed` in a traceback.
        self.review_fails()
        head = self.fix()
        self.review()
        code, out = self.confirmed(head, criteria=[{"n": 1, "result": "pass", "note": "still passes"}])
        self.assertEqual((code, out["effective"]), (2, None))
        self.assertEqual(out["problems"], ["verdict-2.json: a confirm answers the fix list and carries no `criteria`: what you saw of a criterion goes in an answer's note or in a finding"])


class ConfirmVerdictTest(RunCase):
    """What a confirm's verdict must say: an answer for every entry of the
    fix list it was given, and whether the fix breaks anything."""

    def setUp(self):
        super().setUp()
        self.init()
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "code", "confirm": "delta"}})
        self.agree()
        self.build()
        self.grade(session="rev-1", verdict="FAIL", results=["pass", "fail"])
        self.fixes = [FIX, SKIPPED]
        first = self.item_dir() / "verdict-1.json"
        write_json(first, {**read_json(first), "fix_list": self.fixes})
        self.build(session="build-2")
        plan = {"confirms": 1, "from_sha": "a" * 40, "verdict_sha": verdict.digest(read_json(first))}
        items.start_phase(self.run_dir, "A-1", "grade", session="rev-2", head_sha="b" * 40, confirm=plan)

    def say(self, **said):
        self.write("A-1", "verdict-2.json", json.dumps(confirm_verdict(**{"answers": ("fixed", "fixed"), **said})))
        return verdict.check(self.item_dir(), 2, style="code")

    def rewrite(self, mutate):
        path = self.item_dir() / "verdict-2.json"
        data = read_json(path)
        mutate(data)
        path.write_text(json.dumps(data))
        return verdict.check(self.item_dir(), 2, style="code")

    def test_every_entry_fixed_and_a_clean_fix_is_a_pass(self):
        out = self.say()
        self.assertEqual((out["problems"], out["raw"], out["effective"], out["downgraded"]), ([], "PASS", "PASS", None))
        self.assertEqual((out["confirms"], out["answers"], out["delta"], out["fix_list"]), (1, {"fixed": [1, 2], "not_fixed": [], "not_checked": []}, "clean", 0))
        self.assertEqual((out["by_checks"], out["criteria_in_question"]), ([], []))

    def test_a_missing_answer_makes_the_verdict_unusable(self):
        out = self.say(answers=("fixed",))
        self.assertEqual((out["effective"], out["problems"]), (None, ["answers must be exactly entries [1, 2] of the fix list, in that order, got [1]"]))

    def test_answers_must_be_the_entries_once_each_and_in_order(self):
        for entries in ([2, 1], [1, 1, 2], [1, 2, 3], [0, 1], []):
            self.say()
            out = self.rewrite(lambda d, entries=entries: d.update(answers=[{"entry": k, "result": "fixed", "evidence": [], "note": "seen"} for k in entries]))
            self.assertEqual(out["problems"], [f"answers must be exactly entries [1, 2] of the fix list, in that order, got {entries}"], entries)

    def test_an_entry_that_is_not_fixed_is_not_a_pass(self):
        out = self.say(answers=("fixed", "not_fixed"))
        self.assertEqual((out["raw"], out["effective"], out["downgraded"]), ("PASS", "FAIL", "verdict says PASS but entries [2] of the fix list are not fixed"))
        self.assertEqual(out["answers"], {"fixed": [1], "not_fixed": [2], "not_checked": []})
        # Not fixed outweighs not checked.
        self.assertEqual(self.say(answers=("not_checked", "not_fixed"))["effective"], "FAIL")

    def test_an_entry_that_was_not_checked_is_not_a_pass(self):
        out = self.say(answers=("not_checked", "fixed"))
        self.assertEqual((out["effective"], out["downgraded"]), ("INCONCLUSIVE", "verdict says PASS but entries [1] of the fix list are not_checked"))

    def test_a_fix_that_breaks_something_is_not_a_pass(self):
        out = self.say(delta="breaks")
        self.assertEqual((out["effective"], out["downgraded"], out["delta"]), ("FAIL", "verdict says PASS but the fix is said to break something", "breaks"))

    def test_a_pass_with_something_still_to_change_is_a_fail(self):
        out = self.say(fix_list=[FIX])
        self.assertEqual((out["effective"], out["downgraded"], out["fix_list"]), ("FAIL", "verdict says PASS but the fix list has 1 entries", 1))

    def test_a_look_that_differs_or_a_look_that_was_needed_and_not_made_is_not_a_pass(self):
        (self.item_dir() / "evidence-2").mkdir()
        (self.item_dir() / "evidence-2" / "a.png").write_bytes(PNG)
        look = {"mode": "dark", "url": ALERTS, "screenshot": "evidence-2/a.png", "result": "differs"}
        out = self.say(looks=[look])
        self.assertEqual((out["effective"], out["downgraded"]), ("FAIL", "verdict says PASS but a look differs from its reference"))
        self.assertEqual(self.say(looks=[{**look, "result": "match"}], look_needed=True)["effective"], "PASS")
        out = self.say(look_needed=True)
        self.assertEqual((out["effective"], out["downgraded"]), ("INCONCLUSIVE", "verdict says PASS and that the fix alters something a person sees, but records no look"))

    def test_a_fail_says_what_is_still_to_change_and_any_other_word_stands(self):
        out = self.say(word="FAIL", answers=("fixed", "not_fixed"), fix_list=[SKIPPED])
        self.assertEqual((out["problems"], out["effective"], out["downgraded"]), ([], "FAIL", None))
        self.assertEqual(self.say(word="FAIL", answers=("fixed", "not_fixed"))["problems"], ["a FAIL needs a fix list: say what the builder must change"])
        self.assertEqual(self.say(word="INCONCLUSIVE", answers=("fixed", "not_checked"))["effective"], "INCONCLUSIVE")

    def test_every_shape_problem_is_reported_at_once(self):
        self.say()

        def spoil(d):
            d.update(verdict="pass", how_run="", delta={"result": "fine", "note": "x"}, fix_list="none", looks="none", look_needed="no")
            d["findings"] = [{"kind": "taste", "severity": "low", "confidence": 0.5, "summary": "s", "evidence": []}]
            d["answers"] = ["fixed", {"entry": "1"}, {"entry": True}, {"entry": 1, "result": "done", "note": " "}, {"entry": 2, "result": "fixed", "evidence": [], "note": 7}]

        problems = self.rewrite(spoil)["problems"]
        for expected in (
            "verdict must be one of PASS, FAIL, INCONCLUSIVE, got 'pass'",
            "each answer must be an object, got 'fixed'",
            "an answer is missing the number of the entry it answers: {'entry': '1'}",
            "an answer is missing the number of the entry it answers: {'entry': True}",
            "the answer to entry 1 has result 'done'; use fixed, not_fixed or not_checked",
            "the answer to entry 1 is missing an evidence list",
            "the answer to entry 1 is missing a note: say what you ran and what you saw",
            "the answer to entry 2 is missing a note: say what you ran and what you saw",
            "delta must be an object whose result is clean or breaks: say whether the fix breaks anything",
            "how_run must be a non-empty string",
            "fix_list must be a list (empty on a PASS)",
            "looks must be a list",
            "look_needed must be true or false",
            "a finding has kind 'taste'; use one of pre-existing, other-files, environment, design, criterion",
        ):
            self.assertIn(expected, problems)
        self.assertEqual(len(problems), 14)

    def test_the_parts_are_each_asked_for(self):
        self.say()
        self.assertEqual(self.rewrite(lambda d: d.pop("answers"))["problems"][0], "answers must be a list, with one answer for each entry of the fix list")
        self.say()
        self.assertEqual(self.rewrite(lambda d: d.pop("delta"))["problems"], ["delta must be an object whose result is clean or breaks: say whether the fix breaks anything"])
        self.say()
        self.assertEqual(self.rewrite(lambda d: d["delta"].update(note=" "))["problems"], ["delta is missing a note: say what the fix changes beyond the fix list and what you ran"])
        self.say()
        self.assertEqual(self.rewrite(lambda d: d.pop("findings"))["problems"], ["findings must be a list (it may be empty)"])
        self.write("A-1", "verdict-2.json", json.dumps(["PASS"]))
        self.assertEqual(verdict.check(self.item_dir(), 2, style="code")["problems"], ["the file must hold one JSON object"])

    def test_a_reviews_verdict_does_not_pass_for_a_confirms(self):
        full = {**read_json(self.item_dir() / "verdict-1.json"), "verdict": "PASS", "fix_list": []}
        full["criteria"] = [{**c, "result": "pass"} for c in full["criteria"]]
        self.write("A-1", "verdict-2.json", json.dumps(full))
        out = verdict.check(self.item_dir(), 2, style="code")
        self.assertEqual(out["effective"], None)
        self.assertIn("answers must be a list, with one answer for each entry of the fix list", out["problems"])
        # The same file as the verdict of a full review is a pass: which it is comes from the attempt log, not the file.
        self.write("A-1", "verdict-1.json", json.dumps(full))
        self.assertEqual(verdict.check(self.item_dir(), 1, style="code")["effective"], "PASS")

    def test_the_fix_list_it_answers_must_be_the_one_it_was_given(self):
        first = self.item_dir() / "verdict-1.json"
        kept = read_json(first)
        write_json(first, {**kept, "fix_list": [FIX]})
        out = self.say(answers=("fixed",))
        self.assertEqual(out["problems"], ["verdict-1.json is not the verdict this confirm was given to answer: the file changed after the confirm started"])
        # Anything in it: the looks it recorded are what the confirm must look at again.
        write_json(first, {**kept, "looks": []})
        self.assertIn("is not the verdict this confirm was given to answer", self.say()["problems"][0])
        write_json(first, kept)
        self.assertEqual(self.say()["effective"], "PASS")
        write_json(first, {**kept, "fix_list": "none"})
        self.assertIn("is not the verdict this confirm was given to answer", self.say()["problems"][0])
        # Even with its digest on the attempt: a fix list that is not a list has no entries to answer.
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        attempts["log"][-1]["verdict_sha"] = verdict.digest(read_json(first))
        write_json(path, attempts)
        self.assertIn("is not the verdict this confirm was given to answer", self.say()["problems"][0])
        for text in ("not json", json.dumps({"verdict": "FAIL"}), json.dumps([1])):
            first.write_text(text)
            self.assertIn("verdict-1.json, whose fix list this confirm answers, cannot be read", self.say()["problems"][0], text)
        first.unlink()
        self.assertIn("verdict-1.json, whose fix list this confirm answers, cannot be read", self.say()["problems"][0])

    def test_evidence_and_criteria_are_held_as_in_a_review(self):
        out = self.say(fix_list=[{**FIX, "criterion": 9}], word="FAIL", answers=("not_fixed", "fixed"))
        self.assertEqual(out["problems"], ["a fix-list entry names criterion 9, which the contract does not have"])
        self.say()
        out = self.rewrite(lambda d: d["answers"][0].update(evidence=["evidence-2/entry-1.txt"]))
        self.assertEqual(out["problems"], ["the answer to entry 1 cites 'evidence-2/entry-1.txt', which does not exist"])
        (self.item_dir() / "evidence-2").mkdir()
        (self.item_dir() / "evidence-2" / "entry-1.txt").write_text("1\n")
        self.assertEqual(verdict.check(self.item_dir(), 2, style="code")["effective"], "PASS")
        finding = {"kind": "criterion", "criterion": 2, "severity": "medium", "confidence": 0.9, "summary": "entry 1 asks for what the item does not", "evidence": []}
        out = self.say(answers=("not_checked", "fixed"), findings=[finding])
        self.assertEqual((out["effective"], out["criteria_in_question"]), ("INCONCLUSIVE", [2]))
        (self.item_dir() / "contract.md").unlink()
        self.assertIn("contract.md cannot be read to compare criteria", self.say()["problems"][0])

    def test_a_transcript_that_drove_nothing_is_a_problem_as_for_any_verdict(self):
        self.say()
        transcript = self.tmp / "t.jsonl"
        transcript.write_text(json.dumps({"message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]}}) + "\n")
        out = verdict.check(self.item_dir(), 2, transcript=transcript, style="code")
        self.assertEqual((out["driving_calls"], out["problems"]), (0, ["the evaluator's transcript shows no shell or browser call: the build was not driven"]))
        self.assertIn("cannot be read", verdict.check(self.item_dir(), 2, transcript=self.tmp / "none.jsonl", style="code")["problems"][0])

    def test_which_review_is_a_confirm_comes_from_the_attempt_log(self):
        self.say()
        self.assertEqual(verdict.confirm_of(self.item_dir(), 2)["confirms"], 1)
        self.assertIsNone(verdict.confirm_of(self.item_dir(), 1))
        self.assertIsNone(verdict.confirm_of(self.tmp, 2))
        # A blind grade is never read as one, and neither is an attempt that was voided.
        self.assertEqual(verdict.check(self.item_dir(), 2, style="blind")["problems"][0], "criteria must be a non-empty list")
        path = self.item_dir() / "attempts.json"
        attempts = read_json(path)
        attempts["log"][-1]["voided"] = {"at": "2026-10-12T00:00:00Z", "note": "by hand"}
        write_json(path, attempts)
        self.assertIsNone(verdict.confirm_of(self.item_dir(), 2))
        self.assertEqual(verdict.check(self.item_dir(), 2, style="code")["problems"][0], "criteria must be a non-empty list")

    def test_the_command_line_refuses_a_malformed_confirm_and_takes_a_usable_one(self):
        def cli():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = verdict.main(["--item-dir", str(self.item_dir()), "--n", "2"])
            return code, json.loads(out.getvalue())

        self.say()
        code, said = cli()
        self.assertEqual((code, said["effective"], said["confirms"]), (0, "PASS", 1))
        self.say(answers=("fixed",))
        code, said = cli()
        self.assertEqual((code, said["effective"]), (2, None))
        self.assertIn("answers must be exactly entries [1, 2]", said["problems"][0])


class PolicyTest(RunCase):
    def test_a_delta_confirm_needs_reviews_that_read_the_code(self):
        for policy in ({"confirm": "delta"}, {"review": "blind", "confirm": "delta"}):
            with self.assertRaises(Refusal) as caught:
                items.check_policy(policy)
            self.assertIn("a delta confirm of a rework needs reviews that read the code and write a fix list (`review: code`)", str(caught.exception))
        with self.assertRaises(Refusal) as caught:
            items.check_policy({"review": "code", "confirm": "sometimes"})
        self.assertIn("confirm 'sometimes' is not one of full, delta", str(caught.exception))

    def test_a_run_keeps_the_bound_it_was_created_with(self):
        checked = items.check_policy({"review": "code", "confirm": "delta"})
        self.assertEqual((checked["confirm"], checked["confirm_bound"]), ("delta", {"share": 0.25, "lines": 40, "files": 2}))
        self.assertEqual(items.check_policy({"review": "code", "confirm": "delta", "confirm_bound": {"lines": 10}})["confirm_bound"], {"share": 0.25, "lines": 10, "files": 2})
        run = items.init_run(self.run_dir, "run-1", "owner/repo", [str(self.file_a)], policy={"review": "code", "confirm": "delta", "confirm_bound": {"share": 1, "files": 0}})
        self.assertEqual(run["policy"]["confirm_bound"], {"share": 1, "lines": 40, "files": 0})
        self.assertEqual((items.policy(run)["confirm"], items.policy(run)["confirm_bound"]), ("delta", {"share": 1, "lines": 40, "files": 0}))
        # A run file that names the setting and no bound is held to the kit's own.
        self.assertEqual(items.policy({"policy": {"review": "code", "confirm": "delta"}})["confirm_bound"], items.CONFIRM_BOUND)

    def test_a_run_whose_reworks_get_a_full_review_records_nothing_of_it(self):
        for policy in ({"review": "code"}, {"review": "code", "confirm": "full"}, {"review": "blind", "confirm": "full", "confirm_bound": {"lines": 5}}):
            checked = items.check_policy(policy)
            self.assertNotIn("confirm", checked)
            self.assertNotIn("confirm_bound", checked)
        self.assertEqual((items.policy({"policy": {"review": "code"}})["confirm"], items.policy({"policy": {"review": "code", "confirm_bound": {"lines": 5}}})["confirm_bound"]), ("full", {}))

    def test_a_bound_that_makes_no_sense_is_refused(self):
        for bound in ({"share": 0}, {"share": 1.5}, {"share": True}, {"share": "half"}, {"lines": -1}, {"lines": 2.5}, {"lines": True}, {"files": -1}, {"files": "2"}, {"words": 3}):
            with self.assertRaises(Refusal, msg=bound) as caught:
                items.check_policy({"review": "code", "confirm": "delta", "confirm_bound": bound})
            self.assertIn("`confirm_bound` takes `share` (above 0, at most 1) and `lines` and `files` (whole numbers, 0 or more)", str(caught.exception))
        for bound in ({"share": 1}, {"share": 0.01}, {"lines": 0}, {"files": 0}):
            self.assertEqual(items.check_policy({"review": "code", "confirm": "delta", "confirm_bound": bound})["confirm"], "delta")

    def test_a_new_run_takes_the_setting_from_the_programs_settings(self):
        settings = {"premise": "routed", "review": "code", "bounds": {}}
        self.assertEqual(program.run_policy(settings, "o/r"), {"premise": "routed", "review": "code", "bounds": {}})
        self.assertEqual(program.run_policy({**settings, "confirm": "full", "confirm_bound": {"lines": 5}}, "o/r"), {"premise": "routed", "review": "code", "bounds": {}})
        chosen = program.run_policy({**settings, "confirm": "delta", "confirm_bound": {"lines": 5}}, "o/r")
        self.assertEqual((chosen["confirm"], chosen["confirm_bound"]), ("delta", {"lines": 5}))
        self.assertEqual(program.run_policy({**settings, "confirm": "delta"}, "o/r")["confirm_bound"], {})
        # It works with a train or without one.
        on_train = program.run_policy({**settings, "program": "p", "confirm": "delta", "repos": {"o/r": {"train": {}}}}, "o/r")
        self.assertEqual((on_train["merge"], items.check_policy(on_train)["confirm"]), ("train", "delta"))

    def test_init_through_the_cli_gives_the_run_the_setting_and_keeps_it(self):
        root = self.tmp / "root"
        root.mkdir()
        settings = {"program": "p", "base": "main", "worker_claude_ref": "op://a/b/c", "gh_token_ref": "op://a/b/c", "cursor_model": "m", "review": "code"}
        write_json(root / "program.json", {**settings, "confirm": "delta", "confirm_bound": {"files": 1}})
        argv = ["--run-dir", str(root / "runs" / "r"), "init", "--run-id", "r", "--repo", "o/r", "--file", str(self.file_a), "--root", str(root)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(items.main(argv), 0)
        self.assertEqual((json.loads(out.getvalue())["policy"]["confirm"], json.loads(out.getvalue())["policy"]["confirm_bound"]), ("delta", {"share": 0.25, "lines": 40, "files": 1}))
        # The program's settings change; the run does not.
        write_json(root / "program.json", settings)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(items.main(argv), 0)
        self.assertEqual(items.policy(items.load_run(root / "runs" / "r"))["confirm"], "delta")
        write_json(root / "program.json", {**settings, "review": "blind", "confirm": "delta"})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(items.main([*argv[:1], str(root / "runs" / "s"), *argv[2:4], "s", *argv[5:]]), 2)
        self.assertFalse((root / "runs" / "s" / "run.json").exists())


class BaseOnlyCloneTest(TrainConfirmTest):
    """A clone told to follow only the base and the loop's own item branches,
    as staging makes one for a program whose spec says `base_only`."""

    def before(self):
        git(self.clone, "config", "--unset-all", "remote.origin.fetch")
        for spec in ("+refs/heads/main:refs/remotes/origin/main", "+refs/heads/hl/*:refs/remotes/origin/hl/*"):
            git(self.clone, "config", "--add", "remote.origin.fetch", spec)
        super().before()

    def test_the_gate_reads_the_train_as_the_remote_has_it(self):
        self.review_fails()
        head = self.fix()
        self.on_record(head)
        self.push_to(TRAIN, "later.txt", "an item that merged meanwhile\n")
        self.assertEqual(self.gate(train_ahead=(1, 4))["head_sha"], head)
        self.assertEqual(git(self.clone, "rev-parse", f"origin/{TRAIN}"), git(self.origin, "rev-parse", TRAIN))


class StartOfAReviewTest(SpawnCase):
    """A review of any kind of run has the state of its checkout noted as it
    starts, where there is a checkout git can read. Here there is none: the
    worktree is a plain directory."""

    def reviewing(self):
        self.start()
        self.finish_build()
        return self.start("--set", "READY_SUMMARY=answered 200", phase="grade")

    def test_a_review_whose_checkout_git_cannot_read_starts_with_no_state_noted(self):
        self.assertTrue(self.reviewing()["started"])
        self.assertNotIn("baseline", self.attempts()["log"][-1])

    def test_a_review_starts_where_there_is_no_git_to_ask(self):
        with mock.patch("hl.spawn.merge.Tools", side_effect=Refusal("git and gh must both be available (pass --git and --gh)")):
            self.assertTrue(self.reviewing()["started"])
        self.assertNotIn("baseline", self.attempts()["log"][-1])
