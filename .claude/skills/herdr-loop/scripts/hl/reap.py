"""Stop what a worker left running once its pane is closed.

What was seen on the mini (2026-11-02, Claude Code 2.1.316, macOS 27):

- Claude Code runs each Bash command in a zsh it starts in a session of its
  own: no terminal, and the shell's pid is its session and its process
  group. A job the command put in the background keeps that session when
  the shell exits and launchd adopts it. The pane shell's session never holds
  it, and its process group is that one command's. A builder's four CPU
  loops ran on that way for nine and a half hours (the help-desk catalog
  program; a decision in its ledger).
- A session id lasts while anything is in it, and macOS does not give the
  number to a new process until then: the session of a shell long gone
  still names everything it left.
- On macOS `ps -o sess` prints 0 and `pgrep` has no `-s`; `os.getsid` reads
  any process's session.
- macOS hides the environment of its own programs (`/bin/zsh`, `/bin/sleep`)
  from other processes, so a variable set for the worker finds a `node`, a
  `python` or a browser, and never a shell loop or a `sleep`.

So every shell of the worker writes itself down as it starts. Just before
the agent starts, `hl-spawn start` types one line into the worker's pane
(`pane_line`): it names the attempt's shells file in `HL_SHELLS`, points
`ZDOTDIR` at the kit's `zdotdir/`, and writes the pane shell down. Every zsh
started under the pane reads `zdotdir/.zshenv` first; it writes the shell's
pid and start time to that file, puts `ZDOTDIR` back and reads the
operator's own `.zshenv`, so the shell is otherwise the one it would have
been.

Once the worker's pane is closed, a process is the worker's when its session
is one a shell in that file led, when its environment (where macOS shows it)
names that file, or when it shares a session with one that does: a program
that started a server or a browser in a session of its own. Those are sent
TERM, and what is still there a few seconds later KILL. Never signalled:
this process, its ancestors and their sessions (the manager and the Herdr
server), and anything another user runs. A shell still running counts only
if it started when it wrote it did: a pid since given to another process
does not. Only shells write to the file, so a record whose pane never held
a real shell names nothing.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shlex
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PS = "/bin/ps"
# The variable that names an attempt's shells file: in the pane and in every process the worker starts.
VAR = "HL_SHELLS"
# Where `ZDOTDIR` pointed before the pane was given the kit's, for `zdotdir/.zshenv` to put back.
WAS = "HL_ZDOTDIR"
# A shell's own clock reading and the start time `ps` gives in whole seconds differ by up to this.
SLACK_S = 2
TERM_WAIT_S = 5
KILL_WAIT_S = 5
SHOWN = 300

CTL_KERN, KERN_PROCARGS2 = 1, 49


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    uid: int
    start: float | None
    command: str
    zombie: bool = False


def shells_path(item_dir, phase: str, n: int) -> Path:
    return Path(item_dir) / f"{phase}-{n}.shells"


def zdotdir(kit) -> Path:
    return Path(kit) / "scripts" / "zdotdir"


def pane_line(shells: Path, kit, nonce: str) -> str:
    """The line typed into a worker's pane before its agent starts. The
    ready line carries the nonce in a form the typed line does not have."""
    q = shlex.quote
    # ZDOTDIR is read into HL_ZDOTDIR by a command of its own, before it is changed. The pane shell is
    # written down with the time it started: `SECONDS` counts from there.
    return (
        f'export {WAS}="${{ZDOTDIR-}}" {VAR}={q(str(shells))} && export ZDOTDIR={q(str(zdotdir(kit)))} && '
        f'printf \'%s %s\\n\' "$$" "$(( $(date +%s) - SECONDS ))" >> "${VAR}" && '
        f"printf 'hl-shells ok %s-%s\\n' {nonce} ready || printf 'hl-shells FAILED %s\\n' {nonce}"
    )


def recorded(path) -> list[tuple[int, int | None]]:
    """The shells a worker wrote down: pid and start time, as written. A line that is not one is skipped."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        words = line.split()
        if words and words[0].isdigit():
            out.append((int(words[0]), int(words[1]) if len(words) > 1 and words[1].isdigit() else None))
    return out


class Procs:
    """The machine's processes, as far as reaping needs them."""

    def __init__(self):
        self._libc = None

    def table(self) -> list[Proc]:
        out = subprocess.run(
            [PS, "-A", "-ww", "-o", "pid=,ppid=,uid=,stat=,lstart=,command="],
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
        rows = []
        for line in out.splitlines():
            # pid ppid uid stat, the start time in five words, then the command line.
            words = line.split(None, 9)
            if len(words) < 9 or not words[0].isdigit():
                continue
            rows.append(Proc(int(words[0]), int(words[1]), int(words[2]), _epoch(" ".join(words[4:9])), words[9] if len(words) > 9 else "", words[3].startswith("Z")))
        return rows

    def session(self, pid: int) -> int | None:
        try:
            return os.getsid(pid)
        except OSError:
            return None

    def environ(self, pid: int) -> list[str]:
        """A process's environment as it was started, or nothing where macOS does not show it."""
        if sys.platform != "darwin":
            return []
        if self._libc is None:
            self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            self._libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        size = ctypes.c_size_t(0)
        if self._libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if self._libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return []
        return environ_of(buf.raw[: size.value])

    def kill(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def me(self) -> int:
        return os.getpid()

    def uid(self) -> int:
        return os.getuid()

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def environ_of(raw: bytes) -> list[str]:
    """The environment in what `KERN_PROCARGS2` answers: the argument count,
    the program's path and its padding, the arguments, then the environment
    up to an empty string."""
    if len(raw) < 4:
        return []
    argc = struct.unpack_from("i", raw)[0]
    pos = raw.find(b"\0", 4)
    if pos < 0:
        return []
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    env = []
    for field in raw[pos:].split(b"\0")[argc:]:
        if not field:
            break
        env.append(field.decode("utf-8", "replace"))
    return env


def _epoch(text: str) -> float | None:
    try:
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def _ancestors(by_pid: dict, pid: int) -> set[int]:
    out = set()
    while pid in by_pid and pid not in out and pid > 1:
        out.add(pid)
        pid = by_pid[pid].ppid
    return out


def find(procs: Procs, shells: Path) -> list[Proc]:
    """The processes the worker whose shells file this is left running."""
    table = procs.table()
    by_pid = {p.pid: p for p in table}
    spared = _ancestors(by_pid, procs.me()) | {1, procs.me()}
    spared_sessions = {procs.session(pid) for pid in spared} - {None}
    sessions = set()
    for pid, began in recorded(shells):
        live = by_pid.get(pid)
        if live is not None and (began is None or live.start is None or abs(live.start - began) > SLACK_S):
            # The pid is another process's now: a session it leads is not the worker's.
            continue
        sessions.add(pid)
    mark = f"{VAR}={shells}"
    mine = [p for p in table if p.uid == procs.uid() and p.pid not in spared and not p.zombie]
    marked = {p.pid for p in mine if mark in procs.environ(p.pid)}
    sessions |= {procs.session(pid) for pid in marked} - {None}
    sessions -= spared_sessions
    return [p for p in mine if p.pid in marked or procs.session(p.pid) in sessions]


def reap(procs: Procs, shells, term_wait: float = TERM_WAIT_S, kill_wait: float = KILL_WAIT_S) -> dict:
    """TERM what the worker left running, then KILL what is still there.
    Each process is signalled only while it is the one that was found (the
    same pid and start time). `reaped` is what is gone, with the last signal
    it was sent; `left` is what KILL did not end."""
    shells = Path(shells)
    out = {"shells": len(recorded(shells)), "reaped": [], "left": []}
    if not shells.exists():
        # The pane never ran the line that creates it: no shell wrote itself down and nothing carries the mark.
        return out
    found = find(procs, shells)
    if not found:
        return out
    sent: dict[int, str] = {}

    def alive(targets: list[Proc]) -> list[Proc]:
        now = {(p.pid, p.start) for p in procs.table() if not p.zombie}
        return [p for p in targets if (p.pid, p.start) in now]

    def settle(targets: list[Proc], seconds: float) -> list[Proc]:
        deadline = procs.now() + seconds
        left = alive(targets)
        while left and procs.now() < deadline:
            procs.sleep(0.5)
            left = alive(left)
        return left

    left = found
    for sig, name, wait in ((signal.SIGTERM, "TERM", term_wait), (signal.SIGKILL, "KILL", kill_wait)):
        for p in alive(left):
            procs.kill(p.pid, sig)
            sent[p.pid] = name
        left = settle(left, wait)
        if not left:
            break
    gone = {p.pid for p in found} - {p.pid for p in left}
    out["reaped"] = [{"pid": p.pid, "command": _shown(p.command), "signal": sent.get(p.pid)} for p in found if p.pid in gone]
    out["left"] = [{"pid": p.pid, "command": _shown(p.command)} for p in left]
    return out


def _shown(command: str) -> str:
    return command if len(command) <= SHOWN else command[: SHOWN - 1] + "…"


def said(result: dict) -> str | None:
    """What a reap did, in a clause for a close note, or None when it found nothing."""
    reaped, left = result.get("reaped") or [], result.get("left") or []
    if not reaped and not left:
        return None
    parts = []
    if reaped:
        parts.append(f"stopped {len(reaped)} process{'es' if len(reaped) != 1 else ''} it left running (" + "; ".join(f"pid {p['pid']} `{p['command'][:80]}`" for p in reaped[:4]) + (f"; and {len(reaped) - 4} more" if len(reaped) > 4 else "") + ")")
    if left:
        parts.append(f"{len(left)} still running after KILL (" + "; ".join(f"pid {p['pid']}" for p in left) + ")")
    return ", ".join(parts)
