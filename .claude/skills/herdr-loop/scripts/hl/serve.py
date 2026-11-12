"""Run a build for a grade, and stop it afterwards.

    start   read the builder's Serve and Ready lines, check the item's port
            is free, run the Serve command in a pane of its own, and wait
            until Ready answers
    stop    close that pane and prove the port is free and the processes gone
    status  what is recorded, and what the port looks like now

Where the repository's spec names a `reference` directory (the pages a look
is compared with, such as `brand`), the build of an item a person looks at
is served with that directory of its checkout beside it, on a free port,
by a static server in a second pane: the browser tool refuses `file://`,
so a reviewer could otherwise compare the build only with the reference's
source (an item of an app rebrand program). `stop` stops both. A reference that
does not come up is noted and the build is still served: the review is
then of the source, as before.

The manager serves the build; the evaluator only drives it. The Serve line
is written to a file and run with `/bin/sh`, because it was written for `sh`
and a pane's shell is zsh. The pane is a tab in the run's workspace, beside
the evaluator that drives the build.

A port that is already in use stops the start before anything is run. This
script never kills a process it did not start: after the pane is closed, a
process is signalled only if it is one recorded at the start and still has
the start time recorded then.

Exit codes: 0 done, 2 refused (among them: the port is taken), 3 the build
did not become ready.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import claim, contractfmt, items, program, spawn
from .common import Refusal, absolute, emit, read_json, utc_now, write_json
from .herdr import Herdr, HerdrError

LSOF = "/usr/sbin/lsof"
READY_TIMEOUT_S = 180
STOP_WAIT_S = 15
# A static server over a directory answers at once; one that does not within this is not coming.
REFERENCE_TIMEOUT_S = 30


class Host:
    """The machine, as far as serving needs it."""

    procs = claim.Procs()

    def listeners(self, port: int) -> list[int]:
        out = subprocess.run([LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True).stdout
        return sorted({int(line) for line in out.split() if line.isdigit()})

    def start_time(self, pid: int) -> str | None:
        return self.procs.start(pid)

    def command(self, pid: int) -> str:
        found = self.procs.parent(pid)
        return found[1] if found else ""

    def kill(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass

    def url_status(self, url: str) -> int | None:
        """The HTTP status a URL answers with, or None while nothing answers."""
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            error.close()
            return error.code
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def run(self, command: str, cwd: str, timeout: float = 120) -> int:
        try:
            return subprocess.run(["/bin/sh", "-c", command], cwd=cwd, capture_output=True, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            return 124

    def free_port(self) -> int:
        """A port nothing listens on now, as the system hands one out."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def record_path(run_dir, item_id: str, n: int) -> Path:
    return items.item_dir(run_dir, item_id) / f"serve-{n}.json"


def _ready(host: Host, how: dict, cwd: str) -> str | None:
    """What answered, or None while the build is not up."""
    if how["ready_kind"] == "URL":
        status = host.url_status(how["ready"])
        return f"answered {status}" if status is not None and status < 500 else None
    return "exited 0" if host.run(how["ready"], cwd) == 0 else None


def _shown(command: str, width: int = 500) -> str:
    """A command line as it is, unless it is very long: then its two ends.
    The start alone can be all interpreter path (120 characters of it on this
    host), and it is the arguments that say what the process is."""
    half = (width - 3) // 2
    return command if len(command) <= width else f"{command[:half]} … {command[-half:]}"


def _build(run_dir, item_id: str) -> tuple[dict, int, dict, Path]:
    item = items.load_item(run_dir, item_id)
    n = items.load_attempts(run_dir, item_id)["build"]
    d = items.item_dir(run_dir, item_id)
    if item["status"] != "in_progress" or n == 0:
        raise Refusal(f"{item_id} is {item['status']} with {n} builds; there is nothing to serve")
    report = d / f"build-{n}.md"
    if not report.exists():
        raise Refusal(f"{report.name} does not exist")
    try:
        how = contractfmt.how_to_run(report.read_text())
    except contractfmt.ContractError as exc:
        raise Refusal(f"{report.name}: {exc}") from exc
    return item, n, how, d


def start(root, run_dir, item_id: str, herdr, host: Host, workspace: str | None = None, timeout_s=READY_TIMEOUT_S):
    cfg = program.load(root)
    item, n, how, d = _build(run_dir, item_id)
    path = record_path(run_dir, item_id, n)
    if path.exists() and not read_json(path).get("stopped_at"):
        raise Refusal(f"build {n} of {item_id} is already being served; run `hl-serve stop` first")
    worktree = item["fields"].get("worktree")
    if not worktree or not Path(worktree).is_dir():
        raise Refusal(f"{item_id} has no worktree recorded (`hl-items set --field worktree=<path>`)")
    record = {"build": n, "command": how["serve"], "ready_kind": how["ready_kind"], "ready": how["ready"]}
    began = host.now()

    if how["serve"].strip() == "true":
        answer = _ready(host, how, worktree)
        record.update(served=False, started_at=utc_now(), stopped_at=utc_now())
        if answer is None:
            record["summary"] = None
            write_json(path, record)
            return 3, {"result": "not-ready", **record, "detail": f"`{how['ready']}` did not succeed in {worktree}"}
        record["summary"] = f"Nothing is served for this item. `{how['ready']}` {answer} in the worktree at {utc_now()}."
        write_json(path, record)
        return 0, {"result": "ready", **record}

    port = item["fields"].get("port")
    if not port or not str(port).isdigit():
        raise Refusal(f"{item_id} has no port recorded (`hl-items set --field port=<number>`)")
    port = int(port)
    holders = host.listeners(port)
    if holders:
        who = "; ".join(f"pid {pid}: {_shown(host.command(pid))}" for pid in holders)
        raise Refusal(f"port {port} is already in use ({who}). The build was not started and nothing was killed.")

    script = d / f"serve-{n}.sh"
    script.write_text(f"#!/bin/sh\ncd {_sh(worktree)} || exit 97\n{how['serve']}\n")
    workspace = workspace or spawn.run_workspace(herdr, cfg, run_dir)
    made = herdr.call("tab", "create", "--workspace", workspace, "--cwd", worktree, "--label", f"{item_id} serve", "--no-focus")
    pane = made["root_pane"]["pane_id"]
    info = herdr.call("pane", "process-info", "--pane", pane)["process_info"]
    record.update(served=True, port=port, pane=pane, shell_pid=info.get("shell_pid"), script=str(script), started_at=utc_now())
    record.update(stopped_at=None, summary=None, pids={})
    write_json(path, record)
    herdr.call("pane", "run", pane, f"/bin/sh {_sh(str(script))}")

    answer = None
    while host.now() - began < timeout_s:
        answer = _ready(host, how, worktree)
        if answer:
            break
        host.sleep(2)
    # Recorded whether or not it came up: these are the only processes `stop` may signal.
    record["pids"] = {str(pid): host.start_time(pid) for pid in host.listeners(port)}
    if answer is None:
        try:
            (d / f"serve-{n}.log").write_text(herdr.text("pane", "read", pane, "--source", "recent-unwrapped", "--lines", "60"))
        except HerdrError:
            pass
        write_json(path, record)
        stopped = stop(run_dir, item_id, herdr, host)
        detail = f"`{how['ready']}` did not answer within {int(timeout_s)} s; the pane's last lines are in serve-{n}.log"
        return 3, {"result": "not-ready", **read_json(path), "detail": detail, "stop": stopped}
    waited = int(host.now() - began)
    record["summary"] = f"`{how['ready']}` {answer} {waited} s after the build was started on port {port}, at {utc_now()}."
    write_json(path, record)
    # What it did is in the record at `path` as it is done (`reference`), so the answer is read back from there.
    _serve_reference(cfg, items.load_run(run_dir), item, d, n, worktree, herdr, host, workspace, path)
    return 0, {"result": "ready", **read_json(path)}


def reference_dir(cfg: dict, run: dict, item: dict) -> str | None:
    """The directory of the checkout to serve beside an item's build: the
    repository's `reference` fact, for an item routed with a look or whose
    floor asks for one. None otherwise, which is the kit as it was."""
    root = (program.repo_facts(cfg, run["repo"]).get("reference") or "").strip().strip("/")
    looked = (item.get("route") or {}).get("look") or "look" in (item.get("floor") or [])
    return root if root and looked else None


def _serve_reference(cfg, run, item, d: Path, n: int, worktree, herdr, host: Host, workspace, path: Path, timeout_s=REFERENCE_TIMEOUT_S) -> dict | None:
    """Serve the reference beside the build, and write what was done into
    the serve record at `path` as it is done: the pane, its shell and the
    port are on record before the pane runs anything, so a start interrupted
    while it waits leaves a server that `stop` knows to close."""
    rel = reference_dir(cfg, run, item)
    if rel is None:
        return None

    def note(ref: dict) -> dict:
        write_json(path, {**read_json(path), "reference": ref})
        return ref

    root = Path(worktree) / rel
    if not root.is_dir():
        return note({"served": False, "root": rel, "note": f"`{rel}` is not a directory of this checkout, so no reference is served"})
    port = host.free_port()
    if host.listeners(port):
        return note({"served": False, "root": rel, "note": f"port {port} was taken as it was handed out, so no reference is served"})
    script = d / f"reference-{n}.sh"
    # The interpreter that runs this script: it is on the host for certain. Bound to the loopback, like the build.
    script.write_text(f"#!/bin/sh\ncd {_sh(str(root))} || exit 97\nexec {_sh(sys.executable)} -m http.server {port} --bind 127.0.0.1\n")
    made = herdr.call("tab", "create", "--workspace", workspace, "--cwd", str(root), "--label", f"{item['id']} reference", "--no-focus")
    pane = made["root_pane"]["pane_id"]
    info = herdr.call("pane", "process-info", "--pane", pane)["process_info"]
    ref = note({"served": True, "root": rel, "port": port, "url": f"http://127.0.0.1:{port}/", "pane": pane, "shell_pid": info.get("shell_pid"), "script": str(script), "pids": {}, "started_at": utc_now()})
    herdr.call("pane", "run", pane, f"/bin/sh {_sh(str(script))}")
    began, up = host.now(), False
    while host.now() - began < timeout_s:
        status = host.url_status(ref["url"])
        if status is not None and status < 500:
            up = True
            break
        host.sleep(1)
    # Recorded whether or not it came up, as for the build: these are the only processes `stop` may signal.
    ref = note({**ref, "pids": {str(pid): host.start_time(pid) for pid in host.listeners(port)}})
    if not up:
        try:
            notes = _stop_served(herdr, host, ref, f"the reference server of build {n} of {item['id']}")
        except (Refusal, HerdrError) as failure:
            return note({**ref, "note": f"`{rel}` did not answer on port {port} within {int(timeout_s)} s, and could not be stopped: {failure}"})
        return note({**ref, "served": False, "stopped_at": utc_now(), "stop_notes": notes, "note": f"`{rel}` did not answer on port {port} within {int(timeout_s)} s, so no reference is served"})
    return ref


def _sh(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def stop(run_dir, item_id: str, herdr, host: Host) -> dict:
    n = items.load_attempts(run_dir, item_id)["build"]
    path = record_path(run_dir, item_id, n)
    if not path.exists():
        raise Refusal(f"nothing was served for build {n} of {item_id}")
    record = read_json(path)
    if record.get("stopped_at"):
        return {"stopped": True, "already": True, "build": n}
    notes: list[str] = []
    failures: list[Refusal | HerdrError] = []
    # Each server is tried whatever became of the other, and each is written down as stopped on its own: a build whose
    # port a stranger holds does not leave the reference running, and the next stop tries only what is still up.
    if not record.get("build_stopped_at"):
        try:
            build_notes = _stop_served(herdr, host, record, f"build {n} of {item_id}")
        except (Refusal, HerdrError) as failure:
            failures.append(failure)
        else:
            record.update(build_stopped_at=utc_now(), build_stop_notes=build_notes)
            notes += build_notes
            write_json(path, record)
    reference = record.get("reference") or {}
    if reference.get("pane") and not reference.get("stopped_at"):
        # Stopped with the build, and proved stopped the same way: nothing is left serving once the review is over.
        try:
            reference_notes = _stop_served(herdr, host, reference, f"the reference server of build {n} of {item_id}")
        except (Refusal, HerdrError) as failure:
            failures.append(failure)
        else:
            record["reference"] = {**reference, "stopped_at": utc_now(), "stop_notes": reference_notes}
            notes += [f"reference: {note}" for note in reference_notes]
            write_json(path, record)
    if failures:
        if len(failures) == 1:
            raise failures[0]
        raise Refusal([str(failure) for failure in failures])
    record.update(stopped_at=utc_now(), stop_notes=notes)
    write_json(path, record)
    return {"stopped": True, "already": False, "build": n, "port": record["port"], "notes": notes}


def _stop_served(herdr, host: Host, served: dict, what: str) -> list[str]:
    """Close a served pane and prove its port free and its processes gone.
    `served` is what was recorded at the start: the pane, the shell it
    held, the port, and the processes listening then with their start
    times. Only those processes are ever signalled. Refused, with who holds
    the port, when it cannot be proved stopped."""
    notes = []
    pane, port = served["pane"], served["port"]
    try:
        info = herdr.call("pane", "process-info", "--pane", pane)["process_info"]
    except HerdrError as error:
        if not error.not_found:
            raise
        info = None
        notes.append("the pane was already gone")
    if info is not None and info.get("shell_pid") != served.get("shell_pid"):
        notes.append("the pane id now belongs to something else; left alone")
    elif info is not None:
        try:
            herdr.call("pane", "send-keys", pane, "ctrl+c")
        except HerdrError:
            pass
        herdr.call("pane", "close", pane)

    # Ours are the processes recorded at the start that still have that start time.
    ours = {int(pid): began for pid, began in (served.get("pids") or {}).items()}

    def alive() -> list[int]:
        return [pid for pid, began in ours.items() if began and host.start_time(pid) == began]

    def settle(seconds: float) -> bool:
        deadline = host.now() + seconds
        while host.now() < deadline:
            if not alive() and not host.listeners(port):
                return True
            host.sleep(1)
        return not alive() and not host.listeners(port)

    if not settle(STOP_WAIT_S):
        for sig, name in ((signal.SIGTERM, "TERM"), (signal.SIGKILL, "KILL")):
            survivors = alive()
            if not survivors:
                break
            for pid in survivors:
                host.kill(pid, sig)
            notes.append(f"sent {name} to {survivors}")
            if settle(5):
                break
    holders = host.listeners(port)
    if alive() or holders:
        who = "; ".join(f"pid {pid}: {_shown(host.command(pid))}" for pid in holders) or "none"
        raise Refusal(
            f"{what} is not fully stopped: recorded processes alive {alive()}, port {port} held by {who}. "
            "A holder that is not a recorded process is not this loop's to kill."
        )
    return notes


def status(run_dir, item_id: str, host: Host) -> dict:
    n = items.load_attempts(run_dir, item_id)["build"]
    path = record_path(run_dir, item_id, n)
    record = read_json(path) if path.exists() else None
    out = {"build": n, "record": record}
    if record and record.get("port"):
        out["listening_now"] = host.listeners(record["port"])
    if record and (record.get("reference") or {}).get("port"):
        out["reference_listening_now"] = host.listeners(record["reference"]["port"])
    return out


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-serve", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=absolute, required=True, help="the program's loop root")
    p.add_argument("--run-dir", required=True, type=absolute)
    p.add_argument("--herdr", help="absolute path of the herdr binary (default: the pinned one)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="serve the item's latest build and wait until it is ready")
    s.add_argument("--id", required=True)
    s.add_argument("--workspace", help="herdr workspace id (default: the run's own, named for its work)")
    s.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_S)
    s = sub.add_parser("stop", help="stop the served build and prove it is gone")
    s.add_argument("--id", required=True)
    s = sub.add_parser("status", help="the serve record and who listens on the port now")
    s.add_argument("--id", required=True)
    return p


def main(argv: list[str] | None = None, herdr=None, host: Host | None = None) -> int:
    args = _parser().parse_args(argv)
    host = host or Host()
    try:
        if args.cmd == "status":
            emit(status(args.run_dir, args.id, host))
            return 0
        if herdr is None:
            if os.environ.get("HERDR_ENV") != "1":
                raise Refusal("not inside a Herdr pane (HERDR_ENV is not 1); builds are only served from inside Herdr")
            herdr = Herdr(args.herdr or claim.check_pins(args.root, ("herdr",))["bins"]["herdr"])
        if args.cmd == "start":
            code, result = start(args.root, args.run_dir, args.id, herdr, host, args.workspace, args.ready_timeout)
            emit(result)
            return code
        emit(stop(args.run_dir, args.id, herdr, host))
    except (Refusal, HerdrError) as failure:
        reasons = failure.reasons if isinstance(failure, Refusal) else [str(failure)]
        emit({"result": "refused", "refused": reasons})
        print(f"hl-serve: refused: {'; '.join(reasons)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
