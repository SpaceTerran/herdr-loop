"""The closing audit walks the pages a file changed (the velocity review's change 4). With `closing_audit: pages` the
manager starts a worker of the kit's, phase `walk` of the file's last merged item: it serves the commit that holds the
whole file from a checkout the kit makes, with a browser kept to what it serves on the host, in a tab of its own that
a hand-over of the manager does not end. The default leaves the prompt as it was and refuses a walk."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path
from unittest import mock

from hl import claim, herdr, items, merge, program, spawn, stage
from hl.common import Refusal, read_json, write_json

from . import test_stage
from .support import FakeEnvs, FakeProcs, SpawnCase

DEFAULT_END = "do not start another closing audit. Write every repair the audit gives as one file and one run, not a run for each.\n- Have the same sub-agent"
WALK_BRIEF = test_stage.KIT / "references" / "briefs" / "walk.md"


class ClosingAuditStageTest(test_stage.StageCase):
    def prompt(self):
        return (self.root / "MANAGER-PROMPT.md").read_text()

    def audit(self):
        return self.prompt().split("## When a file completes", 1)[1].split("\n## ", 1)[0]

    def test_by_default_the_closing_audit_reads_as_it_did_and_names_no_walk(self):
        self.stage()
        # Word for word: the placeholder leaves nothing behind, not even a space.
        self.assertIn(DEFAULT_END, self.prompt())
        self.assertNotIn("walk", self.audit())
        self.assertEqual(program.closing_audit(program.load(self.root)), "contract")

    def test_pages_ends_the_audits_first_step_with_the_walk(self):
        self.spec["settings"]["closing_audit"] = "pages"
        self.write_spec()
        self.stage()
        walk = stage.PAGE_WALK.replace("{{ROOT}}", str(self.root))
        self.assertIn("not a run for each." + walk + "\n- Have the same sub-agent", self.prompt())
        self.assertEqual(read_json(self.root / "program.json")["closing_audit"], "pages")

    def test_the_walk_is_a_worker_started_on_the_last_merge_and_the_repairs_do_not_wait_for_it(self):
        for words in (
            "`hl-spawn --root {{ROOT}} --run-dir <run> start --id <the file's last merged item> --phase walk --engine claude --note-file",
            "`hl-wait --for <ID>:walk-<n>`",
            "write the contract audit's repairs without waiting for it",
            "read `walk-<n>.md` in the item's directory, close it (`hl-spawn close`)",
            "as one more file and one more run, each repair item with `**Origin.** audit: the page walk of <file>`",
            "`velocity.origins` counts them",
            "outlives a hand-over: write it in \"Now\", and a manager that finds it there watches it again",
            "Leave it running: `end-run` refuses while it is open, so end the run once the walk is closed and placed",
            "A walk that is done with no `walk-<n>.md` did not walk (`walk-<n>.no-browser` beside it: it had no browser): close it, give it back (`hl-items --run-dir <run> void --id <ID> --phase walk --n <n>",
            "start it once more; if that one ends the same way, write in the ledger that the file's pages were not walked",
            "(when it is another item, the refusal names it)",
            "on a train, once its batch is promoted",
        ):
            self.assertIn(words, stage.PAGE_WALK)

    def test_a_value_outside_the_two_stops_the_stage(self):
        self.spec["settings"]["closing_audit"] = "live"
        self.write_spec()
        self.assertIn("settings: `closing_audit` is one of contract, pages, got 'live'", "; ".join(self.refusal()))
        self.assertFalse((self.root / "program.json").exists())


class WalkAttemptTest(SpawnCase):
    """The walk as the item's record counts it: once, after the merge, and on records made before it existed."""

    def merge(self, item_id="A-1"):
        item = items.load_item(self.run_dir, item_id)
        item["status"] = "merged"
        items.save_item(self.run_dir, item)

    def test_a_record_made_before_the_walk_counts_none(self):
        path = self.item_dir() / "attempts.json"
        old = read_json(path)
        del old["walk"]
        write_json(path, old)
        self.assertEqual(items.load_attempts(self.run_dir, "A-1")["walk"], 0)
        self.assertIsNone(items.bound_reached(self.run_dir, "A-1", "walk"))
        # The merge gate reads every bound of the run on such a record.
        self.assertEqual({phase for phase in items.bounds(items.load_run(self.run_dir))} - set(items.load_attempts(self.run_dir, "A-1")), {items.GIVE_BACK})

    def test_the_walk_runs_only_after_the_merge(self):
        with self.assertRaises(Refusal) as caught:
            items.start_phase(self.run_dir, "A-1", "walk", session="walk-1", engine="claude")
        self.assertIn("the page walk of its file runs only after the merge", str(caught.exception))
        self.merge()
        self.assertEqual(items.start_phase(self.run_dir, "A-1", "walk", session="walk-1", engine="claude")["n"], 1)

    def test_hl_items_start_counts_no_walk_since_only_hl_spawn_checks_one(self):
        self.merge()
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            items.main(["--run-dir", str(self.run_dir), "start", "--id", "A-1", "--phase", "walk"])
        self.assertEqual(items.load_attempts(self.run_dir, "A-1")["walk"], 0)

    def test_one_walk_for_an_item_and_one_that_wrote_nothing_is_given_back(self):
        self.merge()
        items.start_phase(self.run_dir, "A-1", "walk", session="walk-1", engine="claude")
        with self.assertRaises(Refusal) as caught:
            items.start_phase(self.run_dir, "A-1", "walk", session="walk-2", engine="claude")
        self.assertIn("walk bound reached for A-1: 1 of 1", str(caught.exception))
        items.void_attempt(self.run_dir, "A-1", "walk", 1, "the pane died before it wrote anything")
        self.assertEqual(items.start_phase(self.run_dir, "A-1", "walk", session="walk-2", engine="claude")["n"], 1)
        self.write("A-1", "walk-1.md", "No defect.\n\n## Not reached\n\nNone.\n")
        with self.assertRaises(Refusal) as caught:
            items.void_attempt(self.run_dir, "A-1", "walk", 1, "again")
        self.assertIn("walk-1.md exists", str(caught.exception))


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class WalkCase(SpawnCase):
    """A run whose file is complete: A-1, A-2 and B-1 merged in that order, each on the one before, B-1 last, on a
    clone with a real origin. A walk's pane has no GitHub token."""

    def setUp(self):
        super().setUp()
        origin = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        self.clone = self.root / "repos" / "main" / "repo"
        git(self.tmp, "clone", "-q", str(origin), str(self.clone))
        for name in ("user.email", "user.name"):
            git(self.clone, "config", name, "walk@example.invalid")
        (self.clone / "page.html").write_text("<h1>A page</h1>\n")
        git(self.clone, "add", "page.html")
        git(self.clone, "commit", "-q", "-m", "the file's pages")
        self.commits = {}
        for item_id in ("A-1", "A-2", "B-1"):
            self.commits[item_id] = self.commit(item_id)
        git(self.clone, "push", "-q", "origin", "HEAD:main")
        self.head = self.commits["B-1"]
        cfg = read_json(self.root / "program.json")
        cfg.update(
            closing_audit="pages",
            streams={"main": {"repo": "owner/repo", "clone": str(self.clone)}},
            repos={
                "owner/repo": {
                    "setup": "npm ci",
                    "serve": {"command": "npm run dev -- --port {port}", "ready": "http://127.0.0.1:{port}/"},
                    "facts": ["Every page needs the header `x-hd-host: acme.helpdesk.test`."],
                }
            },
        )
        write_json(self.root / "program.json", cfg)
        run = items.load_run(self.run_dir)
        write_json(self.run_dir / "run.json", {**run, "stream": "main"})
        for minute, item_id in enumerate(("A-1", "A-2", "B-1")):
            self.merged(item_id, f"2026-11-19T10:0{minute}:00Z", self.commits[item_id])
        self.note = self.tmp / "pages.md"
        self.note.write_text("- `/desk/tickets`: open a ticket and cancel it.\n")
        self.tools = merge.Tools(git=shutil.which("git"), gh="/usr/bin/true")
        self.envs = FakeEnvs(absent={"GH_TOKEN"})
        patcher = mock.patch.object(spawn, "_listening", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def commit(self, name, on=None):
        """A commit in the clone that adds `<name>.txt`: on its branch, or beside it on `on`, back on the branch after."""
        if on:
            git(self.clone, "checkout", "-q", "--detach", on)
        (self.clone / f"{name}.txt").write_text(f"{name}\n")
        git(self.clone, "add", f"{name}.txt")
        git(self.clone, "commit", "-q", "-m", name)
        made = git(self.clone, "rev-parse", "HEAD")
        if on:
            git(self.clone, "checkout", "-q", "main")
        return made

    def merged(self, item_id, at, sha):
        item = items.load_item(self.run_dir, item_id)
        item.update(status="merged", merged_at=at)
        items.save_item(self.run_dir, item)
        write_json(self.item_dir(item_id) / "merge.json", {"state": "MERGED", "at": at, "merge_sha": sha})

    def walk(self, *extra, item_id="B-1", note=True):
        opts = self.opts(*(["--note-file", str(self.note)] if note else []), *extra, phase="walk")
        return spawn.start(self.root, self.run_dir, item_id, "walk", "claude", self.herdr, opts, envs=self.envs, sleep=lambda s: None, tools=self.tools, procs=FakeProcs())

    def checkout(self, n=1):
        return self.item_dir("B-1") / f"walk-{n}.work" / "checkout"

    def refused(self, *extra, item_id="B-1", note=True) -> str:
        tabs = len(self.herdr.called("tab", "create"))
        with self.assertRaises(Refusal) as caught:
            self.walk(*extra, item_id=item_id, note=note)
        # Refused before anything is counted, opened or checked out.
        self.assertEqual(items.load_attempts(self.run_dir, item_id)["walk"], 0)
        self.assertEqual(len(self.herdr.called("tab", "create")), tabs)
        self.assertFalse((self.item_dir(item_id) / "walk-1.work" / "checkout").exists())
        return str(caught.exception)

    def worktrees(self):
        return [line.split(" ", 1)[1] for line in git(self.clone, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")]


class WalkStartTest(WalkCase):
    def test_the_walk_checks_out_the_files_last_merge_and_starts_an_evaluator_with_the_walks_browser(self):
        out = self.walk()
        self.assertEqual(out["watch"], "B-1:walk-1")
        self.assertTrue(out["agent"].endswith("-b1-w1"), out["agent"])
        self.assertEqual(git(self.checkout(), "rev-parse", "HEAD"), self.head)
        self.assertIn(str(self.checkout().resolve()), [str(Path(p).resolve()) for p in self.worktrees()])
        argv = self.agent_argv()
        self.assertEqual(argv[argv.index("--tools") + 1], spawn.TOOLS["evaluator"])
        config = Path(argv[argv.index("--mcp-config") + 1])
        self.assertEqual(config, self.item_dir("B-1") / "walk-1.mcp.json")
        evaluators = read_json(test_stage.KIT / "references" / "mcp-playwright.json")["mcpServers"]["playwright"]
        walks = read_json(config)["mcpServers"]["playwright"]
        # The evaluators' own browser, and then the limit: loopback origins, and a proxy that answers nothing for the rest.
        self.assertEqual(walks["args"][: len(evaluators["args"])], evaluators["args"])
        self.assertEqual(
            walks["args"][len(evaluators["args"]) :],
            ["--allowed-origins", "http://127.0.0.1:*;http://localhost:*", "--proxy-server", "http://127.0.0.1:9", "--proxy-bypass", "<-loopback>,127.0.0.1,localhost"],
        )
        self.assertEqual({k: v for k, v in walks.items() if k != "args"}, {k: v for k, v in evaluators.items() if k != "args"})
        record = herdr.read_worker(self.item_dir("B-1"), "walk", 1)
        self.assertEqual(record["walk"], {"clone": str(self.clone), "checkout": str(self.checkout()), "commit": self.head})
        self.assertTrue(record["browser"])
        self.assertEqual(items.load_attempts(self.run_dir, "B-1")["walk"], 1)
        # Every item of the file is in what it serves.
        self.assertEqual(sorted(p.name for p in self.checkout().glob("*.txt")), ["A-1.txt", "A-2.txt", "B-1.txt"])

    def test_the_walks_pane_is_given_no_github_token(self):
        self.walk()
        self.assertIn("hl-env.sh op://Loop/worker-claude/password - ", self.herdr.called("pane", "run")[0][3])
        self.assertIn("GH_TOKEN", [name for _, name in self.envs.asked])

    def test_the_walk_starts_on_the_merge_that_holds_the_others_whatever_order_they_were_recorded_in(self):
        # A-2 merged on GitHub before B-1, and was recorded after it (a merge `hl-merge` saw late).
        self.merged("A-2", "2026-11-19T10:09:00Z", self.commits["A-2"])
        self.assertIn("the walk starts on the file's last merged item, B-1", self.refused(item_id="A-2"))
        self.walk()
        self.assertEqual(git(self.checkout(), "rev-parse", "HEAD"), self.commits["B-1"])

    def test_items_that_share_their_commit_walk_on_the_last_recorded(self):
        # A train's batch: one promoted commit for every item in it.
        self.merged("A-2", "2026-11-19T10:01:00Z", self.commits["B-1"])
        self.assertIn("the walk starts on the file's last merged item, B-1", self.refused(item_id="A-2"))
        self.merged("A-2", "2026-11-19T10:09:00Z", self.commits["B-1"])
        self.assertIn("the walk starts on the file's last merged item, A-2", self.refused(item_id="B-1"))

    def test_the_brief_carries_the_commit_the_checkout_the_repositorys_facts_and_the_pages(self):
        self.walk()
        brief = (self.item_dir("B-1") / "walk-1.brief.md").read_text()
        for words in (
            f"`{self.head}` of `owner/repo`, checked out for you at `{self.checkout()}`",
            "Setup, which makes the checkout ready: `npm ci`",
            "Serve, which serves the product on a port: `npm run dev -- --port {port}`, up once `http://127.0.0.1:{port}/` answers",
            "- Every page needs the header `x-hd-host: acme.helpdesk.test`.",
            "## A note from the manager\n\n- `/desk/tickets`: open a ticket and cancel it.",
            f"`filename` set to `{self.item_dir('B-1').resolve() / 'walk-1.work' / spawn.TEXT_FIT_RUN}`",
            f"`{self.item_dir('B-1')}/walk-1.md`",
            f"`{self.item_dir('B-1')}/walk-1.done`",
        ):
            self.assertIn(words, brief)
        self.assertNotIn("{{", brief)
        # The check the brief names, where the walk's browser may read it.
        self.assertTrue((self.item_dir("B-1") / "walk-1.work" / spawn.TEXT_FIT_RUN).is_file())

    def test_a_repository_with_no_setup_or_serve_named_says_so(self):
        cfg = read_json(self.root / "program.json")
        cfg["repos"]["owner/repo"] = {"facts": ["Two servers: the desk and the portal."]}
        write_json(self.root / "program.json", cfg)
        self.walk()
        brief = (self.item_dir("B-1") / "walk-1.brief.md").read_text()
        self.assertIn("Setup, which makes the checkout ready: none is named", brief)
        self.assertIn("Serve, which serves the product on a port: none is named: the facts below say how each server is started", brief)

    def test_a_commit_the_clone_lacks_is_fetched_on_the_loops_token(self):
        other = self.tmp / "other"
        git(self.tmp, "clone", "-q", str(self.tmp / "origin.git"), str(other))
        for name in ("user.email", "user.name"):
            git(other, "config", name, "walk@example.invalid")
        (other / "page.html").write_text("<h1>A later page</h1>\n")
        git(other, "commit", "-q", "-am", "the last merge")
        git(other, "push", "-q", "origin", "HEAD:main")
        later = git(other, "rev-parse", "HEAD")
        self.merged("B-1", "2026-11-19T10:09:00Z", later)
        with mock.patch.dict(os.environ, {"GH_TOKEN": "x"}), mock.patch.object(spawn, "pinned_tools", return_value=self.tools):
            self.walk()
        self.assertEqual(git(self.checkout(), "rev-parse", "HEAD"), later)

    def test_without_the_loops_token_a_missing_commit_is_not_fetched_and_nothing_is_counted(self):
        self.merged("B-1", "2026-11-19T10:09:00Z", "f" * 40)
        with mock.patch.dict(os.environ, {"GH_TOKEN": ""}), self.assertRaises(Refusal) as caught:
            self.walk()
        self.assertIn("GH_TOKEN is not set", str(caught.exception))
        self.assertEqual(items.load_attempts(self.run_dir, "B-1")["walk"], 0)
        self.assertEqual(self.herdr.called("tab", "create"), [])


class WalkRefusalTest(WalkCase):
    def test_a_program_whose_audit_reads_the_contracts_walks_nothing(self):
        write_json(self.root / "program.json", {**read_json(self.root / "program.json"), "closing_audit": "contract"})
        self.assertIn("`closing_audit: pages`", self.refused())

    def test_the_walk_starts_on_the_files_last_merged_item_only(self):
        self.assertIn("the walk starts on the file's last merged item, B-1", self.refused(item_id="A-2"))

    def test_a_file_with_an_item_still_open_is_not_walked(self):
        item = items.load_item(self.run_dir, "A-2")
        item["status"] = "in_progress"
        items.save_item(self.run_dir, item)
        self.assertIn("the file is not complete (A-2 still open)", self.refused())

    def test_on_a_train_the_walk_waits_for_the_promotion(self):
        with mock.patch.object(items, "on_train", return_value=True):
            self.assertIn("A-1, A-2, B-1 are merged into the train and the batch has not been promoted", self.refused())

    def test_a_merge_with_no_commit_on_record_is_not_walked(self):
        write_json(self.item_dir("B-1") / "merge.json", {"state": "MERGED", "at": "2026-11-19T10:02:00Z"})
        self.assertIn("no merge commit of B-1 is on record", self.refused())

    def test_merges_that_are_not_on_one_line_have_no_commit_that_holds_the_file(self):
        # A-2's commit is beside B-1's, not under it: no commit of the file has both.
        self.merged("A-2", "2026-11-19T10:01:00Z", self.commit("A-2-beside", on=self.commits["A-1"]))
        said = self.refused()
        self.assertIn("no merge commit of the file holds all of it: B-1 is not in A-2's", said)
        self.assertIn("the file's pages are not walked", said)

    def test_one_walk_of_a_file_at_a_time(self):
        self.walk()
        # A-2 was set aside while B-1's walk began, then taken up again and merged on top of it.
        later = self.commit("A-2-again")
        git(self.clone, "push", "-q", "origin", "HEAD:main")
        self.merged("A-2", "2026-11-19T10:09:00Z", later)
        self.assertIn("a page walk of the file is open (B-1/walk-1.worker.json): one runs at a time", self.refused(item_id="A-2"))

    def test_the_walks_own_values_are_not_taken_from_set(self):
        for name in spawn.WALK_VALUES:
            with self.subTest(name=name):
                self.assertIn(f"{name} is the kit's to fill, from the commit it checks out for the walk", self.refused("--set", f"{name}=elsewhere"))

    def test_the_pages_come_from_the_manager(self):
        self.assertIn("pass `--note-file", self.refused(note=False))

    def test_nothing_may_listen_where_the_browser_is_sent_for_other_sites(self):
        with mock.patch.object(spawn, "_listening", return_value=True):
            self.assertIn("something listens on 127.0.0.1:9", self.refused())

    def test_a_browser_configuration_of_another_shape_is_said(self):
        real = spawn.read_json
        for wrong in ({"mcpServers": {"playwright": "npx"}}, {"mcpServers": {"playwright": {"command": "npx", "args": "--headless"}}}, {}):
            with self.subTest(wrong=wrong), mock.patch.object(spawn, "read_json", side_effect=lambda path, w=wrong: w if str(path).endswith("mcp-playwright.json") else real(path)):
                self.assertIn("could not be read as a server with arguments", self.refused())

    def test_a_second_walk_of_the_item_is_past_its_bound(self):
        self.walk()
        self.write("B-1", "walk-1.md", "No defect.\n\n## Not reached\n\nNone.\n")
        spawn.close(self.run_dir, "B-1", "walk", 1, self.herdr, sleep=lambda s: None, procs=FakeProcs(), tools=self.tools)
        with self.assertRaises(Refusal) as caught:
            self.walk()
        self.assertIn("walk bound reached for B-1: 1 of 1", str(caught.exception))


class WalkCloseTest(WalkCase):
    def test_closing_the_walk_stops_what_it_left_running_and_removes_its_checkout(self):
        self.walk()
        procs = FakeProcs()
        (self.item_dir("B-1") / "walk-1.shells").write_text("4000 100\n")
        procs.add(4001, 4000, "node next dev --port 51234")
        procs.add(6100, 6100, "not the walk's")
        out = spawn.close(self.run_dir, "B-1", "walk", 1, self.herdr, sleep=lambda s: None, procs=procs, tools=self.tools)
        self.assertEqual(procs.killed, [(4001, signal.SIGTERM)])
        self.assertFalse(self.checkout().exists())
        self.assertEqual(len(self.worktrees()), 1)
        self.assertTrue(out["note"].endswith("; its checkout is removed"), out["note"])
        self.assertNotIn("warning", out)
        self.assertEqual(herdr.read_worker(self.item_dir("B-1"), "walk", 1)["close_note"], out["note"])

    def test_a_checkout_that_cannot_be_removed_is_said(self):
        self.walk()
        failing = mock.Mock(wraps=self.tools)
        failing.git.side_effect = lambda args, cwd, check=True: subprocess.CompletedProcess(args, 1 if args[:2] == ["worktree", "remove"] else 0, "", "fatal: busy")
        out = spawn.close(self.run_dir, "B-1", "walk", 1, self.herdr, sleep=lambda s: None, procs=FakeProcs(), tools=failing)
        self.assertIn("could not be removed: fatal: busy", out["warning"])
        self.assertIn("could not be removed", herdr.read_worker(self.item_dir("B-1"), "walk", 1)["close_note"])

    def test_a_close_with_no_git_to_hand_still_closes_the_walk_and_says_what_is_left(self):
        self.walk()
        with mock.patch.object(merge, "Tools", side_effect=Refusal("git and gh must both be available")):
            out = spawn.close(self.run_dir, "B-1", "walk", 1, self.herdr, sleep=lambda s: None, procs=FakeProcs())
        self.assertIn("was not removed (git and gh must both be available)", out["warning"])
        self.assertTrue(herdr.read_worker(self.item_dir("B-1"), "walk", 1)["closed_at"])

    def test_a_walk_that_does_not_come_up_leaves_no_checkout_and_gives_the_attempt_back(self):
        # Here it came up with a GitHub token, which a walk is never to have.
        self.envs = FakeEnvs()
        with self.assertRaises(Refusal) as caught:
            self.walk()
        self.assertIn("walk 1 of B-1 was not started and the attempt was given back", str(caught.exception))
        self.assertIn("has a GitHub token, which a page walk is not given", str(caught.exception))
        self.assertFalse(self.checkout().exists())
        self.assertEqual(len(self.worktrees()), 1)
        self.assertEqual(items.load_attempts(self.run_dir, "B-1")["walk"], 0)

    def test_the_runs_workspace_stays_open_while_the_walk_is_and_the_walk_is_left_running(self):
        self.walk()
        with self.assertRaises(Refusal) as caught:
            spawn.end_run(self.run_dir, self.herdr)
        self.assertIn("the file's page walk is still open (B-1/walk-1.worker.json): leave it running", str(caught.exception))
        self.assertIsNone(herdr.read_worker(self.item_dir("B-1"), "walk", 1).get("closed_at"))

    def test_closing_a_walk_from_the_command_line_with_no_git_to_hand_still_closes_it(self):
        self.walk()
        argv = ["--root", str(self.root), "--run-dir", str(self.run_dir), "close", "--id", "B-1", "--phase", "walk", "--n", "1"]
        with mock.patch.object(claim, "check_pins", side_effect=Refusal("git is gone")), mock.patch.object(spawn, "_reap", return_value={"reaped": [], "left": []}), mock.patch.object(spawn, "emit") as said:
            self.assertEqual(spawn.main(argv, herdr=self.herdr), 0)
        self.assertIn("was not removed (git is gone)", said.call_args[0][0]["warning"])
        self.assertTrue(herdr.read_worker(self.item_dir("B-1"), "walk", 1)["closed_at"])

    def test_a_walk_whose_clone_is_gone_is_closed_and_its_checkout_deleted(self):
        self.walk()
        shutil.rmtree(self.clone)
        out = spawn.close(self.run_dir, "B-1", "walk", 1, self.herdr, sleep=lambda s: None, procs=FakeProcs(), tools=self.tools)
        self.assertIn(f"its clone {self.clone} is gone, so its checkout was deleted as a directory", out["warning"])
        self.assertFalse(self.checkout().exists())
        self.assertTrue(herdr.read_worker(self.item_dir("B-1"), "walk", 1)["closed_at"])


class ListeningTest(test_stage.unittest.TestCase):
    """The check every walk test stands in for: something that accepts on the port, and nothing."""

    def test_a_port_that_accepts_is_listening_and_a_closed_one_is_not(self):
        import socket

        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            self.assertTrue(spawn._listening("127.0.0.1", port))
        self.assertFalse(spawn._listening("127.0.0.1", port))


class WalkBriefTest(test_stage.unittest.TestCase):
    def test_the_walk_checks_its_browser_first_and_keeps_to_its_own_store_and_host(self):
        text = WALK_BRIEF.read_text()
        for words in (
            "1. If `browser_navigate` is not among your tools, no page can be walked: write the names of the tools you have to `{{ITEM_DIR}}/walk-{{N}}.no-browser`, write no `walk-{{N}}.md`",
            "`page.setExtraHTTPHeaders`",
            "never rewrite requests with `page.route` and never fetch with `page.request`".capitalize(),
            "Add records, and submit forms, only where the facts show that the store is the served product's own (a memory store, or an emulator you started for this walk from the checkout)",
            "A store that anything else on this machine also uses, a shared emulator or database among them, is not",
            "a font, an image or a script a page takes from another site",
            "Your shell is not held to this",
            "Where they do not, add nothing, submit nothing, only look, and say so in your result",
            "s.bind((\"127.0.0.1\", 0))",
            "under `timeout 5400`",
            "1280×800", "1440×900", "the narrowest width it is made for",
            "Stop walking 45 minutes after the first server answered",
            "never sign in to a production system",
            "commit, push and fetch nothing",
            "## Not reached",
            "write this file once, at the end",
        ):
            self.assertIn(words, text)
        # Every phase's brief is listed.
        self.assertIn("| `walk.md` (phase `walk`", (WALK_BRIEF.parent / "README.md").read_text())


if __name__ == "__main__":
    test_stage.unittest.main()
