"""Run a merged item's deploy check, its `**Verify.**` line, and record what it gave.

The command is the one the item was written with, read from the item's
record and never retyped: a manager once built the script by hand from the
wrong key, ran an empty one, and read its exit 0 as a pass. It runs under
bash in a throwaway checkout of the base branch as the remote has it now,
because a Verify line reads the base (`git log`, the head's check runs).

The item's `Wait.` is a deadline, not a soak. The command is tried at once
and again each time this is run (or, with `--wait`, every `--interval`
seconds) until it succeeds or the deadline passes. A try that fails before
the deadline decides nothing; only the try at or after it records a failure.
The deadline counts from the merge.

    verify-<n>.txt        the attempt's result, written once: what ran, where, and what it printed
    verify-<n>.try-<k>.txt  a try that failed before the deadline
    verify-<n>.json       the attempt's own record: its deadline and its tries

`verified` and `verify-failed` are written to the item only here. The result
file is written first and the item's field after it, so a run that died
between the two is put right by the next one, which reads the result file.
One run at a time per item: a second finds the first's lock and refuses.

A Verify recorded as failed is run again with `--retry`, once, after its
cause is fixed: the run that failed was cancelled or timed out for an
outside reason.

A deploy that failed in its own right is fixed forward by an item of its
own, in a run of its own. The first item's Verify reads its own merge and
can then never pass, so `--fixed-by <run dir>:<ID>` settles it instead: when
that later item is merged after this one and its own Verify passed, this
one is recorded `fixed-forward`, which lets the rest of its run merge.

`--outside <pr>` checks the deploy of an outside pull request this run
merged (`hl-merge --outside`) instead of an item's: the repository's own
deploy runs (`deploys` in the spec's facts for it) on the merge commit,
never a command taken from the pull request (hl/outside.py).

Exit codes: 0 verified, 2 refused, 3 not yet (tried, and the deadline has
not passed), 4 failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import claim, items, merge, program
from .common import Refusal, absolute, emit, read_json, utc_now, write_json

TRY_TIMEOUT_S = 25 * 60
INTERVAL_S = 60
OUTPUT_KEPT = 20000
LOCK = "verify.lock.d"
EXIT_RE = re.compile(r"^exit: (-?\d+)$", re.MULTILINE)


def _epoch(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_command(command: str, cwd: str, env: dict, timeout: float) -> tuple[int, str]:
    """Exit status and everything the command printed, whatever bytes it
    printed. 124 when it ran out of time; everything it started is then
    stopped with it, so nothing is left running in a checkout that is about
    to be removed."""
    proc = subprocess.Popen(
        ["/bin/bash", "-c", command], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
    )
    try:
        said, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        said, _ = proc.communicate()
        return 124, (said or b"").decode("utf-8", "replace") + f"\n[hl-verify: no answer in {int(timeout)} s]\n"
    return proc.returncode, (said or b"").decode("utf-8", "replace")


class Checkout:
    """A throwaway worktree of the base branch as the remote has it now."""

    def __init__(self, tools, repo, base: str, item_id: str):
        self.tools, self.repo, self.base = tools, Path(repo), base
        self.path = self.repo / ".worktrees" / f"verify-{item_id}"

    def __enter__(self) -> "Checkout":
        fetched = self.tools.git(["fetch", "origin", self.base], self.repo, check=False)
        if fetched.returncode != 0:
            raise Refusal(f"could not fetch origin/{self.base} in {self.repo}: {fetched.stderr.strip()[:200]}")
        self._remove()
        made = self.tools.git(["worktree", "add", "--detach", str(self.path), f"origin/{self.base}"], self.repo, check=False)
        if made.returncode != 0:
            raise Refusal(f"could not check out origin/{self.base} at {self.path}: {made.stderr.strip()[:200]}")
        self.head = self.tools.git(["rev-parse", "HEAD"], self.path).stdout.strip()
        return self

    def __exit__(self, *exc) -> None:
        self._remove()

    def _remove(self) -> None:
        if self.path.exists():
            self.tools.git(["worktree", "remove", "--force", str(self.path)], self.repo, check=False)
        self.tools.git(["worktree", "prune"], self.repo, check=False)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Lock:
    """One hl-verify at a time for an item: two would share one checkout
    path, and the second would remove it under the first. The owner's pid,
    host and start time are written inside the lock directory: a lock whose
    owner is not alive on this host is a dead run's and is taken over at
    once. Where the owner cannot be told (another host, no record yet), a
    lock older than any run can be is taken over."""

    def __init__(self, item_dir: Path, stale_s: float):
        self.path, self.stale_s = Path(item_dir) / LOCK, stale_s
        self.owner = self.path / "owner.json"

    def _dead_owner(self) -> bool:
        try:
            record = json.loads(self.owner.read_text())
            return record.get("host") == socket.gethostname() and not _pid_alive(int(record["pid"]))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _remove(self) -> None:
        for gone in (self.owner, self.path):
            try:
                gone.unlink() if gone is self.owner else os.rmdir(gone)
            except FileNotFoundError:
                pass

    def __enter__(self) -> "Lock":
        try:
            os.mkdir(self.path)
        except FileExistsError:
            try:
                age = time.time() - self.path.stat().st_mtime
            except FileNotFoundError:
                age = self.stale_s + 1
            if age < self.stale_s and not self._dead_owner():
                raise Refusal(f"another hl-verify is running for this item (lock {self.path.name}, {int(age)} s old); wait for it")
            self._remove()
            os.mkdir(self.path)
        self.owner.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started_at": time.time()}))
        return self

    def __exit__(self, *exc) -> None:
        self._remove()


def _recorded_in(result_file: Path) -> str | None:
    """What a result file says the attempt gave: its `exit:` line."""
    try:
        found = EXIT_RE.search(result_file.read_text(errors="replace"))
    except OSError:
        return None
    if not found:
        return None
    return "verified" if found.group(1) == "0" else "verify-failed"


def _open_attempt(run_dir, item_id: str) -> tuple[dict | None, bool]:
    """The latest verify attempt that still counts, and whether it has its result."""
    live = [e for e in items.load_attempts(run_dir, item_id)["log"] if e["phase"] == "verify" and not e.get("voided")]
    if not live:
        return None, False
    return live[-1], (items.item_dir(run_dir, item_id) / f"verify-{live[-1]['n']}.txt").exists()


def verify(root, run_dir, repo, item_id: str, tools, opts, clock=time.time, sleep=time.sleep, runner=run_command) -> tuple[int, dict]:
    program.load(root)
    d = items.item_dir(run_dir, item_id)
    items.load_item(run_dir, item_id)
    # Longer than one try can take, so a live run's lock is never taken for a dead one's.
    with Lock(d, opts.try_timeout + opts.interval + 300):
        return _verify(root, run_dir, repo, item_id, tools, opts, clock, sleep, runner)


def names(text: str, item_id: str) -> bool:
    """Whether an item's text names another item: how a fix-forward says which deploy it fixes."""
    return bool(re.search(rf"(?<![\w-]){re.escape(item_id)}(?![\w-])", text))


def fixed_by(root, run_dir, repo, item_id: str, other: str, tools, stale_s: float = TRY_TIMEOUT_S + INTERVAL_S + 300) -> tuple[int, dict]:
    """Settle a failed Verify by the fix-forward that put its deploy right.

    This is for a Verify that can no longer pass by being run again (it
    reads the failed deploy itself); one that can is settled by `--retry`.
    So the evidence is the fix's own: an item of this program, written for
    this one (its text names it), in the same repository, merged by
    `hl-merge` on top of this item's merge, whose own Verify command ran and
    passed."""
    fix_run, sep, fix_id = other.rpartition(":")
    if not sep or not fix_run or not fix_id:
        raise Refusal(f"--fixed-by takes <run directory>:<ID> of the fix-forward item, got {other!r}")
    runs = (Path(root) / "runs").resolve()
    if Path(fix_run).resolve().parent != runs or Path(run_dir).resolve().parent != runs:
        raise Refusal(f"a fix-forward is an item of this program: its run directory is one of {runs}/<run>")
    d = items.item_dir(run_dir, item_id)
    items.load_item(run_dir, item_id)
    # Not beside a Verify of this item that is still running: its result would be written over this one.
    with Lock(d, stale_s):
        item = items.load_item(run_dir, item_id)
        if item["status"] != "merged" or item["fields"].get("verify") != "verify-failed":
            raise Refusal(f"--fixed-by settles a Verify recorded as failed; {item_id} is {item['status']} with verify={item['fields'].get('verify')}")
        if items.load_run(fix_run)["repo"].lower() != items.load_run(run_dir)["repo"].lower():
            raise Refusal(f"{fix_id} is in {items.load_run(fix_run)['repo']}, not in {items.load_run(run_dir)['repo']}: it deploys something else")
        fix = items.load_item(fix_run, fix_id)
        fix_dir = items.item_dir(fix_run, fix_id)
        if fix["status"] != "merged" or fix["fields"].get("verify") != "verified":
            raise Refusal(f"{fix_id} is {fix['status']} with verify={fix['fields'].get('verify')}: a fix-forward settles a failed deploy once it has merged and its own Verify has passed")
        entry, finished = _open_attempt(fix_run, fix_id)
        merged = read_json(fix_dir / "merge.json") if (fix_dir / "merge.json").exists() else {}
        # Its own records must bear it out: a merge this loop made, and a Verify command that ran and exited 0.
        if merged.get("state") != "MERGED" or not finished or _recorded_in(fix_dir / f"verify-{entry['n']}.txt") != "verified":
            raise Refusal(f"{fix_id}'s own records do not show a merge by this loop and a Verify that ran and passed")
        if not names((fix_dir / "item.md").read_text(), item_id):
            raise Refusal(f"{fix_id} does not name {item_id}: a fix-forward is written for the deploy it fixes, and says so in its text")
        mine, theirs = read_json(d / "merge.json").get("merge_sha"), merged.get("merge_sha")
        # "After" is what the base branch says, not what a clock said when a record was written.
        tools.git(["fetch", "--quiet", "origin"], repo, check=False)
        on_top = bool(mine and theirs and mine != theirs) and tools.git(["merge-base", "--is-ancestor", mine, theirs], repo, check=False).returncode == 0
        if not on_top:
            raise Refusal(f"{fix_id} merged as {str(theirs)[:12]}, which is not on top of {item_id}'s merge ({str(mine)[:12]}): it cannot have fixed that deploy")
        items.record_verify(run_dir, item_id, "fixed-forward")
        items.set_fields(run_dir, item_id, [f"verify_fixed_by={items.load_run(fix_run)['run_id']}/{fix_id}"])
    return 0, {"result": "fixed-forward", "item": item_id, "fixed_by": f"{items.load_run(fix_run)['run_id']}/{fix_id}"}


def landed(run: dict, item: dict, item_dir: Path) -> dict:
    """When and as what an item's change reached the base branch: its own
    merge, or, for a run on a train, the promotion of the batch that holds
    it. `at` is None until it has."""
    record = read_json(item_dir / "merge.json") if (item_dir / "merge.json").exists() else {}
    if items.on_train(run):
        return {"at": item["fields"].get("promoted_at"), "base_sha": item["fields"].get("promoted_sha"), "merge_sha": record.get("merge_sha")}
    return {"at": record.get("at"), "base_sha": record.get("merge_sha"), "merge_sha": record.get("merge_sha")}


def _verify(root, run_dir, repo, item_id: str, tools, opts, clock, sleep, runner) -> tuple[int, dict]:
    cfg = program.load(root)
    run = items.load_run(run_dir)
    item = items.load_item(run_dir, item_id)
    d = items.item_dir(run_dir, item_id)
    if item["status"] != "merged":
        raise Refusal(f"{item_id} is {item['status']}; the deploy check runs only after the merge")
    command = (item.get("verify_command") or "").strip()
    if not command:
        raise Refusal(f"{item_id} has no Verify line; there is nothing to run and nothing to record")
    if items.on_train(run) and not item["fields"].get("promoted"):
        # Nothing of it is on the base yet, so nothing of it is deployed; waiting here would only wait for the batch.
        return 3, {"result": "not-promoted", "item": item_id, "detail": "its merge is on the train; the deploy check runs once `hl-train promote` has promoted its batch"}
    wait_s = (item.get("verify_wait_min") or 0) * 60
    entry, finished = _open_attempt(run_dir, item_id)
    if finished:
        # A run that died after writing the result and before recording it: the result file is what happened.
        said = _recorded_in(d / f"verify-{entry['n']}.txt")
        # Not over a settled record: a deploy that was fixed forward keeps that, whatever its own last result says.
        if said and item["fields"].get("verify") not in items.VERIFY_SETTLED and item["fields"].get("verify") != said:
            item = items.record_verify(run_dir, item_id, said)
    recorded = item["fields"].get("verify")

    if opts.retry:
        if not finished or recorded != "verify-failed":
            raise Refusal(f"--retry is for a Verify recorded as failed; {item_id} has verify={recorded}")
        if not opts.note:
            raise Refusal("--retry needs --note: say what was fixed or re-run since the failure")
        entry = items.start_phase(run_dir, item_id, "verify")
        record = {"n": entry["n"], "started_at": utc_now(), "deadline": clock() + wait_s, "tries": 0, "retry_note": opts.note}
    elif recorded in items.VERIFY_SETTLED:
        return 0, {"result": recorded, "already": True, "item": item_id}
    elif entry is None:
        since = landed(run, item, d)["at"]
        if not since:
            raise Refusal(f"{item_id}: nothing on record says when it reached the base branch")
        entry = items.start_phase(run_dir, item_id, "verify")
        record = {"n": entry["n"], "started_at": utc_now(), "deadline": _epoch(since) + wait_s, "tries": 0}
    elif finished:
        return 4, {
            "result": "failed",
            "already": True,
            "item": item_id,
            "file": f"verify-{entry['n']}.txt",
            "next": "a run that was cancelled or timed out for an outside reason: re-run it, then --retry --note <what changed>. A deploy that failed in its own right: fix it forward with an item in a run of its own, then --fixed-by <run dir>:<ID>",
        }
    else:
        try:
            record = read_json(d / f"verify-{entry['n']}.json")
        except (OSError, ValueError):
            # A run that died between counting the attempt and writing its record. The first attempt's deadline
            # counts from the merge, a retry's from when it was started; both are still on file.
            since = _epoch(landed(run, item, d)["at"]) if entry["n"] == 1 else _epoch(entry["at"])
            tries = len(list(d.glob(f"verify-{entry['n']}.try-*.txt")))
            record = {"n": entry["n"], "started_at": entry["at"], "deadline": since + wait_s, "tries": tries}
    n = record["n"]
    record_path = d / f"verify-{n}.json"
    write_json(record_path, record)
    where = landed(run, item, d)
    merge_sha = where["merge_sha"]
    # For a Verify line that reads a run or a commit: the item's own merge, and the commit that carried it onto the base
    # (the same one, unless a batch was promoted).
    env = {**tools.env, "HL_MERGE_SHA": merge_sha or "", "HL_BASE_SHA": where["base_sha"] or ""}

    while True:
        with Checkout(tools, repo, cfg["base"], item_id) as checkout:
            started = clock()
            code, said = runner(command, str(checkout.path), env, opts.try_timeout)
            head = checkout.head
        now = clock()
        record["tries"] += 1
        write_json(record_path, record)
        text = (
            f"# Verify {item_id}, attempt {n}, try {record['tries']}\n\n"
            f"command: {command}\n"
            f"checkout: origin/{cfg['base']} at {head}\n"
            f"merge commit: {merge_sha}\n" + (f"promoted as: {where['base_sha']}\n" if where["base_sha"] != merge_sha else "") + 
            f"ran: {_stamp(started)} for {int(now - started)} s\n"
            f"deadline: {_stamp(record['deadline'])}\n"
            f"exit: {code}\n\n"
            f"{said[-OUTPUT_KEPT:]}"
        )
        out = {
            "item": item_id,
            "attempt": n,
            "tries": record["tries"],
            "exit": code,
            "base_head": head,
            "merge_sha": merge_sha,
            # A command that reads the base's head is reading a later merge once the base has moved on.
            "base_moved": bool(merge_sha) and head != merge_sha,
            "deadline": _stamp(record["deadline"]),
        }
        if code == 0:
            (d / f"verify-{n}.txt").write_text(text)
            items.record_verify(run_dir, item_id, "verified")
            return 0, {"result": "verified", **out, "file": f"verify-{n}.txt"}
        if now >= record["deadline"]:
            (d / f"verify-{n}.txt").write_text(text)
            items.record_verify(run_dir, item_id, "verify-failed")
            return 4, {"result": "failed", **out, "file": f"verify-{n}.txt", "last_lines": said.strip().splitlines()[-5:]}
        (d / f"verify-{n}.try-{record['tries']}.txt").write_text(text)
        left = record["deadline"] - now
        if not opts.wait:
            return 3, {"result": "not-yet", **out, "minutes_left": round(left / 60, 1), "last_lines": said.strip().splitlines()[-5:]}
        sleep(min(opts.interval, max(left, 1)))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-verify", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=absolute, required=True, help="the program's loop root")
    p.add_argument("--run-dir", required=True, type=absolute)
    p.add_argument("--repo-dir", type=absolute, required=True, help="the stream's clone of the target repository")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--id")
    which.add_argument(
        "--outside",
        type=int,
        metavar="PR",
        help="the deploy check of an outside pull request this run merged (`hl-merge --outside`): the repository's deploy runs on its merge commit",
    )
    p.add_argument(
        "--deploy-wait",
        type=float,
        default=None,
        help="with --outside: seconds from the merge within which its deploy runs must finish (default 5400)",
    )
    p.add_argument("--wait", action="store_true", help="keep trying every --interval seconds until it passes or the deadline is reached")
    p.add_argument("--interval", type=float, default=INTERVAL_S)
    p.add_argument("--try-timeout", type=float, default=TRY_TIMEOUT_S, help="seconds one run of the command may take")
    p.add_argument("--retry", action="store_true", help="run a Verify that was recorded as failed once more")
    p.add_argument("--note", help="with --retry: what was fixed or re-run since the failure")
    p.add_argument("--fixed-by", metavar="RUN_DIR:ID", help="settle a Verify recorded as failed: the fix-forward item that merged later and whose own Verify passed")
    return p


def main(argv: list[str] | None = None) -> int:
    opts = _parser().parse_args(argv)
    try:
        # A Verify line calls gh; on the mini gh otherwise falls back to the keyring's token.
        merge.require_loop_token(opts.root)
        pinned = claim.check_pins(opts.root, ("git", "gh"))["bins"]
        tools = merge.Tools(git=pinned["git"], gh=pinned["gh"])
        if opts.outside is not None:
            if opts.fixed_by:
                raise Refusal("--fixed-by settles an item's Verify; an outside pull request's deploy check is settled by its deploy runs (`--retry`)")
            from . import outside  # here, not at the top: outside.py reads this module's lock

            if opts.deploy_wait is None:
                opts.deploy_wait = outside.DEPLOY_WAIT_S
            code, result = outside.verify_outside(opts.root, opts.run_dir, opts.repo_dir, opts.outside, tools, opts)
            emit(result)
            return code
        if opts.deploy_wait is not None:
            raise Refusal("--deploy-wait goes with --outside")
        if opts.fixed_by:
            code, result = fixed_by(opts.root, opts.run_dir, opts.repo_dir, opts.id, opts.fixed_by, tools)
        else:
            code, result = verify(opts.root, opts.run_dir, opts.repo_dir, opts.id, tools, opts)
    except Refusal as refusal:
        emit({"result": "refused", "refused": refusal.reasons})
        print(f"hl-verify: refused: {refusal}", file=sys.stderr)
        return 2
    emit(result)
    return code


if __name__ == "__main__":
    sys.exit(main())
