"""hl-read-secret: every kind of reference, and every refusal.

Nothing here reads a real secret store. `op` and `security` are stand-ins on
a PATH of the test's own, ahead of /usr/bin and /bin only, and each records
how it was called; a test of a missing `op` runs where none can be found.
Every run gets a home and a temporary directory of its own.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from .support import KIT

READER = KIT / "scripts" / "hl-read-secret"

# A stand-in for `op`. It writes its arguments beside itself, then answers from the files there: what to print on
# standard output and on standard error, and the exit status.
FAKE_OP = r"""#!/bin/sh
here=$(dirname "$0")
printf '%s\n' "$@" > "$here/op.argv"
[ -f "$here/op.stderr" ] && cat "$here/op.stderr" >&2
[ -f "$here/op.stdout" ] && cat "$here/op.stdout"
exit "$(cat "$here/op.rc" 2>/dev/null || echo 0)"
"""

# A stand-in for macOS `security`, the same way. The real one prints the password and a newline.
FAKE_SECURITY = r"""#!/bin/sh
here=$(dirname "$0")
printf '%s\n' "$@" > "$here/security.argv"
[ -f "$here/security.stderr" ] && cat "$here/security.stderr" >&2
[ -f "$here/security.stdout" ] && cat "$here/security.stdout"
exit "$(cat "$here/security.rc" 2>/dev/null || echo 0)"
"""


class ReadSecretCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.bin = self.tmp / "bin"
        self.home = self.tmp / "home"
        self.scratch = self.tmp / "scratch"
        for d in (self.bin, self.home, self.scratch):
            d.mkdir()
        for name, text in (("op", FAKE_OP), ("security", FAKE_SECURITY)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "TMPDIR": str(self.scratch)}

    def read(self, *args, **env):
        proc = subprocess.run([str(READER), *args], capture_output=True, env={**self.env, **env}, stdin=subprocess.DEVNULL, timeout=30)
        return proc.returncode, proc.stdout.decode(), proc.stderr.decode()

    def refused(self, *args, **env) -> str:
        """The one line a refusal prints, after checking it is a refusal: exit 1, nothing on standard output."""
        code, out, err = self.read(*args, **env)
        self.assertEqual((code, out), (1, ""), err)
        self.assertTrue(err.startswith("hl-read-secret: ") and err.endswith("\n"), err)
        self.assertEqual(err.count("\n"), 1, f"more than one line on standard error: {err!r}")
        return err[len("hl-read-secret: ") : -1]

    def answer(self, tool: str, stdout: str = "", stderr: str = "", rc: int = 0) -> None:
        for part, text in (("stdout", stdout), ("stderr", stderr), ("rc", str(rc))):
            (self.bin / f"{tool}.{part}").write_text(text)

    def argv(self, tool: str) -> list[str]:
        return (self.bin / f"{tool}.argv").read_text().splitlines()

    def secret_file(self, content: str, mode: int = 0o600, name: str = "token") -> Path:
        path = self.home / name
        if path.exists():
            # A mode a test gave it may not let it be written again.
            path.unlink()
        path.write_text(content)
        path.chmod(mode)
        return path


class FileTest(ReadSecretCase):
    def test_the_content_is_given_less_one_trailing_newline_and_with_none_added(self):
        for content, given in (("tok-123\n", "tok-123"), ("tok-123", "tok-123"), ("tok-123\n\n", "tok-123\n"), ("two\nlines\n", "two\nlines"), ("  spaced  \n", "  spaced  ")):
            path = self.secret_file(content)
            self.assertEqual(self.read(f"file:{path}"), (0, given, ""), repr(content))

    def test_a_leading_tilde_slash_is_the_home_directory(self):
        self.secret_file("from-home\n", name="gh-token")
        self.assertEqual(self.read("file:~/gh-token"), (0, "from-home", ""))
        (self.home / ".config" / "herdr-loop").mkdir(parents=True)
        self.secret_file("nested\n", name=".config/herdr-loop/gh-token")
        self.assertEqual(self.read("file:~/.config/herdr-loop/gh-token"), (0, "nested", ""))

    def test_a_tilde_in_any_other_form_is_refused(self):
        self.assertIn("takes ~/ for the home directory", self.refused("file:~someone/token"))
        self.assertIn("takes ~/ for the home directory", self.refused("file:~"))

    def test_a_file_others_may_use_is_refused_whatever_its_owner_may_do(self):
        for mode in (0o640, 0o604, 0o644, 0o660, 0o602, 0o610, 0o601, 0o400 | 0o040):
            path = self.secret_file("tok\n", mode=mode)
            said = self.refused(f"file:{path}")
            self.assertIn("group or others may use it", said, oct(mode))
            self.assertIn("chmod 600", said)
            self.assertNotIn("tok", said.replace(str(path), ""))
        # Readable by its owner alone, in any way the owner likes, it is read.
        for mode in (0o600, 0o400, 0o700):
            path = self.secret_file("tok\n", mode=mode)
            self.assertEqual(self.read(f"file:{path}"), (0, "tok", ""), oct(mode))

    @unittest.skipIf(os.getuid() == 0, "root owns the file it would be compared with")
    def test_a_file_of_another_user_is_refused(self):
        theirs = next((p for p in (Path("/etc/hosts"), Path("/etc/passwd")) if p.exists() and p.resolve().stat().st_uid != os.getuid()), None)
        if theirs is None:
            self.skipTest("no file of another user to point at")
        said = self.refused(f"file:{theirs}")
        self.assertIn(f"belongs to uid {theirs.resolve().stat().st_uid}, not to you (uid {os.getuid()})", said)

    def test_a_link_is_judged_by_the_file_it_points_at(self):
        target = self.secret_file("linked\n")
        link = self.home / "link"
        link.symlink_to(target)
        self.assertEqual(self.read(f"file:{link}"), (0, "linked", ""))
        target.chmod(0o644)
        self.assertIn("group or others may use it", self.refused(f"file:{link}"))

    def test_a_path_with_a_glob_character_is_read_as_it_is(self):
        path = self.secret_file("starred\n", name="to*en")
        self.secret_file("other\n", mode=0o644, name="toBen")
        self.assertEqual(self.read(f"file:{path}"), (0, "starred", ""))

    def test_a_missing_file_a_directory_and_an_empty_file_are_refused(self):
        self.assertEqual(self.refused(f"file:{self.home}/nope"), f"{self.home}/nope: no such file")
        self.assertEqual(self.refused(f"file:{self.home}"), f"{self.home}: not a regular file")
        empty = self.secret_file("")
        self.assertEqual(self.refused(f"file:{empty}"), f"empty read: {empty} holds nothing")
        lone = self.secret_file("\n")
        self.assertEqual(self.refused(f"file:{lone}"), f"empty read: {lone} holds nothing")
        self.assertEqual(self.refused("file:"), "file: names no path")

    @unittest.skipIf(os.getuid() == 0, "root reads a file whatever its mode")
    def test_a_file_its_owner_cannot_read_is_refused(self):
        path = self.secret_file("tok\n", mode=0o200)
        self.assertEqual(self.refused(f"file:{path}"), f"{path}: not readable")


class OnePasswordTest(ReadSecretCase):
    REF = "op://Loop/gh-token/credential"

    def test_op_read_is_asked_for_the_reference_with_no_newline_and_its_answer_is_given(self):
        self.answer("op", stdout="tok-from-op")
        self.assertEqual(self.read(self.REF), (0, "tok-from-op", ""))
        self.assertEqual(self.argv("op"), ["read", "--no-newline", "--", self.REF])

    def test_a_failed_read_says_ops_first_line_and_nothing_else(self):
        self.answer("op", stderr="\n[ERROR] 2026/10/13 09:14:02 Too many requests\nsecond line\n", rc=1)
        self.assertEqual(self.refused(self.REF), "op read failed (exit 1): [ERROR] 2026/10/13 09:14:02 Too many requests")
        self.answer("op", rc=6)
        self.assertEqual(self.refused(self.REF), "op read failed (exit 6): it said nothing")

    def test_a_read_that_gives_nothing_is_an_empty_read(self):
        self.answer("op", stdout="")
        self.assertEqual(self.refused(self.REF), f"empty read: op read gave nothing for {self.REF}")

    def test_without_op_it_says_so(self):
        (self.bin / "op").unlink()
        self.assertIn("op, the 1Password CLI that reads op:// references, is not installed or not on PATH", self.refused(self.REF))

    def test_the_file_that_held_ops_messages_is_removed(self):
        self.answer("op", stderr="[ERROR] nope\n", rc=1)
        self.refused(self.REF)
        self.answer("op", stdout="tok")
        self.read(self.REF)
        self.assertEqual(os.listdir(self.scratch), [])


class KeychainTest(ReadSecretCase):
    def test_security_is_asked_for_the_services_password_and_its_newline_is_taken_off(self):
        self.answer("security", stdout="tok-from-keychain\n")
        self.assertEqual(self.read("keychain:herdr-loop-gh"), (0, "tok-from-keychain", ""))
        self.assertEqual(self.argv("security"), ["find-generic-password", "-w", "-s", "herdr-loop-gh"])

    def test_a_failed_read_says_securitys_first_line(self):
        self.answer("security", stderr="security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n", rc=44)
        said = self.refused("keychain:herdr-loop-gh")
        self.assertEqual(
            said,
            "keychain item for service 'herdr-loop-gh' could not be read (exit 44): "
            "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.",
        )

    def test_over_ssh_the_refusal_says_the_login_keychain_is_locked_there(self):
        self.answer("security", stderr="security: SecKeychainSearchCopyNext: User interaction is not allowed.\n", rc=36)
        said = self.refused("keychain:herdr-loop-gh", SSH_CONNECTION="10.0.0.2 50000 10.0.0.3 22")
        self.assertIn("This is an SSH shell, where the login keychain is locked", said)
        self.assertIn("stage.sh over SSH needs a file: reference", said)

    def test_an_empty_password_and_a_missing_service_are_refused(self):
        self.answer("security", stdout="\n")
        self.assertEqual(self.refused("keychain:herdr-loop-gh"), "empty read: the keychain item for service 'herdr-loop-gh' holds nothing")
        self.assertEqual(self.refused("keychain:"), "keychain: names no service")


class EnvTest(ReadSecretCase):
    def test_the_variables_value_is_given(self):
        self.assertEqual(self.read("env:LOOP_GH_TOKEN", LOOP_GH_TOKEN="tok-from-env"), (0, "tok-from-env", ""))
        # Its own trailing newline is kept: only the one `printenv` adds is taken off.
        self.assertEqual(self.read("env:LOOP_GH_TOKEN", LOOP_GH_TOKEN="tok\n"), (0, "tok\n", ""))

    def test_the_tokens_hl_env_unsets_are_refused(self):
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
            said = self.refused(f"env:{name}", **{name: "set-anyway"})
            self.assertTrue(said.startswith(f"env:{name} is refused: hl-env.sh unsets {name}"), said)
            self.assertNotIn("set-anyway", said)

    def test_an_unset_an_empty_and_a_badly_named_variable_are_refused(self):
        self.assertEqual(self.refused("env:LOOP_GH_TOKEN"), "env:LOOP_GH_TOKEN is not set")
        self.assertEqual(self.refused("env:LOOP_GH_TOKEN", LOOP_GH_TOKEN=""), "empty read: env:LOOP_GH_TOKEN is set and empty")
        for bad in ("", "1ST", "A-B", "A B", "$(id)"):
            self.assertIn("env: needs a variable's name", self.refused(f"env:{bad}"), bad)

    def test_a_variable_of_the_scripts_own_is_not_read(self):
        # Set inside the script and never exported: not the environment the reader was given.
        self.assertEqual(self.refused("env:hlrs_ref"), "env:hlrs_ref is not set")


class AnythingElseTest(ReadSecretCase):
    def test_a_reference_of_no_known_scheme_is_refused_without_being_repeated(self):
        for ref in ("ghp_looks-like-a-token", "op:/one-slash", "File:/tmp/x", "vault://x", "/abs/path"):
            said = self.refused(ref)
            self.assertEqual(said, "unknown reference scheme: a reference starts with file:, op://, keychain: or env:")
            self.assertNotIn(ref, said)

    def test_it_takes_exactly_one_argument(self):
        for args in ((), ("file:/a", "file:/b")):
            self.assertTrue(self.refused(*args).startswith("usage: hl-read-secret"))

    def test_it_is_executable_and_a_posix_shell_script(self):
        self.assertTrue(READER.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(READER.read_text().splitlines()[0], "#!/bin/sh")


if __name__ == "__main__":
    unittest.main()
