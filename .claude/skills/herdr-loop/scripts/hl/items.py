"""Run state: which item is next, how many attempts each phase has used, and
which text is the item's contract.

The manager decides what a failure means. This module only keeps the counts
and refuses what must not happen: a phase past its bound, a build with no
agreed contract, an evaluator session that is also a builder's, an item
marked merged that the merge script did not merge.

Layout under a run directory:

    run.json                      run id, repo, files (with hashes), item order
    items/<ID>/item.md            the item's text, frozen at init
    items/<ID>/item.json          status, depends, verify, contract record
    items/<ID>/attempts.json      counters and a log entry per started phase
    items/<ID>/contract.md        installed by `contract`, never by hand
    items/<ID>/<phase>-<n>.*      written by workers and by the other scripts
    items/<ID>/voided/<phase>-<n>.<k>/   what a voided attempt left, with why
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import backlog, contractfmt
from .common import Refusal, absolute, emit, read_json, sha256_text, utc_now, write_json

TERMINAL = ("merged", "failed", "parked", "skipped")
WORKSPACE_FILE = "workspace.json"
# One bound is not a phase: how many of an item's reviews may be given back
# after they passed (`void_attempt`). It is counted from the attempt log.
GIVE_BACK = "give_back"
BOUNDS = {"agree": 2, "build": 2, "grade": 2, "correction": 1, "ci_rerun": 1, "verify": 2, GIVE_BACK: 1}
# In attempts.json: how many Verify tries were made before the item's line was last corrected (`verify-line`), so not counted.
VERIFY_SET_ASIDE = "verify_set_aside"
# What `start_phase` counts, each in its own number in attempts.json.
COUNTED = tuple(k for k in BOUNDS if k != GIVE_BACK)
# How a run decides whether an item gets an independent premise check before
# its build. `always`: every item does, and a build is refused without one
# (the kit's first behaviour, and what a run without a policy has). `routed`:
# the manager records a route for each item; an independent check runs where
# the route or the item's floor asks for one, and elsewhere the builder plans
# for itself and may dispute a criterion before any review.
PREMISE_POLICIES = ("always", "routed")
ROUTE_PREMISES = ("builder", "independent")
# What an evaluator does with a build. `blind`: it grades the running build
# and may not read the diff (the kit's first behaviour). `code`: it reviews,
# reading the code and exercising the change, and writes a fix list.
REVIEW_STYLES = ("blind", "code")
# Where an item's pull request merges. `item`: into the program's base
# branch, once the repository's required checks are green on it (the kit's
# first behaviour). `train`: into the repository's integration branch, after
# its review and the checks the repository names for an item; the required
# checks run later, on a batch of merged items, and a green batch is
# promoted to the base (`hl-train`).
MERGE_MODES = ("item", "train")
# How a run's pull requests are merged. `squash`: as one commit, which is what an item is. `merge`: with a merge
# commit that keeps the branch's own history, for the one kind of item whose work is a merge (bringing a base
# that conflicts into a train): squashed, the files would agree and the histories would not, and the conflict
# would come back the next time the two are joined.
LANDINGS = ("squash", "merge")
# What reviews a rework. `full`: a full fresh review of the new head (the kit's first behaviour). `delta`:
# where a full review failed with a fix list and the fix since is small and holds nobody else's work, a
# fresh reviewer confirms the fix alone: it answers the fix list entry by entry and is given the change between
# the two heads. `hl/confirm.py` decides when, and the merge gate checks it again. Once for an item.
CONFIRMS = ("full", "delta")
# The kind of a review that is a delta confirm, in the attempt log. A review without a kind is a full one.
CONFIRM = "confirm"
# How large a fix a delta confirm may cover. Past it the fix is new work, and gets a full review.
#   share, lines  the fix may change `share` of the lines in the diff the failed review was given, or `lines`, whichever is
#                 more. A quarter: a fix is narrower than the build it mends, and one that rewrites more
#                 than that leaves little of what the review passed standing. Forty lines: about what is
#                 read whole in one look, and under it a share of a small pull request says nothing.
#   files         the files the fix may touch that the failed review's diff did not: a test for what was
#                 fixed and one caller. More is a fix that has spread.
# No rework ran in the first program, so none of the three is measured: they are where the canary starts.
CONFIRM_BOUND = {"share": 0.25, "lines": 40, "files": 2}
EVALUATOR_PHASES = ("agree", "grade", "correction")
BRAKE_COUNT = 3
# A contract draft is not counted against a bound: it carries the number of
# the agree round that will review it.
DRAFT = "contract-draft"
VOIDED_DIR = "voided"

# What a finished attempt leaves behind. An attempt with none of these never
# produced a result, so it may be voided without spending the bound. One
# with its result stays counted, but for a pass over a changed checkout
# (`void_attempt`).
RESULT_FILES = {
    "agree": "agree-{n}.md",
    "build": "build-{n}.md",
    "grade": "verdict-{n}.json",
    "correction": "correction-{n}.md",
    "verify": "verify-{n}.txt",
    DRAFT: "contract-draft-{n}.md",
}


def item_dir(run_dir, item_id: str) -> Path:
    return Path(run_dir) / "items" / item_id


def load_run(run_dir) -> dict:
    path = Path(run_dir) / "run.json"
    if not path.exists():
        raise Refusal(f"{path} does not exist: run `hl-items init` first")
    return read_json(path)


def policy(run: dict) -> dict:
    """The run's policy, fixed when the run was created. A run from before
    there was one behaves as the kit did then."""
    recorded = run.get("policy") or {}
    return {
        "premise": recorded.get("premise", "always"),
        "review": recorded.get("review", "blind"),
        "bounds": dict(recorded.get("bounds") or {}),
        "merge": recorded.get("merge", "item"),
        "train": dict(recorded.get("train") or {}),
        "confirm": recorded.get("confirm", "full"),
        "confirm_bound": {**CONFIRM_BOUND, **(recorded.get("confirm_bound") or {})} if recorded.get("confirm") == "delta" else {},
        # The protected paths an item of this run may be exempted for (`hl-items exempt`); none for a run created without them.
        "exemptable": list(recorded.get("exemptable") or []),
    }


def on_train(run: dict) -> bool:
    return policy(run)["merge"] == "train"


def base_of(run: dict, program_base: str) -> str:
    """The branch a run's items are built on and merge into: its
    repository's train, or the program's base."""
    return policy(run)["train"]["branch"] if on_train(run) else program_base


def bounds(run: dict) -> dict:
    return {**BOUNDS, **policy(run)["bounds"]}


def check_policy(policy_in: dict | None) -> dict | None:
    if policy_in is None:
        return None
    premise = policy_in.get("premise", "always")
    if premise not in PREMISE_POLICIES:
        raise Refusal(f"premise policy {premise!r} is not one of {', '.join(PREMISE_POLICIES)}")
    review = policy_in.get("review", "blind")
    if review not in REVIEW_STYLES:
        raise Refusal(f"review style {review!r} is not one of {', '.join(REVIEW_STYLES)}")
    given = policy_in.get("bounds") or {}
    bad = [f"{k}={v!r}" for k, v in given.items() if k not in BOUNDS or not isinstance(v, int) or isinstance(v, bool) or v < 1]
    if bad:
        raise Refusal(f"bounds must be whole numbers of at least 1 for {', '.join(BOUNDS)}; got {', '.join(bad)}")
    checked = {"premise": premise, "review": review, "bounds": dict(given)}
    mode = policy_in.get("merge", "item")
    if mode not in MERGE_MODES:
        raise Refusal(f"merge mode {mode!r} is not one of {', '.join(MERGE_MODES)}")
    if mode == "train":
        train = policy_in.get("train") or {}
        checks = train.get("item_checks") or []
        if not isinstance(train.get("branch"), str) or not train["branch"].strip():
            raise Refusal("a run on a train needs the train's branch")
        if not isinstance(checks, list) or not all(isinstance(c, str) and c.strip() for c in checks):
            raise Refusal("`item_checks` must be a list of check names")
        if review != "code":
            # On a train the full checks run after the merge: what stands before it is a review that ran what the change touches.
            raise Refusal("a run on a train needs reviews that read the code and exercise the change (`review: code`)")
        checked.update(merge="train", train={"branch": train["branch"].strip(), "item_checks": [c.strip() for c in checks]})
    confirm = policy_in.get("confirm", "full")
    if confirm not in CONFIRMS:
        raise Refusal(f"confirm {confirm!r} is not one of {', '.join(CONFIRMS)}")
    if confirm == "delta":
        if review != "code":
            # A delta confirm answers a fix list, and only a review that read the code writes one.
            raise Refusal("a delta confirm of a rework needs reviews that read the code and write a fix list (`review: code`)")
        # Written out in full: a run keeps the bound it started with, whatever the kit's own becomes.
        checked.update(confirm="delta", confirm_bound=_confirm_bound(policy_in.get("confirm_bound") or {}))
    exemptable = policy_in.get("exemptable") or []
    if not isinstance(exemptable, list) or not all(isinstance(e, str) and e.strip() for e in exemptable):
        raise Refusal("`exemptable` must be a list of path patterns")
    if exemptable:
        checked["exemptable"] = [e.strip() for e in exemptable]
    return checked


def _confirm_bound(given: dict) -> dict:
    bound = {**CONFIRM_BOUND, **given}
    number = isinstance(bound["share"], (int, float)) and not isinstance(bound["share"], bool) and 0 < bound["share"] <= 1
    whole = all(isinstance(bound[k], int) and not isinstance(bound[k], bool) and bound[k] >= 0 for k in ("lines", "files"))
    if len(bound) != len(CONFIRM_BOUND) or not number or not whole:
        raise Refusal(f"`confirm_bound` takes `share` (above 0, at most 1) and `lines` and `files` (whole numbers, 0 or more); got {given!r}")
    return bound


def load_item(run_dir, item_id: str) -> dict:
    path = item_dir(run_dir, item_id) / "item.json"
    if not path.exists():
        raise Refusal(f"unknown item {item_id}")
    return read_json(path)


def save_item(run_dir, item: dict) -> None:
    write_json(item_dir(run_dir, item["id"]) / "item.json", item)


def load_attempts(run_dir, item_id: str) -> dict:
    path = item_dir(run_dir, item_id) / "attempts.json"
    if not path.exists():
        # Everything a merge rests on is in it, and nothing else holds a copy.
        raise Refusal(f"{path} is missing: it is the record of every attempt of {item_id}, and no command makes it again. Leave the item as it is and tell the operator")
    return read_json(path)


def save_attempts(run_dir, item_id: str, attempts: dict) -> None:
    write_json(item_dir(run_dir, item_id) / "attempts.json", attempts)


# ---------------------------------------------------------------- init


def init_run(
    run_dir,
    run_id: str,
    repo: str,
    files: list[str],
    work: str | None = None,
    policy: dict | None = None,
    stream: str | None = None,
    repair: bool = False,
    land: str = "squash",
) -> dict:
    """`work` names what the run completes, in the operator's words ("Help desk
    portal"). It is the label of the run's herdr workspace. `policy` and
    `stream` are written once, when the run is created: a run keeps the rules
    it started under, whatever the program's settings say later."""
    run_dir = Path(run_dir)
    work = (work or "").strip() or None
    policy = check_policy(policy)
    parsed = []
    owner: dict[str, str] = {}
    for f in files:
        text = Path(f).read_text()
        try:
            items = backlog.parse_backlog(text)
        except backlog.BacklogError as exc:
            raise Refusal(f"{f}: {exc}") from exc
        for it in items:
            if it.id in owner:
                raise Refusal(f"duplicate id {it.id}: in {owner[it.id]} and {f}")
            owner[it.id] = str(f)
        parsed.append((str(Path(f).resolve()), sha256_text(text), items))
    if not owner:
        raise Refusal("no items found in the given files")

    file_records = [{"path": p, "sha256": sha} for p, sha, _ in parsed]
    run_path = run_dir / "run.json"
    if run_path.exists():
        existing = read_json(run_path)
        same = existing.get("run_id") == run_id and [r["sha256"] for r in existing["files"]] == [
            r["sha256"] for r in file_records
        ]
        why = ""
        if not same and existing.get("run_id") == run_id and [r["path"] for r in existing["files"]] == [r["path"] for r in file_records]:
            # The same files, edited since. A corrected Verify line is the one edit a running run takes, and only
            # through `verify-line`, so the run is left as it is; anything else is refused as before.
            try:
                verify_drift(run_dir)
                same = True
            except Refusal as refusal:
                why = f": {refusal}"
        if same:
            if work and work != existing.get("work"):
                opened = run_dir / WORKSPACE_FILE
                if opened.exists() and not read_json(opened).get("closed_at"):
                    raise Refusal(
                        f"the run's workspace is already open as {read_json(opened)['label']!r}; "
                        "a run is renamed before its first worker starts"
                    )
                existing["work"] = work
                write_json(run_path, existing)
            return existing
        raise Refusal(
            "this run is already initialised from different files or under another id; "
            f"a run's files never change once it exists, but for an item's Verify line{why}"
        )

    order: list[str] = []
    for path, _, items in parsed:
        for it in items:
            order.append(it.id)
            d = item_dir(run_dir, it.id)
            d.mkdir(parents=True, exist_ok=True)
            (d / "item.md").write_text(it.text)
            write_json(
                d / "item.json",
                {
                    "id": it.id,
                    "title": it.title,
                    "file": path,
                    "depends_on": it.depends_on,
                    "accept_count": len(it.accept),
                    "verify_command": it.verify_command,
                    "verify_wait_min": it.verify_wait_min,
                    "floor": it.floor,
                    "status": "pending",
                    "note": None,
                    "contract": None,
                    "route": None,
                    "fields": {},
                },
            )
            write_json(d / "attempts.json", {**{k: 0 for k in COUNTED}, "log": []})

    run = {
        "run_id": run_id,
        "work": work,
        "repo": repo,
        "created": utc_now(),
        "files": file_records,
        "order": order,
        "externals_merged": [],
        "events": [],
        "brake_cleared_at": None,
    }
    if policy is not None:
        run["policy"] = policy
    if stream:
        run["stream"] = stream
    if repair:
        # A run whose work is to put a train right. `hl-merge` does not hold the train's limit against it.
        run["repair"] = True
    if land not in LANDINGS:
        raise Refusal(f"a run's pull requests land by {' or '.join(LANDINGS)}, not {land!r}")
    if (repair or land != "squash") and (policy or {}).get("merge") != "train":
        # A run that merges into the base behaves exactly as it always has.
        raise Refusal("--repair and --land are for a run in a repository that has a train")
    if land != "squash":
        run["land"] = land
    # Written last: a crash before this line leaves no run, so init can be repeated.
    write_json(run_path, run)
    return run


# ---------------------------------------------------------------- next


def _counts(items: list[dict]) -> dict:
    counts = {s: 0 for s in ("pending", "in_progress") + TERMINAL}
    for it in items:
        counts[it["status"]] += 1
    return counts


def _reason_class(note: str | None) -> str:
    return (note or "").split(":", 1)[0].strip().lower()


def brake_state(run: dict) -> dict | None:
    """Three items in a row ended failed or parked for the same class of
    reason: stop the stream. An environment fault repeats fast and totally."""
    cleared = run.get("brake_cleared_at") or ""
    tail = [e for e in run["events"] if e["at"] > cleared][-BRAKE_COUNT:]
    if len(tail) < BRAKE_COUNT:
        return None
    if any(e["status"] not in ("failed", "parked") for e in tail):
        return None
    classes = {e["reason_class"] for e in tail}
    if len(classes) != 1 or classes == {""}:
        return None
    return {"reason_class": classes.pop(), "items": [e["id"] for e in tail]}


def next_item(run_dir) -> dict:
    run = load_run(run_dir)
    items = [load_item(run_dir, i) for i in run["order"]]
    by_id = {it["id"]: it for it in items}
    out = {"next": None, "counts": _counts(items)}

    brake = brake_state(run)
    if brake:
        return {**out, "state": "brake", **brake}

    in_progress = [it for it in items if it["status"] == "in_progress"]
    if in_progress:
        return {**out, "state": "resume", "next": in_progress[0]["id"]}

    externals = set(run["externals_merged"])
    blocked = []
    for it in items:
        if it["status"] != "pending":
            continue
        unmet = []
        for dep in it["depends_on"]:
            if dep in externals:
                continue
            status = by_id[dep]["status"] if dep in by_id else "not-in-this-run"
            # A failed item skipped as superseded: set only once its repair shipped, so its work is on the base.
            if status == "skipped" and by_id[dep].get("superseded_by"):
                continue
            if status != "merged":
                unmet.append({"id": dep, "status": status})
        if not unmet:
            return {**out, "state": "ready", "next": it["id"]}
        blocked.append({"id": it["id"], "unmet": unmet})

    if blocked:
        return {**out, "state": "blocked-dependencies", "blocked": blocked}
    return {**out, "state": "complete"}


# ---------------------------------------------------------------- start / void


def _agreed_contract(run_dir, item: dict, attempts: dict) -> str | None:
    """None when the installed contract is agreed; otherwise why not."""
    d = item_dir(run_dir, item["id"])
    record = item.get("contract")
    if record is None or not (d / "contract.md").exists():
        return "no contract installed"
    rounds = [e for e in attempts["log"] if e["phase"] == "agree" and not e.get("voided")]
    if not rounds:
        return "no agree round has run"
    latest = rounds[-1]["n"]
    result = d / f"agree-{latest}.md"
    if not result.exists():
        return f"agree-{latest}.md is not written yet"
    word = contractfmt.final_word(result.read_text())
    if word == "AGREED":
        if record["source"] == "draft" and record.get("n") != latest:
            return f"agree-{latest}.md agreed a draft that is not the installed contract"
        return None
    if word == "REVISE":
        # The round's amended contract, or a correction installed after it, is the contract it asked for. A correction
        # counts only if its attempt started after the round: one written before it corrected a contract the round never saw.
        installs = record.get("history", []) + [record]
        revised = [i for i, r in enumerate(installs) if r["source"] == "amended" and r.get("file") == result.name]
        since = attempts["log"][attempts["log"].index(rounds[-1]) + 1 :]
        later = {f"correction-{e['n']}.md" for e in since if e["phase"] == "correction" and not e.get("voided")}
        if revised and all(r["source"] == "amended" and r.get("file") in later for r in installs[revised[-1] + 1 :]):
            return None
        return f"agree-{latest}.md says REVISE and its amended contract is not installed"
    return f"agree-{latest}.md does not end with AGREED or REVISE"


def contract_problem(run_dir, item_id: str) -> str | None:
    """Why the item's contract.md is not the contract that was installed,
    or None when it is. A contract is changed only by installing one
    (`hl-items contract`), which keeps the text's digest on the item; a
    builder can write the file, and a review pins whatever it finds there."""
    record = load_item(run_dir, item_id).get("contract") or {}
    path = item_dir(run_dir, item_id) / "contract.md"
    if path.exists() and sha256_text(path.read_text()) == record.get("sha256"):
        return None
    return (
        f"contract.md of {item_id} is not the contract that was installed: something wrote the file, and a contract is changed only by installing one. "
        f"Put the installed one back (`hl-items contract --id {item_id} --restore`)"
    )


def bound_reached(run_dir, item_id: str, phase: str) -> str | None:
    """Why one more attempt of this phase is past its bound, or None when it
    is not. Reads only; `start_phase` is what counts an attempt."""
    attempts = load_attempts(run_dir, item_id)
    limits = bounds(load_run(run_dir))
    if phase == "grade":
        live = [e for e in attempts["log"] if not e.get("voided")]
        used = sum(1 for e in live if e["phase"] == "grade" and e["build"] == attempts["build"])
        # A correction buys exactly one more grade of the build it corrected.
        bound = limits["grade"] + attempts["correction"]
        if used >= bound:
            said = f"grade bound reached for build {attempts['build']} of {item_id}: {used} of {bound}"
            latest = [e for e in live if e["phase"] == "grade"][-1]
            if changed_pass(latest) and not bound_reached(run_dir, item_id, GIVE_BACK):
                # Not a dead end: a pass over a checkout that changed under it was no review of the head.
                said += f". Review {latest['n']} passed over a checkout that changed under it and may be given back: {void_command(item_id, latest['n'])}"
            return said
        return None
    if phase not in limits:
        return None
    used = counted(attempts, phase)
    if used >= limits[phase]:
        return f"{phase} bound reached for {item_id}: {used} of {limits[phase]}"
    return None


def counted(attempts: dict, phase: str) -> int:
    """How many attempts of this phase count against its bound. Verify
    tries made with a line the operator corrected since (`verify-line`) are
    set aside: they tested nothing the corrected line checks. They keep
    their numbers and results; only the bound stops counting them."""
    if phase == GIVE_BACK:
        return len(given_back(attempts))
    if phase == "verify":
        return max(0, attempts["verify"] - attempts.get(VERIFY_SET_ASIDE, 0))
    return attempts[phase]


def given_back(attempts: dict) -> list[dict]:
    """An item's reviews that were given back after they passed
    (`void_attempt`). Read from the attempt log, where each stays, marked:
    a manager that took the program over finds the same count."""
    return [e for e in attempts["log"] if (e.get("voided") or {}).get("given_back")]


def changed_pass(review: dict) -> bool:
    """Whether a review is a pass whose first read found its checkout moved
    or changed under it: the one attempt with a result that may be given
    back. A fail or an inconclusive over such a checkout stands as what it
    is, and the build goes back as after any other. The word is the one
    that first read took from the verdict and kept on the attempt: the
    file can be rewritten afterwards by anything that can write the run
    directory."""
    read = review.get("read") or {}
    return bool(read.get("checkout")) and read.get("effective") == "PASS"


def void_command(item_id: str, n: int) -> str:
    """In the short form every refusal of the kit writes a command in: the manager gives each line its `--run-dir`."""
    return f'`hl-items void --id {item_id} --phase grade --n {n} --note "<what changed the checkout>"`'


def changed_pass_next(run_dir, item_id: str, n: int) -> str:
    """What follows a pass that was read over a changed checkout, with the
    command: the attempt is given back and the head reviewed again, or,
    once the item's give-backs are used, the item is parked. Twice running
    is no reviewer's slip: something rewrites a tracked file whenever the
    build is reviewed, and only a change to the repository ends that."""
    past = bound_reached(run_dir, item_id, GIVE_BACK)
    if past:
        return (
            f"{past}, so it is not given back again. Something changes the checkout whenever this item is reviewed: park it for the operator "
            f'(`hl-items set --id {item_id} --status parked --note "environment: <what changes the checkout during a review>"`; '
            'troubles.md, "A tracked file changes during every review")'
        )
    return (
        f"it never was a review of the head, so give the attempt back ({void_command(item_id, n)}) and review the head again "
        f'(`hl-flow review --id {item_id}`; troubles.md, "A review that does not count")'
    )


def disputed_criteria(run_dir, item_id: str) -> list[int]:
    """The criteria the latest build's report disputes. A builder that finds
    a criterion wrong builds what the item asks and says so there."""
    n = load_attempts(run_dir, item_id)["build"]
    report = item_dir(run_dir, item_id) / f"build-{n}.md"
    if not n or not report.exists():
        return []
    return contractfmt.disputed(report.read_text())


def route_item(run_dir, item_id: str, premise: str, look: bool, reason: str) -> dict:
    """Record how the manager routes an item: whether an independent premise
    check runs before the build, and whether what it changes is something a
    person looks at. The choice is the manager's; a route below the item's
    floor is refused."""
    if premise not in ROUTE_PREMISES:
        raise Refusal(f"--premise is one of {', '.join(ROUTE_PREMISES)}, got {premise!r}")
    if not (reason or "").strip():
        raise Refusal("--reason is required: say in a line why the item is routed this way")
    item = load_item(run_dir, item_id)
    if item["status"] in TERMINAL:
        raise Refusal(f"{item_id} is {item['status']}; it is not routed any more")
    floor = item.get("floor") or []
    below = []
    if "premise" in floor and premise != "independent":
        below.append("the item's floor asks for an independent premise check (`--premise independent`)")
    if "look" in floor and not look:
        below.append("the item's floor asks for a look in a real browser (`--look yes`)")
    if policy(load_run(run_dir))["premise"] == "always" and premise != "independent":
        below.append("this run gives every item an independent premise check (`--premise independent`)")
    if below:
        raise Refusal([f"{item_id} cannot be routed below what is required: {why}" for why in below])
    fields = item.get("fields") or {}
    if look and fields.get("worktree") and not str(fields.get("port") or "").isdigit():
        # A look is of a served build, and a build is served on the item's port. Prepared with none, its builder is told
        # it serves nothing, writes `Serve: true`, and the build is sent back at `built`: a whole build spent.
        raise Refusal(
            f"{item_id} was prepared with no port (its stream has no `port_base`, or it was prepared with `--port none`), and a look "
            f"is of a served build: give it a port first (`hl-flow prepare --id {item_id} --port <n>`, one no other stream uses), then route it again"
        )
    current = item.get("route") or {}
    built = any(e["phase"] == "build" and not e.get("voided") for e in load_attempts(run_dir, item_id)["log"])
    if current.get("look") and not look and built:
        # Otherwise a review that did not look could be made good after the fact by routing the look away.
        raise Refusal(f"{item_id} is routed with a look and is already being built; a look is not dropped after that")
    route = {"premise": premise, "look": bool(look), "reason": reason.strip(), "at": utc_now()}
    if item.get("route"):
        item.setdefault("route_history", []).append(item["route"])
    item["route"] = route
    save_item(run_dir, item)
    return item


def _build_premise(run_dir, item: dict, attempts: dict, run: dict) -> str | None:
    """None when the item's premise is settled for a build; otherwise why not."""
    floor = item.get("floor") or []
    route = item.get("route")
    if policy(run)["premise"] == "routed":
        if not route:
            return (
                "it has no route: record one with `hl-items route --id "
                f"{item['id']} --premise builder|independent --look yes|no --reason <why>`"
            )
        if route["premise"] != "independent" and "premise" not in floor:
            # The builder plans for itself and may dispute a criterion; the contract is the item's Accept list.
            if item.get("contract") is None or not (item_dir(run_dir, item["id"]) / "contract.md").exists():
                return "no contract installed (`hl-items contract --from-accept`)"
            return None
    return _agreed_contract(run_dir, item, attempts)


def start_phase(
    run_dir,
    item_id: str,
    phase: str,
    session: str | None = None,
    engine: str | None = None,
    head_sha: str | None = None,
    drafter_session: str | None = None,
    effort: str | None = None,
    why: str | None = None,
    confirm: dict | None = None,
    baseline: dict | None = None,
    how_run: str | None = None,
    notes: dict | None = None,
) -> dict:
    """`confirm`, for a review that is a delta confirm: the review it follows,
    the commit that review was of, and the digest of that review's verdict.
    Only `hl-spawn` passes one, from what `hl/confirm.py` decided.
    `baseline`, for any review: where the checkout was and which tracked
    files differed from that commit when the review was started."""
    if phase not in COUNTED:
        raise Refusal(f"unknown phase {phase!r}; one of {', '.join(COUNTED)}")
    item = load_item(run_dir, item_id)
    run = load_run(run_dir)
    routed = policy(run)["premise"] == "routed"
    # The deploy check is the one phase that runs after the merge.
    if item["status"] in TERMINAL and not (phase == "verify" and item["status"] == "merged"):
        raise Refusal(f"{item_id} is {item['status']}; it takes no further phases")
    if phase == "verify" and item["status"] != "merged":
        raise Refusal(f"{item_id} is {item['status']}; the deploy check runs only after the merge")
    attempts = load_attempts(run_dir, item_id)
    live = [e for e in attempts["log"] if not e.get("voided")]
    builder_sessions = {e["session"] for e in live if e["phase"] == "build"} | {
        e["drafter_session"] for e in live if e.get("drafter_session")
    }
    # A review that was given back after it passed is still an evaluation by its session.
    evaluator_sessions = {e["session"] for e in live + given_back(attempts) if e["phase"] in EVALUATOR_PHASES}

    if phase in EVALUATOR_PHASES or phase == "build":
        if not session:
            raise Refusal(f"--session is required for phase {phase}")
    if phase in EVALUATOR_PHASES:
        if session in builder_sessions or session == drafter_session:
            raise Refusal(f"session {session} has built or drafted for this item; it cannot evaluate")
        if session in evaluator_sessions:
            raise Refusal(f"session {session} already evaluated this item; every evaluation is a fresh session")
    if phase == "build" and session in evaluator_sessions:
        raise Refusal(f"session {session} has evaluated this item; it cannot build")
    if drafter_session and drafter_session in evaluator_sessions:
        raise Refusal(f"session {drafter_session} has evaluated this item; it cannot draft")

    entry = {"phase": phase, "at": utc_now()}
    if session:
        entry["session"] = session
    if engine:
        entry["engine"] = engine
    if drafter_session:
        entry["drafter_session"] = drafter_session
    if notes:
        # What lay below the script's own brief when it was sent: how much, and as one value.
        entry["brief_notes"] = notes
    if effort:
        entry["effort"] = effort
    if why:
        entry["why"] = why

    if phase == "build":
        unsettled = _build_premise(run_dir, item, attempts, run)
        if unsettled:
            raise Refusal(f"cannot build {item_id}: {unsettled}")
        if engine == "cursor" and "sonnet" in (item.get("floor") or []):
            raise Refusal(f"cannot build {item_id} with Cursor: the item's floor asks for a Sonnet builder from the first build")
        if routed and not (why or "").strip():
            raise Refusal(f"cannot build {item_id}: say in a line why this builder and effort (`--why`)")
    if phase == "grade":
        if attempts["build"] == 0:
            raise Refusal(f"cannot grade {item_id}: no build has started")
        if not head_sha:
            raise Refusal("--head-sha is required for phase grade")
        rewritten = contract_problem(run_dir, item_id)
        if rewritten:
            raise Refusal(f"cannot grade {item_id}: {rewritten}")
        contract_path = item_dir(run_dir, item_id) / "contract.md"
        try:
            contractfmt.parse_criteria(contract_path.read_text())
        except (OSError, contractfmt.ContractError) as exc:
            raise Refusal(f"cannot grade {item_id}: contract.md does not parse: {exc}") from exc
        entry["head_sha"] = head_sha
        entry["contract_sha"] = sha256_text(contract_path.read_text())
        if confirm:
            entry.update(kind=CONFIRM, confirms=confirm["confirms"], from_sha=confirm["from_sha"], verdict_sha=confirm["verdict_sha"])
        if baseline is not None:
            entry["baseline"] = baseline
        if how_run:
            # What the build's report said to serve and how to tell it is up, as one value: a rework that changes either is no small fix.
            entry["how_run"] = how_run
    elif phase == "correction":
        graded = any(e["phase"] == "grade" for e in live)
        if not graded and not (routed and disputed_criteria(run_dir, item_id)):
            also = " and its latest build report disputes no criterion" if routed else ""
            raise Refusal(f"cannot correct {item_id}: nothing has been graded{also}")
    past = bound_reached(run_dir, item_id, phase)
    if past:
        raise Refusal(past)

    attempts[phase] += 1
    entry["n"] = attempts[phase]
    entry["build"] = attempts["build"]
    attempts["log"].append(entry)
    save_attempts(run_dir, item_id, attempts)
    if item["status"] == "pending":
        item["status"] = "in_progress"
        item["started_at"] = entry["at"]
        save_item(run_dir, item)
    return entry


def record_read(run_dir, item_id: str, n: int, read: dict) -> dict:
    """Keep the first read of a review's checkout on the review's attempt,
    and return the read that is on record. Written once: a later read finds
    a checkout that may have been put back, and changes nothing here. The
    attempt log is what the merge gate already trusts for who reviewed
    what; a file beside it could be deleted and written again. Called by
    `hl-flow reviewed` and by nothing else."""
    attempts = load_attempts(run_dir, item_id)
    entry = next((e for e in attempts["log"] if e["phase"] == "grade" and e["n"] == n and not e.get("voided")), None)
    if entry is None:
        raise Refusal(f"{item_id} has no review {n}")
    if "read" not in entry:
        entry["read"] = {**read, "at": utc_now()}
        save_attempts(run_dir, item_id, attempts)
    return entry["read"]


def _leftovers(d: Path, phase: str, n: int) -> list[Path]:
    """Everything one attempt has in the item directory: its brief, its
    worker record, its done file, and whatever its worker wrote."""
    left = sorted(d.glob(f"{phase}-{n}.*"))
    evidence = d / f"evidence-{n}"
    if phase == "grade":
        # A review that is given back after it wrote a verdict (`void_attempt`) leaves that too, and what was made of it.
        left += sorted(d.glob(f"verdict-{n}.*")) + sorted(d.glob(f"review-{n}.json"))
    if phase == "grade" and evidence.exists():
        left.append(evidence)
    return left


def _set_aside(d: Path, phase: str, n: int, voided: dict) -> str | None:
    """Move what a voided attempt left out of the way, so the next attempt
    under the same number starts with nothing of it: no record of a worker
    that is gone, no half-collected evidence, no brief filled from old values."""
    kept = []
    for path in _leftovers(d, phase, n):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
        else:
            kept.append(path)
    if not kept:
        return None
    k = 1
    while (d / VOIDED_DIR / f"{phase}-{n}.{k}").exists():
        k += 1
    target = d / VOIDED_DIR / f"{phase}-{n}.{k}"
    target.mkdir(parents=True)
    for path in kept:
        path.rename(target / path.name)
    write_json(target / "voided.json", {"phase": phase, "n": n, **voided})
    return f"{VOIDED_DIR}/{target.name}"


def void_attempt(run_dir, item_id: str, phase: str, n: int | None, note: str) -> dict:
    """Give back an attempt, and set aside what it left so that the same
    number can be started again. Two kinds of attempt are given back: one
    that never produced a result (a dead pane, a usage limit, a CI
    outage), as often as that happens; and a review that passed over a
    checkout that changed under it, `give_back` times for an item. Any
    other attempt whose result file exists is refused: a result is an
    attempt, whatever it says. Without `n`, the latest attempt of the
    phase.

    One result is not a result of the thing it was asked about: a review
    that passed, and whose first read found its checkout moved or changed
    under it (`read` on the attempt, written by `hl-flow reviewed`). That
    pass never was a pass of the head, the merge gate refuses it, and it
    may be given back: the build gets the review again. Only a pass, and
    only `give_back` times for an item (a bound of the run, 1 unless its
    program says otherwise): whoever changes a tracked file before a
    review is read could otherwise have any review erased and run again,
    a fail among them, without end. A given-back review stays in the log,
    marked, and still counts as what it was for everything but the
    build's reviews: its session has evaluated the item, and a delta
    confirm that was given back was the item's one confirm."""
    if not note:
        raise Refusal("--note is required to void an attempt")
    d = item_dir(run_dir, item_id)
    attempts = load_attempts(run_dir, item_id)
    drafting = phase == DRAFT
    latest = attempts["agree"] + 1 if drafting else attempts[phase]
    n = latest if n is None else n
    finishing = False
    if drafting:
        if n != latest or not _leftovers(d, phase, n):
            raise Refusal(f"{item_id} has no contract draft {n} to void")
        entry = {"phase": phase, "n": n}
    else:
        same = [e for e in attempts["log"] if e["phase"] == phase and e["n"] == n]
        live = [e for e in same if not e.get("voided")]
        if live and n != latest:
            raise Refusal(f"only the latest {phase} attempt ({latest}) can be voided")
        if live:
            entry = live[-1]
        elif same and n == latest + 1 and "set_aside" not in same[-1]["voided"] and _leftovers(d, phase, n):
            # Voided already, by a kit from before files were set aside (its
            # record says nothing of them) or by a give-back that was cut
            # short, and what it left is still under its number: finish
            # that void.
            entry, finishing = same[-1], True
        else:
            raise Refusal(f"{item_id} has no live {phase} attempt {n}" + (f"; the latest is {latest}" if latest else ""))
    result = d / RESULT_FILES[phase].format(n=n) if phase in RESULT_FILES else None
    giving_back = False
    # A give-back that was cut short has its mark, and its verdict still in place: it is only finished.
    marked = finishing and entry["voided"].get("given_back")
    there = result is not None and result.exists()
    # A review that `hl-flow reviewed` read has a result, whether or not its file is still there: the file is a worker's to delete.
    if (there or entry.get("read") is not None) and not marked:
        counted = f"{result.name} exists: that attempt produced a result" if there else f"{phase} {n} was read (`hl-flow reviewed`), so it produced a result whether or not {result.name} is still there"
        counted += "" if drafting else " and stays counted"
        if not (entry.get("read") or {}).get("checkout"):
            raise Refusal(counted)
        if not changed_pass(entry):
            raise Refusal(
                f"{counted}. Its checkout changed under it, and only a pass over such a checkout is given back: "
                "a review that did not pass stands as what it is"
            )
        if bound_reached(run_dir, item_id, GIVE_BACK):
            raise Refusal(f"{counted}. Review {n} passed over a checkout that changed under it; {changed_pass_next(run_dir, item_id, n)}")
        giving_back = True
    worker = d / f"{phase}-{n}.worker.json"
    if worker.exists() and not read_json(worker).get("closed_at"):
        # Set aside while open, its record would no longer stop a second worker on the item.
        raise Refusal(f"{phase} {n} of {item_id} still has its worker open; close it with `hl-spawn close` first")
    if finishing:
        entry["voided"]["set_aside"] = _set_aside(d, phase, n, entry["voided"])
        save_attempts(run_dir, item_id, attempts)
        return entry
    voided = {"at": utc_now(), "note": note}
    if giving_back:
        # A review with a verdict: its mark and the count are written before its files move. Stopped in between, it is on
        # record as given back and the same command finishes the rest. The other way round its verdict would be gone while
        # it was still live, and it could be voided as an attempt that never produced a result, outside the bound.
        entry["voided"] = {**voided, "given_back": True}
        attempts[phase] -= 1
        save_attempts(run_dir, item_id, attempts)
        entry["voided"]["set_aside"] = _set_aside(d, phase, n, entry["voided"])
        save_attempts(run_dir, item_id, attempts)
        return entry
    # The files move before the count changes: stopped in between, the attempt is still live and can be voided again.
    voided["set_aside"] = _set_aside(d, phase, n, voided)
    entry["voided"] = voided
    if not drafting:
        attempts[phase] -= 1
        save_attempts(run_dir, item_id, attempts)
    return entry


# ---------------------------------------------------------------- status


def shipped_problem(run_dir, item_id: str) -> str | None:
    """Why an item's work has not shipped, or None when it has: merged,
    promoted where its run is on a train, and its Verify recorded as passed
    where it has one."""
    item = load_item(run_dir, item_id)
    if item["status"] != "merged":
        return f"{item_id} is {item['status']}"
    if on_train(load_run(run_dir)) and not item["fields"].get("promoted"):
        return f"{item_id} is merged into the train and its batch has not been promoted"
    if item.get("verify_command") and item["fields"].get("verify") not in VERIFY_SETTLED:
        return f"{item_id} is merged but its Verify is not recorded as passed (field verify={item['fields'].get('verify')})"
    return None


def set_status(
    run_dir, item_id: str, status: str, note: str | None, reopen: bool = False, superseded_by: str | None = None, superseded_run: str | None = None
) -> dict:
    item = load_item(run_dir, item_id)
    current = item["status"]
    d = item_dir(run_dir, item_id)
    stamp = f"{status}_at"

    if status == "merged":
        merge_record = d / "merge.json"
        if current != "in_progress":
            raise Refusal(f"{item_id} is {current}; only an item in progress can become merged")
        if not merge_record.exists() or read_json(merge_record).get("state") != "MERGED":
            raise Refusal(f"{item_id}: merge.json does not record a merge; only hl-merge makes an item merged")
    elif status in ("failed", "parked"):
        if current != "in_progress":
            raise Refusal(f"{item_id} is {current}; only an item in progress can become {status}")
        if not note:
            raise Refusal(f"--note is required to mark an item {status} (write it as 'class: detail')")
    elif status == "skipped" and superseded_by:
        # A failed item whose work a repair item carried and shipped: nothing of it is left to do, so its run can close.
        if current != "failed":
            raise Refusal(f"{item_id} is {current}; only a failed item is set skipped as superseded")
        if not note:
            raise Refusal("--note is required to skip an item")
        other = Path(superseded_run) if superseded_run else Path(run_dir)
        if not (other / "run.json").exists():
            raise Refusal(f"{other} is not a run directory")
        if superseded_by == item_id and other.resolve() == Path(run_dir).resolve():
            raise Refusal(f"{item_id} cannot supersede itself")
        problem = shipped_problem(other, superseded_by)
        if problem:
            raise Refusal(f"{item_id} is set skipped as superseded only once the item that replaces it has shipped: {problem}")
        item["superseded_by"] = {"id": superseded_by, "run_dir": str(other.resolve())}
    elif status == "skipped":
        if current != "pending":
            raise Refusal(f"{item_id} is {current}; only a pending item can be skipped, or a failed one with --superseded-by")
        if not note:
            raise Refusal("--note is required to skip an item")
    elif status == "pending":
        started = any(not e.get("voided") for e in load_attempts(run_dir, item_id)["log"])
        # `pending` with attempts behind it is how a kit before this one left a reopened item; reopening again puts it right.
        if not reopen or not (current in ("failed", "parked", "skipped") or (current == "pending" and started)):
            raise Refusal(f"{item_id} is {current}; reopening needs --reopen on a failed, parked or skipped item")
        if not note:
            raise Refusal("--note is required to reopen an item")
        # An item that had started goes on from where it stopped: its build can
        # be served and graded, its PASS merged. Only one that never started
        # waits its turn again.
        status = "in_progress" if started else "pending"
        stamp = "reopened_at"
    else:
        raise Refusal(f"cannot set status {status!r}")
    if superseded_by and status != "skipped":
        raise Refusal("--superseded-by goes only with --status skipped")

    item["status"] = status
    item["note"] = note
    item[stamp] = utc_now()
    save_item(run_dir, item)

    run = load_run(run_dir)
    run["events"].append({"id": item_id, "status": status, "reason_class": _reason_class(note), "at": item[stamp]})
    write_json(Path(run_dir) / "run.json", run)
    return item


# Fields a script records from what it saw itself. Typed by hand they would be a claim, not a record.
RECORDED_FIELDS = {
    "verify": "hl-verify records it from the Verify command's own exit status",
    "promoted": "hl-train records it when the batch that holds the item's merge is merged into the base",
    "promoted_sha": "hl-train records it with the promotion",
    "promoted_at": "hl-train records it with the promotion",
}
# `fixed-forward`: the deploy this item's merge broke was put right by a later item, whose own Verify passed.
VERIFY_RESULTS = ("verified", "verify-failed", "fixed-forward")
# A merge whose deploy check ended one of these ways holds nothing else up.
VERIFY_SETTLED = ("verified", "fixed-forward")


def set_fields(run_dir, item_id: str, pairs: list[str]) -> dict:
    item = load_item(run_dir, item_id)
    for pair in pairs:
        if "=" not in pair:
            raise Refusal(f"--field takes key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        if key in RECORDED_FIELDS:
            raise Refusal(f"the field {key!r} is not set by hand: {RECORDED_FIELDS[key]}")
        item["fields"][key] = value
    save_item(run_dir, item)
    return item


def record_verify(run_dir, item_id: str, result: str) -> dict:
    """What the deploy check gave. Called by `hl-verify` and by nothing else."""
    if result not in VERIFY_RESULTS:
        raise Refusal(f"a Verify ends {', '.join(VERIFY_RESULTS[:-1])} or {VERIFY_RESULTS[-1]}, not {result!r}")
    item = load_item(run_dir, item_id)
    if item["status"] != "merged":
        raise Refusal(f"{item_id} is {item['status']}; only a merged item has a deploy check")
    item["fields"]["verify"] = result
    item["fields"]["verify_at"] = utc_now()
    save_item(run_dir, item)
    return item


def _items_in_files(run: dict) -> dict:
    """The run's items as its files have them now, by id. Refused where the
    files no longer hold the run's items in the run's order."""
    found: dict = {}
    for record in run["files"]:
        path = Path(record["path"])
        if not path.exists():
            raise Refusal(f"{path}, one of this run's files, is missing")
        try:
            parsed = backlog.parse_backlog(path.read_text())
        except backlog.BacklogError as exc:
            raise Refusal(f"{path}: {exc}") from exc
        for it in parsed:
            found[it.id] = it
    if list(found) != run["order"]:
        raise Refusal(f"the run's files now hold {', '.join(found) or 'no items'}, where the run has {', '.join(run['order'])}")
    return found


def verify_drift(run_dir) -> list[str]:
    """The items whose Verify line in the run's files differs from the one the
    run has, in run order.

    Refused where anything else differs: which items there are, their order,
    or any of an item's text outside its Verify block. That text is what the
    item's contract and briefs were made from, and it never changes under a
    run; a deploy check can be wrong in a way only a real deploy shows."""
    run = load_run(run_dir)
    now = _items_in_files(run)
    changed, drifted = [], []
    for item_id in run["order"]:
        frozen = (item_dir(run_dir, item_id) / "item.md").read_text()
        if backlog.without_verify(now[item_id].text) != backlog.without_verify(frozen):
            changed.append(item_id)
            continue
        item = load_item(run_dir, item_id)
        if (now[item_id].verify_command, now[item_id].verify_wait_min) != (item.get("verify_command"), item.get("verify_wait_min")):
            drifted.append(item_id)
    if changed:
        raise Refusal(f"the text of {', '.join(changed)} in the run's files differs from the run's outside the Verify line")
    return drifted


def reload_verify(run_dir, item_id: str, note: str) -> dict:
    """Load an item's corrected Verify line from its file: the command, and
    its `Wait.`.

    For a deploy check that is itself wrong (it reads a log its deploy prints
    differently, say), corrected in the backlog file by whoever owns that
    file. Refused for a check that has passed or been settled, while one is
    running, and where the file differs from the run anywhere but in Verify
    lines. The line the item had is kept in `verify_history`. A Verify
    recorded as failed is then run again with `hl-verify --retry`."""
    if not (note or "").strip():
        raise Refusal("--note is required: say what was wrong with the Verify line")
    load_item(run_dir, item_id)
    from . import verify  # here, not at the top: verify.py imports this module

    # The lock `hl-verify` holds while it runs: not beside one, which would record the old line's result.
    with verify.Lock(item_dir(run_dir, item_id), verify.TRY_TIMEOUT_S + verify.INTERVAL_S + 300):
        return _reload_verify(run_dir, item_id, note.strip())


def _reload_verify(run_dir, item_id: str, note: str) -> dict:
    item = load_item(run_dir, item_id)
    recorded = item["fields"].get("verify")
    if recorded in VERIFY_SETTLED:
        raise Refusal(f"{item_id}'s Verify is recorded {recorded}: a deploy check that has settled is not rewritten")
    live = [e for e in load_attempts(run_dir, item_id)["log"] if e["phase"] == "verify" and not e.get("voided")]
    if live and not (item_dir(run_dir, item_id) / RESULT_FILES["verify"].format(n=live[-1]["n"])).exists():
        raise Refusal(
            f"{item_id}'s verify-{live[-1]['n']} has no result yet: let `hl-verify` finish it, "
            "or give it back with `void --phase verify` if it died"
        )
    if item_id not in verify_drift(run_dir):
        raise Refusal(f"{item['file']} has the Verify line {item_id} already has: correct the line in that file first")
    run = load_run(run_dir)
    new = _items_in_files(run)[item_id]
    if not new.verify_command or not item.get("verify_command"):
        raise Refusal(f"{item_id}: a Verify line is corrected here, never added or taken away")
    at = utc_now()
    # Tries made with the line that was wrong tested nothing the corrected line checks, so they do not count against its bound
    # (an item of the help-desk catalog program: both tries were spent on a wrong workflow name, and the corrected line
    # could then not be run once).
    attempts = load_attempts(run_dir, item_id)
    set_aside = counted(attempts, "verify")
    was = {"command": item["verify_command"], "wait_min": item.get("verify_wait_min")}
    item.setdefault("verify_history", []).append({**was, "replaced_at": at, "note": note, "attempts_set_aside": set_aside})
    item["verify_command"], item["verify_wait_min"] = new.verify_command, new.verify_wait_min
    save_item(run_dir, item)
    attempts[VERIFY_SET_ASIDE] = attempts["verify"]
    save_attempts(run_dir, item_id, attempts)
    if not verify_drift(run_dir):
        # The run and its files agree again, so the files are recorded as they are now.
        for record in run["files"]:
            record["sha256"] = sha256_text(Path(record["path"]).read_text())
    run["events"].append({"id": item_id, "status": "verify-line-reloaded", "reason_class": "", "at": at, "note": note})
    write_json(Path(run_dir) / "run.json", run)
    return {
        "id": item_id,
        "verify_command": item["verify_command"],
        "verify_wait_min": item["verify_wait_min"],
        "was": was,
        "attempts_set_aside": set_aside,
        "verify": recorded,
        "next": "hl-verify … --retry --note <what was corrected>" if recorded == "verify-failed" else "hl-verify as usual, once the item has merged",
    }


def record_promoted(run_dir, item_id: str, batch: int, sha: str, at: str) -> dict:
    """The batch that took this item's merge from its train to the base. Called by `hl-train` and by nothing else."""
    item = load_item(run_dir, item_id)
    if item["status"] != "merged":
        raise Refusal(f"{item_id} is {item['status']}; only a merged item can be promoted")
    item["fields"].update(promoted=str(batch), promoted_sha=sha, promoted_at=at)
    save_item(run_dir, item)
    return item


# Characters that make a path a pattern. An exemption names one path, never a set of them. A bracket is not among
# them: `app/[id]/page.tsx` is a file's name, and an exemption is matched as the exact path, never read as a pattern.
PATTERN_CHARS = set("*?{}!")


def exemptions(item: dict) -> dict:
    """The protected paths this one item may change, each with the
    manager's reason: `{path: {"reason", "at", "granted_by"}}`. Empty unless
    `hl-items exempt` recorded one. A record of any other shape is refused,
    never read as far as it goes: it was not written by `exempt`."""
    raw = item.get("protected_exemptions")
    if raw is None:
        return {}
    bad = (
        not isinstance(raw, dict)
        or any(not isinstance(path, str) or not path.strip() for path in raw)
        or any(not isinstance(record, dict) or not isinstance(record.get("reason"), str) or not record["reason"].strip() for record in raw.values())
    )
    if bad:
        raise Refusal(
            f"{item.get('id')}'s protected_exemptions in item.json is not a record `hl-items exempt` wrote "
            f"(a map of file paths to their reason, got {str(raw)[:120]!r}): nothing is exempted from it. Put it back with `hl-items exempt`"
        )
    return {path: dict(record) for path, record in raw.items()}


def granted(run: dict, item: dict) -> dict:
    """The exemptions that count for this item now: those it records whose
    path the run's own `exemptable` list covers. That list was fixed from the
    repository's spec when the run was created, so a spec changed later, or
    a record written by hand for another path, lets nothing more through."""
    from . import review  # here, not at the top: review.py reads this module

    allowed = policy(run)["exemptable"]
    return {path: record for path, record in exemptions(item).items() if review.matching([path], allowed)}


def exempt_path(run_dir, item_id: str, path: str, reason: str, protected, withdraw: bool = False, granted_by: dict | None = None, tree_kind=None) -> dict:
    """Record that one item may change one path its repository's spec
    protects, or withdraw that. The decision is the manager's, said with its
    reason; this keeps the record, and `hl-merge` reads it, both when a build
    is checked and at the merge itself. Before it, the only way to let one
    item change a protected file was to take the file out of `protected` for
    every item of the program (an item of a help-desk hardening program).

    Only a named path is recorded: one file, as `git diff` names it, that a
    pattern of the repository's `protected` covers and that the run's own
    `exemptable` list (fixed from the spec when the run was created) allows
    to be exempted at all. A run created without one takes no exemption,
    which is the kit before this. A pattern, a path nothing protects, one
    outside `exemptable`, and (where `tree_kind` can tell, from the base
    branch) a directory are refused, so an exemption never stands for more
    than it says, and a typo cannot pass for a granted path while the real
    one stays refused. `granted_by` is who recorded it: the manager holding
    the program's claim, as `hl-items exempt` finds it. Every grant and
    withdrawal stays on the item's history."""
    from . import review  # here, not at the top: review.py reads this module

    if not (reason or "").strip():
        raise Refusal("--reason is required: say why this item, and no other, may change this path")
    item = load_item(run_dir, item_id)
    if item["status"] in TERMINAL:
        raise Refusal(f"{item_id} is {item['status']}; nothing is exempted for it any more")
    path = (path or "").strip()
    parts = path.split("/")
    if not path or path.startswith("/") or path.endswith("/") or any(part in ("", ".", "..") for part in parts):
        raise Refusal(f"--path takes one file's path in the repository, as `git diff --name-only` prints it; got {path!r}")
    if PATTERN_CHARS & set(path):
        raise Refusal(f"--path {path!r} is a pattern: an exemption names one file, so record each file the item must change")
    current = exemptions(item)
    history = item.setdefault("protected_exemption_history", [])
    who = dict(granted_by or {})
    if withdraw:
        if path not in current:
            raise Refusal(f"{item_id} has no exemption for {path!r} (it has: {', '.join(sorted(current)) or 'none'})")
        del current[path]
        history.append({"path": path, "action": "withdrawn", "reason": reason.strip(), "at": utc_now(), "by": who})
    else:
        run = load_run(run_dir)
        if not review.matching([path], protected):
            raise Refusal(f"{path!r} is not a protected path of {run['repo']} (its spec protects: {', '.join(protected or []) or 'nothing'}): there is nothing to exempt")
        allowed = policy(run)["exemptable"]
        if not review.matching([path], allowed):
            raise Refusal(
                f"this run takes no exemption for {path!r}: when it was created, its repository's spec named "
                f"{', '.join(f'`{a}`' for a in allowed) if allowed else 'no path'} under `exemptable`, and a run keeps the policy it was created with. "
                "What may be exempted is the operator's to say, in the spec, for runs created after it is staged"
            )
        if tree_kind is not None and tree_kind(path) == "tree":
            raise Refusal(f"{path!r} is a directory on the base branch: an exemption names one file, so record each file the item must change")
        current[path] = {"reason": reason.strip(), "at": utc_now(), "granted_by": who}
        history.append({"path": path, "action": "granted", "reason": reason.strip(), "at": current[path]["at"], "by": who})
    item["protected_exemptions"] = current
    save_item(run_dir, item)
    return item


def mark_external(run_dir, item_id: str, note: str) -> dict:
    if not note:
        raise Refusal("--note is required: say where you saw it merged")
    run = load_run(run_dir)
    if item_id in run["order"]:
        raise Refusal(f"{item_id} is an item of this run; it becomes merged by being merged")
    if item_id not in run["externals_merged"]:
        run["externals_merged"].append(item_id)
        run["events"].append({"id": item_id, "status": "external-merged", "reason_class": "", "at": utc_now(), "note": note})
        write_json(Path(run_dir) / "run.json", run)
    return run


def drop_item_check(run_dir, name: str, note: str) -> dict:
    """Stop holding a run's items to one of the checks its repository named
    for an item. For a check that pull requests into the train never start:
    a run keeps the policy it began with, so correcting the spec does not
    free the items already waiting. The batch still runs every required
    check before anything reaches the base."""
    if not (note or "").strip():
        raise Refusal("--note is required: say why this check is no longer asked of an item")
    run = load_run(run_dir)
    if not on_train(run):
        raise Refusal("this run merges into the base: its items are held to the repository's required checks, and none of those is dropped here")
    asked = policy(run)["train"].get("item_checks") or []
    if name not in asked:
        raise Refusal(f"{name!r} is not one of the checks this run asks of an item ({', '.join(asked) or 'it asks none'})")
    run["policy"]["train"]["item_checks"] = [c for c in asked if c != name]
    run["events"].append({"id": "-", "status": "item-check-dropped", "reason_class": "", "at": utc_now(), "note": f"{name}: {note.strip()}"})
    write_json(Path(run_dir) / "run.json", run)
    return run


def clear_brake(run_dir, note: str) -> dict:
    if not note:
        raise Refusal("--note is required: say what the cause was and what fixed it")
    run = load_run(run_dir)
    run["brake_cleared_at"] = utc_now()
    run["events"].append({"id": "-", "status": "brake-cleared", "reason_class": "", "at": run["brake_cleared_at"], "note": note})
    write_json(Path(run_dir) / "run.json", run)
    return run


# ---------------------------------------------------------------- contract


def _install_contract(run_dir, item: dict, text: str, record: dict) -> dict:
    try:
        criteria = contractfmt.parse_criteria(text)
    except contractfmt.ContractError as exc:
        raise Refusal(f"{item['id']}: contract does not parse: {exc}") from exc
    d = item_dir(run_dir, item["id"])
    path = d / "contract.md"
    history = (item.get("contract") or {}).get("history", [])
    if path.exists():
        kept = d / f"contract.v{len(history) + 1}.md"
        shutil.copyfile(path, kept)
        previous = {k: v for k, v in item["contract"].items() if k != "history"}
        history = history + [{**previous, "kept_as": kept.name}]
    path.write_text(text)
    item["contract"] = {
        **record,
        "sha256": sha256_text(text),
        "criteria": len(criteria),
        "installed_at": utc_now(),
        "history": history,
    }
    save_item(run_dir, item)
    return item


def contract_from_accept(run_dir, item_id: str) -> dict:
    item = load_item(run_dir, item_id)
    if item.get("contract"):
        raise Refusal(f"{item_id} already has a contract ({item['contract']['source']})")
    parsed = {it.id: it for it in backlog.parse_backlog((item_dir(run_dir, item_id) / "item.md").read_text())}
    it = parsed[item_id]
    text = contractfmt.render_from_accept(item_id, it.title, it.accept)
    return _install_contract(run_dir, item, text, {"source": "accept"})


def contract_restore(run_dir, item_id: str) -> dict:
    """Write contract.md again as it was installed, from what it was
    installed from: the item's Accept list, an agreed draft, or an amended
    contract. Nothing is installed: the text has to be the one whose digest
    the item holds, or the file it came from was written too."""
    item = load_item(run_dir, item_id)
    record = item.get("contract")
    if not record:
        raise Refusal(f"{item_id} has no contract installed")
    d = item_dir(run_dir, item_id)
    if record["source"] == "accept":
        it = {i.id: i for i in backlog.parse_backlog((d / "item.md").read_text())}[item_id]
        text = contractfmt.render_from_accept(item_id, it.title, it.accept)
    else:
        source = d / record["file"]
        text = source.read_text() if source.exists() else None
        if text is not None and record["source"] == "amended":
            text = contractfmt.extract_amended(text)
    if text is None or sha256_text(text) != record["sha256"]:
        raise Refusal(f"what the contract of {item_id} was installed from ({record['source']}) no longer gives the text that was installed. Leave the item as it is and tell the operator")
    (d / "contract.md").write_text(text)
    return item


def contract_from_draft(run_dir, item_id: str, n: int) -> dict:
    item = load_item(run_dir, item_id)
    d = item_dir(run_dir, item_id)
    review, draft = d / f"agree-{n}.md", d / f"contract-draft-{n}.md"
    if not draft.exists():
        raise Refusal(f"{draft.name} does not exist")
    if not review.exists() or contractfmt.final_word(review.read_text()) != "AGREED":
        raise Refusal(f"{review.name} does not end with AGREED; a draft is installed only when agreed")
    return _install_contract(run_dir, item, draft.read_text(), {"source": "draft", "n": n, "file": draft.name})


def contract_from_amended(run_dir, item_id: str, filename: str) -> dict:
    item = load_item(run_dir, item_id)
    attempts = load_attempts(run_dir, item_id)
    name = Path(filename).name
    phase, _, rest = name.partition("-")
    number = rest.removesuffix(".md")
    if phase not in ("agree", "correction") or not number.isdigit():
        raise Refusal("an amended contract comes from agree-<n>.md or correction-<n>.md in the item directory")
    n = int(number)
    entries = [e for e in attempts["log"] if e["phase"] == phase and e["n"] == n and not e.get("voided")]
    if not entries:
        raise Refusal(f"{item_id} has no {phase} attempt {n}; start the phase before installing its result")
    source = item_dir(run_dir, item_id) / name
    if not source.exists():
        raise Refusal(f"{name} does not exist")
    text = source.read_text()
    expected = "REVISE" if phase == "agree" else "CORRECTED"
    if contractfmt.final_word(text) != expected:
        raise Refusal(f"{name} does not end with {expected}")
    amended = contractfmt.extract_amended(text)
    if amended is None:
        raise Refusal(f"{name} has no '## Amended contract' section")
    record = {"source": "amended", "file": name, "session": entries[-1]["session"]}
    return _install_contract(run_dir, item, amended, record)


# ---------------------------------------------------------------- views


def timeline(run_dir, item_id: str) -> list[dict]:
    d = item_dir(run_dir, item_id)
    rows = []
    for e in load_attempts(run_dir, item_id)["log"]:
        done = d / f"{e['phase']}-{e['n']}.done"
        row = {k: e[k] for k in ("phase", "n", "build", "at") if k in e}
        row["engine"] = e.get("engine")
        row["voided"] = bool(e.get("voided"))
        # A voided attempt's files were set aside; a done file under its number belongs to the attempt started after it.
        if not row["voided"] and done.exists():
            row["done_at"] = datetime.fromtimestamp(done.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(row)
    return rows


def list_items(run_dir) -> list[dict]:
    run = load_run(run_dir)
    out = []
    for i in run["order"]:
        it = load_item(run_dir, i)
        out.append({"id": i, "status": it["status"], "depends_on": it["depends_on"], "note": it["note"]})
    return out


# ---------------------------------------------------------------- cli


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-items", description=__doc__.split("\n\n")[0])
    p.add_argument("--run-dir", required=True, type=absolute)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the run from one or more ITEMS files")
    s.add_argument("--run-id", required=True)
    s.add_argument("--repo", required=True, help="owner/name of the target repository")
    s.add_argument("--file", action="append", required=True)
    s.add_argument("--work", help="what this run completes, in a few words; it names the run's herdr workspace")
    s.add_argument("--root", type=absolute, help="the program's loop root: the run takes its policy (premise, bounds) from its program.json")
    s.add_argument("--stream", help="the stream this run belongs to, as program.json names it")
    s.add_argument("--land", choices=LANDINGS, default="squash", help="how this run's pull requests are merged; `merge` only for an item whose work is itself a merge")
    s.add_argument("--repair", action="store_true", help="this run repairs a train (a red batch, or a base that no longer merges into it): its merges are not held to the train's limit")

    sub.add_parser("next", help="the item to work now, or why there is none")
    sub.add_parser("list", help="every item with its status")

    s = sub.add_parser("show", help="one item: record, attempts, timeline")
    s.add_argument("--id", required=True)

    s = sub.add_parser("start", help="count a phase against its bound and log it")
    s.add_argument("--id", required=True)
    s.add_argument("--phase", required=True, choices=list(COUNTED))
    s.add_argument("--session")
    s.add_argument("--drafter-session")
    s.add_argument("--engine", choices=["claude", "cursor"])
    s.add_argument("--head-sha")
    s.add_argument("--effort")
    s.add_argument("--why", help="for a build in a routed run: why this builder and effort, in a line")

    s = sub.add_parser("route", help="record how an item is routed; refused below the item's floor")
    s.add_argument("--id", required=True)
    s.add_argument("--premise", required=True, choices=list(ROUTE_PREMISES), help="builder: it plans for itself and may dispute; independent: a premise check runs first")
    s.add_argument("--look", required=True, choices=["yes", "no"], help="whether the item changes something a person looks at")
    s.add_argument("--reason", required=True)

    s = sub.add_parser(
        "void",
        help="give back an attempt so that it can be started again: one that produced no result, or a review that passed over a checkout "
        "that changed under it (the run's `give_back` bound: once for an item unless its program says otherwise)",
    )
    s.add_argument("--id", required=True)
    s.add_argument("--phase", required=True, choices=[*COUNTED, DRAFT])
    s.add_argument("--n", type=int, help="the attempt's number; without it, the latest of the phase")
    s.add_argument("--note", required=True)

    s = sub.add_parser("set", help="change an item's status, or record fields")
    s.add_argument("--id", required=True)
    s.add_argument("--status", choices=["merged", "failed", "parked", "skipped", "pending"])
    s.add_argument("--note")
    s.add_argument("--reopen", action="store_true", help="with --status pending: take up a failed, parked or skipped item again; one that had started is in progress where it stopped")
    s.add_argument("--superseded-by", metavar="ID", help="with --status skipped on a failed item: the repair item that carried its work, which must have shipped (merged, promoted on a train, Verify passed)")
    s.add_argument("--superseded-run", type=absolute, metavar="RUN_DIR", help="the run directory of --superseded-by, when it is another run's item")
    s.add_argument("--field", action="append", default=[])

    s = sub.add_parser(
        "verify-line",
        help="load an item's corrected Verify line from its backlog file; refused once its Verify has passed, while one runs, "
        "and where the file differs from the run anywhere else",
    )
    s.add_argument("--id", required=True)
    s.add_argument("--note", required=True, help="what was wrong with the line")

    s = sub.add_parser("contract", help="install the item's contract from one of its three sources")
    s.add_argument("--id", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--from-accept", action="store_true")
    g.add_argument("--from-draft", type=int, metavar="N")
    g.add_argument("--from-amended", metavar="FILE")
    g.add_argument("--restore", action="store_true", help="write contract.md again as it was installed, where something else has written the file")

    s = sub.add_parser("exempt", help="let this one item change one path its repository's spec protects, with your reason; or --withdraw that")
    s.add_argument("--id", required=True)
    s.add_argument("--path", required=True, help="one file's path in the repository, as `git diff --name-only` prints it; never a pattern")
    s.add_argument("--reason", required=True, help="why this item, and no other, changes it")
    s.add_argument("--withdraw", action="store_true", help="take an exemption this item has back")
    s.add_argument("--root", type=absolute, help="the program's loop root, whose program.json says what is protected (default: the one the run directory lies in)")

    s = sub.add_parser("fixes-outside", help="record that this item fixes forward the failed deploy of an outside pull request, with your reason; or --withdraw that")
    s.add_argument("--id", required=True)
    s.add_argument("--pr", required=True, type=int, help="the outside pull request whose deploy check is recorded failed in this run's repository")
    s.add_argument("--reason", required=True, help="what this item fixes in that deploy")
    s.add_argument("--withdraw", action="store_true")
    s.add_argument("--root", type=absolute, help="the program's loop root (default: the one the run directory lies in)")

    s = sub.add_parser("external", help="record that a dependency outside this run is merged")
    s.add_argument("--id", required=True)
    s.add_argument("--note", required=True)

    s = sub.add_parser("clear-brake", help="resume after three failures of one kind")
    s.add_argument("--note", required=True)

    s = sub.add_parser("drop-item-check", help="on a train: stop holding this run's items to a named check that their pull requests never start")
    s.add_argument("--name", required=True)
    s.add_argument("--note", required=True)
    return p


def _exempt_from_cli(args, procs=None, parent: int | None = None) -> dict:
    """`hl-items exempt`: only the manager that holds the program's claim
    records or withdraws an exemption, typed in its own shell, and the
    record says which manager that was. The command must have been started
    straight from a shell that the claim's holder started: a worker runs
    under a `claude` of its own, and a line of a worker's that one of the
    kit's scripts runs for the manager (a build's Ready command, run by
    `hl-flow review` under the manager's `claude`) has that script between
    it and the shell. Nearest-`claude` was not enough: a Ready line found
    the manager there, and its record named the manager."""
    from . import claim, flow, program  # here, not at the top: nothing else in this module needs them

    root = Path(args.root) if args.root else Path(args.run_dir).parent.parent
    if not (root / "program.json").exists():
        raise Refusal(f"no program.json at {root}: pass --root, the program's loop root, so the repository's protected paths can be read")
    procs = procs or claim.Procs()
    holder = claim.read_claim(root)
    why = claim.started_by_holders_shell(procs, holder, parent)
    if why:
        raise Refusal(f"an exemption is recorded only by the manager that holds the program, typed in its own shell: {why}")
    cfg = program.load(root)
    run = load_run(args.run_dir)
    protected = program.repo_facts(cfg, run["repo"]).get("protected") or []

    def tree_kind(path: str) -> str | None:
        # What the base branch holds at the path, as git names it, where the stream's clone can say; None where it cannot.
        import shutil
        import subprocess

        try:
            clone = flow.clone_of(cfg, run)
        except Refusal:
            return None
        git = shutil.which("git")
        if not git:
            return None
        ref = f"origin/{base_of(run, cfg['base'])}:{path}"
        said = subprocess.run([git, "-C", str(clone), "cat-file", "-t", ref], capture_output=True, text=True)
        return said.stdout.strip() if said.returncode == 0 else None

    granted_by = {key: holder.get(key) for key in ("pid", "session", "pane", "claimed_at")}
    return exempt_path(args.run_dir, args.id, args.path, args.reason, protected, withdraw=args.withdraw, granted_by=granted_by, tree_kind=tree_kind)


def _fixes_outside_from_cli(args, procs=None, parent: int | None = None) -> dict:
    """`hl-items fixes-outside`: the one record that lets an item merge past
    the hold a failed outside deploy puts on its repository, so the loop can
    ship the fix. Guarded as `exempt` is, and for the same reason: only the
    manager that holds the program records it, typed in its own shell, and
    the record says which manager that was. Refused unless that pull
    request's deploy check is recorded failed in the item's repository."""
    from . import claim, outside  # here, not at the top: nothing else in this module needs them

    root = Path(args.root) if args.root else Path(args.run_dir).parent.parent
    if not (root / "program.json").exists():
        raise Refusal(f"no program.json at {root}: pass --root, the program's loop root")
    procs = procs or claim.Procs()
    holder = claim.read_claim(root)
    why = claim.started_by_holders_shell(procs, holder, parent)
    if why:
        raise Refusal(f"a fix-forward of an outside deploy is recorded only by the manager that holds the program, typed in its own shell: {why}")
    run = load_run(args.run_dir)
    item = load_item(args.run_dir, args.id)
    if args.withdraw:
        item.pop("fixes_outside", None)
    else:
        failed = [r for r in outside.records(root, run["repo"]) if r.get("state") == "MERGED" and r["pr"] == int(args.pr) and r.get("verify") == "failed"]
        if not failed:
            raise Refusal(f"no outside pull request #{args.pr} with a failed deploy check is recorded in {run['repo']}: there is nothing for {args.id} to fix forward")
        reason = " ".join((args.reason or "").split())
        if not reason:
            raise Refusal("--reason is required")
        granted_by = {key: (holder or {}).get(key) for key in ("pid", "session", "pane", "claimed_at")}
        item["fixes_outside"] = {"pr": int(args.pr), "reason": reason, "granted_by": granted_by, "at": utc_now()}
    save_item(args.run_dir, item)
    return item


def main(argv: list[str] | None = None, procs=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "init":
            chosen = None
            if args.root:
                from . import program  # here, not at the top: program.py is needed for nothing else in this module

                cfg = program.load(args.root)
                chosen = program.run_policy(cfg, args.repo)
                if args.stream and args.stream not in (cfg.get("streams") or {args.stream: None}):
                    raise Refusal(f"program.json names no stream {args.stream!r}")
            run = init_run(args.run_dir, args.run_id, args.repo, args.file, args.work, policy=chosen, stream=args.stream, repair=args.repair, land=args.land)
            emit(
                {
                    "run_id": run["run_id"],
                    "work": run.get("work"),
                    "stream": run.get("stream"),
                    "policy": policy(run),
                    "items": len(run["order"]),
                    "order": run["order"],
                }
            )
        elif args.cmd == "next":
            emit(next_item(args.run_dir))
        elif args.cmd == "list":
            emit(list_items(args.run_dir))
        elif args.cmd == "show":
            emit(
                {
                    "item": load_item(args.run_dir, args.id),
                    "attempts": load_attempts(args.run_dir, args.id),
                    "timeline": timeline(args.run_dir, args.id),
                }
            )
        elif args.cmd == "start":
            emit(
                start_phase(
                    args.run_dir,
                    args.id,
                    args.phase,
                    session=args.session,
                    engine=args.engine,
                    head_sha=args.head_sha,
                    drafter_session=args.drafter_session,
                    effort=args.effort,
                    why=args.why,
                )
            )
        elif args.cmd == "route":
            item = route_item(args.run_dir, args.id, args.premise, args.look == "yes", args.reason)
            emit({"id": args.id, "route": item["route"], "floor": item.get("floor") or []})
        elif args.cmd == "void":
            emit(void_attempt(args.run_dir, args.id, args.phase, args.n, args.note))
        elif args.cmd == "set":
            if not args.status and not args.field:
                raise Refusal("set needs --status or --field")
            item = set_fields(args.run_dir, args.id, args.field) if args.field else None
            if args.status:
                item = set_status(args.run_dir, args.id, args.status, args.note, reopen=args.reopen, superseded_by=args.superseded_by, superseded_run=args.superseded_run)
            emit(item)
        elif args.cmd == "verify-line":
            emit(reload_verify(args.run_dir, args.id, args.note))
        elif args.cmd == "contract":
            if args.restore:
                item = contract_restore(args.run_dir, args.id)
            elif args.from_accept:
                item = contract_from_accept(args.run_dir, args.id)
            elif args.from_draft is not None:
                item = contract_from_draft(args.run_dir, args.id, args.from_draft)
            else:
                item = contract_from_amended(args.run_dir, args.id, args.from_amended)
            emit(item["contract"])
        elif args.cmd == "fixes-outside":
            item = _fixes_outside_from_cli(args, procs)
            emit({"id": args.id, "fixes_outside": item.get("fixes_outside")})
            return 0
        elif args.cmd == "exempt":
            item = _exempt_from_cli(args, procs)
            emit({"id": args.id, "protected_exemptions": exemptions(item)})
        elif args.cmd == "external":
            emit({"externals_merged": mark_external(args.run_dir, args.id, args.note)["externals_merged"]})
        elif args.cmd == "clear-brake":
            emit({"brake_cleared_at": clear_brake(args.run_dir, args.note)["brake_cleared_at"]})
        elif args.cmd == "drop-item-check":
            emit({"item_checks": policy(drop_item_check(args.run_dir, args.name, args.note))["train"]["item_checks"]})
    except Refusal as refusal:
        emit({"refused": refusal.reasons})
        print(f"hl-items: refused: {refusal}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
