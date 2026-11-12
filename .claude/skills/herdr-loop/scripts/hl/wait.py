"""Wait for the next thing the manager must look at, then say what it is.

One of these runs in the background per manager. It exits, which wakes the
manager, on the first of:

    done     the attempt's done file exists
    gone     herdr no longer has the worker's agent
    blocked  the worker is waiting at a dialog
    limit    a Claude worker's last reply is a usage-limit refusal (with the seat it hit)
    stopped  the worker is not working, has shown no progress for
             --stopped-min minutes, and wrote no done file
    quiet    the worker reads as working and has shown no progress for
             --quiet-min minutes
    checks   every required check on the item's pull request has finished
    error    herdr or gh has failed several polls in a row
    seat     the program is paused because every Claude seat is full, or it
             has moved off the seat this manager runs on (needs --root)
    time     --max-min minutes have passed

Herdr's reading of an agent is a hint, never the signal. A Claude session
reads `done` while a background task of its own is still running, and a
Cursor session reads `done` while it waits on a question. So progress is
judged from the session's transcript where there is one (Claude), and from
how long herdr has seen the agent not working where there is not (Cursor).

A Claude worker may hand work to a sub-agent or leave a command running in
the background while its own turn ends; it is woken when either finishes.
So a sub-agent's transcript counts as the worker's progress, and a worker
with a shell command of its own still running is judged as one at work
(`quiet` after --quiet-min), not as one that stopped.

`blocked`, `limit`, `stopped` and `quiet` are reported once per occurrence.
A wait started again while the same dialog is up, or while the same silence
continues, does not fire for it again: a manager that looked and chose to
keep waiting is woken next by something new, or by the clock. `done`, `gone`
and `checks` are facts about the attempt and are reported every time, and so
is `seat`, which holds until the manager has handed over or paused.

A watch names one attempt exactly, `<ID>:<phase>-<n>`. Attempt 1's done file
never satisfies a wait for attempt 2.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import claim, herdr as herdr_mod, items, merge, program, seat
from .common import Refusal, absolute, emit, read_json, utc_now, write_json
from .herdr import Herdr, HerdrError

WATCH_RE = re.compile(r"^(?P<id>[A-Za-z][A-Za-z0-9]*-\d+):(?P<phase>[a-z]+(?:-[a-z]+)*)-(?P<n>\d+)$")
ERROR_POLLS = 5
FINISHED_BUCKETS = ("pass", "fail", "skipping", "cancel")
GREEN_BUCKETS = ("pass", "skipping")


# How Claude Code 2.1.296 starts a shell command for its Bash tool, as `ps` shows it on the mini:
# `/bin/zsh -c source /Users/<you>/.claude/shell-snapshots/snapshot-zsh-<n>-<id>.sh … && eval '<command>'`,
# a child of the `claude` process itself, whether the command is in the foreground or the background.
SHELL_MARK = ".claude/shell-snapshots/snapshot-"


class Shells:
    """Whether an agent process has a shell command of its own still running."""

    def running(self, pid) -> bool:
        if not pid:
            return False
        out = subprocess.run([claim.PS, "-axo", "ppid=,command="], capture_output=True, text=True).stdout
        for line in out.splitlines():
            ppid, _, command = line.strip().partition(" ")
            if ppid == str(pid) and SHELL_MARK in command:
                return True
        return False


class Watch:
    """One attempt and the worker that is running it."""

    def __init__(self, run_dir, spec: str):
        match = WATCH_RE.match(spec)
        if not match:
            raise Refusal(f"--for takes <ID>:<phase>-<n>, for example HL-02:build-1; got {spec!r}")
        self.item_id, self.phase, self.n = match["id"], match["phase"], int(match["n"])
        self.spec = spec
        items.load_item(run_dir, self.item_id)
        self.dir = items.item_dir(run_dir, self.item_id)
        self.worker = herdr_mod.read_worker(self.dir, self.phase, self.n)
        self.done_file = self.dir / f"{self.phase}-{self.n}.done"

    def describe(self) -> dict:
        return {"item": self.item_id, "phase": self.phase, "n": self.n, "agent": self.worker.get("agent")}


def _epoch(stamp: str | None) -> float | None:
    if not stamp:
        return None
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _progress(watch: Watch, info: dict, now: float, idle_since: dict) -> tuple[str, float] | None:
    """A name for the worker's last sign of progress and its age in seconds,
    or None when there is nothing to judge it by. A Claude session's
    transcript grows with every step it takes. Any other worker is judged by
    how long herdr has seen it not working."""
    worker = watch.worker
    if worker.get("kind") == "claude" and worker.get("session"):
        mtimes = []
        for transcript in herdr_mod.claude_transcripts(worker["session"]):
            try:
                mtimes.append(transcript.stat().st_mtime)
            except OSError:
                continue
        if mtimes:
            # A sub-agent at work writes its own transcript while the worker's stands still.
            return f"transcript@{int(max(mtimes))}", now - max(mtimes)
        return "no-transcript", now - (_epoch(worker.get("started_at")) or now)
    if info.get("agent_status") == "working":
        idle_since.pop(watch.spec, None)
        return None
    return f"not-working@{info.get('state_change_seq', 0)}", now - idle_since.setdefault(watch.spec, now)


def usage_limit(transcript: Path) -> dict | None:
    """The usage-limit refusal a Claude session is stopped on, if its latest
    reply is one. Claude Code writes it as an assistant record with
    `isApiErrorMessage` and `error: "rate_limit"`, and the reset time beside
    it. Text on the screen is never read for this: a notice that a limit is
    near looks much the same as one that it was hit."""
    with open(transcript, "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - 256 * 1024))
        tail = fh.read().decode("utf-8", "replace")
    for line in reversed(tail.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        if not (record.get("isApiErrorMessage") and record.get("error") == "rate_limit"):
            return None
        quota = record.get("quotaLimits") or {}
        resets = quota.get("resetsAt")
        content = (record.get("message") or {}).get("content") or []
        text = next((b.get("text") for b in content if isinstance(b, dict) and b.get("text")), None)
        return {
            "id": record.get("uuid") or record.get("timestamp") or str(resets),
            "resets_at": datetime.fromtimestamp(resets, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if resets else None,
            "limit_type": quota.get("rateLimitType"),
            "text": (text or "")[:200],
        }
    return None


def worker_event(watch: Watch, herdr, now: float, opts, seen: dict, idle_since: dict, shells: Shells | None = None) -> dict | None:
    if watch.done_file.exists():
        return {"event": "done", **watch.describe(), "file": str(watch.done_file)}
    info = herdr.agent(watch.worker["agent"])
    if info is None:
        return {"event": "gone", **watch.describe(), "pane": watch.worker.get("pane")}

    def once(kind: str, fingerprint: str, **detail) -> dict | None:
        if seen.get(f"{watch.spec}:{kind}") == fingerprint:
            return None
        seen[f"{watch.spec}:{kind}"] = fingerprint
        return {"event": kind, **watch.describe(), "pane": info.get("pane_id"), "status": status, **detail}

    status = info.get("agent_status")
    if status == "blocked":
        return once("blocked", f"seq@{info.get('state_change_seq', 0)}")
    worker = watch.worker
    if worker.get("kind") == "claude" and worker.get("session"):
        transcript = herdr_mod.claude_transcript(worker["session"])
        limit = usage_limit(transcript) if transcript is not None else None
        if limit:
            fingerprint = limit.pop("id")
            # The seat it hit: its transcript is under `<seat>/projects/<dir>/`. A manager starts the
            # attempt again at once when that is no longer the program's seat (`references/troubles.md`).
            limit["seat"] = str(transcript.parents[2])
            return once("limit", str(fingerprint), **limit)
    progress = _progress(watch, info, now, idle_since)
    if progress is None:
        return None
    fingerprint, age = progress
    at_work = status == "working"
    if not at_work and worker.get("kind") == "claude" and age > opts.stopped_min * 60:
        # Its turn ended, and a command it started is still running: it will be woken when that finishes.
        at_work = (shells or Shells()).running(worker.get("pid"))
    kind, after_s = ("quiet", opts.quiet_min * 60) if at_work else ("stopped", opts.stopped_min * 60)
    if age > after_s:
        detail = {"command_running": True} if at_work and status != "working" else {}
        return once(kind, fingerprint, idle_min=int(age // 60), **detail)
    return None


def checks_event(run_dir, item_id: str, tools, waited_s: float, grace_s: float) -> dict | None:
    item = items.load_item(run_dir, item_id)
    pr = item["fields"].get("pr")
    if not pr:
        raise Refusal(f"{item_id} has no pr field; record it with `hl-items set --field pr=<number>` first")
    run = items.load_run(run_dir)
    slug = run["repo"]
    # Asked first: a head that conflicts with its base gets no checks at all, and was waited for until the grace ran out
    # and then reported as `none_reported`, with nothing to say why (an item of a help-desk feature program).
    seen = json.loads(tools.gh(["pr", "view", str(pr), "--repo", slug, "--json", merge.CONFLICT_FIELDS], run_dir).stdout)
    if merge.conflicts(seen):
        build = items.load_attempts(run_dir, item_id)["build"]
        return {
            "event": "checks",
            "item": item_id,
            "pr": pr,
            "passed": False,
            "conflicting": True,
            "head_sha": seen.get("headRefOid"),
            "checks": [],
            "next": merge.conflict_next(item_id, build, seen.get("baseRefName") or "its base"),
        }
    asked, checks = merge.item_checks(tools, run_dir, slug, pr, run, seen.get("baseRefName"))
    if not asked:
        # A run on a train whose repository asks no check of an item: there is nothing to wait for.
        return {"event": "checks", "item": item_id, "pr": pr, "passed": True, "none_asked": True, "checks": []}
    if not checks:
        # GitHub lists a pull request's checks a little after it opens.
        if waited_s < grace_s:
            return None
        return {"event": "checks", "item": item_id, "pr": pr, "passed": False, "none_reported": True, "checks": []}
    unfinished = [c for c in checks if c.get("bucket") not in FINISHED_BUCKETS]
    if unfinished:
        absent = [c["name"] for c in unfinished if c.get("state") == "NOT_REPORTED"]
        if len(absent) == len(unfinished) and waited_s >= grace_s:
            # A check the spec names for an item that the pull request never starts would be waited for until the end of time.
            return {"event": "checks", "item": item_id, "pr": pr, "passed": False, "not_reported": absent, "checks": [{"name": c["name"], "bucket": c.get("bucket")} for c in checks]}
        return None
    view = json.loads(tools.gh(["pr", "view", str(pr), "--repo", slug, "--json", "headRefOid"], run_dir).stdout)
    event = {
        "event": "checks",
        "item": item_id,
        "pr": pr,
        "passed": all(c.get("bucket") in GREEN_BUCKETS for c in checks),
        "head_sha": view.get("headRefOid"),
        "checks": [{"name": c["name"], "bucket": c.get("bucket")} for c in checks],
    }
    if not event["passed"]:
        event["checks_file"] = str(write_red_checks(run_dir, item_id, slug, pr, event["head_sha"], checks))
    else:
        # Checks that were red on this build and are green now (a re-run) leave nothing for the builder to fix.
        stale = items.item_dir(run_dir, item_id) / f"checks-{items.load_attempts(run_dir, item_id)['build']}.md"
        if stale.exists():
            stale.unlink()
            event["cleared_checks_file"] = str(stale)
    return event


def batch_event(root, repo: str, tools, waited_s: float, grace_s: float) -> dict | None:
    """The required checks of a train's open batch, once every one has finished."""
    from . import program, train  # here, not at the top: train.py reads this module's log format

    record = train._open_batch(root, repo, train.load_state(root, repo))
    if not record or record["state"] != "testing":
        raise Refusal(f"no batch of {repo} is being tested: `hl-train --root {root} --repo {repo} test` opens one")
    how, checks = train.batch_checks(tools, train.clone_of(program.load(root), repo), repo, record)
    if how == "running" or (how == "none" and waited_s < grace_s):
        return None
    event = {
        "event": "batch",
        "repo": repo,
        "batch": record["n"],
        "pr": record["pr"],
        "passed": how == "green",
        "checks": [{"name": c["name"], "bucket": c.get("bucket")} for c in checks],
        "next": f"hl-train --root {root} --repo {repo} promote",
    }
    if how == "none":
        event["none_reported"] = True
    return event


RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")


def write_red_checks(run_dir, item_id: str, slug: str, pr, head_sha: str | None, checks: list[dict]) -> Path:
    """Required checks that finished red, written where the builder's next
    brief points: which, on which commit, and how to read each one's log.
    One file per build; a later look at the same build replaces it."""
    build = items.load_attempts(run_dir, item_id)["build"]
    path = items.item_dir(run_dir, item_id) / f"checks-{build}.md"
    lines = [
        f"# Required checks on pull request #{pr} of {slug}: not green",
        "",
        f"Read at {utc_now()}, on commit `{head_sha}`.",
        "",
    ]
    for check in checks:
        if check.get("bucket") in GREEN_BUCKETS:
            continue
        link = check.get("link") or ""
        run = RUN_ID_RE.search(link)
        log = f" Its log: `gh run view {run.group(1)} --repo {slug} --log-failed`." if run else ""
        lines.append(f"- `{check['name']}`: {check.get('bucket') or check.get('state')}. {link}{log}".rstrip())
    green = sorted(c["name"] for c in checks if c.get("bucket") in GREEN_BUCKETS)
    if green:
        lines += ["", "Green: " + ", ".join(f"`{name}`" for name in green) + "."]
    path.write_text("\n".join(lines) + "\n")
    return path


def wait(run_dir, watches, check_ids, herdr, tools, opts, clock=time.time, sleep=time.sleep, shells: Shells | None = None) -> dict:
    run_dir = Path(run_dir)
    seen_path = run_dir / "wait-seen.json"
    seen = read_json(seen_path) if seen_path.exists() else {}
    idle_since: dict = {}
    failures = {"herdr": 0, "gh": 0}
    started = clock()
    next_checks = next_batches = started
    trains = list(getattr(opts, "train", None) or [])
    # The program's seat, when the wait knows its root: a move or a pause outranks everything else.
    seat_cfg = None
    if getattr(opts, "root", None):
        try:
            seat_cfg = program.load(opts.root)
        except Refusal:
            seat_cfg = None

    def fired(event: dict) -> dict:
        if event["event"] in ("blocked", "limit", "stopped", "quiet"):
            write_json(seen_path, seen)
        return {**event, "waited_s": int(clock() - started)}

    while True:
        now = clock()
        told = seat.notice(seat_cfg) if seat_cfg is not None else None
        if told:
            return fired({"event": "seat", **told})
        for watch in watches:
            try:
                event = worker_event(watch, herdr, now, opts, seen, idle_since, shells)
                failures["herdr"] = 0
            except HerdrError as error:
                failures["herdr"] += 1
                event = None
                if failures["herdr"] >= ERROR_POLLS:
                    event = {"event": "error", "source": "herdr", "detail": str(error), **watch.describe()}
            if event:
                return fired(event)
        if check_ids and now >= next_checks:
            next_checks = now + opts.checks_interval
            for item_id in check_ids:
                try:
                    event = checks_event(run_dir, item_id, tools, now - started, opts.checks_grace_min * 60)
                    failures["gh"] = 0
                except (RuntimeError, ValueError) as error:
                    failures["gh"] += 1
                    event = None
                    if failures["gh"] >= ERROR_POLLS:
                        event = {"event": "error", "source": "gh", "detail": str(error)[:400], "item": item_id}
                if event:
                    return fired(event)
        if trains and now >= next_batches:
            next_batches = now + opts.checks_interval
            for repo in trains:
                try:
                    event = batch_event(opts.root, repo, tools, now - started, opts.checks_grace_min * 60)
                    failures["gh"] = 0
                except (RuntimeError, ValueError) as error:
                    failures["gh"] += 1
                    event = None
                    if failures["gh"] >= ERROR_POLLS:
                        event = {"event": "error", "source": "gh", "detail": str(error)[:400], "repo": repo}
                if event:
                    return fired(event)
        if now - started >= opts.max_min * 60:
            watching = [w.spec for w in watches] + [f"{i}:checks" for i in check_ids] + [f"{repo}:batch" for repo in trains]
            return fired({"event": "time", "watching": watching})
        sleep(opts.interval)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-wait", description=__doc__.split("\n\n")[0])
    p.add_argument("--run-dir", required=True, type=absolute)
    p.add_argument("--for", dest="watch", action="append", default=[], metavar="ID:PHASE-N", help="an attempt to watch")
    p.add_argument("--checks", action="append", default=[], metavar="ID", help="an item whose required checks to watch")
    p.add_argument("--train", action="append", default=[], metavar="OWNER/NAME", help="a repository whose open batch to watch (needs --root)")
    p.add_argument("--stopped-min", type=float, default=3, help="no progress for this long while not working")
    p.add_argument("--quiet-min", type=float, default=10, help="no progress for this long while reading as working")
    p.add_argument("--max-min", type=float, default=20)
    p.add_argument("--interval", type=float, default=20, help="seconds between looks")
    p.add_argument("--checks-interval", type=float, default=30, help="seconds between looks at GitHub")
    p.add_argument("--checks-grace-min", type=float, default=5, help="how long a pull request may show no checks at all")
    p.add_argument("--root", type=absolute, help="the program's loop root: use its pinned herdr and gh, after checking them")
    p.add_argument("--herdr", help="absolute path of the herdr binary (instead of --root)")
    p.add_argument("--gh", help="absolute path of the gh binary (instead of --root)")
    return p


def main(argv: list[str] | None = None) -> int:
    opts = _parser().parse_args(argv)
    try:
        if opts.root and (opts.herdr or opts.gh):
            raise Refusal("pass --root, or --herdr and --gh, not both")
        watches = [Watch(opts.run_dir, spec) for spec in opts.watch]
        for item_id in opts.checks:
            items.load_item(opts.run_dir, item_id)
        if opts.train and not opts.root:
            raise Refusal("--train needs --root: a batch is the program's, not a run's")
        herdr = tools = None
        needed = (("herdr",) if watches else ()) + (("gh", "git") if opts.checks or opts.train else ())
        pinned = claim.check_pins(opts.root, needed)["bins"] if opts.root and needed else {}
        if watches:
            herdr = Herdr(pinned.get("herdr") or opts.herdr)
        if opts.checks or opts.train:
            merge.require_loop_token(opts.root)
            tools = merge.Tools(git=pinned.get("git"), gh=pinned.get("gh") or opts.gh)
        event = wait(opts.run_dir, watches, opts.checks, herdr, tools, opts)
    except Refusal as refusal:
        emit({"event": "refused", "refused": refusal.reasons})
        print(f"hl-wait: refused: {refusal}", file=sys.stderr)
        return 2
    emit(event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
