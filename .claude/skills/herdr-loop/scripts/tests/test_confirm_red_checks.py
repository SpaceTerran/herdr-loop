"""A delta confirm of a fix for red checks (`confirm_red_checks: delta`):
a review passed, the pull request's required checks then came out red on
the commit it passed, and the builder pushed a fix. The confirm answers
those checks in place of a fix list; everything else a delta confirm is
held to holds the same. Driven, like `test_confirm.py`, through the
commands a manager runs: `hl-wait --checks` records the checks,
`hl-flow review` decides, `hl-flow reviewed` reads, `hl-merge` asks again."""

from __future__ import annotations

import contextlib
import io
import json

from hl import items, ledger, merge, program, spawn, verdict, wait
from hl.common import Refusal, read_json, write_json

from .support import RunCase
from .test_confirm import CURL, IN_FULL, ConfirmCase, confirm_verdict
from .test_merge import TEST_REF, git
from .test_review import write_transcript

LINK = "https://github.com/owner/repo/actions/runs/55/job/1"


class RedChecksCase(ConfirmCase):
    """An item whose first review passed, in a run whose reworks may get a
    delta confirm, and whose fixes for red checks may get one too."""

    POLICY = {"confirm_red_checks": "delta"}

    def passes(self):
        """Review 1 passes: every criterion, no fix list, read by `hl-flow reviewed`."""
        return self.review_fails(fix_list=(), results=("pass", "pass"), word="PASS")

    def checks_red(self, head=None):
        """`hl-wait --checks` finds the required checks of the pull request red on `head` (the commit review 1 passed)."""
        self.set_gh(head or self.first, checks="fail")
        event = wait.checks_event(self.run_dir, "A-1", self.tools, 0, 0)
        self.assertFalse(event["passed"], event)
        return event

    def brief(self, n=2):
        return (self.item_dir() / f"grade-{n}.brief.md").read_text()

    def build_entry(self, n):
        return next(e for e in items.load_attempts(self.run_dir, "A-1")["log"] if e["phase"] == "build" and e["n"] == n)

    def on_record_red(self, head, session="confirm-by-hand", **said):
        """A passed confirm of a fix for red checks on the record, whether or not the script would have started it."""
        passed = read_json(self.item_dir() / "verdict-1.json")
        record = read_json(items.red_checks_record(self.item_dir(), 1))
        plan = {"confirms": 1, "from_sha": self.first, "verdict_sha": verdict.digest(passed), "red_checks": record["red"], "checks_sha": verdict.digest(record)}
        entry = items.start_phase(self.run_dir, "A-1", "grade", session=session, head_sha=head, confirm=plan, how_run=self.entry(1).get("how_run"))
        write_transcript(self.home, session, [self.prints(head), CURL])
        self.write("A-1", f"verdict-{entry['n']}.json", json.dumps(confirm_verdict(**said)))
        items.record_read(self.run_dir, "A-1", entry["n"], {"checkout": [], "dirty_before": []})
        return entry


class RedChecksTest(RedChecksCase):
    """The path the setting is for: a pass, red checks, one fix, one confirm, a merge."""

    def test_a_small_fix_for_checks_that_came_out_red_after_a_pass_is_given_a_delta_confirm(self):
        self.passes()
        event = self.checks_red()
        record = read_json(items.red_checks_record(self.item_dir(), 1))
        self.assertEqual((record["head_sha"], record["red"], record["pr"]), (self.first, [{"name": "ci", "bucket": "fail", "link": LINK}], "7"))
        self.assertEqual(event["checks_file"], str(self.item_dir() / "checks-1.md"))
        head = self.fix()
        code, out = self.review()
        self.assertEqual((code, out["result"]), (0, "reviewing"), out)
        self.assertEqual(
            out["confirm"],
            f"delta: review 1 passed at {self.first[:12]} and required checks then came out red on it (`ci`), and the fix since is 2 changed lines in 1 file (a confirm may cover 40)",
        )
        entry = self.entry(2)
        self.assertEqual(
            (entry["kind"], entry["confirms"], entry["from_sha"], entry["head_sha"], entry["red_checks"], entry["checks_sha"]),
            ("confirm", 1, self.first, head, record["red"], verdict.digest(record)),
        )
        brief = self.brief()
        self.assertTrue(brief.startswith("# Confirm brief: A-1, review 2"), brief[:80])
        self.assertIn(
            f"Review 1 was a full review of the change at commit `{self.first}`, and passed it. The pull request's required checks then came out red on that same commit, "
            "and the fix list below is those checks, one entry for each. A builder has since pushed a fix.",
            brief,
        )
        self.assertIn(f"- The required checks that came out red on `{self.first}`, numbered as you answer them. `{self.item_dir() / 'checks-1.md'}` lists them", brief)
        self.assertIn(f"\n  1. Required check `ci`: fail. {LINK}\n", brief)
        self.assertNotIn("The manager's note to the builder", brief)
        self.assertNotIn("{{", brief)

    def test_a_confirm_of_it_that_passes_merges_and_the_merge_says_what_it_confirmed(self):
        self.passes()
        self.checks_red()
        head = self.fix()
        self.review()
        code, out = self.confirmed(head)
        self.assertEqual((code, out["effective"], out["problems"]), (0, "PASS", []))
        self.assertEqual((out["confirms"], out["answers"]), (1, {"fixed": [1], "not_fixed": [], "not_checked": []}))
        opts = merge._parser().parse_args(["--run-dir", str(self.run_dir), "--repo-dir", str(self.clone), "--id", "A-1", "--lock-ref", TEST_REF, "--lock-wait", "0.3", "--lock-poll", "0.05"])
        code, merged = merge.merge(self.run_dir, "A-1", self.clone, self.tools, opts)
        self.assertEqual((code, merged["result"], merged["head_sha"], merged["graded_n"]), (0, "merged", head, 2))
        asked = [c for c in self.gh_calls() if c[:2] == ["pr", "merge"]][0]
        self.assertIn(
            f"Review 1 was a full review of {self.first[:12]} and passed it, and required checks then came out red on it (ci); review 2 confirmed the fix at {head[:12]}, contract ",
            asked[asked.index("--body") + 1],
        )
        self.assertEqual(ledger.item_record(self.run_dir, "A-1")["reviews"], ["PASS", "PASS (confirm)"])

    def test_its_verdict_answers_every_red_check_and_no_more(self):
        self.passes()
        self.checks_red()
        head = self.fix()
        self.review()
        code, out = self.confirmed(head, answers=("fixed", "fixed"))
        self.assertEqual(out["effective"], None, out)
        self.assertIn("answers must be exactly entries [1] of the fix list, in that order, got [1, 2]", " | ".join(out["problems"]))

    def test_the_managers_note_on_the_rework_is_named_in_the_brief(self):
        self.passes()
        self.checks_red()
        note = self.item_dir() / "rework-2.md"
        note.write_text("# A-1 rework 2 (manager)\n\nThe failing test is tests/test_feature.py::test_it; the item changed what it asserts.\n")
        self.fix()
        self.review()
        self.assertIn(f"nothing merges until it is green. The manager's note to the builder about them: `{note}`.\n", self.brief())

    def test_a_fix_after_a_failed_review_is_confirmed_as_before(self):
        # The setting adds a kind of rework; the one there was is briefed word for word as it was.
        self.review_fails()
        self.fix()
        _, out = self.review()
        self.assertTrue(out["confirm"].startswith(f"delta: review 1 failed at {self.first[:12]} with 1 fix-list entry"), out["confirm"])
        self.assertNotIn("red_checks", self.entry(2))
        brief = self.brief()
        self.assertIn(f"Review 1 was a full review of the change at commit `{self.first}`, and failed it with the fix list below. A builder has since pushed a fix.", brief)
        self.assertIn("- Its fix list, numbered as you answer it:\n\n  1. Criterion 2: prints 2, the item asks for 1 (`out.txt`)\n", brief)
        self.assertNotIn("required checks then came out red", brief)


class WhenThereIsNothingToConfirmTest(RedChecksCase):
    """What a confirm of a fix for red checks needs, and the full review where it is missing."""

    def test_without_the_setting_the_fix_gets_a_full_review_as_before(self):
        self.policy(review="code", confirm="delta")
        self.passes()
        self.checks_red()
        self.fix()
        self.gives_full("review 1 is PASS, not a fail with a fix list")

    def test_a_rework_after_a_pass_with_no_red_checks_on_record_is_reviewed_in_full(self):
        self.passes()
        (self.item_dir() / "rework-2.md").write_text("Merge main into the branch.\n")
        self.fix()
        self.gives_full("review 1 passed, and no required check is on record as red on the commit it was of (checks-1.json, which `hl-wait --checks` writes)")

    def test_checks_that_came_out_red_on_another_commit_are_not_this_fix(self):
        self.passes()
        self.checks_red(head="0" * 40)
        self.fix()
        self.gives_full(f"the checks in checks-1.json came out red on 000000000000, not on {self.first[:12]}, the commit review 1 passed")

    def test_checks_that_went_green_again_on_the_same_build_leave_nothing_to_confirm(self):
        self.passes()
        self.checks_red()
        self.set_gh(self.first)
        cleared = wait.checks_event(self.run_dir, "A-1", self.tools, 0, 0)
        self.assertEqual((cleared["passed"], cleared["cleared_checks_file"]), (True, str(self.item_dir() / "checks-1.md")))
        self.assertFalse(items.red_checks_record(self.item_dir(), 1).exists())
        self.assertNotIn("red_checks_sha", self.build_entry(1))

    def test_hl_wait_keeps_the_records_digest_on_the_builds_attempt(self):
        self.passes()
        self.checks_red()
        record = read_json(items.red_checks_record(self.item_dir(), 1))
        self.assertEqual(self.build_entry(1)["red_checks_sha"], items.red_checks_digest(record))

    def test_a_record_the_reworks_builder_wrote_itself_means_a_full_review(self):
        # The kit's fresh review of this change (2026-11-19): with nothing red on record, the rework's builder, whose
        # permission checks are off, wrote checks-1.json beside checks-1.md and so turned its full review into a confirm.
        self.passes()
        (self.item_dir() / "rework-2.md").write_text("Rename the flag the item adds.\n")
        path = items.red_checks_record(self.item_dir(), 1)

        def builder_forges_the_record():
            write_json(path, {"head_sha": self.first, "red": [{"name": "ci", "bucket": "fail", "link": ""}]})
            self.commit(("feature.txt", "feature, renamed\n"))
            git(self.wt, "push", "origin", self.BRANCH)

        self.fix(how=builder_forges_the_record)
        self.gives_full("checks-1.json is not the record `hl-wait --checks` wrote of build 1's checks: it was written or changed by something else")

    def test_a_record_changed_by_hand_after_hl_wait_wrote_it_means_a_full_review(self):
        self.passes()
        self.checks_red()
        path = items.red_checks_record(self.item_dir(), 1)
        record = read_json(path)
        self.fix()
        write_json(path, {**record, "red": record["red"][:1] + [{"name": "lint", "bucket": "fail", "link": ""}]})
        self.gives_full("checks-1.json is not the record `hl-wait --checks` wrote of build 1's checks: it was written or changed by something else")

    def test_a_record_that_cannot_be_read_or_names_no_check_means_a_full_review(self):
        self.passes()
        self.checks_red()
        path = items.red_checks_record(self.item_dir(), 1)
        self.fix()
        path.write_text("{")
        self.assertIn("checks-1.json, the record of the checks that came out red after review 1 passed, cannot be read", self.plan()["why"])
        # A record with nothing red is one no script writes; were one kept, it would still name nothing to answer.
        empty = {"head_sha": self.first, "red": []}
        write_json(path, empty)
        items.keep_red_checks(self.run_dir, "A-1", 1, empty)
        self.assertIn("checks-1.json names no required check that came out red after review 1 passed", self.plan()["why"])

    def test_a_pass_whose_item_already_had_its_confirm_gets_a_full_review(self):
        self.policy(review="code", confirm="delta", confirm_red_checks="delta", bounds={"build": 3})
        self.review_fails()
        head = self.fix()
        self.review()
        self.confirmed(head)
        self.checks_red(head)
        self.fix(("feature.txt", "feature, fixed for the checks\n"), build=3)
        self.gives_full("review 2 was this item's one delta confirm")

    def plan(self):
        return spawn.review_plan(program.load(self.root), self.run_dir, "A-1", tools=self.tools)


class AtTheGateTest(RedChecksCase):
    """`hl-merge` asks it all again, from git and the run's records."""

    def test_the_gate_lets_a_confirm_the_script_would_have_given_merge(self):
        self.passes()
        self.checks_red()
        head = self.fix()
        self.on_record_red(head)
        self.assertEqual(self.gate()["head_sha"], head)

    def test_a_confirm_of_red_checks_in_a_run_without_the_setting_does_not_merge(self):
        self.passes()
        self.checks_red()
        head = self.fix()
        self.on_record_red(head)
        self.policy(review="code", confirm="delta")
        self.assertIn(f"review 2 is recorded as a delta confirm of a fix for red checks, and this run gives such a fix a full review{IN_FULL}", self.refusal())

    def test_a_confirm_whose_red_checks_changed_under_it_does_not_merge(self):
        self.passes()
        self.checks_red()
        head = self.fix()
        self.on_record_red(head)
        path = items.red_checks_record(self.item_dir(), 1)
        record = read_json(path)
        # As a later read of the same build by `hl-wait --checks` would write it, and keep it.
        later = {**record, "red": record["red"] + [{"name": "lint", "bucket": "fail", "link": ""}]}
        write_json(path, later)
        items.keep_red_checks(self.run_dir, "A-1", 1, later)
        self.assertIn(f"review 2 is a delta confirm that cannot stand in for a full review: checks-1.json, the checks it answers, changed after it started{IN_FULL}", self.refusal())

    def test_a_confirm_whose_record_was_changed_by_hand_or_removed_does_not_merge(self):
        self.passes()
        self.checks_red()
        head = self.fix()
        self.on_record_red(head)
        path = items.red_checks_record(self.item_dir(), 1)
        record = read_json(path)
        write_json(path, {**record, "red": record["red"] + [{"name": "lint", "bucket": "fail", "link": ""}]})
        self.assertIn(
            "review 2 is a delta confirm that cannot stand in for a full review: checks-1.json is not the record `hl-wait --checks` wrote of build 1's checks",
            self.refusal(),
        )
        path.unlink()
        self.assertIn("review 2 is a delta confirm that cannot stand in for a full review: review 1 passed, and no required check is on record as red", self.refusal())

    def test_a_confirm_that_answers_a_fix_list_after_a_pass_does_not_merge(self):
        self.passes()
        self.checks_red()
        head = self.fix()
        # An empty fix list answered entry by entry: no entry is missing, and still nothing it answers is what was red.
        self.on_record(head, answers=())
        self.assertIn(f"review 2 is a delta confirm that cannot stand in for a full review: review 1 is PASS, not a fail with a fix list{IN_FULL}", self.refusal())

    def test_a_confirm_that_answers_red_checks_after_a_fail_does_not_merge(self):
        self.review_fails()
        self.checks_red()
        head = self.fix()
        self.on_record_red(head)
        self.assertIn("it answers red checks, and review 1 is a fail with a fix list", self.refusal())


class RedChecksPolicyTest(RunCase):
    def test_it_extends_the_delta_confirm_and_takes_only_its_two_values(self):
        for policy in ({"review": "code", "confirm_red_checks": "delta"}, {"review": "code", "confirm": "full", "confirm_red_checks": "delta"}):
            with self.assertRaises(Refusal) as caught:
                items.check_policy(policy)
            self.assertIn("a delta confirm of a fix for red checks extends the delta confirm of a rework: it needs `confirm: delta`", str(caught.exception))
        with self.assertRaises(Refusal) as caught:
            items.check_policy({"review": "code", "confirm": "delta", "confirm_red_checks": "sometimes"})
        self.assertIn("confirm_red_checks 'sometimes' is not one of full, delta", str(caught.exception))

    def test_a_run_records_it_only_when_it_is_on_and_reads_as_before_without_it(self):
        self.assertEqual(items.check_policy({"review": "code", "confirm": "delta", "confirm_red_checks": "delta"})["confirm_red_checks"], "delta")
        for policy in ({"review": "code", "confirm": "delta"}, {"review": "code", "confirm": "delta", "confirm_red_checks": "full"}, {"review": "code"}):
            self.assertNotIn("confirm_red_checks", items.check_policy(policy))
        run = items.init_run(self.run_dir, "run-1", "owner/repo", [str(self.file_a)], policy={"review": "code", "confirm": "delta", "confirm_red_checks": "delta"})
        self.assertTrue(items.red_checks_confirmed(run))
        # Not a key of the policy a run is read by: a run made without it reads exactly as it did.
        self.assertNotIn("confirm_red_checks", items.policy(run))
        self.assertFalse(items.red_checks_confirmed({"policy": {"review": "code", "confirm": "delta"}}))
        self.assertFalse(items.red_checks_confirmed({"policy": {"review": "code", "confirm": "full", "confirm_red_checks": "delta"}}))

    def test_a_new_run_takes_it_from_the_programs_settings(self):
        settings = {"premise": "routed", "review": "code", "bounds": {}}
        self.assertEqual(program.run_policy({**settings, "confirm": "delta"}, "o/r").get("confirm_red_checks"), None)
        chosen = program.run_policy({**settings, "confirm": "delta", "confirm_red_checks": "delta"}, "o/r")
        self.assertEqual((chosen["confirm"], chosen["confirm_red_checks"], items.check_policy(chosen)["confirm_red_checks"]), ("delta", "delta", "delta"))
        self.assertEqual(program.DEFAULTS["confirm_red_checks"], "full")

    def test_init_through_the_cli_gives_the_run_the_setting_and_refuses_it_without_the_delta_confirm(self):
        root = self.tmp / "root"
        root.mkdir()
        settings = {"program": "p", "base": "main", "worker_claude_ref": "op://a/b/c", "gh_token_ref": "op://a/b/c", "cursor_model": "m", "review": "code"}
        write_json(root / "program.json", {**settings, "confirm": "delta", "confirm_red_checks": "delta"})
        argv = ["--run-dir", str(root / "runs" / "r"), "init", "--run-id", "r", "--repo", "o/r", "--file", str(self.file_a), "--root", str(root)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(items.main(argv), 0)
        self.assertEqual(json.loads(out.getvalue())["policy"]["confirm_red_checks"], "delta")
        write_json(root / "program.json", {**settings, "confirm_red_checks": "delta"})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(items.main([*argv[:1], str(root / "runs" / "s"), *argv[2:4], "s", *argv[5:]]), 2)
        self.assertFalse((root / "runs" / "s" / "run.json").exists())
