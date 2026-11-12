"""stage.sh itself, run under zsh against local bare repositories.

GitHub is replaced by a directory: git is told to read `https://github.com/`
from it, and `gh api …/contents/…` is answered by a stand-in that reads the
same repository and notes whether it was given the loop's token. The token is
read by the kit's real `hl-read-secret`, from a `file:` reference in the
test's own home. The control repository here carries a stand-in `hl-stage`,
so what is tested is the part the shell script does before it hands over:
the token, the ledger clone, its branch, and bringing that branch up to the
one being staged."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from .support import KIT

STAGE_SH = KIT / "scripts" / "stage.sh"
READ_SECRET = KIT / "scripts" / "hl-read-secret"
ZSH = shutil.which("zsh")
REPO = "owner/control-repo"
TOKEN = "loop-token-for-tests"
TOKEN_REF = "file:~/.config/herdr-loop/gh-token"
SPEC = "programs/demo/program.spec.json"
KIT_FILE = ".claude/skills/herdr-loop/SKILL.md"
HL_STAGE = ".claude/skills/herdr-loop/scripts/hl-stage"

FAKE_GH = """#!/bin/sh
# gh api repos/<owner>/<name>/contents/<path>?ref=<branch> --jq .content
[ "$GH_TOKEN" = "$HL_TEST_TOKEN" ] && echo expected >> "$HL_TEST_HUB/gh-token-seen" || echo other >> "$HL_TEST_HUB/gh-token-seen"
url=$2
repo=${url#repos/}; repo=${repo%%/contents/*}
rest=${url#*/contents/}; path=${rest%%\\?ref=*}; ref=${rest##*\\?ref=}
git --git-dir "$HL_TEST_HUB/$repo.git" show "$ref:$path" | base64
"""

IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}  # fmt: skip


def git(cwd, *args):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env={**os.environ, **IDENTITY})
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


@unittest.skipUnless(ZSH, "stage.sh is a zsh script")
class StageShTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.hub = self.tmp / "hub"
        self.root = self.tmp / "loops" / "demo"
        self.home = self.tmp / "home"
        (self.home / ".local" / "bin").mkdir(parents=True)
        gh = self.home / ".local" / "bin" / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        # Piped in, the script has nothing beside it: the reader is the one a host keeps in ~/.local/bin.
        shutil.copy(READ_SECRET, self.home / ".local" / "bin" / "hl-read-secret")
        # The loop's token, in a file only its owner may read.
        token = self.home / ".config" / "herdr-loop" / "gh-token"
        token.parent.mkdir(parents=True)
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        (self.home / ".zshenv").write_text("")

        # The control repository, with the program's task branch.
        bare = self.hub / "owner" / "control-repo.git"
        bare.parent.mkdir(parents=True)
        git(self.tmp, "init", "--quiet", "--bare", "-b", "main", str(bare))
        self.work = self.tmp / "work"
        git(self.tmp, "clone", "--quiet", str(bare), str(self.work))
        self.put("README.md", "control repository\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "--quiet", "-m", "base")
        git(self.work, "push", "--quiet", "origin", "HEAD:main")
        git(self.work, "checkout", "--quiet", "-b", "task")
        self.put(SPEC, json.dumps({"root": str(self.root), "ledger_branch": "demo-ledger"}))
        self.put(KIT_FILE, "the kit, as the task branch has it\n")
        self.put(HL_STAGE, 'import sys\nprint("handed over:", sys.argv[1:])\n')
        git(self.work, "add", "-A")
        git(self.work, "commit", "--quiet", "-m", "the program's spec and the kit")
        git(self.work, "push", "--quiet", "origin", "task")

    def put(self, name, text):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def env(self, **changed):
        env = {
            "HOME": str(self.home),
            "PATH": os.environ["PATH"],
            "HL_TEST_HUB": str(self.hub),
            "HL_TEST_TOKEN": TOKEN,
            "HL_GH_TOKEN_REF": TOKEN_REF,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.hub}/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/",
            **IDENTITY,
            **changed,
        }
        return {k: v for k, v in env.items() if v is not None}

    def stage(self, branch, *, args=None, **env):
        """stage.sh piped into `zsh -s`, as it is run over SSH."""
        with open(STAGE_SH) as script:
            return subprocess.run([ZSH, "-s", "--", *(args if args is not None else (REPO, branch, SPEC))], stdin=script, capture_output=True, text=True, env=self.env(**env))

    def gh_saw(self) -> list[str]:
        seen = self.hub / "gh-token-seen"
        return seen.read_text().split() if seen.exists() else []

    def ledger(self, *args):
        return git(self.root / "ledger", *args)

    def write_the_ledger(self):
        """What the manager does between stagings: commits to its own file on the program's branch."""
        path = self.root / "ledger" / "programs" / "demo" / "LEDGER.md"
        path.write_text("# Ledger\n\nHL-01 merged.\n")
        self.ledger("add", "programs/demo/LEDGER.md")
        self.ledger("commit", "--quiet", "-m", "demo ledger: HL-01 merged")

    def squash_the_task_branch_into_main(self, kit_text):
        """The task branch merged the way this repository merges: as one new commit on main, sharing no history with it."""
        git(self.work, "checkout", "--quiet", "main")
        git(self.work, "checkout", "task", "--", ".")
        self.put(KIT_FILE, kit_text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "--quiet", "-m", "the program's kit (#1)")
        git(self.work, "push", "--quiet", "origin", "main")

    def test_a_first_stage_clones_the_ledger_on_the_programs_branch_and_hands_over(self):
        done = self.stage("task")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"handed over: ['--spec', '{self.root}/ledger/{SPEC}']", done.stdout)
        self.assertEqual(self.ledger("rev-parse", "--abbrev-ref", "HEAD"), "demo-ledger")
        self.assertEqual(self.ledger("status", "--porcelain"), "")
        self.assertEqual(self.ledger("remote", "get-url", "origin"), f"https://github.com/{REPO}.git")
        # gh was given the token the reference names, and nothing printed it.
        self.assertEqual(self.gh_saw(), ["expected"])
        self.assertNotIn(TOKEN, done.stdout + done.stderr)

    def test_the_arguments_and_the_token_reference_are_required(self):
        for args in ((), ("task", SPEC), (REPO, "task", SPEC, "extra")):
            done = self.stage("task", args=args)
            self.assertEqual(done.returncode, 64, args)
            self.assertIn("usage: HL_GH_TOKEN_REF=<reference> stage.sh <owner/control-repo> <branch>", done.stderr)
        done = self.stage("task", HL_GH_TOKEN_REF=None)
        self.assertEqual(done.returncode, 64)
        self.assertIn("HL_GH_TOKEN_REF must name the loop's GitHub token", done.stderr)
        done = self.stage("task", args=("control-repo", "task", SPEC))
        self.assertEqual(done.returncode, 64)
        self.assertIn("'control-repo' is not <owner>/<repository>", done.stderr)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.gh_saw(), [])

    def test_a_token_that_cannot_be_read_stops_it_with_the_readers_reason_before_anything_is_asked(self):
        (self.home / ".config" / "herdr-loop" / "gh-token").chmod(0o644)
        done = self.stage("task")
        self.assertEqual(done.returncode, 1)
        self.assertIn("gh-token: group or others may use it (mode -rw-r--r--", done.stderr)
        self.assertIn(f"stage: could not read the GitHub token ({TOKEN_REF})", done.stderr)
        self.assertNotIn(TOKEN, done.stdout + done.stderr)
        self.assertEqual(self.gh_saw(), [])
        self.assertFalse(self.root.exists())

    def test_with_no_reader_to_be_found_it_says_where_to_put_one(self):
        (self.home / ".local" / "bin" / "hl-read-secret").unlink()
        done = self.stage("task", PATH="/usr/bin:/bin")
        self.assertEqual(done.returncode, 1)
        self.assertIn("stage: no hl-read-secret to read HL_GH_TOKEN_REF with", done.stderr)
        self.assertEqual(self.gh_saw(), [])

    def test_a_reader_named_outright_is_the_one_used(self):
        (self.home / ".local" / "bin" / "hl-read-secret").unlink()
        named = self.tmp / "elsewhere" / "reader"
        named.parent.mkdir()
        named.write_text(f'#!/bin/sh\necho "$1" > {self.tmp}/asked\nprintf %s {TOKEN}\n')
        named.chmod(0o755)
        done = self.stage("task", HL_READ_SECRET=str(named))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(((self.tmp / "asked").read_text().strip(), self.gh_saw()), (TOKEN_REF, ["expected"]))

    def test_run_from_a_file_it_reads_with_the_reader_beside_it(self):
        (self.home / ".local" / "bin" / "hl-read-secret").unlink()
        scripts = self.tmp / "kit-scripts"
        scripts.mkdir()
        shutil.copy(STAGE_SH, scripts / "stage.sh")
        shutil.copy(READ_SECRET, scripts / "hl-read-secret")
        done = subprocess.run([ZSH, str(scripts / "stage.sh"), REPO, "task", SPEC], capture_output=True, text=True, env=self.env())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.gh_saw(), ["expected"])

    def test_a_root_that_is_not_an_absolute_path_is_refused(self):
        for root, said in (("loops/demo", "the spec's root must be an absolute path: 'loops/demo'"), ("", "the spec names no root")):
            self.put(SPEC, json.dumps({"root": root, "ledger_branch": "demo-ledger"}))
            git(self.work, "commit", "--quiet", "-am", f"root {root!r}")
            git(self.work, "push", "--quiet", "origin", "task")
            done = self.stage("task")
            self.assertEqual(done.returncode, 1, root)
            self.assertIn(f"stage: {said}", done.stderr)

    def test_staging_again_from_a_branch_that_moved_on_brings_the_kit_and_keeps_the_ledger(self):
        self.stage("task")
        self.write_the_ledger()
        self.put(KIT_FILE, "the kit, fixed\n")
        git(self.work, "commit", "--quiet", "-am", "a kit fix")
        git(self.work, "push", "--quiet", "origin", "task")
        done = self.stage("task")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.root / "ledger" / KIT_FILE).read_text(), "the kit, fixed\n")
        self.assertIn("HL-01 merged", (self.root / "ledger" / "programs" / "demo" / "LEDGER.md").read_text())

    def test_staging_from_main_after_the_task_branch_was_squash_merged_takes_mains_kit(self):
        # The canary's own case: staged from its branch, the branch squash-merged with one more change, staged from main.
        self.stage("task")
        self.write_the_ledger()
        self.squash_the_task_branch_into_main("the kit, as main has it after the merge\n")
        done = self.stage("main")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.root / "ledger" / KIT_FILE).read_text(), "the kit, as main has it after the merge\n")
        self.assertIn("HL-01 merged", (self.root / "ledger" / "programs" / "demo" / "LEDGER.md").read_text())
        self.assertEqual(self.ledger("status", "--porcelain"), "")
        self.assertIn("handed over", done.stdout)

    def test_a_kit_file_the_staged_branch_no_longer_has_does_not_linger(self):
        self.put(".claude/skills/herdr-loop/scripts/old-helper", "superseded\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "--quiet", "-m", "a helper")
        git(self.work, "push", "--quiet", "origin", "task")
        self.stage("task")
        self.write_the_ledger()
        (self.work / ".claude/skills/herdr-loop/scripts/old-helper").unlink()
        git(self.work, "commit", "--quiet", "-am", "the helper is gone")
        self.squash_the_task_branch_into_main("the kit, as main has it after the merge\n")
        done = self.stage("main")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse((self.root / "ledger" / ".claude/skills/herdr-loop/scripts/old-helper").exists())
        self.assertEqual(self.ledger("diff", "--stat", "origin/main", "HEAD", "--", ".claude/skills/herdr-loop"), "")
        self.assertEqual(self.ledger("log", "-1", "--format=%s"), "demo-ledger: take the kit as origin/main has it")
        self.assertEqual(self.ledger("status", "--porcelain"), "")

    def test_a_conflict_git_cannot_settle_is_undone_and_the_clone_left_at_its_last_commit(self):
        self.stage("task")
        # The program's branch changed a file that the staged branch then removed: neither side can simply win.
        (self.root / "ledger" / "README.md").write_text("control repository, as the ledger branch rewrote it\n")
        self.ledger("add", "README.md")
        self.ledger("commit", "--quiet", "-m", "demo ledger: a note in the readme")
        before = self.ledger("rev-parse", "HEAD")
        git(self.work, "rm", "--quiet", "README.md")
        git(self.work, "commit", "--quiet", "-m", "no readme")
        git(self.work, "push", "--quiet", "origin", "task")
        done = self.stage("task")
        self.assertEqual(done.returncode, 1)
        self.assertIn("stage: could not bring demo-ledger up to origin/task", done.stderr)
        self.assertEqual((self.ledger("rev-parse", "HEAD"), self.ledger("status", "--porcelain")), (before, ""))
        self.assertFalse((self.root / "ledger" / ".git" / "MERGE_HEAD").exists())
        self.assertNotIn("handed over", done.stdout)

    def test_a_merge_that_cannot_be_made_leaves_the_ledger_clone_exactly_as_it_was(self):
        self.stage("task")
        self.write_the_ledger()
        before = self.ledger("rev-parse", "HEAD")
        # The manager has an unsaved change to a file the staged branch also changed: git will not merge over it.
        (self.root / "ledger" / KIT_FILE).write_text("edited in the ledger clone and never committed\n")
        self.put(KIT_FILE, "the kit, fixed\n")
        git(self.work, "commit", "--quiet", "-am", "a kit fix")
        git(self.work, "push", "--quiet", "origin", "task")
        done = self.stage("task")
        self.assertEqual(done.returncode, 1)
        self.assertIn("stage: could not bring demo-ledger up to origin/task", done.stderr)
        self.assertNotIn("handed over", done.stdout)
        self.assertEqual(self.ledger("rev-parse", "HEAD"), before)
        self.assertEqual(self.ledger("status", "--porcelain").split(), ["M", KIT_FILE])
        self.assertFalse((self.root / "ledger" / ".git" / "MERGE_HEAD").exists())


if __name__ == "__main__":
    unittest.main()
