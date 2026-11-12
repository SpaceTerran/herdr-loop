"""One manager per program, and the binaries it trusts.

A program's loop root holds three files this module owns:

    manager.lock.d/claim.json   who the manager is: pid, process start time, pane
    heartbeat.json              stamped by `beat`, from the manager's own turn
    pins.json                   path and hash of every binary the loop runs

The claim is a directory because `mkdir` succeeds for exactly one caller. A
holder is alive when a process with the recorded pid still has the recorded
start time; the pid alone is not an identity, the system reuses pids. Herdr's
view of the pane is never asked: a pane outlives the agent that ran in it.

A pin covers the file a name resolves to. For a launcher that loads other
files (the Cursor CLI), an upgrade shows as a changed resolved path; for a
single binary (`claude`, which updates itself), as changed content. Vendors'
auto-updates are normal operation (operator, 2026-10-22): `check` accepts a
changed binary on its own, records the old pin, the new one and its version in
the history, and goes on. It refuses only a binary that is gone and does not
come back within `MISSING_WAIT_S` (an installer swapping files). `repin` is
for pinning a different file by hand.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import seat
from .common import Refusal, absolute, emit, read_json, utc_now, write_json

LOCK_DIR = "manager.lock.d"
# `cursor-agent` is the name herdr starts for `--kind cursor`; `agent` is a link to the same file.
PINNED = ("claude", "cursor-agent", "gh", "git", "herdr")
CLAIMING_GRACE_S = 30
# An installer can leave a binary missing for a moment; wait this long before refusing.
MISSING_WAIT_S = 60
MISSING_POLL_S = 5
VERSION_TIMEOUT_S = 15
PS = "/bin/ps"


class Procs:
    """Process facts, read from ps."""

    def _ps(self, fields: str, pid: int) -> str:
        proc = subprocess.run([PS, "-o", fields, "-p", str(pid)], capture_output=True, text=True)
        return proc.stdout.strip()

    def start(self, pid: int) -> str | None:
        """The process's start time as ps prints it, or None when it is gone."""
        return self._ps("lstart=", pid) or None

    def parent(self, pid: int) -> tuple[int, str] | None:
        """Parent pid and command line of a process."""
        line = self._ps("ppid=,command=", pid)
        if not line:
            return None
        ppid, _, command = line.partition(" ")
        return int(ppid), command.strip()


def find_manager_pid(procs: Procs, name: str = "claude", start_pid: int | None = None) -> int | None:
    """The nearest ancestor whose program is `name`: the agent whose shell
    tool ran this script."""
    pid = os.getppid() if start_pid is None else start_pid
    for _ in range(16):
        if pid <= 1:
            return None
        found = procs.parent(pid)
        if found is None:
            return None
        ppid, command = found
        argv0 = command.split(" ", 1)[0]
        if Path(argv0).name == name:
            return pid
        pid = ppid
    return None


# Programs that are a shell, by the name they were started under (a login shell's name begins with `-`).
SHELLS = frozenset(("sh", "bash", "zsh", "dash", "ksh", "fish"))


def started_by_holders_shell(procs: Procs, holder: dict | None, parent: int | None = None) -> str | None:
    """Why this process was not started straight from a shell that the
    agent holding the program started, or None when it was: the manager's
    own shell tool runs a command as a child of a shell that is a child of
    its `claude`. Anything between is refused: a script of the kit's (a
    Ready line `hl-flow review` runs through `/bin/sh -c` sits under the
    manager's `claude` too, and the nearest `claude` above it is the
    manager's), an interpreter, a shell a script started, an orphan. So is a
    holder that is gone, or a pid that now belongs to another process."""
    if not holder:
        return "nobody holds this program"
    if procs.start(holder["pid"]) != holder.get("start"):
        return f"the agent that holds this program (pid {holder['pid']}) is not running"
    shell = os.getppid() if parent is None else parent
    found = procs.parent(shell)
    if found is None:
        return f"the process that started this one (pid {shell}) is gone"
    grand, command = found
    name = Path(command.split(" ", 1)[0]).name.lstrip("-")
    if name not in SHELLS:
        return f"this was started by {name or '?'} (pid {shell}), not straight from a shell"
    if grand != holder["pid"]:
        return f"the shell that started this (pid {shell}) was started by pid {grand}, not by the agent that holds this program (pid {holder['pid']})"
    return None


# ---------------------------------------------------------------- claim


def _lock(root) -> Path:
    return Path(root) / LOCK_DIR


def read_claim(root) -> dict | None:
    try:
        return read_json(_lock(root) / "claim.json")
    except (OSError, ValueError):
        return None


def _log(root, event: str, record: dict | None) -> None:
    with open(Path(root) / "claims.log", "a") as fh:
        fh.write(json.dumps({"at": utc_now(), "event": event, "claim": record}, sort_keys=True) + "\n")


def last_holder(root) -> dict | None:
    """The claim most recently written to the log: the manager that held the
    program last, whether or not it still does. A manager that recycles
    itself releases its claim, and this is what still says where it ran."""
    try:
        lines = (Path(root) / "claims.log").read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line).get("claim")
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("pid"):
            return record
    return None


def last_event(root) -> dict | None:
    """The newest line of the claims log, whole: what happened to the
    program last (`claimed`, `released: <note>`, `replaced-dead-claim`), when,
    and whose claim it was about."""
    try:
        lines = (Path(root) / "claims.log").read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("event"):
            return record
    return None


def _is(holder: dict | None, pid: int, start: str) -> bool:
    return bool(holder) and holder.get("pid") == pid and holder.get("start") == start


def holder_alive(holder: dict | None, procs: Procs) -> bool:
    return bool(holder) and procs.start(holder["pid"]) == holder.get("start")


def _describe(holder: dict) -> str:
    return f"pid {holder['pid']} in pane {holder.get('pane') or '?'}, claimed {holder.get('claimed_at')}"


def claim(root, pid: int, pane: str | None, session: str | None, procs: Procs) -> dict:
    root = Path(root)
    if not root.is_dir():
        raise Refusal(f"{root} is not a directory: create the loop root first")
    start = procs.start(pid)
    if start is None:
        raise Refusal(f"process {pid} is not running; pass the manager's own pid")
    lock = _lock(root)
    parent = procs.parent(pid)
    mine = {
        "pid": pid,
        "start": start,
        "pane": pane,
        # The shell the manager was started from: how the watchdog knows the pane id still names this pane.
        "parent_pid": parent[0] if parent else None,
        "session": session,
        "host": socket.gethostname(),
        "claimed_at": utc_now(),
    }
    replaced = None
    for _ in range(3):
        try:
            os.mkdir(lock)
        except FileExistsError:
            holder = read_claim(root)
            if _is(holder, pid, start):
                if holder.get("parent_pid") is None and mine["parent_pid"] is not None:
                    # A claim from before the shell was recorded learns it the next time its holder claims.
                    holder["parent_pid"] = mine["parent_pid"]
                    write_json(lock / "claim.json", holder)
                return {"claimed": True, "already": True, "claim": holder}
            if holder_alive(holder, procs):
                raise Refusal(f"another manager holds this program: {_describe(holder)}. Stop; do not work it.")
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if holder is None and age < CLAIMING_GRACE_S:
                raise Refusal("another manager is claiming this program right now; run claim again in a minute")
            # The holder is dead. Rename is atomic: of several managers that
            # see the same dead claim, one moves it aside and the rest find
            # the winner's fresh claim on the next pass.
            aside = root / f"{LOCK_DIR}.stale-{int(time.time())}-{os.getpid()}"
            try:
                os.rename(lock, aside)
            except FileNotFoundError:
                continue
            shutil.rmtree(aside, ignore_errors=True)
            replaced = holder
            _log(root, "replaced-dead-claim", holder)
            continue
        write_json(lock / "claim.json", mine)
        # Taking the program is this manager's first sign of life: the heartbeat left by the last one is not its own.
        write_json(root / "heartbeat.json", {"at": mine["claimed_at"], "epoch": int(time.time()), "pid": pid, "note": "claimed"})
        _log(root, "claimed", mine)
        return {"claimed": True, "already": False, "claim": mine, "replaced": replaced}
    raise Refusal("lost the race for the claim three times; read manager.lock.d/claim.json and stop")


def _require_mine(root, pid: int, procs: Procs) -> dict:
    holder = read_claim(root)
    start = procs.start(pid)
    if start is None or not _is(holder, pid, start):
        who = _describe(holder) if holder else "nobody"
        raise Refusal(f"this program is not claimed by pid {pid} (holder: {who}). Stop; do not work it.")
    return holder


def beat(root, pid: int, procs: Procs, note: str | None = None) -> dict:
    """Stamp the heartbeat. Refused when the claim is not this manager's, so
    a manager that lost the program learns it at its next save."""
    _require_mine(root, pid, procs)
    record = {"at": utc_now(), "epoch": int(time.time()), "pid": pid, "note": note}
    write_json(Path(root) / "heartbeat.json", record)
    return record


def release(root, pid: int, procs: Procs, note: str) -> dict:
    if not note:
        raise Refusal("--note is required: say why the manager is giving the program up")
    holder = _require_mine(root, pid, procs)
    lock = _lock(root)
    (lock / "claim.json").unlink()
    os.rmdir(lock)
    _log(root, f"released: {note}", holder)
    return {"released": True, "claim": holder}


def status(root, procs: Procs) -> dict:
    root = Path(root)
    holder = read_claim(root)
    out = {"held": _lock(root).is_dir(), "claim": holder, "alive": holder_alive(holder, procs), "heartbeat": None}
    try:
        heartbeat = read_json(root / "heartbeat.json")
    except (OSError, ValueError):
        return out
    out["heartbeat"] = {**heartbeat, "age_s": int(time.time()) - int(heartbeat.get("epoch", 0))}
    return out


# ---------------------------------------------------------------- pins


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_binary(name: str, path: str | None = None) -> dict:
    found = path or shutil.which(name)
    if not found:
        raise Refusal(f"{name} is not on PATH")
    found = os.path.abspath(found)
    real = os.path.realpath(found)
    if not os.path.isfile(real):
        raise Refusal(f"{name}: {found} does not resolve to a file")
    return {"path": found, "realpath": real, "sha256": _sha256_file(real)}


def _pins_path(root) -> Path:
    return Path(root) / "pins.json"


def load_pins(root) -> dict:
    path = _pins_path(root)
    if not path.exists():
        raise Refusal(f"{path} does not exist: run `hl-claim pin` first")
    return read_json(path)


def pin(root, overrides: dict[str, str] | None = None) -> dict:
    """Record the binaries the first time. A second call changes nothing."""
    path = _pins_path(root)
    if path.exists():
        return {"created": False, **read_json(path)}
    overrides = overrides or {}
    pins = {
        "pinned_at": utc_now(),
        "bins": {name: describe_binary(name, overrides.get(name)) for name in PINNED},
        "history": [],
    }
    write_json(path, pins)
    return {"created": True, **pins}


def _version(path: str) -> str | None:
    """The first line `<binary> --version` prints, or None. Only for the record:
    a binary that cannot answer here (Cursor's CLI over SSH, with the keychain
    locked) is still accepted."""
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (proc.stdout or "").strip().splitlines()
    return lines[0].strip() if proc.returncode == 0 and lines else None


def _current(name: str, was: dict | None, on_path: bool) -> dict:
    """The file the name stands for now: the one on PATH for a caller with a
    pane's PATH, else the pinned path. Waits out a binary that is missing for a
    moment while it is being replaced."""
    deadline = time.monotonic() + MISSING_WAIT_S
    while True:
        found = shutil.which(name) if on_path else None
        try:
            return describe_binary(name, found or (was or {}).get("path"))
        except Refusal:
            if time.monotonic() >= deadline:
                raise
        time.sleep(MISSING_POLL_S)


def _changed(was: dict | None, now: dict) -> str | None:
    if was is None:
        return "not pinned before"
    if now["path"] != was["path"]:
        return f"now resolves to {now['path']} (was {was['path']})"
    if now["realpath"] != was["realpath"]:
        return f"now points at {now['realpath']} (was {was['realpath']})"
    if now["sha256"] != was["sha256"]:
        return "content changed"
    return None


def check_pins(root, names=PINNED, on_path: bool = True) -> dict:
    """Give the path of every named binary, accepting any that changed since
    it was pinned (an auto-update) and recording the change in the history.
    `on_path=False` uses the pinned paths only, for a caller whose PATH is not
    a pane's (launchd). Refuses only a binary that is gone."""
    pins = load_pins(root)
    current, problems = {}, []
    for name in names:
        was = pins["bins"].get(name)
        try:
            current[name] = _current(name, was, on_path)
        except Refusal as refusal:
            problems.extend(refusal.reasons)
    if problems:
        raise Refusal(problems)
    changes = {name: why for name in names if (why := _changed(pins["bins"].get(name), current[name]))}
    updated = []
    if changes:
        with _pins_lock(root):
            pins = load_pins(root)  # again, under the lock: another script may have accepted the same update
            for name, why in changes.items():
                was = pins["bins"].get(name)
                if not _changed(was, current[name]):
                    continue
                version = _version(current[name]["realpath"])
                note = f"accepted automatically: {why}" + (f"; {version}" if version else "")
                pins["history"].append({"name": name, "was": was, "at": utc_now(), "note": note, "auto": True})
                pins["bins"][name] = current[name]
                updated.append({"name": name, "why": why, "version": version})
            if updated:
                write_json(_pins_path(root), pins)
    return {"ok": True, "bins": {name: current[name]["path"] for name in names}, "updated": updated}


class _pins_lock:
    """One writer of pins.json at a time: every script of the kit checks pins."""

    def __init__(self, root):
        self.path = Path(root) / "pins.json.lock"

    def __enter__(self):
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def repin(root, name: str, note: str, path: str | None = None) -> dict:
    """Pin a binary by hand (another file than the one on PATH, say). `check`
    already accepts an auto-update on its own. The old record is kept with the reason."""
    if not note:
        raise Refusal("--note is required: say what changed the binary and how you know")
    if name not in PINNED:
        raise Refusal(f"unknown binary {name!r}; one of {', '.join(PINNED)}")
    now = describe_binary(name, path)
    with _pins_lock(root):
        pins = load_pins(root)
        was = pins["bins"].get(name)
        pins["history"].append({"name": name, "was": was, "at": utc_now(), "note": note})
        pins["bins"][name] = now
        write_json(_pins_path(root), pins)
    return {"name": name, "was": was, "now": now}


# ---------------------------------------------------------------- cli


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-claim", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=absolute, required=True, help="the program's loop root")
    p.add_argument("--pid", type=int, help="the manager's pid (default: the nearest ancestor named by --agent)")
    p.add_argument("--agent", default="claude", help="program name of the manager process (default: claude)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("claim", help="take the program, or say who holds it")
    s.add_argument("--pane", default=os.environ.get("HERDR_PANE_ID"))
    s.add_argument("--session")

    s = sub.add_parser("beat", help="prove the claim is still yours and stamp the heartbeat")
    s.add_argument("--note")

    s = sub.add_parser("release", help="give the program up")
    s.add_argument("--note", required=True)

    sub.add_parser("status", help="who holds the program, and the heartbeat's age")

    s = sub.add_parser("pin", help="record the binaries, the first time only")
    s.add_argument("--path", action="append", default=[], metavar="NAME=PATH")

    s = sub.add_parser("check", help="give the binaries' paths, accepting and recording any that updated")
    s.add_argument("--name", action="append", choices=list(PINNED))

    s = sub.add_parser("repin", help="pin a binary by hand, with the reason")
    s.add_argument("--name", required=True, choices=list(PINNED))
    s.add_argument("--note", required=True)
    s.add_argument("--path")
    return p


def _manager_pid(args, procs: Procs) -> int:
    if args.pid:
        return args.pid
    pid = find_manager_pid(procs, args.agent)
    if pid is None:
        raise Refusal(f"no ancestor process named {args.agent!r}; pass --pid")
    return pid


def main(argv: list[str] | None = None, procs: Procs | None = None) -> int:
    args = _parser().parse_args(argv)
    procs = procs or Procs()
    try:
        if args.cmd == "claim":
            emit(claim(args.root, _manager_pid(args, procs), args.pane, args.session, procs))
        elif args.cmd == "beat":
            stamped = beat(args.root, _manager_pid(args, procs), procs, args.note)
            # A seat move or pause reaches a manager in a long turn at its next save, as well as at its next wait.
            told = seat.notice_for(args.root)
            emit({**stamped, "seat": told} if told else stamped)
        elif args.cmd == "release":
            emit(release(args.root, _manager_pid(args, procs), procs, args.note))
        elif args.cmd == "status":
            emit(status(args.root, procs))
        elif args.cmd == "pin":
            overrides = {}
            for pair in args.path:
                name, sep, path = pair.partition("=")
                if not sep or name not in PINNED:
                    raise Refusal(f"--path takes NAME=PATH with NAME one of {', '.join(PINNED)}, got {pair!r}")
                overrides[name] = path
            emit(pin(args.root, overrides))
        elif args.cmd == "check":
            emit(check_pins(args.root, tuple(args.name) if args.name else PINNED))
        elif args.cmd == "repin":
            emit(repin(args.root, args.name, args.note, args.path))
    except Refusal as refusal:
        emit({"refused": refusal.reasons})
        print(f"hl-claim: refused: {refusal}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
