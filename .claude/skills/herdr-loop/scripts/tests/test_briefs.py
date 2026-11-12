"""The two sets of briefs: the kit's first, kept word for word so the two can
be compared on the same work, and the current set, whose paragraphs follow the
item's route, the program's settings and the repository's facts."""

from __future__ import annotations

import hashlib
import json

from hl import items, program, spawn, train
from hl.common import Refusal, read_json, write_json

from .support import allow_exemptions, KIT, SpawnCase

# sha256 of each brief as workspace main had it at 13d2a863, before round 2 touched the kit.
FIRST_BRIEFS = {
    "agree.md": "d9e765ebe39e1405729526c8a39d89601625c7725ab90cc053c08939f5be759f",
    "build.md": "9cf9faff14d914708e5be46479a78dc3afd831fd4cd3c629ee4479ffbbbd3ede",
    "contract.md": "f406a37aeaaf8ba3661a29a2060fccfd1b488fee8001775e4f8196b32c7aee6d",
    "correction.md": "d7d31ee289c9b4a60864579e8496111409ac4be1057204ef74be3def9df2a4f5",
    "grade.md": "a34950a8c171178aa9a473060b2b5fef4de109c30dceae8594e08f4a55f7ed25",
    "rework-block.md": "eac49d08a044430763c57d85c0efb3441de446643b4e460c93d18f2427f10cb6",
}


class BriefCase(SpawnCase):
    SETTINGS: dict = {"briefs": "v2"}

    def setUp(self):
        super().setUp()
        self.settings(**self.SETTINGS)

    def settings(self, **changes):
        write_json(self.root / "program.json", {**read_json(self.root / "program.json"), **changes})

    def repo(self, **facts):
        self.settings(repos={"owner/repo": facts})

    def render(self, phase="build", engine=None, **sets):
        return spawn.render_brief(self.root, self.run_dir, "A-1", phase, sets, engine).read_text()

    def built(self, report_extra=""):
        self.start()
        self.write("A-1", "build-1.md", "**Serve.** `PORT=8401 python3 app.py`\n**Ready.** URL `http://127.0.0.1:8401/`\n" + report_extra)
        self.write("A-1", "build-1.done", "")
        spawn.close(self.run_dir, "A-1", "build", 1, self.herdr, sleep=lambda s: None)
        items.set_fields(self.run_dir, "A-1", ["pr=7", "pr_url=https://example.invalid/pull/7", "head_sha=" + "a" * 40])


class FirstBriefsTest(BriefCase):
    SETTINGS = {"briefs": "v1"}

    def test_the_first_briefs_are_kept_word_for_word(self):
        for name, digest in FIRST_BRIEFS.items():
            text = (KIT / "references" / "briefs-v1" / name).read_bytes()
            self.assertEqual(hashlib.sha256(text).hexdigest(), digest, f"briefs-v1/{name} was edited: it is the comparison's fixed side")

    def test_a_program_set_to_them_is_briefed_from_them(self):
        text = self.render()
        self.assertIn("You are the builder for one backlog item in an unattended build loop.", text)
        self.assertIn("## Do not", text)
        self.assertIn("serve any build on `8401`", text)
        self.assertIn("The item's port, if it serves anything: `8401`.", self.render("agree"))

    def test_settings_that_do_not_say_which_get_the_first_briefs(self):
        settings = read_json(self.root / "program.json")
        del settings["briefs"]
        write_json(self.root / "program.json", settings)
        self.assertEqual(program.load(self.root)["briefs"], "v1")
        self.assertIn("## Do not", self.render())

    def test_what_the_first_set_never_had_comes_from_the_current_one(self):
        cfg = program.load(self.root)
        for name in ("review.md", "blocks.md", "rework-checks-block.md"):
            self.assertEqual(spawn.brief_file(cfg, name).parent.name, "briefs", name)
        self.assertEqual(spawn.brief_file(cfg, "build.md").parent.name, "briefs-v1")

    def test_a_first_correction_brief_still_reads_as_it_did(self):
        self.built()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade()
        text = self.render("correction", CRITERION="2")
        self.assertIn("The disputed criterion: number 2.", text)
        self.assertIn(f"`{self.item_dir()}/verdict-1.json`", text)


class BuildBriefTest(BriefCase):
    def test_the_build_brief_says_what_is_wanted_and_leaves_no_hole(self):
        text = self.render()
        self.assertNotIn("{{", text)
        self.assertTrue(text.startswith("# Build brief: A-1, build 1"))
        self.assertIn("## Plan before you build", text)
        self.assertIn("starting `Criterion <n>:`", text)
        self.assertIn(f"Your worktree is `{self.worktree}`, on branch `hl/run-1/A-1`", text)
        self.assertIn("Port: `8401`.", text)
        self.assertIn(f"`{self.item_dir()}/build-1.done`", text)
        # The rules a script holds are told once, as what the check does, not as a list of prohibitions.
        self.assertNotIn("## Do not", text)
        self.assertIn("A script checks the build the moment the done file appears", text)

    def test_how_much_to_test_before_the_push_follows_the_setting(self):
        self.assertIn("run the repository's tests and linters once in full, the way its CI does, before you push", self.render())
        self.settings(prepush="touched")
        text = self.render()
        self.assertIn("**Test what you touch while you work**", text)
        self.assertIn("a full local run of them is not asked of you", text)
        self.assertNotIn("once in full", text)
        # The two kinds of check a new file fails (an item of an account security sweep: an unregistered script, a
        # scanner rule not skipped).
        self.assertIn("the scanners and linters the repository pins, at the version it pins (a security scanner such as checkov over the infrastructure you changed", text)
        self.assertIn("any test that holds a list of files to the tree (an index in a README, a registry of scripts or of pages) when you added, renamed or removed such a file", text)

    def test_the_look_follows_the_route_and_the_engine(self):
        self.assertIn("If this item changes anything a person looks at", self.render())
        items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        self.assertIn("**This item changes something a person looks at.** Open it in the browser", self.render(engine="claude"))
        cursor = self.render(engine="cursor")
        self.assertIn("this session has no browser", cursor)
        self.assertNotIn("Open it in the browser and compare", cursor)
        items.route_item(self.run_dir, "A-1", "independent", False, "a script")
        self.assertIn("This item was routed as changing nothing a person looks at.", self.render())

    def test_a_floor_asks_the_builder_to_look_whatever_the_route_says(self):
        item = items.load_item(self.run_dir, "A-1")
        item["floor"] = ["look"]
        item["route"] = {"premise": "independent", "look": False, "reason": "edited by hand", "at": "2026-10-12T00:00:00Z"}
        items.save_item(self.run_dir, item)
        self.assertIn("**This item changes something a person looks at.** Open it in the browser", self.render(engine="claude"))

    def test_sub_agents_are_named_only_to_a_worker_that_has_them(self):
        self.assertIn("Run long commands in the foreground and wait for them.", self.render())
        self.settings(subagents=True)
        text = self.render(engine="claude")
        self.assertIn("You have two sub-agents: `scout`", text)
        self.assertIn("the done file is written only when everything you started has finished", text)
        self.assertIn("Run long commands in the foreground", self.render(engine="cursor"))

    def test_a_worktree_that_arrived_ready_says_so_with_the_command(self):
        self.repo(setup="make deps")
        self.assertNotIn("Your worktree is ready", self.render())
        item = items.load_item(self.run_dir, "A-1")
        item["fields"]["setup"] = "ready"
        items.save_item(self.run_dir, item)
        text = self.render()
        self.assertIn("**Your worktree is ready.** `make deps` was run in it before you started", text)
        self.assertIn("Do not reinstall", text)

    def test_the_repositorys_facts_and_protected_paths_reach_the_builder(self):
        self.repo(facts=["CI uses Node 22 and npm 10.9.9: a lockfile written by another npm is refused."], protected=["brand/family/**", ".github/workflows/CICD.yml"])
        text = self.render()
        self.assertIn("## About this repository\n\n- CI uses Node 22 and npm 10.9.9", text)
        self.assertIn("or if the pull request changes a path no item may change (`brand/family/**`, `.github/workflows/CICD.yml`)", text)
        self.repo()
        text = self.render()
        self.assertNotIn("About this repository", text)
        self.assertNotIn("no item may change", text)

    def test_a_builder_is_told_which_protected_path_its_item_alone_may_change(self):
        protected = ["brand/family/**", ".github/workflows/CICD.yml"]
        self.repo(protected=protected)
        allow_exemptions(self.run_dir, ".github/workflows/CICD.yml")
        items.exempt_path(self.run_dir, "A-1", ".github/workflows/CICD.yml", "the item pins the deploy job's runner", protected)
        text = self.render()
        self.assertIn(
            "or if the pull request changes a path no item may change (`brand/family/**`, `.github/workflows/CICD.yml`; this item alone may change "
            "`.github/workflows/CICD.yml` (the item pins the deploy job's runner))",
            text,
        )

    def test_the_repositorys_facts_are_found_however_its_name_is_cased(self):
        # A run started as `owner/repo` in a program whose spec wrote `Owner/Repo`: GitHub knows them as one, and so do `hl-flow` and `hl-merge`.
        self.settings(repos={"Owner/Repo": {"facts": ["CI uses Node 22."], "protected": ["brand/**"]}})
        text = self.render()
        self.assertIn("## About this repository\n\n- CI uses Node 22.", text)
        self.assertIn("a path no item may change (`brand/**`)", text)

    def test_the_brief_start_renders_is_for_the_engine_it_starts(self):
        items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        self.start(engine="cursor")
        self.assertIn("this session has no browser", (self.item_dir() / "build-1.brief.md").read_text())

    def test_a_rework_reads_the_fix_list(self):
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "code"}})
        self.built()
        items.start_phase(self.run_dir, "A-1", "grade", session="rev-1", head_sha="a" * 40)
        verdict = {
            "verdict": "FAIL",
            "criteria": [{"n": 1, "result": "pass", "evidence": [], "note": "ran"}, {"n": 2, "result": "fail", "evidence": [], "note": "prints 2"}],
            "fix_list": [{"criterion": 2, "what": "prints 2, the item asks for 1", "where": "out.txt", "evidence": []}],
            "findings": [],
            "how_run": "make test",
        }
        self.write("A-1", "verdict-1.json", json.dumps(verdict))
        text = self.render()
        self.assertIn("## This is a rework", text)
        self.assertIn("its fix list, where it has one, is what you must change", text)
        self.assertIn("The verdict's `findings` are kept for follow-up and are not yours to act on.", text)
        self.assertIn(f"`{self.item_dir()}/verdict-1.json`", text)


    def test_a_reviews_pass_with_a_fix_list_is_a_rework_all_the_same(self):
        # Read as a grade's, this verdict is a pass and nothing would be sent back; read as the review's it is a fail.
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "code"}})
        self.built()
        items.start_phase(self.run_dir, "A-1", "grade", session="rev-1", head_sha="a" * 40)
        verdict = {
            "verdict": "PASS",
            "criteria": [{"n": 1, "result": "pass", "evidence": [], "note": "ran"}, {"n": 2, "result": "pass", "evidence": [], "note": "ran"}],
            "fix_list": [{"criterion": None, "what": "the test was rewritten to skip", "where": "tests/test_x.py:3", "evidence": []}],
            "findings": [],
            "how_run": "make test",
        }
        self.write("A-1", "verdict-1.json", json.dumps(verdict))
        self.assertIn("## This is a rework", self.render())


class TrainBriefTest(BriefCase):
    """A builder whose pull request goes into a train."""

    def setUp(self):
        super().setUp()
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "code", "merge": "train", "train": {"branch": "hl-train/canary", "item_checks": []}}})

    def test_a_builder_on_a_train_is_told_its_pull_request_runs_none_of_the_full_checks(self):
        # Whatever the program asks of a builder before a push elsewhere.
        for prepush in ("full", "touched"):
            self.settings(prepush=prepush)
            text = self.render()
            self.assertNotIn("{{", text)
            self.assertIn("Your pull request goes into `hl-train/canary`, the branch this program's items are gathered on, and it merges there on its review: the repository's full checks are not held on it", text)
            self.assertIn("before any of them reaches `main`", text)
            self.assertIn("No check is held on this pull request.", text)
            self.assertIn("Do not run the full suites yourself, and do not wait for them: never `gh pr checks --watch`", text)
            self.assertNotIn("once in full, the way its CI does", text)
            self.assertIn("run them on what you touched although the full checks run them too", text)
            self.assertIn("Here a failure of either is found only on a batch, and costs a repair after other items were built on yours.", text)
            self.assertNotIn("The repository's required checks run its full suites once, on your pull request", text)
        # Everything that names the base names the train: where the worktree was cut from, and where the pull request goes.
        self.assertIn("base branch `hl-train/canary`", text)
        self.assertIn("open one pull request against `hl-train/canary`", text)
        self.assertNotIn("origin/main", text)

    def test_while_the_train_is_red_a_builder_is_told_where_to_read_what_fails(self):
        self.assertNotIn("a repair is under way", self.render())
        red = train.red_file(self.root, "owner/repo")
        red.parent.mkdir(parents=True)
        red.write_text("# Batch 3: the required checks failed\n")
        text = self.render()
        self.assertIn(f"`{red}` says what failed. A test that fails for that reason is not yours to fix", text)
        self.assertNotIn("{{", text)

    def test_the_quick_checks_a_repository_still_holds_on_an_item_are_named(self):
        # Found in review: the brief said to wait for no check, and the merge waits for the ones the spec names.
        run = read_json(self.run_dir / "run.json")
        run["policy"]["train"]["item_checks"] = ["Lint and Validate Syntax", "secret-scan"]
        write_json(self.run_dir / "run.json", run)
        text = self.render()
        self.assertIn("Held on this pull request before it merges, and nothing else: `Lint and Validate Syntax`, `secret-scan`.", text)
        self.assertNotIn("No check is held on this pull request.", text)
        self.assertNotIn("{{", text)

    def test_a_run_that_merges_into_the_base_is_briefed_as_before(self):
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "code"}})
        self.settings(prepush="touched")
        text = self.render()
        self.assertIn("base branch `main`", text)
        self.assertIn("The repository's required checks run its full suites once, on your pull request", text)
        self.assertNotIn("hl-train", text)


class PremiseAndCorrectionBriefTest(BriefCase):
    def test_the_premise_brief_knows_how_the_build_will_be_reviewed(self):
        self.repo(facts=["`pyproject.toml` sets `-q`: a pytest run prints dots, not a summary."])
        text = self.render("agree")
        self.assertNotIn("{{", text)
        self.assertIn("a script serves the build from the builder's worktree before the review starts", text)
        self.assertIn("It is never stale because your own rebuild of the thing gave another value", text)
        self.assertIn("A criterion that the base branch already breaks, in files the item may not write, is not reachable.", text)
        self.assertIn("- `pyproject.toml` sets `-q`", text)

    def test_a_correction_after_a_review_reads_the_verdict_and_the_objection(self):
        self.built("\n## Disputed criterion\n\nCriterion 2: the item pins `1`; the criterion was amended to `2`.\n")
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade()
        text = self.render("correction", CRITERION="2")
        self.assertNotIn("{{", text)
        self.assertIn(f"The dispute: criterion 2. The verdict that failed it or could not check it: `{self.item_dir()}/verdict-1.json`", text)
        self.assertIn(f"The builder's objection: the `## Disputed criterion` section of `{self.item_dir()}/build-1.md`", text)
        self.assertNotIn("No review has run yet", text)

    def test_a_correction_before_any_review_reads_the_builders_dispute(self):
        self.built("\n## Disputed criterion\n\nCriterion 2: the item pins `1`; the criterion says `2`.\n")
        text = self.render("correction", CRITERION="2")
        self.assertIn("The builder's objection: the `## Disputed criterion` section of", text)
        self.assertIn("No review has run yet", text)
        self.assertNotIn("verdict-", text.split("## Decide")[0])

    def test_a_correction_with_nothing_in_dispute_is_not_briefed(self):
        self.built()
        with self.assertRaises(Refusal) as caught:
            self.render("correction", CRITERION="2")
        self.assertIn("DISPUTE: nothing is in dispute yet", str(caught.exception))

    def test_every_brief_of_the_current_set_renders_whole(self):
        self.repo(facts=["a fact"], setup="make deps", protected=["brand/**"], ci_covers=["`ci`: everything"])
        for phase in ("agree", "contract-draft", "build"):
            self.assertNotIn("{{", self.render(phase), phase)
        self.built()
        self.assertNotIn("{{", self.render("grade", READY_SUMMARY="answered 200"), "grade")

    def test_the_blind_grade_of_the_current_set_no_longer_forbids_the_criterions_own_install(self):
        self.built()
        text = self.render("grade", READY_SUMMARY="answered 200")
        self.assertIn("A criterion that names a suite or an install is yours to run", text)
        self.assertIn(f"`git clone -q {self.worktree} {self.item_dir()}/evidence-1/work`", text)
        self.assertIn("Leave the copy in place when you finish", text)
        self.assertNotIn("Do not install anything on this machine.", text)

    def test_a_reviewer_is_told_not_to_switch_commits_and_what_a_changed_file_at_its_start_is(self):
        self.built()
        switch = f"Do not switch the checkout to another commit or branch (`git checkout <sha>`, `git switch`): to read another commit use `git -C {self.worktree} show <sha>:<path>` or `git diff`."
        self.assertIn(switch, self.render("grade", READY_SUMMARY="answered 200"))
        for name in ("review.md", "confirm.md"):
            text = (KIT / "references" / "briefs" / name).read_text()
            self.assertIn("Do not switch the checkout to another commit or branch (`git checkout <sha>`, `git switch`)", text, name)
            self.assertIn("No review is started over a tracked file that differs from the commit, with one exception: a path the program's spec names", text, name)
            self.assertNotIn("is on record with that script", text, name)
        # The first set of briefs says what it always said.
        self.assertNotIn("Do not switch the checkout", (KIT / "references" / "briefs-v1" / "grade.md").read_text())

    def test_every_worker_is_told_about_commands_that_never_return(self):
        self.repo(ci_covers=["`ci`: everything"])
        rendered = {phase: self.render(phase) for phase in ("agree", "contract-draft", "build")}
        self.built()
        rendered["grade"] = self.render("grade", READY_SUMMARY="answered 200")
        for phase, text in rendered.items():
            self.assertIn("**Commands that never return.**", text, phase)
            self.assertIn("never a bare `until <check>; do sleep 2; done`", text, phase)
            self.assertIn("`sed -i '' 's/a/b/' file`", text, phase)
            self.assertNotIn("bounded foreground loop (`until", text, phase)
        for name in ("agree.md", "build.md", "confirm.md", "contract.md", "correction.md", "grade.md", "review.md"):
            self.assertIn("{{SHELL}}", (KIT / "references" / "briefs" / name).read_text(), name)
        # The first set of briefs says what it always said.
        for name in FIRST_BRIEFS:
            self.assertNotIn("{{SHELL}}", (KIT / "references" / "briefs-v1" / name).read_text(), name)

    def test_both_confirm_check_blocks_say_not_to_reinstall_run_a_full_suite_or_rebuild(self):
        blocks = spawn.brief_blocks(program.load(self.root))
        for name in ("checks-confirm", "checks-confirm-train"):
            self.assertIn("Do not reinstall, run a full suite or rebuild where `ci_covers` applies", blocks[name], name)

    def test_every_block_a_builder_can_be_given_exists(self):
        blocks = spawn.brief_blocks(program.load(self.root))
        for name in ("testing-touched", "testing-full", "look-builder", "look-builder-none", "no-look-builder", "look-builder-unknown", "helpers", "no-helpers", "setup-ready", "protected"):
            self.assertTrue(blocks[name].strip(), name)
