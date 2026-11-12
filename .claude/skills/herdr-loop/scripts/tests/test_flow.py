"""The fixed sequences, each through the real functions of the scripts it
strings together. Git, the files and the counters are real; Herdr, `gh` and
the served build are the stand-ins the other tests use."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

from hl import archive, claim, clocks, flow, items, ledger, merge, spawn
from hl.common import Refusal, read_json, write_json

from .support import SpawnCase, allow_exemptions, install_fake_gh
from .test_merge import TEST_REF, git
from .test_review import write_transcript
from .test_serve import FakeHost, ServingHerdr

class FlowHerdr(ServingHerdr):
    """The build comes up when its serve script is run in a pane, and goes
    down when that pane is closed. A worker's pane does neither.
    `on_serve()` and `on_stop()` are what the build does by itself as it
    comes up and as it goes down."""

    on_serve = on_stop = None

    def call(self, *args, timeout=None):
        serving = args[:2] == ("pane", "run") and "/bin/sh " in args[3]
        result = super(ServingHerdr, self).call(*args, timeout=timeout)
        if serving and self.comes_up:
            self.serve_pane = args[2]
            self.host.up()
            if self.on_serve:
                self.on_serve()
        if args[:2] == ("pane", "close") and args[2] == getattr(self, "serve_pane", None) and self.dies_on_close:
            self.host.down()
            if self.on_stop:
                self.on_stop()
        return result


SERVE = "**Serve.** `PORT=8401 python3 app.py`\n**Ready.** URL `http://127.0.0.1:8401/health`\n"
NOTHING_SERVED = "**Serve.** `true`\n**Ready.** COMMAND `test -e feature.txt`\n"


class FlowCase(SpawnCase):
    BRANCH = "hl/run-1/A-1"

    def setUp(self):
        # The contract is agreed by the canned evaluator of SpawnCase; the clone and the worktree are real here.
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"GH_TOKEN": "loop-token-for-tests", "HOME": str(self.tmp / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = self.tmp / "home"
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        self.clone = self.root / "repos" / "main" / "scratch"
        shutil.rmtree(self.clone)
        git(self.tmp, "clone", str(self.origin), str(self.clone))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.clone, "config", key, value)
        (self.clone / "README.md").write_text("base\n")
        (self.clone / "requirements.txt").write_text("pytest==8\n")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-m", "base")
        git(self.clone, "push", "origin", "HEAD:main")
        git(self.clone, "checkout", "-q", "--detach", "origin/main")
        # What staging adds to every stream's clone.
        (self.clone / ".git" / "info" / "exclude").write_text(".worktrees/\n.playwright-mcp/\n.scratch/\n.deps/\n")
        self.gh_state = self.tmp / "gh.json"
        self.tools = merge.Tools(gh=str(install_fake_gh(self.tmp)), env={**os.environ, "HL_FAKE_GH": str(self.gh_state)}, sleep=lambda s: None)
        self.host = FakeHost()
        self.host.statuses = [200]
        self.herdr = FlowHerdr(self.host)
        self.settings(streams={"main": {"repo": "owner/repo", "clone": str(self.clone), "port_base": 8400}})
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "stream": "main"})
        item = items.load_item(self.run_dir, "A-1")
        item["fields"] = {}
        items.save_item(self.run_dir, item)

    def settings(self, **changes):
        write_json(self.root / "program.json", {**read_json(self.root / "program.json"), **changes})

    def repo(self, **facts):
        self.settings(repos={"owner/repo": facts})

    def policy(self, **policy):
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "policy": {"premise": "always", "review": "blind", **policy}})

    def fopts(self, *argv):
        return flow._parser().parse_args(["--root", str(self.root), "--run-dir", str(self.run_dir), *argv])

    def prepare(self, *extra, item_id="A-1"):
        return flow.prepare(self.root, self.run_dir, item_id, self.tools, self.fopts("prepare", "--id", item_id, *extra))

    def fields(self, item_id="A-1"):
        return items.load_item(self.run_dir, item_id)["fields"]

    def set_gh(self, head, body=None, checks="pass", **pr):
        contract = (self.item_dir() / "contract.md").read_text()
        state = {
            "pr": {
                "number": 7, "url": "https://example.invalid/pull/7", "state": "OPEN", "isDraft": False, "title": "A-1: First item",
                "body": contract + "\n" + SERVE if body is None else body, "headRefName": self.BRANCH, "headRefOid": head,
                "baseRefName": "main", "mergeStateStatus": "CLEAN", "autoMergeRequest": None, "mergeCommit": None, **pr,
            },
            "checks": None if checks is None else [{"name": "ci", "bucket": checks, "state": checks.upper(), "link": "https://github.com/owner/repo/actions/runs/55/job/1"}],
            "merged_message": "https://example.invalid/pull/7#issuecomment-1\n",
        }  # fmt: skip
        self.gh_state.write_text(json.dumps(state))

    def gh_calls(self):
        return read_json(self.gh_state).get("calls", [])

    def build(self, report=SERVE, unpushed=False, files=(("feature.txt", "feature\n"),)):
        """A builder's work: prepared, started, committed, pushed, reported, done.
        `unpushed` leaves one more commit in the worktree than the pull request has."""
        self.prepare()
        self.start()
        worktree = Path(self.fields()["worktree"])
        for name, text in files:
            (worktree / name).parent.mkdir(parents=True, exist_ok=True)
            (worktree / name).write_text(text)
            git(worktree, "add", name)
        git(worktree, "commit", "-m", "A-1: add the feature")
        git(worktree, "push", "origin", self.BRANCH)
        head = git(worktree, "rev-parse", "HEAD")
        if unpushed:
            (worktree / "late.txt").write_text("committed after the push\n")
            git(worktree, "add", "late.txt")
            git(worktree, "commit", "-m", "A-1: one more thing")
        self.write("A-1", "build-1.md", report)
        self.write("A-1", "build-1.done", "")
        self.set_gh(head)
        return head

    def built(self):
        return flow.built(self.root, self.run_dir, "A-1", self.tools, self.herdr, self.fopts("built", "--id", "A-1"))

    def review(self, *extra, item_id="A-1"):
        return flow.start_review(self.root, self.run_dir, item_id, self.tools, self.herdr, self.host, self.fopts("review", "--id", item_id, *extra), envs=self.envs)

    def reviewed(self, *extra):
        return flow.reviewed(self.root, self.run_dir, "A-1", self.tools, self.herdr, self.host, self.fopts("reviewed", "--id", "A-1", *extra))

    def verdict(self, word="PASS", results=("pass", "pass"), n=1, **extra):
        data = {
            "verdict": word,
            "criteria": [{"n": i + 1, "result": r, "evidence": [], "note": "ran it"} for i, r in enumerate(results)],
            "findings": [],
            "how_run": "curl the served build",
            **extra,
        }
        self.write("A-1", f"verdict-{n}.json", json.dumps(data))
        self.write("A-1", f"grade-{n}.done", "")

    def run_named(self, said: str) -> list[str]:
        """Run the commands a script's words name, in the order they are
        written there: a way out that is named is a way out that works. A
        `git` command is run through a shell exactly as it is written. The
        kit's own commands are written in the short form of every refusal
        of the kit (`hl-items void --id …`, `hl-flow review --id …`); as the
        manager's prompt has it for every line, `--root` and `--run-dir`
        are added here, and nothing else is."""
        ran = []
        for command in re.findall(r"`([^`]+)`", said):
            words = shlex.split(command)
            if words[0].startswith("hl-") and "--id" not in words:
                # A script's name in a sentence (`hl-flow reviewed` read it) is not a command to run.
                continue
            if words[0] == "hl-items":
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as refused:
                    code = items.main(["--run-dir", str(self.run_dir), *words[1:]])
                self.assertEqual(code, 0, (command, refused.getvalue()))
            elif words[0] == "git":
                # Through a shell, as somebody would type it: one of these is two commands and a pipe.
                subprocess.run(command, shell=True, check=True, capture_output=True)
            elif words[:3] == ["hl-flow", "review", "--id"]:
                self.assertEqual(self.review(*words[4:], item_id=words[3])[0], 0, command)
            elif words == ["hl-flow", "reviewed", "--id", "A-1"]:
                self.assertEqual(self.reviewed("--no-comment")[0], 0, command)
            elif words == ["hl-flow", "prepare", "--id", "A-1"]:
                self.assertEqual(self.prepare()[0], 0, command)
            elif words == ["hl-flow", "built", "--id", "A-1"]:
                self.assertEqual(self.built()[0], 0, command)
            elif words == ["hl-serve", "stop", "--id", "A-1"]:
                flow.serve.stop(self.run_dir, "A-1", self.herdr, self.host)
            elif words == ["hl-serve", "start", "--id", "A-1"]:
                self.assertEqual(flow.serve.start(self.root, self.run_dir, "A-1", self.herdr, self.host)[0], 0, command)
            else:
                self.fail(f"not a command this test can run: {command}")
            ran.append(command)
        return ran


class PrepareTest(FlowCase):
    def test_an_item_gets_its_worktree_branch_port_and_contract_in_one_step(self):
        code, out = self.prepare(item_id="A-2")
        self.assertEqual((code, out["result"], out["worktree_made"], out["setup"]), (0, "prepared", True, "none"))
        fields = self.fields("A-2")
        worktree = self.clone / ".worktrees" / "run-1-A-2"
        self.assertEqual((fields["branch"], fields["worktree"], fields["port"]), ("hl/run-1/A-2", str(worktree), "8402"))
        self.assertEqual(git(worktree, "rev-parse", "--abbrev-ref", "HEAD"), "hl/run-1/A-2")
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), git(self.clone, "rev-parse", "origin/main"))
        self.assertEqual(fields["base_sha"], git(self.clone, "rev-parse", "origin/main"))
        # The Accept list is installed as the contract, and what the manager needs to route it is in the answer.
        self.assertEqual(items.load_item(self.run_dir, "A-2")["contract"]["source"], "accept")
        self.assertEqual((out["criteria"], out["has_verify"], out["floor"], out["port"]), (1, False, [], "8402"))
        # The stream's own checkout is where it was.
        self.assertEqual(git(self.clone, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")

    def test_the_worktree_is_cut_from_the_base_as_the_remote_has_it_now(self):
        other = self.tmp / "other"
        git(self.tmp, "clone", str(self.origin), str(other))
        (other / "later.txt").write_text("merged meanwhile\n")
        git(other, "add", "later.txt")
        git(other, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "another item")
        git(other, "push", "origin", "HEAD:main")
        self.prepare()
        self.assertTrue((Path(self.fields()["worktree"]) / "later.txt").exists())

    def test_the_repositorys_setup_is_run_in_the_worktree_and_its_inputs_fingerprinted(self):
        self.repo(setup="echo installing && mkdir -p .deps && touch .deps/installed", setup_inputs=["requirements.txt", "package-lock.json"])
        code, out = self.prepare()
        worktree = Path(self.fields()["worktree"])
        self.assertEqual((code, out["setup"]), (0, "ready"))
        self.assertTrue((worktree / ".deps" / "installed").exists())
        self.assertEqual((self.fields()["setup"], self.fields()["setup_key"]), ("ready", flow.setup_key(worktree, ["requirements.txt", "package-lock.json"])))
        self.assertIn("installing", (self.item_dir() / "setup-1.log").read_text())

    def test_a_stream_that_needs_none_of_the_repositorys_setup_gets_none(self):
        self.repo(setup="mkdir -p .deps && touch .deps/installed")
        self.settings(streams={"main": {"repo": "owner/repo", "clone": str(self.clone), "port_base": 8400, "setup": False}})
        code, out = self.prepare()
        self.assertEqual((code, out["setup"]), (0, "none"))
        self.assertFalse((Path(self.fields()["worktree"]) / ".deps").exists())
        self.assertNotIn("setup", self.fields())

    def test_a_setup_that_fails_is_said_with_its_last_lines_and_the_worktree_is_not_called_ready(self):
        self.repo(setup="echo 'ERROR: no matching distribution' && exit 7")
        code, out = self.prepare()
        self.assertEqual((code, out["result"], out["exit"], out["last_lines"]), (3, "setup-failed", 7, ["ERROR: no matching distribution"]))
        self.assertEqual(self.fields()["setup"], "failed")

    def test_a_setup_that_leaves_files_git_does_not_ignore_is_not_a_ready_worktree(self):
        self.repo(setup="touch installed-marker")
        code, out = self.prepare()
        self.assertEqual((code, out["result"], out["left_behind"]), (3, "setup-failed", ["installed-marker"]))
        self.assertIn("a setup may only write paths git ignores", out["detail"])
        self.assertEqual(self.fields()["setup"], "failed")

    def test_preparing_again_changes_nothing_and_does_not_install_twice(self):
        self.repo(setup="mkdir -p .deps && echo run >> .deps/runs")
        self.prepare()
        code, out = self.prepare()
        self.assertEqual((code, out["worktree_made"], out["setup"]), (0, False, "ready"))
        self.assertEqual((Path(self.fields()["worktree"]) / ".deps" / "runs").read_text(), "run\n")

    def test_an_item_taken_up_again_gets_its_branch_back_with_its_commits(self):
        self.prepare()
        worktree = Path(self.fields()["worktree"])
        (worktree / "half.txt").write_text("half done\n")
        git(worktree, "add", "half.txt")
        git(worktree, "commit", "-m", "A-1: half")
        git(self.clone, "worktree", "remove", "--force", str(worktree))
        code, out = self.prepare()
        self.assertEqual((code, out["worktree_made"]), (0, True))
        self.assertTrue((worktree / "half.txt").exists())

    def test_a_contract_with_fewer_criteria_than_the_accept_list_has_numbered_lines_is_refused(self):
        # Whatever the parser misses, the numbered lines of the Accept section are counted on their own.
        self.write("A-2", "item.md", "## A-2 — Second item\n\n**Scope.** s\n\n**Accept.**\n1. `make test` exits 0.\n\nThen, from the repository root:\n2. `git diff --name-only` prints nothing.\n\n**Depends.** A-1.\n")
        with self.assertRaises(Refusal) as caught:
            self.prepare(item_id="A-2")
        self.assertIn("the contract installed from the Accept list of A-2 has 1 criteria, and the Accept section has 2 numbered lines", str(caught.exception))
        with self.assertRaises(Refusal):
            self.prepare(item_id="A-2")

    def test_numbered_lines_in_a_fenced_example_or_under_a_later_heading_are_not_counted(self):
        # The count ends at a heading and skips a fence, so neither a fenced example nor a `### Notes` list refuses a whole contract.
        self.write(
            "A-2",
            "item.md",
            "## A-2 — Second item\n\n**Scope.** s\n\n**Accept.**\n1. `make test` exits 0.\n2. `./check` prints `OK`, as in this run:\n\n"
            "```\n$ ./check\n# step one\n1. step one\nOK\n```\n\n### Notes\n\n1. A note, not a criterion.\n2. Another.\n",
        )
        self.assertEqual(self.prepare(item_id="A-2")[0], 0)
        self.assertEqual(items.load_item(self.run_dir, "A-2")["contract"]["criteria"], 2)

    def test_the_port_can_be_given_or_be_none(self):
        self.assertEqual(self.prepare("--port", "9100")[1]["port"], "9100")
        self.assertEqual(self.prepare("--port", "none", item_id="A-2")[1]["port"], None)
        self.assertNotIn("port", self.fields("A-2"))
        with self.assertRaises(Refusal):
            self.prepare("--port", "eighty", item_id="B-1")

    def test_a_stream_with_no_port_base_serves_nothing_unless_told(self):
        self.settings(streams={"main": {"repo": "owner/repo", "clone": str(self.clone)}})
        self.assertIsNone(self.prepare()[1]["port"])

    def test_a_look_on_a_stream_with_no_port_base_is_refused_when_routed_not_at_built(self):
        # An item of a help-desk feature program: a look item of the `brand` stream was prepared with no port, its
        # builder wrote `Serve: true`, and `built` sent the build back. The route is where that is known.
        self.settings(streams={"main": {"repo": "owner/repo", "clone": str(self.clone)}})
        self.assertIsNone(self.prepare()[1]["port"])
        with self.assertRaises(Refusal) as caught:
            items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        self.assertIn("A-1 was prepared with no port", str(caught.exception))
        self.assertIn("`hl-flow prepare --id A-1 --port <n>`", str(caught.exception))
        self.assertIsNone(items.load_item(self.run_dir, "A-1").get("route"))
        # Without a look nothing is served, so no port is needed; with one given, the look is routed.
        self.assertFalse(items.route_item(self.run_dir, "A-1", "independent", False, "backend")["route"]["look"])
        self.assertEqual(self.prepare("--port", "8508")[1]["port"], "8508")
        self.assertTrue(items.route_item(self.run_dir, "A-1", "independent", True, "a page")["route"]["look"])

    def test_an_item_routed_with_a_look_before_it_is_prepared_is_not_prepared_without_a_port(self):
        self.settings(streams={"main": {"repo": "owner/repo", "clone": str(self.clone)}})
        items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        for extra in ((), ("--port", "none")):
            with self.assertRaises(Refusal) as caught:
                self.prepare(*extra)
            self.assertIn("A-1 would get no port", str(caught.exception))
            self.assertIn("routed with a look", str(caught.exception))
        # Refused before anything was made: no worktree, no fields.
        self.assertEqual(self.fields(), {})
        self.assertFalse((self.clone / ".worktrees" / "run-1-A-1").exists())
        code, out = self.prepare("--port", "8508")
        self.assertEqual((code, out["port"], out["worktree_made"]), (0, "8508", True))
        self.assertTrue(self.fields()["base_sha"])

    def test_a_look_floor_needs_a_port_at_prepare_too(self):
        self.settings(streams={"main": {"repo": "owner/repo", "clone": str(self.clone)}})
        item = items.load_item(self.run_dir, "A-1")
        item["floor"] = ["look"]
        items.save_item(self.run_dir, item)
        with self.assertRaises(Refusal) as caught:
            self.prepare()
        self.assertIn("its floor asks for a look", str(caught.exception))
        # A stream with a port base gives the item its port, and nothing is refused.
        self.settings(streams={"main": {"repo": "owner/repo", "clone": str(self.clone), "port_base": 8400}})
        self.assertEqual(self.prepare()[1]["port"], "8401")

    def test_a_criterion_whose_tool_the_setup_did_not_install_is_reported_before_the_build(self):
        # An item of a help-desk feature program: criterion 6 ran `.venv/bin/ruff` from `services/platform`, whose
        # setup installs no ruff. It was built and reviewed before anyone saw the criterion could not run.
        self.repo(setup="mkdir -p .deps/bin && : > .deps/bin/lint")
        contract = self.item_dir() / "contract.md"
        contract.write_text(
            "## Criteria\n\n"
            "1. `.deps/bin/lint src` exits 0, and `.deps/bin/fmt --check` prints nothing.\n"
            "2. `make test` exits 0 (a program on the PATH is the worker's to have, and is not looked for).\n"
        )
        code, out = self.prepare()
        self.assertEqual((code, out["setup"]), (0, "ready"))
        self.assertEqual(out["missing_commands"], [{"criterion": 1, "command": ".deps/bin/fmt", "looked_in": ["."]}])
        # Reported, not refused: the worktree is prepared and the item goes on as the manager decides.
        self.assertTrue(Path(self.fields()["worktree"], ".deps", "bin", "lint").exists())

    def test_the_tools_a_criterion_names_are_looked_for_where_it_says_it_runs(self):
        tree = self.tmp / "tree"
        (tree / "services" / "platform" / ".venv" / "bin").mkdir(parents=True)
        (tree / "services" / "platform" / ".venv" / "bin" / "python").write_text("")
        (tree / "web" / "node_modules" / ".bin").mkdir(parents=True)
        (tree / "web" / "node_modules" / ".bin" / "tsc").write_text("")
        text = (
            "## Criteria\n\n"
            "1. From `services/platform`: `.venv/bin/python -m pytest -q tests/test_x.py` ends `2 passed`.\n"
            "2. From `services/platform`: `.venv/bin/ruff check src/x.py` exits 0.\n"
            "3. `cd web && ./node_modules/.bin/tsc --noEmit` exits 0, and `cd web && node_modules/.bin/eslint .` exits 0.\n"
            "4. From `services/platform`, in one shell: `D=$(mktemp -d); cp a $D/a; .venv/bin/python -m pytest | tail -1; cmp -s a $D/a && echo restored`.\n"
            "5. `services/platform/.venv/bin/python -c 1` and `/usr/bin/true` and `$VENV/bin/x` and `scripts/new-check.sh` exit 0.\n"
        )
        self.assertEqual(
            flow.missing_commands(tree, text),
            [
                {"criterion": 2, "command": ".venv/bin/ruff", "looked_in": [".", "services/platform"]},
                {"criterion": 3, "command": "node_modules/.bin/eslint", "looked_in": [".", "web"]},
            ],
        )
        self.assertEqual(flow.missing_commands(tree, "no criteria here"), [])

    def test_an_ended_item_and_a_run_with_no_clone_are_refused(self):
        items.set_status(self.run_dir, "A-2", "skipped", "not needed")
        with self.assertRaises(Refusal) as caught:
            self.prepare(item_id="A-2")
        self.assertIn("A-2 is skipped", str(caught.exception))
        self.settings(streams={})
        with self.assertRaises(Refusal) as caught:
            self.prepare()
        self.assertIn("names no clone for run run-1", str(caught.exception))


class BuiltTest(FlowCase):
    def test_a_finished_build_is_recorded_and_its_builder_closed(self):
        head = self.build()
        code, out = self.built()
        self.assertEqual((code, out["result"], out["head_sha"], out["disputed"]), (0, "built", head, []))
        self.assertEqual(spawn._open_workers(self.run_dir, "A-1"), [])
        self.assertEqual((self.fields()["pr"], self.fields()["head_sha"]), ("7", head))

    def test_a_build_with_no_worker_on_record_is_refused_before_anything_is_written_on_the_item(self):
        # Found by the session that built the trial harness: the pull request and the head were on the item by the time this was refused.
        self.build()
        record = self.item_dir() / "build-1.worker.json"
        record.unlink()
        with self.assertRaises(Refusal) as caught:
            self.built()
        self.assertEqual(str(caught.exception), f"{record} does not exist: no worker was started for build 1 (hl-spawn writes it)")
        self.assertEqual((self.fields().get("pr"), self.fields().get("head_sha"), (self.item_dir() / "build-1.done").exists()), (None, None, True))

    def test_a_dispute_in_the_report_is_handed_to_the_manager_by_number(self):
        self.build(report=SERVE + "\n## Disputed criterion\n\nCriterion 2: the item pins `1`; the criterion was amended to `2`.\n")
        self.assertEqual(self.built()[1]["disputed"], [2])

    def test_a_build_that_is_not_finished_goes_back_to_its_builder_who_is_still_open(self):
        self.build(unpushed=True)
        code, out = self.built()
        self.assertEqual((code, out["result"], out["times_sent_back"], out["prompted"], out["watch"]), (2, "sent-back", 1, True, "A-1:build-1"))
        self.assertTrue(any("push what you committed" in r for r in out["refused"]))
        prompt = self.herdr.called("agent", "prompt")[-1][3]
        self.assertEqual(prompt, f"Read {out['fix_file']} and do exactly what it says.")
        self.assertEqual(spawn._open_workers(self.run_dir, "A-1"), ["build-1.worker.json"])
        self.assertFalse((self.item_dir() / "build-1.done").exists())

    def test_a_send_back_whose_one_line_did_not_reach_the_builder_says_so_and_is_not_counted_twice(self):
        # Found in review: the send-back was counted, the prompt failed, and nothing said the builder never heard.
        from hl.herdr import HerdrError

        self.build(unpushed=True)
        with mock.patch("hl.flow.spawn.send", side_effect=HerdrError("exit_1", "server not running")):
            code, out = self.built()
        self.assertEqual((code, out["result"], out["prompted"], out["times_sent_back"]), (2, "sent-back", False, 1))
        self.assertIn("hl-spawn prompt --id A-1 --phase build --n 1 --file", out["next"])
        self.assertEqual(spawn._open_workers(self.run_dir, "A-1"), ["build-1.worker.json"])
        # Run again before the builder has answered, it counts nothing more: there is no done file to check.
        with self.assertRaises(Refusal) as caught:
            self.built()
        self.assertIn("build-1.done does not exist", str(caught.exception))
        self.assertEqual(len(list(self.item_dir().glob("build-1.fix-*.md"))), 1)

    def test_a_build_sent_back_twice_has_its_builder_closed_and_is_the_managers_to_decide(self):
        self.build(unpushed=True)
        self.built()
        self.write("A-1", "build-1.done", "")
        prompts = len(self.herdr.called("agent", "prompt"))
        code, out = self.built()
        self.assertEqual((code, out["result"], out["times_sent_back"]), (2, "sent-back-twice", 2))
        self.assertEqual(len(self.herdr.called("agent", "prompt")), prompts)
        self.assertEqual(spawn._open_workers(self.run_dir, "A-1"), [])

    def test_a_path_the_spec_protects_sends_the_build_back(self):
        self.repo(protected=["feature.txt"])
        self.build()
        code, out = self.built()
        self.assertEqual(code, 2)
        self.assertTrue(any("changes paths no item may change: feature.txt" in r for r in out["refused"]))


class ReviewTest(FlowCase):
    def setUp(self):
        super().setUp()
        self.head = self.build()
        self.built()

    def test_the_build_is_served_and_its_reviewer_started_while_checks_still_run(self):
        self.settings(review_with_ci=True)
        self.set_gh(self.head, checks="pending")
        code, out = self.review()
        self.assertEqual((code, out["result"], out["checks"], out["watch"], out["prompt_sent"]), (0, "reviewing", "running", "A-1:grade-1", True))
        self.assertIn("answered", out["served"])
        self.assertEqual(spawn._open_workers(self.run_dir, "A-1"), ["grade-1.worker.json"])
        self.assertEqual(self.host.listeners(8401), [4242])

    def test_a_program_that_reviews_after_its_checks_waits_for_them(self):
        self.set_gh(self.head, checks="pending")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertIn("the required checks of A-1 are running; this program reviews after its checks have passed", str(caught.exception))
        self.assertEqual((self.host.listeners(8401), spawn._open_workers(self.run_dir, "A-1")), ([], []))
        self.set_gh(self.head, checks="pass")
        self.assertEqual(self.review()[1]["checks"], "green")

    def test_a_head_the_checks_already_refused_is_not_reviewed(self):
        for with_ci in (True, False):
            self.settings(review_with_ci=with_ci)
            self.set_gh(self.head, checks="fail")
            code, out = self.review()
            self.assertEqual((code, out["result"]), (4, "checks-red"))
            self.assertIn("gh run view 55 --repo owner/repo --log-failed", Path(out["checks_file"]).read_text())
            self.assertEqual((self.host.listeners(8401), spawn._open_workers(self.run_dir, "A-1"), self.attempts()["grade"]), ([], [], 0))

    def test_a_pull_request_that_conflicts_with_its_base_is_not_served_or_reviewed(self):
        for with_ci in (True, False):
            self.settings(review_with_ci=with_ci)
            self.set_gh(self.head, checks=None, mergeable="CONFLICTING", mergeStateStatus="DIRTY")
            code, out = self.review()
            self.assertEqual((code, out["result"], out["checks"]), (4, "conflicting", "conflicting"))
            self.assertIn("conflicts with `main`: a rework. Write in `rework-2.md`", out["next"])
            self.assertEqual((self.host.listeners(8401), spawn._open_workers(self.run_dir, "A-1"), self.attempts()["grade"]), ([], [], 0))
            self.assertFalse((self.item_dir() / "serve-1.json").exists())

    def test_the_reviewers_effort_is_the_managers_to_name(self):
        self.review("--effort", "medium")
        argv = self.agent_argv()
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")

    def test_a_build_that_does_not_come_up_starts_no_reviewer(self):
        self.herdr.comes_up = False
        self.host.statuses = [None]
        code, out = self.review()
        self.assertEqual((code, out["result"]), (3, "not-ready"))
        self.assertEqual((spawn._open_workers(self.run_dir, "A-1"), self.attempts()["grade"]), ([], 0))

    def test_a_reviewer_that_does_not_start_leaves_no_build_being_served(self):
        self.envs.absent.add("CLAUDE_CODE_OAUTH_TOKEN")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertIn("is running without CLAUDE_CODE_OAUTH_TOKEN", str(caught.exception))
        self.assertEqual(self.attempts()["grade"], 0)
        self.assertEqual(self.host.listeners(8401), [])
        self.assertTrue(read_json(self.item_dir() / "serve-1.json")["stopped_at"])

    def test_a_review_brief_carries_the_managers_note_in_a_slot_of_its_own(self):
        # An item's third review, in a help-desk feature program: a note written below a review brief rendered before
        # the build was served never matched what `review` renders once it is (the brief says what the served build
        # answered), so it was refused.
        self.policy(review="code")
        items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        note = self.tmp / "note.md"
        note.write_text("Compare the header at 1280 wide only; the item names no other width.\n")
        code, out = self.review("--note-file", str(note))
        self.assertEqual((code, out["result"], out["brief_notes"], out["manager_note"]), (0, "reviewing", 0, str(note)))
        brief = (self.item_dir() / "grade-1.brief.md").read_text()
        self.assertIn("## A note from the manager\n\nCompare the header at 1280 wide only; the item names no other width.\n", brief)
        self.assertLess(brief.index("## A note from the manager"), brief.index("## How to review"))
        self.assertEqual(self.attempts()["log"][-1]["brief_notes"]["manager_note"]["file"], str(note))
        # The look, the screenshots and the evidence, as the reviewer is told them.
        evidence = self.item_dir() / "evidence-1"
        self.assertIn(f"Give the browser tool's screenshot a full path in that directory as its `filename` (`{evidence}/alerts-dark.png`): given a bare name, the browser tool saves it under `{evidence}/.playwright-mcp/` instead.", brief)
        self.assertIn("Open every page it touches with the browser tool on the running build", brief)
        self.assertIn("so a page a Playwright script or any other program opened from the shell is not a look", brief)
        self.assertIn("That counts as exercising the change, never as a look at a page (below).", brief)
        self.assertIn("names one file (to cite a folder of screenshots, list the files in it)", brief)

    def test_a_reviewer_is_told_which_protected_path_the_manager_exempted_for_this_item(self):
        self.policy(review="code")
        self.repo(protected=["feature.txt"])
        allow_exemptions(self.run_dir, "feature.txt")
        items.exempt_path(self.run_dir, "A-1", "feature.txt", "the item exists to add it", ["feature.txt"])
        self.assertEqual(self.review()[0], 0)
        brief = (self.item_dir() / "grade-1.brief.md").read_text()
        self.assertIn("Changed paths this repository's spec says no item may change: `feature.txt`.", brief)
        self.assertIn("Of those, the manager has recorded that this item alone may change `feature.txt` (the item exists to add it).", brief)
        self.assertEqual(read_json(self.item_dir() / "grade-1.facts.json")["exempted"], {"feature.txt": "the item exists to add it"})

    def test_a_review_without_a_note_has_an_empty_slot(self):
        self.policy(review="code")
        code, out = self.review()
        self.assertEqual((code, out["brief_notes"]), (0, 0))
        self.assertNotIn("manager_note", out)
        brief = (self.item_dir() / "grade-1.brief.md").read_text()
        self.assertNotIn("A note from the manager", brief)
        self.assertNotIn("{{", brief)

    def test_a_note_for_a_review_brief_with_no_slot_is_refused_before_anything_is_served(self):
        # Found in review: a program on the first set of briefs grades from its `grade.md`, which has no slot, and the
        # refusal came only from the brief's rendering, after the build was up.
        self.settings(briefs="v1")
        note = self.tmp / "note.md"
        note.write_text("Look at the header only.\n")
        with self.assertRaises(Refusal) as caught:
            self.review("--note-file", str(note))
        self.assertIn("grade.md of this program's briefs has no slot for a manager's note", str(caught.exception))
        self.assertFalse((self.item_dir() / "serve-1.json").exists())
        self.assertEqual((self.host.listeners(8401), self.attempts()["grade"]), ([], 0))

    def test_a_placeholder_in_a_note_is_its_text_not_a_hole_in_the_brief(self):
        self.policy(review="code")
        note = self.tmp / "note.md"
        note.write_text("The page prints `{{count}}` until the data loads; that is the item's own text.\n")
        code, out = self.review("--note-file", str(note))
        self.assertEqual((code, out["result"]), (0, "reviewing"))
        self.assertIn("The page prints `{ {count}}` until the data loads", (self.item_dir() / "grade-1.brief.md").read_text())

    def test_a_note_that_cannot_be_read_refuses_the_review_before_anything_is_served(self):
        for path, said in ((self.tmp / "absent.md", "cannot be read"), (self.write("A-1", "empty.md", "  \n"), "is empty")):
            with self.assertRaises(Refusal) as caught:
                self.review("--note-file", str(path))
            self.assertIn(said, str(caught.exception))
        self.assertEqual((self.host.listeners(8401), self.attempts()["grade"]), ([], 0))
        self.assertFalse((self.item_dir() / "serve-1.json").exists())

    def test_before_a_build_is_accepted_there_is_nothing_to_review(self):
        item = items.load_item(self.run_dir, "A-1")
        del item["fields"]["head_sha"]
        items.save_item(self.run_dir, item)
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertIn("has no accepted build to review", str(caught.exception))


class ReferenceReviewTest(FlowCase):
    """A look review in a repository whose spec names its reference directory: the reference is served beside the
    build, the reviewer is told where, and both are stopped when the review is read."""

    def setUp(self):
        super().setUp()
        from .support import FakeHerdr
        from .test_serve import REF_PID, REF_PORT

        self.ref_pid, self.ref_port = REF_PID, REF_PORT
        herdr = self.herdr
        real_call = herdr.call

        def call(*args, timeout=None):
            if args[:2] == ("pane", "run") and "reference-" in args[3]:
                result = FakeHerdr.call(herdr, *args, timeout=timeout)
                herdr.ref_pane = args[2]
                self.host.up(pid=REF_PID, port=REF_PORT)
                return result
            if args[:2] == ("pane", "close") and args[2] == getattr(herdr, "ref_pane", None):
                result = FakeHerdr.call(herdr, *args, timeout=timeout)
                self.host.down(REF_PID)
                return result
            return real_call(*args, timeout=timeout)

        herdr.call = call
        self.host.free_port = lambda: REF_PORT
        self.repo(reference="brand")
        self.policy(review="code")
        self.head = self.build(files=(("feature.txt", "feature\n"), ("brand/family/reference.html", "<p>reference</p>\n")))
        self.built()

    def test_the_reviewer_is_told_where_the_reference_is_served_and_both_stop_with_the_review(self):
        items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        code, out = self.review()
        self.assertEqual((code, out["result"]), (0, "reviewing"))
        brief = (self.item_dir() / "grade-1.brief.md").read_text()
        url = f"http://127.0.0.1:{self.ref_port}/"
        self.assertIn(f"**The reference pages are served beside the build**: `brand/` of this checkout, at {url} (a file `brand/<path>` is at {url}<path>).", brief)
        self.assertIn("The browser tool does not open `file://`", brief)
        self.assertNotIn("{{", brief)
        self.assertEqual((self.host.listeners(8401), self.host.listeners(self.ref_port)), ([4242], [self.ref_pid]))
        self.verdict()
        # The stand-in reviewer left no transcript, so the review itself does not count; what is checked here is the stop.
        self.reviewed("--no-comment")
        self.assertEqual((self.host.listeners(8401), self.host.listeners(self.ref_port)), ([], []))
        served = read_json(self.item_dir() / "serve-1.json")
        self.assertTrue(served["stopped_at"] and served["reference"]["stopped_at"])

    def test_an_item_routed_without_a_look_is_reviewed_as_before(self):
        items.route_item(self.run_dir, "A-1", "independent", False, "backend")
        self.assertEqual(self.review()[0], 0)
        brief = (self.item_dir() / "grade-1.brief.md").read_text()
        self.assertNotIn("reference pages are served", brief)
        self.assertNotIn("reference", read_json(self.item_dir() / "serve-1.json"))


class ReadyAgainTest(FlowCase):
    def test_a_build_that_changed_a_lockfile_has_its_dependencies_made_again_before_the_review(self):
        self.repo(setup="mkdir -p .deps && echo run >> .deps/runs", setup_inputs=["requirements.txt"])
        self.build(files=(("feature.txt", "feature\n"), ("requirements.txt", "pytest==8\nrequests==2\n")))
        self.built()
        worktree = Path(self.fields()["worktree"])
        before = self.fields()["setup_key"]
        code, out = self.review()
        self.assertEqual(code, 0)
        self.assertIn("setup_again_seconds", out)
        self.assertEqual((worktree / ".deps" / "runs").read_text(), "run\nrun\n")
        self.assertNotEqual(self.fields()["setup_key"], before)

    def test_a_build_that_changed_no_manifest_is_reviewed_as_it_is(self):
        self.repo(setup="mkdir -p .deps && echo run >> .deps/runs", setup_inputs=["requirements.txt"])
        self.build()
        self.built()
        code, out = self.review()
        self.assertNotIn("setup_again_seconds", out)
        self.assertEqual((Path(self.fields()["worktree"]) / ".deps" / "runs").read_text(), "run\n")

    def test_a_setup_that_fails_before_the_review_stops_it(self):
        self.repo(setup="test ! -e feature.txt", setup_inputs=["requirements.txt"])
        self.build(files=(("feature.txt", "feature\n"), ("requirements.txt", "pytest==9\n")))
        self.built()
        code, out = self.review()
        self.assertEqual((code, out["result"]), (3, "setup-failed"))
        self.assertEqual(spawn._open_workers(self.run_dir, "A-1"), [])


class RouteAgainstTheDiffTest(FlowCase):
    def test_an_item_routed_without_a_look_whose_diff_touches_what_a_person_sees_is_not_reviewed(self):
        self.policy(premise="routed", review="code")
        self.repo(seen=["web/**", "*.css"])
        self.prepare()
        items.route_item(self.run_dir, "A-1", "independent", False, "labelled backend")
        self.start("--why", "small")
        worktree = Path(self.fields()["worktree"])
        (worktree / "web").mkdir()
        (worktree / "web" / "page.html").write_text("<p>new</p>\n")
        git(worktree, "add", "web/page.html")
        git(worktree, "commit", "-m", "A-1: change")
        git(worktree, "push", "origin", self.BRANCH)
        self.write("A-1", "build-1.md", SERVE)
        self.write("A-1", "build-1.done", "")
        self.set_gh(git(worktree, "rev-parse", "HEAD"))
        self.built()
        with self.assertRaises(Refusal) as caught:
            self.review()
        said = str(caught.exception)
        self.assertIn("is routed without a look, and its pull request changes paths this repository's spec says a person sees (web/page.html)", said)
        self.assertIn("hl-items route --id A-1 --premise independent --look yes", said)
        self.assertEqual((self.host.listeners(8401), self.attempts()["grade"]), ([], 0))
        # Routed with a look, the same build is reviewed.
        items.route_item(self.run_dir, "A-1", "independent", True, "the diff touches a page")
        self.assertEqual(self.review()[0], 0)


class ReviewedTest(FlowCase):
    def setUp(self):
        super().setUp()
        self.head = self.build()
        self.built()

    def test_a_reviewer_that_has_not_finished_is_not_read(self):
        self.review()
        with self.assertRaises(Refusal) as caught:
            self.reviewed()
        self.assertIn("grade-1.done does not exist", str(caught.exception))
        self.assertEqual(self.host.listeners(8401), [4242])

    def test_a_grade_is_read_the_build_stopped_the_reviewer_closed_and_the_verdict_posted(self):
        self.review()
        self.verdict()
        code, out = self.reviewed()
        self.assertEqual((code, out["effective"], out["problems"], out["checks"]), (0, "PASS", [], "green"))
        self.assertEqual((self.host.listeners(8401), spawn._open_workers(self.run_dir, "A-1")), ([], []))
        self.assertEqual(out["comment"], "https://example.invalid/pull/7#issuecomment-1")
        posts = [c for c in self.gh_calls() if c[:1] == ["api"] and "POST" in c]
        self.assertEqual(len(posts), 1)
        self.assertIn("repos/owner/repo/issues/7/comments", posts[0])
        body = (self.item_dir() / "verdict-1.comment.md").read_text()
        self.assertIn("**A-1, review 1: PASS**", body)
        self.assertIn("| 1 | pass | ran it |", body)
        self.assertEqual(read_json(self.item_dir() / "review-1.json")["effective"], "PASS")

    def test_a_pass_on_a_pull_request_that_has_come_to_conflict_says_so(self):
        # An item of a help-desk feature program: a review passed a pull request GitHub reported CONFLICTING, and
        # `reviewed` answered `checks: none`.
        self.review()
        self.verdict()
        self.set_gh(self.head, checks=None, mergeable="CONFLICTING", mergeStateStatus="DIRTY")
        code, out = self.reviewed()
        self.assertEqual((code, out["effective"], out["checks"]), (0, "PASS", "conflicting"))
        self.assertIn("conflicts with `main`: a rework. Write in `rework-2.md`", out["next"])

    def test_reading_it_again_posts_nothing_twice(self):
        self.review()
        self.verdict()
        self.reviewed()
        code, out = self.reviewed()
        self.assertEqual((code, out["effective"]), (0, "PASS"))
        self.assertEqual(len([c for c in self.gh_calls() if c[:1] == ["api"] and "POST" in c]), 1)

    def test_a_verdict_that_is_not_usable_is_said_and_not_posted(self):
        self.review()
        self.write("A-1", "verdict-1.json", json.dumps({"verdict": "pass"}))
        self.write("A-1", "grade-1.done", "")
        code, out = self.reviewed()
        self.assertEqual((code, out["effective"]), (2, None))
        self.assertTrue(any("verdict must be one of" in p for p in out["problems"]))
        self.assertEqual([c for c in self.gh_calls() if c[:1] == ["api"] and "POST" in c], [])
        # The build is stopped and the reviewer closed all the same.
        self.assertEqual((self.host.listeners(8401), spawn._open_workers(self.run_dir, "A-1")), ([], []))

    def test_a_checkout_the_reviewer_changed_is_a_problem_with_the_review(self):
        self.review()
        (Path(self.fields()["worktree"]) / "feature.txt").write_text("edited during the review\n")
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual(code, 2)
        self.assertIn("tracked files were changed in the checkout during the review: feature.txt", out["problems"])
        self.assertNotIn("comment", out)

    def gate(self):
        return merge.gate(self.run_dir, "A-1", self.clone, self.tools)

    def void(self, note="the reviewer edited a file and put it back", n=None):
        """`hl-items void` for the item's latest review, from the command line. What it refused with is kept in `refused`."""
        argv = ["--run-dir", str(self.run_dir), "void", "--id", "A-1", "--phase", "grade", "--note", note, *(["--n", str(n)] if n else [])]
        with contextlib.redirect_stdout(io.StringIO()) as said, contextlib.redirect_stderr(io.StringIO()):
            code = items.main(argv)
        self.refused = json.loads(said.getvalue()).get("refused")
        return code

    def test_a_pass_over_a_checkout_that_was_edited_and_put_back_does_not_merge_and_is_given_back(self):
        # Found in review: the gate never read what this command had seen, so the pass below merged.
        self.review()
        worktree = Path(self.fields()["worktree"])
        (worktree / "feature.txt").write_text("edited during the review\n")
        self.verdict()
        edited = "tracked files were changed in the checkout during the review: feature.txt"
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["effective"], out["problems"], out["checkout"], out["dirty_before"]), (2, "PASS", [edited], [edited], []))
        way_on = (
            f'it never was a review of the head, so give the attempt back (`hl-items void --id A-1 --phase grade --n 1 --note "<what changed the checkout>"`) '
            'and review the head again (`hl-flow review --id A-1`; troubles.md, "A review that does not count")'
        )
        self.assertEqual(out["next"], way_on)
        git(worktree, "checkout", "--", "feature.txt")
        # Put back, the checkout looks as if nobody had touched it. The first read is on the review's attempt and
        # stands: read again, and read again after the file this command writes is deleted (found in the second
        # review: the file was then written anew, clean), it says what it said.
        for forget in (False, True):
            if forget:
                (self.item_dir() / "review-1.json").unlink()
            code, again = self.reviewed("--no-comment")
            self.assertEqual((code, again["problems"], again["checkout"]), (2, [edited], [edited]), forget)
            with self.assertRaises(Refusal) as caught:
                self.gate()
            self.assertEqual(str(caught.exception), f"review 1 does not count for a merge: {edited}; {way_on}")
        # The way on, run as the refusal names it: the attempt is given back, with what it left set aside, and the head reviewed again.
        self.assertEqual(len(self.run_named(str(caught.exception))), 2)
        self.assertEqual(self.attempts()["grade"], 1)
        given_back = self.attempts()["log"][-2]
        self.assertEqual((given_back["n"], given_back["voided"]["given_back"], given_back["voided"]["note"]), (1, True, "<what changed the checkout>"))
        aside = self.item_dir() / "voided" / "grade-1.1"
        self.assertEqual(sorted(p.name for p in aside.glob("*erdict-1.json")) + sorted(p.name for p in aside.glob("review-1.json")), ["verdict-1.json", "review-1.json"])
        self.assertFalse((self.item_dir() / "verdict-1.json").exists())
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["review"], out["problems"], out["checkout"]), (0, 1, [], []))
        self.assertNotIn("next", out)
        self.assertEqual(self.gate()["head_sha"], self.head)

    def test_a_review_that_wrote_a_verdict_over_an_untouched_checkout_is_not_given_back(self):
        for word, results in (("FAIL", ("pass", "fail")), ("PASS", ("pass", "pass"))):
            self.review()
            n = self.attempts()["grade"]
            self.verdict(word, results, n=n)
            self.reviewed("--no-comment")
            self.assertEqual(self.void(), 2)
            self.assertEqual(self.refused, [f"verdict-{n}.json exists: that attempt produced a result and stays counted"])
            self.assertEqual(self.attempts()["grade"], n)

    def test_a_review_that_did_not_pass_stands_whatever_happened_to_its_checkout(self):
        # Found in the third review: a fail could be erased by whoever changed a tracked file before it was first read.
        # A fail over a changed checkout sends the build back like any fail, which is the safe side.
        worktree = Path(self.fields()["worktree"])
        edited = "tracked files were changed in the checkout during the review: feature.txt"
        for n, (word, results) in enumerate((("INCONCLUSIVE", ("pass", "not_checked")), ("FAIL", ("pass", "fail"))), 1):
            self.review()
            (worktree / "feature.txt").write_text(f"edited during review {n}\n")
            self.verdict(word, results, n=n)
            code, out = self.reviewed("--no-comment")
            self.assertEqual((code, out["effective"], out["problems"], out["checkout"]), (0, word, [], [edited]))
            self.assertNotIn("next", out)
            self.assertEqual(out["notes"], [f"the checkout changed under this review ({edited}), and a review that did not pass stands as it is"])
            self.assertEqual(self.void(), 2)
            self.assertEqual(
                self.refused,
                [
                    f"verdict-{n}.json exists: that attempt produced a result and stays counted. Its checkout changed under it, and only a pass over such a "
                    "checkout is given back: a review that did not pass stands as what it is"
                ],
            )
            self.assertEqual((self.attempts()["grade"], items.given_back(self.attempts())), (n, []))
            with self.assertRaises(Refusal) as caught:
                self.gate()
            self.assertEqual(str(caught.exception), f"the latest grade is {word}, not PASS")
            git(worktree, "checkout", "--", "feature.txt")
        # Both stay counted, and neither is named as one to give back.
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertEqual(str(caught.exception), "grade bound reached for build 1 of A-1: 2 of 2")

    def test_a_verdict_nobody_can_use_is_not_given_back_for_its_checkout_either(self):
        self.review()
        (Path(self.fields()["worktree"]) / "feature.txt").write_text("edited during the review\n")
        self.write("A-1", "verdict-1.json", json.dumps({"verdict": "PASS"}))
        self.write("A-1", "grade-1.done", "")
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["effective"]), (2, None))
        self.assertNotIn("next", out)
        self.assertEqual(self.void(), 2)
        self.assertIn("only a pass over such a checkout is given back", self.refused[0])

    def rewrites(self, name="requirements.txt", text="pytest==8\n# rewritten when the build was served\n"):
        """The served build rewrites a tracked file by itself, each time it comes up, and the program's spec says so."""
        self.repo(rewrites=[name])
        self.herdr.on_serve = lambda: (Path(self.fields()["worktree"]) / name).write_text(text)

    def test_a_file_the_served_build_rewrote_before_the_reviewer_started_holds_nothing_up(self):
        # A tracked file the setup or the served build rewrites by itself. Found in the second review: every
        # review of such a repository was refused at the gate, and a new review met the same file.
        self.rewrites()
        self.review()
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["problems"], out["checkout"], out["dirty_before"]), (0, [], [], ["requirements.txt"]))
        self.assertEqual(read_json(self.item_dir() / "review-1.json")["dirty_before"], ["requirements.txt"])
        self.assertEqual(self.gate()["head_sha"], self.head)

    def test_a_file_that_differed_at_the_start_and_differs_otherwise_at_the_end_was_changed_during_the_review(self):
        worktree = Path(self.fields()["worktree"])
        self.repo(rewrites=["requirements.txt", "/README.md"])
        self.herdr.on_serve = lambda: [(worktree / name).write_text("rewritten when the build was served\n") for name in ("requirements.txt", "README.md")]
        self.review()
        (worktree / "requirements.txt").write_text("pytest==8\n# rewritten again, by the reviewer's own install\n")
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["checkout"], out["dirty_before"]), (2, ["tracked files were changed in the checkout during the review: requirements.txt"], ["README.md"]))

    def test_a_file_that_differed_at_the_start_and_was_put_back_was_changed_during_the_review(self):
        # Left as it was found is the rule, in both directions: the build that was served had the file as it was at the start.
        self.rewrites()
        self.review()
        git(Path(self.fields()["worktree"]), "checkout", "--", "requirements.txt")
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["checkout"]), (2, ["tracked files were changed in the checkout during the review: requirements.txt"]))

    def test_a_checkout_that_is_not_at_the_commit_under_review_is_a_problem_with_the_review(self):
        self.review()
        worktree = Path(self.fields()["worktree"])
        (worktree / "late.txt").write_text("committed under the reviewer\n")
        git(worktree, "add", "late.txt")
        git(worktree, "commit", "-m", "A-1: one more thing")
        at = git(worktree, "rev-parse", "HEAD")
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["checkout"]), (2, [f"the checkout moved during the review: it is at {at[:12]}, the commit under review is {self.head[:12]}"]))

    def test_the_state_of_the_checkout_is_noted_on_the_attempt_before_the_reviewer_exists(self):
        self.rewrites(text="pytest==9\n")
        seen = []
        self.herdr.on_take = lambda name, text: seen.append(self.attempts()["log"][-1].get("baseline"))
        self.review()
        baseline = self.attempts()["log"][-1]["baseline"]
        self.assertEqual((baseline["head"], list(baseline["dirty"])), (self.head, ["requirements.txt"]))
        self.assertEqual(seen, [baseline])
        self.assertNotIn("read", self.attempts()["log"][-1])
        self.verdict()
        self.reviewed("--no-comment")
        read = self.attempts()["log"][-1]["read"]
        self.assertEqual((read["checkout"], read["dirty_before"], bool(read["at"])), ([], ["requirements.txt"], True))

    def not_the_commit(self, *paths):
        worktree = self.fields()["worktree"]
        return (
            f"the checkout of A-1 is not the commit under review: tracked files in it differ from that commit ({', '.join(paths)}). A review is of what is "
            f"committed, so none was started and none is spent. Put the files back as the commit has them (`git -C {worktree} reset --hard HEAD`; it "
            "leaves untracked files alone) and run this again. What an earlier review's read found is on its attempt, and this does not change it"
        )

    def test_a_review_does_not_begin_on_a_checkout_that_is_not_the_commit(self):
        # Whatever is in the checkout before the build is served was put there by something that is not the commit:
        # the reviewer would run it, and the read at the end would take it for something that was so before.
        worktree = Path(self.fields()["worktree"])
        (worktree / "feature.txt").write_text("a fix nobody committed\n")
        (worktree / "README.md").unlink()
        (worktree / "new.txt").write_text("staged, never committed\n")
        git(worktree, "add", "new.txt")
        git(worktree, "mv", "requirements.txt", "reqs.txt")
        (worktree / "scratch.log").write_text("untracked, and nobody's business\n")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertEqual(str(caught.exception), self.not_the_commit("README.md", "feature.txt", "new.txt", "reqs.txt"))
        # Nothing was started and nothing is spent: no build served, no reviewer, no attempt.
        self.assertEqual((self.host.listeners(8401), spawn._open_workers(self.run_dir, "A-1"), self.attempts()["grade"]), ([], [], 0))
        self.assertFalse((self.item_dir() / "serve-1.json").exists())
        # The way out, as the refusal names it: it works for every kind of change, and leaves what git does not track.
        self.assertEqual(self.run_named(str(caught.exception)), [f"git -C {worktree} reset --hard HEAD"])
        self.assertEqual(git(worktree, "status", "--porcelain"), "?? scratch.log")
        self.assertEqual(self.review()[0], 0)
        self.assertEqual(self.attempts()["log"][-1]["baseline"], {"head": self.head, "dirty": {}})

    def test_more_changed_files_than_fit_a_line_are_counted(self):
        worktree = Path(self.fields()["worktree"])
        for k in range(10):
            (worktree / f"f{k}.txt").write_text("staged\n")
            git(worktree, "add", f"f{k}.txt")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertIn("differ from that commit (f0.txt, f1.txt, f2.txt, f3.txt, f4.txt, f5.txt, f6.txt, f7.txt and 2 more). A review is", str(caught.exception))

    def test_a_reviewer_started_by_hand_does_not_begin_on_a_changed_checkout_either(self):
        # `hl-spawn start` serves nothing, so the checkout as it is then is what its reviewer would be given.
        worktree = Path(self.fields()["worktree"])
        (worktree / "feature.txt").write_text("a fix nobody committed\n")
        opts = spawn._parser().parse_args(["--root", str(self.root), "--run-dir", str(self.run_dir), "start", "--id", "A-1", "--phase", "grade", "--engine", "claude"])
        opts.sets = {"READY_SUMMARY": "nothing is served"}
        with self.assertRaises(Refusal) as caught:
            spawn.start(self.root, self.run_dir, "A-1", "grade", "claude", self.herdr, opts, envs=self.envs, tools=self.tools)
        self.assertEqual(str(caught.exception), self.not_the_commit("feature.txt"))
        self.assertEqual((self.attempts()["grade"], sorted(p.name for p in self.item_dir().glob("grade-1.*"))), (0, []))
        self.run_named(str(caught.exception))
        self.assertTrue(spawn.start(self.root, self.run_dir, "A-1", "grade", "claude", self.herdr, opts, envs=self.envs, tools=self.tools)["started"])

    def test_what_a_given_back_review_left_in_the_checkout_is_not_what_the_next_one_reviews(self):
        # Found while closing the third review's findings: a pass over an edited checkout was given back, the edit
        # stayed, and the next review read it as a file that had differed before it began. That pass merged a
        # commit its reviewer never ran.
        worktree = Path(self.fields()["worktree"])
        self.review()
        (worktree / "feature.txt").write_text("the reviewer's own fix, never committed\n")
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual(code, 2)
        with self.assertRaises(Refusal) as caught:
            self.run_named(out["next"])
        self.assertEqual(str(caught.exception), self.not_the_commit("feature.txt"))
        self.assertEqual((self.attempts()["grade"], len(items.given_back(self.attempts()))), (0, 1))
        self.run_named(str(caught.exception))
        self.assertEqual((worktree / "feature.txt").read_text(), "feature\n")
        self.assertEqual(self.review()[0], 0)
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["problems"], out["checkout"], out["dirty_before"]), (0, [], [], []))
        self.assertEqual(self.gate()["head_sha"], self.head)

    def parked(self):
        return (
            "give_back bound reached for A-1: 1 of 1, so it is not given back again. Something changes the checkout whenever this item is reviewed: "
            f'park it for the operator (`hl-items set --id A-1 --status parked --note "environment: <what changes the checkout during a review>"`; '
            'troubles.md, "A tracked file changes during every review")'
        )

    def test_a_pass_is_given_back_once_for_an_item_and_after_that_the_item_is_the_operators(self):
        # Found in the third review, and run here as it was run there: change a tracked file, have the review read,
        # void it, review again. The count of reviews went back each time, without end.
        worktree = Path(self.fields()["worktree"])
        edited = "tracked files were changed in the checkout during the review: feature.txt"
        nexts = []
        for n in (1, 1, 2):
            git(worktree, "checkout", "--", "feature.txt")
            self.assertEqual(self.review()[0], 0)
            (worktree / "feature.txt").write_text("changed before the review was first read\n")
            self.verdict(n=n)
            code, out = self.reviewed("--no-comment")
            self.assertEqual((code, out["review"], out["checkout"]), (2, n, [edited]))
            nexts.append(out["next"])
            given = self.void()
            if len(nexts) == 1:
                self.assertEqual((given, self.attempts()["grade"]), (0, 0))
                continue
            # The second time, and every time after it: refused, counted, and the end is the one every bound has.
            self.assertEqual((given, self.attempts()["grade"], len(items.given_back(self.attempts()))), (2, n, 1))
            self.assertEqual(self.refused, [f"verdict-{n}.json exists: that attempt produced a result and stays counted. Review {n} passed over a checkout that changed under it; {self.parked()}"])
            self.assertEqual(out["next"], self.parked())
            with self.assertRaises(Refusal) as caught:
                self.gate()
            self.assertEqual(str(caught.exception), f"review {n} does not count for a merge: {edited}; {self.parked()}")
        self.assertIn("give the attempt back", nexts[0])
        git(worktree, "checkout", "--", "feature.txt")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertEqual(str(caught.exception), "grade bound reached for build 1 of A-1: 2 of 2")
        self.run_named(nexts[-1])
        item = items.load_item(self.run_dir, "A-1")
        self.assertEqual((item["status"], item["note"]), ("parked", "environment: <what changes the checkout during a review>"))

    def test_the_bound_on_giving_back_is_read_from_the_attempt_log_by_whoever_asks(self):
        # A manager that took the program over knows nothing of what the one before it gave back: the log does.
        worktree = Path(self.fields()["worktree"])
        answers = []
        for _ in range(2):
            git(worktree, "checkout", "--", "feature.txt")
            self.review()
            (worktree / "feature.txt").write_text("changed before the review was first read\n")
            self.verdict()
            self.reviewed("--no-comment")
            # Another process each time, with nothing but the run directory.
            argv = [sys.executable, "-m", "hl.items", "--run-dir", str(self.run_dir), "void", "--id", "A-1", "--phase", "grade", "--n", "1", "--note", "the checkout changed"]
            ran = subprocess.run(argv, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            answers.append(ran.returncode)
        marked = [(e["n"], e["voided"]["given_back"]) for e in read_json(self.item_dir() / "attempts.json")["log"] if e.get("voided")]
        self.assertEqual((answers, marked), ([0, 2], [(1, True)]))
        self.assertIn("give_back bound reached for A-1: 1 of 1", ran.stderr)

    def test_a_run_keeps_the_bound_on_giving_back_it_was_made_with(self):
        self.policy(bounds={"give_back": 2})
        worktree = Path(self.fields()["worktree"])
        voids = []
        for _ in range(3):
            git(worktree, "checkout", "--", "feature.txt")
            self.review()
            (worktree / "feature.txt").write_text("changed before the review was first read\n")
            self.verdict()
            self.reviewed("--no-comment")
            voids.append(self.void())
        self.assertEqual(voids, [0, 0, 2])
        self.assertIn("give_back bound reached for A-1: 2 of 2", self.refused[0])

    def test_giving_back_is_not_a_phase_anything_starts(self):
        with self.assertRaises(Refusal) as caught:
            items.start_phase(self.run_dir, "A-1", "give_back", session="s")
        self.assertEqual(str(caught.exception), "unknown phase 'give_back'; one of agree, build, grade, correction, ci_rerun, verify")
        for verb in (["start"], ["void", "--note", "x"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as said:
                items.main(["--run-dir", str(self.run_dir), *verb, "--id", "A-1", "--phase", "give_back"])
            self.assertIn("invalid choice: 'give_back'", said.getvalue())
        self.assertNotIn("give_back", self.attempts())

    def test_a_give_back_that_was_cut_short_is_on_record_and_is_finished_by_the_same_command(self):
        # Its mark is written before its files move. The other way round the verdict would be gone while the review was
        # still live, and it could be voided as an attempt that never produced a result, outside the bound.
        worktree = Path(self.fields()["worktree"])
        self.review()
        (worktree / "feature.txt").write_text("edited during the review\n")
        self.verdict()
        self.reviewed("--no-comment")
        git(worktree, "checkout", "--", "feature.txt")

        class Cut(BaseException):
            pass

        with mock.patch.object(items, "_set_aside", side_effect=Cut), self.assertRaises(Cut):
            items.void_attempt(self.run_dir, "A-1", "grade", 1, "the reviewer edited a file")
        attempts = self.attempts()
        self.assertEqual((attempts["grade"], attempts["log"][-1]["voided"]["given_back"], "set_aside" in attempts["log"][-1]["voided"]), (0, True, False))
        self.assertTrue((self.item_dir() / "verdict-1.json").exists())
        # What it left is still under its number, and the start of the next review says how to finish.
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertIn("void it with `hl-items void --id A-1 --phase grade --n 1 --note <why>`", str(caught.exception))
        self.assertEqual(self.void(n=1), 0)
        self.assertEqual(self.attempts()["log"][-1]["voided"]["set_aside"], "voided/grade-1.1")
        self.assertEqual((self.attempts()["grade"], len(items.given_back(self.attempts())), (self.item_dir() / "verdict-1.json").exists()), (0, 1, False))
        self.review()
        (worktree / "feature.txt").write_text("edited during the review\n")
        self.verdict()
        self.reviewed("--no-comment")
        self.assertEqual(self.void(), 2)
        self.assertIn("give_back bound reached for A-1: 1 of 1", self.refused[0])

    def test_an_attempt_voided_for_leaving_no_result_is_no_give_back(self):
        # A reviewer that died is voided as often as it dies, as it always was, and uses up nothing.
        worktree = Path(self.fields()["worktree"])
        for _ in range(2):
            self.review()
            flow.serve.stop(self.run_dir, "A-1", self.herdr, self.host)
            spawn.close(self.run_dir, "A-1", "grade", 1, self.herdr, sleep=lambda s: None)
            self.assertEqual(self.void("the reviewer's pane went away"), 0)
        self.assertEqual((len([e for e in self.attempts()["log"] if e.get("voided")]), items.given_back(self.attempts())), (2, []))
        self.review()
        (worktree / "feature.txt").write_text("edited during the review\n")
        self.verdict()
        self.reviewed("--no-comment")
        self.assertEqual(self.void(), 0)
        self.assertEqual(len(items.given_back(self.attempts())), 1)

    def test_the_session_of_a_review_that_was_given_back_has_evaluated_the_item(self):
        worktree = Path(self.fields()["worktree"])
        self.review()
        session = read_json(self.item_dir() / "grade-1.worker.json")["session"]
        (worktree / "feature.txt").write_text("edited during the review\n")
        self.verdict()
        self.reviewed("--no-comment")
        git(worktree, "checkout", "--", "feature.txt")
        self.assertEqual(self.void(), 0)
        for phase, why in (("grade", "already evaluated this item; every evaluation is a fresh session"), ("build", "has evaluated this item; it cannot build")):
            with self.assertRaises(Refusal) as caught:
                items.start_phase(self.run_dir, "A-1", phase, session=session, engine="claude", head_sha=self.head)
            self.assertIn(f"session {session} {why}", str(caught.exception))

    def test_two_passes_over_a_checkout_that_changed_at_the_bound_name_the_one_that_may_be_given_back(self):
        # Without the way on, the item would be where every command refuses: the gate, and a third review past the bound.
        edited = "tracked files were changed in the checkout during the review: feature.txt"
        worktree = Path(self.fields()["worktree"])
        for n in (1, 2):
            self.review()
            (worktree / "feature.txt").write_text(f"edited during review {n}\n")
            self.verdict(n=n)
            self.assertEqual(self.reviewed("--no-comment")[1]["checkout"], [edited])
            git(worktree, "checkout", "--", "feature.txt")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertEqual(
            str(caught.exception),
            "grade bound reached for build 1 of A-1: 2 of 2. Review 2 passed over a checkout that changed under it and may be given back: "
            f'`hl-items void --id A-1 --phase grade --n 2 --note "<what changed the checkout>"`',
        )
        self.run_named(str(caught.exception))
        self.assertEqual(self.review()[0], 0)
        # Once the item's give-back is used, nothing is named: the bound is the bound.
        (worktree / "feature.txt").write_text("edited during review 2, again\n")
        self.verdict(n=2)
        self.reviewed("--no-comment")
        git(worktree, "checkout", "--", "feature.txt")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertEqual(str(caught.exception), "grade bound reached for build 1 of A-1: 2 of 2")

    def test_at_the_bound_a_pass_that_read_clean_is_not_named_as_one_to_give_back(self):
        for n in (1, 2):
            self.review()
            self.verdict(n=n)
            self.assertEqual(self.reviewed("--no-comment")[0], 0)
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertEqual(str(caught.exception), "grade bound reached for build 1 of A-1: 2 of 2")

    def test_the_first_read_is_written_once_and_only_for_a_review_there_is(self):
        self.review()
        first = items.record_read(self.run_dir, "A-1", 1, {"checkout": ["tracked files were changed in the checkout during the review: x"], "dirty_before": []})
        again = items.record_read(self.run_dir, "A-1", 1, {"checkout": [], "dirty_before": []})
        self.assertEqual((again, self.attempts()["log"][-1]["read"]), (first, first))
        self.assertEqual(first["checkout"], ["tracked files were changed in the checkout during the review: x"])
        with self.assertRaises(Refusal) as caught:
            items.record_read(self.run_dir, "A-1", 2, {"checkout": [], "dirty_before": []})
        self.assertIn("A-1 has no review 2", str(caught.exception))

    def test_past_the_bound_only_a_review_of_this_build_is_named_as_one_to_give_back(self):
        # Build 1's one review was read over a changed checkout; build 2 has had its two, both read clean.
        self.review()
        worktree = Path(self.fields()["worktree"])
        (worktree / "feature.txt").write_text("edited during the review\n")
        self.verdict("FAIL", ("pass", "fail"))
        self.reviewed("--no-comment")
        git(worktree, "checkout", "--", "feature.txt")
        items.start_phase(self.run_dir, "A-1", "build", session="build-2", engine="claude")
        for n in (2, 3):
            items.start_phase(self.run_dir, "A-1", "grade", session=f"rev-{n}", head_sha=self.head)
            items.record_read(self.run_dir, "A-1", n, {"checkout": [], "dirty_before": []})
        self.assertEqual(items.bound_reached(self.run_dir, "A-1", "grade"), "grade bound reached for build 2 of A-1: 2 of 2")

    def test_what_a_reviewers_commands_left_behind_is_not_a_problem(self):
        self.review()
        (Path(self.fields()["worktree"]) / ".pytest_cache").mkdir()
        (Path(self.fields()["worktree"]) / ".pytest_cache" / "x").write_text("cache\n")
        self.verdict()
        self.assertEqual(self.reviewed()[1]["problems"], [])

    def test_a_review_is_also_read_against_its_own_transcript(self):
        self.policy(review="code")
        self.review()
        session = read_json(self.item_dir() / "grade-1.worker.json")["session"]
        write_transcript(self.home, session, [("Bash", {"command": "git diff origin/main...HEAD | cat"})])
        fix = {"criterion": 2, "what": "prints 2", "where": "out.txt", "evidence": []}
        self.verdict("FAIL", ("pass", "fail"), fix_list=[fix])
        code, out = self.reviewed()
        self.assertEqual((code, out["effective"], out["fix_list"]), (2, "FAIL", 1))
        self.assertTrue(any("the reviewer ran nothing" in p for p in out["problems"]))
        self.assertEqual(out["exercise"]["exercised"], False)
        body = (self.item_dir() / "verdict-1.comment.md").read_text()
        self.assertIn("Fix list:\n- criterion 2: prints 2 (out.txt)", body)
        # Found in review: the pull request must not carry a verdict that reads as good when the gate will refuse it.
        self.assertIn("**This review does not count for a merge:**\n- review 1: the reviewer ran nothing", body)

    def test_a_review_nobody_read_does_not_merge_until_it_is_read(self):
        self.policy(review="code")
        self.review()
        session = read_json(self.item_dir() / "grade-1.worker.json")["session"]
        write_transcript(self.home, session, [("Bash", {"command": "curl -s http://127.0.0.1:8401/health"})])
        self.verdict(fix_list=[])
        with self.assertRaises(Refusal) as caught:
            self.gate()
        self.assertEqual(str(caught.exception), "review 1 has not been read: `hl-flow reviewed --id A-1` reads it against its transcript and its checkout, and a merge rests on what that finds")
        self.assertEqual(self.run_named(str(caught.exception)), ["hl-flow reviewed --id A-1"])
        self.assertEqual(self.gate()["head_sha"], self.head)

    def test_a_review_that_was_read_has_a_result_whether_or_not_its_file_is_still_there(self):
        # Found in the fifth review: a fail that was read, its verdict then deleted, was voided as an attempt that left nothing, as often as wanted.
        self.review()
        self.verdict("FAIL", ("pass", "fail"))
        self.reviewed("--no-comment")
        (self.item_dir() / "verdict-1.json").unlink()
        self.assertEqual(self.void(), 2)
        self.assertEqual(self.refused, ["grade 1 was read (`hl-flow reviewed`), so it produced a result whether or not verdict-1.json is still there and stays counted"])
        self.assertEqual((self.attempts()["grade"], [e for e in self.attempts()["log"] if e.get("voided")]), (1, []))
        # One that nobody read, and that left no verdict, is still an attempt with no result.
        self.review()
        flow.serve.stop(self.run_dir, "A-1", self.herdr, self.host)
        spawn.close(self.run_dir, "A-1", "grade", 2, self.herdr, sleep=lambda s: None)
        self.assertEqual((self.void(), self.attempts()["grade"]), (0, 1))

    def test_a_pass_whose_verdict_was_gone_when_it_was_given_back_is_still_the_pass_that_was_read(self):
        self.review()
        (Path(self.fields()["worktree"]) / "feature.txt").write_text("edited during the review\n")
        self.verdict()
        self.reviewed("--no-comment")
        (self.item_dir() / "verdict-1.json").unlink()
        self.assertEqual(self.void(), 0)
        self.assertEqual(len(items.given_back(self.attempts())), 1)

    def test_a_verdict_rewritten_after_it_was_read_is_not_what_a_merge_stands_on(self):
        self.review()
        (Path(self.fields()["worktree"]) / "feature.txt").write_text("edited during the review\n")
        self.verdict("FAIL", ("pass", "fail"))
        self.reviewed("--no-comment")
        git(Path(self.fields()["worktree"]), "checkout", "--", "feature.txt")
        # A fail, rewritten into a pass by something that can write the run directory.
        self.verdict()
        with self.assertRaises(Refusal) as caught:
            self.gate()
        self.assertEqual(
            str(caught.exception),
            "verdict-1.json is not the verdict `hl-flow reviewed` read: it was changed afterwards, or is gone. Nothing merges on it: review the head again (`hl-flow review --id A-1`)",
        )
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["problems"]), (2, ["verdict-1.json is not the verdict that was first read: it was changed afterwards, or is gone"]))
        self.assertNotIn("next", out)
        # It is not given back as a pass over a changed checkout either: the word that was read was FAIL.
        self.assertEqual(self.void(), 2)
        self.assertIn("only a pass over such a checkout is given back", self.refused[0])
        self.assertEqual(len(self.run_named(str(caught.exception))), 1)
        self.verdict(n=2)
        self.assertEqual(self.reviewed("--no-comment")[0], 0)
        self.assertEqual(self.gate()["head_sha"], self.head)

    def elsewhere(self, *paths):
        worktree = self.fields()["worktree"]
        base = git(self.clone, "rev-parse", "origin/main")
        differ = f"; tracked files in it differ from the commit it is at ({', '.join(paths)})" if paths else ""
        reset = f"`git -C {worktree} reset --hard HEAD`, then " if paths else ""
        return (
            f"the checkout of A-1 is not the commit under review: it is at {base[:12]}, and the commit under review is {self.head[:12]}{differ}. A review is of "
            f"what is committed, so none was started and none is spent. Put the checkout back on that commit ({reset}`git -C {worktree} checkout -B {self.BRANCH} "
            f"{self.head}`; it leaves untracked files alone) and run this again. What an earlier review's read found is on its attempt, and this does not change it"
        )

    def test_a_review_does_not_begin_on_a_clean_checkout_of_another_commit(self):
        # Found in the fifth review: only the files were asked about, never where the checkout was.
        worktree = Path(self.fields()["worktree"])
        git(worktree, "checkout", "-q", "--detach", "origin/main")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertEqual(str(caught.exception), self.elsewhere())
        self.assertEqual((self.host.listeners(8401), self.attempts()["grade"], (self.item_dir() / "serve-1.json").exists()), ([], 0, False))
        self.assertEqual(self.run_named(str(caught.exception)), [f"git -C {worktree} checkout -B {self.BRANCH} {self.head}"])
        self.assertEqual((git(worktree, "rev-parse", "HEAD"), git(worktree, "rev-parse", "--abbrev-ref", "HEAD")), (self.head, self.BRANCH))
        self.assertEqual(self.review()[0], 0)
        # With an edit there as well, the files are put back first.
        self.verdict("FAIL", ("pass", "fail"))
        self.reviewed("--no-comment")
        git(worktree, "checkout", "-q", "--detach", "origin/main")
        (worktree / "README.md").write_text("edited at the other commit\n")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertEqual(str(caught.exception), self.elsewhere("README.md"))
        self.assertEqual(len(self.run_named(str(caught.exception))), 2)
        self.assertEqual((git(worktree, "rev-parse", "HEAD"), git(worktree, "status", "--porcelain")), (self.head, ""))

    def test_a_reviewer_that_switched_commits_to_compare_does_not_park_an_honest_item(self):
        # As it was run in the fifth review: the pass is given back as every refusal names it, and the next review
        # began at the other commit, passed there, and the item was parked for a file that "changes during every review".
        worktree = Path(self.fields()["worktree"])
        self.review()
        git(worktree, "checkout", "-q", "--detach", "origin/main")
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, len(out["checkout"])), (2, 1))
        self.assertIn("the checkout moved during the review", out["checkout"][0])
        with self.assertRaises(Refusal) as caught:
            self.run_named(out["next"])
        self.assertEqual(str(caught.exception), self.elsewhere())
        self.run_named(str(caught.exception))
        self.assertEqual(self.review()[0], 0)
        self.assertEqual(self.attempts()["log"][-1]["baseline"], {"head": self.head, "dirty": {}})
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["problems"]), (0, []))
        self.assertEqual(self.gate()["head_sha"], self.head)

    def test_a_branch_that_went_on_since_its_build_was_accepted_is_taken_up_and_not_put_back(self):
        # Found in the second look: after a pass the base was merged in and pushed, as `troubles.md` says to; the refusal
        # named `checkout -B <branch> <head on record>`, the review after it was of the old head, and the item ran out of reviews.
        worktree = Path(self.fields()["worktree"])
        self.review()
        self.verdict()
        self.reviewed("--no-comment")
        (worktree / "late.txt").write_text("the base, merged in\n")
        git(worktree, "add", "late.txt")
        git(worktree, "commit", "-m", "A-1: take the base in")
        git(worktree, "push", "origin", self.BRANCH)
        gone_on = git(worktree, "rev-parse", "HEAD")
        self.set_gh(gone_on)
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertEqual(
            str(caught.exception),
            f"the checkout of A-1 is not the commit under review: it is at {gone_on[:12]}, which is what its branch holds on the remote, and the commit under review is {self.head[:12]}. A review is of "
            "what is committed, so none was started and none is spent. The branch has gone on since its build was accepted (its base merged in, or a commit "
            "pushed): take the new head up (`hl-flow built --id A-1`) and run this again. What an earlier review's read found is on its attempt, and this does "
            "not change it",
        )
        self.assertEqual(self.run_named(str(caught.exception)), ["hl-flow built --id A-1"])
        self.assertEqual((self.fields()["head_sha"], git(worktree, "rev-parse", "HEAD")), (gone_on, gone_on))
        self.assertEqual(self.review()[0], 0)
        self.assertEqual(self.attempts()["log"][-1]["head_sha"], gone_on)

    def test_a_commit_made_in_the_checkout_and_never_pushed_is_put_back_and_not_taken_up(self):
        # Found in the last look: every checkout that had gone on from the head on record was sent to `hl-flow built`.
        # For a commit a reviewer made in the worktree, `built` says to push it, and no command got the item out.
        worktree = Path(self.fields()["worktree"])
        self.review()
        (worktree / "late.txt").write_text("committed under the reviewer, never pushed\n")
        git(worktree, "add", "late.txt")
        git(worktree, "commit", "-m", "a reviewer's own")
        own = git(worktree, "rev-parse", "HEAD")
        self.verdict()
        self.assertEqual(self.reviewed("--no-comment")[0], 2)
        self.assertEqual(self.void(), 0)
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertIn(f"it is at {own[:12]}, and the commit under review is {self.head[:12]}. A review is of", str(caught.exception))
        self.assertEqual(self.run_named(str(caught.exception)), [f"git -C {worktree} checkout -B {self.BRANCH} {self.head}"])
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.review()[0], 0)

    def test_a_contract_rewritten_by_something_other_than_an_install_is_refused_at_each_step_and_installed_again(self):
        # Found in the second look, and older: a builder made a criterion trivial in contract.md, the review pinned
        # the file as it found it, and the squash message cited a contract nobody had installed.
        contract = self.item_dir() / "contract.md"
        agreed = contract.read_text()
        said = "contract.md of A-1 is not the contract that was installed: something wrote the file, and a contract is changed only by installing one. Put the installed one back (`hl-items contract --id A-1 --restore`)"
        self.review()
        self.verdict()
        self.reviewed("--no-comment")
        for step, expected in ((self.built, said), (self.review, said), (self.gate, "contract.md is not the contract that was graded; grade again; " + said)):
            contract.write_text(agreed.replace("exits 0", "exits 0 or 1"))
            self.assertNotEqual(contract.read_text(), agreed)
            if step == self.built:
                self.write("A-1", "build-1.done", "")
            with self.assertRaises(Refusal) as caught:
                step()
            self.assertEqual(str(caught.exception), expected)
            self.assertEqual(self.run_named(str(caught.exception)), ["hl-items contract --id A-1 --restore"])
            self.assertEqual(contract.read_text(), agreed)
        self.assertEqual(self.gate()["head_sha"], self.head)
        with self.assertRaises(Refusal) as caught:
            contract.write_text(agreed + "\n")
            items.start_phase(self.run_dir, "A-1", "grade", session="by-hand", head_sha=self.head)
        self.assertEqual(str(caught.exception), "cannot grade A-1: " + said)
        # Where what it was installed from was written too, nothing here can say what was agreed.
        item_md = self.item_dir() / "item.md"
        item_md.write_text(item_md.read_text().replace("exits 0", "exits 0 or 1"))
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(items.main(["--run-dir", str(self.run_dir), "contract", "--id", "A-1", "--restore"]), 2)
        self.assertEqual(json.loads(out.getvalue())["refused"], ["what the contract of A-1 was installed from (accept) no longer gives the text that was installed. Leave the item as it is and tell the operator"])
        self.assertEqual(contract.read_text(), agreed + "\n")

    def test_what_the_first_read_made_of_a_reviews_exercise_is_what_the_gate_goes_by(self):
        # Found in the second look: the gate read the exercise from the files as they were then, and the record of what was served is a file.
        self.policy(review="code")
        self.review()
        write_transcript(self.home, read_json(self.item_dir() / "grade-1.worker.json")["session"], [("Bash", {"command": "python3 -m pytest tests/test_feature.py"})])
        self.verdict(fix_list=[])
        never_called = "the build was served on port 8401 and nothing in the review called it"
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["problems"], self.attempts()["log"][-1]["read"]["exercise"]), (2, [f"review 1: {never_called}"], [never_called]))
        served = self.item_dir() / "serve-1.json"
        write_json(served, {**read_json(served), "served": False})
        with self.assertRaises(Refusal) as caught:
            self.gate()
        self.assertEqual(str(caught.exception), f"review 1: {never_called}")

    def test_a_build_served_by_hand_before_its_checkout_was_put_back_is_stopped_and_served_again(self):
        # Found in the second look: the reset the refusal named put the files back under a build that had been served from the edited ones and was still up.
        worktree = Path(self.fields()["worktree"])
        self.herdr.on_serve = lambda: (worktree / "feature.txt").write_text("written by the build as it came up\n")
        self.assertEqual(flow.serve.start(self.root, self.run_dir, "A-1", self.herdr, self.host)[0], 0)
        opts = spawn._parser().parse_args(["--root", str(self.root), "--run-dir", str(self.run_dir), "start", "--id", "A-1", "--phase", "grade", "--engine", "claude"])
        opts.sets = {}
        by_hand = lambda: spawn.start(self.root, self.run_dir, "A-1", "grade", "claude", self.herdr, opts, envs=self.envs, tools=self.tools)  # noqa: E731
        with self.assertRaises(Refusal) as caught:
            by_hand()
        said = str(caught.exception)
        self.assertTrue(said.startswith("a build of A-1 is being served from this checkout as it is: stop it first (`hl-serve stop --id A-1`). The checkout of A-1 is not the commit under review: tracked files in it differ from that commit (feature.txt)."), said)
        self.assertTrue(said.endswith(f"(`git -C {worktree} reset --hard HEAD`; it leaves untracked files alone) and run this again. What an earlier review's read found is on its attempt, and this does not change it. Then serve the build again (`hl-serve start --id A-1`)"), said)
        # The reset alone leaves the build that was served from the edited file up: that is refused too, and says so.
        git(worktree, "reset", "--hard", "HEAD")
        with self.assertRaises(Refusal) as caught:
            by_hand()
        self.assertEqual(str(caught.exception), "the build of A-1 that is being served was started before its checkout was put back: stop it (`hl-serve stop --id A-1`), serve it again (`hl-serve start --id A-1`), and run this again")
        self.herdr.on_serve = None
        self.assertEqual(self.run_named(str(caught.exception)), ["hl-serve stop --id A-1", "hl-serve start --id A-1"])
        self.assertTrue(by_hand()["started"])

    def test_a_reviewer_started_by_hand_does_not_begin_on_another_commit(self):
        worktree = Path(self.fields()["worktree"])
        git(worktree, "checkout", "-q", "--detach", "origin/main")
        opts = spawn._parser().parse_args(["--root", str(self.root), "--run-dir", str(self.run_dir), "start", "--id", "A-1", "--phase", "grade", "--engine", "claude"])
        opts.sets = {"READY_SUMMARY": "nothing is served"}
        with self.assertRaises(Refusal) as caught:
            spawn.start(self.root, self.run_dir, "A-1", "grade", "claude", self.herdr, opts, envs=self.envs, tools=self.tools)
        self.assertEqual(str(caught.exception), self.elsewhere())

    def test_a_reviewer_started_by_hand_begins_over_a_file_the_spec_names_and_over_no_other(self):
        # Nothing is served by `hl-spawn start`, so what the build wrote as somebody served it is already there.
        worktree = Path(self.fields()["worktree"])
        self.repo(rewrites=["requirements.txt"])
        (worktree / "requirements.txt").write_text("pytest==8\n# rewritten when the build was served\n")
        (worktree / "feature.txt").write_text("a fix nobody committed\n")
        opts = spawn._parser().parse_args(["--root", str(self.root), "--run-dir", str(self.run_dir), "start", "--id", "A-1", "--phase", "grade", "--engine", "claude"])
        opts.sets = {"READY_SUMMARY": "nothing is served"}
        with self.assertRaises(Refusal) as caught:
            spawn.start(self.root, self.run_dir, "A-1", "grade", "claude", self.herdr, opts, envs=self.envs, tools=self.tools)
        self.assertIn("tracked files in it differ from that commit (feature.txt). A review is", str(caught.exception))
        git(worktree, "checkout", "--", "feature.txt")
        self.assertTrue(spawn.start(self.root, self.run_dir, "A-1", "grade", "claude", self.herdr, opts, envs=self.envs, tools=self.tools)["started"])
        self.assertEqual(list(self.attempts()["log"][-1]["baseline"]["dirty"]), ["requirements.txt"])

    def test_a_checkout_that_cannot_be_read_when_the_review_is_over_goes_down_as_one_that_changed(self):
        # Found in the fifth review, twice: first such a checkout was written down as a clean read; then it was
        # refused with nothing recorded, the worktree made again as the refusal said, and the fresh one read clean.
        worktree = Path(self.fields()["worktree"])
        self.review()
        (worktree / "feature.txt").write_text("edited during the review\n")
        git(self.clone, "worktree", "remove", "--force", str(worktree))
        self.verdict()
        gone = f"the checkout ({worktree}) could not be read when the review was over, so nothing shows it was left as it was found"
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["effective"], out["problems"], self.attempts()["log"][-1]["read"]["checkout"]), (2, "PASS", [gone], [gone]))
        with self.assertRaises(Refusal) as refused:
            self.gate()
        self.assertIn(f"review 1 does not count for a merge: {gone}; it never was a review of the head, so give the attempt back", str(refused.exception))
        # Made again, it reads clean; what was read the first time stands.
        self.assertEqual(self.prepare()[0], 0)
        self.assertEqual(self.reviewed("--no-comment")[1]["checkout"], [gone])
        git(self.clone, "worktree", "remove", "--force", str(worktree))
        # The way on, as each refusal names the next step: given back, the worktree made again, the head reviewed again.
        with self.assertRaises(Refusal) as caught:
            self.run_named(out["next"])
        self.assertEqual(str(caught.exception), f"the checkout of A-1 ({worktree}) cannot be read, so there is nothing to serve or to review. `hl-flow prepare --id A-1` makes it again from the item's branch")
        self.run_named(str(caught.exception))
        self.assertEqual(self.review()[0], 0)
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["problems"]), (0, []))
        self.assertEqual(self.gate()["head_sha"], self.head)

    def test_a_fail_over_a_checkout_that_cannot_be_read_stands(self):
        worktree = Path(self.fields()["worktree"])
        self.review()
        git(self.clone, "worktree", "remove", "--force", str(worktree))
        self.verdict("FAIL", ("pass", "fail"))
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["effective"], out["problems"], len(out["checkout"])), (0, "FAIL", [], 1))
        self.assertEqual(self.void(), 2)

    def test_a_review_with_no_checkout_on_record_has_nothing_to_read_of_one(self):
        self.review()
        item = items.load_item(self.run_dir, "A-1")
        del item["fields"]["worktree"]
        items.save_item(self.run_dir, item)
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["checkout"], self.attempts()["log"][-1]["read"]["checkout"]), (0, [], []))

    def test_an_item_whose_record_of_attempts_is_gone_is_refused_in_words(self):
        # Found in the fifth review: every command ended in a traceback.
        path = self.item_dir() / "attempts.json"
        path.unlink()
        said = f"{path} is missing: it is the record of every attempt of A-1, and no command makes it again. Leave the item as it is and tell the operator"
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(items.main(["--run-dir", str(self.run_dir), "show", "--id", "A-1"]), 2)
        self.assertEqual(json.loads(out.getvalue())["refused"], [said])
        for call in (self.review, self.reviewed, self.gate):
            with self.assertRaises(Refusal) as caught:
                call()
            self.assertEqual(str(caught.exception), said)

    def test_a_review_that_exercised_the_change_reads_clean(self):
        self.policy(review="code")
        self.review()
        session = read_json(self.item_dir() / "grade-1.worker.json")["session"]
        write_transcript(self.home, session, [("Bash", {"command": "curl -s http://127.0.0.1:8401/health"})])
        finding = {"kind": "criterion", "criterion": 2, "severity": "medium", "confidence": 0.9, "summary": "the item pins 1", "evidence": []}
        self.verdict("INCONCLUSIVE", ("pass", "not_checked"), fix_list=[], findings=[finding])
        code, out = self.reviewed()
        self.assertEqual((code, out["effective"], out["criteria_in_question"], out["problems"]), (0, "INCONCLUSIVE", [2], []))
        self.assertEqual((out["exercise"]["ran"], out["exercise"]["served_calls"]), (1, 1))
        body = (self.item_dir() / "verdict-1.comment.md").read_text()
        self.assertIn("The reviewer's transcript shows 1 commands that ran something, 1 calls on the served build", body)
        self.assertNotIn("does not count for a merge", body)


class RewrittenBeforeTheReviewerStartsTest(FlowCase):
    """A tracked file the repository's own setup or its served build
    rewrites before the reviewer starts. The third review read the scripts
    as refusing such a pass at the merge, with only a clean-up by hand as
    the way on. Run end to end, nothing refuses there: the check for a
    worktree that is not clean belongs to `hl-flow built`, which runs when
    a build is accepted and never after a pass. Where that check does meet
    the file, after a fail, the builder is told the command."""

    def merge(self):
        opts = merge._parser().parse_args(["--run-dir", str(self.run_dir), "--repo-dir", str(self.clone), "--id", "A-1", "--lock-ref", TEST_REF, "--lock-wait", "0.3", "--lock-poll", "0.05"])
        return merge.merge(self.run_dir, "A-1", self.clone, self.tools, opts)

    def test_a_pass_over_a_file_the_setup_rewrote_before_the_reviewer_started_merges_and_the_item_closes(self):
        # The first time, when the worktree is prepared, the setup writes only what git ignores (anything else is
        # refused there). Run again before the review, because the build changed one of its inputs, it rewrites a tracked file.
        self.repo(setup="mkdir -p .deps && if test -e .deps/ran; then echo '# pinned by the setup' >> requirements.txt; fi; touch .deps/ran", setup_inputs=["requirements.txt"], rewrites=["requirements.txt"])
        head = self.build(files=(("feature.txt", "feature\n"), ("requirements.txt", "pytest==8\nrequests==2\n")))
        self.assertEqual(self.built()[0], 0)
        worktree = Path(self.fields()["worktree"])
        code, out = self.review()
        self.assertEqual(code, 0, out)
        self.assertEqual((worktree / "requirements.txt").read_text(), "pytest==8\nrequests==2\n# pinned by the setup\n")
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["problems"], out["checkout"], out["dirty_before"]), (0, [], [], ["requirements.txt"]))
        code, merged = self.merge()
        self.assertEqual((code, merged["result"], merged["head_sha"]), (0, "merged", head))
        # What merged is the commit: the file as the setup left it never was in the pull request, and goes with the worktree.
        self.assertEqual(git(worktree, "status", "--porcelain"), "M requirements.txt")
        code, out = flow.close(self.root, self.run_dir, "A-1", self.tools, self.herdr, self.host, self.fopts("close", "--id", "A-1"))
        self.assertEqual((code, out["worktree"], worktree.exists()), (0, "removed", False))

    def test_after_a_fail_the_builder_is_told_how_to_put_such_a_file_back_and_its_rework_is_accepted(self):
        self.repo(rewrites=["requirements.txt"])
        self.herdr.on_serve = lambda: (Path(self.fields()["worktree"]) / "requirements.txt").write_text("pytest==8\n# rewritten when the build was served\n")
        self.build()
        self.built()
        worktree = Path(self.fields()["worktree"])
        self.review()
        self.verdict("FAIL", ("pass", "fail"))
        # The reviewer left the file as it found it, as its brief says, and the read holds nothing against the review.
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["effective"], out["problems"], out["dirty_before"]), (0, "FAIL", [], ["requirements.txt"]))
        self.start()
        (worktree / "feature.txt").write_text("feature, fixed\n")
        git(worktree, "add", "feature.txt")
        git(worktree, "commit", "-m", "A-1: fix what the review found")
        git(worktree, "push", "origin", self.BRANCH)
        head = git(worktree, "rev-parse", "HEAD")
        self.write("A-1", "build-2.md", SERVE)
        self.write("A-1", "build-2.done", "")
        self.set_gh(head)
        code, out = self.built()
        self.assertEqual((code, out["result"], out["refused"]), (2, "sent-back", ["the worktree is not clean: requirements.txt"]))
        told = Path(out["fix_file"]).read_text()
        self.assertIn(
            "A file git tracks that something else rewrote (an install, the build while it was served for a review) is not the item's: "
            "put it back as your last commit has it (`git checkout HEAD -- <path>`).",
            told,
        )
        # The command as the builder is given it, in its worktree.
        subprocess.run(["git", "checkout", "HEAD", "--", "requirements.txt"], cwd=worktree, check=True, capture_output=True)
        self.write("A-1", "build-2.done", "")
        code, out = self.built()
        self.assertEqual((code, out["result"], out["head_sha"]), (0, "built", head))
        # The next review begins on the commit, and what the served build then writes is again only noted.
        self.assertEqual(self.review()[0], 0)
        self.verdict(n=2)
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["review"], out["problems"], out["dirty_before"]), (0, 2, [], ["requirements.txt"]))
        self.assertEqual(self.merge()[1]["result"], "merged")


READY_WRITES = "**Serve.** `true`\n**Ready.** COMMAND `cp .scratch/works.txt feature.txt`\n"


class WrittenByTheBuildsOwnLinesTest(FlowCase):
    """What a build's own Serve and Ready lines write to tracked files before
    the reviewer starts. Found in the fifth review, with the real Ready
    command run as `hl-serve` runs it: the commit held `broken, as
    committed`, the Ready line copied a working file over it, the file was
    only noted, and the pass would have merged the broken commit. A tracked
    file that differs from the commit when the reviewer is about to start
    is allowed only where the program's spec names its path."""

    def setUp(self):
        super().setUp()
        # The Ready line is run for real, the way `hl-serve` runs it on the host.
        self.host.run = lambda command, cwd, timeout=120: subprocess.run(["/bin/sh", "-c", command], cwd=cwd, capture_output=True).returncode
        self.head = self.build(report=READY_WRITES, files=(("feature.txt", "broken, as committed\n"),))
        self.worktree = Path(self.fields()["worktree"])
        (self.worktree / ".scratch").mkdir()
        (self.worktree / ".scratch" / "works.txt").write_text("works, never committed\n")
        self.assertEqual(self.built()[0], 0)

    def test_no_review_starts_and_the_build_goes_back_with_the_files_named(self):
        code, out = self.review()
        note = self.item_dir() / "rework-2.md"
        self.assertEqual((code, out["result"], out["files"], out["note_file"]), (4, "written-before-review", ["feature.txt"], str(note)))
        self.assertEqual(
            out["detail"],
            "once the build of A-1 is served, tracked files differ from the commit under review (feature.txt): its own Serve or Ready line wrote them, or the setup "
            "run again. The reviewer would be given a build that is not the commit, so no review was started and none is spent. It goes back to its builder: start "
            "the next build, whose brief points at rework-2.md. If these are paths the repository's own setup or served build rewrites by itself, and that is "
            "harmless, the operator names them under `repos` > `owner/repo` > `rewrites` in the program's spec and stages the program again: a repository's facts "
            "are read from program.json at every review, whatever policy the run froze, so the run takes them up as it is",
        )
        # Nothing is spent and nothing is left running.
        self.assertEqual((self.attempts()["grade"], spawn._open_workers(self.run_dir, "A-1"), sorted(p.name for p in self.item_dir().glob("grade-1.*"))), (0, [], []))
        self.assertTrue(read_json(self.item_dir() / "serve-1.json")["stopped_at"])
        said = note.read_text()
        self.assertIn(f"these tracked files differed from your commit `{self.head}`:\n\n- `feature.txt`\n", said)
        self.assertIn("- What was written belongs to the item: commit and push it.\n- It does not: stop writing it.", said)
        # Asked again without a rework: the file the Ready line wrote is still there, and is put back first; then it says the same, and the note is not written twice.
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.run_named(str(caught.exception))
        self.assertEqual(self.review()[1]["result"], "written-before-review")
        self.assertEqual(note.read_text(), said)
        # The next build's brief points at the note; the builder commits what its Ready line writes, and the review starts.
        self.start()
        self.assertIn(str(note), (self.item_dir() / "build-2.brief.md").read_text())
        git(self.worktree, "commit", "-am", "A-1: the file as it is served")
        git(self.worktree, "push", "origin", self.BRANCH)
        self.write("A-1", "build-2.md", READY_WRITES)
        self.write("A-1", "build-2.done", "")
        self.set_gh(git(self.worktree, "rev-parse", "HEAD"))
        self.assertEqual(self.built()[0], 0)
        code, out = self.review()
        self.assertEqual((code, out["result"], self.attempts()["log"][-1]["baseline"]["dirty"]), (0, "reviewing", {}))

    def test_what_a_manager_wrote_for_the_next_build_stays_above_what_the_script_adds(self):
        note = self.item_dir() / "rework-2.md"
        note.write_text("Rebase is not wanted: merge the base in.\n")
        self.review()
        self.assertTrue(note.read_text().startswith("Rebase is not wanted: merge the base in.\n\n# Build 1 of A-1 wrote to tracked files as it was served\n"))

    def test_a_path_the_spec_names_is_noted_and_holds_nothing_up(self):
        # The way out the refusal names for an operator who knows the path is harmless: the spec's key, which a run reads from program.json as it is.
        self.assertEqual(self.review()[0], 4)
        git(self.worktree, "checkout", "--", "feature.txt")
        self.repo(rewrites=["feature.txt"])
        code, out = self.review()
        self.assertEqual((code, out["result"]), (0, "reviewing"))
        self.assertEqual(list(self.attempts()["log"][-1]["baseline"]["dirty"]), ["feature.txt"])
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["problems"], out["checkout"], out["dirty_before"]), (0, [], [], ["feature.txt"]))
        # Said every time, by the read and by the gate: the spec's word is all that lets such a file through.
        over = "ran over tracked files that differed from the commit, which the spec names under `rewrites`: feature.txt"
        self.assertEqual(out["notes"], [f"this review {over}"])
        self.assertEqual(merge.gate(self.run_dir, "A-1", self.clone, self.tools)["warnings"], [f"review 1 {over}"])

    def test_a_named_path_does_not_cover_another_file_written_beside_it(self):
        self.repo(rewrites=["requirements.txt"])
        code, out = self.review()
        self.assertEqual((code, out["files"]), (4, ["feature.txt"]))

    def test_many_files_are_counted_in_the_refusal_and_all_named_in_the_note(self):
        names = [f"g{k:02d}.txt" for k in range(10)]
        with self.assertRaises(spawn.WrittenBeforeReview) as caught:
            raise spawn.WrittenBeforeReview("A-1", names)
        self.assertIn(f"({', '.join(names[:8])} and 2 more)", str(caught.exception))
        note = flow.written_note(self.run_dir, "A-1", 1, self.head, names)
        self.assertEqual(note.read_text().count("\n- `g"), 10)


class RewrittenWhileTheBuildIsServedTest(FlowCase):
    """A build that writes a tracked file after its reviewer has started, or
    as it is stopped. It reads as a checkout that changed, and no reviewer
    can put back what is written as the build stops: one give-back for the
    item, then the operator, whose cure is in the repository."""

    def as_it_stops(self):
        self.herdr.on_stop = lambda: (Path(self.fields()["worktree"]) / "requirements.txt").write_text("pytest==8\n# written as the build was stopped\n")

    def test_a_pass_gets_its_one_give_back_and_is_then_the_operators_and_every_refusal_says_what_to_run(self):
        self.as_it_stops()
        self.build()
        self.built()
        changed = "tracked files were changed in the checkout during the review: requirements.txt"
        self.review()
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["checkout"]), (2, [changed]))
        # Given back, and the file the build left is put back before the head is reviewed again: each step is the one the step before names.
        with self.assertRaises(Refusal) as caught:
            self.run_named(out["next"])
        self.assertIn("the checkout of A-1 is not the commit under review: tracked files in it differ from that commit (requirements.txt)", str(caught.exception))
        self.run_named(str(caught.exception))
        self.assertEqual(self.review()[0], 0)
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["checkout"]), (2, [changed]))
        with self.assertRaises(Refusal) as caught:
            merge.gate(self.run_dir, "A-1", self.clone, self.tools)
        said = str(caught.exception)
        self.assertEqual(said, f"review 1 does not count for a merge: {changed}; {out['next']}")
        self.assertIn('park it for the operator (`hl-items set --id A-1 --status parked', said)
        self.assertIn('troubles.md, "A tracked file changes during every review"', said)
        self.run_named(said)
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "parked")

    def test_a_file_the_repository_no_longer_tracks_holds_nothing_up(self):
        # The cure the documents name, as an item's own commit: the file out of what git tracks, and ignored.
        self.as_it_stops()
        self.prepare()
        self.start()
        worktree = Path(self.fields()["worktree"])
        git(worktree, "rm", "--cached", "requirements.txt")
        (worktree / ".gitignore").write_text("requirements.txt\n")
        git(worktree, "add", ".gitignore")
        git(worktree, "commit", "-m", "A-1: the build writes requirements.txt itself")
        git(worktree, "push", "origin", self.BRANCH)
        self.write("A-1", "build-1.md", SERVE)
        self.write("A-1", "build-1.done", "")
        self.set_gh(git(worktree, "rev-parse", "HEAD"))
        self.assertEqual(self.built()[0], 0)
        self.review()
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["problems"], out["checkout"], out["dirty_before"]), (0, [], [], []))
        self.assertIn("written as the build was stopped", (worktree / "requirements.txt").read_text())


MARKS = (("--skip-worktree",), ("--assume-unchanged",), ("--skip-worktree", "--assume-unchanged"))


class HiddenFromGitTest(FlowCase):
    """A tracked file edited under the skip-worktree or the assume-unchanged
    mark (`git update-index`). Found in the fourth review: `git status`
    lists no such file, so a review could pass over an edit that is not in
    the commit and nothing noted it. The mark itself counts as a change, at
    each point where a plain edit is seen."""

    def hide(self, marks, name="feature.txt", text="edited where git does not look\n"):
        worktree = Path(self.fields()["worktree"])
        for mark in marks:
            git(worktree, "update-index", mark, "--", name)
        (worktree / name).write_text(text)
        self.assertEqual(git(worktree, "status", "--porcelain"), "")
        return worktree

    def test_a_review_does_not_begin_on_a_file_hidden_from_git_and_the_way_out_is_the_commands_it_names(self):
        self.build()
        self.built()
        for marks in MARKS:
            worktree = self.hide(marks)
            with self.assertRaises(Refusal) as caught:
                self.review()
            self.assertEqual(
                str(caught.exception),
                "the checkout of A-1 is not the commit under review: tracked files in it differ from that commit (feature.txt). Git was told not to look at "
                "1 of them (feature.txt: marked assume-unchanged or skip-worktree, by hand or by a sparse checkout, which a loop's worktree may not be), and "
                "such a file counts as changed whatever it holds. A review is of what is committed, so none was started and none is spent. Put the files back "
                f"as the commit has them (`git -C {worktree} sparse-checkout disable`, `git -C {worktree} ls-files -z | git -C {worktree} update-index "
                f"--no-skip-worktree -z --stdin`, `git -C {worktree} ls-files -z | git -C {worktree} update-index --no-assume-unchanged -z --stdin`, then "
                f"`git -C {worktree} reset --hard HEAD`; it leaves untracked files alone) and run this again. What an earlier review's read found is on its "
                "attempt, and this does not change it",
                marks,
            )
            self.assertEqual((self.host.listeners(8401), self.attempts()["grade"]), ([], 0))
            # `reset --hard` alone puts back neither the mark nor, under skip-worktree, the file.
            self.assertEqual(len(self.run_named(str(caught.exception))), 4)
            self.assertEqual((git(worktree, "ls-files", "-v", "--", "feature.txt"), (worktree / "feature.txt").read_text()), ("H feature.txt", "feature\n"), marks)
        self.assertEqual(self.review()[0], 0)
        self.assertEqual(self.attempts()["log"][-1]["baseline"], {"head": self.head_of(), "dirty": {}})

    def head_of(self):
        return git(Path(self.fields()["worktree"]), "rev-parse", "HEAD")

    def test_plain_edits_beside_a_hidden_one_are_named_with_it_and_only_the_hidden_one_is_counted_as_hidden(self):
        self.build()
        self.built()
        worktree = self.hide(("--assume-unchanged",), "requirements.txt")
        (worktree / "feature.txt").write_text("edited in plain sight\n")
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertIn("differ from that commit (feature.txt, requirements.txt). Git was told not to look at 1 of them (requirements.txt: marked", str(caught.exception))
        self.run_named(str(caught.exception))
        self.assertEqual(git(worktree, "status", "--porcelain"), "")
        self.assertEqual(self.review()[0], 0)

    def test_a_reviewer_started_by_hand_does_not_begin_on_a_file_hidden_from_git(self):
        self.build()
        self.built()
        self.hide(("--skip-worktree",))
        opts = spawn._parser().parse_args(["--root", str(self.root), "--run-dir", str(self.run_dir), "start", "--id", "A-1", "--phase", "grade", "--engine", "claude"])
        opts.sets = {"READY_SUMMARY": "nothing is served"}
        with self.assertRaises(Refusal) as caught:
            spawn.start(self.root, self.run_dir, "A-1", "grade", "claude", self.herdr, opts, envs=self.envs, tools=self.tools)
        self.assertIn("Git was told not to look at 1 of them (feature.txt: marked assume-unchanged or skip-worktree, by hand or by a sparse checkout", str(caught.exception))
        self.assertEqual(self.attempts()["grade"], 0)

    def test_a_file_the_build_hid_as_it_came_up_is_on_the_reviews_record_like_any_file_it_rewrote(self):
        # What the build does by itself as it comes up is noted, whether or not git looks at the file, and holds nothing up.
        self.policy(bounds={"grade": 3})
        self.repo(rewrites=["requirements.txt"])
        self.build()
        self.built()
        for n, marks in enumerate(MARKS, 1):
            self.herdr.on_serve = lambda marks=marks: self.hide(marks, "requirements.txt", "rewritten as the build came up\n")
            self.assertEqual(self.review()[0], 0)
            baseline = self.attempts()["log"][-1]["baseline"]["dirty"]
            self.assertEqual((list(baseline), baseline["requirements.txt"][:17]), (["requirements.txt"], "hidden from git, "), marks)
            self.verdict("INCONCLUSIVE", ("pass", "not_checked"), n=n)
            code, out = self.reviewed("--no-comment")
            self.assertEqual((code, out["checkout"], out["dirty_before"]), (0, [], ["requirements.txt"]), marks)
            # The next review of the same build begins on the commit again: the refusal says how.
            self.herdr.on_serve = None
            with self.assertRaises(Refusal) as caught:
                self.review()
            self.assertIn("Git was told not to look at 1 of them (requirements.txt:" if n < 3 else "grade bound reached for build 1 of A-1: 3 of 3", str(caught.exception))
            self.run_named(str(caught.exception))

    def test_a_second_edit_under_the_mark_while_the_review_runs_is_a_change(self):
        self.repo(rewrites=["requirements.txt"])
        self.build()
        self.built()
        self.herdr.on_serve = lambda: self.hide(("--assume-unchanged",), "requirements.txt", "rewritten as the build came up\n")
        self.review()
        (Path(self.fields()["worktree"]) / "requirements.txt").write_text("edited again, under the reviewer\n")
        self.verdict()
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["checkout"], out["dirty_before"]), (2, ["tracked files were changed in the checkout during the review: requirements.txt"], []))

    def test_an_edit_hidden_from_git_while_a_review_runs_is_what_its_first_read_finds(self):
        self.policy(bounds={"grade": 3})
        self.build()
        self.built()
        changed = "tracked files were changed in the checkout during the review: feature.txt"
        worktree = Path(self.fields()["worktree"])
        for n, marks in enumerate(MARKS[:2], 1):
            self.assertEqual(self.review()[0], 0)
            self.hide(marks)
            self.verdict("FAIL", ("pass", "fail"), n=n)
            code, out = self.reviewed("--no-comment")
            self.assertEqual((out["effective"], out["checkout"]), ("FAIL", [changed]), marks)
            for clear in ("--no-skip-worktree", "--no-assume-unchanged"):
                git(worktree, "update-index", clear, "--", "feature.txt")
            git(worktree, "checkout", "HEAD", "--", "feature.txt")
        # The mark alone, set while the review ran on a file nobody edited, is a change too: it hides whatever follows.
        self.assertEqual(self.review()[0], 0)
        git(worktree, "update-index", "--skip-worktree", "--", "feature.txt")
        self.verdict(n=3)
        code, out = self.reviewed("--no-comment")
        self.assertEqual((code, out["effective"], out["problems"]), (2, "PASS", [changed]))
        # A pass, so it is given back; and the next review does not begin until the mark is off.
        with self.assertRaises(Refusal) as caught:
            self.run_named(out["next"])
        self.assertIn("Git was told not to look at 1 of them (feature.txt: marked assume-unchanged or skip-worktree, by hand or by a sparse checkout", str(caught.exception))
        self.run_named(str(caught.exception))
        self.assertEqual(self.review()[0], 0)

    def not_accepted(self, marks):
        """A build with an edit nobody committed, under these marks, comes to `hl-flow built`; the builder runs what it is told, and it is accepted."""
        head = self.build()
        worktree = self.hide(marks, "requirements.txt", "pytest==8\n# a fix nobody committed\n")
        code, out = self.built()
        self.assertEqual((code, out["result"]), (2, "sent-back"))
        self.assertEqual(
            out["refused"],
            [
                "git was told not to look at tracked files in the worktree (requirements.txt: marked assume-unchanged or skip-worktree, by hand or by a "
                "sparse checkout, which this worktree may not be), so nothing shows they are as the pull request has them: switch a sparse checkout off "
                "(`git sparse-checkout disable`) and clear the marks (`git update-index --no-skip-worktree -- <path>` and `git update-index "
                "--no-assume-unchanged -- <path>`, one command for each mark), then commit and push a change that belongs to the item, or put the file "
                "back (`git checkout HEAD -- <path>`)"
            ],
        )
        self.assertIn("- git was told not to look at tracked files in the worktree (requirements.txt:", Path(out["fix_file"]).read_text())
        # The three commands as the builder is given them, in its worktree, with the path.
        for command in re.findall(r"`(git [^`]+)`", out["refused"][0]):
            subprocess.run(command.replace("<path>", "requirements.txt"), shell=True, cwd=worktree, check=True, capture_output=True)
        self.assertEqual((git(worktree, "ls-files", "-v", "--", "requirements.txt"), git(worktree, "status", "--porcelain")), ("H requirements.txt", ""))
        self.write("A-1", "build-1.done", "")
        code, out = self.built()
        self.assertEqual((code, out["result"], out["head_sha"]), (0, "built", head))

    def test_a_build_with_an_edit_under_skip_worktree_is_not_accepted_and_its_builder_is_told_the_commands(self):
        self.not_accepted(MARKS[0])

    def test_a_build_with_an_edit_under_assume_unchanged_is_not_accepted_and_its_builder_is_told_the_commands(self):
        self.not_accepted(MARKS[1])

    def test_a_build_with_an_edit_under_both_marks_is_not_accepted_and_its_builder_is_told_the_commands(self):
        self.not_accepted(MARKS[2])

    def test_a_plain_edit_and_a_hidden_one_are_both_said_when_a_build_is_not_accepted(self):
        self.build()
        worktree = self.hide(("--skip-worktree",), "requirements.txt")
        (worktree / "README.md").write_text("edited in plain sight\n")
        code, out = self.built()
        self.assertEqual((len(out["refused"]), out["refused"][0]), (2, "the worktree is not clean: README.md"))
        self.assertIn("git was told not to look at tracked files in the worktree (requirements.txt:", out["refused"][1])

    def test_more_hidden_files_than_fit_a_line_are_counted(self):
        names = [f"f{k:02d}.txt" for k in range(12)]
        self.build(files=tuple((name, "one line\n") for name in names))
        worktree = Path(self.fields()["worktree"])
        git(worktree, "update-index", "--assume-unchanged", "--", *names)
        code, out = self.built()
        self.assertIn(f"in the worktree ({', '.join(names[:10])} and 2 more: marked assume-unchanged or skip-worktree,", out["refused"][0])
        git(worktree, "update-index", "--no-assume-unchanged", "--", *names)
        self.write("A-1", "build-1.done", "")
        self.assertEqual(self.built()[0], 0)
        git(worktree, "update-index", "--assume-unchanged", "--", *names)
        with self.assertRaises(Refusal) as caught:
            self.review()
        first = ", ".join(names[:8]) + " and 4 more"
        self.assertIn(f"differ from that commit ({first}). Git was told not to look at 12 of them ({first}: marked assume-unchanged or skip-worktree,", str(caught.exception))

    def sparse(self, how):
        """A build accepted clean; then its worktree is made sparse, and `hl-flow built` and `hl-flow review` are asked again."""
        head = self.build()
        self.assertEqual(self.built()[0], 0)
        worktree = Path(self.fields()["worktree"])
        how(worktree)
        self.assertEqual(git(worktree, "status", "--porcelain"), "")
        self.write("A-1", "build-1.done", "")
        code, out = self.built()
        self.assertEqual((code, out["result"]), (2, "sent-back"))
        self.assertIn("marked assume-unchanged or skip-worktree, by hand or by a sparse checkout, which this worktree may not be", out["refused"][0])
        with self.assertRaises(Refusal) as caught:
            self.review()
        self.assertIn("by hand or by a sparse checkout, which a loop's worktree may not be", str(caught.exception))
        # The commands the refusal names, as written: the sparse checkout off, both marks off, the files back.
        self.assertEqual(len(self.run_named(str(caught.exception))), 4)
        self.assertEqual((git(worktree, "ls-files", "-v"), git(worktree, "status", "--porcelain")), ("H README.md\nH feature.txt\nH requirements.txt", ""))
        self.assertEqual(((worktree / "feature.txt").read_text(), (worktree / "requirements.txt").read_text()), ("feature\n", "pytest==8\n"))
        self.assertEqual(self.review()[0], 0)
        return head

    def test_a_tracked_file_a_sparse_checkout_took_out_of_the_build_is_seen(self):
        # Found in the second look: `git sparse-checkout set` takes a committed file out of the checkout, and nothing was seen.
        self.sparse(lambda worktree: git(worktree, "sparse-checkout", "set", "--no-cone", "/*", "!/requirements.txt"))

    def test_an_edit_under_a_sparse_checkout_switched_on_by_hand_is_seen(self):
        # Found in the second look: three commands and an edit, in a worktree whose files are all there.
        def by_hand(worktree):
            git(worktree, "config", "core.sparseCheckout", "true")
            git(worktree, "config", "sparse.expectFilesOutsideOfPatterns", "true")
            git(worktree, "update-index", "--skip-worktree", "--", "feature.txt")
            (worktree / "feature.txt").write_text("a fix nobody committed\n")

        self.sparse(by_hand)


class NothingServedTest(FlowCase):
    def test_an_item_that_serves_nothing_goes_through_the_same_two_steps(self):
        self.build(report=NOTHING_SERVED)
        self.built()
        code, out = self.review()
        self.assertEqual(code, 0)
        self.assertIn("Nothing is served for this item.", out["served"])
        self.verdict()
        code, out = self.reviewed()
        self.assertEqual((code, out["effective"], out["problems"]), (0, "PASS", []))


class CloseTest(FlowCase):
    def close(self):
        return flow.close(self.root, self.run_dir, "A-1", self.tools, self.herdr, self.host, self.fopts("close", "--id", "A-1"))

    def test_a_merged_items_worktree_and_local_branch_are_removed(self):
        self.build()
        self.built()
        worktree = Path(self.fields()["worktree"])
        (worktree / "node_modules").mkdir()
        (worktree / "node_modules" / "left.js").write_text("ignored build output\n")
        self.ship("A-1")
        code, out = self.close()
        self.assertEqual((code, out["worktree"], out["branch"]), (0, "removed", "removed"))
        self.assertFalse(worktree.exists())
        self.assertNotIn(self.BRANCH, git(self.clone, "branch", "--list"))
        self.assertTrue(self.fields()["closed_at"])
        # Closing again finds nothing left and says so.
        self.assertEqual(self.close()[1]["worktree"], "none")

    def test_a_failed_item_skipped_as_superseded_has_its_worktree_removed(self):
        self.build()
        self.built()
        worktree = Path(self.fields()["worktree"])
        items.set_status(self.run_dir, "A-1", "failed", "build: carried by A-2")
        with self.assertRaises(Refusal) as caught:
            self.close()
        self.assertIn("A-1 is failed: only a merged item's worktree", str(caught.exception))
        self.ship("A-2")
        items.set_status(self.run_dir, "A-1", "skipped", "superseded by A-2", superseded_by="A-2")
        code, out = self.close()
        self.assertEqual((code, out["worktree"], out["branch"]), (0, "removed", "removed"))
        self.assertFalse(worktree.exists())

    def test_only_a_merged_item_is_closed_and_never_with_a_worker_open(self):
        self.build()
        with self.assertRaises(Refusal) as caught:
            self.close()
        self.assertIn("A-1 is in_progress: only a merged item's worktree", str(caught.exception))
        self.ship("A-1")
        with self.assertRaises(Refusal) as caught:
            self.close()
        self.assertIn("still has a worker open (build-1.worker.json)", str(caught.exception))
        self.assertTrue(Path(self.fields()["worktree"]).is_dir())


class LedgerCase(FlowCase):
    """A program with its ledger in a clone of the control repository, claimed by PID."""

    PID = 4321

    def setUp(self):
        super().setUp()
        self.ledger_origin = self.tmp / "workspace.git"
        git(self.tmp, "init", "--bare", "-b", "p-ledger", str(self.ledger_origin))
        self.ledger_clone = self.root / "ledger"
        git(self.tmp, "clone", str(self.ledger_origin), str(self.ledger_clone))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.ledger_clone, "config", key, value)
        task = self.ledger_clone / "tasks" / "p"
        task.mkdir(parents=True)
        (task / "LEDGER.md").write_text("# Ledger\n\n## Now\n\nStarting.\n")
        git(self.ledger_clone, "add", ".")
        git(self.ledger_clone, "commit", "-m", "start the ledger")
        git(self.ledger_clone, "push", "-u", "origin", "HEAD:p-ledger")
        self.task = task
        self.settings(ledger=str(task / "LEDGER.md"))
        self.procs = mock.Mock()
        self.procs.start.return_value = "boot"
        self.procs.parent.return_value = (1, "zsh")
        claim.claim(self.root, self.PID, "w1:p1", None, self.procs)

    def save(self, note="A-1 merged"):
        return flow.save(self.root, self.PID, self.procs, note)

    def status(self, item_id, status):
        item = items.load_item(self.run_dir, item_id)
        item["status"] = status
        items.save_item(self.run_dir, item)


class SaveTest(LedgerCase):
    def test_a_save_writes_the_tables_commits_pushes_and_stamps_the_heartbeat(self):
        (self.task / "LEDGER.md").write_text("# Ledger\n\n## Now\n\nA-1 is building.\n")
        code, out = self.save("A-1 build started")
        self.assertEqual((code, out["committed"], out["pushed"], out["heartbeat"]), (0, True, True, "stamped"))
        self.assertEqual(git(self.ledger_clone, "log", "-1", "--format=%s"), "canary ledger: A-1 build started")
        self.assertEqual(git(self.ledger_clone, "rev-parse", "HEAD"), git(self.tmp, "--git-dir", str(self.ledger_origin), "rev-parse", "p-ledger"))
        self.assertEqual(sorted(p.name for p in self.task.iterdir()), ["FOLLOW-UPS.md", "LEDGER-ITEMS.md", "LEDGER.md"])
        self.assertEqual(read_json(self.root / "heartbeat.json")["note"], "A-1 build started")
        self.assertEqual(git(self.ledger_clone, "status", "--porcelain"), "")

    def test_a_save_tells_the_manager_of_a_seat_move_or_pause(self):
        a, b = str(self.tmp / "seat-a"), str(self.tmp / "seat-b")
        self.settings(claude_config_dir=a, claude_seats=[a, b], worker_claude_ref="-")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": a}):
            self.assertNotIn("seat", self.save("nothing moved")[1])
            write_json(self.root / "seat.json", {"home": b, "why": "a is full"})
            told = self.save("the program moved")[1]["seat"]
            self.assertEqual((told["action"], told["to"], told["why"]), ("move", b, "a is full"))
            write_json(self.root / "seat.json", {"home": b, "paused_until": 1791849600})
            self.assertEqual(self.save("paused")[1]["seat"]["action"], "pause")

    def test_a_save_by_a_manager_that_lost_the_program_writes_nothing(self):
        with self.assertRaises(Refusal) as caught:
            flow.save(self.root, self.PID + 1, self.procs, "should not land")
        self.assertIn("is not claimed by pid 4322", str(caught.exception))
        self.assertFalse((self.task / "LEDGER-ITEMS.md").exists())

    def test_a_push_that_fails_is_said_and_the_heartbeat_still_stamped(self):
        git(self.ledger_clone, "remote", "set-url", "origin", str(self.tmp / "nowhere.git"))
        code, out = self.save()
        self.assertEqual((code, out["pushed"], out["heartbeat"]), (2, False, "stamped"))
        self.assertTrue(out["push_error"])

    def test_the_ledger_is_pushed_on_the_loops_own_token_and_no_credential_the_host_holds(self):
        # Found on the host: `save` ran before the loop's token was asked for, so git took whatever the host's
        # credential helper gave, which on the mini is the operator's keyring token.
        import base64
        import http.server
        import threading

        offered = []

        class Asks(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                given = self.headers.get("Authorization")
                if given:
                    offered.append(base64.b64decode(given.split()[1]).decode())
                self.send_response(403 if given else 401)
                if not given:
                    self.send_header("WWW-Authenticate", 'Basic realm="ledger"')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Asks)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        asked = self.tmp / "the-hosts-helper-was-asked"
        git(self.ledger_clone, "config", "credential.helper", f"!f() {{ echo asked >> {asked}; echo username=operator; echo password=keyring-token; }}; f")
        git(self.ledger_clone, "remote", "set-url", "origin", f"http://127.0.0.1:{server.server_address[1]}/workspace.git")
        code, out = self.save()
        self.assertEqual((code, out["pushed"], out["heartbeat"]), (2, False, "stamped"))
        self.assertEqual((set(offered), asked.exists()), ({"x-access-token:loop-token-for-tests"}, False))
        # The kit's other git calls go the same way: the gate's fetch, here.
        offered.clear()
        self.tools.git(["fetch", "origin"], self.ledger_clone, check=False)
        self.assertEqual((set(offered), asked.exists()), ({"x-access-token:loop-token-for-tests"}, False))

    def test_without_the_loops_token_the_ledger_is_committed_and_the_heartbeat_stamped_and_nothing_is_sent(self):
        git(self.ledger_clone, "remote", "set-url", "origin", str(self.tmp / "nowhere.git"))
        code, out = flow.save(self.root, self.PID, self.procs, "A-1 merged", no_token="the program's keeper did not give the loop's token")
        self.assertEqual((code, out["committed"], out["pushed"], out["heartbeat"]), (2, True, False, "stamped"))
        self.assertEqual(out["push_error"], "not pushed, because the loop's own token could not be had: the program's keeper did not give the loop's token")
        # From the command line the token is asked for before anything is sent, and its lack is what the save then says.
        with mock.patch.object(merge, "require_loop_token", side_effect=Refusal("GH_TOKEN is not set")) as asked, mock.patch.object(flow, "save", return_value=(2, {})) as saved:
            with contextlib.redirect_stdout(io.StringIO()):
                flow.main(["--root", str(self.root), "save", "--pid", str(self.PID), "--note", "x"], procs=self.procs)
        self.assertEqual((asked.call_count, saved.call_args.kwargs["no_token"]), (1, "GH_TOKEN is not set"))
        with mock.patch.object(merge, "require_loop_token") as asked, mock.patch.object(flow, "save", return_value=(0, {})) as saved:
            with contextlib.redirect_stdout(io.StringIO()):
                flow.main(["--root", str(self.root), "save", "--pid", str(self.PID), "--note", "x"], procs=self.procs)
        self.assertEqual((asked.call_count, saved.call_args.kwargs["no_token"]), (1, None))

    def test_the_answer_lists_the_pull_requests_from_outside_that_wait_for_rollout(self):
        waiting = {"adopt": True, "label": "rollout-ready", "prs": [{"repo": "owner/repo", "pr": 12, "checks": "green"}], "errors": []}
        code, out = flow.save(self.root, self.PID, self.procs, "A-1 merged", outside_facts=lambda: waiting)
        self.assertEqual((code, out["heartbeat"], out["outside_prs_ready"]), (0, "stamped", waiting))

    def test_an_outside_read_that_breaks_is_said_and_never_fails_the_save(self):
        def broken():
            raise RuntimeError("gh exploded")

        code, out = flow.save(self.root, self.PID, self.procs, "A-1 merged", outside_facts=broken)
        self.assertEqual((code, out["heartbeat"], out["outside_prs_ready"]), (0, "stamped", {"errors": ["could not be read: gh exploded"]}))

    def test_the_command_line_reads_the_outside_pull_requests_at_every_save(self):
        from hl import outside

        with mock.patch.object(merge, "require_loop_token"), mock.patch.object(flow, "save", return_value=(0, {})) as saved, mock.patch.object(
            outside, "ready_or_said", return_value={"prs": []}
        ) as read:
            with contextlib.redirect_stdout(io.StringIO()):
                flow.main(["--root", str(self.root), "save", "--pid", str(self.PID), "--note", "x"], procs=self.procs)
            self.assertEqual(saved.call_args.kwargs["outside_facts"](), {"prs": []})
        self.assertEqual(read.call_args.kwargs["no_token"], None)

    def test_the_answer_says_how_long_items_wait_on_ci_and_a_broken_read_never_fails_the_save(self):
        timed = {"budget_minutes": {"checks": 15, "verify": 20}, "repos": {"owner/repo": {"checks_minutes": 64.0, "over": ["checks"]}}, "over_budget": ["owner/repo"], "errors": []}
        code, out = flow.save(self.root, self.PID, self.procs, "A-1 merged", ci_facts=lambda: timed)
        self.assertEqual((code, out["heartbeat"], out["ci_time"]), (0, "stamped", timed))

        def broken():
            raise RuntimeError("gh exploded")

        code, out = flow.save(self.root, self.PID, self.procs, "A-1 merged", ci_facts=broken)
        self.assertEqual((code, out["heartbeat"], out["ci_time"]), (0, "stamped", {"errors": ["could not be read: gh exploded"]}))

    def test_the_command_line_reads_the_ci_time_at_every_save(self):
        from hl import pace

        with mock.patch.object(merge, "require_loop_token", side_effect=Refusal("GH_TOKEN is not set")), mock.patch.object(
            flow, "save", return_value=(0, {})
        ) as saved, mock.patch.object(pace, "facts_or_said", return_value={"repos": {}}) as read:
            with contextlib.redirect_stdout(io.StringIO()):
                flow.main(["--root", str(self.root), "save", "--pid", str(self.PID), "--note", "x"], procs=self.procs)
            self.assertEqual(saved.call_args.kwargs["ci_facts"](), {"repos": {}})
        self.assertEqual(read.call_args.kwargs["no_token"], "GH_TOKEN is not set")

    def elsewhere(self, name, text):
        """A commit on the ledger branch made from another clone, as the operator would."""
        other = self.tmp / f"operator-{len(list(self.tmp.glob('operator-*')))}"
        git(self.tmp, "clone", str(self.ledger_origin), str(other))
        for key, value in (("user.name", "o"), ("user.email", "o@example.invalid"), ("commit.gpgsign", "false")):
            git(other, "config", key, value)
        (other / "tasks" / "p" / name).write_text(text)
        git(other, "add", ".")
        git(other, "commit", "-m", f"operator: {name}")
        git(other, "push", "origin", "HEAD:p-ledger")

    def test_what_someone_else_pushed_to_the_ledger_branch_is_taken_in_before_the_push(self):
        # Found in review: a save never pulled, so one commit by the operator left every later save unpushed.
        self.elsewhere("DECISION-A-2.md", "Go ahead.\n")
        (self.task / "LEDGER.md").write_text("# Ledger\n\n## Now\n\nA-1 is building.\n")
        code, out = self.save()
        self.assertEqual((code, out["pushed"], out["took_in_remote"]), (0, True, True))
        self.assertEqual((self.task / "DECISION-A-2.md").read_text(), "Go ahead.\n")
        self.assertEqual(git(self.ledger_clone, "rev-parse", "HEAD"), git(self.tmp, "--git-dir", str(self.ledger_origin), "rev-parse", "p-ledger"))
        self.assertNotIn("took_in_remote", self.save("again")[1])

    def test_a_ledger_that_conflicts_with_what_was_pushed_is_left_whole_and_said_to_be_unpushed(self):
        self.elsewhere("LEDGER.md", "# Ledger\n\n## Now\n\nThe operator rewrote this.\n")
        (self.task / "LEDGER.md").write_text("# Ledger\n\n## Now\n\nA-1 is building.\n")
        code, out = self.save()
        self.assertEqual((code, out["pushed"]), (2, False))
        self.assertTrue(out["push_error"])
        # Not left in the middle of a rebase, and the manager's own text is still there.
        self.assertFalse((self.ledger_clone / ".git" / "rebase-merge").exists() or (self.ledger_clone / ".git" / "rebase-apply").exists())
        self.assertIn("A-1 is building.", (self.task / "LEDGER.md").read_text())
        self.assertEqual(git(self.ledger_clone, "status", "--porcelain"), "")

    def test_the_answer_names_each_stream_with_no_item_in_progress_and_since_when(self):
        # The help-desk catalog program, 2026-11-07: streams stood idle most of a day with placeable work noted, and no
        # answer said so.
        self.settings(streams={**read_json(self.root / "program.json")["streams"], "docs": {"repo": "owner/repo", "clone": str(self.clone)}})
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {**run, "created": "2026-11-07T10:00:00Z", "events": [{"id": "A-0", "status": "merged", "reason_class": "merged", "at": "2026-11-07T11:30:00Z"}]})
        # A stream with an item in progress is at work, and is not listed; one that has no run yet is.
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "in_progress")
        self.assertEqual(self.save("A-1 building")[1]["idle"], {"streams": [{"stream": "docs", "since": None, "minutes": None, "pending": 0}]})
        item = items.load_item(self.run_dir, "A-1")
        item["status"] = "merged"
        items.save_item(self.run_dir, item)
        with mock.patch.object(flow, "utc_now", return_value="2026-11-07T15:02:40Z"):
            code, out = self.save("nothing started since A-1")
        self.assertEqual(code, 0)
        self.assertEqual(
            out["idle"],
            {"streams": [{"stream": "docs", "since": None, "minutes": None, "pending": 0}, {"stream": "main", "since": "2026-11-07T11:30:00Z", "minutes": 212, "pending": 2}]},
        )

    def test_a_run_of_no_stream_is_named_and_never_counted_for_one(self):
        # A run made without `--stream`, or for a stream the program no longer has: its item in progress is nobody's,
        # so the answer says the list cannot be complete.
        run = read_json(self.run_dir / "run.json")
        write_json(self.run_dir / "run.json", {k: v for k, v in run.items() if k != "stream"})
        idle = self.save()[1]["idle"]
        self.assertEqual(([s["stream"] for s in idle["streams"]], idle["runs_outside_streams"]), (["main"], [run["run_id"]]))

    def test_an_idle_read_that_breaks_is_said_and_never_fails_the_save(self):
        with mock.patch.object(ledger, "idle_streams", side_effect=RuntimeError("a run.json is half written")):
            code, out = self.save()
        self.assertEqual((code, out["heartbeat"], out["idle"]), (0, "stamped", {"errors": ["could not be read: a run.json is half written"]}))

    def test_the_answer_names_what_still_waits_on_a_clock_a_reading_or_another_run(self):
        # The help-desk catalog program, 2026-11-10: an item was to be built only if a Renovate queue had not drained
        # in a day, a run header held three items until another run closed, and every stream stood idle four hours. A
        # save lists each such run, item and open gate, so the manager tests them without searching.
        self.assertEqual(self.save()[1]["clocks"], {"runs": [], "items": [], "gates": []})
        self.file_a.write_text(self.file_a.read_text().replace("Human-only preamble.", "Held until run `hd-lambda-lock` closes."))
        (items.item_dir(self.run_dir, "A-2") / "item.md").write_text(
            "## A-2 — If the Renovate queue does not drain in a day, raise its limits\n\n**Scope.** s\n\n**Accept.**\n1. x.\n"
        )
        (self.task / "LEDGER.md").write_text(
            "# Ledger\n\n## Now\n\nx\n\n## Gates\n\n| Gate | Before | Result | When | How it was checked |\n|---|---|---|---|---|\n"
            "| G-QUEUE | A-2 | open | | The drain reading, 24 hours after the first run. |\n"
            "| G-POOL | A-3 | **passed by the operator** | | Next read 11-11. |\n"
        )
        code, out = self.save("clocks")
        self.assertEqual(code, 0)
        clocks = out["clocks"]
        self.assertEqual([(r["run"], r["file"], r["kind"]) for r in clocks["runs"]], [("run-1", "A-ITEMS.md", "edge")])
        self.assertEqual([(i["item"], i["kind"]) for i in clocks["items"]], [("A-2", "wait")])
        self.assertEqual(clocks["gates"], [{"gate": "G-QUEUE", "result": "open", "kind": "wait", "says": "The drain reading, 24 hours after the first run."}])
        # A merged item, and a run with nothing left, wait on nothing.
        for item_id in ("A-1", "A-2", "B-1"):
            item = items.load_item(self.run_dir, item_id)
            item["status"] = "merged"
            items.save_item(self.run_dir, item)
        self.assertEqual(self.save()[1]["clocks"]["runs"], [])

    def test_a_clocks_read_that_breaks_is_said_and_never_fails_the_save(self):
        with mock.patch.object(flow.clocks, "program_clocks", side_effect=RuntimeError("a run.json is half written")):
            code, out = self.save()
        self.assertEqual((code, out["heartbeat"], out["clocks"]), (0, "stamped", {"errors": ["could not be read: a run.json is half written"]}))

    def test_the_manager_is_told_how_full_its_context_is_and_when_to_hand_over(self):
        session = "11111111-2222-3333-4444-555555555555"
        write_json(self.root / "watchdog.json", {"manager": {"session": session}})
        project = self.home / ".claude" / "projects" / "-root"
        project.mkdir(parents=True)
        usage = lambda n: json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 12, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": n, "output_tokens": 900}}})
        (project / f"{session}.jsonl").write_text(usage(90_000) + "\n" + json.dumps({"type": "user", "message": {"content": "x"}}) + "\n" + usage(180_000) + "\n")
        self.assertEqual(flow.context_tokens(self.root), 183_012)
        out = self.save()[1]
        self.assertEqual(out["context_tokens"], 183_012)
        self.assertNotIn("hand_over", out)
        self.settings(handover_tokens=200_000)
        self.assertEqual((self.save()[1]["hand_over"], self.save()[1]["hand_over_at"]), (False, 200_000))
        self.settings(handover_tokens=150_000)
        self.assertTrue(self.save()[1]["hand_over"])

    def test_with_no_transcript_to_read_the_context_is_not_guessed(self):
        self.settings(handover_tokens=150_000)
        out = self.save()[1]
        self.assertEqual(out["context_tokens"], None)
        self.assertNotIn("hand_over", out)

    def test_the_answer_names_each_backlog_file_no_run_carries_and_the_ledger_does_not_settle(self):
        # The help-desk catalog program: `hd-nav` (decided in its own Decisions row), then `hd-catalog-design` and
        # `hd-request-view`, were decided and never initialised; the second pair was found by the operator eight days
        # later.
        for name in ("C", "D", "E", "F", "G"):
            (self.task / f"{name}-ITEMS.md").write_text(BACKLOG_ONE.replace("A-1", f"{name}-1"))
        items.init_run(self.root / "runs" / "run-f", "run-f", "owner/repo", [str(self.task / "F-ITEMS.md")])
        # A run set aside by hand carries nothing.
        items.init_run(self.root / "runs" / ".void-run-g", "run-g", "owner/repo", [str(self.task / "G-ITEMS.md")])
        (self.task / "LEDGER.md").write_text("# Ledger\n\n## Decisions\n\n| 7 | D | Not wanted: dropped: D-ITEMS.md (Decision 6). | | | decided |\n")
        # In the archive an end counts and a hold does not: a hold is looked at again once its row has moved. The file
        # named after `superseded:` is the one it ends, not the one that carries its work.
        (self.task / archive.ARCHIVE).write_text(
            "# Ledger archive\n\n## Decisions\n\n| 3 | E | superseded: `E-ITEMS.md` by `C-ITEMS.md`. | | | decided |\n"
            "| 4 | C | held: C-ITEMS.md until G-X passes. | | | decided |\n"
        )
        out = self.save()[1]["unstarted_files"]
        self.assertEqual(out, {"item_files": 5, "files": ["C-ITEMS.md", "G-ITEMS.md"]})
        # The generated table of items is no backlog file.
        self.assertTrue((self.task / "LEDGER-ITEMS.md").exists())
        (self.task / "LEDGER.md").write_text("# Ledger\n\n## Decisions\n\n| 8 | C | held: C-ITEMS.md until the gate. | | | decided |\n")
        self.assertEqual(self.save()[1]["unstarted_files"]["files"], ["D-ITEMS.md", "G-ITEMS.md"])

    def test_an_unstarted_files_read_that_breaks_is_said_and_never_fails_the_save(self):
        with mock.patch.object(ledger, "unstarted_files", side_effect=RuntimeError("a run.json is half written")):
            code, out = self.save()
        self.assertEqual((code, out["heartbeat"], out["unstarted_files"]), (0, "stamped", {"errors": ["could not be read: a run.json is half written"]}))

    def test_a_run_set_aside_by_hand_is_no_run(self):
        # The help-desk catalog program (2026-11-11): two `runs/.void-*` directories were counted as waiting in `idle`.
        items.init_run(self.root / "runs" / ".void-run-2", "run-2", "owner/repo", [str(self.file_b)])
        self.assertEqual([p.name for p in ledger.run_dirs(self.root)], ["run-1"])
        self.assertNotIn("runs_outside_streams", self.save()[1]["idle"])

    def test_the_archive_fact_is_said_only_when_the_program_sets_a_size_and_is_due_at_it(self):
        self.assertNotIn("archive", self.save()[1])
        (self.task / "LEDGER.md").write_text(archive_ledger_text(range(1, 81), filler="x" * 900))
        self.settings(ledger_archive_kb=128)
        fact = self.save()[1]["archive"]
        self.assertEqual((fact["at_kb"], fact["due"]), (128, False))
        self.assertGreater(fact["movable_kb"], 30)
        self.settings(ledger_archive_kb=30)
        self.assertTrue(self.save()[1]["archive"]["due"])
        (self.task / archive.ARCHIVED_REPORTS).write_text("not json")
        out = self.save()[1]
        self.assertTrue(out["archive"]["errors"])
        self.assertEqual(out["heartbeat"], "stamped")
        # Said where the program sets no size too: FOLLOW-UPS.md is written whole because of it.
        self.settings(ledger_archive_kb=None)
        self.assertEqual(self.save()[1]["archive"], {"errors": [mock.ANY]})


BACKLOG_ONE = "## A-1 — One item\n\n**Scope.** s\n\n**Accept.**\n1. x.\n\n**Verify.** `true`\n"


def archive_ledger_text(decisions=range(1, 51), filler="", extra_rows=""):
    """A ledger as managers write one: prose sections, a Gates table read by its last rows, and numbered Decisions."""
    rows = "".join(f"| {n} | HD-{n} | Question {n}. {filler} | Recommendation. | — | decided |\n" for n in decisions)
    return (
        "# Ledger: canary\n\nKeep it short.\n\n## Now\n\nWorking on A-1.\n\n## Placements\n\n- Manager: Opus.\n\n"
        "## Gates\n\nConditions between files.\n\n| Gate | Before | Result | When | How it was checked |\n|---|---|---|---|---|\n"
        "| G-DONE | A | passed | 11-01 | Read. |\n"
        "| G-READINGS | B | not passed | 11-01 | Next read 11-02. |\n"
        "| G-OPEN | C | open | | Waits on the tenant. |\n"
        "| G-READINGS | B | **passed** | 11-02 | Read again. |\n"
        "| G-REOPENED | D | passed | 11-01 | |\n"
        "| G-REOPENED | D | not passed | 11-03 | Regressed. |\n"
        "\n## Decisions\n\nOne row per decision.\n\n| # | Item | Question | Recommendation | Asked | State |\n|---|---|---|---|---|---|\n"
        + rows
        + extra_rows
        + "\n## Lessons\n\n- 2026-11-10: a lesson, newest first.\n\n- An older lesson.\n"
    )


class ArchiveTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.ledger_file = self.task / "LEDGER.md"
        self.archive_file = self.task / archive.ARCHIVE

    def archive(self, dry_run=False, pid=None, out=None, swept="sweep"):
        """A dry run as asked. A real run is given a sweep as the manager's is: the file a dry run wrote just before."""
        if dry_run:
            return flow.archive_ledger(self.root, pid or self.PID, self.procs, True, out)
        if swept == "sweep":
            swept = self.tmp / "sweep-before.md"
            flow.archive_ledger(self.root, None, self.procs, True, swept)
        return flow.archive_ledger(self.root, pid or self.PID, self.procs, False, None, swept)

    def rows(self, path, name):
        return archive._section_rows(path.read_text().split("\n"), name) if path.exists() else []

    def numbers(self, path):
        return [int(archive.DECISION_ROW.match(r).group(1)) for r in self.rows(path, "Decisions")]

    def test_a_dry_run_says_what_would_move_and_writes_nothing(self):
        self.ledger_file.write_text(archive_ledger_text())
        before = self.ledger_file.read_text()
        code, out = self.archive(dry_run=True, out=self.tmp / "sweep.md")
        self.assertEqual(code, 0)
        self.assertEqual(out["result"], "would-move")
        self.assertEqual(out["decisions"], {"rows": 10, "numbers": "1-10", "kept": 40})
        self.assertEqual(out["gates"], {"rows": 3, "gates": ["G-DONE", "G-READINGS"]})
        self.assertEqual(self.ledger_file.read_text(), before)
        self.assertFalse(self.archive_file.exists())
        # What would move is in the sweep file whole, for the sub-agent that reads it before it goes.
        sweep = (self.tmp / "sweep.md").read_text()
        self.assertIn("| 10 | HD-10 |", sweep)
        self.assertNotIn("| 11 | HD-11 |", sweep)
        self.assertIn("| G-READINGS | B | not passed |", sweep)
        self.assertNotIn("G-OPEN", sweep)

    def test_settled_history_moves_and_everything_else_stays_where_it_was(self):
        # Decisions row 3 sits out of order, as a manager may leave one; the archive keeps number order.
        text = archive_ledger_text([1, 2] + list(range(4, 51)), extra_rows="| 3 | HD-3 | Late row. | | | done |\n")
        self.ledger_file.write_text(text)
        before = archive._counted(text.split("\n"), None)
        code, out = self.archive()
        self.assertEqual((code, out["result"]), (0, "archived"))
        self.assertEqual(self.numbers(self.archive_file), list(range(1, 11)))
        self.assertEqual(self.numbers(self.ledger_file), list(range(11, 51)))
        gates_left = [r.split("|")[1].strip() for r in self.rows(self.ledger_file, "Gates")]
        self.assertEqual(gates_left, ["G-OPEN", "G-REOPENED", "G-REOPENED"])
        self.assertEqual([r.split("|")[1].strip() for r in self.rows(self.archive_file, "Gates")], ["G-DONE", "G-READINGS", "G-READINGS"])
        # Not one row lost or doubled, and the prose sections untouched.
        self.assertEqual(archive._counted(self.ledger_file.read_text().split("\n"), self.archive_file.read_text().split("\n")), before)
        after = self.ledger_file.read_text()
        for kept in ("## Now\n\nWorking on A-1.\n", "## Placements\n\n- Manager: Opus.\n", "- 2026-11-10: a lesson, newest first.\n\n- An older lesson.\n", "One row per decision.\n"):
            self.assertIn(kept, after)
        self.assertIn("Older rows: `LEDGER-ARCHIVE.md` holds Decisions 1-10,", after)
        self.assertIn("Older rows: `LEDGER-ARCHIVE.md` holds 2 settled gates,", after)
        # The ledger's own tables still read: the pointer stands apart from each table.
        self.assertIn("(last at ", after)
        self.assertIn("LEDGER-ARCHIVE.md`.\n\n| # | Item |", after)
        self.assertIn("| Gate | Before | Result | When | How it was checked |\n|---|---|---|---|---|\n| G-OPEN |", after)
        self.assertTrue(self.archive_file.read_text().startswith("# Ledger archive: canary\n"))

    def test_a_second_archive_merges_and_points_once(self):
        self.ledger_file.write_text(archive_ledger_text())
        self.archive()
        self.assertEqual(self.archive()[1]["result"], "nothing-to-move")
        text = self.ledger_file.read_text()
        more = "".join(f"| {n} | HD-{n} | Question {n}. | | | decided |\n" for n in range(51, 61))
        text = text.replace("\n## Lessons", more + "\n## Lessons").replace("| G-OPEN | C | open |", "| G-OPEN | C | dropped |")
        self.ledger_file.write_text(text)
        self.assertEqual(self.archive()[1]["decisions"]["numbers"], "11-20")
        self.assertEqual(self.numbers(self.archive_file), list(range(1, 21)))
        self.assertEqual(self.numbers(self.ledger_file), list(range(21, 61)))
        after = self.ledger_file.read_text()
        self.assertEqual(after.count(archive.POINTER), 2)
        self.assertIn("holds Decisions 1-20,", after)
        self.assertIn("holds 3 settled gates,", after)
        self.assertEqual(self.archive_file.read_text().count("## Decisions"), 1)

    def test_followups_of_finished_items_move_and_stay_out_of_every_later_save(self):
        self.ledger_file.write_text("# Ledger\n\n## Now\n\nx\n")
        self.write("A-1", "build-1.md", SERVE + "\n## Proposed items\n\n- A-1 still at work found this.\n")
        self.write("A-2", "verdict-1.json", json.dumps({"findings": [{"severity": "low", "summary": "A-2 left the footer unaligned."}]}))
        self.status("A-2", "merged")
        out = self.archive()[1]
        self.assertEqual((out["result"], out["followups"]), ("archived", {"reports": 1, "rows": 1}))
        # Only follow-ups moved: the ledger is as it was and no ledger archive is made.
        self.assertEqual(self.ledger_file.read_text(), "# Ledger\n\n## Now\n\nx\n")
        self.assertFalse(self.archive_file.exists())
        self.assertEqual(list(read_json(self.task / archive.ARCHIVED_REPORTS)["reports"]), ["runs/run-1/items/A-2/verdict-1.json"])
        self.save()
        live, gone = (self.task / archive.FOLLOWUPS).read_text(), (self.task / archive.FOLLOWUPS_ARCHIVE).read_text()
        self.assertIn("A-1 still at work found this.", live)
        self.assertNotIn("footer", live)
        self.assertIn("A-2 left the footer unaligned.", gone)
        self.assertTrue(gone.startswith("# Follow-ups archive: canary\n"))

    def test_only_what_the_sweep_read_moves(self):
        self.ledger_file.write_text(archive_ledger_text())
        sweep = self.tmp / "sweep.md"
        self.archive(dry_run=True, out=sweep)
        # While the sub-agent reads: five Decisions push 11-15 past the newest 40, row 3 is edited, G-OPEN is
        # dropped, and an item finishes, which frees its follow-ups. None of that was read, so none of it moves yet.
        more = "".join(f"| {n} | HD-{n} | Question {n}. | | | decided |\n" for n in range(51, 56))
        text = self.ledger_file.read_text().replace("\n## Lessons", more + "\n## Lessons")
        text = text.replace("| G-OPEN | C | open |", "| G-OPEN | C | dropped |").replace("| 3 | HD-3 | Question 3. ", "| 3 | HD-3 | Question 3, answered. ")
        self.ledger_file.write_text(text)
        self.write("A-2", "verdict-1.json", json.dumps({"findings": [{"severity": "low", "summary": "A-2 left the footer unaligned."}]}))
        self.status("A-2", "merged")
        code, out = self.archive(swept=sweep)
        self.assertEqual((code, out["result"]), (0, "archived"))
        self.assertEqual(out["decisions"]["numbers"], "1-2, 4-10")
        self.assertEqual(out["gates"]["gates"], ["G-DONE", "G-READINGS"])
        self.assertEqual(out["followups"]["rows"], 0)
        self.assertEqual(out["unswept"], {"decisions": 6, "gates": 1, "followup_rows": 1})
        self.assertIn("| 3 | HD-3 | Question 3, answered.", self.ledger_file.read_text())
        self.save()  # the save after an archive writes FOLLOW-UPS.md, as at every save
        self.assertIn("A-2 left the footer unaligned.", (self.task / archive.FOLLOWUPS).read_text())
        # The next sweep reads them, and then they go.
        out = self.archive()[1]
        self.assertEqual((out["decisions"]["numbers"], out["gates"]["gates"], out["followups"]["rows"]), ("3, 11-15", ["G-OPEN"], 1))
        self.assertEqual(out["unswept"], {"decisions": 0, "gates": 0, "followup_rows": 0})
        self.assertEqual(self.numbers(self.archive_file), list(range(1, 16)))

    def test_a_move_needs_a_sweep_file_and_takes_no_other_file(self):
        self.ledger_file.write_text(archive_ledger_text())
        before = self.ledger_file.read_text()
        with self.assertRaises(Refusal) as caught:
            self.archive(swept=None)
        self.assertIn("--dry-run --out", str(caught.exception))
        other = self.tmp / "notes.md"
        other.write_text(before)  # every row of the ledger, but not a sweep
        with self.assertRaises(Refusal) as caught:
            self.archive(swept=other)
        self.assertIn("is not a file", str(caught.exception))
        self.assertEqual(self.ledger_file.read_text(), before)
        self.assertFalse(self.archive_file.exists())

    def test_a_ledger_written_meanwhile_is_not_written_over(self):
        # A save or a sub-agent's edit lands between the archive's read of the ledger and its write.
        self.ledger_file.write_text(archive_ledger_text())
        sweep = self.tmp / "sweep.md"
        self.archive(dry_run=True, out=sweep)
        real = archive._atomic_write

        def edited_first(path, text):
            if path.name == archive.ARCHIVE and not self.ledger_file.read_text().startswith("Edited"):
                self.ledger_file.write_text("Edited meanwhile.\n" + self.ledger_file.read_text())
            real(path, text)

        with mock.patch.object(archive, "_atomic_write", side_effect=edited_first), self.assertRaises(Refusal) as caught:
            self.archive(swept=sweep)
        self.assertIn("the ledger changed", str(caught.exception))
        self.assertTrue(self.ledger_file.read_text().startswith("Edited meanwhile.\n"))
        self.assertEqual(self.numbers(self.ledger_file), list(range(1, 51)))
        self.assertFalse(self.archive_file.exists())
        # Run again, the move is worked out from the ledger as it reads now, and what was written meanwhile stays.
        self.assertEqual(self.archive(swept=sweep)[1]["result"], "archived")
        self.assertTrue(self.ledger_file.read_text().startswith("Edited meanwhile.\n"))
        self.assertEqual(self.numbers(self.ledger_file), list(range(11, 51)))

    def test_a_ledger_that_would_change_beyond_the_rows_that_move_is_refused(self):
        self.ledger_file.write_text(archive_ledger_text())
        before = self.ledger_file.read_text()
        real = archive._new_ledger

        def loses_a_lesson(*args):
            return [line for line in real(*args) if not line.startswith("- An older lesson")]

        with mock.patch.object(archive, "_new_ledger", side_effect=loses_a_lesson), self.assertRaises(Refusal) as caught:
            self.archive()
        self.assertIn("beyond the rows that move", str(caught.exception))
        self.assertEqual(self.ledger_file.read_text(), before)
        self.assertFalse(self.archive_file.exists())

    def test_a_row_broken_over_two_lines_stays_whole_and_is_named(self):
        text = archive_ledger_text().replace("| 5 | HD-5 | Question 5.  |", "| 5 | HD-5 | Question 5,\nwhich runs on. |")
        # A second half may start with `#`, and is no heading for it.
        text = text.replace("| 7 | HD-7 | Question 7.  |", "| 7 | HD-7 | Question 7, fixed by\n#41 in the end. |")
        text = text.replace("| G-DONE | A | passed | 11-01 | Read. |", "| G-DONE | A | passed | 11-01 | Read,\nover two lines. |")
        # A comment, or a row with no number, after a row is no second half of it.
        text = text.replace("| 3 | HD-3 | Question 3.  | Recommendation. | — | decided |\n", "| 3 | HD-3 | Question 3.  | Recommendation. | — | decided |\n<!-- a note -->\n")
        text = text.replace("| 8 | HD-8 | Question 8.  | Recommendation. | — | decided |\n", "| 8 | HD-8 | Question 8.  | Recommendation. | — | decided |\n| — | Unnumbered. | | | | |\n")
        # A row with an empty first cell, written under the row it adds to, is that row's second half.
        text = text.replace("| 9 | HD-9 | Question 9.  | Recommendation. | — | decided |\n", "| 9 | HD-9 | Question 9.  | Recommendation. | — | decided |\n| | | Answered 11-03. | | | done |\n")
        self.ledger_file.write_text(text)
        before = archive._counted(text.split("\n"), None)
        out = self.archive()[1]
        self.assertEqual(out["broken"], {"decisions": [5, 7, 9], "gates": ["G-DONE"], "unclear_gates": []})
        self.assertEqual((out["decisions"]["numbers"], out["gates"]["gates"]), ("1-4, 6, 8, 10", ["G-READINGS"]))
        after = self.ledger_file.read_text()
        self.assertIn("| 5 | HD-5 | Question 5,\nwhich runs on. |", after)
        self.assertIn("| 7 | HD-7 | Question 7, fixed by\n#41 in the end. |", after)
        self.assertIn("| G-DONE | A | passed | 11-01 | Read,\nover two lines. |", after)
        self.assertEqual(archive._counted(after.split("\n"), self.archive_file.read_text().split("\n")), before)

    def test_a_report_written_again_is_read_again_before_it_moves(self):
        self.ledger_file.write_text("# Ledger\n\n## Now\n\nx\n")

        def verdict(*found):
            return json.dumps({"findings": [{"severity": "low", "summary": f} for f in found]})

        self.write("A-2", "verdict-1.json", verdict("F1: the footer is unaligned."))
        self.status("A-2", "parked")
        sweep = self.tmp / "sweep.md"
        self.archive(dry_run=True, out=sweep)
        # A reviewer still at work after the park writes the verdict again, with a finding the sweep never read.
        self.write("A-2", "verdict-1.json", verdict("F1: the footer is unaligned.", "F2: the header overlaps."))
        out = self.archive(swept=sweep)[1]
        self.assertEqual((out["result"], out["unswept"]["followup_rows"]), ("nothing-to-move", 2))
        self.assertEqual(self.archive()[1]["followups"], {"reports": 1, "rows": 2})
        self.save()
        self.assertNotIn("F2", (self.task / archive.FOLLOWUPS).read_text())
        # Written again after it moved (a review given back and done again under the same number): listed again.
        self.write("A-2", "verdict-1.json", verdict("F3: the badge is the wrong colour."))
        self.save()
        self.assertIn("F3", (self.task / archive.FOLLOWUPS).read_text())
        self.assertEqual(self.archive()[1]["followups"], {"reports": 1, "rows": 1})
        self.save()
        self.assertNotIn("F3", (self.task / archive.FOLLOWUPS).read_text())
        self.assertIn("F3", (self.task / archive.FOLLOWUPS_ARCHIVE).read_text())

    def test_a_write_that_fails_at_the_last_file_puts_every_file_back_byte_for_byte(self):
        # A ledger with Windows line endings is put back with them.
        self.ledger_file.write_bytes(archive_ledger_text().replace("\n", "\r\n").encode())
        self.write("A-2", "verdict-1.json", json.dumps({"findings": [{"severity": "low", "summary": "A-2 left the footer unaligned."}]}))
        self.status("A-2", "merged")
        self.save()
        before = {name: (self.task / name).read_bytes() for name in ("LEDGER.md", archive.FOLLOWUPS)}
        real = archive._atomic_write
        order = []

        def fails_at_the_last(path, data):
            order.append(path.name)
            if path.name == archive.FOLLOWUPS:
                raise OSError("disk full")
            real(path, data)

        with mock.patch.object(archive, "_atomic_write", side_effect=fails_at_the_last), self.assertRaises(Refusal) as caught:
            self.archive()
        self.assertIn("nothing was archived: disk full", str(caught.exception))
        # Each file that gains rows is written before the one that loses them.
        self.assertEqual(order[:5], [archive.ARCHIVE, "LEDGER.md", archive.FOLLOWUPS_ARCHIVE, archive.ARCHIVED_REPORTS, archive.FOLLOWUPS])
        for name, data in before.items():
            self.assertEqual((self.task / name).read_bytes(), data, name)
        for name in (archive.ARCHIVE, archive.ARCHIVED_REPORTS, archive.FOLLOWUPS_ARCHIVE):
            self.assertFalse((self.task / name).exists(), name)

    def test_an_undo_that_fails_leaves_every_row_in_one_file_or_both(self):
        # A full disk: FOLLOW-UPS-ARCHIVE.md cannot be written, nor can the ledger be put back. Removing the new
        # archive would then leave its rows in neither file (507 of 552 on a copy of the help-desk catalog
        # program's).
        self.ledger_file.write_text(archive_ledger_text())
        self.write("A-2", "verdict-1.json", json.dumps({"findings": [{"severity": "low", "summary": "A-2 left the footer unaligned."}]}))
        self.status("A-2", "merged")
        before_rows = archive._counted(self.ledger_file.read_text().split("\n"), None)
        before = self.ledger_file.read_bytes()
        real = archive._atomic_write

        def disk_full(path, data):
            if path.name == archive.FOLLOWUPS_ARCHIVE or (path.name == "LEDGER.md" and data == before):
                raise OSError(28, "No space left on device")
            real(path, data)

        with mock.patch.object(archive, "_atomic_write", side_effect=disk_full), self.assertRaises(Refusal) as caught:
            self.archive()
        said = str(caught.exception)
        self.assertIn("could not be undone whole", said)
        self.assertIn(f"left as they are: {archive.ARCHIVE}, LEDGER.md", said)
        self.assertEqual(archive._counted(self.ledger_file.read_text().split("\n"), self.archive_file.read_text().split("\n")), before_rows)

    def test_an_undo_never_writes_over_what_another_writer_wrote(self):
        # A save or a sub-agent writes the ledger just after the archive has: putting the ledger back would lose it.
        self.ledger_file.write_text(archive_ledger_text())
        before = self.ledger_file.read_bytes()
        real = archive._atomic_write

        def written_meanwhile(path, data):
            real(path, data)
            if path.name == "LEDGER.md" and data != before:
                self.ledger_file.write_text(self.ledger_file.read_text() + "Written meanwhile.\n")

        with mock.patch.object(archive, "_atomic_write", side_effect=written_meanwhile), self.assertRaises(Refusal) as caught:
            self.archive()
        self.assertIn("did not read back as written", str(caught.exception))
        self.assertIn("written again since, by another writer", str(caught.exception))
        self.assertTrue(self.ledger_file.read_text().endswith("Written meanwhile.\n"))
        self.assertEqual(self.numbers(self.archive_file), list(range(1, 11)))

    def test_a_save_that_writes_the_ledger_a_moment_after_the_archive_is_no_failure(self):
        self.ledger_file.write_text(archive_ledger_text())
        self.write("A-2", "verdict-1.json", json.dumps({"findings": [{"severity": "low", "summary": "A-2 left the footer unaligned."}]}))
        self.status("A-2", "merged")
        real = archive._atomic_write

        def saved_meanwhile(path, data):
            if path.name == archive.FOLLOWUPS_ARCHIVE:  # the ledger is written and read back by now
                self.ledger_file.write_text(self.ledger_file.read_text() + "Written meanwhile.\n")
            real(path, data)

        with mock.patch.object(archive, "_atomic_write", side_effect=saved_meanwhile):
            self.assertEqual(self.archive()[1]["result"], "archived")
        self.assertTrue(self.ledger_file.read_text().endswith("Written meanwhile.\n"))
        self.assertEqual(self.numbers(self.ledger_file), list(range(11, 51)))

    def test_the_ledger_keeps_its_line_endings(self):
        # Every line ending "\r\n" stays so. A lone carriage return breaks a line, as it does for the clocks fact and
        # for GitHub: row 5 is a row broken over two lines, and stays, carriage return and all.
        text = archive_ledger_text().replace("| 5 | HD-5 | Question 5. ", "| 5 | HD-5 | Question 5.\rasked again ")
        text = text.replace("| 45 | HD-45 | Question 45. ", "| 45 | HD-45 | Question 45.\rasked again ")
        self.ledger_file.write_bytes(text.replace("\n", "\r\n").encode())
        out = self.archive()[1]
        self.assertEqual((out["decisions"]["numbers"], out["broken"]["decisions"]), ("1-4, 6-10", [5]))
        after = self.ledger_file.read_bytes()
        self.assertEqual(after.count(b"\n"), after.count(b"\r\n"))
        self.assertIn(b"| 5 | HD-5 | Question 5.\rasked again ", after)
        self.assertIn(b"| 45 | HD-45 | Question 45.\rasked again ", after)
        self.assertEqual(self.numbers(self.ledger_file), [5] + list(range(11, 51)))
        # A new archive takes the ledger's line endings.
        started = (self.task / archive.ARCHIVE).read_bytes()
        self.assertEqual(started.count(b"\n"), started.count(b"\r\n"))
        # Rows written since end "\n" alone: each line keeps its own break, a pointer written again too.
        mixed = after.replace(b"\r\n## Lessons", b"".join(b"| %d | HD-%d | Question %d. | | | decided |\n" % (n, n, n) for n in range(51, 56)) + b"\r\n## Lessons")
        self.ledger_file.write_bytes(mixed)
        self.assertEqual(self.archive()[1]["decisions"]["numbers"], "11-15")
        again = self.ledger_file.read_bytes()
        self.assertEqual(re.findall(rb"Older rows[^\r\n]*(\r?\n)", again), [b"\r\n", b"\r\n"])
        self.assertIn(b"| 51 | HD-51 | Question 51. | | | decided |\n| 52 |", again)

    def test_the_followups_archive_keeps_its_line_endings(self):
        self.ledger_file.write_text("# Ledger\n\n## Now\n\nx\n")
        (self.task / archive.FOLLOWUPS_ARCHIVE).write_bytes(b"# Follow-ups archive\r\n\r\nAn older finding.\r\n")
        self.write("A-2", "verdict-1.json", json.dumps({"findings": [{"severity": "low", "summary": "A-2 left the footer unaligned."}]}))
        self.status("A-2", "merged")
        self.assertEqual(self.archive()[1]["result"], "archived")
        moved = (self.task / archive.FOLLOWUPS_ARCHIVE).read_bytes()
        self.assertEqual(moved.count(b"\n"), moved.count(b"\r\n"))
        self.assertTrue(moved.startswith(b"# Follow-ups archive\r\n\r\nAn older finding.\r\n"))
        self.assertIn(b"A-2 left the footer unaligned.", moved)

    def test_a_finding_with_a_carriage_return_moves_on_one_line(self):
        self.ledger_file.write_text("# Ledger\n\n## Now\n\nx\n")
        self.write("A-2", "verdict-1.json", json.dumps({"findings": [{"severity": "low", "summary": "F1: the footer\r\nis unaligned."}]}))
        self.status("A-2", "merged")
        self.assertEqual(self.archive()[1]["result"], "archived")
        moved = (self.task / archive.FOLLOWUPS_ARCHIVE).read_bytes()
        self.assertNotIn(b"\r", moved)
        self.assertIn(b"F1: the footer is unaligned.", moved)

    def test_followups_that_moved_stay_in_the_archive_when_their_run_is_set_aside(self):
        self.ledger_file.write_text("# Ledger\n\n## Now\n\nx\n")
        self.write("A-2", "verdict-1.json", json.dumps({"findings": [{"severity": "low", "summary": "A-2 left the footer unaligned."}]}))
        self.status("A-2", "merged")
        self.archive()
        # The run is voided by hand, and another run's follow-ups move at the next archive.
        self.run_dir.rename(self.run_dir.with_name(".void-run-1"))
        (self.task / "C-ITEMS.md").write_text(BACKLOG_ONE.replace("A-1", "C-1"))
        run_c = self.root / "runs" / "run-c"
        items.init_run(run_c, "run-c", "owner/repo", [str(self.task / "C-ITEMS.md")])
        path = items.item_dir(run_c, "C-1") / "verdict-1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"findings": [{"severity": "low", "summary": "C-1 left the badge grey."}]}))
        item = items.load_item(run_c, "C-1")
        item["status"] = "merged"
        items.save_item(run_c, item)
        self.assertEqual(self.archive()[1]["followups"], {"reports": 1, "rows": 1})
        moved = (self.task / archive.FOLLOWUPS_ARCHIVE).read_text()
        self.assertIn("A-2 left the footer unaligned.", moved)
        self.assertIn("C-1 left the badge grey.", moved)
        self.assertEqual(moved.count("\n## Archived "), 2)
        self.assertEqual(sorted(read_json(self.task / archive.ARCHIVED_REPORTS)["reports"]), ["runs/run-1/items/A-2/verdict-1.json", "runs/run-c/items/C-1/verdict-1.json"])

    def test_a_write_that_cannot_be_undone_says_so(self):
        self.ledger_file.write_text(archive_ledger_text())
        real = archive._atomic_write

        def fails_at_the_ledger(path, text):
            if path.name == "LEDGER.md":
                raise OSError("disk full")
            real(path, text)

        with mock.patch.object(archive, "_atomic_write", side_effect=fails_at_the_ledger):
            with mock.patch.object(Path, "unlink", side_effect=OSError("read-only")), self.assertRaises(Refusal) as caught:
                self.archive()
        self.assertIn("could not be undone whole", str(caught.exception))
        self.assertIn(archive.ARCHIVE, str(caught.exception))

    def test_a_failed_write_leaves_no_temporary_file(self):
        with mock.patch.object(archive.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            archive._atomic_write(self.tmp / "x.md", b"text")
        self.assertEqual(list(self.tmp.glob(".x.md.tmp-*")), [])

    def test_a_dry_run_over_an_unreadable_record_is_refused_and_no_traceback(self):
        self.ledger_file.write_text(archive_ledger_text())
        (self.task / archive.ARCHIVED_REPORTS).write_text("not json")
        with self.assertRaises(Refusal) as caught:
            self.archive(dry_run=True)
        self.assertIn("could not be worked out", str(caught.exception))

    def test_a_gate_row_that_reads_two_ways_is_never_settled(self):
        more = (
            # A pipe written `\|` splits nothing for GitHub, but split at every `|`, as the kit read gate rows before,
            # its Result is "y'`". It cannot be said.
            "| G-ESCAPED | `grep -E 'x\\|y'` | passed | 11-01 | Read. |\n"
            # Read with the code span whole, passed; split at every `|`, its Result is "y'`". It cannot be said.
            "| G-CODE | `grep -E 'x|y'` | passed | 11-01 | Read. |\n"
            # One cell short with a pipe in its code: split at every `|` it has the header's five cells, and its
            # Result is "deleted' log` reads 0", which reads as settled. The gate is open (a gate of the help-desk
            # catalog program's ledger, with such a row added): it must not move.
            "| G-SHORT | `grep -cE 'IDX|deleted' log` reads 0 | open: re-read 11-12 | 11-12 |\n"
            # A backtick left open: read with the code span whole, the rest of the row is one cell. Split at every
            # `|`, its Result is open, so the earlier passed row does not settle it, and open is all it can be.
            "| G-STRAY | A | passed | 11-01 | Read. |\n"
            "| G-STRAY | after `hd-x merges | open | 11-11 | ran `grep -c x log` |\n"
            "| G-ODD | `x|y` | passed |\n"
            # Two cells with the code span whole, so no Result; split at every `|`, its Result is "passed` in the
            # log". Only one reading has a Result, and one reading cannot settle a gate.
            "| G-HALF | check `x | passed` in the log |\n"
        )
        text = archive_ledger_text().replace("| G-OPEN | C | open |", "| G-OPEN | `grep -E 'x|done'` | open |")
        text = text.replace("| G-REOPENED | D | not passed | 11-03 | Regressed. |\n", "| G-REOPENED | D | not passed | 11-03 | Regressed. |\n" + more)
        # A legend beside the Gates table is no gate.
        text = text.replace("Conditions between files.\n", "Conditions between files.\n\n| Word | Meaning |\n|---|---|\n| passed | done and read |\n")
        self.ledger_file.write_text(text)
        out = self.archive(dry_run=True)[1]
        self.assertEqual(out["gates"]["gates"], ["G-DONE", "G-READINGS"])
        self.assertEqual(out["broken"], {"decisions": [], "gates": [], "unclear_gates": ["G-OPEN", "G-ESCAPED", "G-CODE", "G-SHORT", "G-ODD", "G-HALF"]})

    def test_a_gate_row_is_read_wherever_the_clocks_fact_reads_it(self):
        # Each case puts a row that says G-RE is open after the row that passed it. The clocks fact reads the gate
        # as open; were the archive to leave the open row out, the passed row would settle G-RE and move it.
        passed = "| G-RE | E | passed | 11-01 | Read. |"
        reopened = "| G-RE | E | open: regressed, next read 11-12 | 11-11 | Re-read. |\n"
        legend = "| Word | Meaning |\n|---|---|\n| passed | done and read |\n"
        cases = {
            "under a legend's rows": passed + "\n\n" + legend + reopened,
            "below a legend and a blank line": passed + "\n\n" + legend + "\n" + reopened,
            "under a stray rule": passed + "\n|---|---|---|---|---|\n" + reopened,
            "after a lone carriage return": passed + "\r" + reopened,
            "with a pipe at its start": passed + "\n|" + reopened,
        }
        last_gate = "| G-REOPENED | D | not passed | 11-03 | Regressed. |\n"
        for label, rows in cases.items():
            with self.subTest(label):
                self.ledger_file.write_bytes(archive_ledger_text().replace(last_gate, last_gate + rows).encode())
                out = self.archive(dry_run=True)[1]
                self.assertEqual(out["gates"]["gates"], ["G-DONE", "G-READINGS"])
                self.assertNotIn("broken", out)  # a legend's words are no gates
                # Read as the clocks fact reads it: `read_text()` would turn the lone carriage return into a newline.
                read = self.ledger_file.read_bytes().decode("utf-8")
                self.assertIn("G-RE", [g["gate"] for g in clocks.gate_rows(read)])
        # Archived for real, both of G-RE's rows stay as they were written, carriage return and all.
        self.ledger_file.write_bytes(archive_ledger_text().replace(last_gate, last_gate + cases["after a lone carriage return"]).encode())
        self.assertEqual(self.archive()[1]["gates"]["gates"], ["G-DONE", "G-READINGS"])
        self.assertIn(cases["after a lone carriage return"].encode(), self.ledger_file.read_bytes())

    def test_a_line_breaks_only_where_markdown_breaks_it(self):
        # GitHub, an editor's search and the manager read a line separator, a next-line character, a form feed, a
        # vertical tab or a record separator as part of its line; so do the archive and the clocks fact. A row
        # holding one before a `|` moves whole, and the two never read a gate differently.
        last_gate = "| G-REOPENED | D | not passed | 11-03 | Regressed. |\n"
        for char in (" ", "\x85", "\x0c", "\x0b", "\x1e"):
            with self.subTest(repr(char)):
                decision = f"| 3 | HD-3 | Question 3.{char}| see Decision 9 "
                gate = f"| G-DONE | A | passed | 11-01 | Read. |{char}| read by the manager |"
                rejoined = f"| G-RE | E | passed | 11-01 | Read. |{char}| G-RE | E | open: regressed, next read 11-12 | 11-11 | Re-read. |"
                text = archive_ledger_text().replace("| 3 | HD-3 | Question 3. ", decision).replace("| G-DONE | A | passed | 11-01 | Read. |", gate)
                self.ledger_file.write_bytes(text.replace(last_gate, last_gate + rejoined + "\n").encode())
                if self.archive_file.exists():
                    self.archive_file.unlink()
                out = self.archive()[1]
                self.assertEqual((out["decisions"]["numbers"], out["gates"]["gates"]), ("1-10", ["G-DONE", "G-READINGS", "G-RE"]))
                self.assertNotIn("broken", out)
                kept, moved = self.ledger_file.read_text(), self.archive_file.read_text()
                for line in (decision, gate, rejoined):
                    self.assertIn(line, moved)
                for half in ("see Decision 9", "read by the manager", "| G-RE |"):
                    self.assertNotIn(half, kept)
                self.assertNotIn("G-RE", [g["gate"] for g in clocks.gate_rows(text.replace(last_gate, last_gate + rejoined + "\n"))])

    def test_the_line_before_a_last_line_that_moves_keeps_its_break(self):
        # A ledger with no break at its end, whose last line moves: the line before it ends as it did.
        header = "| Gate | Before | Result | When | How it was checked |\n|---|---|---|---|---|\n"
        self.ledger_file.write_bytes(f"# Ledger\n\n## Gates\n\n{header}| G2 | HD-2 | open | 11-02 | x |\n| G1 | HD-1 | passed | 11-01 | read |".encode())
        self.assertEqual(self.archive()[1]["gates"]["gates"], ["G1"])
        self.assertTrue(self.ledger_file.read_bytes().endswith(b"| G2 | HD-2 | open | 11-02 | x |\n"))
        # An archive that ends with blank lines keeps them when rows are added to it.
        self.archive_file.write_bytes(self.archive_file.read_bytes() + b"\n\n")
        self.ledger_file.write_bytes(self.ledger_file.read_bytes() + b"| G2 | HD-2 | passed | 11-03 | read again |\n")
        self.assertEqual(self.archive()[1]["gates"]["gates"], ["G2"])
        after = self.archive_file.read_bytes()
        self.assertIn(b"| G2 | HD-2 | open | 11-02 | x |\n| G2 | HD-2 | passed | 11-03 | read again |\n", after)
        self.assertTrue(after.endswith(b"|---|---|---|---|---|---|\n\n\n"))

    def test_a_new_archive_takes_the_gates_header_and_the_pointer_goes_above_it(self):
        # A legend before the Gates table, headed `Gate` or not, gives neither the archive's header nor the pointer's place.
        for legend in ("| Word | Meaning |\n|---|---|\n| passed | done and read |\n\n", "| Gate | Meaning |\n|---|---|\n| passed | done and read |\n\n"):
            with self.subTest(legend.split("\n")[0]):
                if self.archive_file.exists():
                    self.archive_file.unlink()
                self.ledger_file.write_text(archive_ledger_text().replace("Conditions between files.\n", "Conditions between files.\n\n" + legend))
                self.archive()
                gates = self.archive_file.read_text().split("## Gates")[1]
                self.assertIn("| Gate | Before | Result | When | How it was checked |\n|---|---|---|---|---|\n| G-DONE |", gates)
                self.assertNotIn("| Meaning |", gates)
                after = self.ledger_file.read_text()
                self.assertLess(after.index("| Meaning |"), after.index(archive.POINTER))
                self.assertRegex(after, r"\nOlder rows: `LEDGER-ARCHIVE.md` holds 2 settled gates[^\n]*\n\n\| Gate \| Before \|")

    def test_a_gate_row_just_above_the_header_is_never_read_with_the_pointer(self):
        early = "| G-EARLY | HD-9 | open | 11-01 | written above the header |\n"
        header = "| Gate | Before | Result | When | How it was checked |\n"
        self.ledger_file.write_text(archive_ledger_text().replace(header, early + header))
        self.archive()
        after = self.ledger_file.read_text()
        # The pointer goes above the rows that run into the header, never between a row and the next.
        self.assertIn(early + header, after)
        self.assertRegex(after, r"\nOlder rows: [^\n]*\n\n" + re.escape(early))
        # Even where the manager puts it under a row, a pointer is no second half of that row.
        pointer = next(line for line in after.split("\n") if line.startswith(archive.POINTER))
        moved = after.replace(pointer + "\n\n", "").replace(early, early + pointer + "\n")
        last_gate = "| G-REOPENED | D | not passed | 11-03 | Regressed. |\n"
        self.ledger_file.write_text(moved.replace(last_gate, last_gate + "| G-EARLY | HD-9 | passed | 11-03 | read again |\n"))
        out = self.archive()[1]
        self.assertEqual(out["gates"]["gates"], ["G-EARLY"])
        self.assertNotIn("broken", out)

    def test_the_sweep_file_is_written_as_utf8_whatever_the_locale(self):
        # Written in a Latin-1 locale's encoding, a dry run of a ledger holding "…" failed with a traceback.
        self.ledger_file.write_bytes(archive_ledger_text(filler="… ✅").encode())
        sweep = self.tmp / "sweep-latin1.md"
        code = "import json, sys; from hl import flow; print(json.dumps(flow.archive_ledger(sys.argv[1], None, None, True, sys.argv[2])[1]['result']))"
        env = {**os.environ, "LC_ALL": "en_US.ISO8859-1", "LANG": "en_US.ISO8859-1", "PYTHONUTF8": "0"}
        run = subprocess.run(
            [sys.executable, "-X", "utf8=0", "-c", code, str(self.root), str(sweep)],
            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), "would-move")
        self.assertIn("| 1 | HD-1 | Question 1. … ✅ |", sweep.read_bytes().decode("utf-8"))
        self.assertEqual(self.archive(swept=sweep)[1]["decisions"]["numbers"], "1-10")

    def test_a_followups_archive_with_nothing_in_it_is_started_afresh(self):
        self.ledger_file.write_text("# Ledger\n\n## Now\n\nx\n")
        (self.task / archive.FOLLOWUPS_ARCHIVE).write_text("\n")
        self.write("A-2", "verdict-1.json", json.dumps({"findings": [{"severity": "low", "summary": "A-2 left the footer unaligned."}]}))
        self.status("A-2", "merged")
        self.assertEqual(self.archive()[1]["result"], "archived")
        self.assertTrue((self.task / archive.FOLLOWUPS_ARCHIVE).read_text().startswith("# Follow-ups archive"))

    def test_a_ledger_with_nothing_settled_moves_nothing(self):
        self.ledger_file.write_text("# Ledger\n\n## Now\n\nx\n\n## Lessons\n\n- y\n")
        self.assertEqual(self.archive()[1]["result"], "nothing-to-move")
        self.assertFalse(self.archive_file.exists())

    def test_only_the_manager_holding_the_program_moves_its_ledger(self):
        self.ledger_file.write_text(archive_ledger_text())
        before = self.ledger_file.read_text()
        with self.assertRaises(Refusal):
            self.archive(pid=self.PID + 1)
        self.assertEqual(self.ledger_file.read_text(), before)
        self.assertFalse(self.archive_file.exists())

    def test_a_write_that_fails_part_way_leaves_both_files_as_they_were(self):
        self.ledger_file.write_text(archive_ledger_text())
        before = self.ledger_file.read_bytes()
        real = archive._atomic_write

        def fails_at_the_ledger(path, data):
            if path.name == "LEDGER.md" and data != before:
                raise OSError("disk full")
            real(path, data)

        with mock.patch.object(archive, "_atomic_write", side_effect=fails_at_the_ledger), self.assertRaises(Refusal) as caught:
            self.archive()
        self.assertIn("disk full", str(caught.exception))
        self.assertEqual(self.ledger_file.read_bytes(), before)
        self.assertFalse(self.archive_file.exists())
        self.assertFalse((self.task / archive.ARCHIVED_REPORTS).exists())

    def test_rows_that_would_not_add_up_are_refused_before_anything_is_written(self):
        self.ledger_file.write_text(archive_ledger_text())
        before = self.ledger_file.read_text()
        with mock.patch.object(archive, "_new_archive", side_effect=lambda old, moved, lines, program: ["# Ledger archive", "", "## Decisions", ""]):
            with self.assertRaises(Refusal) as caught:
                self.archive()
        self.assertIn("would differ", str(caught.exception))
        self.assertEqual(self.ledger_file.read_text(), before)
        self.assertFalse(self.archive_file.exists())

    def test_the_command_line_runs_a_dry_run_without_a_claim_and_refuses_out_without_one(self):
        self.ledger_file.write_text(archive_ledger_text())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = flow.main(["--root", str(self.root), "archive", "--dry-run"], procs=self.procs)
        self.assertEqual((code, json.loads(out.getvalue())["result"]), (0, "would-move"))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(flow.main(["--root", str(self.root), "archive", "--out", str(self.tmp / "x.md")], procs=self.procs), 2)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            # a folder that is not there is a refusal, not a traceback
            code = flow.main(["--root", str(self.root), "archive", "--dry-run", "--out", str(self.tmp / "missing" / "x.md")], procs=self.procs)
        self.assertEqual(code, 2)
        self.assertIn("could not be written", err.getvalue())
        sweep = str(self.tmp / "sweep.md")
        for refused in (["--pid", str(self.PID)], ["--dry-run", "--swept", sweep]):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(flow.main(["--root", str(self.root), "archive", *refused], procs=self.procs), 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(flow.main(["--root", str(self.root), "archive", "--dry-run", "--out", sweep], procs=self.procs), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = flow.main(["--root", str(self.root), "archive", "--pid", str(self.PID), "--swept", sweep], procs=self.procs)
        self.assertEqual((code, json.loads(out.getvalue())["result"]), (0, "archived"))

    def test_ranges_read_as_a_person_would_write_them(self):
        self.assertEqual(archive.ranges([5, 1, 2, 3, 9, 10]), "1-3, 5, 9-10")
        self.assertEqual(archive.ranges([]), "")
        # the help-desk catalog program's ledger holds one decision twice, word for word
        self.assertEqual(archive.ranges([294, 295, 295, 296]), "294-296")


class LedgerTablesTest(FlowCase):
    def test_the_tables_are_what_the_run_directories_hold(self):
        self.policy(premise="routed", review="blind")
        self.prepare()
        items.route_item(self.run_dir, "A-1", "independent", True, "a page")
        self.start("--effort", "medium", "--why", "well specified")
        self.write("A-1", "build-1.md", SERVE + "\n## Proposed items\n\n- The mock API answers 404 for /users/me/orgs.\n- None\n")
        items.start_phase(self.run_dir, "A-1", "grade", session="rev-1", head_sha="a" * 40)
        finding = {"severity": "low", "confidence": 0.6, "summary": "The mock API answers 404 for /users/me/orgs.", "evidence": []}
        self.verdict(findings=[finding, {"kind": "design", "severity": "low", "confidence": 0.5, "summary": "The 16px favicon has no mark.", "evidence": []}])
        item = items.load_item(self.run_dir, "A-1")
        item.update(status="merged", started_at="2026-10-12T10:00:00Z")
        item["fields"].update(pr="7", verify="verified")
        items.save_item(self.run_dir, item)
        write_json(self.item_dir() / "merge.json", {"state": "MERGED", "at": "2026-10-12T10:21:30Z"})
        record = ledger.item_record(self.run_dir, "A-1")
        self.assertEqual((record["builders"], record["reviews"], record["minutes"], record["premise"], record["look"]), (["claude@medium"], ["PASS"], 21.5, "independent", True))
        text = ledger.items_text(self.root, "canary", "2026-10-12T10:30:00Z")
        self.assertIn("| run-1 | main |  | routed, blind | ready | 1 | 0 | 2 | 0 | 0 | 0 |", text)
        self.assertIn("| A-1 | run-1 | merged | independent, look | claude@medium | PASS |  | 21.5 | #7 | verified |  |", text)
        self.assertIn("| claude@medium | 1 | 1 of 1 | 1 | 21.5 | 1 |", text)
        self.assertNotIn("| A-2 |", text)
        followups = ledger.followups_text(self.root, "canary", "2026-10-12T10:30:00Z")
        # The same remark from the builder and from the reviewer is one row; "None" is not a proposal.
        self.assertEqual(followups.count("The mock API answers 404"), 1)
        self.assertIn("| proposed, finding | low | The mock API answers 404 for /users/me/orgs. | A-1 build-1, A-1 verdict-1 |", followups)
        self.assertIn("| design | low | The 16px favicon has no mark. | A-1 verdict-1 |", followups)
        self.assertNotIn("| None |", followups)


class CommandLineTest(FlowCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = flow.main(["--root", str(self.root), "--run-dir", str(self.run_dir), *argv], herdr=self.herdr, host=self.host, tools=self.tools, envs=self.envs)
        return code, json.loads(out.getvalue())

    def test_the_verbs_run_end_to_end_from_the_command_line(self):
        code, out = self.run_cli("prepare", "--id", "A-1")
        self.assertEqual((code, out["result"]), (0, "prepared"))
        self.start()
        worktree = Path(self.fields()["worktree"])
        (worktree / "feature.txt").write_text("feature\n")
        git(worktree, "add", "feature.txt")
        git(worktree, "commit", "-m", "A-1: add the feature")
        git(worktree, "push", "origin", self.BRANCH)
        self.write("A-1", "build-1.md", SERVE)
        self.write("A-1", "build-1.done", "")
        self.set_gh(git(worktree, "rev-parse", "HEAD"))
        self.assertEqual(self.run_cli("built", "--id", "A-1")[1]["result"], "built")
        self.assertEqual(self.run_cli("review", "--id", "A-1")[1]["result"], "reviewing")
        self.verdict()
        self.assertEqual(self.run_cli("reviewed", "--id", "A-1")[1]["effective"], "PASS")

    def test_relative_paths_on_the_command_line_serve_and_review_as_absolute_ones_do(self):
        # A help-desk feature program and a hardening program typed `--run-dir runs/<run>` from the loop root: the
        # serve script was then named relative to the root and run in the worktree's pane, where that path names
        # nothing.
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.root.parent)
        relative = ["--root", self.root.name, "--run-dir", str(self.run_dir.relative_to(self.root.parent))]

        def cli(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = flow.main([*relative, *argv], herdr=self.herdr, host=self.host, tools=self.tools, envs=self.envs)
            return code, json.loads(out.getvalue())

        self.assertEqual(cli("prepare", "--id", "A-1")[1]["result"], "prepared")
        self.start()
        worktree = Path(self.fields()["worktree"])
        self.assertTrue(worktree.is_absolute(), worktree)
        (worktree / "feature.txt").write_text("feature\n")
        git(worktree, "add", "feature.txt")
        git(worktree, "commit", "-m", "A-1: add the feature")
        git(worktree, "push", "origin", self.BRANCH)
        self.write("A-1", "build-1.md", SERVE)
        self.write("A-1", "build-1.done", "")
        self.set_gh(git(worktree, "rev-parse", "HEAD"))
        self.assertEqual(cli("built", "--id", "A-1")[1]["result"], "built")
        self.assertEqual(cli("review", "--id", "A-1")[1]["result"], "reviewing")
        served = [c for c in self.herdr.called("pane", "run") if "/bin/sh " in c[3]]
        script = Path(shlex.split(served[-1][3])[1])
        self.assertTrue(script.is_absolute() and script.is_file(), served[-1][3])
        self.verdict()
        self.assertEqual(cli("reviewed", "--id", "A-1")[1]["effective"], "PASS")

    def test_every_path_a_script_is_given_is_made_absolute(self):
        from hl import claim as claim_mod, secrets, serve, spawn as spawn_mod, train, verdict, verify, wait, watchdog

        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.root.parent)
        base = Path(os.getcwd())
        given = {
            flow: (["--root", "r", "--run-dir", "runs/x", "--repo-dir", "c", "save", "--note", "n"], ("root", "run_dir", "repo_dir")),
            serve: (["--root", "r", "--run-dir", "runs/x", "status", "--id", "A-1"], ("root", "run_dir")),
            verify: (["--root", "r", "--run-dir", "runs/x", "--repo-dir", "c", "--id", "A-1"], ("root", "run_dir", "repo_dir")),
            merge: (["--root", "r", "--run-dir", "runs/x", "--repo-dir", "c", "--id", "A-1"], ("root", "run_dir", "repo_dir")),
            spawn_mod: (["--root", "r", "--run-dir", "runs/x", "end-run"], ("root", "run_dir")),
            items: (["--run-dir", "runs/x", "init", "--run-id", "x", "--repo", "o/r", "--file", "f", "--root", "r"], ("run_dir", "root")),
            wait: (["--run-dir", "runs/x", "--root", "r"], ("run_dir", "root")),
            train: (["--root", "r", "--repo", "o/r", "--repo-dir", "c", "status"], ("root", "repo_dir")),
            claim_mod: (["--root", "r", "check"], ("root",)),
            watchdog: (["--root", "r", "tick"], ("root",)),
            secrets: (["--root", "r", "status"], ("root",)),
        }
        for module, (argv, names) in given.items():
            args = module._parser().parse_args(argv)
            for name in names:
                value = getattr(args, name)
                self.assertTrue(Path(value).is_absolute() and Path(value).parent in (base, base / "runs"), (module.__name__, name, value))
        self.assertEqual(items._parser().parse_args(["--run-dir", "runs/x", "set", "--id", "A-1", "--superseded-run", "runs/y"]).superseded_run, str(base / "runs" / "y"))
        # A path given absolute is kept as typed, symlinks and all.
        self.assertEqual(flow._parser().parse_args(["--root", "/tmp/r", "save", "--note", "n"]).root, "/tmp/r")
        with mock.patch.object(verdict, "check", return_value={"problems": []}) as checked, contextlib.redirect_stdout(io.StringIO()):
            verdict.main(["--item-dir", "runs/x/items/A-1", "--n", "1", "--style", "code"])
        self.assertEqual(checked.call_args[0][0], str(base / "runs" / "x" / "items" / "A-1"))

    def test_a_refusal_exits_2_with_the_reason(self):
        code, out = self.run_cli("review", "--id", "A-1")
        self.assertEqual((code, out["result"]), (2, "refused"))
        self.assertIn("has no accepted build to review", out["refused"][0])

    def test_the_loops_own_token_is_needed(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GH_TOKEN": ""}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = flow.main(["--root", str(self.root), "--run-dir", str(self.run_dir), "prepare", "--id", "A-1"])
        self.assertEqual(code, 2)
        self.assertIn("GH_TOKEN is not set", json.loads(out.getvalue())["refused"][0])
