from __future__ import annotations

import contextlib
import io
import json
import os
import re
import signal
import subprocess
from pathlib import Path
from unittest import mock

from hl import herdr, items, program, spawn, train
from hl.common import sha256_text, Refusal, read_json, write_json
from hl.herdr import HerdrError

from .support import AGENTS_PATH_ERROR, FakeEnvs, FakeProcs, SpawnCase

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

TRUST_SCREEN = """ Accessing workspace:
 /Users/you/loops/canary/repos/main/scratch/.worktrees/run-1-A-1
 Quick safety check: Is this a project you created or one you trust?
 ❯ No, exit
   Yes, I trust this folder
"""


class BriefTest(SpawnCase):
    def render(self, phase="build", **sets):
        return spawn.render_brief(self.root, self.run_dir, "A-1", phase, sets).read_text()

    def test_a_build_brief_is_filled_from_the_items_record(self):
        text = self.render()
        self.assertNotIn("{{", text)
        self.assertIn(f"Your worktree: `{self.worktree}`, on branch `hl/run-1/A-1`", text)
        self.assertIn(f"`{self.item_dir()}/build-1.done`", text)
        self.assertIn("Repository `owner/repo`, default branch `main`", text)
        self.assertIn("serve any build on `8401`", text)
        self.assertNotIn("This is a rework", text)
        self.assertTrue((self.item_dir() / "build-1.brief.md").exists())

    def test_a_brief_with_a_value_missing_is_refused_and_says_how_to_supply_it(self):
        item = items.load_item(self.run_dir, "A-1")
        del item["fields"]["branch"]
        items.save_item(self.run_dir, item)
        with self.assertRaises(Refusal) as caught:
            self.render()
        self.assertIn("BRANCH: record it with `hl-items set --field branch=<value>`", str(caught.exception))
        self.assertFalse((self.item_dir() / "build-1.brief.md").exists())

    def test_an_item_that_serves_nothing_needs_no_port(self):
        item = items.load_item(self.run_dir, "A-1")
        del item["fields"]["port"]
        items.save_item(self.run_dir, item)
        for phase in ("agree", "build"):
            self.assertIn("`none (this item serves nothing)`", self.render(phase))

    def test_a_value_the_template_does_not_use_is_refused(self):
        with self.assertRaises(Refusal) as caught:
            self.render(CRITERION="2")
        self.assertIn("build.md has no placeholder named CRITERION", str(caught.exception))

    def test_an_agree_brief_can_be_pointed_at_a_draft(self):
        draft = str(self.item_dir() / "contract-draft-2.md")
        text = self.render("agree", CONTRACT_PATH=draft)
        self.assertIn(f"The contract under review: `{draft}`", text)
        self.assertTrue((self.item_dir() / "agree-2.brief.md").exists())

    def test_a_grade_brief_carries_how_to_run_the_build_and_where_evidence_goes(self):
        self.start()
        self.finish_build()
        write_json(self.item_dir() / "serve-1.json", {"summary": "http://127.0.0.1:8401/ answered 200 after 2 s."})
        text = self.render("grade")
        self.assertNotIn("{{", text)
        self.assertIn("**Serve.** `PORT=8401 python3 app.py`\n  **Ready.** URL `http://127.0.0.1:8401/`", text)
        self.assertIn("answered 200 after 2 s.", text)
        self.assertIn(f"Your working directory is `{self.item_dir()}/evidence-1`", text)
        self.assertIn("head commit `" + "a" * 40 + "`", text)

    def test_a_grade_brief_before_the_build_was_served_is_refused(self):
        self.start()
        self.finish_build()
        with self.assertRaises(Refusal) as caught:
            self.render("grade")
        self.assertIn("READY_SUMMARY: pass `--set READY_SUMMARY=<value>`", str(caught.exception))

    def test_a_second_build_brief_is_a_rework_pointing_at_the_verdict(self):
        self.start()
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade()
        text = self.render()
        self.assertIn("## This is a rework", text)
        self.assertIn(f"`{self.item_dir()}/verdict-1.json`", text)
        self.assertIn(f"`{self.item_dir()}/build-2.done`", text)
        self.assertNotIn("required checks on your build are not green", text)

    def test_red_checks_before_any_grade_send_the_build_back_with_the_checks(self):
        # The first program met this: a required check failed before a grade, and no rework brief could be written.
        self.start()
        self.finish_build()
        red = self.write("A-1", "checks-1.md", "- `app-tests`: fail.\n")
        text = self.render()
        self.assertNotIn("{{", text)
        self.assertIn("## The required checks on your build are not green", text)
        self.assertIn(f"`{red}` lists them", text)
        self.assertIn("If the same check fails on `origin/main` without your change", text)
        self.assertNotIn("## This is a rework", text)

    def test_a_failed_grade_and_red_checks_are_both_in_the_rework(self):
        self.start()
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade()
        self.write("A-1", "checks-1.md", "- `app-tests`: fail.\n")
        text = self.render()
        self.assertLess(text.index("## This is a rework"), text.index("## The required checks on your build are not green"))

    def test_checks_that_went_red_after_a_pass_do_not_call_the_grade_a_failure(self):
        self.start()
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.write("A-1", "verdict-1.json", json.dumps(self.passing_verdict()))
        self.write("A-1", "grade-1.done", "")
        spawn.close(self.run_dir, "A-1", "grade", 1, self.herdr, sleep=lambda s: None)
        self.write("A-1", "checks-1.md", "- `app-tests`: fail.\n")
        text = self.render()
        self.assertIn("## The required checks on your build are not green", text)
        self.assertNotIn("## This is a rework", text)

    def test_a_rework_the_manager_asks_for_carries_its_note(self):
        # Found in review: a branch that conflicts with its base after a pass, or a build sent back twice, has no
        # failed verdict and no red checks, and its further build could not be briefed at all.
        self.start()
        self.finish_build()
        note = self.write("A-1", "rework-2.md", "The branch conflicts with `origin/main` in `app.py`: merge the base in and resolve it.\n")
        text = self.render()
        self.assertNotIn("{{", text)
        self.assertIn("## What is still to do", text)
        self.assertIn(f"`{note}` says what and why", text)
        self.assertIn("merge `origin/main` into it; never rebase", text)
        self.assertNotIn("## This is a rework", text)

    def test_a_note_for_another_build_is_not_this_builds_reason(self):
        self.start()
        self.finish_build()
        self.write("A-1", "rework-3.md", "for a later build\n")
        with self.assertRaises(Refusal) as caught:
            self.render()
        self.assertIn("no rework-2.md", str(caught.exception))

    def test_a_further_build_with_nothing_to_fix_from_is_refused(self):
        self.start()
        self.finish_build()
        with self.assertRaises(Refusal) as caught:
            self.render()
        self.assertIn("build 1 has nothing on record to fix", str(caught.exception))
        self.assertIn("checks-1.md", str(caught.exception))
        self.assertIn(f"write what the builder must do in {self.item_dir()}/rework-2.md", str(caught.exception))
        # An old build's checks are not this build's.
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade()
        self.start()
        self.finish_build(2)
        self.write("A-1", "checks-1.md", "- `app-tests`: fail.\n")
        with self.assertRaises(Refusal) as caught:
            self.render()
        self.assertIn("build 2 has nothing on record to fix", str(caught.exception))

    def passing_verdict(self):
        count = items.load_item(self.run_dir, "A-1")["contract"]["criteria"]
        criteria = [{"n": i + 1, "result": "pass", "evidence": [], "note": "seen"} for i in range(count)]
        return {"verdict": "PASS", "criteria": criteria, "findings": [], "how_run": "curl"}


class ReviewBriefTest(SpawnCase):
    """A run whose evaluators review the code briefs a grade from review.md,
    with what the diff touches read from git and what the repository's spec says."""

    def setUp(self):
        super().setUp()
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "code"}})
        # The item's worktree as a real checkout: a base the remote has, and the pull request's one commit on top.
        git = lambda *args: subprocess.run(["git", "-C", str(self.worktree), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q", "-b", "hl/run-1/A-1")
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git("config", key, value)
        (self.worktree / "web").mkdir()
        (self.worktree / "web" / "page.html").write_text("<p>old</p>\n")
        (self.worktree / "tests").mkdir()
        (self.worktree / "tests" / "test_page.py").write_text("def test_page():\n    assert True\n")
        git("add", ".")
        git("commit", "-q", "-m", "base")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        (self.worktree / "web" / "page.html").write_text("<p>new</p>\n")
        (self.worktree / "tests" / "test_page.py").write_text("import pytest\n\n\n@pytest.mark.skip\ndef test_page():\n    assert True\n")
        git("add", ".")
        git("commit", "-q", "-m", "A-1: the change")
        self.head = git("rev-parse", "HEAD")
        self.start()
        self.finish_build()
        items.set_fields(self.run_dir, "A-1", [f"head_sha={self.head}"])
        write_json(self.item_dir() / "serve-1.json", {"summary": "http://127.0.0.1:8401/ answered 200 after 2 s."})

    def repo_facts(self, **facts):
        settings = read_json(self.root / "program.json")
        write_json(self.root / "program.json", {**settings, "repos": {"owner/repo": facts}})

    def render(self):
        return spawn.render_brief(self.root, self.run_dir, "A-1", "grade", {}).read_text()

    def test_a_review_brief_is_rendered_with_the_diffs_own_facts(self):
        self.repo_facts(seen=["web/**"])
        text = self.render()
        self.assertNotIn("{{", text)
        self.assertTrue(text.startswith("# Review brief: A-1, review 1"))
        self.assertIn(f"The change is `git -C {self.worktree} diff origin/main...HEAD`", text)
        self.assertIn("  - 2 files changed, 5 lines added and 1 removed: `tests/test_page.py`, `web/page.html`.", text)
        self.assertIn("  - Lines the diff adds that switch a check off or loosen one:\n    - `tests/test_page.py`: `@pytest.mark.skip`", text)
        self.assertIn("  - Changed paths this repository's spec says a person sees: `web/page.html`.", text)
        self.assertIn("**Serve.** `PORT=8401 python3 app.py`", text)
        facts = read_json(self.item_dir() / "grade-1.facts.json")
        self.assertEqual((facts["head"], facts["seen"]), (self.head, ["web/page.html"]))

    def test_on_a_train_the_reviewer_is_told_the_full_checks_have_not_run_and_what_to_run_instead(self):
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "code", "merge": "train", "train": {"branch": "hl-train/canary", "item_checks": []}}})
        # The train is where the item was cut from; the base has since moved somewhere the diff must not be read against.
        subprocess.run(["git", "-C", str(self.worktree), "update-ref", "refs/remotes/origin/hl-train/canary", "refs/remotes/origin/main"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        self.repo_facts(train={}, ci_covers=["`app-tests`: pytest and the frontend gate."], not_in_ci=["`pytest tests/extended`."])
        text = self.render()
        self.assertNotIn("{{", text)
        self.assertIn("**The repository's full checks are not held on this pull request.** It merges into `hl-train/canary`", text)
        self.assertIn("No check is held on this pull request. So nothing but this review stands between this change and that branch", text)
        self.assertIn("- `app-tests`: pytest and the frontend gate.", text)
        self.assertIn("the batch holds it before anything reaches `main`", text)
        self.assertIn("Run the tests that cover what the diff touches", text)
        self.assertIn("- `pytest tests/extended`.", text)
        self.assertNotIn("Required checks are not yours to run", text)
        self.assertIn(f"The change is `git -C {self.worktree} diff origin/hl-train/canary...HEAD`", text)
        self.assertIn("  - 2 files changed, 5 lines added and 1 removed: `tests/test_page.py`, `web/page.html`.", text)
        self.assertNotIn("a repair is under way", text)
        # While the latest tested batch is red, the reviewer is told where to read what fails.
        red = train.red_file(self.root, "owner/repo")
        red.parent.mkdir(parents=True)
        red.write_text("# Batch 1: the required checks failed\n")
        text = self.render()
        self.assertIn(f"The full checks failed on that branch when it was last tested, and a repair is under way: `{red}` says what failed.", text)
        self.assertNotIn("{{", text)
        # A spec that does not say what the checks run says so, and names none.
        self.repo_facts(train={})
        self.assertIn("- (the program's spec does not say what they run)", self.render())

    def test_a_run_that_grades_blind_keeps_the_grade_brief(self):
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "blind"}})
        text = self.render()
        self.assertTrue(text.startswith("# Grade brief: A-1, grade 1"))
        self.assertIn("Do not read the git diff", text)
        self.assertFalse((self.item_dir() / "grade-1.facts.json").exists())

    def test_the_look_follows_the_route_and_the_floor(self):
        self.assertIn("**Decide first whether this change alters anything a person looks at.**", self.render())
        items.route_item(self.run_dir, "A-1", "independent", False, "a script")
        text = self.render()
        self.assertIn("**This item was routed as changing nothing a person looks at.**", text)
        self.assertIn("set `look_needed` to `true`", text)
        item = items.load_item(self.run_dir, "A-1")
        item["floor"] = ["look"]
        items.save_item(self.run_dir, item)
        # A floor asks for the look whatever a route written before it says.
        self.assertIn("**This item changes something a person looks at.**", self.render())
        item["floor"] = []
        items.save_item(self.run_dir, item)
        items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        self.assertIn("**This item changes something a person looks at.**", self.render())

    def test_what_the_checks_run_is_said_when_the_spec_says_it(self):
        text = self.render()
        self.assertIn("Nothing is recorded about what this repository's required checks run", text)
        self.assertIn("`gh pr checks 7 --repo owner/repo`", text)
        self.repo_facts(
            facts=["`pyproject.toml` sets `-q` for pytest: a run prints dots, not a summary line."],
            ci_covers=["`app-tests`: `npm ci && npm run typecheck && npm run test && npm run build` in `Frontend/react`, then `pytest -n 8`."],
            not_in_ci=["`pytest tests/extended` (the delivery and server-rendered page tests)."],
        )
        text = self.render()
        self.assertIn("**Required checks are not yours to run or to wait for.**", text)
        self.assertIn("- `app-tests`: `npm ci && npm run typecheck", text)
        self.assertIn("is `checks`: the merge enforces it", text)
        self.assertIn("No required check runs these", text)
        self.assertIn("- `pytest tests/extended` (the delivery and server-rendered page tests).", text)
        self.assertIn("## About this repository\n\n- `pyproject.toml` sets `-q` for pytest", text)
        self.assertNotIn("Nothing is recorded", text)

    def test_checks_with_nothing_left_outside_them_say_no_more(self):
        self.repo_facts(ci_covers=["`ci`: the whole suite."])
        text = self.render()
        self.assertIn("- `ci`: the whole suite.", text)
        self.assertNotIn("No required check runs these", text)

    def test_a_worktree_whose_diff_cannot_be_read_stops_the_brief(self):
        items.set_fields(self.run_dir, "A-1", ["head_sha=" + "9" * 40])
        with self.assertRaises(Refusal) as caught:
            self.render()
        self.assertIn("could not read the diff of A-1", str(caught.exception))
        self.assertFalse((self.item_dir() / "grade-1.brief.md").exists())

    def test_every_block_the_briefs_use_is_in_the_blocks_file(self):
        blocks = spawn.brief_blocks(program.load(self.root))
        for name in ("look-review", "no-look-review", "look-unknown-review", "checks-known", "checks-unknown", "not-in-ci"):
            self.assertTrue(blocks[name].strip(), name)
            self.assertNotIn("### block:", blocks[name])


class StartTest(SpawnCase):
    def test_a_claude_builder_is_counted_started_with_its_role_and_sent_its_brief(self):
        out = self.start()
        self.assertEqual((out["started"], out["agent"], out["watch"], out["prompt_sent"]), (True, "canary-a1-b1", "A-1:build-1", True))
        entry = self.attempts()["log"][-1]
        self.assertEqual((entry["phase"], entry["n"], entry["engine"], entry["session"]), ("build", 1, "claude", out["session"]))
        self.assertRegex(out["session"], UUID_RE)

        tab = self.herdr.called("tab", "create")[0]
        self.assertEqual(tab[tab.index("--cwd") + 1], str(self.worktree))
        self.assertIn("--no-focus", tab)
        argv = self.agent_argv()
        self.assertEqual(argv[:5], ["--dangerously-skip-permissions", "--model", "claude-sonnet-5-5", "--effort", "high"])
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash,Read,Edit,Write,Glob,Grep")
        self.assertEqual(argv[argv.index("--session-id") + 1], out["session"])
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")
        self.assertTrue(argv[argv.index("--mcp-config") + 1].endswith("references/mcp-none.json"))
        for flag in ("--disable-slash-commands", "--strict-mcp-config"):
            self.assertIn(flag, argv)
        for path_flag in ("--settings", "--mcp-config"):
            self.assertTrue(Path(argv[argv.index(path_flag) + 1]).is_file(), path_flag)

        prompt = self.herdr.called("agent", "prompt")[0]
        self.assertEqual(prompt[2:4], ("canary-a1-b1", f"Read {self.item_dir()}/build-1.brief.md and do exactly what it says."))
        # Sent so that herdr says whether work began: without --wait it answers ok for text that went nowhere.
        self.assertEqual(prompt[4:], ("--wait", "--until", "working", "--until", "blocked", "--timeout", "15000"))
        self.assertEqual(self.herdr.taken, {"canary-a1-b1": [prompt[3]]})
        record = herdr.read_worker(self.item_dir(), "build", 1)
        self.assertEqual((record["pane"], record["kind"], record["session"]), (out["pane"], "claude", out["session"]))
        self.assertIsNotNone(record["shell_pid"])

    def test_secrets_are_loaded_in_the_pane_before_the_agent_and_matched_by_a_line_not_typed(self):
        self.start()
        order = [c[:2] for c in self.herdr.calls]
        self.assertLess(order.index(("pane", "wait-output")), order.index(("agent", "start")))
        typed = self.herdr.called("pane", "run")[0][3]
        self.assertIn("hl-env.sh op://Loop/worker-claude/password op://Loop/loop-gh/password ", typed)
        wait = self.herdr.called("pane", "wait-output")[0]
        pattern = wait[wait.index("--regex") + 1]
        self.assertRegex(pattern, r"^hl-env \(ok ([0-9a-f]{8})-ready\|FAILED \1:\)$")
        # Neither line the wait ends on is in what was typed, so only the script's own output can end it.
        self.assertIsNone(re.search(pattern, typed))

    def test_a_cursor_builder_gets_the_operators_flags_and_no_claude_token(self):
        out = self.start(engine="cursor")
        self.assertEqual(
            self.agent_argv(),
            ["--force", "--trust", "--approve-mcps", "--sandbox", "disabled", "--model", "composer-2.5[fast=false]"],
        )
        start = self.herdr.called("agent", "start")[0]
        self.assertEqual(start[start.index("--kind") + 1], "cursor")
        self.assertIn("hl-env.sh - op://Loop/loop-gh/password ", self.herdr.called("pane", "run")[0][3])
        self.assertEqual(self.attempts()["log"][-1]["session"], "cursor:canary-a1-b1")
        self.assertNotIn("session", herdr.read_worker(self.item_dir(), "build", 1))
        self.assertEqual({name for _, name in self.envs.asked}, {"GH_TOKEN", "HL_SHELLS", "ZDOTDIR"})
        self.assertEqual(out["kind"], "cursor")
        self.check_pins.assert_any_call(self.root, ("cursor-agent", "herdr"))

    def test_only_claude_evaluates(self):
        self.start()
        self.finish_build()
        with self.assertRaises(Refusal) as caught:
            self.start(phase="grade", engine="cursor")
        self.assertIn("every evaluation is a Claude session", str(caught.exception))
        self.assertEqual(self.attempts()["grade"], 0)

    def test_an_evaluator_gets_no_edit_tool_its_own_browser_and_a_directory_beside_the_item(self):
        self.start()
        self.finish_build()
        out = self.start("--set", "READY_SUMMARY=answered", phase="grade")
        argv = self.agent_argv()
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash,Read,Glob,Grep,Write")
        self.assertTrue(argv[argv.index("--mcp-config") + 1].endswith("references/mcp-playwright.json"))
        self.assertEqual(out["cwd"], str(self.item_dir() / "evidence-1"))
        self.assertEqual(out["agent"], "canary-a1-g1")
        entry = self.attempts()["log"][-1]
        self.assertEqual((entry["phase"], entry["head_sha"]), ("grade", "a" * 40))
        self.assertNotEqual(entry["session"], self.attempts()["log"][-2]["session"])

    def test_ui_gives_a_builder_a_browser(self):
        self.start("--ui")
        argv = self.agent_argv()
        self.assertTrue(argv[argv.index("--mcp-config") + 1].endswith("references/mcp-playwright.json"))

    def test_a_failed_secret_read_ends_the_wait_at_once_and_stops_the_spawn(self):
        self.herdr.secrets_say = "hl-env FAILED {nonce}: empty read for the Claude token"
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertRegex(str(caught.exception), r"the worker's secrets did not load \(hl-env FAILED [0-9a-f]{8}: empty read for the Claude token\)")
        self.assertEqual((self.herdr.called("agent", "start"), self.herdr.tab_panes(), self.attempts()["build"]), ([], {}, 0))

    def test_a_ready_line_for_another_spawn_does_not_count(self):
        self.herdr.secrets_say = "hl-env ok 00000000-ready"
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("did not load (hl-env ok 00000000-ready)", str(caught.exception))
        self.assertEqual(self.herdr.called("agent", "start"), [])

    def test_secrets_that_do_not_load_stop_the_spawn_and_give_the_attempt_back(self):
        self.herdr.fail[("pane", "wait-output")] = [HerdrError("timeout", "timed out waiting for output match")]
        self.herdr.screen = "you@loop-host % . hl-env.sh …\nhl-env FAILED: empty value for the GitHub token\n"
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("hl-env FAILED: empty value for the GitHub token", str(caught.exception))
        self.assertIn("the attempt was given back", str(caught.exception))
        self.assertEqual(self.herdr.called("agent", "start"), [])
        self.assertEqual(self.herdr.tab_panes(), {})
        self.assertEqual(self.attempts()["build"], 0)
        self.assertTrue(self.attempts()["log"][-1]["voided"])
        self.assertFalse(herdr.worker_path(self.item_dir(), "build", 1).exists())

    def test_a_pane_not_yet_at_its_prompt_is_tried_again(self):
        busy = HerdrError("agent_pane_busy", "pane is busy")
        self.herdr.fail[("agent", "start")] = [busy, busy, None]
        self.assertTrue(self.start()["started"])
        self.assertEqual(len(self.herdr.called("agent", "start")), 3)

    def test_the_folder_trust_dialog_is_answered_yes_by_moving_to_that_line(self):
        self.herdr.trust_dialog = True
        self.herdr.screen = TRUST_SCREEN
        self.assertTrue(self.start()["started"])
        self.assertEqual(self.herdr.keys, [("down", "enter")])

    def test_a_dialog_with_yes_already_selected_gets_only_enter(self):
        self.herdr.trust_dialog = True
        self.herdr.screen = " ❯ Yes, I trust this folder\n   No, exit\n"
        self.start()
        self.assertEqual(self.herdr.keys, [("enter",)])

    def test_a_dialog_with_yes_above_the_cursor_moves_up(self):
        self.herdr.trust_dialog = True
        self.herdr.screen = "   Yes, I trust this folder\n ❯ No, exit\n"
        self.start()
        self.assertEqual(self.herdr.keys, [("up", "enter")])

    def test_any_other_dialog_at_start_is_never_answered(self):
        self.herdr.trust_dialog = True
        self.herdr.screen = " Select login method:\n ❯ 1. Claude account with subscription\n   2. Console account\n"
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("not the folder-trust dialog", str(caught.exception))
        self.assertEqual(self.herdr.keys, [])
        self.assertEqual((self.herdr.tab_panes(), self.attempts()["build"]), ({}, 0))

    def test_a_claude_worker_without_its_own_token_is_closed_before_it_is_prompted(self):
        self.envs = FakeEnvs(absent={"CLAUDE_CODE_OAUTH_TOKEN"})
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("running without CLAUDE_CODE_OAUTH_TOKEN", str(caught.exception))
        self.assertEqual(self.herdr.called("agent", "prompt"), [])
        self.assertEqual((self.herdr.tab_panes(), self.attempts()["build"]), ({}, 0))

    def signed_in(self):
        """The program's Claude workers run on the program's seat, as its manager is."""
        settings = read_json(self.root / "program.json")
        write_json(self.root / "program.json", {**settings, "worker_claude_ref": "-"})

    def test_a_claude_worker_on_the_signed_in_account_is_started_without_a_claude_token(self):
        self.signed_in()
        self.envs = FakeEnvs(absent={"CLAUDE_CODE_OAUTH_TOKEN"})
        out = self.start()
        self.assertEqual((out["started"], out["kind"], out["prompt_sent"]), (True, "claude", True))
        self.assertIn("hl-env.sh - op://Loop/loop-gh/password ", self.herdr.called("pane", "run")[0][3])
        self.assertEqual({name for _, name in self.envs.asked}, {"GH_TOKEN", "HL_SHELLS", "ZDOTDIR"})
        self.assertEqual(self.attempts()["build"], 1)

    def test_a_claude_worker_on_the_signed_in_account_still_needs_the_loops_github_token(self):
        self.signed_in()
        self.envs = FakeEnvs(absent={"CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN"})
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("is running without GH_TOKEN", str(caught.exception))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", str(caught.exception))
        self.assertEqual(self.herdr.called("agent", "prompt"), [])
        self.assertEqual((self.herdr.tab_panes(), self.attempts()["build"]), ({}, 0))

    def test_one_worker_per_item_is_open_at_a_time(self):
        self.start()
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("still has a worker open (build-1.worker.json)", str(caught.exception))
        self.assertEqual(self.attempts()["build"], 1)
        self.assertEqual(len(self.herdr.called("tab", "create")), 1)

    def test_a_rework_resumes_the_builders_session_at_the_higher_effort(self):
        first = self.start()
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade()
        second = self.start()
        argv = self.agent_argv()
        self.assertEqual((second["n"], second["session"], second["resumed"]), (2, first["session"], True))
        self.assertEqual(argv[argv.index("--resume") + 1], first["session"])
        self.assertNotIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash,Read,Edit,Write,Glob,Grep")

    def test_a_cursor_build_that_failed_is_reworked_by_a_fresh_claude_session(self):
        self.start(engine="cursor")
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade()
        second = self.start()
        argv = self.agent_argv()
        self.assertFalse(second["resumed"])
        self.assertEqual(argv[argv.index("--session-id") + 1], second["session"])
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_fresh_reworks_in_a_new_session(self):
        first = self.start()
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade()
        second = self.start("--fresh")
        self.assertNotEqual(second["session"], first["session"])
        self.assertIn("--session-id", self.agent_argv())

    def test_the_manager_may_name_the_effort_of_any_attempt(self):
        out = self.start("--effort", "medium", "--why", "one file, well specified")
        argv = self.agent_argv()
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        entry = self.attempts()["log"][-1]
        self.assertEqual((entry["effort"], entry["why"]), ("medium", "one file, well specified"))
        record = read_json(self.item_dir() / "build-1.worker.json")
        self.assertEqual((record["effort"], record["model"], record["browser"]), ("medium", "claude-sonnet-5-5", False))
        self.assertEqual(out["effort"], "medium")

    def test_an_effort_the_program_does_not_allow_opens_nothing_and_counts_nothing(self):
        for effort in ("low", "max", "turbo"):
            with self.assertRaises(Refusal) as caught:
                self.start("--effort", effort)
            self.assertIn(f"effort {effort!r} is not one this program allows (medium, high, xhigh)", str(caught.exception))
        self.assertEqual((self.attempts()["build"], self.herdr.called("tab", "create")), (0, []))

    def test_an_effort_is_not_something_a_cursor_worker_takes(self):
        with self.assertRaises(Refusal) as caught:
            self.start("--effort", "high", engine="cursor")
        self.assertIn("--effort is for a Claude worker", str(caught.exception))
        self.assertEqual(self.attempts()["build"], 0)
        self.start(engine="cursor")
        record = read_json(self.item_dir() / "build-1.worker.json")
        self.assertEqual((record["effort"], record["model"]), (None, "composer-2.5[fast=false]"))

    def test_with_no_effort_named_each_role_starts_at_its_own(self):
        settings = read_json(self.root / "program.json")
        write_json(self.root / "program.json", {**settings, "build_effort": "medium", "review_effort": "xhigh", "rework_effort": "high"})
        self.start()
        self.assertEqual(self.agent_argv()[self.agent_argv().index("--effort") + 1], "medium")
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.assertEqual(self.agent_argv()[self.agent_argv().index("--effort") + 1], "xhigh")
        self.finish_grade()
        # A build after the first runs at the rework effort unless the manager names another.
        self.start()
        self.assertEqual(self.agent_argv()[self.agent_argv().index("--effort") + 1], "high")

    def test_a_rework_takes_the_effort_the_manager_names(self):
        self.start()
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade()
        self.start("--effort", "medium")
        argv = self.agent_argv()
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertIn("--resume", argv)

    def test_a_builder_whose_item_is_routed_with_a_look_gets_a_browser(self):
        items.route_item(self.run_dir, "A-1", "independent", True, "restyles a page")
        self.start()
        argv = self.agent_argv()
        self.assertTrue(argv[argv.index("--mcp-config") + 1].endswith("mcp-playwright.json"))
        self.assertTrue(read_json(self.item_dir() / "build-1.worker.json")["browser"])

    def test_a_builder_whose_item_is_routed_without_a_look_gets_none(self):
        items.route_item(self.run_dir, "A-1", "independent", False, "a script")
        self.start()
        argv = self.agent_argv()
        self.assertTrue(argv[argv.index("--mcp-config") + 1].endswith("mcp-none.json"))

    def open_subagents(self):
        settings = read_json(self.root / "program.json")
        write_json(self.root / "program.json", {**settings, "subagents": True})

    def test_without_the_setting_a_worker_has_no_sub_agents(self):
        self.start()
        argv = self.agent_argv()
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash,Read,Edit,Write,Glob,Grep")
        self.assertNotIn("--agents", argv)
        self.assertTrue(argv[argv.index("--settings") + 1].endswith("references/worker-settings.json"))
        self.assertFalse(read_json(self.item_dir() / "build-1.worker.json")["subagents"])
        self.assertFalse((self.item_dir() / "build-1.settings.json").exists())

    def test_with_sub_agents_open_a_builder_gets_the_kits_own_behind_the_guard(self):
        self.open_subagents()
        self.start()
        argv = self.agent_argv()
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash,Read,Edit,Write,Glob,Grep,Agent")
        kit = self.root / ".claude" / "skills" / "herdr-loop"
        # Found on the host: given the file's path, Claude Code printed that it takes a path only with -p, and exited.
        # No worker with sub-agents ever started, and the stand-in here had taken the path without a word.
        given = argv[argv.index("--agents") + 1]
        self.assertEqual(json.loads(given), read_json(kit / "references" / "worker-agents.json"))
        # One argument of the command line handed to Herdr, on one line, with nothing a shell would read as a quote.
        call = self.herdr.called("agent", "start")[0]
        self.assertEqual((call.count(given), "\n" in given, "'" in given, "`" in given, len(given) < 4000), (1, False, False, False, True))
        self.assertIn("'", json.dumps(read_json(kit / "references" / "worker-agents.json")))

        settings_path = Path(argv[argv.index("--settings") + 1])
        self.assertEqual(settings_path, self.item_dir() / "build-1.settings.json")
        settings = read_json(settings_path)
        # Attribution stays off, as in the kit's own settings file.
        self.assertEqual((settings["attribution"], settings["includeCoAuthoredBy"]), ({"commit": "", "pr": ""}, False))
        hook = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "Agent|Task")
        # A hook that cannot run at all must stop the call, not wave it through: exit 2 is what blocks.
        self.assertEqual(hook["hooks"][0]["command"], f"{(kit / 'scripts' / 'hooks' / 'agent-guard').resolve()} runner scout || exit 2")
        self.assertTrue(read_json(self.item_dir() / "build-1.worker.json")["subagents"])

    def test_a_worker_that_is_given_the_agents_as_a_file_does_not_start_and_the_refusal_says_what_its_pane_showed(self):
        self.open_subagents()
        with mock.patch("hl.spawn.agents_argument", return_value=str(self.root / ".claude" / "skills" / "herdr-loop" / "references" / "worker-agents.json")):
            with self.assertRaises(Refusal) as caught:
                self.start()
        self.assertEqual(
            str(caught.exception),
            "build 1 of A-1 was not started and the attempt was given back. timeout: timed out waiting for agent startup The pane's last lines: " + AGENTS_PATH_ERROR,
        )
        self.assertEqual(self.attempts()["build"], 0)

    def test_a_worker_that_did_not_start_in_a_pane_that_shows_nothing_is_refused_as_before(self):
        self.envs.absent.add("GH_TOKEN")
        self.herdr.screen = ""
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertNotIn("The pane's last lines", str(caught.exception))

    def test_an_evaluator_gets_them_too_and_still_cannot_edit(self):
        self.open_subagents()
        self.start()
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        argv = self.agent_argv()
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash,Read,Glob,Grep,Write,Agent")
        self.assertEqual(Path(argv[argv.index("--settings") + 1]), self.item_dir() / "grade-1.settings.json")

    def test_a_cursor_worker_is_started_as_before(self):
        self.open_subagents()
        self.start(engine="cursor")
        self.assertEqual(self.agent_argv(), ["--force", "--trust", "--approve-mcps", "--sandbox", "disabled", "--model", "composer-2.5[fast=false]"])
        self.assertFalse(read_json(self.item_dir() / "build-1.worker.json")["subagents"])

    def test_no_sub_agent_the_kit_defines_can_start_another_or_drive_a_browser(self):
        agents = spawn.worker_agents(program.load(self.root))
        self.assertEqual(sorted(agents), ["runner", "scout"])
        for name, agent in agents.items():
            self.assertTrue(agent["description"].strip() and agent["prompt"].strip(), name)
            for tool in agent["tools"]:
                self.assertIn(tool, ("Bash", "Read", "Glob", "Grep"), name)

    def test_a_voided_attempts_settings_file_is_set_aside_with_it(self):
        self.open_subagents()
        out = self.start()
        del self.herdr.panes[out["pane"]]
        spawn.close(self.run_dir, "A-1", "build", 1, self.herdr, sleep=lambda s: None)
        voided = items.void_attempt(self.run_dir, "A-1", "build", None, "its pane went away")
        self.assertFalse((self.item_dir() / "build-1.settings.json").exists())
        self.assertTrue((self.item_dir() / voided["voided"]["set_aside"] / "build-1.settings.json").exists())

    def test_a_build_past_its_bound_opens_nothing(self):
        self.start()
        self.finish_build(1)
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade(1)
        self.start()
        self.finish_build(2)
        panes_opened = len(self.herdr.called("tab", "create"))
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("build bound reached", str(caught.exception))
        self.assertEqual(len(self.herdr.called("tab", "create")), panes_opened)

    def test_a_working_directory_outside_the_loop_root_is_refused(self):
        with self.assertRaises(Refusal) as caught:
            self.start("--cwd", str(self.tmp))
        self.assertIn("is outside the loop root", str(caught.exception))
        self.assertEqual(self.attempts()["build"], 0)

    def test_a_prompt_herdr_refuses_is_reported_and_never_sent_again(self):
        self.herdr.fail[("agent", "prompt")] = [HerdrError("agent_blocked", "agent is blocked; nothing was sent")]
        out = self.start()
        self.assertEqual((out["started"], out["prompt_sent"]), (True, False))
        self.assertIn("read the pane before sending anything", out["warning"])
        self.assertEqual(len(self.herdr.called("agent", "prompt")), 1)
        self.assertEqual(self.attempts()["build"], 1)
        self.assertTrue(herdr.worker_path(self.item_dir(), "build", 1).exists())


class FirstPromptTest(SpawnCase):
    """A manager's first prompt in a new loop root sat unsent on 2026-10-12:
    herdr called the session ready while Claude was still starting, and
    answered ok for a prompt that went nowhere. Four of six went that way."""

    NAME = "canary-a1-b1"

    def line(self):
        return f"Read {self.item_dir()}/build-1.brief.md and do exactly what it says."

    def typed_times(self):
        return len(self.herdr.called("agent", "prompt"))

    def transcript(self, home, *texts):
        """A Claude transcript for the worker that was started, holding these messages from its user."""
        start = self.herdr.called("agent", "start")[0]
        session = start[start.index("--session-id") + 1]
        path = home / ".claude" / "projects" / "-root" / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [{"type": "attachment", "attachment": {"type": "hook_success"}}]
        records += [{"type": "user", "message": {"role": "user", "content": text}} for text in texts]
        path.write_text("".join(json.dumps(r) + "\n" for r in records))

    def home(self):
        home = self.tmp / "home"
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return home

    def test_after_the_trust_dialog_the_brief_waits_for_the_input_box(self):
        self.herdr.trust_dialog = True
        self.herdr.screen = TRUST_SCREEN
        self.herdr.box_at = 4
        out = self.start()
        self.assertTrue(out["prompt_sent"])
        self.assertEqual(self.herdr.taken, {self.NAME: [self.line()]})
        self.assertEqual((self.typed_times(), self.herdr.keys), (1, [("down", "enter")]))

    def test_a_shell_prompt_that_starts_with_the_mark_is_not_taken_for_the_input_box(self):
        self.assertFalse(spawn._box_drawn("Last login: Mon Oct 12\n\u276f claude --dangerously-skip-permissions\n"))
        self.assertFalse(spawn._box_drawn(TRUST_SCREEN + "\u2500\u2500\u2500\n\u276f\u00a0\n"))
        self.assertTrue(spawn._box_drawn("\u2500\u2500\u2500\n\u276f\u00a0Try \"fix lint errors\"\n\u2500\u2500\u2500\n"))

    def test_a_session_that_never_draws_its_input_box_is_closed_and_the_attempt_given_back(self):
        self.herdr.trust_dialog = True
        self.herdr.screen = TRUST_SCREEN
        self.herdr.box_at = 10**6
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("did not draw its input box", str(caught.exception))
        self.assertEqual((self.typed_times(), self.herdr.tab_panes(), self.attempts()["build"]), (0, {}, 0))

    def test_a_prompt_that_never_arrived_is_typed_once_more(self):
        self.herdr.fates = ["lost"]
        out = self.start()
        self.assertTrue(out["prompt_sent"])
        self.assertEqual((self.typed_times(), self.herdr.keys), (2, []))
        self.assertEqual(self.herdr.taken, {self.NAME: [self.line()]})

    def test_a_prompt_left_typed_in_the_input_box_gets_its_enter_and_is_not_typed_again(self):
        self.herdr.fates = ["typed"]
        out = self.start()
        self.assertTrue(out["prompt_sent"])
        self.assertEqual((self.typed_times(), self.herdr.keys), (1, [("enter",)]))
        self.assertEqual(self.herdr.taken, {self.NAME: [self.line()]})

    def test_a_prompt_that_stays_typed_is_reported_and_left_for_a_reader(self):
        self.herdr.fates = ["stuck"]
        out = self.start()
        self.assertEqual((out["started"], out["prompt_sent"]), (True, False))
        self.assertIn("Enter did not start it", out["warning"])
        self.assertIn("read the pane before sending anything", out["warning"])
        self.assertEqual((self.typed_times(), self.herdr.keys), (1, [("enter",)] * spawn.SUBMIT_TRIES))
        self.assertEqual(self.attempts()["build"], 1)
        self.assertTrue(herdr.worker_path(self.item_dir(), "build", 1).exists())

    def test_a_prompt_nothing_takes_is_typed_a_bounded_number_of_times(self):
        self.herdr.fates = ["lost"] * 20
        out = self.start()
        self.assertFalse(out["prompt_sent"])
        self.assertIn("herdr saw no work begin", out["warning"])
        self.assertEqual((self.typed_times(), self.herdr.keys), (1 + spawn.SUBMIT_TRIES, []))

    def test_an_agent_that_stopped_on_a_dialog_is_not_typed_into(self):
        self.herdr.fates = ["dialog"]
        out = self.start()
        self.assertFalse(out["prompt_sent"])
        self.assertIn("the agent is blocked", out["warning"])
        self.assertEqual((self.typed_times(), self.herdr.keys), (1, []))

    def test_a_prompt_herdr_did_not_see_start_is_not_typed_again_when_the_transcript_has_it(self):
        home = self.home()
        self.herdr.fates = ["unseen"]
        self.herdr.on_take = lambda name, text: self.transcript(home, "an earlier message", text)
        out = self.start()
        self.assertTrue(out["prompt_sent"])
        self.assertEqual((self.typed_times(), self.herdr.keys), (1, []))

    def test_a_prompt_is_not_typed_again_for_an_agent_that_is_working(self):
        self.herdr.fates = ["late"]
        out = self.start(engine="cursor")
        self.assertTrue(out["prompt_sent"])
        self.assertEqual((self.typed_times(), self.herdr.keys), (1, []))

    def test_only_a_message_from_the_user_in_the_transcript_says_the_prompt_arrived(self):
        home = self.home()
        self.herdr.fates = ["unseen"]

        def quoted(name, text):
            self.transcript(home)
            start = self.herdr.called("agent", "start")[0]
            path = home / ".claude" / "projects" / "-root" / f"{start[start.index('--session-id') + 1]}.jsonl"
            with open(path, "a") as fh:
                fh.write(json.dumps({"type": "last-prompt", "lastPrompt": text}) + "\n")
                fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
                fh.write("not json\n")

        self.herdr.on_take = quoted
        out = self.start()
        self.assertFalse(out["prompt_sent"])
        self.assertEqual(self.typed_times(), 1)

    def test_without_a_transcript_a_prompt_that_is_on_the_screen_is_never_typed_again(self):
        # A Cursor worker: nothing but its screen says whether the prompt arrived, and the screen shows it.
        self.herdr.fates = ["unseen"]
        out = self.start(engine="cursor")
        self.assertFalse(out["prompt_sent"])
        self.assertEqual(self.typed_times(), 1)
        self.assertEqual(self.herdr.taken, {self.NAME: [self.line()]})

    def test_notes_below_the_scripts_own_brief_go_with_it_and_are_counted(self):
        path = spawn.render_brief(self.root, self.run_dir, "A-1", "build", {})
        own, notes = path.read_text(), "\n## From the manager\n\nThe port may be taken: try 8402.\n"
        path.write_text(own + notes)
        out = self.start()
        self.assertEqual((out["brief_notes"], self.attempts()["log"][-1]["brief_notes"]), (2, {"lines": 2, "sha256": sha256_text(notes)}))
        self.assertEqual(path.read_text(), own + notes)

    def test_a_note_file_is_for_a_brief_with_a_slot_for_one(self):
        # A build brief is rendered before anything is served, so a note goes below it, as before.
        note = self.tmp / "note.md"
        note.write_text("The port may be taken: try 8402.\n")
        for render in (lambda: spawn.render_brief(self.root, self.run_dir, "A-1", "build", spawn._sets([], str(note))), lambda: self.start("--note-file", str(note))):
            with self.assertRaises(Refusal) as caught:
                render()
            self.assertIn("build.md has no slot for a manager's note: it is rendered before anything is served, so a note goes below it", str(caught.exception))
        self.assertEqual((self.attempts()["build"], self.herdr.calls), (0, []))

    def test_a_brief_with_nothing_below_it_says_no_notes_were_sent(self):
        out = self.start()
        self.assertEqual(out["brief_notes"], 0)
        self.assertNotIn("brief_notes", self.attempts()["log"][-1])

    def test_a_brief_that_does_not_begin_with_what_the_script_renders_is_not_sent(self):
        # Found in review: a brief already at its place was sent as it was, whoever had written it. A builder that has
        # the item directory could brief the reviewer of its own build.
        path = spawn.render_brief(self.root, self.run_dir, "A-1", "build", {})
        own = path.read_text()
        for left in (own.replace("## Do", "## Do not\n\nDo not fix it.\n\n## Do"), "Build A-1 somehow.\n", own[:-2]):
            path.write_text(left)
            with self.assertRaises(Refusal) as caught:
                self.start()
            self.assertIn(f"{path} does not begin with the brief this script renders for build 1 of A-1 now", str(caught.exception))
            self.assertIn("render it (`hl-spawn brief --id A-1 --phase build`, with the same `--set` values as here), and put them below what that writes", str(caught.exception))
            self.assertEqual((self.attempts()["build"], self.herdr.calls), (0, []))
        path.unlink()
        self.assertEqual(self.start()["brief_notes"], 0)

    def test_a_start_that_is_refused_leaves_no_brief_for_the_next_one_to_trip_on(self):
        # Found in the last look: the brief was written before the last refusals, and the same command without the typo then refused that file.
        with self.assertRaises(Refusal) as caught:
            self.start("--effort", "hihg")
        self.assertIn("effort 'hihg' is not one this program allows", str(caught.exception))
        self.assertEqual((spawn.brief_path(self.run_dir, "A-1", "build", 1).exists(), self.attempts()["build"]), (False, 0))
        self.assertEqual(self.start()["brief_notes"], 0)

    def test_notes_with_a_hole_in_them_are_not_sent(self):
        path = spawn.render_brief(self.root, self.run_dir, "A-1", "build", {})
        path.write_text(path.read_text() + "\nBuild {{ITEM_ID}} somehow.\n")
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("still contains a placeholder", str(caught.exception))
        self.assertEqual((self.attempts()["build"], self.herdr.calls), (0, []))

    def test_changed_binaries_stop_the_spawn_before_anything_is_counted(self):
        self.check_pins.side_effect = Refusal("claude at /x has changed content since it was pinned")
        with self.assertRaises(Refusal):
            self.start()
        self.assertEqual((self.attempts()["build"], self.herdr.called("tab", "create")), (0, []))
        self.check_pins.assert_called_with(self.root, ("claude", "herdr"))


class WorkspaceTest(SpawnCase):
    """A run's workers live in one workspace named for the run's work."""

    def name_run(self, work, run_dir=None, files=None):
        files = files or (self.file_a, self.file_b)
        return items.init_run(run_dir or self.run_dir, "run-1", "owner/repo", [str(f) for f in files], work=work)

    def record(self):
        return read_json(self.run_dir / "workspace.json")

    def end(self):
        return spawn.end_run(self.run_dir, self.herdr)

    def refusal(self):
        with self.assertRaises(Refusal) as caught:
            self.end()
        return str(caught.exception)

    def test_the_workspace_is_named_for_the_work_and_opened_once_in_the_run_directory(self):
        self.name_run("Help desk portal")
        self.start()
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.assertEqual(self.herdr.labels(), ["Help desk portal"])
        made = self.herdr.called("workspace", "create")
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0][made[0].index("--cwd") + 1], str(self.run_dir))

    def test_a_run_nobody_named_is_labeled_with_its_program_and_run(self):
        self.start()
        self.assertEqual(self.herdr.labels(), ["canary run-1"])

    def test_a_worker_is_a_tab_beside_the_first_pane_which_nothing_is_typed_into(self):
        out = self.start()
        record = self.record()
        self.assertEqual(out["workspace"], record["workspace"])
        self.assertNotEqual(out["pane"], record["pane"])
        self.assertNotIn("ran", self.herdr.panes[record["pane"]])
        self.assertEqual((self.herdr.tabs[record["tab"]], self.herdr.tabs[out["tab"]]), ("run-1", "A-1 build-1"))

    def test_two_runs_worked_at_once_are_two_workspaces(self):
        cfg = program.load(self.root)
        self.name_run("Help desk portal")
        other = self.root / "runs" / "run-2"
        items.init_run(other, "run-2", "owner/repo", [str(self.file_b)], work="Help desk agent pages")
        first = spawn.run_workspace(self.herdr, cfg, self.run_dir)
        second = spawn.run_workspace(self.herdr, cfg, other)
        self.assertNotEqual(first, second)
        self.assertEqual(self.herdr.labels(), ["Help desk portal", "Help desk agent pages"])
        self.assertEqual(spawn.run_workspace(self.herdr, cfg, other), second)

    def test_a_run_is_renamed_only_before_its_workspace_opens(self):
        self.assertEqual(self.name_run("Portal")["work"], "Portal")
        self.assertEqual(self.name_run("Help desk portal")["work"], "Help desk portal")
        self.start()
        with self.assertRaises(Refusal) as caught:
            self.name_run("Something else")
        self.assertIn("already open as 'Help desk portal'", str(caught.exception))
        self.assertEqual(self.name_run(None)["work"], "Help desk portal")

    def test_a_workspace_is_not_closed_before_its_work_is_shipped(self):
        self.start()
        self.finish_build()
        said = self.refusal()
        self.assertIn("closes only when its work is completed and shipped", said)
        self.assertIn("A-1 is in_progress", said)
        self.assertIn("A-2 is pending", said)
        self.assertEqual(self.herdr.labels(), ["canary run-1"])
        self.assertEqual(self.herdr.called("pane", "close")[-1][2], read_json(self.item_dir() / "build-1.worker.json")["pane"])

    def test_a_merged_item_whose_deploy_check_has_not_passed_is_not_shipped(self):
        self.start()
        self.finish_build()
        self.ship("A-2", "B-1")
        self.ship("A-1", verify=None)
        self.assertIn("A-1 is merged but its Verify is not recorded as passed", self.refusal())
        self.ship("A-1", verify="verify-failed")
        self.assertIn("field verify=verify-failed", self.refusal())

    def test_a_merged_item_with_no_deploy_check_is_shipped(self):
        self.start()
        self.finish_build()
        self.ship("A-1")
        self.ship("A-2", "B-1", verify=None)  # neither has a Verify line
        self.assertTrue(self.end()["closed"])

    def test_a_failed_or_parked_item_keeps_the_workspace_and_a_skipped_one_does_not(self):
        self.start()
        self.finish_build()
        self.ship("A-1", "B-1")
        for status in ("failed", "parked"):
            item = items.load_item(self.run_dir, "A-2")
            item["status"] = status
            items.save_item(self.run_dir, item)
            self.assertIn(f"A-2 is {status}", self.refusal())
        item["status"] = "skipped"
        items.save_item(self.run_dir, item)
        self.assertTrue(self.end()["closed"])

    def test_a_failed_item_skipped_as_superseded_by_a_shipped_repair_lets_the_workspace_close(self):
        self.start()
        self.finish_build()
        self.ship("A-1", "B-1")
        item = items.load_item(self.run_dir, "A-2")
        item["status"] = "in_progress"
        items.save_item(self.run_dir, item)
        items.set_status(self.run_dir, "A-2", "failed", "build: carried by B-1")
        self.assertIn("A-2 is failed", self.refusal())
        items.set_status(self.run_dir, "A-2", "skipped", "superseded by B-1", superseded_by="B-1")
        self.assertTrue(self.end()["closed"])

    def test_shipped_work_closes_its_workspace(self):
        self.start()
        self.finish_build()
        self.ship()
        out = self.end()
        self.assertEqual((out["closed"], out["already"], out["label"], out["workspace_still_listed"]), (True, False, "canary run-1", False))
        self.assertEqual((self.herdr.labels(), self.herdr.panes), ([], {}))
        self.assertTrue(self.end()["already"])

    def test_an_open_worker_holds_the_workspace_even_when_everything_is_merged(self):
        self.start()
        self.ship()
        said = self.refusal()
        self.assertIn("still has something open (A-1/build-1.worker.json)", said)
        self.assertEqual(self.herdr.labels(), ["canary run-1"])

    def test_a_build_being_served_holds_the_workspace(self):
        self.start()
        self.finish_build()
        self.ship()
        write_json(self.item_dir() / "serve-1.json", {"served": True, "stopped_at": None})
        self.assertIn("A-1/serve-1.json", self.refusal())
        write_json(self.item_dir() / "serve-1.json", {"served": True, "stopped_at": "2026-10-11T00:00:00Z"})
        self.assertTrue(self.end()["closed"])

    def test_a_pane_someone_else_opened_keeps_the_workspace_and_the_answer_says_so(self):
        self.start()
        self.finish_build()
        self.ship()
        workspace = self.record()["workspace"]
        mine = self.herdr.call("tab", "create", "--workspace", workspace, "--cwd", str(self.root), "--label", "notes", "--no-focus")
        out = self.end()
        self.assertEqual((out["closed"], out["workspace_still_listed"]), (True, True))
        self.assertEqual(list(self.herdr.panes), [mine["root_pane"]["pane_id"]])

    def test_a_first_pane_whose_id_now_holds_another_shell_is_left_alone(self):
        self.start()
        self.finish_build()
        self.ship()
        # herdr was restarted and the id is someone else's workspace now.
        self.herdr.spaces[0]["label"] = "Bills"
        self.herdr.panes[self.record()["pane"]]["shell_pid"] = 99999
        out = self.end()
        self.assertIn("left alone", out["note"])
        self.assertEqual(self.herdr.called("pane", "close")[-1][2], read_json(self.item_dir() / "build-1.worker.json")["pane"])

    def test_the_runs_workspace_brought_back_after_a_reboot_is_closed_while_its_shell_runs_nothing(self):
        # After a host move's reboot herdr restored w1F, w1H and w1J with new shells; end-run left all three open.
        self.start()
        self.finish_build()
        self.ship()
        pane = self.record()["pane"]
        self.herdr.panes[pane].update(shell_pid=99999, processes=[{"pid": 99999, "argv0": "zsh"}, {"pid": 99998, "argv0": "vim"}])
        busy = self.end()
        self.assertIn("left alone", busy["note"])
        write_json(spawn.workspace_record(self.run_dir), {k: v for k, v in self.record().items() if k not in ("closed_at", "close_note")})
        self.herdr.panes[pane]["processes"] = [{"pid": 99999, "argv0": "zsh"}]
        out = self.end()
        self.assertEqual((out["note"], out["workspace_still_listed"]), ("closed", False))
        self.assertNotIn(pane, self.herdr.panes)

    def test_a_workspace_already_named_for_this_work_is_used_and_never_closed_from_here(self):
        self.name_run("Help desk portal")
        self.herdr.open_space("w7", "Help desk portal")
        self.herdr.panes["w7:p90"] = {"shell_pid": 41, "processes": [], "first": True}
        out = self.start()
        self.assertEqual((out["workspace"], self.herdr.called("workspace", "create")), ("w7", []))
        self.finish_build()
        self.ship()
        ended = self.end()
        self.assertIn("did not open the workspace", ended["note"])
        self.assertEqual((ended["workspace_still_listed"], list(self.herdr.panes)), (True, ["w7:p90"]))

    def test_a_workspace_the_operator_renamed_is_still_the_runs(self):
        self.start()
        self.finish_build()
        self.herdr.spaces[0]["label"] = "portal (watching)"
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.assertEqual(len(self.herdr.called("workspace", "create")), 1)

    def test_an_id_that_now_names_other_work_is_not_taken_for_the_runs(self):
        self.start()
        self.finish_build()
        # herdr was restarted: the id and the pane id now belong to someone else's workspace.
        self.herdr.spaces[0]["label"] = "Bills"
        self.herdr.panes[self.record()["pane"]]["shell_pid"] = 99999
        out = self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.assertEqual(self.herdr.labels(), ["Bills", "canary run-1"])
        self.assertEqual(out["workspace"], self.record()["workspace"])
        self.assertNotEqual(out["workspace"], self.herdr.spaces[0]["workspace_id"])

    def test_a_run_that_never_started_a_worker_has_no_workspace_to_close(self):
        self.assertIn("never opened a workspace", self.refusal())


class DraftTest(SpawnCase):
    def setUp(self):
        super().setUp()
        # A loose item: no contract yet, so start from an item nothing has touched.
        items.set_fields(self.run_dir, "A-2", ["branch=hl/run-1/A-2", f"worktree={self.worktree}", "port=8402"])

    def test_the_drafters_session_is_named_when_its_draft_is_reviewed(self):
        opts = self.opts(phase="contract-draft", engine="cursor")
        draft = spawn.start(self.root, self.run_dir, "A-2", "contract-draft", "cursor", self.herdr, opts, envs=self.envs)
        self.assertEqual((draft["agent"], draft["n"]), ("canary-a2-d1", 1))
        self.assertEqual(items.load_attempts(self.run_dir, "A-2")["agree"], 0)
        spawn.close(self.run_dir, "A-2", "contract-draft", 1, self.herdr, sleep=lambda s: None)

        opts = self.opts("--set", f"CONTRACT_PATH={self.item_dir('A-2')}/contract-draft-1.md", phase="agree")
        spawn.start(self.root, self.run_dir, "A-2", "agree", "claude", self.herdr, opts, envs=self.envs)
        entry = items.load_attempts(self.run_dir, "A-2")["log"][-1]
        self.assertEqual((entry["phase"], entry["n"], entry["drafter_session"]), ("agree", 1, "cursor:canary-a2-d1"))


class PromptCloseTest(SpawnCase):
    def setUp(self):
        super().setUp()
        self.out = self.start()

    def test_an_open_worker_is_pointed_at_a_file_in_its_item_directory(self):
        fix = self.write("A-1", "build-1.fix-1.md", "Fix the trailer.\n")
        sent = spawn.send(self.run_dir, "A-1", "build", 1, str(fix), self.herdr)
        self.assertEqual(sent["watch"], "A-1:build-1")
        self.assertEqual(self.herdr.called("agent", "prompt")[-1][3], f"Read {fix.resolve()} and do exactly what it says.")

    def test_a_pointer_that_never_arrived_is_typed_once_more(self):
        fix = self.write("A-1", "build-1.fix-1.md", "Fix the trailer.\n")
        # A worker whose last turn ended reads `done` as often as `idle`.
        self.herdr.agents["canary-a1-b1"]["agent_status"] = "done"
        self.herdr.fates = ["lost"]
        sent = spawn.send(self.run_dir, "A-1", "build", 1, str(fix), self.herdr, sleep=lambda s: None)
        self.assertTrue(sent["prompted"])
        self.assertEqual(self.herdr.taken["canary-a1-b1"][-1], f"Read {fix.resolve()} and do exactly what it says.")

    def test_a_pointer_the_worker_did_not_take_is_refused(self):
        fix = self.write("A-1", "build-1.fix-1.md", "Fix the trailer.\n")
        self.herdr.agents["canary-a1-b1"]["agent_status"] = "idle"
        self.herdr.fates = ["stuck"]
        with self.assertRaises(Refusal) as caught:
            spawn.send(self.run_dir, "A-1", "build", 1, str(fix), self.herdr, sleep=lambda s: None)
        self.assertIn("canary-a1-b1 did not take the prompt", str(caught.exception))
        self.assertIn("read its pane before sending anything", str(caught.exception))

    def test_a_pointer_herdr_did_not_see_start_is_found_in_the_workers_transcript(self):
        fix = self.write("A-1", "build-1.fix-1.md", "Fix the trailer.\n")
        home = self.tmp / "home"
        path = home / ".claude" / "projects" / "-root" / f"{self.out['session']}.jsonl"
        path.parent.mkdir(parents=True)
        self.herdr.agents["canary-a1-b1"]["agent_status"] = "idle"
        self.herdr.fates = ["unseen"]
        self.herdr.on_take = lambda name, text: path.write_text(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            sent = spawn.send(self.run_dir, "A-1", "build", 1, str(fix), self.herdr, sleep=lambda s: None)
        self.assertTrue(sent["prompted"])
        self.assertEqual(len(self.herdr.called("agent", "prompt")), 2)  # the brief at start, and this pointer once

    def test_the_same_pointer_sent_before_does_not_vouch_for_this_one(self):
        fix = self.write("A-1", "build-1.fix-1.md", "Fix the trailer.\n")
        line = f"Read {fix.resolve()} and do exactly what it says."
        home = self.tmp / "home"
        path = home / ".claude" / "projects" / "-root" / f"{self.out['session']}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": line}]}}) + "\n")
        self.herdr.agents["canary-a1-b1"]["agent_status"] = "idle"
        self.herdr.fates = ["stuck"]
        with mock.patch.dict(os.environ, {"HOME": str(home)}), self.assertRaises(Refusal):
            spawn.send(self.run_dir, "A-1", "build", 1, str(fix), self.herdr, sleep=lambda s: None)

    def test_a_file_outside_the_item_directory_is_not_sent(self):
        outside = self.tmp / "note.md"
        outside.write_text("x")
        with self.assertRaises(Refusal):
            spawn.send(self.run_dir, "A-1", "build", 1, str(outside), self.herdr)

    def test_close_closes_the_pane_this_script_opened(self):
        result = spawn.close(self.run_dir, "A-1", "build", 1, self.herdr, sleep=lambda s: None)
        self.assertEqual((result["closed"], result["already"]), (True, False))
        self.assertEqual(self.herdr.called("pane", "close")[0][2], self.out["pane"])
        self.assertIn("closed_at", herdr.read_worker(self.item_dir(), "build", 1))
        self.assertTrue(spawn.close(self.run_dir, "A-1", "build", 1, self.herdr)["already"])
        self.assertEqual(len(self.herdr.called("pane", "close")), 1)

    def test_a_pane_that_is_already_gone_is_recorded_as_closed(self):
        del self.herdr.panes[self.out["pane"]]
        result = spawn.close(self.run_dir, "A-1", "build", 1, self.herdr, sleep=lambda s: None)
        self.assertEqual(result["note"], "the pane was already gone")
        self.assertEqual(self.herdr.called("pane", "close"), [])

    def test_a_pane_id_that_now_holds_another_shell_is_left_alone(self):
        self.herdr.panes[self.out["pane"]]["shell_pid"] = 99999
        result = spawn.close(self.run_dir, "A-1", "build", 1, self.herdr, sleep=lambda s: None)
        self.assertIn("left alone", result["note"])
        self.assertEqual(self.herdr.called("pane", "close"), [])
        self.assertIn(self.out["pane"], self.herdr.panes)

    def test_a_closed_worker_cannot_be_prompted(self):
        spawn.close(self.run_dir, "A-1", "build", 1, self.herdr, sleep=lambda s: None)
        fix = self.write("A-1", "build-1.fix-1.md", "x")
        with self.assertRaises(Refusal):
            spawn.send(self.run_dir, "A-1", "build", 1, str(fix), self.herdr)


class CliTest(SpawnCase):
    def cli(self, *argv, herdr_fake=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = spawn.main(["--root", str(self.root), "--run-dir", str(self.run_dir), *argv], herdr=herdr_fake)
        return code, json.loads(out.getvalue())

    def test_a_brief_can_be_rendered_anywhere(self):
        code, out = self.cli("brief", "--id", "A-1", "--phase", "build")
        self.assertEqual((code, Path(out["brief"]).name), (0, "build-1.brief.md"))

    def test_starting_a_worker_outside_herdr_is_refused(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("HERDR_ENV", None)
            code, out = self.cli("start", "--id", "A-1", "--phase", "build", "--engine", "claude")
        self.assertEqual(code, 2)
        self.assertIn("not inside a Herdr pane", out["refused"][0])
        self.assertEqual(self.attempts()["build"], 0)

    def test_a_refusal_exits_2_and_a_herdr_failure_is_reported_the_same_way(self):
        self.start()
        self.herdr.fail[("pane", "process-info")] = [HerdrError("exit_1", "connection refused")]
        code, out = self.cli("close", "--id", "A-1", "--phase", "build", "--n", "1", herdr_fake=self.herdr)
        self.assertEqual(code, 2)
        self.assertIn("connection refused", out["refused"][0])


class EndRunCliTest(CliTest):
    def test_end_run_refuses_unshipped_work_with_exit_2(self):
        self.start()
        self.finish_build()
        code, out = self.cli("end-run", herdr_fake=self.herdr)
        self.assertEqual(code, 2)
        self.assertIn("completed and shipped", out["refused"][0])

    def test_end_run_closes_shipped_work(self):
        self.start()
        self.finish_build()
        self.ship()
        code, out = self.cli("end-run", herdr_fake=self.herdr)
        self.assertEqual((code, out["closed"], self.herdr.labels()), (0, True, []))


class StartAgainTest(CliTest):
    """What the canary's manager did when a worker's pane went away: close the
    record, void the attempt, start the same attempt again."""

    def items_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = items.main(["--run-dir", str(self.run_dir), *argv])
        return code, json.loads(out.getvalue())

    def lose(self, phase="build", n=1, engine="cursor", item_id="A-1"):
        """A worker that came up and whose pane then went away; the manager closes its record."""
        opts = self.opts(phase=phase, engine=engine)
        out = spawn.start(self.root, self.run_dir, item_id, phase, engine, self.herdr, opts, envs=self.envs, sleep=lambda s: None)
        del self.herdr.panes[out["pane"]]
        code, closed = self.cli("close", "--id", item_id, "--phase", phase, "--n", str(n), herdr_fake=self.herdr)
        self.assertEqual((code, closed["note"]), (0, "the pane was already gone"))
        return out

    def test_a_voided_attempt_whose_worker_came_up_is_started_again_under_the_same_number(self):
        first = self.lose()
        code, voided = self.items_cli("void", "--id", "A-1", "--phase", "build", "--n", "1", "--note", "its pane was closed")
        self.assertEqual((code, self.attempts()["build"]), (0, 0))
        again = self.start(engine="cursor")
        self.assertEqual((again["started"], again["n"], again["watch"]), (True, 1, "A-1:build-1"))
        self.assertNotEqual(again["pane"], first["pane"])
        self.assertEqual(self.attempts()["build"], 1)
        self.assertEqual([bool(e.get("voided")) for e in self.attempts()["log"] if e["phase"] == "build"], [True, False])
        # The live record is the new worker's, and it is the only open one.
        self.assertEqual(herdr.read_worker(self.item_dir(), "build", 1)["pane"], again["pane"])
        self.assertEqual(spawn._open_workers(self.run_dir, "A-1"), ["build-1.worker.json"])

    def test_what_the_voided_attempt_left_is_kept_out_of_the_way_with_the_reason(self):
        first = self.lose()
        self.write("A-1", "build-1.done.refused-1", "")
        code, voided = self.items_cli("void", "--id", "A-1", "--phase", "build", "--n", "1", "--note", "its pane was closed")
        kept = self.item_dir() / "voided" / "build-1.1"
        self.assertEqual(voided["voided"]["set_aside"], "voided/build-1.1")
        self.assertEqual(sorted(p.name for p in kept.iterdir()), ["build-1.brief.md", "build-1.done.refused-1", "build-1.worker.json", "voided.json"])
        self.assertEqual(read_json(kept / "build-1.worker.json")["pane"], first["pane"])
        self.assertEqual(read_json(kept / "voided.json")["note"], "its pane was closed")
        self.assertEqual(sorted(p.name for p in self.item_dir().glob("build-1.*")), [])

    def test_a_second_loss_of_the_same_attempt_is_kept_beside_the_first(self):
        for k in (1, 2):
            self.lose()
            code, voided = self.items_cli("void", "--id", "A-1", "--phase", "build", "--note", f"lost {k}")
            self.assertEqual((code, voided["voided"]["set_aside"]), (0, f"voided/build-1.{k}"))
        self.assertTrue(self.start(engine="cursor")["started"])
        self.assertEqual(self.attempts()["build"], 1)

    def test_an_attempt_whose_worker_is_still_open_is_not_voided(self):
        self.start(engine="cursor")
        code, out = self.items_cli("void", "--id", "A-1", "--phase", "build", "--n", "1", "--note", "x")
        self.assertEqual(code, 2)
        self.assertIn("still has its worker open; close it with `hl-spawn close` first", out["refused"][0])
        self.assertEqual(self.attempts()["build"], 1)
        self.assertTrue(herdr.worker_path(self.item_dir(), "build", 1).exists())

    def test_void_without_a_number_means_the_latest_attempt_and_a_wrong_number_names_it(self):
        self.lose()
        code, out = self.items_cli("void", "--id", "A-1", "--phase", "build", "--n", "2", "--note", "x")
        self.assertEqual(code, 2)
        self.assertIn("A-1 has no live build attempt 2; the latest is 1", out["refused"][0])
        code, out = self.items_cli("void", "--id", "A-1", "--phase", "build", "--note", "x")
        self.assertEqual((code, out["n"], self.attempts()["build"]), (0, 1, 0))

    def test_a_grade_started_again_gets_an_empty_evidence_directory_and_a_fresh_evaluator(self):
        self.start()
        self.finish_build()
        opts = self.opts("--set", "READY_SUMMARY=answered", phase="grade")
        first = spawn.start(self.root, self.run_dir, "A-1", "grade", "claude", self.herdr, opts, envs=self.envs, sleep=lambda s: None)
        (self.item_dir() / "evidence-1" / "criterion-1.png").write_text("half a grade")
        del self.herdr.panes[first["pane"]]
        spawn.close(self.run_dir, "A-1", "grade", 1, self.herdr, sleep=lambda s: None)
        code, voided = self.items_cli("void", "--id", "A-1", "--phase", "grade", "--note", "the evaluator hung in a browser call")
        self.assertEqual(code, 0)
        self.assertTrue((self.item_dir() / "voided" / "grade-1.1" / "evidence-1" / "criterion-1.png").exists())
        again = self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.assertEqual((again["n"], again["cwd"]), (1, str(self.item_dir() / "evidence-1")))
        self.assertEqual(list((self.item_dir() / "evidence-1").iterdir()), [])
        self.assertNotEqual(again["session"], first["session"])

    def test_a_spawn_that_fails_leaves_nothing_behind_and_the_same_line_works_again(self):
        self.start()
        self.finish_build()
        self.herdr.secrets_say = "hl-env FAILED {nonce}: empty read for the Claude token"
        with self.assertRaises(Refusal):
            self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.assertEqual(sorted(p.name for p in self.item_dir().glob("grade-1.*")) + sorted(p.name for p in self.item_dir().glob("evidence-1")), [])
        # The evidence directory it made held nothing, so it is removed, not kept.
        self.assertEqual(sorted(p.name for p in (self.item_dir() / "voided" / "grade-1.1").iterdir()), ["grade-1.brief.md", "voided.json"])
        self.herdr.secrets_say = None
        self.assertTrue(self.start("--set", "READY_SUMMARY=answered", phase="grade")["started"])

    def test_a_void_that_left_its_files_in_place_is_finished_by_voiding_again(self):
        # What a kit before files were set aside left behind: the number given back, the dead worker's record still under it.
        first = self.lose()
        attempts = self.attempts()
        attempts["log"][-1]["voided"] = {"at": "2026-10-11T23:22:45Z", "note": "exercise 2"}
        attempts["build"] = 0
        items.save_attempts(self.run_dir, "A-1", attempts)
        with self.assertRaises(Refusal) as caught:
            self.start(engine="cursor")
        self.assertIn("void it with `hl-items void --id A-1 --phase build --n 1 --note <why>`", str(caught.exception))
        code, out = self.items_cli("void", "--id", "A-1", "--phase", "build", "--n", "1", "--note", "finishing the earlier void")
        self.assertEqual((code, out["voided"]), (0, {"at": "2026-10-11T23:22:45Z", "note": "exercise 2", "set_aside": "voided/build-1.1"}))
        self.assertEqual((self.attempts()["build"], self.attempts()["log"][-1]["voided"]["set_aside"]), (0, "voided/build-1.1"))
        # A brief rendered for the attempt to come is not part of that void.
        brief = spawn.render_brief(self.root, self.run_dir, "A-1", "build")
        code, out = self.items_cli("void", "--id", "A-1", "--phase", "build", "--n", "1", "--note", "again")
        self.assertEqual((code, brief.exists()), (2, True))
        again = self.start(engine="cursor")
        self.assertEqual((again["n"], self.attempts()["build"]), (1, 1))
        self.assertNotEqual(again["pane"], first["pane"])
        # Once finished there is nothing more to void under that number until the new attempt is the live one.
        spawn.close(self.run_dir, "A-1", "build", 1, self.herdr, sleep=lambda s: None)
        self.items_cli("void", "--id", "A-1", "--phase", "build", "--note", "x")
        code, out = self.items_cli("void", "--id", "A-1", "--phase", "build", "--n", "1", "--note", "x")
        self.assertEqual((code, out["refused"]), (2, ["A-1 has no live build attempt 1"]))

    def test_a_void_that_left_a_result_in_place_is_not_finished(self):
        # The number was given back and a result is under it: that is not what a void leaves, and it is not set aside as one.
        self.lose()
        attempts = self.attempts()
        attempts["log"][-1]["voided"] = {"at": "2026-10-11T23:22:45Z", "note": "exercise 2"}
        attempts["build"] = 0
        items.save_attempts(self.run_dir, "A-1", attempts)
        self.write("A-1", "build-1.md", "**Serve.** `true`\n**Ready.** COMMAND `true`\n")
        code, out = self.items_cli("void", "--id", "A-1", "--phase", "build", "--n", "1", "--note", "finishing the earlier void")
        self.assertEqual((code, out["refused"]), (2, ["build-1.md exists: that attempt produced a result and stays counted"]))
        self.assertTrue((self.item_dir() / "build-1.md").exists())

    def test_void_without_a_number_voids_the_rework_and_not_the_first_build(self):
        self.start()
        self.finish_build()
        self.start("--set", "READY_SUMMARY=answered", phase="grade")
        self.finish_grade()
        self.lose(n=2, engine="claude")
        code, out = self.items_cli("void", "--id", "A-1", "--phase", "build", "--note", "the rework's pane went away")
        self.assertEqual((code, out["n"], out["voided"]["set_aside"], self.attempts()["build"]), (0, 2, "voided/build-2.1", 1))
        self.assertTrue((self.item_dir() / "build-1.worker.json").exists())

    def test_a_voided_attempt_does_not_show_the_done_time_of_the_one_started_after_it(self):
        self.lose()
        self.items_cli("void", "--id", "A-1", "--phase", "build", "--note", "its pane was closed")
        self.start(engine="cursor")
        self.write("A-1", "build-1.done", "")
        rows = [r for r in items.timeline(self.run_dir, "A-1") if r["phase"] == "build"]
        self.assertEqual([(r["voided"], "done_at" in r) for r in rows], [(True, False), (False, True)])

    def test_a_void_that_left_nothing_says_so_and_is_not_taken_for_an_unfinished_one(self):
        items.start_phase(self.run_dir, "A-1", "build", session="b", engine="cursor")
        code, out = self.items_cli("void", "--id", "A-1", "--phase", "build", "--note", "counted by hand, nothing started")
        self.assertEqual((code, out["voided"]["set_aside"]), (0, None))
        brief = spawn.render_brief(self.root, self.run_dir, "A-1", "build")
        code, out = self.items_cli("void", "--id", "A-1", "--phase", "build", "--n", "1", "--note", "again")
        self.assertEqual((code, brief.exists()), (2, True))

    def test_a_lost_drafter_is_voided_and_started_again(self):
        items.set_fields(self.run_dir, "A-2", ["branch=hl/run-1/A-2", f"worktree={self.worktree}", "port=8402"])
        self.lose(phase="contract-draft", item_id="A-2")
        opts = self.opts(phase="contract-draft", engine="cursor")
        with self.assertRaises(Refusal) as caught:
            spawn.start(self.root, self.run_dir, "A-2", "contract-draft", "cursor", self.herdr, opts, envs=self.envs, sleep=lambda s: None)
        self.assertIn("void it with `hl-items void", str(caught.exception))
        code, out = self.items_cli("void", "--id", "A-2", "--phase", "contract-draft", "--note", "its pane went away")
        self.assertEqual((code, out["n"], out["voided"]["set_aside"]), (0, 1, "voided/contract-draft-1.1"))
        again = spawn.start(self.root, self.run_dir, "A-2", "contract-draft", "cursor", self.herdr, opts, envs=self.envs, sleep=lambda s: None)
        self.assertEqual((again["started"], again["n"]), (True, 1))
        self.assertEqual(items.load_attempts(self.run_dir, "A-2")["agree"], 0)

    def test_a_draft_that_was_written_is_not_voided(self):
        items.set_fields(self.run_dir, "A-2", ["branch=hl/run-1/A-2", f"worktree={self.worktree}", "port=8402"])
        self.lose(phase="contract-draft", item_id="A-2")
        self.write("A-2", "contract-draft-1.md", "# Contract\n")
        code, out = self.items_cli("void", "--id", "A-2", "--phase", "contract-draft", "--note", "x")
        self.assertEqual(code, 2)
        self.assertIn("contract-draft-1.md exists", out["refused"][0])

    def test_there_is_no_draft_to_void_before_one_was_started(self):
        code, out = self.items_cli("void", "--id", "A-2", "--phase", "contract-draft", "--note", "x")
        self.assertEqual(code, 2)
        self.assertIn("A-2 has no contract draft 1 to void", out["refused"][0])


class NameTest(SpawnCase):
    def test_names_are_ones_herdr_accepts_and_differ_by_phase(self):
        names = {spawn.agent_name("canary", "HL-02", phase, 1) for phase in spawn.PHASES}
        self.assertEqual(len(names), len(spawn.PHASES))
        for name in names:
            self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,31}$")
        self.assertEqual(spawn.agent_name("Ticketdesk gap review", "HD-128", "grade", 2), "ticketdesk-hd128-g2")


class ClaudeHomeStartTest(SpawnCase):
    """Claude workers of a program whose sessions run on another sign-in (`claude_config_dir`)."""

    def setUp(self):
        super().setUp()
        self.team = str(self.tmp / "home" / ".claude-team")
        settings = read_json(self.root / "program.json")
        write_json(self.root / "program.json", {**settings, "worker_claude_ref": "-", "claude_config_dir": self.team})

    def test_a_claude_worker_starts_on_the_programs_sign_in(self):
        out = self.start()
        self.assertTrue(out["started"])
        ran = [c[3] for c in self.herdr.called("pane", "run")]
        self.assertEqual(len(ran), 3)
        self.assertIn("hl-env.sh", ran[0])
        self.assertIn(f"export CLAUDE_CONFIG_DIR={self.team};", ran[1])
        self.assertIn("export HL_ZDOTDIR=", ran[2])
        order = [c[:2] for c in self.herdr.calls]
        self.assertLess(len(order) - order[::-1].index(("pane", "run")) - 1, order.index(("agent", "start")))
        self.assertIn("CLAUDE_CONFIG_DIR", {name for _, name in self.envs.asked})

    def test_a_claude_worker_starts_on_the_seat_the_program_moved_to(self):
        other = str(self.tmp / "home" / ".claude-other")
        write_json(self.root / "program.json", {**read_json(self.root / "program.json"), "claude_seats": [self.team, other]})
        write_json(self.root / "seat.json", {"home": other})
        self.assertTrue(self.start()["started"])
        ran = [c[3] for c in self.herdr.called("pane", "run")]
        self.assertIn(f"export CLAUDE_CONFIG_DIR={other};", ran[1])

    def test_a_claude_worker_without_the_programs_sign_in_is_closed_before_it_is_prompted(self):
        self.envs = FakeEnvs(absent={"CLAUDE_CONFIG_DIR"})
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("running without CLAUDE_CONFIG_DIR", str(caught.exception))
        self.assertEqual(self.herdr.called("agent", "prompt"), [])
        self.assertEqual((self.herdr.tab_panes(), self.attempts()["build"]), ({}, 0))

    def test_a_sign_in_that_does_not_load_starts_no_worker_and_gives_the_attempt_back(self):
        self.herdr.home_say = f"hl-home FAILED {{nonce}}: no Claude sign-in in {self.team}"
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("sign-in did not load", str(caught.exception))
        self.assertEqual(self.herdr.called("agent", "start"), [])
        self.assertEqual((self.herdr.tab_panes(), self.attempts()["build"]), ({}, 0))

    def test_a_cursor_worker_is_given_no_claude_sign_in(self):
        self.start(engine="cursor")
        ran = [c[3] for c in self.herdr.called("pane", "run")]
        self.assertFalse(any("CLAUDE_CONFIG_DIR" in line for line in ran))


class WorkerReapTest(SpawnCase):
    """Every shell a worker starts writes itself down, and closing the worker
    stops what those shells left running (`hl/reap.py`). The leftovers here
    are what the mini showed: the tool shell 4000 is gone, and launchd has
    adopted its two jobs, which keep its session."""

    def shells(self, phase="build", n=1):
        return self.item_dir() / f"{phase}-{n}.shells"

    def leftovers(self, procs):
        self.shells().write_text("4000 100\n")
        procs.add(4001, 4000, "sleep 4271")
        procs.add(4002, 4000, "sleep 4272")
        procs.add(6100, 6100, "not the worker's")

    def close(self, procs):
        return spawn.close(self.run_dir, "A-1", "build", 1, self.herdr, sleep=lambda s: None, procs=procs)

    def test_the_pane_has_its_shells_write_themselves_down_last_before_the_agent_starts(self):
        out = self.start()
        kit = program.load(self.root)["kit"]
        line = self.herdr.called("pane", "run")[-1][3]
        self.assertTrue(line.startswith(f'export HL_ZDOTDIR="${{ZDOTDIR-}}" HL_SHELLS={self.shells()} && export ZDOTDIR={kit}/scripts/zdotdir && '), line)
        typed = self.herdr.calls.index(self.herdr.called("pane", "run")[-1])
        self.assertLess(typed, self.herdr.calls.index(self.herdr.called("agent", "start")[0]))
        self.assertIn("hl-env.sh", self.herdr.called("pane", "run")[0][3])
        self.assertEqual(out["shells"], str(self.shells()))
        self.assertLessEqual({"HL_SHELLS", "ZDOTDIR"}, {name for _, name in self.envs.asked})

    def test_a_pane_that_cannot_write_its_shells_down_starts_no_agent(self):
        self.herdr.shells_say = "hl-shells FAILED {nonce}"
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("could not write its shell down", str(caught.exception))
        self.assertEqual(self.herdr.called("agent", "start"), [])
        self.assertEqual((self.herdr.tab_panes(), self.attempts()["build"]), ({}, 0))

    def test_an_agent_whose_shells_would_not_write_themselves_down_is_closed_before_it_is_prompted(self):
        self.envs = FakeEnvs(absent={"ZDOTDIR"})
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("is running without ZDOTDIR", str(caught.exception))
        self.assertEqual(self.herdr.called("agent", "prompt"), [])

    def test_closing_a_worker_stops_what_it_left_running_and_nothing_else(self):
        self.start()
        procs = FakeProcs()
        self.leftovers(procs)
        out = self.close(procs)
        self.assertEqual(procs.killed, [(4001, signal.SIGTERM), (4002, signal.SIGTERM)])
        self.assertEqual(out["reaped"], [{"pid": 4001, "command": "sleep 4271", "signal": "TERM"}, {"pid": 4002, "command": "sleep 4272", "signal": "TERM"}])
        self.assertEqual(out["note"], "closed; stopped 2 processes it left running (pid 4001 `sleep 4271`; pid 4002 `sleep 4272`)")
        self.assertNotIn("warning", out)
        record = herdr.read_worker(self.item_dir(), "build", 1)
        self.assertEqual((record["reap"]["reaped"], record["reap"]["left"], record["close_note"]), (out["reaped"], [], out["note"]))
        self.assertIn(6100, procs.rows)

    def test_a_worker_that_left_nothing_is_closed_as_before(self):
        self.start()
        self.shells().write_text("4000 100\n")
        out = self.close(FakeProcs())
        self.assertEqual((out["note"], out["reaped"], out["left"]), ("closed", [], []))
        self.assertEqual(herdr.read_worker(self.item_dir(), "build", 1)["reap"]["shells"], 1)

    def test_a_worker_whose_pane_is_already_gone_still_has_its_leftovers_stopped(self):
        out = self.start()
        del self.herdr.panes[out["pane"]]
        procs = FakeProcs()
        self.leftovers(procs)
        closed = self.close(procs)
        self.assertTrue(closed["note"].startswith("the pane was already gone; stopped 2 processes"), closed["note"])

    def test_what_kill_does_not_end_is_reported(self):
        self.start()
        procs = FakeProcs(ends_on={4002: ()})
        self.leftovers(procs)
        out = self.close(procs)
        self.assertEqual(out["left"], [{"pid": 4002, "command": "sleep 4272"}])
        self.assertIn("still running after KILL: 4002", out["warning"])
        self.assertIn("1 still running after KILL (pid 4002)", herdr.read_worker(self.item_dir(), "build", 1)["close_note"])

    def test_a_worker_started_by_an_older_kit_is_closed_as_before(self):
        self.start()
        record = herdr.read_worker(self.item_dir(), "build", 1)
        del record["shells"]
        herdr.write_worker(self.item_dir(), "build", 1, record)
        procs = FakeProcs()
        out = self.close(procs)
        self.assertIn("started by an older kit", out["note"])
        self.assertEqual(procs.looked, 0)

    def test_a_worker_that_did_not_come_up_has_what_it_had_started_stopped(self):
        # Its pane wrote its shell down, and the agent started a server before it was refused for a missing token.
        self.envs = FakeEnvs(absent={"GH_TOKEN"})
        self.item_dir().mkdir(parents=True, exist_ok=True)
        procs = FakeProcs()
        self.leftovers(procs)
        opts = self.opts()
        with self.assertRaises(Refusal) as caught:
            spawn.start(self.root, self.run_dir, "A-1", "build", "claude", self.herdr, opts, envs=self.envs, sleep=lambda s: None, procs=procs)
        self.assertIn("Its pane is closed, and the script stopped 2 processes it left running (pid 4001 `sleep 4271`", str(caught.exception))
        self.assertEqual(self.attempts()["build"], 0)
