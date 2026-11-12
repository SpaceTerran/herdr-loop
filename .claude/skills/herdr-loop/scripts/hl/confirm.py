"""A delta confirm: a rework checked against the fix list it answers.

A run whose policy says `confirm: full` gives every rework a full fresh
review, as the kit always did. With `confirm: delta`, a rework that follows
a failed review can be checked by a fresh reviewer whose brief is the fix:
the failed review's fix list, entry by entry, and the change between the
commit that review was of and the new head. That review was given the
whole change, and its transcript shows it exercised it; the confirm is
given what changed since.

Nothing here judges the fix. This module says when a delta confirm may
stand in for a full review, from git and the run's own records, and it is
asked twice: when a review is about to start (`decide`), and again by the
merge gate before anything merges on a confirm's pass (`chain_problems`).
Both ask the same questions (`chain`):

    the review before it   a full review of this pull request that failed
                           with a fix list, that exercised the change and
                           looked where the item asks for a look, that left
                           no criterion unchecked, and whose every failed
                           criterion an entry of its fix list names
    as it was read         its verdict file is the one `hl-flow reviewed`
                           read, and what that read made of its transcript
                           stands beside what the transcript shows now
    the same contract      the criteria are the ones that review was given
    only the fix           the commit that review was of is an ancestor of the
                           new head, nothing between them is a merge, the
                           pull request's merge base is where it was, and
                           the build is served and found ready by the same
                           two lines of its report
    a small fix            within the run's `confirm_bound`, by git's own
                           count of lines; a change that count does not
                           show (a binary file, a submodule's pointer, a
                           file's mode, a file added or deleted with no line
                           in it), anywhere in the fix, means a full review,
                           and so does a rename or a copy as git finds one
    nothing it cannot see  the fix touches no path no item may change, and
                           no path a person sees unless the failed review
                           looked and that path was in the diff it was given

One confirm for an item: when it fails, the next rework gets a full review,
and so does the head of a confirm that was given back after it passed over
a changed checkout.
Whoever starts a review may always ask for a full one; nothing asks for a
delta where these answer no.

What this cannot hold: that the fix broke nothing the failed review had
passed. The confirm's reviewer is asked, and its transcript must show it
was shown every line of the fix, but no script can see what it made of
them. The bound keeps the fix small, and on a train the full checks still
run on the batch.
"""

from __future__ import annotations

from pathlib import Path

from . import items, review, verdict
from .common import read_json, sha256_text


def plan_path(item_dir, n: int) -> Path:
    """Where the plan of a delta confirm is kept, beside its brief. A full review's brief has none beside it; a confirm's brief without its plan is refused."""
    return Path(item_dir) / f"grade-{n}.confirm.json"


def reviews(run_dir, item_id: str) -> list[dict]:
    return [e for e in items.load_attempts(run_dir, item_id)["log"] if e["phase"] == "grade" and not e.get("voided")]


def confirms_had(run_dir, item_id: str) -> list[dict]:
    """The delta confirms an item has had: those that stand, and one that
    passed over a changed checkout and was given back (`items.void_attempt`).
    Giving a confirm back gives the build its review again, not the item
    its one confirm: the next review is a full one."""
    attempts = items.load_attempts(run_dir, item_id)
    return [e for e in reviews(run_dir, item_id) + items.given_back(attempts) if e.get("kind") == items.CONFIRM]


def _failed_review(run_dir, item_id: str, first: dict) -> tuple[list[str], dict]:
    """Why review `first` cannot be what a delta confirm builds on, and what a confirm takes from it."""
    d = items.item_dir(run_dir, item_id)
    n = first["n"]
    said = f"review {n}"
    read = first.get("read")
    if read is not None and read.get("verdict_sha") != verdict.file_sha(d, n):
        # The fix list a confirm answers is this file's, and the rework's builder is pointed at it with its permission checks off.
        return [f"verdict-{n}.json is not the verdict `hl-flow reviewed` read of {said}: it was changed afterwards, or is gone"], {}
    checked = verdict.check(d, n, style="code")
    if checked["problems"]:
        return [f"{said} left no usable verdict ({checked['problems'][0]})"], {}
    if checked["effective"] != "FAIL":
        return [f"{said} is {checked['effective']}, not a fail with a fix list"], {}
    data = read_json(d / f"verdict-{n}.json")
    fixes = data["fix_list"]
    problems = []
    if not fixes:
        problems.append(f"{said} failed without a fix list")
    unchecked = sorted(c["n"] for c in data["criteria"] if c["result"] == "not_checked")
    if unchecked:
        problems.append(f"{said} left criteria {unchecked} unchecked, and a confirm does not go through the criteria")
    named = {f.get("criterion") for f in fixes}
    unnamed = sorted(c["n"] for c in data["criteria"] if c["result"] == "fail" and c["n"] not in named)
    if unnamed:
        problems.append(f"criteria {unnamed} failed in {said} and no entry of its fix list names them")
    # What the review did to its checkout was read once, when it was over, and is on its attempt: nothing can read it again later.
    noted = ["it was never read (`hl-flow reviewed` reads a review, and notes on its attempt what it did to its checkout)"] if read is None else read["checkout"]
    seen = review.check(run_dir, item_id, n)
    # What its transcript showed is read again here, and what the first read made of it stands beside that: the
    # record of what was served and the pictures of its looks lie in the run directory, where a worker can write.
    # Whether it looked is what the first read found, for the same reason.
    then = (read or {}).get("exercise") or []
    for problem in noted[:1] + (then[:1] or seen["problems"][:1]):
        problems.append(f"{said} does not count: {problem}")
    contract = d / "contract.md"
    if not contract.exists() or sha256_text(contract.read_text()) != first.get("contract_sha"):
        problems.append(f"the contract changed after {said}")
    return problems, {"verdict": data, "fixes": fixes, "looks": review.recorded_looks(d, n), "looked": bool((read or {}).get("looked"))}


def chain(run_dir, item_id: str, first: dict, head: str, tools, checkout, base_ref: str | None, repo_facts: dict, bound: dict, how_run: str | None) -> tuple[list[str], dict]:
    """Why a confirm of the change from the commit review `first` was of to
    `head` may not stand in for a full review of `head`; nothing when it
    may. `base_ref` is the branch the pull request goes into, as the clone
    has it (its repository's train, for a run on one), or None for a pull
    request that has already merged, whose base has taken it in."""
    d = items.item_dir(run_dir, item_id)
    n = first["n"]
    said = f"review {n}"
    start = first.get("head_sha") or ""
    problems, found = _failed_review(run_dir, item_id, first)
    if problems:
        return problems, found
    if head == start:
        return [f"the head has not moved since {said}: it is still {start[:12]}"], found
    if tools.git(["merge-base", "--is-ancestor", start, head], checkout, check=False).returncode != 0:
        return [f"{start[:12]}, the commit {said} was of, is not an ancestor of {head[:12]}: the branch was rewritten"], found
    merges = tools.git(["rev-list", "--merges", f"{start}..{head}"], checkout, check=False).stdout.split()
    if merges:
        problems.append(f"the fix holds a merge ({merges[0][:12]}): it carries work that is not the fix")
    if not how_run or first.get("how_run") != how_run:
        # Neither line is in git, so neither is in the fix a confirm is given.
        problems.append(f"the rework was not served and found ready the way {said} was (the Serve and Ready lines of its report): how a build is served is outside the fix a confirm is given")
    facts = d / f"grade-{n}.facts.json"
    recorded = read_json(facts) if facts.exists() else {}
    base = recorded.get("base") if recorded.get("head") == start else None
    if not base:
        return problems + [f"{said} left no record of the diff it was given ({facts.name})"], found
    if base_ref:
        now = tools.git(["merge-base", base_ref, head], checkout, check=False).stdout.strip()
        if now != base:
            problems.append(f"the pull request's merge base moved from {base[:12]} to {now[:12] or 'none'} since {said}")
    try:
        before = review.diff_facts(tools, checkout, base, start)
        fix = review.diff_facts(tools, checkout, start, head, repo_facts)
        moved = review.moved_files(tools, checkout, start, head)
    except RuntimeError as failure:
        return problems + [f"the fix could not be read from git: {failure}"], found
    read_before = {f["path"] for f in before["files"]}
    lines = fix["insertions"] + fix["deletions"]
    allowed = max(bound["lines"], int(bound["share"] * (before["insertions"] + before["deletions"])))
    outside = [f["path"] for f in fix["files"] if f["path"] not in read_before]
    if not fix["files"]:
        problems.append(f"nothing differs between {start[:12]} and {head[:12]}")
    if fix["unmeasured"]:
        # Anywhere in the fix, beside whatever else it changes: a confirm is held to the lines it was shown, and these are not lines.
        named = "; ".join(f"`{path}`, {why}" for path, why in list(fix["unmeasured"].items())[:5])
        problems.append(f"the fix holds a change no count of lines shows ({named})")
    if moved:
        # Decided here, before a confirm is spent: its reviewer's diff may follow the rename, and would then not print the lines it is held to.
        problems.append(f"the fix renames or copies a file as git reads it ({', '.join(moved[:5])}), and a confirm is held to the lines of a diff that follows neither")
    if lines > allowed:
        problems.append(
            f"the fix changes {lines} lines, more than the {allowed} a confirm may cover "
            f"({bound['share']} of the {before['insertions'] + before['deletions']} in the diff {said} was given, or {bound['lines']})"
        )
    if len(outside) > bound["files"]:
        problems.append(f"the fix touches {len(outside)} files that were not in the diff {said} was given ({', '.join(outside[:5])}), more than the {bound['files']} a confirm may cover")
    if fix["protected"]:
        problems.append(f"the fix touches a path no item may change ({', '.join(fix['protected'][:5])})")
    unseen = [path for path in fix["seen"] if path not in read_before]
    if unseen:
        problems.append(f"the fix touches a path a person sees that was not in the diff {said} was given ({', '.join(unseen[:5])})")
    if fix["seen"] and not found["looked"]:
        problems.append(f"the fix touches what a person sees ({', '.join(fix['seen'][:5])}) and {said} did not look")
    return problems, {**found, "fix": fix, "lines": lines, "allowed": allowed}


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def decide(run_dir, item_id: str, head: str, tools, checkout, base_ref: str, repo_facts: dict | None = None, full: bool = False, how_run: str | None = None) -> dict:
    """Which review the next one of an item is, in a run whose policy is
    `confirm: delta`, and why, in a line: `full`, or `delta` with what its
    brief and its record need."""
    run = items.load_run(run_dir)
    done = reviews(run_dir, item_id)
    spent = confirms_had(run_dir, item_id)
    if not done:
        why = "no review of this item has run yet"
    elif full:
        why = "a full review was asked for"
    elif spent:
        back = ", given back after it passed over a changed checkout," if spent[0].get("voided") else ""
        why = f"review {spent[0]['n']}{back} was this item's one delta confirm"
    else:
        first = done[-1]
        problems, found = chain(run_dir, item_id, first, head, tools, checkout, base_ref, repo_facts or {}, items.policy(run)["confirm_bound"], how_run)
        if not problems:
            return {
                "kind": "delta",
                "why": (
                    f"review {first['n']} failed at {first['head_sha'][:12]} with {_count(len(found['fixes']), 'fix-list entry', 'fix-list entries')}, and the fix "
                    f"since is {_count(found['lines'], 'changed line', 'changed lines')} in {_count(len(found['fix']['files']), 'file', 'files')} "
                    f"(a confirm may cover {found['allowed']})"
                ),
                "confirms": first["n"],
                "from_sha": first["head_sha"],
                "head_sha": head,
                "verdict_sha": verdict.digest(found["verdict"]),
                "fix_list": found["fixes"],
                "looks": found["looks"],
                "facts": found["fix"],
            }
        why = "; ".join(problems)
    return {"kind": "full", "why": why}


def chain_problems(run_dir, item_id: str, entry: dict, tools, checkout, base_ref: str | None, repo_facts: dict | None) -> list[str]:
    """What the merge gate asks of a review that is a delta confirm, beyond
    its own pass: everything `decide` asked when the confirm started, read
    again, and that the record itself is one `decide` could have made."""
    run = items.load_run(run_dir)
    n = entry["n"]
    # What follows a confirm that cannot stand: a full review of the same head. Every refusal says so.
    in_full = f"; review the head in full (`hl-flow review --id {item_id} --full`)"
    if items.policy(run)["confirm"] != "delta" or items.policy(run)["review"] != "code":
        return [f"review {n} is recorded as a delta confirm, and this run confirms a rework with a full review{in_full}"]
    done = reviews(run_dir, item_id)
    earlier = [e for e in done if e["n"] < n]
    first = earlier[-1] if earlier else None
    if first is None or first.get("kind") == items.CONFIRM or first["n"] != entry.get("confirms") or first.get("head_sha") != entry.get("from_sha"):
        return [f"review {n} is a delta confirm, and the review before it is not the failed full review at {str(entry.get('from_sha'))[:12]} that it names{in_full}"]
    problems = []
    if len(confirms_had(run_dir, item_id)) > 1:
        problems.append(f"{item_id} has had more than one delta confirm; an item gets one{in_full}")
    live = [e for e in items.load_attempts(run_dir, item_id)["log"] if not e.get("voided")]
    builders = {e["session"] for e in live if e["phase"] == "build"} | {e["drafter_session"] for e in live if e.get("drafter_session")}
    if not first.get("session") or first["session"] in builders or first["session"] == entry.get("session"):
        problems.append(f"review {first['n']} and its confirm must be two sessions, neither of them a builder's{in_full}")
    if repo_facts is None:
        return problems + [f"the program of {run['run_id']} was not found, so the paths a person sees and those no item may change are unknown: pass --root"]
    found, _ = chain(run_dir, item_id, first, entry["head_sha"], tools, checkout, base_ref, repo_facts, items.policy(run)["confirm_bound"], entry.get("how_run"))
    return problems + [f"review {n} is a delta confirm that cannot stand in for a full review: {problem}{in_full}" for problem in found]
