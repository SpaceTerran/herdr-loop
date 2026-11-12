from __future__ import annotations

import os
import random
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from hl import reap

from .support import KIT, FakeProcs

TERM, KILL = signal.SIGTERM, signal.SIGKILL
ZSH = Path("/bin/zsh")


class FindTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.shells = Path(self._tmp.name) / "build-1.shells"

    def write(self, *lines):
        self.shells.write_text("".join(line + "\n" for line in lines))

    def test_what_a_shell_that_is_gone_left_running_is_stopped_and_nothing_else(self):
        # What the mini showed: the tool shell 4000 exited, its two jobs were adopted by launchd and kept its session.
        self.write("3990 90", "4000 100")
        procs = FakeProcs()
        procs.add(4001, 4000, "sleep 4271")
        procs.add(4002, 4000, "sleep 4272")
        procs.add(5000, 5000, "sleep 600", start=50.0)
        out = reap.reap(procs, self.shells)
        self.assertEqual(procs.killed, [(4001, TERM), (4002, TERM)])
        self.assertEqual(out["reaped"], [{"pid": 4001, "command": "sleep 4271", "signal": "TERM"}, {"pid": 4002, "command": "sleep 4272", "signal": "TERM"}])
        self.assertEqual((out["left"], out["shells"]), ([], 2))
        self.assertIn(5000, procs.rows)

    def test_one_that_ignores_term_is_killed_and_one_that_outlives_kill_is_reported(self):
        self.write("4000 100")
        procs = FakeProcs(ends_on={4001: (KILL,), 4002: ()})
        procs.add(4001, 4000, "sh -c trap '' TERM; while :; do :; done")
        procs.add(4002, 4000, "stuck")
        out = reap.reap(procs, self.shells)
        self.assertEqual(procs.killed, [(4001, TERM), (4002, TERM), (4001, KILL), (4002, KILL)])
        self.assertEqual([(p["pid"], p["signal"]) for p in out["reaped"]], [(4001, "KILL")])
        self.assertEqual(out["left"], [{"pid": 4002, "command": "stuck"}])
        self.assertIn("1 still running after KILL (pid 4002)", reap.said(out))

    def test_a_shell_still_running_is_stopped_with_its_jobs_when_it_started_when_it_said(self):
        self.write("4000 100")
        procs = FakeProcs()
        procs.add(4000, 4000, "/bin/zsh -c run_in_background", start=101.0)
        procs.add(4001, 4000, "python3 -m http.server 8401", ppid=4000)
        reap.reap(procs, self.shells)
        self.assertEqual(sorted(pid for pid, _ in procs.killed), [4000, 4001])

    def test_a_pid_given_to_another_process_since_is_not_the_workers(self):
        self.write("4000 100", "4100")
        procs = FakeProcs()
        procs.add(4000, 4000, "someone else's shell", start=900.0)
        procs.add(4003, 4000, "someone else's job", ppid=4000)
        procs.add(4100, 4100, "a shell that wrote no start time")
        self.assertEqual(reap.find(procs, self.shells), [])

    def test_the_manager_its_session_and_launchd_are_never_signalled(self):
        # A file that names the manager's own sessions, or anything above it, names nothing.
        self.write("890 10", "870 4", "700 1", "1 0", "900 10")
        procs = FakeProcs()
        procs.add(895, 890, "a command of the manager's", ppid=890)
        self.assertEqual(reap.find(procs, self.shells), [])

    def test_another_users_process_and_a_zombie_are_left_alone(self):
        self.write("4000 100")
        procs = FakeProcs()
        procs.add(4001, 4000, "root's", uid=0)
        procs.add(4002, 4000, "<defunct>", zombie=True)
        procs.add(4003, 4000, "sleep 4271")
        self.assertEqual([p.pid for p in reap.find(procs, self.shells)], [4003])

    def test_what_carries_the_mark_is_stopped_with_its_session(self):
        # A server a test started in a session of its own, and the shell it runs under: neither went through a shell
        # that wrote itself down, and the server's environment names this attempt.
        self.write("4000 100")
        mark = f"HL_SHELLS={self.shells}"
        procs = FakeProcs(envs={6000: ["HOME=/Users/you", mark], 7000: ["HL_SHELLS=/elsewhere/build-1.shells"]})
        procs.add(6000, 6000, "node server.js")
        procs.add(6001, 6000, "/bin/sh -c watch", ppid=6000)
        procs.add(7000, 7000, "another attempt's server")
        self.assertEqual(sorted(p.pid for p in reap.find(procs, self.shells)), [6000, 6001])

    def test_nothing_is_looked_for_when_no_shell_wrote_itself_down(self):
        procs = FakeProcs()
        out = reap.reap(procs, self.shells)
        self.assertEqual((out, procs.looked), ({"shells": 0, "reaped": [], "left": []}, 0))
        self.assertIsNone(reap.said(out))

    def test_a_line_that_is_not_a_shell_is_skipped(self):
        self.write("4000 100", "", "garbage", "4001", "4002 x")
        self.assertEqual(reap.recorded(self.shells), [(4000, 100), (4001, None), (4002, None)])

    def test_the_environment_is_read_from_after_the_arguments(self):
        raw = struct.pack("i", 2) + b"/bin/sleep\0\0\0\0sleep\0HL_SHELLS=not-env\0HOME=/x\0HL_SHELLS=/y\0\0junk\0"
        self.assertEqual(reap.environ_of(raw), ["HOME=/x", "HL_SHELLS=/y"])
        self.assertEqual(reap.environ_of(b"\0\0"), [])


@unittest.skipUnless(sys.platform == "darwin" and ZSH.exists(), "needs macOS and /bin/zsh")
class OnThisMachineTest(unittest.TestCase):
    """The handle, on the real machine: a zsh started in a session of its
    own, as Claude Code starts its Bash tool's shell, leaves two jobs and
    exits. launchd adopts them; they keep the dead shell's session, which
    the kit's `.zshenv` wrote down; the reap stops them and nothing else."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.shells = self.tmp / "build-1.shells"
        self.started: list[int] = []
        self.children: list[subprocess.Popen] = []
        self.addCleanup(self._kill_started)
        # Durations no other process on the machine is running, to find these by.
        self.tag = random.randint(100000, 999999)

    def _kill_started(self):
        for pid in self.started + [child.pid for child in self.children]:
            try:
                os.kill(pid, KILL)
            except OSError:
                pass
        for child in self.children:
            child.wait(timeout=10)

    def env(self, **extra):
        # The operator's own files are never read here: HOME is the test's.
        return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.home), "HL_SHELLS": str(self.shells), "ZDOTDIR": str(reap.zdotdir(KIT)), **extra}

    def sleeps(self, *seconds) -> dict[int, reap.Proc]:
        wanted = {f"sleep {s}" for s in seconds}
        found = {}
        for _ in range(50):
            found = {p.pid: p for p in reap.Procs().table() if p.command in wanted and not p.zombie}
            if len(found) == len(wanted):
                break
            time.sleep(0.1)
        self.started += list(found)
        return found

    def test_a_tool_shells_orphans_are_found_by_its_session_and_stopped(self):
        a, b, apart, here = self.tag * 10 + 1, self.tag * 10 + 2, self.tag * 10 + 3, self.tag * 10 + 4
        (self.home / ".zshenv").write_text('print -r -- "read ${ZDOTDIR-unset}" > ~/zshenv-was-read\n')
        tool = subprocess.Popen([str(ZSH), "-c", f"(sleep {a}) & (sleep {b}) & exit"], env=self.env(), start_new_session=True)
        tool.wait(timeout=10)
        # Not the worker's: one in a session of its own that wrote nothing down, and one in this test's own session.
        others = [subprocess.Popen(["/bin/sleep", str(apart)], start_new_session=True), subprocess.Popen(["/bin/sleep", str(here)])]
        self.children += others

        left = self.sleeps(a, b)
        self.assertEqual(len(left), 2, "the shell's two jobs are running")
        self.assertEqual(reap.recorded(self.shells)[0][0], tool.pid, "the shell wrote itself down")
        self.assertEqual((self.home / "zshenv-was-read").read_text(), "read unset\n", "the operator's .zshenv is read, with ZDOTDIR as it was")
        for proc in left.values():
            self.assertEqual(proc.ppid, 1, "launchd adopted it")
            self.assertEqual(os.getsid(proc.pid), tool.pid, "it keeps the session of the shell that is gone")
            self.assertNotEqual(os.getsid(proc.pid), os.getsid(0))

        out = reap.reap(reap.Procs(), self.shells)
        self.assertEqual(sorted(p["pid"] for p in out["reaped"]), sorted(left))
        self.assertEqual({p["signal"] for p in out["reaped"]}, {"TERM"})
        self.assertEqual(out["left"], [])
        self.assertEqual([p.poll() for p in others], [None, None], "nothing else was signalled")

    def test_a_program_in_a_session_of_its_own_is_found_by_its_environment(self):
        probe = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=self.env(), start_new_session=True)
        self.children.append(probe)
        time.sleep(0.3)
        if f"HL_SHELLS={self.shells}" not in reap.Procs().environ(probe.pid):
            self.skipTest(f"macOS does not show the environment of {sys.executable}")
        self.shells.write_text("")
        out = reap.reap(reap.Procs(), self.shells)
        self.assertEqual([p["pid"] for p in out["reaped"]], [probe.pid])
        self.assertIsNotNone(probe.wait(timeout=5))

    def tool_shell(self, command: str, stdin) -> subprocess.CompletedProcess:
        """A zsh started as Claude Code starts its Bash tool's: non-interactive, in a session of its own."""
        return subprocess.run([str(ZSH), "-c", command], env=self.env(), stdin=stdin, start_new_session=True, capture_output=True, text=True, timeout=15)

    def test_a_tool_shell_given_a_socket_nobody_writes_to_reads_nothing_and_does_not_wait(self):
        # What Claude Code gives a command that holds a heredoc: a socket it never writes to or closes.
        mine, theirs = socket.socketpair()
        self.addCleanup(mine.close)
        self.addCleanup(theirs.close)
        out = self.tool_shell(f"{sys.executable} -; print -r -- rc=$?", theirs.fileno())
        self.assertEqual(out.stdout, "rc=0\n", "`python3 -` read an empty program from /dev/null instead of waiting")

    def test_a_tool_shell_still_reads_a_pipe_a_file_and_its_own_heredocs(self):
        piped = subprocess.run(f"print -r -- from-a-pipe | {ZSH} -c 'cat'", shell=True, executable=str(ZSH), env=self.env(), start_new_session=True, capture_output=True, text=True, timeout=15)
        self.assertEqual(piped.stdout, "from-a-pipe\n")
        given = self.tmp / "given.txt"
        given.write_text("from-a-file\n")
        with given.open() as handle:
            self.assertEqual(self.tool_shell("cat", handle).stdout, "from-a-file\n")
        mine, theirs = socket.socketpair()
        self.addCleanup(mine.close)
        self.addCleanup(theirs.close)
        heredoc = f"{sys.executable} - <<'EOF'\nprint('from-a-heredoc')\nEOF"
        self.assertEqual(self.tool_shell(heredoc, theirs.fileno()).stdout, "from-a-heredoc\n")

    def test_an_interactive_shell_keeps_its_socket(self):
        mine, theirs = socket.socketpair()
        self.addCleanup(mine.close)
        self.addCleanup(theirs.close)
        mine.sendall(b"print -r -- typed-in\nexit\n")
        out = subprocess.run([str(ZSH), "-i"], env=self.env(), stdin=theirs.fileno(), start_new_session=True, capture_output=True, text=True, timeout=15)
        self.assertIn("typed-in", out.stdout)

    def test_the_pane_line_writes_the_pane_shell_down_and_keeps_zdotdir(self):
        line = reap.pane_line(self.shells, KIT, "abc123")
        out = subprocess.run([str(ZSH), "-f", "-c", f"ZDOTDIR=/before; {line}; print -r -- $HL_ZDOTDIR $ZDOTDIR"], env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)}, capture_output=True, text=True, timeout=10)
        self.assertEqual(out.stdout.splitlines(), ["hl-shells ok abc123-ready", f"/before {reap.zdotdir(KIT)}"])
        self.assertNotIn("abc123-ready", line)
        [(pid, began)] = reap.recorded(self.shells)
        self.assertLessEqual(abs(began - time.time()), reap.SLACK_S + 10)
        self.assertGreater(pid, 1)


if __name__ == "__main__":
    unittest.main()
