"""How long items wait on CI, as a save reports it: the checks' wall time on
each merged pull request, the merge-to-Verify time, the medians against the
program's budget, what is kept so GitHub is asked once, and that nothing
here fails a save. `gh` is a stand-in callable."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hl import items, pace
from hl.common import read_json, write_json

PROGRAM = {"program": "p", "base": "main", "worker_claude_ref": "-", "gh_token_ref": "op://x/y/z", "cursor_model": "composer"}

BACKLOG = "\n\n---\n\n".join(
    f"## T-{n} — Item {n}\n\n**Scope.** Thing {n}.\n\n**Accept.**\n1. It works.\n\n**Verify.** `true`" for n in range(1, 8)
)


def check(name, start, end, bucket="pass"):
    return {"name": name, "bucket": bucket, "startedAt": start, "completedAt": end}


# A container repository's #92 as `gh pr checks 92 --json name,bucket,startedAt,completedAt`
# gave it on 2026-11-02. Only `Lint and Validate Syntax` is required there; the
# skipped `Change scope` ends before it starts, as GitHub stamps it.
CONTAINER_92 = [
    check("CodeQL", "2026-11-02T21:14:43Z", "2026-11-02T21:14:45Z"),
    check("Change scope", "2026-11-02T21:14:17Z", "2026-11-02T21:14:09Z", bucket="skipping"),
    check("Python and YAML syntax", "2026-11-02T21:14:12Z", "2026-11-02T21:15:40Z"),
    check("Lint and Validate Syntax", "2026-11-02T21:14:11Z", "2026-11-02T21:16:16Z"),
    check("Full pytest suite", "2026-11-02T21:14:11Z", "2026-11-02T21:59:21Z"),
    check("Analyze (actions)", "2026-11-02T21:14:10Z", "2026-11-02T21:14:53Z"),
    check("Analyze (python)", "2026-11-02T21:14:10Z", "2026-11-02T21:15:25Z"),
]


def at(minutes: int) -> str:
    """A stamp `minutes` after 2026-11-01T00:00:00Z."""
    return (datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGh:
    """Answers `gh pr checks <pr> --repo <slug> --json …` from a table, and counts the calls.
    A read of the required checks alone is refused: it is the read that missed the container repository's suite."""

    def __init__(self, checks: dict):
        self.checks, self.calls = checks, []

    def __call__(self, args, timeout=None):
        self.calls.append(args)
        if args[:2] != ["pr", "checks"] or "--required" in args or "bucket" not in args[args.index("--json") + 1].split(","):
            return subprocess.CompletedProcess(args, 1, "", f"unexpected: {args}")
        answer = self.checks.get((args[args.index("--repo") + 1], args[2]))
        if answer is None:
            return subprocess.CompletedProcess(args, 1, "", "no checks reported on the 'x' branch")
        if isinstance(answer, str):
            return subprocess.CompletedProcess(args, 1, "", answer)
        return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")


class PaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        write_json(self.root / "program.json", PROGRAM)
        self.backlog = self.root / "T-ITEMS.md"
        self.backlog.write_text(BACKLOG)

    def run_of(self, name, repo="owner/repo", policy=None):
        run_dir = self.root / "runs" / name
        items.init_run(run_dir, name, repo, [str(self.backlog)], policy=policy)
        return run_dir

    def merged(self, run_dir, item_id, pr, merged_at, verify_at=None, verified=True):
        item = items.load_item(run_dir, item_id)
        item["status"] = "merged"
        item["merged_at"] = merged_at
        item["fields"].update(pr=str(pr), head_sha="c" * 40)
        if verify_at:
            item["fields"].update(verify="verified" if verified else "failed", verify_at=verify_at)
        items.save_item(run_dir, item)

    def facts(self, gh, cfg=None):
        return pace.facts({**pace.program.DEFAULTS, **PROGRAM, **(cfg or {})}, self.root, gh)


class ChecksTime(unittest.TestCase):
    def test_the_wall_time_runs_from_the_first_start_to_the_last_end_and_names_the_longest_check(self):
        timed = pace.checks_time(
            [
                check("lint", "2026-11-02T15:54:44Z", "2026-11-02T15:54:58Z"),
                check("full-ci", "2026-11-02T15:54:34Z", "2026-11-02T16:27:18Z"),
                check("secret-scan", "2026-11-02T15:54:18Z", "2026-11-02T15:54:37Z"),
            ]
        )
        self.assertEqual(timed, {"checks_minutes": 33.0, "slowest": "full-ci", "slowest_minutes": 32.7})

    def test_a_check_that_has_not_finished_gives_no_time(self):
        self.assertIsNone(pace.checks_time([check("full-ci", "2026-11-02T15:54:34Z", "0001-01-01T00:00:00Z")]))
        self.assertIsNone(pace.checks_time([check("full-ci", "2026-11-02T15:54:34Z", None)]))
        self.assertIsNone(pace.checks_time([]))

    def test_a_check_that_is_not_required_counts_and_a_skipped_one_does_not(self):
        self.assertEqual(pace.checks_time(CONTAINER_92), {"checks_minutes": 45.2, "slowest": "Full pytest suite", "slowest_minutes": 45.2})
        # A skipped check never holds the time back, though its stamps run backwards.
        self.assertIsNone(pace.checks_time([check("Change scope", "2026-11-02T21:14:17Z", "2026-11-02T21:14:09Z", bucket="skipping")]))
        self.assertIsNone(pace.checks_time([check("Change scope", "2026-11-02T21:14:17Z", "0001-01-01T00:00:00Z", bucket="skipping")]))


class Facts(PaceCase):
    def test_medians_of_the_last_five_merges_are_measured_against_the_budget(self):
        run = self.run_of("r1")
        answers = {}
        # Seven merges, an hour apart; the checks grow by ten minutes each time, the deploy takes 30.
        for n in range(1, 8):
            self.merged(run, f"T-{n}", 100 + n, f"2026-11-02T{n:02d}:00:00Z", verify_at=f"2026-11-02T{n:02d}:30:00Z")
            answers[("owner/repo", str(100 + n))] = [check("lint", at(60 * n), at(60 * n + 1)), check("tests", at(60 * n), at(60 * n + 10 * n))]
        gh = FakeGh(answers)
        out = self.facts(gh)
        repo = out["repos"]["owner/repo"]
        # The last five are T-7..T-3: 70, 60, 50, 40, 30 minutes of checks.
        self.assertEqual((repo["items"], repo["checks_minutes"], repo["slowest"], repo["verify_minutes"]), (5, 50.0, "tests", 30.0))
        self.assertEqual((repo["over"], out["over_budget"], out["budget_minutes"]), (["checks", "verify"], ["owner/repo"], {"checks": 15, "verify": 20}))
        self.assertEqual(repo["latest"], {"id": "T-7", "merged_at": "2026-11-02T07:00:00Z", "verify_minutes": 30.0, "checks_minutes": 70.0, "slowest": "tests"})
        self.assertEqual(sorted(c[2] for c in gh.calls), ["103", "104", "105", "106", "107"])
        self.assertEqual(out["errors"], [])

    def test_a_repository_under_budget_is_not_listed_and_the_program_may_set_its_own(self):
        run = self.run_of("r1")
        for n in (1, 2, 3):
            self.merged(run, f"T-{n}", n, f"2026-11-02T0{n}:00:00Z", verify_at=f"2026-11-02T0{n}:10:00Z")
        gh = FakeGh({("owner/repo", str(n)): [check("tests", at(60 * n), at(60 * n + 12))] for n in (1, 2, 3)})
        out = self.facts(gh)
        self.assertEqual((out["repos"]["owner/repo"]["over"], out["over_budget"]), ([], []))
        out = self.facts(gh, {"ci_budget_minutes": {"checks": 10}})
        self.assertEqual((out["budget_minutes"], out["repos"]["owner/repo"]["over"], out["over_budget"]), ({"checks": 10, "verify": 20}, ["checks"], ["owner/repo"]))

    def test_fewer_than_three_measured_merges_are_reported_but_never_over_budget(self):
        run = self.run_of("r1")
        for n in (1, 2):
            self.merged(run, f"T-{n}", n, f"2026-11-02T0{n}:00:00Z", verify_at=f"2026-11-02T0{n}:50:00Z")
        gh = FakeGh({("owner/repo", str(n)): [check("secret-scan", at(60 * n), at(60 * n + 58))] for n in (1, 2)})
        out = self.facts(gh)
        repo = out["repos"]["owner/repo"]
        self.assertEqual((repo["checks_minutes"], repo["verify_minutes"], repo["over"], out["over_budget"]), (58.0, 50.0, [], []))

    def test_a_merged_items_checks_are_read_once_and_kept_in_its_folder(self):
        run = self.run_of("r1")
        self.merged(run, "T-1", 7, "2026-11-02T01:00:00Z")
        gh = FakeGh({("owner/repo", "7"): [check("tests", "2026-11-02T00:00:00Z", "2026-11-02T00:20:00Z")]})
        self.facts(gh)
        self.facts(gh)
        self.assertEqual(len(gh.calls), 1)
        kept = read_json(items.item_dir(run, "T-1") / pace.CACHE)
        self.assertEqual((kept["pr"], kept["checks_minutes"], kept["head_sha"], kept["scope"]), ("7", 20.0, "c" * 40, "all"))

    def test_a_repository_whose_slow_check_is_not_required_goes_over_budget(self):
        run = self.run_of("r1", repo="owner/container-repo")
        for n in (1, 2, 3):
            self.merged(run, f"T-{n}", 90 + n, f"2026-11-02T2{n}:00:00Z")
        gh = FakeGh({("owner/container-repo", str(90 + n)): CONTAINER_92 for n in (1, 2, 3)})
        out = self.facts(gh)
        repo = out["repos"]["owner/container-repo"]
        self.assertEqual((repo["checks_minutes"], repo["slowest"], repo["over"], out["over_budget"]), (45.2, "Full pytest suite", ["checks"], ["owner/container-repo"]))
        self.assertEqual(out["errors"], [])

    def test_a_read_kept_from_the_required_checks_alone_is_read_again_once(self):
        run = self.run_of("r1", repo="owner/container-repo")
        self.merged(run, "T-1", 92, "2026-11-02T22:00:00Z")
        old = {"pr": "92", "head_sha": "c" * 40, "read_at": "2026-11-02T22:10:00Z", "checks_minutes": 2.1, "slowest": "Lint and Validate Syntax", "slowest_minutes": 2.1}
        write_json(items.item_dir(run, "T-1") / pace.CACHE, old)
        gh = FakeGh({("owner/container-repo", "92"): CONTAINER_92})
        out = self.facts(gh)
        self.assertEqual((out["repos"]["owner/container-repo"]["checks_minutes"], out["repos"]["owner/container-repo"]["latest"]["slowest"]), (45.2, "Full pytest suite"))
        self.facts(gh)
        self.assertEqual(len(gh.calls), 1)

    def test_a_read_kept_from_the_required_checks_alone_is_not_reported_once_the_budget_is_spent(self):
        run = self.run_of("r1")
        self.merged(run, "T-1", 7, "2026-11-02T01:00:00Z")
        write_json(items.item_dir(run, "T-1") / pace.CACHE, {"pr": "7", "checks_minutes": 2.0, "slowest": "lint"})
        ticks = iter([0, 100, 100, 100])
        out = pace.facts({**pace.program.DEFAULTS, **PROGRAM}, self.root, FakeGh({}), budget_s=30, clock=lambda: next(ticks))
        repo = out["repos"]["owner/repo"]
        self.assertEqual((repo["checks_minutes"], "slowest" in repo["latest"], out["errors"]), (None, False, ["owner/repo #7: not read within 30 s"]))

    def test_checks_still_running_are_asked_again_at_the_next_save(self):
        run = self.run_of("r1")
        self.merged(run, "T-1", 7, "2026-11-02T01:00:00Z")
        gh = FakeGh({("owner/repo", "7"): [check("tests", "2026-11-02T00:00:00Z", "0001-01-01T00:00:00Z")]})
        out = self.facts(gh)
        self.assertEqual((out["repos"]["owner/repo"]["checks_minutes"], (items.item_dir(run, "T-1") / pace.CACHE).exists()), (None, False))
        self.facts(gh)
        self.assertEqual(len(gh.calls), 2)

    def test_items_without_a_merge_or_a_pull_request_and_runs_on_a_train_are_left_out(self):
        run = self.run_of("r1")
        self.merged(run, "T-1", 1, "2026-11-02T01:00:00Z")
        item = items.load_item(run, "T-2")
        item["merged_at"] = "2026-11-02T02:00:00Z"  # merged, but no pull request recorded
        items.save_item(run, item)
        train = self.run_of("r2", repo="owner/trained", policy={"review": "code", "merge": "train", "train": {"branch": "hl-train/p", "item_checks": []}})
        self.merged(train, "T-1", 9, "2026-11-02T03:00:00Z")
        gh = FakeGh({("owner/repo", "1"): [check("tests", "2026-11-02T00:00:00Z", "2026-11-02T00:05:00Z")]})
        out = self.facts(gh)
        self.assertEqual((list(out["repos"]), out["repos"]["owner/repo"]["items"]), (["owner/repo"], 1))

    def test_a_run_set_aside_by_hand_counts_for_nothing(self):
        # `runs/.void-*`: a run voided and made again under its id would count its items twice.
        self.merged(self.run_of(".void-r1"), "T-1", 1, "2026-11-02T01:00:00Z")
        self.assertEqual(self.facts(FakeGh({}))["repos"], {})

    def test_a_verify_that_has_not_passed_counts_for_nothing(self):
        run = self.run_of("r1")
        self.merged(run, "T-1", 1, "2026-11-02T01:00:00Z", verify_at="2026-11-02T03:00:00Z", verified=False)
        out = self.facts(FakeGh({}))
        self.assertEqual((out["repos"]["owner/repo"]["verify_minutes"], out["repos"]["owner/repo"]["checks_minutes"]), (None, None))

    def test_one_repository_across_runs_is_one_entry_and_a_failed_read_is_said(self):
        first, second = self.run_of("r1"), self.run_of("r2")
        self.merged(first, "T-1", 1, "2026-11-02T01:00:00Z")
        self.merged(second, "T-1", 2, "2026-11-02T02:00:00Z")
        gh = FakeGh({("owner/repo", "1"): [check("tests", "2026-11-02T00:00:00Z", "2026-11-02T00:30:00Z")], ("owner/repo", "2"): "HTTP 502"})
        out = self.facts(gh)
        self.assertEqual((out["repos"]["owner/repo"]["items"], out["repos"]["owner/repo"]["checks_minutes"]), (2, 30.0))
        self.assertEqual(out["errors"], ["owner/repo #2: checks could not be read: HTTP 502"])

    def test_the_read_stops_at_its_budget_and_says_what_it_left(self):
        run = self.run_of("r1")
        for n in (1, 2):
            self.merged(run, f"T-{n}", n, f"2026-11-02T0{n}:00:00Z")
        ticks = iter([0, 0, 100, 100, 100, 100])
        gh = FakeGh({("owner/repo", "2"): [check("tests", "2026-11-02T00:00:00Z", "2026-11-02T00:05:00Z")]})
        out = pace.facts({**pace.program.DEFAULTS, **PROGRAM}, self.root, gh, budget_s=30, clock=lambda: next(ticks))
        self.assertEqual((len(gh.calls), out["errors"]), (1, ["owner/repo #1: not read within 30 s"]))
        # What is already kept is still counted after the budget is spent.
        ticks = iter([0, 100, 100, 100, 100, 100])
        out = pace.facts({**pace.program.DEFAULTS, **PROGRAM}, self.root, gh, budget_s=30, clock=lambda: next(ticks))
        self.assertEqual((len(gh.calls), out["repos"]["owner/repo"]["checks_minutes"], out["errors"]), (1, 5.0, ["owner/repo #1: not read within 30 s"]))


class FactsOrSaid(PaceCase):
    def test_without_the_loops_token_nothing_is_read(self):
        out = pace.facts_or_said(self.root, no_token="no token", gh=FakeGh({}))
        self.assertEqual(out, {"budget_minutes": {"checks": 15, "verify": 20}, "not_checked": "not read, because the loop's own token could not be had: no token"})

    def test_a_read_that_breaks_is_said(self):
        def broken(args, timeout=None):
            raise RuntimeError("gh exploded")

        run = self.run_of("r1")
        self.merged(run, "T-1", 1, "2026-11-02T01:00:00Z")
        self.assertEqual(pace.facts_or_said(self.root, gh=broken)["errors"], ["could not be read: gh exploded"])

    def test_a_program_json_that_cannot_be_read_is_said(self):
        (self.root / "program.json").write_text("{}")
        self.assertTrue(pace.facts_or_said(self.root, gh=FakeGh({}))["errors"][0].startswith("program.json could not be read: "))


if __name__ == "__main__":
    unittest.main()
