"""install-watchdog.sh, with stand-ins for `launchctl` and `plutil` and a
home of the test's own: the label it installs under, the plist it writes,
and taking it away again. Nothing reaches the real launchd."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from .support import KIT

SCRIPT = KIT / "scripts" / "install-watchdog.sh"

# Writes each call's arguments, one call a line, beside itself.
RECORDER = """#!/bin/sh
printf '%s\\n' "$(basename "$0") $*" >> "$(dirname "$0")/calls"
"""


@unittest.skipUnless(Path("/usr/bin/python3").exists(), "the script works out the label with /usr/bin/python3")
class InstallWatchdogTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in ("launchctl", "plutil"):
            (self.bin / name).write_text(RECORDER)
            (self.bin / name).chmod(0o755)
        self.root = self.tmp / "loops" / "help-desk"
        (self.root / ".claude" / "skills").mkdir(parents=True)
        (self.root / ".claude" / "skills" / "herdr-loop").symlink_to(KIT)
        (self.root / "program.json").write_text('{"program": "Help Desk_2"}\n')

    def run_it(self, *args, **env):
        full = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", **env}
        return subprocess.run(["/bin/sh", str(SCRIPT), *args], capture_output=True, text=True, env=full, timeout=60)

    def calls(self) -> list[str]:
        path = self.bin / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def test_the_label_is_the_default_prefix_and_the_programs_name(self):
        done = self.run_it(str(self.root))
        self.assertEqual(done.returncode, 0, done.stderr)
        label = "local.herdr-loop.watchdog.help-desk-2"
        plist = self.home / "Library" / "LaunchAgents" / f"{label}.plist"
        self.assertTrue(plist.exists())
        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["Label"], label)
        self.assertEqual(data["ProgramArguments"], [f"{self.root}/.claude/skills/herdr-loop/scripts/hl-watchdog", "--root", str(self.root), "tick"])
        self.assertEqual((data["StartInterval"], data["RunAtLoad"], data["StandardOutPath"]), (300, False, f"{self.root}/watchdog.out"))
        self.assertTrue(data["EnvironmentVariables"]["PATH"].startswith(f"{self.home}/.local/bin:/opt/homebrew/bin:"))
        uid = os.getuid()
        self.assertEqual(self.calls(), [f"launchctl bootout gui/{uid}/{label}", f"plutil -lint {plist}", f"launchctl bootstrap gui/{uid} {plist}"])
        self.assertIn(f"installed {label}", done.stdout)

    def test_the_prefix_is_the_operators_to_choose(self):
        done = self.run_it(str(self.root), HL_LAUNCHD_PREFIX="org.example.loops")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.home / "Library" / "LaunchAgents" / "org.example.loops.watchdog.help-desk-2.plist").exists())

    def test_remove_takes_the_agent_away_under_the_same_label(self):
        self.run_it(str(self.root))
        plist = self.home / "Library" / "LaunchAgents" / "local.herdr-loop.watchdog.help-desk-2.plist"
        done = self.run_it("--remove", str(self.root))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(plist.exists())
        self.assertEqual(self.calls()[-1], f"launchctl bootout gui/{os.getuid()}/local.herdr-loop.watchdog.help-desk-2")
        self.assertIn("removed local.herdr-loop.watchdog.help-desk-2", done.stdout)

    def test_a_directory_that_is_no_loop_root_is_refused(self):
        (self.root / "program.json").unlink()
        done = self.run_it(str(self.root))
        self.assertEqual(done.returncode, 1)
        self.assertIn("program.json does not exist", done.stderr)
        self.assertEqual(self.calls(), [])
        done = self.run_it()
        self.assertEqual(done.returncode, 64)

    def test_the_script_is_executable(self):
        self.assertTrue(SCRIPT.stat().st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
