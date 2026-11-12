"""Keep one manager alive for a program. No model runs here.

    tick     one look, run by launchd every five minutes
    start    start a manager now: a program's first, or one started by hand
    arm      a manager should exist: start one whenever there is none
    disarm   stop starting managers (the program is complete, or paused)
    status   what is recorded

A tick starts a manager when the watchdog is armed and

    nobody holds the program (never started, or the last manager released it
    to be replaced by a fresh one),
    the holder's process is gone, or
    the holder is alive but has not stamped its heartbeat for 45 minutes.

The heartbeat is written by the manager's own turn (`hl-claim beat`), so a
manager that is alive and wedged does not look healthy. A stuck manager's
pane is closed first, unless a merge is running under this root: `hl-merge`
is left to finish and the next tick looks again. A manager stopped on a
usage limit is not replaced before the limit resets; a new session would
stop on the same limit.

Before any of that, a tick looks at the program's Claude seats (`seat.py`).
When the seat in use is full and another is open, the program moves there:
the next manager and every Claude worker started from then on come up on the
new seat, and the manager in charge hands over when `hl-wait` or `hl-flow
save` tells it. A manager stopped on a usage limit of a seat the program has
left is replaced at once. When every seat is full, the program pauses: the
manager saves and gives the program up, and no manager is started until the
first seat resets, when one is started on it.

Three starts inside an hour means starting is not what is wrong. The
watchdog then halts, tells the operator, and stays halted until `arm`.

A planned hand-over is not one of those three. A manager that released its
claim at an item boundary asked to be replaced, and how often that happens
follows from how much work an hour holds, so hand-overs are not counted
against the hour. They have a brake of their own: three managers in a row
that hand over without anything in the run directories having moved are a
manager that is not working, and that halts the watchdog too. A manager that
was started and never claimed is not a hand-over, whatever the log's last
line says: it is a start that failed.

The manager is one tab of a workspace. Its replacement opens as another tab
of the same workspace, so the manager stays where the operator put it; when
that workspace is gone, it goes to the workspace labeled with the program's
name. Workers never share the manager's workspace: each run has its own.

This is the one script that drives herdr from outside a pane: launchd has
no pane to run it in.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import claim, herdr as herdr_mod, program, seat, secrets as keeper, spawn, wait
from .common import Refusal, absolute, emit, read_json, utc_now, write_json
from .herdr import Herdr, HerdrError

STALE_S = 45 * 60
MAX_STARTS = 3
WINDOW_S = 60 * 60
# Managers in a row that may hand over with nothing moved before the watchdog stops replacing them.
MAX_IDLE_HANDOVERS = 3
HANDOVER_REASON = "the last manager handed over"
TICK_LOCK_STALE_S = 15 * 60


class System:
    """What a tick reads from, and says to, the machine."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def merge_running(self, root: str) -> bool:
        out = subprocess.run([claim.PS, "-axo", "command="], capture_output=True, text=True).stdout
        return any("hl-merge" in line and root in line for line in out.splitlines())

    def secret(self, ref: str, reader: list[str] | None = None) -> tuple[str | None, str | None]:
        """One read with the program's reader (`secrets_reader`), or with the
        kit's `hl-read-secret` when it names none: the value, or None and the
        reader's first line on stderr. Never logged."""
        return keeper.read_secret(ref, reader)

    def password(self, cfg: dict) -> tuple[str | None, str | None]:
        """The ntfy password, or None and why there is none."""
        ref = cfg.get("ntfy_password_ref")
        if not ref:
            return None, "program.json names no ntfy_password_ref"
        try:
            reader = keeper.configured_reader(cfg)
            if keeper.in_use(cfg):
                return keeper.get(cfg["root"], "ntfy"), None
        except keeper.Unreachable:
            # No keeper could be reached or started, and nothing was sent to one, so nothing has been asked of
            # the store: read it here, as this script always did. A notice that a manager was replaced does not
            # depend on another process being there.
            pass
        except Refusal as refusal:
            # The keeper asked and the store said no, or a keeper took the request and did not answer. Asking
            # from here as well could spend a second request on the same answer.
            return None, str(refusal)
        value, said = self.secret(ref, reader)
        return (value, None) if value else (None, f"the reader did not give the ntfy password ({ref}): {said or 'it said nothing'}")

    def notify(self, cfg: dict, title: str, body: str) -> bool:
        """Tell the operator. Never raises: a tick goes on whether or not it
        could, and the log says why it could not."""
        if not cfg.get("ntfy_url"):
            return False
        password = None
        try:
            password, why = self.password(cfg)
            if password is not None:
                why = keeper.post_notice(cfg, password, title, body)
        except Exception as broke:
            # Whatever it was, the tick that called this still has its state to write.
            why = f"{type(broke).__name__}: {broke}"
            if password:
                why = why.replace(password, "[withheld]")
        if why and cfg.get("root"):
            _log(cfg["root"], f"not notified ({title}): {why}")
        return why is None


def _state_path(root) -> Path:
    return Path(root) / "watchdog.json"


def load_state(root) -> dict:
    path = _state_path(root)
    base = {"armed": False, "halted": None, "starts": [], "handovers": [], "launched": 0, "manager": None, "told": {}}
    return {**base, **read_json(path)} if path.exists() else base


def _log(root, line: str) -> None:
    try:
        with open(Path(root) / "watchdog.log", "a") as fh:
            fh.write(f"{utc_now()} {line}\n")
    except OSError:
        pass


def progress_mark(root) -> str:
    """A fingerprint of what a manager moves when it gets work done: every
    item's status and deploy check, the phases it has started that still
    count, and the done files its workers wrote. Two marks that are equal
    mean nothing moved.

    An attempt that was voided is left out: a manager whose every worker
    dies at once starts one, voids it and hands over, and that is not work.
    A worker that is still building moves nothing here either. That is
    meant: a hand-over belongs between items, and three managers in a row
    that came and went during one build did nothing but wait."""
    digest = hashlib.sha256()
    for path in sorted(Path(root).glob("runs/*/items/*/item.json")):
        d = path.parent
        try:
            item = read_json(path)
            log = read_json(d / "attempts.json").get("log", [])
        except (OSError, ValueError):
            continue
        counted = sum(1 for entry in log if not entry.get("voided"))
        done = len(list(d.glob("*.done"))) + len(list(d.glob("*.done.refused-*")))
        verify = (item.get("fields") or {}).get("verify")
        digest.update(f"{d.parent.parent.name}/{d.name}:{item.get('status')}:{verify}:{counted}:{done}\n".encode())
    return digest.hexdigest()[:16]


def planned_handover(root, state: dict) -> dict | None:
    """The release that asks for this start, when the program's last event
    is one and it came from a manager that held the program after the last
    start made here. A manager this watchdog started that never claimed left
    the older release as the log's last line; that is a failed start."""
    event = claim.last_event(root)
    if not event or not str(event.get("event", "")).startswith("released"):
        return None
    started = (state.get("manager") or {}).get("started_at")
    if started and not str(event.get("at", "")) > started:
        return None
    return event


def idle_handovers(state: dict, mark: str) -> int:
    """How many hand-overs in a row, counting the one about to be made, began
    with the run directories exactly as the hand-over before found them."""
    idle = 0
    previous = mark
    for entry in reversed(state.get("handovers") or []):
        if entry.get("progress") != previous:
            break
        idle += 1
        previous = entry.get("progress")
    return idle


def _manager_limit(state: dict, now: float) -> dict | None:
    """The usage limit the manager this watchdog started is stopped on, while it has not reset."""
    session = (state.get("manager") or {}).get("session")
    transcript = herdr_mod.claude_transcript(session) if session else None
    limit = wait.usage_limit(transcript) if transcript else None
    if not limit or not limit.get("resets_at"):
        return None
    resets = datetime.strptime(limit["resets_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    return limit if resets > now else None


def _refused_seats(state: dict, now: float) -> dict:
    """The seat the last manager started here is stopped on a usage limit
    of, with the limit's reset: that seat is full whatever its reading says."""
    home = (state.get("manager") or {}).get("home")
    limit = _manager_limit(state, now) if home else None
    return {home: _epoch(limit["resets_at"])} if limit else {}


def _stranded(cfg: dict, state: dict, now: float) -> str | None:
    """Why the last manager started here must be replaced now: it is stopped
    on a usage limit of a seat the program has since left."""
    home = (state.get("manager") or {}).get("home")
    here = program.claude_home(cfg)
    if not home or not here or home == here:
        return None
    limit = _manager_limit(state, now)
    if not limit:
        return None
    return f"the manager stopped on a usage limit of {seat.name(home)} (it resets {limit['resets_at']}); the program is on {seat.name(here)} now"


def _seat_step(cfg: dict, state: dict, system: System, now: float) -> dict:
    """Move, pause or resume the program's Claude seat, and say so once."""
    for home in program.claude_seats(cfg):
        seat.prune(home, now)
    decision = seat.step(cfg, now, _refused_seats(state, now))
    action = decision["action"]
    if action in ("move", "pause", "resume"):
        decision["why"] = seat.explain(decision)
        seat.apply(cfg, decision, now)
        _log(cfg["root"], f"seat {action}: {decision['why']}")
        title = {
            "move": "moved to another Claude seat",
            "pause": "paused: every Claude seat is full",
            "resume": "resumed: a Claude seat has reset",
        }[action]
        system.notify(cfg, f"{cfg['program']}: {title}", decision["why"])
    return decision


def _close_stuck(herdr, holder: dict) -> str:
    """Close the stuck manager's pane, after checking the pane still runs that manager."""
    pane = holder.get("pane")
    if not pane:
        return "the claim names no pane; the old manager was left running"
    try:
        info = herdr.call("pane", "process-info", "--pane", pane)["process_info"]
    except HerdrError as error:
        return f"could not read pane {pane}: {error.code}"
    if holder["pid"] not in [p.get("pid") for p in info.get("foreground_processes", [])]:
        return f"pane {pane} no longer runs pid {holder['pid']}; left alone"
    herdr.call("pane", "close", pane)
    return f"closed pane {pane}"


def _close_previous(herdr, state: dict) -> None:
    """Close the pane of the last manager this watchdog started, if it still
    holds the shell that was started in it. A dead manager leaves its pane."""
    last = state.get("manager") or {}
    if not last.get("pane"):
        return
    try:
        info = herdr.call("pane", "process-info", "--pane", last["pane"])["process_info"]
        if info.get("shell_pid") == last.get("shell_pid"):
            herdr.call("pane", "close", last["pane"])
    except HerdrError:
        pass


def _still_names(herdr, pane, shell_pid=None, pid=None) -> bool:
    """Whether a pane id still names the pane a manager ran in. Pane ids are
    reused, so the id alone says nothing: the pane must hold the same shell,
    or be running the manager's own process."""
    if not pane:
        return False
    try:
        info = herdr.call("pane", "process-info", "--pane", pane)["process_info"]
    except HerdrError:
        return False
    if shell_pid is not None and info.get("shell_pid") == shell_pid:
        return True
    return pid is not None and pid in [p.get("pid") for p in info.get("foreground_processes", [])]


def manager_pane(herdr, cfg: dict, holder: dict | None, alive: bool, state: dict) -> str:
    """A pane for the next manager, opened before the old one's is closed so
    the workspace they share never empties."""
    last = state.get("manager") or {}
    known = []
    # A manager that recycled itself released its claim; the claims log still says where it ran.
    ran = holder or claim.last_holder(cfg["root"])
    if ran and _still_names(herdr, ran.get("pane"), ran.get("parent_pid"), ran["pid"] if alive else None):
        known.append(ran["pane"])
    if _still_names(herdr, last.get("pane"), last.get("shell_pid")):
        known.append(last["pane"])
    listed = herdr.workspaces()
    ids = {w.get("workspace_id") for w in listed}
    workspace = next((p.split(":")[0] for p in known if p.split(":")[0] in ids), None)
    workspace = workspace or next((w["workspace_id"] for w in listed if w.get("label") == cfg["program"]), None)
    if workspace is None:
        # A workspace opens with one pane; the manager takes it.
        return herdr.create_workspace(cfg["program"], cfg["root"])["pane"]
    label = f"{cfg['program']} manager"
    made = herdr.call("tab", "create", "--workspace", workspace, "--cwd", cfg["root"], "--label", label, "--no-focus")
    return made["root_pane"]["pane_id"]


def launch(cfg: dict, state: dict, herdr, system: System, reason: str, pane: str, handover: str | None = None) -> dict:
    """`handover` is the progress mark of a planned hand-over: such a start
    is written down as one, and not as one of the hour's three."""
    # The program's Claude seat (`program.claude_home`), read at each start so a move reaches the next
    # manager; first, so that a sign-in that is not there leaves the previous manager's pane as it was.
    spawn.set_claude_home(herdr, pane, cfg)
    _close_previous(herdr, state)
    state["launched"] += 1
    name = f"{spawn.program_slug(cfg['program'])}-mgr-{state['launched']}"
    session = str(uuid.uuid4())
    root = cfg["root"]
    # The operator's own launch line for a manager, plus a session id so its transcript can be found.
    argv = ["--dangerously-skip-permissions", "--model", cfg["manager_model"], "--effort", cfg["manager_effort"]]
    argv += ["--session-id", session]
    # What the program adds: by default, only the tools a manager uses and none of the host's MCP servers or
    # skills, which were four fifths of the 50k tokens every request of the first program's manager began with.
    argv += [str(arg).replace("{{KIT}}", cfg["kit"]) for arg in cfg.get("manager_args") or []]
    spawn._start_agent(herdr, name, "claude", pane, argv, system.sleep)
    shell_pid = herdr.call("pane", "process-info", "--pane", pane)["process_info"].get("shell_pid")
    prompt = f"Read {Path(root) / cfg['manager_prompt']} and follow it. You were started by hl-watchdog: {reason}."
    unsent = spawn.submit(herdr, name, prompt[: spawn.MAX_PROMPT], system.sleep, session)
    if unsent:
        # A session that never read its prompt is not a manager. Whoever called closes its pane.
        raise Refusal(f"{name} was started and did not take its prompt: {unsent}")
    # The seat it came up on, so a manager left behind by a move is known for one (`_stranded`).
    home = program.claude_home(cfg)
    manager = {"agent": name, "pane": pane, "shell_pid": shell_pid, "session": session, "started_at": utc_now(), "reason": reason, "home": home}
    if handover is None:
        state["starts"].append(int(system.now()))
    else:
        state["handovers"] = (state.get("handovers") or [])[-20:] + [{"at": int(system.now()), "progress": handover}]
    state["manager"] = manager
    return manager


def tick(root, herdr, system: System, procs: claim.Procs | None = None) -> dict:
    cfg = program.load(root)
    root = cfg["root"]
    procs = procs or claim.Procs()
    state = load_state(root)
    now = system.now()

    def done(action: str, **detail) -> dict:
        write_json(_state_path(root), state)
        _log(root, f"{action} {detail or ''}".strip())
        return {"action": action, **detail}

    if not state["armed"]:
        return done("disarmed")
    if state["halted"]:
        return done("halted", reason=state["halted"])

    seated = _seat_step(cfg, state, system, now)
    if seated["action"] in ("pause", "paused"):
        # Whatever holds the program: a manager saving its work is left to it, and none is started.
        return done("seat-paused", until=seat.stamp(seated["until"]))

    held = claim.status(root, procs)
    holder, alive = held["claim"], held["alive"]
    beat_age = now - held["heartbeat"]["epoch"] if held["heartbeat"] else None
    claimed_age = now - _epoch(holder["claimed_at"]) if holder else None
    if alive:
        # A manager that has not saved yet is judged from when it claimed.
        quiet_for = min(a for a in (beat_age, claimed_age) if a is not None)
        stranded = _stranded(cfg, state, now)
        if quiet_for <= STALE_S and not stranded:
            return done("healthy", pid=holder["pid"], quiet_min=int(quiet_for // 60))
        if system.merge_running(root):
            return done("merge-running", pid=holder["pid"])
        if stranded:
            reason = stranded
        else:
            limit = _manager_limit(state, now)
            if limit:
                if state["told"].get("limit") != limit["id"]:
                    state["told"]["limit"] = limit["id"]
                    system.notify(cfg, f"{cfg['program']}: manager is on a usage limit", f"It resets at {limit['resets_at']}. Nothing is started before then.")
                return done("limit", resets_at=limit["resets_at"])
            reason = f"the manager (pid {holder['pid']}) saved nothing for {int(quiet_for // 60)} minutes"
    elif holder:
        reason = f"the manager (pid {holder['pid']}) is gone"
    else:
        reason = "no manager holds the program"

    def halt(why: str) -> dict:
        state["halted"] = why
        system.notify(cfg, f"{cfg['program']}: watchdog halted", why + ". Run `hl-watchdog arm` after looking.")
        return done("halted", reason=why)

    # Starts that failed count against the hour whatever asked for them.
    state["starts"] = [t for t in state["starts"] if now - t < WINDOW_S]
    if len(state["starts"]) >= MAX_STARTS:
        return halt(f"{MAX_STARTS} managers were started within an hour and none stayed healthy; last: {reason}")
    mark = None
    released = None if holder else planned_handover(root, state)
    if released:
        note = str(released["event"]).partition(":")[2].strip()
        if note.startswith("seat"):
            # Given up for a seat move or a pause: the seats asked for it, not a manager that does no work. A
            # mark of its own breaks the run of idle hand-overs.
            mark = f"seat:{int(now)}"
        else:
            mark = progress_mark(root)
            if idle_handovers(state, mark) >= MAX_IDLE_HANDOVERS:
                return halt(f"{MAX_IDLE_HANDOVERS} managers in a row handed over and nothing in the run directories moved between them")
        reason = HANDOVER_REASON + (f" ({note})" if note else "")
    if seated["action"] == "resume":
        reason = f"{seated['why']}; {reason}"

    closed = pane = None
    try:
        pane = manager_pane(herdr, cfg, holder, alive, state)
        closed = _close_stuck(herdr, holder) if alive else None
        manager = launch(cfg, state, herdr, system, reason, pane, handover=mark)
    except (Refusal, HerdrError) as failure:
        if pane:
            try:
                herdr.call("pane", "close", pane)
            except HerdrError:
                pass
        state["starts"].append(int(now))
        return done("start-failed", reason=reason, detail=str(failure), closed=closed)
    title = "manager handed over" if released else "manager started"
    system.notify(cfg, f"{cfg['program']}: {title}", f"{reason}. New manager {manager['agent']} in pane {manager['pane']}.")
    return done("started", closed=closed, handover=bool(released), **manager)


def start_now(root, herdr, system: System, note: str, procs: claim.Procs | None = None) -> dict:
    """Start a manager without waiting for a tick. This is how a program's
    first manager is started; it then claims the program, pins the binaries
    and arms the watchdog itself."""
    cfg = program.load(root)
    root = cfg["root"]
    state = load_state(root)
    held = claim.status(root, procs or claim.Procs())
    if held["alive"]:
        holder = held["claim"]
        raise Refusal(f"a manager already holds this program (pid {holder['pid']}, pane {holder.get('pane')}); nothing was started")
    if not (Path(root) / cfg["manager_prompt"]).is_file():
        raise Refusal(f"{Path(root) / cfg['manager_prompt']} does not exist: stage the program first")
    pane = None
    try:
        pane = manager_pane(herdr, cfg, held["claim"], False, state)
        manager = launch(cfg, state, herdr, system, note, pane)
    except (Refusal, HerdrError):
        if pane:
            try:
                herdr.call("pane", "close", pane)
            except HerdrError:
                pass
        raise
    write_json(_state_path(root), state)
    _log(root, f"started by hand: {manager}")
    return {"action": "started", **manager}


def _epoch(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def set_armed(root, armed: bool, note: str) -> dict:
    program.load(root)
    if armed:
        claim.load_pins(root)
    state = load_state(root)
    state.update(armed=armed, halted=None, starts=[] if armed else state["starts"])
    if armed:
        state["handovers"] = []
    write_json(_state_path(root), state)
    _log(root, f"{'armed' if armed else 'disarmed'}: {note}")
    return {"armed": armed}


def _tick_locked(root, herdr, system: System) -> dict:
    """One tick at a time: a tick can outlast the five minutes between them."""
    lock = Path(root) / "watchdog.lock.d"
    try:
        os.mkdir(lock)
    except FileExistsError:
        if system.now() - lock.stat().st_mtime < TICK_LOCK_STALE_S:
            return {"action": "another-tick-running"}
        os.rmdir(lock)
        os.mkdir(lock)
    try:
        return tick(root, herdr, system)
    finally:
        os.rmdir(lock)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-watchdog", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=absolute, required=True, help="the program's loop root")
    p.add_argument("--herdr", help="absolute path of the herdr binary (default: the pinned one)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick", help="one look; start a manager if one is needed")
    s = sub.add_parser("start", help="start a manager now, unless a live one holds the program")
    s.add_argument("--note", required=True, help="why, in a few words; the manager is told")
    s = sub.add_parser("arm", help="start a manager whenever there is none")
    s.add_argument("--note", required=True)
    s = sub.add_parser("disarm", help="stop starting managers")
    s.add_argument("--note", required=True)
    sub.add_parser("status", help="what is recorded")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "arm":
            emit(set_armed(args.root, True, args.note))
        elif args.cmd == "disarm":
            emit(set_armed(args.root, False, args.note))
        elif args.cmd == "status":
            emit({"watchdog": load_state(args.root), "program": claim.status(args.root, claim.Procs()), "seat": seat.status(program.load(args.root), time.time())})
        elif args.cmd == "start":
            # A program's first manager is started before anything is pinned; after that the pinned file is the one used.
            pinned = (Path(args.root) / "pins.json").exists()
            binary = claim.check_pins(args.root, ("herdr", "claude"), on_path=False)["bins"]["herdr"] if pinned else args.herdr
            emit(start_now(args.root, Herdr(binary), System(), args.note))
        else:
            state = load_state(args.root)
            if not state["armed"] or state["halted"]:
                # Nothing to do, and nothing to check: a tick every five minutes must stay quiet.
                emit({"action": "halted" if state["armed"] else "disarmed"})
                return 0
            # launchd's PATH is not a pane's, so only the pinned files themselves are checked.
            checked = claim.check_pins(args.root, ("herdr", "claude"), on_path=False)
            for update in checked.get("updated", []):
                _log(args.root, f"accepted an update of {update['name']}: {update['why']}" + (f" ({update['version']})" if update["version"] else ""))
            emit(_tick_locked(args.root, Herdr(args.herdr or checked["bins"]["herdr"]), System()))
    except (Refusal, HerdrError) as failure:
        reasons = failure.reasons if isinstance(failure, Refusal) else [str(failure)]
        emit({"refused": reasons})
        print(f"hl-watchdog: refused: {'; '.join(reasons)}", file=sys.stderr)
        _log(args.root, f"refused: {'; '.join(reasons)}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
