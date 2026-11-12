"""Staging a program from its spec, with real git against local bare
repositories and the real kit as the thing installed."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hl import items, program, stage
from hl.common import Refusal, read_json, write_json

from .support import BACKLOG_A, BACKLOG_B, KIT


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}  # fmt: skip
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


class StageCase(unittest.TestCase):
    TASK = "tasks/portal"
    BRANCH = "portal-ledger"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.root = self.tmp / "loops" / "helpdesk"
        self.remotes = self.tmp / "remotes"
        # The host's Claude sign-ins: the default, which no loop runs on, and two seats beside it.
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.seats = [str(self.home / ".claude-seat-a"), str(self.home / ".claude-seat-b")]
        for seat_dir in self.seats:
            self.sign_in(seat_dir)
        for repo in ("owner/desk", "owner/pages"):
            self.make_target(repo)
        self.spec = {
            "program": "helpdesk",
            "root": str(self.root),
            "ledger_branch": self.BRANCH,
            "repo": "owner/desk",
            "base": "main",
            "remote_base": str(self.remotes),
            "streams": [
                {
                    "name": "portal",
                    "writable": "`portal/**`",
                    "ports": "Port for item `A-n`: `840n`.",
                    "run": {"id": "portal-1", "work": "Help desk portal", "file": "A-ITEMS.md"},
                }
            ],
            "settings": {"ntfy_url": "https://ntfy.example.invalid/loop-topic", "claude_seats": list(self.seats)},
            "prompt": {
                "FILES_AND_GATES": "  1. `{{TASK_DIR}}/A-ITEMS.md`: run id `portal-1`, run directory `{{ROOT}}/runs/portal-1`.",
                "PRODUCTION_BOUNDARY": "Nothing here is production.",
                "CAPACITY": "Two streams at most; measure before every spawn.",
                "ACCOUNT_SHARING": "Nobody else uses the workers' account.",
            },
        }
        # The control repository: guardrails, the backlog files and the spec. The kit is passed in.
        self.workspace_origin = self.remotes / "workspace.git"
        git(self.tmp, "init", "--quiet", "--bare", "-b", "main", str(self.workspace_origin))
        self.ledger = self.root / "ledger"
        self.ledger.parent.mkdir(parents=True)
        git(self.tmp, "clone", "--quiet", str(self.workspace_origin), str(self.ledger))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.ledger, "config", key, value)
        git(self.ledger, "checkout", "--quiet", "-b", self.BRANCH)
        (self.ledger / "CLAUDE.md").write_text("# guardrails\n")
        task = self.ledger / self.TASK
        task.mkdir(parents=True)
        (task / "A-ITEMS.md").write_text(BACKLOG_A)
        (task / "B-ITEMS.md").write_text(BACKLOG_B)
        self.spec_path = task / "program.spec.json"
        self.write_spec()
        git(self.ledger, "add", ".")
        git(self.ledger, "commit", "--quiet", "-m", "the program's files")
        # Last, so the git set-up above ran under the real HOME: staging checks the seats against this one.
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def sign_in(path):
        Path(path).mkdir(parents=True, exist_ok=True)
        write_json(Path(path) / ".claude.json", {"oauthAccount": {"emailAddress": f"{Path(path).name}@example.invalid"}})

    def make_target(self, repo):
        bare = self.remotes / f"{repo}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        git(self.tmp, "init", "--quiet", "--bare", "-b", "main", str(bare))
        seed = self.tmp / "seed" / repo.replace("/", "-")
        git(self.tmp, "clone", "--quiet", str(bare), str(seed))
        (seed / "README.md").write_text("base\n")
        git(seed, "add", "README.md")
        git(seed, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "base")
        git(seed, "push", "--quiet", "origin", "HEAD:main")

    def write_spec(self):
        write_json(self.spec_path, self.spec)

    def stage(self):
        return stage.stage(self.spec_path, kit_source=KIT, run_tests=False)

    def refusal(self):
        with self.assertRaises(Refusal) as caught:
            self.stage()
        return caught.exception.reasons

    @property
    def clone(self):
        return self.root / "repos" / "portal" / "desk"


class FreshStageTest(StageCase):
    def test_a_fresh_stage_makes_everything_a_manager_needs(self):
        out = self.stage()
        kit = self.root / ".claude" / "skills" / "herdr-loop"
        self.assertTrue(os.access(kit / "scripts" / "hl-spawn", os.X_OK))
        self.assertEqual((self.root / "KIT-COMMIT").read_text().strip(), out["kit_commit"])
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "# guardrails\n")

        cfg = program.load(self.root)
        self.assertEqual((cfg["program"], cfg["base"], cfg["ntfy_url"]), ("helpdesk", "main", "https://ntfy.example.invalid/loop-topic"))

        self.assertEqual(git(self.clone, "rev-parse", "HEAD"), git(self.clone, "rev-parse", "origin/main"))
        # Detached: the primary checkout is on no branch.
        self.assertEqual(git(self.clone, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertTrue(os.access(self.clone / ".git" / "hooks" / "commit-msg", os.X_OK))
        excluded = (self.clone / ".git" / "info" / "exclude").read_text().splitlines()
        for line in (".worktrees/", ".playwright-mcp/", ".scratch/"):
            self.assertIn(line, excluded)

        run = items.load_run(self.root / "runs" / "portal-1")
        self.assertEqual((run["work"], run["repo"], run["order"]), ("Help desk portal", "owner/desk", ["A-1", "A-2"]))
        # A new program's runs start routed, with a third build allowed, and know their stream.
        self.assertEqual((run["policy"], run["stream"]), ({"premise": "routed", "review": "code", "bounds": {"build": 3}}, "portal"))
        self.assertEqual(out["runs"], [{"stream": "portal", "run": "portal-1", "work": "Help desk portal", "run_dir": str(self.root / "runs" / "portal-1"), "state": "ready", "next": "A-1"}])  # fmt: skip
        self.assertIn("hl-watchdog --root " + str(self.root), out["start"])
        self.assertEqual((out["open_workers"], out["warning"]), ([], None))

    def test_the_clone_refuses_a_commit_that_names_a_tool(self):
        self.stage()
        hook = self.clone / ".git" / "hooks" / "commit-msg"
        self.assertEqual(hook.read_text(), (KIT / "scripts" / "hooks" / "commit-msg").read_text())
        (self.clone / "note.txt").write_text("x\n")
        git(self.clone, "add", "note.txt")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}  # fmt: skip
        message = "add a note\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n"
        refused = subprocess.run(["git", "-C", str(self.clone), "-c", "commit.gpgsign=false", "commit", "-m", message], capture_output=True, text=True, env=env)
        self.assertNotEqual(refused.returncode, 0)
        plain = subprocess.run(["git", "-C", str(self.clone), "-c", "commit.gpgsign=false", "commit", "-m", "add a note"], capture_output=True, text=True, env=env)
        self.assertEqual(plain.returncode, 0, plain.stderr)

    def test_the_prompt_is_filled_from_the_spec_with_nothing_left_open(self):
        self.stage()
        prompt = (self.root / "MANAGER-PROMPT.md").read_text()
        self.assertNotIn("{{", prompt)
        self.assertTrue(prompt.startswith("You are the manager of a backlog run for helpdesk."))
        task = self.ledger / self.TASK
        self.assertIn(f"  1. `{task}/A-ITEMS.md`: run id `portal-1`, run directory `{self.root}/runs/portal-1`.", prompt)
        self.assertIn(f"- `portal` (Help desk portal): repository `owner/desk`, clone `{self.clone}`. Writable paths: `portal/**`. Port for item `A-n`: `840n`.", prompt)  # fmt: skip
        self.assertIn(f"`{task}/LEDGER.md`", prompt)
        self.assertIn("ntfy topic `loop-topic`", prompt)
        self.assertIn("Nothing here is production.", prompt)

    def test_the_ledger_is_started_committed_and_pushed_on_the_programs_branch(self):
        out = self.stage()
        self.assertTrue(out["ledger_created"])
        text = (self.ledger / self.TASK / "LEDGER.md").read_text()
        self.assertTrue(text.startswith("# Ledger: helpdesk\n"))
        self.assertNotIn("{{", text)
        self.assertEqual(git(self.ledger, "status", "--porcelain"), "")
        self.assertEqual(git(self.workspace_origin, "rev-parse", self.BRANCH), git(self.ledger, "rev-parse", "HEAD"))
        self.assertEqual(git(self.ledger, "log", "-1", "--format=%s"), "helpdesk: start the ledger")

    def test_streams_may_have_their_own_repositories_and_each_gets_a_clone_and_a_run(self):
        self.spec["streams"].append(
            {
                "name": "pages",
                "repo": "owner/pages",
                "writable": "`agent/**`",
                "ports": "Port for item `B-n`: `850n`.",
                "run": {"id": "pages-1", "work": "Help desk agent pages", "file": "B-ITEMS.md"},
            }
        )
        self.write_spec()
        out = self.stage()
        self.assertEqual([c["clone"] for c in out["clones"]], [str(self.clone), str(self.root / "repos" / "pages" / "pages")])
        self.assertEqual([(r["run"], r["work"]) for r in out["runs"]], [("portal-1", "Help desk portal"), ("pages-1", "Help desk agent pages")])
        self.assertEqual(items.load_run(self.root / "runs" / "pages-1")["repo"], "owner/pages")
        self.assertIn("- `pages` (Help desk agent pages): repository `owner/pages`", (self.root / "MANAGER-PROMPT.md").read_text())


class PolicyStageTest(StageCase):
    def test_a_spec_can_keep_the_first_behaviour_for_its_runs(self):
        self.spec["settings"].update(premise="always", review="blind", bounds={})
        self.write_spec()
        self.stage()
        run = items.load_run(self.root / "runs" / "portal-1")
        self.assertEqual(items.policy(run), {"premise": "always", "review": "blind", "bounds": {}, "merge": "item", "train": {}, "confirm": "full", "confirm_bound": {}, "exemptable": []})
        self.assertEqual(program.load(self.root)["premise"], "always")

    def test_a_spec_can_give_its_runs_a_delta_confirm_of_a_rework(self):
        self.spec["settings"].update(confirm="delta", confirm_bound={"lines": 60})
        self.write_spec()
        self.stage()
        policy = items.policy(items.load_run(self.root / "runs" / "portal-1"))
        self.assertEqual((policy["confirm"], policy["confirm_bound"]), ("delta", {"share": 0.25, "lines": 60, "files": 2}))
        self.assertEqual(program.load(self.root)["confirm"], "delta")

    def test_a_confirm_setting_that_cannot_work_stops_the_stage_before_anything_is_made(self):
        for settings, why in (
            ({"confirm": "delta", "review": "blind"}, "settings: a delta confirm of a rework needs reviews that read the code and write a fix list (`review: code`)"),
            ({"confirm": "quick"}, "settings: confirm 'quick' is not one of full, delta"),
            ({"confirm": "delta", "confirm_bound": {"share": 2}}, "settings: `confirm_bound` takes `share` (above 0, at most 1)"),
        ):
            self.spec["settings"] = {**self.spec["settings"], "review": "code", "confirm_bound": {}, **settings}
            self.write_spec()
            self.assertIn(why, "; ".join(self.refusal()), settings)
            self.assertFalse((self.root / "program.json").exists())

    def test_a_policy_that_makes_no_sense_stops_the_stage(self):
        self.spec["settings"]["premise"] = "sometimes"
        self.write_spec()
        self.assertIn("premise policy 'sometimes' is not one of always, routed", self.refusal()[0])


class SpecFactsTest(StageCase):
    """What a spec says once about a stream and a repository reaches the settings the scripts read."""

    def test_streams_repository_facts_and_the_ledger_are_written_into_the_settings(self):
        self.spec["streams"][0]["port_base"] = 8400
        self.spec["streams"][0]["setup"] = False
        self.spec["repos"] = {
            "owner/desk": {
                "facts": ["CI uses Node 22."],
                "setup": "make deps",
                "setup_inputs": ["package-lock.json"],
                "ci_covers": ["`app-tests`: the frontend gate, then pytest."],
                "not_in_ci": ["`pytest tests/extended`."],
                "seen": ["web/**"],
                "protected": ["brand/family/**"],
                "rewrites": ["next-env.d.ts"],
            }
        }
        self.write_spec()
        self.stage()
        cfg = program.load(self.root)
        self.assertEqual(cfg["repos"]["owner/desk"]["rewrites"], ["next-env.d.ts"])
        self.assertEqual(cfg["streams"], {"portal": {"repo": "owner/desk", "clone": str(self.clone), "port_base": 8400, "setup": False}})
        self.assertEqual(cfg["repos"]["owner/desk"]["setup"], "make deps")
        self.assertEqual(cfg["ledger"], str(self.ledger / self.TASK / "LEDGER.md"))
        # The manager's launch line comes with the settings: only the tools it uses, none of the host's servers or skills.
        self.assertEqual(cfg["manager_args"][:2], ["--tools", "Bash,Read,Edit,Write,Agent,TaskStop"])
        self.assertIn("--strict-mcp-config", cfg["manager_args"])

    def test_remarks_on_the_accept_lists_come_back_with_the_stage_and_stop_nothing(self):
        out = self.stage()
        # The fixture's first item asks for `make test`, which is nobody's whole suite by name, and draws no remark.
        self.assertEqual(out["accept_remarks"], [])
        (self.ledger / self.TASK / "A-ITEMS.md").write_text("## A-1 — x\n\n**Scope.** s\n\n**Accept.**\n1. `gh pr checks 7 --required` lists `ci` as `pass`.\n")
        git(self.ledger, "add", ".")
        git(self.ledger, "commit", "--quiet", "-m", "another backlog")
        self.root.joinpath("runs").mkdir(exist_ok=True)
        import shutil

        shutil.rmtree(self.root / "runs")
        out = self.stage()
        self.assertEqual(out["accept_remarks"], ["A-1 criterion 1 asks for the required checks: the merge holds them green"])
        self.assertEqual(out["runs"][0]["state"], "ready")

    def test_prose_that_hands_a_gate_to_a_person_draws_a_remark_and_stops_nothing(self):
        out = self.stage()
        self.assertEqual(out["gate_remarks"], [])
        self.spec["prompt"]["FILES_AND_GATES"] = "Gates held by the authoring session. You wait on these and never do them."
        self.spec["prompt"]["PRODUCTION_BOUNDARY"] = "Out of bounds: AWS root credentials, and no loop worker or manager uploads, ever."
        self.write_spec()
        out = self.stage()
        self.assertEqual(out["gate_remarks"], [
            "FILES_AND_GATES names the authoring session as a party: a gate is the manager's, done the loop's way",
            "FILES_AND_GATES tells the manager never to do a gate",
            "PRODUCTION_BOUNDARY puts a credential out of the manager's reach: it may use any credential the vault holds",
            "PRODUCTION_BOUNDARY forbids the manager an upload: uploads are the manager's; submitting for review is the operator's",
        ])
        self.assertTrue((self.root / "MANAGER-PROMPT.md").exists())

    def test_a_wait_in_the_specs_gates_or_the_backlogs_it_names_draws_a_remark_and_stops_nothing(self):
        # the help-desk catalog program, 2026-11-10: an item built only if a queue had not drained in a day, and a run held until
        # another closed, stood every stream idle for four hours. Staging says where a spec or its files wait so.
        out = self.stage()
        self.assertEqual(out["clock_remarks"], [])
        self.spec["prompt"]["FILES_AND_GATES"] += (
            " Then `{{TASK_DIR}}/B-ITEMS.md`, after run `portal-1` closes. G-QUEUE: read the queue again after 24 h."
            " `{{TASK_DIR}}/GUIDE.md` is the program's guide, and `{{TASK_DIR}}/GONE.md` is not written yet."
        )
        self.write_spec()
        task = self.ledger / self.TASK
        (task / "GUIDE.md").write_text("# Guide\n\nRead the count a day later.\n")
        (task / "B-ITEMS.md").write_text(
            "# B\n\nB-1 waits for the drain reading.\n\n"
            "## B-1 — If the queue does not drain in a day, raise the limit\n\n"
            "**Scope.** The reminder goes 3 days after the request; that is the product, not a wait.\n\n"
            "**Accept.**\n1. It works.\n\n**Verify.** `true`\nWait. 60\n"
        )
        out = self.stage()
        wait, clock, edge = (stage.clocks.REMARK[k] for k in ("wait", "clock", "edge"))
        said = out["clock_remarks"]
        self.assertEqual([r.split(" (\"")[0] for r in said], [
            f"FILES_AND_GATES {edge}",
            f"FILES_AND_GATES {clock}",
            f"B-ITEMS.md header {wait}",
            f"B-1 {wait}",
        ])
        for remark, words in zip(said, ("after run `portal-1` closes", "again after 24 h", "B-1 waits for the drain reading.", "If the queue does not drain in a day")):
            self.assertIn(words, remark)
        # The guide is no backlog, a file that does not exist is nobody's, and nothing was refused.
        self.assertEqual(stage.named_backlogs(self.spec, task), ["A-ITEMS.md", "B-ITEMS.md"])
        self.assertTrue((self.root / "MANAGER-PROMPT.md").exists())

    def test_a_spec_with_no_facts_stages_as_before(self):
        self.stage()
        cfg = program.load(self.root)
        self.assertEqual((cfg["repos"], cfg["streams"]["portal"]["clone"]), ({}, str(self.clone)))
        self.assertNotIn("port_base", cfg["streams"]["portal"])

    def test_facts_the_kit_does_not_read_or_cannot_use_stop_the_stage(self):
        self.spec["streams"][0]["port_base"] = 80
        self.spec["streams"][0]["setup"] = "make deps"
        self.spec["repos"] = {
            "owner/desk": {"setup": ["make", "deps"], "seen": "web/**", "tests": ["pytest"], "setup_inputs": ["x.lock"]},
            "owner/elsewhere": {"facts": ["x"]},
            "owner/pages": "fast",
        }
        self.write_spec()
        said = " | ".join(self.refusal())
        for part in (
            "streams[0]: `port_base` must be a port number of 1024 or more, got 80",
            "streams[0]: `setup` may only be `false`",
            "repos[owner/desk]: `setup` must be one command line",
            "repos[owner/desk]: `seen` must be a list of strings",
            "repos[owner/desk]: `tests` is not a fact the kit reads",
            "repos: 'owner/elsewhere' is not the repository of any stream",
            "repos[owner/pages]: must be an object",
        ):
            self.assertIn(part, said)
        self.assertFalse((self.root / "program.json").exists())

    def test_a_repository_with_a_train_gives_its_runs_the_train(self):
        self.spec["repos"] = {"owner/desk": {"train": {"batch": 3}, "item_checks": ["Lint and Validate Syntax"]}}
        self.write_spec()
        self.stage()
        cfg = program.load(self.root)
        self.assertEqual(program.train_settings(cfg, "owner/desk"), {"batch": 3, "limit": 15})
        policy = items.policy(items.load_run(self.root / "runs" / "portal-1"))
        self.assertEqual((policy["merge"], policy["train"]), ("train", {"branch": "hl-train/helpdesk", "item_checks": ["Lint and Validate Syntax"]}))

    def test_a_train_that_makes_no_sense_stops_the_stage(self):
        for facts, why in (
            ({"train": True}, "`train` must be an object with whole numbers `batch` and `limit`"),
            ({"train": {"batch": 0}}, "`train` must be an object with whole numbers"),
            ({"train": {"batch": 2.5}}, "`train` must be an object with whole numbers"),
            ({"train": {"size": 5}}, "`train` must be an object with whole numbers"),
            ({"train": {"batch": 6, "limit": 5}}, "cannot be less than its `batch`"),
            ({"train": {"limit": 3}}, "cannot be less than its `batch`"),
            ({"train": {}, "item_checks": "lint"}, "`item_checks` must be a list of strings"),
            ({"item_checks": ["lint"]}, "`item_checks` without `train`"),
        ):
            self.spec["repos"] = {"owner/desk": facts}
            self.write_spec()
            self.assertIn(why, " | ".join(self.refusal()), facts)
        self.spec["repos"] = {"owner/desk": {"train": {}}}
        self.spec["settings"]["review"] = "blind"
        self.write_spec()
        self.assertIn("a train needs reviews that read the code", " | ".join(self.refusal()))
        self.assertFalse((self.root / "program.json").exists())

    def test_a_pattern_that_names_every_path_as_one_the_repository_rewrites_is_refused(self):
        # Such a pattern would let a review start over whatever its build wrote.
        for pattern in ("*", "**", "**/*"):
            self.spec["repos"] = {"owner/desk": {"rewrites": ["next-env.d.ts", pattern]}}
            self.write_spec()
            with self.assertRaises(Refusal) as caught:
                self.stage()
            self.assertIn(f"repos[owner/desk]: `rewrites` pattern {pattern!r} matches every path; name the files the repository rewrites by itself", str(caught.exception))
            self.assertFalse((self.root / "program.json").exists())
        self.spec["repos"] = {"owner/desk": {"rewrites": ["next-env.d.ts", "src/generated/", "*.lock"]}}
        self.write_spec()
        self.stage()
        self.assertEqual(program.load(self.root)["repos"]["owner/desk"]["rewrites"], ["next-env.d.ts", "src/generated/", "*.lock"])

    def test_a_reference_directory_is_one_relative_directory_and_reaches_the_settings(self):
        for bad in ("", "/", "/abs/brand", "brand/*", "../brand", "brand/../x", ["brand"], 3):
            self.spec["repos"] = {"owner/desk": {"reference": bad}}
            self.write_spec()
            self.assertIn("repos[owner/desk]: `reference` must be one directory of the repository, as a relative path (`brand`)", " | ".join(self.refusal()), bad)
            self.assertFalse((self.root / "program.json").exists())
        self.spec["repos"] = {"owner/desk": {"reference": "brand/family"}}
        self.write_spec()
        self.stage()
        self.assertEqual(program.repo_facts(program.load(self.root), "owner/desk")["reference"], "brand/family")

    def test_what_may_be_exempted_is_fixed_into_each_run_when_it_is_created(self):
        self.spec["repos"] = {"owner/desk": {"exemptable": ["brand/x"]}}
        self.write_spec()
        self.assertIn("repos[owner/desk]: `exemptable` without `protected`", " | ".join(self.refusal()))
        self.spec["repos"] = {"owner/desk": {"protected": [".github/workflows/CICD.yml", "brand/**"], "exemptable": ["brand/**"]}}
        self.write_spec()
        self.stage()
        run = items.load_run(self.root / "runs" / "portal-1")
        self.assertEqual(items.policy(run)["exemptable"], ["brand/**"])
        # The program's settings changing later changes nothing for the run: it keeps the policy it was created with.
        cfg = read_json(self.root / "program.json")
        cfg["repos"]["owner/desk"]["exemptable"] = [".github/workflows/CICD.yml"]
        write_json(self.root / "program.json", cfg)
        self.assertEqual(items.policy(items.load_run(self.root / "runs" / "portal-1"))["exemptable"], ["brand/**"])
        # A spec that names none gives its runs none: the kit before exemptions.
        self.assertEqual(items.policy({"policy": program.run_policy({"repos": {}}, "owner/desk")})["exemptable"], [])

    def test_setup_inputs_without_a_setup_say_nothing_and_are_refused(self):
        self.spec["repos"] = {"owner/desk": {"setup_inputs": ["x.lock"]}}
        self.write_spec()
        self.assertIn("repos[owner/desk]: `setup_inputs` without `setup`", self.refusal()[0])

    def test_a_base_only_clone_holds_the_base_and_the_loops_own_branches_and_nothing_else(self):
        other = self.tmp / "seed" / "owner-desk"
        git(other, "checkout", "--quiet", "-b", "someone-elses-branch")
        (other / "secret-plan.md").write_text("another arm's work\n")
        git(other, "add", "secret-plan.md")
        git(other, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "other work")
        git(other, "push", "--quiet", "origin", "someone-elses-branch")
        git(other, "checkout", "--quiet", "-b", "hl/portal-1/A-1", "main")
        git(other, "push", "--quiet", "origin", "hl/portal-1/A-1")
        self.spec["base_only"] = True
        self.write_spec()
        self.stage()
        refs = git(self.clone, "for-each-ref", "--format=%(refname)", "refs/remotes").split()
        self.assertIn("refs/remotes/origin/main", refs)
        self.assertIn("refs/remotes/origin/hl/portal-1/A-1", refs)
        self.assertNotIn("refs/remotes/origin/someone-elses-branch", refs)
        # And a later fetch does not bring it in.
        git(self.clone, "fetch", "--quiet", "origin")
        self.assertNotIn("refs/remotes/origin/someone-elses-branch", git(self.clone, "for-each-ref", "--format=%(refname)", "refs/remotes").split())


class OutsideStageTest(StageCase):
    """`outside_prs` and a repository's `deploys`: what lets a program merge a pull request a steward marked ready, and read its deploy."""

    def test_a_new_program_adopts_nothing_unless_its_spec_says_adopt(self):
        self.stage()
        self.assertEqual(program.load(self.root)["outside_prs"], "off")
        self.assertEqual(read_json(KIT / "references" / "program.template.json")["outside_prs"], "off")
        self.spec["settings"]["outside_prs"] = "adopt"
        self.write_spec()
        git(self.ledger, "commit", "--quiet", "-am", "adopt")
        self.stage()
        self.assertEqual(program.load(self.root)["outside_prs"], "adopt")

    def test_a_program_staged_again_keeps_its_setting_whatever_the_template_says(self):
        # A live program staged before the setting existed has none: staged again, it still adopts nothing.
        self.stage()
        settings = read_json(self.root / "program.json")
        settings.pop("outside_prs")
        write_json(self.root / "program.json", settings)
        self.stage()
        self.assertEqual(program.load(self.root)["outside_prs"], "off")
        # Its spec may still turn it on outright.
        self.spec["settings"]["outside_prs"] = "adopt"
        self.write_spec()
        self.stage()
        self.assertEqual(program.load(self.root)["outside_prs"], "adopt")
        # And once on, a spec that stops saying so leaves it on: the template's `off` is for new programs only.
        del self.spec["settings"]["outside_prs"]
        self.write_spec()
        self.stage()
        self.assertEqual(program.load(self.root)["outside_prs"], "adopt")

    def test_a_value_the_kit_does_not_know_is_refused(self):
        self.spec["settings"]["outside_prs"] = "yes"
        self.write_spec()
        self.assertTrue(any("`outside_prs` must be one of off, adopt" in r for r in self.refusal()))
        self.spec["settings"]["outside_prs"] = "adopt"
        self.spec["settings"]["outside_pr_vetters"] = "steward-bot"
        self.write_spec()
        self.assertTrue(any("`outside_pr_vetters` must be a list of GitHub logins" in r for r in self.refusal()))
        self.spec["settings"]["outside_pr_vetters"] = ["steward-bot"]
        self.spec["settings"]["outside_pr_shared_login"] = "sure"
        self.write_spec()
        self.assertTrue(any("`outside_pr_shared_login` must be one of refuse, accept" in r for r in self.refusal()))

    def test_the_stewards_logins_and_a_shared_login_acceptance_come_from_the_spec_alone(self):
        self.spec["settings"]["outside_pr_vetters"] = ["steward-bot"]
        self.write_spec()
        self.stage()
        self.assertEqual(program.load(self.root)["outside_pr_vetters"], ["steward-bot"])
        # Taken from the spec alone: removing them there revokes them, and so for a shared-login acceptance.
        self.spec["settings"]["outside_pr_shared_login"] = "accept"
        self.write_spec()
        self.stage()
        self.assertEqual(program.load(self.root)["outside_pr_shared_login"], "accept")
        del self.spec["settings"]["outside_pr_vetters"]
        del self.spec["settings"]["outside_pr_shared_login"]
        self.write_spec()
        self.stage()
        self.assertEqual((program.load(self.root)["outside_pr_vetters"], program.load(self.root)["outside_pr_shared_login"]), ([], "refuse"))

    def test_a_repositorys_deploy_workflows_reach_the_settings(self):
        self.spec["repos"] = {"owner/desk": {"deploys": ["apply-main.yml"]}}
        self.write_spec()
        self.stage()
        self.assertEqual(program.repo_facts(program.load(self.root), "owner/desk")["deploys"], ["apply-main.yml"])

    def test_a_program_json_without_the_setting_adopts_nothing(self):
        self.assertEqual(program.DEFAULTS["outside_prs"], "off")


class AgainTest(StageCase):
    def test_staging_again_installs_the_newer_kit_and_keeps_the_ledger_the_runs_and_the_worktrees(self):
        self.stage()
        kit = self.root / ".claude" / "skills" / "herdr-loop"
        (kit / "scripts" / "dropped-by-the-newer-kit").write_text("stale\n")
        ledger_file = self.ledger / self.TASK / "LEDGER.md"
        ledger_file.write_text(ledger_file.read_text() + "\nThe manager wrote this.\n")
        git(self.ledger, "commit", "--quiet", "-am", "ledger: saved")
        run_dir = self.root / "runs" / "portal-1"
        items.contract_from_accept(run_dir, "A-1")
        items.start_phase(run_dir, "A-1", "agree", session="s1")
        worktree = self.clone / ".worktrees" / "portal-1-A-1"
        git(self.clone, "worktree", "add", "--quiet", "-b", "hl/portal-1/A-1", str(worktree), "origin/main")
        (worktree / "half-built.txt").write_text("work in progress\n")
        (self.root / "MANAGER-PROMPT.md").write_text("an older prompt\n")

        out = self.stage()

        self.assertFalse((kit / "scripts" / "dropped-by-the-newer-kit").exists())
        self.assertFalse(out["ledger_created"])
        self.assertIn("The manager wrote this.", ledger_file.read_text())
        self.assertEqual(items.load_item(run_dir, "A-1")["status"], "in_progress")
        self.assertEqual(out["runs"][0]["state"], "resume")
        self.assertEqual((worktree / "half-built.txt").read_text(), "work in progress\n")
        self.assertTrue((self.root / "MANAGER-PROMPT.md").read_text().startswith("You are the manager"))
        self.assertEqual(git(self.workspace_origin, "rev-parse", self.BRANCH), git(self.ledger, "rev-parse", "HEAD"))

    def test_a_verify_line_corrected_since_the_run_began_is_named_and_left_to_load(self):
        self.stage()
        run_dir = self.root / "runs" / "portal-1"
        items_file = self.ledger / self.TASK / "A-ITEMS.md"
        items_file.write_text(items_file.read_text().replace("**Verify.** `true`", "**Verify.** `echo deployed`"))
        git(self.ledger, "commit", "--quiet", "-am", "operator: correct A-1's Verify line")

        out = self.stage()

        self.assertEqual(items.load_item(run_dir, "A-1")["verify_command"], "true")
        self.assertEqual(out["runs"][0]["verify_lines_to_load"], [f"hl-items --run-dir {run_dir} verify-line --id A-1 --note <what was wrong>"])
        items.reload_verify(run_dir, "A-1", "the line was wrong")
        self.assertNotIn("verify_lines_to_load", self.stage()["runs"][0])

    def test_any_other_edit_to_a_running_runs_file_still_refuses(self):
        self.stage()
        items_file = self.ledger / self.TASK / "A-ITEMS.md"
        items_file.write_text(items_file.read_text().replace("Build the first thing.", "Build something else."))
        git(self.ledger, "commit", "--quiet", "-am", "an edit under a running run")
        self.assertTrue(any("outside the Verify line" in r for r in self.refusal()))

    def test_a_worker_that_is_still_open_is_named_in_the_answer(self):
        self.stage()
        record = self.root / "runs" / "portal-1" / "items" / "A-1" / "build-1.worker.json"
        write_json(record, {"agent": "helpdesk-a1-b1", "closed_at": None})
        out = self.stage()
        self.assertEqual(out["open_workers"], ["runs/portal-1/items/A-1/build-1.worker.json"])
        self.assertIn("between items", out["warning"])
        write_json(record, {"agent": "helpdesk-a1-b1", "closed_at": "2026-10-11T00:00:00Z"})
        self.assertIsNone(self.stage()["warning"])

    def test_a_primary_checkout_someone_left_dirty_is_not_moved(self):
        self.stage()
        was = git(self.clone, "rev-parse", "HEAD")
        # The base moves on in the same file that someone edited in the primary checkout.
        seed = self.tmp / "seed" / "owner-desk"
        (seed / "README.md").write_text("base, changed upstream\n")
        git(seed, "-c", "commit.gpgsign=false", "commit", "--quiet", "-am", "upstream")
        git(seed, "push", "--quiet", "origin", "HEAD:main")
        (self.clone / "README.md").write_text("edited in the primary checkout\n")
        self.stage()
        self.assertEqual((self.clone / "README.md").read_text(), "edited in the primary checkout\n")
        self.assertEqual(git(self.clone, "rev-parse", "HEAD"), was)
        self.assertNotEqual(git(self.clone, "rev-parse", "origin/main"), was)

    def test_a_clean_primary_checkout_follows_the_base(self):
        self.stage()
        seed = self.tmp / "seed" / "owner-desk"
        (seed / "more.txt").write_text("more\n")
        git(seed, "add", "more.txt")
        git(seed, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "upstream")
        git(seed, "push", "--quiet", "origin", "HEAD:main")
        out = self.stage()
        self.assertEqual(git(self.clone, "rev-parse", "HEAD"), git(seed, "rev-parse", "HEAD"))
        self.assertEqual(out["clones"][0]["head"], git(seed, "rev-parse", "--short", "HEAD"))


class RefusalTest(StageCase):
    def test_an_incomplete_spec_names_everything_that_is_missing(self):
        del self.spec["streams"][0]["writable"]
        del self.spec["streams"][0]["run"]["work"]
        del self.spec["prompt"]["CAPACITY"]
        self.write_spec()
        said = " | ".join(self.refusal())
        for part in ("streams[0]: `writable` is missing", "streams[0].run: `work` is missing", "prompt: `CAPACITY` is missing"):
            self.assertIn(part, said)
        self.assertFalse((self.root / "program.json").exists())

    def test_a_root_that_is_not_an_absolute_path_is_refused(self):
        self.spec["root"] = "loops/helpdesk"
        self.write_spec()
        self.assertIn("`root` must be an absolute path", self.refusal()[0])

    def test_a_spec_that_is_not_in_the_programs_ledger_clone_is_refused(self):
        self.spec["root"] = str(self.tmp / "loops" / "elsewhere")
        self.write_spec()
        self.assertIn("run scripts/stage.sh, which clones the ledger there first", self.refusal()[0])

    def test_a_ledger_clone_on_another_branch_is_refused(self):
        self.spec["ledger_branch"] = "some-other-branch"
        self.write_spec()
        self.assertIn("the ledger clone is on 'portal-ledger'; the spec names 'some-other-branch'", self.refusal()[0])

    def test_a_backlog_file_the_spec_names_must_exist(self):
        self.spec["streams"][0]["run"]["file"] = "MISSING-ITEMS.md"
        self.write_spec()
        self.assertIn("MISSING-ITEMS.md does not exist in the ledger clone", self.refusal()[0])
        self.assertFalse((self.root / "KIT-COMMIT").exists())

    def test_a_prompt_placeholder_nobody_gave_a_value_for_is_refused(self):
        self.spec["prompt"]["CAPACITY"] = "See {{CAPACITY_TABLE}}."
        self.write_spec()
        self.assertIn("the manager prompt: no value for {{CAPACITY_TABLE}}", self.refusal()[0])

    def test_two_streams_cannot_share_a_name(self):
        self.spec["streams"].append(dict(self.spec["streams"][0]))
        self.write_spec()
        self.assertIn("the name 'portal' is used twice", " | ".join(self.refusal()))

    def test_the_command_line_exits_2_on_a_refusal_and_0_with_the_summary(self):
        def cli(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = stage.main(["--spec", str(self.spec_path), "--kit-source", str(KIT), "--no-tests", *argv])
            return code, json.loads(out.getvalue())

        code, out = cli()
        self.assertEqual((code, out["staged"], out["tests"]), (0, str(self.root), []))
        self.spec_path.write_text(json.dumps({"program": "x"}))
        code, out = cli()
        self.assertEqual(code, 2)
        self.assertIn("`root` is missing", " | ".join(out["refused"]))


class KitTestsTest(StageCase):
    def scripts(self, name, body):
        scripts = self.tmp / name / "scripts"
        (scripts / "tests").mkdir(parents=True)
        (scripts / "tests" / "__init__.py").write_text("")
        (scripts / "tests" / "test_one.py").write_text(f"import unittest\n\n\nclass T(unittest.TestCase):\n    def test_it(self):\n        {body}\n")
        return scripts

    def test_the_installed_kits_tests_are_run_and_their_last_line_kept(self):
        good = stage.kit_tests(self.scripts("good-kit", "pass"), [sys.executable])
        self.assertEqual(good, [{"python": sys.executable, "passed": True, "result": "OK"}])
        bad = stage.kit_tests(self.scripts("bad-kit", "self.fail('broken')"), [sys.executable])
        self.assertEqual((bad[0]["passed"], bad[0]["result"]), (False, "FAILED (failures=1)"))

    def test_a_python_that_is_not_on_this_machine_is_left_out(self):
        self.assertEqual(stage.kit_tests(self.scripts("any-kit", "pass"), ["/no/such/python3"]), [])

    def test_a_kit_whose_tests_fail_is_not_reported_as_staged(self):
        bad = [{"python": "/usr/bin/python3", "passed": False, "result": "FAILED (failures=2)"}]
        with mock.patch("hl.stage.kit_tests", return_value=bad) as ran:
            with self.assertRaises(Refusal) as caught:
                stage.stage(self.spec_path, kit_source=KIT, run_tests=True)
        ran.assert_called_once_with(self.root / ".claude" / "skills" / "herdr-loop" / "scripts")
        self.assertIn("the installed kit's tests fail under /usr/bin/python3: FAILED (failures=2)", caught.exception.reasons[0])


class SpecTemplateTest(unittest.TestCase):
    def test_the_template_has_every_field_the_stager_requires(self):
        template = read_json(KIT / "references" / "program.spec.template.json")
        for key in ("program", "root", "ledger_branch", "streams", "prompt"):
            self.assertIn(key, template)
        for key in stage.PROMPT_REQUIRED:
            self.assertIn(key, template["prompt"])
        for key in ("name", "writable", "ports", "run"):
            self.assertIn(key, template["streams"][0])
        self.assertEqual(sorted(template["streams"][0]["run"]), ["file", "id", "work"])


class ClaudeHomeStageTest(StageCase):
    """`claude_config_dir`: the Claude sign-in a program's sessions run on, checked where the program runs."""

    def with_home(self, value, signed_in=True):
        if signed_in and value:
            path = Path(os.path.expanduser(value))
            path.mkdir(parents=True, exist_ok=True)
            write_json(path / ".claude.json", {"oauthAccount": {"emailAddress": "loops@example.invalid"}})
        self.spec["settings"] = {**self.spec["settings"], "claude_config_dir": value}
        self.write_spec()

    def test_a_signed_in_directory_beside_the_default_is_staged_and_named_in_the_prompt(self):
        team = str(self.home / ".claude-team")
        self.with_home(team)
        self.stage()
        self.assertEqual(read_json(self.root / "program.json")["claude_config_dir"], team)
        prompt = (self.root / "MANAGER-PROMPT.md").read_text()
        self.assertIn(f"are on the sign-in in `{team}`", prompt)
        self.assertNotIn("{{CLAUDE_ACCOUNT}}", prompt)

    def test_without_the_setting_the_program_starts_on_its_first_seat_and_the_prompt_names_every_seat(self):
        self.stage()
        settings = read_json(self.root / "program.json")
        self.assertIsNone(settings.get("claude_config_dir"))
        self.assertEqual(program.claude_home({**settings, "root": str(self.root)}), self.seats[0])
        prompt = (self.root / "MANAGER-PROMPT.md").read_text()
        self.assertIn(f"are on the sign-in in `{self.seats[0]}`", prompt)
        self.assertIn(f"Its other seats are `{self.seats[1]}`", prompt)
        self.assertIn("below 99 % of both its five-hour and weekly windows", prompt)
        self.assertIn(f"scripts/hl-seat --root {self.root} status", prompt)

    def test_the_templates_seats_are_written_with_a_tilde_and_are_the_loop_users_own(self):
        # The template names `~/.claude-seat-a` and `~/.claude-seat-b`; program.json keeps them as written, and every
        # script reads them through `program.claude_seats`, which expands `~/` to the home of the user the loop runs as.
        del self.spec["settings"]["claude_seats"]
        self.write_spec()
        self.stage()
        settings = read_json(self.root / "program.json")
        self.assertEqual(settings["claude_seats"], ["~/.claude-seat-a", "~/.claude-seat-b"])
        self.assertEqual(program.claude_seats(settings), self.seats)
        self.assertEqual(program.claude_home({**settings, "root": str(self.root)}), self.seats[0])
        self.assertEqual(program.seats_problems(settings), [])
        prompt = (self.root / "MANAGER-PROMPT.md").read_text()
        self.assertIn(f"are on the sign-in in `{self.seats[0]}`", prompt)
        # A seat the template names that holds no sign-in on this host is refused before anything is staged.
        (self.home / ".claude-seat-b" / ".claude.json").unlink()
        self.assertTrue(any("holds no Claude sign-in" in r and ".claude-seat-b" in r for r in self.refusal()))

    def test_a_program_with_no_seat_is_refused_because_the_default_sign_in_is_the_operators(self):
        self.spec["settings"]["claude_seats"] = []
        self.write_spec()
        reasons = self.refusal()
        self.assertTrue(any("names no Claude seat" in r for r in reasons), reasons)
        self.assertFalse((self.root / "program.json").exists())

    def test_a_seat_with_no_sign_in_is_refused_before_anything_is_staged(self):
        unsigned = str(self.home / ".claude-seat-c")
        Path(unsigned).mkdir()
        self.spec["settings"]["claude_seats"] = [*self.seats, unsigned]
        self.write_spec()
        reasons = self.refusal()
        self.assertTrue(any("`claude_seats`" in r and "holds no Claude sign-in" in r for r in reasons), reasons)
        self.assertFalse((self.root / "program.json").exists())

    def test_more_than_one_seat_needs_workers_on_the_seat_and_not_on_a_token(self):
        self.spec["settings"]["worker_claude_ref"] = "op://Loop/some-token/password"
        self.write_spec()
        reasons = self.refusal()
        self.assertTrue(any("`worker_claude_ref` must be `-`" in r for r in reasons), reasons)
        # One seat may keep its token: the workers never move.
        self.spec["settings"]["claude_seats"] = self.seats[:1]
        self.write_spec()
        self.stage()
        self.assertEqual(read_json(self.root / "program.json")["worker_claude_ref"], "op://Loop/some-token/password")

    def test_a_threshold_outside_reason_is_refused(self):
        for value in (30, 101, "99", True):
            self.spec["settings"]["seat_full_percent"] = value
            self.write_spec()
            reasons = self.refusal()
            self.assertTrue(any("`seat_full_percent` must be a number from 50 to 100" in r for r in reasons), (value, reasons))

    def test_a_directory_with_no_sign_in_is_refused_before_anything_is_staged(self):
        self.with_home(str(self.home / ".claude-team"), signed_in=False)
        (self.home / ".claude-team").mkdir()
        reasons = self.refusal()
        self.assertTrue(any("holds no Claude sign-in" in r for r in reasons), reasons)
        self.assertFalse((self.root / "program.json").exists())

    def test_a_directory_anywhere_else_is_refused(self):
        for value in (str(self.tmp / "elsewhere" / ".claude-team"), str(self.home / ".claude"), str(self.home / "claude-team"), "relative/.claude-team"):
            # Nothing is made on disk for these: the shape is refused before any sign-in is looked for.
            self.with_home(value, signed_in=False)
            reasons = self.refusal()
            self.assertTrue(any("must be a" in r for r in reasons), (value, reasons))


if __name__ == "__main__":
    unittest.main()
