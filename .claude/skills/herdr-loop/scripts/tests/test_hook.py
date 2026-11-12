"""The commit-msg hook, driven by real git."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "commit-msg"


class CommitMsgHookTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            self.git("config", key, value)
        shutil.copy(HOOK, self.repo / ".git" / "hooks" / "commit-msg")
        (self.repo / "f.txt").write_text("one\n")
        self.git("add", "f.txt")

    def git(self, *args, check=True):
        proc = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
        return proc

    def commits(self):
        return self.git("rev-list", "--all", "--count").stdout.strip()

    def test_a_plain_commit_goes_through(self):
        self.git("commit", "-m", "add the file")
        self.assertEqual(self.commits(), "1")

    def test_a_trailer_added_on_the_command_line_is_refused(self):
        # The exact command a Cursor worker ran on the mini.
        proc = self.git(
            "commit", "--trailer", "Co-authored-by: Cursor <cursoragent@cursor.com>", "-m", "spike: add line", check=False
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Never pass --trailer", proc.stderr)
        self.assertEqual(self.commits(), "0")

    def test_a_generated_with_line_is_refused(self):
        proc = self.git("commit", "-m", "add the file\n\nGenerated with [Claude Code](https://claude.com)", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.commits(), "0")

    def test_a_human_co_author_goes_through(self):
        self.git("commit", "-m", "add the file\n\nCo-authored-by: Ada Lovelace <ada@example.invalid>")
        self.assertEqual(self.commits(), "1")

    def test_the_hook_and_the_merge_gate_refuse_the_same_text(self):
        from hl import merge

        for text in ("Co-authored-by: Cursor <cursoragent@cursor.com>", "Generated with [Claude Code]", "Made with Cursor"):
            self.assertTrue(merge.ATTRIBUTION_RE.search(text), text)
            message = self.repo / "m.txt"
            message.write_text(f"subject\n\n{text}\n")
            self.assertEqual(subprocess.run([str(HOOK), str(message)], capture_output=True).returncode, 1, text)


if __name__ == "__main__":
    unittest.main()


GUARD = Path(__file__).resolve().parent.parent / "hooks" / "agent-guard"


class AgentGuardTest(unittest.TestCase):
    """The PreToolUse hook on a worker's Agent tool, run as Claude Code runs
    it: the event as JSON on standard input, the decision as JSON on standard
    output. Proved against Claude Code 2.1.296 on 2026-10-12: with this hook
    a call that asked for the background and another model ran in the
    foreground on the worker's own, and a built-in type was denied."""

    def decide(self, event, *allowed, python="/usr/bin/python3"):
        import json

        text = event if isinstance(event, str) else json.dumps(event)
        proc = subprocess.run([python, str(GUARD), *allowed], input=text, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        return out

    def call(self, **tool_input):
        return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": tool_input}

    def test_an_allowed_type_runs_in_the_foreground_on_the_workers_own_model(self):
        asked = self.call(subagent_type="runner", description="run the suite", prompt="pytest -q", run_in_background=True, model="opus", effort="max", isolation="worktree", name="r1")
        out = self.decide(asked, "scout", "runner")
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertEqual(out["updatedInput"], {"subagent_type": "runner", "description": "run the suite", "prompt": "pytest -q", "run_in_background": False})

    def test_a_call_that_named_no_background_is_put_in_the_foreground_too(self):
        # With nothing said, Claude Code 2.1.296 launches the sub-agent in the background and the turn may end.
        out = self.decide(self.call(subagent_type="scout", description="find it", prompt="where is the theme"), "scout", "runner")
        self.assertIs(out["updatedInput"]["run_in_background"], False)

    def test_a_type_the_kit_does_not_define_is_denied_with_the_ones_it_does(self):
        for kind in ("general-purpose", "Explore", "Plan", "claude", None, ""):
            out = self.decide(self.call(subagent_type=kind, prompt="x"), "scout", "runner")
            self.assertEqual(out["permissionDecision"], "deny", kind)
            self.assertIn("runner, scout", out["permissionDecisionReason"])
            self.assertNotIn("updatedInput", out)

    def test_with_no_type_allowed_every_call_is_denied(self):
        self.assertEqual(self.decide(self.call(subagent_type="scout", prompt="x"))["permissionDecision"], "deny")

    def test_what_it_cannot_read_is_denied(self):
        for event in ("not json", "[]", {"tool_name": "Agent"}, {"tool_input": "scout"}):
            self.assertEqual(self.decide(event, "scout")["permissionDecision"], "deny", event)

    def test_whatever_goes_wrong_inside_the_guard_the_call_is_denied(self):
        import contextlib
        import importlib.machinery
        import importlib.util
        import io
        import json
        from unittest import mock

        loader = importlib.machinery.SourceFileLoader("agent_guard", str(GUARD))
        guard = importlib.util.module_from_spec(importlib.util.spec_from_loader("agent_guard", loader))
        loader.exec_module(guard)
        out = io.StringIO()
        with mock.patch.object(guard, "decide", side_effect=RuntimeError("boom")), mock.patch("sys.stdin", io.StringIO("{}")), contextlib.redirect_stdout(out):
            self.assertEqual(guard.main(), 0)
        decision = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("the sub-agent guard failed (RuntimeError)", decision["permissionDecisionReason"])

    def test_a_guard_that_cannot_run_blocks_the_call(self):
        # Found in review: a hook that exits with anything but 0 or 2 is a warning, and the call goes ahead.
        # The settings file therefore runs the guard as `<guard> … || exit 2`.
        proc = subprocess.run("/nonexistent/agent-guard scout || exit 2", shell=True, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        proc = subprocess.run(f"{GUARD} scout || exit 2", shell=True, input='{"tool_input": {"subagent_type": "scout"}}', capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)

    def test_it_runs_under_the_python_the_shebang_names(self):
        self.assertEqual(GUARD.read_text().splitlines()[0], "#!/usr/bin/python3")
        self.assertTrue(GUARD.stat().st_mode & 0o111)
