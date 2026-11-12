"""The fixed sequences between a manager's decisions, one command each.

    prepare   a worktree and branch for an item, its port, its contract from
              the Accept list, and the repository's setup run in the worktree
    built     the check after a build: close the builder, or send the build back
    review    serve the build and start its reviewer
    reviewed  stop the served build, close the reviewer, read the verdict and
              the reviewer's transcript, and post the verdict on the pull request
    close     after the merge: remove the worktree and the local branch
    save      write the generated ledger tables, commit and push the ledger,
              stamp the heartbeat, and say how full the manager's context is
              and which pull requests from outside the program wait for
              rollout (hl/outside.py)

Each is a sequence the first program's manager ran by hand, a command at a
time: about 29 requests for every merged item, each carrying its whole
context. None of them decides anything. How an item is routed, which engine
builds it and at what effort, and what a failure means stay the manager's;
the merge stays `hl-merge`, and the deploy check stays `hl-verify`.

Every step is the same function the single-purpose scripts run, so a
refusal here is the refusal that script would give.

Exit codes: 0 done, 2 refused or sent back, 3 a step did not come up (the
setup, the served build), 4 required checks are red.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import archive, backlog, claim, clocks, contractfmt, herdr as herdr_mod, items, ledger, merge, program, review, seat, serve, spawn, train, verdict, wait
from .common import Refusal, absolute, emit, read_json, utc_now, write_json
from .herdr import Herdr, HerdrError

SETUP_TIMEOUT_S = 30 * 60
SENT_BACK_BOUND = 2
TAIL_LINES = 15


# ---------------------------------------------------------------- what the settings know


def stream_of(cfg: dict, run: dict) -> dict:
    return (cfg.get("streams") or {}).get(run.get("stream") or "") or {}


def clone_of(cfg: dict, run: dict, given=None) -> Path:
    """The stream's clone of the target repository."""
    path = given or stream_of(cfg, run).get("clone")
    if not path:
        raise Refusal(
            f"program.json names no clone for run {run['run_id']} (stream {run.get('stream')!r}): pass --repo-dir, "
            "or stage the program again so its streams are recorded"
        )
    if not (Path(path) / ".git").exists():
        raise Refusal(f"{path} is not a clone of the target repository")
    return Path(path)


def repo_facts(cfg: dict, run: dict) -> dict:
    return program.repo_facts(cfg, run["repo"])


def setup_key(worktree, inputs) -> str:
    """A fingerprint of the files a ready checkout depends on (lockfiles,
    requirement files). When it changes, the checkout is not ready any more."""
    digest = hashlib.sha256()
    for name in sorted(inputs or []):
        path = Path(worktree) / name
        digest.update(name.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"absent")
    return digest.hexdigest()[:16]


def run_setup(command: str, worktree, log: Path, timeout: float = SETUP_TIMEOUT_S) -> dict:
    """Run the repository's setup command in a worktree, under bash, with its
    output in a log beside the item. Everything it starts ends with it."""
    started = time.time()
    with open(log, "wb") as out:
        proc = subprocess.Popen(["/bin/bash", "-c", command], cwd=str(worktree), stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
            code = 124
    tail = log.read_text(errors="replace").strip().splitlines()[-TAIL_LINES:]
    return {"exit": code, "seconds": int(time.time() - started), "log": str(log), "last_lines": tail}


# ---------------------------------------------------------------- prepare

# Directories a repository's setup installs its tools into. A criterion's command that lives in one of them is there
# only if the setup put it there, and nothing an item builds will.
TOOL_DIRS = frozenset((".venv", "venv", "node_modules", ".deps"))
SHELL_SPLIT_RE = re.compile(r"&&|\|\||[;|()]|\$\(|`")
# "From `services/platform`: …", "in `web`, …": the directory a criterion's commands are run from.
RUN_FROM_RE = re.compile(r"\b(?:from|in)\s+`([^`\s]+)`", re.IGNORECASE)


def _command_words(command: str) -> list[tuple[str, str | None]]:
    """Each simple command's program in one shell line, with the directory a
    `cd` before it in the same line moved to (or None)."""
    import shlex

    out, moved = [], None
    for piece in SHELL_SPLIT_RE.split(command):
        try:
            words = shlex.split(piece)
        except ValueError:
            words = piece.split()
        words = [w for w in words if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w)] if words else []
        if not words:
            continue
        if words[0] == "cd" and len(words) > 1:
            moved = words[1]
            continue
        out.append((words[0], moved))
    return out


def missing_commands(worktree, contract_text: str) -> list[dict]:
    """The criteria whose command is a program under one of the worktree's
    tool directories (`.venv/bin/ruff`, `node_modules/.bin/tsc`) that is not
    there once the worktree is prepared. An item of a help-desk feature
    program named `.venv/bin/ruff` in a criterion, the platform's setup
    installs no ruff, and the item was built and reviewed before anyone saw the criterion
    could not run. Only such paths are looked at: a program on the PATH is
    the worker's to have, and a script elsewhere in the repository may be
    one the item adds. This reports; it does not refuse, because it reads
    a criterion's prose and may read it wrong."""
    worktree = Path(worktree)
    try:
        criteria = contractfmt.parse_criteria(contract_text)
    except contractfmt.ContractError:
        return []
    found = []
    for n, text in criteria:
        named = [d.rstrip("/") for d in RUN_FROM_RE.findall(text) if not d.startswith(("/", "~")) and ".." not in d.split("/")]
        bases = [""] + [d for d in named if (worktree / d).is_dir()]
        for span in re.findall(r"`([^`]+)`", text):
            for word, moved in _command_words(span):
                parts = word.split("/")
                if word.startswith(("/", "~", "$")) or not TOOL_DIRS & set(parts[:-1]):
                    continue
                here = bases + ([moved] if moved and not moved.startswith(("/", "~", "$")) else [])
                if not any((worktree / base / word).exists() for base in here):
                    entry = {"criterion": n, "command": word, "looked_in": [base or "." for base in here]}
                    if entry not in found:
                        found.append(entry)
    return found


def prepare(root, run_dir, item_id: str, tools, opts) -> tuple[int, dict]:
    cfg = program.load(root)
    run = items.load_run(run_dir)
    item = items.load_item(run_dir, item_id)
    d = items.item_dir(run_dir, item_id)
    if item["status"] in items.TERMINAL:
        raise Refusal(f"{item_id} is {item['status']}; there is nothing to prepare")
    clone = clone_of(cfg, run, opts.repo_dir)
    base = items.base_of(run, cfg["base"])
    fields = item["fields"]
    branch = fields.get("branch") or f"hl/{run['run_id']}/{item_id}"
    worktree = Path(fields.get("worktree") or clone / ".worktrees" / f"{run['run_id']}-{item_id}")

    port = fields.get("port")
    if opts.port:
        port = None if opts.port == "none" else opts.port
        if port is not None and not str(port).isdigit():
            raise Refusal(f"--port takes a number or `none`, got {opts.port!r}")
    elif port is None and stream_of(cfg, run).get("port_base") is not None:
        number = re.search(r"(\d+)$", item_id)
        port = str(int(stream_of(cfg, run)["port_base"]) + int(number.group(1))) if number else None
    looked_at = (item.get("route") or {}).get("look") or "look" in (item.get("floor") or [])
    if looked_at and port is None:
        # Said before anything is made: the same refusal `hl-items route --look yes` gives an item prepared with no port.
        why = "it is routed with a look" if (item.get("route") or {}).get("look") else "its floor asks for a look"
        raise Refusal(
            f"{item_id} would get no port ({'--port none' if opts.port == 'none' else 'its stream has no `port_base`'}), and {why}, which is of a "
            f"served build: pass `--port <n>`, one no other stream uses"
        )

    made = False
    if not worktree.is_dir():
        if items.on_train(run):
            # The item is built on what has merged before it, tested or not: the train, made from the base the first
            # time, and fetched by name so that a clone told to follow only the base still gets it.
            train.ensure(cfg, run["repo"], clone, tools)
        else:
            fetched = tools.git(["fetch", "origin", base], clone, check=False)
            if fetched.returncode != 0:
                raise Refusal(f"could not fetch origin/{base} in {clone}: {fetched.stderr.strip()[:200]}")
        tools.git(["worktree", "prune"], clone, check=False)
        exists = tools.git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], clone, check=False).returncode == 0
        # A branch that is already there is an item taken up again: its commits are the work so far.
        argv = ["worktree", "add", str(worktree), branch] if exists else ["worktree", "add", "-b", branch, str(worktree), f"origin/{base}"]
        added = tools.git(argv, clone, check=False)
        if added.returncode != 0:
            raise Refusal(f"could not make the worktree {worktree}: {added.stderr.strip()[:300]}")
        made = True

    pairs = [f"branch={branch}", f"worktree={worktree}"] + ([f"port={port}"] if port else [])
    if made:
        pairs.append("base_sha=" + tools.git(["rev-parse", f"origin/{base}"], clone).stdout.strip())
    items.set_fields(run_dir, item_id, pairs)

    if not item.get("contract"):
        items.contract_from_accept(run_dir, item_id)
    # A criterion the parser missed is never built or graded: a short contract is said, not installed quietly.
    record = items.load_item(run_dir, item_id)["contract"]
    numbered = backlog.numbered_lines((d / "item.md").read_text(), backlog.MARKER_ACCEPT)
    if record["source"] == "accept" and record["criteria"] < numbered:
        raise Refusal(
            f"the contract installed from the Accept list of {item_id} has {record['criteria']} criteria, and the Accept section has {numbered} numbered lines: "
            "the kit's parser missed some. Do not edit the contract or the item; say so in a note below the agree brief, let the premise check "
            f"REVISE it with every criterion and install that (`hl-items contract --id {item_id} --from-amended agree-<n>.md`), and tell the operator"
        )

    facts = repo_facts(cfg, run)
    out = {
        "item": item_id,
        "title": item["title"],
        "floor": item.get("floor") or [],
        "criteria": item.get("accept_count"),
        "has_verify": bool(item.get("verify_command")),
        "item_file": str(d / "item.md"),
        "branch": branch,
        "worktree": str(worktree),
        "worktree_made": made,
        "port": port,
        "setup": "none",
        "base": base,
        "merge": items.policy(run)["merge"],
    }
    # A stream may opt out of its repository's setup: a tools area beside an application needs none of its installs.
    command = facts.get("setup") if stream_of(cfg, run).get("setup", True) else None
    if command and fields.get("setup") != "ready":
        ran = run_setup(command, worktree, d / "setup-1.log", opts.setup_timeout)
        if ran["exit"] != 0:
            items.set_fields(run_dir, item_id, ["setup=failed"])
            return 3, {**out, "result": "setup-failed", "setup": "failed", **ran}
        left = [line[3:] for line in tools.git(["status", "--porcelain"], worktree, check=False).stdout.splitlines() if line.strip()]
        if left:
            # The check after a build refuses a worktree that is not clean, so every build of this repository would be sent back.
            items.set_fields(run_dir, item_id, ["setup=failed"])
            return 3, {
                **out,
                "result": "setup-failed",
                "setup": "failed",
                **ran,
                "left_behind": left[:20],
                "detail": "the setup command left files the repository does not ignore; a setup may only write paths git ignores (a virtualenv, node_modules)",
            }
        items.set_fields(run_dir, item_id, ["setup=ready", f"setup_key={setup_key(worktree, facts.get('setup_inputs'))}"])
        out.update(setup="ready", setup_seconds=ran["seconds"])
    elif command:
        out["setup"] = "ready"
    # Once the setup has run: a criterion whose tool it did not install fails every build and review of the item.
    contract = d / "contract.md"
    out["missing_commands"] = missing_commands(worktree, contract.read_text()) if contract.is_file() else []
    return 0, {"result": "prepared", **out}


# ---------------------------------------------------------------- built


def built(root, run_dir, item_id: str, tools, herdr, opts) -> tuple[int, dict]:
    cfg = program.load(root)
    run = items.load_run(run_dir)
    clone = clone_of(cfg, run, opts.repo_dir)
    # Asked first: an accepted build has its pull request and head written on the item, and this command then closes
    # the builder it finds on record. A build with no worker on record is refused before anything is written.
    herdr_mod.read_worker(items.item_dir(run_dir, item_id), "build", items.load_attempts(run_dir, item_id)["build"])
    try:
        out = merge.after_build(run_dir, item_id, clone, tools, repo_facts=repo_facts(cfg, run))
    except merge.NotFinished as not_finished:
        sent = merge.send_back(run_dir, item_id, not_finished.reasons)
        if sent["times_sent_back"] >= SENT_BACK_BOUND:
            # Sent back twice is a build that did not finish. The builder is closed; what happens next is the manager's.
            closed = spawn.close(run_dir, item_id, "build", sent["build"], herdr)
            return 2, {**sent, "result": "sent-back-twice", "builder": closed.get("note", "closed")}
        try:
            prompted = spawn.send(run_dir, item_id, "build", sent["build"], sent["fix_file"], herdr)
        except (Refusal, HerdrError) as failure:
            # The fix list is written and the send-back counted; only the one line to the builder did not go.
            return 2, {
                **sent,
                "prompted": False,
                "prompt_error": str(failure),
                "next": f"read the builder's pane, then `hl-spawn prompt --id {item_id} --phase build --n {sent['build']} --file {sent['fix_file']}`",
            }
        return 2, {**sent, "prompted": True, "watch": prompted["watch"]}
    # A builder that is still open can push after it said it was done.
    closed = spawn.close(run_dir, item_id, "build", out["build"], herdr)
    return 0, {**out, "builder": closed.get("note", "closed"), "disputed": items.disputed_criteria(run_dir, item_id)}


# ---------------------------------------------------------------- review


def _checks_state(tools, clone, run: dict, pr) -> tuple[str, list[dict]]:
    """`green`, `red`, `running` or `none` for the checks an item's pull request must show, and the checks.
    A run on a train whose repository asks no check of an item has nothing to wait for: `green`.
    `conflicting` before any of them: the pull request conflicts with its base, so it gets no checks and cannot merge."""
    seen = json.loads(tools.gh(["pr", "view", str(pr), "--repo", run["repo"], "--json", merge.CONFLICT_FIELDS], clone).stdout)
    if merge.conflicts(seen):
        return "conflicting", [{"base": seen.get("baseRefName")}]
    asked, checks = merge.item_checks(tools, clone, run["repo"], pr, run, seen.get("baseRefName"))
    if not asked:
        return "green", []
    if not checks:
        return "none", []
    if any(c.get("bucket") not in wait.FINISHED_BUCKETS for c in checks):
        return "running", checks
    return ("green" if all(c.get("bucket") in wait.GREEN_BUCKETS for c in checks) else "red"), checks


def written_note(run_dir, item_id: str, build: int, head: str, files: list[str]) -> Path:
    """Say in the note of the next build what this one wrote to tracked
    files as it was served, and what its builder is to do about each. The
    note is the file a rework's brief points at when no verdict and no red
    check sent the build back; what a manager wrote there stays."""
    note = spawn.rework_note(items.item_dir(run_dir, item_id), build + 1)
    lines = [
        f"# Build {build} of {item_id} wrote to tracked files as it was served",
        "",
        f"After your own Serve and Ready lines had run (and the repository's setup, where it ran again), these tracked files differed from your commit `{head}`:",
        "",
        *[f"- `{path}`" for path in files],
        "",
        "A review is of what is committed, so none was started. For each file, one of two things:",
        "",
        "- What was written belongs to the item: commit and push it.",
        "- It does not: stop writing it. Have the build write under `.scratch/` or another path git ignores, and put the file back (`git checkout HEAD -- <path>`).",
        "",
        "Then run your Serve and Ready lines once more: `git status --porcelain` must print nothing afterwards.",
        "",
    ]
    text = "\n".join(lines)
    was = note.read_text() if note.exists() else ""
    if text not in was:
        note.write_text(was + ("\n" if was else "") + text)
    return note


def start_review(root, run_dir, item_id: str, tools, herdr, host, opts, envs=None) -> tuple[int, dict]:
    cfg = program.load(root)
    run = items.load_run(run_dir)
    item = items.load_item(run_dir, item_id)
    d = items.item_dir(run_dir, item_id)
    fields = item["fields"]
    build = items.load_attempts(run_dir, item_id)["build"]
    if item["status"] != "in_progress" or not build or not fields.get("head_sha") or not fields.get("pr"):
        raise Refusal(f"{item_id} has no accepted build to review: run `hl-flow built` when its builder is done")
    past = items.bound_reached(run_dir, item_id, "grade")
    if past:
        raise Refusal(past)
    rewritten = items.contract_problem(run_dir, item_id)
    if rewritten:
        # Before anything is served: a review is of the contract that was installed.
        raise Refusal(rewritten)
    clone = clone_of(cfg, run, opts.repo_dir)
    facts = repo_facts(cfg, run)
    worktree = fields.get("worktree")
    # Read before anything is served: a note that cannot be read, or that this program's review brief has no slot for,
    # refuses the review, not a reviewer with a build up.
    sets = spawn._sets([], getattr(opts, "note_file", None))
    if spawn.NOTE in sets:
        no_slot = spawn.note_slot_problem(cfg, run_dir, item_id, "grade")
        if no_slot:
            raise Refusal(no_slot)

    state, checks = _checks_state(tools, clone, run, fields["pr"])
    if state == "conflicting":
        # Not served and not reviewed: whatever the review said, the head that merges is the one after the conflict is resolved.
        return 4, {"result": "conflicting", "checks": state, "next": merge.conflict_next(item_id, build, checks[0]["base"] or items.base_of(run, cfg["base"]))}
    if state == "red":
        # A head the checks already refused is not worth a review: it goes back to its builder.
        head = json.loads(tools.gh(["pr", "view", str(fields["pr"]), "--repo", run["repo"], "--json", "headRefOid"], clone).stdout).get("headRefOid")
        red = wait.write_red_checks(run_dir, item_id, run["repo"], fields["pr"], head, checks)
        return 4, {"result": "checks-red", "checks_file": str(red), "checks": [{"name": c["name"], "bucket": c.get("bucket")} for c in checks]}
    if not cfg.get("review_with_ci") and state != "green":
        raise Refusal(
            f"the required checks of {item_id} are {state}; this program reviews after its checks have passed "
            f"(`review_with_ci` is off): wait with `hl-wait --checks {item_id}`"
        )

    # A review begins on the commit. Asked here, before the setup runs again and the build is served: what those
    # write by themselves is noted when the reviewer starts and holds nothing up, and anything already there is not theirs.
    began = review.checkout_state(tools, worktree)
    if began is None and worktree:
        raise Refusal(f"the checkout of {item_id} ({worktree}) cannot be read, so there is nothing to serve or to review. `hl-flow prepare --id {item_id}` makes it again from the item's branch")
    changed = review.begins_changed(began, worktree, item_id, fields["head_sha"], fields.get("branch"), tools)
    if changed:
        raise Refusal(changed)

    out: dict = {"checks": state}
    if facts.get("setup") and fields.get("setup") == "ready":
        key = setup_key(worktree, facts.get("setup_inputs"))
        if key != fields.get("setup_key"):
            # The build changed a manifest or a lockfile: what was installed before it is stale.
            ran = run_setup(facts["setup"], worktree, d / f"setup-{build + 1}.log", opts.setup_timeout)
            if ran["exit"] != 0:
                return 3, {"result": "setup-failed", **ran}
            items.set_fields(run_dir, item_id, [f"setup_key={key}"])
            out["setup_again_seconds"] = ran["seconds"]

    route = item.get("route")
    if route and not route.get("look") and facts.get("seen"):
        try:
            seen = review.diff_facts(tools, worktree, f"origin/{items.base_of(run, cfg['base'])}", fields["head_sha"], facts)["seen"]
        except (RuntimeError, OSError) as failure:
            raise Refusal(f"could not read the diff of {item_id} in {worktree}: {failure}") from failure
        if seen:
            shown = ", ".join(seen[:8]) + (f" and {len(seen) - 8} more" if len(seen) > 8 else "")
            raise Refusal(
                f"{item_id} is routed without a look, and its pull request changes paths this repository's spec says a person "
                f"sees ({shown}). Route it again: `hl-items route --id {item_id} --premise {route['premise']} --look yes --reason <why>`"
            )

    code, served = serve.start(root, run_dir, item_id, herdr, host)
    if code != 0:
        return 3, {**out, **served}
    argv = ["--root", str(root), "--run-dir", str(run_dir), "start", "--id", item_id, "--phase", "grade", "--engine", "claude"]
    argv += ["--effort", opts.effort] if opts.effort else []
    argv += ["--full"] if opts.full else []
    argv += ["--note-file", opts.note_file] if getattr(opts, "note_file", None) else []
    spawn_opts = spawn._parser().parse_args(argv)
    spawn_opts.sets = sets
    try:
        # What was served is what `hl-serve` read from the report before it ran anything: the report is the build's to rewrite once it is up.
        how_run = spawn.how_run_of(served["command"], served["ready_kind"], served["ready"])
        started = spawn.start(root, run_dir, item_id, "grade", "claude", herdr, spawn_opts, envs=envs, tools=tools, began=began, how_run=how_run)
    except (Refusal, HerdrError) as failure:
        # A served build with nobody reviewing it would hold its port.
        try:
            serve.stop(run_dir, item_id, herdr, host)
        except (Refusal, HerdrError):
            pass
        if not isinstance(failure, spawn.WrittenBeforeReview):
            raise
        # The build's own doing, so it goes back to its builder like a worktree that is not clean: the next build's brief points at the note.
        note = written_note(run_dir, item_id, build, fields["head_sha"], failure.files)
        return 4, {
            **out,
            "result": "written-before-review",
            "files": failure.files,
            "note_file": str(note),
            "detail": (
                f"{failure}. It goes back to its builder: start the next build, whose brief points at {note.name}. If these are paths the repository's own "
                f"setup or served build rewrites by itself, and that is harmless, the operator names them under `repos` > `{run['repo']}` > `rewrites` in the "
                "program's spec and stages the program again: a repository's facts are read from program.json at every review, whatever policy the run "
                "froze, so the run takes them up as it is"
            ),
        }
    return 0, {
        "result": "reviewing",
        **out,
        "served": served.get("summary"),
        "watch": started["watch"],
        "agent": started["agent"],
        "prompt_sent": started["prompt_sent"],
        # Lines that lay below the script's own brief and were sent with it. Not yours: something else wrote in the item directory.
        "brief_notes": started["brief_notes"],
        **({"manager_note": started["manager_note"]} if started.get("manager_note") else {}),
        **({"warning": started["warning"]} if started.get("warning") else {}),
        # In a run whose reworks may get a delta confirm: which review the script gave this head, and why.
        **({"confirm": started["confirm"]} if started.get("confirm") else {}),
    }


# ---------------------------------------------------------------- reviewed


def comment_text(item_id: str, n: int, data: dict, checked: dict, seen: dict | None, problems: list[str] | None = None) -> str:
    """The verdict as the pull request's record of it: every criterion with
    its result and note, the fix list, the looks, the findings, and how the
    change was exercised. Evidence stays in the run directory and is named.
    A review that does not count for a merge says so first."""
    confirms = checked.get("confirms")
    what = f"review {n}" + (f", a confirm of the fix made since review {confirms}" if confirms else "")
    lines = [f"**{item_id}, {what}: {checked['effective']}**" + (f" ({checked['downgraded']})" if checked.get("downgraded") else ""), ""]
    if problems:
        lines += ["**This review does not count for a merge:**", *[f"- {p}" for p in problems], ""]
    # A confirm answers the failed review's fix list; a review goes through the criteria.
    rows = [(a["entry"], a) for a in data["answers"]] if confirms else [(c["n"], c) for c in data["criteria"]]
    lines += [f"| {'Entry' if confirms else '#'} | Result | Note | Evidence |", "|---|---|---|---|"]
    for number, row in rows:
        note = str(row.get("note", "")).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {number} | {row['result']} | {note} | {', '.join(row.get('evidence') or [])} |")
    if confirms:
        lines += ["", f"The fix beyond the fix list: {data['delta']['result']}. {data['delta']['note']}"]
    if data.get("fix_list"):
        lines += ["", "Still to change:" if confirms else "Fix list:"]
        for f in data["fix_list"]:
            where = f" ({f['where']})" if f.get("where") else ""
            about = f"criterion {f['criterion']}" if f.get("criterion") is not None else "no criterion"
            lines.append(f"- {about}: {f['what']}{where}")
    if data.get("looks"):
        lines += ["", "Looks:"]
        lines += [f"- {look['mode']}, {look['url']}: {look['result']} ({look['screenshot']}). {look.get('note', '')}".rstrip() for look in data["looks"]]
    if data.get("findings"):
        lines += ["", "Findings (kept for follow-up; nothing here was acted on in this item):"]
        for f in data["findings"]:
            kind = f"{f['kind']}, " if f.get("kind") else ""
            lines.append(f"- {kind}{f.get('severity')}: {f.get('summary')}")
    lines += ["", f"How it was run: {data.get('how_run', '')}"]
    if seen:
        lines.append(
            f"The reviewer's transcript shows {seen.get('ran', 0)} commands that ran something, {seen.get('served_calls', 0)} calls on the "
            f"served build and {seen.get('served_pages', 0)} of its pages opened in the browser."
            + (f" Its own commands showed it the fix's diff of {seen['fix_files_shown']} of the {seen['fix_files']} files the fix changes." if "fix_files" in seen else "")
        )
    return "\n".join(lines) + "\n"


def reviewed(root, run_dir, item_id: str, tools, herdr, host, opts) -> tuple[int, dict]:
    cfg = program.load(root)
    run = items.load_run(run_dir)
    item = items.load_item(run_dir, item_id)
    d = items.item_dir(run_dir, item_id)
    attempts = items.load_attempts(run_dir, item_id)
    n = attempts["grade"]
    if not n:
        raise Refusal(f"{item_id} has no review to read")
    if not (d / f"grade-{n}.done").exists():
        raise Refusal(
            f"grade-{n}.done does not exist: the reviewer has not finished. Wait on it; one that ended without a result is "
            "closed and voided, and a fresh one started"
        )
    clone = clone_of(cfg, run, opts.repo_dir)
    notes = []
    try:
        serve.stop(run_dir, item_id, herdr, host)
    except Refusal as refusal:
        # Nothing was served for this build (`true`), or the port is held by something else: said, and the verdict is still read.
        notes.append(f"serve: {refusal}")
    spawn.close(run_dir, item_id, "grade", n, herdr)

    style = items.policy(run)["review"]
    checked = verdict.check(d, n, style=style)
    out: dict = {"item": item_id, "review": n, "effective": checked["effective"], "raw": checked["raw"], "downgraded": checked["downgraded"]}
    problems = [f"verdict-{n}.json: {p}" for p in checked["problems"]]
    seen = None
    if style == "code":
        seen = review.check(run_dir, item_id, n, tools, clone)
        problems += [f"review {n}: {p}" for p in seen["problems"]]
        out.update(fix_list=checked.get("fix_list"), criteria_in_question=checked.get("criteria_in_question") or [], by_checks=checked.get("by_checks") or [])
        # Findings of kind `criterion` that name no criterion: notes about the item's own text, kept for follow-up and corrected nowhere.
        out["criterion_notes"] = checked.get("criterion_notes") or 0
        out["exercise"] = {k: seen.get(k) for k in ("ran", "served_calls", "served_pages", "looks", "exercised", "looked", "look_required", "sub_agents")}
        if checked.get("confirms"):
            # A delta confirm: which review's fix list it answered and how, and how much of the fix its transcript shows it was shown.
            out.update(confirms=checked["confirms"], answers=checked["answers"], delta=checked["delta"])
            out["exercise"].update(fix_files=seen.get("fix_files"), fix_files_shown=seen.get("fix_files_shown"))

    # What the review did to its checkout is read once, now that the reviewer and the served build are gone,
    # against the state noted when the review began, and kept on the review's attempt. Read again later, a file
    # that was edited under the reviewer and then put back would look as if it had never been touched.
    entry = next(e for e in attempts["log"] if e["phase"] == "grade" and e["n"] == n and not e.get("voided"))
    worktree = item["fields"].get("worktree")
    now = review.checkout_state(tools, worktree)
    # Kept only if it is the first: `record_read` answers with the read that is on the attempt. With what the
    # checkout showed goes what the verdict said and what the transcript showed, as they were now: the files
    # they are read from stay where a worker can write, and what is taken from them later counts only while
    # they are still the ones that were read.
    first = {
        # A checkout on record that cannot be read now goes down as one that changed, for good: made again later, it would read clean.
        **review.checkout_read(entry.get("baseline"), now, entry.get("head_sha"), worktree),
        "verdict_sha": verdict.file_sha(d, n),
        "effective": checked["effective"],
        "exercise": seen["problems"] if seen else [],
        "looked": bool(seen and seen.get("looked")),
    }
    read = items.record_read(run_dir, item_id, n, first)
    if read.get("verdict_sha") != first["verdict_sha"]:
        problems.append(f"verdict-{n}.json is not the verdict that was first read: it was changed afterwards, or is gone")
    out.update(checkout=read["checkout"], dirty_before=read["dirty_before"])
    if read["dirty_before"]:
        # Said every time: only the spec's word lets a review run over such a file.
        notes.append(f"this review ran over tracked files that differed from the commit, which the spec names under `rewrites`: {', '.join(read['dirty_before'][:8])}")
    if read["checkout"] and read.get("effective") == "PASS":
        # A pass of something that was not the head is no pass of it. Once for an item, it is given back and does not use up one of the build's reviews.
        problems += read["checkout"]
        out["next"] = items.changed_pass_next(run_dir, item_id, n)
    elif read["checkout"]:
        # Anything but a pass stands as what it is: the build goes back, or the verdict is unusable, as it would over an untouched checkout.
        notes.append(f"the checkout changed under this review ({'; '.join(read['checkout'])}), and a review that did not pass stands as it is")

    posted = d / f"verdict-{n}.comment.json"
    if not checked["problems"] and not posted.exists() and not opts.no_comment:
        body = d / f"verdict-{n}.comment.md"
        body.write_text(comment_text(item_id, n, json.loads((d / f"verdict-{n}.json").read_text()), checked, seen, problems))
        # `gh pr comment` asks GitHub for fields the loop's token has no scope for; the REST call does not.
        sent = tools.gh(["api", "--method", "POST", f"repos/{run['repo']}/issues/{item['fields'].get('pr')}/comments", "-F", f"body=@{body}", "--jq", ".html_url"], clone, check=False)
        if sent.returncode == 0:
            write_json(posted, {"url": sent.stdout.strip(), "at": utc_now()})
        else:
            notes.append(f"the verdict could not be posted on the pull request: {sent.stderr.strip()[:200]}")
    if posted.exists():
        out["comment"] = read_json(posted).get("url")

    try:
        out["checks"], seen_checks = _checks_state(tools, clone, run, item["fields"].get("pr"))
        if out["checks"] == "conflicting":
            # A pass of this head cannot merge: GitHub refuses a conflicting pull request, and so does the gate.
            out["next"] = out.get("next") or merge.conflict_next(item_id, attempts["build"], seen_checks[0]["base"] or items.base_of(run, cfg["base"]))
    except RuntimeError as failure:
        notes.append(f"the required checks could not be read: {str(failure)[:200]}")
    out.update(problems=problems, notes=notes)
    write_json(d / f"review-{n}.json", out)
    return (2 if problems else 0), out


# ---------------------------------------------------------------- close


def close(root, run_dir, item_id: str, tools, herdr, host, opts) -> tuple[int, dict]:
    cfg = program.load(root)
    run = items.load_run(run_dir)
    item = items.load_item(run_dir, item_id)
    if item["status"] != "merged" and not (item["status"] == "skipped" and item.get("superseded_by")):
        raise Refusal(
            f"{item_id} is {item['status']}: only a merged item's worktree and local branch are removed, or a failed one's once it is skipped as superseded "
            "(a failed or parked item's stay, and its branch stays pushed)"
        )
    still_open = spawn._open_workers(run_dir, item_id)
    if still_open:
        raise Refusal(f"{item_id} still has a worker open ({', '.join(still_open)})")
    served = serve.status(run_dir, item_id, host)
    if served.get("record") and not served["record"].get("stopped_at"):
        raise Refusal(f"{item_id}'s build is still being served: `hl-serve stop` first")
    clone = clone_of(cfg, run, opts.repo_dir)
    fields = item["fields"]
    out = {"item": item_id, "worktree": "none", "branch": "none", "listening": served.get("listening_now") or []}
    worktree, branch = fields.get("worktree"), fields.get("branch")
    if worktree and Path(worktree).is_dir():
        # --force: a builder's ignored build output is still in it.
        removed = tools.git(["worktree", "remove", "--force", str(worktree)], clone, check=False)
        if removed.returncode != 0:
            raise Refusal(f"could not remove the worktree {worktree}: {removed.stderr.strip()[:200]}")
        out["worktree"] = "removed"
    tools.git(["worktree", "prune"], clone, check=False)
    if branch and tools.git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], clone, check=False).returncode == 0:
        deleted = tools.git(["branch", "-D", branch], clone, check=False)
        out["branch"] = "removed" if deleted.returncode == 0 else f"kept: {deleted.stderr.strip()[:120]}"
    items.set_fields(run_dir, item_id, [f"closed_at={utc_now()}"])
    return 0, {"result": "closed", **out}


# ---------------------------------------------------------------- save


def context_tokens(root) -> int | None:
    """How large the manager's last request was: the size of its context
    now. Read from the transcript of the session the watchdog started."""
    path = Path(root) / "watchdog.json"
    if not path.exists():
        return None
    session = (read_json(path).get("manager") or {}).get("session")
    transcript = herdr_mod.claude_transcript(session) if session else None
    if transcript is None:
        return None
    last = None
    with open(transcript, "rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - 512 * 1024))
        tail = fh.read().decode("utf-8", "replace")
    for line in tail.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        usage = ((record.get("message") or {}).get("usage") if isinstance(record, dict) and record.get("type") == "assistant" else None) or None
        if usage:
            last = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return last


def save(root, pid: int, procs, note: str, git=None, no_token: str | None = None, outside_facts=None, ci_facts=None) -> tuple[int, dict]:
    """Write the generated ledger files, commit and push the ledger, and
    stamp the heartbeat. Refused, before anything is written, when the
    program is no longer this manager's.

    The push, and the pull it may need, offer the loop's own token and no
    credential the host holds (`merge.LOOP_CREDENTIAL`). `no_token` is why
    that token could not be had: the ledger is then written, committed
    and the heartbeat stamped as always, and nothing is sent.

    The answer also says which pull requests from outside the program wait
    in its repositories, labeled `rollout-ready` by a steward
    (`outside_prs_ready`, hl/outside.py): a save ends every cycle, so the
    manager sees them there. It is a fact, read after the heartbeat is
    stamped; nothing about it can fail the save. `outside_facts` is that
    read (`outside.ready_or_said`, as the command line passes it).

    It says too how long the last merged items of each repository waited on
    their required checks and on their deploy, against the program's budget
    (`ci_time`, hl/pace.py), so a CI that slows a little with every item is
    seen in the cycle it crosses the budget. `ci_facts` is that read
    (`pace.facts_or_said`); it cannot fail the save either.

    And it says which streams have no item in progress, since when, and how
    many items wait in their runs (`idle`, `ledger.idle_streams`), read from
    the run directories alone. What an idle stream should do is the
    manager's to decide; this only makes sure it is seen at every save.

    Last, it names what is still open and waits on a clock, a reading or
    another run closing (`clocks`, hl/clocks.py): the runs whose file's
    header says so, the items whose text does, and the gates of the
    ledger's Gates table that are not settled. Nothing waits to see whether
    it fixes itself (the operator, 2026-11-10); which wait protects a person
    is the manager's to say. A fact like the others: it cannot fail the
    save."""
    cfg = program.load(root)
    claim._require_mine(root, pid, procs)
    ledger_file = cfg.get("ledger")
    if not ledger_file or not Path(ledger_file).parent.is_dir():
        raise Refusal(f"program.json names no ledger ({ledger_file!r}): stage the program again")
    task = Path(ledger_file).parent
    at = utc_now()
    (task / "LEDGER-ITEMS.md").write_text(ledger.items_text(root, cfg["program"], at))
    followup_rows = ledger.followups(root)
    try:
        # An archived report that reads otherwise now (written again since) is listed here again, to be read again.
        gone = archive.gone_reports(followup_rows, archive.archived_reports(task))
        archived_error = None
    except Exception as exc:  # noqa: BLE001 - listed whole then: never fewer follow-ups than there are
        gone, archived_error = set(), f"{archive.ARCHIVED_REPORTS} could not be read: {exc}"[:300]
    (task / "FOLLOW-UPS.md").write_text(ledger.followups_text(root, cfg["program"], at, [r for r in followup_rows if r["report"] not in gone]))

    def run_git(*args):
        if git is not None:
            return git(*args)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(["git", *merge.LOOP_CREDENTIAL, "-C", str(task), *args], capture_output=True, text=True, env=env)

    out: dict = {"saved_at": at}
    # The task folder of the ledger clone is the manager's own: what it wrote there (a parked decision, a note) goes with the ledger.
    run_git("add", "--", ".")
    if run_git("diff", "--cached", "--quiet").returncode != 0:
        committed = run_git("commit", "--quiet", "-m", f"{cfg['program']} ledger: {note}")
        if committed.returncode != 0:
            raise Refusal(f"the ledger could not be committed: {committed.stderr.strip()[:300]}")
        out["committed"] = True
    else:
        out["committed"] = False
    if no_token:
        # Nothing is sent on another credential: what is committed goes with the next save that has the token.
        out.update(pushed=False, push_error=f"not pushed, because the loop's own token could not be had: {no_token}"[:400])
    else:
        pushed = run_git("push", "--quiet", "origin", "HEAD")
        if pushed.returncode != 0:
            # Someone else wrote to the ledger branch (the operator answering a parked decision): take that in, then push.
            branch = run_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if branch and branch != "HEAD" and run_git("pull", "--rebase", "--quiet", "origin", branch).returncode == 0:
                out["took_in_remote"] = True
                pushed = run_git("push", "--quiet", "origin", "HEAD")
            else:
                run_git("rebase", "--abort")
        out["pushed"] = pushed.returncode == 0
        if not out["pushed"]:
            out["push_error"] = pushed.stderr.strip()[:300]
    claim.beat(root, pid, procs, note)
    out["heartbeat"] = "stamped"
    # A seat move or a pause (`seat.py`), told at every save as well as at every wait.
    told = seat.notice_for(root)
    if told:
        out["seat"] = told
    size = context_tokens(root)
    limit = cfg.get("handover_tokens")
    out["context_tokens"] = size
    if size is not None and limit:
        out["hand_over"] = size >= limit
        out["hand_over_at"] = limit
    try:
        out["idle"] = ledger.idle_streams(root, cfg.get("streams") or {}, at)
    except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
        out["idle"] = {"errors": [f"could not be read: {exc}"[:300]]}
    try:
        out["clocks"] = clocks.program_clocks(root, ledger_file)
    except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
        out["clocks"] = {"errors": [f"could not be read: {exc}"[:300]]}
    try:
        out["unstarted_files"] = ledger.unstarted_files(root, ledger_file, task / archive.ARCHIVE)
    except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
        out["unstarted_files"] = {"errors": [f"could not be read: {exc}"[:300]]}
    at_kb = cfg.get("ledger_archive_kb")
    if at_kb:
        try:
            out["archive"] = archive.fact(root, ledger_file, at_kb, followup_rows)
        except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
            out["archive"] = {"errors": [f"could not be read: {exc}"[:300]]}
    if archived_error:
        # Said even where the program sets no size: FOLLOW-UPS.md was written whole because of it.
        out.setdefault("archive", {}).setdefault("errors", []).append(archived_error)
    if outside_facts is not None:
        try:
            out["outside_prs_ready"] = outside_facts()
        except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
            out["outside_prs_ready"] = {"errors": [f"could not be read: {exc}"[:300]]}
    if ci_facts is not None:
        try:
            out["ci_time"] = ci_facts()
        except Exception as exc:  # noqa: BLE001 - a fact, never a failed save
            out["ci_time"] = {"errors": [f"could not be read: {exc}"[:300]]}
    return (0 if out["pushed"] else 2), out


# ---------------------------------------------------------------- archive


def archive_ledger(root, pid: int | None, procs, dry_run: bool, out_file=None, swept_file=None) -> tuple[int, dict]:
    """Move the ledger's settled history into LEDGER-ARCHIVE.md and FOLLOW-UPS-ARCHIVE.md (hl/archive.py).

    `dry_run` answers what would move and writes nothing in the task folder; with `out_file` it writes what
    would move there, for the sub-agent that reads it for defects to place before it goes. The move itself is
    the manager's: refused unless the program is claimed by `pid`, like a save, and refused without
    `swept_file`, that sweep file, whose rows and reports are all it moves. The next save commits it."""
    cfg = program.load(root)
    ledger_file = cfg.get("ledger")
    if not ledger_file or not Path(ledger_file).is_file():
        raise Refusal(f"program.json names no ledger ({ledger_file!r}): stage the program again")
    if not dry_run and not swept_file:
        raise Refusal("an archive moves only what a sweep has read: `archive --dry-run --out <file>`, have a sub-agent read the file, then `archive --swept <file>`")
    if dry_run:
        try:
            planned = archive.plan(root, ledger_file)
        except (archive.ArchiveError, OSError, ValueError) as exc:
            raise Refusal(f"what would move could not be worked out: {exc}") from exc
        answer = {"result": "would-move", **archive.public(planned)}
        if out_file:
            try:
                # UTF-8 whatever the locale: the ledger is read so, and `--swept` reads this file so.
                Path(out_file).write_bytes(archive.sweep_text(planned, cfg["program"]).encode("utf-8"))
            except OSError as exc:
                raise Refusal(f"the sweep file could not be written: {exc}") from exc
            answer["out"] = str(out_file)
        return 0, answer
    claim._require_mine(root, pid, procs)
    try:
        return 0, archive.apply(root, ledger_file, cfg["program"], utc_now(), archive.read_swept(swept_file))
    except archive.UndoError as exc:
        raise Refusal(f"the archive failed and could not be undone whole: {exc}. Archive nothing more and tell the operator") from exc
    except (archive.ArchiveError, OSError, ValueError) as exc:
        # Every file written is put back before this (hl/archive.py `_write_all`); unreadable JSON is a ValueError.
        raise Refusal(f"nothing was archived: {exc}") from exc


# ---------------------------------------------------------------- cli


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-flow", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=absolute, required=True, help="the program's loop root")
    p.add_argument("--run-dir", type=absolute, help="the run directory (every verb but save)")
    p.add_argument("--repo-dir", type=absolute, help="the stream's clone of the target repository (default: the one program.json records for the run's stream)")
    p.add_argument("--herdr", help="absolute path of the herdr binary (default: the pinned one)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("prepare", help="worktree, branch, port, contract from the Accept list, and the repository's setup")
    s.add_argument("--id", required=True)
    s.add_argument("--port", help="the item's port, or `none` (default: the stream's port base plus the item's number, when the spec gives one)")
    s.add_argument("--setup-timeout", type=float, default=SETUP_TIMEOUT_S)

    s = sub.add_parser("built", help="check a finished build; close its builder, or send the build back to it")
    s.add_argument("--id", required=True)

    s = sub.add_parser("review", help="serve the build and start its reviewer")
    s.add_argument("--id", required=True)
    s.add_argument("--effort", help="the reviewer's effort (default: the program's)")
    s.add_argument("--full", action="store_true", help="a full review, where the script would give a rework a delta confirm")
    s.add_argument("--note-file", type=absolute, help="a file holding a note for the reviewer, put into the review brief's own slot for one")
    s.add_argument("--setup-timeout", type=float, default=SETUP_TIMEOUT_S)

    s = sub.add_parser("reviewed", help="stop the served build, close the reviewer, read the verdict and post it")
    s.add_argument("--id", required=True)
    s.add_argument("--no-comment", action="store_true", help="do not post the verdict on the pull request")

    s = sub.add_parser("close", help="after the merge: remove the worktree and the local branch")
    s.add_argument("--id", required=True)

    s = sub.add_parser("save", help="generate the ledger tables, commit and push the ledger, stamp the heartbeat")
    s.add_argument("--note", required=True, help="what changed, in a few words")
    s.add_argument("--pid", type=int, help="the manager's pid (default: the nearest ancestor named claude)")

    s = sub.add_parser("archive", help="move the ledger's settled history into LEDGER-ARCHIVE.md and FOLLOW-UPS-ARCHIVE.md")
    s.add_argument("--dry-run", action="store_true", help="say what would move and write nothing in the task folder")
    s.add_argument("--out", type=absolute, help="with --dry-run: write what would move to this file, for the sub-agent that reads it first")
    s.add_argument("--swept", type=absolute, help="the file a dry run wrote with --out and a sub-agent has read: the move takes only what it lists")
    s.add_argument("--pid", type=int, help="the manager's pid (default: the nearest ancestor named claude)")
    return p


def main(argv: list[str] | None = None, herdr=None, host=None, tools=None, procs=None, envs=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "save":
            procs = procs or claim.Procs()
            pid = args.pid or claim.find_manager_pid(procs)
            if pid is None:
                raise Refusal("no ancestor process named 'claude'; pass --pid")
            # The ledger is pushed on the loop's own token, like everything else the kit sends. Without it the save
            # still writes, commits and stamps the heartbeat: a manager that cannot push is not a manager that is gone.
            no_token = None
            try:
                merge.require_loop_token(args.root)
            except Refusal as refusal:
                no_token = str(refusal)
            def outside_facts():
                from . import outside  # here, and inside the read: nothing about it may fail the save

                return outside.ready_or_said(args.root, no_token=no_token)

            def ci_facts():
                from . import pace  # here, and inside the read: nothing about it may fail the save

                return pace.facts_or_said(args.root, no_token=no_token)

            code, result = save(args.root, pid, procs, args.note, no_token=no_token, outside_facts=outside_facts, ci_facts=ci_facts)
            emit(result)
            return code
        if args.cmd == "archive":
            if args.out and not args.dry_run:
                raise Refusal("--out goes with --dry-run")
            if args.swept and args.dry_run:
                raise Refusal("--swept goes without --dry-run: it is the sweep file the move is given back")
            pid = None
            if not args.dry_run:
                procs = procs or claim.Procs()
                pid = args.pid or claim.find_manager_pid(procs)
                if pid is None:
                    raise Refusal("no ancestor process named 'claude'; pass --pid")
            code, result = archive_ledger(args.root, pid, procs, args.dry_run, args.out, args.swept)
            emit(result)
            return code
        if not args.run_dir:
            raise Refusal("--run-dir is required")
        if tools is None:
            merge.require_loop_token(args.root)
            pinned = claim.check_pins(args.root, ("git", "gh"))["bins"]
            tools = merge.Tools(git=pinned["git"], gh=pinned["gh"])
        if args.cmd == "prepare":
            code, result = prepare(args.root, args.run_dir, args.id, tools, args)
            emit(result)
            return code
        if herdr is None:
            if os.environ.get("HERDR_ENV") != "1":
                raise Refusal("not inside a Herdr pane (HERDR_ENV is not 1); workers are only driven from inside Herdr")
            herdr = Herdr(args.herdr or claim.check_pins(args.root, ("herdr",))["bins"]["herdr"])
        host = host or serve.Host()
        if args.cmd == "built":
            code, result = built(args.root, args.run_dir, args.id, tools, herdr, args)
        elif args.cmd == "review":
            code, result = start_review(args.root, args.run_dir, args.id, tools, herdr, host, args, envs=envs)
        elif args.cmd == "reviewed":
            code, result = reviewed(args.root, args.run_dir, args.id, tools, herdr, host, args)
        else:
            code, result = close(args.root, args.run_dir, args.id, tools, herdr, host, args)
    except (Refusal, HerdrError, RuntimeError) as failure:
        reasons = failure.reasons if isinstance(failure, Refusal) else [str(failure)]
        emit({"result": "refused", "refused": reasons})
        print(f"hl-flow: refused: {'; '.join(reasons)}", file=sys.stderr)
        return 2
    emit(result)
    return code


if __name__ == "__main__":
    sys.exit(main())
