"""One item, start to merged, through every script's own functions in the
order the manager calls them. Herdr is a stand-in and so is `gh`; git, the
lock, the files and the counters are real. This is the test of the seams:
what one script records is what the next one reads."""

from __future__ import annotations

import json
import os
import shutil
from unittest import mock

from hl import herdr, items, merge, serve, spawn, verdict, verify, wait
from hl.common import Refusal, read_json

from .support import FakeEnvs, SpawnCase, install_fake_gh
from .test_merge import TEST_REF, git
from .test_serve import FakeHost


class WalkTest(SpawnCase):
    BRANCH = "hl/run-1/A-1"

    def setUp(self):
        # SpawnCase agrees the contract with a canned evaluator; this walk does it through hl-spawn instead.
        with mock.patch.object(SpawnCase, "agree", lambda self, *a, **k: None):
            super().setUp()
        patcher = mock.patch.dict(os.environ, {"GH_TOKEN": "loop-token-for-tests"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        self.clone = self.root / "repos" / "main" / "scratch"
        shutil.rmtree(self.clone)  # the fixture's stand-in worktree; this walk makes a real one
        git(self.tmp, "clone", str(self.origin), str(self.clone))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.clone, "config", key, value)
        (self.clone / "README.md").write_text("base\n")
        git(self.clone, "add", "README.md")
        git(self.clone, "commit", "-m", "base")
        git(self.clone, "push", "origin", "HEAD:main")
        git(self.clone, "worktree", "add", "-b", self.BRANCH, str(self.worktree), "origin/main")
        self.gh_state = self.tmp / "gh.json"
        fake_gh = install_fake_gh(self.tmp)
        self.tools = merge.Tools(gh=str(fake_gh), env={**os.environ, "HL_FAKE_GH": str(self.gh_state)}, sleep=lambda s: None)
        self.host = FakeHost()
        self.envs = FakeEnvs()

    def spawn(self, phase, engine="claude", *extra):
        opts = self.opts(*extra, phase=phase, engine=engine)
        return spawn.start(self.root, self.run_dir, "A-1", phase, engine, self.herdr, opts, envs=self.envs, sleep=lambda s: None)

    def waited(self, watch):
        opts = wait._parser().parse_args(["--run-dir", str(self.run_dir)])
        return wait.wait(self.run_dir, [wait.Watch(self.run_dir, watch)], [], self.herdr, None, opts)

    def close(self, phase, n=1):
        return spawn.close(self.run_dir, "A-1", phase, n, self.herdr, sleep=lambda s: None)

    def test_one_item_from_ready_to_merged(self):
        d = self.item_dir()
        self.assertEqual(items.next_item(self.run_dir)["next"], "A-1")

        # Agree: the Accept list is the contract, and an evaluator confirms it.
        items.contract_from_accept(self.run_dir, "A-1")
        agree = self.spawn("agree")
        self.assertIn(f"The contract under review: `{d}/contract.md`", (d / "agree-1.brief.md").read_text())
        self.write("A-1", "agree-1.md", "Every criterion holds against main.\n\nAGREED\n")
        self.write("A-1", "agree-1.done", "")
        self.assertEqual(self.waited(agree["watch"])["event"], "done")
        self.close("agree")

        # Build: a Cursor builder commits, pushes and opens the pull request.
        build = self.spawn("build", "cursor")
        (self.worktree / "feature.txt").write_text("feature\n")
        git(self.worktree, "add", "feature.txt")
        git(self.worktree, "commit", "-m", "A-1: add the feature")
        git(self.worktree, "push", "origin", self.BRANCH)
        head = git(self.worktree, "rev-parse", "HEAD")
        self.write("A-1", "build-1.md", "**Serve.** `true`\n**Ready.** COMMAND `test -e feature.txt`\n")
        self.write("A-1", "build-1.done", "")
        pr = {
            "number": 7, "url": "https://example.invalid/pull/7", "state": "OPEN", "isDraft": False,
            "title": "A-1: First item", "body": (d / "contract.md").read_text(), "headRefName": self.BRANCH,
            "headRefOid": head, "baseRefName": "main", "mergeStateStatus": "CLEAN", "autoMergeRequest": None,
            "mergeCommit": None,
        }  # fmt: skip
        checks = [{"name": "ci", "bucket": "pass", "state": "SUCCESS"}]
        self.gh_state.write_text(json.dumps({"pr": pr, "checks": checks}))
        self.assertEqual(self.waited(build["watch"])["event"], "done")
        built = merge.after_build(self.run_dir, "A-1", self.clone, self.tools)
        self.assertEqual((built["result"], built["head_sha"], built["serve"]), ("built", head, "true"))
        self.close("build")

        # CI, read by the wait.
        opts = wait._parser().parse_args(["--run-dir", str(self.run_dir)])
        ci = wait.wait(self.run_dir, [], ["A-1"], None, self.tools, opts)
        self.assertEqual((ci["event"], ci["passed"], ci["head_sha"]), ("checks", True, head))

        # Grade: nothing is served, so only the Ready command runs; then a fresh evaluator.
        code, served = serve.start(self.root, self.run_dir, "A-1", self.herdr, self.host)
        self.assertEqual((code, served["served"]), (0, False))
        grade = self.spawn("grade")
        brief = (d / "grade-1.brief.md").read_text()
        self.assertIn(f"head commit `{head}`", brief)
        self.assertIn("**Ready.** COMMAND `test -e feature.txt`", brief)
        self.assertIn("Nothing is served for this item.", brief)
        self.assertEqual(grade["cwd"], str(d / "evidence-1"))
        (d / "evidence-1" / "criterion-1.txt").write_text("exit 0\n")
        result = {
            "verdict": "PASS",
            "criteria": [
                {"n": 1, "result": "pass", "evidence": ["evidence-1/criterion-1.txt"], "note": "ran it"},
                {"n": 2, "result": "pass", "evidence": [], "note": "printed 1"},
            ],
            "findings": [],
            "how_run": "make test",
        }
        self.write("A-1", "verdict-1.json", json.dumps(result))
        self.write("A-1", "grade-1.done", "")
        self.assertEqual(self.waited(grade["watch"])["event"], "done")
        self.assertTrue(serve.stop(self.run_dir, "A-1", self.herdr, self.host)["already"])
        self.close("grade")
        checked = verdict.check(d, 1)
        self.assertEqual((checked["problems"], checked["effective"]), ([], "PASS"))

        # Merge, through the gate and the real lock.
        argv = ["--run-dir", str(self.run_dir), "--repo-dir", str(self.clone), "--id", "A-1", "--lock-ref", TEST_REF]
        code, merged = merge.merge(self.run_dir, "A-1", self.clone, self.tools, merge._parser().parse_args(argv))
        self.assertEqual((code, merged["result"], merged["attribution_on_base"]), (0, "merged", None))
        self.assertEqual(items.load_item(self.run_dir, "A-1")["status"], "merged")
        self.assertIsNone(merge.lock_remote_sha(self.tools, self.clone, TEST_REF))

        # The deploy check is the one phase left, and the next item is free to start.
        self.assertEqual(items.next_item(self.run_dir)["next"], "A-2")

        # What the walk left behind: no open worker, no tab, and every phase by its own session.
        self.assertEqual(spawn._open_workers(self.run_dir, "A-1"), [])
        self.assertEqual(self.herdr.tab_panes(), {})

        # The run's workspace is still there, because the run has not shipped: A-1's deploy check and two items are left.
        self.assertEqual(self.herdr.labels(), ["canary run-1"])
        with self.assertRaises(Refusal) as caught:
            spawn.end_run(self.run_dir, self.herdr)
        self.assertIn("A-1 is merged but its Verify is not recorded as passed", str(caught.exception))
        self.assertIn("A-2 is pending", str(caught.exception))
        argv = ["--root", str(self.root), "--run-dir", str(self.run_dir), "--repo-dir", str(self.clone), "--id", "A-1"]
        code, checked = verify.verify(self.root, self.run_dir, self.clone, "A-1", self.tools, verify._parser().parse_args(argv))
        self.assertEqual((code, checked["result"]), (0, "verified"))
        self.ship("A-2", "B-1")
        self.assertFalse(spawn.end_run(self.run_dir, self.herdr)["workspace_still_listed"])
        self.assertEqual((self.herdr.labels(), self.herdr.panes), ([], {}))
        log = items.load_attempts(self.run_dir, "A-1")["log"]
        self.assertEqual([e["phase"] for e in log], ["agree", "build", "grade", "verify"])
        sessions = [e["session"] for e in log if "session" in e]
        self.assertEqual(len(set(sessions)), 3)
        self.assertEqual(read_json(d / "build-1.worker.json")["kind"], "cursor")
        self.assertIsNone(herdr.claude_transcript("no-such-session"))
