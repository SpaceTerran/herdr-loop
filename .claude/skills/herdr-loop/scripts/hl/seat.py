"""Which Claude seat a program runs on, and when it moves or pauses.

    status   the seats' readings, the seat in use, and what the watchdog would do now

A seat is one Claude sign-in on the host: a configuration directory
(`~/.claude-<name>`) with usage windows of its own, five hours and a week.
A program starts on `claude_config_dir` and may run on any of `claude_seats`
(`program.claude_seats`). A seat is full when either window is at
`seat_full_percent` (99) or more and has not reset. At each tick the watchdog
decides:

    the program's seat is full, another is open   move there
    every seat is full                            pause until the first one resets
    paused, and a seat is open again              resume there

Only the watchdog writes the decision, to `<root>/seat.json`. Every Claude
session started afterwards comes up on the new seat, because
`program.claude_home` reads that file. A manager learns of a move or a pause
from `hl-wait` (event `seat`) and from the answer of `hl-flow save` (and of
`hl-claim beat`), and hands over or pauses (`references/troubles.md`,
"`seat`").

The readings come from the host's status line. Claude Code hands it the
account's usage windows each time it draws the line, in every interactive
session, as of that session's last request, and the line writes them to
`<seat>/hl-usage/<session>.json` (`references/host-facts.md`, "Seat
readings"). A line is drawn when the conversation changes and when a
window resets (leaving that window out until the next request), so a
working session keeps its seat's reading fresh and an idle one's goes stale. That is
why each session has a file of its own and the readings are merged: within
one window use only rises, so for each window the highest figure among the
readings of its latest reset time is the newest, and an idle session drawn
again never takes a seat back down. A window whose reset time has passed
counts as empty; otherwise a reading can only understate. A seat with no
reading is never moved to. A usage-limit refusal in the last manager's
transcript makes its seat full until the refusal's reset, whatever the
reading says.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import program
from .common import Refusal, absolute, emit, read_json, write_json

USAGE_DIR = "hl-usage"
STATE_FILE = "seat.json"
WINDOWS = (("five_hour", "5-hour window"), ("seven_day", "week"))
# A window counts as reset this long after the time it gives, so nothing starts in the old window's last seconds.
RESET_MARGIN_S = 120
# A full window that gives no reset time is looked at again after this long.
UNKNOWN_RESET_S = 60 * 60
# Two readings whose reset times are this close are of the same window.
SAME_WINDOW_S = 15 * 60
# A session's reading that gives no reset time is removed after this long.
STALE_READING_S = 8 * 24 * 60 * 60
HISTORY = 30


def stamp(epoch) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_path(root) -> Path:
    return Path(root) / STATE_FILE


def load_state(root) -> dict:
    try:
        return read_json(state_path(root)) or {}
    except (OSError, ValueError):
        return {}


def account(home) -> str | None:
    """The account signed in on a seat, as its `.claude.json` names it."""
    try:
        return ((read_json(Path(home) / ".claude.json") or {}).get("oauthAccount") or {}).get("emailAddress")
    except (OSError, ValueError, AttributeError):
        return None


def name(home) -> str:
    return account(home) or str(home)


def _records(home) -> list[dict]:
    """Each session's last reading of a seat, as the status line wrote it."""
    out = []
    for path in sorted((Path(home) / USAGE_DIR).glob("*.json")):
        try:
            data = read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("rate_limits"), dict):
            out.append(data)
    return out


def _figures(data: dict, key: str) -> tuple | None:
    """One reading's (used, resets_at) for a window, or None."""
    window = data["rate_limits"].get(key)
    if not isinstance(window, dict):
        return None
    try:
        used = float(window.get("used_percentage") or 0)
        resets = float(window["resets_at"]) if window.get("resets_at") is not None else None
    except (TypeError, ValueError):
        return None
    return used, resets


def _latest(records: list[dict], key: str) -> tuple | None:
    """A window's newest figure across the sessions' readings: the highest
    use among the readings of its latest reset time."""
    seen = [f for f in (_figures(data, key) for data in records) if f is not None]
    timed = [f for f in seen if f[1] is not None]
    if timed:
        latest = max(resets for _, resets in timed)
        same = [f for f in timed if latest - f[1] <= SAME_WINDOW_S]
        return max(used for used, _ in same), latest
    return (max(used for used, _ in seen), None) if seen else None


def reading(home: str, now: float, full_percent: float, refused: dict | None = None) -> dict:
    """What a seat's readings say now. `refused` maps a seat to the reset
    time of a usage-limit refusal a session on it is stopped on."""
    entry = {"home": home, "known": False, "full": False, "open_at": None, "observed_at": None, "windows": {}}
    records = _records(home)
    observed = [data["observed_at"] for data in records if isinstance(data.get("observed_at"), (int, float))]
    entry["observed_at"] = stamp(max(observed)) if observed else None
    windows: dict = {}
    for key, _ in WINDOWS:
        figures = _latest(records, key)
        if figures is None:
            continue
        used, resets = figures
        if resets is not None and now >= resets + RESET_MARGIN_S:
            used, resets = 0.0, None
        windows[key] = {"used": round(used, 1), "resets_at": resets, "full": used >= full_percent}
    limit_reset = (refused or {}).get(home)
    if limit_reset is not None and now < limit_reset + RESET_MARGIN_S:
        windows["refused"] = {"used": 100.0, "resets_at": float(limit_reset), "full": True}
    if not windows:
        return entry
    entry.update(known=True, windows=windows)
    full = [w for w in windows.values() if w["full"]]
    if full:
        resets = [w["resets_at"] for w in full]
        entry["full"] = True
        entry["open_at"] = max(resets) + RESET_MARGIN_S if None not in resets else now + UNKNOWN_RESET_S
    return entry


def prune(home, now: float) -> list[str]:
    """Remove the readings that say nothing any more: every window in them
    has reset. The watchdog calls this at each tick.

    When every reading with reset times has reset, the newest of them is
    kept: it is what says the seat is open. Without it a seat no session
    has drawn on since its reset has no reading, counts as unknown, and is
    never resumed or moved to (2026-11-06: a pause meant to end at 11:02Z
    ran 17 hours longer)."""
    readings = []
    for path in (Path(home) / USAGE_DIR).glob("*.json"):
        try:
            data = read_json(path)
            limits = data["rate_limits"] if isinstance(data, dict) and isinstance(data.get("rate_limits"), dict) else None
            figures = [_figures(data, key) for key, _ in WINDOWS] if limits else []
            resets = [f[1] for f in figures if f is not None and f[1] is not None]
            if resets:
                spent = now >= max(resets) + RESET_MARGIN_S
            else:
                spent = now - path.stat().st_mtime > STALE_READING_S
            observed = data.get("observed_at") if limits and isinstance(data.get("observed_at"), (int, float)) else path.stat().st_mtime
        except (OSError, ValueError):
            continue
        readings.append((path, spent, bool(resets), observed))
    timed = [r for r in readings if r[2]]
    keep = max(timed, key=lambda r: (r[3], r[0].name))[0] if timed and all(r[1] for r in timed) else None
    removed = []
    for path, spent, _, _ in readings:
        if not spent or path == keep:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path.name)
    return removed


def _room(entry: dict) -> tuple:
    windows = entry["windows"]
    return (windows.get("seven_day", {}).get("used", 0.0), windows.get("five_hour", {}).get("used", 0.0))


def _until(seats: list[dict], now: float) -> float:
    """When the first full seat opens again."""
    opens = [s["open_at"] for s in seats if s["known"] and s["full"] and s["open_at"] is not None]
    return min(opens) if opens else now + UNKNOWN_RESET_S


def step(cfg: dict, now: float, refused: dict | None = None) -> dict:
    """What the watchdog should do about the program's seat now. It reads
    and writes nothing but the readings: `apply` writes."""
    pool = program.claude_seats(cfg)
    if not pool:
        return {"action": "none", "seat": None, "seats": []}
    full_percent = float(cfg.get("seat_full_percent") or 99)
    here = program.claude_home(cfg)
    seats = [reading(home, now, full_percent, refused) for home in pool]
    current = next(s for s in seats if s["home"] == here)
    open_seats = sorted((s for s in seats if s["known"] and not s["full"]), key=lambda s: (_room(s), pool.index(s["home"])))
    base = {"seat": here, "seats": seats, "full_percent": full_percent}
    if load_state(cfg["root"]).get("paused_until") is not None:
        if open_seats:
            return {**base, "action": "resume", "to": open_seats[0]["home"]}
        return {**base, "action": "paused", "until": _until(seats, now)}
    if not current["full"]:
        return {**base, "action": "stay"}
    others = [s for s in open_seats if s["home"] != here]
    if others:
        return {**base, "action": "move", "from": here, "to": others[0]["home"]}
    return {**base, "action": "pause", "until": _until(seats, now)}


def _windows_text(entry: dict) -> str:
    if not entry["known"]:
        return "no reading"
    parts = [f"{label} {entry['windows'][key]['used']:g} %" for key, label in WINDOWS if key in entry["windows"]]
    if "refused" in entry["windows"]:
        parts.append(f"stopped on a usage limit until {stamp(entry['windows']['refused']['resets_at'])}")
    return ", ".join(parts)


def explain(decision: dict) -> str:
    """One sentence for the log, the operator's phone and the manager."""
    seats = {s["home"]: s for s in decision.get("seats", [])}
    action = decision["action"]
    if action == "move":
        old, new = seats[decision["from"]], seats[decision["to"]]
        return f"{name(old['home'])} is full ({_windows_text(old)}); the program moves to {name(new['home'])} ({_windows_text(new)})"
    if action in ("pause", "paused"):
        each = "; ".join(f"{name(s['home'])} {_windows_text(s)}" for s in decision["seats"])
        return f"every seat is full ({each}); the program is paused until {stamp(decision['until'])}, when the first one resets"
    if action == "resume":
        new = seats[decision["to"]]
        return f"{name(new['home'])} is open again ({_windows_text(new)}); the program resumes there"
    if action == "stay":
        here = seats[decision["seat"]]
        return f"{name(here['home'])} has room ({_windows_text(here)})"
    return "the program names no seat: it runs on the host's default sign-in"


def apply(cfg: dict, decision: dict, now: float) -> dict:
    """Write what `step` decided. Only the watchdog calls this."""
    root = cfg["root"]
    state = load_state(root)
    action = decision["action"]
    why = decision.setdefault("why", explain(decision))
    entry = {"at": stamp(now), "action": action, "why": why}
    if action in ("move", "resume"):
        state.update(home=decision["to"], since=stamp(now), paused_until=None, why=why)
        entry["to"] = decision["to"]
    elif action == "pause":
        state.update(paused_until=int(decision["until"]), why=why)
        entry["until"] = stamp(decision["until"])
    else:
        return state
    state["history"] = (state.get("history") or [])[-(HISTORY - 1):] + [entry]
    write_json(state_path(root), state)
    return state


def _same(a, b) -> bool:
    return os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(os.path.expanduser(str(b)))


def started_on(root) -> str | None:
    """The seat the watchdog started the program's last manager on, as its
    record (`watchdog.json`) says."""
    try:
        return ((read_json(Path(root) / "watchdog.json") or {}).get("manager") or {}).get("home")
    except (OSError, ValueError, AttributeError):
        return None


def notice(cfg: dict, own: str | None = None) -> dict | None:
    """What a manager must act on now, or None: the program is paused, or it
    has moved off the seat this manager runs on. That seat is the session's
    own `CLAUDE_CONFIG_DIR`, which its commands inherit; where a command
    does not have it, the seat the watchdog started the manager on."""
    state = load_state(cfg["root"])
    if state.get("paused_until") is not None:
        return {"action": "pause", "until": stamp(state["paused_until"]), "why": state.get("why")}
    own = own if own is not None else (os.environ.get("CLAUDE_CONFIG_DIR") or started_on(cfg["root"]))
    here = program.claude_home(cfg)
    if own and here and not _same(own, here):
        return {"action": "move", "from": own, "to": here, "why": state.get("why")}
    return None


def notice_for(root) -> dict | None:
    """`notice` for a loop root, or None where it has no program to read."""
    try:
        return notice(program.load(root))
    except (Refusal, OSError, ValueError):
        return None


def status(cfg: dict, now: float, full_percent=None) -> dict:
    """Everything `hl-seat status` shows. With `full_percent`, what the
    watchdog would decide at that threshold instead; nothing is written."""
    if full_percent is not None:
        cfg = {**cfg, "seat_full_percent": full_percent}
    decision = step(cfg, now)
    shown = []
    for s in decision["seats"]:
        row = {"seat": s["home"], "account": account(s["home"]), "read_at": s["observed_at"], "full": s["full"], "opens": stamp(s["open_at"])}
        for key, _ in WINDOWS:
            window = s["windows"].get(key)
            row[key] = None if window is None else {"used": window["used"], "resets": stamp(window["resets_at"])}
        shown.append(row)
    out = {
        "program": cfg.get("program"),
        "seat": decision["seat"],
        "account": account(decision["seat"]) if decision["seat"] else None,
        "full_percent": decision.get("full_percent"),
        "decision": {"action": decision["action"], "why": explain(decision)},
        "seats": shown,
        "state": load_state(cfg["root"]),
        # What the session running this must act on: the same notice `hl-wait` and `hl-flow save` give.
        "notice": notice(cfg),
    }
    for key in ("to", "from"):
        if key in decision:
            out["decision"][key] = decision[key]
    if "until" in decision:
        out["decision"]["until"] = stamp(decision["until"])
    return out


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-seat", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=absolute, required=True, help="the program's loop root")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="the seats' readings and what the watchdog would do now; writes nothing")
    s.add_argument("--full-percent", type=float, help="decide at this threshold instead of the program's, to try the rule on live readings")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        emit(status(program.load(args.root), time.time(), args.full_percent))
    except Refusal as refusal:
        emit({"refused": refusal.reasons})
        print(f"hl-seat: refused: {refusal}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
