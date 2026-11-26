"""Three rules a review is held to, each loosened only where a program sets it (the velocity review of
the help-desk catalog program, 2026-11-19, change 3): a criterion that only limits the paths a change may write
(`write_list`), the text-fit check at every width a look's brief asks for (`text_fit_widths`), and a Serve line
that starts nothing for an item a person looks at (a repository's own `serve` command in the spec). Without the
setting, each holds as it did."""

from __future__ import annotations

import subprocess
from unittest import mock

from hl import contractfmt, items, merge, program, spawn
from hl.common import Refusal, read_json, write_json

from . import test_merge, test_review, test_serve
from .support import RunCase, SpawnCase

SERVE = {"command": "sh services/desk/run-dev.sh {port}", "ready": "http://127.0.0.1:{port}/healthz"}
WRITE_LIST = "**The paths the item may write are a note in this program, not a criterion.**"


def set_policy(run_dir, **options):
    run = read_json(run_dir / "run.json")
    write_json(run_dir / "run.json", {**run, "policy": {**(run.get("policy") or {}), **options}})


class OptionsTest(RunCase):
    def test_each_takes_its_two_values_and_a_run_records_one_only_when_it_is_on(self):
        self.assertEqual(items.check_policy({"review": "code"}), {"premise": "always", "review": "code", "bounds": {}})
        self.assertEqual(items.check_policy({"review": "code", "write_list": "criterion", "text_fit_widths": "counted"}), {"premise": "always", "review": "code", "bounds": {}})
        on = items.check_policy({"review": "code", "write_list": "note", "text_fit_widths": "recorded"})
        self.assertEqual((on["write_list"], on["text_fit_widths"]), ("note", "recorded"))
        for key, bad, why in (("write_list", "loose", "write_list 'loose' is not one of criterion, note"), ("text_fit_widths", 3, "text_fit_widths 3 is not one of counted, recorded")):
            with self.assertRaises(Refusal) as caught:
                items.check_policy({key: bad})
            self.assertIn(why, str(caught.exception))

    def test_a_run_made_without_them_reads_as_it_always_did(self):
        self.init()
        run = items.load_run(self.run_dir)
        self.assertEqual((items.option(run, "write_list"), items.option(run, "text_fit_widths"), items.options_on(run)), ("criterion", "counted", {}))
        self.assertNotIn("write_list", items.policy(run))

    def test_a_new_run_takes_them_from_the_programs_settings(self):
        cfg = {**program.DEFAULTS, "program": "p", "base": "main"}
        self.assertNotIn("write_list", program.run_policy(cfg, "owner/repo"))
        chosen = program.run_policy({**cfg, "write_list": "note", "text_fit_widths": "recorded"}, "owner/repo")
        self.assertEqual((chosen["write_list"], chosen["text_fit_widths"]), ("note", "recorded"))


class RuleBriefTest(SpawnCase):
    """`write_list: note`: the contract's drafter writes no criterion that only limits the paths, and a reviewer
    marks one `checks` and names each path written outside it for the manager. On the help-desk catalog program 15 of the
    62 reviews that did not pass in the week to 2026-11-19 were such a line, each sent to a correction."""

    def setUp(self):
        super().setUp()
        set_policy(self.run_dir, review="code")
        git = lambda *args: subprocess.run(["git", "-C", str(self.worktree), *args], check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
        git("init", "-q", "-b", "hl/run-1/A-1")
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git("config", key, value)
        (self.worktree / "page.html").write_text("<p>old</p>\n")
        git("add", ".")
        git("commit", "-q", "-m", "base")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        (self.worktree / "page.html").write_text("<p>new</p>\n")
        git("commit", "-q", "-am", "A-1: the change")
        # The briefs every program staged from a spec runs (`program.template.json`): the first set has no such line.
        settings = read_json(self.root / "program.json")
        write_json(self.root / "program.json", {**settings, "briefs": "v2"})
        self.start()
        self.finish_build()
        items.set_fields(self.run_dir, "A-1", [f"head_sha={git('rev-parse', 'HEAD')}"])
        write_json(self.item_dir() / "serve-1.json", {"summary": "http://127.0.0.1:8401/ answered 200 after 2 s."})

    def render(self, phase):
        return spawn.render_brief(self.root, self.run_dir, "A-1", phase, {}).read_text()

    def test_without_the_setting_both_briefs_say_what_they_said(self):
        review, contract = self.render("grade"), self.render("contract-draft")
        self.assertNotIn(WRITE_LIST, review)
        self.assertIn("- A builder changing only the files the item lets it write must be able to make every criterion pass.\n", contract)
        self.assertNotIn("{{", review + contract)

    def test_with_it_the_reviewer_takes_a_paths_criterion_as_checks_and_names_each_path_outside_it(self):
        set_policy(self.run_dir, write_list="note")
        review = self.render("grade")
        self.assertIn(WRITE_LIST, review)
        self.assertIn("(`git diff --name-only origin/main...HEAD` lists only …) is `checks`, whatever paths the diff touches", review)
        self.assertIn("write one finding of kind `criterion` with `criterion` set to `null`", review)
        self.assertNotIn("{{", review)

    def test_with_it_the_drafter_writes_no_criterion_that_only_limits_the_paths(self):
        set_policy(self.run_dir, write_list="note")
        contract = self.render("contract-draft")
        self.assertIn(
            "- A builder changing only the files the item lets it write must be able to make every criterion pass. The paths the item may write are its Scope's, "
            "and in this program no criterion limits them: leave out a line of the item that only does (`git diff --name-only origin/main...HEAD` lists only …).",
            contract,
        )
        self.assertNotIn("{{", contract)


    def test_the_build_brief_says_what_happens_to_a_serve_line_that_starts_nothing(self):
        items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        items.set_fields(self.run_dir, "A-1", ["port=8401"])
        # The next build is a rework of the first, and says why.
        (self.item_dir() / "rework-2.md").write_text("Make the heading bold.\n")
        sent_back = "For an item routed with a look, the Serve line must start the real server: an empty line, `true`, `:` or any other no-op is sent back, because a reviewer cannot look at a build that is not running. When nothing is served"
        self.assertIn(sent_back, self.render("build"))
        settings = read_json(self.root / "program.json")
        write_json(self.root / "program.json", {**settings, "repos": {"owner/repo": {"serve": SERVE}}})
        build = self.render("build")
        self.assertNotIn("is sent back, because a reviewer cannot look", build)
        self.assertIn("when your Serve line starts nothing (an empty line, `true`, `:`), `sh services/desk/run-dev.sh 8401` is served on port 8401 in its place, ready at `http://127.0.0.1:8401/healthz`", build)
        self.assertNotIn("{{", build)

    def test_a_looks_brief_says_whether_a_skipped_width_counts(self):
        items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        counted = self.render("grade")
        self.assertIn("one that did not run it at 1280, at 1440 or wider and at 1024 or narrower does not count toward the merge", counted)
        set_policy(self.run_dir, text_fit_widths="recorded")
        recorded = self.render("grade")
        self.assertNotIn("does not count toward the merge", recorded)
        self.assertIn("The widths the check answered (its `width`) are recorded with the review.", recorded)
        self.assertNotIn("{{", recorded)


class RecordedWidthsTest(test_review.CheckCase):
    """`text_fit_widths: recorded`: a review whose text-fit check skipped a width still counts, and the widths it
    answered are recorded. The rule came with the kit's change of 2026-11-18; the required text-fit check in CI stays."""

    PAGE = test_review.navigate("http://127.0.0.1:8401/app/alerts")

    def setUp(self):
        super().setUp()
        self.route(look=True)
        self.look(self.picture())
        (self.item_dir() / "grade-1.brief.md").write_text("# Review\n\npass the whole of `/kit/references/text-fit.js` as the `function` of `browser_evaluate`\n")

    def test_without_the_setting_the_review_does_not_count(self):
        out = self.check([self.PAGE, test_review.text_fit(1280)])
        self.assertEqual(out["text_fit_widths"], [1280])
        self.assertTrue(any("text-fit check answered at 1280 px only" in p for p in out["problems"]), out["problems"])

    def test_with_it_the_review_counts_and_the_widths_are_recorded(self):
        set_policy(self.run_dir, text_fit_widths="recorded")
        out = self.check([self.PAGE, test_review.text_fit(1280)])
        self.assertEqual((out["text_fit_widths"], out["problems"]), ([1280], []))
        self.assertEqual(out["text_fit_skipped"], ["at 1024 or narrower", "at 1440 or wider"])

    def test_a_value_that_is_not_one_of_the_settings_reads_as_counted(self):
        # run.json lies where a worker can write. A stray `Recorded` let the review count while its brief, which
        # reads the value another way, said a skipped width does not (the fresh review of change 3).
        set_policy(self.run_dir, text_fit_widths="Recorded")
        self.assertEqual(items.option(items.load_run(self.run_dir), "text_fit_widths"), "counted")
        out = self.check([self.PAGE, test_review.text_fit(1280)])
        self.assertTrue(any("text-fit check answered at 1280 px only" in p for p in out["problems"]), out["problems"])


class RecordedWidthsGateTest(test_merge.MergeCase):
    """`hl-merge` lets a review that skipped a width count only where the repository's own required text-fit
    check, named in the spec, is among the checks the merge holds green."""

    SKIPPED = {"problems": [], "text_fit_skipped": ["at 1440 or wider"]}

    def setUp(self):
        super().setUp()
        set_policy(self.run_dir, review="code", text_fit_widths="recorded")
        verdict_path = self.item_dir() / "verdict-1.json"
        write_json(verdict_path, {**read_json(verdict_path), "fix_list": []})
        # Read, as `hl-flow reviewed` leaves a review that did nothing to its checkout.
        items.record_read(self.run_dir, "A-1", 1, {"checkout": [], "dirty_before": []})

    def reasons(self, facts):
        with mock.patch("hl.review.check", return_value=self.SKIPPED):
            try:
                merge.gate(self.run_dir, "A-1", self.repo, self.tools, repo_facts=facts)
            except Refusal as refusal:
                return " | ".join(refusal.reasons)
        return ""

    def test_without_the_repositorys_check_named_a_review_that_skipped_a_width_does_not_merge(self):
        said = self.reasons({})
        self.assertIn("review 1's text-fit check did not answer at 1440 or wider", said)
        self.assertIn("the program's spec names no `text_fit_check` for owner/repo", said)

    def test_a_named_check_the_merge_does_not_hold_is_no_gate(self):
        self.assertIn("`full-ci / text-fit` is not among the checks this merge holds green (ci)", self.reasons({"text_fit_check": "full-ci / text-fit"}))

    def test_with_it_among_the_required_checks_the_review_counts(self):
        self.assertNotIn("text-fit", self.reasons({"text_fit_check": "ci"}))

    def test_a_run_that_counts_the_widths_is_not_asked_for_the_check(self):
        set_policy(self.run_dir, text_fit_widths="counted")
        self.assertNotIn("text_fit_check", self.reasons({}))


class ServingTest(RunCase):
    """`contractfmt.serving`: the repository's own serve command stands in for a Serve line that starts nothing,
    for an item routed with a look, on its port; anything else is served as its report says."""

    NOOP = {"serve": "true", "ready_kind": "COMMAND", "ready": "true"}

    def test_a_serve_line_that_starts_nothing_on_an_item_with_a_look_takes_the_repositorys(self):
        self.assertEqual(
            contractfmt.serving(self.NOOP, True, SERVE, "8412"),
            {"serve": "sh services/desk/run-dev.sh 8412", "ready_kind": "URL", "ready": "http://127.0.0.1:8412/healthz", "serve_default": True},
        )

    def test_a_line_that_starts_something_after_a_no_op_is_never_swapped(self):
        # `noop_serve` reads these as starting nothing; served in their place, the repository's command would run while
        # the reviewer is told the build's own line starts nothing (the fresh review of change 3).
        for line in ("echo starting; exec npm run dev -- --port 8412", "echo up & npm run dev", "true\nnpm run dev", "echo `npm run dev`"):
            self.assertTrue(contractfmt.noop_serve(line), line)
            how = {**self.NOOP, "serve": line}
            self.assertIs(contractfmt.serving(how, True, SERVE, "8412"), how, line)

    def test_anything_else_is_served_as_its_report_says(self):
        real = {"serve": "npm run start -- --port 8412", "ready_kind": "URL", "ready": "http://127.0.0.1:8412/"}
        for how, look, default, port in ((real, True, SERVE, "8412"), (self.NOOP, False, SERVE, "8412"), (self.NOOP, True, None, "8412"), (self.NOOP, True, SERVE, None)):
            self.assertIs(contractfmt.serving(how, look, default, port), how)


class ServeDefaultAfterBuildTest(test_merge.MergeCase):
    """`hl-merge` sends back a build routed with a look whose Serve line starts nothing, unless the repository's
    spec says how it serves: ten rebuilds in the week to 2026-11-19 changed only those lines."""

    def setUp(self):
        super().setUp()
        self.write("A-1", "build-1.done", "")
        self.set_gh(pr={"body": (self.item_dir() / "contract.md").read_text() + "\n**Serve.** `true`\n"})
        item = items.load_item(self.run_dir, "A-1")
        item["route"] = {"premise": "no", "look": True, "reason": "a page", "at": "now"}
        items.save_item(self.run_dir, item)
        self.write("A-1", "build-1.md", "**Serve.** `true`\n**Ready.** COMMAND `true`\n")

    def check(self, facts):
        return merge.after_build(self.run_dir, "A-1", self.repo, self.tools, repo_facts=facts)

    def test_without_a_serve_command_in_the_spec_the_build_goes_back(self):
        with self.assertRaises(merge.NotFinished) as caught:
            self.check({})
        self.assertIn("starts nothing", " | ".join(caught.exception.reasons))

    def test_with_one_the_build_is_built_and_served_with_it(self):
        items.set_fields(self.run_dir, "A-1", ["port=8412"])
        out = self.check({"serve": SERVE})
        self.assertEqual((out["result"], out["serve"], out["ready"], out["serve_default"]), ("built", "sh services/desk/run-dev.sh 8412", "http://127.0.0.1:8412/healthz", True))

    def test_a_no_op_followed_by_a_command_goes_back_as_before(self):
        items.set_fields(self.run_dir, "A-1", ["port=8412"])
        self.write("A-1", "build-1.md", "**Serve.** `echo starting; exec npm run dev -- --port 8412`\n**Ready.** URL `http://127.0.0.1:8412/`\n")
        with self.assertRaises(merge.NotFinished) as caught:
            self.check({"serve": SERVE})
        self.assertIn("starts nothing", " | ".join(caught.exception.reasons))

    def test_without_a_port_the_build_goes_back_as_before(self):
        with self.assertRaises(merge.NotFinished) as caught:
            self.check({"serve": SERVE})
        self.assertIn("starts nothing", " | ".join(caught.exception.reasons))


class ServeDefaultStartTest(test_serve.ServeCase):
    def setUp(self):
        super().setUp()
        self.built("**Serve.** `true`\n**Ready.** COMMAND `true`\n")
        settings = read_json(self.root / "program.json")
        write_json(self.root / "program.json", {**settings, "repos": {"owner/repo": {"serve": SERVE}}})

    def test_the_repositorys_serve_command_runs_on_the_items_port_and_the_record_says_so(self):
        items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        self.host.statuses = [200]
        code, result = self.start()
        self.assertEqual((code, result["result"], result["command"], result["ready"], result["serve_default"]), (0, "ready", "sh services/desk/run-dev.sh 8401", "http://127.0.0.1:8401/healthz", True))
        self.assertEqual((self.item_dir() / "serve-1.sh").read_text(), f"#!/bin/sh\ncd '{self.worktree}' || exit 97\nsh services/desk/run-dev.sh 8401\n")
        # The reviewer is told what was served, and why it is not the report's line.
        items.set_fields(self.run_dir, "A-1", ["pr=7", "pr_url=https://example.invalid/pull/7", "head_sha=" + "a" * 40])
        text = spawn.render_brief(self.root, self.run_dir, "A-1", "grade", {}).read_text()
        self.assertIn("**Serve.** `sh services/desk/run-dev.sh 8401`", text)
        self.assertIn("The repository's own serve command, from the program's spec: the build report's Serve line starts nothing.", text)
        # What a review is started with is what was served.
        self.assertEqual(spawn.served_as(self.run_dir, "A-1", program.load(self.root)), spawn.how_run_of("sh services/desk/run-dev.sh 8401", "URL", "http://127.0.0.1:8401/healthz"))

    def test_an_item_nobody_looks_at_still_serves_nothing(self):
        items.route_item(self.run_dir, "A-1", "independent", False, "a script")
        code, result = self.start()
        self.assertEqual((code, result["result"], result["served"]), (0, "ready", False))
        self.assertNotIn("serve_default", result)
