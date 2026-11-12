"""hl-train: a repository's integration branch, and the batches that leave it.

    hl-train --root <root> --repo <owner/name> status
    hl-train --root <root> --repo <owner/name> test [--note "<why now>"]
    hl-train --root <root> --repo <owner/name> promote
    hl-train --root <root> --repo <owner/name> repair
    hl-train --root <root> --repo <owner/name> abandon --note "<why>"

A repository whose spec gives it a `train` takes its items differently. An
item's pull request goes into the train's branch (`hl-train/<program>`), not
into the base, and `hl-merge` merges it there after its review, without
waiting for the repository's required checks: those run later, on a batch.
So the next item is built on top of the last the moment it has merged, and
the long checks are paid once for several items instead of once for each.

Nothing reaches the base that way but a batch:

    test     pins the train's head as a branch of its own and opens the
             batch's pull request into the base, which starts the required
             checks on exactly that commit. If the base has moved, it is
             merged into the train first, so what is tested holds it.
    promote  when every required check is green on that commit: merges the
             pull request with a merge commit, under the merge lock, and
             records the promotion on every item in it. The base's tree is
             then the tree the checks ran on, and no other. When a check is
             red it says which and changes nothing: a check can fail for a
             reason that is not the batch's (a lost runner, an outage), and
             running it again is cheaper than testing a new batch.
    repair   for a batch whose checks failed in their own right: closes it,
             and writes what failed and a one-item backlog to repair it.
             Items go on merging into the train meanwhile; nothing is
             promoted until a batch is green.

A promotion is refused when the base moved after the test, when a commit on
the train is not one this loop merged, when an earlier promotion's deploy
check has not passed, or when a commit in the batch carries attribution.

When to test is the manager's call: `status` says a test is `due` once
`batch` items have merged since the last promotion, and it may test sooner
or hold. One bound is not its call: `hl-merge` refuses to merge into a
train that is `limit` items ahead of the last promotion.

State is in `<root>/trains/<owner>__<repo>/`: `train.json`, one
`batch-<n>.json` for each batch, `batch-<n>-red.md` and
`repair-<n>-ITEMS.md` for a red one, and `red.md` while the latest tested
batch is red (briefs name what is failing, so nobody chases it).

Exit codes: 0 done; 2 refused; 3 the merge lock is held; 4 the batch's
checks are red (`promote`) or the base conflicts with the train (`test`);
5 the checks are still running; 6 the base moved after the test, and the
batch was closed.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from . import claim, items, merge, program, verify
from .common import Refusal, absolute, emit, read_json, utc_now, write_json

FINISHED_BUCKETS = ("pass", "fail", "skipping", "cancel")
GREEN_BUCKETS = ("pass", "skipping")
PUSH_TRIES = 3
REPAIR_PREFIX = "TRN"
CONFLICT_PREFIX = "TRC"
LOCK_FILE = "promote-lock.json"


# ---------------------------------------------------------------- state


def state_dir(root, repo: str) -> Path:
    return Path(root) / "trains" / repo.lower().replace("/", "__")


def load_state(root, repo: str) -> dict:
    path = state_dir(root, repo) / "train.json"
    blank = {"repo": repo, "batches": 0, "open": None, "red": None, "merge_backs": []}
    return {**blank, **read_json(path)} if path.exists() else blank


def save_state(root, repo: str, state: dict) -> None:
    write_json(state_dir(root, repo) / "train.json", state)


@contextlib.contextmanager
def _locked(path: Path, wait: bool):
    """A lock on a file that the system frees when this process ends, however it ends: nothing is left to go stale."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Refusal("another hl-train command is at work on this repository: run this again when it has finished") from None
        yield
    finally:
        os.close(fd)


def update_state(root, repo: str, change) -> dict:
    """Read, change and write train.json as one step. Another stream's
    `hl-flow prepare` may be writing it in the same moment."""
    with _locked(state_dir(root, repo) / "state.lock", wait=True):
        state = load_state(root, repo)
        change(state)
        save_state(root, repo, state)
        return state


def one_command(root, repo: str):
    """Held by `test`, `promote`, `repair` and `abandon` while they run. Two
    at once on one repository could close a batch under a merge in flight,
    or free a merge lock that is in use."""
    return _locked(state_dir(root, repo) / "command.lock", wait=False)


def batch_path(root, repo: str, n: int) -> Path:
    return state_dir(root, repo) / f"batch-{int(n)}.json"


def red_file(root, repo: str) -> Path:
    """What is failing on the train now; absent while the latest tested batch was green."""
    return state_dir(root, repo) / "red.md"


def clone_of(cfg: dict, repo: str, given=None) -> Path:
    """A clone of the repository to run git in: any of its streams' clones."""
    path = given
    if not path:
        for stream in (cfg.get("streams") or {}).values():
            if str(stream.get("repo", "")).lower() == repo.lower() and stream.get("clone"):
                path = stream["clone"]
                break
    if not path:
        raise Refusal(f"program.json names no stream in {repo}: pass --repo-dir")
    if not (Path(path) / ".git").exists():
        raise Refusal(f"{path} is not a clone of {repo}")
    return Path(path)


def settings(cfg: dict, repo: str) -> dict:
    found = program.train_settings(cfg, repo)
    if found is None:
        raise Refusal(f"{repo} has no train: its entry under `repos` in the program's spec has no `train`")
    return found


# ---------------------------------------------------------------- what has merged


def runs_of(root, repo: str):
    """The runs of this program that merge into the repository's train."""
    for run_file in sorted((Path(root) / "runs").glob("*/run.json")):
        run = read_json(run_file)
        if str(run.get("repo", "")).lower() == repo.lower() and items.on_train(run):
            yield run_file.parent, run


def landed(root, repo: str) -> list[dict]:
    """Every item merged into the train, promoted or not, in the order the runs list them."""
    out = []
    for run_dir, run in runs_of(root, repo):
        for item_id in run["order"]:
            item = items.load_item(run_dir, item_id)
            if item["status"] != "merged":
                continue
            record_file = items.item_dir(run_dir, item_id) / "merge.json"
            record = read_json(record_file) if record_file.exists() else {}
            out.append(
                {
                    "run_dir": str(run_dir),
                    "run": run["run_id"],
                    "id": item_id,
                    "title": item["title"],
                    "pr": record.get("pr"),
                    "merge_sha": record.get("merge_sha"),
                    "promoted": item["fields"].get("promoted"),
                    "has_verify": bool(item.get("verify_command")),
                    "verify": item["fields"].get("verify"),
                }
            )
    return out


def unpromoted(root, repo: str) -> list[dict]:
    return [entry for entry in landed(root, repo) if not entry["promoted"]]


# ---------------------------------------------------------------- git


def _ref(branch: str) -> str:
    return f"refs/remotes/origin/{branch}"


def fetch(tools, clone, *branches: str) -> None:
    """Bring the named branches up to date as remote-tracking refs, whatever the clone was told to follow."""
    specs = [f"+refs/heads/{b}:{_ref(b)}" for b in branches]
    got = tools.git(["fetch", "--quiet", "origin", *specs], clone, check=False)
    if got.returncode != 0:
        raise Refusal(f"could not fetch {', '.join(branches)} in {clone}: {got.stderr.strip()[:200]}")


def _sha(tools, clone, ref: str) -> str:
    return tools.git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], clone, check=False).stdout.strip()


def _is_ancestor(tools, clone, older: str, newer: str) -> bool:
    return tools.git(["merge-base", "--is-ancestor", older, newer], clone, check=False).returncode == 0


def _on_remote(tools, clone, branch: str) -> str | None:
    line = tools.git(["ls-remote", "origin", f"refs/heads/{branch}"], clone).stdout.strip()
    return line.split()[0] if line else None


def ensure(cfg: dict, repo: str, clone, tools) -> str:
    """The train's branch, made from the base the first time it is wanted."""
    base, branch = cfg["base"], program.train_branch(cfg)
    if _on_remote(tools, clone, branch) is None:
        fetch(tools, clone, base)
        made = tools.git(["push", "origin", f"{_ref(base)}:refs/heads/{branch}"], clone, check=False)
        # Another stream of the program may have made it in the same moment; that is as good.
        if made.returncode != 0 and _on_remote(tools, clone, branch) is None:
            raise Refusal(f"could not create {branch} in {repo}: {made.stderr.strip()[:300]}")

        def made_now(state):
            state.setdefault("created_at", utc_now())
            state["branch"], state["base"] = branch, base

        update_state(cfg["root"], repo, made_now)
    fetch(tools, clone, base, branch)
    return branch


def bring_base_in(cfg: dict, repo: str, clone, tools) -> dict:
    """Make the train hold everything the base holds. After a promotion that
    is the promotion's own merge commit, which changes no file; it can also
    be something another hand merged into the base. A merge the two cannot
    make cleanly is left unmade and its files are named: resolving it is an
    item's work, not a script's."""
    base, branch = cfg["base"], program.train_branch(cfg)
    for _ in range(PUSH_TRIES):
        fetch(tools, clone, base, branch)
        base_sha, head = _sha(tools, clone, _ref(base)), _sha(tools, clone, _ref(branch))
        if _is_ancestor(tools, clone, base_sha, head):
            return {"brought_in": None}
        if _is_ancestor(tools, clone, head, base_sha):
            # Nothing has merged into the train since: it simply moves up to the base.
            pushed = tools.git(["push", "origin", f"{base_sha}:refs/heads/{branch}"], clone, check=False)
            if pushed.returncode == 0:
                fetch(tools, clone, branch)
                return {"brought_in": base_sha, "how": "moved up"}
            continue
        work = Path(clone) / ".worktrees" / f"train-{branch.split('/')[-1]}"
        tools.git(["worktree", "remove", "--force", str(work)], clone, check=False)
        tools.git(["worktree", "prune"], clone, check=False)
        made = tools.git(["worktree", "add", "--detach", str(work), head], clone, check=False)
        if made.returncode != 0:
            raise Refusal(f"could not check out {branch} at {work}: {made.stderr.strip()[:200]}")
        try:
            merged = tools.git(["merge", "--no-ff", "--no-edit", "-m", f"{branch}: take in {base} at {base_sha[:12]}", base_sha], work, check=False)
            if merged.returncode != 0:
                files = tools.git(["diff", "--name-only", "--diff-filter=U"], work, check=False).stdout.split()
                tools.git(["merge", "--abort"], work, check=False)
                return {"conflict": files or [merged.stderr.strip()[:200]], "base_sha": base_sha, "head": head}
            new = _sha(tools, work, "HEAD")
            # On record before it is pushed: a commit of this script's that is on the train and not on record would be
            # taken for a stranger's, and nothing could then be tested. One on record that was never pushed harms nothing.
            update_state(cfg["root"], repo, lambda state: state["merge_backs"].append(new))
            pushed = tools.git(["push", "origin", f"{new}:refs/heads/{branch}"], work, check=False)
        finally:
            tools.git(["worktree", "remove", "--force", str(work)], clone, check=False)
            tools.git(["worktree", "prune"], clone, check=False)
        if pushed.returncode == 0:
            fetch(tools, clone, branch)
            return {"brought_in": base_sha, "how": "merged", "commit": new}
        # An item merged into the train in the same moment: read it again and merge on top of that.
    raise Refusal(f"could not push the merge of {base} into {branch} in {PUSH_TRIES} tries: the train kept moving")


def not_ours(tools, clone, base_sha: str, head: str, known: set[str]) -> list[str]:
    """Commits put on the train, between the base and `head`, that this loop
    did not put there. The train is an ordinary branch and GitHub guards
    nothing on it, so the promotion reads its history: each commit along it
    must be an item's merge on record or a merge of the base this script
    made. What such a merge brought in behind it (the base's own commits,
    or the commits of a pull request that was merged whole and not squashed)
    came in through a commit that is answered for."""
    listing = tools.git(["rev-list", "--first-parent", "--format=%H %s", "--no-commit-header", f"{base_sha}..{head}"], clone).stdout
    out = []
    for line in listing.splitlines():
        sha, _, subject = line.partition(" ")
        if sha and sha not in known:
            out.append(f"{sha[:12]} ({subject[:70]})")
    return out


def in_batch(tools, clone, base_sha: str, head: str, entries: list[dict]) -> list[dict]:
    """The unpromoted items whose merge is part of what `head` adds to the base, oldest first."""
    order = tools.git(["rev-list", "--reverse", f"{base_sha}..{head}"], clone).stdout.split()
    place = {sha: i for i, sha in enumerate(order)}
    held = [e for e in entries if e["merge_sha"] in place]
    return sorted(held, key=lambda e: place[e["merge_sha"]])


def _tree(tools, clone, sha: str | None) -> str:
    """A commit's tree, or nothing when the commit cannot be read here."""
    if not sha:
        return ""
    return tools.git(["rev-parse", "--verify", "--quiet", f"{sha}^{{tree}}"], clone, check=False).stdout.strip()


def mend(root, repo: str, clone, tools) -> list[str]:
    """Fill in a merge's commit where its record lacks one: GitHub does not
    always name the commit in the moment it answers that it merged."""
    mended = []
    for entry in unpromoted(root, repo):
        if entry["merge_sha"] or not entry["pr"]:
            continue
        try:
            seen = _gh_json(tools, clone, ["pr", "view", str(entry["pr"]), "--repo", repo, "--json", "state,mergeCommit"]) or {}
        except (RuntimeError, ValueError):
            # Left as it is: the test says which merge has no commit, which is the fact.
            continue
        sha = (seen.get("mergeCommit") or {}).get("oid")
        if seen.get("state") == "MERGED" and sha:
            path = items.item_dir(entry["run_dir"], entry["id"]) / "merge.json"
            write_json(path, {**read_json(path), "merge_sha": sha, "merge_sha_read_at": utc_now()})
            mended.append(entry["id"])
    return mended


def _recorded_promotion(root, repo: str, entry: dict) -> dict | None:
    """The batch on record that took an item to the base, if one did."""
    for path in state_dir(root, repo).glob("batch-*.json"):
        if not path.stem[len("batch-") :].isdigit():
            continue
        record = read_json(path)
        if record.get("promoted_sha") and any(h["id"] == entry["id"] and h["run"] == entry["run"] for h in record.get("items", [])):
            return record
    return None


def not_in_batch(root, repo: str, clone, tools, cfg: dict, head: str, base_sha: str, rest: list[dict]) -> list[str]:
    """Why a merge on record that `head` does not add to the base keeps the
    train from being tested, one reason for each kind. A merge made after
    `head` was read is no reason: it waits for the next batch."""
    base, branch = cfg["base"], program.train_branch(cfg)
    fetch(tools, clone, branch)
    now = _sha(tools, clone, _ref(branch))
    no_commit, on_base, gone = [], [], []
    for entry in rest:
        sha = entry["merge_sha"]
        if not sha:
            no_commit.append(entry["id"])
        elif _is_ancestor(tools, clone, sha, base_sha):
            # A promotion whose records were cut short. Its batch says which; anything else put it there.
            record = _recorded_promotion(root, repo, entry)
            if record:
                items.record_promoted(entry["run_dir"], entry["id"], record["n"], record["promoted_sha"], record.get("promoted_at") or utc_now())
            else:
                on_base.append(entry["id"])
        elif not _is_ancestor(tools, clone, sha, now):
            gone.append(entry["id"])
    reasons = []
    if no_commit:
        reasons.append(f"merged by this loop's records with no commit named, and GitHub names none for their pull requests: {', '.join(no_commit)}")
    if on_base:
        reasons.append(f"on {base} already, and no promotion on record put them there: {', '.join(on_base)}. Something else merged them; tell the operator")
    if gone:
        reasons.append(f"merged by this loop's records and not on {branch}: {', '.join(gone)}. The train was rewritten by something else; nothing is tested until that is understood")
    return reasons


def held_up_by_verify(root, repo: str, batch_items: list[dict]) -> list[str]:
    """Promotions whose deploy check has not passed, which nothing more may
    land on top of. One recorded as failed does not hold up the batch that
    carries its fix-forward: the fix cannot be deployed, and so cannot be
    verified, any other way. A fix-forward is an item of a run made to
    repair the train (`--repair`) whose text names the failed item. An
    ordinary item that names it, as one that depends on it does, is not."""
    texts = []
    for held in batch_items:
        path = items.item_dir(held["run_dir"], held["id"]) / "item.md"
        if items.load_run(held["run_dir"]).get("repair") and path.exists():
            texts.append(path.read_text())
    out = []
    for e in landed(root, repo):
        if not (e["promoted"] and e["has_verify"]) or e["verify"] in items.VERIFY_SETTLED:
            continue
        if e["verify"] == "verify-failed" and any(verify.names(text, e["id"]) for text in texts):
            continue
        how = "its Verify failed, and this batch holds no item that fixes it forward (an item of a `--repair` run whose text names it)" if e["verify"] == "verify-failed" else "its Verify is not recorded as passed"
        out.append(f"{e['id']} (batch {e['promoted']}, verify={e['verify']}): {how}; nothing more is promoted until it is settled (`hl-verify`)")
    return out


# ---------------------------------------------------------------- status


def _open_batch(root, repo: str, state: dict) -> dict | None:
    return read_json(batch_path(root, repo, state["open"])) if state.get("open") else None


def settle(root, repo: str) -> None:
    """Finish what a command that was cut short at its last line left: a
    batch whose own record says it is over, and which the train's state
    still calls open. Run by every command before it reads anything."""
    record = _open_batch(root, repo, load_state(root, repo))
    if not record or record["state"] not in ("promoted", "red", "abandoned"):
        return
    _closed(root, repo, record)


def _closed(root, repo: str, record: dict) -> None:
    """The train's state after a batch ended, written in one step."""
    n, how = record["n"], record["state"]

    def change(state):
        state["open"] = None
        if how == "promoted":
            state.update(red=None, last_promoted=n)
        elif how == "red":
            state["red"] = n

    update_state(root, repo, change)
    if how == "promoted":
        red_file(root, repo).unlink(missing_ok=True)


def verify_pending(root, repo: str) -> list[str]:
    return [f"{e['id']} (batch {e['promoted']}, verify={e['verify']})" for e in landed(root, repo) if e["promoted"] and e["has_verify"] and e["verify"] not in items.VERIFY_SETTLED]


def status(root, repo: str, clone, tools) -> dict:
    cfg = program.load(root)
    limits = settings(cfg, repo)
    state = load_state(root, repo)
    base, branch = cfg["base"], program.train_branch(cfg)
    waiting = unpromoted(root, repo)
    out: dict = {
        "repo": repo,
        "train": branch,
        "base": base,
        "ahead": len(waiting),
        "items": [e["id"] for e in waiting],
        "batch": limits["batch"],
        "limit": limits["limit"],
        "red": state.get("red"),
        "verify_pending": verify_pending(root, repo),
    }
    opened = _open_batch(root, repo, state)
    out["open"] = {k: opened[k] for k in ("n", "pr", "head", "opened_at")} | {"items": [e["id"] for e in opened["items"]]} if opened else None
    if _on_remote(tools, clone, branch) is None:
        out.update(head=None, due=False, full=False, base_moved=False)
        return out
    fetch(tools, clone, base, branch)
    head, base_sha = _sha(tools, clone, _ref(branch)), _sha(tools, clone, _ref(base))
    out["head"] = head
    out["base_moved"] = not _is_ancestor(tools, clone, base_sha, head)
    out["missing"] = [e["id"] for e in waiting if not (e["merge_sha"] and _is_ancestor(tools, clone, e["merge_sha"], head))]
    # In an open batch already, or merged since it was opened.
    untested = [e["id"] for e in waiting if not opened or e["id"] not in {held["id"] for held in opened["items"]}]
    out["untested"] = untested
    out["due"] = not opened and len(waiting) >= limits["batch"]
    out["full"] = len(waiting) >= limits["limit"]
    return out


# ---------------------------------------------------------------- test


def _gh_json(tools, clone, argv: list[str]):
    proc = tools.gh(argv, clone, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(argv[:3])} failed: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout or "null")


def _batch_text(cfg: dict, repo: str, n: int, head: str, held: list[dict]) -> tuple[str, str]:
    ids = ", ".join(e["id"] for e in held)
    title = f"{cfg['program']}: batch {n} ({ids})"
    lines = [
        f"Batch {n} of `{program.train_branch(cfg)}` at `{head[:12]}`: every item below merged into that branch after its own review. "
        f"The required checks on this pull request are the first run of the full checks on them together.",
        "",
    ]
    lines += [f"- `{e['id']}` {e['title']}" + (f" (#{e['pr']})" if e.get("pr") else "") + f", merged as `{str(e['merge_sha'])[:12]}`" for e in held]
    return title[:240], "\n".join(lines) + "\n"


def _open_pr(tools, clone, repo: str, branch: str, base: str, title: str, body: str) -> dict:
    """The batch's pull request: the one already open from its branch, or a new one."""
    found = _gh_json(tools, clone, ["pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", "number,url"])
    if found:
        return {"number": found[0]["number"], "url": found[0].get("url")}
    made = _gh_json(tools, clone, ["api", "--method", "POST", f"repos/{repo}/pulls", "-f", f"title={title}", "-f", f"head={branch}", "-f", f"base={base}", "-f", f"body={body}"])
    return {"number": made["number"], "url": made.get("html_url")}


def _unfinished(record: dict | None) -> str | None:
    """Why nothing but `promote` may touch a batch GitHub has already merged."""
    if record and record["state"] == "landed":
        return f"batch {record['n']} is merged into the base and its records are not finished: `promote` finishes them"
    return None


def _conflict(root, repo: str, cfg: dict, took: dict, n: int) -> dict:
    """The base no longer merges into the train: say so, and write the item that brings it in."""
    base, branch = cfg["base"], program.train_branch(cfg)
    files = ", ".join(f"`{f}`" for f in took["conflict"])
    item_id = f"{CONFLICT_PREFIX}-{n}"
    path = state_dir(root, repo) / f"conflict-{n}-ITEMS.md"
    path.write_text(
        f"## {item_id} — Bring {base} into {branch}\n\n"
        f"**Scope.** `{base}` has moved to `{took['base_sha'][:12]}` and no longer merges cleanly into `{branch}`, the branch this program's items are gathered "
        f"on. These files conflict: {files}. On your branch, which is cut from `{branch}`, fetch and merge `origin/{base}` (`git fetch origin {base}`, "
        f"`git merge origin/{base}`) and resolve each conflict so the file holds what both sides meant: the change that reached `{base}` and the items' own. "
        "Commit the merge as a merge: do not rebase and do not squash it, because the branch has to hold "
        f"`{base}`'s history for the two to count as joined. Change nothing else, and take neither side whole unless the other's change is truly gone from the product.\n\n"
        "**Accept.**\n"
        f"1. `git merge-base --is-ancestor origin/{base} HEAD` exits 0 on this branch.\n"
        "2. The tests that cover the files named above pass.\n"
    )
    return {
        "result": "conflict",
        "files": took["conflict"],
        "detail": f"{base} moved to {took['base_sha'][:12]} and does not merge cleanly into {branch}",
        "repair_file": str(path),
        "repair_id": item_id,
        "next": (
            f"run that file as an item in a run of its own (`hl-items init --run-dir {Path(root) / 'runs' / f'conflict-{n}'} --root {root} --stream <stream> "
            f"--run-id conflict-{n} --repo {repo} --file {path} --work \"bring {base} into the train\" --repair --land merge`), then test again. "
            "`--land merge` matters: a squash would put the resolved files on the train and leave the two histories apart, and the same conflict would come back"
        ),
    }


def test(root, repo: str, clone, tools, note: str | None = None) -> tuple[int, dict]:
    with one_command(root, repo):
        return _test(root, repo, clone, tools, note)


def _test(root, repo: str, clone, tools, note: str | None) -> tuple[int, dict]:
    cfg = program.load(root)
    settings(cfg, repo)
    release_left(root, repo, clone, tools)
    settle(root, repo)
    state = load_state(root, repo)
    opened = _open_batch(root, repo, state)
    if _unfinished(opened):
        raise Refusal(_unfinished(opened))
    if opened and opened["state"] == "testing":
        raise Refusal(f"batch {opened['n']} is being tested (pull request #{opened['pr']}): `promote` it when its checks have finished, or `abandon` it")
    base = cfg["base"]
    branch = ensure(cfg, repo, clone, tools)
    mend(root, repo, clone, tools)

    took = bring_base_in(cfg, repo, clone, tools)
    if took.get("conflict"):
        # Numbered by themselves: two conflicts can come before any batch is opened.
        k = update_state(root, repo, lambda st: st.update(conflicts=st.get("conflicts", 0) + 1))["conflicts"]
        return 4, _conflict(root, repo, cfg, took, k)
    head, base_sha = _sha(tools, clone, _ref(branch)), _sha(tools, clone, _ref(base))
    waiting = unpromoted(root, repo)
    held = in_batch(tools, clone, base_sha, head, waiting)
    reasons = not_in_batch(root, repo, clone, tools, cfg, head, base_sha, [e for e in waiting if e not in held])
    if reasons:
        raise Refusal(reasons)
    if not held:
        raise Refusal(f"nothing has merged into {branch} since the last promotion: there is nothing to test")
    state = load_state(root, repo)
    strangers = not_ours(tools, clone, base_sha, head, {e["merge_sha"] for e in landed(root, repo)} | set(state["merge_backs"]))
    if strangers:
        raise Refusal(
            f"{branch} carries commits this loop did not merge: {'; '.join(strangers)}. Nothing is tested or promoted with them on it. "
            "(An item's merge that is being recorded this moment reads the same way: run this once more before anything else.)"
        )

    # A batch that was being opened when this script died is taken up again under its own number.
    n = opened["n"] if opened and opened["state"] == "opening" else state["batches"] + 1
    pinned = program.batch_branch(cfg, n)
    record = {
        "n": n,
        "state": "opening",
        "head": head,
        "base_sha": base_sha,
        "branch": pinned,
        "items": [{k: e[k] for k in ("run_dir", "run", "id", "title", "pr", "merge_sha")} for e in held],
        "opened_at": utc_now(),
        "note": note,
    }
    write_json(batch_path(root, repo, n), record)
    update_state(root, repo, lambda st: st.update(batches=max(st["batches"], n), open=n))

    pushed = tools.git(["push", "--force", "origin", f"{head}:refs/heads/{pinned}"], clone, check=False)
    if pushed.returncode != 0:
        raise Refusal(f"could not push {pinned}: {pushed.stderr.strip()[:300]}")
    title, body = _batch_text(cfg, repo, n, head, held)
    found = merge._attribution("the batch's pull request", title + "\n" + body)
    if found:
        raise Refusal(found)
    pull = _open_pr(tools, clone, repo, pinned, base, title, body)
    record.update(state="testing", pr=pull["number"], url=pull["url"])
    write_json(batch_path(root, repo, n), record)
    return 0, {
        "result": "testing",
        "batch": n,
        "pr": pull["number"],
        "url": pull["url"],
        "head": head,
        "items": [e["id"] for e in held],
        **({"brought_in": took["brought_in"]} if took.get("brought_in") else {}),
        "next": f"hl-wait --root {root} --train {repo}, then `hl-train … promote`",
    }


# ---------------------------------------------------------------- promote


def batch_checks(tools, clone, repo: str, record: dict) -> tuple[str, list[dict]]:
    """`green`, `red`, `running` or `none` for the batch's required checks, and the checks."""
    checks = merge.required_checks(tools, clone, repo, record["pr"])
    if not checks or all(c.get("state") == "NOT_REPORTED" for c in checks):
        # GitHub has heard from none of them yet.
        return "none", checks
    if any(c.get("bucket") not in FINISHED_BUCKETS for c in checks):
        return "running", checks
    return ("green" if all(c.get("bucket") in GREEN_BUCKETS for c in checks) else "red"), checks


def _close_batch(root, repo: str, clone, tools, record: dict, state_name: str, **more) -> dict:
    """End a batch that will not be promoted: its pull request closed, its
    branch removed. Never one GitHub has merged: that batch is on the base,
    and giving it up here would leave its items on the base and off the
    records for good."""
    if record.get("pr"):
        seen = _gh_json(tools, clone, ["pr", "view", str(record["pr"]), "--repo", repo, "--json", "state"]) or {}
        if seen.get("state") == "MERGED":
            raise Refusal(f"the batch's pull request #{record['pr']} is merged, so the batch cannot be given up: `promote` records it")
        tools.gh(["api", "--method", "PATCH", f"repos/{repo}/pulls/{record['pr']}", "-f", "state=closed"], clone, check=False)
    tools.git(["push", "origin", "--delete", record["branch"]], clone, check=False)
    record.update(state=state_name, closed_at=utc_now(), **more)
    write_json(batch_path(root, repo, record["n"]), record)
    _closed(root, repo, record)
    return record


def _paths(tools, clone, sha: str) -> list[str]:
    return tools.git(["diff-tree", "--no-commit-id", "--name-only", "-r", sha], clone, check=False).stdout.split("\n")[:-1]


def repair(root, repo: str, clone, tools) -> tuple[int, dict]:
    """Give up the open batch because its required checks failed, and write
    the item that repairs it. Refused while the checks are anything but red:
    a batch is not closed on a guess."""
    with one_command(root, repo):
        return _repair(root, repo, clone, tools)


def _repair(root, repo: str, clone, tools) -> tuple[int, dict]:
    cfg = program.load(root)
    settings(cfg, repo)
    release_left(root, repo, clone, tools)
    settle(root, repo)
    record = _open_batch(root, repo, load_state(root, repo))
    if _unfinished(record):
        raise Refusal(_unfinished(record))
    if not record or record["state"] != "testing":
        raise Refusal("no batch is being tested: there is nothing to repair")
    how, checks = batch_checks(tools, clone, repo, record)
    if how != "red":
        ways = {"green": "green: `promote` it", "running": "still running: wait for them", "none": "not reported"}
        raise Refusal(f"the required checks of batch {record['n']} are {ways[how]}. A repair is written for a batch whose checks have failed")
    return 0, _red(root, repo, clone, tools, cfg, record, checks)


def _red(root, repo: str, clone, tools, cfg: dict, record: dict, checks: list[dict]) -> dict:
    """A batch whose checks failed: what failed, for whoever repairs it, and a one-item backlog for the repair."""
    from . import wait  # for the same reading of a failed check's log that an item's rework gets

    n, branch = record["n"], program.train_branch(cfg)
    sdir = state_dir(root, repo)
    failed = [c for c in checks if c.get("bucket") not in GREEN_BUCKETS]
    lines = [f"# Batch {n} of {repo}: the required checks failed", "", f"Read at {utc_now()}, on commit `{record['head']}` of `{branch}` (pull request #{record['pr']}).", ""]
    for check in failed:
        link = check.get("link") or ""
        run = wait.RUN_ID_RE.search(link)
        log = f" Its log: `gh run view {run.group(1)} --repo {repo} --log-failed`." if run else ""
        lines.append(f"- `{check['name']}`: {check.get('bucket') or check.get('state')}. {link}{log}".rstrip())
    green = sorted(c["name"] for c in checks if c.get("bucket") in GREEN_BUCKETS)
    lines += (["", "Green: " + ", ".join(f"`{name}`" for name in green) + "."] if green else [])
    lines += ["", "## What the batch holds, oldest first", ""]
    for held in record["items"]:
        paths = _paths(tools, clone, held["merge_sha"])
        shown = ", ".join(f"`{p}`" for p in paths[:12]) + (f" and {len(paths) - 12} more" if len(paths) > 12 else "")
        lines.append(f"- `{held['id']}` {held['title']} (`{held['merge_sha'][:12]}`): {shown or 'no paths read'}")
    red_text = "\n".join(lines) + "\n"
    detail = sdir / f"batch-{n}-red.md"
    detail.write_text(red_text)
    red_file(root, repo).write_text(red_text)

    repair_id = f"{REPAIR_PREFIX}-{n}"
    names = ", ".join(f"`{c['name']}`" for c in failed)
    accept = [f"{i}. What failed in the check `{c['name']}` passes on this branch: run what its log names as failed and see it pass." for i, c in enumerate(failed[:10], 1)]
    repair = sdir / f"repair-{n}-ITEMS.md"
    repair.write_text(
        f"## {repair_id} — Repair batch {n} of {repo}\n\n"
        f"**Scope.** The repository's full checks failed on its integration branch `{branch}` at `{record['head'][:12]}`, on a batch of items that each "
        f"passed a review of its own. These failed: {names}. What failed, how to read each log, and what the batch holds are in `{detail}`. "
        "Find what makes them fail and put it right with the smallest change that does. The cause may be one item, or two that do not fit together. "
        "Keep what the items did wherever the failure does not involve it. If an item's change is wrong in itself, name the item in your report under "
        "`## Proposed items`. Nothing is deployed from this branch; the batch is tested again once this has merged.\n\n"
        "**Accept.**\n" + "\n".join(accept) + "\n"
    )
    record = _close_batch(root, repo, clone, tools, record, "red", failed=[c["name"] for c in failed])
    return {
        "result": "repair",
        "batch": n,
        "failed": [c["name"] for c in failed],
        "red_file": str(detail),
        "repair_file": str(repair),
        "repair_id": repair_id,
        "next": (
            f"read {detail.name} and the failed logs; run the repair as an item of its own (`hl-items init --run-dir {Path(root) / 'runs' / f'repair-{n}'} "
            f"--root {root} --stream <stream> --run-id repair-{n} --repo {repo} --file {repair} --work \"repair batch {n}\" --repair`), amending its Accept "
            "first if you can name the failing tests; then test again. Items go on merging into the train meanwhile unless you decide to hold them"
        ),
    }


def _record_promotion(root, repo: str, clone, tools, cfg: dict, record: dict, promoted_sha: str | None = None, recovered: bool = False) -> dict:
    """Write down a promotion GitHub has made. It can be cut short at any
    line and taken up again by the next `promote`: the batch is marked as
    landed first, each item is marked once, and the batch stays the open one
    until everything is on record."""
    n, base = record["n"], cfg["base"]
    if record["state"] != "landed":
        if not promoted_sha:
            named = tools.gh(["api", f"repos/{repo}/pulls/{record['pr']}", "--jq", ".merge_commit_sha"], clone, check=False)
            promoted_sha = named.stdout.strip() if named.returncode == 0 and len(named.stdout.strip()) == 40 else None
        fetch(tools, clone, base)
        # The lock keeps this loop's own merges apart. A push to the base by something else, in the moment between the last
        # look and GitHub's merge, cannot be stopped from here; it can be seen, and is said.
        tested = _tree(tools, clone, record["head"])
        same = bool(tested) and _tree(tools, clone, promoted_sha) == tested
        record.update(state="landed", promoted_sha=promoted_sha, promoted_at=utc_now(), tree_is_the_tested_one=same, **({"recovered": True} if recovered else {}))
        write_json(batch_path(root, repo, n), record)
    promoted_sha, at = record["promoted_sha"], record["promoted_at"]
    for held in record["items"]:
        if items.load_item(held["run_dir"], held["id"])["fields"].get("promoted") != str(n):
            items.record_promoted(held["run_dir"], held["id"], n, promoted_sha or "", at)
    landed_msg = tools.gh(["api", f"repos/{repo}/commits/{promoted_sha}", "--jq", ".commit.message"], clone, check=False)
    # Nothing can take it back now; the manager files it and tells the operator.
    record.update(state="promoted", attribution_on_base=merge._attribution("the merged commit", landed_msg.stdout) if landed_msg.returncode == 0 else None)
    write_json(batch_path(root, repo, n), record)
    _closed(root, repo, record)
    # GitHub removes the branch of a merged pull request where the repository says so; elsewhere this does.
    tools.git(["push", "origin", "--delete", record["branch"]], clone, check=False)
    out = {
        "result": "promoted",
        "batch": n,
        "promoted_sha": promoted_sha,
        "items": [held["id"] for held in record["items"]],
        "attribution_on_base": record["attribution_on_base"],
        "tree_is_the_tested_one": record["tree_is_the_tested_one"],
        "verify": [{"run_dir": held["run_dir"], "id": held["id"]} for held in record["items"] if items.load_item(held["run_dir"], held["id"]).get("verify_command")],
    }
    if not record["tree_is_the_tested_one"]:
        out["warning"] = (
            f"{base} moved in the moment the batch was merged: its tree is not the tree the checks ran on. Nothing here can undo that. "
            "Tell the operator at once, with the commit that was pushed to it"
        )
    try:
        out["train"] = bring_base_in(cfg, repo, clone, tools)
    except Refusal as late:
        # The promotion stands; the next test brings the base in before it pins anything.
        out["train"] = {"not_brought_in": str(late)}
    out["ahead"] = len(unpromoted(root, repo))
    return out


def release_left(root, repo: str, clone, tools) -> str | None:
    """Free the merge lock a promotion that died left held."""
    path = state_dir(root, repo) / LOCK_FILE
    if not path.exists():
        return None
    token = read_json(path)
    freed = merge.release_lock(tools, clone, token)
    path.unlink()
    return f"released {token['held_sha'][:12]}" if freed else "the lock a dead promotion held was no longer its own"


def promote(root, repo: str, clone, tools, opts) -> tuple[int, dict]:
    with one_command(root, repo):
        return _promote(root, repo, clone, tools, opts)


def _promote(root, repo: str, clone, tools, opts) -> tuple[int, dict]:
    cfg = program.load(root)
    settings(cfg, repo)
    # Only one hl-train command runs for a repository at a time, so a lock still on file is a dead promotion's.
    release_left(root, repo, clone, tools)
    settle(root, repo)
    state = load_state(root, repo)
    record = _open_batch(root, repo, state)
    if record and record["state"] == "landed":
        # GitHub merged it and the records were cut short: finish them.
        return 0, _record_promotion(root, repo, clone, tools, cfg, record)
    if not record or record["state"] != "testing":
        raise Refusal("no batch is being tested: `hl-train … test` opens one")
    base, branch = cfg["base"], program.train_branch(cfg)
    n, head = record["n"], record["head"]
    subject = f"{cfg['program']}: batch {n} ({', '.join(held['id'] for held in record['items'])})"[:240]
    body = f"Batch {n} of {branch}, tested at {head[:12]}.\n"

    def look() -> tuple[dict, list[str], tuple[int, dict] | None]:
        """Every condition of a promotion. Returns the pull request, the reasons against, and an outcome that ends this call without one."""
        view = merge._pr_view(tools, clone, repo, record["pr"])
        if view["state"] == "MERGED":
            landed_as = (view.get("mergeCommit") or {}).get("oid")
            fetch(tools, clone, base)
            on_base = bool(landed_as) and _is_ancestor(tools, clone, landed_as, _sha(tools, clone, _ref(base)))
            if view["headRefOid"] != head or view.get("baseRefName") != base or not on_base:
                raise Refusal(
                    f"the batch's pull request #{record['pr']} was merged outside this script, and not as the commit that was tested ({head[:12]}) "
                    f"into {base}: it is {view['headRefOid'][:12]} into {view.get('baseRefName')}, merged as {str(landed_as)[:12]}. "
                    "Nothing here records that as a promotion: tell the operator"
                )
            return view, [], (0, _record_promotion(root, repo, clone, tools, cfg, record, landed_as, recovered=True))
        if view["state"] != "OPEN":
            raise Refusal(f"the batch's pull request #{record['pr']} is {view['state']}: it was closed by something else. `abandon` the batch and test again")
        reasons = []
        if view["headRefOid"] != head or view.get("baseRefName") != base:
            reasons.append(f"the batch's pull request is {view['headRefOid'][:12]} into {view.get('baseRefName')}; {head[:12]} into {base} was tested")
        if view.get("isDraft") or view.get("autoMergeRequest"):
            reasons.append("the batch's pull request is a draft or is armed to merge itself")
        how, checks = batch_checks(tools, clone, repo, record)
        if how == "none" and not opts.allow_no_required_checks:
            reasons.append("no required checks are reported for the batch's pull request")
        if how == "running":
            return view, reasons, (5, {"result": "pending", "batch": n, "pr": record["pr"], "checks": [{"name": c["name"], "bucket": c.get("bucket")} for c in checks]})
        if how == "red":
            failed = [c for c in checks if c.get("bucket") not in GREEN_BUCKETS]
            said = {
                "result": "red",
                "batch": n,
                "pr": record["pr"],
                "failed": [c["name"] for c in failed],
                "links": [c.get("link") for c in failed if c.get("link")],
                "next": (
                    "read what failed. For a reason that is not the batch's (a runner lost, an outage, a check that says it is being retried): run it "
                    "again once, or wait, and promote again. Failed in its own right: `hl-train … repair` closes the batch and writes its repair"
                ),
            }
            return view, reasons, (4, said)
        fetch(tools, clone, base, branch)
        base_sha = _sha(tools, clone, _ref(base))
        if not _is_ancestor(tools, clone, base_sha, head):
            _close_batch(root, repo, clone, tools, record, "abandoned", note=f"{base} moved to {base_sha[:12]} after the test")
            return view, reasons, (6, {"result": "base-moved", "batch": n, "base_sha": base_sha, "next": "test again: the next batch takes the base in first"})
        strangers = not_ours(tools, clone, base_sha, head, {e["merge_sha"] for e in landed(root, repo)} | set(load_state(root, repo)["merge_backs"]))
        if strangers:
            reasons.append(f"the batch carries commits this loop did not merge: {'; '.join(strangers)}")
        reasons += merge.commit_attribution(tools, clone, base_sha, head)
        found = merge._attribution("the merge message", subject + "\n" + body)
        if found:
            reasons.append(found)
        # A promotion whose deploy check has not passed may be a red deploy. Nothing else lands on top of it.
        reasons += held_up_by_verify(root, repo, record["items"])
        state = view.get("mergeStateStatus")
        # By here every required check GitHub lists has finished green, or it lists none. A draft is blocked
        # for being one, and is refused as one above.
        unexplained = not view.get("isDraft")
        if state == "BLOCKED" and unexplained:
            state = merge.settled_merge_state(tools, clone, repo, record["pr"])
        if state == "DIRTY":
            reasons.append("the batch's pull request conflicts with its base")
        elif state == "BLOCKED" and unexplained:
            reasons.append(merge.blocked_reason("the batch's pull request", checks))
        return view, reasons, None

    # Everything slow happens before the lock: the lock covers the merge only.
    view, reasons, ended = look()
    if ended:
        return ended
    if reasons:
        raise Refusal(reasons)
    if opts.dry_run:
        return 0, {"result": "dry-run-passed", "batch": n, "pr": record["pr"], "head": head}

    try:
        token = merge.acquire_lock(tools, clone, f"train:{cfg['program']}", f"batch-{n}", ref=opts.lock_ref, wait_s=opts.lock_wait, poll_s=opts.lock_poll)
    except merge.LockTimeout as held:
        return 3, {"result": "lock-timeout", "holder": held.holder, "detail": str(held)}
    token_path = state_dir(root, repo) / LOCK_FILE
    write_json(token_path, token)
    try:
        # Again, under the lock: a check that turned red, or a base that moved, while this run waited.
        view, reasons, ended = look()
        if ended:
            return ended
        if reasons:
            raise Refusal(reasons)
        merged = tools.gh(["pr", "merge", str(record["pr"]), "--repo", repo, "--merge", "--match-head-commit", head, "--subject", subject, "--body", body], clone, check=False)
        if merged.returncode != 0:
            record.update(merge_failed=merged.stderr.strip()[:600], merge_failed_at=utc_now())
            write_json(batch_path(root, repo, n), record)
            return 2, {"result": "refused", "refused": [f"GitHub did not merge the batch's pull request: {merged.stderr.strip()[:400]}"]}
        deadline = time.monotonic() + opts.merge_wait
        while True:
            seen = json.loads(tools.gh(["pr", "view", str(record["pr"]), "--repo", repo, "--json", "state,mergeCommit"], clone).stdout)
            if seen["state"] == "MERGED":
                break
            if seen["state"] == "CLOSED" or time.monotonic() >= deadline:
                return 2, {"result": "refused", "refused": [f"the merge of pull request #{record['pr']} was not seen to complete (state {seen['state']}); run `promote` again"]}
            tools.sleep(opts.merge_poll)
        return 0, _record_promotion(root, repo, clone, tools, cfg, record, (seen.get("mergeCommit") or {}).get("oid"))
    finally:
        merge.release_lock(tools, clone, token)
        token_path.unlink(missing_ok=True)


def abandon(root, repo: str, clone, tools, note: str) -> tuple[int, dict]:
    with one_command(root, repo):
        return _abandon(root, repo, clone, tools, note)


def _abandon(root, repo: str, clone, tools, note: str) -> tuple[int, dict]:
    program.load(root)
    release_left(root, repo, clone, tools)
    settle(root, repo)
    state = load_state(root, repo)
    record = _open_batch(root, repo, state)
    if not record:
        raise Refusal("no batch is open")
    if _unfinished(record):
        raise Refusal(_unfinished(record))
    if not (note or "").strip():
        raise Refusal("--note is required: say why the batch is given up")
    record = _close_batch(root, repo, clone, tools, record, "abandoned", note=note.strip())
    return 0, {"result": "abandoned", "batch": record["n"], "pr": record.get("pr")}


# ---------------------------------------------------------------- cli


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-train", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=absolute, required=True, help="the program's loop root")
    p.add_argument("--repo", required=True, metavar="OWNER/NAME")
    p.add_argument("--repo-dir", type=absolute, help="a clone of the repository (default: a stream's clone, from program.json)")
    p.add_argument("--lock-ref", default=merge.LOCK_REF)
    p.add_argument("--lock-wait", type=float, default=merge.LOCK_WAIT_S)
    p.add_argument("--lock-poll", type=float, default=merge.LOCK_POLL_S)
    p.add_argument("--merge-wait", type=float, default=merge.MERGE_WAIT_S)
    p.add_argument("--merge-poll", type=float, default=merge.MERGE_POLL_S)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="what has merged into the train and not been promoted, and whether a test is due")
    s = sub.add_parser("test", help="open a batch on the train's head: its pull request into the base starts the required checks")
    s.add_argument("--note", help="why now, when it is not because a batch is due")
    s = sub.add_parser("promote", help="merge the batch into the base when its checks are green; say which are red when they are not")
    s.add_argument("--dry-run", action="store_true", help="check every condition and stop before the lock")
    s.add_argument("--allow-no-required-checks", action="store_true", help="for a repository whose base requires no checks")
    sub.add_parser("repair", help="close a batch whose checks failed, and write what failed and the item that repairs it")
    s = sub.add_parser("abandon", help="give up the open batch: its pull request is closed and nothing is promoted")
    s.add_argument("--note", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    opts = _parser().parse_args(argv)
    try:
        merge.require_loop_token(opts.root)
        pinned = claim.check_pins(opts.root, ("git", "gh"))["bins"]
        tools = merge.Tools(git=pinned["git"], gh=pinned["gh"])
        clone = clone_of(program.load(opts.root), opts.repo, opts.repo_dir)
        if opts.cmd == "status":
            code, result = 0, status(opts.root, opts.repo, clone, tools)
        elif opts.cmd == "test":
            code, result = test(opts.root, opts.repo, clone, tools, opts.note)
        elif opts.cmd == "promote":
            code, result = promote(opts.root, opts.repo, clone, tools, opts)
        elif opts.cmd == "repair":
            code, result = repair(opts.root, opts.repo, clone, tools)
        else:
            code, result = abandon(opts.root, opts.repo, clone, tools, opts.note)
    except Refusal as refusal:
        emit({"result": "refused", "refused": refusal.reasons})
        print(f"hl-train: refused: {refusal}", file=sys.stderr)
        return 2
    except RuntimeError as failure:
        emit({"result": "error", "detail": str(failure)[:400]})
        print(f"hl-train: {failure}", file=sys.stderr)
        return 2
    emit(result)
    return code


if __name__ == "__main__":
    sys.exit(main())
