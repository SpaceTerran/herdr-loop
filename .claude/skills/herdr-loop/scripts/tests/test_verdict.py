from __future__ import annotations

import json

from hl import items as items_mod, verdict

from .support import RunCase


class VerdictTest(RunCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.agree()
        self.build()

    def check(self, n=1, **kw):
        return verdict.check(self.item_dir(), n, **kw)

    def rewrite(self, mutate, n=1):
        path = self.item_dir() / f"verdict-{n}.json"
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data))

    def test_a_clean_pass_is_a_pass(self):
        self.grade()
        result = self.check()
        self.assertEqual((result["problems"], result["effective"]), ([], "PASS"))

    def test_a_pass_with_a_criterion_not_checked_is_inconclusive(self):
        self.grade(results=["pass", "not_checked"])
        result = self.check()
        self.assertEqual(result["effective"], "INCONCLUSIVE")
        self.assertIn("[2] are not_checked", result["downgraded"])

    def test_a_pass_with_a_failed_criterion_is_a_fail(self):
        self.grade(results=["pass", "fail"])
        self.assertEqual(self.check()["effective"], "FAIL")

    def test_a_fail_that_names_no_failed_criterion_is_inconclusive(self):
        self.grade(verdict="FAIL")
        self.assertEqual(self.check()["effective"], "INCONCLUSIVE")

    def test_a_missing_file_is_a_problem_not_a_crash(self):
        result = self.check()
        self.assertEqual(result["effective"], None)
        self.assertIn("verdict-1.json does not exist", result["problems"][0])

    def test_text_that_is_not_json_is_reported(self):
        self.write("A-1", "verdict-1.json", "PASS, all good")
        self.assertIn("not valid JSON", self.check()["problems"][0])

    def test_every_shape_problem_is_reported_at_once(self):
        self.grade()
        self.rewrite(lambda d: (d.update(verdict="pass", how_run=""), d["criteria"][0].pop("note")))
        problems = self.check()["problems"]
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("verdict must be one of" in p for p in problems))
        self.assertTrue(any("criterion 1 is missing a note" in p for p in problems))
        self.assertTrue(any("how_run" in p for p in problems))

    def test_criteria_must_match_the_contracts_numbers(self):
        self.grade()
        self.rewrite(lambda d: d["criteria"].pop())
        self.assertIn("exactly [1, 2]", self.check()["problems"][0])

    def test_cited_evidence_must_exist_inside_the_item_directory(self):
        self.grade()
        self.rewrite(lambda d: d["criteria"][0].update(evidence=["evidence-1/shot.png", "../../run.json"]))
        problems = self.check()["problems"]
        self.assertTrue(any("'evidence-1/shot.png', which does not exist" in p for p in problems))
        self.assertTrue(any("outside the item directory" in p for p in problems))
        self.write("A-1", "evidence-1/shot.png", "png")
        self.rewrite(lambda d: d["criteria"][0].update(evidence=["evidence-1/shot.png"]))
        self.assertEqual(self.check()["problems"], [])

    def test_a_directory_cited_as_evidence_is_refused_as_what_it_is(self):
        # A reviewer in a help-desk feature program cited `evidence-1/previews/shots` (116 pictures) and was told only
        # that it "does not exist".
        self.grade()
        self.write("A-1", "evidence-1/shots/home-dark.png", "png")
        self.rewrite(lambda d: d["criteria"][0].update(evidence=["evidence-1/shots"]))
        self.assertEqual(self.check()["problems"], ["criterion 1 cites 'evidence-1/shots', which is a directory: an evidence entry names one file, so list the files in it"])
        self.rewrite(lambda d: d["criteria"][0].update(evidence=["evidence-1/shots/home-dark.png"]))
        self.assertEqual(self.check()["problems"], [])

    def test_a_verdict_written_without_driving_the_build_is_refused(self):
        self.grade()
        transcript = self.tmp / "session.jsonl"
        read_only = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}
        transcript.write_text(json.dumps(read_only) + "\nnot json\n")
        result = self.check(transcript=transcript)
        self.assertIn("was not driven", result["problems"][0])
        self.assertEqual(result["effective"], None)

    def test_a_shell_or_browser_call_counts_as_driving(self):
        self.grade()
        transcript = self.tmp / "session.jsonl"
        lines = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__playwright__browser_click"}]}},
            {"type": "user", "message": {"content": "plain text"}},
        ]
        transcript.write_text("\n".join(json.dumps(line) for line in lines))
        result = self.check(transcript=transcript)
        self.assertEqual((result["driving_calls"], result["effective"]), (2, "PASS"))

    def test_the_cli_exits_2_on_problems_and_0_on_a_usable_verdict(self):
        import contextlib
        import io

        argv = ["--item-dir", str(self.item_dir()), "--n", "1"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verdict.main(argv), 2)
            self.grade(verdict="FAIL", results=["fail", "pass"])
            self.assertEqual(verdict.main(argv), 0)


class ReviewVerdictTest(RunCase):
    """A review's verdict: a fix list the builder acts on, the looks it took, and criteria left to the required checks."""

    def setUp(self):
        super().setUp()
        self.init()
        self.agree()
        self.build()

    def verdict(self, word="PASS", results=("pass", "pass"), **extra):
        self.grade(verdict=word, results=list(results))
        path = self.item_dir() / "verdict-1.json"
        data = json.loads(path.read_text())
        data.update({"fix_list": [], **extra})
        path.write_text(json.dumps(data))
        return verdict.check(self.item_dir(), 1, style="code")

    def fix(self, **over):
        return {"criterion": 2, "what": "the count is 3, the item asks for 4", "where": "src/app.py:41", "evidence": [], **over}

    def look(self, name="evidence-1/alerts-dark.png", result="match"):
        self.write("A-1", name, "png")
        return {"mode": "dark", "url": "http://127.0.0.1:8401/app/alerts", "screenshot": name, "result": result, "note": "against the reference"}

    def test_a_clean_review_is_a_pass_with_an_empty_fix_list(self):
        result = self.verdict()
        self.assertEqual((result["problems"], result["effective"], result["fix_list"], result["by_checks"]), ([], "PASS", 0, []))

    def test_a_verdict_without_a_fix_list_is_not_a_reviews_verdict(self):
        self.grade()
        result = verdict.check(self.item_dir(), 1, style="code")
        self.assertEqual(result["problems"], ["fix_list must be a list (empty on a PASS)"])
        # The same file is a usable grade.
        self.assertEqual(verdict.check(self.item_dir(), 1)["effective"], "PASS")

    def test_a_fail_carries_what_the_builder_must_change(self):
        result = self.verdict("FAIL", ("pass", "fail"), fix_list=[self.fix()])
        self.assertEqual((result["problems"], result["effective"], result["fix_list"]), ([], "FAIL", 1))
        empty = self.verdict_again("FAIL", ("pass", "fail"))
        self.assertEqual(empty["problems"], ["a FAIL needs a fix list: say what the builder must change"])

    def verdict_again(self, word, results, **extra):
        (self.item_dir() / "verdict-1.json").unlink()
        attempts = items_mod.load_attempts(self.run_dir, "A-1")
        attempts["grade"] -= 1
        attempts["log"] = [e for e in attempts["log"] if e["phase"] != "grade"]
        items_mod.save_attempts(self.run_dir, "A-1", attempts)
        return self.verdict(word, results, **extra)

    def test_a_defect_no_criterion_names_fails_the_review_by_the_fix_list_alone(self):
        result = self.verdict("FAIL", ("pass", "pass"), fix_list=[self.fix(criterion=None, what="the new test asserts nothing: it was rewritten to skip")])
        self.assertEqual((result["problems"], result["effective"]), ([], "FAIL"))

    def test_a_pass_with_a_fix_list_is_a_fail(self):
        result = self.verdict("PASS", ("pass", "pass"), fix_list=[self.fix()])
        self.assertEqual(result["effective"], "FAIL")
        self.assertIn("the fix list has 1 entries", result["downgraded"])

    def test_a_pass_with_a_look_that_differs_is_a_fail(self):
        result = self.verdict(looks=[self.look(), self.look("evidence-1/alerts-light.png", "differs")])
        self.assertEqual(result["effective"], "FAIL")
        self.assertIn("a look differs from its reference", result["downgraded"])

    def test_a_criterion_left_to_the_required_checks_counts_as_passed(self):
        result = self.verdict(results=("pass", "checks"))
        self.assertEqual((result["problems"], result["effective"], result["by_checks"]), ([], "PASS", [2]))
        # A grade has no such result.
        self.assertIn("criterion 2 has result 'checks'; use pass, fail or not_checked", verdict.check(self.item_dir(), 1)["problems"][0])

    def test_a_review_that_left_every_criterion_to_the_checks_exercised_nothing(self):
        result = self.verdict(results=("checks", "checks"))
        self.assertEqual(result["effective"], "INCONCLUSIVE")
        self.assertIn("every criterion is left to the required checks", result["downgraded"])

    def test_a_change_a_person_sees_with_no_look_recorded_is_inconclusive(self):
        result = self.verdict(look_needed=True)
        self.assertEqual(result["effective"], "INCONCLUSIVE")
        self.assertIn("records no look", result["downgraded"])
        looked = self.verdict_again("PASS", ("pass", "pass"), look_needed=True, looks=[self.look()])
        self.assertEqual((looked["problems"], looked["effective"]), ([], "PASS"))

    def test_the_review_parts_are_checked_for_shape_all_at_once(self):
        bad_fixes = [{"criterion": "two", "what": " ", "evidence": "x"}, "fix it"]
        bad_looks = [{"mode": "dark", "result": "close enough"}, 7]
        result = self.verdict("FAIL", ("pass", "fail"), fix_list=bad_fixes, looks=bad_looks, look_needed="yes")
        said = " | ".join(result["problems"])
        for part in (
            "a fix-list entry is missing `what`",
            "has criterion 'two'",
            "a fix-list entry's evidence must be a list",
            "each fix-list entry must be an object",
            "a look is missing `url`",
            "a look is missing `screenshot`",
            "a look has result 'close enough'; use match or differs",
            "each look must be an object",
            "look_needed must be true or false",
        ):
            self.assertIn(part, said)

    def finding(self, **over):
        return {"kind": "environment", "severity": "low", "confidence": 0.8, "summary": "the mock API answers 404 for /users/me/orgs", "evidence": [], **over}

    def test_a_reviews_finding_says_what_kind_it_is(self):
        result = self.verdict(findings=[self.finding(), self.finding(kind="pre-existing"), self.finding(kind="design")])
        self.assertEqual((result["problems"], result["effective"], result["criteria_in_question"]), ([], "PASS", []))
        for kind in (None, "defect", "blocks"):
            result = self.verdict_again("PASS", ("pass", "pass"), findings=[self.finding(kind=kind)])
            self.assertIn(f"a finding has kind {kind!r}; use one of pre-existing, other-files, environment, design, criterion", result["problems"])
        # A grade's findings have no kind, and need none.
        self.assertEqual(verdict.check(self.item_dir(), 1)["problems"], [])

    def test_a_criterion_the_reviewer_says_is_wrong_is_named_for_the_correction(self):
        wrong = self.finding(kind="criterion", criterion=2, summary="the item pins `abc`; the criterion says `abd`. Corrected: prints `abc`.")
        result = self.verdict("INCONCLUSIVE", ("pass", "not_checked"), findings=[wrong, self.finding()])
        self.assertEqual((result["problems"], result["effective"], result["criteria_in_question"]), ([], "INCONCLUSIVE", [2]))
        # A number that is not a criterion's number is still refused: it is neither a correction nor a note.
        for given in ("2", "criterion 2", True, 2.0):
            bad = self.verdict_again("INCONCLUSIVE", ("pass", "not_checked"), findings=[self.finding(kind="criterion", criterion=given)])
            self.assertIn(f"a finding of kind criterion has criterion {given!r}: give the criterion's number, or null", bad["problems"][0])

    def test_a_criterion_finding_that_names_no_criterion_is_a_note_and_the_verdict_stands(self):
        # An item of a help-desk feature program: the item's prose pinned pixel values the build proved wrong, the
        # reviewer passed every criterion and filed that as a `criterion` finding with no number, and the whole PASS
        # was refused, twice. Two more items did the same after a correction, comparing the Accept text with the
        # contract that replaced it.
        about_prose = self.finding(kind="criterion", summary="the Scope pins the arc top at y=12; the build draws it at y=14, as the reference does")
        for note in (about_prose, {**about_prose, "criterion": None}):
            if note is about_prose:
                result = self.verdict(findings=[note])
            else:
                result = self.verdict_again("PASS", ("pass", "pass"), findings=[note])
            self.assertEqual((result["problems"], result["effective"], result["criteria_in_question"], result["criterion_notes"]), ([], "PASS", [], 1))
        # Beside a numbered one, only the number goes to a correction.
        wrong = self.finding(kind="criterion", criterion=2, summary="the criterion says `abd`; the item pins `abc`")
        both = self.verdict_again("INCONCLUSIVE", ("pass", "not_checked"), findings=[wrong, about_prose])
        self.assertEqual((both["problems"], both["effective"], both["criteria_in_question"], both["criterion_notes"]), ([], "INCONCLUSIVE", [2], 1))

    def test_a_fix_for_a_criterion_the_contract_does_not_have_is_a_problem(self):
        result = self.verdict("FAIL", ("pass", "fail"), fix_list=[self.fix(criterion=9)])
        self.assertIn("a fix-list entry names criterion 9, which the contract does not have", result["problems"])

    def test_a_looks_picture_and_a_fixs_evidence_must_exist(self):
        look = self.look()
        (self.item_dir() / look["screenshot"]).unlink()
        result = self.verdict("FAIL", ("pass", "fail"), fix_list=[self.fix(evidence=["evidence-1/missing.txt"])], looks=[look])
        self.assertIn("a fix-list entry cites 'evidence-1/missing.txt', which does not exist", result["problems"])
        self.assertIn("a look cites 'evidence-1/alerts-dark.png', which does not exist", result["problems"])

    def test_the_command_line_takes_the_style_from_the_run(self):
        import contextlib
        import io

        self.verdict()
        run = json.loads((self.run_dir / "run.json").read_text())
        for style, expected in (("code", 0), ("blind", 0)):
            run["policy"] = {"premise": "always", "review": style}
            (self.run_dir / "run.json").write_text(json.dumps(run))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = verdict.main(["--item-dir", str(self.item_dir()), "--n", "1"])
            self.assertEqual(code, expected)
            self.assertEqual("fix_list" in json.loads(out.getvalue()), style == "code")
        data = json.loads((self.item_dir() / "verdict-1.json").read_text())
        del data["fix_list"]
        (self.item_dir() / "verdict-1.json").write_text(json.dumps(data))
        run["policy"]["review"] = "code"
        (self.run_dir / "run.json").write_text(json.dumps(run))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verdict.main(["--item-dir", str(self.item_dir()), "--n", "1"]), 2)
            self.assertEqual(verdict.main(["--item-dir", str(self.item_dir()), "--n", "1", "--style", "blind"]), 0)

    def test_an_unknown_style_is_an_error_of_the_caller(self):
        with self.assertRaises(ValueError):
            verdict.check(self.item_dir(), 1, style="skim")
