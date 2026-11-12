"""Floors, the run's policy, routes, and a builder's dispute before any
review: what the manager may choose, and what a script refuses."""

from __future__ import annotations

import contextlib
import io
import json

from hl import backlog, contractfmt, items
from hl.common import Refusal, read_json, write_json

from .support import RunCase

BACKLOG = """## R-1 — An ordinary item

**Scope.** Small and plain.

**Accept.**
1. One.
2. Two.

## R-2 — The shared sign-in page

**Scope.** Everyone signs in here.

**Accept.**
1. One.

**Floor.** look, premise: every product shares this page.

## R-3 — A billing change

**Scope.** Money.

**Accept.**
1. One.

**Floor.** sonnet.
"""

ROUTED = {"premise": "routed", "review": "code", "bounds": {"build": 3}}


class FloorParseTest(RunCase):
    def test_floors_are_read_in_a_fixed_order_and_the_reason_is_for_people(self):
        parsed = {it.id: it.floor for it in backlog.parse_backlog(BACKLOG)}
        self.assertEqual(parsed, {"R-1": [], "R-2": ["premise", "look"], "R-3": ["sonnet"]})

    def test_a_word_that_is_not_a_floor_is_refused_by_name(self):
        for line in ("**Floor.** premiss", "**Floor.** look and premise", "**Floor.**", "**Floor.** : because"):
            with self.assertRaisesRegex(backlog.BacklogError, r"R-9: malformed \*\*Floor\.\*\* line", msg=line):
                backlog.parse_backlog(f"## R-9 — x\n\n**Scope.** s\n\n**Accept.**\n1. a\n\n{line}\n")

    def test_a_repeated_floor_counts_once(self):
        text = "## R-9 — x\n\n**Scope.** s\n\n**Accept.**\n1. a\n\n**Floor.** Look, look, SONNET.\n"
        self.assertEqual(backlog.parse_backlog(text)[0].floor, ["look", "sonnet"])


class LintTest(RunCase):
    def remarks(self, *accept):
        lines = "\n".join(f"{n}. {text}" for n, text in enumerate(accept, start=1))
        return backlog.lint(backlog.parse_backlog(f"## L-1 — x\n\n**Scope.** s\n\n**Accept.**\n{lines}\n")[0])

    def test_criteria_that_restate_the_checks_or_a_whole_suite_are_remarked_on(self):
        remarks = self.remarks(
            "`cd Frontend/react && npm ci && npm run typecheck && npm run test && npm run build` exits 0.",
            "`gh pr checks <this PR> --required --watch --interval 30` exits 0.",
            "`.venv/bin/python -m pytest -q -n 8 --no-cov` exits 0 and its last line matches `<N> passed`.",
        )
        said = " | ".join(remarks)
        self.assertIn("L-1 criterion 1 runs a whole suite", said)
        self.assertIn("L-1 criterion 2 asks for the required checks", said)
        self.assertIn("L-1 criterion 2 waits on checks with --watch", said)
        self.assertIn("L-1 criterion 3 runs a whole suite", said)
        self.assertIn("L-1 criterion 3 expects a line of a test runner's summary", said)

    def test_criteria_that_state_behaviour_draw_no_remark(self):
        self.assertEqual(
            self.remarks(
                "`curl -s http://127.0.0.1:8406/ping` prints `pong`.",
                "At `/app/alerts`, `getComputedStyle(document.body).backgroundColor` is `rgb(13, 11, 18)` in dark mode.",
                "`python3 -m pytest tests/web/test_shell.py::test_title -q` exits 0.",
                "`grep -c 'backdropFilter' Frontend/react/src/providers/ShellLayout.tsx` prints `0`.",
                "`npx vitest run src/theme/roles.test.ts` exits 0.",
            ),
            [],
        )


class RouteCase(RunCase):
    POLICY = ROUTED

    def setUp(self):
        super().setUp()
        self.file_r = self.tmp / "R-ITEMS.md"
        self.file_r.write_text(BACKLOG)
        items.init_run(self.run_dir, "run-1", "owner/repo", [str(self.file_r)], policy=self.POLICY, stream="main")

    def route(self, item_id="R-1", premise="builder", look=False, reason="one file, mechanical"):
        return items.route_item(self.run_dir, item_id, premise, look, reason)

    def build(self, item_id="R-1", session="build-1", engine="claude", why="small: the cheaper builder first"):
        entry = items.start_phase(self.run_dir, item_id, "build", session=session, engine=engine, why=why)
        self.write(item_id, f"build-{entry['n']}.md", "**Serve.** `true`\n**Ready.** COMMAND `true`\n")
        return entry

    def refused(self, call, *args, **kwargs):
        with self.assertRaises(Refusal) as caught:
            call(*args, **kwargs)
        return str(caught.exception)


class PolicyTest(RouteCase):
    def test_a_run_keeps_the_policy_and_stream_it_was_created_with(self):
        run = items.load_run(self.run_dir)
        self.assertEqual((run["policy"], run["stream"]), (ROUTED, "main"))
        self.assertEqual(items.bounds(run)["build"], 3)
        self.assertEqual(items.bounds(run)["grade"], 2)
        # Initialising again, even with other settings, changes nothing: a run keeps the rules it started under.
        again = items.init_run(self.run_dir, "run-1", "owner/repo", [str(self.file_r)], policy={"premise": "always"})
        self.assertEqual(again["policy"], ROUTED)

    def test_a_run_without_a_policy_behaves_as_the_kit_first_did(self):
        other = self.tmp / "old-run"
        run = items.init_run(other, "old", "owner/repo", [str(self.file_r)])
        self.assertNotIn("policy", run)
        self.assertEqual(items.policy(run), {"premise": "always", "review": "blind", "bounds": {}, "merge": "item", "train": {}, "confirm": "full", "confirm_bound": {}, "exemptable": []})
        self.assertEqual(items.bounds(run), items.BOUNDS)

    def test_how_often_a_pass_may_be_given_back_is_a_bound_of_the_run_like_the_others(self):
        self.assertEqual(items.bounds(items.load_run(self.run_dir))["give_back"], 1)
        other = self.tmp / "twice"
        items.init_run(other, "twice", "owner/repo", [str(self.file_r)], policy={"bounds": {"give_back": 2}})
        self.assertEqual(items.bounds(items.load_run(other))["give_back"], 2)
        # It is counted from the attempt log, so no item starts with a number for it.
        self.assertEqual(sorted(items.load_attempts(other, "R-1")), ["agree", "build", "ci_rerun", "correction", "grade", "log", "verify"])
        with self.assertRaises(Refusal) as caught:
            items.init_run(self.tmp / "never", "never", "owner/repo", [str(self.file_r)], policy={"bounds": {"give_back": 0}})
        self.assertEqual(str(caught.exception), "bounds must be whole numbers of at least 1 for agree, build, grade, correction, ci_rerun, verify, give_back; got give_back=0")

    def test_a_policy_that_makes_no_sense_is_refused(self):
        for bad in ({"premise": "sometimes"}, {"review": "skim"}, {"bounds": {"build": 0}}, {"bounds": {"builds": 3}}, {"bounds": {"build": "3"}}, {"bounds": {"build": True}}):
            with self.assertRaises(Refusal, msg=bad):
                items.init_run(self.tmp / "bad", "bad", "owner/repo", [str(self.file_r)], policy=bad)
            self.assertFalse((self.tmp / "bad" / "run.json").exists())

    def test_the_build_bound_is_the_runs_own(self):
        self.route()
        items.contract_from_accept(self.run_dir, "R-1")
        for n in (1, 2, 3):
            self.assertEqual(self.build(session=f"build-{n}")["n"], n)
        self.assertIn("build bound reached for R-1: 3 of 3", self.refused(self.build, session="build-4"))

    def test_init_through_the_cli_takes_the_policy_from_the_programs_settings(self):
        root = self.tmp / "root"
        root.mkdir()
        settings = {"program": "p", "base": "main", "worker_claude_ref": "op://a/b/c", "gh_token_ref": "op://a/b/c", "cursor_model": "m"}
        write_json(root / "program.json", {**settings, "premise": "routed", "review": "code", "bounds": {"build": 3}})
        run_dir = root / "runs" / "next"
        argv = ["--run-dir", str(run_dir), "init", "--run-id", "next", "--repo", "o/r", "--file", str(self.file_r), "--root", str(root), "--stream", "site"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(items.main(argv), 0)
        said = json.loads(out.getvalue())
        self.assertEqual((said["policy"], said["stream"]), ({**ROUTED, "merge": "item", "train": {}, "confirm": "full", "confirm_bound": {}, "exemptable": []}, "site"))
        # Settings that say nothing leave a new run on the first behaviour.
        write_json(root / "program.json", settings)
        with contextlib.redirect_stdout(io.StringIO()):
            items.main(["--run-dir", str(root / "runs" / "plain"), "init", "--run-id", "plain", "--repo", "o/r", "--file", str(self.file_r), "--root", str(root)])
        self.assertEqual(items.policy(items.load_run(root / "runs" / "plain")), {"premise": "always", "review": "blind", "bounds": {}, "merge": "item", "train": {}, "confirm": "full", "confirm_bound": {}, "exemptable": []})


class RouteTest(RouteCase):
    def test_a_route_is_recorded_with_its_reason(self):
        item = self.route(look=True, reason="restyles the alerts page")
        self.assertEqual({k: item["route"][k] for k in ("premise", "look", "reason")}, {"premise": "builder", "look": True, "reason": "restyles the alerts page"})
        self.assertEqual(items.load_item(self.run_dir, "R-1")["floor"], [])

    def test_a_route_needs_a_reason(self):
        self.assertIn("--reason is required", self.refused(self.route, reason="  "))
        self.assertIsNone(items.load_item(self.run_dir, "R-1")["route"])

    def test_a_route_below_the_items_floor_is_refused_and_names_each_shortfall(self):
        said = self.refused(self.route, "R-2", "builder", False, "looks small")
        self.assertIn("the item's floor asks for an independent premise check", said)
        self.assertIn("the item's floor asks for a look in a real browser", said)
        self.assertIsNone(items.load_item(self.run_dir, "R-2")["route"])
        self.assertIn("independent premise check", self.refused(self.route, "R-2", "builder", True, "x"))
        self.assertIn("look in a real browser", self.refused(self.route, "R-2", "independent", False, "x"))

    def test_a_route_at_or_above_the_floor_is_taken(self):
        self.assertEqual(self.route("R-2", "independent", True, "floored")["route"]["premise"], "independent")
        # Above a floor that asks for nothing: the manager's call.
        self.assertTrue(self.route("R-1", "independent", True, "the criteria read loosely")["route"]["look"])

    def test_routing_again_keeps_what_was_there(self):
        self.route(reason="first thought")
        item = self.route(premise="independent", look=True, reason="the diff touched a template")
        self.assertEqual(item["route"]["reason"], "the diff touched a template")
        self.assertEqual([r["reason"] for r in item["route_history"]], ["first thought"])

    def test_a_look_is_not_dropped_once_the_item_is_being_built(self):
        # Found in review: a review that did not look could be made good afterwards by routing the look away.
        self.route(look=True, reason="a page")
        self.assertFalse(self.route(look=False, reason="on second thought")["route"]["look"])
        self.route(look=True, reason="a page after all")
        items.contract_from_accept(self.run_dir, "R-1")
        self.build()
        self.assertIn("is routed with a look and is already being built", self.refused(self.route, look=False, reason="the review did not look"))
        self.assertTrue(items.load_item(self.run_dir, "R-1")["route"]["look"])
        # Going up is always allowed, and so is a look that was never routed.
        self.assertEqual(self.route(premise="independent", look=True, reason="more care")["route"]["premise"], "independent")

    def test_a_build_that_was_voided_is_not_being_built(self):
        self.route(look=True, reason="a page")
        items.contract_from_accept(self.run_dir, "R-1")
        items.start_phase(self.run_dir, "R-1", "build", session="b-1", engine="claude", why="x")
        items.void_attempt(self.run_dir, "R-1", "build", None, "pane went away")
        self.assertFalse(self.route(look=False, reason="not a page")["route"]["look"])

    def test_an_item_that_has_ended_is_not_routed(self):
        items.set_status(self.run_dir, "R-1", "skipped", "not needed")
        self.assertIn("R-1 is skipped", self.refused(self.route))

    def test_the_command_line_says_the_route_and_the_floor(self):
        out = io.StringIO()
        argv = ["--run-dir", str(self.run_dir), "route", "--id", "R-2", "--premise", "independent", "--look", "yes", "--reason", "floored"]
        with contextlib.redirect_stdout(out):
            self.assertEqual(items.main(argv), 0)
        said = json.loads(out.getvalue())
        self.assertEqual((said["floor"], said["route"]["look"]), (["premise", "look"], True))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(items.main(argv[:6] + ["builder", "--look", "no", "--reason", "x"]), 2)
        self.assertEqual(len(json.loads(out.getvalue())["refused"]), 2)


class RoutedBuildTest(RouteCase):
    def test_an_item_nobody_routed_is_not_built(self):
        items.contract_from_accept(self.run_dir, "R-1")
        said = self.refused(self.build)
        self.assertIn("cannot build R-1: it has no route", said)
        self.assertIn("hl-items route --id R-1", said)
        self.assertEqual(items.load_attempts(self.run_dir, "R-1")["build"], 0)

    def test_a_builder_routed_item_builds_from_its_accept_list_with_no_agree_round(self):
        self.route()
        self.assertIn("no contract installed", self.refused(self.build))
        items.contract_from_accept(self.run_dir, "R-1")
        entry = self.build()
        self.assertEqual((entry["n"], entry["why"]), (1, "small: the cheaper builder first"))
        self.assertEqual(items.load_attempts(self.run_dir, "R-1")["agree"], 0)

    def test_an_independently_routed_item_waits_for_its_premise_check(self):
        self.route(premise="independent", reason="criteria read loosely")
        items.contract_from_accept(self.run_dir, "R-1")
        self.assertIn("no agree round has run", self.refused(self.build))
        entry = items.start_phase(self.run_dir, "R-1", "agree", session="eval-a")
        self.write("R-1", f"agree-{entry['n']}.md", "Checked.\n\nAGREED\n")
        self.assertEqual(self.build()["n"], 1)

    def test_a_floored_item_waits_for_its_premise_check_even_if_its_route_was_edited_by_hand(self):
        self.route("R-2", "independent", True, "floored")
        items.contract_from_accept(self.run_dir, "R-2")
        item = items.load_item(self.run_dir, "R-2")
        item["route"]["premise"] = "builder"
        items.save_item(self.run_dir, item)
        self.assertIn("no agree round has run", self.refused(self.build, "R-2"))

    def test_a_build_in_a_routed_run_says_why_this_builder(self):
        self.route()
        items.contract_from_accept(self.run_dir, "R-1")
        self.assertIn("say in a line why this builder and effort", self.refused(self.build, why=" "))
        self.assertEqual(items.load_attempts(self.run_dir, "R-1")["build"], 0)

    def test_a_sonnet_floor_refuses_a_cursor_builder(self):
        self.route("R-3", reason="billing")
        items.contract_from_accept(self.run_dir, "R-3")
        self.assertIn("the item's floor asks for a Sonnet builder", self.refused(self.build, "R-3", engine="cursor"))
        self.assertEqual(self.build("R-3", engine="claude")["engine"], "claude")
        # An item with no such floor takes either.
        self.route("R-1")
        items.contract_from_accept(self.run_dir, "R-1")
        self.assertEqual(self.build("R-1", engine="cursor")["engine"], "cursor")


class AlwaysPolicyTest(RouteCase):
    POLICY = {"premise": "always"}

    def test_the_first_behaviour_is_a_blind_grade(self):
        self.assertEqual(items.policy(items.load_run(self.run_dir))["review"], "blind")

    def test_every_item_keeps_its_premise_check_and_needs_no_route(self):
        items.contract_from_accept(self.run_dir, "R-1")
        self.assertIn("no agree round has run", self.refused(self.build, why=None))
        entry = items.start_phase(self.run_dir, "R-1", "agree", session="eval-a")
        self.write("R-1", f"agree-{entry['n']}.md", "Checked.\n\nAGREED\n")
        self.assertEqual(self.build(why=None)["n"], 1)

    def test_a_builder_premise_is_not_a_route_this_run_takes(self):
        self.assertIn("this run gives every item an independent premise check", self.refused(self.route))
        self.assertEqual(self.route(premise="independent", look=True)["route"]["look"], True)

    def test_a_dispute_does_not_open_a_correction_before_a_grade(self):
        items.contract_from_accept(self.run_dir, "R-1")
        entry = items.start_phase(self.run_dir, "R-1", "agree", session="eval-a")
        self.write("R-1", f"agree-{entry['n']}.md", "Checked.\n\nAGREED\n")
        self.build(why=None)
        self.write("R-1", "build-1.md", "**Serve.** `true`\n\n## Disputed criterion\n\nCriterion 2: the item pins another value.\n")
        said = self.refused(items.start_phase, self.run_dir, "R-1", "correction", session="eval-c")
        self.assertEqual(said, "cannot correct R-1: nothing has been graded")


class DisputeTest(RouteCase):
    def setUp(self):
        super().setUp()
        self.route()
        items.contract_from_accept(self.run_dir, "R-1")
        self.build()

    def dispute(self, body):
        self.write("R-1", "build-1.md", f"**Serve.** `true`\n**Ready.** COMMAND `true`\n\n## Proposed items\n\nNone\n\n## Disputed criterion\n\n{body}\n")

    def test_a_builders_dispute_opens_a_correction_before_any_review(self):
        self.dispute("Criterion 2: the item's table gives `abc`; the criterion says `abd`. `grep -c abc table.md` prints 1.")
        self.assertEqual(items.disputed_criteria(self.run_dir, "R-1"), [2])
        entry = items.start_phase(self.run_dir, "R-1", "correction", session="eval-c")
        self.assertEqual((entry["n"], entry["build"]), (1, 1))
        # One correction is the bound, as before.
        self.assertIn("correction bound reached", self.refused(items.start_phase, self.run_dir, "R-1", "correction", session="eval-d"))

    def test_without_a_dispute_a_correction_still_needs_a_grade(self):
        said = self.refused(items.start_phase, self.run_dir, "R-1", "correction", session="eval-c")
        self.assertIn("nothing has been graded and its latest build report disputes no criterion", said)

    def test_a_dispute_section_that_names_no_criterion_is_not_a_dispute(self):
        self.dispute("I am not sure about the second one.")
        self.assertEqual(items.disputed_criteria(self.run_dir, "R-1"), [])
        self.refused(items.start_phase, self.run_dir, "R-1", "correction", session="eval-c")

    def test_an_older_builds_dispute_is_not_the_latest_builds(self):
        self.dispute("Criterion 1: wrong.")
        self.build(session="build-2")
        self.assertEqual(items.disputed_criteria(self.run_dir, "R-1"), [])

    def test_the_session_that_built_cannot_rule_on_its_own_dispute(self):
        self.dispute("Criterion 1: wrong.")
        self.assertIn("has built or drafted", self.refused(items.start_phase, self.run_dir, "R-1", "correction", session="build-1"))

    def test_disputed_criteria_are_read_in_the_shapes_a_builder_writes(self):
        cases = {
            "Criterion 2: x": [2],
            "- Criterion 3: x\n- criterion 1 is stale": [3, 1],
            "**Criterion 4.** x\n\nCriterion 4 again": [4],
            "The criterion 2 is wrong": [],
            "None": [],
        }
        for body, expected in cases.items():
            self.assertEqual(contractfmt.disputed(f"# report\n\n## Disputed criterion\n\n{body}\n\n## Other\n\nCriterion 9: not in the section\n"), expected, body)
        self.assertEqual(contractfmt.disputed("# report\n\nCriterion 2: no heading\n"), [])
