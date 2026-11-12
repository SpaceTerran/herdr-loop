"""hl-env.sh without a loop root, sourced in a real shell: each secret is read
with `hl-read-secret` beside it. The references are `env:` ones, so nothing
here reads a real secret store."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from .support import KIT

SCRIPT = KIT / "scripts" / "hl-env.sh"
SHELL = shutil.which("zsh") or shutil.which("bash")
# No startup files: whatever the operator's own define is not part of the test.
BARE = [SHELL, "-f"] if SHELL and SHELL.endswith("zsh") else [SHELL, "--norc", "--noprofile"]
# What the shell reports after sourcing: the exit status, and whether each variable is set. Never a value.
REPORT = 'echo "rc=$?"; echo "claude=${CLAUDE_CODE_OAUTH_TOKEN:+set} gh=${GH_TOKEN:+set} api=${ANTHROPIC_API_KEY:+set} memory=${CLAUDE_CODE_DISABLE_AUTO_MEMORY:-}"'
CLAUDE_REF = "env:HL_TEST_CLAUDE"
GH_REF = "env:HL_TEST_GH"
EMPTY_REF = "env:HL_TEST_EMPTY"
VALUES = {"HL_TEST_CLAUDE": "secret-for-claude", "HL_TEST_GH": "secret-for-gh", "HL_TEST_EMPTY": ""}


@unittest.skipUnless(SHELL, "needs zsh or bash")
class EnvTest(unittest.TestCase):
    def source(self, *args, before="", script=SCRIPT):
        line = f"{before}\n. {script} {' '.join(args)}\n{REPORT}\n"
        proc = subprocess.run([*BARE, "-c", line], capture_output=True, text=True, env={"PATH": os.environ["PATH"], **VALUES})
        for value in ("secret-for-claude", "secret-for-gh"):
            self.assertNotIn(value, proc.stdout + proc.stderr, "a secret was printed")
        return proc.stdout.strip().splitlines()

    def test_both_reads_export_the_tokens_and_print_the_ready_line_last(self):
        out = self.source(CLAUDE_REF, GH_REF, "ab12cd34")
        self.assertEqual(out, ["hl-env ok ab12cd34-ready", "rc=0", "claude=set gh=set api= memory=1"])

    def test_the_values_exported_are_the_ones_read(self):
        line = f". {SCRIPT} {CLAUDE_REF} {GH_REF} ab12cd34 >/dev/null\n"
        line += '[ "$GH_TOKEN" = "$HL_TEST_GH" ] && echo gh-is-the-read-one\n[ "$CLAUDE_CODE_OAUTH_TOKEN" = "$HL_TEST_CLAUDE" ] && echo claude-is-the-read-one\n'
        proc = subprocess.run([*BARE, "-c", line], capture_output=True, text=True, env={"PATH": os.environ["PATH"], **VALUES})
        self.assertEqual(proc.stdout.split(), ["gh-is-the-read-one", "claude-is-the-read-one"])

    def test_a_cursor_worker_gets_no_claude_token(self):
        out = self.source("-", GH_REF, "ab12cd34")
        self.assertEqual(out, ["hl-env ok ab12cd34-ready", "rc=0", "claude= gh=set api= memory=1"])

    def test_an_empty_claude_read_fails_closed_with_the_readers_reason_and_names_this_spawn(self):
        out = self.source(EMPTY_REF, GH_REF, "ab12cd34")
        self.assertEqual(out, ["hl-env FAILED ab12cd34: no Claude token: hl-read-secret: empty read: env:HL_TEST_EMPTY is set and empty", "rc=1", "claude= gh= api= memory="])

    def test_an_empty_github_read_exports_nothing_at_all(self):
        out = self.source(CLAUDE_REF, EMPTY_REF, "ab12cd34")
        self.assertEqual(out, ["hl-env FAILED ab12cd34: no GitHub token: hl-read-secret: empty read: env:HL_TEST_EMPTY is set and empty", "rc=1", "claude= gh= api= memory="])

    def test_a_reference_the_reader_cannot_place_fails_closed_without_repeating_it(self):
        out = self.source(CLAUDE_REF, "not-a-reference-but-a-token", "ab12cd34")
        self.assertEqual(out[0], "hl-env FAILED ab12cd34: no GitHub token: hl-read-secret: unknown reference scheme: a reference starts with file:, op://, keychain: or env:")
        self.assertNotIn("not-a-reference-but-a-token", out[0])

    def test_tokens_already_in_the_shell_are_dropped_before_anything_is_read(self):
        before = "export GH_TOKEN=keyring-admin CLAUDE_CODE_OAUTH_TOKEN=signed-in ANTHROPIC_API_KEY=key"
        out = self.source(EMPTY_REF, GH_REF, "ab12cd34", before=before)
        self.assertEqual(out[1:], ["rc=1", "claude= gh= api= memory="])
        # Nor can a reference read one of them back: the reader refuses those names.
        out = self.source("-", "env:GH_TOKEN", "ab12cd34", before=before)
        self.assertRegex(out[0], r"^hl-env FAILED ab12cd34: no GitHub token: hl-read-secret: env:GH_TOKEN is refused")
        self.assertEqual(out[1:], ["rc=1", "claude= gh= api= memory="])

    def test_a_kit_without_the_reader_beside_it_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            alone = Path(tmp).resolve()
            shutil.copy(SCRIPT, alone / "hl-env.sh")
            out = self.source(CLAUDE_REF, GH_REF, "ab12cd34", script=alone / "hl-env.sh")
        self.assertEqual(out, [f"hl-env FAILED ab12cd34: {alone}/hl-read-secret is not there to read with", "rc=1", "claude= gh= api= memory="])

    def test_the_readers_message_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for refs in ((CLAUDE_REF, GH_REF), (EMPTY_REF, GH_REF), (CLAUDE_REF, EMPTY_REF)):
                self.source(*refs, "ab12cd34", before=f"export TMPDIR={tmp}")
            self.assertEqual(os.listdir(tmp), [])

    def test_neither_line_the_spawn_waits_for_is_in_the_line_that_is_typed(self):
        typed = f". {SCRIPT} 'op://v/claude/password' 'op://v/gh/password' ab12cd34"
        for line in ("hl-env ok ab12cd34-ready", "hl-env FAILED ab12cd34:"):
            self.assertNotIn(line, typed)

    def test_the_function_does_not_stay_defined_in_the_workers_shell(self):
        line = f". {SCRIPT} - {GH_REF} ab12cd34 >/dev/null\ncommand -v hl_env >/dev/null && echo defined || echo gone\n"
        proc = subprocess.run([*BARE, "-c", line], capture_output=True, text=True, env={"PATH": os.environ["PATH"], **VALUES})
        self.assertEqual(proc.stdout.strip(), "gone")


if __name__ == "__main__":
    unittest.main()
